# Feature: fourth-walking-slice, Property 57: Slice 1, Slice 2, and Slice 3 non-modification under Slice 4 actions
"""Property 57 — Slice 1, Slice 2, and Slice 3 non-modification under Slice 4 actions (task 15.12).

**Property 57: Slice 1, Slice 2, and Slice 3 non-modification under Slice 4 actions**

*For all* test sessions exercising the Outcome_Service, at every observation
point after any sequence of Slice 4 actions, every row created by Slice 1,
Slice 2, or Slice 3 — ``Audit_Records``, ``Identifier_Registry``,
``Interim_ADR_Records``, ``Disclosure_Policies``,
``Disclosure_Policy_Coverage`` (apart from the additive coverage rows seeded
by AD-WS-34), ``Role_Assignments`` (apart from the additive twelve-value
enumeration permitted by AD-WS-33), ``Decisions``, ``Document_Revisions``,
``Region_Occurrences``, ``Finding_Revisions``, ``Recommendation_Revisions``,
``Relationships`` (apart from new rows inserted by Slice 4 actions),
``Trail_Revisions``, ``Trail_Steps``, ``Provenance_Manifests``,
``Objective_Revisions``, ``Intended_Outcome_Revisions``, ``Project_Revisions``,
``Deliverable_Expectation_Revisions``, ``Activity_Plans``, ``Plan_Revisions``,
``Plan_Review_Revisions``, ``Plan_Approval_Records``,
``Work_Assignment_Records``, ``Work_Event_Records``, ``Time_Entry_Records``,
``Deliverable_Production_Records``, ``Milestone_Acceptance_Records``,
``Completion_Records``, ``Deliverable_Resources``, and ``Deliverable_Revisions``
— is byte-equivalent to its state before the Slice 4 actions began.

**Validates: Requirements 46.8, 47.8, 48.7, 49.8, 53.1, 53.5, 54.5, 60.1,
60.2, 60.3, 60.4, 61.12**

Strategy
========

Each Hypothesis case:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing Strategy" —
   per-case database isolation) carrying the Slice 1 schema
   (:func:`walking_slice.persistence.create_schema`), the Slice 2 Planning
   schema, the Slice 3 Execution + Deliverable_Repository schemas, and the
   Slice 4 Outcome schema.

2. Seeds the full prior-slice (Slice 1 + Slice 2 + Slice 3) baseline:

   - a representative Slice 1 knowledge graph (Document Revision, Region
     Occurrence, Finding Revision, Recommendation Revision, Decision, a
     ``Supports`` Relationship, a Trail + Revision + five Steps, a
     Provenance Manifest, an Interim ADR row, and one consequential Audit
     row) by direct ``INSERT`` (matching the Property 21 / Property 41
     convention);
   - the Slice 1 ``slice-default-2026`` Disclosure_Policies row plus the
     additive Slice 2 / Slice 3 ``Disclosure_Policy_Coverage`` rows;
   - the Slice 2 Objective + the Intended Outcome (``outcome_kind =
     'intended'``) created through the *real* Slice 2
     :class:`IntendedOutcomeService` so the anchor the Slice 4 services
     resolve exists exactly as production writes it (and so its
     consequential Audit / Relationship / Identifier_Registry rows are part
     of the protected baseline);
   - the citable Slice 3 Completion Record, Work Assignment Record,
     Deliverable Resource, and produced Deliverable Revision by direct
     ``INSERT`` (the Outcome Review cites them);
   - every Role Assignment the Slice 4 pipeline needs.

3. Snapshots every protected prior-slice table named by Property 57.
   Tables that legitimately *grow* under Slice 4 actions
   (``Audit_Records``, ``Identifier_Registry``, ``Relationships``,
   ``Disclosure_Policy_Coverage``, ``Role_Assignments``) are snapshotted by
   their captured primary-key set so new Slice 4 rows are excluded by
   construction (the property's explicit exceptions). Every other
   prior-slice table is snapshotted as a full ``{pk: row}`` map so the
   post-action comparison catches an erroneous INSERT, DELETE, or UPDATE.

4. Runs a Hypothesis-drawn sequence of Slice 4 actions through the *real*
   Outcome_Service write services: a Measurement Definition, a native
   and/or imported Measurement Record, an Observed Outcome Revision
   (optionally a second linked Revision), a Success-Condition Assessment,
   an Outcome Review citing the Assessment + Completion + produced
   Deliverable Revision, and an optional *denied* Measurement Definition
   attempt (an unauthorized Party) that appends a Denial Record. The
   denied attempt exercises Requirement 53.5 — a rejected Slice 4 action
   must leave every prior-slice row byte-equivalent.

5. After *each* Slice 4 action (the "every observation point" quantifier),
   re-reads the protected tables and asserts byte-equivalence with the
   pre-Slice-4 snapshot. A final whole-snapshot check runs after the
   complete sequence so a regression that only manifests at end-of-pipeline
   still surfaces.

Setup follows the conventions of the Slice 1–4 property tests (per-case
:class:`tempfile.TemporaryDirectory` ownership of the SQLite file, fresh
services per case so :class:`IdentityService` in-memory state cannot bleed
across shrinks, :class:`FixedClock` pinned to ``2026-01-01T00:00:00.000Z``).
``@settings(max_examples=100, deadline=2000)`` per Requirement 61.15 /
AD-WS-13; ``HealthCheck.too_slow`` and ``HealthCheck.data_too_large`` are
suppressed because the per-case setup builds the full prior-slice baseline
and runs the multi-step Slice 4 pipeline.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.disclosure import seed as disclosure_seed
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionAuthorizationError,
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import OutcomeReviewService
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import seed_planning_coverage
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.plan_revisions import PlanRevisionService
from walking_slice.execution._disclosure import seed_execution_coverage


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and constants.
#
# The prior-slice baseline IDs are deterministic so a shrunken counterexample
# is actionable; the Slice 4 entities under test mint fresh UUIDv7 identifiers
# through the real services.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

# Prior-slice seed timestamp is offset from the per-case clock so any row
# whose recorded_at accidentally changes to the Slice 4 clock value surfaces
# as a snapshot diff.
_SEED_TS: Final[str] = "2025-12-15T10:30:00.000Z"

# Parties.
_OWNER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_DEFINER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a3"
_RECORDER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a4"
_ASSESSOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a5"
_REVIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a6"
_COMPLETING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a7"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a8"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a9"

# Slice 1 knowledge-graph identifiers.
_DOCUMENT_RESOURCE_ID: Final[str] = "00000000-0000-7000-8000-0000000000b1"
_DOCUMENT_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b2"
_REGION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b3"
_FINDING_ID: Final[str] = "00000000-0000-7000-8000-0000000000b4"
_FINDING_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b5"
_RECOMMENDATION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b6"
_RECOMMENDATION_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b7"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b8"
_RELATIONSHIP_ID: Final[str] = "00000000-0000-7000-8000-0000000000b9"
_TRAIL_ID: Final[str] = "00000000-0000-7000-8000-0000000000ba"
_TRAIL_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000bb"
_MANIFEST_ID: Final[str] = "00000000-0000-7000-8000-0000000000bc"
_INTERIM_ADR_RECORD_ID: Final[str] = "00000000-0000-7000-8000-0000000000bd"
_AUDIT_RECORD_ID: Final[str] = "00000000-0000-7000-8000-0000000000be"
_TRAIL_STEP_IDS: Final[tuple[str, ...]] = (
    "00000000-0000-7000-8000-0000000000c1",
    "00000000-0000-7000-8000-0000000000c2",
    "00000000-0000-7000-8000-0000000000c3",
    "00000000-0000-7000-8000-0000000000c4",
    "00000000-0000-7000-8000-0000000000c5",
)

_SLICE1_IDENTIFIER_ROWS: Final[tuple[tuple[str, str], ...]] = (
    (_DOCUMENT_RESOURCE_ID, "resource"),
    (_DOCUMENT_REVISION_ID, "revision"),
    (_REGION_ID, "region"),
    (_FINDING_ID, "resource"),
    (_FINDING_REVISION_ID, "revision"),
    (_RECOMMENDATION_ID, "resource"),
    (_RECOMMENDATION_REVISION_ID, "revision"),
    (_DECISION_ID, "immutable_record"),
    (_RELATIONSHIP_ID, "relationship"),
    (_TRAIL_ID, "trail"),
    (_TRAIL_REVISION_ID, "trail_revision"),
    (_MANIFEST_ID, "manifest"),
)

# Slice 2 identifiers.
_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_OBJECTIVE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000d2"

# Slice 3 citable artifacts (for the Outcome Review).
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-0000000000e1"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000000000e2"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-0000000000e3"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000e4"
)

_SCOPE: Final[str] = "property-57/scope"
_UNIT: Final[str] = "percent"
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"

_AUTHORITY_BASIS_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000000f0")
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

_SOURCE_SYSTEM_AUTHORITIES: Final[tuple[str, ...]] = (
    "authoritative",
    "replica",
    "projection",
    "index",
    "federation",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every slice's schema."""
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
# Per-case service bundle (mirrors Property 46).
# ---------------------------------------------------------------------------


