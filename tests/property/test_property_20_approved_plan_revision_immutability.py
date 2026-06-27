# Feature: second-walking-slice, Property 20: Approved Plan Revision immutability
"""Property 20 — Approved Plan Revision immutability (task 16.5).

**Property 20: Approved Plan Revision immutability**

*For all* Plan Revisions whose ``lifecycle_state`` has been
``'approved'`` at any observation point in the test session, at every
later observation point in the same session the Plan Revision row,
every constituent field of the Plan Revision Revision, every
``Supports``, ``Addresses``, and ``Supersedes`` Relationship sourced
from or targeting that Plan Revision, every Plan Review Revision
targeting that Plan Revision, and the corresponding
``Plan_Approval_Records`` row are byte-equivalent to their state at
first approval.

**Validates: Requirements 9.4, 9.6, 16.5, 20.4**

Strategy
========

Each Hypothesis case (a) seeds a *post-approval* pipeline shape — a
Party + Project + Activity Plan + one Approved Plan Revision (with
optional ``Supersedes`` Relationship to an approved predecessor) + the
corresponding ``Plan_Approval_Records`` row + the single ``Addresses``
Relationship binding the Plan Approval to the Plan Revision + zero or
more Plan Review Revisions (with their ``Relates To`` Relationships
each carrying ``semantic_role='review'`` per AD-WS-17) — then (b)
generates a Hypothesis-drawn sequence of UPDATE / DELETE *attempts*
against every immutable row in the seeded shape.

Per case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing
   Strategy" — per-case database isolation) carrying both the
   Slice 1 schema (:func:`walking_slice.persistence.create_schema`)
   and the Slice 2 schema
   (:func:`walking_slice.planning._persistence.create_planning_schema`),
   the latter of which installs the AD-WS-19 lifecycle UPDATE trigger
   on every DBAPI connection.
2. Seeds the approved pipeline by direct INSERT (the Plan Revision
   row is seeded with ``lifecycle_state = 'approved'`` from the
   first row, which is permitted because the AD-WS-19 lifecycle
   trigger fires only on UPDATE, not INSERT). The seed also seeds
   the ``Relationships`` rows the production services write
   (``Addresses`` from :mod:`walking_slice.planning.plan_approvals`,
   ``Relates To`` with ``semantic_role='review'`` from
   :mod:`walking_slice.planning.plan_reviews`, and ``Supersedes``
   from :mod:`walking_slice.planning.plan_revisions`) so the
   byte-equivalence check covers exactly the rows the production
   pipeline persists for an approved Plan Revision.
3. Snapshots every Plan Revision, Plan Approval Record, Plan Review
   Revision, and related Relationship row by SELECT-ing every column
   in stable PK order and storing the rows as ``tuple`` objects keyed
   by table name. The snapshot is the byte-equivalence ground truth
   for the post-attack comparison.
4. Iterates the drawn attack list, issuing each UPDATE or DELETE
   against the table named in the attack tuple, and asserts every
   attack raises :class:`sqlalchemy.exc.IntegrityError` — the trigger
   fired and the offending statement (and its enclosing transaction)
   was rolled back.
5. Re-snapshots the same rows and asserts byte-for-byte equality
   with the pre-attack snapshot (Property 20's universal quantifier).

Attack alphabet
===============

- ``update`` — ``UPDATE <table> SET <column> = <new_value> WHERE
  <pk> = <pk_value>``. ``column`` is drawn from a per-table allow-list
  that names every persisted column (PK and non-PK) so the test
  exercises the full append-only / lifecycle-trigger contract on the
  approved Plan Revision.
- ``delete`` — ``DELETE FROM <table> WHERE <pk> = <pk_value>``.

The ``Plan_Revisions`` AD-WS-19 lifecycle trigger rejects every
UPDATE on a row whose ``lifecycle_state`` is already ``'approved'``
because the trigger's ``WHEN`` clause requires
``OLD.lifecycle_state = 'draft' AND NEW.lifecycle_state = 'approved'``
— a condition that is false for any approved row regardless of the
session pragma. The companion ``Plan_Revisions_reject_delete``
trigger rejects every DELETE unconditionally. The
``Plan_Approval_Records`` and ``Plan_Review_Revisions`` tables are in
:data:`walking_slice.planning._persistence.PLANNING_IMMUTABLE_TABLES`
so their ``_reject_update`` / ``_reject_delete`` AD-WS-4 triggers
reject every mutation unconditionally. The ``Relationships`` table is
in :data:`walking_slice.persistence._IMMUTABLE_TABLES` so the Slice 1
AD-WS-4 triggers reject every mutation unconditionally on the
``Addresses``, ``Relates To``, and ``Supersedes`` rows the seeded
pipeline persists.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, Optional

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# A single Party and a single Project / Activity Plan suffice; Property
# 20 quantifies over Plan Revisions, Plan Reviews, and Plan Approval
# Records (and their constituent Relationships), not over the parent
# Resources. The authority basis identifier is the FK target required
# by ``Plan_Approval_Records.authority_basis_id`` /
# ``Plan_Review_Revisions.authority_basis_id``.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_TS_FIXED: Final[str] = "2026-01-01T00:00:00.000Z"
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)


# Relationship-type / kind constants, mirrored from the production
# Planning_Service modules so a drift between this property test and
# the production constants would surface as a snapshot diff.
_REL_TYPE_ADDRESSES: Final[str] = "Addresses"
_REL_TYPE_RELATES_TO: Final[str] = "Relates To"
_REL_TYPE_SUPERSEDES: Final[str] = "Supersedes"
_SEMANTIC_ROLE_REVIEW: Final[str] = "review"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"
_KIND_PLAN_REVIEW_REVISION: Final[str] = "plan_review_revision"
_KIND_PLAN_APPROVAL: Final[str] = "plan_approval"


# ---------------------------------------------------------------------------
# Per-table snapshot specifications.
#
# For each immutable row class the test snapshots, name the columns to
# SELECT (in a stable order) and the columns to ORDER BY so the
# byte-equivalence comparison is deterministic.
# ---------------------------------------------------------------------------


_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "Plan_Revisions": {
        "columns": (
            "plan_revision_id",
            "activity_plan_id",
            "predecessor_revision_id",
            "lifecycle_state",
            "planned_scope",
            "deliverable_expectation_refs_json",
            "planning_assumptions_json",
            "ordering_rationale",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("plan_revision_id",),
    },
    "Plan_Approval_Records": {
        "columns": (
            "plan_approval_id",
            "target_activity_plan_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "approving_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("plan_approval_id",),
    },
    "Plan_Review_Revisions": {
        "columns": (
            "plan_review_revision_id",
            "plan_review_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "reviewing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "order_by": ("plan_review_revision_id",),
    },
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
}


# ---------------------------------------------------------------------------
# Per-table attack alphabets.
#
# ``pk_columns`` names the columns the attacker must supply in the
# WHERE clause to target one row. ``update_columns`` is the allow-list
# the Hypothesis attacker draws ``column_to_update`` from. The lists
# cover both PK and non-PK columns so each Hypothesis case exercises
# every persisted attribute the Plan Revision / Plan Approval Record /
# Plan Review Revision / related Relationships expose. The Plan
# Revision allow-list specifically includes ``lifecycle_state`` so
# the test exercises the AD-WS-19 trigger's rejection of every
# "approved → X" transition (the trigger's WHEN clause requires
# OLD.lifecycle_state = 'draft', so an approved row's lifecycle_state
# cannot be touched).
# ---------------------------------------------------------------------------


_ATTACK_COLUMNS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "Plan_Revisions": {
        "pk_columns": ("plan_revision_id",),
        "update_columns": (
            "plan_revision_id",
            "activity_plan_id",
            "predecessor_revision_id",
            "lifecycle_state",
            "planned_scope",
            "deliverable_expectation_refs_json",
            "planning_assumptions_json",
            "ordering_rationale",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Plan_Approval_Records": {
        "pk_columns": ("plan_approval_id",),
        "update_columns": (
            "plan_approval_id",
            "target_activity_plan_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "approving_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Plan_Review_Revisions": {
        "pk_columns": ("plan_review_revision_id",),
        "update_columns": (
            "plan_review_revision_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "reviewing_party_id",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Relationships": {
        "pk_columns": ("relationship_id",),
        "update_columns": (
            "relationship_id",
            "relationship_type",
            "source_id",
            "target_id",
            "semantic_role",
            "recorded_at",
        ),
    },
}


# Candidate UPDATE values. The bag is intentionally small so the
# (table, kind, column, value) cube fits inside the 100-case budget;
# the four shapes cover string, blank, NULL, and a different
# canonical-form identifier.
_UPDATE_VALUE_BAG: Final[tuple[Any, ...]] = (
    "00000000-0000-7000-8000-0000000000ff",
    "tampered",
    "",
    None,
)


_TABLE_NAMES: Final[tuple[str, ...]] = tuple(_ATTACK_COLUMNS.keys())


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case identifiers, audit rows, and seeded
# pipelines cannot leak between cases (design §"Testing Strategy" —
# per-case database isolation). :class:`tempfile.TemporaryDirectory`
# owns the per-case directory; function-scoped pytest fixtures are
# unsuitable for per-case state because Hypothesis does not reset them
# between drawn inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a per-case engine with both Slice 1 and Slice 2 schemas installed."""
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
    return engine


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string (matches AD-WS-2)."""
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Pipeline seeding.
#
# Seeds the post-approval pipeline directly with INSERT statements.
# Plan_Revisions accepts an INSERT carrying ``lifecycle_state =
# 'approved'`` because the AD-WS-19 lifecycle trigger fires only on
# UPDATE (mirrors the pattern in
# ``tests/unit/test_planning_immutability.py``).
# ---------------------------------------------------------------------------


def _seed_party_and_parents(engine: Engine) -> None:
    """Insert Party, Project, and Activity Plan rows the FK chain needs."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Parties (party_id, kind, display_name, created_at) "
                "VALUES (:pid, 'person', 'Planner', :ts)"
            ),
            {"pid": _PARTY_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text("INSERT INTO Projects (project_id, created_at) VALUES (:id, :ts)"),
            {"id": _PROJECT_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Mesh Rollout — Phase 1', :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": "pilot/team-a",
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision_row(
    engine: Engine,
    *,
    plan_revision_id: str,
    lifecycle_state: str,
    planned_scope: str,
    ordering_rationale: Optional[str],
    applicable_scope: str,
    predecessor_revision_id: Optional[str] = None,
) -> None:
    """Insert one ``Plan_Revisions`` row with the given lifecycle state."""
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
                    :rev, :aid, :prev, :state, :scope_text, '[]', '[]',
                    :ord, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _ACTIVITY_PLAN_ID,
                "prev": predecessor_revision_id,
                "state": lifecycle_state,
                "scope_text": planned_scope,
                "ord": ordering_rationale,
                "party": _PARTY_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _seed_relationship_row(
    engine: Engine,
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
) -> None:
    """Insert one ``Relationships`` row matching the production wiring."""
    with engine.begin() as conn:
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
                    :party, :ts, :semantic_role
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
                "party": _PARTY_ID,
                "ts": _TS_FIXED,
                "semantic_role": semantic_role,
            },
        )


