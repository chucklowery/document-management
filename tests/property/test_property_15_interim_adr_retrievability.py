# Feature: first-walking-slice, Property 15: Interim ADR records retrievable by backlog ADR identifier
"""Property 15 — Interim ADR records retrievable by backlog ADR identifier (task 13.3).

**Property 15: Interim ADR records retrievable by backlog ADR identifier**

For all interim behaviors implemented by the slice that motivate a
backlog ADR — ``ADR-HT-002``, ``ADR-HT-003``, ``ADR-HT-004``,
``ADR-HT-005``, ``ADR-HT-008`` — the ``Interim_ADR_Records`` table
contains a row identifying the motivating Requirement number,
motivating criterion, observable behavior, recorded date, and backlog
ADR identifier. For any given backlog ADR identifier, querying the
table by that identifier returns the complete set of interim records
that name it.

**Validates: Requirements 16.3**

Strategy:

Each Hypothesis case spins up a fresh per-test SQLite engine on a
unique :class:`tempfile.TemporaryDirectory` path so cross-case state
cannot contaminate query results. It seeds the table once with
:func:`walking_slice.interim_adr.seed` (idempotent ``INSERT OR
IGNORE``; task 13.1) and then draws a list of *probe* backlog ADR
identifiers. Each probe is either:

- a known backlog ADR identifier sampled from
  ``{ADR-HT-002, ADR-HT-003, ADR-HT-004, ADR-HT-005, ADR-HT-008}``
  (the five identifiers Property 15 names), or
- an arbitrary text string that may or may not coincide with a known
  identifier — drawing arbitrary strings exercises the negative
  branch ("query by an identifier that no Interim ADR row names
  returns zero rows") without coupling the strategy to a hand-picked
  list of "obviously unknown" identifiers.

The expected result for each probe is derived independently from the
:data:`~walking_slice.interim_adr.INTERIM_ADR_SEED_ROWS` module
constant: the *expected* set of rows for probe ``id`` is exactly the
subset of seed rows whose ``backlog_adr_id`` equals ``id``. This is
the "complete set of motivating-requirement rows" the property names.
For a known identifier this is a singleton (each Gap G-1..G-5 produces
one row, attached to one backlog ADR); for an unknown identifier this
is the empty set. The assertion compares the database read against
this independently derived expectation, so a seed-side bug (a missing
row, a duplicated row, or a row attached to the wrong backlog
identifier) and a query-side bug (a stale or filtered read) are both
caught.

Per probe the test asserts three things:

1. **Set equality on identity.** The set of ``record_id`` values
   returned by the query equals the set of ``record_id`` values in
   the expected subset. This pins the "complete set" half of the
   property — neither under-fetch (missing rows) nor over-fetch
   (rows naming a different backlog ADR) is allowed.
2. **Row content fidelity.** For every returned row, the four
   non-time fields named by Requirement 16.3 —
   ``motivating_requirement``, ``motivating_criterion``,
   ``observable_behavior``, ``backlog_adr_id`` — are byte-equivalent
   to the corresponding ``InterimAdrSeedRow`` field. This is the
   "row identifying the motivating Requirement number, motivating
   criterion, observable behavior … and backlog ADR identifier"
   half of the property. ``recorded_at`` is checked separately
   (assertion 3) because it is generated at seed time from the
   :class:`FixedClock`, not by the module constant.
3. **Recorded date present.** Every returned row carries a non-empty
   ``recorded_at`` ISO-8601 string (Requirement 16.3 — "recorded
   date of the choice"). The exact value is generated at seed time
   from the per-case :class:`FixedClock`, so the assertion checks
   presence and the configured fixed instant rather than an exact
   wall-clock value.

The test deliberately calls :func:`seed` exactly once per case and
queries the same engine repeatedly: Property 15 is about retrieval
of an already-seeded table, not about the seed function's
idempotence (that is the subject of
``tests/unit/test_interim_adr.py``). The single seed call also keeps
the case well inside the 2000 ms deadline.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.interim_adr import INTERIM_ADR_SEED_ROWS, InterimAdrSeedRow, seed
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Constants derived directly from the spec wording.
#
# ``_KNOWN_BACKLOG_IDS`` is the five-element set Property 15 enumerates.
# ``_FIXED_NOW`` pins the seed clock so ``recorded_at`` is deterministic
# across cases and shrinking — Requirement 16.3 names "recorded date of
# the choice" without constraining its exact value, so a fixed instant
# is the simplest way to make the assertion exact.
# ---------------------------------------------------------------------------


_KNOWN_BACKLOG_IDS: Final[tuple[str, ...]] = (
    "ADR-HT-002",
    "ADR-HT-003",
    "ADR-HT-004",
    "ADR-HT-005",
    "ADR-HT-008",
)


_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FIXED_NOW_ISO: Final[str] = _FIXED_NOW.isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# A *probe* is a backlog ADR identifier the test will query the table
# by. The strategy mixes known identifiers and arbitrary text so each
# Hypothesis case exercises both the "complete set returned" branch
# (known IDs) and the "no rows returned" branch (unknown IDs). The
# text strategy uses ``min_size=0`` so the empty-string case is also
# exercised — querying the table by an empty string is a valid SQL
# operation and must return zero rows.
# ---------------------------------------------------------------------------


_known_probe = st.sampled_from(_KNOWN_BACKLOG_IDS)
_arbitrary_probe = st.text(min_size=0, max_size=32)
_probe_strategy = st.one_of(_known_probe, _arbitrary_probe)

_probes_strategy = st.lists(_probe_strategy, min_size=1, max_size=16)


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case identifiers and ``Interim_ADR_Records``
# rows cannot leak between cases (design §"Testing Strategy" — "Each
# property and example test gets a fresh SQLite database"). A
# :class:`tempfile.TemporaryDirectory` context inside the test body
# owns the per-case directory; Hypothesis disallows function-scoped
# pytest fixtures for per-case state because they would not reset
# between generated inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas."""
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
# Database probe helper used in the assertion loop.
# ---------------------------------------------------------------------------


