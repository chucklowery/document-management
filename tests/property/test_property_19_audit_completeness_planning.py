# Feature: second-walking-slice, Property 19: Audit completeness for consequential and denied planning actions
"""Property 19 — Audit completeness for consequential and denied planning actions (task 16.4).

**Property 19: Audit completeness for consequential and denied planning actions**

For all consequential planning writes (Objective, Intended Outcome,
Project, Deliverable Expectation, Activity Plan, Plan Revision, Plan
Review, Plan Approval) and all denied attempts (denied authorization
on any of those creations *and* attempted modifications of an Approved
Plan Revision), exactly one ``Audit_Records`` row exists per operation
that matches the originating call on ``actor_party_id``,
``action_type``, ``target_id``, ``target_revision_id``, ``outcome``,
``recorded_at``, and ``correlation_id``. Denied attempts leave no
in-flight planning write persisted.

**Validates: Requirements 2.7, 7.6, 16.1, 16.2, 16.5**

Strategy:

Each Hypothesis case (a) seeds a fresh per-test SQLite engine with the
Slice 1 schema, the Slice 2 schema, the Slice 1 disclosure policy, the
Slice 2 disclosure coverage rows, the three Parties needed by the
authority gates, one Role Assignment granting the authorized Party the
union of ``modify``, ``review``, and ``approve`` over the case scope,
one ``Accept`` Decision via :class:`KnowledgeService` (so Objective
creation has a resolvable target), and one full pre-seeded planning
spine — Objective → Intended Outcome → Project → Deliverable
Expectation → Activity Plan → one draft Plan Revision (the *review
host*) → one already-``approved`` Plan Revision (the *immutability
host*, INSERTed directly so the AD-WS-19 UPDATE trigger never fires
during seeding). The spine is created via the unwired Planning_Service
constructors so a single seed is reproducible across cases; the
``Audit_Records`` rows appended during seeding are tagged with a
distinct correlation-id prefix so the per-op post-hoc assertion can
filter them out.

(b) draws a sequence of 1..10 operations from a closed alphabet:

- ``create_objective_permit`` / ``create_objective_deny``
  (``ObjectiveService.create_objective`` — Requirement 2.7
  consequential audit row, AD-WS-9 separate-transaction Denial Record
  per Requirement 7.6).
- ``create_intended_outcome_permit`` / ``create_intended_outcome_deny``.
- ``create_project_permit`` / ``create_project_deny``.
- ``create_deliverable_expectation_permit`` /
  ``create_deliverable_expectation_deny``.
- ``create_activity_plan_permit`` / ``create_activity_plan_deny``.
- ``create_plan_revision_permit`` / ``create_plan_revision_deny``.
- ``create_plan_review_permit`` / ``create_plan_review_deny`` —
  target the pre-seeded *review host* draft Plan Revision (Plan
  Reviews leave their target Plan Revision byte-equivalent in draft
  per Requirement 8.7 so the same target works for every op).
- ``create_plan_approval_permit`` —
  :meth:`PlanApprovalService.create_plan_approval` with
  ``outcome='Reject_Approval'``. The UNIQUE constraint on
  ``Plan_Approval_Records.target_plan_revision_id`` (Requirement 9.5)
  means each permit attempt needs a fresh draft Plan Revision; the op
  INSERTs one inline (bypassing :class:`PlanRevisionService` so the
  fresh-revision INSERT does not show up under the same correlation
  identifier as the Plan Approval audit row).
- ``create_plan_approval_deny`` — Targets a per-op fresh draft Plan
  Revision so the deny row is unambiguous.
- ``attempt_modify_approved_plan_revision`` —
  :func:`walking_slice.planning._immutability.enforce_approved_plan_revision_immutability`
  against the pre-seeded *immutability host* approved Plan Revision.
  This is the only path that exercises the Requirement 9.6 / 16.5
  "modify approved Resource" denial: the helper detects the approved
  lifecycle state, appends a Denial Record in a separate transaction
  with ``reason_code='approved-plan-revision-immutable'`` (an additive
  reason code per Requirement 13.2's extensibility clause), and
  raises :class:`ApprovedPlanRevisionImmutableError`.

Every operation is invoked with an explicit ``correlation_id`` so the
post-hoc assertion can locate its audit row(s) deterministically. Each
operation also records the expected ``outcome``, ``action_type``,
``actor_party_id``, ``target_id``, and ``target_revision_id`` so the
audit row content can be compared field-by-field.

Assertions per case (run after the whole scenario has executed):

1. **Existence and uniqueness.** For every recorded operation there
   is exactly one ``Audit_Records`` row matching its
   ``(correlation_id, outcome)`` pair for ``consequential`` writes,
   and exactly one *Denial Record* (the deny row with
   ``authorities_required IS NULL``) for every denied attempt. The
   authorization-evaluation row (which also carries ``outcome='deny'``
   when authorization denies) is filtered out by
   ``authorities_required IS NOT NULL`` so the property statement's
   "exactly one Denial Record" pins the dedicated audit row.
2. **Attribute fidelity.** The row's ``actor_party_id``,
   ``action_type``, ``target_id``, ``target_revision_id``, and
   ``correlation_id`` are byte-equal to the expected values
   captured at call time (Requirements 16.1, 16.2).
3. **Recorded time.** The row's ``recorded_at`` matches the
   slice-wide millisecond-precision UTC pattern
   ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (Requirement 16.1).
4. **Denial leaves no in-flight write.** For every denied attempt,
   the planning tables (``Objectives``, ``Objective_Revisions``,
   ``Intended_Outcomes``, …, ``Plan_Approval_Records``) carry no
   row originating from that op (Requirement 7.6 / 16.5).

Test scaffolding mirrors the conventions established by
``tests/property/test_property_11_audit_completeness.py``: per-case
:class:`tempfile.TemporaryDirectory` ownership of the SQLite file, a
:class:`~walking_slice.clock.FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps across
shrinks, and ``@settings(max_examples=100, deadline=2000,
suppress_health_check=[HealthCheck.too_slow])``.
"""

