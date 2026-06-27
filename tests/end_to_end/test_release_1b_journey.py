"""End-to-end HTTP test for the Release 1B journey (task 17.1).

This test drives the fully-composed FastAPI app — built by
:func:`walking_slice.app.create_app` — through :class:`httpx.AsyncClient`
over the ASGI transport, exercising the full Release 1B pipeline from
captured Evidence through an approved Plan Revision and back along
the Planning Provenance Chain to the originating Document Revision
text.

Pipeline (one transaction per stage, all stages observable to the
authoring Party):

1. **Capture Evidence (Slice 1).** ``POST /api/v1/documents`` followed
   by ``POST /api/v1/documents/{rid}/revisions/{rev}/regions`` to
   record a Source Document and a Region Occurrence over the span
   ``content_bytes[start:end] = b"quick brown fox"``.
2. **Create Finding (Slice 1).** ``POST /api/v1/findings`` citing the
   seeded Region Occurrence.
3. **Create Recommendation (Slice 1).** ``POST /api/v1/recommendations``
   derived from the Finding.
4. **Record Decision (Slice 1, Requirement 6 in slice 1).**
   ``POST /api/v1/recommendations/{rid}/decisions`` with
   ``outcome = "Accept"`` so the resulting Decision is eligible as
   the material source of a Slice 2 Objective.
5. **Create Objective (Requirement 2.1).** ``POST /api/v1/objectives``
   targeting the authorized Decision.
6. **Record Intended Outcome (Requirement 3.1).**
   ``POST /api/v1/intended-outcomes`` targeting the Objective; the
   response carries ``outcome_kind = "intended"`` per Requirement 3.2.
7. **Create Project (Requirement 4.1).** ``POST /api/v1/projects``
   addressing the Objective with a well-ordered planned-date range.
8. **Declare Deliverable Expectation (Requirement 5.1).**
   ``POST /api/v1/deliverable-expectations`` on the Project with a
   ``deliverable_kind`` drawn from the enumerated set.
9. **Create Activity Plan (Requirement 6.1).**
   ``POST /api/v1/activity-plans`` within the Project.
10. **Submit Plan Revision (Requirement 7.1).**
    ``POST /api/v1/activity-plans/{ap_id}/plan-revisions`` records a
    Draft Plan Revision (``lifecycle_state = "draft"``).
11. **Record Plan Review (Requirement 8.1).**
    ``POST /api/v1/plan-revisions/{pr_id}/reviews`` records an
    ``Endorse`` review against the Draft Plan Revision; the
    Plan Revision's lifecycle_state is left unchanged
    per Requirement 8.7 (verified indirectly through step 12).
12. **Approve Plan Revision (Requirement 9.1).**
    ``POST /api/v1/plan-revisions/{pr_id}/approvals`` with
    ``outcome = "Approve"`` records a Plan Approval Record and
    transitions the Plan Revision to ``approved`` atomically inside
    one transaction (AD-WS-19 / Requirement 9.7).
13. **Navigate Planning Provenance Chain (Requirement 14.1).**
    ``GET /api/v1/plan-approvals/{pa_id}/provenance`` returns the
    full ordered chain Plan Approval → Plan Revision → Activity Plan
    → Project Revision → Objective Revision → Decision →
    Recommendation Revision → Finding Revision → Region Occurrence
    → Document Revision. The test asserts every node resolves and
    the Region Occurrence's bounded text round-trips byte-equivalent
    to the originally captured Document content slice.

Authorities required by the pipeline are granted via a single broad
Role Assignment on the wildcard scope ``"*"`` so every action's
``TargetRef`` (the call-site ``applicable_scope`` for the create
actions, the resolved resource identity for the ``view.<kind>``
checks performed by the navigator) is covered. The pipeline-author
Party therefore holds ``view``, ``modify``, ``review``, and
``approve`` authority everywhere — the test is about the *journey*,
not the per-stage authorization-aware behavior, which is covered by
:mod:`tests.end_to_end.test_demonstrations` and the Property 17 /
Property 18 suites.

Validates: Requirements 2.1, 3.1, 4.1, 5.1, 6.1, 7.1, 8.1, 9.1, 14.1.
"""

from __future__ import annotations

import base64
import hashlib
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
# Constants.
# ---------------------------------------------------------------------------


