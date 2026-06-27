"""SQLite schema, append-only triggers, and indexes for the Planning_Service.

Design reference: ``.kiro/specs/second-walking-slice/design.md`` §"Data Models
— Schema Additions", §"Indexes", and AD-WS-4 / AD-WS-19 / AD-WS-20.

Responsibilities of this module:

1. Expose :func:`create_planning_schema` that issues every ``CREATE TABLE``,
   ``CREATE INDEX``, and ``CREATE TRIGGER`` statement specified in design
   §"Data Models — Schema Additions" for the Slice 2 tables:
   ``Objectives``, ``Objective_Revisions``, ``Intended_Outcomes``,
   ``Intended_Outcome_Revisions``, ``Projects``, ``Project_Revisions``,
   ``Deliverable_Expectations``, ``Deliverable_Expectation_Revisions``,
   ``Activity_Plans``, ``Plan_Revisions``, ``Plan_Reviews``,
   ``Plan_Review_Revisions``, ``Plan_Approval_Records``, and
   ``Disclosure_Policy_Coverage``.
2. Install ``UPDATE`` and ``DELETE`` rejection triggers on every new table,
   matching the Slice 1 AD-WS-4 pattern.
3. Install the special ``Plan_Revisions`` ``UPDATE`` trigger (AD-WS-19): it
   permits exactly the lifecycle transition ``('draft','approved')`` when
   the connection-scoped pragma ``walking_slice.plan_approval_in_progress``
   is set, and rejects every other ``UPDATE`` — including attempts to
   mutate any non-lifecycle column.
4. Install every composite index named in design §"Indexes".
5. Expose helpers (:func:`set_plan_approval_in_progress`,
   :func:`clear_plan_approval_in_progress`) used by Planning_Service to
   open and close the session-scoped pragma window inside the Plan Approval
   transaction. The pragma is implemented as a per-connection ``TEMP`` table
   (``temp.walking_slice_session_state``) created on every new DBAPI
   connection via a SQLAlchemy ``connect`` event listener; SQLite does not
   permit user-defined dotted ``PRAGMA`` names, so a connection-private
   TEMP table is the idiomatic stand-in and is referenced verbatim by the
   ``Plan_Revisions`` trigger's ``WHEN`` clause.

Requirements satisfied (per task 1.3):
    9.4   — approved Plan Revisions are byte-equivalent forever
            (Plan_Revisions UPDATE trigger + Plan_Approval_Records DELETE
            rejection + Relationships immutability inherited from Slice 1).
    12.5  — every new Planning Resource and Revision row is append-only.
    13.6  — Intended_Outcome_Revisions.outcome_kind CHECK plus append-only
            triggers prevent any execution / observed-outcome mutation.
    16.3  — Slice 2 interim ADR rows reference the same Interim_ADR_Records
            table; the new ``Disclosure_Policy_Coverage`` table is the
            additive surface that lets the Slice 2 backlog ADRs be recorded
            without modifying Slice 1's policy row.
    19.4  — every Slice 1 row remains byte-equivalent: the Slice 2 schema
            is created in its own statements and does not touch any Slice 1
            table.
    20.4  — Approved Plan Revision immutability: the Plan_Revisions UPDATE
            trigger permits exactly the documented one transition.
    20.11 — Slice 1 non-modification: nothing here references a Slice 1
            table for ``CREATE``, ``DROP``, ``ALTER`` (the two additive
            Slice 1 columns are applied by ``walking_slice.persistence`` in
            task 1.2 and read by this schema only via foreign-key targets).

Notes:
- All identifier columns are ``TEXT`` (canonical UUIDv7 strings) per the
  Slice 1 ``Identifier_Registry`` invariants; the registry's UNIQUE
  constraint enforces non-reuse globally (AD-WS-2).
- All timestamps are stored as ISO-8601 strings with millisecond precision;
  the application layer formats them per design §"Cross-Cutting Concerns".
- ``applicable_scope`` is persisted byte-equivalent from the request body.
"""

from __future__ import annotations

