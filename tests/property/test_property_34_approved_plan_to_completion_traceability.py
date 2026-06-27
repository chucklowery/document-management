# Feature: third-walking-slice, Property 34: Approved-Plan-to-Completion traceability
"""Property 34 — Approved-Plan-to-Completion traceability (task 16.4).

**Property 34: Approved-Plan-to-Completion traceability**

*For all* Completion Records finalized in any test session, the
Walking_Slice_System satisfies:

(a) The Completion Record's ``Addresses`` target resolves to a Plan
    Revision whose ``lifecycle_state`` at the Completion Record's
    ``recorded_at`` is ``approved`` (Requirements 23.2, 23.4, 29.1,
    31.1).

(b) At least one Milestone Acceptance Record exists with
    ``outcome = 'Accept'`` whose ``Addresses`` target resolves to a
    produced Deliverable Revision whose
    ``Deliverable_Production_Records`` row's source Work Assignment
    Record targets the same Plan Revision Identity as the Completion
    (Requirements 27.3, 29.1, 41.1).

(c) At least one Work Assignment Record exists whose ``Addresses``
    target equals the Completion Record's target Plan Revision
    Identity (Requirements 23.2, 23.4, 41.1).

No orphan Completion Record exists — that is, every persisted
Completion Record has its Plan Revision + Milestone Acceptance +
Work Assignment chain wired (Requirement 41.1).

**Validates: Requirements 23.2, 23.4, 27.3, 29.1, 31.1, 41.1**

Strategy
========

Each Hypothesis case draws 1..2 *pipeline scenarios*. Each scenario
materialises one approved Plan Revision and one finalised
Completion Record produced *through the* :class:`CompletionService`
*service layer*. Per pipeline the seed seeds:

- One Project, one Activity Plan, one approved Plan Revision (direct
  INSERT — :class:`AD-WS-19` only fires on UPDATE).
- 1..2 Work Assignment Records (direct INSERT + matching
  ``Addresses`` Relationship row to the Plan Revision so the AD-WS-26
  Relationship-row contract is mirrored byte-equivalent).
- One produced Deliverable Resource + Revision (direct INSERT).
- One Deliverable Expectation header + Revision (direct INSERT).
- One Deliverable Production Record per Work Assignment (direct
  INSERT + the three ``Produces`` / ``Addresses`` / ``Relates To``
  Relationship rows per AD-WS-26).
- 1..2 Milestone Acceptance Records per Production with mixed
  ``Accept`` / ``Reject`` outcomes (direct INSERT + matching
  ``Addresses`` Relationship row to the produced Deliverable
  Revision). At least one Acceptance is ``Accept`` so the
  Completion's accepted-Milestone existence check succeeds.
- One ``complete`` role assignment granting the Completion Authority
  Party authority over the pipeline scope.

The Completion itself is created through
:meth:`CompletionService.create_completion` so the production
``Addresses`` Relationship row for the Completion is written by the
real service code path — the property targets the
``Relationships``-graph invariant that the production code produces.

After every pipeline has finalised its Completion, the property
walks the entire ``Completion_Records`` and ``Relationships`` graph
on a *single read transaction* and asserts the three invariants
listed above for every persisted Completion Record.

Hypothesis settings
===================

``@settings(max_examples=100, deadline=2000)`` per task 16's task
notes. ``suppress_health_check`` covers ``too_slow`` and
``data_too_large`` because each case allocates a fresh on-disk
SQLite database carrying Slice 1 + Slice 2 + Slice 3 schemas and
seeds 1..2 complete pipelines (with their Relationship rows) before
running the Completion writes.
"""

from __future__ import annotations

import tempfile
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import (
    CompletionService,
    CreateCompletionResult,
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
# Every persisted row in a case carries the same ``recorded_at``
# (Slice 3 §"Cross-Cutting Concerns — Transactionality"). The
# ``_NOW`` constant anchors the :class:`FixedClock` so the
# Completion Service's recorded time falls strictly after every
# seeded row's ``recorded_at`` and so the ``approved`` Plan Revision
# lifecycle is already in place when the Completion is written.
# ---------------------------------------------------------------------------


_SEED_TS: Final[str] = "2026-01-01T00:00:00.000Z"
_NOW: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)

# Identifier basis for every persisted row's authority-basis column.
_AUTHORITY_BASIS_UUID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-000000b00001"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_UUID
)
_AUTHORITY_BASIS_ID_STR: Final[str] = str(_AUTHORITY_BASIS_UUID)


# Canonical kind strings written into ``Relationships`` rows. The
# property's invariant assertions filter the table by these constants
# so a drift in any production string surfaces here as well.
_KIND_COMPLETION_RECORD: Final[str] = "completion_record"
_KIND_WORK_ASSIGNMENT_RECORD: Final[str] = "work_assignment_record"
_KIND_MILESTONE_ACCEPTANCE_RECORD: Final[str] = "milestone_acceptance_record"
_KIND_DELIVERABLE_PRODUCTION_RECORD: Final[str] = "deliverable_production_record"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"
_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"

_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_PRODUCES: Final[str] = "Produces"
_RELATIONSHIP_TYPE_RELATES_TO: Final[str] = "Relates To"

_SEMANTIC_ROLE_PRODUCTION_SOURCE: Final[str] = "production_source"
_SEMANTIC_ROLE_ASSIGNEE: Final[str] = "assignee"


