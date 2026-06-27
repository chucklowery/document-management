"""Unit tests for :meth:`MeasurementDefinitionService.create_measurement_definition`
(fourth-walking-slice task 4.2).

Pins the contract established by task 4.1, design
§"Outcome_Service.MeasurementDefinitions", and Requirements 44.2, 44.3,
44.4, 44.7, 52.6, 53.2:

- **44.2** — every descriptor field is validated to its declared range
  (measurand 1..4000, unit 1..200, observation window / cadence /
  data source 1..1000) before any database read; the boundaries are
  exercised at both ends and just outside.
- **44.3** — at most one Measurement Definition Resource per target
  Intended Outcome Resource; a second attempt is rejected with nothing
  added and the first left byte-equivalent.
- **44.4** — the target Intended Outcome Revision must resolve and carry
  ``outcome_kind == 'intended'``; a request naming more than one target
  is rejected.
- **44.7** — the persisted Measurement Definition Revision and its
  ``Addresses`` Relationship are immutable (UPDATE / DELETE rejected by
  the schema triggers).
- **52.6** — ``create.measurement_definition`` requires the
  ``define_measurement`` authority; the deny path appends exactly one
  Denial Record in a separate transaction (AD-WS-9).
- **53.2** — a request body carrying any intended-side attribute key is
  rejected at the boundary with no row persisted.

The tests mirror the fixture / seed-helper style of
``tests/unit/test_planning_intended_outcomes.py`` (the analogous Slice 2
service test) and the denial-record counting helper from
``tests/unit/test_execution_deliverable_productions.py``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
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
    MeasurementDefinitionAuthorizationError,
    MeasurementDefinitionDuplicateError,
    MeasurementDefinitionService,
    MeasurementDefinitionTargetNotResolvableError,
    MeasurementDefinitionValidationError,
)
from walking_slice.outcome.models import CreateMeasurementDefinitionResult


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# The Intended Outcome owner (Slice 2 author) and the Resource Steward who
# assigns roles. ``_AUTHOR_PARTY_ID`` is the Measurement Definition author.
_OWNER_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-000000a00003"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

# A syntactically valid identifier never minted into the schema, used for
# the unresolvable-target branch (Requirement 44.4).
_UNRESOLVABLE_REV_ID = "00000000-0000-7000-8000-0000000fffff"

_ACTION = "create.measurement_definition"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Valid descriptor defaults reused across the create helper.
_VALID_MEASURAND = "Adoption rate of the new workflow."
_VALID_UNIT = "percent"
_VALID_WINDOW = "Q1 2026"
_VALID_CADENCE = "monthly"
_VALID_DATA_SOURCE = "product analytics"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_engine(engine: Engine) -> Engine:
    """Per-test engine carrying the Slice 1, Slice 2, and Slice 4 schemas.

    ``create_schema`` installs Slice 1 plus the additive
    ``Identifier_Registry.resource_kind`` and ``Relationships.semantic_role``
    columns; ``create_planning_schema`` installs Slice 2 (the Intended
    Outcome tables seeded as the target); ``create_outcome_schema`` installs
    the Slice 4 Measurement Definition tables and their append-only triggers.
    """
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
    """Slice 2 service used both to seed the target and as the read-only
    ``intended_outcome_reader`` of the Measurement Definition service."""
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
    """MeasurementDefinitionService wired with a real AuthorizationService.

    The authorization deny path is exercised by *not* assigning the
    ``define_measurement`` role rather than by swapping in a stub service,
    so the real evaluation code path participates in the test.
    """
    return MeasurementDefinitionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_planning_intended_outcomes.py).
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
        _seed_party(conn, _AUTHOR_PARTY_ID, "Measurement Definer")


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
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=(authority,),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _assign_intended_outcome_owner_role(
    authorization_service: AuthorizationService, engine: Engine
) -> str:
    """Grant the Slice 2 ``modify`` authority so the target can be seeded."""
    return _assign_role(
        authorization_service,
        engine,
        party_id=_OWNER_PARTY_ID,
        role_name="intended_outcome_owner",
        authority="modify",
    )


def _assign_define_measurement_role(
    authorization_service: AuthorizationService, engine: Engine
) -> str:
    """Grant ``define_measurement`` to the Measurement Definition author
    (AD-WS-33 / Requirement 52.6)."""
    return _assign_role(
        authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="measurement_definer",
        authority="define_measurement",
    )


def _create_intended_outcome(
    intended_outcome_service: IntendedOutcomeService, engine: Engine
):
    """Create one Intended Outcome Revision to serve as the resolvable
    target, returning the Slice 2 result with the real identities."""
    with engine.begin() as conn:
        return intended_outcome_service.create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition="Onboarding completes in under two days.",
            observation_window="30 days post launch",
            attribution_assumption="Sampling rate held constant.",
            authoring_party_id=_OWNER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )


def _seed_target_and_authority(
    intended_outcome_service: IntendedOutcomeService,
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    grant_define_measurement: bool = True,
):
    """Seed parties, the Objective, the role grants, and one Intended
    Outcome target. Returns the Slice 2 creation result."""
    _seed_required_parties(engine)
    _assign_intended_outcome_owner_role(authorization_service, engine)
    if grant_define_measurement:
        _assign_define_measurement_role(authorization_service, engine)
    _seed_objective(engine)
    return _create_intended_outcome(intended_outcome_service, engine)


def _create_definition(
    service: MeasurementDefinitionService,
    engine: Engine,
    *,
    target_revision_id,
    measurand_description: str = _VALID_MEASURAND,
    unit_of_measure: str = _VALID_UNIT,
    observation_window: str = _VALID_WINDOW,
    cadence: str = _VALID_CADENCE,
    data_source: str = _VALID_DATA_SOURCE,
    applicable_scope: str = _SCOPE,
    authoring_party_id: str = _AUTHOR_PARTY_ID,
    request_attributes=None,
    correlation_id: Optional[str] = None,
) -> CreateMeasurementDefinitionResult:
    """Drive ``create_measurement_definition`` inside one transaction."""
    with engine.begin() as conn:
        return service.create_measurement_definition(
            conn,
            target_intended_outcome_revision_id=target_revision_id,
            measurand_description=measurand_description,
            unit_of_measure=unit_of_measure,
            observation_window=observation_window,
            cadence=cadence,
            data_source=data_source,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            engine=engine,
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


def _count_addresses_relationships(engine: Engine, source_id: str) -> int:
    """Count ``Addresses`` Relationship rows (``semantic_role IS NULL``)
    whose ``source_id`` is the Measurement Definition Resource (AD-WS-35)."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = 'Addresses' "
                    "AND source_id = :sid AND semantic_role IS NULL"
                ),
                {"sid": source_id},
            ).scalar_one()
        )


