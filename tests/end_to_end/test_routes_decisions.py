"""End-to-end HTTP tests for the Knowledge_Service Decisions routes (task 8.3).

These tests drive the :mod:`walking_slice.routes.decisions`
:class:`APIRouter` through :class:`httpx.AsyncClient` over the FastAPI
ASGI transport, exercising:

- ``POST /api/v1/recommendations/{rec_id}/decisions`` by an authorized
  Decision-Maker → 201 (Requirements 6.1, 6.2, 6.3, 6.4; AD-WS-5,
  AD-WS-10, AD-WS-11).
- Unauthorized caller → 403 with the AD-WS-9 indistinguishable denial
  shape carrying **only** ``generic_denial_indicator``,
  ``reason_code``, and ``correlation_id`` (Requirement 7.4).
- Missing required fields → 400 (Requirement 6.7).
- Invalid outcome → 400 (AD-WS-11).
- Duplicate Decision (same target Recommendation Revision) → 409 with
  ``existing_decision_id`` (Requirement 6.5).
- Unknown Recommendation Revision → 404 (Requirement 6.1).
- ``GET /api/v1/decisions/{decision_id}`` returns the persisted row
  → 200.
- 404 on unknown ``decision_id``.

These tests deliberately do not exercise the bearer-token
authentication middleware (task 15.1); the actor Party Identity
travels in the body's ``deciding_party_id`` field or in the temporary
``X-Actor-Party-Id`` header.
"""

from __future__ import annotations

import re
import uuid
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
from walking_slice.knowledge import (
    CreateRecommendationResult,
    KnowledgeService,
)
from walking_slice.persistence import create_schema
from walking_slice.routes.decisions import (
    get_engine,
    get_knowledge_service,
    router as decisions_router,
)


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Seed identifiers / constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-000000000002"
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-000000000003"
_SCOPE = "pilot/team-a"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
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


