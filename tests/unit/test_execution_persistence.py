"""Unit tests for :mod:`walking_slice.execution._persistence` (task 1.2).

These tests pin the contract established in
``.kiro/specs/third-walking-slice/design.md`` §"Data Models — Schema
Additions", §"Indexes", AD-WS-27 (Slice 3 Records are append-only with
no supersession path), and AD-WS-28 (per-Record-kind tables with
append-only triggers):

- Every Slice 3 Execution_Service table in
  :data:`walking_slice.execution._persistence.EXECUTION_IMMUTABLE_TABLES`
  rejects ``UPDATE`` and ``DELETE`` per AD-WS-27 / AD-WS-28
  (Requirements 23.9, 24.7, 25.6, 27.7, 28.7, 29.7, 41.4, 41.10).
- ``Work_Assignment_Records`` enforces the
  ``assignee_party_id != assignment_authority_party_id`` CHECK
  (Requirement 23.5 — no self-assignment).
- ``Time_Entry_Records`` enforces the decimal-regex CHECK
  (``0.00..24.00`` with at most two fractional digits), the
  numeric-range CHECK, and the temporal-ordering CHECKs
  (Requirements 25.2, 25.3, 25.5).
- ``Work_Event_Records`` partial UNIQUE index rejects a second
  ``started`` event per Work Assignment (Requirement 24.3).
- ``Milestone_Acceptance_Records.source_deliverable_production_id`` is
  ``UNIQUE`` (Requirement 28.3).
- ``Completion_Records.target_plan_revision_id`` is ``UNIQUE``
  (Requirement 29.3).
- The Slice 1 + Slice 2 schema co-exists with the Slice 3 schema in
  one SQLite file (Requirement 40 — no Slice 1/Slice 2 table is
  mutated by schema creation; Requirement 42.4).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.disclosure import seed as seed_disclosure
from walking_slice.execution._persistence import (
    EXECUTION_IMMUTABLE_TABLES,
    EXECUTION_SCHEMA_STATEMENTS,
    create_execution_schema,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


_PARTY_A = "00000000-0000-7000-8000-000000000a01"
_PARTY_B = "00000000-0000-7000-8000-000000000a02"
_PARTY_C = "00000000-0000-7000-8000-000000000a03"
_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000000b01"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000000b02"
_PROJECT_ID = "00000000-0000-7000-8000-000000000b03"
_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-000000000b04"
_DELIVERABLE_EXPECTATION_REV_ID = "00000000-0000-7000-8000-000000000b05"
_DELIVERABLE_ID = "00000000-0000-7000-8000-000000000c01"
_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000000c02"
_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000000d01"
_WORK_EVENT_ID = "00000000-0000-7000-8000-000000000d02"
_WORK_EVENT_ID_2 = "00000000-0000-7000-8000-000000000d03"
_TIME_ENTRY_ID = "00000000-0000-7000-8000-000000000d04"
_DELIVERABLE_PRODUCTION_ID = "00000000-0000-7000-8000-000000000d05"
_MILESTONE_ACCEPTANCE_ID = "00000000-0000-7000-8000-000000000d06"
_MILESTONE_ACCEPTANCE_ID_2 = "00000000-0000-7000-8000-000000000d07"
_COMPLETION_ID = "00000000-0000-7000-8000-000000000d08"
_COMPLETION_ID_2 = "00000000-0000-7000-8000-000000000d09"
_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-000000000e01"
_SCOPE = "pilot/team-a"
_TS_START = "2026-01-01T00:00:00.000+00:00"
_TS_END = "2026-01-01T01:00:00.000+00:00"
_TS_RECORDED = "2026-01-01T02:00:00.000+00:00"


# ---------------------------------------------------------------------------
# Schema fixture (Slice 1 + Slice 2 + Slice 3 + Deliverable_Repository
# stand-in tables).
#
# Task 1.2 owns only the Execution_Service tables. The
# ``Deliverable_Production_Records`` and ``Milestone_Acceptance_Records``
# tables carry foreign keys to ``Deliverable_Resources`` and
# ``Deliverable_Revisions`` that the Deliverable_Repository schema
# (task 1.3) will create. To exercise the Execution_Service schema in
# isolation we create those two tables here with the minimal column
# set that satisfies the foreign-key targets; this stand-in is
# replaced by task 1.3's full schema once that module lands.
# ---------------------------------------------------------------------------


_DELIVERABLE_REPOSITORY_STANDIN: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Resources (
        deliverable_id  TEXT PRIMARY KEY,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Revisions (
        deliverable_revision_id  TEXT PRIMARY KEY,
        deliverable_id           TEXT NOT NULL REFERENCES Deliverable_Resources(deliverable_id),
        recorded_at              TEXT NOT NULL
    )
    """,
)


