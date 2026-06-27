"""SQLite schema, append-only triggers, and indexes for the Outcome_Service.

Design reference: ``.kiro/specs/fourth-walking-slice/design.md`` §"Data
Models — Schema Additions", §"Persistence Invariants Summary", and
AD-WS-36 / AD-WS-37 / AD-WS-38 / AD-WS-39.

Responsibilities of this module (task 1.2):

1. Expose :func:`create_outcome_schema` that issues every
   ``CREATE TABLE``, ``CREATE INDEX``, and ``CREATE TRIGGER`` statement
   specified in design §"Data Models — Schema Additions" for the seven
   Slice 4 Outcome_Service tables:

   - ``Measurement_Definitions``
   - ``Measurement_Definition_Revisions``
   - ``Measurement_Records``
   - ``Observed_Outcomes``
   - ``Observed_Outcome_Revisions``
   - ``Success_Condition_Assessment_Records``
   - ``Outcome_Review_Records``

2. Install ``UPDATE`` and ``DELETE`` rejection triggers on every new
   table, matching the Slice 1 AD-WS-4, Slice 2 AD-WS-19, and Slice 3
   AD-WS-27 patterns and honoring Slice 4 AD-WS-36 / AD-WS-37 (every
   Slice 4 Resource, Revision, and Immutable Record is insert-only with
   no supersession path).

3. Install the table-level ``CHECK`` constraints prescribed by the
   design:

   - ``Measurement_Definitions``:
     ``UNIQUE(target_intended_outcome_resource_id)`` (Requirement 44.3 —
     at most one Measurement Definition per Intended Outcome).
   - ``Measurement_Records``: the ``origin`` enumeration CHECK
     (AD-WS-38), the native-vs-imported source-system-attribute CHECK
     (Requirements 45.3 / 46.2 / 46.4), the
     ``observation_time <= recorded_at`` CHECK, and the imported-row
     ``observation_time <= source_system_retrieval_at <= recorded_at``
     / ``import_at = recorded_at`` ordering CHECK.
   - ``Observed_Outcome_Revisions``: ``outcome_kind = 'observed'`` CHECK
     (§7.4 invariant 6) and the ``predecessor_revision_id`` self-
     reference (AD-WS-36 chain).
   - ``Success_Condition_Assessment_Records``: the ``assessment_category``
     enumeration CHECK and the Unassessable ``length >= 200`` rationale
     CHECK (Requirement 48.3).
   - ``Outcome_Review_Records``:
     ``UNIQUE(target_intended_outcome_revision_id)`` (Requirement 49.3 —
     at most one Outcome Review per Intended Outcome Revision), the
     ``review_outcome`` / ``attribution_stance`` / ``confidence``
     enumeration CHECKs, and the Asserted/Contradicted non-empty
     attribution-evidence CHECK (Requirement 49.4).

4. Install the partial ``UNIQUE`` indexes prescribed by the design:

   - ``idx_measurement_records_import_idempotency`` scoped
     ``WHERE origin = 'imported'`` (AD-WS-39 — at most one imported
     Record per ``(target_measurement_definition_revision_id,
     source_system_id, source_system_record_id)``; Requirement 46.3).
   - ``idx_oo_revisions_one_successor`` scoped
     ``WHERE predecessor_revision_id IS NOT NULL`` (keeps the Observed
     Outcome Revision predecessor chain linear; AD-WS-36, Requirement
     47).

   plus every composite index named in design §"Data Models — Schema
   Additions":

   - ``idx_md_revisions_by_definition``,
     ``idx_md_revisions_by_intended_outcome``
   - ``idx_measurement_records_by_definition_revision``
   - ``idx_oo_revisions_by_resource``,
     ``idx_oo_revisions_by_intended_outcome``
   - ``idx_sca_by_intended_outcome``, ``idx_sca_by_observed_outcome``
   - ``idx_outcome_reviews_by_intended_outcome``

   The two ``UNIQUE`` columns on ``Measurement_Definitions`` and
   ``Outcome_Review_Records`` already create implicit indexes and are
   therefore not duplicated. The Slice 1
   ``idx_relationships_backlinks`` index covers Slice 4 backlink scans
   unchanged (design §"Relationships rows written by Slice 4").

5. Expose :data:`OUTCOME_RESOURCE_KINDS`, the seven additive
   ``Identifier_Registry.resource_kind`` values emitted by Slice 4
   (AD-WS-37, Requirement 43.8). The ``resource_kind`` column is the
   NULLable additive Slice 2 column (AD-WS-19); Slice 4 simply emits
   seven new disjoint values onto it via the identifier-registration
   helper in :mod:`walking_slice.outcome._helpers` (task 3.2). The set
   is exported here so the registration helper, the disclosure-coverage
   seed, and the schema tests share one authoritative list.

Requirements satisfied (per task 1.2):

    43.8  — seven disjoint Slice 4 ``resource_kind`` roles registered on
            ``Identifier_Registry`` (AD-WS-37).
    44.3  — ``Measurement_Definitions.target_intended_outcome_resource_id``
            ``UNIQUE``.
    45.3  — native ``Measurement_Records`` carry NULL for every
            source-system attribute (table-level CHECK).
    46.2  — imported ``Measurement_Records`` carry all source-system
            attributes non-null with the observation/retrieval/recorded
            ordering enforced.
    46.3  — AD-WS-39 partial unique idempotency index for imported
            Measurement Records.
    46.4  — ``source_system_authority`` enumeration CHECK; never
            defaulted to ``authoritative`` (the imported-row branch
            requires it non-null).
    47.7  — Observed Outcome Revisions reject ``UPDATE`` / ``DELETE``;
            ``outcome_kind = 'observed'`` CHECK; linear predecessor
            chain enforced by ``idx_oo_revisions_one_successor``.
    48.3  — Unassessable assessments require ``length >= 200`` rationale.
    48.6  — Success-Condition Assessment Records reject ``UPDATE`` /
            ``DELETE``.
    49.3  — ``Outcome_Review_Records.target_intended_outcome_revision_id``
            ``UNIQUE``.
    49.4  — Asserted / Contradicted Outcome Reviews require a non-empty
            attribution-evidence reference.
    49.7  — Outcome Review Records reject ``UPDATE`` / ``DELETE``.
    57.3  — every Slice 4 table is insert-only with append-only
            triggers (audit-grade immutability).
    60.3  — Slice 4 schema co-exists with Slice 1 + Slice 2 + Slice 3
            schema in one SQLite file; no prior-slice table is touched
            by this module.

Notes:

- All identifier columns are ``TEXT`` (canonical UUIDv7 strings) per the
  Slice 1 ``Identifier_Registry`` invariants; the registry's UNIQUE
  constraint enforces non-reuse globally (AD-WS-2).
- All timestamps are stored as ISO-8601 strings with millisecond
  precision; because they are zero-padded fixed-width strings,
  lexicographic ``<=`` matches chronological ordering, so the temporal
  CHECK constraints below compare strings directly.
- ``applicable_scope`` is persisted byte-equivalent from the request
  body.
- Prior-slice identity references (e.g.
  ``target_intended_outcome_revision_id``) are FK-enforced in
  application code through the prior-slice read APIs (AD-WS-40), not by
  SQL FOREIGN KEY, so the Outcome_Service stays decoupled from
  prior-slice schemas.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Engine


__all__ = [
    "create_outcome_schema",
    "OUTCOME_SCHEMA_STATEMENTS",
    "OUTCOME_IMMUTABLE_TABLES",
    "OUTCOME_RESOURCE_KINDS",
]


# ---------------------------------------------------------------------------
# Slice 4 resource_kind tag values (AD-WS-37, Requirement 43.8).
#
# Seven disjoint identifier roles emitted on the existing additive
# ``Identifier_Registry.resource_kind`` column. Exported so the
# identifier-registration helper (task 3.2), the disclosure-coverage
# seed (task 1.3), and the schema tests (task 1.4) share one
# authoritative list. Held as a ``frozenset`` so a typo at a call site
# fails fast with :class:`ValueError` before any SQL is issued.
# ---------------------------------------------------------------------------


OUTCOME_RESOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "measurement_definition",
        "measurement_definition_revision",
        "measurement_record",
        "observed_outcome",
        "observed_outcome_revision",
        "success_condition_assessment_record",
        "outcome_review_record",
    }
)
"""The seven Slice 4 ``Identifier_Registry.resource_kind`` values.

