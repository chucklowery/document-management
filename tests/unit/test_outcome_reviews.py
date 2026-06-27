"""Unit tests for :class:`OutcomeReviewService` (fourth-walking-slice task 9.2).

Pins the contract established by task 9.1, design
§"Outcome_Service.OutcomeReviews", and Requirements 49.3, 49.4, 49.9, 52.9,
54.1, 54.2:

- **49.4 (enumerations)** — the review-outcome is drawn from
  ``{Achieved, Partially_Achieved, Not_Achieved, Inconclusive}``; the
  attribution stance from ``{Asserted, Partial, Unattributed, Contradicted}``;
  the confidence indicator from ``{High, Moderate, Low}``. An out-of-enumeration
  value for any of the three is rejected with nothing persisted.
- **49.4 (attribution evidence)** — an ``Asserted`` or ``Contradicted`` stance
  requires a non-empty attribution-evidence reference; an empty reference is
  rejected. The other two stances accept an empty reference.
- **49.4 (citations)** — at least one cited Success-Condition Assessment and at
  least one cited Completion Record are required; an empty list for either is
  rejected. A cited Assessment whose ``Addresses`` target differs from the named
  target is rejected. An unresolvable cited Completion Record or produced
  Deliverable Revision is rejected.
- **49.3 (duplicate)** — at most one Outcome Review per target Intended Outcome
  Revision; a second attempt is rejected and the first Record is left
  byte-equivalent.
- **49.9 / 54.1** — no Outcome Review is created as a side effect of a Slice 3
  Completion finalization.
- **52.9** — ``create.outcome_review`` requires ``issue_outcome_review``
  (AD-WS-33); a Party without it is denied, no Record is created, and exactly
  one Denial Record is appended in a separate transaction (AD-WS-9).
- **49.7** — the persisted Outcome Review Record is immutable (UPDATE / DELETE
  rejected by the schema triggers).
- **AD-WS-35** — one ``Cites`` Relationship per cited Assessment
  (``semantic_role = 'review_assessment'``), per cited Completion Record
  (``semantic_role = 'review_completion'``), and per cited produced Deliverable
  Revision (``semantic_role = 'review_deliverable'``).

The tests mirror the fixture / seed-helper style of
``tests/unit/test_outcome_success_condition_assessments.py`` (task 8.2's tests)
for the Slice 4 reviewable chain and
``tests/unit/test_execution_completions.py`` (task 11.2's tests) for the Slice 3
Completion happy-path graph. The authorization deny path is exercised by
reviewing as a Party without the ``issue_outcome_review`` role rather than by
swapping in a stub, so the real evaluation code path participates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.models import CreateOutcomeReviewResult
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import (
    OutcomeReviewAuthorizationError,
    OutcomeReviewCitationError,
    OutcomeReviewConflictError,
    OutcomeReviewService,
    OutcomeReviewValidationError,
)
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_OWNER_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID = "00000000-0000-7000-8000-000000a00005"
_REVIEWER_PARTY_ID = "00000000-0000-7000-8000-000000a00006"
# A seeded Party holding no role grant, used for the authorization deny path.
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-000000a00007"
_COMPLETING_PARTY_ID = "00000000-0000-7000-8000-000000a00008"
_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-000000a00009"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_AUTHORITY_BASIS_ID = UUID("00000000-0000-7000-8000-0000000ba001")
_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

# A syntactically valid identifier never minted into the schema.
_UNRESOLVABLE_ID = "00000000-0000-7000-8000-0000000fffff"

_ACTION = "create.outcome_review"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_UNIT = "percent"
_WINDOW_2025 = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW = datetime(2025, 6, 1, tzinfo=timezone.utc)

_RATIONALE = "Reviewed evidence and concluded the outcome was achieved."

# Directly-seeded citable Slice 3 artifacts.
_CITABLE_COMPLETION_ID = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000d00004"

# Full Slice 3 Completion happy-path graph identifiers (no-side-effect test).
_PROJECT_ID = "00000000-0000-7000-8000-000000c01010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c01020"
_APPROVED_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c01030"
_WA_ID = "00000000-0000-7000-8000-000000d01001"
_DELIV_ID = "00000000-0000-7000-8000-000000e01001"
_DELIV_REVISION_ID = "00000000-0000-7000-8000-000000e01002"
_DELIV_EXPECTATION_ID = "00000000-0000-7000-8000-000000f01001"
_DELIV_EXPECTATION_REVISION_ID = "00000000-0000-7000-8000-000000f01002"
_DELIV_PRODUCTION_ID = "00000000-0000-7000-8000-0000000d010a1"
_ACCEPT_ACCEPTANCE_ID = "00000000-0000-7000-8000-0000000d010b1"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1, Slice 2, Slice 3, and Slice 4 schemas.

    The Outcome Review Service crosses all four: Slice 1/2 for the Intended
    Outcome and authorization plumbing, Slice 4 for the Outcome/Assessment
    chain, and Slice 3 (Execution + Deliverable_Repository) for the cited
    Completion Records and produced Deliverable Revisions.
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    create_outcome_schema(engine)
    return engine


@pytest.fixture
def intended_outcome_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> IntendedOutcomeService:
    return IntendedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


@pytest.fixture
def measurement_definition_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> MeasurementDefinitionService:
    return MeasurementDefinitionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
    )


@pytest.fixture
def measurement_record_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    measurement_definition_service: MeasurementDefinitionService,
) -> MeasurementRecordService:
    return MeasurementRecordService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        definition_reader=measurement_definition_service,
    )


@pytest.fixture
def observed_outcome_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_record_service: MeasurementRecordService,
    measurement_definition_service: MeasurementDefinitionService,
) -> ObservedOutcomeService:
    return ObservedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        measurement_reader=measurement_record_service,
        definition_reader=measurement_definition_service,
    )


@pytest.fixture
def assessment_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    observed_outcome_service: ObservedOutcomeService,
) -> SuccessConditionAssessmentService:
    return SuccessConditionAssessmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        observed_outcome_reader=observed_outcome_service,
    )


@pytest.fixture
def completion_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> CompletionService:
    """Slice 3 :class:`CompletionService`.

    Used both as the ``completion_reader`` of the Outcome Review Service (only
    its ``get_completion`` static read is consulted there) and to drive a real
    Completion finalization in the no-side-effect test.
    """
    return CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=PlanRevisionService(
            clock=None,  # type: ignore[arg-type]
            identity_service=None,  # type: ignore[arg-type]
            audit_log=None,  # type: ignore[arg-type]
            authorization_service=None,  # type: ignore[arg-type]
        ),
        project_resolver=ProjectResolver(),
        denial_audit_sleep=lambda _seconds: None,
    )


@pytest.fixture
def deliverable_repository_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> DeliverableRepositoryService:
    """Slice 3 :class:`DeliverableRepositoryService` used as the
    ``deliverable_reader`` (only its ``get_revision`` static read is
    consulted)."""
    return DeliverableRepositoryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )


@pytest.fixture
def review_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    completion_service: CompletionService,
    deliverable_repository_service: DeliverableRepositoryService,
) -> OutcomeReviewService:
    """:class:`OutcomeReviewService` wired with real readers and a real
    :class:`AuthorizationService`. The deny path is exercised by reviewing as a
    Party without ``issue_outcome_review`` rather than by swapping in a stub."""
    return OutcomeReviewService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        assessment_reader=assessment_service,
        completion_reader=completion_service,
        deliverable_reader=deliverable_repository_service,
        denial_audit_sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_party(conn, _OWNER_PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
        _seed_party(conn, _DEFINER_PARTY_ID, "Measurement Definer")
        _seed_party(conn, _RECORDER_PARTY_ID, "Measurement Recorder")
        _seed_party(conn, _ASSESSOR_PARTY_ID, "Outcome Assessor")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")


def _seed_objective(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id,
                    parent_revision_id, statement, rationale,
                    target_decision_id, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :oid, NULL,
                    'Adopt service-mesh telemetry.',
                    'Anchored on the accepted decision.',
                    :did, :pid, :scope, :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REV_ID,
                "oid": _OBJECTIVE_ID,
                "did": _DECISION_ID,
                "pid": _OWNER_PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    role_name: str,
    authority: str,
    scope: str = _SCOPE,
) -> str:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=(authority,),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_world(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    grant_issue_outcome_review: bool = True,
) -> None:
    """Seed parties, the Objective, and the role grants.

    Always grants the chain roles needed to seed targets, Measurement Records,
    Observed Outcomes, and Assessments (``modify``, ``define_measurement``,
    ``record_measurement``, ``assess_outcome``) plus the Slice 3 ``complete``
    authority. Grants ``issue_outcome_review`` to the Reviewer unless the deny
    path is being exercised.
    """
    _seed_required_parties(engine)
    _seed_objective(engine)
    _assign_role(
        authorization_service,
        engine,
        party_id=_OWNER_PARTY_ID,
        role_name="intended_outcome_owner",
        authority="modify",
    )
    _assign_role(
        authorization_service,
        engine,
        party_id=_DEFINER_PARTY_ID,
        role_name="measurement_definer",
        authority="define_measurement",
    )
    _assign_role(
        authorization_service,
        engine,
        party_id=_RECORDER_PARTY_ID,
        role_name="measurement_recorder",
        authority="record_measurement",
    )
    _assign_role(
        authorization_service,
        engine,
        party_id=_ASSESSOR_PARTY_ID,
        role_name="outcome_assessor",
        authority="assess_outcome",
    )
    _assign_role(
        authorization_service,
        engine,
        party_id=_COMPLETING_PARTY_ID,
        role_name="completion_authority",
        authority="complete",
    )
    if grant_issue_outcome_review:
        _assign_role(
            authorization_service,
            engine,
            party_id=_REVIEWER_PARTY_ID,
            role_name="outcome_reviewer",
            authority="issue_outcome_review",
        )


@dataclass(frozen=True)
class _Reviewable:
    """A fully-seeded reviewable target: an Intended Outcome Revision plus a
    Success-Condition Assessment Record that addresses it."""

    intended_outcome_revision_id: str
    intended_outcome_id: str
    assessment_id: str


def _make_reviewable(
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    engine: Engine,
) -> _Reviewable:
    """Seed one Intended Outcome, its Measurement Definition + Record, an
    Observed Outcome Revision, and a Success-Condition Assessment Record that
    addresses the Intended Outcome.

    Each call mints a fresh Intended Outcome so callers may create several
    independent reviewables (used by the addressing-mismatch test).
    """
    with engine.begin() as conn:
        intended = intended_outcome_service.create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition="Onboarding completes in under two days.",
            observation_window="30 days post launch",
            attribution_assumption="Sampling rate held constant.",
            authoring_party_id=_OWNER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        definition = (
            measurement_definition_service.create_measurement_definition(
                conn,
                target_intended_outcome_revision_id=(
                    intended.intended_outcome_revision_id
                ),
                measurand_description="Adoption rate of the new workflow.",
                unit_of_measure=_UNIT,
                observation_window=_WINDOW_2025,
                cadence="monthly",
                data_source="product analytics",
                authoring_party_id=_DEFINER_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=engine,
            )
        )
    with engine.begin() as conn:
        record = measurement_record_service.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=(
                definition.measurement_definition_revision_id
            ),
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=_OBS_IN_WINDOW,
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        observed = observed_outcome_service.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=(
                intended.intended_outcome_revision_id
            ),
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=[record.measurement_record_id],
            authoring_party_id=_ASSESSOR_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        assessment = assessment_service.create_assessment(
            conn,
            target_intended_outcome_revision_id=(
                intended.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                observed.observed_outcome_revision_id
            ),
            assessment_category="Satisfied",
            assessment_rationale="Measured adoption met the success threshold.",
            assessing_party_id=_ASSESSOR_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return _Reviewable(
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        intended_outcome_id=intended.intended_outcome_id,
        assessment_id=assessment.assessment_id,
    )


def _seed_citable_completion(engine: Engine) -> str:
    """Seed one resolvable Completion Record by direct INSERT.

    The Outcome Review Service resolves cited Completion Records via the
    read-only ``CompletionService.get_completion`` (AD-WS-40); a directly
    inserted row is sufficient for that resolution. Only ``completing_party_id``
    carries a foreign key (to ``Parties``); the remaining columns are free text.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id, target_plan_revision_id,
                    target_activity_plan_id, target_project_id,
                    outcome, rationale, source_milestone_acceptance_ids_json,
                    completing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :cid, :prev, :aid, :proj, 'Completed',
                    'Phase 1 completed.', '[]', :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": _CITABLE_COMPLETION_ID,
                "prev": "00000000-0000-7000-8000-0000000c0fff",
                "aid": "00000000-0000-7000-8000-0000000a0fff",
                "proj": "00000000-0000-7000-8000-0000000b0fff",
                "party": _COMPLETING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
    return _CITABLE_COMPLETION_ID


