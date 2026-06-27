# Feature: third-walking-slice, Property 35: Plan / Execution separation
"""Property 35 — Plan / Execution separation enforced from the execution side
(task 16.5).

**Property 35: Plan / Execution separation enforced from the execution side**

For all request bodies submitted to any Execution_Service or
Deliverable_Repository endpoint, if the body contains any top-level
field whose name matches a prohibited planning-attribute prefix
(``planned-``, ``planning-assumption-``, ``ordering-rationale-``,
``plan-review-``, ``plan-approval-``), the request is rejected with no
row persisted in any Slice 3 table and no row of any Slice 2 planning
table is mutated as a consequence.

**Validates: Requirements 33.1, 33.2, 33.3, 33.4, 40.3, 40.4, 41.5**

Strategy
========

The property statement bundles three sub-invariants. This module
exercises all of them inside a single Hypothesis-driven property test
that drives all seven Slice 3 service write surfaces (the six
Execution_Service services plus the Deliverable_Repository) through a
prohibited planning-attribute key:

1. **Rejection invariant** (Requirements 33.2 / 33.3 / 33.4): every
   request body that names a top-level field matching one of the five
   planning-attribute prefixes must be rejected with a
   ``*ValidationError`` exposing ``failed_constraint =
   'prohibited_attribute'`` and the offending key on
   :attr:`prohibited_keys`.

2. **No-Slice-3-row-persisted invariant** (Requirement 41.5 / Property
   35's "no row persisted"): after rejection, every Slice 3 table —
   the six Execution_Service Record tables plus the two
   Deliverable_Repository Resource / Revision tables — must contain
   zero rows for this Hypothesis case.

3. **No-Slice-2-row-mutated invariant** (Requirements 33.1, 40.3,
   40.4 / Property 35's "no row of any Slice 2 planning table is
   mutated"): the Slice 2 planning tables seeded in step "Seed" below
   must be byte-equivalent before and after the rejected Slice 3
   call. This is a non-vacuous guarantee — every case writes a real
   Slice 2 chain (one Project + Activity Plan + draft Plan Revision +
   Plan Review + Plan Approval Record + Objective + Intended Outcome +
   Deliverable Expectation), captures a row-level snapshot, runs the
   rejected Slice 3 call, and re-reads the same rows to confirm zero
   delta.

The Hypothesis strategy draws:

- ``service_kind``: one of the seven Slice 3 write surfaces:
  ``work_assignment``, ``work_event``, ``time_entry``,
  ``produced_deliverable``, ``deliverable_production``,
  ``milestone_acceptance``, ``completion``.
- ``prohibited_prefix``: one of the five planning-attribute prefixes
  enumerated in
  :data:`walking_slice.execution._helpers.PLANNING_PROHIBITED_PREFIXES`.
- ``tail``: 0..32 alphanumeric / hyphen / underscore characters
  appended to the prefix so the matcher's hyphen/underscore
  canonicalization is exercised by both variants.
- ``case_mode``: ``'lower'`` / ``'upper'`` / ``'title'`` so the
  case-insensitive matching contract is exercised explicitly.

The screen in every Slice 3 service runs *before* any database read,
authorization side-effect, identity-service touch, or target
resolution (see step 1 of every ``create_<entity>`` docstring), so the
test only needs the bare schemas plus a small Slice 2 seed — no
Parties, role assignments, Slice 3 rows, or Slice 1 source-evidence
chain are required to drive the rejection path.

Setup follows the conventions established by Property 22
(:mod:`tests.property.test_property_22_plan_execution_outcome_separation`)
and Property 21
(:mod:`tests.property.test_property_21_slice1_non_modification`):
per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory
state cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps.
"""

from __future__ import annotations

