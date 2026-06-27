# Feature: fourth-walking-slice, Property 52: Outcome-status Projection envelope and contents
"""Property 52 — Outcome-status Projection envelope and contents (task 15.7).

**Property 52: Outcome-status Projection envelope and contents**

*For all* status-bearing responses returned by the Outcome_Service that
surface a derived outcome status (``Intended Outcome unmeasured``,
``Intended Outcome measurement defined``, ``Intended Outcome measured``,
``Intended Outcome observed``, the four ``Intended Outcome success
condition <...>`` labels, ``Intended Outcome reviewed``, or ``Provenance
incomplete``), the response body contains a
:class:`~walking_slice.projection.ProjectionEnvelope` carrying the
Projection Definition, source Record Identities, source Revision
Identities, applicable temporal boundary (ISO-8601 ≥ second precision),
generated time, and a derivation indicator distinguishing the status from
authoritative source Records and from the Outcome Review Record itself. The
response contains no derived percent-attainment, cost-per-outcome, ROI,
budget-variance, forecast-attainment, causal-attribution probability, or
cross-Outcome aggregate value, and no field that would constitute an
observed measurement, Observed Outcome value, or success-condition
assessment. On an unresolvable Projection Definition the response withholds
the status and returns an explanation-unavailable indicator naming the
missing element; source Records remain byte-equivalent. The projected
status is never aliased as an Observed Outcome, Success-Condition
Assessment, or Outcome Review.

**Validates: Requirements 59.1, 59.2, 59.3, 59.4, 59.5, 59.6**

Strategy
========

Each Hypothesis case draws:

- a *pipeline stage* sampled from the closed enumeration of nine
  status-producing configurations (:data:`_ALL_STAGES`). Each stage names a
  fully-seeded Slice 1 + Slice 2 + Slice 4 graph that drives
  :func:`project_outcome_status` down a distinct branch of the seven-step
  Projection Definition from
  ``.kiro/specs/fourth-walking-slice/design.md`` §"Outcome-status
  Projection".
- the per-case applicable temporal boundary (UTC, second precision — the
  envelope validator requires this canonical form). The window starts well
  after every seeded role's effective-start so the projecting Party's
  ``view`` authority resolves for every drawn boundary.
- the per-case clock instant the Projection stamps as ``generated_at`` on
  the envelope (UTC, second precision).
- the per-case *registry kind* drawn from ``{"registered",
  "empty_registry"}``. The ``"empty_registry"`` kind exercises Requirement
  59.5 (unresolvable Projection Definition); the ``"registered"`` kind
  exercises Requirements 59.1, 59.2, 59.3, 59.6 on every status-bearing
  response.

For each case the test:

1. Spins up a fresh per-case SQLite engine carrying Slice 1 + Slice 2
   (Planning) + Slice 4 (Outcome) schemas on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case identifier,
   audit, and resource state cannot leak. Fresh services per case keep
   :class:`IdentityService` in-memory state from bleeding across shrinks.
2. Seeds the per-stage evidence chain through the *real* Slice 2 / Slice 4
   services (the Outcome Review leg is a direct INSERT, mirroring
   ``tests/unit/test_outcome_projection.py``) so the anchoring rows exist
   exactly as production would write them.
3. Snapshots every consulted Slice 2 + Slice 4 source table.
4. Calls :func:`project_outcome_status` with the per-case boundary, clock,
   and registry inside a read-only connection.
5. Asserts the universal invariants of Property 52 (see the test
   docstring).

``@settings(max_examples=120, deadline=None)`` keeps the case count safely
above the 100-case floor for the task; each case allocates a fresh on-disk
SQLite database carrying three schemas and seeds a per-stage pipeline, so
``HealthCheck.too_slow`` and ``HealthCheck.data_too_large`` are suppressed.
"""

from __future__ import annotations

import dataclasses
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Iterator, Literal
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock
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


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and seed values — the deterministic prerequisite chain.
# Property 52 asserts the envelope-shape / contents / withholding / read-only
# invariants, so deterministic prerequisite IDs keep shrunken counterexamples
# focused on the per-case stage / boundary / clock / registry dimensions.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_OWNER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00005"
_VIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00006"
_REVIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00008"

_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID: Final[str] = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-000000c00003"
_SCOPE: Final[str] = "pilot/team-a"

_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-0000000ba001"
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=UUID(_AUTHORITY_BASIS_ID)
)
_OUTCOME_REVIEW_ID: Final[str] = "00000000-0000-7000-8000-0000000a0e01"

