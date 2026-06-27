# Feature: fourth-walking-slice, Property 53: Outcome Measurement Provenance Chain end-to-end
"""Property 53 — Outcome Measurement Provenance Chain end-to-end (task 15.8).

**Property 53: Outcome Measurement Provenance Chain end-to-end**

*For all* Outcome Review Records whose entire Outcome Measurement
Provenance Chain is visible to a requesting Party, traversal from the
Review via :meth:`ProvenanceNavigator.navigate_outcome_review` yields the
ordered Assessment leg ``Outcome Review → Success-Condition Assessment(s) →
Observed Outcome Revision → Measurement Record(s) → Measurement Definition
Revision → Intended Outcome Revision → Objective → Slice 1 Decision →
Recommendation Revision → Finding Revision(s) → Content Region
Occurrence(s) → Document Revision`` and the parallel Completion leg
``Outcome Review → Cites Completion Record(s) → Slice 3 Execution Provenance
Chain → produced Deliverable Revision(s)``. Every node identity resolves;
the returned Content Region Occurrence span fields are byte-equivalent to
the recorded bytes and digest-match the recorded content digest;
Measurement Record nodes carry the origin indicator (and, for imported
Records visible to the Party, the source-system identifier and authority
designation); the chains are byte-equivalent across at least five repeated
invocations (idempotent retrieval); restricted nodes appear as
``{kind, redacted: true}`` markers; and unresolved/stale/unavailable nodes
return gap descriptors carrying ``stage``, ``category`` ``∈ {unavailable,
restricted, stale, unresolved}``, and (when applicable) the next reachable
node identity.

**Validates: Requirements 51.1, 51.2, 51.3, 51.4, 55.1, 55.2, 55.4, 55.5,
55.8, 61.8**

Strategy
========

Two Hypothesis-driven property tests, each running ``max_examples=100``
generated cases:

- :func:`test_outcome_provenance_chain_resolves_end_to_end` builds a full
  Slice 1–4 pipeline fully visible to a wildcard-``view`` requester and
  asserts both ordered chains return, every identity resolves, the
  delegated Slice 1 Decision tail's Content Region Occurrence span
  digest-matches the recorded content digest (recomputed independently
  from the scenario bytes), each Measurement Record node carries its
  ``origin`` indicator with the imported Record additionally surfacing its
  source-system identifier and authority designation, the returned tree is
  byte-equivalent across five repeated invocations, and an unresolved
  Omission Entry seeded on the Decision subject surfaces a gap descriptor
  carrying only ``stage``, ``category``, and the next reachable node
  identity (the Slice 4 traversal delegates unresolved-link detection to
  this same collection helper).
- :func:`test_restricted_measurement_record_redacts` places the native
  Measurement Record under a scope the requesting Party cannot view and
  asserts it surfaces as a :class:`RedactedNode` ``{kind, redacted: True}``
  marker whose downstream Measurement Definition Revision is withheld
  (cascade by parent restriction), while the imported Record in the
  viewable scope resolves normally.

Setup follows the Slice 4 property-test conventions
(``tests/property/test_property_46_outcome_creation_anchoring.py``): a fresh
per-case SQLite engine on a unique :class:`tempfile.TemporaryDirectory`
path so cross-case state cannot leak; fresh services per case so
:class:`IdentityService` in-memory state cannot bleed across shrinks; a
:class:`FixedClock` pinned to ``2026-01-01T00:00:00.000Z``. The full chain
is built through the *real* Slice 4 services so the ``Relationships`` rows
the traversal walks exist exactly as production writes them; the Slice 1
leg is seeded through the real :class:`EvidenceRepository` and
:class:`KnowledgeService` so the Region Occurrence digest recorded at
occurrence-creation time is byte-equivalent to the digest the delegated
:meth:`navigate_decision` tail surfaces (mirroring
``tests/property/test_property_37_execution_provenance_chain.py``).
"""

