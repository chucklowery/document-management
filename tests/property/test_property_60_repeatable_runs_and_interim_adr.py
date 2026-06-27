# Feature: fourth-walking-slice, Property 60: Repeatable property runs and Slice 4 Interim ADR retrievability
"""Property 60 — Repeatable property runs and Slice 4 Interim ADR retrievability (task 15.15).

**Property 60: Repeatable property runs**

The Slice 4 property-based test suite executes at least 100 generated
cases per property under the Hypothesis library with
``@settings(max_examples=100, deadline=2000)``, records the seed of
every test invocation, and on re-execution with the same seed produces
identical pass/fail outcomes and identical minimal counterexamples for
failing properties. In addition, *for all* backlog ADR identifiers in
the enumerated Slice 4 set
``{ADR-HT-018, ADR-HT-019, ADR-HT-020, ADR-HT-021, ADR-HT-022}``,
querying ``Interim_ADR_Records`` by backlog ADR identifier returns at
least one row whose motivating Requirement number, motivating
criterion, observable behavior, recorded date, and backlog ADR
identifier match the AD-WS-33..AD-WS-38 design decisions. Those rows
are byte-equivalent at every observation point after their initial
seeding, and re-running the seeder is idempotent.

**Validates: Requirements 60.5, 61.15**

Strategy
========

Task 15.15 folds two clauses of the cumulative verification contract
into one file:

* the *operational* repeatable-runs guarantee (Requirement 61.15), the
  Slice 4 analogue of Slice 1 Property 13, Slice 2 Property 30, and
  Slice 3 task 16.16; and
* the *Interim ADR retrievability* guarantee (Requirement 60.5), the
  Slice 4 analogue of Slice 1 Property 15, Slice 2 Property 26, and
  Slice 3 Property 45.

The repeatable-runs leg reuses the same ``tests/conftest.py`` profiles
(``dev`` / ``ci`` / ``debug``), the same ``--hypothesis-seed``
precedence chain, the same ``pytest_runtest_makereport`` hook capturing
every ``pytest.mark.property`` test, and the same
``build/hypothesis-seeds.json`` artifact written at session finish that
the prior three slices established. No changes to ``tests/conftest.py``
are needed for Slice 4 — Slice 4 inherits the cumulative wiring because
each Slice 4 property test file declares
``pytestmark = pytest.mark.property`` and uses
``@settings(max_examples=100, deadline=2000, ...)``.

The Interim ADR leg builds a fresh per-case SQLite engine, seeds the
five Slice 4 rows for Gaps G-16..G-20 / AD-WS-33..AD-WS-38 via
:func:`walking_slice.outcome._interim_adr.seed_outcome_interim_adr`,
and asserts retrievability, content fidelity, byte-equivalence across
passive observations, and idempotence across a second (later-clock)
seed call. The expected row set is derived independently from the
:data:`OUTCOME_INTERIM_ADR_SEED_ROWS` module constant rather than by
re-running the seeder during assertion, so a seed-side misroute or a
query-side leak both surface as property violations rather than being
masked by an expectation drawn from the same code path as the
implementation.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import (
    HealthCheck,
    given,
    seed,
    settings,
    strategies as st,
)
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.interim_adr import InterimAdrSeedRow
from walking_slice.outcome._interim_adr import (
    OUTCOME_INTERIM_ADR_SEED_ROWS,
    seed_outcome_interim_adr,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Constants derived directly from the spec wording.
#
# ``_SLICE4_BACKLOG_IDS`` is the five-element set Property 60 enumerates
# (task 15.15; design §"Correctness Properties → Property 60"; Requirement
# 60.5 / 61 §4). It is defined as a frozenset to make set-equality
# assertions order-free. ``_FIXED_NOW`` pins the seed clock so
# ``recorded_at`` is deterministic across cases and shrinking — Requirement
# 61 §3 names "recorded date of the choice" without constraining its exact
# value, so a fixed instant is the simplest way to make the assertion exact
# while still exercising the byte-equivalence-across-seed-calls clause (a
# *later* fixed instant is used for the second seed call).
# ---------------------------------------------------------------------------


_SLICE4_BACKLOG_IDS: Final[frozenset[str]] = frozenset(
    {
        "ADR-HT-018",
        "ADR-HT-019",
        "ADR-HT-020",
        "ADR-HT-021",
        "ADR-HT-022",
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


# Stable seeds dedicated to the repeatable-runs probes in this file.
# Using local seeds (rather than the session master seed) means the
# property under test — determinism of Hypothesis itself — does not
# depend on what seed the enclosing session happened to draw. The two
# seeds differ so the pass-case and fail-case probes are independent.
# The high-order bits differ from Property 13 (``...CAFE01/02``),
# Property 30 (``...CAFE20/21``), and the Slice 3 probes
# (``...CAFE30/31``) so a session-master-seed collision with a prior
# slice probe cannot accidentally couple the four suites' determinism
# checks.
_PASS_SEED: Final[int] = 0xC0FFEEC0DECAFE40
_FAIL_SEED: Final[int] = 0xC0FFEEC0DECAFE41


# Bounded integer range so shrink targets are well-defined. The failing
# predicate below is "x != 0", which Hypothesis is guaranteed to shrink
# to the canonical minimum integer ``0`` independently of the seed; we
# still pin the seed so the *generation* sequence is identical between
# runs.
_INT_RANGE = st.integers(min_value=-1_000_000, max_value=1_000_000)


# The set of Slice 4 property test files that participate in the
# operational seed-capture contract. Properties 46 through 60 are the
# Slice 4 numbered properties enumerated in design §"Correctness
# Properties"; this file is Property 60. Files that have not yet landed
# (because their owning tasks are still in progress) are simply skipped
# by the wiring check below — the check only enforces the contract on
# files that *exist*.
_SLICE4_PROPERTY_NUMBERS: Final[tuple[int, ...]] = tuple(range(46, 61))


# ---------------------------------------------------------------------------
# Hypothesis strategies (Interim ADR leg).
#
# A *probe* is a backlog ADR identifier the test will query the table
# by. The strategy mixes known Slice 4 identifiers and arbitrary text so
# each Hypothesis case exercises both the "complete set returned" branch
# (known IDs) and the "no Slice 4 rows leak" branch (unknown IDs). The
# text strategy uses ``min_size=0`` so the empty-string case is exercised
# too — querying the table by an empty string is a valid SQL operation
# and must return zero rows because no seeded row carries an empty
# backlog ADR identifier.
# ---------------------------------------------------------------------------


_known_probe = st.sampled_from(sorted(_SLICE4_BACKLOG_IDS))
_arbitrary_probe = st.text(min_size=0, max_size=32)
_probe_strategy = st.one_of(_known_probe, _arbitrary_probe)

_probes_strategy = st.lists(_probe_strategy, min_size=1, max_size=16)


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case ``Interim_ADR_Records`` rows cannot leak between
# cases. A :class:`tempfile.TemporaryDirectory` context inside the test
# body owns the per-case directory; Hypothesis disallows function-scoped
# pytest fixtures for per-case state because they would not reset between
# generated inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with the Slice 1 schema.

    Only the Slice 1 schema is required because ``Interim_ADR_Records``
    is a Slice 1 table; the Slice 4 seeder simply appends additive rows
    to it (Requirement 60.5).
    """
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


