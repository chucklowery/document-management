# Feature: fourth-walking-slice, Property 49: Outcome-entity immutability
"""Property 49 — Outcome-entity immutability across every Slice 4 table (task 15.4).

**Property 49: Outcome-entity immutability**

*For all* Measurement Definition Revisions, Measurement Records, Observed
Outcome Revisions, Success-Condition Assessment Records, and Outcome
Review Records finalized at any observation point in the test session, at
every later observation point the Resource row (where applicable), the
Revision or Record row, every constituent field, and every ``Addresses``
and ``Cites`` Relationship sourced from or targeting it are
byte-equivalent to their state at first finalization; the corresponding
``Audit_Records`` rows are also byte-equivalent. Attempts to UPDATE or
DELETE any of these rows or their Relationships are rejected by the
AD-WS-36 / AD-WS-37 append-only triggers.

**Validates: Requirements 44.7, 45.6, 47.7, 48.6, 49.7, 57.3, 57.5,
61.4**

Strategy
========

Each Hypothesis case (a) seeds a full Slice 4 outcome-measurement
pipeline — one Measurement Definition Resource + Revision, one
Measurement Record (native or imported, drawn per case), one Observed
Outcome Resource + Revision (optionally a second Revision linked through
the AD-WS-36 ``predecessor_revision_id`` chain), one Success-Condition
Assessment Record, and one Outcome Review Record — together with every
``Addresses`` / ``Cites`` Relationship the five Outcome_Service write
services persist (AD-WS-35) and one consequential ``Audit_Records`` row
per finalization (AD-WS-5 audit atomicity) — then (b) generates a
Hypothesis-drawn sequence of UPDATE / DELETE attempts against every
Slice 4 table.

Per case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing Strategy"
   — per-case database isolation) carrying the Slice 1 schema
   (:func:`walking_slice.persistence.create_schema` — ``Parties``,
   ``Relationships``, ``Audit_Records``, ``Identifier_Registry`` plus
   the additive Slice 2 ``Relationships.semantic_role`` column) and the
   Slice 4 schema (:func:`walking_slice.outcome._persistence.create_outcome_schema`
   — the seven Outcome_Service tables with their AD-WS-36 / AD-WS-37
   ``<table>_reject_update`` and ``<table>_reject_delete`` triggers).

   The Slice 4 tables carry no SQL FOREIGN KEY to any Slice 2 / Slice 3
   table (prior-slice identity references are validated in application
   code per AD-WS-40), and the ``Relationships`` table is polymorphic
   (``source_kind`` / ``target_kind`` are un-constrained TEXT), so the
   seed can reference the Intended Outcome Revision, Completion Record,
   and produced Deliverable Revision identities the Outcome Review cites
   without materialising the prior-slice rows. This keeps the property
   focused on the Slice 4 rows it quantifies over.

2. Seeds the full pipeline by direct INSERT (mirroring the established
   Slice 3 Property 33 convention), writing each Slice 4 row, every
   AD-WS-35 ``Addresses`` / ``Cites`` Relationship row alongside the
   relevant entity, and one consequential ``Audit_Records`` row per
   finalization via :meth:`AuditLog.append_consequential`, so the
   byte-equivalence check covers exactly the rows a happy-path
   Outcome_Service pipeline persists.

3. Snapshots every Slice 4 row, every ``Relationships`` row, and every
   ``Audit_Records`` row by SELECT-ing every column in stable PK order
   and storing the rows as ``tuple`` objects keyed by table name. The
   snapshot is the byte-equivalence ground truth for the post-attack
   comparison.

4. Iterates the drawn attack list, issuing each UPDATE or DELETE against
   the table named in the attack tuple, and asserts every attack raises
   :class:`sqlalchemy.exc.IntegrityError` — the AD-WS-36 / AD-WS-37
   trigger fired and the offending statement (and its enclosing
   transaction) was rolled back.

5. Re-snapshots the same rows and asserts byte-for-byte equality with
   the pre-attack snapshot (Property 49's universal quantifier). Because
   a trigger ``RAISE(ABORT, …)`` rolls back the whole statement, the
   ``Audit_Records`` snapshot being unchanged also confirms no
   collateral row (consequential or otherwise) survived a rejected
   mutation attempt — the append-only contract is enforced at the
   database layer, not by an application-level denial append (a raw SQL
   UPDATE / DELETE never reaches an Outcome_Service code path).

The ``Relationships`` and ``Audit_Records`` tables additionally carry
the Slice 1 AD-WS-4 unconditional UPDATE / DELETE rejection triggers
(Property 12); this property snapshots both so the post-attack diff
catches any collateral side effect a Slice 4 mutation attempt might have
produced on the Relationship and Audit rows sourced from or targeting a
Slice 4 entity.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, Optional

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.clock import FixedClock
from walking_slice.outcome._persistence import (
    OUTCOME_IMMUTABLE_TABLES,
    create_outcome_schema,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# Property 49 quantifies over Slice 4 entities and their Relationship /
# Audit rows, not over Parties / Intended Outcomes / Completions, so a small
# fixed Identity set is sufficient. The prior-slice references the seed cites
# (Intended Outcome Revision, Completion Record, produced Deliverable
# Revision) are opaque identifiers; no prior-slice row is materialised
# because no Slice 4 table or Relationships row FK-references one.
# ---------------------------------------------------------------------------


_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-00000000a001"
_RECORDING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-00000000a002"
_REVIEWING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-00000000a003"

_INTENDED_OUTCOME_RESOURCE_ID: Final[str] = "00000000-0000-7000-8000-00000000b001"
_INTENDED_OUTCOME_REVISION_ID: Final[str] = "00000000-0000-7000-8000-00000000b002"
_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-00000000b003"
_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-00000000b004"
_DELIVERABLE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-00000000b005"

_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-00000000c001"
_SCOPE: Final[str] = "pilot/team-d"
_TS_FIXED: Final[str] = "2026-01-01T00:00:00.000Z"
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Per-table snapshot specifications.
#
# For each protected row class the test snapshots, name the columns to
# SELECT (in a stable order) and the columns to ORDER BY so the
# byte-equivalence comparison is deterministic. The seven Slice 4 tables,
# the ``Relationships`` table, and the ``Audit_Records`` table are all
# snapshotted.
# ---------------------------------------------------------------------------


_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "Measurement_Definitions": {
        "columns": (
            "measurement_definition_id",
            "target_intended_outcome_resource_id",
            "created_at",
        ),
        "order_by": ("measurement_definition_id",),
    },
    "Measurement_Definition_Revisions": {
        "columns": (
            "measurement_definition_revision_id",
            "measurement_definition_id",
            "target_intended_outcome_resource_id",
            "target_intended_outcome_revision_id",
            "measurand_description",
            "unit_of_measure",
            "observation_window",
            "cadence",
            "data_source",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("measurement_definition_revision_id",),
    },
    "Measurement_Records": {
        "columns": (
            "measurement_record_id",
            "target_measurement_definition_id",
            "target_measurement_definition_revision_id",
            "origin",
            "observed_value",
            "observed_value_unit",
            "observation_time",
            "source_system_id",
            "source_system_record_id",
            "source_system_authority",
            "source_system_retrieval_at",
            "import_at",
            "recording_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("measurement_record_id",),
    },
    "Observed_Outcomes": {
        "columns": (
            "observed_outcome_id",
            "target_intended_outcome_resource_id",
            "created_at",
        ),
        "order_by": ("observed_outcome_id",),
    },
    "Observed_Outcome_Revisions": {
        "columns": (
            "observed_outcome_revision_id",
            "observed_outcome_id",
            "outcome_kind",
            "target_intended_outcome_resource_id",
            "target_intended_outcome_revision_id",
            "assessment_summary",
            "predecessor_revision_id",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("recorded_at", "observed_outcome_revision_id"),
    },
    "Success_Condition_Assessment_Records": {
        "columns": (
            "assessment_id",
            "target_intended_outcome_resource_id",
            "target_intended_outcome_revision_id",
            "sourced_observed_outcome_id",
            "sourced_observed_outcome_revision_id",
            "assessment_category",
            "assessment_rationale",
            "assessing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("assessment_id",),
    },
    "Outcome_Review_Records": {
        "columns": (
            "outcome_review_id",
            "target_intended_outcome_resource_id",
            "target_intended_outcome_revision_id",
            "review_outcome",
            "attribution_stance",
            "confidence",
            "review_rationale",
            "attribution_evidence_reference",
            "reviewing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("outcome_review_id",),
    },
    # ``Relationships`` carries every AD-WS-35 row written alongside a
    # Slice 4 entity. ORDER BY relationship_id for determinism; the
    # post-attack comparison covers every row.
    "Relationships": {
        "columns": (
            "relationship_id",
            "relationship_type",
            "source_kind",
            "source_id",
            "source_revision_id",
            "target_kind",
            "target_id",
            "target_revision_id",
            "authoring_party_id",
            "recorded_at",
            "semantic_role",
        ),
        "order_by": ("relationship_id",),
    },
    # ``Audit_Records`` carries one consequential row per Slice 4
    # finalization. Snapshotting the full row catches any UPDATE / DELETE
    # collateral the attack loop might have produced and confirms no
    # extra row was appended by a rejected mutation attempt.
    "Audit_Records": {
        "columns": (
            "audit_record_id",
            "append_sequence",
            "actor_party_id",
            "action_type",
            "outcome",
            "target_id",
            "target_revision_id",
            "evaluated_role_assignment_id",
            "authorities_required",
            "authorities_held",
            "reason_code",
            "correlation_id",
            "recorded_at",
        ),
        "order_by": ("append_sequence",),
    },
}


# ---------------------------------------------------------------------------
# Per-table attack alphabets.
#
# ``pk_columns`` names the columns the attacker supplies in the WHERE clause
# to target one row. ``update_columns`` is the allow-list the Hypothesis
# attacker draws ``column_to_update`` from — it names every persisted column
# (PK and non-PK) so each case exercises the full append-only contract on
# every Slice 4 table.
#
# Only the seven Slice 4 tables appear in the attack list — the property
# spec says "UPDATE / DELETE against every Slice 4 table". The
# ``Relationships`` and ``Audit_Records`` tables are append-only via the
# Slice 1 AD-WS-4 triggers (Property 12) and are snapshotted (so a
# collateral side effect surfaces in the post-attack diff) but not attacked
# directly here.
# ---------------------------------------------------------------------------


_ATTACK_COLUMNS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    name: {"pk_columns": (spec["columns"][0],), "update_columns": spec["columns"]}
    for name, spec in _TABLE_SPECS.items()
    if name in OUTCOME_IMMUTABLE_TABLES
}


# Candidate UPDATE values. The bag is intentionally small so the
# (table, kind, column, value) cube fits inside the 100-case budget. The
# four shapes cover a different canonical-form identifier, an arbitrary
# string, the empty string, and SQL NULL.
_UPDATE_VALUE_BAG: Final[tuple[Any, ...]] = (
    "00000000-0000-7000-8000-00000000ffff",
    "tampered",
    "",
    None,
)


_TABLE_NAMES: Final[tuple[str, ...]] = tuple(_ATTACK_COLUMNS.keys())


# ---------------------------------------------------------------------------
# Per-case engine helper.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a per-case engine with the Slice 1 + Slice 4 schemas installed."""
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

    # Slice 1: Parties, Identifier_Registry, Relationships, Audit_Records …
    # plus the additive Slice 2 ``Relationships.semantic_role`` column.
    create_schema(engine)
    # Slice 4: the seven Outcome_Service tables with their AD-WS-36 /
    # AD-WS-37 append-only UPDATE / DELETE rejection triggers.
    create_outcome_schema(engine)
    return engine


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string (matches AD-WS-2)."""
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Relationship insert helper.
# ---------------------------------------------------------------------------


def _insert_relationship(
    conn: Connection,
    *,
    relationship_id: str,
    relationship_type: str,
    source_kind: str,
    source_id: str,
    source_revision_id: Optional[str],
    target_kind: str,
    target_id: str,
    target_revision_id: Optional[str],
    semantic_role: Optional[str],
    authoring_party_id: str,
    recorded_at: str = _TS_FIXED,
) -> None:
    """Insert one ``Relationships`` row with explicit ``semantic_role``.

    Mirrors the column list the Slice 4 services write so the snapshot
    captures the exact AD-WS-35 wiring the production code persists.
    """
    conn.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at, semantic_role
            ) VALUES (
                :relationship_id, :relationship_type,
                :source_kind, :source_id, :source_revision_id,
                :target_kind, :target_id, :target_revision_id,
                :authoring_party_id, :recorded_at, :semantic_role
            )
            """
        ),
        {
            "relationship_id": relationship_id,
            "relationship_type": relationship_type,
            "source_kind": source_kind,
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_revision_id": target_revision_id,
            "authoring_party_id": authoring_party_id,
            "recorded_at": recorded_at,
            "semantic_role": semantic_role,
        },
    )


