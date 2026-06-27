"""HTTP routes for the Slice 4 Outcome_Service (task 13.1).

Design reference: ``.kiro/specs/fourth-walking-slice/design.md``
§"Components and Interfaces" (the per-service HTTP surface tables) and
§"Error Handling" (the seven error categories and the AD-WS-9
indistinguishable denial shape). AD-WS-32 names this module
``outcome/_routes.py`` and pins it to one FastAPI :class:`APIRouter`
mounted under ``/api/v1``.

This module wires every Outcome_Service Resource / Record write and read
to its HTTP surface. The endpoint inventory mirrors the design tables
exactly:

| Method | Path | Service / function |
|--------|------|---------------------|
| ``POST`` | ``/api/v1/measurement-definitions`` | :meth:`MeasurementDefinitionService.create_measurement_definition` |
| ``GET``  | ``/api/v1/measurement-definitions/{id}`` | direct ``Measurement_Definitions`` read |
| ``GET``  | ``/api/v1/measurement-definitions/{id}/revisions/{rid}`` | :meth:`MeasurementDefinitionService.get_definition_revision` |
| ``POST`` | ``/api/v1/measurement-records`` | :meth:`MeasurementRecordService.create_native_measurement` |
| ``POST`` | ``/api/v1/measurement-records/imported`` | :meth:`MeasurementRecordService.create_imported_measurement` |
| ``GET``  | ``/api/v1/measurement-records/{id}`` | :meth:`MeasurementRecordService.get_measurement_record` (source-system attrs redacted) |
| ``POST`` | ``/api/v1/observed-outcomes`` | :meth:`ObservedOutcomeService.create_observed_outcome` |
| ``POST`` | ``/api/v1/observed-outcomes/{id}/revisions`` | :meth:`ObservedOutcomeService.revise_observed_outcome` |
| ``GET``  | ``/api/v1/observed-outcomes/{id}/revisions/{rid}`` | :meth:`ObservedOutcomeService.get_observed_outcome_revision` |
| ``POST`` | ``/api/v1/success-condition-assessments`` | :meth:`SuccessConditionAssessmentService.create_assessment` |
| ``GET``  | ``/api/v1/success-condition-assessments/{id}`` | :meth:`SuccessConditionAssessmentService.get_assessment` |
| ``POST`` | ``/api/v1/outcome-reviews`` | :meth:`OutcomeReviewService.create_outcome_review` |
| ``GET``  | ``/api/v1/outcome-reviews/{id}`` | direct ``Outcome_Review_Records`` read |
| ``GET``  | ``/api/v1/outcome-reviews/{id}/provenance`` | :meth:`ProvenanceNavigator.navigate_outcome_review` |
| ``GET``  | ``/api/v1/measurement-records/{id}/provenance`` | :meth:`ProvenanceNavigator.navigate_outcome_node` |
| ``GET``  | ``/api/v1/observed-outcomes/{id}/revisions/{rid}/provenance`` | :meth:`ProvenanceNavigator.navigate_outcome_node` |
| ``GET``  | ``/api/v1/intended-outcomes/{intended_outcome_revision_id}/outcome-status`` | :func:`project_outcome_status` |

Responsibilities (per task 13.1):

1. Wire each route through the Slice 1 :class:`RequestContext`
   dependency so the handler resolves the actor Party Identity
   (``ctx.party_id``), the per-request :class:`Engine` (``ctx.engine``),
   the per-request :class:`Clock` (``ctx.clock``), the authorization
   service (``ctx.authz``), and the correlation handle
   (``ctx.correlation_id``) from one bearer-token-validated bundle. The
   recording / authoring / assessing / reviewing Party Identity is
   **always** sourced from ``ctx.party_id`` and never from the request
   body so a caller cannot impersonate another Party (matching the
   Slice 3 convention).
2. Define Pydantic v2 request models with
   ``ConfigDict(extra='forbid', frozen=True)`` so any typo'd / unknown
   field surfaces as a structured 400 at the API boundary, and combine
   them with
   :func:`walking_slice.outcome._helpers._reject_prohibited_attributes`
   on the raw request body for every write so the intended-side
   prohibited-attribute guard (Requirements 53, 54) runs at the API
   boundary. The raw request body is also forwarded to each service as
   ``request_attributes`` so the same guard runs at the service layer as
   defense in depth.
3. Map every Outcome_Service exception to the HTTP code listed in design
   §"Error Handling":

   - ``*ValidationError`` → 400 with ``error_code`` and a
     ``failed_constraints`` list / ``prohibited_keys`` list.
   - ``*TargetNotResolvableError`` / ``*CitationError`` /
     ``*SourcingError`` → 404 (AD-WS-9-shaped when the Party lacks view
     authority on the target).
   - ``*DuplicateError`` / ``*ConflictError`` / ``*ConcurrencyError``
     (stale predecessor) → 409.
   - ``*AuthorizationError`` → 403 with the AD-WS-9 indistinguishable
     denial body carrying **only** ``generic_denial_indicator``,
     ``reason_code``, and ``correlation_id``.
   - ``*AuditFailureError`` / :class:`walking_slice.audit.AuditAppendError`
     → 503 with the audit-failure indicator.

**Response shaping.** Design §"Error Handling" describes shaping every
response through a ``walking_slice.provenance._shape_response_constant_time(...)``
helper. That helper is not yet realized in code (it is tracked as a
backlog item shared with Slices 1–3, see the Slice 1/2/3 property-test
notes referencing ``ADR-HT-009`` / ``ADR-HT-014``); the established
Slice 3 route convention — direct exception mapping with the fixed
three-field :class:`DenialResponseBody` for every denial — is followed
here so the wire contract matches the rest of the cumulative monolith.
Restricted-vs-nonexistent indistinguishability is enforced at the
service layer (single ``*AuthorizationError`` / single ``*Unresolvable``
exception type) rather than via the unrealized helper.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope; the factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly. Task 13.2 wires the concrete instances through
``walking_slice.app.create_app`` via
:attr:`fastapi.FastAPI.dependency_overrides`. The outcome-status
Projection reaches the :class:`AuthorizationService` and :class:`Clock`
through ``ctx.authz`` / ``ctx.clock`` so it needs no extra factory.

Requirements satisfied (per task 13.1):
    44.1, 45.1, 46.1, 47.1, 48.1, 49.1 — the Outcome_Service Record /
        Revision creation endpoints are mounted at the design's canonical
        paths and delegate each consequential write to the matching
        service inside one ``ctx.engine.begin()`` transaction.
    50.7 — every denial response is the AD-WS-9 three-field shape.
    51.1 — the Outcome Measurement Provenance Chain traversal endpoints
        delegate to the additive ``navigate_outcome_*`` navigator methods.
    53.2 — request bodies carrying a prohibited intended-side attribute
        are rejected with the offending keys identified, with no row
        persisted.
    55.7 — restricted nodes surface as redaction markers / gap
        descriptors through the navigator's own shaping.
    59.1 — the outcome-status Projection endpoint wraps the projected
        status in a :class:`ProjectionEnvelope`.
"""

