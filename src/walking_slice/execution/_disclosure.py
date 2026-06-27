"""Additive seeding of ``Disclosure_Policy_Coverage`` for Slice 3 node kinds.

Design reference: ``.kiro/specs/third-walking-slice/design.md`` §"AD-WS-25 —
Additive Disclosure-policy extension via new coverage rows (closes Gap G-12)".

Responsibility of this module (task 1.4)
=========================================

This module is the additive surface AD-WS-25 mandates: instead of mutating
the seeded ``slice-default-2026`` row in ``Disclosure_Policies`` or
introducing a separate disclosure policy, the slice extends coverage by
inserting one ``Disclosure_Policy_Coverage`` row per Slice 3 node kind. The
existing Slice 1 ``Disclosure_Policies`` row identity, rule set, and
``effective_start`` are unchanged; the existing Slice 2
``Disclosure_Policy_Coverage`` rows are unchanged (Requirement 40.1 / 40.2
— Slice 1 and Slice 2 non-modification; Requirement 38.5 — the additive
extension does not alter the policy identity or the Slice 1 + Slice 2 rule
scope).

The seeded coverage rows let the existing ``slice-default-2026`` rule set
(redaction-marker, gap-descriptor, restricted-vs-nonexistent normalization
— per AD-WS-9 as extended by Slice 2 Requirement 17) apply uniformly to
every Slice 3 node kind that the Provenance_Navigator,
Authorization_Service, Execution_Service, and Deliverable_Repository may
surface:

- ``work_assignment_record``
- ``work_event_record``
- ``time_entry_record``
- ``deliverable_resource``
- ``deliverable_revision``
- ``deliverable_production_record``
- ``milestone_acceptance_record``
- ``completion_record``

Each row records ``policy_id = 'slice-default-2026'``, the recorded date,
and ``backlog_adr_id = 'ADR-HT-014'`` (the backlog ADR reserved by Gap
G-12 for the future ADR that formalizes the additive policy-extension
surface for Slice 3 node kinds). The ``Disclosure_Policy_Coverage`` table
is insert-only after seeding (the ``UPDATE`` / ``DELETE`` triggers
installed by :mod:`walking_slice.planning._persistence` reject mutation),
so the seed uses ``INSERT OR IGNORE`` against the composite primary key
``(policy_id, node_kind)`` and is idempotent across repeated calls.

The Slice 1 + Slice 2 lookup function
:func:`walking_slice.disclosure.policy_for` already consults
``Disclosure_Policy_Coverage`` before falling back to the baseline
``slice-default-2026`` policy row (see AD-WS-23 / Slice 2
§"Disclosure policy coverage is enforced by lookup"), so seeding these
rows is sufficient to extend coverage to the Slice 3 node kinds without
any change to the lookup code path.

Requirements satisfied (per task 1.4)
=====================================

    38.1 — every Slice 3 node kind is covered by an additive extension of
           ``slice-default-2026`` rather than a separate policy.
    38.2 — restricted Slice 3 nodes are replaced with the AD-WS-9
           redaction marker (the rule set is inherited from the existing
           policy row; coverage rows opt the node kind in).
    38.3 — Slice 3 nodes in unavailable/stale/unresolved categories
           return the AD-WS-9 gap descriptor (same inheritance path).
    38.4 — Slice 3 restricted-vs-nonexistent observability is normalized
           to the Slice 1 + Slice 2 behavior (same inheritance path).
    38.5 — the extension is recorded as additive rows that do not alter
           the policy identity or the Slice 1 + Slice 2 rule scope; each
           row identifies a covered node kind, the recorded date, and
           the backlog ADR identifier reserved for replacement
           (Gap G-12 → ``ADR-HT-014``).
    40.2 — the only Slice 1 + Slice 2 touch is the additive sibling-table
           rows; the ``Disclosure_Policies`` row and the existing Slice 2
           ``Disclosure_Policy_Coverage`` rows are not modified.

Wiring
======

Task 15.3 calls :func:`seed_execution_coverage` from
:mod:`walking_slice.app` startup, in the same hook that calls
:func:`walking_slice.execution._persistence.create_execution_schema`, the
Slice 3 Interim ADR seeder, and the Slice 2 Disclosure_Policy_Coverage and
Interim ADR seeders. The function accepts a SQLAlchemy
:class:`~sqlalchemy.engine.Connection` so the seeding participates in the
caller's transaction (the production startup hook opens one
``engine.begin()`` block that runs schema creation and seeding together so
a partial bootstrap is rolled back).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.clock import Clock, SystemClock
from walking_slice.disclosure import SLICE_DEFAULT_POLICY_ID


__all__ = [
    "EXECUTION_NODE_KINDS",
    "EXECUTION_COVERAGE_BACKLOG_ADR_ID",
    "ExecutionCoverageSeedRow",
    "seed_execution_coverage",
]


# ---------------------------------------------------------------------------
# Public constants.
# ---------------------------------------------------------------------------


EXECUTION_COVERAGE_BACKLOG_ADR_ID: Final[str] = "ADR-HT-014"
"""Backlog ADR identifier recorded on every Slice 3 coverage row (AD-WS-25).

