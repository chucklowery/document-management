"""HTTP routes for the Slice 3 Deliverable_Repository (task 15.2).

Design reference: ``.kiro/specs/third-walking-slice/design.md``
§"Deliverable_Repository" HTTP surface and §"Error Handling".

This module exposes a single :class:`fastapi.APIRouter` mounted under
``/api/v1`` that wires the Deliverable_Repository HTTP surface from the
design's "Components and Interfaces" table:

| Method | Path | Service |
|--------|------|---------|
| ``POST``  | ``/api/v1/deliverables`` | :meth:`DeliverableRepositoryService.create_produced_deliverable` |
| ``POST``  | ``/api/v1/deliverables/{deliverable_id}/revisions`` | :meth:`DeliverableRepositoryService.create_produced_deliverable` |
| ``GET``   | ``/api/v1/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}`` | :meth:`DeliverableRepositoryService.get_revision` |
| ``GET``   | ``/api/v1/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}/content`` | :meth:`DeliverableRepositoryService.get_revision_text` |

Responsibilities (per task 15.2):

1. Wire each route through the Slice 1 :class:`RequestContext`
   dependency so the route handler resolves the actor Party Identity
   (``ctx.party_id``), the per-request :class:`Engine`
   (``ctx.engine``), the per-request :class:`Clock` (``ctx.clock``),
   and the correlation handle (``ctx.correlation_id``) from one
   bearer-token-validated bundle (matching the Slice 2 Planning route
   pattern in :mod:`walking_slice.planning._routes`).
2. Define Pydantic v2 request models with
   ``ConfigDict(extra='forbid', frozen=True)`` so any typo'd field
   surfaces as a structured 400 rather than being silently dropped.
   Additionally call
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   on the raw request body for every produced-Deliverable write so
   Property 35 / 36 (Plan/Execution and Output/Outcome separation) is
   enforced at the API boundary (Requirements 33.3, 34.2).
3. Map every Deliverable_Repository exception to the HTTP code listed
   in design §"Error Handling":

   - :class:`DeliverableContentValidationError` → 400 with
     ``error_code = 'deliverable_validation_failed'`` and the
     ``failed_constraint`` from the exception.
   - :class:`WorkAssignmentNotResolvableError` → 404 with
     ``error_code = 'originating_work_assignment_not_resolvable'``.
   - :class:`DeliverableRepositoryAuthorizationError` (and its
     :class:`WorkAssignmentAssigneeBindingError` subclass) → 403 with
     the AD-WS-9 indistinguishable denial body carrying **only**
     ``generic_denial_indicator``, ``reason_code``, and
     ``correlation_id`` (Requirement 30.7 / 38.4).
   - :class:`DeliverableRepositoryAuditFailureError` → 503 with the
     audit-failure indicator set (matching the Slice 1 / Slice 2
     contract and design §"Error Handling" rule 7).
   - :class:`DeliverableRevisionNotFoundError` → 404 (the metadata
     read could not find a matching ``Deliverable_Revisions`` row).
   - :class:`DeliverableRevisionDigestMismatchError` → 503 (the
     persisted digest does not match the stored bytes — an AD-WS-27
     invariant violation, treated as operator-facing rather than a
     caller error).
   - :class:`walking_slice.audit.AuditAppendError` → 503.

Requirements satisfied:
    22.1, 22.2, 22.3, 22.8 — produced Deliverable Resource and
        Revision identities flow through ``Identifier_Registry`` per
        the service contract; the HTTP layer surfaces both.
    26.1, 26.2, 26.3, 26.5 — input validation and the persisted
        ``role_marker = 'generated_output'`` / content-digest invariants
        are observable through the HTTP responses.
    26.6, 26.7 — denial path uses the AD-WS-9 indistinguishable
        response shape; consequential audit row is appended inside the
        service transaction.
    31.1, 35.1 — the additive provenance routes mounted in
        :mod:`walking_slice.routes.provenance` (extended by task 15.2)
        surface the Slice 3 navigation anchors used by an auditor to
        walk back to the originating Document Revision text.
    33.3, 34.2 — request bodies carrying prohibited planning-attribute
        or observed-outcome prefixes are rejected at the API boundary
        before the service is invoked.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope; the factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly. Task 15.3 wires the concrete instances through
``walking_slice.app.create_app`` via
:attr:`fastapi.FastAPI.dependency_overrides`.
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any, Final, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from walking_slice.app import get_request_context
from walking_slice.audit import AuditAppendError
from walking_slice.auth_middleware import RequestContext
from walking_slice.deliverables.repository import (
    DeliverableContentValidationError,
    DeliverableRepositoryAuditFailureError,
    DeliverableRepositoryAuthorizationError,
    DeliverableRepositoryService,
    DeliverableRevisionDigestMismatchError,
    DeliverableRevisionNotFoundError,
    WorkAssignmentAssigneeBindingError,
    WorkAssignmentNotResolvableError,
)
from walking_slice.execution._helpers import (
    ALL_PROHIBITED_PREFIXES,
    ExecutionValidationError,
    _reject_prohibited_attributes,
)


__all__ = [
    "CreateDeliverableRequestBody",
    "CreateDeliverableResponseBody",
    "DeliverableRevisionMetadataBody",
    "DenialResponseBody",
    "ErrorBody",
    "get_deliverable_repository_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["deliverables"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholder.
#
# Task 15.3 wires the concrete :class:`DeliverableRepositoryService` instance
# through ``walking_slice.app.create_app`` by adding an override to
# :attr:`fastapi.FastAPI.dependency_overrides`. Tests do the same on their
# per-test :class:`fastapi.FastAPI` instances.
# ---------------------------------------------------------------------------


def get_deliverable_repository_service() -> DeliverableRepositoryService:
    """Provide the slice's :class:`DeliverableRepositoryService` singleton.

    The service is connection-scoped at call time: its public methods
    accept the caller's SQLAlchemy connection / engine, so a single
    instance serves every request safely.
    """
    raise NotImplementedError(
        "walking_slice.deliverables._routes.get_deliverable_repository_service "
        "must be overridden by app composition (task 15.3) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Shared response shells.
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """Structured error envelope for 400 / 404 / 409 / 503 responses.

    Matches the Planning_Service envelope shape so a single client-side
    error handler covers every Slice 1 / 2 / 3 endpoint. Fields are
    optional; only the ones relevant to the failure are populated.

    The 403 denial response uses :class:`DenialResponseBody` instead of
    this envelope so the AD-WS-9 indistinguishable shape contains
    **only** ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` (Requirement 30.7).
    """

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    failed_constraint: Optional[str] = None
    prohibited_keys: list[str] = Field(default_factory=list)
    deliverable_id: Optional[str] = None
    deliverable_revision_id: Optional[str] = None
    originating_work_assignment_id: Optional[str] = None
    audit_failure_indicator: Optional[str] = None
    correlation_id: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


class DenialResponseBody(BaseModel):
    """403 response body for a denied produced-Deliverable attempt (AD-WS-9).

    The shape is fixed by AD-WS-9 / Requirement 30.7:
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` — nothing else. ``extra='forbid'`` keeps this
    invariant locally: an accidental extra field would surface as a
    model-validation failure (a 500) rather than silently shipping a
    leak.
    """

    model_config = ConfigDict(extra="forbid")

    generic_denial_indicator: Literal["denied"] = "denied"
    reason_code: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Request / response models.
# ---------------------------------------------------------------------------


# The seven enumerated produced-Deliverable content types from
# Requirement 26.1. Re-declared here as a Literal so Pydantic produces
# a structured 400 with the value error when callers submit one of the
# many possible unsupported types (the service still validates against
# the canonical set held in :mod:`walking_slice.deliverables.repository`).
_ContentTypeLiteral = Literal[
    "text/markdown",
    "text/plain",
    "application/pdf",
    "application/json",
    "image/png",
    "image/svg+xml",
    "application/octet-stream",
]


class CreateDeliverableRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/deliverables`` and ``POST
    /api/v1/deliverables/{deliverable_id}/revisions``.

    ``content_bytes`` is declared as a base64-encoded string on the
    wire (matching the Slice 1 Evidence endpoints' encoding) and
    decoded by the route handler before the service is invoked. The
    service-layer 1..100 MB length and content-type / name enumeration
    checks run against the decoded payload (Requirement 26.1) and
    surface as :class:`DeliverableContentValidationError` with stable
    ``failed_constraint`` strings.

    The ``authoring_party_id`` field on the design's service signature
    is *not* drawn from the request body — Requirement 32.7 / AD-WS-29
    require the authoring Party Identity to come from the
    bearer-token-validated :class:`RequestContext` so a client cannot
    forge an attribution. The route handler therefore wires
    ``ctx.party_id`` into the service call and does not accept the
    field on the request body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    content_bytes: str = Field(
        min_length=1,
        description=(
            "Base64-encoded produced-Deliverable bytes. Decoded length "
            "must be 1..100 MB per Requirement 26.1 (enforced by the "
            "service)."
        ),
    )
    content_type: _ContentTypeLiteral = Field(
        description=(
            "IANA-style content type from the Requirement 26.1 "
            "enumeration: text/markdown, text/plain, application/pdf, "
            "application/json, image/png, image/svg+xml, or "
            "application/octet-stream."
        ),
    )
    produced_deliverable_name: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Human-readable name of the produced Deliverable, "
            "1..200 characters (Requirement 26.1 / 26.5)."
        ),
    )
    originating_work_assignment_id: str = Field(
        min_length=1,
        description=(
            "Identity of the Work Assignment Record under whose "
            "authority this Revision is being authored. Must resolve "
            "to an existing Work_Assignment_Records row whose "
            "assignee_party_id matches the requesting Party (AD-WS-29)."
        ),
    )


class CreateDeliverableResponseBody(BaseModel):
    """201 response body for the produced-Deliverable creation endpoints.

    Surfaces every field of
    :class:`walking_slice.deliverables.repository.CreateProducedDeliverableResult`
    so callers can correlate the created Resource and Revision with the
    consequential audit row in one round-trip (Requirement 22.2 — the
    Resource Identity and Revision Identity are returned as a pair).
    """

    model_config = ConfigDict(extra="forbid")

    deliverable_id: str
    deliverable_revision_id: str
    produced_deliverable_name: str
    content_type: str
    content_digest_sha256: str
    content_length_bytes: int
    role_marker: str
    originating_work_assignment_id: str
    authoring_party_id: str
    recorded_at: str
    correlation_id: str


class DeliverableRevisionMetadataBody(BaseModel):
    """200 response body for the produced-Deliverable Revision metadata read.

    Mirrors
    :class:`walking_slice.deliverables.repository.DeliverableRevisionRow`
    over the wire; ``content_bytes`` is NOT included so the metadata
    read remains constant-cost regardless of the Revision's content
    size. Callers needing the byte content fetch the dedicated
    ``/content`` endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    deliverable_id: str
    deliverable_revision_id: str
    content_type: str
    content_digest_sha256: str
    role_marker: str
    originating_work_assignment_id: str
    authoring_party_id: str
    recorded_at: str
    content_length_bytes: int