def _query_by_backlog_adr_id(engine: Engine, *, backlog_adr_id: str) -> list[dict]:
    """Return every ``Interim_ADR_Records`` row whose ``backlog_adr_id`` matches.

    Ordered by ``record_id`` so the result is deterministic regardless of
    SQLite's insertion order, which makes the equality assertions in the
    test body stable across runs and shrinking.
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
    """Return the seed rows whose ``backlog_adr_id`` equals *backlog_adr_id*.

    The expected set is computed directly from
    :data:`INTERIM_ADR_SEED_ROWS` so a regression in the seed module
    that changes which row attaches to which backlog ADR identifier
    surfaces as a property violation rather than being masked by an
    expectation derived from the same source as the implementation.
    """
    return tuple(
        row for row in INTERIM_ADR_SEED_ROWS if row.backlog_adr_id == backlog_adr_id
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 15: Interim ADR records retrievable by backlog ADR identifier
@given(probes=_probes_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates a fresh temp directory and a fresh SQLite
    # database, so per-case setup is slightly more expensive than a
    # pure in-memory property test. The setup is well under the
    # 2000 ms deadline locally but the data-generation health check
    # is suppressed so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_interim_adr_records_retrievable_by_backlog_adr_id(
    probes: list[str],
) -> None:
    """Querying ``Interim_ADR_Records`` by a backlog ADR identifier returns
    exactly the subset of seed rows whose ``backlog_adr_id`` matches."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop15_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        try:
            # Single ``seed`` call per case: Property 15 is about
            # retrieval of an already-seeded table, not about the
            # seed function's idempotence (that is covered in
            # ``tests/unit/test_interim_adr.py``). Pinning the clock
            # to ``_FIXED_NOW`` makes ``recorded_at`` deterministic
            # across cases and shrinking.
            seed(engine, clock=FixedClock(_FIXED_NOW))

            for probe in probes:
                expected = _expected_rows_for(probe)
                actual_rows = _query_by_backlog_adr_id(
                    engine, backlog_adr_id=probe
                )

                # ----- Assertion 1 — Set equality on identity ---------
                # The set of returned ``record_id`` values equals the
                # set of expected ``record_id`` values. Neither
                # under-fetch (a missing row attached to a known
                # backlog ADR identifier) nor over-fetch (a row
                # leaking through the ``backlog_adr_id`` filter) is
                # allowed. Comparing by ``record_id`` is sufficient
                # because ``Interim_ADR_Records.record_id`` is the
                # primary key.
                expected_ids = {row.record_id for row in expected}
                actual_ids = {row["record_id"] for row in actual_rows}
                assert actual_ids == expected_ids, (
                    "Querying Interim_ADR_Records by "
                    f"backlog_adr_id={probe!r} returned record_id set "
                    f"{sorted(actual_ids)!r}; Property 15 requires the "
                    "complete set "
                    f"{sorted(expected_ids)!r} (derived from "
                    "INTERIM_ADR_SEED_ROWS)."
                )

                # ----- Assertion 2 — Row content fidelity -------------
                # For every returned row, the four non-time fields
                # named by Requirement 16.3 are byte-equivalent to the
                # corresponding ``InterimAdrSeedRow`` field. This pins
                # the "row identifying the motivating Requirement
                # number, motivating criterion, observable behavior
                # … and backlog ADR identifier" half of the property.
                expected_by_id: dict[str, InterimAdrSeedRow] = {
                    row.record_id: row for row in expected
                }
                for actual in actual_rows:
                    seed_row = expected_by_id[actual["record_id"]]
                    assert actual["motivating_requirement"] == (
                        seed_row.motivating_requirement
                    ), (
                        "Interim_ADR_Records row "
                        f"{actual['record_id']!r} retrieved by "
                        f"backlog_adr_id={probe!r} has "
                        "motivating_requirement="
                        f"{actual['motivating_requirement']!r}; "
                        "INTERIM_ADR_SEED_ROWS declares "
                        f"{seed_row.motivating_requirement!r}."
                    )
                    assert actual["motivating_criterion"] == (
                        seed_row.motivating_criterion
                    ), (
                        "Interim_ADR_Records row "
                        f"{actual['record_id']!r} retrieved by "
                        f"backlog_adr_id={probe!r} has "
                        "motivating_criterion="
                        f"{actual['motivating_criterion']!r}; "
                        "INTERIM_ADR_SEED_ROWS declares "
                        f"{seed_row.motivating_criterion!r}."
                    )
                    assert actual["observable_behavior"] == (
                        seed_row.observable_behavior
                    ), (
                        "Interim_ADR_Records row "
                        f"{actual['record_id']!r} retrieved by "
                        f"backlog_adr_id={probe!r} has "
                        "observable_behavior="
                        f"{actual['observable_behavior']!r}; "
                        "INTERIM_ADR_SEED_ROWS declares "
                        f"{seed_row.observable_behavior!r}."
                    )
                    assert actual["backlog_adr_id"] == seed_row.backlog_adr_id, (
                        "Interim_ADR_Records row "
                        f"{actual['record_id']!r} retrieved by "
                        f"backlog_adr_id={probe!r} has backlog_adr_id="
                        f"{actual['backlog_adr_id']!r}; "
                        "INTERIM_ADR_SEED_ROWS declares "
                        f"{seed_row.backlog_adr_id!r}. The query filter "
                        "must not leak rows naming a different backlog "
                        "ADR identifier."
                    )

                    # ----- Assertion 3 — Recorded date present --------
                    # Every returned row carries a non-empty ISO-8601
                    # ``recorded_at`` (Requirement 16.3 — "recorded
                    # date of the choice"). The exact value is the
                    # ISO-millisecond rendering of the per-case
                    # :class:`FixedClock`; pinning the clock makes
                    # this assertion exact rather than approximate.
                    assert actual["recorded_at"] == _FIXED_NOW_ISO, (
                        "Interim_ADR_Records row "
                        f"{actual['record_id']!r} retrieved by "
                        f"backlog_adr_id={probe!r} has recorded_at="
                        f"{actual['recorded_at']!r}; the per-case "
                        f"FixedClock should produce {_FIXED_NOW_ISO!r}."
                    )
        finally:
            engine.dispose()
