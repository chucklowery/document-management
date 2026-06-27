"""SQLite schema, append-only triggers, indexes, and pragmas for the slice.

Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"Table-by-Table
Specification", §"Persistence Invariants Summary", and AD-WS-4/5/7/8.

Responsibilities of this module:

1. Expose :func:`create_schema` that issues every ``CREATE TABLE`` and
   ``CREATE TRIGGER`` statement specified in the design — including the
   ``Identifier_Registry``, ``Parties``, ``Role_Assignments``,
   ``Source_Documents``, ``Document_Revisions``, ``Content_Regions``,
   ``Region_Occurrences``, ``Findings``/``Finding_Revisions``,
   ``Recommendations``/``Recommendation_Revisions``, ``Decisions``,
   ``Relationships``, ``Trails``/``Trail_Revisions``/``Trail_Steps``,
   ``Provenance_Manifests``/``Omission_Entries``, ``Audit_Records``,
   ``Interim_ADR_Records``, and ``Disclosure_Policies`` tables.
2. Install append-only triggers that reject ``UPDATE`` and ``DELETE`` on every
   immutable table named in AD-WS-4: ``Document_Revisions``,
   ``Region_Occurrences``, ``Finding_Revisions``, ``Recommendation_Revisions``,
   ``Decisions``, ``Relationships``, ``Trail_Revisions``, ``Trail_Steps``,
   ``Provenance_Manifests``, ``Audit_Records``.
3. Install one-shot triggers for ``Role_Assignments.revoked_at`` and
   ``Omission_Entries.resolved_at`` so the field may transition from ``NULL``
   to a recorded timestamp exactly once.
4. Install the composite indexes named in AD-WS-8 and design §"Persistence
   Invariants Summary": the backlink scan index on ``Relationships``, the
   outbound traversal index, the ``Document_Revisions(resource_id,
   recorded_at)`` index, and the ``Audit_Records(recorded_at,
   append_sequence)`` index.
5. Provide :func:`install_pragmas` that registers a SQLAlchemy ``connect``
   event listener which sets ``PRAGMA journal_mode=WAL`` and
   ``PRAGMA foreign_keys=ON`` on every new DBAPI connection.

Requirements satisfied (per task 1.3):
    2.4  — Document_Revisions immutable (UPDATE/DELETE rejection trigger).
    2.7  — append-only Audit_Records and rollback on audit failure rely on the
           insert-only audit table.
    6.6  — Decisions Immutable Record (UPDATE/DELETE rejection trigger).
    13.3 — append-only Audit_Records (UPDATE/DELETE rejection trigger).
    13.5 — Audit insertion order preserved by the unique ``append_sequence``
           and ``(recorded_at, append_sequence)`` index.
    16.2 — SQLite ``WAL`` journal mode and foreign-key enforcement enabled on
           every new connection.

Notes:
- All timestamps are stored as ISO-8601 text columns; the application layer
  is responsible for formatting them as UTC with millisecond precision per
  design §"Cross-Cutting Concerns" (*Time*).
- All identifier columns are ``TEXT`` (canonical UUIDv7 strings); the
  ``Identifier_Registry`` global UNIQUE constraint enforces non-reuse per
  AD-WS-2.
"""

from __future__ import annotations

from typing import Final
from weakref import WeakSet

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import Connection


__all__ = [
    "create_schema",
    "install_pragmas",
    "SCHEMA_STATEMENTS",
    "SLICE2_ADDITIVE_COLUMNS",
]


# ---------------------------------------------------------------------------
# Pragma installation.
#
# Per task 1.3 and AD-WS-1, every DBAPI connection opened against the slice's
# SQLite store must apply ``journal_mode=WAL`` (transactional concurrency with
# crash safety) and ``foreign_keys=ON`` (FK enforcement is per-connection in
# SQLite).
# ---------------------------------------------------------------------------


_engines_with_pragmas: "WeakSet[Engine]" = WeakSet()