# ---------------------------------------------------------------------------
# Request-body helpers (mirrors Slice 2 planning helpers for consistency).
# ---------------------------------------------------------------------------


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Read and JSON-decode the request body, returning a dict.

    Empty bodies and non-object bodies surface as a structured 400.
    The returned dict is passed both to
    :func:`_reject_prohibited_attributes` (the prohibited-prefix
    screen) and to Pydantic (for declared-field validation) so the
    two screens together cover Property 35 / 36 (Plan/Execution and
    Output/Outcome separation).
    """
    raw = await request.body()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code="empty_request_body",
                message="A JSON request body is required for this endpoint.",
            ).model_dump(exclude_none=True),
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code="invalid_json_body",
                message=f"Request body is not valid JSON: {exc.msg}",
            ).model_dump(exclude_none=True),
        ) from exc
    if not isinstance(decoded, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code="invalid_json_body",
                message="Request body must be a JSON object.",
            ).model_dump(exclude_none=True),
        )
    return decoded


def _screen_prohibited_attributes(body: dict[str, Any]) -> None:
    """Reject the request body when any top-level key matches the
    prohibited planning-attribute or observed-outcome prefix sets.

    Wraps
    :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
    so an :class:`ExecutionValidationError` becomes a structured 400 at
    the API boundary (Requirements 33.3, 34.2 / Property 35 / 36).
    """
    try:
        _reject_prohibited_attributes(body, ALL_PROHIBITED_PREFIXES)
    except ExecutionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code="deliverable_validation_failed",
                message=str(exc),
                failed_constraint="prohibited_attribute",
                prohibited_keys=list(exc.prohibited_keys),
            ).model_dump(exclude_none=True),
        ) from exc


def _validation_error_to_http(exc: ValidationError) -> HTTPException:
    """Convert a Pydantic :class:`ValidationError` to a 400 ``HTTPException``."""
    errors = exc.errors(include_url=False)
    missing: list[str] = []
    other: list[dict[str, Any]] = []
    for err in errors:
        loc = err.get("loc", ())
        field = ".".join(str(part) for part in loc) if loc else "<root>"
        err_type = err.get("type", "")
        if err_type in {"missing", "missing_argument"}:
            missing.append(field)
        else:
            other.append({key: value for key, value in err.items() if key != "ctx"})
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error_code="deliverable_validation_failed",
            message="Request failed validation.",
            missing=sorted(set(missing)),
            validation_errors=other,
        ).model_dump(exclude_none=True),
    )


def _content_validation_to_http(
    exc: DeliverableContentValidationError,
) -> HTTPException:
    """Map :class:`DeliverableContentValidationError` to a 400."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error_code="deliverable_validation_failed",
            message=str(exc),
            failed_constraint=exc.failed_constraint,
            prohibited_keys=list(exc.prohibited_keys),
        ).model_dump(exclude_none=True),
    )


