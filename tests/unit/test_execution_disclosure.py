"""Unit tests for :mod:`walking_slice.execution._disclosure` and the Slice 3
extension of :func:`walking_slice.disclosure.policy_for` (third-walking-slice
task 1.5).

Covers the contract established by task 1.4 and AD-WS-25 / AD-WS-23:

- :func:`seed_execution_coverage` inserts exactly one
  ``Disclosure_Policy_Coverage`` row per Slice 3 node kind, every row
  keyed on ``policy_id = 'slice-default-2026'`` and
  ``backlog_adr_id = 'ADR-HT-014'``.
- Repeat invocations are idempotent (``INSERT OR IGNORE`` against the
  composite primary key ``(policy_id, node_kind)``).
- The existing ``Disclosure_Policies`` row for ``slice-default-2026``
  is byte-equivalent before and after the Slice 3 seed (Requirement
  38.5, 40.1, 40.2 — Slice 1 and Slice 2 non-modification).
- The existing Slice 2 ``Disclosure_Policy_Coverage`` rows are
  byte-equivalent before and after the Slice 3 seed (Requirement 40.2
  — additive-only extension preserves prior coverage rows unchanged).
- :func:`walking_slice.disclosure.policy_for` resolves the active policy
  for every Slice 3 node kind via the new coverage rows; Slice 1 and
  Slice 2 node kinds continue to resolve to the same policy after
  Slice 3 seeding.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    SLICE_DEFAULT_POLICY_NAME,
    SLICE_DEFAULT_RULESET,
    DisclosurePolicyNotFoundError,
    policy_for,
    seed as seed_disclosure,
)
from walking_slice.execution._disclosure import (
    EXECUTION_COVERAGE_BACKLOG_ADR_ID,
    EXECUTION_NODE_KINDS,
    seed_execution_coverage,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import (
    PLANNING_NODE_KINDS,
    seed_planning_coverage,
)
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def execution_disclosure_engine(engine: Engine) -> Engine:
    """A per-test engine carrying Slice 1 + Slice 2 schema with both
    the baseline ``slice-default-2026`` policy and the Slice 2 coverage
    rows seeded.

    The fixture sets up the prerequisites every task-1.5 disclosure test
    needs:

    1. ``create_schema(engine)`` materializes Slice 1 tables.
    2. ``create_planning_schema(engine)`` materializes Slice 2 tables,
       including ``Disclosure_Policy_Coverage`` (the additive sibling
       table the Slice 3 seeder inserts into).
    3. ``seed_disclosure(engine)`` inserts the
       ``slice-default-2026`` row so the FOREIGN KEY from
       ``Disclosure_Policy_Coverage.policy_id`` resolves.
    4. ``seed_planning_coverage(connection)`` inserts the thirteen
       Slice 2 coverage rows so the Slice 3 seeder can be observed
       running additively on top of them (Requirement 40.2 — the
       existing Slice 2 rows must remain byte-equivalent after Slice 3
       seeding).

    The fixture deliberately does NOT call ``seed_execution_coverage``
    — every test exercises the Slice 3 seeder explicitly so the
    behavior under test is observable as the difference between
    pre- and post-seed snapshots.
    """
    create_schema(engine)
    create_planning_schema(engine)
    seed_disclosure(engine)
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)
    return engine


# ---------------------------------------------------------------------------
# EXECUTION_NODE_KINDS — the published tuple
# ---------------------------------------------------------------------------


def test_execution_node_kinds_matches_task_1_4() -> None:
    """The published tuple lists exactly the eight Slice 3 node kinds.

    Task 1.4 (and Requirement 38.1) enumerate: work_assignment_record,
    work_event_record, time_entry_record, deliverable_resource,
    deliverable_revision, deliverable_production_record,
    milestone_acceptance_record, completion_record.
    """
    expected = {
        "work_assignment_record",
        "work_event_record",
        "time_entry_record",
        "deliverable_resource",
        "deliverable_revision",
        "deliverable_production_record",
        "milestone_acceptance_record",
        "completion_record",
    }
    assert set(EXECUTION_NODE_KINDS) == expected
    assert len(EXECUTION_NODE_KINDS) == 8  # no duplicates


def test_execution_node_kinds_disjoint_from_planning_node_kinds() -> None:
    """No Slice 3 node kind overlaps with any Slice 2 node kind.

    Requirement 38.1 names a separate set of node kinds for the Slice 3
    additive extension; Requirement 40.2 keeps every Slice 2 coverage
    row unchanged. Disjointness lets the two seeders run independently
    on the same table without overwriting each other's rows.
    """
    assert set(EXECUTION_NODE_KINDS).isdisjoint(set(PLANNING_NODE_KINDS))


# ---------------------------------------------------------------------------
# seed_execution_coverage — happy path
# ---------------------------------------------------------------------------


def test_seed_execution_coverage_inserts_one_row_per_node_kind(
    execution_disclosure_engine: Engine,
) -> None:
    """A fresh seeding inserts exactly one coverage row per Slice 3 node kind."""
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    with execution_disclosure_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT policy_id, node_kind, recorded_at, backlog_adr_id "
                "FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({placeholders}) "
                "ORDER BY node_kind".format(
                    placeholders=", ".join(
                        f":k{i}" for i in range(len(EXECUTION_NODE_KINDS))
                    )
                )
            ),
            {f"k{i}": kind for i, kind in enumerate(EXECUTION_NODE_KINDS)},
        ).all()

    assert len(rows) == len(EXECUTION_NODE_KINDS)
    # Every row carries the slice-default policy and the Gap G-12
    # backlog ADR identifier.
    for row in rows:
        assert row.policy_id == SLICE_DEFAULT_POLICY_ID
        assert row.backlog_adr_id == EXECUTION_COVERAGE_BACKLOG_ADR_ID
        assert row.recorded_at == "2026-01-01T00:00:00.000+00:00"

    persisted_kinds = {row.node_kind for row in rows}
    assert persisted_kinds == set(EXECUTION_NODE_KINDS)


def test_seed_execution_coverage_is_idempotent(
    execution_disclosure_engine: Engine,
) -> None:
    """Repeated invocations do not raise and do not duplicate rows."""
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)
    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)
    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    with execution_disclosure_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({placeholders})".format(
                    placeholders=", ".join(
                        f":k{i}" for i in range(len(EXECUTION_NODE_KINDS))
                    )
                )
            ),
            {f"k{i}": kind for i, kind in enumerate(EXECUTION_NODE_KINDS)},
        ).scalar_one()

    assert count == len(EXECUTION_NODE_KINDS)


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 non-modification (Requirements 38.5, 40.1, 40.2)
# ---------------------------------------------------------------------------


def test_seed_execution_coverage_does_not_modify_slice_default_policy_row(
    execution_disclosure_engine: Engine,
) -> None:
    """Requirement 38.5 / 40.2 — the existing ``slice-default-2026`` row is unchanged.

    Captures the row before the Slice 3 seed and asserts byte-equivalent
    contents after seeding completes. The additive surface is the new
    Slice 3 coverage rows; the original ``Disclosure_Policies`` row
    identity, name, ruleset, effective start, and supersession status
    must be invariant.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with execution_disclosure_engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT policy_id, policy_name, ruleset_json, "
                "effective_start, superseded_by "
                "FROM Disclosure_Policies WHERE policy_id = :pid"
            ),
            {"pid": SLICE_DEFAULT_POLICY_ID},
        ).one()

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    with execution_disclosure_engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT policy_id, policy_name, ruleset_json, "
                "effective_start, superseded_by "
                "FROM Disclosure_Policies WHERE policy_id = :pid"
            ),
            {"pid": SLICE_DEFAULT_POLICY_ID},
        ).one()

    assert tuple(before) == tuple(after)


