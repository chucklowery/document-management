"""Unit tests for :mod:`walking_slice.auth_middleware` (task 15.1).

The middleware must:

- Validate an HMAC-SHA256 signed JWT against a slice-local secret and
  return the decoded claims (Requirement 7.1, 12.1).
- Reject malformed tokens, expired tokens, ``alg=none``, and tokens
  signed with the wrong key.
- Produce a :class:`RequestContext` whose ``party_id`` is taken from the
  ``sub`` claim and whose collaborators (clock, engine, identity
  service, authorization service, audit log) are the bundle injected
  into the resolver.
- Fall back to the legacy ``X-Actor-Party-Id`` header when no bearer
  token is supplied so the wave-7 routes keep working until they are
  migrated. The fallback can be disabled via
  ``allow_actor_header_fallback=False``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.auth_middleware import (
    ACTOR_HEADER,
    AUTHORIZATION_HEADER,
    InvalidTokenError,
    RequestContext,
    RequestContextResolver,
    TokenExpiredError,
    _b64url_encode,
    create_bearer_token,
    verify_bearer_token,
)
from walking_slice.authorization import AuthorizationService
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-0000000000aa"
_SECRET = b"slice-local-key-for-tests-only"
_NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _claims(*, sub: str = _PARTY_ID, exp_offset: int | None = 3600, **extra) -> dict:
    payload = {"sub": sub, "iat": int(_NOW.timestamp())}
    if exp_offset is not None:
        payload["exp"] = int(_NOW.timestamp()) + exp_offset
    payload.update(extra)
    return payload


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock(_NOW)


@pytest.fixture
def resolver(
    fixed_clock: FixedClock,
    engine: Engine,
    identity_service: IdentityService,
    authorization_service: AuthorizationService,
    audit_log: AuditLog,
) -> RequestContextResolver:
    return RequestContextResolver(
        secret=_SECRET,
        clock=fixed_clock,
        engine=engine,
        ids=identity_service,
        authz=authorization_service,
        audit=audit_log,
    )


# ---------------------------------------------------------------------------
# verify_bearer_token — happy path.
# ---------------------------------------------------------------------------


def test_verify_returns_claims_for_valid_token() -> None:
    token = create_bearer_token(_claims(), _SECRET)
    claims = verify_bearer_token(token, _SECRET, now=_NOW)
    assert claims["sub"] == _PARTY_ID
    assert claims["exp"] == int(_NOW.timestamp()) + 3600


def test_verify_accepts_token_without_exp() -> None:
    """``exp`` is optional per RFC 7519 §4.1.4."""
    token = create_bearer_token(_claims(exp_offset=None), _SECRET)
    claims = verify_bearer_token(token, _SECRET, now=_NOW)
    assert claims["sub"] == _PARTY_ID
    assert "exp" not in claims


def test_verify_accepts_token_with_jti() -> None:
    token = create_bearer_token(_claims(jti="corr-123"), _SECRET)
    claims = verify_bearer_token(token, _SECRET, now=_NOW)
    assert claims["jti"] == "corr-123"


# ---------------------------------------------------------------------------
# verify_bearer_token — rejection paths.
# ---------------------------------------------------------------------------


def test_verify_rejects_token_signed_with_wrong_secret() -> None:
    token = create_bearer_token(_claims(), _SECRET)
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_bearer_token(token, b"different-key", now=_NOW)
    assert exc_info.value.reason == "invalid_signature"


def test_verify_rejects_expired_token() -> None:
    token = create_bearer_token(_claims(exp_offset=-1), _SECRET)
    with pytest.raises(TokenExpiredError) as exc_info:
        verify_bearer_token(token, _SECRET, now=_NOW)
    assert exc_info.value.reason == "token_expired"


def test_verify_rejects_not_yet_valid_token() -> None:
    future = int((_NOW + timedelta(hours=1)).timestamp())
    token = create_bearer_token(_claims(nbf=future), _SECRET)
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_bearer_token(token, _SECRET, now=_NOW)
    assert exc_info.value.reason == "token_not_yet_valid"


def test_verify_rejects_alg_none() -> None:
    """The ``alg=none`` family is the classic JWT bypass; reject explicitly."""
    header = {"alg": "none", "typ": "JWT"}
    header_segment = _b64url_encode(json.dumps(header).encode("utf-8"))
    payload_segment = _b64url_encode(json.dumps(_claims()).encode("utf-8"))
    token = f"{header_segment}.{payload_segment}."
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_bearer_token(token, _SECRET, now=_NOW)
    assert exc_info.value.reason == "unsupported_algorithm"


def test_verify_rejects_token_with_two_segments() -> None:
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_bearer_token("only.two", _SECRET, now=_NOW)
    assert exc_info.value.reason == "malformed_token"


def test_verify_rejects_empty_token() -> None:
    with pytest.raises(InvalidTokenError):
        verify_bearer_token("", _SECRET, now=_NOW)


def test_verify_rejects_token_with_missing_sub() -> None:
    header_segment = _b64url_encode(
        json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8")
    )
    payload_segment = _b64url_encode(json.dumps({"iat": 1}).encode("utf-8"))
    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature = hmac.new(_SECRET, signing_input, hashlib.sha256).digest()
    signature_segment = _b64url_encode(signature)
    token = f"{header_segment}.{payload_segment}.{signature_segment}"

    with pytest.raises(InvalidTokenError) as exc_info:
        verify_bearer_token(token, _SECRET, now=_NOW)
    assert exc_info.value.reason == "missing_subject"


def test_verify_rejects_token_with_empty_sub() -> None:
    token = create_bearer_token(_claims(sub=""), _SECRET)
    with pytest.raises(InvalidTokenError) as exc_info:
        verify_bearer_token(token, _SECRET, now=_NOW)
    assert exc_info.value.reason == "missing_subject"


def test_verify_respects_leeway() -> None:
    """A small leeway prevents borderline-expiry tokens from flapping."""
    token = create_bearer_token(_claims(exp_offset=-1), _SECRET)
    claims = verify_bearer_token(token, _SECRET, now=_NOW, leeway_seconds=5)
    assert claims["sub"] == _PARTY_ID


# ---------------------------------------------------------------------------
# RequestContextResolver — bearer-token path.
# ---------------------------------------------------------------------------


def test_resolver_builds_context_from_bearer_token(
    resolver: RequestContextResolver,
    fixed_clock: FixedClock,
    engine: Engine,
    identity_service: IdentityService,
    authorization_service: AuthorizationService,
    audit_log: AuditLog,
) -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:
        # We only return the identifying bits; everything else is asserted
        # via the closed-over fixtures below.
        return {"party_id": ctx.party_id, "correlation_id": ctx.correlation_id}

    token = create_bearer_token(_claims(jti="corr-from-token"), _SECRET)
    with TestClient(app) as client:
        response = client.get("/whoami", headers={AUTHORIZATION_HEADER: f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["party_id"] == _PARTY_ID
    assert body["correlation_id"] == "corr-from-token"


def test_resolver_mints_correlation_id_when_jti_absent(
    resolver: RequestContextResolver,
) -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:
        return {"correlation_id": ctx.correlation_id}

    token = create_bearer_token(_claims(), _SECRET)  # no jti
    with TestClient(app) as client:
        response = client.get(
            "/whoami", headers={AUTHORIZATION_HEADER: f"Bearer {token}"}
        )
    assert response.status_code == 200
    correlation_id = response.json()["correlation_id"]
    # UUIDv7 canonical form: 8-4-4-4-12 hex characters; version nibble = '7'.
    assert len(correlation_id) == 36
    assert correlation_id[14] == "7"


def test_resolver_returns_401_for_missing_authorization_header(
    resolver: RequestContextResolver,
) -> None:
    # Disable the fallback so the missing header is the only failure mode.
    resolver.allow_actor_header_fallback = False
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:  # pragma: no cover - never reached
        return {"party_id": ctx.party_id}

    with TestClient(app) as client:
        response = client.get("/whoami")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    body = response.json()["detail"]
    assert body["generic_denial_indicator"] == "unauthorized"
    assert body["reason_code"] == "missing_bearer_token"


def test_resolver_returns_401_for_invalid_signature(
    resolver: RequestContextResolver,
) -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:  # pragma: no cover - never reached
        return {"party_id": ctx.party_id}

    forged = create_bearer_token(_claims(), b"wrong-key")
    with TestClient(app) as client:
        response = client.get(
            "/whoami", headers={AUTHORIZATION_HEADER: f"Bearer {forged}"}
        )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"]["reason_code"] == "invalid_signature"


def test_resolver_returns_401_for_expired_token(
    resolver: RequestContextResolver,
) -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:  # pragma: no cover - never reached
        return {"party_id": ctx.party_id}

    token = create_bearer_token(_claims(exp_offset=-1), _SECRET)
    with TestClient(app) as client:
        response = client.get(
            "/whoami", headers={AUTHORIZATION_HEADER: f"Bearer {token}"}
        )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"]["reason_code"] == "token_expired"


def test_resolver_returns_401_for_non_bearer_scheme(
    resolver: RequestContextResolver,
) -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:  # pragma: no cover - never reached
        return {"party_id": ctx.party_id}

    with TestClient(app) as client:
        response = client.get(
            "/whoami", headers={AUTHORIZATION_HEADER: "Basic dXNlcjpwYXNz"}
        )
    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert response.json()["detail"]["reason_code"] == "malformed_authorization_header"


def test_resolver_accepts_lowercase_bearer_scheme(
    resolver: RequestContextResolver,
) -> None:
    """RFC 6750 §2.1: the scheme name is case-insensitive."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:
        return {"party_id": ctx.party_id}

    token = create_bearer_token(_claims(), _SECRET)
    with TestClient(app) as client:
        response = client.get(
            "/whoami", headers={AUTHORIZATION_HEADER: f"bearer {token}"}
        )
    assert response.status_code == 200
    assert response.json()["party_id"] == _PARTY_ID


