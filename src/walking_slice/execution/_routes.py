"""HTTP routes for the Slice 3 Execution_Service (task 15.1).

Design reference: ``.kiro/specs/third-walking-slice/design.md``
§"Components and Interfaces" and §"Error Handling".

This module exposes a single :class:`fastapi.APIRouter` mounted under
``/api/v1`` that wires every Execution_Service Resource to its HTTP
surface. The endpoint inventory mirrors the design tables exactly:

| Method | Path | Service / function |
|--------|------|---------------------|
| ``POST``  | ``/api/v1/work-assignments`` | :meth:`WorkAssignmentService.create_work_assignment` |
| ``GET``   | ``/api/v1/work-assignments/{work_assignment_id}`` | direct ``Work_Assignment_Records`` read |
| ``POST``  | ``/api/v1/work-events`` | :meth:`WorkEventService.create_work_event` |
| ``GET``   | ``/api/v1/work-events/{work_event_id}`` | direct ``Work_Event_Records`` read |
| ``POST``  | ``/api/v1/time-entries`` | :meth:`TimeEntryService.create_time_entry` |
| ``GET``   | ``/api/v1/time-entries/{time_entry_id}`` | direct ``Time_Entry_Records`` read |
| ``POST``  | ``/api/v1/deliverable-productions`` | :meth:`DeliverableProductionService.create_deliverable_production` |
| ``GET``   | ``/api/v1/deliverable-productions/{deliverable_production_id}`` | direct ``Deliverable_Production_Records`` read |
| ``POST``  | ``/api/v1/milestone-acceptances`` | :meth:`MilestoneAcceptanceService.create_milestone_acceptance` |
| ``GET``   | ``/api/v1/milestone-acceptances/{milestone_acceptance_id}`` | direct ``Milestone_Acceptance_Records`` read |
| ``POST``  | ``/api/v1/completions`` | :meth:`CompletionService.create_completion` |
| ``GET``   | ``/api/v1/completions/{completion_id}`` | direct ``Completion_Records`` read |
| ``GET``   | ``/api/v1/plan-revisions/{plan_revision_id}/execution-status`` | :func:`project_execution_status` |

Responsibilities (per task 15.1):

1. Wire each route through the Slice 1 :class:`RequestContext`
   dependency so the route handler resolves the actor Party Identity
   (``ctx.party_id``), the per-request :class:`Engine`
   (``ctx.engine``), the per-request :class:`Clock` (``ctx.clock``),
   and the correlation handle (``ctx.correlation_id``) from one
   bearer-token-validated bundle.
2. Define Pydantic v2 request models with
   ``ConfigDict(extra='forbid', frozen=True)`` so any typo'd field
   surfaces as a structured 400 rather than being silently dropped.
   Additionally call
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   on the raw request body for every Execution_Service write so
   Property 35 (Plan / Execution separation) and Property 36
   (Output / Outcome separation) are enforced at the API boundary —
   the Pydantic ``extra='forbid'`` guard catches unknown fields the
   validator missed; the prohibited-attribute screen catches
   semantically forbidden names that happen to coincide with declared
   fields.
3. Map every Execution_Service exception to the HTTP code listed in
   design §"Error Handling":

   - ``*ValidationError`` → 400 with ``error_code`` and structured
     body identifying the failed constraint.
   - ``*NotResolvableError`` / ``*NotApprovedError`` /
     ``*ProjectMismatchError`` / state-machine violations / etc. →
     404 or 409 per the design's category mapping.
   - ``*AuthorizationError`` → 403 with the AD-WS-9 indistinguishable
     denial body carrying **only** ``generic_denial_indicator``,
     ``reason_code``, and ``correlation_id``.
   - ``*ConflictError`` (Milestone Acceptance / Completion) → 409.
   - ``*AuditFailureError`` → 503 with the audit-failure indicator
     (matching Slice 1 Requirement 13.6 and Slice 2 Requirement
     10.7).
   - :class:`walking_slice.audit.AuditAppendError` → 503.

Requirements satisfied:
    23.1, 24.1, 25.1, 27.1, 28.1, 29.1 — the six Execution_Service
        Record creation endpoints are mounted at the design's canonical
        paths and delegate the consequential write to the matching
        service inside one ``ctx.engine.begin()`` transaction.
    30.1 — every consequential write resolves the actor Party Identity,
        evaluates authority, and (on deny) emits an AD-WS-9-shaped
        indistinguishable denial response.
    33.4 — request bodies that carry a prohibited planning-attribute
        prefix are rejected with the offending keys identified.
    34.5 — request bodies that carry a prohibited observed-outcome
        prefix are rejected with the offending keys identified.
    39.1 — the execution-status Projection endpoint wraps the projected
        status in a :class:`ProjectionEnvelope` carrying the Projection
        Definition, source Resource / Revision Identities, applicable
        temporal boundary, and generated time.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope; the factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly. Task 15.3 wires the concrete instances through
``walking_slice.app.create_app`` via
:attr:`fastapi.FastAPI.dependency_overrides`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Final, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.app import get_request_context
from walking_slice.audit import AuditAppendError
from walking_slice.auth_middleware import RequestContext
from walking_slice.execution._helpers import (
    ALL_PROHIBITED_PREFIXES,
    ExecutionValidationError,
    _reject_prohibited_attributes,
)
from walking_slice.execution._projection import (
    ExecutionStatusProjection,
    project_execution_status,
)
from walking_slice.execution.completions import (
    CompletionAuditFailureError,
    CompletionAuthorizationError,
    CompletionConflictError,
    CompletionNoAcceptedMilestonesError,
    CompletionPlanRevisionNotApprovedError,
    CompletionPlanRevisionNotResolvableError,
    CompletionService,
    CompletionSourceMilestoneAcceptanceNotResolvableError,
    CompletionValidationError,
)
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionAssigneeBindingError,
    DeliverableProductionAuditFailureError,
    DeliverableProductionAuthorizationError,
    DeliverableProductionDeliverableExpectationNotResolvableError,
    DeliverableProductionDeliverableRevisionNotResolvableError,
    DeliverableProductionOriginatingBindingError,
    DeliverableProductionProjectMismatchError,
    DeliverableProductionService,
    DeliverableProductionValidationError,
    DeliverableProductionWorkAssignmentNotResolvableError,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceAuditFailureError,
    MilestoneAcceptanceAuthorizationError,
    MilestoneAcceptanceConflictError,
    MilestoneAcceptanceProductionNotResolvableError,
    MilestoneAcceptanceProductionRelationshipsCorruptError,
    MilestoneAcceptanceService,
    MilestoneAcceptanceValidationError,
)
from walking_slice.execution.time_entries import (
    TimeEntryAssigneeBindingError,
    TimeEntryAuditFailureError,
    TimeEntryAuthorizationError,
    TimeEntryService,
    TimeEntryValidationError,
    TimeEntryWorkAssignmentNotResolvableError,
)
from walking_slice.execution.work_assignments import (
    WorkAssignmentAssigneeNotResolvableError,
    WorkAssignmentAuditFailureError,
    WorkAssignmentAuthorizationError,
    WorkAssignmentPlanRevisionNotApprovedError,
    WorkAssignmentPlanRevisionNotResolvableError,
    WorkAssignmentPlanRevisionScopeMismatchError,
    WorkAssignmentSelfAssignmentError,
    WorkAssignmentService,
    WorkAssignmentValidationError,
)
from walking_slice.execution.work_events import (
    WorkEventAssigneeBindingError,
    WorkEventAuditFailureError,
    WorkEventAuthorizationError,
    WorkEventNoPriorStartedError,
    WorkEventResumeRequiresPausedError,
    WorkEventService,
    WorkEventStartedAlreadyExistsError,
    WorkEventValidationError,
    WorkEventWorkAssignmentNotResolvableError,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    StatusProjector,
)


__all__ = [
    "AuthorityBasisRequestBody",
    "CreateCompletionRequestBody",
    "CreateDeliverableProductionRequestBody",
    "CreateMilestoneAcceptanceRequestBody",
    "CreateTimeEntryRequestBody",
    "CreateWorkAssignmentRequestBody",
    "CreateWorkEventRequestBody",
    "DenialResponseBody",
    "ErrorBody",
    "get_completion_service",
    "get_deliverable_production_service",
    "get_engine",
    "get_milestone_acceptance_service",
    "get_status_projector",
    "get_time_entry_service",
    "get_work_assignment_service",
    "get_work_event_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["execution"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# Each factory raises :class:`NotImplementedError` until task 15.3 wires
# the concrete instance through ``walking_slice.app.create_app``. Tests
# do the same on their per-test :class:`fastapi.FastAPI` instances. The
# placeholders are named with the same convention used by Slice 1
# routes (``get_engine`` / ``get_<service>``) so the wiring is
# grep-friendly.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    The RequestContext injected through ``ctx.engine`` is the
    authoritative per-request engine handle; this placeholder is
    retained for the GET endpoints that do *not* need the full bundle
    (only the engine, for a single SELECT). The same engine must be
    shared with the RequestContext resolver — task 15.3 enforces this
    by overriding both factories with the same lambda.
    """
    raise NotImplementedError(
        "walking_slice.execution._routes.get_engine must be overridden "
        "by app composition (task 15.3) or test fixtures."
    )


