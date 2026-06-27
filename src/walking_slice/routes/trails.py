"""HTTP routes for the Trail_Service (task 10.3).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Trail_Service" HTTP surface plus AD-WS-4 (immutable Trail Revisions
and Trail Steps), AD-WS-5 (audit and manifest append inside the
originating transaction), AD-WS-9 (indistinguishable denial response
shape), and AD-WS-12 (slice restricts ``selection_mode`` to ``'Pinned'``):

| Method | Path                                                                | Purpose                                                                                |
|--------|---------------------------------------------------------------------|----------------------------------------------------------------------------------------|
| POST   | ``/api/v1/trails``                                                  | Create a Trail Resource plus its initial Trail Revision and five Trail Steps (Requirement 9.1). |
| POST   | ``/api/v1/trails/{trail_id}/revisions``                             | Append a new Trail Revision when the canonical form differs materially (Requirement 9.4). |
| GET    | ``/api/v1/trails/{trail_id}/revisions/{revision_id}``               | Read a persisted Trail Revision with its five ordered Trail Steps.                     |

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates consequential
  writes to :class:`walking_slice.trails.TrailService` inside one
  ``engine.begin()`` transaction so the Trail header, Trail Revision,
  five Trail Steps, optional Provenance Manifest, and ``Audit_Records``
  consequential row commit together (AD-WS-5, Requirement 13.1).
- Pre-validate request shapes with Pydantic v2 :class:`~pydantic.BaseModel`
  definitions using ``extra="forbid"`` so typo'd field names are
  rejected and convert any :class:`~pydantic.ValidationError` to a
  structured ``HTTP 400`` (instead of FastAPI's default 422) so the
  wire contract is uniform with the deeper service-layer responses.
- Map service exceptions to the codes listed in the task description:

  - :class:`~walking_slice.trails.TrailValidationError`
    → ``400`` with ``failed_constraint``.
  - :class:`~walking_slice.trails.TrailTargetUnresolvedError`
    → ``400`` with a per-ordinal ``unresolved_steps`` list (Requirement 9.5).
  - :class:`~walking_slice.trails.TrailNotFoundError`
    → ``404`` with the offending ``trail_id``.
  - :class:`~walking_slice.trails.TrailAuthorizationError`
    → ``403`` with the AD-WS-9 indistinguishable denial response shape
    carrying **only** ``generic_denial_indicator``, ``reason_code``,
    and ``correlation_id`` (no other fields — Requirement 7.4).
  - :class:`~walking_slice.trails.TrailAuditFailureError`
    → ``503`` with an ``audit_failure_indicator`` (Requirement 7.6).
  - :class:`~walking_slice.audit.AuditAppendError`
    → ``503``.

**Authentication.** This module accepts the actor's Party Identity from
the temporary ``X-Actor-Party-Id`` header. Task 15.1 will replace this
placeholder with the bearer-token authenticated ``RequestContext``
described in design §"Application-Level Composition". The header-based
shim mirrors the pattern in every other ``routes/*.py`` module so a
single future middleware change swaps them all in one go.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope. The factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly; tests (and the eventual ``app.py`` in task 15.2) override them
via :data:`fastapi.FastAPI.dependency_overrides`.

Requirements satisfied (per task 10.3):
    9.1 — ``POST /api/v1/trails`` records a Trail Resource plus its
          immutable first Trail Revision containing exactly five
          Trail Steps.
    9.4 — ``POST /api/v1/trails/{trail_id}/revisions`` appends a new
          immutable Trail Revision linked to the prior Revision by
          identity when the canonical form differs materially.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError
from walking_slice.manifests import ManifestValidationError
from walking_slice.trails import (
    AppendTrailRevisionResult,
    CreateTrailResult,
    ORDINAL_TARGET_KIND,
    TrailAuditFailureError,
    TrailAuthorizationError,
    TrailNotFoundError,
    TrailService,
    TrailStepInput,
    TrailTargetUnresolvedError,
    TrailValidationError,
)


__all__ = [
    "AppendTrailRevisionRequestBody",
    "AppendTrailRevisionResponseBody",
    "CreateTrailRequestBody",
    "CreateTrailResponseBody",
    "DenialResponseBody",
    "ErrorBody",
    "TrailRevisionResponseBody",
    "TrailStepRequestBody",
    "TrailStepResponseBody",
    "UnresolvedTrailStepBody",
    "get_engine",
    "get_trail_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["trails"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# These factories are deliberately stubs; task 15.2 wires concrete
# implementations through ``walking_slice.app.create_app``. Tests
# override them on the per-test :class:`fastapi.FastAPI` instance via
# ``app.dependency_overrides[get_engine] = lambda: engine`` etc., per
# the pattern recommended in the FastAPI docs and used elsewhere in the
# slice.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    Overridden in tests and in the application composition layer
    (task 15.2). Never called unwrapped from a route handler.
    """
    raise NotImplementedError(
        "walking_slice.routes.trails.get_engine must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


def get_trail_service() -> TrailService:
    """Provide the slice's :class:`TrailService` singleton.

    The service is connection-scoped at call time — every public
    method accepts the caller's SQLAlchemy connection — so a single
    instance serves all requests safely. When the service is wired
    with an :class:`~walking_slice.authorization.AuthorizationService`
    the Requirement 7 authority check is enforced; otherwise the
    back-compat path (no enforcement) is exercised.
    """
    raise NotImplementedError(
        "walking_slice.routes.trails.get_trail_service must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Validation limits mirrored from :mod:`walking_slice.trails` so the
# Pydantic schema can short-circuit obvious shape violations before a
# database connection is opened. The service layer re-validates
# defensively — the duplication catches structural errors without
# coupling the route's ``Field`` constraints to a private symbol.
# ---------------------------------------------------------------------------


_PURPOSE_MIN_CHARS: Final[int] = 1
_PURPOSE_MAX_CHARS: Final[int] = 500
_AUDIENCE_MIN_CHARS: Final[int] = 1
_ORDERING_RATIONALE_MAX_CHARS: Final[int] = 500
_ANNOTATION_MAX_CHARS: Final[int] = 2_000
_REQUIRED_STEP_COUNT: Final[int] = 5

_TARGET_KIND_LITERAL = Literal[
    "document_revision",
    "region_occurrence",
    "finding_revision",
    "recommendation_revision",
    "decision",
]
_SELECTION_MODE_LITERAL = Literal["Pinned"]


# ---------------------------------------------------------------------------
# Pydantic v2 boundary models.
# ---------------------------------------------------------------------------


class TrailStepRequestBody(BaseModel):
    """One Trail Step entry on a create / append request body.

    Mirrors :class:`walking_slice.trails.TrailStepInput`. The
    per-ordinal field interpretation (which identifier fields are
    required vs forbidden for each ordinal) is enforced by the
    service-layer
    :meth:`~walking_slice.trails.TrailService._validate_step_identifiers`;
    here we only enforce the shape rules that are knowable without
    inspecting other entries.

    ``selection_mode`` defaults to ``"Pinned"`` (AD-WS-12) so a client
    that omits the field still produces a valid Trail; explicit
    non-``"Pinned"`` values are rejected at the Pydantic layer with a
    stable error before any database round-trip.
    """

    model_config = ConfigDict(extra="forbid")

    ordinal: int = Field(
        ge=1,
        le=_REQUIRED_STEP_COUNT,
        description=(
            "Ordinal position of the step (1..5). Each ordinal must "
            "appear exactly once across the five steps (Requirement 9.2)."
        ),
    )
    target_kind: _TARGET_KIND_LITERAL = Field(
        description=(
            "Target-kind from the five-stage pipeline enumeration. "
            "Must match the kind required for the ordinal "
            "(Requirement 9.2): 1→document_revision, 2→region_occurrence, "
            "3→finding_revision, 4→recommendation_revision, 5→decision."
        ),
    )
    target_id: str = Field(
        min_length=1,
        description=(
            "Primary identifier of the target row. See "
            ":class:`walking_slice.trails.TrailStepInput` for the "
            "per-ordinal interpretation."
        ),
    )
    target_revision_id: Optional[str] = Field(
        default=None,
        description=(
            "Revision Identity of the target, when applicable. "
            "Required for ordinals 1, 3, 4 and omitted for ordinals "
            "2, 5."
        ),
    )
    region_id: Optional[str] = Field(
        default=None,
        description=(
            "Region Identity for ordinal 2 (region_occurrence); "
            "omitted for every other ordinal."
        ),
    )
    selection_mode: _SELECTION_MODE_LITERAL = Field(
        default="Pinned",
        description=(
            "Selection mode for the step. AD-WS-12 restricts the slice "
            "to ``'Pinned'``."
        ),
    )
    annotation: Optional[str] = Field(
        default=None,
        max_length=_ANNOTATION_MAX_CHARS,
        description=(
            "Optional 0..2,000-character annotation on the step "
            "(Requirement 9.3)."
        ),
    )


class CreateTrailRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/trails``.

    Mirrors :meth:`walking_slice.trails.TrailService.create_trail`'s
    keyword arguments. ``extra="forbid"`` rejects typo'd field names
    so clients receive an explicit error rather than silently dropped
    attributes. The service performs the *semantic* checks (existence
    of the step targets, authority for the applicable scope); this
    layer only enforces the shape rules that are knowable without
    database access.
    """

    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(
        min_length=_PURPOSE_MIN_CHARS,
        max_length=_PURPOSE_MAX_CHARS,
        description=(
            "Trail purpose text, 1..500 characters (Requirement 9.6)."
        ),
    )
    audience_id: str = Field(
        min_length=_AUDIENCE_MIN_CHARS,
        description="Audience identifier (non-empty, Requirement 9.6).",
    )
    ordering_rationale: Optional[str] = Field(
        default=None,
        max_length=_ORDERING_RATIONALE_MAX_CHARS,
        description=(
            "Optional 0..500-character rationale explaining the step "
            "ordering (Requirement 9.6)."
        ),
    )
    authoring_party_id: str = Field(
        min_length=1,
        description=(
            "Identity of the recording Party. Persisted on "
            "``Trail_Revisions.authoring_party_id`` and the "
            "consequential audit row's ``actor_party_id`` "
            "(Requirements 9.6, 13.1)."
        ),
    )
    scope: Optional[str] = Field(
        default=None,
        description=(
            "Optional scope identifier passed to "
            ":meth:`AuthorizationService.evaluate` as ``target.scope`` "
            "when authorization is wired. Ignored otherwise."
        ),
    )
    steps: list[TrailStepRequestBody] = Field(
        description=(
            "Exactly five Trail Steps. The service layer re-checks the "
            "count, ordinal contiguity, and per-ordinal target kind "
            "(Requirement 9.1, 9.2, 9.7)."
        ),
    )


class AppendTrailRevisionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/trails/{trail_id}/revisions``.

    Identical in shape to :class:`CreateTrailRequestBody` because the
    Trail Revision payload is the same on a create and an append; the
    ``trail_id`` rides in the path so it does not appear here. Material
    change detection happens inside the service layer per
    Requirement 9.4.
    """

    model_config = ConfigDict(extra="forbid")

    purpose: str = Field(
        min_length=_PURPOSE_MIN_CHARS,
        max_length=_PURPOSE_MAX_CHARS,
    )
    audience_id: str = Field(min_length=_AUDIENCE_MIN_CHARS)
    ordering_rationale: Optional[str] = Field(
        default=None, max_length=_ORDERING_RATIONALE_MAX_CHARS
    )
    authoring_party_id: str = Field(min_length=1)
    scope: Optional[str] = Field(default=None)
    steps: list[TrailStepRequestBody]


class TrailStepResponseBody(BaseModel):
    """One Trail Step entry on a response body.

    Mirrors :class:`walking_slice.trails.TrailStepResult`. Returned in
    ordinal order so callers can correlate the issued
    ``trail_step_id`` values back to their submitted step entries.
    """

    model_config = ConfigDict(extra="forbid")

    trail_step_id: str
    ordinal: int
    target_kind: str
    target_id: str
    target_revision_id: Optional[str]
    region_id: Optional[str]
    selection_mode: str
    annotation: Optional[str]


class CreateTrailResponseBody(BaseModel):
    """Successful response from ``POST /api/v1/trails`` (HTTP 201).

    Mirrors :class:`walking_slice.trails.CreateTrailResult`.
    """

    model_config = ConfigDict(extra="forbid")

    trail_id: str
    trail_revision_id: str
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str]
    steps: list[TrailStepResponseBody]
    manifest_id: Optional[str]
    recorded_at: str


class AppendTrailRevisionResponseBody(BaseModel):
    """Successful response from
    ``POST /api/v1/trails/{trail_id}/revisions`` (HTTP 201 on a new
    Revision, HTTP 200 on a no-op).

    Mirrors :class:`walking_slice.trails.AppendTrailRevisionResult`.
    When ``created_new_revision`` is ``False`` the identifier fields
    name the *prior* Revision's rows (Requirement 9.4 — "preserve the
    prior Trail Revision unchanged"); otherwise they name the newly
    inserted Revision's rows.
    """

    model_config = ConfigDict(extra="forbid")

    trail_id: str
    trail_revision_id: str
    predecessor_revision_id: str
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str]
    steps: list[TrailStepResponseBody]
    manifest_id: Optional[str]
    recorded_at: str
    created_new_revision: bool


