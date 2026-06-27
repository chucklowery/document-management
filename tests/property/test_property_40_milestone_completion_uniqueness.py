# Feature: third-walking-slice, Property 40: Milestone Acceptance and Completion uniqueness
"""Property 40 — Uniqueness of Milestone Acceptance and Completion
(task 16.10).

**Property 40: Uniqueness of Milestone Acceptance and Completion**

*For all* source Deliverable Production Records persisted in any test
session and *for all* target Plan Revisions, at every observation
point at most one Milestone Acceptance Record exists per source
Deliverable Production Record (Requirement 28.3) and at most one
Completion Record exists per target Plan Revision (Requirement 29.3).
A second attempt against the same source / target is rejected, leaves
no second Record persisted, and leaves the first Record byte-equivalent
to its prior state.

**Validates: Requirements 28.3, 29.3, 41.10**

Strategy
========

Two independent property tests, each one drawing a pair of attempts
against the same uniqueness key:

- :func:`test_double_milestone_acceptance_rejected_and_first_record_byte_equivalent`
  drives :meth:`MilestoneAcceptanceService.create_milestone_acceptance`
  twice against the same source Deliverable Production Record. The
  first attempt succeeds (Requirement 28.1); the second attempt is
  rejected with :class:`MilestoneAcceptanceConflictError` carrying
  ``failed_constraint='milestone_acceptance_already_recorded'``
  (Requirement 28.3). The schema-level
  ``UNIQUE(source_deliverable_production_id)`` constraint is the
  source of truth; the application-level pre-check surfaces the
  structured 409.
- :func:`test_double_completion_rejected_and_first_record_byte_equivalent`
  drives :meth:`CompletionService.create_completion` twice against the
  same target Approved Plan Revision. The first attempt succeeds
  (Requirement 29.1); the second attempt is rejected with
  :class:`CompletionConflictError` carrying
  ``failed_constraint='completion_already_exists'`` (Requirement
  29.3).

Per Hypothesis case, each test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing
   Strategy" — per-case database isolation) carrying the Slice 1
   schema, the Slice 2 Planning schema, the Slice 3 Execution
   schema, and the Slice 3 Deliverable_Repository schema. The
   Slice 3 Execution schema is where the
   ``UNIQUE(source_deliverable_production_id)`` and
   ``UNIQUE(target_plan_revision_id)`` constraints live.
2. Seeds the actor Party plus the assigning-authority Party, a
   ``Projects`` row, an ``Activity_Plans`` row, one ``approved``
   ``Plan_Revisions`` row, one ``Work_Assignment_Records`` row, one
   ``Deliverable_Resources`` + first ``Deliverable_Revisions`` pair,
   one ``Deliverable_Expectations`` + first
   ``Deliverable_Expectation_Revisions`` pair, one
   ``Deliverable_Production_Records`` row with its three AD-WS-26
   Relationship rows present (``Produces``, ``Addresses``,
   ``Relates To``), and — for the Completion test only — one
   ``Accept``-outcome ``Milestone_Acceptance_Records`` row so the
   Completion service's accepted-Milestone existence check returns
   ``>= 1`` (Requirement 29.1).
3. Grants the actor the precise required authority on the
   applicable scope: ``accept_milestone`` for the Milestone
   Acceptance test (Requirement 32.8) and ``complete`` for the
   Completion test (Requirement 32.9).
4. Issues the first
   :meth:`MilestoneAcceptanceService.create_milestone_acceptance`
   or :meth:`CompletionService.create_completion` call. The first
   attempt is permitted by Requirements 28.1 / 29.1 because the
   uniqueness key is fresh; exactly one Record row is persisted.
5. Snapshots that row by SELECT-ing every persisted column in stable
   primary-key order. The snapshot is the byte-equivalence ground
   truth for the post-second-attempt comparison.
6. Issues a second create call against the *same* uniqueness key.
   Requirements 28.3 / 29.3 reject this attempt; the service
   surfaces the structured ConflictError before the second INSERT
   is even attempted.
7. Asserts exactly one row remains in the target Slice 3 table
   after the second attempt and re-snapshots it. The post-snapshot
   is asserted byte-equivalent to the pre-snapshot — Property 40's
   universal quantifier (Requirement 41.10 — Slice 3 row immutability
   under repeated attempts).

Setup follows the conventions established by the Slice 1 / Slice 2 /
Slice 3 property tests (per-case
:class:`tempfile.TemporaryDirectory` ownership of the SQLite file,
fresh services per case so :class:`IdentityService` in-memory state
cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps).
"""