def install_pragmas(engine: Engine) -> None:
    """Register a ``connect`` event listener that sets WAL + FK pragmas.

    The listener is idempotent — calling :func:`install_pragmas` more than
    once on the same :class:`~sqlalchemy.engine.Engine` is a no-op so that
    tests which already wire pragmas in their own fixtures (e.g.
    ``tests/conftest.py``) do not stack duplicate handlers.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database.
    """
    if engine in _engines_with_pragmas:
        return

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover - hit on every test connect
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    _engines_with_pragmas.add(engine)


# ---------------------------------------------------------------------------
# Table definitions.
#
# The statements below mirror design §"Table-by-Table Specification" verbatim.
# Identifiers are stored as TEXT (canonical UUIDv7); timestamps are stored as
# TEXT (ISO-8601 strings) so SQLite ordering matches lexicographic ordering of
# the formatted timestamp, which is sufficient for the audit append-sequence
# invariants in design §"Persistence Invariants Summary" #8.
# ---------------------------------------------------------------------------


_TABLE_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Identifier_Registry --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Identifier_Registry (
        identifier      TEXT PRIMARY KEY,
        kind            TEXT NOT NULL CHECK (kind IN (
                            'resource', 'revision', 'relationship',
                            'region', 'region_occurrence',
                            'immutable_record', 'trail', 'trail_revision',
                            'trail_step', 'manifest'
                        )),
        content_digest  TEXT,
        issued_at       TEXT NOT NULL
    )
    """,
    # ----- Parties ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Parties (
        party_id      TEXT PRIMARY KEY,
        kind          TEXT NOT NULL CHECK (kind IN (
                          'person', 'organization', 'team', 'automated_agent'
                      )),
        display_name  TEXT,
        created_at    TEXT NOT NULL
    )
    """,
    # ----- Role_Assignments -----------------------------------------------
    # Immutable except `revoked_at` which is one-shot (see triggers below).
    """
    CREATE TABLE IF NOT EXISTS Role_Assignments (
        role_assignment_id      TEXT PRIMARY KEY,
        party_id                TEXT NOT NULL REFERENCES Parties(party_id),
        role_name               TEXT NOT NULL,
        scope                   TEXT NOT NULL,
        authorities_granted     TEXT NOT NULL,
        effective_start         TEXT NOT NULL,
        effective_end           TEXT,
        revoked_at              TEXT,
        assigning_authority_id  TEXT NOT NULL,
        recorded_at             TEXT NOT NULL
    )
    """,
    # ----- Source_Documents ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Source_Documents (
        resource_id          TEXT PRIMARY KEY,
        current_location     TEXT,
        external_identifier  TEXT,
        source_system_id     TEXT,
        authority            TEXT NOT NULL CHECK (authority IN (
                                 'authoritative',
                                 'imported-replica',
                                 'imported-projection',
                                 'imported-index',
                                 'imported-federation-point',
                                 'reference-to-system-of-record'
                             )),
        created_at           TEXT NOT NULL
    )
    """,
    # ----- Document_Revisions (immutable) ---------------------------------
    """
    CREATE TABLE IF NOT EXISTS Document_Revisions (
        revision_id              TEXT PRIMARY KEY,
        resource_id              TEXT NOT NULL REFERENCES Source_Documents(resource_id),
        parent_revision_id       TEXT REFERENCES Document_Revisions(revision_id),
        content_bytes            BLOB NOT NULL,
        content_digest_sha256    TEXT NOT NULL,
        contributing_party_id    TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at              TEXT NOT NULL,
        change_description       TEXT
    )
    """,
    # ----- Content_Regions -------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Content_Regions (
        region_id           TEXT PRIMARY KEY,
        parent_resource_id  TEXT NOT NULL REFERENCES Source_Documents(resource_id),
        created_at          TEXT NOT NULL
    )
    """,
    # ----- Region_Occurrences (immutable) ---------------------------------
    """
    CREATE TABLE IF NOT EXISTS Region_Occurrences (
        region_id                    TEXT NOT NULL REFERENCES Content_Regions(region_id),
        document_revision_id         TEXT NOT NULL REFERENCES Document_Revisions(revision_id),
        start_offset_bytes           INTEGER NOT NULL CHECK (start_offset_bytes >= 0),
        end_offset_bytes             INTEGER NOT NULL,
        span_byte_length             INTEGER NOT NULL,
        span_content_digest_sha256   TEXT NOT NULL,
        recorded_at                  TEXT NOT NULL,
        PRIMARY KEY (region_id, document_revision_id),
        CHECK (end_offset_bytes > start_offset_bytes),
        CHECK (span_byte_length = end_offset_bytes - start_offset_bytes)
    )
    """,
    # ----- Findings and Finding_Revisions ---------------------------------
    """
    CREATE TABLE IF NOT EXISTS Findings (
        finding_id   TEXT PRIMARY KEY,
        created_at   TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Finding_Revisions (
        finding_revision_id  TEXT PRIMARY KEY,
        finding_id           TEXT NOT NULL REFERENCES Findings(finding_id),
        parent_revision_id   TEXT REFERENCES Finding_Revisions(finding_revision_id),
        statement            TEXT NOT NULL,
        is_hypothesis        INTEGER NOT NULL CHECK (is_hypothesis IN (0, 1)),
        authoring_party_id   TEXT NOT NULL REFERENCES Parties(party_id),
        assumptions_json     TEXT NOT NULL,
        confidence_note      TEXT,
        recorded_at          TEXT NOT NULL
    )
    """,
    # ----- Recommendations and Recommendation_Revisions -------------------
    """
    CREATE TABLE IF NOT EXISTS Recommendations (
        recommendation_id  TEXT PRIMARY KEY,
        created_at         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Recommendation_Revisions (
        recommendation_revision_id  TEXT PRIMARY KEY,
        recommendation_id           TEXT NOT NULL REFERENCES Recommendations(recommendation_id),
        parent_revision_id          TEXT REFERENCES Recommendation_Revisions(recommendation_revision_id),
        rationale                   TEXT,
        assumptions_json            TEXT NOT NULL,
        confidence                  TEXT CHECK (confidence IN ('Low', 'Medium', 'High') OR confidence IS NULL),
        authoring_party_id          TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at                 TEXT NOT NULL
    )
    """,
    # ----- Decisions (Immutable Record) -----------------------------------
    """
    CREATE TABLE IF NOT EXISTS Decisions (
        decision_id                          TEXT PRIMARY KEY,
        target_recommendation_id             TEXT NOT NULL,
        target_recommendation_revision_id    TEXT NOT NULL,
        outcome                              TEXT NOT NULL CHECK (outcome IN ('Accept', 'Reject', 'Defer')),
        rationale                            TEXT NOT NULL,
        deciding_party_id                    TEXT NOT NULL REFERENCES Parties(party_id),
        authority_basis_type                 TEXT NOT NULL CHECK (authority_basis_type IN (
                                                  'role-grant-id', 'scope-id', 'delegation-chain-id'
                                              )),
        authority_basis_id                   TEXT NOT NULL,
        applicable_scope                     TEXT NOT NULL,
        recorded_at                          TEXT NOT NULL,
        UNIQUE (target_recommendation_id, target_recommendation_revision_id)
    )
    """,
    # ----- Relationships ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Relationships (
        relationship_id      TEXT PRIMARY KEY,
        -- The ``Relates To`` value supports the Slice 2 Plan Review
        -- Relationship per AD-WS-17 (the additive ``semantic_role``
        -- column on this same table carries the ``'review'``
        -- discriminator). Adding ``Relates To`` to the CHECK
        -- enumeration is backward-compatible: every Slice 1 row
        -- carried a value drawn from the original five-member set,
        -- so widening the CHECK leaves every pre-existing row valid
        -- (Requirement 19.4 — additive only, no mutation of existing
        -- rows).
        --
        -- The ``Produces`` value supports the Slice 3 Deliverable
        -- Production Record → produced Deliverable Revision
        -- Relationship per AD-WS-26 (the ``Produces`` pattern is the
        -- precise type for "Execution Record created this output"
        -- per ``02-domain-model.md`` §10.10). Widening the CHECK to
        -- include ``Produces`` is backward-compatible for the same
        -- reason ``Relates To`` was: every pre-existing Slice 1 /
        -- Slice 2 row carried a value drawn from the prior six-member
        -- set, so the broadened enumeration leaves every pre-existing
        -- row valid (Slice 3 Requirement 40.1 — additive only, no
        -- mutation of existing rows).
        --
        -- The ``Cites`` value supports the Slice 4 outcome-measurement
        -- Relationships per AD-WS-35 (the §10.7 evidence-citation type
        -- for "this Record draws on that Record/Revision as its
        -- evidentiary basis"): a Measurement Record cites its target
        -- Measurement Definition Revision, an Observed Outcome Revision
        -- cites each supporting Measurement Record, a Success-Condition
        -- Assessment cites its sourced Observed Outcome Revision, and an
        -- Outcome Review cites its Assessments, Completion Records, and
        -- produced Deliverable Revisions (the additive
        -- ``semantic_role`` column disambiguates them). Widening the
        -- CHECK to include ``Cites`` is backward-compatible for the same
        -- reason ``Relates To`` and ``Produces`` were: every
        -- pre-existing Slice 1 / Slice 2 / Slice 3 row carried a value
        -- drawn from the prior seven-member set, so the broadened
        -- enumeration leaves every pre-existing row valid (Slice 4
        -- Requirement 60 — additive only, no mutation of existing rows).
        relationship_type    TEXT NOT NULL CHECK (relationship_type IN (
                                 'Supports', 'Contradicts', 'Derived From',
                                 'Addresses', 'Supersedes', 'Relates To',
                                 'Produces', 'Cites'
                             )),
        source_kind          TEXT NOT NULL,
        source_id            TEXT NOT NULL,
        source_revision_id   TEXT,
        target_kind          TEXT NOT NULL,
        target_id            TEXT NOT NULL,
        target_revision_id   TEXT,
        authoring_party_id   TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at          TEXT NOT NULL
    )
    """,
    # ----- Trails / Trail_Revisions / Trail_Steps -------------------------
    # `current_revision_id` is a mutable convenience pointer (Principle 5.23),
    # explicitly not authoritative; the immutable Trail_Revisions row is.
    """
    CREATE TABLE IF NOT EXISTS Trails (
        trail_id              TEXT PRIMARY KEY,
        created_at            TEXT NOT NULL,
        current_revision_id   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Trail_Revisions (
        trail_revision_id        TEXT PRIMARY KEY,
        trail_id                 TEXT NOT NULL REFERENCES Trails(trail_id),
        predecessor_revision_id  TEXT REFERENCES Trail_Revisions(trail_revision_id),
        purpose                  TEXT NOT NULL,
        audience_id              TEXT NOT NULL,
        ordering_rationale       TEXT,
        authoring_party_id       TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at              TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Trail_Steps (
        trail_step_id         TEXT PRIMARY KEY,
        trail_revision_id     TEXT NOT NULL REFERENCES Trail_Revisions(trail_revision_id),
        ordinal               INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 5),
        selection_mode        TEXT NOT NULL CHECK (selection_mode = 'Pinned'),
        target_kind           TEXT NOT NULL,
        target_id             TEXT NOT NULL,
        target_revision_id    TEXT,
        region_id             TEXT,
        annotation            TEXT,
        UNIQUE (trail_revision_id, ordinal),
        CHECK (
            (ordinal = 1 AND target_kind = 'document_revision')
         OR (ordinal = 2 AND target_kind = 'region_occurrence' AND region_id IS NOT NULL)
         OR (ordinal = 3 AND target_kind = 'finding_revision')
         OR (ordinal = 4 AND target_kind = 'recommendation_revision')
         OR (ordinal = 5 AND target_kind = 'decision')
        )
    )
    """,
    # ----- Provenance_Manifests and Omission_Entries ----------------------
    #
    # Note on subject_kind enumeration: the four Slice 1 syntheses
    # ('finding_revision', 'recommendation_revision', 'decision',
    # 'trail_revision') are the original Requirement 10.1 set. The
    # additive value 'plan_approval' is permitted here so the
    # second-walking-slice :class:`walking_slice.planning.plan_approvals.PlanApprovalService`
    # can write a Plan Approval Provenance Manifest via the existing
    # :class:`walking_slice.manifests.ProvenanceManifestWriter` per
    # design §"Planning_Service.PlanApprovals". The addition is purely
    # additive — every Slice 1 row remains valid, every Slice 1
    # validator still accepts the same four kinds — and is permitted
    # by Requirement 19.2 (additive extensions of Slice 1
    # enumerations).
    """
    CREATE TABLE IF NOT EXISTS Provenance_Manifests (
        manifest_id             TEXT PRIMARY KEY,
        subject_kind            TEXT NOT NULL CHECK (subject_kind IN (
                                    'finding_revision', 'recommendation_revision',
                                    'decision', 'trail_revision',
                                    'plan_approval'
                                )),
        subject_id              TEXT NOT NULL,
        subject_revision_id     TEXT,
        authoring_party_id      TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at             TEXT NOT NULL,
        included_sources_json   TEXT NOT NULL,
        is_complete             INTEGER NOT NULL DEFAULT 1 CHECK (is_complete IN (0, 1))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS Omission_Entries (
        omission_entry_id              TEXT PRIMARY KEY,
        manifest_id                    TEXT NOT NULL REFERENCES Provenance_Manifests(manifest_id),
        excluded_source_id             TEXT NOT NULL,
        excluded_source_revision_id    TEXT,
        category                       TEXT NOT NULL CHECK (category IN (
                                           'intentional', 'unavailable',
                                           'restricted', 'stale', 'unresolved'
                                       )),
        rationale                      TEXT NOT NULL,
        authoring_party_id             TEXT NOT NULL REFERENCES Parties(party_id),
        recorded_at                    TEXT NOT NULL,
        resolved_at                    TEXT
    )
    """,
    # ----- Audit_Records (append-only) ------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Audit_Records (
        audit_record_id              TEXT PRIMARY KEY,
        append_sequence              INTEGER NOT NULL UNIQUE,
        actor_party_id               TEXT NOT NULL REFERENCES Parties(party_id),
        action_type                  TEXT NOT NULL,
        outcome                      TEXT NOT NULL CHECK (outcome IN (
                                         'permit', 'deny', 'consequential'
                                     )),
        target_id                    TEXT,
        target_revision_id           TEXT,
        evaluated_role_assignment_id TEXT,
        authorities_required         TEXT,
        authorities_held             TEXT,
        reason_code                  TEXT,
        correlation_id               TEXT NOT NULL,
        recorded_at                  TEXT NOT NULL
    )
    """,
    # ----- Interim_ADR_Records --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Interim_ADR_Records (
        record_id                TEXT PRIMARY KEY,
        motivating_requirement   TEXT NOT NULL,
        motivating_criterion     TEXT NOT NULL,
        observable_behavior      TEXT NOT NULL,
        recorded_at              TEXT NOT NULL,
        backlog_adr_id           TEXT NOT NULL,
        resolved_by_adr_id       TEXT,
        resolved_at              TEXT
    )
    """,
    # ----- Disclosure_Policies --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS Disclosure_Policies (
        policy_id        TEXT PRIMARY KEY,
        policy_name      TEXT NOT NULL UNIQUE,
        ruleset_json     TEXT NOT NULL,
        effective_start  TEXT NOT NULL,
        superseded_by    TEXT
    )
    """,
)