# ---------------------------------------------------------------------------
# Per-case engine + service factory.
# ---------------------------------------------------------------------------


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string (AD-WS-2)."""
    return str(uuid_utils.uuid7())


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every schema.

    Slice 1 + Slice 2 + Slice 3 (Execution + Deliverable_Repository)
    schemas are layered on the same file in the same order
    :func:`walking_slice.app.create_app` uses, so triggers and FK
    constraints match production.
    """
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover
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


def _build_completion_service(
    *,
    clock: FixedClock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> CompletionService:
    """Wire a real :class:`CompletionService` with read-only Slice 2
    collaborators.

    :class:`PlanRevisionService` and :class:`ProjectResolver` only
    invoke read methods on the supplied connection, so the wiring
    arguments not consulted by those read methods are passed as
    ``None`` for brevity (mirrors the production unit-test fixture
    in ``tests/unit/test_execution_completions.py``).
    """
    return CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=PlanRevisionService(
            clock=None,  # type: ignore[arg-type]
            identity_service=None,  # type: ignore[arg-type]
            audit_log=None,  # type: ignore[arg-type]
            authorization_service=None,  # type: ignore[arg-type]
        ),
        project_resolver=ProjectResolver(),
        denial_audit_sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers (direct INSERT pattern matching
# ``tests/unit/test_execution_completions.py``).
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _SEED_TS},
    )


def _seed_project(conn: Connection, project_id: str) -> None:
    conn.execute(
        text("INSERT INTO Projects (project_id, created_at) VALUES (:pid, :ts)"),
        {"pid": project_id, "ts": _SEED_TS},
    )


def _seed_activity_plan(
    conn: Connection,
    *,
    activity_plan_id: str,
    project_id: str,
    authoring_party_id: str,
    scope: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Activity_Plans (
                activity_plan_id, target_project_id, title,
                authoring_party_id, applicable_scope, recorded_at
            ) VALUES (:aid, :pid, 'Pipeline activities', :party, :scope, :ts)
            """
        ),
        {
            "aid": activity_plan_id,
            "pid": project_id,
            "party": authoring_party_id,
            "scope": scope,
            "ts": _SEED_TS,
        },
    )


def _seed_approved_plan_revision(
    conn: Connection,
    *,
    plan_revision_id: str,
    activity_plan_id: str,
    authoring_party_id: str,
    scope: str,
) -> None:
    """Insert an ``approved`` Plan Revision by direct INSERT.

    The AD-WS-19 lifecycle trigger fires only on UPDATE, so an
    ``approved`` row may be inserted in a single statement without
    going through the Plan Approval transaction.
    """
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
                :rev, :aid, NULL, 'approved', 'Pipeline scope', '[]',
                '[]', NULL, :party, :scope, :ts
            )
            """
        ),
        {
            "rev": plan_revision_id,
            "aid": activity_plan_id,
            "party": authoring_party_id,
            "scope": scope,
            "ts": _SEED_TS,
        },
    )


