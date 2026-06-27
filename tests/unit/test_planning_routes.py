"""Unit tests for ``walking_slice.planning._routes`` (task 15.1).

The route handlers themselves cannot be exercised end-to-end until
task 15.2 wires them into the FastAPI app and overrides the
dependency placeholders with real services; the Slice 2 end-to-end
HTTP suite (task 17) drives the wired routes.

These unit tests cover the surfaces this task creates *on its own*:

1. Every endpoint listed in design §"Components and Interfaces" is
   mounted on the :data:`router` at the canonical path the design
   names. A regression that drops or renames a path here would not
   be caught by the end-to-end suite until task 17 runs, so we
   guard it explicitly.
2. The placeholder dependency factories raise
   :class:`NotImplementedError` when invoked unwrapped — the
   convention every Slice 1 route module shares so an unwired call
   fails loudly rather than silently returning ``None``.
3. Every Pydantic request model rejects unknown fields via
   ``extra='forbid'`` so a typo'd field name surfaces as a
   structured 400 rather than being silently dropped.
4. The prohibited-attribute screen rejects each forbidden prefix
   (Property 22 / Requirements 12.1, 12.2, 13.1, 13.2, 13.5) at the
   API boundary; this is the "combine with
   ``_reject_prohibited_attributes`` for the execution /
   observed-outcome / produced-deliverable rejection paths" clause
   of task 15.1.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from walking_slice.planning import _routes
from walking_slice.planning._routes import (
    CreateActivityPlanRequestBody,
    CreateDeliverableExpectationRequestBody,
    CreateIntendedOutcomeRequestBody,
    CreateObjectiveRequestBody,
    CreatePlanApprovalRequestBody,
    CreatePlanRevisionRequestBody,
    CreatePlanReviewRequestBody,
    CreateProjectRequestBody,
    _screen_prohibited_attributes,
    get_activity_plan_service,
    get_deliverable_expectation_service,
    get_engine,
    get_intended_outcome_service,
    get_objective_service,
    get_plan_approval_service,
    get_plan_review_service,
    get_plan_revision_service,
    get_project_service,
    get_provenance_navigator,
    router,
)


# ---------------------------------------------------------------------------
# Endpoint inventory.
# ---------------------------------------------------------------------------


# The full endpoint inventory from design §"Components and Interfaces".
# Both method *and* path are pinned so a renamed handler that still
# serves the right HTTP verb is caught here.
_EXPECTED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/objectives"),
    ("GET", "/api/v1/objectives/{objective_id}/revisions/{revision_id}"),
    ("POST", "/api/v1/intended-outcomes"),
    (
        "GET",
        "/api/v1/intended-outcomes/{intended_outcome_id}/revisions/{revision_id}",
    ),
    ("POST", "/api/v1/projects"),
    ("GET", "/api/v1/projects/{project_id}/revisions/{revision_id}"),
    ("POST", "/api/v1/deliverable-expectations"),
    (
        "GET",
        "/api/v1/deliverable-expectations/"
        "{deliverable_expectation_id}/revisions/{revision_id}",
    ),
    ("POST", "/api/v1/activity-plans"),
    ("GET", "/api/v1/activity-plans/{activity_plan_id}"),
    (
        "POST",
        "/api/v1/activity-plans/{activity_plan_id}/plan-revisions",
    ),
    (
        "GET",
        "/api/v1/activity-plans/{activity_plan_id}/plan-revisions/{revision_id}",
    ),
    ("POST", "/api/v1/plan-revisions/{plan_revision_id}/reviews"),
    (
        "GET",
        "/api/v1/plan-reviews/{plan_review_id}/revisions/{revision_id}",
    ),
    ("POST", "/api/v1/plan-revisions/{plan_revision_id}/approvals"),
    ("GET", "/api/v1/plan-approvals/{plan_approval_id}"),
    ("GET", "/api/v1/plan-approvals/{plan_approval_id}/provenance"),
)


def test_router_mounts_every_design_endpoint() -> None:
    """Every (method, path) pair from design §"Components and Interfaces" must mount."""
    mounted = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    missing = set(_EXPECTED_ENDPOINTS) - mounted
    assert not missing, f"Missing endpoints: {sorted(missing)}"


def test_router_carries_no_extra_endpoints() -> None:
    """The router exposes exactly the design's endpoint inventory and no more.

    A handler accidentally added under the wrong path (typo'd
    plural, missing prefix) would show up here as a surplus entry.
    """
    mounted = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    extra = mounted - set(_EXPECTED_ENDPOINTS)
    assert not extra, f"Unexpected endpoints: {sorted(extra)}"


def test_router_prefix_and_tag() -> None:
    """The router is mounted at ``/api/v1`` with the ``planning`` tag."""
    assert router.prefix == "/api/v1"
    assert router.tags == ["planning"]


# ---------------------------------------------------------------------------
# Dependency placeholders.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        get_engine,
        get_objective_service,
        get_intended_outcome_service,
        get_project_service,
        get_deliverable_expectation_service,
        get_activity_plan_service,
        get_plan_revision_service,
        get_plan_review_service,
        get_plan_approval_service,
        get_provenance_navigator,
    ],
)
def test_dependency_placeholders_raise_until_wired(factory) -> None:
    """Unwired placeholder factories must raise :class:`NotImplementedError`.

    Task 15.2 will override every placeholder on
    :attr:`fastapi.FastAPI.dependency_overrides`. Until then a stray
    call must fail loudly so a misconfigured app is detected at the
    first request rather than silently shipping a ``None`` collaborator.
    """
    with pytest.raises(NotImplementedError):
        factory()


# ---------------------------------------------------------------------------
# Pydantic request models.
# ---------------------------------------------------------------------------


_REQUEST_MODELS = {
    "objective": (
        CreateObjectiveRequestBody,
        {
            "statement": "valid statement",
            "rationale": "valid rationale",
            "target_decision_id": "dec-1",
            "applicable_scope": "scope-1",
        },
    ),
    "intended_outcome": (
        CreateIntendedOutcomeRequestBody,
        {
            "target_objective_id": "obj-1",
            "success_condition": "the outcome will be observable",
            "observation_window": "Q1 2026",
            "attribution_assumption": "no confounders",
            "applicable_scope": "scope-1",
        },
    ),
    "project": (
        CreateProjectRequestBody,
        {
            "target_objective_id": "obj-1",
            "name": "Project A",
            "summary": "a summary",
            "planned_start_date": "2026-01-01",
            "planned_end_date": "2026-06-30",
            "applicable_scope": "scope-1",
        },
    ),
    "deliverable_expectation": (
        CreateDeliverableExpectationRequestBody,
        {
            "target_project_id": "proj-1",
            "name": "Deliverable A",
            "description": "a description",
            "deliverable_kind": "Document",
            "acceptance_criteria": "must satisfy spec",
            "applicable_scope": "scope-1",
        },
    ),
    "activity_plan": (
        CreateActivityPlanRequestBody,
        {
            "target_project_id": "proj-1",
            "title": "Activity Plan Alpha",
            "applicable_scope": "scope-1",
        },
    ),
    "plan_revision": (
        CreatePlanRevisionRequestBody,
        {
            "planned_scope": "the planned scope statement",
            "deliverable_expectation_refs": [],
            "planning_assumptions": [],
            "ordering_rationale": None,
            "predecessor_plan_revision_id": None,
            "applicable_scope": "scope-1",
        },
    ),
    "plan_review": (
        CreatePlanReviewRequestBody,
        {
            "outcome": "Endorse",
            "rationale": "looks correct",
            "authority_basis": {
                "type": "role-grant-id",
                "id": "00000000-0000-7000-8000-000000000001",
            },
            "applicable_scope": "scope-1",
        },
    ),
    "plan_approval": (
        CreatePlanApprovalRequestBody,
        {
            "outcome": "Approve",
            "rationale": "all checks pass",
            "authority_basis": {
                "type": "role-grant-id",
                "id": "00000000-0000-7000-8000-000000000001",
            },
            "applicable_scope": "scope-1",
            "omissions": [],
        },
    ),
}


@pytest.mark.parametrize("name", list(_REQUEST_MODELS.keys()))
def test_request_models_accept_valid_payloads(name: str) -> None:
    """Sanity check: every model parses a representative valid payload."""
    model_cls, payload = _REQUEST_MODELS[name]
    instance = model_cls.model_validate(payload)
    # Pydantic v2 builds frozen models from the same dict; verify the
    # round-trip is total.
    assert instance is not None


@pytest.mark.parametrize("name", list(_REQUEST_MODELS.keys()))
def test_request_models_reject_unknown_fields(name: str) -> None:
    """``extra='forbid'`` must reject any unknown top-level field."""
    model_cls, payload = _REQUEST_MODELS[name]
    polluted = dict(payload)
    polluted["unknown_field_xyz"] = "should be rejected"
    with pytest.raises(ValidationError):
        model_cls.model_validate(polluted)


def test_request_models_are_frozen() -> None:
    """Every request model is configured with ``frozen=True`` per task 15.1."""
    for model_cls, _ in _REQUEST_MODELS.values():
        assert model_cls.model_config.get("frozen") is True
        assert model_cls.model_config.get("extra") == "forbid"


# ---------------------------------------------------------------------------
# Prohibited-attribute screen.
# ---------------------------------------------------------------------------


_EXECUTION_KEY = "work-performed-evidence"
_OBSERVED_OUTCOME_KEY = "observed-outcome-value"
_PRODUCED_DELIVERABLE_KEY = "produced-deliverable-id"


def test_screen_rejects_execution_attribute_for_objective() -> None:
    """Execution-prefix keys are rejected on Objective bodies (Property 22)."""
    body = {
        "statement": "ok",
        "rationale": None,
        "target_decision_id": "d",
        "applicable_scope": "s",
        _EXECUTION_KEY: "leak",
    }
    from walking_slice.planning._helpers import ALL_PROHIBITED_PREFIXES

    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(
            body,
            prefixes=ALL_PROHIBITED_PREFIXES,
            error_code="objective_validation_failed",
        )
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error_code"] == "objective_validation_failed"
    assert detail["failed_constraint"] == "prohibited_attribute"
    assert _EXECUTION_KEY in detail["prohibited_keys"]


def test_screen_rejects_observed_outcome_attribute_for_intended_outcome() -> None:
    """Observed-outcome keys are rejected on Intended Outcome bodies (Requirement 13.1)."""
    body = {
        "target_objective_id": "o",
        "success_condition": "ok",
        "applicable_scope": "s",
        _OBSERVED_OUTCOME_KEY: 42,
    }
    from walking_slice.planning._helpers import (
        OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
    )

    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(
            body,
            prefixes=OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
            error_code="intended_outcome_validation_failed",
        )
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error_code"] == "intended_outcome_validation_failed"
    assert _OBSERVED_OUTCOME_KEY in detail["prohibited_keys"]


def test_screen_rejects_produced_deliverable_attribute_for_deliverable_expectation() -> None:
    """Produced-deliverable keys are rejected on Deliverable Expectation bodies (Requirement 13.2)."""
    body = {
        "target_project_id": "p",
        "name": "n",
        "deliverable_kind": "Document",
        "applicable_scope": "s",
        _PRODUCED_DELIVERABLE_KEY: "01999999",
    }
    from walking_slice.planning._helpers import (
        PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
    )

    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(
            body,
            prefixes=PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
            error_code="deliverable_expectation_validation_failed",
        )
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert _PRODUCED_DELIVERABLE_KEY in detail["prohibited_keys"]


def test_screen_passes_clean_body() -> None:
    """A body free of prohibited keys passes through silently."""
    body = {
        "statement": "ok",
        "rationale": None,
        "target_decision_id": "d",
        "applicable_scope": "s",
    }
    from walking_slice.planning._helpers import ALL_PROHIBITED_PREFIXES

    # No exception => screen accepted the body.
    _screen_prohibited_attributes(
        body,
        prefixes=ALL_PROHIBITED_PREFIXES,
        error_code="objective_validation_failed",
    )


# ---------------------------------------------------------------------------
# Module __all__ export surface.
# ---------------------------------------------------------------------------


def test_module_exports_router_and_request_models() -> None:
    """The ``__all__`` list covers every public symbol task 15.2 needs."""
    exported = set(_routes.__all__)
    expected_subset = {
        "router",
        "get_engine",
        "get_objective_service",
        "get_intended_outcome_service",
        "get_project_service",
        "get_deliverable_expectation_service",
        "get_activity_plan_service",
        "get_plan_revision_service",
        "get_plan_review_service",
        "get_plan_approval_service",
        "get_provenance_navigator",
        "ErrorBody",
        "DenialResponseBody",
        "CreateObjectiveRequestBody",
        "CreateIntendedOutcomeRequestBody",
        "CreateProjectRequestBody",
        "CreateDeliverableExpectationRequestBody",
        "CreateActivityPlanRequestBody",
        "CreatePlanRevisionRequestBody",
        "CreatePlanReviewRequestBody",
        "CreatePlanApprovalRequestBody",
    }
    missing = expected_subset - exported
    assert not missing, f"Missing exports: {sorted(missing)}"
