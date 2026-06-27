# Feature: third-walking-slice, Property 31: Execution-creation success
"""Property 31 — Execution-creation success (task 16.1).

**Property 31: Execution-creation success**

For any authorized execution creation request (Work Assignment,
Work Event, Time Entry, produced Deliverable Revision, Deliverable
Production, Milestone Acceptance, or Completion) that passes input
validation and authority + assignee-binding checks, exactly one
Record row (and for produced Deliverables, one first Revision row),
exactly one consequential ``Audit_Records`` row, and the prescribed
Relationship rows per AD-WS-26 are persisted in one transaction with
byte-equivalent recorded times.

**Validates: Requirements 23.1, 23.3, 23.8, 24.1, 24.6, 25.1, 25.5,
26.1, 26.7, 27.1, 27.6, 28.1, 28.6, 29.1, 29.6, 37.1, 41.1**

Strategy
========

Seven independent property tests, one per Execution_Service /
Deliverable_Repository request body, each driven by a Hypothesis
strategy that generates a valid request payload for that endpoint:

- :func:`test_work_assignment_creation_persists_one_record_two_relationships_one_audit`
  exercises :meth:`WorkAssignmentService.create_work_assignment`
  against a seeded ``approved`` Plan Revision plus assignee Party.
  AD-WS-26 prescribes two Relationship rows: ``Addresses`` to the
  Plan Revision (``semantic_role IS NULL``) and ``Relates To`` to
  the assignee Party (``semantic_role='assignee'``).
- :func:`test_work_event_creation_persists_one_record_one_relationship_one_audit`
  exercises :meth:`WorkEventService.create_work_event` against a
  seeded Work Assignment whose assignee matches the recording
  Party. AD-WS-26 prescribes one ``Relates To`` Relationship
  (``semantic_role='work_event'``).
- :func:`test_time_entry_creation_persists_one_record_one_relationship_one_audit`
  exercises :meth:`TimeEntryService.create_time_entry` against the
  same Work Assignment fixture. AD-WS-26 prescribes one
  ``Relates To`` Relationship (``semantic_role='time_entry'``).
- :func:`test_produced_deliverable_creation_persists_one_resource_one_revision_one_audit`
  exercises
  :meth:`DeliverableRepositoryService.create_produced_deliverable`.
  Produced Deliverables are the special "one Resource + one first
  Revision" case the property names explicitly; AD-WS-26 does not
  attach any Relationship row to ``create.produced_deliverable``
  (the ``Produces`` Relationship that names the Revision is
  written later by ``create.deliverable_production``).
- :func:`test_deliverable_production_creation_persists_one_record_three_relationships_one_audit`
  exercises
  :meth:`DeliverableProductionService.create_deliverable_production`
  against a seeded source Work Assignment, produced Deliverable
  Revision, and Deliverable Expectation Revision. AD-WS-26
  prescribes three Relationship rows: ``Produces`` to the produced
  Revision (NULL), ``Addresses`` to the Expectation Revision
  (NULL), and ``Relates To`` to the source Work Assignment
  (``semantic_role='production_source'``).
- :func:`test_milestone_acceptance_creation_persists_one_record_one_relationship_one_audit`
  exercises
  :meth:`MilestoneAcceptanceService.create_milestone_acceptance`
  against a seeded Deliverable Production Record. AD-WS-26
  prescribes one ``Addresses`` Relationship to the produced
  Deliverable Revision (``semantic_role IS NULL``).
- :func:`test_completion_creation_persists_one_record_one_relationship_one_audit`
  exercises :meth:`CompletionService.create_completion` against a
  seeded ``approved`` Plan Revision rolling up at least one
  ``Accept``-outcome Milestone Acceptance. AD-WS-26 prescribes one
  ``Addresses`` Relationship to the target Plan Revision
  (``semantic_role IS NULL``).

Per Hypothesis case, each test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case identifier,
   audit, and resource state cannot leak. The engine carries the
   Slice 1, Slice 2, Slice 3 Execution, and Slice 3 Deliverable
   schemas (subset selected per test as needed).
2. Seeds the actor Party and assigning-authority Party rows so the
   ``Audit_Records.actor_party_id`` and
   ``Role_Assignments.assigning_authority_id`` FKs resolve.
3. Assigns the precise required authority to the actor: ``assign``
   for Work Assignment, ``contribute`` for Work Event / Time Entry /
   produced Deliverable / Deliverable Production,
   ``accept_milestone`` for Milestone Acceptance, ``complete`` for
   Completion (Requirement 32 / AD-WS-24).
4. Seeds the prerequisite chain needed by the action under test
   (e.g. ``Plan_Revisions`` row for Work Assignment / Completion,
   ``Work_Assignment_Records`` row for Work Event / Time Entry /
   produced Deliverable / Deliverable Production, additionally
   ``Deliverable_Revisions`` + ``Deliverable_Expectation_Revisions``
   for Deliverable Production, additionally a Deliverable
   Production Record for Milestone Acceptance, additionally an
   Accept Milestone Acceptance for Completion). Prerequisites are
   seeded with direct ``INSERT`` rather than through the upstream
   service to keep the property scoped to the single action under
   test.
5. Invokes the create method with the Hypothesis-drawn body inside
   one ``engine.begin()`` block so the AD-WS-5 "audit-and-write
   atomic" contract participates in the test.
6. Asserts the four invariants of Property 31:
   - **Record count** — exactly one row in the target Slice 3
     Record table; for produced Deliverables, additionally exactly
     one row in ``Deliverable_Revisions``.
   - **Relationship count** — exactly the prescribed AD-WS-26
     Relationship rows sourced from the new Record with the exact
     ``relationship_type`` and ``semantic_role`` values.
   - **Consequential audit count** — exactly one ``Audit_Records``
     row with ``outcome='consequential'`` and the action_type for
     the kind under test.
   - **Byte-equivalent recorded times** — the persisted Record
     row's ``recorded_at`` (or ``created_at`` for produced
     Deliverable Resource), the persisted first Revision row's
     ``recorded_at`` (for produced Deliverable), every prescribed
     Relationship row's ``recorded_at``, and the consequential
     audit row's ``recorded_at`` are all the same string.

Setup follows the conventions established by Slice 1 property tests
(per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory
state cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps), Slice 2
property tests (per-case engine carrying both schemas), and Slice 3
unit tests (direct-INSERT helpers for Slice 2 / Slice 3 prerequisite
rows that bypass services not under test).
"""