def _seed_work_assignment(
    conn: Connection,
    *,
    work_assignment_id: str,
    plan_revision_id: str,
    assignee_party_id: str,
    assignment_authority_party_id: str,
    scope: str,
) -> None:
    """Insert one ``Work_Assignment_Records`` row plus its AD-WS-26
    ``Addresses`` Relationship to the Plan Revision.

    AD-WS-26 row 1 names the Work Assignment Record → Plan Revision
    ``Addresses`` relationship with ``semantic_role = NULL`` and
    ``target_revision_id = NULL`` (Plan Revisions live in a single
    Revision-level table per Slice 2). Mirroring the production
    insertion ensures the traceability walk in this property reads
    the same shape that
    :class:`walking_slice.execution.work_assignments.WorkAssignmentService`
    writes.
    """
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
                'Pipeline work assignment', 'role-grant-id',
                :abid, :scope, :ts
            )
            """
        ),
        {
            "wid": work_assignment_id,
            "prev": plan_revision_id,
            "assignee": assignee_party_id,
            "authority": assignment_authority_party_id,
            "abid": _AUTHORITY_BASIS_ID_STR,
            "scope": scope,
            "ts": _SEED_TS,
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
                :rid, :rtype,
                :skind, :sid, NULL,
                :tkind, :tid, NULL,
                :party, :ts, NULL
            )
            """
        ),
        {
            "rid": _new_uuid7(),
            "rtype": _RELATIONSHIP_TYPE_ADDRESSES,
            "skind": _KIND_WORK_ASSIGNMENT_RECORD,
            "sid": work_assignment_id,
            "tkind": _KIND_PLAN_REVISION,
            "tid": plan_revision_id,
            "party": assignment_authority_party_id,
            "ts": _SEED_TS,
        },
    )
    # The AD-WS-26 row 2 (assignee ``Relates To`` Party) is not
    # consulted by Property 34's invariants but is recorded here so
    # the seeded graph matches production-shape byte-for-byte.
    conn.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at, semantic_role
            ) VALUES (
                :rid, :rtype,
                :skind, :sid, NULL,
                'party', :tid, NULL,
                :party, :ts, :semantic_role
            )
            """
        ),
        {
            "rid": _new_uuid7(),
            "rtype": _RELATIONSHIP_TYPE_RELATES_TO,
            "skind": _KIND_WORK_ASSIGNMENT_RECORD,
            "sid": work_assignment_id,
            "tid": assignee_party_id,
            "party": assignment_authority_party_id,
            "ts": _SEED_TS,
            "semantic_role": _SEMANTIC_ROLE_ASSIGNEE,
        },
    )


def _seed_deliverable(
    conn: Connection,
    *,
    deliverable_id: str,
    deliverable_revision_id: str,
    work_assignment_id: str,
    authoring_party_id: str,
) -> None:
    """Insert one Deliverable Resource + Revision pair by direct
    INSERT.

    The Revision carries ``role_marker = 'generated_output'``
    (Requirement 26.2) and ``originating_work_assignment_id`` points
    at the source Work Assignment so the Production Record's FK
    references resolve.
    """
    digest = "a" * 64
    conn.execute(
        text(
            """
            INSERT INTO Deliverable_Resources (
                deliverable_id, produced_deliverable_name, created_at
            ) VALUES (:did, 'Pipeline runbook', :ts)
            """
        ),
        {"did": deliverable_id, "ts": _SEED_TS},
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
            "bytes": b"pipeline-output",
            "digest": digest,
            "wa": work_assignment_id,
            "party": authoring_party_id,
            "ts": _SEED_TS,
        },
    )


def _seed_deliverable_expectation(
    conn: Connection,
    *,
    deliverable_expectation_id: str,
    deliverable_expectation_revision_id: str,
    project_id: str,
    authoring_party_id: str,
    scope: str,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Deliverable_Expectations
                (deliverable_expectation_id, created_at)
            VALUES (:did, :ts)
            """
        ),
        {"did": deliverable_expectation_id, "ts": _SEED_TS},
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
                :rev, :did, NULL, :pid, 'Pipeline runbook',
                NULL, 'Document', NULL, :party, :scope, :ts
            )
            """
        ),
        {
            "rev": deliverable_expectation_revision_id,
            "did": deliverable_expectation_id,
            "pid": project_id,
            "party": authoring_party_id,
            "scope": scope,
            "ts": _SEED_TS,
        },
    )


def _seed_deliverable_production(
    conn: Connection,
    *,
    deliverable_production_id: str,
    work_assignment_id: str,
    deliverable_id: str,
    deliverable_revision_id: str,
    deliverable_expectation_id: str,
    deliverable_expectation_revision_id: str,
    recording_party_id: str,
    scope: str,
) -> None:
    """Insert one ``Deliverable_Production_Records`` row plus its
    three AD-WS-26 Relationship rows.

    AD-WS-26 rows for Deliverable Production:

    - ``Produces`` → produced Deliverable Revision
      (``semantic_role = NULL``).
    - ``Addresses`` → target Deliverable Expectation Revision
      (``semantic_role = NULL``).
    - ``Relates To`` → source Work Assignment
      (``semantic_role = 'production_source'``).
    """
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
                'Pipeline production.', :party, 'role-grant-id',
                :abid, :scope, :ts
            )
            """
        ),
        {
            "pid": deliverable_production_id,
            "wa": work_assignment_id,
            "did": deliverable_id,
            "rev": deliverable_revision_id,
            "exp_did": deliverable_expectation_id,
            "exp_rev": deliverable_expectation_revision_id,
            "party": recording_party_id,
            "abid": _AUTHORITY_BASIS_ID_STR,
            "scope": scope,
            "ts": _SEED_TS,
        },
    )
    # Produces → produced Deliverable Revision.
    conn.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at, semantic_role
            ) VALUES (
                :rid, :rtype,
                :skind, :sid, NULL,
                :tkind, :tid, :trev,
                :party, :ts, NULL
            )
            """
        ),
        {
            "rid": _new_uuid7(),
            "rtype": _RELATIONSHIP_TYPE_PRODUCES,
            "skind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
            "sid": deliverable_production_id,
            "tkind": _KIND_DELIVERABLE_REVISION,
            "tid": deliverable_id,
            "trev": deliverable_revision_id,
            "party": recording_party_id,
            "ts": _SEED_TS,
        },
    )
    # Addresses → target Deliverable Expectation Revision.
    conn.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at, semantic_role
            ) VALUES (
                :rid, :rtype,
                :skind, :sid, NULL,
                'deliverable_expectation_revision', :tid, :trev,
                :party, :ts, NULL
            )
            """
        ),
        {
            "rid": _new_uuid7(),
            "rtype": _RELATIONSHIP_TYPE_ADDRESSES,
            "skind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
            "sid": deliverable_production_id,
            "tid": deliverable_expectation_id,
            "trev": deliverable_expectation_revision_id,
            "party": recording_party_id,
            "ts": _SEED_TS,
        },
    )
    # Relates To → source Work Assignment.
    conn.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at, semantic_role
            ) VALUES (
                :rid, :rtype,
                :skind, :sid, NULL,
                :tkind, :tid, NULL,
                :party, :ts, :semantic_role
            )
            """
        ),
        {
            "rid": _new_uuid7(),
            "rtype": _RELATIONSHIP_TYPE_RELATES_TO,
            "skind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
            "sid": deliverable_production_id,
            "tkind": _KIND_WORK_ASSIGNMENT_RECORD,
            "tid": work_assignment_id,
            "party": recording_party_id,
            "ts": _SEED_TS,
            "semantic_role": _SEMANTIC_ROLE_PRODUCTION_SOURCE,
        },
    )


def _seed_milestone_acceptance(
    conn: Connection,
    *,
    milestone_acceptance_id: str,
    deliverable_production_id: str,
    deliverable_id: str,
    deliverable_revision_id: str,
    deliverable_expectation_id: str,
    deliverable_expectation_revision_id: str,
    outcome: str,
    accepting_party_id: str,
    scope: str,
) -> None:
    """Insert one ``Milestone_Acceptance_Records`` row plus its
    AD-WS-26 ``Addresses`` Relationship to the produced Deliverable
    Revision.

    AD-WS-26 names ``semantic_role = NULL`` for this row and writes
    both ``target_id`` (the Deliverable Resource Identity) and
    ``target_revision_id`` (the produced Deliverable Revision
    Identity) — Revision-scoped ``Addresses`` rows carry both
    columns.
    """
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
                :outcome, 'Pipeline acceptance.', :party,
                'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "mid": milestone_acceptance_id,
            "pid": deliverable_production_id,
            "did": deliverable_id,
            "rev": deliverable_revision_id,
            "exp_did": deliverable_expectation_id,
            "exp_rev": deliverable_expectation_revision_id,
            "outcome": outcome,
            "party": accepting_party_id,
            "abid": _AUTHORITY_BASIS_ID_STR,
            "scope": scope,
            "ts": _SEED_TS,
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
                :rid, :rtype,
                :skind, :sid, NULL,
                :tkind, :tid, :trev,
                :party, :ts, NULL
            )
            """
        ),
        {
            "rid": _new_uuid7(),
            "rtype": _RELATIONSHIP_TYPE_ADDRESSES,
            "skind": _KIND_MILESTONE_ACCEPTANCE_RECORD,
            "sid": milestone_acceptance_id,
            "tkind": _KIND_DELIVERABLE_REVISION,
            "tid": deliverable_id,
            "trev": deliverable_revision_id,
            "party": accepting_party_id,
            "ts": _SEED_TS,
        },
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


