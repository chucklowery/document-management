"""Unit tests for :mod:`walking_slice.planning._disclosure` and
:func:`walking_slice.disclosure.policy_for` (task 1.4).

Covers the contract established by task 1.4 and AD-WS-16 / AD-WS-23:

- :func:`seed_planning_coverage` inserts exactly one
  ``Disclosure_Policy_Coverage`` row per Slice 2 node kind, every row
  keyed on ``policy_id = 'slice-default-2026'`` and
  ``backlog_adr_id = 'ADR-HT-009'``.
- Repeat invocations are idempotent (``INSERT OR IGNORE`` against the
  composite primary key ``(policy_id, node_kind)``).
- The existing ``Disclosure_Policies`` row for ``slice-default-2026``
  is byte-equivalent before and after the seed (Requirement 17.5,
  Requirement 19.2 — Slice 1 non-modification).
- :func:`walking_slice.disclosure.policy_for` resolves the active policy
  for every Slice 2 node kind via the coverage row and for Slice 1 node
  kinds via the baseline default. The function raises
  :class:`DisclosurePolicyNotFoundError` when the slice is
  mis-bootstrapped.
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
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import (
    PLANNING_COVERAGE_BACKLOG_ADR_ID,
    PLANNING_NODE_KINDS,
    seed_planning_coverage,
)
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """A per-test engine with Slice 1 + Slice 2 schema and the default
    Disclosure policy seeded.

    The fixture sets up the prerequisites every task-1.4 test needs:
    1. ``create_schema(engine)`` materializes Slice 1 tables.
    2. ``create_planning_schema(engine)`` materializes Slice 2 tables,
       including ``Disclosure_Policy_Coverage``.
    3. ``seed_disclosure(engine)`` inserts the
       ``slice-default-2026`` row so the FOREIGN KEY from
       ``Disclosure_Policy_Coverage.policy_id`` resolves.
    """
    create_schema(engine)
    create_planning_schema(engine)
    seed_disclosure(engine)
    return engine


# ---------------------------------------------------------------------------
# PLANNING_NODE_KINDS
# ---------------------------------------------------------------------------


def test_planning_node_kinds_matches_task_1_4() -> None:
    """The published tuple lists exactly the 13 Slice 2 node kinds.

    Task 1.4 enumerates: objective, objective_revision, intended_outcome,
    intended_outcome_revision, project, project_revision,
    deliverable_expectation, deliverable_expectation_revision,
    activity_plan, plan_revision, plan_review, plan_review_revision,
    plan_approval.
    """
    expected = {
        "objective",
        "objective_revision",
        "intended_outcome",
        "intended_outcome_revision",
        "project",
        "project_revision",
        "deliverable_expectation",
        "deliverable_expectation_revision",
        "activity_plan",
        "plan_revision",
        "plan_review",
        "plan_review_revision",
        "plan_approval",
    }
    assert set(PLANNING_NODE_KINDS) == expected
    assert len(PLANNING_NODE_KINDS) == 13  # no duplicates


# ---------------------------------------------------------------------------
# seed_planning_coverage
# ---------------------------------------------------------------------------


def test_seed_planning_coverage_inserts_one_row_per_node_kind(
    planning_engine: Engine,
) -> None:
    """A fresh database receives exactly one coverage row per Slice 2 node kind."""
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    with planning_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT policy_id, node_kind, recorded_at, backlog_adr_id "
                "FROM Disclosure_Policy_Coverage "
                "ORDER BY node_kind"
            )
        ).all()

    assert len(rows) == len(PLANNING_NODE_KINDS)
    # Every row carries the slice-default policy and the Gap G-7 backlog ADR.
    for row in rows:
        assert row.policy_id == SLICE_DEFAULT_POLICY_ID
        assert row.backlog_adr_id == PLANNING_COVERAGE_BACKLOG_ADR_ID
        assert row.recorded_at == "2026-01-01T00:00:00.000+00:00"

    persisted_kinds = {row.node_kind for row in rows}
    assert persisted_kinds == set(PLANNING_NODE_KINDS)


def test_seed_planning_coverage_is_idempotent(
    planning_engine: Engine,
) -> None:
    """Repeated invocations do not raise and do not duplicate rows."""
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    with planning_engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Disclosure_Policy_Coverage")
        ).scalar_one()

    assert count == len(PLANNING_NODE_KINDS)


def test_seed_planning_coverage_preserves_first_recorded_at(
    planning_engine: Engine,
) -> None:
    """``INSERT OR IGNORE`` preserves the first-seeded ``recorded_at``.

    Requirement 17.5 specifies the extension records a recorded date for
    each newly covered node kind. The first successful seed wins; later
    invocations (with a later clock) must not overwrite the original
    date.
    """
    earlier_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    later_clock = FixedClock(datetime(2027, 6, 15, tzinfo=timezone.utc))

    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=earlier_clock)
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=later_clock)

    with planning_engine.connect() as conn:
        recorded_at_values = {
            row.recorded_at
            for row in conn.execute(
                text("SELECT recorded_at FROM Disclosure_Policy_Coverage")
            ).all()
        }

    assert recorded_at_values == {"2026-01-01T00:00:00.000+00:00"}


def test_seed_planning_coverage_does_not_modify_slice_1_policy_row(
    planning_engine: Engine,
) -> None:
    """Requirement 17.5 / 19.2 — the existing ``slice-default-2026`` row is unchanged.

    Captures the row before seeding and asserts byte-equivalent contents
    after seeding completes. The additive surface is the new
    ``Disclosure_Policy_Coverage`` table; the original
    ``Disclosure_Policies`` row identity, name, ruleset, effective start,
    and supersession status must be invariant.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with planning_engine.connect() as conn:
        before = conn.execute(
            text(
                "SELECT policy_id, policy_name, ruleset_json, "
                "effective_start, superseded_by "
                "FROM Disclosure_Policies WHERE policy_id = :pid"
            ),
            {"pid": SLICE_DEFAULT_POLICY_ID},
        ).one()

    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    with planning_engine.connect() as conn:
        after = conn.execute(
            text(
                "SELECT policy_id, policy_name, ruleset_json, "
                "effective_start, superseded_by "
                "FROM Disclosure_Policies WHERE policy_id = :pid"
            ),
            {"pid": SLICE_DEFAULT_POLICY_ID},
        ).one()

    assert tuple(before) == tuple(after)


