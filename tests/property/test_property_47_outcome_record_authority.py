# Feature: fourth-walking-slice, Property 47: Outcome-Record authority correctness
"""Property 47 — Outcome-Record authority correctness (task 15.2).

**Property 47: Outcome-Record authority correctness**

*For all* persisted Slice 4 entities, the
authoring/recording/assessing/reviewing Party held an effective Role
Assignment at the recorded time whose granted authorities include the
*precise* authority required by the action —

- ``define_measurement`` for a Measurement Definition,
- ``record_measurement`` for native *and* imported Measurement Records,
- ``assess_outcome`` for Observed Outcome Revisions *and* Success-Condition
  Assessment Records,
- ``issue_outcome_review`` for Outcome Review Records —

whose scope covers the target's applicable scope, and whose effective
period encloses the recorded time. No outcome-measurement entity exists
without a matching effective authority record.

**Validates: Requirements 44.5, 45.4, 46.5, 47.5, 48.4, 49.5, 50.1, 50.3,
52.6, 52.7, 52.8, 52.9, 61.2**

Strategy
========

Each Hypothesis case draws a *scenario* containing:

- a set of *candidate* Parties (1..3) — the Parties whose authority is
  under test — each carrying a list of 0..3 Role Assignments whose
  dimensions vary independently along the five gating axes named by the
  task description: ``effective_start`` offset, ``effective_end`` offset
  (or ``None``), revocation offset (or ``None``), ``scope`` drawn from a
  small alphabet including the wildcard ``"*"``, and a non-empty subset
  of granted authorities drawn from the cumulative *twelve*-value
  enumeration (AD-WS-33 / Requirement 52.1);
- 1..4 *attempts*, each picking one of the six Slice 4 write kinds
  (Measurement Definition, native Measurement Record, imported
  Measurement Record, Observed Outcome, Success-Condition Assessment,
  Outcome Review), a candidate Party index, and a ``target_scope`` drawn
  from ``{scope-a, scope-b, scope-c}`` used as the request's
  ``applicable_scope``.

Per case the test spins up a fresh per-case SQLite engine carrying the
Slice 1 + Slice 2 + Slice 3 + Slice 4 schemas, a shared
:class:`~walking_slice.clock.FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` (so every artifact in the case carries the
same recorded time, which keeps the assertion deterministic across
shrinks), and the full authorization-wired Outcome_Service stack. It
then:

1. Seeds the fixed *seeder* Parties (each granted exactly one of the four
   chain authorities over the wildcard scope ``"*"`` with an open-ended
   2025 effective period) plus the candidate Parties so every FK target
   resolves. The seeder Parties build the prerequisite chain for each
   attempt; the candidate Party performs only the final write under test.
2. Persists every drawn candidate Role Assignment via
   :meth:`AuthorizationService.assign_role`; assignments whose drawn
   parameters violate Requirement 12.6 (inverted effective period) are
   skipped at the strategy boundary rather than persisted.
3. Stamps ``revoked_at`` directly via UPDATE for any assignment whose
   strategy drew a revocation offset; the
   ``Role_Assignments_revoked_at_one_shot`` trigger guarantees the
   one-shot semantic regardless.
4. Seeds one Objective (the FK anchor for every Intended Outcome) and one
   shared citable Completion Record (resolvable by the Outcome Review
   service via the Slice 3 read API, AD-WS-40).
5. For every attempt, builds a *fresh* prerequisite chain at the
   attempt's ``target_scope`` using the always-authorized seeder Parties
   (a fresh Intended Outcome → Measurement Definition → Measurement Record
   → Observed Outcome → Success-Condition Assessment, only as deep as the
   attempt needs), then performs the final write with the candidate Party.
   Each final write either persists the entity (the wired
   :class:`AuthorizationService` permitted it) or raises a tolerated
   rejection (authorization deny, validation, uniqueness); both outcomes
   are accepted by the property — the assertion runs over the rows that
   *did* land.

After every attempt is processed the test queries each of the five Slice
4 tables that carry a recording Party Identity, and for every persisted
row (whether written by a seeder building a prerequisite chain or by a
candidate performing the write under test), scans ``Role_Assignments``
directly for a row that simultaneously:

- belongs to the artifact's recording Party (the ``authoring_party_id``
  for Measurement Definition Revisions and Observed Outcome Revisions, the
  ``recording_party_id`` for Measurement Records, the
  ``assessing_party_id`` for Success-Condition Assessment Records, the
  ``reviewing_party_id`` for Outcome Review Records);
- carries the *precise* required authority (``define_measurement`` /
  ``record_measurement`` / ``assess_outcome`` / ``issue_outcome_review``)
  in ``authorities_granted`` — substitution between any of the twelve
  authority types is forbidden by Requirement 52.10 / 52.11;
- covers ``applicable_scope`` (either ``"*"`` or an exact match);
- has ``effective_start <= recorded_at`` (not-yet-effective is not
  violated);
- has ``effective_end IS NULL`` *or* ``effective_end > recorded_at`` (not
  expired);
- has ``revoked_at IS NULL`` *or* ``revoked_at > recorded_at`` (not
  revoked).

The predicate is the same one the
:class:`~walking_slice.authorization.AuthorizationService` itself applies;
the property is therefore a *post-hoc* end-to-end check that the
Outcome_Service never persists an outcome-measurement entity when no such
Role Assignment exists.

Because the entire prerequisite chain for an attempt is built at one
scope, the target entity's scope, the new row's ``applicable_scope``, and
the scope the service evaluates against all coincide, so the post-hoc
scope-coverage comparison matches the service's own evaluation exactly.

Test scaffolding follows the conventions of
``tests/property/test_property_32_execution_authority.py`` (the Slice 3
authority property): a :class:`tempfile.TemporaryDirectory` owns the
per-case SQLite file (so state cannot leak between Hypothesis cases the
way a function-scoped pytest fixture would), and pragma-aware engine setup
matches the conftest fixtures exactly.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional
from uuid import UUID

import pytest
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock, FixedClock
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
# Fixed constants — recorded-time anchor, Party identifiers, the six write
# kinds and their precise required authorities.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

# Candidate-Party UUIDv7 template; each draw produces a stable Identity of
# the form ``..a0NNN`` so shrinkage diagnostics are easy to read.
_PARTY_BASE: Final[str] = "00000000-0000-7000-8000-0000000a0"


def _party_id(index: int) -> str:
    """Stable UUIDv7-shaped candidate-Party Identity for a given index."""
    return f"{_PARTY_BASE}{index:03d}"


# Fixed seeder / prerequisite Parties. Each holds exactly one chain
# authority over the wildcard scope so the prerequisite chain for every
# attempt can be built irrespective of the candidate Party's draw. The
# Assigning Authority signs every Role Assignment; the Completion Authority
# is the FK target for the shared citable Completion Record.
_SEED_OWNER_ID: Final[str] = "00000000-0000-7000-8000-0000000b0001"
_SEED_DEFINER_ID: Final[str] = "00000000-0000-7000-8000-0000000b0002"
_SEED_RECORDER_ID: Final[str] = "00000000-0000-7000-8000-0000000b0003"
_SEED_ASSESSOR_ID: Final[str] = "00000000-0000-7000-8000-0000000b0004"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000b0005"
_COMPLETION_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000b0006"

# Objective anchor (one per case; Intended Outcomes share it — no
# uniqueness constraint binds Intended Outcomes to one Objective).
_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000c0001"
_OBJECTIVE_REV_ID: Final[str] = "00000000-0000-7000-8000-0000000c0002"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-0000000c0003"

# Shared citable Slice 3 Completion Record (resolvable by the Outcome
# Review service via the read-only ``CompletionService.get_completion``).
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-0000000d0001"

# Authority basis shared by Assessment and Review writes. The property is
# orthogonal to AD-WS-10 basis-enumeration validation, so a single fixed
# basis is sufficient.
_AUTHORITY_BASIS_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000ba001")
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Measurement window covering the 2025 observation instants used by the
# Measurement Record helpers; both edges precede the fixed recorded time so
# native and imported records validate cleanly.
_UNIT: Final[str] = "percent"
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW: Final[datetime] = datetime(2025, 6, 1, tzinfo=timezone.utc)
_RETRIEVAL_IN_ORDER: Final[datetime] = datetime(2025, 9, 1, tzinfo=timezone.utc)

# Scope alphabet. The wildcard ``"*"`` exercises the wildcard branch of
# scope coverage; the three discrete scope identifiers exercise the
# equality branch and the scope-mismatch axis.
_SCOPES: Final[tuple[str, ...]] = ("scope-a", "scope-b", "scope-c")
_ROLE_SCOPES: Final[tuple[str, ...]] = _SCOPES + ("*",)

# The twelve-value cumulative authority enumeration after the Slice 4 /
# AD-WS-33 additive extension (Requirement 52.1). Drawn as subsets so the
# strategy can exercise the "authority does not include the required value"
# axis (Requirement 52.10 / 52.11 non-substitution) without substituting
# one authority for another.
_AUTHORITIES: Final[tuple[str, ...]] = (
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
)

# Six Slice 4 write kinds tested in one case so the non-substitution rule
# across the four new authority types is exercised in one place.
_WRITE_KINDS: Final[tuple[str, ...]] = (
    "measurement_definition",
    "measurement_record_native",
    "measurement_record_imported",
    "observed_outcome",
    "success_condition_assessment",
    "outcome_review",
)

# Per-table verification tuple: (table_name, party_column, required_authority).
# Every persisted row in each table is the subject of one Property 47
# invariant assertion. Each row's recording Party MUST hold the precise
# required authority effective at the row's recorded time over a scope that
# covers the row's applicable scope; substitution between any of the twelve
# authority types is forbidden (Requirement 52.10 / 52.11).
_VERIFY_TABLES: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "Measurement_Definition_Revisions",
        "authoring_party_id",
        "define_measurement",
    ),
    (
        "Measurement_Records",
        "recording_party_id",
        "record_measurement",
    ),
    (
        "Observed_Outcome_Revisions",
        "authoring_party_id",
        "assess_outcome",
    ),
    (
        "Success_Condition_Assessment_Records",
        "assessing_party_id",
        "assess_outcome",
    ),
    (
        "Outcome_Review_Records",
        "reviewing_party_id",
        "issue_outcome_review",
    ),
)


# ---------------------------------------------------------------------------
# Per-case engine helper.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    pragmas and all four slice schemas installed."""
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
# Seed helpers — pure SQL INSERTs that bypass the wired services for the
# fixtures the services need to resolve (Parties, Objective, citable
# Completion). Mirrors the patterns in the Slice 4 unit tests.
# ---------------------------------------------------------------------------


