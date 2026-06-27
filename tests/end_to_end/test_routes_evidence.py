"""End-to-end HTTP tests for the Evidence_Repository routes (task 5.4).

These tests drive the :mod:`walking_slice.routes.evidence` :class:`APIRouter`
through :class:`httpx.AsyncClient` over the FastAPI ASGI transport, exercising:

- ``POST /api/v1/documents`` — 201 on success; 400 when content exceeds
  the Requirement 2.6 size cap; structured error codes for missing
  contributing Party and unknown authority.
- ``POST /api/v1/documents/{rid}/revisions`` — 201 chained to a real
  ``parent_revision_id``.
- ``POST /api/v1/documents/{rid}/revisions/{rev}/regions`` — 201 with
  digest + offsets; 400 on inverted or out-of-range spans.
- ``GET /api/v1/documents/{rid}/revisions/{rev}`` — returns the
  Document Revision content as base64; 404 on unknown identifier.
- ``GET /api/v1/regions/{rid}/occurrences/{rev}`` — returns the Region
  Occurrence row; 404 on unknown identifier.
- ``PATCH /api/v1/documents/{rid}/location`` — renames the Source
  Document without changing its identity, persists a ``rename.document``
  audit row.

The tests deliberately do not exercise the bearer-token authentication
middleware (task 15.1); the actor Party Identity travels in the body's
``contributing_party_id`` field or in the temporary
``X-Actor-Party-Id`` header, depending on the endpoint.
"""

from __future__ import annotations

import base64
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

import walking_slice.evidence as evidence_module
from walking_slice.audit import AuditLog
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.routes.evidence import (
    get_engine,
    get_evidence_repository,
    router as evidence_router,
)


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Seed identifiers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UNKNOWN_UUID7 = "00000000-0000-7000-8000-deadbeefcafe"


def _seed_party(conn, party_id: str = _PARTY_ID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Test Party', :ts)
            """
        ),
        {"pid": party_id, "ts": "2026-01-01T00:00:00.000Z"},
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


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the evidence router with overridden DI."""
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)

    clock = FixedClock(_FIXED_INSTANT)
    audit_log = AuditLog(clock)
    identity_service = IdentityService()
    repository = EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )

    app = FastAPI()
    app.include_router(evidence_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_evidence_repository] = lambda: repository

    app.state.engine = engine
    app.state.clock = clock
    return app


