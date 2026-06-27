"""End-to-end tests for the application composition layer (task 15.2).

These tests exercise :func:`walking_slice.app.create_app` end-to-end:

- The factory builds a :class:`fastapi.FastAPI` instance with every
  ``routes/*.py`` router mounted.
- Startup runs :func:`walking_slice.persistence.create_schema`,
  :func:`walking_slice.interim_adr.seed`, and
  :func:`walking_slice.disclosure.seed` so a fresh database is fully
  bootstrapped after one call to ``create_app``.
- The healthcheck endpoint reports ``200 {"status": "ok"}``.
- A round-trip through ``POST /api/v1/documents`` exercises the
  ``EvidenceRepository`` wiring (the simplest write endpoint that
  touches every cross-cutting service: identifier minting, audit
  appending, transaction management).
- The ``slice-default-2026`` :class:`DisclosurePolicy` and the AD-WS-6
  ``Interim_ADR_Records`` row are observable in the database.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.app import (
    DATABASE_URL_ENV,
    DEFAULT_DATABASE_URL,
    SliceServices,
    create_app,
    create_default_engine,
)
from walking_slice.clock import FixedClock


pytestmark = pytest.mark.end_to_end


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_FIXED_INSTANT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _build_engine(tmp_path: Path) -> Engine:
    """Construct an isolated SQLite engine for the test.

    The engine is freshly created per test so cross-test state cannot
    leak through the shared default ``walking_slice.db`` path. The
    pragmas are installed inline rather than via
    :func:`walking_slice.persistence.install_pragmas` so the test can
    verify that ``create_app`` itself also installs them on engines it
    is handed.
    """
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


def _seed_party(engine: Engine, party_id: str = _PARTY_ID) -> None:
    """Insert the contributing Party referenced by the round-trip test.

    ``create_app`` does not seed Parties — that is a domain concern, not a
    bootstrap concern — so the test seeds the single Party it needs
    before driving the HTTP layer.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Parties (party_id, kind, display_name, created_at)
                VALUES (:pid, 'person', 'Composition Test Party', :ts)
                """
            ),
            {"pid": party_id, "ts": "2026-01-01T00:00:00.000Z"},
        )


@pytest.fixture
def composed_app(tmp_path: Path) -> FastAPI:
    """Build a FastAPI app via :func:`create_app` with a per-test engine.

    The fixture pins the :class:`Clock` to :data:`_FIXED_INSTANT` so
    ``recorded_at`` values are byte-equivalent across runs, and pins
    the JWT secret to a constant so tests that exercise the bearer
    token surface (added in a later wave) reproduce.
    """
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    app = create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"composition-test-secret",
    )
    _seed_party(engine)
    return app


@pytest_asyncio.fixture
async def client(composed_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=composed_app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


# ---------------------------------------------------------------------------
# Healthcheck.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthcheck_returns_ok(client: AsyncClient) -> None:
    """``GET /healthz`` returns ``200 {"status": "ok"}``.

    Smoke test confirming the app composes, the lifespan starts cleanly,
    and the router mounting succeeded.
    """
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Startup bootstrap (Requirements 16.1, 16.3).
# ---------------------------------------------------------------------------


def test_create_app_creates_schema_and_seeds(tmp_path: Path) -> None:
    """``create_app`` creates the schema and seeds Interim ADRs + disclosure policy.

    Confirms Requirement 16.1 (schema bootstrap on startup) and
    Requirement 16.3 (Interim ADR records seeded for G-1..G-5) without
    going through the HTTP layer. The test inspects the database
    directly so a regression in the seed wiring fails here loudly
    rather than blocking on an HTTP timeout.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    with engine.connect() as conn:
        # AD-WS-6 row is present (lazy seed in evidence.py uses the same
        # primary key; the startup seed in interim_adr.py owns it here
        # because ``create_app`` runs it before any evidence write).
        adws6 = conn.execute(
            text(
                "SELECT backlog_adr_id, motivating_criterion "
                "FROM Interim_ADR_Records WHERE record_id = :rid"
            ),
            {"rid": "ad-ws-6"},
        ).first()
        assert adws6 is not None
        assert adws6.backlog_adr_id == "ADR-HT-003"

        # All ten G-1..G-10 rows are present: the Slice 1 seed inserts
        # ad-ws-6..ad-ws-10 (Gaps G-1..G-5 / backlog IDs ADR-HT-002,
        # ADR-HT-003, ADR-HT-004, ADR-HT-005, ADR-HT-008); the Slice 2
        # seed in :mod:`walking_slice.planning._interim_adr` additively
        # inserts ad-ws-15..ad-ws-19 (Gaps G-6..G-10 / backlog IDs
        # ADR-HT-006, ADR-HT-009, ADR-HT-010, ADR-HT-011, ADR-HT-012);
        # the Slice 3 seed in
        # :mod:`walking_slice.execution._interim_adr` additively inserts
        # ad-ws-24..ad-ws-28 (Gaps G-11..G-15 / backlog IDs
        # ADR-HT-013..ADR-HT-017). The Slice 3-specific row contents are
        # asserted separately in
        # :mod:`tests.end_to_end.test_app_composition_slice3`.
        rows = conn.execute(
            text(
                "SELECT backlog_adr_id FROM Interim_ADR_Records "
                "ORDER BY record_id"
            )
        ).all()
        backlog_ids = {row.backlog_adr_id for row in rows}
        assert backlog_ids == {
            "ADR-HT-002",
            "ADR-HT-003",
            "ADR-HT-004",
            "ADR-HT-005",
            "ADR-HT-008",
            "ADR-HT-006",
            "ADR-HT-009",
            "ADR-HT-010",
            "ADR-HT-011",
            "ADR-HT-012",
            "ADR-HT-013",
            "ADR-HT-014",
            "ADR-HT-015",
            "ADR-HT-016",
            "ADR-HT-017",
        }

        # The slice-default-2026 disclosure policy was seeded.
        policy = conn.execute(
            text(
                "SELECT policy_id, policy_name FROM Disclosure_Policies "
                "WHERE policy_id = :pid"
            ),
            {"pid": "slice-default-2026"},
        ).first()
        assert policy is not None
        assert policy.policy_name == "slice-default-2026"


