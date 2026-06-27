"""Unit tests for the outcome-status Projection (fourth-walking-slice task
11.2).

Pins the contract established by task 11.1, design §"Outcome-status Projection
(single explainable Projection)", Requirement 59, and Principle 5.23:

- **59.1 / 59.2** — every exposed projected status is wrapped in the Slice 1
  :class:`walking_slice.projection.ProjectionEnvelope` carrying the Projection
  Definition, source Record + Revision Identities, the applicable temporal
  boundary and generated time (ISO-8601 ≥ second precision), and a derivation
  indicator (``"derived"``) distinguishing the status from authoritative source
  Records and from the Outcome Review Record itself.
- **status derivation** — the projected status progresses through
  ``unmeasured → measurement defined → measured → observed →
  success condition <...> → reviewed`` as partial Slice 4 evidence chains are
  seeded, picking the most-progressed label observed.
- **59.5** — an unresolvable Projection Definition withholds the status and
  returns an :class:`walking_slice.projection.ExplanationUnavailableResponse`
  naming the missing element; source Records remain byte-equivalent.
- **59.3** — :class:`OutcomeStatusProjection` never carries a prohibited derived
  value (percent-attainment, cost-per-outcome, ROI, budget-variance,
  forecast-attainment, causal-attribution probability, cross-Outcome
  aggregate).
- **59.6** — the Projection is never aliased as an Observed Outcome,
  Success-Condition Assessment, or Outcome Review.
- **AD-WS-9 / authority** — an unresolvable target and a target whose ``view``
  authority is denied both surface through the same
  :class:`OutcomeStatusTargetUnresolvableError` so the response is
  indistinguishable.
- **59.4 / read-only** — computing the Projection mutates no source row, even as
  new evidence arrives.

The fixture / seed-helper style mirrors
``tests/unit/test_outcome_success_condition_assessments.py`` (task 8.2). The
authorization paths are exercised against a real :class:`AuthorizationService`
(a viewer Party granted ``view`` and an unauthorized Party with no grant) rather
than a stub, so the real evaluation code path participates.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock, FixedClock
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome._projection import (
    OUTCOME_STATUS_PROJECTION_DEFINITION,
    OutcomeStatusProjection,
    OutcomeStatusTargetUnresolvableError,
    project_outcome_status,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
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
_VIEWER_PARTY_ID = "00000000-0000-7000-8000-000000a00006"
# A seeded Party holding no role grant, used for the view-denied path.
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-000000a00007"
_REVIEWER_PARTY_ID = "00000000-0000-7000-8000-000000a00008"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-0000000ba001"
_BASIS = AuthorityBasisRef(type="role-grant-id", id=UUID(_AUTHORITY_BASIS_ID))

# A syntactically valid identifier never minted into the schema.
_UNRESOLVABLE_REV_ID = "00000000-0000-7000-8000-0000000fffff"

# The instant the Projection is computed at; inside every seeded role's
# effective period and at second precision so the envelope validator accepts
# it as the applicable temporal boundary.
_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)

_UNIT = "percent"
_WINDOW_2025 = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW = datetime(2025, 6, 1, tzinfo=timezone.utc)

# Status label literals mirrored from _projection.py so the tests pin the exact
# externally observable strings.
_STATUS_UNMEASURED = "Intended Outcome unmeasured"
_STATUS_MEASUREMENT_DEFINED = "Intended Outcome measurement defined"
_STATUS_MEASURED = "Intended Outcome measured"
_STATUS_OBSERVED = "Intended Outcome observed"
_STATUS_REVIEWED = "Intended Outcome reviewed"
_ASSESSMENT_TO_STATUS = {
    "Satisfied": "Intended Outcome success condition satisfied",
    "Partially_Satisfied": (
        "Intended Outcome success condition partially satisfied"
    ),
    "Not_Satisfied": "Intended Outcome success condition not satisfied",
    "Unassessable": "Intended Outcome success condition unassessable",
}

# Field-name fragments that would constitute a prohibited derived value
# (Requirement 59.3) or alias the Projection as a source entity
# (Requirement 59.6).
_PROHIBITED_FIELD_FRAGMENTS = (
    "percent",
    "attainment",
    "cost",
    "roi",
    "budget",
    "variance",
    "forecast",
    "causal",
    "attribution",
    "aggregate",
    "observed_value",
    "review_outcome",
    "assessment_category",
)


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


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_outcome_success_condition_assessments).
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
        _seed_party(conn, _VIEWER_PARTY_ID, "Outcome Viewer")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")


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
    grant_view: bool = True,
) -> None:
    """Seed parties, the Objective, and the role grants the chain needs.

    Always grants ``modify`` (Intended Outcome owner), ``define_measurement``,
    ``record_measurement``, and ``assess_outcome`` so the evidence chain can be
    built. Grants ``view`` to the Viewer Party unless the view-denied path is
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
    _assign_role(
        authorization_service,
        engine,
        party_id=_RECORDER_PARTY_ID,
        role_name="measurement_recorder",
        authority="record_measurement",
    )
    _assign_role(
        authorization_service,
        engine,
        party_id=_ASSESSOR_PARTY_ID,
        role_name="outcome_assessor",
        authority="assess_outcome",
    )
    if grant_view:
        _assign_role(
            authorization_service,
            engine,
            party_id=_VIEWER_PARTY_ID,
            role_name="outcome_viewer",
            authority="view",
        )


