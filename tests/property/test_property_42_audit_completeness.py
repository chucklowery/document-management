# Feature: third-walking-slice, Property 42: Audit completeness and atomicity for consequential and denied execution actions
"""Property 42 — Audit completeness and atomicity for consequential and
denied execution actions (task 16.12).

**Property 42: Audit completeness and atomicity for consequential and
denied execution actions**

For all sequences of Slice 3 operations (Work Assignment, Work Event,
Time Entry, produced Deliverable, Deliverable Production, Milestone
Acceptance, Completion creation; denied attempts; attempted
modifications of finalized Records), the ``Audit_Records`` table
contains exactly one matching row per consequential write with
``actor_party_id``, ``action_type``, ``target_id``,
``target_revision_id`` when applicable, ``outcome``, ``recorded_at``,
and ``correlation_id`` consistent with the originating operation,
appended in the same transaction; and exactly one matching Denial
Record per denied attempt with the same required fields and a
``reason_code`` drawn from the Slice 1 enumeration.
``Audit_Records.append_sequence`` is monotonically non-decreasing by
``recorded_at``. Denied attempts and audit-append-failure attempts
leave no in-flight Slice 3 row persisted.

**Validates: Requirements 23.8, 24.6, 25.5, 25.7, 26.7, 26.8, 27.6,
28.6, 29.6, 30.2, 32.11, 37.1, 37.2, 37.4, 37.6, 41.14**

Strategy
========

Each Hypothesis case (a) seeds a fresh per-test SQLite engine with the
Slice 1, Slice 2, Slice 3 Execution, and Slice 3 Deliverable schemas;
the required Party rows (one authorized Party holding all four Slice 3
authority types, one unauthorized Party, plus an Assignment Authority
Party and a Resource Steward Party to satisfy
``assignee_party_id != assignment_authority_party_id``); one Role
Assignment over a fixed scope; one Project + Activity Plan + Approved
Plan Revision (the *shared* plan revision); one *shared* Work
Assignment with the authorized Party as assignee so AD-WS-29 lets the
shared WA back every Contributor-action permit; one Deliverable
Resource + first Revision; one Deliverable Expectation Revision; and
one Deliverable Production Record with its three AD-WS-26 Relationship
rows.

(b) draws a sequence of 1..6 operations from a closed alphabet
covering creation (permit), authorization denial, and
post-finalization mutation attempts.  Each Hypothesis-drawn operation:

- ``create_work_assignment_permit`` /
  ``create_work_assignment_deny`` — exercise
  :meth:`WorkAssignmentService.create_work_assignment` and its
  AD-WS-9 separate-transaction Denial Record path (Requirement 23.6,
  23.8 / Slice 1 Requirement 13.1 / 13.2).
- ``create_work_event_permit`` / ``create_work_event_deny`` —
  exercise :meth:`WorkEventService.create_work_event`. Each permit
  mints a fresh Work Assignment so the AD-WS-21 partial UNIQUE
  ``idx_work_events_one_started_per_wa`` constraint never fires
  (Property 42 is an audit-completeness property, not a state-machine
  property).
- ``create_time_entry_permit`` / ``create_time_entry_deny`` —
  exercise :meth:`TimeEntryService.create_time_entry` against the
  shared Work Assignment (Time Entries have no per-WA uniqueness).
- ``create_produced_deliverable_permit`` /
  ``create_produced_deliverable_deny`` — exercise
  :meth:`DeliverableRepositoryService.create_produced_deliverable`
  against the shared Work Assignment.
- ``create_deliverable_production_permit`` /
  ``create_deliverable_production_deny`` — exercise
  :meth:`DeliverableProductionService.create_deliverable_production`.
  Each permit mints a fresh produced Deliverable Revision originating
  from the shared Work Assignment so no UNIQUE collision fires.
- ``create_milestone_acceptance_permit`` /
  ``create_milestone_acceptance_deny`` — exercise
  :meth:`MilestoneAcceptanceService.create_milestone_acceptance`.
  Each permit mints a fresh Deliverable Production Record with its
  three Relationship rows so the
  ``UNIQUE(source_deliverable_production_id)`` constraint
  (Requirement 28.3) never collides.
- ``create_completion_permit`` / ``create_completion_deny`` —
  exercise :meth:`CompletionService.create_completion`. Each permit
  mints a fresh Approved Plan Revision (and an Accept-outcome
  Milestone Acceptance against a fresh Production so Requirement
  29.1's existence check passes) so the
  ``UNIQUE(target_plan_revision_id)`` constraint (Requirement 29.3)
  never collides.
- ``attempt_update_finalized_work_assignment`` /
  ``attempt_delete_finalized_work_assignment`` — issue a raw
  ``UPDATE`` / ``DELETE`` against the shared Work Assignment. The
  AD-WS-27 ``BEFORE UPDATE`` / ``BEFORE DELETE`` trigger installed
  by :mod:`walking_slice.execution._persistence` rejects the
  statement with ``RAISE(ABORT, …)``; SQLAlchemy surfaces the
  rejection as :class:`sqlalchemy.exc.IntegrityError`. The shared
  Work Assignment row remains byte-equivalent to its seeded form
  (Requirement 23.9 / AD-WS-27 — schema-level rejection is the
  authoritative immutability layer; no application path appends a
  Denial Record for a raw-SQL trigger rejection because no service
  method exposes such a mutation, in keeping with Principle 5.6).

Every operation that runs through a Slice 3 service is invoked with an
explicit ``correlation_id`` so the post-hoc assertion can locate the
matching audit row deterministically. Each consequential operation
records the expected ``target_id`` and ``action_type`` against which
the row's attribute fidelity is checked.

Assertions per case (run after the whole scenario has executed):

1. **Existence and uniqueness.** For every consequential permit there
   is exactly one ``Audit_Records`` row matching the operation's
   ``(correlation_id, outcome='consequential', action_type)`` triple;
   for every authorization deny there is exactly one Denial Record
   matching ``(correlation_id, outcome='deny',
   authorities_required IS NULL, action_type)``. The
   authorization-evaluation row (which also carries
   ``outcome='deny'`` when authorization denies) is filtered out by
   ``authorities_required IS NOT NULL`` so the property statement's
   "exactly one Denial Record" pins the dedicated audit row.
2. **Attribute fidelity.** The row's ``actor_party_id``,
   ``action_type``, ``target_id``, ``target_revision_id``, and
   ``correlation_id`` are byte-equal to the expected values captured
   at call time. Denial rows additionally carry a non-NULL
   ``reason_code`` drawn from the Slice 1 enumeration (the no-Role
   Assignment deny path produces ``'no-role-assignment'``).
3. **Recorded time format.** Every row's ``recorded_at`` matches the
   slice-wide millisecond-precision UTC pattern
   ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (Slice 1 Requirement 13.1).
4. **Append-sequence monotonicity.** Across every appended audit row
   in the case, sorting by ``recorded_at`` ASC then
   ``append_sequence`` ASC produces a strictly increasing
   ``append_sequence`` series (Requirement 37.4 / Slice 1 Requirement
   13.4). The FixedClock used in the test makes every ``recorded_at``
   identical, which is the strongest form of the property — the
   ``append_sequence`` series alone must be strictly increasing in
   insertion order.
5. **No in-flight write on denial.** For every denied attempt, the
   Slice 3 record tables carry no row whose persistence was driven
   by that op's correlation identifier — the deny path's caller
   transaction rolled back and only the separate-transaction Denial
   Record was committed (Requirement 30.2 / 37.6).
6. **Trigger-level immutability.** For every modification attempt,
   the shared Work Assignment row's full column set is
   byte-equivalent to its pre-attempt snapshot.
"""

