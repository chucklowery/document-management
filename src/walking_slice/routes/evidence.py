"""HTTP routes for the Evidence_Repository (task 5.4).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Evidence_Repository" HTTP surface:

| Method | Path                                                                  | Purpose                                                                          |
|--------|-----------------------------------------------------------------------|----------------------------------------------------------------------------------|
| POST   | ``/api/v1/documents``                                                 | Create a Source Document plus its initial Document Revision (Requirement 2.1).  |
| POST   | ``/api/v1/documents/{resource_id}/revisions``                         | Append a new immutable Document Revision (Requirement 2.4).                     |
| POST   | ``/api/v1/documents/{resource_id}/revisions/{revision_id}/regions``   | Record a Content Region Occurrence (Requirement 3.1).                           |
| GET    | ``/api/v1/documents/{resource_id}/revisions/{revision_id}``           | Read a Document Revision (Requirement 2.4 / 11.2).                              |
| GET    | ``/api/v1/regions/{region_id}/occurrences/{revision_id}``             | Read a Region Occurrence (Requirement 3.4).                                     |
| PATCH  | ``/api/v1/documents/{resource_id}/location``                          | Rename or relocate a Source Document, preserving identity (Requirement 1.3).    |

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates to
  :class:`walking_slice.evidence.EvidenceRepository` for every
  consequential write, wrapping each call in the caller's transaction
  (``engine.begin()`` block) so the Source Document, Document Revision,
  Identifier Registry, Region Occurrence, and ``Audit_Records`` rows all
  commit together (AD-WS-5).
- Pre-validate request shapes with Pydantic v2 :class:`~pydantic.BaseModel`
  definitions whose :func:`~pydantic.Field` constraints catch the obvious
  shape violations before they reach the service. Any
  :class:`~pydantic.ValidationError` is converted to a structured
  ``HTTP 400`` rather than the FastAPI-default 422 so that the wire
  contract is uniform with the deeper
  :class:`~walking_slice.evidence.InvalidContentError` and
  :class:`~walking_slice.evidence.InvalidSpanError` responses (also
  surfaced as 400).
- Map service exceptions to HTTP status codes per the task description:
  :class:`~walking_slice.evidence.InvalidContentError` and
  :class:`~walking_slice.evidence.InvalidSpanError` → 400;
  :class:`~walking_slice.evidence.RevisionNotFoundError`,
  :class:`~walking_slice.evidence.RegionNotFoundError`, and
  :class:`~walking_slice.evidence.SourceDocumentNotFoundError` → 404.

**Binary content encoding.** ``content_bytes`` arrives as a base64-encoded
JSON string (Pydantic v2's :class:`pydantic.Base64Bytes` decodes the
string into raw bytes during validation). Base64 keeps the wire format
uniformly JSON for every endpoint and avoids the multipart/form-data
parser. A future task may add a multipart variant for very large
uploads; until then a single JSON body is sufficient for the slice's
test footprint (Requirement 2.6 caps Document content at 100 MB, which
is ~134 MB base64-encoded — well within FastAPI's default body limit).

**Authentication.** This module accepts the actor's Party Identity from
the temporary ``X-Actor-Party-Id`` header. Task 15.1 will replace this
placeholder with the bearer-token authenticated ``RequestContext``
described in design §"Application-Level Composition". The header-based
shim mirrors the pattern in :mod:`walking_slice.routes.roles` so a
single future middleware change swaps both modules in one go.

**Dependency injection.** Every collaborator is reached through a
:func:`fastapi.Depends` factory exposed at module scope. The factories
raise :class:`NotImplementedError` by default so an unwired call fails
loudly; tests (and the eventual ``app.py`` in task 15.2) override them
via :data:`fastapi.FastAPI.dependency_overrides`.

Requirements satisfied (per task 5.4):
    2.1 — ``POST /api/v1/documents`` records a Source Document and an
          initial Document Revision in one consequential write.
    3.1 — ``POST /.../regions`` records a Content Region Occurrence
          against a Document Revision.
    3.4 — ``GET /api/v1/regions/{region_id}/occurrences/{revision_id}``
          returns the persisted Region Occurrence (anchors plus digest)
          so a caller can verify resolvability against the owning
          Document Revision.
    13.1 — Every consequential endpoint runs inside
          ``EvidenceRepository`` which appends an ``Audit_Records`` row
          inside the same transaction. The route layer adds nothing on
          top of that contract; it only wraps the service call in
          ``engine.begin()``.
"""

