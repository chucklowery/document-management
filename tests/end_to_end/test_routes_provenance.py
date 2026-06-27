"""End-to-end HTTP tests for the Provenance_Navigator routes (task 12.3).

These tests drive the :mod:`walking_slice.routes.provenance`
:class:`APIRouter` through :class:`httpx.AsyncClient` over the FastAPI
ASGI transport, exercising the Region Occurrence text resolution
endpoint introduced by task 12.3:

- ``GET /api/v1/regions/{region_id}/occurrences/{revision_id}/text``
  by an authorized requesting Party → 200 with base64-encoded
  byte-equivalent bounded text, anchors, persisted and computed
  digests, and ``digest_matches=True`` (Requirements 3.4, 11.2).
- Unauthorized requesting Party → 403 with the AD-WS-9
  indistinguishable denial response shape carrying **only**
  ``generic_denial_indicator``, ``reason_code``, and
  ``correlation_id`` (Requirement 7.4).
- Missing ``X-Actor-Party-Id`` header → 400.
- Unknown region or revision → 404 (Requirement 3.6).

These tests deliberately do not exercise the bearer-token
authentication middleware (task 15.1); the requesting Party
Identity travels in the temporary ``X-Actor-Party-Id`` header.
"""

from __future__ import annotations

import base64
import hashlib
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
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.provenance import ProvenanceNavigator
from walking_slice.routes.provenance import (
    get_engine,
    get_provenance_navigator,
    router as provenance_router,
)


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Seed identifiers / constants.
# ---------------------------------------------------------------------------


_CONTRIBUTING_PARTY_ID = "00000000-0000-7000-8000-0000000e0001"
_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000e0002"
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-0000000e0003"
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-0000000e0004"

_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UNKNOWN_UUID7 = "00000000-0000-7000-8000-deadbeefcafe"

_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]
_EXPECTED_SPAN_DIGEST = hashlib.sha256(_EXPECTED_SPAN_BYTES).hexdigest()


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
        assigning_authority_id=_ASSIGNING_PARTY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


class SeededRegion:
    """Bundle of identifiers returned by :func:`_seed_region`."""

    def __init__(
        self,
        *,
        document_resource_id: str,
        document_revision_id: str,
        region_id: str,
    ) -> None:
        self.document_resource_id = document_resource_id
        self.document_revision_id = document_revision_id
        self.region_id = region_id