from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import uuid_utils

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.evidence import (
    CreateDocumentResult,
    CreateRegionResult,
    EvidenceRepository,
)
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateDecisionResult,
    CreateFindingResult,
    CreateRecommendationResult,
    KnowledgeService,
    SupportRef,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome._provenance import (
    AssessmentObservationChain,
    IntendedOutcomeRevisionNode,
    MeasurementChain,
    MeasurementDefinitionRevisionNode,
    MeasurementRecordNode,
    ObservedOutcomeRevisionNode,
    OutcomeProvenanceTree,
    OutcomeReviewNode,
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
    ChainGapDescriptor,
    DecisionNode,
    DeliverableRevisionNode,
    DocumentRevisionNode,
    FindingRevisionNode,
    ProvenanceNavigator,
    RecommendationRevisionNode,
    RedactedNode,
    RegionOccurrenceNode,
)
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    get_policy,
    seed as seed_disclosure_policies,
)


pytestmark = pytest.mark.property

# The Slice 4 traversals are attached to ProvenanceNavigator at import time.
register_outcome_navigation()


# ---------------------------------------------------------------------------
# Fixed identifiers and constants.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"
# Navigation effective time falls strictly after every seeded ``recorded_at``
# and after the requester's Role Assignment ``effective_start``.
_AT: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_REPETITIONS: Final[int] = 5

_OWNER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00005"
_REVIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00006"
_COMPLETING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00007"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00008"
_VIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00009"
_SCOPED_VIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a0000a"

_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID: Final[str] = "00000000-0000-7000-8000-000000c00002"

_SCOPE: Final[str] = "pilot/team-a"
_SCOPE_OTHER: Final[str] = "pilot/team-b"

_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-0000000ba001"
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=UUID(_AUTHORITY_BASIS_ID)
)

# Directly-seeded citable Slice 3 artifacts (the parallel Completion leg).
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000d00004"
)

_UNIT: Final[str] = "percent"
# Closed observation window covering the 2025 observation instants drawn by
# the strategies; both edges precede the fixed recorded time (2026-01-01).
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"

_SOURCE_SYSTEM_AUTHORITIES: Final[tuple[str, ...]] = (
    "authoritative",
    "replica",
    "projection",
    "index",
    "federation",
)
_REVIEW_OUTCOMES: Final[tuple[str, ...]] = (
    "Achieved",
    "Partially_Achieved",
    "Not_Achieved",
    "Inconclusive",
)
_ATTRIBUTION_STANCES: Final[tuple[str, ...]] = (
    "Asserted",
    "Partial",
    "Unattributed",
    "Contradicted",
)
_CONFIDENCE_LEVELS: Final[tuple[str, ...]] = ("High", "Moderate", "Low")
# Excludes ``Unassessable`` so the assessment rationale stays short and valid
# without tripping the >= 200-char Unassessable rule (Requirement 48.3).
_ASSESSMENT_CATEGORIES: Final[tuple[str, ...]] = (
    "Satisfied",
    "Partially_Satisfied",
    "Not_Satisfied",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every schema."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    create_outcome_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Per-case service bundle.
# ---------------------------------------------------------------------------


class _Services:
    """Per-case bundle of every collaborator the chain builders need."""

    def __init__(self) -> None:
        self.clock = FixedClock(_NOW)
        self.identity = IdentityService()
        self.audit = AuditLog(self.clock)
        self.authz = AuthorizationService(
            clock=self.clock,
            audit_log=self.audit,
            identity_service=self.identity,
        )
        self.evidence = EvidenceRepository(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
        )
        self.knowledge = KnowledgeService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
        )
        self.intended = IntendedOutcomeService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
        )
        self.definitions = MeasurementDefinitionService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
        )
        self.records = MeasurementRecordService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            definition_reader=self.definitions,
        )
        self.observed = ObservedOutcomeService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
            measurement_reader=self.records,
            definition_reader=self.definitions,
        )
        self.assessments = SuccessConditionAssessmentService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
            observed_outcome_reader=self.observed,
        )
        self.completions = CompletionService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            planning_reader=PlanRevisionService(
                clock=None,  # type: ignore[arg-type]
                identity_service=None,  # type: ignore[arg-type]
                audit_log=None,  # type: ignore[arg-type]
                authorization_service=None,  # type: ignore[arg-type]
            ),
            project_resolver=ProjectResolver(),
            denial_audit_sleep=lambda _seconds: None,
        )
        self.deliverables = DeliverableRepositoryService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            denial_audit_sleep=lambda _seconds: None,
        )
        self.reviews = OutcomeReviewService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
            assessment_reader=self.assessments,
            completion_reader=self.completions,
            deliverable_reader=self.deliverables,
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
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