def _run_slice4_seed(engine: Engine, *, clock: FixedClock) -> None:
    """Open a transaction and invoke the Slice 4 Interim ADR seeder.

    Mirrors the production startup hook (``walking_slice.app``): the
    seeder shares the caller's ``engine.begin()`` transaction so a
    partial bootstrap is rolled back. Like the Slice 3 seeder, the
    Slice 4 seeder accepts a :class:`~sqlalchemy.engine.Connection`
    (rather than an :class:`~sqlalchemy.engine.Engine`), so this helper
    centralizes the ``with engine.begin() as conn`` boilerplate every
    invocation needs.
    """
    with engine.begin() as conn:
        seed_outcome_interim_adr(conn, clock=clock)


# ---------------------------------------------------------------------------
# Database probe helper used in the assertion loops.
# ---------------------------------------------------------------------------


def _query_by_backlog_adr_id(engine: Engine, *, backlog_adr_id: str) -> list[dict]:
    """Return every ``Interim_ADR_Records`` row whose ``backlog_adr_id`` matches.

    The five fields named by Requirement 60.5 / 61 §3 (which reuse
    Slice 1 Requirement 16.3) are returned plus ``record_id`` for
    primary-key set equality. Ordered by ``record_id`` so the result is
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
    """Return the Slice 4 seed rows whose ``backlog_adr_id`` matches.

    The expected set is computed directly from
    :data:`OUTCOME_INTERIM_ADR_SEED_ROWS` so a regression in the seed
    module that changes which row attaches to which backlog ADR
    identifier surfaces as a property violation rather than being masked
    by an expectation derived from the same source as the
    implementation. Arbitrary (unknown) probes correctly resolve to an
    empty tuple here because no Slice 4 seed row matches.
    """
    return tuple(
        row
        for row in OUTCOME_INTERIM_ADR_SEED_ROWS
        if row.backlog_adr_id == backlog_adr_id
    )


# ---------------------------------------------------------------------------
# Property 60 — Interim ADR retrievability leg (Requirement 60.5).
# ---------------------------------------------------------------------------


# Feature: fourth-walking-slice, Property 60: Slice 4 Interim ADR retrievability
@given(probes=_probes_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates a fresh temp directory and a fresh SQLite
    # database, then issues a seed call plus several reads, so per-case
    # setup is slightly more expensive than a pure in-memory property
    # test. The setup is well under the 2000 ms deadline locally but the
    # data-generation health check is suppressed so any one slow case
    # does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_slice4_interim_adr_records_retrievable_by_backlog_adr_id(
    probes: list[str],
) -> None:
    """Querying ``Interim_ADR_Records`` by a Slice 4 backlog ADR
    identifier returns the documented row; rows are byte-equivalent
    across observation points and re-running the seeder is idempotent
    (Property 60 / Requirement 60.5)."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop60_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        try:
            # Single ``seed_outcome_interim_adr`` call per case to start.
            # Property 60 is about retrieval of the already-seeded table
            # and the idempotence of a *re-seed*, not about the seed
            # function being called many times in a row (that is covered
            # in the Slice 4 unit tests). Pinning the clock to
            # ``_FIXED_NOW`` makes ``recorded_at`` deterministic across
            # cases and shrinking.
            _run_slice4_seed(engine, clock=FixedClock(_FIXED_NOW))

            # ----- Pass 1 — Unconditional Slice 4 enumeration ------------
            # The "for all enumerated Slice 4 backlog ADR identifiers"
            # half of Property 60 is exercised on every case by probing
            # the full set unconditionally. This guarantees the "at least
            # one row exists" assertion runs even when Hypothesis happens
            # to draw zero known identifiers into ``probes``.
            for backlog_adr_id in sorted(_SLICE4_BACKLOG_IDS):
                _assert_known_slice4_identifier(
                    engine, backlog_adr_id=backlog_adr_id
                )

            # ----- Pass 2 — Hypothesis-drawn probes ----------------------
            # Known probes re-exercise the positive branch (the
            # repeat-read loop catches any divergence between two
            # independent observations of the same identifier within one
            # engine). Arbitrary probes exercise the negative branch —
            # querying by an identifier no seed row carries leaks no
            # Slice 4 row through the ``backlog_adr_id`` filter.
            for probe in probes:
                if probe in _SLICE4_BACKLOG_IDS:
                    _assert_known_slice4_identifier(
                        engine, backlog_adr_id=probe
                    )
                else:
                    _assert_unknown_identifier_returns_no_slice4_row(
                        engine, backlog_adr_id=probe
                    )

            # ----- Pass 3 — Byte-equivalence across a second seed --------
            # Re-running the seeder with a *later* clock must leave every
            # Slice 4 row byte-equivalent to the first observation
            # (``INSERT OR IGNORE`` against the stable ``record_id``
            # primary key preserves the originally recorded instant).
            # This realises the "byte-equivalent at every observation
            # point" clause across the boundary of an additional write
            # attempt against the same table and the "re-running the
            # seeder is idempotent" clause of Property 60.
            _run_slice4_seed(engine, clock=FixedClock(_LATER_NOW))
            for backlog_adr_id in sorted(_SLICE4_BACKLOG_IDS):
                _assert_known_slice4_identifier(
                    engine, backlog_adr_id=backlog_adr_id
                )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Per-identifier assertion helpers.
