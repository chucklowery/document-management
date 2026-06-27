"""End-to-end HTTP tests for the Knowledge_Service Findings routes (task 6.2).

These tests drive the :mod:`walking_slice.routes.findings`
:class:`APIRouter` through :class:`httpx.AsyncClient` over the FastAPI
ASGI transport, exercising:

- ``POST /api/v1/findings`` with ``is_hypothesis=True`` and zero supports
  → 201 (Requirement 4.1's hypothesis branch).
- ``POST /api/v1/findings`` with ``is_hypothesis=False`` and valid
  supports → 201 with one Relationship Identity per cited Region
  Occurrence (Requirement 4.5).
- ``POST /api/v1/findings`` with ``is_hypothesis=False`` and zero
  supports → 400 with ``failed_constraint=supports_required_for_non_hypothesis``
  (Requirement 4.3).
- ``POST /api/v1/findings`` citing an unresolvable Region Occurrence
  → 400 with ``region_id`` + ``document_revision_id`` populated.
- ``POST /api/v1/findings/{finding_id}/contradictions`` → 201 with a
  ``Contradicts`` Relationship Identity (Requirement 4.4).
- ``GET /api/v1/findings/{finding_id}/revisions/{revision_id}`` returns
  the persisted row → 200.
- 404 on unknown Finding identifier and on unknown Revision identifier.

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
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService
from walking_slice.persistence import create_schema
from walking_slice.routes.findings import (
    get_engine,
    get_knowledge_service,
    router as findings_router,
)


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Seed identifiers / constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_UNKNOWN_UUID7 = "00000000-0000-7000-8000-deadbeefcafe"


# ---------------------------------------------------------------------------
# Engine + seed helpers.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def findings_app(tmp_path: Path) -> FastAPI:
    """A FastAPI app mounting the findings router with overridden DI.

    The Evidence_Repository is built alongside so test setup can seed a
    real Source Document Revision plus Region Occurrence — needed by
    every non-hypothesis create path. The Region Occurrence is built
    *in-process* (not through the evidence router) because task 6.2
    only mounts the findings router; mixing the two routers here would
    couple the test suite to task 5.4's HTTP shape unnecessarily.
    """
    engine = _build_engine(tmp_path)
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)

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

    app = FastAPI()
    app.include_router(findings_router)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[get_knowledge_service] = lambda: knowledge_service

    app.state.engine = engine
    app.state.clock = clock
    app.state.evidence_repository = evidence_repository
    return app


@pytest_asyncio.fixture
async def client(findings_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=findings_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Region Occurrence seeding helpers.
# ---------------------------------------------------------------------------


def _create_region_occurrence(
    app: FastAPI,
    *,
    content: bytes = b"hello evidence supporting a finding",
    start: int = 0,
    end: int = 5,
) -> tuple[str, str]:
    """Seed a Source Document + Region Occurrence; return (region_id, revision_id).

    Goes through :class:`EvidenceRepository` directly because the
    evidence routes are not mounted in this app instance.
    """
    engine: Engine = app.state.engine
    repository: EvidenceRepository = app.state.evidence_repository
    with engine.begin() as conn:
        doc = repository.create_document(
            conn,
            content_bytes=content,
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )
        region = repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=start,
            end_offset_bytes=end,
            contributing_party_id=_PARTY_ID,
        )
    return region.region_id, doc.revision_id


# ---------------------------------------------------------------------------
# POST /api/v1/findings — hypothesis branch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_hypothesis_finding_with_zero_supports_returns_201(
    client: AsyncClient, findings_app: FastAPI
) -> None:
    """``is_hypothesis=True`` allows zero supports per Requirement 4.1."""
    response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Hypothesis: customers may prefer dark mode.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["finding_id"]), body
    assert _CANONICAL_UUID7_REGEX.match(body["finding_revision_id"]), body
    assert body["is_hypothesis"] is True
    assert body["supporting_relationship_ids"] == []
    assert body["recorded_at"].endswith("Z")

    # Verify the persisted row carries is_hypothesis=1 and no Supports
    # Relationships were written.
    engine: Engine = findings_app.state.engine
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT is_hypothesis FROM Finding_Revisions "
                    "WHERE finding_revision_id = :rev"
                ),
                {"rev": body["finding_revision_id"]},
            )
            .mappings()
            .one()
        )
        relationship_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE source_id = :fid AND relationship_type = 'Supports'"
            ),
            {"fid": body["finding_id"]},
        ).scalar_one()

    assert row["is_hypothesis"] == 1
    assert relationship_count == 0


# ---------------------------------------------------------------------------
# POST /api/v1/findings — non-hypothesis branch.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_non_hypothesis_finding_with_valid_supports_returns_201(
    client: AsyncClient, findings_app: FastAPI
) -> None:
    """A non-hypothesis Finding inserts one Supports row per cited Occurrence.

    Validates Requirements 4.1 (non-hypothesis cites at least one
    Region Occurrence) and 4.5 (one Supports Relationship per cited
    Occurrence).
    """
    region_a = _create_region_occurrence(
        findings_app, content=b"alpha content here", start=0, end=5
    )
    region_b = _create_region_occurrence(
        findings_app, content=b"beta content distinct", start=0, end=4
    )

    response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Two pieces of evidence support this conclusion.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": False,
            "supporting_region_occurrences": [
                {
                    "region_id": region_a[0],
                    "document_revision_id": region_a[1],
                },
                {
                    "region_id": region_b[0],
                    "document_revision_id": region_b[1],
                },
            ],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_hypothesis"] is False
    assert len(body["supporting_relationship_ids"]) == 2
    for rid in body["supporting_relationship_ids"]:
        assert _CANONICAL_UUID7_REGEX.match(rid), rid

    # Verify the two ``Supports`` rows landed with the right pointers.
    engine: Engine = findings_app.state.engine
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_type, target_id, target_revision_id
                    FROM Relationships
                    WHERE source_id = :fid
                    ORDER BY recorded_at, relationship_id
                    """
                ),
                {"fid": body["finding_id"]},
            )
            .mappings()
            .all()
        )

    assert [r["relationship_type"] for r in rows] == ["Supports", "Supports"]
    targets = {(r["target_id"], r["target_revision_id"]) for r in rows}
    assert targets == {region_a, region_b}