from typing import Final
from weakref import WeakSet

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import Connection


__all__ = [
    "create_planning_schema",
    "install_planning_session_state",
    "set_plan_approval_in_progress",
    "clear_plan_approval_in_progress",
    "PLAN_APPROVAL_PRAGMA_KEY",
    "SESSION_STATE_TEMP_TABLE",
    "PLANNING_SCHEMA_STATEMENTS",
    "PLANNING_IMMUTABLE_TABLES",
]


# ---------------------------------------------------------------------------
# Session-scoped pragma backing.
#
# AD-WS-19 specifies that the special ``Plan_Revisions`` UPDATE trigger
# fires only when the "session pragma" ``walking_slice.plan_approval_in_progress``
# is set to the current correlation identifier. SQLite does not support
# user-defined dotted PRAGMA names, so the pragma is implemented as a
# connection-private ``TEMP`` table whose contents are visible to triggers
# fired on the same connection and invisible to every other connection.
# This is the idiomatic SQLite stand-in for connection-scoped session state
# and is referenced verbatim by the ``Plan_Revisions`` trigger's WHEN
# clause below.
# ---------------------------------------------------------------------------


SESSION_STATE_TEMP_TABLE: Final[str] = "walking_slice_session_state"
"""Name of the connection-private TEMP table backing the planning pragma."""

PLAN_APPROVAL_PRAGMA_KEY: Final[str] = "plan_approval_in_progress"
"""Key under which Planning_Service records the in-flight correlation id."""


_SESSION_STATE_DDL: Final[str] = f"""
CREATE TABLE IF NOT EXISTS temp.{SESSION_STATE_TEMP_TABLE} (
    key   TEXT PRIMARY KEY,
    value TEXT
)
"""


# The Plan_Revisions ``UPDATE`` trigger is installed as a ``TEMP TRIGGER``
# rather than a persistent main-schema trigger. SQLite forbids
# main-schema triggers from referencing ``temp.*`` objects (see
# https://www.sqlite.org/lang_createtrigger.html — "no schema other than
# the one in which the trigger is created may be referenced"), but
# ``temp`` triggers may reference both ``temp.*`` and ``main.*`` and may
# fire on UPDATE statements against tables in any attached database.
#
# Re-creating the trigger on every new DBAPI connection is consistent with
# the table being a TEMP table: both objects live in the connection's
# private TEMP schema and disappear when the connection closes, so the
# trigger and its backing table have the same lifetime by construction.
_PLAN_REVISIONS_LIFECYCLE_TRIGGER_DDL: Final[str] = f"""
CREATE TEMP TRIGGER IF NOT EXISTS Plan_Revisions_lifecycle_transition_only
BEFORE UPDATE ON main.Plan_Revisions
WHEN
       -- Reject any UPDATE that touches a column other than lifecycle_state.
       OLD.plan_revision_id                    IS NOT NEW.plan_revision_id
    OR OLD.activity_plan_id                    IS NOT NEW.activity_plan_id
    OR OLD.predecessor_revision_id             IS NOT NEW.predecessor_revision_id
    OR OLD.planned_scope                       IS NOT NEW.planned_scope
    OR OLD.deliverable_expectation_refs_json   IS NOT NEW.deliverable_expectation_refs_json
    OR OLD.planning_assumptions_json           IS NOT NEW.planning_assumptions_json
    OR OLD.ordering_rationale                  IS NOT NEW.ordering_rationale
    OR OLD.authoring_party_id                  IS NOT NEW.authoring_party_id
    OR OLD.applicable_scope                    IS NOT NEW.applicable_scope
    OR OLD.recorded_at                         IS NOT NEW.recorded_at
       -- Reject any lifecycle_state change other than draft -> approved.
    OR NOT (
           OLD.lifecycle_state = 'draft'
           AND NEW.lifecycle_state = 'approved'
       )
       -- Reject the permitted transition when the session pragma is unset.
    OR NOT EXISTS (
           SELECT 1
             FROM temp.{SESSION_STATE_TEMP_TABLE}
            WHERE key = '{PLAN_APPROVAL_PRAGMA_KEY}'
              AND value IS NOT NULL
       )
BEGIN
    SELECT RAISE(ABORT,
        'Plan_Revisions UPDATE rejected: only the draft->approved lifecycle transition is permitted, and only while the walking_slice.plan_approval_in_progress session pragma is set (AD-WS-19 / AD-WS-20).');
END
"""


