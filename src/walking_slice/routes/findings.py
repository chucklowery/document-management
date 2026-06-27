"""HTTP routes for the Knowledge_Service Findings surface (task 6.2).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Knowledge_Service" HTTP surface (Findings rows only — Recommendations
and Decisions ship in tasks 7.2 and 8.3):

| Method | Path                                                                     | Purpose                                                                          |
|--------|--------------------------------------------------------------------------|----------------------------------------------------------------------------------|
| POST   | ``/api/v1/findings``                                                     | Create a Finding (Resource + first Revision) plus its ``Supports`` Relationships (Requirements 4.1, 4.2, 4.3, 4.5). |
| POST   | ``/api/v1/findings/{finding_id}/contradictions``                         | Record a ``Contradicts`` Relationship between an existing Finding Revision and an existing Finding Resource (Requirement 4.4). |
| GET    | ``/api/v1/findings/{finding_id}/revisions/{revision_id}``                | Read a single immutable Finding Revision row.                                    |

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates the consequential
  writes to :class:`walking_slice.knowledge.KnowledgeService` inside one
  ``engine.begin()`` transaction so the Finding header, Finding Revision,
  ``Supports`` / ``Contradicts`` Relationship rows, and the
  ``Audit_Records`` consequential row commit together (AD-WS-5,
  Requirement 13.1).
- Pre-validate request shapes with Pydantic v2 :class:`~pydantic.BaseModel`
  definitions using ``extra="forbid"`` so typo'd field names are rejected
  and converting any :class:`~pydantic.ValidationError` to a structured
  ``HTTP 400`` (instead of FastAPI's default 422) so the wire contract is
  uniform with the deeper
  :class:`~walking_slice.knowledge.FindingValidationError` responses
  (also surfaced as 400 with a ``failed_constraint`` field).
- Map service exceptions to the codes listed in the task description:
  :class:`~walking_slice.knowledge.FindingValidationError` → 400 with
  ``failed_constraint``; :class:`~walking_slice.knowledge.FindingNotResolvableError`
  → 400 with ``region_id`` + ``document_revision_id``;
  :class:`~walking_slice.knowledge.FindingNotFoundError` → 404 with the
  ``role`` (source/target) and the offending identifier.

**Authentication.** This module accepts the actor's Party Identity from
the temporary ``X-Actor-Party-Id`` header. Task 15.1 will replace this
placeholder with the bearer-token authenticated ``RequestContext``
described in design §"Application-Level Composition". The header-based
shim mirrors the pattern in :mod:`walking_slice.routes.roles` and
:mod:`walking_slice.routes.evidence` so a single future middleware
change swaps all three modules in one go.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope. The factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly; tests (and the eventual ``app.py`` in task 15.2) override them
via :data:`fastapi.FastAPI.dependency_overrides`.

Requirements satisfied (per task 6.2):
    4.1 — ``POST /api/v1/findings`` records a Finding with ``Supports``
          Relationships to cited Region Occurrences or, when
          ``is_hypothesis`` is true, with zero supports.
    4.4 — ``POST /api/v1/findings/{finding_id}/contradictions`` records a
          ``Contradicts`` Relationship between two Findings while
          leaving both Finding records byte-equivalent to their prior
          state (Finding_Revisions is append-only per AD-WS-4).
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Final, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError
from walking_slice.knowledge import (
    FindingNotFoundError,
    FindingNotResolvableError,
    FindingValidationError,
    KnowledgeService,
    SupportRef,
)
from walking_slice.manifests import ManifestValidationError


__all__ = [
    "ContradictionRequestBody",
    "ContradictionResponseBody",
    "CreateFindingRequestBody",
    "CreateFindingResponseBody",
    "ErrorBody",
    "FindingRevisionResponseBody",
    "SupportRefBody",
    "get_engine",
    "get_knowledge_service",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["findings"])


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
        "walking_slice.routes.findings.get_engine must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


def get_knowledge_service() -> KnowledgeService:
    """Provide the slice's :class:`KnowledgeService` singleton.

    The service is connection-scoped at call time — every public method
    accepts the caller's SQLAlchemy connection — so a single instance
    serves all requests safely.
    """
    raise NotImplementedError(
        "walking_slice.routes.findings.get_knowledge_service must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Pydantic v2 boundary models.
#
# Requirement 4.1 demands a non-hypothesis Finding cite at least one
# Content Region Occurrence by composite key (region_id,
# document_revision_id). The service re-validates the rule defensively;
# these models exist to short-circuit obviously-malformed requests
# (missing fields, wrong types, extra fields) with a structured 400
# before a database connection is opened.
# ---------------------------------------------------------------------------


class SupportRefBody(BaseModel):
    """Pair ``(region_id, document_revision_id)`` cited by a Finding.

    Mirrors :class:`walking_slice.knowledge.SupportRef`. The service
    re-validates each pair against ``Region_Occurrences`` so a
    non-resolvable reference surfaces as a
    :class:`FindingNotResolvableError` (→ 400 with the offending
    identifiers).
    """

    model_config = ConfigDict(extra="forbid")

    region_id: str = Field(min_length=1, description="Content Region Identity.")
    document_revision_id: str = Field(
        min_length=1,
        description=(
            "Document Revision Identity that owns the Region Occurrence "
            "(second half of the composite PK on ``Region_Occurrences``)."
        ),
    )


class CreateFindingRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/findings``.

    Mirrors :class:`walking_slice.knowledge.KnowledgeService.create_finding`'s
    keyword arguments. ``extra="forbid"`` rejects typo'd field names so
    clients receive an explicit error rather than silently dropped
    attributes. The service performs the *semantic* checks (non-empty
    ``statement``, non-empty ``authoring_party_id``,
    non-hypothesis-requires-supports); this layer only enforces the
    shape rules that are knowable without database access.
    """

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(
        min_length=1,
        description=(
            "Non-empty Finding statement. Requirement 4.1 implies a "
            "Finding carries an interpretive statement."
        ),
    )
    authoring_party_id: str = Field(
        min_length=1,
        description=(
            "Identity of the recording Party. Persisted on "
            "Finding_Revisions.authoring_party_id, every Supports "
            "Relationship's authoring_party_id, and the consequential "
            "audit row's actor_party_id (Requirement 4.2)."
        ),
    )
    is_hypothesis: bool = Field(
        default=False,
        description=(
            "When true the Finding may have zero supports "
            "(Requirement 4.1's hypothesis branch). When false the "
            "service requires at least one entry in "
            "supporting_region_occurrences (Requirement 4.3)."
        ),
    )
    supporting_region_occurrences: list[SupportRefBody] = Field(
        default_factory=list,
        description=(
            "Region Occurrences cited by this Finding. One Supports "
            "Relationship row is inserted per entry (Requirement 4.5). "
            "May be empty when is_hypothesis=true."
        ),
    )
    assumptions: list[str] = Field(
        default_factory=list,
        description=(
            "Optional assumption strings stored as a JSON array on "
            "Finding_Revisions.assumptions_json."
        ),
    )
    confidence_note: Optional[str] = Field(
        default=None,
        description=(
            "Optional free-form confidence note stored on "
            "Finding_Revisions.confidence_note."
        ),
    )

    @field_validator("assumptions")
    @classmethod
    def _validate_assumptions(cls, value: list[str]) -> list[str]:
        """Reject non-string assumption entries.

        The service serializes assumptions as a JSON array via
        ``json.dumps(list(assumptions))``; non-string entries would
        produce valid JSON but break downstream consumers that expect a
        homogeneous string array. Rejecting them here keeps the wire
        contract sharp.
        """
        for index, entry in enumerate(value):
            if not isinstance(entry, str):
                raise ValueError(
                    f"assumptions[{index}] must be a string; received "
                    f"{type(entry).__name__}."
                )
        return value


