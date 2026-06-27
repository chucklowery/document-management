"""Unit tests for :mod:`walking_slice.outcome._interim_adr` (task 12.2).

These tests pin the contract established in task 12.1, design
§"Architectural Decisions" (AD-WS-33 through AD-WS-38),
:ref:`requirements.md` §"Gaps Flagged for Resolution" (Gaps G-16 through
G-20), and Requirements 60.4 / 60.5:

- :func:`walking_slice.outcome._interim_adr.seed_outcome_interim_adr`
  inserts one ``Interim_ADR_Records`` row per Slice 4 Gap G-16..G-20
  (five rows total) carrying the documented motivating Requirement
  number, motivating criterion, observable behavior, recorded date,
  and backlog ADR identifier.
- The five rows attach to the backlog ADR identifiers enumerated by
  Requirement 60.4:
  ``{ADR-HT-018, ADR-HT-019, ADR-HT-020, ADR-HT-021, ADR-HT-022}``.
- The ``ADR-HT-018`` row records the chosen ``{native, imported}`` origin
  member set and the
  ``{authoritative, replica, projection, index, federation}``
  source-system authority member set (AD-WS-38).
- Re-running :func:`seed_outcome_interim_adr` is idempotent: row counts
  are unchanged and row contents are byte-equivalent across observation
  points (Requirement 60.4 / 60.5 byte-equivalence).
- Existing Slice 1 + Slice 2 + Slice 3 ``Interim_ADR_Records`` rows are
  byte-equivalent after the Slice 4 seeder runs (Requirement 60.5 —
  additive only; Slice 4 actions never mutate prior
  ``Interim_ADR_Records`` rows). The Slice 4 seeder does *not* delegate
  to any prior-slice seeder; this fixture invokes them explicitly to
  reproduce the production startup hook's wiring (task 13.2).

The seeder accepts a :class:`~sqlalchemy.engine.Connection` (rather than
an :class:`~sqlalchemy.engine.Engine`) so the writes participate in the
caller's transaction (matching the
:func:`walking_slice.outcome._disclosure.seed_outcome_coverage`
signature). Every Slice 4 invocation in this module is wrapped in
``engine.begin()`` so the connection-scoped transaction is committed.

The fixture surface mirrors :mod:`tests.unit.test_execution_interim_adr`:
a per-test SQLite engine from ``conftest.py``, with the Slice 1 schema
installed via :func:`walking_slice.persistence.create_schema` (which
creates ``Interim_ADR_Records``). Neither the Slice 2 planning schema,
the Slice 3 execution schema, nor the Slice 4 outcome schema is required
because the seeder only writes to the Slice 1 table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.clock import FixedClock
from walking_slice.execution._interim_adr import (
    EXECUTION_INTERIM_ADR_SEED_ROWS,
    seed_execution_interim_adr,
)
from walking_slice.interim_adr import (
    INTERIM_ADR_SEED_ROWS,
    seed as seed_slice1_interim_adr,
)
from walking_slice.outcome._interim_adr import (
    OUTCOME_INTERIM_ADR_SEED_ROWS,
    seed_outcome_interim_adr,
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


def _run_slice4_seed(engine: Engine, *, clock: FixedClock | None = None) -> None:
    """Open a transaction and invoke the Slice 4 seeder.

    Mirrors the production startup hook (task 13.2): the seeder shares
    the caller's ``engine.begin()`` transaction so a partial bootstrap
    is rolled back. Every test in this module routes through this helper
    so the connection-scoped signature contract is honored.
    """
    with engine.begin() as conn:
        assert isinstance(conn, Connection)
        if clock is None:
            seed_outcome_interim_adr(conn)
        else:
            seed_outcome_interim_adr(conn, clock=clock)


# Requirement 60.4 enumeration of Slice 4 backlog ADR identifiers, aligned
# with AD-WS-38 / AD-WS-33 / AD-WS-34 / AD-WS-35 / AD-WS-36 (Gaps G-16
# through G-20).
_SLICE4_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-018", "ADR-HT-019", "ADR-HT-020", "ADR-HT-021", "ADR-HT-022"}
)

# Slice 1 backlog ADR identifiers from Property 15 / AD-WS-6..AD-WS-10.
_SLICE1_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-002", "ADR-HT-003", "ADR-HT-004", "ADR-HT-005", "ADR-HT-008"}
)

# Slice 2 backlog ADR identifiers from Property 26 / AD-WS-15..AD-WS-19.
_SLICE2_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-006", "ADR-HT-009", "ADR-HT-010", "ADR-HT-011", "ADR-HT-012"}
)

# Slice 3 backlog ADR identifiers from Property 45 / AD-WS-24..AD-WS-28.
_SLICE3_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-013", "ADR-HT-014", "ADR-HT-015", "ADR-HT-016", "ADR-HT-017"}
)


def _seed_all_prior_slices(engine: Engine, *, clock: FixedClock) -> None:
    """Seed Slice 1 + Slice 2 + Slice 3 rows, mirroring the startup hook.

    ``seed_planning_interim_adr`` invokes the Slice 1 seeder internally, so
    one call seeds the ten Slice 1 + Slice 2 rows. The Slice 3 seeder is
    connection-scoped and does not delegate, so it is run in its own
    ``engine.begin()`` block.
    """
    seed_planning_interim_adr(engine, clock=clock)
    with engine.begin() as conn:
        seed_execution_interim_adr(conn, clock=clock)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestSeedInsertsAllFiveSlice4Rows:
    """:func:`seed_outcome_interim_adr` writes one row per Gap G-16..G-20
    (Requirements 60.4 / 60.5)."""

    def test_seeds_exactly_five_slice4_rows(self, engine: Engine) -> None:
        """Five Slice 4 rows — one per Gap G-16, G-17, G-18, G-19, G-20."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice4_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS
        ]
        assert len(slice4_rows) == 5

    def test_covers_all_five_slice4_backlog_adr_identifiers(
        self, engine: Engine
    ) -> None:
        """The Slice 4 backlog ADR identifiers match the Requirement 60.4
        set ``{ADR-HT-018..ADR-HT-022}``."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice4_ids = {
            row["backlog_adr_id"]
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS
        }
        assert slice4_ids == _SLICE4_BACKLOG_IDS

    def test_each_slice4_backlog_adr_id_has_at_least_one_row(
        self, engine: Engine
    ) -> None:
        """Requirement 60.4 — every backlog ADR identifier in the
        enumerated set is retrievable by ``backlog_adr_id``."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE4_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert rows, (
                f"Expected at least one row for {backlog_adr_id}; got none"
            )

    def test_each_slice4_backlog_adr_id_has_exactly_one_row(
        self, engine: Engine
    ) -> None:
        """The Slice 4 seeder writes exactly one interim decision per
        backlog ADR identifier, matching the Slice 1 / 2 / 3 shape."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE4_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert len(rows) == 1, (
                f"Expected exactly one row for {backlog_adr_id}; got {len(rows)}"
            )


class TestSeedRowContents:
    """Per Requirement 60.4, each row records motivating_requirement,
    motivating_criterion, observable_behavior, recorded_at, and
    backlog_adr_id. None of those columns may be empty, and the two
    backlog-ADR columns reserved for the future replacement
    (``resolved_by_adr_id`` / ``resolved_at``) are left NULL because no
    Slice 4 backlog ADR has been Accepted yet."""

    def test_every_slice4_row_has_non_empty_required_fields(
        self, engine: Engine
    ) -> None:
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice4_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS
        ]

        assert len(slice4_rows) == 5
        for row in slice4_rows:
            assert row["motivating_requirement"], row
            assert row["motivating_criterion"], row
            assert row["observable_behavior"], row
            assert row["recorded_at"], row
            assert row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS, row

    def test_slice4_rows_leave_resolution_columns_null(
        self, engine: Engine
    ) -> None:
        """No Slice 4 backlog ADR is Accepted yet, so ``resolved_by_adr_id``
        and ``resolved_at`` are NULL on every seeded row (Requirement
        60.3 / 60.4 — interim behavior shipped ahead of the ADR)."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for row in _all_rows(engine):
            if row["backlog_adr_id"] not in _SLICE4_BACKLOG_IDS:
                continue
            assert row["resolved_by_adr_id"] is None, row
            assert row["resolved_at"] is None, row

    def test_g16_row_attaches_to_adr_ht_018_and_records_member_sets(
        self, engine: Engine
    ) -> None:
        """G-16 → AD-WS-38 → ADR-HT-018 records the chosen origin member
        set ``{native, imported}`` and the source-system authority member
        set ``{authoritative, replica, projection, index, federation}``."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-018")

        assert len(rows) == 1
        row = rows[0]
        assert "G-16" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The origin enumeration member set.
        assert "native" in observable
        assert "imported" in observable
        # The source-system authority enumeration member set.
        assert "authoritative" in observable
        assert "replica" in observable
        assert "projection" in observable
        assert "index" in observable
        assert "federation" in observable

    def test_g17_row_attaches_to_adr_ht_019_and_authority_enumeration(
        self, engine: Engine
    ) -> None:
        """G-17 → AD-WS-33 → ADR-HT-019 (additive Slice 4 authority values)."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-019")

        assert len(rows) == 1
        row = rows[0]
        assert "G-17" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must enumerate the four new authority values.
        assert "define_measurement" in observable
        assert "record_measurement" in observable
        assert "assess_outcome" in observable
        assert "issue_outcome_review" in observable

    def test_g18_row_attaches_to_adr_ht_020_and_disclosure_coverage(
        self, engine: Engine
    ) -> None:
        """G-18 → AD-WS-34 → ADR-HT-020 (additive disclosure-policy extension
        with per-attribute restriction for imported Measurement Records)."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-020")

        assert len(rows) == 1
        row = rows[0]
        assert "G-18" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        assert "Disclosure_Policy_Coverage" in observable
        assert "measurement_record" in observable

    def test_g19_row_attaches_to_adr_ht_021_and_relationship_semantics(
        self, engine: Engine
    ) -> None:
        """G-19 → AD-WS-35 → ADR-HT-021 (Slice 4 Relationship Types and
        semantic-role markers)."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-021")

        assert len(rows) == 1
        row = rows[0]
        assert "G-19" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must enumerate the canonical Relationship
        # Types and the semantic-role markers introduced by AD-WS-35.
        assert "Addresses" in observable
        assert "Cites" in observable
        assert "semantic_role" in observable
        assert "measurement_basis" in observable

    def test_g20_row_attaches_to_adr_ht_022_and_append_only_per_kind_tables(
        self, engine: Engine
    ) -> None:
        """G-20 → AD-WS-36 / AD-WS-37 → ADR-HT-022 (append-only per-kind
        tables, predecessor chain, and the seven new
        ``Identifier_Registry.resource_kind`` values)."""
        create_schema(engine)
        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-022")

        assert len(rows) == 1
        row = rows[0]
        assert "G-20" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior must call out UPDATE / DELETE rejection,
        # the predecessor chain, and the additive resource_kind values.
        assert "UPDATE" in observable
        assert "DELETE" in observable
        assert "predecessor_revision_id" in observable
        assert "Identifier_Registry" in observable
        assert "resource_kind" in observable