from __future__ import annotations

import base64
import json
from typing import Annotated, Any, Final, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import Base64Bytes, BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError
from walking_slice.evidence import (
    AUTHORITY_ENUM,
    EvidenceRepository,
    InvalidContentError,
    InvalidSpanError,
    RegionNotFoundError,
    RevisionNotFoundError,
    SourceDocumentNotFoundError,
)


__all__ = [
    "AppendRevisionRequestBody",
    "AppendRevisionResponseBody",
    "CreateDocumentRequestBody",
    "CreateDocumentResponseBody",
    "CreateRegionRequestBody",
    "CreateRegionResponseBody",
    "DocumentRevisionResponseBody",
    "ErrorBody",
    "RegionOccurrenceResponseBody",
    "RenameDocumentRequestBody",
    "RenameDocumentResponseBody",
    "get_engine",
    "get_evidence_repository",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["evidence"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# These factories are deliberately stubs; task 15.2 wires concrete
# implementations through ``walking_slice.app.create_app``. Tests override
# them on the per-test :class:`fastapi.FastAPI` instance via
# ``app.dependency_overrides[get_engine] = lambda: engine`` etc.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    Overridden in tests and in the application composition layer
    (task 15.2). Never called unwrapped from a route handler.
    """
    raise NotImplementedError(
        "walking_slice.routes.evidence.get_engine must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


def get_evidence_repository() -> EvidenceRepository:
    """Provide the slice's :class:`EvidenceRepository` singleton.

    The repository is connection-scoped at call time — every method
    accepts the caller's SQLAlchemy connection — so a single instance
    serves all requests safely.
    """
    raise NotImplementedError(
        "walking_slice.routes.evidence.get_evidence_repository must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Pydantic v2 boundary models.
#
# The constraints below mirror Requirement 2.6's input rules (1..100 MB
# content, non-empty contributing Party, authority from the AD-WS-1
# enumeration) and Requirement 3.5's offset rules (0 <= start < end). The
# service layer re-checks every rule defensively — these models exist to
# short-circuit obviously-malformed requests with a structured 400 before
# a database connection is opened.
# ---------------------------------------------------------------------------


_AUTHORITY_VALUES: Final[frozenset[str]] = AUTHORITY_ENUM


class CreateDocumentRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/documents``.

    ``content_bytes`` is declared as :class:`pydantic.Base64Bytes` so a
    base64-encoded JSON string is decoded to raw bytes during validation;
    the service-layer 1..100 MB length check runs against the decoded
    payload (Requirement 2.6). The 100 MB cap is *not* re-enforced at
    this layer because the response code and error shape must come from
    one place; duplicating the constant would risk drift the next time
    the slice tunes the cap.
    """

    model_config = ConfigDict(extra="forbid")

    content_bytes: Base64Bytes = Field(
        description=(
            "Base64-encoded document bytes. Decoded length must be "
            "1..100 MB per Requirement 2.6 (enforced by the service)."
        ),
    )
    contributing_party_id: str = Field(
        min_length=1,
        description="Identity of the Party submitting the document (Requirement 2.6).",
    )
    authority: str = Field(
        description=(
            "Authority designation from the AD-WS-1 enumeration "
            "(authoritative / imported-replica / imported-projection / "
            "imported-index / imported-federation-point / "
            "reference-to-system-of-record)."
        ),
    )
    external_identifier: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Optional external-system identifier (Requirement 2.3).",
    )
    source_system_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Optional source-system identifier (Requirement 2.3).",
    )
    current_location: Optional[str] = Field(
        default=None,
        description=(
            "Optional initial display path. May be changed later via "
            "PATCH /documents/{id}/location without changing identity."
        ),
    )


class CreateDocumentResponseBody(BaseModel):
    """Successful response from ``POST /api/v1/documents`` (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    revision_id: str
    content_digest_sha256: str
    recorded_at: str


class AppendRevisionRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/documents/{resource_id}/revisions``."""

    model_config = ConfigDict(extra="forbid")

    content_bytes: Base64Bytes = Field(
        description="Base64-encoded Revision bytes. Decoded length 1..100 MB (Requirement 2.6).",
    )
    contributing_party_id: str = Field(
        min_length=1,
        description="Identity of the Party submitting the new Revision.",
    )
    change_description: Optional[str] = Field(
        default=None,
        max_length=10_000,
        description="Optional free-text description of the change.",
    )