from __future__ import annotations

import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

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
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import (
    CompletionConflictError,
    CompletionService,
)
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceConflictError,
    MilestoneAcceptanceService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# Property 40 quantifies over one uniqueness key per Hypothesis case —
# a single Deliverable Production Record (Milestone Acceptance test)
# or a single Plan Revision (Completion test). Deterministic seed
# identifiers keep shrunken counterexamples actionable; the
# Hypothesis-drawn fields are only ``outcome`` and ``rationale`` for
# each attempt.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_ACTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_ASSIGNMENT_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a3"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a4"

_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-40/scope"

# Slice 2 prerequisite identifiers (seeded directly via INSERT so the
# property test exercises exactly the Slice 3 service under test).
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_APPROVED_PLAN_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000c3"
)

# Slice 3 prerequisite identifiers (seeded directly via INSERT).
_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-0000000000e1"
_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000e2"
)
_DELIVERABLE_EXPECTATION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000f1"
)
_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000f2"
)
_DELIVERABLE_PRODUCTION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d2"
)
_ACCEPT_MILESTONE_ACCEPTANCE_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d3"
)


# Columns of ``Milestone_Acceptance_Records`` snapshotted for the
# byte-equivalence check. Every persisted column is listed in stable
# order so any drift between the pre- and post-second-attempt
# snapshots surfaces a precise tuple diff in a failing assertion.
_MILESTONE_ACCEPTANCE_COLUMNS: Final[tuple[str, ...]] = (
    "milestone_acceptance_id",
    "source_deliverable_production_id",
    "produced_deliverable_id",
    "produced_deliverable_revision_id",
    "target_deliverable_expectation_id",
    "target_deliverable_expectation_revision_id",
    "outcome",
    "rationale",
    "accepting_party_id",
    "authority_basis_type",
    "authority_basis_id",
    "applicable_scope",
    "recorded_at",
)


