"""Unit tests for :class:`SuccessConditionAssessmentService` (fourth-walking-
slice task 8.2).

Pins the contract established by task 8.1, design
§"Outcome_Service.SuccessConditionAssessments", and Requirements 48.3, 48.4,
48.6, 48.7, 52.8:

- **48.3 (validation)** — the assessment category is drawn from
  ``{Satisfied, Partially_Satisfied, Not_Satisfied, Unassessable}``; the
  rationale is 1..4000 characters (0 and 4001 rejected) and at least 200
  characters when the category is ``Unassessable`` (199 rejected, 200
  accepted); the authority basis type is in the AD-WS-10 set; a sourced
  Observed Outcome Revision whose ``Addresses`` target differs from the named
  target Intended Outcome Revision is rejected. Each rejection persists
  nothing.
- **48.4 / 52.8** — ``create.success_condition_assessment`` requires
  ``assess_outcome`` (AD-WS-33); a Party without it is denied, no Record is
  created, and exactly one Denial Record is appended in a separate transaction
  (AD-WS-9).
- **48.6** — the persisted Success-Condition Assessment Record is immutable
  (UPDATE / DELETE rejected by the schema triggers).
- **48.7** — the addressed Intended Outcome Revision and the sourced Observed
  Outcome Revision are byte-equivalent after the assessment is recorded.
- one ``Addresses`` Relationship to the target Intended Outcome Revision
  (``semantic_role IS NULL``) plus one ``Cites`` Relationship to the sourced
  Observed Outcome Revision (``semantic_role = 'assessment_basis'``, AD-WS-35).

The tests mirror the fixture / seed-helper style of
``tests/unit/test_outcome_observed_outcomes.py`` (task 7.2's tests). The
authorization deny path is exercised by assessing as a Party without the
``assess_outcome`` role rather than by swapping in a stub, so the real
evaluation code path participates.
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
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.models import CreateAssessmentResult
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentAuthorizationError,
    SuccessConditionAssessmentService,
    SuccessConditionAssessmentSourcingError,
    SuccessConditionAssessmentValidationError,
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
# A seeded Party holding no role grant, used for the authorization deny path.
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-000000a00006"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_AUTHORITY_BASIS_ID = UUID("00000000-0000-7000-8000-0000000ba001")
_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

# A syntactically valid identifier never minted into the schema.
_UNRESOLVABLE_REV_ID = "00000000-0000-7000-8000-0000000fffff"

_ACTION = "create.success_condition_assessment"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_UNIT = "percent"
_WINDOW_2025 = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW = datetime(2025, 6, 1, tzinfo=timezone.utc)

# A non-Unassessable rationale comfortably inside 1..4000.
_RATIONALE = "Measured adoption met the success threshold."


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
    """Slice 2 service used to seed targets and as the
    ``intended_outcome_reader`` of the Assessment service."""
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
    """Slice 4 Measurement Definition service used to seed Definitions."""
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
    """Slice 4 Measurement Record service used to seed citable Records."""
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
    """Slice 4 Observed Outcome service used to seed sourceable Observed
    Outcome Revisions and as the ``observed_outcome_reader`` of the Assessment
    service."""
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
    """SuccessConditionAssessmentService wired with real readers and a real
    AuthorizationService.

    The deny path is exercised by assessing as a Party without the
    ``assess_outcome`` role rather than by swapping in a stub, so the real
    evaluation code path participates in the test.
    """
    return SuccessConditionAssessmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        observed_outcome_reader=observed_outcome_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_outcome_observed_outcomes.py).
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
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")


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
    so targets and sourceable Observed Outcomes can be created. Grants
    ``assess_outcome`` to the Assessor unless the deny path is being
    exercised.
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
class _Assessable:
    """A fully-seeded assessable target: an Intended Outcome Revision and an
    Observed Outcome Revision that addresses it (sourced through a Measurement
    Definition + Measurement Record)."""

    intended_outcome_revision_id: str
    intended_outcome_id: str
    observed_outcome_revision_id: str
    observed_outcome_id: str


def _make_assessable(
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    engine: Engine,
) -> _Assessable:
    """Seed one Intended Outcome, its single addressing Measurement Definition,
    one anchored Measurement Record, and one Observed Outcome Revision that
    addresses the Intended Outcome and cites the Measurement Record.

    Each call mints a fresh Intended Outcome so callers may create several
    independent assessables.
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
    return _Assessable(
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        intended_outcome_id=intended.intended_outcome_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        observed_outcome_id=observed.observed_outcome_id,
    )