# ---------------------------------------------------------------------------
# POST /api/v1/findings — rejection cases.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_non_hypothesis_finding_with_zero_supports_returns_400(
    client: AsyncClient, findings_app: FastAPI
) -> None:
    """A non-hypothesis Finding with zero supports is rejected (Req 4.3).

    No Findings, Finding_Revisions, or Relationships row may be observable
    after the rejection.
    """
    response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "This Finding should be rejected.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": False,
            "supporting_region_occurrences": [],
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "supports_required_for_non_hypothesis"
    assert detail["failed_constraint"] == "supports_required_for_non_hypothesis"

    engine: Engine = findings_app.state.engine
    with engine.connect() as conn:
        finding_count = conn.execute(text("SELECT COUNT(*) FROM Findings")).scalar_one()
        revision_count = conn.execute(
            text("SELECT COUNT(*) FROM Finding_Revisions")
        ).scalar_one()
        relationship_count = conn.execute(
            text("SELECT COUNT(*) FROM Relationships")
        ).scalar_one()
    assert finding_count == 0
    assert revision_count == 0
    assert relationship_count == 0


@pytest.mark.asyncio
async def test_create_finding_with_unresolvable_region_occurrence_returns_400(
    client: AsyncClient, findings_app: FastAPI
) -> None:
    """A citation to a non-existent Region Occurrence yields a 400.

    The response body carries the offending ``region_id`` and
    ``document_revision_id`` so the caller learns which citation
    failed. No partial Findings / Finding_Revisions / Relationships row
    may exist post-rejection (the service verifies all citations
    before any write).
    """
    response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Cites a Region Occurrence that doesn't exist.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": False,
            "supporting_region_occurrences": [
                {
                    "region_id": _UNKNOWN_UUID7,
                    "document_revision_id": _UNKNOWN_UUID7,
                }
            ],
        },
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "region_occurrence_not_resolvable"
    assert detail["region_id"] == _UNKNOWN_UUID7
    assert detail["document_revision_id"] == _UNKNOWN_UUID7

    engine: Engine = findings_app.state.engine
    with engine.connect() as conn:
        finding_count = conn.execute(text("SELECT COUNT(*) FROM Findings")).scalar_one()
        relationship_count = conn.execute(
            text("SELECT COUNT(*) FROM Relationships")
        ).scalar_one()
    assert finding_count == 0
    assert relationship_count == 0