_engines_with_session_state: "WeakSet[Engine]" = WeakSet()


def install_planning_session_state(engine: Engine) -> None:
    """Register a ``connect`` listener that materialises the session-state TEMP table and trigger.

    Every new DBAPI connection opened against the engine receives:

    1. The ``temp.walking_slice_session_state`` TEMP table backing the
       ``walking_slice.plan_approval_in_progress`` pragma.
    2. The ``Plan_Revisions_lifecycle_transition_only`` TEMP trigger that
       gates the single AD-WS-19 lifecycle transition.

    Both objects live in the connection-private TEMP schema and share its
    lifetime. The listener is idempotent per engine: repeated calls
    register at most one event handler per engine instance (mirroring
    :func:`walking_slice.persistence.install_pragmas`).

    The ``Plan_Revisions`` table itself must exist in the ``main`` schema
    before the trigger is created on a connection. Within
    :func:`create_planning_schema` the table is created in the same
    transaction that the schema-setup connection later uses to install
    the trigger; for connections acquired after schema setup, the table
    has already been committed and the trigger compiles cleanly.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database.
    """
    if engine in _engines_with_session_state:
        return

    @event.listens_for(engine, "connect")
    def _create_session_state(dbapi_connection, _connection_record) -> None:  # pragma: no cover - hit on every connect
        cur = dbapi_connection.cursor()
        try:
            cur.execute(_SESSION_STATE_DDL)
            # Skip the trigger if Plan_Revisions has not yet been created;
            # ``create_planning_schema`` will install the trigger inside
            # its own DDL transaction once the table exists. This makes
            # the listener safe for the bootstrap connection that runs
            # before ``CREATE TABLE Plan_Revisions``.
            row = cur.execute(
                "SELECT 1 FROM main.sqlite_master "
                "WHERE type='table' AND name='Plan_Revisions'"
            ).fetchone()
            if row is not None:
                cur.execute(_PLAN_REVISIONS_LIFECYCLE_TRIGGER_DDL)
        finally:
            cur.close()

    _engines_with_session_state.add(engine)


def set_plan_approval_in_progress(connection: Connection, correlation_id: str) -> None:
    """Set ``walking_slice.plan_approval_in_progress`` to ``correlation_id``.

    Called by :class:`PlanApprovalService` (task 11) at the start of the
    Plan Approval transaction; the value remains visible to the
    ``Plan_Revisions`` UPDATE trigger only for the duration of the same
    connection (the TEMP table is connection-private). The companion call
    :func:`clear_plan_approval_in_progress` removes the value at the end of
    the transaction.

    Args:
        connection: The SQLAlchemy ``Connection`` carrying the Plan Approval
            transaction.
        correlation_id: The correlation identifier for the in-flight
            transaction; persisted as the pragma value so that audit and
            denial entries can be reconciled against the transaction.
    """
    connection.execute(
        text(
            f"INSERT INTO temp.{SESSION_STATE_TEMP_TABLE} (key, value) "
            f"VALUES (:key, :value) "
            f"ON CONFLICT(key) DO UPDATE SET value = :value"
        ),
        {"key": PLAN_APPROVAL_PRAGMA_KEY, "value": correlation_id},
    )


def clear_plan_approval_in_progress(connection: Connection) -> None:
    """Clear ``walking_slice.plan_approval_in_progress`` on this connection.

    Called by :class:`PlanApprovalService` (task 11) immediately after the
    one permitted ``Plan_Revisions`` lifecycle UPDATE — the pragma window
    is intentionally as small as possible so that any other UPDATE attempt
    on the same connection (within or outside the Plan Approval
    transaction) is rejected by the trigger.
    """
    connection.execute(
        text(
            f"DELETE FROM temp.{SESSION_STATE_TEMP_TABLE} "
            f"WHERE key = :key"
        ),
        {"key": PLAN_APPROVAL_PRAGMA_KEY},
    )


