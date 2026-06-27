"""Unit tests for :class:`ObservedOutcomeService` (fourth-walking-slice
task 7.2).

Pins the contract established by task 7.1, design
§"Outcome_Service.ObservedOutcomes", and Requirements 47.2, 47.3, 47.4, 47.7,
47.8, 52.8, 53.2:

- **47.2 / 47.4** — the target Intended Outcome Revision must resolve and carry
  ``outcome_kind == 'intended'``; at least one Measurement Record must be
  cited; every cited Measurement Record must resolve and be anchored to the
  single Measurement Definition Resource that addresses the target Intended
  Outcome Resource (AD-WS-40); an ``outcome_kind`` supplied with any value
  other than ``'observed'`` is rejected. Each rejection persists nothing.
- **47.3 / AD-WS-36** — a new Revision is appended through the predecessor
  chain. The current most-recent Revision is the single Revision not named as
  any other Revision's ``predecessor_revision_id``; a revise carrying a stale
  ``predecessor_revision_id`` is rejected (optimistic concurrency) with no
  Revision appended and every prior Revision left byte-equivalent.
- **47.7** — the persisted Observed Outcome Revision is immutable (UPDATE /
  DELETE rejected by the schema triggers).
- **47.8** — the addressed Intended Outcome Revision is never mutated; it is
  byte-equivalent after a revise.
- **52.8** — ``create.observed_outcome`` requires the ``assess_outcome``
  authority; the deny path appends exactly one Denial Record in a separate
  transaction (AD-WS-9).
- **53.2** — no Observed Outcome request may carry a prohibited intended-side
  attribute.
- one ``Addresses`` Relationship to the target Intended Outcome Revision
  (``semantic_role IS NULL``) plus one ``Cites`` Relationship per cited
  Measurement Record (``semantic_role = 'observation_basis'``, AD-WS-35).

The tests mirror the fixture / seed-helper style of
``tests/unit/test_outcome_measurement_records.py`` (task 5.2's tests). The
authorization deny path is exercised by assessing as a Party without the
``assess_outcome`` role rather than by swapping in a stub, so the real
evaluation code path participates. The wrong-``outcome_kind`` target branch is
exercised with a controlled ``intended_outcome_reader`` because the
``Intended_Outcome_Revisions`` CHECK forbids seeding a non-``intended`` row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
from walking_slice.planning.intended_outcomes import (
    IntendedOutcomeRevisionRow,
    IntendedOutcomeService,
)
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.models import CreateObservedOutcomeResult
from walking_slice.outcome.observed_outcomes import (
    ObservedOutcomeAuthorizationError,
    ObservedOutcomeCitationError,
    ObservedOutcomeConcurrencyError,
    ObservedOutcomeService,
    ObservedOutcomeTargetNotResolvableError,
    ObservedOutcomeValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_OWNER_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID = "00000000-0000-7000-8000-000000a00005"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

# A syntactically valid identifier never minted into the schema, used for the
# unresolvable-target and unresolvable-citation branches.
_UNRESOLVABLE_REV_ID = "00000000-0000-7000-8000-0000000fffff"
_UNRESOLVABLE_RECORD_ID = "00000000-0000-7000-8000-0000000ffffe"

_ACTION = "create.observed_outcome"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_UNIT = "percent"
# An ISO-8601 closed-interval window covering the 2025 observation instants
# the record helper uses; both edges precede the fixed recorded time.
_WINDOW_2025 = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW = datetime(2025, 6, 1, tzinfo=timezone.utc)


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
    """Slice 2 service used to seed resolvable Intended Outcome targets and as
    the ``intended_outcome_reader`` of the Observed Outcome service."""
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
    """Slice 4 Measurement Definition service used both to seed Definitions
    and as the ``definition_reader`` of the Observed Outcome service."""
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
    """Slice 4 Measurement Record service used to seed citable Records and as
    the ``measurement_reader`` of the Observed Outcome service."""
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
    """ObservedOutcomeService wired with real readers and a real
    AuthorizationService.

    The deny path is exercised by assessing as a Party without the
    ``assess_outcome`` role rather than by swapping in a stub, so the real
    evaluation code path participates in the test.
    """
    return ObservedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        measurement_reader=measurement_record_service,
        definition_reader=measurement_definition_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_outcome_measurement_records.py).
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
    grant_assess_outcome: bool = True,
) -> None:
    """Seed parties, the Objective, and the role grants.

    Always grants ``modify`` (Intended Outcome owner), ``define_measurement``
    (Measurement Definer), and ``record_measurement`` (Measurement Recorder)
    so targets and citable Records can be created. Grants ``assess_outcome``
    to the Assessor unless the deny path is being exercised.
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
    if grant_assess_outcome:
        _assign_role(
            authorization_service,
            engine,
            party_id=_ASSESSOR_PARTY_ID,
            role_name="outcome_assessor",
            authority="assess_outcome",
        )


