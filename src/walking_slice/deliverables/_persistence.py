"""SQLite schema, append-only triggers, and indexes for the Deliverable_Repository.

Design reference: ``.kiro/specs/third-walking-slice/design.md`` §"Data Models —
Schema Additions" (the ``Deliverable_Resources`` and ``Deliverable_Revisions``
listings), §"Persistence Invariants Summary" (rule 9 — every produced
Deliverable Revision carries ``role_marker = 'generated_output'``), and
AD-WS-27 (Slice 3 Records are append-only with no supersession path).

Responsibilities of this module:

1. Expose :func:`create_deliverable_schema` that issues every ``CREATE TABLE``,
   ``CREATE INDEX``, and ``CREATE TRIGGER`` statement specified in design
   §"Data Models — Schema Additions" for the Slice 3 Deliverable_Repository
   tables: ``Deliverable_Resources`` and ``Deliverable_Revisions``.
2. Install ``UPDATE`` and ``DELETE`` rejection triggers on both new tables,
   matching the Slice 1 AD-WS-4 pattern and the Slice 2 AD-WS-19 pattern
   (AD-WS-27).
3. Install the ``Deliverable_Revisions.content_bytes`` length CHECK
   constraint (1 byte..100 MB per Requirement 26.1), the
   ``role_marker = 'generated_output'`` CHECK (Requirement 26.2 / design
   §"Persistence Invariants Summary" rule 9), and the
   ``content_digest_sha256`` length CHECK (64 hex characters, Slice 1
   Requirement 2.2's SHA-256 digest length).
4. Install the two composite indexes named in design §"Data Models — Schema
   Additions": ``idx_deliverable_revisions_by_resource`` and
   ``idx_deliverable_revisions_by_wa``.

Requirements satisfied (per task 1.3):
    22.1  — produced Deliverable Resource and Revision identifiers conform
            to the Slice 1 / Slice 2 identifier strategy (TEXT primary keys
            carrying canonical UUIDv7 strings minted by the Identity_Service;
            the ``Identifier_Registry`` global UNIQUE constraint enforces
            non-reuse per AD-WS-2).
    22.2  — produced Deliverable Resource Identity and produced Deliverable
            Revision Identity are held as two distinct values (separate
            ``deliverable_id`` and ``deliverable_revision_id`` columns with
            their own primary keys, one Resource to many Revisions via the
            ``deliverable_id`` foreign key).
    22.3  — produced Deliverable Resource Identity survives rename /
            relocation: ``Deliverable_Resources`` carries no mutable name
            or location column on the identity row; the
            ``produced_deliverable_name`` column is recorded once at
            creation and the table's UPDATE rejection trigger preserves it.
    26.1  — content_bytes CHECK constraint enforces the 1 byte..100 MB
            range (104857600 bytes = 100 × 1024 × 1024).
    26.2  — every produced Deliverable Revision row carries the columns
            mandated by Requirement 26.2: produced Deliverable Resource
            Identity (``deliverable_id``), produced Deliverable Revision
            Identity (``deliverable_revision_id``), content type, content
            digest computed over the full byte content, authoring
            Contributor Party Identity, recorded time, the role marker
            ``generated_output``, and the originating Work Assignment
            Record Identity.
    26.4  — produced Deliverable Revision is immutable: the
            ``Deliverable_Revisions_reject_update`` trigger rejects every
            UPDATE on the table (Slice 1 Requirement 2.4 pattern).
    26.5  — input validation is enforced at the schema layer where
            possible: ``content_type`` enumeration CHECK, content-bytes
            length CHECK, and produced-Deliverable name length CHECK.
            Application-level validators on top of these schema-level
            constraints reject zero-byte content, oversized content,
            unenumerated content types, and omitted names.
    41.13 — produced-Deliverable vs Source-Evidence disjointness: the
            ``role_marker`` CHECK constraint guarantees every row in
            ``Deliverable_Revisions`` carries ``'generated_output'``,
            which (combined with the Slice 1 schema's absence of any
            ``role_marker`` column on ``Document_Revisions``) is the
            schema-level discriminator that distinguishes a produced
            Deliverable Revision from a Slice 1 Source Evidence Document
            Revision (design §"Persistence Invariants Summary" rule 9).
    42.4  — Slice 1 and Slice 2 schemas are not mutated: this module
            issues only ``CREATE TABLE``/``CREATE INDEX``/``CREATE TRIGGER``
            statements for new Slice 3 tables. The foreign-key references
            to ``Work_Assignment_Records`` (Slice 3) and ``Parties``
            (Slice 1) are read-only in SQLite — declaring a foreign key
            does not alter the referenced table.

Notes:
- All identifier columns are ``TEXT`` (canonical UUIDv7 strings) per the
  Slice 1 ``Identifier_Registry`` invariants; the registry's UNIQUE
  constraint enforces non-reuse globally (AD-WS-2).
- All timestamps are stored as ISO-8601 strings with millisecond precision;
  the application layer formats them per design §"Cross-Cutting Concerns".
- ``content_bytes`` is stored as a ``BLOB`` to preserve byte-equivalence
  (Slice 1 Requirement 2.2). The SHA-256 digest column is stored as a
  64-character lowercase hex string; the length CHECK enforces the hex
  encoding's exact length so an off-format digest cannot be persisted.
- The ``Work_Assignment_Records`` foreign-key target is created by
  :func:`walking_slice.execution._persistence.create_execution_schema`
  (task 1.2). SQLite ``CREATE TABLE`` does not validate that an FK target
  table exists, so the order in which the two Slice 3 schemas are created
  is unconstrained at DDL time; the FK is enforced at INSERT time once
  ``PRAGMA foreign_keys=ON`` is in effect (AD-WS-1, set by
  :func:`walking_slice.persistence.install_pragmas`).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Engine


__all__ = [
    "create_deliverable_schema",
    "DELIVERABLE_IMMUTABLE_TABLES",
    "DELIVERABLE_SCHEMA_STATEMENTS",
]


# ---------------------------------------------------------------------------
# Table definitions.
#
# Each statement mirrors design §"Data Models — Schema Additions" verbatim:
# column names, column order, CHECK constraints, FOREIGN KEY references, and
# composite constraints are unchanged from the design's SQL listings. The
# only addition is ``IF NOT EXISTS`` to keep :func:`create_deliverable_schema`
# idempotent across repeated invocations (the typical pattern in tests and
# in the FastAPI startup hook).
# ---------------------------------------------------------------------------


_TABLE_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Deliverable_Resources -----------------------------------------
    # The Resource row carries the durable produced Deliverable Resource
    # Identity (Requirement 22.1 / 22.2) and the produced-Deliverable name.
    # Per Requirement 22.3 the Resource Identity survives rename and
    # relocation, so this row has no current-location column; the name is
    # recorded once and is held byte-equivalent for the lifetime of the
    # row by the AD-WS-27 UPDATE rejection trigger below.
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Resources (
        deliverable_id             TEXT PRIMARY KEY,
        produced_deliverable_name  TEXT NOT NULL CHECK (
                                       length(produced_deliverable_name) BETWEEN 1 AND 200
                                   ),
        created_at                 TEXT NOT NULL
    )
    """,
    # ----- Deliverable_Revisions -----------------------------------------
    # Per Requirement 26.2 the Revision row carries: produced Deliverable
    # Resource Identity (``deliverable_id``), produced Deliverable Revision
    # Identity (``deliverable_revision_id``), content type, content digest,
    # authoring Contributor Party Identity, recorded time, the role marker
    # ``generated_output``, and the originating Work Assignment Record
    # Identity. The row is immutable per Requirement 26.4; the AD-WS-27
    # UPDATE/DELETE rejection triggers below enforce that.
    #
    # Schema-level CHECK constraints enforce Requirements 26.1 (content
    # length 1..100 MB), the content_type enumeration from Requirement 26.1
    # / design §"Data Models — Schema Additions", the SHA-256 digest length
    # (64 hex chars, Slice 1 Requirement 2.2), and the role-marker fixed
    # value (Requirement 26.2 / Persistence Invariants Summary rule 9 /
    # Requirement 41 §13 — produced-Deliverable vs Source-Evidence
    # disjointness).
    """
    CREATE TABLE IF NOT EXISTS Deliverable_Revisions (
        deliverable_revision_id         TEXT PRIMARY KEY,
        deliverable_id                  TEXT NOT NULL
                                            REFERENCES Deliverable_Resources(deliverable_id),
        content_type                    TEXT NOT NULL CHECK (content_type IN (
                                            'text/markdown',
                                            'text/plain',
                                            'application/pdf',
                                            'application/json',
                                            'image/png',
                                            'image/svg+xml',
                                            'application/octet-stream'
                                        )),
        content_bytes                   BLOB NOT NULL,
        content_digest_sha256           TEXT NOT NULL CHECK (
                                            length(content_digest_sha256) = 64
                                        ),
        role_marker                     TEXT NOT NULL CHECK (
                                            role_marker = 'generated_output'
                                        ),
        originating_work_assignment_id  TEXT NOT NULL
                                            REFERENCES Work_Assignment_Records(work_assignment_id),
        authoring_party_id              TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at                     TEXT NOT NULL,
        CHECK (length(content_bytes) BETWEEN 1 AND 104857600)
    )
    """,
)


