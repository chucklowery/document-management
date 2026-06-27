"""Unit tests for :mod:`walking_slice.outcome._disclosure` and the Slice 4
extension of :func:`walking_slice.disclosure.policy_for` (fourth-walking-slice
task 1.3).

Covers the contract established by task 1.3 and AD-WS-34 / AD-WS-23:

- :func:`seed_outcome_coverage` inserts exactly one
  ``Disclosure_Policy_Coverage`` row per Slice 4 node kind, every row
  keyed on ``policy_id = 'slice-default-2026'`` and
  ``backlog_adr_id = 'ADR-HT-020'``.
- The ``measurement_record`` row — and only that row — carries the
  AD-WS-34 ``restricted_attributes_json`` payload naming the imported
  source-system attributes as restricted (Requirement 58.5); every
  other Slice 4 row stores ``NULL`` for that column.
- Repeat invocations are idempotent (``INSERT OR IGNORE`` against the
  composite primary key ``(policy_id, node_kind)``).
- The existing ``Disclosure_Policies`` row for ``slice-default-2026``
  and the existing Slice 2 + Slice 3 ``Disclosure_Policy_Coverage`` rows
  are byte-equivalent before and after the Slice 4 seed (Requirement
  60.2 — Slice 1, Slice 2, and Slice 3 non-modification).
- :func:`walking_slice.disclosure.policy_for` resolves the active policy
  for every Slice 4 node kind via the new coverage rows; prior node
  kinds continue to resolve to the same policy after Slice 4 seeding.
"""

from __future__ import annotations

import json
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
    EXECUTION_NODE_KINDS,
    seed_execution_coverage,
)
from walking_slice.outcome._disclosure import (
    MEASUREMENT_RECORD_NODE_KIND,
    MEASUREMENT_RECORD_REDACTION_MARKER,
    MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES,
    MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES_JSON,
    OUTCOME_COVERAGE_BACKLOG_ADR_ID,
    OUTCOME_NODE_KINDS,
    seed_outcome_coverage,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import (
    PLANNING_NODE_KINDS,
    seed_planning_coverage,
)
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.unit


_FIXED = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FIXED_RECORDED_AT = "2026-01-01T00:00:00.000+00:00"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_disclosure_engine(engine: Engine) -> Engine:
    """A per-test engine carrying Slice 1 + Slice 2 + Slice 3 disclosure state.

    Sets up the prerequisites every task-1.3 disclosure test needs:

    1. ``create_schema(engine)`` materializes Slice 1 tables.
    2. ``create_planning_schema(engine)`` materializes Slice 2 tables,
       including ``Disclosure_Policy_Coverage`` (the additive sibling
       table the Slice 4 seeder inserts into).
    3. ``seed_disclosure(engine)`` inserts the ``slice-default-2026``
       row so the FOREIGN KEY from ``Disclosure_Policy_Coverage.policy_id``
       resolves.
    4. ``seed_planning_coverage`` + ``seed_execution_coverage`` insert
       the Slice 2 + Slice 3 coverage rows so the Slice 4 seeder can be
       observed running additively on top of them (Requirement 60.2 —
       prior rows remain byte-equivalent after Slice 4 seeding).

    The fixture deliberately does NOT call ``seed_outcome_coverage`` —
    every test exercises the Slice 4 seeder explicitly so the behavior
    under test is observable as the difference between pre- and post-seed
    snapshots.
    """
    create_schema(engine)
    create_planning_schema(engine)
    seed_disclosure(engine)
    fixed_clock = FixedClock(_FIXED)
    with engine.begin() as conn:
        seed_planning_coverage(conn, clock=fixed_clock)
        seed_execution_coverage(conn, clock=fixed_clock)
    return engine


# ---------------------------------------------------------------------------
# OUTCOME_NODE_KINDS — the published tuple
# ---------------------------------------------------------------------------


def test_outcome_node_kinds_matches_task_1_3() -> None:
    """The published tuple lists exactly the seven Slice 4 node kinds."""
    expected = {
        "measurement_definition",
        "measurement_definition_revision",
        "measurement_record",
        "observed_outcome",
        "observed_outcome_revision",
        "success_condition_assessment_record",
        "outcome_review_record",
    }
    assert set(OUTCOME_NODE_KINDS) == expected
    assert len(OUTCOME_NODE_KINDS) == 7  # no duplicates


def test_outcome_node_kinds_disjoint_from_prior_slice_node_kinds() -> None:
    """No Slice 4 node kind overlaps with any Slice 2 or Slice 3 node kind.

    Disjointness lets the seeders run independently on the same table
    without overwriting each other's rows (Requirement 60.2).
    """
    assert set(OUTCOME_NODE_KINDS).isdisjoint(set(PLANNING_NODE_KINDS))
    assert set(OUTCOME_NODE_KINDS).isdisjoint(set(EXECUTION_NODE_KINDS))


def test_restricted_attributes_payload_names_ad_ws_34_attributes() -> None:
    """The measurement_record payload names exactly the AD-WS-34 attributes."""
    assert MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES == (
        "source_system_id",
        "source_system_record_id",
        "source_system_authority",
        "source_system_retrieval_at",
        "import_at",
    )
    assert MEASUREMENT_RECORD_REDACTION_MARKER == {
        "kind": "measurement_record",
        "redacted": True,
    }
    payload = json.loads(MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES_JSON)
    assert payload["restricted_attributes"] == list(
        MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES
    )
    assert payload["redaction_marker"] == MEASUREMENT_RECORD_REDACTION_MARKER


# ---------------------------------------------------------------------------
# seed_outcome_coverage — happy path
# ---------------------------------------------------------------------------


def test_seed_outcome_coverage_inserts_one_row_per_node_kind(
    outcome_disclosure_engine: Engine,
) -> None:
    """A fresh seeding inserts exactly one coverage row per Slice 4 node kind."""
    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    with outcome_disclosure_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT policy_id, node_kind, recorded_at, backlog_adr_id "
                "FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({placeholders}) "
                "ORDER BY node_kind".format(
                    placeholders=", ".join(
                        f":k{i}" for i in range(len(OUTCOME_NODE_KINDS))
                    )
                )
            ),
            {f"k{i}": kind for i, kind in enumerate(OUTCOME_NODE_KINDS)},
        ).all()

    assert len(rows) == len(OUTCOME_NODE_KINDS)
    for row in rows:
        assert row.policy_id == SLICE_DEFAULT_POLICY_ID
        assert row.backlog_adr_id == OUTCOME_COVERAGE_BACKLOG_ADR_ID
        assert row.recorded_at == _FIXED_RECORDED_AT

    assert {row.node_kind for row in rows} == set(OUTCOME_NODE_KINDS)


