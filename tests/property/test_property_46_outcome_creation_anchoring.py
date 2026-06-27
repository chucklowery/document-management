# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
"""Property 46 — Intended-Outcome anchoring and creation success (task 15.1).

**Property 46: Intended-Outcome anchoring and creation success**

*For any* authorized outcome-measurement creation request (Measurement
Definition, native or imported Measurement Record, Observed Outcome
Revision, Success-Condition Assessment, or Outcome Review) that passes
input validation and authority checks, exactly one Resource and/or
Revision and/or Record row, exactly one consequential ``Audit_Records``
row, and the prescribed ``Addresses`` and ``Cites`` Relationship rows per
AD-WS-35 are persisted in one transaction with byte-equivalent recorded
times; and every Measurement Definition Revision, Observed Outcome
Revision, Success-Condition Assessment, and Outcome Review has exactly one
``Addresses`` Relationship to an Intended Outcome Revision Identity that
resolves in the Planning_Service with ``outcome_kind = 'intended'``. No
Measurement Definition Revision, Observed Outcome Revision,
Success-Condition Assessment, or Outcome Review exists without a matching
``intended`` Intended Outcome Revision.

**Validates: Requirements 44.1, 44.2, 45.1, 45.2, 46.1, 46.2, 47.1, 47.2,
48.1, 48.2, 49.1, 49.2, 61.1**

Strategy
========

Six independent property tests, one per Outcome_Service creation request
body, each driven by a Hypothesis strategy that generates a *valid* request
payload for that endpoint:

- :func:`test_measurement_definition_creation_anchors_and_persists`
  exercises
  :meth:`MeasurementDefinitionService.create_measurement_definition`
  against a seeded ``intended`` Intended Outcome Revision. AD-WS-35
  prescribes one ``Addresses`` Relationship to the target Intended
  Outcome Revision (``semantic_role IS NULL``).
- :func:`test_native_measurement_record_creation_persists` exercises
  :meth:`MeasurementRecordService.create_native_measurement` against a
  seeded Measurement Definition. AD-WS-35 prescribes one ``Cites``
  Relationship to the target Measurement Definition Revision
  (``semantic_role = 'measurement_basis'``).
- :func:`test_imported_measurement_record_creation_persists` exercises
  :meth:`MeasurementRecordService.create_imported_measurement` against the
  same seeded Definition with the source-system authority drawn across the
  enumerated set.
- :func:`test_observed_outcome_creation_anchors_and_persists` exercises
  :meth:`ObservedOutcomeService.create_observed_outcome`. AD-WS-35
  prescribes one ``Addresses`` Relationship to the target Intended Outcome
  Revision (``semantic_role IS NULL``) and one ``Cites`` Relationship per
  cited Measurement Record (``semantic_role = 'observation_basis'``).
- :func:`test_success_condition_assessment_creation_anchors_and_persists`
  exercises
  :meth:`SuccessConditionAssessmentService.create_assessment`. AD-WS-35
  prescribes one ``Addresses`` Relationship to the target Intended Outcome
  Revision (``semantic_role IS NULL``) and one ``Cites`` Relationship to
  the sourced Observed Outcome Revision
  (``semantic_role = 'assessment_basis'``).
- :func:`test_outcome_review_creation_anchors_and_persists` exercises
  :meth:`OutcomeReviewService.create_outcome_review`. AD-WS-35 prescribes
  one ``Addresses`` Relationship to the target Intended Outcome Revision
  (``semantic_role IS NULL``), one ``Cites`` Relationship per cited
  Success-Condition Assessment (``semantic_role = 'review_assessment'``),
  per cited Completion Record (``semantic_role = 'review_completion'``),
  and per cited produced Deliverable Revision
  (``semantic_role = 'review_deliverable'``).

Per Hypothesis case, each test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case identifier,
   audit, and resource state cannot leak. The engine carries Slice 1,
   Slice 2 (Planning), Slice 3 (Execution + Deliverable_Repository — only
   the Outcome Review test consults these), and Slice 4 (Outcome)
   schemas.
2. Seeds the actor Parties and the role grants needed by the action under
   test plus the deterministic prerequisite chain. The prerequisite chain
   is built through the *real* Slice 2 / Slice 4 services with fixed valid
   inputs so the anchoring relationships exist exactly as production would
   write them; only the entity under test draws its fields from Hypothesis.
3. Invokes the create method with the Hypothesis-drawn body inside one
   ``engine.begin()`` block so the AD-WS-5 "audit-and-write atomic"
   contract participates in the test.
4. Asserts the invariants of Property 46:
   - **Row count** — exactly one row in the target Slice 4 Record /
     Revision table (and, for the Resource/Revision kinds, exactly one
     Resource header row).
   - **Consequential audit count** — exactly one ``Audit_Records`` row
     with ``outcome='consequential'`` and the action_type for the kind
     under test, naming the created entity as its ``target_id``.
   - **Relationship rows** — exactly the prescribed AD-WS-35 ``Addresses``
     / ``Cites`` Relationship rows sourced from the new entity with the
     exact ``relationship_type`` and ``semantic_role`` values.
   - **Byte-equivalent recorded times** — the persisted domain row's
     ``recorded_at``, every prescribed Relationship row's ``recorded_at``,
     and the consequential audit row's ``recorded_at`` are all the same
     string.
   - **Intended-Outcome anchor** — for the four anchoring kinds
     (Measurement Definition Revision, Observed Outcome Revision,
     Success-Condition Assessment, Outcome Review) the single ``Addresses``
     Relationship targets an Intended Outcome Revision that resolves in the
     Planning_Service with ``outcome_kind = 'intended'``, and no such
     entity exists in the database without that matching ``intended``
     Revision.

Setup follows the conventions established by the Slice 1/2/3 property tests
(per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite file,
fresh services per case so :class:`IdentityService` in-memory state cannot
bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps), and reuses the
chain-builder helper style from the Slice 4 unit tests
(``tests/unit/test_outcome_observed_outcomes.py`` and
``tests/unit/test_outcome_reviews.py``). The authorization permit path is
exercised by granting the precise required authority rather than by swapping
in a stub, so the real evaluation code path participates.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
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
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import create_outcome_schema
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


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and constants — the deterministic prerequisite chain.
# Property 46 only asserts the cardinality / linkage / timestamp / anchor
# invariants, so deterministic prerequisite IDs keep shrunken
# counterexamples actionable; the entity under test draws its fields from
# Hypothesis.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_OWNER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00005"
_REVIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00006"
_COMPLETING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00007"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00008"

_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID: Final[str] = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-000000c00003"
_SCOPE: Final[str] = "pilot/team-a"

_AUTHORITY_BASIS_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000ba001")
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Directly-seeded citable Slice 3 artifacts (Outcome Review test only).
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000d00004"
)

_UNIT: Final[str] = "percent"
# ISO-8601 closed window covering the 2025 observation instants the record
# strategies draw; both edges precede the fixed recorded time (2026-01-01).
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"

_CANONICAL_UUID7: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_SOURCE_SYSTEM_AUTHORITIES: Final[tuple[str, ...]] = (
    "authoritative",
    "replica",
    "projection",
    "index",
    "federation",
)


# ---------------------------------------------------------------------------
# Per-case engine builder. Each Hypothesis case builds a fresh SQLite engine
# on a unique temp-dir path so cross-case state cannot leak between generated
# inputs; the engine carries every schema the Outcome_Service may consult.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
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
    """Per-case bundle of every collaborator the chain builders need.

    Fresh per Hypothesis case so :class:`IdentityService` in-memory state
    cannot bleed across shrinks; the denial-audit sleep on the deny-path
    services is a no-op so the (unused on the permit path) retry sequence
    never spends real time.
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
# Seed helpers (mirror tests/unit/test_outcome_*.py).
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