class _Services:
    """Per-case bundle of every Outcome_Service collaborator the pipeline uses.

    Fresh per Hypothesis case so :class:`IdentityService` in-memory state
    cannot bleed across shrinks; the denial-audit sleep is a no-op so the
    deny path never spends real time on its retry sequence.
    """

    def __init__(self) -> None:
        self.clock = FixedClock(_NOW)
        self.identity = IdentityService()
        self.audit = AuditLog(self.clock)
        self.authz = AuthorizationService(
            clock=self.clock,
            audit_log=self.audit,
            identity_service=self.identity,
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
# Prior-slice seed helpers.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _SEED_TS},
    )


def _assign_role(
    authz: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    role_name: str,
    authority: str,
) -> None:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=_SCOPE,
        authorities_granted=(authority,),
        effective_start=_NOW - timedelta(days=365),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authz.assign_role(conn, request)


def _seed_slice1_graph(engine: Engine) -> None:
    """Seed a representative Slice 1 knowledge graph by direct INSERT."""
    with engine.begin() as conn:
        for identifier, kind in _SLICE1_IDENTIFIER_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO Identifier_Registry
                        (identifier, kind, content_digest, issued_at, resource_kind)
                    VALUES (:identifier, :kind, NULL, :issued_at, NULL)
                    """
                ),
                {"identifier": identifier, "kind": kind, "issued_at": _SEED_TS},
            )

        conn.execute(
            text(
                "INSERT INTO Source_Documents "
                "(resource_id, current_location, external_identifier, "
                " source_system_id, authority, created_at) "
                "VALUES (:rid, 'file:///doc.txt', NULL, NULL, "
                " 'authoritative', :ts)"
            ),
            {"rid": _DOCUMENT_RESOURCE_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Document_Revisions (
                    revision_id, resource_id, parent_revision_id,
                    content_bytes, content_digest_sha256,
                    contributing_party_id, recorded_at, change_description
                ) VALUES (
                    :rev, :res, NULL, :bytes, :digest, :party, :ts, 'initial'
                )
                """
            ),
            {
                "rev": _DOCUMENT_REVISION_ID,
                "res": _DOCUMENT_RESOURCE_ID,
                "bytes": b"hello prior slices",
                "digest": "a" * 64,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SEED_TS,
            },
        )

        conn.execute(
            text(
                "INSERT INTO Content_Regions "
                "(region_id, parent_resource_id, created_at) "
                "VALUES (:rid, :pid, :ts)"
            ),
            {"rid": _REGION_ID, "pid": _DOCUMENT_RESOURCE_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Region_Occurrences (
                    region_id, document_revision_id,
                    start_offset_bytes, end_offset_bytes, span_byte_length,
                    span_content_digest_sha256, recorded_at
                ) VALUES (:rid, :rev, 0, 5, 5, :digest, :ts)
                """
            ),
            {
                "rid": _REGION_ID,
                "rev": _DOCUMENT_REVISION_ID,
                "digest": "b" * 64,
                "ts": _SEED_TS,
            },
        )

        conn.execute(
            text("INSERT INTO Findings (finding_id, created_at) VALUES (:fid, :ts)"),
            {"fid": _FINDING_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Finding_Revisions (
                    finding_revision_id, finding_id, parent_revision_id,
                    statement, is_hypothesis, authoring_party_id,
                    assumptions_json, confidence_note, recorded_at
                ) VALUES (
                    :rev, :fid, NULL, 'Pre-Slice-4 finding.', 0, :party,
                    '[]', NULL, :ts
                )
                """
            ),
            {
                "rev": _FINDING_REVISION_ID,
                "fid": _FINDING_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SEED_TS,
            },
        )

        conn.execute(
            text(
                "INSERT INTO Recommendations (recommendation_id, created_at) "
                "VALUES (:rid, :ts)"
            ),
            {"rid": _RECOMMENDATION_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Recommendation_Revisions (
                    recommendation_revision_id, recommendation_id,
                    parent_revision_id, rationale, assumptions_json,
                    confidence, authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :rid, NULL, 'Pre-Slice-4 recommendation.', '[]',
                    'Medium', :party, :ts
                )
                """
            ),
            {
                "rev": _RECOMMENDATION_REVISION_ID,
                "rid": _RECOMMENDATION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SEED_TS,
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
                    :did, :rid, :rev, 'Accept', 'Pre-Slice-4 decision.', :party,
                    'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "did": _DECISION_ID,
                "rid": _RECOMMENDATION_ID,
                "rev": _RECOMMENDATION_REVISION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Supports', 'finding_revision', :fid, :frev,
                    'region_occurrence', :region, NULL, :party, :ts, NULL
                )
                """
            ),
            {
                "rid": _RELATIONSHIP_ID,
                "fid": _FINDING_ID,
                "frev": _FINDING_REVISION_ID,
                "region": _REGION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SEED_TS,
            },
        )

        conn.execute(
            text(
                "INSERT INTO Trails (trail_id, created_at, current_revision_id) "
                "VALUES (:tid, :ts, :rev)"
            ),
            {"tid": _TRAIL_ID, "ts": _SEED_TS, "rev": _TRAIL_REVISION_ID},
        )
        conn.execute(
            text(
                """
                INSERT INTO Trail_Revisions (
                    trail_revision_id, trail_id, predecessor_revision_id,
                    purpose, audience_id, ordering_rationale,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :tid, NULL, 'Pre-Slice-4 trail purpose.', :aud,
                    NULL, :party, :ts
                )
                """
            ),
            {
                "rev": _TRAIL_REVISION_ID,
                "tid": _TRAIL_ID,
                "aud": _ASSIGNING_AUTHORITY_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SEED_TS,
            },
        )
        _step_specs: tuple[
            tuple[int, str, str, Optional[str], Optional[str]], ...
        ] = (
            (1, "document_revision", _DOCUMENT_REVISION_ID, None, None),
            (2, "region_occurrence", _REGION_ID, _DOCUMENT_REVISION_ID, _REGION_ID),
            (3, "finding_revision", _FINDING_REVISION_ID, None, None),
            (4, "recommendation_revision", _RECOMMENDATION_REVISION_ID, None, None),
            (5, "decision", _DECISION_ID, None, None),
        )
        for step_id, (ordinal, target_kind, target_id, target_rev, region) in zip(
            _TRAIL_STEP_IDS, _step_specs
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Trail_Steps (
                        trail_step_id, trail_revision_id, ordinal,
                        selection_mode, target_kind, target_id,
                        target_revision_id, region_id, annotation
                    ) VALUES (
                        :sid, :trev, :ord, 'Pinned', :kind, :tid, :trgrev,
                        :rgn, NULL
                    )
                    """
                ),
                {
                    "sid": step_id,
                    "trev": _TRAIL_REVISION_ID,
                    "ord": ordinal,
                    "kind": target_kind,
                    "tid": target_id,
                    "trgrev": target_rev,
                    "rgn": region,
                },
            )

        conn.execute(
            text(
                """
                INSERT INTO Provenance_Manifests (
                    manifest_id, subject_kind, subject_id, subject_revision_id,
                    authoring_party_id, recorded_at, included_sources_json,
                    is_complete
                ) VALUES (:mid, 'decision', :sid, NULL, :party, :ts, '[]', 1)
                """
            ),
            {
                "mid": _MANIFEST_ID,
                "sid": _DECISION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SEED_TS,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO Interim_ADR_Records (
                    record_id, motivating_requirement, motivating_criterion,
                    observable_behavior, recorded_at, backlog_adr_id,
                    resolved_by_adr_id, resolved_at
                ) VALUES (
                    :rid, 'Slice1.R16', 'Slice1.R16.3',
                    'Pre-Slice-4 interim ADR behavior.', :ts, 'ADR-HT-001',
                    NULL, NULL
                )
                """
            ),
            {"rid": _INTERIM_ADR_RECORD_ID, "ts": _SEED_TS},
        )

        conn.execute(
            text(
                """
                INSERT INTO Audit_Records (
                    audit_record_id, append_sequence, actor_party_id,
                    action_type, outcome, target_id, target_revision_id,
                    evaluated_role_assignment_id, authorities_required,
                    authorities_held, reason_code, correlation_id, recorded_at
                ) VALUES (
                    :aid, 1, :party, 'create.decision', 'consequential', :did,
                    NULL, NULL, NULL, NULL, NULL, 'pre-slice4-correlation', :ts
                )
                """
            ),
            {
                "aid": _AUDIT_RECORD_ID,
                "party": _AUTHORING_PARTY_ID,
                "did": _DECISION_ID,
                "ts": _SEED_TS,
            },
        )