def _create_assessment(
    assessment_service: SuccessConditionAssessmentService,
    engine: Engine,
    *,
    target_intended_outcome_revision_id: str,
    sourced_observed_outcome_revision_id: str,
    assessment_category: str = "Satisfied",
    assessment_rationale: str = _RATIONALE,
    assessing_party_id: str = _ASSESSOR_PARTY_ID,
    authority_basis=_BASIS,
    applicable_scope: str = _SCOPE,
    request_attributes=None,
    correlation_id: Optional[str] = None,
) -> CreateAssessmentResult:
    with engine.begin() as conn:
        return assessment_service.create_assessment(
            conn,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            assessment_category=assessment_category,
            assessment_rationale=assessment_rationale,
            assessing_party_id=assessing_party_id,
            authority_basis=authority_basis,
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


def _assessment_row(engine: Engine, assessment_id: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT * FROM Success_Condition_Assessment_Records "
                    "WHERE assessment_id = :id"
                ),
                {"id": assessment_id},
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


# ---------------------------------------------------------------------------
# Duck-typed authority basis whose ``type`` is outside the AD-WS-10 set.
#
# ``AuthorityBasisRef`` rejects out-of-enumeration types at construction, so
# the service-level ``authority_basis_type_invalid`` branch (Requirement 48.3,
# AD-WS-41) is exercised with a controlled object carrying a bad ``type``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _BadBasis:
    type: str = "party-id"
    id: UUID = _AUTHORITY_BASIS_ID


# ===========================================================================
# Happy-path baseline — confirms the wiring before the negative paths run.
# ===========================================================================


def test_create_assessment_one_addresses_and_one_cites(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """With ``assess_outcome`` authority, a resolvable ``intended`` target, and
    a sourced Observed Outcome Revision addressing that target, the service
    creates one immutable Record, exactly one ``Addresses`` Relationship, one
    ``Cites`` Relationship, and one consequential audit row (Requirements 48.1,
    48.2, 48.5, 52.8, 57.1, AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
        correlation_id="corr-assessment",
    )

    assert isinstance(result, CreateAssessmentResult)
    assert _CANONICAL_UUID7.match(result.assessment_id)
    assert result.assessment_category == "Satisfied"
    assert result.correlation_id == "corr-assessment"
    assert result.authority_basis == _BASIS

    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 1
    assert _count_consequential_audit_rows(outcome_engine) == 1
    assert _count_denial_audit_rows(outcome_engine) == 0
    assert (
        _count_relationships(
            outcome_engine, rel_type="Addresses", source_id=result.assessment_id
        )
        == 1
    )
    assert (
        _count_relationships(
            outcome_engine, rel_type="Cites", source_id=result.assessment_id
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
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """The ``Addresses`` Relationship targets the Intended Outcome Revision
    with ``semantic_role IS NULL``; the single ``Cites`` Relationship targets
    the sourced Observed Outcome Revision with
    ``semantic_role = 'assessment_basis'`` (AD-WS-35)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
    )

    with outcome_engine.connect() as conn:
        addresses = (
            conn.execute(
                text(
                    "SELECT source_revision_id, target_id, target_revision_id, "
                    "semantic_role, source_kind, target_kind FROM Relationships "
                    "WHERE relationship_type = 'Addresses' AND source_id = :sid"
                ),
                {"sid": result.assessment_id},
            )
            .mappings()
            .all()
        )
        cites = (
            conn.execute(
                text(
                    "SELECT target_id, target_revision_id, semantic_role, "
                    "source_kind, target_kind FROM Relationships "
                    "WHERE relationship_type = 'Cites' AND source_id = :sid"
                ),
                {"sid": result.assessment_id},
            )
            .mappings()
            .all()
        )

    assert len(addresses) == 1
    addr = addresses[0]
    # The Assessment Record is an Immutable Record, so the source has no
    # Revision.
    assert addr["source_revision_id"] is None
    assert addr["target_id"] == assessable.intended_outcome_id
    assert addr["target_revision_id"] == assessable.intended_outcome_revision_id
    assert addr["semantic_role"] is None
    assert addr["source_kind"] == "success_condition_assessment_record"
    assert addr["target_kind"] == "intended_outcome_revision"

    assert len(cites) == 1
    cite = cites[0]
    assert cite["target_id"] == assessable.observed_outcome_id
    assert cite["target_revision_id"] == assessable.observed_outcome_revision_id
    assert cite["semantic_role"] == "assessment_basis"
    assert cite["source_kind"] == "success_condition_assessment_record"
    assert cite["target_kind"] == "observed_outcome_revision"


# ===========================================================================
# Requirement 48.3 — assessment-category enumeration boundaries.
# ===========================================================================


@pytest.mark.parametrize(
    "category",
    ["Satisfied", "Partially_Satisfied", "Not_Satisfied"],
)
def test_each_enumerated_category_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    category: str,
) -> None:
    """Each non-``Unassessable`` enumerated category is accepted with a
    standard rationale (``Unassessable`` is covered by its own >= 200 rule
    test) (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
        assessment_category=category,
    )

    assert result.assessment_category == category
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 1


def test_out_of_enumeration_category_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """An assessment category outside the enumerated set is rejected with
    nothing persisted (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentValidationError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            assessment_category="Inconclusive",
        )

    assert exc_info.value.failed_constraint == "assessment_category_invalid"
    assert "assessment_category" in exc_info.value.invalid_attributes
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0


