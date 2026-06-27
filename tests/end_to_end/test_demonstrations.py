"""End-to-end HTTP demonstrations for the five named scenarios (task 15.5).

Each test drives the fully-composed FastAPI app — built by
:func:`walking_slice.app.create_app` — through :class:`httpx.AsyncClient`
over the ASGI transport, exercising one of the five demonstrations from
the design's *Demonstration Surface*:

1. **Authorization-aware backlinks** (Requirement 8.1). A Party with
   view authority on the target endpoint but not on the source endpoint
   of an inbound Relationship sees an empty (or trimmed) authorized
   projection, while a Party with view authority on both endpoints sees
   the full backlink page.
2. **One linear Trail** (Requirement 9.1). POST a five-step Trail
   covering every pipeline stage, then GET the Trail Revision back and
   confirm the persisted ordinals, target kinds, and Pinned selection
   mode round-trip byte-equivalently.
3. **Omission-aware provenance** (Requirements 10.4, 10.6). Submit a
   Decision with explicit material-source Omission Entries and then
   GET the Decision provenance, confirming the manifest's gap
   descriptors are surfaced in the response so callers can see what
   was deliberately excluded.
4. **Denied unauthorized Decision** (Requirements 7.1, 7.4). A Party
   without ``approve`` authority on the Recommendation's scope gets
   the AD-WS-9 indistinguishable denial response carrying *only*
   ``generic_denial_indicator``, ``reason_code``, and
   ``correlation_id``.
5. **Navigation back to exact Evidence** (Requirement 11.1). GET the
   full Decision → Recommendation → Finding → Region → Document chain
   for a Decision and confirm the returned ``bounded_text`` matches
   ``content_bytes[start:end]`` of the resolved Document Revision
   (Requirement 11.2 byte-equivalence).

Every test authenticates via the temporary ``X-Actor-Party-Id`` header
that the slice carries until task 15.1 lands the bearer-token middleware;
the header travels alongside the body field that names the contributing,
authoring, or deciding Party so the wire contract is uniform across
endpoints.
"""

from __future__ import annotations

import base64
import hashlib
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

from walking_slice.app import SliceServices, create_app
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock


pytestmark = pytest.mark.end_to_end


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# Pipeline-author party: holds view + modify + approve authority on every
# scope the chain touches so the test-setup writes (documents, findings,
# recommendations, decisions, trails) all succeed without re-shuffling
# role assignments. The five demonstrations under test each then exercise
# a *different* Party so the authorization-aware behaviour is visible.
_AUTHOR_PARTY_ID = "00000000-0000-7000-8000-0000000d0001"

# Broad-view Party: granted view authority on the document, finding,
# recommendation, decision, and trail scopes. Used by the backlinks
# demonstration to see the full authorized projection, and by the
# Evidence-navigation demonstration to walk the chain end-to-end.
_BROAD_VIEW_PARTY_ID = "00000000-0000-7000-8000-0000000d0002"

# Limited-view Party: granted view authority on the document scope only.
# Used by the backlinks demonstration to confirm authorization-aware
# behaviour — without view authority on the Finding source endpoint,
# the inbound Supports Relationship is filtered out of the authorized
# projection (Property 4 non-leakage).
_LIMITED_VIEW_PARTY_ID = "00000000-0000-7000-8000-0000000d0003"

# Unauthorized Party: holds no role at all. Used by the denial
# demonstration to trigger AD-WS-9 indistinguishable response shape.
_UNAUTHORIZED_PARTY_ID = "00000000-0000-7000-8000-0000000d0004"

# Resource-steward identity recorded as the ``assigning_authority_id``
# on every role assignment. Identifier opacity (Requirement 1.6) means
# this value never reaches the API surface; the column just needs a
# valid Party Identity reference.
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-0000000d0005"

