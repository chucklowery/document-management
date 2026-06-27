# Feature: fourth-walking-slice, Property 50: Intended/Observed separation enforced from the outcome side
"""Property 50 — Intended/Observed separation enforced from the outcome side (task 15.5).

**Property 50: Intended/Observed separation enforced from the outcome side**

*For all* request bodies submitted to any Outcome_Service endpoint, if the
body contains any field whose name matches a prohibited intended-side prefix
(``success-condition-``, ``attribution-assumption-``, ``planned-``,
``plan-review-``, ``plan-approval-``, ``milestone-acceptance-outcome-``,
``completion-outcome-``, ``intended-``) beyond the explicit ``Addresses`` /
``Cites`` Identity references named in Requirements 44–49, or an
``outcome_kind`` other than ``observed`` on an Observed Outcome Revision,
the request is rejected with no row persisted. No persisted
outcome-measurement entity carries any intended-side attribute as a value on
the row itself; and no Intended Outcome Resource/Revision, Objective,
Project, Deliverable Expectation, Plan Revision, Plan Review Revision, Plan
Approval Record, or any Slice 3 execution Record is mutated as a consequence
of any Slice 4 action.

**Validates: Requirements 53.1, 53.2, 53.3, 53.4, 47.8, 48.7, 61.5**

Strategy
========

Three independent property tests, each driven by a Hypothesis strategy:

- :func:`test_prohibited_intended_side_prefix_rejected_at_every_endpoint`
  draws one of the six Outcome_Service creation endpoints and one
  Hypothesis-built field name whose normalized form begins with one of the
  eight prohibited intended-side prefixes (with case and hyphen/underscore
  variants drawn so the boundary guard's normalization is exercised). The
  endpoint is invoked with an otherwise-valid request body plus the
  prohibited key in ``request_attributes``; the test asserts the create is
  rejected with ``failed_constraint == 'prohibited_attribute'``, the
  offending key surfaces verbatim in ``prohibited_keys`` (Requirement 53.3),
  and the endpoint's target table is left with **zero** rows
  (Requirements 53.2, 53.3). Because the prerequisite chain for each
  endpoint is seeded through the *real* Slice 2 / Slice 4 services with
  valid inputs and the target table starts empty, a zero count after the
  call proves the prohibited key alone caused the rejection.

- :func:`test_non_observed_outcome_kind_rejected` draws an ``outcome_kind``
  value other than the literal ``observed`` and submits it to the Observed
  Outcome creation endpoint with otherwise-valid inputs; the test asserts
  rejection with ``failed_constraint == 'outcome_kind_invalid'`` and zero
  ``Observed_Outcomes`` / ``Observed_Outcome_Revisions`` rows
  (Requirement 53.3, 47.4).

- :func:`test_successful_pipeline_leaves_prior_slice_byte_equivalent` seeds
  the Slice 2 anchor chain (Objective + Intended Outcome) and two citable
  Slice 3 execution artifacts (a Completion Record and a produced
  Deliverable Revision), snapshots every Slice 1 / Slice 2 / Slice 3 table,
  then runs a full *valid* Slice 4 pipeline (Measurement Definition →
  native Measurement Record → Observed Outcome → Success-Condition
  Assessment → Outcome Review) with Hypothesis-drawn valid inputs, and
  asserts every prior-slice table is byte-for-byte identical to its
  pre-pipeline snapshot (Requirements 53.1, 47.8, 48.7). It additionally
  asserts that no Slice 4 table carries a column whose name matches a
  prohibited intended-side prefix — the addressed Intended Outcome Revision
  is referenced only through the explicit ``Addresses`` Identity reference
  and the ``target_intended_outcome_*`` Identity columns, never as an
  intended-side *value* attribute (Requirement 53.2).

Setup follows the conventions established by the Slice 1/2/3/4 property
tests (per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory state
cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps,
``pytest.mark.property``, ``@settings(max_examples=100, deadline=2000)``,
conftest seed-capture). The helper scaffolding mirrors
``tests/property/test_property_46_outcome_creation_anchoring.py``.
"""

