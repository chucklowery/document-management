"""Unit tests for ``walking_slice.execution._routes`` (task 15.1).

The route handlers themselves cannot be exercised end-to-end until task
15.3 wires them into the FastAPI app and overrides the dependency
placeholders with real services; the Slice 3 end-to-end HTTP suite
(task 17) drives the wired routes.

These unit tests cover the surfaces this task creates *on its own*:

1. Every endpoint listed in design §"Components and Interfaces" is
   mounted on the :data:`router` at the canonical path the design
   names. A regression that drops or renames a path here would not be
   caught by the end-to-end suite until task 17 runs, so we guard it
   explicitly.
2. The placeholder dependency factories raise
   :class:`NotImplementedError` when invoked unwrapped — the
   convention every Slice 1 / Slice 2 route module shares so an
   unwired call fails loudly rather than silently returning ``None``.
3. Every Pydantic request model rejects unknown fields via
   ``extra='forbid'`` so a typo'd field name surfaces as a
   structured 400 rather than being silently dropped.
4. The prohibited-attribute screen rejects each forbidden prefix
   (Property 35 / Property 36 / Requirements 33.4, 34.5) at the API
   boundary; this is the "combine with ``_reject_prohibited_attributes``
   for the planning-attribute and observed-outcome rejection paths"
   clause of task 15.1.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from walking_slice.execution import _routes
from walking_slice.execution._routes import (
    CreateCompletionRequestBody,
    CreateDeliverableProductionRequestBody,
    CreateMilestoneAcceptanceRequestBody,
    CreateTimeEntryRequestBody,
    CreateWorkAssignmentRequestBody,
    CreateWorkEventRequestBody,
    _screen_prohibited_attributes,
    get_completion_service,
    get_deliverable_production_service,
    get_engine,
    get_milestone_acceptance_service,
    get_status_projector,
    get_time_entry_service,
    get_work_assignment_service,
    get_work_event_service,
    router,
)


# ---------------------------------------------------------------------------
# Endpoint inventory.
# ---------------------------------------------------------------------------


# The full endpoint inventory from design §"Components and Interfaces".
# Both method *and* path are pinned so a renamed handler that still
# serves the right HTTP verb is caught here.
_EXPECTED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/work-assignments"),
    ("GET", "/api/v1/work-assignments/{work_assignment_id}"),
    ("POST", "/api/v1/work-events"),
    ("GET", "/api/v1/work-events/{work_event_id}"),
    ("POST", "/api/v1/time-entries"),
    ("GET", "/api/v1/time-entries/{time_entry_id}"),
    ("POST", "/api/v1/deliverable-productions"),
    (
        "GET",
        "/api/v1/deliverable-productions/{deliverable_production_id}",
    ),
    ("POST", "/api/v1/milestone-acceptances"),
    (
        "GET",
        "/api/v1/milestone-acceptances/{milestone_acceptance_id}",
    ),
    ("POST", "/api/v1/completions"),
    ("GET", "/api/v1/completions/{completion_id}"),
    (
        "GET",
        "/api/v1/plan-revisions/{plan_revision_id}/execution-status",
    ),
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

    A handler accidentally added under the wrong path (typo'd plural,
    missing prefix) would show up here as a surplus entry.
    """
    mounted = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    extra = mounted - set(_EXPECTED_ENDPOINTS)
    assert not extra, f"Unexpected endpoints: {sorted(extra)}"


def test_router_prefix_and_tag() -> None:
    """The router is mounted at ``/api/v1`` with the ``execution`` tag."""
    assert router.prefix == "/api/v1"
    assert router.tags == ["execution"]


# ---------------------------------------------------------------------------
# Dependency placeholders.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "factory",
    [
        get_engine,
        get_work_assignment_service,
        get_work_event_service,
        get_time_entry_service,
        get_deliverable_production_service,
        get_milestone_acceptance_service,
        get_completion_service,
        get_status_projector,
    ],
)
def test_dependency_placeholders_raise_until_wired(factory) -> None:
    """Unwired placeholder factories must raise :class:`NotImplementedError`.

    Task 15.3 will override every placeholder on
    :attr:`fastapi.FastAPI.dependency_overrides`. Until then a stray
    call must fail loudly so a misconfigured app is detected at the
    first request rather than silently shipping a ``None`` collaborator.
    """
    with pytest.raises(NotImplementedError):
        factory()