def _seed_citable_deliverable_revision(engine: Engine) -> str:
    """Seed one resolvable produced Deliverable Revision by direct INSERT.

    Requires a Work Assignment Record (the Revision's
    ``originating_work_assignment_id`` FK target) and a Deliverable Resource.
    The Outcome Review Service resolves cited produced Deliverable Revisions via
    the read-only ``DeliverableRepositoryService.get_revision`` (AD-WS-40).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Assignment_Records (
                    work_assignment_id, target_plan_revision_id,
                    assignee_party_id, assignment_authority_party_id,
                    assignment_rationale, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :wid, :prev, :assignee, :authority,
                    'Assigning the rollout.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _CITABLE_WORK_ASSIGNMENT_ID,
                "prev": "00000000-0000-7000-8000-0000000c0ffe",
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNING_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Mesh runbook', :ts)
                """
            ),
            {"did": _CITABLE_DELIVERABLE_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes, content_digest_sha256,
                    role_marker, originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :did, 'text/markdown', :bytes, :digest,
                    'generated_output', :wa, :party, :ts
                )
                """
            ),
            {
                "rev": _CITABLE_DELIVERABLE_REVISION_ID,
                "did": _CITABLE_DELIVERABLE_ID,
                "bytes": b"produced",
                "digest": "a" * 64,
                "wa": _CITABLE_WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )
    return _CITABLE_DELIVERABLE_REVISION_ID


# ---------------------------------------------------------------------------
# Full Slice 3 Completion happy-path graph (no-side-effect test).
#
# Mirrors the proven seed helpers in tests/unit/test_execution_completions.py
# so a real Completion finalization can be driven through
# CompletionService.create_completion.
# ---------------------------------------------------------------------------


def _seed_completion_graph(engine: Engine) -> None:
    """Seed the Project / Activity Plan / approved Plan Revision / Work
    Assignment / Deliverable / Production / accepted Milestone graph that a
    permitted ``create_completion`` call rolls up.

    Parties and the ``complete`` role grant are seeded by :func:`_seed_world`.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Mesh Rollout Activities',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNING_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :aid, NULL, 'approved', 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ASSIGNING_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Work_Assignment_Records (
                    work_assignment_id, target_plan_revision_id,
                    assignee_party_id, assignment_authority_party_id,
                    assignment_rationale, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :wid, :prev, :assignee, :authority,
                    'Assigning the rollout.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _WA_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNING_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Phase 1 runbook', :ts)
                """
            ),
            {"did": _DELIV_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes, content_digest_sha256,
                    role_marker, originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :did, 'text/markdown', :bytes, :digest,
                    'generated_output', :wa, :party, :ts
                )
                """
            ),
            {
                "rev": _DELIV_REVISION_ID,
                "did": _DELIV_ID,
                "bytes": b"produced",
                "digest": "b" * 64,
                "wa": _WA_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _DELIV_EXPECTATION_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectation_Revisions (
                    deliverable_expectation_revision_id,
                    deliverable_expectation_id, parent_revision_id,
                    target_project_id, name, description,
                    deliverable_kind, acceptance_criteria,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :did, NULL, :pid, 'Mesh Operations Runbook',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIV_EXPECTATION_REVISION_ID,
                "did": _DELIV_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNING_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Production_Records (
                    deliverable_production_id, source_work_assignment_id,
                    produced_deliverable_id, produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    production_rationale, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :pid, :wa, :did, :rev, :exp_did, :exp_rev,
                    'Produced runbook for milestone one.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": _DELIV_PRODUCTION_ID,
                "wa": _WA_ID,
                "did": _DELIV_ID,
                "rev": _DELIV_REVISION_ID,
                "exp_did": _DELIV_EXPECTATION_ID,
                "exp_rev": _DELIV_EXPECTATION_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Milestone_Acceptance_Records (
                    milestone_acceptance_id,
                    source_deliverable_production_id,
                    produced_deliverable_id,
                    produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    outcome, rationale, accepting_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :mid, :pid, :did, :rev, :exp_did, :exp_rev,
                    'Accept', 'Milestone one criteria satisfied.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": _ACCEPT_ACCEPTANCE_ID,
                "pid": _DELIV_PRODUCTION_ID,
                "did": _DELIV_ID,
                "rev": _DELIV_REVISION_ID,
                "exp_did": _DELIV_EXPECTATION_ID,
                "exp_rev": _DELIV_EXPECTATION_REVISION_ID,
                "party": _COMPLETING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


# ---------------------------------------------------------------------------
# Row counters / snapshots.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_relationships(
    engine: Engine, *, rel_type: str, source_id: str
) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = :rt AND source_id = :sid"
                ),
                {"rt": rel_type, "sid": source_id},
            ).scalar_one()
        )