from __future__ import annotations

import base64
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from typing import Annotated, Any, Final, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.app import get_request_context
from walking_slice.audit import AuditAppendError
from walking_slice.auth_middleware import RequestContext
from walking_slice.models import AuthorityBasisRef
from walking_slice.projection import ExplanationUnavailableResponse
from walking_slice.provenance import ProvenanceNavigator, RedactedNode
from walking_slice.outcome._helpers import (
    OUTCOME_PROHIBITED_PREFIXES,
    OutcomeValidationError,
    _reject_prohibited_attributes,
)
# Importing the provenance module registers ``navigate_outcome_review`` /
# ``navigate_outcome_node`` onto :class:`ProvenanceNavigator` at import
# time (``register_outcome_navigation`` runs in the module body).
from walking_slice.outcome import _provenance as _outcome_provenance
from walking_slice.outcome._projection import (
    OutcomeStatusProjection,
    OutcomeStatusTargetUnresolvableError,
    project_outcome_status,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionAuditFailureError,
    MeasurementDefinitionAuthorizationError,
    MeasurementDefinitionDuplicateError,
    MeasurementDefinitionService,
    MeasurementDefinitionTargetNotResolvableError,
    MeasurementDefinitionValidationError,
)
from walking_slice.outcome.measurement_records import (
    MeasurementRecordAuditFailureError,
    MeasurementRecordAuthorizationError,
    MeasurementRecordDuplicateError,
    MeasurementRecordService,
    MeasurementRecordTargetNotResolvableError,
    MeasurementRecordValidationError,
)
from walking_slice.outcome.observed_outcomes import (
    ObservedOutcomeAuditFailureError,
    ObservedOutcomeAuthorizationError,
    ObservedOutcomeCitationError,
    ObservedOutcomeConcurrencyError,
    ObservedOutcomeService,
    ObservedOutcomeTargetNotResolvableError,
    ObservedOutcomeValidationError,
)
from walking_slice.outcome.outcome_reviews import (
    OutcomeReviewAuditFailureError,
    OutcomeReviewAuthorizationError,
    OutcomeReviewCitationError,
    OutcomeReviewConflictError,
    OutcomeReviewService,
    OutcomeReviewTargetNotResolvableError,
    OutcomeReviewValidationError,
)
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentAuditFailureError,
    SuccessConditionAssessmentAuthorizationError,
    SuccessConditionAssessmentService,
    SuccessConditionAssessmentSourcingError,
    SuccessConditionAssessmentTargetNotResolvableError,
    SuccessConditionAssessmentValidationError,
)
from walking_slice.outcome._provenance import (
    OutcomeNodeUnresolvableError,
    OutcomeReviewUnresolvableError,
)


__all__ = [
    "AuthorityBasisRequestBody",
    "CreateImportedMeasurementRecordRequestBody",
    "CreateMeasurementDefinitionRequestBody",
    "CreateNativeMeasurementRecordRequestBody",
    "CreateObservedOutcomeRequestBody",
    "CreateOutcomeReviewRequestBody",
    "CreateSuccessConditionAssessmentRequestBody",
    "DenialResponseBody",
    "ErrorBody",
    "ReviseObservedOutcomeRequestBody",
    "get_engine",
    "get_measurement_definition_service",
    "get_measurement_record_service",
    "get_observed_outcome_service",
    "get_outcome_review_service",
    "get_provenance_navigator",
    "get_success_condition_assessment_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------


router = APIRouter(prefix="/api/v1", tags=["outcome"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# Each factory raises :class:`NotImplementedError` until task 13.2 wires
# the concrete instance through ``walking_slice.app.create_app``. Tests
# do the same on their per-test :class:`fastapi.FastAPI` instances. The
# placeholders follow the Slice 1/2/3 ``get_engine`` / ``get_<service>``
# convention so the wiring is grep-friendly.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    Used by the GET read endpoints that issue a single ``SELECT`` and do
    not need the full :class:`RequestContext` bundle. Task 13.2 overrides
    this with the same engine the RequestContext resolver uses.
    """
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_engine must be overridden by "
        "app composition (task 13.2) or test fixtures."
    )


def get_measurement_definition_service() -> MeasurementDefinitionService:
    """Provide the slice's :class:`MeasurementDefinitionService` singleton."""
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_measurement_definition_service "
        "must be overridden by app composition (task 13.2) or test fixtures."
    )


def get_measurement_record_service() -> MeasurementRecordService:
    """Provide the slice's :class:`MeasurementRecordService` singleton."""
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_measurement_record_service must "
        "be overridden by app composition (task 13.2) or test fixtures."
    )


def get_observed_outcome_service() -> ObservedOutcomeService:
    """Provide the slice's :class:`ObservedOutcomeService` singleton."""
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_observed_outcome_service must "
        "be overridden by app composition (task 13.2) or test fixtures."
    )


def get_success_condition_assessment_service() -> (
    SuccessConditionAssessmentService
):
    """Provide the slice's :class:`SuccessConditionAssessmentService`."""
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_success_condition_assessment_"
        "service must be overridden by app composition (task 13.2) or test "
        "fixtures."
    )


def get_outcome_review_service() -> OutcomeReviewService:
    """Provide the slice's :class:`OutcomeReviewService` singleton."""
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_outcome_review_service must "
        "be overridden by app composition (task 13.2) or test fixtures."
    )