# ---------------------------------------------------------------------------
# Pydantic request models.
# ---------------------------------------------------------------------------


_VALID_AUTHORITY_BASIS = {
    "type": "role-grant-id",
    "id": "00000000-0000-7000-8000-000000000001",
}


_REQUEST_MODELS = {
    "work_assignment": (
        CreateWorkAssignmentRequestBody,
        {
            "target_plan_revision_id": "pr-1",
            "assignee_party_id": "party-2",
            "assignment_rationale": "do the work",
            "authority_basis": _VALID_AUTHORITY_BASIS,
            "applicable_scope": "scope-1",
        },
    ),
    "work_event": (
        CreateWorkEventRequestBody,
        {
            "target_work_assignment_id": "wa-1",
            "event_kind": "started",
            "event_note": "kickoff",
            "authority_basis": _VALID_AUTHORITY_BASIS,
            "applicable_scope": "scope-1",
        },
    ),
    "time_entry": (
        CreateTimeEntryRequestBody,
        {
            "target_work_assignment_id": "wa-1",
            "effort_hours": "1.50",
            "effort_period_start": "2026-01-01T00:00:00.000+00:00",
            "effort_period_end": "2026-01-01T01:30:00.000+00:00",
            "authority_basis": _VALID_AUTHORITY_BASIS,
            "applicable_scope": "scope-1",
        },
    ),
    "deliverable_production": (
        CreateDeliverableProductionRequestBody,
        {
            "source_work_assignment_id": "wa-1",
            "produced_deliverable_revision_id": "dr-1",
            "target_deliverable_expectation_revision_id": "der-1",
            "production_rationale": "produced the deliverable",
            "authority_basis": _VALID_AUTHORITY_BASIS,
            "applicable_scope": "scope-1",
        },
    ),
    "milestone_acceptance": (
        CreateMilestoneAcceptanceRequestBody,
        {
            "source_deliverable_production_id": "dp-1",
            "outcome": "Accept",
            "rationale": "meets criteria",
            "authority_basis": _VALID_AUTHORITY_BASIS,
            "applicable_scope": "scope-1",
        },
    ),
    "completion": (
        CreateCompletionRequestBody,
        {
            "target_plan_revision_id": "pr-1",
            "outcome": "Completed",
            "rationale": "all milestones accepted",
            "source_milestone_acceptance_ids": ["ma-1"],
            "authority_basis": _VALID_AUTHORITY_BASIS,
            "applicable_scope": "scope-1",
        },
    ),
}


@pytest.mark.parametrize("name", list(_REQUEST_MODELS.keys()))
def test_request_models_accept_valid_payloads(name: str) -> None:
    """Sanity check: every model parses a representative valid payload."""
    model_cls, payload = _REQUEST_MODELS[name]
    instance = model_cls.model_validate(payload)
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


def test_work_event_kind_enumeration_enforced() -> None:
    """``event_kind`` is constrained to the five-value closed set."""
    _, payload = _REQUEST_MODELS["work_event"]
    polluted = dict(payload, event_kind="not-a-real-kind")
    with pytest.raises(ValidationError):
        CreateWorkEventRequestBody.model_validate(polluted)


def test_milestone_outcome_enumeration_enforced() -> None:
    """``outcome`` on Milestone Acceptance must be in ``{Accept, Reject}``."""
    _, payload = _REQUEST_MODELS["milestone_acceptance"]
    polluted = dict(payload, outcome="Maybe")
    with pytest.raises(ValidationError):
        CreateMilestoneAcceptanceRequestBody.model_validate(polluted)


def test_completion_outcome_enumeration_enforced() -> None:
    """``outcome`` on Completion must be in the two-value closed set."""
    _, payload = _REQUEST_MODELS["completion"]
    polluted = dict(payload, outcome="Almost_Done")
    with pytest.raises(ValidationError):
        CreateCompletionRequestBody.model_validate(polluted)


def test_authority_basis_type_enumeration_enforced() -> None:
    """``authority_basis.type`` must be in the AD-WS-10 set."""
    _, payload = _REQUEST_MODELS["work_assignment"]
    polluted = dict(
        payload,
        authority_basis={
            "type": "not-a-real-basis-type",
            "id": "00000000-0000-7000-8000-000000000001",
        },
    )
    with pytest.raises(ValidationError):
        CreateWorkAssignmentRequestBody.model_validate(polluted)


