# Feature: fourth-walking-slice, Property 56: Uniqueness of Measurement Definition, Outcome Review, and imported Measurement Record
"""Property 56 — Uniqueness of Measurement Definition, Outcome Review, and
imported Measurement Record (task 15.11).

**Property 56: Uniqueness of Measurement Definition, Outcome Review, and
imported Measurement Record**

*For all* Intended Outcome Resource Identities created in any test session,
at most one Measurement Definition Resource exists for a given target
Intended Outcome Resource; a second creation attempt is rejected with no
Measurement Definition persisted and the first left byte-equivalent. *For
all* Intended Outcome Revision Identities, at most one Outcome Review Record
exists for a given target Intended Outcome Revision; a second attempt is
rejected with no Outcome Review persisted and the first left
byte-equivalent. *For all* imported Measurement Records, the pair
(source-system identifier, source-system record identifier) is unique per
target Measurement Definition Revision Identity; a second imported Record
with a matching pair against the same Definition Revision is rejected with
no second Record persisted.

**Validates: Requirements 44.3, 46.3, 49.3, 61.11**

Strategy
========

Three independent property tests, one per uniqueness key, each drawing a
*pair* of valid creation attempts against the same key:

- :func:`test_double_measurement_definition_rejected_and_first_byte_equivalent`
  drives :meth:`MeasurementDefinitionService.create_measurement_definition`
  twice against the same target Intended Outcome Revision (hence the same
  Intended Outcome Resource). The first attempt succeeds (Requirement
  44.1); the second is rejected with
  :class:`MeasurementDefinitionDuplicateError` carrying
  ``failed_constraint='duplicate_measurement_definition'`` (Requirement
  44.3). The schema ``UNIQUE(target_intended_outcome_resource_id)`` is the
  source of truth; the application pre-check surfaces the structured
  conflict.
- :func:`test_double_imported_measurement_rejected_and_first_byte_equivalent`
  drives :meth:`MeasurementRecordService.create_imported_measurement` twice
  with a matching ``(source_system_id, source_system_record_id)`` pair
  against the same Measurement Definition Revision. The first succeeds
  (Requirement 46.1); the second is rejected with
  :class:`MeasurementRecordDuplicateError` carrying
  ``failed_constraint='imported_measurement_duplicate'`` (Requirement
  46.3 / AD-WS-39).
- :func:`test_double_outcome_review_rejected_and_first_byte_equivalent`
  drives :meth:`OutcomeReviewService.create_outcome_review` twice against
  the same target Intended Outcome Revision. The first succeeds
  (Requirement 49.1); the second is rejected with
  :class:`OutcomeReviewConflictError` carrying
  ``failed_constraint='outcome_review_already_exists'`` (Requirement 49.3).

Per Hypothesis case, each test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing Strategy" —
   per-case database isolation) carrying the Slice 1, Slice 2 (Planning),
   Slice 3 (Execution + Deliverable_Repository), and Slice 4 (Outcome)
   schemas.
2. Constructs fresh services per case (so :class:`IdentityService`'s
   in-memory issued-identifier set cannot bleed across shrinks), pinned to
   a :class:`FixedClock` at ``2026-01-01T00:00:00.000Z`` for deterministic
   timestamps, and seeds the deterministic prerequisite chain through the
   *real* Slice 2 / Slice 4 services so the uniqueness key exists exactly
   as production would write it.
3. Grants the precise required authority on the applicable scope so both
   attempts clear authorization and the *only* reason the second attempt
   is rejected is the uniqueness invariant under test.
4. Issues the first create call, asserts it persisted exactly one row, and
   snapshots that row (and its Resource header where applicable) by
   ``SELECT *`` in stable primary-key order — the byte-equivalence ground
   truth.
5. Issues the second create call against the same key, asserts the
   structured conflict error is raised, asserts exactly one row remains in
   the target table, and re-snapshots it asserting byte-for-byte equality
   with the pre-second-attempt snapshot (Property 56's universal
   quantifier).

Setup mirrors the Slice 4 Property 46 chain-builder conventions
(``tests/property/test_property_46_outcome_creation_anchoring.py``) and the
double-attempt uniqueness convention established by Slice 3 Property 40
(``tests/property/test_property_40_milestone_completion_uniqueness.py``).
The authorization permit path is exercised by granting the precise required
authority rather than by swapping in a stub, so the real evaluation code
path participates.
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
    MeasurementDefinitionDuplicateError,
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import (
    MeasurementRecordDuplicateError,
    MeasurementRecordService,
)
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import (
    OutcomeReviewConflictError,
    OutcomeReviewService,
)
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
# Property 56 only asserts the uniqueness / non-persistence / byte-equivalence
# invariants, so deterministic prerequisite IDs keep shrunken counterexamples
# actionable; the two attempts draw their varying fields from Hypothesis.
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
# on a unique temp-dir path so cross-case identifier, audit, and resource
# state cannot leak; the engine carries every schema the Outcome_Service may
# consult.
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
# Per-case service bundle (mirrors Property 46's ``_Services``).
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
# Seed helpers (mirror tests/property/test_property_46_*.py).
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
# Probe / snapshot helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _snapshot(engine: Engine, table: str) -> list[tuple]:
    """Snapshot every row of ``table`` (every column, stable PK order).

    Returned as ``tuple`` objects so a post-second-attempt diff surfaces a
    precise byte-equivalence violation in a failing assertion.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")).all()
    return [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# Strategies. Text generators are restricted to a narrow printable alphabet
# so generated content round-trips through SQLite's UTF-8 TEXT columns
# without escape ambiguity; each strategy stays inside the per-attribute
# length range named by the design surface.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


# Observed value with at most six fractional digits (Requirement 45.2 / 46.2).
_observed_value_strategy = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    places=6,
    allow_nan=False,
    allow_infinity=False,
)


