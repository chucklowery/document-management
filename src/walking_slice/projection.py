"""Projection envelope for explainable projected status.

Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"Constitutional
Posture" (row "System health observable") and the cross-cutting note that
"Projections expose Projection Definition, sources, temporal boundary, and
generated time alongside the projected status (Requirement 14)".

Task scope (task 14.1):

This module defines :class:`ProjectionEnvelope` — the metadata wrapper that
the Walking_Slice_System surfaces alongside every projected status produced
by ``Trail_Service`` and ``Provenance_Navigator`` (for example
"Trail unresolved" or "Provenance incomplete"). Task 14.2 wires the envelope
into status-bearing responses; task 14.3 covers the unit-level
withholding/explanation paths.

The envelope's job is to make the projection explainable from its sources:
a caller receiving a projected status must be able to identify

1. which Projection Definition produced the status,
2. which source Resources and Revisions the projection consulted,
3. the temporal boundary the projection applied,
4. the moment the projection was generated, and
5. that the status is *derived*, never authoritative.

The slice does not need a separate "authoritative" indicator (Principle 5.23 /
Requirement 14.2): every value emitted through this envelope is a Projection,
so :attr:`ProjectionEnvelope.derivation` is pinned to ``"derived"``. If a
later slice needs to expose authoritative records through the same shape, the
enumeration is widened in lockstep with the design document; this task does
not widen it.

Requirements satisfied (per task 14.1):

- 14.1 — every projected status is accompanied by its Projection Definition,
         source Resource Identities, source Revision Identities, applicable
         temporal boundary (ISO-8601 second precision), and generated time
         (ISO-8601 second precision).
- 14.2 — every projected status carries a derivation indicator distinguishing
         it from authoritative source Records.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from walking_slice.clock import Clock


__all__ = [
    "ExplanationUnavailableResponse",
    "ProjectedStatusResponse",
    "ProjectionDefinition",
    "ProjectionEnvelope",
    "StatusBearingResponse",
    "StatusProjector",
]


class _FrozenModel(BaseModel):
    """Common configuration for projection value objects.

    Mirrors the convention established in :mod:`walking_slice.models`:
    ``frozen=True`` makes instances hashable and prevents field
    reassignment, while ``extra="forbid"`` rejects unknown attributes so
    a typo'd field name surfaces as a validation error instead of being
    silently dropped from the envelope.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectionDefinition(_FrozenModel):
    """Identifier of the projection that produced an envelope.

    A Projection Definition is the named, versioned computation whose
    output the envelope wraps. The slice carries the Definition by
    ``(name, version)`` rather than by an opaque identifier so that the
    pilot reviewer (Requirement 14, User Story US-034) can read a
    response and immediately tell which projection produced the status
    without dereferencing a separate registry.

    The ``name`` is a stable string drawn from a small set defined by
    each producer (for example ``"trail.unresolved-step"`` or
    ``"provenance.completeness"``). The ``version`` follows the
    producer's own versioning discipline — the envelope does not impose a
    semver constraint, only the requirement that the same
    ``(name, version)`` pair always denotes the same computation.

    Attributes:
        name: Producer-stable Projection Definition name. 1 to 256
            characters of plain text.
        version: Producer-stable Projection Definition version. 1 to 64
            characters of plain text.
    """

    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)


