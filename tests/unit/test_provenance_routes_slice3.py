"""Unit tests for the Slice 3 additions to :mod:`walking_slice.routes.provenance`
(task 15.2).

Task 15.2 extends the existing provenance routes module additively
with three new endpoints that expose the Execution Provenance Chain
traversals from :class:`ProvenanceNavigator`:

- ``GET /api/v1/completions/{completion_id}/provenance``
- ``GET /api/v1/deliverable-productions/{deliverable_production_id}/provenance``
- ``GET /api/v1/deliverables/{deliverable_id}/revisions/``
  ``{deliverable_revision_id}/provenance``

The handlers themselves cannot be exercised end-to-end until task 15.3
wires Slice 3 into the FastAPI app; the Slice 3 end-to-end HTTP suite
(task 17) drives the wired routes.

These unit tests cover the surfaces this extension creates *on its own*:

1. The router still exposes every Slice 1 / Slice 2 endpoint it had
   before task 15.2 — additivity is enforced, not just hoped for
   (Requirement 40.1 — Reuse and Non-Modification of Slice 1 and
   Slice 2 Contexts).
2. The router additionally mounts every Slice 3 endpoint at the
   canonical path the design names.
3. The Slice 3 :class:`RequestContext` placeholder
   :func:`get_request_context` raises :class:`NotImplementedError`
   when invoked unwrapped — the convention every Slice 1 / 2 route
   module shares so an unwired call fails loudly rather than silently
   returning ``None``.
4. The new response body classes enforce ``extra='forbid'`` — a
   regression that adds a sensitive field to the denial-or-redaction
   surface would surface here rather than silently shipping a
   Requirement 30.7 / 38.4 leak.
"""

from __future__ import annotations

import pytest

from walking_slice.routes import provenance as provenance_routes
from walking_slice.routes.provenance import (
    CompletionNodeBody,
    CompletionProvenanceResponseBody,
    DeliverableProductionNodeBody,
    DeliverableProductionProvenanceResponseBody,
    DeliverableRevisionNodeBody,
    DeliverableRevisionProvenanceResponseBody,
    MilestoneAcceptanceChainBody,
    MilestoneAcceptanceNodeBody,
    PlanApprovalProvenanceBody,
    TimeEntryNodeBody,
    WorkAssignmentChainBody,
    WorkAssignmentNodeBody,
    WorkEventNodeBody,
    get_request_context,
    router,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Endpoint inventory — Slice 1 + Slice 2 + Slice 3.
# ---------------------------------------------------------------------------


# Slice 1 and Slice 2 endpoints already mounted on the provenance
# router before task 15.2. The set is reproduced here so a regression
# that drops or renames one would fail this test rather than only
# manifesting in the Slice 1 / Slice 2 end-to-end suites.
_SLICE_1_AND_2_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/v1/regions/{region_id}/occurrences/{revision_id}/text"),
    ("GET", "/api/v1/backlinks"),
    ("GET", "/api/v1/decisions/{decision_id}/provenance"),
    ("GET", "/api/v1/findings/{finding_id}/provenance"),
    ("GET", "/api/v1/recommendations/{recommendation_id}/provenance"),
    ("GET", "/api/v1/trails/{trail_id}/revisions/{revision_id}/provenance"),
})


# Slice 3 endpoints introduced by task 15.2.
_SLICE_3_ENDPOINTS: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/api/v1/completions/{completion_id}/provenance"),
    (
        "GET",
        "/api/v1/deliverable-productions/{deliverable_production_id}/provenance",
    ),
    (
        "GET",
        "/api/v1/deliverables/{deliverable_id}/revisions/"
        "{deliverable_revision_id}/provenance",
    ),
})


def _mounted_endpoints() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", ())
    }


