"""Unit tests for :class:`MeasurementRecordService` (fourth-walking-slice
task 5.2).

Pins the contract established by task 5.1, design
§"Outcome_Service.MeasurementRecords", and Requirements 45.2, 45.3, 45.6,
46.2, 46.3, 46.4, 46.7, 52.7:

Native (Requirement 45):
- **45.2** — the observed value carries at most six fractional digits
  (6 accepted, 7 rejected).
- **45.3** — the observed-value unit must match the Definition's
  ``unit_of_measure``; the observation time must fall within the
  observation-window descriptor and must not be later than the recorded
  time; no imported-only source-system attribute may be supplied; the
  persisted ``origin`` is ``'native'`` with every source-system column
  NULL.

Imported (Requirement 46):
- **46.2 / 46.4** — every source-system attribute is required;
  ``source_system_authority`` is validated against the enumerated set and
  is explicitly rejected when absent (Requirement 46.7 — never defaulted
  to ``authoritative``); the observation ≤ retrieval ≤ recorded ordering
  is enforced; ``import_at = recorded_at``; the returned representation
  surfaces ``origin = imported`` and the authority designation explicitly.
- **46.3** — a duplicate ``(source_system_id, source_system_record_id)``
  pair per Definition Revision is rejected with no second Record persisted
  and the first left byte-equivalent (AD-WS-39).

Both:
- **52.7** — ``create.measurement_record`` requires the
  ``record_measurement`` authority; the deny path appends exactly one
  Denial Record in a separate transaction (AD-WS-9).
- the persisted Measurement Record is immutable (UPDATE / DELETE rejected
  by the schema triggers, AD-WS-36).
- exactly one ``Cites`` Relationship to the target Measurement Definition
  Revision carries ``semantic_role = 'measurement_basis'`` (AD-WS-35).

The tests mirror the fixture / seed-helper style of
``tests/unit/test_outcome_measurement_definitions.py`` (task 4.2's tests).
Per the task 5.1 note, the observation-window descriptor is free text;
when it is an ISO-8601 interval ``<start>/<end>`` the observation time must
fall inside inclusive of the edges, otherwise free text imposes no bound.
The window-edge tests use the interval form; the later-than-recorded test
uses a free-text window so the window check passes and the ordering check
is exercised in isolation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import (
    MeasurementRecordAuthorizationError,
    MeasurementRecordDuplicateError,
    MeasurementRecordService,
    MeasurementRecordValidationError,
)
from walking_slice.outcome.models import CreateMeasurementRecordResult


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_OWNER_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID = "00000000-0000-7000-8000-000000a00004"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

# A syntactically valid identifier never minted into the schema, used for
# the unresolvable-target branch.
_UNRESOLVABLE_REV_ID = "00000000-0000-7000-8000-0000000fffff"

_ACTION = "create.measurement_record"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# The clock is pinned to 2026-01-01T00:00:00.000Z (see tests/conftest.py).
# Native observation times must be <= the recorded time; the windows below
# bracket the observation instants the helpers use.
_UNIT = "percent"

# An ISO-8601 closed-interval window comfortably covering the 2025
# observation instants the record helpers use; both edges precede the fixed
# recorded time so native and imported records validate cleanly.
_WINDOW_2025 = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
# A restrictive interval used to drive the observation-outside-window branch.
_WINDOW_NARROW = "2025-06-01T00:00:00Z/2025-12-01T00:00:00Z"
# A free-text descriptor that imposes no machine-checkable bound, used to
# isolate the observation-later-than-recorded ordering check.
_WINDOW_FREE_TEXT = "rolling quarter"

# Observation instants (all timezone-aware UTC).
_OBS_IN_WINDOW = datetime(2025, 6, 1, tzinfo=timezone.utc)
_OBS_BEFORE_WINDOW = datetime(2025, 1, 1, tzinfo=timezone.utc)
_OBS_AFTER_RECORDED = datetime(2026, 6, 1, tzinfo=timezone.utc)
_RETRIEVAL_IN_ORDER = datetime(2025, 9, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_engine(engine: Engine) -> Engine:
    """Per-test engine carrying the Slice 1, Slice 2, and Slice 4 schemas."""
    create_schema(engine)
    create_planning_schema(engine)
    create_outcome_schema(engine)
    return engine


@pytest.fixture
def intended_outcome_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> IntendedOutcomeService:
    """Slice 2 service used to seed resolvable Intended Outcome targets."""
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
    """Slice 4 Measurement Definition service used both to seed Definition
    Revisions and as the ``definition_reader`` of the record service."""
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
    """MeasurementRecordService wired with a real AuthorizationService.

    The authorization deny path is exercised by recording as a Party
    without the ``record_measurement`` role rather than by swapping in a
    stub, so the real evaluation code path participates in the test.
    """
    return MeasurementRecordService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        definition_reader=measurement_definition_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_outcome_measurement_definitions.py).
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
    grant_record_measurement: bool = True,
) -> None:
    """Seed parties, the Objective, and the role grants.

    Always grants ``modify`` (Intended Outcome owner) and
    ``define_measurement`` (Measurement Definer) so targets can be created.
    Grants ``record_measurement`` to the Recorder unless the deny path is
    being exercised.
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
    if grant_record_measurement:
        _assign_role(
            authorization_service,
            engine,
            party_id=_RECORDER_PARTY_ID,
            role_name="measurement_recorder",
            authority="record_measurement",
        )


