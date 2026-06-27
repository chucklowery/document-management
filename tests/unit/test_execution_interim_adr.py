"""Unit tests for :mod:`walking_slice.execution._interim_adr` (task 14.2).

These tests pin the contract established in task 14.1, design
§"Architectural Decisions" (AD-WS-24 through AD-WS-28),
:ref:`requirements.md` §"Identified gaps" (Gaps G-11 through G-15), and
Requirements 40.5 / 42.3 / 42.4:

- :func:`walking_slice.execution._interim_adr.seed_execution_interim_adr`
  inserts one ``Interim_ADR_Records`` row per Slice 3 Gap G-11..G-15
  (five rows total) carrying the documented motivating Requirement
  number, motivating criterion, observable behavior, recorded date,
  and backlog ADR identifier.
- The five rows attach to the backlog ADR identifiers enumerated by
  Property 45 (task 16.15):
  ``{ADR-HT-013, ADR-HT-014, ADR-HT-015, ADR-HT-016, ADR-HT-017}``.
- Re-running :func:`seed_execution_interim_adr` is idempotent: row
  counts are unchanged and row contents are byte-equivalent across
  observation points (Requirement 40.5; Property 45 byte-equivalence).
- Existing Slice 1 + Slice 2 ``Interim_ADR_Records`` rows are
  byte-equivalent after the Slice 3 seeder runs (Requirement 40.5 —
  additive only; Requirement 40.4 — Slice 3 actions never mutate prior
  ``Interim_ADR_Records`` rows). The Slice 3 seeder does *not* delegate
  to either prior-slice seeder; this fixture invokes them explicitly to
  reproduce the production startup hook's wiring (task 15.3).

The seeder accepts a :class:`~sqlalchemy.engine.Connection` (rather than
an :class:`~sqlalchemy.engine.Engine`) so the writes participate in the
caller's transaction (matching the
:func:`walking_slice.execution._disclosure.seed_execution_coverage`
signature). Every Slice 3 invocation in this module is wrapped in
``engine.begin()`` so the connection-scoped transaction is committed.

The fixture surface mirrors :mod:`tests.unit.test_planning_interim_adr`:
a per-test SQLite engine from ``conftest.py``, with the Slice 1 schema
installed via :func:`walking_slice.persistence.create_schema` (which
creates ``Interim_ADR_Records``). Neither the Slice 2 planning schema
nor the Slice 3 execution schema is required because the seeder only
writes to the Slice 1 table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.execution._interim_adr import (
    EXECUTION_INTERIM_ADR_SEED_ROWS,
    seed_execution_interim_adr,
)
from walking_slice.interim_adr import (
    INTERIM_ADR_SEED_ROWS,
    seed as seed_slice1_interim_adr,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._interim_adr import (
    PLANNING_INTERIM_ADR_SEED_ROWS,
    seed_planning_interim_adr,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _all_rows(engine: Engine) -> list[dict]:
    """Return every ``Interim_ADR_Records`` row as a list of dicts.

    Includes every column the seeder writes plus the ``resolved_by_adr_id``
    and ``resolved_at`` columns so byte-equivalence comparisons cover the
    full row.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT record_id, motivating_requirement, motivating_criterion,
                       observable_behavior, recorded_at, backlog_adr_id,
                       resolved_by_adr_id, resolved_at
                FROM Interim_ADR_Records
                ORDER BY record_id
                """
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def _rows_for(engine: Engine, *, backlog_adr_id: str) -> list[dict]:
    """Return every row attached to the given backlog ADR identifier."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT record_id, motivating_requirement, motivating_criterion,
                       observable_behavior, recorded_at, backlog_adr_id
                FROM Interim_ADR_Records
                WHERE backlog_adr_id = :id
                """
            ),
            {"id": backlog_adr_id},
        ).mappings().all()
    return [dict(row) for row in rows]


def _run_slice3_seed(engine: Engine, *, clock: FixedClock | None = None) -> None:
    """Open a transaction and invoke the Slice 3 seeder.

    Mirrors the production startup hook (task 15.3): the seeder shares
    the caller's ``engine.begin()`` transaction so a partial bootstrap
    is rolled back. Every test in this module routes through this helper
    so the connection-scoped signature contract is honored.
    """
    with engine.begin() as conn:
        if clock is None:
            seed_execution_interim_adr(conn)
        else:
            seed_execution_interim_adr(conn, clock=clock)


# Property 45 enumeration of Slice 3 backlog ADR identifiers (task 16.15
# / design §"Correctness Properties → Property 45"). Aligned with
# AD-WS-24..AD-WS-28 (Gaps G-11 through G-15).
_SLICE3_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-013", "ADR-HT-014", "ADR-HT-015", "ADR-HT-016", "ADR-HT-017"}
)

# Slice 1 backlog ADR identifiers from Property 15 / AD-WS-6..AD-WS-10.
_SLICE1_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-002", "ADR-HT-003", "ADR-HT-004", "ADR-HT-005", "ADR-HT-008"}
)

# Slice 2 backlog ADR identifiers from Property 26 / AD-WS-15..AD-WS-19.
_SLICE2_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-006", "ADR-HT-009", "ADR-HT-010", "ADR-HT-011", "ADR-HT-012"}
)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestSeedInsertsAllFiveSlice3Rows:
    """:func:`seed_execution_interim_adr` writes one row per Gap G-11..G-15
    (Requirements 40.5 / 42.3 / 42.4 / 16.3 reused)."""

    def test_seeds_exactly_five_slice3_rows(self, engine: Engine) -> None:
        """Five Slice 3 rows — one per Gap G-11, G-12, G-13, G-14, G-15."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice3_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS
        ]
        assert len(slice3_rows) == 5

    def test_covers_all_five_slice3_backlog_adr_identifiers(
        self, engine: Engine
    ) -> None:
        """The Slice 3 backlog ADR identifiers match the Property 45 set."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice3_ids = {
            row["backlog_adr_id"]
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS
        }
        assert slice3_ids == _SLICE3_BACKLOG_IDS

    def test_each_slice3_backlog_adr_id_has_at_least_one_row(
        self, engine: Engine
    ) -> None:
        """Property 45 / Requirement 42.3 — every backlog ADR identifier in
        the enumerated set is retrievable by ``backlog_adr_id``."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE3_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert rows, (
                f"Expected at least one row for {backlog_adr_id}; got none"
            )

    def test_each_slice3_backlog_adr_id_has_exactly_one_row(
        self, engine: Engine
    ) -> None:
        """The Slice 3 seeder writes exactly one interim decision per
        backlog ADR identifier, matching the Slice 1 / Slice 2 shape."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE3_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert len(rows) == 1, (
                f"Expected exactly one row for {backlog_adr_id}; got {len(rows)}"
            )


class TestSeedRowContents:
    """Per Requirement 16.3 / 42.3, each row records motivating_requirement,
    motivating_criterion, observable_behavior, recorded_at, and
    backlog_adr_id. None of those columns may be empty."""

    def test_every_slice3_row_has_non_empty_required_fields(
        self, engine: Engine
    ) -> None:
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice3_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS
        ]

        assert len(slice3_rows) == 5
        for row in slice3_rows:
            assert row["motivating_requirement"], row
            assert row["motivating_criterion"], row
            assert row["observable_behavior"], row
            assert row["recorded_at"], row
            assert row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS, row

    def test_g11_row_attaches_to_adr_ht_013_and_authority_enumeration(
        self, engine: Engine
    ) -> None:
        """G-11 → AD-WS-24 → ADR-HT-013 (additive Slice 3 authority values)."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-013")

        assert len(rows) == 1
        row = rows[0]
        assert "G-11" in row["motivating_requirement"]
        assert "32.1" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must enumerate the four new authority values.
        assert "assign" in observable
        assert "contribute" in observable
        assert "accept_milestone" in observable
        assert "complete" in observable

    def test_g12_row_attaches_to_adr_ht_014_and_disclosure_coverage(
        self, engine: Engine
    ) -> None:
        """G-12 → AD-WS-25 → ADR-HT-014 (additive disclosure-policy extension)."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-014")

        assert len(rows) == 1
        row = rows[0]
        assert "G-12" in row["motivating_requirement"]
        assert "38.1" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        assert "Disclosure_Policy_Coverage" in observable

    def test_g13_row_attaches_to_adr_ht_015_and_relationship_semantics(
        self, engine: Engine
    ) -> None:
        """G-13 → AD-WS-26 → ADR-HT-015 (Slice 3 Relationship Types and
        semantic-role markers)."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-015")

        assert len(rows) == 1
        row = rows[0]
        assert "G-13" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must enumerate the canonical Relationship
        # Types and the semantic-role markers introduced by AD-WS-26.
        assert "Addresses" in observable
        assert "Relates To" in observable
        assert "Produces" in observable
        assert "semantic_role" in observable

    def test_g14_row_attaches_to_adr_ht_016_and_append_only_no_supersession(
        self, engine: Engine
    ) -> None:
        """G-14 → AD-WS-27 → ADR-HT-016 (append-only-no-supersession stance)."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-016")

        assert len(rows) == 1
        row = rows[0]
        assert "G-14" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must call out UPDATE / DELETE rejection
        # and the absence of a lifecycle-supersession path.
        assert "UPDATE" in observable
        assert "DELETE" in observable

    def test_g15_row_attaches_to_adr_ht_017_and_per_record_tables(
        self, engine: Engine
    ) -> None:
        """G-15 → AD-WS-28 → ADR-HT-017 (per-Record-kind tables and the
        eight additive ``Identifier_Registry.resource_kind`` values)."""
        create_schema(engine)
        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-017")

        assert len(rows) == 1
        row = rows[0]
        assert "G-15" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must enumerate the per-Record-kind table
        # surface and the additive Identifier_Registry.resource_kind values.
        assert "Work_Assignment_Records" in observable
        assert "Identifier_Registry" in observable
        assert "resource_kind" in observable


class TestSeedIdempotence:
    """Re-running :func:`seed_execution_interim_adr` must leave the
    ``Interim_ADR_Records`` table byte-equivalent (Requirement 40.5;
    Property 45 byte-equivalence across observation points). The FastAPI
    startup hook from task 15.3 relies on this so multiple processes can
    safely call the seeder against the same SQLite file."""

    def test_two_seed_calls_leave_table_byte_equivalent(
        self, engine: Engine
    ) -> None:
        """A second seed call (with a later clock) leaves rows unchanged."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _run_slice3_seed(engine, clock=first_clock)
        first_snapshot = _all_rows(engine)

        # Run again with a *later* clock — INSERT OR IGNORE must not
        # overwrite the originally recorded date.
        later_clock = FixedClock(
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)
        )
        _run_slice3_seed(engine, clock=later_clock)

        second_snapshot = _all_rows(engine)
        assert second_snapshot == first_snapshot

    def test_repeated_seed_calls_do_not_grow_the_table(
        self, engine: Engine
    ) -> None:
        """Calling seed five times still leaves exactly five Slice 3 rows."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

        for _ in range(5):
            _run_slice3_seed(engine, clock=clock)

        slice3_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS
        ]
        assert len(slice3_rows) == 5

    def test_recorded_at_of_first_seed_is_preserved(
        self, engine: Engine
    ) -> None:
        """The originally recorded date wins on every subsequent call."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _run_slice3_seed(engine, clock=first_clock)
        original_dates = {
            row["record_id"]: row["recorded_at"]
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS
        }

        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice3_seed(engine, clock=later_clock)

        # Every Slice 3 row's recorded_at is byte-equivalent to the
        # first call's value.
        for row in _all_rows(engine):
            if row["backlog_adr_id"] not in _SLICE3_BACKLOG_IDS:
                continue
            assert row["recorded_at"] == original_dates[row["record_id"]], row

    def test_seed_uses_system_clock_when_clock_omitted(
        self, engine: Engine
    ) -> None:
        """Calling seed without a clock argument still populates the table.

        Task 15.3 calls ``seed_execution_interim_adr(conn)`` from the
        FastAPI startup hook without passing a clock, so the default
        branch must work end-to-end.
        """
        create_schema(engine)

        _run_slice3_seed(engine)

        slice3_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE3_BACKLOG_IDS
        ]
        assert len(slice3_rows) == 5
        for row in slice3_rows:
            assert isinstance(row["recorded_at"], str)
            assert len(row["recorded_at"]) >= len("2026-01-01")