from __future__ import annotations

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

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceService,
)
from walking_slice.execution.time_entries import TimeEntryService
from walking_slice.execution.work_assignments import WorkAssignmentService
from walking_slice.execution.work_events import WorkEventService
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
# Fixed identifiers — seed contents that resolve every FK without
# bleeding into the Hypothesis-drawn fields. Property 31 only asserts
# on the cardinality / linkage / timestamp invariants, so deterministic
# IDs keep the shrunken counterexamples actionable.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_ACTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNEE_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a3"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a4"

_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-31/scope"

# Slice 2 prerequisite identifiers (seeded directly via INSERT).
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_APPROVED_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"

# Slice 3 prerequisite identifiers (seeded directly via INSERT).
_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-0000000000e1"
_DELIVERABLE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000e2"
_DELIVERABLE_EXPECTATION_ID: Final[str] = "00000000-0000-7000-8000-0000000000f1"
_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000f2"
)
_DELIVERABLE_PRODUCTION_ID: Final[str] = "00000000-0000-7000-8000-0000000000d2"
_ACCEPT_MILESTONE_ACCEPTANCE_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d3"
)

# Canonical UUIDv7 lowercase-hex pattern, mirroring the Slice 1 /
# Slice 2 property tests. Property 31 only requires uniqueness of the
# persisted identifiers; checking the canonical form keeps a sanity
# rail in place against any future refactor that swaps the identity
# generator.
_CANONICAL_UUID7: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# Effort-period window strictly inside the FixedClock instant so every
# Hypothesis-generated Time Entry satisfies
# ``effort_period_start <= effort_period_end <= recorded_at``.
_PERIOD_START_DT: Final[datetime] = datetime(
    2025, 12, 31, 22, 0, 0, tzinfo=timezone.utc
)
_PERIOD_END_DT: Final[datetime] = datetime(
    2025, 12, 31, 23, 0, 0, tzinfo=timezone.utc
)



# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case state cannot leak between generated
# inputs. The engine carries Slice 1, Slice 2 (Planning), Slice 3
# Execution_Service, and Slice 3 Deliverable_Repository schemas — the
# full surface every property in this file may consult. A
# :class:`tempfile.TemporaryDirectory` context inside each test body
# owns the per-case directory; function-scoped pytest fixtures would
# not reset between Hypothesis-generated inputs.
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
    return engine


# ---------------------------------------------------------------------------
# Service bundle.
#
# Each property test constructs its own service instance(s) using the
# bundle below. Fresh services per Hypothesis case so
# :class:`IdentityService` in-memory state cannot bleed across
# shrinks, and the denial-audit sleep is replaced with a no-op so the
# (unused) deny-path retries do not spend real time.
# ---------------------------------------------------------------------------


def _build_services() -> tuple[
    FixedClock,
    IdentityService,
    AuditLog,
    AuthorizationService,
]:
    """Construct the per-case service bundle.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state and any audit-correlation accumulator cannot
    bleed across shrinks.
    """
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    return clock, identity_service, audit_log, authorization_service