@dataclass(frozen=True)
class _Anchor:
    """A seeded Intended Outcome + addressing Measurement Definition, plus one
    citable Measurement Record anchored to that Definition."""

    intended_outcome_revision_id: str
    intended_outcome_id: str
    measurement_definition_revision_id: str


def _make_intended_and_definition(
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    engine: Engine,
) -> _Anchor:
    """Create one Intended Outcome plus the single Measurement Definition that
    addresses it; return the identities the Observed Outcome service anchors
    against.

    Each call mints a fresh Intended Outcome (no uniqueness constraint per
    Objective), so callers may create several anchors without tripping the
    one-Definition-per-Intended-Outcome rule.
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
    return _Anchor(
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        intended_outcome_id=intended.intended_outcome_id,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )


def _record_native(
    measurement_record_service: MeasurementRecordService,
    engine: Engine,
    *,
    target_revision_id: str,
    observed_value=Decimal("12.5"),
) -> str:
    """Record one native Measurement Record against the given Definition
    Revision; return its Identity (a citable Measurement Record)."""
    with engine.begin() as conn:
        result = measurement_record_service.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=target_revision_id,
            observed_value=observed_value,
            observed_value_unit=_UNIT,
            observation_time=_OBS_IN_WINDOW,
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.measurement_record_id


def _create_observed(
    observed_outcome_service: ObservedOutcomeService,
    engine: Engine,
    *,
    target_intended_outcome_revision_id: str,
    cited_measurement_record_ids,
    assessment_summary: str = "Adoption trending toward the success target.",
    authoring_party_id: str = _ASSESSOR_PARTY_ID,
    applicable_scope: str = _SCOPE,
    outcome_kind: Optional[str] = None,
    request_attributes=None,
    correlation_id: Optional[str] = None,
) -> CreateObservedOutcomeResult:
    with engine.begin() as conn:
        return observed_outcome_service.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_measurement_record_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            engine=engine,
            outcome_kind=outcome_kind,
            request_attributes=request_attributes,
            correlation_id=correlation_id,
        )


def _revise_observed(
    observed_outcome_service: ObservedOutcomeService,
    engine: Engine,
    *,
    observed_outcome_id: str,
    predecessor_revision_id: str,
    cited_measurement_record_ids,
    assessment_summary: str = "Updated assessment after the next window.",
    authoring_party_id: str = _ASSESSOR_PARTY_ID,
    applicable_scope: str = _SCOPE,
) -> CreateObservedOutcomeResult:
    with engine.begin() as conn:
        return observed_outcome_service.revise_observed_outcome(
            conn,
            observed_outcome_id=observed_outcome_id,
            predecessor_revision_id=predecessor_revision_id,
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_measurement_record_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            engine=engine,
        )


# ---------------------------------------------------------------------------
# Row counters / snapshots.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_relationships(engine: Engine, *, rel_type: str, source_id: str) -> int:
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
    """Count Denial Record rows for the action.

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


def _observed_revision_row(engine: Engine, revision_id: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT * FROM Observed_Outcome_Revisions "
                    "WHERE observed_outcome_revision_id = :id"
                ),
                {"id": revision_id},
            )
            .mappings()
            .one()
        )


