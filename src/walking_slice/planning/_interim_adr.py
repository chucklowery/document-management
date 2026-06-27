"""Additive Interim_ADR_Records seeding for Slice 2 Gaps G-6 through G-10.

Design reference: ``.kiro/specs/second-walking-slice/design.md`` §"AD-WS-15"
through §"AD-WS-19"; ``.kiro/specs/second-walking-slice/requirements.md``
§"Identified gaps" (G-6 through G-10), §19.5, and §21.3.

Responsibility of this module (task 13.1)
=========================================

The Slice 1 module :mod:`walking_slice.interim_adr` seeds exactly one
``Interim_ADR_Records`` row per Slice 1 Gap G-1..G-5 (AD-WS-6..AD-WS-10).
Its public contract — and the Slice 1 test that pins
``len(INTERIM_ADR_SEED_ROWS) == 5`` — is part of the Slice 1 stable surface
that Requirement 19.1 forbids Slice 2 from weakening, broadening, or
replacing. This module is the additive surface Slice 2 uses to record the
five new Gaps it introduces without touching the Slice 1 module:

==== ======== =====================  ==================================================
Gap  AD-WS    Backlog ADR            Subject
==== ======== =====================  ==================================================
G-6  AD-WS-15 ``ADR-HT-006``         Additive ``review`` authority enumeration value
G-7  AD-WS-16 ``ADR-HT-009``         Additive Disclosure-policy extension surface
G-8  AD-WS-17 ``ADR-HT-010``         Plan Review / Plan Approval relationship semantics
G-9  AD-WS-18 ``ADR-HT-011``         Plan Revision lifecycle state enumeration
G-10 AD-WS-19 ``ADR-HT-012``         Per-Resource-kind tables with append-only triggers
==== ======== =====================  ==================================================

Each row records the motivating Requirement number, the motivating
criterion number, the observable behavior chosen by Slice 2, the recorded
date of the choice, and the backlog ADR identifier reserved for
replacement — the exact field set Slice 1 Requirement 16.3 mandates for
every interim behavior.

Idempotence and Slice 1 reuse
=============================

:func:`seed_planning_interim_adr` first invokes the existing Slice 1
:func:`walking_slice.interim_adr.seed` (so the Slice 1 rows are present
regardless of which seed entrypoint the application chooses to wire) and
then inserts the five Slice 2 rows. Both stages use ``INSERT OR IGNORE``
against a stable per-row primary key (``ad-ws-15`` through ``ad-ws-19``
for this module's rows), so repeated invocations leave the table
byte-equivalent after the first successful call. This matches the
Slice 1 behavior the FastAPI startup hook (task 15.2) relies on and the
behavior Property 26 (task 16.11) probes for byte-equivalence across
observation points.

Requirements satisfied (per task 13.1)
======================================

    16.3 — every interim behavior introduced by Slice 2 has a record
           carrying the motivating Requirement number, motivating
           criterion, observable behavior, recorded date, and backlog
           ADR identifier, retrievable by backlog ADR identifier.
    19.5 — additive Interim ADR records cover Gaps G-6 through G-10 as
           new rows in the ``Interim_ADR_Records`` registry without
           mutating any Slice 1 row.
    21.3 — every Slice 2 interim behavior is recorded with the field
           set named by Slice 1 Requirement 16.3 and is retrievable by
           backlog ADR identifier.

Wiring
======

Task 15.2 calls :func:`seed_planning_interim_adr` from
:mod:`walking_slice.app` startup in the same hook that calls
:func:`walking_slice.planning._persistence.create_planning_schema` and
:func:`walking_slice.planning._disclosure.seed_planning_coverage`. The
function accepts a SQLAlchemy :class:`~sqlalchemy.engine.Engine` so its
signature mirrors the existing Slice 1 seed entrypoint.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import Clock, SystemClock
from walking_slice.interim_adr import (
    InterimAdrSeedRow,
    seed as seed_slice1_interim_adr,
)


__all__ = [
    "PLANNING_INTERIM_ADR_SEED_ROWS",
    "seed_planning_interim_adr",
]


# ---------------------------------------------------------------------------
# Row definitions.
#
# Every row is shaped by :class:`walking_slice.interim_adr.InterimAdrSeedRow`
# so this module shares the Slice 1 value object exactly. The ``record_id``
# values continue the ``ad-ws-N`` numbering scheme established by the Slice 1
# seed (which uses ``ad-ws-6`` through ``ad-ws-10``); ``INSERT OR IGNORE``
# against these stable keys is what makes repeated seeding idempotent across
# processes and across multiple invocations within one process.
# ---------------------------------------------------------------------------


# AD-WS-15 — Additive ``review`` authority enumeration value (closes Gap G-6,
# input to ADR-HT-006). Design §"AD-WS-15"; Requirement 11.1, 11.4, 11.5.
_AD_WS_15 = InterimAdrSeedRow(
    record_id="ad-ws-15",
    motivating_requirement="Requirement 11.1, 11.4, 11.5; Gap G-6",
    motivating_criterion="additive review authority enumeration value",
    observable_behavior=(
        "Authorization_Service._VALID_AUTHORITIES is extended additively to "
        "include the literal value 'review' alongside 'view', 'modify', and "
        "'approve'; the _required_authority mapping routes create.plan_review "
        "to 'review' and create.plan_approval to 'approve'; the Slice 1 "
        "non-substitution rule is preserved (Requirement 12.4)"
    ),
    backlog_adr_id="ADR-HT-006",
)

# AD-WS-16 — Additive Disclosure-policy extension via new coverage rows
# (closes Gap G-7, input to ADR-HT-009). Design §"AD-WS-16"; Requirement
# 17.1, 17.2, 17.5.
_AD_WS_16 = InterimAdrSeedRow(
    record_id="ad-ws-16",
    motivating_requirement="Requirement 17.1, 17.2, 17.5; Gap G-7",
    motivating_criterion="additive disclosure-policy extension mechanism",
    observable_behavior=(
        "the slice-default-2026 Disclosure_Policies row is left "
        "byte-equivalent; Slice 2 coverage is added by inserting one "
        "Disclosure_Policy_Coverage row per Slice 2 node kind, and "
        "walking_slice.disclosure.policy_for consults the union of "
        "Disclosure_Policies.policy_rules and "
        "Disclosure_Policy_Coverage.node_kind to determine policy "
        "applicability"
    ),
    backlog_adr_id="ADR-HT-009",
)

# AD-WS-17 — Plan Review uses ``Relates To`` with a ``review`` semantic-role
# discriminator; Plan Approval uses ``Addresses`` (closes Gap G-8, input to
# ADR-HT-010). Design §"AD-WS-17"; Requirement 8.1, 8.4, 9.1.
_AD_WS_17 = InterimAdrSeedRow(
    record_id="ad-ws-17",
    motivating_requirement="Requirement 8.1, 8.4, 9.1; Gap G-8",
    motivating_criterion="Plan Review and Plan Approval relationship semantics",
    observable_behavior=(
        "a Plan Review inserts one Relationships row with "
        "relationship_type='Relates To' and semantic_role='review' on the "
        "new additive Relationships.semantic_role column; a Plan Approval "
        "inserts one Relationships row with relationship_type='Addresses' "
        "and semantic_role=NULL, mirroring the Slice 1 Decision -> "
        "Recommendation Revision pattern"
    ),
    backlog_adr_id="ADR-HT-010",
)

# AD-WS-18 — Plan Revision lifecycle states limited to ``{draft, approved}``
# for this slice (closes Gap G-9, input to ADR-HT-011). Design §"AD-WS-18";
# Requirement 7.1, 7.3, 9.1.
_AD_WS_18 = InterimAdrSeedRow(
    record_id="ad-ws-18",
    motivating_requirement="Requirement 7.1, 7.3, 9.1; Gap G-9",
    motivating_criterion="Plan Revision lifecycle state enumeration",
    observable_behavior=(
        "Plan_Revisions.lifecycle_state accepts exactly the two values "
        "'draft' and 'approved' via a CHECK constraint; the only governed "
        "transition is 'draft' -> 'approved', occurring atomically inside "
        "the Plan Approval transaction (AD-WS-20); the constitutional "
        "states 'superseded', 'withdrawn', and 'archived' are deferred to "
        "a later slice"
    ),
    backlog_adr_id="ADR-HT-011",
)

# AD-WS-19 — Per-Resource-kind tables with append-only triggers, plus the
# tightly scoped ``Plan_Revisions`` UPDATE exception (closes Gap G-10, input
# to ADR-HT-012). Design §"AD-WS-19"; Requirement 1.1, 9.4, 19.4.
_AD_WS_19 = InterimAdrSeedRow(
    record_id="ad-ws-19",
    motivating_requirement="Requirement 1.1, 9.4, 19.4; Gap G-10",
    motivating_criterion="Planning Resource persistence representation",
    observable_behavior=(
        "each Slice 2 Resource and Revision kind (Objectives, "
        "Objective_Revisions, Intended_Outcomes, Intended_Outcome_Revisions, "
        "Projects, Project_Revisions, Deliverable_Expectations, "
        "Deliverable_Expectation_Revisions, Activity_Plans, Plan_Revisions, "
        "Plan_Reviews, Plan_Review_Revisions, Plan_Approval_Records, "
        "Disclosure_Policy_Coverage) is held in its own SQLite table with "
        "UPDATE and DELETE triggers that reject mutation (Slice 1 AD-WS-4 "
        "pattern); Plan_Revisions additionally has a tightly scoped "
        "trigger that permits exactly the transition ('draft','approved') "
        "only when the connection-scoped pragma "
        "walking_slice.plan_approval_in_progress is set inside the Plan "
        "Approval transaction"
    ),
    backlog_adr_id="ADR-HT-012",
)


# Public, ordered tuple — rows are listed in Gap G-6..G-10 / AD-WS-15..19
# order, which is the order Property 26 (task 16.11) iterates the
# enumerated identifier set ``{ADR-HT-006, ADR-HT-009, ADR-HT-010,
# ADR-HT-011, ADR-HT-012}`` and the order this module's docstring
# documents.
PLANNING_INTERIM_ADR_SEED_ROWS: Final[tuple[InterimAdrSeedRow, ...]] = (
    _AD_WS_15,
    _AD_WS_16,
    _AD_WS_17,
    _AD_WS_18,
    _AD_WS_19,
)


# ---------------------------------------------------------------------------
# Seed entry point.
# ---------------------------------------------------------------------------


_INSERT_STATEMENT: Final[str] = """
    INSERT OR IGNORE INTO Interim_ADR_Records (
        record_id,
        motivating_requirement,
        motivating_criterion,
        observable_behavior,
        recorded_at,
        backlog_adr_id
    ) VALUES (
        :record_id,
        :motivating_requirement,
        :motivating_criterion,
        :observable_behavior,
        :recorded_at,
        :backlog_adr_id
    )