def test_seed_planning_coverage_uses_system_clock_when_clock_omitted(
    planning_engine: Engine,
) -> None:
    """Omitting the ``clock`` keyword falls back to :class:`SystemClock`."""
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn)

    with planning_engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Disclosure_Policy_Coverage")
        ).scalar_one()
        sample = conn.execute(
            text(
                "SELECT recorded_at FROM Disclosure_Policy_Coverage "
                "WHERE node_kind = :kind"
            ),
            {"kind": "objective"},
        ).scalar_one()

    assert count == len(PLANNING_NODE_KINDS)
    # The recorded_at is ISO-8601 with millisecond precision; the exact
    # value is non-deterministic when the system clock backs the seed,
    # so just assert it parses as a UTC datetime.
    parsed = datetime.fromisoformat(sample)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_seed_planning_coverage_rejects_update_via_trigger(
    planning_engine: Engine,
) -> None:
    """The ``Disclosure_Policy_Coverage`` table is insert-only after seeding.

    Verifies the AD-WS-19 immutability triggers from task 1.3 are wired
    against this table: an UPDATE attempt raises an integrity error.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))

    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    # The trigger raises ``RAISE(ABORT, ...)`` which SQLAlchemy surfaces
    # as an OperationalError.
    from sqlalchemy.exc import DatabaseError

    with pytest.raises(DatabaseError):
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE Disclosure_Policy_Coverage "
                    "SET backlog_adr_id = 'ADR-XXX-999' "
                    "WHERE node_kind = 'objective'"
                )
            )


# ---------------------------------------------------------------------------
# policy_for(engine, node_kind)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_kind", list(PLANNING_NODE_KINDS))
def test_policy_for_resolves_every_slice_2_node_kind_via_coverage(
    planning_engine: Engine, node_kind: str
) -> None:
    """Every Slice 2 node kind resolves to ``slice-default-2026`` via coverage."""
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    policy = policy_for(planning_engine, node_kind)

    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID
    assert policy.policy_name == SLICE_DEFAULT_POLICY_NAME
    # The ruleset round-trips from JSON storage via the existing
    # ``Disclosure_Policies`` row — it is byte-equivalent to the in-memory
    # SLICE_DEFAULT_RULESET constant.
    assert policy.ruleset == dict(SLICE_DEFAULT_RULESET)


@pytest.mark.parametrize(
    "slice_1_kind",
    ["document_revision", "finding_revision", "decision", "trail_revision"],
)
def test_policy_for_falls_back_to_default_for_slice_1_kinds(
    planning_engine: Engine, slice_1_kind: str
) -> None:
    """Slice 1 node kinds (with no coverage row) resolve to the baseline default."""
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    policy = policy_for(planning_engine, slice_1_kind)

    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID
    assert policy.ruleset == dict(SLICE_DEFAULT_RULESET)


def test_policy_for_falls_back_to_default_when_coverage_table_absent(
    engine: Engine,
) -> None:
    """A Slice 1-only database resolves every node kind to the baseline default.

    Ensures the function is backward-compatible with a database that
    predates Slice 2 (no ``Disclosure_Policy_Coverage`` table). The
    LEFT JOIN protects against this: the query against the missing
    table is caught and the baseline lookup runs instead.
    """
    create_schema(engine)
    seed_disclosure(engine)

    policy = policy_for(engine, "objective")
    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID


def test_policy_for_raises_when_slice_misbootstrapped(engine: Engine) -> None:
    """When neither lookup yields a row, the function raises a typed error.

    The error message names the requested ``node_kind`` so callers (and
    operators inspecting logs) can correlate the failure with the
    incoming request.
    """
    # Schema is in place but no policy is seeded; the function must not
    # silently return a default.
    create_schema(engine)
    create_planning_schema(engine)

    with pytest.raises(DisclosurePolicyNotFoundError) as exc_info:
        policy_for(engine, "objective")

    assert "objective" in str(exc_info.value)
    assert SLICE_DEFAULT_POLICY_ID in str(exc_info.value)


def test_policy_for_is_byte_equivalent_across_repeated_invocations(
    planning_engine: Engine,
) -> None:
    """Property: idempotent retrieval (Slice 1 Requirement 14.5 / Slice 2 14.5).

    Repeated calls for the same ``node_kind`` return byte-equivalent
    policy contents; the lookup is a pure read with no side effects.
    """
    fixed_clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    with planning_engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)

    first = policy_for(planning_engine, "plan_approval")
    second = policy_for(planning_engine, "plan_approval")
    third = policy_for(planning_engine, "plan_approval")

    assert first == second == third
