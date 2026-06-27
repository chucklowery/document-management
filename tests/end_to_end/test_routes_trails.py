"""End-to-end HTTP tests for the Trail_Service routes (task 10.3).

These tests drive the :mod:`walking_slice.routes.trails`
:class:`APIRouter` through :class:`httpx.AsyncClient` over the FastAPI
ASGI transport, exercising:

- ``POST /api/v1/trails`` with a valid five-step submission → 201
  (Requirement 9.1).
- ``POST /api/v1/trails`` missing required fields → 400 with the
  Pydantic validation envelope (Requirement 9.6).
- ``POST /api/v1/trails`` with a non-Pinned ``selection_mode`` → 400
  (AD-WS-12).
- ``POST /api/v1/trails`` with an unresolvable step target → 400 with
  the per-ordinal ``unresolved_steps`` list (Requirement 9.5).
- ``POST /api/v1/trails`` denied by the wired AuthorizationService
  → 403 with the AD-WS-9 indistinguishable denial shape
  (Requirement 7.4).
- ``POST /api/v1/trails/{trail_id}/revisions`` with a material change
  → 201 with ``created_new_revision=true`` and a populated
  ``predecessor_revision_id`` (Requirement 9.4).
- ``POST /api/v1/trails/{trail_id}/revisions`` against an unknown
  ``trail_id`` → 404 (Requirement 9.4).
- ``GET /api/v1/trails/{trail_id}/revisions/{revision_id}`` returns
  the persisted Trail Revision and its five ordered Trail Steps → 200.
- ``GET`` against an unknown ``revision_id`` → 404.

These tests deliberately do not exercise the bearer-token
authentication middleware (task 15.1); the actor Party Identity
travels in the body's ``authoring_party_id`` field or in the
temporary ``X-Actor-Party-Id`` header.
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
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService, SupportRef
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.routes.trails import (
    get_engine,
    get_trail_service,
    router as trails_router,
)
from walking_slice.trails import TrailService


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


def _seed_full_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> dict[str, str]:
    """Seed a full Source Evidence → Decision pipeline.

    Returns the identifiers each Trail Step needs to cite resolvable
    targets across the five ordinals.
    """
    with engine.begin() as conn:
        doc = evidence_repository.create_document(
            conn,
            content_bytes=b"Hello, world. The quick brown fox jumps.",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=5,
            contributing_party_id=_PARTY_ID,
        )
        finding = knowledge_service.create_finding(
            conn,
            statement="An evidence-backed claim about the corpus.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                )
            ],
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="A rationale derived from the supporting finding.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=recommendation.recommendation_revision_id,
            outcome="Accept",
            rationale="Approved based on the recommendation.",
            deciding_party_id=_PARTY_ID,
            authority_basis=AuthorityBasisRef(
                type="role-grant-id", id=_AUTHORITY_BASIS_ID
            ),
            applicable_scope=_SCOPE,
        )

    return {
        "document_resource_id": doc.resource_id,
        "document_revision_id": doc.revision_id,
        "region_id": region.region_id,
        "finding_id": finding.finding_id,
        "finding_revision_id": finding.finding_revision_id,
        "recommendation_id": recommendation.recommendation_id,
        "recommendation_revision_id": recommendation.recommendation_revision_id,
        "decision_id": decision.decision_id,
    }


def _valid_step_bodies(ids: dict[str, str]) -> list[dict]:
    """Build the five Trail Step request bodies matching the seeded pipeline."""
    return [
        {
            "ordinal": 1,
            "target_kind": "document_revision",
            "target_id": ids["document_resource_id"],
            "target_revision_id": ids["document_revision_id"],
            "annotation": "The source document.",
        },
        {
            "ordinal": 2,
            "target_kind": "region_occurrence",
            "target_id": ids["document_revision_id"],
            "region_id": ids["region_id"],
            "annotation": "The cited region.",
        },
        {
            "ordinal": 3,
            "target_kind": "finding_revision",
            "target_id": ids["finding_id"],
            "target_revision_id": ids["finding_revision_id"],
            "annotation": "The supporting finding.",
        },
        {
            "ordinal": 4,
            "target_kind": "recommendation_revision",
            "target_id": ids["recommendation_id"],
            "target_revision_id": ids["recommendation_revision_id"],
            "annotation": "The recommendation.",
        },
        {
            "ordinal": 5,
            "target_kind": "decision",
            "target_id": ids["decision_id"],
            "annotation": "The authorized decision.",
        },
    ]


def _valid_body(ids: dict[str, str]) -> dict:
    """Build a valid create-Trail request body."""
    return {
        "purpose": "Walk the slice from evidence to authorized decision.",
        "audience_id": "pilot/team-a",
        "ordering_rationale": "Pipeline order.",
        "authoring_party_id": _PARTY_ID,
        "scope": _SCOPE,
        "steps": _valid_step_bodies(ids),
    }


def _assign_trail_author_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str = _SCOPE,
) -> None:
    """Grant ``party_id`` a Trail Author role with ``modify`` authority.

    The Trail_Service authorization check (when wired) consults
    ``modify`` authority on ``create.trail``; granting view + modify
    keeps the role analogous to other authoring roles in the slice.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="trail-author",
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
def trails_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the trails router without authorization.

    The :class:`TrailService` is built without an
    :class:`AuthorizationService` so structural validation, target
    resolvability, persistence, and material-change detection can be
    exercised in isolation. The authorization-wired variant lives in
    :func:`trails_app_authorized`.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, display="Trail Author")

    clock = FixedClock(_FIXED_INSTANT)
    audit_log = AuditLog(clock)
    identity_service = IdentityService()
    evidence_repository = EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    trail_service = TrailService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )

    app = FastAPI()
    app.include_router(trails_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_trail_service] = lambda: trail_service

    app.state.engine = engine
    app.state.clock = clock
    app.state.evidence_repository = evidence_repository
    app.state.knowledge_service = knowledge_service
    app.state.trail_service = trail_service
    return app