def test_create_app_is_idempotent(tmp_path: Path) -> None:
    """Repeated ``create_app`` calls against one engine do not duplicate seed rows.

    Confirms the ``INSERT OR IGNORE`` posture of both seeds. Without
    idempotence the second call would raise a UNIQUE constraint
    violation and the test would fail at the second ``create_app``.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))
    # Second call against the same engine must not raise.
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Interim_ADR_Records")
        ).scalar_one()
        # 5 Slice 1 rows (ad-ws-6..ad-ws-10) + 5 Slice 2 rows
        # (ad-ws-15..ad-ws-19) + 5 Slice 3 rows (ad-ws-24..ad-ws-28) —
        # see ``walking_slice.planning._interim_adr`` and
        # ``walking_slice.execution._interim_adr``.
        assert count == 15

        policy_count = conn.execute(
            text("SELECT COUNT(*) FROM Disclosure_Policies")
        ).scalar_one()
        assert policy_count == 1


def test_app_state_exposes_service_bundle(composed_app: FastAPI) -> None:
    """``app.state.services`` is the :class:`SliceServices` bundle.

    The bundle is the recommended way for tests and operator tooling
    to reach the wired services without re-deriving the wiring from
    the engine. This test guards that contract.
    """
    services = composed_app.state.services
    assert isinstance(services, SliceServices)
    assert services.engine is composed_app.state.engine
    # Every service slot is populated; an attribute error here would
    # signal that the bundle was constructed with the wrong kwargs.
    for slot in SliceServices.__slots__:
        assert getattr(services, slot) is not None


# ---------------------------------------------------------------------------
# Round-trip through the EvidenceRepository.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_document_round_trip(client: AsyncClient) -> None:
    """``POST /api/v1/documents`` succeeds against the composed app.

    Exercises the simplest write endpoint that touches every service in
    the composition: ``IdentityService`` mints the Resource and Revision
    identifiers, ``EvidenceRepository`` inserts both rows, and
    ``AuditLog`` appends the consequential row — all inside one
    transaction on the engine ``create_app`` wired up.
    """
    content = b"composition test bytes"
    payload = {
        "content_bytes": base64.b64encode(content).decode("ascii"),
        "contributing_party_id": _PARTY_ID,
        "authority": "authoritative",
    }
    response = await client.post("/api/v1/documents", json=payload)
    assert response.status_code == 201, response.text

    body = response.json()
    assert "resource_id" in body
    assert "revision_id" in body
    # The persisted digest matches the SHA-256 of the request bytes.
    import hashlib

    assert body["content_digest_sha256"] == hashlib.sha256(content).hexdigest()


@pytest.mark.asyncio
async def test_audit_row_appended_for_consequential_write(
    composed_app: FastAPI, client: AsyncClient
) -> None:
    """A successful document write appends one consequential audit row.

    Confirms that the ``AuditLog`` instance wired by ``create_app`` is
    actually reachable from the route handler (a wiring regression
    would leave ``Audit_Records`` empty after a successful 201).
    """
    content = b"audit-row check"
    payload = {
        "content_bytes": base64.b64encode(content).decode("ascii"),
        "contributing_party_id": _PARTY_ID,
        "authority": "authoritative",
    }
    response = await client.post("/api/v1/documents", json=payload)
    assert response.status_code == 201, response.text

    engine: Engine = composed_app.state.engine
    with engine.connect() as conn:
        audit_rows = conn.execute(
            text(
                "SELECT action_type, outcome FROM Audit_Records "
                "WHERE action_type = 'create.document_revision'"
            )
        ).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].outcome == "consequential"


# ---------------------------------------------------------------------------
# Default engine construction.
# ---------------------------------------------------------------------------


def test_create_default_engine_uses_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """:func:`create_default_engine` reads the database URL from the env var.

    Confirms the resolution order ``argument > env var > DEFAULT_DATABASE_URL``
    by setting the env var and observing the engine's URL.
    """
    sqlite_path = tmp_path / "from_env.sqlite"
    url = f"sqlite:///{sqlite_path.as_posix()}"
    monkeypatch.setenv(DATABASE_URL_ENV, url)

    engine = create_default_engine()
    try:
        assert str(engine.url) == url
    finally:
        engine.dispose()


def test_create_default_engine_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an argument or env var, the factory uses
    :data:`DEFAULT_DATABASE_URL`."""
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    engine = create_default_engine()
    try:
        assert str(engine.url) == DEFAULT_DATABASE_URL
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Slice 2 (Planning_Service) wiring.
#
# These tests confirm the task 15.2 (second-walking-slice) additive
# wiring: the planning router is mounted, the Slice 2 schema is created
# on startup, the additive Disclosure_Policy_Coverage rows are seeded,
# and the planning route ``Depends`` placeholders are overridden with
# the matching :class:`SliceServices` singletons.
# ---------------------------------------------------------------------------