def get_provenance_navigator() -> ProvenanceNavigator:
    """Provide the slice's :class:`ProvenanceNavigator` singleton.

    The same navigator instance that serves the Slice 1 / Slice 2 /
    Slice 3 traversals is reused; importing this module has already
    attached the additive ``navigate_outcome_review`` /
    ``navigate_outcome_node`` methods to the class (AD-WS-32, Requirement
    51.1).
    """
    raise NotImplementedError(
        "walking_slice.outcome._routes.get_provenance_navigator must "
        "be overridden by app composition (task 13.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Shared response shells.
# ---------------------------------------------------------------------------


class ErrorBody(BaseModel):
    """Structured error envelope used for 400 / 404 / 409 / 503 responses.

    The shape is a superset of the per-route error bodies the Slice 1 /
    Slice 2 / Slice 3 routes use so a single client-side handler covers
    every Outcome_Service endpoint. Fields are optional; only the ones
    relevant to the failure are populated.

    The 403 denial response uses :class:`DenialResponseBody` instead of
    this envelope so the AD-WS-9 indistinguishable shape contains
    **only** ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id``.
    """

    model_config = ConfigDict(extra="forbid")

    error_code: str
    message: Optional[str] = None
    failed_constraint: Optional[str] = None
    failed_constraints: list[str] = Field(default_factory=list)
    prohibited_keys: list[str] = Field(default_factory=list)
    invalid_attributes: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    target_intended_outcome_revision_id: Optional[str] = None
    target_measurement_definition_revision_id: Optional[str] = None
    sourced_observed_outcome_revision_id: Optional[str] = None
    observed_outcome_id: Optional[str] = None
    existing_measurement_definition_id: Optional[str] = None
    existing_measurement_record_id: Optional[str] = None
    existing_outcome_review_id: Optional[str] = None
    current_revision_id: Optional[str] = None
    audit_failure_indicator: Optional[str] = None
    correlation_id: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


class DenialResponseBody(BaseModel):
    """403 response body for a denied Outcome_Service attempt (AD-WS-9).

    The shape is fixed by AD-WS-9 and Requirement 50.7:
    ``generic_denial_indicator``, ``reason_code``, and ``correlation_id``
    — nothing else. ``extra='forbid'`` keeps this invariant locally: an
    accidental extra field surfaces as a model-validation failure (a 500)
    rather than silently shipping a leak. ``reason_code`` mirrors the
    Slice 1 Requirement 7.2 enumeration.
    """

    model_config = ConfigDict(extra="forbid")

    generic_denial_indicator: str = "denied"
    reason_code: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Shared request building blocks.
# ---------------------------------------------------------------------------


_AUTHORITY_BASIS_TYPE_LITERAL = ("role-grant-id", "scope-id", "delegation-chain-id")


class AuthorityBasisRequestBody(BaseModel):
    """Authority basis sub-object on the Assessment / Review request bodies.

    Mirrors :class:`walking_slice.models.AuthorityBasisRef` (AD-WS-41
    reuses the Slice 1 enumeration unchanged). The ``id`` is typed as
    :class:`UUID` so the wire contract is unambiguous; the service layer
    accepts the Slice 1 :class:`AuthorityBasisRef` whose ``id`` is also a
    :class:`UUID`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str = Field(min_length=1)
    id: UUID


# ---------------------------------------------------------------------------
# Per-Resource request body models.
#
# The authoring / recording / assessing / reviewing Party Identity is
# never carried on the request body — it is sourced from
# ``ctx.party_id`` so a caller cannot impersonate a different Party
# (matching the Slice 3 convention). Enumerated string fields
# (``assessment_category``, ``review_outcome``, ``attribution_stance``,
# ``confidence``, ``source_system_authority``, ``origin``,
# ``outcome_kind``) are accepted as plain strings so the service layer
# performs the authoritative enumeration validation and returns its
# structured ``failed_constraint`` discriminator (and so the
# "never default to authoritative" / "reject outcome_kind != observed"
# rules run at the service layer per Requirements 46.7, 47).
# ---------------------------------------------------------------------------


class CreateMeasurementDefinitionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/measurement-definitions``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_intended_outcome_revision_id: str = Field(min_length=1)
    measurand_description: str = Field(min_length=1, max_length=4_000)
    unit_of_measure: str = Field(min_length=1, max_length=200)
    observation_window: str = Field(min_length=1, max_length=1_000)
    cadence: str = Field(min_length=1, max_length=1_000)
    data_source: str = Field(min_length=1, max_length=1_000)
    applicable_scope: str = Field(min_length=1)


class CreateNativeMeasurementRecordRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/measurement-records`` (native).

    ``observed_value`` is accepted as a JSON string so the Decimal
    normalization (≤ 6 fractional digits, Requirement 45.2) happens at
    the service layer without float-precision drift.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_measurement_definition_revision_id: str = Field(min_length=1)
    observed_value: str = Field(min_length=1, max_length=64)
    observed_value_unit: str = Field(min_length=1, max_length=200)
    observation_time: datetime
    applicable_scope: str = Field(min_length=1)


class CreateImportedMeasurementRecordRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/measurement-records/imported``.

    ``source_system_authority`` is :data:`Optional` with no default so an
    absent designation reaches the service unchanged and is *rejected*
    there — never defaulted to ``authoritative`` (Requirement 46.7).
    ``origin``, when supplied, must equal ``imported``; the service
    rejects any other value (Requirement 46.4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_measurement_definition_revision_id: str = Field(min_length=1)
    observed_value: str = Field(min_length=1, max_length=64)
    observed_value_unit: str = Field(min_length=1, max_length=200)
    observation_time: datetime
    source_system_id: Optional[str] = None
    source_system_record_id: Optional[str] = None
    source_system_authority: Optional[str] = None
    source_system_retrieval_time: datetime
    origin: Optional[str] = None
    applicable_scope: str = Field(min_length=1)


class CreateObservedOutcomeRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/observed-outcomes``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_intended_outcome_revision_id: str = Field(min_length=1)
    assessment_summary: str = Field(min_length=1, max_length=4_000)
    cited_measurement_record_ids: list[str] = Field(
        default_factory=list, max_length=1_000
    )
    applicable_scope: str = Field(min_length=1)
    outcome_kind: Optional[str] = None


class ReviseObservedOutcomeRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/observed-outcomes/{id}/revisions``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    predecessor_revision_id: str = Field(min_length=1)
    assessment_summary: str = Field(min_length=1, max_length=4_000)
    cited_measurement_record_ids: list[str] = Field(
        default_factory=list, max_length=1_000
    )
    applicable_scope: str = Field(min_length=1)
    outcome_kind: Optional[str] = None


class CreateSuccessConditionAssessmentRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/success-condition-assessments``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_intended_outcome_revision_id: str = Field(min_length=1)
    sourced_observed_outcome_revision_id: str = Field(min_length=1)
    assessment_category: str = Field(min_length=1, max_length=64)
    assessment_rationale: str = Field(min_length=1, max_length=4_000)
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


class CreateOutcomeReviewRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/outcome-reviews``.

    ``attribution_evidence_reference`` defaults to the empty string so an
    omitted reference is accepted structurally and the service applies
    the Requirement 49.4 rule (``Asserted`` / ``Contradicted`` require a
    non-empty reference).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_intended_outcome_revision_id: str = Field(min_length=1)
    review_outcome: str = Field(min_length=1, max_length=64)
    attribution_stance: str = Field(min_length=1, max_length=64)
    confidence: str = Field(min_length=1, max_length=64)
    review_rationale: str = Field(min_length=1, max_length=4_000)
    attribution_evidence_reference: str = Field(default="", max_length=4_000)
    cited_assessment_ids: list[str] = Field(
        default_factory=list, max_length=1_000
    )
    cited_completion_ids: list[str] = Field(
        default_factory=list, max_length=1_000
    )
    cited_produced_deliverable_revision_ids: list[str] = Field(
        default_factory=list, max_length=1_000
    )
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str = Field(min_length=1)


# ---------------------------------------------------------------------------
# Request-body helpers.
# ---------------------------------------------------------------------------


async def _read_json_body(request: Request) -> dict[str, Any]:
    """Read and JSON-decode the request body, returning a dict.

    Empty bodies and non-object bodies surface as a structured 400. The
    returned dict is the raw input shape — it is passed both to Pydantic
    for declared-field validation and to
    :func:`_reject_prohibited_attributes` (via
    :func:`_screen_prohibited_attributes`) for the prohibited-key screen,
    and is forwarded to the service as ``request_attributes`` so the same
    guard runs again at the service layer.
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


def _screen_prohibited_attributes(
    body: dict[str, Any],
    *,
    error_code: str,
) -> None:
    """Reject the request body when any top-level key is prohibited.

    Wraps :func:`walking_slice.outcome._helpers._reject_prohibited_attributes`
    so an :class:`OutcomeValidationError` becomes a structured 400 at the
    API boundary (Requirements 53, 54). Runs before the Pydantic model is
    constructed so the prohibited-attribute error takes precedence over a
    declared-field error.
    """
    try:
        _reject_prohibited_attributes(body, OUTCOME_PROHIBITED_PREFIXES)
    except OutcomeValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error_code=error_code,
                message=str(exc),
                failed_constraint="prohibited_attribute",
                prohibited_keys=list(exc.prohibited_keys),
            ).model_dump(exclude_none=True),
        ) from exc