def get_work_assignment_service() -> WorkAssignmentService:
    """Provide the slice's :class:`WorkAssignmentService` singleton."""
    raise NotImplementedError(
        "walking_slice.execution._routes.get_work_assignment_service must "
        "be overridden by app composition (task 15.3) or test fixtures."
    )


def get_work_event_service() -> WorkEventService:
    """Provide the slice's :class:`WorkEventService` singleton."""
    raise NotImplementedError(
        "walking_slice.execution._routes.get_work_event_service must "
        "be overridden by app composition (task 15.3) or test fixtures."
    )


def get_time_entry_service() -> TimeEntryService:
    """Provide the slice's :class:`TimeEntryService` singleton."""
    raise NotImplementedError(
        "walking_slice.execution._routes.get_time_entry_service must "
        "be overridden by app composition (task 15.3) or test fixtures."
    )


def get_deliverable_production_service() -> DeliverableProductionService:
    """Provide the slice's :class:`DeliverableProductionService` singleton."""
    raise NotImplementedError(
        "walking_slice.execution._routes.get_deliverable_production_service "
        "must be overridden by app composition (task 15.3) or test fixtures."
    )


def get_milestone_acceptance_service() -> MilestoneAcceptanceService:
    """Provide the slice's :class:`MilestoneAcceptanceService` singleton."""
    raise NotImplementedError(
        "walking_slice.execution._routes.get_milestone_acceptance_service "
        "must be overridden by app composition (task 15.3) or test fixtures."
    )


def get_completion_service() -> CompletionService:
    """Provide the slice's :class:`CompletionService` singleton."""
    raise NotImplementedError(
        "walking_slice.execution._routes.get_completion_service must "
        "be overridden by app composition (task 15.3) or test fixtures."
    )