class AppendRevisionResponseBody(BaseModel):
    """Successful response from the append-revision endpoint (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    revision_id: str
    parent_revision_id: str
    content_digest_sha256: str
    recorded_at: str


class CreateRegionRequestBody(BaseModel):
    """Validated body of ``POST /.../regions``.

    Offsets are byte offsets into the owning Document Revision's
    ``content_bytes``. The service re-validates against the actual
    content length (Requirement 3.5); the constraints here only catch
    the shape errors that are knowable without the content.
    """

    model_config = ConfigDict(extra="forbid")

    start_offset_bytes: int = Field(
        ge=0,
        description="Byte offset of the first byte of the span (0 <= start).",
    )
    end_offset_bytes: int = Field(
        ge=1,
        description="Byte offset one past the last byte of the span (start < end).",
    )
    contributing_party_id: str = Field(
        min_length=1,
        description="Identity of the Party recording the Occurrence.",
    )
    region_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Optional Region Identity. Omit to create a fresh Region; "
            "supply to anchor an existing Region in another Revision "
            "(Requirement 3.3)."
        ),
    )


class CreateRegionResponseBody(BaseModel):
    """Successful response from the create-region endpoint (HTTP 201)."""

    model_config = ConfigDict(extra="forbid")

    region_id: str
    revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    recorded_at: str


class DocumentRevisionResponseBody(BaseModel):
    """Body returned from ``GET /api/v1/documents/{rid}/revisions/{rev}``.

    ``content_bytes`` is a base64-encoded string on the wire so the
    response symmetrically pairs with the create endpoints' base64
    input. We use a plain :class:`str` (rather than
    :class:`pydantic.Base64Bytes`) because Base64Bytes would treat the
    stored raw bytes as an already-base64-encoded payload on
    serialization and silently decode them; a plain string with an
    explicit :func:`base64.b64encode` on the route side keeps the
    encoding direction unambiguous.
    """

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    revision_id: str
    parent_revision_id: Optional[str]
    content_bytes: str = Field(
        description=(
            "Base64-encoded document content. Decode with "
            "``base64.b64decode`` to recover the raw bytes."
        ),
    )
    content_digest_sha256: str
    contributing_party_id: str
    recorded_at: str
    change_description: Optional[str]


class RegionOccurrenceResponseBody(BaseModel):
    """Body returned from ``GET /api/v1/regions/{rid}/occurrences/{rev}``."""

    model_config = ConfigDict(extra="forbid")

    region_id: str
    revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    recorded_at: str


class RenameDocumentRequestBody(BaseModel):
    """Validated body of ``PATCH /api/v1/documents/{resource_id}/location``.

    ``new_current_location`` may be ``None`` (JSON ``null``) to clear the
    display path; the service layer accepts that case and the schema
    column is nullable. ``actor_party_id`` is optional on the body
    because the header takes precedence; if both are absent the request
    is rejected with a 400 (the actor is required for every
    consequential write per Requirement 13.1).
    """

    model_config = ConfigDict(extra="forbid")

    new_current_location: Optional[str] = Field(
        default=None,
        description=(
            "New display path for the Source Document. May be null to "
            "clear the path. Resource Identity and existing Revision "
            "identifiers are preserved across the rename (Requirement 1.3)."
        ),
    )
    actor_party_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "PLACEHOLDER (task 15.1): Party Identity of the actor "
            "recording the rename. Carried here or in the "
            "'X-Actor-Party-Id' header until bearer-token auth lands."
        ),
    )


class RenameDocumentResponseBody(BaseModel):
    """Successful response from the rename endpoint (HTTP 200)."""

    model_config = ConfigDict(extra="forbid")

    resource_id: str
    new_current_location: Optional[str]
    previous_location: Optional[str]
    recorded_at: str


class ErrorBody(BaseModel):
    """Structured error body returned on 400 / 404 / 409 / 503 responses.

    The shape mirrors :class:`walking_slice.routes.roles.ErrorBody` so a
    single client-side error handler works across every route module.
    Fields are deliberately optional; only the ones relevant to the
    failure are populated.
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    failed_constraint: Optional[str] = None
    resource_id: Optional[str] = None
    revision_id: Optional[str] = None
    region_id: Optional[str] = None
    validation_errors: Optional[list[dict[str, Any]]] = None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_HEADER_ACTOR: Final[str] = "X-Actor-Party-Id"


