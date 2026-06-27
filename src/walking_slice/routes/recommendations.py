"""HTTP routes for the Knowledge_Service Recommendations surface (task 7.2).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Knowledge_Service" HTTP surface (Recommendations rows only —
Decisions ship in task 8.3):

| Method | Path                                                                         | Purpose                                                                                                  |
|--------|------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------|
| POST   | ``/api/v1/recommendations``                                                  | Create a Recommendation (Resource + first Revision) plus ``Derived From`` Relationships (Requirement 5). |
| GET    | ``/api/v1/recommendations/{rec_id}/revisions/{revision_id}``                 | Read a single immutable Recommendation Revision row.                                                     |

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates the consequential
  write to :meth:`walking_slice.knowledge.KnowledgeService.create_recommendation`
  inside one ``engine.begin()`` transaction so the Recommendation header,
  Recommendation Revision, every ``Derived From`` Relationship row, the
  ``Identifier_Registry`` bindings, and the ``Audit_Records``
  consequential row commit together (AD-WS-5, Requirement 13.1).
- Pre-validate request shapes with Pydantic v2 :class:`~pydantic.BaseModel`
  definitions using ``extra="forbid"`` so typo'd field names are rejected,
  and convert any :class:`~pydantic.ValidationError` to a structured
  ``HTTP 400`` (instead of FastAPI's default 422) so the wire contract is
  uniform with the deeper service-layer responses.
- Map service exceptions to the codes listed in the task description:

  - :class:`~walking_slice.knowledge.RecommendationValidationError`
    → ``400`` with ``failed_constraint``.
  - :class:`~walking_slice.knowledge.RecommendationNotResolvableError`
    → ``400`` with ``finding_id`` (and ``failed_constraint=invalid_derived_from``).
  - :class:`~walking_slice.knowledge.RecommendationAuthorizationError`
    → ``403`` with the AD-WS-9 indistinguishable-denial shape
    (``generic_denial_indicator``, ``reason_code``, ``correlation_id``).
  - :class:`~walking_slice.audit.AuditAppendError` → ``503``.

**Authentication.** This module accepts the actor's Party Identity from
the temporary ``X-Actor-Party-Id`` header. Task 15.1 will replace this
placeholder with the bearer-token authenticated ``RequestContext``
described in design §"Application-Level Composition". The header-based
shim mirrors the pattern in :mod:`walking_slice.routes.roles`,
:mod:`walking_slice.routes.evidence`, and
:mod:`walking_slice.routes.findings` so a single future middleware
change swaps every route module in one go.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope. The factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly; tests (and the eventual ``app.py`` in task 15.2) override them
via :data:`fastapi.FastAPI.dependency_overrides`.

Requirements satisfied (per task 7.2):
    5.1 — ``POST /api/v1/recommendations`` records a Recommendation with
          1..50 ``Derived From`` Relationships to existing Findings.
    5.7 — Unauthenticated callers and callers lacking effective Analyst
          role for the applicable scope are rejected with an
          authorization-denial response shaped per AD-WS-9.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError
from walking_slice.knowledge import (
    KnowledgeService,
    RecommendationAuthorizationError,
    RecommendationNotResolvableError,
    RecommendationValidationError,
)
from walking_slice.manifests import ManifestValidationError


__all__ = [
    "CreateRecommendationRequestBody",
    "CreateRecommendationResponseBody",
    "ErrorBody",
    "RecommendationRevisionResponseBody",
    "get_engine",
    "get_knowledge_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["recommendations"])


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
        "walking_slice.routes.recommendations.get_engine must be overridden "
        "by app composition (task 15.2) or test fixtures."
    )


def get_knowledge_service() -> KnowledgeService:
    """Provide the slice's :class:`KnowledgeService` singleton.

    The service is connection-scoped at call time — every public method
    accepts the caller's SQLAlchemy connection — so a single instance
    serves all requests safely. When the service is wired with an
    :class:`~walking_slice.authorization.AuthorizationService` the
    Requirement 5.7 authority check is enforced; otherwise the
    back-compat path (no enforcement) is exercised.
    """
    raise NotImplementedError(
        "walking_slice.routes.recommendations.get_knowledge_service must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Validation limits (mirrored from
# :mod:`walking_slice.knowledge` so the Pydantic schema can short-circuit
# obvious shape violations before a database connection is opened).
#
# These constants are duplicated intentionally — the service layer
# re-validates defensively, so the duplication catches structural errors
# without coupling the route's Field constraints to a private symbol.
# ---------------------------------------------------------------------------

_DERIVED_FROM_MIN: Final[int] = 1
_DERIVED_FROM_MAX: Final[int] = 50
_RATIONALE_MAX_CHARS: Final[int] = 10_000
_ASSUMPTION_MAX_CHARS: Final[int] = 2_000
_ASSUMPTIONS_MAX_ENTRIES: Final[int] = 50
_CONFIDENCE_LITERAL = Literal["Low", "Medium", "High"]


# ---------------------------------------------------------------------------
# Pydantic v2 boundary models.
#
# Requirement 5 names four content ranges and a fifth structural rule:
#
#   5.1 — between 1 and 50 ``Derived From`` references.
#   5.3 — when supplied, rationale carries 1..10,000 characters.
#   5.4 — when supplied, assumptions carries 0..50 entries × 1..2,000 chars.
#   5.5 — when supplied, confidence is one of {Low, Medium, High}.
#   5.7 — the caller must hold effective Analyst role for the scope.
#
# The service layer enforces all of these — the duplication here exists so
# clients see a structured 400 immediately rather than a 422 from
# FastAPI's default error handler.
# ---------------------------------------------------------------------------


class CreateRecommendationRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/recommendations``.

    Mirrors :meth:`walking_slice.knowledge.KnowledgeService.create_recommendation`'s
    keyword arguments. ``extra="forbid"`` rejects typo'd field names so
    clients receive an explicit error rather than silently dropped
    attributes. The service performs the *semantic* checks (existence
    of every cited Finding, authority for the applicable scope); this
    layer only enforces the shape rules that are knowable without
    database access.
    """

    model_config = ConfigDict(extra="forbid")

    authoring_party_id: str = Field(
        min_length=1,
        description=(
            "Identity of the Party recording the Recommendation. "
            "Persisted on Recommendation_Revisions.authoring_party_id, "
            "every Derived From Relationship's authoring_party_id, and "
            "the consequential audit row's actor_party_id "
            "(Requirements 5.7, 13.1)."
        ),
    )
    derived_from_findings: list[str] = Field(
        min_length=_DERIVED_FROM_MIN,
        max_length=_DERIVED_FROM_MAX,
        description=(
            "Finding Identities the Recommendation derives from. One "
            "Derived From Relationship row is inserted per entry. "
            "Requirement 5.1 caps the count at 1..50."
        ),
    )
    rationale: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=_RATIONALE_MAX_CHARS,
        description=(
            "Optional rationale text. When supplied, Requirement 5.3 "
            "limits the length to 1..10,000 characters; ``None`` (the "
            "default) is persisted as SQL NULL."
        ),
    )
    assumptions: list[str] = Field(
        default_factory=list,
        max_length=_ASSUMPTIONS_MAX_ENTRIES,
        description=(
            "Optional assumption strings stored as a JSON array on "
            "Recommendation_Revisions.assumptions_json. Requirement 5.4 "
            "caps the count at 0..50 entries of 1..2,000 characters each."
        ),
    )
    confidence: Optional[_CONFIDENCE_LITERAL] = Field(
        default=None,
        description=(
            "Optional confidence designation drawn from {Low, Medium, "
            "High} per Requirement 5.5. ``None`` is persisted as SQL NULL."
        ),
    )
    applicable_scope: Optional[str] = Field(
        default=None,
        description=(
            "Optional scope identifier passed to the authority check "
            "(Requirement 5.7). Used only when the wired "
            "KnowledgeService carries an AuthorizationService."
        ),
    )

    @field_validator("derived_from_findings")
    @classmethod
    def _validate_derived_from(cls, value: list[str]) -> list[str]:
        """Reject empty entries in ``derived_from_findings``.

        Pydantic enforces ``min_length=1`` / ``max_length=50`` on the
        list itself; this validator additionally rejects empty-string
        entries so the service does not have to defend against
        ``""`` masquerading as a Finding Identity.
        """
        for index, entry in enumerate(value):
            if not isinstance(entry, str):
                raise ValueError(
                    f"derived_from_findings[{index}] must be a string; "
                    f"received {type(entry).__name__}."
                )
            if not entry:
                raise ValueError(
                    f"derived_from_findings[{index}] is empty; "
                    "every entry must be a non-empty Finding Identity."
                )
        return value

    @field_validator("assumptions")
    @classmethod
    def _validate_assumptions(cls, value: list[str]) -> list[str]:
        """Reject non-string and out-of-range assumption entries.

        The service serializes assumptions as a JSON array via
        ``json.dumps(list(assumptions))``; non-string entries would
        produce valid JSON but break downstream consumers that expect a
        homogeneous string array. Requirement 5.4 also constrains each
        entry to 1..2,000 characters so we enforce that range here.
        """
        for index, entry in enumerate(value):
            if not isinstance(entry, str):
                raise ValueError(
                    f"assumptions[{index}] must be a string; received "
                    f"{type(entry).__name__}."
                )
            if len(entry) == 0:
                raise ValueError(
                    f"assumptions[{index}] is empty; Requirement 5.4 "
                    "requires every entry to carry 1..2,000 characters."
                )
            if len(entry) > _ASSUMPTION_MAX_CHARS:
                raise ValueError(
                    f"assumptions[{index}] length {len(entry)} exceeds "
                    f"the {_ASSUMPTION_MAX_CHARS}-character per-entry "
                    "limit imposed by Requirement 5.4."
                )
        return value