# ---------------------------------------------------------------------------
# Index definitions.
#
# Names below appear in AD-WS-8 and design §"Persistence Invariants Summary".
# ---------------------------------------------------------------------------


_INDEX_STATEMENTS: Final[tuple[str, ...]] = (
    # Document_Revisions(resource_id, recorded_at) — listed under Document_Revisions schema.
    """
    CREATE INDEX IF NOT EXISTS ix_document_revisions_resource_recorded
        ON Document_Revisions (resource_id, recorded_at)
    """,
    # Backlink scan index — AD-WS-8.
    """
    CREATE INDEX IF NOT EXISTS ix_relationships_target_backlink
        ON Relationships (target_id, target_revision_id, relationship_type, recorded_at)
    """,
    # Outbound traversal index — Relationships schema.
    """
    CREATE INDEX IF NOT EXISTS ix_relationships_source_outbound
        ON Relationships (source_id, source_revision_id, relationship_type)
    """,
    # Audit ordering index — Audit_Records schema.
    """
    CREATE INDEX IF NOT EXISTS ix_audit_records_recorded_sequence
        ON Audit_Records (recorded_at, append_sequence)
    """,
)


# ---------------------------------------------------------------------------
# Trigger definitions.
#
# `RAISE(ABORT, ...)` rolls back the offending statement (and its enclosing
# transaction) and surfaces as `sqlite3.IntegrityError` through the DBAPI,
# which SQLAlchemy wraps as :class:`sqlalchemy.exc.IntegrityError`.
# ---------------------------------------------------------------------------


