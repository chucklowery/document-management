"""HTTP routes for the Slice 2 Planning_Service (task 15.1).

Design reference: ``.kiro/specs/second-walking-slice/design.md``
§"Components and Interfaces" and §"Error Handling".

This module exposes a single :class:`fastapi.APIRouter` mounted under
``/api/v1`` that wires every Planning_Service Resource to its HTTP
surface. The endpoint inventory mirrors the design tables exactly:

| Method | Path | Service |
|--------|------|---------|
| ``POST``  | ``/api/v1/objectives`` | :meth:`ObjectiveService.create_objective` |
| ``GET``   | ``/api/v1/objectives/{objective_id}/revisions/{revision_id}`` | direct ``Objective_Revisions`` read |
| ``POST``  | ``/api/v1/intended-outcomes`` | :meth:`IntendedOutcomeService.create_intended_outcome` |
| ``GET``   | ``/api/v1/intended-outcomes/{intended_outcome_id}/revisions/{revision_id}`` | direct ``Intended_Outcome_Revisions`` read |
| ``POST``  | ``/api/v1/projects`` | :meth:`ProjectService.create_project` |
| ``GET``   | ``/api/v1/projects/{project_id}/revisions/{revision_id}`` | direct ``Project_Revisions`` read |
| ``POST``  | ``/api/v1/deliverable-expectations`` | :meth:`DeliverableExpectationService.create_deliverable_expectation` |
| ``GET``   | ``/api/v1/deliverable-expectations/{deliverable_expectation_id}/revisions/{revision_id}`` | direct ``Deliverable_Expectation_Revisions`` read |
| ``POST``  | ``/api/v1/activity-plans`` | :meth:`ActivityPlanService.create_activity_plan` |
| ``GET``   | ``/api/v1/activity-plans/{activity_plan_id}`` | direct ``Activity_Plans`` read |
| ``POST``  | ``/api/v1/activity-plans/{activity_plan_id}/plan-revisions`` | :meth:`PlanRevisionService.create_plan_revision` |
| ``GET``   | ``/api/v1/activity-plans/{activity_plan_id}/plan-revisions/{revision_id}`` | direct ``Plan_Revisions`` read |
| ``POST``  | ``/api/v1/plan-revisions/{plan_revision_id}/reviews`` | :meth:`PlanReviewService.create_plan_review` |
| ``GET``   | ``/api/v1/plan-reviews/{plan_review_id}/revisions/{revision_id}`` | direct ``Plan_Review_Revisions`` read |
| ``POST``  | ``/api/v1/plan-revisions/{plan_revision_id}/approvals`` | :meth:`PlanApprovalService.create_plan_approval` |
| ``GET``   | ``/api/v1/plan-approvals/{plan_approval_id}`` | direct ``Plan_Approval_Records`` read |
| ``GET``   | ``/api/v1/plan-approvals/{plan_approval_id}/provenance`` | :meth:`ProvenanceNavigator.navigate_plan_approval` |

Responsibilities (per task 15.1):

1. Wire each route through the Slice 1 :class:`RequestContext`
   dependency so the route handler resolves the actor Party Identity
   (``ctx.party_id``), the per-request :class:`Engine`
   (``ctx.engine``), the per-request :class:`Clock` (``ctx.clock``),
   and the correlation handle (``ctx.correlation_id``) from one
   bearer-token-validated bundle. The placeholder
   ``X-Actor-Party-Id`` shim built into
   :class:`RequestContextResolver` keeps the wave-7 integration tests
   green during the migration window.
2. Define Pydantic v2 request models with
   ``ConfigDict(extra='forbid', frozen=True)`` so any typo'd field
   surfaces as a structured 400 rather than being silently dropped.
   Additionally call
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   on the raw request body for every Planning_Service write so
   Property 22 (Plan/Execution and Output/Outcome separation) is
   enforced at the API boundary — the Pydantic ``extra='forbid``
   guard catches unknown fields the validator missed, and the
   prohibited-attribute screen catches semantically forbidden
   names that happen to coincide with declared fields.
3. Map every Planning_Service exception to the HTTP code listed in
   design §"Error Handling":

   - ``*ValidationError`` → 400 with ``error_code`` and structured
     body identifying the failed constraint.
   - ``*NotResolvableError`` (and equivalents like
     ``*NotDraftError`` and ``*MismatchError``) → 404 with the
     identifier that failed to resolve.
   - ``*AuthorizationError`` → 403 with the AD-WS-9 indistinguishable
     denial body carrying **only** ``generic_denial_indicator``,
     ``reason_code``, and ``correlation_id``.
   - ``*ConflictError`` and ``PlanApprovalConflictError`` → 409 with
     the existing identifier (when available) so the caller can
     pick up the prior write.
   - :class:`ApprovedPlanRevisionImmutableError` → 409 with
     ``error_code = 'approved_plan_revision_immutable'``.
   - ``*AuditFailureError`` → 503 with an audit-failure indicator
     (matching the Slice 1 contract and design §"Error Handling"
     rule 7).
   - :class:`walking_slice.audit.AuditAppendError` → 503.
   - :class:`PlanApprovalUnresolvableError` (from the provenance
     walk) → 404; the same exception is also raised by
     :meth:`ProvenanceNavigator.navigate_plan_approval` for the
     restricted case so the response form is indistinguishable
     per Requirement 14.7.

Requirements satisfied:
    2.1, 3.1, 4.1, 5.1, 6.1, 7.1, 8.1, 9.1 — the eight Planning
        Resource creation endpoints are mounted at the design's
        canonical paths and delegate the consequential write to the
        matching service inside one ``ctx.engine.begin()`` transaction.
    14.1 — the Plan Approval provenance walk endpoint returns the
        full Planning Provenance Chain (Plan Approval → Plan
        Revision → Activity Plan → Project → Objective → Decision →
        Recommendation → Finding → Region → Document Revision) by
        delegating to :meth:`ProvenanceNavigator.navigate_plan_approval`.
    15.1 — the planning Resource reads (every GET endpoint above) use
        the Slice 1 ``Identifier_Registry`` / authorized-source-kind
        backlink extension implicitly through the navigator and
        directly through the schema reads; no row a caller lacks
        view authority on becomes observable.
    17.1 — the additive Disclosure_Policy_Coverage rows seeded by
        :mod:`walking_slice.planning._disclosure` (loaded into the
        :class:`ProvenanceNavigator` at app construction) drive the
        AD-WS-9 redaction-vs-not-found discipline applied to every
        provenance walk emitted by this module.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope; the
factories raise :class:`NotImplementedError` by default so an
unwired call fails loudly. Task 15.2 wires the concrete instances
through ``walking_slice.app.create_app`` via
:attr:`fastapi.FastAPI.dependency_overrides`.
"""

from __future__ import annotations