Drawn verbatim from AD-WS-37 and design §"Persistence Invariants
Summary" rule 4. Every Slice 4 identifier carries ``kind ∈ {'resource',
'revision', 'immutable_record'}`` and a ``resource_kind`` drawn from this
set, keeping the seven Slice 4 identifier roles pairwise disjoint
relative to every Slice 1, Slice 2, and Slice 3 identifier (Requirement
43.8).
"""


# ---------------------------------------------------------------------------
# Table definitions.
#
# Each statement mirrors design §"Data Models — Schema Additions" verbatim:
# column names, column order, CHECK constraints, FOREIGN KEY references
# (Slice 4-internal and Slice 1 ``Parties`` only), UNIQUE columns, and
# composite CHECK constraints are unchanged from the design's SQL
# listings. ``IF NOT EXISTS`` is added so the schema creation is
# idempotent.
# ---------------------------------------------------------------------------


_TABLE_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Measurement_Definitions ---------------------------------------
    # Requirement 44.3: at most one Measurement Definition per Intended
    # Outcome, enforced by UNIQUE(target_intended_outcome_resource_id).
    """
    CREATE TABLE IF NOT EXISTS Measurement_Definitions (
        measurement_definition_id            TEXT PRIMARY KEY,
        target_intended_outcome_resource_id  TEXT NOT NULL UNIQUE,
        created_at                           TEXT NOT NULL
    )
    """,
    # ----- Measurement_Definition_Revisions ------------------------------
    # Requirement 44.2: immutable Revision carrying the measurand
    # description, unit, observation window, cadence, data source, and
    # applicable scope length constraints.
    """
    CREATE TABLE IF NOT EXISTS Measurement_Definition_Revisions (
        measurement_definition_revision_id   TEXT PRIMARY KEY,
        measurement_definition_id            TEXT NOT NULL REFERENCES Measurement_Definitions(measurement_definition_id),
        target_intended_outcome_resource_id  TEXT NOT NULL,
        target_intended_outcome_revision_id  TEXT NOT NULL,
        measurand_description                TEXT NOT NULL CHECK (length(measurand_description) BETWEEN 1 AND 4000),
        unit_of_measure                      TEXT NOT NULL CHECK (length(unit_of_measure) BETWEEN 1 AND 200),
        observation_window                   TEXT NOT NULL CHECK (length(observation_window) BETWEEN 1 AND 1000),
        cadence                              TEXT NOT NULL CHECK (length(cadence) BETWEEN 1 AND 1000),
        data_source                          TEXT NOT NULL CHECK (length(data_source) BETWEEN 1 AND 1000),
        authoring_party_id                   TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope                     TEXT NOT NULL,
        recorded_at                          TEXT NOT NULL
    )
    """,
    # ----- Measurement_Records -------------------------------------------
    # Requirements 45, 46, AD-WS-38, AD-WS-39.
    #
    # ``origin`` enumerates native vs imported. The first GLOB CHECK
    # guards gross malformation of ``observed_value`` (the six-fractional-
    # digit rule and the unit match are enforced in application code
    # before INSERT). The ``observation_time <= recorded_at`` CHECK
    # applies to every row. The final table-level CHECK keyed on
    # ``origin`` enforces the native-vs-imported source-system-attribute
    # rule (Requirements 45.3 / 46.2 / 46.4) and the imported-row
    # observation <= retrieval <= recorded ordering plus
    # ``import_at = recorded_at``. The source-system authority designation
    # is never defaulted to 'authoritative' — the imported branch
    # requires ``source_system_authority IS NOT NULL`` (Requirement 46.4 /
    # 46.7).
    """
    CREATE TABLE IF NOT EXISTS Measurement_Records (
        measurement_record_id                       TEXT PRIMARY KEY,
        target_measurement_definition_id            TEXT NOT NULL REFERENCES Measurement_Definitions(measurement_definition_id),
        target_measurement_definition_revision_id   TEXT NOT NULL REFERENCES Measurement_Definition_Revisions(measurement_definition_revision_id),
        origin                                      TEXT NOT NULL CHECK (origin IN ('native','imported')),
        observed_value                              TEXT NOT NULL,
        observed_value_unit                         TEXT NOT NULL,
        observation_time                            TEXT NOT NULL,
        source_system_id                            TEXT NULL CHECK (source_system_id IS NULL OR length(source_system_id) BETWEEN 1 AND 200),
        source_system_record_id                     TEXT NULL CHECK (source_system_record_id IS NULL OR length(source_system_record_id) BETWEEN 1 AND 200),
        source_system_authority                     TEXT NULL CHECK (source_system_authority IS NULL OR source_system_authority IN ('authoritative','replica','projection','index','federation')),
        source_system_retrieval_at                  TEXT NULL,
        import_at                                   TEXT NULL,
        recording_party_id                          TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope                            TEXT NOT NULL,
        recorded_at                                 TEXT NOT NULL,
        CHECK (observed_value GLOB '[0-9]*' OR observed_value GLOB '-[0-9]*'),
        CHECK (observation_time <= recorded_at),
        CHECK (
            (origin = 'native'
               AND source_system_id IS NULL AND source_system_record_id IS NULL
               AND source_system_authority IS NULL AND source_system_retrieval_at IS NULL AND import_at IS NULL)
            OR
            (origin = 'imported'
               AND source_system_id IS NOT NULL AND source_system_record_id IS NOT NULL
               AND source_system_authority IS NOT NULL AND source_system_retrieval_at IS NOT NULL AND import_at IS NOT NULL
               AND observation_time <= source_system_retrieval_at
               AND source_system_retrieval_at <= recorded_at
               AND import_at = recorded_at)
        )
    )
    """,
    # ----- Observed_Outcomes ---------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Observed_Outcomes (
        observed_outcome_id                  TEXT PRIMARY KEY,
        target_intended_outcome_resource_id  TEXT NOT NULL,
        created_at                           TEXT NOT NULL
    )
    """,
    # ----- Observed_Outcome_Revisions ------------------------------------
    # §7.4 invariant 6: outcome_kind is always 'observed'. AD-WS-36: the
    # predecessor chain is an explicit self-reference, kept linear by the
    # idx_oo_revisions_one_successor partial unique index below.
    """
    CREATE TABLE IF NOT EXISTS Observed_Outcome_Revisions (
        observed_outcome_revision_id         TEXT PRIMARY KEY,
        observed_outcome_id                  TEXT NOT NULL REFERENCES Observed_Outcomes(observed_outcome_id),
        outcome_kind                         TEXT NOT NULL CHECK (outcome_kind = 'observed'),
        target_intended_outcome_resource_id  TEXT NOT NULL,
        target_intended_outcome_revision_id  TEXT NOT NULL,
        assessment_summary                   TEXT NOT NULL CHECK (length(assessment_summary) BETWEEN 1 AND 4000),
        predecessor_revision_id              TEXT NULL REFERENCES Observed_Outcome_Revisions(observed_outcome_revision_id),
        authoring_party_id                   TEXT NOT NULL REFERENCES Parties(party_id),
        applicable_scope                     TEXT NOT NULL,
        recorded_at                          TEXT NOT NULL
    )
    """,
    # ----- Success_Condition_Assessment_Records --------------------------
    # Requirement 48.3: Unassessable requires a >= 200-char rationale,
    # enforced by the trailing table-level CHECK.
    """
    CREATE TABLE IF NOT EXISTS Success_Condition_Assessment_Records (
        assessment_id                        TEXT PRIMARY KEY,
        target_intended_outcome_resource_id  TEXT NOT NULL,
        target_intended_outcome_revision_id  TEXT NOT NULL,
        sourced_observed_outcome_id          TEXT NOT NULL REFERENCES Observed_Outcomes(observed_outcome_id),
        sourced_observed_outcome_revision_id TEXT NOT NULL REFERENCES Observed_Outcome_Revisions(observed_outcome_revision_id),
        assessment_category                  TEXT NOT NULL CHECK (assessment_category IN ('Satisfied','Partially_Satisfied','Not_Satisfied','Unassessable')),
        assessment_rationale                 TEXT NOT NULL CHECK (length(assessment_rationale) BETWEEN 1 AND 4000),
        assessing_party_id                   TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type                 TEXT NOT NULL CHECK (authority_basis_type IN ('role-grant-id','scope-id','delegation-chain-id')),
        authority_basis_id                   TEXT NOT NULL,
        applicable_scope                     TEXT NOT NULL,
        recorded_at                          TEXT NOT NULL,
        CHECK (assessment_category != 'Unassessable' OR length(assessment_rationale) >= 200)
    )
    """,
    # ----- Outcome_Review_Records ----------------------------------------
    # Requirement 49.3: at most one Outcome Review per Intended Outcome
    # Revision, enforced by UNIQUE(target_intended_outcome_revision_id).
    # Requirement 49.4: Asserted / Contradicted require a non-empty
    # attribution-evidence reference, enforced by the trailing CHECK.
    """
    CREATE TABLE IF NOT EXISTS Outcome_Review_Records (
        outcome_review_id                    TEXT PRIMARY KEY,
        target_intended_outcome_resource_id  TEXT NOT NULL,
        target_intended_outcome_revision_id  TEXT NOT NULL UNIQUE,
        review_outcome                       TEXT NOT NULL CHECK (review_outcome IN ('Achieved','Partially_Achieved','Not_Achieved','Inconclusive')),
        attribution_stance                   TEXT NOT NULL CHECK (attribution_stance IN ('Asserted','Partial','Unattributed','Contradicted')),
        confidence                           TEXT NOT NULL CHECK (confidence IN ('High','Moderate','Low')),
        review_rationale                     TEXT NOT NULL CHECK (length(review_rationale) BETWEEN 1 AND 4000),
        attribution_evidence_reference       TEXT NOT NULL CHECK (length(attribution_evidence_reference) BETWEEN 0 AND 4000),
        reviewing_party_id                   TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type                 TEXT NOT NULL CHECK (authority_basis_type IN ('role-grant-id','scope-id','delegation-chain-id')),
        authority_basis_id                   TEXT NOT NULL,
        applicable_scope                     TEXT NOT NULL,
        recorded_at                          TEXT NOT NULL,
        CHECK (attribution_stance NOT IN ('Asserted','Contradicted') OR length(attribution_evidence_reference) >= 1)
    )
    """,
)