def _make_definition(
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    engine: Engine,
    *,
    observation_window: str = _WINDOW_2025,
    unit_of_measure: str = _UNIT,
) -> str:
    """Create one Intended Outcome + Measurement Definition with the given
    observation window and unit; return the Measurement Definition Revision
    Identity.

    Each call mints a fresh Intended Outcome (no uniqueness constraint per
    Objective), so callers may create several Definitions with different
    windows without tripping the one-Definition-per-Intended-Outcome rule.
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
                unit_of_measure=unit_of_measure,
                observation_window=observation_window,
                cadence="monthly",
                data_source="product analytics",
                authoring_party_id=_DEFINER_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=engine,
            )
        )
    return definition.measurement_definition_revision_id


def _create_native(
    service: MeasurementRecordService,
    engine: Engine,
    *,
    target_revision_id: str,
    observed_value=Decimal("12.5"),
    observed_value_unit: str = _UNIT,
    observation_time: datetime = _OBS_IN_WINDOW,
    recording_party_id: str = _RECORDER_PARTY_ID,
    applicable_scope: str = _SCOPE,
    request_attributes=None,
    correlation_id: Optional[str] = None,
) -> CreateMeasurementRecordResult:
    with engine.begin() as conn:
        return service.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=target_revision_id,
            observed_value=observed_value,
            observed_value_unit=observed_value_unit,
            observation_time=observation_time,
            recording_party_id=recording_party_id,
            applicable_scope=applicable_scope,
            engine=engine,
            request_attributes=request_attributes,
            correlation_id=correlation_id,
        )


def _create_imported(
    service: MeasurementRecordService,
    engine: Engine,
    *,
    target_revision_id: str,
    observed_value=Decimal("12.5"),
    observed_value_unit: str = _UNIT,
    observation_time: datetime = _OBS_IN_WINDOW,
    source_system_id="crm-prod",
    source_system_record_id="row-42",
    source_system_authority: Optional[str] = "authoritative",
    source_system_retrieval_time: datetime = _RETRIEVAL_IN_ORDER,
    importing_party_id: str = _RECORDER_PARTY_ID,
    applicable_scope: str = _SCOPE,
    origin: Optional[str] = None,
    request_attributes=None,
    correlation_id: Optional[str] = None,
) -> CreateMeasurementRecordResult:
    with engine.begin() as conn:
        return service.create_imported_measurement(
            conn,
            target_measurement_definition_revision_id=target_revision_id,
            observed_value=observed_value,
            observed_value_unit=observed_value_unit,
            observation_time=observation_time,
            source_system_id=source_system_id,
            source_system_record_id=source_system_record_id,
            source_system_authority=source_system_authority,
            source_system_retrieval_time=source_system_retrieval_time,
            importing_party_id=importing_party_id,
            applicable_scope=applicable_scope,
            engine=engine,
            origin=origin,
            request_attributes=request_attributes,
            correlation_id=correlation_id,
        )


# ---------------------------------------------------------------------------
# Row counters / snapshots.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_cites_relationships(engine: Engine, source_id: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = 'Cites' AND source_id = :sid"
                ),
                {"sid": source_id},
            ).scalar_one()
        )


def _count_denial_audit_rows(engine: Engine) -> int:
    """Count Denial Record rows for the action.

    A Denial Record is distinguished from the authorization evaluation row
    (which also carries ``outcome='deny'``) by ``authorities_required``
    being NULL.
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