# ---------------------------------------------------------------------------
# POST /api/v1/findings/{finding_id}/contradictions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_contradiction_returns_201_with_relationship(
    client: AsyncClient, findings_app: FastAPI
) -> None:
    """A valid Contradicts request inserts one Relationships row.

    Validates Requirement 4.4: both Finding records remain unchanged
    (Finding_Revisions is append-only) and the recorded relationship
    carries the source Finding Identity, source Revision Identity, target
    Finding Identity, authoring Party, and recorded time.
    """
    # Build two hypothesis Findings so neither needs Region Occurrences.
    response_a = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Original interpretation.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
        },
    )
    assert response_a.status_code == 201, response_a.text
    target = response_a.json()

    response_b = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Competing interpretation.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
        },
    )
    assert response_b.status_code == 201, response_b.text
    source = response_b.json()

    # Snapshot the source + target rows so we can verify "preserve both
    # Finding records unchanged" after the contradiction lands.
    engine: Engine = findings_app.state.engine
    with engine.connect() as conn:
        snapshot_before = (
            conn.execute(
                text(
                    "SELECT finding_revision_id, statement, is_hypothesis "
                    "FROM Finding_Revisions ORDER BY finding_revision_id"
                )
            )
            .mappings()
            .all()
        )

    response = await client.post(
        f"/api/v1/findings/{target['finding_id']}/contradictions",
        json={
            "source_finding_revision_id": source["finding_revision_id"],
            "authoring_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert _CANONICAL_UUID7_REGEX.match(body["relationship_id"]), body
    assert body["relationship_type"] == "Contradicts"
    assert body["source_finding_id"] == source["finding_id"]
    assert body["source_finding_revision_id"] == source["finding_revision_id"]
    assert body["target_finding_id"] == target["finding_id"]
    assert body["authoring_party_id"] == _PARTY_ID

    with engine.connect() as conn:
        snapshot_after = (
            conn.execute(
                text(
                    "SELECT finding_revision_id, statement, is_hypothesis "
                    "FROM Finding_Revisions ORDER BY finding_revision_id"
                )
            )
            .mappings()
            .all()
        )
        contradiction = (
            conn.execute(
                text(
                    """
                    SELECT relationship_type, source_id, source_revision_id,
                           target_id, target_revision_id
                    FROM Relationships
                    WHERE relationship_id = :rid
                    """
                ),
                {"rid": body["relationship_id"]},
            )
            .mappings()
            .one()
        )

    # Both Finding_Revisions rows are byte-equivalent to their prior
    # state (the contradiction inserts only a Relationships row).
    assert [dict(r) for r in snapshot_after] == [dict(r) for r in snapshot_before]
    assert contradiction["relationship_type"] == "Contradicts"
    assert contradiction["source_id"] == source["finding_id"]
    assert contradiction["source_revision_id"] == source["finding_revision_id"]
    assert contradiction["target_id"] == target["finding_id"]
    # Requirement 4.4 keys the relationship on the Finding Resource —
    # the target_revision_id is NULL for Contradicts rows.
    assert contradiction["target_revision_id"] is None


@pytest.mark.asyncio
async def test_record_contradiction_unknown_source_revision_returns_404(
    client: AsyncClient,
) -> None:
    """An unresolved source Revision yields a 404 with role=source."""
    # A real target Finding so the target side is not the failing one.
    target_response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Real target Finding.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
        },
    )
    assert target_response.status_code == 201
    target = target_response.json()

    response = await client.post(
        f"/api/v1/findings/{target['finding_id']}/contradictions",
        json={
            "source_finding_revision_id": _UNKNOWN_UUID7,
            "authoring_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "finding_not_found"
    assert detail["role"] == "source"
    assert detail["finding_revision_id"] == _UNKNOWN_UUID7


@pytest.mark.asyncio
async def test_record_contradiction_unknown_target_finding_returns_404(
    client: AsyncClient,
) -> None:
    """An unresolved target Finding yields a 404 with role=target."""
    # A real source Revision so the source side is not the failing one.
    source_response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Real source Finding.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
        },
    )
    assert source_response.status_code == 201
    source = source_response.json()

    response = await client.post(
        f"/api/v1/findings/{_UNKNOWN_UUID7}/contradictions",
        json={
            "source_finding_revision_id": source["finding_revision_id"],
            "authoring_party_id": _PARTY_ID,
        },
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "finding_not_found"
    assert detail["role"] == "target"
    assert detail["finding_id"] == _UNKNOWN_UUID7


# ---------------------------------------------------------------------------
# GET /api/v1/findings/{finding_id}/revisions/{revision_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_finding_revision_returns_persisted_row(
    client: AsyncClient,
) -> None:
    """The GET endpoint returns every persisted column."""
    create_response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Persisted Finding statement.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
            "assumptions": ["Assumption one.", "Assumption two."],
            "confidence_note": "Confidence is medium.",
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()

    response = await client.get(
        f"/api/v1/findings/{created['finding_id']}/revisions/"
        f"{created['finding_revision_id']}"
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["finding_id"] == created["finding_id"]
    assert body["finding_revision_id"] == created["finding_revision_id"]
    assert body["parent_revision_id"] is None
    assert body["statement"] == "Persisted Finding statement."
    assert body["is_hypothesis"] is True
    assert body["authoring_party_id"] == _PARTY_ID
    assert body["assumptions"] == ["Assumption one.", "Assumption two."]
    assert body["confidence_note"] == "Confidence is medium."
    assert body["recorded_at"] == created["recorded_at"]


@pytest.mark.asyncio
async def test_get_unknown_finding_revision_returns_404(
    client: AsyncClient,
) -> None:
    response = await client.get(
        f"/api/v1/findings/{_UNKNOWN_UUID7}/revisions/{_UNKNOWN_UUID7}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "finding_revision_not_found"
    assert detail["finding_id"] == _UNKNOWN_UUID7
    assert detail["finding_revision_id"] == _UNKNOWN_UUID7


@pytest.mark.asyncio
async def test_get_finding_revision_with_mismatched_finding_id_returns_404(
    client: AsyncClient,
) -> None:
    """A Revision Identity belonging to a different Finding yields a 404.

    The endpoint matches on the composite ``(finding_id,
    finding_revision_id)`` so a caller cannot get a Revision by
    accident through some other Finding's identifier.
    """
    create_response = await client.post(
        "/api/v1/findings",
        json={
            "statement": "Sample.",
            "authoring_party_id": _PARTY_ID,
            "is_hypothesis": True,
            "supporting_region_occurrences": [],
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()

    response = await client.get(
        f"/api/v1/findings/{_UNKNOWN_UUID7}/revisions/"
        f"{created['finding_revision_id']}"
    )

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "finding_revision_not_found"