def test_create_app_creates_planning_schema(tmp_path: Path) -> None:
    """``create_app`` creates every Slice 2 table on startup.

    Confirms the second-walking-slice task 15.2 wiring: the
    Planning_Service schema (Objectives, Intended Outcomes, Projects,
    Deliverable Expectations, Activity Plans, Plan Revisions, Plan
    Reviews, Plan Approvals, and their *_Revisions sibling tables,
    plus Disclosure_Policy_Coverage) is materialised before any
    request hits the app.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    expected_tables = {
        "Objectives",
        "Objective_Revisions",
        "Intended_Outcomes",
        "Intended_Outcome_Revisions",
        "Projects",
        "Project_Revisions",
        "Deliverable_Expectations",
        "Deliverable_Expectation_Revisions",
        "Activity_Plans",
        "Plan_Revisions",
        "Plan_Reviews",
        "Plan_Review_Revisions",
        "Plan_Approval_Records",
        "Disclosure_Policy_Coverage",
    }
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name IN ({names})".format(
                    names=", ".join(f"'{name}'" for name in expected_tables)
                )
            )
        ).all()
    actual_tables = {row.name for row in rows}
    assert actual_tables == expected_tables


def test_create_app_seeds_disclosure_policy_coverage(tmp_path: Path) -> None:
    """``create_app`` seeds one ``Disclosure_Policy_Coverage`` row per
    Slice 2 node kind.

    Confirms the second-walking-slice task 15.2 wiring of
    :func:`walking_slice.planning._disclosure.seed_planning_coverage`.
    Every row carries ``policy_id = 'slice-default-2026'`` and
    ``backlog_adr_id = 'ADR-HT-009'`` (the backlog ADR reserved by
    Gap G-7 for the additive policy-extension surface).

    The Slice 3 coverage rows (added by task 15.3) are additive and
    asserted separately in
    :mod:`tests.end_to_end.test_app_composition_slice3` — this test
    filters them out so a regression in the Slice 2 coverage seed
    fails here, not in the Slice 3 suite.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    expected_node_kinds = {
        "objective",
        "objective_revision",
        "intended_outcome",
        "intended_outcome_revision",
        "project",
        "project_revision",
        "deliverable_expectation",
        "deliverable_expectation_revision",
        "activity_plan",
        "plan_revision",
        "plan_review",
        "plan_review_revision",
        "plan_approval",
    }
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT node_kind, policy_id, backlog_adr_id "
                "FROM Disclosure_Policy_Coverage "
                "WHERE node_kind IN ({names}) "
                "ORDER BY node_kind".format(
                    names=", ".join(
                        f"'{name}'" for name in expected_node_kinds
                    )
                )
            )
        ).all()
    actual_kinds = {row.node_kind for row in rows}
    assert actual_kinds == expected_node_kinds
    for row in rows:
        assert row.policy_id == "slice-default-2026"
        assert row.backlog_adr_id == "ADR-HT-009"


