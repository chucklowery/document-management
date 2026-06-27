"""Unit tests for :mod:`walking_slice.interim_adr`.

These tests pin the contract established in task 13.1, design §"Architectural
Decisions" (AD-WS-6 through AD-WS-10), design §"Table-by-Table Specification —
``Interim_ADR_Records``", and Requirement 16.3:

- :func:`walking_slice.interim_adr.seed` inserts exactly one row per Gap
  G-1..G-5 (five rows total).
- Each row records ``motivating_requirement``, ``motivating_criterion``,
  ``observable_behavior``, ``recorded_at``, and ``backlog_adr_id`` per
  Requirement 16.3.
- The five rows attach to the backlog ADR identifiers listed in design
  §"Correctness Properties → Property 15": ``ADR-HT-002``, ``ADR-HT-003``,
  ``ADR-HT-004``, ``ADR-HT-005``, ``ADR-HT-008``.
- :func:`seed` is idempotent: repeated invocations leave the table
  byte-equivalent and never duplicate rows on the stable primary key.
- The lazy seed in :mod:`walking_slice.evidence` (which independently
  inserts the AD-WS-6 row) does not collide with or duplicate the row
  inserted here, because both code paths share the same primary key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.interim_adr import INTERIM_ADR_SEED_ROWS, seed
from walking_slice.persistence import create_schema


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


# Expected (Gap, AD-WS, backlog ADR) tuples from design AD-WS-6..10 and
# requirements.md §"Identified gaps".
_EXPECTED_BACKLOG_IDS: frozenset[str] = frozenset(
    {"ADR-HT-002", "ADR-HT-003", "ADR-HT-004", "ADR-HT-005", "ADR-HT-008"}
)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


class TestSeedInsertsAllFiveRows:
    """:func:`seed` writes one row per Gap G-1..G-5 (Requirement 16.3)."""

    def test_seed_inserts_exactly_five_rows(self, engine: Engine) -> None:
        """Five rows total — one per Gap G-1, G-2, G-3, G-4, G-5."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        rows = _all_rows(engine)

        assert len(rows) == 5

    def test_seed_covers_all_five_backlog_adr_identifiers(
        self, engine: Engine
    ) -> None:
        """The backlog ADR identifiers match the Property 15 enumeration."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        rows = _all_rows(engine)
        backlog_ids = {row["backlog_adr_id"] for row in rows}

        assert backlog_ids == _EXPECTED_BACKLOG_IDS

    def test_each_backlog_adr_id_has_exactly_one_row(
        self, engine: Engine
    ) -> None:
        """Property 15 expects "the complete set" to be retrievable; the
        slice records exactly one interim decision per backlog ADR, so
        each query returns one row."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        for backlog_adr_id in _EXPECTED_BACKLOG_IDS:
            rows = _rows_for(engine, backlog_adr_id=backlog_adr_id)
            assert len(rows) == 1, (
                f"Expected exactly one row for {backlog_adr_id}; got {len(rows)}"
            )


