"""End-to-end HTTP tests for the Slice 3 named denial demonstrations (task 17.2).

Each test drives the fully-composed FastAPI app — built by
:func:`walking_slice.app.create_app` — through :class:`httpx.AsyncClient`
over the ASGI transport, exercising one of the seven denial scenarios
named in the task statement:

1. **Contributor attempting a Work Assignment is denied with an
   AD-WS-9-shaped response and a Denial Record** (Requirements 30.1,
   32.6). A Party holding only ``contribute`` authority submits a
   Work Assignment; ``create.work_assignment`` requires ``assign``
   per AD-WS-24 so the Authorization_Service rejects the attempt.
   The response carries *only* the AD-WS-9 trio
   (``generic_denial_indicator``, ``reason_code``,
   ``correlation_id``) and a Denial Record with
   ``action_type='create.work_assignment'`` is appended to
   ``Audit_Records`` in a separate transaction.

2. **Completion-Authority Party attempting a Milestone Acceptance is
   denied** (Requirements 30.1, 32.8). A Party holding only
   ``complete`` authority submits a Milestone Acceptance;
   ``create.milestone_acceptance`` requires ``accept_milestone`` per
   AD-WS-15 so the Authorization_Service rejects the attempt. The
   response is the AD-WS-9 trio and a Denial Record is appended.

3. **Party with ``contribute`` authority but not the named assignee
   is denied with ``no-role-assignment``** (Requirements 30.4, 32.7).
   A Party holds ``contribute`` authority on the scope but is not
   the named assignee on the target Work Assignment, so the
   AD-WS-29 assignee-binding check rejects the attempt with
   ``reason_code='no-role-assignment'``. The response shape is
   indistinguishable from the authority-evaluation deny path.

4. **Modifying a finalized Completion Record is rejected and the
   original row is byte-equivalent** (Requirement 29.3, 29.7, 37.5,
   AD-WS-27). A second Completion submission against the same Plan
   Revision — the closest HTTP-level analog of "modifying a
   finalized Completion Record" — is rejected with HTTP 409 and
   ``error_code='completion_already_exists'``. The original
   Completion Record row remains byte-equivalent and no second row
   persists. The AD-WS-27 UPDATE/DELETE triggers additionally
   reject any direct SQL mutation attempt against the finalized
   row.

5. **Submitting a Work Assignment with a ``planned-`` attribute is
   rejected with no row persisted and no Slice 2 row mutated**
   (Requirements 33.4, 40.3). The Execution_Service's
   prohibited-attribute screen fires before the Pydantic
   ``extra='forbid'`` guard so the response carries
   ``failed_constraint='prohibited_attribute'`` and the prohibited
   key in ``prohibited_keys``. No ``Work_Assignment_Records`` row is
   persisted and every Slice 2 row in the chain remains
   byte-equivalent (Property 35 — Plan/Execution separation).

6. **Submitting a Completion with an ``observed-`` attribute is
   rejected** (Requirements 34.5, 39.6). The same prohibited-
   attribute screen catches observed-outcome prefixes; no
   ``Completion_Records`` row is persisted (Property 36 — Output/
   Outcome separation).

7. **Submitting a Completion against a non-existent Plan Revision is
   indistinguishable from submitting against a restricted Plan
   Revision the caller cannot view** (Requirements 30.5, 30.7,
   AD-WS-9 rule 1). Neither response leaks any attribute of the
   target Plan Revision the caller did not already supply on the
   request body.

Authentication threads through the temporary ``X-Actor-Party-Id``
header carried until the bearer-token middleware lands; the header
carries the same Party Identity the body field names so the wire
contract is uniform across endpoints.

Validates: Requirements 30.1, 30.4, 30.5, 30.7, 32.7, 33.4, 34.5, 37.5.
"""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.app import SliceServices, create_app
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Party Identities.
#
# Each Party plays one role in the denial demonstrations. Identifier
# opacity (Slice 1 Requirement 1.7) means the actual UUIDv7 values are
# not the focus of the tests — they only need to be valid canonical
# UUIDv7 strings the persistence layer accepts.
# ---------------------------------------------------------------------------


# Pipeline-author Party: holds every authority needed to build the
# Slice 1 + Slice 2 chain and to record the seed Work Assignment used by
# demos 3 and 4. The wildcard scope keeps the chain-creation calls
# simple; the named denial demonstrations each exercise a *different*
# Party so the rejection behaviour is visible.
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-0000000d3001"

# Contributor-only Party for demo 1: holds ``contribute`` (and ``view``
# / ``modify`` for chain reads) but NOT ``assign``. Attempts to create
# a Work Assignment — should be denied (AD-WS-9).
_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-0000000d3002"

# Completion-Authority Party for demo 2: holds ``complete`` (and
# ``view``) but NOT ``accept_milestone``. Attempts a Milestone
# Acceptance — should be denied.
_COMPLETION_ONLY_PARTY_ID = "00000000-0000-7000-8000-0000000d3003"

# Named assignee Party for the seeded Work Assignment used by demos 3
# and 4. Holds ``contribute`` so it can record Work Events / Time
# Entries / produced Deliverables / Deliverable Productions under the
# AD-WS-29 assignee-binding contract.
_ASSIGNEE_PARTY_ID = "00000000-0000-7000-8000-0000000d3004"

# Stranger-Contributor Party for demo 3: holds ``contribute`` but is
# *not* the named assignee on the seeded Work Assignment, so the
# AD-WS-29 assignee-binding check should reject with
# ``reason_code='no-role-assignment'``.
_STRANGER_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-0000000d3005"

# Milestone-Acceptance + Completion Authority Party: holds the two
# higher-level authorities needed to complete the demo-4 setup.
_ACCEPTING_AUTHORITY_PARTY_ID = "00000000-0000-7000-8000-0000000d3006"

# Unauthorized Party for demo 7: holds no role at all. Drives the
# "restricted Plan Revision the caller cannot view" half of the
# indistinguishable-denial pair.
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-0000000d3007"

# Resource-steward identity recorded as ``assigning_authority_id`` on
# every Role Assignment. Identifier opacity means this value never
# reaches the API surface; the column just needs a valid Party row.
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-0000000d3008"


# ---------------------------------------------------------------------------
# Shared scope and authority basis constants.
# ---------------------------------------------------------------------------


# One scope string covers every creation request body. Each role
# assignment seeded below uses the wildcard scope (``"*"``) so the
# exact string here only ever lands on persisted rows.
_SCOPE = "slice3-denial-demo/pilot"

# Authority basis identifier used on every authority-bearing record.
# AD-WS-10 lists three accepted ``type`` values; ``role-grant-id`` is
# the most common and is chosen here for uniformity. The basis is a
# stable constant per AD-WS-10 and is not the focus of these tests.
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000d30a1")