# ---------------------------------------------------------------------------
# Pipeline builders. Each builds the evidence chain up to a given stage and
# returns the target Intended Outcome Revision Identity.
# ---------------------------------------------------------------------------


def _seed_intended_outcome(
    intended_outcome_service: IntendedOutcomeService, engine: Engine
):
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


def _add_measurement_definition(
    measurement_definition_service: MeasurementDefinitionService,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
):
    with engine.begin() as conn:
        return measurement_definition_service.create_measurement_definition(
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


def _add_measurement_record(
    measurement_record_service: MeasurementRecordService,
    engine: Engine,
    *,
    measurement_definition_revision_id: str,
):
    with engine.begin() as conn:
        return measurement_record_service.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=(
                measurement_definition_revision_id
            ),
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=_OBS_IN_WINDOW,
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )


def _add_observed_outcome(
    observed_outcome_service: ObservedOutcomeService,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    measurement_record_id: str,
):
    with engine.begin() as conn:
        return observed_outcome_service.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=[measurement_record_id],
            authoring_party_id=_ASSESSOR_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )


def _add_assessment(
    assessment_service: SuccessConditionAssessmentService,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    observed_outcome_revision_id: str,
    assessment_category: str,
):
    # The Unassessable category requires a rationale of >= 200 characters
    # (Requirement 48.3); a single comfortably-long rationale satisfies every
    # category so we reuse it for all four.
    rationale = (
        "Assessed against the recorded measurement evidence and the success "
        "condition. " * 6
    )
    with engine.begin() as conn:
        return assessment_service.create_assessment(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            sourced_observed_outcome_revision_id=observed_outcome_revision_id,
            assessment_category=assessment_category,
            assessment_rationale=rationale,
            assessing_party_id=_ASSESSOR_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
        )