def _seed_party(engine: Engine, party_id: str, display: str) -> None:
    with engine.begin() as conn:
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
                    :did, :pid, '*', :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REV_ID,
                "oid": _OBJECTIVE_ID,
                "did": _DECISION_ID,
                "pid": _SEED_OWNER_ID,
                "ts": _NOW_ISO,
            },
        )


def _seed_citable_completion(engine: Engine) -> None:
    """Seed one resolvable Completion Record by direct INSERT.

    The Outcome Review service resolves cited Completion Records via the
    read-only ``CompletionService.get_completion`` (AD-WS-40); a directly
    inserted row is sufficient. Only ``completing_party_id`` carries a
    foreign key (to ``Parties``); the remaining columns are free text.
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
                    :abid, '*', :ts
                )
                """
            ),
            {
                "cid": _CITABLE_COMPLETION_ID,
                "prev": "00000000-0000-7000-8000-0000000c0fff",
                "aid": "00000000-0000-7000-8000-0000000a0fff",
                "proj": "00000000-0000-7000-8000-0000000b0fff",
                "party": _COMPLETION_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "ts": _NOW_ISO,
            },
        )


def _assign(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str,
    authorities: list[str],
    effective_start: datetime,
    effective_end: Optional[datetime],
) -> str:
    """Persist one Role Assignment and return its ``role_assignment_id``."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="property_47_role",
        scope=scope,
        authorities_granted=tuple(authorities),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _stamp_revoked_at(
    engine: Engine, role_assignment_id: str, when: datetime
) -> None:
    """Stamp ``revoked_at`` on a Role Assignment via direct UPDATE.

    The ``Role_Assignments_revoked_at_one_shot`` trigger enforces the
    one-shot semantic regardless of how the column is mutated.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(when), "rid": role_assignment_id},
        )


