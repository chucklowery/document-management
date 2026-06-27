"""End-to-end HTTP tests for the Knowledge_Service Recommendations routes (task 7.2).

These tests drive the :mod:`walking_slice.routes.recommendations`
:class:`APIRouter` through :class:`httpx.AsyncClient` over the FastAPI
ASGI transport, exercising:

- ``POST /api/v1/recommendations`` with a valid Derived From list → 201
  (Requirement 5.1).
- ``POST /api/v1/recommendations`` with missing ``derived_from_findings``
  rejected → 400 (Requirement 5.6).
- ``POST /api/v1/recommendations`` with an empty rationale rejected
  (Requirement 5.3).
- ``POST /api/v1/recommendations`` with an invalid confidence rejected
  (Requirement 5.5).
- ``POST /api/v1/recommendations`` unauthorized (no Analyst role) → 403
  with the AD-WS-9 indistinguishable denial shape (Requirement 5.7).
- ``GET /api/v1/recommendations/{rid}/revisions/{rev}`` returns the
  persisted row → 200.
- 404 on unknown identifier.

These tests deliberately do not exercise the bearer-token authentication
middleware (task 15.1); the actor Party Identity travels in the body's
``authoring_party_id`` field or in the temporary ``X-Actor-Party-Id``
header.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService
from walking_slice.persistence import create_schema
from walking_slice.routes.recommendations import (
    get_engine,
    get_knowledge_service,
    router as recommendations_router,
)


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Seed identifiers / constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-000000000002"
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-000000000003"
_SCOPE = "pilot/team-a"
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UNKNOWN_UUID7 = "00000000-0000-7000-8000-deadbeefcafe"


# ---------------------------------------------------------------------------
# Engine + seed helpers.
# ---------------------------------------------------------------------------


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


def _seed_hypothesis_finding(
    engine: Engine, knowledge_service: KnowledgeService, *, statement: str
) -> str:
    """Create a hypothesis Finding and return its ``finding_id``.

    Hypothesis Findings need no Region Occurrences (Requirement 4.1),
    which keeps the Recommendation tests independent of the
    Evidence_Repository routes.
    """
    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement=statement,
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
    return result.finding_id


def _assign_analyst_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str = _SCOPE,
) -> None:
    """Grant ``party_id`` an Analyst role with ``modify`` authority.

    Per design §"Authorization_Service" the action ``create.recommendation``
    requires the ``modify`` authority type; an Analyst role grants
    ``modify`` for the configured scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="analyst",
        scope=scope,
        authorities_granted=("view", "modify"),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_PARTY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def recommendations_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the recommendations router with overridden DI.

    The fixture wires a back-compat :class:`KnowledgeService` (no
    authorization service) so Requirement 5.7's authority check is not
    enforced. The dedicated authorization fixture
    :func:`recommendations_app_authorized` covers the enforced-path
    tests.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, display="Analyst")
        _seed_party(conn, _ASSIGNING_PARTY_ID, display="Resource Steward")

    clock = FixedClock(_FIXED_INSTANT)
    audit_log = AuditLog(clock)
    identity_service = IdentityService()
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )

    app = FastAPI()
    app.include_router(recommendations_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service

    app.state.engine = engine
    app.state.clock = clock
    app.state.knowledge_service = knowledge_service
    return app


@pytest.fixture
def recommendations_app_authorized(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the recommendations router with authorization
    enforced.

    Wires a :class:`KnowledgeService` whose ``authorization_service`` is
    a real :class:`AuthorizationService`. Parties seeded into the
    fixture have no role assignment until the test explicitly grants
    one — so the default state denies every Recommendation creation
    with ``reason_code=no-role-assignment``.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, display="Analyst")
        _seed_party(conn, _ASSIGNING_PARTY_ID, display="Resource Steward")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, display="Unauthorized")

    clock = FixedClock(_FIXED_INSTANT)
    audit_log = AuditLog(clock)
    identity_service = IdentityService()
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )

    app = FastAPI()
    app.include_router(recommendations_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service

    app.state.engine = engine
    app.state.clock = clock
    app.state.knowledge_service = knowledge_service
    app.state.authorization_service = authorization_service
    return app


@pytest_asyncio.fixture
async def client(recommendations_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=recommendations_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest_asyncio.fixture
async def authorized_client(
    recommendations_app_authorized: FastAPI,
) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=recommendations_app_authorized)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# POST /api/v1/recommendations — happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_recommendation_with_valid_derived_from_returns_201(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """A valid Recommendation inserts the header, Revision, and one
    Derived From Relationship per cited Finding (Requirement 5.1)."""
    engine: Engine = recommendations_app.state.engine
    knowledge_service: KnowledgeService = recommendations_app.state.knowledge_service
    finding_a = _seed_hypothesis_finding(
        engine, knowledge_service, statement="hypothesis a"
    )
    finding_b = _seed_hypothesis_finding(
        engine, knowledge_service, statement="hypothesis b"
    )

    response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [finding_a, finding_b],
            "rationale": "Two pieces of analysis justify this proposed action.",
            "assumptions": ["Assumption one.", "Assumption two."],
            "confidence": "Medium",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["recommendation_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["recommendation_revision_id"]), body
    assert body["rationale"] == (
        "Two pieces of analysis justify this proposed action."
    )
    assert body["assumptions"] == ["Assumption one.", "Assumption two."]
    assert body["confidence"] == "Medium"
    assert len(body["derived_from_relationship_ids"]) == 2
    for rid in body["derived_from_relationship_ids"]:
        assert _CANONICAL_UUID7_REGEX.match(rid), rid
    assert body["recorded_at"].endswith("Z")

    # Verify the two Derived From rows landed and point at the expected
    # Findings.
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_type, target_id, target_revision_id
                    FROM Relationships
                    WHERE source_id = :rid
                    ORDER BY recorded_at, relationship_id
                    """
                ),
                {"rid": body["recommendation_id"]},
            )
            .mappings()
            .all()
        )

    assert [r["relationship_type"] for r in rows] == ["Derived From", "Derived From"]
    assert {r["target_id"] for r in rows} == {finding_a, finding_b}
    # Derived From keys on Finding Identity only (target_revision_id is NULL).
    assert all(r["target_revision_id"] is None for r in rows)


