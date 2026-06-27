"""Additive seeding of ``Disclosure_Policy_Coverage`` for Slice 2 node kinds.

Design reference: ``.kiro/specs/second-walking-slice/design.md`` §"AD-WS-16 —
Additive Disclosure-policy extension via new coverage rows" and §"AD-WS-23 —
Disclosure policy coverage is enforced by lookup, not by per-node code paths".

Responsibility of this module (task 1.4)
=========================================

This module is the additive surface AD-WS-16 mandates: instead of mutating
the seeded ``slice-default-2026`` row in ``Disclosure_Policies``, the slice
extends coverage by inserting one ``Disclosure_Policy_Coverage`` row per
Slice 2 node kind. The existing Slice 1 row identity, rule set, and
``effective_start`` are unchanged (Requirement 19.2 — Slice 1
non-modification; Requirement 17.5 — additive extension that does not alter
the policy identity or the Slice 1 rule scope).

The seeded coverage rows let the Slice 1 ``slice-default-2026`` rule set
(redaction-marker, gap-descriptor, restricted-vs-nonexistent normalization)
apply uniformly to every Slice 2 node kind that the Provenance_Navigator,
Authorization_Service, and Planning_Service may surface:

- ``objective``                          / ``objective_revision``
- ``intended_outcome``                   / ``intended_outcome_revision``
- ``project``                            / ``project_revision``
- ``deliverable_expectation``            / ``deliverable_expectation_revision``
- ``activity_plan``
- ``plan_revision``
- ``plan_review``                        / ``plan_review_revision``
- ``plan_approval``

Each row records ``policy_id = 'slice-default-2026'``, the recorded date,
and ``backlog_adr_id = 'ADR-HT-009'`` (the backlog ADR reserved by Gap G-7
to formalize the additive policy-extension surface). The
``Disclosure_Policy_Coverage`` table is insert-only after seeding (the
``UPDATE`` / ``DELETE`` triggers in
:mod:`walking_slice.planning._persistence` reject mutation), so the seed
uses ``INSERT OR IGNORE`` against the composite primary key
``(policy_id, node_kind)`` and is idempotent across repeated calls.

Requirements satisfied (per task 1.4)
=====================================

    17.1 — every Slice 2 node kind is covered by an additive extension of
           ``slice-default-2026`` rather than a separate policy.
    17.2 — restricted Slice 2 nodes are replaced with the AD-WS-9 redaction
           marker (the rule set is inherited from the existing policy row;
           coverage rows opt the node kind in).
    17.3 — Slice 2 nodes in unavailable/stale/unresolved categories return
           the AD-WS-9 gap descriptor (same inheritance path).
    17.4 — Slice 2 restricted-vs-nonexistent observability is normalized to
           the Slice 1 behavior (same inheritance path).
    17.5 — the extension is recorded as additive rows that do not alter the
           policy identity or the Slice 1 rule scope; each row identifies a
           covered node kind, the recorded date, and the backlog ADR
           identifier reserved for replacement (Gap G-7).
    19.2 — the only Slice 1 touch is the additive sibling table; the
           ``Disclosure_Policies`` row is not modified.

Wiring
======

Task 15.2 calls :func:`seed_planning_coverage` from
:mod:`walking_slice.app` startup, in the same hook that calls
:func:`walking_slice.planning._persistence.create_planning_schema` and the
Slice 2 Interim ADR seeder. The function accepts a SQLAlchemy
:class:`~sqlalchemy.engine.Connection` so the seeding participates in the
caller's transaction (the production startup hook opens one
``engine.begin()`` block that runs schema creation, coverage seeding, and
Interim ADR seeding together so a partial bootstrap is rolled back).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.clock import Clock, SystemClock
from walking_slice.disclosure import SLICE_DEFAULT_POLICY_ID


__all__ = [
    "PLANNING_NODE_KINDS",
    "PLANNING_COVERAGE_BACKLOG_ADR_ID",
    "PlanningCoverageSeedRow",
    "seed_planning_coverage",
]


# ---------------------------------------------------------------------------
# Public constants.
# ---------------------------------------------------------------------------


PLANNING_COVERAGE_BACKLOG_ADR_ID: Final[str] = "ADR-HT-009"
"""Backlog ADR identifier recorded on every Slice 2 coverage row (AD-WS-16).