def get_status_projector() -> StatusProjector:
    """Provide the slice's :class:`StatusProjector` singleton.

    The same projector is shared with Slice 1 (Trail) and Slice 2
    (Planning) producers; task 15.3 wires one
    :class:`walking_slice.projection.StatusProjector` instance
    registered with every Projection Definition known to any slice.
    """
    raise NotImplementedError(
        "walking_slice.execution._routes.get_status_projector must "
        "be overridden by app composition (task 15.3) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Shared response shells.
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """Structured error envelope used for 400 / 404 / 409 / 503 responses.

    The shape is a superset of the per-route error bodies defined on
    the Slice 1 / Slice 2 routes so a single client-side handler covers
    every Execution_Service endpoint. Fields are optional; only the
    ones relevant to the failure are populated.

    The 403 denial response uses :class:`DenialResponseBody` instead of
    this envelope so the AD-WS-9 indistinguishable shape contains
    **only** ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` (no extra fields that could leak information
    about the target).
    """

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    failed_constraint: Optional[str] = None
    prohibited_keys: list[str] = Field(default_factory=list)
    target_plan_revision_id: Optional[str] = None
    target_work_assignment_id: Optional[str] = None
    target_deliverable_expectation_id: Optional[str] = None
    target_deliverable_expectation_revision_id: Optional[str] = None
    target_deliverable_revision_id: Optional[str] = None
    source_work_assignment_id: Optional[str] = None
    source_deliverable_production_id: Optional[str] = None
    assignee_party_id: Optional[str] = None
    existing_milestone_acceptance_id: Optional[str] = None
    existing_completion_id: Optional[str] = None
    missing_milestone_acceptance_id: Optional[str] = None
    observed_lifecycle_state: Optional[str] = None
    plan_revision_scope: Optional[str] = None
    requested_scope: Optional[str] = None
    audit_failure_indicator: Optional[str] = None
    correlation_id: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


class DenialResponseBody(BaseModel):
    """403 response body for a denied Execution_Service attempt (AD-WS-9).

    The shape is fixed by AD-WS-9 and Requirement 30.4:
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` — nothing else. ``extra='forbid'`` keeps this
    invariant locally: an accidental ``target_plan_revision_id`` or
    ``rationale`` field would surface as a model-validation failure
    (a 500) rather than silently shipping a leak.

    The ``reason_code`` enumeration mirrors Slice 1 Requirement 7.2 and
    Slice 3 Requirement 30.4 — ``no-role-assignment`` is used for both
    the authority-evaluation deny path and the AD-WS-29 assignee-binding
    failure path so the response is indistinguishable between the two
    causes.
    """

    model_config = ConfigDict(extra="forbid")

    generic_denial_indicator: Literal["denied"] = "denied"
    reason_code: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Shared request body building blocks.
# ---------------------------------------------------------------------------


_AUTHORITY_BASIS_TYPE_LITERAL = Literal[
    "role-grant-id", "scope-id", "delegation-chain-id"
]

# Closed enumerations mirrored from the design's per-Record sections
# and the schema CHECK constraints. Centralizing them here gives the
# Pydantic request models a single source of truth.
_WORK_EVENT_KIND_LITERAL = Literal[
    "started", "progress_note", "paused", "resumed", "deliverable_drafted"
]
_MILESTONE_OUTCOME_LITERAL = Literal["Accept", "Reject"]
_COMPLETION_OUTCOME_LITERAL = Literal["Completed", "Completed_With_Reservation"]


class AuthorityBasisRequestBody(BaseModel):
    """Authority basis sub-object on every Execution_Service request body.

    Mirrors :class:`walking_slice.models.AuthorityBasisRef`. AD-WS-31
    reuses the Slice 1 enumeration unchanged. The Pydantic ``id`` is
    typed as :class:`UUID` so the wire contract is unambiguous (a UUID
    string) while still being persisted as plain text on the
    service-layer columns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: _AUTHORITY_BASIS_TYPE_LITERAL
    id: UUID


# ---------------------------------------------------------------------------
# Per-Resource request body models.
# ---------------------------------------------------------------------------


class CreateWorkAssignmentRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/work-assignments``.

    The Assignment Authority Party Identity is *not* carried on the
    request body; it is sourced from the bearer-token-resolved
    :class:`RequestContext` (``ctx.party_id``) so a caller cannot
    impersonate a different Assignment Authority. The self-assignment
    guard then runs at the service layer against
    ``ctx.party_id != assignee_party_id`` (Requirement 23.5).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_plan_revision_id: str = Field(min_length=1)
    assignee_party_id: str = Field(min_length=1)
    assignment_rationale: Optional[str] = Field(default=None, max_length=4_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreateWorkEventRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/work-events``.

    The recording Contributor Party Identity is sourced from
    ``ctx.party_id``; the AD-WS-29 assignee-binding check then enforces
    ``ctx.party_id == work_assignment.assignee_party_id`` at the
    service layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_work_assignment_id: str = Field(min_length=1)
    event_kind: _WORK_EVENT_KIND_LITERAL
    event_note: Optional[str] = Field(default=None, max_length=4_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreateTimeEntryRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/time-entries``.

    ``effort_hours`` is accepted as a JSON string so the Decimal
    normalization happens at the service layer (Requirement 25.2 — the
    decimal value is normalized to two-fractional-digit form before
    persistence). Accepting it as a string also keeps the JSON wire
    representation explicit and avoids float-precision drift on
    intermediate decoders.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_work_assignment_id: str = Field(min_length=1)
    effort_hours: str = Field(min_length=1, max_length=16)
    effort_period_start: datetime
    effort_period_end: datetime
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreateDeliverableProductionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/deliverable-productions``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_work_assignment_id: str = Field(min_length=1)
    produced_deliverable_revision_id: str = Field(min_length=1)
    target_deliverable_expectation_revision_id: str = Field(min_length=1)
    production_rationale: Optional[str] = Field(default=None, max_length=4_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreateMilestoneAcceptanceRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/milestone-acceptances``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_deliverable_production_id: str = Field(min_length=1)
    outcome: _MILESTONE_OUTCOME_LITERAL
    rationale: str = Field(min_length=1, max_length=4_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreateCompletionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/completions``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_plan_revision_id: str = Field(min_length=1)
    outcome: _COMPLETION_OUTCOME_LITERAL
    rationale: str = Field(min_length=1, max_length=4_000)
    source_milestone_acceptance_ids: list[str] = Field(
        default_factory=list, max_length=1_000
    )
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Request-body helpers.
# ---------------------------------------------------------------------------


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Read and JSON-decode the request body, returning a dict.

    Empty bodies and non-object bodies surface as a structured 400.
    The returned dict is the raw input shape — we pass it both to
    Pydantic for declared-field validation and to
    :func:`_reject_prohibited_attributes` (via
    :func:`_screen_prohibited_attributes`) for the prohibited-key
    screen so unknown-but-prohibited fields are rejected even if
    Pydantic's ``extra='forbid'`` already covers the structural case.
    The double-screen is intentional: the Pydantic guard catches typos
    at the declared-field layer; the prohibited-attribute guard catches
    semantically forbidden names.
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


def _validation_error_to_http(
    exc: ValidationError, *, error_code: str
) -> HTTPException:
    """Convert a Pydantic :class:`ValidationError` to a 400 ``HTTPException``.

    Field names from ``loc`` are joined with ``.`` for nested fields
    (e.g. ``authority_basis.type``). Errors of type ``missing`` /
    ``missing_argument`` are surfaced on the ``missing`` list; every
    other error lands in ``validation_errors`` with the JSON-safe
    subset of the error dict (the ``ctx`` field is dropped because
    Pydantic v2 attaches the original exception object there for
    ``value_error`` failures, which breaks JSON encoding).
    """
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
            error_code=error_code,
            message="Request failed validation.",
            missing=sorted(set(missing)),
            validation_errors=other,
        ).model_dump(exclude_none=True),
    )


def _screen_prohibited_attributes(
    body: dict[str, Any],
    *,
    error_code: str,
    prefixes: tuple[str, ...] = ALL_PROHIBITED_PREFIXES,
) -> None:
    """Reject the request body when any top-level key matches ``prefixes``.

    Wraps :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
    so an :class:`ExecutionValidationError` becomes a structured 400 at
    the API boundary (Property 35, Property 36). Every Execution_Service
    write invokes this helper before the Pydantic model is constructed
    so the prohibited-attribute error takes precedence over the
    declared-field error — the more specific failure wins, matching the
    per-service validators that run inside each
    ``create_*`` call as a defense in depth.
    """
    try:
        _reject_prohibited_attributes(body, prefixes)
    except ExecutionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code=error_code,
                message=str(exc),
                failed_constraint="prohibited_attribute",
                prohibited_keys=list(exc.prohibited_keys),
            ).model_dump(exclude_none=True),
        ) from exc


# ---------------------------------------------------------------------------
# Error mappers — one per exception family. Every Execution_Service
# mirror of a Slice 1 / Slice 2 error follows the same mapping so the
# wire contract is uniform across slices.
# ---------------------------------------------------------------------------


def _validation_to_http(
    exc: Exception,
    *,
    error_code: str,
) -> HTTPException:
    """Map an Execution_Service ``*ValidationError`` to a structured 400.

    Every Execution_Service validation error exposes a
    ``failed_constraint`` attribute (stable enumerated identifier) and,
    for prohibited-attribute failures, a ``prohibited_keys`` tuple.
    Both surface verbatim on the response so the client sees a stable
    code rather than a free-form message.
    """
    failed_constraint = getattr(exc, "failed_constraint", None)
    prohibited_keys = getattr(exc, "prohibited_keys", ())
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error_code=error_code,
            message=str(exc),
            failed_constraint=failed_constraint,
            prohibited_keys=list(prohibited_keys),
        ).model_dump(exclude_none=True),
    )


def _not_resolvable_to_http(
    exc: Exception,
    *,
    error_code: str,
    extra: Optional[dict[str, Any]] = None,
) -> HTTPException:
    """Map an Execution_Service ``*NotResolvableError`` (or equivalent) to a 404.

    The body carries the supplied ``error_code`` and any additional
    identifiers provided by the caller in ``extra``. The
    Execution_Service not-resolvable errors store the failing identity
    on a typed attribute (e.g. ``target_plan_revision_id``); the route
    layer reads that attribute by name in the call site rather than via
    introspection.
    """
    payload: dict[str, Any] = {
        "error_code": error_code,
        "message": str(exc),
    }
    if extra:
        payload.update(extra)
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(**payload).model_dump(exclude_none=True),
    )


def _conflict_to_http(
    exc: Exception,
    *,
    error_code: str,
    extra: Optional[dict[str, Any]] = None,
) -> HTTPException:
    """Map a 409-class Execution_Service exception to a structured 409.

    Used for state-machine violations (Work Events) and uniqueness
    conflicts (Milestone Acceptance / Completion). The body carries the
    supplied ``error_code`` plus any extra identifiers the caller wants
    to surface — for example the existing ``milestone_acceptance_id``
    when the caller holds view authority on it (Slice 3 Requirement
    30.4).
    """
    payload: dict[str, Any] = {
        "error_code": error_code,
        "message": str(exc),
    }
    if extra:
        payload.update(extra)
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ErrorBody(**payload).model_dump(exclude_none=True),
    )


def _authorization_denial_to_http(exc: Exception) -> HTTPException:
    """Map an Execution_Service ``*AuthorizationError`` to a 403 (AD-WS-9).

    Per AD-WS-9 the body carries **only** the three fields and the
    fixed ``"denied"`` indicator — no information about *what* was
    being attempted, *which* role assignment was missing, or *whether*
    the target exists is leaked. The exception exposes ``reason_code``
    and ``correlation_id`` on typed attributes; we surface both
    verbatim. The dedicated :class:`DenialResponseBody` enforces the
    three-field-only shape via ``extra='forbid'`` so an accidental
    field addition surfaces as a Pydantic failure rather than silently
    shipping a leak.

    The same handler covers the AD-WS-29 assignee-binding denial
    subclasses (``WorkEventAssigneeBindingError``,
    ``TimeEntryAssigneeBindingError``,
    ``DeliverableProductionAssigneeBindingError``) so the response
    shape is indistinguishable between the authority-evaluation deny
    path and the assignee-binding failure path (Requirement 30.4).
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=DenialResponseBody(
            reason_code=getattr(exc, "reason_code", "denied"),
            correlation_id=getattr(exc, "correlation_id", ""),
        ).model_dump(),
    )