# ---------------------------------------------------------------------------
# POST /api/v1/recommendations — rejection cases.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_recommendation_with_missing_derived_from_returns_400(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """Missing ``derived_from_findings`` is rejected by the Pydantic
    boundary (Requirement 5.1/5.6); the response names the missing field."""
    response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_recommendation_request"
    assert "derived_from_findings" in detail["missing"]

    engine: Engine = recommendations_app.state.engine
    with engine.connect() as conn:
        rec_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendations")
        ).scalar_one()
        rev_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendation_Revisions")
        ).scalar_one()
    assert rec_count == 0
    assert rev_count == 0


@pytest.mark.asyncio
async def test_create_recommendation_with_empty_derived_from_returns_400(
    client: AsyncClient,
) -> None:
    """An empty ``derived_from_findings`` list is rejected by the
    Pydantic ``min_length=1`` constraint (Requirement 5.1/5.6)."""
    response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [],
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_recommendation_request"


@pytest.mark.asyncio
async def test_create_recommendation_with_empty_rationale_returns_400(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """An explicit empty-string rationale violates Requirement 5.3.

    Pydantic's ``min_length=1`` on the rationale field rejects the
    empty string at the boundary; the resulting error envelope carries
    the ``invalid_recommendation_request`` code.
    """
    engine: Engine = recommendations_app.state.engine
    knowledge_service: KnowledgeService = recommendations_app.state.knowledge_service
    finding = _seed_hypothesis_finding(
        engine, knowledge_service, statement="anchor finding"
    )

    response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [finding],
            "rationale": "",
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_recommendation_request"

    with engine.connect() as conn:
        rec_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendations")
        ).scalar_one()
    assert rec_count == 0


@pytest.mark.asyncio
async def test_create_recommendation_with_invalid_confidence_returns_400(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """A confidence value outside {Low, Medium, High} is rejected
    (Requirement 5.5). The Pydantic Literal type catches it at the
    boundary; the resulting error envelope carries the
    ``invalid_recommendation_request`` code.
    """
    engine: Engine = recommendations_app.state.engine
    knowledge_service: KnowledgeService = recommendations_app.state.knowledge_service
    finding = _seed_hypothesis_finding(
        engine, knowledge_service, statement="anchor finding"
    )

    response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [finding],
            "confidence": "Maybe",
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_recommendation_request"

    with engine.connect() as conn:
        rec_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendations")
        ).scalar_one()
    assert rec_count == 0


@pytest.mark.asyncio
async def test_create_recommendation_with_unresolvable_finding_returns_400(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """A Derived From reference to a non-existent Finding yields 400 with
    the ``finding_id`` field populated (Requirement 5.6)."""
    response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [_UNKNOWN_UUID7],
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_derived_from"
    assert detail["failed_constraint"] == "invalid_derived_from"
    assert detail["finding_id"] == _UNKNOWN_UUID7

    engine: Engine = recommendations_app.state.engine
    with engine.connect() as conn:
        rec_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendations")
        ).scalar_one()
        rel_count = conn.execute(
            text("SELECT COUNT(*) FROM Relationships")
        ).scalar_one()
    assert rec_count == 0
    assert rel_count == 0


# ---------------------------------------------------------------------------
# POST /api/v1/recommendations — authorization denial (Requirement 5.7).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_recommendation_without_analyst_role_returns_403(
    authorized_client: AsyncClient, recommendations_app_authorized: FastAPI
) -> None:
    """A caller without an Analyst role for the applicable scope yields 403
    with the AD-WS-9 indistinguishable denial shape (Requirement 5.7)."""
    engine: Engine = recommendations_app_authorized.state.engine
    knowledge_service: KnowledgeService = (
        recommendations_app_authorized.state.knowledge_service
    )
    # Seed a Finding using the service's back-compat path on the
    # authorization-wired service. The Finding write itself goes
    # through create_finding which has no authorization gate, so this
    # succeeds even without role assignment.
    finding = _seed_hypothesis_finding(
        engine, knowledge_service, statement="anchor finding"
    )

    # The unauthorized Party has no role assignment at all → the
    # authorization service returns ``deny`` with
    # ``reason_code=no-role-assignment``.
    response = await authorized_client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _UNAUTHORIZED_PARTY_ID,
            "derived_from_findings": [finding],
            "applicable_scope": _SCOPE,
        },
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    # AD-WS-9 indistinguishable denial shape — only the generic
    # indicator, reason code, and correlation id are exposed.
    assert detail["error"] == "authorization_denied"
    assert detail["generic_denial_indicator"] == "denied"
    assert detail["reason_code"] == "no-role-assignment"
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # No Recommendation row was inserted; the surrounding transaction
    # was rolled back and carried only the authorization evaluation
    # audit row.
    with engine.connect() as conn:
        rec_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendations")
        ).scalar_one()
        rev_count = conn.execute(
            text("SELECT COUNT(*) FROM Recommendation_Revisions")
        ).scalar_one()
    assert rec_count == 0
    assert rev_count == 0


@pytest.mark.asyncio
async def test_create_recommendation_with_analyst_role_returns_201(
    authorized_client: AsyncClient, recommendations_app_authorized: FastAPI
) -> None:
    """Sanity check: granting an Analyst role permits Recommendation creation
    on the authorization-wired path."""
    engine: Engine = recommendations_app_authorized.state.engine
    knowledge_service: KnowledgeService = (
        recommendations_app_authorized.state.knowledge_service
    )
    authorization_service: AuthorizationService = (
        recommendations_app_authorized.state.authorization_service
    )

    _assign_analyst_role(
        authorization_service, engine, party_id=_PARTY_ID, scope=_SCOPE
    )
    finding = _seed_hypothesis_finding(
        engine, knowledge_service, statement="anchor finding"
    )

    response = await authorized_client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [finding],
            "applicable_scope": _SCOPE,
            "rationale": "Sanity check for the happy path.",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["recommendation_id"]), body


# ---------------------------------------------------------------------------
# GET /api/v1/recommendations/{rec_id}/revisions/{revision_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recommendation_revision_returns_persisted_row(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """The GET endpoint returns every persisted column."""
    engine: Engine = recommendations_app.state.engine
    knowledge_service: KnowledgeService = recommendations_app.state.knowledge_service
    finding = _seed_hypothesis_finding(
        engine, knowledge_service, statement="anchor finding"
    )

    create_response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [finding],
            "rationale": "Persisted rationale text.",
            "assumptions": ["Assumption one."],
            "confidence": "High",
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()

    response = await client.get(
        f"/api/v1/recommendations/{created['recommendation_id']}/revisions/"
        f"{created['recommendation_revision_id']}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recommendation_id"] == created["recommendation_id"]
    assert body["recommendation_revision_id"] == created["recommendation_revision_id"]
    assert body["parent_revision_id"] is None
    assert body["rationale"] == "Persisted rationale text."
    assert body["assumptions"] == ["Assumption one."]
    assert body["confidence"] == "High"
    assert body["authoring_party_id"] == _PARTY_ID
    assert body["recorded_at"] == created["recorded_at"]


@pytest.mark.asyncio
async def test_get_unknown_recommendation_revision_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.get(
        f"/api/v1/recommendations/{_UNKNOWN_UUID7}/revisions/{_UNKNOWN_UUID7}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "recommendation_revision_not_found"
    assert detail["recommendation_id"] == _UNKNOWN_UUID7
    assert detail["recommendation_revision_id"] == _UNKNOWN_UUID7


@pytest.mark.asyncio
async def test_get_recommendation_revision_with_mismatched_id_returns_404(
    client: AsyncClient, recommendations_app: FastAPI
) -> None:
    """A Revision Identity belonging to a different Recommendation yields 404.

    The endpoint matches on the composite ``(recommendation_id,
    recommendation_revision_id)`` so a caller cannot get a Revision by
    accident through some other Recommendation's identifier.
    """
    engine: Engine = recommendations_app.state.engine
    knowledge_service: KnowledgeService = recommendations_app.state.knowledge_service
    finding = _seed_hypothesis_finding(
        engine, knowledge_service, statement="anchor finding"
    )

    create_response = await client.post(
        "/api/v1/recommendations",
        json={
            "authoring_party_id": _PARTY_ID,
            "derived_from_findings": [finding],
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()

    response = await client.get(
        f"/api/v1/recommendations/{_UNKNOWN_UUID7}/revisions/"
        f"{created['recommendation_revision_id']}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "recommendation_revision_not_found"
