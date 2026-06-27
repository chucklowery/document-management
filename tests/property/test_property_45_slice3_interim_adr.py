# Feature: third-walking-slice, Property 45: Slice 3 Interim ADR records retrievability
"""Property 45 — Slice 3 Interim ADR records retrievability (task 16.15).

**Property 45: Slice 3 Interim ADR records retrievability**

*For all* backlog ADR identifiers in the enumerated Slice 3 set
``{ADR-HT-013, ADR-HT-014, ADR-HT-015, ADR-HT-016, ADR-HT-017}``,
querying ``Interim_ADR_Records`` by backlog ADR identifier returns at
least one row whose motivating Requirement number, motivating
criterion, observable behavior, recorded date, and backlog ADR
identifier match the AD-WS-24..AD-WS-28 design decisions. These rows
are byte-equivalent at every observation point after their initial
seeding; re-running the seeder is idempotent.

**Validates: Requirements 40.5, 42.3, 42.4, 41.15**

Strategy
========

Property 45 is the Slice 3 analogue of Property 26 (Slice 2 Interim
ADR retrievability, task 16.11) and Property 15 (Slice 1 Interim ADR
retrievability, task 13.3). Each Hypothesis case:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state
   cannot contaminate query results (design §"Testing Strategy" —
   per-case database isolation). Only the Slice 1 schema is required
   because ``Interim_ADR_Records`` is a Slice 1 table; the Slice 3
   seeder simply appends additive rows to it (Requirement 40.5).
2. Seeds the table once with
   :func:`walking_slice.execution._interim_adr.seed_execution_interim_adr`,
   which inserts the five Slice 3 rows for Gaps G-11..G-15 / AD-WS-24
   through AD-WS-28. The clock is pinned to a per-case
   :class:`FixedClock` so ``recorded_at`` is deterministic across
   cases and shrinking. Unlike the Slice 2 seeder, the Slice 3
   seeder takes a :class:`~sqlalchemy.engine.Connection` rather than
   an :class:`~sqlalchemy.engine.Engine` so the writes participate
   in the caller's transaction — every invocation in this test
   wraps the call in ``engine.begin()`` to honor that contract.
3. Draws a list of *probes* — backlog ADR identifiers the test will
   query the table by — mixing known Slice 3 identifiers from
   ``{ADR-HT-013, ADR-HT-014, ADR-HT-015, ADR-HT-016, ADR-HT-017}``
   with arbitrary text. The arbitrary branch exercises the
   "unrecognized identifier returns no leaking Slice 3 rows" path
   without coupling the strategy to a hand-picked list of "obviously
   unknown" identifiers. The full Slice 3 set is also probed
   unconditionally on every case so the "at least one row exists for
   every enumerated Slice 3 identifier" half of the property is
   exercised on every case rather than only when Hypothesis happens
   to draw a known identifier into ``probes``.
4. For each Slice 3 backlog ADR identifier, asserts the four
   non-time fields named by Requirement 42.3 (which reuses Slice 1
   Requirement 16.3) — ``motivating_requirement``,
   ``motivating_criterion``, ``observable_behavior``,
   ``backlog_adr_id`` — and the ``recorded_at`` ISO-8601 column are
   byte-equivalent to the corresponding
   :class:`InterimAdrSeedRow` from
   :data:`EXECUTION_INTERIM_ADR_SEED_ROWS`. This pins the "row
   identifying the motivating Requirement number, motivating
   criterion, observable behavior, recorded date, and backlog ADR
   identifier" clause.
5. Re-reads the same backlog ADR identifier multiple times within
   the same engine without intervening writes, and asserts every
   subsequent observation is byte-equivalent to the first. This is
   the "byte-equivalent at every observation point" clause for
   passive observations.
6. Re-invokes :func:`seed_execution_interim_adr` with a *later*
   :class:`FixedClock` and asserts every Slice 3 row is still
   byte-equivalent to the original snapshot. ``INSERT OR IGNORE``
   against the stable ``record_id`` primary key (``ad-ws-24``
   through ``ad-ws-28``) must preserve the originally recorded
   instant; this is the "re-running the seeder is idempotent" clause
   and the "byte-equivalent across observation points that span an
   additional seed call" clause.
7. For arbitrary unknown probes, asserts no Slice 3 row leaks
   through the ``backlog_adr_id`` filter — the set of Slice 3
   ``record_id`` values returned is empty. An arbitrary probe drawn
   by Hypothesis could in principle coincide with a Slice 1 or
   Slice 2 backlog ADR identifier (e.g. ``"ADR-HT-002"``), but those
   rows are not seeded by this test (the Slice 3 seeder does not
   delegate to either prior-slice seeder), so the assertion is the
   stronger "no rows at all" form here.

The expected row set is derived independently from the
:data:`EXECUTION_INTERIM_ADR_SEED_ROWS` module constant rather than
by re-running the seeder during assertion: a seed-side bug that
misroutes a row to the wrong backlog ADR identifier, or a query-side
bug that leaks rows through the ``backlog_adr_id`` filter, both
surface as property violations rather than being masked by an
expectation derived from the same code path as the implementation.
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
from walking_slice.execution._interim_adr import (
    EXECUTION_INTERIM_ADR_SEED_ROWS,
    seed_execution_interim_adr,
)
from walking_slice.interim_adr import InterimAdrSeedRow
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Constants derived directly from the spec wording.
#
# ``_SLICE3_BACKLOG_IDS`` is the five-element set Property 45 enumerates
# (task 16.15; design §"Correctness Properties → Property 45"). It is
# defined as a frozenset to make set-equality assertions order-free.
# ``_FIXED_NOW`` pins the seed clock so ``recorded_at`` is deterministic
# across cases and shrinking — Requirement 42.3 names "recorded date of
# the choice" without constraining its exact value, so a fixed instant
# is the simplest way to make the assertion exact while still exercising
# the byte-equivalence-across-seed-calls clause (a *later* fixed instant
# is used for the second seed call).
# ---------------------------------------------------------------------------


_SLICE3_BACKLOG_IDS: Final[frozenset[str]] = frozenset(
    {
        "ADR-HT-013",
        "ADR-HT-014",
        "ADR-HT-015",
        "ADR-HT-016",
        "ADR-HT-017",
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
# by. The strategy mixes known Slice 3 identifiers and arbitrary text so
# each Hypothesis case exercises both the "complete set returned"
# branch (known IDs) and the "no Slice 3 rows leak" branch (unknown
# IDs). The text strategy uses ``min_size=0`` so the empty-string case
# is exercised too — querying the table by an empty string is a valid
# SQL operation and must return zero rows because no seeded row carries
# an empty backlog ADR identifier.
# ---------------------------------------------------------------------------


_known_probe = st.sampled_from(sorted(_SLICE3_BACKLOG_IDS))
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
# function-scoped pytest fixtures for per-case state because they
# would not reset between generated inputs.
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


def _run_slice3_seed(engine: Engine, *, clock: FixedClock) -> None:
    """Open a transaction and invoke the Slice 3 Interim ADR seeder.

    Mirrors the production startup hook from task 15.3: the seeder
    shares the caller's ``engine.begin()`` transaction so a partial
    bootstrap is rolled back. The Slice 3 seeder differs from the
    Slice 1 and Slice 2 seeders by accepting a
    :class:`~sqlalchemy.engine.Connection` (rather than an
    :class:`~sqlalchemy.engine.Engine`), so this helper centralizes
    the ``with engine.begin() as conn`` boilerplate every invocation
    needs.
    """
    with engine.begin() as conn:
        seed_execution_interim_adr(conn, clock=clock)


# ---------------------------------------------------------------------------
# Database probe helper used in the assertion loops.
# ---------------------------------------------------------------------------


def _query_by_backlog_adr_id(
    engine: Engine, *, backlog_adr_id: str
) -> list[dict]:
    """Return every ``Interim_ADR_Records`` row whose ``backlog_adr_id`` matches.

    The five fields named by Requirement 42.3 (which reuses Slice 1
    Requirement 16.3) are returned plus ``record_id`` for primary-key
    set equality. Ordered by ``record_id`` so the result is
    deterministic regardless of SQLite's insertion order, which makes
    the byte-equivalence assertions stable across runs and shrinking.
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
    """Return the Slice 3 seed rows whose ``backlog_adr_id`` matches.

    The expected set is computed directly from
    :data:`EXECUTION_INTERIM_ADR_SEED_ROWS` so a regression in the
    seed module that changes which row attaches to which backlog ADR
    identifier surfaces as a property violation rather than being
    masked by an expectation derived from the same source as the
    implementation. Arbitrary (unknown) probes correctly resolve to an
    empty tuple here because no Slice 3 seed row matches.
    """
    return tuple(
        row
        for row in EXECUTION_INTERIM_ADR_SEED_ROWS
        if row.backlog_adr_id == backlog_adr_id
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 45: Slice 3 Interim ADR records retrievability
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
def test_slice3_interim_adr_records_retrievable_by_backlog_adr_id(
    probes: list[str],
) -> None:
    """Querying ``Interim_ADR_Records`` by a Slice 3 backlog ADR
    identifier returns the documented row; rows are byte-equivalent
    across observation points and re-running the seeder is idempotent
    (Property 45 / Requirements 40.5, 42.3, 42.4, 41.15)."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop45_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        try:
            # Single ``seed_execution_interim_adr`` call per case to start.
            # Property 45 is about retrieval of the already-seeded table
            # and the idempotence of a *re-seed*, not about the seed
            # function being called many times in a row (that is covered
            # in ``tests/unit/test_execution_interim_adr.py``). Pinning
            # the clock to ``_FIXED_NOW`` makes ``recorded_at``
            # deterministic across cases and shrinking.
            _run_slice3_seed(engine, clock=FixedClock(_FIXED_NOW))

            # ----- Pass 1 — Unconditional Slice 3 enumeration ------------
            # The "for all enumerated Slice 3 backlog ADR identifiers"
            # half of Property 45 is exercised on every case by probing
            # the full set unconditionally. This guarantees the "at
            # least one row exists" assertion runs even when Hypothesis
            # happens to draw zero known identifiers into ``probes``.
            for backlog_adr_id in sorted(_SLICE3_BACKLOG_IDS):
                _assert_known_slice3_identifier(
                    engine, backlog_adr_id=backlog_adr_id
                )

            # ----- Pass 2 — Hypothesis-drawn probes ----------------------
            # Known probes re-exercise the positive branch (the
            # repeat-read loop catches any divergence between two
            # independent observations of the same identifier within
            # one engine). Arbitrary probes exercise the negative
            # branch — querying by an identifier no seed row carries
            # leaks no Slice 3 row through the ``backlog_adr_id``
            # filter.
            for probe in probes:
                if probe in _SLICE3_BACKLOG_IDS:
                    _assert_known_slice3_identifier(
                        engine, backlog_adr_id=probe
                    )
                else:
                    _assert_unknown_identifier_returns_no_slice3_row(
                        engine, backlog_adr_id=probe
                    )

            # ----- Pass 3 — Byte-equivalence across a second seed --------
            # Re-running the seeder with a *later* clock must leave
            # every Slice 3 row byte-equivalent to the first
            # observation (``INSERT OR IGNORE`` against the stable
            # ``record_id`` primary key preserves the originally
            # recorded instant). This realises the "byte-equivalent at
            # every observation point" clause across the boundary of
            # an additional write attempt against the same table and
            # the "re-running the seeder is idempotent" clause of
            # Property 45.
            _run_slice3_seed(engine, clock=FixedClock(_LATER_NOW))
            for backlog_adr_id in sorted(_SLICE3_BACKLOG_IDS):
                _assert_known_slice3_identifier(
                    engine, backlog_adr_id=backlog_adr_id
                )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Per-identifier assertion helpers.
#
# Split out so the assertion failure messages identify exactly which
# clause of Property 45 was violated. Both helpers re-read the table
# inside themselves rather than receiving a pre-fetched row list,
# which keeps the byte-equivalence-across-repeat-reads clause
# verifiable from inside the helper.
# ---------------------------------------------------------------------------


def _assert_known_slice3_identifier(
    engine: Engine, *, backlog_adr_id: str
) -> None:
    """Assert a known Slice 3 backlog ADR identifier has the documented row.

    Exercises four clauses of Property 45 simultaneously:

    1. *At least one row exists.* The non-empty assertion on the
       database read fails if the seed surface ever drops a Slice 3
       row.
    2. *Set equality on record identity.* The set of returned
       ``record_id`` values equals the set of expected ``record_id``
       values derived from :data:`EXECUTION_INTERIM_ADR_SEED_ROWS`.
       Neither under-fetch (a missing row attached to a known
       backlog ADR identifier) nor over-fetch (a row leaking
       through the ``backlog_adr_id`` filter) is allowed.
    3. *Row content fidelity.* For every returned row, the four
       non-time fields named by Requirement 42.3 (which reuses
       Slice 1 Requirement 16.3) are byte-equivalent to the
       corresponding :class:`InterimAdrSeedRow` field. The
       ``recorded_at`` column is byte-equivalent to the ISO-8601
       rendering of ``_FIXED_NOW`` (the per-case ``FixedClock``).
    4. *Byte-equivalence across repeat observations.* The same
       query is issued :data:`_REPEAT_READS` times against the same
       engine without intervening writes; every subsequent
       observation is byte-equivalent to the first. A read-side
       staleness or SQLite-side caching regression that surfaced
       different bytes across passive reads would fail this clause.
    """
    expected = _expected_rows_for(backlog_adr_id)
    assert expected, (
        f"Test invariant violated: {backlog_adr_id!r} should be a Slice 3 "
        f"identifier resolvable in EXECUTION_INTERIM_ADR_SEED_ROWS but "
        f"resolved to no rows. Update the test constants or "
        f"EXECUTION_INTERIM_ADR_SEED_ROWS so they agree."
    )

    first_read = _query_by_backlog_adr_id(engine, backlog_adr_id=backlog_adr_id)

    # ----- Clause 1 — at least one row exists ---------------------
    assert first_read, (
        f"Querying Interim_ADR_Records by backlog_adr_id="
        f"{backlog_adr_id!r} returned zero rows; Property 45 requires "
        f"at least one row for every backlog ADR identifier in "
        f"{sorted(_SLICE3_BACKLOG_IDS)!r}."
    )

    # ----- Clause 2 — set equality on identity --------------------
    expected_ids = {row.record_id for row in expected}
    actual_ids = {row["record_id"] for row in first_read}
    assert actual_ids == expected_ids, (
        f"Querying Interim_ADR_Records by backlog_adr_id="
        f"{backlog_adr_id!r} returned record_id set "
        f"{sorted(actual_ids)!r}; Property 45 requires the complete set "
        f"{sorted(expected_ids)!r} (derived from "
        f"EXECUTION_INTERIM_ADR_SEED_ROWS). Mismatch indicates either an "
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
            f"EXECUTION_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.motivating_requirement!r}."
        )
        assert actual["motivating_criterion"] == (
            seed_row.motivating_criterion
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"motivating_criterion={actual['motivating_criterion']!r}; "
            f"EXECUTION_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.motivating_criterion!r}."
        )
        assert actual["observable_behavior"] == (
            seed_row.observable_behavior
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"observable_behavior={actual['observable_behavior']!r}; "
            f"EXECUTION_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.observable_behavior!r}."
        )
        assert actual["backlog_adr_id"] == seed_row.backlog_adr_id, (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has backlog_adr_id="
            f"{actual['backlog_adr_id']!r}; "
            f"EXECUTION_INTERIM_ADR_SEED_ROWS declares "
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
            f"Property 45 requires byte-equivalence at every "
            f"observation point after the initial seeding."
        )


def _assert_unknown_identifier_returns_no_slice3_row(
    engine: Engine, *, backlog_adr_id: str
) -> None:
    """Assert an arbitrary (non-Slice-3) identifier returns no Slice 3 rows.

    Property 45's universal quantifier is restricted to the
    enumerated Slice 3 set; identifiers outside that set must not
    cause any Slice 3 seed row to be retrievable through the
    ``backlog_adr_id`` filter. This pins the negative branch of the
    retrievability property: a Slice 3 seed row must not be
    retrievable under any backlog ADR identifier other than the one
    its design decision documents.

    The Slice 3 seeder does not delegate to either prior-slice
    seeder, so on the engine this test seeds the only rows present in
    ``Interim_ADR_Records`` are the five Slice 3 rows. An arbitrary
    probe that happens to coincide with a Slice 1 backlog ADR
    identifier (e.g. ``"ADR-HT-002"``) therefore resolves to zero
    rows in this fixture — the helper asserts the stronger
    "no rows at all" form here, which is correct because no
    Slice 1 / Slice 2 row is present to be confused with a leaking
    Slice 3 row.
    """
    actual_rows = _query_by_backlog_adr_id(
        engine, backlog_adr_id=backlog_adr_id
    )

    leaking_slice3_ids = {
        row["record_id"]
        for row in actual_rows
        if row["record_id"]
        in {seed.record_id for seed in EXECUTION_INTERIM_ADR_SEED_ROWS}
    }
    assert not leaking_slice3_ids, (
        f"Querying Interim_ADR_Records by an arbitrary identifier "
        f"{backlog_adr_id!r} returned Slice 3 record_id set "
        f"{sorted(leaking_slice3_ids)!r}; Property 45 forbids a Slice 3 "
        f"row from being retrievable under any backlog ADR identifier "
        f"other than the one its design decision documents."
    )