def _intended_revision_row(engine: Engine, revision_id: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT * FROM Intended_Outcome_Revisions "
                    "WHERE intended_outcome_revision_id = :id"
                ),
                {"id": revision_id},
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------------------
# Stub reader for the wrong-outcome-kind branch.
#
# The ``Intended_Outcome_Revisions`` CHECK forbids ``outcome_kind != 'intended'``,
# so the wrong-kind branch (Requirement 47.4) is exercised with a controlled
# reader returning a row whose discriminator is ``'observed'``.
# ---------------------------------------------------------------------------


class _WrongKindReader:
    """``intended_outcome_reader`` stub returning a non-``intended`` row."""

    def __init__(self, *, revision_id: str, resource_id: str) -> None:
        self._row = IntendedOutcomeRevisionRow(
            intended_outcome_revision_id=revision_id,
            intended_outcome_id=resource_id,
            parent_revision_id=None,
            outcome_kind="observed",
            target_objective_id=_OBJECTIVE_ID,
            success_condition="success",
            observation_window=None,
            attribution_assumption=None,
            authoring_party_id=_OWNER_PARTY_ID,
            applicable_scope=_SCOPE,
            recorded_at=_TS_FIXED,
        )

    def get_revision(
        self, connection: Connection, revision_id: str
    ) -> IntendedOutcomeRevisionRow:
        return self._row


# ===========================================================================
# Happy-path baseline — confirms the wiring before the negative paths run.
# ===========================================================================


def test_create_observed_outcome_one_addresses_and_one_cites(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """With ``assess_outcome`` authority, a resolvable ``intended`` target, and
    a citable anchored Measurement Record, the service creates one Resource,
    one initial immutable Revision (``predecessor_revision_id`` NULL,
    ``outcome_kind = 'observed'``), exactly one ``Addresses`` Relationship, one
    ``Cites`` Relationship, and one consequential audit row (Requirements 47.1,
    47.2, 52.8, 57.1, AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )

    result = _create_observed(
        observed_outcome_service,
        outcome_engine,
        target_intended_outcome_revision_id=anchor.intended_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
        correlation_id="corr-observed",
    )

    assert isinstance(result, CreateObservedOutcomeResult)
    assert _CANONICAL_UUID7.match(result.observed_outcome_id)
    assert _CANONICAL_UUID7.match(result.observed_outcome_revision_id)
    assert result.outcome_kind == "observed"
    assert result.predecessor_revision_id is None
    assert result.correlation_id == "corr-observed"
    assert result.cited_measurement_record_ids == (record_id,)

    assert _count(outcome_engine, "Observed_Outcomes") == 1
    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 1
    assert _count_consequential_audit_rows(outcome_engine) == 1
    assert _count_denial_audit_rows(outcome_engine) == 0
    assert (
        _count_relationships(
            outcome_engine,
            rel_type="Addresses",
            source_id=result.observed_outcome_id,
        )
        == 1
    )
    assert (
        _count_relationships(
            outcome_engine,
            rel_type="Cites",
            source_id=result.observed_outcome_id,
        )
        == 1
    )


def test_relationships_carry_expected_roles_and_targets(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """The ``Addresses`` Relationship targets the Intended Outcome Revision
    with ``semantic_role IS NULL``; each ``Cites`` Relationship targets a cited
    Measurement Record with ``semantic_role = 'observation_basis'``
    (AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_a = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )
    record_b = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
        observed_value=Decimal("33.0"),
    )

    result = _create_observed(
        observed_outcome_service,
        outcome_engine,
        target_intended_outcome_revision_id=anchor.intended_outcome_revision_id,
        cited_measurement_record_ids=[record_a, record_b],
    )

    with outcome_engine.connect() as conn:
        addresses = (
            conn.execute(
                text(
                    "SELECT source_revision_id, target_id, target_revision_id, "
                    "semantic_role, source_kind, target_kind FROM Relationships "
                    "WHERE relationship_type = 'Addresses' AND source_id = :sid"
                ),
                {"sid": result.observed_outcome_id},
            )
            .mappings()
            .all()
        )
        cites = (
            conn.execute(
                text(
                    "SELECT target_id, semantic_role, source_kind, target_kind "
                    "FROM Relationships "
                    "WHERE relationship_type = 'Cites' AND source_id = :sid "
                    "ORDER BY target_id"
                ),
                {"sid": result.observed_outcome_id},
            )
            .mappings()
            .all()
        )

    assert len(addresses) == 1
    addr = addresses[0]
    assert addr["source_revision_id"] == result.observed_outcome_revision_id
    assert addr["target_id"] == anchor.intended_outcome_id
    assert addr["target_revision_id"] == anchor.intended_outcome_revision_id
    assert addr["semantic_role"] is None
    assert addr["source_kind"] == "observed_outcome_revision"
    assert addr["target_kind"] == "intended_outcome_revision"

    assert len(cites) == 2
    assert {c["target_id"] for c in cites} == {record_a, record_b}
    for c in cites:
        assert c["semantic_role"] == "observation_basis"
        assert c["source_kind"] == "observed_outcome_revision"
        assert c["target_kind"] == "measurement_record"


