# Feature: second-walking-slice, Property 26: Slice 2 Interim ADR records retrievability
"""Property 26 — Slice 2 Interim ADR records retrievability (task 16.11).

**Property 26: Slice 2 Interim ADR records retrievability**

*For all* backlog ADR identifiers in the enumerated Slice 2 set
``{ADR-HT-006, ADR-HT-009, ADR-HT-010, ADR-HT-011, ADR-HT-012}``,
querying ``Interim_ADR_Records`` by backlog ADR identifier returns at
least one row whose motivating Requirement number, motivating
criterion, observable behavior, recorded date, and backlog ADR
identifier match the AD-WS-15..AD-WS-19 design decisions. These rows
are byte-equivalent at every observation point after their initial
seeding.

**Validates: Requirements 19.5, 21.3**

Strategy
========

Property 26 is the Slice 2 analogue of Property 15 (Slice 1 Interim
ADR retrievability, task 13.3) extended with a byte-equivalence
clause. Each Hypothesis case:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state
   cannot contaminate query results (design §"Testing Strategy" —
   per-case database isolation). Only the Slice 1 schema is required
   because ``Interim_ADR_Records`` is a Slice 1 table; the Slice 2
   seeder simply appends additive rows to it (Requirement 19.5).
2. Seeds the table once with
   :func:`walking_slice.planning._interim_adr.seed_planning_interim_adr`,
   which first delegates to the Slice 1 seeder (so Slice 1 rows are
   present too) and then inserts the five Slice 2 rows for Gaps
   G-6..G-10. The clock is pinned to a per-case :class:`FixedClock`
   so ``recorded_at`` is deterministic across cases and shrinking.
3. Draws a list of *probes* — backlog ADR identifiers the test will
   query the table by — mixing known Slice 2 identifiers from
   ``{ADR-HT-006, ADR-HT-009, ADR-HT-010, ADR-HT-011, ADR-HT-012}``
   with arbitrary text. The arbitrary branch exercises the
   "unrecognized identifier returns zero rows" path without coupling
   the strategy to a hand-picked list of "obviously unknown"
   identifiers. The full Slice 2 set is also probed unconditionally
   on every case so the "at least one row exists for every
   enumerated Slice 2 identifier" half of the property is exercised
   on every case rather than only when Hypothesis happens to draw a
   known identifier into ``probes``.
4. For each Slice 2 backlog ADR identifier, asserts the four
   non-time fields named by Requirement 16.3 / 21.3 —
   ``motivating_requirement``, ``motivating_criterion``,
   ``observable_behavior``, ``backlog_adr_id`` — and the
   ``recorded_at`` ISO-8601 column are byte-equivalent to the
   corresponding :class:`InterimAdrSeedRow` from
   :data:`PLANNING_INTERIM_ADR_SEED_ROWS`. This pins the "row
   identifying the motivating Requirement number, motivating
   criterion, observable behavior, recorded date, and backlog ADR
   identifier" clause.
5. Re-reads the same backlog ADR identifier multiple times within
   the same engine without intervening writes, and asserts every
   subsequent observation is byte-equivalent to the first. This is
   the "byte-equivalent at every observation point" clause for
   passive observations.
6. Re-invokes :func:`seed_planning_interim_adr` with a *later*
   :class:`FixedClock` and asserts every Slice 2 row is still
   byte-equivalent to the original snapshot. ``INSERT OR IGNORE``
   against the stable ``record_id`` primary key (``ad-ws-15``
   through ``ad-ws-19``) must preserve the originally recorded
   instant; this is the "byte-equivalent across observation points
   that span an additional seed call" clause.
7. For arbitrary unknown probes, asserts the database returns zero
   rows — set-equal to the empty subset of
   :data:`PLANNING_INTERIM_ADR_SEED_ROWS` whose ``backlog_adr_id``
   matches the probe. This pins the negative branch of the
   retrievability property (no leakage of Slice 2 rows under a
   different identifier).

The expected row set is derived independently from the
:data:`PLANNING_INTERIM_ADR_SEED_ROWS` module constant rather than by
re-running the seeder during assertion: a seed-side bug that misroutes
a row to the wrong backlog ADR identifier, or a query-side bug that
leaks rows through the ``backlog_adr_id`` filter, both surface as
property violations rather than being masked by an expectation derived
from the same code path as the implementation.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.interim_adr import InterimAdrSeedRow
from walking_slice.persistence import create_schema
from walking_slice.planning._interim_adr import (
    PLANNING_INTERIM_ADR_SEED_ROWS,
    seed_planning_interim_adr,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Constants derived directly from the spec wording.
#
# ``_SLICE2_BACKLOG_IDS`` is the five-element set Property 26 enumerates
# (task 16.11; design §"Correctness Properties → Property 26"). It is
# defined as a frozenset to make set-equality assertions order-free.
# ``_FIXED_NOW`` pins the seed clock so ``recorded_at`` is deterministic
# across cases and shrinking — Requirement 21.3 names "recorded date of
# the choice" without constraining its exact value, so a fixed instant
# is the simplest way to make the assertion exact while still exercising
# the byte-equivalence-across-seed-calls clause (a *later* fixed instant
# is used for the second seed call).
# ---------------------------------------------------------------------------


_SLICE2_BACKLOG_IDS: Final[frozenset[str]] = frozenset(
    {
        "ADR-HT-006",
        "ADR-HT-009",
        "ADR-HT-010",
        "ADR-HT-011",
        "ADR-HT-012",
    }
)


_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FIXED_NOW_ISO: Final[str] = _FIXED_NOW.isoformat(timespec="milliseconds")

# The second seed call uses a later instant so a regression that
# overwrites the originally recorded date (rather than honoring
# ``INSERT OR IGNORE``) surfaces as a byte-equivalence violation.
_LATER_NOW: Final[datetime] = _FIXED_NOW + timedelta(days=30)


# Number of repeated reads per probed identifier used to exercise the
# byte-equivalence-across-passive-observations clause. Five reads is
# enough to surface any read-side staleness (a single stale read in the
# repeat loop fails the property) while staying well inside the
# 2000 ms case deadline.
_REPEAT_READS: Final[int] = 5


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# A *probe* is a backlog ADR identifier the test will query the table
# by. The strategy mixes known Slice 2 identifiers and arbitrary text so
# each Hypothesis case exercises both the "complete set returned"
# branch (known IDs) and the "no rows returned" branch (unknown IDs).
# The text strategy uses ``min_size=0`` so the empty-string case is
# exercised too — querying the table by an empty string is a valid SQL
# operation and must return zero rows because no seeded row carries an
# empty backlog ADR identifier.
# ---------------------------------------------------------------------------


_known_probe = st.sampled_from(sorted(_SLICE2_BACKLOG_IDS))
_arbitrary_probe = st.text(min_size=0, max_size=32)
_probe_strategy = st.one_of(_known_probe, _arbitrary_probe)

_probes_strategy = st.lists(_probe_strategy, min_size=1, max_size=16)


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case ``Interim_ADR_Records`` rows cannot leak
# between cases. A :class:`tempfile.TemporaryDirectory` context inside
# the test body owns the per-case directory; Hypothesis disallows
# function-scoped pytest fixtures for per-case state because they would
# not reset between generated inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with the Slice 1 schema."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Database probe helper used in the assertion loops.
# ---------------------------------------------------------------------------


def _query_by_backlog_adr_id(engine: Engine, *, backlog_adr_id: str) -> list[dict]:
    """Return every ``Interim_ADR_Records`` row whose ``backlog_adr_id`` matches.

    The five fields named by Requirement 16.3 / 21.3 are returned plus
    ``record_id`` for primary-key set equality. Ordered by ``record_id``
    so the result is deterministic regardless of SQLite's insertion
    order, which makes the byte-equivalence assertions stable across
    runs and shrinking.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT record_id, motivating_requirement,
                           motivating_criterion, observable_behavior,
                           recorded_at, backlog_adr_id
                      FROM Interim_ADR_Records
                     WHERE backlog_adr_id = :id
                     ORDER BY record_id
                    """
                ),
                {"id": backlog_adr_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _expected_rows_for(backlog_adr_id: str) -> tuple[InterimAdrSeedRow, ...]:
    """Return the Slice 2 seed rows whose ``backlog_adr_id`` equals *backlog_adr_id*.

    The expected set is computed directly from
    :data:`PLANNING_INTERIM_ADR_SEED_ROWS` so a regression in the seed
    module that changes which row attaches to which backlog ADR
    identifier surfaces as a property violation rather than being
    masked by an expectation derived from the same source as the
    implementation. Arbitrary (unknown) probes correctly resolve to an
    empty tuple here because no Slice 2 seed row matches.
    """
    return tuple(
        row
        for row in PLANNING_INTERIM_ADR_SEED_ROWS
        if row.backlog_adr_id == backlog_adr_id
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 26: Slice 2 Interim ADR records retrievability
@given(probes=_probes_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates a fresh temp directory and a fresh SQLite
    # database, then issues a seed call plus several reads, so per-case
    # setup is slightly more expensive than a pure in-memory property
    # test. The setup is well under the 2000 ms deadline locally but
    # the data-generation health check is suppressed so any one slow
    # case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_slice2_interim_adr_records_retrievable_by_backlog_adr_id(
    probes: list[str],
) -> None:
    """Querying ``Interim_ADR_Records`` by a Slice 2 backlog ADR
    identifier returns the documented row; rows are byte-equivalent
    across observation points (Property 26 / Requirements 19.5, 21.3)."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop26_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        try:
            # Single ``seed_planning_interim_adr`` call per case: Property 26
            # is about retrieval of the already-seeded table, not about the
            # seed function's idempotence (that is covered in
            # ``tests/unit/test_planning_interim_adr.py``). Pinning the
            # clock to ``_FIXED_NOW`` makes ``recorded_at`` deterministic
            # across cases and shrinking.
            seed_planning_interim_adr(engine, clock=FixedClock(_FIXED_NOW))

            # ----- Pass 1 — Unconditional Slice 2 enumeration ------------
            # The "for all enumerated Slice 2 backlog ADR identifiers"
            # half of Property 26 is exercised on every case by probing
            # the full set unconditionally. This guarantees the "at
            # least one row exists" assertion runs even when
            # Hypothesis happens to draw zero known identifiers into
            # ``probes``.
            for backlog_adr_id in sorted(_SLICE2_BACKLOG_IDS):
                _assert_known_slice2_identifier(
                    engine, backlog_adr_id=backlog_adr_id
                )

            # ----- Pass 2 — Hypothesis-drawn probes ----------------------
            # Known probes re-exercise the positive branch (the
            # repeat-read loop catches any divergence between two
            # independent observations of the same identifier within
            # one engine). Arbitrary probes exercise the negative
            # branch — querying by an identifier no seed row carries
            # returns zero rows.
            for probe in probes:
                if probe in _SLICE2_BACKLOG_IDS:
                    _assert_known_slice2_identifier(
                        engine, backlog_adr_id=probe
                    )
                else:
                    _assert_unknown_identifier_returns_empty(
                        engine, backlog_adr_id=probe
                    )

            # ----- Pass 3 — Byte-equivalence across a second seed --------
            # Re-running the seeder with a *later* clock must leave
            # every Slice 2 row byte-equivalent to the first
            # observation (``INSERT OR IGNORE`` against the stable
            # ``record_id`` primary key preserves the originally
            # recorded instant). This realises the "byte-equivalent at
            # every observation point" clause across the boundary of
            # an additional write attempt against the same table.
            seed_planning_interim_adr(engine, clock=FixedClock(_LATER_NOW))
            for backlog_adr_id in sorted(_SLICE2_BACKLOG_IDS):
                _assert_known_slice2_identifier(
                    engine, backlog_adr_id=backlog_adr_id
                )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Per-identifier assertion helpers.
