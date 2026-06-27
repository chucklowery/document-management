"""Additive seeding of ``Disclosure_Policy_Coverage`` for Slice 4 node kinds.

Design reference: ``.kiro/specs/fourth-walking-slice/design.md`` §"AD-WS-34 —
Additive Disclosure-policy extension via new coverage rows, with per-attribute
restriction for imported Measurement Records (closes Gap G-18; backlog
``ADR-HT-020``)".

Responsibility of this module (task 1.3)
=========================================

This module is the additive surface AD-WS-34 mandates: instead of mutating
the seeded ``slice-default-2026`` row in ``Disclosure_Policies`` or
introducing a separate disclosure policy, the slice extends coverage by
inserting one ``Disclosure_Policy_Coverage`` row per Slice 4 node kind. The
existing Slice 1 ``Disclosure_Policies`` row identity, rule set, and
``effective_start`` are unchanged; the existing Slice 2 + Slice 3
``Disclosure_Policy_Coverage`` rows are unchanged (Requirement 60.2 — Slice
1, Slice 2, and Slice 3 non-modification; Requirement 58.1 — the additive
extension does not alter the policy identity or the prior rule scope).

The seeded coverage rows let the existing ``slice-default-2026`` rule set
(redaction-marker, gap-descriptor, restricted-vs-nonexistent normalization
— per AD-WS-9 as extended by Slice 2 Requirement 17 and Slice 3 Requirement
38) apply uniformly to every Slice 4 node kind that the
Provenance_Navigator, Authorization_Service, and Outcome_Service may
surface:

- ``measurement_definition``           / ``measurement_definition_revision``
- ``measurement_record``
- ``observed_outcome``                  / ``observed_outcome_revision``
- ``success_condition_assessment_record``
- ``outcome_review_record``

Each row records ``policy_id = 'slice-default-2026'``, the recorded date,
and ``backlog_adr_id = 'ADR-HT-020'`` (the backlog ADR reserved by Gap
G-18 for the future ADR that formalizes the additive policy-extension
surface — and, in particular, the per-attribute restriction for imported
Measurement Records). The ``Disclosure_Policy_Coverage`` table is
insert-only after seeding (the ``UPDATE`` / ``DELETE`` triggers installed
by :mod:`walking_slice.planning._persistence` reject mutation), so the
seed uses ``INSERT OR IGNORE`` against the composite primary key
``(policy_id, node_kind)`` and is idempotent across repeated calls.

Per-attribute restriction for imported Measurement Records (AD-WS-34)
=====================================================================

The ``measurement_record`` coverage row — and only that row — carries a
``restricted_attributes_json`` payload naming the imported source-system
attributes as restricted: ``source_system_id``,
``source_system_record_id``, ``source_system_authority``,
``source_system_retrieval_at``, and ``import_at`` (Requirement 58.5). When
the requesting Party lacks view authority on an imported Measurement
Record, the whole Record is replaced with the redaction marker
``{"kind": "measurement_record", "redacted": true}`` and the source-system
attributes never leak through partial or summary representations.

The ``restricted_attributes_json`` column is an *additive field on the
coverage row* (AD-WS-34): the coverage-row schema is the one Slice 2
introduced, extended with one nullable ``TEXT`` column. The column is
added idempotently by :func:`seed_outcome_coverage` if it is not already
present, so the seeder is self-contained and does not depend on the order
in which the Slice 4 schema and disclosure seeders run. Adding a nullable
column is additive — every existing Slice 1 + Slice 2 + Slice 3 coverage
row keeps its values unchanged (the new column reads ``NULL`` for them),
satisfying Requirement 60.2.

The Slice 1 + Slice 2 + Slice 3 lookup function
:func:`walking_slice.disclosure.policy_for` already consults
``Disclosure_Policy_Coverage`` before falling back to the baseline
``slice-default-2026`` policy row (see AD-WS-23 / Slice 2 §"Disclosure
policy coverage is enforced by lookup"), so seeding these rows is
sufficient to extend coverage to the Slice 4 node kinds without any change
to the lookup code path.

Requirements satisfied (per task 1.3)
=====================================

    58.1 — every Slice 4 node kind is covered by an additive extension of
           ``slice-default-2026`` rather than a separate policy.
    58.2 — restricted Slice 4 nodes are replaced with the AD-WS-9
           redaction marker (the rule set is inherited from the existing
           policy row; coverage rows opt the node kind in).
    58.3 — Slice 4 nodes in unavailable/stale/unresolved categories
           return the AD-WS-9 gap descriptor (same inheritance path).
    58.4 — Slice 4 restricted-vs-nonexistent observability is normalized
           to the Slice 1 + Slice 2 + Slice 3 behavior (same inheritance
           path).
    58.5 — the imported-Measurement-Record source-system attributes are
           named as restricted attributes on the ``measurement_record``
           coverage row, and an unauthorized requester sees the whole
           Record replaced with ``{"kind": "measurement_record",
           "redacted": true}``.
    60.2 — the only Slice 1 + Slice 2 + Slice 3 touch is the additive
           sibling-table rows (and the additive nullable column); the
           ``Disclosure_Policies`` row and the existing Slice 2 + Slice 3
           ``Disclosure_Policy_Coverage`` rows are not modified.

Wiring
======

The application startup hook calls :func:`seed_outcome_coverage` in the
same ``engine.begin()`` block that creates the Slice 4 schema and seeds
the Slice 4 Interim ADR rows, after the Slice 2 schema creation
(``create_planning_schema``) and baseline policy seed
(``walking_slice.disclosure.seed``) so the ``Disclosure_Policy_Coverage``
table and the ``slice-default-2026`` ``Disclosure_Policies`` row both
exist. The function accepts a SQLAlchemy
:class:`~sqlalchemy.engine.Connection` so the seeding participates in the
caller's transaction (a partial bootstrap is rolled back together).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.clock import Clock, SystemClock
from walking_slice.disclosure import SLICE_DEFAULT_POLICY_ID


__all__ = [
    "OUTCOME_NODE_KINDS",
    "OUTCOME_COVERAGE_BACKLOG_ADR_ID",
    "MEASUREMENT_RECORD_NODE_KIND",
    "MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES",
    "MEASUREMENT_RECORD_REDACTION_MARKER",
    "MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES_JSON",
    "OutcomeCoverageSeedRow",
    "seed_outcome_coverage",
]


# ---------------------------------------------------------------------------
# Public constants.
# ---------------------------------------------------------------------------


OUTCOME_COVERAGE_BACKLOG_ADR_ID: Final[str] = "ADR-HT-020"
"""Backlog ADR identifier recorded on every Slice 4 coverage row (AD-WS-34).

