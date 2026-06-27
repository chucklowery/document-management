"""Unit tests for :mod:`walking_slice.deliverables._routes` (task 15.2).

The route handlers cannot be exercised end-to-end until task 15.3 wires
them into the FastAPI app and overrides the dependency placeholders
with real services; the Slice 3 end-to-end HTTP suite (task 17) drives
the wired routes.

These unit tests cover the surfaces this task creates *on its own*:

1. Every endpoint listed in design §"Deliverable_Repository" HTTP
   surface is mounted on the :data:`router` at the canonical path the
   design names. A regression that drops or renames a path here would
   not be caught by the end-to-end suite until task 17 runs, so we
   guard it explicitly.
2. The placeholder dependency factory raises
   :class:`NotImplementedError` when invoked unwrapped — the
   convention every Slice 1 / 2 route module shares so an unwired
   call fails loudly rather than silently returning ``None``.
3. The Pydantic request model rejects unknown fields via
   ``extra='forbid'`` (Property 35 — Plan/Execution separation
   enforced at the API boundary).
4. The prohibited-attribute screen rejects each forbidden prefix
   (Requirements 33.3, 34.2 / Property 35 / 36) at the API boundary
   before the service is invoked.
5. The base64 content decoder surfaces a structured 400 on malformed
   input rather than letting an opaque exception propagate.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from walking_slice.deliverables import _routes
from walking_slice.deliverables._routes import (
    CreateDeliverableRequestBody,
    CreateDeliverableResponseBody,
    DeliverableRevisionMetadataBody,
    DenialResponseBody,
    ErrorBody,
    _decode_content_bytes,
    _screen_prohibited_attributes,
    get_deliverable_repository_service,
    router,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Endpoint inventory — pins every (method, path) pair from design.
# ---------------------------------------------------------------------------


_EXPECTED_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/deliverables"),
    ("POST", "/api/v1/deliverables/{deliverable_id}/revisions"),
    (
        "GET",
        "/api/v1/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}",
    ),
    (
        "GET",
        "/api/v1/deliverables/{deliverable_id}/revisions/"
        "{deliverable_revision_id}/content",
    ),
)


def test_router_mounts_every_design_endpoint() -> None:
    """Every (method, path) pair from design §"Deliverable_Repository" must mount."""
    mounted = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    missing = set(_EXPECTED_ENDPOINTS) - mounted
    assert not missing, f"Missing endpoints: {sorted(missing)}"


def test_router_carries_no_extra_endpoints() -> None:
    """The router exposes exactly the design's endpoint inventory and no more."""
    mounted = {
        (method, route.path)
        for route in router.routes
        for method in getattr(route, "methods", ())
    }
    extra = mounted - set(_EXPECTED_ENDPOINTS)
    assert not extra, f"Unexpected endpoints: {sorted(extra)}"


def test_router_prefix_and_tag() -> None:
    """The router is mounted at ``/api/v1`` with the ``deliverables`` tag."""
    assert router.prefix == "/api/v1"
    assert router.tags == ["deliverables"]


# ---------------------------------------------------------------------------
# Dependency placeholders.
# ---------------------------------------------------------------------------