def test_seed_outcome_coverage_populates_measurement_record_payload_only(
    outcome_disclosure_engine: Engine,
) -> None:
    """Only the measurement_record row carries the restricted-attributes payload.

    AD-WS-34 / Requirement 58.5 — the imported source-system attributes
    are named restricted on the ``measurement_record`` coverage row; every
    other Slice 4 row stores ``NULL`` for ``restricted_attributes_json``.
    """
    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    with outcome_disclosure_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT node_kind, restricted_attributes_json "
                "FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({placeholders})".format(
                    placeholders=", ".join(
                        f":k{i}" for i in range(len(OUTCOME_NODE_KINDS))
                    )
                )
            ),
            {f"k{i}": kind for i, kind in enumerate(OUTCOME_NODE_KINDS)},
        ).all()

    payload_by_kind = {row.node_kind: row.restricted_attributes_json for row in rows}

    # The measurement_record row carries the canonical payload.
    assert (
        payload_by_kind[MEASUREMENT_RECORD_NODE_KIND]
        == MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES_JSON
    )
    parsed = json.loads(payload_by_kind[MEASUREMENT_RECORD_NODE_KIND])
    assert parsed["restricted_attributes"] == list(
        MEASUREMENT_RECORD_RESTRICTED_ATTRIBUTES
    )
    assert parsed["redaction_marker"] == MEASUREMENT_RECORD_REDACTION_MARKER

    # Every other Slice 4 row leaves the column NULL.
    for node_kind, payload in payload_by_kind.items():
        if node_kind == MEASUREMENT_RECORD_NODE_KIND:
            continue
        assert payload is None


def test_seed_outcome_coverage_is_idempotent(
    outcome_disclosure_engine: Engine,
) -> None:
    """Repeated invocations do not raise and do not duplicate rows."""
    for _ in range(3):
        with outcome_disclosure_engine.begin() as conn:
            seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    with outcome_disclosure_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({placeholders})".format(
                    placeholders=", ".join(
                        f":k{i}" for i in range(len(OUTCOME_NODE_KINDS))
                    )
                )
            ),
            {f"k{i}": kind for i, kind in enumerate(OUTCOME_NODE_KINDS)},
        ).scalar_one()

    assert count == len(OUTCOME_NODE_KINDS)


def test_seed_outcome_coverage_preserves_first_recorded_at(
    outcome_disclosure_engine: Engine,
) -> None:
    """``INSERT OR IGNORE`` preserves the first-seeded ``recorded_at``."""
    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))
    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(
            conn, clock=FixedClock(datetime(2027, 6, 15, tzinfo=timezone.utc))
        )

    with outcome_disclosure_engine.connect() as conn:
        recorded_at_values = {
            row.recorded_at
            for row in conn.execute(
                text(
                    "SELECT recorded_at FROM Disclosure_Policy_Coverage "
                    "WHERE node_kind IN ({placeholders})".format(
                        placeholders=", ".join(
                            f":k{i}" for i in range(len(OUTCOME_NODE_KINDS))
                        )
                    )
                ),
                {f"k{i}": kind for i, kind in enumerate(OUTCOME_NODE_KINDS)},
            ).all()
        }

    assert recorded_at_values == {_FIXED_RECORDED_AT}


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 + Slice 3 non-modification (Requirement 60.2)
# ---------------------------------------------------------------------------