@st.composite
def _pipeline_strategy(draw) -> dict[str, Any]:
    """Draw one full pipeline scenario.

    A scenario carries the number of Work Assignment Records per
    Plan Revision (1..2) and the Completion outcome. The Production
    + Milestone Acceptance shape is fixed at one Production per
    pipeline with one ``Accept`` Milestone Acceptance — the design
    caps Acceptances at one per Production via the
    ``UNIQUE(source_deliverable_production_id)`` constraint
    (Requirement 28.3), and the Completion Service requires at
    least one ``Accept`` Milestone for the Plan Revision (Requirement
    29.1). Additional Work Assignments are seeded with their
    AD-WS-26 ``Addresses`` Relationship row but no Production so
    invariant (c) is exercised against multi-WA pipelines as well as
    single-WA ones.
    """
    work_assignment_count = draw(st.integers(min_value=1, max_value=2))
    completion_outcome = draw(
        st.sampled_from(["Completed", "Completed_With_Reservation"])
    )
    return {
        "work_assignment_count": work_assignment_count,
        "completion_outcome": completion_outcome,
    }


# 1..2 pipelines per case. Two pipelines exercise the multi-Completion
# graph (the property must isolate every Completion's trace from every
# other) while a single pipeline shrinks to a minimal counterexample
# if one breaks.
_pipeline_scenarios = st.lists(
    _pipeline_strategy(), min_size=1, max_size=2
)


# ---------------------------------------------------------------------------
# Per-pipeline build state.
# ---------------------------------------------------------------------------