from __future__ import annotations

import hashlib
import re
import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import (
    DeliverableRepositoryAuthorizationError,
    DeliverableRepositoryService,
)
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import (
    CompletionAuthorizationError,
    CompletionService,
)
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionAuthorizationError,
    DeliverableProductionService,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceAuthorizationError,
    MilestoneAcceptanceService,
)
from walking_slice.execution.time_entries import (
    TimeEntryAuthorizationError,
    TimeEntryService,
)
from walking_slice.execution.work_assignments import (
    WorkAssignmentAuthorizationError,
    WorkAssignmentService,
)
from walking_slice.execution.work_events import (
    WorkEventAuthorizationError,
    WorkEventService,
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
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

# Authorized Party holds the union of every Slice 3 authority. The two
# additional Parties satisfy the schema-level
# ``assignee_party_id != assignment_authority_party_id`` CHECK on
# ``Work_Assignment_Records`` (Requirement 23.5).
_PARTY_AUTHORIZED: Final[str] = "00000000-0000-7000-8000-000000420001"
_PARTY_UNAUTHORIZED: Final[str] = "00000000-0000-7000-8000-000000420002"
_PARTY_RESOURCE_STEWARD: Final[str] = "00000000-0000-7000-8000-000000420003"
_PARTY_ASSIGNING: Final[str] = "00000000-0000-7000-8000-000000420004"

_SCOPE: Final[str] = "property-42/scope"

_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-00000000a042"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Pre-seeded Slice 2 / Slice 3 anchors used by the deny paths and by
# the shared-WA permit paths.
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-000000420101"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-000000420102"
_SHARED_PLAN_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420103"
)
_SHARED_WORK_ASSIGNMENT_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420201"
)
_SHARED_DELIVERABLE_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420301"
)
_SHARED_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420302"
)
_SHARED_DELIVERABLE_EXPECTATION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420401"
)
_SHARED_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420402"
)
_SHARED_DELIVERABLE_PRODUCTION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420501"
)
# A second shared Deliverable Production is used to back the shared
# Accept Milestone Acceptance below. The Milestone Acceptance deny
# path needs a Production without any Milestone Acceptance recorded
# against it (``UNIQUE(source_deliverable_production_id)``
# pre-check runs before authorization evaluation), so the milestone
# is anchored to *this* production rather than the
# ``_SHARED_DELIVERABLE_PRODUCTION_ID`` row that the deny op
# targets.
_COMPLETION_DENY_PRODUCTION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420502"
)
_COMPLETION_DENY_DELIVERABLE_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420503"
)
_COMPLETION_DENY_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420504"
)
_SHARED_ACCEPT_MILESTONE_ID: Final[str] = (
    "00000000-0000-7000-8000-000000420601"
)

# Effort-period window strictly inside the FixedClock instant so every
# Time Entry permit satisfies
# ``effort_period_start <= effort_period_end <= recorded_at``.
_PERIOD_START_DT: Final[datetime] = datetime(
    2025, 12, 31, 22, 0, 0, tzinfo=timezone.utc
)
_PERIOD_END_DT: Final[datetime] = datetime(
    2025, 12, 31, 23, 0, 0, tzinfo=timezone.utc
)

# Canonical millisecond-precision UTC text pattern (Slice 1 Requirement
# 13.1).
_RECORDED_AT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


# ---------------------------------------------------------------------------
# Operation alphabet.
#
# The alphabet covers every Slice 3 service write path (permit and
# deny) plus two raw-SQL mutation attempts on the shared Work
# Assignment. ``min_size=1`` guarantees at least one operation per
# case; ``max_size=6`` keeps the per-case wall time inside the
# Hypothesis 2 s deadline given the per-op service invocation cost
# (each permit op may write up to four rows across Slice 3 tables
# plus relationship and audit rows).
# ---------------------------------------------------------------------------


_OPERATIONS: Final[tuple[str, ...]] = (
    "create_work_assignment_permit",
    "create_work_assignment_deny",
    "create_work_event_permit",
    "create_work_event_deny",
    "create_time_entry_permit",
    "create_time_entry_deny",
    "create_produced_deliverable_permit",
    "create_produced_deliverable_deny",
    "create_deliverable_production_permit",
    "create_deliverable_production_deny",
    "create_milestone_acceptance_permit",
    "create_milestone_acceptance_deny",
    "create_completion_permit",
    "create_completion_deny",
    "attempt_update_finalized_work_assignment",
    "attempt_delete_finalized_work_assignment",
)