# ===========================================================================
# Requirement 47.4 — target resolution.
# ===========================================================================


def test_unresolvable_target_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A target Intended Outcome Revision that does not resolve is rejected
    with nothing persisted and no Denial Record (the resolution gate runs
    before authorization)."""
    _seed_world(authorization_service, outcome_engine)

    with pytest.raises(ObservedOutcomeTargetNotResolvableError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=_UNRESOLVABLE_REV_ID,
            cited_measurement_record_ids=[_UNRESOLVABLE_RECORD_ID],
        )

    assert exc_info.value.failed_constraint == (
        "target_intended_outcome_not_resolvable"
    )
    assert _count(outcome_engine, "Observed_Outcomes") == 0
    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0


def test_target_outcome_kind_not_intended_rejected(
    outcome_engine: Engine,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    measurement_record_service: MeasurementRecordService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """A resolvable target whose ``outcome_kind`` is not ``'intended'`` is
    rejected (Requirement 47.4).

    The wrong-kind branch is exercised with a controlled reader because the
    ``Intended_Outcome_Revisions`` CHECK forbids seeding such a row.
    """
    _seed_world(authorization_service, outcome_engine)

    reader = _WrongKindReader(
        revision_id="00000000-0000-7000-8000-000000d00001",
        resource_id="00000000-0000-7000-8000-000000d00002",
    )
    service = ObservedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=reader,
        measurement_reader=measurement_record_service,
        definition_reader=measurement_definition_service,
    )

    with pytest.raises(ObservedOutcomeTargetNotResolvableError) as exc_info:
        _create_observed(
            service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                reader._row.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[_UNRESOLVABLE_RECORD_ID],
        )

    assert exc_info.value.failed_constraint == (
        "target_outcome_kind_not_intended"
    )
    assert _count(outcome_engine, "Observed_Outcomes") == 0
    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 0


# ===========================================================================
# Requirement 47.4 — citation rules.
# ===========================================================================


def test_zero_cited_measurement_records_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """An Observed Outcome citing zero Measurement Records is rejected with
    nothing persisted (Requirement 47.4)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )

    with pytest.raises(ObservedOutcomeValidationError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                anchor.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[],
        )

    assert exc_info.value.failed_constraint == "no_cited_measurement_records"
    assert _count(outcome_engine, "Observed_Outcomes") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0