def _audit_failure_to_http(
    exc: Exception,
    *,
    error_code: str,
) -> HTTPException:
    """Map an Execution_Service ``*AuditFailureError`` to a 503.

    Per design §"Error Handling" rule 7 (and Slice 1 Requirement 13.6
    / Slice 3 Requirement 30.6), audit-append failures roll back the
    originating transaction and surface as ``HTTP 503`` with the
    ``audit_failure_indicator`` flag set so the operator-facing surface
    can differentiate this from a routine deny. The response
    intentionally does *not* carry the AD-WS-9 denial shape — the 503
    is operator-facing and the operator needs the audit-failure
    signal.
    """
    correlation_id = getattr(exc, "correlation_id", None)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code=error_code,
            message=str(exc),
            audit_failure_indicator="denial_audit_unavailable",
            correlation_id=correlation_id,
        ).model_dump(exclude_none=True),
    )


def _audit_append_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map :class:`AuditAppendError` to a 503 response.

    Audit append failures roll back the surrounding transaction (the
    Record row, the consequential Relationship rows, and the
    consequential audit row are discarded). The 503 status code matches
    the contract used in every other route module so a single
    client-side handler covers every consequential write.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="audit_append_failed",
            message=str(exc),
        ).model_dump(exclude_none=True),
    )


def _authority_basis_to_ref(body: AuthorityBasisRequestBody) -> AuthorityBasisRef:
    """Convert the request-body sub-object to the slice-wide value object.

    The Pydantic request model carries ``id`` as :class:`UUID` for
    wire-format clarity; the service layer accepts the Slice 1
    :class:`AuthorityBasisRef` whose ``id`` is also a :class:`UUID`.
    Centralizing the conversion keeps the per-route handlers narrowly
    focused on orchestration.
    """
    return AuthorityBasisRef(type=body.type, id=body.id)