def _snapshot_records(engine: Engine) -> list[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM Measurement_Records ORDER BY 1")
        ).all()
    return [tuple(row) for row in rows]


def _record_row(engine: Engine, measurement_record_id: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT * FROM Measurement_Records "
                    "WHERE measurement_record_id = :id"
                ),
                {"id": measurement_record_id},
            )
            .mappings()
            .one()
        )


# ===========================================================================
# Happy-path baselines — confirm the wiring before the negative paths run.
# ===========================================================================


def test_native_measurement_created_with_one_cites_relationship(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A native Measurement Record is persisted with one consequential audit
    row and exactly one ``Cites`` Relationship to the target Definition
    Revision (Requirements 45.1, 45.5, 52.7, 57.1, AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    result = _create_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
        correlation_id="corr-native",
    )

    assert isinstance(result, CreateMeasurementRecordResult)
    assert _CANONICAL_UUID7.match(result.measurement_record_id)
    assert _CANONICAL_UUID7.match(result.cites_relationship_id)
    assert result.origin == "native"
    assert result.correlation_id == "corr-native"
    assert _count(outcome_engine, "Measurement_Records") == 1
    assert _count_consequential_audit_rows(outcome_engine) == 1
    assert _count_denial_audit_rows(outcome_engine) == 0
    assert (
        _count_cites_relationships(
            outcome_engine, result.measurement_record_id
        )
        == 1
    )


def test_imported_measurement_created_with_one_cites_relationship(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An imported Measurement Record is persisted with one ``Cites``
    Relationship and one consequential audit row (Requirements 46.1, 46.6,
    52.7, 57.1, AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    result = _create_imported(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
    )

    assert result.origin == "imported"
    assert _count(outcome_engine, "Measurement_Records") == 1
    assert _count_consequential_audit_rows(outcome_engine) == 1
    assert (
        _count_cites_relationships(
            outcome_engine, result.measurement_record_id
        )
        == 1
    )


def test_cites_relationship_carries_measurement_basis_role(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """The single ``Cites`` Relationship targets the Definition Revision and
    carries ``semantic_role = 'measurement_basis'`` (AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )
    result = _create_native(
        measurement_record_service, outcome_engine, target_revision_id=rev_id
    )

    with outcome_engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT target_revision_id, semantic_role, source_kind, "
                    "target_kind FROM Relationships "
                    "WHERE relationship_type = 'Cites' AND source_id = :sid"
                ),
                {"sid": result.measurement_record_id},
            )
            .mappings()
            .all()
        )

    assert len(rows) == 1
    row = rows[0]
    assert row["target_revision_id"] == rev_id
    assert row["semantic_role"] == "measurement_basis"
    assert row["source_kind"] == "measurement_record"
    assert row["target_kind"] == "measurement_definition_revision"


# ===========================================================================
# Native — Requirement 45.2: observed-value fractional-digit boundary.
# ===========================================================================


def test_native_six_fractional_digits_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An observed value with exactly six fractional digits is accepted
    (Requirement 45.2)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    result = _create_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
        observed_value=Decimal("1.234567"),
    )
    assert result.observed_value == "1.234567"
    assert _count(outcome_engine, "Measurement_Records") == 1


def test_native_seven_fractional_digits_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An observed value with seven fractional digits is rejected with no
    Record persisted (Requirement 45.2)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            observed_value=Decimal("1.2345678"),
        )

    assert exc_info.value.failed_constraint == (
        "observed_value_too_many_fractional_digits"
    )
    assert _count(outcome_engine, "Measurement_Records") == 0


# ===========================================================================
# Native — Requirement 45.3: unit mismatch, observation-window / ordering.
# ===========================================================================


def test_native_unit_mismatch_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A native observed-value unit that differs from the Definition's
    ``unit_of_measure`` is rejected (Requirement 45.3)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            observed_value_unit="count",
        )

    assert exc_info.value.failed_constraint == "observed_value_unit_mismatch"
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_native_observation_outside_window_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An observation time outside the ISO-8601 interval observation window
    is rejected (Requirement 45.3). The instant precedes the recorded time
    so the window rejection is exercised in isolation."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
        observation_window=_WINDOW_NARROW,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            observation_time=_OBS_BEFORE_WINDOW,
        )

    assert exc_info.value.failed_constraint == "observation_time_outside_window"
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_native_window_edges_inclusive(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """The ISO-8601 interval window includes both edges (task 5.1 note): an
    observation at the window start edge is accepted (Requirement 45.3)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
        observation_window=_WINDOW_NARROW,
    )

    # Window start edge 2025-06-01T00:00:00Z; precedes the recorded time.
    result = _create_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
        observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    assert result.observation_time == "2025-06-01T00:00:00.000Z"
    assert _count(outcome_engine, "Measurement_Records") == 1


def test_native_observation_later_than_recorded_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An observation time later than the recorded time is rejected
    (Requirement 45.3). A free-text window imposes no bound so the ordering
    check is exercised in isolation."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
        observation_window=_WINDOW_FREE_TEXT,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            observation_time=_OBS_AFTER_RECORDED,
        )

    assert exc_info.value.failed_constraint == "observation_time_after_recorded"
    assert _count(outcome_engine, "Measurement_Records") == 0


# ===========================================================================
# Native — Requirement 45.3: no imported-only source-system attribute.
# ===========================================================================


@pytest.mark.parametrize(
    "source_attribute_key",
    [
        "source-system-id",
        "source_system_record_id",
        "source-system-authority",
        "source_system_retrieval_time",
        "import-at",
    ],
)
def test_native_rejects_supplied_source_system_attribute(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    source_attribute_key: str,
) -> None:
    """A native request body carrying any imported-only source-system
    attribute is rejected with no Record persisted (Requirement 45.3)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            request_attributes={source_attribute_key: "smuggled"},
        )

    assert exc_info.value.failed_constraint == (
        "native_source_system_attribute_supplied"
    )
    assert source_attribute_key in exc_info.value.invalid_attributes
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_native_persists_origin_native_with_null_source_columns(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A native Measurement Record persists ``origin = 'native'`` with every
    source-system column NULL (Requirement 45.3, AD-WS-38)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )
    result = _create_native(
        measurement_record_service, outcome_engine, target_revision_id=rev_id
    )

    assert result.source_system_id is None
    assert result.source_system_record_id is None
    assert result.source_system_authority is None
    assert result.source_system_retrieval_time is None
    assert result.import_at is None

    row = _record_row(outcome_engine, result.measurement_record_id)
    assert row["origin"] == "native"
    assert row["source_system_id"] is None
    assert row["source_system_record_id"] is None
    assert row["source_system_authority"] is None
    assert row["source_system_retrieval_at"] is None
    assert row["import_at"] is None


# ===========================================================================
# Imported — Requirement 46.4: every source-system attribute required.
# ===========================================================================


@pytest.mark.parametrize(
    "field, missing_value",
    [
        ("source_system_id", ""),
        ("source_system_record_id", ""),
        ("source_system_authority", None),
        ("source_system_retrieval_time", None),
    ],
)
def test_imported_missing_source_system_attribute_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    field: str,
    missing_value,
) -> None:
    """Every source-system attribute is required on an imported Record; an
    omitted attribute is rejected with no Record persisted (Requirement
    46.4)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            **{field: missing_value},
        )

    assert exc_info.value.failed_constraint == "source_system_attribute_missing"
    assert field in exc_info.value.invalid_attributes
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_imported_authority_absent_rejected_not_defaulted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An absent ``source_system_authority`` is rejected and never defaulted
    to ``authoritative`` (Requirement 46.7)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            source_system_authority=None,
        )

    assert exc_info.value.failed_constraint == "source_system_attribute_missing"
    assert "source_system_authority" in exc_info.value.invalid_attributes
    # Nothing was persisted, so no Record carries a defaulted authority.
    assert _count(outcome_engine, "Measurement_Records") == 0