def _resolve_actor(header_value: Optional[str], body_value: Optional[str]) -> str:
    """Pick the actor Party Identity from the header or the request body.

    The header wins when both are present so a future authentication
    middleware (task 15.1) can simply set the header unconditionally.
    Missing actors are rejected with a 400 — the actor is required for
    every consequential write per Requirement 13.1.
    """
    actor = (header_value or "").strip() or (body_value or "").strip()
    if not actor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="actor_party_id_required",
                message=(
                    "actor_party_id must be supplied via the X-Actor-Party-Id "
                    "header or as a top-level 'actor_party_id' field on the "
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

    The ``missing`` list mirrors Requirement 2.6 / 3.5's "missing field"
    language by extracting field names from errors whose Pydantic type is
    ``missing``. All other errors land in ``validation_errors`` so
    clients see the full detail without the non-JSON-serialisable
    ``ctx['error']`` payload.
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


def _invalid_content_to_http(exc: InvalidContentError) -> HTTPException:
    """Map an :class:`InvalidContentError` to a 400 response.

    The ``failed_constraint`` carried on the exception names the precise
    Requirement 2.6 / 1.3 sub-rule that failed (e.g.
    ``content_empty``, ``content_too_large``,
    ``contributing_party_id_missing``, ``authority_invalid``). The
    client uses this code to render a stable error message without
    string-matching on the human-readable message.
    """
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _invalid_span_to_http(exc: InvalidSpanError) -> HTTPException:
    """Map an :class:`InvalidSpanError` to a 400 response."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error=exc.failed_constraint,
            message=str(exc),
            failed_constraint=exc.failed_constraint,
        ).model_dump(),
    )


def _authority_check(body: CreateDocumentRequestBody) -> None:
    """Reject authority values outside the AD-WS-1 enumeration.

    Mirrors the deeper validation in
    :meth:`EvidenceRepository.create_document`; we check here so the
    request fails fast with a stable ``authority_invalid`` code before a
    database connection is opened.
    """
    if body.authority not in _AUTHORITY_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="authority_invalid",
                message=(
                    f"authority {body.authority!r} is not in the AD-WS-1 "
                    f"enumeration {sorted(_AUTHORITY_VALUES)!r}."
                ),
                failed_constraint="authority_invalid",
                invalid=["authority"],
            ).model_dump(),
        )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/documents",
    response_model=CreateDocumentResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Create a Source Document and its initial Document Revision.",
)
async def create_document(
    request: Request,
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    evidence_repository: EvidenceRepository = Depends(get_evidence_repository),
) -> CreateDocumentResponseBody:
    """Create a Source Document plus its initial Document Revision.

    The endpoint:

    1. Reads and JSON-decodes the request body (400 on empty / malformed).
    2. Validates the body against :class:`CreateDocumentRequestBody`;
       any :class:`~pydantic.ValidationError` becomes a 400 ``ErrorBody``
       naming the missing or invalid fields.
    3. Resolves the actor Party Identity from the header. The header is
       the only path here because the body's
       ``contributing_party_id`` is the *Party recording the content*,
       not necessarily the actor (in the slice's pilot wave they are
       the same, but the contract reserves room for delegation later).
    4. Calls :meth:`EvidenceRepository.create_document` inside one
       transaction so the Source Document, Document Revision,
       Identifier Registry, and ``Audit_Records`` rows commit together
       (AD-WS-5).

    Returns HTTP 201 with the Resource Identity, Revision Identity,
    content digest, and recorded timestamp.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = CreateDocumentRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(exc, error_code="invalid_document_request") from exc

    _authority_check(body)
    # The actor header is consulted only for parity with the rest of the
    # slice; the contributing Party from the body is what
    # ``EvidenceRepository.create_document`` records on the Document
    # Revision and the audit row. We *do not* reject a missing
    # ``X-Actor-Party-Id`` here because Requirement 2.6 names the
    # contributing Party (already required on the body), not a separate
    # actor.
    _ = x_actor_party_id

    try:
        with engine.begin() as connection:
            result = evidence_repository.create_document(
                connection,
                content_bytes=bytes(body.content_bytes),
                contributing_party_id=body.contributing_party_id,
                authority=body.authority,
                external_identifier=body.external_identifier,
                source_system_id=body.source_system_id,
                current_location=body.current_location,
            )
    except InvalidContentError as exc:
        raise _invalid_content_to_http(exc) from exc
    except AuditAppendError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorBody(
                error="audit_append_failed",
                message=str(exc),
            ).model_dump(),
        ) from exc

    return CreateDocumentResponseBody(
        resource_id=result.resource_id,
        revision_id=result.revision_id,
        content_digest_sha256=result.content_digest_sha256,
        recorded_at=result.recorded_at,
    )


