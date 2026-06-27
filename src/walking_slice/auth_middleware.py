"""Bearer-token authentication and ``RequestContext`` injection (task 15.1).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Application-Level Composition":

    The FastAPI router wires components together via constructor injection.
    Each request creates a ``RequestContext`` bundle ... Endpoints accept
    ``RequestContext`` and the parsed request body. Internal services accept
    ``RequestContext`` rather than reading globals — this is essential for
    property tests to inject deterministic clocks and isolated SQLite
    instances.

Design reference: ``design.md`` §"Cross-Cutting Concerns" → *Authorization*:

    Every write endpoint resolves the calling Party from a bearer token
    (HMAC-signed JSON Web Token, signed by a slice-local key — the slice
    does not federate to an external IdP).

This module provides three things:

1. :func:`verify_bearer_token` — pure function that validates an
   HMAC-SHA256 ("HS256") signed JWT against a slice-local secret. The
   implementation uses only :mod:`hmac`, :mod:`hashlib`, and
   :mod:`base64` from the standard library so we do not pull in
   ``pyjwt`` for the small surface the slice exercises.

   Validation rules (in this order):

   * The token must consist of three ``.``-separated base64url segments.
   * The header must declare ``alg="HS256"`` and ``typ="JWT"``. The
     ``alg="none"`` family is explicitly rejected.
   * The signature must equal ``HMAC-SHA256(secret, b"<header>.<payload>")``
     under a constant-time comparison.
   * When present, ``nbf`` (not-before) and ``exp`` (expiration) are
     evaluated against the resolver's :class:`~walking_slice.clock.Clock`.
   * ``sub`` (subject) must be a non-empty string — it carries the
     calling Party Identity per design §"Authorization".

2. :class:`RequestContext` — the per-request bundle from design
   §"Application-Level Composition". The design names the SQLAlchemy
   slot ``db: Session`` but the walking slice runs on SQLAlchemy *Core*
   (per design §"Persistence Strategy"), so the bundle exposes the
   per-process :class:`~sqlalchemy.engine.Engine` instead. Every write
   endpoint opens a transaction via ``ctx.engine.begin()`` so the
   transactional contract from AD-WS-5 is unchanged.

3. :class:`RequestContextResolver` — a callable that is mounted as a
   FastAPI dependency. Each call extracts the bearer token from the
   request's ``Authorization`` header, validates it, resolves the Party
   Identity from the ``sub`` claim, and returns a fully-populated
   :class:`RequestContext` for the route handler.

   **Backward-compatibility shim.** Until every existing route is
   migrated off the placeholder ``X-Actor-Party-Id`` header (task 15.2
   and the wave-22 route updates), the resolver also accepts that
   header as a fallback when no bearer token is present. This keeps
   the wave-7 through wave-20 end-to-end tests green while the
   authentication layer is wired in. Both paths produce an equivalent
   :class:`RequestContext`; only the ``correlation_id`` differs (token
   path uses ``jti`` when present, header path generates a fresh
   correlation identifier).

Requirements satisfied:
    7.1 — every consequential endpoint resolves a Party Identity before
          touching the Authorization_Service.
    12.1 — Role assignment endpoints (and every other endpoint that
          performs an authorization evaluation) receive the actor
          Party Identity through the ``RequestContext`` so it can be
          recorded as the ``actor_party_id`` on the audit row.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Mapping, Optional

from fastapi import HTTPException, Request, status
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AuthorizationService
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService


__all__ = [
    "ACTOR_HEADER",
    "AUTHORIZATION_HEADER",
    "BEARER_PREFIX",
    "InvalidTokenError",
    "MissingTokenError",
    "RequestContext",
    "RequestContextResolver",
    "TokenExpiredError",
    "create_bearer_token",
    "verify_bearer_token",
]


# ---------------------------------------------------------------------------
# Public constants.
# ---------------------------------------------------------------------------

#: HTTP header carrying the bearer token.
AUTHORIZATION_HEADER: Final[str] = "Authorization"

#: Case-insensitive prefix on the ``Authorization`` header value. We compare
#: with :meth:`str.lower` so ``"Bearer "``, ``"bearer "``, and ``"BEARER "``
#: are all accepted, matching RFC 6750 §2.1.
BEARER_PREFIX: Final[str] = "bearer "

#: Placeholder header used by the wave-7 routes until every endpoint moves
#: to the bearer-token surface. Kept here so the resolver and the existing
#: routes import the same constant.
ACTOR_HEADER: Final[str] = "X-Actor-Party-Id"


# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class InvalidTokenError(Exception):
    """Raised when a bearer token fails validation for any reason.

    The :attr:`reason` field carries a stable machine-readable code that
    callers map to an HTTP 401 body. The string is intentionally generic
    (e.g. ``"invalid_signature"`` rather than ``"signature mismatch on
    HS256 with key 0x..."``) so denial responses do not leak verification
    internals per design §"Privacy and inference leakage".
    """

    def __init__(self, reason: str, *, detail: str | None = None) -> None:
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


class MissingTokenError(InvalidTokenError):
    """Raised when no bearer token is supplied on the request.

    Distinct from :class:`InvalidTokenError` so the resolver can choose
    between "no token at all → fall back to ``X-Actor-Party-Id``" and
    "token present but malformed → return 401".
    """

    def __init__(self) -> None:
        super().__init__("missing_bearer_token")


class TokenExpiredError(InvalidTokenError):
    """Raised when the token's ``exp`` claim is at or before the current time."""

    def __init__(self) -> None:
        super().__init__("token_expired")


# ---------------------------------------------------------------------------
# Base64url helpers.
#
# JWT uses URL-safe base64 *without* trailing ``=`` padding (RFC 7515 §2).
# The stdlib only accepts padded input, so we re-pad on decode and strip on
# encode. ``binascii.Error`` is normalized to ``InvalidTokenError`` so all
# token errors travel through one exception type.
# ---------------------------------------------------------------------------


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url segment, restoring any stripped ``=`` padding."""
    try:
        # Encode to ASCII first so callers can pass either ``str`` or ``bytes``.
        raw = segment.encode("ascii")
    except UnicodeEncodeError as exc:
        raise InvalidTokenError("malformed_token", detail=str(exc)) from exc
    padding = b"=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw + padding)
    except (binascii.Error, ValueError) as exc:
        raise InvalidTokenError("malformed_token", detail=str(exc)) from exc


def _b64url_encode(data: bytes) -> str:
    """Encode bytes as a base64url segment without trailing padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# JWT verification.
# ---------------------------------------------------------------------------


def verify_bearer_token(
    token: str,
    secret: bytes,
    *,
    now: Optional[datetime] = None,
    leeway_seconds: int = 0,
) -> dict[str, Any]:
    """Validate an HMAC-SHA256 signed JWT and return its claims.

    Args:
        token: The compact-serialized JWT (``header.payload.signature``)
            extracted from the ``Authorization: Bearer ...`` header.
        secret: The slice-local signing key (raw bytes).
        now: Reference time used to evaluate ``exp``/``nbf``. When
            ``None``, defaults to :func:`datetime.now` in UTC. Pass an
            explicit value (typically ``ctx.clock.now()``) from
            production code so the verifier and the audit log share one
            clock.
        leeway_seconds: Tolerance applied to ``exp`` and ``nbf`` to
            accommodate small clock skew. Defaults to zero — the
            walking slice runs a single process against a deterministic
            clock in tests, so no skew is expected.

    Returns:
        The decoded JWT payload (claims) as a :class:`dict`.

    Raises:
        InvalidTokenError: The token is malformed, the algorithm is
            unsupported, the signature does not verify, or a required
            claim is missing or malformed.
        TokenExpiredError: The token's ``exp`` claim is at or before
            ``now``.

    The verifier is intentionally narrow: it only supports the ``HS256``
    algorithm because that is the only algorithm the slice signs with
    (design §"Cross-Cutting Concerns" — *Authorization*). The
    ``alg="none"`` family — which would let an attacker submit an
    unsigned token and bypass signature verification — is explicitly
    rejected before any other check.
    """
    if not isinstance(token, str) or not token:
        raise InvalidTokenError("malformed_token", detail="token must be a non-empty string")

    segments = token.split(".")
    if len(segments) != 3:
        raise InvalidTokenError(
            "malformed_token",
            detail=f"expected 3 segments, got {len(segments)}",
        )
    header_segment, payload_segment, signature_segment = segments

    header_bytes = _b64url_decode(header_segment)
    payload_bytes = _b64url_decode(payload_segment)
    signature = _b64url_decode(signature_segment)

    try:
        header = json.loads(header_bytes)
    except json.JSONDecodeError as exc:
        raise InvalidTokenError("malformed_header", detail=str(exc)) from exc
    if not isinstance(header, dict):
        raise InvalidTokenError("malformed_header", detail="header is not a JSON object")

    # Reject ``alg=none`` and any non-HS256 algorithm explicitly. We compare
    # against the canonical upper-case string so ``"HS256"`` matches while
    # ``"none"``, ``"None"``, or ``"NONE"`` all fail.
    alg = header.get("alg")
    if alg != "HS256":
        raise InvalidTokenError(
            "unsupported_algorithm",
            detail=f"expected alg=HS256, got alg={alg!r}",
        )
    typ = header.get("typ")
    if typ is not None and typ != "JWT":
        raise InvalidTokenError(
            "unsupported_type",
            detail=f"expected typ=JWT, got typ={typ!r}",
        )

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = hmac.new(secret, signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise InvalidTokenError("invalid_signature")

    try:
        claims = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise InvalidTokenError("malformed_payload", detail=str(exc)) from exc
    if not isinstance(claims, dict):
        raise InvalidTokenError("malformed_payload", detail="payload is not a JSON object")

    reference_now = now if now is not None else datetime.now(timezone.utc)
    if reference_now.tzinfo is None:
        # Defensive: every Clock implementation returns UTC, but a caller
        # that passes a naive datetime would silently produce wrong
        # comparisons against ``exp`` and ``nbf`` (both UNIX seconds).
        raise InvalidTokenError(
            "malformed_clock",
            detail="now must be a timezone-aware datetime",
        )
    now_epoch = reference_now.timestamp()

    nbf = claims.get("nbf")
    if nbf is not None:
        if not isinstance(nbf, (int, float)):
            raise InvalidTokenError("malformed_nbf", detail="nbf must be a number")
        if now_epoch + leeway_seconds < float(nbf):
            raise InvalidTokenError("token_not_yet_valid")

    exp = claims.get("exp")
    if exp is not None:
        if not isinstance(exp, (int, float)):
            raise InvalidTokenError("malformed_exp", detail="exp must be a number")
        if now_epoch - leeway_seconds >= float(exp):
            raise TokenExpiredError()

    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise InvalidTokenError(
            "missing_subject",
            detail="sub claim must be a non-empty string carrying the Party Identity",
        )

    return claims


def create_bearer_token(
    claims: Mapping[str, Any],
    secret: bytes,
) -> str:
    """Sign ``claims`` with ``secret`` and return the compact JWT string.

    This helper exists so tests (and any internal tooling that needs to
    mint tokens) can construct a valid token without a third-party
    library. Production code paths sign tokens through whatever issuance
    surface the deploying environment provides; this helper is *only*
    meant for tests and local development.
    """
    header = {"alg": "HS256", "typ": "JWT"}
    header_segment = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_segment = _b64url_encode(json.dumps(dict(claims), separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(secret, signing_input, hashlib.sha256).digest()
    signature_segment = _b64url_encode(signature)
    return f"{header_segment}.{payload_segment}.{signature_segment}"


# ---------------------------------------------------------------------------
# RequestContext.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestContext:
    """Per-request dependency bundle (design §"Application-Level Composition").

    The design lists these fields::

        party_id, token_correlation_id, clock, db, ids, authz, audit

    The walking slice runs on SQLAlchemy *Core* (per AD-WS-1 and design
    §"Persistence Strategy"), so the bundle exposes the per-process
    :class:`~sqlalchemy.engine.Engine` instead of an ORM
    :class:`~sqlalchemy.orm.Session`. Route handlers open transactions
    via ``ctx.engine.begin()``; the transactional contract from AD-WS-5
    is unchanged.

    ``correlation_id`` is named per the task description (the design's
    ``token_correlation_id``). When the request supplies a JWT with a
    ``jti`` claim, that value is reused so audit rows tie back to the
    issued token. Otherwise the resolver mints a fresh UUIDv7 via
    :class:`~walking_slice.identity.IdentityService` so every request
    still has a stable correlation handle for the audit log.
    """

    party_id: str
    correlation_id: str
    clock: Clock
    engine: Engine
    ids: IdentityService
    authz: AuthorizationService
    audit: AuditLog


# ---------------------------------------------------------------------------
# FastAPI dependency.
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class RequestContextResolver:
    """Callable that builds a :class:`RequestContext` for each request.

    The dataclass is constructed with ``eq=False`` so it falls back to
    identity-based equality and hashing. FastAPI uses dependency
    callables as keys in its per-request dependency cache (see
    ``fastapi.dependencies.utils.solve_dependencies``); a default
    ``@dataclass`` would generate ``__eq__`` and silently null out
    ``__hash__`` so the resolver instance becomes unhashable and the
    request fails with ``TypeError: unhashable type``. Identity-based
    hashing is the right semantics anyway — each resolver instance
    represents one wiring of services and should be distinct from
    every other.

    Mount this resolver as a FastAPI dependency::

        resolver = RequestContextResolver(
            secret=b"slice-local-key",
            clock=clock,
            engine=engine,
            ids=identity_service,
            authz=authorization_service,
            audit=audit_log,
        )
        app.dependency_overrides[get_request_context] = resolver

    Each invocation:

    1. Reads the ``Authorization`` header; if it carries a
       ``Bearer <token>`` value, validates the token via
       :func:`verify_bearer_token` and uses the ``sub`` claim as the
       Party Identity.
    2. Otherwise, falls back to the legacy ``X-Actor-Party-Id`` header
       so the wave-7 routes keep working until they are migrated. This
       fallback is intended to disappear once the wave-22 route updates
       land; it is gated by :attr:`allow_actor_header_fallback` so a
       production deployment can flip the bool off and require bearer
       tokens unconditionally.
    3. Raises :class:`fastapi.HTTPException` (401) when no party can be
       resolved or the token is invalid. The body follows the AD-WS-9
       denial shape so token errors are indistinguishable from
       authorization denials at the wire level.

    Attributes:
        secret: Slice-local HMAC-SHA256 signing key.
        clock: Injected per-request clock; reused inside the resulting
            :class:`RequestContext`.
        engine: SQLAlchemy Core engine bound to the slice's SQLite store.
        ids: Identity_Service singleton; the resolver also uses it to
            mint correlation identifiers when the token omits ``jti``.
        authz: Authorization_Service singleton, threaded through every
            consequential write path.
        audit: Audit_Log singleton, threaded through every consequential
            and denied write path.
        allow_actor_header_fallback: When ``True`` (default during the
            backward-compatibility window) the resolver accepts the
            ``X-Actor-Party-Id`` header as a fallback. When ``False``,
            every request must carry a valid bearer token.
        leeway_seconds: Forwarded to :func:`verify_bearer_token`.
    """

    secret: bytes
    clock: Clock
    engine: Engine
    ids: IdentityService
    authz: AuthorizationService
    audit: AuditLog
    allow_actor_header_fallback: bool = True
    leeway_seconds: int = 0

    def __call__(self, request: Request) -> RequestContext:
        """Build a :class:`RequestContext` from ``request``.

        FastAPI invokes this on every route handler that depends on
        ``RequestContext``. The function is sync because the underlying
        validation is pure CPU work — there is no I/O on the happy path,
        so introducing ``async`` would only add overhead.
        """
        authorization = request.headers.get(AUTHORIZATION_HEADER)
        if authorization:
            party_id, correlation_id = self._resolve_via_bearer(authorization)
        elif self.allow_actor_header_fallback:
            party_id, correlation_id = self._resolve_via_actor_header(request)
        else:
            raise self._unauthorized("missing_bearer_token")

        return RequestContext(
            party_id=party_id,
            correlation_id=correlation_id,
            clock=self.clock,
            engine=self.engine,
            ids=self.ids,
            authz=self.authz,
            audit=self.audit,
        )

    # ------------------------------------------------------------------
    # Resolution helpers.
    # ------------------------------------------------------------------

    def _resolve_via_bearer(self, header_value: str) -> tuple[str, str]:
        """Extract and validate the bearer token, returning ``(party_id, correlation_id)``."""
        if not header_value.lower().startswith(BEARER_PREFIX):
            raise self._unauthorized(
                "malformed_authorization_header",
                detail="Authorization header must use the Bearer scheme",
            )
        token = header_value[len(BEARER_PREFIX) :].strip()
        if not token:
            raise self._unauthorized("missing_bearer_token")

        try:
            claims = verify_bearer_token(
                token,
                self.secret,
                now=self.clock.now(),
                leeway_seconds=self.leeway_seconds,
            )
        except TokenExpiredError as exc:
            raise self._unauthorized(exc.reason) from exc
        except InvalidTokenError as exc:
            raise self._unauthorized(exc.reason) from exc

        party_id = claims["sub"]  # presence already enforced by verify_bearer_token
        jti = claims.get("jti")
        if isinstance(jti, str) and jti:
            correlation_id = jti
        else:
            correlation_id = self._mint_correlation_id()
        return party_id, correlation_id

    def _resolve_via_actor_header(self, request: Request) -> tuple[str, str]:
        """Backward-compat fallback for routes still on ``X-Actor-Party-Id``."""
        actor = request.headers.get(ACTOR_HEADER)
        if not actor or not actor.strip():
            raise self._unauthorized(
                "missing_credentials",
                detail=(
                    "Request must carry an Authorization: Bearer header or "
                    f"the legacy {ACTOR_HEADER} header"
                ),
            )
        return actor.strip(), self._mint_correlation_id()

    def _mint_correlation_id(self) -> str:
        """Return a fresh correlation identifier.

        Prefers a UUIDv7 from the injected :class:`IdentityService` so
        every correlation id sorts by issuance time. Falls back to
        :func:`secrets.token_hex` if the identity service is
        unavailable for any reason — the fallback is defensive only;
        production wiring always passes a real service.
        """
        try:
            return self.ids.new_immutable_record_id()
        except Exception:  # pragma: no cover - defensive
            return secrets.token_hex(16)

    @staticmethod
    def _unauthorized(reason: str, *, detail: str | None = None) -> HTTPException:
        """Build the 401 :class:`HTTPException` for token errors.

        The body matches the AD-WS-9 denial shape (``generic_denial_indicator``,
        ``reason_code``, ``correlation_id``) so token errors travel through
        the same response format authorization denials use. The
        ``correlation_id`` is a fresh random value because we do not have a
        per-request handle when a token fails this early.
        """
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "generic_denial_indicator": "unauthorized",
                "reason_code": reason,
                "correlation_id": secrets.token_hex(16),
                "message": detail or reason,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