@pytest.mark.parametrize(
    "authority", ["authoritative", "replica", "projection", "index", "federation"]
)
def test_imported_authority_enumeration_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    authority: str,
) -> None:
    """Every enumerated ``source_system_authority`` designation is accepted
    and surfaced explicitly (Requirements 46.4, 46.7)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    result = _create_imported(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
        source_system_authority=authority,
    )
    assert result.source_system_authority == authority


def test_imported_authority_outside_enumeration_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A ``source_system_authority`` outside the enumerated set is rejected
    (Requirement 46.4)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            source_system_authority="canonical",
        )

    assert exc_info.value.failed_constraint == "source_system_authority_invalid"
    assert _count(outcome_engine, "Measurement_Records") == 0


# ===========================================================================
# Imported — Requirement 46.4: observation <= retrieval <= recorded ordering.
# ===========================================================================


def test_imported_observation_after_retrieval_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An observation time later than the retrieval time is rejected
    (Requirement 46.4)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            observation_time=datetime(2025, 8, 1, tzinfo=timezone.utc),
            source_system_retrieval_time=datetime(
                2025, 7, 1, tzinfo=timezone.utc
            ),
        )

    assert exc_info.value.failed_constraint == "observation_after_retrieval"
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_imported_retrieval_after_recorded_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A retrieval time later than the recorded time is rejected
    (Requirement 46.4). The recorded time is the fixed clock instant."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            observation_time=_OBS_IN_WINDOW,
            source_system_retrieval_time=datetime(
                2026, 6, 1, tzinfo=timezone.utc
            ),
        )

    assert exc_info.value.failed_constraint == "retrieval_after_recorded"
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_imported_origin_other_than_imported_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An ``origin`` supplied as anything other than ``imported`` is rejected
    (Requirement 46.4)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordValidationError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            origin="native",
        )

    assert exc_info.value.failed_constraint == "origin_indicator_invalid"
    assert _count(outcome_engine, "Measurement_Records") == 0


def test_imported_sets_import_at_equal_to_recorded_at(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """An imported Record sets ``import_at = recorded_at`` (Requirement
    46.2) and surfaces ``origin = imported`` with the authority explicitly."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    result = _create_imported(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
        source_system_authority="replica",
    )

    assert result.origin == "imported"
    assert result.source_system_authority == "replica"
    assert result.import_at == result.recorded_at

    row = _record_row(outcome_engine, result.measurement_record_id)
    assert row["origin"] == "imported"
    assert row["import_at"] == row["recorded_at"]
    assert row["source_system_authority"] == "replica"


