"""Planning_Service projection-envelope wrapper (task 14.1).

Design references
=================

- ``.kiro/specs/second-walking-slice/design.md`` §"Reused Slice 1
  components": *Projection envelope — unchanged. Planning_Service status
  responses (e.g. "Plan Approved") wrap their projected status in
  ``ProjectionEnvelope`` per Requirement 18.*
- ``.kiro/specs/second-walking-slice/design.md`` §"Property 29 —
  Projection envelope wrapper": every status-bearing planning response
  carries a ``ProjectionEnvelope`` with Projection Definition, source
  Resource Identities, source Revision Identities, applicable temporal
  boundary in ISO-8601 second precision, generated time in ISO-8601
  second precision, and a derivation indicator. The example statuses the
  property names are *"Plan Revision draft"*, *"Plan Approved"*, *"Plan
  Revision superseded"*, *"Provenance incomplete"*, and *"Plan Revision
  orphaned"*.
- Slice 1: ``.kiro/specs/first-walking-slice/design.md`` Requirement 14
  (Explainable Projection of Slice Status) and the existing
  :class:`walking_slice.projection.StatusProjector` and
  :class:`walking_slice.projection.ProjectionEnvelope` value objects.
- Slice 1 reference implementation: :mod:`walking_slice.trails` and
  :meth:`walking_slice.trails.TrailService.create_trail_projected` —
  the canonical "wrap a derived status in an envelope" pattern this
  module reproduces verbatim for Planning_Service responses.

Task scope (task 14.1)
======================

Most Slice 2 service methods today return concrete result dataclasses
(``CreateObjectiveResult``, ``CreatePlanApprovalResult``, etc.) that
carry persisted identifiers but not a *derived* status name; the
``Plan_Revisions.lifecycle_state`` column already records the
*authoritative* lifecycle value, and Provenance completeness is
established by :meth:`walking_slice.provenance.Provenance_Navigator.navigate_plan_approval`.
Even so, Requirement 18 mandates that **every** response surfacing a
*derived* status (the status was computed by walking source Records
rather than read out of a single authoritative column) MUST come back
wrapped in a :class:`ProjectionEnvelope`. This module provides the
single Slice 2 surface for that wrapping so:

1. There is one Projection Definition name the Planning_Service
   registers with the
   :class:`walking_slice.projection.StatusProjector` (parallel to
   :data:`walking_slice.trails.TRAIL_PROJECTION_DEFINITION_NAME`) and
   one Projection Definition version literal. Routes and service
   methods reference the constant rather than spelling the name at
   each call site so a typo trips the explanation-unavailable path
   on the registration side instead of silently mislabeling an
   envelope.
2. The known status string constants this slice surfaces
   (``"Plan Approved"``, ``"Plan Revision draft"``,
   ``"Plan Revision superseded"``, ``"Provenance incomplete"``,
   ``"Plan Revision orphaned"``) are declared once. Producers
   import them; tests assert against them. Adding a new status is
   an explicit edit here, not a string-literal change at a call
   site.
3. A single helper :func:`wrap_planning_status` builds the wrapped
   response. It accepts the Projection Definition name (defaulting
   to the Planning_Service one declared here), the projected
   status value, the source Resource and Revision Identities the
   projection consulted, the applicable temporal boundary, and
   optional details / missing-source markers. On the happy path it
   returns a :class:`ProjectedStatusResponse` wrapping a fully
   populated :class:`ProjectionEnvelope`; on the unresolvable-
   Projection-Definition or missing-source-Revision paths it
   withholds the projected status and returns an
   :class:`ExplanationUnavailableResponse` identifying the missing
   element — matching the Slice 1 task 14.2 pattern named in the
   task brief.

The helper does not persist anything (Principle 5.23 — Projections are
derived). It is safe to call from inside a domain transaction or after
it has committed; the transactional contract belongs to the
Planning_Service modules that originate the status, not to the
projection wrapper.

Requirements satisfied (per task 14.1)
======================================

- 18.1 — Every status-bearing planning response carries its Projection
         Definition, source Resource Identities, source Revision
         Identities, applicable temporal boundary, and generated time
         in the envelope this helper builds.
- 18.2 — The envelope's ``derivation`` indicator is pinned to
         ``"derived"`` by :class:`ProjectionEnvelope`; this module
         never overrides it.
- 18.3 — The helper does not mutate source Records; the wrapped
         response is a pure value-object. Producers calling the
         helper after appending corrections leave the prior Records
         byte-equivalent.
- 18.4 — The helper returns an :class:`ExplanationUnavailableResponse`
         identifying the missing element when the Projection
         Definition is unregistered or a required source Revision is
         missing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Final, Optional
from uuid import UUID

from walking_slice.projection import (
    ProjectionDefinition,
    StatusBearingResponse,
    StatusProjector,
)


__all__ = [
    "PLANNING_PROJECTION_DEFINITION_NAME",
    "PLANNING_PROJECTION_DEFINITION_VERSION",
    "PLANNING_PROJECTION_DEFINITION",
    "PLAN_STATUS_APPROVED",
    "PLAN_STATUS_DRAFT",
    "PLAN_STATUS_SUPERSEDED",
    "PLAN_STATUS_ORPHANED",
    "PROVENANCE_STATUS_INCOMPLETE",
    "PLANNING_PROJECTED_STATUSES",
    "wrap_planning_status",
    "planning_projection_registry",
]


# ---------------------------------------------------------------------------
# Projection Definition name and version.
#
# Centralized so the producer (a Planning_Service method or the
# Planning_Service HTTP layer in task 15.x) and the
# :class:`StatusProjector` registry the composition wires at startup
# match on the same string. A typo at either site trips the
# explanation-unavailable path (Requirement 18.4) instead of mislabeling
# the envelope.
# ---------------------------------------------------------------------------


PLANNING_PROJECTION_DEFINITION_NAME: Final[str] = "planning.status"
"""Single Projection Definition name for every Slice 2 planning status.