def test_dependency_placeholder_raises_until_wired() -> None:
    """Unwired :func:`get_deliverable_repository_service` raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        get_deliverable_repository_service()


# ---------------------------------------------------------------------------
# CreateDeliverableRequestBody — schema and validation.
# ---------------------------------------------------------------------------


def _valid_create_payload() -> dict:
    """A minimal request body that satisfies every declared-field rule."""
    return {
        # 4-character base64 string decodes to 3 bytes which sits in
        # the Requirement 26.1 accepted range. Service-layer length
        # checks would normally validate against the decoded payload.
        "content_bytes": "YWJj",  # base64 of b"abc"
        "content_type": "text/markdown",
        "produced_deliverable_name": "Pilot Rollout Plan",
        "originating_work_assignment_id": "00000000-0000-7000-8000-000000d00001",
    }


def test_request_model_accepts_valid_payload() -> None:
    """Sanity check: the request model parses a well-formed payload."""
    body = CreateDeliverableRequestBody.model_validate(_valid_create_payload())
    assert body.content_type == "text/markdown"
    assert body.produced_deliverable_name == "Pilot Rollout Plan"


def test_request_model_rejects_unknown_fields() -> None:
    """``extra='forbid'`` must reject any unknown top-level field (Property 35)."""
    polluted = _valid_create_payload()
    polluted["unknown_field_xyz"] = "should be rejected"
    with pytest.raises(ValidationError):
        CreateDeliverableRequestBody.model_validate(polluted)


def test_request_model_is_frozen_and_forbids_extra() -> None:
    """Request model is configured ``frozen=True`` and ``extra='forbid'``."""
    cfg = CreateDeliverableRequestBody.model_config
    assert cfg.get("frozen") is True
    assert cfg.get("extra") == "forbid"


def test_request_model_rejects_unsupported_content_type() -> None:
    """Content types outside the 7-value enumeration are rejected by Pydantic."""
    polluted = _valid_create_payload()
    polluted["content_type"] = "application/zip"
    with pytest.raises(ValidationError):
        CreateDeliverableRequestBody.model_validate(polluted)


def test_request_model_rejects_zero_length_name() -> None:
    """A 0-character name violates the Field(min_length=1) constraint."""
    polluted = _valid_create_payload()
    polluted["produced_deliverable_name"] = ""
    with pytest.raises(ValidationError):
        CreateDeliverableRequestBody.model_validate(polluted)


def test_request_model_rejects_201_character_name() -> None:
    """A 201-character name violates the Field(max_length=200) constraint."""
    polluted = _valid_create_payload()
    polluted["produced_deliverable_name"] = "a" * 201
    with pytest.raises(ValidationError):
        CreateDeliverableRequestBody.model_validate(polluted)


def test_request_model_rejects_empty_originating_work_assignment_id() -> None:
    """A 0-character originating Work Assignment Identity is rejected."""
    polluted = _valid_create_payload()
    polluted["originating_work_assignment_id"] = ""
    with pytest.raises(ValidationError):
        CreateDeliverableRequestBody.model_validate(polluted)


# ---------------------------------------------------------------------------
# Response models — frozen / forbidden-extra contracts.
# ---------------------------------------------------------------------------


def test_response_models_forbid_extra_fields() -> None:
    """Every response model enforces ``extra='forbid'``.

    A future contributor accidentally adding a sensitive field to the
    denial body would surface here as a model-validation failure
    rather than silently shipping a Requirement 30.7 / 38.4 leak.
    """
    for cls in (
        CreateDeliverableResponseBody,
        DeliverableRevisionMetadataBody,
        DenialResponseBody,
        ErrorBody,
    ):
        assert cls.model_config.get("extra") == "forbid"


def test_denial_response_body_carries_only_three_fields() -> None:
    """:class:`DenialResponseBody` carries only the AD-WS-9 three-field shape."""
    body = DenialResponseBody(reason_code="no-role-assignment", correlation_id="c1")
    dumped = body.model_dump()
    assert set(dumped.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }
    assert dumped["generic_denial_indicator"] == "denied"


# ---------------------------------------------------------------------------
# Prohibited-attribute screen — Property 35 / 36.
# ---------------------------------------------------------------------------


def test_screen_rejects_planning_attribute_key() -> None:
    """A request body carrying a ``planned-`` key is rejected at the API boundary.

    Requirement 33.3 / Property 35: produced-Deliverable submissions
    that carry any planning-attribute prefix are rejected before the
    service is invoked.
    """
    body = _valid_create_payload()
    body["planned-budget-line"] = "leak"
    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(body)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error_code"] == "deliverable_validation_failed"
    assert detail["failed_constraint"] == "prohibited_attribute"
    assert "planned-budget-line" in detail["prohibited_keys"]


def test_screen_rejects_observed_outcome_attribute_key() -> None:
    """A request body carrying an ``observed-`` key is rejected (Requirement 34.2)."""
    body = _valid_create_payload()
    body["observed-outcome-value"] = 42
    with pytest.raises(HTTPException) as exc_info:
        _screen_prohibited_attributes(body)
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "observed-outcome-value" in detail["prohibited_keys"]


def test_screen_passes_clean_body() -> None:
    """A body with no prohibited prefixes passes through silently."""
    body = _valid_create_payload()
    # No exception means the screen accepted the body.
    _screen_prohibited_attributes(body)


# ---------------------------------------------------------------------------
# Base64 content-bytes decoder.
# ---------------------------------------------------------------------------


def test_decode_content_bytes_round_trip() -> None:
    """A valid base64 string decodes to the original bytes."""
    decoded = _decode_content_bytes("YWJj")
    assert decoded == b"abc"


def test_decode_content_bytes_rejects_invalid_base64() -> None:
    """Malformed base64 surfaces as a structured 400."""
    with pytest.raises(HTTPException) as exc_info:
        _decode_content_bytes("!!!not-base64!!!")
    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["error_code"] == "deliverable_validation_failed"
    assert detail["failed_constraint"] == "content_bytes_invalid_base64"


# ---------------------------------------------------------------------------
# Module __all__ export surface.
# ---------------------------------------------------------------------------


def test_module_exports_router_and_request_models() -> None:
    """The ``__all__`` list covers every public symbol task 15.2 needs."""
    exported = set(_routes.__all__)
    expected_subset = {
        "router",
        "get_deliverable_repository_service",
        "CreateDeliverableRequestBody",
        "CreateDeliverableResponseBody",
        "DeliverableRevisionMetadataBody",
        "DenialResponseBody",
        "ErrorBody",
    }
    missing = expected_subset - exported
    assert not missing, f"Missing exports: {sorted(missing)}"