def _assign_seeder_roles(
    authorization_service: AuthorizationService, engine: Engine
) -> None:
    """Grant each seeder Party its single chain authority over ``"*"`` with
    an open-ended 2025 effective period so the prerequisite chain for every
    attempt builds irrespective of the candidate Party's draw."""
    _start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for party_id, authority in (
        (_SEED_OWNER_ID, "modify"),
        (_SEED_DEFINER_ID, "define_measurement"),
        (_SEED_RECORDER_ID, "record_measurement"),
        (_SEED_ASSESSOR_ID, "assess_outcome"),
    ):
        _assign(
            authorization_service,
            engine,
            party_id=party_id,
            scope="*",
            authorities=[authority],
            effective_start=_start,
            effective_end=None,
        )


# ---------------------------------------------------------------------------
# Prerequisite-chain builder. Each attempt gets a *fresh* chain at its
# target scope so per-target uniqueness constraints (one Measurement
# Definition per Intended Outcome Resource, one Outcome Review per Intended
# Outcome Revision) never block a later attempt for reasons orthogonal to
# authority. Every prerequisite write is performed by the always-authorized
# seeder Parties.
# ---------------------------------------------------------------------------


def _make_intended(services: dict[str, Any], engine: Engine, scope: str) -> Any:
    with engine.begin() as conn:
        return services["intended"].create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition="Onboarding completes in under two days.",
            observation_window="30 days post launch",
            attribution_assumption="Sampling rate held constant.",
            authoring_party_id=_SEED_OWNER_ID,
            applicable_scope=scope,
            engine=engine,
        )