Gap G-18 ("additive policy extension with per-attribute restriction for
imported Measurement Records") reserves ``ADR-HT-020`` for the future ADR
that formalizes the additive policy-extension surface for Slice 4. Every
coverage row carries this identifier so :mod:`walking_slice.outcome._interim_adr`
and the Slice 4 non-modification property test can join
``Disclosure_Policy_Coverage`` back to the corresponding
``Interim_ADR_Records`` row by ``backlog_adr_id``.
"""


MEASUREMENT_RECORD_NODE_KIND: Final[str] = "measurement_record"
"""The single Slice 4 node kind carrying the restricted-attributes payload."""


# Ordered tuple of every Slice 4 node kind that needs disclosure coverage.
#
# The order follows the dependency graph in design §"Components and
# Interfaces": Measurement Definition → Measurement Record → Observed
# Outcome → Success-Condition Assessment → Outcome Review. Tests and the
# Slice 4 non-modification property iterate this tuple in declaration
# order so a change to the set produces a single localized diff rather
# than a scattered set of test edits.
OUTCOME_NODE_KINDS: Final[tuple[str, ...]] = (
    "measurement_definition",
    "measurement_definition_revision",
    "measurement_record",
    "observed_outcome",
    "observed_outcome_revision",
    "success_condition_assessment_record",
    "outcome_review_record",
)
"""Every Slice 4 node kind that receives a ``Disclosure_Policy_Coverage`` row.

Sourced verbatim from task 1.3 and Requirement 58.1. The tuple is a
``Final`` so callers (tests, the Slice 4 non-modification property, the
Provenance_Navigator's ``policy_for`` lookups) can rely on its membership
being stable across imports. The values match the
``Identifier_Registry.resource_kind`` tags the Slice 4 services use
(design §"Persistence Invariants Summary" rule 4 / AD-WS-37 / Requirement
43.8), so the disclosure lookup and the identifier registry agree on the
same seven-kind enumeration.
"""


# The imported source-system attributes that are restricted on the
# ``measurement_record`` coverage row (AD-WS-34 / Requirement 58.5). When
# the requesting Party lacks view authority on an imported Measurement
# Record, none of these attributes may appear in any representation.
MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES: Final[tuple[str, ...]] = (
    "source_system_id",
    "source_system_record_id",
    "source_system_authority",
    "source_system_retrieval_at",
    "import_at",
)
"""Imported-source-system attributes restricted on the measurement_record row."""


# The whole-Record replacement an unauthorized requester sees instead of
# an imported Measurement Record (AD-WS-34 / Requirement 58.5). The
# response shaper substitutes this marker rather than emitting any
# partial or summary representation that could leak the restricted
# source-system attributes.
MEASUREMENT_RECORD_REDACTION_MARKER: Final[dict[str, object]] = {
    "kind": "measurement_record",
    "redacted": True,
}
"""Redaction marker that replaces a restricted imported Measurement Record."""


# The ``restricted_attributes_json`` payload persisted on the
# ``measurement_record`` coverage row. ``sort_keys=True`` makes the
# serialized text deterministic so repeated seeds are byte-equivalent and
# tests can assert an exact string.
MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES_JSON: Final[str] = json.dumps(
    {
        "restricted_attributes": list(MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES),
        "redaction_marker": MEASUREMENT_RECORD_REDACTION_MARKER,
    },
    sort_keys=True,
    separators=(",", ":"),
)
"""Canonical JSON payload stored on the measurement_record coverage row."""


# ---------------------------------------------------------------------------
# Row shape.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeCoverageSeedRow:
    """Canonical contents of one Slice 4 ``Disclosure_Policy_Coverage`` row.

    Attributes:
        policy_id: The ``Disclosure_Policies`` row the coverage extends.
            Always :data:`walking_slice.disclosure.SLICE_DEFAULT_POLICY_ID`
            for the Slice 4 seed.
        node_kind: The covered node kind, drawn from
            :data:`OUTCOME_NODE_KINDS`.
        backlog_adr_id: The backlog ADR identifier reserved for the
            future replacement (Gap G-18 → ``ADR-HT-020``).
        restricted_attributes_json: The per-attribute restriction
            payload (AD-WS-34). Populated only for the
            ``measurement_record`` node kind; ``None`` for every other
            Slice 4 node kind.
    """

    policy_id: str
    node_kind: str
    backlog_adr_id: str
    restricted_attributes_json: str | None


# ---------------------------------------------------------------------------
# Seed entry point.
# ---------------------------------------------------------------------------


_INSERT_STATEMENT: Final[str] = """
    INSERT OR IGNORE INTO Disclosure_Policy_Coverage (
        policy_id,
        node_kind,
        recorded_at,
        backlog_adr_id,
        restricted_attributes_json
    ) VALUES (
        :policy_id,
        :node_kind,
        :recorded_at,
        :backlog_adr_id,
        :restricted_attributes_json
    )
"""


def _ensure_restricted_attributes_column(connection: Connection) -> None:
    """Idempotently add the additive ``restricted_attributes_json`` column.

    AD-WS-34 records the per-attribute restriction "on the coverage row
    as a ``restricted_attributes_json`` payload — an additive field on
    the coverage row". The Slice 2 ``Disclosure_Policy_Coverage`` table
    (created by ``create_planning_schema``) does not declare this
    column, so it is added here as a nullable ``TEXT`` column.

    Adding a nullable column is additive: every existing Slice 1 + Slice
    2 + Slice 3 coverage row keeps its stored values unchanged (the new
    column reads ``NULL`` for them), so Requirement 60.2 is preserved.
    The check is performed with ``PRAGMA table_info`` because SQLite's
    ``ALTER TABLE ... ADD COLUMN`` does not support ``IF NOT EXISTS``;
    skipping the ``ALTER`` when the column already exists keeps the
    seeder idempotent across repeated startups and safe to run alongside
    a future schema task that may declare the column directly.
    """
    existing_columns = {
        row[1]
        for row in connection.execute(
            text("PRAGMA table_info(Disclosure_Policy_Coverage)")
        ).all()
    }
    if "restricted_attributes_json" not in existing_columns:
        connection.execute(
            text(
                "ALTER TABLE Disclosure_Policy_Coverage "
                "ADD COLUMN restricted_attributes_json TEXT"
            )
        )


def seed_outcome_coverage(
    connection: Connection,
    *,
    clock: Clock | None = None,
) -> None:
    """Insert one ``Disclosure_Policy_Coverage`` row per Slice 4 node kind.

    Every row carries ``policy_id = 'slice-default-2026'``, the recorded
    date, and ``backlog_adr_id = 'ADR-HT-020'``. The ``measurement_record``
    row — and only that row — additionally carries the AD-WS-34
    ``restricted_attributes_json`` payload naming the imported
    source-system attributes as restricted (Requirement 58.5); every
    other Slice 4 row stores ``NULL`` for that column.

    The function is idempotent: every insert uses ``INSERT OR IGNORE``
    against the composite primary key ``(policy_id, node_kind)``, so
    repeated calls against the same database leave the table
    byte-equivalent after the first successful invocation. The additive
    ``restricted_attributes_json`` column is created on first call (and
    skipped on subsequent calls), so the seeder is safe to run against
    an already-seeded database — the behavior :mod:`walking_slice.app`
    relies on at startup.

    The function accepts a SQLAlchemy
    :class:`~sqlalchemy.engine.Connection` (rather than an
    :class:`~sqlalchemy.engine.Engine`) so the seeding participates in
    the caller's transaction. The production startup hook opens one
    ``engine.begin()`` block that runs Slice 4 schema creation, this
    coverage seed, and the Slice 4 Interim ADR seed together so a
    partial bootstrap is rolled back.

    The Slice 1 + Slice 2 + Slice 3 row contents — the
    ``slice-default-2026`` row in ``Disclosure_Policies`` and the
    twenty-one prior coverage rows — are not touched (Requirement 60.2).
    After this seeder runs, :func:`walking_slice.disclosure.policy_for`
    resolves each Slice 4 ``node_kind`` to the same
    :class:`~walking_slice.disclosure.DisclosurePolicy` that already
    covers Slice 1, Slice 2, and Slice 3 (AD-WS-34 / AD-WS-23 — one
    cohesive disclosure contract across all four slices).

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
    _ensure_restricted_attributes_column(connection)

    active_clock = clock if clock is not None else SystemClock()
    recorded_at = active_clock.now().isoformat(timespec="milliseconds")

    for node_kind in OUTCOME_NODE_KINDS:
        restricted_attributes_json = (
            MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES_JSON
            if node_kind == MEASUREMENT_RECORD_NODE_KIND
            else None
        )
        connection.execute(
            text(_INSERT_STATEMENT),
            {
                "policy_id": SLICE_DEFAULT_POLICY_ID,
                "node_kind": node_kind,
                "recorded_at": recorded_at,
                "backlog_adr_id": OUTCOME_COVERAGE_BACKLOG_ADR_ID,
                "restricted_attributes_json": restricted_attributes_json,
            },
        )