def test_cited_record_not_addressing_target_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A cited Measurement Record whose Measurement Definition does not address
    the target Intended Outcome is rejected (Requirement 47.2/47.4, AD-WS-40).

    The Record is anchored to a *different* Intended Outcome's Definition, so
    it is not anchored to the Definition addressing the target.
    """
    _seed_world(authorization_service, outcome_engine)
    anchor_a = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    anchor_b = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    # Record anchored to anchor_a's Definition.
    foreign_record = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor_a.measurement_definition_revision_id,
    )

    # Target anchor_b but cite the record anchored to anchor_a.
    with pytest.raises(ObservedOutcomeCitationError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                anchor_b.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[foreign_record],
        )

    assert exc_info.value.failed_constraint == (
        "cited_measurement_record_definition_mismatch"
    )
    assert foreign_record in exc_info.value.invalid_measurement_record_ids
    assert _count(outcome_engine, "Observed_Outcomes") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0


def test_cited_record_unresolvable_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A cited Measurement Record that does not resolve is rejected
    (Requirement 47.4)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )

    with pytest.raises(ObservedOutcomeCitationError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                anchor.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[_UNRESOLVABLE_RECORD_ID],
        )

    assert exc_info.value.failed_constraint == (
        "cited_measurement_record_unresolvable"
    )
    assert _count(outcome_engine, "Observed_Outcomes") == 0


# ===========================================================================
# Requirement 47.4 — outcome_kind / Requirement 53.2 — prohibited attribute.
# ===========================================================================


def test_outcome_kind_other_than_observed_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """An ``outcome_kind`` supplied with any value other than ``'observed'`` is
    rejected with nothing persisted (Requirement 47.4)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )

    with pytest.raises(ObservedOutcomeValidationError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                anchor.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[record_id],
            outcome_kind="intended",
        )

    assert exc_info.value.failed_constraint == "outcome_kind_invalid"
    assert "outcome_kind" in exc_info.value.invalid_attributes
    assert _count(outcome_engine, "Observed_Outcomes") == 0


def test_prohibited_intended_side_attribute_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A request body carrying a prohibited intended-side attribute is rejected
    with nothing persisted (Requirement 53.2)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )

    with pytest.raises(ObservedOutcomeValidationError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                anchor.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[record_id],
            request_attributes={
                "success_condition_statement": "smuggled intended-side"
            },
        )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert _count(outcome_engine, "Observed_Outcomes") == 0


# ===========================================================================
# Requirement 47.3 / AD-WS-36 — predecessor chain.
# ===========================================================================


def test_revise_appends_to_predecessor_chain_tail(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A revise appends a new Revision whose ``predecessor_revision_id`` is the
    prior most-recent Revision; the chain tail (the single Revision not named
    as any other Revision's predecessor) is the latest Revision
    (Requirement 47.3, AD-WS-36)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )

    created = _create_observed(
        observed_outcome_service,
        outcome_engine,
        target_intended_outcome_revision_id=anchor.intended_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )
    revised = _revise_observed(
        observed_outcome_service,
        outcome_engine,
        observed_outcome_id=created.observed_outcome_id,
        predecessor_revision_id=created.observed_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )

    assert revised.observed_outcome_id == created.observed_outcome_id
    assert revised.predecessor_revision_id == created.observed_outcome_revision_id
    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 2

    # The tail is the single Revision not named as any other Revision's
    # predecessor — that is the revised Revision.
    with outcome_engine.connect() as conn:
        tail = (
            conn.execute(
                text(
                    "SELECT observed_outcome_revision_id "
                    "FROM Observed_Outcome_Revisions oor "
                    "WHERE oor.observed_outcome_id = :oid "
                    "  AND NOT EXISTS ( "
                    "    SELECT 1 FROM Observed_Outcome_Revisions s "
                    "    WHERE s.predecessor_revision_id = "
                    "          oor.observed_outcome_revision_id "
                    "  )"
                ),
                {"oid": created.observed_outcome_id},
            )
            .scalars()
            .all()
        )
    assert tail == [revised.observed_outcome_revision_id]


def test_stale_predecessor_revision_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A revise supplying a stale ``predecessor_revision_id`` (not the current
    most-recent Revision) is rejected with no Revision appended (optimistic
    concurrency, AD-WS-36 / Requirement 47.4)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )

    created = _create_observed(
        observed_outcome_service,
        outcome_engine,
        target_intended_outcome_revision_id=anchor.intended_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )
    # First revise advances the chain tail to rev2.
    revised = _revise_observed(
        observed_outcome_service,
        outcome_engine,
        observed_outcome_id=created.observed_outcome_id,
        predecessor_revision_id=created.observed_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )

    # Second revise reuses the now-stale initial Revision as predecessor.
    with pytest.raises(ObservedOutcomeConcurrencyError) as exc_info:
        _revise_observed(
            observed_outcome_service,
            outcome_engine,
            observed_outcome_id=created.observed_outcome_id,
            predecessor_revision_id=created.observed_outcome_revision_id,
            cited_measurement_record_ids=[record_id],
        )

    assert exc_info.value.failed_constraint == "stale_predecessor_revision"
    assert (
        exc_info.value.current_revision_id
        == revised.observed_outcome_revision_id
    )
    # No third Revision was appended.
    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 2


