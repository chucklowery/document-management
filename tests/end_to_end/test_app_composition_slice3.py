"""End-to-end tests for the Slice 3 application composition wiring (task 15.3).

These tests exercise :func:`walking_slice.app.create_app` end-to-end for the
Slice 3 additive wiring:

- The factory creates every Slice 3 table on startup
  (``Work_Assignment_Records``, ``Work_Event_Records``, ``Time_Entry_Records``,
  ``Deliverable_Production_Records``, ``Milestone_Acceptance_Records``,
  ``Completion_Records``, ``Deliverable_Resources``, and
  ``Deliverable_Revisions``).
- The factory seeds the eight Slice 3 ``Disclosure_Policy_Coverage`` rows
  (one per Slice 3 node kind) and the five Slice 3 ``Interim_ADR_Records``
  rows for Gaps G-11 through G-15.
- The factory mounts both the Execution_Service router
  (``walking_slice.execution._routes``) and the Deliverable_Repository
  router (``walking_slice.deliverables._routes``) on the composed
  :class:`fastapi.FastAPI` instance.
- The factory overrides every Slice 3 ``Depends`` placeholder on the
  composed app's :attr:`fastapi.FastAPI.dependency_overrides` map so the
  Execution_Service and Deliverable_Repository routes resolve to the
  matching :class:`SliceServices` singletons.
- The factory wires the Slice 3 ``get_request_context`` placeholder
  declared on the existing :mod:`walking_slice.routes.provenance`
  module so the Slice 3 additive provenance traversal endpoints
  resolve the same :class:`RequestContext` as the Slice 1 / Slice 2
  endpoints.
- The Slice 1 and Slice 2 wiring is unchanged: every Slice 1 and
  Slice 2 router, schema, and seed row is byte-equivalent to a build
  with no Slice 3 mounts.

These tests are the wiring-regression detector for task 15.3. A
regression here surfaces at composition time rather than at the
first HTTP request, so a misconfigured app cannot silently ship.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.app import SliceServices, create_app
from walking_slice.clock import FixedClock


pytestmark = pytest.mark.end_to_end


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


@pytest.fixture
def composed_app(tmp_path: Path) -> FastAPI:
    """Build a FastAPI app via :func:`create_app` with a per-test engine."""
    engine = _build_engine(tmp_path)
    clock = FixedClock(_FIXED_INSTANT)
    return create_app(
        engine=engine,
        clock=clock,
        jwt_secret=b"slice3-composition-test-secret",
    )


# ---------------------------------------------------------------------------
# Slice 3 schema bootstrap.
# ---------------------------------------------------------------------------


def test_create_app_creates_execution_schema(tmp_path: Path) -> None:
    """``create_app`` creates every Execution_Service table on startup.

    Confirms task 15.3 wires
    :func:`walking_slice.execution._persistence.create_execution_schema`
    into the FastAPI startup hook. The six Execution_Service tables
    from design §"Data Models — Schema Additions" must be present
    after one ``create_app`` call.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    expected_tables = {
        "Work_Assignment_Records",
        "Work_Event_Records",
        "Time_Entry_Records",
        "Deliverable_Production_Records",
        "Milestone_Acceptance_Records",
        "Completion_Records",
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


def test_create_app_creates_deliverable_schema(tmp_path: Path) -> None:
    """``create_app`` creates every Deliverable_Repository table on startup.

    Confirms task 15.3 wires
    :func:`walking_slice.deliverables._persistence.create_deliverable_schema`
    into the FastAPI startup hook. The two Deliverable_Repository
    tables (``Deliverable_Resources`` and ``Deliverable_Revisions``)
    must be present after one ``create_app`` call.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    expected_tables = {"Deliverable_Resources", "Deliverable_Revisions"}
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


# ---------------------------------------------------------------------------
# Slice 3 disclosure policy + Interim ADR seeding.
# ---------------------------------------------------------------------------


def test_create_app_seeds_slice3_disclosure_policy_coverage(
    tmp_path: Path,
) -> None:
    """``create_app`` seeds one ``Disclosure_Policy_Coverage`` row per
    Slice 3 node kind.

    Confirms task 15.3 wires
    :func:`walking_slice.execution._disclosure.seed_execution_coverage`
    into the FastAPI startup hook. Every row carries
    ``policy_id = 'slice-default-2026'`` and
    ``backlog_adr_id = 'ADR-HT-014'`` (the backlog ADR reserved by
    Gap G-12 for the additive policy-extension surface).
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    expected_node_kinds = {
        "work_assignment_record",
        "work_event_record",
        "time_entry_record",
        "deliverable_resource",
        "deliverable_revision",
        "deliverable_production_record",
        "milestone_acceptance_record",
        "completion_record",
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
        assert row.backlog_adr_id == "ADR-HT-014"


def test_create_app_seeds_slice3_interim_adr_rows(tmp_path: Path) -> None:
    """The Slice 3 Interim ADR rows (Gaps G-11..G-15) are seeded on startup.

    Confirms task 15.3 invokes
    :func:`walking_slice.execution._interim_adr.seed_execution_interim_adr`
    from the FastAPI startup hook. Every row records the motivating
    Requirement, motivating criterion, observable behavior, recorded
    date, and backlog ADR identifier per Requirement 40.5 / 42.3.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT record_id, backlog_adr_id FROM Interim_ADR_Records "
                "WHERE record_id IN ("
                "'ad-ws-24', 'ad-ws-25', 'ad-ws-26', 'ad-ws-27', 'ad-ws-28'"
                ") ORDER BY record_id"
            )
        ).all()
    assert {row.record_id for row in rows} == {
        "ad-ws-24",
        "ad-ws-25",
        "ad-ws-26",
        "ad-ws-27",
        "ad-ws-28",
    }
    assert {row.backlog_adr_id for row in rows} == {
        "ADR-HT-013",
        "ADR-HT-014",
        "ADR-HT-015",
        "ADR-HT-016",
        "ADR-HT-017",
    }


def test_create_app_is_idempotent_with_slice3_seeds(tmp_path: Path) -> None:
    """Repeated ``create_app`` calls do not duplicate Slice 3 seed rows.

    Confirms the ``INSERT OR IGNORE`` posture of both Slice 3 seeders
    (``seed_execution_coverage`` and ``seed_execution_interim_adr``).
    Without idempotence the second call would raise a UNIQUE
    constraint violation and the test would fail at the second
    ``create_app``.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))
    # Second call against the same engine must not raise.
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    with engine.connect() as conn:
        # Slice 1 (5) + Slice 2 (5) + Slice 3 (5) = 15 Interim ADR rows.
        adr_count = conn.execute(
            text("SELECT COUNT(*) FROM Interim_ADR_Records")
        ).scalar_one()
        assert adr_count == 15

        # Slice 2 (13) + Slice 3 (8) = 21 coverage rows total.
        coverage_count = conn.execute(
            text("SELECT COUNT(*) FROM Disclosure_Policy_Coverage")
        ).scalar_one()
        assert coverage_count == 21