# Pipeline-author Party: holds view + modify + review + approve
# authority on the wildcard scope so every step of the Release 1B
# journey succeeds without scope-shuffling between steps.
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-00000017a001"

# Resource-steward identity recorded as the ``assigning_authority_id``
# on the seeded Role Assignment. Identifier opacity (Requirement 1.7)
# means this value never reaches the API surface; the column just
# needs a valid Party row.
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-00000017a005"

# A single shared scope used as the ``applicable_scope`` for every
# creation request. The Role Assignment seeded by ``composed_app`` is
# pinned to ``"*"`` so this exact string never has to match anything
# server-side, but it travels on every request body so the persisted
# columns carry a recognisable test scope.
_SCOPE = "release-1b/pilot-team"

# Authority-basis identifier used for every authority-bearing record
# (Decision, Plan Review, Plan Approval). The basis is a stable
# constant per AD-WS-10 and is not the focus of this test.
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000017a0a1")

# Pin every ``recorded_at`` so the asserted response bodies are
# deterministic across runs.
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Document content used to seed the Slice 1 leg of the pipeline. The
# span offsets target the substring ``"quick brown fox"`` so the
# byte-equivalence assertion in the navigation step is grep-friendly.
_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # exclusive end of "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]
_EXPECTED_DOC_DIGEST = hashlib.sha256(_DOC_CONTENT).hexdigest()
_EXPECTED_SPAN_DIGEST = hashlib.sha256(_EXPECTED_SPAN_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# Engine + Party seeding helpers.
# ---------------------------------------------------------------------------


def _build_engine(tmp_path: Path) -> Engine:
    """Construct a per-test on-disk SQLite engine.

    Mirrors the engine-construction helper used by
    :mod:`tests.end_to_end.test_demonstrations`: ``journal_mode=WAL``
    plus ``foreign_keys=ON`` so the persistence-layer triggers and
    foreign keys behave as production.
    """
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

    The composed app does not seed Parties on startup — Party rows are
    a domain concern, not a bootstrap concern — so the test seeds the
    two Parties it needs before driving the HTTP layer.
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
    """A fully-composed FastAPI app with the pipeline-author Party seeded.

    The clock is pinned to :data:`_FIXED_INSTANT` so every recorded
    ``recorded_at`` is byte-equivalent across runs; the JWT secret is
    pinned to a constant so any future bearer-token surface
    reproduces. The pipeline-author Party is granted every authority
    Release 1B needs (``view``, ``modify``, ``review``, ``approve``)
    on the wildcard scope so the full pipeline succeeds without
    re-shuffling role assignments mid-test.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"release-1b-e2e-test-secret",
    )
    with engine.begin() as conn:
        _seed_party(conn, _AUTHOR_PARTY_ID, "Release 1B Pipeline Author")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")

    services: SliceServices = app.state.services
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="release-1b-pipeline-author",
        # ``review`` is the Slice 2 authority added by AD-WS-15 and
        # mapped to ``create.plan_review``; ``approve`` is mapped to
        # ``create.plan_approval``. ``modify`` covers every Slice 2
        # ``create.*`` action other than the two above and every
        # Slice 1 modify-authority action (create.recommendation,
        # create.decision, ...). ``view`` covers every backlink and
        # provenance walk the navigator performs.
        authorities=("view", "modify", "review", "approve"),
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
# The end-to-end pipeline test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_1b_pipeline_end_to_end(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Drive the full Release 1B pipeline end-to-end through HTTP.

    Each step asserts the HTTP contract documented on the matching
    requirement clause (creation returns 201 with a canonical UUIDv7
    identifier and the per-Resource declarative attributes) and
    threads the returned identifiers into the next step. The final
    step navigates the Planning Provenance Chain rooted at the issued
    Plan Approval Record and asserts the chain returns visible nodes
    end-to-end with the Region Occurrence's bounded text matching
    the originally captured Document content slice.
    """
    headers = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}

    # ----- 1. Capture Evidence (Slice 1) ----------------------------
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
    document_resource_id = document["resource_id"]
    document_revision_id = document["revision_id"]
    assert _CANONICAL_UUID7_REGEX.match(document_resource_id), document
    assert _CANONICAL_UUID7_REGEX.match(document_revision_id), document

    region_response = await client.post(
        f"/api/v1/documents/{document_resource_id}/revisions/"
        f"{document_revision_id}/regions",
        json={
            "start_offset_bytes": _DOC_SPAN_START,
            "end_offset_bytes": _DOC_SPAN_END,
            "contributing_party_id": _AUTHOR_PARTY_ID,
        },
        headers=headers,
    )
    assert region_response.status_code == 201, region_response.text
    region = region_response.json()
    assert _CANONICAL_UUID7_REGEX.match(region["region_id"]), region

    # ----- 2. Create Finding (Slice 1) ------------------------------
    finding_response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "The corpus documents a quick brown fox.",
            "authoring_party_id": _AUTHOR_PARTY_ID,
            "is_hypothesis": False,
            "supporting_region_occurrences": [
                {
                    "region_id": region["region_id"],
                    "document_revision_id": document_revision_id,
                }
            ],
        },
        headers=headers,
    )
    assert finding_response.status_code == 201, finding_response.text
    finding = finding_response.json()
    assert _CANONICAL_UUID7_REGEX.match(finding["finding_id"]), finding

    # ----- 3. Create Recommendation (Slice 1) -----------------------
    rec_response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _AUTHOR_PARTY_ID,
            "derived_from_findings": [finding["finding_id"]],
            "rationale": (
                "Recommend documenting the fox observation in the team "
                "playbook so future cohorts inherit the insight."
            ),
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert rec_response.status_code == 201, rec_response.text
    recommendation = rec_response.json()
    assert _CANONICAL_UUID7_REGEX.match(
        recommendation["recommendation_id"]
    ), recommendation

    # ----- 4. Record Decision (Slice 1) -----------------------------
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
    assert decision["outcome"] == "Accept"
    assert _CANONICAL_UUID7_REGEX.match(decision["decision_id"]), decision

    # ----- 5. Create Objective (Slice 2 — Requirement 2.1) ----------
    objective_response = await client.post(
        "/api/v1/objectives",
        json={
            "statement": (
                "Establish a reusable playbook anchored to the corpus "
                "fox finding within one quarter."
            ),
            "rationale": (
                "Anchors strategic intent to the authorized Slice 1 "
                "decision so downstream planning artifacts inherit "
                "the evidence chain."
            ),
            "target_decision_id": decision["decision_id"],
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert objective_response.status_code == 201, objective_response.text
    objective = objective_response.json()
    assert _CANONICAL_UUID7_REGEX.match(objective["objective_id"]), objective
    assert objective["target_decision_id"] == decision["decision_id"]
    assert objective["applicable_scope"] == _SCOPE

    # ----- 6. Record Intended Outcome (Requirement 3.1) -------------
    io_response = await client.post(
        "/api/v1/intended-outcomes",
        json={
            "target_objective_id": objective["objective_id"],
            "success_condition": (
                "Every new team member references the playbook within "
                "their first sprint."
            ),
            "observation_window": "The first full quarter after rollout.",
            "attribution_assumption": (
                "No other onboarding artifacts are introduced during "
                "the observation window."
            ),
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert io_response.status_code == 201, io_response.text
    intended_outcome = io_response.json()
    assert intended_outcome["outcome_kind"] == "intended"
    assert intended_outcome["target_objective_id"] == objective["objective_id"]

    # ----- 7. Create Project (Requirement 4.1) ----------------------
    project_response = await client.post(
        "/api/v1/projects",
        json={
            "target_objective_id": objective["objective_id"],
            "name": "Onboarding Playbook Initiative",
            "summary": (
                "Cross-cutting project addressing the onboarding "
                "objective and Slice 1 fox decision."
            ),
            "planned_start_date": "2026-07-01",
            "planned_end_date": "2026-12-31",
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()
    assert _CANONICAL_UUID7_REGEX.match(project["project_id"]), project
    assert project["target_objective_id"] == objective["objective_id"]

    # ----- 8. Declare Deliverable Expectation (Requirement 5.1) -----
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
    assert deliverable_expectation["deliverable_kind"] == "Document"

    # ----- 9. Create Activity Plan (Requirement 6.1) ----------------
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
    assert _CANONICAL_UUID7_REGEX.match(
        activity_plan["activity_plan_id"]
    ), activity_plan
    assert activity_plan["target_project_id"] == project["project_id"]

    # ----- 10. Submit Plan Revision (Requirement 7.1) ---------------
    pr_response = await client.post(
        f"/api/v1/activity-plans/{activity_plan['activity_plan_id']}"
        "/plan-revisions",
        json={
            "planned_scope": (
                "Draft, review, and publish the onboarding playbook "
                "in two iterations during the quarter."
            ),
            "deliverable_expectation_refs": [
                deliverable_expectation["deliverable_expectation_id"],
            ],
            "planning_assumptions": [
                "Two team members can co-author each iteration.",
                "Steering committee reviews occur within five "
                "business days of submission.",
            ],
            "ordering_rationale": (
                "Iteration two depends on feedback gathered during "
                "iteration one."
            ),
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert pr_response.status_code == 201, pr_response.text
    plan_revision = pr_response.json()
    assert _CANONICAL_UUID7_REGEX.match(
        plan_revision["plan_revision_id"]
    ), plan_revision
    assert plan_revision["lifecycle_state"] == "draft"
    assert plan_revision["target_activity_plan_id"] == (
        activity_plan["activity_plan_id"]
    )

    # ----- 11. Record Plan Review (Requirement 8.1) -----------------
    review_response = await client.post(
        f"/api/v1/plan-revisions/{plan_revision['plan_revision_id']}/reviews",
        json={
            "outcome": "Endorse",
            "rationale": (
                "The plan's two-iteration approach is sound and the "
                "stated assumptions are reasonable for the cohort."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=headers,
    )
    assert review_response.status_code == 201, review_response.text
    plan_review = review_response.json()
    assert plan_review["outcome"] == "Endorse"
    assert plan_review["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )

    # The Plan Review must not transition the Plan Revision lifecycle
    # (Requirement 8.7) — re-fetch the Plan Revision row and confirm
    # ``lifecycle_state`` is still ``"draft"``.
    pr_get_response = await client.get(
        f"/api/v1/activity-plans/{activity_plan['activity_plan_id']}"
        f"/plan-revisions/{plan_revision['plan_revision_id']}",
        headers=headers,
    )
    assert pr_get_response.status_code == 200, pr_get_response.text
    assert pr_get_response.json()["lifecycle_state"] == "draft"

    # ----- 12. Approve Plan Revision (Requirement 9.1) --------------
    approval_response = await client.post(
        f"/api/v1/plan-revisions/{plan_revision['plan_revision_id']}"
        "/approvals",
        json={
            "outcome": "Approve",
            "rationale": (
                "Steering committee approves the plan based on the "
                "endorsing Plan Review and the linked Slice 1 fox "
                "decision."
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
    plan_approval = approval_response.json()
    assert _CANONICAL_UUID7_REGEX.match(
        plan_approval["plan_approval_id"]
    ), plan_approval
    assert plan_approval["outcome"] == "Approve"
    assert plan_approval["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )
    assert plan_approval["target_activity_plan_id"] == (
        activity_plan["activity_plan_id"]
    )
    # Plan Approval transitions the Plan Revision to ``approved``
    # atomically inside the same transaction as the Plan Approval
    # Record insert (Requirement 9.7 / AD-WS-19).
    assert plan_approval["new_lifecycle_state"] == "approved"

    # Confirm the lifecycle transition is observable through the
    # Plan Revision read endpoint.
    pr_after_approval = await client.get(
        f"/api/v1/activity-plans/{activity_plan['activity_plan_id']}"
        f"/plan-revisions/{plan_revision['plan_revision_id']}",
        headers=headers,
    )
    assert pr_after_approval.status_code == 200, pr_after_approval.text
    assert pr_after_approval.json()["lifecycle_state"] == "approved"

    # ----- 13. Navigate Planning Provenance Chain (Requirement 14.1)
    chain_response = await client.get(
        f"/api/v1/plan-approvals/{plan_approval['plan_approval_id']}"
        "/provenance",
        headers=headers,
    )
    assert chain_response.status_code == 200, chain_response.text
    chain = chain_response.json()

    # ----- 13a. Plan Approval head node ----------------------------
    pa_node = chain["plan_approval"]
    assert pa_node["plan_approval_id"] == plan_approval["plan_approval_id"]
    assert pa_node["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )
    assert pa_node["target_activity_plan_id"] == (
        activity_plan["activity_plan_id"]
    )
    assert pa_node["outcome"] == "Approve"

    # ----- 13b. Plan Revision (approved) ---------------------------
    pr_node = chain["plan_revision"]
    assert pr_node.get("redacted") is not True, pr_node
    assert pr_node["plan_revision_id"] == plan_revision["plan_revision_id"]
    assert pr_node["activity_plan_id"] == activity_plan["activity_plan_id"]
    assert pr_node["lifecycle_state"] == "approved"

    # ----- 13c. Activity Plan --------------------------------------
    ap_node = chain["activity_plan"]
    assert ap_node.get("redacted") is not True, ap_node
    assert ap_node["activity_plan_id"] == activity_plan["activity_plan_id"]
    assert ap_node["target_project_id"] == project["project_id"]

    # ----- 13d. Project Revision -----------------------------------
    project_node = chain["project_revision"]
    assert project_node.get("redacted") is not True, project_node
    assert project_node["project_id"] == project["project_id"]
    assert project_node["target_objective_id"] == objective["objective_id"]

    # ----- 13e. Objective Revision ---------------------------------
    objective_node = chain["objective_revision"]
    assert objective_node.get("redacted") is not True, objective_node
    assert objective_node["objective_id"] == objective["objective_id"]
    assert objective_node["target_decision_id"] == decision["decision_id"]

    # ----- 13f. Slice 1 Decision tail ------------------------------
    decision_chain = chain["decision_chain"]
    assert decision_chain is not None, chain
    assert decision_chain["decision"]["decision_id"] == decision["decision_id"]
    assert decision_chain["decision"]["outcome"] == "Accept"

    rec_node = decision_chain["recommendation_revision"]
    assert rec_node.get("redacted") is not True, rec_node
    assert rec_node["recommendation_id"] == recommendation["recommendation_id"]

    findings_nodes = decision_chain["findings"]
    assert len(findings_nodes) == 1, findings_nodes
    finding_node = findings_nodes[0]
    assert finding_node.get("redacted") is not True, finding_node
    assert finding_node["finding_id"] == finding["finding_id"]

    region_nodes = decision_chain["region_occurrences"]
    assert len(region_nodes) == 1, region_nodes
    region_node = region_nodes[0]
    assert region_node.get("redacted") is not True, region_node
    assert region_node["region_id"] == region["region_id"]
    assert region_node["start_offset_bytes"] == _DOC_SPAN_START
    assert region_node["end_offset_bytes"] == _DOC_SPAN_END
    assert (
        region_node["span_content_digest_sha256"] == _EXPECTED_SPAN_DIGEST
    ), region_node

    # ----- 13g. Document Revision ----------------------------------
    document_nodes = decision_chain["document_revisions"]
    assert len(document_nodes) == 1, document_nodes
    document_node = document_nodes[0]
    assert document_node.get("redacted") is not True, document_node
    assert document_node["resource_id"] == document_resource_id
    assert document_node["revision_id"] == document_revision_id
    assert document_node["content_digest_sha256"] == _EXPECTED_DOC_DIGEST

    # ----- 13h. Byte-equivalent navigation back to the exact text --
    # The Region Occurrence node's ``bounded_text`` is the
    # byte-equivalent span ``content_bytes[start:end]`` of the
    # originating Document Revision (Requirement 11.2, inherited by
    # Requirement 14.2 through the navigator's delegation to
    # :meth:`navigate_decision`). The planning provenance route
    # serializes the bytes through FastAPI's :func:`jsonable_encoder`,
    # which decodes ASCII byte content to a string; the corpus seeded
    # here is pure ASCII so the round-trip equality holds without an
    # explicit base64 decode. For the symmetric base64-encoded
    # variant — required when binary content is recorded — callers
    # use the Slice 1 ``GET /api/v1/regions/.../text`` endpoint,
    # which is exercised by
    # :mod:`tests.end_to_end.test_routes_provenance_traversal`.
    assert (
        region_node["bounded_text"] == _EXPECTED_SPAN_BYTES.decode("ascii")
    ), region_node["bounded_text"]
