"""Shared pytest configuration for the first walking slice.

Provides the per-test SQLite file fixture mandated by design §"Testing Strategy"
("Database isolation: Each property and example test gets a fresh SQLite
database (file URL with unique suffix) seeded with a minimal Party + Role
set.") and the shared dependency-injection slots that subsequent tasks will
populate with their service implementations.

Hypothesis profiles are registered and selected here so that every test file
inherits the same configuration; per-test profile selection is driven by the
``HYPOTHESIS_PROFILE`` environment variable (default ``dev``) or by the
``--hypothesis-profile`` flag exposed by the Hypothesis pytest plugin.
"""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, Phase, Verbosity, settings
from hypothesis import core as _hypothesis_core
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AuthorizationService
from walking_slice.clock import Clock, FixedClock
from walking_slice.identity import IdentityService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.persistence import create_schema
from walking_slice.trails import TrailService

# ---------------------------------------------------------------------------
# Hypothesis profile registration.
#
# - `dev` is the local default: fewer cases, faster feedback.
# - `ci` matches Requirement 15.13 (>= 100 generated cases per property).
# - `debug` keeps shrinking aggressive and verbose for interactive triage.
#
# Selection precedence (highest first):
#   1. ``--hypothesis-profile=<name>`` on the pytest CLI (Hypothesis plugin).
#   2. ``HYPOTHESIS_PROFILE`` environment variable, loaded below.
#   3. ``dev`` fallback.
# ---------------------------------------------------------------------------