def _count_denial_audit_rows(engine: Engine) -> int:
    """Count Denial Record rows for the action.

    A Denial Record is distinguished from the authorization evaluation row
    (which also carries ``outcome='deny'``) by ``authorities_required``
    being NULL — the evaluation row populates that column with the
    JSON-encoded required authority.
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


_DEFINITION_TABLES = (
    "Measurement_Definitions",
    "Measurement_Definition_Revisions",
)


def _snapshot_definitions(engine: Engine) -> dict[str, list[tuple]]:
    snapshot: dict[str, list[tuple]] = {}
    with engine.connect() as conn:
        for table in _DEFINITION_TABLES:
            rows = conn.execute(
                text(f"SELECT * FROM {table} ORDER BY 1")
            ).all()
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


# ---------------------------------------------------------------------------
# Stub reader for the wrong-outcome-kind branch.
#
# The real ``Intended_Outcome_Revisions`` table carries a
# ``CHECK (outcome_kind = 'intended')`` constraint, so a row whose
# ``outcome_kind`` differs cannot be seeded through the schema. The
# wrong-kind branch (Requirement 44.4) is therefore exercised with a
# controlled reader returning a row whose discriminator is ``'observed'``;
# this drives real service logic against an input the schema cannot produce.
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


def test_create_measurement_definition_permits_when_role_grants_authority(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """With an effective ``define_measurement`` role and a resolvable
    Intended Outcome target, the service creates one Resource, one initial
    immutable Revision, exactly one ``Addresses`` Relationship, and one
    consequential audit row inside one transaction (Requirements 44.1,
    44.6, 52.6, 57.1, AD-WS-35)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    result = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
        correlation_id="corr-permit",
    )

    assert isinstance(result, CreateMeasurementDefinitionResult)
    assert _CANONICAL_UUID7.match(result.measurement_definition_id)
    assert _CANONICAL_UUID7.match(result.measurement_definition_revision_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert (
        result.target_intended_outcome_revision_id
        == created.intended_outcome_revision_id
    )
    assert (
        result.target_intended_outcome_resource_id
        == created.intended_outcome_id
    )
    assert result.correlation_id == "corr-permit"

    assert _count(outcome_engine, "Measurement_Definitions") == 1
    assert _count(outcome_engine, "Measurement_Definition_Revisions") == 1
    assert (
        _count_addresses_relationships(
            outcome_engine, result.measurement_definition_id
        )
        == 1
    )
    assert _count_consequential_audit_rows(outcome_engine) == 1
    assert _count_denial_audit_rows(outcome_engine) == 0


def test_addresses_relationship_targets_intended_outcome_revision(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """Exactly one ``Addresses`` Relationship per Revision, pointing at the
    target Intended Outcome Revision with ``semantic_role IS NULL``
    (AD-WS-35 / Requirement 44.4)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )
    result = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
    )

    with outcome_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT source_revision_id, target_id, target_revision_id, "
                "semantic_role FROM Relationships "
                "WHERE relationship_type = 'Addresses' AND source_id = :sid"
            ),
            {"sid": result.measurement_definition_id},
        ).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    assert row["source_revision_id"] == result.measurement_definition_revision_id
    assert row["target_id"] == created.intended_outcome_id
    assert row["target_revision_id"] == created.intended_outcome_revision_id
    assert row["semantic_role"] is None


# ===========================================================================
# Requirement 44.4 — target resolution and multi-target rejection.
# ===========================================================================


def test_unresolvable_target_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """A target Intended Outcome Revision that does not resolve is rejected
    with no Measurement Definition persisted (Requirement 44.4)."""
    _seed_required_parties(outcome_engine)
    _assign_define_measurement_role(authorization_service, outcome_engine)

    with pytest.raises(
        MeasurementDefinitionTargetNotResolvableError
    ) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=_UNRESOLVABLE_REV_ID,
        )

    assert exc_info.value.failed_constraint == (
        "target_intended_outcome_not_resolvable"
    )
    assert _count(outcome_engine, "Measurement_Definitions") == 0
    assert _count(outcome_engine, "Measurement_Definition_Revisions") == 0
    # The resolution gate runs before authorization, so no Denial Record.
    assert _count_denial_audit_rows(outcome_engine) == 0


def test_target_outcome_kind_not_intended_rejected(
    outcome_engine: Engine,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> None:
    """A resolvable target whose ``outcome_kind`` is not ``'intended'`` is
    rejected (Requirement 44.4).

    The wrong-kind branch is exercised with a controlled reader because the
    ``Intended_Outcome_Revisions`` CHECK forbids seeding such a row.
    """
    _seed_required_parties(outcome_engine)
    _assign_define_measurement_role(authorization_service, outcome_engine)

    reader = _WrongKindReader(
        revision_id="00000000-0000-7000-8000-000000d00001",
        resource_id="00000000-0000-7000-8000-000000d00002",
    )
    service = MeasurementDefinitionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=reader,
    )

    with pytest.raises(
        MeasurementDefinitionTargetNotResolvableError
    ) as exc_info:
        _create_definition(
            service,
            outcome_engine,
            target_revision_id=reader._row.intended_outcome_revision_id,
        )

    assert exc_info.value.failed_constraint == (
        "target_outcome_kind_not_intended"
    )
    assert _count(outcome_engine, "Measurement_Definitions") == 0
    assert _count(outcome_engine, "Measurement_Definition_Revisions") == 0


def test_multiple_targets_named_as_list_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """A typed target carrying more than one identifier is rejected; a
    Measurement Definition addresses exactly one (Requirement 44.4)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    with pytest.raises(MeasurementDefinitionValidationError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=[
                created.intended_outcome_revision_id,
                _UNRESOLVABLE_REV_ID,
            ],
        )

    assert exc_info.value.failed_constraint == "multiple_targets_named"
    assert _count(outcome_engine, "Measurement_Definitions") == 0


def test_multiple_targets_named_in_request_body_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """A raw request body whose pluralized target key carries a list of
    identifiers is rejected before the typed path runs (Requirement 44.4)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    with pytest.raises(MeasurementDefinitionValidationError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=created.intended_outcome_revision_id,
            request_attributes={
                "target_intended_outcome_revision_ids": [
                    created.intended_outcome_revision_id,
                    _UNRESOLVABLE_REV_ID,
                ],
            },
        )

    assert exc_info.value.failed_constraint == "multiple_targets_named"
    assert _count(outcome_engine, "Measurement_Definitions") == 0


# ===========================================================================
# Requirement 44.2 — descriptor length boundaries.
# ===========================================================================


# (field-name, min-length, max-length, missing-constraint, too-long-constraint)
_BOUNDARY_FIELDS = (
    (
        "measurand_description",
        1,
        4_000,
        "measurand_description_missing",
        "measurand_description_too_long",
    ),
    (
        "unit_of_measure",
        1,
        200,
        "unit_of_measure_missing",
        "unit_of_measure_too_long",
    ),
    (
        "observation_window",
        1,
        1_000,
        "observation_window_missing",
        "observation_window_too_long",
    ),
    ("cadence", 1, 1_000, "cadence_missing", "cadence_too_long"),
    (
        "data_source",
        1,
        1_000,
        "data_source_missing",
        "data_source_too_long",
    ),
)


@pytest.mark.parametrize(
    "field, min_len, max_len, missing_constraint, too_long_constraint",
    _BOUNDARY_FIELDS,
)
def test_descriptor_at_min_length_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    field: str,
    min_len: int,
    max_len: int,
    missing_constraint: str,
    too_long_constraint: str,
) -> None:
    """The minimum length (1 character) of each descriptor is accepted
    (Requirement 44.2)."""
    del max_len, missing_constraint, too_long_constraint
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    result = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
        **{field: "x" * min_len},
    )
    assert len(getattr(result, field)) == min_len
    assert _count(outcome_engine, "Measurement_Definitions") == 1


@pytest.mark.parametrize(
    "field, min_len, max_len, missing_constraint, too_long_constraint",
    _BOUNDARY_FIELDS,
)
def test_descriptor_at_max_length_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    field: str,
    min_len: int,
    max_len: int,
    missing_constraint: str,
    too_long_constraint: str,
) -> None:
    """The maximum length of each descriptor is accepted (Requirement 44.2)."""
    del min_len, missing_constraint, too_long_constraint
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    result = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
        **{field: "x" * max_len},
    )
    assert len(getattr(result, field)) == max_len


@pytest.mark.parametrize(
    "field, min_len, max_len, missing_constraint, too_long_constraint",
    _BOUNDARY_FIELDS,
)
def test_descriptor_empty_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    field: str,
    min_len: int,
    max_len: int,
    missing_constraint: str,
    too_long_constraint: str,
) -> None:
    """An empty descriptor (below the 1-character lower bound) is rejected
    before any database write (Requirement 44.2)."""
    del min_len, max_len, too_long_constraint
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    with pytest.raises(MeasurementDefinitionValidationError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=created.intended_outcome_revision_id,
            **{field: ""},
        )

    assert exc_info.value.failed_constraint == missing_constraint
    assert _count(outcome_engine, "Measurement_Definitions") == 0


@pytest.mark.parametrize(
    "field, min_len, max_len, missing_constraint, too_long_constraint",
    _BOUNDARY_FIELDS,
)
def test_descriptor_over_max_length_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    field: str,
    min_len: int,
    max_len: int,
    missing_constraint: str,
    too_long_constraint: str,
) -> None:
    """One character past the upper bound is rejected (Requirement 44.2)."""
    del min_len, missing_constraint
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    with pytest.raises(MeasurementDefinitionValidationError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=created.intended_outcome_revision_id,
            **{field: "x" * (max_len + 1)},
        )

    assert exc_info.value.failed_constraint == too_long_constraint
    assert _count(outcome_engine, "Measurement_Definitions") == 0


# ===========================================================================
# Requirement 44.3 — at most one Measurement Definition per Intended Outcome.
# ===========================================================================


def test_duplicate_definition_rejected_and_first_byte_equivalent(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """A second Measurement Definition against the same Intended Outcome
    Resource is rejected with nothing added and the first left
    byte-equivalent (Requirement 44.3)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )
    first = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
    )

    before = _snapshot_definitions(outcome_engine)

    with pytest.raises(MeasurementDefinitionDuplicateError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=created.intended_outcome_revision_id,
            measurand_description="A different measurand entirely.",
        )

    assert (
        exc_info.value.target_intended_outcome_resource_id
        == created.intended_outcome_id
    )
    assert (
        exc_info.value.existing_measurement_definition_id
        == first.measurement_definition_id
    )
    assert exc_info.value.failed_constraint == "duplicate_measurement_definition"

    # Exactly one Definition / Revision persisted and byte-equivalent.
    assert _count(outcome_engine, "Measurement_Definitions") == 1
    assert _count(outcome_engine, "Measurement_Definition_Revisions") == 1
    assert _snapshot_definitions(outcome_engine) == before