def _make_definition(
    services: dict[str, Any],
    engine: Engine,
    scope: str,
    intended_revision_id: str,
) -> Any:
    with engine.begin() as conn:
        return services["definition"].create_measurement_definition(
            conn,
            target_intended_outcome_revision_id=intended_revision_id,
            measurand_description="Adoption rate of the new workflow.",
            unit_of_measure=_UNIT,
            observation_window=_WINDOW_2025,
            cadence="monthly",
            data_source="product analytics",
            authoring_party_id=_SEED_DEFINER_ID,
            applicable_scope=scope,
            engine=engine,
        )


def _make_native_record(
    services: dict[str, Any],
    engine: Engine,
    scope: str,
    definition_revision_id: str,
) -> Any:
    with engine.begin() as conn:
        return services["record"].create_native_measurement(
            conn,
            target_measurement_definition_revision_id=definition_revision_id,
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=_OBS_IN_WINDOW,
            recording_party_id=_SEED_RECORDER_ID,
            applicable_scope=scope,
            engine=engine,
        )


def _make_observed(
    services: dict[str, Any],
    engine: Engine,
    scope: str,
    intended_revision_id: str,
    record_id: str,
) -> Any:
    with engine.begin() as conn:
        return services["observed"].create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=intended_revision_id,
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=[record_id],
            authoring_party_id=_SEED_ASSESSOR_ID,
            applicable_scope=scope,
            engine=engine,
        )


def _make_assessment(
    services: dict[str, Any],
    engine: Engine,
    scope: str,
    intended_revision_id: str,
    observed_revision_id: str,
) -> Any:
    with engine.begin() as conn:
        return services["assessment"].create_assessment(
            conn,
            target_intended_outcome_revision_id=intended_revision_id,
            sourced_observed_outcome_revision_id=observed_revision_id,
            assessment_category="Satisfied",
            assessment_rationale="Measured adoption met the success threshold.",
            assessing_party_id=_SEED_ASSESSOR_ID,
            authority_basis=_BASIS,
            applicable_scope=scope,
            engine=engine,
        )


# ---------------------------------------------------------------------------
# Per-attempt dispatch. Each attempt builds the prerequisite chain only as
# deep as its write kind requires, then performs the final write with the
# candidate Party. Any tolerated rejection (authorization deny, validation,
# uniqueness) is caught and treated as "the attempt did not persist a row";
# both outcomes are accepted by Property 47, which only quantifies over
# rows that *did* land.
# ---------------------------------------------------------------------------


_TOLERATED_REJECTIONS: Final[tuple[type[BaseException], ...]] = (
    PermissionError,
    LookupError,
    ValueError,
    RuntimeError,
)