settings.register_profile(
    "dev",
    max_examples=50,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=100,
    deadline=2000,
)
settings.register_profile(
    "debug",
    max_examples=10,
    deadline=None,
    verbosity=Verbosity.verbose,
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


# ---------------------------------------------------------------------------
# Seed capture (Requirement 15.13 / Property 13 — task 15.6).
#
# Operational contract: the property-based test suite records the Hypothesis
# seed of every property test invocation to a build artifact so that
# replaying with the same seed reproduces identical pass/fail outcomes and
# identical minimal counterexamples (design §"Seed Capture").
#
# Seed precedence (highest first):
#   1. ``--hypothesis-seed=N`` on the pytest CLI (Hypothesis plugin sets
#      ``hypothesis.core.global_force_seed`` from ``tryfirst`` configure).
#   2. ``HYPOTHESIS_SEED`` environment variable.
#   3. A fresh 63-bit integer generated via :func:`secrets.randbits`,
#      installed as the session master seed so the artifact is sufficient
#      to replay a run end-to-end.
#
# Capture artifact: JSON written at session finish to
# ``${WALKING_SLICE_SEED_ARTIFACT}`` (default
# ``<rootpath>/build/hypothesis-seeds.json``). Schema:
#   {
#     "master_seed": <int>,
#     "hypothesis_profile": "<dev|ci|debug>",
#     "invocations": [
#       {"nodeid": str, "outcome": "passed"|"failed"|"skipped",
#        "hypothesis_seed": <int>}
#     ]
#   }
# ---------------------------------------------------------------------------


# Session-scoped state populated by the seed-capture hooks. Lives at module
# scope so the ``pytest_sessionfinish`` hook can read it after the session
# closes; pytest loads conftest.py once per invocation so there is no
# cross-session leakage.
_SEED_CAPTURE_STATE: dict[str, Any] = {
    "master_seed": None,
    "hypothesis_profile": None,
    "invocations": [],
}


def _resolve_master_seed() -> int:
    """Resolve and install the master Hypothesis seed for this session.

    Reads the seed in the precedence documented above. When neither the
    ``--hypothesis-seed`` CLI option nor the ``HYPOTHESIS_SEED`` env var is
    set, a fresh 63-bit integer is generated and installed via
    ``hypothesis.core.global_force_seed`` so that every property test in
    the session runs against the same master seed (Requirement 15.13).
    """
    if _hypothesis_core.global_force_seed is not None:
        return int(_hypothesis_core.global_force_seed)

    env_seed = os.environ.get("HYPOTHESIS_SEED")
    if env_seed:
        try:
            seed = int(env_seed, 0)
        except ValueError as exc:
            raise RuntimeError(
                f"HYPOTHESIS_SEED must be an integer (decimal, 0x, or 0o); "
                f"got {env_seed!r}."
            ) from exc
        _hypothesis_core.global_force_seed = seed
        return seed

    seed = secrets.randbits(63)
    _hypothesis_core.global_force_seed = seed
    return seed


def _seed_artifact_path(rootpath: Path) -> Path:
    override = os.environ.get("WALKING_SLICE_SEED_ARTIFACT")
    if override:
        return Path(override)
    return rootpath / "build" / "hypothesis-seeds.json"


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """Install the master Hypothesis seed and surface it on the config.

    Declared ``trylast=True`` so the Hypothesis pytest plugin's own
    ``pytest_configure`` runs first; that plugin honors
    ``--hypothesis-seed=N`` by setting
    :attr:`hypothesis.core.global_force_seed`. By running after, we
    either read the user's seed or install a fresh one when none was
    supplied. The seed is stashed on ``config`` so individual tests
    (in particular Property 13's wiring assertion) can confirm the
    mechanism is active without reading the on-disk artifact.
    """
    seed = _resolve_master_seed()
    _SEED_CAPTURE_STATE["master_seed"] = seed
    # Read the *actual* active Hypothesis profile rather than the env
    # var alone, so that ``--hypothesis-profile`` overrides applied by
    # the Hypothesis pytest plugin (which runs before this hook) are
    # reflected in the captured artifact.
    try:
        active_profile = settings.get_current_profile_name()
    except Exception:  # pragma: no cover - defensive
        active_profile = os.environ.get("HYPOTHESIS_PROFILE", "dev")
    _SEED_CAPTURE_STATE["hypothesis_profile"] = active_profile
    # Expose on config under a private attribute so tests can introspect.
    config._walking_slice_seed = seed  # type: ignore[attr-defined]

    terminal = config.pluginmanager.getplugin("terminalreporter")
    if terminal is not None:  # pragma: no branch - depends on pytest plugins
        terminal.write_line(
            f"[walking-slice] hypothesis master seed = {seed} "
            f"(profile={_SEED_CAPTURE_STATE['hypothesis_profile']!r})"
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Iterator[None]:
    """Record (nodeid, outcome, seed) for every property-marked test call.

    Only the ``call`` phase is captured (setup/teardown outcomes are
    surfaced separately by pytest); failures during setup/teardown still
    produce a ``call`` report with outcome ``"failed"`` so the artifact
    remains complete for triage.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    if item.get_closest_marker("property") is None:
        return
    _SEED_CAPTURE_STATE["invocations"].append(
        {
            "nodeid": item.nodeid,
            "outcome": report.outcome,
            "hypothesis_seed": _SEED_CAPTURE_STATE["master_seed"],
        }
    )


def pytest_sessionfinish(
    session: pytest.Session, exitstatus: int
) -> None:  # noqa: ARG001 - exitstatus required by the hook signature
    """Persist the seed-capture artifact for the just-finished session.

    Written even when the session produced zero property invocations so
    that CI consumers can rely on the artifact's existence (the
    ``invocations`` array is simply empty in that case).
    """
    artifact_path = _seed_artifact_path(Path(session.config.rootpath))
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "master_seed": _SEED_CAPTURE_STATE["master_seed"],
        "hypothesis_profile": _SEED_CAPTURE_STATE["hypothesis_profile"],
        "invocations": list(_SEED_CAPTURE_STATE["invocations"]),
    }
    artifact_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Per-test SQLite fixtures.
#
# `sqlite_path` allocates a unique on-disk file inside pytest's ``tmp_path``;
# the directory is removed automatically at test teardown so no state leaks
# between tests. `engine` returns a SQLAlchemy Core engine bound to that file
# with the two pragmas mandated by AD-WS-1 / task 1.3 set on every connection
# (`journal_mode=WAL`, `foreign_keys=ON`). Schema creation (task 1.3) and
# seeding (task 13.1, 13.2) are layered on top of this fixture in later tasks.
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    """Return a unique SQLite file path for the current test."""
    return tmp_path / "walking_slice.sqlite"


@pytest.fixture
def engine(sqlite_path: Path) -> Iterator[Engine]:
    """Yield a per-test SQLAlchemy Core engine bound to an isolated SQLite file."""
    url = f"sqlite:///{sqlite_path.as_posix()}"
    eng = create_engine(url, future=True)

    @event.listens_for(eng, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover - exercised by every test
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    try:
        yield eng
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Shared dependency-injection slots.
#
# These fixtures name the wiring points the walking slice expects every
# request and every property/example test to receive. Each fixture currently
# raises so that any test accidentally relying on a not-yet-implemented
# service receives a clear "implement in task X" signal rather than a
# misleading import error. Subsequent tasks override these fixtures by
# defining same-named fixtures closer to the test (or by replacing the body
# here once the service module lands).
# ---------------------------------------------------------------------------


def _pending(task: str, name: str) -> None:
    raise NotImplementedError(
        f"{name} fixture is pending {task}; override in a closer conftest.py "
        f"or extend tests/conftest.py once the service module exists."
    )


@pytest.fixture
def clock() -> Clock:
    """Deterministic :class:`Clock` for tests.

    Returns a :class:`walking_slice.clock.FixedClock` pinned to
    ``2026-01-01T00:00:00.000Z``. Tests requiring a different fixed instant
    can override this fixture in a closer ``conftest.py`` or via
    ``@pytest.fixture(name="clock")``. The default value sits comfortably
    inside the slice's pilot horizon and uses millisecond precision so it
    round-trips through the audit-storage contract from
    design §"Cross-Cutting Concerns".
    """
    return FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.fixture
def identity_service(engine, clock, audit_log) -> IdentityService:
    """:class:`~walking_slice.identity.IdentityService` wired to the per-test engine.

    The audit_log fixture has already installed the schema and constructed
    the AuditLog over the same clock, so the IdentityService can append
    Denial Records on identifier-conflict from a separate transaction
    against the same SQLite file (design §"Error Handling — Identifier
    conflict (Requirement 1.4)").
    """
    return IdentityService(engine=engine, audit_log=audit_log, clock=clock)


@pytest.fixture
def audit_log(engine, clock) -> AuditLog:
    """:class:`~walking_slice.audit.AuditLog` bound to the per-test clock.

    The audit service is connection-scoped at call time (each
    :meth:`append_consequential`/:meth:`append_denial` invocation receives
    the caller's SQLAlchemy connection so the append participates in the
    caller's transaction per AD-WS-5). The fixture therefore needs only the
    injected :class:`~walking_slice.clock.Clock`; ``engine`` is consumed so
    the schema is initialized once and the audit ``INSERT`` resolves its FK
    references to a real ``Audit_Records`` table.
    """
    create_schema(engine)
    return AuditLog(clock)


@pytest.fixture
def authorization_service(engine, clock, audit_log) -> AuthorizationService:
    """:class:`~walking_slice.authorization.AuthorizationService` for tests.

    Wires the per-test :class:`Clock` and :class:`AuditLog` through the
    service constructor. The service is connection-scoped at call time
    (both ``assign_role`` and ``evaluate`` accept the caller's
    SQLAlchemy connection so writes participate in the caller's
    transaction per AD-WS-5); the fixture therefore needs only the
    cross-request collaborators. The :class:`IdentityService` is the
    in-memory default — task 2.2 will swap in the persistent registry
    once it ships, without changing this fixture's contract.

    ``engine`` is consumed so the schema is installed via the
    ``audit_log`` fixture before the AuthorizationService is asked to
    insert ``Role_Assignments`` rows or audit records.
    """
    return AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=IdentityService(),
    )


@pytest.fixture
def manifest_writer(
    clock: Clock, identity_service: IdentityService
) -> ProvenanceManifestWriter:
    """:class:`~walking_slice.manifests.ProvenanceManifestWriter` for tests.

    Wires the per-test :class:`Clock` and :class:`IdentityService`
    through the writer constructor (task 9.2). The writer is
    connection-scoped at call time (:meth:`write_manifest` accepts the
    caller's SQLAlchemy connection so the manifest INSERTs participate
    in the caller's transaction per AD-WS-5); the fixture therefore
    needs only the cross-request collaborators.

    Tests that need to drive :class:`KnowledgeService.create_finding`,
    :meth:`create_recommendation`, or :meth:`create_decision` *with*
    manifest persistence wired (the task 9.2 path) construct their
    :class:`KnowledgeService` with this writer; tests that pre-date
    task 9.2 construct the service without ``manifest_writer=`` and
    exercise the back-compat path where the inline Decision manifest
    write is preserved.
    """
    return ProvenanceManifestWriter(
        clock=clock,
        identity_service=identity_service,
    )


@pytest.fixture
def trail_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> TrailService:
    """:class:`~walking_slice.trails.TrailService` wired for tests.

    Built without an :class:`AuthorizationService` or a
    :class:`ProvenanceManifestWriter` so tests that exercise the
    structural validators and the persistence path do not need to
    seed a role assignment or a manifest fixture. Tests that need
    those collaborators construct their own ``TrailService(...)``
    in the test body (mirroring the pattern in
    ``test_knowledge_decision_authority.py``); the connection-scoped
    surface keeps the override cost low.
    """
    return TrailService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def request_context(
    engine,
    clock,
    identity_service,
    authorization_service,
    audit_log,
):
    """``RequestContext`` bundle from design §"Application-Level Composition".

    Returns a fully-populated :class:`walking_slice.auth_middleware.RequestContext`
    so unit tests that exercise service composition (and the wave-22 route
    refactor that mounts ``get_request_context`` as a FastAPI dependency)
    can rely on a ready-made bundle. End-to-end HTTP tests that need to
    drive the bundle through an actual request use the
    :class:`~walking_slice.auth_middleware.RequestContextResolver` helper
    directly so they can override the dependency on a per-test
    ``FastAPI()`` instance.
    """
    from walking_slice.auth_middleware import RequestContext

    return RequestContext(
        party_id="00000000-0000-7000-8000-0000000000aa",
        correlation_id="00000000-0000-7000-8000-0000000000cc",
        clock=clock,
        engine=engine,
        ids=identity_service,
        authz=authorization_service,
        audit=audit_log,
    )