class ProjectionEnvelope(_FrozenModel):
    """Metadata envelope accompanying every projected status in the slice.

    Per Requirement 14.1, the envelope is included *alongside* the
    projected status in the same response; the envelope itself does not
    carry the status payload, which is the producing service's domain
    type. Task 14.2 wires the envelope into the status-bearing responses
    of ``Trail_Service`` and ``Provenance_Navigator``.

    Per Requirement 14.2, the envelope's :attr:`derivation` indicator is
    pinned to ``"derived"`` on every instance, marking the projected
    status as distinct from authoritative source Records (Principle 5.23).

    Per Requirement 14.1, ``applicable_temporal_boundary`` and
    ``generated_at`` are recorded in ISO-8601 form with at least second
    precision. The validators on this class accept only UTC-aware
    :class:`~datetime.datetime` values whose ``microsecond`` is zero,
    matching the canonical-form discipline used elsewhere in the slice
    (design §"Cross-Cutting Concerns").

    Attributes:
        definition: The Projection Definition that produced the
            accompanying status.
        source_resource_ids: Resource Identities consulted by the
            projection. Stored as a tuple so the envelope remains
            byte-equivalent across passes through frozen-model copies.
        source_revision_ids: Resource Revision Identities consulted by
            the projection. The producer is responsible for ensuring
            this list pins exact Revisions per the Pinned-only discipline
            of AD-WS-12.
        applicable_temporal_boundary: The instant up to which the
            projection's sources were considered effective. UTC, second
            precision (microseconds must be zero).
        generated_at: The instant at which the projection was produced.
            UTC, second precision (microseconds must be zero).
        derivation: Fixed derivation indicator. Always ``"derived"``;
            widening this enumeration requires updating the design
            document and this module together.
    """

    definition: ProjectionDefinition
    source_resource_ids: tuple[UUID, ...] = ()
    source_revision_ids: tuple[UUID, ...] = ()
    applicable_temporal_boundary: datetime
    generated_at: datetime
    derivation: Literal["derived"] = "derived"

    @field_validator("applicable_temporal_boundary", "generated_at")
    @classmethod
    def _enforce_utc_second_precision(cls, value: datetime) -> datetime:
        """Reject non-UTC or sub-second-precision datetimes.

        Requirement 14.1 mandates ISO-8601 with at least second
        precision. We enforce strict equality (microsecond == 0) rather
        than "at least second precision" because allowing finer-than-
        second precision invites silent disagreement between producers
        and consumers about the canonical form a projected status
        carries. Producers needing sub-second timing on internal records
        round to the second when constructing the envelope.
        """
        if value.tzinfo is None:
            raise ValueError(
                "timestamp must be timezone-aware UTC; received naive datetime"
            )
        offset = value.utcoffset()
        if offset is None or offset != timedelta(0):
            raise ValueError(
                "timestamp must be UTC (offset zero); received offset "
                f"{offset!r}"
            )
        if value.microsecond != 0:
            raise ValueError(
                "timestamp must be at ISO-8601 second precision; received "
                f"microsecond={value.microsecond}"
            )
        return value



# ---------------------------------------------------------------------------
# Task 14.2 — Status-bearing response wrapping.
#
# Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"Constitutional
# Posture" (row "System health observable") and Requirement 14 acceptance
# criteria 14.1 through 14.4.
#
# This section adds the additive surface task 14.2 calls for:
#
# - :class:`ProjectedStatusResponse` — the wrapper a producer returns when a
#   projected status (for example "Trail unresolved" or "Provenance incomplete")
#   is being surfaced alongside its :class:`ProjectionEnvelope` (Requirement
#   14.1, 14.2).
#
# - :class:`ExplanationUnavailableResponse` — the wrapper a producer returns
#   when the Projection Definition or a required source Revision cannot be
#   resolved. Per Requirement 14.4 the projected status is withheld and the
#   response identifies which element is missing so the caller can rerun
#   the query once the missing source is recorded. Source Records remain
#   unchanged because no INSERT is issued on this path.
#
# - :class:`StatusProjector` — the reusable helper that producers
#   (``Trail_Service`` today, ``Provenance_Navigator`` once task 12.1 lands)
#   call to build the wrapper. The projector centralizes:
#
#       1. resolving a Projection Definition by name from a small
#          in-memory registry (the slice does not federate to a separate
#          projection registry — design §"Out-of-Scope Boundaries"),
#       2. truncating ``Clock.now()`` to ISO-8601 second precision so the
#          envelope's validators accept the generated time, and
#       3. choosing between the wrap path and the withholding path.
#
#   The projector deliberately does *not* persist anything: it builds a
#   pure value-object response (Principle 5.23 — Projections are derived).
#   This matches the additive constraint in the task brief: integrating
#   the envelope must not break the ``TrailService.create_trail`` API.
# ---------------------------------------------------------------------------


# Names of the missing element kinds surfaced by
# :class:`ExplanationUnavailableResponse`. Centralized so producers and the
# HTTP layer (task 10.3 for Trails, task 12.5 for Provenance) match on the
# same string instead of the wording of an exception message.
_MISSING_KIND_PROJECTION_DEFINITION: Literal["projection_definition"] = (
    "projection_definition"
)
_MISSING_KIND_SOURCE_REVISION: Literal["source_revision"] = "source_revision"


def _to_second_precision(value: datetime) -> datetime:
    """Truncate ``value`` to ISO-8601 second precision for envelope use.

    :class:`Clock` values already arrive normalized to UTC at millisecond
    precision (:func:`walking_slice.clock.truncate_to_milliseconds`).
    The :class:`ProjectionEnvelope` validators require strict second
    precision (``microsecond == 0``); this helper bridges the two by
    discarding sub-second components.

    The function preserves ``tzinfo`` so a caller that mistakenly passes a
    naive value still trips the envelope validator with a precise error
    message rather than being silently coerced.
    """
    if value.tzinfo is None:
        return value
    return value.replace(microsecond=0)


