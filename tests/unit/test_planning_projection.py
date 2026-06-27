"""Basic unit tests for :mod:`walking_slice.planning._projection` (task 14.1).

Scope: smoke-test the Planning_Service projection-envelope wrapper.

The helper itself is a thin adapter over the existing Slice 1
:class:`walking_slice.projection.StatusProjector`; full coverage of the
wrap / withhold / source-record byte-equivalence behavior lives with
task 14.2 (the dedicated unit-test suite, in a separate file) and with
Property 29 (the cross-cutting property test under task 16.14). These
tests verify only that:

1. The Planning_Service Projection Definition name and version
   constants are present and consistent with the published
   :data:`PLANNING_PROJECTION_DEFINITION` value object.
2. The :data:`PLANNING_PROJECTED_STATUSES` set contains every status
   string named in design.md §"Property 29" and in tasks.md §14.1.
3. :func:`wrap_planning_status` returns a
   :class:`ProjectedStatusResponse` on the happy path with the
   Planning_Service Projection Definition, the supplied source
   identities, the supplied temporal boundary, and the projector
   clock's generated time (truncated to second precision).
4. :func:`wrap_planning_status` returns an
   :class:`ExplanationUnavailableResponse` identifying the missing
   element when the Projection Definition is not registered
   (Requirement 18.4 — unresolvable definition path) and when a
   required source Revision is missing (Requirement 18.4 —
   missing-source-Revision path).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from walking_slice.clock import FixedClock
from walking_slice.planning._projection import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_ORPHANED,
    PLAN_STATUS_SUPERSEDED,
    PLANNING_PROJECTED_STATUSES,
    PLANNING_PROJECTION_DEFINITION,
    PLANNING_PROJECTION_DEFINITION_NAME,
    PLANNING_PROJECTION_DEFINITION_VERSION,
    PROVENANCE_STATUS_INCOMPLETE,
    planning_projection_registry,
    wrap_planning_status,
)
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectedStatusResponse,
    ProjectionEnvelope,
    StatusProjector,
)


pytestmark = pytest.mark.unit


# Boundary chosen at second precision (microsecond=0) so the envelope
# validator on ``applicable_temporal_boundary`` accepts it directly.
_BOUNDARY = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# Clock fixed to a different second so a test can assert the envelope's
# ``generated_at`` is sourced from the clock and not from the boundary.
_CLOCK_INSTANT = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)


def _build_projector(*, with_planning_definition: bool) -> StatusProjector:
    """Return a :class:`StatusProjector` for the test.

    The ``with_planning_definition`` flag toggles whether the
    Planning_Service Projection Definition is pre-registered, which is
    the boundary between the happy path and the unresolvable-definition
    path covered by Requirement 18.4.
    """
    registry = (
        planning_projection_registry() if with_planning_definition else {}
    )
    return StatusProjector(
        clock=FixedClock(_CLOCK_INSTANT),
        definition_registry=registry,
    )


# ---------------------------------------------------------------------------
# Constants and registry.
# ---------------------------------------------------------------------------


class TestPlanningProjectionConstants:
    """Verify the published constants are self-consistent."""

    def test_definition_name_and_version_are_paired(self) -> None:
        assert PLANNING_PROJECTION_DEFINITION.name == (
            PLANNING_PROJECTION_DEFINITION_NAME
        )
        assert PLANNING_PROJECTION_DEFINITION.version == (
            PLANNING_PROJECTION_DEFINITION_VERSION
        )

    def test_known_statuses_are_all_in_the_membership_set(self) -> None:
        # Every status string named in design.md §"Property 29" and
        # in tasks.md §14.1 is published as a module-level constant
        # AND appears in the membership set. A future status added
        # via the module is added in both places at the same time.
        assert PLANNING_PROJECTED_STATUSES == frozenset(
            {
                PLAN_STATUS_APPROVED,
                PLAN_STATUS_DRAFT,
                PLAN_STATUS_SUPERSEDED,
                PLAN_STATUS_ORPHANED,
                PROVENANCE_STATUS_INCOMPLETE,
            }
        )

    def test_status_strings_match_design_wording(self) -> None:
        # Spot-check the exact wording the design document carries so
        # a typo here trips a unit test rather than slipping into the
        # API surface.
        assert PLAN_STATUS_APPROVED == "Plan Approved"
        assert PLAN_STATUS_DRAFT == "Plan Revision draft"
        assert PLAN_STATUS_SUPERSEDED == "Plan Revision superseded"
        assert PLAN_STATUS_ORPHANED == "Plan Revision orphaned"
        assert PROVENANCE_STATUS_INCOMPLETE == "Provenance incomplete"

    def test_registry_returns_fresh_copy(self) -> None:
        first = planning_projection_registry()
        second = planning_projection_registry()
        # Mutating one returned dict must not affect the next call.
        first["other"] = PLANNING_PROJECTION_DEFINITION  # type: ignore[assignment]
        assert PLANNING_PROJECTION_DEFINITION_NAME in second
        assert "other" not in second


# ---------------------------------------------------------------------------
# Happy path — Requirements 18.1, 18.2.
# ---------------------------------------------------------------------------


class TestWrapPlanningStatusHappyPath:
    """Requirements 18.1, 18.2: the wrapped response carries the
    Planning_Service Projection Definition and every envelope field.
    """

    def test_returns_projected_status_response_with_envelope(self) -> None:
        projector = _build_projector(with_planning_definition=True)

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.status == PLAN_STATUS_APPROVED
        # Requirement 18.1 — envelope is present and carries the
        # Planning_Service Projection Definition.
        assert isinstance(response.envelope, ProjectionEnvelope)
        assert response.envelope.definition == PLANNING_PROJECTION_DEFINITION
        assert response.envelope.applicable_temporal_boundary == _BOUNDARY
        # Generated time comes from the projector's clock truncated to
        # second precision.
        assert response.envelope.generated_at == _CLOCK_INSTANT
        # Requirement 18.2 — derivation indicator is pinned to derived.
        assert response.envelope.derivation == "derived"

    def test_propagates_source_ids_and_details(self) -> None:
        projector = _build_projector(with_planning_definition=True)
        plan_revision_id = uuid.uuid4()
        plan_approval_id = uuid.uuid4()

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[plan_revision_id],
            source_revision_ids=[plan_approval_id],
            details={"plan_approval_id": str(plan_approval_id)},
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.envelope.source_resource_ids == (plan_revision_id,)
        assert response.envelope.source_revision_ids == (plan_approval_id,)
        assert response.details == {"plan_approval_id": str(plan_approval_id)}

    def test_default_details_is_empty_dict(self) -> None:
        projector = _build_projector(with_planning_definition=True)

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.details == {}


# ---------------------------------------------------------------------------
# Withholding paths — Requirement 18.4.
# ---------------------------------------------------------------------------


class TestWrapPlanningStatusExplanationUnavailable:
    """Requirement 18.4: when the Projection Definition is unregistered
    or a required source Revision is missing, the helper withholds the
    projected status and returns an explanation-unavailable indicator.
    """

    def test_unregistered_definition_yields_explanation_unavailable(
        self,
    ) -> None:
        # Registry intentionally empty — the Planning_Service
        # Projection Definition is not registered, simulating a
        # mis-configured deployment.
        projector = _build_projector(with_planning_definition=False)

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "projection_definition"
        assert response.missing_element_identifier == (
            PLANNING_PROJECTION_DEFINITION_NAME
        )

    def test_missing_source_revision_yields_explanation_unavailable(
        self,
    ) -> None:
        # Definition IS registered, so the missing-source-Revision
        # path must take precedence and name the precise missing
        # element rather than reporting a definition gap.
        projector = _build_projector(with_planning_definition=True)
        missing_revision_id = uuid.uuid4()

        response = wrap_planning_status(
            status_projector=projector,
            status=PROVENANCE_STATUS_INCOMPLETE,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=missing_revision_id,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "source_revision"
        assert response.missing_element_identifier == str(missing_revision_id)

    def test_missing_revision_takes_precedence_over_missing_definition(
        self,
    ) -> None:
        # Both gaps exist: the projector has no registered definition
        # AND the producer supplied a missing source Revision. The
        # more-precise missing-element wins per the helper's documented
        # precedence (mirrors :meth:`StatusProjector.project_status`).
        projector = _build_projector(with_planning_definition=False)
        missing_revision_id = uuid.uuid4()

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=missing_revision_id,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "source_revision"
        assert response.missing_element_identifier == str(missing_revision_id)
