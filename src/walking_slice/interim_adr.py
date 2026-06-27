"""Startup seeding for ``Interim_ADR_Records`` (Requirement 16.3).

Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"Architectural
Decisions" (AD-WS-6 through AD-WS-10) and §"Table-by-Table Specification —
``Interim_ADR_Records``"; ``.kiro/specs/first-walking-slice/requirements.md``
§"Identified gaps" (G-1 through G-5) and §16.3.

Responsibility
==============

This module exposes a single :func:`seed` function that, on application
startup, populates one ``Interim_ADR_Records`` row per Gap G-1..G-5 so the
slice satisfies Requirement 16.3:

    WHERE the slice implements an interim behavior in advance of a backlog
    ADR being ``Accepted``, THE project SHALL record, for each such interim
    behavior, the motivating Requirement number, the motivating criterion
    number, the observable behavior chosen, the recorded date of the choice,
    and the backlog ADR identifier, and SHALL make the record retrievable
    by backlog ADR identifier.

Each row maps a slice-local architectural decision (AD-WS-N) to the backlog
ADR that will eventually resolve the gap:

==== ======== =====================  ==============================================
Gap  AD-WS    Backlog ADR            Subject
==== ======== =====================  ==============================================
G-1  AD-WS-6  ``ADR-HT-003``         Content Region anchoring (byte offsets)
G-2  AD-WS-7  ``ADR-HT-004``         Relationship lifecycle (immutable assertions)
G-3  AD-WS-8  ``ADR-HT-005``         Backlink indexing (on-demand reverse scan)
G-4  AD-WS-9  ``ADR-HT-008``         Default Completeness Disclosure policy
G-5  AD-WS-10 ``ADR-HT-002``         Authority-basis enumeration
==== ======== =====================  ==============================================

Idempotence
===========

:func:`seed` uses ``INSERT OR IGNORE`` against a fixed primary key per row
(``ad-ws-6`` through ``ad-ws-10``), so repeated startup calls — and the
lazy seed for AD-WS-6 in :mod:`walking_slice.evidence` — never produce
duplicates. The primary key shared with ``evidence.py`` means whichever code
path inserts the AD-WS-6 row first wins; subsequent attempts silently
no-op while leaving the originally recorded date in place.

Requirements satisfied
======================

    16.3 — Interim ADR records are retrievable by backlog ADR identifier.

Wiring
======

Task 15.2 calls :func:`seed` from :mod:`walking_slice.app` startup, in the
same hook that calls :func:`walking_slice.persistence.create_schema`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import Clock, SystemClock


__all__ = ["seed", "INTERIM_ADR_SEED_ROWS", "InterimAdrSeedRow"]


# ---------------------------------------------------------------------------
# Row definitions.
#
# Each :class:`InterimAdrSeedRow` is the canonical value shape used by both
# this module's :func:`seed` and the lazy seed in :mod:`walking_slice.evidence`
# (which still owns the AD-WS-6 row independently so Property 15 is satisfied
# the moment any Region Occurrence is recorded, even if :func:`seed` has not
# yet run). The ``record_id`` is intentionally a stable string (not a
# UUIDv7) so ``INSERT OR IGNORE`` is idempotent across processes and across
# repeated invocations within one process.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterimAdrSeedRow:
    """Canonical contents of one ``Interim_ADR_Records`` row.

    Attributes:
        record_id: Stable primary key (e.g. ``"ad-ws-6"``).
        motivating_requirement: Free-form Requirement reference identifying
            the acceptance criterion that motivated the interim decision
            (Requirement 16.3 — *motivating Requirement number*).
        motivating_criterion: Short name of the criterion (Requirement 16.3
            — *motivating criterion number*). Free-form text; the slice does
            not enforce a vocabulary.
        observable_behavior: One-line description of the behavior implemented
            in the slice (Requirement 16.3 — *observable behavior chosen*).
        backlog_adr_id: The backlog ADR identifier this row attaches to.
            Property 15 queries the table by this column.
    """

    record_id: str
    motivating_requirement: str
    motivating_criterion: str
    observable_behavior: str
    backlog_adr_id: str


# AD-WS-6 — Interim Content Region anchoring (input to ADR-HT-003).
# Values are intentionally byte-equivalent to the ``_AD_WS_6_*`` constants
# in :mod:`walking_slice.evidence` so the lazy seed there and the startup
# seed here cannot diverge on the contents of the shared row.
_AD_WS_6 = InterimAdrSeedRow(
    record_id="ad-ws-6",
    motivating_requirement="Requirement 3.1, 3.2; Gap G-1",
    motivating_criterion="byte-offset anchoring",
    observable_behavior=(
        "spans are validated against the Document Revision's content_bytes length"
    ),
    backlog_adr_id="ADR-HT-003",
)

# AD-WS-7 — Interim Relationship lifecycle: immutable assertions (input to
# ADR-HT-004). Closes Gap G-2 per requirements.md §"Identified gaps".
_AD_WS_7 = InterimAdrSeedRow(
    record_id="ad-ws-7",
    motivating_requirement="Requirement 4.2, 4.4; Gap G-2",
    motivating_criterion="Relationship lifecycle",
    observable_behavior=(
        "Relationships of type Supports, Contradicts, Derived From, and "
        "Addresses are immutable assertions; supersession is represented by a "
        "later Relationship of type Supersedes"
    ),
    backlog_adr_id="ADR-HT-004",
)

# AD-WS-8 — Interim backlink indexing: on-demand reverse scan with a covering
# composite index (input to ADR-HT-005). Closes Gap G-3.
_AD_WS_8 = InterimAdrSeedRow(
    record_id="ad-ws-8",
    motivating_requirement="Requirement 8.1; Gap G-3",
    motivating_criterion="backlink indexing approach",
    observable_behavior=(
        "backlink queries scan the Relationships table filtered by target_id "
        "and target_revision_id, using the composite index "
        "(target_id, target_revision_id, relationship_type, recorded_at); "
        "authorization filtering is applied after retrieval but before "
        "pagination"
    ),
    backlog_adr_id="ADR-HT-005",
)

# AD-WS-9 — Default Completeness Disclosure policy (closes Gap G-4, input to
# ADR-HT-008).
_AD_WS_9 = InterimAdrSeedRow(
    record_id="ad-ws-9",
    motivating_requirement="Requirement 10.5, 11.3; Gap G-4",
    motivating_criterion="default Completeness Disclosure policy",
    observable_behavior=(
        "slice ships a single default Completeness Disclosure policy named "
        "slice-default-2026: restricted nodes are replaced with a "
        "redaction_marker {kind, redacted: true}; unavailable, stale, and "
        "unresolved nodes return a gap descriptor containing only stage, "
        "category, and (if the next reachable node is visible) the next "
        "reachable node's identity; restricted-vs-nonexistent observability "
        "is normalized"
    ),
    backlog_adr_id="ADR-HT-008",
)

# AD-WS-10 — Authority-basis enumeration (closes Gap G-5, input to
# ADR-HT-002). The backlog identifier is assigned by elimination from
# Property 15's enumerated set {ADR-HT-002, ADR-HT-003, ADR-HT-004,
# ADR-HT-005, ADR-HT-008}: G-1..G-4 consume the other four identifiers.
_AD_WS_10 = InterimAdrSeedRow(
    record_id="ad-ws-10",
    motivating_requirement="Requirement 6.2, 12.1; Gap G-5",
    motivating_criterion="authority-basis enumeration",
    observable_behavior=(
        "Decisions.authority_basis and Audit denial records accept exactly "
        "one of {role-grant-id, scope-id, delegation-chain-id}"
    ),
    backlog_adr_id="ADR-HT-002",
)


# Public, ordered tuple — the row order matches Gap G-1..G-5 / AD-WS-6..10,
# which is the order Property 15's enumerated identifier set is iterated
# in design §"Correctness Properties → Property 15".
INTERIM_ADR_SEED_ROWS: Final[tuple[InterimAdrSeedRow, ...]] = (
    _AD_WS_6,
    _AD_WS_7,
    _AD_WS_8,
    _AD_WS_9,
    _AD_WS_10,
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


def seed(engine: Engine, *, clock: Clock | None = None) -> None:
    """Insert one ``Interim_ADR_Records`` row per Gap G-1..G-5.

    The function is idempotent: every row uses ``INSERT OR IGNORE`` against
    a stable primary key, so repeated invocations against the same database
    leave the table byte-equivalent after the first successful call. This is
    the behavior :mod:`walking_slice.app` relies on at startup (task 15.2)
    and the behavior tests rely on when calling :func:`seed` more than once
    in a fixture.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database whose
            schema has already been created via
            :func:`walking_slice.persistence.create_schema`. The function
            does not create the schema itself; callers MUST run
            ``create_schema`` first (task 1.3) so the
            ``Interim_ADR_Records`` table exists.
        clock: Optional :class:`~walking_slice.clock.Clock`. When omitted,
            a :class:`~walking_slice.clock.SystemClock` is constructed and
            used to read the current UTC time. The ``recorded_at`` column is
            populated with ``clock.now()`` rendered as an ISO-8601 string
            with millisecond precision, matching design §"Cross-Cutting
            Concerns" (*Time*). Because of ``INSERT OR IGNORE``, the
            ``recorded_at`` of the very first successful call is the value
            preserved in the database; subsequent calls (with a later
            ``clock.now()``) do not overwrite it.
    """
    active_clock = clock if clock is not None else SystemClock()
    recorded_at = active_clock.now().isoformat(timespec="milliseconds")

    # A single ``BEGIN IMMEDIATE`` transaction wraps all five inserts so a
    # mid-seed crash either persists every row or none, matching the
    # whole-transaction posture of ``create_schema`` (task 1.3).
    with engine.begin() as conn:
        for row in INTERIM_ADR_SEED_ROWS:
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