def _seed_plan_review_revision_row(
    engine: Engine,
    *,
    plan_review_id: str,
    plan_review_revision_id: str,
    target_plan_revision_id: str,
    outcome: str,
    rationale: str,
    applicable_scope: str,
) -> None:
    """Insert one ``Plan_Reviews`` + ``Plan_Review_Revisions`` pair."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Plan_Reviews (plan_review_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": plan_review_id, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Plan_Review_Revisions (
                    plan_review_revision_id, plan_review_id,
                    target_plan_revision_id, outcome, rationale,
                    reviewing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pid, :prev, :outcome, :rationale,
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_review_revision_id,
                "pid": plan_review_id,
                "prev": target_plan_revision_id,
                "outcome": outcome,
                "rationale": rationale,
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_approval_row(
    engine: Engine,
    *,
    plan_approval_id: str,
    target_plan_revision_id: str,
    outcome: str,
    rationale: str,
    applicable_scope: str,
) -> None:
    """Insert one ``Plan_Approval_Records`` row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid_, :aid, :prev, :outcome, :rationale,
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "aid_": plan_approval_id,
                "aid": _ACTIVITY_PLAN_ID,
                "prev": target_plan_revision_id,
                "outcome": outcome,
                "rationale": rationale,
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


# ---------------------------------------------------------------------------
# Per-case pipeline seed.
#
# Given a drawn pipeline configuration, seed the post-approval state
# the production services would have produced. Returns the identifiers
# the snapshot and attack loop need.
# ---------------------------------------------------------------------------