def _assign_role(
    authz: AuthorizationService,
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
        effective_start=_NOW - timedelta(days=365),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authz.assign_role(conn, request)


def _seed_parties(engine: Engine) -> None:
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


def _grant_write_roles(authz: AuthorizationService, engine: Engine) -> None:
    """Grant the precise Slice 4 write authorities (real permit path)."""
    for party_id, role_name, authority in (
        (_OWNER_PARTY_ID, "intended_outcome_owner", "modify"),
        (_DEFINER_PARTY_ID, "measurement_definer", "define_measurement"),
        (_RECORDER_PARTY_ID, "measurement_recorder", "record_measurement"),
        (_ASSESSOR_PARTY_ID, "outcome_assessor", "assess_outcome"),
        (_REVIEWER_PARTY_ID, "outcome_reviewer", "issue_outcome_review"),
    ):
        _assign_role(
            authz,
            engine,
            party_id=party_id,
            role_name=role_name,
            authority=authority,
            scope="*",
        )


def _grant_view(
    authz: AuthorizationService, engine: Engine, *, party_id: str, scope: str
) -> None:
    _assign_role(
        authz,
        engine,
        party_id=party_id,
        role_name="viewer",
        authority="view",
        scope=scope,
    )


def _seed_slice1_decision(
    svc: _Services,
    engine: Engine,
    *,
    content_bytes: bytes,
    span: tuple[int, int],
    finding_statement: str,
) -> tuple[str, str, str, str, str, str]:
    """Seed the Slice 1 evidence/knowledge leg via the real services.

    Returns ``(decision_id, recommendation_id, recommendation_revision_id,
    finding_id, region_id, document_revision_id)``.
    """
    start, end = span
    with engine.begin() as conn:
        doc: CreateDocumentResult = svc.evidence.create_document(
            conn,
            content_bytes=content_bytes,
            contributing_party_id=_OWNER_PARTY_ID,
            authority="authoritative",
        )
        region: CreateRegionResult = svc.evidence.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=start,
            end_offset_bytes=end,
            contributing_party_id=_OWNER_PARTY_ID,
        )
        finding: CreateFindingResult = svc.knowledge.create_finding(
            conn,
            statement=finding_statement,
            authoring_party_id=_OWNER_PARTY_ID,
            is_hypothesis=False,
            supporting_region_occurrences=(
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                ),
            ),
        )
        recommendation: CreateRecommendationResult = (
            svc.knowledge.create_recommendation(
                conn,
                authoring_party_id=_OWNER_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="Adopt the recommended telemetry platform.",
            )
        )
        decision: CreateDecisionResult = svc.knowledge.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Decision anchored on the accepted recommendation.",
            deciding_party_id=_OWNER_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
        )
    return (
        decision.decision_id,
        recommendation.recommendation_id,
        recommendation.recommendation_revision_id,
        finding.finding_id,
        region.region_id,
        doc.revision_id,
    )


def _seed_objective(engine: Engine, *, target_decision_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _NOW_ISO},
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
                "did": target_decision_id,
                "pid": _OWNER_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
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
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
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
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
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
            {"did": _CITABLE_DELIVERABLE_ID, "ts": _NOW_ISO},
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
                "ts": _NOW_ISO,
            },
        )
    return _CITABLE_DELIVERABLE_REVISION_ID


