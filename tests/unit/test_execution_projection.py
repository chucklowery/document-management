"""Unit tests for :mod:`walking_slice.execution._projection` (task 13.2).

Scope (per ``.kiro/specs/third-walking-slice/tasks.md`` §13.2):

    Cover: each projected status value at the appropriate pipeline
    stage; envelope carries every required field; unresolvable-
    definition path withholds status and returns the explanation-
    unavailable indicator; source Records remain byte-equivalent
    when a correction arrives; absence of prohibited derived fields
    in the response body.

    _Requirements: 39.1, 39.2, 39.3, 39.4, 39.5, 39.6_

These tests are the deterministic, example-driven counterpart to
the property-based test for Requirement 39 (task 16.14 / Property
44). They drive
:func:`walking_slice.execution._projection.project_execution_status`
against a per-test SQLite database seeded with the minimum Slice 1
/ Slice 2 / Slice 3 rows required to reach each branch of the
seven-step Projection Definition from
``.kiro/specs/third-walking-slice/design.md`` §"Execution-status
Projection".

Covered behaviors
=================

- **39.1 — each projected status value at the appropriate pipeline
  stage.** :class:`TestProjectedStatusByPipelineStage` exercises
  every status string declared in :data:`EXECUTION_PROJECTED_STATUSES`
  by seeding the precise pipeline-stage shape that should produce
  that status:

  * ``Plan Revision approved`` — no Work Assignment, or Work
    Assignment present but zero Work Events.
  * ``Plan Revision in execution`` — at least one ``started`` Work
    Event, no later Production.
  * ``Plan Revision execution paused`` — every Work Assignment has
    Work Events and every most-recent event is ``paused``.
  * ``Plan Revision deliverable produced`` — at least one
    Deliverable Production Record sourced from a Work Assignment
    targeting the Plan Revision.
  * ``Plan Revision milestone accepted`` — at least one
    ``Accept``-outcome Milestone Acceptance against a Production
    sourced from a Work Assignment targeting the Plan Revision.
  * ``Plan Revision completion recorded`` — a Completion Record
    targets the Plan Revision.
  * ``Provenance incomplete`` — required source Record (Plan
    Revision itself) cannot be resolved, or the Plan Revision is
    not approved (design step 1).

- **39.1, 39.2 — envelope carries every required field.**
  :class:`TestEnvelopeContents` parametrically asserts that every
  pipeline stage and every status produces a
  :class:`ProjectionEnvelope` carrying the Projection Definition,
  source Resource Identities, source Revision Identities, applicable
  temporal boundary (ISO-8601 second precision, UTC), generated time
  (ISO-8601 second precision, UTC), and the fixed ``"derived"``
  derivation indicator.

- **39.5 — unresolvable-definition path withholds status.**
  :class:`TestUnresolvableProjectionDefinition` constructs a
  :class:`StatusProjector` with an empty registry and asserts the
  helper returns an :class:`ExplanationUnavailableResponse`
  identifying the missing Projection Definition. No envelope and no
  projected status are surfaced.

- **39.4 — source Records remain byte-equivalent.**
  :class:`TestSourceRecordByteEquivalence` snapshots the Slice 1 /
  Slice 2 / Slice 3 row state before each
  :func:`project_execution_status` invocation and confirms every
  consulted row remains bit-for-bit identical afterward (including
  across a correction cycle where a new Slice 3 Record arrives and
  the projection re-runs).

- **39.3, 39.6 — absence of prohibited derived fields.**
  :class:`TestProhibitedDerivedFields` introspects the
  :class:`ExecutionStatusProjection` dataclass and the
  :class:`ProjectionEnvelope` Pydantic model and asserts that no
  field name matches any of the six prohibited derived metrics
  (Requirement 39.3) or any observed-outcome alias (Requirement
  39.6). A future regression that adds, for example, a
  ``percent_complete`` attribute to the response shape would trip
  these tests before it reaches the HTTP layer.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from datetime import datetime, timezone
from typing import Iterator, Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution._projection import (
    EXECUTION_PROJECTED_STATUSES,
    EXECUTION_PROJECTION_DEFINITION,
    EXECUTION_PROJECTION_DEFINITION_NAME,
    EXECUTION_PROJECTION_DEFINITION_VERSION,
    EXECUTION_STATUS_APPROVED,
    EXECUTION_STATUS_COMPLETION_RECORDED,
    EXECUTION_STATUS_DELIVERABLE_PRODUCED,
    EXECUTION_STATUS_EXECUTION_PAUSED,
    EXECUTION_STATUS_IN_EXECUTION,
    EXECUTION_STATUS_MILESTONE_ACCEPTED,
    EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
    ExecutionStatusProjection,
    execution_projection_registry,
    project_execution_status,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
    StatusProjector,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers and seed values.
#
# Every Slice 2 / Slice 3 identifier is a canonical UUIDv7 hex string;
# fixed values keep the test fully deterministic so a regression points
# at the exact row that was misread.
# ---------------------------------------------------------------------------


# Party Identities — only two are required: an authoring/owning Party
# (recorded on every seeded row) and a separate query-Party that
# requests the projection. Disclosure enforcement lives at the HTTP
# layer (task 15.1) so the projection helper itself is party-agnostic;
# the value flows through unchanged.
_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_QUERY_PARTY_ID = "00000000-0000-7000-8000-000000a00002"

_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"

# Plan Revision Identities covering each Requirement 39 branch.
_APPROVED_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00030"
_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00031"
_UNRESOLVABLE_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000deadbe01"

# Slice 3 Identities seeded as the pipeline progresses.
_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00001"
_SECOND_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00002"

_WORK_EVENT_STARTED_ID = "00000000-0000-7000-8000-000000e00001"
_WORK_EVENT_PAUSED_ID = "00000000-0000-7000-8000-000000e00002"
_SECOND_WORK_EVENT_STARTED_ID = "00000000-0000-7000-8000-000000e00003"
_SECOND_WORK_EVENT_PAUSED_ID = "00000000-0000-7000-8000-000000e00004"

_DELIVERABLE_ID = "00000000-0000-7000-8000-000000f00001"
_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000f00002"
_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-000000f00003"
_DELIVERABLE_EXPECTATION_REVISION_ID = "00000000-0000-7000-8000-000000f00004"

_DELIVERABLE_PRODUCTION_ID = "00000000-0000-7000-8000-00000d000a01"
_ACCEPT_MILESTONE_ID = "00000000-0000-7000-8000-00000d000b01"
_COMPLETION_ID = "00000000-0000-7000-8000-00000d000c01"

_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-000000b00001"
_SCOPE = "pilot/team-a"

# Timestamps — Slice 3 stores ISO-8601 strings with millisecond
# precision in ``recorded_at`` columns. Distinct values for the
# ``started`` / ``paused`` events let the most-recent lookup return
# the paused row deterministically.
_TS_FIRST = "2026-01-01T00:00:00.000Z"
_TS_SECOND = "2026-01-01T00:01:00.000Z"
_TS_THIRD = "2026-01-01T00:02:00.000Z"

# Applicable temporal boundary the producer passes through to the
# envelope. Already at second precision so the envelope validator
# accepts the value directly.
_BOUNDARY = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# Clock instant fixed to a *different* second so a test can confirm
# the envelope's ``generated_at`` is sourced from the clock rather
# than from the boundary.
_CLOCK_INSTANT = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def projection_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas.

    The projection helper crosses three schemas:

    * Slice 1 (``Parties``) — the ``Work_Assignment_Records`` FK on
      ``assignee_party_id`` resolves against it.
    * Slice 2 (``Projects``, ``Activity_Plans``, ``Plan_Revisions``) —
      the projection's AD-WS-30 read API
      (:meth:`PlanRevisionService.get_plan_revision`) consults
      ``Plan_Revisions``; the other two are required as FK targets.
    * Slice 3 Execution_Service (``Work_Assignment_Records``,
      ``Work_Event_Records``, ``Deliverable_Production_Records``,
      ``Milestone_Acceptance_Records``, ``Completion_Records``) and
      Deliverable_Repository (``Deliverable_Resources``,
      ``Deliverable_Revisions``) — every read the projection
      performs hits one of these tables.

    Disclosure policy seeding is intentionally omitted: the
    projection helper does not consult ``Disclosure_Policy_Coverage``
    (route-layer authority evaluation, task 15.1, owns that check)
    so the schema is sufficient without seeded coverage rows.
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


@pytest.fixture
def status_projector() -> StatusProjector:
    """A :class:`StatusProjector` registered with the Execution
    Projection Definition.

    Fixes the clock at :data:`_CLOCK_INSTANT` so the envelope's
    ``generated_at`` is deterministic across tests.
    """
    return StatusProjector(
        clock=FixedClock(_CLOCK_INSTANT),
        definition_registry=execution_projection_registry(),
    )


@pytest.fixture
def empty_projector() -> StatusProjector:
    """A :class:`StatusProjector` with no Projection Definitions
    registered.

    Drives the Requirement 39.5 unresolvable-definition path: the
    helper short-circuits before any database SELECT and returns an
    :class:`ExplanationUnavailableResponse` naming the missing
    definition.
    """
    return StatusProjector(
        clock=FixedClock(_CLOCK_INSTANT),
        definition_registry={},
    )


# ---------------------------------------------------------------------------
# Seed helpers — direct INSERTs against the three schemas.
#
# AD-WS-27 append-only triggers fire only on UPDATE / DELETE so every
# row below is seeded with a single INSERT. The Slice 2 AD-WS-19
# lifecycle trigger fires only on UPDATE, so a Plan Revision can be
# inserted with ``lifecycle_state = 'approved'`` directly. The Slice 1
# / Slice 2 / Slice 3 read APIs the projection consults are
# read-only, so no service write path is exercised during seeding.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIRST},
    )


def _seed_required_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Contributor")
        _seed_party(conn, _QUERY_PARTY_ID, "Pilot Reviewer")


def _seed_project(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIRST},
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
                    :aid, :pid, 'Mesh Rollout Activities',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    lifecycle_state: str = "approved",
) -> None:
    """Insert one ``Plan_Revisions`` row.

    The AD-WS-19 lifecycle trigger only fires on UPDATE; a row may
    be inserted with ``lifecycle_state = 'approved'`` in one
    statement (mirrors the pattern in
    ``tests/unit/test_planning_slice3_read_apis.py``).
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
                    :rev, :aid, NULL, :state, 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _ACTIVITY_PLAN_ID,
                "state": lifecycle_state,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    target_plan_revision_id: str = _APPROVED_PLAN_REVISION_ID,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    Picks distinct assignee and assignment-authority Identities to
    honor the Requirement 23.5 CHECK (``assignee_party_id !=
    assignment_authority_party_id``). Both identifiers reference
    seeded ``Parties`` rows.
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
                    'Assigning the rollout.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_assignment_id,
                "prev": target_plan_revision_id,
                "assignee": _PARTY_ID,
                "authority": _QUERY_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_work_event(
    engine: Engine,
    *,
    work_event_id: str,
    work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    event_kind: str,
    recorded_at: str = _TS_FIRST,
) -> None:
    """Insert one ``Work_Event_Records`` row by direct INSERT.

    The partial UNIQUE index ``idx_work_events_one_started_per_wa``
    enforces at-most-one ``started`` per Work Assignment, so tests
    that need two ``started`` events use distinct Work Assignments.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Event_Records (
                    work_event_id, target_work_assignment_id,
                    event_kind, event_note, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :wid, :wa, :kind, NULL, :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_event_id,
                "wa": work_assignment_id,
                "kind": event_kind,
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": recorded_at,
            },
        )


def _seed_deliverable(engine: Engine) -> None:
    """Insert one Deliverable Resource + Revision pair.

    The Revision is referenced as the FK target on
    ``Deliverable_Production_Records.produced_deliverable_revision_id``.
    """
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Mesh runbook', :ts)
                """
            ),
            {"did": _DELIVERABLE_ID, "ts": _TS_FIRST},
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
                "party": _PARTY_ID,
                "ts": _TS_FIRST,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _DELIVERABLE_EXPECTATION_ID, "ts": _TS_FIRST},
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
                    :rev, :did, NULL, :pid, 'Mesh Operations Runbook',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_deliverable_production(engine: Engine) -> None:
    """Insert the source ``Deliverable_Production_Records`` row."""
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
                    'Produced runbook for milestone one.',
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
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_milestone_acceptance(
    engine: Engine, *, outcome: str = "Accept"
) -> None:
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
                    :outcome, 'Milestone one criteria satisfied.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": _ACCEPT_MILESTONE_ID,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "outcome": outcome,
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_completion(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id, target_plan_revision_id,
                    target_activity_plan_id, target_project_id,
                    outcome, rationale,
                    source_milestone_acceptance_ids_json,
                    completing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :cid, :prev, :aid, :proj,
                    'Completed', 'Phase 1 completed.',
                    :sources, :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": _COMPLETION_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "proj": _PROJECT_ID,
                "sources": f'["{_ACCEPT_MILESTONE_ID}"]',
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_baseline_graph(engine: Engine) -> None:
    """Seed the Slice 1 / Slice 2 prerequisites and the approved Plan
    Revision used by every pipeline-stage test.

    Specifically:

    * Two ``Parties`` rows (authoring + querying).
    * One ``Projects`` row.
    * One ``Activity_Plans`` row pointing at the Project.
    * One ``Plan_Revisions`` row with ``lifecycle_state = 'approved'``.

    Pipeline-stage tests then layer one or more Slice 3 Records on
    top to reach the specific projected status under test.
    """
    _seed_required_parties(engine)
    _seed_project(engine)
    _seed_activity_plan(engine)
    _seed_plan_revision(
        engine, plan_revision_id=_APPROVED_PLAN_REVISION_ID
    )


def _project(
    engine: Engine,
    projector: StatusProjector,
    *,
    plan_revision_id: str = _APPROVED_PLAN_REVISION_ID,
    at: datetime = _BOUNDARY,
) -> ExecutionStatusProjection | ExplanationUnavailableResponse:
    """Drive :func:`project_execution_status` against the per-test
    engine.

    Encapsulated so the pipeline-stage tests stay focused on their
    seed configuration; the connection plumbing and keyword wiring
    are uniform across the file.
    """
    with engine.connect() as conn:
        return project_execution_status(
            conn,
            plan_revision_id=plan_revision_id,
            party_id=_QUERY_PARTY_ID,
            at=at,
            status_projector=projector,
        )


# ---------------------------------------------------------------------------
# Row-level helpers for the byte-equivalence assertions.
# ---------------------------------------------------------------------------


def _table_snapshot(engine: Engine, table: str) -> list[dict]:
    """Return every row in ``table`` as a sorted list of dicts.

    Used by :class:`TestSourceRecordByteEquivalence` to capture
    pre-projection row state and confirm the row state is unchanged
    after :func:`project_execution_status` runs. The dict form
    preserves every column value verbatim so a regression introducing
    a column mutation surfaces as a key-value difference.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
    # Sort by sorted-key value tuple so the comparison is order
    # independent (the projection's deterministic ORDER BY is asserted
    # separately).
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: tuple(sorted(row.items())),
    )


# Tables every byte-equivalence test snapshots before the projection
# call. Slice 1 ``Audit_Records`` is included so a regression that
# accidentally appends a consequential row (the projection is
# read-only — Principle 5.23) trips the assertion.
_SNAPSHOT_TABLES: tuple[str, ...] = (
    # Slice 1.
    "Parties",
    "Audit_Records",
    # Slice 2.
    "Projects",
    "Activity_Plans",
    "Plan_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    # Slice 3 Execution.
    "Work_Assignment_Records",
    "Work_Event_Records",
    "Deliverable_Production_Records",
    "Milestone_Acceptance_Records",
    "Completion_Records",
    # Slice 3 Deliverable_Repository.
    "Deliverable_Resources",
    "Deliverable_Revisions",
)


def _full_snapshot(engine: Engine) -> dict[str, list[dict]]:
    """Return a snapshot of every byte-equivalence-tracked table."""
    return {table: _table_snapshot(engine, table) for table in _SNAPSHOT_TABLES}


# ===========================================================================
# Tests
# ===========================================================================


# ---------------------------------------------------------------------------
# Requirement 39.1 — each projected status value at the appropriate
# pipeline stage (design §"Execution-status Projection" steps 2..7).
# ---------------------------------------------------------------------------


class TestProjectedStatusByPipelineStage:
    """Each pipeline stage maps to exactly one projected status string.

    The seven status values declared by
    :data:`EXECUTION_PROJECTED_STATUSES` are covered: each test
    seeds the precise pipeline-stage shape that produces its target
    status and asserts the projection returns that string verbatim.
    """

    def test_known_status_set_matches_module_export(self) -> None:
        # Pin the membership set so a future status added to the
        # module without updating these tests is caught immediately.
        assert EXECUTION_PROJECTED_STATUSES == frozenset(
            {
                EXECUTION_STATUS_APPROVED,
                EXECUTION_STATUS_IN_EXECUTION,
                EXECUTION_STATUS_EXECUTION_PAUSED,
                EXECUTION_STATUS_DELIVERABLE_PRODUCED,
                EXECUTION_STATUS_MILESTONE_ACCEPTED,
                EXECUTION_STATUS_COMPLETION_RECORDED,
                EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
            }
        )

    def test_no_work_assignment_yields_plan_revision_approved(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 2: zero Work Assignments → baseline projected
        # status. The approved Plan Revision exists but no execution
        # has begun.
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.projected_status == EXECUTION_STATUS_APPROVED
        assert response.explanation_unavailable is None

    def test_work_assignment_without_work_event_yields_plan_revision_approved(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # A Work Assignment exists, but no Work Event yet — execution
        # has not begun. Per design step 7 the projection falls back
        # to ``approved`` rather than ``in execution``.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.projected_status == EXECUTION_STATUS_APPROVED

    def test_started_event_yields_plan_revision_in_execution(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 7 fall-back: at least one Work Event exists
        # AND the most-recent event on at least one Work Assignment
        # is not ``paused`` (here it is ``started``).
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.projected_status == EXECUTION_STATUS_IN_EXECUTION

    def test_paused_most_recent_event_yields_plan_revision_execution_paused(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 7: ``paused`` only when every Work Assignment
        # has at least one Work Event AND every most-recent event is
        # ``paused``. Two events on a single Work Assignment cover
        # the most-recent-wins path; a strictly later ``recorded_at``
        # ensures the ``paused`` row wins the ORDER BY recorded_at
        # DESC tie-break.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_PAUSED_ID,
            event_kind="paused",
            recorded_at=_TS_SECOND,
        )

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.projected_status == EXECUTION_STATUS_EXECUTION_PAUSED

    def test_paused_does_not_apply_when_one_work_assignment_lacks_events(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Per design step 7 the ``paused`` projection requires
        # *every* Work Assignment to have at least one Work Event.
        # A second Work Assignment with no Work Event prevents the
        # ``paused`` classification — the projection falls back to
        # ``in execution``.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_assignment(
            projection_engine,
            work_assignment_id=_SECOND_WORK_ASSIGNMENT_ID,
        )
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_PAUSED_ID,
            event_kind="paused",
            work_assignment_id=_WORK_ASSIGNMENT_ID,
            recorded_at=_TS_FIRST,
        )

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.projected_status == EXECUTION_STATUS_IN_EXECUTION

    def test_deliverable_production_yields_plan_revision_deliverable_produced(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 4: at least one Deliverable Production sourced
        # from a Work Assignment targeting the Plan Revision wins
        # over the in-execution / paused states.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_DELIVERABLE_PRODUCED
        )

    def test_accept_milestone_acceptance_yields_plan_revision_milestone_accepted(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 5: at least one ``Accept``-outcome Milestone
        # Acceptance against a Production sourced from a Work
        # Assignment targeting the Plan Revision.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)
        _seed_milestone_acceptance(projection_engine, outcome="Accept")

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_MILESTONE_ACCEPTED
        )

    def test_reject_milestone_acceptance_does_not_yield_milestone_accepted(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 5 explicitly requires the Milestone outcome to
        # be ``Accept``. A ``Reject``-outcome Milestone Acceptance
        # is filtered out and the projection falls back to the next
        # most-progressed status (``deliverable produced`` because a
        # Production exists).
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)
        _seed_milestone_acceptance(projection_engine, outcome="Reject")

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_DELIVERABLE_PRODUCED
        )

    def test_completion_record_yields_plan_revision_completion_recorded(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 6: a Completion Record targeting the Plan
        # Revision is the terminal projected status — it wins over
        # every earlier projection.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)
        _seed_milestone_acceptance(projection_engine, outcome="Accept")
        _seed_completion(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_COMPLETION_RECORDED
        )

    def test_unresolvable_plan_revision_yields_provenance_incomplete(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Requirement 39.5 / design step 1: the Plan Revision Identity
        # does not resolve. The projection withholds the most-
        # progressed status and surfaces ``Provenance incomplete``
        # with the explanation-unavailable indicator naming the
        # missing source Revision.
        _seed_required_parties(projection_engine)
        _seed_project(projection_engine)
        _seed_activity_plan(projection_engine)
        # Note: no ``Plan_Revisions`` row inserted for the
        # unresolvable Identity.

        response = _project(
            projection_engine,
            status_projector,
            plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
        )

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_PROVENANCE_INCOMPLETE
        )
        assert response.explanation_unavailable is not None
        assert (
            response.explanation_unavailable.missing_element_kind
            == "source_revision"
        )
        assert response.explanation_unavailable.missing_element_identifier == (
            _UNRESOLVABLE_PLAN_REVISION_ID
        )

    def test_draft_plan_revision_yields_provenance_incomplete(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Design step 1 narrows the projection to ``approved`` Plan
        # Revisions. A ``draft`` Plan Revision is treated as an
        # unresolvable source Record (the projection cannot speak
        # about execution of an unapproved plan).
        _seed_required_parties(projection_engine)
        _seed_project(projection_engine)
        _seed_activity_plan(projection_engine)
        _seed_plan_revision(
            projection_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        response = _project(
            projection_engine,
            status_projector,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
        )

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_PROVENANCE_INCOMPLETE
        )
        assert response.explanation_unavailable is not None
        assert (
            response.explanation_unavailable.missing_element_kind
            == "source_revision"
        )


# ---------------------------------------------------------------------------
# Requirement 39.1, 39.2 — envelope carries every required field.
# ---------------------------------------------------------------------------


class TestEnvelopeContents:
    """Every projected status surfaced by the helper rides on a
    fully populated :class:`ProjectionEnvelope` (Requirement 39.1)
    with the derivation indicator fixed at ``"derived"`` (Requirement
    39.2).
    """

    def test_envelope_carries_projection_definition(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert isinstance(response.envelope, ProjectionEnvelope)
        assert response.envelope.definition == EXECUTION_PROJECTION_DEFINITION
        assert response.envelope.definition.name == (
            EXECUTION_PROJECTION_DEFINITION_NAME
        )
        assert response.envelope.definition.version == (
            EXECUTION_PROJECTION_DEFINITION_VERSION
        )

    def test_envelope_source_resource_ids_contain_target_plan_revision(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Requirement 39.1 mandates the envelope identify the source
        # Resource Identities. For this projection the Plan Revision
        # is the addressed Resource; it must appear in the envelope
        # so the caller can correlate the response with the request.
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.envelope.source_resource_ids == (
            uuid.UUID(_APPROVED_PLAN_REVISION_ID),
        )

    def test_envelope_source_revision_ids_include_every_consulted_record(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Requirement 39.1 — every Slice 3 Record consulted by the
        # projection must appear on the envelope's
        # ``source_revision_ids``. The full-pipeline graph below
        # exercises every consultation: one Work Assignment, one
        # most-recent Work Event, one Production, one Accept
        # Milestone, one Completion.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)
        _seed_milestone_acceptance(projection_engine, outcome="Accept")
        _seed_completion(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        revision_ids = set(response.envelope.source_revision_ids)
        # Every Slice 3 Record the projection consults must be
        # represented.
        assert uuid.UUID(_WORK_ASSIGNMENT_ID) in revision_ids
        assert uuid.UUID(_WORK_EVENT_STARTED_ID) in revision_ids
        assert uuid.UUID(_DELIVERABLE_PRODUCTION_ID) in revision_ids
        assert uuid.UUID(_ACCEPT_MILESTONE_ID) in revision_ids
        assert uuid.UUID(_COMPLETION_ID) in revision_ids

    def test_envelope_temporal_boundary_at_second_precision_utc(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.envelope.applicable_temporal_boundary == _BOUNDARY
        assert response.envelope.applicable_temporal_boundary.tzinfo == (
            timezone.utc
        )
        assert response.envelope.applicable_temporal_boundary.microsecond == 0

    def test_envelope_temporal_boundary_truncates_sub_second_input(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # The Slice 1 :class:`Clock` carries millisecond precision;
        # the helper truncates to second precision so the envelope
        # validator accepts the value. Requirement 39.1 phrasing
        # ("ISO-8601 with at least second precision") is enforced
        # as strict second precision by :class:`ProjectionEnvelope`.
        _seed_baseline_graph(projection_engine)
        sub_second_boundary = datetime(
            2026, 1, 1, 12, 0, 0, 123_000, tzinfo=timezone.utc
        )

        response = _project(
            projection_engine, status_projector, at=sub_second_boundary
        )

        assert isinstance(response, ExecutionStatusProjection)
        assert response.envelope.applicable_temporal_boundary == _BOUNDARY
        assert response.envelope.applicable_temporal_boundary.microsecond == 0

    def test_envelope_generated_at_at_second_precision_from_clock(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # ``generated_at`` is sourced from the projector's clock and
        # truncated to second precision so the envelope validator
        # accepts the value (Requirement 39.1).
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.envelope.generated_at == _CLOCK_INSTANT
        assert response.envelope.generated_at.tzinfo == timezone.utc
        assert response.envelope.generated_at.microsecond == 0

    def test_envelope_derivation_indicator_is_derived(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Requirement 39.2 — every projected status carries a
        # derivation indicator marking it as distinct from
        # authoritative source Records.
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.envelope.derivation == "derived"

    @pytest.mark.parametrize(
        "seed_callable, expected_status",
        [
            (
                lambda eng: _seed_baseline_graph(eng),
                EXECUTION_STATUS_APPROVED,
            ),
            (
                lambda eng: (
                    _seed_baseline_graph(eng),
                    _seed_work_assignment(eng),
                    _seed_work_event(
                        eng,
                        work_event_id=_WORK_EVENT_STARTED_ID,
                        event_kind="started",
                        recorded_at=_TS_FIRST,
                    ),
                ),
                EXECUTION_STATUS_IN_EXECUTION,
            ),
            (
                lambda eng: (
                    _seed_baseline_graph(eng),
                    _seed_work_assignment(eng),
                    _seed_work_event(
                        eng,
                        work_event_id=_WORK_EVENT_STARTED_ID,
                        event_kind="started",
                        recorded_at=_TS_FIRST,
                    ),
                    _seed_work_event(
                        eng,
                        work_event_id=_WORK_EVENT_PAUSED_ID,
                        event_kind="paused",
                        recorded_at=_TS_SECOND,
                    ),
                ),
                EXECUTION_STATUS_EXECUTION_PAUSED,
            ),
            (
                lambda eng: (
                    _seed_baseline_graph(eng),
                    _seed_work_assignment(eng),
                    _seed_work_event(
                        eng,
                        work_event_id=_WORK_EVENT_STARTED_ID,
                        event_kind="started",
                        recorded_at=_TS_FIRST,
                    ),
                    _seed_deliverable(eng),
                    _seed_deliverable_expectation(eng),
                    _seed_deliverable_production(eng),
                ),
                EXECUTION_STATUS_DELIVERABLE_PRODUCED,
            ),
            (
                lambda eng: (
                    _seed_baseline_graph(eng),
                    _seed_work_assignment(eng),
                    _seed_work_event(
                        eng,
                        work_event_id=_WORK_EVENT_STARTED_ID,
                        event_kind="started",
                        recorded_at=_TS_FIRST,
                    ),
                    _seed_deliverable(eng),
                    _seed_deliverable_expectation(eng),
                    _seed_deliverable_production(eng),
                    _seed_milestone_acceptance(eng, outcome="Accept"),
                ),
                EXECUTION_STATUS_MILESTONE_ACCEPTED,
            ),
            (
                lambda eng: (
                    _seed_baseline_graph(eng),
                    _seed_work_assignment(eng),
                    _seed_work_event(
                        eng,
                        work_event_id=_WORK_EVENT_STARTED_ID,
                        event_kind="started",
                        recorded_at=_TS_FIRST,
                    ),
                    _seed_deliverable(eng),
                    _seed_deliverable_expectation(eng),
                    _seed_deliverable_production(eng),
                    _seed_milestone_acceptance(eng, outcome="Accept"),
                    _seed_completion(eng),
                ),
                EXECUTION_STATUS_COMPLETION_RECORDED,
            ),
        ],
        ids=[
            "approved",
            "in_execution",
            "execution_paused",
            "deliverable_produced",
            "milestone_accepted",
            "completion_recorded",
        ],
    )
    def test_every_pipeline_stage_carries_complete_envelope(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
        seed_callable,
        expected_status: str,
    ) -> None:
        # Parametric check over every happy-path projected status:
        # the envelope shape must be uniform across pipeline stages.
        # A regression where, for example, the completion-recorded
        # branch forgets to attach the Completion Identity surfaces
        # here on that branch alone.
        seed_callable(projection_engine)

        response = _project(projection_engine, status_projector)

        assert isinstance(response, ExecutionStatusProjection)
        assert response.projected_status == expected_status
        # Every required envelope field is present and well-formed.
        envelope = response.envelope
        assert envelope.definition == EXECUTION_PROJECTION_DEFINITION
        assert envelope.source_resource_ids == (
            uuid.UUID(_APPROVED_PLAN_REVISION_ID),
        )
        assert isinstance(envelope.source_revision_ids, tuple)
        assert envelope.applicable_temporal_boundary == _BOUNDARY
        assert envelope.applicable_temporal_boundary.microsecond == 0
        assert envelope.generated_at == _CLOCK_INSTANT
        assert envelope.generated_at.microsecond == 0
        assert envelope.derivation == "derived"

    def test_withholding_envelope_carries_plan_revision_in_resource_ids(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # The withholding branch (Requirement 39.5) still surfaces a
        # complete envelope. ``source_resource_ids`` carries the
        # requested Plan Revision Identity so the caller can
        # correlate the withholding response with the original
        # request.
        _seed_required_parties(projection_engine)

        response = _project(
            projection_engine,
            status_projector,
            plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
        )

        assert isinstance(response, ExecutionStatusProjection)
        envelope = response.envelope
        assert envelope.definition == EXECUTION_PROJECTION_DEFINITION
        assert envelope.source_resource_ids == (
            uuid.UUID(_UNRESOLVABLE_PLAN_REVISION_ID),
        )
        # No Slice 3 Record could be consulted on the missing-source
        # path; the list is empty.
        assert envelope.source_revision_ids == ()
        assert envelope.applicable_temporal_boundary == _BOUNDARY
        assert envelope.generated_at == _CLOCK_INSTANT
        assert envelope.derivation == "derived"


# ---------------------------------------------------------------------------
# Requirement 39.5 — unresolvable-definition path withholds status.
# ---------------------------------------------------------------------------


class TestUnresolvableProjectionDefinition:
    """When the Projection Definition is not registered on the
    projector, the helper withholds the projected status and returns
    an :class:`ExplanationUnavailableResponse` naming the missing
    definition. No envelope is surfaced and no database SELECT is
    issued.
    """

    def test_unregistered_definition_yields_explanation_unavailable(
        self,
        projection_engine: Engine,
        empty_projector: StatusProjector,
    ) -> None:
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, empty_projector)

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "projection_definition"
        assert response.missing_element_identifier == (
            EXECUTION_PROJECTION_DEFINITION_NAME
        )

    def test_unregistered_definition_does_not_surface_projected_status(
        self,
        projection_engine: Engine,
        empty_projector: StatusProjector,
    ) -> None:
        # Requirement 39.5 mandates the status be *withheld*. The
        # response shape must not carry any of the wrapped-status
        # attributes (envelope, projected_status, etc.).
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, empty_projector)

        assert isinstance(response, ExplanationUnavailableResponse)
        assert not hasattr(response, "envelope")
        assert not hasattr(response, "projected_status")

    def test_unregistered_definition_short_circuits_before_select(
        self,
        projection_engine: Engine,
        empty_projector: StatusProjector,
    ) -> None:
        # The Plan Revision is *not* seeded. If the helper attempted
        # a SELECT against ``Plan_Revisions`` before checking the
        # registry it would return ``Provenance incomplete`` instead
        # of withholding the definition. Asserting the
        # ``projection_definition`` kind here pins the short-circuit
        # ordering.
        _seed_required_parties(projection_engine)
        # No Project, Activity Plan, or Plan Revision seeded.

        response = _project(
            projection_engine,
            empty_projector,
            plan_revision_id=_APPROVED_PLAN_REVISION_ID,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "projection_definition"
        assert response.missing_element_identifier == (
            EXECUTION_PROJECTION_DEFINITION_NAME
        )


# ---------------------------------------------------------------------------
# Requirement 39.4 — source Records remain byte-equivalent when a
# correction arrives.
# ---------------------------------------------------------------------------


class TestSourceRecordByteEquivalence:
    """The projection helper performs only read-only SELECTs
    (Principle 5.23 — Projections are derived). Every Slice 1 /
    Slice 2 / Slice 3 row remains byte-equivalent across a
    :func:`project_execution_status` invocation, and across a
    correction cycle where a new Slice 3 Record arrives between
    two invocations.
    """

    def test_full_graph_unchanged_after_projection(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Seed every consulted-table row, snapshot the full graph,
        # run the projection, and assert every snapshotted table is
        # byte-equivalent afterward.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)
        _seed_milestone_acceptance(projection_engine, outcome="Accept")
        _seed_completion(projection_engine)

        before = _full_snapshot(projection_engine)

        response = _project(projection_engine, status_projector)
        assert isinstance(response, ExecutionStatusProjection)

        after = _full_snapshot(projection_engine)
        assert before == after

    def test_repeated_projection_is_byte_equivalent(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Two consecutive projections over the same source set
        # produce byte-equivalent responses (Property 7 — idempotent
        # retrieval, mirrored at the example-test level).
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )

        first = _project(projection_engine, status_projector)
        second = _project(projection_engine, status_projector)

        assert isinstance(first, ExecutionStatusProjection)
        assert isinstance(second, ExecutionStatusProjection)
        assert first == second
        assert first.envelope == second.envelope

    def test_source_records_unchanged_when_late_arriving_record_changes_status(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Requirement 39.4 headline scenario: a late-arriving source
        # fact (here, a Milestone Acceptance arriving after the
        # first projection) corrects the projected status. Every
        # source Record that existed before the correction must
        # remain byte-equivalent to its recorded state; the new
        # fact arrives as an *additional* Record (Principle 5.6 —
        # Durable states are historical), never as an overwrite.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)

        # First projection: ``Plan Revision deliverable produced``.
        before_correction = _project(projection_engine, status_projector)
        assert isinstance(before_correction, ExecutionStatusProjection)
        assert (
            before_correction.projected_status
            == EXECUTION_STATUS_DELIVERABLE_PRODUCED
        )

        # Snapshot the row state before the correction arrives.
        snapshot_before_correction = _full_snapshot(projection_engine)

        # Correction: a Milestone Acceptance arrives as a new Record.
        _seed_milestone_acceptance(projection_engine, outcome="Accept")

        # The Milestone Acceptance is the *only* table that gained
        # a row. Every other tracked table remains byte-equivalent.
        snapshot_after_correction = _full_snapshot(projection_engine)
        for table in _SNAPSHOT_TABLES:
            if table == "Milestone_Acceptance_Records":
                # Acceptance row count strictly increases — the new
                # Record is the correction.
                assert len(snapshot_after_correction[table]) == (
                    len(snapshot_before_correction[table]) + 1
                )
            else:
                assert (
                    snapshot_after_correction[table]
                    == snapshot_before_correction[table]
                ), f"table {table} mutated when only the new Milestone Acceptance should have been appended"

        # Second projection: the corrected status surfaces.
        after_correction = _project(projection_engine, status_projector)
        assert isinstance(after_correction, ExecutionStatusProjection)
        assert (
            after_correction.projected_status
            == EXECUTION_STATUS_MILESTONE_ACCEPTED
        )

        # The first projection response remains byte-equivalent — a
        # frozen dataclass cannot be mutated. (Value-object
        # equality already covers this; the assertion pins the
        # behavior explicitly so a future change introducing a
        # shared mutable cache would fail fast.)
        assert (
            before_correction.projected_status
            == EXECUTION_STATUS_DELIVERABLE_PRODUCED
        )

    def test_no_audit_record_appended_during_projection(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Principle 5.23 / Requirement 39.4 — Projections are
        # derived; the projection helper must not append a
        # consequential audit row. Snapshotting and comparing the
        # ``Audit_Records`` table is the structural proof.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )

        with projection_engine.connect() as conn:
            before = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM Audit_Records")
                ).scalar_one()
            )

        _project(projection_engine, status_projector)

        with projection_engine.connect() as conn:
            after = int(
                conn.execute(
                    text("SELECT COUNT(*) FROM Audit_Records")
                ).scalar_one()
            )
        assert before == after

    def test_withholding_branch_leaves_source_records_unchanged(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Requirement 39.5 phrasing: "leave stored source Records
        # unchanged". The withholding branch (unresolvable Plan
        # Revision) must not mutate any Slice 1 / Slice 2 / Slice 3
        # row.
        _seed_required_parties(projection_engine)
        _seed_project(projection_engine)
        _seed_activity_plan(projection_engine)
        # No Plan Revision seeded — the lookup returns None.

        before = _full_snapshot(projection_engine)

        response = _project(
            projection_engine,
            status_projector,
            plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
        )

        assert isinstance(response, ExecutionStatusProjection)
        assert (
            response.projected_status
            == EXECUTION_STATUS_PROVENANCE_INCOMPLETE
        )
        after = _full_snapshot(projection_engine)
        assert before == after


# ---------------------------------------------------------------------------
# Requirement 39.3, 39.6 — absence of prohibited derived fields.
# ---------------------------------------------------------------------------


# Field-name fragments that must NEVER appear in the projection
# response shape per Requirement 39.3. Centralized so a future
# regression that adds a column with one of these substrings is
# caught by every test that uses the list.
_PROHIBITED_DERIVED_TERMS: tuple[str, ...] = (
    "percent_complete",
    "percent_complete".replace("_", ""),
    "actual_cost",
    "actual_cost".replace("_", ""),
    "remaining_work",
    "remaining_work".replace("_", ""),
    "budget_variance",
    "budget_variance".replace("_", ""),
    "forecast_cost",
    "forecast_cost".replace("_", ""),
    "outcome_attainment",
    "outcome_attainment".replace("_", ""),
)


# Field-name fragments that must NEVER appear in the projection
# response shape per Requirement 39.6. The projection is a
# projection of *work performed*, not of outcome.
_PROHIBITED_OUTCOME_TERMS: tuple[str, ...] = (
    "observed_outcome",
    "observed_outcomes",
    "measurement",
    "success_condition",
    "attribution_evidence",
)


def _all_field_names() -> set[str]:
    """Return every declared field name on the projection response
    shapes the helper can return.

    Used by the prohibited-field assertions so a regression that
    adds a new field tripping a substring match is caught no matter
    which response class the field lands on.
    """
    names: set[str] = set()
    names.update(f.name for f in dataclasses.fields(ExecutionStatusProjection))
    names.update(ProjectionEnvelope.model_fields.keys())
    names.update(ExplanationUnavailableResponse.model_fields.keys())
    return names


class TestProhibitedDerivedFields:
    """Requirements 39.3 and 39.6: the projection response carries
    only the projected-status label and the envelope. No derived
    percent-complete, actual-cost, remaining-work, budget-variance,
    forecast-cost, or outcome-attainment value is surfaced, and the
    projection is never labeled or aliased as an Observed Outcome.
    """

    def test_execution_status_projection_declared_fields_are_minimal(
        self,
    ) -> None:
        # The shape is locked to exactly the four fields the design
        # specifies. Adding a field requires a deliberate update
        # here so a regression is impossible to merge silently.
        declared = {f.name for f in dataclasses.fields(ExecutionStatusProjection)}
        assert declared == {
            "plan_revision_id",
            "projected_status",
            "envelope",
            "explanation_unavailable",
        }

    def test_envelope_declared_fields_are_minimal(self) -> None:
        # The Slice 1 envelope is shared across producers; pinning
        # its field set in the Slice 3 test confirms the slice does
        # not silently widen the envelope to carry an outcome
        # value. A widening would require updating both this test
        # and the Slice 1 / Slice 2 mirror tests.
        declared = set(ProjectionEnvelope.model_fields.keys())
        assert declared == {
            "definition",
            "source_resource_ids",
            "source_revision_ids",
            "applicable_temporal_boundary",
            "generated_at",
            "derivation",
        }

    @pytest.mark.parametrize("forbidden", _PROHIBITED_DERIVED_TERMS)
    def test_no_prohibited_derived_field_appears_on_response_shape(
        self, forbidden: str
    ) -> None:
        # Requirement 39.3 — none of the six prohibited derived
        # metric names may appear on any field of any response
        # shape the projection can return. The substring match
        # catches both the exact name and any spelling variant
        # (``percentComplete``, ``percent_complete_value``, etc.).
        for name in _all_field_names():
            assert forbidden not in name.lower(), (
                f"prohibited derived field name {forbidden!r} "
                f"appeared on response shape field {name!r}"
            )

    @pytest.mark.parametrize("forbidden", _PROHIBITED_OUTCOME_TERMS)
    def test_no_observed_outcome_alias_appears_on_response_shape(
        self, forbidden: str
    ) -> None:
        # Requirement 39.6 — the projected status is never an
        # Observed Outcome, a Measurement, an attribution evidence
        # reference, or a success-condition assessment. No field
        # name on the response shape may suggest such an alias.
        for name in _all_field_names():
            assert forbidden not in name.lower(), (
                f"prohibited outcome term {forbidden!r} appeared on "
                f"response shape field {name!r}"
            )

    @pytest.mark.parametrize("forbidden", _PROHIBITED_DERIVED_TERMS)
    def test_no_prohibited_derived_key_in_serialized_response(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
        forbidden: str,
    ) -> None:
        # End-to-end check: serialize the projection response and
        # confirm no key in the serialized output matches any
        # prohibited derived term. Catches a regression where, for
        # example, a future iteration adds a derived field on the
        # serialization layer rather than on the dataclass.
        _seed_baseline_graph(projection_engine)

        response = _project(projection_engine, status_projector)
        assert isinstance(response, ExecutionStatusProjection)
        serialized = response.envelope.model_dump()

        for key in serialized.keys():
            assert forbidden not in key.lower(), (
                f"prohibited derived term {forbidden!r} appeared in "
                f"serialized envelope key {key!r}"
            )

    def test_projected_status_strings_do_not_reference_observed_outcome(
        self,
    ) -> None:
        # Requirement 39.6: every projected-status string is a
        # projection of *work performed*. None of the seven status
        # strings may reference an Observed Outcome, a Measurement,
        # a success-condition assessment, or an Intended Outcome.
        forbidden_substrings = (
            "Observed Outcome",
            "Measurement",
            "Intended Outcome",
            "success condition",
            "attainment",
        )
        for status in EXECUTION_PROJECTED_STATUSES:
            for term in forbidden_substrings:
                assert term.lower() not in status.lower(), (
                    f"projected status {status!r} references prohibited "
                    f"outcome term {term!r}"
                )

    def test_response_body_has_no_prohibited_field_for_each_pipeline_stage(
        self,
        projection_engine: Engine,
        status_projector: StatusProjector,
    ) -> None:
        # Drive a full pipeline through to ``Plan Revision
        # completion recorded`` and confirm the serialized response
        # body contains none of the prohibited derived fields. The
        # completion-recorded stage is the richest source set so
        # any prohibited field that escapes into the envelope on
        # any branch would also escape here.
        _seed_baseline_graph(projection_engine)
        _seed_work_assignment(projection_engine)
        _seed_work_event(
            projection_engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(projection_engine)
        _seed_deliverable_expectation(projection_engine)
        _seed_deliverable_production(projection_engine)
        _seed_milestone_acceptance(projection_engine, outcome="Accept")
        _seed_completion(projection_engine)

        response = _project(projection_engine, status_projector)
        assert isinstance(response, ExecutionStatusProjection)
        envelope_payload = response.envelope.model_dump()

        # Recurse the serialized payload for any prohibited key.
        def _collect_keys(node) -> Iterator[str]:
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from _collect_keys(v)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    yield from _collect_keys(item)

        keys_in_payload = set(_collect_keys(envelope_payload))
        for forbidden in _PROHIBITED_DERIVED_TERMS + _PROHIBITED_OUTCOME_TERMS:
            for key in keys_in_payload:
                assert forbidden not in key.lower(), (
                    f"prohibited term {forbidden!r} appeared in "
                    f"envelope payload key {key!r}"
                )