from __future__ import annotations

import re
import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.disclosure import seed as seed_disclosure
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    KnowledgeService,
    SupportRef,
)
from walking_slice.evidence import EvidenceRepository
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import seed_planning_coverage
from walking_slice.planning._immutability import (
    APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE,
    ApprovedPlanRevisionImmutableError,
    enforce_approved_plan_revision_immutability,
)
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.activity_plans import (
    ActivityPlanAuthorizationError,
    ActivityPlanService,
)
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationAuthorizationError,
    DeliverableExpectationService,
)
from walking_slice.planning.intended_outcomes import (
    IntendedOutcomeAuthorizationError,
    IntendedOutcomeService,
)
from walking_slice.planning.objectives import (
    ObjectiveAuthorizationError,
    ObjectiveService,
)
from walking_slice.planning.plan_approvals import (
    PlanApprovalAuthorizationError,
    PlanApprovalService,
)
from walking_slice.planning.plan_reviews import (
    PlanReviewAuthorizationError,
    PlanReviewService,
)
from walking_slice.planning.plan_revisions import (
    PlanRevisionAuthorizationError,
    PlanRevisionService,
)
from walking_slice.planning.projects import (
    ProjectAuthorizationError,
    ProjectService,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

# Two Parties cover every actor role the operation alphabet needs:
# the authorized Party owns one Role Assignment carrying every
# authority Slice 2 mints actions for (``modify`` for creations,
# ``review`` for Plan Review, ``approve`` for Plan Approval), and the
# unauthorized Party holds no Role Assignment so every authorization
# evaluation against them denies with reason
# ``no-role-assignment``.
_PARTY_AUTHORIZED: Final[str] = "00000000-0000-7000-8000-000000010001"
_PARTY_UNAUTHORIZED: Final[str] = "00000000-0000-7000-8000-000000010002"
_PARTY_ASSIGNING: Final[str] = "00000000-0000-7000-8000-000000010003"

# Scope used by every Slice 2 attempt and by the seeded Role
# Assignment so the authorization evaluation permits the authorized
# Party and denies the unauthorized Party.
_SCOPE: Final[str] = "property-19/scope"

# Authority-basis identifier referenced by every authority-bearing
# request (Plan Review, Plan Approval). Property 19 does not exercise
# authority-basis selection; the value is fixed so every
# :class:`AuthorityBasisRef` validates without further bookkeeping.
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-00000000a019"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Operation alphabet — the closed set of operations Hypothesis draws
# from. Listed in :data:`_OPERATIONS` so the strategy and the
# per-operation dispatch read from one source of truth.
_OPERATIONS: Final[tuple[str, ...]] = (
    "create_objective_permit",
    "create_objective_deny",
    "create_intended_outcome_permit",
    "create_intended_outcome_deny",
    "create_project_permit",
    "create_project_deny",
    "create_deliverable_expectation_permit",
    "create_deliverable_expectation_deny",
    "create_activity_plan_permit",
    "create_activity_plan_deny",
    "create_plan_revision_permit",
    "create_plan_revision_deny",
    "create_plan_review_permit",
    "create_plan_review_deny",
    "create_plan_approval_permit",
    "create_plan_approval_deny",
    "attempt_modify_approved_plan_revision",
)

# Canonical millisecond-precision UTC text pattern. Centralized so the
# recorded-at assertion in the property body matches the format used
# everywhere else in the slice (Requirement 16.1).
_RECORDED_AT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)