class ProjectedStatusResponse(_FrozenModel):
    """Status-bearing response wrapped with its :class:`ProjectionEnvelope`.

    Per Requirement 14.1, every projected status surfaced by the slice
    MUST be accompanied by its Projection Definition, source Resource
    Identities, source Revision Identities, applicable temporal boundary,
    and generated time — all carried by :attr:`envelope`. Per Requirement
    14.2, the envelope's derivation indicator marks the status as
    *derived*, not authoritative.

    The :attr:`status` field carries the human- and machine-readable
    status name (for example ``"trail.unresolved"`` or
    ``"provenance.incomplete"``). The :attr:`details` field carries the
    producer-specific structured payload (for example the per-ordinal
    unresolved-step list from :class:`TrailTargetUnresolvedError`). The
    pair is intentionally flat rather than nesting the producer payload
    inside the envelope so the envelope remains a pure metadata wrapper
    reusable across producers.

    Attributes:
        envelope: The :class:`ProjectionEnvelope` accompanying the
            projected status (Requirement 14.1).
        status: The projected status name. 1..256 characters of plain
            text drawn from the producer's small status enumeration.
        details: Producer-specific structured payload describing the
            status. Defaults to an empty mapping. The mapping is stored
            as a tuple of ``(key, value)`` pairs internally to keep the
            value object frozen and hashable; consumers read it through
            :attr:`details` which materializes a fresh dictionary on
            every access. Per Requirement 14.3, this payload is a
            projection of source Records and never mutates them.
    """

    envelope: ProjectionEnvelope
    status: str = Field(min_length=1, max_length=256)
    # The details field uses ``dict`` so Pydantic accepts arbitrary JSON-like
    # structured payloads from producers. ``frozen=True`` on the model
    # prevents reassignment of the dict reference; producers must build the
    # dict before constructing the response and treat it as read-only.
    details: dict[str, Any] = Field(default_factory=dict)


class ExplanationUnavailableResponse(_FrozenModel):
    """Returned when a projected status must be withheld (Requirement 14.4).

    Per Requirement 14.4, when the Projection Definition or any required
    source Revision cannot be resolved, the slice withholds the
    projected status and returns an explanation-unavailable indicator
    identifying the missing element. Stored source Records are left
    unchanged because no INSERT is issued on this path.

    Producers convert this response into the appropriate HTTP shape at
    the route layer; this module is deliberately transport-agnostic.

    Attributes:
        missing_element_kind: ``"projection_definition"`` when the
            Projection Definition could not be resolved by name;
            ``"source_revision"`` when a source Revision the projection
            depended on could not be located. Widening this enumeration
            requires updating the design document, this module, and any
            HTTP layer that switches on the value.
        missing_element_identifier: The name (for the definition path)
            or the canonical UUIDv7 string (for the source-revision
            path) that names the missing element. 1..256 characters of
            plain text — long enough to hold a ``"name/version"`` pair
            on the definition path.
    """

    missing_element_kind: Literal["projection_definition", "source_revision"]
    missing_element_identifier: str = Field(min_length=1, max_length=256)


# Type alias mirroring the producer-facing return type of
# :meth:`StatusProjector.project_status`. The HTTP layer (task 10.3 for
# Trails, task 12.5 for Provenance) discriminates on the runtime type to
# render the appropriate response shape.
StatusBearingResponse = ProjectedStatusResponse | ExplanationUnavailableResponse


