"""HTTP routes for the Knowledge_Service Decisions surface (task 8.3).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Knowledge_Service" HTTP surface (Decisions rows) plus AD-WS-5
(audit/manifest append inside the originating transaction), AD-WS-9
(indistinguishable denial response shape), AD-WS-10 (authority-basis
enumeration), and AD-WS-11 (slice-restricted outcomes
``{Accept, Reject, Defer}``):

| Method | Path                                                              | Purpose                                                                                  |
|--------|-------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| POST   | ``/api/v1/recommendations/{rec_id}/decisions``                    | Create a Decision Immutable Record plus its ``Addresses`` Relationship, Provenance Manifest, and any Omission Entries (Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.7; AD-WS-5). |
| GET    | ``/api/v1/decisions/{decision_id}``                               | Read a single Decision Immutable Record row.                                             |

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates the consequential
  write to :meth:`walking_slice.knowledge.KnowledgeService.create_decision`
  inside one ``engine.begin()`` transaction so the Decision row, the
  ``Addresses`` Relationship, the Provenance Manifest, every Omission
  Entry, the ``Identifier_Registry`` binding, and the ``Audit_Records``
  consequential row commit together (AD-WS-5, Requirement 6.4, 13.1).
- Pre-validate request shapes with Pydantic v2 :class:`~pydantic.BaseModel`
  definitions using ``extra="forbid"`` so typo'd field names are
  rejected, and convert any :class:`~pydantic.ValidationError` to a
  structured ``HTTP 400`` (instead of FastAPI's default 422) so the
  wire contract is uniform with the deeper service-layer responses.
- Map service exceptions to the codes listed in the task description:

  - :class:`~walking_slice.knowledge.DecisionValidationError`
    → ``400`` with ``failed_constraint``.
  - :class:`~walking_slice.knowledge.RecommendationRevisionNotResolvableError`
    → ``404`` with ``target_recommendation_id`` and
    ``target_recommendation_revision_id``.
  - :class:`~walking_slice.knowledge.DecisionConflictError`
    → ``409`` with ``existing_decision_id``.
  - :class:`~walking_slice.knowledge.DecisionAuthorizationError`
    → ``403`` with the AD-WS-9 indistinguishable-denial shape carrying
    **only** ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` (no other fields — Requirement 7.4).
  - :class:`~walking_slice.knowledge.DecisionAuditFailureError`
    → ``503`` with an ``audit_failure_indicator`` (Requirement 7.6).
  - :class:`~walking_slice.audit.AuditAppendError`
    → ``503``.

**Authentication.** This module accepts the actor's Party Identity from
the temporary ``X-Actor-Party-Id`` header. Task 15.1 will replace this
placeholder with the bearer-token authenticated ``RequestContext``
described in design §"Application-Level Composition". The header-based
shim mirrors the pattern in :mod:`walking_slice.routes.roles`,
:mod:`walking_slice.routes.evidence`,
:mod:`walking_slice.routes.findings`, and
:mod:`walking_slice.routes.recommendations` so a single future
middleware change swaps every route module in one go.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope. The factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly; tests (and the eventual ``app.py`` in task 15.2) override them
via :data:`fastapi.FastAPI.dependency_overrides`.

Requirements satisfied (per task 8.3):
    6.1 — ``POST /api/v1/recommendations/{rec_id}/decisions`` records a
          Decision Immutable Record targeting the named Recommendation
          Revision.
    7.1 — Unauthorized callers (no effective Decision-Maker role for
          the applicable scope) are rejected with the AD-WS-9
          indistinguishable denial response shape and no Decision,
          Addresses Relationship, Provenance Manifest, or Omission
          Entry is persisted.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError
from walking_slice.knowledge import (
    DecisionAuditFailureError,
    DecisionAuthorizationError,
    DecisionConflictError,
    DecisionOmissionEntry,
    DecisionValidationError,
    KnowledgeService,
    RecommendationRevisionNotResolvableError,
)
from walking_slice.manifests import ManifestValidationError
from walking_slice.models import AuthorityBasisRef


__all__ = [
    "AuthorityBasisRequestBody",
    "CreateDecisionRequestBody",
    "CreateDecisionResponseBody",
    "DecisionOmissionEntryRequestBody",
    "DecisionResponseBody",
    "DenialResponseBody",
    "ErrorBody",
    "get_engine",
    "get_knowledge_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["decisions"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# These factories are deliberately stubs; task 15.2 wires concrete
# implementations through ``walking_slice.app.create_app``. Tests override
# them on the per-test :class:`fastapi.FastAPI` instance via
# ``app.dependency_overrides[get_engine] = lambda: engine`` etc., per the
# pattern recommended in the FastAPI docs and used elsewhere in the slice.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    Overridden in tests and in the application composition layer
    (task 15.2). Never called unwrapped from a route handler.
    """
    raise NotImplementedError(
        "walking_slice.routes.decisions.get_engine must be overridden "
        "by app composition (task 15.2) or test fixtures."
    )