def test_create_app_mounts_planning_router(composed_app: FastAPI) -> None:
    """The Planning_Service router is mounted on the composed app.

    Confirms the second-walking-slice task 15.2 wiring of
    :func:`walking_slice.app.create_app`: every Planning_Service
    endpoint listed in design §"Components and Interfaces" is
    reachable on the composed app's route table. FastAPI 0.138+
    wraps each ``include_router`` call in an ``_IncludedRouter``
    proxy that defers route expansion; this test walks the
    ``original_router.routes`` of every proxy so the assertion
    is robust against the public-router-shape change.
    """
    expected_planning_paths = {
        "/api/v1/objectives",
        "/api/v1/objectives/{objective_id}/revisions/{revision_id}",
        "/api/v1/intended-outcomes",
        "/api/v1/intended-outcomes/{intended_outcome_id}/revisions/{revision_id}",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/revisions/{revision_id}",
        "/api/v1/deliverable-expectations",
        "/api/v1/deliverable-expectations/"
        "{deliverable_expectation_id}/revisions/{revision_id}",
        "/api/v1/activity-plans",
        "/api/v1/activity-plans/{activity_plan_id}",
        "/api/v1/activity-plans/{activity_plan_id}/plan-revisions",
        "/api/v1/activity-plans/{activity_plan_id}/plan-revisions/{revision_id}",
        "/api/v1/plan-revisions/{plan_revision_id}/reviews",
        "/api/v1/plan-reviews/{plan_review_id}/revisions/{revision_id}",
        "/api/v1/plan-revisions/{plan_revision_id}/approvals",
        "/api/v1/plan-approvals/{plan_approval_id}",
        "/api/v1/plan-approvals/{plan_approval_id}/provenance",
    }

    discovered_paths: set[str] = set()
    for route in composed_app.routes:
        # Top-level Route / APIRoute.
        path = getattr(route, "path", None)
        if path is not None:
            discovered_paths.add(path)
        # FastAPI 0.138+ ``_IncludedRouter`` proxy; the wrapped router's
        # routes carry the prefixed paths.
        included = getattr(route, "original_router", None)
        if included is not None:
            for sub in included.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    discovered_paths.add(sub_path)

    missing = expected_planning_paths - discovered_paths
    assert missing == set(), f"missing planning routes: {missing}"