class TrailRevisionResponseBody(BaseModel):
    """Body returned from
    ``GET /api/v1/trails/{trail_id}/revisions/{revision_id}`` (HTTP 200).

    Mirrors the columns persisted on ``Trail_Revisions`` joined with
    the five ``Trail_Steps`` rows in ordinal order. The
    ``predecessor_revision_id`` field is populated for follow-up
    Revisions and ``None`` for the initial Revision (Requirement 9.4).
    """

    model_config = ConfigDict(extra="forbid")

    trail_id: str
    trail_revision_id: str
    predecessor_revision_id: Optional[str]
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str]
    authoring_party_id: str
    steps: list[TrailStepResponseBody]
    recorded_at: str


class UnresolvedTrailStepBody(BaseModel):
    """One unresolved-step descriptor in a 400 response (Requirement 9.5).

    Mirrors :class:`walking_slice.trails.UnresolvedTrailStep`. The
    400 response carries an ordered list of these descriptors so the
    caller learns *which* step failed to resolve and *what* identifiers
    it carried.
    """

    model_config = ConfigDict(extra="forbid")

    ordinal: int
    target_kind: str
    target_id: str
    target_revision_id: Optional[str]
    region_id: Optional[str]


class ErrorBody(BaseModel):
    """Structured error body returned on 400 / 404 / 503 responses.

    The shape is a superset of the error envelopes used in every other
    ``routes/*.py`` module so a single client-side error handler works
    across the slice. Fields are optional; only the ones relevant to
    the failure are populated.

    The 403 denial response uses :class:`DenialResponseBody` instead
    of this envelope so the AD-WS-9 indistinguishable shape contains
    **only** ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` (no extra fields that could leak information
    about the target — Requirement 7.4).
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    failed_constraint: Optional[str] = None
    trail_id: Optional[str] = None
    trail_revision_id: Optional[str] = None
    audit_failure_indicator: Optional[str] = None
    unresolved_steps: Optional[list[UnresolvedTrailStepBody]] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


class DenialResponseBody(BaseModel):
    """403 response body for a denied Trail creation (AD-WS-9).

    Requirement 7.4 forbids leaking authorized Party identities, target
    contents, role assignment details, or target existence beyond the
    requesting Party's view authority through the denial response.
    AD-WS-9 fixes the indistinguishable response shape to *exactly*
    three fields:

    - ``generic_denial_indicator`` — the constant string ``"denied"``.
    - ``reason_code`` — one of
      ``{not-yet-effective, expired, revoked, out-of-scope,
      no-role-assignment}`` per Requirement 7.2 / 12.2.
    - ``correlation_id`` — the operation correlation identifier
      shared with the (rolled-back) evaluation audit row and the
      separate-transaction Denial Record (Requirement 7.6).

    ``extra="forbid"`` ensures no additional field can be added by
    accident; a model-validation failure here would surface as a 500
    rather than silently leaking information.
    """

    model_config = ConfigDict(extra="forbid")

    generic_denial_indicator: Literal["denied"] = "denied"
    reason_code: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_HEADER_ACTOR: Final[str] = "X-Actor-Party-Id"


def _resolve_actor(header_value: Optional[str], body_value: Optional[str]) -> str:
    """Pick the actor Party Identity from the header or the request body.

    The header wins when both are present so a future authentication
    middleware (task 15.1) can simply set the header unconditionally
    without having to filter the request body. When neither carries a
    value the request is rejected with a 400 — the actor is required
    for every consequential write so the audit row in this transaction
    has a valid ``actor_party_id`` (Requirement 13.1).
    """
    actor = (header_value or "").strip() or (body_value or "").strip()
    if not actor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="actor_party_id_required",
                message=(
                    "actor_party_id must be supplied via the X-Actor-Party-Id "
                    "header or as the 'authoring_party_id' field on the "
                    "request body (placeholder until task 15.1)."
                ),
                missing=["actor_party_id"],
            ).model_dump(),
        )
    return actor


async def _read_json_body(request: Request, *, required: bool) -> Optional[Any]:
    """Read and JSON-decode the request body.

    ``required=True`` rejects empty bodies with a structured 400
    instead of letting Pydantic produce a less-helpful validation
    error. Decode failures map to ``invalid_json_body``.
    """
    raw = await request.body()
    if not raw:
        if required:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorBody(
                    error="empty_request_body",
                    message="A JSON request body is required for this endpoint.",
                ).model_dump(),
            )
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="invalid_json_body",
                message=f"Request body is not valid JSON: {exc.msg}",
            ).model_dump(),
        ) from exc


def _validation_error_to_http(
    exc: ValidationError, *, error_code: str
) -> HTTPException:
    """Convert a Pydantic :class:`ValidationError` to a 400 ``HTTPException``.

    The ``missing`` list extracts field names from errors whose
    Pydantic type is ``missing``. All other errors land in
    ``validation_errors`` so clients see the full detail without the
    non-JSON-serialisable ``ctx['error']`` payload (Pydantic v2
    attaches the original exception object there for ``value_error``
    failures, which breaks JSON encoding).
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
            error=error_code,
            message="Request failed validation.",
            missing=sorted(set(missing)),
            validation_errors=other,
        ).model_dump(),
    )