# ===========================================================================
# Imported — Requirement 46.3: idempotency-key duplicate rejection.
# ===========================================================================


def test_imported_duplicate_idempotency_key_rejected_first_byte_equivalent(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A second imported Record carrying the same
    ``(source_system_id, source_system_record_id)`` pair against the same
    Definition Revision is rejected with no second Record persisted and the
    first left byte-equivalent (Requirement 46.3, AD-WS-39)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    first = _create_imported(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_id,
        source_system_id="crm-prod",
        source_system_record_id="row-99",
    )

    before = _snapshot_records(outcome_engine)

    with pytest.raises(MeasurementRecordDuplicateError) as exc_info:
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            source_system_id="crm-prod",
            source_system_record_id="row-99",
            observed_value=Decimal("99.9"),
        )

    assert (
        exc_info.value.existing_measurement_record_id
        == first.measurement_record_id
    )
    assert exc_info.value.failed_constraint == "imported_measurement_duplicate"

    assert _count(outcome_engine, "Measurement_Records") == 1
    assert _snapshot_records(outcome_engine) == before


def test_imported_same_pair_different_definition_revision_allowed(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """The idempotency key is scoped per Definition Revision: the same
    source-system pair against a different Definition Revision is accepted
    (Requirement 46.3, AD-WS-39)."""
    _seed_world(authorization_service, outcome_engine)
    rev_a = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )
    rev_b = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    _create_imported(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_a,
        source_system_id="crm-prod",
        source_system_record_id="row-7",
    )
    _create_imported(
        measurement_record_service,
        outcome_engine,
        target_revision_id=rev_b,
        source_system_id="crm-prod",
        source_system_record_id="row-7",
    )

    assert _count(outcome_engine, "Measurement_Records") == 2