# ===========================================================================
# Requirement 47.8 — addressed Intended Outcome Revision never mutated.
# ===========================================================================


def test_addressed_intended_outcome_revision_byte_equivalent_after_revise(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A revise leaves the addressed Intended Outcome Revision and every prior
    Observed Outcome Revision byte-equivalent (Requirements 47.8, 47.3)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )
    created = _create_observed(
        observed_outcome_service,
        outcome_engine,
        target_intended_outcome_revision_id=anchor.intended_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )

    intended_before = _intended_revision_row(
        outcome_engine, anchor.intended_outcome_revision_id
    )
    initial_revision_before = _observed_revision_row(
        outcome_engine, created.observed_outcome_revision_id
    )

    _revise_observed(
        observed_outcome_service,
        outcome_engine,
        observed_outcome_id=created.observed_outcome_id,
        predecessor_revision_id=created.observed_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )

    assert (
        _intended_revision_row(
            outcome_engine, anchor.intended_outcome_revision_id
        )
        == intended_before
    )
    assert (
        _observed_revision_row(
            outcome_engine, created.observed_outcome_revision_id
        )
        == initial_revision_before
    )


# ===========================================================================
# Requirement 52.8 — authorization deny path.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """A Party without ``assess_outcome`` is denied; no Observed Outcome is
    created and exactly one Denial Record is appended in a separate transaction
    (Requirement 52.8, AD-WS-9)."""
    _seed_world(
        authorization_service, outcome_engine, grant_assess_outcome=False
    )
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )

    with pytest.raises(ObservedOutcomeAuthorizationError) as exc_info:
        _create_observed(
            observed_outcome_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                anchor.intended_outcome_revision_id
            ),
            cited_measurement_record_ids=[record_id],
            correlation_id="corr-deny",
        )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-deny"

    assert _count(outcome_engine, "Observed_Outcomes") == 0
    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 0
    assert _count_consequential_audit_rows(outcome_engine) == 0
    assert _count_denial_audit_rows(outcome_engine) == 1


# ===========================================================================
# Requirement 47.7 — immutability of the persisted Observed Outcome Revision.
# ===========================================================================


def test_persisted_observed_outcome_revision_rejects_update_and_delete(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """The Observed Outcome Revision written by the service is immutable:
    UPDATE and DELETE are rejected by the schema triggers (Requirement 47.7,
    AD-WS-36)."""
    _seed_world(authorization_service, outcome_engine)
    anchor = _make_intended_and_definition(
        intended_outcome_service, measurement_definition_service, outcome_engine
    )
    record_id = _record_native(
        measurement_record_service,
        outcome_engine,
        target_revision_id=anchor.measurement_definition_revision_id,
    )
    result = _create_observed(
        observed_outcome_service,
        outcome_engine,
        target_intended_outcome_revision_id=anchor.intended_outcome_revision_id,
        cited_measurement_record_ids=[record_id],
    )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Observed_Outcome_Revisions "
                    "SET assessment_summary = 'tampered' "
                    "WHERE observed_outcome_revision_id = :id"
                ),
                {"id": result.observed_outcome_revision_id},
            )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Observed_Outcome_Revisions "
                    "WHERE observed_outcome_revision_id = :id"
                ),
                {"id": result.observed_outcome_revision_id},
            )

    assert _count(outcome_engine, "Observed_Outcome_Revisions") == 1