Parallel to :data:`walking_slice.trails.TRAIL_PROJECTION_DEFINITION_NAME`.
The name names the *computation* — "the Planning_Service derives this
status name from source Plan Revision and Plan Approval Records" — not
any specific status string. The status string travels on
:attr:`ProjectedStatusResponse.status`.
"""


PLANNING_PROJECTION_DEFINITION_VERSION: Final[str] = "2026.01"
"""Version of the Planning_Service projection.

Follows the same year-month string convention the slice's other
Projection Definitions use (cf. ``_TRAIL_DEFINITION`` in
``tests/unit/test_projection_status_wrapping.py``). A breaking change
to the computation (a status string is renamed, a new source is
introduced) bumps this literal.
"""


PLANNING_PROJECTION_DEFINITION: Final[ProjectionDefinition] = (
    ProjectionDefinition(
        name=PLANNING_PROJECTION_DEFINITION_NAME,
        version=PLANNING_PROJECTION_DEFINITION_VERSION,
    )
)
"""Convenience instance for the Planning_Service Projection Definition.

Composition (task 15.2) registers this instance with the singleton
:class:`StatusProjector` so producer call sites can pass the bare
name and the projector resolves the full
:class:`ProjectionDefinition` from its registry.
"""


# ---------------------------------------------------------------------------
# Status string constants.
#
# Sourced verbatim from design.md §"Property 29 — Projection envelope
# wrapper" and tasks.md §14.1: *"Plan Approved", "Plan Revision draft",
# "Plan Revision superseded", "Provenance incomplete", "Plan Revision
# orphaned"*. The exact wording (capitalization, spacing) is preserved
# so the test surface and the design document refer to the same string.
#
# Status names are intentionally human-readable rather than dot-prefixed
# enum codes — the design names them in prose and Requirement 18.1
# treats them as the surfaced "projected status" value. A producer that
# prefers a machine-readable code surfaces it under a separate key
# inside :attr:`ProjectedStatusResponse.details`.
# ---------------------------------------------------------------------------


PLAN_STATUS_APPROVED: Final[str] = "Plan Approved"
"""Derived status surfaced once a Plan Revision has an associated Plan
Approval Record (Requirement 9). Production-time source: a single SELECT
joining ``Plan_Revisions`` and ``Plan_Approval_Records``."""


PLAN_STATUS_DRAFT: Final[str] = "Plan Revision draft"
"""Derived status surfaced for a Plan Revision whose
``lifecycle_state = 'draft'`` and which is not the target of any Plan
Approval Record. Production-time source: a single SELECT against
``Plan_Revisions``."""


PLAN_STATUS_SUPERSEDED: Final[str] = "Plan Revision superseded"
"""Derived status surfaced for a Plan Revision that is the *target* of
a ``Supersedes`` Relationship from a later Plan Revision of the same
Activity Plan (Requirement 7.4 / domain-model §10.6)."""


PLAN_STATUS_ORPHANED: Final[str] = "Plan Revision orphaned"
"""Derived status surfaced when a Plan Revision's source Activity Plan,
Project, or Objective has been withdrawn or is otherwise unresolvable
from the requesting Party's vantage (design §"Property 29" enumerates
the status name)."""


PROVENANCE_STATUS_INCOMPLETE: Final[str] = "Provenance incomplete"
"""Derived status surfaced by the Planning Provenance Chain walk
(Requirement 14 / task 12.1) when one or more hops are not authorized
to the requesting Party — the projection that the chain is *complete*
cannot be made. Production-time source: a
:meth:`Provenance_Navigator.navigate_plan_approval` walk that returned
gaps."""


PLANNING_PROJECTED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        PLAN_STATUS_APPROVED,
        PLAN_STATUS_DRAFT,
        PLAN_STATUS_SUPERSEDED,
        PLAN_STATUS_ORPHANED,
        PROVENANCE_STATUS_INCOMPLETE,
    }
)
"""Set of every known planning projected status string.