class CreateRecommendationResponseBody(BaseModel):
    """Successful response from ``POST /api/v1/recommendations`` (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    recommendation_revision_id: str
    rationale: Optional[str]
    assumptions: list[str]
    confidence: Optional[str]
    derived_from_relationship_ids: list[str]
    recorded_at: str


class RecommendationRevisionResponseBody(BaseModel):
    """Body returned from ``GET /.../recommendations/{rid}/revisions/{rev}``.

    Mirrors the columns persisted on ``Recommendation_Revisions`` plus
    the parent ``recommendation_id`` for symmetry with the create
    response. ``assumptions`` is decoded from the stored JSON array so
    callers see a structured list rather than the raw JSON string.
    """

    model_config = ConfigDict(extra="forbid")

    recommendation_id: str
    recommendation_revision_id: str
    parent_revision_id: Optional[str]
    rationale: Optional[str]
    assumptions: list[str]
    confidence: Optional[str]
    authoring_party_id: str
    recorded_at: str


class ErrorBody(BaseModel):
    """Structured error body returned on 400 / 403 / 404 / 503 responses.

    The shape is a superset of the error envelopes used in
    :mod:`walking_slice.routes.findings`,
    :mod:`walking_slice.routes.evidence`, and
    :mod:`walking_slice.routes.roles` so a single client-side error
    handler works across every route module. Fields are optional; only
    the ones relevant to the failure are populated.

    The ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` fields support the AD-WS-9 indistinguishable
    denial response: an unauthorized Recommendation creation yields a
    body carrying only those three fields plus the ``error`` tag.
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    failed_constraint: Optional[str] = None
    finding_id: Optional[str] = None
    recommendation_id: Optional[str] = None
    recommendation_revision_id: Optional[str] = None
    # AD-WS-9 denial response fields.
    generic_denial_indicator: Optional[str] = None
    reason_code: Optional[str] = None
    correlation_id: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_HEADER_ACTOR: Final[str] = "X-Actor-Party-Id"


def _resolve_actor(header_value: Optional[str], body_value: Optional[str]) -> str:
    """Pick the actor Party Identity from the header or the request body.

    The header wins when both are present so a future authentication
    middleware (task 15.1) can simply set the header unconditionally
    without having to filter the request body. When neither carries a
    value the request is rejected with a 400 — the actor is required for
    every consequential write so the audit row in this transaction has a
    valid ``actor_party_id`` (Requirement 13.1).
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
    ``ctx['error']`` payload (Pydantic v2 attaches the original exception
    object there for ``value_error`` failures, which breaks JSON
    encoding).
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


def _recommendation_validation_to_http(
    exc: RecommendationValidationError,
) -> HTTPException:
    """Map a :class:`RecommendationValidationError` to a structured 400.

    The ``failed_constraint`` attribute (one of the values enumerated on
    :class:`RecommendationValidationError`) becomes both the ``error``
    code and the ``failed_constraint`` field so a client that picks
    either name finds the same stable identifier. This matches the
    pattern established for :class:`FindingValidationError` in
    :mod:`walking_slice.routes.findings`.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _recommendation_not_resolvable_to_http(
    exc: RecommendationNotResolvableError,
) -> HTTPException:
    """Map a :class:`RecommendationNotResolvableError` to a structured 400.

    Per the task spec the response carries ``finding_id`` so the caller
    learns *which* Derived From reference failed to resolve. We use 400
    (not 404) because the failure is a request-shape issue from the
    service's perspective — the caller referenced a Finding the
    Knowledge_Service has never seen. The error envelope also carries
    ``failed_constraint=invalid_derived_from`` so a uniform client-side
    handler can branch on the same constraint name used by other
    Recommendation rejections.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error="invalid_derived_from",
            message=str(exc),
            failed_constraint=exc.failed_constraint,
            finding_id=exc.finding_id,
        ).model_dump(),
    )


def _authorization_denial_to_http(
    exc: RecommendationAuthorizationError,
) -> HTTPException:
    """Map a :class:`RecommendationAuthorizationError` to a 403 response.

    Per AD-WS-9 the denial response carries only the
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` fields — no information about *what* was being
    accessed, *which* role assignment was missing, or *whether* the
    target exists is leaked. Requirement 7.4 also calls this out for the
    denied-Decision response; we apply the same shape here for
    consistency across every authority-denial path in the slice.

    The 403 status code follows the design §"Error Handling" table:
    "Authorization denial … 403 for action-authority-missing on a
    visible target". Requirement 5.7 explicitly mentions the
    "applicable scope" check, which is an action-authority decision on
    a visible (newly-created) Recommendation Resource.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=ErrorBody(
            error="authorization_denied",
            generic_denial_indicator="denied",
            reason_code=exc.reason_code,
            correlation_id=exc.correlation_id,
        ).model_dump(),
    )


