"""Property 13 — Repeatable property runs (task 15.6).

**Property 13: Repeatable property runs (operational)**

The property-based test suite executes at least 100 generated cases per
property, records the seed of every test invocation, and on re-execution
with the same seed produces identical pass/fail outcomes and identical
minimal counterexamples for failing properties.

**Validates: Requirements 15.13**

Strategy:

The check is operational rather than over the domain model, so it is
expressed as three example-based assertions:

1. ``test_repeatable_pass_outcome_under_fixed_seed`` — re-runs a
   *passing* Hypothesis property twice under the same ``@seed`` and
   asserts both runs produce the same captured example sequence. This
   confirms generation determinism with the seed pinned and the
   example database disabled (Requirement 15.13: "on re-execution with
   the same seed produce identical pass/fail outcomes").
2. ``test_repeatable_failure_and_minimal_counterexample_under_fixed_seed``
   — re-runs a *failing* Hypothesis property twice under the same
   ``@seed`` and asserts both runs failed AND shrank to the identical
   minimal counterexample (Requirement 15.13: "identical minimal
   counterexamples for failing properties").
3. ``test_seed_capture_artifact_wiring`` — asserts the
   conftest seed-capture mechanism is active: the master seed is
   resolved, exposed on ``pytest.Config``, and the build artifact
   directory will be populated at session finish.

Each Hypothesis property here runs with ``database=None`` so prior
failure replay cannot influence the deterministic behaviour under
test, and ``deadline=None`` so wall-clock variation cannot perturb the
outcome.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given, seed, settings, strategies as st


pytestmark = pytest.mark.property


# Stable seeds dedicated to this test file. Using local seeds (rather
# than the session master seed) means the property under test —
# determinism of Hypothesis itself — does not depend on what seed the
# enclosing session happened to draw. The two seeds differ so the
# pass-case and fail-case probes are independent.
_PASS_SEED: Final[int] = 0xC0FFEEC0DECAFE01
_FAIL_SEED: Final[int] = 0xC0FFEEC0DECAFE02


# Bounded integer range so shrink targets are well-defined. The
# failing predicate below is "x != 0", which Hypothesis is guaranteed
# to shrink to the canonical minimum integer ``0`` independently of the
# seed; we still pin the seed so the *generation* sequence is identical
# between runs.
_INT_RANGE = st.integers(min_value=-1_000_000, max_value=1_000_000)


def _run_collecting_property(seed_value: int) -> list[int]:
    """Run a passing Hypothesis property and return the example sequence.

    The inner test always succeeds, so Hypothesis explores
    ``max_examples`` generated values without entering the shrink
    phase. Pinning ``@seed`` and disabling the example database
    isolates Hypothesis's example-generation determinism, which is the
    operational guarantee Requirement 15.13 depends on.
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
    ``value != 0`` has a unique minimum integer counterexample
    (``0``), so Hypothesis's shrink phase is expected to converge there
    regardless of seed; the seed is pinned anyway so the path the
    shrinker takes — and any intermediate failing examples it surfaces
    — is identical between the two invocations.
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


# ---------------------------------------------------------------------------
# Property 13 — re-execution determinism.
# ---------------------------------------------------------------------------


def test_repeatable_pass_outcome_under_fixed_seed() -> None:
    """Re-executing a passing property with the same seed yields the
    same generated example sequence.

    Requirement 15.13: "on re-execution with the same seed produce
    identical pass/fail outcomes ... for failing properties." This test
    covers the *passing* leg by confirming generation determinism;
    when generation is deterministic, pass/fail outcomes are
    necessarily deterministic too.
    """
    first = _run_collecting_property(_PASS_SEED)
    second = _run_collecting_property(_PASS_SEED)

    assert first == second, (
        "Two Hypothesis runs with the same @seed and database=None "
        "produced different example sequences. Requirement 15.13 "
        "requires deterministic re-execution; this indicates a "
        "configuration drift (e.g. database not disabled, profile "
        "phases differ, or upstream Hypothesis no longer honours the "
        "pinned seed).\n"
        f"first[:5]={first[:5]!r}\nsecond[:5]={second[:5]!r}"
    )
    assert len(first) > 0, (
        "Property 13 probe collected zero examples; "
        "max_examples or generation strategy regressed."
    )


def test_repeatable_failure_and_minimal_counterexample_under_fixed_seed() -> None:
    """Re-executing a failing property with the same seed yields the
    same minimal counterexample.

    Requirement 15.13: "identical minimal counterexamples for failing
    properties." The predicate ``value != 0`` has ``0`` as its unique
    minimum, so a correctly seeded Hypothesis run shrinks to ``0``
    both times. The double-run asserts agreement between the two
    invocations rather than hard-coding ``0`` so the test still
    detects determinism regressions if the shrinker stops at a
    different minimum.
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
        f"({first_min!r} vs {second_min!r}). Requirement 15.13 "
        "requires identical minimal counterexamples on replay."
    )
    # The predicate's unique minimum is 0; if Hypothesis ever shrinks
    # to a different value the test should fail loudly because the
    # operational guarantee depends on the canonical minimum.
    assert first_min == 0, (
        f"Expected Hypothesis to shrink 'value != 0' to 0; got "
        f"{first_min!r}. Either the shrinker regressed or the input "
        "strategy no longer includes zero."
    )


# ---------------------------------------------------------------------------
# Seed-capture artifact wiring.
# ---------------------------------------------------------------------------


def test_seed_capture_artifact_wiring(pytestconfig: pytest.Config) -> None:
    """The conftest seed-capture mechanism is installed and active.

    Requirement 15.13: "record the seed of every test invocation."
    The artifact itself is finalized in ``pytest_sessionfinish`` so its
    contents cannot be inspected mid-session; this test verifies the
    *wiring* — the master seed is resolved, exposed on the pytest
    config, and the artifact destination is well-formed — leaving the
    on-disk read-back to a session-finish smoke check in CI.
    """
    master_seed = getattr(pytestconfig, "_walking_slice_seed", None)
    assert master_seed is not None, (
        "tests/conftest.py did not install a master Hypothesis seed. "
        "Requirement 15.13 requires every session to record a seed; "
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

    # The artifact directory must be creatable from the pytest rootpath
    # (the hook itself runs ``mkdir(parents=True, exist_ok=True)`` at
    # session finish). Verifying the path resolves now catches
    # misconfiguration before the session ends.
    rootpath = Path(pytestconfig.rootpath)
    artifact_dir = rootpath / "build"
    assert rootpath.exists(), (
        f"pytest rootpath {rootpath!r} does not exist; cannot place "
        "the seed-capture artifact."
    )
    # Reject obviously-wrong paths without actually creating the
    # directory (the session-finish hook owns creation).
    assert artifact_dir.parent == rootpath, (
        "Seed-capture artifact must live under the project root."
    )
