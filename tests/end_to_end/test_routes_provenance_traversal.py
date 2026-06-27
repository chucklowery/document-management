"""End-to-end HTTP tests for the task 12.5 provenance traversal routes.

Exercises the five new endpoints added by task 12.5:

- ``GET /api/v1/backlinks?target_id=...&target_revision_id=...&after_cursor=...``
- ``GET /api/v1/decisions/{decision_id}/provenance``
- ``GET /api/v1/findings/{finding_id}/provenance``
- ``GET /api/v1/recommendations/{recommendation_id}/provenance``
- ``GET /api/v1/trails/{trail_id}/revisions/{revision_id}/provenance``

These tests drive the :mod:`walking_slice.routes.provenance` router
through :class:`httpx.AsyncClient` over the FastAPI ASGI transport.
They verify the happy-path response shape (Requirement 8.2 backlink
attributes, Requirement 11.1 five-stage chain, Requirement 10.4
provenance shapes), the AD-WS-9 indistinguishable denial path
(Requirement 11.7 unresolvable / denied → 404 not-found
indistinguishable response shape), and the 400 missing-actor case.
"""

from __future__ import annotations

import base64
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
from walking_slice.provenance import ProvenanceNavigator
from walking_slice.routes.provenance import (
    get_engine,
    get_provenance_navigator,
    router as provenance_router,
)
from walking_slice.trails import TrailService, TrailStepInput


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_DECIDING_PARTY_ID = "00000000-0000-7000-8000-0000000e0011"
_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000e0012"
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-0000000e0013"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000e0014"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000e00a1")

_SCOPE = "pilot/team-a"
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]

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


def _assign_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str,
) -> None:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="reviewer",
        scope=scope,
        authorities_granted=("view",),
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


class SeededPipeline:
    """Bundle of identifiers for the full Evidence → Decision pipeline."""

    def __init__(
        self,
        *,
        document_resource_id: str,
        document_revision_id: str,
        region_id: str,
        finding_id: str,
        finding_revision_id: str,
        recommendation_id: str,
        recommendation_revision_id: str,
        decision_id: str,
        trail_id: str,
        trail_revision_id: str,
    ) -> None:
        self.document_resource_id = document_resource_id
        self.document_revision_id = document_revision_id
        self.region_id = region_id
        self.finding_id = finding_id
        self.finding_revision_id = finding_revision_id
        self.recommendation_id = recommendation_id
        self.recommendation_revision_id = recommendation_revision_id
        self.decision_id = decision_id
        self.trail_id = trail_id
        self.trail_revision_id = trail_revision_id


def _seed_full_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
    trail_service: TrailService,
) -> SeededPipeline:
    basis = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)
    with engine.begin() as conn:
        document = evidence_repository.create_document(
            conn,
            content_bytes=_DOC_CONTENT,
            contributing_party_id=_DECIDING_PARTY_ID,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=document.resource_id,
            revision_id=document.revision_id,
            start_offset_bytes=_DOC_SPAN_START,
            end_offset_bytes=_DOC_SPAN_END,
            contributing_party_id=_DECIDING_PARTY_ID,
        )
        finding = knowledge_service.create_finding(
            conn,
            statement="The quick brown fox is documented.",
            authoring_party_id=_DECIDING_PARTY_ID,
            supporting_region_occurrences=(
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=document.revision_id,
                ),
            ),
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_DECIDING_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend action.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Accept recommendation.",
            deciding_party_id=_DECIDING_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )
        trail = trail_service.create_trail(
            conn,
            purpose="Walk Evidence to Decision.",
            audience_id="reviewers",
            steps=(
                TrailStepInput(
                    ordinal=1,
                    target_kind="document_revision",
                    target_id=document.resource_id,
                    target_revision_id=document.revision_id,
                ),
                TrailStepInput(
                    ordinal=2,
                    target_kind="region_occurrence",
                    target_id=document.revision_id,
                    region_id=region.region_id,
                ),
                TrailStepInput(
                    ordinal=3,
                    target_kind="finding_revision",
                    target_id=finding.finding_id,
                    target_revision_id=finding.finding_revision_id,
                ),
                TrailStepInput(
                    ordinal=4,
                    target_kind="recommendation_revision",
                    target_id=recommendation.recommendation_id,
                    target_revision_id=(
                        recommendation.recommendation_revision_id
                    ),
                ),
                TrailStepInput(
                    ordinal=5,
                    target_kind="decision",
                    target_id=decision.decision_id,
                ),
            ),
            authoring_party_id=_DECIDING_PARTY_ID,
        )

    return SeededPipeline(
        document_resource_id=document.resource_id,
        document_revision_id=document.revision_id,
        region_id=region.region_id,
        finding_id=finding.finding_id,
        finding_revision_id=finding.finding_revision_id,
        recommendation_id=recommendation.recommendation_id,
        recommendation_revision_id=recommendation.recommendation_revision_id,
        decision_id=decision.decision_id,
        trail_id=trail.trail_id,
        trail_revision_id=trail.trail_revision_id,
    )