# Pin every ``recorded_at`` so the asserted response bodies are
# deterministic across runs. The instant sits inside the slice's
# pilot horizon.
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# Document content used to seed the Slice 1 leg of the pipeline. The
# span offsets target the substring ``"quick brown fox"`` so the
# byte-equivalence assertions in any downstream provenance walk are
# grep-friendly.
_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # exclusive end of "quick brown fox"


# Produced-Deliverable content used by demos 4 and the supporting
# pipeline. Small and well below the 100 MB cap.
_DELIVERABLE_BYTES = b"Slice 3 denial demonstrations: sample deliverable body."


# Plan Revision attribute values seeded into demo 7's restricted-target
# universe so the leak-test assertion has explicit substrings to scan
# the denial responses for.
_PR_PLANNED_SCOPE = (
    "Draft, review, and publish the slice-3 denial fox playbook "
    "across the pilot horizon."
)
_PR_ORDERING_RATIONALE = "Iteration two depends on iteration-one feedback."
_PR_ASSUMPTION_ONE = "Two team members can co-author each iteration."
_PR_ASSUMPTION_TWO = "Steering committee reviews within five business days."


# ---------------------------------------------------------------------------
# Engine + Party seeding helpers.
# ---------------------------------------------------------------------------


def _build_engine(tmp_path: Path) -> Engine:
    """Construct a per-test on-disk SQLite engine with the slice's pragmas."""
    sqlite_path = tmp_path / "walking_slice.sqlite"
    url = f"sqlite:///{sqlite_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    return engine


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert one ``Parties`` row directly via SQL.

    The composed app does not seed Parties on startup — Party rows are a
    domain concern, not a bootstrap concern — so the test seeds the
    Parties it needs before driving the HTTP layer.
    """
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": "2026-01-01T00:00:00.000Z"},
    )


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    role_name: str,
    authorities: tuple[str, ...],
    scope: str,
) -> None:
    """Insert one ``Role_Assignments`` row via the wired AuthorizationService."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=authorities,
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_PARTY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def composed_app(tmp_path: Path) -> FastAPI:
    """A fully-composed FastAPI app with every demo Party seeded.

    The pipeline-author Party is granted every authority Slice 1 +
    Slice 2 + Slice 3 needs (``view``, ``modify``, ``review``,
    ``approve``, ``assign``, ``contribute``, ``accept_milestone``,
    ``complete``) on the wildcard scope so the chain-creation calls
    succeed without re-shuffling role assignments mid-test. Each demo
    Party holds exactly the authorities its scenario requires so the
    rejection behaviour under test is the *only* observable difference
    between the would-be authorized and unauthorized actors.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"slice3-denial-demonstrations-test-secret",
    )
    with engine.begin() as conn:
        _seed_party(conn, _AUTHOR_PARTY_ID, "Slice 3 Pipeline Author")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor Only")
        _seed_party(conn, _COMPLETION_ONLY_PARTY_ID, "Completion Authority Only")
        _seed_party(conn, _ASSIGNEE_PARTY_ID, "Named Assignee Contributor")
        _seed_party(
            conn, _STRANGER_CONTRIBUTOR_PARTY_ID, "Stranger Contributor"
        )
        _seed_party(
            conn,
            _ACCEPTING_AUTHORITY_PARTY_ID,
            "Milestone Acceptance + Completion Authority",
        )
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")

    services: SliceServices = app.state.services
    # Pipeline author: every authority on every scope so the chain seed
    # never trips an authorization check.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="slice3-pipeline-author",
        authorities=(
            "view",
            "modify",
            "review",
            "approve",
            "assign",
            "contribute",
            "accept_milestone",
            "complete",
        ),
        scope="*",
    )
    # Demo 1: Contributor with ``contribute`` (+ ``view``) but no
    # ``assign``. Attempts to record a Work Assignment — denied.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_CONTRIBUTOR_PARTY_ID,
        role_name="slice3-contributor-only",
        authorities=("view", "contribute"),
        scope="*",
    )
    # Demo 2: Completion Authority with ``complete`` (+ ``view``) but
    # no ``accept_milestone``. Attempts a Milestone Acceptance — denied.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_COMPLETION_ONLY_PARTY_ID,
        role_name="slice3-completion-only",
        authorities=("view", "complete"),
        scope="*",
    )
    # Named assignee Party: holds ``contribute`` (+ ``view``) so the
    # demo-4 Slice 3 chain can be seeded under its identity. The
    # AD-WS-29 assignee-binding check passes for this Party because the
    # seeded Work Assignment names it as the assignee.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_ASSIGNEE_PARTY_ID,
        role_name="slice3-named-assignee",
        authorities=("view", "contribute"),
        scope="*",
    )
    # Demo 3: Stranger Contributor with ``contribute`` (+ ``view``) but
    # is NOT the named assignee on the seeded Work Assignment. The
    # AD-WS-29 assignee-binding check should reject with
    # ``reason_code='no-role-assignment'``.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_STRANGER_CONTRIBUTOR_PARTY_ID,
        role_name="slice3-stranger-contributor",
        authorities=("view", "contribute"),
        scope="*",
    )
    # Milestone Acceptance + Completion Authority. Used by demo 4 to
    # seed the finalized Completion that the demo then attempts to
    # "modify" via a duplicate submission.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_ACCEPTING_AUTHORITY_PARTY_ID,
        role_name="slice3-accepting-authority",
        authorities=("view", "accept_milestone", "complete"),
        scope="*",
    )
    # Unauthorized Party (demo 7) holds no role assignment.
    return app


@pytest_asyncio.fixture
async def client(composed_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An :class:`httpx.AsyncClient` bound to the composed app via ASGI."""
    transport = ASGITransport(app=composed_app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Pipeline-seeding helpers.
#
# These helpers drive the Slice 1 + Slice 2 chain (Document → Region →
# Finding → Recommendation → Decision → Objective → Project →
# Activity Plan → Plan Revision → Plan Approval) under the pipeline-
# author Party so each Slice 3 demo can focus on the specific Slice 3
# denial scenario it exercises. The helpers mirror the pattern in
# :mod:`tests.end_to_end.test_release_1b_journey` and
# :mod:`tests.end_to_end.test_slice2_denial_demonstrations` so a
# regression in the Slice 1 / Slice 2 surface fails predictably here.
# ---------------------------------------------------------------------------


async def _seed_decision(client: AsyncClient) -> dict[str, str]:
    """Seed a Slice 1 Decision and return the chain identifiers.

    Drives ``POST /api/v1/documents`` → ``POST .../regions`` →
    ``POST /api/v1/findings`` → ``POST /api/v1/recommendations`` →
    ``POST .../decisions`` under the pipeline-author Party. The
    Decision is recorded with ``outcome='Accept'`` so it is eligible
    as the material source of a downstream Slice 2 Objective
    (AD-WS-21).
    """
    headers = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}

    doc_response = await client.post(
        "/api/v1/documents",
        json={
            "content_bytes": base64.b64encode(_DOC_CONTENT).decode("ascii"),
            "contributing_party_id": _AUTHOR_PARTY_ID,
            "authority": "authoritative",
        },
        headers=headers,
    )
    assert doc_response.status_code == 201, doc_response.text
    document = doc_response.json()

    region_response = await client.post(
        f"/api/v1/documents/{document['resource_id']}/revisions/"
        f"{document['revision_id']}/regions",
        json={
            "start_offset_bytes": _DOC_SPAN_START,
            "end_offset_bytes": _DOC_SPAN_END,
            "contributing_party_id": _AUTHOR_PARTY_ID,
        },
        headers=headers,
    )
    assert region_response.status_code == 201, region_response.text
    region = region_response.json()

    finding_response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "The corpus documents a quick brown fox.",
            "authoring_party_id": _AUTHOR_PARTY_ID,
            "is_hypothesis": False,
            "supporting_region_occurrences": [
                {
                    "region_id": region["region_id"],
                    "document_revision_id": document["revision_id"],
                }
            ],
        },
        headers=headers,
    )
    assert finding_response.status_code == 201, finding_response.text
    finding = finding_response.json()

    rec_response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _AUTHOR_PARTY_ID,
            "derived_from_findings": [finding["finding_id"]],
            "rationale": (
                "Recommend documenting the corpus fox observation "
                "in the team playbook."
            ),
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert rec_response.status_code == 201, rec_response.text
    recommendation = rec_response.json()

    decision_response = await client.post(
        f"/api/v1/recommendations/{recommendation['recommendation_id']}/decisions",
        json={
            "target_recommendation_revision_id": (
                recommendation["recommendation_revision_id"]
            ),
            "outcome": "Accept",
            "rationale": (
                "Accept the recommendation; the supporting evidence is "
                "byte-equivalent to the cited corpus span."
            ),
            "deciding_party_id": _AUTHOR_PARTY_ID,
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
            "omissions": [],
        },
        headers=headers,
    )
    assert decision_response.status_code == 201, decision_response.text
    decision = decision_response.json()
    return {
        "document_resource_id": document["resource_id"],
        "document_revision_id": document["revision_id"],
        "region_id": region["region_id"],
        "finding_id": finding["finding_id"],
        "recommendation_id": recommendation["recommendation_id"],
        "recommendation_revision_id": recommendation["recommendation_revision_id"],
        "decision_id": decision["decision_id"],
    }


