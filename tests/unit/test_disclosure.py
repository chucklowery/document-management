"""Unit tests for :mod:`walking_slice.disclosure`.

Covers the contract established by task 13.2 and AD-WS-9:

- :func:`seed` inserts exactly one row keyed on ``slice-default-2026`` and
  is idempotent across repeated invocations.
- The seeded row carries the AD-WS-9 ruleset: restricted nodes are replaced
  with ``{kind, redacted: true}`` markers, gap descriptors enumerate the
  ``unavailable``/``stale``/``unresolved`` categories, and restricted-vs-
  nonexistent observability is normalized across count, identifier set,
  ordering, cursor, response size, error wording, and latency (100 ms
  tolerance).
- :func:`get_policy` returns a :class:`DisclosurePolicy` that round-trips
  the persisted columns, including a decoded ``ruleset`` mapping.
- :func:`get_policy` raises :class:`DisclosurePolicyNotFoundError` when
  the row does not exist.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.disclosure import (
    SLICE_DEFAULT_EFFECTIVE_START,
    SLICE_DEFAULT_POLICY_ID,
    SLICE_DEFAULT_POLICY_NAME,
    SLICE_DEFAULT_RULESET,
    DisclosurePolicy,
    DisclosurePolicyNotFoundError,
    get_policy,
    seed,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# seed()
# ---------------------------------------------------------------------------


def test_seed_inserts_slice_default_row(engine: Engine) -> None:
    """A fresh database receives the slice-default-2026 policy row."""
    create_schema(engine)

    seed(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT policy_id, policy_name, effective_start, superseded_by "
                "FROM Disclosure_Policies"
            )
        ).one()

    assert row.policy_id == SLICE_DEFAULT_POLICY_ID
    assert row.policy_name == SLICE_DEFAULT_POLICY_NAME
    assert row.effective_start == SLICE_DEFAULT_EFFECTIVE_START
    assert row.superseded_by is None


def test_seed_is_idempotent(engine: Engine) -> None:
    """Repeated invocations do not raise and do not duplicate the row."""
    create_schema(engine)

    seed(engine)
    seed(engine)
    seed(engine)

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Disclosure_Policies")
        ).scalar_one()

    assert count == 1


def test_seed_preserves_operator_edits(engine: Engine) -> None:
    """``INSERT OR IGNORE`` must not overwrite an already-present row.

    A future ADR may insert a different policy with the same
    ``policy_id``; ``seed`` is only responsible for *bootstrapping* the
    default and must not clobber a row an operator has tuned.
    """
    create_schema(engine)
    seed(engine)

    # Simulate an operator-edited supersession by updating the
    # mutable-by-design ``superseded_by`` field. The schema allows this
    # because ``Disclosure_Policies`` is intentionally not in the AD-WS-4
    # immutable-tables set.
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Disclosure_Policies SET superseded_by = :sup "
                "WHERE policy_id = :pid"
            ),
            {"sup": "slice-default-2027", "pid": SLICE_DEFAULT_POLICY_ID},
        )

    seed(engine)

    policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
    assert policy.superseded_by == "slice-default-2027"


# ---------------------------------------------------------------------------
# Ruleset shape (AD-WS-9)
# ---------------------------------------------------------------------------


def test_seeded_ruleset_encodes_ad_ws_9_rules(engine: Engine) -> None:
    """The persisted ``ruleset_json`` matches the AD-WS-9 contract verbatim."""
    create_schema(engine)
    seed(engine)

    policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
    ruleset = policy.ruleset

    # Rule 1 — restricted node treatment.
    restricted = ruleset["restricted_node_treatment"]
    assert restricted["action"] == "replace_with_marker"
    assert restricted["marker_shape"] == {"kind": "<node_kind>", "redacted": True}
    for excluded in ("identifier", "attributes", "count"):
        assert excluded in restricted["marker_excludes"]

    # Rule 2 — gap descriptor categories.
    gap = ruleset["gap_descriptor"]
    assert sorted(gap["categories"]) == ["stale", "unavailable", "unresolved"]
    # ``restricted`` MUST NOT be among the gap categories; restricted nodes
    # are replaced with a marker (rule 1), not surfaced as a gap.
    assert "restricted" not in gap["categories"]
    # ``intentional`` is recorded in the Provenance Manifest, not navigation.
    assert "intentional" not in gap["categories"]

    # Rule 3 — restricted-vs-nonexistent normalization.
    norm = ruleset["restricted_vs_nonexistent_normalization"]
    expected_dimensions = {
        "count",
        "identifier_set",
        "ordering",
        "cursor",
        "response_size",
        "error_wording",
    }
    assert set(norm["indistinguishable_dimensions"]) == expected_dimensions
    assert norm["latency_tolerance_ms"] == 100


def test_module_constant_matches_seeded_ruleset(engine: Engine) -> None:
    """``SLICE_DEFAULT_RULESET`` and the persisted JSON are equal.

    Guards against drift between the in-process constant (used by tests
    that read the active policy without a database) and the JSON written
    to ``Disclosure_Policies`` on startup.
    """
    create_schema(engine)
    seed(engine)

    with engine.connect() as conn:
        ruleset_json = conn.execute(
            text(
                "SELECT ruleset_json FROM Disclosure_Policies "
                "WHERE policy_id = :pid"
            ),
            {"pid": SLICE_DEFAULT_POLICY_ID},
        ).scalar_one()

    assert json.loads(ruleset_json) == dict(SLICE_DEFAULT_RULESET)


# ---------------------------------------------------------------------------
# get_policy()
# ---------------------------------------------------------------------------


def test_get_policy_returns_typed_value_object(engine: Engine) -> None:
    """:func:`get_policy` returns a ``DisclosurePolicy`` with decoded ruleset."""
    create_schema(engine)
    seed(engine)

    policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)

    assert isinstance(policy, DisclosurePolicy)
    assert policy.policy_id == SLICE_DEFAULT_POLICY_ID
    assert policy.policy_name == SLICE_DEFAULT_POLICY_NAME
    assert policy.effective_start == SLICE_DEFAULT_EFFECTIVE_START
    assert policy.superseded_by is None
    # ``ruleset`` is a decoded mapping, not a raw JSON string.
    assert isinstance(policy.ruleset, dict)
    assert policy.ruleset["policy_name"] == SLICE_DEFAULT_POLICY_NAME


def test_get_policy_raises_when_row_missing(engine: Engine) -> None:
    """Unknown ``policy_id`` produces :class:`DisclosurePolicyNotFoundError`."""
    create_schema(engine)
    # Note: no seed() — the table is empty.

    with pytest.raises(DisclosurePolicyNotFoundError) as exc_info:
        get_policy(engine, SLICE_DEFAULT_POLICY_ID)

    assert SLICE_DEFAULT_POLICY_ID in str(exc_info.value)


def test_get_policy_does_not_see_other_policies(engine: Engine) -> None:
    """Lookup is keyed strictly on ``policy_id``; other rows are not returned."""
    create_schema(engine)
    seed(engine)

    # Insert a second, distinct policy row directly. Use a different
    # ``policy_name`` (UNIQUE) and ``policy_id`` so the insert succeeds.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Disclosure_Policies (
                    policy_id, policy_name, ruleset_json, effective_start
                ) VALUES (
                    'experimental-2027', 'experimental-2027', '{}',
                    '2027-01-01T00:00:00.000Z'
                )
                """
            )
        )

    with pytest.raises(DisclosurePolicyNotFoundError):
        get_policy(engine, "no-such-policy")

    default = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
    other = get_policy(engine, "experimental-2027")
    assert default.policy_id == SLICE_DEFAULT_POLICY_ID
    assert other.policy_id == "experimental-2027"
    assert default.ruleset != other.ruleset