def test_seed_execution_coverage_does_not_modify_slice_2_coverage_rows(
    execution_disclosure_engine: Engine,
) -> None:
    """Requirement 40.2 — existing Slice 2 coverage rows remain byte-equivalent
    after Slice 3 seeding.

    The Slice 2 ``seed_planning_coverage`` invocation in the fixture
    seeded thirteen rows with their own ``recorded_at`` and
    ``backlog_adr_id``. The Slice 3 seeder must not overwrite or
    otherwise touch them; this test snapshots all columns of each Slice
    2 row before the Slice 3 seed and asserts byte-equivalence after.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with execution_disclosure_engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT policy_id, node_kind, recorded_at, backlog_adr_id "
                "FROM Disclosure_Policy_Coverage "
                "ORDER BY node_kind"
            )
        ).all()

    # Capture as tuples so the comparison is value-based and order-stable.
    before_tuples = sorted(tuple(row) for row in before)

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    with execution_disclosure_engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT policy_id, node_kind, recorded_at, backlog_adr_id "
                "FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({placeholders}) "
                "ORDER BY node_kind".format(
                    placeholders=", ".join(
                        f":k{i}" for i in range(len(PLANNING_NODE_KINDS))
                    )
                )
            ),
            {f"k{i}": kind for i, kind in enumerate(PLANNING_NODE_KINDS)},
        ).all()

    after_tuples = sorted(tuple(row) for row in after)
    assert after_tuples == before_tuples


def test_seed_execution_coverage_preserves_first_recorded_at(
    execution_disclosure_engine: Engine,
) -> None:
    """``INSERT OR IGNORE`` preserves the first-seeded ``recorded_at``.

    Requirement 38.5 — the additive extension records a recorded date
    for each newly covered node kind. The first successful seed wins;
    later invocations (with a later clock) must not overwrite the
    original date.
    """
    earlier_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    later_clock = FixedClock(datetime(2027, 6, 15, tzinfo=timezone.utc))

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=earlier_clock)
    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=later_clock)

    with execution_disclosure_engine.connect() as conn:
        recorded_at_values = {
            row.recorded_at
            for row in conn.execute(
                text(
                    "SELECT recorded_at FROM Disclosure_Policy_Coverage "
                    "WHERE node_kind IN ({placeholders})".format(
                        placeholders=", ".join(
                            f":k{i}" for i in range(len(EXECUTION_NODE_KINDS))
                        )
                    )
                ),
                {f"k{i}": kind for i, kind in enumerate(EXECUTION_NODE_KINDS)},
            ).all()
        }

    assert recorded_at_values == {"2026-01-01T00:00:00.000+00:00"}


# ---------------------------------------------------------------------------
# policy_for(engine, node_kind) — visibility of Slice 3 coverage rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_kind", list(EXECUTION_NODE_KINDS))
def test_policy_for_resolves_every_slice_3_node_kind_after_seeding(
    execution_disclosure_engine: Engine, node_kind: str
) -> None:
    """Every Slice 3 node kind resolves to ``slice-default-2026`` via coverage.

    Requirement 38.1 — the Slice 3 node kinds receive an additive
    extension of the existing ``slice-default-2026`` policy rather than
    a separate policy; :func:`walking_slice.disclosure.policy_for`
    consults ``Disclosure_Policy_Coverage`` first and returns the
    inherited rule set unchanged.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    policy = policy_for(execution_disclosure_engine, node_kind)

    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID
    assert policy.policy_name == SLICE_DEFAULT_POLICY_NAME
    # The ruleset round-trips from JSON storage via the existing
    # ``Disclosure_Policies`` row — it is byte-equivalent to the
    # in-memory SLICE_DEFAULT_RULESET constant.
    assert policy.ruleset == dict(SLICE_DEFAULT_RULESET)