_SCOPE = "pilot/team-a"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000d00a1")
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_CANONICAL_UUID7_REGEX = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Document content used to seed Source Document, Region Occurrence, and
# the downstream Finding / Recommendation / Decision chain. The span
# offsets target the substring ``"quick brown fox"`` so the byte-
# equivalence assertion in the navigation demonstration is grep-friendly.
_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]
_EXPECTED_DOC_DIGEST = hashlib.sha256(_DOC_CONTENT).hexdigest()


# ---------------------------------------------------------------------------
# Engine + Party seeding.
#
# The composed app does not seed Parties on startup — Party rows are a
# domain concern, not a bootstrap concern — so the test seeds the four
# Parties it needs before driving the HTTP layer. We also seed role
# assignments via the wired :class:`AuthorizationService` reachable
# through ``app.state.services``.
# ---------------------------------------------------------------------------


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


def _seed_party(conn, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": "2026-01-01T00:00:00.000Z"},
    )


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    role_name: str,
    authorities: tuple[str, ...],
    scope: str,
) -> None:
    """Insert one Role_Assignments row via the wired AuthorizationService.

    Each demonstration grants only the authorities its scenario requires
    so the difference between authorized and unauthorized behaviour is
    visible in the test body. The ``assigning_authority_id`` is held
    constant so the column always references a valid Party row.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=authorities,
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_PARTY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def composed_app(tmp_path: Path) -> FastAPI:
    """A fully-composed FastAPI app with the four Parties seeded.

    The clock is pinned to :data:`_FIXED_INSTANT` so ``recorded_at``
    values are byte-equivalent across runs, and the JWT secret is pinned
    to a constant so any future bearer-token surface reproduces. The
    pipeline-author Party is granted view + modify + approve authority
    on the shared scope so the demonstration set-up writes succeed
    without re-shuffling role assignments mid-test.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"demonstrations-test-secret",
    )
    with engine.begin() as conn:
        _seed_party(conn, _AUTHOR_PARTY_ID, "Pipeline Author")
        _seed_party(conn, _BROAD_VIEW_PARTY_ID, "Broad View Reviewer")
        _seed_party(conn, _LIMITED_VIEW_PARTY_ID, "Limited View Reviewer")
        _seed_party(conn, _UNAUTHORIZED_PARTY_ID, "Unauthorized Party")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")

    services: SliceServices = app.state.services
    # Grant the pipeline-author Party every authority on the shared
    # scope so the chain-creation calls below succeed. Tests then add
    # *only* the authorities each demonstration's protagonist needs.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_AUTHOR_PARTY_ID,
        role_name="pipeline-author",
        authorities=("view", "modify", "approve"),
        scope=_SCOPE,
    )
    return app