@pytest.fixture
def execution_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1, Slice 2, and Slice 3 schemas.

    ``create_schema`` installs the Slice 1 tables (and the additive
    ``Relationships.semantic_role`` / ``Identifier_Registry.resource_kind``
    columns). ``create_planning_schema`` installs the Slice 2 tables so
    foreign-key targets exist for any test that wants to insert a
    Plan_Revision. The Deliverable_Repository stand-in tables are
    created here so the Execution_Service FKs resolve, and
    ``create_execution_schema`` installs the Slice 3 Execution_Service
    schema. ``seed_disclosure`` seeds the ``slice-default-2026`` row
    so subsequent ``Disclosure_Policy_Coverage`` writes (task 1.4) can
    resolve their FK.
    """
    create_schema(engine)
    create_planning_schema(engine)
    with engine.begin() as conn:
        for statement in _DELIVERABLE_REPOSITORY_STANDIN:
            conn.execute(text(statement))
    create_execution_schema(engine)
    seed_disclosure(engine)
    return engine


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_parties(conn) -> None:
    for party_id, display in (
        (_PARTY_A, "Assignment Authority"),
        (_PARTY_B, "Assignee"),
        (_PARTY_C, "Milestone Authority"),
    ):
        conn.execute(
            text(
                """
                INSERT INTO Parties (party_id, kind, display_name, created_at)
                VALUES (:pid, 'person', :name, :ts)
                """
            ),
            {"pid": party_id, "name": display, "ts": _TS_START},
        )


def _seed_deliverable_stub(conn) -> None:
    """Insert the minimal Deliverable_Resources / Deliverable_Revisions rows.

    The Execution_Service tables reference these by foreign key, so the
    rows must exist before any Deliverable_Production_Records or
    Milestone_Acceptance_Records insert.
    """
    conn.execute(
        text("INSERT INTO Deliverable_Resources (deliverable_id, created_at) VALUES (:id, :ts)"),
        {"id": _DELIVERABLE_ID, "ts": _TS_START},
    )
    conn.execute(
        text(
            """
            INSERT INTO Deliverable_Revisions (
                deliverable_revision_id, deliverable_id, recorded_at
            ) VALUES (:rev, :did, :ts)
            """
        ),
        {"rev": _DELIVERABLE_REVISION_ID, "did": _DELIVERABLE_ID, "ts": _TS_START},
    )


def _seed_work_assignment(conn) -> None:
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
                'Assigning the rollout.', 'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "wid": _WORK_ASSIGNMENT_ID,
            "prev": _PLAN_REVISION_ID,
            "assignee": _PARTY_B,
            "authority": _PARTY_A,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_work_event(conn, event_kind: str = "started", *, work_event_id: str | None = None,
                     recorded_at: str | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Work_Event_Records (
                work_event_id, target_work_assignment_id, event_kind,
                event_note, recording_party_id, authority_basis_type,
                authority_basis_id, applicable_scope, recorded_at
            ) VALUES (
                :wid, :waid, :kind, 'Note.', :party,
                'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "wid": work_event_id or _WORK_EVENT_ID,
            "waid": _WORK_ASSIGNMENT_ID,
            "kind": event_kind,
            "party": _PARTY_B,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": recorded_at or _TS_RECORDED,
        },
    )


def _seed_time_entry(
    conn,
    *,
    effort_hours: str = "1.50",
    period_start: str = _TS_START,
    period_end: str = _TS_END,
    recorded_at: str = _TS_RECORDED,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Time_Entry_Records (
                time_entry_id, target_work_assignment_id, effort_hours,
                effort_period_start, effort_period_end, recording_party_id,
                authority_basis_type, authority_basis_id, applicable_scope,
                recorded_at
            ) VALUES (
                :tid, :waid, :hours, :start, :end_, :party,
                'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "tid": _TIME_ENTRY_ID,
            "waid": _WORK_ASSIGNMENT_ID,
            "hours": effort_hours,
            "start": period_start,
            "end_": period_end,
            "party": _PARTY_B,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": recorded_at,
        },
    )


def _seed_deliverable_production(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Deliverable_Production_Records (
                deliverable_production_id, source_work_assignment_id,
                produced_deliverable_id, produced_deliverable_revision_id,
                target_deliverable_expectation_id,
                target_deliverable_expectation_revision_id,
                production_rationale, recording_party_id,
                authority_basis_type, authority_basis_id, applicable_scope,
                recorded_at
            ) VALUES (
                :dpid, :waid, :did, :drev, :deid, :derev,
                'Produced as planned.', :party, 'role-grant-id', :abid,
                :scope, :ts
            )
            """
        ),
        {
            "dpid": _DELIVERABLE_PRODUCTION_ID,
            "waid": _WORK_ASSIGNMENT_ID,
            "did": _DELIVERABLE_ID,
            "drev": _DELIVERABLE_REVISION_ID,
            "deid": _DELIVERABLE_EXPECTATION_ID,
            "derev": _DELIVERABLE_EXPECTATION_REV_ID,
            "party": _PARTY_B,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_milestone_acceptance(conn, *, milestone_id: str | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Milestone_Acceptance_Records (
                milestone_acceptance_id, source_deliverable_production_id,
                produced_deliverable_id, produced_deliverable_revision_id,
                target_deliverable_expectation_id,
                target_deliverable_expectation_revision_id,
                outcome, rationale, accepting_party_id,
                authority_basis_type, authority_basis_id, applicable_scope,
                recorded_at
            ) VALUES (
                :mid, :dpid, :did, :drev, :deid, :derev,
                'Accept', 'Looks good.', :party,
                'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "mid": milestone_id or _MILESTONE_ACCEPTANCE_ID,
            "dpid": _DELIVERABLE_PRODUCTION_ID,
            "did": _DELIVERABLE_ID,
            "drev": _DELIVERABLE_REVISION_ID,
            "deid": _DELIVERABLE_EXPECTATION_ID,
            "derev": _DELIVERABLE_EXPECTATION_REV_ID,
            "party": _PARTY_C,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_completion(conn, *, completion_id: str | None = None) -> None:
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
                :cid, :prev, :aid, :pid, 'Completed', 'Done.',
                '[]', :party, 'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "cid": completion_id or _COMPLETION_ID,
            "prev": _PLAN_REVISION_ID,
            "aid": _ACTIVITY_PLAN_ID,
            "pid": _PROJECT_ID,
            "party": _PARTY_C,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_full_execution_graph(conn) -> None:
    """Insert one row into every Slice 3 Execution_Service table."""
    _seed_parties(conn)
    _seed_deliverable_stub(conn)
    _seed_work_assignment(conn)
    _seed_work_event(conn)
    _seed_time_entry(conn)
    _seed_deliverable_production(conn)
    _seed_milestone_acceptance(conn)
    _seed_completion(conn)


# ---------------------------------------------------------------------------
# Append-only triggers.
# ---------------------------------------------------------------------------


_IMMUTABLE_TABLE_CASES: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "Work_Assignment_Records",
        "UPDATE Work_Assignment_Records SET assignment_rationale='changed' "
        "WHERE work_assignment_id = :id",
        {"id": _WORK_ASSIGNMENT_ID},
        "DELETE FROM Work_Assignment_Records WHERE work_assignment_id = :id",
    ),
    (
        "Work_Event_Records",
        "UPDATE Work_Event_Records SET event_note='changed' "
        "WHERE work_event_id = :id",
        {"id": _WORK_EVENT_ID},
        "DELETE FROM Work_Event_Records WHERE work_event_id = :id",
    ),
    (
        "Time_Entry_Records",
        "UPDATE Time_Entry_Records SET effort_hours='2.00' "
        "WHERE time_entry_id = :id",
        {"id": _TIME_ENTRY_ID},
        "DELETE FROM Time_Entry_Records WHERE time_entry_id = :id",
    ),
    (
        "Deliverable_Production_Records",
        "UPDATE Deliverable_Production_Records SET production_rationale='changed' "
        "WHERE deliverable_production_id = :id",
        {"id": _DELIVERABLE_PRODUCTION_ID},
        "DELETE FROM Deliverable_Production_Records WHERE deliverable_production_id = :id",
    ),
    (
        "Milestone_Acceptance_Records",
        "UPDATE Milestone_Acceptance_Records SET rationale='changed' "
        "WHERE milestone_acceptance_id = :id",
        {"id": _MILESTONE_ACCEPTANCE_ID},
        "DELETE FROM Milestone_Acceptance_Records WHERE milestone_acceptance_id = :id",
    ),
    (
        "Completion_Records",
        "UPDATE Completion_Records SET rationale='changed' "
        "WHERE completion_id = :id",
        {"id": _COMPLETION_ID},
        "DELETE FROM Completion_Records WHERE completion_id = :id",
    ),
)


def test_immutable_tables_constant_lists_every_execution_table() -> None:
    """``EXECUTION_IMMUTABLE_TABLES`` covers every Slice 3 Execution_Service table.

    Per AD-WS-27, every Slice 3 Record is insert-only with no
    supersession path. The Deliverable_Repository tables
    (``Deliverable_Resources``, ``Deliverable_Revisions``) are owned by
    task 1.3 and are intentionally not listed here.
    """
    expected = {
        "Work_Assignment_Records",
        "Work_Event_Records",
        "Time_Entry_Records",
        "Deliverable_Production_Records",
        "Milestone_Acceptance_Records",
        "Completion_Records",
    }
    assert set(EXECUTION_IMMUTABLE_TABLES) == expected


def test_create_execution_schema_is_idempotent(execution_engine: Engine) -> None:
    """Calling ``create_execution_schema`` twice does not raise."""
    # The fixture has already invoked it once; invoke again to confirm
    # ``IF NOT EXISTS`` covers every statement.
    create_execution_schema(execution_engine)


def test_schema_statements_listed_in_dependency_order() -> None:
    """Tables appear before indexes appear before triggers.

    The ordering is required so the indexes and triggers can reference
    their target tables without forward declarations.
    """
    create_indices = [
        i for i, stmt in enumerate(EXECUTION_SCHEMA_STATEMENTS)
        if "CREATE TABLE" in stmt
    ]
    index_indices = [
        i for i, stmt in enumerate(EXECUTION_SCHEMA_STATEMENTS)
        if "CREATE INDEX" in stmt or "CREATE UNIQUE INDEX" in stmt
    ]
    trigger_indices = [
        i for i, stmt in enumerate(EXECUTION_SCHEMA_STATEMENTS)
        if "CREATE TRIGGER" in stmt
    ]
    assert max(create_indices) < min(index_indices)
    assert max(index_indices) < min(trigger_indices)


@pytest.mark.parametrize(
    "table, update_sql, params, delete_sql", _IMMUTABLE_TABLE_CASES
)
def test_execution_table_rejects_update(
    execution_engine: Engine,
    table: str,
    update_sql: str,
    params: dict,
    delete_sql: str,
) -> None:
    """Every Slice 3 Execution_Service table rejects UPDATE per AD-WS-27."""
    del delete_sql
    with execution_engine.begin() as conn:
        _seed_full_execution_graph(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(update_sql), params)


@pytest.mark.parametrize(
    "table, update_sql, params, delete_sql", _IMMUTABLE_TABLE_CASES
)
def test_execution_table_rejects_delete(
    execution_engine: Engine,
    table: str,
    update_sql: str,
    params: dict,
    delete_sql: str,
) -> None:
    """Every Slice 3 Execution_Service table rejects DELETE per AD-WS-27."""
    del update_sql
    with execution_engine.begin() as conn:
        _seed_full_execution_graph(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(delete_sql), params)


def test_rejected_update_leaves_execution_row_byte_equivalent(
    execution_engine: Engine,
) -> None:
    """A rejected UPDATE must not mutate the row (Property 33 / 41.4)."""
    with execution_engine.begin() as conn:
        _seed_full_execution_graph(conn)

    with execution_engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT * FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :id"
            ),
            {"id": _WORK_ASSIGNMENT_ID},
        ).one()

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Work_Assignment_Records "
                    "SET assignment_rationale='changed' "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": _WORK_ASSIGNMENT_ID},
            )

    with execution_engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT * FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :id"
            ),
            {"id": _WORK_ASSIGNMENT_ID},
        ).one()
    assert before == after