def _count_denial_audit_rows(engine: Engine) -> int:
    """Count Denial Records for ``create.outcome_review``.

    A Denial Record is distinguished from the authorization evaluation row
    (which also carries ``outcome='deny'``) by ``authorities_required`` being
    NULL.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE outcome = 'deny' AND action_type = :a "
                    "AND authorities_required IS NULL"
                ),
                {"a": _ACTION},
            ).scalar_one()
        )


def _count_consequential_audit_rows(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE outcome = 'consequential' AND action_type = :a"
                ),
                {"a": _ACTION},
            ).scalar_one()
        )


def _review_row(engine: Engine, outcome_review_id: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT * FROM Outcome_Review_Records "
                    "WHERE outcome_review_id = :id"
                ),
                {"id": outcome_review_id},
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------------------
# Duck-typed authority basis whose ``type`` is outside the AD-WS-10 set.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BadBasis:
    type: str = "party-id"
    id: UUID = _AUTHORITY_BASIS_ID


# ---------------------------------------------------------------------------
# Review-creation wrapper.
# ---------------------------------------------------------------------------


def _create_review(
    review_service: OutcomeReviewService,
    engine: Engine,
    *,
    target_intended_outcome_revision_id: str,
    cited_assessment_ids,
    cited_completion_ids,
    cited_produced_deliverable_revision_ids=(),
    review_outcome: str = "Achieved",
    attribution_stance: str = "Partial",
    confidence: str = "High",
    review_rationale: str = _RATIONALE,
    attribution_evidence_reference: str = "",
    reviewing_party_id: str = _REVIEWER_PARTY_ID,
    authority_basis=_BASIS,
    applicable_scope: str = _SCOPE,
    correlation_id: Optional[str] = None,
    request_attributes=None,
) -> CreateOutcomeReviewResult:
    with engine.begin() as conn:
        return review_service.create_outcome_review(
            conn,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            review_outcome=review_outcome,  # type: ignore[arg-type]
            attribution_stance=attribution_stance,  # type: ignore[arg-type]
            confidence=confidence,  # type: ignore[arg-type]
            review_rationale=review_rationale,
            attribution_evidence_reference=attribution_evidence_reference,
            cited_assessment_ids=cited_assessment_ids,
            cited_completion_ids=cited_completion_ids,
            cited_produced_deliverable_revision_ids=(
                cited_produced_deliverable_revision_ids
            ),
            reviewing_party_id=reviewing_party_id,
            authority_basis=authority_basis,
            applicable_scope=applicable_scope,
            engine=engine,
            correlation_id=correlation_id,
            request_attributes=request_attributes,
        )


# ---------------------------------------------------------------------------
# Combined setup helper.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Setup:
    reviewable: _Reviewable
    completion_id: str
    deliverable_revision_id: str


def _full_setup(
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    engine: Engine,
    *,
    grant_issue_outcome_review: bool = True,
) -> _Setup:
    """Seed the world, one reviewable, and one citable Completion Record plus
    produced Deliverable Revision."""
    _seed_world(
        authorization_service,
        engine,
        grant_issue_outcome_review=grant_issue_outcome_review,
    )
    reviewable = _make_reviewable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        engine,
    )
    completion_id = _seed_citable_completion(engine)
    deliverable_revision_id = _seed_citable_deliverable_revision(engine)
    return _Setup(
        reviewable=reviewable,
        completion_id=completion_id,
        deliverable_revision_id=deliverable_revision_id,
    )


# ===========================================================================
# Happy-path baseline + the three Cites semantic_role markers (AD-WS-35).
# ===========================================================================


def test_create_outcome_review_persists_one_record_and_relationships(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """With ``issue_outcome_review`` authority, a resolvable ``intended``
    target, a cited Assessment that addresses it, a cited Completion Record, and
    a cited produced Deliverable Revision, the service creates one immutable
    Record, one ``Addresses`` Relationship, three ``Cites`` Relationships, and
    one consequential audit row (Requirements 49.1, 49.2, 49.6, 52.9,
    AD-WS-35)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
        cited_produced_deliverable_revision_ids=[
            setup.deliverable_revision_id
        ],
        correlation_id="corr-review",
    )

    assert isinstance(result, CreateOutcomeReviewResult)
    assert _CANONICAL_UUID7.match(result.outcome_review_id)
    assert result.correlation_id == "corr-review"
    assert _count(outcome_engine, "Outcome_Review_Records") == 1
    assert _count_consequential_audit_rows(outcome_engine) == 1
    assert _count_denial_audit_rows(outcome_engine) == 0
    assert (
        _count_relationships(
            outcome_engine,
            rel_type="Addresses",
            source_id=result.outcome_review_id,
        )
        == 1
    )
    assert (
        _count_relationships(
            outcome_engine,
            rel_type="Cites",
            source_id=result.outcome_review_id,
        )
        == 3
    )


