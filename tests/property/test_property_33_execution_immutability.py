# Feature: third-walking-slice, Property 33: Execution-Record immutability
"""Property 33 — Execution-Record immutability across every Slice 3 table (task 16.3).

**Property 33: Execution-Record immutability**

*For all* Slice 3 Records (Work Assignment, Work Event, Time Entry,
Deliverable Production, Milestone Acceptance, Completion) and produced
Deliverable Revisions finalized at any observation point in the test
session, at every later observation point in the same session the
Record row, every constituent field of the Record, every ``Produces``,
``Addresses``, and ``Relates To`` Relationship sourced from or
targeting that Record, and (for produced Deliverable Revisions) the
``content_digest_sha256``, ``role_marker``,
``originating_work_assignment_id``, and content bytes are
byte-equivalent to their state at first finalization. The
``Audit_Records`` rows for those finalizations are also
byte-equivalent.

**Validates: Requirements 23.9, 24.7, 25.6, 26.4, 27.7, 28.7, 37.3,
37.5, 39.4, 41.4**

Strategy
========

Each Hypothesis case (a) seeds a full Slice 1 → Slice 2 → Slice 3
pipeline that reaches Completion — one Party set, one Project, one
Activity Plan, one Approved Plan Revision, one Plan Approval Record,
one Deliverable Expectation Revision, one Work Assignment Record (with
its two AD-WS-26 Relationship rows), one Work Event Record (with its
AD-WS-26 Relationship row), one Time Entry Record (with its AD-WS-26
Relationship row), one produced Deliverable Resource + one produced
Deliverable Revision (with computed SHA-256 ``content_digest_sha256``
and ``role_marker='generated_output'``), one Deliverable Production
Record (with its three AD-WS-26 Relationship rows), one Milestone
Acceptance Record (with its AD-WS-26 Relationship row), one Completion
Record (with its AD-WS-26 Relationship row), and one consequential
``Audit_Records`` row per finalization — then (b) generates a
Hypothesis-drawn sequence of UPDATE / DELETE attempts against every
Slice 3 table.

Per case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing
   Strategy" — per-case database isolation) carrying every schema
   the pipeline spans:
   :func:`walking_slice.persistence.create_schema` (Slice 1),
   :func:`walking_slice.planning._persistence.create_planning_schema`
   (Slice 2),
   :func:`walking_slice.execution._persistence.create_execution_schema`
   (Slice 3 Execution_Service), and
   :func:`walking_slice.deliverables._persistence.create_deliverable_schema`
   (Slice 3 Deliverable_Repository). The Slice 3 ``CREATE TABLE``
   statements install the AD-WS-27 ``<table>_reject_update`` and
   ``<table>_reject_delete`` triggers on every Slice 3 table.

2. Seeds the full pipeline by direct INSERT. The Plan Revision row
   is seeded with ``lifecycle_state='approved'`` from the first
   INSERT, which is permitted because the AD-WS-19 lifecycle trigger
   fires only on UPDATE, not INSERT (mirrors the pattern in
   ``tests/property/test_property_20_approved_plan_revision_immutability.py``).
   The seed also writes every AD-WS-26 Relationship row alongside
   the relevant Record and appends one consequential
   ``Audit_Records`` row per Slice 3 finalization via
   :class:`AuditLog.append_consequential` so the byte-equivalence
   check covers exactly the rows the production pipeline persists
   for a completed Slice 3 pipeline.

3. Snapshots every Slice 3 Record row, every produced Deliverable
   Resource / Revision row, every ``Relationships`` row sourced
   from or targeting a Slice 3 Record, and every ``Audit_Records``
   row by SELECT-ing every column in stable PK order and storing
   the rows as ``tuple`` objects keyed by table name. The snapshot
   is the byte-equivalence ground truth for the post-attack
   comparison. BLOB columns (``Deliverable_Revisions.content_bytes``)
   are coerced from ``memoryview`` to ``bytes`` so two snapshots
   compare equal regardless of driver version.

4. Iterates the drawn attack list, issuing each UPDATE or DELETE
   against the table named in the attack tuple, and asserts every
   attack raises :class:`sqlalchemy.exc.IntegrityError` — the
   AD-WS-27 trigger fired and the offending statement (and its
   enclosing transaction) was rolled back.

5. Re-snapshots the same rows and asserts byte-for-byte equality
   with the pre-attack snapshot (Property 33's universal quantifier).

Attack alphabet
===============

- ``update`` — ``UPDATE <table> SET <column> = <new_value> WHERE
  <pk> = <pk_value>``. ``column`` is drawn from a per-table allow-list
  that names every persisted column (PK and non-PK) so the test
  exercises the full AD-WS-27 append-only contract on every Slice 3
  table. The ``Deliverable_Revisions`` allow-list explicitly includes
  ``content_bytes``, ``content_digest_sha256``, ``role_marker``, and
  ``originating_work_assignment_id`` because Requirement 26.4 and
  Property 33 enumerate those four fields specifically.
- ``delete`` — ``DELETE FROM <table> WHERE <pk> = <pk_value>``.

The eight Slice 3 tables (``Work_Assignment_Records``,
``Work_Event_Records``, ``Time_Entry_Records``,
``Deliverable_Production_Records``, ``Milestone_Acceptance_Records``,
``Completion_Records``, ``Deliverable_Resources``,
``Deliverable_Revisions``) each carry a ``BEFORE UPDATE`` and a
``BEFORE DELETE`` trigger installed by
:mod:`walking_slice.execution._persistence` /
:mod:`walking_slice.deliverables._persistence`. Both triggers abort
with a descriptive message that SQLAlchemy surfaces as IntegrityError
(AD-WS-27).

The Slice 1 ``Relationships`` and ``Audit_Records`` tables also carry
unconditional AD-WS-4 UPDATE / DELETE rejection triggers covered by
Property 12 (Append-only immutability); this property test snapshots
both tables so the post-attack byte-equivalence diff catches any
collateral side effect a Slice 3 mutation attempt might have produced
on the Relationships and Audit_Records rows sourced from or targeting
a Slice 3 Record.
"""