# ---------------------------------------------------------------------------
# Seed helpers.
#
# The helpers below seed the minimum prerequisite chain each property
# test needs. Prerequisites are written through direct INSERT (rather
# than through the corresponding Execution / Deliverable Service) so
# each property test exercises exactly one create operation — Property
# 31 is a single-action invariant and isolating the action under test
# keeps shrunken counterexamples actionable.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the four Party rows referenced by the test surface."""
    with engine.begin() as conn:
        for party_id, display in (
            (_ACTOR_PARTY_ID, "Property 31 Actor"),
            (_ASSIGNEE_PARTY_ID, "Property 31 Assignee"),
            (_ASSIGNING_AUTHORITY_ID, "Property 31 Resource Steward"),
            (_CONTRIBUTOR_PARTY_ID, "Property 31 Contributor"),
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


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    authorities: tuple[str, ...],
    role_name: str,
    party_id: str = _ACTOR_PARTY_ID,
) -> None:
    """Grant ``authorities`` over ``_SCOPE`` to the actor Party.

    Each property test selects the authority required by the action
    under test (``assign`` for Work Assignment, ``contribute`` for
    Work Event / Time Entry / produced Deliverable / Deliverable
    Production, ``accept_milestone`` for Milestone Acceptance,
    ``complete`` for Completion per Requirement 32 / AD-WS-24). The
    role-assignment effective period generously brackets the fixed
    clock instant so a Hypothesis-shrunken case never misses on
    timing.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=_SCOPE,
        authorities_granted=authorities,
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_project(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _NOW_ISO},
        )


def _seed_activity_plan(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 31 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_approved_plan_revision(engine: Engine) -> None:
    """Seed one ``Plan_Revisions`` row with ``lifecycle_state='approved'``.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so a row
    with ``lifecycle_state='approved'`` may be inserted in one
    statement without driving the full Plan Approval transaction.
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
                    :rev, :aid, NULL, 'approved',
                    'Property 31 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    assignee_party_id: str = _CONTRIBUTOR_PARTY_ID,
    assignment_authority_party_id: str = _ACTOR_PARTY_ID,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The schema-level CHECK constraint
    ``assignee_party_id != assignment_authority_party_id``
    (Requirement 23.5) must be honored. Tests where the actor is
    the Work Assignment's assignee (Work Event / Time Entry /
    produced Deliverable / Deliverable Production — AD-WS-29
    second-stage check) pass a distinct
    ``assignment_authority_party_id`` (typically
    :data:`_ASSIGNEE_PARTY_ID`, which is otherwise unused in those
    tests) so the CHECK passes.
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
                    'Property 31 Work Assignment rationale.',
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _WORK_ASSIGNMENT_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
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
    originating_work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    authoring_party_id: str = _CONTRIBUTOR_PARTY_ID,
) -> None:
    """Insert one Deliverable Resource + first Revision pair."""
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Property 31 runbook', :ts)
                """
            ),
            {"did": _DELIVERABLE_ID, "ts": _NOW_ISO},
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
                "rev": _DELIVERABLE_REVISION_ID,
                "did": _DELIVERABLE_ID,
                "bytes": b"produced",
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
            {"did": _DELIVERABLE_EXPECTATION_ID, "ts": _NOW_ISO},
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
                    'Property 31 Expected Deliverable',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production_with_relationships(engine: Engine) -> None:
    """Insert a Deliverable Production Record and its three
    AD-WS-26 Relationships by direct INSERT.

    Required by the Milestone Acceptance test (resolves the
    produced Revision / target Expectation Revision via
    ``Produces`` / ``Addresses``) and the Completion test (joins
    through the Production Record to find Accept Milestones for
    a given Plan Revision).
    """
    produces_id = "00000000-0000-7000-8000-00000000d201"
    addresses_id = "00000000-0000-7000-8000-00000000d202"
    relates_to_id = "00000000-0000-7000-8000-00000000d203"
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
                    'Property 31 production rationale.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "wa": _WORK_ASSIGNMENT_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )
        # ``Produces`` Relationship to the produced Deliverable Revision.
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
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )
        # ``Addresses`` Relationship to the target Deliverable
        # Expectation Revision.
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
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )
        # ``Relates To`` Relationship to the source Work Assignment
        # Record with ``semantic_role='production_source'``.
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
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )


def _seed_accept_milestone_acceptance(engine: Engine) -> None:
    """Insert one ``Milestone_Acceptance_Records`` row with outcome
    ``'Accept'`` so the Completion service's accepted-Milestone
    existence check returns ``>= 1`` for the seeded Plan Revision.
    """
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
                    'Accept', 'Property 31 milestone accepted.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": _ACCEPT_MILESTONE_ACCEPTANCE_ID,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _ACTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )



# ---------------------------------------------------------------------------
# Audit-row + record-row probe helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_consequential_audit_rows(
    engine: Engine, *, action_type: str
) -> list[dict[str, Any]]:
    """Return every ``outcome='consequential'`` audit row of one action.

    Only Property 31's consequential audit row matters here. The
    authorization evaluation row that
    :meth:`AuthorizationService.evaluate` writes carries
    ``outcome='permit'`` (not ``'consequential'``) and is therefore
    naturally excluded by the predicate.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT actor_party_id, action_type, outcome,
                           target_id, target_revision_id,
                           correlation_id, recorded_at
                      FROM Audit_Records
                     WHERE outcome = 'consequential'
                       AND action_type = :a
                    """
                ),
                {"a": action_type},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_single_column(
    engine: Engine,
    *,
    table: str,
    column: str,
    id_column: str,
    id_value: str,
) -> Any:
    """Read one column from one row by primary-key identity."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                f"SELECT {column} FROM {table} "
                f"WHERE {id_column} = :i"
            ),
            {"i": id_value},
        ).scalar_one()