def _seed_objective(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO Objectives (objective_id, created_at) VALUES (:oid, :ts)"),
            {"oid": _OBJECTIVE_ID, "ts": _SEED_TS},
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
                    'Anchored on the accepted decision.', :did, :pid, :scope, :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REVISION_ID,
                "oid": _OBJECTIVE_ID,
                "did": _DECISION_ID,
                "pid": _OWNER_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )


def _seed_citable_completion(engine: Engine) -> None:
    """Seed one resolvable Slice 3 Completion Record by direct INSERT."""
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
                    :cid, :prev, :aid, :proj, 'Completed', 'Phase 1 completed.',
                    '[]', :party, 'role-grant-id', :abid, :scope, :ts
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
                "ts": _SEED_TS,
            },
        )


def _seed_citable_deliverable_revision(engine: Engine) -> None:
    """Seed one resolvable Slice 3 produced Deliverable Revision by direct INSERT."""
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
                    :wid, :prev, :assignee, :authority, 'Assigning the rollout.',
                    'role-grant-id', :abid, :scope, :ts
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
                "ts": _SEED_TS,
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
            {"did": _CITABLE_DELIVERABLE_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id, content_type,
                    content_bytes, content_digest_sha256, role_marker,
                    originating_work_assignment_id, authoring_party_id, recorded_at
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
                "digest": "c" * 64,
                "wa": _CITABLE_WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _SEED_TS,
            },
        )


