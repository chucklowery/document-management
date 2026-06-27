"""Unit tests for :mod:`walking_slice.identity` (task 2.1).

Coverage scope (per task 2.1):

- Canonical UUIDv7 form: every factory method returns a string matching
  the lowercase 8-4-4-4-12 hex regex from design §"Cross-Cutting Concerns".
- UUIDv7 version and variant bits: the third hex group starts with ``7``
  and the fourth hex group starts with one of ``[89ab]`` (variant bits
  ``10``).
- Uniqueness: at least 1 000 generated identifiers per session, drawn
  across every factory method on the surface, are all distinct.

Conflict-rejection unit tests for :meth:`IdentityService.reject_if_duplicate`
on the *persisted* registry are deferred to task 2.3. The smoke tests
here only exercise the in-memory branch wired by task 2.1 so a
regression in this task's surface surfaces immediately.
"""

from __future__ import annotations

import re
from typing import Callable

import pytest

from walking_slice.identity import (
    CANONICAL_UUID7_REGEX,
    IdentityConflictError,
    IdentityFormatError,
    IdentityService,
)


pytestmark = pytest.mark.unit


# Every public factory method on :class:`IdentityService`. Parametrising the
# tests over this list makes "every factory" coverage explicit and means a
# new factory added later will only pass these tests after a deliberate
# update to this list.
FACTORY_METHOD_NAMES = (
    "new_resource_id",
    "new_revision_id",
    "new_relationship_id",
    "new_region_id",
    "new_immutable_record_id",
    "new_trail_id",
    "new_trail_revision_id",
    "new_trail_step_id",
    "new_manifest_id",
)


@pytest.fixture
def service() -> IdentityService:
    """Return a fresh in-memory :class:`IdentityService` per test."""
    return IdentityService()


# ---------------------------------------------------------------------------
# Canonical-form regex sanity checks.
#
# These guard against accidental edits to the regex itself; if the regex
# drifts from the contract documented in design §"Cross-Cutting Concerns",
# tests in this file (and Property 10's strategy) silently weaken.
# ---------------------------------------------------------------------------


def test_canonical_regex_accepts_known_valid_uuid7() -> None:
    valid = "019ef20a-95d0-7521-983d-d7d5d81aad60"
    assert CANONICAL_UUID7_REGEX.match(valid) is not None


@pytest.mark.parametrize(
    "invalid_identifier",
    [
        "",  # empty
        "not-a-uuid",
        "019EF20A-95D0-7521-983D-D7D5D81AAD60",  # uppercase rejected
        "019ef20a95d07521983dd7d5d81aad60",  # missing hyphens
        "019ef20a-95d0-6521-983d-d7d5d81aad60",  # version nibble = 6 (UUIDv6)
        "019ef20a-95d0-8521-983d-d7d5d81aad60",  # version nibble = 8 (UUIDv8)
        "019ef20a-95d0-7521-c83d-d7d5d81aad60",  # variant byte starts with c (10xx is required)
        "019ef20a-95d0-7521-783d-d7d5d81aad60",  # variant byte starts with 7 (top bits 0xxx)
        "019ef20a-95d0-7521-983d-d7d5d81aad6",   # too short (11 hex chars in last group)
        "019ef20a-95d0-7521-983d-d7d5d81aad600",  # too long (13 hex chars in last group)
    ],
)
def test_canonical_regex_rejects_invalid_strings(invalid_identifier: str) -> None:
    assert CANONICAL_UUID7_REGEX.match(invalid_identifier) is None


# ---------------------------------------------------------------------------
# validate_canonical
# ---------------------------------------------------------------------------


def test_validate_canonical_accepts_known_valid(service: IdentityService) -> None:
    assert service.validate_canonical("019ef20a-95d0-7521-983d-d7d5d81aad60") is True


@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "not-a-uuid",
        "019EF20A-95D0-7521-983D-D7D5D81AAD60",
        "019ef20a-95d0-6521-983d-d7d5d81aad60",
        "019ef20a-95d0-7521-c83d-d7d5d81aad60",
    ],
)
def test_validate_canonical_rejects_invalid_strings(
    service: IdentityService, invalid: str
) -> None:
    assert service.validate_canonical(invalid) is False


@pytest.mark.parametrize(
    "non_string",
    [None, 42, 3.14, object(), b"019ef20a-95d0-7521-983d-d7d5d81aad60"],
)
def test_validate_canonical_returns_false_for_non_string_inputs(
    service: IdentityService, non_string: object
) -> None:
    assert service.validate_canonical(non_string) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Factory methods — canonical form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method_name", FACTORY_METHOD_NAMES)
def test_factory_returns_canonical_uuid7_string(
    service: IdentityService, method_name: str
) -> None:
    """Every factory returns a canonical UUIDv7 string."""
    method: Callable[[], str] = getattr(service, method_name)
    identifier = method()
    assert isinstance(identifier, str)
    assert CANONICAL_UUID7_REGEX.match(identifier) is not None, identifier