from __future__ import annotations

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
from walking_slice.outcome._helpers import OUTCOME_PROHIBITED_PREFIXES
from walking_slice.outcome._persistence import (
    OUTCOME_IMMUTABLE_TABLES,
    create_outcome_schema,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
    MeasurementDefinitionValidationError,
)
from walking_slice.outcome.measurement_records import (
    MeasurementRecordService,
    MeasurementRecordValidationError,
)
from walking_slice.outcome.observed_outcomes import (
    ObservedOutcomeService,
    ObservedOutcomeValidationError,
)
from walking_slice.outcome.outcome_reviews import (
    OutcomeReviewService,
    OutcomeReviewValidationError,
)
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
    SuccessConditionAssessmentValidationError,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and constants — the deterministic prerequisite chain.
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

# Directly-seeded citable Slice 3 artifacts (Outcome Review endpoint).
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000d00004"
)

_UNIT: Final[str] = "percent"
# ISO-8601 closed window covering the 2025 observation instants the strategies
# draw; both edges precede the fixed recorded time (2026-01-01).
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"

_OUTCOME_KIND_OBSERVED: Final[str] = "observed"

# The Slice 1 / Slice 2 / Slice 3 tables whose rows must be byte-equivalent
# before and after every Slice 4 write (Requirement 53.1). All exist after the
# four schema creators run; the snapshot covers every table whether or not the
# pipeline writes a row into it.
_PRIOR_SLICE_TABLES: Final[tuple[str, ...]] = (
    # Slice 2 — planning
    "Objectives",
    "Objective_Revisions",
    "Intended_Outcomes",
    "Intended_Outcome_Revisions",
    "Projects",
    "Project_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    "Activity_Plans",
    "Plan_Revisions",
    "Plan_Reviews",
    "Plan_Review_Revisions",
    "Plan_Approval_Records",
    # Slice 3 — execution
    "Work_Assignment_Records",
    "Work_Event_Records",
    "Time_Entry_Records",
    "Deliverable_Production_Records",
    "Milestone_Acceptance_Records",
    "Completion_Records",
    # Slice 3 — deliverable repository
    "Deliverable_Resources",
    "Deliverable_Revisions",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
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
# Per-case service bundle (mirrors test_property_46).
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
# Seed helpers (mirror tests/property/test_property_46 and tests/unit/test_outcome_*.py).
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
    """Seed one resolvable Completion Record by direct INSERT (Review endpoint)."""
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


def _snapshot_prior_slice(engine: Engine) -> dict[str, list[tuple[Any, ...]]]:
    """Snapshot every Slice 1/2/3 table as a list of row tuples in PK order.

    The ``SELECT *`` column order is stable for a given schema, so the
    returned structure is a byte-equivalence ground truth for the
    post-pipeline comparison (Requirement 53.1).
    """
    snapshot: dict[str, list[tuple[Any, ...]]] = {}
    with engine.connect() as conn:
        for table in _PRIOR_SLICE_TABLES:
            rows = conn.execute(
                text(f"SELECT * FROM {table} ORDER BY 1")
            ).fetchall()
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


def _table_columns(engine: Engine, table: str) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk.
    return [str(row[1]) for row in rows]


def _normalize_key(key: str) -> str:
    """Match the Outcome_Service boundary-guard normalization."""
    return key.lower().replace("_", "-")


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)

# A narrow token alphabet for synthesized field-name suffixes; the field name
# only needs to be a plausible key, never persisted.
_KEY_SUFFIX_ALPHABET: Final[str] = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