_IMMUTABLE_TABLES: Final[tuple[str, ...]] = (
    "Document_Revisions",
    "Region_Occurrences",
    "Finding_Revisions",
    "Recommendation_Revisions",
    "Decisions",
    "Relationships",
    "Trail_Revisions",
    "Trail_Steps",
    "Provenance_Manifests",
    "Audit_Records",
)


def _build_immutable_triggers() -> tuple[str, ...]:
    """Build append-only UPDATE/DELETE rejection triggers for AD-WS-4 tables.

    For each table in :data:`_IMMUTABLE_TABLES`, emit two triggers:

    - ``<table>_reject_update`` — fires ``BEFORE UPDATE`` and aborts.
    - ``<table>_reject_delete`` — fires ``BEFORE DELETE`` and aborts.
    """
    statements: list[str] = []
    for table in _IMMUTABLE_TABLES:
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_update
            BEFORE UPDATE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; UPDATE rejected per design AD-WS-4.');
            END
            """
        )
        statements.append(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN
                SELECT RAISE(ABORT,
                    '{table} is append-only; DELETE rejected per design AD-WS-4.');
            END
            """
        )
    return tuple(statements)


_ONE_SHOT_TRIGGER_STATEMENTS: Final[tuple[str, ...]] = (
    # ----- Role_Assignments: revoked_at one-shot -------------------------
    # Reject any UPDATE that mutates a column other than revoked_at.
    """
    CREATE TRIGGER IF NOT EXISTS Role_Assignments_reject_non_revoked_update
    BEFORE UPDATE ON Role_Assignments
    WHEN
           OLD.role_assignment_id     IS NOT NEW.role_assignment_id
        OR OLD.party_id               IS NOT NEW.party_id
        OR OLD.role_name              IS NOT NEW.role_name
        OR OLD.scope                  IS NOT NEW.scope
        OR OLD.authorities_granted    IS NOT NEW.authorities_granted
        OR OLD.effective_start        IS NOT NEW.effective_start
        OR OLD.effective_end          IS NOT NEW.effective_end
        OR OLD.assigning_authority_id IS NOT NEW.assigning_authority_id
        OR OLD.recorded_at            IS NOT NEW.recorded_at
    BEGIN
        SELECT RAISE(ABORT,
            'Role_Assignments columns are immutable except revoked_at (one-shot).');
    END
    """,
    # Reject any attempt to re-write revoked_at once set, or to clear it.
    """
    CREATE TRIGGER IF NOT EXISTS Role_Assignments_revoked_at_one_shot
    BEFORE UPDATE OF revoked_at ON Role_Assignments
    WHEN OLD.revoked_at IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT,
            'Role_Assignments.revoked_at is one-shot; cannot be re-set or cleared.');
    END
    """,
    # DELETE is never permitted on Role_Assignments — history is preserved.
    """
    CREATE TRIGGER IF NOT EXISTS Role_Assignments_reject_delete
    BEFORE DELETE ON Role_Assignments
    BEGIN
        SELECT RAISE(ABORT,
            'Role_Assignments is append-only; DELETE rejected per design AD-WS-4.');
    END
    """,
    # ----- Omission_Entries: resolved_at one-shot ------------------------
    """
    CREATE TRIGGER IF NOT EXISTS Omission_Entries_reject_non_resolved_update
    BEFORE UPDATE ON Omission_Entries
    WHEN
           OLD.omission_entry_id           IS NOT NEW.omission_entry_id
        OR OLD.manifest_id                 IS NOT NEW.manifest_id
        OR OLD.excluded_source_id          IS NOT NEW.excluded_source_id
        OR OLD.excluded_source_revision_id IS NOT NEW.excluded_source_revision_id
        OR OLD.category                    IS NOT NEW.category
        OR OLD.rationale                   IS NOT NEW.rationale
        OR OLD.authoring_party_id          IS NOT NEW.authoring_party_id
        OR OLD.recorded_at                 IS NOT NEW.recorded_at
    BEGIN
        SELECT RAISE(ABORT,
            'Omission_Entries columns are immutable except resolved_at (one-shot).');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS Omission_Entries_resolved_at_one_shot
    BEFORE UPDATE OF resolved_at ON Omission_Entries
    WHEN OLD.resolved_at IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT,
            'Omission_Entries.resolved_at is one-shot; cannot be re-set or cleared.');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS Omission_Entries_reject_delete
    BEFORE DELETE ON Omission_Entries
    BEGIN
        SELECT RAISE(ABORT,
            'Omission_Entries is append-only; DELETE rejected per design AD-WS-4.');
    END
    """,
    # ----- Identifier_Registry: identifier non-reuse ---------------------
    # AD-WS-2: once bound, an identifier is never re-bound to different
    # content. UPDATE/DELETE are rejected wholesale.
    """
    CREATE TRIGGER IF NOT EXISTS Identifier_Registry_reject_update
    BEFORE UPDATE ON Identifier_Registry
    BEGIN
        SELECT RAISE(ABORT,
            'Identifier_Registry is append-only; UPDATE rejected per AD-WS-2.');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS Identifier_Registry_reject_delete
    BEFORE DELETE ON Identifier_Registry
    BEGIN
        SELECT RAISE(ABORT,
            'Identifier_Registry is append-only; DELETE rejected per AD-WS-2.');
    END
    """,
)