def _fetch_relationship_rows(
    engine: Engine,
    *,
    relationship_type: str,
    source_id: str,
    semantic_role: Optional[str] = None,
    semantic_role_is_null: bool = False,
) -> list[dict[str, Any]]:
    """Return ``Relationships`` rows matching the source / type / role.

    When ``semantic_role_is_null`` is ``True`` the match selects rows
    whose ``semantic_role IS NULL``; when ``semantic_role`` is
    provided the column must equal that value exactly.
    """
    sql = (
        "SELECT relationship_id, relationship_type, source_kind, "
        "source_id, source_revision_id, target_kind, target_id, "
        "target_revision_id, semantic_role, recorded_at "
        "FROM Relationships "
        "WHERE relationship_type = :rt AND source_id = :sid "
    )
    params: dict = {"rt": relationship_type, "sid": source_id}
    if semantic_role_is_null:
        sql += "AND semantic_role IS NULL"
    elif semantic_role is not None:
        sql += "AND semantic_role = :role"
        params["role"] = semantic_role
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


# ===========================================================================
# Property 31 strategies.
#
# Hypothesis text generators are restricted to printable ASCII plus a
# handful of common Latin extras so the generated content round-trips
# through SQLite's UTF-8 TEXT columns without escape ambiguity. Each
# strategy stays within the per-attribute length range named by the
# design §"Components and Interfaces" surface (and re-enforced by the
# schema CHECK constraints in
# :mod:`walking_slice.execution._persistence` and
# :mod:`walking_slice.deliverables._persistence`).
# ===========================================================================


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Strategy for a non-control text run of ``min_size..max_size`` chars."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