# ===========================================================================
# Requirement 48.3 — rationale length boundaries (1, 4000 ok; 0, 4001 reject).
# ===========================================================================


@pytest.mark.parametrize("length", [1, 4000])
def test_rationale_length_boundaries_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    length: int,
) -> None:
    """A rationale of exactly 1 and exactly 4000 characters is accepted for a
    non-``Unassessable`` category (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
        assessment_rationale="x" * length,
    )

    assert len(result.assessment_rationale) == length
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 1


def test_empty_rationale_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """A zero-length rationale is rejected as missing (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentValidationError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            assessment_rationale="",
        )

    assert exc_info.value.failed_constraint == "assessment_rationale_missing"
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0


def test_rationale_exceeding_4000_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """A rationale of 4001 characters is rejected as too long
    (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentValidationError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            assessment_rationale="x" * 4001,
        )

    assert exc_info.value.failed_constraint == "assessment_rationale_too_long"
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0


# ===========================================================================
# Requirement 48.3 — Unassessable >= 200-char rule (199 reject, 200 accept).
# ===========================================================================


def test_unassessable_rationale_below_200_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """An ``Unassessable`` assessment with a 199-character rationale is
    rejected (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentValidationError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            assessment_category="Unassessable",
            assessment_rationale="x" * 199,
        )

    assert exc_info.value.failed_constraint == (
        "assessment_rationale_too_short_for_unassessable"
    )
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0


def test_unassessable_rationale_at_200_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """An ``Unassessable`` assessment with a 200-character rationale is
    accepted (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
        assessment_category="Unassessable",
        assessment_rationale="x" * 200,
    )

    assert result.assessment_category == "Unassessable"
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 1


# ===========================================================================
# Requirement 48.3 / AD-WS-41 — authority-basis-type enumeration.
# ===========================================================================


def test_missing_authority_basis_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """A missing authority basis is rejected (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentValidationError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            authority_basis=None,
        )

    assert exc_info.value.failed_constraint == "authority_basis_missing"
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0


def test_authority_basis_type_outside_ad_ws_10_set_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """An authority basis whose ``type`` is outside the AD-WS-10 set is
    rejected (Requirement 48.3, AD-WS-41)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentValidationError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            authority_basis=_BadBasis(),
        )

    assert exc_info.value.failed_constraint == "authority_basis_type_invalid"
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0


@pytest.mark.parametrize(
    "basis_type", ["role-grant-id", "scope-id", "delegation-chain-id"]
)
def test_each_ad_ws_10_authority_basis_type_accepted(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    basis_type: str,
) -> None:
    """All three AD-WS-10 authority-basis types are accepted
    (Requirement 48.3, AD-WS-41)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
        authority_basis=AuthorityBasisRef(
            type=basis_type, id=_AUTHORITY_BASIS_ID
        ),
    )

    assert result.authority_basis.type == basis_type
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 1


# ===========================================================================
# Requirement 48.3 — sourced Observed Outcome Revision addressing mismatch.
# ===========================================================================