@router.post(
    "/documents/{resource_id}/revisions",
    response_model=AppendRevisionResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Append a new Document Revision to an existing Source Document.",
)
async def append_revision(
    request: Request,
    resource_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    evidence_repository: EvidenceRepository = Depends(get_evidence_repository),
) -> AppendRevisionResponseBody:
    """Append a new immutable Document Revision (Requirement 2.4).

    The new Revision's ``parent_revision_id`` is set to the most recent
    prior Revision of ``resource_id`` by the service. A 404 is returned
    when no prior Revision exists — either the Source Document was
    never created or its identifier is wrong; both states are
    indistinguishable through the public API (Property 4 forbids
    leaking the difference).
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = AppendRevisionRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(exc, error_code="invalid_revision_request") from exc

    try:
        with engine.begin() as connection:
            result = evidence_repository.append_revision(
                connection,
                resource_id=resource_id,
                content_bytes=bytes(body.content_bytes),
                contributing_party_id=body.contributing_party_id,
                change_description=body.change_description,
            )
    except InvalidContentError as exc:
        raise _invalid_content_to_http(exc) from exc
    except RevisionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="source_document_not_found",
                message=str(exc),
                resource_id=exc.resource_id,
            ).model_dump(),
        ) from exc
    except AuditAppendError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorBody(
                error="audit_append_failed",
                message=str(exc),
            ).model_dump(),
        ) from exc

    return AppendRevisionResponseBody(
        resource_id=result.resource_id,
        revision_id=result.revision_id,
        parent_revision_id=result.parent_revision_id,
        content_digest_sha256=result.content_digest_sha256,
        recorded_at=result.recorded_at,
    )


@router.post(
    "/documents/{resource_id}/revisions/{revision_id}/regions",
    response_model=CreateRegionResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Record a Content Region Occurrence within a Document Revision.",
)
async def create_region_occurrence(
    request: Request,
    resource_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    evidence_repository: EvidenceRepository = Depends(get_evidence_repository),
) -> CreateRegionResponseBody:
    """Create a Region Occurrence anchored to ``(resource_id, revision_id)``.

    Maps Requirement 3.5 failures (empty, inverted, or out-of-range
    spans) to 400 and unresolvable targets (unknown Document Revision
    or Region) to 404.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = CreateRegionRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(exc, error_code="invalid_region_request") from exc

    try:
        with engine.begin() as connection:
            result = evidence_repository.create_region_occurrence(
                connection,
                resource_id=resource_id,
                revision_id=revision_id,
                start_offset_bytes=body.start_offset_bytes,
                end_offset_bytes=body.end_offset_bytes,
                contributing_party_id=body.contributing_party_id,
                region_id=body.region_id,
            )
    except InvalidContentError as exc:
        raise _invalid_content_to_http(exc) from exc
    except InvalidSpanError as exc:
        raise _invalid_span_to_http(exc) from exc
    except RevisionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="revision_not_found",
                message=str(exc),
                resource_id=exc.resource_id,
                revision_id=exc.revision_id,
            ).model_dump(),
        ) from exc
    except RegionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="region_not_found",
                message=str(exc),
                region_id=exc.region_id,
            ).model_dump(),
        ) from exc
    except AuditAppendError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorBody(
                error="audit_append_failed",
                message=str(exc),
            ).model_dump(),
        ) from exc

    return CreateRegionResponseBody(
        region_id=result.region_id,
        revision_id=result.revision_id,
        start_offset_bytes=result.start_offset_bytes,
        end_offset_bytes=result.end_offset_bytes,
        span_byte_length=result.span_byte_length,
        span_content_digest_sha256=result.span_content_digest_sha256,
        recorded_at=result.recorded_at,
    )