@pytest_asyncio.fixture
async def client(evidence_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=evidence_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _create_document_payload(
    content: bytes = b"hello evidence", **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "content_bytes": _b64(content),
        "contributing_party_id": _PARTY_ID,
        "authority": "authoritative",
    }
    payload.update(overrides)
    return payload


async def _create_document(
    client: AsyncClient, content: bytes = b"hello evidence", **overrides: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/documents",
        json=_create_document_payload(content=content, **overrides),
    )
    assert response.status_code == 201, response.text
    return response.json()


# ---------------------------------------------------------------------------
# POST /api/v1/documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_document_returns_201_with_resource_and_revision_ids(
    client: AsyncClient, evidence_app: FastAPI
) -> None:
    """Valid submission yields 201 with canonical UUIDv7 identifiers."""
    response = await client.post(
        "/api/v1/documents",
        json=_create_document_payload(content=b"hello evidence repository"),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["resource_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["revision_id"]), body
    assert len(body["content_digest_sha256"]) == 64
    assert body["recorded_at"].endswith("Z")

    # Verify rows landed in the database — Source_Documents,
    # Document_Revisions, and Audit_Records all in one transaction (AD-WS-5).
    engine: Engine = evidence_app.state.engine
    with engine.connect() as conn:
        doc_count = conn.execute(
            text("SELECT COUNT(*) FROM Source_Documents WHERE resource_id = :r"),
            {"r": body["resource_id"]},
        ).scalar_one()
        rev_row = (
            conn.execute(
                text(
                    "SELECT parent_revision_id, content_bytes, "
                    "content_digest_sha256, contributing_party_id "
                    "FROM Document_Revisions WHERE revision_id = :rev"
                ),
                {"rev": body["revision_id"]},
            )
            .mappings()
            .one()
        )
        audit_row = (
            conn.execute(
                text(
                    "SELECT action_type, outcome FROM Audit_Records "
                    "WHERE target_revision_id = :rev"
                ),
                {"rev": body["revision_id"]},
            )
            .mappings()
            .one()
        )

    assert doc_count == 1
    assert rev_row["parent_revision_id"] is None
    assert bytes(rev_row["content_bytes"]) == b"hello evidence repository"
    assert rev_row["content_digest_sha256"] == body["content_digest_sha256"]
    assert rev_row["contributing_party_id"] == _PARTY_ID
    assert audit_row["action_type"] == "create.document_revision"
    assert audit_row["outcome"] == "consequential"


@pytest.mark.asyncio
async def test_create_document_over_size_cap_returns_400_content_too_large(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content exceeding ``MAX_CONTENT_BYTES`` is rejected with a 400.

    The 100 MB cap is enforced inside ``EvidenceRepository`` via
    :data:`walking_slice.evidence.MAX_CONTENT_BYTES`. Monkey-patching
    the constant to a small value lets the test fire the
    ``content_too_large`` branch without shipping 100+ MB over the
    wire; the validator reads the module-level constant at call time so
    the override takes effect immediately.
    """
    monkeypatch.setattr(evidence_module, "MAX_CONTENT_BYTES", 16)

    response = await client.post(
        "/api/v1/documents",
        # 17 bytes > the patched cap (16). The base64 payload is
        # decoded by Pydantic before the service-layer length check.
        json=_create_document_payload(content=b"x" * 17),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "content_too_large"
    assert detail["failed_constraint"] == "content_too_large"


@pytest.mark.asyncio
async def test_create_document_empty_content_returns_400(
    client: AsyncClient,
) -> None:
    """Empty ``content_bytes`` is rejected per Requirement 2.6."""
    response = await client.post(
        "/api/v1/documents",
        json=_create_document_payload(content=b""),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "content_empty"


@pytest.mark.asyncio
async def test_create_document_unknown_authority_returns_400(
    client: AsyncClient,
) -> None:
    """Authority outside the AD-WS-1 enumeration yields a 400."""
    response = await client.post(
        "/api/v1/documents",
        json=_create_document_payload(authority="not-a-real-authority"),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "authority_invalid"


# ---------------------------------------------------------------------------
# POST /api/v1/documents/{resource_id}/revisions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_revision_returns_201_chained_to_parent(
    client: AsyncClient,
) -> None:
    """An appended Revision points to the prior Revision via ``parent_revision_id``."""
    first = await _create_document(client, content=b"v1")

    response = await client.post(
        f"/api/v1/documents/{first['resource_id']}/revisions",
        json={
            "content_bytes": _b64(b"v2-changed"),
            "contributing_party_id": _PARTY_ID,
            "change_description": "Edited section 2",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["resource_id"] == first["resource_id"]
    assert _CANONICAL_UUID7_REGEX.match(body["revision_id"]), body
    assert body["parent_revision_id"] == first["revision_id"]
    assert body["revision_id"] != first["revision_id"]


@pytest.mark.asyncio
async def test_append_revision_unknown_resource_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.post(
        f"/api/v1/documents/{_UNKNOWN_UUID7}/revisions",
        json={
            "content_bytes": _b64(b"v2"),
            "contributing_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "source_document_not_found"


# ---------------------------------------------------------------------------
# POST /api/v1/documents/{rid}/revisions/{rev}/regions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_region_occurrence_returns_201_with_digest(
    client: AsyncClient,
) -> None:
    """A valid span returns 201 with a region_id, offsets, and digest."""
    doc = await _create_document(client, content=b"the quick brown fox")

    response = await client.post(
        f"/api/v1/documents/{doc['resource_id']}/revisions/{doc['revision_id']}/regions",
        json={
            "start_offset_bytes": 4,
            "end_offset_bytes": 9,  # spans "quick"
            "contributing_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["region_id"]), body
    assert body["revision_id"] == doc["revision_id"]
    assert body["start_offset_bytes"] == 4
    assert body["end_offset_bytes"] == 9
    assert body["span_byte_length"] == 5
    # SHA-256 hex of b"quick" — kept here as a literal so a regression
    # in span computation (off-by-one, wrong slice direction) surfaces.
    import hashlib
    expected_digest = hashlib.sha256(b"quick").hexdigest()
    assert body["span_content_digest_sha256"] == expected_digest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "expected_constraint"),
    [
        (5, 5, "start_offset_not_less_than_end_offset"),  # empty span
        (10, 5, "start_offset_not_less_than_end_offset"),  # inverted span
        (-1, 5, "start_offset_negative"),  # negative start (also Pydantic ge=0)
    ],
)
async def test_create_region_invalid_offsets_returns_400(
    client: AsyncClient,
    start: int,
    end: int,
    expected_constraint: str,
) -> None:
    """Requirement 3.5 sub-rules each surface as a structured 400."""
    doc = await _create_document(client, content=b"some content here")

    response = await client.post(
        f"/api/v1/documents/{doc['resource_id']}/revisions/{doc['revision_id']}/regions",
        json={
            "start_offset_bytes": start,
            "end_offset_bytes": end,
            "contributing_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 400, response.text
    # Negative start is caught by Pydantic ``ge=0`` before the service
    # is invoked, so the error code is the generic
    # ``invalid_region_request``; the in-range inverted / empty cases
    # surface the service-layer ``failed_constraint``.
    detail = response.json()["detail"]
    if start < 0:
        assert detail["error"] == "invalid_region_request"
    else:
        assert detail["failed_constraint"] == expected_constraint


@pytest.mark.asyncio
async def test_create_region_offset_exceeds_content_returns_400(
    client: AsyncClient,
) -> None:
    """A span whose end_offset exceeds content length is rejected."""
    doc = await _create_document(client, content=b"abc")

    response = await client.post(
        f"/api/v1/documents/{doc['resource_id']}/revisions/{doc['revision_id']}/regions",
        json={
            "start_offset_bytes": 0,
            "end_offset_bytes": 100,  # content is only 3 bytes
            "contributing_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["failed_constraint"] == "end_offset_exceeds_content_length"


# ---------------------------------------------------------------------------
# GET /api/v1/documents/{rid}/revisions/{rev}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_document_revision_returns_content(client: AsyncClient) -> None:
    content = b"persisted document content"
    doc = await _create_document(client, content=content)

    response = await client.get(
        f"/api/v1/documents/{doc['resource_id']}/revisions/{doc['revision_id']}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resource_id"] == doc["resource_id"]
    assert body["revision_id"] == doc["revision_id"]
    assert body["parent_revision_id"] is None
    # ``content_bytes`` is base64-encoded on the wire.
    assert base64.b64decode(body["content_bytes"]) == content
    assert body["content_digest_sha256"] == doc["content_digest_sha256"]
    assert body["contributing_party_id"] == _PARTY_ID


@pytest.mark.asyncio
async def test_get_unknown_document_revision_returns_404(client: AsyncClient) -> None:
    response = await client.get(
        f"/api/v1/documents/{_UNKNOWN_UUID7}/revisions/{_UNKNOWN_UUID7}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "revision_not_found"


# ---------------------------------------------------------------------------
# GET /api/v1/regions/{rid}/occurrences/{rev}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_region_occurrence_returns_anchors_and_digest(
    client: AsyncClient,
) -> None:
    doc = await _create_document(client, content=b"the quick brown fox")
    region_response = await client.post(
        f"/api/v1/documents/{doc['resource_id']}/revisions/{doc['revision_id']}/regions",
        json={
            "start_offset_bytes": 4,
            "end_offset_bytes": 9,
            "contributing_party_id": _PARTY_ID,
        },
    )
    assert region_response.status_code == 201
    region = region_response.json()

    response = await client.get(
        f"/api/v1/regions/{region['region_id']}/occurrences/{doc['revision_id']}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["region_id"] == region["region_id"]
    assert body["revision_id"] == doc["revision_id"]
    assert body["start_offset_bytes"] == 4
    assert body["end_offset_bytes"] == 9
    assert body["span_byte_length"] == 5
    assert body["span_content_digest_sha256"] == region["span_content_digest_sha256"]


@pytest.mark.asyncio
async def test_get_unknown_region_occurrence_returns_404(client: AsyncClient) -> None:
    response = await client.get(
        f"/api/v1/regions/{_UNKNOWN_UUID7}/occurrences/{_UNKNOWN_UUID7}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "region_occurrence_not_found"


# ---------------------------------------------------------------------------
# PATCH /api/v1/documents/{rid}/location
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_document_returns_200_and_persists_audit_row(
    client: AsyncClient, evidence_app: FastAPI
) -> None:
    """A rename preserves identity, updates the display path, and audits."""
    doc = await _create_document(
        client,
        content=b"to be renamed",
        current_location="/inbox/original.txt",
    )

    response = await client.patch(
        f"/api/v1/documents/{doc['resource_id']}/location",
        json={"new_current_location": "/archive/renamed.txt"},
        headers={"X-Actor-Party-Id": _PARTY_ID},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resource_id"] == doc["resource_id"]
    assert body["new_current_location"] == "/archive/renamed.txt"
    assert body["previous_location"] == "/inbox/original.txt"

    engine: Engine = evidence_app.state.engine
    with engine.connect() as conn:
        current_location = conn.execute(
            text(
                "SELECT current_location FROM Source_Documents "
                "WHERE resource_id = :r"
            ),
            {"r": doc["resource_id"]},
        ).scalar_one()
        audit_row = (
            conn.execute(
                text(
                    "SELECT action_type, outcome, target_id, target_revision_id, "
                    "actor_party_id FROM Audit_Records "
                    "WHERE target_id = :r AND action_type = 'rename.document'"
                ),
                {"r": doc["resource_id"]},
            )
            .mappings()
            .one()
        )

    assert current_location == "/archive/renamed.txt"
    assert audit_row["action_type"] == "rename.document"
    assert audit_row["outcome"] == "consequential"
    assert audit_row["target_id"] == doc["resource_id"]
    assert audit_row["target_revision_id"] is None
    assert audit_row["actor_party_id"] == _PARTY_ID


@pytest.mark.asyncio
async def test_rename_unknown_document_returns_404(client: AsyncClient) -> None:
    response = await client.patch(
        f"/api/v1/documents/{_UNKNOWN_UUID7}/location",
        json={"new_current_location": "/anywhere"},
        headers={"X-Actor-Party-Id": _PARTY_ID},
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "source_document_not_found"
    assert detail["resource_id"] == _UNKNOWN_UUID7


@pytest.mark.asyncio
async def test_rename_without_actor_returns_400(client: AsyncClient) -> None:
    """Missing ``X-Actor-Party-Id`` and no body actor → 400."""
    doc = await _create_document(client)

    response = await client.patch(
        f"/api/v1/documents/{doc['resource_id']}/location",
        json={"new_current_location": "/somewhere"},
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "actor_party_id_required"