def _trail_validation_to_http(exc: TrailValidationError) -> HTTPException:
    """Map a :class:`TrailValidationError` to a structured 400.

    The ``failed_constraint`` attribute (one of the values enumerated
    on :class:`TrailValidationError`) becomes both the ``error`` code
    and the ``failed_constraint`` field so a client that picks either
    name finds the same stable identifier. This matches the pattern
    established by every other route module.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _trail_target_unresolved_to_http(
    exc: TrailTargetUnresolvedError,
) -> HTTPException:
    """Map a :class:`TrailTargetUnresolvedError` to a structured 400.

    Per Requirement 9.5 the response identifies each unresolved Trail
    Step by ordinal and target reference, so the body carries a
    typed list of :class:`UnresolvedTrailStepBody` rather than a
    single offending identifier.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.error_code,
            message=str(exc),
            failed_constraint=exc.error_code,
            unresolved_steps=[
                UnresolvedTrailStepBody(
                    ordinal=u.ordinal,
                    target_kind=u.target_kind,
                    target_id=u.target_id,
                    target_revision_id=u.target_revision_id,
                    region_id=u.region_id,
                )
                for u in exc.unresolved_steps
            ],
        ).model_dump(),
    )


def _trail_not_found_to_http(exc: TrailNotFoundError) -> HTTPException:
    """Map a :class:`TrailNotFoundError` to a structured 404.

    Per Requirement 9.4 :meth:`append_revision` is framed as an
    update to an existing Trail; a missing Trail is a 404 not a 400.
    The body carries the offending ``trail_id`` so the caller can
    log or surface the failed identifier.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error=exc.error_code,
            message=str(exc),
            trail_id=exc.trail_id,
        ).model_dump(),
    )


def _trail_authorization_denial_to_http(
    exc: TrailAuthorizationError,
) -> HTTPException:
    """Map a :class:`TrailAuthorizationError` to a 403 response.

    Per AD-WS-9 the denial response carries **only** the
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` fields — no information about *what* was being
    created, *which* role assignment was missing, or *whether* the
    target exists is leaked. Requirement 7.4 makes this leakage
    discipline explicit. We use a dedicated
    :class:`DenialResponseBody` (rather than :class:`ErrorBody`) so
    the model itself enforces the three-field-only shape — adding a
    leak by accident would surface as a Pydantic validation failure
    rather than silently shipping extra fields.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=DenialResponseBody(
            reason_code=exc.reason_code,
            correlation_id=exc.correlation_id,
        ).model_dump(),
    )


def _trail_audit_failure_to_http(
    exc: TrailAuditFailureError,
) -> HTTPException:
    """Map a :class:`TrailAuditFailureError` to a 503 response.

    Per Requirement 7.6: "IF the Audit_Log append for a denied
    Decision attempt fails, THEN THE Authorization_Service SHALL retry
    up to 3 times, keep the action denied, and surface an
    audit-failure indicator to the operator so that denial and audit
    cannot silently diverge." The same retry contract applies to the
    Trail creation deny path; the 503 carries an explicit
    ``audit_failure_indicator`` so an operator-facing surface can
    differentiate this from a routine consequential-audit failure.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error="trail_audit_failed",
            message=str(exc),
            audit_failure_indicator="denial_audit_unavailable",
        ).model_dump(),
    )


def _audit_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map an :class:`AuditAppendError` to a 503 response.

    Audit append failures roll back the surrounding transaction (the
    Trail header, Trail Revision, Trail Steps, and Provenance Manifest
    are discarded), which is the behaviour Requirement 13.6
    prescribes. The 503 status code matches the contract used in
    every other route module so a single client-side handler covers
    every consequential write.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error="audit_append_failed",
            message=str(exc),
        ).model_dump(),
    )


def _manifest_validation_to_http(
    exc: ManifestValidationError,
) -> HTTPException:
    """Map a :class:`ManifestValidationError` to a structured 400.

    The wired :class:`~walking_slice.manifests.ProvenanceManifestWriter`
    raises this exception when an Included Source fails Requirement
    10.x validation; surface as a structured 400 with the writer's
    ``failed_constraint`` so a client picks up the same stable
    identifier the service emits.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _manifest_persistence_failure_to_http(exc: Exception) -> HTTPException:
    """Map an unexpected manifest-persistence exception to a 503 response.

    Per Requirement 10.6 / design §"Provenance manifest persistence
    failure": when the originating Trail transaction fails because
    the Provenance Manifest could not be persisted, the whole
    synthesis rolls back and
    ``503 provenance_manifest_persistence_failed`` is returned.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error="provenance_manifest_persistence_failed",
            message=str(exc),
        ).model_dump(),
    )


def _steps_from_body(steps: list[TrailStepRequestBody]) -> tuple[TrailStepInput, ...]:
    """Convert request-body steps to the service's :class:`TrailStepInput`.

    The conversion preserves field order so the service's
    :meth:`_validate_steps` helper sees the entries in submission
    order; structural validation then sorts them by ordinal before
    persisting.
    """
    return tuple(
        TrailStepInput(
            ordinal=step.ordinal,
            target_kind=step.target_kind,
            target_id=step.target_id,
            target_revision_id=step.target_revision_id,
            region_id=step.region_id,
            selection_mode=step.selection_mode,
            annotation=step.annotation,
        )
        for step in steps
    )


def _create_result_to_response(
    result: CreateTrailResult,
) -> CreateTrailResponseBody:
    """Map a :class:`CreateTrailResult` to its response body."""
    return CreateTrailResponseBody(
        trail_id=result.trail_id,
        trail_revision_id=result.trail_revision_id,
        purpose=result.purpose,
        audience_id=result.audience_id,
        ordering_rationale=result.ordering_rationale,
        steps=[
            TrailStepResponseBody(
                trail_step_id=step.trail_step_id,
                ordinal=step.ordinal,
                target_kind=step.target_kind,
                target_id=step.target_id,
                target_revision_id=step.target_revision_id,
                region_id=step.region_id,
                selection_mode=step.selection_mode,
                annotation=step.annotation,
            )
            for step in result.steps
        ],
        manifest_id=result.manifest_id,
        recorded_at=result.recorded_at,
    )


def _append_result_to_response(
    result: AppendTrailRevisionResult,
) -> AppendTrailRevisionResponseBody:
    """Map an :class:`AppendTrailRevisionResult` to its response body."""
    return AppendTrailRevisionResponseBody(
        trail_id=result.trail_id,
        trail_revision_id=result.trail_revision_id,
        predecessor_revision_id=result.predecessor_revision_id,
        purpose=result.purpose,
        audience_id=result.audience_id,
        ordering_rationale=result.ordering_rationale,
        steps=[
            TrailStepResponseBody(
                trail_step_id=step.trail_step_id,
                ordinal=step.ordinal,
                target_kind=step.target_kind,
                target_id=step.target_id,
                target_revision_id=step.target_revision_id,
                region_id=step.region_id,
                selection_mode=step.selection_mode,
                annotation=step.annotation,
            )
            for step in result.steps
        ],
        manifest_id=result.manifest_id,
        recorded_at=result.recorded_at,
        created_new_revision=result.created_new_revision,
    )


# Reference to the public ``ORDINAL_TARGET_KIND`` mapping so a future
# diagnostic surface (or doc generator) can introspect the per-ordinal
# expectation without re-importing :mod:`walking_slice.trails`. The
# field-level :data:`_TARGET_KIND_LITERAL` is the wire-contract source
# of truth; this constant keeps both visible side-by-side.
_ORDINAL_TARGET_KIND_REFERENCE: Final = ORDINAL_TARGET_KIND


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/trails",
    response_model=CreateTrailResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Create a Trail Resource plus its initial Trail Revision and "
        "five Trail Steps."
    ),
)
async def create_trail(
    request: Request,
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    trail_service: TrailService = Depends(get_trail_service),
) -> CreateTrailResponseBody:
    """Create a Trail (Resource + first Revision) plus its five steps.

    The endpoint:

    1. Reads and JSON-decodes the request body (400 on empty / malformed).
    2. Validates the body against :class:`CreateTrailRequestBody`; any
       :class:`~pydantic.ValidationError` becomes a 400 ``ErrorBody``
       naming the missing or invalid fields.
    3. Resolves the actor Party Identity from the header (fallback to
       the body's ``authoring_party_id`` per :func:`_resolve_actor`).
       The header takes precedence so a future middleware can simply
       set it unconditionally.
    4. Calls :meth:`TrailService.create_trail` inside one
       ``engine.begin()`` transaction so the Trails header, Trail
       Revision, five Trail Steps, optional Provenance Manifest, the
       ``Identifier_Registry`` bindings, and the ``Audit_Records``
       consequential row commit together (AD-WS-5, Requirement 13.1).
       The ``engine`` is also passed to the service so the deny path
       (when authorization is wired) can persist its Denial Record on
       a separate transaction that survives the caller's rollback
       (Requirement 7.6).

    Exception mapping (per task 10.3):

    - :class:`TrailValidationError` → 400 with ``failed_constraint``.
    - :class:`TrailTargetUnresolvedError` → 400 with per-ordinal
      ``unresolved_steps`` list (Requirement 9.5).
    - :class:`TrailAuthorizationError` → 403 with the AD-WS-9
      indistinguishable denial response shape.
    - :class:`TrailAuditFailureError` → 503 with
      ``audit_failure_indicator``.
    - :class:`AuditAppendError` → 503.
    - Manifest persistence failures → 503
      ``provenance_manifest_persistence_failed`` (Requirement 10.6).
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = CreateTrailRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="invalid_trail_request"
        ) from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.authoring_party_id)
    service_steps = _steps_from_body(body.steps)

    try:
        with engine.begin() as connection:
            result = trail_service.create_trail(
                connection,
                purpose=body.purpose,
                audience_id=body.audience_id,
                steps=service_steps,
                authoring_party_id=actor_party_id,
                ordering_rationale=body.ordering_rationale,
                scope=body.scope,
                engine=engine,
            )
    except TrailValidationError as exc:
        raise _trail_validation_to_http(exc) from exc
    except TrailTargetUnresolvedError as exc:
        raise _trail_target_unresolved_to_http(exc) from exc
    except TrailAuthorizationError as exc:
        raise _trail_authorization_denial_to_http(exc) from exc
    except TrailAuditFailureError as exc:
        raise _trail_audit_failure_to_http(exc) from exc
    except ManifestValidationError as exc:
        raise _manifest_validation_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_failure_to_http(exc) from exc
    except HTTPException:
        # Already a structured HTTP error; re-raise without wrapping.
        raise
    except Exception as exc:
        # Catch-all for any unexpected exception raised inside the
        # ``engine.begin()`` block — typically a manifest-persistence
        # failure surfaced by the wired
        # :class:`ProvenanceManifestWriter` (Requirement 10.6 /
        # design §"Provenance manifest persistence failure"). The
        # transaction has already rolled back so no Trail, Trail
        # Revision, Trail Step, manifest, or consequential audit row
        # was persisted.
        raise _manifest_persistence_failure_to_http(exc) from exc

    return _create_result_to_response(result)


@router.post(
    "/trails/{trail_id}/revisions",
    response_model=AppendTrailRevisionResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Append a new Trail Revision when the canonical form differs "
        "materially."
    ),
)
async def append_trail_revision(
    request: Request,
    trail_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    trail_service: TrailService = Depends(get_trail_service),
) -> AppendTrailRevisionResponseBody:
    """Append a new Trail Revision (Requirement 9.4).

    The service compares the canonical form of the submission against
    the prior Revision's canonical form. On a difference a new
    immutable Trail Revision is inserted with
    ``predecessor_revision_id`` pointing at the prior Revision; on
    byte equivalence the existing Revision is returned and no new
    row is inserted (the response carries
    ``created_new_revision=false``).

    The endpoint follows the same flow and exception mapping as
    :func:`create_trail` with one additional path:

    - :class:`TrailNotFoundError` → 404 with ``trail_id``.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = AppendTrailRevisionRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="invalid_trail_revision_request"
        ) from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.authoring_party_id)
    service_steps = _steps_from_body(body.steps)

    try:
        with engine.begin() as connection:
            result = trail_service.append_revision(
                connection,
                trail_id=trail_id,
                purpose=body.purpose,
                audience_id=body.audience_id,
                steps=service_steps,
                authoring_party_id=actor_party_id,
                ordering_rationale=body.ordering_rationale,
                scope=body.scope,
                engine=engine,
            )
    except TrailNotFoundError as exc:
        raise _trail_not_found_to_http(exc) from exc
    except TrailValidationError as exc:
        raise _trail_validation_to_http(exc) from exc
    except TrailTargetUnresolvedError as exc:
        raise _trail_target_unresolved_to_http(exc) from exc
    except TrailAuthorizationError as exc:
        raise _trail_authorization_denial_to_http(exc) from exc
    except TrailAuditFailureError as exc:
        raise _trail_audit_failure_to_http(exc) from exc
    except ManifestValidationError as exc:
        raise _manifest_validation_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_failure_to_http(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _manifest_persistence_failure_to_http(exc) from exc

    return _append_result_to_response(result)


@router.get(
    "/trails/{trail_id}/revisions/{revision_id}",
    response_model=TrailRevisionResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Read a persisted Trail Revision with its five Trail Steps.",
)
async def read_trail_revision(
    trail_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> TrailRevisionResponseBody:
    """Return the Trail Revision matching ``(trail_id, revision_id)``.

    Direct SQL lookup against ``Trail_Revisions`` filtered on
    ``trail_id`` so a caller that passes a Revision Identity belonging
    to a different Trail still gets a 404 (not silently redirected to
    whatever Trail actually owns the Revision). The five
    ``Trail_Steps`` rows are loaded in ordinal order so the response
    walks the Trail from Source Document Revision (ordinal 1) through
    the Decision (ordinal 5).

    A read operation; no audit row is appended (reads are non-
    consequential in this slice — design §"Audit_Log").
    """
    with engine.connect() as connection:
        revision_row = (
            connection.execute(
                text(
                    """
                    SELECT trail_revision_id, trail_id,
                           predecessor_revision_id, purpose, audience_id,
                           ordering_rationale, authoring_party_id,
                           recorded_at
                      FROM Trail_Revisions
                     WHERE trail_revision_id = :revision_id
                       AND trail_id = :trail_id
                    """
                ),
                {"trail_id": trail_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
        if revision_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=ErrorBody(
                    error="trail_revision_not_found",
                    message=(
                        f"No Trail_Revisions row for trail_id={trail_id!r}, "
                        f"trail_revision_id={revision_id!r}."
                    ),
                    trail_id=trail_id,
                    trail_revision_id=revision_id,
                ).model_dump(),
            )

        step_rows = (
            connection.execute(
                text(
                    """
                    SELECT trail_step_id, ordinal, target_kind, target_id,
                           target_revision_id, region_id, selection_mode,
                           annotation
                      FROM Trail_Steps
                     WHERE trail_revision_id = :revision_id
                     ORDER BY ordinal
                    """
                ),
                {"revision_id": revision_id},
            )
            .mappings()
            .all()
        )

    return TrailRevisionResponseBody(
        trail_id=revision_row["trail_id"],
        trail_revision_id=revision_row["trail_revision_id"],
        predecessor_revision_id=revision_row["predecessor_revision_id"],
        purpose=revision_row["purpose"],
        audience_id=revision_row["audience_id"],
        ordering_rationale=revision_row["ordering_rationale"],
        authoring_party_id=revision_row["authoring_party_id"],
        steps=[
            TrailStepResponseBody(
                trail_step_id=row["trail_step_id"],
                ordinal=row["ordinal"],
                target_kind=row["target_kind"],
                target_id=row["target_id"],
                target_revision_id=row["target_revision_id"],
                region_id=row["region_id"],
                selection_mode=row["selection_mode"],
                annotation=row["annotation"],
            )
            for row in step_rows
        ],
        recorded_at=revision_row["recorded_at"],
    )
