"""End-to-end HTTP test for the Release 1D journey (task 16.1).

This test drives the fully-composed FastAPI app — built by
:func:`walking_slice.app.create_app` — through :class:`httpx.AsyncClient`
over the ASGI transport, exercising the full Release 1D pipeline: the
cumulative Slice 1 → Slice 2 → Slice 3 journey through a recorded
Completion Record (mirroring
:mod:`tests.end_to_end.test_release_1c_journey`), then the additive
Slice 4 Outcome_Service pipeline:

1. Define a Measurement Definition addressing the Intended Outcome
   Revision (``POST /api/v1/measurement-definitions``) — Requirement
   44.1.
2. Record a native Measurement Record citing the Measurement Definition
   Revision (``POST /api/v1/measurement-records``) — Requirement 45.1.
3. Record an imported Measurement Record carrying its source-system
   provenance (``POST /api/v1/measurement-records/imported``) —
   Requirement 46.1.
4. Record an Observed Outcome Revision citing both Measurement Records
   (``POST /api/v1/observed-outcomes``) — Requirement 47.1.
5. Record a Success-Condition Assessment sourcing the Observed Outcome
   Revision (``POST /api/v1/success-condition-assessments``) —
   Requirement 48.1.
6. Record an Outcome Review citing the Assessment and the Completion
   Record (``POST /api/v1/outcome-reviews``) — Requirement 49.1.
7. Navigate the Outcome Measurement Provenance Chain rooted at the
   Outcome Review (``GET /api/v1/outcome-reviews/{id}/provenance``) back
   to the exact originating Document Revision text (Requirement 51.1)
   and along the parallel Completion → produced Deliverable Revision
   leg.
8. Read the outcome-status Projection
   (``GET /api/v1/intended-outcomes/{rid}/outcome-status``) and assert
   the most-progressed status label (Requirement 59.1).

Authorities are granted via two Role Assignments at wildcard scope:

- **Party A (the "actor")** holds every authority the cumulative
  Slice 1 / 2 / 3 pipeline needs *other than* ``contribute`` plus the
  four additive Slice 4 write authorities (``define_measurement``,
  ``record_measurement``, ``assess_outcome``, ``issue_outcome_review``)
  and ``view``. Party A drives Slices 1 / 2 end-to-end, creates the
  Work Assignment, accepts the Milestone, completes the Plan Revision,
  records every Outcome_Service artifact, and walks the provenance chain
  at the end.
- **Party B (the "assignee")** holds ``view`` + ``contribute`` on
  ``"*"`` and drives every Slice 3 contributor write that AD-WS-29 binds
  to the assignee Party.

Validates: Requirements 44.1, 45.1, 46.1, 47.1, 48.1, 49.1, 51.1, 59.1.
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


# Pipeline-author Party A (the "actor"): Slice 1 / 2 / 3 authority plus the
# four additive Slice 4 write authorities.
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-00000017d001"

# Assignee Party B: drives the Slice 3 Contributor writes bound to the
# assignee by AD-WS-29.
_ASSIGNEE_PARTY_ID = "00000000-0000-7000-8000-00000017d002"

# Resource-steward identity recorded as the ``assigning_authority_id`` on
# the seeded Role Assignments.
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-00000017d005"

_SCOPE = "release-1d/pilot-team"

_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000017d0a1")

# Pin every ``recorded_at`` so the asserted response bodies are deterministic.
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Document content used to seed the Slice 1 leg. The span offsets target
# the substring ``"quick brown fox"`` so the byte-equivalence assertion in
# the navigation step is grep-friendly.
_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # exclusive end of "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]
_EXPECTED_DOC_DIGEST = hashlib.sha256(_DOC_CONTENT).hexdigest()
_EXPECTED_SPAN_DIGEST = hashlib.sha256(_EXPECTED_SPAN_BYTES).hexdigest()

# Produced-Deliverable content the assignee writes during the execution leg.
_DELIVERABLE_CONTENT = (
    b"# Onboarding Playbook (Iteration 1)\n\n"
    b"This Deliverable captures the team's first-pass response to the "
    b"corpus fox finding.\n"
)
_EXPECTED_DELIVERABLE_DIGEST = hashlib.sha256(_DELIVERABLE_CONTENT).hexdigest()

# Slice 4 Measurement constants. The observation window is an ISO-8601
# interval so the native Measurement Record's observation time is validated
# against it; the observation time precedes the retrieval time precedes the
# fixed ``recorded_at`` so both Records satisfy the schema time-ordering
# CHECKs (observation <= recorded; observation <= retrieval <= recorded).
_MEASURE_UNIT = "percent"
_OBSERVATION_WINDOW = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBSERVATION_TIME = "2025-06-01T00:00:00Z"
_RETRIEVAL_TIME = "2025-07-01T00:00:00Z"
_SOURCE_SYSTEM_AUTHORITY = "replica"


# ---------------------------------------------------------------------------
# Engine + Party seeding helpers.
# ---------------------------------------------------------------------------


def _build_engine(tmp_path: Path) -> Engine:
    """Construct a per-test on-disk SQLite engine with production pragmas."""
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
    """Insert one ``Parties`` row directly via SQL."""
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

    Party A receives every authority the cumulative Slice 1 / 2 / 3 pipeline
    needs *other than* ``contribute``, plus the four additive Slice 4 write
    authorities. Party B holds ``view`` + ``contribute`` so every
    assignee-bound Slice 3 write succeeds when the actor is Party B.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"release-1d-e2e-test-secret",
    )
    with engine.begin() as conn:
        _seed_party(conn, _AUTHOR_PARTY_ID, "Release 1D Pipeline Author")
        _seed_party(conn, _ASSIGNEE_PARTY_ID, "Release 1D Assignee")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")

    services: SliceServices = app.state.services
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="release-1d-pipeline-author",
        # Slice 1 / 2 / 3 authorities (view, modify, review, approve, assign,
        # accept_milestone, complete) plus the four additive Slice 4 write
        # authorities (AD-WS-33): define_measurement (create.measurement_
        # definition), record_measurement (create.measurement_record),
        # assess_outcome (create.observed_outcome + create.success_condition_
        # assessment), issue_outcome_review (create.outcome_review).
        authorities=(
            "view",
            "modify",
            "review",
            "approve",
            "assign",
            "accept_milestone",
            "complete",
            "define_measurement",
            "record_measurement",
            "assess_outcome",
            "issue_outcome_review",
        ),
        scope="*",
    )
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_ASSIGNEE_PARTY_ID,
        role_name="release-1d-assignee",
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
async def test_release_1d_pipeline_end_to_end(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Drive the full Release 1D pipeline end-to-end through HTTP."""
    author_headers = {"X-Actor-Party-Id": _AUTHOR_PARTY_ID}
    assignee_headers = {"X-Actor-Party-Id": _ASSIGNEE_PARTY_ID}

    # =======================================================================
    # Slice 1 — Knowledge capture (Party A).
    # =======================================================================

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

    # =======================================================================
    # Slice 2 — Planning (Party A).
    # =======================================================================

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
        headers=author_headers,
    )
    assert io_response.status_code == 201, io_response.text
    intended_outcome = io_response.json()
    assert intended_outcome["outcome_kind"] == "intended"
    intended_outcome_revision_id = intended_outcome["intended_outcome_revision_id"]

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

    # =======================================================================
    # Slice 3 — Execution.
    # =======================================================================

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

    started_response = await client.post(
        "/api/v1/work-events",
        json={
            "target_work_assignment_id": work_assignment["work_assignment_id"],
            "event_kind": "started",
            "event_note": "Beginning the iteration-one playbook draft.",
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=assignee_headers,
    )
    assert started_response.status_code == 201, started_response.text

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
    assert deliverable["content_digest_sha256"] == _EXPECTED_DELIVERABLE_DIGEST

    production_response = await client.post(
        "/api/v1/deliverable-productions",
        json={
            "source_work_assignment_id": work_assignment["work_assignment_id"],
            "produced_deliverable_revision_id": (
                deliverable["deliverable_revision_id"]
            ),
            "target_deliverable_expectation_revision_id": (
                deliverable_expectation["deliverable_expectation_revision_id"]
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
    completion_id = completion["completion_id"]

    # =======================================================================
    # Slice 4 — Outcome_Service (Party A).
    # =======================================================================

    # ----- 21. Define a Measurement Definition (Req. 44.1) ----------
    md_response = await client.post(
        "/api/v1/measurement-definitions",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "measurand_description": (
                "Proportion of new team members referencing the playbook "
                "within their first sprint."
            ),
            "unit_of_measure": _MEASURE_UNIT,
            "observation_window": _OBSERVATION_WINDOW,
            "cadence": "monthly",
            "data_source": "onboarding analytics dashboard",
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert md_response.status_code == 201, md_response.text
    measurement_definition = md_response.json()
    assert _CANONICAL_UUID7_REGEX.match(
        measurement_definition["measurement_definition_id"]
    ), measurement_definition
    measurement_definition_revision_id = measurement_definition[
        "measurement_definition_revision_id"
    ]
    assert (
        measurement_definition["target_intended_outcome_revision_id"]
        == intended_outcome_revision_id
    )

    # ----- 22. Record a native Measurement Record (Req. 45.1) -------
    native_response = await client.post(
        "/api/v1/measurement-records",
        json={
            "target_measurement_definition_revision_id": (
                measurement_definition_revision_id
            ),
            "observed_value": "62.5",
            "observed_value_unit": _MEASURE_UNIT,
            "observation_time": _OBSERVATION_TIME,
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert native_response.status_code == 201, native_response.text
    native_record = native_response.json()
    assert native_record["origin"] == "native"
    assert native_record["source_system_authority"] is None
    native_record_id = native_record["measurement_record_id"]

    # ----- 23. Record an imported Measurement Record (Req. 46.1) ----
    imported_response = await client.post(
        "/api/v1/measurement-records/imported",
        json={
            "target_measurement_definition_revision_id": (
                measurement_definition_revision_id
            ),
            "observed_value": "70.0",
            "observed_value_unit": _MEASURE_UNIT,
            "observation_time": _OBSERVATION_TIME,
            "source_system_id": "onboarding-warehouse",
            "source_system_record_id": "row-4471",
            "source_system_authority": _SOURCE_SYSTEM_AUTHORITY,
            "source_system_retrieval_time": _RETRIEVAL_TIME,
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert imported_response.status_code == 201, imported_response.text
    imported_record = imported_response.json()
    assert imported_record["origin"] == "imported"
    # The authority designation surfaces explicitly and is never defaulted to
    # ``authoritative`` (Requirement 46.7).
    assert imported_record["source_system_authority"] == _SOURCE_SYSTEM_AUTHORITY
    # ``import_at`` equals ``recorded_at`` for imported Records (Req. 46.2).
    assert imported_record["import_at"] == imported_record["recorded_at"]
    imported_record_id = imported_record["measurement_record_id"]

    # ----- 24. Record an Observed Outcome Revision (Req. 47.1) ------
    oo_response = await client.post(
        "/api/v1/observed-outcomes",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "assessment_summary": (
                "Observed playbook adoption is trending toward the success "
                "target across both native and imported measurements."
            ),
            "cited_measurement_record_ids": [
                native_record_id,
                imported_record_id,
            ],
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert oo_response.status_code == 201, oo_response.text
    observed_outcome = oo_response.json()
    assert observed_outcome["outcome_kind"] == "observed"
    assert set(observed_outcome["cited_measurement_record_ids"]) == {
        native_record_id,
        imported_record_id,
    }
    observed_outcome_revision_id = observed_outcome["observed_outcome_revision_id"]

    # ----- 25. Record a Success-Condition Assessment (Req. 48.1) ----
    assessment_response = await client.post(
        "/api/v1/success-condition-assessments",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "sourced_observed_outcome_revision_id": (
                observed_outcome_revision_id
            ),
            "assessment_category": "Satisfied",
            "assessment_rationale": (
                "Measured adoption exceeded the success threshold across the "
                "observation window."
            ),
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert assessment_response.status_code == 201, assessment_response.text
    assessment = assessment_response.json()
    assert assessment["assessment_category"] == "Satisfied"
    assessment_id = assessment["assessment_id"]

    # ----- 26. Record an Outcome Review (Req. 49.1) -----------------
    review_outcome_response = await client.post(
        "/api/v1/outcome-reviews",
        json={
            "target_intended_outcome_revision_id": intended_outcome_revision_id,
            "review_outcome": "Achieved",
            "attribution_stance": "Partial",
            "confidence": "High",
            "review_rationale": (
                "Reviewed the Satisfied Assessment and the completed work; "
                "the Intended Outcome is judged achieved with partial "
                "attribution to the delivered playbook."
            ),
            "cited_assessment_ids": [assessment_id],
            "cited_completion_ids": [completion_id],
            "authority_basis": {
                "type": "role-grant-id",
                "id": str(_AUTHORITY_BASIS_ID),
            },
            "applicable_scope": _SCOPE,
        },
        headers=author_headers,
    )
    assert review_outcome_response.status_code == 201, (
        review_outcome_response.text
    )
    outcome_review = review_outcome_response.json()
    assert _CANONICAL_UUID7_REGEX.match(outcome_review["outcome_review_id"]), (
        outcome_review
    )
    assert outcome_review["review_outcome"] == "Achieved"
    assert outcome_review["cited_assessment_ids"] == [assessment_id]
    assert outcome_review["cited_completion_ids"] == [completion_id]
    outcome_review_id = outcome_review["outcome_review_id"]

    # =======================================================================
    # 27. Navigate the Outcome Measurement Provenance Chain.
    # =======================================================================

    chain_response = await client.get(
        f"/api/v1/outcome-reviews/{outcome_review_id}/provenance",
        headers=author_headers,
    )
    assert chain_response.status_code == 200, chain_response.text
    tree = chain_response.json()

    # ----- 27a. Outcome Review head node ----------------------------
    assert tree["outcome_review"]["outcome_review_id"] == outcome_review_id

    # ----- 27b. Assessment → Observed Outcome → Measurement leg -----
    assessment_chains = tree["assessment_chains"]
    assert len(assessment_chains) == 1, assessment_chains
    ac = assessment_chains[0]
    assert ac["assessment"]["assessment_id"] == assessment_id
    assert (
        ac["observed_outcome_revision"]["observed_outcome_revision_id"]
        == observed_outcome_revision_id
    )
    measurement_chains = ac["measurement_chains"]
    assert len(measurement_chains) == 2, measurement_chains
    chain_record_ids = {
        mc["measurement_record"]["measurement_record_id"]
        for mc in measurement_chains
    }
    assert chain_record_ids == {native_record_id, imported_record_id}
    # Each Measurement Record node carries the origin indicator (Req. 55.8).
    chain_origins = {
        mc["measurement_record"]["origin"] for mc in measurement_chains
    }
    assert chain_origins == {"native", "imported"}
    for mc in measurement_chains:
        assert (
            mc["measurement_definition_revision"][
                "measurement_definition_revision_id"
            ]
            == measurement_definition_revision_id
        )

    # ----- 27c. Intended Outcome → Slice 1 Decision tail ------------
    assert (
        tree["intended_outcome_revision"]["intended_outcome_revision_id"]
        == intended_outcome_revision_id
    )
    decision_chain = tree["decision_chain"]
    assert decision_chain is not None, tree
    assert decision_chain["decision"]["decision_id"] == decision["decision_id"]

    # ----- 27d. Back to the exact originating Document Revision text -
    region_nodes = decision_chain["region_occurrences"]
    assert len(region_nodes) == 1, region_nodes
    region_node = region_nodes[0]
    assert region_node["region_id"] == region["region_id"]
    assert region_node["start_offset_bytes"] == _DOC_SPAN_START
    assert region_node["end_offset_bytes"] == _DOC_SPAN_END
    assert region_node["span_content_digest_sha256"] == _EXPECTED_SPAN_DIGEST
    # ``bounded_text`` is serialized as base64 ASCII (the navigator node
    # carries the raw span bytes); decoding it returns the byte-equivalent
    # span of the originating Document Revision (Requirement 51.1 / 55.2).
    assert (
        base64.b64decode(region_node["bounded_text"]) == _EXPECTED_SPAN_BYTES
    ), region_node["bounded_text"]

    document_nodes = decision_chain["document_revisions"]
    assert len(document_nodes) == 1, document_nodes
    document_node = document_nodes[0]
    assert document_node["resource_id"] == document_resource_id
    assert document_node["revision_id"] == document_revision_id
    assert document_node["content_digest_sha256"] == _EXPECTED_DOC_DIGEST

    # ----- 27e. Parallel leg → produced Deliverable Revision --------
    completion_chains = tree["completion_chains"]
    assert len(completion_chains) == 1, completion_chains
    completion_chain = completion_chains[0]
    assert completion_chain["completion_id"] == completion_id
    execution_tree = completion_chain["execution_tree"]
    assert execution_tree is not None, completion_chain
    assert execution_tree["completion"]["completion_id"] == completion_id
    ma_chains = execution_tree["milestone_acceptance_chains"]
    assert len(ma_chains) == 1, ma_chains
    produced_revision_node = ma_chains[0]["produced_deliverable_revision"]
    assert produced_revision_node is not None, ma_chains[0]
    assert (
        produced_revision_node["deliverable_revision_id"]
        == deliverable["deliverable_revision_id"]
    )
    assert produced_revision_node["role_marker"] == "generated_output"
    assert (
        produced_revision_node["content_digest_sha256"]
        == _EXPECTED_DELIVERABLE_DIGEST
    )

    # =======================================================================
    # 28. Read the outcome-status Projection (Req. 59.1).
    # =======================================================================

    status_response = await client.get(
        f"/api/v1/intended-outcomes/{intended_outcome_revision_id}"
        "/outcome-status",
        headers=author_headers,
    )
    assert status_response.status_code == 200, status_response.text
    outcome_status = status_response.json()
    assert (
        outcome_status["intended_outcome_revision_id"]
        == intended_outcome_revision_id
    )
    # An Outcome Review has been recorded, so the most-progressed status label
    # is "Intended Outcome reviewed" (design §"Outcome-status Projection").
    assert outcome_status["projected_status"] == "Intended Outcome reviewed"
    assert outcome_status["envelope"] is not None, outcome_status