def _seed_objective(engine: Engine) -> None:
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
                "ts": _NOW_ISO,
            },
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


def _seed_world(svc: _Services, engine: Engine) -> None:
    """Seed every Party, the Objective, and all role grants the chain needs."""
    with engine.begin() as conn:
        _seed_party(conn, _OWNER_PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
        _seed_party(conn, _DEFINER_PARTY_ID, "Measurement Definer")
        _seed_party(conn, _RECORDER_PARTY_ID, "Measurement Recorder")
        _seed_party(conn, _ASSESSOR_PARTY_ID, "Outcome Assessor")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
    _seed_objective(engine)
    for party_id, role_name, authority in (
        (_OWNER_PARTY_ID, "intended_outcome_owner", "modify"),
        (_DEFINER_PARTY_ID, "measurement_definer", "define_measurement"),
        (_RECORDER_PARTY_ID, "measurement_recorder", "record_measurement"),
        (_ASSESSOR_PARTY_ID, "outcome_assessor", "assess_outcome"),
        (_REVIEWER_PARTY_ID, "outcome_reviewer", "issue_outcome_review"),
    ):
        _assign_role(
            svc.authz,
            engine,
            party_id=party_id,
            role_name=role_name,
            authority=authority,
        )


def _seed_intended_outcome(svc: _Services, engine: Engine) -> tuple[str, str]:
    """Create one Intended Outcome (``outcome_kind = 'intended'``) via the real
    Slice 2 service; return ``(intended_outcome_id, revision_id)``."""
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
    return intended.intended_outcome_id, intended.intended_outcome_revision_id


def _seed_definition(
    svc: _Services, engine: Engine, *, intended_outcome_revision_id: str
) -> str:
    """Create the single Measurement Definition addressing the Intended Outcome;
    return its Revision Identity (the target for Measurement Records)."""
    with engine.begin() as conn:
        definition = svc.definitions.create_measurement_definition(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            measurand_description="Adoption rate of the new workflow.",
            unit_of_measure=_UNIT,
            observation_window=_WINDOW_2025,
            cadence="monthly",
            data_source="product analytics",
            authoring_party_id=_DEFINER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return definition.measurement_definition_revision_id


def _seed_native_record(
    svc: _Services, engine: Engine, *, definition_revision_id: str
) -> str:
    """Record one citable native Measurement Record; return its Identity."""
    with engine.begin() as conn:
        result = svc.records.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=definition_revision_id,
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.measurement_record_id


def _seed_observed_outcome(
    svc: _Services,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    cited_record_ids: list[str],
) -> str:
    """Create one Observed Outcome Revision; return its Revision Identity."""
    with engine.begin() as conn:
        result = svc.observed.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=cited_record_ids,
            authoring_party_id=_ASSESSOR_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.observed_outcome_revision_id


def _seed_assessment(
    svc: _Services,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    sourced_observed_outcome_revision_id: str,
) -> str:
    """Create one Success-Condition Assessment Record; return its Identity."""
    with engine.begin() as conn:
        result = svc.assessments.create_assessment(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            assessment_category="Satisfied",
            assessment_rationale="Measured adoption met the success threshold.",
            assessing_party_id=_ASSESSOR_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.assessment_id


def _seed_citable_completion(engine: Engine) -> str:
    """Seed one resolvable Completion Record by direct INSERT (Review test)."""
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
                "ts": _NOW_ISO,
            },
        )
    return _CITABLE_COMPLETION_ID


def _seed_citable_deliverable_revision(engine: Engine) -> str:
    """Seed one resolvable produced Deliverable Revision by direct INSERT."""
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
                "ts": _NOW_ISO,
            },
        )
    return _CITABLE_DELIVERABLE_REVISION_ID