def _seed_world(svc: _Services, engine: Engine) -> str:
    """Seed the full prior-slice baseline; return the Intended Outcome Revision id.

    Steps: Parties, the Slice 1 knowledge graph, the Slice 1 / Slice 2 /
    Slice 3 disclosure rows, the Objective, the role grants, the Intended
    Outcome (real Slice 2 service), and the citable Slice 3 artifacts.
    """
    with engine.begin() as conn:
        _seed_party(conn, _OWNER_PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
        _seed_party(conn, _DEFINER_PARTY_ID, "Measurement Definer")
        _seed_party(conn, _RECORDER_PARTY_ID, "Measurement Recorder")
        _seed_party(conn, _ASSESSOR_PARTY_ID, "Outcome Assessor")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
        _seed_party(conn, _AUTHORING_PARTY_ID, "Knowledge Author")

    _seed_slice1_graph(engine)

    # Disclosure baseline: Slice 1 policy + Slice 2 / Slice 3 coverage rows.
    disclosure_seed(engine)
    with engine.begin() as conn:
        seed_planning_coverage(conn)
        seed_execution_coverage(conn)

    _seed_objective(engine)

    for party_id, role_name, authority in (
        (_OWNER_PARTY_ID, "intended_outcome_owner", "modify"),
        (_DEFINER_PARTY_ID, "measurement_definer", "define_measurement"),
        (_RECORDER_PARTY_ID, "measurement_recorder", "record_measurement"),
        (_ASSESSOR_PARTY_ID, "outcome_assessor", "assess_outcome"),
        (_REVIEWER_PARTY_ID, "outcome_reviewer", "issue_outcome_review"),
    ):
        _assign_role(
            svc.authz, engine, party_id=party_id, role_name=role_name, authority=authority
        )

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

    _seed_citable_completion(engine)
    _seed_citable_deliverable_revision(engine)

    return intended.intended_outcome_revision_id


# ---------------------------------------------------------------------------
# Protected prior-slice table specifications.
#
# For every table named by Property 57, name the columns to SELECT and the
# primary-key columns the snapshot keys by. The additive
# ``Identifier_Registry.resource_kind`` column and the additive
# ``Relationships.semantic_role`` column are included so the post-action
# snapshot diff covers them — for pre-existing prior-slice rows these columns
# must remain byte-equivalent.
#
# Tables in :data:`_GROWTH_TABLES` legitimately receive *new* rows under
# Slice 4 actions (the property's explicit exceptions). The comparison helper
# checks only the captured baseline primary keys for those tables, so new
# Slice 4 rows are excluded by construction; every other table is compared
# for exact equality (catching any erroneous INSERT, DELETE, or UPDATE).
# ---------------------------------------------------------------------------


_PROTECTED_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    # ----- Slice 1 tables -----
    "Audit_Records": {
        "columns": (
            "audit_record_id",
            "append_sequence",
            "actor_party_id",
            "action_type",
            "outcome",
            "target_id",
            "target_revision_id",
            "evaluated_role_assignment_id",
            "authorities_required",
            "authorities_held",
            "reason_code",
            "correlation_id",
            "recorded_at",
        ),
        "pk": ("audit_record_id",),
    },
    "Identifier_Registry": {
        "columns": (
            "identifier",
            "kind",
            "content_digest",
            "issued_at",
            "resource_kind",
        ),
        "pk": ("identifier",),
    },
    "Interim_ADR_Records": {
        "columns": (
            "record_id",
            "motivating_requirement",
            "motivating_criterion",
            "observable_behavior",
            "recorded_at",
            "backlog_adr_id",
            "resolved_by_adr_id",
            "resolved_at",
        ),
        "pk": ("record_id",),
    },
    "Disclosure_Policies": {
        "columns": (
            "policy_id",
            "policy_name",
            "ruleset_json",
            "effective_start",
            "superseded_by",
        ),
        "pk": ("policy_id",),
    },
    "Disclosure_Policy_Coverage": {
        "columns": (
            "policy_id",
            "node_kind",
            "recorded_at",
            "backlog_adr_id",
        ),
        "pk": ("policy_id", "node_kind"),
    },
    "Role_Assignments": {
        "columns": (
            "role_assignment_id",
            "party_id",
            "role_name",
            "scope",
            "authorities_granted",
            "effective_start",
            "effective_end",
            "revoked_at",
            "assigning_authority_id",
            "recorded_at",
        ),
        "pk": ("role_assignment_id",),
    },
    "Decisions": {
        "columns": (
            "decision_id",
            "target_recommendation_id",
            "target_recommendation_revision_id",
            "outcome",
            "rationale",
            "deciding_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("decision_id",),
    },
    "Document_Revisions": {
        "columns": (
            "revision_id",
            "resource_id",
            "parent_revision_id",
            "content_bytes",
            "content_digest_sha256",
            "contributing_party_id",
            "recorded_at",
            "change_description",
        ),
        "pk": ("revision_id",),
    },
    "Region_Occurrences": {
        "columns": (
            "region_id",
            "document_revision_id",
            "start_offset_bytes",
            "end_offset_bytes",
            "span_byte_length",
            "span_content_digest_sha256",
            "recorded_at",
        ),
        "pk": ("region_id", "document_revision_id"),
    },
    "Finding_Revisions": {
        "columns": (
            "finding_revision_id",
            "finding_id",
            "parent_revision_id",
            "statement",
            "is_hypothesis",
            "authoring_party_id",
            "assumptions_json",
            "confidence_note",
            "recorded_at",
        ),
        "pk": ("finding_revision_id",),
    },
    "Recommendation_Revisions": {
        "columns": (
            "recommendation_revision_id",
            "recommendation_id",
            "parent_revision_id",
            "rationale",
            "assumptions_json",
            "confidence",
            "authoring_party_id",
            "recorded_at",
        ),
        "pk": ("recommendation_revision_id",),
    },
    "Relationships": {
        "columns": (
            "relationship_id",
            "relationship_type",
            "source_kind",
            "source_id",
            "source_revision_id",
            "target_kind",
            "target_id",
            "target_revision_id",
            "authoring_party_id",
            "recorded_at",
            "semantic_role",
        ),
        "pk": ("relationship_id",),
    },
    "Trail_Revisions": {
        "columns": (
            "trail_revision_id",
            "trail_id",
            "predecessor_revision_id",
            "purpose",
            "audience_id",
            "ordering_rationale",
            "authoring_party_id",
            "recorded_at",
        ),
        "pk": ("trail_revision_id",),
    },
    "Trail_Steps": {
        "columns": (
            "trail_step_id",
            "trail_revision_id",
            "ordinal",
            "selection_mode",
            "target_kind",
            "target_id",
            "target_revision_id",
            "region_id",
            "annotation",
        ),
        "pk": ("trail_step_id",),
    },
    "Provenance_Manifests": {
        "columns": (
            "manifest_id",
            "subject_kind",
            "subject_id",
            "subject_revision_id",
            "authoring_party_id",
            "recorded_at",
            "included_sources_json",
            "is_complete",
        ),
        "pk": ("manifest_id",),
    },
    # ----- Slice 2 tables -----
    "Objective_Revisions": {
        "columns": (
            "objective_revision_id",
            "objective_id",
            "parent_revision_id",
            "statement",
            "rationale",
            "target_decision_id",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("objective_revision_id",),
    },
    "Intended_Outcome_Revisions": {
        "columns": (
            "intended_outcome_revision_id",
            "intended_outcome_id",
            "parent_revision_id",
            "outcome_kind",
            "target_objective_id",
            "success_condition",
            "observation_window",
            "attribution_assumption",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("intended_outcome_revision_id",),
    },
    "Project_Revisions": {
        "columns": (
            "project_revision_id",
            "project_id",
            "parent_revision_id",
            "name",
            "summary",
            "target_objective_id",
            "planned_start_date",
            "planned_end_date",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("project_revision_id",),
    },
    "Deliverable_Expectation_Revisions": {
        "columns": (
            "deliverable_expectation_revision_id",
            "deliverable_expectation_id",
            "parent_revision_id",
            "target_project_id",
            "name",
            "description",
            "deliverable_kind",
            "acceptance_criteria",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("deliverable_expectation_revision_id",),
    },
    "Activity_Plans": {
        "columns": (
            "activity_plan_id",
            "target_project_id",
            "title",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("activity_plan_id",),
    },
    "Plan_Revisions": {
        "columns": (
            "plan_revision_id",
            "activity_plan_id",
            "predecessor_revision_id",
            "lifecycle_state",
            "planned_scope",
            "deliverable_expectation_refs_json",
            "planning_assumptions_json",
            "ordering_rationale",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("plan_revision_id",),
    },
    "Plan_Review_Revisions": {
        "columns": (
            "plan_review_revision_id",
            "plan_review_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "reviewing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("plan_review_revision_id",),
    },
    "Plan_Approval_Records": {
        "columns": (
            "plan_approval_id",
            "target_activity_plan_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "approving_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("plan_approval_id",),
    },
    # ----- Slice 3 tables -----
    "Work_Assignment_Records": {
        "columns": (
            "work_assignment_id",
            "target_plan_revision_id",
            "assignee_party_id",
            "assignment_authority_party_id",
            "assignment_rationale",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("work_assignment_id",),
    },
    "Work_Event_Records": {
        "columns": (
            "work_event_id",
            "target_work_assignment_id",
            "event_kind",
            "event_note",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("work_event_id",),
    },
    "Time_Entry_Records": {
        "columns": (
            "time_entry_id",
            "target_work_assignment_id",
            "effort_hours",
            "effort_period_start",
            "effort_period_end",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("time_entry_id",),
    },
    "Deliverable_Production_Records": {
        "columns": (
            "deliverable_production_id",
            "source_work_assignment_id",
            "produced_deliverable_id",
            "produced_deliverable_revision_id",
            "target_deliverable_expectation_id",
            "target_deliverable_expectation_revision_id",
            "production_rationale",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("deliverable_production_id",),
    },
    "Milestone_Acceptance_Records": {
        "columns": (
            "milestone_acceptance_id",
            "source_deliverable_production_id",
            "produced_deliverable_id",
            "produced_deliverable_revision_id",
            "target_deliverable_expectation_id",
            "target_deliverable_expectation_revision_id",
            "outcome",
            "rationale",
            "accepting_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("milestone_acceptance_id",),
    },
    "Completion_Records": {
        "columns": (
            "completion_id",
            "target_plan_revision_id",
            "target_activity_plan_id",
            "target_project_id",
            "outcome",
            "rationale",
            "source_milestone_acceptance_ids_json",
            "completing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("completion_id",),
    },
    "Deliverable_Resources": {
        "columns": (
            "deliverable_id",
            "produced_deliverable_name",
            "created_at",
        ),
        "pk": ("deliverable_id",),
    },
    "Deliverable_Revisions": {
        "columns": (
            "deliverable_revision_id",
            "deliverable_id",
            "content_type",
            "content_bytes",
            "content_digest_sha256",
            "role_marker",
            "originating_work_assignment_id",
            "authoring_party_id",
            "recorded_at",
        ),
        "pk": ("deliverable_revision_id",),
    },
}


# Tables that legitimately *grow* under Slice 4 actions — the property's
# explicit additive exceptions (AD-WS-33 / AD-WS-34 / AD-WS-37 and the new
# Slice 4 ``Relationships`` rows). For these the comparison helper checks
# only the captured baseline primary keys: pre-existing prior-slice rows must
# stay byte-equivalent, while new Slice 4 rows are permitted.
_GROWTH_TABLES: Final[frozenset[str]] = frozenset(
    {
        "Audit_Records",
        "Identifier_Registry",
        "Relationships",
        "Disclosure_Policy_Coverage",
        "Role_Assignments",
    }
)


# ---------------------------------------------------------------------------
# Snapshot + comparison helpers.
# ---------------------------------------------------------------------------


def _snapshot(engine: Engine) -> dict[str, dict[tuple[Any, ...], dict[str, Any]]]:
    """Capture ``{table: {pk_tuple: {column: value}}}`` for every protected table."""
    snapshot: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    with engine.connect() as conn:
        for table, spec in _PROTECTED_TABLE_SPECS.items():
            columns = spec["columns"]
            pk = spec["pk"]
            rows = (
                conn.execute(
                    text(f"SELECT {', '.join(columns)} FROM {table}")
                )
                .mappings()
                .all()
            )
            table_map: dict[tuple[Any, ...], dict[str, Any]] = {}
            for row in rows:
                key = tuple(row[col] for col in pk)
                table_map[key] = {col: row[col] for col in columns}
            snapshot[table] = table_map
    return snapshot


def _assert_unchanged(
    baseline: dict[str, dict[tuple[Any, ...], dict[str, Any]]],
    engine: Engine,
    *,
    observation: str,
) -> None:
    """Assert every protected prior-slice row is byte-equivalent to baseline.

    Growth tables are compared on the captured baseline primary-key set only
    (new Slice 4 rows are the property's explicit exceptions); every other
    table is compared for exact equality so an erroneous INSERT, DELETE, or
    UPDATE on a prior-slice row surfaces immediately.
    """
    current = _snapshot(engine)
    for table in _PROTECTED_TABLE_SPECS:
        base_map = baseline[table]
        cur_map = current[table]
        if table in _GROWTH_TABLES:
            for key, row in base_map.items():
                assert key in cur_map, (
                    f"[{observation}] prior-slice row vanished from {table}: "
                    f"pk={key}"
                )
                assert cur_map[key] == row, (
                    f"[{observation}] prior-slice row mutated in {table}: "
                    f"pk={key}; before={row}; after={cur_map[key]}"
                )
        else:
            assert cur_map == base_map, (
                f"[{observation}] prior-slice table {table} changed; "
                f"before={base_map}; after={cur_map}"
            )


# ---------------------------------------------------------------------------
# Hypothesis strategy — one fully-valid Slice 4 action plan per case.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)
_IDENT_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_."
)
_ASSESSMENT_CATEGORIES: Final[tuple[str, ...]] = (
    "Satisfied",
    "Partially_Satisfied",
    "Not_Satisfied",
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


def _decimal_strategy() -> st.SearchStrategy[Decimal]:
    """Draw an observed value with at most six fractional digits (Requirement 45)."""
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
def _action_plan_strategy(draw) -> dict[str, Any]:
    """Draw one fully-valid Slice 4 action plan."""
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
    if stance in ("Asserted", "Contradicted"):
        evidence = draw(
            st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=80)
        )
    else:
        evidence = draw(
            st.text(alphabet=_TEXT_ALPHABET, min_size=0, max_size=80)
        )

    return {
        "include_denied_attempt": draw(st.booleans()),
        "include_imported_record": draw(st.booleans()),
        "measurand_description": draw(
            st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=80)
        ),
        "assessment_summary": draw(
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
    }


# ---------------------------------------------------------------------------
# Property test.
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(plan=_action_plan_strategy())
def test_slice123_non_modification_under_slice4_actions(
    plan: dict[str, Any],
) -> None:
    """Property 57 — prior-slice rows stay byte-equivalent at every Slice 4
    observation point.

    **Validates: Requirements 46.8, 47.8, 48.7, 49.8, 53.1, 53.5, 54.5,
    60.1, 60.2, 60.3, 60.4, 61.12**
    """
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop57_") as raw:
        engine = _build_engine(Path(raw))
        try:
            svc = _Services()
            intended_outcome_revision_id = _seed_world(svc, engine)

            # Ground truth: every protected prior-slice table before any
            # Slice 4 action runs.
            baseline = _snapshot(engine)

            # -- Optional denied Measurement Definition attempt (Req 53.5).
            # An unauthorized Party's attempt appends a Denial Record (an
            # additive Audit_Records row) and must leave every prior-slice
            # row byte-equivalent. Run first so the uniqueness pre-check
            # cannot mask the authorization denial.
            if plan["include_denied_attempt"]:
                with engine.begin() as conn:
                    try:
                        svc.definitions.create_measurement_definition(
                            conn,
                            target_intended_outcome_revision_id=(
                                intended_outcome_revision_id
                            ),
                            measurand_description="Unauthorized attempt.",
                            unit_of_measure=_UNIT,
                            observation_window=_WINDOW_2025,
                            cadence="monthly",
                            data_source="unauthorized source",
                            authoring_party_id=_AUTHORING_PARTY_ID,
                            applicable_scope=_SCOPE,
                            engine=engine,
                        )
                        raise AssertionError(
                            "expected the unauthorized attempt to be denied"
                        )
                    except MeasurementDefinitionAuthorizationError:
                        pass
                _assert_unchanged(
                    baseline, engine, observation="after-denied-definition"
                )

            # -- Measurement Definition (authorized).
            with engine.begin() as conn:
                definition = svc.definitions.create_measurement_definition(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    measurand_description=plan["measurand_description"],
                    unit_of_measure=_UNIT,
                    observation_window=_WINDOW_2025,
                    cadence="monthly",
                    data_source="product analytics",
                    authoring_party_id=_DEFINER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_unchanged(
                baseline, engine, observation="after-measurement-definition"
            )

            definition_revision_id = (
                definition.measurement_definition_revision_id
            )

            # -- Native Measurement Record.
            with engine.begin() as conn:
                native = svc.records.create_native_measurement(
                    conn,
                    target_measurement_definition_revision_id=(
                        definition_revision_id
                    ),
                    observed_value=plan["native_value"],
                    observed_value_unit=_UNIT,
                    observation_time=plan["native_observation_time"],
                    recording_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_unchanged(
                baseline, engine, observation="after-native-record"
            )

            cited_record_ids = [native.measurement_record_id]

            # -- Optional imported Measurement Record.
            if plan["include_imported_record"]:
                with engine.begin() as conn:
                    imported = svc.records.create_imported_measurement(
                        conn,
                        target_measurement_definition_revision_id=(
                            definition_revision_id
                        ),
                        observed_value=plan["imported_value"],
                        observed_value_unit=_UNIT,
                        observation_time=plan["imported_observation_time"],
                        source_system_id=plan["source_system_id"],
                        source_system_record_id=plan["source_system_record_id"],
                        source_system_authority=plan["source_system_authority"],
                        source_system_retrieval_time=(
                            plan["imported_retrieval_time"]
                        ),
                        importing_party_id=_RECORDER_PARTY_ID,
                        applicable_scope=_SCOPE,
                        engine=engine,
                    )
                cited_record_ids.append(imported.measurement_record_id)
                _assert_unchanged(
                    baseline, engine, observation="after-imported-record"
                )

            # -- Observed Outcome Revision.
            with engine.begin() as conn:
                observed = svc.observed.create_observed_outcome(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    assessment_summary=plan["assessment_summary"],
                    cited_measurement_record_ids=cited_record_ids,
                    authoring_party_id=_ASSESSOR_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_unchanged(
                baseline, engine, observation="after-observed-outcome"
            )

            # -- Success-Condition Assessment.
            with engine.begin() as conn:
                assessment = svc.assessments.create_assessment(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    sourced_observed_outcome_revision_id=(
                        observed.observed_outcome_revision_id
                    ),
                    assessment_category=plan["assessment_category"],
                    assessment_rationale=(
                        "Measured adoption met the success threshold."
                    ),
                    assessing_party_id=_ASSESSOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_unchanged(
                baseline, engine, observation="after-assessment"
            )

            # -- Outcome Review citing the Assessment, the Slice 3 Completion,
            #    and the produced Slice 3 Deliverable Revision.
            with engine.begin() as conn:
                svc.reviews.create_outcome_review(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    review_outcome=plan["review_outcome"],
                    attribution_stance=plan["attribution_stance"],
                    confidence=plan["confidence"],
                    review_rationale="Reviewed evidence and concluded.",
                    attribution_evidence_reference=(
                        plan["attribution_evidence_reference"]
                    ),
                    cited_assessment_ids=[assessment.assessment_id],
                    cited_completion_ids=[_CITABLE_COMPLETION_ID],
                    cited_produced_deliverable_revision_ids=[
                        _CITABLE_DELIVERABLE_REVISION_ID
                    ],
                    reviewing_party_id=_REVIEWER_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id=None,
                    request_attributes=None,
                )
            _assert_unchanged(
                baseline, engine, observation="after-outcome-review"
            )

            # -- Final whole-snapshot check after the complete sequence so a
            #    regression that only manifests at end-of-pipeline surfaces.
            _assert_unchanged(
                baseline, engine, observation="end-of-pipeline"
            )
        finally:
            engine.dispose()