def _run_attempt(
    *,
    engine: Engine,
    attempt: dict,
    party_ids: list[str],
    services: dict[str, Any],
) -> None:
    """Dispatch one attempt to its wired Outcome_Service write path."""
    kind = attempt["kind"]
    candidate = party_ids[attempt["party_index"]]
    scope = attempt["target_scope"]

    try:
        if kind == "measurement_definition":
            intended = _make_intended(services, engine, scope)
            with engine.begin() as conn:
                services["definition"].create_measurement_definition(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended.intended_outcome_revision_id
                    ),
                    measurand_description="Adoption rate of the new workflow.",
                    unit_of_measure=_UNIT,
                    observation_window=_WINDOW_2025,
                    cadence="monthly",
                    data_source="product analytics",
                    authoring_party_id=candidate,
                    applicable_scope=scope,
                    engine=engine,
                )
        elif kind == "measurement_record_native":
            intended = _make_intended(services, engine, scope)
            definition = _make_definition(
                services, engine, scope, intended.intended_outcome_revision_id
            )
            with engine.begin() as conn:
                services["record"].create_native_measurement(
                    conn,
                    target_measurement_definition_revision_id=(
                        definition.measurement_definition_revision_id
                    ),
                    observed_value=Decimal("12.5"),
                    observed_value_unit=_UNIT,
                    observation_time=_OBS_IN_WINDOW,
                    recording_party_id=candidate,
                    applicable_scope=scope,
                    engine=engine,
                )
        elif kind == "measurement_record_imported":
            intended = _make_intended(services, engine, scope)
            definition = _make_definition(
                services, engine, scope, intended.intended_outcome_revision_id
            )
            with engine.begin() as conn:
                services["record"].create_imported_measurement(
                    conn,
                    target_measurement_definition_revision_id=(
                        definition.measurement_definition_revision_id
                    ),
                    observed_value=Decimal("12.5"),
                    observed_value_unit=_UNIT,
                    observation_time=_OBS_IN_WINDOW,
                    source_system_id="crm-prod",
                    source_system_record_id="row-42",
                    source_system_authority="authoritative",
                    source_system_retrieval_time=_RETRIEVAL_IN_ORDER,
                    importing_party_id=candidate,
                    applicable_scope=scope,
                    engine=engine,
                )
        elif kind == "observed_outcome":
            intended = _make_intended(services, engine, scope)
            definition = _make_definition(
                services, engine, scope, intended.intended_outcome_revision_id
            )
            record = _make_native_record(
                services,
                engine,
                scope,
                definition.measurement_definition_revision_id,
            )
            with engine.begin() as conn:
                services["observed"].create_observed_outcome(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended.intended_outcome_revision_id
                    ),
                    assessment_summary=(
                        "Adoption trending toward the success target."
                    ),
                    cited_measurement_record_ids=[record.measurement_record_id],
                    authoring_party_id=candidate,
                    applicable_scope=scope,
                    engine=engine,
                )
        elif kind == "success_condition_assessment":
            intended = _make_intended(services, engine, scope)
            definition = _make_definition(
                services, engine, scope, intended.intended_outcome_revision_id
            )
            record = _make_native_record(
                services,
                engine,
                scope,
                definition.measurement_definition_revision_id,
            )
            observed = _make_observed(
                services,
                engine,
                scope,
                intended.intended_outcome_revision_id,
                record.measurement_record_id,
            )
            with engine.begin() as conn:
                services["assessment"].create_assessment(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended.intended_outcome_revision_id
                    ),
                    sourced_observed_outcome_revision_id=(
                        observed.observed_outcome_revision_id
                    ),
                    assessment_category="Satisfied",
                    assessment_rationale=(
                        "Measured adoption met the success threshold."
                    ),
                    assessing_party_id=candidate,
                    authority_basis=_BASIS,
                    applicable_scope=scope,
                    engine=engine,
                )
        elif kind == "outcome_review":
            intended = _make_intended(services, engine, scope)
            definition = _make_definition(
                services, engine, scope, intended.intended_outcome_revision_id
            )
            record = _make_native_record(
                services,
                engine,
                scope,
                definition.measurement_definition_revision_id,
            )
            observed = _make_observed(
                services,
                engine,
                scope,
                intended.intended_outcome_revision_id,
                record.measurement_record_id,
            )
            assessment = _make_assessment(
                services,
                engine,
                scope,
                intended.intended_outcome_revision_id,
                observed.observed_outcome_revision_id,
            )
            with engine.begin() as conn:
                services["review"].create_outcome_review(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended.intended_outcome_revision_id
                    ),
                    review_outcome="Achieved",
                    attribution_stance="Partial",
                    confidence="High",
                    review_rationale=(
                        "Reviewed evidence and concluded the outcome held."
                    ),
                    attribution_evidence_reference="",
                    cited_assessment_ids=[assessment.assessment_id],
                    cited_completion_ids=[_CITABLE_COMPLETION_ID],
                    cited_produced_deliverable_revision_ids=(),
                    reviewing_party_id=candidate,
                    authority_basis=_BASIS,
                    applicable_scope=scope,
                    engine=engine,
                )
        else:  # pragma: no cover - defensive
            raise AssertionError(f"Unknown write kind: {kind!r}")
    except _TOLERATED_REJECTIONS:
        # Expected for any attempt the wired services reject; the property
        # holds vacuously for denied / rejected attempts because no row
        # was persisted.
        pass