@st.composite
def _prohibited_key(draw: Any) -> str:
    """Synthesize a field name whose normalized form begins with one of the
    eight prohibited intended-side prefixes (Requirement 53.2).

    The prefix is drawn from the canonical hyphen-lowercase set; a suffix is
    appended; then case and hyphen/underscore variants are applied so the
    boundary guard's normalization (case-insensitive,
    hyphen/underscore-invariant) is exercised. The verbatim synthesized key is
    returned — it is the exact key the guard echoes back in
    ``prohibited_keys``.
    """
    prefix = draw(st.sampled_from(OUTCOME_PROHIBITED_PREFIXES))
    suffix = draw(
        st.text(alphabet=_KEY_SUFFIX_ALPHABET, min_size=1, max_size=20)
    )
    key = prefix + suffix
    # Optionally rewrite hyphens to underscores (the snake_case convention).
    if draw(st.booleans()):
        key = key.replace("-", "_")
    # Optionally upper-case the whole key.
    if draw(st.booleans()):
        key = key.upper()
    return key


# Endpoint dispatch table: name -> (validation error type, target table).
_ENDPOINTS: Final[tuple[str, ...]] = (
    "definition",
    "native",
    "imported",
    "observed",
    "assessment",
    "review",
)

_ENDPOINT_ERRORS: Final[dict[str, type]] = {
    "definition": MeasurementDefinitionValidationError,
    "native": MeasurementRecordValidationError,
    "imported": MeasurementRecordValidationError,
    "observed": ObservedOutcomeValidationError,
    "assessment": SuccessConditionAssessmentValidationError,
    "review": OutcomeReviewValidationError,
}

_ENDPOINT_TARGET_TABLES: Final[dict[str, str]] = {
    "definition": "Measurement_Definitions",
    "native": "Measurement_Records",
    "imported": "Measurement_Records",
    "observed": "Observed_Outcomes",
    "assessment": "Success_Condition_Assessment_Records",
    "review": "Outcome_Review_Records",
}