#
# Split out so the assertion failure messages identify exactly which
# clause of Property 60 was violated. Both helpers re-read the table
# inside themselves rather than receiving a pre-fetched row list, which
# keeps the byte-equivalence-across-repeat-reads clause verifiable from
# inside the helper.
# ---------------------------------------------------------------------------


def _assert_known_slice4_identifier(
    engine: Engine, *, backlog_adr_id: str
) -> None:
    """Assert a known Slice 4 backlog ADR identifier has the documented row.

    Exercises four clauses of Property 60 simultaneously:

    1. *At least one row exists.* The non-empty assertion on the
       database read fails if the seed surface ever drops a Slice 4 row.
    2. *Set equality on record identity.* The set of returned
       ``record_id`` values equals the set of expected ``record_id``
       values derived from :data:`OUTCOME_INTERIM_ADR_SEED_ROWS`.
       Neither under-fetch (a missing row attached to a known backlog
       ADR identifier) nor over-fetch (a row leaking through the
       ``backlog_adr_id`` filter) is allowed.
    3. *Row content fidelity.* For every returned row, the four non-time
       fields named by Requirement 60.5 / 61 §3 (which reuse Slice 1
       Requirement 16.3) are byte-equivalent to the corresponding
       :class:`InterimAdrSeedRow` field. The ``recorded_at`` column is
       byte-equivalent to the ISO-8601 rendering of ``_FIXED_NOW`` (the
       per-case ``FixedClock``).
    4. *Byte-equivalence across repeat observations.* The same query is
       issued :data:`_REPEAT_READS` times against the same engine
       without intervening writes; every subsequent observation is
       byte-equivalent to the first. A read-side staleness or
       SQLite-side caching regression that surfaced different bytes
       across passive reads would fail this clause.
    """
    expected = _expected_rows_for(backlog_adr_id)
    assert expected, (
        f"Test invariant violated: {backlog_adr_id!r} should be a Slice 4 "
        f"identifier resolvable in OUTCOME_INTERIM_ADR_SEED_ROWS but "
        f"resolved to no rows. Update the test constants or "
        f"OUTCOME_INTERIM_ADR_SEED_ROWS so they agree."
    )

    first_read = _query_by_backlog_adr_id(engine, backlog_adr_id=backlog_adr_id)

    # ----- Clause 1 — at least one row exists ---------------------
    assert first_read, (
        f"Querying Interim_ADR_Records by backlog_adr_id="
        f"{backlog_adr_id!r} returned zero rows; Property 60 requires "
        f"at least one row for every backlog ADR identifier in "
        f"{sorted(_SLICE4_BACKLOG_IDS)!r}."
    )

    # ----- Clause 2 — set equality on identity --------------------
    expected_ids = {row.record_id for row in expected}
    actual_ids = {row["record_id"] for row in first_read}
    assert actual_ids == expected_ids, (
        f"Querying Interim_ADR_Records by backlog_adr_id="
        f"{backlog_adr_id!r} returned record_id set "
        f"{sorted(actual_ids)!r}; Property 60 requires the complete set "
        f"{sorted(expected_ids)!r} (derived from "
        f"OUTCOME_INTERIM_ADR_SEED_ROWS). Mismatch indicates either an "
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
            f"OUTCOME_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.motivating_requirement!r}."
        )
        assert actual["motivating_criterion"] == (
            seed_row.motivating_criterion
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"motivating_criterion={actual['motivating_criterion']!r}; "
            f"OUTCOME_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.motivating_criterion!r}."
        )
        assert actual["observable_behavior"] == (
            seed_row.observable_behavior
        ), (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has "
            f"observable_behavior={actual['observable_behavior']!r}; "
            f"OUTCOME_INTERIM_ADR_SEED_ROWS declares "
            f"{seed_row.observable_behavior!r}."
        )
        assert actual["backlog_adr_id"] == seed_row.backlog_adr_id, (
            f"Interim_ADR_Records row {actual['record_id']!r} retrieved "
            f"by backlog_adr_id={backlog_adr_id!r} has backlog_adr_id="
            f"{actual['backlog_adr_id']!r}; "
            f"OUTCOME_INTERIM_ADR_SEED_ROWS declares "
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
    # intervening writes; every subsequent read must be byte-equivalent
    # to the first. ``_query_by_backlog_adr_id`` already orders by
    # ``record_id`` so a stable comparison is legitimate. A read-side
    # regression that surfaced different bytes across passive
    # observations fails here.
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
            f"Property 60 requires byte-equivalence at every "
            f"observation point after the initial seeding."
        )