# ---------------------------------------------------------------------------
# RequestContextResolver — X-Actor-Party-Id fallback (backward compat).
# ---------------------------------------------------------------------------


def test_resolver_falls_back_to_actor_header(
    resolver: RequestContextResolver,
) -> None:
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:
        return {"party_id": ctx.party_id}

    with TestClient(app) as client:
        response = client.get("/whoami", headers={ACTOR_HEADER: _PARTY_ID})
    assert response.status_code == 200
    assert response.json()["party_id"] == _PARTY_ID


def test_resolver_rejects_when_fallback_disabled_and_no_token(
    resolver: RequestContextResolver,
) -> None:
    resolver.allow_actor_header_fallback = False
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:  # pragma: no cover - never reached
        return {"party_id": ctx.party_id}

    with TestClient(app) as client:
        response = client.get("/whoami", headers={ACTOR_HEADER: _PARTY_ID})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_resolver_prefers_bearer_token_over_actor_header(
    resolver: RequestContextResolver,
) -> None:
    """If both are present, the bearer token wins so legacy clients can
    upgrade incrementally without changing their callers."""
    app = FastAPI()

    @app.get("/whoami")
    def whoami(ctx: RequestContext = _depend(resolver)) -> dict:
        return {"party_id": ctx.party_id}

    other_party = "00000000-0000-7000-8000-0000000000bb"
    token = create_bearer_token(_claims(sub=_PARTY_ID), _SECRET)
    with TestClient(app) as client:
        response = client.get(
            "/whoami",
            headers={
                AUTHORIZATION_HEADER: f"Bearer {token}",
                ACTOR_HEADER: other_party,
            },
        )
    assert response.status_code == 200
    # The token's ``sub`` wins.
    assert response.json()["party_id"] == _PARTY_ID