# ---------------------------------------------------------------------------
# Index definitions.
#
# Every index named in design §"Data Models — Schema Additions" is
# included, in the same order it appears there. The two ``UNIQUE``
# columns (``Measurement_Definitions.target_intended_outcome_resource_id``
# and ``Outcome_Review_Records.target_intended_outcome_revision_id``)
# already carry implicit indexes and are intentionally not duplicated
# here. The two partial UNIQUE indexes encode invariants:
#
#   - ``idx_measurement_records_import_idempotency`` (AD-WS-39): at most
#     one imported Record per (Definition Revision, source system,
#     source record) triple.
#   - ``idx_oo_revisions_one_successor`` (AD-WS-36): at most one
#     successor per predecessor, keeping the Observed Outcome Revision
#     chain linear (no forking).
# ---------------------------------------------------------------------------


_INDEX_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Measurement_Definition_Revisions ------------------------------
    """
    CREATE INDEX IF NOT EXISTS idx_md_revisions_by_definition
        ON Measurement_Definition_Revisions (measurement_definition_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_md_revisions_by_intended_outcome
        ON Measurement_Definition_Revisions (target_intended_outcome_revision_id, recorded_at)
    """,
    # ----- Measurement_Records -------------------------------------------
    # AD-WS-39 import idempotency key (imported rows only).
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_measurement_records_import_idempotency
        ON Measurement_Records (target_measurement_definition_revision_id, source_system_id, source_system_record_id)
        WHERE origin = 'imported'
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_measurement_records_by_definition_revision
        ON Measurement_Records (target_measurement_definition_revision_id, recorded_at)
    """,
    # ----- Observed_Outcome_Revisions ------------------------------------
    # At most one successor per predecessor keeps the chain linear.
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_oo_revisions_one_successor
        ON Observed_Outcome_Revisions (predecessor_revision_id)
        WHERE predecessor_revision_id IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_oo_revisions_by_resource
        ON Observed_Outcome_Revisions (observed_outcome_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_oo_revisions_by_intended_outcome
        ON Observed_Outcome_Revisions (target_intended_outcome_revision_id, recorded_at)
    """,
    # ----- Success_Condition_Assessment_Records --------------------------
    """
    CREATE INDEX IF NOT EXISTS idx_sca_by_intended_outcome
        ON Success_Condition_Assessment_Records (target_intended_outcome_revision_id, recorded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_sca_by_observed_outcome
        ON Success_Condition_Assessment_Records (sourced_observed_outcome_revision_id, recorded_at)
    """,
    # ----- Outcome_Review_Records ----------------------------------------
    """
    CREATE INDEX IF NOT EXISTS idx_outcome_reviews_by_intended_outcome
        ON Outcome_Review_Records (target_intended_outcome_revision_id)
    """,
)