def _seed_party(conn: Connection, party_id: str, name: str) -> None:
    """Insert one ``Parties`` row required by the FK chain."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": name, "ts": _TS_FIXED},
    )


# ---------------------------------------------------------------------------
# Pipeline seeding.
#
# Inserts the full Slice 4 outcome-measurement pipeline plus every AD-WS-35
# Relationship row and one consequential Audit_Records row per finalization.
# Returns the seeded shape (relationship and audit counts) so the test can
# sanity-check the seed before the attack loop.
# ---------------------------------------------------------------------------


def _seed_full_pipeline(engine: Engine, *, audit_log: AuditLog, config: dict[str, Any]) -> dict[str, Any]:
    """Seed one full Slice 4 pipeline; return identifiers and expected counts."""
    measurement_definition_id = _new_uuid7()
    measurement_definition_revision_id = _new_uuid7()
    measurement_record_id = _new_uuid7()
    observed_outcome_id = _new_uuid7()
    observed_outcome_revision_id = _new_uuid7()
    second_oo_revision_id = _new_uuid7() if config["second_oo_revision"] else None
    assessment_id = _new_uuid7()
    outcome_review_id = _new_uuid7()

    correlation_id = _new_uuid7()

    expected_relationships = 0
    expected_audits = 0

    with engine.begin() as conn:
        # --- Parties ---------------------------------------------------
        _seed_party(conn, _AUTHORING_PARTY_ID, "Property 49 Author")
        _seed_party(conn, _RECORDING_PARTY_ID, "Property 49 Recorder")
        _seed_party(conn, _REVIEWING_PARTY_ID, "Property 49 Reviewer")

        # --- Measurement Definition Resource + Revision ---------------
        conn.execute(
            text(
                """
                INSERT INTO Measurement_Definitions (
                    measurement_definition_id,
                    target_intended_outcome_resource_id, created_at
                ) VALUES (:md, :io_res, :ts)
                """
            ),
            {
                "md": measurement_definition_id,
                "io_res": _INTENDED_OUTCOME_RESOURCE_ID,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Measurement_Definition_Revisions (
                    measurement_definition_revision_id,
                    measurement_definition_id,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    measurand_description, unit_of_measure,
                    observation_window, cadence, data_source,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :md, :io_res, :io_rev,
                    'Median ticket resolution time', 'hours',
                    'Rolling 30 days', 'Weekly', 'Helpdesk export',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": measurement_definition_revision_id,
                "md": measurement_definition_id,
                "io_res": _INTENDED_OUTCOME_RESOURCE_ID,
                "io_rev": _INTENDED_OUTCOME_REVISION_ID,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-35: Measurement Definition Revision -> Intended Outcome
        # Revision via Addresses, semantic_role NULL.
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Addresses",
            source_kind="measurement_definition_revision",
            source_id=measurement_definition_id,
            source_revision_id=measurement_definition_revision_id,
            target_kind="intended_outcome_revision",
            target_id=_INTENDED_OUTCOME_RESOURCE_ID,
            target_revision_id=_INTENDED_OUTCOME_REVISION_ID,
            semantic_role=None,
            authoring_party_id=_AUTHORING_PARTY_ID,
        )
        expected_relationships += 1
        audit_log.append_consequential(
            conn,
            actor_party_id=_AUTHORING_PARTY_ID,
            action_type="create.measurement_definition",
            target_id=measurement_definition_revision_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )
        expected_audits += 1

        # --- Measurement Record (native or imported) ------------------
        if config["measurement_origin"] == "imported":
            source_cols = {
                "ssid": "helpdesk-prod",
                "ssrid": "TICKET-001",
                "ssauth": config["source_system_authority"],
                "ssret": _TS_FIXED,
                "import_at": _TS_FIXED,
            }
        else:
            source_cols = {
                "ssid": None,
                "ssrid": None,
                "ssauth": None,
                "ssret": None,
                "import_at": None,
            }
        conn.execute(
            text(
                """
                INSERT INTO Measurement_Records (
                    measurement_record_id,
                    target_measurement_definition_id,
                    target_measurement_definition_revision_id,
                    origin, observed_value, observed_value_unit,
                    observation_time, source_system_id,
                    source_system_record_id, source_system_authority,
                    source_system_retrieval_at, import_at,
                    recording_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :mr, :md, :mdr, :origin, '42.5', 'hours',
                    :ts, :ssid, :ssrid, :ssauth, :ssret, :import_at,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "mr": measurement_record_id,
                "md": measurement_definition_id,
                "mdr": measurement_definition_revision_id,
                "origin": config["measurement_origin"],
                "ts": _TS_FIXED,
                "party": _RECORDING_PARTY_ID,
                "scope": _SCOPE,
                **source_cols,
            },
        )
        # AD-WS-35: Measurement Record -> Measurement Definition Revision
        # via Cites, semantic_role 'measurement_basis'.
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Cites",
            source_kind="measurement_record",
            source_id=measurement_record_id,
            source_revision_id=None,
            target_kind="measurement_definition_revision",
            target_id=measurement_definition_id,
            target_revision_id=measurement_definition_revision_id,
            semantic_role="measurement_basis",
            authoring_party_id=_RECORDING_PARTY_ID,
        )
        expected_relationships += 1
        audit_log.append_consequential(
            conn,
            actor_party_id=_RECORDING_PARTY_ID,
            action_type="create.measurement_record",
            target_id=measurement_record_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )
        expected_audits += 1

        # --- Observed Outcome Resource + Revision(s) ------------------
        conn.execute(
            text(
                """
                INSERT INTO Observed_Outcomes (
                    observed_outcome_id,
                    target_intended_outcome_resource_id, created_at
                ) VALUES (:oo, :io_res, :ts)
                """
            ),
            {
                "oo": observed_outcome_id,
                "io_res": _INTENDED_OUTCOME_RESOURCE_ID,
                "ts": _TS_FIXED,
            },
        )

        def _insert_oo_revision(rev_id: str, predecessor: Optional[str]) -> None:
            conn.execute(
                text(
                    """
                    INSERT INTO Observed_Outcome_Revisions (
                        observed_outcome_revision_id, observed_outcome_id,
                        outcome_kind, target_intended_outcome_resource_id,
                        target_intended_outcome_revision_id,
                        assessment_summary, predecessor_revision_id,
                        authoring_party_id, applicable_scope, recorded_at
                    ) VALUES (
                        :rev, :oo, 'observed', :io_res, :io_rev,
                        'Resolution time trending toward target.',
                        :pred, :party, :scope, :ts
                    )
                    """
                ),
                {
                    "rev": rev_id,
                    "oo": observed_outcome_id,
                    "io_res": _INTENDED_OUTCOME_RESOURCE_ID,
                    "io_rev": _INTENDED_OUTCOME_REVISION_ID,
                    "pred": predecessor,
                    "party": _AUTHORING_PARTY_ID,
                    "scope": _SCOPE,
                    "ts": _TS_FIXED,
                },
            )
            # AD-WS-35: Observed Outcome Revision -> Intended Outcome
            # Revision via Addresses (semantic_role NULL) and -> cited
            # Measurement Record via Cites (semantic_role
            # 'observation_basis').
            _insert_relationship(
                conn,
                relationship_id=_new_uuid7(),
                relationship_type="Addresses",
                source_kind="observed_outcome_revision",
                source_id=observed_outcome_id,
                source_revision_id=rev_id,
                target_kind="intended_outcome_revision",
                target_id=_INTENDED_OUTCOME_RESOURCE_ID,
                target_revision_id=_INTENDED_OUTCOME_REVISION_ID,
                semantic_role=None,
                authoring_party_id=_AUTHORING_PARTY_ID,
            )
            _insert_relationship(
                conn,
                relationship_id=_new_uuid7(),
                relationship_type="Cites",
                source_kind="observed_outcome_revision",
                source_id=observed_outcome_id,
                source_revision_id=rev_id,
                target_kind="measurement_record",
                target_id=measurement_record_id,
                target_revision_id=None,
                semantic_role="observation_basis",
                authoring_party_id=_AUTHORING_PARTY_ID,
            )
            audit_log.append_consequential(
                conn,
                actor_party_id=_AUTHORING_PARTY_ID,
                action_type="create.observed_outcome",
                target_id=rev_id,
                correlation_id=correlation_id,
                recorded_time=_FIXED_NOW,
            )

        _insert_oo_revision(observed_outcome_revision_id, None)
        expected_relationships += 2
        expected_audits += 1
        if second_oo_revision_id is not None:
            _insert_oo_revision(second_oo_revision_id, observed_outcome_revision_id)
            expected_relationships += 2
            expected_audits += 1

        # The assessment and review source the most-recent Revision.
        latest_oo_revision_id = second_oo_revision_id or observed_outcome_revision_id

        # --- Success-Condition Assessment Record ----------------------
        conn.execute(
            text(
                """
                INSERT INTO Success_Condition_Assessment_Records (
                    assessment_id, target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    sourced_observed_outcome_id,
                    sourced_observed_outcome_revision_id,
                    assessment_category, assessment_rationale,
                    assessing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :io_res, :io_rev, :oo, :oo_rev,
                    :category, :rationale, :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "aid": assessment_id,
                "io_res": _INTENDED_OUTCOME_RESOURCE_ID,
                "io_rev": _INTENDED_OUTCOME_REVISION_ID,
                "oo": observed_outcome_id,
                "oo_rev": latest_oo_revision_id,
                "category": config["assessment_category"],
                "rationale": config["assessment_rationale"],
                "party": _AUTHORING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Addresses",
            source_kind="success_condition_assessment_record",
            source_id=assessment_id,
            source_revision_id=None,
            target_kind="intended_outcome_revision",
            target_id=_INTENDED_OUTCOME_RESOURCE_ID,
            target_revision_id=_INTENDED_OUTCOME_REVISION_ID,
            semantic_role=None,
            authoring_party_id=_AUTHORING_PARTY_ID,
        )
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Cites",
            source_kind="success_condition_assessment_record",
            source_id=assessment_id,
            source_revision_id=None,
            target_kind="observed_outcome_revision",
            target_id=observed_outcome_id,
            target_revision_id=latest_oo_revision_id,
            semantic_role="assessment_basis",
            authoring_party_id=_AUTHORING_PARTY_ID,
        )
        expected_relationships += 2
        audit_log.append_consequential(
            conn,
            actor_party_id=_AUTHORING_PARTY_ID,
            action_type="create.success_condition_assessment",
            target_id=assessment_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )
        expected_audits += 1

        # --- Outcome Review Record ------------------------------------
        conn.execute(
            text(
                """
                INSERT INTO Outcome_Review_Records (
                    outcome_review_id, target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id, review_outcome,
                    attribution_stance, confidence, review_rationale,
                    attribution_evidence_reference, reviewing_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :orid, :io_res, :io_rev, :outcome, :stance,
                    :confidence, 'Outcome achieved within target window.',
                    :evidence, :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "orid": outcome_review_id,
                "io_res": _INTENDED_OUTCOME_RESOURCE_ID,
                "io_rev": _INTENDED_OUTCOME_REVISION_ID,
                "outcome": config["review_outcome"],
                "stance": config["attribution_stance"],
                "confidence": config["confidence"],
                "evidence": config["attribution_evidence_reference"],
                "party": _REVIEWING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Addresses",
            source_kind="outcome_review_record",
            source_id=outcome_review_id,
            source_revision_id=None,
            target_kind="intended_outcome_revision",
            target_id=_INTENDED_OUTCOME_RESOURCE_ID,
            target_revision_id=_INTENDED_OUTCOME_REVISION_ID,
            semantic_role=None,
            authoring_party_id=_REVIEWING_PARTY_ID,
        )
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Cites",
            source_kind="outcome_review_record",
            source_id=outcome_review_id,
            source_revision_id=None,
            target_kind="success_condition_assessment_record",
            target_id=assessment_id,
            target_revision_id=None,
            semantic_role="review_assessment",
            authoring_party_id=_REVIEWING_PARTY_ID,
        )
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Cites",
            source_kind="outcome_review_record",
            source_id=outcome_review_id,
            source_revision_id=None,
            target_kind="completion_record",
            target_id=_COMPLETION_ID,
            target_revision_id=None,
            semantic_role="review_completion",
            authoring_party_id=_REVIEWING_PARTY_ID,
        )
        _insert_relationship(
            conn,
            relationship_id=_new_uuid7(),
            relationship_type="Cites",
            source_kind="outcome_review_record",
            source_id=outcome_review_id,
            source_revision_id=None,
            target_kind="deliverable_revision",
            target_id=_DELIVERABLE_ID,
            target_revision_id=_DELIVERABLE_REVISION_ID,
            semantic_role="review_deliverable",
            authoring_party_id=_REVIEWING_PARTY_ID,
        )
        expected_relationships += 4
        audit_log.append_consequential(
            conn,
            actor_party_id=_REVIEWING_PARTY_ID,
            action_type="create.outcome_review",
            target_id=outcome_review_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )
        expected_audits += 1

    return {
        "expected_relationships": expected_relationships,
        "expected_audits": expected_audits,
    }


