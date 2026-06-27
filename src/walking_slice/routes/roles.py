"""HTTP routes for the Authorization_Service (task 3.3).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Authorization_Service" HTTP surface:

| Method | Path                                                     | Purpose                                          |
|--------|----------------------------------------------------------|--------------------------------------------------|
| POST   | ``/api/v1/roles/assignments``                            | Assign a contextual role (Requirement 12.1).     |
| POST   | ``/api/v1/roles/assignments/{id}/revocations``           | Revoke a role assignment (Requirement 7.3).      |

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates to
  :class:`walking_slice.authorization.AuthorizationService.assign_role`
  for new assignments and writes ``Role_Assignments.revoked_at`` via the
  one-shot trigger contract for revocations.
- Pre-validate the Requirement 12.6 fields (Party Identity, role, scope,
  granted authorities, effective-start) with Pydantic v2
  :class:`~pydantic.BaseModel` definitions whose
  :func:`~pydantic.Field`/:func:`~pydantic.field_validator` constraints
  catch the obvious shape violations before they reach the service. Any
  :class:`~pydantic.ValidationError` is converted to a structured
  ``HTTP 400`` rather than the FastAPI-default 422 so that the wire
  contract is uniform with the deeper
  :class:`walking_slice.authorization.InvalidRoleAssignmentError`
  response shape (also surfaced as 400).
- Append a ``revoke.role`` ``'consequential'`` ``Audit_Records`` row
  inside the revocation transaction (Requirement 13.1 / AD-WS-5).

Authentication is intentionally **not** wired here. Task 15.1 will replace
the temporary ``X-Actor-Party-Id`` header / ``actor_party_id`` body field
with a bearer-token authenticated ``RequestContext``. Until then this
module accepts the actor's Party Identity from either of those two
locations; if both are present, the header wins so a future production
middleware can simply unconditionally set the header.

Dependency injection follows the slice's general pattern: every
collaborator is reached through a ``Depends(...)`` factory exposed at
module scope. The factories raise :class:`NotImplementedError` by default
so an unwired call fails loudly; tests (and the eventual ``app.py`` in
task 15.2) override them via :data:`fastapi.FastAPI.dependency_overrides`
or :meth:`fastapi.APIRouter.dependency_overrides` (when using
``app.include_router``).

Requirements satisfied (per task 3.3):
    12.1 — Role assignments are recorded with Party Identity, role,
           scope, granted authorities, effective period, and assigning
           authority via the existing
           :class:`AuthorizationService.assign_role` surface.
    12.6 — Submissions missing Party Identity, role, scope, granted
           authorities, or effective-start are rejected with a 400
           response that names the missing field(s); submissions whose
           ``authorities_granted`` carries values outside
           ``{"view", "modify", "approve"}`` are similarly rejected.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any, Final, Optional

import uuid_utils
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
    InvalidRoleAssignmentError,
)
from walking_slice.clock import Clock


__all__ = [
    "AssignRoleRequestBody",
    "AssignRoleResponseBody",
    "ErrorBody",
    "RevokeRoleRequestBody",
    "RevokeRoleResponseBody",
    "get_audit_log",
    "get_authorization_service",
    "get_clock",
    "get_engine",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/roles", tags=["roles"])


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
        "walking_slice.routes.roles.get_engine must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


def get_authorization_service() -> AuthorizationService:
    """Provide the slice's :class:`AuthorizationService` singleton."""
    raise NotImplementedError(
        "walking_slice.routes.roles.get_authorization_service must be "
        "overridden by app composition (task 15.2) or test fixtures."
    )