def _grant_full_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    pipeline: SeededPipeline,
) -> None:
    """Grant the Party view authority on every scope the chain touches."""
    for scope in (
        _SCOPE,
        pipeline.recommendation_id,
        pipeline.finding_id,
        pipeline.document_resource_id,
        pipeline.trail_id,
    ):
        _assign_view_role(
            authorization_service, engine, party_id=party_id, scope=scope
        )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def provenance_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app with the provenance router and pipeline seeded."""
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _DECIDING_PARTY_ID, display="Decision Maker")
        _seed_party(conn, _REQUESTER_PARTY_ID, display="Reviewer")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, display="Unauthorized")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, display="Resource Steward")

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
    )
    navigator = ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )

    pipeline = _seed_full_pipeline(
        engine, evidence_repository, knowledge_service, trail_service
    )

    app = FastAPI()
    app.include_router(provenance_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_provenance_navigator] = lambda: navigator

    app.state.engine = engine
    app.state.clock = clock
    app.state.authorization_service = authorization_service
    app.state.navigator = navigator
    app.state.pipeline = pipeline
    return app


@pytest_asyncio.fixture
async def client(provenance_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=provenance_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# GET /backlinks (Requirements 8.1, 8.2, 8.6).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backlinks_returns_authorized_inbound_relationships(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """Inbound Relationships on the Region Occurrence — Supports row."""
    engine: Engine = provenance_app.state.engine
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )
    pipeline: SeededPipeline = provenance_app.state.pipeline

    _grant_full_view_authority(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        pipeline=pipeline,
    )

    response = await client.get(
        "/api/v1/backlinks",
        params={
            "target_id": pipeline.region_id,
            "target_revision_id": pipeline.document_revision_id,
        },
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["target_id"] == pipeline.region_id
    assert body["response_size"] == len(body["entries"])
    # One Supports Relationship: Finding Revision → Region Occurrence.
    assert body["response_size"] == 1
    entry = body["entries"][0]
    # Requirement 8.2: identity attributes are surfaced.
    assert entry["relationship_type"] == "Supports"
    assert entry["source_kind"] == "finding_revision"
    assert entry["source_id"] == pipeline.finding_id
    assert entry["source_revision_id"] == pipeline.finding_revision_id


@pytest.mark.asyncio
async def test_backlinks_missing_actor_header_returns_400(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        "/api/v1/backlinks",
        params={"target_id": pipeline.region_id},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == "actor_party_id_required"


@pytest.mark.asyncio
async def test_backlinks_malformed_cursor_returns_400(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        "/api/v1/backlinks",
        params={
            "target_id": pipeline.region_id,
            "after_cursor": "not-a-valid-cursor",
        },
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"]["error"] == "invalid_after_cursor"


@pytest.mark.asyncio
async def test_backlinks_unauthorized_party_returns_empty_page(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """A Party without view authority sees an empty authorized projection.

    Per Requirement 8.3 / Property 4 the response is indistinguishable
    from one for a non-existent endpoint: the entries list is empty,
    response_size is 0, and the cursor is absent.
    """
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        "/api/v1/backlinks",
        params={"target_id": pipeline.region_id},
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["entries"] == []
    assert body["response_size"] == 0
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# GET /decisions/{decision_id}/provenance (Requirement 11.1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_provenance_authorized_returns_full_chain(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    engine: Engine = provenance_app.state.engine
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )
    pipeline: SeededPipeline = provenance_app.state.pipeline

    _grant_full_view_authority(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        pipeline=pipeline,
    )

    response = await client.get(
        f"/api/v1/decisions/{pipeline.decision_id}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Decision at the head.
    assert body["decision"]["decision_id"] == pipeline.decision_id
    assert body["decision"]["outcome"] == "Accept"
    # Recommendation Revision.
    assert body["recommendation_revision"]["kind"] == "recommendation_revision"
    assert body["recommendation_revision"]["recommendation_id"] == (
        pipeline.recommendation_id
    )
    # Findings, regions, documents — one of each.
    assert len(body["findings"]) == 1
    assert body["findings"][0]["kind"] == "finding_revision"
    assert len(body["region_occurrences"]) == 1
    assert body["region_occurrences"][0]["kind"] == "region_occurrence"
    decoded_text = base64.b64decode(
        body["region_occurrences"][0]["bounded_text"]
    )
    assert decoded_text == _EXPECTED_SPAN_BYTES
    assert len(body["document_revisions"]) == 1
    assert body["document_revisions"][0]["kind"] == "document_revision"
    assert body["requested_decision_id"] == pipeline.decision_id


@pytest.mark.asyncio
async def test_decision_provenance_unknown_decision_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    response = await client.get(
        f"/api/v1/decisions/{_UNKNOWN_UUID7}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_decision_provenance_unauthorized_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """Indistinguishable not-found response per Requirement 11.7."""
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        f"/api/v1/decisions/{pipeline.decision_id}/provenance",
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# GET /findings/{finding_id}/provenance (Requirement 10.4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_provenance_authorized_returns_chain(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    engine: Engine = provenance_app.state.engine
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )
    pipeline: SeededPipeline = provenance_app.state.pipeline

    _grant_full_view_authority(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        pipeline=pipeline,
    )

    response = await client.get(
        f"/api/v1/findings/{pipeline.finding_id}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finding_revision"]["finding_id"] == pipeline.finding_id
    assert body["finding_revision"]["finding_revision_id"] == (
        pipeline.finding_revision_id
    )
    assert len(body["region_occurrences"]) == 1
    assert len(body["document_revisions"]) == 1
    assert body["requested_finding_id"] == pipeline.finding_id


@pytest.mark.asyncio
async def test_finding_provenance_unauthorized_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        f"/api/v1/findings/{pipeline.finding_id}/provenance",
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# GET /recommendations/{recommendation_id}/provenance (Requirement 10.4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recommendation_provenance_authorized_returns_chain(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    engine: Engine = provenance_app.state.engine
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )
    pipeline: SeededPipeline = provenance_app.state.pipeline

    _grant_full_view_authority(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        pipeline=pipeline,
    )

    response = await client.get(
        f"/api/v1/recommendations/{pipeline.recommendation_id}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recommendation_revision"]["recommendation_id"] == (
        pipeline.recommendation_id
    )
    assert len(body["findings"]) == 1
    assert len(body["region_occurrences"]) == 1
    assert len(body["document_revisions"]) == 1
    assert body["requested_recommendation_id"] == pipeline.recommendation_id


@pytest.mark.asyncio
async def test_recommendation_provenance_unknown_id_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    response = await client.get(
        f"/api/v1/recommendations/{_UNKNOWN_UUID7}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# GET /trails/{trail_id}/revisions/{revision_id}/provenance (Requirement 10.4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trail_revision_provenance_authorized_returns_chain(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    engine: Engine = provenance_app.state.engine
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )
    pipeline: SeededPipeline = provenance_app.state.pipeline

    _grant_full_view_authority(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        pipeline=pipeline,
    )

    response = await client.get(
        f"/api/v1/trails/{pipeline.trail_id}/revisions/"
        f"{pipeline.trail_revision_id}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["trail_revision"]["trail_id"] == pipeline.trail_id
    assert body["trail_revision"]["trail_revision_id"] == (
        pipeline.trail_revision_id
    )
    # Five steps in ordinal order.
    assert len(body["steps"]) == 5
    assert [s["ordinal"] for s in body["steps"]] == [1, 2, 3, 4, 5]
    # Inner Decision chain populated because the requesting Party has
    # view authority on every stage.
    assert body["decision_chain"] is not None
    assert body["decision_chain"]["decision"]["decision_id"] == (
        pipeline.decision_id
    )


@pytest.mark.asyncio
async def test_trail_revision_provenance_unauthorized_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        f"/api/v1/trails/{pipeline.trail_id}/revisions/"
        f"{pipeline.trail_revision_id}/provenance",
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_trail_revision_provenance_unknown_revision_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    pipeline: SeededPipeline = provenance_app.state.pipeline
    response = await client.get(
        f"/api/v1/trails/{pipeline.trail_id}/revisions/{_UNKNOWN_UUID7}/provenance",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )
    assert response.status_code == 404, response.text
