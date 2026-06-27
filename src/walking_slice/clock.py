"""Clock protocol and concrete implementations for the first walking slice.

Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"Cross-Cutting
Concerns" — *Time*:

    All recorded timestamps are UTC ISO-8601 with millisecond precision for
    audit (Requirements 2.5, 7.2, 13.1) and second precision for most domain
    fields (Requirements 4.2, 12.1, 12.5). The slice uses a single ``Clock``
    protocol so tests can inject deterministic time.

This module provides:

- :class:`Clock` — the runtime-checkable protocol every service depends on.
- :class:`SystemClock` — production implementation backed by
  :func:`datetime.now`, normalized to UTC and truncated to millisecond
  precision so that values produced by this clock round-trip through the
  millisecond-precision storage required by Requirement 13.1.
- :class:`FixedClock` — deterministic implementation used by tests; the
  fixed value is normalized to UTC and truncated to millisecond precision so
  generated examples cannot drift from the storage contract.

Both implementations return :class:`datetime.datetime` objects that are
timezone-aware (``tzinfo == timezone.utc``) and whose ``microsecond`` field is
always an integral multiple of 1 000 (millisecond precision).

Requirements satisfied:
    2.5  — millisecond-precision recorded time for audit appends.
    4.2  — second-precision (or better) recorded time on Relationships.
    6.2  — second-precision (or better) recorded time on Decision records.
    12.1 — millisecond-precision evaluation timestamps for authorization.
    13.1 — millisecond-precision UTC timestamps for every Audit_Records row.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


__all__ = ["Clock", "SystemClock", "FixedClock", "truncate_to_milliseconds"]


def truncate_to_milliseconds(value: datetime) -> datetime:
    """Normalize ``value`` to UTC and truncate to millisecond precision.

    Any sub-millisecond microseconds are discarded by integer division so that
    every value returned by a :class:`Clock` implementation matches the
    millisecond-precision storage contract from design §"Cross-Cutting
    Concerns".

    Args:
        value: A timezone-aware :class:`datetime.datetime`.

    Returns:
        A :class:`datetime.datetime` in UTC whose ``microsecond`` field is an
        integral multiple of 1 000.

    Raises:
        ValueError: If ``value`` is naive (no ``tzinfo``).
    """
    if value.tzinfo is None:
        raise ValueError(
            "Clock values must be timezone-aware; received naive datetime "
            f"{value!r}."
        )
    utc_value = value.astimezone(timezone.utc)
    truncated_micros = (utc_value.microsecond // 1_000) * 1_000
    return utc_value.replace(microsecond=truncated_micros)


@runtime_checkable
class Clock(Protocol):
    """Protocol every service depends on to read the current time.

    Implementations MUST return a timezone-aware :class:`datetime.datetime`
    in UTC with millisecond precision. The protocol is intentionally minimal
    so the production :class:`SystemClock` and the test :class:`FixedClock`
    are interchangeable via constructor injection through ``RequestContext``
    (design §"Application-Level Composition").
    """

    def now(self) -> datetime:
        """Return the current time as a UTC datetime with ms precision."""
        ...


@dataclass(frozen=True)
class SystemClock:
    """Production :class:`Clock` backed by :func:`datetime.now`.

    Reads the operating-system clock, normalizes the result to UTC, and
    truncates sub-millisecond precision so that values produced by this clock
    round-trip through the millisecond-precision storage contract required by
    Requirements 2.5 and 13.1.
    """

    def now(self) -> datetime:
        """Return the current wall-clock time in UTC, truncated to milliseconds."""
        return truncate_to_milliseconds(datetime.now(timezone.utc))


@dataclass(frozen=True)
class FixedClock:
    """Deterministic :class:`Clock` used by property and example tests.

    The clock returns the same fixed value on every call to :meth:`now`. The
    value is normalized to UTC and truncated to millisecond precision at
    construction time so callers do not need to defensively re-truncate.

    Attributes:
        fixed_time: The :class:`datetime.datetime` returned by every call to
            :meth:`now`. Must be timezone-aware; tests typically pass a UTC
            value such as ``datetime(2026, 1, 1, tzinfo=timezone.utc)``.
    """

    fixed_time: datetime

    def __post_init__(self) -> None:
        # ``frozen=True`` prevents direct attribute assignment, so normalize
        # via ``object.__setattr__`` after dataclass initialization.
        object.__setattr__(
            self, "fixed_time", truncate_to_milliseconds(self.fixed_time)
        )

    def now(self) -> datetime:
        """Return the fixed UTC datetime supplied at construction."""
        return self.fixed_time