_UNIT: Final[str] = "percent"
# ISO-8601 closed window covering the 2025 observation instant the seeded
# Measurement Record draws; both edges precede the fixed recorded time.
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBS_IN_WINDOW: Final[datetime] = datetime(2025, 6, 1, tzinfo=timezone.utc)

# Role grants start far in the past so every drawn temporal boundary lands
# inside the projecting Party's effective ``view`` window; otherwise the
# authority evaluation at ``at`` would deny and the projection would raise.
_ROLE_EFFECTIVE_START: Final[datetime] = datetime(
    1999, 1, 1, tzinfo=timezone.utc
)


# ---------------------------------------------------------------------------
# Status labels (mirrored from _projection.py so the test pins the exact
# externally observable strings) and the published membership set.
# ---------------------------------------------------------------------------


_STATUS_UNMEASURED: Final[str] = "Intended Outcome unmeasured"
_STATUS_MEASUREMENT_DEFINED: Final[str] = "Intended Outcome measurement defined"
_STATUS_MEASURED: Final[str] = "Intended Outcome measured"
_STATUS_OBSERVED: Final[str] = "Intended Outcome observed"
_STATUS_REVIEWED: Final[str] = "Intended Outcome reviewed"
_STATUS_SC_SATISFIED: Final[str] = "Intended Outcome success condition satisfied"
_STATUS_SC_PARTIALLY: Final[str] = (
    "Intended Outcome success condition partially satisfied"
)
_STATUS_SC_NOT: Final[str] = "Intended Outcome success condition not satisfied"
_STATUS_SC_UNASSESSABLE: Final[str] = (
    "Intended Outcome success condition unassessable"
)
_STATUS_PROVENANCE_INCOMPLETE: Final[str] = "Provenance incomplete"

_PUBLISHED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        _STATUS_UNMEASURED,
        _STATUS_MEASUREMENT_DEFINED,
        _STATUS_MEASURED,
        _STATUS_OBSERVED,
        _STATUS_SC_SATISFIED,
        _STATUS_SC_PARTIALLY,
        _STATUS_SC_NOT,
        _STATUS_SC_UNASSESSABLE,
        _STATUS_REVIEWED,
        _STATUS_PROVENANCE_INCOMPLETE,
    }
)


# ---------------------------------------------------------------------------
# Prohibited field-name substrings (Requirements 59.3 and 59.6).
#
# Asserted against every key reachable from the serialized envelope payload
# and against the projection's own field names, so a regression that adds a
# key carrying one of these substrings on any branch is caught by every case.
# Requirement 59.3 — derived metrics this slice MUST NOT surface.
# Requirement 59.6 — observed-measurement / source-entity aliases.
# ---------------------------------------------------------------------------


_PROHIBITED_FIELD_FRAGMENTS: Final[tuple[str, ...]] = (
    # Requirement 59.3 — prohibited derived values.
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
    # Requirement 59.6 — observed-measurement / source-entity aliases.
    "observed_value",
    "measurement_value",
    "review_outcome",
    "assessment_category",
)


# ---------------------------------------------------------------------------
# Pipeline-stage enumeration.
#
# Each stage names a fully-seeded Slice 1 + Slice 2 + Slice 4 graph that
# drives :func:`project_outcome_status` down a distinct branch of the
# seven-step Projection Definition. The enumeration is closed: adding a stage
# requires updating the seeding switch and the expected-status map together so
# a regression where a new branch escapes coverage cannot merge silently.
# ---------------------------------------------------------------------------


_Stage = Literal[
    "unmeasured",
    "measurement_defined",
    "measured",
    "observed",
    "assessed_satisfied",
    "assessed_partially_satisfied",
    "assessed_not_satisfied",
    "assessed_unassessable",
    "reviewed",
]

_ALL_STAGES: Final[tuple[_Stage, ...]] = (
    "unmeasured",
    "measurement_defined",
    "measured",
    "observed",
    "assessed_satisfied",
    "assessed_partially_satisfied",
    "assessed_not_satisfied",
    "assessed_unassessable",
    "reviewed",
)

# Expected projected status per stage. Consulted only on the ``registered``
# registry path; on ``empty_registry`` every response is withheld regardless
# of the seeded pipeline.
_EXPECTED_STATUS_BY_STAGE: Final[dict[_Stage, str]] = {
    "unmeasured": _STATUS_UNMEASURED,
    "measurement_defined": _STATUS_MEASUREMENT_DEFINED,
    "measured": _STATUS_MEASURED,
    "observed": _STATUS_OBSERVED,
    "assessed_satisfied": _STATUS_SC_SATISFIED,
    "assessed_partially_satisfied": _STATUS_SC_PARTIALLY,
    "assessed_not_satisfied": _STATUS_SC_NOT,
    "assessed_unassessable": _STATUS_SC_UNASSESSABLE,
    "reviewed": _STATUS_REVIEWED,
}