#
# Split out so the assertion failure messages identify exactly which
# clause of Property 26 was violated. Both helpers re-read the table
# inside themselves rather than receiving a pre-fetched row list,
# which keeps the byte-equivalence-across-repeat-reads clause
# verifiable from inside the helper.
# ---------------------------------------------------------------------------


def _assert_known_slice2_identifier(
    engine: Engine, *, backlog_adr_id: str
) -> None:
    """Assert a known Slice 2 backlog ADR identifier has the documented row.

    Exercises four clauses of Property 26 simultaneously:

    1. *At least one row exists.* The non-empty assertion on the
       database read fails if the seed surface ever drops a Slice 2
       row.
    2. *Set equality on record identity.* The set of returned
       ``record_id`` values equals the set of expected ``record_id``
       values derived from :data:`PLANNING_INTERIM_ADR_SEED_ROWS`.
       Neither under-fetch (a missing row attached to a known
       backlog ADR identifier) nor over-fetch (a row leaking
       through the ``backlog_adr_id`` filter) is allowed.
    3. *Row content fidelity.* For every returned row, the four
       non-time fields named by Requirement 21.3 /
       Slice 1 Requirement 16.3 are byte-equivalent to the
       corresponding :class:`InterimAdrSeedRow` field. The
       ``recorded_at`` column is byte-equivalent to the ISO-8601
       rendering of ``_FIXED_NOW`` (the per-case ``FixedClock``).
    4. *Byte-equivalence across repeat observations.* The same query
       is issued :data:`_REPEAT_READS` times against the same engine
       without intervening writes; every subsequent observation is
       byte-equivalent to the first. A read-side staleness or
       SQLite-side caching regression that surfaced different bytes
       across passive reads would fail this clause.
    """
    expected = _expected_rows_for(backlog_adr_id)
    assert expected, (
        f"Test invariant violated: {backlog_adr_id!r} should be a Slice 2 "
        f"identifier resolvable in PLANNING_INTERIM_ADR_SEED_ROWS but "
        f"resolved to no rows. Update the test constants or "
        f"PLANNING_INTERIM_ADR_SEED_ROWS so they agree."
    )

    first_read = _query_by_backlog_adr_id(engine, backlog_adr_id=backlog_adr_id)

    # ----- Clause 1 — at least one row exists ---------------------
    assert first_read, (
        f"Querying Interim_ADR_Records by backlog_adr_id="
        f"{backlog_adr_id!r} returned zero rows; Property 26 requires "
        f"at least one row for every backlog ADR identifier in "
        f"{sorted(_SLICE2_BACKLOG_IDS)!r}."
    )

    # ----- Clause 2 — set equality on identity --------------------
    expected_ids = {row.record_id for row in expected}
    actual_ids = {row["record_id"] for row in first_read}
    assert actual_ids == expected_ids, (
        f"Querying Interim_ADR_Records by backlog_adr_id="
        f"{backlog_adr_id!r} returned record_id set "
        f"{sorted(actual_ids)!r}; Property 26 requires the complete set "
        f"{sorted(expected_ids)!r} (derived from "
        f"PLANNING_INTERIM_ADR_SEED_ROWS). Mismatch indicates either an "
        f"under-fetch (a seed row missing) or an over-fetch (a row "
        f"leaking through the backlog_adr_id filter)."
    )

    # ----- Clause 3 — row content fidelity ------------------------
    expected_by_id: dict[str, InterimAdrSeedRow] = {
        row.record_id: row for row in expected
    }
    for actual in first_read:
        seed_row = expected_by_id[actual["record_id"]]
        assert actual["motivating_requirement"] == (
            seed_row.motivating_requirement
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"motivating_requirement={actual['motivating_requirement']!r}; "
            f"PLANNING_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.motivating_requirement!r}."
        )
        assert actual["motivating_criterion"] == (
            seed_row.motivating_criterion
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"motivating_criterion={actual['motivating_criterion']!r}; "
            f"PLANNING_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.motivating_criterion!r}."
        )
        assert actual["observable_behavior"] == (
            seed_row.observable_behavior
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"observable_behavior={actual['observable_behavior']!r}; "
            f"PLANNING_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.observable_behavior!r}."
        )
        assert actual["backlog_adr_id"] == seed_row.backlog_adr_id, (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has backlog_adr_id="
            f"{actual['backlog_adr_id']!r}; "
            f"PLANNING_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.backlog_adr_id!r}. The query filter must not "
            f"leak rows naming a different backlog ADR identifier."
        )
        assert actual["recorded_at"] == _FIXED_NOW_ISO, (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has recorded_at="
            f"{actual['recorded_at']!r}; the per-case FixedClock should "
            f"produce {_FIXED_NOW_ISO!r} on the first seed call, and "
            f"INSERT OR IGNORE preserves it across any later seed call."
        )

    # ----- Clause 4 — byte-equivalence across repeat observations -
    # Re-read the same identifier ``_REPEAT_READS`` times without
    # intervening writes; every subsequent read must be
    # byte-equivalent to the first. ``_query_by_backlog_adr_id``
    # already orders by ``record_id`` so a stable comparison is
    # legitimate. A read-side regression that surfaced different
    # bytes across passive observations fails here.
    for observation_index in range(1, _REPEAT_READS):
        repeat_read = _query_by_backlog_adr_id(
            engine, backlog_adr_id=backlog_adr_id
        )
        assert repeat_read == first_read, (
            f"Observation #{observation_index} of "
            f"Interim_ADR_Records[backlog_adr_id={backlog_adr_id!r}] "
            f"diverged from observation #0:\n"
            f"  first  = {first_read!r}\n"
            f"  repeat = {repeat_read!r}\n"
            f"Property 26 requires byte-equivalence at every "
            f"observation point after the initial seeding."
        )