Gap G-7 ("additive policy extension for new node kinds") reserves
``ADR-HT-009`` for the future ADR that formalizes the additive
policy-extension surface. Every coverage row carries this identifier so
:mod:`walking_slice.interim_adr` and Property 26 (task 16.11) can join
``Disclosure_Policy_Coverage`` back to the corresponding
``Interim_ADR_Records`` row by ``backlog_adr_id``.
"""


# Ordered tuple of every Slice 2 node kind that needs disclosure coverage.
#
# The order follows the dependency graph in design §"Planning Resource
# services": Objective → Intended Outcome / Project → Deliverable
# Expectation / Activity Plan → Plan Revision → Plan Review → Plan
# Approval. Tests and Property 26 iterate this tuple in declaration order
# so a change to the set produces a single localized diff rather than a
# scattered set of test edits.
PLANNING_NODE_KINDS: Final[tuple[str, ...]] = (
    "objective",
    "objective_revision",
    "intended_outcome",
    "intended_outcome_revision",
    "project",
    "project_revision",
    "deliverable_expectation",
    "deliverable_expectation_revision",
    "activity_plan",
    "plan_revision",
    "plan_review",
    "plan_review_revision",
    "plan_approval",
)
"""Every Slice 2 node kind that receives a ``Disclosure_Policy_Coverage`` row.

Sourced verbatim from task 1.4 and Requirement 17.1. The tuple is a
``Final`` so callers (tests, Property 26, the Planning_Service's
``policy_for`` lookups) can rely on its membership being stable across
imports.
"""


# ---------------------------------------------------------------------------
# Row shape.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanningCoverageSeedRow:
    """Canonical contents of one ``Disclosure_Policy_Coverage`` row.

    Attributes:
        policy_id: The ``Disclosure_Policies`` row the coverage extends.
            Always :data:`walking_slice.disclosure.SLICE_DEFAULT_POLICY_ID`
            for the Slice 2 seed.
        node_kind: The covered node kind, drawn from
            :data:`PLANNING_NODE_KINDS`.
        backlog_adr_id: The backlog ADR identifier reserved for the
            future replacement (Gap G-7 → ``ADR-HT-009``).
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


def seed_planning_coverage(
    connection: Connection,
    *,
    clock: Clock | None = None,
) -> None:
    """Insert one ``Disclosure_Policy_Coverage`` row per Slice 2 node kind.

    Every row carries ``policy_id = 'slice-default-2026'``, the recorded
    date, and ``backlog_adr_id = 'ADR-HT-009'``. The function is
    idempotent: every insert uses ``INSERT OR IGNORE`` against the
    composite primary key ``(policy_id, node_kind)``, so repeated calls
    against the same database leave the table byte-equivalent after the
    first successful invocation. This is the behavior
    :mod:`walking_slice.app` relies on at startup (task 15.2) — the
    Planning_Service is mounted by every process that starts the slice,
    so the coverage seed runs once per process and must be safe to run
    against an already-seeded database.

    The function accepts a SQLAlchemy
    :class:`~sqlalchemy.engine.Connection` (rather than an
    :class:`~sqlalchemy.engine.Engine`) so the seeding participates in
    the caller's transaction. The production startup hook opens one
    ``engine.begin()`` block that runs Slice 2 schema creation, this
    coverage seed, and the Slice 2 Interim ADR seed together so a
    partial bootstrap is rolled back.

    Args:
        connection: A SQLAlchemy :class:`~sqlalchemy.engine.Connection`
            with an active transaction. The ``Disclosure_Policies``
            row keyed on ``slice-default-2026`` MUST already be present
            (seeded by :func:`walking_slice.disclosure.seed`) — the
            ``Disclosure_Policy_Coverage`` foreign key fails otherwise
            and the transaction aborts.
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

    for node_kind in PLANNING_NODE_KINDS:
        connection.execute(
            text(_INSERT_STATEMENT),
            {
                "policy_id": SLICE_DEFAULT_POLICY_ID,
                "node_kind": node_kind,
                "recorded_at": recorded_at,
                "backlog_adr_id": PLANNING_COVERAGE_BACKLOG_ADR_ID,
            },
        )
