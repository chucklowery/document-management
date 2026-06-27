# Feature: second-walking-slice, Property 30: Repeatable property runs (operational)
"""Property 30 — Repeatable property runs for Slice 2 (task 16.15).

**Property 30: Repeatable property runs (operational)**

The Slice 2 property-based test suite executes at least 100 generated
cases per property, records the seed of every property test invocation,
and on re-execution with the same seed produces identical pass/fail
outcomes and identical minimal counterexamples for failing properties.
The suite runs alongside the Slice 1 property suite and reports both
seed sets to a shared build artifact.

**Validates: Requirements 20.13, 21.3**

Strategy
========

The Slice 1 operational guarantee implemented by Property 13
(``tests/property/test_property_13_repeatable_runs.py``) is reused
unchanged for Slice 2: the same ``tests/conftest.py`` profiles
(``dev`` / ``ci`` / ``debug``), the same ``--hypothesis-seed``
precedence chain, the same ``pytest_runtest_makereport`` hook
capturing every ``pytest.mark.property`` test, and the same
``build/hypothesis-seeds.json`` artifact written at session finish
all flow through to Slice 2 property tests automatically because each
Slice 2 test file declares ``pytestmark = pytest.mark.property`` and
uses ``@settings(max_examples=100, deadline=2000, ...)``.

What this file adds on top of Property 13 is a Slice-2-scoped check
of three things:

1. ``test_slice2_repeatable_pass_outcome_under_fixed_seed`` — re-runs
   a *passing* Hypothesis property twice under the same ``@seed`` and
   asserts both runs produce the same captured example sequence. This
   confirms generation determinism for the Slice 2 suite end-to-end
   (Requirement 20.13: "on re-execution with the same seed produce
   identical pass/fail outcomes").
2. ``test_slice2_repeatable_failure_and_minimal_counterexample_under_fixed_seed``
   — re-runs a *failing* Hypothesis property twice under the same
   ``@seed`` and asserts both runs failed AND shrank to the identical
   minimal counterexample (Requirement 20.13: "identical minimal
   counterexamples for failing properties").
3. ``test_slice2_seed_capture_artifact_includes_slice2_tests`` —
   asserts the conftest seed-capture mechanism is wired so that every
   Slice 2 property test file is eligible for capture: each
   ``test_property_{16..29}_*.py`` file declares
   ``pytestmark = pytest.mark.property`` and uses ``@settings(...)``
   with ``deadline`` and ``max_examples`` explicitly pinned. This is
   the static counterpart of the runtime artifact written by
   ``pytest_sessionfinish`` — verifying the wiring here means a
   passing session necessarily produces a Slice 2 entry alongside
   Slice 1 entries in ``build/hypothesis-seeds.json``.

Each Hypothesis probe property runs with ``database=None`` so prior
failure replay cannot influence the deterministic behaviour under
test, and ``deadline=None`` so wall-clock variation cannot perturb the
outcome. Local ``@seed`` values (rather than the session master seed)
mean the determinism guarantee under test does not depend on what
seed the enclosing session happened to draw.
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
# differ from Property 13's seeds so a session-master-seed collision
# with Property 13 cannot accidentally produce a spurious correlation
# between the two suites' probes.
_PASS_SEED: Final[int] = 0xC0FFEEC0DECAFE20
_FAIL_SEED: Final[int] = 0xC0FFEEC0DECAFE21


# Bounded integer range so shrink targets are well-defined. The
# failing predicate below is "x != 0", which Hypothesis is guaranteed
# to shrink to the canonical minimum integer ``0`` independently of the
# seed; we still pin the seed so the *generation* sequence is identical
# between runs.
_INT_RANGE = st.integers(min_value=-1_000_000, max_value=1_000_000)


# The set of Slice 2 property test files that participate in the
# operational seed-capture contract. Properties 16 through 29 are the
# Slice 2 numbered properties enumerated in design §"Mapping properties
# to test files"; property 30 is this file. Files that have not yet
# landed (because their owning tasks are still in progress) are simply
# skipped by the wiring check below — the check only enforces the
# contract on files that *exist*.
_SLICE2_PROPERTY_NUMBERS: Final[tuple[int, ...]] = tuple(range(16, 30))


def _run_collecting_property(seed_value: int) -> list[int]:
    """Run a passing Hypothesis property and return the example sequence.

    The inner test always succeeds, so Hypothesis explores
    ``max_examples`` generated values without entering the shrink
    phase. Pinning ``@seed`` and disabling the example database
    isolates Hypothesis's example-generation determinism, which is the
    operational guarantee Requirement 20.13 depends on.
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
# Property 30 — re-execution determinism (Slice 2 leg).
# ---------------------------------------------------------------------------


