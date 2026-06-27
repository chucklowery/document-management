"""Additive Interim_ADR_Records seeding for Slice 3 Gaps G-11 through G-15.

Design reference: ``.kiro/specs/third-walking-slice/design.md`` §"AD-WS-24"
through §"AD-WS-28"; ``.kiro/specs/third-walking-slice/requirements.md``
§"Identified gaps" (G-11 through G-15) and §37.6.

Responsibility of this module (task 14.1)
=========================================

The Slice 1 module :mod:`walking_slice.interim_adr` seeds exactly one
``Interim_ADR_Records`` row per Slice 1 Gap G-1..G-5 (AD-WS-6..AD-WS-10).
The Slice 2 module :mod:`walking_slice.planning._interim_adr` seeds one
row per Slice 2 Gap G-6..G-10 (AD-WS-15..AD-WS-19). Their public contracts
— and the tests that pin ``len(INTERIM_ADR_SEED_ROWS) == 5`` and
``len(PLANNING_INTERIM_ADR_SEED_ROWS) == 5`` — are part of the stable
Slice 1 + Slice 2 surface that Requirement 40.1 forbids Slice 3 from
weakening, broadening, or replacing. This module is the additive surface
Slice 3 uses to record the five new Gaps it introduces without touching
either prior-slice module:

==== ======== =====================  ==================================================
Gap  AD-WS    Backlog ADR            Subject
==== ======== =====================  ==================================================
G-11 AD-WS-24 ``ADR-HT-013``         Additive ``assign``, ``contribute``,
                                     ``accept_milestone``, ``complete`` authority values
G-12 AD-WS-25 ``ADR-HT-014``         Additive Disclosure-policy extension via new
                                     ``Disclosure_Policy_Coverage`` rows
G-13 AD-WS-26 ``ADR-HT-015``         Canonical Slice 3 Relationship Types and
                                     semantic-role markers
G-14 AD-WS-27 ``ADR-HT-016``         Append-only-no-supersession stance for every
                                     Slice 3 Record and produced Deliverable Revision
G-15 AD-WS-28 ``ADR-HT-017``         Per-Record-kind tables with append-only triggers;
                                     eight new ``Identifier_Registry.resource_kind`` values
==== ======== =====================  ==================================================

Each row records the motivating Requirement number, the motivating
criterion number, the observable behavior chosen by Slice 3, the recorded
date of the choice, and the backlog ADR identifier reserved for
replacement — the exact field set Slice 1 Requirement 16.3 mandates for
every interim behavior and Slice 3 Requirement 37.6 re-asserts for the
five new gaps.

Idempotence and Slice 1 / Slice 2 reuse
=======================================

:func:`seed_execution_interim_adr` inserts the five Slice 3 rows via
``INSERT OR IGNORE`` against the stable per-row primary key
``ad-ws-24`` through ``ad-ws-28``, so repeated invocations against the
same database leave the table byte-equivalent after the first successful
call. The Slice 1 rows (seeded by :func:`walking_slice.interim_adr.seed`)
and the Slice 2 rows (seeded by
:func:`walking_slice.planning._interim_adr.seed_planning_interim_adr`)
are not re-written by this module; the startup hook in
:mod:`walking_slice.app` is responsible for invoking each slice's
seeder in turn so all fifteen rows are present after bootstrap.

The Slice 2 seeder calls the Slice 1 seeder internally (so the Slice 1
rows are present regardless of which seed entrypoint the application
chooses to wire), but this Slice 3 seeder does *not* delegate to the
prior-slice seeders: it takes a :class:`~sqlalchemy.engine.Connection`
rather than an :class:`~sqlalchemy.engine.Engine` so the writes
participate in the caller's transaction (matching the
:func:`walking_slice.execution._disclosure.seed_execution_coverage`
signature), and the Slice 1 / Slice 2 seeders open their own
``engine.begin()`` blocks which would not nest inside the caller's
transaction. The production startup hook (task 15.3) invokes the three
seeders in the documented order; this module's only responsibility is
the additive Slice 3 rows.

Requirements satisfied (per task 14.1)
======================================

    16.3 — every interim behavior introduced by Slice 3 has a record
           carrying the motivating Requirement number, motivating
           criterion, observable behavior, recorded date, and backlog
           ADR identifier, retrievable by backlog ADR identifier (per
           the Slice 1 stable surface reused via Requirement 40.1).
    37.6 — Slice 3 seeds five additive ``Interim_ADR_Records`` rows
           with backlog ADR identifiers ``ADR-HT-013``, ``ADR-HT-014``,
           ``ADR-HT-015``, ``ADR-HT-016``, and ``ADR-HT-017``,
           corresponding respectively to Gaps G-11 through G-15.
    40.5 — additive Interim ADR records cover Gaps G-11 through G-15 as
           new rows in the ``Interim_ADR_Records`` registry without
           mutating any Slice 1 or Slice 2 row.
    42.3 — every Slice 3 interim behavior is recorded with the field
           set named by Slice 1 Requirement 16.3 and is retrievable by
           backlog ADR identifier.
    42.4 — the five Slice 3 backlog ADR identifiers
           ``{ADR-HT-013, ADR-HT-014, ADR-HT-015, ADR-HT-016, ADR-HT-017}``
           are seeded as a disjoint extension alongside the Slice 1
           identifiers ``{ADR-HT-002, ADR-HT-003, ADR-HT-004, ADR-HT-005,
           ADR-HT-008}`` and the Slice 2 identifiers ``{ADR-HT-006,
           ADR-HT-009, ADR-HT-010, ADR-HT-011, ADR-HT-012}``.

Wiring
======

Task 15.3 calls :func:`seed_execution_interim_adr` from
:mod:`walking_slice.app` startup, in the same hook that calls
:func:`walking_slice.execution._persistence.create_execution_schema`,
:func:`walking_slice.deliverables._persistence.create_deliverable_schema`,
and :func:`walking_slice.execution._disclosure.seed_execution_coverage`.
The function accepts a SQLAlchemy
:class:`~sqlalchemy.engine.Connection` so the seeding participates in
the caller's transaction (the production startup hook opens one
``engine.begin()`` block that runs Slice 3 schema creation and the
Slice 3 seeders together so a partial bootstrap is rolled back).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.clock import Clock, SystemClock
from walking_slice.interim_adr import InterimAdrSeedRow


__all__ = [
    "EXECUTION_INTERIM_ADR_SEED_ROWS",
    "seed_execution_interim_adr",
]


# ---------------------------------------------------------------------------
# Row definitions.
#
# Every row is shaped by :class:`walking_slice.interim_adr.InterimAdrSeedRow`
# so this module shares the Slice 1 value object exactly. The ``record_id``
# values continue the ``ad-ws-N`` numbering scheme established by Slice 1
# (which uses ``ad-ws-6`` through ``ad-ws-10``) and Slice 2 (which uses
# ``ad-ws-15`` through ``ad-ws-19``); ``INSERT OR IGNORE`` against these
# stable keys is what makes repeated seeding idempotent across processes
# and across multiple invocations within one process.
# ---------------------------------------------------------------------------


# AD-WS-24 — Additive ``assign``, ``contribute``, ``accept_milestone``,
# ``complete`` authority values (closes Gap G-11, input to ADR-HT-013).
# Design §"AD-WS-24"; Requirement 32.1, 32.2, 32.3, 32.4, 32.5, 32.10,
# 32.11.
_AD_WS_24 = InterimAdrSeedRow(
    record_id="ad-ws-24",
    motivating_requirement=(
        "Requirement 32.1, 32.2, 32.3, 32.4, 32.5, 32.10, 32.11; Gap G-11"
    ),
    motivating_criterion="additive Slice 3 authority enumeration values",
    observable_behavior=(
        "Authorization_Service._VALID_AUTHORITIES is extended additively to "
        "include the literal values 'assign', 'contribute', "
        "'accept_milestone', and 'complete' alongside 'view', 'modify', "
        "'review', and 'approve'; the _required_authority mapping routes "
        "create.work_assignment to 'assign', create.work_event / "
        "create.time_entry / create.produced_deliverable / "
        "create.deliverable_production to 'contribute', "
        "create.milestone_acceptance to 'accept_milestone', and "
        "create.completion to 'complete'; the Slice 1 non-substitution rule "
        "is preserved across the cumulative eight-value enumeration "
        "(Requirements 12.4, 11.6, 32.10, 32.11)"
    ),
    backlog_adr_id="ADR-HT-013",
)

# AD-WS-25 — Additive Disclosure-policy extension via new coverage rows
# for Slice 3 node kinds (closes Gap G-12, input to ADR-HT-014). Design
# §"AD-WS-25"; Requirement 38.1, 38.2, 38.3, 38.4, 38.5.
_AD_WS_25 = InterimAdrSeedRow(
    record_id="ad-ws-25",
    motivating_requirement=(
        "Requirement 38.1, 38.2, 38.3, 38.4, 38.5; Gap G-12"
    ),
    motivating_criterion=(
        "additive disclosure-policy extension to Slice 3 node kinds"
    ),
    observable_behavior=(
        "the slice-default-2026 Disclosure_Policies row is left "
        "byte-equivalent; Slice 3 coverage is added by inserting one "
        "Disclosure_Policy_Coverage row per Slice 3 node kind — "
        "work_assignment_record, work_event_record, time_entry_record, "
        "deliverable_resource, deliverable_revision, "
        "deliverable_production_record, milestone_acceptance_record, "
        "completion_record — reusing the Slice 2 AD-WS-16 coverage table "
        "with no new column or policy identity; "
        "walking_slice.disclosure.policy_for resolves each Slice 3 "
        "node_kind to the same DisclosurePolicy already covering Slice 1 "
        "and Slice 2"
    ),
    backlog_adr_id="ADR-HT-014",
)

# AD-WS-26 — Canonical Slice 3 Relationship Types and semantic-role
# markers (closes Gap G-13, input to ADR-HT-015). Design §"AD-WS-26";
# Requirements 23.7, 24.4, 25.3, 27.5, 28.4, 29.4, 36.6.
_AD_WS_26 = InterimAdrSeedRow(
    record_id="ad-ws-26",
    motivating_requirement=(
        "Requirement 23.7, 24.4, 25.3, 27.5, 28.4, 29.4, 36.6; Gap G-13"
    ),
    motivating_criterion=(
        "canonical Slice 3 Relationship Types and semantic-role markers"
    ),
    observable_behavior=(
        "Work Assignment Record -> Plan Revision is 'Addresses' with "
        "semantic_role NULL; Work Assignment Record -> assignee Party is "
        "'Relates To' with semantic_role 'assignee'; Work Event Record -> "
        "Work Assignment Record is 'Relates To' with semantic_role "
        "'work_event'; Time Entry Record -> Work Assignment Record is "
        "'Relates To' with semantic_role 'time_entry'; Deliverable "
        "Production Record -> produced Deliverable Revision is 'Produces' "
        "with semantic_role NULL; Deliverable Production Record -> target "
        "Deliverable Expectation Revision is 'Addresses' with semantic_role "
        "NULL; Deliverable Production Record -> source Work Assignment "
        "Record is 'Relates To' with semantic_role 'production_source'; "
        "Milestone Acceptance Record -> produced Deliverable Revision is "
        "'Addresses' with semantic_role NULL; Completion Record -> target "
        "Approved Plan Revision is 'Addresses' with semantic_role NULL; "
        "all markers are written into the existing Slice 2 AD-WS-19 "
        "Relationships.semantic_role column with no new column or "
        "Relationship Type introduced"
    ),
    backlog_adr_id="ADR-HT-015",
)

# AD-WS-27 — Slice 3 Records are append-only with no supersession path
# (closes Gap G-14, input to ADR-HT-016). Design §"AD-WS-27";
# Requirements 23.9, 24.7, 25.6, 26.4, 27.7, 28.7, 29.7, 41.4.
_AD_WS_27 = InterimAdrSeedRow(
    record_id="ad-ws-27",
    motivating_requirement=(
        "Requirement 23.9, 24.7, 25.6, 26.4, 27.7, 28.7, 29.7, 41.4; Gap G-14"
    ),
    motivating_criterion=(
        "append-only-no-supersession stance for Slice 3 Records"
    ),
    observable_behavior=(
        "every Slice 3 Record kind (Work_Assignment_Records, "
        "Work_Event_Records, Time_Entry_Records, "
        "Deliverable_Production_Records, Milestone_Acceptance_Records, "
        "Completion_Records) and every produced Deliverable Resource and "
        "Revision (Deliverable_Resources, Deliverable_Revisions) is "
        "insert-only; UPDATE and DELETE triggers reject every mutation "
        "(Slice 1 AD-WS-4 / Slice 2 AD-WS-19 pattern); no Slice 3 "
        "lifecycle-supersession path comparable to the Slice 2 "
        "Plan_Revisions.lifecycle_state transition is introduced — "
        "Record correction is deferred to a later slice consistent with "
        "Principle 5.6"
    ),
    backlog_adr_id="ADR-HT-016",
)

# AD-WS-28 — Per-Record-kind tables with append-only triggers; eight
# new ``Identifier_Registry.resource_kind`` values (closes Gap G-15,
# input to ADR-HT-017). Design §"AD-WS-28"; Requirements 22.1, 22.2,
# 22.3, 22.8, 26.3, 41.4.
_AD_WS_28 = InterimAdrSeedRow(
    record_id="ad-ws-28",
    motivating_requirement=(
        "Requirement 22.1, 22.2, 22.3, 22.8, 26.3, 41.4; Gap G-15"
    ),
    motivating_criterion=(
        "per-Record-kind tables for Slice 3 persistence representation"
    ),
    observable_behavior=(
        "each Slice 3 Record and produced Deliverable kind is held in "
        "its own SQLite table — Work_Assignment_Records, "
        "Work_Event_Records, Time_Entry_Records, Deliverable_Resources, "
        "Deliverable_Revisions, Deliverable_Production_Records, "
        "Milestone_Acceptance_Records, Completion_Records — with UPDATE "
        "and DELETE triggers that reject mutation (Slice 1 AD-WS-4 / "
        "Slice 2 AD-WS-19 pattern); no new column is added to any Slice 1 "
        "or Slice 2 table; Slice 3 emits eight new "
        "Identifier_Registry.resource_kind values "
        "{work_assignment_record, work_event_record, time_entry_record, "
        "deliverable_resource, deliverable_revision, "
        "deliverable_production_record, milestone_acceptance_record, "
        "completion_record} on the existing column, providing the "
        "disjointness assertion required by Requirements 22.8 and 26.3"
    ),
    backlog_adr_id="ADR-HT-017",
)


# Public, ordered tuple — rows are listed in Gap G-11..G-15 / AD-WS-24..28
# order, which is the order Property 45 (task 16.15) iterates the
# enumerated identifier set ``{ADR-HT-013, ADR-HT-014, ADR-HT-015,
# ADR-HT-016, ADR-HT-017}`` and the order this module's docstring
# documents.
EXECUTION_INTERIM_ADR_SEED_ROWS: Final[tuple[InterimAdrSeedRow, ...]] = (
    _AD_WS_24,
    _AD_WS_25,
    _AD_WS_26,
    _AD_WS_27,
    _AD_WS_28,
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


def seed_execution_interim_adr(
    connection: Connection,
    *,
    clock: Clock | None = None,
) -> None:
    """Insert one ``Interim_ADR_Records`` row per Slice 3 Gap G-11..G-15.

    Every row carries the motivating Requirement number, the motivating
    criterion, the observable behavior chosen by Slice 3, the recorded
    date, and the backlog ADR identifier reserved for the future
    replacement (``ADR-HT-013`` through ``ADR-HT-017``). The
    ``resolved_by_adr_id`` and ``resolved_at`` columns are left NULL
    because none of the Slice 3 backlog ADRs has been ``Accepted`` yet —
    the slice ships the interim behavior in advance of the ADR, exactly
    as Slice 1 Requirement 16.3 and Slice 3 Requirement 42.3 require.

    The function is idempotent: every insert uses ``INSERT OR IGNORE``
    against the stable record-id primary keys ``ad-ws-24`` through
    ``ad-ws-28``, so repeated calls against the same database leave the
    table byte-equivalent after the first successful invocation. This is
    the behavior :mod:`walking_slice.app` relies on at startup (task
    15.3) — the Execution_Service is mounted by every process that
    starts the slice, so the Interim ADR seed runs once per process and
    must be safe to run against an already-seeded database.

    The function accepts a SQLAlchemy
    :class:`~sqlalchemy.engine.Connection` (rather than an
    :class:`~sqlalchemy.engine.Engine`) so the seeding participates in
    the caller's transaction. The production startup hook opens one
    ``engine.begin()`` block that runs Slice 3 schema creation, the
    Slice 3 ``Disclosure_Policy_Coverage`` seed, and this Interim ADR
    seed together so a partial bootstrap is rolled back. This signature
    mirrors
    :func:`walking_slice.execution._disclosure.seed_execution_coverage`
    and differs from the Slice 1 / Slice 2 seeders only by sharing the
    caller's transaction.

    The Slice 1 + Slice 2 ``Interim_ADR_Records`` rows are not touched
    by this function (Requirement 40.5 — additive only). The startup
    hook is responsible for invoking the Slice 1 seeder
    (:func:`walking_slice.interim_adr.seed`) and the Slice 2 seeder
    (:func:`walking_slice.planning._interim_adr.seed_planning_interim_adr`)
    before or alongside this one so all fifteen Slice 1 + Slice 2 +
    Slice 3 rows are present after bootstrap.

    Args:
        connection: A SQLAlchemy :class:`~sqlalchemy.engine.Connection`
            with an active transaction. The ``Interim_ADR_Records``
            table MUST already have been created by
            :func:`walking_slice.persistence.create_schema`; this
            function does not create the table itself.
        clock: Optional :class:`~walking_slice.clock.Clock`. When
            omitted, a :class:`~walking_slice.clock.SystemClock` is
            constructed and used to read the current UTC time. The
            ``recorded_at`` column on each inserted row is populated
            with ``clock.now()`` rendered as an ISO-8601 string with
            millisecond precision, matching design §"Cross-Cutting
            Concerns" (*Time*). Because of ``INSERT OR IGNORE``, the
            ``recorded_at`` of the very first successful call is the
            value preserved in the database; subsequent calls (with a
            later ``clock.now()``) do not overwrite it.
    """
    active_clock = clock if clock is not None else SystemClock()
    recorded_at = active_clock.now().isoformat(timespec="milliseconds")

    for row in EXECUTION_INTERIM_ADR_SEED_ROWS:
        connection.execute(
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
