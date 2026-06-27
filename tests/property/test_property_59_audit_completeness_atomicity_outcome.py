# Feature: fourth-walking-slice, Property 59: Audit completeness and atomicity for every outcome-measurement action
"""Property 59 — Audit completeness and atomicity for every
outcome-measurement action (task 15.14).

**Property 59: Audit completeness and atomicity for every
outcome-measurement action**

*For all* sequences of Slice 4 operations (Measurement Definition,
native / imported Measurement Record, Observed Outcome Revision,
Success-Condition Assessment, Outcome Review creation; denied attempts;
attempted modifications of finalized entities), the ``Audit_Records``
table contains exactly one matching row per consequential write with
``actor_party_id``, ``action_type``, ``target_id``,
``target_revision_id`` when applicable, ``outcome``, ``recorded_at``,
and ``correlation_id`` consistent with the originating operation,
appended in the same transaction; and exactly one matching Denial Record
per denied attempt with the same required fields and a ``reason_code``
drawn from the Slice 1 enumeration. ``Audit_Records.append_sequence`` is
monotonically non-decreasing by ``recorded_at``. If the audit append
fails for any test-generated finalization, the originating finalization
is rolled back and is not observable from any query path.

**Validates: Requirements 44.6, 45.5, 45.7, 46.6, 47.6, 48.5, 49.6,
50.2, 57.1, 57.2, 57.4, 57.6, 61.14**

Strategy
========

Two property tests carry the property:

``test_audit_completeness_and_atomicity_across_outcome_actions``
    Each Hypothesis case (a) seeds a fresh per-case SQLite engine on a
    unique :class:`tempfile.TemporaryDirectory` path carrying the Slice 1,
    Slice 2 (Planning), Slice 3 (Execution + Deliverable), and Slice 4
    (Outcome) schemas; the Party rows (one Party per Slice 4 authority,
    one unauthorized Party, plus an Intended-Outcome owner, a completing
    Party, and a contributor); the role grants; one *shared* Intended
    Outcome Revision; one *shared* Measurement Definition addressing it;
    one *shared* native Measurement Record; one *shared* Observed Outcome
    Revision; one *shared* Success-Condition Assessment; and one citable
    Completion Record + produced Deliverable Revision (direct INSERT).

    (b) draws a sequence of 1..4 operations from a closed alphabet
    covering creation (permit), authorization denial, and
    post-finalization mutation attempts. Every consequential write and
    every denied attempt runs through the *real* Slice 4 service with an
    explicit ``correlation_id`` so the post-hoc assertion locates the
    matching audit row deterministically. Permit ops that collide with a
    UNIQUE constraint (Measurement Definition per Intended Outcome
    Resource — Requirement 44.3; Outcome Review per Intended Outcome
    Revision — Requirement 49.3) mint a fresh prerequisite chain so the
    permit reaches the consequential write rather than a uniqueness
    pre-check.

    Assertions per case:

    1. **Existence and uniqueness.** Exactly one ``Audit_Records`` row
       per consequential permit matching
       ``(correlation_id, outcome='consequential', action_type)``; exactly
       one Denial Record per authorization deny matching
       ``(correlation_id, outcome='deny', authorities_required IS NULL)``.
       The authorization-evaluation row (also ``outcome='deny'``) is
       filtered out by ``authorities_required IS NOT NULL`` so "exactly one
       Denial Record" pins the dedicated audit row.
    2. **Attribute fidelity.** ``actor_party_id``, ``action_type``,
       ``target_id``, ``target_revision_id``, and ``correlation_id`` are
       byte-equal to the values captured at call time; Denial rows carry a
       non-NULL ``reason_code`` from the Slice 1 enumeration.
    3. **Recorded-time format.** Every appended row's ``recorded_at``
       matches the slice-wide millisecond-precision UTC pattern.
    4. **Append-sequence monotonicity.** Across every appended row, sorting
       by ``recorded_at`` ASC then ``append_sequence`` ASC yields a
       strictly increasing ``append_sequence`` series (Requirement 57.4 /
       Slice 1 Requirement 13.4). The FixedClock makes every
       ``recorded_at`` identical, the strongest form of the property.
    5. **No in-flight write on denial.** Each deny op's ``correlation_id``
       carries zero consequential audit rows — the caller transaction
       rolled back and only the separate-transaction Denial Record
       committed (Requirement 50.2 / 57.6).
    6. **Trigger-level immutability.** Each raw-SQL mutation attempt on a
       finalized Record is rejected by the AD-WS-36 trigger and leaves the
       target row byte-equivalent to its pre-attempt snapshot.

``test_audit_append_failure_rolls_back_outcome_finalization``
    Each Hypothesis case wires a Measurement Definition service whose
    consequential audit append always raises :class:`AuditAppendError`
    (the authorization service keeps a healthy audit log so the permit
    evaluation succeeds and the finalization reaches the consequential
    append). The case asserts the create raises, that no
    ``Measurement_Definitions`` / ``Measurement_Definition_Revisions`` /
    ``Relationships`` row survives, and that no consequential
    ``Audit_Records`` row carrying the operation's ``correlation_id`` is
    observable — the audit-append failure rolled the finalization back
    (Requirement 57.2 / Slice 1 Requirement 13.6).

Setup follows the conventions established by the Slice 1/2/3/4 property
tests: per-case :class:`tempfile.TemporaryDirectory` ownership of the
SQLite file, fresh services per case so :class:`IdentityService`
in-memory state cannot bleed across shrinks, :class:`FixedClock` pinned
to ``2026-01-01T00:00:00.000Z``, and the permit path is exercised by
granting the precise required authority rather than by swapping in a
stub so the real evaluation code path participates.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from walking_slice.audit import AuditAppendError, AuditLog, AuditRecord
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
    MeasurementDefinitionAuthorizationError,
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import (
    MeasurementRecordAuthorizationError,
    MeasurementRecordService,
)
from walking_slice.outcome.observed_outcomes import (
    ObservedOutcomeAuthorizationError,
    ObservedOutcomeService,
)
from walking_slice.outcome.outcome_reviews import (
    OutcomeReviewAuthorizationError,
    OutcomeReviewService,
)
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentAuthorizationError,
    SuccessConditionAssessmentService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and constants.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_OWNER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000590002"
_DEFINER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590003"
_RECORDER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590004"
_ASSESSOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590005"
_REVIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590006"
_COMPLETING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590007"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590008"
_UNAUTHORIZED_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000590009"

_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000005900c1"
_OBJECTIVE_REV_ID: Final[str] = "00000000-0000-7000-8000-0000005900c2"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-0000005900c3"
_SCOPE: Final[str] = "prop-59/scope"

_AUTHORITY_BASIS_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000005900ba")
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Directly-seeded citable Slice 3 artifacts (Outcome Review citations).
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-0000005900d1"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000005900d2"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-0000005900d3"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000005900d4"
)

_UNIT: Final[str] = "percent"
# ISO-8601 closed window covering the 2025 observation instants the record
# ops draw; both edges precede the fixed recorded time (2026-01-01).
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"
_OBSERVATION_TIME: Final[datetime] = datetime(2025, 6, 1, tzinfo=timezone.utc)
_RETRIEVAL_TIME: Final[datetime] = datetime(2025, 6, 2, tzinfo=timezone.utc)

_RECORDED_AT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)

# Canonical action-type strings written on the consequential audit rows.
_ACTION_MEASUREMENT_DEFINITION: Final[str] = "create.measurement_definition"
_ACTION_MEASUREMENT_RECORD: Final[str] = "create.measurement_record"
_ACTION_OBSERVED_OUTCOME: Final[str] = "create.observed_outcome"
_ACTION_ASSESSMENT: Final[str] = "create.success_condition_assessment"
_ACTION_OUTCOME_REVIEW: Final[str] = "create.outcome_review"


# ---------------------------------------------------------------------------
# Operation alphabet.
# ---------------------------------------------------------------------------


_OPERATIONS: Final[tuple[str, ...]] = (
    "measurement_definition_permit",
    "native_measurement_permit",
    "imported_measurement_permit",
    "observed_outcome_permit",
    "assessment_permit",
    "outcome_review_permit",
    "measurement_definition_deny",
    "measurement_record_deny",
    "observed_outcome_deny",
    "assessment_deny",
    "outcome_review_deny",
    "attempt_update_finalized_measurement_record",
    "attempt_delete_finalized_measurement_definition_revision",
)

# ``min_size=1`` guarantees at least one operation; ``max_size=4`` keeps the
# per-case wall time bounded because an ``outcome_review_permit`` mints a full
# fresh prerequisite chain (Intended Outcome -> Definition -> native Record ->
# Observed Outcome -> Assessment -> Review).
_scenario_strategy = st.lists(
    st.sampled_from(_OPERATIONS), min_size=1, max_size=4
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every schema the Slice
    4 service surface spans (Slice 1 + Slice 2 + Slice 3 + Slice 4)."""
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
    """Per-case bundle of every collaborator the scenario needs.

    Fresh per Hypothesis case so :class:`IdentityService` in-memory state
    cannot bleed across shrinks; the deny-path denial-audit sleep is a no-op
    so the AD-WS-9 retry sequence never spends real time.
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
# Seed helpers (mirror tests/property/test_property_46_*).
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
    """Seed every Party, the Objective, and the role grants the scenario uses.

    The unauthorized Party holds *no* role assignment so every deny op's
    authorization evaluation denies with ``reason_code='no-role-assignment'``.
    """
    with engine.begin() as conn:
        _seed_party(conn, _OWNER_PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
        _seed_party(conn, _DEFINER_PARTY_ID, "Measurement Definer")
        _seed_party(conn, _RECORDER_PARTY_ID, "Measurement Recorder")
        _seed_party(conn, _ASSESSOR_PARTY_ID, "Outcome Assessor")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")
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


def _seed_citable_completion(engine: Engine) -> None:
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
                "prev": "00000000-0000-7000-8000-0000005c0fff",
                "aid": "00000000-0000-7000-8000-0000005a0fff",
                "proj": "00000000-0000-7000-8000-0000005b0fff",
                "party": _COMPLETING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_citable_deliverable_revision(engine: Engine) -> None:
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
                "prev": "00000000-0000-7000-8000-0000005c0ffe",
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


# ---------------------------------------------------------------------------
# Mint helpers — build prerequisite chains through the real services.
#
# These run with the *authorized* Parties (no correlation_id supplied) so the
# scenario's per-op correlation identifiers remain unique to the op under
# test; the audit rows these helpers append carry auto-generated correlation
# identifiers and never collide with an expected descriptor.
# ---------------------------------------------------------------------------


def _mint_intended_outcome(svc: _Services, engine: Engine) -> tuple[str, str]:
    """Create one ``outcome_kind='intended'`` Intended Outcome via Slice 2.

    Returns ``(intended_outcome_id, intended_outcome_revision_id)``.
    """
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


def _mint_definition(
    svc: _Services, engine: Engine, *, intended_outcome_revision_id: str
) -> str:
    """Create the single Measurement Definition addressing the Intended
    Outcome; return its Revision Identity."""
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


def _mint_native_record(
    svc: _Services, engine: Engine, *, definition_revision_id: str
) -> str:
    """Record one citable native Measurement Record; return its Identity."""
    with engine.begin() as conn:
        result = svc.records.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=definition_revision_id,
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=_OBSERVATION_TIME,
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.measurement_record_id


def _mint_observed_outcome(
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


def _mint_assessment(
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


def _mint_review_chain(svc: _Services, engine: Engine) -> tuple[str, str]:
    """Mint a fresh Intended Outcome + Assessment chain for an Outcome Review.

    Returns ``(intended_outcome_revision_id, assessment_id)`` where the
    Assessment ``Addresses`` the same Intended Outcome Revision, satisfying
    Requirement 49.4's anchor-equality rule for the Review.
    """
    _io_id, io_rev = _mint_intended_outcome(svc, engine)
    definition_rev = _mint_definition(
        svc, engine, intended_outcome_revision_id=io_rev
    )
    record_id = _mint_native_record(
        svc, engine, definition_revision_id=definition_rev
    )
    observed_rev = _mint_observed_outcome(
        svc,
        engine,
        intended_outcome_revision_id=io_rev,
        cited_record_ids=[record_id],
    )
    assessment_id = _mint_assessment(
        svc,
        engine,
        intended_outcome_revision_id=io_rev,
        sourced_observed_outcome_revision_id=observed_rev,
    )
    return io_rev, assessment_id


# ---------------------------------------------------------------------------
# Audit-row probe helpers.
# ---------------------------------------------------------------------------


def _fetch_audit_rows_for(
    engine: Engine,
    *,
    correlation_id: str,
    outcome: str,
    require_authorities_required_null: Optional[bool] = None,
) -> list[dict[str, Any]]:
    """Return ``Audit_Records`` rows matching ``(correlation_id, outcome)``.

    When ``require_authorities_required_null`` is ``True`` only the dedicated
    Denial Record (``authorities_required IS NULL``, written via
    :meth:`AuditLog.append_denial`) is kept; the authorization-evaluation row
    (``authorities_required IS NOT NULL``) is filtered out.
    """
    sql = (
        "SELECT audit_record_id, append_sequence, actor_party_id, "
        "action_type, outcome, target_id, target_revision_id, "
        "reason_code, correlation_id, recorded_at, "
        "authorities_required, authorities_held "
        "FROM Audit_Records "
        "WHERE correlation_id = :cid AND outcome = :outcome "
    )
    if require_authorities_required_null is True:
        sql += "AND authorities_required IS NULL "
    elif require_authorities_required_null is False:
        sql += "AND authorities_required IS NOT NULL "
    sql += "ORDER BY append_sequence"
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(sql), {"cid": correlation_id, "outcome": outcome}
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _count_consequential_rows_for_correlation(
    engine: Engine, correlation_id: str
) -> int:
    """Return the number of consequential rows carrying ``correlation_id``."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM Audit_Records
                     WHERE correlation_id = :cid
                       AND outcome = 'consequential'
                    """
                ),
                {"cid": correlation_id},
            ).scalar_one()
        )


def _fetch_all_audit_rows(engine: Engine) -> list[dict[str, Any]]:
    """Return every ``Audit_Records`` row ordered by recorded_at then sequence."""
    sql = (
        "SELECT audit_record_id, append_sequence, recorded_at, "
        "outcome, action_type, correlation_id "
        "FROM Audit_Records "
        "ORDER BY recorded_at ASC, append_sequence ASC"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [dict(row) for row in rows]


def _fetch_row(engine: Engine, table: str, id_column: str, row_id: str) -> dict[str, Any]:
    """Return the full row from ``table`` whose ``id_column`` equals ``row_id``."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(f"SELECT * FROM {table} WHERE {id_column} = :id"),
                {"id": row_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


# ---------------------------------------------------------------------------
# Expected-audit descriptor.
# ---------------------------------------------------------------------------


def _expected(
    *,
    correlation_id: str,
    outcome: str,
    action_type: str,
    actor_party_id: str,
    target_id: Optional[str],
    target_revision_id: Optional[str],
    require_authorities_required_null: Optional[bool] = None,
) -> dict[str, Any]:
    """Build one expected-audit descriptor."""
    return {
        "correlation_id": correlation_id,
        "outcome": outcome,
        "action_type": action_type,
        "actor_party_id": actor_party_id,
        "target_id": target_id,
        "target_revision_id": target_revision_id,
        "require_authorities_required_null": (
            require_authorities_required_null
        ),
    }


# ---------------------------------------------------------------------------
# Property 59 — main test: completeness + atomicity across the operation set.
# ---------------------------------------------------------------------------


# Feature: fourth-walking-slice, Property 59: Audit completeness and atomicity for every outcome-measurement action
@given(scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_audit_completeness_and_atomicity_across_outcome_actions(
    scenario: list[str],
) -> None:
    """For every consequential Slice 4 write and every denied attempt the
    scenario runs, exactly one ``Audit_Records`` row exists carrying the
    expected ``actor_party_id``, ``action_type``, ``target_id``,
    ``target_revision_id``, ``outcome``, millisecond-precision
    ``recorded_at``, and ``correlation_id``; the
    ``Audit_Records.append_sequence`` series is strictly increasing in
    insertion order; every denial leaves no in-flight Slice 4 row persisted;
    and every raw-SQL mutation attempt on a finalized Record is rejected by
    the AD-WS-36 trigger and leaves the target row byte-equivalent.

    **Validates: Requirements 44.6, 45.5, 45.7, 46.6, 47.6, 48.5, 49.6,
    50.2, 57.1, 57.2, 57.4, 57.6, 61.14**
    """
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop59_"
    ) as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)

            # Shared prerequisite chain reused by the deny ops and the
            # measurement-record / observed-outcome / assessment permits.
            _shared_io_id, shared_io_rev = _mint_intended_outcome(svc, engine)
            shared_def_rev = _mint_definition(
                svc, engine, intended_outcome_revision_id=shared_io_rev
            )
            shared_record_id = _mint_native_record(
                svc, engine, definition_revision_id=shared_def_rev
            )
            shared_observed_rev = _mint_observed_outcome(
                svc,
                engine,
                intended_outcome_revision_id=shared_io_rev,
                cited_record_ids=[shared_record_id],
            )
            shared_assessment_id = _mint_assessment(
                svc,
                engine,
                intended_outcome_revision_id=shared_io_rev,
                sourced_observed_outcome_revision_id=shared_observed_rev,
            )
            _seed_citable_completion(engine)

            # Snapshots for the AD-WS-36 trigger-level immutability assertions.
            shared_record_snapshot = _fetch_row(
                engine,
                "Measurement_Records",
                "measurement_record_id",
                shared_record_id,
            )
            shared_def_rev_snapshot = _fetch_row(
                engine,
                "Measurement_Definition_Revisions",
                "measurement_definition_revision_id",
                shared_def_rev,
            )

            expected_audit: list[dict[str, Any]] = []

            for op_index, op in enumerate(scenario):
                # Stable per-operation correlation identifier so the
                # post-hoc assertion locates the matching audit row
                # deterministically; embedding the index and op name keeps
                # shrunken counterexamples readable.
                correlation_id = f"prop59-op-{op_index:03d}-{op}"

                # ----- Measurement Definition ----------------------------
                if op == "measurement_definition_permit":
                    # UNIQUE(target_intended_outcome_resource_id) — mint a
                    # fresh Intended Outcome so the permit reaches the
                    # consequential write (Requirement 44.3).
                    _io_id, fresh_io_rev = _mint_intended_outcome(svc, engine)
                    with engine.begin() as conn:
                        result = (
                            svc.definitions.create_measurement_definition(
                                conn,
                                target_intended_outcome_revision_id=(
                                    fresh_io_rev
                                ),
                                measurand_description=(
                                    f"Adoption rate {op_index}."
                                ),
                                unit_of_measure=_UNIT,
                                observation_window=_WINDOW_2025,
                                cadence="monthly",
                                data_source="product analytics",
                                authoring_party_id=_DEFINER_PARTY_ID,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type=_ACTION_MEASUREMENT_DEFINITION,
                            actor_party_id=_DEFINER_PARTY_ID,
                            target_id=result.measurement_definition_id,
                            target_revision_id=(
                                result.measurement_definition_revision_id
                            ),
                        )
                    )
                elif op == "measurement_definition_deny":
                    # Fresh IO so no UNIQUE/conflict pre-check fires before the
                    # authorization deny.
                    _io_id, deny_io_rev = _mint_intended_outcome(svc, engine)
                    with pytest.raises(
                        MeasurementDefinitionAuthorizationError
                    ):
                        with engine.begin() as conn:
                            svc.definitions.create_measurement_definition(
                                conn,
                                target_intended_outcome_revision_id=(
                                    deny_io_rev
                                ),
                                measurand_description=(
                                    f"Deny definition {op_index}."
                                ),
                                unit_of_measure=_UNIT,
                                observation_window=_WINDOW_2025,
                                cadence="monthly",
                                data_source="product analytics",
                                authoring_party_id=_UNAUTHORIZED_PARTY_ID,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type=_ACTION_MEASUREMENT_DEFINITION,
                            actor_party_id=_UNAUTHORIZED_PARTY_ID,
                            target_id=deny_io_rev,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Measurement Record (native) -----------------------
                elif op == "native_measurement_permit":
                    with engine.begin() as conn:
                        result = svc.records.create_native_measurement(
                            conn,
                            target_measurement_definition_revision_id=(
                                shared_def_rev
                            ),
                            observed_value=Decimal("12.5"),
                            observed_value_unit=_UNIT,
                            observation_time=_OBSERVATION_TIME,
                            recording_party_id=_RECORDER_PARTY_ID,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type=_ACTION_MEASUREMENT_RECORD,
                            actor_party_id=_RECORDER_PARTY_ID,
                            target_id=result.measurement_record_id,
                            target_revision_id=None,
                        )
                    )

                # ----- Measurement Record (imported) ---------------------
                elif op == "imported_measurement_permit":
                    with engine.begin() as conn:
                        result = svc.records.create_imported_measurement(
                            conn,
                            target_measurement_definition_revision_id=(
                                shared_def_rev
                            ),
                            observed_value=Decimal("7.5"),
                            observed_value_unit=_UNIT,
                            observation_time=_OBSERVATION_TIME,
                            source_system_id="ext-analytics",
                            source_system_record_id=(
                                f"ext-rec-{op_index:04d}"
                            ),
                            source_system_authority="authoritative",
                            source_system_retrieval_time=_RETRIEVAL_TIME,
                            importing_party_id=_RECORDER_PARTY_ID,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type=_ACTION_MEASUREMENT_RECORD,
                            actor_party_id=_RECORDER_PARTY_ID,
                            target_id=result.measurement_record_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "measurement_record_deny":
                    with pytest.raises(MeasurementRecordAuthorizationError):
                        with engine.begin() as conn:
                            svc.records.create_native_measurement(
                                conn,
                                target_measurement_definition_revision_id=(
                                    shared_def_rev
                                ),
                                observed_value=Decimal("3.0"),
                                observed_value_unit=_UNIT,
                                observation_time=_OBSERVATION_TIME,
                                recording_party_id=_UNAUTHORIZED_PARTY_ID,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type=_ACTION_MEASUREMENT_RECORD,
                            actor_party_id=_UNAUTHORIZED_PARTY_ID,
                            target_id=shared_def_rev,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Observed Outcome ----------------------------------
                elif op == "observed_outcome_permit":
                    with engine.begin() as conn:
                        result = svc.observed.create_observed_outcome(
                            conn,
                            target_intended_outcome_revision_id=(
                                shared_io_rev
                            ),
                            assessment_summary=(
                                f"Adoption trending up {op_index}."
                            ),
                            cited_measurement_record_ids=[shared_record_id],
                            authoring_party_id=_ASSESSOR_PARTY_ID,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type=_ACTION_OBSERVED_OUTCOME,
                            actor_party_id=_ASSESSOR_PARTY_ID,
                            target_id=result.observed_outcome_id,
                            target_revision_id=(
                                result.observed_outcome_revision_id
                            ),
                        )
                    )
                elif op == "observed_outcome_deny":
                    with pytest.raises(ObservedOutcomeAuthorizationError):
                        with engine.begin() as conn:
                            svc.observed.create_observed_outcome(
                                conn,
                                target_intended_outcome_revision_id=(
                                    shared_io_rev
                                ),
                                assessment_summary=(
                                    f"Deny observed {op_index}."
                                ),
                                cited_measurement_record_ids=[
                                    shared_record_id
                                ],
                                authoring_party_id=_UNAUTHORIZED_PARTY_ID,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type=_ACTION_OBSERVED_OUTCOME,
                            actor_party_id=_UNAUTHORIZED_PARTY_ID,
                            target_id=shared_io_rev,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Success-Condition Assessment ----------------------
                elif op == "assessment_permit":
                    with engine.begin() as conn:
                        result = svc.assessments.create_assessment(
                            conn,
                            target_intended_outcome_revision_id=(
                                shared_io_rev
                            ),
                            sourced_observed_outcome_revision_id=(
                                shared_observed_rev
                            ),
                            assessment_category="Satisfied",
                            assessment_rationale=(
                                f"Adoption met the threshold {op_index}."
                            ),
                            assessing_party_id=_ASSESSOR_PARTY_ID,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type=_ACTION_ASSESSMENT,
                            actor_party_id=_ASSESSOR_PARTY_ID,
                            target_id=result.assessment_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "assessment_deny":
                    with pytest.raises(
                        SuccessConditionAssessmentAuthorizationError
                    ):
                        with engine.begin() as conn:
                            svc.assessments.create_assessment(
                                conn,
                                target_intended_outcome_revision_id=(
                                    shared_io_rev
                                ),
                                sourced_observed_outcome_revision_id=(
                                    shared_observed_rev
                                ),
                                assessment_category="Satisfied",
                                assessment_rationale=(
                                    f"Deny assessment {op_index}."
                                ),
                                assessing_party_id=_UNAUTHORIZED_PARTY_ID,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type=_ACTION_ASSESSMENT,
                            actor_party_id=_UNAUTHORIZED_PARTY_ID,
                            target_id=shared_io_rev,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Outcome Review ------------------------------------
                elif op == "outcome_review_permit":
                    # UNIQUE(target_intended_outcome_revision_id) — mint a
                    # fresh Intended Outcome + Assessment chain so the permit
                    # reaches the consequential write (Requirement 49.3).
                    review_io_rev, review_assessment_id = _mint_review_chain(
                        svc, engine
                    )
                    with engine.begin() as conn:
                        result = svc.reviews.create_outcome_review(
                            conn,
                            target_intended_outcome_revision_id=(
                                review_io_rev
                            ),
                            review_outcome="Achieved",
                            attribution_stance="Asserted",
                            confidence="High",
                            review_rationale=(
                                f"Outcome achieved {op_index}."
                            ),
                            attribution_evidence_reference=(
                                "Evidence: telemetry dashboards."
                            ),
                            cited_assessment_ids=[review_assessment_id],
                            cited_completion_ids=[_CITABLE_COMPLETION_ID],
                            cited_produced_deliverable_revision_ids=[],
                            reviewing_party_id=_REVIEWER_PARTY_ID,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type=_ACTION_OUTCOME_REVIEW,
                            actor_party_id=_REVIEWER_PARTY_ID,
                            target_id=result.outcome_review_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "outcome_review_deny":
                    # Fresh chain so no UNIQUE/conflict pre-check fires before
                    # the authorization deny, with valid citations so the only
                    # failure is the authorization denial.
                    deny_review_io_rev, deny_review_assessment_id = (
                        _mint_review_chain(svc, engine)
                    )
                    with pytest.raises(OutcomeReviewAuthorizationError):
                        with engine.begin() as conn:
                            svc.reviews.create_outcome_review(
                                conn,
                                target_intended_outcome_revision_id=(
                                    deny_review_io_rev
                                ),
                                review_outcome="Achieved",
                                attribution_stance="Asserted",
                                confidence="High",
                                review_rationale=(
                                    f"Deny review {op_index}."
                                ),
                                attribution_evidence_reference=(
                                    "Evidence: telemetry dashboards."
                                ),
                                cited_assessment_ids=[
                                    deny_review_assessment_id
                                ],
                                cited_completion_ids=[_CITABLE_COMPLETION_ID],
                                cited_produced_deliverable_revision_ids=[],
                                reviewing_party_id=_UNAUTHORIZED_PARTY_ID,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type=_ACTION_OUTCOME_REVIEW,
                            actor_party_id=_UNAUTHORIZED_PARTY_ID,
                            target_id=deny_review_io_rev,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Mutation attempts on finalized entities -----------
                elif op == "attempt_update_finalized_measurement_record":
                    with engine.connect() as conn, pytest.raises(
                        IntegrityError
                    ):
                        with conn.begin():
                            conn.execute(
                                text(
                                    "UPDATE Measurement_Records "
                                    "SET observed_value_unit = 'tampered' "
                                    "WHERE measurement_record_id = :id"
                                ),
                                {"id": shared_record_id},
                            )
                elif (
                    op
                    == "attempt_delete_finalized_measurement_definition_revision"
                ):
                    with engine.connect() as conn, pytest.raises(
                        IntegrityError
                    ):
                        with conn.begin():
                            conn.execute(
                                text(
                                    "DELETE FROM "
                                    "Measurement_Definition_Revisions "
                                    "WHERE measurement_definition_revision_id "
                                    "= :id"
                                ),
                                {"id": shared_def_rev},
                            )

                else:  # pragma: no cover - defensive
                    raise AssertionError(f"unknown op: {op!r}")

            # ---------------------------------------------------------
            # Post-hoc assertions.
            # ---------------------------------------------------------

            # (1)+(2)+(3) Existence, uniqueness, attribute fidelity, and
            # recorded-time format per expected-audit descriptor.
            for expected in expected_audit:
                rows = _fetch_audit_rows_for(
                    engine,
                    correlation_id=expected["correlation_id"],
                    outcome=expected["outcome"],
                    require_authorities_required_null=(
                        expected["require_authorities_required_null"]
                    ),
                )
                assert len(rows) == 1, (
                    f"Property 59: expected exactly one Audit_Records row "
                    f"with correlation_id={expected['correlation_id']!r}, "
                    f"outcome={expected['outcome']!r}, "
                    f"authorities_required_null filter="
                    f"{expected['require_authorities_required_null']!r}; "
                    f"got {len(rows)} ({rows!r})."
                )
                row = rows[0]

                assert (
                    row["actor_party_id"] == expected["actor_party_id"]
                ), (
                    f"Property 59: audit row {row['audit_record_id']!r} has "
                    f"actor_party_id={row['actor_party_id']!r}; expected "
                    f"{expected['actor_party_id']!r}."
                )
                assert row["action_type"] == expected["action_type"], (
                    f"Property 59: audit row {row['audit_record_id']!r} has "
                    f"action_type={row['action_type']!r}; expected "
                    f"{expected['action_type']!r}."
                )
                assert row["target_id"] == expected["target_id"], (
                    f"Property 59: audit row {row['audit_record_id']!r} has "
                    f"target_id={row['target_id']!r}; expected "
                    f"{expected['target_id']!r}."
                )
                assert (
                    row["target_revision_id"]
                    == expected["target_revision_id"]
                ), (
                    f"Property 59: audit row {row['audit_record_id']!r} has "
                    f"target_revision_id={row['target_revision_id']!r}; "
                    f"expected {expected['target_revision_id']!r}."
                )
                assert (
                    row["correlation_id"] == expected["correlation_id"]
                ), (
                    f"Property 59: audit row {row['audit_record_id']!r} has "
                    f"correlation_id={row['correlation_id']!r}; expected "
                    f"{expected['correlation_id']!r}."
                )
                assert _RECORDED_AT_PATTERN.match(row["recorded_at"]), (
                    f"Property 59: audit row {row['audit_record_id']!r} has "
                    f"recorded_at={row['recorded_at']!r}; expected canonical "
                    f"millisecond-precision UTC text matching "
                    f"{_RECORDED_AT_PATTERN.pattern!r}."
                )
                if expected["outcome"] == "deny":
                    assert row["reason_code"] is not None and (
                        row["reason_code"] != ""
                    ), (
                        f"Property 59: denial audit row "
                        f"{row['audit_record_id']!r} has empty reason_code; "
                        f"expected a non-empty value drawn from the Slice 1 "
                        f"Requirement 7.2 enumeration."
                    )

                    # (5) Denial leaves no in-flight Slice 4 row — the caller
                    # transaction rolled back and only the separate-transaction
                    # Denial Record committed (Requirement 50.2 / 57.6).
                    consequential_count = (
                        _count_consequential_rows_for_correlation(
                            engine, expected["correlation_id"]
                        )
                    )
                    assert consequential_count == 0, (
                        f"Property 59: denied attempt with correlation_id="
                        f"{expected['correlation_id']!r} left "
                        f"{consequential_count} consequential Audit_Records "
                        f"row(s) — a denied attempt must leave no in-flight "
                        f"Slice 4 write persisted (Requirement 50.2 / 57.6)."
                    )

            # (4) Append-sequence monotonicity across the entire case.
            all_rows = _fetch_all_audit_rows(engine)
            previous_sequence: Optional[int] = None
            for row in all_rows:
                current = int(row["append_sequence"])
                if previous_sequence is not None:
                    assert current > previous_sequence, (
                        f"Property 59: Audit_Records.append_sequence is not "
                        f"strictly increasing in (recorded_at, "
                        f"append_sequence) order — observed "
                        f"{previous_sequence} then {current} on row "
                        f"{row['audit_record_id']!r} "
                        f"(recorded_at={row['recorded_at']!r}). "
                        f"(Requirement 57.4 / Slice 1 Requirement 13.4)."
                    )
                previous_sequence = current

            # (6) Trigger-level immutability — the shared Measurement Record and
            # shared Measurement Definition Revision rows are byte-equivalent to
            # their pre-attempt snapshots (AD-WS-36).
            assert (
                _fetch_row(
                    engine,
                    "Measurement_Records",
                    "measurement_record_id",
                    shared_record_id,
                )
                == shared_record_snapshot
            ), (
                "Property 59: shared Measurement_Records row is not "
                "byte-equivalent to its pre-attempt snapshot — a mutation "
                "attempt may have leaked through the AD-WS-36 trigger."
            )
            assert (
                _fetch_row(
                    engine,
                    "Measurement_Definition_Revisions",
                    "measurement_definition_revision_id",
                    shared_def_rev,
                )
                == shared_def_rev_snapshot
            ), (
                "Property 59: shared Measurement_Definition_Revisions row is "
                "not byte-equivalent to its pre-attempt snapshot — a mutation "
                "attempt may have leaked through the AD-WS-36 trigger."
            )

        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Property 59 — atomicity test: an audit-append failure rolls back the
# originating finalization and leaves it unobservable from any query path
# (Requirement 57.2 / Slice 1 Requirement 13.6).
# ---------------------------------------------------------------------------


class _FailingConsequentialAuditLog(AuditLog):
    """``AuditLog`` whose consequential append always raises.

    Denial and evaluation appends delegate to the real :class:`AuditLog`
    surface so the authorization evaluation continues to function; only the
    consequential append — the final step of the finalization — fails,
    forcing the caller's transaction to roll back.
    """

    def append_consequential(self, *args: Any, **kwargs: Any) -> AuditRecord:  # noqa: ANN401
        raise AuditAppendError(
            "Property 59 forced consequential audit-append failure."
        )


# Feature: fourth-walking-slice, Property 59: Audit completeness and atomicity for every outcome-measurement action
@given(suffix=st.integers(min_value=0, max_value=10_000))
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_audit_append_failure_rolls_back_outcome_finalization(
    suffix: int,
) -> None:
    """When the consequential audit append fails during a Measurement
    Definition finalization, the create raises, no
    ``Measurement_Definitions`` / ``Measurement_Definition_Revisions`` /
    ``Relationships`` row survives, and no consequential ``Audit_Records``
    row carrying the operation's ``correlation_id`` is observable from any
    query path.

    **Validates: Requirements 57.1, 57.2, 61.14**
    """
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop59_fail_"
    ) as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            clock = FixedClock(_NOW)
            identity = IdentityService()
            healthy_audit = AuditLog(clock)
            failing_audit = _FailingConsequentialAuditLog(clock)

            # The authorization service keeps a healthy audit log so the
            # permit evaluation succeeds and the finalization reaches the
            # consequential append (which then fails).
            authz = AuthorizationService(
                clock=clock,
                audit_log=healthy_audit,
                identity_service=identity,
            )
            intended = IntendedOutcomeService(
                clock=clock,
                identity_service=identity,
                audit_log=healthy_audit,
                authorization_service=authz,
            )
            failing_definitions = MeasurementDefinitionService(
                clock=clock,
                identity_service=identity,
                audit_log=failing_audit,
                authorization_service=authz,
                intended_outcome_reader=intended,
            )

            # Seed parties, objective, and role grants (reuse the shared
            # helpers via a throwaway service bundle's authorization service is
            # unnecessary — seed directly against ``authz``).
            with engine.begin() as conn:
                _seed_party(conn, _OWNER_PARTY_ID, "Intended Outcome Owner")
                _seed_party(
                    conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward"
                )
                _seed_party(conn, _DEFINER_PARTY_ID, "Measurement Definer")
            _seed_objective(engine)
            for party_id, role_name, authority in (
                (_OWNER_PARTY_ID, "intended_outcome_owner", "modify"),
                (
                    _DEFINER_PARTY_ID,
                    "measurement_definer",
                    "define_measurement",
                ),
            ):
                _assign_role(
                    authz,
                    engine,
                    party_id=party_id,
                    role_name=role_name,
                    authority=authority,
                )

            # Mint one Intended Outcome via the healthy service so the
            # finalization has a valid anchor.
            with engine.begin() as conn:
                intended_result = intended.create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition=(
                        "Onboarding completes in under two days."
                    ),
                    observation_window="30 days post launch",
                    attribution_assumption="Sampling rate held constant.",
                    authoring_party_id=_OWNER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            io_rev = intended_result.intended_outcome_revision_id

            # Pre-attempt baselines.
            relationships_before = _count(engine, "Relationships")
            correlation_id = f"prop59-fail-{suffix:08d}"

            with pytest.raises(AuditAppendError):
                with engine.begin() as conn:
                    failing_definitions.create_measurement_definition(
                        conn,
                        target_intended_outcome_revision_id=io_rev,
                        measurand_description="Adoption rate of the workflow.",
                        unit_of_measure=_UNIT,
                        observation_window=_WINDOW_2025,
                        cadence="monthly",
                        data_source="product analytics",
                        authoring_party_id=_DEFINER_PARTY_ID,
                        applicable_scope=_SCOPE,
                        engine=engine,
                        correlation_id=correlation_id,
                    )

            # The finalization rolled back: no Measurement Definition Resource,
            # Revision, or new Relationship row survives.
            assert _count(engine, "Measurement_Definitions") == 0, (
                "Property 59: a Measurement_Definitions row survived an "
                "audit-append failure — the finalization must roll back "
                "(Requirement 57.2)."
            )
            assert (
                _count(engine, "Measurement_Definition_Revisions") == 0
            ), (
                "Property 59: a Measurement_Definition_Revisions row survived "
                "an audit-append failure — the finalization must roll back "
                "(Requirement 57.2)."
            )
            assert (
                _count(engine, "Relationships") == relationships_before
            ), (
                "Property 59: a Relationships row survived an audit-append "
                "failure — the finalization (including its Addresses "
                "Relationship) must roll back (Requirement 57.2)."
            )

            # No consequential audit row carrying the operation correlation_id
            # is observable from any query path.
            assert (
                _count_consequential_rows_for_correlation(
                    engine, correlation_id
                )
                == 0
            ), (
                "Property 59: a consequential Audit_Records row survived an "
                "audit-append failure — the finalization is observable, "
                "violating Requirement 57.2 / Slice 1 Requirement 13.6."
            )

        finally:
            engine.dispose()