# Columns of ``Completion_Records`` snapshotted for the byte-
# equivalence check. Same convention as
# :data:`_MILESTONE_ACCEPTANCE_COLUMNS`.
_COMPLETION_COLUMNS: Final[tuple[str, ...]] = (
    "completion_id",
    "target_plan_revision_id",
    "target_activity_plan_id",
    "target_project_id",
    "outcome",
    "rationale",
    "source_milestone_acceptance_ids_json",
    "completing_party_id",
    "authority_basis_type",
    "authority_basis_id",
    "applicable_scope",
    "recorded_at",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case identifiers, audit rows, and seeded
# pipelines cannot leak between cases (design §"Testing Strategy").
# :class:`tempfile.TemporaryDirectory` owns the per-case directory;
# function-scoped pytest fixtures cannot be used here because
# Hypothesis does not reset them between drawn inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    and the Slice 1 + Slice 2 + Slice 3 (Execution + Deliverable)
    schemas installed."""
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
# Service factories.
#
# Fresh services per Hypothesis case so :class:`IdentityService`'s
# in-memory issued-identifier set and any audit-correlation
# accumulator cannot bleed across shrinks. The denial-audit sleep is
# replaced with a no-op so the (unused on permitted-then-conflict
# paths) deny-path retries do not spend real time.
# ---------------------------------------------------------------------------


def _build_authorization() -> tuple[
    FixedClock,
    IdentityService,
    AuditLog,
    AuthorizationService,
]:
    """Construct the per-case core service bundle."""
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    return clock, identity_service, audit_log, authorization_service


def _build_milestone_acceptance_service(
    clock: FixedClock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> MilestoneAcceptanceService:
    """Construct a :class:`MilestoneAcceptanceService` with a
    minimally-wired :class:`DeliverableProductionService` as its
    ``production_reader`` collaborator (the field is declared on the
    public dataclass surface even though the implementation resolves
    the source Production row via direct SQL — mirrors the convention
    in ``tests/unit/test_execution_milestone_acceptances.py``)."""
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
    return MilestoneAcceptanceService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        production_reader=production_reader,
        denial_audit_sleep=lambda _seconds: None,
    )


def _build_completion_service(
    clock: FixedClock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> CompletionService:
    """Construct a :class:`CompletionService` wired with a real
    :class:`PlanRevisionService` and :class:`ProjectResolver` so the
    accepted-Milestone existence query and the Plan Revision lookup
    participate against the seeded schema."""
    planning_reader = PlanRevisionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    project_resolver = ProjectResolver()
    return CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=planning_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Prerequisites are written through direct INSERT (rather than through
# the corresponding Slice 1 / Slice 2 / Slice 3 service) so each
# property case exercises exactly the *one* uniqueness invariant
# under test, and a shrunken counterexample stays actionable.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the four Party rows referenced by the test surface."""
    with engine.begin() as conn:
        for party_id, display in (
            (_ACTOR_PARTY_ID, "Property 40 Actor"),
            (_CONTRIBUTOR_PARTY_ID, "Property 40 Contributor"),
            (_ASSIGNMENT_AUTHORITY_ID, "Property 40 Assignment Authority"),
            (_ASSIGNING_AUTHORITY_ID, "Property 40 Resource Steward"),
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
) -> None:
    """Grant ``authorities`` over ``_SCOPE`` to the actor Party.

    Either ``accept_milestone`` (Requirement 32.8) for the Milestone
    Acceptance test or ``complete`` (Requirement 32.9) for the
    Completion test. The role-assignment effective period generously
    brackets the fixed clock instant so a Hypothesis-shrunken case
    never misses on timing.
    """
    request = AssignRoleRequest(
        party_id=_ACTOR_PARTY_ID,
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
                    :aid, :pid, 'Property 40 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_approved_plan_revision(engine: Engine) -> None:
    """Insert one ``Plan_Revisions`` row with
    ``lifecycle_state='approved'``.

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
                    'Property 40 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_assignment(engine: Engine) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The schema-level CHECK constraint
    ``assignee_party_id != assignment_authority_party_id``
    (Requirement 23.5) is honored by naming the Contributor as the
    assignee and a distinct Assignment Authority Party as the
    assignment authority. Neither this row nor any other Slice 3
    row referencing it is mutated by the action under test
    (the AD-WS-29 second-stage check does not apply to
    ``accept_milestone`` or ``complete``).
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
                    'Property 40 Work Assignment rationale.',
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _WORK_ASSIGNMENT_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNMENT_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_resource_and_revision(engine: Engine) -> None:
    """Insert one Deliverable Resource + first Revision pair."""
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Property 40 runbook', :ts)
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
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
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
                    'Property 40 Expected Deliverable',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production_with_relationships(engine: Engine) -> None:
    """Insert a Deliverable Production Record and its three AD-WS-26
    Relationships by direct INSERT.

    The Milestone Acceptance Service resolves the produced
    Deliverable Revision and the target Deliverable Expectation
    Revision from the source Production Record's ``Produces`` and
    ``Addresses`` Relationship rows; the third ``Relates To``
    Relationship is seeded for completeness (Requirement 27.2
    requires every Production Record to carry all three).
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
                    'Property 40 production rationale.',
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
        # ``Produces`` Relationship to the produced Deliverable Revision
        # per AD-WS-26 (``semantic_role IS NULL``).
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
        # Expectation Revision per AD-WS-26 (``semantic_role IS NULL``).
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
        # Record per AD-WS-26 (``semantic_role='production_source'``).
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
    existence check (Requirement 29.1) returns ``>= 1`` for the
    seeded Plan Revision.

    Only used by the Completion test; the Milestone Acceptance test
    does not seed any ``Milestone_Acceptance_Records`` row because
    that test exercises the *first*-attempt write through the
    service.
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
                    'Accept', 'Property 40 milestone accepted.',
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
# Snapshot helpers.
#
# Read every Slice 3 Record row in stable PK order and return the
# rows as a tuple of column tuples so the byte-equivalence comparison
# reduces to one ``==``. Storing the full row tuple keeps a failing
# assertion informative — Hypothesis prints the differing tuples
# directly.
# ---------------------------------------------------------------------------


def _snapshot_milestone_acceptance_rows(
    engine: Engine,
) -> tuple[tuple[Any, ...], ...]:
    """Return every ``Milestone_Acceptance_Records`` row in PK order."""
    columns = ", ".join(_MILESTONE_ACCEPTANCE_COLUMNS)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {columns} FROM Milestone_Acceptance_Records "
                "ORDER BY milestone_acceptance_id"
            )
        ).all()
    return tuple(tuple(row) for row in rows)