import tempfile
import uuid as uuid_lib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Mapping

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AuthorizationService
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import (
    DeliverableContentValidationError,
    DeliverableRepositoryService,
)
from walking_slice.disclosure import seed as disclosure_seed
from walking_slice.execution._helpers import PLANNING_PROHIBITED_PREFIXES
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import (
    CompletionService,
    CompletionValidationError,
)
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
    DeliverableProductionValidationError,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceService,
    MilestoneAcceptanceValidationError,
)
from walking_slice.execution.time_entries import (
    TimeEntryService,
    TimeEntryValidationError,
)
from walking_slice.execution.work_assignments import (
    WorkAssignmentService,
    WorkAssignmentValidationError,
)
from walking_slice.execution.work_events import (
    WorkEventService,
    WorkEventValidationError,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# Identifiers used for setup-only artifacts (Parties, prerequisite
# Slice 2 rows, authority-basis ids). Per-case fresh services mean
# these identifiers never collide across Hypothesis examples.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"
_SEED_TS: Final[str] = "2025-12-15T10:30:00.000Z"

_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNEE_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-35/scope"

# Slice 2 seed identifiers. Each follows the canonical UUIDv7-shaped
# textual convention used by the Slice 2 fixtures and unit tests. The
# seeded chain is intentionally minimal — one row per Slice 2 table
# named by Requirements 33.1 / 40.3 — so the no-mutation snapshot
# diff is dense but shrinks fast.
_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_OBJECTIVE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_INTENDED_OUTCOME_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"
_INTENDED_OUTCOME_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c4"
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c5"
_PROJECT_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c6"
_DELIVERABLE_EXPECTATION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c7"
_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000c8"
)
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c9"
_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000ca"
_PLAN_REVIEW_ID: Final[str] = "00000000-0000-7000-8000-0000000000cb"
_PLAN_REVIEW_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000cc"
_PLAN_APPROVAL_ID: Final[str] = "00000000-0000-7000-8000-0000000000cd"
_TARGET_DECISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000ce"

# Placeholder UUIDv7 used for kwargs whose value the service never
# inspects because the prohibited-attribute screen fires first. The
# typed kwargs themselves cannot carry a prohibited attribute (the
# signatures do not declare any such field); only ``request_attributes``
# is the rejection surface.
_PLACEHOLDER_UUID7: Final[str] = "00000000-0000-7000-8000-0000000000ff"


# ---------------------------------------------------------------------------
# Slice 3 tables that the rejection path MUST NOT populate.
#
# Property 35's "no row persisted" clause is verified by asserting
# every one of these tables is empty after a rejected request. Listed
# in Resource → Revision → Record order so a counterexample reads
# naturally.
# ---------------------------------------------------------------------------


_SLICE3_TABLES: Final[tuple[str, ...]] = (
    "Work_Assignment_Records",
    "Work_Event_Records",
    "Time_Entry_Records",
    "Deliverable_Resources",
    "Deliverable_Revisions",
    "Deliverable_Production_Records",
    "Milestone_Acceptance_Records",
    "Completion_Records",
)


# ---------------------------------------------------------------------------
# Slice 2 planning tables whose rows must remain byte-equivalent
# across the Slice 3 rejected action (Property 35's no-mutation clause
# / Requirements 33.1, 40.3, 40.4). Listed in Resource → Revision →
# Record order.
# ---------------------------------------------------------------------------