def _seed_region(
    engine: Engine, evidence_repository: EvidenceRepository
) -> SeededRegion:
    with engine.begin() as conn:
        document = evidence_repository.create_document(
            conn,
            content_bytes=_DOC_CONTENT,
            contributing_party_id=_CONTRIBUTING_PARTY_ID,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=document.resource_id,
            revision_id=document.revision_id,
            start_offset_bytes=_DOC_SPAN_START,
            end_offset_bytes=_DOC_SPAN_END,
            contributing_party_id=_CONTRIBUTING_PARTY_ID,
        )
    return SeededRegion(
        document_resource_id=document.resource_id,
        document_revision_id=document.revision_id,
        region_id=region.region_id,
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def provenance_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the provenance router with all collaborators wired."""
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn, _CONTRIBUTING_PARTY_ID, display="Researcher")
        _seed_party(conn, _REQUESTER_PARTY_ID, display="Reviewer")
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
    navigator = ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )

    app = FastAPI()
    app.include_router(provenance_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_provenance_navigator] = lambda: navigator

    app.state.engine = engine
    app.state.clock = clock
    app.state.evidence_repository = evidence_repository
    app.state.authorization_service = authorization_service
    app.state.navigator = navigator
    return app


@pytest_asyncio.fixture
async def client(provenance_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=provenance_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Happy path (authorized, Requirements 3.4 / 11.2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_region_text_authorized_returns_200(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """An authorized requesting Party gets the byte-equivalent span,
    anchors, persisted and computed digests, and ``digest_matches=True``."""
    engine: Engine = provenance_app.state.engine
    evidence_repository: EvidenceRepository = provenance_app.state.evidence_repository
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )

    region = _seed_region(engine, evidence_repository)
    _assign_view_role(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        scope=region.document_resource_id,
    )

    response = await client.get(
        f"/api/v1/regions/{region.region_id}/occurrences/"
        f"{region.document_revision_id}/text",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["region_id"] == region.region_id
    assert body["revision_id"] == region.document_revision_id
    assert body["start_offset_bytes"] == _DOC_SPAN_START
    assert body["end_offset_bytes"] == _DOC_SPAN_END
    assert body["span_byte_length"] == _DOC_SPAN_END - _DOC_SPAN_START
    assert body["span_content_digest_sha256"] == _EXPECTED_SPAN_DIGEST
    assert body["computed_digest_sha256"] == _EXPECTED_SPAN_DIGEST
    assert body["digest_matches"] is True
    assert body["recorded_at"].endswith("Z")

    # ``bounded_text`` is base64-encoded; decoding it yields the
    # exact bytes that were anchored at write time (Requirement
    # 11.2 byte-equivalence).
    decoded = base64.b64decode(body["bounded_text"])
    assert decoded == _EXPECTED_SPAN_BYTES


# ---------------------------------------------------------------------------
# Unauthorized denial (Requirement 7.4 / AD-WS-9).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_region_text_unauthorized_returns_ad_ws_9_denial(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """A caller without view authority on the owning Document yields 403
    with the AD-WS-9 indistinguishable denial shape (Requirement 7.4).

    The response body MUST carry exactly three fields:
    ``generic_denial_indicator``, ``reason_code``, ``correlation_id``.
    No information about the region, the document, or the existence
    of either is exposed.
    """
    engine: Engine = provenance_app.state.engine
    evidence_repository: EvidenceRepository = provenance_app.state.evidence_repository

    region = _seed_region(engine, evidence_repository)
    # Deliberately do not assign any role to ``_UNAUTHORIZED_PARTY_ID``.

    response = await client.get(
        f"/api/v1/regions/{region.region_id}/occurrences/"
        f"{region.document_revision_id}/text",
        headers={"X-Actor-Party-Id": _UNAUTHORIZED_PARTY_ID},
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


# ---------------------------------------------------------------------------
# Unresolvable region / revision (Requirement 3.6).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_region_text_unknown_region_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """Unknown ``region_id`` yields 404 with the offending identifiers."""
    engine: Engine = provenance_app.state.engine
    evidence_repository: EvidenceRepository = provenance_app.state.evidence_repository
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )

    region = _seed_region(engine, evidence_repository)
    _assign_view_role(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        scope=region.document_resource_id,
    )

    response = await client.get(
        f"/api/v1/regions/{_UNKNOWN_UUID7}/occurrences/"
        f"{region.document_revision_id}/text",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "region_occurrence_not_found"
    assert detail["region_id"] == _UNKNOWN_UUID7
    assert detail["revision_id"] == region.document_revision_id


@pytest.mark.asyncio
async def test_resolve_region_text_unknown_revision_returns_404(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """Unknown ``revision_id`` yields 404 with the offending identifiers."""
    engine: Engine = provenance_app.state.engine
    evidence_repository: EvidenceRepository = provenance_app.state.evidence_repository
    authorization_service: AuthorizationService = (
        provenance_app.state.authorization_service
    )

    region = _seed_region(engine, evidence_repository)
    _assign_view_role(
        authorization_service,
        engine,
        party_id=_REQUESTER_PARTY_ID,
        scope=region.document_resource_id,
    )

    response = await client.get(
        f"/api/v1/regions/{region.region_id}/occurrences/{_UNKNOWN_UUID7}/text",
        headers={"X-Actor-Party-Id": _REQUESTER_PARTY_ID},
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "region_occurrence_not_found"
    assert detail["region_id"] == region.region_id
    assert detail["revision_id"] == _UNKNOWN_UUID7


# ---------------------------------------------------------------------------
# Missing actor header.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_region_text_missing_actor_header_returns_400(
    client: AsyncClient, provenance_app: FastAPI
) -> None:
    """Missing ``X-Actor-Party-Id`` header → 400."""
    engine: Engine = provenance_app.state.engine
    evidence_repository: EvidenceRepository = provenance_app.state.evidence_repository

    region = _seed_region(engine, evidence_repository)

    response = await client.get(
        f"/api/v1/regions/{region.region_id}/occurrences/"
        f"{region.document_revision_id}/text",
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "actor_party_id_required"
    assert "X-Actor-Party-Id" in detail["missing"]