@pytest.fixture
def trails_app_authorized(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the trails router with authorization enforced.

    Tests that exercise the denial path leave ``_PARTY_ID`` without a
    role assignment so the wired :class:`AuthorizationService` returns
    ``deny`` with ``reason_code=no-role-assignment``. Tests that
    exercise the authorized path explicitly grant a Trail Author role
    via :func:`_assign_trail_author_role`.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, display="Trail Author")
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
    evidence_repository = EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    trail_service = TrailService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )

    app = FastAPI()
    app.include_router(trails_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_trail_service] = lambda: trail_service

    app.state.engine = engine
    app.state.clock = clock
    app.state.evidence_repository = evidence_repository
    app.state.knowledge_service = knowledge_service
    app.state.trail_service = trail_service
    app.state.authorization_service = authorization_service
    return app


@pytest_asyncio.fixture
async def client(trails_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=trails_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


@pytest_asyncio.fixture
async def authorized_client(
    trails_app_authorized: FastAPI,
) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=trails_app_authorized)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# POST /api/v1/trails — happy path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_trail_with_valid_five_steps_returns_201(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """A valid five-step submission inserts a Trail, Trail Revision, and
    five Trail Steps (Requirement 9.1)."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )

    response = await client.post("/api/v1/trails", json=_valid_body(ids))

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["trail_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["trail_revision_id"]), body
    assert body["purpose"] == "Walk the slice from evidence to authorized decision."
    assert body["audience_id"] == "pilot/team-a"
    assert body["ordering_rationale"] == "Pipeline order."
    assert len(body["steps"]) == 5
    # Steps come back in ordinal order.
    ordinals = [step["ordinal"] for step in body["steps"]]
    assert ordinals == [1, 2, 3, 4, 5]
    # Every step carries the AD-WS-12 selection mode.
    for step in body["steps"]:
        assert step["selection_mode"] == "Pinned"
        assert _CANONICAL_UUID7_REGEX.match(step["trail_step_id"]), step

    # Verify the persisted Trail header points at the new Revision.
    engine: Engine = trails_app.state.engine
    with engine.connect() as conn:
        trail_row = (
            conn.execute(
                text(
                    "SELECT current_revision_id FROM Trails WHERE trail_id = :tid"
                ),
                {"tid": body["trail_id"]},
            )
            .mappings()
            .one()
        )
        step_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Trail_Steps "
                "WHERE trail_revision_id = :trev"
            ),
            {"trev": body["trail_revision_id"]},
        ).scalar_one()

    assert trail_row["current_revision_id"] == body["trail_revision_id"]
    assert step_count == 5


# ---------------------------------------------------------------------------
# POST /api/v1/trails — request-shape rejections.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_trail_missing_purpose_returns_400(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """A request missing ``purpose`` is rejected by the Pydantic layer."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    body = _valid_body(ids)
    del body["purpose"]

    response = await client.post("/api/v1/trails", json=body)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_trail_request"
    assert "purpose" in detail["missing"]