def _validation_error_to_http(
    exc: ValidationError, *, error_code: str
) -> HTTPException:
    """Convert a Pydantic :class:`ValidationError` to a 400 ``HTTPException``.

    Field names from ``loc`` are joined with ``.`` for nested fields.
    ``missing`` / ``missing_argument`` errors are surfaced on the
    ``missing`` list; every other error lands in ``validation_errors``
    with the JSON-safe subset of the error dict (the ``ctx`` field is
    dropped because Pydantic v2 attaches the original exception object
    there, which breaks JSON encoding).
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
            other.append(
                {key: value for key, value in err.items() if key != "ctx"}
            )
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error_code=error_code,
            message="Request failed validation.",
            missing=sorted(set(missing)),
            validation_errors=other,
        ).model_dump(exclude_none=True),
    )


def _authority_basis_to_ref(body: AuthorityBasisRequestBody) -> AuthorityBasisRef:
    """Convert the request-body sub-object to the slice-wide value object."""
    return AuthorityBasisRef(type=body.type, id=body.id)


# ---------------------------------------------------------------------------
# Error mappers — one per HTTP category. Each reads the typed attributes
# the Outcome_Service exceptions expose (``failed_constraint``,
# ``prohibited_keys``, identity fields) via ``getattr`` so a single
# mapper covers every service's parallel exception family.
# ---------------------------------------------------------------------------


def _validation_to_http(exc: Exception, *, error_code: str) -> HTTPException:
    """Map an Outcome_Service ``*ValidationError`` to a structured 400."""
    failed_constraint = getattr(exc, "failed_constraint", None)
    prohibited_keys = getattr(exc, "prohibited_keys", ())
    invalid_attributes = getattr(exc, "invalid_attributes", ())
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error_code=error_code,
            message=str(exc),
            failed_constraint=failed_constraint,
            failed_constraints=[failed_constraint] if failed_constraint else [],
            prohibited_keys=list(prohibited_keys),
            invalid_attributes=list(invalid_attributes),
        ).model_dump(exclude_none=True),
    )


def _not_resolvable_to_http(
    exc: Exception,
    *,
    error_code: str,
    extra: Optional[dict[str, Any]] = None,
) -> HTTPException:
    """Map an Outcome_Service resolution failure to a 404.

    Per design §"Error Handling" category 2 the body carries the supplied
    ``error_code`` and any additional identifiers. When the requesting
    Party lacks view authority on the target, the service raises the same
    exception type with no extra detail so the response is
    indistinguishable from a genuinely non-existent target (AD-WS-9).
    """
    payload: dict[str, Any] = {
        "error_code": getattr(exc, "failed_constraint", None) or error_code,
        "message": str(exc),
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
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
    """Map a 409-class Outcome_Service exception to a structured 409.

    Used for uniqueness conflicts (Measurement Definition, Outcome
    Review, imported Measurement Record idempotency key) and the
    optimistic-concurrency stale-predecessor case. The body carries the
    existing entity Identity only when supplied by the caller (the
    service surfaces it only when the caller holds view authority on it,
    AD-WS-9 / AD-WS-39).
    """
    payload: dict[str, Any] = {
        "error_code": getattr(exc, "failed_constraint", None) or error_code,
        "message": str(exc),
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ErrorBody(**payload).model_dump(exclude_none=True),
    )


def _authorization_denial_to_http(exc: Exception) -> HTTPException:
    """Map an Outcome_Service ``*AuthorizationError`` to a 403 (AD-WS-9).

    The body carries **only** the three AD-WS-9 fields — no information
    about *what* was attempted, *which* role assignment was missing, or
    *whether* the target exists is leaked (Requirement 50.7).
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=DenialResponseBody(
            reason_code=getattr(exc, "reason_code", "denied"),
            correlation_id=getattr(exc, "correlation_id", ""),
        ).model_dump(),
    )


