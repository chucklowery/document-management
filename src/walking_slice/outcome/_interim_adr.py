"""Additive Interim_ADR_Records seeding for Slice 4 Gaps G-16 through G-20.

Design reference: ``.kiro/specs/fourth-walking-slice/design.md`` §"AD-WS-33"
through §"AD-WS-38"; ``.kiro/specs/fourth-walking-slice/requirements.md``
§"Gaps Flagged for Resolution" (G-16 through G-20) and §§60.4, 60.5.

Responsibility of this module (task 12.1)
=========================================

The Slice 1 module :mod:`walking_slice.interim_adr` seeds exactly one
``Interim_ADR_Records`` row per Slice 1 Gap G-1..G-5 (AD-WS-6..AD-WS-10).
The Slice 2 module :mod:`walking_slice.planning._interim_adr` seeds one
row per Slice 2 Gap G-6..G-10 (AD-WS-15..AD-WS-19). The Slice 3 module
:mod:`walking_slice.execution._interim_adr` seeds one row per Slice 3
Gap G-11..G-15 (AD-WS-24..AD-WS-28). Their public contracts — and the
tests that pin ``len(INTERIM_ADR_SEED_ROWS) == 5``,
``len(PLANNING_INTERIM_ADR_SEED_ROWS) == 5``, and
``len(EXECUTION_INTERIM_ADR_SEED_ROWS) == 5`` — are part of the stable
Slice 1 + Slice 2 + Slice 3 surface that Requirement 60.1 forbids Slice 4
from weakening, broadening, or replacing. This module is the additive
surface Slice 4 uses to record the five new Gaps it introduces without
touching any prior-slice module:

==== ============== =================  =================================================
Gap  AD-WS          Backlog ADR        Subject
==== ============== =================  =================================================
G-16 AD-WS-38       ``ADR-HT-018``     Origin enumeration ``{native, imported}`` and
                                       source-system authority enumeration
                                       ``{authoritative, replica, projection, index,
                                       federation}`` as registered enumeration columns
G-17 AD-WS-33       ``ADR-HT-019``     Additive ``define_measurement``,
                                       ``record_measurement``, ``assess_outcome``,
                                       ``issue_outcome_review`` authority values
G-18 AD-WS-34       ``ADR-HT-020``     Additive Disclosure-policy extension via new
                                       coverage rows + per-attribute restriction for
                                       imported Measurement Records
G-19 AD-WS-35       ``ADR-HT-021``     Canonical Slice 4 Relationship Types and
                                       semantic-role markers
G-20 AD-WS-36 /     ``ADR-HT-022``     Append-only Records/Resources + explicit
     AD-WS-37                          predecessor chain; per-kind tables with
                                       append-only triggers; seven new
                                       ``Identifier_Registry.resource_kind`` values
==== ============== =================  =================================================

Each row records the motivating Requirement number, the motivating
criterion number, the observable behavior chosen by Slice 4, the recorded
date of the choice, and the backlog ADR identifier reserved for
replacement — the exact field set Slice 1 Requirement 16.3 mandates for
every interim behavior and Slice 4 Requirements 60.3/60.4 re-assert for
the five new gaps. The ``ADR-HT-018`` row additionally records the chosen
``{native, imported}`` origin member set and the
``{authoritative, replica, projection, index, federation}`` source-system
authority member set as input to the backlog ADR (AD-WS-38).

The G-20 row carries ``backlog_adr_id = 'ADR-HT-022'`` and folds both
AD-WS-36 (append-only Records/Resources with an explicit Observed Outcome
Revision predecessor chain and no supersession path) and AD-WS-37
(per-kind insert-only tables with UPDATE/DELETE triggers plus the seven
new ``Identifier_Registry.resource_kind`` values), because both decisions
record the chosen persistence representation for the single backlog ADR
``ADR-HT-022`` (design §"AD-WS-37").

Idempotence and prior-slice reuse
=================================

:func:`seed_outcome_interim_adr` inserts the five Slice 4 rows via
``INSERT OR IGNORE`` against the stable per-row primary keys ``ad-ws-38``,
``ad-ws-33``, ``ad-ws-34``, ``ad-ws-35``, and ``ad-ws-36``, so repeated
invocations against the same database leave the table byte-equivalent
after the first successful call. The Slice 1, Slice 2, and Slice 3 rows
are not re-written by this module; the startup hook in
:mod:`walking_slice.app` is responsible for invoking each slice's seeder
in turn so all twenty rows are present after bootstrap.

Like the Slice 3 seeder, this module does *not* delegate to the
prior-slice seeders: it takes a :class:`~sqlalchemy.engine.Connection`
rather than an :class:`~sqlalchemy.engine.Engine` so the writes
participate in the caller's transaction (matching the
:func:`walking_slice.outcome._disclosure.seed_outcome_coverage`
signature), and the Slice 1 / Slice 2 seeders open their own
``engine.begin()`` blocks which would not nest inside the caller's
transaction.

Requirements satisfied (per task 12.1)
======================================

    60.4 — Slice 4 seeds five new ``Interim_ADR_Records`` rows on first
            start, one each for the backlog ADR identifiers
            ``ADR-HT-018``, ``ADR-HT-019``, ``ADR-HT-020``,
            ``ADR-HT-021``, and ``ADR-HT-022``, corresponding
            respectively to Gaps G-16 through G-20, each carrying the
            motivating Requirement number, motivating criterion, the
            observable behavior chosen, the recorded date, and the
            backlog ADR identifier, retrievable by backlog ADR identifier.
    60.5 — the additive Interim ADR records cover Gaps G-16 through G-20
            as new rows in the ``Interim_ADR_Records`` registry without
            mutating any Slice 1, Slice 2, or Slice 3 row.

Wiring
======

Task 13.2 calls :func:`seed_outcome_interim_adr` from
:mod:`walking_slice.app` startup, in the same hook that calls
:func:`walking_slice.outcome._persistence.create_outcome_schema` and
:func:`walking_slice.outcome._disclosure.seed_outcome_coverage`. The
function accepts a SQLAlchemy :class:`~sqlalchemy.engine.Connection` so
the seeding participates in the caller's transaction (the production
startup hook opens one ``engine.begin()`` block that runs Slice 4 schema
creation and the Slice 4 seeders together so a partial bootstrap is
rolled back).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.clock import Clock, SystemClock
from walking_slice.interim_adr import InterimAdrSeedRow


__all__ = [
    "OUTCOME_INTERIM_ADR_SEED_ROWS",
    "seed_outcome_interim_adr",
]


# ---------------------------------------------------------------------------
# Row definitions.
#
# Every row is shaped by :class:`walking_slice.interim_adr.InterimAdrSeedRow`
# so this module shares the Slice 1 value object exactly. The ``record_id``
# values continue the ``ad-ws-N`` numbering scheme established by Slice 1
# (``ad-ws-6`` through ``ad-ws-10``), Slice 2 (``ad-ws-15`` through
# ``ad-ws-19``), and Slice 3 (``ad-ws-24`` through ``ad-ws-28``), reusing
# the Slice 4 AD-WS numbers 33..38; ``INSERT OR IGNORE`` against these
# stable keys is what makes repeated seeding idempotent across processes
# and across multiple invocations within one process. Rows are ordered by
# Gap G-16..G-20 / backlog ADR ADR-HT-018..ADR-HT-022.
# ---------------------------------------------------------------------------


# AD-WS-38 — Origin enumeration and source-system authority enumeration as
# registered enumeration columns (closes Gap G-16, input to ADR-HT-018).
# Design §"AD-WS-38"; Requirement 46.1, 46.2, 46.4, 46.7, 58.5.
_AD_WS_38 = InterimAdrSeedRow(
    record_id="ad-ws-38",
    motivating_requirement="Requirement 46.1, 46.2, 46.4, 46.7; Gap G-16",
    motivating_criterion=(
        "origin enumeration and source-system authority enumeration for "
        "Measurement Records"
    ),
    observable_behavior=(
        "every Measurement_Records row carries origin TEXT NOT NULL CHECK "
        "(origin IN ('native','imported')); imported rows additionally carry "
        "source_system_authority TEXT CHECK (source_system_authority IN "
        "('authoritative','replica','projection','index','federation')) per "
        "Principle 5.27; native rows hold source_system_authority and every "
        "other source-system attribute NULL while imported rows require all "
        "source-system attributes non-null (table-level CHECK keyed on "
        "origin); the source-system authority designation is never defaulted "
        "to 'authoritative' — the column is required on the imported request "
        "body and rejected if absent; the chosen origin member set is "
        "{native, imported} and the chosen source-system authority member "
        "set is {authoritative, replica, projection, index, federation}, "
        "encoded as CHECK-constrained enumeration columns keeping native and "
        "imported Records in one Record kind"
    ),
    backlog_adr_id="ADR-HT-018",
)

# AD-WS-33 — Additive ``define_measurement``, ``record_measurement``,
# ``assess_outcome``, ``issue_outcome_review`` authority values (closes
# Gap G-17, input to ADR-HT-019). Design §"AD-WS-33"; Requirement 52.
_AD_WS_33 = InterimAdrSeedRow(
    record_id="ad-ws-33",
    motivating_requirement=(
        "Requirement 52.1, 52.6, 52.7, 52.8, 52.9; Gap G-17"
    ),
    motivating_criterion="additive Slice 4 authority enumeration values",
    observable_behavior=(
        "Authorization_Service._VALID_AUTHORITIES is extended additively "
        "from the Slice 3 eight-value set {view, modify, review, approve, "
        "assign, contribute, accept_milestone, complete} to the twelve-value "
        "set adding 'define_measurement', 'record_measurement', "
        "'assess_outcome', and 'issue_outcome_review'; the "
        "_required_authority mapping routes create.measurement_definition to "
        "'define_measurement', create.measurement_record to "
        "'record_measurement', create.observed_outcome and "
        "create.success_condition_assessment to 'assess_outcome', and "
        "create.outcome_review to 'issue_outcome_review'; the Slice 1 "
        "non-substitution rule is preserved across the cumulative "
        "twelve-value enumeration (Requirements 12.4, 11.6, 32.10, 52)"
    ),
    backlog_adr_id="ADR-HT-019",
)

# AD-WS-34 — Additive Disclosure-policy extension via new coverage rows,
# with per-attribute restriction for imported Measurement Records (closes
# Gap G-18, input to ADR-HT-020). Design §"AD-WS-34"; Requirement 58.
_AD_WS_34 = InterimAdrSeedRow(
    record_id="ad-ws-34",
    motivating_requirement=(
        "Requirement 58.1, 58.2, 58.4, 58.5, 58.6; Gap G-18"
    ),
    motivating_criterion=(
        "additive disclosure-policy extension to Slice 4 node kinds and "
        "per-attribute restriction for imported Measurement Records"
    ),
    observable_behavior=(
        "the slice-default-2026 Disclosure_Policies row is left "
        "byte-equivalent; Slice 4 coverage is added by inserting one "
        "Disclosure_Policy_Coverage row per Slice 4 node kind — "
        "measurement_definition, measurement_definition_revision, "
        "measurement_record, observed_outcome, observed_outcome_revision, "
        "success_condition_assessment_record, and outcome_review_record — "
        "reusing the Slice 2 AD-WS-16 coverage table with no new column or "
        "policy identity; for imported Measurement Records the source-system "
        "attributes (source_system_id, source_system_record_id, "
        "source_system_authority, source_system_retrieval_at, import_at) are "
        "recorded as restricted attributes on the coverage row via a "
        "restricted_attributes_json payload, so a requesting Party lacking "
        "view authority on the imported Measurement Record receives the "
        "redaction marker {kind: 'measurement_record', redacted: true} and "
        "the source-system attributes never leak through partial or summary "
        "representations; walking_slice.disclosure.policy_for resolves each "
        "Slice 4 node_kind to the same DisclosurePolicy already covering "
        "Slices 1-3"
    ),
    backlog_adr_id="ADR-HT-020",
)

# AD-WS-35 — Canonical Relationship Types and semantic-role markers for
# Slice 4 (closes Gap G-19, input to ADR-HT-021). Design §"AD-WS-35";
# Requirements 44, 45, 46, 47, 48, 49.
_AD_WS_35 = InterimAdrSeedRow(
    record_id="ad-ws-35",
    motivating_requirement="Requirement 44, 45, 47, 48, 49; Gap G-19",
    motivating_criterion=(
        "canonical Slice 4 Relationship Types and semantic-role markers"
    ),
    observable_behavior=(
        "Measurement Definition Revision -> Intended Outcome Revision is "
        "'Addresses' with semantic_role NULL; Measurement Record -> "
        "Measurement Definition Revision is 'Cites' with semantic_role "
        "'measurement_basis'; Observed Outcome Revision -> Intended Outcome "
        "Revision is 'Addresses' with semantic_role NULL; Observed Outcome "
        "Revision -> cited Measurement Record is 'Cites' with semantic_role "
        "'observation_basis'; Success-Condition Assessment Record -> "
        "Intended Outcome Revision is 'Addresses' with semantic_role NULL; "
        "Success-Condition Assessment Record -> sourced Observed Outcome "
        "Revision is 'Cites' with semantic_role 'assessment_basis'; Outcome "
        "Review Record -> Intended Outcome Revision is 'Addresses' with "
        "semantic_role NULL; Outcome Review Record -> cited Success-Condition "
        "Assessment Record is 'Cites' with semantic_role 'review_assessment'; "
        "Outcome Review Record -> cited Completion Record is 'Cites' with "
        "semantic_role 'review_completion'; Outcome Review Record -> cited "
        "produced Deliverable Revision is 'Cites' with semantic_role "
        "'review_deliverable'; all markers are written into the existing "
        "Slice 2 AD-WS-19 Relationships.semantic_role column with no new "
        "column or Relationship Type introduced"
    ),
    backlog_adr_id="ADR-HT-021",
)

# AD-WS-36 / AD-WS-37 — Slice 4 Records/Resources are append-only with an
# explicit Observed Outcome Revision predecessor chain and no supersession
# path (AD-WS-36), held in per-kind insert-only tables with UPDATE/DELETE
# triggers plus seven new Identifier_Registry.resource_kind values
# (AD-WS-37) (closes Gap G-20, input to ADR-HT-022). Both decisions record
# the chosen persistence representation for the single backlog ADR
# ADR-HT-022. Design §"AD-WS-36", §"AD-WS-37"; Requirements 43.8, 44.3,
# 44.7, 45.6, 47.3, 47.7, 48.6, 49.3, 49.7, 61 §4.
_AD_WS_36 = InterimAdrSeedRow(
    record_id="ad-ws-36",
    motivating_requirement=(
        "Requirement 43.8, 44.3, 44.7, 45.6, 47.3, 47.7, 48.6, 49.3, 49.7; "
        "Gap G-20"
    ),
    motivating_criterion=(
        "append-only persistence representation for Slice 4 "
        "Resources/Revisions/Records"
    ),
    observable_behavior=(
        "every Slice 4 Resource (Measurement_Definitions, Observed_Outcomes), "
        "every Revision (Measurement_Definition_Revisions, "
        "Observed_Outcome_Revisions), and every Immutable Record "
        "(Measurement_Records, Success_Condition_Assessment_Records, "
        "Outcome_Review_Records) is held in its own SQLite table, all "
        "insert-only with UPDATE and DELETE triggers that reject every "
        "mutation (Slice 1 AD-WS-4 / Slice 2 AD-WS-19 / Slice 3 AD-WS-27 "
        "pattern); Observed Outcome evolution is an append-only predecessor "
        "chain where each Observed_Outcome_Revisions row carries "
        "predecessor_revision_id (NULL on the initial Revision) and the most "
        "recent Revision is the unique one not named as any other Revision's "
        "predecessor; no supersession path is implemented in this slice; no "
        "new column is added to any Slice 1/2/3 table; Slice 4 emits seven "
        "new Identifier_Registry.resource_kind values {measurement_definition, "
        "measurement_definition_revision, measurement_record, "
        "observed_outcome, observed_outcome_revision, "
        "success_condition_assessment_record, outcome_review_record} on the "
        "existing column, providing the seven-disjoint-roles assertion "
        "required by Requirement 43.8"
    ),
    backlog_adr_id="ADR-HT-022",
)


# Public, ordered tuple — rows are listed in Gap G-16..G-20 order, which is
# the backlog-ADR order ADR-HT-018..ADR-HT-022 that Property 60 iterates and
# the order this module's docstring documents.
OUTCOME_INTERIM_ADR_SEED_ROWS: Final[tuple[InterimAdrSeedRow, ...]] = (
    _AD_WS_38,
    _AD_WS_33,
    _AD_WS_34,
    _AD_WS_35,
    _AD_WS_36,
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


def seed_outcome_interim_adr(
    connection: Connection,
    *,
    clock: Clock | None = None,
) -> None:
    """Insert one ``Interim_ADR_Records`` row per Slice 4 Gap G-16..G-20.

    Every row carries the motivating Requirement number, the motivating
    criterion, the observable behavior chosen by Slice 4, the recorded
    date, and the backlog ADR identifier reserved for the future
    replacement (``ADR-HT-018`` through ``ADR-HT-022``). The
    ``resolved_by_adr_id`` and ``resolved_at`` columns are left NULL
    because none of the Slice 4 backlog ADRs has been ``Accepted`` yet —
    the slice ships the interim behavior in advance of the ADR, exactly
    as Slice 1 Requirement 16.3 and Slice 4 Requirement 60.3 require.

    The function is idempotent: every insert uses ``INSERT OR IGNORE``
    against the stable record-id primary keys ``ad-ws-38``, ``ad-ws-33``,
    ``ad-ws-34``, ``ad-ws-35``, and ``ad-ws-36``, so repeated calls
    against the same database leave the table byte-equivalent after the
    first successful invocation. This is the behavior
    :mod:`walking_slice.app` relies on at startup (task 13.2) — the
    Outcome_Service is mounted by every process that starts the slice, so
    the Interim ADR seed runs once per process and must be safe to run
    against an already-seeded database.

    The function accepts a SQLAlchemy
    :class:`~sqlalchemy.engine.Connection` (rather than an
    :class:`~sqlalchemy.engine.Engine`) so the seeding participates in
    the caller's transaction. The production startup hook opens one
    ``engine.begin()`` block that runs Slice 4 schema creation, the
    Slice 4 ``Disclosure_Policy_Coverage`` seed, and this Interim ADR
    seed together so a partial bootstrap is rolled back. This signature
    mirrors
    :func:`walking_slice.outcome._disclosure.seed_outcome_coverage` and
    :func:`walking_slice.execution._interim_adr.seed_execution_interim_adr`.

    The Slice 1 + Slice 2 + Slice 3 ``Interim_ADR_Records`` rows are not
    touched by this function (Requirement 60.5 — additive only). The
    startup hook is responsible for invoking the Slice 1, Slice 2, and
    Slice 3 seeders before or alongside this one so all twenty rows are
    present after bootstrap.

    Args:
        connection: A SQLAlchemy :class:`~sqlalchemy.engine.Connection`
            with an active transaction. The ``Interim_ADR_Records``
            table MUST already have been created by
            :func:`walking_slice.persistence.create_schema`; this
            function does not create the table itself.
        clock: Optional :class:`~walking_slice.clock.Clock`. When
            omitted, a :class:`~walking_slice.clock.SystemClock` is
            constructed and used to read the current UTC time. The
            ``recorded_at`` column on each inserted row is populated with
            ``clock.now()`` rendered as an ISO-8601 string with
            millisecond precision, matching design §"Cross-Cutting
            Concerns" (*Time*). Because of ``INSERT OR IGNORE``, the
            ``recorded_at`` of the very first successful call is the
            value preserved in the database; subsequent calls (with a
            later ``clock.now()``) do not overwrite it.
    """
    active_clock = clock if clock is not None else SystemClock()
    recorded_at = active_clock.now().isoformat(timespec="milliseconds")

    for row in OUTCOME_INTERIM_ADR_SEED_ROWS:
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