def _audit_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map an :class:`AuditAppendError` to a 503 response.

    Audit append failures roll back the surrounding transaction (the
    Recommendations header, Recommendation Revision, and Relationships
    rows are discarded), which is the behaviour Requirement 13.6
    prescribes. The 503 status code matches the contract used in every
    other route module so a single client-side handler covers every
    consequential write.
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

    Mirrors the handler in :mod:`walking_slice.routes.findings`: a
    wired :class:`~walking_slice.manifests.ProvenanceManifestWriter`
    raised on a Requirement 10.x validation failure. The
    ``failed_constraint`` attribute becomes both the ``error`` code
    and the ``failed_constraint`` field for a stable client contract.
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
    failure": when the originating Recommendation transaction fails
    because the Provenance Manifest could not be persisted, the whole
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


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/recommendations",
    response_model=CreateRecommendationResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Recommendation plus its Derived From Relationships.",
)
async def create_recommendation(
    request: Request,
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> CreateRecommendationResponseBody:
    """Create a Recommendation (Resource + first Revision) plus derivations.

    The endpoint:

    1. Reads and JSON-decodes the request body (400 on empty / malformed).
    2. Validates the body against :class:`CreateRecommendationRequestBody`;
       any :class:`~pydantic.ValidationError` becomes a 400 ``ErrorBody``
       naming the missing or invalid fields.
    3. Resolves the actor Party Identity from the header (fallback to
       the body's ``authoring_party_id`` per :func:`_resolve_actor`).
       The header takes precedence so a future middleware can simply
       set it unconditionally.
    4. Calls :meth:`KnowledgeService.create_recommendation` inside one
       ``engine.begin()`` transaction so the Recommendations header,
       Recommendation Revision, every ``Derived From`` Relationship
       row, the ``Identifier_Registry`` bindings, and the
       ``Audit_Records`` consequential row commit together (AD-WS-5,
       Requirement 13.1). The same connection's transaction also
       carries any authorization evaluation audit row appended by the
       wired :class:`AuthorizationService` (Requirement 12.5).

    Returns HTTP 201 with the Recommendation identifiers, the persisted
    rationale / assumptions / confidence, the ordered ``Derived From``
    Relationship identifiers, and the recorded timestamp.

    Exception mapping (per task 7.2):

    - :class:`RecommendationValidationError` → 400 with
      ``failed_constraint``.
    - :class:`RecommendationNotResolvableError` → 400 with ``finding_id``.
    - :class:`RecommendationAuthorizationError` → 403 with the AD-WS-9
      indistinguishable denial shape (``generic_denial_indicator``,
      ``reason_code``, ``correlation_id``).
    - :class:`AuditAppendError` → 503.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = CreateRecommendationRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="invalid_recommendation_request"
        ) from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.authoring_party_id)

    try:
        with engine.begin() as connection:
            result = knowledge_service.create_recommendation(
                connection,
                authoring_party_id=actor_party_id,
                derived_from_findings=tuple(body.derived_from_findings),
                rationale=body.rationale,
                assumptions=tuple(body.assumptions),
                confidence=body.confidence,
                applicable_scope=body.applicable_scope,
            )
    except RecommendationAuthorizationError as exc:
        # Authority denial returns 403 per AD-WS-9; raised *before*
        # any Recommendations / Recommendation_Revisions / Relationships
        # row is inserted, so the surrounding transaction rolls back
        # carrying only the evaluation audit row appended by the
        # AuthorizationService (Requirement 12.5).
        raise _authorization_denial_to_http(exc) from exc
    except RecommendationNotResolvableError as exc:
        # NotResolvableError is a subclass of LookupError (not of
        # RecommendationValidationError) so we catch it first; the
        # ``failed_constraint`` is ``invalid_derived_from`` per
        # Requirement 5.6.
        raise _recommendation_not_resolvable_to_http(exc) from exc
    except RecommendationValidationError as exc:
        raise _recommendation_validation_to_http(exc) from exc
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
        # :class:`ProvenanceManifestWriter` (Requirement 10.6).
        raise _manifest_persistence_failure_to_http(exc) from exc

    return CreateRecommendationResponseBody(
        recommendation_id=result.recommendation_id,
        recommendation_revision_id=result.recommendation_revision_id,
        rationale=result.rationale,
        assumptions=list(result.assumptions),
        confidence=result.confidence,
        derived_from_relationship_ids=list(result.derived_from_relationship_ids),
        recorded_at=result.recorded_at,
    )


@router.get(
    "/recommendations/{recommendation_id}/revisions/{revision_id}",
    response_model=RecommendationRevisionResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Read a persisted Recommendation Revision.",
)
async def read_recommendation_revision(
    recommendation_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> RecommendationRevisionResponseBody:
    """Return the Recommendation Revision matching ``(rec_id, revision_id)``.

    Direct SQL lookup against ``Recommendation_Revisions`` joined on
    the ``recommendation_id`` column so a caller that passes a Revision
    Identity belonging to a different Recommendation still gets a 404
    (not silently redirected to whatever Recommendation actually owns
    the Revision). The response decodes ``assumptions_json`` so callers
    see a structured list rather than the raw JSON string.

    A read operation; no audit row is appended (reads are non-
    consequential in this slice — design §"Audit_Log").
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT recommendation_revision_id, recommendation_id,
                           parent_revision_id, rationale, assumptions_json,
                           confidence, authoring_party_id, recorded_at
                    FROM Recommendation_Revisions
                    WHERE recommendation_revision_id = :revision_id
                      AND recommendation_id = :recommendation_id
                    """
                ),
                {
                    "recommendation_id": recommendation_id,
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
                error="recommendation_revision_not_found",
                message=(
                    "No Recommendation_Revisions row for "
                    f"recommendation_id={recommendation_id!r}, "
                    f"recommendation_revision_id={revision_id!r}."
                ),
                recommendation_id=recommendation_id,
                recommendation_revision_id=revision_id,
            ).model_dump(),
        )

    # ``assumptions_json`` is a JSON-encoded array (the column is NOT
    # NULL and the service serializes the empty case as "[]" so this
    # decode is always well-defined).
    try:
        assumptions = json.loads(row["assumptions_json"])
    except json.JSONDecodeError:
        # Defence in depth: a corrupted JSON column should not surface
        # as an opaque 500. Treat it as an empty list and rely on the
        # next operational audit to catch the inconsistency.
        assumptions = []
    if not isinstance(assumptions, list):
        assumptions = []

    return RecommendationRevisionResponseBody(
        recommendation_id=row["recommendation_id"],
        recommendation_revision_id=row["recommendation_revision_id"],
        parent_revision_id=row["parent_revision_id"],
        rationale=row["rationale"],
        assumptions=assumptions,
        confidence=row["confidence"],
        authoring_party_id=row["authoring_party_id"],
        recorded_at=row["recorded_at"],
    )
