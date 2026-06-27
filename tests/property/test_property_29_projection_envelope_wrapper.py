# Feature: second-walking-slice, Property 29: Projection envelope wrapper
"""Property 29 — Projection envelope wrapper (task 16.14).

**Property 29: Projection envelope wrapper**

For every planning operation that produces a status-bearing response,
the response carries a :class:`ProjectionEnvelope` whose six required
fields — Projection Definition, source Resource Identities, source
Revision Identities, applicable temporal boundary, generated time, and
derivation indicator — are present and consistent with the inputs the
operation supplied.

**Validates: Requirements 18.1, 18.2**

Strategy
========

Each Hypothesis case draws a non-empty *sequence* of planning operation
specifications (1..6 entries per sequence) and a per-case fixed clock
instant. Every operation specification carries:

- a projected status name drawn from
  :data:`walking_slice.planning._projection.PLANNING_PROJECTED_STATUSES`
  (the set of derived status strings the Planning_Service surfaces —
  ``"Plan Approved"``, ``"Plan Revision draft"``,
  ``"Plan Revision superseded"``, ``"Plan Revision orphaned"``, and
  ``"Provenance incomplete"``);
- the source Resource Identities the projection consulted (0..4
  ``uuid.UUID`` values — the field is required but defaults to an empty
  tuple per :class:`ProjectionEnvelope`);
- the source Revision Identities the projection consulted (0..4
  ``uuid.UUID`` values);
- an applicable temporal boundary chosen at ISO-8601 second precision
  in UTC (microsecond == 0) — the :class:`ProjectionEnvelope` validator
  rejects sub-second values, so the strategy constrains generation to
  the canonical form;
- an optional structured-details payload (0..3 entries) so the test
  covers both the empty-details and the populated-details code paths.

Per case the test constructs a fresh :class:`StatusProjector` with the
Planning_Service Projection Definition registered (via
:func:`planning_projection_registry`) and a :class:`FixedClock` pinned
to the per-case instant. It then iterates the operation sequence,
invoking :func:`wrap_planning_status` for each operation and asserting
the universal envelope-shape invariant on the returned response:

1. The response is a
   :class:`~walking_slice.projection.ProjectedStatusResponse` — the
   projected status is *included* in the response, not withheld
   (Requirement 18.1 — "include alongside the projected status in the
   same response"). The withholding paths (Requirement 18.4) are out of
   scope for Property 29 and are not exercised here.
2. The response carries a
   :class:`~walking_slice.projection.ProjectionEnvelope` whose six
   required fields are populated:

   - **Projection Definition** — equal to
     :data:`PLANNING_PROJECTION_DEFINITION` (Requirement 18.1).
   - **Source Resource Identities** — equal to the input collection
     coerced to a tuple (Requirement 18.1).
   - **Source Revision Identities** — equal to the input collection
     coerced to a tuple (Requirement 18.1).
   - **Applicable temporal boundary** — equal to the input boundary,
     UTC, at second precision (Requirement 18.1).
   - **Generated time** — equal to the projector's clock instant,
     UTC, at second precision (Requirement 18.1).
   - **Derivation indicator** — pinned to ``"derived"`` (Requirement
     18.2 + Principle 5.23).

The sequence framing matters: it asserts the envelope-shape invariant
is *stable across many operations dispatched against the same
projector*, not merely on the first response. A regression that builds
the envelope correctly on the first call but degrades on later calls
(for example, caches a wrong definition, drops source ids, or flips
the derivation indicator) would be caught by the per-operation
assertions inside the loop.

Setup follows the conventions established by Slice 2 property tests:
fresh services per Hypothesis case so any in-memory state cannot bleed
across shrinks, an explicit second-precision clock so generated times
are deterministic, and per-operation assertions inside the loop so a
shrunken counterexample names the operation that violated the
invariant.
"""

from __future__ import annotations