def _build_schema_statements() -> tuple[str, ...]:
    """Concatenate tables → indexes → triggers in dependency order."""
    return (
        *_TABLE_STATEMENTS,
        *_INDEX_STATEMENTS,
        *_build_immutable_triggers(),
        *_ONE_SHOT_TRIGGER_STATEMENTS,
    )


SCHEMA_STATEMENTS: Final[tuple[str, ...]] = _build_schema_statements()


# ---------------------------------------------------------------------------
# Slice 2 additive columns.
#
# Per second-walking-slice design AD-WS-17 and AD-WS-19, Slice 2 introduces
# two strictly additive columns on existing Slice 1 tables:
#
#   * ``Relationships.semantic_role`` — NULLable discriminator that lets a
#     ``Relates To`` row carry the semantic role ``'review'`` for Plan Review
#     Revisions (AD-WS-17). The column is NULL for every pre-existing Slice 1
#     row; the existing ``Relationships`` UPDATE/DELETE rejection triggers
#     and the outbound/backlink indexes are unchanged.
#
#   * ``Identifier_Registry.resource_kind`` — NULLable tag that lets Slice 2
#     keep the Project and Activity Plan identifier sets disjoint (Requirement
#     4.5) by tagging each Resource identifier with the registry-kind it
#     belongs to (``'project'``, ``'activity_plan'``, ``'objective'``, etc.).
#     The column is NULL for every pre-existing Slice 1 row; the existing
#     primary-key UNIQUE constraint on ``identifier`` and the
#     ``Identifier_Registry`` immutability triggers are unchanged.
#
# Requirements: 4.5 (Project/Activity-Plan identifier disjointness), 19.2
# (additive extension only), 19.4 (no mutation of existing Slice 1 rows).
# ---------------------------------------------------------------------------