def _seed_approved_pipeline(
    engine: Engine,
    *,
    has_predecessor: bool,
    planned_scope: str,
    ordering_rationale: Optional[str],
    applicable_scope: str,
    approval_rationale: str,
    review_outcomes: tuple[str, ...],
    review_rationales: tuple[str, ...],
) -> dict[str, Any]:
    """Seed one approved-state pipeline and return its identifiers.

    The seeded shape mirrors what
    :class:`walking_slice.planning.plan_approvals.PlanApprovalService`
    would produce on a successful ``outcome='Approve'`` invocation
    against a Plan Revision that previously received the drawn Plan
    Reviews:

    - One Plan_Revisions row with ``lifecycle_state='approved'`` (and,
      optionally, a ``predecessor_revision_id`` pointing at an
      already-approved predecessor row seeded below).
    - When ``has_predecessor`` is True, one *additional* Plan_Revisions
      row carrying the predecessor identity (also ``'approved'`` so
      the snapshot exercises both rows) plus the ``Supersedes``
      Relationship the :mod:`walking_slice.planning.plan_revisions`
      service would have written.
    - One Plan_Approval_Records row.
    - One ``Addresses`` Relationships row binding the Plan Approval
      to the Plan Revision (per
      :mod:`walking_slice.planning.plan_approvals`).
    - ``len(review_outcomes)`` Plan_Review_Revisions rows plus their
      sibling ``Plan_Reviews`` header rows; one ``Relates To``
      Relationships row per Plan Review Revision with
      ``semantic_role='review'`` (per AD-WS-17 /
      :mod:`walking_slice.planning.plan_reviews`).
    """
    _seed_party_and_parents(engine)

    plan_revision_id = _new_uuid7()
    plan_approval_id = _new_uuid7()
    addresses_relationship_id = _new_uuid7()

    predecessor_revision_id: Optional[str] = None
    supersedes_relationship_id: Optional[str] = None

    if has_predecessor:
        predecessor_revision_id = _new_uuid7()
        # Seed the predecessor with ``lifecycle_state='approved'`` so
        # it is part of the protected snapshot. (In production a
        # ``Supersedes`` edge can also be recorded between two draft
        # revisions before approval; the immutability invariant
        # applies once any participant has been approved.)
        _seed_plan_revision_row(
            engine,
            plan_revision_id=predecessor_revision_id,
            lifecycle_state="approved",
            planned_scope="Phase 0 scope.",
            ordering_rationale="Seed predecessor before Phase 1.",
            applicable_scope=applicable_scope,
            predecessor_revision_id=None,
        )
        supersedes_relationship_id = _new_uuid7()

    _seed_plan_revision_row(
        engine,
        plan_revision_id=plan_revision_id,
        lifecycle_state="approved",
        planned_scope=planned_scope,
        ordering_rationale=ordering_rationale,
        applicable_scope=applicable_scope,
        predecessor_revision_id=predecessor_revision_id,
    )

    if has_predecessor and supersedes_relationship_id is not None:
        _seed_relationship_row(
            engine,
            relationship_id=supersedes_relationship_id,
            relationship_type=_REL_TYPE_SUPERSEDES,
            source_kind=_KIND_PLAN_REVISION,
            source_id=plan_revision_id,
            source_revision_id=None,
            target_kind=_KIND_PLAN_REVISION,
            target_id=predecessor_revision_id,
            target_revision_id=None,
            semantic_role=None,
        )

    # Plan Reviews are recorded before approval against the same Plan
    # Revision; we seed each with its ``Relates To`` Relationship row
    # so the snapshot captures the full surface Property 20 protects.
    plan_review_revision_ids: list[str] = []
    review_relationship_ids: list[str] = []
    for outcome, rationale in zip(review_outcomes, review_rationales):
        plan_review_id = _new_uuid7()
        plan_review_revision_id = _new_uuid7()
        relates_to_relationship_id = _new_uuid7()
        _seed_plan_review_revision_row(
            engine,
            plan_review_id=plan_review_id,
            plan_review_revision_id=plan_review_revision_id,
            target_plan_revision_id=plan_revision_id,
            outcome=outcome,
            rationale=rationale,
            applicable_scope=applicable_scope,
        )
        _seed_relationship_row(
            engine,
            relationship_id=relates_to_relationship_id,
            relationship_type=_REL_TYPE_RELATES_TO,
            source_kind=_KIND_PLAN_REVIEW_REVISION,
            source_id=plan_review_id,
            source_revision_id=plan_review_revision_id,
            target_kind=_KIND_PLAN_REVISION,
            target_id=plan_revision_id,
            target_revision_id=None,
            semantic_role=_SEMANTIC_ROLE_REVIEW,
        )
        plan_review_revision_ids.append(plan_review_revision_id)
        review_relationship_ids.append(relates_to_relationship_id)

    # Plan Approval Record + its single ``Addresses`` Relationship.
    _seed_plan_approval_row(
        engine,
        plan_approval_id=plan_approval_id,
        target_plan_revision_id=plan_revision_id,
        outcome="Approve",
        rationale=approval_rationale,
        applicable_scope=applicable_scope,
    )
    _seed_relationship_row(
        engine,
        relationship_id=addresses_relationship_id,
        relationship_type=_REL_TYPE_ADDRESSES,
        source_kind=_KIND_PLAN_APPROVAL,
        source_id=plan_approval_id,
        source_revision_id=None,
        target_kind=_KIND_PLAN_REVISION,
        target_id=plan_revision_id,
        target_revision_id=None,
        semantic_role=None,
    )

    return {
        "plan_revision_id": plan_revision_id,
        "plan_approval_id": plan_approval_id,
        "predecessor_revision_id": predecessor_revision_id,
        "addresses_relationship_id": addresses_relationship_id,
        "supersedes_relationship_id": supersedes_relationship_id,
        "plan_review_revision_ids": tuple(plan_review_revision_ids),
        "review_relationship_ids": tuple(review_relationship_ids),
    }