class TestSeedRowContents:
    """Per Requirement 16.3, each row records motivating_requirement,
    motivating_criterion, observable_behavior, recorded_at, and
    backlog_adr_id. None of those columns may be empty."""

    def test_every_row_has_non_empty_required_fields(
        self, engine: Engine
    ) -> None:
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        for row in _all_rows(engine):
            assert row["motivating_requirement"], row
            assert row["motivating_criterion"], row
            assert row["observable_behavior"], row
            assert row["recorded_at"], row
            assert row["backlog_adr_id"] in _EXPECTED_BACKLOG_IDS, row

    def test_g1_row_attaches_to_adr_ht_003_and_byte_offset_anchoring(
        self, engine: Engine
    ) -> None:
        """G-1 → AD-WS-6 → ADR-HT-003 per requirements.md and design."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-003")

        assert len(rows) == 1
        row = rows[0]
        assert "3.1" in row["motivating_requirement"]
        assert "3.2" in row["motivating_requirement"]
        assert "G-1" in row["motivating_requirement"]
        assert row["motivating_criterion"] == "byte-offset anchoring"

    def test_g4_row_attaches_to_adr_ht_008_with_slice_default_policy(
        self, engine: Engine
    ) -> None:
        """G-4 → AD-WS-9 → ADR-HT-008 (Completeness Disclosure policy)."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-008")

        assert len(rows) == 1
        row = rows[0]
        assert "10.5" in row["motivating_requirement"]
        assert "G-4" in row["motivating_requirement"]
        assert "slice-default-2026" in row["observable_behavior"]

    def test_g5_row_attaches_to_adr_ht_002_with_authority_basis_enum(
        self, engine: Engine
    ) -> None:
        """G-5 → AD-WS-10 → ADR-HT-002 (authority-basis enumeration)."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-002")

        assert len(rows) == 1
        row = rows[0]
        assert "G-5" in row["motivating_requirement"]
        # The observable behavior must enumerate the three values from AD-WS-10.
        observable = row["observable_behavior"]
        assert "role-grant-id" in observable
        assert "scope-id" in observable
        assert "delegation-chain-id" in observable


class TestSeedIdempotence:
    """:func:`seed` is safe to call multiple times (Requirement 16.3 — the
    row must remain *retrievable* across restarts, which implies repeated
    seeding from startup hooks must not duplicate or churn rows)."""

    def test_two_seed_calls_leave_table_byte_equivalent(
        self, engine: Engine
    ) -> None:
        """A second seed call produces the same rows as the first."""
        create_schema(engine)
        first_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        seed(engine, clock=first_clock)
        first_snapshot = _all_rows(engine)

        # Run again with a *later* clock — INSERT OR IGNORE must not
        # overwrite the originally recorded date.
        later_clock = FixedClock(
            datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)
        )
        seed(engine, clock=later_clock)

        second_snapshot = _all_rows(engine)
        assert second_snapshot == first_snapshot

    def test_repeated_seed_calls_do_not_grow_the_table(
        self, engine: Engine
    ) -> None:
        """Calling seed() five times still leaves exactly five rows."""
        create_schema(engine)
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

        for _ in range(5):
            seed(engine, clock=clock)

        rows = _all_rows(engine)
        assert len(rows) == 5

    def test_seed_uses_system_clock_when_clock_omitted(
        self, engine: Engine
    ) -> None:
        """Calling seed without a clock argument still populates the table.

        Task 15.2 calls ``seed(engine)`` from application startup without
        passing a clock, so the default branch must work end-to-end.
        """
        create_schema(engine)

        seed(engine)

        rows = _all_rows(engine)
        assert len(rows) == 5
        # The recorded_at column must be a non-empty ISO-8601 string with
        # at least date precision; we cannot pin the exact value because it
        # comes from the system clock.
        for row in rows:
            assert isinstance(row["recorded_at"], str)
            assert len(row["recorded_at"]) >= len("2026-01-01")


class TestSeedRowsModuleConstant:
    """:data:`INTERIM_ADR_SEED_ROWS` mirrors the table contents.

    Property 15 (task 13.3) is expected to derive its expected row set
    from this module-level constant, so the constant must match what
    :func:`seed` writes.
    """

    def test_module_constant_lists_five_rows(self) -> None:
        assert len(INTERIM_ADR_SEED_ROWS) == 5

    def test_module_constant_covers_all_five_backlog_adr_ids(self) -> None:
        ids = {row.backlog_adr_id for row in INTERIM_ADR_SEED_ROWS}
        assert ids == _EXPECTED_BACKLOG_IDS

    def test_module_constant_record_ids_are_unique(self) -> None:
        record_ids = [row.record_id for row in INTERIM_ADR_SEED_ROWS]
        assert len(set(record_ids)) == len(record_ids)


class TestSeedCoexistsWithEvidenceLazySeed:
    """The AD-WS-6 lazy seed in :mod:`walking_slice.evidence` shares the
    ``record_id = 'ad-ws-6'`` primary key with the row written here. The
    two code paths must not produce duplicates regardless of ordering."""

    def test_seed_then_lazy_seed_leaves_single_ad_ws_6_row(
        self, engine: Engine
    ) -> None:
        """seed() first, then a separate INSERT OR IGNORE for ad-ws-6."""
        create_schema(engine)
        seed(engine, clock=FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))

        # Simulate the lazy seed: an INSERT OR IGNORE against the same
        # primary key with different contents. The row inserted by seed()
        # must win because the lazy path used INSERT OR IGNORE.
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT OR IGNORE INTO Interim_ADR_Records (
                        record_id, motivating_requirement, motivating_criterion,
                        observable_behavior, recorded_at, backlog_adr_id
                    ) VALUES (
                        'ad-ws-6', 'different', 'different', 'different',
                        '2099-01-01T00:00:00.000+00:00', 'ADR-HT-003'
                    )
                    """
                )
            )

        rows = _rows_for(engine, backlog_adr_id="ADR-HT-003")
        assert len(rows) == 1
        # The seed() row's contents survive; the lazy path no-ops on
        # primary-key collision.
        assert rows[0]["motivating_criterion"] == "byte-offset anchoring"