def test_cites_semantic_role_markers_present(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """The three ``Cites`` Relationships carry the AD-WS-35 ``semantic_role``
    markers ``review_assessment``, ``review_completion``, and
    ``review_deliverable`` exactly once each; the ``Addresses`` Relationship
    carries ``semantic_role IS NULL``."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
        cited_produced_deliverable_revision_ids=[
            setup.deliverable_revision_id
        ],
    )

    with outcome_engine.connect() as conn:
        cites_roles = [
            row["semantic_role"]
            for row in conn.execute(
                text(
                    "SELECT semantic_role FROM Relationships "
                    "WHERE relationship_type = 'Cites' AND source_id = :sid"
                ),
                {"sid": result.outcome_review_id},
            ).mappings()
        ]
        addresses_role = conn.execute(
            text(
                "SELECT semantic_role FROM Relationships "
                "WHERE relationship_type = 'Addresses' AND source_id = :sid"
            ),
            {"sid": result.outcome_review_id},
        ).scalar_one()

    assert sorted(cites_roles) == [
        "review_assessment",
        "review_completion",
        "review_deliverable",
    ]
    assert addresses_role is None


# ===========================================================================
# Requirement 49.4 — review-outcome / attribution-stance / confidence
# enumeration boundaries.
# ===========================================================================


@pytest.mark.parametrize(
    "review_outcome",
    ["Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"],
)
def test_each_review_outcome_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    review_outcome: str,
) -> None:
    """Each enumerated review-outcome value is accepted (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
        review_outcome=review_outcome,
    )

    assert result.review_outcome == review_outcome
    assert _count(outcome_engine, "Outcome_Review_Records") == 1


@pytest.mark.parametrize(
    "attribution_stance", ["Partial", "Unattributed"]
)
def test_each_stance_without_evidence_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    attribution_stance: str,
) -> None:
    """The two stances that do not require evidence are accepted with an empty
    attribution-evidence reference (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
        attribution_stance=attribution_stance,
        attribution_evidence_reference="",
    )

    assert result.attribution_stance == attribution_stance
    assert _count(outcome_engine, "Outcome_Review_Records") == 1


@pytest.mark.parametrize("confidence", ["High", "Moderate", "Low"])
def test_each_confidence_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    confidence: str,
) -> None:
    """Each enumerated confidence indicator is accepted (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
        confidence=confidence,
    )

    assert result.confidence == confidence
    assert _count(outcome_engine, "Outcome_Review_Records") == 1