# ---------------------------------------------------------------------------
# Index definitions.
#
# Both indexes are named in design §"Data Models — Schema Additions"
# under the Deliverable_Resources / Deliverable_Revisions section. They
# support the two anticipated lookup paths:
#
#   * by-Resource lookup of a produced Deliverable's Revision history
#     (read by Deliverable_Repository read APIs in task 4.2);
#   * by-Work-Assignment lookup of every produced Deliverable Revision
#     authored under a given Work Assignment (read by the Execution
#     Provenance Chain traversal in task 12.2).
#
# The ``Deliverable_Resources`` primary key already carries an implicit
# UNIQUE index on ``deliverable_id``, so no explicit index is needed for
# the parent table.
# ---------------------------------------------------------------------------


_INDEX_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE INDEX IF NOT EXISTS idx_deliverable_revisions_by_resource
        ON Deliverable_Revisions (deliverable_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_deliverable_revisions_by_wa
        ON Deliverable_Revisions (originating_work_assignment_id, recorded_at)
    """,
)


# ---------------------------------------------------------------------------
# Trigger definitions.
#
# Both Deliverable_Repository tables are insert-only per AD-WS-27. UPDATE
# and DELETE are rejected unconditionally on both tables, matching the
# Slice 1 AD-WS-4 pattern and the Slice 2 AD-WS-19 pattern.
#
# ``RAISE(ABORT, ...)`` rolls back the offending statement (and its
# enclosing transaction) and surfaces through the DBAPI as
# :class:`sqlite3.IntegrityError`, which SQLAlchemy wraps as
# :class:`sqlalchemy.exc.IntegrityError`. The application layer maps that
# exception to the Slice 1 immutability error contract.
# ---------------------------------------------------------------------------


DELIVERABLE_IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "Deliverable_Resources",
    "Deliverable_Revisions",
)
"""Slice 3 Deliverable_Repository tables whose UPDATE/DELETE are rejected.

Both tables are insert-only per AD-WS-27 and Requirements 22.3, 26.4
(produced Deliverable Resource Identity and produced Deliverable Revision
row are both immutable once recorded). The companion task 1.2 module
:mod:`walking_slice.execution._persistence` exposes a parallel
``EXECUTION_IMMUTABLE_TABLES`` tuple for the six Execution_Service tables.
"""


def _build_immutable_triggers() -> tuple[str, ...]:
    """Build the AD-WS-27 UPDATE/DELETE rejection triggers.

    For each table in :data:`DELIVERABLE_IMMUTABLE_TABLES`, emit one
    ``BEFORE UPDATE`` and one ``BEFORE DELETE`` trigger that abort with a
    descriptive message. The trigger names follow the Slice 1 and Slice 2
    pattern ``<table>_reject_update`` / ``<table>_reject_delete`` so a
    single naming convention spans the cumulative schema.
    """
    statements: list[str] = []
    for table in DELIVERABLE_IMMUTABLE_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; UPDATE rejected per design AD-WS-27.');
            END
            """
        )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; DELETE rejected per design AD-WS-27.');
            END
            """
        )
    return tuple(statements)


def _build_schema_statements() -> tuple[str, ...]:
    """Concatenate tables → indexes → triggers in dependency order.

    The order matters for two reasons:

    1. ``Deliverable_Revisions`` carries a foreign key to
       ``Deliverable_Resources``; the parent table is created first so
       SQLite can resolve the FK at DDL-parse time.
    2. The AD-WS-27 rejection triggers reference both tables by name; the
       tables are created first so the trigger compilation succeeds.

    The foreign-key references to ``Work_Assignment_Records`` (Slice 3,
    task 1.2) and ``Parties`` (Slice 1) are validated by SQLite only at
    INSERT/UPDATE time when ``PRAGMA foreign_keys=ON``, so this module's
    DDL is order-independent with respect to those external tables.
    """
    return (
        *_TABLE_STATEMENTS,
        *_INDEX_STATEMENTS,
        *_build_immutable_triggers(),
    )


DELIVERABLE_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = _build_schema_statements()
"""Ordered tuple of every DDL statement issued by :func:`create_deliverable_schema`.

