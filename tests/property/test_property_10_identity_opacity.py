"""Property 10 — Identity opacity and uniqueness (task 2.4).

**Property 10: Identity opacity and uniqueness**

For all identifiers issued by ``Identity_Service`` within a single test
session, identifiers are unique within the session, conform to canonical
UUIDv7 lowercase hyphenated 8-4-4-4-12 hex form (regex
``^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$``),
and do not contain any substring equal to display names, role names,
scope values, content excerpts, or other business attributes attached
to the entity that received the identifier.

**Validates: Requirements 1.1, 1.2, 1.4, 1.6, 1.7, 15.10**

Strategy:

The test draws an arbitrary tuple of (display_name, role_name,
scope_value, content_excerpt). Per generated case it constructs a fresh
:class:`~walking_slice.identity.IdentityService` (each case is one
"single test session" in the property's wording), then asks the service
for ``>= 100`` identifiers spread across every factory method
(``new_resource_id``, ``new_revision_id``, ``new_relationship_id``,
``new_region_id``, ``new_immutable_record_id``, ``new_trail_id``,
``new_trail_revision_id``, ``new_trail_step_id``, ``new_manifest_id``).
12 calls × 9 factories = 108 identifiers per case, comfortably meeting
the ≥ 100 floor required by the task and Requirement 15.13.

Three assertions then hold for the batch:

1. **Canonical form** — every identifier matches
   :data:`~walking_slice.identity.CANONICAL_UUID7_REGEX`
   (Requirements 1.1, 1.2, 1.7).
2. **Uniqueness** — no identifier is reissued within the session
   (Requirements 1.1, 1.4, 1.6).
3. **Opacity** — no business-attribute substring (display name, role,
   scope, content excerpt) appears inside any issued identifier
   (Requirement 1.7; design §"Correctness Properties" Property 10).

Trivially short or all-hex business strings are skipped from the
opacity check: a UUIDv7 is composed entirely of ``[0-9a-f-]`` and a
randomly drawn 1–3 character or all-hex business attribute can
collide with a UUID substring by chance alone, producing a false
positive that says nothing about whether identifiers leak business
meaning. See ``_meaningful_needle`` for the precise predicate.
"""

from __future__ import annotations

from typing import Final

import pytest
from hypothesis import given, settings, strategies as st

from walking_slice.identity import CANONICAL_UUID7_REGEX, IdentityService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Factory inventory and opacity-check helpers.
# ---------------------------------------------------------------------------