def get_knowledge_service() -> KnowledgeService:
    """Provide the slice's :class:`KnowledgeService` singleton.

    The service is connection-scoped at call time — every public method
    accepts the caller's SQLAlchemy connection — so a single instance
    serves all requests safely. When the service is wired with an
    :class:`~walking_slice.authorization.AuthorizationService` the
    Requirement 7.1 authority check is enforced; otherwise the
    back-compat path (no enforcement) is exercised. The test app
    composition wires authorization unconditionally; production
    (task 15.2) does the same.
    """
    raise NotImplementedError(
        "walking_slice.routes.decisions.get_knowledge_service must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Validation limits mirrored from :mod:`walking_slice.knowledge` so the
# Pydantic schema can short-circuit obvious shape violations before a
# database connection is opened. The service layer re-validates
# defensively — the duplication catches structural errors without
# coupling the route's ``Field`` constraints to a private symbol.
# ---------------------------------------------------------------------------

_RATIONALE_MIN_CHARS: Final[int] = 1
_RATIONALE_MAX_CHARS: Final[int] = 4_000
_OMISSION_RATIONALE_MIN_CHARS: Final[int] = 1
_OMISSION_RATIONALE_MAX_CHARS: Final[int] = 2_000

_OUTCOME_LITERAL = Literal["Accept", "Reject", "Defer"]
_AUTHORITY_BASIS_TYPE_LITERAL = Literal[
    "role-grant-id", "scope-id", "delegation-chain-id"
]
_OMISSION_CATEGORY_LITERAL = Literal[
    "intentional", "unavailable", "restricted", "stale", "unresolved"
]


# ---------------------------------------------------------------------------
# Pydantic v2 boundary models.
# ---------------------------------------------------------------------------


class AuthorityBasisRequestBody(BaseModel):
    """Authority basis sub-object on the create-decision request body.

    Mirrors :class:`walking_slice.models.AuthorityBasisRef`. The
    ``type`` Literal mirrors AD-WS-10 (the slice-restricted authority
    basis enumeration); the ``id`` field is the identifier of the
    specific role-grant, scope, or delegation chain a Party invokes
    when finalizing the Decision.

    ``extra="forbid"`` keeps the wire contract sharp: a typo'd
    ``type_`` or ``ids`` field surfaces as a structured 400 rather
    than being silently dropped.
    """

    model_config = ConfigDict(extra="forbid")

    type: _AUTHORITY_BASIS_TYPE_LITERAL = Field(
        description=(
            "Authority-basis type from AD-WS-10's enumeration "
            "``{role-grant-id, scope-id, delegation-chain-id}``."
        ),
    )
    id: UUID = Field(
        description=(
            "Identifier of the specific role-grant, scope, or "
            "delegation chain the Party invokes."
        ),
    )


class DecisionOmissionEntryRequestBody(BaseModel):
    """Optional material-source omission entry on the request body.

    Mirrors :class:`walking_slice.knowledge.DecisionOmissionEntry`.
    Each entry becomes one ``Omission_Entries`` row inside the same
    transaction as the Decision (AD-WS-5). Requirement 10.3 fixes the
    category enumeration; Requirement 10.2 fixes the rationale range.
    """

    model_config = ConfigDict(extra="forbid")

    excluded_source_id: str = Field(
        min_length=1,
        description="Resource Identity of the omitted material source.",
    )
    excluded_source_revision_id: Optional[str] = Field(
        default=None,
        description=(
            "Revision Identity of the omitted source when known; "
            "``None`` when only the Resource Identity is known "
            "(Requirement 10.2)."
        ),
    )
    category: _OMISSION_CATEGORY_LITERAL = Field(
        description=(
            "Omission category from Requirement 10.3's enumeration "
            "``{intentional, unavailable, restricted, stale, "
            "unresolved}``."
        ),
    )
    rationale: str = Field(
        min_length=_OMISSION_RATIONALE_MIN_CHARS,
        max_length=_OMISSION_RATIONALE_MAX_CHARS,
        description=(
            "Free-form rationale of 1..2,000 characters explaining the "
            "exclusion (Requirement 10.2)."
        ),
    )


class CreateDecisionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/recommendations/{rec_id}/decisions``.

    Mirrors
    :meth:`walking_slice.knowledge.KnowledgeService.create_decision`'s
    keyword arguments. The ``target_recommendation_id`` travels in the
    path; this body provides the matching Revision Identity, the
    outcome, the rationale, the deciding Party, the authority basis,
    the applicable scope, and any Omission Entries.

    ``extra="forbid"`` rejects typo'd field names so clients receive an
    explicit error rather than silently dropped attributes. The service
    performs the *semantic* checks (existence of the Recommendation
    Revision, uniqueness of the Decision target, authority for the
    applicable scope); this layer only enforces the shape rules that
    are knowable without database access.
    """

    model_config = ConfigDict(extra="forbid")

    target_recommendation_revision_id: str = Field(
        min_length=1,
        description=(
            "Recommendation Revision Identity the Decision targets. "
            "The composite pair (``rec_id`` from the path, this value) "
            "must resolve to an existing ``Recommendation_Revisions`` "
            "row (Requirement 6.1)."
        ),
    )
    outcome: _OUTCOME_LITERAL = Field(
        description=(
            "Decision outcome restricted to AD-WS-11's slice "
            "enumeration ``{Accept, Reject, Defer}``."
        ),
    )
    rationale: str = Field(
        min_length=_RATIONALE_MIN_CHARS,
        max_length=_RATIONALE_MAX_CHARS,
        description=(
            "Decision rationale of 1..4,000 characters "
            "(Requirement 6.2)."
        ),
    )
    deciding_party_id: str = Field(
        min_length=1,
        description=(
            "Identity of the deciding Party. Persisted on "
            "``Decisions.deciding_party_id``, every Addresses "
            "Relationship's ``authoring_party_id``, the Provenance "
            "Manifest's ``authoring_party_id``, every Omission "
            "Entry's ``authoring_party_id``, and the consequential "
            "audit row's ``actor_party_id`` (Requirements 6.2, 13.1)."
        ),
    )
    authority_basis: AuthorityBasisRequestBody = Field(
        description=(
            "Authority basis the Party invokes to finalize the "
            "Decision (Requirement 6.2, AD-WS-10)."
        ),
    )
    applicable_scope: str = Field(
        min_length=1,
        description=(
            "Scope identifier the Decision applies within. Passed as "
            "``target.scope`` to "
            ":meth:`AuthorizationService.evaluate` so the wired role "
            "assignment must cover the same scope (Requirement 7.1)."
        ),
    )
    omissions: list[DecisionOmissionEntryRequestBody] = Field(
        default_factory=list,
        description=(
            "Optional material-source omissions. Each entry becomes "
            "one ``Omission_Entries`` row inside the same transaction "
            "as the Decision (AD-WS-5)."
        ),
    )


class CreateDecisionResponseBody(BaseModel):
    """Successful response from
    ``POST /api/v1/recommendations/{rec_id}/decisions`` (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    target_recommendation_id: str
    target_recommendation_revision_id: str
    outcome: str
    rationale: str
    deciding_party_id: str
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str
    addresses_relationship_id: str
    manifest_id: str
    omission_entry_ids: list[str]
    recorded_at: str


class DecisionResponseBody(BaseModel):
    """Body returned from ``GET /api/v1/decisions/{decision_id}`` (HTTP 200).

    Mirrors the columns persisted on ``Decisions``. The ``Decisions``
    table is a single-row immutable record (AD-WS-3 / AD-WS-4 — a
    Decision has no revisions); this response therefore returns the
    one row directly rather than a list.
    """

    model_config = ConfigDict(extra="forbid")

    decision_id: str
    target_recommendation_id: str
    target_recommendation_revision_id: str
    outcome: str
    rationale: str
    deciding_party_id: str
    authority_basis: AuthorityBasisRequestBody
    applicable_scope: str
    recorded_at: str


class ErrorBody(BaseModel):
    """Structured error body returned on 400 / 404 / 409 / 503 responses.

    The shape is a superset of the error envelopes used in
    :mod:`walking_slice.routes.findings`,
    :mod:`walking_slice.routes.evidence`,
    :mod:`walking_slice.routes.recommendations`, and
    :mod:`walking_slice.routes.roles` so a single client-side error
    handler works across every route module. Fields are optional; only
    the ones relevant to the failure are populated.

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
    target_recommendation_id: Optional[str] = None
    target_recommendation_revision_id: Optional[str] = None
    existing_decision_id: Optional[str] = None
    decision_id: Optional[str] = None
    audit_failure_indicator: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


class DenialResponseBody(BaseModel):
    """403 response body for a denied Decision attempt (AD-WS-9).

    Requirement 7.4 forbids leaking authorized Party identities,
    Recommendation contents, role assignment details, or target
    existence beyond the requesting Party's view authority through the
    denial response. AD-WS-9 fixes the indistinguishable response
    shape to *exactly* three fields:

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
                    "header or as the 'deciding_party_id' field on the "
                    "request body (placeholder until task 15.1)."
                ),
                missing=["actor_party_id"],
            ).model_dump(),
        )
    return actor