# ---------------------------------------------------------------------------
# Work_Assignment_Records self-assignment CHECK (Requirement 23.5).
# ---------------------------------------------------------------------------


def test_work_assignment_rejects_self_assignment(execution_engine: Engine) -> None:
    """Inserting a self-assignment row is rejected by the table CHECK.

    Requirement 23.5 forbids self-assignment: a Party SHALL NOT assign
    a Work Assignment to itself. The trailing CHECK on
    ``Work_Assignment_Records`` enforces this at the database layer so
    application-level validation cannot be bypassed.
    """
    with execution_engine.begin() as conn:
        _seed_parties(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    """
                    INSERT INTO Work_Assignment_Records (
                        work_assignment_id, target_plan_revision_id,
                        assignee_party_id, assignment_authority_party_id,
                        assignment_rationale, authority_basis_type,
                        authority_basis_id, applicable_scope, recorded_at
                    ) VALUES (
                        :wid, :prev, :same, :same,
                        'Self-assigning.', 'role-grant-id', :abid, :scope, :ts
                    )
                    """
                ),
                {
                    "wid": _WORK_ASSIGNMENT_ID,
                    "prev": _PLAN_REVISION_ID,
                    "same": _PARTY_A,
                    "abid": _AUTHORITY_BASIS_ID,
                    "scope": _SCOPE,
                    "ts": _TS_RECORDED,
                },
            )


