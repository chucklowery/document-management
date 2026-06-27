"""Unit tests for :mod:`walking_slice.clock`.

These tests pin the contract established in design §"Cross-Cutting Concerns"
(*Time*): every :class:`Clock` value is UTC and millisecond-precise. The
property tests added later (e.g. Property 11 — audit completeness) rely on
this invariant when comparing recorded times against generated timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from walking_slice.clock import (
    Clock,
    FixedClock,
    SystemClock,
    truncate_to_milliseconds,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# truncate_to_milliseconds
# ---------------------------------------------------------------------------


def test_truncate_drops_sub_millisecond_microseconds() -> None:
    raw = datetime(2026, 1, 1, 12, 30, 45, 123_456, tzinfo=timezone.utc)
    result = truncate_to_milliseconds(raw)
    assert result.microsecond == 123_000
    # Other fields are preserved.
    assert (result.year, result.month, result.day) == (2026, 1, 1)
    assert (result.hour, result.minute, result.second) == (12, 30, 45)


def test_truncate_normalizes_non_utc_to_utc() -> None:
    east_plus_5 = timezone(timedelta(hours=5))
    raw = datetime(2026, 1, 1, 12, 0, 0, 999_999, tzinfo=east_plus_5)
    result = truncate_to_milliseconds(raw)
    assert result.tzinfo == timezone.utc
    # 12:00 +05:00 == 07:00 UTC
    assert (result.hour, result.minute) == (7, 0)
    assert result.microsecond == 999_000


def test_truncate_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        truncate_to_milliseconds(datetime(2026, 1, 1))  # noqa: DTZ001 — intentional


# ---------------------------------------------------------------------------
# SystemClock
# ---------------------------------------------------------------------------


def test_system_clock_returns_utc_datetime() -> None:
    value = SystemClock().now()
    assert value.tzinfo == timezone.utc


def test_system_clock_truncates_to_milliseconds() -> None:
    value = SystemClock().now()
    assert value.microsecond % 1_000 == 0


def test_system_clock_satisfies_clock_protocol() -> None:
    assert isinstance(SystemClock(), Clock)


# ---------------------------------------------------------------------------
# FixedClock
# ---------------------------------------------------------------------------


def test_fixed_clock_returns_supplied_time() -> None:
    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    clock = FixedClock(fixed)
    assert clock.now() == fixed


def test_fixed_clock_is_idempotent() -> None:
    fixed = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    clock = FixedClock(fixed)
    assert clock.now() == clock.now() == fixed


def test_fixed_clock_normalizes_inputs_to_utc_and_milliseconds() -> None:
    east_plus_5 = timezone(timedelta(hours=5))
    raw = datetime(2026, 1, 1, 12, 0, 0, 123_456, tzinfo=east_plus_5)
    clock = FixedClock(raw)
    result = clock.now()
    assert result.tzinfo == timezone.utc
    assert (result.hour, result.minute) == (7, 0)
    assert result.microsecond == 123_000


def test_fixed_clock_rejects_naive_input() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 1, 1))  # noqa: DTZ001 — intentional


def test_fixed_clock_satisfies_clock_protocol() -> None:
    assert isinstance(FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)), Clock)


def test_fixed_clock_is_hashable_and_frozen() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    # Frozen dataclass — assigning a new value must fail.
    with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
        clock.fixed_time = datetime(2030, 1, 1, tzinfo=timezone.utc)  # type: ignore[misc]
    # Frozen dataclasses are hashable.
    assert hash(clock) == hash(
        FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    )


# ---------------------------------------------------------------------------
# clock fixture wiring
# ---------------------------------------------------------------------------


def test_clock_fixture_returns_fixed_clock(clock: Clock) -> None:
    assert isinstance(clock, FixedClock)
    value = clock.now()
    assert value.tzinfo == timezone.utc
    assert value.microsecond % 1_000 == 0