from __future__ import annotations

import hashlib
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
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# A small set of fixed Identities is sufficient — Property 33 quantifies
# over Slice 3 Records and their relationships, not over Parties / Projects /
# Plan Revisions. The two-Party split is required by Requirement 23.5:
# ``assignment_authority_party_id != assignee_party_id``. The fixed
# authority basis identifier is the FK target the Slice 3 ``authority_basis_id``
# columns reference (the value is opaque per AD-WS-10).
# ---------------------------------------------------------------------------


_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-00000000a001"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-00000000a002"
_ASSIGNMENT_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-00000000a003"
_APPROVING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-00000000a004"

_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-00000000b001"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-00000000b002"

_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-00000000c001"
_SCOPE: Final[str] = "pilot/team-c"
_TS_FIXED: Final[str] = "2026-01-01T00:00:00.000Z"
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Produced Deliverable content. The SHA-256 digest is computed at module
# load time so the seeded ``content_digest_sha256`` value matches the
# ``content_bytes`` byte-equivalent body (Requirement 26.2 / Property 33).
_DELIVERABLE_BYTES: Final[bytes] = (
    b"Property 33: produced Deliverable content snapshot."
)
_DELIVERABLE_DIGEST: Final[str] = hashlib.sha256(
    _DELIVERABLE_BYTES
).hexdigest()


# ---------------------------------------------------------------------------
# Per-table snapshot specifications.
#
# For each immutable row class the test snapshots, name the columns to
# SELECT (in a stable order) and the columns to ORDER BY so the
# byte-equivalence comparison is deterministic. The eight Slice 3 tables,
# the ``Relationships`` table (filtered to rows sourced from or targeting
# Slice 3 Records), and the ``Audit_Records`` table are all snapshotted.
# ---------------------------------------------------------------------------