def test_slice2_repeatable_pass_outcome_under_fixed_seed() -> None:
    """Re-executing a passing property with the same seed yields the
    same generated example sequence (Slice 2 leg of Property 30).

    Requirement 20.13: "on re-execution with the same seed produce
    identical pass/fail outcomes ... for failing properties." This
    test covers the *passing* leg by confirming generation determinism
    using the same probe pattern Property 13 uses for Slice 1; when
    generation is deterministic, pass/fail outcomes are necessarily
    deterministic too. The Slice 2 suite cannot diverge from this
    guarantee because it reuses the same ``tests/conftest.py``
    seed-resolution mechanism — but running the probe here makes the
    Slice 2 operational contract observable in the artifact and in
    failure triage.
    """
    first = _run_collecting_property(_PASS_SEED)
    second = _run_collecting_property(_PASS_SEED)

    assert first == second, (
        "Two Hypothesis runs with the same @seed and database=None "
        "produced different example sequences. Requirement 20.13 "
        "requires deterministic re-execution; this indicates a "
        "configuration drift (e.g. database not disabled, profile "
        "phases differ, or upstream Hypothesis no longer honours the "
        "pinned seed).\n"
        f"first[:5]={first[:5]!r}\nsecond[:5]={second[:5]!r}"
    )
    assert len(first) > 0, (
        "Property 30 Slice 2 probe collected zero examples; "
        "max_examples or generation strategy regressed."
    )


def test_slice2_repeatable_failure_and_minimal_counterexample_under_fixed_seed() -> None:
    """Re-executing a failing property with the same seed yields the
    same minimal counterexample (Slice 2 leg of Property 30).

    Requirement 20.13: "identical minimal counterexamples for failing
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
        f"({first_min!r} vs {second_min!r}). Requirement 20.13 "
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
# Seed-capture artifact wiring (Slice 2-scoped).
# ---------------------------------------------------------------------------


def test_slice2_seed_capture_artifact_wiring(pytestconfig: pytest.Config) -> None:
    """The conftest seed-capture mechanism is active for the Slice 2 suite.

    Requirement 20.13 (and Requirement 21.3 by reference to Slice 1
    Requirement 15.13): "record the seed of every test invocation."
    The artifact itself is finalized in ``pytest_sessionfinish`` so
    its contents cannot be inspected mid-session; this test verifies
    the *wiring* — the master seed is resolved, exposed on the pytest
    config, and the artifact destination is well-formed. The wiring
    is shared with Slice 1 by design, so the same master seed governs
    both suites and both contribute invocations to the same
    ``build/hypothesis-seeds.json`` artifact.
    """
    master_seed = getattr(pytestconfig, "_walking_slice_seed", None)
    assert master_seed is not None, (
        "tests/conftest.py did not install a master Hypothesis seed. "
        "Requirement 20.13 requires every session to record a seed; "
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


def test_slice2_property_files_declare_property_marker_and_settings() -> None:
    """Every Slice 2 property test file is wired into the seed-capture path.

    Requirement 20.13 mandates the Slice 2 suite "records the seed of
    every test invocation." The capture hook in ``tests/conftest.py``
    filters on ``item.get_closest_marker("property")``, so a Slice 2
    test file that omits ``pytestmark = pytest.mark.property`` would
    silently drop its invocations from the artifact. Likewise the
    "at least 100 generated cases per property" clause is honored
    per-test via ``@settings(max_examples=..., deadline=...)``; the
    presence of an explicit ``@settings`` decorator on each property
    test guards against accidental fallback to whatever the active
    profile default happens to be at session time.

    This check is static — it reads source text — and tolerates Slice
    2 test files that have not yet landed (the task list shows tasks
    16.11 through 16.14 may still be in progress when this property
    runs in isolation). The check enforces the contract on files that
    *do* exist.
    """
    property_dir = Path(__file__).parent
    missing_marker: list[str] = []
    missing_settings: list[str] = []
    settings_without_required_keys: list[tuple[str, list[str]]] = []

    for number in _SLICE2_PROPERTY_NUMBERS:
        matches = sorted(property_dir.glob(f"test_property_{number}_*.py"))
        for path in matches:
            source = path.read_text(encoding="utf-8")
            if "pytestmark = pytest.mark.property" not in source:
                missing_marker.append(path.name)
            if "@settings(" not in source:
                missing_settings.append(path.name)
                continue
            # Each property test in the Slice 2 suite must declare
            # both ``max_examples`` and ``deadline`` explicitly on its
            # ``@settings`` decorator so that the per-test contract is
            # legible from the test source alone (Requirement 20.13's
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
        "Slice 2 property test files are missing "
        "``pytestmark = pytest.mark.property``; their invocations "
        "will not be captured in build/hypothesis-seeds.json. "
        f"Affected files: {missing_marker!r}."
    )
    assert not missing_settings, (
        "Slice 2 property test files are missing an explicit "
        "``@settings(...)`` decorator; Requirement 20.13 requires "
        "per-property pinning of max_examples and deadline. "
        f"Affected files: {missing_settings!r}."
    )
    assert not settings_without_required_keys, (
        "Slice 2 property test files have ``@settings(...)`` but do "
        "not declare ``max_examples`` and ``deadline`` explicitly. "
        "Both keys must be present per Requirement 20.13 / design "
        "§\"Cumulative verification with Slice 1\". "
        f"Affected files: {settings_without_required_keys!r}."
    )