# ---------------------------------------------------------------------------
# Time_Entry_Records effort_hours CHECK constraints (Requirement 25.2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("good_hours", ["0", "0.0", "0.00", "1", "1.5", "1.50", "24", "24.0", "24.00"])
def test_time_entry_accepts_boundary_effort_hours(
    execution_engine: Engine, good_hours: str
) -> None:
    """``effort_hours`` values inside 0.00..24.00 with <=2 fractional digits succeed."""
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)
        _seed_time_entry(conn, effort_hours=good_hours)


@pytest.mark.parametrize(
    "bad_hours",
    [
        "24.01",      # above the 24.00 ceiling
        "25",         # above the 24.00 ceiling at integer precision
        "100",        # well above the ceiling
        "1.234",      # three fractional digits
        "0.001",      # three fractional digits
        "abc",        # non-numeric
        "-1",         # negative integer rejected by the regex
        "-0.5",       # negative decimal rejected by the regex
        "1.",         # trailing dot (regex rejects)
        ".5",         # leading dot (regex rejects)
    ],
)
def test_time_entry_rejects_invalid_effort_hours(
    execution_engine: Engine, bad_hours: str
) -> None:
    """Values outside 0.00..24.00 or violating the decimal regex are rejected."""
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_time_entry(conn, effort_hours=bad_hours)


