"""End-to-end HTTP tests for the Slice 4 named denial + separation demos (task 16.2).

Each test drives the fully-composed FastAPI app — built by
:func:`walking_slice.app.create_app` — through :class:`httpx.AsyncClient`
over the ASGI transport, exercising one of the eight scenarios named in the
task statement:

1. **A Measurement Recorder attempting an Outcome Review is denied with an
   AD-WS-9-shaped response and a Denial Record** (Requirements 50.4, 50.7,
   54.2). A Party holding only ``record_measurement`` submits an Outcome
   Review; ``create.outcome_review`` requires ``issue_outcome_review`` per
   AD-WS-33 so the Authorization_Service rejects the attempt after the request
   passes validation and citation-resolution. The response carries *only* the
   AD-WS-9 trio (``generic_denial_indicator``, ``reason_code``,
   ``correlation_id``) and a Denial Record with
   ``action_type='create.outcome_review'`` is appended to ``Audit_Records``.

2. **An Outcome Assessor attempting a Measurement Definition is denied**
   (Requirements 50.4, 50.7). A Party holding only ``assess_outcome`` submits
   a Measurement Definition; ``create.measurement_definition`` requires
   ``define_measurement`` so the attempt is denied with the AD-WS-9 trio and a
   Denial Record.

3. **Submitting an outcome-measurement request with a prohibited
   intended-side attribute is rejected with no row persisted and no
   prior-slice row mutated** (Requirements 53.2). The Outcome_Service's
   prohibited-attribute screen fires at the API boundary so the response
   carries ``failed_constraint='prohibited_attribute'`` and the offending key.
   No Observed Outcome row is persisted and every Slice 1 / Slice 2 row remains
   byte-equivalent.

4. **Submitting an Observed Outcome with ``outcome_kind`` other than
   ``observed`` is rejected** (Requirements 54.1). The service rejects the
   supplied ``outcome_kind='intended'`` with
   ``failed_constraint='outcome_kind_invalid'`` and persists no row.

5. **An Outcome Review with stance ``Asserted`` and an empty
   attribution-evidence reference is rejected** (Requirements 54.1, 54.2). The
   Requirement 49.4 rule fires with
   ``failed_constraint='attribution_evidence_reference_missing_for_stance'``.

6. **A second imported Measurement Record with a matching source-system pair
   is rejected** (Requirements 58.4). The AD-WS-39 idempotency key rejects a
   duplicate ``(source_system_id, source_system_record_id)`` pair per
   Definition Revision with HTTP 409 and no second Record persisted; the first
   remains byte-equivalent.

7. **An unauthorized requester reading an imported Measurement Record receives
   the ``{kind, redacted: true}`` marker with no source-system attribute
   leakage** (Requirements 55.7, 58.5). A Party that may view the Outcome
   Review chain but lacks view authority on the (separately-scoped) imported
   Measurement Record navigates the Outcome Measurement Provenance Chain; the
   Record node surfaces as the AD-WS-34 redaction marker and the response body
   never contains the restricted source-system identifier / authority.

8. **A creation against a non-existent Intended Outcome Revision is
   indistinguishable from one against a restricted Revision the caller cannot
   view** (Requirements 50.5, 50.7). Two ``POST /measurement-definitions``
   attempts — one naming a non-existent target, one naming a real target the
   caller cannot view — both fail without leaking any target attribute the
   caller did not already supply on the request body.

Authentication threads through the temporary ``X-Actor-Party-Id`` header
carried by the :class:`RequestContextResolver` backward-compatibility shim
until the bearer-token surface lands; the header names the Party Identity each
write is recorded under (the body never carries it).

Validates: Requirements 50.4, 50.5, 50.7, 53.2, 54.1, 54.2, 55.7, 58.4, 58.5.
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

from walking_slice.app import SliceServices, create_app
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Party Identities.
#
# Identifier opacity (Slice 1 Requirement 1.7) means the actual UUIDv7 values
# are not the focus of the tests — they only need to be valid canonical
# UUIDv7 strings the persistence layer accepts. Each Party plays one role in
# the denial / separation demonstrations.
# ---------------------------------------------------------------------------


# Pipeline-author Party: holds every authority the cumulative Slice 1 +
# Slice 2 + Slice 3 + Slice 4 chain needs (so the chain-creation calls succeed
# without re-shuffling role assignments mid-test) on the wildcard scope.
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-0000000d4001"

# Measurement Recorder for scenario 1: holds ``view`` + ``record_measurement``
# but NOT ``issue_outcome_review``. Attempts an Outcome Review — denied.
_RECORDER_PARTY_ID = "00000000-0000-7000-8000-0000000d4002"

# Outcome Assessor for scenario 2: holds ``view`` + ``assess_outcome`` but NOT
# ``define_measurement``. Attempts a Measurement Definition — denied.
_ASSESSOR_PARTY_ID = "00000000-0000-7000-8000-0000000d4003"

# Scoped viewer for scenario 7: holds ``view`` only on the *main* scope
# (``_SCOPE``) and nothing on the *restricted* scope (``_RESTRICTED_SCOPE``)
# the imported Measurement Record is recorded under, so the Record node
# redacts while the rest of the chain remains visible.
_SCOPED_VIEWER_PARTY_ID = "00000000-0000-7000-8000-0000000d4004"

# Unauthorized Party for scenario 8: holds no role at all. Drives the
# "restricted Intended Outcome Revision the caller cannot view" half of the
# indistinguishable pair.
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-0000000d4005"

# Resource-steward identity recorded as ``assigning_authority_id`` on every
# Role Assignment. Identifier opacity means this value never reaches the API
# surface; the column just needs a valid Party row.
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-0000000d4006"

# Named-assignee Contributor used to build the Slice 3 completion leg. The
# author cannot assign work to itself (Requirement 23.5 forbids
# self-assignment), so a distinct assignee Party holding ``contribute`` records
# the Work Events and produced Deliverables under the AD-WS-29 assignee binding.
_ASSIGNEE_PARTY_ID = "00000000-0000-7000-8000-0000000d4007"


# ---------------------------------------------------------------------------
# Shared scope, authority-basis, and timing constants.
# ---------------------------------------------------------------------------


# The main scope every chain-creation request body uses. The scoped viewer
# (scenario 7) holds ``view`` here.
_SCOPE = "slice4-denial-demo/pilot"

# The restricted scope the imported Measurement Record and its Definition are
# recorded under for scenario 7 so they redact for the scoped viewer.
_RESTRICTED_SCOPE = "slice4-denial-demo/restricted"

# Authority basis recorded on every authority-bearing record. AD-WS-10 lists
# three accepted ``type`` values; ``role-grant-id`` is chosen for uniformity.
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000d40a1")

# Pin every ``recorded_at`` so the asserted response bodies are deterministic.
# The instant sits inside the slice's pilot horizon.
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Measurement timing: observation precedes retrieval precedes the recorded
# time (the FixedClock instant), satisfying the imported ordering rule
# (Requirement 46.4) and the native observation<=recorded rule (45.3).
_OBSERVATION_TIME = "2026-05-01T00:00:00+00:00"
_RETRIEVAL_TIME = "2026-05-15T00:00:00+00:00"

# A free-text observation-window descriptor imposes no machine-checkable
# bound (the window parser treats non-interval text as informational), so the
# observation-time-within-window check always passes for these fixtures.
_OBSERVATION_WINDOW = "rolling monthly pilot window"
_UNIT = "percent"

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Slice 1 seed Document content. The span targets "quick brown fox".
_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # exclusive end of "quick brown fox"

# Produced-Deliverable content for the Slice 3 completion leg.
_DELIVERABLE_BYTES = b"Slice 4 denial demonstrations: sample deliverable body."

# Distinctive source-system attribute values for scenario 7's redaction test
# so the leak assertion has explicit substrings to scan the response for.
_SECRET_SOURCE_SYSTEM_ID = "EXTERNAL-CRM-SYS-SECRET-7"
_SECRET_SOURCE_RECORD_ID = "EXT-REC-SECRET-99"

# A success-condition string seeded onto the restricted Intended Outcome
# Revision used by scenario 8 so the leak assertion can scan for it.
_RESTRICTED_SUCCESS_CONDITION = (
    "Restricted-success-condition-marker: ninety percent adoption within two "
    "quarters across the confidential pilot."
)


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
    domain concern, not a bootstrap concern — so the test seeds the Parties it
    needs before driving the HTTP layer.
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

    The pipeline-author Party is granted every authority the cumulative
    Slice 1–4 chain needs on the wildcard scope so the chain-creation calls
    succeed without re-shuffling role assignments mid-test. Each demo Party
    holds exactly the authorities its scenario requires so the rejection /
    redaction behaviour under test is the only observable difference between
    the would-be authorized and unauthorized actors.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"slice4-denial-demonstrations-test-secret",
    )
    with engine.begin() as conn:
        _seed_party(conn, _AUTHOR_PARTY_ID, "Slice 4 Pipeline Author")
        _seed_party(conn, _RECORDER_PARTY_ID, "Measurement Recorder Only")
        _seed_party(conn, _ASSESSOR_PARTY_ID, "Outcome Assessor Only")
        _seed_party(conn, _SCOPED_VIEWER_PARTY_ID, "Scoped Viewer")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")
        _seed_party(conn, _ASSIGNEE_PARTY_ID, "Named Assignee Contributor")

    services: SliceServices = app.state.services
    # Pipeline author: every authority on every scope.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="slice4-pipeline-author",
        authorities=(
            "view",
            "modify",
            "review",
            "approve",
            "assign",
            "contribute",
            "accept_milestone",
            "complete",
            "define_measurement",
            "record_measurement",
            "assess_outcome",
            "issue_outcome_review",
        ),
        scope="*",
    )
    # Scenario 1: Measurement Recorder with ``record_measurement`` (+ ``view``)
    # but no ``issue_outcome_review``. Attempts an Outcome Review — denied.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_RECORDER_PARTY_ID,
        role_name="slice4-recorder-only",
        authorities=("view", "record_measurement"),
        scope="*",
    )
    # Scenario 2: Outcome Assessor with ``assess_outcome`` (+ ``view``) but no
    # ``define_measurement``. Attempts a Measurement Definition — denied.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_ASSESSOR_PARTY_ID,
        role_name="slice4-assessor-only",
        authorities=("view", "assess_outcome"),
        scope="*",
    )
    # Scenario 7: Scoped viewer holds ``view`` only on the main scope, so the
    # restricted-scope imported Measurement Record redacts while the rest of
    # the chain (recorded on the main scope) stays visible.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_SCOPED_VIEWER_PARTY_ID,
        role_name="slice4-scoped-viewer",
        authorities=("view",),
        scope=_SCOPE,
    )
    # Scenario 8: Unauthorized Party holds no role assignment.
    # Named assignee Contributor used by the completion-leg seeding helper.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_ASSIGNEE_PARTY_ID,
        role_name="slice4-named-assignee",
        authorities=("view", "contribute"),
        scope="*",
    )
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
# Slice 1 + Slice 2 seeding helpers (author Party).
# ---------------------------------------------------------------------------


def _basis_body() -> dict:
    """The authority-basis sub-object reused on every authority-bearing body."""
    return {"type": "role-grant-id", "id": str(_AUTHORITY_BASIS_ID)}


async def _seed_decision(client: AsyncClient, *, scope: str = _SCOPE) -> dict[str, str]:
    """Seed a Slice 1 Decision (Document → Region → Finding → Recommendation →
    Decision) under the author Party and return the chain identifiers.

    The Decision is recorded with ``outcome='Accept'`` so it is eligible as the
    material source of a downstream Slice 2 Objective (AD-WS-21).
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
                "Recommend documenting the corpus fox observation in the team "
                "playbook and measuring its adoption."
            ),
            "applicable_scope": scope,
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
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
            "omissions": [],
        },
        headers=headers,
    )
    assert decision_response.status_code == 201, decision_response.text
    decision = decision_response.json()
    return {"decision_id": decision["decision_id"]}