@pytest.mark.parametrize("node_kind", list(EXECUTION_NODE_KINDS))
def test_policy_for_does_not_resolve_slice_3_node_kind_without_seeding(
    execution_disclosure_engine: Engine, node_kind: str
) -> None:
    """Before the Slice 3 seed runs, Slice 3 node kinds fall back to the baseline policy.

    The Slice 1 + Slice 2 ``policy_for`` lookup degrades gracefully when
    no coverage row exists for a given ``node_kind``: it returns the
    baseline ``slice-default-2026`` policy via the fallback path. This
    test pins the pre-seed behavior so the post-seed visibility test
    above measures a real state change.
    """
    # Fixture has seeded only Slice 1 + Slice 2 coverage; the Slice 3
    # seeder has NOT been called. The baseline-default fallback returns
    # the same policy identifier without going through a coverage row.
    policy = policy_for(execution_disclosure_engine, node_kind)
    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID

    # And — no coverage row yet exists for this Slice 3 node kind.
    with execution_disclosure_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT 1 FROM Disclosure_Policy_Coverage "
                "WHERE node_kind = :nk"
            ),
            {"nk": node_kind},
        ).all()
    assert rows == []


@pytest.mark.parametrize("node_kind", list(PLANNING_NODE_KINDS))
def test_policy_for_resolves_slice_2_node_kinds_byte_equivalent_after_slice_3_seed(
    execution_disclosure_engine: Engine, node_kind: str
) -> None:
    """Requirement 40.2 — Slice 2 node-kind resolution is unchanged by Slice 3 seeding.

    The before/after policy objects are equal field-for-field: same
    ``policy_id``, same ``policy_name``, same ``ruleset``, same
    ``effective_start``, same ``superseded_by``.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    before = policy_for(execution_disclosure_engine, node_kind)

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    after = policy_for(execution_disclosure_engine, node_kind)
    assert before == after


@pytest.mark.parametrize(
    "slice_1_kind",
    ["document_revision", "finding_revision", "decision", "trail_revision"],
)
def test_policy_for_resolves_slice_1_node_kinds_byte_equivalent_after_slice_3_seed(
    execution_disclosure_engine: Engine, slice_1_kind: str
) -> None:
    """Requirement 40.1 — Slice 1 node-kind resolution is unchanged by Slice 3 seeding.

    Slice 1 node kinds do not have coverage rows in either Slice 2 or
    Slice 3 (the baseline policy applies to them via fallback). The
    fallback continues to return the unchanged ``slice-default-2026``
    policy after the Slice 3 additive seeder runs.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    before = policy_for(execution_disclosure_engine, slice_1_kind)

    with execution_disclosure_engine.begin() as conn:
        seed_execution_coverage(conn, clock=fixed_clock)

    after = policy_for(execution_disclosure_engine, slice_1_kind)
    assert before == after


def test_policy_for_raises_when_slice_misbootstrapped(engine: Engine) -> None:
    """``DisclosurePolicyNotFoundError`` is raised when no policy is seeded at all.

    Mirrors the Slice 2 contract: ``policy_for`` does not silently
    return a default when neither a coverage row nor the baseline
    policy exists. Operators investigating a mis-bootstrapped slice see
    the requested ``node_kind`` and the baseline ``policy_id`` in the
    error message.
    """
    create_schema(engine)
    create_planning_schema(engine)
    # Note: neither seed_disclosure nor any coverage seed runs here.

    with pytest.raises(DisclosurePolicyNotFoundError) as exc_info:
        policy_for(engine, "work_assignment_record")

    assert "work_assignment_record" in str(exc_info.value)
    assert SLICE_DEFAULT_POLICY_ID in str(exc_info.value)