@pytest_asyncio.fixture
async def client(composed_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=composed_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Pipeline-creation helpers.
#
# Each demonstration seeds a Source Document → Region Occurrence →
# Finding → Recommendation → Decision pipeline via the HTTP surface so
# every test exercises the full app composition. The helpers below
# return the identifiers each demonstration needs to cite back into the
# API.
# ---------------------------------------------------------------------------


async def _create_document(client: AsyncClient) -> dict:
    """POST a Source Document and return the parsed 201 response body."""
    payload = {
        "content_bytes": base64.b64encode(_DOC_CONTENT).decode("ascii"),
        "contributing_party_id": _AUTHOR_PARTY_ID,
        "authority": "authoritative",
    }
    response = await client.post(
        "/api/v1/documents",
        json=payload,
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_region(
    client: AsyncClient, resource_id: str, revision_id: str
) -> dict:
    """POST a Region Occurrence over the seeded Document Revision."""
    payload = {
        "start_offset_bytes": _DOC_SPAN_START,
        "end_offset_bytes": _DOC_SPAN_END,
        "contributing_party_id": _AUTHOR_PARTY_ID,
    }
    response = await client.post(
        f"/api/v1/documents/{resource_id}/revisions/{revision_id}/regions",
        json=payload,
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_finding(
    client: AsyncClient, region_id: str, document_revision_id: str
) -> dict:
    """POST a Finding citing the seeded Region Occurrence."""
    payload = {
        "statement": "The corpus documents a quick brown fox.",
        "authoring_party_id": _AUTHOR_PARTY_ID,
        "is_hypothesis": False,
        "supporting_region_occurrences": [
            {
                "region_id": region_id,
                "document_revision_id": document_revision_id,
            }
        ],
    }
    response = await client.post(
        "/api/v1/findings",
        json=payload,
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_recommendation(client: AsyncClient, finding_id: str) -> dict:
    """POST a Recommendation derived from the seeded Finding."""
    payload = {
        "authoring_party_id": _AUTHOR_PARTY_ID,
        "derived_from_findings": [finding_id],
        "rationale": "Recommend action X based on the fox finding.",
        "applicable_scope": _SCOPE,
    }
    response = await client.post(
        "/api/v1/recommendations",
        json=payload,
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_decision(
    client: AsyncClient,
    *,
    recommendation_id: str,
    recommendation_revision_id: str,
    deciding_party_id: str,
    omissions: list[dict] | None = None,
) -> tuple[int, dict]:
    """POST a Decision targeting the seeded Recommendation Revision.

    Returns ``(status_code, parsed_body)`` so individual demonstrations
    can branch on success vs. denial without re-implementing the call.
    """
    payload = {
        "target_recommendation_revision_id": recommendation_revision_id,
        "outcome": "Accept",
        "rationale": "Accept on the basis of the fox finding.",
        "deciding_party_id": deciding_party_id,
        "authority_basis": {
            "type": "role-grant-id",
            "id": str(_AUTHORITY_BASIS_ID),
        },
        "applicable_scope": _SCOPE,
        "omissions": omissions or [],
    }
    response = await client.post(
        f"/api/v1/recommendations/{recommendation_id}/decisions",
        json=payload,
        headers={"X-Actor-Party-Id": deciding_party_id},
    )
    return response.status_code, response.json()


# ---------------------------------------------------------------------------
# Demonstration 1 — Authorization-aware backlinks (Requirement 8.1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_authorization_aware_backlinks(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A limited-view Party sees a strict subset of the authorized backlinks.

    Seeds a Source Document → Region Occurrence → Finding chain, then
    asks the backlinks endpoint for the inbound Relationships on the
    Region Occurrence under two identities:

    * ``_BROAD_VIEW_PARTY_ID`` holds view authority on **both** the
      Document scope (target endpoint) and the Finding scope (source
      endpoint of the inbound ``Supports`` Relationship), so the
      authorized projection contains the Supports row.
    * ``_LIMITED_VIEW_PARTY_ID`` holds view authority on the Document
      scope only; the Finding source endpoint is not visible and the
      authorized projection therefore omits the Supports row entirely
      (Requirement 8.3 / Property 4 non-leakage).

    The test asserts the visible-set inequality — broad view yields a
    non-empty page, limited view yields an empty page — without
    inspecting the *contents* of the broad page (that's covered by the
    existing ``test_routes_provenance_traversal`` end-to-end tests).
    """
    services: SliceServices = composed_app.state.services
    engine: Engine = composed_app.state.engine

    document = await _create_document(client)
    region = await _create_region(
        client, document["resource_id"], document["revision_id"]
    )
    finding = await _create_finding(
        client, region["region_id"], document["revision_id"]
    )

    # Broad-view Party — view authority on both the Document scope and
    # the Finding scope, so the Supports Relationship is in the
    # authorized projection.
    for scope in (document["resource_id"], finding["finding_id"]):
        _assign_role(
            services.authorization_service,
            engine,
            party_id=_BROAD_VIEW_PARTY_ID,
            role_name="broad-reviewer",
            authorities=("view",),
            scope=scope,
        )

    # Limited-view Party — view authority on the Document scope only.
    # The Finding source endpoint is not visible, so the Supports
    # Relationship falls out of the authorized projection.
    _assign_role(
        services.authorization_service,
        engine,
        party_id=_LIMITED_VIEW_PARTY_ID,
        role_name="limited-reviewer",
        authorities=("view",),
        scope=document["resource_id"],
    )

    broad_response = await client.get(
        "/api/v1/backlinks",
        params={
            "target_id": region["region_id"],
            "target_revision_id": document["revision_id"],
        },
        headers={"X-Actor-Party-Id": _BROAD_VIEW_PARTY_ID},
    )
    assert broad_response.status_code == 200, broad_response.text
    broad_body = broad_response.json()

    limited_response = await client.get(
        "/api/v1/backlinks",
        params={
            "target_id": region["region_id"],
            "target_revision_id": document["revision_id"],
        },
        headers={"X-Actor-Party-Id": _LIMITED_VIEW_PARTY_ID},
    )
    assert limited_response.status_code == 200, limited_response.text
    limited_body = limited_response.json()

    # Broad view sees the inbound Supports Relationship from the Finding.
    assert broad_body["response_size"] >= 1
    assert any(
        entry["source_id"] == finding["finding_id"]
        and entry["relationship_type"] == "Supports"
        for entry in broad_body["entries"]
    ), broad_body

    # Limited view, missing view authority on the Finding scope, gets
    # an empty authorized projection (Property 4 non-leakage). Both
    # responses have the same response shape — only the visible
    # entries differ.
    assert limited_body["response_size"] == 0
    assert limited_body["entries"] == []
    assert limited_body["next_cursor"] is None
    # The authorized projection is a strict subset of (or equal to) the
    # broader projection — the limited Party never sees a Relationship
    # the broad Party cannot.
    limited_ids = {entry["relationship_id"] for entry in limited_body["entries"]}
    broad_ids = {entry["relationship_id"] for entry in broad_body["entries"]}
    assert limited_ids <= broad_ids


# ---------------------------------------------------------------------------
# Demonstration 2 — One linear Trail (Requirement 9.1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_one_linear_trail(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """POSTing a five-step Trail and GETting it back round-trips byte-equivalently.

    Seeds the full pipeline through the HTTP surface, POSTs a Trail
    whose steps walk the five pipeline stages in order (Document
    Revision → Region Occurrence → Finding Revision → Recommendation
    Revision → Decision), then GETs the Trail Revision back and
    asserts the persisted ordinals, target kinds, and ``Pinned``
    selection mode survive the round trip — the persistence contract
    AD-WS-12 enforces.
    """
    document = await _create_document(client)
    region = await _create_region(
        client, document["resource_id"], document["revision_id"]
    )
    finding = await _create_finding(
        client, region["region_id"], document["revision_id"]
    )
    recommendation = await _create_recommendation(client, finding["finding_id"])
    decision_status, decision = await _create_decision(
        client,
        recommendation_id=recommendation["recommendation_id"],
        recommendation_revision_id=recommendation["recommendation_revision_id"],
        deciding_party_id=_AUTHOR_PARTY_ID,
    )
    assert decision_status == 201, decision

    trail_body = {
        "purpose": "Walk the slice from evidence to authorized decision.",
        "audience_id": "pilot/team-a",
        "ordering_rationale": "Pipeline order.",
        "authoring_party_id": _AUTHOR_PARTY_ID,
        "scope": _SCOPE,
        "steps": [
            {
                "ordinal": 1,
                "target_kind": "document_revision",
                "target_id": document["resource_id"],
                "target_revision_id": document["revision_id"],
                "annotation": "The source document.",
            },
            {
                "ordinal": 2,
                "target_kind": "region_occurrence",
                "target_id": document["revision_id"],
                "region_id": region["region_id"],
                "annotation": "The cited region.",
            },
            {
                "ordinal": 3,
                "target_kind": "finding_revision",
                "target_id": finding["finding_id"],
                "target_revision_id": finding["finding_revision_id"],
                "annotation": "The supporting finding.",
            },
            {
                "ordinal": 4,
                "target_kind": "recommendation_revision",
                "target_id": recommendation["recommendation_id"],
                "target_revision_id": recommendation["recommendation_revision_id"],
                "annotation": "The recommendation.",
            },
            {
                "ordinal": 5,
                "target_kind": "decision",
                "target_id": decision["decision_id"],
                "annotation": "The authorized decision.",
            },
        ],
    }
    post_response = await client.post(
        "/api/v1/trails",
        json=trail_body,
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert post_response.status_code == 201, post_response.text
    posted = post_response.json()
    assert _CANONICAL_UUID7_REGEX.match(posted["trail_id"])
    assert _CANONICAL_UUID7_REGEX.match(posted["trail_revision_id"])

    # Round-trip: GET the persisted Trail Revision and assert the five
    # ordered steps survived byte-equivalently.
    get_response = await client.get(
        f"/api/v1/trails/{posted['trail_id']}/revisions/"
        f"{posted['trail_revision_id']}",
        headers={"X-Actor-Party-Id": _AUTHOR_PARTY_ID},
    )
    assert get_response.status_code == 200, get_response.text
    fetched = get_response.json()

    assert fetched["trail_id"] == posted["trail_id"]
    assert fetched["trail_revision_id"] == posted["trail_revision_id"]
    assert fetched["purpose"] == trail_body["purpose"]
    assert fetched["audience_id"] == trail_body["audience_id"]
    assert len(fetched["steps"]) == 5
    assert [step["ordinal"] for step in fetched["steps"]] == [1, 2, 3, 4, 5]
    assert [step["target_kind"] for step in fetched["steps"]] == [
        "document_revision",
        "region_occurrence",
        "finding_revision",
        "recommendation_revision",
        "decision",
    ]
    for step in fetched["steps"]:
        assert step["selection_mode"] == "Pinned"


# ---------------------------------------------------------------------------
# Demonstration 3 — Omission-aware provenance (Requirements 10.4, 10.6).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_omission_aware_provenance(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """Provenance responses surface gap descriptors from the manifest.

    Submits a Decision with two explicit Omission Entries (one
    ``unavailable`` and one ``intentional``), then GETs the Decision's
    provenance and asserts the gap descriptors are surfaced in the
    response. The ``unavailable`` category produces a non-empty
    ``gap_descriptors`` entry per the slice-default-2026 Disclosure
    Policy seeded by ``create_app`` (Requirement 10.4 — completeness
    cues are observable to authorized callers).
    """
    services: SliceServices = composed_app.state.services
    engine: Engine = composed_app.state.engine

    document = await _create_document(client)
    region = await _create_region(
        client, document["resource_id"], document["revision_id"]
    )
    finding = await _create_finding(
        client, region["region_id"], document["revision_id"]
    )
    recommendation = await _create_recommendation(client, finding["finding_id"])

    # Submit the Decision with two Omission Entries. The
    # ``unavailable`` category drives ``is_complete = 0`` on the
    # manifest (Requirement 10.3) and surfaces a gap descriptor in
    # the Decision provenance response.
    omitted_resource_id = "00000000-0000-7000-8000-000000ffff01"
    decision_status, decision = await _create_decision(
        client,
        recommendation_id=recommendation["recommendation_id"],
        recommendation_revision_id=recommendation["recommendation_revision_id"],
        deciding_party_id=_AUTHOR_PARTY_ID,
        omissions=[
            {
                "excluded_source_id": omitted_resource_id,
                "category": "unavailable",
                "rationale": (
                    "A second supporting finding was unavailable at decision "
                    "time; recorded so downstream readers see the gap."
                ),
            },
            {
                "excluded_source_id": "00000000-0000-7000-8000-000000ffff02",
                "category": "intentional",
                "rationale": (
                    "A tangential corpus was intentionally excluded as out "
                    "of scope for the pilot horizon."
                ),
            },
        ],
    )
    assert decision_status == 201, decision
    assert len(decision["omission_entry_ids"]) == 2

    # Grant the broad-view Party view authority on every scope the
    # chain touches so the provenance response is fully populated;
    # without this the chain would be redacted and gap descriptors
    # alone would not let us assert on the underlying nodes.
    for scope in (
        _SCOPE,
        recommendation["recommendation_id"],
        finding["finding_id"],
        document["resource_id"],
    ):
        _assign_role(
            services.authorization_service,
            engine,
            party_id=_BROAD_VIEW_PARTY_ID,
            role_name="broad-reviewer",
            authorities=("view",),
            scope=scope,
        )

    response = await client.get(
        f"/api/v1/decisions/{decision['decision_id']}/provenance",
        headers={"X-Actor-Party-Id": _BROAD_VIEW_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # Gap descriptors are surfaced (Requirement 10.4). The slice-
    # default-2026 Disclosure Policy reports the omission categories
    # the Decision's manifest recorded, so the response carries at
    # least the ``unavailable`` entry. (``intentional`` may be filtered
    # by the policy under the "intentional omissions remain authoritative"
    # rule, which is why we assert on the unavailable category by name
    # rather than the total count.)
    assert isinstance(body["gap_descriptors"], list)
    categories = {descriptor["category"] for descriptor in body["gap_descriptors"]}
    assert "unavailable" in categories, body["gap_descriptors"]

    # The policy fields are populated because ``create_app`` seeded the
    # slice-default-2026 Disclosure Policy on startup.
    assert body["policy_id"] == "slice-default-2026"
    assert body["policy_name"] == "slice-default-2026"


# ---------------------------------------------------------------------------
# Demonstration 4 — Denied unauthorized Decision (Requirements 7.1, 7.4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_denied_unauthorized_decision(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """An unauthorized Party gets the AD-WS-9 indistinguishable denial.

    Seeds the pipeline up to a Recommendation, then submits a Decision
    under an identity that holds no role assignment at all. The
    response is the AD-WS-9 indistinguishable shape: exactly three
    fields — ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` — and nothing about the target Recommendation
    Revision, the would-be Decision Identity, or any other observable
    that could distinguish "denied" from "non-existent".

    The test also asserts that no Decision row, ``Addresses``
    Relationship, Provenance Manifest, or consequential audit row was
    persisted — the caller's transaction rolled back atomically.
    """
    engine: Engine = composed_app.state.engine

    document = await _create_document(client)
    region = await _create_region(
        client, document["resource_id"], document["revision_id"]
    )
    finding = await _create_finding(
        client, region["region_id"], document["revision_id"]
    )
    recommendation = await _create_recommendation(client, finding["finding_id"])

    # Deliberately submit the Decision under the unauthorized Party,
    # who holds no role assignment for the scope. The wired
    # AuthorizationService denies with ``reason_code = no-role-assignment``.
    decision_status, body = await _create_decision(
        client,
        recommendation_id=recommendation["recommendation_id"],
        recommendation_revision_id=recommendation["recommendation_revision_id"],
        deciding_party_id=_UNAUTHORIZED_PARTY_ID,
    )

    assert decision_status == 403, body
    detail = body["detail"]
    # AD-WS-9 indistinguishable response shape — *exactly* three fields.
    assert set(detail.keys()) == {
        "generic_denial_indicator",
        "reason_code",
        "correlation_id",
    }, detail
    assert detail["generic_denial_indicator"] == "denied"
    assert detail["reason_code"] == "no-role-assignment"
    assert _CANONICAL_UUID7_REGEX.match(detail["correlation_id"]), detail

    # No partial persistence: the caller's transaction rolled back, so
    # the Decisions, Relationships (Addresses-only filter), Provenance
    # Manifests (subject_kind='decision'), and consequential audit rows
    # all remain empty. The Denial Record itself was written in a
    # separate transaction so it survives the rollback (Requirement 7.6).
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
        decision_manifest_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Provenance_Manifests "
                "WHERE subject_kind = 'decision'"
            )
        ).scalar_one()
        denial_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Audit_Records "
                "WHERE outcome = 'deny' AND actor_party_id = :pid"
            ),
            {"pid": _UNAUTHORIZED_PARTY_ID},
        ).scalar_one()
    assert decision_count == 0
    assert addresses_count == 0
    assert decision_manifest_count == 0
    assert denial_count >= 1


# ---------------------------------------------------------------------------
# Demonstration 5 — Navigation back to exact Evidence (Requirement 11.1).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demo_navigation_back_to_exact_evidence(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """The Decision → Document chain returns byte-equivalent bounded text.

    Seeds the full pipeline, finalizes a Decision against the
    Recommendation, then GETs the Decision provenance under a Party
    holding view authority on every stage. The test asserts:

    1. The chain spans the five expected node kinds (Decision,
       Recommendation Revision, one Finding, one Region Occurrence,
       one Document Revision).
    2. The Region Occurrence node carries a ``bounded_text`` field
       whose base64-decoded bytes equal ``_DOC_CONTENT[start:end]``
       (Requirement 11.2 byte-equivalence).
    3. The Document Revision node references the resource and revision
       identifiers the test seeded with.
    """
    services: SliceServices = composed_app.state.services
    engine: Engine = composed_app.state.engine

    document = await _create_document(client)
    region = await _create_region(
        client, document["resource_id"], document["revision_id"]
    )
    finding = await _create_finding(
        client, region["region_id"], document["revision_id"]
    )
    recommendation = await _create_recommendation(client, finding["finding_id"])
    decision_status, decision = await _create_decision(
        client,
        recommendation_id=recommendation["recommendation_id"],
        recommendation_revision_id=recommendation["recommendation_revision_id"],
        deciding_party_id=_AUTHOR_PARTY_ID,
    )
    assert decision_status == 201, decision

    # Grant the navigating Party view authority on every scope the
    # chain touches; without this the provenance response would
    # collapse to a 404 per Requirement 11.7.
    for scope in (
        _SCOPE,
        recommendation["recommendation_id"],
        finding["finding_id"],
        document["resource_id"],
    ):
        _assign_role(
            services.authorization_service,
            engine,
            party_id=_BROAD_VIEW_PARTY_ID,
            role_name="broad-reviewer",
            authorities=("view",),
            scope=scope,
        )

    response = await client.get(
        f"/api/v1/decisions/{decision['decision_id']}/provenance",
        headers={"X-Actor-Party-Id": _BROAD_VIEW_PARTY_ID},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The chain spans the five expected node kinds.
    assert body["decision"]["decision_id"] == decision["decision_id"]
    assert body["decision"]["outcome"] == "Accept"
    assert body["recommendation_revision"]["recommendation_id"] == (
        recommendation["recommendation_id"]
    )
    assert body["recommendation_revision"]["kind"] == "recommendation_revision"
    assert len(body["findings"]) == 1
    assert body["findings"][0]["finding_id"] == finding["finding_id"]
    assert len(body["region_occurrences"]) == 1
    assert len(body["document_revisions"]) == 1

    # Byte-equivalence: the bounded_text round-trips to the exact span
    # of the resolved Document Revision (Requirement 11.2).
    region_node = body["region_occurrences"][0]
    decoded = base64.b64decode(region_node["bounded_text"])
    assert decoded == _EXPECTED_SPAN_BYTES
    assert region_node["start_offset_bytes"] == _DOC_SPAN_START
    assert region_node["end_offset_bytes"] == _DOC_SPAN_END
    # Digest of the returned bytes matches the persisted Region digest.
    assert (
        region_node["span_content_digest_sha256"]
        == hashlib.sha256(_EXPECTED_SPAN_BYTES).hexdigest()
    )

    # The Document Revision node references the seeded identifiers and
    # the persisted content digest matches the SHA-256 of _DOC_CONTENT.
    document_node = body["document_revisions"][0]
    assert document_node["resource_id"] == document["resource_id"]
    assert document_node["revision_id"] == document["revision_id"]
    assert document_node["content_digest_sha256"] == _EXPECTED_DOC_DIGEST