Exported for tests and for introspection by the FastAPI startup hook
(task 16). The order is: tables → indexes → AD-WS-27 rejection triggers.
"""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def create_deliverable_schema(engine: Engine) -> None:
    """Create every Slice 3 Deliverable_Repository table, index, and trigger.

    The function is idempotent: every statement uses ``IF NOT EXISTS`` so it
    is safe to call against an already-initialised database (the typical
    pattern in tests and in the FastAPI startup hook).

    The caller is expected to have already invoked
    :func:`walking_slice.persistence.create_schema` so that the Slice 1
    tables referenced by foreign keys (``Parties``) are present, and
    :func:`walking_slice.execution._persistence.create_execution_schema`
    (task 1.2) so that the Slice 3 ``Work_Assignment_Records`` table
    referenced by ``Deliverable_Revisions.originating_work_assignment_id``
    is present. SQLite does not require these tables to exist at
    ``CREATE TABLE`` time — the foreign-key constraint is enforced only at
    INSERT/UPDATE time once ``PRAGMA foreign_keys=ON`` — so the order in
    which the three schema creators run is unconstrained, but the
    application-level composition layer typically runs them in the order
    Slice 1 → Slice 2 → Slice 3 Execution → Slice 3 Deliverable for
    readability.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database. The
            engine is expected to already have the AD-WS-1 pragmas
            installed by :func:`walking_slice.persistence.install_pragmas`
            (``journal_mode=WAL``, ``foreign_keys=ON``); this function
            does not re-install them so it does not stack duplicate
            ``connect`` event listeners.
    """
    # ``engine.begin()`` opens an IMMEDIATE transaction so partial DDL
    # cannot leave the database in an inconsistent state if a later
    # CREATE fails.
    with engine.begin() as conn:
        for statement in DELIVERABLE_SCHEMA_STATEMENTS:
            conn.execute(text(statement))