# ---------------------------------------------------------------------------
# Snapshot helper.
# ---------------------------------------------------------------------------


def _snapshot(engine: Engine) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Snapshot every protected row as ``{table_name: tuple_of_rows}``."""
    out: dict[str, tuple[tuple[Any, ...], ...]] = {}
    with engine.connect() as conn:
        for table_name, spec in _TABLE_SPECS.items():
            columns = ", ".join(spec["columns"])
            order_by = ", ".join(spec["order_by"])
            rows = conn.execute(
                text(f"SELECT {columns} FROM {table_name} ORDER BY {order_by}")
            ).all()
            normalized = tuple(
                tuple(bytes(v) if isinstance(v, memoryview) else v for v in row)
                for row in rows
            )
            out[table_name] = normalized
    return out


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


_SOURCE_SYSTEM_AUTHORITY = st.sampled_from(
    ("authoritative", "replica", "projection", "index", "federation")
)
_ASSESSMENT_CATEGORY = st.sampled_from(
    ("Satisfied", "Partially_Satisfied", "Not_Satisfied", "Unassessable")
)
_REVIEW_OUTCOME = st.sampled_from(
    ("Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive")
)
_ATTRIBUTION_STANCE = st.sampled_from(
    ("Asserted", "Partial", "Unattributed", "Contradicted")
)
_CONFIDENCE = st.sampled_from(("High", "Moderate", "Low"))


@st.composite
def _pipeline_strategy(draw) -> dict[str, Any]:
    """Draw one full-pipeline configuration honoring the schema CHECKs."""
    category = draw(_ASSESSMENT_CATEGORY)
    # Requirement 48.3: Unassessable requires a >= 200-char rationale; all
    # other categories require 1..4000.
    if category == "Unassessable":
        rationale = "U" * 200
    else:
        rationale = "Resolution time met the success condition."

    stance = draw(_ATTRIBUTION_STANCE)
    # Requirement 49.4: Asserted / Contradicted require a non-empty
    # attribution-evidence reference; the others may be empty.
    if stance in ("Asserted", "Contradicted"):
        evidence = "Causal analysis memo ANALYSIS-001."
    else:
        evidence = draw(st.sampled_from(("", "Optional supporting note.")))

    return {
        "measurement_origin": draw(st.sampled_from(("native", "imported"))),
        "source_system_authority": draw(_SOURCE_SYSTEM_AUTHORITY),
        "assessment_category": category,
        "assessment_rationale": rationale,
        "review_outcome": draw(_REVIEW_OUTCOME),
        "attribution_stance": stance,
        "confidence": draw(_CONFIDENCE),
        "attribution_evidence_reference": evidence,
        "second_oo_revision": draw(st.booleans()),
    }


@st.composite
def _attack_strategy(draw) -> dict[str, Any]:
    """Draw one (table, kind, column?, new_value) attack tuple."""
    table = draw(st.sampled_from(_TABLE_NAMES))
    kind: Literal["update", "delete"] = draw(st.sampled_from(("update", "delete")))
    column = draw(st.sampled_from(_ATTACK_COLUMNS[table]["update_columns"]))
    new_value = draw(st.sampled_from(_UPDATE_VALUE_BAG))
    return {"table": table, "kind": kind, "column": column, "new_value": new_value}


# Each scenario is 1..15 attacks; ``min_size=1`` guarantees at least one
# rejection attempt per case.
_scenario_strategy = st.lists(_attack_strategy(), min_size=1, max_size=15)


# ---------------------------------------------------------------------------
# Attack executor.
# ---------------------------------------------------------------------------


def _first_pk(engine: Engine, *, table: str) -> Optional[dict[str, Any]]:
    """Return the PK column values of the first row in ``table``, or ``None``."""
    spec = _ATTACK_COLUMNS[table]
    pk_columns = spec["pk_columns"]
    pk_select = ", ".join(pk_columns)
    order_by = ", ".join(pk_columns)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {pk_select} FROM {table} ORDER BY {order_by} LIMIT 1")
        ).first()
    if row is None:
        return None
    return {col: row[i] for i, col in enumerate(pk_columns)}


def _apply_attack(engine: Engine, attack: dict[str, Any]) -> None:
    """Execute one attack against the engine and assert it was rejected."""
    table = attack["table"]
    kind = attack["kind"]
    pk_values = _first_pk(engine, table=table)
    if pk_values is None:
        raise AssertionError(
            f"Seed regression: no row found in {table!r} for Property 49 "
            f"attack {attack!r}. The pipeline seed must insert at least one "
            f"row per Slice 4 table."
        )

    where_clause = " AND ".join(f"{col} = :pk_{col}" for col in pk_values.keys())
    params: dict[str, Any] = {f"pk_{col}": val for col, val in pk_values.items()}

    if kind == "delete":
        statement = f"DELETE FROM {table} WHERE {where_clause}"
    else:
        column = attack["column"]
        params["new_value"] = attack["new_value"]
        statement = f"UPDATE {table} SET {column} = :new_value WHERE {where_clause}"

    raised = False
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params)
    except IntegrityError:
        raised = True
    except Exception as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"Attack {attack!r} raised {type(exc).__name__} instead of "
            f"sqlalchemy.exc.IntegrityError; the AD-WS-36 / AD-WS-37 trigger "
            f"contract regressed."
        ) from exc

    assert raised, (
        f"Attack {attack!r} was NOT rejected — the AD-WS-36 / AD-WS-37 "
        f"immutability trigger on {table!r} failed to fire. Property 49 / "
        f"Requirements 44.7, 45.6, 47.7, 48.6, 49.7, 57.3, 57.5 require every "
        f"UPDATE/DELETE against a Slice 4 entity to raise IntegrityError."
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: fourth-walking-slice, Property 49: Outcome-entity immutability
@given(pipeline=_pipeline_strategy(), scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup builds a full Slice 4 pipeline (≈8 INSERTs plus the
    # AD-WS-35 Relationship rows and the per-finalization audit appends)
    # and runs the attack loop and the post-attack snapshot diff. The
    # data-generation health check is suppressed because the per-case work
    # is heavier than a pure in-memory property test by design.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_outcome_entity_immutability(
    pipeline: dict[str, Any], scenario: list[dict[str, Any]]
) -> None:
    """Every UPDATE/DELETE against a Slice 4 entity is rejected; every Slice 4
    row, every ``Addresses`` / ``Cites`` Relationship row, and every
    consequential ``Audit_Records`` row remain byte-equivalent across every
    later observation point."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop49_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            clock = FixedClock(_FIXED_NOW)
            audit_log = AuditLog(clock)

            # --- Phase 1: seed the full pipeline -------------------
            seeded = _seed_full_pipeline(
                engine, audit_log=audit_log, config=pipeline
            )

            # --- Phase 2: snapshot ---------------------------------
            pre_snapshot = _snapshot(engine)

            # Sanity-check the seeded shape: each Slice 4 table has at
            # least one row (Observed_Outcome_Revisions may have two).
            for table_name in _TABLE_NAMES:
                assert len(pre_snapshot[table_name]) >= 1, (
                    f"Seed regression: {table_name!r} has "
                    f"{len(pre_snapshot[table_name])} rows; expected >= 1."
                )
            assert len(pre_snapshot["Relationships"]) == seeded["expected_relationships"], (
                f"Seed regression: Relationships has "
                f"{len(pre_snapshot['Relationships'])} rows; expected "
                f"{seeded['expected_relationships']} AD-WS-35 rows."
            )
            assert len(pre_snapshot["Audit_Records"]) == seeded["expected_audits"], (
                f"Seed regression: Audit_Records has "
                f"{len(pre_snapshot['Audit_Records'])} rows; expected "
                f"{seeded['expected_audits']} consequential rows."
            )

            # --- Phase 3: attack loop ------------------------------
            for attack in scenario:
                _apply_attack(engine, attack)

            # --- Phase 4: post-attack byte-equivalence -------------
            post_snapshot = _snapshot(engine)
            for table_name in _TABLE_SPECS:
                assert post_snapshot[table_name] == pre_snapshot[table_name], (
                    f"Byte-equivalence violation on {table_name!r}: a Slice 4 "
                    f"UPDATE/DELETE attempt mutated a protected row. Property 49 "
                    f"requires every Slice 4 row, Relationship, and Audit_Records "
                    f"row to be byte-equivalent across every later observation "
                    f"point."
                )
        finally:
            engine.dispose()