@pytest.mark.parametrize(
    "field,value,constraint",
    [
        ("review_outcome", "Maybe", "review_outcome_invalid"),
        ("attribution_stance", "Unknown", "attribution_stance_invalid"),
        ("confidence", "Medium", "confidence_invalid"),
    ],
)
def test_out_of_enumeration_value_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    field: str,
    value: str,
    constraint: str,
) -> None:
    """An out-of-enumeration review-outcome / attribution-stance / confidence
    value is rejected with nothing persisted (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    kwargs = {field: value}
    with pytest.raises(OutcomeReviewValidationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[setup.completion_id],
            **kwargs,
        )

    assert exc_info.value.failed_constraint == constraint
    assert _count(outcome_engine, "Outcome_Review_Records") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0


# ===========================================================================
# Requirement 49.4 — Asserted / Contradicted require non-empty evidence.
# ===========================================================================


@pytest.mark.parametrize("stance", ["Asserted", "Contradicted"])
def test_evidence_required_stance_rejects_empty_evidence(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    stance: str,
) -> None:
    """An ``Asserted`` or ``Contradicted`` stance with an empty
    attribution-evidence reference is rejected (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    with pytest.raises(OutcomeReviewValidationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[setup.completion_id],
            attribution_stance=stance,
            attribution_evidence_reference="",
        )

    assert exc_info.value.failed_constraint == (
        "attribution_evidence_reference_missing_for_stance"
    )
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


@pytest.mark.parametrize("stance", ["Asserted", "Contradicted"])
def test_evidence_required_stance_accepts_non_empty_evidence(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    stance: str,
) -> None:
    """An ``Asserted`` or ``Contradicted`` stance with a non-empty
    attribution-evidence reference is accepted (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
        attribution_stance=stance,
        attribution_evidence_reference="See measured adoption series.",
    )

    assert result.attribution_stance == stance
    assert result.attribution_evidence_reference == (
        "See measured adoption series."
    )
    assert _count(outcome_engine, "Outcome_Review_Records") == 1


# ===========================================================================
# Requirement 49.4 — at least one Assessment and one Completion cited.
# ===========================================================================


def test_zero_cited_assessments_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """An empty cited-Assessment list is rejected (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    with pytest.raises(OutcomeReviewValidationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[],
            cited_completion_ids=[setup.completion_id],
        )

    assert exc_info.value.failed_constraint == "cited_assessment_ids_empty"
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


def test_zero_cited_completions_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """An empty cited-Completion list is rejected (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    with pytest.raises(OutcomeReviewValidationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[],
        )

    assert exc_info.value.failed_constraint == "cited_completion_ids_empty"
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


# ===========================================================================
# Requirement 49.4 — cited Assessment addressing mismatch.
# ===========================================================================


def test_cited_assessment_addressing_other_target_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """A cited Assessment whose ``Addresses`` target differs from the named
    target Intended Outcome Revision is rejected (Requirement 49.4).

    Two independent reviewables are seeded; the review names target A but cites
    the Assessment that addresses target B.
    """
    _seed_world(authorization_service, outcome_engine)
    reviewable_a = _make_reviewable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )
    reviewable_b = _make_reviewable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )
    completion_id = _seed_citable_completion(outcome_engine)

    with pytest.raises(OutcomeReviewCitationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                reviewable_a.intended_outcome_revision_id
            ),
            cited_assessment_ids=[reviewable_b.assessment_id],
            cited_completion_ids=[completion_id],
        )

    assert exc_info.value.failed_constraint == (
        "cited_assessment_addresses_mismatch"
    )
    assert exc_info.value.offending_id == reviewable_b.assessment_id
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


# ===========================================================================
# Requirement 49.4 — unresolvable cited Completion / Deliverable Revision.
# ===========================================================================


def test_unresolvable_cited_completion_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """An unresolvable cited Completion Record is rejected (Requirement
    49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    with pytest.raises(OutcomeReviewCitationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[_UNRESOLVABLE_ID],
        )

    assert exc_info.value.failed_constraint == (
        "cited_completion_not_resolvable"
    )
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


def test_unresolvable_cited_deliverable_revision_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """An unresolvable cited produced Deliverable Revision is rejected
    (Requirement 49.4)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    with pytest.raises(OutcomeReviewCitationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[setup.completion_id],
            cited_produced_deliverable_revision_ids=[_UNRESOLVABLE_ID],
        )

    assert exc_info.value.failed_constraint == (
        "cited_produced_deliverable_revision_not_resolvable"
    )
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


# ===========================================================================
# Requirement 49.3 — duplicate Outcome Review rejected, first byte-equivalent.
# ===========================================================================


def test_duplicate_review_rejected_first_left_byte_equivalent(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """A second Outcome Review against the same target Intended Outcome Revision
    is rejected; the first Record remains byte-equivalent and exactly one Record
    persists (Requirement 49.3)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )

    first = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
    )
    before = _review_row(outcome_engine, first.outcome_review_id)

    with pytest.raises(OutcomeReviewConflictError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[setup.completion_id],
        )

    assert exc_info.value.target_intended_outcome_revision_id == (
        setup.reviewable.intended_outcome_revision_id
    )
    assert _count(outcome_engine, "Outcome_Review_Records") == 1
    assert _review_row(outcome_engine, first.outcome_review_id) == before


# ===========================================================================
# Requirements 49.9 / 54.1 — no Review created as a side effect of a Slice 3
# Completion finalization.
# ===========================================================================


def test_completion_finalization_creates_no_outcome_review(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    completion_service: CompletionService,
) -> None:
    """Finalizing a Slice 3 Completion Record produces no Outcome Review Record
    as a side effect (Requirements 49.9, 54.1).

    A full Completion happy-path graph is seeded and a real Completion Record is
    created through ``CompletionService.create_completion``; afterward the
    ``Outcome_Review_Records`` table is still empty — the Outcome Review Service
    is never invoked from any Slice 3 finalization path.
    """
    _seed_world(authorization_service, outcome_engine)
    _seed_completion_graph(outcome_engine)

    assert _count(outcome_engine, "Outcome_Review_Records") == 0

    with outcome_engine.begin() as conn:
        completion = completion_service.create_completion(
            conn,
            target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
            outcome="Completed",
            rationale="Phase 1 work completed; success criteria met.",
            source_milestone_acceptance_ids=(),
            completing_party_id=_COMPLETING_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=outcome_engine,
        )

    assert _CANONICAL_UUID7.match(completion.completion_id)
    assert _count(outcome_engine, "Completion_Records") == 1
    # No Outcome Review Record materialized as a side effect.
    assert _count(outcome_engine, "Outcome_Review_Records") == 0


# ===========================================================================
# Requirement 52.9 — authorization deny path.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """A Party without ``issue_outcome_review`` is denied; no Record is created
    and exactly one Denial Record is appended in a separate transaction
    (Requirements 49.5, 52.9, AD-WS-9)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
        grant_issue_outcome_review=False,
    )

    with pytest.raises(OutcomeReviewAuthorizationError) as exc_info:
        _create_review(
            review_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                setup.reviewable.intended_outcome_revision_id
            ),
            cited_assessment_ids=[setup.reviewable.assessment_id],
            cited_completion_ids=[setup.completion_id],
            reviewing_party_id=_UNAUTHORIZED_PARTY_ID,
            correlation_id="corr-deny",
        )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-deny"
    assert _count(outcome_engine, "Outcome_Review_Records") == 0
    assert _count_consequential_audit_rows(outcome_engine) == 0
    assert _count_denial_audit_rows(outcome_engine) == 1


# ===========================================================================
# Requirement 49.7 — immutability of the persisted Outcome Review Record.
# ===========================================================================


def test_persisted_outcome_review_rejects_update_and_delete(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """The Outcome Review Record written by the service is immutable: UPDATE and
    DELETE are rejected by the schema triggers (Requirement 49.7)."""
    setup = _full_setup(
        authorization_service,
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        assessment_service,
        outcome_engine,
    )
    result = _create_review(
        review_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            setup.reviewable.intended_outcome_revision_id
        ),
        cited_assessment_ids=[setup.reviewable.assessment_id],
        cited_completion_ids=[setup.completion_id],
    )
    before = _review_row(outcome_engine, result.outcome_review_id)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Outcome_Review_Records "
                    "SET review_rationale = 'tampered' "
                    "WHERE outcome_review_id = :id"
                ),
                {"id": result.outcome_review_id},
            )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Outcome_Review_Records "
                    "WHERE outcome_review_id = :id"
                ),
                {"id": result.outcome_review_id},
            )

    assert _count(outcome_engine, "Outcome_Review_Records") == 1
    assert _review_row(outcome_engine, result.outcome_review_id) == before