# ---------------------------------------------------------------------------
# Trigger definitions.
#
# Every Slice 4 Outcome_Service table is insert-only per AD-WS-36 /
# AD-WS-37. The triggers below match the Slice 1 AD-WS-4, Slice 2
# AD-WS-19, and Slice 3 AD-WS-27 patterns: ``BEFORE UPDATE`` and
# ``BEFORE DELETE`` triggers abort with a descriptive message.
# ``RAISE(ABORT, ...)`` rolls back the offending statement (and its
# enclosing transaction) and surfaces through the DBAPI as
# :class:`sqlite3.IntegrityError`, which SQLAlchemy wraps as
# :class:`sqlalchemy.exc.IntegrityError`.
# ---------------------------------------------------------------------------


OUTCOME_IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "Measurement_Definitions",
    "Measurement_Definition_Revisions",
    "Measurement_Records",
    "Observed_Outcomes",
    "Observed_Outcome_Revisions",
    "Success_Condition_Assessment_Records",
    "Outcome_Review_Records",
)
"""Slice 4 Outcome_Service tables whose UPDATE/DELETE are rejected.

Every Slice 4 Resource, Revision, and Immutable Record is insert-only
with no supersession path (AD-WS-36, AD-WS-37). New evidence creates a
new Observed Outcome Revision via the explicit predecessor chain rather
than overwriting any prior row (Requirement 47.3).
"""