class CreateFindingResponseBody(BaseModel):
    """Successful response from ``POST /api/v1/findings`` (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    finding_revision_id: str
    is_hypothesis: bool
    supporting_relationship_ids: list[str]
    recorded_at: str


class ContradictionRequestBody(BaseModel):
    """Validated body of ``POST /.../{finding_id}/contradictions``.

    ``finding_id`` from the path is the *target* Finding Resource being
    contradicted (Requirement 4.4 keys the relationship on Finding
    Identity, not a specific Revision). The body carries the source
    Finding Revision asserting the contradiction so the service can
    derive the source Finding Identity from the row itself, keeping the
    Relationship's ``(source_id, source_revision_id)`` pair internally
    consistent by construction.
    """

    model_config = ConfigDict(extra="forbid")

    source_finding_revision_id: str = Field(
        min_length=1,
        description=(
            "Identity of the source Finding Revision asserting the "
            "contradiction. Must resolve to an existing "
            "Finding_Revisions row."
        ),
    )
    authoring_party_id: str = Field(
        min_length=1,
        description=(
            "Identity of the recording Party. Persisted on the "
            "Contradicts Relationship and the consequential audit row "
            "(Requirement 4.2 / 13.1)."
        ),
    )


class ContradictionResponseBody(BaseModel):
    """Successful response from the contradictions endpoint (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    relationship_type: str
    source_finding_id: str
    source_finding_revision_id: str
    target_finding_id: str
    authoring_party_id: str
    recorded_at: str


class FindingRevisionResponseBody(BaseModel):
    """Body returned from ``GET /.../findings/{fid}/revisions/{rev}``.

    Mirrors the columns persisted on ``Finding_Revisions`` plus the
    parent ``finding_id`` for symmetry with the create response.
    ``assumptions`` is decoded from the stored JSON array so callers see
    a structured list rather than the raw JSON string.
    """

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    finding_revision_id: str
    parent_revision_id: Optional[str]
    statement: str
    is_hypothesis: bool
    authoring_party_id: str
    assumptions: list[str]
    confidence_note: Optional[str]
    recorded_at: str


class ErrorBody(BaseModel):
    """Structured error body returned on 400 / 404 / 503 responses.

    The shape mirrors :class:`walking_slice.routes.evidence.ErrorBody`
    and :class:`walking_slice.routes.roles.ErrorBody` so a single
    client-side error handler works across every route module. Fields
    are optional; only the ones relevant to the failure are populated.
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    failed_constraint: Optional[str] = None
    finding_id: Optional[str] = None
    finding_revision_id: Optional[str] = None
    region_id: Optional[str] = None
    document_revision_id: Optional[str] = None
    role: Optional[str] = None
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


def _finding_validation_to_http(exc: FindingValidationError) -> HTTPException:
    """Map a :class:`FindingValidationError` to a structured 400.

    The ``failed_constraint`` attribute (one of ``statement_empty``,
    ``authoring_party_id_missing``,
    ``supports_required_for_non_hypothesis``) becomes both the ``error``
    code and the ``failed_constraint`` field so a client that picks
    either name finds the same stable identifier.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _finding_not_resolvable_to_http(exc: FindingNotResolvableError) -> HTTPException:
    """Map a :class:`FindingNotResolvableError` to a structured 400.

    Per the task spec the response carries both ``region_id`` and
    ``document_revision_id`` so the caller learns *which* citation
    failed to resolve. We use 400 (not 404) because the failure is a
    request-shape issue from the service's perspective — the caller
    referenced a Region Occurrence the Evidence_Repository has never
    seen.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error="region_occurrence_not_resolvable",
            message=str(exc),
            failed_constraint="region_occurrence_not_resolvable",
            region_id=exc.region_id,
            document_revision_id=exc.document_revision_id,
        ).model_dump(),
    )


def _finding_not_found_to_http(exc: FindingNotFoundError) -> HTTPException:
    """Map a :class:`FindingNotFoundError` to a structured 404.

    ``role`` (``source`` / ``target``) is preserved on the response so
    the caller learns which side of the contradiction failed. The
    ``finding_id`` / ``finding_revision_id`` field is populated based on
    the role so a uniform error handler can render a useful message.
    """
    error_payload: dict[str, Any] = {
        "error": "finding_not_found",
        "message": str(exc),
        "role": exc.role,
    }
    if exc.role == "source":
        error_payload["finding_revision_id"] = exc.identifier
    else:
        error_payload["finding_id"] = exc.identifier
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(**error_payload).model_dump(),
    )


def _audit_failure_to_http(exc: AuditAppendError) -> HTTPException:
    """Map an :class:`AuditAppendError` to a 503 response.

    Audit append failures roll back the surrounding transaction (the
    Finding header, Finding Revision, and Relationships rows are
    discarded), which is the behaviour Requirement 13.6 prescribes. The
    503 status code matches the contract used in
    :mod:`walking_slice.routes.evidence` and
    :mod:`walking_slice.routes.roles` so a single client-side handler
    covers every consequential write.
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
    raises :class:`ManifestValidationError` when an Included Source or
    Omission Entry fails Requirement 10.x validation; the
    ``failed_constraint`` attribute (one of ``subject_kind_invalid``,
    ``included_source_kind_invalid``, ``omission_category_invalid`` …)
    becomes both the ``error`` code and the ``failed_constraint`` field
    so a client that picks either name finds the same stable identifier.
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

    Per Requirement 10.6 and design §"Provenance manifest persistence
    failure": when the originating transaction fails because the
    Provenance Manifest could not be persisted, the whole synthesis
    rolls back and ``503 provenance_manifest_persistence_failed`` is
    returned. This handler is the catch-all for *unexpected* manifest-
    related exceptions raised during the ``engine.begin()`` block —
    a wired
    :class:`~walking_slice.manifests.ProvenanceManifestWriter`'s INSERT
    failing on a constraint, an
    :class:`~walking_slice.manifests.StalenessError`, or any other
    exception that the known handlers above did not cover. Known
    domain exceptions (FindingValidationError, FindingNotResolvable,
    FindingNotFound, AuditAppendError) are handled by their own
    mappers before this catch-all is consulted.
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
    "/findings",
    response_model=CreateFindingResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Finding plus its Supports Relationships.",
)
async def create_finding(
    request: Request,
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> CreateFindingResponseBody:
    """Create a Finding (Resource + first Revision) plus its supports.

    The endpoint:

    1. Reads and JSON-decodes the request body (400 on empty / malformed).
    2. Validates the body against :class:`CreateFindingRequestBody`; any
       :class:`~pydantic.ValidationError` becomes a 400 ``ErrorBody``
       naming the missing or invalid fields.
    3. Resolves the actor Party Identity from the header (fallback to
       the body's ``authoring_party_id`` per
       :func:`_resolve_actor`). The header takes precedence so a future
       middleware can simply set it unconditionally.
    4. Calls :meth:`KnowledgeService.create_finding` inside one
       ``engine.begin()`` transaction so the Findings header, Finding
       Revision, every ``Supports`` Relationship row, the
       ``Identifier_Registry`` bindings, and the ``Audit_Records``
       consequential row commit together (AD-WS-5, Requirement 13.1).

    The header / body actor are reconciled defensively: when both are
    supplied the header wins (so a future middleware can always set the
    header); when only the body's ``authoring_party_id`` is present that
    value is used; when neither is present the request is rejected with
    a 400. The service-layer ``authoring_party_id`` parameter receives
    whichever value won so the Relationships rows and audit row carry
    consistent acting-Party identifiers.

    Returns HTTP 201 with the Finding identifiers, the ordered
    ``Supports`` Relationship identifiers, and the recorded timestamp.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = CreateFindingRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(exc, error_code="invalid_finding_request") from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.authoring_party_id)

    supports = tuple(
        SupportRef(
            region_id=ref.region_id,
            document_revision_id=ref.document_revision_id,
        )
        for ref in body.supporting_region_occurrences
    )

    try:
        with engine.begin() as connection:
            result = knowledge_service.create_finding(
                connection,
                statement=body.statement,
                authoring_party_id=actor_party_id,
                is_hypothesis=body.is_hypothesis,
                supporting_region_occurrences=supports,
                assumptions=body.assumptions,
                confidence_note=body.confidence_note,
            )
    except FindingValidationError as exc:
        raise _finding_validation_to_http(exc) from exc
    except FindingNotResolvableError as exc:
        raise _finding_not_resolvable_to_http(exc) from exc
    except ManifestValidationError as exc:
        # A wired ProvenanceManifestWriter raised on an Included
        # Source or Omission Entry that fails Requirement 10.x
        # validation; surface as a structured 400 (Requirement 10.6
        # — manifest persistence failures roll the synthesis back,
        # but a *validation* failure is a request-shape issue, not
        # a persistence outage).
        raise _manifest_validation_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_failure_to_http(exc) from exc
    except HTTPException:
        # Already a structured HTTP error from the handlers above;
        # re-raise without re-wrapping.
        raise
    except Exception as exc:
        # Catch-all for any *unexpected* exception raised inside the
        # ``engine.begin()`` block — typically a manifest-persistence
        # failure surfaced by the wired
        # :class:`ProvenanceManifestWriter` (Requirement 10.6 /
        # design §"Provenance manifest persistence failure"). The
        # transaction has already rolled back so no Finding, Finding
        # Revision, Relationship, Identifier Registry binding, or
        # consequential audit row was persisted.
        raise _manifest_persistence_failure_to_http(exc) from exc

    return CreateFindingResponseBody(
        finding_id=result.finding_id,
        finding_revision_id=result.finding_revision_id,
        is_hypothesis=result.is_hypothesis,
        supporting_relationship_ids=list(result.supporting_relationship_ids),
        recorded_at=result.recorded_at,
    )


@router.post(
    "/findings/{finding_id}/contradictions",
    response_model=ContradictionResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Record a Contradicts Relationship targeting a Finding Resource.",
)
async def create_contradiction(
    request: Request,
    finding_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    knowledge_service: KnowledgeService = Depends(get_knowledge_service),
) -> ContradictionResponseBody:
    """Record a ``Contradicts`` Relationship (Requirement 4.4).

    The ``finding_id`` path parameter is the *target* Finding Resource
    being contradicted (Requirement 4.4 keys the relationship on
    Finding Identity, not a specific Revision). The body carries the
    source Finding Revision asserting the contradiction; the service
    derives the source Finding Identity from the row itself so the
    Relationship's ``(source_id, source_revision_id)`` pair is
    internally consistent by construction — the caller cannot claim a
    Revision belongs to a different Finding than it actually does.

    Both Finding records are left byte-equivalent to their prior state
    because:

    - ``Finding_Revisions`` is append-only (AD-WS-4 trigger).
    - ``Findings`` is touched only via INSERT in the
      :class:`KnowledgeService` — :meth:`record_contradiction` does not
      write to it at all.

    Returns HTTP 201 with the Relationship identifier and every
    Requirement-4.2 attribute of the inserted row.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = ContradictionRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(
            exc, error_code="invalid_contradiction_request"
        ) from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.authoring_party_id)

    try:
        with engine.begin() as connection:
            result = knowledge_service.record_contradiction(
                connection,
                source_finding_revision_id=body.source_finding_revision_id,
                target_finding_id=finding_id,
                authoring_party_id=actor_party_id,
            )
    except FindingValidationError as exc:
        raise _finding_validation_to_http(exc) from exc
    except FindingNotFoundError as exc:
        raise _finding_not_found_to_http(exc) from exc
    except ManifestValidationError as exc:
        raise _manifest_validation_to_http(exc) from exc
    except AuditAppendError as exc:
        raise _audit_failure_to_http(exc) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise _manifest_persistence_failure_to_http(exc) from exc

    # ``result.source_revision_id`` is guaranteed non-None for a
    # Contradicts row because the service derives the source Finding
    # Identity from the source Revision row; we cast for the typed
    # response body.
    assert result.source_revision_id is not None
    return ContradictionResponseBody(
        relationship_id=result.relationship_id,
        relationship_type=result.relationship_type,
        source_finding_id=result.source_id,
        source_finding_revision_id=result.source_revision_id,
        target_finding_id=result.target_id,
        authoring_party_id=result.authoring_party_id,
        recorded_at=result.recorded_at,
    )