"""


def seed_planning_interim_adr(engine: Engine, *, clock: Clock | None = None) -> None:
    """Seed Slice 1 and Slice 2 ``Interim_ADR_Records`` rows.

    The function performs two stages, both idempotent:

    1. Invoke :func:`walking_slice.interim_adr.seed` to insert the
       Slice 1 rows for Gaps G-1..G-5 (AD-WS-6..AD-WS-10). Calling
       through the Slice 1 entrypoint — rather than duplicating its
       row literals here — guarantees the two modules cannot drift on
       Slice 1 row contents.
    2. Open a fresh ``engine.begin()`` transaction and insert the
       Slice 2 rows for Gaps G-6..G-10 (AD-WS-15..AD-WS-19) via
       ``INSERT OR IGNORE`` against the stable record-id keys
       ``ad-ws-15`` through ``ad-ws-19``.

    Because every insert is ``INSERT OR IGNORE`` against a stable
    primary key, repeated invocations against the same database are
    byte-equivalent: the ``recorded_at`` value of the very first
    successful insert is the value preserved in the database;
    subsequent calls (with a later ``clock.now()``) do not overwrite
    it. The FastAPI startup hook in :mod:`walking_slice.app` (task
    15.2) relies on this — multiple processes may start the slice
    against one SQLite file and every process must see the same row
    contents.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database
            whose schema has already been created — both the Slice 1
            schema via :func:`walking_slice.persistence.create_schema`
            (which builds ``Interim_ADR_Records``) and, for the
            companion seeders called alongside this one at startup,
            the Slice 2 schema via
            :func:`walking_slice.planning._persistence.create_planning_schema`.
        clock: Optional :class:`~walking_slice.clock.Clock`. When
            omitted, a :class:`~walking_slice.clock.SystemClock` is
            constructed and used to read the current UTC time. The
            ``recorded_at`` column on each inserted row is populated
            with ``clock.now()`` rendered as an ISO-8601 string with
            millisecond precision, matching design §"Cross-Cutting
            Concerns" (*Time*). The same clock is forwarded to the
            Slice 1 seed call so every row inserted in the same
            startup hook shares one recorded time.
    """
    active_clock = clock if clock is not None else SystemClock()

    # Stage 1 — Slice 1 rows. The Slice 1 seeder opens its own
    # transaction and is fully idempotent against the shared table.
    seed_slice1_interim_adr(engine, clock=active_clock)

    # Stage 2 — Slice 2 rows. A single ``engine.begin()`` transaction
    # wraps all five inserts so a mid-seed crash either persists every
    # Slice 2 row or none, matching the whole-transaction posture of
    # the Slice 1 seed.
    recorded_at = active_clock.now().isoformat(timespec="milliseconds")
    with engine.begin() as conn:
        for row in PLANNING_INTERIM_ADR_SEED_ROWS:
            conn.execute(
                text(_INSERT_STATEMENT),
                {
                    "record_id": row.record_id,
                    "motivating_requirement": row.motivating_requirement,
                    "motivating_criterion": row.motivating_criterion,
                    "observable_behavior": row.observable_behavior,
                    "recorded_at": recorded_at,
                    "backlog_adr_id": row.backlog_adr_id,
                },
            )