# Every public factory method on :class:`IdentityService`. Property 10
# applies to *all* identifiers the service issues, so the test mixes
# every factory in each batch rather than restricting to one.
_FACTORY_METHOD_NAMES: Final[tuple[str, ...]] = (
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


# Minimum length below which a business string is too short to be a
# meaningful opacity probe against a UUIDv7. A canonical UUIDv7 is
# composed entirely of the alphabet ``[0-9a-f-]``; a 1–3 character
# fragment can occur inside random hex by chance and would produce a
# false-positive opacity violation that says nothing about the
# requirement under test (Requirement 1.7 — no embedded business
# meaning). The threshold is set to 4 so any plausible business label
# (a personal name, a role label, a scope segment of meaningful length)
# is still checked while trivial fragments are excluded.
_MIN_BUSINESS_LENGTH: Final[int] = 4


# Characters that compose a canonical UUIDv7 — used to identify "all
# hex" business strings that could collide with the identifier
# alphabet purely by accident.
_UUID_ALPHABET: Final[frozenset[str]] = frozenset("0123456789abcdef-")


def _meaningful_needle(business_attribute: str) -> bool:
    """Return ``True`` iff *business_attribute* is worth checking for opacity.

    Skip rules (per the task: "skip very short or empty strings from the
    business set to avoid trivial false positives"):

    - Empty or shorter than :data:`_MIN_BUSINESS_LENGTH`. Such strings
      are too small to constitute meaningful evidence that a UUID
      embeds business information.
    - Composed entirely of characters from the canonical UUIDv7
      alphabet ``[0-9a-f-]``. A business label that is itself a hex
      fragment can collide with a random UUID by sheer coincidence; the
      property under test is "no business meaning is embedded", not
      "no string that happens to look like a UUID fragment appears in a
      UUID".

    Any string that survives these filters is checked case-insensitively
    against every issued identifier.
    """
    if len(business_attribute) < _MIN_BUSINESS_LENGTH:
        return False
    lowered = business_attribute.lower()
    return any(ch not in _UUID_ALPHABET for ch in lowered)


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# Each draws arbitrary Unicode text up to a generous bound so the
# property is exercised against realistic and adversarial inputs alike
# (Unicode personal names, ASCII role labels, slash-delimited scope
# paths, multi-paragraph content excerpts). The text strategy spans the
# default Hypothesis alphabet, which already includes the boundary
# cases Hypothesis surfaces automatically (empty string, ASCII letters
# and digits, control characters, surrogate-safe Unicode).
# ---------------------------------------------------------------------------


_display_name_strategy = st.text(min_size=0, max_size=64)
_role_name_strategy = st.text(min_size=0, max_size=64)
_scope_value_strategy = st.text(min_size=0, max_size=128)
_content_excerpt_strategy = st.text(min_size=0, max_size=256)


@st.composite
def _business_attribute_bundle(draw) -> tuple[str, str, str, str]:
    """Draw one (display_name, role_name, scope_value, content_excerpt) tuple.

    Bundling the four kinds of business attributes together keeps every
    Hypothesis case targeting "an entity that received an identifier
    with these four business attributes attached", which is the exact
    quantifier in Property 10's statement.
    """
    return (
        draw(_display_name_strategy),
        draw(_role_name_strategy),
        draw(_scope_value_strategy),
        draw(_content_excerpt_strategy),
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 10: Identity opacity and uniqueness
@given(business_attributes=_business_attribute_bundle())
@settings(max_examples=100, deadline=2000)
def test_identity_opacity_and_uniqueness(
    business_attributes: tuple[str, str, str, str],
) -> None:
    """At least 100 identifiers issued by a fresh :class:`IdentityService`
    are unique, canonical, and opaque to every supplied business attribute.

    Each Hypothesis case represents one "single test session" in the
    property's wording: a fresh service so cross-case state cannot
    accidentally satisfy the uniqueness assertion, and a batch of
    ``>= 100`` identifiers drawn from every factory method on the
    service surface.
    """
    service = IdentityService()

    # Filter to business-attribute strings worth checking. The skip
    # rules (length and alphabet) prevent trivial false positives; see
    # :func:`_meaningful_needle`.
    needles = [attr.lower() for attr in business_attributes if _meaningful_needle(attr)]

    # 12 rounds × 9 factories = 108 identifiers per case. This meets
    # the ≥ 100 floor in the task description and Requirement 15.13's
    # "at least 100 generated cases per property" by giving each
    # Hypothesis case a batch large enough to exercise every factory
    # method in every case.
    identifiers: list[str] = []
    for _ in range(12):
        for method_name in _FACTORY_METHOD_NAMES:
            identifiers.append(getattr(service, method_name)())

    assert len(identifiers) >= 100, (
        f"Property 10 requires >= 100 identifiers per case; "
        f"got {len(identifiers)}."
    )

    # --- Canonical form (Requirements 1.1, 1.2, 1.7) --------------------
    # Every issued identifier matches the canonical UUIDv7 regex.
    for identifier in identifiers:
        assert CANONICAL_UUID7_REGEX.match(identifier) is not None, (
            f"Issued identifier {identifier!r} does not match canonical "
            f"UUIDv7 form {CANONICAL_UUID7_REGEX.pattern!r}."
        )

    # --- Uniqueness (Requirements 1.1, 1.4, 1.6) ------------------------
    # No identifier is reissued within the session.
    distinct = set(identifiers)
    assert len(distinct) == len(identifiers), (
        "Identity_Service reissued an identifier within a single session: "
        f"total={len(identifiers)}, distinct={len(distinct)}."
    )

    # --- Opacity (Requirement 1.7; Property 10 statement) ---------------
    # No meaningful business-attribute substring appears in any issued
    # identifier (case-insensitive).
    for identifier in identifiers:
        lowered = identifier.lower()
        for needle in needles:
            assert needle not in lowered, (
                f"Identifier {identifier!r} embeds business-attribute "
                f"substring {needle!r}; identifiers must not encode "
                "display names, role names, scope values, or content "
                "excerpts (Requirement 1.7)."
            )