import uuid as uuid_lib
from datetime import datetime, timezone
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from walking_slice.clock import FixedClock
from walking_slice.planning._projection import (
    PLANNING_PROJECTED_STATUSES,
    PLANNING_PROJECTION_DEFINITION,
    PLANNING_PROJECTION_DEFINITION_NAME,
    PLANNING_PROJECTION_DEFINITION_VERSION,
    planning_projection_registry,
    wrap_planning_status,
)
from walking_slice.projection import (
    ProjectedStatusResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
    StatusProjector,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Strategies.
#
# The strategies generate the inputs Property 29 quantifies over. Each
# strategy is named after the envelope field it drives so a shrunken
# counterexample points at the precise input dimension that exposed
# the regression.
# ---------------------------------------------------------------------------


# Sample status strings from the published membership set. ``sorted``
# keeps the draw order deterministic across Python versions so the
# Hypothesis shrink corpus is stable. ``sampled_from`` lets Hypothesis
# bias the draw toward earlier-listed entries during shrinking, which
# is fine here because every entry is symmetric with respect to the
# envelope-shape invariant.
_STATUS_STRATEGY: Final[st.SearchStrategy[str]] = st.sampled_from(
    sorted(PLANNING_PROJECTED_STATUSES)
)


# Source identities. Hypothesis ``uuids`` returns Python :class:`uuid.UUID`
# instances; :class:`ProjectionEnvelope` accepts any UUID for its source-id
# fields (the canonical-UUIDv7 discipline is enforced at identifier-issue
# time by :class:`IdentityService`, not on every consumer of a UUID).
_UUID_STRATEGY: Final[st.SearchStrategy[uuid_lib.UUID]] = st.uuids()


# Applicable temporal boundary. The :class:`ProjectionEnvelope` validator
# requires UTC tzinfo and microsecond == 0. ``st.datetimes`` with
# ``timezones=st.just(timezone.utc)`` produces UTC-aware datetimes
# directly; the ``.map`` step truncates sub-second precision so the
# generated value is always acceptable to the envelope validator.
#
# The 2020..2030 window keeps shrunken counterexamples readable while
# still covering both pre- and post-pilot dates.
_TEMPORAL_BOUNDARY_STRATEGY: Final[st.SearchStrategy[datetime]] = st.datetimes(
    min_value=datetime(2020, 1, 1, 0, 0, 0),
    max_value=datetime(2030, 12, 31, 23, 59, 59),
    timezones=st.just(timezone.utc),
).map(lambda dt: dt.replace(microsecond=0))


# Per-operation details payload. Keys are short ASCII tokens; values are
# short ASCII strings. The wrap helper stores the payload on
# :attr:`ProjectedStatusResponse.details`, not on the envelope itself —
# the envelope is pure metadata — but generating non-empty payloads
# exercises the copy-on-wrap behavior that protects the envelope's
# immutability invariant from producer mutations to the input mapping.
_DETAILS_KEY_ALPHABET: Final[str] = (
    "abcdefghijklmnopqrstuvwxyz0123456789_"
)
_DETAILS_VALUE_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)
_DETAILS_STRATEGY: Final[st.SearchStrategy[dict[str, str]]] = st.dictionaries(
    keys=st.text(alphabet=_DETAILS_KEY_ALPHABET, min_size=1, max_size=24),
    values=st.text(alphabet=_DETAILS_VALUE_ALPHABET, min_size=0, max_size=64),
    min_size=0,
    max_size=3,
)


# Generated time the projector stamps onto the envelope. Pinned per
# Hypothesis case (one clock instant per ``StatusProjector`` instance)
# so a single per-case generated_at value flows through every operation
# in the sequence; the strategy varies the instant across cases so the
# assertion does not lock in a single literal.
_CLOCK_INSTANT_STRATEGY: Final[st.SearchStrategy[datetime]] = (
    _TEMPORAL_BOUNDARY_STRATEGY
)


@st.composite
def _operation_strategy(draw: Any) -> dict[str, Any]:
    """Draw a single planning operation specification.

    Each operation specification is the input shape
    :func:`wrap_planning_status` accepts: a status name, an applicable
    temporal boundary, the consulted source Resource Identities, the
    consulted source Revision Identities, and an optional details
    payload. The composite shape lets a shrunken counterexample name
    every input dimension on a single dict.
    """
    return {
        "status": draw(_STATUS_STRATEGY),
        "applicable_temporal_boundary": draw(_TEMPORAL_BOUNDARY_STRATEGY),
        "source_resource_ids": draw(
            st.lists(_UUID_STRATEGY, min_size=0, max_size=4)
        ),
        "source_revision_ids": draw(
            st.lists(_UUID_STRATEGY, min_size=0, max_size=4)
        ),
        "details": draw(_DETAILS_STRATEGY),
    }


# Sequence length capped at 6 so the inner loop stays well within the
# 2-second Hypothesis deadline at max_examples=100. Each wrap call is
# pure value-object construction, so the inner work per operation is
# microseconds; the cap exists mainly to keep shrunken counterexamples
# short enough to read.
_OPERATION_SEQUENCE_STRATEGY: Final[
    st.SearchStrategy[list[dict[str, Any]]]
] = st.lists(_operation_strategy(), min_size=1, max_size=6)


# ---------------------------------------------------------------------------
# Helper: build a fresh per-case projector.
# ---------------------------------------------------------------------------


def _build_projector(generated_at: datetime) -> StatusProjector:
    """Construct a per-case :class:`StatusProjector`.

    The projector is built with:

    1. A :class:`FixedClock` pinned to ``generated_at`` so every
       operation in the sequence stamps the same generated time onto
       its envelope. Property 29 is about the *presence* of the
       field, not about any time-of-day variation between operations.
    2. A copy of :func:`planning_projection_registry` so the
       Planning_Service Projection Definition resolves on every
       :func:`wrap_planning_status` call — the missing-definition
       withholding path (Requirement 18.4) is out of scope for
       Property 29.

    A fresh projector per Hypothesis case prevents any in-memory
    registry state from bleeding across shrinks.
    """
    return StatusProjector(
        clock=FixedClock(generated_at),
        definition_registry=planning_projection_registry(),
    )