class TestSeedRowsModuleConstant:
    """:data:`EXECUTION_INTERIM_ADR_SEED_ROWS` mirrors the Slice 3 portion
    of the table. Property 45 (task 16.15) is expected to derive its
    expected row set from this module-level constant, so the constant
    must match what :func:`seed_execution_interim_adr` writes."""

    def test_module_constant_lists_five_slice3_rows(self) -> None:
        assert len(EXECUTION_INTERIM_ADR_SEED_ROWS) == 5

    def test_module_constant_covers_all_five_slice3_backlog_adr_ids(
        self,
    ) -> None:
        ids = {row.backlog_adr_id for row in EXECUTION_INTERIM_ADR_SEED_ROWS}
        assert ids == _SLICE3_BACKLOG_IDS

    def test_module_constant_record_ids_are_unique(self) -> None:
        record_ids = [row.record_id for row in EXECUTION_INTERIM_ADR_SEED_ROWS]
        assert len(set(record_ids)) == len(record_ids)

    def test_slice_record_ids_are_pairwise_disjoint(self) -> None:
        """The Slice 3 record_ids (``ad-ws-24``..``ad-ws-28``) must not
        collide with the Slice 1 record_ids (``ad-ws-6``..``ad-ws-10``)
        or the Slice 2 record_ids (``ad-ws-15``..``ad-ws-19``) so the
        primary-key ``INSERT OR IGNORE`` semantics remain non-overlapping
        between slices (Requirements 40.1, 40.2, 40.5)."""
        slice1_ids = {row.record_id for row in INTERIM_ADR_SEED_ROWS}
        slice2_ids = {row.record_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        slice3_ids = {row.record_id for row in EXECUTION_INTERIM_ADR_SEED_ROWS}
        assert slice1_ids.isdisjoint(slice3_ids)
        assert slice2_ids.isdisjoint(slice3_ids)
        # Cross-check the Slice 1 / Slice 2 disjointness too so a future
        # collision in any direction shows up here.
        assert slice1_ids.isdisjoint(slice2_ids)

    def test_slice_backlog_adr_ids_are_pairwise_disjoint(self) -> None:
        """The Slice 3 backlog ADR identifiers must not overlap with the
        Slice 1 or Slice 2 enumerations (Requirement 40.5 — Slice 3
        additions stand alongside the prior-slice surfaces, not on top
        of them; Requirement 42.4 — disjoint extension)."""
        slice1_ids = {row.backlog_adr_id for row in INTERIM_ADR_SEED_ROWS}
        slice2_ids = {row.backlog_adr_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        slice3_ids = {
            row.backlog_adr_id for row in EXECUTION_INTERIM_ADR_SEED_ROWS
        }
        assert slice1_ids.isdisjoint(slice3_ids)
        assert slice2_ids.isdisjoint(slice3_ids)


class TestSeedDoesNotMutatePriorSliceRows:
    """Requirement 40.4 / 40.5 — the Slice 3 seeder must be strictly
    additive: existing Slice 1 and Slice 2 rows (already inserted by
    prior seed calls) must remain byte-equivalent after the Slice 3
    seeder runs. The Slice 3 seeder does not delegate to either
    prior-slice seeder; the production startup hook (task 15.3) is
    responsible for invoking all three seeders in order."""

    def test_slice1_rows_are_byte_equivalent_after_slice3_seed(
        self, engine: Engine
    ) -> None:
        """Seed Slice 1 first, then Slice 3, and verify Slice 1 rows
        unchanged (no churn on ``recorded_at`` or any other column)."""
        create_schema(engine)
        slice1_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_slice1_interim_adr(engine, clock=slice1_clock)
        slice1_snapshot = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE1_BACKLOG_IDS
        }
        assert len(slice1_snapshot) == 5

        # Run the Slice 3 seeder with a later clock — Slice 1 rows must
        # not be touched.
        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice3_seed(engine, clock=later_clock)

        after = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE1_BACKLOG_IDS
        }
        assert after == slice1_snapshot

    def test_slice2_rows_are_byte_equivalent_after_slice3_seed(
        self, engine: Engine
    ) -> None:
        """Seed Slice 2 (which also seeds Slice 1 internally), then Slice 3,
        and verify Slice 2 rows unchanged."""
        create_schema(engine)
        slice2_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_planning_interim_adr(engine, clock=slice2_clock)
        slice2_snapshot = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE2_BACKLOG_IDS
        }
        assert len(slice2_snapshot) == 5

        # Run the Slice 3 seeder with a later clock — Slice 2 rows must
        # not be touched.
        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice3_seed(engine, clock=later_clock)

        after = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE2_BACKLOG_IDS
        }
        assert after == slice2_snapshot

    def test_all_prior_slice_rows_are_byte_equivalent_after_slice3_seed(
        self, engine: Engine
    ) -> None:
        """Seed Slice 1 and Slice 2 together, snapshot every prior-slice
        row, run the Slice 3 seeder, and assert byte-equivalence of every
        Slice 1 + Slice 2 row (Requirement 40.5)."""
        create_schema(engine)
        prior_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        # ``seed_planning_interim_adr`` invokes the Slice 1 seeder
        # internally so a single call seeds all ten prior-slice rows.
        seed_planning_interim_adr(engine, clock=prior_clock)

        prior_slice_ids = _SLICE1_BACKLOG_IDS | _SLICE2_BACKLOG_IDS
        before = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in prior_slice_ids
        }
        assert len(before) == 10  # five Slice 1 + five Slice 2

        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice3_seed(engine, clock=later_clock)

        after = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in prior_slice_ids
        }
        assert after == before

    def test_total_row_count_after_all_three_seeds(self, engine: Engine) -> None:
        """Five Slice 1 + five Slice 2 + five Slice 3 = fifteen total."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_planning_interim_adr(engine, clock=clock)
        _run_slice3_seed(engine, clock=clock)

        rows = _all_rows(engine)
        assert len(rows) == 15

    def test_slice3_seed_does_not_create_slice1_or_slice2_rows_on_its_own(
        self, engine: Engine
    ) -> None:
        """The Slice 3 seeder is strictly additive: when invoked against a
        fresh schema (no prior-slice rows present), it writes only its
        own five rows. Slice 1 + Slice 2 row presence is the
        responsibility of the production startup hook (task 15.3), not
        of this seeder.
        """
        create_schema(engine)

        _run_slice3_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _all_rows(engine)
        assert len(rows) == 5
        backlog_ids = {row["backlog_adr_id"] for row in rows}
        assert backlog_ids == _SLICE3_BACKLOG_IDS

    def test_slice1_and_slice2_seeders_remain_idempotent_after_slice3_seed(
        self, engine: Engine
    ) -> None:
        """Running the Slice 1 / Slice 2 seeders again after Slice 3
        leaves every row byte-equivalent. Reconciles Requirement 40.5
        (additive only) with the production startup hook's reseed-safe
        contract."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_planning_interim_adr(engine, clock=clock)
        _run_slice3_seed(engine, clock=clock)
        first_snapshot = _all_rows(engine)

        # Re-run each seeder with a later clock; INSERT OR IGNORE
        # against the shared primary keys must not overwrite any row.
        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        seed_slice1_interim_adr(engine, clock=later_clock)
        seed_planning_interim_adr(engine, clock=later_clock)
        _run_slice3_seed(engine, clock=later_clock)

        second_snapshot = _all_rows(engine)
        assert second_snapshot == first_snapshot