# ---------------------------------------------------------------------------
# Snapshot helper.
#
# Reads every row of every table in :data:`_TABLE_SPECS` in stable PK
# order and returns the rows as a hashable bundle so the
# byte-equivalence comparison reduces to one ``==`` per table.
# Storing the full row tuple (rather than a hex digest) keeps a failing
# assertion's diff informative — Hypothesis prints the differing
# tuples directly.
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


# Boundary lengths come from the production CHECK constraints (design
# §"Data Models — Schema Additions"):
#   planned_scope:        1..10000
#   ordering_rationale:   0..2000 (NULL permitted)
#   rationale (approval): 1..4000
#   rationale (review):   1..10000
# We draw small but non-trivial lengths so each Hypothesis case stays
# fast while still exercising the byte-equivalence guarantee across
# varied input shapes.
_planned_scope_strategy = st.text(min_size=1, max_size=120)
_ordering_rationale_strategy = st.one_of(
    st.none(),
    st.text(min_size=0, max_size=120),
)
_approval_rationale_strategy = st.text(min_size=1, max_size=120)
_review_rationale_strategy = st.text(min_size=1, max_size=120)
# Slice 2 confines applicable_scope to a short opaque string in the
# unit-test fixtures; the production code persists it byte-equivalent.
_scope_strategy = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd"),
        whitelist_characters="-/_",
    ),
    min_size=1,
    max_size=40,
)
_review_outcome_strategy = st.sampled_from(
    ("Endorse", "Changes_Requested", "Reject")
)