_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "Work_Assignment_Records": {
        "columns": (
            "work_assignment_id",
            "target_plan_revision_id",
            "assignee_party_id",
            "assignment_authority_party_id",
            "assignment_rationale",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("work_assignment_id",),
    },
    "Work_Event_Records": {
        "columns": (
            "work_event_id",
            "target_work_assignment_id",
            "event_kind",
            "event_note",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("work_event_id",),
    },
    "Time_Entry_Records": {
        "columns": (
            "time_entry_id",
            "target_work_assignment_id",
            "effort_hours",
            "effort_period_start",
            "effort_period_end",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("time_entry_id",),
    },
    "Deliverable_Production_Records": {
        "columns": (
            "deliverable_production_id",
            "source_work_assignment_id",
            "produced_deliverable_id",
            "produced_deliverable_revision_id",
            "target_deliverable_expectation_id",
            "target_deliverable_expectation_revision_id",
            "production_rationale",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("deliverable_production_id",),
    },
    "Milestone_Acceptance_Records": {
        "columns": (
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
        ),
        "order_by": ("milestone_acceptance_id",),
    },
    "Completion_Records": {
        "columns": (
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
        ),
        "order_by": ("completion_id",),
    },
    "Deliverable_Resources": {
        "columns": (
            "deliverable_id",
            "produced_deliverable_name",
            "created_at",
        ),
        "order_by": ("deliverable_id",),
    },
    # Property 33 enumerates four fields on produced Deliverable Revisions
    # specifically: ``content_digest_sha256``, ``role_marker``,
    # ``originating_work_assignment_id``, and content bytes. The snapshot
    # captures every column on the row so the byte-equivalence diff also
    # catches mutations of the surrounding fields (``content_type``,
    # ``authoring_party_id``, ``recorded_at``).
    "Deliverable_Revisions": {
        "columns": (
            "deliverable_revision_id",
            "deliverable_id",
            "content_type",
            "content_bytes",
            "content_digest_sha256",
            "role_marker",
            "originating_work_assignment_id",
            "authoring_party_id",
            "recorded_at",
        ),
        "order_by": ("deliverable_revision_id",),
    },
    # ``Relationships`` carries every AD-WS-26 row written alongside a
    # Slice 3 Record. The snapshot uses ``ORDER BY relationship_id`` for
    # determinism; the post-attack comparison covers every row.
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
    # ``Audit_Records`` carries one consequential row per Slice 3
    # finalization. Snapshotting the full row catches any UPDATE / DELETE
    # collateral the attack loop might have produced.
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
# ``pk_columns`` names the columns the attacker must supply in the WHERE
# clause to target one row. ``update_columns`` is the allow-list the
# Hypothesis attacker draws ``column_to_update`` from. The lists cover
# both PK and non-PK columns so each Hypothesis case exercises every
# persisted attribute the Slice 3 Records expose.
#
# Only the eight Slice 3 tables appear in the attack list — the property
# spec says "UPDATE / DELETE against every Slice 3 table". The
# ``Relationships`` and ``Audit_Records`` tables are append-only via
# Slice 1 AD-WS-4 triggers (Property 12) and are snapshotted (so a
# collateral side effect would surface in the post-attack diff) but not
# attacked directly.
# ---------------------------------------------------------------------------


_ATTACK_COLUMNS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "Work_Assignment_Records": {
        "pk_columns": ("work_assignment_id",),
        "update_columns": (
            "work_assignment_id",
            "target_plan_revision_id",
            "assignee_party_id",
            "assignment_authority_party_id",
            "assignment_rationale",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Work_Event_Records": {
        "pk_columns": ("work_event_id",),
        "update_columns": (
            "work_event_id",
            "target_work_assignment_id",
            "event_kind",
            "event_note",
            "recording_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Time_Entry_Records": {
        "pk_columns": ("time_entry_id",),
        "update_columns": (
            "time_entry_id",
            "target_work_assignment_id",
            "effort_hours",
            "effort_period_start",
            "effort_period_end",
            "recording_party_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Deliverable_Production_Records": {
        "pk_columns": ("deliverable_production_id",),
        "update_columns": (
            "deliverable_production_id",
            "source_work_assignment_id",
            "produced_deliverable_id",
            "produced_deliverable_revision_id",
            "target_deliverable_expectation_id",
            "target_deliverable_expectation_revision_id",
            "production_rationale",
            "recording_party_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Milestone_Acceptance_Records": {
        "pk_columns": ("milestone_acceptance_id",),
        "update_columns": (
            "milestone_acceptance_id",
            "source_deliverable_production_id",
            "produced_deliverable_id",
            "produced_deliverable_revision_id",
            "outcome",
            "rationale",
            "accepting_party_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Completion_Records": {
        "pk_columns": ("completion_id",),
        "update_columns": (
            "completion_id",
            "target_plan_revision_id",
            "target_activity_plan_id",
            "target_project_id",
            "outcome",
            "rationale",
            "source_milestone_acceptance_ids_json",
            "completing_party_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Deliverable_Resources": {
        "pk_columns": ("deliverable_id",),
        "update_columns": (
            "deliverable_id",
            "produced_deliverable_name",
            "created_at",
        ),
    },
    # The four fields Property 33 names explicitly on produced Deliverable
    # Revisions (``content_digest_sha256``, ``role_marker``,
    # ``originating_work_assignment_id``, and ``content_bytes``) are all
    # listed in ``update_columns`` so each Hypothesis case exercises every
    # one of them.
    "Deliverable_Revisions": {
        "pk_columns": ("deliverable_revision_id",),
        "update_columns": (
            "deliverable_revision_id",
            "deliverable_id",
            "content_type",
            "content_bytes",
            "content_digest_sha256",
            "role_marker",
            "originating_work_assignment_id",
            "authoring_party_id",
            "recorded_at",
        ),
    },
}


# Candidate UPDATE values. The bag is intentionally small so the
# (table, kind, column, value) cube fits inside the 100-case budget.
# The five shapes cover string, blank, NULL, a different canonical-form
# identifier, and a BLOB-bytes shape (the last is needed because
# ``Deliverable_Revisions.content_bytes`` is the only BLOB column in the
# Slice 3 set).
_UPDATE_VALUE_BAG: Final[tuple[Any, ...]] = (
    "00000000-0000-7000-8000-00000000ffff",
    "tampered",
    "",
    None,
    b"tampered-blob",
)


_TABLE_NAMES: Final[tuple[str, ...]] = tuple(_ATTACK_COLUMNS.keys())


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, audit rows, and seeded pipelines cannot
# leak between cases (design §"Testing Strategy" — per-case database
# isolation). :class:`tempfile.TemporaryDirectory` owns the per-case
# directory; function-scoped pytest fixtures are unsuitable for per-case
# state because Hypothesis does not reset them between drawn inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a per-case engine with all four schemas installed."""
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
    # ``create_schema`` also adds the additive ``Relationships.semantic_role``
    # and ``Identifier_Registry.resource_kind`` columns via ALTER TABLE
    # (Slice 2 AD-WS-19).
    create_schema(engine)
    # Slice 2: Projects, Activity_Plans, Plan_Revisions, Plan_Approval_Records,
    # Deliverable_Expectations, Deliverable_Expectation_Revisions, …
    create_planning_schema(engine)
    # Slice 3 Execution_Service: Work_Assignment_Records, Work_Event_Records,
    # Time_Entry_Records, Deliverable_Production_Records,
    # Milestone_Acceptance_Records, Completion_Records (with their AD-WS-27
    # append-only triggers).
    create_execution_schema(engine)
    # Slice 3 Deliverable_Repository: Deliverable_Resources,
    # Deliverable_Revisions (with their AD-WS-27 append-only triggers).
    create_deliverable_schema(engine)
    return engine


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string (matches AD-WS-2)."""
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Pipeline seeding.
#
# Each helper inserts one row plus (where applicable) the AD-WS-26
# Relationship row(s) sourced from that Record. The seed pipeline mirrors
# what the production Slice 3 services would persist on a happy-path
# end-to-end run (Work Assignment → Work Event → Time Entry →
# Deliverable Production → Milestone Acceptance → Completion).
# ---------------------------------------------------------------------------


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


def _seed_parties_project_and_plan(engine: Engine) -> dict[str, str]:
    """Insert every Slice 1 / Slice 2 parent row the Slice 3 pipeline needs."""
    plan_revision_id = _new_uuid7()
    plan_approval_id = _new_uuid7()
    deliverable_expectation_id = _new_uuid7()
    deliverable_expectation_revision_id = _new_uuid7()

    with engine.begin() as conn:
        # --- Slice 1 Parties --------------------------------------------
        _seed_party(conn, _REQUESTER_PARTY_ID, "Property 33 Reviewer")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Property 33 Contributor")
        _seed_party(
            conn,
            _ASSIGNMENT_AUTHORITY_ID,
            "Property 33 Assignment Authority",
        )
        _seed_party(conn, _APPROVING_PARTY_ID, "Property 33 Approver")

        # --- Slice 2 Project + Activity Plan ----------------------------
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Mesh Rollout — Phase 3', :party,
                    :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )

        # --- Slice 2 Plan Revision (approved at first INSERT) -----------
        # The AD-WS-19 lifecycle trigger fires only on UPDATE, so an INSERT
        # carrying ``lifecycle_state='approved'`` is permitted.
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
                    :rev, :aid, NULL, 'approved', 'Phase 3 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )

        # --- Slice 2 Plan Approval --------------------------------------
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :pa, :aid, :rev, 'Approve', 'Phase 3 approved.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pa": plan_approval_id,
                "aid": _ACTIVITY_PLAN_ID,
                "rev": plan_revision_id,
                "party": _APPROVING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )

        # --- Slice 2 Deliverable Expectation + Revision -----------------
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": deliverable_expectation_id, "ts": _TS_FIXED},
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
                "rev": deliverable_expectation_revision_id,
                "did": deliverable_expectation_id,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )

    return {
        "plan_revision_id": plan_revision_id,
        "plan_approval_id": plan_approval_id,
        "deliverable_expectation_id": deliverable_expectation_id,
        "deliverable_expectation_revision_id": (
            deliverable_expectation_revision_id
        ),
    }


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

    Mirrors the column list written by the Slice 3 services so the
    snapshot captures the exact AD-WS-26 wiring the production code
    persists.
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


def _seed_full_pipeline(
    engine: Engine,
    *,
    audit_log: AuditLog,
    work_event_kind: str,
    work_event_note: Optional[str],
    effort_hours: str,
    milestone_outcome: Literal["Accept"],
    completion_outcome: str,
) -> dict[str, str]:
    """Seed the full Slice 1 → Slice 2 → Slice 3 pipeline reaching Completion.

    Inserts every row a happy-path end-to-end pipeline would persist:
    one Work Assignment, one Work Event, one Time Entry, one produced
    Deliverable Resource + Revision, one Deliverable Production, one
    Milestone Acceptance, and one Completion — plus every AD-WS-26
    Relationship row and one consequential ``Audit_Records`` row per
    finalization.

    Returns the identifiers the snapshot and attack loops need.
    """
    parents = _seed_parties_project_and_plan(engine)

    # Mint every Slice 3 Identity up front so the helper can issue all
    # INSERT statements inside a single transaction without re-querying
    # for the just-inserted primary keys.
    work_assignment_id = _new_uuid7()
    work_event_id = _new_uuid7()
    time_entry_id = _new_uuid7()
    deliverable_id = _new_uuid7()
    deliverable_revision_id = _new_uuid7()
    deliverable_production_id = _new_uuid7()
    milestone_acceptance_id = _new_uuid7()
    completion_id = _new_uuid7()

    rel_wa_addresses_pr = _new_uuid7()
    rel_wa_assignee = _new_uuid7()
    rel_we_work_event = _new_uuid7()
    rel_te_time_entry = _new_uuid7()
    rel_dp_produces = _new_uuid7()
    rel_dp_addresses_der = _new_uuid7()
    rel_dp_prod_source = _new_uuid7()
    rel_ma_addresses_dr = _new_uuid7()
    rel_cp_addresses_pr = _new_uuid7()

    correlation_id = _new_uuid7()

    with engine.begin() as conn:
        # --- Work Assignment Record + AD-WS-26 rows 1 & 2 --------------
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
                "prev": parents["plan_revision_id"],
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNMENT_AUTHORITY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 1: Work Assignment Record -> Plan Revision via
        # Addresses with semantic_role = NULL.
        _insert_relationship(
            conn,
            relationship_id=rel_wa_addresses_pr,
            relationship_type="Addresses",
            source_kind="work_assignment_record",
            source_id=work_assignment_id,
            source_revision_id=None,
            target_kind="plan_revision",
            target_id=parents["plan_revision_id"],
            target_revision_id=None,
            semantic_role=None,
            authoring_party_id=_ASSIGNMENT_AUTHORITY_ID,
        )
        # AD-WS-26 row 2: Work Assignment Record -> assignee Party via
        # Relates To with semantic_role = 'assignee'.
        _insert_relationship(
            conn,
            relationship_id=rel_wa_assignee,
            relationship_type="Relates To",
            source_kind="work_assignment_record",
            source_id=work_assignment_id,
            source_revision_id=None,
            target_kind="party",
            target_id=_CONTRIBUTOR_PARTY_ID,
            target_revision_id=None,
            semantic_role="assignee",
            authoring_party_id=_ASSIGNMENT_AUTHORITY_ID,
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_ASSIGNMENT_AUTHORITY_ID,
            action_type="create.work_assignment",
            target_id=work_assignment_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

        # --- Work Event Record + AD-WS-26 row 3 ------------------------
        conn.execute(
            text(
                """
                INSERT INTO Work_Event_Records (
                    work_event_id, target_work_assignment_id,
                    event_kind, event_note, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :weid, :wid, :ek, :note, :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "weid": work_event_id,
                "wid": work_assignment_id,
                "ek": work_event_kind,
                "note": work_event_note,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=rel_we_work_event,
            relationship_type="Relates To",
            source_kind="work_event_record",
            source_id=work_event_id,
            source_revision_id=None,
            target_kind="work_assignment_record",
            target_id=work_assignment_id,
            target_revision_id=None,
            semantic_role="work_event",
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_CONTRIBUTOR_PARTY_ID,
            action_type="create.work_event",
            target_id=work_event_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

        # --- Time Entry Record + AD-WS-26 row 4 ------------------------
        conn.execute(
            text(
                """
                INSERT INTO Time_Entry_Records (
                    time_entry_id, target_work_assignment_id,
                    effort_hours, effort_period_start,
                    effort_period_end, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :tid, :wid, :hrs, :start, :end, :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "tid": time_entry_id,
                "wid": work_assignment_id,
                "hrs": effort_hours,
                "start": _TS_FIXED,
                "end": _TS_FIXED,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=rel_te_time_entry,
            relationship_type="Relates To",
            source_kind="time_entry_record",
            source_id=time_entry_id,
            source_revision_id=None,
            target_kind="work_assignment_record",
            target_id=work_assignment_id,
            target_revision_id=None,
            semantic_role="time_entry",
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_CONTRIBUTOR_PARTY_ID,
            action_type="create.time_entry",
            target_id=time_entry_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

        # --- Deliverable Resource + Revision ---------------------------
        # Requirement 26.2 / Property 33 enumerates the four fields on
        # produced Deliverable Revisions: ``content_digest_sha256``,
        # ``role_marker``, ``originating_work_assignment_id``, and
        # ``content_bytes`` — all written explicitly here.
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Mesh Operations Runbook', :ts)
                """
            ),
            {"did": deliverable_id, "ts": _TS_FIXED},
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
                "bytes": _DELIVERABLE_BYTES,
                "digest": _DELIVERABLE_DIGEST,
                "wa": work_assignment_id,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_CONTRIBUTOR_PARTY_ID,
            action_type="create.produced_deliverable",
            target_id=deliverable_revision_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

        # --- Deliverable Production Record + AD-WS-26 rows 5, 6, 7 ----
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
                    'Produced runbook for milestone one.', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": deliverable_production_id,
                "wa": work_assignment_id,
                "did": deliverable_id,
                "rev": deliverable_revision_id,
                "exp_did": parents["deliverable_expectation_id"],
                "exp_rev": parents["deliverable_expectation_revision_id"],
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=rel_dp_produces,
            relationship_type="Produces",
            source_kind="deliverable_production_record",
            source_id=deliverable_production_id,
            source_revision_id=None,
            target_kind="deliverable_revision",
            target_id=deliverable_id,
            target_revision_id=deliverable_revision_id,
            semantic_role=None,
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        _insert_relationship(
            conn,
            relationship_id=rel_dp_addresses_der,
            relationship_type="Addresses",
            source_kind="deliverable_production_record",
            source_id=deliverable_production_id,
            source_revision_id=None,
            target_kind="deliverable_expectation_revision",
            target_id=parents["deliverable_expectation_id"],
            target_revision_id=parents["deliverable_expectation_revision_id"],
            semantic_role=None,
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        _insert_relationship(
            conn,
            relationship_id=rel_dp_prod_source,
            relationship_type="Relates To",
            source_kind="deliverable_production_record",
            source_id=deliverable_production_id,
            source_revision_id=None,
            target_kind="work_assignment_record",
            target_id=work_assignment_id,
            target_revision_id=None,
            semantic_role="production_source",
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_CONTRIBUTOR_PARTY_ID,
            action_type="create.deliverable_production",
            target_id=deliverable_production_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

        # --- Milestone Acceptance Record + AD-WS-26 row 8 -------------
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
                "mid": milestone_acceptance_id,
                "pid": deliverable_production_id,
                "did": deliverable_id,
                "rev": deliverable_revision_id,
                "exp_did": parents["deliverable_expectation_id"],
                "exp_rev": parents["deliverable_expectation_revision_id"],
                "outcome": milestone_outcome,
                "party": _APPROVING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=rel_ma_addresses_dr,
            relationship_type="Addresses",
            source_kind="milestone_acceptance_record",
            source_id=milestone_acceptance_id,
            source_revision_id=None,
            target_kind="deliverable_revision",
            target_id=deliverable_id,
            target_revision_id=deliverable_revision_id,
            semantic_role=None,
            authoring_party_id=_APPROVING_PARTY_ID,
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_APPROVING_PARTY_ID,
            action_type="create.milestone_acceptance",
            target_id=milestone_acceptance_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

        # --- Completion Record + AD-WS-26 row 9 -----------------------
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
                    :cid, :prev, :aid, :pid, :outcome,
                    'All planned work completed.', '[]', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": completion_id,
                "prev": parents["plan_revision_id"],
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "outcome": completion_outcome,
                "party": _APPROVING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        _insert_relationship(
            conn,
            relationship_id=rel_cp_addresses_pr,
            relationship_type="Addresses",
            source_kind="completion_record",
            source_id=completion_id,
            source_revision_id=None,
            target_kind="plan_revision",
            target_id=parents["plan_revision_id"],
            target_revision_id=None,
            semantic_role=None,
            authoring_party_id=_APPROVING_PARTY_ID,
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_APPROVING_PARTY_ID,
            action_type="create.completion",
            target_id=completion_id,
            correlation_id=correlation_id,
            recorded_time=_FIXED_NOW,
        )

    return {
        "plan_revision_id": parents["plan_revision_id"],
        "work_assignment_id": work_assignment_id,
        "work_event_id": work_event_id,
        "time_entry_id": time_entry_id,
        "deliverable_id": deliverable_id,
        "deliverable_revision_id": deliverable_revision_id,
        "deliverable_production_id": deliverable_production_id,
        "milestone_acceptance_id": milestone_acceptance_id,
        "completion_id": completion_id,
    }


# ---------------------------------------------------------------------------
# Snapshot helper.
#
# Reads every row of every table in :data:`_TABLE_SPECS` in stable PK order
# and returns the rows as a hashable bundle so the byte-equivalence
# comparison reduces to one ``==`` per table. Storing the full row tuple
# (rather than a hex digest) keeps a failing assertion's diff informative
# — Hypothesis prints the differing tuples directly. ``memoryview`` BLOBs
# are coerced to ``bytes`` so two snapshots taken from the same row compare
# equal regardless of driver version.
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


# Work Event Kind / note / effort-hours / Completion outcome dimensions
# come from the per-table CHECK constraints in
# :mod:`walking_slice.execution._persistence`. Drawing from the enumerated
# sets covers the realistic shape space without producing rows the
# AD-WS-27 trigger would never see. Reject-outcome Milestones are
# excluded because Property 33 quantifies over *full* pipelines that
# reach Completion, and a Reject-outcome Milestone Acceptance does not
# unlock Completion per Requirement 29.1.
_work_event_kind_strategy = st.sampled_from(
    ("started", "progress_note", "paused", "resumed", "deliverable_drafted")
)
_work_event_note_strategy = st.one_of(
    st.none(),
    st.text(min_size=0, max_size=200),
)
# ``effort_hours`` is stored as TEXT under a GLOB CHECK; the strategy
# draws values that satisfy both the regex and the 0.00..24.00 numeric
# range. The bag covers single-digit, decimal-fractional, and two-digit
# shapes the production schema accepts.
_effort_hours_strategy = st.sampled_from(
    ("0", "1", "0.5", "1.50", "23", "24", "24.00", "0.00")
)
_completion_outcome_strategy = st.sampled_from(
    ("Completed", "Completed_With_Reservation")
)


@st.composite
def _pipeline_strategy(draw) -> dict[str, Any]:
    """Draw one full-pipeline configuration."""
    return {
        "work_event_kind": draw(_work_event_kind_strategy),
        "work_event_note": draw(_work_event_note_strategy),
        "effort_hours": draw(_effort_hours_strategy),
        "milestone_outcome": "Accept",
        "completion_outcome": draw(_completion_outcome_strategy),
    }


@st.composite
def _attack_strategy(draw) -> dict[str, Any]:
    """Draw one (table, kind, column?, new_value) attack tuple."""
    table = draw(st.sampled_from(_TABLE_NAMES))
    kind: Literal["update", "delete"] = draw(
        st.sampled_from(("update", "delete"))
    )
    column = draw(
        st.sampled_from(_ATTACK_COLUMNS[table]["update_columns"])
    )
    new_value = draw(st.sampled_from(_UPDATE_VALUE_BAG))
    return {
        "table": table,
        "kind": kind,
        "column": column,
        "new_value": new_value,
    }


# Each scenario is 1..15 attacks; ``min_size=1`` guarantees at least one
# rejection attempt per case (a case with zero attacks would leave the
# rejection assertion vacuously satisfied).
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
    """Execute one attack against the engine and assert it was rejected.

    Each attack runs inside a fresh ``engine.begin()`` block so a
    successful (incorrectly permitted) statement would commit and
    immediately break the byte-equivalence post-condition; the fresh
    transaction per attack also matches the "any two observation points"
    wording in Property 33.
    """
    table = attack["table"]
    kind = attack["kind"]
    pk_values = _first_pk(engine, table=table)
    if pk_values is None:
        # The seed pipeline guarantees at least one row in every Slice 3
        # table, so a ``None`` here would indicate a seed regression.
        # Fail loudly rather than silently skipping the attack.
        raise AssertionError(
            f"Seed regression: no row found in {table!r} for Property 33 "
            f"attack {attack!r}. The pipeline seed must insert at least "
            f"one row per Slice 3 table."
        )

    where_clause = " AND ".join(
        f"{col} = :pk_{col}" for col in pk_values.keys()
    )
    params: dict[str, Any] = {
        f"pk_{col}": val for col, val in pk_values.items()
    }

    if kind == "delete":
        statement = f"DELETE FROM {table} WHERE {where_clause}"
    else:
        column = attack["column"]
        params["new_value"] = attack["new_value"]
        statement = (
            f"UPDATE {table} SET {column} = :new_value WHERE {where_clause}"
        )

    raised = False
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params)
    except IntegrityError:
        raised = True
    except Exception as exc:  # pragma: no cover - defensive
        # Any other exception type is a regression: the AD-WS-27
        # triggers ABORT, which SQLAlchemy surfaces as IntegrityError.
        # A different exception class would silently let the
        # byte-equivalence assertion still pass while hiding a trigger
        # regression.
        raise AssertionError(
            f"Attack {attack!r} raised {type(exc).__name__} instead of "
            f"sqlalchemy.exc.IntegrityError; the AD-WS-27 trigger "
            f"contract regressed."
        ) from exc

    assert raised, (
        f"Attack {attack!r} was NOT rejected — the AD-WS-27 immutability "
        f"trigger on {table!r} failed to fire. Property 33 / Requirements "
        f"23.9, 24.7, 25.6, 26.4, 27.7, 28.7, 41.4 require every "
        f"UPDATE/DELETE against a Slice 3 Record (and its constituent "
        f"Relationship and Audit_Records rows) to raise IntegrityError."
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 33: Execution-Record immutability
@given(pipeline=_pipeline_strategy(), scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup builds a full end-to-end Slice 1 + Slice 2 + Slice 3
    # pipeline (≈12 INSERTs plus 7 audit appends) and runs the attack
    # loop and the post-attack snapshot diff. The data-generation
    # health check is suppressed because the per-case work is heavier
    # than a pure in-memory property test by design.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_execution_record_immutability(
    pipeline: dict[str, Any], scenario: list[dict[str, Any]]
) -> None:
    """Every UPDATE/DELETE against a Slice 3 Record (and its constituent
    Relationship and Audit_Records rows) is rejected; every Slice 3
    Record row, every produced Deliverable Resource / Revision row,
    every Relationship row sourced from or targeting a Slice 3 Record,
    and every consequential Audit_Records row remain byte-equivalent
    across every later observation point."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop33_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # Fresh clock + audit log per case so the per-case audit
            # appends share one fixed recorded_at and the ``append_sequence``
            # ordering is deterministic across shrinks.
            clock = FixedClock(_FIXED_NOW)
            audit_log = AuditLog(clock)

            # --- Phase 1: seed the full pipeline -------------------
            seed_ids = _seed_full_pipeline(
                engine,
                audit_log=audit_log,
                work_event_kind=pipeline["work_event_kind"],
                work_event_note=pipeline["work_event_note"],
                effort_hours=pipeline["effort_hours"],
                milestone_outcome=pipeline["milestone_outcome"],
                completion_outcome=pipeline["completion_outcome"],
            )

            # --- Phase 2: snapshot ---------------------------------
            pre_snapshot = _snapshot(engine)

            # Sanity-check the seeded shape: each Slice 3 table has
            # exactly one row, the Relationships table has the nine
            # AD-WS-26 rows, and the Audit_Records table has the seven
            # consequential rows the seed appended.
            for table_name in _TABLE_NAMES:
                assert len(pre_snapshot[table_name]) == 1, (
                    f"Seed regression: {table_name!r} has "
                    f"{len(pre_snapshot[table_name])} rows; expected 1."
                )
            assert len(pre_snapshot["Relationships"]) == 9, (
                f"Seed regression: Relationships has "
                f"{len(pre_snapshot['Relationships'])} rows; expected 9 "
                f"AD-WS-26 rows."
            )
            assert len(pre_snapshot["Audit_Records"]) == 7, (
                f"Seed regression: Audit_Records has "
                f"{len(pre_snapshot['Audit_Records'])} rows; expected 7 "
                f"consequential rows (one per Slice 3 finalization)."
            )

            # --- Phase 3: attack loop ------------------------------
            for attack in scenario:
                _apply_attack(engine, attack)

            # --- Phase 4: re-snapshot and byte-equivalence diff ---
            post_snapshot = _snapshot(engine)
            for table_name in _TABLE_SPECS.keys():
                assert post_snapshot[table_name] == pre_snapshot[table_name], (
                    f"Byte-equivalence violated on {table_name!r}: "
                    f"pre={pre_snapshot[table_name]!r}, "
                    f"post={post_snapshot[table_name]!r}. Property 33 / "
                    f"Requirements 23.9, 24.7, 25.6, 26.4, 27.7, 28.7, "
                    f"41.4 require every Slice 3 Record row (and its "
                    f"constituent Relationship and Audit_Records rows) "
                    f"to remain byte-equivalent across the attack "
                    f"sequence."
                )

            # --- Phase 5: spot-check the four Property-33-named
            # fields on Deliverable_Revisions ----------------------
            # Requirement 26.4 and Property 33 enumerate the four
            # fields on produced Deliverable Revisions specifically.
            # Confirm each one is still at its seeded value after the
            # attack sequence so a regression that incorrectly permits
            # any of them to mutate surfaces here with a focused
            # diagnostic rather than only in the broader snapshot
            # diff.
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        """
                        SELECT content_bytes, content_digest_sha256,
                               role_marker, originating_work_assignment_id
                          FROM Deliverable_Revisions
                         WHERE deliverable_revision_id = :rev
                        """
                    ),
                    {"rev": seed_ids["deliverable_revision_id"]},
                ).mappings().one()
            content_bytes = row["content_bytes"]
            if isinstance(content_bytes, memoryview):
                content_bytes = bytes(content_bytes)
            assert content_bytes == _DELIVERABLE_BYTES, (
                "Deliverable_Revisions.content_bytes regressed from the "
                "seeded value; the AD-WS-27 trigger contract is broken."
            )
            assert row["content_digest_sha256"] == _DELIVERABLE_DIGEST, (
                "Deliverable_Revisions.content_digest_sha256 regressed "
                "from the seeded value; the AD-WS-27 trigger contract "
                "is broken."
            )
            assert row["role_marker"] == "generated_output", (
                f"Deliverable_Revisions.role_marker regressed to "
                f"{row['role_marker']!r}; the AD-WS-27 trigger contract "
                f"is broken."
            )
            assert row["originating_work_assignment_id"] == (
                seed_ids["work_assignment_id"]
            ), (
                "Deliverable_Revisions.originating_work_assignment_id "
                "regressed from the seeded value; the AD-WS-27 trigger "
                "contract is broken."
            )
        finally:
            engine.dispose()
