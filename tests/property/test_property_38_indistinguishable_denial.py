# Feature: third-walking-slice, Property 38: Indistinguishable denial across Slice 3 endpoints
"""Property 38 — Indistinguishable denial across Slice 3 endpoints (task 16.8).

**Property 38: Indistinguishable denial across Slice 3 endpoints**

For all pairs ``(P, P')`` of Parties differing only in authority on a
Slice 3 Resource ``R``, the response visible to ``P'`` for
provenance traversal attempts on ``R`` is indistinguishable from the
response ``P'`` would receive in a universe where ``R`` does not
exist, across:

- result count
- identifier set
- ordering positions
- pagination cursors
- response size
- response body keys
- error category (HTTP status)
- error wording (response body content)
- latency baseline (within 100 ms tolerance)

**Validates: Requirements 30.1, 30.4, 30.5, 30.7, 31.5, 31.6, 35.3,
35.6, 35.7, 36.3, 36.5, 38.2, 38.3, 38.4, 41.8**

Strategy
--------

Each Hypothesis case draws a *scenario* targeting one of the three
Slice 3 provenance traversal endpoints that the design pins as
fully normalized today:

- ``GET /api/v1/completions/{completion_id}/provenance``
  delegated to :meth:`ProvenanceNavigator.navigate_completion` —
  the unresolved and restricted cases both raise the same
  :class:`CompletionUnresolvableError` so the response is byte-
  equivalent (Requirements 31.5 / 31.6).
- ``GET /api/v1/deliverable-productions/{deliverable_production_id}/provenance``
  delegated to :meth:`ProvenanceNavigator.navigate_deliverable_production`
  — same :class:`DeliverableProductionUnresolvableError` pattern
  (Requirements 35.6 / 35.7).
- ``GET /api/v1/deliverables/{deliverable_id}/revisions/{deliverable_revision_id}/provenance``
  delegated to :meth:`ProvenanceNavigator.navigate_produced_deliverable_revision`
  — same :class:`DeliverableRevisionUnresolvableError` pattern
  (Requirements 35.6 / 35.7).

These are the read-surface counterparts of Slice 1 Property 4 /
Requirement 8.3 extended to Slice 3. They are the *only* Slice 3
surfaces where one navigator call alone produces a complete
"response" whose dimensions (count, identifier set, ordering,
cursor, response size, error category, error wording, latency) are
fully defined and *implemented today*. The creation, read (GET
single Record), backlink, and projection endpoints emit denial
responses through the AD-WS-9 shape-stable
:class:`DenialResponseBody`; the design-mandated full HTTP-layer
indistinguishability between "restricted Resource" and "non-existent
target identifier" on those surfaces awaits the
``walking_slice.provenance._shape_response_constant_time`` helper
described in design §"Disclosure policy enforcement on error
responses" (Gap G-12 / ``ADR-HT-014``). Property 38 as stated here
pins the dimensions that the implemented surfaces *do* normalize
today; the full HTTP-layer normalization is tracked separately —
this is the same scoping discipline the Slice 2 analogue
(:mod:`tests.property.test_property_18_indistinguishable_denial_planning`)
applies and is consistent with the design's note that the test
"pins the dimensions that the implemented surfaces *do* normalize
today".

Per case the test stands up two minimal FastAPI applications, each
backed by its own on-disk SQLite database:

- **Universe X** — the target Slice 3 Resource ``R`` (Completion
  Record / Deliverable Production Record / produced Deliverable
  Revision) is persisted with ``applicable_scope`` set to a value
  the requesting Party ``P'`` lacks view authority on. The
  navigator's authorization gate denies the request and the
  unresolved-indistinguishable path raises the same exception that
  is raised for a non-existent identifier.

- **Universe Y** — no Slice 3 Resource is persisted at all. The
  navigator's ``WHERE`` clause resolves the identifier to zero
  rows and raises the same exception (with the same constructor
  argument).

The two universes share identical Party Identities, identical
``view`` role assignments for the requesting Party ``P'`` (covering
a scope that does *not* cover ``R`` in Universe X), and identical
schema seeding (Slice 1 + Slice 2 + Slice 3 schemas + the
``slice-default-2026`` Disclosure Policy + the Slice 2 / Slice 3
coverage rows). The two universes differ *only* in whether ``R``
exists.

The HTTP request to each universe is constructed identically — same
URL, same headers, same path parameters — so any divergence in the
response body is attributable to the existence of ``R`` rather than
to incidental request shape.

Per case the test then asserts the nine dimensions named in the
property statement:

- **HTTP status code** equality (error category — both 404 here).
- **Body-key set** equality (``set(json.keys())`` of the response).
- **Body content** equality (full JSON deep-equality covers
  identifier set, ordering, pagination cursor, and error wording).
- **Response size** equality (``len(response.content)`` in bytes).
- **Result count** equality (a 404 carries no "results" array; the
  count dimension is satisfied by the body-keys / body-content
  equality above — there is nothing to count and the count is
  trivially equal).
- **Latency** within ``_LATENCY_TOLERANCE_SECONDS`` (100 ms tolerance
  per Requirements 30.7 / 38.4 — both responses are 404s with
  identical bodies, so the latency difference reflects only
  request-handling jitter rather than work that depends on the
  existence of ``R``).

Hypothesis settings
-------------------

``@settings(max_examples=100, deadline=2000)`` per task 16.8's task
notes. ``suppress_health_check`` covers ``too_slow``,
``data_too_large``, and ``function_scoped_fixture`` because each
case allocates two on-disk SQLite databases plus two minimal
FastAPI applications and runs one HTTP round-trip per universe.
The setup is dominated by SQLite schema creation and is comfortably
under the 2000 ms deadline locally; the suppressions exist so a
single slow case (e.g. on a cold filesystem cache) does not abort
the property run.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Optional

import pytest
import uuid_utils
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.auth_middleware import RequestContext
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    get_policy,
    seed as seed_disclosure_policies,
)
from walking_slice.execution._disclosure import seed_execution_coverage
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import seed_planning_coverage
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import ProvenanceNavigator
from walking_slice.routes.provenance import (
    get_engine as provenance_get_engine,
    get_provenance_navigator,
    get_request_context as provenance_get_request_context,
    router as provenance_router,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


# A single fixed instant anchors every persisted ``recorded_at`` so the
# row contents are byte-equivalent across universes; the navigator's
# read paths read the column but the indistinguishability property
# concerns itself with the response *form*, not the row contents.
_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"


# Authority basis identifier persisted on every Slice 3 row's
# ``authority_basis_id`` column. The column has no FK constraint so
# any opaque string is acceptable; centralizing the value keeps the
# seed deterministic across Hypothesis shrinks.
_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-000000380001"


# Fixed Party Identities. The requester (``P'``) is the Party whose
# response we compare across the two universes. The other Identities
# are required to satisfy FK constraints on the seeded rows in
# Universe X.
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000380101"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000380102"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000380103"
_COMPLETING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000380104"


# Scopes. The requester ``P'`` is granted ``view`` on
# :data:`_REQUESTER_SCOPE` so they are a fully-authenticated Party
# with some role assignment; they are *not* granted view on
# :data:`_RESTRICTED_SCOPE` so the Slice 3 Resource seeded under
# that scope in Universe X is invisible to them.
_REQUESTER_SCOPE: Final[str] = "prop-38/requester-scope"
_RESTRICTED_SCOPE: Final[str] = "prop-38/restricted-scope"


# Latency tolerance from the property statement / Requirement 30.7 /
# Requirement 38.4. The two universes return 404s with byte-
# equivalent bodies in the indistinguishable cases, so the practical
# latency delta is dominated by request-handling jitter; the 100 ms
# tolerance accommodates incidental scheduler variation without
# masking a real timing channel.
_LATENCY_TOLERANCE_SECONDS: Final[float] = 0.1


# The three Slice 3 traversal endpoints under test. The strategy
# samples from this set so the property is exercised against every
# implemented indistinguishable surface.
_TARGET_KINDS: Final[tuple[str, ...]] = (
    "completion",
    "deliverable_production",
    "deliverable_revision",
)


# ---------------------------------------------------------------------------
# Per-universe engine + app builder.
# ---------------------------------------------------------------------------


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string."""
    return str(uuid_utils.uuid7())