def _audit_failure_to_http(exc: Exception, *, error_code: str) -> HTTPException:
    """Map an Outcome_Service ``*AuditFailureError`` to a 503.

    Per design §"Error Handling" category 6 (Requirements 50.6 / 57.6) a
    total denial-record append failure rolls back the originating
    transaction and surfaces as ``HTTP 503`` with the
    ``audit_failure_indicator`` flag set so the operator-facing surface
    can differentiate it from a routine deny.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code=error_code,
            message=str(exc),
            audit_failure_indicator="denial_audit_unavailable",
            correlation_id=getattr(exc, "correlation_id", None),
        ).model_dump(exclude_none=True),
    )


def _audit_append_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map :class:`AuditAppendError` to a 503 response.

    Audit append failures roll back the surrounding transaction (the
    Record / Revision row, the consequential Relationship rows, and the
    consequential audit row are discarded), matching the contract used in
    every other route module.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error_code="audit_append_failed",
            message=str(exc),
        ).model_dump(exclude_none=True),
    )


# ---------------------------------------------------------------------------
# Generic JSON serializer for nested read-model / provenance structures.
# ---------------------------------------------------------------------------


def _serialize(obj: Any) -> Any:
    """Recursively convert a read-model / provenance object to JSON-safe data.

    Handles the heterogeneous shapes the Outcome_Service returns without a
    bespoke mapper per node type:

    - :class:`RedactedNode` → ``{"kind": ..., "redacted": True}`` (the
      AD-WS-9 redaction marker; Requirement 55.3 / 58.2).
    - Pydantic models → ``model_dump(mode="json")``.
    - frozen dataclasses (provenance nodes, chains, the
      :class:`OutcomeProvenanceTree`, the read-model rows) → field dict.
    - lists / tuples → list of serialized elements.
    - mappings → dict of serialized values.
    - :class:`bytes` → base64 ASCII (so binary spans on a delegated
      Slice 1 leg remain JSON-valid; Requirement 55.2 byte-equivalence
      survives the lossless base64 round-trip).
    - :class:`datetime` → ISO-8601 string; :class:`UUID` / other scalars
      → ``str``.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, RedactedNode):
        return {"kind": obj.kind, "redacted": True}
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return base64.b64encode(obj).decode("ascii")
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _serialize(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    if isinstance(obj, dict):
        return {str(key): _serialize(value) for key, value in obj.items()}
    if isinstance(obj, UUID):
        return str(obj)
    return str(obj)


# ---------------------------------------------------------------------------
# Measurement Definitions endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/measurement-definitions",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Measurement Definition Resource + first Revision.",
)
async def create_measurement_definition(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: MeasurementDefinitionService = Depends(
        get_measurement_definition_service
    ),
) -> dict[str, Any]:
    """Create a Measurement Definition Resource + initial Revision (Req. 44.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="measurement_definition_validation_failed"
    )
    try:
        body = CreateMeasurementDefinitionRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="measurement_definition_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_measurement_definition(
                connection,
                target_intended_outcome_revision_id=(
                    body.target_intended_outcome_revision_id
                ),
                measurand_description=body.measurand_description,
                unit_of_measure=body.unit_of_measure,
                observation_window=body.observation_window,
                cadence=body.cadence,
                data_source=body.data_source,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except MeasurementDefinitionAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except MeasurementDefinitionAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="measurement_definition_audit_failed"
        ) from exc
    except MeasurementDefinitionTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_intended_outcome_not_resolvable",
            extra={
                "target_intended_outcome_revision_id": (
                    exc.target_intended_outcome_revision_id
                )
            },
        ) from exc
    except MeasurementDefinitionDuplicateError as exc:
        raise _conflict_to_http(
            exc,
            error_code="measurement_definition_already_exists",
            extra={
                "existing_measurement_definition_id": (
                    exc.existing_measurement_definition_id
                )
            },
        ) from exc
    except MeasurementDefinitionValidationError as exc:
        raise _validation_to_http(
            exc, error_code="measurement_definition_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.get(
    "/measurement-definitions/{measurement_definition_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Measurement Definition Resource.",
)
async def read_measurement_definition(
    measurement_definition_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Measurement Definition Resource joined to its Revision."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT d.measurement_definition_id,
                           d.target_intended_outcome_resource_id,
                           d.created_at,
                           r.measurement_definition_revision_id,
                           r.target_intended_outcome_revision_id,
                           r.measurand_description, r.unit_of_measure,
                           r.observation_window, r.cadence, r.data_source,
                           r.authoring_party_id, r.applicable_scope,
                           r.recorded_at
                    FROM Measurement_Definitions d
                    JOIN Measurement_Definition_Revisions r
                      ON r.measurement_definition_id =
                         d.measurement_definition_id
                    WHERE d.measurement_definition_id =
                          :measurement_definition_id
                    """
                ),
                {"measurement_definition_id": measurement_definition_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="measurement_definition_not_found",
                message=(
                    "No Measurement_Definitions row for "
                    f"measurement_definition_id={measurement_definition_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    return dict(row)


@router.get(
    "/measurement-definitions/{measurement_definition_id}/revisions/"
    "{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Measurement Definition Revision.",
)
async def read_measurement_definition_revision(
    measurement_definition_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    service: MeasurementDefinitionService = Depends(
        get_measurement_definition_service
    ),
) -> dict[str, Any]:
    """Return the Measurement Definition Revision matching ``revision_id``."""
    with engine.connect() as connection:
        row = service.get_definition_revision(
            connection, measurement_definition_revision_id=revision_id
        )
    if row is None or row.measurement_definition_id != measurement_definition_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="measurement_definition_revision_not_found",
                message=(
                    "No Measurement_Definition_Revisions row for "
                    f"revision_id={revision_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    return _serialize(row)


# ---------------------------------------------------------------------------
# Measurement Records endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/measurement-records",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a native Measurement Record.",
)
async def create_native_measurement_record(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: MeasurementRecordService = Depends(get_measurement_record_service),
) -> dict[str, Any]:
    """Create a native Measurement Record (``origin = native``, Req. 45.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="measurement_record_validation_failed"
    )
    try:
        body = CreateNativeMeasurementRecordRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="measurement_record_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_native_measurement(
                connection,
                target_measurement_definition_revision_id=(
                    body.target_measurement_definition_revision_id
                ),
                observed_value=body.observed_value,
                observed_value_unit=body.observed_value_unit,
                observation_time=body.observation_time,
                recording_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except MeasurementRecordAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except MeasurementRecordAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="measurement_record_audit_failed"
        ) from exc
    except MeasurementRecordTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_measurement_definition_revision_not_resolvable",
            extra={
                "target_measurement_definition_revision_id": (
                    exc.target_measurement_definition_revision_id
                )
            },
        ) from exc
    except MeasurementRecordValidationError as exc:
        raise _validation_to_http(
            exc, error_code="measurement_record_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.post(
    "/measurement-records/imported",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create an imported Measurement Record.",
)
async def create_imported_measurement_record(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: MeasurementRecordService = Depends(get_measurement_record_service),
) -> dict[str, Any]:
    """Create an imported Measurement Record (``origin = imported``, Req. 46.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="measurement_record_validation_failed"
    )
    try:
        body = CreateImportedMeasurementRecordRequestBody.model_validate(
            raw_body
        )
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="measurement_record_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_imported_measurement(
                connection,
                target_measurement_definition_revision_id=(
                    body.target_measurement_definition_revision_id
                ),
                observed_value=body.observed_value,
                observed_value_unit=body.observed_value_unit,
                observation_time=body.observation_time,
                source_system_id=body.source_system_id,
                source_system_record_id=body.source_system_record_id,
                source_system_authority=body.source_system_authority,
                source_system_retrieval_time=body.source_system_retrieval_time,
                importing_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                origin=body.origin,
                request_attributes=raw_body,
            )
    except MeasurementRecordAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except MeasurementRecordAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="measurement_record_audit_failed"
        ) from exc
    except MeasurementRecordTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_measurement_definition_revision_not_resolvable",
            extra={
                "target_measurement_definition_revision_id": (
                    exc.target_measurement_definition_revision_id
                )
            },
        ) from exc
    except MeasurementRecordDuplicateError as exc:
        raise _conflict_to_http(
            exc,
            error_code="imported_measurement_duplicate",
            extra={
                "existing_measurement_record_id": (
                    exc.existing_measurement_record_id
                )
            },
        ) from exc
    except MeasurementRecordValidationError as exc:
        raise _validation_to_http(
            exc, error_code="measurement_record_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.get(
    "/measurement-records/{measurement_record_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Measurement Record (source-system attributes redacted).",
)
async def read_measurement_record(
    measurement_record_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    service: MeasurementRecordService = Depends(get_measurement_record_service),
) -> dict[str, Any]:
    """Return the Measurement Record matching ``measurement_record_id``.

    Uses the read-model row that omits the restricted source-system
    attributes (AD-WS-34) so they never leak through this simple read.
    """
    with engine.connect() as connection:
        row = service.get_measurement_record(
            connection, measurement_record_id=measurement_record_id
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="measurement_record_not_found",
                message=(
                    "No Measurement_Records row for "
                    f"measurement_record_id={measurement_record_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    return _serialize(row)


# ---------------------------------------------------------------------------
# Observed Outcomes endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/observed-outcomes",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create an Observed Outcome Resource + initial Revision.",
)
async def create_observed_outcome(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: ObservedOutcomeService = Depends(get_observed_outcome_service),
) -> dict[str, Any]:
    """Create an Observed Outcome Resource + initial Revision (Req. 47.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="observed_outcome_validation_failed"
    )
    try:
        body = CreateObservedOutcomeRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="observed_outcome_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.create_observed_outcome(
                connection,
                target_intended_outcome_revision_id=(
                    body.target_intended_outcome_revision_id
                ),
                assessment_summary=body.assessment_summary,
                cited_measurement_record_ids=body.cited_measurement_record_ids,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                outcome_kind=body.outcome_kind,
                request_attributes=raw_body,
            )
    except ObservedOutcomeAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except ObservedOutcomeAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="observed_outcome_audit_failed"
        ) from exc
    except ObservedOutcomeTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_intended_outcome_not_resolvable",
            extra={
                "target_intended_outcome_revision_id": (
                    exc.target_intended_outcome_revision_id
                )
            },
        ) from exc
    except ObservedOutcomeCitationError as exc:
        raise _not_resolvable_to_http(
            exc, error_code="cited_measurement_record_not_resolvable"
        ) from exc
    except ObservedOutcomeValidationError as exc:
        raise _validation_to_http(
            exc, error_code="observed_outcome_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.post(
    "/observed-outcomes/{observed_outcome_id}/revisions",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Append a new Observed Outcome Revision.",
)
async def revise_observed_outcome(
    observed_outcome_id: Annotated[str, Path(min_length=1)],
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: ObservedOutcomeService = Depends(get_observed_outcome_service),
) -> dict[str, Any]:
    """Append a new immutable Observed Outcome Revision (Req. 47.3, AD-WS-36)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="observed_outcome_validation_failed"
    )
    try:
        body = ReviseObservedOutcomeRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="observed_outcome_validation_failed"
        ) from exc

    try:
        with ctx.engine.begin() as connection:
            result = service.revise_observed_outcome(
                connection,
                observed_outcome_id=observed_outcome_id,
                predecessor_revision_id=body.predecessor_revision_id,
                assessment_summary=body.assessment_summary,
                cited_measurement_record_ids=body.cited_measurement_record_ids,
                authoring_party_id=ctx.party_id,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                outcome_kind=body.outcome_kind,
                request_attributes=raw_body,
            )
    except ObservedOutcomeAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except ObservedOutcomeAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="observed_outcome_audit_failed"
        ) from exc
    except ObservedOutcomeTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_intended_outcome_not_resolvable",
            extra={
                "target_intended_outcome_revision_id": (
                    exc.target_intended_outcome_revision_id
                )
            },
        ) from exc
    except ObservedOutcomeConcurrencyError as exc:
        if getattr(exc, "failed_constraint", None) == "observed_outcome_not_resolvable":
            raise _not_resolvable_to_http(
                exc,
                error_code="observed_outcome_not_resolvable",
                extra={"observed_outcome_id": exc.observed_outcome_id},
            ) from exc
        raise _conflict_to_http(
            exc,
            error_code="observed_outcome_predecessor_stale",
            extra={
                "observed_outcome_id": exc.observed_outcome_id,
                "current_revision_id": getattr(exc, "current_revision_id", None),
            },
        ) from exc
    except ObservedOutcomeCitationError as exc:
        raise _not_resolvable_to_http(
            exc, error_code="cited_measurement_record_not_resolvable"
        ) from exc
    except ObservedOutcomeValidationError as exc:
        raise _validation_to_http(
            exc, error_code="observed_outcome_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.get(
    "/observed-outcomes/{observed_outcome_id}/revisions/{revision_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read an Observed Outcome Revision.",
)
async def read_observed_outcome_revision(
    observed_outcome_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    service: ObservedOutcomeService = Depends(get_observed_outcome_service),
) -> dict[str, Any]:
    """Return the Observed Outcome Revision matching ``revision_id``."""
    with engine.connect() as connection:
        row = service.get_observed_outcome_revision(
            connection, observed_outcome_revision_id=revision_id
        )
    if row is None or row.observed_outcome_id != observed_outcome_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="observed_outcome_revision_not_found",
                message=(
                    "No Observed_Outcome_Revisions row for "
                    f"revision_id={revision_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    return _serialize(row)


# ---------------------------------------------------------------------------
# Success-Condition Assessments endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/success-condition-assessments",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Success-Condition Assessment Record.",
)
async def create_success_condition_assessment(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: SuccessConditionAssessmentService = Depends(
        get_success_condition_assessment_service
    ),
) -> dict[str, Any]:
    """Create a Success-Condition Assessment Record (Req. 48.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="success_condition_assessment_validation_failed"
    )
    try:
        body = CreateSuccessConditionAssessmentRequestBody.model_validate(
            raw_body
        )
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="success_condition_assessment_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_assessment(
                connection,
                target_intended_outcome_revision_id=(
                    body.target_intended_outcome_revision_id
                ),
                sourced_observed_outcome_revision_id=(
                    body.sourced_observed_outcome_revision_id
                ),
                assessment_category=body.assessment_category,
                assessment_rationale=body.assessment_rationale,
                assessing_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except SuccessConditionAssessmentAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except SuccessConditionAssessmentAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="success_condition_assessment_audit_failed"
        ) from exc
    except SuccessConditionAssessmentTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_intended_outcome_not_resolvable",
            extra={
                "target_intended_outcome_revision_id": (
                    exc.target_intended_outcome_revision_id
                )
            },
        ) from exc
    except SuccessConditionAssessmentSourcingError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="sourced_observed_outcome_revision_not_resolvable",
            extra={
                "sourced_observed_outcome_revision_id": getattr(
                    exc, "sourced_observed_outcome_revision_id", None
                )
            },
        ) from exc
    except SuccessConditionAssessmentValidationError as exc:
        raise _validation_to_http(
            exc, error_code="success_condition_assessment_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.get(
    "/success-condition-assessments/{assessment_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read a Success-Condition Assessment Record.",
)
async def read_success_condition_assessment(
    assessment_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    service: SuccessConditionAssessmentService = Depends(
        get_success_condition_assessment_service
    ),
) -> dict[str, Any]:
    """Return the Success-Condition Assessment Record matching ``assessment_id``."""
    with engine.connect() as connection:
        row = service.get_assessment(connection, assessment_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="success_condition_assessment_not_found",
                message=(
                    "No Success_Condition_Assessment_Records row for "
                    f"assessment_id={assessment_id!r}."
                ),
            ).model_dump(exclude_none=True),
        )
    return _serialize(row)


# ---------------------------------------------------------------------------
# Outcome Reviews endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/outcome-reviews",
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create an Outcome Review Record.",
)
async def create_outcome_review(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
    service: OutcomeReviewService = Depends(get_outcome_review_service),
) -> dict[str, Any]:
    """Create an Outcome Review Governance Decision Record (Req. 49.1)."""
    raw_body = await _read_json_body(request)
    _screen_prohibited_attributes(
        raw_body, error_code="outcome_review_validation_failed"
    )
    try:
        body = CreateOutcomeReviewRequestBody.model_validate(raw_body)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="outcome_review_validation_failed"
        ) from exc

    authority_basis = _authority_basis_to_ref(body.authority_basis)

    try:
        with ctx.engine.begin() as connection:
            result = service.create_outcome_review(
                connection,
                target_intended_outcome_revision_id=(
                    body.target_intended_outcome_revision_id
                ),
                review_outcome=body.review_outcome,
                attribution_stance=body.attribution_stance,
                confidence=body.confidence,
                review_rationale=body.review_rationale,
                attribution_evidence_reference=(
                    body.attribution_evidence_reference
                ),
                cited_assessment_ids=body.cited_assessment_ids,
                cited_completion_ids=body.cited_completion_ids,
                cited_produced_deliverable_revision_ids=(
                    body.cited_produced_deliverable_revision_ids
                ),
                reviewing_party_id=ctx.party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                engine=ctx.engine,
                correlation_id=ctx.correlation_id,
                request_attributes=raw_body,
            )
    except OutcomeReviewAuthorizationError as exc:
        raise _authorization_denial_to_http(exc) from exc
    except OutcomeReviewAuditFailureError as exc:
        raise _audit_failure_to_http(
            exc, error_code="outcome_review_audit_failed"
        ) from exc
    except OutcomeReviewTargetNotResolvableError as exc:
        raise _not_resolvable_to_http(
            exc,
            error_code="target_intended_outcome_not_resolvable",
            extra={
                "target_intended_outcome_revision_id": (
                    exc.target_intended_outcome_revision_id
                )
            },
        ) from exc
    except OutcomeReviewConflictError as exc:
        raise _conflict_to_http(
            exc,
            error_code="outcome_review_already_exists",
            extra={
                "existing_outcome_review_id": getattr(
                    exc, "existing_outcome_review_id", None
                )
            },
        ) from exc
    except OutcomeReviewCitationError as exc:
        raise _not_resolvable_to_http(
            exc, error_code="cited_assessment_not_resolvable"
        ) from exc
    except OutcomeReviewValidationError as exc:
        raise _validation_to_http(
            exc, error_code="outcome_review_validation_failed"
        ) from exc
    except AuditAppendError as exc:
        raise _audit_append_failure_to_http(exc) from exc

    return result.model_dump(mode="json")


@router.get(
    "/outcome-reviews/{outcome_review_id}",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Read an Outcome Review Record.",
)
async def read_outcome_review(
    outcome_review_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> dict[str, Any]:
    """Return the Outcome Review Record matching ``outcome_review_id``."""
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT outcome_review_id,
                           target_intended_outcome_resource_id,
                           target_intended_outcome_revision_id,
                           review_outcome, attribution_stance, confidence,
                           review_rationale, attribution_evidence_reference,
                           reviewing_party_id, authority_basis_type,
                           authority_basis_id, applicable_scope, recorded_at
                    FROM Outcome_Review_Records
                    WHERE outcome_review_id = :outcome_review_id
                    """
                ),
                {"outcome_review_id": outcome_review_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="outcome_review_not_found",
                message=(
                    "No Outcome_Review_Records row for "
                    f"outcome_review_id={outcome_review_id!r}."
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
# Provenance traversal endpoints (the Outcome Measurement Provenance Chain).
# ---------------------------------------------------------------------------


@router.get(
    "/outcome-reviews/{outcome_review_id}/provenance",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Walk the Outcome Measurement Provenance Chain from an Outcome Review.",
)
async def navigate_outcome_review_provenance(
    outcome_review_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> dict[str, Any]:
    """Return the chain rooted at an Outcome Review Record (Req. 51.1).

    Restricted nodes surface as ``{kind, redacted: true}`` markers and
    unresolved / stale / unavailable links surface as gap descriptors —
    the navigator shapes both (Requirements 55.3, 55.4). An unresolvable
    or restricted root yields an indistinguishable 404 (AD-WS-9).
    """
    try:
        with ctx.engine.begin() as connection:
            tree = navigator.navigate_outcome_review(
                connection,
                outcome_review_id=outcome_review_id,
                party_id=ctx.party_id,
                at=ctx.clock.now(),
            )
    except OutcomeReviewUnresolvableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="outcome_review_not_resolvable",
                message=str(exc),
            ).model_dump(exclude_none=True),
        ) from exc
    return _serialize(tree)


@router.get(
    "/measurement-records/{measurement_record_id}/provenance",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Walk the chain rooted at a Measurement Record.",
)
async def navigate_measurement_record_provenance(
    measurement_record_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> dict[str, Any]:
    """Return the short-form chain rooted at a Measurement Record (Req. 55.1)."""
    try:
        with ctx.engine.begin() as connection:
            tree = navigator.navigate_outcome_node(
                connection,
                node_kind="measurement_record",
                node_id=measurement_record_id,
                party_id=ctx.party_id,
                at=ctx.clock.now(),
            )
    except OutcomeNodeUnresolvableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="outcome_node_not_resolvable",
                message=str(exc),
            ).model_dump(exclude_none=True),
        ) from exc
    return _serialize(tree)