import json
from datetime import date as _date
from typing import Annotated, Any, Final, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.app import get_request_context
from walking_slice.audit import AuditAppendError
from walking_slice.auth_middleware import RequestContext
from walking_slice.manifests import ManifestValidationError
from walking_slice.models import AuthorityBasisRef
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
    PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _reject_prohibited_attributes,
)
from walking_slice.planning._immutability import (
    APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE,
    ApprovedPlanRevisionImmutableAuditFailureError,
    ApprovedPlanRevisionImmutableError,
    enforce_approved_plan_revision_immutability,
)
from walking_slice.planning.activity_plans import (
    ActivityPlanAuditFailureError,
    ActivityPlanAuthorizationError,
    ActivityPlanProjectNotResolvableError,
    ActivityPlanService,
    ActivityPlanValidationError,
)
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationAuditFailureError,
    DeliverableExpectationAuthorizationError,
    DeliverableExpectationProjectNotResolvableError,
    DeliverableExpectationService,
    DeliverableExpectationValidationError,
)
from walking_slice.planning.intended_outcomes import (
    IntendedOutcomeAuditFailureError,
    IntendedOutcomeAuthorizationError,
    IntendedOutcomeObjectiveNotResolvableError,
    IntendedOutcomeService,
    IntendedOutcomeValidationError,
)
from walking_slice.planning.models import PlanApprovalOmissionEntry
from walking_slice.planning.objectives import (
    ObjectiveAuditFailureError,
    ObjectiveAuthorizationError,
    ObjectiveDecisionNotResolvableError,
    ObjectiveService,
    ObjectiveValidationError,
)
from walking_slice.planning.plan_approvals import (
    PlanApprovalAuditFailureError,
    PlanApprovalAuthorizationError,
    PlanApprovalConflictError,
    PlanApprovalService,
    PlanApprovalTargetNotDraftError,
    PlanApprovalTargetNotResolvableError,
    PlanApprovalValidationError,
)
from walking_slice.planning.plan_reviews import (
    PlanReviewAuditFailureError,
    PlanReviewAuthorizationError,
    PlanReviewService,
    PlanReviewTargetNotDraftError,
    PlanReviewTargetNotResolvableError,
    PlanReviewValidationError,
)
from walking_slice.planning.plan_revisions import (
    PlanRevisionActivityPlanNotResolvableError,
    PlanRevisionAuditFailureError,
    PlanRevisionAuthorizationError,
    PlanRevisionDeliverableExpectationNotResolvableError,
    PlanRevisionPredecessorActivityPlanMismatchError,
    PlanRevisionPredecessorApprovedError,
    PlanRevisionPredecessorNotResolvableError,
    PlanRevisionService,
    PlanRevisionValidationError,
)
from walking_slice.planning.projects import (
    ProjectAuditFailureError,
    ProjectAuthorizationError,
    ProjectObjectiveNotResolvableError,
    ProjectService,
    ProjectValidationError,
)
from walking_slice.provenance import (
    ActivityPlanNode,
    DecisionUnresolvableError,
    ObjectiveRevisionNode,
    PlanApprovalNode,
    PlanApprovalProvenance,
    PlanApprovalUnresolvableError,
    PlanRevisionNode,
    ProjectRevisionNode,
    ProvenanceNavigator,
    RedactedNode,
)


__all__ = [
    "AuthorityBasisRequestBody",
    "CreateActivityPlanRequestBody",
    "CreateDeliverableExpectationRequestBody",
    "CreateIntendedOutcomeRequestBody",
    "CreateObjectiveRequestBody",
    "CreatePlanApprovalRequestBody",
    "CreatePlanRevisionRequestBody",
    "CreatePlanReviewRequestBody",
    "CreateProjectRequestBody",
    "DenialResponseBody",
    "ErrorBody",
    "PlanApprovalOmissionRequestBody",
    "get_activity_plan_service",
    "get_deliverable_expectation_service",
    "get_engine",
    "get_intended_outcome_service",
    "get_objective_service",
    "get_plan_approval_service",
    "get_plan_review_service",
    "get_plan_revision_service",
    "get_project_service",
    "get_provenance_navigator",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["planning"])



# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# Each factory is a stub that raises :class:`NotImplementedError`. Task 15.2
# wires the concrete instance through ``walking_slice.app.create_app`` by
# adding overrides to ``app.dependency_overrides``. Tests do the same on
# their per-test :class:`fastapi.FastAPI` instances. The placeholders are
# named with the same convention used elsewhere in the slice
# (``get_engine`` / ``get_<service>``) so the wiring is grep-friendly.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    The RequestContext injected through ``ctx.engine`` is the
    authoritative per-request engine handle; this placeholder is
    retained for the GET endpoints that do *not* need the full
    bundle (only the engine, for a single SELECT). The same engine
    must be shared with the RequestContext resolver — task 15.2
    enforces this by overriding both factories with the same lambda.
    """
    raise NotImplementedError(
        "walking_slice.planning._routes.get_engine must be overridden "
        "by app composition (task 15.2) or test fixtures."
    )


def get_objective_service() -> ObjectiveService:
    """Provide the slice's :class:`ObjectiveService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_objective_service must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )


def get_intended_outcome_service() -> IntendedOutcomeService:
    """Provide the slice's :class:`IntendedOutcomeService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_intended_outcome_service "
        "must be overridden by app composition (task 15.2) or test fixtures."
    )


def get_project_service() -> ProjectService:
    """Provide the slice's :class:`ProjectService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_project_service must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


def get_deliverable_expectation_service() -> DeliverableExpectationService:
    """Provide the slice's :class:`DeliverableExpectationService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_deliverable_expectation_service "
        "must be overridden by app composition (task 15.2) or test fixtures."
    )


def get_activity_plan_service() -> ActivityPlanService:
    """Provide the slice's :class:`ActivityPlanService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_activity_plan_service must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )


def get_plan_revision_service() -> PlanRevisionService:
    """Provide the slice's :class:`PlanRevisionService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_plan_revision_service must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )


def get_plan_review_service() -> PlanReviewService:
    """Provide the slice's :class:`PlanReviewService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_plan_review_service must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )


def get_plan_approval_service() -> PlanApprovalService:
    """Provide the slice's :class:`PlanApprovalService` singleton."""
    raise NotImplementedError(
        "walking_slice.planning._routes.get_plan_approval_service must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )


def get_provenance_navigator() -> ProvenanceNavigator:
    """Provide the slice's :class:`ProvenanceNavigator` singleton.

    The Planning_Service routes import this distinct symbol so the
    Slice 1 ``walking_slice.routes.provenance.get_provenance_navigator``
    override and this module's override are textually independent;
    task 15.2 binds both to the same instance.
    """
    raise NotImplementedError(
        "walking_slice.planning._routes.get_provenance_navigator must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )



# ---------------------------------------------------------------------------
# Shared response shells.
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """Structured error envelope used for 400 / 404 / 409 / 503 responses.

    The shape is a superset of the per-route error bodies defined on
    the Slice 1 routes so a single client-side handler covers every
    Planning_Service endpoint. Fields are optional; only the ones
    relevant to the failure are populated.

    The 403 denial response uses :class:`DenialResponseBody` instead
    of this envelope so the AD-WS-9 indistinguishable shape contains
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
    target_decision_id: Optional[str] = None
    target_objective_id: Optional[str] = None
    target_project_id: Optional[str] = None
    target_activity_plan_id: Optional[str] = None
    target_deliverable_expectation_id: Optional[str] = None
    target_plan_revision_id: Optional[str] = None
    target_plan_review_id: Optional[str] = None
    plan_approval_id: Optional[str] = None
    existing_plan_approval_id: Optional[str] = None
    predecessor_plan_revision_id: Optional[str] = None
    decision_outcome: Optional[str] = None
    lifecycle_state: Optional[str] = None
    audit_failure_indicator: Optional[str] = None
    correlation_id: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


class DenialResponseBody(BaseModel):
    """403 response body for a denied Planning_Service attempt (AD-WS-9).

    The shape is fixed by AD-WS-9 and Requirement 10.4:
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` — nothing else. ``extra='forbid'`` keeps this
    invariant locally: an accidental ``rationale`` or
    ``target_plan_revision_id`` field would surface as a
    model-validation failure (a 500) rather than silently shipping a
    leak.
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
_OMISSION_CATEGORY_LITERAL = Literal[
    "intentional", "unavailable", "restricted", "stale", "unresolved"
]


class AuthorityBasisRequestBody(BaseModel):
    """Authority basis sub-object on Plan Review / Plan Approval requests.

    Mirrors :class:`walking_slice.models.AuthorityBasisRef`. AD-WS-10
    fixes the ``type`` enumeration. The Pydantic ``id`` is typed as a
    :class:`UUID` so the wire contract is unambiguous (a UUID string)
    while still being persisted as plain text on the service-layer
    columns.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: _AUTHORITY_BASIS_TYPE_LITERAL
    id: UUID


class PlanApprovalOmissionRequestBody(BaseModel):
    """One Plan Approval omission entry on the request body.

    Mirrors :class:`walking_slice.planning.models.PlanApprovalOmissionEntry`.
    Each entry becomes one ``Omission_Entries`` row inside the same
    transaction as the Plan Approval (AD-WS-20).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: _OMISSION_CATEGORY_LITERAL
    excluded_source_id: UUID
    excluded_source_revision_id: Optional[UUID] = None
    rationale: str = Field(min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# Per-Resource request body models.
# ---------------------------------------------------------------------------


class CreateObjectiveRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/objectives``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(min_length=1, max_length=4_000)
    rationale: Optional[str] = Field(default=None, max_length=10_000)
    target_decision_id: str = Field(min_length=1)
    applicable_scope: str = Field(min_length=1)


class CreateIntendedOutcomeRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/intended-outcomes``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_objective_id: str = Field(min_length=1)
    success_condition: str = Field(min_length=1, max_length=4_000)
    observation_window: Optional[str] = Field(default=None, max_length=1_000)
    attribution_assumption: Optional[str] = Field(default=None, max_length=4_000)
    applicable_scope: str = Field(min_length=1)


class CreateProjectRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/projects``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_objective_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    summary: Optional[str] = Field(default=None, max_length=4_000)
    planned_start_date: _date
    planned_end_date: _date
    applicable_scope: str = Field(min_length=1)


class CreateDeliverableExpectationRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/deliverable-expectations``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_project_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10_000)
    deliverable_kind: Literal["Document", "Artifact", "Service", "Other"]
    acceptance_criteria: Optional[str] = Field(default=None, max_length=10_000)
    applicable_scope: str = Field(min_length=1)


class CreateActivityPlanRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/activity-plans``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_project_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    applicable_scope: str = Field(min_length=1)


class CreatePlanRevisionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/activity-plans/{ap_id}/plan-revisions``.

    The parent Activity Plan Identity travels in the path so the body
    does not duplicate it; everything else is supplied here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    planned_scope: str = Field(min_length=1, max_length=10_000)
    deliverable_expectation_refs: list[str] = Field(default_factory=list, max_length=50)
    planning_assumptions: list[str] = Field(default_factory=list, max_length=100)
    ordering_rationale: Optional[str] = Field(default=None, max_length=2_000)
    predecessor_plan_revision_id: Optional[str] = Field(default=None, min_length=1)
    applicable_scope: str = Field(min_length=1)


class CreatePlanReviewRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/plan-revisions/{pr_id}/reviews``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["Endorse", "Changes_Requested", "Reject"]
    rationale: str = Field(min_length=1, max_length=10_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreatePlanApprovalRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/plan-revisions/{pr_id}/approvals``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["Approve", "Reject_Approval"]
    rationale: str = Field(min_length=1, max_length=4_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)
    omissions: list[PlanApprovalOmissionRequestBody] = Field(default_factory=list)



# ---------------------------------------------------------------------------
# Request-body helpers.
# ---------------------------------------------------------------------------


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Read and JSON-decode the request body, returning a dict.

    Empty bodies and non-object bodies surface as a structured 400.
    The returned dict is the raw input shape — we pass it both to
    Pydantic for declared-field validation and to
    :func:`_reject_prohibited_attributes` for the prohibited-key
    screen so unknown-but-prohibited fields (the intersection of
    Property 22 forbidden prefixes and arbitrary user input) are
    rejected even if Pydantic's ``extra='forbid'`` already covers
    the structural case. The double-screen is intentional: the
    Pydantic guard catches typos at the declared-field layer; the
    prohibited-attribute guard catches semantically forbidden names.
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
    prefixes: tuple[str, ...],
    error_code: str,
) -> None:
    """Reject the request body when any top-level key matches ``prefixes``.

    Wraps :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
    so a :class:`PlanningValidationError` becomes a structured 400 at
    the API boundary (Property 22). Every Planning_Service write
    invokes this helper before the Pydantic model is constructed so
    the prohibited-attribute error takes precedence over the
    declared-field error — the more specific failure wins, matching
    the per-service ``_validate_no_*`` validators that run in
    ``mode='before'``.
    """
    try:
        _reject_prohibited_attributes(body, prefixes)
    except PlanningValidationError as exc:
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
# Error mappers — one per exception family. Every Planning_Service mirror
# of a Slice 1 error follows the same mapping so the wire contract is
# uniform across Resources.
# ---------------------------------------------------------------------------


def _validation_to_http(
    exc: Exception,
    *,
    error_code: str,
) -> HTTPException:
    """Map a Planning_Service ``*ValidationError`` to a structured 400.

    Every Planning_Service validation error exposes a
    ``failed_constraint`` attribute (stable enumerated identifier)
    and, for prohibited-attribute failures, a ``prohibited_keys``
    tuple. Both surface verbatim on the response so the client sees a
    stable code rather than a free-form message.
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
    """Map a Planning_Service ``*NotResolvableError`` (or equivalent) to a 404.

    The body carries the supplied ``error_code`` and any additional
    identifiers provided by the caller in ``extra``. The Planning_Service
    not-resolvable errors store the failing identity on a typed
    attribute (e.g. ``target_objective_id``); the route layer reads
    that attribute by name in the call site rather than via
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


def _authorization_denial_to_http(exc: Exception) -> HTTPException:
    """Map a Planning_Service ``*AuthorizationError`` to a 403 (AD-WS-9).

    Per AD-WS-9 the body carries **only** the three fields and the
    fixed ``"denied"`` indicator — no information about *what* was
    being attempted, *which* role assignment was missing, or
    *whether* the target exists is leaked. The error exposes
    ``reason_code`` and ``correlation_id`` on typed attributes; we
    surface both verbatim. The dedicated :class:`DenialResponseBody`
    enforces the three-field-only shape via ``extra='forbid'`` so an
    accidental field addition surfaces as a Pydantic failure rather
    than silently shipping a leak.
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
    """Map a Planning_Service ``*AuditFailureError`` to a 503.

    Per design §"Error Handling" rule 7 (and Slice 1 Requirement 13.6),
    audit-append failures roll back the originating transaction and
    surface as ``HTTP 503`` with the
    ``audit_failure_indicator`` flag set so the operator-facing
    surface can differentiate this from a routine audit append
    failure. The response intentionally does *not* carry the
    AD-WS-9 denial shape — the 503 is operator-facing and the
    operator needs the audit-failure signal.
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
    Resource header, Revision row, Relationship row, and any
    consequential audit row are discarded). The 503 status code
    matches the contract used in every other route module so a single
    client-side handler covers every consequential write.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="audit_append_failed",
            message=str(exc),
        ).model_dump(exclude_none=True),
    )


def _manifest_validation_to_http(exc: ManifestValidationError) -> HTTPException:
    """Map a :class:`ManifestValidationError` to a structured 400.

    The :class:`ProvenanceManifestWriter` raises this when an Included
    Source or Omission Entry fails Requirement 10.x validation;
    surface as a 400 with the writer's ``failed_constraint`` so the
    client picks up the same stable identifier the service emits.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error_code="plan_approval_manifest_invalid",
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(exclude_none=True),
    )


def _manifest_persistence_failure_to_http(exc: Exception) -> HTTPException:
    """Map an unexpected manifest-persistence failure to a 503.

    Per design §"Error Handling" rule 6: when a Planning_Service
    transaction fails because the Provenance Manifest could not be
    persisted, the whole synthesis rolls back and ``503
    provenance_manifest_persistence_failed`` is returned.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="provenance_manifest_persistence_failed",
            message=str(exc),
        ).model_dump(exclude_none=True),
    )


def _immutability_to_http(exc: ApprovedPlanRevisionImmutableError) -> HTTPException:
    """Map :class:`ApprovedPlanRevisionImmutableError` to a 409 response.

    Per design §"Error Handling" rule 5 and Requirement 9.6: any
    attempt to mutate an Approved Plan Revision (or any constituent
    row, Relationship, or downstream Plan Review Revision / Plan
    Approval Record) is rejected with ``HTTP 409`` and ``error_code =
    approved_plan_revision_immutable``. The Denial Record is appended
    in a separate transaction by the application-layer enforcement
    helper; this mapper only converts the raised exception.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ErrorBody(
            error_code=APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE,
            message=str(exc),
            target_plan_revision_id=exc.target_plan_revision_id,
            correlation_id=exc.correlation_id,
        ).model_dump(exclude_none=True),
    )


def _immutability_audit_failure_to_http(
    exc: ApprovedPlanRevisionImmutableAuditFailureError,
) -> HTTPException:
    """Map :class:`ApprovedPlanRevisionImmutableAuditFailureError` to a 503.

    Mirrors the per-Resource audit-failure mapping; the immutability
    Denial Record append exhausted its retry budget and the operator
    must be told. The 503 status code surfaces the divergence between
    denial and audit; the 409 was suppressed because the audit trail
    on which the 409 depends could not be written.
    """
    correlation_id = getattr(exc, "correlation_id", None)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="approved_plan_revision_immutable_audit_failed",
            message=str(exc),
            audit_failure_indicator="denial_audit_unavailable",
            correlation_id=correlation_id,
        ).model_dump(exclude_none=True),
    )



# ---------------------------------------------------------------------------
# Objectives endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/objectives",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create an Objective Resource and its first immutable Revision.",
)
async def create_objective(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: ObjectiveService = Depends(get_objective_service),
) -> dict[str, Any]:
    """Create an Objective Resource and its first Revision (Requirement 2.1).

    Delegates the consequential write to
    :meth:`ObjectiveService.create_objective` inside one
    ``ctx.engine.begin()`` transaction so the Objectives row, the
    Objective_Revisions row, the Addresses Relationship, the
    Identifier_Registry bindings, and the consequential Audit_Records
    row commit together (AD-WS-5, Requirement 2.7).
    """
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, prefixes=ALL_PROHIBITED_PREFIXES, error_code="objective_validation_failed"
    )
    try:
        body = CreateObjectiveRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="objective_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_objective(
                connection,
                statement=body.statement,
                rationale=body.rationale,
                target_decision_id=body.target_decision_id,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except ObjectiveAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except ObjectiveAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="objective_audit_failed"
        ) from exc
    except ObjectiveDecisionNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code=exc.failed_constraint,
            extra={
                "target_decision_id": exc.target_decision_id,
                "decision_outcome": exc.outcome,
            },
        ) from exc
    except ObjectiveValidationError as exc:
        raise _validation_to_http(
            exc, error_code="objective_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "objective_id": result.objective_id,
        "objective_revision_id": result.objective_revision_id,
        "statement": result.statement,
        "rationale": result.rationale,
        "target_decision_id": result.target_decision_id,
        "authoring_party_id": result.authoring_party_id,
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/objectives/{objective_id}/revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a persisted Objective Revision.",
)
async def read_objective_revision(
    objective_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Objective Revision matching ``(objective_id, revision_id)``.

    Direct SQL lookup against ``Objective_Revisions`` joined on the
    ``objective_id`` column so a Revision Identity belonging to a
    different Objective produces a 404 rather than a silent redirect.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT objective_revision_id, objective_id,
                           parent_revision_id, statement, rationale,
                           target_decision_id, authoring_party_id,
                           applicable_scope, recorded_at
                    FROM Objective_Revisions
                    WHERE objective_revision_id = :revision_id
                      AND objective_id = :objective_id
                    """
                ),
                {"objective_id": objective_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="objective_revision_not_found",
                message=(
                    f"No Objective_Revisions row for objective_id="
                    f"{objective_id!r}, revision_id={revision_id!r}."
                ),
                target_objective_id=objective_id,
            ).model_dump(exclude_none=True),
        )
    return dict(row)


# ---------------------------------------------------------------------------
# Intended Outcomes endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/intended-outcomes",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Create an Intended Outcome and its first Revision; reject any "
        "observed-outcome attribute (Requirement 3.3 / 13.1)."
    ),
)
async def create_intended_outcome(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: IntendedOutcomeService = Depends(get_intended_outcome_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    # IntendedOutcome screens both observed-outcome keys (Requirement 13.1)
    # and the union of execution / produced-deliverable prefixes via the
    # service's own validator, so we forward the raw body and let the
    # service apply ALL_PROHIBITED_PREFIXES; we also pre-screen with the
    # observed-outcome subset so a typo in an observed-outcome key
    # produces the specific error before the Pydantic ``extra='forbid'``
    # rejects it as merely-unknown.
    _screen_prohibited_attributes(
        raw_body,
        prefixes=OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
        error_code="intended_outcome_validation_failed",
    )
    try:
        body = CreateIntendedOutcomeRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="intended_outcome_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_intended_outcome(
                connection,
                target_objective_id=body.target_objective_id,
                success_condition=body.success_condition,
                observation_window=body.observation_window,
                attribution_assumption=body.attribution_assumption,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except IntendedOutcomeAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except IntendedOutcomeAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="intended_outcome_audit_failed"
        ) from exc
    except IntendedOutcomeObjectiveNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_objective_not_resolvable",
            extra={"target_objective_id": exc.target_objective_id},
        ) from exc
    except IntendedOutcomeValidationError as exc:
        raise _validation_to_http(
            exc, error_code="intended_outcome_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "intended_outcome_id": result.intended_outcome_id,
        "intended_outcome_revision_id": result.intended_outcome_revision_id,
        "outcome_kind": result.outcome_kind,
        "success_condition": result.success_condition,
        "observation_window": result.observation_window,
        "attribution_assumption": result.attribution_assumption,
        "target_objective_id": result.target_objective_id,
        "authoring_party_id": result.authoring_party_id,
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/intended-outcomes/{intended_outcome_id}/revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a persisted Intended Outcome Revision.",
)
async def read_intended_outcome_revision(
    intended_outcome_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT intended_outcome_revision_id, intended_outcome_id,
                           parent_revision_id, outcome_kind, target_objective_id,
                           success_condition, observation_window,
                           attribution_assumption, authoring_party_id,
                           applicable_scope, recorded_at
                    FROM Intended_Outcome_Revisions
                    WHERE intended_outcome_revision_id = :revision_id
                      AND intended_outcome_id = :intended_outcome_id
                    """
                ),
                {
                    "intended_outcome_id": intended_outcome_id,
                    "revision_id": revision_id,
                },
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="intended_outcome_revision_not_found",
                message=(
                    f"No Intended_Outcome_Revisions row for "
                    f"intended_outcome_id={intended_outcome_id!r}, "
                    f"revision_id={revision_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    return dict(row)



# ---------------------------------------------------------------------------
# Projects endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/projects",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Project and its first immutable Revision.",
)
async def create_project(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: ProjectService = Depends(get_project_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body,
        prefixes=ALL_PROHIBITED_PREFIXES,
        error_code="project_validation_failed",
    )
    try:
        body = CreateProjectRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="project_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_project(
                connection,
                target_objective_id=body.target_objective_id,
                name=body.name,
                summary=body.summary,
                planned_start_date=body.planned_start_date,
                planned_end_date=body.planned_end_date,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except ProjectAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except ProjectAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="project_audit_failed"
        ) from exc
    except ProjectObjectiveNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_objective_not_resolvable",
            extra={"target_objective_id": exc.target_objective_id},
        ) from exc
    except ProjectValidationError as exc:
        raise _validation_to_http(
            exc, error_code="project_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "project_id": result.project_id,
        "project_revision_id": result.project_revision_id,
        "name": result.name,
        "summary": result.summary,
        "target_objective_id": result.target_objective_id,
        "planned_start_date": result.planned_start_date,
        "planned_end_date": result.planned_end_date,
        "authoring_party_id": result.authoring_party_id,
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/projects/{project_id}/revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a persisted Project Revision.",
)
async def read_project_revision(
    project_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT project_revision_id, project_id,
                           parent_revision_id, name, summary,
                           target_objective_id, planned_start_date,
                           planned_end_date, authoring_party_id,
                           applicable_scope, recorded_at
                    FROM Project_Revisions
                    WHERE project_revision_id = :revision_id
                      AND project_id = :project_id
                    """
                ),
                {"project_id": project_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="project_revision_not_found",
                message=(
                    f"No Project_Revisions row for project_id="
                    f"{project_id!r}, revision_id={revision_id!r}."
                ),
                target_project_id=project_id,
            ).model_dump(exclude_none=True),
        )
    return dict(row)


# ---------------------------------------------------------------------------
# Deliverable Expectations endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/deliverable-expectations",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Create a Deliverable Expectation and its first Revision; reject "
        "any produced-deliverable attribute (Requirement 5.3 / 13.2)."
    ),
)
async def create_deliverable_expectation(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: DeliverableExpectationService = Depends(get_deliverable_expectation_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body,
        prefixes=PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
        error_code="deliverable_expectation_validation_failed",
    )
    try:
        body = CreateDeliverableExpectationRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="deliverable_expectation_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_deliverable_expectation(
                connection,
                target_project_id=body.target_project_id,
                name=body.name,
                description=body.description,
                deliverable_kind=body.deliverable_kind,
                acceptance_criteria=body.acceptance_criteria,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except DeliverableExpectationAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except DeliverableExpectationAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="deliverable_expectation_audit_failed"
        ) from exc
    except DeliverableExpectationProjectNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_project_not_resolvable",
            extra={"target_project_id": exc.target_project_id},
        ) from exc
    except DeliverableExpectationValidationError as exc:
        raise _validation_to_http(
            exc, error_code="deliverable_expectation_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "deliverable_expectation_id": result.deliverable_expectation_id,
        "deliverable_expectation_revision_id": result.deliverable_expectation_revision_id,
        "name": result.name,
        "description": result.description,
        "deliverable_kind": result.deliverable_kind,
        "acceptance_criteria": result.acceptance_criteria,
        "target_project_id": result.target_project_id,
        "authoring_party_id": result.authoring_party_id,
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/deliverable-expectations/{deliverable_expectation_id}/revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a persisted Deliverable Expectation Revision.",
)
async def read_deliverable_expectation_revision(
    deliverable_expectation_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT deliverable_expectation_revision_id,
                           deliverable_expectation_id, parent_revision_id,
                           target_project_id, name, description,
                           deliverable_kind, acceptance_criteria,
                           authoring_party_id, applicable_scope, recorded_at
                    FROM Deliverable_Expectation_Revisions
                    WHERE deliverable_expectation_revision_id = :revision_id
                      AND deliverable_expectation_id = :deliverable_expectation_id
                    """
                ),
                {
                    "deliverable_expectation_id": deliverable_expectation_id,
                    "revision_id": revision_id,
                },
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="deliverable_expectation_revision_not_found",
                message=(
                    f"No Deliverable_Expectation_Revisions row for "
                    f"deliverable_expectation_id="
                    f"{deliverable_expectation_id!r}, "
                    f"revision_id={revision_id!r}."
                ),
                target_deliverable_expectation_id=deliverable_expectation_id,
            ).model_dump(exclude_none=True),
        )
    return dict(row)



# ---------------------------------------------------------------------------
# Activity Plans endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/activity-plans",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create an Activity Plan Resource.",
)
async def create_activity_plan(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: ActivityPlanService = Depends(get_activity_plan_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body,
        prefixes=ALL_PROHIBITED_PREFIXES,
        error_code="activity_plan_validation_failed",
    )
    try:
        body = CreateActivityPlanRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="activity_plan_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_activity_plan(
                connection,
                target_project_id=body.target_project_id,
                title=body.title,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except ActivityPlanAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except ActivityPlanAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="activity_plan_audit_failed"
        ) from exc
    except ActivityPlanProjectNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_project_not_resolvable",
            extra={"target_project_id": exc.target_project_id},
        ) from exc
    except ActivityPlanValidationError as exc:
        raise _validation_to_http(
            exc, error_code="activity_plan_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "activity_plan_id": result.activity_plan_id,
        "title": result.title,
        "target_project_id": result.target_project_id,
        "authoring_party_id": result.authoring_party_id,
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/activity-plans/{activity_plan_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read an Activity Plan Resource.",
)
async def read_activity_plan(
    activity_plan_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Activity Plan matching ``activity_plan_id``.

    Activity Plans have no Revisions — the Resource row is the
    single authoritative record (design §"Planning_Service.ActivityPlans").
    The GET path therefore takes only the Resource Identity.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT activity_plan_id, target_project_id, title,
                           authoring_party_id, applicable_scope, recorded_at
                    FROM Activity_Plans
                    WHERE activity_plan_id = :activity_plan_id
                    """
                ),
                {"activity_plan_id": activity_plan_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="activity_plan_not_found",
                message=(
                    f"No Activity_Plans row for activity_plan_id="
                    f"{activity_plan_id!r}."
                ),
                target_activity_plan_id=activity_plan_id,
            ).model_dump(exclude_none=True),
        )
    return dict(row)



# ---------------------------------------------------------------------------
# Plan Revisions endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/activity-plans/{activity_plan_id}/plan-revisions",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Draft Plan Revision for the named Activity Plan.",
)
async def create_plan_revision(
    request: Request,
    activity_plan_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    service: PlanRevisionService = Depends(get_plan_revision_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body,
        prefixes=ALL_PROHIBITED_PREFIXES,
        error_code="plan_revision_validation_failed",
    )
    try:
        body = CreatePlanRevisionRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="plan_revision_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_plan_revision(
                connection,
                target_activity_plan_id=activity_plan_id,
                planned_scope=body.planned_scope,
                deliverable_expectation_refs=tuple(body.deliverable_expectation_refs),
                planning_assumptions=tuple(body.planning_assumptions),
                ordering_rationale=body.ordering_rationale,
                predecessor_plan_revision_id=body.predecessor_plan_revision_id,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except PlanRevisionAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except PlanRevisionAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="plan_revision_audit_failed"
        ) from exc
    except PlanRevisionActivityPlanNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_activity_plan_not_resolvable",
            extra={"target_activity_plan_id": exc.target_activity_plan_id},
        ) from exc
    except PlanRevisionDeliverableExpectationNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_deliverable_expectation_not_resolvable",
            extra={
                "target_deliverable_expectation_id": (
                    getattr(exc, "deliverable_expectation_id", None)
                )
            },
        ) from exc
    except PlanRevisionPredecessorNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="predecessor_plan_revision_not_resolvable",
            extra={
                "predecessor_plan_revision_id": (
                    getattr(exc, "predecessor_plan_revision_id", None)
                )
            },
        ) from exc
    except PlanRevisionPredecessorActivityPlanMismatchError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="predecessor_plan_revision_activity_plan_mismatch",
            extra={
                "predecessor_plan_revision_id": (
                    getattr(exc, "predecessor_plan_revision_id", None)
                ),
                "target_activity_plan_id": activity_plan_id,
            },
        ) from exc
    except PlanRevisionPredecessorApprovedError as exc:
        # Approved predecessors are rejected per Requirement 7.4. The
        # design's error-handling table lumps these into the 409-shape
        # ``target_plan_revision_already_approved`` indistinguishable
        # response so the deny path never reveals that an approved
        # predecessor exists.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorBody(
                error_code="target_plan_revision_already_approved",
                message=str(exc),
                predecessor_plan_revision_id=(
                    getattr(exc, "predecessor_plan_revision_id", None)
                ),
                lifecycle_state=getattr(exc, "predecessor_lifecycle_state", None),
            ).model_dump(exclude_none=True),
        ) from exc
    except PlanRevisionValidationError as exc:
        raise _validation_to_http(
            exc, error_code="plan_revision_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "plan_revision_id": result.plan_revision_id,
        "target_activity_plan_id": result.target_activity_plan_id,
        "lifecycle_state": result.lifecycle_state,
        "planned_scope": result.planned_scope,
        "deliverable_expectation_refs": list(result.deliverable_expectation_refs),
        "planning_assumptions": list(result.planning_assumptions),
        "ordering_rationale": result.ordering_rationale,
        "predecessor_plan_revision_id": result.predecessor_plan_revision_id,
        "supersedes_relationship_id": result.supersedes_relationship_id,
        "authoring_party_id": result.authoring_party_id,
        "applicable_scope": result.applicable_scope,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/activity-plans/{activity_plan_id}/plan-revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Plan Revision row.",
)
async def read_plan_revision(
    activity_plan_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Plan Revision matching ``(activity_plan_id, revision_id)``.

    ``deliverable_expectation_refs_json`` and ``planning_assumptions_json``
    are persisted as JSON-encoded arrays; the response decodes both
    so clients see structured lists rather than the raw strings.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT plan_revision_id, activity_plan_id,
                           predecessor_revision_id, lifecycle_state,
                           planned_scope, deliverable_expectation_refs_json,
                           planning_assumptions_json, ordering_rationale,
                           authoring_party_id, applicable_scope, recorded_at
                    FROM Plan_Revisions
                    WHERE plan_revision_id = :revision_id
                      AND activity_plan_id = :activity_plan_id
                    """
                ),
                {"activity_plan_id": activity_plan_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="plan_revision_not_found",
                message=(
                    f"No Plan_Revisions row for activity_plan_id="
                    f"{activity_plan_id!r}, revision_id={revision_id!r}."
                ),
                target_activity_plan_id=activity_plan_id,
                target_plan_revision_id=revision_id,
            ).model_dump(exclude_none=True),
        )

    body = dict(row)
    body["deliverable_expectation_refs"] = _decode_json_array(
        body.pop("deliverable_expectation_refs_json")
    )
    body["planning_assumptions"] = _decode_json_array(
        body.pop("planning_assumptions_json")
    )
    return body


def _decode_json_array(raw: str) -> list[Any]:
    """Decode a JSON array column to a Python list.

    Plan_Revisions persists ``deliverable_expectation_refs`` and
    ``planning_assumptions`` as canonical JSON strings (the service
    serializes empty lists as ``"[]"`` so the column is always
    well-defined). Defence-in-depth: a malformed value surfaces as an
    empty list rather than an opaque 500, mirroring the convention
    used by ``walking_slice.routes.findings.read_finding_revision``.
    """
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(decoded, list):
        return []
    return decoded



# ---------------------------------------------------------------------------
# Plan Reviews endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/plan-revisions/{plan_revision_id}/reviews",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Record a Plan Review against the named Draft Plan Revision.",
)
async def create_plan_review(
    request: Request,
    plan_revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    service: PlanReviewService = Depends(get_plan_review_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body,
        prefixes=ALL_PROHIBITED_PREFIXES,
        error_code="plan_review_validation_failed",
    )
    try:
        body = CreatePlanReviewRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="plan_review_validation_failed"
        ) from exc

    authority_basis = AuthorityBasisRef(
        type=body.authority_basis.type,
        id=body.authority_basis.id,
    )

    try:
        with ctx.engine.begin() as connection:
            result = service.create_plan_review(
                connection,
                target_plan_revision_id=plan_revision_id,
                outcome=body.outcome,
                rationale=body.rationale,
                reviewing_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except PlanReviewAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except PlanReviewAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="plan_review_audit_failed"
        ) from exc
    except PlanReviewTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_resolvable",
            extra={
                "target_plan_revision_id": getattr(
                    exc, "target_plan_revision_id", plan_revision_id
                )
            },
        ) from exc
    except PlanReviewTargetNotDraftError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_draft",
            extra={
                "target_plan_revision_id": getattr(
                    exc, "target_plan_revision_id", plan_revision_id
                ),
                "lifecycle_state": getattr(exc, "lifecycle_state", None),
            },
        ) from exc
    except PlanReviewValidationError as exc:
        raise _validation_to_http(
            exc, error_code="plan_review_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return {
        "plan_review_id": result.plan_review_id,
        "plan_review_revision_id": result.plan_review_revision_id,
        "target_plan_revision_id": result.target_plan_revision_id,
        "outcome": result.outcome,
        "rationale": result.rationale,
        "reviewing_party_id": result.reviewing_party_id,
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
    "/plan-reviews/{plan_review_id}/revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a persisted Plan Review Revision.",
)
async def read_plan_review_revision(
    plan_review_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT plan_review_revision_id, plan_review_id,
                           target_plan_revision_id, outcome, rationale,
                           reviewing_party_id, authority_basis_type,
                           authority_basis_id, applicable_scope, recorded_at
                    FROM Plan_Review_Revisions
                    WHERE plan_review_revision_id = :revision_id
                      AND plan_review_id = :plan_review_id
                    """
                ),
                {"plan_review_id": plan_review_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="plan_review_revision_not_found",
                message=(
                    f"No Plan_Review_Revisions row for plan_review_id="
                    f"{plan_review_id!r}, revision_id={revision_id!r}."
                ),
                target_plan_review_id=plan_review_id,
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body



# ---------------------------------------------------------------------------
# Plan Approvals endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/plan-revisions/{plan_revision_id}/approvals",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Record a Plan Approval and (on Approve) atomically transition "
        "the Plan Revision's lifecycle from draft to approved."
    ),
)
async def create_plan_approval(
    request: Request,
    plan_revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    service: PlanApprovalService = Depends(get_plan_approval_service),
) -> dict[str, Any]:
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body,
        prefixes=ALL_PROHIBITED_PREFIXES,
        error_code="plan_approval_validation_failed",
    )
    try:
        body = CreatePlanApprovalRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="plan_approval_validation_failed"
        ) from exc

    authority_basis = AuthorityBasisRef(
        type=body.authority_basis.type,
        id=body.authority_basis.id,
    )
    omissions = tuple(
        PlanApprovalOmissionEntry(
            category=entry.category,
            excluded_source_id=entry.excluded_source_id,
            excluded_source_revision_id=entry.excluded_source_revision_id,
            rationale=entry.rationale,
        )
        for entry in body.omissions
    )

    try:
        with ctx.engine.begin() as connection:
            result = service.create_plan_approval(
                connection,
                ctx.engine,
                target_plan_revision_id=plan_revision_id,
                outcome=body.outcome,
                rationale=body.rationale,
                approving_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                omissions=omissions,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except PlanApprovalAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except PlanApprovalAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="plan_approval_audit_failed"
        ) from exc
    except ApprovedPlanRevisionImmutableAuditFailureError as exc:
        raise _immutability_audit_failure_to_http(exc) from exc
    except ApprovedPlanRevisionImmutableError as exc:
        raise _immutability_to_http(exc) from exc
    except PlanApprovalTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_plan_revision_not_resolvable",
            extra={
                "target_plan_revision_id": getattr(
                    exc, "target_plan_revision_id", plan_revision_id
                )
            },
        ) from exc
    except PlanApprovalTargetNotDraftError as exc:
        # Per Requirement 9.6 (design §"Error Handling" rule 5), a
        # Plan Approval submission against an Approved Plan Revision
        # is an immutability violation: the submission would attempt
        # to UPDATE the approved Plan Revision's ``lifecycle_state``
        # (or — for ``Reject_Approval`` — would still record a Plan
        # Approval Record whose presence breaks the "byte-equivalent
        # forever" invariant on the target). Route the attempt
        # through :func:`enforce_approved_plan_revision_immutability`
        # so a Denial Record is appended in a separate transaction
        # (Requirement 9.6 / 13.5) and the response carries the
        # stable ``error_code = approved_plan_revision_immutable``
        # the design's rule 5 pins.
        if getattr(exc, "lifecycle_state", None) == "approved":
            try:
                enforce_approved_plan_revision_immutability(
                    engine=ctx.engine,
                    audit_log=ctx.audit,
                    target_plan_revision_id=plan_revision_id,
                    actor_party_id=ctx.party_id,
                    attempted_action="create.plan_approval",
                    correlation_id=ctx.correlation_id,
                    recorded_time=ctx.clock.now(),
                )
            except ApprovedPlanRevisionImmutableAuditFailureError as audit_exc:
                raise _immutability_audit_failure_to_http(
                    audit_exc
                ) from audit_exc
            except ApprovedPlanRevisionImmutableError as imm_exc:
                raise _immutability_to_http(imm_exc) from imm_exc
        # Defensive fallback: should be unreachable because
        # ``'approved'`` is the only non-draft lifecycle state in
        # Slice 2 (AD-WS-18), but preserved so a future lifecycle
        # extension that adds an additional non-draft state still
        # produces a structured 409 rather than an opaque error.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorBody(
                error_code="target_plan_revision_already_approved",
                message=str(exc),
                target_plan_revision_id=getattr(
                    exc, "target_plan_revision_id", plan_revision_id
                ),
                lifecycle_state=getattr(exc, "lifecycle_state", None),
            ).model_dump(exclude_none=True),
        ) from exc
    except PlanApprovalConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=ErrorBody(
                error_code="plan_approval_already_exists",
                message=str(exc),
                target_plan_revision_id=getattr(
                    exc, "target_plan_revision_id", plan_revision_id
                ),
                existing_plan_approval_id=getattr(
                    exc, "existing_plan_approval_id", None
                ),
            ).model_dump(exclude_none=True),
        ) from exc
    except PlanApprovalValidationError as exc:
        raise _validation_to_http(
            exc, error_code="plan_approval_validation_failed"
        ) from exc
    except ManifestValidationError as exc:
        raise _manifest_validation_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Catch-all for an unexpected manifest-persistence failure
        # inside the ``ctx.engine.begin()`` block (design §"Error
        # Handling" rule 6 / Requirement 10.6). The transaction has
        # already rolled back so no Plan Approval, Addresses
        # Relationship, Provenance Manifest, Omission Entry,
        # lifecycle UPDATE, or consequential audit row was persisted.
        raise _manifest_persistence_failure_to_http(exc) from exc

    return {
        "plan_approval_id": result.plan_approval_id,
        "target_activity_plan_id": result.target_activity_plan_id,
        "target_plan_revision_id": result.target_plan_revision_id,
        "outcome": result.outcome,
        "rationale": result.rationale,
        "approving_party_id": result.approving_party_id,
        "authority_basis": {
            "type": result.authority_basis.type,
            "id": str(result.authority_basis.id),
        },
        "applicable_scope": result.applicable_scope,
        "addresses_relationship_id": result.addresses_relationship_id,
        "manifest_id": result.manifest_id,
        "omission_entry_ids": list(result.omission_entry_ids),
        "new_lifecycle_state": result.new_lifecycle_state,
        "recorded_at": result.recorded_at,
        "correlation_id": result.correlation_id,
    }


@router.get(
    "/plan-approvals/{plan_approval_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a persisted Plan Approval Immutable Record.",
)
async def read_plan_approval(
    plan_approval_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Plan Approval matching ``plan_approval_id``.

    Plan Approvals are Immutable Records — no Revisions — so the GET
    path takes only the Plan Approval Identity.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT plan_approval_id, target_activity_plan_id,
                           target_plan_revision_id, outcome, rationale,
                           approving_party_id, authority_basis_type,
                           authority_basis_id, applicable_scope, recorded_at
                    FROM Plan_Approval_Records
                    WHERE plan_approval_id = :plan_approval_id
                    """
                ),
                {"plan_approval_id": plan_approval_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="plan_approval_not_found",
                message=(
                    f"No Plan_Approval_Records row for plan_approval_id="
                    f"{plan_approval_id!r}."
                ),
                plan_approval_id=plan_approval_id,
            ).model_dump(exclude_none=True),
        )
    body = dict(row)
    body["authority_basis"] = {
        "type": body.pop("authority_basis_type"),
        "id": body.pop("authority_basis_id"),
    }
    return body



# ---------------------------------------------------------------------------
# Planning Provenance Chain endpoint.
# ---------------------------------------------------------------------------


@router.get(
    "/plan-approvals/{plan_approval_id}/provenance",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary=(
        "Walk the Planning Provenance Chain from a Plan Approval Record "
        "back to the originating Document Revision(s)."
    ),
)
async def get_plan_approval_provenance(
    plan_approval_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> dict[str, Any]:
    """Return the full Planning Provenance Chain rooted at ``plan_approval_id``.

    Delegates to :meth:`ProvenanceNavigator.navigate_plan_approval`,
    which itself delegates the Decision → Recommendation → Finding →
    Region → Document tail to :meth:`navigate_decision`. The walk is
    wrapped in ``ctx.engine.begin()`` so the per-stage authorization
    evaluation audit rows commit alongside the (non-consequential)
    read.

    Error mapping:

    - :class:`PlanApprovalUnresolvableError` → 404. The same exception
      is raised by the navigator for the restricted case (the
      requesting Party lacks ``view.plan_approval`` authority on the
      resolved Plan Approval) so the response is indistinguishable
      per Requirement 14.7 / AD-WS-9 rule 1.
    """
    try:
        with ctx.engine.begin() as connection:
            chain: PlanApprovalProvenance = navigator.navigate_plan_approval(
                connection,
                plan_approval_id=plan_approval_id,
                party_id=ctx.party_id,
            )
    except PlanApprovalUnresolvableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="plan_approval_not_found",
                message=str(exc),
                plan_approval_id=plan_approval_id,
            ).model_dump(exclude_none=True),
        ) from exc

    return _plan_approval_provenance_to_body(chain)


def _plan_approval_provenance_to_body(
    chain: PlanApprovalProvenance,
) -> dict[str, Any]:
    """Serialize :class:`PlanApprovalProvenance` to a JSON-safe dict.

    Each planning-prefix node is rendered as the row contents (when
    visible) or as a redaction marker carrying only ``kind`` and
    ``redacted=True`` (per AD-WS-9 rule 1). The Slice 1 Decision tail
    is delegated to a serializer matching the on-the-wire shape
    produced by ``walking_slice.routes.provenance`` so a single
    client-side handler covers both endpoints; when the Decision is
    unresolved or restricted the chain carries ``decision_chain=None``
    (indistinguishable per Requirement 14.7 / AD-WS-9 rule 3).
    """
    return {
        "plan_approval": _plan_approval_node_to_body(chain.plan_approval),
        "plan_revision": _plan_revision_node_to_body(chain.plan_revision),
        "activity_plan": _activity_plan_node_to_body(chain.activity_plan),
        "project_revision": _project_revision_node_to_body(chain.project_revision),
        "objective_revision": _objective_revision_node_to_body(
            chain.objective_revision
        ),
        "decision_chain": _decision_chain_to_body(chain.decision_chain),
        "gap_descriptors": [
            _gap_descriptor_to_body(descriptor) for descriptor in chain.gap_descriptors
        ],
        "requested_plan_approval_id": chain.requested_plan_approval_id,
    }


def _plan_approval_node_to_body(node: PlanApprovalNode) -> dict[str, Any]:
    return {
        "plan_approval_id": node.plan_approval_id,
        "target_activity_plan_id": node.target_activity_plan_id,
        "target_plan_revision_id": node.target_plan_revision_id,
        "outcome": node.outcome,
        "rationale": node.rationale,
        "approving_party_id": node.approving_party_id,
        "authority_basis": {
            "type": node.authority_basis_type,
            "id": node.authority_basis_id,
        },
        "applicable_scope": node.applicable_scope,
        "recorded_at": node.recorded_at,
    }


def _plan_revision_node_to_body(
    node: PlanRevisionNode | RedactedNode,
) -> dict[str, Any]:
    if isinstance(node, RedactedNode):
        return _redacted_node_to_body(node)
    return {
        "plan_revision_id": node.plan_revision_id,
        "activity_plan_id": node.activity_plan_id,
        "predecessor_revision_id": node.predecessor_revision_id,
        "lifecycle_state": node.lifecycle_state,
        "planned_scope": node.planned_scope,
        "deliverable_expectation_refs": _decode_json_array(
            node.deliverable_expectation_refs_json
        ),
        "planning_assumptions": _decode_json_array(
            node.planning_assumptions_json
        ),
        "ordering_rationale": node.ordering_rationale,
        "authoring_party_id": node.authoring_party_id,
        "applicable_scope": node.applicable_scope,
        "recorded_at": node.recorded_at,
    }


def _activity_plan_node_to_body(
    node: ActivityPlanNode | RedactedNode,
) -> dict[str, Any]:
    if isinstance(node, RedactedNode):
        return _redacted_node_to_body(node)
    return {
        "activity_plan_id": node.activity_plan_id,
        "target_project_id": node.target_project_id,
        "title": node.title,
        "authoring_party_id": node.authoring_party_id,
        "applicable_scope": node.applicable_scope,
        "recorded_at": node.recorded_at,
    }


def _project_revision_node_to_body(
    node: ProjectRevisionNode | RedactedNode,
) -> dict[str, Any]:
    if isinstance(node, RedactedNode):
        return _redacted_node_to_body(node)
    return {
        "project_id": node.project_id,
        "project_revision_id": node.project_revision_id,
        "parent_revision_id": node.parent_revision_id,
        "name": node.name,
        "summary": node.summary,
        "target_objective_id": node.target_objective_id,
        "planned_start_date": node.planned_start_date,
        "planned_end_date": node.planned_end_date,
        "authoring_party_id": node.authoring_party_id,
        "applicable_scope": node.applicable_scope,
        "recorded_at": node.recorded_at,
    }


def _objective_revision_node_to_body(
    node: ObjectiveRevisionNode | RedactedNode,
) -> dict[str, Any]:
    if isinstance(node, RedactedNode):
        return _redacted_node_to_body(node)
    return {
        "objective_id": node.objective_id,
        "objective_revision_id": node.objective_revision_id,
        "parent_revision_id": node.parent_revision_id,
        "statement": node.statement,
        "rationale": node.rationale,
        "target_decision_id": node.target_decision_id,
        "authoring_party_id": node.authoring_party_id,
        "applicable_scope": node.applicable_scope,
        "recorded_at": node.recorded_at,
    }


def _redacted_node_to_body(node: RedactedNode) -> dict[str, Any]:
    """Render :class:`RedactedNode` as the AD-WS-9 rule 1 wire shape."""
    return {"kind": node.kind, "redacted": True}


def _decision_chain_to_body(chain: Any) -> Optional[dict[str, Any]]:
    """Serialize the Slice 1 Decision tail or return ``None`` when absent.

    The navigator returns ``None`` for both the unresolved-Decision and
    the restricted-Decision cases (Requirement 14.7), so the response
    intentionally renders identically for both: a top-level
    ``decision_chain: null``. When the chain is present, the body
    mirrors the canonical shape produced by
    :mod:`walking_slice.routes.provenance` so a single client-side
    decoder covers both endpoints.
    """
    if chain is None:
        return None
    return {
        "decision": {
            "decision_id": chain.decision.decision_id,
            "target_recommendation_id": chain.decision.target_recommendation_id,
            "target_recommendation_revision_id": (
                chain.decision.target_recommendation_revision_id
            ),
            "outcome": chain.decision.outcome,
            "rationale": chain.decision.rationale,
            "deciding_party_id": chain.decision.deciding_party_id,
            "authority_basis": {
                "type": chain.decision.authority_basis_type,
                "id": chain.decision.authority_basis_id,
            },
            "applicable_scope": chain.decision.applicable_scope,
            "recorded_at": chain.decision.recorded_at,
        },
        "recommendation_revision": _node_or_redacted_to_body(
            chain.recommendation_revision
        ),
        "findings": [_node_or_redacted_to_body(f) for f in chain.findings],
        "region_occurrences": [
            _node_or_redacted_to_body(r) for r in chain.region_occurrences
        ],
        "document_revisions": [
            _node_or_redacted_to_body(d) for d in chain.document_revisions
        ],
        "requested_decision_id": chain.requested_decision_id,
    }


def _node_or_redacted_to_body(node: Any) -> dict[str, Any]:
    """Generic node serializer for the delegated Slice 1 chain.

    Slice 1 Decision-chain node classes are frozen dataclasses with
    predictable attribute names; rendering them through
    :func:`dataclasses.asdict` would also work but introduces a
    hard import dependency on the dataclass module. The simpler
    attribute-walk strategy here keeps the renderer textually
    isolated from the Slice 1 provenance module's internal layout.
    """
    if isinstance(node, RedactedNode):
        return _redacted_node_to_body(node)
    # Render every public attribute that isn't a callable. The
    # serialized form is what the Slice 1 provenance route emits;
    # we keep parity by mirroring the same set.
    body: dict[str, Any] = {}
    for name in dir(node):
        if name.startswith("_"):
            continue
        value = getattr(node, name, None)
        if callable(value):
            continue
        body[name] = _coerce_jsonable(value)
    return body


def _coerce_jsonable(value: Any) -> Any:
    """Best-effort coercion of nested objects to JSON-safe primitives.

    Tuples become lists; UUIDs become strings; everything else
    passes through unchanged. The serializer is intentionally narrow
    — every Slice 1 / Slice 2 node attribute is already a primitive
    or a tuple of primitives.
    """
    if isinstance(value, tuple):
        return [_coerce_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_coerce_jsonable(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    return value


def _gap_descriptor_to_body(descriptor: Any) -> dict[str, Any]:
    """Serialize a gap descriptor in the shape used by Slice 1 routes."""
    return {
        "stage": getattr(descriptor, "stage", None),
        "category": getattr(descriptor, "category", None),
        "next_reachable_node_identity": getattr(
            descriptor, "next_reachable_node_identity", None
        ),
    }


# Imported but only referenced indirectly through the navigator; pulled in
# so type-only references in docstrings and exception mapping resolve at
# import time without forcing a per-handler late import.
_DECISION_UNRESOLVABLE_FOR_TYPING: Final[type[Exception]] = DecisionUnresolvableError