def _assert_unknown_identifier_returns_no_slice4_row(
    engine: Engine, *, backlog_adr_id: str
) -> None:
    """Assert an arbitrary (non-Slice-4) identifier returns no Slice 4 rows.

    Property 60's universal quantifier over Interim ADR retrievability
    is restricted to the enumerated Slice 4 set; identifiers outside
    that set must not cause any Slice 4 seed row to be retrievable
    through the ``backlog_adr_id`` filter. This pins the negative branch
    of the retrievability property: a Slice 4 seed row must not be
    retrievable under any backlog ADR identifier other than the one its
    design decision documents.

    The Slice 4 seeder does not delegate to any prior-slice seeder, so
    on the engine this test seeds the only rows present in
    ``Interim_ADR_Records`` are the five Slice 4 rows. An arbitrary
    probe that happens to coincide with a Slice 1/2/3 backlog ADR
    identifier therefore resolves to zero rows in this fixture — the
    helper asserts the stronger "no Slice 4 rows" form by filtering the
    returned ``record_id`` set against the known Slice 4 record ids.
    """
    actual_rows = _query_by_backlog_adr_id(
        engine, backlog_adr_id=backlog_adr_id
    )

    leaking_slice4_ids = {
        row["record_id"]
        for row in actual_rows
        if row["record_id"]
        in {seed_row.record_id for seed_row in OUTCOME_INTERIM_ADR_SEED_ROWS}
    }
    assert not leaking_slice4_ids, (
        f"Querying Interim_ADR_Records by an arbitrary identifier "
        f"{backlog_adr_id!r} returned Slice 4 record_id set "
        f"{sorted(leaking_slice4_ids)!r}; Property 60 forbids a Slice 4 "
        f"row from being retrievable under any backlog ADR identifier "
        f"other than the one its design decision documents."
    )


