"""End-to-end HTTP test for the Release 1C journey (task 17.1).

This test drives the fully-composed FastAPI app — built by
:func:`walking_slice.app.create_app` — through :class:`httpx.AsyncClient`
over the ASGI transport, exercising the full Release 1C pipeline from
captured Evidence through a recorded Completion and back along the
Execution Provenance Chain to the originating Document Revision text.

Pipeline (one transaction per stage, all stages observable to the
authoring parties):

**Slice 1 — Knowledge capture (Party A)**

1. ``POST /api/v1/documents`` + region creation → captures a Source
   Document and a Region Occurrence over the span
   ``content_bytes[start:end] = b"quick brown fox"`` (Requirements 4.1
   / 11.2 from Slice 1).
2. ``POST /api/v1/findings`` citing the seeded Region Occurrence.
3. ``POST /api/v1/recommendations`` derived from the Finding.
4. ``POST /api/v1/recommendations/{rid}/decisions`` with
   ``outcome = "Accept"``.

**Slice 2 — Planning (Party A)**

5. ``POST /api/v1/objectives`` targeting the authorized Decision.
6. ``POST /api/v1/intended-outcomes`` targeting the Objective.
7. ``POST /api/v1/projects`` addressing the Objective.
8. ``POST /api/v1/deliverable-expectations`` on the Project.
9. ``POST /api/v1/activity-plans`` within the Project.
10. ``POST /api/v1/activity-plans/{ap_id}/plan-revisions`` records a
    Draft Plan Revision.
11. ``POST /api/v1/plan-revisions/{pr_id}/reviews`` records an
    ``Endorse`` review.
12. ``POST /api/v1/plan-revisions/{pr_id}/approvals`` with
    ``outcome = "Approve"`` transitions the Plan Revision to
    ``approved`` atomically (AD-WS-19).

**Slice 3 — Execution**

13. ``POST /api/v1/work-assignments`` (Party A as Assignment Authority,
    Party B as assignee) — Requirement 23.1.
14. ``POST /api/v1/work-events`` with ``event_kind = "started"``
    (Party B) — Requirement 24.1.
15. ``POST /api/v1/work-events`` with ``event_kind = "progress_note"``
    (Party B) — Requirement 24.1.
16. ``POST /api/v1/time-entries`` (Party B) — Requirement 25.1.
17. ``POST /api/v1/deliverables`` (Party B authoring) — records a
    produced Deliverable Resource + first Revision with
    ``role_marker = 'generated_output'`` — Requirement 26.1.
18. ``POST /api/v1/deliverable-productions`` (Party B) — links the
    produced Revision to the target Deliverable Expectation Revision
    — Requirement 27.1.
19. ``POST /api/v1/milestone-acceptances`` (Party A as Milestone
    Acceptance Authority) with ``outcome = "Accept"`` — Requirement
    28.1.
20. ``POST /api/v1/completions`` (Party A as Completion Authority) with
    ``outcome = "Completed"`` — Requirement 29.1.

**Navigate the Execution Provenance Chain**

21. ``GET /api/v1/completions/{completion_id}/provenance`` returns the
    full :class:`ExecutionProvenanceTree` with the Completion head,
    the Milestone Acceptance leg (Acceptance → Production → produced
    Revision), the Work Assignment leg (Work Assignment → Work Events,
    Time Entries), and the Plan Approval delegation envelope —
    Requirement 31.1 / 35.1.
22. ``GET /api/v1/plan-approvals/{plan_approval_id}/provenance``
    follows the Plan Approval delegation envelope back through the
    Slice 2 Planning chain and the Slice 1 Decision chain to the
    Region Occurrence and Document Revision (Requirement 35.1 — the
    Planning leg is delegated to ``navigate_plan_approval``). The
    Region Occurrence node's bounded text is asserted byte-equivalent
    to the originally captured Document content slice.

Authorities are granted via two Role Assignments at wildcard scope:

- **Party A (the "actor")** holds every authority the pipeline needs
  on ``"*"``: ``view``, ``modify``, ``review``, ``approve``,
  ``assign``, ``accept_milestone``, and ``complete``. Party A drives
  Slices 1 / 2 end-to-end, creates the Work Assignment (Assignment
  Authority), accepts the Milestone, completes the Plan Revision,
  and walks the provenance chain at the end.
- **Party B (the "assignee")** holds ``view`` + ``contribute`` on
  ``"*"``. Party B drives every Slice 3 contributor write that
  AD-WS-29 binds to the assignee Party: Work Events, Time Entries,
  the produced Deliverable Revision authoring step, and the
  Deliverable Production record.

Validates: Requirements 23.1, 24.1, 25.1, 26.1, 27.1, 28.1, 29.1,
31.1, 35.1.
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


# Pipeline-author Party A (the "actor"): Assignment Authority,
# Milestone Acceptance Authority, Completion Authority, Slice 1 + 2
# Knowledge / Planning authority.
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-00000017c001"

# Assignee Party B: drives every Slice 3 Contributor write that AD-WS-29
# binds to the assignee — Work Events, Time Entries, produced
# Deliverable Revision authoring, Deliverable Production.
_ASSIGNEE_PARTY_ID = "00000000-0000-7000-8000-00000017c002"

# Resource-steward identity recorded as the ``assigning_authority_id``
# on the seeded Role Assignments. Identifier opacity (Requirement 1.7)
# means this value never reaches the API surface; the column just
# needs a valid Party row.
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-00000017c005"

# A single shared applicable_scope value used on every creation
# request. The two Role Assignments seeded by the ``composed_app``
# fixture are pinned to ``"*"`` so this exact string never has to
# match anything server-side, but it travels on every request body
# so the persisted columns carry a recognisable test scope.
_SCOPE = "release-1c/pilot-team"

# Authority-basis identifier used for every authority-bearing record
# (Decision, Plan Review, Plan Approval, Work Assignment, Milestone
# Acceptance, Completion). The basis is a stable constant per
# AD-WS-10 and is not the focus of this test.
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000017c0a1")

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

# Produced-Deliverable content the assignee writes during the
# execution leg. Plain ASCII (text/markdown) so the recorded
# content digest is easy to verify and the Slice 3 / Slice 1
# disjointness check (Resource Identity is in the deliverable_resource
# tag of Identifier_Registry, not source_evidence_document) survives
# the full pipeline.
_DELIVERABLE_CONTENT = (
    b"# Onboarding Playbook (Iteration 1)\n\n"
    b"This Deliverable captures the team's first-pass response to the "
    b"corpus fox finding.\n"
)
_EXPECTED_DELIVERABLE_DIGEST = hashlib.sha256(_DELIVERABLE_CONTENT).hexdigest()


# ---------------------------------------------------------------------------
# Engine + Party seeding helpers.
# ---------------------------------------------------------------------------


def _build_engine(tmp_path: Path) -> Engine:
    """Construct a per-test on-disk SQLite engine.

    Mirrors the engine-construction helper used by
    :mod:`tests.end_to_end.test_release_1b_journey`: ``journal_mode=WAL``
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
    three Parties it needs before driving the HTTP layer.
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
    """A fully-composed FastAPI app with two Parties + Role Assignments seeded.

    The clock is pinned to :data:`_FIXED_INSTANT` so every recorded
    ``recorded_at`` is byte-equivalent across runs; the JWT secret is
    pinned to a constant so any future bearer-token surface
    reproduces.

    Party A receives every authority the cumulative Slice 1 / 2 / 3
    pipeline needs *other than* ``contribute`` (so the AD-WS-29
    assignee-binding rule rejects any Contributor write Party A
    attempts and the test verifies the binding at the same time it
    drives the journey). Party B holds ``view`` + ``contribute`` so
    every assignee-bound Slice 3 write succeeds when the actor is
    Party B.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"release-1c-e2e-test-secret",
    )
    with engine.begin() as conn:
        _seed_party(conn, _AUTHOR_PARTY_ID, "Release 1C Pipeline Author")
        _seed_party(conn, _ASSIGNEE_PARTY_ID, "Release 1C Assignee")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")

    services: SliceServices = app.state.services
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="release-1c-pipeline-author",
        # ``view`` covers every backlink, provenance walk, and
        # disclosure check the navigator performs.
        # ``modify`` covers the Slice 1 ``create.<knowledge>`` and the
        # Slice 2 ``create.<planning>`` actions that map to ``modify``.
        # ``review`` covers ``create.plan_review`` (AD-WS-15).
        # ``approve`` covers ``create.plan_approval`` (AD-WS-15).
        # ``assign`` covers ``create.work_assignment`` (Slice 3
        # Requirement 32.6).
        # ``accept_milestone`` covers ``create.milestone_acceptance``
        # (Requirement 32.8).
        # ``complete`` covers ``create.completion`` (Requirement 32.9).
        authorities=(
            "view",
            "modify",
            "review",
            "approve",
            "assign",
            "accept_milestone",
            "complete",
        ),
        scope="*",
    )
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_ASSIGNEE_PARTY_ID,
        role_name="release-1c-assignee",
        # ``contribute`` is the Slice 3 authority added by AD-WS-24 and
        # mapped to every Contributor write (Work Events, Time Entries,
        # produced Deliverable Revision authoring, Deliverable
        # Production) per Requirement 32.7. ``view`` covers the
        # provenance / disclosure surface so Party B can read back
        # everything it wrote.
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
# The end-to-end pipeline test.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_1c_pipeline_end_to_end(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Drive the full Release 1C pipeline end-to-end through HTTP.

    Each step asserts the HTTP contract documented on the matching
    requirement clause (creation returns 201 with a canonical UUIDv7
    identifier and the per-Resource declarative attributes) and
    threads the returned identifiers into the next step. The final
    step navigates the Execution Provenance Chain rooted at the
    issued Completion Record and walks back through the Planning
    chain to the originating Document Revision text.
    """
    author_headers = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}
    assignee_headers = {"X-Actor-Party-Id": _ASSIGNEE_PARTY_ID}

    # =======================================================================
    # Slice 1 — Knowledge capture (Party A).
    # =======================================================================

    # ----- 1. Capture Evidence --------------------------------------
    doc_response = await client.post(
        "/api/v1/documents",
        json={
            "content_bytes": base64.b64encode(_DOC_CONTENT).decode("ascii"),
            "contributing_party_id": _AUTHOR_PARTY_ID,
            "authority": "authoritative",
        },
        headers=author_headers,
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
        headers=author_headers,
    )
    assert region_response.status_code == 201, region_response.text
    region = region_response.json()
    assert _CANONICAL_UUID7_REGEX.match(region["region_id"]), region

    # ----- 2. Create Finding ----------------------------------------
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
        headers=author_headers,
    )
    assert finding_response.status_code == 201, finding_response.text
    finding = finding_response.json()

    # ----- 3. Create Recommendation ---------------------------------
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
        headers=author_headers,
    )
    assert rec_response.status_code == 201, rec_response.text
    recommendation = rec_response.json()

    # ----- 4. Record Decision ---------------------------------------
    decision_response = await client.post(
        f"/api/v1/recommendations/{recommendation['recommendation_id']}"
        "/decisions",
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
        headers=author_headers,
    )
    assert decision_response.status_code == 201, decision_response.text
    decision = decision_response.json()
    assert decision["outcome"] == "Accept"

    # =======================================================================
    # Slice 2 — Planning (Party A).
    # =======================================================================

    # ----- 5. Create Objective --------------------------------------
    objective_response = await client.post(
        "/api/v1/objectives",
        json={
            "statement": (
                "Establish a reusable playbook anchored to the corpus "
                "fox finding within one quarter."
            ),
            "rationale": (
                "Anchors strategic intent to the authorized Slice 1 "
                "decision so downstream planning artifacts inherit the "
                "evidence chain."
            ),
            "target_decision_id": decision["decision_id"],
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert objective_response.status_code == 201, objective_response.text
    objective = objective_response.json()

    # ----- 6. Record Intended Outcome -------------------------------
    io_response = await client.post(
        "/api/v1/intended-outcomes",
        json={
            "target_objective_id": objective["objective_id"],
            "success_condition": (
                "Every new team member references the playbook within "
                "their first sprint."
            ),
            "observation_window": (
                "The first full quarter after rollout."
            ),
            "attribution_assumption": (
                "No other onboarding artifacts are introduced during "
                "the observation window."
            ),
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert io_response.status_code == 201, io_response.text
    intended_outcome = io_response.json()
    assert intended_outcome["outcome_kind"] == "intended"

    # ----- 7. Create Project ----------------------------------------
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
        headers=author_headers,
    )
    assert project_response.status_code == 201, project_response.text
    project = project_response.json()

    # ----- 8. Declare Deliverable Expectation -----------------------
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
        headers=author_headers,
    )
    assert de_response.status_code == 201, de_response.text
    deliverable_expectation = de_response.json()

    # ----- 9. Create Activity Plan ----------------------------------
    ap_response = await client.post(
        "/api/v1/activity-plans",
        json={
            "target_project_id": project["project_id"],
            "title": "Q3 Onboarding Playbook Activities",
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert ap_response.status_code == 201, ap_response.text
    activity_plan = ap_response.json()

    # ----- 10. Submit Plan Revision ---------------------------------
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
        headers=author_headers,
    )
    assert pr_response.status_code == 201, pr_response.text
    plan_revision = pr_response.json()
    assert plan_revision["lifecycle_state"] == "draft"

    # ----- 11. Record Plan Review -----------------------------------
    review_response = await client.post(
        f"/api/v1/plan-revisions/{plan_revision['plan_revision_id']}"
        "/reviews",
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
        headers=author_headers,
    )
    assert review_response.status_code == 201, review_response.text

    # ----- 12. Approve Plan Revision --------------------------------
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
        headers=author_headers,
    )
    assert approval_response.status_code == 201, approval_response.text
    plan_approval = approval_response.json()
    assert plan_approval["new_lifecycle_state"] == "approved"

    # =======================================================================
    # Slice 3 — Execution.
    # =======================================================================

    # ----- 13. Record Work Assignment (Party A — Assignment Authority)
    wa_response = await client.post(
        "/api/v1/work-assignments",
        json={
            "target_plan_revision_id": plan_revision["plan_revision_id"],
            "assignee_party_id": _ASSIGNEE_PARTY_ID,
            "assignment_rationale": (
                "Assigning the iteration-one playbook authoring effort "
                "to the named Contributor."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert wa_response.status_code == 201, wa_response.text
    work_assignment = wa_response.json()
    assert _CANONICAL_UUID7_REGEX.match(
        work_assignment["work_assignment_id"]
    ), work_assignment
    assert work_assignment["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )
    assert work_assignment["assignee_party_id"] == _ASSIGNEE_PARTY_ID
    assert work_assignment["assignment_authority_party_id"] == _AUTHOR_PARTY_ID

    # ----- 14. Record `started` Work Event (Party B) ---------------
    started_response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": work_assignment["work_assignment_id"],
            "event_kind": "started",
            "event_note": (
                "Beginning the iteration-one playbook draft."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert started_response.status_code == 201, started_response.text
    started_event = started_response.json()
    assert started_event["event_kind"] == "started"
    assert started_event["recording_party_id"] == _ASSIGNEE_PARTY_ID

    # ----- 15. Record `progress_note` Work Event (Party B) ---------
    progress_response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": work_assignment["work_assignment_id"],
            "event_kind": "progress_note",
            "event_note": (
                "Outline complete; drafting the corpus-fox case study."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert progress_response.status_code == 201, progress_response.text
    progress_event = progress_response.json()
    assert progress_event["event_kind"] == "progress_note"

    # ----- 16. Record Time Entry (Party B) -------------------------
    time_entry_response = await client.post(
        "/api/v1/time-entries",
        json={
            "target_work_assignment_id": work_assignment["work_assignment_id"],
            "effort_hours": "3.50",
            "effort_period_start": "2026-05-01T09:00:00Z",
            "effort_period_end": "2026-05-01T12:30:00Z",
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert time_entry_response.status_code == 201, time_entry_response.text
    time_entry = time_entry_response.json()
    assert time_entry["effort_hours"] == "3.50"
    assert time_entry["recording_party_id"] == _ASSIGNEE_PARTY_ID

    # ----- 17. Record produced Deliverable Revision (Party B) ------
    deliverable_response = await client.post(
        "/api/v1/deliverables",
        json={
            "content_bytes": base64.b64encode(_DELIVERABLE_CONTENT).decode(
                "ascii"
            ),
            "content_type": "text/markdown",
            "produced_deliverable_name": (
                "Onboarding Playbook — Iteration 1 Draft"
            ),
            "originating_work_assignment_id": (
                work_assignment["work_assignment_id"]
            ),
        },
        headers=assignee_headers,
    )
    assert deliverable_response.status_code == 201, deliverable_response.text
    deliverable = deliverable_response.json()
    assert _CANONICAL_UUID7_REGEX.match(deliverable["deliverable_id"]), (
        deliverable
    )
    assert _CANONICAL_UUID7_REGEX.match(
        deliverable["deliverable_revision_id"]
    ), deliverable
    assert deliverable["role_marker"] == "generated_output"
    assert deliverable["content_digest_sha256"] == _EXPECTED_DELIVERABLE_DIGEST
    assert deliverable["originating_work_assignment_id"] == (
        work_assignment["work_assignment_id"]
    )
    assert deliverable["authoring_party_id"] == _ASSIGNEE_PARTY_ID

    # ----- 18. Record Deliverable Production (Party B) -------------
    production_response = await client.post(
        "/api/v1/deliverable-productions",
        json={
            "source_work_assignment_id": work_assignment["work_assignment_id"],
            "produced_deliverable_revision_id": (
                deliverable["deliverable_revision_id"]
            ),
            "target_deliverable_expectation_revision_id": (
                deliverable_expectation[
                    "deliverable_expectation_revision_id"
                ]
            ),
            "production_rationale": (
                "Iteration-one draft fulfills the Document expectation "
                "for the playbook."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert production_response.status_code == 201, production_response.text
    production = production_response.json()
    assert production["source_work_assignment_id"] == (
        work_assignment["work_assignment_id"]
    )
    assert production["produced_deliverable_revision_id"] == (
        deliverable["deliverable_revision_id"]
    )
    assert production["target_deliverable_expectation_revision_id"] == (
        deliverable_expectation["deliverable_expectation_revision_id"]
    )

    # ----- 19. Record Milestone Acceptance (Party A) ---------------
    ma_response = await client.post(
        "/api/v1/milestone-acceptances",
        json={
            "source_deliverable_production_id": (
                production["deliverable_production_id"]
            ),
            "outcome": "Accept",
            "rationale": (
                "Iteration-one playbook draft meets the Document "
                "expectation and is accepted as a Milestone."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert ma_response.status_code == 201, ma_response.text
    milestone_acceptance = ma_response.json()
    assert milestone_acceptance["outcome"] == "Accept"
    assert milestone_acceptance["accepting_party_id"] == _AUTHOR_PARTY_ID
    assert milestone_acceptance["source_deliverable_production_id"] == (
        production["deliverable_production_id"]
    )

    # ----- 20. Record Completion (Party A) -------------------------
    completion_response = await client.post(
        "/api/v1/completions",
        json={
            "target_plan_revision_id": plan_revision["plan_revision_id"],
            "outcome": "Completed",
            "rationale": (
                "Iteration-one is complete with one accepted Milestone."
            ),
            "source_milestone_acceptance_ids": [
                milestone_acceptance["milestone_acceptance_id"],
            ],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert completion_response.status_code == 201, completion_response.text
    completion = completion_response.json()
    assert _CANONICAL_UUID7_REGEX.match(completion["completion_id"]), (
        completion
    )
    assert completion["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )
    assert completion["target_activity_plan_id"] == (
        activity_plan["activity_plan_id"]
    )
    assert completion["target_project_id"] == project["project_id"]
    assert completion["outcome"] == "Completed"
    assert completion["completing_party_id"] == _AUTHOR_PARTY_ID
    assert completion["source_milestone_acceptance_ids"] == [
        milestone_acceptance["milestone_acceptance_id"],
    ]

    # =======================================================================
    # 21. Navigate Execution Provenance Chain (Slice 3 head).
    # =======================================================================

    chain_response = await client.get(
        f"/api/v1/completions/{completion['completion_id']}/provenance",
        headers=author_headers,
    )
    assert chain_response.status_code == 200, chain_response.text
    chain = chain_response.json()

    # ----- 21a. Completion head node -------------------------------
    completion_node = chain["completion"]
    assert completion_node["completion_id"] == completion["completion_id"]
    assert completion_node["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )
    assert completion_node["target_activity_plan_id"] == (
        activity_plan["activity_plan_id"]
    )
    assert completion_node["target_project_id"] == project["project_id"]
    assert completion_node["outcome"] == "Completed"

    # ----- 21b. Plan Approval delegation envelope ------------------
    plan_approval_envelope = chain["plan_approval_chain"]
    assert plan_approval_envelope is not None, chain
    assert plan_approval_envelope["plan_approval_id"] == (
        plan_approval["plan_approval_id"]
    )
    assert plan_approval_envelope["target_plan_revision_id"] == (
        plan_revision["plan_revision_id"]
    )
    assert plan_approval_envelope["target_activity_plan_id"] == (
        activity_plan["activity_plan_id"]
    )

    # ----- 21c. Milestone Acceptance → Production → Revision leg ---
    ma_chains = chain["milestone_acceptance_chains"]
    assert len(ma_chains) == 1, ma_chains
    ma_chain = ma_chains[0]

    ma_node = ma_chain["milestone_acceptance"]
    assert ma_node.get("redacted") is not True, ma_node
    assert ma_node["milestone_acceptance_id"] == (
        milestone_acceptance["milestone_acceptance_id"]
    )
    assert ma_node["outcome"] == "Accept"

    production_node = ma_chain["deliverable_production"]
    assert production_node is not None, ma_chain
    assert production_node.get("redacted") is not True, production_node
    assert production_node["deliverable_production_id"] == (
        production["deliverable_production_id"]
    )
    assert production_node["source_work_assignment_id"] == (
        work_assignment["work_assignment_id"]
    )

    revision_node = ma_chain["produced_deliverable_revision"]
    assert revision_node is not None, ma_chain
    assert revision_node.get("redacted") is not True, revision_node
    assert revision_node["deliverable_id"] == deliverable["deliverable_id"]
    assert revision_node["deliverable_revision_id"] == (
        deliverable["deliverable_revision_id"]
    )
    # Requirement 35.8: produced Deliverable Revision node carries the
    # role marker and content digest so the chain is self-describing.
    assert revision_node["role_marker"] == "generated_output"
    assert revision_node["content_digest_sha256"] == (
        _EXPECTED_DELIVERABLE_DIGEST
    )
    assert revision_node["originating_work_assignment_id"] == (
        work_assignment["work_assignment_id"]
    )

    # ----- 21d. Work Assignment leg (Work Assignment → Events / Time)
    wa_chains = chain["work_assignment_chains"]
    assert len(wa_chains) == 1, wa_chains
    wa_chain = wa_chains[0]

    wa_node = wa_chain["work_assignment"]
    assert wa_node.get("redacted") is not True, wa_node
    assert wa_node["work_assignment_id"] == (
        work_assignment["work_assignment_id"]
    )
    assert wa_node["assignee_party_id"] == _ASSIGNEE_PARTY_ID
    assert wa_node["assignment_authority_party_id"] == _AUTHOR_PARTY_ID

    work_events = wa_chain["work_events"]
    assert len(work_events) == 2, work_events
    # Ordered by ``(recorded_at ASC, work_event_id ASC)``; the fixed
    # clock collapses ``recorded_at`` so the secondary sort by
    # ``work_event_id`` (UUIDv7 → lexicographically increasing by mint
    # order) determines the order. We assert by set membership so the
    # test does not depend on the secondary-sort key.
    event_kinds = {ev["event_kind"] for ev in work_events}
    assert event_kinds == {"started", "progress_note"}
    event_ids = {ev["work_event_id"] for ev in work_events}
    assert event_ids == {
        started_event["work_event_id"],
        progress_event["work_event_id"],
    }

    time_entries = wa_chain["time_entries"]
    assert len(time_entries) == 1, time_entries
    time_entry_node = time_entries[0]
    assert time_entry_node.get("redacted") is not True, time_entry_node
    assert time_entry_node["time_entry_id"] == time_entry["time_entry_id"]
    assert time_entry_node["effort_hours"] == "3.50"

    # ----- 21e. No gap descriptors on a fully-visible chain --------
    assert chain["gap_descriptors"] == [], chain["gap_descriptors"]

    # =======================================================================
    # 22. Navigate the delegated Slice 2 Plan Approval chain back to
    #     the originating Document Revision text.
    # =======================================================================

    planning_response = await client.get(
        f"/api/v1/plan-approvals/{plan_approval['plan_approval_id']}"
        "/provenance",
        headers=author_headers,
    )
    assert planning_response.status_code == 200, planning_response.text
    planning_chain = planning_response.json()

    # The Slice 2 Plan Approval chain re-exposes the head node and the
    # full Slice 1 Decision chain via the ``decision_chain`` envelope.
    pa_node = planning_chain["plan_approval"]
    assert pa_node["plan_approval_id"] == plan_approval["plan_approval_id"]
    assert pa_node["outcome"] == "Approve"

    pr_node = planning_chain["plan_revision"]
    assert pr_node["plan_revision_id"] == plan_revision["plan_revision_id"]
    assert pr_node["lifecycle_state"] == "approved"

    ap_node = planning_chain["activity_plan"]
    assert ap_node["activity_plan_id"] == activity_plan["activity_plan_id"]
    assert ap_node["target_project_id"] == project["project_id"]

    project_node = planning_chain["project_revision"]
    assert project_node["project_id"] == project["project_id"]
    assert project_node["target_objective_id"] == objective["objective_id"]

    objective_node = planning_chain["objective_revision"]
    assert objective_node["objective_id"] == objective["objective_id"]
    assert objective_node["target_decision_id"] == decision["decision_id"]

    decision_chain = planning_chain["decision_chain"]
    assert decision_chain is not None, planning_chain
    assert decision_chain["decision"]["decision_id"] == (
        decision["decision_id"]
    )
    assert decision_chain["decision"]["outcome"] == "Accept"

    # Region Occurrence → Document Revision tail.
    region_nodes = decision_chain["region_occurrences"]
    assert len(region_nodes) == 1, region_nodes
    region_node_body = region_nodes[0]
    assert region_node_body["region_id"] == region["region_id"]
    assert region_node_body["start_offset_bytes"] == _DOC_SPAN_START
    assert region_node_body["end_offset_bytes"] == _DOC_SPAN_END
    assert region_node_body["span_content_digest_sha256"] == (
        _EXPECTED_SPAN_DIGEST
    )

    document_nodes = decision_chain["document_revisions"]
    assert len(document_nodes) == 1, document_nodes
    document_node = document_nodes[0]
    assert document_node["resource_id"] == document_resource_id
    assert document_node["revision_id"] == document_revision_id
    assert document_node["content_digest_sha256"] == _EXPECTED_DOC_DIGEST

    # ----- 22a. Byte-equivalent navigation back to the exact text --
    # The Region Occurrence node's ``bounded_text`` is the
    # byte-equivalent span ``content_bytes[start:end]`` of the
    # originating Document Revision (Requirement 11.2, inherited by
    # Requirement 14.2 / 35.1 through the navigator's delegation
    # chain). The planning provenance route serializes the bytes
    # through FastAPI's :func:`jsonable_encoder`, which decodes ASCII
    # byte content to a string; the corpus seeded here is pure ASCII
    # so the round-trip equality holds without an explicit base64
    # decode (the symmetric base64-encoded byte-content variant is
    # exercised by the Region text endpoint covered in
    # :mod:`tests.end_to_end.test_routes_provenance_traversal`).
    assert (
        region_node_body["bounded_text"]
        == _EXPECTED_SPAN_BYTES.decode("ascii")
    ), region_node_body["bounded_text"]