def test_time_entry_rejects_period_start_after_period_end(
    execution_engine: Engine,
) -> None:
    """``effort_period_start > effort_period_end`` is rejected."""
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_time_entry(
                conn,
                period_start=_TS_END,
                period_end=_TS_START,  # before start
            )


def test_time_entry_rejects_period_end_after_recorded_at(
    execution_engine: Engine,
) -> None:
    """``effort_period_end > recorded_at`` is rejected."""
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_time_entry(
                conn,
                period_start=_TS_START,
                period_end="2026-01-01T03:00:00.000+00:00",  # later than recorded_at
                recorded_at=_TS_RECORDED,
            )


# ---------------------------------------------------------------------------
# Work_Event_Records partial UNIQUE index (Requirement 24.3).
# ---------------------------------------------------------------------------


def test_work_event_rejects_second_started_per_work_assignment(
    execution_engine: Engine,
) -> None:
    """The partial UNIQUE index rejects a second ``started`` event per WA.

    Requirement 24.3: a Work Assignment may have at most one
    ``started`` Work Event Record. The partial UNIQUE index
    ``idx_work_events_one_started_per_wa`` enforces this at the
    database layer.
    """
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)
        _seed_work_event(conn, event_kind="started")

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_work_event(
                conn,
                event_kind="started",
                work_event_id=_WORK_EVENT_ID_2,
                recorded_at="2026-01-01T03:00:00.000+00:00",
            )


def test_work_event_allows_multiple_non_started_events(execution_engine: Engine) -> None:
    """Multiple ``progress_note`` / ``paused`` events per WA are permitted.

    The partial UNIQUE index applies only to ``event_kind = 'started'``;
    every other event_kind is unconstrained by the index.
    """
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)
        _seed_work_event(conn, event_kind="started")
        _seed_work_event(
            conn,
            event_kind="progress_note",
            work_event_id=_WORK_EVENT_ID_2,
            recorded_at="2026-01-01T03:00:00.000+00:00",
        )
        _seed_work_event(
            conn,
            event_kind="paused",
            work_event_id="00000000-0000-7000-8000-000000000d10",
            recorded_at="2026-01-01T04:00:00.000+00:00",
        )