@router.get(
    "/findings/{finding_id}/revisions/{revision_id}",
    response_model=FindingRevisionResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Read a persisted Finding Revision.",
)
async def read_finding_revision(
    finding_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> FindingRevisionResponseBody:
    """Return the Finding Revision matching ``(finding_id, revision_id)``.

    Direct SQL lookup against ``Finding_Revisions`` joined on the
    ``finding_id`` column so a caller that passes a Revision Identity
    belonging to a different Finding still gets a 404 (not silently
    redirected to whatever Finding actually owns the Revision). The
    response decodes ``assumptions_json`` so callers see a structured
    list rather than the raw JSON string.

    A read operation; no audit row is appended (reads are non-
    consequential in this slice — design §"Audit_Log").
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT finding_revision_id, finding_id,
                           parent_revision_id, statement, is_hypothesis,
                           authoring_party_id, assumptions_json,
                           confidence_note, recorded_at
                    FROM Finding_Revisions
                    WHERE finding_revision_id = :revision_id
                      AND finding_id = :finding_id
                    """
                ),
                {"finding_id": finding_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="finding_revision_not_found",
                message=(
                    f"No Finding_Revisions row for finding_id={finding_id!r}, "
                    f"finding_revision_id={revision_id!r}."
                ),
                finding_id=finding_id,
                finding_revision_id=revision_id,
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

    return FindingRevisionResponseBody(
        finding_id=row["finding_id"],
        finding_revision_id=row["finding_revision_id"],
        parent_revision_id=row["parent_revision_id"],
        statement=row["statement"],
        # ``is_hypothesis`` is stored as 0/1; coerce to bool so the JSON
        # response carries the canonical Python boolean rather than an
        # integer.
        is_hypothesis=bool(row["is_hypothesis"]),
        authoring_party_id=row["authoring_party_id"],
        assumptions=assumptions,
        confidence_note=row["confidence_note"],
        recorded_at=row["recorded_at"],
    )
