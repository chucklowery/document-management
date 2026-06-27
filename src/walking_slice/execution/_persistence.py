"""SQLite schema, append-only triggers, and indexes for the Execution_Service.

Design reference: ``.kiro/specs/third-walking-slice/design.md`` §"Data
Models — Schema Additions", §"Indexes", and AD-WS-27 / AD-WS-28.

Responsibilities of this module (task 1.2):

1. Expose :func:`create_execution_schema` that issues every
   ``CREATE TABLE``, ``CREATE INDEX``, and ``CREATE TRIGGER`` statement
   specified in design §"Data Models — Schema Additions" for the Slice 3
   Execution_Service tables:

   - ``Work_Assignment_Records``
   - ``Work_Event_Records``
   - ``Time_Entry_Records``
   - ``Deliverable_Production_Records``
   - ``Milestone_Acceptance_Records``
   - ``Completion_Records``

2. Install ``UPDATE`` and ``DELETE`` rejection triggers on every new
   table, matching the Slice 1 AD-WS-4 / Slice 2 AD-WS-19 patterns and
   honoring Slice 3 AD-WS-27 (Slice 3 Records are append-only with no
   supersession path).

3. Install the table-level ``CHECK`` constraints prescribed by the
   design:

   - ``Work_Assignment_Records``: ``assignee_party_id !=
     assignment_authority_party_id`` (Requirement 23.5 — no
     self-assignment).
   - ``Time_Entry_Records``: ``effort_hours`` decimal regex via
     ``GLOB`` plus the numeric range 0.00..24.00 (Requirement 25.2),
     and the temporal ordering rule
     ``effort_period_start <= effort_period_end <= recorded_at``
     (Requirements 25.3 / 25.5).
   - ``Milestone_Acceptance_Records.source_deliverable_production_id``
     ``UNIQUE`` (Requirement 28.3 — at most one Milestone Acceptance
     per Deliverable Production).
   - ``Completion_Records.target_plan_revision_id`` ``UNIQUE``
     (Requirement 29.3 — at most one Completion per Approved Plan
     Revision).

4. Install the partial ``UNIQUE`` index
   ``idx_work_events_one_started_per_wa`` that enforces at-most-one
   ``'started'`` Work Event per Work Assignment (Requirement 24.3),
   plus every composite index named in design §"Data Models — Schema
   Additions" and §"Indexes":

   - ``idx_work_assignments_by_plan``,
     ``idx_work_assignments_by_assignee``
   - ``idx_work_events_by_wa_recent``
   - ``idx_time_entries_by_wa``
   - ``idx_deliverable_productions_by_wa``,
     ``idx_deliverable_productions_by_revision``,
     ``idx_deliverable_productions_by_expectation``

   The two ``UNIQUE`` columns on ``Milestone_Acceptance_Records`` and
   ``Completion_Records`` already create implicit indexes and are
   therefore not duplicated. The Slice 1
   ``ix_relationships_target_backlink`` index covers Slice 3 backlink
   scans unchanged (design §"Indexes").

Requirements satisfied (per task 1.2):

    23.1  — Work Assignment Record creation persists the
            ``Work_Assignment_Records`` row exposed by this schema.
    23.9  — Work Assignment Records reject ``UPDATE`` / ``DELETE`` via
            append-only triggers (AD-WS-27).
    24.7  — Work Event Records reject ``UPDATE`` / ``DELETE``.
    25.2  — ``Time_Entry_Records.effort_hours`` ``CHECK`` constraint
            (decimal regex + 0.00..24.00 numeric range).
    25.6  — Time Entry Records reject ``UPDATE`` / ``DELETE``.
    27.7  — Deliverable Production Records reject ``UPDATE`` /
            ``DELETE``.
    28.3  — ``Milestone_Acceptance_Records.source_deliverable_production_id``
            ``UNIQUE``.
    28.7  — Milestone Acceptance Records reject ``UPDATE`` /
            ``DELETE``.
    29.3  — ``Completion_Records.target_plan_revision_id`` ``UNIQUE``.
    29.7  — Completion Records reject ``UPDATE`` / ``DELETE``.
    41.4  — Execution-Record immutability invariant (the append-only
            triggers below are the database-level enforcement of
            Property 33).
    41.10 — Slice 3 Records are append-only with no supersession
            path (AD-WS-27).
    42.4  — Slice 3 schema co-exists with Slice 1 + Slice 2 schema in
            one SQLite file; no Slice 1 or Slice 2 table is touched
            by this module.

Notes:

- All identifier columns are ``TEXT`` (canonical UUIDv7 strings) per
  the Slice 1 ``Identifier_Registry`` invariants; the registry's
  UNIQUE constraint enforces non-reuse globally (AD-WS-2).
- All timestamps are stored as ISO-8601 strings with millisecond
  precision; the application layer formats them per design
  §"Cross-Cutting Concerns".
- ``applicable_scope`` is persisted byte-equivalent from the request
  body.
- Foreign-key columns that target the Deliverable_Repository tables
  (``Deliverable_Resources``, ``Deliverable_Revisions``) are declared
  here using SQLite's standard ``REFERENCES`` syntax. SQLite resolves
  FK targets lazily at INSERT/UPDATE time (with
  ``PRAGMA foreign_keys=ON``), so the Deliverable_Repository schema
  in task 1.3 may be created after this one without ordering issues.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Engine


__all__ = [
    "create_execution_schema",
    "EXECUTION_SCHEMA_STATEMENTS",
    "EXECUTION_IMMUTABLE_TABLES",
]


# ---------------------------------------------------------------------------
# Table definitions.
#
# Each statement mirrors design §"Data Models — Schema Additions" verbatim:
# column names, column order, CHECK constraints, FOREIGN KEY references,
# UNIQUE columns, and composite constraints are unchanged from the design's
# SQL listings.
# ---------------------------------------------------------------------------


_TABLE_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Work_Assignment_Records ---------------------------------------
    # Requirements 23.1, 23.3, 23.5, 23.9.
    # The trailing CHECK enforces Requirement 23.5 (no self-assignment).
    """
    CREATE TABLE IF NOT EXISTS Work_Assignment_Records (
        work_assignment_id              TEXT PRIMARY KEY,
        target_plan_revision_id         TEXT NOT NULL,
        assignee_party_id               TEXT NOT NULL REFERENCES Parties(party_id),
        assignment_authority_party_id   TEXT NOT NULL REFERENCES Parties(party_id),
        assignment_rationale            TEXT NULL CHECK (
                                            assignment_rationale IS NULL
                                            OR length(assignment_rationale) BETWEEN 0 AND 4000
                                        ),
        authority_basis_type            TEXT NOT NULL CHECK (
                                            authority_basis_type IN (
                                                'role-grant-id', 'scope-id', 'delegation-chain-id'
                                            )
                                        ),
        authority_basis_id              TEXT NOT NULL,
        applicable_scope                TEXT NOT NULL,
        recorded_at                     TEXT NOT NULL,
        CHECK (assignee_party_id != assignment_authority_party_id)
    )
    """,
    # ----- Work_Event_Records --------------------------------------------
    # Requirements 24.1, 24.2, 24.3, 24.7.
    # The partial UNIQUE index ``idx_work_events_one_started_per_wa``
    # below enforces Requirement 24.3 (at most one 'started' per Work
    # Assignment) at the database layer.
    """
    CREATE TABLE IF NOT EXISTS Work_Event_Records (
        work_event_id                   TEXT PRIMARY KEY,
        target_work_assignment_id       TEXT NOT NULL REFERENCES Work_Assignment_Records(work_assignment_id),
        event_kind                      TEXT NOT NULL CHECK (
                                            event_kind IN (
                                                'started', 'progress_note',
                                                'paused', 'resumed',
                                                'deliverable_drafted'
                                            )
                                        ),
        event_note                      TEXT NULL CHECK (
                                            event_note IS NULL
                                            OR length(event_note) BETWEEN 0 AND 4000
                                        ),
        recording_party_id              TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type            TEXT NOT NULL CHECK (
                                            authority_basis_type IN (
                                                'role-grant-id', 'scope-id', 'delegation-chain-id'
                                            )
                                        ),
        authority_basis_id              TEXT NOT NULL,
        applicable_scope                TEXT NOT NULL,
        recorded_at                     TEXT NOT NULL
    )
    """,
    # ----- Time_Entry_Records --------------------------------------------
    # Requirements 25.1, 25.2, 25.3, 25.5, 25.6.
    #
    # ``effort_hours`` is stored as TEXT so the decimal regex CHECK can
    # be expressed via SQLite ``GLOB`` patterns. The application layer
    # normalises Decimal to two-fractional-digit form before persistence
    # (design §"Effort-quantity validation"). The GLOB alternatives
    # cover the six syntactic shapes admitted by the design's regex
    # ``^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$``:
    #
    #   1 digit              -> '[0-9]'
    #   1 digit . 1 digit    -> '[0-9].[0-9]'
    #   1 digit . 2 digits   -> '[0-9].[0-9][0-9]'
    #   2 digits             -> '[0-9][0-9]'
    #   2 digits . 1 digit   -> '[0-9][0-9].[0-9]'
    #   2 digits . 2 digits  -> '[0-9][0-9].[0-9][0-9]'
    #
    # The numeric-range CHECK enforces 0.00..24.00 (Requirement 25.2);
    # the two ordering CHECKs enforce
    # ``effort_period_start <= effort_period_end <= recorded_at``
    # (Requirements 25.3 / 25.5). Because all timestamps are ISO-8601
    # strings with millisecond precision, lexicographic ``<=`` matches
    # chronological ordering.
    """
    CREATE TABLE IF NOT EXISTS Time_Entry_Records (
        time_entry_id                   TEXT PRIMARY KEY,
        target_work_assignment_id       TEXT NOT NULL REFERENCES Work_Assignment_Records(work_assignment_id),
        effort_hours                    TEXT NOT NULL,
        effort_period_start             TEXT NOT NULL,
        effort_period_end               TEXT NOT NULL,
        recording_party_id              TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type            TEXT NOT NULL CHECK (
                                            authority_basis_type IN (
                                                'role-grant-id', 'scope-id', 'delegation-chain-id'
                                            )
                                        ),
        authority_basis_id              TEXT NOT NULL,
        applicable_scope                TEXT NOT NULL,
        recorded_at                     TEXT NOT NULL,
        CHECK (
            effort_hours GLOB '[0-9]'
            OR effort_hours GLOB '[0-9].[0-9]'
            OR effort_hours GLOB '[0-9].[0-9][0-9]'
            OR effort_hours GLOB '[0-9][0-9]'
            OR effort_hours GLOB '[0-9][0-9].[0-9]'
            OR effort_hours GLOB '[0-9][0-9].[0-9][0-9]'
        ),
        CHECK (CAST(effort_hours AS REAL) >= 0 AND CAST(effort_hours AS REAL) <= 24.00),
        CHECK (effort_period_start <= effort_period_end),
        CHECK (effort_period_end <= recorded_at)
    )
    """,
    # ----- Deliverable_Production_Records --------------------------------
    # Requirements 27.1, 27.2, 27.3, 27.4, 27.7.
    #
    # ``produced_deliverable_id`` and ``produced_deliverable_revision_id``
    # reference the ``Deliverable_Resources`` and ``Deliverable_Revisions``
    # tables created by the Deliverable_Repository schema (task 1.3).
    # SQLite resolves FK targets lazily, so the two schemas may be
    # created in either order.
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Production_Records (
        deliverable_production_id                   TEXT PRIMARY KEY,
        source_work_assignment_id                   TEXT NOT NULL REFERENCES Work_Assignment_Records(work_assignment_id),
        produced_deliverable_id                     TEXT NOT NULL REFERENCES Deliverable_Resources(deliverable_id),
        produced_deliverable_revision_id            TEXT NOT NULL REFERENCES Deliverable_Revisions(deliverable_revision_id),
        target_deliverable_expectation_id           TEXT NOT NULL,
        target_deliverable_expectation_revision_id  TEXT NOT NULL,
        production_rationale                        TEXT NULL CHECK (
                                                        production_rationale IS NULL
                                                        OR length(production_rationale) BETWEEN 0 AND 4000
                                                    ),
        recording_party_id                          TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type                        TEXT NOT NULL CHECK (
                                                        authority_basis_type IN (
                                                            'role-grant-id', 'scope-id', 'delegation-chain-id'
                                                        )
                                                    ),
        authority_basis_id                          TEXT NOT NULL,
        applicable_scope                            TEXT NOT NULL,
        recorded_at                                 TEXT NOT NULL
    )
    """,
    # ----- Milestone_Acceptance_Records ----------------------------------
    # Requirements 28.1, 28.2, 28.3, 28.7.
    #
    # ``source_deliverable_production_id UNIQUE`` enforces Requirement
    # 28.3 (at most one Milestone Acceptance per Deliverable
    # Production). The implicit UNIQUE index covers every lookup keyed
    # on ``source_deliverable_production_id`` so no separate index is
    # required.
    """
    CREATE TABLE IF NOT EXISTS Milestone_Acceptance_Records (
        milestone_acceptance_id                     TEXT PRIMARY KEY,
        source_deliverable_production_id            TEXT NOT NULL UNIQUE REFERENCES Deliverable_Production_Records(deliverable_production_id),
        produced_deliverable_id                     TEXT NOT NULL REFERENCES Deliverable_Resources(deliverable_id),
        produced_deliverable_revision_id            TEXT NOT NULL REFERENCES Deliverable_Revisions(deliverable_revision_id),
        target_deliverable_expectation_id           TEXT NOT NULL,
        target_deliverable_expectation_revision_id  TEXT NOT NULL,
        outcome                                     TEXT NOT NULL CHECK (
                                                        outcome IN ('Accept', 'Reject')
                                                    ),
        rationale                                   TEXT NOT NULL CHECK (
                                                        length(rationale) BETWEEN 1 AND 4000
                                                    ),
        accepting_party_id                          TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type                        TEXT NOT NULL CHECK (
                                                        authority_basis_type IN (
                                                            'role-grant-id', 'scope-id', 'delegation-chain-id'
                                                        )
                                                    ),
        authority_basis_id                          TEXT NOT NULL,
        applicable_scope                            TEXT NOT NULL,
        recorded_at                                 TEXT NOT NULL
    )
    """,
    # ----- Completion_Records --------------------------------------------
    # Requirements 29.1, 29.2, 29.3, 29.5, 29.7.
    #
    # ``target_plan_revision_id UNIQUE`` enforces Requirement 29.3 (at
    # most one Completion per Approved Plan Revision). The implicit
    # UNIQUE index covers every lookup keyed on
    # ``target_plan_revision_id``.
    #
    # ``source_milestone_acceptance_ids_json`` carries the optional
    # list of source Milestone Acceptance Identities passed by the
    # caller per design §"Execution_Service.Completions"; the
    # application layer validates each entry resolves to an
    # ``Accept``-outcome Milestone Acceptance Record before the
    # INSERT.
    """
    CREATE TABLE IF NOT EXISTS Completion_Records (
        completion_id                          TEXT PRIMARY KEY,
        target_plan_revision_id                TEXT NOT NULL UNIQUE,
        target_activity_plan_id                TEXT NOT NULL,
        target_project_id                      TEXT NOT NULL,
        outcome                                TEXT NOT NULL CHECK (
                                                   outcome IN (
                                                       'Completed',
                                                       'Completed_With_Reservation'
                                                   )
                                               ),
        rationale                              TEXT NOT NULL CHECK (
                                                   length(rationale) BETWEEN 1 AND 4000
                                               ),
        source_milestone_acceptance_ids_json   TEXT NOT NULL,
        completing_party_id                    TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type                   TEXT NOT NULL CHECK (
                                                   authority_basis_type IN (
                                                       'role-grant-id', 'scope-id', 'delegation-chain-id'
                                                   )
                                               ),
        authority_basis_id                     TEXT NOT NULL,
        applicable_scope                       TEXT NOT NULL,
        recorded_at                            TEXT NOT NULL
    )
    """,
)