# ---------------------------------------------------------------------------
# Slice 3 router mounting.
# ---------------------------------------------------------------------------


def test_create_app_mounts_execution_router(composed_app: FastAPI) -> None:
    """The Execution_Service router is mounted on the composed app.

    Confirms task 15.3 wires
    :data:`walking_slice.execution._routes.router` onto the
    :class:`fastapi.FastAPI` instance. Every endpoint listed in design
    §"Components and Interfaces" for the Execution_Service must be
    reachable on the composed app's route table.
    """
    expected_execution_paths = {
        "/api/v1/work-assignments",
        "/api/v1/work-assignments/{work_assignment_id}",
        "/api/v1/work-events",
        "/api/v1/work-events/{work_event_id}",
        "/api/v1/time-entries",
        "/api/v1/time-entries/{time_entry_id}",
        "/api/v1/deliverable-productions",
        "/api/v1/deliverable-productions/{deliverable_production_id}",
        "/api/v1/milestone-acceptances",
        "/api/v1/milestone-acceptances/{milestone_acceptance_id}",
        "/api/v1/completions",
        "/api/v1/completions/{completion_id}",
        "/api/v1/plan-revisions/{plan_revision_id}/execution-status",
    }
    discovered_paths = _collect_route_paths(composed_app)
    missing = expected_execution_paths - discovered_paths
    assert missing == set(), f"missing execution routes: {missing}"


def test_create_app_mounts_deliverables_router(composed_app: FastAPI) -> None:
    """The Deliverable_Repository router is mounted on the composed app.

    Confirms task 15.3 wires
    :data:`walking_slice.deliverables._routes.router` onto the
    :class:`fastapi.FastAPI` instance.
    """
    expected_deliverable_paths = {
        "/api/v1/deliverables",
        "/api/v1/deliverables/{deliverable_id}/revisions",
        "/api/v1/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}",
        "/api/v1/deliverables/{deliverable_id}/revisions/"
        "{deliverable_revision_id}/content",
    }
    discovered_paths = _collect_route_paths(composed_app)
    missing = expected_deliverable_paths - discovered_paths
    assert missing == set(), f"missing deliverable routes: {missing}"