# ---------------------------------------------------------------------------
# Property 60 — repeatable-runs leg (Requirement 61.15).
# ---------------------------------------------------------------------------


def _run_collecting_property(seed_value: int) -> list[int]:
    """Run a passing Hypothesis property and return the example sequence.

    The inner test always succeeds, so Hypothesis explores
    ``max_examples`` generated values without entering the shrink phase.
    Pinning ``@seed`` and disabling the example database isolates
    Hypothesis's example-generation determinism, which is the
    operational guarantee Requirement 61.15 depends on.
    """
    collected: list[int] = []

    @seed(seed_value)
    @settings(max_examples=25, deadline=None, database=None)
    @given(value=_INT_RANGE)
    def _inner(value: int) -> None:
        collected.append(value)

    _inner()
    return list(collected)


def _run_failing_property(seed_value: int) -> tuple[bool, int | None]:
    """Run a Hypothesis property that fails on ``value == 0``.

    Returns ``(failed, minimal_counterexample)``. The failing predicate
    ``value != 0`` has a unique minimum integer counterexample (``0``),
    so Hypothesis's shrink phase is expected to converge there
    regardless of seed; the seed is pinned anyway so the path the
    shrinker takes — and any intermediate failing examples it surfaces —
    is identical between the two invocations.
    """

    @seed(seed_value)
    @settings(max_examples=200, deadline=None, database=None)
    @given(value=_INT_RANGE)
    def _inner(value: int) -> None:
        # Embed the value in the assertion message so the minimal
        # counterexample is recoverable from the AssertionError that
        # Hypothesis re-raises after shrinking.
        assert value != 0, f"value={value}"

    try:
        _inner()
    except AssertionError as exc:
        match = re.search(r"value=(-?\d+)", str(exc))
        assert match is not None, (
            "Hypothesis re-raised an AssertionError without a "
            f"recoverable value marker: {exc!r}."
        )
        return (True, int(match.group(1)))
    return (False, None)


