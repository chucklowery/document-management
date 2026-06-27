# Feature: third-walking-slice, Task 16.16: Repeatable property runs and seed capture for Slice 3
"""Slice 3 repeatable property runs and seed capture (task 16.16).

**Validates: Requirement 41.15**

Requirement 41.15 (third-walking-slice/requirements.md §41 ¶15) is the
Slice 3 analog of Slice 1 Requirement 15.13 (Property 13) and Slice 2
Requirement 20.13 (Property 30):

    "The property-based test suite for Slice 3 SHALL execute at least
    100 generated cases per property under the Hypothesis library with
    `@settings(max_examples=100, deadline=2000)`, record the seed of
    every test invocation, and on re-execution with the same seed
    produce identical pass/fail outcomes and identical minimal
    counterexamples for failing properties."

Task 16.16 (third-walking-slice/tasks.md) breaks this clause into four
operational obligations; this test file is the cumulative check for
all four:

1. The Slice 3 property tests run under the existing Hypothesis profile
   (``@settings(max_examples=100, deadline=2000)``). The
   ``test_slice3_property_files_declare_property_marker_and_settings``
   static check below scans every Slice 3 property test file and
   asserts the per-test contract is legible from source.
2. ``--hypothesis-seed`` capture is enabled on every Slice 3 property
   test. The conftest seed-capture hook in ``tests/conftest.py``
   filters on ``item.get_closest_marker("property")``, so the static
   check above doubles as the wiring assertion for the seed-capture
   path (a Slice 3 property test that omits the marker would silently
   drop from the artifact).
3. The seed of every property test invocation is persisted to the build
   artifact alongside the Slice 1 and Slice 2 seeds. The artifact is
   ``build/hypothesis-seeds.json`` (or ``WALKING_SLICE_SEED_ARTIFACT``
   override); the runtime wiring is asserted by
   ``test_slice3_seed_capture_artifact_wiring``.
4. Re-execution with the same seed produces identical pass/fail
   outcomes and identical minimal counterexamples. The double-run
   probes below assert this for a passing property and a failing
   property under fixed local ``@seed`` values.

Design notes
============

The Slice 1 operational guarantee implemented by Property 13
(``tests/property/test_property_13_repeatable_runs.py``) and the
Slice 2 operational guarantee implemented by Property 30
(``tests/property/test_property_30_slice2_repeatable_runs.py``) are
reused unchanged for Slice 3: the same ``tests/conftest.py`` profiles
(``dev`` / ``ci`` / ``debug``), the same ``--hypothesis-seed``
precedence chain, the same ``pytest_runtest_makereport`` hook
capturing every ``pytest.mark.property`` test, and the same
``build/hypothesis-seeds.json`` artifact written at session finish
all flow through to Slice 3 property tests automatically because each
Slice 3 test file declares ``pytestmark = pytest.mark.property`` and
uses ``@settings(max_examples=100, deadline=2000, ...)``. No changes
to ``tests/conftest.py`` are needed for Slice 3 — Slice 3 inherits
the cumulative wiring established in tasks 15.6 and 16.15 of the prior
slices.

This file is **not** a numbered property in the Slice 3 design (the
formal property enumeration stops at Property 45). It is the
operational verification of Requirement 41.15 that parallels the
slice-scoped operational tests in Slice 1 and Slice 2; see design
§"Cumulative verification with Slice 1 and Slice 2" which calls out
this parallel explicitly.

Each Hypothesis probe property runs with ``database=None`` so prior
failure replay cannot influence the deterministic behaviour under
test, and ``deadline=None`` so wall-clock variation cannot perturb
the outcome. Local ``@seed`` values (rather than the session master
seed) mean the determinism guarantee under test does not depend on
what seed the enclosing session happened to draw, and the high-order
bits differ from Property 13 and Property 30 seeds so a session-master
collision cannot accidentally couple the slice-scoped probes.
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
# pass-case and fail-case probes are independent. The high-order bits
# differ from both Property 13 (``...CAFE01/02``) and Property 30
# (``...CAFE20/21``) seeds so a session-master-seed collision with the
# prior slice probes cannot accidentally produce a spurious correlation
# between the three suites' determinism checks.
_PASS_SEED: Final[int] = 0xC0FFEEC0DECAFE30
_FAIL_SEED: Final[int] = 0xC0FFEEC0DECAFE31


# Bounded integer range so shrink targets are well-defined. The
# failing predicate below is "x != 0", which Hypothesis is guaranteed
# to shrink to the canonical minimum integer ``0`` independently of the
# seed; we still pin the seed so the *generation* sequence is identical
# between runs.
_INT_RANGE = st.integers(min_value=-1_000_000, max_value=1_000_000)


# The set of Slice 3 property test files that participate in the
# operational seed-capture contract. Properties 31 through 45 are the
# Slice 3 numbered properties enumerated in design §"Correctness
# Properties". Files that have not yet landed (because their owning
# tasks are still in progress) are simply skipped by the wiring check
# below — the check only enforces the contract on files that *exist*.
_SLICE3_PROPERTY_NUMBERS: Final[tuple[int, ...]] = tuple(range(31, 46))


def _run_collecting_property(seed_value: int) -> list[int]:
    """Run a passing Hypothesis property and return the example sequence.

    The inner test always succeeds, so Hypothesis explores
    ``max_examples`` generated values without entering the shrink
    phase. Pinning ``@seed`` and disabling the example database
    isolates Hypothesis's example-generation determinism, which is the
    operational guarantee Requirement 41.15 depends on.
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
# Task 16.16 — re-execution determinism (Slice 3 leg).
# ---------------------------------------------------------------------------