def get_audit_log() -> AuditLog:
    """Provide the slice's :class:`AuditLog` singleton."""
    raise NotImplementedError(
        "walking_slice.routes.roles.get_audit_log must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


def get_clock() -> Clock:
    """Provide the slice's :class:`Clock` singleton."""
    raise NotImplementedError(
        "walking_slice.routes.roles.get_clock must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Pydantic v2 boundary models.
#
# The constraints below are Requirement 12.6's "missing field" rules
# rendered as a Pydantic schema:
#
#   - ``party_id``, ``role_name``, ``scope`` — non-empty strings.
#   - ``authorities_granted`` — non-empty list of values drawn from
#     ``{"view", "modify", "approve"}`` (validated by
#     :meth:`AssignRoleRequestBody._validate_authorities`).
#   - ``effective_start`` — required ISO-8601 datetime; Pydantic parses
#     the string into a timezone-aware :class:`datetime.datetime`
#     automatically.
#
# Field-level errors are converted to HTTP 400 (not the default 422) so
# every "request shape is wrong" failure carries the same status code as
# the deeper :class:`InvalidRoleAssignmentError` raised by the service.
# ---------------------------------------------------------------------------


_VALID_AUTHORITIES: Final[frozenset[str]] = frozenset({"view", "modify", "approve"})


class AssignRoleRequestBody(BaseModel):
    """Validated body of ``POST /api/v1/roles/assignments``.

    Mirrors :class:`walking_slice.authorization.AssignRoleRequest` at the
    HTTP boundary and enforces Requirement 12.6's "required fields" rule
    before the request reaches
    :meth:`AuthorizationService.assign_role`. ``extra="forbid"`` rejects
    typo'd field names so clients receive an explicit error rather than
    silently dropped attributes.
    """

    model_config = ConfigDict(extra="forbid")

    party_id: str = Field(min_length=1, description="Identity of the Party receiving the role.")
    role_name: str = Field(min_length=1, description="Symbolic role name (e.g. 'decision_maker').")
    scope: str = Field(min_length=1, description="Opaque scope identifier the role is bounded to.")
    authorities_granted: list[str] = Field(
        min_length=1,
        description=(
            "Subset of {'view', 'modify', 'approve'} the role grants. "
            "Per Requirement 12.3/12.4 these authorities are not substitutable."
        ),
    )
    effective_start: datetime = Field(
        description="Earliest UTC instant at which the assignment is in effect.",
    )
    effective_end: Optional[datetime] = Field(
        default=None,
        description="Latest UTC instant at which the assignment is in effect (open-ended when omitted).",
    )
    # Placeholder until task 15.1 wires bearer-token authentication. Either
    # this field or the ``X-Actor-Party-Id`` header must carry the Resource
    # Steward's Party Identity; the header takes precedence when both are
    # supplied so a future middleware can simply set the header
    # unconditionally.
    actor_party_id: Optional[str] = Field(
        default=None,
        description=(
            "PLACEHOLDER (task 15.1): Party Identity of the Resource Steward "
            "recording the assignment. Carried here or in the "
            "'X-Actor-Party-Id' header until bearer-token auth lands."
        ),
    )

    @field_validator("authorities_granted")
    @classmethod
    def _validate_authorities(cls, value: list[str]) -> list[str]:
        """Reject any authority outside ``{view, modify, approve}``.

        Mirrored by the deeper validation in
        :meth:`AuthorizationService.assign_role`; we duplicate the check
        here so the request fails fast with a Pydantic-flavoured error
        message before a database connection is opened.
        """
        invalid = [authority for authority in value if authority not in _VALID_AUTHORITIES]
        if invalid:
            raise ValueError(
                f"authorities_granted contains values outside "
                f"{sorted(_VALID_AUTHORITIES)!r}: {invalid!r}"
            )
        return value


class AssignRoleResponseBody(BaseModel):
    """Successful response from ``POST /api/v1/roles/assignments``."""

    model_config = ConfigDict(extra="forbid")

    role_assignment_id: str = Field(
        description="Canonical UUIDv7 identifier of the newly recorded role assignment.",
    )


class RevokeRoleRequestBody(BaseModel):
    """Optional body of ``POST /api/v1/roles/assignments/{id}/revocations``.

    The body is optional because the actor identity can be supplied via
    the ``X-Actor-Party-Id`` header. Including this model keeps the
    placeholder field documented and lets clients send a JSON body when
    that is more ergonomic for them.
    """

    model_config = ConfigDict(extra="forbid")

    actor_party_id: Optional[str] = Field(
        default=None,
        description=(
            "PLACEHOLDER (task 15.1): Party Identity of the Resource Steward "
            "recording the revocation. Carried here or in the "
            "'X-Actor-Party-Id' header until bearer-token auth lands."
        ),
    )


class RevokeRoleResponseBody(BaseModel):
    """Successful response from the revocation endpoint."""

    model_config = ConfigDict(extra="forbid")

    role_assignment_id: str
    revoked_at: str = Field(
        description="UTC ISO-8601 timestamp (millisecond precision) at which revocation was recorded.",
    )


class ErrorBody(BaseModel):
    """Structured error body returned on 400 / 404 / 409 responses.

    The shape is intentionally narrow: an ``error`` tag (machine-readable
    code) plus optional context fields. The ``missing`` and ``invalid``
    lists are populated when the underlying
    :class:`InvalidRoleAssignmentError` carries them so the wire response
    mirrors the service-layer exception attributes.
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    message: Optional[str] = None
    missing: list[str] = Field(default_factory=list)
    invalid: list[str] = Field(default_factory=list)
    role_assignment_id: Optional[str] = None
    revoked_at: Optional[str] = None
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
    every consequential action so the audit row in this transaction has a
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
                    "header or as a top-level 'actor_party_id' field on the "
                    "request body (placeholder until task 15.1)."
                ),
                missing=["actor_party_id"],
            ).model_dump(),
        )
    return actor


async def _read_json_body(request: Request, *, required: bool) -> Optional[Any]:
    """Read and JSON-decode the request body, returning ``None`` when empty.

    The ``required`` flag distinguishes the assignment endpoint (body
    required) from the revocation endpoint (body optional — the actor can
    travel on the header alone). Decode failures are surfaced as 400 with
    a clear ``invalid_json_body`` code rather than the FastAPI default.
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


def _new_correlation_id() -> str:
    """Generate a fresh correlation identifier for this HTTP operation.

    Correlation identifiers are not domain Resource identifiers but they
    share the same canonical UUIDv7 form used elsewhere in the slice so
    they sort temporally. Task 15.1 will replace this with a value
    derived from the inbound ``X-Correlation-Id`` header when present.
    """
    return str(uuid_utils.uuid7())


def _validation_error_to_http(exc: ValidationError) -> HTTPException:
    """Convert a Pydantic :class:`ValidationError` to a 400 ``HTTPException``.

    The ``missing`` list mirrors Requirement 12.6's "missing field"
    language by extracting field names from errors whose Pydantic type is
    ``missing`` (the v2 code for "field absent from input"). All other
    errors land in ``validation_errors`` so clients can see the full
    detail.
    """
    errors = exc.errors(include_url=False)
    missing: list[str] = []
    invalid: list[str] = []
    other: list[dict[str, Any]] = []
    for err in errors:
        loc = err.get("loc", ())
        field = ".".join(str(part) for part in loc) if loc else "<root>"
        err_type = err.get("type", "")
        if err_type in {"missing", "missing_argument"}:
            missing.append(field)
        elif err_type == "value_error" and "authorities" in field:
            invalid.append(field)
            other.append(_strip_error_ctx(err))
        else:
            other.append(_strip_error_ctx(err))
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=ErrorBody(
            error="invalid_role_assignment",
            message="Role assignment request failed validation.",
            missing=sorted(set(missing)),
            invalid=sorted(set(invalid)),
            validation_errors=other,
        ).model_dump(),
    )


def _strip_error_ctx(err: dict[str, Any]) -> dict[str, Any]:
    """Strip non-JSON-serialisable ``ctx`` values from a Pydantic error.

    Pydantic v2 attaches the original exception object in ``ctx['error']``
    for ``value_error`` failures, which breaks JSON encoding. We keep the
    rest of the error dict intact so callers see the offending field, the
    error type, and the message.
    """
    return {key: value for key, value in err.items() if key != "ctx"}


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.post(
    "/assignments",
    response_model=AssignRoleResponseBody,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
    },
    summary="Record a contextual role assignment.",
)
async def create_role_assignment(
    request: Request,
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    authorization_service: AuthorizationService = Depends(get_authorization_service),
) -> AssignRoleResponseBody:
    """Create a new role assignment per design §"Authorization_Service".

    The endpoint:

    1. Reads and JSON-decodes the request body (400 on empty / malformed).
    2. Validates the body against :class:`AssignRoleRequestBody`; any
       :class:`~pydantic.ValidationError` becomes a 400 ``ErrorBody``
       that names the missing or invalid fields (Requirement 12.6).
    3. Resolves the actor Party Identity from the header or body (400
       when neither is supplied — placeholder until task 15.1).
    4. Calls :meth:`AuthorizationService.assign_role` inside a single
       transaction so the ``Role_Assignments`` insert and the
       ``Audit_Records`` consequential append commit together (AD-WS-5,
       Requirement 13.1). Any
       :class:`~walking_slice.authorization.InvalidRoleAssignmentError`
       returned by the deeper validation is also surfaced as 400.

    Returns the canonical UUIDv7 identifier of the new assignment, with
    HTTP 201 ``Created``.
    """
    payload = await _read_json_body(request, required=True)
    try:
        body = AssignRoleRequestBody.model_validate(payload)
    except ValidationError as exc:
        raise _validation_error_to_http(exc) from exc

    actor_party_id = _resolve_actor(x_actor_party_id, body.actor_party_id)

    service_request = AssignRoleRequest(
        party_id=body.party_id,
        role_name=body.role_name,
        scope=body.scope,
        authorities_granted=tuple(body.authorities_granted),
        effective_start=body.effective_start,
        effective_end=body.effective_end,
        assigning_authority_id=actor_party_id,
    )

    try:
        with engine.begin() as connection:
            role_assignment_id = authorization_service.assign_role(
                connection, service_request
            )
    except InvalidRoleAssignmentError as exc:
        # Defence in depth: the Pydantic body should already have caught
        # the obvious shapes, but the service may apply rules the HTTP
        # surface does not know about. Surfacing the same 400 keeps the
        # wire contract uniform.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="invalid_role_assignment",
                message=str(exc),
                missing=list(exc.missing),
                invalid=list(exc.invalid),
            ).model_dump(),
        ) from exc

    return AssignRoleResponseBody(role_assignment_id=str(role_assignment_id))