# ---------------------------------------------------------------------------
# Prohibited-attribute screen.
# ---------------------------------------------------------------------------


# One representative key from each prohibited-prefix family
# (Requirements 33.2, 33.3, 34.1, 34.2).
_PLANNING_KEY = "planned-scope"
_PLANNING_ASSUMPTION_KEY = "planning-assumption-1"
_ORDERING_RATIONALE_KEY = "ordering-rationale-text"
_PLAN_REVIEW_KEY = "plan-review-outcome"
_PLAN_APPROVAL_KEY = "plan-approval-outcome"
_OBSERVED_KEY = "observed-outcome-value"
_MEASUREMENT_KEY = "measurement-record-id"
_OUTCOME_REVIEW_KEY = "outcome-review-id"
_ATTRIBUTION_EVIDENCE_KEY = "attribution-evidence-id"
_SUCCESS_CONDITION_KEY = "success-condition-assessment-id"


@pytest.mark.parametrize(
    "prohibited_key",
    [
        _PLANNING_KEY,
        _PLANNING_ASSUMPTION_KEY,
        _ORDERING_RATIONALE_KEY,
        _PLAN_REVIEW_KEY,
        _PLAN_APPROVAL_KEY,
        _OBSERVED_KEY,
        _MEASUREMENT_KEY,
        _OUTCOME_REVIEW_KEY,
        _ATTRIBUTION_EVIDENCE_KEY,
        _SUCCESS_CONDITION_KEY,
    ],
)
def test_screen_rejects_every_prohibited_prefix(prohibited_key: str) -> None:
    """The screen rejects every Slice 3 prohibited prefix family.

    Property 35 (planning-attribute prefixes) and Property 36
    (observed-outcome prefixes) together define every prohibited prefix
    Slice 3 enforces at the API boundary.
    """
    body = {
        "target_plan_revision_id": "pr-1",
        "applicable_scope": "scope-1",
        prohibited_key: "leaked-value",
    }
    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(
            body, error_code="work_assignment_validation_failed"
        )
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error_code"] == "work_assignment_validation_failed"
    assert detail["failed_constraint"] == "prohibited_attribute"
    assert prohibited_key in detail["prohibited_keys"]


def test_screen_rejects_snake_case_planning_attribute() -> None:
    """Matching is case-insensitive and hyphen/underscore-invariant.

    ``planned_scope`` (snake_case, the Python/JSON convention) must be
    rejected by the same screen that catches ``planned-scope``
    (the design's hyphenated prose).
    """
    body = {
        "target_plan_revision_id": "pr-1",
        "applicable_scope": "scope-1",
        "planned_scope": "leaked plan",
    }
    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(
            body, error_code="work_assignment_validation_failed"
        )
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "planned_scope" in detail["prohibited_keys"]


def test_screen_passes_clean_body() -> None:
    """A body free of prohibited keys passes through silently."""
    body = {
        "target_plan_revision_id": "pr-1",
        "assignee_party_id": "party-2",
        "applicable_scope": "scope-1",
        "authority_basis": _VALID_AUTHORITY_BASIS,
    }
    # No exception => screen accepted the body.
    _screen_prohibited_attributes(
        body, error_code="work_assignment_validation_failed"
    )


# ---------------------------------------------------------------------------
# Module __all__ export surface.
# ---------------------------------------------------------------------------


def test_module_exports_router_and_request_models() -> None:
    """The ``__all__`` list covers every public symbol task 15.3 needs."""
    exported = set(_routes.__all__)
    expected_subset = {
        "router",
        "get_engine",
        "get_work_assignment_service",
        "get_work_event_service",
        "get_time_entry_service",
        "get_deliverable_production_service",
        "get_milestone_acceptance_service",
        "get_completion_service",
        "get_status_projector",
        "ErrorBody",
        "DenialResponseBody",
        "AuthorityBasisRequestBody",
        "CreateWorkAssignmentRequestBody",
        "CreateWorkEventRequestBody",
        "CreateTimeEntryRequestBody",
        "CreateDeliverableProductionRequestBody",
        "CreateMilestoneAcceptanceRequestBody",
        "CreateCompletionRequestBody",
    }
    missing = expected_subset - exported
    assert not missing, f"Missing exports: {sorted(missing)}"