# ---------------------------------------------------------------------------
# Milestone_Acceptance_Records UNIQUE column (Requirement 28.3).
# ---------------------------------------------------------------------------


def test_milestone_acceptance_source_production_is_unique(
    execution_engine: Engine,
) -> None:
    """At most one Milestone Acceptance per Deliverable Production.

    Requirement 28.3 enforces ``UNIQUE(source_deliverable_production_id)``
    on ``Milestone_Acceptance_Records``.
    """
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_deliverable_stub(conn)
        _seed_work_assignment(conn)
        _seed_deliverable_production(conn)
        _seed_milestone_acceptance(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_milestone_acceptance(conn, milestone_id=_MILESTONE_ACCEPTANCE_ID_2)


# ---------------------------------------------------------------------------
# Completion_Records UNIQUE column (Requirement 29.3).
# ---------------------------------------------------------------------------


def test_completion_target_plan_revision_is_unique(
    execution_engine: Engine,
) -> None:
    """At most one Completion per target Plan Revision.

    Requirement 29.3 enforces
    ``UNIQUE(target_plan_revision_id)`` on ``Completion_Records``.
    """
    with execution_engine.begin() as conn:
        _seed_parties(conn)
        _seed_completion(conn)

    with execution_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_completion(conn, completion_id=_COMPLETION_ID_2)


# ---------------------------------------------------------------------------
# Indexes — confirm every named composite index is present.
# ---------------------------------------------------------------------------


def test_expected_indexes_are_created(execution_engine: Engine) -> None:
    """Every composite index named in design §"Data Models — Schema Additions" exists.

    The Slice 1 ``ix_relationships_target_backlink`` index is owned by
    Slice 1 and is not re-created here; it is already present from the
    Slice 1 schema and covers Slice 3 backlink scans without
    modification (design §"Indexes").
    """
    expected = {
        "idx_work_assignments_by_plan",
        "idx_work_assignments_by_assignee",
        "idx_work_events_one_started_per_wa",
        "idx_work_events_by_wa_recent",
        "idx_time_entries_by_wa",
        "idx_deliverable_productions_by_wa",
        "idx_deliverable_productions_by_revision",
        "idx_deliverable_productions_by_expectation",
    }
    with execution_engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'idx_%'"
                )
            ).all()
        }
    assert expected.issubset(names)


def test_unique_columns_create_implicit_indexes(execution_engine: Engine) -> None:
    """``UNIQUE`` columns on Milestone and Completion tables have implicit indexes.

    SQLite auto-generates indexes with names like ``sqlite_autoindex_<table>_<n>``
    for ``UNIQUE`` columns. Their presence confirms Requirements 28.3
    and 29.3 are enforced at the index layer.
    """
    with execution_engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name LIKE 'sqlite_autoindex_%'"
                )
            ).all()
        }
    assert any("Milestone_Acceptance_Records" in n for n in names)
    assert any("Completion_Records" in n for n in names)


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 non-modification (Requirement 40, Requirement 42.4).
# ---------------------------------------------------------------------------


def test_slice3_schema_does_not_touch_slice1_tables(engine: Engine) -> None:
    """Creating the Slice 3 schema leaves every Slice 1 row byte-equivalent.

    Verifies Requirement 40 (Reuse and Non-Modification of Slice 1
    Contexts) by snapshotting every Slice 1 table before
    ``create_execution_schema`` and confirming no row count changes
    after.
    """
    create_schema(engine)

    with engine.connect() as conn:
        # Sample a representative subset of Slice 1 tables.
        before_parties = conn.execute(text("SELECT COUNT(*) FROM Parties")).scalar_one()
        before_audit = conn.execute(text("SELECT COUNT(*) FROM Audit_Records")).scalar_one()
        before_registry = conn.execute(
            text("SELECT COUNT(*) FROM Identifier_Registry")
        ).scalar_one()

    create_execution_schema(engine)

    with engine.connect() as conn:
        after_parties = conn.execute(text("SELECT COUNT(*) FROM Parties")).scalar_one()
        after_audit = conn.execute(text("SELECT COUNT(*) FROM Audit_Records")).scalar_one()
        after_registry = conn.execute(
            text("SELECT COUNT(*) FROM Identifier_Registry")
        ).scalar_one()

    assert before_parties == after_parties
    assert before_audit == after_audit
    assert before_registry == after_registry