def test_slice4_repeatable_pass_outcome_under_fixed_seed() -> None:
    """Re-executing a passing property with the same seed yields the
    same generated example sequence (Slice 4 leg of Property 60).

    Requirement 61.15: "on re-execution with the same seed produce
    identical pass/fail outcomes ... for failing properties." This test
    covers the *passing* leg by confirming generation determinism using
    the same probe pattern Property 13 (Slice 1), Property 30 (Slice 2),
    and the Slice 3 task 16.16 probes use; when generation is
    deterministic, pass/fail outcomes are necessarily deterministic too.
    The Slice 4 suite cannot diverge from this guarantee because it
    reuses the same ``tests/conftest.py`` seed-resolution mechanism —
    but running the probe here makes the Slice 4 operational contract
    observable in the artifact and in failure triage.
    """
    first = _run_collecting_property(_PASS_SEED)
    second = _run_collecting_property(_PASS_SEED)

    assert first == second, (
        "Two Hypothesis runs with the same @seed and database=None "
        "produced different example sequences. Requirement 61.15 "
        "requires deterministic re-execution; this indicates a "
        "configuration drift (e.g. database not disabled, profile "
        "phases differ, or upstream Hypothesis no longer honours the "
        "pinned seed).\n"
        f"first[:5]={first[:5]!r}\nsecond[:5]={second[:5]!r}"
    )
    assert len(first) > 0, (
        "Slice 4 repeatable-runs probe collected zero examples; "
        "max_examples or generation strategy regressed."
    )


def test_slice4_repeatable_failure_and_minimal_counterexample_under_fixed_seed() -> None:
    """Re-executing a failing property with the same seed yields the
    same minimal counterexample (Slice 4 leg of Property 60).

    Requirement 61.15: "identical minimal counterexamples for failing
    properties." The predicate ``value != 0`` has ``0`` as its unique
    minimum, so a correctly seeded Hypothesis run shrinks to ``0`` both
    times. The double-run asserts agreement between the two invocations
    rather than hard-coding ``0`` so the test still detects determinism
    regressions if the shrinker stops at a different minimum.
    """
    first_failed, first_min = _run_failing_property(_FAIL_SEED)
    second_failed, second_min = _run_failing_property(_FAIL_SEED)

    assert first_failed and second_failed, (
        "Failing-property probe did not fail; the property setup is "
        "stale (predicate, range, or settings changed) and no longer "
        "exercises the failure path."
    )
    assert first_min == second_min, (
        "Two Hypothesis runs with the same @seed and the same failing "
        "predicate produced different minimal counterexamples "
        f"({first_min!r} vs {second_min!r}). Requirement 61.15 requires "
        "identical minimal counterexamples on replay."
    )
    # The predicate's unique minimum is 0; if Hypothesis ever shrinks to
    # a different value the test should fail loudly because the
    # operational guarantee depends on the canonical minimum.
    assert first_min == 0, (
        f"Expected Hypothesis to shrink 'value != 0' to 0; got "
        f"{first_min!r}. Either the shrinker regressed or the input "
        "strategy no longer includes zero."
    )


def test_slice4_seed_capture_artifact_wiring(pytestconfig: pytest.Config) -> None:
    """The conftest seed-capture mechanism is active for the Slice 4 suite.

    Requirement 61.15 (matching Slice 1 Requirement 15.13, Slice 2
    Requirement 20.13, and Slice 3 Requirement 41.15 by reference):
    "record the seed of every test invocation." The artifact itself is
    finalized in ``pytest_sessionfinish`` so its contents cannot be
    inspected mid-session; this test verifies the *wiring* — the master
    seed is resolved, exposed on the pytest config, and the artifact
    destination is well-formed. The wiring is shared with Slices 1-3 by
    design, so the same master seed governs all four suites and all four
    contribute invocations to the same ``build/hypothesis-seeds.json``
    artifact.
    """
    master_seed = getattr(pytestconfig, "_walking_slice_seed", None)
    assert master_seed is not None, (
        "tests/conftest.py did not install a master Hypothesis seed. "
        "Requirement 61.15 requires every session to record a seed; "
        "check pytest_configure in tests/conftest.py."
    )
    assert isinstance(master_seed, int), (
        f"Master Hypothesis seed must be an int; got "
        f"{type(master_seed).__name__}."
    )
    assert master_seed >= 0, (
        f"Master Hypothesis seed must be non-negative; got "
        f"{master_seed!r}."
    )

    rootpath = Path(pytestconfig.rootpath)
    artifact_dir = rootpath / "build"
    assert rootpath.exists(), (
        f"pytest rootpath {rootpath!r} does not exist; cannot place the "
        "seed-capture artifact."
    )
    # Reject obviously-wrong paths without actually creating the
    # directory (the session-finish hook owns creation).
    assert artifact_dir.parent == rootpath, (
        "Seed-capture artifact must live under the project root."
    )