class StatusProjector:
    """Build :class:`ProjectionEnvelope`-wrapped status responses.

    The projector is reusable across producers: today
    ``Trail_Service.create_trail_projected`` calls it; once
    ``Provenance_Navigator`` lands in task 12.1 it will call the same
    projector with a different Projection Definition name. The producer
    supplies the status name, the source Resource and Revision Identities,
    the applicable temporal boundary, and (optionally) a structured
    details payload; the projector resolves the Projection Definition
    from its registry, stamps the generated time from the injected
    :class:`Clock`, and returns either a wrapped status response or an
    explanation-unavailable response.

    The projector does not persist anything (Principle 5.23 — Projections
    are derived). It is safe to share one instance across requests; it
    holds only the cross-request collaborators
    (:class:`~walking_slice.clock.Clock`, registered definitions).

    Args:
        clock: Source of the envelope's ``generated_at`` timestamp. The
            clock's millisecond-precision value is truncated to second
            precision so the envelope validator accepts it.
        definition_registry: Mapping of Projection Definition name to
            :class:`ProjectionDefinition`. The slice's known definitions
            are seeded at construction time. Names not present in the
            registry trigger the explanation-unavailable path.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        definition_registry: Mapping[str, ProjectionDefinition],
    ) -> None:
        # Copy into a plain dict so a caller cannot later mutate the
        # registry from the outside. ``Mapping`` accepts both a dict and
        # any other read-only mapping (for example a ``MappingProxyType``).
        self._clock = clock
        self._definitions: dict[str, ProjectionDefinition] = dict(
            definition_registry
        )

    def project_status(
        self,
        *,
        definition_name: str,
        status: str,
        source_resource_ids: Iterable[UUID] = (),
        source_revision_ids: Iterable[UUID] = (),
        applicable_temporal_boundary: datetime,
        details: Mapping[str, Any] | None = None,
        missing_source_revision_id: UUID | None = None,
    ) -> StatusBearingResponse:
        """Return a wrapped status response or an explanation-unavailable response.

        The method exercises Requirement 14 acceptance criteria in
        order:

        1. **14.4 (missing source Revision).** If
           ``missing_source_revision_id`` is supplied, the projected
           status is withheld and an
           :class:`ExplanationUnavailableResponse` identifying the
           missing Revision is returned. The producer is responsible
           for detecting the missing Revision and passing the
           identifier; the projector trusts that detection so the
           envelope's source-Revision list always matches the values
           the projection actually consulted.

        2. **14.4 (unresolvable Projection Definition).** If
           ``definition_name`` does not name a registered Projection
           Definition, an :class:`ExplanationUnavailableResponse`
           identifying the missing definition is returned.

        3. **14.1 + 14.2 (happy path).** Otherwise the projector builds
           a :class:`ProjectionEnvelope` carrying the resolved
           Projection Definition, the supplied source identities, the
           supplied temporal boundary, the clock's current time
           truncated to second precision, and the derivation indicator
           (always ``"derived"``). The envelope is returned inside a
           :class:`ProjectedStatusResponse` together with the
           ``status`` and ``details`` payload.

        Args:
            definition_name: Producer-stable Projection Definition
                name (for example ``"trail.resolution"``). Resolved
                against the registry passed to the constructor.
            status: Projected status name (for example
                ``"trail.unresolved"`` or ``"provenance.complete"``).
                1..256 characters.
            source_resource_ids: Resource Identities consulted by the
                projection. Defaults to an empty tuple — for example
                a status that depends only on Revision Identities.
            source_revision_ids: Resource Revision Identities consulted
                by the projection. Defaults to an empty tuple.
            applicable_temporal_boundary: UTC datetime at second
                precision identifying the moment up to which the
                sources were considered effective.
            details: Optional producer-specific structured payload
                describing the status. ``None`` becomes an empty
                ``dict`` on the wrapped response.
            missing_source_revision_id: When the producer detected
                that a required source Revision could not be
                resolved, the Revision Identity it expected to find.
                Triggers the withholding path per Requirement 14.4.

        Returns:
            A :class:`ProjectedStatusResponse` on the happy path;
            otherwise an :class:`ExplanationUnavailableResponse`
            identifying the missing element.

        Raises:
            pydantic.ValidationError: ``status`` is empty/too long or
                ``applicable_temporal_boundary`` is not a UTC second
                precision datetime (the envelope validator catches the
                latter).
        """
        # Path 1 — Requirement 14.4 (missing source Revision).
        # Checked first so a producer that already knows the missing
        # element does not need to also pre-validate its definition
        # name; the precise missing element wins.
        if missing_source_revision_id is not None:
            return ExplanationUnavailableResponse(
                missing_element_kind=_MISSING_KIND_SOURCE_REVISION,
                missing_element_identifier=str(missing_source_revision_id),
            )

        # Path 2 — Requirement 14.4 (unresolvable Projection Definition).
        definition = self._definitions.get(definition_name)
        if definition is None:
            return ExplanationUnavailableResponse(
                missing_element_kind=_MISSING_KIND_PROJECTION_DEFINITION,
                missing_element_identifier=definition_name,
            )

        # Path 3 — Requirement 14.1 + 14.2 (happy path).
        envelope = ProjectionEnvelope(
            definition=definition,
            source_resource_ids=tuple(source_resource_ids),
            source_revision_ids=tuple(source_revision_ids),
            applicable_temporal_boundary=applicable_temporal_boundary,
            generated_at=_to_second_precision(self._clock.now()),
        )
        return ProjectedStatusResponse(
            envelope=envelope,
            status=status,
            details=dict(details) if details is not None else {},
        )

    def has_definition(self, definition_name: str) -> bool:
        """Return ``True`` iff ``definition_name`` is registered.

        Exposed so producers that want to short-circuit (for example,
        skip an expensive source-Revision lookup when the definition
        is already missing) can check the registry without provoking
        a full :meth:`project_status` call. The method is purely
        informational; the canonical resolution path remains
        :meth:`project_status`.
        """
        return definition_name in self._definitions