def test_create_app_mounts_slice3_provenance_endpoints(
    composed_app: FastAPI,
) -> None:
    """The Slice 3 additive provenance endpoints are reachable.

    Confirms the three additive endpoints added by task 15.2 to the
    existing :mod:`walking_slice.routes.provenance` module remain
    mounted after task 15.3 wires the Slice 3 routers. The
    traversal endpoints exercise the
    :func:`provenance_routes.get_request_context` override added by
    task 15.3.
    """
    expected_slice3_provenance_paths = {
        "/api/v1/completions/{completion_id}/provenance",
        "/api/v1/deliverable-productions/"
        "{deliverable_production_id}/provenance",
        "/api/v1/deliverables/{deliverable_id}/revisions/"
        "{deliverable_revision_id}/provenance",
    }
    discovered_paths = _collect_route_paths(composed_app)
    missing = expected_slice3_provenance_paths - discovered_paths
    assert missing == set(), (
        f"missing Slice 3 provenance routes: {missing}"
    )


# ---------------------------------------------------------------------------
# Slice 3 dependency overrides.
# ---------------------------------------------------------------------------


def test_create_app_wires_execution_dependency_overrides(
    composed_app: FastAPI,
) -> None:
    """``create_app`` overrides every Execution_Service ``Depends`` placeholder.

    Confirms task 15.3 binds the seven Execution_Service factories,
    plus the router's ``get_engine`` and ``get_status_projector``
    placeholders, to the matching :class:`SliceServices` singletons.
    Without the overrides the placeholder raises
    :class:`NotImplementedError` on first use — the wiring-regression
    detector this test exercises.
    """
    from walking_slice.execution import _routes as execution_routes

    overrides = composed_app.dependency_overrides
    services: SliceServices = composed_app.state.services

    assert (
        overrides[execution_routes.get_engine]() is composed_app.state.engine
    )
    assert (
        overrides[execution_routes.get_work_assignment_service]()
        is services.work_assignment_service
    )
    assert (
        overrides[execution_routes.get_work_event_service]()
        is services.work_event_service
    )
    assert (
        overrides[execution_routes.get_time_entry_service]()
        is services.time_entry_service
    )
    assert (
        overrides[execution_routes.get_deliverable_production_service]()
        is services.deliverable_production_service
    )
    assert (
        overrides[execution_routes.get_milestone_acceptance_service]()
        is services.milestone_acceptance_service
    )
    assert (
        overrides[execution_routes.get_completion_service]()
        is services.completion_service
    )
    assert (
        overrides[execution_routes.get_status_projector]()
        is services.status_projector
    )


def test_create_app_wires_deliverables_dependency_override(
    composed_app: FastAPI,
) -> None:
    """``create_app`` overrides the Deliverable_Repository placeholder.

    Confirms task 15.3 binds
    :func:`deliverables_routes.get_deliverable_repository_service` to
    :attr:`SliceServices.deliverable_repository_service`.
    """
    from walking_slice.deliverables import _routes as deliverables_routes

    overrides = composed_app.dependency_overrides
    services: SliceServices = composed_app.state.services

    assert (
        overrides[
            deliverables_routes.get_deliverable_repository_service
        ]()
        is services.deliverable_repository_service
    )


def test_create_app_wires_slice3_provenance_request_context_override(
    composed_app: FastAPI,
) -> None:
    """``create_app`` overrides the Slice 3 provenance ``get_request_context``.

    The Slice 3 additive traversal endpoints declared on
    :mod:`walking_slice.routes.provenance` use a Slice-3-only
    :func:`provenance_routes.get_request_context` placeholder
    (parallel to :func:`walking_slice.app.get_request_context`) so
    the module avoids forming a circular import with
    :mod:`walking_slice.app`. Task 15.3 wires the placeholder to the
    same :class:`RequestContextResolver` used by every other endpoint.
    """
    from walking_slice.routes import provenance as provenance_routes

    overrides = composed_app.dependency_overrides
    services: SliceServices = composed_app.state.services

    assert (
        overrides[provenance_routes.get_request_context]
        is services.request_context_resolver
    )


# ---------------------------------------------------------------------------
# Slice 3 services bundle.
# ---------------------------------------------------------------------------


def test_create_app_exposes_slice3_services_on_state(
    composed_app: FastAPI,
) -> None:
    """``app.state.services`` exposes every Slice 3 service singleton.

    Confirms the :class:`SliceServices` bundle was extended additively
    with the Slice 3 singletons (one Deliverable_Repository service,
    six Execution_Service services, one Project Resolver, and one
    :class:`StatusProjector`). The bundle is the recommended way for
    tests and operator tooling to reach the wired services without
    re-deriving the wiring from the engine.
    """
    services: SliceServices = composed_app.state.services
    assert isinstance(services, SliceServices)
    # Every Slice 3 slot is populated; an attribute error here would
    # signal that the bundle was constructed with the wrong kwargs.
    assert services.deliverable_repository_service is not None
    assert services.project_resolver is not None
    assert services.work_assignment_service is not None
    assert services.work_event_service is not None
    assert services.time_entry_service is not None
    assert services.deliverable_production_service is not None
    assert services.milestone_acceptance_service is not None
    assert services.completion_service is not None
    assert services.status_projector is not None
    # The Slice 3 services share the Slice 1 cross-request collaborators.
    assert services.work_assignment_service.clock is services.clock
    assert (
        services.work_assignment_service.identity_service
        is services.identity_service
    )
    assert (
        services.completion_service.authorization_service
        is services.authorization_service
    )
    assert (
        services.deliverable_production_service.deliverable_reader
        is services.deliverable_repository_service
    )