def _seed_unresolved_omission(
    engine: Engine,
    *,
    subject_kind: str,
    subject_id: str,
    category: str,
) -> None:
    """Seed a Provenance Manifest with one unresolved Omission Entry.

    Used to exercise the gap-descriptor surface the Slice 4 traversal
    delegates to (Requirements 51.3 / 55.4).
    """
    manifest_id = str(uuid_utils.uuid7())
    omission_id = str(uuid_utils.uuid7())
    excluded_id = str(uuid_utils.uuid7())
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Provenance_Manifests (
                    manifest_id, subject_kind, subject_id,
                    subject_revision_id, authoring_party_id, recorded_at,
                    included_sources_json, is_complete
                ) VALUES (
                    :mid, :kind, :sid, NULL, :party, :ts, '[]', 0
                )
                """
            ),
            {
                "mid": manifest_id,
                "kind": subject_kind,
                "sid": subject_id,
                "party": _OWNER_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Omission_Entries (
                    omission_entry_id, manifest_id, excluded_source_id,
                    excluded_source_revision_id, category, rationale,
                    authoring_party_id, recorded_at, resolved_at
                ) VALUES (
                    :oid, :mid, :xid, NULL, :cat,
                    'Awaiting upstream link.', :party, :ts, NULL
                )
                """
            ),
            {
                "oid": omission_id,
                "mid": manifest_id,
                "xid": excluded_id,
                "cat": category,
                "party": _OWNER_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Full Slice 4 chain builder (built through the real services).
# ---------------------------------------------------------------------------


def _build_chain(
    svc: _Services,
    engine: Engine,
    *,
    scenario: dict[str, Any],
    native_record_scope: str = _SCOPE,
) -> dict[str, str]:
    """Build the full Slice 1–4 pipeline and return the key identities."""
    (
        decision_id,
        recommendation_id,
        recommendation_revision_id,
        finding_id,
        region_id,
        document_revision_id,
    ) = _seed_slice1_decision(
        svc,
        engine,
        content_bytes=scenario["content_bytes"],
        span=scenario["span"],
        finding_statement=scenario["finding_statement"],
    )
    _seed_objective(engine, target_decision_id=decision_id)

    with engine.begin() as conn:
        intended = svc.intended.create_intended_outcome(
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
        definition = svc.definitions.create_measurement_definition(
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
    with engine.begin() as conn:
        native = svc.records.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=(
                definition.measurement_definition_revision_id
            ),
            observed_value=scenario["native_value"],
            observed_value_unit=_UNIT,
            observation_time=scenario["native_observation_time"],
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=native_record_scope,
            engine=engine,
        )
    with engine.begin() as conn:
        imported = svc.records.create_imported_measurement(
            conn,
            target_measurement_definition_revision_id=(
                definition.measurement_definition_revision_id
            ),
            observed_value=scenario["imported_value"],
            observed_value_unit=_UNIT,
            observation_time=scenario["imported_observation_time"],
            source_system_id=scenario["source_system_id"],
            source_system_record_id=scenario["source_system_record_id"],
            source_system_authority=scenario["source_system_authority"],
            source_system_retrieval_time=scenario["imported_retrieval_time"],
            importing_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    with engine.begin() as conn:
        observed = svc.observed.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=(
                intended.intended_outcome_revision_id
            ),
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
        assessment = svc.assessments.create_assessment(
            conn,
            target_intended_outcome_revision_id=(
                intended.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                observed.observed_outcome_revision_id
            ),
            assessment_category=scenario["assessment_category"],
            assessment_rationale="Measured adoption met the success threshold.",
            assessing_party_id=_ASSESSOR_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    completion_id = _seed_citable_completion(engine)
    deliverable_revision_id = _seed_citable_deliverable_revision(engine)
    with engine.begin() as conn:
        review = svc.reviews.create_outcome_review(
            conn,
            target_intended_outcome_revision_id=(
                intended.intended_outcome_revision_id
            ),
            review_outcome=scenario["review_outcome"],
            attribution_stance=scenario["attribution_stance"],
            confidence=scenario["confidence"],
            review_rationale="Reviewed evidence and concluded success.",
            attribution_evidence_reference=scenario[
                "attribution_evidence_reference"
            ],
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
    return {
        "intended_outcome_revision_id": intended.intended_outcome_revision_id,
        "measurement_definition_revision_id": (
            definition.measurement_definition_revision_id
        ),
        "native_record_id": native.measurement_record_id,
        "imported_record_id": imported.measurement_record_id,
        "observed_outcome_revision_id": observed.observed_outcome_revision_id,
        "assessment_id": assessment.assessment_id,
        "outcome_review_id": review.outcome_review_id,
        "completion_id": completion_id,
        "deliverable_revision_id": deliverable_revision_id,
        "decision_id": decision_id,
        "region_id": region_id,
        "document_revision_id": document_revision_id,
    }


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)
# Source-system identifiers avoid whitespace/control characters that some
# drivers reject; the property is about chain shape, not identifier robustness.
_IDENT_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."
)


def _decimal_strategy() -> st.SearchStrategy[Decimal]:
    """Draw an observed value with at most six fractional digits.

    Six is the maximum the native validation accepts (Requirement 45);
    constraining the generator keeps every drawn value valid so the
    positive-path property exercises the full traversal rather than the
    rejection path.
    """
    int_part = st.integers(min_value=0, max_value=1000)
    frac_digits = st.integers(min_value=0, max_value=6)

    @st.composite
    def _build(draw) -> Decimal:
        whole = draw(int_part)
        digits = draw(frac_digits)
        if digits == 0:
            return Decimal(str(whole))
        frac = draw(st.integers(min_value=0, max_value=10**digits - 1))
        return Decimal(f"{whole}.{frac:0{digits}d}")

    return _build()


@st.composite
def _scenario_strategy(draw) -> dict[str, Any]:
    """Draw one fully-valid Slice 1–4 pipeline scenario."""
    content_length = draw(st.integers(min_value=1, max_value=96))
    content_bytes = draw(
        st.binary(min_size=content_length, max_size=content_length)
    )
    start = draw(st.integers(min_value=0, max_value=content_length - 1))
    end = draw(st.integers(min_value=start + 1, max_value=content_length))

    # Observation/retrieval instants inside 2025 with observation <= retrieval
    # (Requirement 46: observation <= retrieval <= recorded). All precede the
    # fixed recorded time (2026-01-01).
    native_day = draw(st.integers(min_value=1, max_value=300))
    imported_obs_day = draw(st.integers(min_value=1, max_value=200))
    imported_retrieval_offset = draw(st.integers(min_value=0, max_value=120))
    base = datetime(2025, 1, 1, tzinfo=timezone.utc)
    native_observation_time = base + timedelta(days=native_day)
    imported_observation_time = base + timedelta(days=imported_obs_day)
    imported_retrieval_time = imported_observation_time + timedelta(
        days=imported_retrieval_offset
    )

    stance = draw(st.sampled_from(_ATTRIBUTION_STANCES))
    # Asserted / Contradicted require a non-empty attribution-evidence
    # reference (Requirement 49.4).
    if stance in ("Asserted", "Contradicted"):
        evidence = draw(
            st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=80)
        )
    else:
        evidence = draw(
            st.text(alphabet=_TEXT_ALPHABET, min_size=0, max_size=80)
        )

    return {
        "content_bytes": content_bytes,
        "span": (start, end),
        "finding_statement": draw(
            st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=80)
        ),
        "native_value": draw(_decimal_strategy()),
        "imported_value": draw(_decimal_strategy()),
        "native_observation_time": native_observation_time,
        "imported_observation_time": imported_observation_time,
        "imported_retrieval_time": imported_retrieval_time,
        "source_system_id": draw(
            st.text(alphabet=_IDENT_ALPHABET, min_size=1, max_size=40)
        ),
        "source_system_record_id": draw(
            st.text(alphabet=_IDENT_ALPHABET, min_size=1, max_size=40)
        ),
        "source_system_authority": draw(
            st.sampled_from(_SOURCE_SYSTEM_AUTHORITIES)
        ),
        "assessment_category": draw(st.sampled_from(_ASSESSMENT_CATEGORIES)),
        "review_outcome": draw(st.sampled_from(_REVIEW_OUTCOMES)),
        "attribution_stance": stance,
        "confidence": draw(st.sampled_from(_CONFIDENCE_LEVELS)),
        "attribution_evidence_reference": evidence,
        "omission_category": draw(
            st.sampled_from(("unavailable", "stale", "unresolved"))
        ),
    }


# ---------------------------------------------------------------------------
# Property tests.
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(scenario=_scenario_strategy())
def test_outcome_provenance_chain_resolves_end_to_end(
    scenario: dict[str, Any],
) -> None:
    """Both ordered chains resolve; digests match; records carry origin;
    retrieval is byte-equivalent across five repetitions."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop53_") as raw:
        engine = _build_engine(Path(raw))
        try:
            svc = _Services()
            _seed_parties(engine)
            _grant_write_roles(svc.authz, engine)
            ids = _build_chain(svc, engine, scenario=scenario)
            _grant_view(
                svc.authz, engine, party_id=_VIEWER_PARTY_ID, scope="*"
            )

            # Seed an unresolved Omission Entry on the (enumerated) Decision
            # subject at the chain tail so the canonical gap-descriptor shape
            # is exercised across every generated case. The Slice 4 traversal
            # delegates unresolved-link detection to this same collection
            # helper (Slice 4 subject kinds are not yet enumerated in the
            # Provenance_Manifests CHECK, so an enumerated subject is used).
            _seed_unresolved_omission(
                engine,
                subject_kind="decision",
                subject_id=ids["decision_id"],
                category=scenario["omission_category"],
            )
            seed_disclosure_policies(engine)
            policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)

            navigator = ProvenanceNavigator(
                clock=svc.clock, authorization_service=svc.authz
            )
            # Gap-descriptor collection requires a policy-configured navigator
            # (the helper returns an empty tuple otherwise — the Slice 3
            # navigate_completion convention).
            gap_navigator = ProvenanceNavigator(
                clock=svc.clock,
                authorization_service=svc.authz,
                disclosure_policy=policy,
            )

            trees = []
            for _ in range(_REPETITIONS):
                with engine.connect() as conn:
                    trees.append(
                        navigator.navigate_outcome_review(
                            conn,
                            outcome_review_id=ids["outcome_review_id"],
                            party_id=_VIEWER_PARTY_ID,
                            at=_AT,
                        )
                    )

            tree = trees[0]
            assert isinstance(tree, OutcomeProvenanceTree)

            # -- Head resolves. ----------------------------------------
            assert isinstance(tree.outcome_review, OutcomeReviewNode)
            assert tree.outcome_review.outcome_review_id == (
                ids["outcome_review_id"]
            )

            # -- Assessment leg: every identity resolves. --------------
            assert len(tree.assessment_chains) == 1
            ac = tree.assessment_chains[0]
            assert isinstance(ac, AssessmentObservationChain)
            assert isinstance(ac.assessment, SuccessConditionAssessmentNode)
            assert ac.assessment.assessment_id == ids["assessment_id"]
            assert isinstance(
                ac.observed_outcome_revision, ObservedOutcomeRevisionNode
            )
            assert (
                ac.observed_outcome_revision.observed_outcome_revision_id
                == ids["observed_outcome_revision_id"]
            )

            # Both cited Measurement Records resolve with their Definition
            # Revision; each node carries the origin indicator.
            assert len(ac.measurement_chains) == 2
            records: dict[str, MeasurementRecordNode] = {}
            for mc in ac.measurement_chains:
                assert isinstance(mc, MeasurementChain)
                assert isinstance(mc.measurement_record, MeasurementRecordNode)
                records[mc.measurement_record.measurement_record_id] = (
                    mc.measurement_record
                )
                assert isinstance(
                    mc.measurement_definition_revision,
                    MeasurementDefinitionRevisionNode,
                )
                assert (
                    mc.measurement_definition_revision.measurement_definition_revision_id
                    == ids["measurement_definition_revision_id"]
                )
            assert set(records) == {
                ids["native_record_id"],
                ids["imported_record_id"],
            }

            # Requirement 55.8 — native origin with NULL source-system fields;
            # imported origin with source-system identifier + authority.
            native_node = records[ids["native_record_id"]]
            imported_node = records[ids["imported_record_id"]]
            assert native_node.origin == "native"
            assert native_node.source_system_id is None
            assert native_node.source_system_authority is None
            assert imported_node.origin == "imported"
            assert imported_node.source_system_id == (
                scenario["source_system_id"]
            )
            assert imported_node.source_system_authority == (
                scenario["source_system_authority"]
            )

            # -- Completion leg + cited Deliverable Revision. ----------
            assert len(tree.completion_chains) == 1
            assert tree.completion_chains[0].completion_id == (
                ids["completion_id"]
            )
            assert tree.completion_chains[0].execution_tree is not None
            assert len(tree.cited_deliverable_revisions) == 1
            cited_deliverable = tree.cited_deliverable_revisions[0]
            assert isinstance(cited_deliverable, DeliverableRevisionNode)
            assert cited_deliverable.deliverable_revision_id == (
                ids["deliverable_revision_id"]
            )

            # -- Intended Outcome → Slice 1 Decision tail. -------------
            assert isinstance(
                tree.intended_outcome_revision, IntendedOutcomeRevisionNode
            )
            assert (
                tree.intended_outcome_revision.intended_outcome_revision_id
                == ids["intended_outcome_revision_id"]
            )
            assert tree.decision_chain is not None
            assert isinstance(tree.decision_chain.decision, DecisionNode)
            assert tree.decision_chain.decision.decision_id == (
                ids["decision_id"]
            )
            assert isinstance(
                tree.decision_chain.recommendation_revision,
                RecommendationRevisionNode,
            )

            # Requirement 55.2 — Region Occurrence span digest-matches the
            # recorded content digest (recomputed independently).
            assert len(tree.decision_chain.findings) == 1
            assert isinstance(
                tree.decision_chain.findings[0], FindingRevisionNode
            )
            assert len(tree.decision_chain.region_occurrences) == 1
            assert len(tree.decision_chain.document_revisions) == 1
            region_node = tree.decision_chain.region_occurrences[0]
            doc_node = tree.decision_chain.document_revisions[0]
            assert isinstance(region_node, RegionOccurrenceNode)
            assert isinstance(doc_node, DocumentRevisionNode)
            assert region_node.region_id == ids["region_id"]
            assert doc_node.revision_id == ids["document_revision_id"]

            start, end = scenario["span"]
            expected_span_bytes = scenario["content_bytes"][start:end]
            expected_digest = hashlib.sha256(expected_span_bytes).hexdigest()
            assert region_node.bounded_text == expected_span_bytes
            assert region_node.span_content_digest_sha256 == hashlib.sha256(
                region_node.bounded_text
            ).hexdigest()
            assert region_node.span_content_digest_sha256 == expected_digest

            # Requirements 51.4 / 55.5 — byte-equivalent across repetitions.
            for other in trees[1:]:
                assert other == tree

            # Requirements 51.3 / 55.4 — the unresolved Omission Entry seeded
            # on the Decision tail surfaces a gap descriptor carrying only
            # ``stage``, ``category`` ∈ {unavailable, stale, unresolved}, and
            # the next reachable node identity.
            with engine.connect() as conn:
                descriptors = _collect_outcome_gap_descriptors(
                    gap_navigator,
                    conn,
                    subject_kind="decision",
                    subject_id=ids["decision_id"],
                    subject_revision_id=None,
                    next_reachable_node_identity=ids["decision_id"],
                )
            assert len(descriptors) == 1
            gap = descriptors[0]
            assert isinstance(gap, ChainGapDescriptor)
            assert gap.stage == "decision"
            assert gap.category == scenario["omission_category"]
            assert gap.next_reachable_node_identity == ids["decision_id"]
        finally:
            engine.dispose()


@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(scenario=_scenario_strategy())
def test_restricted_measurement_record_redacts(
    scenario: dict[str, Any],
) -> None:
    """A Measurement Record outside the Party's view scope surfaces as a
    ``{kind, redacted: True}`` marker and withholds its Definition Revision."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop53r_") as raw:
        engine = _build_engine(Path(raw))
        try:
            svc = _Services()
            _seed_parties(engine)
            _grant_write_roles(svc.authz, engine)
            ids = _build_chain(
                svc, engine, scenario=scenario, native_record_scope=_SCOPE_OTHER
            )
            # The scoped viewer sees _SCOPE but not _SCOPE_OTHER.
            _grant_view(
                svc.authz,
                engine,
                party_id=_SCOPED_VIEWER_PARTY_ID,
                scope=_SCOPE,
            )

            navigator = ProvenanceNavigator(
                clock=svc.clock, authorization_service=svc.authz
            )
            with engine.connect() as conn:
                tree = navigator.navigate_outcome_review(
                    conn,
                    outcome_review_id=ids["outcome_review_id"],
                    party_id=_SCOPED_VIEWER_PARTY_ID,
                    at=_AT,
                )

            ac = tree.assessment_chains[0]
            redacted = [
                mc
                for mc in ac.measurement_chains
                if isinstance(mc.measurement_record, RedactedNode)
            ]
            visible = [
                mc
                for mc in ac.measurement_chains
                if isinstance(mc.measurement_record, MeasurementRecordNode)
            ]
            assert len(redacted) == 1
            assert len(visible) == 1

            redacted_chain = redacted[0]
            assert redacted_chain.measurement_record.kind == (
                "measurement_record"
            )
            assert redacted_chain.measurement_record.redacted is True
            # Cascade by parent restriction: no Definition Revision leak.
            assert redacted_chain.measurement_definition_revision is None

            assert visible[0].measurement_record.measurement_record_id == (
                ids["imported_record_id"]
            )
        finally:
            engine.dispose()