def _build_immutable_triggers() -> tuple[str, ...]:
    """Build the AD-WS-36 / AD-WS-37-style UPDATE/DELETE rejection triggers.

    For each table in :data:`OUTCOME_IMMUTABLE_TABLES`, emit one
    ``BEFORE UPDATE`` and one ``BEFORE DELETE`` trigger that abort with a
    descriptive message. ``RAISE(ABORT, ...)`` rolls back the offending
    statement (and its enclosing transaction) and surfaces through the
    DBAPI as :class:`sqlite3.IntegrityError`, which SQLAlchemy wraps as
    :class:`sqlalchemy.exc.IntegrityError`.
    """
    statements: list[str] = []
    for table in OUTCOME_IMMUTABLE_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; UPDATE rejected per design AD-WS-36 / AD-WS-37.');
            END
            """
        )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; DELETE rejected per design AD-WS-36 / AD-WS-37.');
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


OUTCOME_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = _build_schema_statements()
"""Ordered tuple of every DDL statement issued by :func:`create_outcome_schema`.

Exported for tests and for introspection by the FastAPI startup hook.
The order is tables → indexes → append-only triggers, so foreign-key
targets within the Outcome_Service set exist before the referring tables
are created. Foreign keys that target Slice 1 tables (``Parties``) are
resolved lazily at INSERT time, so those schemas may be created before
or after this one.
"""


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def create_outcome_schema(engine: Engine) -> None:
    """Create every Slice 4 Outcome_Service table, index, and trigger.

    The function is idempotent: every statement uses ``IF NOT EXISTS`` so
    it is safe to call against an already-initialised database (the
    typical pattern in tests and in the FastAPI startup hook).

    The caller is expected to have already invoked
    :func:`walking_slice.persistence.create_schema` so that the Slice 1
    tables referenced by foreign keys (``Parties``) are present. The
    Slice 2 and Slice 3 schemas may be created either before or after
    this function — SQLite resolves FK targets lazily at INSERT time when
    ``PRAGMA foreign_keys=ON`` is set, and no Slice 4 table references a
    Slice 2 or Slice 3 table by SQL FOREIGN KEY (prior-slice identity
    references are validated in application code per AD-WS-40).

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database.
    """
    # ``engine.begin()`` opens a transaction so partial DDL cannot leave
    # the database in an inconsistent state if a later CREATE fails.
    with engine.begin() as conn:
        for statement in OUTCOME_SCHEMA_STATEMENTS:
            conn.execute(text(statement))