def _assign_complete_role(
    *,
    authorization_service: AuthorizationService,
    engine: Engine,
    completing_party_id: str,
    assigning_party_id: str,
    scope: str,
) -> None:
    """Grant the Completion Authority Party the ``complete`` authority.

    The Completion Service rejects unauthorized callers via AD-WS-9;
    seeding the role assignment is the bare minimum required to put
    the service on its happy path for every pipeline.
    """
    request = AssignRoleRequest(
        party_id=completing_party_id,
        role_name="completion_authority",
        scope=scope,
        authorities_granted=("complete",),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=None,
        assigning_authority_id=assigning_party_id,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 34: Approved-Plan-to-Completion traceability
@given(scenarios=_pipeline_scenarios)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_approved_plan_to_completion_traceability(
    scenarios: list[dict[str, Any]],
) -> None:
    """Every persisted Completion Record satisfies the three Property 34
    invariants and no orphan Completion Record exists.

    Validates Requirements 23.2, 23.4, 27.3, 29.1, 31.1, 41.1.
    """
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop34_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            clock = FixedClock(_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
            authorization_service = AuthorizationService(
                clock=clock,
                audit_log=audit_log,
                identity_service=identity_service,
            )
            completion_service = _build_completion_service(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            # Per-pipeline expectations, indexed by the persisted
            # Completion Identity. Used to check the inverse
            # direction of (a) (b) (c) — namely that every Completion
            # we *created* shows up in the graph walk with the
            # invariants satisfied.
            expected: dict[str, dict[str, Any]] = {}

            # ----- Seed every pipeline ----------------------------------
            for pipeline_index, scenario in enumerate(scenarios):
                scope = f"prop34/pipeline-{pipeline_index}"

                # Per-pipeline Party identities. A fresh set per
                # pipeline keeps the seeded graph easy to reason
                # about and avoids cross-pipeline role-assignment
                # spill.
                completing_party_id = _new_uuid7()
                contributor_party_id = _new_uuid7()
                assignment_authority_id = _new_uuid7()
                assigning_authority_id = _new_uuid7()

                project_id = _new_uuid7()
                activity_plan_id = _new_uuid7()
                plan_revision_id = _new_uuid7()
                deliverable_id = _new_uuid7()
                deliverable_revision_id = _new_uuid7()
                deliverable_expectation_id = _new_uuid7()
                deliverable_expectation_revision_id = _new_uuid7()

                work_assignment_ids: list[str] = []
                production_id: str = ""
                accepted_milestone_ids: list[str] = []

                # The Deliverable Revision row carries an FK on
                # ``originating_work_assignment_id`` so the primary
                # Work Assignment Identity is minted up-front and
                # the seed order is: Parties → Project / Activity
                # Plan / Plan Revision → Work Assignments →
                # Deliverable (which can now reference the primary
                # Work Assignment) → Deliverable Expectation →
                # Deliverable Production → Milestone Acceptance(s).
                # Only the first Work Assignment owns the Production
                # + Milestone Acceptance chain; any additional Work
                # Assignments are seeded with their AD-WS-26
                # ``Addresses`` Relationship to the Plan Revision so
                # invariant (c) is exercised against multi-WA
                # pipelines as well as single-WA ones.
                primary_wa_id = _new_uuid7()
                work_assignment_ids.append(primary_wa_id)
                for _ in range(1, scenario["work_assignment_count"]):
                    work_assignment_ids.append(_new_uuid7())

                with engine.begin() as conn:
                    _seed_party(
                        conn, completing_party_id, "Completion Authority"
                    )
                    _seed_party(
                        conn, contributor_party_id, "Contributor"
                    )
                    _seed_party(
                        conn,
                        assignment_authority_id,
                        "Assignment Authority",
                    )
                    _seed_party(
                        conn,
                        assigning_authority_id,
                        "Resource Steward",
                    )
                    _seed_project(conn, project_id)
                    _seed_activity_plan(
                        conn,
                        activity_plan_id=activity_plan_id,
                        project_id=project_id,
                        authoring_party_id=assignment_authority_id,
                        scope=scope,
                    )
                    _seed_approved_plan_revision(
                        conn,
                        plan_revision_id=plan_revision_id,
                        activity_plan_id=activity_plan_id,
                        authoring_party_id=assignment_authority_id,
                        scope=scope,
                    )
                    for wa_id in work_assignment_ids:
                        _seed_work_assignment(
                            conn,
                            work_assignment_id=wa_id,
                            plan_revision_id=plan_revision_id,
                            assignee_party_id=contributor_party_id,
                            assignment_authority_party_id=(
                                assignment_authority_id
                            ),
                            scope=scope,
                        )
                    _seed_deliverable(
                        conn,
                        deliverable_id=deliverable_id,
                        deliverable_revision_id=deliverable_revision_id,
                        work_assignment_id=primary_wa_id,
                        authoring_party_id=contributor_party_id,
                    )
                    _seed_deliverable_expectation(
                        conn,
                        deliverable_expectation_id=(
                            deliverable_expectation_id
                        ),
                        deliverable_expectation_revision_id=(
                            deliverable_expectation_revision_id
                        ),
                        project_id=project_id,
                        authoring_party_id=assignment_authority_id,
                        scope=scope,
                    )

                    production_id = _new_uuid7()
                    _seed_deliverable_production(
                        conn,
                        deliverable_production_id=production_id,
                        work_assignment_id=primary_wa_id,
                        deliverable_id=deliverable_id,
                        deliverable_revision_id=(
                            deliverable_revision_id
                        ),
                        deliverable_expectation_id=(
                            deliverable_expectation_id
                        ),
                        deliverable_expectation_revision_id=(
                            deliverable_expectation_revision_id
                        ),
                        recording_party_id=contributor_party_id,
                        scope=scope,
                    )

                    # Each Production has at most one Milestone
                    # Acceptance (Slice 3 Requirement 28.3 — UNIQUE
                    # constraint on
                    # ``source_deliverable_production_id``). The
                    # Completion Service additionally requires at
                    # least one ``Accept`` Milestone Acceptance per
                    # target Plan Revision (Requirement 29.1), so
                    # one ``Accept`` row is seeded per pipeline. The
                    # Identity is captured for the no-orphan
                    # inverse-direction check.
                    milestone_id = _new_uuid7()
                    _seed_milestone_acceptance(
                        conn,
                        milestone_acceptance_id=milestone_id,
                        deliverable_production_id=production_id,
                        deliverable_id=deliverable_id,
                        deliverable_revision_id=(
                            deliverable_revision_id
                        ),
                        deliverable_expectation_id=(
                            deliverable_expectation_id
                        ),
                        deliverable_expectation_revision_id=(
                            deliverable_expectation_revision_id
                        ),
                        outcome="Accept",
                        accepting_party_id=completing_party_id,
                        scope=scope,
                    )
                    accepted_milestone_ids.append(milestone_id)

                # ----- Grant ``complete`` and create the Completion
                # via the service layer -----------------------------
                _assign_complete_role(
                    authorization_service=authorization_service,
                    engine=engine,
                    completing_party_id=completing_party_id,
                    assigning_party_id=assigning_authority_id,
                    scope=scope,
                )

                with engine.begin() as conn:
                    completion_result: CreateCompletionResult = (
                        completion_service.create_completion(
                            conn,
                            target_plan_revision_id=plan_revision_id,
                            outcome=scenario["completion_outcome"],
                            rationale=(
                                f"Pipeline {pipeline_index} completed."
                            ),
                            source_milestone_acceptance_ids=(
                                tuple(accepted_milestone_ids)
                            ),
                            completing_party_id=completing_party_id,
                            authority_basis=_AUTHORITY_BASIS,
                            applicable_scope=scope,
                            engine=engine,
                        )
                    )

                expected[completion_result.completion_id] = {
                    "plan_revision_id": plan_revision_id,
                    "work_assignment_ids": tuple(work_assignment_ids),
                    "deliverable_id": deliverable_id,
                    "deliverable_revision_id": deliverable_revision_id,
                    "deliverable_production_id": production_id,
                    "accepted_milestone_ids": (
                        tuple(accepted_milestone_ids)
                    ),
                }

            # ----- Verify Property 34 invariants on the persisted
            # graph -------------------------------------------------
            with engine.connect() as conn:
                persisted_completions = _load_completions(conn)
                # Every Completion we created must show up in the
                # walk, and the walk must not surface anything
                # extra.
                assert set(persisted_completions) == set(expected), (
                    "Property 34 graph-walk completion set differs "
                    f"from the seeded set; persisted="
                    f"{sorted(persisted_completions)}, "
                    f"expected={sorted(expected)}."
                )

                for completion_id, completion_row in (
                    persisted_completions.items()
                ):
                    _assert_invariant_a(conn, completion_row)
                    _assert_invariant_b(conn, completion_row)
                    _assert_invariant_c(conn, completion_row)
                    _assert_no_orphan(
                        conn,
                        completion_row,
                        expected[completion_id],
                    )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Graph walkers and assertion helpers.
# ---------------------------------------------------------------------------


def _load_completions(conn: Connection) -> dict[str, dict[str, Any]]:
    """Return every ``Completion_Records`` row keyed by Completion
    Identity.

    The walk reads ``completion_id``, ``target_plan_revision_id``,
    and ``recorded_at`` because invariant (a) compares the Plan
    Revision's lifecycle state at the Completion's recorded time and
    invariants (b) and (c) join on the Plan Revision Identity.
    """
    rows = conn.execute(
        text(
            "SELECT completion_id, target_plan_revision_id, recorded_at "
            "FROM Completion_Records"
        )
    ).mappings().all()
    return {row["completion_id"]: dict(row) for row in rows}


def _assert_invariant_a(
    conn: Connection, completion_row: dict[str, Any]
) -> None:
    """Invariant (a): the Completion's ``Addresses`` target resolves
    to a Plan Revision whose ``lifecycle_state`` at the Completion
    Record's ``recorded_at`` is ``approved``.

    The walk goes ``Completion_Records.completion_id`` →
    ``Relationships`` (with ``relationship_type='Addresses'``,
    ``source_kind='completion_record'``, ``target_kind='plan_revision'``)
    → ``Plan_Revisions.lifecycle_state``. Plan Revisions live in a
    single Revision-level table (Slice 2 Requirement 7), so the
    ``target_revision_id`` column is NULL on this row and the join
    is purely by ``target_id``.
    """
    completion_id = completion_row["completion_id"]
    plan_revision_id = completion_row["target_plan_revision_id"]
    completion_recorded_at = completion_row["recorded_at"]

    rel_rows = conn.execute(
        text(
            """
            SELECT target_id, target_revision_id, semantic_role
              FROM Relationships
             WHERE source_kind = :skind
               AND source_id   = :sid
               AND relationship_type = :rtype
               AND target_kind = :tkind
            """
        ),
        {
            "skind": _KIND_COMPLETION_RECORD,
            "sid": completion_id,
            "rtype": _RELATIONSHIP_TYPE_ADDRESSES,
            "tkind": _KIND_PLAN_REVISION,
        },
    ).mappings().all()

    assert len(rel_rows) == 1, (
        f"Property 34 invariant (a) violated on completion_id="
        f"{completion_id!r}: expected exactly one Addresses "
        "Relationship to a Plan Revision, got "
        f"{len(rel_rows)} (Requirement 29.2 / AD-WS-26)."
    )
    rel = rel_rows[0]
    assert rel["target_id"] == plan_revision_id, (
        f"Property 34 invariant (a) violated on completion_id="
        f"{completion_id!r}: Addresses target_id "
        f"{rel['target_id']!r} does not match "
        f"Completion_Records.target_plan_revision_id "
        f"{plan_revision_id!r}."
    )
    assert rel["target_revision_id"] is None, (
        f"Property 34 invariant (a) violated on completion_id="
        f"{completion_id!r}: Addresses target_revision_id is "
        "non-NULL but Plan Revisions live in a single "
        "Revision-level table per Slice 2; AD-WS-26 names "
        f"NULL here (got {rel['target_revision_id']!r})."
    )

    pr_row = conn.execute(
        text(
            "SELECT lifecycle_state, recorded_at "
            "FROM Plan_Revisions WHERE plan_revision_id = :pid"
        ),
        {"pid": plan_revision_id},
    ).mappings().first()
    assert pr_row is not None, (
        f"Property 34 invariant (a) violated on completion_id="
        f"{completion_id!r}: target_plan_revision_id "
        f"{plan_revision_id!r} does not resolve to a "
        "Plan_Revisions row (Requirement 23.2 / 29.4)."
    )
    assert pr_row["lifecycle_state"] == "approved", (
        f"Property 34 invariant (a) violated on completion_id="
        f"{completion_id!r}: Plan_Revisions.lifecycle_state="
        f"{pr_row['lifecycle_state']!r}; Requirement 23.4 / 29.4 "
        "require 'approved'."
    )
    # The Plan Revision is append-only (Slice 2 AD-WS-19 transition
    # to approved is a one-way INSERT for the seed shape used here),
    # so its persisted ``recorded_at`` is the only observable
    # lifecycle anchor. Assert it lies at-or-before the Completion's
    # recorded time so the "at the Completion Record's recorded_at"
    # qualifier in the property statement is satisfied.
    assert pr_row["recorded_at"] <= completion_recorded_at, (
        f"Property 34 invariant (a) violated on completion_id="
        f"{completion_id!r}: Plan_Revisions.recorded_at="
        f"{pr_row['recorded_at']!r} is later than "
        f"Completion_Records.recorded_at={completion_recorded_at!r}, "
        "so the Plan Revision could not have been 'approved' at "
        "Completion time."
    )


def _assert_invariant_b(
    conn: Connection, completion_row: dict[str, Any]
) -> None:
    """Invariant (b): at least one Milestone Acceptance Record with
    ``outcome = 'Accept'`` has its ``Addresses`` target resolving to
    a produced Deliverable Revision whose Production Record's source
    Work Assignment Record targets the same Plan Revision Identity
    as the Completion.

    The walk joins:

        Completion (plan_revision)
          → Work_Assignment_Records.target_plan_revision_id
          → Deliverable_Production_Records.source_work_assignment_id
          → Milestone_Acceptance_Records.source_deliverable_production_id
            with outcome='Accept'
          and crosschecks each accepted Milestone's
          ``Addresses`` Relationship row targets the same
          ``produced_deliverable_revision_id``.
    """
    completion_id = completion_row["completion_id"]
    plan_revision_id = completion_row["target_plan_revision_id"]

    accept_rows = conn.execute(
        text(
            """
            SELECT mar.milestone_acceptance_id,
                   mar.produced_deliverable_id,
                   mar.produced_deliverable_revision_id,
                   dpr.deliverable_production_id,
                   dpr.source_work_assignment_id,
                   wa.target_plan_revision_id
              FROM Milestone_Acceptance_Records AS mar
              JOIN Deliverable_Production_Records AS dpr
                ON mar.source_deliverable_production_id =
                   dpr.deliverable_production_id
              JOIN Work_Assignment_Records AS wa
                ON dpr.source_work_assignment_id =
                   wa.work_assignment_id
             WHERE wa.target_plan_revision_id = :prev
               AND mar.outcome = 'Accept'
            """
        ),
        {"prev": plan_revision_id},
    ).mappings().all()

    assert len(accept_rows) >= 1, (
        f"Property 34 invariant (b) violated on completion_id="
        f"{completion_id!r}: zero Accept-outcome Milestone "
        "Acceptance Records reachable via Production → Work "
        "Assignment → target_plan_revision_id="
        f"{plan_revision_id!r}; Requirement 29.1 requires at least "
        "one (the Completion Service's accepted-Milestone existence "
        "check should have rejected this Completion at creation "
        "time)."
    )

    for accept_row in accept_rows:
        # Crosscheck the ``Addresses`` Relationship row written by
        # the Milestone Acceptance Service (AD-WS-26 row 7) so the
        # property fails if a future change loses, mis-targets, or
        # mis-labels the row.
        addr_rows = conn.execute(
            text(
                """
                SELECT target_kind, target_id, target_revision_id
                  FROM Relationships
                 WHERE source_kind = :skind
                   AND source_id   = :sid
                   AND relationship_type = :rtype
                """
            ),
            {
                "skind": _KIND_MILESTONE_ACCEPTANCE_RECORD,
                "sid": accept_row["milestone_acceptance_id"],
                "rtype": _RELATIONSHIP_TYPE_ADDRESSES,
            },
        ).mappings().all()
        assert len(addr_rows) == 1, (
            f"Property 34 invariant (b) violated on completion_id="
            f"{completion_id!r}: Milestone Acceptance "
            f"{accept_row['milestone_acceptance_id']!r} carries "
            f"{len(addr_rows)} Addresses Relationship rows; "
            "AD-WS-26 requires exactly one."
        )
        addr = addr_rows[0]
        assert addr["target_kind"] == _KIND_DELIVERABLE_REVISION, (
            f"Property 34 invariant (b) violated on completion_id="
            f"{completion_id!r}: Milestone Acceptance "
            f"{accept_row['milestone_acceptance_id']!r} Addresses "
            f"target_kind={addr['target_kind']!r}; AD-WS-26 "
            "requires 'deliverable_revision'."
        )
        assert addr["target_id"] == accept_row["produced_deliverable_id"], (
            f"Property 34 invariant (b) violated on completion_id="
            f"{completion_id!r}: Milestone Acceptance "
            f"{accept_row['milestone_acceptance_id']!r} Addresses "
            f"target_id={addr['target_id']!r} does not match the "
            "produced Deliverable Resource Identity "
            f"{accept_row['produced_deliverable_id']!r}."
        )
        assert (
            addr["target_revision_id"]
            == accept_row["produced_deliverable_revision_id"]
        ), (
            f"Property 34 invariant (b) violated on completion_id="
            f"{completion_id!r}: Milestone Acceptance "
            f"{accept_row['milestone_acceptance_id']!r} Addresses "
            f"target_revision_id={addr['target_revision_id']!r} "
            "does not match the produced Deliverable Revision "
            "Identity "
            f"{accept_row['produced_deliverable_revision_id']!r}."
        )


def _assert_invariant_c(
    conn: Connection, completion_row: dict[str, Any]
) -> None:
    """Invariant (c): at least one Work Assignment Record exists whose
    ``Addresses`` target equals the Completion's target Plan Revision
    Identity.

    The walk filters ``Relationships`` by ``source_kind=
    'work_assignment_record'``, ``relationship_type='Addresses'``,
    ``target_kind='plan_revision'``, and the Completion's Plan
    Revision Identity. Every match is crosschecked against the
    backing ``Work_Assignment_Records.target_plan_revision_id``
    column so a Relationship-row drift surfaces here.
    """
    completion_id = completion_row["completion_id"]
    plan_revision_id = completion_row["target_plan_revision_id"]

    wa_rows = conn.execute(
        text(
            """
            SELECT r.source_id AS work_assignment_id,
                   wa.target_plan_revision_id
              FROM Relationships AS r
              JOIN Work_Assignment_Records AS wa
                ON r.source_id = wa.work_assignment_id
             WHERE r.source_kind = :skind
               AND r.relationship_type = :rtype
               AND r.target_kind = :tkind
               AND r.target_id   = :tid
            """
        ),
        {
            "skind": _KIND_WORK_ASSIGNMENT_RECORD,
            "rtype": _RELATIONSHIP_TYPE_ADDRESSES,
            "tkind": _KIND_PLAN_REVISION,
            "tid": plan_revision_id,
        },
    ).mappings().all()

    assert len(wa_rows) >= 1, (
        f"Property 34 invariant (c) violated on completion_id="
        f"{completion_id!r}: zero Work Assignment Records carry an "
        "Addresses Relationship to target_plan_revision_id="
        f"{plan_revision_id!r}; Requirement 23.2 / 23.4 require at "
        "least one."
    )
    for wa_row in wa_rows:
        assert (
            wa_row["target_plan_revision_id"] == plan_revision_id
        ), (
            f"Property 34 invariant (c) violated on completion_id="
            f"{completion_id!r}: Work Assignment "
            f"{wa_row['work_assignment_id']!r} carries an "
            "Addresses Relationship to "
            f"{plan_revision_id!r} but its "
            "Work_Assignment_Records.target_plan_revision_id is "
            f"{wa_row['target_plan_revision_id']!r}; the "
            "Relationship row and the backing column have drifted."
        )


def _assert_no_orphan(
    conn: Connection,
    completion_row: dict[str, Any],
    expected_for_completion: dict[str, Any],
) -> None:
    """Inverse-direction "no orphan" check.

    Property 34 names "no orphan Completion Record" — every persisted
    Completion has its Plan Revision + Milestone Acceptance + Work
    Assignment chain wired. Invariants (a) (b) (c) above assert the
    forward direction. This helper additionally asserts the *seeded*
    Plan Revision, accepted Milestone Acceptance Identities, and
    Work Assignment Identities are all surfaced by the graph walk so
    a future refactor that quietly drops one cannot pass the test.
    """
    completion_id = completion_row["completion_id"]
    plan_revision_id = completion_row["target_plan_revision_id"]

    assert (
        plan_revision_id == expected_for_completion["plan_revision_id"]
    ), (
        f"Property 34 no-orphan check on completion_id="
        f"{completion_id!r}: persisted target_plan_revision_id "
        f"{plan_revision_id!r} differs from seeded "
        f"{expected_for_completion['plan_revision_id']!r}."
    )

    # Every seeded accepted Milestone Acceptance must be reachable
    # via the Production → Work Assignment → Plan Revision join.
    persisted_accepted_ids = {
        row["milestone_acceptance_id"]
        for row in conn.execute(
            text(
                """
                SELECT mar.milestone_acceptance_id
                  FROM Milestone_Acceptance_Records AS mar
                  JOIN Deliverable_Production_Records AS dpr
                    ON mar.source_deliverable_production_id =
                       dpr.deliverable_production_id
                  JOIN Work_Assignment_Records AS wa
                    ON dpr.source_work_assignment_id =
                       wa.work_assignment_id
                 WHERE wa.target_plan_revision_id = :prev
                   AND mar.outcome = 'Accept'
                """
            ),
            {"prev": plan_revision_id},
        ).mappings().all()
    }
    expected_accepted = set(
        expected_for_completion["accepted_milestone_ids"]
    )
    assert expected_accepted.issubset(persisted_accepted_ids), (
        f"Property 34 no-orphan check on completion_id="
        f"{completion_id!r}: seeded accepted Milestone Acceptances "
        f"{sorted(expected_accepted)} are not all reachable via the "
        "Production → Work Assignment → Plan Revision walk "
        f"(reachable={sorted(persisted_accepted_ids)})."
    )

    # Every seeded Work Assignment must surface from the Plan
    # Revision → Work Assignment walk.
    persisted_wa_ids = {
        row["work_assignment_id"]
        for row in conn.execute(
            text(
                """
                SELECT work_assignment_id
                  FROM Work_Assignment_Records
                 WHERE target_plan_revision_id = :prev
                """
            ),
            {"prev": plan_revision_id},
        ).mappings().all()
    }
    expected_wa = set(expected_for_completion["work_assignment_ids"])
    assert expected_wa.issubset(persisted_wa_ids), (
        f"Property 34 no-orphan check on completion_id="
        f"{completion_id!r}: seeded Work Assignment Identities "
        f"{sorted(expected_wa)} are not all reachable via "
        "Work_Assignment_Records.target_plan_revision_id="
        f"{plan_revision_id!r} "
        f"(reachable={sorted(persisted_wa_ids)})."
    )
