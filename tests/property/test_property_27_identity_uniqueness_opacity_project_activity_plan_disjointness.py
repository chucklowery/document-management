# Feature: second-walking-slice, Property 27: Identity uniqueness, opacity, and Project / Activity-Plan disjointness
"""Property 27 — Identity uniqueness, opacity, and Project / Activity-Plan
disjointness (task 16.12).

**Property 27: Identity uniqueness, opacity, and Project / Activity-Plan
disjointness**

*For all* identifiers issued by the :class:`Identity_Service` within
any test session covering both slices, identifiers are unique across
both slices and across every Resource kind, are in canonical UUIDv7
lowercase hyphenated form, do not embed business metadata (no Party
Identity, scope value, role name, or display name substring appears
inside the identifier), and the Project Resource identifier set is
disjoint from the Activity Plan Resource identifier set (verified by
inspecting ``Identifier_Registry.resource_kind`` per identifier).

**Validates: Requirements 1.1, 1.2, 1.4, 1.6, 1.7, 4.5, 20.12**

Strategy
========

Each Hypothesis case represents one "single test session" in the
property's wording. Per case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path with both the Slice 1
   schema (:func:`walking_slice.persistence.create_schema`) and the
   Slice 2 schema
   (:func:`walking_slice.planning._persistence.create_planning_schema`)
   so the additive ``Identifier_Registry.resource_kind`` column
   exists.
2. Constructs a fresh :class:`IdentityService` so cross-case
   in-memory state cannot accidentally satisfy the uniqueness
   assertion.
3. Mints ≥ 5 identifiers from each of the 9 Slice 1 factory methods
   (``new_resource_id``, ``new_revision_id``, ``new_relationship_id``,
   ``new_region_id``, ``new_immutable_record_id``, ``new_trail_id``,
   ``new_trail_revision_id``, ``new_trail_step_id``,
   ``new_manifest_id``) — 45 Slice 1 identifiers. These remain
   in-memory only; the Slice 1 surface intentionally does not
   register a row in ``Identifier_Registry`` until a service writes
   a domain row alongside it (AD-WS-5).
4. Registers ≥ 5 identifiers under each of the 13 Slice 2
   ``resource_kind`` tags in :data:`PLANNING_RESOURCE_KINDS` through
   :func:`walking_slice.planning._helpers._record_planning_resource`
   — 65 Slice 2 identifiers, each inserting a row carrying its
   ``resource_kind`` tag in ``Identifier_Registry`` (AD-WS-19).
5. The combined batch contains 110 identifiers per case — comfortably
   above the ≥ 100 floor required by the task and Requirement 20.13.

Four assertions then hold for the batch:

1. **Canonical form** — every identifier matches
   :data:`~walking_slice.identity.CANONICAL_UUID7_REGEX`
   (Requirements 1.1, 1.2, 1.7).
2. **Uniqueness across both slices** — no identifier is reissued
   within the session, whether it came from a Slice 1 factory or a
   Slice 2 registration (Requirements 1.1, 1.4, 1.6, 20.12).
3. **Opacity** — no meaningful business-attribute substring (Party
   Identity, scope value, role name, display name) appears inside
   any issued identifier (Requirement 1.7; design §"Correctness
   Properties" Property 27).
4. **Project / Activity Plan disjointness** — the set of
   ``Identifier_Registry`` rows whose ``resource_kind = 'project'``
   is disjoint from the set whose ``resource_kind = 'activity_plan'``
   (Requirement 4.5).

Trivially short or all-hex business strings are skipped from the
opacity check: a UUIDv7 is composed entirely of ``[0-9a-f-]`` and a
randomly drawn 1–3 character or all-hex business attribute can
collide with a UUID substring by chance alone, producing a false
positive that says nothing about whether identifiers leak business
meaning. See :func:`_meaningful_needle` for the precise predicate.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.identity import CANONICAL_UUID7_REGEX, IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._helpers import (
    PLANNING_RESOURCE_KINDS,
    _record_planning_resource,
)
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Slice inventories.
# ---------------------------------------------------------------------------


# The 9 Slice 1 :class:`IdentityService` factory methods. Property 27
# applies to *all* identifiers the service issues across both slices,
# so the test mixes every factory in each batch rather than restricting
# to one.
_SLICE1_FACTORY_METHOD_NAMES: Final[tuple[str, ...]] = (
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


# Mapping from each Slice 2 ``resource_kind`` tag (sourced from
# :data:`PLANNING_RESOURCE_KINDS`) to the Slice 1 ``Identifier_Registry.kind``
# discriminator under which it is registered. Resource roots use
# ``'resource'``; Revision rows use ``'revision'``; the Plan Approval
# Immutable Record uses ``'immutable_record'``. Derived from design
# §"Planning_Service" — Identifiers paragraph and the Slice 1
# ``Identifier_Registry`` CHECK enumeration.
_RESOURCE_KIND_TO_REGISTRY_KIND: Final[dict[str, str]] = {
    "objective": "resource",
    "objective_revision": "revision",
    "intended_outcome": "resource",
    "intended_outcome_revision": "revision",
    "project": "resource",
    "project_revision": "revision",
    "deliverable_expectation": "resource",
    "deliverable_expectation_revision": "revision",
    "activity_plan": "resource",
    "plan_revision": "revision",
    "plan_review": "resource",
    "plan_review_revision": "revision",
    "plan_approval": "immutable_record",
}


# Sanity invariant guarded at module import: every value the helper
# accepts is covered, so future extensions to ``PLANNING_RESOURCE_KINDS``
# raise immediately rather than silently dropping coverage.
assert set(_RESOURCE_KIND_TO_REGISTRY_KIND.keys()) == PLANNING_RESOURCE_KINDS, (
    "Property 27 must cover every PLANNING_RESOURCE_KINDS value; "
    f"missing {PLANNING_RESOURCE_KINDS - set(_RESOURCE_KIND_TO_REGISTRY_KIND.keys())!r}, "
    f"extra {set(_RESOURCE_KIND_TO_REGISTRY_KIND.keys()) - PLANNING_RESOURCE_KINDS!r}."
)


# Number of identifiers minted per factory and per Slice 2 resource_kind.
# 9 factories × 5 = 45 Slice 1; 13 resource_kinds × 5 = 65 Slice 2;
# total = 110 ≥ 100 per case (Requirement 20.13 / task floor).
_IDENTIFIERS_PER_KIND: Final[int] = 5


# ---------------------------------------------------------------------------
# Fixed clock and constants.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Opacity-check helpers.
#
# Skip rules per the task: "skip very short or empty strings from the
# business set to avoid trivial false positives". A canonical UUIDv7
# is composed entirely of ``[0-9a-f-]``; a 1–3 character fragment can
# occur inside random hex by chance and would produce a false-positive
# opacity violation that says nothing about Requirement 1.7. The
# threshold is set to 4 so any plausible business label (a personal
# name, a role label, a scope segment of meaningful length) is still
# checked while trivial fragments are excluded.
# ---------------------------------------------------------------------------


_MIN_BUSINESS_LENGTH: Final[int] = 4


# Characters that compose a canonical UUIDv7 — used to identify "all
# hex" business strings that could collide with the identifier
# alphabet purely by accident.
_UUID_ALPHABET: Final[frozenset[str]] = frozenset("0123456789abcdef-")


def _meaningful_needle(business_attribute: str) -> bool:
    """Return ``True`` iff *business_attribute* is worth checking for opacity.

    A string is meaningful when it is at least
    :data:`_MIN_BUSINESS_LENGTH` characters long *and* contains at
    least one character outside the canonical UUIDv7 alphabet.
    """
    if len(business_attribute) < _MIN_BUSINESS_LENGTH:
        return False
    lowered = business_attribute.lower()
    return any(ch not in _UUID_ALPHABET for ch in lowered)


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case identifiers and registry rows cannot
# leak between cases (design §"Testing Strategy"). The engine carries
# both the Slice 1 schema (which adds the
# ``Identifier_Registry.resource_kind`` column in task 1.2) and the
# Slice 2 schema (so the test reads through the same DDL surface the
# Planning_Service uses).
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    and both the Slice 1 and Slice 2 schemas installed."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    create_planning_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# Each case draws one tuple of business attributes that the opacity
# assertion checks against every issued identifier. The text strategy
# spans the default Hypothesis alphabet, which already includes the
# boundary cases Hypothesis surfaces automatically (empty string,
# ASCII letters and digits, control characters, surrogate-safe
# Unicode). The size bounds match realistic Party-identifier,
# scope-path, role-label, and display-name lengths.
# ---------------------------------------------------------------------------


_business_attribute_bundle = st.fixed_dictionaries(
    {
        # Stand-in for ``Party_Id``: any Unicode string of plausible size.
        "party_id": st.text(min_size=0, max_size=64),
        # Stand-in for ``applicable_scope`` payload (slash-delimited
        # path or arbitrary opaque scope identifier).
        "scope": st.text(min_size=0, max_size=128),
        # Stand-in for a contextual role label.
        "role_name": st.text(min_size=0, max_size=64),
        # Stand-in for a Party or Resource display name.
        "display_name": st.text(min_size=0, max_size=64),
    }
)


# ===========================================================================
# Property 27 — Identity uniqueness, opacity, and Project / Activity Plan
# disjointness.
# ===========================================================================


# Feature: second-walking-slice, Property 27: Identity uniqueness, opacity, and Project / Activity-Plan disjointness
@given(business_attributes=_business_attribute_bundle)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_identity_uniqueness_opacity_and_project_activity_plan_disjointness(
    business_attributes: dict[str, str],
) -> None:
    """**Validates: Requirements 1.1, 1.2, 1.4, 1.6, 1.7, 4.5, 20.12**

    For every fresh :class:`IdentityService` session, the union of
    Slice 1 factory-minted identifiers and Slice 2 registry-tagged
    identifiers (≥ 100 per case) satisfies the four invariants:

    1. Every identifier is in canonical UUIDv7 lowercase 8-4-4-4-12
       hex form.
    2. No identifier is reissued within the session.
    3. No meaningful business-attribute substring (Party Identity,
       scope value, role name, or display name) appears inside any
       issued identifier.
    4. The set of identifiers tagged ``resource_kind='project'`` in
       ``Identifier_Registry`` is disjoint from the set tagged
       ``resource_kind='activity_plan'``.
    """
    party_id = business_attributes["party_id"]
    scope = business_attributes["scope"]
    role_name = business_attributes["role_name"]
    display_name = business_attributes["display_name"]

    with tempfile.TemporaryDirectory(prefix="prop27_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            # Fresh per-case service — a Hypothesis shrink cannot
            # accidentally satisfy uniqueness through cross-case
            # in-memory bleed.
            clock = FixedClock(_NOW)
            identity_service = IdentityService(engine=engine, clock=clock)

            # -- Slice 1 factories -----------------------------------------
            # 9 factories × _IDENTIFIERS_PER_KIND = 45 identifiers when
            # the constant is 5. These remain in the in-memory
            # registry; Slice 1's contract is that
            # ``Identifier_Registry`` receives the row only when a
            # domain write happens alongside it (AD-WS-5).
            slice1_identifiers: list[str] = []
            for method_name in _SLICE1_FACTORY_METHOD_NAMES:
                factory = getattr(identity_service, method_name)
                for _ in range(_IDENTIFIERS_PER_KIND):
                    slice1_identifiers.append(factory())

            # -- Slice 2 registrations -------------------------------------
            # 13 resource_kinds × _IDENTIFIERS_PER_KIND = 65 identifiers
            # when the constant is 5. Each is INSERTed into
            # ``Identifier_Registry`` with its ``resource_kind`` tag so
            # the disjointness query below has rows to inspect.
            slice2_identifiers: list[str] = []
            slice2_kind_pairs: list[tuple[str, str]] = []
            for resource_kind, registry_kind in (
                _RESOURCE_KIND_TO_REGISTRY_KIND.items()
            ):
                for _ in range(_IDENTIFIERS_PER_KIND):
                    # Mint a fresh identifier through the appropriate
                    # Slice 1 factory so canonical-form generation
                    # remains exercised end-to-end.
                    if registry_kind == "resource":
                        identifier = identity_service.new_resource_id()
                    elif registry_kind == "revision":
                        identifier = identity_service.new_revision_id()
                    else:  # immutable_record
                        identifier = identity_service.new_immutable_record_id()

                    # Register the binding under its Slice 2
                    # ``resource_kind`` tag inside a fresh transaction
                    # so each INSERT commits independently — matching
                    # the way Planning_Service service classes use
                    # ``_record_planning_resource``.
                    #
                    # The content digest is keyed off the identifier
                    # itself so every binding is unique and the
                    # identifier-conflict branch (Requirement 1.4) is
                    # not exercised here. Property 27 quantifies over
                    # *issued* identifiers, not over conflict handling.
                    digest = f"prop27-digest-{identifier}"
                    with engine.begin() as conn:
                        _record_planning_resource(
                            connection=conn,
                            registry_kind=registry_kind,
                            resource_kind=resource_kind,
                            identifier=identifier,
                            content_digest=digest,
                            identity_service=identity_service,
                            recorded_time=_NOW,
                        )
                    slice2_identifiers.append(identifier)
                    slice2_kind_pairs.append((identifier, resource_kind))

            all_identifiers: list[str] = slice1_identifiers + slice2_identifiers

            # The combined batch must comfortably exceed the ≥ 100 floor.
            assert len(all_identifiers) >= 100, (
                f"Property 27 requires >= 100 identifiers per case; "
                f"got {len(all_identifiers)} "
                f"(slice1={len(slice1_identifiers)}, "
                f"slice2={len(slice2_identifiers)})."
            )

            # --- 1. Canonical form (Requirements 1.1, 1.2, 1.7) ----------
            for identifier in all_identifiers:
                assert (
                    CANONICAL_UUID7_REGEX.match(identifier) is not None
                ), (
                    f"Issued identifier {identifier!r} does not match "
                    f"canonical UUIDv7 form {CANONICAL_UUID7_REGEX.pattern!r}."
                )

            # --- 2. Uniqueness across both slices (Requirements 1.1, 1.4, 1.6, 20.12) ----
            distinct = set(all_identifiers)
            assert len(distinct) == len(all_identifiers), (
                "Identity_Service reissued an identifier within a single "
                "session covering both slices: "
                f"total={len(all_identifiers)}, "
                f"distinct={len(distinct)}."
            )

            # --- 3. Opacity (Requirement 1.7) ----------------------------
            # Every business-attribute substring that survives the
            # length / alphabet filter must not appear (case-insensitively)
            # in any issued identifier.
            needles = [
                attr.lower()
                for attr in (party_id, scope, role_name, display_name)
                if _meaningful_needle(attr)
            ]
            for identifier in all_identifiers:
                lowered = identifier.lower()
                for needle in needles:
                    assert needle not in lowered, (
                        f"Identifier {identifier!r} embeds "
                        f"business-attribute substring {needle!r}; "
                        "identifiers must not encode Party Identity, "
                        "scope value, role name, or display name "
                        "(Requirement 1.7)."
                    )

            # --- 4. Project / Activity Plan disjointness (Requirement 4.5) ----
            # Verified through the persisted
            # ``Identifier_Registry.resource_kind`` column per the task
            # description and design §"Property 27".
            with engine.connect() as conn:
                project_rows = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT identifier FROM Identifier_Registry "
                            "WHERE resource_kind = 'project'"
                        )
                    ).all()
                }
                activity_plan_rows = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT identifier FROM Identifier_Registry "
                            "WHERE resource_kind = 'activity_plan'"
                        )
                    ).all()
                }

            # Both sets must be non-empty so the disjointness assertion
            # is exercised (and so a future regression in the seeding
            # loop surfaces immediately rather than silently passing).
            assert len(project_rows) == _IDENTIFIERS_PER_KIND, (
                "Property 27 expected "
                f"{_IDENTIFIERS_PER_KIND} 'project' rows in "
                f"Identifier_Registry; found {len(project_rows)}."
            )
            assert len(activity_plan_rows) == _IDENTIFIERS_PER_KIND, (
                "Property 27 expected "
                f"{_IDENTIFIERS_PER_KIND} 'activity_plan' rows in "
                f"Identifier_Registry; found {len(activity_plan_rows)}."
            )

            overlap = project_rows & activity_plan_rows
            assert overlap == set(), (
                "Project Resource identifier set must be disjoint from "
                "Activity Plan Resource identifier set "
                "(Requirement 4.5); overlapping identifiers: "
                f"{sorted(overlap)!r}."
            )
        finally:
            engine.dispose()