# Seeded resource identifiers — small, stable, easy to read in
# shrunken counterexamples.
_DECISION_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-00000000b019"
)
_REVIEW_HOST_PLAN_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000019001"
)
_APPROVED_PLAN_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000019002"
)


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# Each Hypothesis case draws a list of 1..10 operations from the
# closed alphabet :data:`_OPERATIONS`. ``min_size=1`` guarantees at
# least one audited write per case. ``max_size=10`` keeps the test
# inside the 2 s Hypothesis deadline given the per-op setup costs and
# the denial path's separate-transaction Denial Record append (which
# is fast on the per-test SQLite WAL file).
# ---------------------------------------------------------------------------


_operation_strategy = st.sampled_from(_OPERATIONS)
_scenario_strategy = st.lists(_operation_strategy, min_size=1, max_size=10)



# ---------------------------------------------------------------------------
# Engine and seeding helpers.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with the slice's pragmas."""
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
    seed_disclosure(engine)
    with engine.begin() as conn:
        seed_planning_coverage(conn)
    return engine


def _seed_parties(engine: Engine) -> None:
    """Insert the three Party rows every consequential write FK-references."""
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_AUTHORIZED, "Property 19 Authorized"),
            (_PARTY_UNAUTHORIZED, "Property 19 Unauthorized"),
            (_PARTY_ASSIGNING, "Property 19 Assigning Authority"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, :ts)
                    """
                ),
                {"pid": party_id, "name": display, "ts": _NOW_ISO},
            )


def _seed_role_assignment(
    authorization_service: AuthorizationService, engine: Engine
) -> str:
    """Grant the authorized Party (modify, review, approve) over the scope.

    Bounded by ``[-30 days, +30 days]`` around the fixed test instant
    so every evaluation run inside the scenario falls inside the
    effective window. Returns the Role Assignment Identity for
    completeness; the test does not depend on it being stored beyond
    this call.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_AUTHORIZED,
        role_name="property_19_full",
        scope=_SCOPE,
        authorities_granted=("modify", "review", "approve"),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_PARTY_ASSIGNING,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_accept_decision(
    engine: Engine, knowledge_service: KnowledgeService,
    evidence_repository: EvidenceRepository,
) -> str:
    """Seed a Source Document → Region → Finding → Recommendation →
    Decision(``Accept``) chain via the unwired Knowledge_Service.

    Returns the Decision Identity. Objective creation requires the
    target Decision Identity to resolve to a Decision whose ``outcome``
    is ``'Accept'`` (Requirement 2.2 / AD-WS-21); this seed satisfies
    that prerequisite once per case so every ``create_objective_*``
    op can reuse the same target.
    """
    with engine.begin() as conn:
        doc = evidence_repository.create_document(
            conn,
            content_bytes=b"Property 19 seed content for the planning spine.",
            contributing_party_id=_PARTY_AUTHORIZED,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=10,
            contributing_party_id=_PARTY_AUTHORIZED,
        )
        finding = knowledge_service.create_finding(
            conn,
            statement="Property 19 seed finding statement.",
            authoring_party_id=_PARTY_AUTHORIZED,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                ),
            ],
        )
        rec = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_AUTHORIZED,
            derived_from_findings=[finding.finding_id],
            rationale="Property 19 seed recommendation.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=rec.recommendation_id,
            target_recommendation_revision_id=rec.recommendation_revision_id,
            outcome="Accept",
            rationale="Property 19 seed decision (Accept).",
            deciding_party_id=_PARTY_AUTHORIZED,
            authority_basis=AuthorityBasisRef(
                type="role-grant-id", id=_DECISION_BASIS_ID
            ),
            applicable_scope=_SCOPE,
        )
    return decision.decision_id