def _build_engine(tmp_dir: Path, *, suffix: str) -> Engine:
    """Create a fresh per-universe SQLite engine with every schema seeded.

    Layers the Slice 1, Slice 2 planning, Slice 3 execution, and
    Slice 3 deliverable schemas in the same order
    :func:`walking_slice.app.create_app` uses so triggers and FK
    constraints match production. Also seeds the
    ``slice-default-2026`` Disclosure Policy and the Slice 2 / Slice
    3 ``Disclosure_Policy_Coverage`` rows so the navigator's gap-
    descriptor and redaction-marker code paths resolve.
    """
    db_path = tmp_dir / f"walking_slice_{suffix}.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    seed_disclosure_policies(engine)
    with engine.begin() as conn:
        seed_planning_coverage(conn)
        seed_execution_coverage(conn, clock=FixedClock(_NOW))
    return engine


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert one ``Parties`` row required by FK constraints."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


def _seed_required_parties(engine: Engine) -> None:
    """Seed the four Party rows the test references.

    The same four Parties are seeded in both universes so the only
    persistence difference is the presence or absence of the target
    Slice 3 Resource ``R``. Identifier opacity (Requirement 1.7)
    means the rows are FK targets only; the API surface does not
    re-emit the Party display name.
    """
    with engine.begin() as conn:
        _seed_party(conn, _REQUESTER_PARTY_ID, "Property 38 Requester")
        _seed_party(conn, _AUTHORING_PARTY_ID, "Property 38 Authoring Party")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Property 38 Steward")
        _seed_party(conn, _COMPLETING_PARTY_ID, "Property 38 Completer")