# ---------------------------------------------------------------------------
# Index definitions.
#
# Every composite index named in design §"Data Models — Schema Additions"
# and §"Indexes" is included. The two ``UNIQUE`` columns
# (``Milestone_Acceptance_Records.source_deliverable_production_id`` and
# ``Completion_Records.target_plan_revision_id``) already carry implicit
# indexes and are intentionally not duplicated here.
# ---------------------------------------------------------------------------


_INDEX_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Work_Assignment_Records ---------------------------------------
    # Covers Plan-Revision-keyed and assignee-keyed lookups per design
    # §"Data Models — Schema Additions / Work_Assignment_Records".
    """
    CREATE INDEX IF NOT EXISTS idx_work_assignments_by_plan
        ON Work_Assignment_Records (target_plan_revision_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_work_assignments_by_assignee
        ON Work_Assignment_Records (assignee_party_id, recorded_at)
    """,
    # ----- Work_Event_Records --------------------------------------------
    # Partial UNIQUE index enforces at-most-one 'started' event per
    # Work Assignment (Requirement 24.3).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_work_events_one_started_per_wa
        ON Work_Event_Records (target_work_assignment_id)
        WHERE event_kind = 'started'
    """,
    # Covers the indexed ``(target_work_assignment_id, recorded_at DESC)``
    # query used by ``WorkEventService.create_work_event`` to enforce the
    # per-Work-Assignment event-kind state machine (design
    # §"Event-kind state machine").
    """
    CREATE INDEX IF NOT EXISTS idx_work_events_by_wa_recent
        ON Work_Event_Records (target_work_assignment_id, recorded_at DESC, event_kind)
    """,
    # ----- Time_Entry_Records --------------------------------------------
    """
    CREATE INDEX IF NOT EXISTS idx_time_entries_by_wa
        ON Time_Entry_Records (target_work_assignment_id, recorded_at)
    """,
    # ----- Deliverable_Production_Records --------------------------------
    """
    CREATE INDEX IF NOT EXISTS idx_deliverable_productions_by_wa
        ON Deliverable_Production_Records (source_work_assignment_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliverable_productions_by_revision
        ON Deliverable_Production_Records (produced_deliverable_revision_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliverable_productions_by_expectation
        ON Deliverable_Production_Records (target_deliverable_expectation_revision_id, recorded_at)
    """,
)


# ---------------------------------------------------------------------------
# Trigger definitions.
#
# Every Slice 3 Execution_Service table is insert-only per AD-WS-27. The
# triggers below match the Slice 1 AD-WS-4 / Slice 2 AD-WS-19 pattern:
# ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers abort with a
# descriptive message. ``RAISE(ABORT, ...)`` rolls back the offending
# statement (and its enclosing transaction) and surfaces through the
# DBAPI as :class:`sqlite3.IntegrityError`, which SQLAlchemy wraps as
# :class:`sqlalchemy.exc.IntegrityError`.
# ---------------------------------------------------------------------------


EXECUTION_IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "Work_Assignment_Records",
    "Work_Event_Records",
    "Time_Entry_Records",
    "Deliverable_Production_Records",
    "Milestone_Acceptance_Records",
    "Completion_Records",
)
"""Slice 3 Execution_Service tables whose UPDATE/DELETE are rejected.

Every Slice 3 Record is insert-only with no supersession path
(AD-WS-27). The companion Deliverable_Repository tables
(``Deliverable_Resources``, ``Deliverable_Revisions``) are append-only
under the same architectural decision but are owned by task 1.3 and
therefore not listed here.
"""


def _build_immutable_triggers() -> tuple[str, ...]:
    """Build the AD-WS-27-style UPDATE/DELETE rejection triggers.

    For each table in :data:`EXECUTION_IMMUTABLE_TABLES`, emit one
    ``BEFORE UPDATE`` and one ``BEFORE DELETE`` trigger that abort with
    a descriptive message. ``RAISE(ABORT, ...)`` rolls back the
    offending statement (and its enclosing transaction) and surfaces
    through the DBAPI as :class:`sqlite3.IntegrityError`, which
    SQLAlchemy wraps as :class:`sqlalchemy.exc.IntegrityError`.
    """
    statements: list[str] = []
    for table in EXECUTION_IMMUTABLE_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; UPDATE rejected per design AD-WS-27 / AD-WS-28.');
            END
            """
        )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; DELETE rejected per design AD-WS-27 / AD-WS-28.');
            END
            """
        )
    return tuple(statements)


def _build_schema_statements() -> tuple[str, ...]:
    """Concatenate tables → indexes → triggers in dependency order."""
    return (
        *_TABLE_STATEMENTS,
        *_INDEX_STATEMENTS,
        *_build_immutable_triggers(),
    )


EXECUTION_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = _build_schema_statements()
"""Ordered tuple of every DDL statement issued by :func:`create_execution_schema`.

Exported for tests and for introspection by the FastAPI startup hook
in task 13. The order is tables → indexes → append-only triggers, so
foreign-key targets within the Execution_Service set exist before the
referring tables are created. Foreign keys that target Slice 1, Slice
2, or Deliverable_Repository tables are resolved lazily at INSERT
time, so those schemas may be created before or after this one.
"""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def create_execution_schema(engine: Engine) -> None:
    """Create every Slice 3 Execution_Service table, index, and trigger.

    The function is idempotent: every statement uses ``IF NOT EXISTS``
    so it is safe to call against an already-initialised database (the
    typical pattern in tests and in the FastAPI startup hook from
    task 13).

    The caller is expected to have already invoked
    :func:`walking_slice.persistence.create_schema` so that the Slice 1
    tables referenced by foreign keys (``Parties``) are present. The
    Deliverable_Repository tables (``Deliverable_Resources``,
    ``Deliverable_Revisions``) may be created either before or after
    this function — SQLite resolves FK targets lazily at INSERT time
    when ``PRAGMA foreign_keys=ON`` is set.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database.
    """
    # ``engine.begin()`` opens an IMMEDIATE transaction so partial DDL
    # cannot leave the database in an inconsistent state if a later
    # CREATE fails.
    with engine.begin() as conn:
        for statement in EXECUTION_SCHEMA_STATEMENTS:
            conn.execute(text(statement))