# ---------------------------------------------------------------------------
# Probe helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _consequential_audit_rows(
    engine: Engine, *, action_type: str
) -> list[dict[str, Any]]:
    """Return every ``outcome='consequential'`` audit row of one action.

    The authorization evaluation row written by
    :meth:`AuthorizationService.evaluate` carries ``outcome='permit'`` (not
    ``'consequential'``) and is naturally excluded.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT actor_party_id, action_type, outcome,
                           target_id, target_revision_id,
                           correlation_id, recorded_at
                      FROM Audit_Records
                     WHERE outcome = 'consequential'
                       AND action_type = :a
                    """
                ),
                {"a": action_type},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _relationship_row(engine: Engine, relationship_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type,
                           source_kind, source_id, source_revision_id,
                           target_kind, target_id, target_revision_id,
                           semantic_role, recorded_at
                      FROM Relationships
                     WHERE relationship_id = :rid
                    """
                ),
                {"rid": relationship_id},
            )
            .mappings()
            .one()
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


def _single_column(
    engine: Engine,
    *,
    table: str,
    column: str,
    id_column: str,
    id_value: str,
) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(f"SELECT {column} FROM {table} WHERE {id_column} = :i"),
                {"i": id_value},
            ).scalar_one()
        )


def _assert_resolves_intended(
    svc: _Services,
    engine: Engine,
    intended_outcome_revision_id: str,
) -> None:
    """Assert the named Intended Outcome Revision resolves in the
    Planning_Service with ``outcome_kind = 'intended'`` (the Property 46
    anchor invariant)."""
    with engine.connect() as conn:
        row = svc.intended.get_revision(conn, intended_outcome_revision_id)
    assert row is not None, (
        "anchor Intended Outcome Revision did not resolve in the "
        "Planning_Service"
    )
    assert row.outcome_kind == "intended", (
        f"anchor resolved with outcome_kind={row.outcome_kind!r}, "
        "expected 'intended'"
    )


# ---------------------------------------------------------------------------
# Strategies. Text generators are restricted to a narrow printable alphabet
# so generated content round-trips through SQLite's UTF-8 TEXT columns
# without escape ambiguity; each strategy stays inside the per-attribute
# length range named by the design surface and re-enforced by the schema
# CHECK constraints.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


# Measurement Definition — Requirement 44.2 (ranges narrowed for readability).
_measurement_definition_strategy = st.fixed_dictionaries(
    {
        "measurand_description": _bounded_text(1, 200),
        "unit_of_measure": _bounded_text(1, 50),
        "observation_window": _bounded_text(1, 200),
        "cadence": _bounded_text(1, 200),
        "data_source": _bounded_text(1, 200),
    }
)


# Observed value with at most six fractional digits (Requirement 45.2).
_observed_value_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


# Observation instants strictly inside the 2025 window and before the fixed
# recorded time (2026-01-01).
def _utc_datetime(start: datetime, end: datetime) -> st.SearchStrategy[datetime]:
    return st.datetimes(min_value=start, max_value=end).map(
        lambda dt: dt.replace(tzinfo=timezone.utc, microsecond=0)
    )


_native_record_strategy = st.fixed_dictionaries(
    {
        "observed_value": _observed_value_strategy,
        "observation_time": _utc_datetime(
            datetime(2025, 1, 1), datetime(2025, 12, 30)
        ),
    }
)


@st.composite
def _imported_record_payload(draw: Any) -> dict[str, Any]:
    """Imported Measurement Record — Requirement 46.

    Draws observation ≤ retrieval, both strictly inside the 2025 window and
    before the fixed recorded time, plus a source-system authority across the
    enumerated set and non-empty source-system identifiers.
    """
    observation = draw(
        _utc_datetime(datetime(2025, 1, 1), datetime(2025, 6, 30))
    )
    retrieval = draw(
        _utc_datetime(datetime(2025, 7, 1), datetime(2025, 12, 30))
    )
    return {
        "observed_value": draw(_observed_value_strategy),
        "observation_time": observation,
        "source_system_retrieval_time": retrieval,
        "source_system_authority": draw(
            st.sampled_from(_SOURCE_SYSTEM_AUTHORITIES)
        ),
        "source_system_id": draw(_bounded_text(1, 60)),
        "source_system_record_id": draw(_bounded_text(1, 60)),
    }


_observed_outcome_strategy = st.fixed_dictionaries(
    {
        "assessment_summary": _bounded_text(1, 200),
    }
)


@st.composite
def _assessment_payload(draw: Any) -> dict[str, Any]:
    """Success-Condition Assessment — Requirement 48.

    Draws a category from the closed enumeration; the rationale honors the
    Unassessable ``>= 200``-char rule and the 1..4000 range otherwise.
    """
    category = draw(
        st.sampled_from(
            ["Satisfied", "Partially_Satisfied", "Not_Satisfied", "Unassessable"]
        )
    )
    if category == "Unassessable":
        rationale = draw(_bounded_text(200, 400))
    else:
        rationale = draw(_bounded_text(1, 200))
    return {"assessment_category": category, "assessment_rationale": rationale}


@st.composite
def _outcome_review_payload(draw: Any) -> dict[str, Any]:
    """Outcome Review — Requirement 49.

    Draws the three enumerations and a review rationale; the
    attribution-evidence reference is always non-empty so the Asserted /
    Contradicted stances satisfy Requirement 49.4 (the other two stances
    accept a non-empty reference as well).
    """
    return {
        "review_outcome": draw(
            st.sampled_from(
                ["Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"]
            )
        ),
        "attribution_stance": draw(
            st.sampled_from(
                ["Asserted", "Partial", "Unattributed", "Contradicted"]
            )
        ),
        "confidence": draw(st.sampled_from(["High", "Moderate", "Low"])),
        "review_rationale": draw(_bounded_text(1, 200)),
        "attribution_evidence_reference": draw(_bounded_text(1, 200)),
    }


# ===========================================================================
# Property 46 — the six creation-body tests.
# ===========================================================================


# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
@given(payload=_measurement_definition_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_measurement_definition_creation_anchors_and_persists(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Measurement Definition request: exactly one
    Resource + one initial Revision + one consequential audit row, one
    ``Addresses`` Relationship to an ``intended`` Intended Outcome Revision
    (``semantic_role IS NULL``), byte-equivalent recorded times, and the
    anchor resolves with ``outcome_kind = 'intended'``."""
    with tempfile.TemporaryDirectory(prefix="prop46_md_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)

            with engine.begin() as conn:
                result = svc.definitions.create_measurement_definition(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    measurand_description=payload["measurand_description"],
                    unit_of_measure=payload["unit_of_measure"],
                    observation_window=payload["observation_window"],
                    cadence=payload["cadence"],
                    data_source=payload["data_source"],
                    authoring_party_id=_DEFINER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.measurement_definition_id)
            assert _CANONICAL_UUID7.match(
                result.measurement_definition_revision_id
            )
            assert _count(engine, "Measurement_Definitions") == 1
            assert _count(engine, "Measurement_Definition_Revisions") == 1

            audit_rows = _consequential_audit_rows(
                engine, action_type="create.measurement_definition"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.measurement_definition_id
            assert audit_row["actor_party_id"] == _DEFINER_PARTY_ID

            # Exactly one Addresses Relationship sourced from the Definition.
            assert (
                _count_relationships(
                    engine,
                    rel_type="Addresses",
                    source_id=result.measurement_definition_id,
                )
                == 1
            )
            addresses = _relationship_row(
                engine, result.addresses_relationship_id
            )
            assert addresses["relationship_type"] == "Addresses"
            assert addresses["semantic_role"] is None
            assert addresses["target_kind"] == "intended_outcome_revision"
            assert addresses["target_revision_id"] == intended_rev
            assert (
                addresses["source_revision_id"]
                == result.measurement_definition_revision_id
            )

            # Byte-equivalent recorded times across Revision + Relationship +
            # consequential audit row.
            revision_recorded_at = _single_column(
                engine,
                table="Measurement_Definition_Revisions",
                column="recorded_at",
                id_column="measurement_definition_revision_id",
                id_value=result.measurement_definition_revision_id,
            )
            assert revision_recorded_at == addresses["recorded_at"]
            assert revision_recorded_at == audit_row["recorded_at"]
            assert revision_recorded_at == result.recorded_at

            # Anchor resolves with outcome_kind = 'intended'.
            _assert_resolves_intended(svc, engine, intended_rev)
        finally:
            engine.dispose()


# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
@given(payload=_native_record_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_native_measurement_record_creation_persists(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid native Measurement Record request: exactly one
    Record row + one consequential audit row + one ``Cites`` Relationship to
    the target Measurement Definition Revision
    (``semantic_role = 'measurement_basis'``) with byte-equivalent recorded
    times."""
    with tempfile.TemporaryDirectory(prefix="prop46_native_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            definition_rev = _seed_definition(
                svc, engine, intended_outcome_revision_id=intended_rev
            )

            with engine.begin() as conn:
                result = svc.records.create_native_measurement(
                    conn,
                    target_measurement_definition_revision_id=definition_rev,
                    observed_value=payload["observed_value"],
                    observed_value_unit=_UNIT,
                    observation_time=payload["observation_time"],
                    recording_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.measurement_record_id)
            assert result.origin == "native"
            assert _count(engine, "Measurement_Records") == 1

            audit_rows = _consequential_audit_rows(
                engine, action_type="create.measurement_record"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.measurement_record_id
            assert audit_row["actor_party_id"] == _RECORDER_PARTY_ID

            assert (
                _count_relationships(
                    engine,
                    rel_type="Cites",
                    source_id=result.measurement_record_id,
                )
                == 1
            )
            cites = _relationship_row(engine, result.cites_relationship_id)
            assert cites["relationship_type"] == "Cites"
            assert cites["semantic_role"] == "measurement_basis"
            assert cites["target_kind"] == "measurement_definition_revision"
            assert cites["target_revision_id"] == definition_rev

            record_recorded_at = _single_column(
                engine,
                table="Measurement_Records",
                column="recorded_at",
                id_column="measurement_record_id",
                id_value=result.measurement_record_id,
            )
            assert record_recorded_at == cites["recorded_at"]
            assert record_recorded_at == audit_row["recorded_at"]
            assert record_recorded_at == result.recorded_at
        finally:
            engine.dispose()


# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
@given(payload=_imported_record_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_imported_measurement_record_creation_persists(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid imported Measurement Record request: exactly
    one Record row + one consequential audit row + one ``Cites`` Relationship
    to the target Measurement Definition Revision
    (``semantic_role = 'measurement_basis'``); ``origin = 'imported'`` and the
    source-system authority is surfaced explicitly; byte-equivalent recorded
    times and ``import_at == recorded_at``."""
    with tempfile.TemporaryDirectory(prefix="prop46_imported_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            definition_rev = _seed_definition(
                svc, engine, intended_outcome_revision_id=intended_rev
            )

            with engine.begin() as conn:
                result = svc.records.create_imported_measurement(
                    conn,
                    target_measurement_definition_revision_id=definition_rev,
                    observed_value=payload["observed_value"],
                    observed_value_unit=_UNIT,
                    observation_time=payload["observation_time"],
                    source_system_id=payload["source_system_id"],
                    source_system_record_id=payload["source_system_record_id"],
                    source_system_authority=payload["source_system_authority"],
                    source_system_retrieval_time=(
                        payload["source_system_retrieval_time"]
                    ),
                    importing_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.measurement_record_id)
            assert result.origin == "imported"
            assert (
                result.source_system_authority
                == payload["source_system_authority"]
            )
            assert result.import_at == result.recorded_at
            assert _count(engine, "Measurement_Records") == 1

            audit_rows = _consequential_audit_rows(
                engine, action_type="create.measurement_record"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.measurement_record_id

            assert (
                _count_relationships(
                    engine,
                    rel_type="Cites",
                    source_id=result.measurement_record_id,
                )
                == 1
            )
            cites = _relationship_row(engine, result.cites_relationship_id)
            assert cites["relationship_type"] == "Cites"
            assert cites["semantic_role"] == "measurement_basis"
            assert cites["target_revision_id"] == definition_rev

            record_recorded_at = _single_column(
                engine,
                table="Measurement_Records",
                column="recorded_at",
                id_column="measurement_record_id",
                id_value=result.measurement_record_id,
            )
            assert record_recorded_at == cites["recorded_at"]
            assert record_recorded_at == audit_row["recorded_at"]
            assert record_recorded_at == result.recorded_at
        finally:
            engine.dispose()


# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
@given(payload=_observed_outcome_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_observed_outcome_creation_anchors_and_persists(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Observed Outcome request: exactly one Resource
    + one initial Revision (``outcome_kind = 'observed'``) + one consequential
    audit row, one ``Addresses`` Relationship to an ``intended`` Intended
    Outcome Revision (``semantic_role IS NULL``), one ``Cites`` Relationship per
    cited Measurement Record (``semantic_role = 'observation_basis'``),
    byte-equivalent recorded times, and the anchor resolves ``intended``."""
    with tempfile.TemporaryDirectory(prefix="prop46_oo_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            definition_rev = _seed_definition(
                svc, engine, intended_outcome_revision_id=intended_rev
            )
            record_id = _seed_native_record(
                svc, engine, definition_revision_id=definition_rev
            )

            with engine.begin() as conn:
                result = svc.observed.create_observed_outcome(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    assessment_summary=payload["assessment_summary"],
                    cited_measurement_record_ids=[record_id],
                    authoring_party_id=_ASSESSOR_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.observed_outcome_id)
            assert _CANONICAL_UUID7.match(result.observed_outcome_revision_id)
            assert result.outcome_kind == "observed"
            assert result.predecessor_revision_id is None
            assert _count(engine, "Observed_Outcomes") == 1
            assert _count(engine, "Observed_Outcome_Revisions") == 1

            audit_rows = _consequential_audit_rows(
                engine, action_type="create.observed_outcome"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.observed_outcome_id

            assert (
                _count_relationships(
                    engine,
                    rel_type="Addresses",
                    source_id=result.observed_outcome_id,
                )
                == 1
            )
            assert (
                _count_relationships(
                    engine,
                    rel_type="Cites",
                    source_id=result.observed_outcome_id,
                )
                == 1
            )
            addresses = _relationship_row(
                engine, result.addresses_relationship_id
            )
            assert addresses["relationship_type"] == "Addresses"
            assert addresses["semantic_role"] is None
            assert addresses["target_revision_id"] == intended_rev

            assert len(result.cites_relationship_ids) == 1
            cites = _relationship_row(
                engine, result.cites_relationship_ids[0]
            )
            assert cites["relationship_type"] == "Cites"
            assert cites["semantic_role"] == "observation_basis"
            assert cites["target_id"] == record_id

            revision_recorded_at = _single_column(
                engine,
                table="Observed_Outcome_Revisions",
                column="recorded_at",
                id_column="observed_outcome_revision_id",
                id_value=result.observed_outcome_revision_id,
            )
            assert revision_recorded_at == addresses["recorded_at"]
            assert revision_recorded_at == cites["recorded_at"]
            assert revision_recorded_at == audit_row["recorded_at"]
            assert revision_recorded_at == result.recorded_at

            _assert_resolves_intended(svc, engine, intended_rev)
        finally:
            engine.dispose()


# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
@given(payload=_assessment_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_success_condition_assessment_creation_anchors_and_persists(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Success-Condition Assessment request: exactly
    one Record row + one consequential audit row, one ``Addresses``
    Relationship to an ``intended`` Intended Outcome Revision
    (``semantic_role IS NULL``), one ``Cites`` Relationship to the sourced
    Observed Outcome Revision (``semantic_role = 'assessment_basis'``),
    byte-equivalent recorded times, and the anchor resolves ``intended``."""
    with tempfile.TemporaryDirectory(prefix="prop46_sca_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            definition_rev = _seed_definition(
                svc, engine, intended_outcome_revision_id=intended_rev
            )
            record_id = _seed_native_record(
                svc, engine, definition_revision_id=definition_rev
            )
            observed_rev = _seed_observed_outcome(
                svc,
                engine,
                intended_outcome_revision_id=intended_rev,
                cited_record_ids=[record_id],
            )

            with engine.begin() as conn:
                result = svc.assessments.create_assessment(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    sourced_observed_outcome_revision_id=observed_rev,
                    assessment_category=payload["assessment_category"],
                    assessment_rationale=payload["assessment_rationale"],
                    assessing_party_id=_ASSESSOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.assessment_id)
            assert _count(engine, "Success_Condition_Assessment_Records") == 1

            audit_rows = _consequential_audit_rows(
                engine, action_type="create.success_condition_assessment"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.assessment_id

            assert (
                _count_relationships(
                    engine,
                    rel_type="Addresses",
                    source_id=result.assessment_id,
                )
                == 1
            )
            assert (
                _count_relationships(
                    engine,
                    rel_type="Cites",
                    source_id=result.assessment_id,
                )
                == 1
            )
            addresses = _relationship_row(
                engine, result.addresses_relationship_id
            )
            assert addresses["semantic_role"] is None
            assert addresses["target_revision_id"] == intended_rev
            cites = _relationship_row(engine, result.cites_relationship_id)
            assert cites["semantic_role"] == "assessment_basis"
            assert cites["target_revision_id"] == observed_rev

            record_recorded_at = _single_column(
                engine,
                table="Success_Condition_Assessment_Records",
                column="recorded_at",
                id_column="assessment_id",
                id_value=result.assessment_id,
            )
            assert record_recorded_at == addresses["recorded_at"]
            assert record_recorded_at == cites["recorded_at"]
            assert record_recorded_at == audit_row["recorded_at"]
            assert record_recorded_at == result.recorded_at

            _assert_resolves_intended(svc, engine, intended_rev)
        finally:
            engine.dispose()


# Feature: fourth-walking-slice, Property 46: Intended-Outcome anchoring and creation success
@given(payload=_outcome_review_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_outcome_review_creation_anchors_and_persists(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Outcome Review request: exactly one Record row
    + one consequential audit row, one ``Addresses`` Relationship to an
    ``intended`` Intended Outcome Revision (``semantic_role IS NULL``), one
    ``Cites`` Relationship per cited Success-Condition Assessment
    (``review_assessment``), Completion Record (``review_completion``), and
    produced Deliverable Revision (``review_deliverable``), byte-equivalent
    recorded times, and the anchor resolves ``intended``."""
    with tempfile.TemporaryDirectory(prefix="prop46_review_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            definition_rev = _seed_definition(
                svc, engine, intended_outcome_revision_id=intended_rev
            )
            record_id = _seed_native_record(
                svc, engine, definition_revision_id=definition_rev
            )
            observed_rev = _seed_observed_outcome(
                svc,
                engine,
                intended_outcome_revision_id=intended_rev,
                cited_record_ids=[record_id],
            )
            assessment_id = _seed_assessment(
                svc,
                engine,
                intended_outcome_revision_id=intended_rev,
                sourced_observed_outcome_revision_id=observed_rev,
            )
            completion_id = _seed_citable_completion(engine)
            deliverable_rev = _seed_citable_deliverable_revision(engine)

            with engine.begin() as conn:
                result = svc.reviews.create_outcome_review(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    review_outcome=payload["review_outcome"],
                    attribution_stance=payload["attribution_stance"],
                    confidence=payload["confidence"],
                    review_rationale=payload["review_rationale"],
                    attribution_evidence_reference=(
                        payload["attribution_evidence_reference"]
                    ),
                    cited_assessment_ids=[assessment_id],
                    cited_completion_ids=[completion_id],
                    cited_produced_deliverable_revision_ids=[deliverable_rev],
                    reviewing_party_id=_REVIEWER_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.outcome_review_id)
            assert _count(engine, "Outcome_Review_Records") == 1

            audit_rows = _consequential_audit_rows(
                engine, action_type="create.outcome_review"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.outcome_review_id

            assert (
                _count_relationships(
                    engine,
                    rel_type="Addresses",
                    source_id=result.outcome_review_id,
                )
                == 1
            )
            # Three Cites Relationships: one per cited Assessment / Completion /
            # produced Deliverable Revision.
            assert (
                _count_relationships(
                    engine,
                    rel_type="Cites",
                    source_id=result.outcome_review_id,
                )
                == 3
            )
            addresses = _relationship_row(
                engine, result.addresses_relationship_id
            )
            assert addresses["semantic_role"] is None
            assert addresses["target_revision_id"] == intended_rev

            assert len(result.cites_assessment_relationship_ids) == 1
            assert len(result.cites_completion_relationship_ids) == 1
            assert len(result.cites_deliverable_relationship_ids) == 1
            assessment_cite = _relationship_row(
                engine, result.cites_assessment_relationship_ids[0]
            )
            completion_cite = _relationship_row(
                engine, result.cites_completion_relationship_ids[0]
            )
            deliverable_cite = _relationship_row(
                engine, result.cites_deliverable_relationship_ids[0]
            )
            assert assessment_cite["semantic_role"] == "review_assessment"
            assert assessment_cite["target_id"] == assessment_id
            assert completion_cite["semantic_role"] == "review_completion"
            assert completion_cite["target_id"] == completion_id
            assert deliverable_cite["semantic_role"] == "review_deliverable"
            assert deliverable_cite["target_revision_id"] == deliverable_rev

            record_recorded_at = _single_column(
                engine,
                table="Outcome_Review_Records",
                column="recorded_at",
                id_column="outcome_review_id",
                id_value=result.outcome_review_id,
            )
            assert record_recorded_at == addresses["recorded_at"]
            assert record_recorded_at == assessment_cite["recorded_at"]
            assert record_recorded_at == completion_cite["recorded_at"]
            assert record_recorded_at == deliverable_cite["recorded_at"]
            assert record_recorded_at == audit_row["recorded_at"]
            assert record_recorded_at == result.recorded_at

            _assert_resolves_intended(svc, engine, intended_rev)
        finally:
            engine.dispose()