def test_slice4_property_files_declare_property_marker_and_settings() -> None:
    """Every Slice 4 property test file is wired into the seed-capture path.

    Requirement 61.15 mandates the Slice 4 suite "records the seed of
    every test invocation." The capture hook in ``tests/conftest.py``
    filters on ``item.get_closest_marker("property")``, so a Slice 4
    test file that omits ``pytestmark = pytest.mark.property`` would
    silently drop its invocations from the artifact. Likewise the "at
    least 100 generated cases per property" clause is honored per-test
    via ``@settings(max_examples=100, deadline=2000)``; the presence of
    an explicit ``@settings`` decorator on each property test guards
    against accidental fallback to whatever the active profile default
    happens to be at session time.

    This check is static — it reads source text — and tolerates Slice 4
    test files that have not yet landed. A file "lands" a Hypothesis
    property only once it declares a ``@given`` decorator; sibling tasks
    under task 15 (for example the in-progress Properties 56-59) may
    have created a test module that so far contains only helper
    scaffolding and no ``@given`` property. Such a stub does not yet
    contribute any invocation to the seed-capture artifact, so the
    seed-capture contract does not bind it. The check therefore enforces
    the marker-and-settings contract on every file that *has* landed a
    ``@given`` property and skips files that have not.

    The check also asserts that every Slice 4 ``@settings(...)``
    decorator declares ``max_examples`` and ``deadline`` explicitly —
    Requirement 61.15's "at least 100 generated cases per property"
    cannot be audited statically without these keys being present in the
    test source. This mirrors the Slice 2 / Slice 3 contracts.
    """
    property_dir = Path(__file__).parent
    missing_marker: list[str] = []
    missing_settings: list[str] = []
    settings_without_required_keys: list[tuple[str, list[str]]] = []

    for number in _SLICE4_PROPERTY_NUMBERS:
        matches = sorted(property_dir.glob(f"test_property_{number}_*.py"))
        for path in matches:
            source = path.read_text(encoding="utf-8")
            if "@given(" not in source:
                # The module exists but has not yet landed a Hypothesis
                # property (an in-progress sibling task under task 15).
                # It contributes no invocation to the seed-capture
                # artifact, so the marker-and-settings contract does not
                # bind it yet.
                continue
            if "pytestmark = pytest.mark.property" not in source:
                missing_marker.append(path.name)
            if "@settings(" not in source:
                missing_settings.append(path.name)
                continue
            # Each property test in the Slice 4 suite must declare both
            # ``max_examples`` and ``deadline`` explicitly on its
            # ``@settings`` decorator so that the per-test contract is
            # legible from the test source alone (Requirement 61.15's
            # "at least 100 generated cases per property" cannot be
            # audited statically without these keys being present).
            missing_keys = [
                key
                for key in ("max_examples", "deadline")
                if key not in source
            ]
            if missing_keys:
                settings_without_required_keys.append((path.name, missing_keys))

    assert not missing_marker, (
        "Slice 4 property test files are missing "
        "``pytestmark = pytest.mark.property``; their invocations will "
        "not be captured in build/hypothesis-seeds.json. "
        f"Affected files: {missing_marker!r}."
    )
    assert not missing_settings, (
        "Slice 4 property test files are missing an explicit "
        "``@settings(...)`` decorator; Requirement 61.15 requires "
        "per-property pinning of max_examples and deadline. "
        f"Affected files: {missing_settings!r}."
    )
    assert not settings_without_required_keys, (
        "Slice 4 property test files have ``@settings(...)`` but do not "
        "declare ``max_examples`` and ``deadline`` explicitly. Both "
        "keys must be present per Requirement 61.15 / design "
        "§\"Cumulative verification with Slices 1-3\". "
        f"Affected files: {settings_without_required_keys!r}."
    )