# ---------------------------------------------------------------------------
# Table definitions.
#
# Each statement mirrors design §"Data Models — Schema Additions" verbatim:
# column names, column order, CHECK constraints, FOREIGN KEY references, and
# composite constraints are unchanged from the design's SQL listings.
# ---------------------------------------------------------------------------


_TABLE_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Objectives + Objective_Revisions ------------------------------
    """
    CREATE TABLE IF NOT EXISTS Objectives (
        objective_id   TEXT PRIMARY KEY,
        created_at     TEXT NOT NULL CHECK (length(created_at) >= 20)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Objective_Revisions (
        objective_revision_id  TEXT PRIMARY KEY,
        objective_id           TEXT NOT NULL REFERENCES Objectives(objective_id),
        parent_revision_id     TEXT NULL REFERENCES Objective_Revisions(objective_revision_id),
        statement              TEXT NOT NULL CHECK (length(statement) BETWEEN 1 AND 4000),
        rationale              TEXT NULL CHECK (
                                   rationale IS NULL
                                   OR length(rationale) BETWEEN 0 AND 10000
                               ),
        target_decision_id     TEXT NOT NULL,
        authoring_party_id     TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope       TEXT NOT NULL,
        recorded_at            TEXT NOT NULL
    )
    """,
    # ----- Intended_Outcomes + Intended_Outcome_Revisions ----------------
    """
    CREATE TABLE IF NOT EXISTS Intended_Outcomes (
        intended_outcome_id  TEXT PRIMARY KEY,
        created_at           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Intended_Outcome_Revisions (
        intended_outcome_revision_id  TEXT PRIMARY KEY,
        intended_outcome_id     TEXT NOT NULL REFERENCES Intended_Outcomes(intended_outcome_id),
        parent_revision_id      TEXT NULL,
        outcome_kind            TEXT NOT NULL CHECK (outcome_kind = 'intended'),
        target_objective_id     TEXT NOT NULL REFERENCES Objectives(objective_id),
        success_condition       TEXT NOT NULL CHECK (length(success_condition) BETWEEN 1 AND 4000),
        observation_window      TEXT NULL CHECK (
                                    observation_window IS NULL
                                    OR length(observation_window) BETWEEN 0 AND 1000
                                ),
        attribution_assumption  TEXT NULL CHECK (
                                    attribution_assumption IS NULL
                                    OR length(attribution_assumption) BETWEEN 0 AND 4000
                                ),
        authoring_party_id      TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope        TEXT NOT NULL,
        recorded_at             TEXT NOT NULL
    )
    """,
    # ----- Projects + Project_Revisions ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS Projects (
        project_id  TEXT PRIMARY KEY,
        created_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Project_Revisions (
        project_revision_id  TEXT PRIMARY KEY,
        project_id           TEXT NOT NULL REFERENCES Projects(project_id),
        parent_revision_id   TEXT NULL,
        name                 TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
        summary              TEXT NULL CHECK (
                                 summary IS NULL
                                 OR length(summary) BETWEEN 0 AND 4000
                             ),
        target_objective_id  TEXT NOT NULL REFERENCES Objectives(objective_id),
        planned_start_date   TEXT NOT NULL,
        planned_end_date     TEXT NOT NULL,
        authoring_party_id   TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope     TEXT NOT NULL,
        recorded_at          TEXT NOT NULL,
        CHECK (planned_start_date <= planned_end_date)
    )
    """,
    # ----- Deliverable_Expectations + Revisions --------------------------
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Expectations (
        deliverable_expectation_id  TEXT PRIMARY KEY,
        created_at                  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Expectation_Revisions (
        deliverable_expectation_revision_id  TEXT PRIMARY KEY,
        deliverable_expectation_id  TEXT NOT NULL REFERENCES Deliverable_Expectations(deliverable_expectation_id),
        parent_revision_id          TEXT NULL,
        target_project_id           TEXT NOT NULL REFERENCES Projects(project_id),
        name                        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
        description                 TEXT NULL CHECK (
                                        description IS NULL
                                        OR length(description) BETWEEN 0 AND 10000
                                    ),
        deliverable_kind            TEXT NOT NULL CHECK (
                                        deliverable_kind IN ('Document','Artifact','Service','Other')
                                    ),
        acceptance_criteria         TEXT NULL CHECK (
                                        acceptance_criteria IS NULL
                                        OR length(acceptance_criteria) BETWEEN 0 AND 10000
                                    ),
        authoring_party_id          TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope            TEXT NOT NULL,
        recorded_at                 TEXT NOT NULL
    )
    """,
    # ----- Activity_Plans -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Activity_Plans (
        activity_plan_id    TEXT PRIMARY KEY,
        target_project_id   TEXT NOT NULL REFERENCES Projects(project_id),
        title               TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
        authoring_party_id  TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope    TEXT NOT NULL,
        recorded_at         TEXT NOT NULL
    )
    """,
    # ----- Plan_Revisions -------------------------------------------------
    # The UPDATE trigger below (Plan_Revisions_lifecycle_transition_only)
    # is the AD-WS-19 exception: it permits exactly the draft->approved
    # transition when the session-scoped pragma is set, and rejects every
    # other UPDATE — including attempts to mutate any non-lifecycle column.
    """
    CREATE TABLE IF NOT EXISTS Plan_Revisions (
        plan_revision_id                   TEXT PRIMARY KEY,
        activity_plan_id                   TEXT NOT NULL REFERENCES Activity_Plans(activity_plan_id),
        predecessor_revision_id            TEXT NULL REFERENCES Plan_Revisions(plan_revision_id),
        lifecycle_state                    TEXT NOT NULL CHECK (
                                               lifecycle_state IN ('draft','approved')
                                           ),
        planned_scope                      TEXT NOT NULL CHECK (
                                               length(planned_scope) BETWEEN 1 AND 10000
                                           ),
        deliverable_expectation_refs_json  TEXT NOT NULL,
        planning_assumptions_json          TEXT NOT NULL,
        ordering_rationale                 TEXT NULL CHECK (
                                               ordering_rationale IS NULL
                                               OR length(ordering_rationale) BETWEEN 0 AND 2000
                                           ),
        authoring_party_id                 TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope                   TEXT NOT NULL,
        recorded_at                        TEXT NOT NULL
    )
    """,
    # ----- Plan_Reviews + Plan_Review_Revisions --------------------------
    """
    CREATE TABLE IF NOT EXISTS Plan_Reviews (
        plan_review_id  TEXT PRIMARY KEY,
        created_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Plan_Review_Revisions (
        plan_review_revision_id  TEXT PRIMARY KEY,
        plan_review_id           TEXT NOT NULL REFERENCES Plan_Reviews(plan_review_id),
        target_plan_revision_id  TEXT NOT NULL REFERENCES Plan_Revisions(plan_revision_id),
        outcome                  TEXT NOT NULL CHECK (
                                     outcome IN ('Endorse','Changes_Requested','Reject')
                                 ),
        rationale                TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 10000),
        reviewing_party_id       TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type     TEXT NOT NULL CHECK (
                                     authority_basis_type IN (
                                         'role-grant-id', 'scope-id', 'delegation-chain-id'
                                     )
                                 ),
        authority_basis_id       TEXT NOT NULL,
        applicable_scope         TEXT NOT NULL,
        recorded_at              TEXT NOT NULL
    )
    """,
    # ----- Plan_Approval_Records -----------------------------------------
    # ``UNIQUE(target_plan_revision_id)`` is the source of truth for
    # Requirement 9.5 (at most one Plan Approval per Plan Revision).
    """
    CREATE TABLE IF NOT EXISTS Plan_Approval_Records (
        plan_approval_id         TEXT PRIMARY KEY,
        target_activity_plan_id  TEXT NOT NULL REFERENCES Activity_Plans(activity_plan_id),
        target_plan_revision_id  TEXT NOT NULL UNIQUE REFERENCES Plan_Revisions(plan_revision_id),
        outcome                  TEXT NOT NULL CHECK (
                                     outcome IN ('Approve','Reject_Approval')
                                 ),
        rationale                TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
        approving_party_id       TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type     TEXT NOT NULL CHECK (
                                     authority_basis_type IN (
                                         'role-grant-id', 'scope-id', 'delegation-chain-id'
                                     )
                                 ),
        authority_basis_id       TEXT NOT NULL,
        applicable_scope         TEXT NOT NULL,
        recorded_at              TEXT NOT NULL
    )
    """,
    # ----- Disclosure_Policy_Coverage ------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Disclosure_Policy_Coverage (
        policy_id       TEXT NOT NULL REFERENCES Disclosure_Policies(policy_id),
        node_kind       TEXT NOT NULL,
        recorded_at     TEXT NOT NULL,
        backlog_adr_id  TEXT NOT NULL,
        PRIMARY KEY (policy_id, node_kind)
    )
    """,
)