def _decode_json_array(raw: Any) -> list[Any]:
    """Decode a JSON array column to a Python list.

    ``Completion_Records.source_milestone_acceptance_ids_json`` is
    persisted as a canonical JSON string (the service serializes empty
    lists as ``"[]"`` so the column is always well-defined). Defence-in-
    depth: a malformed value surfaces as an empty list rather than an
    opaque 500, mirroring the convention used by the Slice 2 planning
    route module.
    """
    if not isinstance(raw, str):
        return []
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return decoded


# ---------------------------------------------------------------------------
# Work Assignments endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/work-assignments",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Work Assignment Record (Assignment Authority).",
)
async def create_work_assignment(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: WorkAssignmentService = Depends(get_work_assignment_service),
) -> dict[str, Any]:
    """Create an immutable Work Assignment Record (Requirement 23.1).

    Delegates the consequential write to
    :meth:`WorkAssignmentService.create_work_assignment` inside one
    ``ctx.engine.begin()`` transaction so the Work Assignment Record,
    the ``Addresses`` Relationship, the ``Relates To`` Relationship,
    the Identifier_Registry binding, and the consequential audit row
    commit together (AD-WS-5, Requirement 23.8).
    """
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="work_assignment_validation_failed"
    )
    try:
        body = CreateWorkAssignmentRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="work_assignment_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_work_assignment(
                connection,
                target_plan_revision_id=body.target_plan_revision_id,
                assignee_party_id=body.assignee_party_id,
                assignment_authority_party_id=ctx.party_id,
                assignment_rationale=body.assignment_rationale,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except WorkAssignmentAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except WorkAssignmentAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="work_assignment_audit_failed"
        ) from exc
    except WorkAssignmentSelfAssignmentError as exc:
        raise _validation_to_http(
            exc, error_code="self_assignment_not_permitted"
        ) from exc
    except WorkAssignmentPlanRevisionNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_resolvable",
            extra={"target_plan_revision_id": exc.target_plan_revision_id},
        ) from exc
    except WorkAssignmentPlanRevisionNotApprovedError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_approved",
            extra={
                "target_plan_revision_id": exc.target_plan_revision_id,
                "observed_lifecycle_state": exc.observed_lifecycle_state,
            },
        ) from exc
    except WorkAssignmentPlanRevisionScopeMismatchError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_resolvable",
            extra={
                "target_plan_revision_id": exc.target_plan_revision_id,
                "plan_revision_scope": exc.plan_revision_scope,
                "requested_scope": exc.requested_scope,
            },
        ) from exc
    except WorkAssignmentAssigneeNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="assignee_party_not_resolvable",
            extra={"assignee_party_id": exc.assignee_party_id},
        ) from exc
    except WorkAssignmentValidationError as exc:
        raise _validation_to_http(
            exc, error_code="work_assignment_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "work_assignment_id": result.work_assignment_id,
        "target_plan_revision_id": result.target_plan_revision_id,
        "assignee_party_id": result.assignee_party_id,
        "assignment_authority_party_id": result.assignment_authority_party_id,
        "assignment_rationale": result.assignment_rationale,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "relates_to_relationship_id": result.relates_to_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/work-assignments/{work_assignment_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Work Assignment Record.",
)
async def read_work_assignment(
    work_assignment_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Work Assignment Record matching ``work_assignment_id``.

    Work Assignment Records are Immutable Records (AD-WS-27) — the
    Record row is the single authoritative entry and there are no
    Revisions; the GET path therefore takes only the Record Identity.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT work_assignment_id, target_plan_revision_id,
                           assignee_party_id, assignment_authority_party_id,
                           assignment_rationale, authority_basis_type,
                           authority_basis_id, applicable_scope, recorded_at
                    FROM Work_Assignment_Records
                    WHERE work_assignment_id = :work_assignment_id
                    """
                ),
                {"work_assignment_id": work_assignment_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="target_work_assignment_not_resolvable",
                message=(
                    f"No Work_Assignment_Records row for work_assignment_id="
                    f"{work_assignment_id!r}."
                ),
                target_work_assignment_id=work_assignment_id,
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body


# ---------------------------------------------------------------------------
# Work Events endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/work-events",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Work Event Record (assigned Contributor).",
)
async def create_work_event(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: WorkEventService = Depends(get_work_event_service),
) -> dict[str, Any]:
    """Create an immutable Work Event Record (Requirement 24.1).

    The recording Contributor Party Identity is sourced from
    ``ctx.party_id``. The AD-WS-29 assignee-binding check runs at the
    service layer; on mismatch the response uses
    ``reason_code='no-role-assignment'`` so the wire shape is
    indistinguishable from the authority-evaluation deny path
    (Requirement 30.4).
    """
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="work_event_validation_failed"
    )
    try:
        body = CreateWorkEventRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="work_event_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_work_event(
                connection,
                target_work_assignment_id=body.target_work_assignment_id,
                event_kind=body.event_kind,
                event_note=body.event_note,
                recording_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except WorkEventAssigneeBindingError as exc:
        # AD-WS-29 assignee-binding failure; ``reason_code`` is fixed
        # at ``"no-role-assignment"`` by the service layer so the
        # response is indistinguishable from the authority deny path.
        raise _authorization_denial_to_http(exc) from exc
    except WorkEventAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except WorkEventAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="work_event_audit_failed"
        ) from exc
    except WorkEventWorkAssignmentNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_work_assignment_not_resolvable",
            extra={"target_work_assignment_id": exc.target_work_assignment_id},
        ) from exc
    except WorkEventStartedAlreadyExistsError as exc:
        raise _conflict_to_http(
            exc,
            error_code="work_event_started_already_exists",
            extra={
                "target_work_assignment_id": getattr(
                    exc, "target_work_assignment_id", body.target_work_assignment_id
                )
            },
        ) from exc
    except WorkEventNoPriorStartedError as exc:
        raise _conflict_to_http(
            exc,
            error_code="work_event_started_required",
            extra={
                "target_work_assignment_id": getattr(
                    exc, "target_work_assignment_id", body.target_work_assignment_id
                )
            },
        ) from exc
    except WorkEventResumeRequiresPausedError as exc:
        raise _conflict_to_http(
            exc,
            error_code="work_event_resume_requires_paused",
            extra={
                "target_work_assignment_id": getattr(
                    exc, "target_work_assignment_id", body.target_work_assignment_id
                )
            },
        ) from exc
    except WorkEventValidationError as exc:
        raise _validation_to_http(
            exc, error_code="work_event_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "work_event_id": result.work_event_id,
        "target_work_assignment_id": result.target_work_assignment_id,
        "event_kind": result.event_kind,
        "event_note": result.event_note,
        "recording_party_id": result.recording_party_id,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "relates_to_relationship_id": result.relates_to_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/work-events/{work_event_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Work Event Record.",
)
async def read_work_event(
    work_event_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Work Event Record matching ``work_event_id``."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT work_event_id, target_work_assignment_id,
                           event_kind, event_note, recording_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Work_Event_Records
                    WHERE work_event_id = :work_event_id
                    """
                ),
                {"work_event_id": work_event_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="work_event_not_found",
                message=(
                    f"No Work_Event_Records row for work_event_id="
                    f"{work_event_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body


# ---------------------------------------------------------------------------
# Time Entries endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/time-entries",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Time Entry Record (assigned Contributor).",
)
async def create_time_entry(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: TimeEntryService = Depends(get_time_entry_service),
) -> dict[str, Any]:
    """Create an immutable Time Entry Record (Requirement 25.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="time_entry_validation_failed"
    )
    try:
        body = CreateTimeEntryRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="time_entry_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_time_entry(
                connection,
                target_work_assignment_id=body.target_work_assignment_id,
                effort_hours=body.effort_hours,
                effort_period_start=body.effort_period_start,
                effort_period_end=body.effort_period_end,
                recording_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except TimeEntryAssigneeBindingError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except TimeEntryAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except TimeEntryAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="time_entry_audit_failed"
        ) from exc
    except TimeEntryWorkAssignmentNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_work_assignment_not_resolvable",
            extra={"target_work_assignment_id": exc.target_work_assignment_id},
        ) from exc
    except TimeEntryValidationError as exc:
        raise _validation_to_http(
            exc, error_code="time_entry_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "time_entry_id": result.time_entry_id,
        "target_work_assignment_id": result.target_work_assignment_id,
        "effort_hours": str(result.effort_hours),
        "effort_period_start": result.effort_period_start,
        "effort_period_end": result.effort_period_end,
        "recording_party_id": result.recording_party_id,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "relates_to_relationship_id": result.relates_to_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/time-entries/{time_entry_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Time Entry Record.",
)
async def read_time_entry(
    time_entry_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Time Entry Record matching ``time_entry_id``."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT time_entry_id, target_work_assignment_id,
                           effort_hours, effort_period_start, effort_period_end,
                           recording_party_id, authority_basis_type,
                           authority_basis_id, applicable_scope, recorded_at
                    FROM Time_Entry_Records
                    WHERE time_entry_id = :time_entry_id
                    """
                ),
                {"time_entry_id": time_entry_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="time_entry_not_found",
                message=(
                    f"No Time_Entry_Records row for time_entry_id="
                    f"{time_entry_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body


# ---------------------------------------------------------------------------
# Deliverable Productions endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/deliverable-productions",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Deliverable Production Record (assigned Contributor).",
)
async def create_deliverable_production(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: DeliverableProductionService = Depends(
        get_deliverable_production_service
    ),
) -> dict[str, Any]:
    """Create an immutable Deliverable Production Record (Requirement 27.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="deliverable_production_validation_failed"
    )
    try:
        body = CreateDeliverableProductionRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="deliverable_production_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_deliverable_production(
                connection,
                source_work_assignment_id=body.source_work_assignment_id,
                produced_deliverable_revision_id=(
                    body.produced_deliverable_revision_id
                ),
                target_deliverable_expectation_revision_id=(
                    body.target_deliverable_expectation_revision_id
                ),
                production_rationale=body.production_rationale,
                recording_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except DeliverableProductionAssigneeBindingError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except DeliverableProductionAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except DeliverableProductionAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="deliverable_production_audit_failed"
        ) from exc
    except DeliverableProductionWorkAssignmentNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_work_assignment_not_resolvable",
            extra={
                "source_work_assignment_id": getattr(
                    exc, "source_work_assignment_id", body.source_work_assignment_id
                )
            },
        ) from exc
    except DeliverableProductionDeliverableRevisionNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_deliverable_revision_not_resolvable",
            extra={
                "target_deliverable_revision_id": getattr(
                    exc,
                    "produced_deliverable_revision_id",
                    body.produced_deliverable_revision_id,
                )
            },
        ) from exc
    except DeliverableProductionDeliverableExpectationNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_deliverable_expectation_not_resolvable",
            extra={
                "target_deliverable_expectation_revision_id": getattr(
                    exc,
                    "target_deliverable_expectation_revision_id",
                    body.target_deliverable_expectation_revision_id,
                )
            },
        ) from exc
    except DeliverableProductionOriginatingBindingError as exc:
        raise _validation_to_http(
            exc, error_code="deliverable_production_validation_failed"
        ) from exc
    except DeliverableProductionProjectMismatchError as exc:
        raise _validation_to_http(
            exc, error_code="deliverable_expectation_project_mismatch"
        ) from exc
    except DeliverableProductionValidationError as exc:
        raise _validation_to_http(
            exc, error_code="deliverable_production_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "deliverable_production_id": result.deliverable_production_id,
        "source_work_assignment_id": result.source_work_assignment_id,
        "produced_deliverable_id": result.produced_deliverable_id,
        "produced_deliverable_revision_id": result.produced_deliverable_revision_id,
        "target_deliverable_expectation_id": result.target_deliverable_expectation_id,
        "target_deliverable_expectation_revision_id": (
            result.target_deliverable_expectation_revision_id
        ),
        "production_rationale": result.production_rationale,
        "recording_party_id": result.recording_party_id,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "produces_relationship_id": result.produces_relationship_id,
        "addresses_relationship_id": result.addresses_relationship_id,
        "relates_to_relationship_id": result.relates_to_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/deliverable-productions/{deliverable_production_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Deliverable Production Record.",
)
async def read_deliverable_production(
    deliverable_production_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Deliverable Production Record matching the identifier."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT deliverable_production_id, source_work_assignment_id,
                           produced_deliverable_id, produced_deliverable_revision_id,
                           target_deliverable_expectation_id,
                           target_deliverable_expectation_revision_id,
                           production_rationale, recording_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Deliverable_Production_Records
                    WHERE deliverable_production_id = :deliverable_production_id
                    """
                ),
                {"deliverable_production_id": deliverable_production_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="target_deliverable_production_not_resolvable",
                message=(
                    f"No Deliverable_Production_Records row for "
                    f"deliverable_production_id={deliverable_production_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body


# ---------------------------------------------------------------------------
# Milestone Acceptances endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/milestone-acceptances",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Milestone Acceptance Record (Milestone Acceptance Authority).",
)
async def create_milestone_acceptance(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: MilestoneAcceptanceService = Depends(get_milestone_acceptance_service),
) -> dict[str, Any]:
    """Create an immutable Milestone Acceptance Record (Requirement 28.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="milestone_acceptance_validation_failed"
    )
    try:
        body = CreateMilestoneAcceptanceRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="milestone_acceptance_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_milestone_acceptance(
                connection,
                source_deliverable_production_id=(
                    body.source_deliverable_production_id
                ),
                outcome=body.outcome,
                rationale=body.rationale,
                accepting_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except MilestoneAcceptanceAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except MilestoneAcceptanceAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="milestone_acceptance_audit_failed"
        ) from exc
    except MilestoneAcceptanceProductionNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_deliverable_production_not_resolvable",
            extra={
                "source_deliverable_production_id": getattr(
                    exc,
                    "source_deliverable_production_id",
                    body.source_deliverable_production_id,
                )
            },
        ) from exc
    except MilestoneAcceptanceProductionRelationshipsCorruptError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_deliverable_production_not_resolvable",
            extra={
                "source_deliverable_production_id": getattr(
                    exc,
                    "source_deliverable_production_id",
                    body.source_deliverable_production_id,
                )
            },
        ) from exc
    except MilestoneAcceptanceConflictError as exc:
        raise _conflict_to_http(
            exc,
            error_code="milestone_acceptance_already_exists",
            extra={
                "source_deliverable_production_id": getattr(
                    exc,
                    "source_deliverable_production_id",
                    body.source_deliverable_production_id,
                ),
                "existing_milestone_acceptance_id": getattr(
                    exc, "existing_milestone_acceptance_id", None
                ),
            },
        ) from exc
    except MilestoneAcceptanceValidationError as exc:
        raise _validation_to_http(
            exc, error_code="milestone_acceptance_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "milestone_acceptance_id": result.milestone_acceptance_id,
        "source_deliverable_production_id": result.source_deliverable_production_id,
        "produced_deliverable_id": result.produced_deliverable_id,
        "produced_deliverable_revision_id": result.produced_deliverable_revision_id,
        "target_deliverable_expectation_id": result.target_deliverable_expectation_id,
        "target_deliverable_expectation_revision_id": (
            result.target_deliverable_expectation_revision_id
        ),
        "outcome": result.outcome,
        "rationale": result.rationale,
        "accepting_party_id": result.accepting_party_id,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/milestone-acceptances/{milestone_acceptance_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Milestone Acceptance Record.",
)
async def read_milestone_acceptance(
    milestone_acceptance_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Milestone Acceptance Record matching the identifier."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT milestone_acceptance_id,
                           source_deliverable_production_id,
                           produced_deliverable_id,
                           produced_deliverable_revision_id,
                           target_deliverable_expectation_id,
                           target_deliverable_expectation_revision_id,
                           outcome, rationale, accepting_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Milestone_Acceptance_Records
                    WHERE milestone_acceptance_id = :milestone_acceptance_id
                    """
                ),
                {"milestone_acceptance_id": milestone_acceptance_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="milestone_acceptance_not_found",
                message=(
                    f"No Milestone_Acceptance_Records row for "
                    f"milestone_acceptance_id={milestone_acceptance_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body


# ---------------------------------------------------------------------------
# Completions endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/completions",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Completion Record (Completion Authority).",
)
async def create_completion(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: CompletionService = Depends(get_completion_service),
) -> dict[str, Any]:
    """Create an immutable Completion Record (Requirement 29.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="completion_validation_failed"
    )
    try:
        body = CreateCompletionRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="completion_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_completion(
                connection,
                target_plan_revision_id=body.target_plan_revision_id,
                outcome=body.outcome,
                rationale=body.rationale,
                source_milestone_acceptance_ids=tuple(
                    body.source_milestone_acceptance_ids
                ),
                completing_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except CompletionAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except CompletionAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="completion_audit_failed"
        ) from exc
    except CompletionPlanRevisionNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_resolvable",
            extra={
                "target_plan_revision_id": getattr(
                    exc, "target_plan_revision_id", body.target_plan_revision_id
                )
            },
        ) from exc
    except CompletionPlanRevisionNotApprovedError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_approved",
            extra={
                "target_plan_revision_id": getattr(
                    exc, "target_plan_revision_id", body.target_plan_revision_id
                ),
                "observed_lifecycle_state": getattr(
                    exc, "observed_lifecycle_state", None
                ),
            },
        ) from exc
    except CompletionNoAcceptedMilestonesError as exc:
        raise _validation_to_http(
            exc, error_code="completion_validation_failed"
        ) from exc
    except CompletionSourceMilestoneAcceptanceNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="completion_validation_failed",
            extra={
                "missing_milestone_acceptance_id": getattr(
                    exc, "missing_milestone_acceptance_id", None
                )
            },
        ) from exc
    except CompletionConflictError as exc:
        raise _conflict_to_http(
            exc,
            error_code="completion_already_exists",
            extra={
                "target_plan_revision_id": getattr(
                    exc, "target_plan_revision_id", body.target_plan_revision_id
                ),
                "existing_completion_id": getattr(
                    exc, "existing_completion_id", None
                ),
            },
        ) from exc
    except CompletionValidationError as exc:
        raise _validation_to_http(
            exc, error_code="completion_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "completion_id": result.completion_id,
        "target_plan_revision_id": result.target_plan_revision_id,
        "target_activity_plan_id": result.target_activity_plan_id,
        "target_project_id": result.target_project_id,
        "outcome": result.outcome,
        "rationale": result.rationale,
        "source_milestone_acceptance_ids": list(
            result.source_milestone_acceptance_ids
        ),
        "completing_party_id": result.completing_party_id,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/completions/{completion_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Completion Record.",
)
async def read_completion(
    completion_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Completion Record matching ``completion_id``.

    ``source_milestone_acceptance_ids_json`` is persisted as a JSON
    array; the response decodes it so clients see a structured list
    rather than the raw string.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT completion_id, target_plan_revision_id,
                           target_activity_plan_id, target_project_id,
                           outcome, rationale,
                           source_milestone_acceptance_ids_json,
                           completing_party_id, authority_basis_type,
                           authority_basis_id, applicable_scope, recorded_at
                    FROM Completion_Records
                    WHERE completion_id = :completion_id
                    """
                ),
                {"completion_id": completion_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="target_completion_not_resolvable",
                message=(
                    f"No Completion_Records row for completion_id="
                    f"{completion_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    body["source_milestone_acceptance_ids"] = _decode_json_array(
        body.pop("source_milestone_acceptance_ids_json")
    )
    return body


# ---------------------------------------------------------------------------
# Execution-status Projection endpoint.
# ---------------------------------------------------------------------------


@router.get(
    "/plan-revisions/{plan_revision_id}/execution-status",
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Return the execution-status Projection for a Plan Revision, "
        "wrapped in a ProjectionEnvelope."
    ),
)
async def get_execution_status(
    plan_revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    projector: StatusProjector = Depends(get_status_projector),
) -> dict[str, Any]:
    """Return the projected execution status of a Plan Revision (Requirement 39).

    Delegates to :func:`project_execution_status` which walks the Plan
    Revision → Work Assignment → Work Event → Deliverable Production →
    Milestone Acceptance → Completion chain in one read-only pass and
    returns either:

    - an :class:`ExecutionStatusProjection` carrying the derived status
      label and a populated :class:`ProjectionEnvelope` (Requirement
      39.1, 39.2), or
    - on the missing-source-Record path, the same
      :class:`ExecutionStatusProjection` with
      :attr:`projected_status` set to ``"Provenance incomplete"`` and
      :attr:`explanation_unavailable` populated (Requirement 39.5), or
    - on the unresolvable-Projection-Definition path, an
      :class:`ExplanationUnavailableResponse` identifying the missing
      definition.

    Per Requirement 39.3 the response carries only the projected status
    label and the envelope — no percent-complete, actual-cost,
    remaining-work, budget-variance, forecast-cost, or
    outcome-attainment value. Per Requirement 39.6 the projection is
    labelled as a projection of *work performed*, never as evidence of
    an Observed Outcome.
    """
    with ctx.engine.connect() as connection:
        result = project_execution_status(
            connection,
            plan_revision_id=plan_revision_id,
            party_id=ctx.party_id,
            at=ctx.clock.now(),
            status_projector=projector,
        )
    return _execution_status_response_to_body(result, plan_revision_id)


def _execution_status_response_to_body(
    result: Any,
    requested_plan_revision_id: str,
) -> dict[str, Any]:
    """Serialize an :class:`ExecutionStatusResponse` to a JSON-safe dict.

    Discriminates on the runtime type returned by
    :func:`project_execution_status`:

    - :class:`ExecutionStatusProjection` → ``{plan_revision_id,
      projected_status, envelope, explanation_unavailable?}``. The
      envelope is rendered through :func:`_envelope_to_body` so the
      :class:`ProjectionEnvelope` validators' canonical timestamp
      strings travel verbatim on the wire.
    - :class:`ExplanationUnavailableResponse` → the Slice 1 envelope
      withholding shape carrying ``missing_element_kind`` and
      ``missing_element_identifier``; the requested Plan Revision
      Identity is echoed for correlation.
    """
    if isinstance(result, ExecutionStatusProjection):
        body: dict[str, Any] = {
            "plan_revision_id": result.plan_revision_id,
            "projected_status": result.projected_status,
            "envelope": _envelope_to_body(result.envelope),
        }
        if result.explanation_unavailable is not None:
            body["explanation_unavailable"] = _explanation_unavailable_to_body(
                result.explanation_unavailable
            )
        return body
    if isinstance(result, ExplanationUnavailableResponse):
        return {
            "plan_revision_id": requested_plan_revision_id,
            "projected_status": None,
            "envelope": None,
            "explanation_unavailable": _explanation_unavailable_to_body(result),
        }
    # Defensive — the projector never returns any other shape.
    raise HTTPException(  # pragma: no cover - defensive
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=ErrorBody(
            error_code="execution_status_response_invalid",
            message=(
                "project_execution_status returned an unexpected response "
                f"shape: {type(result).__name__}."
            ),
        ).model_dump(exclude_none=True),
    )


def _envelope_to_body(envelope: Any) -> dict[str, Any]:
    """Render a :class:`ProjectionEnvelope` as a JSON-safe dict.

    Stringifies every :class:`UUID` and ISO-formats every
    :class:`datetime` so the response is directly serializable by
    FastAPI's default JSON encoder. The envelope's ``derivation``
    indicator is fixed at ``"derived"`` (Requirement 14.2) and is
    surfaced verbatim.
    """
    return {
        "definition": {
            "name": envelope.definition.name,
            "version": envelope.definition.version,
        },
        "source_resource_ids": [str(uid) for uid in envelope.source_resource_ids],
        "source_revision_ids": [str(uid) for uid in envelope.source_revision_ids],
        "applicable_temporal_boundary": (
            envelope.applicable_temporal_boundary.isoformat()
        ),
        "generated_at": envelope.generated_at.isoformat(),
        "derivation": envelope.derivation,
    }


def _explanation_unavailable_to_body(
    response: ExplanationUnavailableResponse,
) -> dict[str, Any]:
    """Render an :class:`ExplanationUnavailableResponse` as a JSON-safe dict.

    Per Requirement 39.5 the indicator names the unresolvable element
    so the caller can re-issue the request once the missing source is
    recorded. The shape mirrors the Slice 1 Trail / Slice 2 Planning
    explanation-unavailable wrappers so a single client-side handler
    covers all three.
    """
    return {
        "missing_element_kind": response.missing_element_kind,
        "missing_element_identifier": response.missing_element_identifier,
    }


# ---------------------------------------------------------------------------
# Defensive imports for typing-only references that would otherwise be
# tree-shaken from the import graph.
# ---------------------------------------------------------------------------

_FINAL_TYPING_HINT: Final[type[Exception]] = AuditAppendError