def _invoke_endpoint(
    endpoint: str,
    svc: _Services,
    engine: Engine,
    *,
    request_attributes: dict[str, Any] | None = None,
    outcome_kind: str | None = None,
) -> None:
    """Seed the prerequisites for *endpoint*, then invoke its create method.

    Prerequisites are seeded incrementally so the endpoint's own target table
    starts empty; only the entity under test is created by the invocation.
    """
    _intended_id, intended_rev = _seed_intended_outcome(svc, engine)

    if endpoint == "definition":
        with engine.begin() as conn:
            svc.definitions.create_measurement_definition(
                conn,
                target_intended_outcome_revision_id=intended_rev,
                measurand_description="Adoption rate of the new workflow.",
                unit_of_measure=_UNIT,
                observation_window=_WINDOW_2025,
                cadence="monthly",
                data_source="product analytics",
                authoring_party_id=_DEFINER_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=engine,
                request_attributes=request_attributes,
            )
        return

    definition_rev = _seed_definition(
        svc, engine, intended_outcome_revision_id=intended_rev
    )

    if endpoint == "native":
        with engine.begin() as conn:
            svc.records.create_native_measurement(
                conn,
                target_measurement_definition_revision_id=definition_rev,
                observed_value=Decimal("12.5"),
                observed_value_unit=_UNIT,
                observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                recording_party_id=_RECORDER_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=engine,
                request_attributes=request_attributes,
            )
        return

    if endpoint == "imported":
        with engine.begin() as conn:
            svc.records.create_imported_measurement(
                conn,
                target_measurement_definition_revision_id=definition_rev,
                observed_value=Decimal("12.5"),
                observed_value_unit=_UNIT,
                observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                source_system_id="helpdesk-prod",
                source_system_record_id="TICKET-001",
                source_system_authority="authoritative",
                source_system_retrieval_time=datetime(
                    2025, 7, 1, tzinfo=timezone.utc
                ),
                importing_party_id=_RECORDER_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=engine,
                request_attributes=request_attributes,
            )
        return

    record_id = _seed_native_record(
        svc, engine, definition_revision_id=definition_rev
    )

    if endpoint == "observed":
        with engine.begin() as conn:
            svc.observed.create_observed_outcome(
                conn,
                target_intended_outcome_revision_id=intended_rev,
                assessment_summary="Adoption trending toward the target.",
                cited_measurement_record_ids=[record_id],
                authoring_party_id=_ASSESSOR_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=engine,
                outcome_kind=outcome_kind,
                request_attributes=request_attributes,
            )
        return

    observed_rev = _seed_observed_outcome(
        svc,
        engine,
        intended_outcome_revision_id=intended_rev,
        cited_record_ids=[record_id],
    )

    if endpoint == "assessment":
        with engine.begin() as conn:
            svc.assessments.create_assessment(
                conn,
                target_intended_outcome_revision_id=intended_rev,
                sourced_observed_outcome_revision_id=observed_rev,
                assessment_category="Satisfied",
                assessment_rationale="Measured adoption met the threshold.",
                assessing_party_id=_ASSESSOR_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=engine,
                request_attributes=request_attributes,
            )
        return

    # endpoint == "review"
    assessment_id = _seed_assessment(
        svc,
        engine,
        intended_outcome_revision_id=intended_rev,
        sourced_observed_outcome_revision_id=observed_rev,
    )
    completion_id = _seed_citable_completion(engine)
    deliverable_rev = _seed_citable_deliverable_revision(engine)
    with engine.begin() as conn:
        svc.reviews.create_outcome_review(
            conn,
            target_intended_outcome_revision_id=intended_rev,
            review_outcome="Achieved",
            attribution_stance="Asserted",
            confidence="High",
            review_rationale="Outcome achieved per the measured assessment.",
            attribution_evidence_reference="evidence-ref-1",
            cited_assessment_ids=[assessment_id],
            cited_completion_ids=[completion_id],
            cited_produced_deliverable_revision_ids=[deliverable_rev],
            reviewing_party_id=_REVIEWER_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


# Valid pipeline-input strategy for the prior-slice non-mutation property.
_observed_value_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


def _utc_datetime(
    start: datetime, end: datetime
) -> st.SearchStrategy[datetime]:
    return st.datetimes(min_value=start, max_value=end).map(
        lambda dt: dt.replace(tzinfo=timezone.utc, microsecond=0)
    )


@st.composite
def _pipeline_inputs(draw: Any) -> dict[str, Any]:
    """Draw a fully-valid set of Slice 4 pipeline inputs.

    Every field stays inside the per-attribute length / range named by the
    design surface so the pipeline writes succeed; only the prior-slice
    non-mutation invariant is under test.
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
    return {
        "measurand_description": draw(_bounded_text(1, 200)),
        "cadence": draw(_bounded_text(1, 50)),
        "data_source": draw(_bounded_text(1, 100)),
        "observed_value": draw(_observed_value_strategy),
        "observation_time": draw(
            _utc_datetime(datetime(2025, 1, 1), datetime(2025, 12, 30))
        ),
        "assessment_summary": draw(_bounded_text(1, 200)),
        "assessment_category": category,
        "assessment_rationale": rationale,
        "review_outcome": draw(
            st.sampled_from(
                ["Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"]
            )
        ),
        "attribution_stance": draw(
            st.sampled_from(["Asserted", "Partial", "Unattributed", "Contradicted"])
        ),
        "confidence": draw(st.sampled_from(["High", "Moderate", "Low"])),
        "review_rationale": draw(_bounded_text(1, 200)),
        "attribution_evidence_reference": draw(_bounded_text(1, 200)),
    }


# Non-`observed` outcome_kind values. Drawn from a curated interesting set
# unioned with arbitrary text filtered to exclude the one accepted literal.
_non_observed_outcome_kind = st.one_of(
    st.sampled_from(
        [
            "intended",
            "Observed",
            "OBSERVED",
            "observed ",
            " observed",
            "observed_outcome",
            "completion",
            "planned",
            "",
        ]
    ),
    st.text(alphabet=_TEXT_ALPHABET, min_size=1, max_size=30).filter(
        lambda s: s != _OUTCOME_KIND_OBSERVED
    ),
)


# ===========================================================================
# Property 50 — Property A: prohibited intended-side prefix rejected at every
# Outcome_Service endpoint, with no row persisted (Requirements 53.2, 53.3).
# ===========================================================================


# Feature: fourth-walking-slice, Property 50: Intended/Observed separation enforced from the outcome side
@given(endpoint=st.sampled_from(_ENDPOINTS), prohibited_key=_prohibited_key())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_prohibited_intended_side_prefix_rejected_at_every_endpoint(
    endpoint: str, prohibited_key: str
) -> None:
    """For any Outcome_Service creation request carrying a field whose name
    matches a prohibited intended-side prefix, the request is rejected with
    ``failed_constraint == 'prohibited_attribute'``, the offending key is
    echoed back verbatim, and the endpoint's target table holds zero rows
    (Requirements 53.2, 53.3)."""
    # Sanity: the synthesized key really is prohibited under the guard's
    # normalization (case-insensitive, hyphen/underscore-invariant).
    normalized = _normalize_key(prohibited_key)
    assert any(
        normalized.startswith(prefix) for prefix in OUTCOME_PROHIBITED_PREFIXES
    )

    request_attributes = {
        "applicable_scope": _SCOPE,
        prohibited_key: "intended-side-value",
    }
    error_type = _ENDPOINT_ERRORS[endpoint]
    target_table = _ENDPOINT_TARGET_TABLES[endpoint]

    with tempfile.TemporaryDirectory(prefix="prop50_prefix_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)

            with pytest.raises(error_type) as exc_info:
                _invoke_endpoint(
                    endpoint,
                    svc,
                    engine,
                    request_attributes=request_attributes,
                )

            assert exc_info.value.failed_constraint == "prohibited_attribute"
            assert prohibited_key in exc_info.value.prohibited_keys

            # No row persisted in the endpoint's target table (Requirement 53.3).
            assert _count(engine, target_table) == 0
        finally:
            engine.dispose()


# ===========================================================================
# Property 50 — Property B: an `outcome_kind` other than `observed` on an
# Observed Outcome Revision is rejected with no row persisted
# (Requirements 53.3, 47.4).
# ===========================================================================


# Feature: fourth-walking-slice, Property 50: Intended/Observed separation enforced from the outcome side
@given(outcome_kind=_non_observed_outcome_kind)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_non_observed_outcome_kind_rejected(outcome_kind: str) -> None:
    """For any ``outcome_kind`` value other than the literal ``observed`` on an
    Observed Outcome creation request, the request is rejected with
    ``failed_constraint == 'outcome_kind_invalid'`` and neither an
    ``Observed_Outcomes`` Resource row nor an ``Observed_Outcome_Revisions``
    row is persisted (Requirements 53.3, 47.4)."""
    with tempfile.TemporaryDirectory(prefix="prop50_kind_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)

            with pytest.raises(ObservedOutcomeValidationError) as exc_info:
                _invoke_endpoint(
                    "observed",
                    svc,
                    engine,
                    outcome_kind=outcome_kind,
                )

            assert exc_info.value.failed_constraint == "outcome_kind_invalid"
            assert "outcome_kind" in exc_info.value.invalid_attributes

            assert _count(engine, "Observed_Outcomes") == 0
            assert _count(engine, "Observed_Outcome_Revisions") == 0
        finally:
            engine.dispose()


# ===========================================================================
# Property 50 — Property C: a full valid Slice 4 pipeline leaves every prior
# slice row byte-equivalent, and no Slice 4 table carries an intended-side
# value column (Requirements 53.1, 53.2, 47.8, 48.7).
# ===========================================================================


# Feature: fourth-walking-slice, Property 50: Intended/Observed separation enforced from the outcome side
@given(payload=_pipeline_inputs())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_successful_pipeline_leaves_prior_slice_byte_equivalent(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Slice 4 pipeline (Measurement Definition →
    native Measurement Record → Observed Outcome → Success-Condition
    Assessment → Outcome Review), every Slice 1/2/3 row is byte-equivalent to
    its state immediately before the pipeline ran, and no Slice 4 table
    carries a column whose name matches a prohibited intended-side prefix
    (Requirements 53.1, 53.2, 47.8, 48.7)."""
    with tempfile.TemporaryDirectory(prefix="prop50_nonmut_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            # Slice 2 anchor + Slice 3 citable artifacts, created before the
            # snapshot so the snapshot captures every prior-slice row that the
            # Slice 4 writes might (illegitimately) touch.
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            completion_id = _seed_citable_completion(engine)
            deliverable_rev = _seed_citable_deliverable_revision(engine)

            baseline = _snapshot_prior_slice(engine)

            # --- Full valid Slice 4 pipeline ------------------------------
            with engine.begin() as conn:
                definition = svc.definitions.create_measurement_definition(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    measurand_description=payload["measurand_description"],
                    unit_of_measure=_UNIT,
                    observation_window=_WINDOW_2025,
                    cadence=payload["cadence"],
                    data_source=payload["data_source"],
                    authoring_party_id=_DEFINER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            definition_rev = definition.measurement_definition_revision_id

            with engine.begin() as conn:
                record = svc.records.create_native_measurement(
                    conn,
                    target_measurement_definition_revision_id=definition_rev,
                    observed_value=payload["observed_value"],
                    observed_value_unit=_UNIT,
                    observation_time=payload["observation_time"],
                    recording_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            record_id = record.measurement_record_id

            with engine.begin() as conn:
                observed = svc.observed.create_observed_outcome(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    assessment_summary=payload["assessment_summary"],
                    cited_measurement_record_ids=[record_id],
                    authoring_party_id=_ASSESSOR_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            observed_rev = observed.observed_outcome_revision_id

            with engine.begin() as conn:
                assessment = svc.assessments.create_assessment(
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
            assessment_id = assessment.assessment_id

            with engine.begin() as conn:
                svc.reviews.create_outcome_review(
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

            # --- Prior-slice byte-equivalence (Requirement 53.1) ----------
            after = _snapshot_prior_slice(engine)
            assert after == baseline, (
                "a Slice 4 write mutated a prior-slice row; the "
                "Intended/Observed separation invariant (Requirement 53.1) "
                "requires every Slice 1/2/3 row to remain byte-equivalent."
            )

            # Confirm the pipeline actually persisted the Slice 4 entities, so
            # the byte-equivalence above is meaningful (real writes occurred).
            assert _count(engine, "Measurement_Definitions") == 1
            assert _count(engine, "Measurement_Records") == 1
            assert _count(engine, "Observed_Outcome_Revisions") == 1
            assert _count(engine, "Success_Condition_Assessment_Records") == 1
            assert _count(engine, "Outcome_Review_Records") == 1

            # --- No intended-side value column on any Slice 4 table -------
            # The addressed Intended Outcome Revision is referenced only via
            # the explicit Addresses Identity reference and the
            # ``target_intended_outcome_*`` Identity columns; no intended-side
            # *fact* (success-condition statement, attribution-assumption text,
            # plan-review/approval outcome, completion outcome) is a column on
            # any Slice 4 table (Requirement 53.2).
            for table in OUTCOME_IMMUTABLE_TABLES:
                for column in _table_columns(engine, table):
                    normalized = _normalize_key(column)
                    assert not any(
                        normalized.startswith(prefix)
                        for prefix in OUTCOME_PROHIBITED_PREFIXES
                    ), (
                        f"Slice 4 table {table!r} carries an intended-side "
                        f"value column {column!r} (Requirement 53.2)."
                    )
        finally:
            engine.dispose()