Gap G-12 ("additive policy extension for new execution and
produced-Deliverable node kinds") reserves ``ADR-HT-014`` for the future
ADR that formalizes the additive policy-extension surface for Slice 3.
Every coverage row carries this identifier so
:mod:`walking_slice.execution._interim_adr` (task 14) and Property 45
(task 16.15) can join ``Disclosure_Policy_Coverage`` back to the
corresponding ``Interim_ADR_Records`` row by ``backlog_adr_id``.
"""


# Ordered tuple of every Slice 3 node kind that needs disclosure coverage.
#
# The order follows the dependency graph in design §"Components and
# Interfaces": Work Assignment → Work Event / Time Entry → Deliverable
# Resource / Revision → Deliverable Production → Milestone Acceptance →
# Completion. Tests and Property 45 iterate this tuple in declaration
# order so a change to the set produces a single localized diff rather
# than a scattered set of test edits.
EXECUTION_NODE_KINDS: Final[tuple[str, ...]] = (
    "work_assignment_record",
    "work_event_record",
    "time_entry_record",
    "deliverable_resource",
    "deliverable_revision",
    "deliverable_production_record",
    "milestone_acceptance_record",
    "completion_record",
)
"""Every Slice 3 node kind that receives a ``Disclosure_Policy_Coverage`` row.

Sourced verbatim from task 1.4 and Requirement 38.1. The tuple is a
``Final`` so callers (tests, Property 45, the Provenance_Navigator's
``policy_for`` lookups) can rely on its membership being stable across
imports. The values match the ``Identifier_Registry.resource_kind`` tags
the Slice 3 services use (design §"Persistence Invariants Summary" rule
4 / Requirements 22.8 and 26.3), so the disclosure lookup and the
identifier registry agree on the same eight-kind enumeration.
"""


# ---------------------------------------------------------------------------
# Row shape.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionCoverageSeedRow:
    """Canonical contents of one Slice 3 ``Disclosure_Policy_Coverage`` row.

    Attributes:
        policy_id: The ``Disclosure_Policies`` row the coverage extends.
            Always :data:`walking_slice.disclosure.SLICE_DEFAULT_POLICY_ID`
            for the Slice 3 seed.
        node_kind: The covered node kind, drawn from
            :data:`EXECUTION_NODE_KINDS`.
        backlog_adr_id: The backlog ADR identifier reserved for the
            future replacement (Gap G-12 → ``ADR-HT-014``).
    """

    policy_id: str
    node_kind: str
    backlog_adr_id: str


# ---------------------------------------------------------------------------
# Seed entry point.
# ---------------------------------------------------------------------------


_INSERT_STATEMENT: Final[str] = """
    INSERT OR IGNORE INTO Disclosure_Policy_Coverage (
        policy_id,
        node_kind,
        recorded_at,
        backlog_adr_id
    ) VALUES (
        :policy_id,
        :node_kind,
        :recorded_at,
        :backlog_adr_id
    )
"""


def seed_execution_coverage(
    connection: Connection,
    *,
    clock: Clock | None = None,
) -> None:
    """Insert one ``Disclosure_Policy_Coverage`` row per Slice 3 node kind.

    Every row carries ``policy_id = 'slice-default-2026'``, the recorded
    date, and ``backlog_adr_id = 'ADR-HT-014'``. The function is
    idempotent: every insert uses ``INSERT OR IGNORE`` against the
    composite primary key ``(policy_id, node_kind)``, so repeated calls
    against the same database leave the table byte-equivalent after the
    first successful invocation. This is the behavior
    :mod:`walking_slice.app` relies on at startup (task 15.3) — the
    Execution_Service is mounted by every process that starts the slice,
    so the coverage seed runs once per process and must be safe to run
    against an already-seeded database.

    The function accepts a SQLAlchemy
    :class:`~sqlalchemy.engine.Connection` (rather than an
    :class:`~sqlalchemy.engine.Engine`) so the seeding participates in
    the caller's transaction. The production startup hook opens one
    ``engine.begin()`` block that runs Slice 3 schema creation, this
    coverage seed, and the Slice 3 Interim ADR seed together so a
    partial bootstrap is rolled back.

    The Slice 1 + Slice 2 row contents — the ``slice-default-2026`` row
    in ``Disclosure_Policies`` and the thirteen Slice 2 coverage rows
    seeded by
    :func:`walking_slice.planning._disclosure.seed_planning_coverage`
    — are not touched (Requirement 40.2). After this seeder runs,
    :func:`walking_slice.disclosure.policy_for` resolves each Slice 3
    ``node_kind`` to the same :class:`~walking_slice.disclosure.DisclosurePolicy`
    that already covers Slice 1 and Slice 2 (AD-WS-25 / AD-WS-23 — one
    cohesive disclosure contract across all three slices).

    Args:
        connection: A SQLAlchemy :class:`~sqlalchemy.engine.Connection`
            with an active transaction. The ``Disclosure_Policies``
            row keyed on ``slice-default-2026`` MUST already be present
            (seeded by :func:`walking_slice.disclosure.seed`) — the
            ``Disclosure_Policy_Coverage`` foreign key fails otherwise
            and the transaction aborts. The
            ``Disclosure_Policy_Coverage`` table itself is created by
            :func:`walking_slice.planning._persistence.create_planning_schema`,
            so Slice 2 schema creation MUST also have run before this
            seeder is invoked.
        clock: Optional :class:`~walking_slice.clock.Clock`. When
            omitted, a :class:`~walking_slice.clock.SystemClock` is
            constructed and used to read the current UTC time. The
            ``recorded_at`` column is populated with ``clock.now()``
            rendered as an ISO-8601 string with millisecond precision,
            matching design §"Cross-Cutting Concerns" (*Time*). Because
            of ``INSERT OR IGNORE``, the ``recorded_at`` of the very
            first successful call is the value preserved in the
            database; subsequent calls (with a later ``clock.now()``)
            do not overwrite it.
    """
    active_clock = clock if clock is not None else SystemClock()
    recorded_at = active_clock.now().isoformat(timespec="milliseconds")

    for node_kind in EXECUTION_NODE_KINDS:
        connection.execute(
            text(_INSERT_STATEMENT),
            {
                "policy_id": SLICE_DEFAULT_POLICY_ID,
                "node_kind": node_kind,
                "recorded_at": recorded_at,
                "backlog_adr_id": EXECUTION_COVERAGE_BACKLOG_ADR_ID,
            },
        )