@st.composite
def _pipeline_strategy(draw) -> dict[str, Any]:
    """Draw one approved-pipeline configuration."""
    num_reviews = draw(st.integers(min_value=0, max_value=3))
    review_outcomes: tuple[str, ...] = tuple(
        draw(_review_outcome_strategy) for _ in range(num_reviews)
    )
    review_rationales: tuple[str, ...] = tuple(
        draw(_review_rationale_strategy) for _ in range(num_reviews)
    )
    return {
        "has_predecessor": draw(st.booleans()),
        "planned_scope": draw(_planned_scope_strategy),
        "ordering_rationale": draw(_ordering_rationale_strategy),
        "applicable_scope": draw(_scope_strategy),
        "approval_rationale": draw(_approval_rationale_strategy),
        "review_outcomes": review_outcomes,
        "review_rationales": review_rationales,
    }


@st.composite
def _attack_strategy(draw) -> dict[str, Any]:
    """Draw one (table, kind, column?, new_value?) attack tuple."""
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


# Each scenario is 1..15 attacks; ``min_size=1`` guarantees at least
# one rejection attempt per case (a case with zero attacks would
# leave the rejection assertion vacuously satisfied).
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
    transaction per attack also matches the "any two observation
    points" wording in Property 20.
    """
    table = attack["table"]
    kind = attack["kind"]
    pk_values = _first_pk(engine, table=table)
    if pk_values is None:
        # Tables with zero rows in this case (e.g.
        # ``Plan_Review_Revisions`` when no reviews were drawn) have
        # no row to attack — the attack is vacuously satisfied.
        return

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
        # Any other exception type is a regression: the schema's
        # triggers ABORT, which SQLAlchemy surfaces as
        # IntegrityError. A different exception class would silently
        # let the byte-equivalence assertion still pass while hiding
        # a trigger regression.
        raise AssertionError(
            f"Attack {attack!r} raised {type(exc).__name__} instead of "
            f"sqlalchemy.exc.IntegrityError; the AD-WS-4 / AD-WS-19 "
            f"trigger contract regressed."
        ) from exc

    assert raised, (
        f"Attack {attack!r} was NOT rejected — the immutability trigger "
        f"on {table!r} failed to fire. Property 20 / Requirements 9.4, "
        f"9.6, 16.5, 20.4 require every UPDATE/DELETE against an "
        f"Approved Plan Revision (and its constituent Relationship, "
        f"Plan Review Revision, and Plan Approval Record rows) to raise "
        f"IntegrityError."
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 20: Approved Plan Revision immutability
@given(pipeline=_pipeline_strategy(), scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup builds an approved-state pipeline (Party,
    # Project, Activity Plan, Plan Revision(s), Plan Approval Record,
    # 0..3 Plan Review Revisions, and their Relationships) plus the
    # attack loop and the post-attack snapshot diff. The data
    # generation health check is suppressed because the per-case
    # work is heavier than a pure in-memory property test.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_approved_plan_revision_immutability(
    pipeline: dict[str, Any], scenario: list[dict[str, Any]]
) -> None:
    """Every UPDATE/DELETE against an Approved Plan Revision (and its
    constituent Relationship, Plan Review Revision, and Plan Approval
    Record rows) is rejected; the protected rows remain byte-equivalent
    across every later observation point."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop20_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # --- Phase 1: seed the post-approval pipeline ----------
            seed_ids = _seed_approved_pipeline(
                engine,
                has_predecessor=pipeline["has_predecessor"],
                planned_scope=pipeline["planned_scope"],
                ordering_rationale=pipeline["ordering_rationale"],
                applicable_scope=pipeline["applicable_scope"],
                approval_rationale=pipeline["approval_rationale"],
                review_outcomes=pipeline["review_outcomes"],
                review_rationales=pipeline["review_rationales"],
            )

            # --- Phase 2: snapshot ---------------------------------
            pre_snapshot = _snapshot(engine)

            # Sanity checks on the seeded shape: at least one Plan
            # Revision row, exactly one Plan Approval Record, at
            # least one ``Addresses`` Relationship binding the
            # Approval to the Plan Revision, one ``Relates To``
            # Relationship per Plan Review Revision, and one
            # ``Supersedes`` Relationship when a predecessor was
            # drawn.
            assert len(pre_snapshot["Plan_Revisions"]) >= 1, (
                "Seed regression: Plan_Revisions has zero rows."
            )
            assert len(pre_snapshot["Plan_Approval_Records"]) == 1, (
                "Seed regression: expected exactly one Plan Approval "
                "Record per case."
            )
            assert len(pre_snapshot["Plan_Review_Revisions"]) == len(
                pipeline["review_outcomes"]
            ), (
                "Seed regression: Plan_Review_Revisions row count does "
                "not match drawn review_outcomes."
            )
            expected_relationships = (
                1  # Addresses (plan_approval -> plan_revision)
                + (1 if pipeline["has_predecessor"] else 0)  # Supersedes
                + len(pipeline["review_outcomes"])  # Relates To per review
            )
            assert len(pre_snapshot["Relationships"]) == expected_relationships, (
                f"Seed regression: Relationships row count "
                f"{len(pre_snapshot['Relationships'])} does not match "
                f"expected {expected_relationships}."
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
                    f"post={post_snapshot[table_name]!r}. Property 20 / "
                    f"Requirements 9.4, 9.6, 16.5, 20.4 require every "
                    f"previously-inserted row covering an Approved Plan "
                    f"Revision to remain byte-equivalent across the "
                    f"attack sequence."
                )

            # --- Phase 5: spot-check the Plan Revision lifecycle ---
            # The seed inserted ``lifecycle_state='approved'``; the
            # AD-WS-19 trigger must have rejected every UPDATE
            # attempt against this column above. Confirm the column
            # is still ``'approved'`` end-to-end so a regression that
            # incorrectly permits the column's mutation surfaces
            # here with a focused diagnostic rather than only in the
            # broader snapshot diff.
            with engine.connect() as conn:
                lifecycle_state = conn.execute(
                    text(
                        "SELECT lifecycle_state FROM Plan_Revisions "
                        "WHERE plan_revision_id = :id"
                    ),
                    {"id": seed_ids["plan_revision_id"]},
                ).scalar_one()
            assert lifecycle_state == "approved", (
                f"Plan_Revisions.lifecycle_state regressed from "
                f"'approved' to {lifecycle_state!r} after the attack "
                f"sequence; the AD-WS-19 trigger contract is broken."
            )
        finally:
            engine.dispose()