async def _read_json_body(request: Request, *, required: bool) -> Optional[Any]:
    """Read and JSON-decode the request body.

    ``required=True`` rejects empty bodies with a structured 400 instead
    of letting Pydantic produce a less-helpful validation error. Decode
    failures map to ``invalid_json_body``.
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

    The ``missing`` list extracts field names from errors whose Pydantic
    type is ``missing``. All other errors land in ``validation_errors``
    so clients see the full detail without the non-JSON-serialisable
    ``ctx['error']`` payload (Pydantic v2 attaches the original
    exception object there for ``value_error`` failures, which breaks
    JSON encoding).
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


def _decision_validation_to_http(
    exc: DecisionValidationError,
) -> HTTPException:
    """Map a :class:`DecisionValidationError` to a structured 400.

    The ``failed_constraint`` attribute (one of the values enumerated
    on :class:`DecisionValidationError`) becomes both the ``error``
    code and the ``failed_constraint`` field so a client that picks
    either name finds the same stable identifier. This matches the
    pattern established by every other route module.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _recommendation_revision_not_resolvable_to_http(
    exc: RecommendationRevisionNotResolvableError,
) -> HTTPException:
    """Map a :class:`RecommendationRevisionNotResolvableError` to a 404.

    Per the task description the response carries the supplied
    ``target_recommendation_id`` and ``target_recommendation_revision_id``
    so the caller learns *which* pair failed to resolve.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="recommendation_revision_not_found",
            message=str(exc),
            target_recommendation_id=exc.target_recommendation_id,
            target_recommendation_revision_id=(
                exc.target_recommendation_revision_id
            ),
        ).model_dump(),
    )


def _decision_conflict_to_http(exc: DecisionConflictError) -> HTTPException:
    """Map a :class:`DecisionConflictError` to a 409 response.

    The body carries ``existing_decision_id`` so the caller can
    discover the prior Decision and act accordingly (Requirement 6.5).
    The HTTP status follows the design §"Error Handling" table:
    "Duplicate Decision … 409 with the duplicate-decision indicator".
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=ErrorBody(
            error="duplicate_decision",
            message=str(exc),
            failed_constraint="duplicate_decision",
            target_recommendation_id=exc.target_recommendation_id,
            target_recommendation_revision_id=(
                exc.target_recommendation_revision_id
            ),
            existing_decision_id=exc.existing_decision_id,
        ).model_dump(),
    )