SLICE2_ADDITIVE_COLUMNS: Final[tuple[tuple[str, str, str], ...]] = (
    # (table, column, column definition)
    ("Relationships", "semantic_role", "TEXT NULL"),
    ("Identifier_Registry", "resource_kind", "TEXT NULL"),
)


def _apply_slice2_additive_columns(connection: Connection) -> None:
    """Emit ``ALTER TABLE ADD COLUMN`` for each Slice 2 column that's missing.

    SQLite does not support ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, so
    membership is checked through ``PRAGMA table_info(<table>)`` and the
    ``ALTER TABLE`` is issued only when the column is absent. This keeps the
    extension idempotent across repeated calls to :func:`create_schema` and
    leaves every existing Slice 1 row byte-equivalent (Requirement 19.4): the
    new columns receive the SQLite default value ``NULL`` for every prior row.

    The function does not touch the ``Relationships`` UPDATE/DELETE triggers
    or the ``Identifier_Registry`` primary-key UNIQUE constraint on
    ``identifier`` — adding a NULLable column with no default has no effect
    on triggers or indexes already defined.
    """
    for table, column, column_definition in SLICE2_ADDITIVE_COLUMNS:
        existing_columns = {
            row[1]
            for row in connection.execute(text(f"PRAGMA table_info({table})")).all()
        }
        if column in existing_columns:
            continue
        # Identifiers come from the module-level constant `SLICE2_ADDITIVE_COLUMNS`,
        # not from user input, so the interpolation is safe.
        connection.execute(
            text(f"ALTER TABLE {table} ADD COLUMN {column} {column_definition}")
        )


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def create_schema(engine: Engine) -> None:
    """Create every table, index, and trigger required by the slice.

    The function is idempotent: every statement uses ``IF NOT EXISTS`` so it
    is safe to call against an already-initialised database (the typical
    pattern in tests and in application startup, design
    §"Application-Level Composition").

    As a side effect, :func:`install_pragmas` is invoked on ``engine`` so that
    every subsequent DBAPI connection has ``journal_mode=WAL`` and
    ``foreign_keys=ON`` set.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database.
    """
    install_pragmas(engine)

    # `BEGIN IMMEDIATE` (via `engine.begin()`) wraps the whole schema in one
    # transaction so partial DDL cannot leave the database in an inconsistent
    # state if a later CREATE fails.
    with engine.begin() as conn:
        for statement in SCHEMA_STATEMENTS:
            conn.execute(text(statement))
        # Slice 2 additive columns (Requirements 4.5, 19.2, 19.4). Applied in
        # the same transaction as the base schema so a partially-extended
        # database cannot be observed by any other connection.
        _apply_slice2_additive_columns(conn)