# ===========================================================================
# Both — Requirement 52.7: authorization deny path.
# ===========================================================================


def test_native_authorization_deny_appends_exactly_one_denial_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A Party without ``record_measurement`` is denied; no Record is created
    and exactly one Denial Record is appended in a separate transaction
    (Requirement 52.7, AD-WS-9)."""
    _seed_world(
        authorization_service, outcome_engine, grant_record_measurement=False
    )
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordAuthorizationError) as exc_info:
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
            correlation_id="corr-deny",
        )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-deny"

    assert _count(outcome_engine, "Measurement_Records") == 0
    assert _count_consequential_audit_rows(outcome_engine) == 0
    assert _count_denial_audit_rows(outcome_engine) == 1


def test_imported_authorization_deny_appends_exactly_one_denial_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """The imported write shares the deny contract: a Party without
    ``record_measurement`` is denied with exactly one Denial Record
    (Requirement 52.7, AD-WS-9)."""
    _seed_world(
        authorization_service, outcome_engine, grant_record_measurement=False
    )
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )

    with pytest.raises(MeasurementRecordAuthorizationError):
        _create_imported(
            measurement_record_service,
            outcome_engine,
            target_revision_id=rev_id,
        )

    assert _count(outcome_engine, "Measurement_Records") == 0
    assert _count_denial_audit_rows(outcome_engine) == 1


# ===========================================================================
# Both — immutability of the persisted Measurement Record (AD-WS-36).
# ===========================================================================


def test_persisted_measurement_record_rejects_update_and_delete(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """The Measurement Record written by the service is immutable: UPDATE and
    DELETE are rejected by the schema triggers (Requirement 45.6, AD-WS-36)."""
    _seed_world(authorization_service, outcome_engine)
    rev_id = _make_definition(
        intended_outcome_service,
        measurement_definition_service,
        outcome_engine,
    )
    result = _create_native(
        measurement_record_service, outcome_engine, target_revision_id=rev_id
    )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Measurement_Records SET observed_value='0' "
                    "WHERE measurement_record_id = :id"
                ),
                {"id": result.measurement_record_id},
            )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Measurement_Records "
                    "WHERE measurement_record_id = :id"
                ),
                {"id": result.measurement_record_id},
            )

    assert _count(outcome_engine, "Measurement_Records") == 1


# ===========================================================================
# Target resolution guard (Requirements 45.3 / 46.4) — runs before auth.
# ===========================================================================


def test_unresolvable_target_rejected_native(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """A native Record naming an unresolvable Definition Revision is rejected
    with no Record persisted and no Denial Record (the resolution gate runs
    before authorization)."""
    from walking_slice.outcome.measurement_records import (
        MeasurementRecordTargetNotResolvableError,
    )

    _seed_world(authorization_service, outcome_engine)

    with pytest.raises(MeasurementRecordTargetNotResolvableError):
        _create_native(
            measurement_record_service,
            outcome_engine,
            target_revision_id=_UNRESOLVABLE_REV_ID,
        )

    assert _count(outcome_engine, "Measurement_Records") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0