@router.post(
    "/assignments/{role_assignment_id}/revocations",
    response_model=RevokeRoleResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
        status.HTTP_409_CONFLICT: {"model": ErrorBody},
    },
    summary="Revoke an existing role assignment.",
)
async def revoke_role_assignment(
    request: Request,
    role_assignment_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    audit_log: AuditLog = Depends(get_audit_log),
    clock: Clock = Depends(get_clock),
) -> RevokeRoleResponseBody:
    """Record the one-shot revocation of a role assignment.

    Implements the contract called out by task 3.3 and the
    ``Role_Assignments.revoked_at`` one-shot trigger installed by
    :mod:`walking_slice.persistence`:

    1. Read the request body if present; allow it to be empty when the
       actor is supplied via header.
    2. Resolve the actor Party Identity (400 when missing — placeholder
       for task 15.1).
    3. Open a transaction. Verify the target row exists (404) and is not
       already revoked (409). Then issue
       ``UPDATE Role_Assignments SET revoked_at = :ts WHERE
       role_assignment_id = :rid AND revoked_at IS NULL``; the
       ``revoked_at IS NULL`` clause is defence-in-depth against a TOCTOU
       race — if rowcount is zero we surface 409.
    4. Append a ``'consequential'`` ``revoke.role`` row to
       ``Audit_Records`` inside the same transaction so the revocation
       and the audit row commit together (AD-WS-5, Requirement 13.1).
    """
    payload = await _read_json_body(request, required=False)
    body: Optional[RevokeRoleRequestBody]
    if payload is None:
        body = None
    else:
        try:
            body = RevokeRoleRequestBody.model_validate(payload)
        except ValidationError as exc:
            raise _validation_error_to_http(exc) from exc

    actor_party_id = _resolve_actor(
        x_actor_party_id, body.actor_party_id if body is not None else None
    )

    revoked_at_dt = clock.now()
    revoked_at_iso = format_iso8601_ms(revoked_at_dt)
    correlation_id = _new_correlation_id()

    try:
        with engine.begin() as connection:
            existing = connection.execute(
                text(
                    "SELECT revoked_at FROM Role_Assignments "
                    "WHERE role_assignment_id = :rid"
                ),
                {"rid": role_assignment_id},
            ).first()

            if existing is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=ErrorBody(
                        error="role_assignment_not_found",
                        message="No role assignment exists with the given identifier.",
                        role_assignment_id=role_assignment_id,
                    ).model_dump(),
                )

            if existing[0] is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=ErrorBody(
                        error="role_assignment_already_revoked",
                        message="The role assignment is already revoked; revocation is one-shot.",
                        role_assignment_id=role_assignment_id,
                        revoked_at=existing[0],
                    ).model_dump(),
                )

            update_result = connection.execute(
                text(
                    "UPDATE Role_Assignments SET revoked_at = :ts "
                    "WHERE role_assignment_id = :rid AND revoked_at IS NULL"
                ),
                {"ts": revoked_at_iso, "rid": role_assignment_id},
            )
            if update_result.rowcount == 0:
                # Defence-in-depth race guard: another transaction
                # revoked the assignment between our SELECT and UPDATE.
                # Treat the same as the explicit already-revoked branch
                # so the wire contract is uniform.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=ErrorBody(
                        error="role_assignment_already_revoked",
                        message="The role assignment was revoked concurrently.",
                        role_assignment_id=role_assignment_id,
                    ).model_dump(),
                )

            try:
                audit_log.append_consequential(
                    connection,
                    actor_party_id=actor_party_id,
                    action_type="revoke.role",
                    target_id=role_assignment_id,
                    target_revision_id=None,
                    correlation_id=correlation_id,
                    recorded_time=revoked_at_dt,
                )
            except AuditAppendError as exc:
                # The audit append failed; rolling back the surrounding
                # transaction discards the revocation as well, which is
                # exactly the behaviour Requirement 13.6 prescribes.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=ErrorBody(
                        error="audit_append_failed",
                        message=str(exc),
                    ).model_dump(),
                ) from exc
    except HTTPException:
        # ``HTTPException`` raised inside ``engine.begin()`` causes the
        # transaction to roll back via ``__exit__`` — we want that.
        raise

    return RevokeRoleResponseBody(
        role_assignment_id=role_assignment_id,
        revoked_at=revoked_at_iso,
    )