async def _seed_planning_chain(
    client: AsyncClient,
    decision_id: str,
    *,
    planned_scope: str = _PR_PLANNED_SCOPE,
    ordering_rationale: str = _PR_ORDERING_RATIONALE,
    planning_assumptions: tuple[str, ...] = (
        _PR_ASSUMPTION_ONE,
        _PR_ASSUMPTION_TWO,
    ),
) -> dict[str, str]:
    """Seed Objective → ... → Draft Plan Revision under the named Decision.

    Returns the full identifier set produced along the way so each
    demo can address the right object. The Plan Revision is left in
    its draft state — the helper :func:`_approve_plan_revision`
    transitions it to ``approved`` when the demo needs an authorized
    target for ``create.work_assignment`` /
    ``create.completion``.
    """
    headers = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}

    objective_response = await client.post(
        "/api/v1/objectives",
        json={
            "statement": "Establish a reusable playbook anchored to the fox decision.",
            "rationale": "Anchor strategic intent to an authorized Slice 1 decision.",
            "target_decision_id": decision_id,
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert objective_response.status_code == 201, objective_response.text
    objective = objective_response.json()

    project_response = await client.post(
        "/api/v1/projects",
        json={
            "target_objective_id": objective["objective_id"],
            "name": "Onboarding Playbook Initiative",
            "summary": "Cross-cutting project addressing the onboarding objective.",
            "planned_start_date": "2026-07-01",
            "planned_end_date": "2026-12-31",
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    de_response = await client.post(
        "/api/v1/deliverable-expectations",
        json={
            "target_project_id": project["project_id"],
            "name": "Reusable Onboarding Playbook",
            "description": (
                "A versioned playbook documenting the corpus fox "
                "finding for new team members."
            ),
            "deliverable_kind": "Document",
            "acceptance_criteria": (
                "Approved by the steering committee and adopted by "
                "two consecutive new-hire cohorts."
            ),
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert de_response.status_code == 201, de_response.text
    deliverable_expectation = de_response.json()

    ap_response = await client.post(
        "/api/v1/activity-plans",
        json={
            "target_project_id": project["project_id"],
            "title": "Q3 Onboarding Playbook Activities",
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert ap_response.status_code == 201, ap_response.text
    activity_plan = ap_response.json()

    pr_response = await client.post(
        f"/api/v1/activity-plans/{activity_plan['activity_plan_id']}"
        "/plan-revisions",
        json={
            "planned_scope": planned_scope,
            "deliverable_expectation_refs": [
                deliverable_expectation["deliverable_expectation_id"],
            ],
            "planning_assumptions": list(planning_assumptions),
            "ordering_rationale": ordering_rationale,
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert pr_response.status_code == 201, pr_response.text
    plan_revision = pr_response.json()
    return {
        "objective_id": objective["objective_id"],
        "project_id": project["project_id"],
        "deliverable_expectation_id": (
            deliverable_expectation["deliverable_expectation_id"]
        ),
        "deliverable_expectation_revision_id": (
            deliverable_expectation["deliverable_expectation_revision_id"]
        ),
        "activity_plan_id": activity_plan["activity_plan_id"],
        "plan_revision_id": plan_revision["plan_revision_id"],
    }


async def _approve_plan_revision(
    client: AsyncClient, plan_revision_id: str
) -> dict[str, str]:
    """Approve the named Plan Revision under the pipeline-author Party.

    The Plan Revision must be in lifecycle state ``approved`` before
    ``create.work_assignment`` and ``create.completion`` are accepted
    by the Execution_Service (Requirements 23.2 / 23.4 / 29.4). The
    helper drives the existing Slice 2 ``POST
    /plan-revisions/{id}/approvals`` endpoint and returns the created
    Plan Approval Record.
    """
    headers = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}
    approval_response = await client.post(
        f"/api/v1/plan-revisions/{plan_revision_id}/approvals",
        json={
            "outcome": "Approve",
            "rationale": (
                "Authorize the playbook plan so the Slice 3 denial demos "
                "have an approved target."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
            "omissions": [],
        },
        headers=headers,
    )
    assert approval_response.status_code == 201, approval_response.text
    return approval_response.json()


async def _seed_full_chain_through_approved_plan_revision(
    client: AsyncClient,
) -> dict[str, str]:
    """Seed Document → ... → Approved Plan Revision and return identifiers."""
    chain = await _seed_decision(client)
    planning = await _seed_planning_chain(client, chain["decision_id"])
    chain.update(planning)
    approval = await _approve_plan_revision(client, planning["plan_revision_id"])
    chain["plan_approval_id"] = approval["plan_approval_id"]
    return chain


async def _seed_work_assignment_for_assignee(
    client: AsyncClient,
    *,
    plan_revision_id: str,
    assignee_party_id: str,
) -> str:
    """Seed a Work Assignment under the pipeline-author Party.

    The Work Assignment names ``assignee_party_id`` as the named
    assignee so the downstream AD-WS-29 assignee-binding check
    succeeds for that Party (and fails for any other Contributor —
    the demo-3 scenario). Returns the issued Work Assignment Record
    Identity.
    """
    response = await client.post(
        "/api/v1/work-assignments",
        json={
            "target_plan_revision_id": plan_revision_id,
            "assignee_party_id": assignee_party_id,
            "assignment_rationale": (
                "Authorize the named assignee to record Work Events, "
                "Time Entries, and produced Deliverables on this plan."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["work_assignment_id"]


async def _seed_finalized_completion(
    client: AsyncClient,
    chain: dict[str, str],
) -> dict[str, str]:
    """Drive Work Assignment → ... → Completion for demo 4.

    Records the full Slice 3 chain ending in a Completion against the
    chain's Approved Plan Revision. Returns the issued identifiers so
    demo 4 can attempt the duplicate-Completion modification and
    assert the original row is byte-equivalent.
    """
    plan_revision_id = chain["plan_revision_id"]

    # 1. Work Assignment under pipeline author → assignee Party.
    wa_id = await _seed_work_assignment_for_assignee(
        client,
        plan_revision_id=plan_revision_id,
        assignee_party_id=_ASSIGNEE_PARTY_ID,
    )

    # 2. Recorded Work Event (started) — the AD-WS-29 assignee-binding
    # check requires the recording Party to be the named assignee.
    event_headers = {"X-Actor-Party-Id": _ASSIGNEE_PARTY_ID}
    started_response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": wa_id,
            "event_kind": "started",
            "event_note": "Kick-off the playbook drafting work.",
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=event_headers,
    )
    assert started_response.status_code == 201, started_response.text

    # 3. Produced Deliverable Revision.
    deliverable_response = await client.post(
        "/api/v1/deliverables",
        json={
            "content_bytes": base64.b64encode(_DELIVERABLE_BYTES).decode("ascii"),
            "content_type": "text/markdown",
            "produced_deliverable_name": "Onboarding Playbook draft v1",
            "originating_work_assignment_id": wa_id,
        },
        headers=event_headers,
    )
    assert deliverable_response.status_code == 201, deliverable_response.text
    deliverable = deliverable_response.json()

    # 4. Deliverable Production Record connecting the produced
    # Deliverable Revision to the target Deliverable Expectation
    # Revision.
    production_response = await client.post(
        "/api/v1/deliverable-productions",
        json={
            "source_work_assignment_id": wa_id,
            "produced_deliverable_revision_id": deliverable[
                "deliverable_revision_id"
            ],
            "target_deliverable_expectation_revision_id": chain[
                "deliverable_expectation_revision_id"
            ],
            "production_rationale": (
                "Record the first iteration of the playbook against the "
                "deliverable expectation."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=event_headers,
    )
    assert production_response.status_code == 201, production_response.text
    production = production_response.json()

    # 5. Milestone Acceptance Record (Accept) under the Milestone
    # Acceptance Authority Party.
    acceptance_response = await client.post(
        "/api/v1/milestone-acceptances",
        json={
            "source_deliverable_production_id": production[
                "deliverable_production_id"
            ],
            "outcome": "Accept",
            "rationale": (
                "The draft playbook satisfies the deliverable "
                "expectation's acceptance criteria for iteration one."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _ACCEPTING_AUTHORITY_PARTY_ID},
    )
    assert acceptance_response.status_code == 201, acceptance_response.text
    acceptance = acceptance_response.json()

    # 6. Completion Record under the Milestone Acceptance + Completion
    # Authority Party. This is the finalized Completion that demo 4
    # then attempts to "modify" via a duplicate submission.
    completion_response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": plan_revision_id,
            "outcome": "Completed",
            "rationale": (
                "Record completion of the playbook plan based on the "
                "accepted milestone."
            ),
            "source_milestone_acceptance_ids": [
                acceptance["milestone_acceptance_id"]
            ],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _ACCEPTING_AUTHORITY_PARTY_ID},
    )
    assert completion_response.status_code == 201, completion_response.text
    completion = completion_response.json()
    return {
        "work_assignment_id": wa_id,
        "deliverable_id": deliverable["deliverable_id"],
        "deliverable_revision_id": deliverable["deliverable_revision_id"],
        "deliverable_production_id": production["deliverable_production_id"],
        "milestone_acceptance_id": acceptance["milestone_acceptance_id"],
        "completion_id": completion["completion_id"],
    }


# ---------------------------------------------------------------------------
# Database snapshot helpers.
# ---------------------------------------------------------------------------


# Slice 2 tables the demo-5 byte-equivalence post-condition compares
# before vs. after the rejected Work Assignment attempt. Property 41
# (Slice 1 + Slice 2 non-modification) requires every Slice 2 row to
# remain byte-equivalent across any Slice 3 action. The demo-5 test
# scopes the assertion to the seven planning tables most directly
# adjacent to the rejected Work Assignment so a regression in the
# Execution_Service's prohibited-attribute screen surfaces here.
_SLICE2_BYTE_EQUIV_TABLES: tuple[str, ...] = (
    "Objectives",
    "Objective_Revisions",
    "Projects",
    "Project_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    "Activity_Plans",
    "Plan_Revisions",
    "Plan_Approval_Records",
)


def _snapshot_tables(
    engine: Engine, tables: tuple[str, ...]
) -> dict[str, list[dict[str, object]]]:
    """Capture an ordered snapshot of every row in the named tables.

    The snapshot drives the demo-5 byte-equivalence post-condition:
    after the rejected Work Assignment attempt, every Slice 2 row
    must remain byte-equivalent to its pre-attempt snapshot.
    Rows are sorted by their full content so the comparison is
    order-independent.
    """
    snapshot: dict[str, list[dict[str, object]]] = {}
    with engine.connect() as conn:
        for table in tables:
            rows = conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
            snapshot[table] = sorted(
                (dict(row) for row in rows),
                key=lambda r: tuple(sorted(r.items())),
            )
    return snapshot


# ---------------------------------------------------------------------------
# Demo 1 — Contributor attempting a Work Assignment is denied with an
# AD-WS-9-shaped response and a Denial Record (Requirements 30.1, 32.6).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_contributor_denied_work_assignment_with_ad_ws_9_shape(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A Contributor (no ``assign``) gets the AD-WS-9 denial shape on Work Assignment.

    The Contributor Party holds ``view`` + ``contribute`` on the
    wildcard scope but not ``assign``. AD-WS-24 maps
    ``create.work_assignment`` to the ``assign`` authority, so the
    Authorization_Service rejects the attempt with
    ``reason_code='out-of-scope'``. The response is the AD-WS-9
    indistinguishable shape — *exactly* three fields,
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` — and nothing about the target Plan Revision,
    assignee, or any other observable.

    The test also asserts the database state after the denial:

    - No ``Work_Assignment_Records`` row was inserted
      (Requirement 23.6).
    - Exactly one Denial Record was appended to ``Audit_Records``
      with ``outcome='deny'`` and
      ``action_type='create.work_assignment'``
      (Requirement 37.2 / Slice 1 Requirement 13.2).
    """
    engine: Engine = composed_app.state.engine
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    plan_revision_id = chain["plan_revision_id"]

    response = await client.post(
        "/api/v1/work-assignments",
        json={
            "target_plan_revision_id": plan_revision_id,
            "assignee_party_id": _ASSIGNEE_PARTY_ID,
            "assignment_rationale": (
                "Contributor-only Party attempts to assign work; "
                "the Authorization_Service should reject this with "
                "the AD-WS-9 indistinguishable denial."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _CONTRIBUTOR_PARTY_ID},
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    # AD-WS-9 indistinguishable response shape — exactly three fields.
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"
    # Either ``out-of-scope`` (the AuthorizationService surfaced the
    # missing ``assign`` authority) or ``no-role-assignment`` (no role
    # covered the action at all) is acceptable — both belong to the
    # Requirement 7.2 / 30.4 enumeration.
    assert detail["reason_code"] in {
        "out-of-scope",
        "no-role-assignment",
    }, detail
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # Database-side: no Work Assignment Record persisted; the
    # Audit_Log carries at least one Denial Record for the rejected
    # ``create.work_assignment`` attempt by this Party with a matching
    # correlation identifier.
    with engine.connect() as conn:
        work_assignment_count = conn.execute(
            text("SELECT COUNT(*) FROM Work_Assignment_Records")
        ).scalar_one()
        denial_rows = conn.execute(
            text(
                "SELECT actor_party_id, action_type, outcome, "
                "reason_code, correlation_id FROM Audit_Records "
                "WHERE outcome = 'deny' "
                "  AND action_type = 'create.work_assignment' "
                "  AND actor_party_id = :pid"
            ),
            {"pid": _CONTRIBUTOR_PARTY_ID},
        ).mappings().all()

    assert work_assignment_count == 0, (
        "Requirement 23.6 violated: a Work Assignment Record was "
        "persisted after the Authorization_Service rejected the "
        "Contributor's attempt."
    )
    assert len(denial_rows) >= 1, (
        "Requirement 37.2 / Slice 1 Requirement 13.2 violated: no "
        "Denial Record was appended for the rejected "
        "create.work_assignment attempt."
    )
    denial_correlation_ids = {row["correlation_id"] for row in denial_rows}
    assert detail["correlation_id"] in denial_correlation_ids, (
        "The HTTP response correlation_id does not tie back to any of "
        "the Denial Records appended for this attempt."
    )


# ---------------------------------------------------------------------------
# Demo 2 — Completion-Authority Party attempting a Milestone Acceptance
# is denied (Requirements 30.1, 32.8).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_completion_authority_denied_milestone_acceptance(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A Completion-only Party gets the AD-WS-9 denial on Milestone Acceptance.

    The Completion-only Party holds ``view`` + ``complete`` on the
    wildcard scope but not ``accept_milestone``. AD-WS-15 maps
    ``create.milestone_acceptance`` to the ``accept_milestone``
    authority (distinct from ``complete`` per Requirements 32.8 and
    32.9), so the Authorization_Service rejects the attempt with the
    AD-WS-9 indistinguishable denial body.

    The test seeds the full chain through a Deliverable Production
    under the pipeline-author + named-assignee Parties so the
    Milestone Acceptance request body is otherwise valid; only the
    requesting Party's authority is missing.
    """
    engine: Engine = composed_app.state.engine
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    plan_revision_id = chain["plan_revision_id"]

    # Seed Work Assignment + Work Event + produced Deliverable +
    # Deliverable Production under the named-assignee Party so the
    # Milestone Acceptance request has a valid source.
    wa_id = await _seed_work_assignment_for_assignee(
        client,
        plan_revision_id=plan_revision_id,
        assignee_party_id=_ASSIGNEE_PARTY_ID,
    )
    assignee_headers = {"X-Actor-Party-Id": _ASSIGNEE_PARTY_ID}
    started_response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": wa_id,
            "event_kind": "started",
            "event_note": "Kick off the work.",
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert started_response.status_code == 201, started_response.text
    deliverable_response = await client.post(
        "/api/v1/deliverables",
        json={
            "content_bytes": base64.b64encode(_DELIVERABLE_BYTES).decode("ascii"),
            "content_type": "text/markdown",
            "produced_deliverable_name": "Demo 2 deliverable",
            "originating_work_assignment_id": wa_id,
        },
        headers=assignee_headers,
    )
    assert deliverable_response.status_code == 201, deliverable_response.text
    deliverable = deliverable_response.json()
    production_response = await client.post(
        "/api/v1/deliverable-productions",
        json={
            "source_work_assignment_id": wa_id,
            "produced_deliverable_revision_id": deliverable[
                "deliverable_revision_id"
            ],
            "target_deliverable_expectation_revision_id": chain[
                "deliverable_expectation_revision_id"
            ],
            "production_rationale": "Demo 2 production rationale.",
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert production_response.status_code == 201, production_response.text
    production_id = production_response.json()["deliverable_production_id"]

    # Completion-only Party attempts the Milestone Acceptance.
    response = await client.post(
        "/api/v1/milestone-acceptances",
        json={
            "source_deliverable_production_id": production_id,
            "outcome": "Accept",
            "rationale": (
                "Completion-only Party attempts a Milestone "
                "Acceptance; the Authorization_Service should "
                "reject this with the AD-WS-9 indistinguishable "
                "denial because ``complete`` does not satisfy "
                "``accept_milestone``."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _COMPLETION_ONLY_PARTY_ID},
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"
    assert detail["reason_code"] in {
        "out-of-scope",
        "no-role-assignment",
    }, detail
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # Database-side: no Milestone Acceptance Record persisted; a
    # Denial Record exists for the rejected attempt.
    with engine.connect() as conn:
        ma_count = conn.execute(
            text("SELECT COUNT(*) FROM Milestone_Acceptance_Records")
        ).scalar_one()
        denial_rows = conn.execute(
            text(
                "SELECT correlation_id FROM Audit_Records "
                "WHERE outcome = 'deny' "
                "  AND action_type = 'create.milestone_acceptance' "
                "  AND actor_party_id = :pid"
            ),
            {"pid": _COMPLETION_ONLY_PARTY_ID},
        ).mappings().all()
    assert ma_count == 0, (
        "Requirement 28 violated: a Milestone Acceptance Record was "
        "persisted after the Authorization_Service rejected the "
        "Completion-only Party's attempt."
    )
    assert len(denial_rows) >= 1, (
        "Requirement 37.2 violated: no Denial Record appended for the "
        "rejected create.milestone_acceptance attempt."
    )
    denial_correlation_ids = {row["correlation_id"] for row in denial_rows}
    assert detail["correlation_id"] in denial_correlation_ids


# ---------------------------------------------------------------------------
# Demo 3 — Party with ``contribute`` but not the named assignee is denied
# with ``reason_code='no-role-assignment'`` (Requirements 30.4, 32.7).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_stranger_contributor_denied_with_no_role_assignment(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A non-assignee Contributor is denied with ``reason_code='no-role-assignment'``.

    The Stranger Contributor Party holds ``view`` + ``contribute`` on
    the wildcard scope, so the AuthorizationService's authority
    evaluation for ``create.work_event`` permits the attempt. The
    AD-WS-29 assignee-binding check then runs against the persisted
    Work Assignment row and observes
    ``assignee_party_id != recording_party_id``; the service rolls
    back and appends a Denial Record with
    ``reason_code='no-role-assignment'`` (fixed by the service layer
    so the wire shape is indistinguishable from the
    authority-evaluation deny path per Requirement 30.4).
    """
    engine: Engine = composed_app.state.engine
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    plan_revision_id = chain["plan_revision_id"]
    wa_id = await _seed_work_assignment_for_assignee(
        client,
        plan_revision_id=plan_revision_id,
        assignee_party_id=_ASSIGNEE_PARTY_ID,
    )

    response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": wa_id,
            "event_kind": "started",
            "event_note": (
                "Stranger Contributor attempts to record a Work Event "
                "against a Work Assignment whose named assignee is a "
                "different Party."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _STRANGER_CONTRIBUTOR_PARTY_ID},
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"
    # The AD-WS-29 assignee-binding failure is recorded with the fixed
    # ``no-role-assignment`` reason code per Requirement 30.4 so the
    # response is indistinguishable from a missing-role-assignment
    # authority-evaluation deny.
    assert detail["reason_code"] == "no-role-assignment", detail
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # Database-side: no Work Event Record persisted, the seeded Work
    # Assignment is unchanged, and a Denial Record was appended.
    with engine.connect() as conn:
        we_count = conn.execute(
            text("SELECT COUNT(*) FROM Work_Event_Records")
        ).scalar_one()
        assignee_party_id = conn.execute(
            text(
                "SELECT assignee_party_id FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :wid"
            ),
            {"wid": wa_id},
        ).scalar_one()
        denial_rows = conn.execute(
            text(
                "SELECT reason_code, correlation_id FROM Audit_Records "
                "WHERE outcome = 'deny' "
                "  AND action_type = 'create.work_event' "
                "  AND actor_party_id = :pid"
            ),
            {"pid": _STRANGER_CONTRIBUTOR_PARTY_ID},
        ).mappings().all()
    assert we_count == 0, (
        "Requirement 24 violated: a Work Event Record was persisted "
        "after the AD-WS-29 assignee-binding check rejected the "
        "Stranger Contributor's attempt."
    )
    assert assignee_party_id == _ASSIGNEE_PARTY_ID
    assert len(denial_rows) >= 1
    # At least one Denial Record carries the AD-WS-29 ``no-role-
    # assignment`` reason code per Requirement 30.4.
    assert any(
        row["reason_code"] == "no-role-assignment" for row in denial_rows
    ), denial_rows
    denial_correlation_ids = {row["correlation_id"] for row in denial_rows}
    assert detail["correlation_id"] in denial_correlation_ids


# ---------------------------------------------------------------------------
# Demo 4 — Modifying a finalized Completion Record is rejected and the
# original row is byte-equivalent (Requirements 29.3, 29.7, 37.5,
# AD-WS-27).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_modifying_finalized_completion_is_rejected(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A second Completion against a finalized target returns the conflict code.

    Drives the full Slice 1 + Slice 2 + Slice 3 pipeline through to a
    finalized Completion Record, then submits a *second* Completion
    against the same Plan Revision — the closest HTTP-level analog of
    "modifying a finalized Completion Record". Requirement 29.3
    enforces ``UNIQUE(target_plan_revision_id)`` on
    ``Completion_Records``; the service pre-checks the constraint and
    raises :class:`CompletionConflictError`, which the route maps to
    HTTP 409 with ``error_code='completion_already_exists'``.

    The test additionally exercises the AD-WS-27 UPDATE/DELETE
    triggers by attempting a direct SQL UPDATE against the persisted
    Completion Record; the trigger raises an
    :class:`sqlalchemy.exc.IntegrityError` and the row remains
    byte-equivalent (Requirement 29.7 / 37.5).
    """
    engine: Engine = composed_app.state.engine
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    slice3 = await _seed_finalized_completion(client, chain)
    plan_revision_id = chain["plan_revision_id"]
    original_completion_id = slice3["completion_id"]

    # Capture a byte-equivalent snapshot of the finalized Completion
    # row before the modification attempts.
    with engine.connect() as conn:
        original_snapshot = dict(
            conn.execute(
                text(
                    "SELECT completion_id, target_plan_revision_id, "
                    "target_activity_plan_id, target_project_id, outcome, "
                    "rationale, source_milestone_acceptance_ids_json, "
                    "completing_party_id, authority_basis_type, "
                    "authority_basis_id, applicable_scope, recorded_at "
                    "FROM Completion_Records "
                    "WHERE completion_id = :cid"
                ),
                {"cid": original_completion_id},
            )
            .mappings()
            .one()
        )

    # ----- Attempt 1: duplicate Completion submission via HTTP -----
    duplicate_response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": plan_revision_id,
            "outcome": "Completed",
            "rationale": (
                "Attempting to re-record the Completion; the "
                "Execution_Service should reject this as a duplicate "
                "per Requirement 29.3."
            ),
            "source_milestone_acceptance_ids": [
                slice3["milestone_acceptance_id"]
            ],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _ACCEPTING_AUTHORITY_PARTY_ID},
    )
    assert duplicate_response.status_code == 409, duplicate_response.text
    duplicate_detail = duplicate_response.json()["detail"]
    assert duplicate_detail["error_code"] == "completion_already_exists"
    # The conflict response surfaces the existing Completion Identity
    # only when the caller holds view authority on it; the acceptance
    # authority Party does in this scenario, so the existing identity
    # is observable.
    assert (
        duplicate_detail.get("existing_completion_id")
        == original_completion_id
    )
    assert (
        duplicate_detail.get("target_plan_revision_id") == plan_revision_id
    )

    # ----- Attempt 2: direct SQL UPDATE against the finalized row ---
    # The AD-WS-27 ``Completion_Records_reject_update`` trigger should
    # fire and abort the UPDATE. The trigger raises a SQLite error
    # which SQLAlchemy surfaces as :class:`IntegrityError`.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE Completion_Records "
                    "SET rationale = :new_rationale "
                    "WHERE completion_id = :cid"
                ),
                {
                    "new_rationale": "Tampered rationale; should be rejected.",
                    "cid": original_completion_id,
                },
            )

    # ----- Attempt 3: direct SQL DELETE against the finalized row ---
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM Completion_Records WHERE completion_id = :cid"
                ),
                {"cid": original_completion_id},
            )

    # ----- Post-attempt invariants ---------------------------------
    with engine.connect() as conn:
        completion_count = conn.execute(
            text("SELECT COUNT(*) FROM Completion_Records")
        ).scalar_one()
        post_attempt_snapshot = dict(
            conn.execute(
                text(
                    "SELECT completion_id, target_plan_revision_id, "
                    "target_activity_plan_id, target_project_id, outcome, "
                    "rationale, source_milestone_acceptance_ids_json, "
                    "completing_party_id, authority_basis_type, "
                    "authority_basis_id, applicable_scope, recorded_at "
                    "FROM Completion_Records "
                    "WHERE completion_id = :cid"
                ),
                {"cid": original_completion_id},
            )
            .mappings()
            .one()
        )

    # Exactly one Completion Record exists — the original; the
    # duplicate attempt did not persist a second row and the direct
    # UPDATE / DELETE attempts did not change the row count.
    assert completion_count == 1, (
        f"Requirement 29.3 / 29.7 violated: expected exactly one "
        f"Completion Record for the target Plan Revision; got "
        f"{completion_count}."
    )
    # Byte-equivalent: every column unchanged after every rejected
    # modification attempt (Requirement 29.7 / 37.5).
    assert post_attempt_snapshot == original_snapshot, (
        "Requirement 29.7 / 37.5 violated: the finalized Completion "
        "Record diverged from its pre-attempt snapshot after the "
        "rejected modification attempts."
    )


# ---------------------------------------------------------------------------
# Demo 5 — Submitting a Work Assignment with a ``planned-`` attribute is
# rejected with no row persisted and no Slice 2 row mutated
# (Requirements 33.4, 40.3).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_work_assignment_with_planned_attribute_is_rejected(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A Work Assignment carrying ``planned-extra`` is rejected at the API boundary.

    The Execution_Service's prohibited-attribute screen (Requirement
    33.4 / Property 35) fires before Pydantic validation: any
    top-level key matching the prohibited planning-attribute prefix
    set ``{planned-, planning-assumption-, ordering-rationale-,
    plan-review-, plan-approval-}`` causes the request to be
    rejected with ``failed_constraint='prohibited_attribute'``.

    The test asserts the full database state:

    - Zero ``Work_Assignment_Records`` rows (no row persisted).
    - Every Slice 2 row in the chain is byte-equivalent to its
      pre-attempt snapshot (Property 41 — Slice 2 non-modification).
    """
    engine: Engine = composed_app.state.engine
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    plan_revision_id = chain["plan_revision_id"]
    pre_attempt_snapshot = _snapshot_tables(engine, _SLICE2_BYTE_EQUIV_TABLES)

    response = await client.post(
        "/api/v1/work-assignments",
        json={
            "target_plan_revision_id": plan_revision_id,
            "assignee_party_id": _ASSIGNEE_PARTY_ID,
            "assignment_rationale": (
                "Attempting to mint a Work Assignment carrying a "
                "prohibited planning-attribute key; the "
                "Execution_Service should reject this."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
            # Prohibited planning-attribute prefix — the prefix screen
            # in ``_reject_prohibited_attributes`` fires before any
            # other validator so the response carries
            # ``failed_constraint='prohibited_attribute'``.
            "planned-iteration-count": 2,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "work_assignment_validation_failed"
    assert detail["failed_constraint"] == "prohibited_attribute"
    assert "planned-iteration-count" in detail["prohibited_keys"], detail

    # Database-side: zero Work Assignment Records were persisted, and
    # every Slice 2 row in the chain is byte-equivalent to its
    # pre-attempt snapshot.
    with engine.connect() as conn:
        wa_count = conn.execute(
            text("SELECT COUNT(*) FROM Work_Assignment_Records")
        ).scalar_one()
    assert wa_count == 0, (
        "Requirement 33.4 violated: a Work Assignment Record was "
        "persisted for a request carrying a prohibited planning-"
        "attribute key."
    )
    post_attempt_snapshot = _snapshot_tables(engine, _SLICE2_BYTE_EQUIV_TABLES)
    assert post_attempt_snapshot == pre_attempt_snapshot, (
        "Requirement 40.3 / Property 41 violated: a Slice 2 row "
        "diverged from its pre-attempt snapshot after a rejected "
        "Slice 3 Work Assignment attempt."
    )


# ---------------------------------------------------------------------------
# Demo 6 — Submitting a Completion with an ``observed-`` attribute is
# rejected (Requirements 34.5, 39.6).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_completion_with_observed_attribute_is_rejected(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A Completion request carrying ``observed-value`` is rejected.

    The Execution_Service rejects any top-level key drawn from the
    observed-outcome prohibited-prefix set
    ``{observed-, measurement-, outcome-review-,
    attribution-evidence-, success-condition-assessment-}``
    (Requirement 34.5 / Property 36 — Output / Outcome separation).
    No ``Completion_Records`` row is persisted.
    """
    engine: Engine = composed_app.state.engine
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    slice3 = await _seed_finalized_completion(client, chain)

    # The earlier seed already persisted one Completion Record; this
    # demo submits a separate attempt with the prohibited attribute.
    # Use a fresh Plan Revision so the prohibited-attribute screen
    # fires before the uniqueness conflict — otherwise the duplicate
    # check could mask the prohibited-attribute rejection.
    second_chain = await _seed_decision(client)
    second_planning = await _seed_planning_chain(
        client, second_chain["decision_id"]
    )
    await _approve_plan_revision(
        client, second_planning["plan_revision_id"]
    )
    response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": second_planning["plan_revision_id"],
            "outcome": "Completed",
            "rationale": (
                "Completion attempt carrying a prohibited observed-"
                "outcome attribute; the Execution_Service should "
                "reject this at the API boundary."
            ),
            "source_milestone_acceptance_ids": [
                slice3["milestone_acceptance_id"]
            ],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
            # Prohibited observed-outcome attribute — the prefix
            # screen fires before any other validator so the response
            # carries ``failed_constraint='prohibited_attribute'``.
            "observed-success-indicator": "playbook-adopted",
        },
        headers={"X-Actor-Party-Id": _ACCEPTING_AUTHORITY_PARTY_ID},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error_code"] == "completion_validation_failed"
    assert detail["failed_constraint"] == "prohibited_attribute"
    assert "observed-success-indicator" in detail["prohibited_keys"], detail

    # Database-side: exactly one Completion Record exists (the one
    # seeded earlier); no Completion was persisted from the rejected
    # attempt.
    with engine.connect() as conn:
        completion_count = conn.execute(
            text("SELECT COUNT(*) FROM Completion_Records")
        ).scalar_one()
        completion_for_second_pr = conn.execute(
            text(
                "SELECT COUNT(*) FROM Completion_Records "
                "WHERE target_plan_revision_id = :pid"
            ),
            {"pid": second_planning["plan_revision_id"]},
        ).scalar_one()
    assert completion_count == 1, (
        "Requirement 34.5 violated: a Completion Record was "
        "persisted for a request carrying a prohibited observed-"
        "outcome attribute."
    )
    assert completion_for_second_pr == 0, (
        "Requirement 34.5 violated: a Completion Record was "
        "persisted against the second Plan Revision despite the "
        "rejected attempt."
    )


# ---------------------------------------------------------------------------
# Demo 7 — Submitting a Completion against a non-existent Plan Revision is
# indistinguishable from submitting against a restricted Plan Revision
# the caller cannot view (Requirements 30.5, 30.7, AD-WS-9 rule 1).
# ---------------------------------------------------------------------------


# Attributes the Planning_Service persists on each Plan Revision row
# that a denial response MUST NOT leak to a caller without view
# authority. The leak-test below asserts none of these substrings
# appears in either denial response body.
_PR_LEAK_SUBSTRINGS: tuple[str, ...] = (
    _PR_PLANNED_SCOPE,
    _PR_ORDERING_RATIONALE,
    _PR_ASSUMPTION_ONE,
    _PR_ASSUMPTION_TWO,
    "approved",
    _AUTHOR_PARTY_ID,
)


@pytest.mark.asyncio
async def test_demo_non_existent_vs_restricted_completion_indistinguishable(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Non-existent and restricted Completion attempts leak no target info.

    Drives two POST attempts against ``/api/v1/completions``:

    - **Non-existent target** — the ``target_plan_revision_id`` is a
      freshly minted UUIDv7 that does not resolve to any row. The
      Execution_Service raises
      :class:`CompletionPlanRevisionNotResolvableError` and the
      route maps it to ``HTTP 404`` with
      ``error_code='target_plan_revision_not_resolvable'``.
    - **Restricted target** — the ``target_plan_revision_id`` names
      an Approved Plan Revision that the unauthorized Party cannot
      view (it holds no role assignment). The
      Authorization_Service's authority evaluation rejects the
      attempt and the route surfaces the AD-WS-9 denial shape.

    Per Requirements 30.5 / 30.7 / AD-WS-9 rule 1 the responses must
    not leak any attribute of the target Plan Revision. The test
    asserts:

    1. Both responses are 4xx errors.
    2. Neither response body contains any of the seeded Plan
       Revision's text attributes (``planned_scope``,
       ``ordering_rationale``, ``planning_assumptions``, the
       authoring Party Identity, or the persisted lifecycle state).
    3. The detail-body keys do not include any Plan Revision
       attribute keys (a defense-in-depth check against accidental
       key additions to the structured error envelope).
    """
    engine: Engine = composed_app.state.engine

    # Universe A: non-existent Plan Revision. The string is a
    # canonical UUIDv7 that does not resolve to any row.
    non_existent_plan_revision_id = "00000000-0000-7000-8000-0000000def01"
    non_existent_response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": non_existent_plan_revision_id,
            "outcome": "Completed",
            "rationale": (
                "Submitting a Completion against a non-existent Plan "
                "Revision; the response should not disclose whether "
                "the target exists."
            ),
            "source_milestone_acceptance_ids": [],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )

    # Universe B: restricted Plan Revision the caller cannot view.
    chain = await _seed_full_chain_through_approved_plan_revision(client)
    restricted_plan_revision_id = chain["plan_revision_id"]
    restricted_response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": restricted_plan_revision_id,
            "outcome": "Completed",
            "rationale": (
                "Submitting a Completion against a Plan Revision the "
                "caller cannot view; the response should be "
                "indistinguishable from the non-existent case."
            ),
            "source_milestone_acceptance_ids": [],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )

    # Both responses are 4xx errors (no consequential write).
    assert 400 <= non_existent_response.status_code < 500, (
        non_existent_response.text
    )
    assert 400 <= restricted_response.status_code < 500, (
        restricted_response.text
    )

    # Information-leak discipline: neither response body discloses
    # any attribute of the seeded Plan Revision that the caller did
    # not supply on the request body.
    non_existent_body_text = non_existent_response.text
    restricted_body_text = restricted_response.text
    for leak_substring in _PR_LEAK_SUBSTRINGS:
        assert leak_substring not in non_existent_body_text, (
            f"Information leak in non-existent-target response: the "
            f"body contains the substring {leak_substring!r} which "
            f"was not part of the caller's request. This breaks "
            f"Requirement 30.7 / AD-WS-9 rule 1 (indistinguishable "
            f"denial)."
        )
        assert leak_substring not in restricted_body_text, (
            f"Information leak in restricted-target response: the "
            f"body contains the substring {leak_substring!r} which "
            f"would let the caller infer the target Plan Revision's "
            f"attributes. This breaks Requirement 30.5 / 30.7 / "
            f"AD-WS-9 rule 1 (indistinguishable denial)."
        )

    # Defense-in-depth: neither detail body carries any forbidden
    # Plan Revision attribute key. The ``ErrorBody`` and
    # ``DenialResponseBody`` Pydantic models pin this contract at the
    # type level (``extra='forbid'``); the assertion below guards
    # against a future regression that constructs a body from a raw
    # dict bypassing those models.
    forbidden_body_keys: set[str] = {
        "planned_scope",
        "ordering_rationale",
        "planning_assumptions",
        "deliverable_expectation_refs",
        "recorded_at",
        "authoring_party_id",
        "lifecycle_state",
    }
    non_existent_detail_keys = set(
        non_existent_response.json()["detail"].keys()
    )
    restricted_detail_keys = set(
        restricted_response.json()["detail"].keys()
    )
    assert non_existent_detail_keys.isdisjoint(forbidden_body_keys), (
        f"Non-existent-target response leaked Plan Revision attribute "
        f"keys: {non_existent_detail_keys & forbidden_body_keys}."
    )
    assert restricted_detail_keys.isdisjoint(forbidden_body_keys), (
        f"Restricted-target response leaked Plan Revision attribute "
        f"keys: {restricted_detail_keys & forbidden_body_keys}."
    )

    # No consequential write: the restricted Plan Revision row is
    # still in its approved state, and no Completion Record was
    # persisted from either denial attempt.
    with engine.connect() as conn:
        lifecycle_state = conn.execute(
            text(
                "SELECT lifecycle_state FROM Plan_Revisions "
                "WHERE plan_revision_id = :pid"
            ),
            {"pid": restricted_plan_revision_id},
        ).scalar_one()
        completion_count = conn.execute(
            text("SELECT COUNT(*) FROM Completion_Records")
        ).scalar_one()
    assert lifecycle_state == "approved", (
        "Requirement 29.4 violated: the restricted Plan Revision's "
        "lifecycle state changed despite the denied attempt."
    )
    assert completion_count == 0, (
        "Requirement 29 violated: a Completion Record was persisted "
        "for one of the denied attempts."
    )