# ===========================================================================
# Requirement 53.2 — prohibited intended-side attribute rejection.
# ===========================================================================


@pytest.mark.parametrize(
    "prohibited_key",
    [
        "success-condition-statement",
        "success_condition_statement",
        "attribution-assumption-text",
        "planned-deliverable-id",
        "intended-outcome-value",
        "INTENDED-OUTCOME-VALUE",
    ],
)
def test_prohibited_intended_side_attribute_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    prohibited_key: str,
) -> None:
    """A request body carrying any intended-side attribute key is rejected
    at the boundary with no row persisted (Requirement 53.2)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )

    with pytest.raises(MeasurementDefinitionValidationError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=created.intended_outcome_revision_id,
            request_attributes={
                "measurand_description": _VALID_MEASURAND,
                prohibited_key: "anything",
            },
        )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert prohibited_key in exc_info.value.prohibited_keys
    assert _count(outcome_engine, "Measurement_Definitions") == 0


# ===========================================================================
# Requirement 52.6 — authorization deny path appends exactly one Denial Record.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """A Party without ``define_measurement`` is denied; no Resource or
    Revision is created and exactly one Denial Record is appended in a
    separate transaction (Requirement 52.6, AD-WS-9)."""
    # Seed the resolvable target but withhold the define_measurement grant.
    created = _seed_target_and_authority(
        intended_outcome_service,
        authorization_service,
        outcome_engine,
        grant_define_measurement=False,
    )

    with pytest.raises(MeasurementDefinitionAuthorizationError) as exc_info:
        _create_definition(
            measurement_definition_service,
            outcome_engine,
            target_revision_id=created.intended_outcome_revision_id,
            correlation_id="corr-deny",
        )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-deny"

    assert _count(outcome_engine, "Measurement_Definitions") == 0
    assert _count(outcome_engine, "Measurement_Definition_Revisions") == 0
    assert _count_consequential_audit_rows(outcome_engine) == 0
    # Exactly one Denial Record survives the caller's rollback (AD-WS-9).
    assert _count_denial_audit_rows(outcome_engine) == 1


# ===========================================================================
# Requirement 44.7 — the persisted Revision and Relationship are immutable.
# ===========================================================================


def test_persisted_revision_rejects_update_and_delete(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """The Measurement Definition Revision written by the service is
    immutable: UPDATE and DELETE are rejected by the schema triggers
    (Requirement 44.7, AD-WS-36)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )
    result = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
    )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Measurement_Definition_Revisions SET cadence='weekly' "
                    "WHERE measurement_definition_revision_id = :id"
                ),
                {"id": result.measurement_definition_revision_id},
            )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Measurement_Definition_Revisions "
                    "WHERE measurement_definition_revision_id = :id"
                ),
                {"id": result.measurement_definition_revision_id},
            )

    # The Revision survives both rejected mutations intact.
    assert _count(outcome_engine, "Measurement_Definition_Revisions") == 1


def test_persisted_addresses_relationship_rejects_update_and_delete(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """The ``Addresses`` Relationship written by the service is immutable
    (Requirement 44.7, AD-WS-36)."""
    created = _seed_target_and_authority(
        intended_outcome_service, authorization_service, outcome_engine
    )
    result = _create_definition(
        measurement_definition_service,
        outcome_engine,
        target_revision_id=created.intended_outcome_revision_id,
    )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Relationships SET semantic_role='x' "
                    "WHERE relationship_id = :id"
                ),
                {"id": result.addresses_relationship_id},
            )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Relationships WHERE relationship_id = :id"
                ),
                {"id": result.addresses_relationship_id},
            )

    assert (
        _count_addresses_relationships(
            outcome_engine, result.measurement_definition_id
        )
        == 1
    )
