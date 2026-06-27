"""Unit tests for the Slice 4 Provenance traversals + backlink extension (task 10.3).

Pins the contract established by tasks 10.1 / 10.2, design
§"Provenance_Navigator (extended)", and Requirements 51.3, 51.4, 55.2,
55.4, 55.5, 55.8, 56.1, 56.2:

- **51.1 / 51.2 — both ordered chains resolve.** ``navigate_outcome_review``
  returns the Outcome Measurement Provenance Chain rooted at the Outcome
  Review: the Assessment leg (Outcome Review → Success-Condition Assessment →
  Observed Outcome Revision → Measurement Record(s) → Measurement Definition
  Revision → Intended Outcome Revision) and the parallel Completion leg
  (Outcome Review → cited Completion Record → delegated Execution chain) plus
  the directly-cited produced Deliverable Revisions. With full view authority
  every node identity resolves (no redaction markers in the visible chain).
- **55.3 / 58.2 — restricted nodes redact.** A node the requesting Party may
  not view appears as a :class:`RedactedNode` ``{kind, redacted: True}``
  marker; a redacted Measurement Record never leaks its source-system
  attributes nor its downstream Measurement Definition Revision (cascade by
  parent restriction).
- **51.3 / 55.4 — gap descriptors.** Unresolved Omission Entries on an
  enumerated-subject manifest surface a :class:`ChainGapDescriptor` carrying
  only ``stage``, ``category``, and (when visible) the next reachable node
  identity. ``Provenance_Manifests.subject_kind`` does not yet enumerate the
  Slice 4 node kinds, so the Outcome Review's own ``gap_descriptors`` tuple is
  empty; the canonical descriptor shape is exercised through the reused
  collection helper the Slice 4 traversal delegates to.
- **51.4 / 55.5 — idempotent retrieval.** Five repeated invocations for the
  same ``(outcome_review_id, party_id, at)`` return byte-equivalent trees.
- **55.8 — Measurement Record origin + authority.** Every Measurement Record
  node carries the ``origin`` indicator; a visible imported Record additionally
  surfaces its source-system identifier and authority designation.
- **56.1 / 56.2 — backlink coverage + semantic_role.** The existing backlink
  algorithm returns inbound Relationships when the queried endpoint is a Slice
  4 node, with the source-endpoint Type discriminator preserved and identical
  attribute values regardless of which endpoint is queried. The persisted
  ``Relationships.semantic_role`` discriminator written by the Slice 4 services
  is verified directly against the row (``BacklinkEntry`` itself does not
  surface ``semantic_role`` — the slice's API contract is the Relationship Type
  plus the source endpoint Type per Requirement 8.2).

The fixtures and seed helpers mirror ``tests/unit/test_outcome_reviews.py``
(task 9.2) for the Slice 4 reviewable chain and
``tests/unit/test_provenance_navigate_completion.py`` (task 12.3 tests) for the
navigator and disclosure-policy plumbing. Building the chain through the real
Slice 4 services exercises the same ``Relationships`` rows the traversal walks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    get_policy,
    seed as seed_disclosure_policies,
)
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome._provenance import (
    AssessmentObservationChain,
    IntendedOutcomeRevisionNode,
    MeasurementChain,
    MeasurementDefinitionRevisionNode,
    MeasurementRecordNode,
    ObservedOutcomeRevisionNode,
    OutcomeNodeUnresolvableError,
    OutcomeProvenanceTree,
    OutcomeReviewNode,
    OutcomeReviewUnresolvableError,
    SuccessConditionAssessmentNode,
    _collect_outcome_gap_descriptors,
    register_outcome_navigation,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import OutcomeReviewService
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.plan_revisions import PlanRevisionService
from walking_slice.provenance import (
    BacklinkPage,
    ChainGapDescriptor,
    DeliverableRevisionNode,
    ProvenanceNavigator,
    RedactedNode,
)


pytestmark = pytest.mark.unit

# The traversals are attached to ProvenanceNavigator at import time; this call
# is a defensive no-op that documents the dependency (it is idempotent).
register_outcome_navigation()


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_OWNER_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID = "00000000-0000-7000-8000-000000a00005"
_REVIEWER_PARTY_ID = "00000000-0000-7000-8000-000000a00006"
_COMPLETING_PARTY_ID = "00000000-0000-7000-8000-000000a00007"
_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-000000a00008"
# A Party holding wildcard ``view`` authority — the requesting navigator.
_VIEWER_PARTY_ID = "00000000-0000-7000-8000-000000a00009"
# A Party holding ``view`` only on the main scope (used for the restricted /
# redaction-marker test).
_SCOPED_VIEWER_PARTY_ID = "00000000-0000-7000-8000-000000a0000a"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_RECOMMENDATION_ID = "00000000-0000-7000-8000-000000c00004"
_RECOMMENDATION_REV_ID = "00000000-0000-7000-8000-000000c00005"

_SCOPE = "pilot/team-a"
_SCOPE_OTHER = "pilot/team-b"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)

_AUTHORITY_BASIS_ID = UUID("00000000-0000-7000-8000-0000000ba001")
_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

_UNRESOLVABLE_ID = "00000000-0000-7000-8000-0000000fffff"

_UNIT = "percent"
_WINDOW_2025 = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
_RETRIEVAL_TIME = datetime(2025, 7, 1, tzinfo=timezone.utc)
_SOURCE_SYSTEM_AUTHORITY = "replica"

# Directly-seeded citable Slice 3 artifacts (resolved through the read-only
# get_completion / get_revision APIs — a directly-inserted row suffices).
_CITABLE_COMPLETION_ID = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000d00004"

# The seven Slice 4 node kinds that appear as Relationship *source* endpoints
# (task 10.2 backlink coverage surface).
_SLICE4_SOURCE_KINDS = frozenset(
    {
        "measurement_definition",
        "measurement_definition_revision",
        "measurement_record",
        "observed_outcome",
        "observed_outcome_revision",
        "success_condition_assessment_record",
        "outcome_review_record",
    }
)

# Expected ``Relationships.semantic_role`` keyed by
# (source_kind, relationship_type, target_kind) — design §"Relationships rows
# written by Slice 4" / AD-WS-35.
_EXPECTED_SEMANTIC_ROLE = {
    ("measurement_definition_revision", "Addresses", "intended_outcome_revision"): None,
    ("measurement_record", "Cites", "measurement_definition_revision"): "measurement_basis",
    ("observed_outcome_revision", "Addresses", "intended_outcome_revision"): None,
    ("observed_outcome_revision", "Cites", "measurement_record"): "observation_basis",
    ("success_condition_assessment_record", "Addresses", "intended_outcome_revision"): None,
    ("success_condition_assessment_record", "Cites", "observed_outcome_revision"): "assessment_basis",
    ("outcome_review_record", "Addresses", "intended_outcome_revision"): None,
    ("outcome_review_record", "Cites", "success_condition_assessment_record"): "review_assessment",
    ("outcome_review_record", "Cites", "completion_record"): "review_completion",
    ("outcome_review_record", "Cites", "deliverable_revision"): "review_deliverable",
}


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1, Slice 2, Slice 3, and Slice 4 schemas."""
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