def _authorization_denial_to_http(
    exc: DecisionAuthorizationError,
) -> HTTPException:
    """Map a :class:`DecisionAuthorizationError` to a 403 response.

    Per AD-WS-9 the denial response carries **only** the
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` fields — no information about *what* was being
    decided, *which* role assignment was missing, or *whether* the
    target exists is leaked. Requirement 7.4 makes this leakage
    discipline explicit for the denied-Decision response. We use a
    dedicated :class:`DenialResponseBody` (rather than
    :class:`ErrorBody`) so the model itself enforces the
    three-field-only shape — adding a leak by accident would
    surface as a Pydantic validation failure rather than silently
    shipping extra fields.

    The 403 status code follows the design §"Error Handling" table:
    "Authorization denial … 403 for action-authority-missing on a
    visible target".
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=DenialResponseBody(
            reason_code=exc.reason_code,
            correlation_id=exc.correlation_id,
        ).model_dump(),
    )


def _decision_audit_failure_to_http(
    exc: DecisionAuditFailureError,
) -> HTTPException:
    """Map a :class:`DecisionAuditFailureError` to a 503 response.

    Per Requirement 7.6: "IF the Audit_Log append for a denied
    Decision attempt fails, THEN THE Authorization_Service SHALL retry
    up to 3 times, keep the action denied, and surface an
    audit-failure indicator to the operator so that denial and audit
    cannot silently diverge." The 503 carries an explicit
    ``audit_failure_indicator`` so an operator-facing surface can
    differentiate this from a routine consequential-audit failure.

    The response intentionally does *not* carry the AD-WS-9 denial
    shape — the operator-facing surface needs the audit-failure
    signal, and Requirement 7.6's "surface an audit-failure indicator
    to the operator" takes precedence over the denial-information
    leakage discipline at the 503 status code (which is itself
    operator-facing rather than requester-facing).
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error="decision_audit_failed",
            message=str(exc),
            audit_failure_indicator="denial_audit_unavailable",
        ).model_dump(),
    )