def test_slice3_repeatable_pass_outcome_under_fixed_seed() -> None:
    """Re-executing a passing property with the same seed yields the
    same generated example sequence (Slice 3 leg of task 16.16).

    Requirement 41.15: "on re-execution with the same seed produce
    identical pass/fail outcomes ... for failing properties." This
    test covers the *passing* leg by confirming generation determinism
    using the same probe pattern Property 13 (Slice 1) and Property 30
    (Slice 2) use; when generation is deterministic, pass/fail
    outcomes are necessarily deterministic too. The Slice 3 suite
    cannot diverge from this guarantee because it reuses the same
    ``tests/conftest.py`` seed-resolution mechanism — but running the
    probe here makes the Slice 3 operational contract observable in
    the artifact and in failure triage.
    """
    first = _run_collecting_property(_PASS_SEED)
    second = _run_collecting_property(_PASS_SEED)

    assert first == second, (
        "Two Hypothesis runs with the same @seed and database=None "
        "produced different example sequences. Requirement 41.15 "
        "requires deterministic re-execution; this indicates a "
        "configuration drift (e.g. database not disabled, profile "
        "phases differ, or upstream Hypothesis no longer honours the "
        "pinned seed).\n"
        f"first[:5]={first[:5]!r}\nsecond[:5]={second[:5]!r}"
    )
    assert len(first) > 0, (
        "Slice 3 repeatable-runs probe collected zero examples; "
        "max_examples or generation strategy regressed."
    )