The helper :func:`wrap_planning_status` does NOT enforce membership —
producers may surface a status not listed here (for example, a future
status added by a later slice) as long as it conforms to
:class:`ProjectionEnvelope`'s 1..256-character constraint. The
membership set exists so tests and future producers can iterate over
the known statuses without re-spelling the string literals.
"""


# ---------------------------------------------------------------------------
# Helper: wrap a derived planning status in a :class:`ProjectionEnvelope`.
# ---------------------------------------------------------------------------


def wrap_planning_status(
    *,
    status_projector: StatusProjector,
    status: str,
    applicable_temporal_boundary: datetime,
    source_resource_ids: Iterable[UUID] = (),
    source_revision_ids: Iterable[UUID] = (),
    details: Optional[Mapping[str, Any]] = None,
    missing_source_revision_id: Optional[UUID] = None,
    definition_name: str = PLANNING_PROJECTION_DEFINITION_NAME,
) -> StatusBearingResponse:
    """Return a Planning_Service projected-status response.

    Per Requirement 18.1 the returned envelope carries the Projection
    Definition (resolved from *definition_name* against the projector's
    registry), the source Resource Identities, the source Revision
    Identities, the applicable temporal boundary, and the generated
    time stamped from the projector's :class:`Clock`. Per Requirement
    18.2 the envelope's ``derivation`` indicator is fixed at
    ``"derived"`` by :class:`ProjectionEnvelope`.

    Per Requirement 18.4 the helper returns an
    :class:`ExplanationUnavailableResponse` identifying the missing
    element when:

    1. *missing_source_revision_id* is supplied — the producer
       detected that a required source Revision could not be located.
       Takes precedence over the definition lookup so the response
       names the most precise missing element.
    2. *definition_name* does not name a registered Projection
       Definition in the projector's registry.

    On both withholding paths no row is INSERTed or UPDATEd by this
    helper (Requirement 18.3 — source Records remain byte-equivalent).

    Args:
        status_projector: The Slice 1
            :class:`walking_slice.projection.StatusProjector` to use.
            Production composition (task 15.2) constructs a single
            projector registered with :data:`PLANNING_PROJECTION_DEFINITION`
            (and Slice 1's Trail definition) and shares it across
            requests; tests construct ad-hoc projectors. Keyword-only
            to keep call sites explicit.
        status: Projected planning status name (for example
            :data:`PLAN_STATUS_APPROVED`). 1..256 characters; the
            :class:`ProjectionEnvelope` validator rejects anything
            outside that range. Membership in
            :data:`PLANNING_PROJECTED_STATUSES` is **not** enforced —
            see the docstring on that constant.
        applicable_temporal_boundary: UTC datetime at second precision
            identifying the moment up to which the projection's
            sources were considered effective.
        source_resource_ids: Resource Identities consulted by the
            projection. Defaults to an empty tuple.
        source_revision_ids: Resource Revision Identities consulted
            by the projection (for example, the Plan Revision and
            Plan Approval Record identifiers for the "Plan Approved"
            projection). Defaults to an empty tuple.
        details: Optional producer-specific structured payload (for
            example, the Plan Approval Record identifier the
            "Plan Approved" status was derived from). ``None`` becomes
            an empty ``dict`` on the wrapped response.
        missing_source_revision_id: When the producer detected that
            a required source Revision could not be resolved, the
            Revision Identity it expected to find. Triggers the
            withholding path per Requirement 18.4.
        definition_name: The Projection Definition name to resolve
            against the projector's registry. Defaults to
            :data:`PLANNING_PROJECTION_DEFINITION_NAME` so the
            common call site is one keyword argument shorter; a
            future planning sub-projection that needs a distinct
            name can override the default without touching the
            projector wiring.

    Returns:
        A :class:`ProjectedStatusResponse` on the happy path, or an
        :class:`ExplanationUnavailableResponse` identifying the
        missing element on either of the two withholding paths.

    Raises:
        pydantic.ValidationError: *status* fails the
            :class:`ProjectionEnvelope` envelope constraints (empty,
            too long, or built from a non-UTC / sub-second
            ``applicable_temporal_boundary``).
    """
    return status_projector.project_status(
        definition_name=definition_name,
        status=status,
        source_resource_ids=source_resource_ids,
        source_revision_ids=source_revision_ids,
        applicable_temporal_boundary=applicable_temporal_boundary,
        details=details,
        missing_source_revision_id=missing_source_revision_id,
    )


def planning_projection_registry() -> dict[str, ProjectionDefinition]:
    """Return the Planning_Service Projection Definition registry.

    Production composition (task 15.2) merges this dict with the
    Slice 1 Trail registry and hands the union to a single
    :class:`StatusProjector` instance. Returning a fresh ``dict`` on
    every call lets callers mutate the result without affecting other
    callers (the :class:`StatusProjector` copies its registry on
    construction).

    The function is small enough to inline at the composition site;
    it lives here so the *single* place that names the Slice 2
    Projection Definition is :data:`PLANNING_PROJECTION_DEFINITION`
    above.
    """
    return {
        PLANNING_PROJECTION_DEFINITION_NAME: PLANNING_PROJECTION_DEFINITION,
    }