# ---------------------------------------------------------------------------
# RequestContext shape.
# ---------------------------------------------------------------------------


def test_request_context_bundles_design_fields(
    resolver: RequestContextResolver,
    fixed_clock: FixedClock,
    engine: Engine,
    identity_service: IdentityService,
    authorization_service: AuthorizationService,
    audit_log: AuditLog,
) -> None:
    """The bundle exposes every collaborator from design §"Application-Level Composition"."""
    ctx = RequestContext(
        party_id=_PARTY_ID,
        correlation_id="corr-abc",
        clock=fixed_clock,
        engine=engine,
        ids=identity_service,
        authz=authorization_service,
        audit=audit_log,
    )
    assert ctx.party_id == _PARTY_ID
    assert ctx.correlation_id == "corr-abc"
    assert ctx.clock is fixed_clock
    assert ctx.engine is engine
    assert ctx.ids is identity_service
    assert ctx.authz is authorization_service
    assert ctx.audit is audit_log


def test_request_context_is_frozen(
    fixed_clock: FixedClock,
    engine: Engine,
    identity_service: IdentityService,
    authorization_service: AuthorizationService,
    audit_log: AuditLog,
) -> None:
    """Frozen dataclass — mutating after construction would let a route
    leak state into another request."""
    ctx = RequestContext(
        party_id=_PARTY_ID,
        correlation_id="corr-abc",
        clock=fixed_clock,
        engine=engine,
        ids=identity_service,
        authz=authorization_service,
        audit=audit_log,
    )
    with pytest.raises(Exception):
        ctx.party_id = "evil"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _depend(resolver: RequestContextResolver):
    """Wrap ``resolver`` in :func:`fastapi.Depends` lazily so the helper
    is importable without pulling the dependency object into module scope."""
    from fastapi import Depends

    return Depends(resolver)