def _grant_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
) -> None:
    """Grant the requesting Party ``view`` authority on ``scope``.

    The scope value is :data:`_REQUESTER_SCOPE` — a scope that does
    *not* cover the Slice 3 Resource seeded under
    :data:`_RESTRICTED_SCOPE` in Universe X. The role assignment is
    issued in both universes so the Parties' authority *set* is
    byte-equivalent across them; the only difference is whether the
    target Resource exists.
    """
    request = AssignRoleRequest(
        party_id=_REQUESTER_PARTY_ID,
        role_name="reviewer",
        scope=scope,
        authorities_granted=("view",),
        effective_start=_NOW,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


# ---------------------------------------------------------------------------
# Universe X seeding — persist the target Resource under a restricted scope.
# ---------------------------------------------------------------------------


def _seed_minimum_planning_chain(engine: Engine, ids: dict) -> None:
    """Seed the minimum Slice 2 planning rows needed by a Completion Record.

    A Completion Record's ``target_plan_revision_id``,
    ``target_activity_plan_id``, and ``target_project_id`` columns
    reference Slice 2 tables that have NOT NULL FK constraints on
    ``Plan_Revisions`` / ``Activity_Plans`` / ``Projects``. The
    rows are seeded with :data:`_RESTRICTED_SCOPE` so they are part
    of the same authority surface the requesting Party lacks view
    on, and any incidental Slice 2 read path also lands on the
    indistinguishable response.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": ids["project_id"], "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 38 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": ids["activity_plan_id"],
                "pid": ids["project_id"],
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :aid, NULL, 'approved',
                    'Property 38 planned scope.', '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": ids["plan_revision_id"],
                "aid": ids["activity_plan_id"],
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_assignment_for_production(
    engine: Engine, ids: dict
) -> None:
    """Seed one Work Assignment Record (FK target of Production Record)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Assignment_Records (
                    work_assignment_id, target_plan_revision_id,
                    assignee_party_id, assignment_authority_party_id,
                    assignment_rationale, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :wid, :prev, :assignee, :authority,
                    'Property 38 WA rationale.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": ids["work_assignment_id"],
                "prev": ids["plan_revision_id"],
                "assignee": _AUTHORING_PARTY_ID,
                "authority": _ASSIGNING_AUTHORITY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_resource_and_revision(
    engine: Engine, ids: dict
) -> None:
    """Seed Deliverable Resource + produced Revision (FK targets)."""
    digest_hex = "00" * 32  # canonical SHA-256 placeholder (64 hex chars)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Property 38 deliverable', :ts)
                """
            ),
            {"did": ids["deliverable_id"], "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes, content_digest_sha256,
                    role_marker, originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :did, 'text/markdown', :bytes, :digest,
                    'generated_output', :wa, :party, :ts
                )
                """
            ),
            {
                "rev": ids["deliverable_revision_id"],
                "did": ids["deliverable_id"],
                "bytes": b"property 38 placeholder content",
                "digest": digest_hex,
                "wa": ids["work_assignment_id"],
                "party": _AUTHORING_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_expectation(engine: Engine, ids: dict) -> None:
    """Seed Deliverable Expectation Revision (FK target of Production)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectations
                    (deliverable_expectation_id, created_at)
                VALUES (:did, :ts)
                """
            ),
            {
                "did": ids["deliverable_expectation_id"],
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectation_Revisions (
                    deliverable_expectation_revision_id,
                    deliverable_expectation_id, parent_revision_id,
                    target_project_id, name, description,
                    deliverable_kind, acceptance_criteria,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :did, NULL, :pid, 'Property 38 expectation',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": ids["deliverable_expectation_revision_id"],
                "did": ids["deliverable_expectation_id"],
                "pid": ids["project_id"],
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production(engine: Engine, ids: dict) -> None:
    """Seed one Deliverable Production Record under the restricted scope."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Production_Records (
                    deliverable_production_id, source_work_assignment_id,
                    produced_deliverable_id, produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    production_rationale, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :pid, :wa, :did, :rev, :exp_did, :exp_rev,
                    'Property 38 production.', :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": ids["deliverable_production_id"],
                "wa": ids["work_assignment_id"],
                "did": ids["deliverable_id"],
                "rev": ids["deliverable_revision_id"],
                "exp_did": ids["deliverable_expectation_id"],
                "exp_rev": ids["deliverable_expectation_revision_id"],
                "party": _AUTHORING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_completion(engine: Engine, ids: dict) -> None:
    """Seed one Completion Record under the restricted scope."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id, target_plan_revision_id,
                    target_activity_plan_id, target_project_id,
                    outcome, rationale,
                    source_milestone_acceptance_ids_json,
                    completing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :cid, :prev, :aid, :pid, 'Completed',
                    'Property 38 completion.', '[]', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": ids["completion_id"],
                "prev": ids["plan_revision_id"],
                "aid": ids["activity_plan_id"],
                "pid": ids["project_id"],
                "party": _COMPLETING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_universe_x(engine: Engine, target_kind: str, ids: dict) -> None:
    """Seed Universe X — the target Resource exists under a restricted scope.

    The seeded rows form the minimum chain required to satisfy the
    FK constraints around the target Slice 3 Resource. They are all
    persisted under :data:`_RESTRICTED_SCOPE`, which the requesting
    Party ``P'`` lacks view authority on, so the navigator's
    head-node authorization gate denies the request and surfaces the
    same exception that is raised for a non-existent identifier
    (Requirements 31.5 / 31.6 / 35.6 / 35.7 — the
    ``not_found_indistinguishable_response`` branch).
    """
    if target_kind == "completion":
        _seed_minimum_planning_chain(engine, ids)
        _seed_completion(engine, ids)
    elif target_kind == "deliverable_production":
        _seed_minimum_planning_chain(engine, ids)
        _seed_work_assignment_for_production(engine, ids)
        _seed_deliverable_resource_and_revision(engine, ids)
        _seed_deliverable_expectation(engine, ids)
        _seed_deliverable_production(engine, ids)
    elif target_kind == "deliverable_revision":
        _seed_minimum_planning_chain(engine, ids)
        _seed_work_assignment_for_production(engine, ids)
        _seed_deliverable_resource_and_revision(engine, ids)
    else:  # pragma: no cover - defensive
        raise AssertionError(f"Unknown target_kind: {target_kind!r}")


# ---------------------------------------------------------------------------
# Minimal FastAPI app wiring.
# ---------------------------------------------------------------------------


def _make_request_context_dependency(
    engine: Engine,
    clock: FixedClock,
    identity_service: IdentityService,
    authorization_service: AuthorizationService,
    audit_log: AuditLog,
):
    """Build a ``get_request_context`` override returning a fixed Party.

    The Slice 3 provenance routes resolve ``ctx.party_id`` from a
    :class:`RequestContext`. The full
    :class:`RequestContextResolver` validates bearer tokens; for
    this property test we only need ``ctx.party_id`` and
    ``ctx.engine`` to be populated, but :class:`RequestContext` is a
    frozen dataclass that types every collaborator slot as non-
    optional. The dependency therefore receives the same set of
    collaborators the universe wired earlier — they are cheap to
    construct and avoid surprising ``AttributeError`` failures if
    the route surface ever expands.
    """
    correlation_id = _new_uuid7()

    def _resolver() -> RequestContext:
        return RequestContext(
            party_id=_REQUESTER_PARTY_ID,
            correlation_id=correlation_id,
            clock=clock,
            engine=engine,
            ids=identity_service,
            authz=authorization_service,
            audit=audit_log,
        )

    return _resolver


def _build_app(
    engine: Engine,
    navigator: ProvenanceNavigator,
    clock: FixedClock,
    identity_service: IdentityService,
    authorization_service: AuthorizationService,
    audit_log: AuditLog,
) -> FastAPI:
    """Build a minimal FastAPI app exposing the provenance routes.

    The app intentionally mounts only the provenance router so the
    per-case setup cost is dominated by SQLite schema creation
    rather than by FastAPI wiring. The three Slice 3 traversal
    endpoints under test resolve through this router; the Slice 1 /
    Slice 2 endpoints are also reachable but the test does not
    drive them.
    """
    app = FastAPI()
    app.include_router(provenance_router)
    overrides = app.dependency_overrides
    overrides[provenance_get_engine] = lambda: engine
    overrides[get_provenance_navigator] = lambda: navigator
    overrides[provenance_get_request_context] = _make_request_context_dependency(
        engine,
        clock,
        identity_service,
        authorization_service,
        audit_log,
    )
    return app


def _build_universe(
    tmp_dir: Path,
    *,
    suffix: str,
    seed_target: bool,
    target_kind: str,
    ids: dict,
) -> tuple[Engine, FastAPI]:
    """Build one universe: engine + parties + role + (optional) target Resource.

    The function is universe-symmetric — Universe X passes
    ``seed_target=True``, Universe Y passes ``seed_target=False``.
    Every other seeding step (Parties, role assignment) is identical
    so the only persistence difference is the presence or absence of
    the target Slice 3 Resource.
    """
    engine = _build_engine(tmp_dir, suffix=suffix)
    _seed_required_parties(engine)

    clock = FixedClock(_NOW)
    audit_log = AuditLog(clock)
    identity_service = IdentityService()
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    # Load the disclosure policy so the navigator can apply the
    # AD-WS-9 redaction marker / gap descriptor rules. Without this
    # the navigator falls back to an "everything visible" default
    # which would collapse the indistinguishability we are testing.
    disclosure_policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
    navigator = ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
        disclosure_policy=disclosure_policy,
    )

    # Grant the requester view authority on :data:`_REQUESTER_SCOPE`.
    # This is the *same* role assignment in both universes — the
    # only persistence difference between X and Y is whether ``R``
    # is seeded (and ``R``'s scope is
    # :data:`_RESTRICTED_SCOPE`, which is *not* covered by this
    # assignment).
    _grant_view_authority(
        authorization_service, engine, scope=_REQUESTER_SCOPE
    )

    if seed_target:
        _seed_universe_x(engine, target_kind, ids)

    app = _build_app(
        engine,
        navigator,
        clock,
        identity_service,
        authorization_service,
        audit_log,
    )
    return engine, app


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


@st.composite
def _scenario(draw) -> dict:
    """Draw one Property 38 scenario.

    A scenario carries:

    - ``target_kind``: the Slice 3 Resource kind the indistinguishable
      response is asserted against. Drawn uniformly from the three
      implemented surfaces so every endpoint is exercised across a
      Hypothesis run.
    - ``ids``: a bundle of fresh UUIDv7 identifiers for every row
      seeded in Universe X (and for the URL path parameters in
      Universe Y). The target Identity is the *same* in both
      universes so the navigator's ``WHERE`` clauses produce a
      deterministic query.

    The ID bundle is drawn per case so the property test does not
    accidentally reuse identifiers across cases — Hypothesis shrinks
    on identifier values would otherwise produce confusingly
    correlated counterexamples.
    """
    target_kind = draw(st.sampled_from(_TARGET_KINDS))
    ids = {
        "completion_id": _new_uuid7(),
        "plan_revision_id": _new_uuid7(),
        "activity_plan_id": _new_uuid7(),
        "project_id": _new_uuid7(),
        "work_assignment_id": _new_uuid7(),
        "deliverable_id": _new_uuid7(),
        "deliverable_revision_id": _new_uuid7(),
        "deliverable_expectation_id": _new_uuid7(),
        "deliverable_expectation_revision_id": _new_uuid7(),
        "deliverable_production_id": _new_uuid7(),
    }
    return {"target_kind": target_kind, "ids": ids}


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


def _request_url_and_label(target_kind: str, ids: dict) -> tuple[str, str]:
    """Build the HTTP URL for the target Slice 3 traversal endpoint.

    Returns the URL plus a stable label used in failure messages so
    the dimension that failed names the endpoint that exhibited the
    leak.
    """
    if target_kind == "completion":
        return (
            f"/api/v1/completions/{ids['completion_id']}/provenance",
            "GET /completions/{id}/provenance",
        )
    if target_kind == "deliverable_production":
        return (
            f"/api/v1/deliverable-productions/"
            f"{ids['deliverable_production_id']}/provenance",
            "GET /deliverable-productions/{id}/provenance",
        )
    if target_kind == "deliverable_revision":
        return (
            f"/api/v1/deliverables/{ids['deliverable_id']}"
            f"/revisions/{ids['deliverable_revision_id']}/provenance",
            "GET /deliverables/{id}/revisions/{rid}/provenance",
        )
    raise AssertionError(  # pragma: no cover - defensive
        f"Unknown target_kind: {target_kind!r}"
    )


def _drive_request(app: FastAPI, url: str) -> tuple[int, dict, bytes, float]:
    """Drive one GET request and return ``(status, body, raw_bytes, latency)``.

    The test client is constructed per-call so the FastAPI lifespan
    runs once per request — startup work is empty for the minimal
    app under test, so per-call construction is cheap. The request
    is timed end-to-end including the JSON deserialization so the
    latency dimension captures the full caller-observable cost.
    """
    headers = {"X-Actor-Party-Id": _REQUESTER_PARTY_ID}
    with TestClient(app) as client:
        start = time.perf_counter()
        response = client.get(url, headers=headers)
        elapsed = time.perf_counter() - start
    try:
        body = response.json()
    except ValueError:
        body = {}
    return response.status_code, body, response.content, elapsed


@given(scenario=_scenario())
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates two on-disk SQLite databases and stands
    # up two minimal FastAPI apps; per-case setup is more expensive
    # than a purely in-memory test. The data-generation and slow-
    # test health checks are suppressed so any one slow case does
    # not abort the property run (Hypothesis's default threshold is
    # tuned for fast-strategy tests).
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_indistinguishable_denial_across_slice3_endpoints(
    scenario: dict,
) -> None:
    """P'`s denial response on R is byte-equivalent to a non-existent universe.

    For each scenario the test:

    1. Constructs two on-disk SQLite engines, one per universe, and
       stands up a minimal FastAPI app over each (Universe X / Y).
    2. Seeds the same Parties and the same ``view`` role assignment
       on :data:`_REQUESTER_SCOPE` in both universes; Universe X
       additionally seeds the target Slice 3 Resource ``R`` under
       :data:`_RESTRICTED_SCOPE` so the requesting Party lacks view
       authority on it.
    3. Drives the same GET request to both apps and captures the
       status code, the parsed JSON body, the raw response bytes,
       and the wall-clock latency.
    4. Asserts the nine dimensions from the property statement —
       error category, body keys, body content (covers identifier
       set, ordering, pagination cursor, error wording, and result
       count), response size, and latency within
       :data:`_LATENCY_TOLERANCE_SECONDS`.
    """
    target_kind = scenario["target_kind"]
    ids = scenario["ids"]
    url, endpoint_label = _request_url_and_label(target_kind, ids)

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop38_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine_x: Optional[Engine] = None
        engine_y: Optional[Engine] = None
        try:
            engine_x, app_x = _build_universe(
                case_dir,
                suffix="x",
                seed_target=True,
                target_kind=target_kind,
                ids=ids,
            )
            engine_y, app_y = _build_universe(
                case_dir,
                suffix="y",
                seed_target=False,
                target_kind=target_kind,
                ids=ids,
            )

            status_x, body_x, raw_x, latency_x = _drive_request(app_x, url)
            status_y, body_y, raw_y, latency_y = _drive_request(app_y, url)

            # ----- Error-category dimension --------------------------
            # The HTTP status code is the most coarse-grained signal
            # of "what kind of response is this". A restricted node
            # and a non-existent node must both produce the same
            # status code; any divergence is the canonical AD-WS-9
            # leak.
            assert status_x == status_y, (
                f"Property 38 violated on the error-category dimension "
                f"({endpoint_label}): Universe X returned status "
                f"{status_x}, Universe Y returned status {status_y}. "
                f"X body={body_x!r}; Y body={body_y!r}."
            )

            # ----- Body-keys dimension -------------------------------
            # The set of top-level keys in the JSON response body
            # must be identical. ``ErrorBody`` is a Pydantic model
            # with stable optional fields; a divergence here would
            # indicate that one branch populated a field the other
            # did not (e.g. a restricted-only attribute leaking
            # through).
            assert set(body_x.keys()) == set(body_y.keys()), (
                f"Property 38 violated on the body-keys dimension "
                f"({endpoint_label}): X-only keys="
                f"{sorted(set(body_x.keys()) - set(body_y.keys()))!r}, "
                f"Y-only keys="
                f"{sorted(set(body_y.keys()) - set(body_x.keys()))!r}."
            )

            # ----- Body content / identifier-set / ordering /
            # cursor / error-wording / count dimension --------------
            # ``ErrorBody`` is a flat dict on the indistinguishable
            # branches so a deep equality covers every observable
            # within the body: the unresolvable identifier surfaced
            # via the ``region_id`` field, the error code, the
            # message wording, and any optional pagination cursor.
            # The result count is implicit (a 404 carries no
            # ``results`` array, so the count dimension is trivially
            # equal — there is nothing to count and the count is
            # zero in both universes).
            assert body_x == body_y, (
                f"Property 38 violated on the body-content dimension "
                f"({endpoint_label}): X={body_x!r}, Y={body_y!r}."
            )

            # ----- Response-size dimension ---------------------------
            # Asserted on the raw byte stream so the failure message
            # names the dimension when the JSON bodies happen to
            # be equal but their serialized forms differ. The
            # responses are produced by the same Pydantic model in
            # both universes, so a divergence here would indicate
            # a code-path difference upstream of JSON serialization
            # (e.g. one branch adding a header that affects
            # ``response.content``).
            assert len(raw_x) == len(raw_y), (
                f"Property 38 violated on the response-size dimension "
                f"({endpoint_label}): "
                f"len(X.content)={len(raw_x)}, len(Y.content)={len(raw_y)}."
            )

            # ----- Latency dimension ---------------------------------
            # The 100 ms tolerance is taken verbatim from
            # Requirements 30.7 / 38.4. Both universes return 404s
            # with byte-equivalent bodies on the indistinguishable
            # branch; the latency delta therefore reflects only
            # request-handling jitter rather than work that depends
            # on the existence of ``R``.
            latency_delta = abs(latency_x - latency_y)
            assert latency_delta <= _LATENCY_TOLERANCE_SECONDS, (
                f"Property 38 violated on the latency dimension "
                f"({endpoint_label}): "
                f"X={latency_x * 1000:.1f} ms, "
                f"Y={latency_y * 1000:.1f} ms, "
                f"|delta|={latency_delta * 1000:.1f} ms exceeds the "
                f"{_LATENCY_TOLERANCE_SECONDS * 1000:.0f} ms tolerance."
            )
        finally:
            if engine_x is not None:
                engine_x.dispose()
            if engine_y is not None:
                engine_y.dispose()