@pytest.mark.asyncio
async def test_create_trail_with_non_pinned_selection_mode_returns_400(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """An explicit non-``Pinned`` ``selection_mode`` is rejected (AD-WS-12)."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    body = _valid_body(ids)
    body["steps"][0]["selection_mode"] = "Live"

    response = await client.post("/api/v1/trails", json=body)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_trail_request"


@pytest.mark.asyncio
async def test_create_trail_with_four_steps_returns_400(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """A submission with fewer than five steps is rejected by the service
    layer with ``failed_constraint=step_count_invalid`` (Requirement 9.7)."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    body = _valid_body(ids)
    body["steps"].pop()  # leaves four steps

    response = await client.post("/api/v1/trails", json=body)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "step_count_invalid"
    assert detail["failed_constraint"] == "step_count_invalid"


# ---------------------------------------------------------------------------
# POST /api/v1/trails — unresolved target (Requirement 9.5).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_trail_with_unresolved_target_returns_400_with_list(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """An unresolvable Trail Step target yields a 400 with per-ordinal detail.

    Requirement 9.5 demands the response identify *each* unresolved
    step by ordinal and target reference. No partial Trail, Trail
    Revision, or Trail Step row is persisted because resolvability
    runs before any INSERT.
    """
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    body = _valid_body(ids)
    # Replace the decision step (ordinal 5) with an unknown identifier.
    body["steps"][4]["target_id"] = _UNKNOWN_UUID7

    response = await client.post("/api/v1/trails", json=body)

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "trail_target_unresolved"
    assert detail["failed_constraint"] == "trail_target_unresolved"
    unresolved = detail["unresolved_steps"]
    assert len(unresolved) == 1
    assert unresolved[0]["ordinal"] == 5
    assert unresolved[0]["target_id"] == _UNKNOWN_UUID7

    # Verify nothing was persisted.
    engine: Engine = trails_app.state.engine
    with engine.connect() as conn:
        trail_count = conn.execute(text("SELECT COUNT(*) FROM Trails")).scalar_one()
        revision_count = conn.execute(
            text("SELECT COUNT(*) FROM Trail_Revisions")
        ).scalar_one()
        step_count = conn.execute(text("SELECT COUNT(*) FROM Trail_Steps")).scalar_one()
    assert trail_count == 0
    assert revision_count == 0
    assert step_count == 0


# ---------------------------------------------------------------------------
# POST /api/v1/trails — authorization denial (Requirement 7.4 / AD-WS-9).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_trail_unauthorized_returns_403_with_denial_shape(
    authorized_client: AsyncClient, trails_app_authorized: FastAPI
) -> None:
    """A caller without a Trail Author role yields 403 with the AD-WS-9
    indistinguishable denial response shape (Requirement 7.4)."""
    ids = _seed_full_pipeline(
        trails_app_authorized.state.engine,
        trails_app_authorized.state.evidence_repository,
        trails_app_authorized.state.knowledge_service,
    )

    response = await authorized_client.post("/api/v1/trails", json=_valid_body(ids))

    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    # The denial response carries ONLY the three AD-WS-9 fields.
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }
    assert detail["generic_denial_indicator"] == "denied"
    assert detail["reason_code"] == "no-role-assignment"
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # No Trail rows persisted; the consequential transaction rolled back.
    engine: Engine = trails_app_authorized.state.engine
    with engine.connect() as conn:
        trail_count = conn.execute(text("SELECT COUNT(*) FROM Trails")).scalar_one()
    assert trail_count == 0


@pytest.mark.asyncio
async def test_create_trail_authorized_role_returns_201(
    authorized_client: AsyncClient, trails_app_authorized: FastAPI
) -> None:
    """An authorized Trail Author can create a Trail via the same router."""
    _assign_trail_author_role(
        trails_app_authorized.state.authorization_service,
        trails_app_authorized.state.engine,
        party_id=_PARTY_ID,
    )
    ids = _seed_full_pipeline(
        trails_app_authorized.state.engine,
        trails_app_authorized.state.evidence_repository,
        trails_app_authorized.state.knowledge_service,
    )

    response = await authorized_client.post("/api/v1/trails", json=_valid_body(ids))

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["trail_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["trail_revision_id"]), body


# ---------------------------------------------------------------------------
# POST /api/v1/trails/{trail_id}/revisions — material change.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_revision_material_change_returns_201_with_predecessor(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """A material change yields a new immutable Trail Revision linked to
    the prior Revision by ``predecessor_revision_id`` (Requirement 9.4)."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    create_response = await client.post("/api/v1/trails", json=_valid_body(ids))
    assert create_response.status_code == 201
    initial = create_response.json()

    # Submit an append with a different purpose — material change.
    body = _valid_body(ids)
    body["purpose"] = "An entirely new purpose statement for this Trail."

    response = await client.post(
        f"/api/v1/trails/{initial['trail_id']}/revisions",
        json=body,
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["created_new_revision"] is True
    assert payload["trail_id"] == initial["trail_id"]
    assert payload["trail_revision_id"] != initial["trail_revision_id"]
    assert payload["predecessor_revision_id"] == initial["trail_revision_id"]
    assert payload["purpose"] == "An entirely new purpose statement for this Trail."

    # Verify two ``Trail_Revisions`` rows exist for the trail and the
    # ``Trails.current_revision_id`` pointer was updated.
    engine: Engine = trails_app.state.engine
    with engine.connect() as conn:
        revision_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Trail_Revisions WHERE trail_id = :tid"
            ),
            {"tid": initial["trail_id"]},
        ).scalar_one()
        pointer = conn.execute(
            text("SELECT current_revision_id FROM Trails WHERE trail_id = :tid"),
            {"tid": initial["trail_id"]},
        ).scalar_one()
    assert revision_count == 2
    assert pointer == payload["trail_revision_id"]