@router.get(
    "/observed-outcomes/{observed_outcome_id}/revisions/{revision_id}/"
    "provenance",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary="Walk the chain rooted at an Observed Outcome Revision.",
)
async def navigate_observed_outcome_revision_provenance(
    observed_outcome_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> dict[str, Any]:
    """Return the short-form chain rooted at an Observed Outcome Revision."""
    try:
        with ctx.engine.begin() as connection:
            tree = navigator.navigate_outcome_node(
                connection,
                node_kind="observed_outcome_revision",
                node_id=revision_id,
                party_id=ctx.party_id,
                at=ctx.clock.now(),
            )
    except OutcomeNodeUnresolvableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="outcome_node_not_resolvable",
                message=str(exc),
            ).model_dump(exclude_none=True),
        ) from exc
    return _serialize(tree)


# ---------------------------------------------------------------------------
# Outcome-status Projection endpoint.
# ---------------------------------------------------------------------------


@router.get(
    "/intended-outcomes/{intended_outcome_revision_id}/outcome-status",
    status_code=status.HTTP_200_OK,
    responses={status.HTTP_404_NOT_FOUND: {"model": ErrorBody}},
    summary=(
        "Return the outcome-status Projection for an Intended Outcome "
        "Revision, wrapped in a ProjectionEnvelope."
    ),
)
async def get_outcome_status(
    intended_outcome_revision_id: Annotated[str, Path(min_length=1)],
    ctx: RequestContext = Depends(get_request_context),
) -> dict[str, Any]:
    """Return the outcome-status Projection (Req. 59.1).

    Delegates to :func:`project_outcome_status`, which derives the
    most-progressed status label from the Slice 4 source Records and wraps
    it in the Slice 1 :class:`ProjectionEnvelope`. ``view`` authority on
    the target Intended Outcome Revision is required; a restricted or
    unresolvable target yields an indistinguishable 404 (AD-WS-9).
    """
    try:
        with ctx.engine.begin() as connection:
            result = project_outcome_status(
                connection,
                intended_outcome_revision_id=intended_outcome_revision_id,
                party_id=ctx.party_id,
                at=ctx.clock.now(),
                authorization_service=ctx.authz,
                clock=ctx.clock,
            )
    except OutcomeStatusTargetUnresolvableError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error_code="target_intended_outcome_not_resolvable",
                message=str(exc),
            ).model_dump(exclude_none=True),
        ) from exc

    if isinstance(result, OutcomeStatusProjection):
        return {
            "intended_outcome_revision_id": result.intended_outcome_revision_id,
            "projected_status": result.projected_status,
            "envelope": _serialize(result.envelope),
        }
    if isinstance(result, ExplanationUnavailableResponse):
        return {
            "intended_outcome_revision_id": intended_outcome_revision_id,
            "projected_status": None,
            "envelope": None,
            "explanation_unavailable": _serialize(result),
        }
    # Defensive — the projection never returns any other shape.
    raise HTTPException(  # pragma: no cover - defensive
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=ErrorBody(
            error_code="outcome_status_response_invalid",
            message=(
                "project_outcome_status returned an unexpected response "
                f"shape: {type(result).__name__}."
            ),
        ).model_dump(exclude_none=True),
    )


# ---------------------------------------------------------------------------
# Defensive reference so the provenance-registration import is not pruned.
# ---------------------------------------------------------------------------

_OUTCOME_PROVENANCE_MODULE: Final = _outcome_provenance