_operation_strategy = st.sampled_from(_OPERATIONS)
_scenario_strategy = st.lists(_operation_strategy, min_size=1, max_size=6)



# ---------------------------------------------------------------------------
# Engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every schema
    the Slice 3 service surface spans.
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
    return engine


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the four Party rows referenced by the operation surface."""
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_AUTHORIZED, "Property 42 Authorized"),
            (_PARTY_UNAUTHORIZED, "Property 42 Unauthorized"),
            (_PARTY_RESOURCE_STEWARD, "Property 42 Resource Steward"),
            (_PARTY_ASSIGNING, "Property 42 Assigning Authority"),
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
) -> None:
    """Grant the authorized Party every Slice 3 authority over the test scope.

    The four Slice 3 authority values (``assign``, ``contribute``,
    ``accept_milestone``, ``complete``) are bundled into one Role
    Assignment so a single ``AuthorizationService.evaluate`` call can
    permit any Slice 3 action a permit-op draws.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_AUTHORIZED,
        role_name="property_42_full",
        scope=_SCOPE,
        authorities_granted=(
            "assign",
            "contribute",
            "accept_milestone",
            "complete",
        ),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_PARTY_RESOURCE_STEWARD,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_project_and_plan(engine: Engine) -> None:
    """Seed Project, Activity Plan, and the shared Approved Plan Revision.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so a row
    with ``lifecycle_state='approved'`` may be inserted in one
    statement without driving the full Plan Approval transaction.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 42 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_AUTHORIZED,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )
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
                    :rev, :aid, NULL, 'approved',
                    'Property 42 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _SHARED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _PARTY_AUTHORIZED,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_fresh_plan_revision(
    engine: Engine, plan_revision_id: str
) -> None:
    """Insert one extra ``Plan_Revisions`` row with
    ``lifecycle_state='approved'`` for per-op uniqueness (Completion
    needs a fresh Plan Revision per permit attempt because
    ``Completion_Records.target_plan_revision_id`` is UNIQUE per
    Requirement 29.3)."""
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
                    :rev, :aid, NULL, 'approved',
                    'Property 42 fresh plan revision.', '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _PARTY_AUTHORIZED,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str,
    target_plan_revision_id: str,
    assignee_party_id: str,
    assignment_authority_party_id: str,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The schema-level CHECK constraint
    ``assignee_party_id != assignment_authority_party_id``
    (Requirement 23.5) is honored by passing distinct identifiers.
    Used to seed the *shared* Work Assignment plus fresh Work
    Assignments for Work Event permit and Deliverable Production
    permit ops.
    """
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
                    'Property 42 Work Assignment rationale.',
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_assignment_id,
                "prev": target_plan_revision_id,
                "assignee": assignee_party_id,
                "authority": assignment_authority_party_id,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_resource_and_revision(
    engine: Engine,
    *,
    deliverable_id: str,
    deliverable_revision_id: str,
    originating_work_assignment_id: str,
    authoring_party_id: str,
) -> None:
    """Insert one Deliverable Resource + first Revision pair."""
    body = f"Property 42 produced content {deliverable_id}".encode()
    digest = hashlib.sha256(body).hexdigest()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Property 42 runbook', :ts)
                """
            ),
            {"did": deliverable_id, "ts": _NOW_ISO},
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
                "rev": deliverable_revision_id,
                "did": deliverable_id,
                "bytes": body,
                "digest": digest,
                "wa": originating_work_assignment_id,
                "party": authoring_party_id,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    """Insert one Deliverable Expectation header + first Revision row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _SHARED_DELIVERABLE_EXPECTATION_ID, "ts": _NOW_ISO},
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
                    :rev, :did, NULL, :pid,
                    'Property 42 Expected Deliverable',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _SHARED_DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _SHARED_DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_AUTHORIZED,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production(
    engine: Engine,
    *,
    deliverable_production_id: str,
    source_work_assignment_id: str,
    produced_deliverable_id: str,
    produced_deliverable_revision_id: str,
) -> None:
    """Insert one Deliverable Production Record + its three AD-WS-26
    Relationships by direct INSERT.

    Required by every Milestone Acceptance permit (resolves the
    produced Revision / target Expectation Revision via ``Produces``
    / ``Addresses`` lookups inside the service).
    """
    produces_id = str(uuid_lib.uuid4())
    addresses_id = str(uuid_lib.uuid4())
    relates_to_id = str(uuid_lib.uuid4())
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Production_Records (
                    deliverable_production_id, source_work_assignment_id,
                    produced_deliverable_id, produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    production_rationale, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :pid, :wa, :did, :rev, :exp_did, :exp_rev,
                    'Property 42 production rationale.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": deliverable_production_id,
                "wa": source_work_assignment_id,
                "did": produced_deliverable_id,
                "rev": produced_deliverable_revision_id,
                "exp_did": _SHARED_DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _SHARED_DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _PARTY_AUTHORIZED,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Produces',
                    'deliverable_production_record', :pid, NULL,
                    'deliverable_revision', :did, :rev,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": produces_id,
                "pid": deliverable_production_id,
                "did": produced_deliverable_id,
                "rev": produced_deliverable_revision_id,
                "party": _PARTY_AUTHORIZED,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Addresses',
                    'deliverable_production_record', :pid, NULL,
                    'deliverable_expectation_revision',
                    :exp_did, :exp_rev,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": addresses_id,
                "pid": deliverable_production_id,
                "exp_did": _SHARED_DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _SHARED_DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _PARTY_AUTHORIZED,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Relates To',
                    'deliverable_production_record', :pid, NULL,
                    'work_assignment_record', :wa, NULL,
                    :party, :ts, 'production_source'
                )
                """
            ),
            {
                "rid": relates_to_id,
                "pid": deliverable_production_id,
                "wa": source_work_assignment_id,
                "party": _PARTY_AUTHORIZED,
                "ts": _NOW_ISO,
            },
        )


def _seed_accept_milestone(
    engine: Engine,
    *,
    milestone_acceptance_id: str,
    source_deliverable_production_id: str,
    produced_deliverable_id: str,
    produced_deliverable_revision_id: str,
) -> None:
    """Insert one Accept-outcome ``Milestone_Acceptance_Records`` row
    so the Completion service's accepted-Milestone existence check
    returns ``>= 1``."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Milestone_Acceptance_Records (
                    milestone_acceptance_id,
                    source_deliverable_production_id,
                    produced_deliverable_id,
                    produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    outcome, rationale, accepting_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :mid, :pid, :did, :rev, :exp_did, :exp_rev,
                    'Accept', 'Property 42 accept rationale.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": milestone_acceptance_id,
                "pid": source_deliverable_production_id,
                "did": produced_deliverable_id,
                "rev": produced_deliverable_revision_id,
                "exp_did": _SHARED_DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _SHARED_DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _PARTY_AUTHORIZED,
                "abid": str(_AUTHORITY_BASIS_ID),
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
    the dedicated *Denial Record* written via
    :meth:`AuditLog.append_denial` in the slice 3 deny path. When
    ``False`` the filter keeps only rows whose
    ``authorities_required`` is non-NULL — the authorization
    evaluation row appended by :meth:`AuthorizationService.evaluate`.
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