# ---------------------------------------------------------------------------
# Property 29 — the universal envelope-shape invariant.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 29: Projection envelope wrapper
# Validates: Requirements 18.1, 18.2
@given(
    operations=_OPERATION_SEQUENCE_STRATEGY,
    clock_instant=_CLOCK_INSTANT_STRATEGY,
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_every_status_bearing_response_carries_full_projection_envelope(
    operations: list[dict[str, Any]],
    clock_instant: datetime,
) -> None:
    """For every status-bearing response produced by a generated
    sequence of planning operations, the response carries a
    :class:`ProjectionEnvelope` whose six required fields are populated
    consistently with the inputs the operation supplied.

    Assertions per operation:

    - Requirement 18.1 — the response is a
      :class:`ProjectedStatusResponse` (the projected status is
      included, not withheld) carrying a :class:`ProjectionEnvelope`
      with the Projection Definition, source Resource Identities,
      source Revision Identities, applicable temporal boundary, and
      generated time the projection consulted.
    - Requirement 18.2 — the envelope's derivation indicator is pinned
      to ``"derived"``.
    """
    projector = _build_projector(generated_at=clock_instant)

    for index, op in enumerate(operations):
        response = wrap_planning_status(
            status_projector=projector,
            status=op["status"],
            applicable_temporal_boundary=op["applicable_temporal_boundary"],
            source_resource_ids=op["source_resource_ids"],
            source_revision_ids=op["source_revision_ids"],
            details=op["details"],
        )

        # Per-operation assertion message names the operation index so
        # a shrunken counterexample points at which operation in the
        # sequence violated the invariant.
        assertion_context = (
            f"operation index {index} of {len(operations)}: status="
            f"{op['status']!r}"
        )

        # Requirement 18.1 — the projected status is *included*, not
        # withheld. The withholding paths (Requirement 18.4) are out
        # of scope for Property 29: the projector is constructed with
        # the Planning_Service Projection Definition registered and
        # no missing-source-revision marker is passed.
        assert isinstance(response, ProjectedStatusResponse), (
            f"expected ProjectedStatusResponse on the happy path "
            f"({assertion_context}); got {type(response).__name__}"
        )
        assert response.status == op["status"], assertion_context

        envelope = response.envelope
        assert isinstance(envelope, ProjectionEnvelope), assertion_context

        # Required field 1 — Projection Definition (Requirement 18.1).
        # The envelope's definition must be the Planning_Service
        # Projection Definition resolved from the registry by name —
        # not a freshly synthesized instance. Equality of the value
        # object plus equality on name + version pins both the
        # structural identity and the registered identity.
        assert isinstance(envelope.definition, ProjectionDefinition), (
            assertion_context
        )
        assert envelope.definition == PLANNING_PROJECTION_DEFINITION, (
            assertion_context
        )
        assert envelope.definition.name == (
            PLANNING_PROJECTION_DEFINITION_NAME
        ), assertion_context
        assert envelope.definition.version == (
            PLANNING_PROJECTION_DEFINITION_VERSION
        ), assertion_context

        # Required field 2 — source Resource Identities (Requirement
        # 18.1). The envelope stores the input as a tuple so the
        # value object stays hashable and immutable; the tuple must
        # equal the input list element-for-element.
        assert isinstance(envelope.source_resource_ids, tuple), (
            assertion_context
        )
        assert envelope.source_resource_ids == tuple(
            op["source_resource_ids"]
        ), assertion_context

        # Required field 3 — source Revision Identities (Requirement
        # 18.1). Same shape contract as the Resource Identities.
        assert isinstance(envelope.source_revision_ids, tuple), (
            assertion_context
        )
        assert envelope.source_revision_ids == tuple(
            op["source_revision_ids"]
        ), assertion_context

        # Required field 4 — applicable temporal boundary (Requirement
        # 18.1). Recorded exactly as supplied; the envelope validator
        # rejects non-UTC and sub-second values, so any deviation in
        # the recorded value would have raised during construction
        # rather than passing silently.
        assert envelope.applicable_temporal_boundary == (
            op["applicable_temporal_boundary"]
        ), assertion_context
        assert envelope.applicable_temporal_boundary.tzinfo == timezone.utc, (
            assertion_context
        )
        assert envelope.applicable_temporal_boundary.microsecond == 0, (
            assertion_context
        )

        # Required field 5 — generated time (Requirement 18.1). The
        # projector stamps the clock's current instant; with a
        # :class:`FixedClock` pinned to ``clock_instant`` this is
        # deterministic across every operation in the sequence.
        assert envelope.generated_at == clock_instant, assertion_context
        assert envelope.generated_at.tzinfo == timezone.utc, assertion_context
        assert envelope.generated_at.microsecond == 0, assertion_context

        # Required field 6 — derivation indicator (Requirement 18.2 /
        # Principle 5.23). The envelope pins the indicator to
        # ``"derived"`` via a :class:`Literal` typing; widening this
        # enumeration requires updating the design document and the
        # :mod:`walking_slice.projection` module together. Pin the
        # literal here so any drift is caught immediately.
        assert envelope.derivation == "derived", assertion_context
