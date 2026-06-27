"""Unit tests for :mod:`walking_slice.planning._interim_adr` (task 13.2).

These tests pin the contract established in task 13.1, design
§"Architectural Decisions" (AD-WS-15 through AD-WS-19),
:ref:`requirements.md` §"Identified gaps" (Gaps G-6 through G-10), and
Requirement 19.5 / 21.3:

- :func:`walking_slice.planning._interim_adr.seed_planning_interim_adr`
  inserts one ``Interim_ADR_Records`` row per Slice 2 Gap G-6..G-10
  (five rows total) carrying the documented motivating Requirement
  number, motivating criterion, observable behavior, recorded date,
  and backlog ADR identifier.
- The five rows attach to the backlog ADR identifiers enumerated by
  Property 26 (task 16.11):
  ``{ADR-HT-006, ADR-HT-009, ADR-HT-010, ADR-HT-011, ADR-HT-012}``.
- Re-running :func:`seed_planning_interim_adr` is idempotent: row
  counts are unchanged and row contents are byte-equivalent across
  observation points (Requirement 19.5; Property 26 byte-equivalence).
- The Slice 1 rows from :mod:`walking_slice.interim_adr` are also
  present after :func:`seed_planning_interim_adr` runs, because the
  Slice 2 seeder calls the Slice 1 seeder first per its implementation.

The fixture surface mirrors :mod:`tests.unit.test_interim_adr`: a per-test
SQLite engine from ``conftest.py``, with the Slice 1 schema installed via
:func:`walking_slice.persistence.create_schema` (which creates
``Interim_ADR_Records``). The Slice 2 schema is not required because the
seeder only writes to the Slice 1 table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.interim_adr import INTERIM_ADR_SEED_ROWS
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
    """Return every ``Interim_ADR_Records`` row as a list of dicts."""
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


# Property 26 enumeration of Slice 2 backlog ADR identifiers (task 16.11
# / design §"Correctness Properties → Property 26"). Aligned with
# AD-WS-15..AD-WS-19 (Gaps G-6 through G-10).
_SLICE2_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-006", "ADR-HT-009", "ADR-HT-010", "ADR-HT-011", "ADR-HT-012"}
)

# Slice 1 backlog ADR identifiers from Property 15 / AD-WS-6..AD-WS-10.
# The Slice 2 seeder calls the Slice 1 seed first, so these rows must
# also be present after :func:`seed_planning_interim_adr` runs.
_SLICE1_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-002", "ADR-HT-003", "ADR-HT-004", "ADR-HT-005", "ADR-HT-008"}
)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestSeedInsertsAllFiveSlice2Rows:
    """:func:`seed_planning_interim_adr` writes one row per Gap G-6..G-10
    (Requirement 19.5 / 21.3 / 16.3)."""

    def test_seeds_exactly_five_slice2_rows(self, engine: Engine) -> None:
        """Five Slice 2 rows — one per Gap G-6, G-7, G-8, G-9, G-10."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice2_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE2_BACKLOG_IDS
        ]
        assert len(slice2_rows) == 5

    def test_covers_all_five_slice2_backlog_adr_identifiers(
        self, engine: Engine
    ) -> None:
        """The Slice 2 backlog ADR identifiers match the Property 26 set."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice2_ids = {
            row["backlog_adr_id"]
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE2_BACKLOG_IDS
        }
        assert slice2_ids == _SLICE2_BACKLOG_IDS

    def test_each_slice2_backlog_adr_id_has_at_least_one_row(
        self, engine: Engine
    ) -> None:
        """Property 26 / Requirement 21.3 — every backlog ADR identifier in
        the enumerated set is retrievable by ``backlog_adr_id``."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE2_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert rows, (
                f"Expected at least one row for {backlog_adr_id}; got none"
            )

    def test_each_slice2_backlog_adr_id_has_exactly_one_row(
        self, engine: Engine
    ) -> None:
        """The Slice 2 seeder writes one and only one interim decision per
        backlog ADR identifier, matching the Slice 1 shape."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE2_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert len(rows) == 1, (
                f"Expected exactly one row for {backlog_adr_id}; got {len(rows)}"
            )


class TestSeedRowContents:
    """Per Requirement 16.3 / 21.3, each row records motivating_requirement,
    motivating_criterion, observable_behavior, recorded_at, and
    backlog_adr_id. None of those columns may be empty."""

    def test_every_slice2_row_has_non_empty_required_fields(
        self, engine: Engine
    ) -> None:
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        slice2_rows = [
            row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE2_BACKLOG_IDS
        ]

        assert len(slice2_rows) == 5
        for row in slice2_rows:
            assert row["motivating_requirement"], row
            assert row["motivating_criterion"], row
            assert row["observable_behavior"], row
            assert row["recorded_at"], row
            assert row["backlog_adr_id"] in _SLICE2_BACKLOG_IDS, row

    def test_g6_row_attaches_to_adr_ht_006_and_review_authority(
        self, engine: Engine
    ) -> None:
        """G-6 → AD-WS-15 → ADR-HT-006 (additive ``review`` authority)."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-006")

        assert len(rows) == 1
        row = rows[0]
        assert "G-6" in row["motivating_requirement"]
        assert "11.1" in row["motivating_requirement"]
        assert "review" in row["observable_behavior"]

    def test_g7_row_attaches_to_adr_ht_009_and_disclosure_coverage(
        self, engine: Engine
    ) -> None:
        """G-7 → AD-WS-16 → ADR-HT-009 (Disclosure_Policy_Coverage table)."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-009")

        assert len(rows) == 1
        row = rows[0]
        assert "G-7" in row["motivating_requirement"]
        assert "17.1" in row["motivating_requirement"]
        assert "Disclosure_Policy_Coverage" in row["observable_behavior"]

    def test_g8_row_attaches_to_adr_ht_010_and_relates_to_semantic_role(
        self, engine: Engine
    ) -> None:
        """G-8 → AD-WS-17 → ADR-HT-010 (Plan Review ``Relates To`` +
        ``semantic_role`` discriminator)."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-010")

        assert len(rows) == 1
        row = rows[0]
        assert "G-8" in row["motivating_requirement"]
        assert "8.1" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        assert "Relates To" in observable
        assert "semantic_role" in observable
        assert "review" in observable

    def test_g9_row_attaches_to_adr_ht_011_and_lifecycle_states(
        self, engine: Engine
    ) -> None:
        """G-9 → AD-WS-18 → ADR-HT-011 (Plan Revision lifecycle states)."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-011")

        assert len(rows) == 1
        row = rows[0]
        assert "G-9" in row["motivating_requirement"]
        assert "7.1" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The enumeration {draft, approved} must be observable.
        assert "draft" in observable
        assert "approved" in observable

    def test_g10_row_attaches_to_adr_ht_012_and_per_resource_tables(
        self, engine: Engine
    ) -> None:
        """G-10 → AD-WS-19 → ADR-HT-012 (per-Resource-kind tables +
        append-only triggers, plus the scoped Plan_Revisions exception)."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-012")

        assert len(rows) == 1
        row = rows[0]
        assert "G-10" in row["motivating_requirement"]
        observable = row["observable_behavior"]
        # The observable behavior enumerates the per-Resource-kind tables
        # and the Plan_Revisions session-pragma trigger exception.
        assert "Plan_Revisions" in observable
        assert "plan_approval_in_progress" in observable