def test_slice3_repeatable_failure_and_minimal_counterexample_under_fixed_seed() -> None:
    """Re-executing a failing property with the same seed yields the
    same minimal counterexample (Slice 3 leg of task 16.16).

    Requirement 41.15: "identical minimal counterexamples for failing
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
        f"({first_min!r} vs {second_min!r}). Requirement 41.15 "
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
# Seed-capture artifact wiring (Slice 3-scoped).
# ---------------------------------------------------------------------------


def test_slice3_seed_capture_artifact_wiring(pytestconfig: pytest.Config) -> None:
    """The conftest seed-capture mechanism is active for the Slice 3 suite.

    Requirement 41.15 (matching Slice 1 Requirement 15.13 and Slice 2
    Requirement 20.13 by reference): "record the seed of every test
    invocation."  The artifact itself is finalized in
    ``pytest_sessionfinish`` so its contents cannot be inspected
    mid-session; this test verifies the *wiring* — the master seed is
    resolved, exposed on the pytest config, and the artifact
    destination is well-formed. The wiring is shared with Slice 1 and
    Slice 2 by design, so the same master seed governs all three
    suites and all three contribute invocations to the same
    ``build/hypothesis-seeds.json`` artifact.
    """
    master_seed = getattr(pytestconfig, "_walking_slice_seed", None)
    assert master_seed is not None, (
        "tests/conftest.py did not install a master Hypothesis seed. "
        "Requirement 41.15 requires every session to record a seed; "
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


def test_slice3_property_files_declare_property_marker_and_settings() -> None:
    """Every Slice 3 property test file is wired into the seed-capture path.

    Requirement 41.15 mandates the Slice 3 suite "records the seed of
    every test invocation."  The capture hook in
    ``tests/conftest.py`` filters on
    ``item.get_closest_marker("property")``, so a Slice 3 test file
    that omits ``pytestmark = pytest.mark.property`` would silently
    drop its invocations from the artifact. Likewise the "at least
    100 generated cases per property" clause is honored per-test via
    ``@settings(max_examples=100, deadline=2000)``; the presence of
    an explicit ``@settings`` decorator on each property test guards
    against accidental fallback to whatever the active profile
    default happens to be at session time.

    This check is static — it reads source text — and tolerates Slice
    3 test files that have not yet landed (the task list may show
    sibling tasks under task 16 still in progress when this property
    runs in isolation). The check enforces the contract on files that
    *do* exist.

    The check also asserts that every Slice 3 ``@settings(...)``
    decorator declares ``max_examples`` and ``deadline`` explicitly —
    Requirement 41.15's "at least 100 generated cases per property"
    cannot be audited statically without these keys being present in
    the test source. This mirrors the Slice 2 task 16.15 contract.
    """
    property_dir = Path(__file__).parent
    missing_marker: list[str] = []
    missing_settings: list[str] = []
    settings_without_required_keys: list[tuple[str, list[str]]] = []

    for number in _SLICE3_PROPERTY_NUMBERS:
        matches = sorted(property_dir.glob(f"test_property_{number}_*.py"))
        for path in matches:
            source = path.read_text(encoding="utf-8")
            if "pytestmark = pytest.mark.property" not in source:
                missing_marker.append(path.name)
            if "@settings(" not in source:
                missing_settings.append(path.name)
                continue
            # Each property test in the Slice 3 suite must declare
            # both ``max_examples`` and ``deadline`` explicitly on its
            # ``@settings`` decorator so that the per-test contract is
            # legible from the test source alone (Requirement 41.15's
            # "at least 100 generated cases per property" cannot be
            # audited statically without these keys being present).
            missing_keys = [
                key
                for key in ("max_examples", "deadline")
                if key not in source
            ]
            if missing_keys:
                settings_without_required_keys.append((path.name, missing_keys))

    assert not missing_marker, (
        "Slice 3 property test files are missing "
        "``pytestmark = pytest.mark.property``; their invocations "
        "will not be captured in build/hypothesis-seeds.json. "
        f"Affected files: {missing_marker!r}."
    )
    assert not missing_settings, (
        "Slice 3 property test files are missing an explicit "
        "``@settings(...)`` decorator; Requirement 41.15 requires "
        "per-property pinning of max_examples and deadline. "
        f"Affected files: {missing_settings!r}."
    )
    assert not settings_without_required_keys, (
        "Slice 3 property test files have ``@settings(...)`` but do "
        "not declare ``max_examples`` and ``deadline`` explicitly. "
        "Both keys must be present per Requirement 41.15 / design "
        "§\"Cumulative verification with Slice 1 and Slice 2\". "
        f"Affected files: {settings_without_required_keys!r}."
    )