def _audit_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map an :class:`AuditAppendError` to a 503 response.

    Audit append failures roll back the surrounding transaction (the
    Decisions row, the Addresses Relationship, the Provenance
    Manifest, and any Omission Entries are discarded), which is the
    behaviour Requirement 13.6 prescribes. The 503 status code matches
    the contract used in every other route module so a single
    client-side handler covers every consequential write.
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
    raises this exception when an Included Source or Omission Entry
    fails Requirement 10.x validation; surface as a structured 400
    with the writer's ``failed_constraint`` so a client picks up the
    same stable identifier the service emits.
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
    failure": when the originating Decision transaction fails because
    the Provenance Manifest could not be persisted, the whole
    synthesis rolls back and
    ``503 provenance_manifest_persistence_failed`` is returned. The
    Decision, Addresses Relationship, Omission Entries, and
    consequential audit row are all part of the same transaction so
    none of them are observable after the rollback.
    """
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=ErrorBody(
            error="provenance_manifest_persistence_failed",
            message=str(exc),
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/recommendations/{recommendation_id}/decisions",
    response_model=CreateDecisionResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary=(
        "Create a Decision Immutable Record plus its Addresses "
        "Relationship, Provenance Manifest, and Omission Entries."
    ),
)
async def create_decision(
    request: Request,
    recommendation_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> CreateDecisionResponseBody:
    """Create a Decision Immutable Record.

    The endpoint:

    1. Reads and JSON-decodes the request body (400 on empty / malformed).
    2. Validates the body against :class:`CreateDecisionRequestBody`;
       any :class:`~pydantic.ValidationError` becomes a 400
       ``ErrorBody`` naming the missing or invalid fields.
    3. Resolves the actor Party Identity from the header (fallback to
       the body's ``deciding_party_id`` per :func:`_resolve_actor`).
       The header takes precedence so a future middleware can simply
       set it unconditionally.
    4. Calls :meth:`KnowledgeService.create_decision` inside one
       ``engine.begin()`` transaction so the Decisions row, the
       Addresses Relationship, the Provenance Manifest, every
       Omission Entry, the ``Identifier_Registry`` binding, and the
       ``Audit_Records`` consequential row commit together
       (AD-WS-5, Requirements 6.4, 13.1). The ``engine`` is *also*
       passed through to the service so the deny path's
       separate-transaction Denial Record (Requirement 7.6) can be
       written outside the caller's transaction.

    Returns HTTP 201 with the Decision identifiers, the persisted
    attributes, the Addresses Relationship identifier, the Provenance
    Manifest identifier, the ordered Omission Entry identifiers, and
    the recorded timestamp.

    Exception mapping (per task 8.3):

    - :class:`DecisionValidationError` → 400 with ``failed_constraint``.
    - :class:`RecommendationRevisionNotResolvableError` → 404 with the
      offending Recommendation Identity pair.
    - :class:`DecisionConflictError` → 409 with ``existing_decision_id``.
    - :class:`DecisionAuthorizationError` → 403 with the AD-WS-9
      indistinguishable denial shape (``generic_denial_indicator``,
      ``reason_code``, ``correlation_id`` only).
    - :class:`DecisionAuditFailureError` → 503 with the
      ``audit_failure_indicator``.
    - :class:`AuditAppendError` → 503.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = CreateDecisionRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="invalid_decision_request"
        ) from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.deciding_party_id)

    authority_basis = AuthorityBasisRef(
        type=body.authority_basis.type,
        id=body.authority_basis.id,
    )
    omissions = tuple(
        DecisionOmissionEntry(
            excluded_source_id=entry.excluded_source_id,
            excluded_source_revision_id=entry.excluded_source_revision_id,
            category=entry.category,
            rationale=entry.rationale,
        )
        for entry in body.omissions
    )

    try:
        with engine.begin() as connection:
            result = knowledge_service.create_decision(
                connection,
                target_recommendation_id=recommendation_id,
                target_recommendation_revision_id=(
                    body.target_recommendation_revision_id
                ),
                outcome=body.outcome,
                rationale=body.rationale,
                deciding_party_id=actor_party_id,
                authority_basis=authority_basis,
                applicable_scope=body.applicable_scope,
                omissions=omissions,
                engine=engine,
            )
    except DecisionAuthorizationError as exc:
        # AD-WS-9 indistinguishable denial. Raised *before* any
        # Decisions / Relationships / Provenance_Manifests /
        # Omission_Entries / consequential Audit_Records row is
        # inserted, so the surrounding transaction rolls back
        # carrying nothing. The Denial Record was already appended
        # in a separate transaction by the service so the audit
        # trail of the denial survives the rollback (Requirement
        # 7.6).
        raise _authorization_denial_to_http(exc) from exc
    except DecisionAuditFailureError as exc:
        # Denial-and-audit divergence per Requirement 7.6. The
        # action remains denied (the Decision was not persisted) and
        # we surface an audit-failure indicator so the operator can
        # investigate.
        raise _decision_audit_failure_to_http(exc) from exc
    except RecommendationRevisionNotResolvableError as exc:
        raise _recommendation_revision_not_resolvable_to_http(exc) from exc
    except DecisionConflictError as exc:
        raise _decision_conflict_to_http(exc) from exc
    except DecisionValidationError as exc:
        raise _decision_validation_to_http(exc) from exc
    except ManifestValidationError as exc:
        raise _manifest_validation_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_failure_to_http(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Catch-all for any unexpected exception during the
        # ``engine.begin()`` block — typically a manifest-persistence
        # failure surfaced by the wired
        # :class:`ProvenanceManifestWriter` (Requirement 10.6 /
        # design §"Provenance manifest persistence failure"). The
        # transaction has already rolled back so no Decision,
        # Addresses Relationship, Provenance Manifest, Omission
        # Entry, or consequential audit row was persisted.
        raise _manifest_persistence_failure_to_http(exc) from exc

    return CreateDecisionResponseBody(
        decision_id=result.decision_id,
        target_recommendation_id=result.target_recommendation_id,
        target_recommendation_revision_id=(
            result.target_recommendation_revision_id
        ),
        outcome=result.outcome,
        rationale=result.rationale,
        deciding_party_id=result.deciding_party_id,
        authority_basis=AuthorityBasisRequestBody(
            type=result.authority_basis_type,  # type: ignore[arg-type]
            id=UUID(result.authority_basis_id),
        ),
        applicable_scope=result.applicable_scope,
        addresses_relationship_id=result.addresses_relationship_id,
        manifest_id=result.manifest_id,
        omission_entry_ids=list(result.omission_entry_ids),
        recorded_at=result.recorded_at,
    )


@router.get(
    "/decisions/{decision_id}",
    response_model=DecisionResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Read a persisted Decision Immutable Record.",
)
async def read_decision(
    decision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> DecisionResponseBody:
    """Return the Decision matching ``decision_id``.

    Direct SQL lookup against ``Decisions``. A Decision has no
    revisions (AD-WS-3 / AD-WS-4 — it is the Immutable Record itself),
    so the GET path is keyed on a single identifier rather than the
    ``(resource_id, revision_id)`` composite used by Findings and
    Recommendations.

    A read operation; no audit row is appended (reads are
    non-consequential in this slice — design §"Audit_Log").
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT decision_id, target_recommendation_id,
                           target_recommendation_revision_id, outcome,
                           rationale, deciding_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Decisions
                    WHERE decision_id = :decision_id
                    """
                ),
                {"decision_id": decision_id},
            )
            .mappings()
            .one_or_none()
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="decision_not_found",
                message=(
                    f"No Decisions row for decision_id={decision_id!r}."
                ),
                decision_id=decision_id,
            ).model_dump(),
        )

    # ``authority_basis_id`` is persisted as a string. We re-parse it
    # to a :class:`UUID` so the response payload matches the input
    # shape (a UUID JSON string) rather than a free-form identifier.
    try:
        authority_basis_id = UUID(row["authority_basis_id"])
    except (TypeError, ValueError) as exc:
        # Defence in depth: a non-UUID value in the column would be a
        # persistence bug. Surface it as a 500 via the standard
        # exception chain rather than letting Pydantic refuse to
        # serialize the row.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=ErrorBody(
                error="authority_basis_id_corrupt",
                message=(
                    "authority_basis_id is not a valid UUID; this "
                    "indicates a persistence-layer inconsistency."
                ),
                decision_id=decision_id,
            ).model_dump(),
        ) from exc

    return DecisionResponseBody(
        decision_id=row["decision_id"],
        target_recommendation_id=row["target_recommendation_id"],
        target_recommendation_revision_id=(
            row["target_recommendation_revision_id"]
        ),
        outcome=row["outcome"],
        rationale=row["rationale"],
        deciding_party_id=row["deciding_party_id"],
        authority_basis=AuthorityBasisRequestBody(
            type=row["authority_basis_type"],  # type: ignore[arg-type]
            id=authority_basis_id,
        ),
        applicable_scope=row["applicable_scope"],
        recorded_at=row["recorded_at"],
    )