# Work Assignment — Requirement 23.3. ``assignment_rationale`` is 0..4000.
_work_assignment_strategy = st.fixed_dictionaries(
    {
        "assignment_rationale": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


# Work Event — Requirement 24.2. Only ``started`` is generated by this
# strategy because the per-Work-Assignment state machine described in
# design §"Event-kind state machine" requires a prior ``started`` for
# every other event kind, and Property 31 is a single-action invariant
# (the multi-event state machine is covered by Slice 3 unit tests).
_work_event_strategy = st.fixed_dictionaries(
    {
        "event_note": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


@st.composite
def _time_entry_payload(draw: Any) -> dict[str, Any]:
    """Time Entry — Requirement 25.2.

    Draws ``effort_hours`` in the canonical two-decimal-place form
    on the inclusive range ``0.00..24.00``. The ISO-decimal regex
    ``^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$`` constrains the textual
    form before normalization; this strategy emits Decimals at the
    normalized two-fractional-digit form so the application's
    normalization step round-trips byte-equivalent.
    """
    # ``effort_hours`` ∈ [0.00, 24.00] in hundredths.
    hundredths = draw(st.integers(min_value=0, max_value=2400))
    effort = Decimal(hundredths) / Decimal(100)
    # Canonicalize to two-fractional-digit form
    # (matches the application-layer normalization).
    effort = effort.quantize(Decimal("0.01"))
    return {"effort_hours": effort}


# Produced Deliverable — Requirement 26.1 / 26.5.
@st.composite
def _produced_deliverable_payload(draw: Any) -> dict[str, Any]:
    """Produced Deliverable — Requirement 26.1 / 26.5.

    Draws content bytes in the inclusive range ``1..256`` (well
    inside the 1..100 MB schema bound but bounded to keep the
    Hypothesis suite memory-light), a content type from the
    enumerated seven-value set, and a produced-Deliverable name of
    1..200 characters.
    """
    return {
        "content_bytes": draw(
            st.binary(min_size=1, max_size=256)
        ),
        "content_type": draw(
            st.sampled_from(
                (
                    "text/markdown",
                    "text/plain",
                    "application/pdf",
                    "application/json",
                    "image/png",
                    "image/svg+xml",
                    "application/octet-stream",
                )
            )
        ),
        "produced_deliverable_name": draw(_bounded_text(1, 200)),
    }


# Deliverable Production — Requirement 27.2.
# ``production_rationale`` is 0..4000.
_deliverable_production_strategy = st.fixed_dictionaries(
    {
        "production_rationale": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


# Milestone Acceptance — Requirement 28.2.
# ``outcome`` ∈ {Accept, Reject}, ``rationale`` 1..4000.
_milestone_acceptance_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(("Accept", "Reject")),
        "rationale": _bounded_text(1, 500),
    }
)


# Completion — Requirement 29.2.
# ``outcome`` ∈ {Completed, Completed_With_Reservation}, ``rationale``
# 1..4000.
_completion_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(
            ("Completed", "Completed_With_Reservation")
        ),
        "rationale": _bounded_text(1, 500),
    }
)



# ===========================================================================
# The seven property tests.
# ===========================================================================


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_work_assignment_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_work_assignment_creation_persists_one_record_two_relationships_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Work Assignment creation request:

    - exactly one ``Work_Assignment_Records`` row exists, named by
      the audit row's ``target_id``,
    - exactly one ``Addresses`` ``Relationships`` row binds the
      Work Assignment to the target Plan Revision with
      ``semantic_role IS NULL`` (AD-WS-26),
    - exactly one ``Relates To`` ``Relationships`` row binds the
      Work Assignment to the assignee Party with
      ``semantic_role='assignee'`` (AD-WS-26),
    - exactly one consequential ``Audit_Records`` row exists with
      ``action_type='create.work_assignment'``,
    - the four rows carry byte-equivalent recorded times.

    **Validates: Requirements 23.1, 23.3, 23.8, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_wa_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("assign",),
                role_name="assignment_authority",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)

            service = WorkAssignmentService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=PlanRevisionService(
                    clock=clock,
                    identity_service=identity_service,
                    audit_log=audit_log,
                    authorization_service=authorization_service,
                ),
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_ACTOR_PARTY_ID,
                    assignment_rationale=payload["assignment_rationale"],
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.work_assignment_id)
            assert _count(engine, "Work_Assignment_Records") == 1

            # Exactly one Addresses Relationship to the Plan Revision
            # with semantic_role IS NULL per AD-WS-26.
            addresses_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Addresses",
                source_id=result.work_assignment_id,
                semantic_role_is_null=True,
            )
            assert len(addresses_rows) == 1
            addresses_row = addresses_rows[0]
            assert addresses_row["source_kind"] == "work_assignment_record"
            assert addresses_row["target_kind"] == "plan_revision"
            assert addresses_row["target_id"] == _APPROVED_PLAN_REVISION_ID
            assert addresses_row["target_revision_id"] is None

            # Exactly one Relates To Relationship to the assignee
            # Party with semantic_role='assignee' per AD-WS-26.
            relates_to_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Relates To",
                source_id=result.work_assignment_id,
                semantic_role="assignee",
            )
            assert len(relates_to_rows) == 1
            relates_to_row = relates_to_rows[0]
            assert relates_to_row["source_kind"] == "work_assignment_record"
            assert relates_to_row["target_kind"] == "party"
            assert relates_to_row["target_id"] == _ASSIGNEE_PARTY_ID

            # Exactly one consequential audit row.
            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.work_assignment"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.work_assignment_id
            assert audit_row["actor_party_id"] == _ACTOR_PARTY_ID

            # Byte-equivalent recorded times.
            record_recorded_at = _fetch_single_column(
                engine,
                table="Work_Assignment_Records",
                column="recorded_at",
                id_column="work_assignment_id",
                id_value=result.work_assignment_id,
            )
            assert addresses_row["recorded_at"] == record_recorded_at
            assert relates_to_row["recorded_at"] == record_recorded_at
            assert audit_row["recorded_at"] == record_recorded_at
            assert result.recorded_at == record_recorded_at
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_work_event_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_work_event_creation_persists_one_record_one_relationship_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Work Event ``started`` creation:

    - exactly one ``Work_Event_Records`` row,
    - exactly one ``Relates To`` ``Relationships`` row binding the
      Work Event to the target Work Assignment Record with
      ``semantic_role='work_event'`` (AD-WS-26),
    - exactly one consequential ``Audit_Records`` row with
      ``action_type='create.work_event'``,
    - byte-equivalent recorded times across the three rows.

    The recording Party (actor) matches the Work Assignment's
    seeded assignee so the AD-WS-29 second-stage check passes.

    **Validates: Requirements 24.1, 24.6, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_we_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            # The actor is also the assignee so AD-WS-29 passes.
            _seed_work_assignment(
                engine,
                assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )

            service = WorkEventService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_work_event(
                    conn,
                    target_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    event_kind="started",
                    event_note=payload["event_note"],
                    recording_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.work_event_id)
            assert _count(engine, "Work_Event_Records") == 1

            relates_to_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Relates To",
                source_id=result.work_event_id,
                semantic_role="work_event",
            )
            assert len(relates_to_rows) == 1
            relates_to_row = relates_to_rows[0]
            assert relates_to_row["source_kind"] == "work_event_record"
            assert relates_to_row["target_kind"] == "work_assignment_record"
            assert relates_to_row["target_id"] == _WORK_ASSIGNMENT_ID

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.work_event"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.work_event_id

            record_recorded_at = _fetch_single_column(
                engine,
                table="Work_Event_Records",
                column="recorded_at",
                id_column="work_event_id",
                id_value=result.work_event_id,
            )
            assert relates_to_row["recorded_at"] == record_recorded_at
            assert audit_row["recorded_at"] == record_recorded_at
            assert result.recorded_at == record_recorded_at
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_time_entry_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_time_entry_creation_persists_one_record_one_relationship_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Time Entry creation request:

    - exactly one ``Time_Entry_Records`` row,
    - exactly one ``Relates To`` ``Relationships`` row binding the
      Time Entry to the target Work Assignment Record with
      ``semantic_role='time_entry'`` (AD-WS-26),
    - exactly one consequential ``Audit_Records`` row with
      ``action_type='create.time_entry'``,
    - byte-equivalent recorded times across the three rows.

    The recording Party (actor) matches the Work Assignment's
    seeded assignee so the AD-WS-29 second-stage check passes.

    **Validates: Requirements 25.1, 25.5, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_te_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(
                engine,
                assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )

            service = TimeEntryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_time_entry(
                    conn,
                    target_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    effort_hours=payload["effort_hours"],
                    effort_period_start=_PERIOD_START_DT,
                    effort_period_end=_PERIOD_END_DT,
                    recording_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.time_entry_id)
            assert _count(engine, "Time_Entry_Records") == 1

            relates_to_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Relates To",
                source_id=result.time_entry_id,
                semantic_role="time_entry",
            )
            assert len(relates_to_rows) == 1
            relates_to_row = relates_to_rows[0]
            assert relates_to_row["source_kind"] == "time_entry_record"
            assert relates_to_row["target_kind"] == "work_assignment_record"
            assert relates_to_row["target_id"] == _WORK_ASSIGNMENT_ID

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.time_entry"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.time_entry_id

            record_recorded_at = _fetch_single_column(
                engine,
                table="Time_Entry_Records",
                column="recorded_at",
                id_column="time_entry_id",
                id_value=result.time_entry_id,
            )
            assert relates_to_row["recorded_at"] == record_recorded_at
            assert audit_row["recorded_at"] == record_recorded_at
            assert result.recorded_at == record_recorded_at
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_produced_deliverable_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_produced_deliverable_creation_persists_one_resource_one_revision_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid produced Deliverable creation:

    - exactly one ``Deliverable_Resources`` row,
    - exactly one ``Deliverable_Revisions`` row carrying
      ``role_marker='generated_output'`` (Requirement 26.2),
    - exactly one consequential ``Audit_Records`` row with
      ``action_type='create.produced_deliverable'``,
    - byte-equivalent recorded times across the three rows.

    AD-WS-26 does not attach a Relationship row to
    ``create.produced_deliverable`` — the ``Produces`` Relationship
    that names the produced Revision is written later by
    ``create.deliverable_production``. The property therefore
    asserts the Resource + first Revision + audit-row triple
    explicitly and does not query ``Relationships``.

    The authoring Party (actor) matches the Work Assignment's
    seeded assignee so the AD-WS-29 second-stage check passes.

    **Validates: Requirements 26.1, 26.7, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_pd_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(
                engine, assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )

            service = DeliverableRepositoryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_produced_deliverable(
                    conn,
                    content_bytes=payload["content_bytes"],
                    content_type=payload["content_type"],
                    produced_deliverable_name=(
                        payload["produced_deliverable_name"]
                    ),
                    originating_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_ACTOR_PARTY_ID,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.deliverable_id)
            assert _CANONICAL_UUID7.match(result.deliverable_revision_id)
            assert _count(engine, "Deliverable_Resources") == 1
            assert _count(engine, "Deliverable_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.produced_deliverable"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            # Deliverable_Repository writes the produced Revision
            # Identity to ``target_revision_id`` on the audit row
            # because the Revision is the post-write Identity that
            # carries the byte payload and the digest.
            assert audit_row["target_id"] == result.deliverable_id

            resource_created_at = _fetch_single_column(
                engine,
                table="Deliverable_Resources",
                column="created_at",
                id_column="deliverable_id",
                id_value=result.deliverable_id,
            )
            revision_recorded_at = _fetch_single_column(
                engine,
                table="Deliverable_Revisions",
                column="recorded_at",
                id_column="deliverable_revision_id",
                id_value=result.deliverable_revision_id,
            )
            assert resource_created_at == revision_recorded_at
            assert audit_row["recorded_at"] == revision_recorded_at
            assert result.recorded_at == revision_recorded_at

            # Persistence Invariants Summary rule 9 / Requirement
            # 41 §13 — produced-Deliverable Revision carries
            # ``role_marker='generated_output'``.
            role_marker = _fetch_single_column(
                engine,
                table="Deliverable_Revisions",
                column="role_marker",
                id_column="deliverable_revision_id",
                id_value=result.deliverable_revision_id,
            )
            assert role_marker == "generated_output"
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_deliverable_production_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_deliverable_production_creation_persists_one_record_three_relationships_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Deliverable Production creation:

    - exactly one ``Deliverable_Production_Records`` row,
    - exactly one ``Produces`` ``Relationships`` row to the produced
      Deliverable Revision (``semantic_role IS NULL``),
    - exactly one ``Addresses`` ``Relationships`` row to the target
      Deliverable Expectation Revision (``semantic_role IS NULL``),
    - exactly one ``Relates To`` ``Relationships`` row to the source
      Work Assignment Record with
      ``semantic_role='production_source'``,
    - exactly one consequential ``Audit_Records`` row with
      ``action_type='create.deliverable_production'``,
    - byte-equivalent recorded times across the five rows.

    The recording Party (actor) matches the Work Assignment's
    seeded assignee so the AD-WS-29 second-stage check passes; the
    produced Deliverable Revision's
    ``originating_work_assignment_id`` matches the source Work
    Assignment so Requirement 27.4 passes; the Expectation Revision
    targets the same Project as the Work Assignment's Plan Revision
    so Requirement 27.3 passes.

    **Validates: Requirements 27.1, 27.6, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_dp_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(
                engine,
                assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )
            _seed_deliverable_resource_and_revision(
                engine, authoring_party_id=_ACTOR_PARTY_ID
            )
            _seed_deliverable_expectation(engine)

            deliverable_reader = DeliverableRepositoryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )
            expectation_reader = DeliverableExpectationService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            project_resolver = ProjectResolver()

            service = DeliverableProductionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                deliverable_reader=deliverable_reader,
                planning_reader=expectation_reader,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_deliverable_production(
                    conn,
                    source_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    produced_deliverable_revision_id=(
                        _DELIVERABLE_REVISION_ID
                    ),
                    target_deliverable_expectation_revision_id=(
                        _DELIVERABLE_EXPECTATION_REVISION_ID
                    ),
                    production_rationale=payload["production_rationale"],
                    recording_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.deliverable_production_id)
            assert _count(engine, "Deliverable_Production_Records") == 1

            produces_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Produces",
                source_id=result.deliverable_production_id,
                semantic_role_is_null=True,
            )
            assert len(produces_rows) == 1
            produces_row = produces_rows[0]
            assert produces_row["source_kind"] == (
                "deliverable_production_record"
            )
            assert produces_row["target_kind"] == "deliverable_revision"
            assert produces_row["target_id"] == _DELIVERABLE_ID
            assert produces_row["target_revision_id"] == (
                _DELIVERABLE_REVISION_ID
            )

            addresses_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Addresses",
                source_id=result.deliverable_production_id,
                semantic_role_is_null=True,
            )
            assert len(addresses_rows) == 1
            addresses_row = addresses_rows[0]
            assert addresses_row["source_kind"] == (
                "deliverable_production_record"
            )
            assert addresses_row["target_kind"] == (
                "deliverable_expectation_revision"
            )

            relates_to_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Relates To",
                source_id=result.deliverable_production_id,
                semantic_role="production_source",
            )
            assert len(relates_to_rows) == 1
            relates_to_row = relates_to_rows[0]
            assert relates_to_row["source_kind"] == (
                "deliverable_production_record"
            )
            assert relates_to_row["target_kind"] == "work_assignment_record"
            assert relates_to_row["target_id"] == _WORK_ASSIGNMENT_ID

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.deliverable_production"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.deliverable_production_id

            record_recorded_at = _fetch_single_column(
                engine,
                table="Deliverable_Production_Records",
                column="recorded_at",
                id_column="deliverable_production_id",
                id_value=result.deliverable_production_id,
            )
            assert produces_row["recorded_at"] == record_recorded_at
            assert addresses_row["recorded_at"] == record_recorded_at
            assert relates_to_row["recorded_at"] == record_recorded_at
            assert audit_row["recorded_at"] == record_recorded_at
            assert result.recorded_at == record_recorded_at
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_milestone_acceptance_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_milestone_acceptance_creation_persists_one_record_one_relationship_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Milestone Acceptance creation:

    - exactly one ``Milestone_Acceptance_Records`` row,
    - exactly one ``Addresses`` ``Relationships`` row to the
      produced Deliverable Revision (``semantic_role IS NULL``)
      per AD-WS-26,
    - exactly one consequential ``Audit_Records`` row with
      ``action_type='create.milestone_acceptance'``,
    - byte-equivalent recorded times across the three rows.

    The accepting Party (actor) holds ``accept_milestone`` (no
    AD-WS-29 binding applies for this action).

    **Validates: Requirements 28.1, 28.6, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_ma_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("accept_milestone",),
                role_name="milestone_acceptor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(engine)
            _seed_deliverable_resource_and_revision(engine)
            _seed_deliverable_expectation(engine)
            _seed_deliverable_production_with_relationships(engine)

            # The Milestone Acceptance Service keeps a Production
            # Service reference on its public dataclass even though
            # the implementation resolves the Production row via
            # direct SQL; passing a partially-wired Production
            # Service (collaborators it does not need set to None)
            # mirrors the Slice 3 unit-test convention in
            # ``tests/unit/test_execution_milestone_acceptances.py``.
            production_reader = DeliverableProductionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                deliverable_reader=None,  # type: ignore[arg-type]
                planning_reader=None,  # type: ignore[arg-type]
                project_resolver=None,  # type: ignore[arg-type]
                denial_audit_sleep=lambda _seconds: None,
            )

            service = MilestoneAcceptanceService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                production_reader=production_reader,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        _DELIVERABLE_PRODUCTION_ID
                    ),
                    outcome=payload["outcome"],
                    rationale=payload["rationale"],
                    accepting_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.milestone_acceptance_id)
            assert _count(engine, "Milestone_Acceptance_Records") == 1

            addresses_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Addresses",
                source_id=result.milestone_acceptance_id,
                semantic_role_is_null=True,
            )
            assert len(addresses_rows) == 1
            addresses_row = addresses_rows[0]
            assert addresses_row["source_kind"] == (
                "milestone_acceptance_record"
            )
            assert addresses_row["target_kind"] == "deliverable_revision"
            assert addresses_row["target_id"] == _DELIVERABLE_ID
            assert addresses_row["target_revision_id"] == (
                _DELIVERABLE_REVISION_ID
            )

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.milestone_acceptance"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.milestone_acceptance_id

            record_recorded_at = _fetch_single_column(
                engine,
                table="Milestone_Acceptance_Records",
                column="recorded_at",
                id_column="milestone_acceptance_id",
                id_value=result.milestone_acceptance_id,
            )
            assert addresses_row["recorded_at"] == record_recorded_at
            assert audit_row["recorded_at"] == record_recorded_at
            assert result.recorded_at == record_recorded_at
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 31: Execution-creation success
@given(payload=_completion_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_completion_creation_persists_one_record_one_relationship_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Completion creation request:

    - exactly one ``Completion_Records`` row,
    - exactly one ``Addresses`` ``Relationships`` row to the target
      Approved Plan Revision (``semantic_role IS NULL``) per
      AD-WS-26,
    - exactly one consequential ``Audit_Records`` row with
      ``action_type='create.completion'``,
    - byte-equivalent recorded times across the three rows.

    The completing Party (actor) holds ``complete`` (no AD-WS-29
    binding applies for this action). The seeded graph carries one
    Accept-outcome Milestone Acceptance so Requirement 29.1's
    existence check returns ``>= 1``.

    **Validates: Requirements 29.1, 29.6, 37.1, 41.1**
    """
    with tempfile.TemporaryDirectory(prefix="prop31_cp_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("complete",),
                role_name="completion_authority",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(engine)
            _seed_deliverable_resource_and_revision(engine)
            _seed_deliverable_expectation(engine)
            _seed_deliverable_production_with_relationships(engine)
            _seed_accept_milestone_acceptance(engine)

            planning_reader = PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            project_resolver = ProjectResolver()

            service = CompletionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=planning_reader,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome=payload["outcome"],  # type: ignore[arg-type]
                    rationale=payload["rationale"],
                    source_milestone_acceptance_ids=(),
                    completing_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.completion_id)
            assert _count(engine, "Completion_Records") == 1

            addresses_rows = _fetch_relationship_rows(
                engine,
                relationship_type="Addresses",
                source_id=result.completion_id,
                semantic_role_is_null=True,
            )
            assert len(addresses_rows) == 1
            addresses_row = addresses_rows[0]
            assert addresses_row["source_kind"] == "completion_record"
            assert addresses_row["target_kind"] == "plan_revision"
            assert addresses_row["target_id"] == _APPROVED_PLAN_REVISION_ID
            assert addresses_row["target_revision_id"] is None

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.completion"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.completion_id

            record_recorded_at = _fetch_single_column(
                engine,
                table="Completion_Records",
                column="recorded_at",
                id_column="completion_id",
                id_value=result.completion_id,
            )
            assert addresses_row["recorded_at"] == record_recorded_at
            assert audit_row["recorded_at"] == record_recorded_at
            assert result.recorded_at == record_recorded_at
        finally:
            engine.dispose()