@router.get(
    "/documents/{resource_id}/revisions/{revision_id}",
    response_model=DocumentRevisionResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Read a persisted Document Revision.",
)
async def read_document_revision(
    resource_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
    evidence_repository: EvidenceRepository = Depends(get_evidence_repository),
) -> DocumentRevisionResponseBody:
    """Return the Document Revision matching the composite key.

    A read operation; no audit row is appended (reads are non-
    consequential in this slice — design §"Audit_Log").
    """
    try:
        with engine.connect() as connection:
            revision = evidence_repository.get_revision(
                connection,
                resource_id=resource_id,
                revision_id=revision_id,
            )
    except RevisionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="revision_not_found",
                message=str(exc),
                resource_id=exc.resource_id,
                revision_id=exc.revision_id,
            ).model_dump(),
        ) from exc

    return DocumentRevisionResponseBody(
        resource_id=revision.resource_id,
        revision_id=revision.revision_id,
        parent_revision_id=revision.parent_revision_id,
        content_bytes=base64.b64encode(revision.content_bytes).decode("ascii"),
        content_digest_sha256=revision.content_digest_sha256,
        contributing_party_id=revision.contributing_party_id,
        recorded_at=revision.recorded_at,
        change_description=revision.change_description,
    )


@router.get(
    "/regions/{region_id}/occurrences/{revision_id}",
    response_model=RegionOccurrenceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Read a Region Occurrence anchored to a Document Revision.",
)
async def read_region_occurrence(
    region_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    engine: Engine = Depends(get_engine),
) -> RegionOccurrenceResponseBody:
    """Return the Region Occurrence matching ``(region_id, revision_id)``.

    Direct SQL lookup against ``Region_Occurrences`` (no service method
    yet — task 12.3 will add the byte-span resolution endpoint that
    wraps this query with the actual span bytes). Missing rows map to
    404; the response carries the same shape used elsewhere so a single
    client error handler covers every case.
    """
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    """
                    SELECT region_id, document_revision_id,
                           start_offset_bytes, end_offset_bytes,
                           span_byte_length, span_content_digest_sha256,
                           recorded_at
                    FROM Region_Occurrences
                    WHERE region_id = :region_id
                      AND document_revision_id = :revision_id
                    """
                ),
                {"region_id": region_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="region_occurrence_not_found",
                message=(
                    f"No Region_Occurrences row for region_id={region_id!r}, "
                    f"revision_id={revision_id!r}."
                ),
                region_id=region_id,
                revision_id=revision_id,
            ).model_dump(),
        )

    return RegionOccurrenceResponseBody(
        region_id=row["region_id"],
        revision_id=row["document_revision_id"],
        start_offset_bytes=row["start_offset_bytes"],
        end_offset_bytes=row["end_offset_bytes"],
        span_byte_length=row["span_byte_length"],
        span_content_digest_sha256=row["span_content_digest_sha256"],
        recorded_at=row["recorded_at"],
    )


@router.patch(
    "/documents/{resource_id}/location",
    response_model=RenameDocumentResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorBody},
    },
    summary="Rename or relocate a Source Document, preserving identity.",
)
async def rename_document(
    request: Request,
    resource_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    evidence_repository: EvidenceRepository = Depends(get_evidence_repository),
) -> RenameDocumentResponseBody:
    """Rename a Source Document while preserving every Identity (Requirement 1.3).

    The endpoint:

    1. Reads and validates the request body. ``new_current_location``
       may be ``None`` to clear the display path.
    2. Resolves the actor Party Identity (header preferred, body
       fallback). Missing actor → 400.
    3. Calls :meth:`EvidenceRepository.rename_document` inside one
       transaction so the ``Source_Documents.current_location``
       update and the consequential ``rename.document`` audit row
       commit together (AD-WS-5, Requirement 13.1).

    Returns HTTP 200 with the Resource Identity (unchanged), the new
    and previous ``current_location`` values, and the recorded
    timestamp.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = RenameDocumentRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(exc, error_code="invalid_rename_request") from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.actor_party_id)

    try:
        with engine.begin() as connection:
            result = evidence_repository.rename_document(
                connection,
                resource_id=resource_id,
                new_current_location=body.new_current_location,
                actor_party_id=actor_party_id,
            )
    except SourceDocumentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="source_document_not_found",
                message=str(exc),
                resource_id=exc.resource_id,
            ).model_dump(),
        ) from exc
    except InvalidContentError as exc:
        raise _invalid_content_to_http(exc) from exc
    except AuditAppendError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorBody(
                error="audit_append_failed",
                message=str(exc),
            ).model_dump(),
        ) from exc

    return RenameDocumentResponseBody(
        resource_id=result.resource_id,
        new_current_location=result.new_current_location,
        previous_location=result.previous_location,
        recorded_at=result.recorded_at,
    )