def _seed_planning_spine(
    engine: Engine,
    *,
    objective_service: ObjectiveService,
    intended_outcome_service: IntendedOutcomeService,
    project_service: ProjectService,
    deliverable_service: DeliverableExpectationService,
    activity_plan_service: ActivityPlanService,
    plan_revision_service: PlanRevisionService,
    decision_id: str,
) -> dict[str, str]:
    """Seed one Objective → … → Activity Plan → draft Plan Revision.

    Each artifact is created via its Slice 2 service so every Slice 2
    invariant (Identifier_Registry tagging, AD-WS-17 ``semantic_role``
    handling, AD-WS-5 atomic audit append) participates in the seed.
    The audit rows emitted during seeding carry a fixed prefix on
    ``correlation_id`` (``"prop19-seed-"``) so the per-op assertion
    loop can ignore them.

    Returns a mapping carrying every Identity downstream ops need to
    cite — the seed Objective Identity for ``create_intended_outcome``
    and ``create_project`` ops, the seed Project Identity for
    ``create_deliverable_expectation`` and ``create_activity_plan``
    ops, the seed Activity Plan Identity for ``create_plan_revision``
    ops, and the seed *review host* draft Plan Revision Identity for
    ``create_plan_review`` ops.
    """
    with engine.begin() as conn:
        objective = objective_service.create_objective(
            conn,
            statement="Property 19 seed objective.",
            rationale=None,
            target_decision_id=decision_id,
            authoring_party_id=_PARTY_AUTHORIZED,
            applicable_scope=_SCOPE,
            engine=engine,
            correlation_id="prop19-seed-objective",
        )
    with engine.begin() as conn:
        intended_outcome = intended_outcome_service.create_intended_outcome(
            conn,
            target_objective_id=objective.objective_id,
            success_condition="Property 19 seed success condition.",
            observation_window=None,
            attribution_assumption=None,
            authoring_party_id=_PARTY_AUTHORIZED,
            applicable_scope=_SCOPE,
            engine=engine,
            correlation_id="prop19-seed-intended-outcome",
        )
    from datetime import date as _date

    with engine.begin() as conn:
        project = project_service.create_project(
            conn,
            target_objective_id=objective.objective_id,
            name="Property 19 Seed Project",
            summary=None,
            planned_start_date=_date(2026, 1, 1),
            planned_end_date=_date(2026, 12, 31),
            authoring_party_id=_PARTY_AUTHORIZED,
            applicable_scope=_SCOPE,
            engine=engine,
            correlation_id="prop19-seed-project",
        )
    with engine.begin() as conn:
        deliverable = (
            deliverable_service.create_deliverable_expectation(
                conn,
                target_project_id=project.project_id,
                name="Property 19 Seed Deliverable",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_AUTHORIZED,
                applicable_scope=_SCOPE,
                engine=engine,
                correlation_id="prop19-seed-deliverable",
            )
        )
    with engine.begin() as conn:
        activity_plan = activity_plan_service.create_activity_plan(
            conn,
            target_project_id=project.project_id,
            title="Property 19 Seed Activity Plan",
            authoring_party_id=_PARTY_AUTHORIZED,
            applicable_scope=_SCOPE,
            engine=engine,
            correlation_id="prop19-seed-activity-plan",
        )
    with engine.begin() as conn:
        plan_revision = plan_revision_service.create_plan_revision(
            conn,
            target_activity_plan_id=activity_plan.activity_plan_id,
            planned_scope="Property 19 seed planned scope.",
            authoring_party_id=_PARTY_AUTHORIZED,
            applicable_scope=_SCOPE,
            engine=engine,
            correlation_id="prop19-seed-plan-revision",
        )
    return {
        "objective_id": objective.objective_id,
        "intended_outcome_id": intended_outcome.intended_outcome_id,
        "project_id": project.project_id,
        "deliverable_expectation_id": (
            deliverable.deliverable_expectation_id
        ),
        "activity_plan_id": activity_plan.activity_plan_id,
        "review_host_plan_revision_id": plan_revision.plan_revision_id,
    }