# A valid Measurement Definition descriptor body (Requirement 44.2; ranges
# narrowed for readability — the schema CHECKs accept the wider ranges).
_measurement_definition_body = st.fixed_dictionaries(
    {
        "measurand_description": _bounded_text(1, 200),
        "unit_of_measure": _bounded_text(1, 50),
        "observation_window": _bounded_text(1, 200),
        "cadence": _bounded_text(1, 200),
        "data_source": _bounded_text(1, 200),
    }
)


# A valid Outcome Review body (Requirement 49.2). The attribution-evidence
# reference is always non-empty so the Asserted / Contradicted stances
# satisfy Requirement 49.4.
_outcome_review_body = st.fixed_dictionaries(
    {
        "review_outcome": st.sampled_from(
            ["Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"]
        ),
        "attribution_stance": st.sampled_from(
            ["Asserted", "Partial", "Unattributed", "Contradicted"]
        ),
        "confidence": st.sampled_from(["High", "Moderate", "Low"]),
        "review_rationale": _bounded_text(1, 200),
        "attribution_evidence_reference": _bounded_text(1, 200),
    }
)


# ===========================================================================
# Requirement 44.3 — at most one Measurement Definition per Intended Outcome
# Resource.
# ===========================================================================


# Feature: fourth-walking-slice, Property 56: Uniqueness of Measurement Definition, Outcome Review, and imported Measurement Record
@given(first_body=_measurement_definition_body, second_body=_measurement_definition_body)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_double_measurement_definition_rejected_and_first_byte_equivalent(
    first_body: dict[str, Any],
    second_body: dict[str, Any],
) -> None:
    """A second Measurement Definition against the same target Intended
    Outcome Resource is rejected with no second Resource / Revision persisted
    and the first left byte-equivalent (Requirement 44.3)."""
    with tempfile.TemporaryDirectory(prefix="prop56_md_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)

            # First attempt succeeds (Requirement 44.1).
            with engine.begin() as conn:
                first = svc.definitions.create_measurement_definition(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    measurand_description=first_body["measurand_description"],
                    unit_of_measure=first_body["unit_of_measure"],
                    observation_window=first_body["observation_window"],
                    cadence=first_body["cadence"],
                    data_source=first_body["data_source"],
                    authoring_party_id=_DEFINER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(first.measurement_definition_id)
            assert _count(engine, "Measurement_Definitions") == 1
            assert _count(engine, "Measurement_Definition_Revisions") == 1

            resource_snapshot = _snapshot(engine, "Measurement_Definitions")
            revision_snapshot = _snapshot(
                engine, "Measurement_Definition_Revisions"
            )

            # Second attempt against the same target is rejected (Requirement
            # 44.3); the schema UNIQUE(target_intended_outcome_resource_id) is
            # the source of truth and the pre-check surfaces it.
            with pytest.raises(MeasurementDefinitionDuplicateError) as exc_info:
                with engine.begin() as conn:
                    svc.definitions.create_measurement_definition(
                        conn,
                        target_intended_outcome_revision_id=intended_rev,
                        measurand_description=second_body["measurand_description"],
                        unit_of_measure=second_body["unit_of_measure"],
                        observation_window=second_body["observation_window"],
                        cadence=second_body["cadence"],
                        data_source=second_body["data_source"],
                        authoring_party_id=_DEFINER_PARTY_ID,
                        applicable_scope=_SCOPE,
                        engine=engine,
                    )

            assert (
                exc_info.value.failed_constraint
                == "duplicate_measurement_definition"
            )
            assert (
                exc_info.value.existing_measurement_definition_id
                == first.measurement_definition_id
            )

            # No second row persisted; the first is byte-equivalent.
            assert _count(engine, "Measurement_Definitions") == 1
            assert _count(engine, "Measurement_Definition_Revisions") == 1
            assert _snapshot(engine, "Measurement_Definitions") == resource_snapshot
            assert (
                _snapshot(engine, "Measurement_Definition_Revisions")
                == revision_snapshot
            )
        finally:
            engine.dispose()


# ===========================================================================
# Requirement 46.3 / AD-WS-39 — the (source_system_id, source_system_record_id)
# pair is an idempotency key per Measurement Definition Revision.
# ===========================================================================


# Feature: fourth-walking-slice, Property 56: Uniqueness of Measurement Definition, Outcome Review, and imported Measurement Record
@given(
    source_system_id=_bounded_text(1, 60),
    source_system_record_id=_bounded_text(1, 60),
    first_value=_observed_value_strategy,
    second_value=_observed_value_strategy,
    first_authority=st.sampled_from(_SOURCE_SYSTEM_AUTHORITIES),
    second_authority=st.sampled_from(_SOURCE_SYSTEM_AUTHORITIES),
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_double_imported_measurement_rejected_and_first_byte_equivalent(
    source_system_id: str,
    source_system_record_id: str,
    first_value: Decimal,
    second_value: Decimal,
    first_authority: str,
    second_authority: str,
) -> None:
    """A second imported Measurement Record carrying a matching
    ``(source_system_id, source_system_record_id)`` pair against the same
    Measurement Definition Revision is rejected with no second Record
    persisted and the first left byte-equivalent (Requirement 46.3 /
    AD-WS-39)."""
    observation = datetime(2025, 6, 1, tzinfo=timezone.utc)
    retrieval = datetime(2025, 7, 1, tzinfo=timezone.utc)

    with tempfile.TemporaryDirectory(prefix="prop56_mr_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)
            _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
            definition_rev = _seed_definition(
                svc, engine, intended_outcome_revision_id=intended_rev
            )

            # First imported Record succeeds (Requirement 46.1).
            with engine.begin() as conn:
                first = svc.records.create_imported_measurement(
                    conn,
                    target_measurement_definition_revision_id=definition_rev,
                    observed_value=first_value,
                    observed_value_unit=_UNIT,
                    observation_time=observation,
                    source_system_id=source_system_id,
                    source_system_record_id=source_system_record_id,
                    source_system_authority=first_authority,
                    source_system_retrieval_time=retrieval,
                    importing_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(first.measurement_record_id)
            assert _count(engine, "Measurement_Records") == 1

            record_snapshot = _snapshot(engine, "Measurement_Records")

            # Second imported Record with the same pair against the same
            # Definition Revision is rejected (Requirement 46.3).
            with pytest.raises(MeasurementRecordDuplicateError) as exc_info:
                with engine.begin() as conn:
                    svc.records.create_imported_measurement(
                        conn,
                        target_measurement_definition_revision_id=definition_rev,
                        observed_value=second_value,
                        observed_value_unit=_UNIT,
                        observation_time=observation,
                        source_system_id=source_system_id,
                        source_system_record_id=source_system_record_id,
                        source_system_authority=second_authority,
                        source_system_retrieval_time=retrieval,
                        importing_party_id=_RECORDER_PARTY_ID,
                        applicable_scope=_SCOPE,
                        engine=engine,
                    )

            assert (
                exc_info.value.failed_constraint
                == "imported_measurement_duplicate"
            )
            assert (
                exc_info.value.existing_measurement_record_id
                == first.measurement_record_id
            )

            # No second row persisted; the first is byte-equivalent.
            assert _count(engine, "Measurement_Records") == 1
            assert _snapshot(engine, "Measurement_Records") == record_snapshot
        finally:
            engine.dispose()


# ===========================================================================
# Requirement 49.3 — at most one Outcome Review Record per Intended Outcome
# Revision.
# ===========================================================================


# Feature: fourth-walking-slice, Property 56: Uniqueness of Measurement Definition, Outcome Review, and imported Measurement Record
@given(first_body=_outcome_review_body, second_body=_outcome_review_body)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_double_outcome_review_rejected_and_first_byte_equivalent(
    first_body: dict[str, Any],
    second_body: dict[str, Any],
) -> None:
    """A second Outcome Review against the same target Intended Outcome
    Revision is rejected with no second Record persisted and the first left
    byte-equivalent (Requirement 49.3)."""
    with tempfile.TemporaryDirectory(prefix="prop56_review_") as raw_tmp:
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

            # First Outcome Review succeeds (Requirement 49.1).
            with engine.begin() as conn:
                first = svc.reviews.create_outcome_review(
                    conn,
                    target_intended_outcome_revision_id=intended_rev,
                    review_outcome=first_body["review_outcome"],
                    attribution_stance=first_body["attribution_stance"],
                    confidence=first_body["confidence"],
                    review_rationale=first_body["review_rationale"],
                    attribution_evidence_reference=(
                        first_body["attribution_evidence_reference"]
                    ),
                    cited_assessment_ids=[assessment_id],
                    cited_completion_ids=[completion_id],
                    cited_produced_deliverable_revision_ids=[deliverable_rev],
                    reviewing_party_id=_REVIEWER_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(first.outcome_review_id)
            assert _count(engine, "Outcome_Review_Records") == 1

            record_snapshot = _snapshot(engine, "Outcome_Review_Records")

            # Second Review against the same target Intended Outcome Revision
            # is rejected (Requirement 49.3); the schema
            # UNIQUE(target_intended_outcome_revision_id) is the source of
            # truth and the pre-check surfaces it.
            with pytest.raises(OutcomeReviewConflictError) as exc_info:
                with engine.begin() as conn:
                    svc.reviews.create_outcome_review(
                        conn,
                        target_intended_outcome_revision_id=intended_rev,
                        review_outcome=second_body["review_outcome"],
                        attribution_stance=second_body["attribution_stance"],
                        confidence=second_body["confidence"],
                        review_rationale=second_body["review_rationale"],
                        attribution_evidence_reference=(
                            second_body["attribution_evidence_reference"]
                        ),
                        cited_assessment_ids=[assessment_id],
                        cited_completion_ids=[completion_id],
                        cited_produced_deliverable_revision_ids=[
                            deliverable_rev
                        ],
                        reviewing_party_id=_REVIEWER_PARTY_ID,
                        authority_basis=_BASIS,
                        applicable_scope=_SCOPE,
                        engine=engine,
                    )

            assert (
                exc_info.value.failed_constraint
                == "outcome_review_already_exists"
            )
            assert (
                exc_info.value.target_intended_outcome_revision_id
                == intended_rev
            )

            # No second row persisted; the first is byte-equivalent.
            assert _count(engine, "Outcome_Review_Records") == 1
            assert _snapshot(engine, "Outcome_Review_Records") == record_snapshot
        finally:
            engine.dispose()