def _assert_unknown_identifier_returns_empty(
    engine: Engine, *, backlog_adr_id: str
) -> None:
    """Assert an arbitrary (non-Slice-2) identifier returns zero rows.

    Property 26's universal quantifier is restricted to the
    enumerated Slice 2 set; identifiers outside that set must
    resolve to the empty subset of
    :data:`PLANNING_INTERIM_ADR_SEED_ROWS`. This pins the negative
    branch of the retrievability property: a Slice 2 seed row must
    not be retrievable under any backlog ADR identifier other than
    the one its design decision documents.

    An arbitrary probe drawn by Hypothesis could in principle coincide
    with a Slice 1 backlog ADR identifier (e.g. ``"ADR-HT-002"``). In
    that case the database read is *not* empty (the Slice 1 seeder
    inserted a row), but the row carries a Slice-1 ``record_id``
    (``ad-ws-6..ad-ws-10``) that is by construction absent from
    :data:`PLANNING_INTERIM_ADR_SEED_ROWS`. The helper accepts this by
    asserting set-equality against the *empty* expected subset from
    :data:`PLANNING_INTERIM_ADR_SEED_ROWS` — which is what Property 26
    asserts ("no leakage of Slice 2 rows under a different
    identifier") — without overreaching into the Slice 1 retrievability
    contract that Property 15 already pins.
    """
    actual_rows = _query_by_backlog_adr_id(
        engine, backlog_adr_id=backlog_adr_id
    )

    leaking_slice2_ids = {
        row["record_id"]
        for row in actual_rows
        if row["record_id"]
        in {seed.record_id for seed in PLANNING_INTERIM_ADR_SEED_ROWS}
    }
    assert not leaking_slice2_ids, (
        f"Querying Interim_ADR_Records by an arbitrary identifier "
        f"{backlog_adr_id!r} returned Slice 2 record_id set "
        f"{sorted(leaking_slice2_ids)!r}; Property 26 forbids a Slice 2 "
        f"row from being retrievable under any backlog ADR identifier "
        f"other than the one its design decision documents."
    )