def _seed_recommendation(
    engine: Engine, knowledge_service: KnowledgeService
) -> CreateRecommendationResult:
    """Seed a hypothesis Finding and one Recommendation derived from it.

    Returns the Recommendation result so individual tests can target
    the freshly-created Recommendation Revision.
    """
    with engine.begin() as conn:
        finding = knowledge_service.create_finding(
            conn,
            statement="Source finding for decision routing tests.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend action X based on hypothesis Finding.",
            applicable_scope=_SCOPE,
        )
    return recommendation


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    role_name: str,
    authorities: tuple[str, ...],
    scope: str = _SCOPE,
) -> None:
    """Grant ``party_id`` a role with the supplied authorities for ``scope``."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=authorities,
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_PARTY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _assign_decision_maker_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str = _SCOPE,
) -> None:
    """Grant ``party_id`` a Decision-Maker role with ``approve`` authority.

    Per design §"Authorization_Service" the action ``approve.decision``
    requires the ``approve`` authority type; a Decision-Maker role
    grants ``approve`` for the configured scope.
    """
    _assign_role(
        authorization_service,
        engine,
        party_id=party_id,
        role_name="decision-maker",
        authorities=("view", "approve"),
        scope=scope,
    )


def _assign_seed_author_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str = _SCOPE,
) -> None:
    """Grant ``party_id`` a permissive seeding role.

    The ``decisions_app`` fixture wires the
    :class:`AuthorizationService` so Recommendation creation requires
    ``modify`` authority (Requirement 5.7). The Decision-routing tests
    that seed a target Recommendation Revision via the
    :class:`KnowledgeService` therefore need the seeding Party to hold
    ``modify`` — granting all three authorities here keeps the seed
    path independent of which Decision-Maker authority the test under
    examination cares about.
    """
    _assign_role(
        authorization_service,
        engine,
        party_id=party_id,
        role_name="seed-author",
        authorities=("view", "modify", "approve"),
        scope=scope,
    )


def _valid_body(revision_id: str) -> dict:
    """Build a valid request body targeting ``revision_id``."""
    return {
        "target_recommendation_revision_id": revision_id,
        "outcome": "Accept",
        "rationale": "Decision rationale anchored on the recommendation.",
        "deciding_party_id": _PARTY_ID,
        "authority_basis": {
            "type": "role-grant-id",
            "id": str(_AUTHORITY_BASIS_ID),
        },
        "applicable_scope": _SCOPE,
    }


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def decisions_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the decisions router with authorization wired.

    The test app *always* wires the :class:`AuthorizationService` so
    Requirement 7.1's authority check is enforced. Tests that exercise
    the authorized path explicitly grant a Decision-Maker role to
    ``_PARTY_ID``; tests that exercise the denial path leave the
    Party without a role assignment (or use ``_UNAUTHORIZED_PARTY_ID``)
    so the service denies with ``reason_code=no-role-assignment``.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, display="Decision Maker")
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
    app.include_router(decisions_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service

    # Pre-grant the seeding Party a permissive role so the
    # :func:`_seed_recommendation` helper can create a Recommendation
    # Revision against the authorization-wired KnowledgeService.
    # Requirement 5.7 requires ``modify`` authority for Recommendation
    # creation; granting view+modify+approve to ``_PARTY_ID`` keeps
    # the seeding independent of which authority the Decision under
    # test cares about. Tests that target the unauthorized denial path
    # use ``_UNAUTHORIZED_PARTY_ID`` instead, which carries no role
    # assignment.
    _assign_seed_author_role(
        authorization_service, engine, party_id=_PARTY_ID, scope=_SCOPE
    )

    app.state.engine = engine
    app.state.clock = clock
    app.state.knowledge_service = knowledge_service
    app.state.authorization_service = authorization_service
    return app


@pytest_asyncio.fixture
async def client(decisions_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=decisions_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# POST .../decisions — happy path (authorized).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_decision_by_authorized_party_returns_201(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """A valid Decision creation by an authorized Decision-Maker yields
    201 and persists the Decision, the Addresses Relationship, the
    Provenance Manifest, and a consequential audit row (AD-WS-5)."""
    engine: Engine = decisions_app.state.engine
    knowledge_service: KnowledgeService = decisions_app.state.knowledge_service
    authorization_service: AuthorizationService = (
        decisions_app.state.authorization_service
    )
    _assign_decision_maker_role(
        authorization_service, engine, party_id=_PARTY_ID, scope=_SCOPE
    )
    recommendation = _seed_recommendation(engine, knowledge_service)

    response = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json=_valid_body(recommendation.recommendation_revision_id),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["decision_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["addresses_relationship_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["manifest_id"]), body
    assert body["target_recommendation_id"] == recommendation.recommendation_id
    assert body["target_recommendation_revision_id"] == (
        recommendation.recommendation_revision_id
    )
    assert body["outcome"] == "Accept"
    assert body["rationale"] == (
        "Decision rationale anchored on the recommendation."
    )
    assert body["deciding_party_id"] == _PARTY_ID
    assert body["authority_basis"]["type"] == "role-grant-id"
    assert body["authority_basis"]["id"] == str(_AUTHORITY_BASIS_ID)
    assert body["applicable_scope"] == _SCOPE
    assert body["omission_entry_ids"] == []
    assert body["recorded_at"].endswith("Z")

    # Persistence checks: the Decision row, the Addresses Relationship,
    # the Provenance Manifest, and the consequential audit row all
    # committed together.
    with engine.connect() as conn:
        decision_row = (
            conn.execute(
                text(
                    "SELECT outcome, rationale, deciding_party_id, "
                    "authority_basis_type, applicable_scope FROM Decisions "
                    "WHERE decision_id = :did"
                ),
                {"did": body["decision_id"]},
            )
            .mappings()
            .one_or_none()
        )
        addresses_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Addresses' "
                "AND source_id = :sid AND target_id = :tid "
                "AND target_revision_id = :rev"
            ),
            {
                "sid": body["decision_id"],
                "tid": recommendation.recommendation_id,
                "rev": recommendation.recommendation_revision_id,
            },
        ).scalar_one()
        manifest_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Provenance_Manifests "
                "WHERE subject_id = :sid AND subject_kind = 'decision'"
            ),
            {"sid": body["decision_id"]},
        ).scalar_one()
        audit_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Audit_Records "
                "WHERE action_type = 'create.decision' AND target_id = :tid"
            ),
            {"tid": body["decision_id"]},
        ).scalar_one()

    assert decision_row is not None
    assert decision_row["outcome"] == "Accept"
    assert decision_row["deciding_party_id"] == _PARTY_ID
    assert decision_row["authority_basis_type"] == "role-grant-id"
    assert decision_row["applicable_scope"] == _SCOPE
    assert addresses_count == 1
    assert manifest_count == 1
    assert audit_count == 1


# ---------------------------------------------------------------------------
# POST .../decisions — unauthorized denial (Requirement 7.1 / AD-WS-9).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_decision_without_authority_returns_ad_ws_9_denial(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """A caller without effective Decision-Maker authority yields 403
    with the AD-WS-9 indistinguishable denial shape (Requirement 7.4).

    The response body MUST carry exactly three fields:
    ``generic_denial_indicator``, ``reason_code``, ``correlation_id``.
    No information about the target, the Recommendation, the role
    assignment, or the existence of the Decision is exposed.
    """
    engine: Engine = decisions_app.state.engine
    knowledge_service: KnowledgeService = decisions_app.state.knowledge_service
    recommendation = _seed_recommendation(engine, knowledge_service)

    response = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json={
            **_valid_body(recommendation.recommendation_revision_id),
            "deciding_party_id": _UNAUTHORIZED_PARTY_ID,
        },
    )

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    # AD-WS-9 indistinguishable shape — exactly three fields, no extras.
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"
    assert detail["reason_code"] == "no-role-assignment"
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # The caller's transaction rolled back: no Decision, no Addresses
    # Relationship, no Provenance Manifest, and no consequential audit
    # row was persisted.
    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
        addresses_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Addresses'"
            )
        ).scalar_one()
        manifest_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Provenance_Manifests "
                "WHERE subject_kind = 'decision'"
            )
        ).scalar_one()
        # The Denial Record was appended in a SEPARATE transaction so
        # it survives the caller's rollback (Requirement 7.6).
        denial_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Audit_Records "
                "WHERE outcome = 'deny' AND actor_party_id = :pid"
            ),
            {"pid": _UNAUTHORIZED_PARTY_ID},
        ).scalar_one()
    assert decision_count == 0
    assert addresses_count == 0
    assert manifest_count == 0
    assert denial_count >= 1


# ---------------------------------------------------------------------------
# POST .../decisions — missing required fields (Requirement 6.7).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_decision_with_missing_required_fields_returns_400(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """Missing required fields are rejected by the Pydantic boundary
    with a 400 naming the missing fields (Requirement 6.7)."""
    engine: Engine = decisions_app.state.engine
    knowledge_service: KnowledgeService = decisions_app.state.knowledge_service
    recommendation = _seed_recommendation(engine, knowledge_service)

    response = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json={
            # Missing: outcome, rationale, deciding_party_id,
            # authority_basis, applicable_scope.
            "target_recommendation_revision_id": (
                recommendation.recommendation_revision_id
            ),
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_decision_request"
    missing = set(detail["missing"])
    # The Pydantic missing-field set must include every required
    # attribute Requirement 6.7 names.
    assert "outcome" in missing
    assert "rationale" in missing
    assert "deciding_party_id" in missing
    assert "authority_basis" in missing
    assert "applicable_scope" in missing

    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
    assert decision_count == 0


# ---------------------------------------------------------------------------
# POST .../decisions — invalid outcome (AD-WS-11).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_decision_with_invalid_outcome_returns_400(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """An outcome outside ``{Accept, Reject, Defer}`` is rejected
    (AD-WS-11). The Pydantic Literal type catches it at the boundary."""
    engine: Engine = decisions_app.state.engine
    knowledge_service: KnowledgeService = decisions_app.state.knowledge_service
    recommendation = _seed_recommendation(engine, knowledge_service)

    response = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json={
            **_valid_body(recommendation.recommendation_revision_id),
            "outcome": "Supersede",
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_decision_request"

    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
    assert decision_count == 0


# ---------------------------------------------------------------------------
# POST .../decisions — duplicate Decision (Requirement 6.5).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_decision_duplicate_target_returns_409(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """A second Decision targeting the same Recommendation Revision is
    rejected with 409 and the response carries ``existing_decision_id``
    (Requirement 6.5)."""
    engine: Engine = decisions_app.state.engine
    knowledge_service: KnowledgeService = decisions_app.state.knowledge_service
    authorization_service: AuthorizationService = (
        decisions_app.state.authorization_service
    )
    _assign_decision_maker_role(
        authorization_service, engine, party_id=_PARTY_ID, scope=_SCOPE
    )
    recommendation = _seed_recommendation(engine, knowledge_service)

    first = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json=_valid_body(recommendation.recommendation_revision_id),
    )
    assert first.status_code == 201, first.text
    existing_decision_id = first.json()["decision_id"]

    # Second submission targeting the same Recommendation Revision.
    response = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json={
            **_valid_body(recommendation.recommendation_revision_id),
            "rationale": "A second attempt against the same revision.",
            "outcome": "Reject",
        },
    )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "duplicate_decision"
    assert detail["failed_constraint"] == "duplicate_decision"
    assert detail["existing_decision_id"] == existing_decision_id
    assert detail["target_recommendation_id"] == (
        recommendation.recommendation_id
    )
    assert detail["target_recommendation_revision_id"] == (
        recommendation.recommendation_revision_id
    )

    # Only the first Decision row exists.
    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
    assert decision_count == 1


# ---------------------------------------------------------------------------
# POST .../decisions — unknown Recommendation Revision.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_decision_with_unknown_revision_returns_404(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """An unresolvable ``(rec_id, target_recommendation_revision_id)``
    pair yields 404 carrying both identifiers (Requirement 6.1)."""
    engine: Engine = decisions_app.state.engine
    authorization_service: AuthorizationService = (
        decisions_app.state.authorization_service
    )
    _assign_decision_maker_role(
        authorization_service, engine, party_id=_PARTY_ID, scope=_SCOPE
    )

    response = await client.post(
        f"/api/v1/recommendations/{_UNKNOWN_UUID7}/decisions",
        json=_valid_body(_UNKNOWN_UUID7),
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "recommendation_revision_not_found"
    assert detail["target_recommendation_id"] == _UNKNOWN_UUID7
    assert detail["target_recommendation_revision_id"] == _UNKNOWN_UUID7

    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
    assert decision_count == 0


# ---------------------------------------------------------------------------
# GET /api/v1/decisions/{decision_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_decision_returns_persisted_row(
    client: AsyncClient, decisions_app: FastAPI
) -> None:
    """The GET endpoint returns every persisted column for the Decision."""
    engine: Engine = decisions_app.state.engine
    knowledge_service: KnowledgeService = decisions_app.state.knowledge_service
    authorization_service: AuthorizationService = (
        decisions_app.state.authorization_service
    )
    _assign_decision_maker_role(
        authorization_service, engine, party_id=_PARTY_ID, scope=_SCOPE
    )
    recommendation = _seed_recommendation(engine, knowledge_service)

    create_response = await client.post(
        f"/api/v1/recommendations/{recommendation.recommendation_id}/decisions",
        json=_valid_body(recommendation.recommendation_revision_id),
    )
    assert create_response.status_code == 201, create_response.text
    created = create_response.json()

    response = await client.get(f"/api/v1/decisions/{created['decision_id']}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["decision_id"] == created["decision_id"]
    assert body["target_recommendation_id"] == recommendation.recommendation_id
    assert body["target_recommendation_revision_id"] == (
        recommendation.recommendation_revision_id
    )
    assert body["outcome"] == "Accept"
    assert body["rationale"] == (
        "Decision rationale anchored on the recommendation."
    )
    assert body["deciding_party_id"] == _PARTY_ID
    assert body["authority_basis"]["type"] == "role-grant-id"
    assert body["authority_basis"]["id"] == str(_AUTHORITY_BASIS_ID)
    assert body["applicable_scope"] == _SCOPE
    assert body["recorded_at"] == created["recorded_at"]


@pytest.mark.asyncio
async def test_get_unknown_decision_returns_404(client: AsyncClient) -> None:
    response = await client.get(f"/api/v1/decisions/{_UNKNOWN_UUID7}")

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "decision_not_found"
    assert detail["decision_id"] == _UNKNOWN_UUID7