_SLICE2_PLANNING_TABLES: Final[tuple[str, ...]] = (
    "Objectives",
    "Objective_Revisions",
    "Intended_Outcomes",
    "Intended_Outcome_Revisions",
    "Projects",
    "Project_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    "Activity_Plans",
    "Plan_Revisions",
    "Plan_Reviews",
    "Plan_Review_Revisions",
    "Plan_Approval_Records",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a per-case engine carrying Slice 1 + Slice 2 + Slice 3 schemas.

    Mirrors :func:`tests.property.test_property_22_plan_execution_outcome_separation._build_engine`.
    Installs every schema the rejection path's ``SELECT COUNT(*)``
    probes will touch, plus the ``slice-default-2026`` Disclosure
    Policies row so :class:`DeliverableRepositoryService` and the
    Slice 3 services do not encounter a missing-policy lookup if the
    screen unexpectedly lets a request progress past step 1.
    """
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
    disclosure_seed(engine)
    return engine


# ---------------------------------------------------------------------------
# Slice 2 seed.
#
# One representative row per Slice 2 planning table. Direct INSERT
# keeps the seed deterministic — Slice 2 service paths would multiply
# the per-case work and slow shrinking without changing the invariant
# under test. Mirrors :func:`tests.property.test_property_21_slice1_non_modification._seed_slice1`.
# ---------------------------------------------------------------------------


def _seed_slice2(engine: Engine) -> None:
    """Populate one row in every Slice 2 planning table.

    The seed is FK-consistent so SQLite's foreign-key enforcement is
    satisfied on every INSERT. The recorded times are intentionally
    offset from the per-case clock so any row whose timestamp
    accidentally changes to the Slice 3 clock value would surface as
    a snapshot diff.
    """
    with engine.begin() as conn:
        # Parties (FK target for many rows).
        for party_id, display in (
            (_PARTY_ID, "Property 35 Actor"),
            (_ASSIGNEE_PARTY_ID, "Property 35 Assignee"),
        ):
            conn.execute(
                text(
                    "INSERT INTO Parties (party_id, kind, display_name, created_at) "
                    "VALUES (:pid, 'person', :name, :ts)"
                ),
                {"pid": party_id, "name": display, "ts": _SEED_TS},
            )

        # Objectives + Objective_Revisions.
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:id, :ts)"
            ),
            {"id": _OBJECTIVE_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :obj, NULL,
                    'Pre-Slice-3 objective statement.', NULL, :tdid,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REVISION_ID,
                "obj": _OBJECTIVE_ID,
                "tdid": _TARGET_DECISION_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Intended_Outcomes + Intended_Outcome_Revisions.
        conn.execute(
            text(
                "INSERT INTO Intended_Outcomes (intended_outcome_id, created_at) "
                "VALUES (:id, :ts)"
            ),
            {"id": _INTENDED_OUTCOME_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Intended_Outcome_Revisions (
                    intended_outcome_revision_id, intended_outcome_id,
                    parent_revision_id, target_objective_id,
                    success_condition, observation_window,
                    attribution_assumption, outcome_kind,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :io, NULL, :obj,
                    'Pre-Slice-3 success condition.', NULL, NULL,
                    'intended', :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _INTENDED_OUTCOME_REVISION_ID,
                "io": _INTENDED_OUTCOME_ID,
                "obj": _OBJECTIVE_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Projects + Project_Revisions.
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:id, :ts)"
            ),
            {"id": _PROJECT_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Project_Revisions (
                    project_revision_id, project_id, parent_revision_id,
                    target_objective_id, name, summary,
                    planned_start_date, planned_end_date,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pid, NULL, :obj,
                    'Pre-Slice-3 project name.', NULL,
                    '2026-01-01', '2026-12-31',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PROJECT_REVISION_ID,
                "pid": _PROJECT_ID,
                "obj": _OBJECTIVE_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Deliverable_Expectations + Revisions.
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:id, :ts)"
            ),
            {"id": _DELIVERABLE_EXPECTATION_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectation_Revisions (
                    deliverable_expectation_revision_id,
                    deliverable_expectation_id, parent_revision_id,
                    target_project_id, name, description,
                    deliverable_kind, acceptance_criteria,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :de, NULL, :pid,
                    'Pre-Slice-3 expectation name.', NULL,
                    'Document', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "de": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Activity_Plans.
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :id, :pid, 'Pre-Slice-3 activity plan title.',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "id": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Plan_Revisions — seeded with lifecycle_state='approved' so
        # the snapshot row covers the only Slice 2 row Slice 3 services
        # are allowed to *read* (via the AD-WS-30 read API). The
        # snapshot diff is what catches any accidental UPDATE.
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
                    :rev, :ap, NULL, 'approved',
                    'Pre-Slice-3 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PLAN_REVISION_ID,
                "ap": _ACTIVITY_PLAN_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Plan_Reviews + Plan_Review_Revisions.
        conn.execute(
            text(
                "INSERT INTO Plan_Reviews (plan_review_id, created_at) "
                "VALUES (:id, :ts)"
            ),
            {"id": _PLAN_REVIEW_ID, "ts": _SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Plan_Review_Revisions (
                    plan_review_revision_id, plan_review_id,
                    target_plan_revision_id, outcome, rationale,
                    reviewing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pr, :plan_rev,
                    'Endorse', 'Pre-Slice-3 review rationale.',
                    :party, 'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "rev": _PLAN_REVIEW_REVISION_ID,
                "pr": _PLAN_REVIEW_ID,
                "plan_rev": _PLAN_REVISION_ID,
                "party": _PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )

        # Plan_Approval_Records — the Plan Revision is approved so
        # exactly one Approval row exists per AD-WS-19 Requirement 9.5.
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :id, :ap, :plan_rev,
                    'Approve', 'Pre-Slice-3 approval rationale.',
                    :party, 'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "id": _PLAN_APPROVAL_ID,
                "ap": _ACTIVITY_PLAN_ID,
                "plan_rev": _PLAN_REVISION_ID,
                "party": _PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SEED_TS,
            },
        )


def _snapshot_slice2(engine: Engine) -> dict[str, list[tuple]]:
    """Return a dict mapping each Slice 2 table to a list of its rows.

    Each row is rendered as a tuple of every column value so the
    no-mutation diff covers every column. Rows are sorted by primary
    key so the snapshot is order-stable across re-reads (SQLite does
    not guarantee insertion order without an explicit ORDER BY).
    """
    snapshot: dict[str, list[tuple]] = {}
    with engine.connect() as conn:
        for table in _SLICE2_PLANNING_TABLES:
            rows = conn.execute(text(f"SELECT * FROM {table}")).all()
            # Sort by row tuple — every column is included so the
            # comparison catches the additive columns too.
            snapshot[table] = sorted(tuple(r) for r in rows)
    return snapshot


def _count(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


# ---------------------------------------------------------------------------
# Per-case service factory.
# ---------------------------------------------------------------------------


def _build_services() -> dict[str, Any]:
    """Construct the per-case Slice 3 service bundle.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state and any audit-correlation accumulator cannot bleed
    across shrinks. The prohibited-attribute screen runs before any
    collaborator is consulted, so the read-only Planning_Service and
    Deliverable_Repository collaborators do not need to be fully wired
    — they only need to be constructible.

    Returns a mapping keyed by service-kind label so the dispatch table
    below can look up the relevant service directly.
    """
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )

    # The Slice 2 read-only collaborators. Per AD-WS-30, only the
    # read-only ``get_plan_revision`` and ``get_revision`` methods are
    # consulted by Slice 3, so the constructors are populated with
    # ``None`` for the cross-request collaborators that the read APIs
    # do not use. The same pattern is used in the Slice 3 unit-test
    # fixtures (e.g.,
    # ``tests/unit/test_execution_work_assignments.py::plan_revision_reader``).
    plan_revision_reader = PlanRevisionService(
        clock=None,  # type: ignore[arg-type]
        identity_service=None,  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        authorization_service=None,  # type: ignore[arg-type]
    )
    expectation_reader = DeliverableExpectationService(
        clock=None,  # type: ignore[arg-type]
        identity_service=None,  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        authorization_service=None,  # type: ignore[arg-type]
    )
    project_resolver = ProjectResolver()

    deliverable_repository = DeliverableRepositoryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    work_assignment_service = WorkAssignmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        denial_audit_sleep=lambda _seconds: None,
    )
    work_event_service = WorkEventService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    time_entry_service = TimeEntryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    deliverable_production_service = DeliverableProductionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        deliverable_reader=deliverable_repository,
        planning_reader=expectation_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )
    milestone_acceptance_service = MilestoneAcceptanceService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        production_reader=deliverable_production_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    completion_service = CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )
    return {
        "work_assignment": work_assignment_service,
        "work_event": work_event_service,
        "time_entry": time_entry_service,
        "produced_deliverable": deliverable_repository,
        "deliverable_production": deliverable_production_service,
        "milestone_acceptance": milestone_acceptance_service,
        "completion": completion_service,
    }



# ---------------------------------------------------------------------------
# Per-service rejection-path drivers.
#
# Each entry tells the rejection-invariant test how to invoke one Slice 3
# write surface with a generated prohibited attribute on its
# ``request_attributes`` parameter:
#
# - ``error_class``: the ``*ValidationError`` the service raises on the
#   prohibited-attribute path. The test asserts both the class and the
#   ``failed_constraint`` / ``prohibited_keys`` attributes.
# - ``call``: closure that drives one service call with the supplied
#   ``request_attributes`` mapping. Receives the service bundle, the
#   engine, and the request_attributes mapping; the typed kwargs the
#   closure passes are valid-looking but never inspected because the
#   prohibited-attribute screen fires first (see step 1 of every
#   ``create_<entity>`` docstring).
# ---------------------------------------------------------------------------


def _call_work_assignment(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["work_assignment"].create_work_assignment(
            conn,
            target_plan_revision_id=_PLACEHOLDER_UUID7,
            assignee_party_id=_ASSIGNEE_PARTY_ID,
            assignment_authority_party_id=_PARTY_ID,
            assignment_rationale=None,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_work_event(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["work_event"].create_work_event(
            conn,
            target_work_assignment_id=_PLACEHOLDER_UUID7,
            event_kind="started",
            event_note=None,
            recording_party_id=_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_time_entry(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["time_entry"].create_time_entry(
            conn,
            target_work_assignment_id=_PLACEHOLDER_UUID7,
            effort_hours=Decimal("1.00"),
            effort_period_start=_NOW,
            effort_period_end=_NOW,
            recording_party_id=_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_produced_deliverable(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["produced_deliverable"].create_produced_deliverable(
            conn,
            content_bytes=b"x",
            content_type="text/plain",
            produced_deliverable_name="placeholder",
            originating_work_assignment_id=_PLACEHOLDER_UUID7,
            authoring_party_id=_PARTY_ID,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_deliverable_production(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["deliverable_production"].create_deliverable_production(
            conn,
            source_work_assignment_id=_PLACEHOLDER_UUID7,
            produced_deliverable_revision_id=_PLACEHOLDER_UUID7,
            target_deliverable_expectation_revision_id=_PLACEHOLDER_UUID7,
            production_rationale=None,
            recording_party_id=_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_milestone_acceptance(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["milestone_acceptance"].create_milestone_acceptance(
            conn,
            source_deliverable_production_id=_PLACEHOLDER_UUID7,
            outcome="Accept",
            rationale="placeholder",
            accepting_party_id=_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_completion(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["completion"].create_completion(
            conn,
            target_plan_revision_id=_PLACEHOLDER_UUID7,
            outcome="Completed",
            rationale="placeholder",
            source_milestone_acceptance_ids=(),
            completing_party_id=_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


_SERVICE_DISPATCH: Final[dict[str, dict[str, Any]]] = {
    "work_assignment": {
        "error_class": WorkAssignmentValidationError,
        "call": _call_work_assignment,
    },
    "work_event": {
        "error_class": WorkEventValidationError,
        "call": _call_work_event,
    },
    "time_entry": {
        "error_class": TimeEntryValidationError,
        "call": _call_time_entry,
    },
    "produced_deliverable": {
        "error_class": DeliverableContentValidationError,
        "call": _call_produced_deliverable,
    },
    "deliverable_production": {
        "error_class": DeliverableProductionValidationError,
        "call": _call_deliverable_production,
    },
    "milestone_acceptance": {
        "error_class": MilestoneAcceptanceValidationError,
        "call": _call_milestone_acceptance,
    },
    "completion": {
        "error_class": CompletionValidationError,
        "call": _call_completion,
    },
}


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


# Tail characters appended to a chosen prohibited prefix. ASCII
# alphanumerics plus ``-`` and ``_`` so the matcher's
# hyphen/underscore canonicalization is exercised by both variants.
# Length 0..32 keeps cases cheap while still spanning the full prefix
# matching surface.
_TAIL_ALPHABET: Final[str] = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_"
)


@st.composite
def _prohibited_planning_attribute(draw: Any) -> dict[str, Any]:
    """Draw one ``(service_kind, prohibited_key)`` rejection scenario.

    Steps:

    1. Pick a service kind from the seven Slice 3 write surfaces
       (six Execution_Service services plus the
       Deliverable_Repository).
    2. Pick a prefix from
       :data:`walking_slice.execution._helpers.PLANNING_PROHIBITED_PREFIXES`
       — Property 35 covers planning-attribute prefixes only.
    3. Generate a random tail of 0..32 alphanumeric / hyphen /
       underscore characters and concatenate to the prefix.
    4. Optionally swap the case of the resulting key
       (case-insensitive matching is part of the screen contract —
       see :func:`walking_slice.execution._helpers._normalize_key`).

    The returned dict is consumed by
    :func:`test_prohibited_planning_attribute_rejected_no_slice2_mutation`.
    """
    service_kind = draw(st.sampled_from(sorted(_SERVICE_DISPATCH.keys())))
    prefix = draw(st.sampled_from(list(PLANNING_PROHIBITED_PREFIXES)))
    tail = draw(st.text(alphabet=_TAIL_ALPHABET, min_size=0, max_size=32))
    key = prefix + tail
    case_mode = draw(st.sampled_from(("lower", "upper", "title")))
    if case_mode == "upper":
        key = key.upper()
    elif case_mode == "title":
        key = key.title()
    return {
        "service_kind": service_kind,
        "prohibited_key": key,
    }


# ---------------------------------------------------------------------------
# Property test.
#
# Property 35: every prohibited planning-attribute prefix is rejected by
# every Slice 3 write surface with no Slice 3 row persisted and no
# Slice 2 row mutated (Requirements 33.1, 33.2, 33.3, 33.4, 40.3, 40.4,
# 41.5).
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 35: Plan / Execution separation
# Validates: Requirements 33.1, 33.2, 33.3, 33.4, 40.3, 40.4, 41.5
@given(scenario=_prohibited_planning_attribute())
@settings(
    max_examples=100,
    deadline=2000,
    # Each case provisions a fresh on-disk SQLite database, installs
    # three schemas, seeds a complete Slice 2 chain, and builds the
    # full Slice 3 service bundle; per-case setup is slower than a
    # purely in-memory test. The Hypothesis data-generation /
    # slow-test health checks are suppressed so a single slow case
    # does not abort the property run (matching the Property 22
    # convention).
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_prohibited_planning_attribute_rejected_no_slice2_mutation(
    scenario: dict[str, Any],
) -> None:
    """For all Slice 3 write surfaces and all prohibited planning-attribute
    keys, the request is rejected, no Slice 3 row is persisted, and no
    Slice 2 row is mutated.

    The prohibited-attribute screen (step 1 of every ``create_<entity>``
    method on each Slice 3 service) raises the service's
    ``*ValidationError`` with ``failed_constraint='prohibited_attribute'``
    and the offending key on :attr:`prohibited_keys`. The caller's
    transaction rolls back; every Slice 3 Record / Resource / Revision
    table remains empty; every Slice 2 planning row captured by the
    pre-call snapshot is byte-equivalent to the post-call read.
    """
    service_kind: str = scenario["service_kind"]
    prohibited_key: str = scenario["prohibited_key"]
    spec = _SERVICE_DISPATCH[service_kind]

    with tempfile.TemporaryDirectory(prefix="prop35_reject_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            _seed_slice2(engine)
            slice2_before = _snapshot_slice2(engine)
            services = _build_services()

            # The request_attributes mapping the route layer would
            # forward to the service. The ``placeholder`` key (and its
            # value) is an arbitrary non-prohibited entry — the screen
            # iterates keys, not values, so only the ``prohibited_key``
            # entry triggers the rejection.
            request_attributes: dict[str, Any] = {
                "placeholder": "ignored",
                prohibited_key: "prohibited-value",
            }

            with pytest.raises(spec["error_class"]) as exc_info:
                spec["call"](services, engine, request_attributes)

            # The error must carry the structured discriminator and
            # the offending key so Requirements 33.4 holds.
            assert exc_info.value.failed_constraint == (
                "prohibited_attribute"
            ), (
                "Property 35 violated: the service raised "
                f"{type(exc_info.value).__name__} but with "
                f"failed_constraint="
                f"{exc_info.value.failed_constraint!r} (expected "
                "'prohibited_attribute'). The rejected request was: "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}."
            )
            assert prohibited_key in exc_info.value.prohibited_keys, (
                "Property 35 violated: the prohibited key was not "
                "surfaced on the error's prohibited_keys attribute. "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}, "
                f"reported={exc_info.value.prohibited_keys!r}."
            )

            # No row landed in any Slice 3 table — Property 35's "no
            # row persisted" clause (Requirement 41.5).
            for table in _SLICE3_TABLES:
                assert _count(engine, table) == 0, (
                    f"Property 35 violated: rejected request "
                    f"persisted a row in Slice 3 table {table!r}. "
                    f"service_kind={service_kind!r}, "
                    f"prohibited_key={prohibited_key!r}."
                )

            # No row of any Slice 2 planning table mutated — Property
            # 35's "no Slice 2 row mutated" clause (Requirements
            # 33.1, 40.3, 40.4).
            slice2_after = _snapshot_slice2(engine)
            assert slice2_after == slice2_before, (
                "Property 35 violated: a Slice 3 rejected action "
                "mutated a Slice 2 planning row. "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}. "
                f"Before: {slice2_before!r}. After: {slice2_after!r}."
            )
        finally:
            engine.dispose()