# ---------------------------------------------------------------------------
# Verification probes.
# ---------------------------------------------------------------------------


def _fetch_rows(engine: Engine, *, table_name: str, party_column: str) -> list[dict[str, Any]]:
    """Return every persisted row in ``table_name`` with its recording
    Party, applicable scope, and recorded time."""
    sql = (
        f"SELECT {party_column} AS party_id, applicable_scope, recorded_at "
        f"FROM {table_name} "
        f"ORDER BY recorded_at, {party_column}"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [dict(row) for row in rows]


def _fetch_role_assignments_for_party(
    engine: Engine, *, party_id: str
) -> list[dict[str, Any]]:
    """Return every Role Assignment row recorded for ``party_id`` — the
    candidate set for Property 47's existential check."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT role_assignment_id, party_id, role_name, scope,
                           authorities_granted, effective_start,
                           effective_end, revoked_at
                    FROM Role_Assignments
                    WHERE party_id = :pid
                    """
                ),
                {"pid": party_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _role_matches_artifact(
    role: dict[str, Any],
    *,
    required_authority: str,
    applicable_scope: str,
    recorded_at_iso: str,
) -> bool:
    """Return ``True`` iff ``role`` satisfies Property 47 for the artifact.

    The predicate mirrors :class:`AuthorizationService` exactly:

    - the role must grant ``required_authority`` (Requirement 52.6 .. 52.9;
      no substitution between any of the twelve authority types per
      Requirement 52.10 / 52.11);
    - the role's ``scope`` must cover ``applicable_scope`` (``"*"`` wildcard
      or exact equality);
    - the artifact's ``recorded_at`` must fall inside the role's effective
      period: ``effective_start <= recorded_at``, ``effective_end`` is unset
      or ``> recorded_at``, and ``revoked_at`` is unset or ``> recorded_at``.

    String comparisons are correct because every timestamp column is stored
    in the lexicographically sortable ``YYYY-MM-DDTHH:MM:SS.mmmZ`` form used
    by :func:`walking_slice.audit.format_iso8601_ms`.
    """
    try:
        authorities = json.loads(role["authorities_granted"])
    except (TypeError, ValueError):
        return False
    if required_authority not in authorities:
        return False
    scope = role["scope"]
    if scope != "*" and scope != applicable_scope:
        return False
    if role["effective_start"] > recorded_at_iso:
        return False  # not-yet-effective
    effective_end = role["effective_end"]
    if effective_end is not None and effective_end <= recorded_at_iso:
        return False  # expired
    revoked_at = role["revoked_at"]
    if revoked_at is not None and revoked_at <= recorded_at_iso:
        return False  # revoked
    return True


# ---------------------------------------------------------------------------
# Hypothesis strategies. The five gating axes map onto five independent
# draws per Role Assignment, sampled from small alphabets so Hypothesis can
# cover the combinations over a 100-case run.
# ---------------------------------------------------------------------------


_offset_days_strategy = st.integers(min_value=-30, max_value=30)
_optional_offset_days_strategy = st.one_of(
    st.none(),
    st.integers(min_value=-30, max_value=30),
)
_authorities_subset_strategy = st.sets(
    st.sampled_from(_AUTHORITIES), min_size=1, max_size=len(_AUTHORITIES)
)
_scope_strategy = st.sampled_from(_ROLE_SCOPES)


@st.composite
def _role_assignment_draw(draw) -> dict:
    """Draw one candidate Role Assignment as a dict of strategy outputs.

    The five fields independently drive the five gating dimensions named in
    the task description — every Role Assignment that ends up matching a
    persisted artifact must, by construction, satisfy *all five*
    simultaneously.
    """
    return {
        "scope": draw(_scope_strategy),
        "authorities": sorted(draw(_authorities_subset_strategy)),
        "effective_start_offset": draw(_offset_days_strategy),
        "effective_end_offset": draw(_optional_offset_days_strategy),
        "revoked_offset": draw(_optional_offset_days_strategy),
    }


@st.composite
def _attempt_draw(draw, *, num_parties: int) -> dict:
    """Draw one Slice 4 write attempt: a write kind, a candidate Party
    index, and the ``applicable_scope`` the request will use."""
    return {
        "kind": draw(st.sampled_from(_WRITE_KINDS)),
        "party_index": draw(st.integers(min_value=0, max_value=num_parties - 1)),
        "target_scope": draw(st.sampled_from(_SCOPES)),
    }


@st.composite
def _scenario_strategy(draw) -> dict:
    """Draw a full scenario: candidate Parties, their Role Assignments, and
    the per-attempt write-kind / party-index / target-scope tuples."""
    num_parties = draw(st.integers(min_value=1, max_value=3))
    party_assignments = [
        draw(st.lists(_role_assignment_draw(), min_size=0, max_size=3))
        for _ in range(num_parties)
    ]
    num_attempts = draw(st.integers(min_value=1, max_value=4))
    attempts = [
        draw(_attempt_draw(num_parties=num_parties)) for _ in range(num_attempts)
    ]
    return {
        "num_parties": num_parties,
        "party_assignments": party_assignments,
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Service-stack builder.
# ---------------------------------------------------------------------------


def _build_services(
    *,
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> dict[str, Any]:
    """Construct the fully authorization-wired Outcome_Service stack plus
    the Slice 3 read collaborators the Outcome Review service consults."""
    intended = IntendedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    definition = MeasurementDefinitionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended,
    )
    record = MeasurementRecordService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        definition_reader=definition,
    )
    observed = ObservedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended,
        measurement_reader=record,
        definition_reader=definition,
    )
    assessment = SuccessConditionAssessmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended,
        observed_outcome_reader=observed,
    )
    # The Outcome Review service consults only the static reads
    # ``CompletionService.get_completion`` and
    # ``DeliverableRepositoryService.get_revision``; the read instances do
    # not need wired write collaborators (mirrors the unit-test fixtures).
    completion_reader = CompletionService(
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
    deliverable_reader = DeliverableRepositoryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    review = OutcomeReviewService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended,
        assessment_reader=assessment,
        completion_reader=completion_reader,
        deliverable_reader=deliverable_reader,
        denial_audit_sleep=lambda _seconds: None,
    )
    return {
        "intended": intended,
        "definition": definition,
        "record": record,
        "observed": observed,
        "assessment": assessment,
        "review": review,
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: fourth-walking-slice, Property 47: Outcome-Record authority correctness
@given(scenario=_scenario_strategy())
@settings(max_examples=100, deadline=2000)
def test_outcome_record_authority_correctness(scenario: dict) -> None:
    """Every persisted Slice 4 entity created by the Outcome_Service has a
    matching effective Role Assignment for its recording Party whose granted
    authorities include the *precise* required authority
    (``define_measurement`` / ``record_measurement`` / ``assess_outcome`` /
    ``issue_outcome_review``), whose scope covers the target's applicable
    scope, and whose effective period encloses the recorded time; no
    outcome-measurement entity exists without a matching effective authority
    record (Property 47 / Requirement 61.2)."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop47_",
        ignore_cleanup_errors=True,
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        try:
            # Fresh per-case services so cross-case IdentityService state
            # cannot leak. The FixedClock anchors every persisted
            # ``recorded_at`` to one instant, keeping the assertion
            # deterministic and Hypothesis shrinkage tractable.
            clock = FixedClock(_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
            authorization_service = AuthorizationService(
                clock=clock,
                audit_log=audit_log,
                identity_service=identity_service,
            )
            services = _build_services(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            party_ids = [_party_id(i) for i in range(scenario["num_parties"])]

            # 1. Seed the fixed seeder Parties, the candidate Parties, the
            #    Objective anchor, and the shared citable Completion Record.
            _seed_party(engine, _SEED_OWNER_ID, "Seed Intended Outcome Owner")
            _seed_party(engine, _SEED_DEFINER_ID, "Seed Measurement Definer")
            _seed_party(engine, _SEED_RECORDER_ID, "Seed Measurement Recorder")
            _seed_party(engine, _SEED_ASSESSOR_ID, "Seed Outcome Assessor")
            _seed_party(engine, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
            _seed_party(engine, _COMPLETION_PARTY_ID, "Completion Authority")
            for index, pid in enumerate(party_ids):
                _seed_party(engine, pid, f"Property 47 Candidate Party {index}")

            _assign_seeder_roles(authorization_service, engine)
            _seed_objective(engine)
            _seed_citable_completion(engine)

            # 2. Persist every drawn candidate Role Assignment. Skipping
            #    assignments where ``effective_end <= effective_start`` keeps
            #    the input space valid: such assignments are never permitting
            #    and the ``AssignRoleRequest`` validator would otherwise
            #    accept them only to make them dead — skipping keeps shrinks
            #    coherent.
            for party_index, assignments in enumerate(
                scenario["party_assignments"]
            ):
                pid = party_ids[party_index]
                for assignment in assignments:
                    eff_start = _NOW + timedelta(
                        days=assignment["effective_start_offset"]
                    )
                    eff_end: Optional[datetime] = None
                    if assignment["effective_end_offset"] is not None:
                        eff_end = _NOW + timedelta(
                            days=assignment["effective_end_offset"]
                        )
                        if eff_end <= eff_start:
                            continue
                    rid = _assign(
                        authorization_service,
                        engine,
                        party_id=pid,
                        scope=assignment["scope"],
                        authorities=assignment["authorities"],
                        effective_start=eff_start,
                        effective_end=eff_end,
                    )
                    if assignment["revoked_offset"] is not None:
                        revoked_at = _NOW + timedelta(
                            days=assignment["revoked_offset"]
                        )
                        _stamp_revoked_at(engine, rid, revoked_at)

            # 3. Run every attempt. Each builds a fresh prerequisite chain
            #    via the seeder Parties and performs the final write with the
            #    candidate Party; permits persist a row, denials / rejections
            #    persist nothing.
            for attempt in scenario["attempts"]:
                _run_attempt(
                    engine=engine,
                    attempt=attempt,
                    party_ids=party_ids,
                    services=services,
                )

            # 4. Property assertion — for every persisted row in every Slice
            #    4 table, there exists a matching Role Assignment for the
            #    recording Party that grants the *precise* required authority
            #    and satisfies all five gating dimensions at the recorded
            #    time. This holds for the prerequisite rows written by the
            #    always-authorized seeder Parties (over ``"*"``) and for the
            #    rows written by the candidate Parties under test.
            for table_name, party_column, required_authority in _VERIFY_TABLES:
                rows = _fetch_rows(
                    engine, table_name=table_name, party_column=party_column
                )
                for row in rows:
                    party_id = row["party_id"]
                    applicable_scope = row["applicable_scope"]
                    recorded_at_iso = row["recorded_at"]
                    candidates = _fetch_role_assignments_for_party(
                        engine, party_id=party_id
                    )
                    assert any(
                        _role_matches_artifact(
                            role,
                            required_authority=required_authority,
                            applicable_scope=applicable_scope,
                            recorded_at_iso=recorded_at_iso,
                        )
                        for role in candidates
                    ), (
                        f"Property 47 violated: a {table_name} row recorded by "
                        f"Party {party_id} at {recorded_at_iso} (scope "
                        f"{applicable_scope!r}) has no effective Role Assignment "
                        f"granting the precise {required_authority!r} authority."
                    )
        finally:
            engine.dispose()