@pytest.mark.asyncio
async def test_append_revision_no_change_returns_existing_revision(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """A byte-equivalent submission returns the prior Revision unchanged
    (Requirement 9.4 — "preserve the prior Trail Revision unchanged")."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    create_response = await client.post("/api/v1/trails", json=_valid_body(ids))
    assert create_response.status_code == 201
    initial = create_response.json()

    # Submit an identical body — no material change.
    response = await client.post(
        f"/api/v1/trails/{initial['trail_id']}/revisions",
        json=_valid_body(ids),
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["created_new_revision"] is False
    assert payload["trail_revision_id"] == initial["trail_revision_id"]
    assert payload["predecessor_revision_id"] == initial["trail_revision_id"]

    # Only one Trail Revision exists.
    engine: Engine = trails_app.state.engine
    with engine.connect() as conn:
        revision_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Trail_Revisions WHERE trail_id = :tid"
            ),
            {"tid": initial["trail_id"]},
        ).scalar_one()
    assert revision_count == 1


@pytest.mark.asyncio
async def test_append_revision_unknown_trail_returns_404(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """An append against an unknown Trail Identity returns a 404 carrying
    the offending ``trail_id`` (Requirement 9.4)."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )

    response = await client.post(
        f"/api/v1/trails/{_UNKNOWN_UUID7}/revisions",
        json=_valid_body(ids),
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "trail_not_found"
    assert detail["trail_id"] == _UNKNOWN_UUID7


# ---------------------------------------------------------------------------
# GET /api/v1/trails/{trail_id}/revisions/{revision_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_trail_revision_returns_persisted_row_with_five_steps(
    client: AsyncClient, trails_app: FastAPI
) -> None:
    """The GET endpoint returns the persisted Trail Revision and its five
    Trail Steps in ordinal order."""
    ids = _seed_full_pipeline(
        trails_app.state.engine,
        trails_app.state.evidence_repository,
        trails_app.state.knowledge_service,
    )
    create_response = await client.post("/api/v1/trails", json=_valid_body(ids))
    assert create_response.status_code == 201
    created = create_response.json()

    response = await client.get(
        f"/api/v1/trails/{created['trail_id']}/revisions/{created['trail_revision_id']}"
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["trail_id"] == created["trail_id"]
    assert payload["trail_revision_id"] == created["trail_revision_id"]
    assert payload["predecessor_revision_id"] is None
    assert payload["purpose"] == "Walk the slice from evidence to authorized decision."
    assert payload["audience_id"] == "pilot/team-a"
    assert payload["ordering_rationale"] == "Pipeline order."
    assert payload["authoring_party_id"] == _PARTY_ID

    steps = payload["steps"]
    assert len(steps) == 5
    assert [step["ordinal"] for step in steps] == [1, 2, 3, 4, 5]
    assert steps[0]["target_kind"] == "document_revision"
    assert steps[1]["target_kind"] == "region_occurrence"
    assert steps[2]["target_kind"] == "finding_revision"
    assert steps[3]["target_kind"] == "recommendation_revision"
    assert steps[4]["target_kind"] == "decision"
    for step in steps:
        assert step["selection_mode"] == "Pinned"


@pytest.mark.asyncio
async def test_get_trail_revision_unknown_returns_404(
    client: AsyncClient,
) -> None:
    """A GET against an unknown ``revision_id`` returns 404."""
    response = await client.get(
        f"/api/v1/trails/{_UNKNOWN_UUID7}/revisions/{_UNKNOWN_UUID7}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "trail_revision_not_found"
    assert detail["trail_id"] == _UNKNOWN_UUID7
    assert detail["trail_revision_id"] == _UNKNOWN_UUID7