# ---------------------------------------------------------------------------
# Index definitions.
#
# Every composite index named in design §"Indexes" is included; the
# ``Plan_Approval_Records.target_plan_revision_id`` UNIQUE column already
# carries an implicit index, so it is intentionally not duplicated here.
# ---------------------------------------------------------------------------


_INDEX_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE INDEX IF NOT EXISTS idx_objective_revisions_by_objective
        ON Objective_Revisions (objective_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_intended_outcome_revisions_by_objective
        ON Intended_Outcome_Revisions (target_objective_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_project_revisions_by_objective
        ON Project_Revisions (target_objective_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliverable_expectation_revisions_by_project
        ON Deliverable_Expectation_Revisions (target_project_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_activity_plans_by_project
        ON Activity_Plans (target_project_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_revisions_by_activity_plan
        ON Plan_Revisions (activity_plan_id, lifecycle_state, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_plan_review_revisions_by_target
        ON Plan_Review_Revisions (target_plan_revision_id, recorded_at)
    """,
)


# ---------------------------------------------------------------------------
# Trigger definitions.
#
# Every Slice 2 table is insert-only (AD-WS-19, mirroring Slice 1 AD-WS-4),
# with the one exception of ``Plan_Revisions.lifecycle_state`` which may
# transition exactly once from ``'draft'`` to ``'approved'`` during a
# Plan Approval transaction.
# ---------------------------------------------------------------------------


PLANNING_IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "Objectives",
    "Objective_Revisions",
    "Intended_Outcomes",
    "Intended_Outcome_Revisions",
    "Projects",
    "Project_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    "Activity_Plans",
    "Plan_Reviews",
    "Plan_Review_Revisions",
    "Plan_Approval_Records",
    "Disclosure_Policy_Coverage",
)
"""Slice 2 tables whose UPDATE/DELETE are rejected unconditionally.

``Plan_Revisions`` is intentionally excluded — it has its own pair of
triggers below that admit the single AD-WS-19 lifecycle transition.
"""


def _build_immutable_triggers() -> tuple[str, ...]:
    """Build the AD-WS-4-style UPDATE/DELETE rejection triggers.

    For each table in :data:`PLANNING_IMMUTABLE_TABLES`, emit one
    ``BEFORE UPDATE`` and one ``BEFORE DELETE`` trigger that abort with a
    descriptive message. ``RAISE(ABORT, ...)`` rolls back the offending
    statement (and its enclosing transaction) and surfaces through the
    DBAPI as :class:`sqlite3.IntegrityError`, which SQLAlchemy wraps as
    :class:`sqlalchemy.exc.IntegrityError`.
    """
    statements: list[str] = []
    for table in PLANNING_IMMUTABLE_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; UPDATE rejected per design AD-WS-4 / AD-WS-19.');
            END
            """
        )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; DELETE rejected per design AD-WS-4 / AD-WS-19.');
            END
            """
        )
    return tuple(statements)


# Plan_Revisions has the single AD-WS-19 exception.
#
# DELETE is always rejected, matching every other planning table; the
# trigger lives in the main schema and is persistent (no temp-schema
# reference).
#
# The companion UPDATE trigger (``Plan_Revisions_lifecycle_transition_only``)
# is installed as a per-connection ``TEMP`` trigger by
# :func:`install_planning_session_state` because it references
# ``temp.walking_slice_session_state`` and SQLite forbids main-schema
# triggers from referencing the temp schema. The two triggers together
# implement AD-WS-19: every UPDATE is rejected unless its only change is
# ``lifecycle_state`` transitioning from ``'draft'`` to ``'approved'`` and
# the connection-scoped pragma ``walking_slice.plan_approval_in_progress``
# is set.
_PLAN_REVISIONS_TRIGGERS: Final[tuple[str, ...]] = (
    """
    CREATE TRIGGER IF NOT EXISTS Plan_Revisions_reject_delete
    BEFORE DELETE ON Plan_Revisions
    BEGIN
        SELECT RAISE(ABORT,
            'Plan_Revisions is append-only; DELETE rejected per design AD-WS-4 / AD-WS-19.');
    END
    """,
)


def _build_schema_statements() -> tuple[str, ...]:
    """Concatenate tables → indexes → triggers in dependency order."""
    return (
        *_TABLE_STATEMENTS,
        *_INDEX_STATEMENTS,
        *_build_immutable_triggers(),
        *_PLAN_REVISIONS_TRIGGERS,
    )


PLANNING_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = _build_schema_statements()
"""Ordered tuple of every DDL statement issued by :func:`create_planning_schema`.

Exported for tests and for introspection by the FastAPI startup hook in
task 15.2. The order is tables → indexes → standard rejection triggers →
``Plan_Revisions`` triggers, so foreign-key targets exist before the
referring tables are created and so the session-state temp table is
already in place when the ``Plan_Revisions`` trigger compiles.
"""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def create_planning_schema(engine: Engine) -> None:
    """Create every Slice 2 table, index, and trigger.

    The function is idempotent: every statement uses ``IF NOT EXISTS`` so it
    is safe to call against an already-initialised database (the typical
    pattern in tests and in the FastAPI startup hook from task 15.2).

    As a side effect, :func:`install_planning_session_state` is invoked on
    ``engine`` so that every subsequent DBAPI connection has the
    ``temp.walking_slice_session_state`` table materialised. The same
    table is also created on the connection used for schema setup so that
    the ``Plan_Revisions`` trigger's WHEN clause compiles cleanly.

    The caller is expected to have already invoked
    :func:`walking_slice.persistence.create_schema` so that the Slice 1
    tables referenced by foreign keys (``Parties``, ``Disclosure_Policies``)
    are present.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database.
    """
    install_planning_session_state(engine)

    # ``engine.begin()`` opens an IMMEDIATE transaction so partial DDL
    # cannot leave the database in an inconsistent state if a later
    # CREATE fails.
    with engine.begin() as conn:
        # Ensure the per-connection TEMP table exists *before* compiling
        # the TEMP trigger that references it. SQLAlchemy's lazy
        # connection acquisition means the ``connect`` listener may have
        # run on this particular connection before ``Plan_Revisions``
        # existed in ``main``, in which case the listener intentionally
        # skipped trigger creation.
        conn.execute(text(_SESSION_STATE_DDL))
        for statement in PLANNING_SCHEMA_STATEMENTS:
            conn.execute(text(statement))
        # Install the per-connection lifecycle trigger now that
        # ``Plan_Revisions`` exists. The same trigger is re-created on
        # every future connection by the ``connect`` listener.
        conn.execute(text(_PLAN_REVISIONS_LIFECYCLE_TRIGGER_DDL))