def _work_assignment_not_resolvable_to_http(
    exc: WorkAssignmentNotResolvableError,
) -> HTTPException:
    """Map :class:`WorkAssignmentNotResolvableError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error_code="originating_work_assignment_not_resolvable",
            message=str(exc),
            originating_work_assignment_id=exc.originating_work_assignment_id,
            failed_constraint=exc.failed_constraint,
        ).model_dump(exclude_none=True),
    )


def _authorization_denial_to_http(
    exc: DeliverableRepositoryAuthorizationError,
) -> HTTPException:
    """Map :class:`DeliverableRepositoryAuthorizationError` to 403 (AD-WS-9).

    The same mapping covers :class:`WorkAssignmentAssigneeBindingError`
    (a subclass) because both yield the AD-WS-9 indistinguishable
    response shape: only ``generic_denial_indicator``, ``reason_code``,
    and ``correlation_id`` are surfaced (Requirement 30.7 / 38.4).
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=DenialResponseBody(
            reason_code=exc.reason_code,
            correlation_id=exc.correlation_id,
        ).model_dump(),
    )


def _audit_failure_to_http(
    exc: DeliverableRepositoryAuditFailureError,
) -> HTTPException:
    """Map :class:`DeliverableRepositoryAuditFailureError` to 503."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="deliverable_audit_failed",
            message=str(exc),
            audit_failure_indicator="denial_audit_unavailable",
            correlation_id=exc.correlation_id,
        ).model_dump(exclude_none=True),
    )


def _audit_append_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map :class:`AuditAppendError` to 503 (Requirement 26.8 / 37.6)."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="deliverable_audit_failed",
            message=str(exc),
            audit_failure_indicator="consequential_audit_unavailable",
        ).model_dump(exclude_none=True),
    )


def _revision_not_found_to_http(
    exc: DeliverableRevisionNotFoundError,
) -> HTTPException:
    """Map :class:`DeliverableRevisionNotFoundError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error_code="deliverable_revision_not_found",
            message=str(exc),
            deliverable_revision_id=exc.deliverable_revision_id,
        ).model_dump(exclude_none=True),
    )


def _digest_mismatch_to_http(
    exc: DeliverableRevisionDigestMismatchError,
) -> HTTPException:
    """Map :class:`DeliverableRevisionDigestMismatchError` to a 503.

    A digest mismatch indicates database corruption (the AD-WS-27
    UPDATE/DELETE rejection triggers preserve both ``content_bytes``
    and ``content_digest_sha256`` byte-equivalent forever once a
    Revision is inserted). Surfaced as 503 so the operator-facing
    surface differentiates this from a routine 404 / 400.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="deliverable_revision_digest_mismatch",
            message=str(exc),
            deliverable_revision_id=exc.deliverable_revision_id,
        ).model_dump(exclude_none=True),
    )


def _decode_content_bytes(encoded: str) -> bytes:
    """Decode the request's base64-encoded ``content_bytes`` to raw bytes.

    Surfaces a structured 400 if the value is not valid base64 — the
    service-layer validators reject zero-byte and oversize content with
    their own ``failed_constraint`` values, so this helper only catches
    the encoding failure.
    """
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code="deliverable_validation_failed",
                message=(
                    "content_bytes is not valid base64-encoded data: "
                    f"{exc}"
                ),
                failed_constraint="content_bytes_invalid_base64",
            ).model_dump(exclude_none=True),
        ) from exc


# ---------------------------------------------------------------------------
# Shared handler: both POST endpoints invoke the same service path.
#
# Per the design's "Components and Interfaces" table, both
# ``POST /deliverables`` (creates Resource + first Revision) and
# ``POST /deliverables/{deliverable_id}/revisions`` delegate to
# :meth:`DeliverableRepositoryService.create_produced_deliverable`,
# which atomically creates the Deliverable Resource and its first
# Revision in one transaction (Requirement 26.7 / 26.8). The
# ``deliverable_id`` path parameter on the second endpoint is accepted
# for URL routing but is currently informational: the service mints a
# fresh produced Deliverable Resource Identity on every call (the only
# supported create surface in Slice 3). A future ADR will introduce an
# "append-to-existing-Resource" service method when the lifecycle for
# multi-Revision produced Deliverables is added; the route shape will
# not change.
# ---------------------------------------------------------------------------


async def _create_produced_deliverable(
    request: Request,
    ctx: RequestContext,
    service: DeliverableRepositoryService,
) -> CreateDeliverableResponseBody:
    """Shared implementation for both produced-Deliverable creation endpoints.

    Both ``POST /deliverables`` and
    ``POST /deliverables/{deliverable_id}/revisions`` flow through this
    helper:

    1. Read and JSON-decode the request body (400 on empty / malformed).
    2. Screen the raw body against the prohibited planning-attribute
       and observed-outcome prefix sets (Requirements 33.3, 34.2 /
       Property 35 / 36).
    3. Validate against :class:`CreateDeliverableRequestBody`; unknown
       fields are rejected by ``extra='forbid'`` (Property 35).
    4. Decode ``content_bytes`` from base64; surface a 400 on encoding
       errors.
    5. Open the caller's transaction via ``ctx.engine.begin()`` and
       call :meth:`DeliverableRepositoryService.create_produced_deliverable`.
       The Resource header, the Revision row, the two
       ``Identifier_Registry`` rows, and the consequential audit row
       all commit together (Requirement 26.7 / 26.8).
    6. Map every service exception to its HTTP code per design
       §"Error Handling".
    """
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(raw_body)

    try:
        body = CreateDeliverableRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(exc) from exc

    content_bytes = _decode_content_bytes(body.content_bytes)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_produced_deliverable(
                connection,
                content_bytes=content_bytes,
                content_type=body.content_type,
                produced_deliverable_name=body.produced_deliverable_name,
                originating_work_assignment_id=body.originating_work_assignment_id,
                authoring_party_id=ctx.party_id,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except DeliverableContentValidationError as exc:
        raise _content_validation_to_http(exc) from exc
    except WorkAssignmentNotResolvableError as exc:
        raise _work_assignment_not_resolvable_to_http(exc) from exc
    except WorkAssignmentAssigneeBindingError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except DeliverableRepositoryAuditFailureError as exc:
        raise _audit_failure_to_http(exc) from exc
    except DeliverableRepositoryAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return CreateDeliverableResponseBody(
        deliverable_id=result.deliverable_id,
        deliverable_revision_id=result.deliverable_revision_id,
        produced_deliverable_name=result.produced_deliverable_name,
        content_type=result.content_type,
        content_digest_sha256=result.content_digest_sha256,
        content_length_bytes=result.content_length_bytes,
        role_marker=result.role_marker,
        originating_work_assignment_id=result.originating_work_assignment_id,
        authoring_party_id=result.authoring_party_id,
        recorded_at=result.recorded_at,
        correlation_id=result.correlation_id,
    )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/deliverables",
    response_model=CreateDeliverableResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Create a produced Deliverable Resource and its first Revision."
    ),
)
async def create_deliverable(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: DeliverableRepositoryService = Depends(
        get_deliverable_repository_service
    ),
) -> CreateDeliverableResponseBody:
    """Create a produced Deliverable Resource and its first Revision.

    Delegates to :func:`_create_produced_deliverable`; see that helper
    for the full handler flow and error mapping.
    """
    return await _create_produced_deliverable(request, ctx, service)


@router.post(
    "/deliverables/{deliverable_id}/revisions",
    response_model=CreateDeliverableResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Create a produced Deliverable Revision under the path's "
        "Deliverable Resource Identity. Slice 3's combined service "
        "mints a fresh Resource on every call; the path parameter is "
        "currently informational and reserved for a future "
        "append-to-existing-Resource service method."
    ),
)
async def create_deliverable_revision(
    request: Request,
    deliverable_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    service: DeliverableRepositoryService = Depends(
        get_deliverable_repository_service
    ),
) -> CreateDeliverableResponseBody:
    """Create a produced Deliverable Revision (combined Resource+Revision flow).

    Per task 15.2 / orchestrator guidance, this endpoint calls
    :meth:`DeliverableRepositoryService.create_produced_deliverable`,
    the single combined service that creates both the Resource and its
    first Revision in one transaction. The ``deliverable_id`` path
    parameter is accepted for URL routing but is currently
    informational — the service mints a fresh Resource Identity on
    every call (the only supported create surface in Slice 3). The
    response carries the newly minted ``deliverable_id`` and
    ``deliverable_revision_id`` so callers can correlate them with the
    consequential audit row.
    """
    # The ``deliverable_id`` path parameter is accepted at the URL
    # boundary for routing; per the orchestrator note it is informational
    # in Slice 3 (the service does not currently support appending
    # a Revision to an existing Resource and the design pins the
    # service-side identifier minting). A future ADR will introduce
    # the append flow; preserving the parameter in the route signature
    # keeps the URL stable across that change.
    _ = deliverable_id  # documented as accepted but not yet consumed
    return await _create_produced_deliverable(request, ctx, service)


@router.get(
    "/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}",
    response_model=DeliverableRevisionMetadataBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Read produced Deliverable Revision metadata (without "
        "content bytes)."
    ),
)
async def read_deliverable_revision(
    deliverable_id: Annotated[str, Path(min_length=1)],
    deliverable_revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    service: DeliverableRepositoryService = Depends(
        get_deliverable_repository_service
    ),
) -> DeliverableRevisionMetadataBody:
    """Return the metadata for one produced Deliverable Revision.

    Delegates to
    :meth:`DeliverableRepositoryService.get_revision`. The Revision's
    persisted content bytes are NOT loaded by this endpoint so the
    metadata read remains constant-cost regardless of payload size;
    callers needing the bytes invoke the dedicated ``/content``
    endpoint.

    The endpoint additionally rejects requests whose path's
    ``deliverable_id`` does not match the loaded row's
    ``deliverable_id`` (a structured 404 — the requested composite key
    does not resolve to a single Revision). This protects callers
    from accidentally reading a Revision that belongs to a different
    Resource than the URL suggests.
    """
    with ctx.engine.connect() as connection:
        row = service.get_revision(connection, deliverable_revision_id)
    if row is None or row.deliverable_id != deliverable_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="deliverable_revision_not_found",
                message=(
                    f"No Deliverable_Revisions row resolves to "
                    f"deliverable_id={deliverable_id!r} / "
                    f"deliverable_revision_id={deliverable_revision_id!r}."
                ),
                deliverable_id=deliverable_id,
                deliverable_revision_id=deliverable_revision_id,
            ).model_dump(exclude_none=True),
        )
    return DeliverableRevisionMetadataBody(
        deliverable_id=row.deliverable_id,
        deliverable_revision_id=row.deliverable_revision_id,
        content_type=row.content_type,
        content_digest_sha256=row.content_digest_sha256,
        role_marker=row.role_marker,
        originating_work_assignment_id=row.originating_work_assignment_id,
        authoring_party_id=row.authoring_party_id,
        recorded_at=row.recorded_at,
        content_length_bytes=row.content_length_bytes,
    )


# Custom Content-Type response header is computed at request time from
# the persisted ``Deliverable_Revisions.content_type`` column. FastAPI
# does not provide an annotation-driven way to set the response media
# type from a runtime value, so the handler constructs a
# :class:`fastapi.Response` directly with the persisted content type as
# its ``media_type``. This honors the orchestrator's note: "The
# /content endpoint returns the persisted bytes with the correct
# content_type (text/markdown, application/pdf, etc.) per the stored
# content_type column."
_HEADER_CONTENT_DIGEST: Final[str] = "X-Content-Digest-Sha256"
_HEADER_ROLE_MARKER: Final[str] = "X-Deliverable-Role-Marker"


@router.get(
    "/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}/content",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Stream the persisted content bytes of a produced Deliverable "
        "Revision, served with the recorded Content-Type."
    ),
)
async def read_deliverable_revision_content(
    deliverable_id: Annotated[str, Path(min_length=1)],
    deliverable_revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    service: DeliverableRepositoryService = Depends(
        get_deliverable_repository_service
    ),
) -> Response:
    """Return the byte-equivalent content of one produced Deliverable Revision.

    The handler:

    1. Reads the Revision metadata via
       :meth:`DeliverableRepositoryService.get_revision` to validate
       the composite key and learn the persisted content type. A
       missing row or a metadata row whose ``deliverable_id`` does
       not match the path yields a structured 404 (the same shape
       used by the metadata endpoint).
    2. Reads the persisted bytes via
       :meth:`DeliverableRepositoryService.get_revision_text`, which
       verifies the recomputed SHA-256 against the persisted
       ``content_digest_sha256`` (Requirement 35.8 / Property 7). A
       mismatch surfaces as :class:`DeliverableRevisionDigestMismatchError`
       and yields a 503 (database corruption / AD-WS-27 invariant
       violation).
    3. Returns a raw :class:`fastapi.Response` carrying the bytes,
       the persisted content type as ``Content-Type`` (per the
       orchestrator's note), the persisted SHA-256 as
       ``X-Content-Digest-Sha256``, and the role marker as
       ``X-Deliverable-Role-Marker`` for client-side observability.

    The single read transaction (``ctx.engine.connect()``) opens *one*
    read connection that drives both lookups; the AD-WS-27 append-only
    triggers guarantee the metadata and content rows remain
    byte-equivalent across the two reads, but holding a single
    connection avoids cross-snapshot inconsistency on hypothetical
    pathological scheduling.
    """
    with ctx.engine.connect() as connection:
        row = service.get_revision(connection, deliverable_revision_id)
        if row is None or row.deliverable_id != deliverable_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ErrorBody(
                    error_code="deliverable_revision_not_found",
                    message=(
                        f"No Deliverable_Revisions row resolves to "
                        f"deliverable_id={deliverable_id!r} / "
                        f"deliverable_revision_id={deliverable_revision_id!r}."
                    ),
                    deliverable_id=deliverable_id,
                    deliverable_revision_id=deliverable_revision_id,
                ).model_dump(exclude_none=True),
            )

        try:
            content_bytes = service.get_revision_text(
                connection, deliverable_revision_id
            )
        except DeliverableRevisionNotFoundError as exc:
            # Defensive — the metadata read above already confirmed
            # the row exists, but the AD-WS-27 triggers make every
            # row write-once so this branch is unreachable in
            # practice. Surface as a 404 to keep the response shape
            # uniform if it ever fires.
            raise _revision_not_found_to_http(exc) from exc
        except DeliverableRevisionDigestMismatchError as exc:
            raise _digest_mismatch_to_http(exc) from exc

    return Response(
        content=content_bytes,
        media_type=row.content_type,
        headers={
            _HEADER_CONTENT_DIGEST: row.content_digest_sha256,
            _HEADER_ROLE_MARKER: row.role_marker,
        },
        status_code=status.HTTP_200_OK,
    )