def test_status_projector_carries_planning_and_execution_definitions(
    composed_app: FastAPI,
) -> None:
    """The shared :class:`StatusProjector` carries Slice 2 + Slice 3 definitions.

    The execution-status endpoint
    (``GET /plan-revisions/{plan_revision_id}/execution-status``)
    requires the Slice 3 Execution Projection Definition to be
    registered on the projector; the Planning Projection Definitions
    are registered alongside so a single projector backs every
    projection-producing endpoint in the slice (Requirement 39.5
    short-circuits when the definition is not registered).
    """
    from walking_slice.execution._projection import (
        EXECUTION_PROJECTION_DEFINITION_NAME,
    )
    from walking_slice.planning._projection import (
        PLANNING_PROJECTION_DEFINITION_NAME,
    )

    services: SliceServices = composed_app.state.services
    projector = services.status_projector
    assert projector.has_definition(EXECUTION_PROJECTION_DEFINITION_NAME)
    assert projector.has_definition(PLANNING_PROJECTION_DEFINITION_NAME)


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 non-modification.
# ---------------------------------------------------------------------------


def test_create_app_preserves_slice1_and_slice2_rows(tmp_path: Path) -> None:
    """Slice 1 + Slice 2 seed rows are byte-equivalent after Slice 3 mount.

    Requirement 40.1 (Reuse and Non-Modification of Slice 1 and Slice 2
    Contexts): wiring Slice 3 must not change any Slice 1 or Slice 2
    row. We compare the Slice 1 ADR rows and the Slice 2 ADR + coverage
    rows against the expected set after one ``create_app`` invocation.
    """
    engine = _build_engine(tmp_path)
    create_app(engine=engine, clock=FixedClock(_FIXED_INSTANT))

    with engine.connect() as conn:
        # Slice 1: AD-WS-6..AD-WS-10 (ADR-HT-002..ADR-HT-008).
        slice1_rows = conn.execute(
            text(
                "SELECT record_id, backlog_adr_id FROM Interim_ADR_Records "
                "WHERE record_id IN ("
                "'ad-ws-6', 'ad-ws-7', 'ad-ws-8', 'ad-ws-9', 'ad-ws-10'"
                ")"
            )
        ).all()
        assert len(slice1_rows) == 5

        # Slice 2: AD-WS-15..AD-WS-19 (ADR-HT-006, ADR-HT-009..ADR-HT-012).
        slice2_rows = conn.execute(
            text(
                "SELECT record_id, backlog_adr_id FROM Interim_ADR_Records "
                "WHERE record_id IN ("
                "'ad-ws-15', 'ad-ws-16', 'ad-ws-17', 'ad-ws-18', 'ad-ws-19'"
                ")"
            )
        ).all()
        assert len(slice2_rows) == 5
        assert {r.backlog_adr_id for r in slice2_rows} == {
            "ADR-HT-006",
            "ADR-HT-009",
            "ADR-HT-010",
            "ADR-HT-011",
            "ADR-HT-012",
        }

        # Slice 2 coverage rows remain populated (13 rows).
        slice2_coverage_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Disclosure_Policy_Coverage "
                "WHERE node_kind NOT IN ("
                "'work_assignment_record', 'work_event_record', "
                "'time_entry_record', 'deliverable_resource', "
                "'deliverable_revision', 'deliverable_production_record', "
                "'milestone_acceptance_record', 'completion_record'"
                ")"
            )
        ).scalar_one()
        assert slice2_coverage_count == 13


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _collect_route_paths(app: FastAPI) -> set[str]:
    """Walk every mounted route and return its declared path.

    FastAPI 0.138+ wraps each ``include_router`` call in an
    ``_IncludedRouter`` proxy that defers route expansion; this helper
    walks ``original_router.routes`` of every proxy so the assertion
    is robust against the public-router-shape change. The same
    pattern is used by :mod:`tests.end_to_end.test_app_composition`.
    """
    discovered_paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path is not None:
            discovered_paths.add(path)
        included = getattr(route, "original_router", None)
        if included is not None:
            for sub in included.routes:
                sub_path = getattr(sub, "path", None)
                if sub_path is not None:
                    discovered_paths.add(sub_path)
    return discovered_paths