# Stage → the Success-Condition Assessment category to seed (assessment and
# reviewed stages only).
_ASSESSMENT_CATEGORY_BY_STAGE: Final[dict[_Stage, str]] = {
    "assessed_satisfied": "Satisfied",
    "assessed_partially_satisfied": "Partially_Satisfied",
    "assessed_not_satisfied": "Not_Satisfied",
    "assessed_unassessable": "Unassessable",
    "reviewed": "Satisfied",
}


# ---------------------------------------------------------------------------
# Per-case engine builder. Each Hypothesis case builds a fresh SQLite engine
# on a unique temp-dir path so cross-case state cannot leak between generated
# inputs; the engine carries Slice 1 + Slice 2 (Planning) + Slice 4 (Outcome).
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
    create_outcome_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Per-case service bundle. Fresh per Hypothesis case so :class:`IdentityService`
# in-memory state cannot bleed across shrinks.
# ---------------------------------------------------------------------------


class _Services:
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


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_outcome_projection.py).
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
        effective_start=_ROLE_EFFECTIVE_START,
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
        _seed_party(conn, _VIEWER_PARTY_ID, "Outcome Viewer")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")
    _seed_objective(engine)
    for party_id, role_name, authority in (
        (_OWNER_PARTY_ID, "intended_outcome_owner", "modify"),
        (_DEFINER_PARTY_ID, "measurement_definer", "define_measurement"),
        (_RECORDER_PARTY_ID, "measurement_recorder", "record_measurement"),
        (_ASSESSOR_PARTY_ID, "outcome_assessor", "assess_outcome"),
        (_VIEWER_PARTY_ID, "outcome_viewer", "view"),
    ):
        _assign_role(
            svc.authz,
            engine,
            party_id=party_id,
            role_name=role_name,
            authority=authority,
        )


def _seed_intended_outcome(svc: _Services, engine: Engine):
    with engine.begin() as conn:
        return svc.intended.create_intended_outcome(
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
    svc: _Services, engine: Engine, *, intended_outcome_revision_id: str
):
    with engine.begin() as conn:
        return svc.definitions.create_measurement_definition(
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
    svc: _Services, engine: Engine, *, measurement_definition_revision_id: str
):
    with engine.begin() as conn:
        return svc.records.create_native_measurement(
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
    svc: _Services,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    measurement_record_id: str,
):
    with engine.begin() as conn:
        return svc.observed.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=[measurement_record_id],
            authoring_party_id=_ASSESSOR_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )


def _add_assessment(
    svc: _Services,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    observed_outcome_revision_id: str,
    assessment_category: str,
):
    # The Unassessable category requires a rationale of >= 200 characters
    # (Requirement 48.3); a single comfortably-long rationale satisfies
    # every category, so we reuse it for all four.
    rationale = (
        "Assessed against the recorded measurement evidence and the success "
        "condition. " * 6
    )
    with engine.begin() as conn:
        return svc.assessments.create_assessment(
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
) -> None:
    """Directly insert one Outcome Review Record addressing the target.

    The Projection's "reviewed" leg only reads ``Outcome_Review_Records``
    keyed on ``target_intended_outcome_revision_id`` (design step 6); a
    directly inserted row exercises it without standing up the Slice 3
    Completion graph the full Outcome Review Service requires.
    """
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
                "rid": _OUTCOME_REVIEW_ID,
                "res": intended_outcome_resource_id,
                "rev": intended_outcome_revision_id,
                "party": _REVIEWER_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_stage(svc: _Services, engine: Engine, stage: _Stage):
    """Seed the evidence chain up to ``stage`` and return the target Intended
    Outcome (with ``.intended_outcome_id`` and ``.intended_outcome_revision_id``).
    """
    intended = _seed_intended_outcome(svc, engine)
    if stage == "unmeasured":
        return intended

    definition = _add_measurement_definition(
        svc,
        engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    if stage == "measurement_defined":
        return intended

    record = _add_measurement_record(
        svc,
        engine,
        measurement_definition_revision_id=(
            definition.measurement_definition_revision_id
        ),
    )
    if stage == "measured":
        return intended

    observed = _add_observed_outcome(
        svc,
        engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        measurement_record_id=record.measurement_record_id,
    )
    if stage == "observed":
        return intended

    _add_assessment(
        svc,
        engine,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
        observed_outcome_revision_id=observed.observed_outcome_revision_id,
        assessment_category=_ASSESSMENT_CATEGORY_BY_STAGE[stage],
    )
    if stage in {
        "assessed_satisfied",
        "assessed_partially_satisfied",
        "assessed_not_satisfied",
        "assessed_unassessable",
    }:
        return intended

    # stage == "reviewed"
    _insert_outcome_review(
        engine,
        intended_outcome_resource_id=intended.intended_outcome_id,
        intended_outcome_revision_id=intended.intended_outcome_revision_id,
    )
    return intended


# ---------------------------------------------------------------------------
# Snapshot helper (read-only / byte-equivalence assertions, Requirement 59.4).
# Mirrors the source-table set used by the Slice 4 unit tests; deliberately
# excludes Audit_Records / Relationships, which the authority evaluation may
# touch on its own (uncommitted) connection.
# ---------------------------------------------------------------------------


_SOURCE_TABLES: Final[tuple[str, ...]] = (
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


def _source_entity_counts(engine: Engine) -> dict[str, int]:
    """Counts of the source-entity tables a Projection must never create or
    alias into (Requirement 59.6)."""
    with engine.connect() as conn:
        return {
            t: int(
                conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()
            )
            for t in (
                "Observed_Outcomes",
                "Observed_Outcome_Revisions",
                "Success_Condition_Assessment_Records",
                "Outcome_Review_Records",
            )
        }


def _collect_keys(node: Any) -> Iterator[str]:
    """Yield every dict key reachable from ``node`` (recursively into lists/
    tuples) so a forbidden key nested in a sub-structure is also caught."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _collect_keys(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _collect_keys(item)


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


# ``sorted`` keeps the draw order deterministic across Python versions so the
# Hypothesis shrink corpus is stable.
_STAGE_STRATEGY: Final[st.SearchStrategy[_Stage]] = st.sampled_from(
    sorted(_ALL_STAGES)
)


# Applicable temporal boundary. :class:`ProjectionEnvelope` requires UTC
# tzinfo with ``microsecond == 0``; the ``.map`` step truncates sub-second
# precision so the generated value is always acceptable. The 2001..2030
# window sits entirely inside the role's effective ``view`` period.
_BOUNDARY_STRATEGY: Final[st.SearchStrategy[datetime]] = st.datetimes(
    min_value=datetime(2001, 1, 1, 0, 0, 0),
    max_value=datetime(2030, 12, 31, 23, 59, 59),
    timezones=st.just(timezone.utc),
).map(lambda dt: dt.replace(microsecond=0))


# Generated time the Projection stamps on every envelope. Same canonical-form
# constraint as the boundary strategy.
_CLOCK_INSTANT_STRATEGY: Final[st.SearchStrategy[datetime]] = _BOUNDARY_STRATEGY


_RegistryKind = Literal["registered", "empty_registry"]
_REGISTRY_KIND_STRATEGY: Final[st.SearchStrategy[_RegistryKind]] = (
    st.sampled_from(["registered", "empty_registry"])
)


# ---------------------------------------------------------------------------
# Property 52 — the universal envelope-shape / contents invariant over
# generated pipeline stages.
# ---------------------------------------------------------------------------


# Feature: fourth-walking-slice, Property 52: Outcome-status Projection envelope and contents
# Validates: Requirements 59.1, 59.2, 59.3, 59.4, 59.5, 59.6
@given(
    stage=_STAGE_STRATEGY,
    boundary=_BOUNDARY_STRATEGY,
    clock_instant=_CLOCK_INSTANT_STRATEGY,
    registry_kind=_REGISTRY_KIND_STRATEGY,
)
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup spins up a fresh on-disk SQLite database carrying three
    # schemas (Slice 1 + Slice 2 + Slice 4) and seeds a per-stage pipeline, so
    # the per-case work is heavier than a pure in-memory property test;
    # ``too_slow`` / ``data_too_large`` are suppressed so any one slow case does
    # not abort the run while the 2000 ms per-example deadline still applies
    # (Requirement 61.15).
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_outcome_status_projection_envelope_and_contents(
    stage: _Stage,
    boundary: datetime,
    clock_instant: datetime,
    registry_kind: _RegistryKind,
) -> None:
    """For every generated outcome-status Projection:

    - **Requirement 59.5 (unresolvable Projection Definition).** When the
      registry omits the outcome-status definition, the response is an
      :class:`ExplanationUnavailableResponse` whose ``missing_element_kind``
      is ``"projection_definition"`` and whose ``missing_element_identifier``
      names the outcome-status definition; no envelope and no projected
      status are surfaced. Source Records remain byte-equivalent.

    - **Requirements 59.1, 59.2 (envelope contents).** Every status-bearing
      response wraps the projected status in a :class:`ProjectionEnvelope`
      carrying the Projection Definition, source Record + Revision
      Identities (the target Intended Outcome Resource + Revision are always
      present), the applicable temporal boundary at ISO-8601 second
      precision, the generated time at second precision, and the
      ``"derived"`` derivation indicator.

    - **Requirement 59.3 (no prohibited derived value).** Neither the
      projection's field names nor any key reachable from the serialized
      envelope payload carries a percent-attainment, cost, ROI,
      budget-variance, forecast, causal-attribution, or cross-Outcome
      aggregate substring.

    - **Requirement 59.6 (no source-entity aliasing).** The response is the
      dedicated :class:`OutcomeStatusProjection` type whose status is a label
      string — never an Observed Outcome, Success-Condition Assessment, or
      Outcome Review row; the projection neither creates nor aliases a source
      entity (the source-entity table counts are unchanged across the call).

    - **Requirement 59.4 (read-only).** Every snapshotted Slice 2 + Slice 4
      source table is byte-equivalent across the projection call.

    **Validates: Requirements 59.1, 59.2, 59.3, 59.4, 59.5, 59.6**
    """
    assertion_context = (
        f"stage={stage!r} boundary={boundary.isoformat()} "
        f"clock={clock_instant.isoformat()} registry={registry_kind!r}"
    )

    with tempfile.TemporaryDirectory(prefix="prop52_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            intended = _seed_stage(svc, engine, stage)
            target_rev_id = intended.intended_outcome_revision_id

            # Snapshot the source graph before the projection runs.
            before = _snapshot(engine)
            entity_counts_before = _source_entity_counts(engine)

            registry = (
                None
                if registry_kind == "registered"
                else {}  # empty registry → withholding path (Requirement 59.5)
            )

            with engine.connect() as conn:
                response = project_outcome_status(
                    conn,
                    intended_outcome_revision_id=target_rev_id,
                    party_id=_VIEWER_PARTY_ID,
                    at=boundary,
                    authorization_service=svc.authz,
                    clock=FixedClock(clock_instant),
                    definition_registry=registry,
                )

            # ----------------------------------------------------------------
            # Requirement 59.4 — read-only: source Records byte-equivalent.
            # ----------------------------------------------------------------
            after = _snapshot(engine)
            assert after == before, (
                f"source Records mutated by projection call "
                f"({assertion_context})"
            )

            # ----------------------------------------------------------------
            # Requirement 59.5 — unresolvable Projection Definition path.
            # ----------------------------------------------------------------
            if registry_kind == "empty_registry":
                assert isinstance(
                    response, ExplanationUnavailableResponse
                ), (
                    f"expected ExplanationUnavailableResponse on the empty-"
                    f"registry path; got {type(response).__name__} "
                    f"({assertion_context})"
                )
                assert response.missing_element_kind == (
                    "projection_definition"
                ), assertion_context
                assert "outcome.status" in response.missing_element_identifier, (
                    assertion_context
                )
                # The withholding shape carries no projected status / envelope.
                assert not hasattr(response, "envelope"), assertion_context
                assert not hasattr(response, "projected_status"), (
                    assertion_context
                )
                # No prohibited key on the withholding payload either.
                for key in _collect_keys(response.model_dump()):
                    lowered = key.lower()
                    for fragment in _PROHIBITED_FIELD_FRAGMENTS:
                        assert fragment not in lowered, (
                            f"prohibited fragment {fragment!r} in withholding "
                            f"payload key {key!r} ({assertion_context})"
                        )
                return

            # ----------------------------------------------------------------
            # Registered path — Requirement 59.6: dedicated projection type.
            # ----------------------------------------------------------------
            assert isinstance(response, OutcomeStatusProjection), (
                f"expected OutcomeStatusProjection on the registered path; "
                f"got {type(response).__name__} ({assertion_context})"
            )

            expected_status = _EXPECTED_STATUS_BY_STAGE[stage]
            assert response.projected_status == expected_status, (
                f"projected_status {response.projected_status!r} != expected "
                f"{expected_status!r} ({assertion_context})"
            )
            # The projected status is a derived label string in the published
            # set — not a source-entity object (Requirement 59.6).
            assert isinstance(response.projected_status, str), assertion_context
            assert response.projected_status in _PUBLISHED_STATUSES, (
                assertion_context
            )
            assert (
                response.intended_outcome_revision_id == target_rev_id
            ), assertion_context

            # ----------------------------------------------------------------
            # Requirements 59.1, 59.2 — envelope contents.
            # ----------------------------------------------------------------
            envelope = response.envelope
            assert isinstance(envelope, ProjectionEnvelope), assertion_context
            # 59.1 — Projection Definition carried verbatim.
            assert envelope.definition == OUTCOME_STATUS_PROJECTION_DEFINITION, (
                assertion_context
            )
            assert isinstance(envelope.definition, ProjectionDefinition), (
                assertion_context
            )
            # 59.2 — derivation indicator distinguishes from authoritative
            # source Records and from the Outcome Review Record itself.
            assert envelope.derivation == "derived", assertion_context
            # 59.1 — source Record + Revision Identities. The target Intended
            # Outcome Resource + Revision are the root sources on every branch.
            assert isinstance(envelope.source_resource_ids, tuple), (
                assertion_context
            )
            assert isinstance(envelope.source_revision_ids, tuple), (
                assertion_context
            )
            assert (
                UUID(intended.intended_outcome_id)
                in envelope.source_resource_ids
            ), assertion_context
            assert (
                UUID(target_rev_id) in envelope.source_revision_ids
            ), assertion_context
            # 59.1 — applicable temporal boundary at ISO-8601 second precision.
            assert envelope.applicable_temporal_boundary == boundary, (
                assertion_context
            )
            assert (
                envelope.applicable_temporal_boundary.tzinfo == timezone.utc
            ), assertion_context
            assert envelope.applicable_temporal_boundary.microsecond == 0, (
                assertion_context
            )
            # 59.1 — generated time at second precision.
            assert envelope.generated_at == clock_instant, assertion_context
            assert envelope.generated_at.tzinfo == timezone.utc, (
                assertion_context
            )
            assert envelope.generated_at.microsecond == 0, assertion_context

            # ----------------------------------------------------------------
            # Requirement 59.3 / 59.6 — no prohibited derived value or
            # source-entity-alias field on the projection or its envelope.
            # ----------------------------------------------------------------
            projection_field_names = {
                f.name for f in dataclasses.fields(response)
            }
            payload_keys = set(_collect_keys(envelope.model_dump()))
            for name in projection_field_names | payload_keys:
                lowered = name.lower()
                for fragment in _PROHIBITED_FIELD_FRAGMENTS:
                    assert fragment not in lowered, (
                        f"prohibited fragment {fragment!r} in field/key "
                        f"{name!r} ({assertion_context})"
                    )

            # ----------------------------------------------------------------
            # Requirement 59.6 — the projection neither creates nor aliases a
            # source entity: source-entity table counts are unchanged.
            # ----------------------------------------------------------------
            assert _source_entity_counts(engine) == entity_counts_before, (
                f"projection altered source-entity counts "
                f"({assertion_context})"
            )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# AD-WS-9 companion — the indistinguishable unresolvable / view-denied target
# path raises rather than surfacing a projection. Kept alongside the envelope
# property so the withholding-vs-deny boundary is pinned for failure triage.
# ---------------------------------------------------------------------------


@given(stage=_STAGE_STRATEGY, boundary=_BOUNDARY_STRATEGY)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_unresolvable_target_raises_indistinguishable_error(
    stage: _Stage, boundary: datetime
) -> None:
    """A target Intended Outcome Revision that does not resolve raises
    :class:`OutcomeStatusTargetUnresolvableError`, regardless of the seeded
    stage (AD-WS-9 indistinguishability)."""
    unresolvable_rev_id = "00000000-0000-7000-8000-0000000fffff"
    with tempfile.TemporaryDirectory(prefix="prop52_deny_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _seed_stage(svc, engine, stage)

            with engine.connect() as conn:
                with pytest.raises(OutcomeStatusTargetUnresolvableError):
                    project_outcome_status(
                        conn,
                        intended_outcome_revision_id=unresolvable_rev_id,
                        party_id=_VIEWER_PARTY_ID,
                        at=boundary,
                        authorization_service=svc.authz,
                        clock=FixedClock(boundary),
                    )
        finally:
            engine.dispose()