class TestSeedIncludesSlice1Rows:
    """The Slice 2 seeder delegates to the Slice 1 seeder first so the
    five Slice 1 rows are present alongside the five Slice 2 rows
    regardless of which startup entrypoint the application chooses.
    Requirement 19.5 (additive only) implies the Slice 1 surface is
    preserved verbatim."""

    def test_slice1_rows_are_present_after_slice2_seed(
        self, engine: Engine
    ) -> None:
        """All five Slice 1 backlog ADR identifiers remain retrievable."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        for backlog_adr_id in _SLICE1_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert len(rows) == 1, (
                f"Expected exactly one Slice 1 row for {backlog_adr_id}; "
                f"got {len(rows)}"
            )

    def test_total_row_count_is_ten(self, engine: Engine) -> None:
        """Five Slice 1 rows + five Slice 2 rows = ten total."""
        create_schema(engine)
        seed_planning_interim_adr(
            engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        )

        rows = _all_rows(engine)
        assert len(rows) == 5 + 5


class TestSeedIdempotence:
    """Re-running :func:`seed_planning_interim_adr` must leave the
    ``Interim_ADR_Records`` table byte-equivalent (Requirement 19.5;
    Property 26 byte-equivalence across observation points). The FastAPI
    startup hook from task 15.2 relies on this so multiple processes can
    safely call the seeder against the same SQLite file."""

    def test_two_seed_calls_leave_table_byte_equivalent(
        self, engine: Engine
    ) -> None:
        """A second seed call (with a later clock) leaves rows unchanged."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_planning_interim_adr(engine, clock=first_clock)
        first_snapshot = _all_rows(engine)

        # Run again with a *later* clock — INSERT OR IGNORE must not
        # overwrite the originally recorded date on either Slice 1 or
        # Slice 2 rows.
        later_clock = FixedClock(
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)
        )
        seed_planning_interim_adr(engine, clock=later_clock)

        second_snapshot = _all_rows(engine)
        assert second_snapshot == first_snapshot

    def test_repeated_seed_calls_do_not_grow_the_table(
        self, engine: Engine
    ) -> None:
        """Calling seed five times still leaves exactly ten rows."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

        for _ in range(5):
            seed_planning_interim_adr(engine, clock=clock)

        rows = _all_rows(engine)
        assert len(rows) == 10

    def test_recorded_at_of_first_seed_is_preserved(
        self, engine: Engine
    ) -> None:
        """The originally recorded date wins on every subsequent call."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_planning_interim_adr(engine, clock=first_clock)
        original_dates = {
            row["record_id"]: row["recorded_at"] for row in _all_rows(engine)
        }

        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        seed_planning_interim_adr(engine, clock=later_clock)

        # Every row's recorded_at is byte-equivalent to the first call's value.
        for row in _all_rows(engine):
            assert row["recorded_at"] == original_dates[row["record_id"]], row

    def test_seed_uses_system_clock_when_clock_omitted(
        self, engine: Engine
    ) -> None:
        """Calling seed without a clock argument still populates the table.

        Task 15.2 calls ``seed_planning_interim_adr(engine)`` from FastAPI
        startup without passing a clock, so the default branch must work
        end-to-end.
        """
        create_schema(engine)

        seed_planning_interim_adr(engine)

        rows = _all_rows(engine)
        assert len(rows) == 10
        for row in rows:
            assert isinstance(row["recorded_at"], str)
            assert len(row["recorded_at"]) >= len("2026-01-01")


