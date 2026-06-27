"""End-to-end HTTP tests for the role assignment routes (task 3.3).

These tests drive the :mod:`walking_slice.routes.roles` :class:`APIRouter`
through :class:`httpx.AsyncClient` over the FastAPI ASGI transport so they
exercise the full request → response cycle, including:

- Pydantic v2 validation of the Requirement 12.6 fields (party_id,
  role_name, scope, authorities_granted, effective_start).
- Conversion of validation failures into structured ``HTTP 400`` bodies
  rather than the FastAPI-default 422.
- The persistent path through
  :class:`walking_slice.authorization.AuthorizationService.assign_role`,
  including the ``Role_Assignments`` insert and the ``Audit_Records``
  consequential append (Requirement 13.1, AD-WS-5).
- The revocation endpoint's one-shot semantics — 200 on first revocation,
  409 on retry (idempotent retry never re-revokes), and 404 when the
  identifier is unknown.

The tests deliberately do not exercise the bearer-token authentication
middleware (task 15.1); the actor Party Identity is supplied via the
temporary ``X-Actor-Party-Id`` header per the task description.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AuthorizationService
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.routes.roles import (
    get_audit_log,
    get_authorization_service,
    get_clock,
    get_engine,
    router as roles_router,
)


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Seed identifiers.
#
# Two Parties are seeded: the role recipient and the Resource Steward acting
# as ``assigning_authority_id``. Both are canonical UUIDv7 strings so the
# ``Audit_Records.actor_party_id`` FK resolves cleanly.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_STEWARD_ID = "00000000-0000-7000-8000-000000000002"
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _seed_party(conn, party_id: str, display: str = "Test Party") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": "2026-01-01T00:00:00.000Z"},
    )


def _build_engine(tmp_path: Path) -> Engine:
    """Construct a per-test SQLite engine wired with WAL + FK pragmas."""
    sqlite_path = tmp_path / "walking_slice.sqlite"
    url = f"sqlite:///{sqlite_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    return engine


@pytest.fixture
def role_assignment_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the roles router with overridden dependencies.

    Constructs a fresh per-test SQLite database, seeds the two Parties
    needed for FK resolution, and wires the route module's placeholder
    DI factories to real instances of every collaborator. The engine
    handle is attached to ``app.state`` so individual tests can inspect
    the persisted state directly.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Subject")
        _seed_party(conn, _STEWARD_ID, "Resource Steward")

    clock = FixedClock(_FIXED_INSTANT)
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=IdentityService(),
    )

    app = FastAPI()
    app.include_router(roles_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_authorization_service] = lambda: authorization_service
    app.dependency_overrides[get_audit_log] = lambda: audit_log
    app.dependency_overrides[get_clock] = lambda: clock

    app.state.engine = engine
    app.state.clock = clock
    return app


@pytest_asyncio.fixture
async def client(role_assignment_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """``httpx.AsyncClient`` bound to the FastAPI app via the ASGI transport."""
    transport = ASGITransport(app=role_assignment_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _valid_payload(**overrides: Any) -> dict[str, Any]:
    """Build a body that satisfies every Requirement 12.6 field by default."""
    payload: dict[str, Any] = {
        "party_id": _PARTY_ID,
        "role_name": "decision_maker",
        "scope": "pilot/team-a",
        "authorities_granted": ["view", "approve"],
        "effective_start": "2026-01-01T00:00:00Z",
    }
    payload.update(overrides)
    return payload


def _steward_headers() -> dict[str, str]:
    return {"X-Actor-Party-Id": _STEWARD_ID}


async def _create_assignment(client: AsyncClient, **overrides: Any) -> str:
    response = await client.post(
        "/api/v1/roles/assignments",
        json=_valid_payload(**overrides),
        headers=_steward_headers(),
    )
    assert response.status_code == 201, response.text
    return response.json()["role_assignment_id"]


# ---------------------------------------------------------------------------
# POST /api/v1/roles/assignments — success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_assignment_returns_201_with_role_assignment_id(
    client: AsyncClient, role_assignment_app: FastAPI
) -> None:
    """Valid submission returns 201 with a canonical UUIDv7 identifier."""
    response = await client.post(
        "/api/v1/roles/assignments",
        json=_valid_payload(),
        headers=_steward_headers(),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body.keys()) == {"role_assignment_id"}
    assert _CANONICAL_UUID7_REGEX.match(body["role_assignment_id"]), body

    # Verify the row landed in Role_Assignments and that an audit row
    # exists for the consequential write (Requirement 13.1, AD-WS-5).
    engine: Engine = role_assignment_app.state.engine
    with engine.connect() as conn:
        role_row = (
            conn.execute(
                text(
                    "SELECT party_id, role_name, scope, authorities_granted, "
                    "effective_start, effective_end, revoked_at, "
                    "assigning_authority_id FROM Role_Assignments "
                    "WHERE role_assignment_id = :rid"
                ),
                {"rid": body["role_assignment_id"]},
            )
            .mappings()
            .one()
        )
        audit_row = (
            conn.execute(
                text(
                    "SELECT action_type, outcome, actor_party_id, target_id "
                    "FROM Audit_Records WHERE target_id = :rid"
                ),
                {"rid": body["role_assignment_id"]},
            )
            .mappings()
            .one()
        )

    assert role_row["party_id"] == _PARTY_ID
    assert role_row["role_name"] == "decision_maker"
    assert role_row["scope"] == "pilot/team-a"
    assert role_row["revoked_at"] is None
    assert role_row["assigning_authority_id"] == _STEWARD_ID
    assert audit_row["action_type"] == "assign.role"
    assert audit_row["outcome"] == "consequential"
    assert audit_row["actor_party_id"] == _STEWARD_ID


# ---------------------------------------------------------------------------
# POST /api/v1/roles/assignments — Requirement 12.6 validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_field",
    ["party_id", "role_name", "scope", "authorities_granted", "effective_start"],
)
async def test_missing_required_field_returns_400_with_structured_error(
    client: AsyncClient, missing_field: str
) -> None:
    """Submissions missing any Requirement 12.6 field yield 400 with names."""
    payload = _valid_payload()
    payload.pop(missing_field)

    response = await client.post(
        "/api/v1/roles/assignments",
        json=payload,
        headers=_steward_headers(),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_role_assignment"
    assert missing_field in detail["missing"], detail


@pytest.mark.asyncio
async def test_authority_outside_known_set_returns_400(client: AsyncClient) -> None:
    """``authorities_granted`` outside the {view, modify, approve} set fails."""
    response = await client.post(
        "/api/v1/roles/assignments",
        json=_valid_payload(authorities_granted=["view", "elevate"]),
        headers=_steward_headers(),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_role_assignment"


@pytest.mark.asyncio
async def test_missing_actor_party_id_returns_400(client: AsyncClient) -> None:
    """When neither header nor body carries the actor, the request fails."""
    response = await client.post(
        "/api/v1/roles/assignments",
        json=_valid_payload(),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "actor_party_id_required"


# ---------------------------------------------------------------------------
# POST /api/v1/roles/assignments/{id}/revocations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_role_assignment_returns_200_and_sets_revoked_at(
    client: AsyncClient, role_assignment_app: FastAPI
) -> None:
    """Successful revocation returns 200 and persists ``revoked_at``."""
    role_assignment_id = await _create_assignment(client)

    response = await client.post(
        f"/api/v1/roles/assignments/{role_assignment_id}/revocations",
        headers=_steward_headers(),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["role_assignment_id"] == role_assignment_id
    assert body["revoked_at"].endswith("Z")

    engine: Engine = role_assignment_app.state.engine
    with engine.connect() as conn:
        revoked_at = conn.execute(
            text("SELECT revoked_at FROM Role_Assignments WHERE role_assignment_id = :r"),
            {"r": role_assignment_id},
        ).scalar_one()
        audit_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Audit_Records "
                "WHERE action_type = 'revoke.role' AND target_id = :r"
            ),
            {"r": role_assignment_id},
        ).scalar_one()

    assert revoked_at is not None
    assert revoked_at == body["revoked_at"]
    assert audit_count == 1


@pytest.mark.asyncio
async def test_revoking_already_revoked_assignment_returns_409(
    client: AsyncClient,
) -> None:
    """A second revocation on the same assignment is idempotent → 409."""
    role_assignment_id = await _create_assignment(client)

    first = await client.post(
        f"/api/v1/roles/assignments/{role_assignment_id}/revocations",
        headers=_steward_headers(),
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/roles/assignments/{role_assignment_id}/revocations",
        headers=_steward_headers(),
    )
    assert second.status_code == 409, second.text
    detail = second.json()["detail"]
    assert detail["error"] == "role_assignment_already_revoked"
    assert detail["role_assignment_id"] == role_assignment_id


@pytest.mark.asyncio
async def test_revoking_unknown_assignment_returns_404(client: AsyncClient) -> None:
    """An unknown ``role_assignment_id`` yields 404 with a structured body."""
    unknown_id = "00000000-0000-7000-8000-deadbeefcafe"

    response = await client.post(
        f"/api/v1/roles/assignments/{unknown_id}/revocations",
        headers=_steward_headers(),
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "role_assignment_not_found"
    assert detail["role_assignment_id"] == unknown_id