def _snapshot_completion_rows(
    engine: Engine,
) -> tuple[tuple[Any, ...], ...]:
    """Return every ``Completion_Records`` row in PK order."""
    columns = ", ".join(_COMPLETION_COLUMNS)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {columns} FROM Completion_Records "
                "ORDER BY completion_id"
            )
        ).all()
    return tuple(tuple(row) for row in rows)


def _count_rows(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# Each case draws a first-attempt ``outcome`` / ``rationale`` and a
# second-attempt ``outcome`` / ``rationale``. The outcome enumerations
# come straight from Requirements 28.2 (Milestone Acceptance) and
# 29.2 (Completion); the rationale alphabet is restricted to a narrow
# printable-ASCII set so cases stay readable when Hypothesis shrinks
# (Property 40 is not about UTF-8 robustness — it is about the
# uniqueness invariant).
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Strategy for a non-control text run of ``min_size..max_size`` chars."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


# Milestone Acceptance — Requirement 28.2.
# ``outcome`` ∈ {Accept, Reject}, ``rationale`` is 1..4000 characters
# (this strategy keeps the upper bound modest so the property suite is
# memory-light; the boundary values are covered by the unit tests).
_double_milestone_acceptance_strategy = st.fixed_dictionaries(
    {
        "first_outcome": st.sampled_from(("Accept", "Reject")),
        "first_rationale": _bounded_text(1, 200),
        "second_outcome": st.sampled_from(("Accept", "Reject")),
        "second_rationale": _bounded_text(1, 200),
    }
)


# Completion — Requirement 29.2.
# ``outcome`` ∈ {Completed, Completed_With_Reservation},
# ``rationale`` is 1..4000 characters (same memory-light bound as
# above).
_double_completion_strategy = st.fixed_dictionaries(
    {
        "first_outcome": st.sampled_from(
            ("Completed", "Completed_With_Reservation")
        ),
        "first_rationale": _bounded_text(1, 200),
        "second_outcome": st.sampled_from(
            ("Completed", "Completed_With_Reservation")
        ),
        "second_rationale": _bounded_text(1, 200),
    }
)


# ===========================================================================
# Property 40 — Milestone Acceptance uniqueness.
# ===========================================================================


# Feature: third-walking-slice, Property 40: Milestone Acceptance and Completion uniqueness
@given(payload=_double_milestone_acceptance_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_double_milestone_acceptance_rejected_and_first_record_byte_equivalent(
    payload: dict[str, Any],
) -> None:
    """**Validates: Requirements 28.3, 41.10**

    For any source Deliverable Production Record and any pair of
    authorized Milestone Acceptance attempts against it:

    - The first attempt persists exactly one
      ``Milestone_Acceptance_Records`` row.
    - The second attempt is rejected with
      :class:`MilestoneAcceptanceConflictError`
      (``failed_constraint='milestone_acceptance_already_recorded'``)
      per Requirement 28.3.
    - No second ``Milestone_Acceptance_Records`` row is persisted.
    - The first ``Milestone_Acceptance_Records`` row is
      byte-equivalent before and after the second attempt — every
      persisted column (including ``recorded_at``) is unchanged
      (Requirement 41.10).
    """
    first_outcome = payload["first_outcome"]
    first_rationale = payload["first_rationale"]
    second_outcome = payload["second_outcome"]
    second_rationale = payload["second_rationale"]

    with tempfile.TemporaryDirectory(prefix="prop40_ma_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_authorization()

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

            service = _build_milestone_acceptance_service(
                clock, identity_service, audit_log, authorization_service
            )

            # 1. First Milestone Acceptance — must succeed and persist
            # exactly one row.
            with engine.begin() as conn:
                first_result = service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        _DELIVERABLE_PRODUCTION_ID
                    ),
                    outcome=first_outcome,
                    rationale=first_rationale,
                    accepting_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop40-ma-first",
                )

            assert _count_rows(engine, "Milestone_Acceptance_Records") == 1
            assert first_result.source_deliverable_production_id == (
                _DELIVERABLE_PRODUCTION_ID
            )

            # 2. Snapshot the persisted Milestone Acceptance row
            # before the second attempt. This snapshot is the
            # byte-equivalence ground truth.
            pre_snapshot = _snapshot_milestone_acceptance_rows(engine)
            assert len(pre_snapshot) == 1

            # 3. Second Milestone Acceptance against the SAME source
            # Deliverable Production Record — must be rejected with
            # :class:`MilestoneAcceptanceConflictError` per
            # Requirement 28.3.
            with pytest.raises(MilestoneAcceptanceConflictError) as exc_info:
                with engine.begin() as conn:
                    service.create_milestone_acceptance(
                        conn,
                        source_deliverable_production_id=(
                            _DELIVERABLE_PRODUCTION_ID
                        ),
                        outcome=second_outcome,
                        rationale=second_rationale,
                        accepting_party_id=_ACTOR_PARTY_ID,
                        authority_basis=_BASIS,
                        applicable_scope=_SCOPE,
                        engine=engine,
                        correlation_id="prop40-ma-second",
                    )

            assert exc_info.value.failed_constraint == (
                "milestone_acceptance_already_recorded"
            )
            assert exc_info.value.source_deliverable_production_id == (
                _DELIVERABLE_PRODUCTION_ID
            )
            # The caller holds only ``accept_milestone`` (not
            # ``view``) over ``_SCOPE``; the AD-WS-9 / Slice 3
            # Requirement 30.4 view-authority gate on the conflict
            # response therefore returns ``None`` for the existing
            # Identity. What the property cares about is that the
            # rejection happened with the structured failed_constraint
            # — the indistinguishable-denial body is the design's
            # default and the unit suite covers the view-permitted
            # branch separately.
            assert exc_info.value.existing_milestone_acceptance_id is None

            # 4. Still exactly one row in
            # ``Milestone_Acceptance_Records`` — no second row was
            # persisted.
            assert _count_rows(engine, "Milestone_Acceptance_Records") == 1

            # 5. The first Milestone Acceptance row is byte-equivalent
            # before and after the second attempt (Property 40's
            # universal quantifier; Requirement 41.10).
            post_snapshot = _snapshot_milestone_acceptance_rows(engine)
            assert post_snapshot == pre_snapshot
        finally:
            engine.dispose()


# ===========================================================================
# Property 40 — Completion uniqueness.
# ===========================================================================


# Feature: third-walking-slice, Property 40: Milestone Acceptance and Completion uniqueness
@given(payload=_double_completion_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_double_completion_rejected_and_first_record_byte_equivalent(
    payload: dict[str, Any],
) -> None:
    """**Validates: Requirements 29.3, 41.10**

    For any target Approved Plan Revision (with at least one
    ``Accept``-outcome Milestone Acceptance Record rolled up by the
    seeded execution graph) and any pair of authorized Completion
    attempts against it:

    - The first attempt persists exactly one ``Completion_Records``
      row.
    - The second attempt is rejected with
      :class:`CompletionConflictError`
      (``failed_constraint='completion_already_exists'``) per
      Requirement 29.3.
    - No second ``Completion_Records`` row is persisted.
    - The first ``Completion_Records`` row is byte-equivalent
      before and after the second attempt — every persisted column
      (including ``recorded_at`` and the JSON-encoded
      ``source_milestone_acceptance_ids_json``) is unchanged
      (Requirement 41.10).
    """
    first_outcome = payload["first_outcome"]
    first_rationale = payload["first_rationale"]
    second_outcome = payload["second_outcome"]
    second_rationale = payload["second_rationale"]

    with tempfile.TemporaryDirectory(prefix="prop40_cp_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_authorization()

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
            # Seed one Accept-outcome Milestone Acceptance so the
            # Completion service's existence check returns >= 1.
            _seed_accept_milestone_acceptance(engine)

            service = _build_completion_service(
                clock, identity_service, audit_log, authorization_service
            )

            # 1. First Completion — must succeed and persist exactly
            # one row.
            with engine.begin() as conn:
                first_result = service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome=first_outcome,  # type: ignore[arg-type]
                    rationale=first_rationale,
                    source_milestone_acceptance_ids=(),
                    completing_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop40-cp-first",
                )

            assert _count_rows(engine, "Completion_Records") == 1
            assert first_result.target_plan_revision_id == (
                _APPROVED_PLAN_REVISION_ID
            )

            # 2. Snapshot the persisted Completion row before the
            # second attempt. This snapshot is the byte-equivalence
            # ground truth.
            pre_snapshot = _snapshot_completion_rows(engine)
            assert len(pre_snapshot) == 1

            # 3. Second Completion against the SAME target Plan
            # Revision — must be rejected with
            # :class:`CompletionConflictError` per Requirement 29.3.
            with pytest.raises(CompletionConflictError) as exc_info:
                with engine.begin() as conn:
                    service.create_completion(
                        conn,
                        target_plan_revision_id=(
                            _APPROVED_PLAN_REVISION_ID
                        ),
                        outcome=second_outcome,  # type: ignore[arg-type]
                        rationale=second_rationale,
                        source_milestone_acceptance_ids=(),
                        completing_party_id=_ACTOR_PARTY_ID,
                        authority_basis=_BASIS,
                        applicable_scope=_SCOPE,
                        engine=engine,
                        correlation_id="prop40-cp-second",
                    )

            assert exc_info.value.failed_constraint == (
                "completion_already_exists"
            )
            assert exc_info.value.target_plan_revision_id == (
                _APPROVED_PLAN_REVISION_ID
            )
            # The caller holds only ``complete`` (not ``view``) over
            # ``_SCOPE``; the AD-WS-9 / Slice 3 Requirement 30.4
            # view-authority gate on the conflict response therefore
            # returns ``None`` for the existing Identity. What the
            # property cares about is that the rejection happened
            # with the structured failed_constraint — the
            # indistinguishable-denial body is the design's default
            # and the unit suite covers the view-permitted branch
            # separately.
            assert exc_info.value.existing_completion_id is None

            # 4. Still exactly one row in ``Completion_Records`` —
            # no second row was persisted.
            assert _count_rows(engine, "Completion_Records") == 1

            # 5. The first Completion row is byte-equivalent before
            # and after the second attempt (Property 40's universal
            # quantifier; Requirement 41.10).
            post_snapshot = _snapshot_completion_rows(engine)
            assert post_snapshot == pre_snapshot
        finally:
            engine.dispose()