async def _seed_objective(
    client: AsyncClient, decision_id: str, *, scope: str = _SCOPE
) -> str:
    """Seed a Slice 2 Objective anchored to the named Decision; return its id."""
    response = await client.post(
        "/api/v1/objectives",
        json={
            "statement": "Establish a reusable playbook anchored to the fox decision.",
            "rationale": "Anchor strategic intent to an authorized Slice 1 decision.",
            "target_decision_id": decision_id,
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["objective_id"]


async def _seed_intended_outcome(
    client: AsyncClient,
    objective_id: str,
    *,
    scope: str = _SCOPE,
    success_condition: str = "Ninety percent of new hires adopt the playbook.",
) -> str:
    """Seed an Intended Outcome (``outcome_kind='intended'``) on the Objective.

    Returns the Intended Outcome Revision Identity — the target every Slice 4
    write addresses.
    """
    response = await client.post(
        "/api/v1/intended-outcomes",
        json={
            "target_objective_id": objective_id,
            "success_condition": success_condition,
            "observation_window": _OBSERVATION_WINDOW,
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["outcome_kind"] == "intended", body
    return body["intended_outcome_revision_id"]


async def _seed_planning_through_completion(
    client: AsyncClient, objective_id: str, *, scope: str = _SCOPE
) -> dict[str, str]:
    """Seed Project → ... → finalized Completion under the named Objective.

    Returns the Completion Record Identity and the produced Deliverable
    Revision Identity so the Outcome Review can cite both legs (the parallel
    Completion → produced Deliverable leg of the Outcome Measurement Provenance
    Chain).
    """
    author = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}

    project_response = await client.post(
        "/api/v1/projects",
        json={
            "target_objective_id": objective_id,
            "name": "Onboarding Playbook Initiative",
            "summary": "Cross-cutting project addressing the onboarding objective.",
            "planned_start_date": "2026-07-01",
            "planned_end_date": "2026-12-31",
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    de_response = await client.post(
        "/api/v1/deliverable-expectations",
        json={
            "target_project_id": project["project_id"],
            "name": "Reusable Onboarding Playbook",
            "description": "A versioned playbook documenting the corpus fox finding.",
            "deliverable_kind": "Document",
            "acceptance_criteria": "Approved by the steering committee.",
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert de_response.status_code == 201, de_response.text
    deliverable_expectation = de_response.json()

    ap_response = await client.post(
        "/api/v1/activity-plans",
        json={
            "target_project_id": project["project_id"],
            "title": "Q3 Onboarding Playbook Activities",
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert ap_response.status_code == 201, ap_response.text
    activity_plan = ap_response.json()

    pr_response = await client.post(
        f"/api/v1/activity-plans/{activity_plan['activity_plan_id']}/plan-revisions",
        json={
            "planned_scope": "Draft, review, and publish the playbook.",
            "deliverable_expectation_refs": [
                deliverable_expectation["deliverable_expectation_id"]
            ],
            "planning_assumptions": ["Two team members co-author each iteration."],
            "ordering_rationale": "Iteration two depends on iteration-one feedback.",
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert pr_response.status_code == 201, pr_response.text
    plan_revision = pr_response.json()
    plan_revision_id = plan_revision["plan_revision_id"]

    approval_response = await client.post(
        f"/api/v1/plan-revisions/{plan_revision_id}/approvals",
        json={
            "outcome": "Approve",
            "rationale": "Authorize the playbook plan so the completion leg can be built.",
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
            "omissions": [],
        },
        headers=author,
    )
    assert approval_response.status_code == 201, approval_response.text

    # Work Assignment → Work Event → produced Deliverable → Production →
    # Milestone Acceptance → Completion (all under the author Party, which
    # holds every authority and is therefore the named assignee).
    wa_response = await client.post(
        "/api/v1/work-assignments",
        json={
            "target_plan_revision_id": plan_revision_id,
            "assignee_party_id": _ASSIGNEE_PARTY_ID,
            "assignment_rationale": "Authorize the assignee to record work on this plan.",
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert wa_response.status_code == 201, wa_response.text
    wa_id = wa_response.json()["work_assignment_id"]

    assignee = {"X-Actor-Party-Id": _ASSIGNEE_PARTY_ID}
    started_response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": wa_id,
            "event_kind": "started",
            "event_note": "Kick-off the playbook drafting work.",
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers=assignee,
    )
    assert started_response.status_code == 201, started_response.text

    deliverable_response = await client.post(
        "/api/v1/deliverables",
        json={
            "content_bytes": base64.b64encode(_DELIVERABLE_BYTES).decode("ascii"),
            "content_type": "text/markdown",
            "produced_deliverable_name": "Onboarding Playbook draft v1",
            "originating_work_assignment_id": wa_id,
        },
        headers=assignee,
    )
    assert deliverable_response.status_code == 201, deliverable_response.text
    deliverable = deliverable_response.json()

    production_response = await client.post(
        "/api/v1/deliverable-productions",
        json={
            "source_work_assignment_id": wa_id,
            "produced_deliverable_revision_id": deliverable["deliverable_revision_id"],
            "target_deliverable_expectation_revision_id": deliverable_expectation[
                "deliverable_expectation_revision_id"
            ],
            "production_rationale": "Record the first iteration of the playbook.",
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers=assignee,
    )
    assert production_response.status_code == 201, production_response.text
    production = production_response.json()

    acceptance_response = await client.post(
        "/api/v1/milestone-acceptances",
        json={
            "source_deliverable_production_id": production["deliverable_production_id"],
            "outcome": "Accept",
            "rationale": "The draft playbook satisfies the acceptance criteria.",
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert acceptance_response.status_code == 201, acceptance_response.text
    acceptance = acceptance_response.json()

    completion_response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": plan_revision_id,
            "outcome": "Completed",
            "rationale": "Record completion of the playbook plan.",
            "source_milestone_acceptance_ids": [
                acceptance["milestone_acceptance_id"]
            ],
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers=author,
    )
    assert completion_response.status_code == 201, completion_response.text
    completion = completion_response.json()
    return {
        "completion_id": completion["completion_id"],
        "deliverable_revision_id": deliverable["deliverable_revision_id"],
    }


# ---------------------------------------------------------------------------
# Slice 4 seeding helpers (author Party).
# ---------------------------------------------------------------------------


async def _seed_measurement_definition(
    client: AsyncClient,
    intended_outcome_revision_id: str,
    *,
    scope: str = _SCOPE,
) -> str:
    """Create a Measurement Definition addressing the Intended Outcome Revision.

    Returns the Measurement Definition Revision Identity.
    """
    response = await client.post(
        "/api/v1/measurement-definitions",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "measurand_description": "Percent of new hires who adopt the playbook.",
            "unit_of_measure": _UNIT,
            "observation_window": _OBSERVATION_WINDOW,
            "cadence": "monthly",
            "data_source": "Onboarding survey export.",
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["measurement_definition_revision_id"]


async def _seed_native_measurement_record(
    client: AsyncClient,
    measurement_definition_revision_id: str,
    *,
    scope: str = _SCOPE,
    observed_value: str = "42",
) -> str:
    """Create a native Measurement Record; return its Identity."""
    response = await client.post(
        "/api/v1/measurement-records",
        json={
            "target_measurement_definition_revision_id": (
                measurement_definition_revision_id
            ),
            "observed_value": observed_value,
            "observed_value_unit": _UNIT,
            "observation_time": _OBSERVATION_TIME,
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["origin"] == "native", body
    return body["measurement_record_id"]


async def _seed_imported_measurement_record(
    client: AsyncClient,
    measurement_definition_revision_id: str,
    *,
    scope: str = _SCOPE,
    source_system_id: str = _SECRET_SOURCE_SYSTEM_ID,
    source_system_record_id: str = _SECRET_SOURCE_RECORD_ID,
    observed_value: str = "55",
):
    """Create an imported Measurement Record; return the raw httpx response.

    Returning the response (rather than just the id) lets the idempotency
    scenario assert on the *second* attempt's status code directly.
    """
    return await client.post(
        "/api/v1/measurement-records/imported",
        json={
            "target_measurement_definition_revision_id": (
                measurement_definition_revision_id
            ),
            "observed_value": observed_value,
            "observed_value_unit": _UNIT,
            "observation_time": _OBSERVATION_TIME,
            "source_system_id": source_system_id,
            "source_system_record_id": source_system_record_id,
            "source_system_authority": "replica",
            "source_system_retrieval_time": _RETRIEVAL_TIME,
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )


async def _seed_observed_outcome(
    client: AsyncClient,
    intended_outcome_revision_id: str,
    cited_measurement_record_ids: list[str],
    *,
    scope: str = _SCOPE,
) -> str:
    """Create an Observed Outcome; return its Revision Identity."""
    response = await client.post(
        "/api/v1/observed-outcomes",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "assessment_summary": "Adoption is trending toward the success condition.",
            "cited_measurement_record_ids": cited_measurement_record_ids,
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["observed_outcome_revision_id"]


async def _seed_assessment(
    client: AsyncClient,
    intended_outcome_revision_id: str,
    sourced_observed_outcome_revision_id: str,
    *,
    scope: str = _SCOPE,
) -> str:
    """Create a Success-Condition Assessment; return its Identity."""
    response = await client.post(
        "/api/v1/success-condition-assessments",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "sourced_observed_outcome_revision_id": (
                sourced_observed_outcome_revision_id
            ),
            "assessment_category": "Partially_Satisfied",
            "assessment_rationale": (
                "Adoption is rising but has not yet reached the success threshold."
            ),
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["assessment_id"]


async def _seed_outcome_review(
    client: AsyncClient,
    *,
    intended_outcome_revision_id: str,
    cited_assessment_ids: list[str],
    cited_completion_ids: list[str],
    cited_produced_deliverable_revision_ids: list[str],
    scope: str = _SCOPE,
) -> str:
    """Create an Outcome Review under the author Party; return its Identity."""
    response = await client.post(
        "/api/v1/outcome-reviews",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "review_outcome": "Partially_Achieved",
            "attribution_stance": "Partial",
            "confidence": "Moderate",
            "review_rationale": (
                "Adoption gains are partially attributable to the playbook rollout."
            ),
            "attribution_evidence_reference": "survey-trend-2026-Q2",
            "cited_assessment_ids": cited_assessment_ids,
            "cited_completion_ids": cited_completion_ids,
            "cited_produced_deliverable_revision_ids": (
                cited_produced_deliverable_revision_ids
            ),
            "authority_basis": _basis_body(),
            "applicable_scope": scope,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()["outcome_review_id"]


async def _seed_chain_through_assessment(
    client: AsyncClient,
    *,
    scope: str = _SCOPE,
    measurement_scope: str | None = None,
) -> dict[str, str]:
    """Drive the full pipeline up to (but not including) the Outcome Review.

    Returns every identifier downstream scenarios need: the target Intended
    Outcome Revision, the Measurement Definition Revision, the native +
    imported Measurement Records, the Observed Outcome Revision, the Assessment,
    the Completion Record, and the produced Deliverable Revision.

    ``measurement_scope`` lets the redaction scenario record the Measurement
    Definition and Records under a *different* scope than the rest of the
    chain so they redact for a scope-limited viewer.
    """
    m_scope = measurement_scope or scope
    decision = await _seed_decision(client, scope=scope)
    objective_id = await _seed_objective(client, decision["decision_id"], scope=scope)
    intended_outcome_revision_id = await _seed_intended_outcome(
        client, objective_id, scope=scope
    )
    completion = await _seed_planning_through_completion(
        client, objective_id, scope=scope
    )
    measurement_definition_revision_id = await _seed_measurement_definition(
        client, intended_outcome_revision_id, scope=m_scope
    )
    native_mr_id = await _seed_native_measurement_record(
        client, measurement_definition_revision_id, scope=m_scope
    )
    imported_response = await _seed_imported_measurement_record(
        client, measurement_definition_revision_id, scope=m_scope
    )
    assert imported_response.status_code == 201, imported_response.text
    imported_mr_id = imported_response.json()["measurement_record_id"]
    observed_outcome_revision_id = await _seed_observed_outcome(
        client,
        intended_outcome_revision_id,
        [native_mr_id, imported_mr_id],
        scope=scope,
    )
    assessment_id = await _seed_assessment(
        client,
        intended_outcome_revision_id,
        observed_outcome_revision_id,
        scope=scope,
    )
    return {
        "intended_outcome_revision_id": intended_outcome_revision_id,
        "measurement_definition_revision_id": measurement_definition_revision_id,
        "native_measurement_record_id": native_mr_id,
        "imported_measurement_record_id": imported_mr_id,
        "observed_outcome_revision_id": observed_outcome_revision_id,
        "assessment_id": assessment_id,
        "completion_id": completion["completion_id"],
        "deliverable_revision_id": completion["deliverable_revision_id"],
    }


# ---------------------------------------------------------------------------
# Database snapshot helpers.
# ---------------------------------------------------------------------------


# Slice 1 + Slice 2 tables the prohibited-attribute scenario compares before
# vs. after the rejected request. Requirement 53.5 / 60.1 require every
# prior-slice row to remain byte-equivalent across any Slice 4 action; this
# scopes the assertion to the planning + knowledge tables adjacent to the
# rejected Observed Outcome.
_PRIOR_SLICE_TABLES: tuple[str, ...] = (
    "Decisions",
    "Recommendations",
    "Recommendation_Revisions",
    "Objectives",
    "Objective_Revisions",
    "Intended_Outcome_Revisions",
)


def _snapshot_tables(
    engine: Engine, tables: tuple[str, ...]
) -> dict[str, list[dict[str, object]]]:
    """Capture an order-independent snapshot of every row in the named tables."""
    snapshot: dict[str, list[dict[str, object]]] = {}
    with engine.connect() as conn:
        for table in tables:
            rows = conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
            snapshot[table] = sorted(
                (dict(row) for row in rows),
                key=lambda r: tuple(sorted((k, str(v)) for k, v in r.items())),
            )
    return snapshot


# ---------------------------------------------------------------------------
# Scenario 1 — A Measurement Recorder attempting an Outcome Review is denied
# with an AD-WS-9-shaped response and a Denial Record
# (Requirements 50.4, 50.7, 54.2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_measurement_recorder_denied_outcome_review_with_ad_ws_9_shape(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A Recorder (no ``issue_outcome_review``) gets the AD-WS-9 denial shape.

    The full Slice 1–4 chain is seeded under the author Party so the Recorder's
    Outcome Review request passes input validation, target resolution, the
    uniqueness pre-check, and citation resolution — the only failing gate is
    the authorization evaluation (``create.outcome_review`` →
    ``issue_outcome_review``). The response is the AD-WS-9 trio and a Denial
    Record with ``action_type='create.outcome_review'`` is appended.
    """
    chain = await _seed_chain_through_assessment(client)

    response = await client.post(
        "/api/v1/outcome-reviews",
        json={
            "target_intended_outcome_revision_id": chain[
                "intended_outcome_revision_id"
            ],
            "review_outcome": "Partially_Achieved",
            "attribution_stance": "Partial",
            "confidence": "Moderate",
            "review_rationale": "Recorder attempts a review beyond its authority.",
            "attribution_evidence_reference": "survey-trend",
            "cited_assessment_ids": [chain["assessment_id"]],
            "cited_completion_ids": [chain["completion_id"]],
            "cited_produced_deliverable_revision_ids": [
                chain["deliverable_revision_id"]
            ],
            "authority_basis": _basis_body(),
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _RECORDER_PARTY_ID},
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    # AD-WS-9 indistinguishable shape — exactly three fields.
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"
    assert detail["reason_code"] in {"out-of-scope", "no-role-assignment"}, detail

    engine: Engine = composed_app.state.engine
    with engine.connect() as conn:
        review_count = conn.execute(
            text("SELECT COUNT(*) FROM Outcome_Review_Records")
        ).scalar_one()
        denial_rows = conn.execute(
            text(
                "SELECT correlation_id FROM Audit_Records "
                "WHERE outcome = 'deny' "
                "  AND action_type = 'create.outcome_review' "
                "  AND actor_party_id = :pid"
            ),
            {"pid": _RECORDER_PARTY_ID},
        ).mappings().all()

    assert review_count == 0, "No Outcome Review Record may persist on a denial."
    assert len(denial_rows) >= 1, (
        "A Denial Record for the rejected create.outcome_review attempt must "
        "be appended (Requirement 50.4 / 57)."
    )
    assert detail["correlation_id"] in {
        row["correlation_id"] for row in denial_rows
    }, "The denial response correlation_id must match the Denial Record."


# ---------------------------------------------------------------------------
# Scenario 2 — An Outcome Assessor attempting a Measurement Definition is
# denied (Requirements 50.4, 50.7).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_assessor_denied_measurement_definition_with_ad_ws_9_shape(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """An Assessor (no ``define_measurement``) gets the AD-WS-9 denial shape.

    A fresh Intended Outcome with no Measurement Definition yet is seeded so
    the Assessor's request passes target resolution and the uniqueness
    pre-check; the only failing gate is authorization
    (``create.measurement_definition`` → ``define_measurement``).
    """
    decision = await _seed_decision(client)
    objective_id = await _seed_objective(client, decision["decision_id"])
    intended_outcome_revision_id = await _seed_intended_outcome(client, objective_id)

    response = await client.post(
        "/api/v1/measurement-definitions",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "measurand_description": "Assessor attempts to define a measurement.",
            "unit_of_measure": _UNIT,
            "observation_window": _OBSERVATION_WINDOW,
            "cadence": "monthly",
            "data_source": "Onboarding survey export.",
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _ASSESSOR_PARTY_ID},
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"

    engine: Engine = composed_app.state.engine
    with engine.connect() as conn:
        definition_count = conn.execute(
            text("SELECT COUNT(*) FROM Measurement_Definitions")
        ).scalar_one()
        denial_rows = conn.execute(
            text(
                "SELECT correlation_id FROM Audit_Records "
                "WHERE outcome = 'deny' "
                "  AND action_type = 'create.measurement_definition' "
                "  AND actor_party_id = :pid"
            ),
            {"pid": _ASSESSOR_PARTY_ID},
        ).mappings().all()

    assert definition_count == 0, "No Measurement Definition may persist on a denial."
    assert len(denial_rows) >= 1, (
        "A Denial Record for the rejected create.measurement_definition attempt "
        "must be appended (Requirement 50.4 / 57)."
    )
    assert detail["correlation_id"] in {row["correlation_id"] for row in denial_rows}


# ---------------------------------------------------------------------------
# Scenario 3 — A prohibited intended-side attribute is rejected with no row
# persisted and no prior-slice row mutated (Requirement 53.2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prohibited_intended_side_attribute_rejected_no_mutation(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """An Observed Outcome carrying an ``intended-`` / ``planned-`` key is rejected.

    The Outcome_Service prohibited-attribute screen fires at the API boundary
    so the response carries ``failed_constraint='prohibited_attribute'`` and the
    offending key. No Observed Outcome row is persisted and every snapshotted
    Slice 1 / Slice 2 row remains byte-equivalent (Property — Plan/Outcome
    separation, Requirement 53.2 / 53.5).
    """
    engine: Engine = composed_app.state.engine

    decision = await _seed_decision(client)
    objective_id = await _seed_objective(client, decision["decision_id"])
    intended_outcome_revision_id = await _seed_intended_outcome(client, objective_id)
    definition_revision_id = await _seed_measurement_definition(
        client, intended_outcome_revision_id
    )
    native_mr_id = await _seed_native_measurement_record(
        client, definition_revision_id
    )

    before = _snapshot_tables(engine, _PRIOR_SLICE_TABLES)
    with engine.connect() as conn:
        observed_count_before = conn.execute(
            text("SELECT COUNT(*) FROM Observed_Outcomes")
        ).scalar_one()

    for prohibited_key in ("intended-target-success", "planned-rollout-date"):
        response = await client.post(
            "/api/v1/observed-outcomes",
            json={
                "target_intended_outcome_revision_id": intended_outcome_revision_id,
                "assessment_summary": "Carries a prohibited intended-side attribute.",
                "cited_measurement_record_ids": [native_mr_id],
                "applicable_scope": _SCOPE,
                prohibited_key: "should be rejected",
            },
            headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
        )
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert detail.get("failed_constraint") == "prohibited_attribute", detail
        assert prohibited_key in detail.get("prohibited_keys", []), detail

    with engine.connect() as conn:
        observed_count_after = conn.execute(
            text("SELECT COUNT(*) FROM Observed_Outcomes")
        ).scalar_one()
    after = _snapshot_tables(engine, _PRIOR_SLICE_TABLES)

    assert observed_count_after == observed_count_before, (
        "No Observed Outcome row may be persisted from a prohibited-attribute "
        "rejection (Requirement 53.2)."
    )
    assert after == before, (
        "Every prior-slice row must remain byte-equivalent across the rejected "
        "Slice 4 request (Requirement 53.5 / 60.1)."
    )


# ---------------------------------------------------------------------------
# Scenario 4 — An Observed Outcome with ``outcome_kind`` other than
# ``observed`` is rejected (Requirement 54.1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observed_outcome_with_non_observed_kind_rejected(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Supplying ``outcome_kind='intended'`` on an Observed Outcome is rejected.

    The Output/Outcome and Intended/Observed separation (Requirement 54.1)
    forbids aliasing an Observed Outcome with any ``outcome_kind`` other than
    ``observed``; the service rejects the request with
    ``failed_constraint='outcome_kind_invalid'`` and persists no row.
    """
    engine: Engine = composed_app.state.engine

    decision = await _seed_decision(client)
    objective_id = await _seed_objective(client, decision["decision_id"])
    intended_outcome_revision_id = await _seed_intended_outcome(client, objective_id)
    definition_revision_id = await _seed_measurement_definition(
        client, intended_outcome_revision_id
    )
    native_mr_id = await _seed_native_measurement_record(
        client, definition_revision_id
    )

    response = await client.post(
        "/api/v1/observed-outcomes",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "assessment_summary": "Attempts to declare a non-observed outcome kind.",
            "cited_measurement_record_ids": [native_mr_id],
            "applicable_scope": _SCOPE,
            "outcome_kind": "intended",
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail.get("failed_constraint") == "outcome_kind_invalid", detail

    with engine.connect() as conn:
        observed_count = conn.execute(
            text("SELECT COUNT(*) FROM Observed_Outcomes")
        ).scalar_one()
    assert observed_count == 0, "No Observed Outcome row may persist on rejection."


# ---------------------------------------------------------------------------
# Scenario 5 — An Outcome Review with stance ``Asserted`` and an empty
# attribution-evidence reference is rejected (Requirements 54.1, 54.2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_asserted_outcome_review_without_evidence_rejected(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """An ``Asserted`` Outcome Review with no attribution evidence is rejected.

    Requirement 49.4 / 54.2 require a non-empty attribution-evidence reference
    whenever the attribution stance is ``Asserted`` (or ``Contradicted``). The
    rule fires during input validation — before authorization — so the author
    Party (which holds ``issue_outcome_review``) still receives the structured
    400 with ``failed_constraint='attribution_evidence_reference_missing_for_stance'``.
    """
    engine: Engine = composed_app.state.engine

    decision = await _seed_decision(client)
    objective_id = await _seed_objective(client, decision["decision_id"])
    intended_outcome_revision_id = await _seed_intended_outcome(client, objective_id)

    # The citation identifiers are never resolved because validation fails
    # first; they only need to make the cited lists non-empty.
    response = await client.post(
        "/api/v1/outcome-reviews",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "review_outcome": "Achieved",
            "attribution_stance": "Asserted",
            "confidence": "High",
            "review_rationale": "Asserts attribution without supplying evidence.",
            "attribution_evidence_reference": "",
            "cited_assessment_ids": [str(uuid.uuid4())],
            "cited_completion_ids": [str(uuid.uuid4())],
            "cited_produced_deliverable_revision_ids": [],
            "authority_basis": _basis_body(),
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail.get("failed_constraint") == (
        "attribution_evidence_reference_missing_for_stance"
    ), detail

    with engine.connect() as conn:
        review_count = conn.execute(
            text("SELECT COUNT(*) FROM Outcome_Review_Records")
        ).scalar_one()
    assert review_count == 0, "No Outcome Review Record may persist on rejection."


# ---------------------------------------------------------------------------
# Scenario 6 — A second imported Measurement Record with a matching
# source-system pair is rejected (Requirement 58.4 / AD-WS-39).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_imported_measurement_record_rejected(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A second imported Record with a matching source-system pair is rejected.

    The AD-WS-39 idempotency key rejects a duplicate
    ``(source_system_id, source_system_record_id)`` pair per Measurement
    Definition Revision with HTTP 409 and no second Record persisted; the first
    Record remains byte-equivalent.
    """
    engine: Engine = composed_app.state.engine

    decision = await _seed_decision(client)
    objective_id = await _seed_objective(client, decision["decision_id"])
    intended_outcome_revision_id = await _seed_intended_outcome(client, objective_id)
    definition_revision_id = await _seed_measurement_definition(
        client, intended_outcome_revision_id
    )

    first = await _seed_imported_measurement_record(
        client, definition_revision_id, observed_value="55"
    )
    assert first.status_code == 201, first.text
    first_record_id = first.json()["measurement_record_id"]

    with engine.connect() as conn:
        first_row_before = conn.execute(
            text("SELECT * FROM Measurement_Records WHERE measurement_record_id = :rid"),
            {"rid": first_record_id},
        ).mappings().one()
        first_row_before = dict(first_row_before)

    # Second import: same source-system pair, different observed value.
    second = await _seed_imported_measurement_record(
        client, definition_revision_id, observed_value="77"
    )

    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail.get("error_code") in {
        "imported_measurement_duplicate",
        "duplicate_imported_measurement_record",
    } or "duplicate" in str(detail).lower(), detail

    with engine.connect() as conn:
        matching_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Measurement_Records "
                "WHERE source_system_id = :sid AND source_system_record_id = :rid"
            ),
            {"sid": _SECRET_SOURCE_SYSTEM_ID, "rid": _SECRET_SOURCE_RECORD_ID},
        ).scalar_one()
        first_row_after = conn.execute(
            text("SELECT * FROM Measurement_Records WHERE measurement_record_id = :rid"),
            {"rid": first_record_id},
        ).mappings().one()
        first_row_after = dict(first_row_after)

    assert matching_count == 1, (
        "Only the first imported Measurement Record may persist for the "
        "source-system pair (Requirement 58.4 / AD-WS-39)."
    )
    assert first_row_after == first_row_before, (
        "The first imported Measurement Record must remain byte-equivalent."
    )


# ---------------------------------------------------------------------------
# Scenario 7 — An unauthorized requester reading an imported Measurement Record
# receives the {kind, redacted: true} marker with no source-system attribute
# leakage (Requirements 55.7, 58.5).
# ---------------------------------------------------------------------------


def _find_redacted_measurement_record(node: object) -> bool:
    """Recursively search a serialized provenance tree for the redaction marker.

    Returns ``True`` when a ``{"kind": "measurement_record", "redacted": true}``
    marker is present anywhere in the tree.
    """
    if isinstance(node, dict):
        if node.get("kind") == "measurement_record" and node.get("redacted") is True:
            return True
        return any(_find_redacted_measurement_record(v) for v in node.values())
    if isinstance(node, (list, tuple)):
        return any(_find_redacted_measurement_record(item) for item in node)
    return False


@pytest.mark.asyncio
async def test_imported_measurement_record_redacts_for_unauthorized_reader(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A scope-limited reader sees the imported Record as a redaction marker.

    The chain's Measurement Definition and Records are recorded under the
    restricted scope while the rest of the chain (Intended Outcome, Observed
    Outcome, Assessment, Outcome Review) is recorded under the main scope. The
    scoped viewer holds ``view`` only on the main scope, so navigating the
    Outcome Measurement Provenance Chain surfaces the imported Measurement
    Record as the AD-WS-34 ``{kind, redacted: true}`` marker — the restricted
    source-system attributes never leak (Requirement 58.5).
    """
    chain = await _seed_chain_through_assessment(
        client, scope=_SCOPE, measurement_scope=_RESTRICTED_SCOPE
    )
    outcome_review_id = await _seed_outcome_review(
        client,
        intended_outcome_revision_id=chain["intended_outcome_revision_id"],
        cited_assessment_ids=[chain["assessment_id"]],
        cited_completion_ids=[chain["completion_id"]],
        cited_produced_deliverable_revision_ids=[chain["deliverable_revision_id"]],
        scope=_SCOPE,
    )

    response = await client.get(
        f"/api/v1/outcome-reviews/{outcome_review_id}/provenance",
        headers={"X-Actor-Party-Id": _SCOPED_VIEWER_PARTY_ID},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    body_text = response.text

    # The imported Measurement Record node redacts to the AD-WS-34 marker.
    assert _find_redacted_measurement_record(body), (
        "The restricted-scope Measurement Record must surface as a "
        "{kind: 'measurement_record', redacted: true} marker for the "
        "scope-limited viewer (Requirement 55.7 / 58.5)."
    )

    # No restricted source-system attribute leaks anywhere in the response.
    assert _SECRET_SOURCE_SYSTEM_ID not in body_text, (
        "The source-system identifier must never leak through a redacted "
        "Measurement Record (Requirement 58.5)."
    )
    assert _SECRET_SOURCE_RECORD_ID not in body_text, (
        "The source-system record identifier must never leak (Requirement 58.5)."
    )

    # Sanity: the surrounding chain is visible (the viewer holds view on the
    # main scope), so the redaction is genuinely mid-chain rather than a
    # whole-tree denial. The Outcome Review root resolved (status 200) and the
    # tree carries at least one assessment chain.
    assert body.get("outcome_review") is not None, body
    assert body.get("assessment_chains"), body


# ---------------------------------------------------------------------------
# Scenario 8 — A creation against a non-existent Intended Outcome Revision is
# indistinguishable from one against a restricted Revision the caller cannot
# view (Requirements 50.5, 50.7).
# ---------------------------------------------------------------------------


# Slice 4 / prior-slice attribute keys that must never appear in either
# indistinguishable-denial response body (they would let the caller infer the
# restricted target's attributes).
_FORBIDDEN_TARGET_BODY_KEYS: frozenset[str] = frozenset(
    {
        "success_condition",
        "observation_window",
        "attribution_assumption",
        "target_objective_id",
        "outcome_kind",
    }
)


@pytest.mark.asyncio
async def test_non_existent_vs_restricted_intended_outcome_indistinguishable(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Non-existent and restricted target attempts leak no target information.

    Two ``POST /measurement-definitions`` attempts are driven by the
    unauthorized Party:

    - **Non-existent target** — the ``target_intended_outcome_revision_id`` is a
      freshly minted UUIDv7 that does not resolve to any row.
    - **Restricted target** — the identifier names a real Intended Outcome
      Revision (carrying a distinctive ``success_condition``) the unauthorized
      Party cannot view.

    Both attempts fail (4xx) and neither response leaks any attribute of the
    target the caller did not already supply on the request body
    (Requirements 50.5, 50.7, AD-WS-9 rule 1).
    """
    # Universe A: a canonical UUIDv7 that resolves to no row.
    non_existent_id = "00000000-0000-7000-8000-0000000def01"
    non_existent_response = await client.post(
        "/api/v1/measurement-definitions",
        json={
            "target_intended_outcome_revision_id": non_existent_id,
            "measurand_description": "Targets a non-existent Intended Outcome.",
            "unit_of_measure": _UNIT,
            "observation_window": _OBSERVATION_WINDOW,
            "cadence": "monthly",
            "data_source": "Onboarding survey export.",
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )

    # Universe B: a real Intended Outcome Revision the caller cannot view,
    # carrying a distinctive success-condition marker.
    decision = await _seed_decision(client, scope=_RESTRICTED_SCOPE)
    objective_id = await _seed_objective(
        client, decision["decision_id"], scope=_RESTRICTED_SCOPE
    )
    restricted_id = await _seed_intended_outcome(
        client,
        objective_id,
        scope=_RESTRICTED_SCOPE,
        success_condition=_RESTRICTED_SUCCESS_CONDITION,
    )
    restricted_response = await client.post(
        "/api/v1/measurement-definitions",
        json={
            "target_intended_outcome_revision_id": restricted_id,
            "measurand_description": "Targets a restricted Intended Outcome.",
            "unit_of_measure": _UNIT,
            "observation_window": _OBSERVATION_WINDOW,
            "cadence": "monthly",
            "data_source": "Onboarding survey export.",
            "applicable_scope": _SCOPE,
        },
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )

    # Both attempts fail without a consequential write.
    assert 400 <= non_existent_response.status_code < 500, non_existent_response.text
    assert 400 <= restricted_response.status_code < 500, restricted_response.text

    # Neither response discloses the restricted target's success condition.
    assert _RESTRICTED_SUCCESS_CONDITION not in non_existent_response.text
    assert _RESTRICTED_SUCCESS_CONDITION not in restricted_response.text, (
        "The restricted Intended Outcome's success_condition must not leak "
        "(Requirement 50.5 / 50.7 / AD-WS-9 rule 1)."
    )

    # Neither detail body carries any target-attribute key the caller did not
    # supply.
    for resp in (non_existent_response, restricted_response):
        detail = resp.json().get("detail", {})
        detail_keys = set(detail.keys()) if isinstance(detail, dict) else set()
        leaked = detail_keys & _FORBIDDEN_TARGET_BODY_KEYS
        assert not leaked, f"Response leaked target attribute keys: {leaked}."

    # No Measurement Definition was persisted from either denied attempt.
    engine: Engine = composed_app.state.engine
    with engine.connect() as conn:
        definition_count = conn.execute(
            text("SELECT COUNT(*) FROM Measurement_Definitions")
        ).scalar_one()
    assert definition_count == 0, (
        "Neither indistinguishable-denial attempt may persist a Measurement "
        "Definition."
    )