def test_sourced_observed_outcome_addressing_other_target_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """A sourced Observed Outcome Revision whose ``Addresses`` target differs
    from the named target Intended Outcome Revision is rejected with nothing
    persisted (Requirement 48.3).

    Two independent assessables are seeded; the assessment names target A but
    sources the Observed Outcome Revision that addresses target B.
    """
    _seed_world(authorization_service, outcome_engine)
    assessable_a = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )
    assessable_b = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentSourcingError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable_a.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable_b.observed_outcome_revision_id
            ),
        )

    assert exc_info.value.failed_constraint == (
        "sourced_observed_outcome_addresses_mismatch"
    )
    assert exc_info.value.named_target_revision_id == (
        assessable_a.intended_outcome_revision_id
    )
    assert exc_info.value.sourced_addresses_target_revision_id == (
        assessable_b.intended_outcome_revision_id
    )
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0
    assert _count_denial_audit_rows(outcome_engine) == 0


def test_unresolvable_sourced_observed_outcome_rejected(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """A sourced Observed Outcome Revision that does not resolve is rejected
    with nothing persisted (Requirement 48.3)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(SuccessConditionAssessmentSourcingError) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=_UNRESOLVABLE_REV_ID,
        )

    assert exc_info.value.failed_constraint == (
        "sourced_observed_outcome_revision_not_resolvable"
    )
    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0


# ===========================================================================
# Requirement 48.7 — addressed Intended Outcome Revision and sourced Observed
# Outcome Revision byte-equivalent after assessment.
# ===========================================================================


def test_target_and_source_byte_equivalent_after_assessment(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """Recording an assessment leaves the addressed Intended Outcome Revision
    and the sourced Observed Outcome Revision byte-equivalent
    (Requirement 48.7)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    intended_before = _intended_revision_row(
        outcome_engine, assessable.intended_outcome_revision_id
    )
    observed_before = _observed_revision_row(
        outcome_engine, assessable.observed_outcome_revision_id
    )

    _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
    )

    assert (
        _intended_revision_row(
            outcome_engine, assessable.intended_outcome_revision_id
        )
        == intended_before
    )
    assert (
        _observed_revision_row(
            outcome_engine, assessable.observed_outcome_revision_id
        )
        == observed_before
    )


# ===========================================================================
# Requirement 48.4 / 52.8 — authorization deny path.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """A Party without ``assess_outcome`` is denied; no Record is created and
    exactly one Denial Record is appended in a separate transaction
    (Requirements 48.4, 52.8, AD-WS-9).

    The sourced Observed Outcome is seeded by the granted Assessor, but the
    assessment is attempted by a distinct Party holding no role grant, so the
    deny path exercises real evaluation (``Role_Assignments`` is append-only,
    so authority is withheld rather than revoked).
    """
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )

    with pytest.raises(
        SuccessConditionAssessmentAuthorizationError
    ) as exc_info:
        _create_assessment(
            assessment_service,
            outcome_engine,
            target_intended_outcome_revision_id=(
                assessable.intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                assessable.observed_outcome_revision_id
            ),
            assessing_party_id=_UNAUTHORIZED_PARTY_ID,
            correlation_id="corr-deny",
        )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-deny"

    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 0
    assert _count_consequential_audit_rows(outcome_engine) == 0
    assert _count_denial_audit_rows(outcome_engine) == 1


# ===========================================================================
# Requirement 48.6 — immutability of the persisted Assessment Record.
# ===========================================================================


def test_persisted_assessment_record_rejects_update_and_delete(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """The Success-Condition Assessment Record written by the service is
    immutable: UPDATE and DELETE are rejected by the schema triggers
    (Requirement 48.6)."""
    _seed_world(authorization_service, outcome_engine)
    assessable = _make_assessable(
        intended_outcome_service,
        measurement_definition_service,
        measurement_record_service,
        observed_outcome_service,
        outcome_engine,
    )
    result = _create_assessment(
        assessment_service,
        outcome_engine,
        target_intended_outcome_revision_id=(
            assessable.intended_outcome_revision_id
        ),
        sourced_observed_outcome_revision_id=(
            assessable.observed_outcome_revision_id
        ),
    )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Success_Condition_Assessment_Records "
                    "SET assessment_rationale = 'tampered' "
                    "WHERE assessment_id = :id"
                ),
                {"id": result.assessment_id},
            )

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Success_Condition_Assessment_Records "
                    "WHERE assessment_id = :id"
                ),
                {"id": result.assessment_id},
            )

    assert _count(outcome_engine, "Success_Condition_Assessment_Records") == 1
    # The persisted row is unchanged.
    assert (
        _assessment_row(outcome_engine, result.assessment_id)[
            "assessment_rationale"
        ]
        == _RATIONALE
    )