class TestSeedIdempotence:
    """Re-running :func:`seed_outcome_interim_adr` must leave the
    ``Interim_ADR_Records`` table byte-equivalent (Requirement 60.4 /
    60.5 byte-equivalence across observation points). The FastAPI startup
    hook from task 13.2 relies on this so multiple processes can safely
    call the seeder against the same SQLite file."""

    def test_two_seed_calls_leave_table_byte_equivalent(
        self, engine: Engine
    ) -> None:
        """A second seed call (with a later clock) leaves rows unchanged."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _run_slice4_seed(engine, clock=first_clock)
        first_snapshot = _all_rows(engine)

        # Run again with a *later* clock — INSERT OR IGNORE must not
        # overwrite the originally recorded date.
        later_clock = FixedClock(
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)
        )
        _run_slice4_seed(engine, clock=later_clock)

        second_snapshot = _all_rows(engine)
        assert second_snapshot == first_snapshot

    def test_repeated_seed_calls_do_not_grow_the_table(
        self, engine: Engine
    ) -> None:
        """Calling seed five times still leaves exactly five Slice 4 rows."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

        for _ in range(5):
            _run_slice4_seed(engine, clock=clock)

        slice4_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS
        ]
        assert len(slice4_rows) == 5

    def test_recorded_at_of_first_seed_is_preserved(
        self, engine: Engine
    ) -> None:
        """The originally recorded date wins on every subsequent call."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _run_slice4_seed(engine, clock=first_clock)
        original_dates = {
            row["record_id"]: row["recorded_at"]
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS
        }

        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice4_seed(engine, clock=later_clock)

        # Every Slice 4 row's recorded_at is byte-equivalent to the
        # first call's value.
        for row in _all_rows(engine):
            if row["backlog_adr_id"] not in _SLICE4_BACKLOG_IDS:
                continue
            assert row["recorded_at"] == original_dates[row["record_id"]], row

    def test_seed_uses_system_clock_when_clock_omitted(
        self, engine: Engine
    ) -> None:
        """Calling seed without a clock argument still populates the table.

        Task 13.2 calls ``seed_outcome_interim_adr(conn)`` from the
        FastAPI startup hook without passing a clock, so the default
        branch must work end-to-end.
        """
        create_schema(engine)

        _run_slice4_seed(engine)

        slice4_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE4_BACKLOG_IDS
        ]
        assert len(slice4_rows) == 5
        for row in slice4_rows:
            assert isinstance(row["recorded_at"], str)
            assert len(row["recorded_at"]) >= len("2026-01-01")


class TestSeedRowsModuleConstant:
    """:data:`OUTCOME_INTERIM_ADR_SEED_ROWS` mirrors the Slice 4 portion of
    the table. Property 60 is expected to derive its expected row set from
    this module-level constant, so the constant must match what
    :func:`seed_outcome_interim_adr` writes."""

    def test_module_constant_lists_five_slice4_rows(self) -> None:
        assert len(OUTCOME_INTERIM_ADR_SEED_ROWS) == 5

    def test_module_constant_covers_all_five_slice4_backlog_adr_ids(
        self,
    ) -> None:
        ids = {row.backlog_adr_id for row in OUTCOME_INTERIM_ADR_SEED_ROWS}
        assert ids == _SLICE4_BACKLOG_IDS

    def test_module_constant_record_ids_are_unique(self) -> None:
        record_ids = [row.record_id for row in OUTCOME_INTERIM_ADR_SEED_ROWS]
        assert len(set(record_ids)) == len(record_ids)

    def test_slice_record_ids_are_pairwise_disjoint(self) -> None:
        """The Slice 4 record_ids must not collide with the Slice 1
        (``ad-ws-6``..``ad-ws-10``), Slice 2 (``ad-ws-15``..``ad-ws-19``),
        or Slice 3 (``ad-ws-24``..``ad-ws-28``) record_ids so the
        primary-key ``INSERT OR IGNORE`` semantics remain non-overlapping
        between slices (Requirement 60.5)."""
        slice1_ids = {row.record_id for row in INTERIM_ADR_SEED_ROWS}
        slice2_ids = {row.record_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        slice3_ids = {row.record_id for row in EXECUTION_INTERIM_ADR_SEED_ROWS}
        slice4_ids = {row.record_id for row in OUTCOME_INTERIM_ADR_SEED_ROWS}
        assert slice4_ids.isdisjoint(slice1_ids)
        assert slice4_ids.isdisjoint(slice2_ids)
        assert slice4_ids.isdisjoint(slice3_ids)

    def test_slice_backlog_adr_ids_are_pairwise_disjoint(self) -> None:
        """The Slice 4 backlog ADR identifiers must not overlap with the
        Slice 1, Slice 2, or Slice 3 enumerations (Requirement 60.5 —
        Slice 4 additions stand alongside the prior-slice surfaces, not on
        top of them)."""
        slice1_ids = {row.backlog_adr_id for row in INTERIM_ADR_SEED_ROWS}
        slice2_ids = {row.backlog_adr_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        slice3_ids = {
            row.backlog_adr_id for row in EXECUTION_INTERIM_ADR_SEED_ROWS
        }
        slice4_ids = {row.backlog_adr_id for row in OUTCOME_INTERIM_ADR_SEED_ROWS}
        assert slice4_ids.isdisjoint(slice1_ids)
        assert slice4_ids.isdisjoint(slice2_ids)
        assert slice4_ids.isdisjoint(slice3_ids)


class TestSeedDoesNotMutatePriorSliceRows:
    """Requirement 60.5 — the Slice 4 seeder must be strictly additive:
    existing Slice 1, Slice 2, and Slice 3 rows (already inserted by prior
    seed calls) must remain byte-equivalent after the Slice 4 seeder runs.
    The Slice 4 seeder does not delegate to any prior-slice seeder; the
    production startup hook (task 13.2) is responsible for invoking all
    four seeders in order."""

    def test_slice1_rows_are_byte_equivalent_after_slice4_seed(
        self, engine: Engine
    ) -> None:
        """Seed Slice 1 first, then Slice 4, and verify Slice 1 rows
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

        # Run the Slice 4 seeder with a later clock — Slice 1 rows must
        # not be touched.
        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice4_seed(engine, clock=later_clock)

        after = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE1_BACKLOG_IDS
        }
        assert after == slice1_snapshot

    def test_all_prior_slice_rows_are_byte_equivalent_after_slice4_seed(
        self, engine: Engine
    ) -> None:
        """Seed Slice 1 + Slice 2 + Slice 3 together, snapshot every
        prior-slice row, run the Slice 4 seeder, and assert byte-equivalence
        of every Slice 1 + Slice 2 + Slice 3 row (Requirement 60.5)."""
        create_schema(engine)
        prior_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_all_prior_slices(engine, clock=prior_clock)

        prior_slice_ids = (
            _SLICE1_BACKLOG_IDS | _SLICE2_BACKLOG_IDS | _SLICE3_BACKLOG_IDS
        )
        before = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in prior_slice_ids
        }
        assert len(before) == 15  # five each for Slice 1 + 2 + 3

        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        _run_slice4_seed(engine, clock=later_clock)

        after = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in prior_slice_ids
        }
        assert after == before

    def test_total_row_count_after_all_four_seeds(self, engine: Engine) -> None:
        """Five Slice 1 + five Slice 2 + five Slice 3 + five Slice 4 =
        twenty total."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_all_prior_slices(engine, clock=clock)
        _run_slice4_seed(engine, clock=clock)

        rows = _all_rows(engine)
        assert len(rows) == 20

    def test_slice4_seed_does_not_create_prior_slice_rows_on_its_own(
        self, engine: Engine
    ) -> None:
        """The Slice 4 seeder is strictly additive: when invoked against a
        fresh schema (no prior-slice rows present), it writes only its own
        five rows. Slice 1 + 2 + 3 row presence is the responsibility of
        the production startup hook (task 13.2), not of this seeder.
        """
        create_schema(engine)

        _run_slice4_seed(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _all_rows(engine)
        assert len(rows) == 5
        backlog_ids = {row["backlog_adr_id"] for row in rows}
        assert backlog_ids == _SLICE4_BACKLOG_IDS

    def test_all_seeders_remain_idempotent_after_slice4_seed(
        self, engine: Engine
    ) -> None:
        """Running the Slice 1 / 2 / 3 seeders again after Slice 4 leaves
        every row byte-equivalent. Reconciles Requirement 60.5 (additive
        only) with the production startup hook's reseed-safe contract."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        _seed_all_prior_slices(engine, clock=clock)
        _run_slice4_seed(engine, clock=clock)
        first_snapshot = _all_rows(engine)

        # Re-run each seeder with a later clock; INSERT OR IGNORE against
        # the shared primary keys must not overwrite any row.
        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        seed_slice1_interim_adr(engine, clock=later_clock)
        _seed_all_prior_slices(engine, clock=later_clock)
        _run_slice4_seed(engine, clock=later_clock)

        second_snapshot = _all_rows(engine)
        assert second_snapshot == first_snapshot