def _insert_outcome_review(
    engine: Engine,
    *,
    intended_outcome_resource_id: str,
    intended_outcome_revision_id: str,
) -> str:
    """Directly insert one Outcome Review Record addressing the target.

    The Projection's "reviewed" leg only reads ``Outcome_Review_Records`` keyed
    on ``target_intended_outcome_revision_id`` (design step 6); a directly
    inserted row is sufficient to exercise it without standing up the Slice 3
    Completion graph the full Outcome Review Service requires.
    """
    review_id = "00000000-0000-7000-8000-0000000a0e01"
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Outcome_Review_Records (
                    outcome_review_id,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    review_outcome, attribution_stance, confidence,
                    review_rationale, attribution_evidence_reference,
                    reviewing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :rid, :res, :rev, 'Achieved', 'Asserted', 'High',
                    'Reviewed and concluded the outcome was achieved.',
                    'evidence://assessment-bundle', :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "rid": review_id,
                "res": intended_outcome_resource_id,
                "rev": intended_outcome_revision_id,
                "party": _REVIEWER_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
    return review_id


# ---------------------------------------------------------------------------
# Snapshot helper (read-only / byte-equivalence assertions).
# ---------------------------------------------------------------------------


_SOURCE_TABLES = (
    "Intended_Outcomes",
    "Intended_Outcome_Revisions",
    "Measurement_Definitions",
    "Measurement_Definition_Revisions",
    "Measurement_Records",
    "Observed_Outcomes",
    "Observed_Outcome_Revisions",
    "Success_Condition_Assessment_Records",
    "Outcome_Review_Records",
)


def _snapshot(engine: Engine) -> dict[str, list[tuple]]:
    snapshot: dict[str, list[tuple]] = {}
    with engine.connect() as conn:
        for table in _SOURCE_TABLES:
            rows = conn.execute(
                text(f"SELECT * FROM {table} ORDER BY 1")
            ).all()
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


def _project(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    *,
    intended_outcome_revision_id: str,
    party_id: str = _VIEWER_PARTY_ID,
    definition_registry=None,
):
    with outcome_engine.connect() as conn:
        return project_outcome_status(
            conn,
            intended_outcome_revision_id=intended_outcome_revision_id,
            party_id=party_id,
            at=_AT,
            authorization_service=authorization_service,
            clock=clock,
            definition_registry=definition_registry,
        )


# ===========================================================================
# Status derivation at each pipeline stage.
# ===========================================================================


def test_status_unmeasured_with_only_intended_outcome(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """An Intended Outcome with no Measurement Definition projects
    ``unmeasured`` (design step 2)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    assert result.projected_status == _STATUS_UNMEASURED
    assert (
        result.intended_outcome_revision_id
        == intended.intended_outcome_revision_id
    )


def test_status_measurement_defined_with_definition_only(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """Adding a Measurement Definition advances the status to
    ``measurement defined`` (design step 2/7)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    assert result.projected_status == _STATUS_MEASUREMENT_DEFINED


def test_status_measured_with_measurement_record(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """Adding a Measurement Record advances the status to ``measured``
    (design step 3/7)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    assert result.projected_status == _STATUS_MEASURED


def test_status_observed_with_observed_outcome(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """Adding an Observed Outcome advances the status to ``observed``
    (design step 4/7)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    assert result.projected_status == _STATUS_OBSERVED


@pytest.mark.parametrize(
    "category",
    ["Satisfied", "Partially_Satisfied", "Not_Satisfied", "Unassessable"],
)
def test_status_success_condition_per_assessment_category(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
    category: str,
) -> None:
    """Adding a Success-Condition Assessment advances the status to the
    success-condition label matching the most-recent Assessment's category
    (design step 5/7)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    observed = _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )
    _add_assessment(
        assessment_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        assessment_category=category,
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    assert result.projected_status == _ASSESSMENT_TO_STATUS[category]


def test_status_reviewed_with_outcome_review(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """Adding an Outcome Review on top of an Assessment advances the status to
    ``reviewed`` (design step 6/7)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    observed = _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )
    _add_assessment(
        assessment_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        assessment_category="Satisfied",
    )
    _insert_outcome_review(
        outcome_engine,
        intended_outcome_resource_id=intended.intended_outcome_id,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    assert result.projected_status == _STATUS_REVIEWED


# ===========================================================================
# Envelope contents and derivation indicator (Requirements 59.1, 59.2).
# ===========================================================================


def test_envelope_carries_every_required_field(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
) -> None:
    """The wrapping :class:`ProjectionEnvelope` carries the Projection
    Definition, source Resource + Revision Identities, the applicable temporal
    boundary and generated time (second precision), and the ``"derived"``
    derivation indicator (Requirements 59.1, 59.2)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    envelope = result.envelope
    assert isinstance(envelope, ProjectionEnvelope)
    # Projection Definition is the exported outcome-status definition.
    assert envelope.definition == OUTCOME_STATUS_PROJECTION_DEFINITION
    # Requirement 59.2 — the status is marked derived, not authoritative.
    assert envelope.derivation == "derived"
    # Requirement 59.1 — second-precision timestamps.
    assert envelope.applicable_temporal_boundary == _AT
    assert envelope.applicable_temporal_boundary.microsecond == 0
    assert envelope.generated_at == clock.now().replace(microsecond=0)
    assert envelope.generated_at.microsecond == 0
    # Requirement 59.1 — the source identity lists name the consulted Records.
    assert UUID(intended.intended_outcome_id) in envelope.source_resource_ids
    assert (
        UUID(intended.intended_outcome_revision_id)
        in envelope.source_revision_ids
    )
    assert (
        UUID(definition.measurement_definition_id)
        in envelope.source_resource_ids
    )
    assert (
        UUID(definition.measurement_definition_revision_id)
        in envelope.source_revision_ids
    )


# ===========================================================================
# Requirement 59.5 — unresolvable Projection Definition withholds the status.
# ===========================================================================


def test_unresolvable_definition_returns_explanation_unavailable(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
) -> None:
    """An empty Projection-Definition registry withholds the status and returns
    an :class:`ExplanationUnavailableResponse` naming the missing element; no
    source row changes (Requirement 59.5)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    before = _snapshot(outcome_engine)

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        # An empty registry omits the outcome-status definition, driving the
        # withholding path.
        definition_registry={},
    )

    assert isinstance(result, ExplanationUnavailableResponse)
    assert result.missing_element_kind == "projection_definition"
    assert "outcome.status" in result.missing_element_identifier
    # Requirement 59.5 — source Records remain byte-equivalent.
    assert _snapshot(outcome_engine) == before


def test_alternate_registry_without_outcome_status_withholds(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """A registry carrying some other definition but not the outcome-status one
    still drives the explanation-unavailable path (Requirement 59.5)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)

    other = ProjectionDefinition(name="some.other", version="1.0")
    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        definition_registry={"some.other": other},
    )

    assert isinstance(result, ExplanationUnavailableResponse)
    assert result.missing_element_kind == "projection_definition"


# ===========================================================================
# Authority / AD-WS-9 — unresolvable target and view-denied raise the same
# error.
# ===========================================================================


def test_unresolvable_target_raises(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
) -> None:
    """A target Intended Outcome Revision that does not resolve raises
    :class:`OutcomeStatusTargetUnresolvableError` (AD-WS-9)."""
    _seed_world(authorization_service, outcome_engine)

    with pytest.raises(OutcomeStatusTargetUnresolvableError):
        _project(
            outcome_engine,
            authorization_service,
            clock,
            intended_outcome_revision_id=_UNRESOLVABLE_REV_ID,
        )


def test_view_denied_raises_same_error_as_unresolvable(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """A requesting Party without ``view`` authority on the resolvable target
    raises the same :class:`OutcomeStatusTargetUnresolvableError`, so the
    response is indistinguishable from the unresolvable case (AD-WS-9)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)

    with pytest.raises(OutcomeStatusTargetUnresolvableError):
        _project(
            outcome_engine,
            authorization_service,
            clock,
            intended_outcome_revision_id=intended.intended_outcome_revision_id,
            party_id=_UNAUTHORIZED_PARTY_ID,
        )


# ===========================================================================
# Requirement 59.4 — read-only: no mutation, even as new evidence arrives.
# ===========================================================================


def test_projection_performs_no_mutation(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """Computing the Projection leaves every source table byte-equivalent
    (Requirement 59.4)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )

    before = _snapshot(outcome_engine)
    # Project several times to be sure repeated reads never perturb sources.
    for _ in range(3):
        _project(
            outcome_engine,
            authorization_service,
            clock,
            intended_outcome_revision_id=(
                intended.intended_outcome_revision_id
            ),
        )
    assert _snapshot(outcome_engine) == before


def test_source_records_byte_equivalent_when_new_evidence_arrives(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
) -> None:
    """When new evidence arrives the projected status advances, yet every
    previously-recorded source row is retained byte-equivalent — new facts are
    appended, never overwritten (Requirement 59.4)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )

    # Status at the "measured" stage, with a snapshot of the source tables.
    measured = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    assert isinstance(measured, OutcomeStatusProjection)
    assert measured.projected_status == _STATUS_MEASURED
    before = _snapshot(outcome_engine)

    # New evidence arrives: an Observed Outcome addressing the same target.
    _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )

    observed = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    assert isinstance(observed, OutcomeStatusProjection)
    # The status advanced.
    assert observed.projected_status == _STATUS_OBSERVED
    # Every previously-recorded row is still present byte-equivalent: the new
    # Observed Outcome rows are additive, not overwrites.
    after = _snapshot(outcome_engine)
    for table, rows in before.items():
        for row in rows:
            assert row in after[table]


# ===========================================================================
# Requirement 59.3 / 59.6 — no prohibited derived value; never aliased as a
# source entity.
# ===========================================================================


def test_projection_carries_no_prohibited_derived_field(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """The :class:`OutcomeStatusProjection` exposes only the target Revision
    Identity, the projected status label, and the envelope — never a
    percent-attainment, cost, ROI, budget-variance, forecast, causal-attribution
    probability, or cross-Outcome aggregate, and never an observed measurement /
    assessment / review value (Requirements 59.3, 59.6)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    observed = _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )
    _add_assessment(
        assessment_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        assessment_category="Satisfied",
    )

    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    assert isinstance(result, OutcomeStatusProjection)
    # The public surface is exactly the three documented fields.
    field_names = {f.name for f in dataclasses.fields(result)}
    assert field_names == {
        "intended_outcome_revision_id",
        "projected_status",
        "envelope",
    }
    # No field name hints at a prohibited derived value or a source-entity
    # alias, on the projection or its envelope.
    envelope_field_names = set(type(result.envelope).model_fields.keys())
    for name in field_names | envelope_field_names:
        lowered = name.lower()
        for fragment in _PROHIBITED_FIELD_FRAGMENTS:
            assert fragment not in lowered, (
                f"projection field {name!r} hints at prohibited content "
                f"{fragment!r}"
            )


def test_projection_not_aliased_as_observed_outcome_or_review(
    outcome_engine: Engine,
    authorization_service: AuthorizationService,
    clock: Clock,
    intended_outcome_service: IntendedOutcomeService,
    measurement_definition_service: MeasurementDefinitionService,
    measurement_record_service: MeasurementRecordService,
    observed_outcome_service: ObservedOutcomeService,
    assessment_service: SuccessConditionAssessmentService,
) -> None:
    """Computing the Projection at the reviewed stage neither creates nor
    aliases an Observed Outcome, Assessment, or Outcome Review: the result is an
    :class:`OutcomeStatusProjection` whose status is a label string, and the
    counts of the source-entity tables are unchanged by the projection
    (Requirement 59.6)."""
    _seed_world(authorization_service, outcome_engine)
    intended = _seed_intended_outcome(intended_outcome_service, outcome_engine)
    definition = _add_measurement_definition(
        measurement_definition_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    record = _add_measurement_record(
        measurement_record_service,
        outcome_engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    observed = _add_observed_outcome(
        observed_outcome_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )
    _add_assessment(
        assessment_service,
        outcome_engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        assessment_category="Satisfied",
    )
    _insert_outcome_review(
        outcome_engine,
        intended_outcome_resource_id=intended.intended_outcome_id,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )

    def _counts() -> dict[str, int]:
        with outcome_engine.connect() as conn:
            return {
                t: int(
                    conn.execute(
                        text(f"SELECT COUNT(*) FROM {t}")
                    ).scalar_one()
                )
                for t in (
                    "Observed_Outcomes",
                    "Observed_Outcome_Revisions",
                    "Success_Condition_Assessment_Records",
                    "Outcome_Review_Records",
                )
            }

    before = _counts()
    result = _project(
        outcome_engine,
        authorization_service,
        clock,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    after = _counts()

    assert isinstance(result, OutcomeStatusProjection)
    # The projected status is a derived label string, not a source entity.
    assert result.projected_status == _STATUS_REVIEWED
    assert isinstance(result.projected_status, str)
    # The projection neither persisted nor aliased a new source entity.
    assert after == before