def test_create_app_wires_planning_dependency_overrides(
    composed_app: FastAPI,
) -> None:
    """``create_app`` overrides every Planning_Service ``Depends`` placeholder.

    Confirms the second-walking-slice task 15.2 wiring: the eight
    Planning_Service ``get_<service>`` factories, plus the planning
    router's ``get_engine`` and ``get_provenance_navigator``
    placeholders, are bound to the matching :class:`SliceServices`
    singletons on ``app.dependency_overrides``. Without the overrides
    the placeholder raises :class:`NotImplementedError` on first use,
    which is the wiring-regression detector this test exercises.
    """
    from walking_slice.planning import _routes as planning_routes

    overrides = composed_app.dependency_overrides
    services: SliceServices = composed_app.state.services

    # Engine and provenance navigator share the Slice 1 singletons.
    assert overrides[planning_routes.get_engine]() is composed_app.state.engine
    assert (
        overrides[planning_routes.get_provenance_navigator]()
        is services.provenance_navigator
    )

    # Each of the eight Planning_Service singletons is reachable
    # through its dedicated factory.
    assert overrides[planning_routes.get_objective_service]() is services.objective_service
    assert (
        overrides[planning_routes.get_intended_outcome_service]()
        is services.intended_outcome_service
    )
    assert overrides[planning_routes.get_project_service]() is services.project_service
    assert (
        overrides[planning_routes.get_deliverable_expectation_service]()
        is services.deliverable_expectation_service
    )
    assert (
        overrides[planning_routes.get_activity_plan_service]()
        is services.activity_plan_service
    )
    assert (
        overrides[planning_routes.get_plan_revision_service]()
        is services.plan_revision_service
    )
    assert (
        overrides[planning_routes.get_plan_review_service]()
        is services.plan_review_service
    )
    assert (
        overrides[planning_routes.get_plan_approval_service]()
        is services.plan_approval_service
    )


def test_create_app_seeds_slice2_interim_adr_rows(tmp_path: Path) -> None:
    """The Slice 2 Interim ADR rows (Gaps G-6..G-10) are seeded on startup.

    Confirms that :func:`walking_slice.planning._interim_adr.seed_planning_interim_adr`
    is invoked from the FastAPI startup hook (task 15.2). Every Slice 2
    row records the motivating Requirement, motivating criterion,
    observable behavior, recorded date, and backlog ADR identifier
    per Requirement 16.3 / 21.3.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT record_id, backlog_adr_id FROM Interim_ADR_Records "
                "WHERE record_id IN ("
                "'ad-ws-15', 'ad-ws-16', 'ad-ws-17', 'ad-ws-18', 'ad-ws-19'"
                ") ORDER BY record_id"
            )
        ).all()
    assert {row.record_id for row in rows} == {
        "ad-ws-15",
        "ad-ws-16",
        "ad-ws-17",
        "ad-ws-18",
        "ad-ws-19",
    }
    assert {row.backlog_adr_id for row in rows} == {
        "ADR-HT-006",
        "ADR-HT-009",
        "ADR-HT-010",
        "ADR-HT-011",
        "ADR-HT-012",
    }