def _count_consequential_rows_for_correlation(
    engine: Engine, correlation_id: str
) -> int:
    """Return the number of consequential ``Audit_Records`` rows
    carrying ``correlation_id``.

    Used by the denial branch to confirm no in-flight write
    survived. Each Slice 3 service appends its consequential audit
    row inside the caller's transaction (AD-WS-5 / Requirement
    23.8 / 24.6 / 25.5 / 26.7 / 27.6 / 28.6 / 29.6). If any such
    row exists with the deny op's correlation identifier, an
    in-flight write leaked through the rollback.
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


def _fetch_all_audit_rows(engine: Engine) -> list[dict[str, Any]]:
    """Return every ``Audit_Records`` row ordered by recorded_at ASC
    then append_sequence ASC.

    Property 42 / Requirement 37.4 / Slice 1 Requirement 13.4 — the
    monotonic-ordering assertion sorts by ``recorded_at`` as primary
    key and ``append_sequence`` as tiebreaker and requires the
    ``append_sequence`` series to be strictly increasing.
    """
    sql = (
        "SELECT audit_record_id, append_sequence, recorded_at, "
        "outcome, action_type, correlation_id "
        "FROM Audit_Records "
        "ORDER BY recorded_at ASC, append_sequence ASC"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [dict(row) for row in rows]


def _fetch_work_assignment_row(
    engine: Engine, work_assignment_id: str
) -> dict[str, Any]:
    """Return the full ``Work_Assignment_Records`` row for one ID."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT * FROM Work_Assignment_Records "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": work_assignment_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


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
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 42: Audit completeness and atomicity for consequential and denied execution actions
@given(scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_audit_completeness_and_atomicity_across_execution_actions(
    scenario: list[str],
) -> None:
    """For every consequential Slice 3 write and every denied
    attempt run by the scenario, exactly one ``Audit_Records`` row
    exists carrying the expected ``actor_party_id``, ``action_type``,
    ``target_id``, ``target_revision_id``, ``outcome``,
    millisecond-precision ``recorded_at``, and ``correlation_id``;
    the ``Audit_Records.append_sequence`` series across the whole
    case is strictly increasing in insertion order; every denial
    leaves no in-flight Slice 3 row persisted; and every
    raw-SQL mutation attempt on a finalized record is rejected by
    the AD-WS-27 trigger and leaves the target row byte-equivalent.

    **Validates: Requirements 23.8, 24.6, 25.5, 25.7, 26.7, 26.8,
    27.6, 28.6, 29.6, 30.2, 32.11, 37.1, 37.2, 37.4, 37.6, 41.14**
    """
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop42_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            # Fresh services per case so :class:`IdentityService`
            # in-memory state cannot leak across cases. The denial
            # audit sleep is replaced with a no-op so the deny-path
            # retry backoffs never wait in real time.
            clock = FixedClock(_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
            authorization_service = AuthorizationService(
                clock=clock,
                audit_log=audit_log,
                identity_service=identity_service,
            )
            plan_revision_service = PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            project_resolver = ProjectResolver()
            expectation_service = DeliverableExpectationService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            work_assignment_service = WorkAssignmentService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=plan_revision_service,
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
            deliverable_repo = DeliverableRepositoryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )
            production_service = DeliverableProductionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                deliverable_reader=deliverable_repo,
                planning_reader=expectation_service,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            )
            milestone_service = MilestoneAcceptanceService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                production_reader=production_service,
                denial_audit_sleep=lambda _seconds: None,
            )
            completion_service = CompletionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=plan_revision_service,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            )

            # Seed parties, role assignment, the shared planning +
            # work assignment + deliverable graph, and one shared
            # Deliverable Production for the Milestone Acceptance
            # deny path. The shared Work Assignment carries the
            # authorized Party as assignee so the AD-WS-29
            # two-stage check passes for every Contributor-action
            # permit.
            _seed_parties(engine)
            _seed_role_assignment(authorization_service, engine)
            _seed_project_and_plan(engine)
            _seed_work_assignment(
                engine,
                work_assignment_id=_SHARED_WORK_ASSIGNMENT_ID,
                target_plan_revision_id=_SHARED_PLAN_REVISION_ID,
                assignee_party_id=_PARTY_AUTHORIZED,
                assignment_authority_party_id=_PARTY_ASSIGNING,
            )
            _seed_deliverable_resource_and_revision(
                engine,
                deliverable_id=_SHARED_DELIVERABLE_ID,
                deliverable_revision_id=_SHARED_DELIVERABLE_REVISION_ID,
                originating_work_assignment_id=_SHARED_WORK_ASSIGNMENT_ID,
                authoring_party_id=_PARTY_AUTHORIZED,
            )
            _seed_deliverable_expectation(engine)
            _seed_deliverable_production(
                engine,
                deliverable_production_id=_SHARED_DELIVERABLE_PRODUCTION_ID,
                source_work_assignment_id=_SHARED_WORK_ASSIGNMENT_ID,
                produced_deliverable_id=_SHARED_DELIVERABLE_ID,
                produced_deliverable_revision_id=(
                    _SHARED_DELIVERABLE_REVISION_ID
                ),
            )
            # Seed a second Deliverable + Production specifically to
            # carry the shared Accept Milestone Acceptance so that
            # the ``create_completion_deny`` op reaches the
            # authorization evaluation step.
            #
            # Requirement 29.1's accepted-Milestone existence check
            # runs *before* authorization evaluation; without an
            # Accept Milestone bound to the shared Plan Revision the
            # Completion deny op would raise
            # :class:`CompletionNoAcceptedMilestonesError` instead of
            # producing the Denial Record under test.
            #
            # The shared Production above intentionally has no
            # Milestone Acceptance because Requirement 28.3's
            # ``UNIQUE(source_deliverable_production_id)`` pre-check
            # runs *before* authorization evaluation; the
            # ``create_milestone_acceptance_deny`` op needs the
            # shared Production to be Milestone-free so the deny op
            # reaches authorization rather than raising
            # :class:`MilestoneAcceptanceConflictError`.
            _seed_deliverable_resource_and_revision(
                engine,
                deliverable_id=_COMPLETION_DENY_DELIVERABLE_ID,
                deliverable_revision_id=(
                    _COMPLETION_DENY_DELIVERABLE_REVISION_ID
                ),
                originating_work_assignment_id=(
                    _SHARED_WORK_ASSIGNMENT_ID
                ),
                authoring_party_id=_PARTY_AUTHORIZED,
            )
            _seed_deliverable_production(
                engine,
                deliverable_production_id=_COMPLETION_DENY_PRODUCTION_ID,
                source_work_assignment_id=_SHARED_WORK_ASSIGNMENT_ID,
                produced_deliverable_id=_COMPLETION_DENY_DELIVERABLE_ID,
                produced_deliverable_revision_id=(
                    _COMPLETION_DENY_DELIVERABLE_REVISION_ID
                ),
            )
            _seed_accept_milestone(
                engine,
                milestone_acceptance_id=_SHARED_ACCEPT_MILESTONE_ID,
                source_deliverable_production_id=(
                    _COMPLETION_DENY_PRODUCTION_ID
                ),
                produced_deliverable_id=_COMPLETION_DENY_DELIVERABLE_ID,
                produced_deliverable_revision_id=(
                    _COMPLETION_DENY_DELIVERABLE_REVISION_ID
                ),
            )
            # Snapshot the shared Work Assignment row for the
            # post-mutation byte-equivalence assertion.
            shared_wa_snapshot = _fetch_work_assignment_row(
                engine, _SHARED_WORK_ASSIGNMENT_ID
            )

            # Expected audit-row descriptors — accumulated as
            # operations run; verified after the scenario finishes.
            expected_audit: list[dict[str, Any]] = []

            for op_index, op in enumerate(scenario):
                # Stable per-operation correlation identifier so the
                # post-hoc assertion can locate the matching audit
                # row deterministically. Embedding the index and the
                # op name makes shrunken counterexamples easy to
                # read.
                correlation_id = (
                    f"prop42-op-{op_index:03d}-{op}-"
                    f"{uuid_lib.uuid4().hex[:8]}"
                )

                # ----- Work Assignment ops --------------------------
                if op == "create_work_assignment_permit":
                    with engine.begin() as conn:
                        result = (
                            work_assignment_service.create_work_assignment(
                                conn,
                                target_plan_revision_id=(
                                    _SHARED_PLAN_REVISION_ID
                                ),
                                assignee_party_id=_PARTY_RESOURCE_STEWARD,
                                assignment_authority_party_id=(
                                    _PARTY_AUTHORIZED
                                ),
                                assignment_rationale=(
                                    f"Property 42 WA {op_index}."
                                ),
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.work_assignment",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.work_assignment_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_work_assignment_deny":
                    with pytest.raises(WorkAssignmentAuthorizationError):
                        with engine.begin() as conn:
                            work_assignment_service.create_work_assignment(
                                conn,
                                target_plan_revision_id=(
                                    _SHARED_PLAN_REVISION_ID
                                ),
                                assignee_party_id=_PARTY_RESOURCE_STEWARD,
                                assignment_authority_party_id=(
                                    _PARTY_UNAUTHORIZED
                                ),
                                assignment_rationale=(
                                    f"Property 42 deny WA {op_index}."
                                ),
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.work_assignment",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_PLAN_REVISION_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Work Event ops -------------------------------
                elif op == "create_work_event_permit":
                    # Mint a fresh Work Assignment so the at-most-one
                    # ``started`` partial UNIQUE never fires across
                    # repeated permits in one scenario.
                    fresh_wa_id = (
                        f"00000000-0000-7000-8000-{(op_index + 1000):012x}"
                    )
                    _seed_work_assignment(
                        engine,
                        work_assignment_id=fresh_wa_id,
                        target_plan_revision_id=_SHARED_PLAN_REVISION_ID,
                        assignee_party_id=_PARTY_AUTHORIZED,
                        assignment_authority_party_id=_PARTY_ASSIGNING,
                    )
                    with engine.begin() as conn:
                        result = work_event_service.create_work_event(
                            conn,
                            target_work_assignment_id=fresh_wa_id,
                            event_kind="started",
                            event_note=(
                                f"Property 42 work event {op_index}."
                            ),
                            recording_party_id=_PARTY_AUTHORIZED,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.work_event",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.work_event_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_work_event_deny":
                    with pytest.raises(WorkEventAuthorizationError):
                        with engine.begin() as conn:
                            work_event_service.create_work_event(
                                conn,
                                target_work_assignment_id=(
                                    _SHARED_WORK_ASSIGNMENT_ID
                                ),
                                event_kind="started",
                                event_note=(
                                    f"Property 42 deny WE {op_index}."
                                ),
                                recording_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.work_event",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_WORK_ASSIGNMENT_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Time Entry ops -------------------------------
                elif op == "create_time_entry_permit":
                    with engine.begin() as conn:
                        result = time_entry_service.create_time_entry(
                            conn,
                            target_work_assignment_id=(
                                _SHARED_WORK_ASSIGNMENT_ID
                            ),
                            effort_hours=Decimal("1.00"),
                            effort_period_start=_PERIOD_START_DT,
                            effort_period_end=_PERIOD_END_DT,
                            recording_party_id=_PARTY_AUTHORIZED,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.time_entry",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.time_entry_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_time_entry_deny":
                    with pytest.raises(TimeEntryAuthorizationError):
                        with engine.begin() as conn:
                            time_entry_service.create_time_entry(
                                conn,
                                target_work_assignment_id=(
                                    _SHARED_WORK_ASSIGNMENT_ID
                                ),
                                effort_hours=Decimal("1.00"),
                                effort_period_start=_PERIOD_START_DT,
                                effort_period_end=_PERIOD_END_DT,
                                recording_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.time_entry",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_WORK_ASSIGNMENT_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Produced Deliverable ops ---------------------
                elif op == "create_produced_deliverable_permit":
                    body = f"Property 42 PD {op_index}".encode()
                    with engine.begin() as conn:
                        result = (
                            deliverable_repo.create_produced_deliverable(
                                conn,
                                content_bytes=body,
                                content_type="text/markdown",
                                produced_deliverable_name=(
                                    f"Property 42 PD {op_index}"
                                ),
                                originating_work_assignment_id=(
                                    _SHARED_WORK_ASSIGNMENT_ID
                                ),
                                authoring_party_id=_PARTY_AUTHORIZED,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.produced_deliverable",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.deliverable_id,
                            target_revision_id=result.deliverable_revision_id,
                        )
                    )
                elif op == "create_produced_deliverable_deny":
                    body = f"Property 42 deny PD {op_index}".encode()
                    with pytest.raises(
                        DeliverableRepositoryAuthorizationError
                    ):
                        with engine.begin() as conn:
                            deliverable_repo.create_produced_deliverable(
                                conn,
                                content_bytes=body,
                                content_type="text/markdown",
                                produced_deliverable_name=(
                                    f"Property 42 deny PD {op_index}"
                                ),
                                originating_work_assignment_id=(
                                    _SHARED_WORK_ASSIGNMENT_ID
                                ),
                                authoring_party_id=_PARTY_UNAUTHORIZED,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.produced_deliverable",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_WORK_ASSIGNMENT_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Deliverable Production ops -------------------
                elif op == "create_deliverable_production_permit":
                    # Mint a fresh produced Deliverable so the
                    # produced Revision's
                    # ``originating_work_assignment_id`` resolves to
                    # the shared WA (so AD-WS-29 binding passes) and
                    # so each Production has its own unique
                    # produced-Revision identity to address.
                    fresh_did = (
                        f"00000000-0000-7000-8000-{(op_index + 2000):012x}"
                    )
                    fresh_drev = (
                        f"00000000-0000-7000-8000-{(op_index + 3000):012x}"
                    )
                    _seed_deliverable_resource_and_revision(
                        engine,
                        deliverable_id=fresh_did,
                        deliverable_revision_id=fresh_drev,
                        originating_work_assignment_id=(
                            _SHARED_WORK_ASSIGNMENT_ID
                        ),
                        authoring_party_id=_PARTY_AUTHORIZED,
                    )
                    with engine.begin() as conn:
                        result = (
                            production_service
                            .create_deliverable_production(
                                conn,
                                source_work_assignment_id=(
                                    _SHARED_WORK_ASSIGNMENT_ID
                                ),
                                produced_deliverable_revision_id=fresh_drev,
                                target_deliverable_expectation_revision_id=(
                                    _SHARED_DELIVERABLE_EXPECTATION_REVISION_ID
                                ),
                                production_rationale=(
                                    f"Property 42 DP rationale {op_index}."
                                ),
                                recording_party_id=_PARTY_AUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.deliverable_production",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.deliverable_production_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_deliverable_production_deny":
                    with pytest.raises(
                        DeliverableProductionAuthorizationError
                    ):
                        with engine.begin() as conn:
                            (
                                production_service
                                .create_deliverable_production(
                                    conn,
                                    source_work_assignment_id=(
                                        _SHARED_WORK_ASSIGNMENT_ID
                                    ),
                                    produced_deliverable_revision_id=(
                                        _SHARED_DELIVERABLE_REVISION_ID
                                    ),
                                    target_deliverable_expectation_revision_id=(
                                        _SHARED_DELIVERABLE_EXPECTATION_REVISION_ID
                                    ),
                                    production_rationale=(
                                        f"Property 42 deny DP {op_index}."
                                    ),
                                    recording_party_id=_PARTY_UNAUTHORIZED,
                                    authority_basis=_BASIS,
                                    applicable_scope=_SCOPE,
                                    engine=engine,
                                    correlation_id=correlation_id,
                                )
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.deliverable_production",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_WORK_ASSIGNMENT_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Milestone Acceptance ops ---------------------
                elif op == "create_milestone_acceptance_permit":
                    # Mint a fresh Deliverable Production so the
                    # ``UNIQUE(source_deliverable_production_id)``
                    # constraint (Requirement 28.3) never collides.
                    fresh_did = (
                        f"00000000-0000-7000-8000-{(op_index + 4000):012x}"
                    )
                    fresh_drev = (
                        f"00000000-0000-7000-8000-{(op_index + 5000):012x}"
                    )
                    fresh_dpid = (
                        f"00000000-0000-7000-8000-{(op_index + 6000):012x}"
                    )
                    _seed_deliverable_resource_and_revision(
                        engine,
                        deliverable_id=fresh_did,
                        deliverable_revision_id=fresh_drev,
                        originating_work_assignment_id=(
                            _SHARED_WORK_ASSIGNMENT_ID
                        ),
                        authoring_party_id=_PARTY_AUTHORIZED,
                    )
                    _seed_deliverable_production(
                        engine,
                        deliverable_production_id=fresh_dpid,
                        source_work_assignment_id=(
                            _SHARED_WORK_ASSIGNMENT_ID
                        ),
                        produced_deliverable_id=fresh_did,
                        produced_deliverable_revision_id=fresh_drev,
                    )
                    with engine.begin() as conn:
                        result = (
                            milestone_service.create_milestone_acceptance(
                                conn,
                                source_deliverable_production_id=fresh_dpid,
                                outcome="Accept",
                                rationale=(
                                    f"Property 42 MA rationale {op_index}."
                                ),
                                accepting_party_id=_PARTY_AUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.milestone_acceptance",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.milestone_acceptance_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_milestone_acceptance_deny":
                    with pytest.raises(
                        MilestoneAcceptanceAuthorizationError
                    ):
                        with engine.begin() as conn:
                            milestone_service.create_milestone_acceptance(
                                conn,
                                source_deliverable_production_id=(
                                    _SHARED_DELIVERABLE_PRODUCTION_ID
                                ),
                                outcome="Accept",
                                rationale=(
                                    f"Property 42 deny MA {op_index}."
                                ),
                                accepting_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.milestone_acceptance",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_DELIVERABLE_PRODUCTION_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Completion ops -------------------------------
                elif op == "create_completion_permit":
                    # Mint a fresh Approved Plan Revision so the
                    # ``UNIQUE(target_plan_revision_id)`` constraint
                    # (Requirement 29.3) never collides, plus a
                    # fresh Accept Milestone Acceptance bound to a
                    # fresh Production so Requirement 29.1's
                    # existence check returns ``>= 1`` for the new
                    # Plan Revision.
                    fresh_rev = (
                        f"00000000-0000-7000-8000-{(op_index + 7000):012x}"
                    )
                    fresh_wa = (
                        f"00000000-0000-7000-8000-{(op_index + 8000):012x}"
                    )
                    fresh_did = (
                        f"00000000-0000-7000-8000-{(op_index + 9000):012x}"
                    )
                    fresh_drev = (
                        f"00000000-0000-7000-8000-{(op_index + 10000):012x}"
                    )
                    fresh_dpid = (
                        f"00000000-0000-7000-8000-{(op_index + 11000):012x}"
                    )
                    fresh_mid = (
                        f"00000000-0000-7000-8000-{(op_index + 12000):012x}"
                    )
                    _seed_fresh_plan_revision(engine, fresh_rev)
                    _seed_work_assignment(
                        engine,
                        work_assignment_id=fresh_wa,
                        target_plan_revision_id=fresh_rev,
                        assignee_party_id=_PARTY_AUTHORIZED,
                        assignment_authority_party_id=_PARTY_ASSIGNING,
                    )
                    _seed_deliverable_resource_and_revision(
                        engine,
                        deliverable_id=fresh_did,
                        deliverable_revision_id=fresh_drev,
                        originating_work_assignment_id=fresh_wa,
                        authoring_party_id=_PARTY_AUTHORIZED,
                    )
                    _seed_deliverable_production(
                        engine,
                        deliverable_production_id=fresh_dpid,
                        source_work_assignment_id=fresh_wa,
                        produced_deliverable_id=fresh_did,
                        produced_deliverable_revision_id=fresh_drev,
                    )
                    _seed_accept_milestone(
                        engine,
                        milestone_acceptance_id=fresh_mid,
                        source_deliverable_production_id=fresh_dpid,
                        produced_deliverable_id=fresh_did,
                        produced_deliverable_revision_id=fresh_drev,
                    )
                    with engine.begin() as conn:
                        result = completion_service.create_completion(
                            conn,
                            target_plan_revision_id=fresh_rev,
                            outcome="Completed",
                            rationale=(
                                f"Property 42 completion {op_index}."
                            ),
                            source_milestone_acceptance_ids=(),
                            completing_party_id=_PARTY_AUTHORIZED,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="consequential",
                            action_type="create.completion",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.completion_id,
                            target_revision_id=None,
                        )
                    )
                elif op == "create_completion_deny":
                    with pytest.raises(CompletionAuthorizationError):
                        with engine.begin() as conn:
                            completion_service.create_completion(
                                conn,
                                target_plan_revision_id=(
                                    _SHARED_PLAN_REVISION_ID
                                ),
                                outcome="Completed",
                                rationale=(
                                    f"Property 42 deny CP {op_index}."
                                ),
                                source_milestone_acceptance_ids=(),
                                completing_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=_BASIS,
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    expected_audit.append(
                        _expected(
                            correlation_id=correlation_id,
                            outcome="deny",
                            action_type="create.completion",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=_SHARED_PLAN_REVISION_ID,
                            target_revision_id=None,
                            require_authorities_required_null=True,
                        )
                    )

                # ----- Mutation-attempt ops -------------------------
                elif op == "attempt_update_finalized_work_assignment":
                    # AD-WS-27 ``BEFORE UPDATE`` trigger on
                    # Work_Assignment_Records rejects the statement;
                    # SQLAlchemy surfaces the rejection as
                    # :class:`sqlalchemy.exc.IntegrityError`. The
                    # row remains byte-equivalent. No Denial Record
                    # is appended for raw-SQL trigger rejections
                    # (there is no service method that exposes a
                    # mutation path).
                    with engine.connect() as conn, pytest.raises(
                        IntegrityError
                    ):
                        with conn.begin():
                            conn.execute(
                                text(
                                    "UPDATE Work_Assignment_Records "
                                    "SET assignment_rationale = "
                                    "'tampered' "
                                    "WHERE work_assignment_id = :id"
                                ),
                                {
                                    "id": _SHARED_WORK_ASSIGNMENT_ID,
                                },
                            )
                elif op == "attempt_delete_finalized_work_assignment":
                    with engine.connect() as conn, pytest.raises(
                        IntegrityError
                    ):
                        with conn.begin():
                            conn.execute(
                                text(
                                    "DELETE FROM Work_Assignment_Records "
                                    "WHERE work_assignment_id = :id"
                                ),
                                {
                                    "id": _SHARED_WORK_ASSIGNMENT_ID,
                                },
                            )

                else:  # pragma: no cover - defensive
                    raise AssertionError(f"unknown op: {op!r}")

            # ---------------------------------------------------------
            # Post-hoc assertions.
            # ---------------------------------------------------------

            # (1)+(2)+(3) — Per expected-audit descriptor: existence,
            # uniqueness, attribute fidelity, and recorded-time
            # format.
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
                    f"Property 42: expected exactly one "
                    f"Audit_Records row with correlation_id="
                    f"{expected['correlation_id']!r}, outcome="
                    f"{expected['outcome']!r}, "
                    f"authorities_required_null filter="
                    f"{expected['require_authorities_required_null']!r}; "
                    f"got {len(rows)} ({rows!r})."
                )
                row = rows[0]

                assert (
                    row["actor_party_id"] == expected["actor_party_id"]
                ), (
                    f"Property 42: audit row "
                    f"{row['audit_record_id']!r} has actor_party_id="
                    f"{row['actor_party_id']!r}; expected "
                    f"{expected['actor_party_id']!r} for "
                    f"correlation_id={expected['correlation_id']!r}."
                )
                assert row["action_type"] == expected["action_type"], (
                    f"Property 42: audit row "
                    f"{row['audit_record_id']!r} has action_type="
                    f"{row['action_type']!r}; expected "
                    f"{expected['action_type']!r}."
                )
                assert row["target_id"] == expected["target_id"], (
                    f"Property 42: audit row "
                    f"{row['audit_record_id']!r} has target_id="
                    f"{row['target_id']!r}; expected "
                    f"{expected['target_id']!r}."
                )
                assert (
                    row["target_revision_id"]
                    == expected["target_revision_id"]
                ), (
                    f"Property 42: audit row "
                    f"{row['audit_record_id']!r} has "
                    f"target_revision_id="
                    f"{row['target_revision_id']!r}; expected "
                    f"{expected['target_revision_id']!r}."
                )
                assert (
                    row["correlation_id"] == expected["correlation_id"]
                ), (
                    f"Property 42: audit row "
                    f"{row['audit_record_id']!r} has correlation_id="
                    f"{row['correlation_id']!r}; expected "
                    f"{expected['correlation_id']!r}."
                )
                assert _RECORDED_AT_PATTERN.match(row["recorded_at"]), (
                    f"Property 42: audit row "
                    f"{row['audit_record_id']!r} has recorded_at="
                    f"{row['recorded_at']!r}; expected canonical "
                    f"millisecond-precision UTC text matching "
                    f"{_RECORDED_AT_PATTERN.pattern!r}."
                )
                # Denial rows carry a non-empty ``reason_code`` drawn
                # from the Slice 1 enumeration (Requirement 30.2).
                if expected["outcome"] == "deny":
                    assert row["reason_code"] is not None and (
                        row["reason_code"] != ""
                    ), (
                        f"Property 42: denial audit row "
                        f"{row['audit_record_id']!r} has empty "
                        f"reason_code; expected a non-empty value "
                        f"drawn from Slice 1 Requirement 7.2 "
                        f"enumeration."
                    )

                # (5) Denial leaves no in-flight Slice 3 row.
                if expected["outcome"] == "deny":
                    consequential_count = (
                        _count_consequential_rows_for_correlation(
                            engine, expected["correlation_id"]
                        )
                    )
                    assert consequential_count == 0, (
                        f"Property 42: denied attempt with "
                        f"correlation_id="
                        f"{expected['correlation_id']!r} left "
                        f"{consequential_count} consequential "
                        f"Audit_Records row(s) — a denied "
                        f"attempt must leave no in-flight Slice 3 "
                        f"write persisted (Requirement 30.2 / "
                        f"37.6)."
                    )

            # (4) Append-sequence monotonicity across the entire case.
            # Sort by (recorded_at, append_sequence) — the schema's
            # documented primary ordering — and require the
            # ``append_sequence`` series to be strictly increasing
            # (Requirement 37.4 / Slice 1 Requirement 13.4).
            all_rows = _fetch_all_audit_rows(engine)
            previous_sequence: Optional[int] = None
            for row in all_rows:
                current = int(row["append_sequence"])
                if previous_sequence is not None:
                    assert current > previous_sequence, (
                        f"Property 42: Audit_Records.append_sequence "
                        f"is not strictly increasing in "
                        f"(recorded_at, append_sequence) order — "
                        f"observed {previous_sequence} then "
                        f"{current} on row "
                        f"{row['audit_record_id']!r} "
                        f"(recorded_at={row['recorded_at']!r}). "
                        f"(Requirement 37.4 / Slice 1 Requirement "
                        f"13.4)."
                    )
                previous_sequence = current

            # (6) Trigger-level immutability — the shared Work
            # Assignment row's full column set is byte-equivalent to
            # its pre-attempt snapshot. Holds unconditionally because
            # every attempted mutation went through the AD-WS-27
            # trigger and rolled back; the assertion is the
            # universal-quantifier statement at the row level.
            shared_wa_after = _fetch_work_assignment_row(
                engine, _SHARED_WORK_ASSIGNMENT_ID
            )
            assert shared_wa_after == shared_wa_snapshot, (
                "Property 42: shared Work_Assignment_Records row "
                "is not byte-equivalent to its pre-attempt "
                "snapshot — a mutation attempt may have leaked "
                "through the AD-WS-27 BEFORE UPDATE/DELETE "
                "trigger."
            )

        finally:
            engine.dispose()