def test_seed_outcome_coverage_does_not_modify_slice_default_policy_row(
    outcome_disclosure_engine: Engine,
) -> None:
    """Requirement 60.2 — the existing ``slice-default-2026`` row is unchanged."""
    select = text(
        "SELECT policy_id, policy_name, ruleset_json, "
        "effective_start, superseded_by "
        "FROM Disclosure_Policies WHERE policy_id = :pid"
    )

    with outcome_disclosure_engine.connect() as conn:
        before = conn.execute(select, {"pid": SLICE_DEFAULT_POLICY_ID}).one()

    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    with outcome_disclosure_engine.connect() as conn:
        after = conn.execute(select, {"pid": SLICE_DEFAULT_POLICY_ID}).one()

    assert tuple(before) == tuple(after)


def test_seed_outcome_coverage_does_not_modify_prior_coverage_rows(
    outcome_disclosure_engine: Engine,
) -> None:
    """Requirement 60.2 — existing Slice 2 + Slice 3 coverage rows are unchanged.

    Snapshots all four base columns of every prior-slice coverage row
    before the Slice 4 seed and asserts byte-equivalence after. The
    additive ``restricted_attributes_json`` column reads ``NULL`` for
    these rows because the seeder only populates it for
    ``measurement_record``.
    """
    prior_kinds = list(PLANNING_NODE_KINDS) + list(EXECUTION_NODE_KINDS)
    select = text(
        "SELECT policy_id, node_kind, recorded_at, backlog_adr_id "
        "FROM Disclosure_Policy_Coverage "
        "WHERE node_kind IN ({placeholders}) "
        "ORDER BY node_kind".format(
            placeholders=", ".join(f":k{i}" for i in range(len(prior_kinds)))
        )
    )
    params = {f"k{i}": kind for i, kind in enumerate(prior_kinds)}

    with outcome_disclosure_engine.connect() as conn:
        before = sorted(tuple(row) for row in conn.execute(select, params).all())

    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    with outcome_disclosure_engine.connect() as conn:
        after = sorted(tuple(row) for row in conn.execute(select, params).all())
        # The additive column reads NULL for every prior-slice row.
        prior_payloads = {
            row.restricted_attributes_json
            for row in conn.execute(
                text(
                    "SELECT restricted_attributes_json "
                    "FROM Disclosure_Policy_Coverage "
                    "WHERE node_kind IN ({placeholders})".format(
                        placeholders=", ".join(
                            f":k{i}" for i in range(len(prior_kinds))
                        )
                    )
                ),
                params,
            ).all()
        }

    assert after == before
    assert prior_payloads == {None}


# ---------------------------------------------------------------------------
# policy_for(engine, node_kind) — visibility of Slice 4 coverage rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_kind", list(OUTCOME_NODE_KINDS))
def test_policy_for_resolves_every_slice_4_node_kind_after_seeding(
    outcome_disclosure_engine: Engine, node_kind: str
) -> None:
    """Every Slice 4 node kind resolves to ``slice-default-2026`` via coverage.

    Requirement 58.1 — the Slice 4 node kinds receive an additive
    extension of the existing ``slice-default-2026`` policy rather than a
    separate policy; :func:`walking_slice.disclosure.policy_for` consults
    ``Disclosure_Policy_Coverage`` first and returns the inherited rule
    set unchanged.
    """
    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    policy = policy_for(outcome_disclosure_engine, node_kind)

    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID
    assert policy.policy_name == SLICE_DEFAULT_POLICY_NAME
    assert policy.ruleset == dict(SLICE_DEFAULT_RULESET)


@pytest.mark.parametrize(
    "prior_kind",
    list(PLANNING_NODE_KINDS) + list(EXECUTION_NODE_KINDS),
)
def test_policy_for_resolves_prior_node_kinds_byte_equivalent_after_slice_4_seed(
    outcome_disclosure_engine: Engine, prior_kind: str
) -> None:
    """Requirement 60.2 — prior node-kind resolution is unchanged by Slice 4 seeding."""
    before = policy_for(outcome_disclosure_engine, prior_kind)

    with outcome_disclosure_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_FIXED))

    after = policy_for(outcome_disclosure_engine, prior_kind)
    assert before == after


def test_policy_for_raises_when_slice_misbootstrapped(engine: Engine) -> None:
    """``DisclosurePolicyNotFoundError`` is raised when no policy is seeded."""
    create_schema(engine)
    create_planning_schema(engine)

    with pytest.raises(DisclosurePolicyNotFoundError) as exc_info:
        policy_for(engine, "measurement_record")

    assert "measurement_record" in str(exc_info.value)
    assert SLICE_DEFAULT_POLICY_ID in str(exc_info.value)