def test_router_preserves_slice1_and_slice2_endpoints() -> None:
    """Every pre-task-15.2 endpoint must still mount (Requirement 40.1)."""
    mounted = _mounted_endpoints()
    missing = _SLICE_1_AND_2_ENDPOINTS - mounted
    assert not missing, f"Slice 1/2 endpoints dropped by task 15.2: {sorted(missing)}"


def test_router_mounts_every_slice3_endpoint() -> None:
    """Every (method, path) pair from design §"Provenance_Navigator (extended)" must mount."""
    mounted = _mounted_endpoints()
    missing = _SLICE_3_ENDPOINTS - mounted
    assert not missing, f"Slice 3 endpoints missing: {sorted(missing)}"


def test_router_carries_no_extra_endpoints_beyond_slice3() -> None:
    """The router exposes exactly the Slice 1+2+3 inventory and no more."""
    mounted = _mounted_endpoints()
    expected = _SLICE_1_AND_2_ENDPOINTS | _SLICE_3_ENDPOINTS
    extra = mounted - expected
    assert not extra, f"Unexpected endpoints: {sorted(extra)}"


# ---------------------------------------------------------------------------
# Slice 3 RequestContext placeholder.
# ---------------------------------------------------------------------------


def test_request_context_placeholder_raises_until_wired() -> None:
    """Unwired :func:`get_request_context` raises NotImplementedError.

    Task 15.3 will override this placeholder on the composed app's
    :attr:`fastapi.FastAPI.dependency_overrides` map. Until then a
    stray call must fail loudly so a misconfigured app is detected
    at the first request rather than silently shipping a ``None``
    collaborator.
    """
    with pytest.raises(NotImplementedError):
        get_request_context()


# ---------------------------------------------------------------------------
# Response body classes — ``extra='forbid'`` is the AD-WS-9 invariant.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls",
    [
        CompletionNodeBody,
        CompletionProvenanceResponseBody,
        DeliverableProductionNodeBody,
        DeliverableProductionProvenanceResponseBody,
        DeliverableRevisionNodeBody,
        DeliverableRevisionProvenanceResponseBody,
        MilestoneAcceptanceChainBody,
        MilestoneAcceptanceNodeBody,
        PlanApprovalProvenanceBody,
        TimeEntryNodeBody,
        WorkAssignmentChainBody,
        WorkAssignmentNodeBody,
        WorkEventNodeBody,
    ],
)
def test_slice3_response_bodies_forbid_extra_fields(cls) -> None:
    """Every Slice 3 response body enforces ``extra='forbid'``.

    A future contributor accidentally adding a sensitive field (e.g.
    a planning attribute on a Completion node body, an observed-
    outcome attribute on a produced-Revision body) would surface
    here as a model-validation failure rather than silently
    shipping a Requirement 33 / 34 / 38.4 leak.
    """
    assert cls.model_config.get("extra") == "forbid"


# ---------------------------------------------------------------------------
# Module __all__ export surface.
# ---------------------------------------------------------------------------


def test_slice3_exports_appended_to_all() -> None:
    """Task 15.2's new public symbols are exported through ``__all__``."""
    exported = set(provenance_routes.__all__)
    expected_subset = {
        # Pre-existing Slice 1 / Slice 2 exports still present.
        "router",
        "get_engine",
        "get_provenance_navigator",
        # New Slice 3 exports.
        "get_request_context",
        "CompletionNodeBody",
        "CompletionProvenanceResponseBody",
        "DeliverableProductionNodeBody",
        "DeliverableProductionProvenanceResponseBody",
        "DeliverableRevisionNodeBody",
        "DeliverableRevisionProvenanceResponseBody",
        "MilestoneAcceptanceChainBody",
        "MilestoneAcceptanceNodeBody",
        "PlanApprovalProvenanceBody",
        "TimeEntryNodeBody",
        "WorkAssignmentChainBody",
        "WorkAssignmentNodeBody",
        "WorkEventNodeBody",
    }
    missing = expected_subset - exported
    assert not missing, f"Missing exports: {sorted(missing)}"