class TestSeedRowsModuleConstant:
    """:data:`PLANNING_INTERIM_ADR_SEED_ROWS` mirrors the Slice 2 portion
    of the table. Property 26 (task 16.11) is expected to derive its
    expected row set from this module-level constant, so the constant
    must match what :func:`seed_planning_interim_adr` writes."""

    def test_module_constant_lists_five_slice2_rows(self) -> None:
        assert len(PLANNING_INTERIM_ADR_SEED_ROWS) == 5

    def test_module_constant_covers_all_five_slice2_backlog_adr_ids(
        self,
    ) -> None:
        ids = {row.backlog_adr_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        assert ids == _SLICE2_BACKLOG_IDS

    def test_module_constant_record_ids_are_unique(self) -> None:
        record_ids = [row.record_id for row in PLANNING_INTERIM_ADR_SEED_ROWS]
        assert len(set(record_ids)) == len(record_ids)

    def test_slice1_and_slice2_record_ids_are_disjoint(self) -> None:
        """The Slice 2 record_ids (``ad-ws-15``..``ad-ws-19``) must not
        collide with the Slice 1 record_ids (``ad-ws-6``..``ad-ws-10``)
        so the primary-key ``INSERT OR IGNORE`` semantics remain
        non-overlapping between slices (Requirement 19.1)."""
        slice1_ids = {row.record_id for row in INTERIM_ADR_SEED_ROWS}
        slice2_ids = {row.record_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        assert slice1_ids.isdisjoint(slice2_ids)

    def test_slice1_and_slice2_backlog_adr_ids_are_disjoint(self) -> None:
        """The Slice 2 backlog ADR identifiers must not overlap with the
        Slice 1 enumeration (Requirement 19.1 — Slice 2 additions stand
        alongside the Slice 1 surface, not on top of it)."""
        slice1_ids = {row.backlog_adr_id for row in INTERIM_ADR_SEED_ROWS}
        slice2_ids = {row.backlog_adr_id for row in PLANNING_INTERIM_ADR_SEED_ROWS}
        assert slice1_ids.isdisjoint(slice2_ids)


class TestSeedDoesNotMutateSlice1Rows:
    """Requirement 19.1 / 19.5 — the Slice 2 seeder must be strictly
    additive: an existing Slice 1 row (already inserted by a prior call
    to ``walking_slice.interim_adr.seed``) must remain byte-equivalent
    after the Slice 2 seeder runs."""

    def test_slice1_rows_are_byte_equivalent_when_seeded_first(
        self, engine: Engine
    ) -> None:
        """Seed Slice 1 first, then Slice 2, and verify Slice 1 rows
        unchanged (no churn on ``recorded_at`` or any other column)."""
        from walking_slice.interim_adr import seed as seed_slice1

        create_schema(engine)
        slice1_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed_slice1(engine, clock=slice1_clock)
        slice1_snapshot = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE1_BACKLOG_IDS
        }

        # Run the Slice 2 seeder with a later clock — Slice 1 rows must
        # not be touched.
        later_clock = FixedClock(
            datetime(2027, 6, 15, 12, 30, tzinfo=timezone.utc)
        )
        seed_planning_interim_adr(engine, clock=later_clock)

        after = {
            row["record_id"]: row
            for row in _all_rows(engine)
            if row["backlog_adr_id"] in _SLICE1_BACKLOG_IDS
        }
        assert after == slice1_snapshot