@pytest.fixture
def navigator(
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """Navigator without a disclosure policy (the common case)."""
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )


@pytest.fixture
def disclosure_navigator(
    outcome_engine: Engine,
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """Navigator wired with the seeded ``slice-default-2026`` policy."""
    seed_disclosure_policies(outcome_engine)
    policy = get_policy(outcome_engine, SLICE_DEFAULT_POLICY_ID)
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
        disclosure_policy=policy,
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
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
        _seed_party(conn, _VIEWER_PARTY_ID, "Wildcard Viewer")
        _seed_party(conn, _SCOPED_VIEWER_PARTY_ID, "Scoped Viewer")


def _seed_objective_and_decision(engine: Engine) -> None:
    """Seed the Objective, its Revision, and the Slice 1 Decision tail head.

    The Objective Revision's ``target_decision_id`` points at a seeded Decision
    Immutable Record so the delegated ``navigate_decision`` resolves the
    Decision node at the tail of the chain. The Decision's Recommendation is
    intentionally left unseeded — ``navigate_decision`` preserves chain shape
    with a redaction marker for the missing Recommendation, which is sufficient
    to confirm the Slice 1 tail is reached.
    """
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
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :oid, NULL, 'Adopt service-mesh telemetry.',
                    'Anchored on the accepted decision.', :did, :pid,
                    :scope, :ts
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
        conn.execute(
            text(
                """
                INSERT INTO Decisions (
                    decision_id, target_recommendation_id,
                    target_recommendation_revision_id, outcome, rationale,
                    deciding_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :did, :rid, :rrev, 'Accept',
                    'Adopt the recommended telemetry platform.', :pid,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "did": _DECISION_ID,
                "rid": _RECOMMENDATION_ID,
                "rrev": _RECOMMENDATION_REV_ID,
                "pid": _OWNER_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
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
    scope: str,
) -> None:
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
        authorization_service.assign_role(conn, request)


def _seed_world(authorization_service: AuthorizationService, engine: Engine) -> None:
    """Seed parties, the Objective + Decision tail, and the write-side roles.

    The chain-building authorities are granted on the wildcard scope ``*`` so a
    single grant covers every scope a node may carry (the redaction test places
    one node in a second scope).
    """
    _seed_required_parties(engine)
    _seed_objective_and_decision(engine)
    _assign_role(
        authorization_service, engine,
        party_id=_OWNER_PARTY_ID, role_name="intended_outcome_owner",
        authority="modify", scope="*",
    )
    _assign_role(
        authorization_service, engine,
        party_id=_DEFINER_PARTY_ID, role_name="measurement_definer",
        authority="define_measurement", scope="*",
    )
    _assign_role(
        authorization_service, engine,
        party_id=_RECORDER_PARTY_ID, role_name="measurement_recorder",
        authority="record_measurement", scope="*",
    )
    _assign_role(
        authorization_service, engine,
        party_id=_ASSESSOR_PARTY_ID, role_name="outcome_assessor",
        authority="assess_outcome", scope="*",
    )
    _assign_role(
        authorization_service, engine,
        party_id=_REVIEWER_PARTY_ID, role_name="outcome_reviewer",
        authority="issue_outcome_review", scope="*",
    )


def _seed_citable_completion(engine: Engine) -> str:
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
                    deliverable_revision_id, deliverable_id, content_type,
                    content_bytes, content_digest_sha256, role_marker,
                    originating_work_assignment_id, authoring_party_id,
                    recorded_at
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
# Chain builder.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Chain:
    intended_outcome_id: str
    intended_outcome_revision_id: str
    measurement_definition_id: str
    measurement_definition_revision_id: str
    native_record_id: str
    imported_record_id: str
    observed_outcome_id: str
    observed_outcome_revision_id: str
    assessment_id: str
    outcome_review_id: str
    completion_id: str
    deliverable_revision_id: str


def _build_chain(
    *,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
    engine: Engine,
    native_record_scope: str = _SCOPE,
) -> _Chain:
    """Build a full Slice 4 chain through the real services.

    Intended Outcome → Measurement Definition → native + imported Measurement
    Records → Observed Outcome (citing both Records) → Success-Condition
    Assessment → Outcome Review (citing the Assessment, a Completion Record,
    and a produced Deliverable Revision).
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
        definition = measurement_definition_service.create_measurement_definition(
            conn,
            target_intended_outcome_revision_id=intended.intended_outcome_revision_id,
            measurand_description="Adoption rate of the new workflow.",
            unit_of_measure=_UNIT,
            observation_window=_WINDOW_2025,
            cadence="monthly",
            data_source="product analytics",
            authoring_party_id=_DEFINER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        native = measurement_record_service.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=(
                definition.measurement_definition_revision_id
            ),
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=_OBS_IN_WINDOW,
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=native_record_scope,
            engine=engine,
        )
    with engine.begin() as conn:
        imported = measurement_record_service.create_imported_measurement(
            conn,
            target_measurement_definition_revision_id=(
                definition.measurement_definition_revision_id
            ),
            observed_value=Decimal("18.0"),
            observed_value_unit=_UNIT,
            observation_time=_OBS_IN_WINDOW,
            source_system_id="metrics-warehouse",
            source_system_record_id="row-991",
            source_system_authority=_SOURCE_SYSTEM_AUTHORITY,
            source_system_retrieval_time=_RETRIEVAL_TIME,
            importing_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        observed = observed_outcome_service.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=intended.intended_outcome_revision_id,
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=[
                native.measurement_record_id,
                imported.measurement_record_id,
            ],
            authoring_party_id=_ASSESSOR_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        assessment = assessment_service.create_assessment(
            conn,
            target_intended_outcome_revision_id=intended.intended_outcome_revision_id,
            sourced_observed_outcome_revision_id=observed.observed_outcome_revision_id,
            assessment_category="Satisfied",
            assessment_rationale="Measured adoption met the success threshold.",
            assessing_party_id=_ASSESSOR_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    completion_id = _seed_citable_completion(engine)
    deliverable_revision_id = _seed_citable_deliverable_revision(engine)
    with engine.begin() as conn:
        review = review_service.create_outcome_review(
            conn,
            target_intended_outcome_revision_id=intended.intended_outcome_revision_id,
            review_outcome="Achieved",
            attribution_stance="Partial",
            confidence="High",
            review_rationale="Reviewed evidence and concluded success.",
            attribution_evidence_reference="",
            cited_assessment_ids=[assessment.assessment_id],
            cited_completion_ids=[completion_id],
            cited_produced_deliverable_revision_ids=[deliverable_revision_id],
            reviewing_party_id=_REVIEWER_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            correlation_id=None,
            request_attributes=None,
        )
    return _Chain(
        intended_outcome_id=intended.intended_outcome_id,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_definition_id=definition.measurement_definition_id,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
        native_record_id=native.measurement_record_id,
        imported_record_id=imported.measurement_record_id,
        observed_outcome_id=observed.observed_outcome_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        assessment_id=assessment.assessment_id,
        outcome_review_id=review.outcome_review_id,
        completion_id=completion_id,
        deliverable_revision_id=deliverable_revision_id,
    )


def _grant_view(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str,
) -> None:
    _assign_role(
        authorization_service, engine,
        party_id=party_id, role_name="viewer", authority="view", scope=scope,
    )


# ---------------------------------------------------------------------------
# navigate_outcome_review — both ordered chains resolve (Requirements 51.1/51.2).
# ---------------------------------------------------------------------------


def test_navigate_outcome_review_returns_both_chains_resolving(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """With wildcard view authority every node identity resolves.

    The Assessment leg walks Assessment → Observed Outcome Revision →
    Measurement Record(s) → Measurement Definition Revision; the Completion leg
    surfaces the cited Completion's delegated Execution chain; the cited
    produced Deliverable Revision and the Intended Outcome → Decision tail also
    resolve.
    """
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    _grant_view(authorization_service, outcome_engine, party_id=_VIEWER_PARTY_ID, scope="*")

    with outcome_engine.connect() as conn:
        tree = navigator.navigate_outcome_review(
            conn,
            outcome_review_id=chain.outcome_review_id,
            party_id=_VIEWER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert isinstance(tree, OutcomeProvenanceTree)
    # Head.
    assert isinstance(tree.outcome_review, OutcomeReviewNode)
    assert tree.outcome_review.outcome_review_id == chain.outcome_review_id

    # Assessment leg.
    assert len(tree.assessment_chains) == 1
    ac = tree.assessment_chains[0]
    assert isinstance(ac, AssessmentObservationChain)
    assert isinstance(ac.assessment, SuccessConditionAssessmentNode)
    assert ac.assessment.assessment_id == chain.assessment_id
    assert isinstance(ac.observed_outcome_revision, ObservedOutcomeRevisionNode)
    assert (
        ac.observed_outcome_revision.observed_outcome_revision_id
        == chain.observed_outcome_revision_id
    )
    # Both cited Measurement Records resolve, each with its Definition Revision.
    assert len(ac.measurement_chains) == 2
    record_ids = set()
    for mc in ac.measurement_chains:
        assert isinstance(mc, MeasurementChain)
        assert isinstance(mc.measurement_record, MeasurementRecordNode)
        record_ids.add(mc.measurement_record.measurement_record_id)
        assert isinstance(
            mc.measurement_definition_revision, MeasurementDefinitionRevisionNode
        )
        assert (
            mc.measurement_definition_revision.measurement_definition_revision_id
            == chain.measurement_definition_revision_id
        )
    assert record_ids == {chain.native_record_id, chain.imported_record_id}

    # Completion leg.
    assert len(tree.completion_chains) == 1
    assert tree.completion_chains[0].completion_id == chain.completion_id
    assert tree.completion_chains[0].execution_tree is not None

    # Directly-cited produced Deliverable Revision.
    assert len(tree.cited_deliverable_revisions) == 1
    assert isinstance(tree.cited_deliverable_revisions[0], DeliverableRevisionNode)
    assert (
        tree.cited_deliverable_revisions[0].deliverable_revision_id
        == chain.deliverable_revision_id
    )

    # Intended Outcome → Decision tail.
    assert isinstance(tree.intended_outcome_revision, IntendedOutcomeRevisionNode)
    assert (
        tree.intended_outcome_revision.intended_outcome_revision_id
        == chain.intended_outcome_revision_id
    )
    assert tree.decision_chain is not None
    assert tree.decision_chain.decision.decision_id == _DECISION_ID


# ---------------------------------------------------------------------------
# Restricted nodes redact (Requirements 55.3 / 58.2).
# ---------------------------------------------------------------------------


def test_restricted_measurement_record_appears_as_redaction_marker(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """A Measurement Record the Party may not view is a ``{kind, redacted}`` marker.

    The native Record is placed in a second scope the requesting Party cannot
    view; it surfaces as a :class:`RedactedNode` and its downstream Measurement
    Definition Revision is withheld (cascade by parent restriction). The
    imported Record, in the viewable scope, resolves normally.
    """
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
        native_record_scope=_SCOPE_OTHER,
    )
    # The scoped viewer can see everything in _SCOPE but not _SCOPE_OTHER.
    _grant_view(
        authorization_service, outcome_engine,
        party_id=_SCOPED_VIEWER_PARTY_ID, scope=_SCOPE,
    )

    with outcome_engine.connect() as conn:
        tree = navigator.navigate_outcome_review(
            conn,
            outcome_review_id=chain.outcome_review_id,
            party_id=_SCOPED_VIEWER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    ac = tree.assessment_chains[0]
    by_kind = {
        ("redacted" if isinstance(mc.measurement_record, RedactedNode) else "visible"): mc
        for mc in ac.measurement_chains
    }
    assert "redacted" in by_kind and "visible" in by_kind

    redacted_chain = by_kind["redacted"]
    assert isinstance(redacted_chain.measurement_record, RedactedNode)
    assert redacted_chain.measurement_record.kind == "measurement_record"
    assert redacted_chain.measurement_record.redacted is True
    # Cascade: the redacted Record withholds its Definition Revision link.
    assert redacted_chain.measurement_definition_revision is None

    visible_chain = by_kind["visible"]
    assert isinstance(visible_chain.measurement_record, MeasurementRecordNode)
    assert (
        visible_chain.measurement_record.measurement_record_id
        == chain.imported_record_id
    )


def test_navigate_outcome_review_head_unresolvable_without_view_authority(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """A Party lacking view authority on the head gets the unresolvable form.

    The restricted and the genuinely-unresolvable cases raise the same
    exception so the response form is indistinguishable (AD-WS-9).
    """
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    # _VIEWER_PARTY_ID has no view grant in this test.
    with outcome_engine.connect() as conn:
        with pytest.raises(OutcomeReviewUnresolvableError):
            navigator.navigate_outcome_review(
                conn,
                outcome_review_id=chain.outcome_review_id,
                party_id=_VIEWER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )
        # A never-minted identifier raises the identical exception type.
        with pytest.raises(OutcomeReviewUnresolvableError):
            navigator.navigate_outcome_review(
                conn,
                outcome_review_id=_UNRESOLVABLE_ID,
                party_id=_VIEWER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )


# ---------------------------------------------------------------------------
# Measurement Record origin indicator + imported authority (Requirement 55.8).
# ---------------------------------------------------------------------------


def test_measurement_record_nodes_carry_origin_and_imported_authority(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """Native Record carries ``origin='native'`` with NULL source-system fields;
    the visible imported Record surfaces ``origin='imported'`` plus its
    source-system identifier and authority designation."""
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    _grant_view(authorization_service, outcome_engine, party_id=_VIEWER_PARTY_ID, scope="*")

    with outcome_engine.connect() as conn:
        tree = navigator.navigate_outcome_review(
            conn,
            outcome_review_id=chain.outcome_review_id,
            party_id=_VIEWER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    records = {
        mc.measurement_record.measurement_record_id: mc.measurement_record
        for mc in tree.assessment_chains[0].measurement_chains
    }
    native = records[chain.native_record_id]
    imported = records[chain.imported_record_id]

    assert native.origin == "native"
    assert native.source_system_id is None
    assert native.source_system_authority is None

    assert imported.origin == "imported"
    assert imported.source_system_id == "metrics-warehouse"
    assert imported.source_system_authority == _SOURCE_SYSTEM_AUTHORITY


# ---------------------------------------------------------------------------
# Idempotent retrieval across 5 repetitions (Requirements 51.4 / 55.5).
# ---------------------------------------------------------------------------


def test_idempotent_retrieval_across_five_repetitions(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """Five invocations for the same (id, party, at) return byte-equivalent trees."""
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    _grant_view(authorization_service, outcome_engine, party_id=_VIEWER_PARTY_ID, scope="*")

    trees = []
    for _ in range(5):
        with outcome_engine.connect() as conn:
            trees.append(
                navigator.navigate_outcome_review(
                    conn,
                    outcome_review_id=chain.outcome_review_id,
                    party_id=_VIEWER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
            )
    first = trees[0]
    for other in trees[1:]:
        assert other == first


# ---------------------------------------------------------------------------
# Short-form traversal (navigate_outcome_node, Requirement 55.1).
# ---------------------------------------------------------------------------


def test_navigate_outcome_node_short_form_resolves_from_assessment(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """A short-form traversal rooted at an Assessment reaches the lower chain."""
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    _grant_view(authorization_service, outcome_engine, party_id=_VIEWER_PARTY_ID, scope="*")

    with outcome_engine.connect() as conn:
        tree = navigator.navigate_outcome_node(
            conn,
            node_kind="success_condition_assessment_record",
            node_id=chain.assessment_id,
            party_id=_VIEWER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert tree.outcome_review is None
    assert tree.requested_node_kind == "success_condition_assessment_record"
    assert tree.requested_node_id == chain.assessment_id
    assert len(tree.assessment_chains) == 1
    assert isinstance(
        tree.assessment_chains[0].assessment, SuccessConditionAssessmentNode
    )
    assert isinstance(tree.intended_outcome_revision, IntendedOutcomeRevisionNode)


def test_navigate_outcome_node_rejects_unrecognized_kind(
    navigator: ProvenanceNavigator,
    outcome_engine: Engine,
) -> None:
    """An unrecognized short-form node kind raises the unresolvable error."""
    with outcome_engine.connect() as conn:
        with pytest.raises(OutcomeNodeUnresolvableError):
            navigator.navigate_outcome_node(
                conn,
                node_kind="outcome_review_record",
                node_id=_UNRESOLVABLE_ID,
                party_id=_VIEWER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )


# ---------------------------------------------------------------------------
# Gap descriptors (Requirements 51.3 / 55.4).
# ---------------------------------------------------------------------------


def test_outcome_review_gap_descriptors_empty_without_enumerated_subject_manifest(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    disclosure_navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """The Outcome Review's own gap-descriptor tuple is empty.

    ``Provenance_Manifests.subject_kind`` does not yet enumerate
    ``outcome_review_record`` so no Outcome-Review-subject manifest can exist;
    the collection helper still runs and returns an empty tuple (mirroring the
    Slice 3 ``navigate_completion`` convention).
    """
    _seed_world(authorization_service, outcome_engine)
    chain = _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    _grant_view(authorization_service, outcome_engine, party_id=_VIEWER_PARTY_ID, scope="*")

    with outcome_engine.connect() as conn:
        tree = disclosure_navigator.navigate_outcome_review(
            conn,
            outcome_review_id=chain.outcome_review_id,
            party_id=_VIEWER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert tree.gap_descriptors == ()


def test_gap_descriptor_shape_carries_stage_category_next_reachable(
    outcome_engine: Engine,
    disclosure_navigator: ProvenanceNavigator,
) -> None:
    """The collection helper the Slice 4 traversal delegates to yields a
    correctly-shaped :class:`ChainGapDescriptor`.

    Exercised through an enumerated subject (``plan_approval``) because the
    schema CHECK does not yet admit a Slice 4 subject. The descriptor discloses
    only ``stage``, ``category``, and the next reachable node identity.
    """
    _seed_required_parties(outcome_engine)
    manifest_id = "00000000-0000-7000-8000-0000000a00a2"
    omission_id = "00000000-0000-7000-8000-0000000a00a3"
    subject_id = "00000000-0000-7000-8000-0000000a00a4"
    with outcome_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Provenance_Manifests (
                    manifest_id, subject_kind, subject_id, subject_revision_id,
                    authoring_party_id, recorded_at, included_sources_json,
                    is_complete
                ) VALUES (
                    :mid, 'plan_approval', :sid, NULL, :party, :ts, '[]', 0
                )
                """
            ),
            {"mid": manifest_id, "sid": subject_id, "party": _OWNER_PARTY_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Omission_Entries (
                    omission_entry_id, manifest_id, excluded_source_id,
                    excluded_source_revision_id, category, rationale,
                    authoring_party_id, recorded_at, resolved_at
                ) VALUES (
                    :oid, :mid, :sid, NULL, 'unresolved',
                    'Awaiting upstream Decision.', :party, :ts, NULL
                )
                """
            ),
            {
                "oid": omission_id,
                "mid": manifest_id,
                "sid": "00000000-0000-7000-8000-0000000a00ff",
                "party": _OWNER_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )

    with outcome_engine.connect() as conn:
        descriptors = _collect_outcome_gap_descriptors(
            disclosure_navigator,
            conn,
            subject_kind="plan_approval",
            subject_id=subject_id,
            subject_revision_id=None,
            next_reachable_node_identity=subject_id,
        )

    assert len(descriptors) == 1
    descriptor = descriptors[0]
    assert isinstance(descriptor, ChainGapDescriptor)
    assert descriptor.stage == "plan_approval"
    assert descriptor.category == "unresolved"
    assert descriptor.next_reachable_node_identity == subject_id


# ---------------------------------------------------------------------------
# Backlink coverage + semantic_role for Slice 4 node kinds (Req 56.1 / 56.2).
# ---------------------------------------------------------------------------


def _slice4_source_relationship_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, source_kind,
                           source_id, source_revision_id, target_kind,
                           target_id, target_revision_id, authoring_party_id,
                           recorded_at, semantic_role
                      FROM Relationships
                    """
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows if r["source_kind"] in _SLICE4_SOURCE_KINDS]


def test_backlinks_for_slice4_node_kinds_round_trip(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    navigator: ProvenanceNavigator,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """Backlink queries return Slice 4 inbound Relationships with identical
    attribute values from both directions.

    For every Relationship whose source endpoint is a Slice 4 node, querying
    the target endpoint returns a :class:`BacklinkEntry` whose attributes equal
    the values persisted on the ``Relationships`` row — the backlink view of
    the edge is identical to the forward (stored) view.
    """
    _seed_world(authorization_service, outcome_engine)
    _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )
    _grant_view(authorization_service, outcome_engine, party_id=_VIEWER_PARTY_ID, scope="*")

    rows = _slice4_source_relationship_rows(outcome_engine)
    # Every Slice 4 source endpoint kind that participates in the chain is
    # present (definition, record, observed, assessment, review).
    present_kinds = {r["source_kind"] for r in rows}
    assert {
        "measurement_definition_revision",
        "measurement_record",
        "observed_outcome_revision",
        "success_condition_assessment_record",
        "outcome_review_record",
    } <= present_kinds

    for row in rows:
        with outcome_engine.connect() as conn:
            page = navigator.list_backlinks(
                conn,
                target_id=row["target_id"],
                target_revision_id=row["target_revision_id"],
                party_id=_VIEWER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )
        assert isinstance(page, BacklinkPage)
        match = [e for e in page.entries if e.relationship_id == row["relationship_id"]]
        assert len(match) == 1, (
            f"backlink for {row['source_kind']} relationship "
            f"{row['relationship_id']} not returned for target {row['target_id']}"
        )
        entry = match[0]
        # Identical attribute values from both directions.
        assert entry.relationship_type == row["relationship_type"]
        assert entry.source_kind == row["source_kind"]
        assert entry.source_id == row["source_id"]
        assert entry.source_revision_id == row["source_revision_id"]
        assert entry.authoring_party_id == row["authoring_party_id"]
        assert entry.recorded_at == row["recorded_at"]


def test_semantic_role_populated_for_slice4_relationships(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    review_service: OutcomeReviewService,
) -> None:
    """Persisted ``Relationships.semantic_role`` matches AD-WS-35 for every
    Slice 4 source endpoint (``Cites`` rows carry the basis/role discriminator;
    ``Addresses`` rows carry NULL)."""
    _seed_world(authorization_service, outcome_engine)
    _build_chain(
        intended_outcome_service=intended_outcome_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        assessment_service=assessment_service,
        review_service=review_service,
        engine=outcome_engine,
    )

    rows = _slice4_source_relationship_rows(outcome_engine)
    seen_keys = set()
    for row in rows:
        key = (row["source_kind"], row["relationship_type"], row["target_kind"])
        assert key in _EXPECTED_SEMANTIC_ROLE, f"unexpected Slice 4 edge {key}"
        assert row["semantic_role"] == _EXPECTED_SEMANTIC_ROLE[key], (
            f"semantic_role mismatch for {key}: "
            f"{row['semantic_role']!r} != {_EXPECTED_SEMANTIC_ROLE[key]!r}"
        )
        seen_keys.add(key)

    # Every AD-WS-35 Slice 4 edge shape is exercised by the built chain.
    assert seen_keys == set(_EXPECTED_SEMANTIC_ROLE.keys())