@pytest.mark.parametrize("method_name", FACTORY_METHOD_NAMES)
def test_factory_output_validates_through_validate_canonical(
    service: IdentityService, method_name: str
) -> None:
    """``validate_canonical`` must accept every factory's own output."""
    method: Callable[[], str] = getattr(service, method_name)
    assert service.validate_canonical(method()) is True


# ---------------------------------------------------------------------------
# UUIDv7 version and variant bit checks
# ---------------------------------------------------------------------------


def _version_nibble(identifier: str) -> int:
    """Extract the version nibble — first hex char of the third group."""
    return int(identifier.split("-")[2][0], 16)


def _variant_top_two_bits(identifier: str) -> int:
    """Extract the top two bits of the variant byte (first hex char of group 4)."""
    nibble = int(identifier.split("-")[3][0], 16)
    return (nibble >> 2) & 0b11


@pytest.mark.parametrize("method_name", FACTORY_METHOD_NAMES)
def test_factory_sets_version_nibble_to_seven(
    service: IdentityService, method_name: str
) -> None:
    """UUIDv7 places ``7`` in the version-nibble position (12 of 16 hex chars in)."""
    identifier = getattr(service, method_name)()
    assert _version_nibble(identifier) == 7


@pytest.mark.parametrize("method_name", FACTORY_METHOD_NAMES)
def test_factory_sets_variant_bits_to_rfc_4122(
    service: IdentityService, method_name: str
) -> None:
    """UUIDv7's variant bits are ``10`` (the RFC 4122 / DCE 1.1 variant)."""
    identifier = getattr(service, method_name)()
    assert _variant_top_two_bits(identifier) == 0b10


# ---------------------------------------------------------------------------
# Uniqueness across at least 1 000 generated identifiers
# ---------------------------------------------------------------------------


def test_factory_methods_generate_distinct_ids_within_session(
    service: IdentityService,
) -> None:
    """A single service instance never reissues the same identifier."""
    identifiers = {service.new_resource_id() for _ in range(1024)}
    assert len(identifiers) == 1024


def test_uniqueness_across_every_factory_combined_session(
    service: IdentityService,
) -> None:
    """At least 1 000 identifiers drawn across every factory method are distinct.

    Property 10 ("identity opacity and uniqueness") in the design's
    Correctness Properties section requires uniqueness "within a single
    test session". This example test enforces that property for an
    interleaved mix of factories so a regression that only affects a
    subset of factories cannot hide behind same-factory testing alone.
    """
    factories = [
        getattr(service, name) for name in FACTORY_METHOD_NAMES
    ]
    # 9 factories * 120 calls = 1 080 identifiers, > 1 000 floor.
    identifiers: set[str] = set()
    for _ in range(120):
        for factory in factories:
            identifiers.add(factory())
    assert len(identifiers) == len(factories) * 120
    # Every issued identifier is canonical — defence in depth.
    assert all(CANONICAL_UUID7_REGEX.match(i) for i in identifiers)


def test_independent_service_instances_do_not_collide() -> None:
    """Two separate :class:`IdentityService` instances issue disjoint sets."""
    a = IdentityService()
    b = IdentityService()
    ids_a = {a.new_resource_id() for _ in range(512)}
    ids_b = {b.new_resource_id() for _ in range(512)}
    # Each session is internally unique.
    assert len(ids_a) == 512
    assert len(ids_b) == 512
    # And the two sessions do not overlap (UUIDv7's time + random fields
    # make collisions astronomically unlikely; a collision here is a
    # regression in the underlying generator).
    assert ids_a.isdisjoint(ids_b)


# ---------------------------------------------------------------------------
# reject_if_duplicate smoke tests (deeper coverage lands in task 2.3)
# ---------------------------------------------------------------------------


def test_reject_if_duplicate_raises_format_error_on_malformed_input(
    service: IdentityService,
) -> None:
    with pytest.raises(IdentityFormatError):
        service.reject_if_duplicate("not-a-uuid", "digest")


def test_reject_if_duplicate_accepts_first_binding(
    service: IdentityService,
) -> None:
    identifier = service.new_resource_id()
    # First binding to a concrete digest succeeds.
    service.reject_if_duplicate(identifier, "digest-a")
    # Idempotent re-confirmation with the same digest also succeeds.
    service.reject_if_duplicate(identifier, "digest-a")


def test_reject_if_duplicate_raises_conflict_on_rebind(
    service: IdentityService,
) -> None:
    identifier = service.new_resource_id()
    service.reject_if_duplicate(identifier, "digest-a")
    with pytest.raises(IdentityConflictError) as exc_info:
        service.reject_if_duplicate(identifier, "digest-b")
    err = exc_info.value
    assert err.identifier == identifier
    assert err.existing_digest == "digest-a"
    assert err.attempted_digest == "digest-b"


def test_reject_if_duplicate_module_regex_matches_published_pattern() -> None:
    """The published regex pattern matches the one specified in the task."""
    expected = (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert CANONICAL_UUID7_REGEX.pattern == expected
    # And the compiled flags do not weaken the pattern (no IGNORECASE etc.).
    assert CANONICAL_UUID7_REGEX.flags & re.IGNORECASE == 0