def _insert_draft_plan_revision_directly(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str,
    lifecycle_state: str = "draft",
) -> None:
    """INSERT a ``Plan_Revisions`` row by hand, bypassing the service.

    INSERTs into ``Plan_Revisions`` are not gated by the AD-WS-19
    lifecycle trigger (which only watches UPDATE), so seeding rows
    with ``lifecycle_state ∈ {'draft', 'approved'}`` is a direct
    INSERT — no pragma plumbing required. Used to mint the fresh
    draft Plan Revisions each ``create_plan_approval_*`` op needs
    (the UNIQUE constraint on
    ``Plan_Approval_Records.target_plan_revision_id`` means every
    Plan Approval op needs a fresh target) and to seed the
    pre-approved ``immutability host`` Plan Revision.

    These INSERTs do not emit audit rows; the property's
    post-hoc assertion therefore sees only the audit rows emitted by
    the scenario operations themselves.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :aid, NULL, :state, :scope_text, '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "scope_text": "Property 19 directly seeded plan revision.",
                "party": _PARTY_AUTHORIZED,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )



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

    When ``require_authorities_required_null`` is ``True`` the filter
    keeps only rows whose ``authorities_required`` column is NULL —
    that's the dedicated *Denial Record* (the consequential-style row
    written from the planning service's separate-transaction
    ``_persist_*_denial`` helper or from
    :func:`enforce_approved_plan_revision_immutability`). When
    ``False`` the filter keeps only rows whose
    ``authorities_required`` is non-NULL — the authorization
    evaluation row. When ``None`` no filter is applied.
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
                text(sql),
                {"cid": correlation_id, "outcome": outcome},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _count_planning_rows_for_correlation(
    engine: Engine, correlation_id: str
) -> int:
    """Return the number of planning rows whose audit row carries ``correlation_id``.

    Used by the denial branch to confirm no in-flight planning write
    survived. Each Slice 2 service appends its consequential audit
    row with ``action_type`` matching the entry in
    :data:`_CONSEQUENTIAL_ACTIONS`; if any row exists with our
    correlation identifier under any of those action types, a write
    leaked through the rollback.

    Returns the total count across the planning-write actions
    (``create.objective``, …, ``create.plan_approval``) and the
    immutability-violation attempted-action prefix
    (``update.plan_revision``). The expectation for denial ops is
    zero.
    """
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


# ---------------------------------------------------------------------------
# Per-operation dispatchers.
#
# Each dispatcher executes one operation against the seeded planning
# state and returns one expected-audit descriptor. A few ops also
# create transient state (e.g. ``create_plan_approval_permit`` mints a
# fresh draft Plan Revision before invoking the service) — those
# helper INSERTs do not emit audit rows because they use a direct
# SQL path rather than the planning service.
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
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 19: Audit completeness for consequential and denied planning actions
@given(scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case temp-directory + SQLite engine + Slice 1 schema +
    # Slice 2 schema + disclosure seed + planning spine seed +
    # multiple per-op writes is well-bounded but exceeds the
    # data-generation health check's default budget. Suppressing it
    # keeps the run deterministic without weakening the 2 s timing
    # budget (the denial path's separate-transaction Denial Record
    # never sleeps when the append succeeds on the first attempt).
    suppress_health_check=[HealthCheck.too_slow],
)
def test_audit_completeness_for_consequential_and_denied_planning_actions(
    scenario: list[str],
) -> None:
    """For every consequential planning write and every denied attempt
    run by the scenario, exactly one ``Audit_Records`` row exists
    carrying the expected ``actor_party_id``, ``action_type``,
    ``target_id``, ``target_revision_id``, ``outcome``,
    millisecond-precision ``recorded_at``, and ``correlation_id``.
    Every denial leaves no in-flight planning write persisted."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop19_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # Fresh services per case so :class:`IdentityService`
            # in-memory state and any audit-correlation accumulator
            # cannot leak across cases.
            clock = FixedClock(_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
            authorization_service = AuthorizationService(
                clock=clock,
                audit_log=audit_log,
                identity_service=identity_service,
            )
            evidence_repository = EvidenceRepository(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            knowledge_service_unwired = KnowledgeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            manifest_writer = ProvenanceManifestWriter(
                clock=clock, identity_service=identity_service
            )
            objective_service = ObjectiveService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                knowledge_service=knowledge_service_unwired,
            )
            intended_outcome_service = IntendedOutcomeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            project_service = ProjectService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            deliverable_service = DeliverableExpectationService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            activity_plan_service = ActivityPlanService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            plan_revision_service = PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            plan_review_service = PlanReviewService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            plan_approval_service = PlanApprovalService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                manifest_writer=manifest_writer,
            )

            # Seed Parties, the authorized Role Assignment, the
            # Slice 1 Accept Decision, the planning spine, and the
            # pre-approved Plan Revision used by the immutability op.
            _seed_parties(engine)
            _seed_role_assignment(authorization_service, engine)
            decision_id = _seed_accept_decision(
                engine, knowledge_service_unwired, evidence_repository
            )
            spine = _seed_planning_spine(
                engine,
                objective_service=objective_service,
                intended_outcome_service=intended_outcome_service,
                project_service=project_service,
                deliverable_service=deliverable_service,
                activity_plan_service=activity_plan_service,
                plan_revision_service=plan_revision_service,
                decision_id=decision_id,
            )
            _insert_draft_plan_revision_directly(
                engine,
                plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                activity_plan_id=spine["activity_plan_id"],
                lifecycle_state="approved",
            )

            # Expected audit-row descriptors — accumulated as
            # operations run; verified after the scenario finishes.
            expected_audit: list[dict[str, Any]] = []
            from datetime import date as _date

            for op_index, op in enumerate(scenario):
                # Stable per-operation correlation identifier so the
                # post-hoc assertion can locate the matching audit
                # row deterministically. Embedding the index and the
                # op name makes shrunken counterexamples easy to
                # read.
                correlation_id = (
                    f"prop19-op-{op_index:03d}-{op}-"
                    f"{uuid_lib.uuid4().hex[:8]}"
                )

                # ----- Objective ops ---------------------------------
                if op == "create_objective_permit":
                    with engine.begin() as conn:
                        result = objective_service.create_objective(
                            conn,
                            statement=(
                                f"Property 19 objective {op_index}."
                            ),
                            rationale=None,
                            target_decision_id=decision_id,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.objective",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.objective_id,
                            target_revision_id=result.objective_revision_id,
                        )
                    )
                elif op == "create_objective_deny":
                    with pytest.raises(ObjectiveAuthorizationError):
                        with engine.begin() as conn:
                            objective_service.create_objective(
                                conn,
                                statement=(
                                    f"Property 19 deny objective {op_index}."
                                ),
                                rationale=None,
                                target_decision_id=decision_id,
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.objective",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=decision_id,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Intended Outcome ops --------------------------
                elif op == "create_intended_outcome_permit":
                    with engine.begin() as conn:
                        result = (
                            intended_outcome_service.create_intended_outcome(
                                conn,
                                target_objective_id=spine["objective_id"],
                                success_condition=(
                                    f"Property 19 intended outcome {op_index}."
                                ),
                                observation_window=None,
                                attribution_assumption=None,
                                authoring_party_id=_PARTY_AUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.intended_outcome",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.intended_outcome_id,
                            target_revision_id=(
                                result.intended_outcome_revision_id
                            ),
                        )
                    )
                elif op == "create_intended_outcome_deny":
                    with pytest.raises(IntendedOutcomeAuthorizationError):
                        with engine.begin() as conn:
                            intended_outcome_service.create_intended_outcome(
                                conn,
                                target_objective_id=spine["objective_id"],
                                success_condition=(
                                    f"Property 19 deny io {op_index}."
                                ),
                                observation_window=None,
                                attribution_assumption=None,
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.intended_outcome",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["objective_id"],
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Project ops -----------------------------------
                elif op == "create_project_permit":
                    with engine.begin() as conn:
                        result = project_service.create_project(
                            conn,
                            target_objective_id=spine["objective_id"],
                            name=f"Property 19 project {op_index}",
                            summary=None,
                            planned_start_date=_date(2026, 1, 1),
                            planned_end_date=_date(2026, 12, 31),
                            authoring_party_id=_PARTY_AUTHORIZED,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.project",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.project_id,
                            target_revision_id=result.project_revision_id,
                        )
                    )
                elif op == "create_project_deny":
                    with pytest.raises(ProjectAuthorizationError):
                        with engine.begin() as conn:
                            project_service.create_project(
                                conn,
                                target_objective_id=spine["objective_id"],
                                name=f"Property 19 deny project {op_index}",
                                summary=None,
                                planned_start_date=_date(2026, 1, 1),
                                planned_end_date=_date(2026, 12, 31),
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.project",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["objective_id"],
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Deliverable Expectation ops -------------------
                elif op == "create_deliverable_expectation_permit":
                    with engine.begin() as conn:
                        result = (
                            deliverable_service.create_deliverable_expectation(
                                conn,
                                target_project_id=spine["project_id"],
                                name=f"Property 19 deliverable {op_index}",
                                description=None,
                                deliverable_kind="Document",
                                acceptance_criteria=None,
                                authoring_party_id=_PARTY_AUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.deliverable_expectation",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.deliverable_expectation_id,
                            target_revision_id=(
                                result.deliverable_expectation_revision_id
                            ),
                        )
                    )
                elif op == "create_deliverable_expectation_deny":
                    with pytest.raises(
                        DeliverableExpectationAuthorizationError
                    ):
                        with engine.begin() as conn:
                            deliverable_service.create_deliverable_expectation(
                                conn,
                                target_project_id=spine["project_id"],
                                name=(
                                    f"Property 19 deny deliverable {op_index}"
                                ),
                                description=None,
                                deliverable_kind="Document",
                                acceptance_criteria=None,
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.deliverable_expectation",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["project_id"],
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Activity Plan ops -----------------------------
                elif op == "create_activity_plan_permit":
                    with engine.begin() as conn:
                        result = activity_plan_service.create_activity_plan(
                            conn,
                            target_project_id=spine["project_id"],
                            title=f"Property 19 plan {op_index}",
                            authoring_party_id=_PARTY_AUTHORIZED,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.activity_plan",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.activity_plan_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_activity_plan_deny":
                    with pytest.raises(ActivityPlanAuthorizationError):
                        with engine.begin() as conn:
                            activity_plan_service.create_activity_plan(
                                conn,
                                target_project_id=spine["project_id"],
                                title=f"Property 19 deny plan {op_index}",
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.activity_plan",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["project_id"],
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Plan Revision ops -----------------------------
                elif op == "create_plan_revision_permit":
                    with engine.begin() as conn:
                        result = plan_revision_service.create_plan_revision(
                            conn,
                            target_activity_plan_id=spine["activity_plan_id"],
                            planned_scope=(
                                f"Property 19 plan revision {op_index}."
                            ),
                            authoring_party_id=_PARTY_AUTHORIZED,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    # Plan Revisions live in a single Revision-level
                    # table without a separate Resource header, so
                    # the consequential audit row carries
                    # ``target_id = target_revision_id =
                    # plan_revision_id`` (the row's own identifier
                    # on both columns) — matching the convention in
                    # :class:`PlanRevisionService.create_plan_revision`.
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.plan_revision",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.plan_revision_id,
                            target_revision_id=result.plan_revision_id,
                        )
                    )
                elif op == "create_plan_revision_deny":
                    with pytest.raises(PlanRevisionAuthorizationError):
                        with engine.begin() as conn:
                            plan_revision_service.create_plan_revision(
                                conn,
                                target_activity_plan_id=(
                                    spine["activity_plan_id"]
                                ),
                                planned_scope=(
                                    f"Property 19 deny revision {op_index}."
                                ),
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.plan_revision",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["activity_plan_id"],
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Plan Review ops -------------------------------
                elif op == "create_plan_review_permit":
                    with engine.begin() as conn:
                        result = plan_review_service.create_plan_review(
                            conn,
                            target_plan_revision_id=(
                                spine["review_host_plan_revision_id"]
                            ),
                            outcome="Endorse",
                            rationale=(
                                f"Property 19 plan review rationale {op_index}."
                            ),
                            reviewing_party_id=_PARTY_AUTHORIZED,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.plan_review",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.plan_review_id,
                            target_revision_id=result.plan_review_revision_id,
                        )
                    )
                elif op == "create_plan_review_deny":
                    with pytest.raises(PlanReviewAuthorizationError):
                        with engine.begin() as conn:
                            plan_review_service.create_plan_review(
                                conn,
                                target_plan_revision_id=(
                                    spine["review_host_plan_revision_id"]
                                ),
                                outcome="Endorse",
                                rationale=(
                                    f"Property 19 deny review {op_index}."
                                ),
                                reviewing_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.plan_review",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["review_host_plan_revision_id"],
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Plan Approval ops -----------------------------
                elif op == "create_plan_approval_permit":
                    # Mint a fresh draft Plan Revision via direct
                    # INSERT (bypass :class:`PlanRevisionService` so
                    # the fresh-revision INSERT does not emit an
                    # audit row under our correlation identifier).
                    # The UNIQUE constraint on
                    # ``Plan_Approval_Records.target_plan_revision_id``
                    # means each permit attempt needs a fresh target.
                    fresh_revision_id = (
                        f"00000000-0000-7000-8000-{op_index:012x}"
                    )
                    _insert_draft_plan_revision_directly(
                        engine,
                        plan_revision_id=fresh_revision_id,
                        activity_plan_id=spine["activity_plan_id"],
                        lifecycle_state="draft",
                    )
                    with engine.begin() as conn:
                        result = plan_approval_service.create_plan_approval(
                            conn,
                            engine,
                            target_plan_revision_id=fresh_revision_id,
                            outcome="Reject_Approval",
                            rationale=(
                                f"Property 19 approval rationale {op_index}."
                            ),
                            approving_party_id=_PARTY_AUTHORIZED,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.plan_approval",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.plan_approval_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_plan_approval_deny":
                    fresh_revision_id = (
                        f"00000000-0000-7000-8000-{(op_index + 1000):012x}"
                    )
                    _insert_draft_plan_revision_directly(
                        engine,
                        plan_revision_id=fresh_revision_id,
                        activity_plan_id=spine["activity_plan_id"],
                        lifecycle_state="draft",
                    )
                    with pytest.raises(PlanApprovalAuthorizationError):
                        with engine.begin() as conn:
                            plan_approval_service.create_plan_approval(
                                conn,
                                engine,
                                target_plan_revision_id=fresh_revision_id,
                                outcome="Approve",
                                rationale=(
                                    f"Property 19 deny approval {op_index}."
                                ),
                                approving_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.plan_approval",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=spine["activity_plan_id"],
                            target_revision_id=fresh_revision_id,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Immutability denial op -----------------------
                elif op == "attempt_modify_approved_plan_revision":
                    # Target the pre-seeded approved Plan Revision.
                    # The helper detects the approved lifecycle
                    # state and appends a Denial Record in a
                    # separate transaction.
                    with pytest.raises(
                        ApprovedPlanRevisionImmutableError
                    ):
                        enforce_approved_plan_revision_immutability(
                            engine=engine,
                            audit_log=audit_log,
                            target_plan_revision_id=(
                                _APPROVED_PLAN_REVISION_ID
                            ),
                            actor_party_id=_PARTY_AUTHORIZED,
                            attempted_action="update.plan_revision",
                            correlation_id=correlation_id,
                            recorded_time=_NOW,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="update.plan_revision",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=None,
                            target_revision_id=_APPROVED_PLAN_REVISION_ID,
                            require_authorities_required_null=True,
                        )
                    )

                else:  # pragma: no cover - defensive
                    raise AssertionError(f"unknown op: {op!r}")

            # ---------------------------------------------------------
            # Post-hoc assertions — one pass over the collected
            # expected-audit descriptors.
            # ---------------------------------------------------------
            for expected in expected_audit:
                rows = _fetch_audit_rows_for(
                    engine,
                    correlation_id=expected["correlation_id"],
                    outcome=expected["outcome"],
                    require_authorities_required_null=(
                        expected["require_authorities_required_null"]
                    ),
                )

                # --- (1) Existence and uniqueness -------------------
                # Property 19 demands exactly one matching
                # consequential row per consequential write, and
                # exactly one matching Denial Record per denied
                # attempt. The denial branch is restricted to
                # ``authorities_required IS NULL`` so the
                # authorization-evaluation row (which also carries
                # ``outcome='deny'`` when authorization denies but
                # populates ``authorities_required``) is filtered
                # out — leaving the dedicated Denial Record as the
                # single matching row.
                assert len(rows) == 1, (
                    f"Property 19: expected exactly one Audit_Records "
                    f"row with correlation_id="
                    f"{expected['correlation_id']!r}, outcome="
                    f"{expected['outcome']!r}, and "
                    f"authorities_required filter="
                    f"{expected['require_authorities_required_null']!r}; "
                    f"got {len(rows)} ({rows!r})."
                )
                row = rows[0]

                # --- (2) Attribute fidelity --------------------------
                assert (
                    row["actor_party_id"] == expected["actor_party_id"]
                ), (
                    f"Property 19: audit row "
                    f"{row['audit_record_id']!r} has actor_party_id="
                    f"{row['actor_party_id']!r}; expected "
                    f"{expected['actor_party_id']!r} for "
                    f"correlation_id={expected['correlation_id']!r}."
                )
                assert row["action_type"] == expected["action_type"], (
                    f"Property 19: audit row "
                    f"{row['audit_record_id']!r} has action_type="
                    f"{row['action_type']!r}; expected "
                    f"{expected['action_type']!r} for "
                    f"correlation_id={expected['correlation_id']!r}."
                )
                assert row["target_id"] == expected["target_id"], (
                    f"Property 19: audit row "
                    f"{row['audit_record_id']!r} has target_id="
                    f"{row['target_id']!r}; expected "
                    f"{expected['target_id']!r} for "
                    f"correlation_id={expected['correlation_id']!r}."
                )
                assert (
                    row["target_revision_id"]
                    == expected["target_revision_id"]
                ), (
                    f"Property 19: audit row "
                    f"{row['audit_record_id']!r} has "
                    f"target_revision_id="
                    f"{row['target_revision_id']!r}; expected "
                    f"{expected['target_revision_id']!r} for "
                    f"correlation_id={expected['correlation_id']!r}."
                )
                assert (
                    row["correlation_id"] == expected["correlation_id"]
                ), (
                    f"Property 19: audit row "
                    f"{row['audit_record_id']!r} has correlation_id="
                    f"{row['correlation_id']!r}; expected "
                    f"{expected['correlation_id']!r}."
                )

                # --- (3) Recorded time format -----------------------
                assert _RECORDED_AT_PATTERN.match(row["recorded_at"]), (
                    f"Property 19: audit row "
                    f"{row['audit_record_id']!r} has recorded_at="
                    f"{row['recorded_at']!r}; expected canonical "
                    f"millisecond-precision UTC text matching "
                    f"{_RECORDED_AT_PATTERN.pattern!r} "
                    "(Requirement 16.1)."
                )

                # --- (4) Denial leaves no in-flight write -----------
                # For every denied op, no consequential planning
                # write should share the op's correlation
                # identifier. The denial path is either a
                # separate-transaction Denial Record append after
                # the caller's transaction rolled back (Requirement
                # 7.6) or the immutability helper which never
                # opens the caller's transaction at all
                # (Requirement 9.6). Either way the planning
                # write must not have committed.
                if expected["outcome"] == "deny":
                    consequential_count = (
                        _count_planning_rows_for_correlation(
                            engine, expected["correlation_id"]
                        )
                    )
                    assert consequential_count == 0, (
                        f"Property 19: denied attempt with "
                        f"correlation_id={expected['correlation_id']!r} "
                        f"left {consequential_count} consequential "
                        f"Audit_Records row(s) — a denied attempt "
                        f"must leave no in-flight planning write "
                        f"(Requirements 7.6, 16.5)."
                    )

        finally:
            engine.dispose()
