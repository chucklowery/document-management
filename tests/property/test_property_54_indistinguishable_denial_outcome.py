# Feature: fourth-walking-slice, Property 54: Indistinguishable denial for outcome-measurement endpoints
"""Property 54 — Indistinguishable denial for outcome-measurement endpoints (task 15.9).

**Property 54: Indistinguishable denial for outcome-measurement endpoints**

For all pairs ``(P, P')`` of Parties differing only in authority on a
Slice 4 outcome-measurement target ``R``, the response visible to ``P'``
for a provenance / projection attempt on ``R`` is indistinguishable from
the response ``P'`` would receive in a universe where ``R`` does not
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

The same indistinguishability holds when ``R`` is restricted by the
``slice-default-2026`` policy as extended by AD-WS-34: restricted-vs-
nonexistent observability is constant.

**Validates: Requirements 50.2, 50.4, 50.5, 50.6, 50.7, 51.5, 51.6,
55.3, 55.6, 55.7, 56.3, 56.5, 58.2, 58.3, 58.4, 58.5, 61.9**

Strategy
--------

Each Hypothesis case draws a *scenario* targeting one of the four Slice
4 read surfaces the design pins as fully normalized today — the surfaces
where one navigator / projection call alone produces a complete
"response" whose dimensions (count, identifier set, ordering, cursor,
response size, error category, error wording, latency) are fully defined
and *implemented today*, and whose restricted-vs-nonexistent observability
is enforced by a *single* indistinguishable exception type:

- ``GET /api/v1/outcome-reviews/{id}/provenance`` delegated to
  :meth:`ProvenanceNavigator.navigate_outcome_review` — both the
  unresolved and the restricted (lacking ``view.outcome_review_record``)
  cases raise the same :class:`OutcomeReviewUnresolvableError` carrying
  only the requested identifier, so the 404 response is byte-equivalent
  (Requirements 55.3 / 55.6 — the *Outcome Review* endpoint).
- ``GET /api/v1/measurement-records/{id}/provenance`` delegated to
  :meth:`ProvenanceNavigator.navigate_outcome_node` — unresolved and
  restricted both surface :class:`OutcomeNodeUnresolvableError`
  (the *Measurement* endpoint; the AD-WS-34 per-attribute restriction on
  imported source-system attributes is subsumed by the whole-Record
  redaction at the head).
- ``GET /api/v1/observed-outcomes/{id}/revisions/{rid}/provenance``
  delegated to :meth:`ProvenanceNavigator.navigate_outcome_node` — same
  :class:`OutcomeNodeUnresolvableError` pattern (the *Observed Outcome*
  endpoint).
- ``GET /api/v1/intended-outcomes/{rid}/outcome-status`` delegated to
  :func:`project_outcome_status` — an unresolvable target, a wrong-kind
  target, and a target the requesting Party may not ``view`` all surface
  the same :class:`OutcomeStatusTargetUnresolvableError` (the
  *Success-Condition Assessment / Outcome Review* projection surface).

These are the Slice 4 read-surface counterparts of Slice 1 Property 4 /
Requirement 8.3, extended to Slice 4 in the same way the Slice 2
(:mod:`tests.property.test_property_18_indistinguishable_denial_planning`)
and Slice 3
(:mod:`tests.property.test_property_38_indistinguishable_denial`)
analogues are. The creation, simple-read (single-row GET), and backlink
endpoints emit denial responses through the AD-WS-9 shape-stable
:class:`DenialResponseBody` /
:class:`~walking_slice.outcome._routes.ErrorBody`; the design-mandated
full HTTP-layer indistinguishability between "restricted Resource" and
"non-existent target identifier" on those surfaces awaits the
``walking_slice.provenance._shape_response_constant_time`` helper
described in design §"Disclosure policy enforcement on error responses"
(the backlog item shared with Slices 1–3, ``ADR-HT-009`` / ``ADR-HT-014``).
Property 54 as stated here pins the dimensions that the implemented
surfaces *do* normalize today; the full HTTP-layer normalization is
tracked separately — the same scoping discipline the Slice 2 / Slice 3
analogues apply.

Per case the test stands up two minimal FastAPI applications, each backed
by its own on-disk SQLite database:

- **Universe X** — the target Slice 4 entity ``R`` is persisted with
  ``applicable_scope`` set to a value the requesting Party ``P'`` lacks
  view authority on. The navigator's / projection's authorization gate
  denies the request and the unresolved-indistinguishable path raises the
  same exception that is raised for a non-existent identifier.

- **Universe Y** — no Slice 4 entity is persisted at all. The lookup
  resolves the identifier to zero rows and raises the same exception
  (with the same constructor argument — the requested identifier, which
  is identical across the two universes).

The two universes share identical Party Identities, identical ``view``
role assignments for the requesting Party ``P'`` (covering a scope that
does *not* cover ``R`` in Universe X), and identical schema seeding
(Slice 1 + Slice 2 + Slice 3 + Slice 4 schemas + the ``slice-default-2026``
Disclosure Policy + the Slice 4 coverage rows). The two universes differ
*only* in whether ``R`` exists.

The HTTP request to each universe is constructed identically — same URL,
same headers, same path parameters — so any divergence in the response is
attributable to the existence of ``R`` rather than to incidental request
shape.

Hypothesis settings
-------------------

``@settings(max_examples=100, deadline=2000)`` per the Slice 4 PBT
configuration (design §"Property-Based Testing Approach"). The
``too_slow`` / ``data_too_large`` / ``function_scoped_fixture`` health
checks are suppressed because each case allocates two on-disk SQLite
databases plus two minimal FastAPI applications and runs one HTTP
round-trip per universe.
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

from walking_slice.app import get_request_context
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
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.identity import IdentityService
from walking_slice.outcome._disclosure import seed_outcome_coverage
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome._provenance import register_outcome_navigation
from walking_slice.outcome._routes import (
    get_engine as outcome_get_engine,
    get_provenance_navigator,
    router as outcome_router,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import ProvenanceNavigator


pytestmark = pytest.mark.property


# The Slice 4 provenance traversals are attached to ProvenanceNavigator at
# import time; this call is a defensive idempotent no-op documenting the
# dependency (importing ``walking_slice.outcome._routes`` already ran it).
register_outcome_navigation()


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


# A single fixed instant anchors every persisted ``recorded_at`` so the row
# contents are byte-equivalent across universes; the navigator's read paths
# read the column but the indistinguishability property concerns itself with
# the response *form*, not the row contents.
_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"
# An observation instant strictly before ``recorded_at`` (the
# ``observation_time <= recorded_at`` CHECK compares ISO strings, and
# "2025..." < "2026..." lexicographically).
_OBS_ISO: Final[str] = "2025-06-01T00:00:00.000Z"


# Authority basis identifier persisted on the rows that carry one. The
# column has no FK constraint so any opaque string is acceptable.
_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-000000540001"


# Fixed Party Identities. The requester (``P'``) is the Party whose response
# we compare across the two universes. The other Identities are required to
# satisfy FK constraints on the seeded rows in Universe X.
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000540101"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000540102"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000540103"


# Scopes. The requester ``P'`` is granted ``view`` on :data:`_REQUESTER_SCOPE`
# so they are a fully-authenticated Party with some role assignment; they are
# *not* granted view on :data:`_RESTRICTED_SCOPE` so the Slice 4 entity seeded
# under that scope in Universe X is invisible to them.
_REQUESTER_SCOPE: Final[str] = "prop-54/requester-scope"
_RESTRICTED_SCOPE: Final[str] = "prop-54/restricted-scope"


# Latency tolerance from the property statement / Requirements 50.7 / 58.4.
_LATENCY_TOLERANCE_SECONDS: Final[float] = 0.1


# The four Slice 4 read surfaces under test. The strategy samples from this
# set so the property is exercised against every implemented indistinguishable
# surface across a Hypothesis run.
_TARGET_KINDS: Final[tuple[str, ...]] = (
    "outcome_review",
    "measurement_record",
    "observed_outcome_revision",
    "intended_outcome_revision",
)


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string."""
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Per-universe engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path, *, suffix: str) -> Engine:
    """Create a fresh per-universe SQLite engine with every schema seeded.

    Layers the Slice 1, Slice 2 planning, Slice 3 execution, Slice 3
    deliverable, and Slice 4 outcome schemas in the same order
    :func:`walking_slice.app.create_app` uses so triggers and FK constraints
    match production. Also seeds the ``slice-default-2026`` Disclosure Policy
    and the Slice 4 ``Disclosure_Policy_Coverage`` rows so the navigator's
    redaction-marker / gap-descriptor code paths resolve.
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
    create_outcome_schema(engine)
    seed_disclosure_policies(engine)
    with engine.begin() as conn:
        seed_outcome_coverage(conn, clock=FixedClock(_NOW))
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
    """Seed the three Party rows the test references.

    The same Parties are seeded in both universes so the only persistence
    difference is the presence or absence of the target Slice 4 entity ``R``.
    Identifier opacity (Requirement 1.7) means the rows are FK targets only;
    the API surface does not re-emit the Party display name.
    """
    with engine.begin() as conn:
        _seed_party(conn, _REQUESTER_PARTY_ID, "Property 54 Requester")
        _seed_party(conn, _AUTHORING_PARTY_ID, "Property 54 Authoring Party")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Property 54 Steward")


def _grant_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
) -> None:
    """Grant the requesting Party ``view`` authority on ``scope``.

    The scope value is :data:`_REQUESTER_SCOPE` — a scope that does *not*
    cover the Slice 4 entity seeded under :data:`_RESTRICTED_SCOPE` in
    Universe X. The role assignment is issued in both universes so the
    Parties' authority *set* is byte-equivalent across them; the only
    difference is whether the target entity exists.
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
# Universe X seeders — persist the target entity under a restricted scope.
#
# Each seeder builds the minimum FK chain required to land the target row
# under :data:`_RESTRICTED_SCOPE`. The requesting Party ``P'`` lacks view
# authority on that scope, so the navigator's / projection's head-node
# authorization gate denies the request and surfaces the same exception that
# is raised for a non-existent identifier (AD-WS-9).
# ---------------------------------------------------------------------------


def _seed_outcome_review(engine: Engine, ids: dict) -> None:
    """Seed one Outcome Review Record under the restricted scope.

    ``attribution_stance = 'Unattributed'`` so the schema CHECK allows an
    empty attribution-evidence reference (only ``Asserted`` / ``Contradicted``
    require one).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Outcome_Review_Records (
                    outcome_review_id, target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id, review_outcome,
                    attribution_stance, confidence, review_rationale,
                    attribution_evidence_reference, reviewing_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rid, :res, :rev, 'Inconclusive', 'Unattributed', 'Low',
                    'Property 54 review rationale.', '', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "rid": ids["outcome_review_id"],
                "res": ids["intended_outcome_resource_id"],
                "rev": ids["intended_outcome_revision_id"],
                "party": _AUTHORING_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_measurement_definition_chain(engine: Engine, ids: dict) -> None:
    """Seed Measurement Definition Resource + Revision (FK targets of a Record)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Measurement_Definitions (
                    measurement_definition_id,
                    target_intended_outcome_resource_id, created_at
                ) VALUES (:did, :res, :ts)
                """
            ),
            {
                "did": ids["measurement_definition_id"],
                "res": ids["intended_outcome_resource_id"],
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Measurement_Definition_Revisions (
                    measurement_definition_revision_id,
                    measurement_definition_id,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    measurand_description, unit_of_measure, observation_window,
                    cadence, data_source, authoring_party_id, applicable_scope,
                    recorded_at
                ) VALUES (
                    :rev, :did, :res, :iorev, 'Adoption rate.', 'percent',
                    '30 days', 'monthly', 'analytics', :party, :scope, :ts
                )
                """
            ),
            {
                "rev": ids["measurement_definition_revision_id"],
                "did": ids["measurement_definition_id"],
                "res": ids["intended_outcome_resource_id"],
                "iorev": ids["intended_outcome_revision_id"],
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_measurement_record(engine: Engine, ids: dict) -> None:
    """Seed one native Measurement Record under the restricted scope."""
    _seed_measurement_definition_chain(engine, ids)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Measurement_Records (
                    measurement_record_id, target_measurement_definition_id,
                    target_measurement_definition_revision_id, origin,
                    observed_value, observed_value_unit, observation_time,
                    source_system_id, source_system_record_id,
                    source_system_authority, source_system_retrieval_at,
                    import_at, recording_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :mrid, :did, :rev, 'native', '42', 'percent', :obs,
                    NULL, NULL, NULL, NULL, NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "mrid": ids["measurement_record_id"],
                "did": ids["measurement_definition_id"],
                "rev": ids["measurement_definition_revision_id"],
                "obs": _OBS_ISO,
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_observed_outcome_revision(engine: Engine, ids: dict) -> None:
    """Seed Observed Outcome Resource + initial Revision under restricted scope."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Observed_Outcomes (
                    observed_outcome_id, target_intended_outcome_resource_id,
                    created_at
                ) VALUES (:ooid, :res, :ts)
                """
            ),
            {
                "ooid": ids["observed_outcome_id"],
                "res": ids["intended_outcome_resource_id"],
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Observed_Outcome_Revisions (
                    observed_outcome_revision_id, observed_outcome_id,
                    outcome_kind, target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id, assessment_summary,
                    predecessor_revision_id, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :ooid, 'observed', :res, :iorev,
                    'Adoption trending up.', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": ids["observed_outcome_revision_id"],
                "ooid": ids["observed_outcome_id"],
                "res": ids["intended_outcome_resource_id"],
                "iorev": ids["intended_outcome_revision_id"],
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_intended_outcome_revision(engine: Engine, ids: dict) -> None:
    """Seed Objective + Intended Outcome Resource + Revision under restricted scope.

    The Intended Outcome Revision is the projection target; ``outcome_kind``
    is the literal ``'intended'`` so it is a valid outcome-status target (a
    wrong-kind target would also raise the same indistinguishable exception,
    but we seed a structurally valid target so the *authority* gate — not the
    wrong-kind gate — is the branch under test in Universe X).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": ids["objective_id"], "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                "INSERT INTO Intended_Outcomes (intended_outcome_id, created_at) "
                "VALUES (:ioid, :ts)"
            ),
            {"ioid": ids["intended_outcome_resource_id"], "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Intended_Outcome_Revisions (
                    intended_outcome_revision_id, intended_outcome_id,
                    parent_revision_id, outcome_kind, target_objective_id,
                    success_condition, observation_window,
                    attribution_assumption, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :ioid, NULL, 'intended', :oid,
                    'Onboarding completes in under two days.', '30 days',
                    'Sampling held constant.', :party, :scope, :ts
                )
                """
            ),
            {
                "rev": ids["intended_outcome_revision_id"],
                "ioid": ids["intended_outcome_resource_id"],
                "oid": ids["objective_id"],
                "party": _AUTHORING_PARTY_ID,
                "scope": _RESTRICTED_SCOPE,
                "ts": _NOW_ISO,
            },
        )


_SEEDERS: Final[dict] = {
    "outcome_review": _seed_outcome_review,
    "measurement_record": _seed_measurement_record,
    "observed_outcome_revision": _seed_observed_outcome_revision,
    "intended_outcome_revision": _seed_intended_outcome_revision,
}


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

    The Slice 4 provenance / projection routes resolve ``ctx.party_id``,
    ``ctx.engine``, ``ctx.clock``, and ``ctx.authz`` from a
    :class:`RequestContext`. The full :class:`RequestContextResolver`
    validates bearer tokens; for this property test we populate the bundle
    directly with the universe's collaborators.
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


def _build_universe(
    tmp_dir: Path,
    *,
    suffix: str,
    seed_target: bool,
    target_kind: str,
    ids: dict,
) -> tuple[Engine, FastAPI]:
    """Build one universe: engine + parties + role + (optional) target entity.

    The function is universe-symmetric — Universe X passes
    ``seed_target=True``, Universe Y passes ``seed_target=False``. Every
    other seeding step (Parties, role assignment) is identical so the only
    persistence difference is the presence or absence of the target entity.
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
    # Load the disclosure policy so the navigator can apply the AD-WS-9
    # redaction-marker / gap-descriptor rules; the head-node authority gate
    # (the branch under test) is independent of the policy, but wiring it
    # matches the production navigator and the Slice 1–3 property analogues.
    disclosure_policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
    navigator = ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
        disclosure_policy=disclosure_policy,
    )

    # Grant the requester view authority on :data:`_REQUESTER_SCOPE`. This is
    # the *same* role assignment in both universes — the only persistence
    # difference between X and Y is whether ``R`` is seeded (and ``R``'s scope
    # is :data:`_RESTRICTED_SCOPE`, which is *not* covered by this grant).
    _grant_view_authority(authorization_service, engine, scope=_REQUESTER_SCOPE)

    if seed_target:
        _SEEDERS[target_kind](engine, ids)

    app = FastAPI()
    app.include_router(outcome_router)
    app.state.engine = engine
    overrides = app.dependency_overrides
    overrides[outcome_get_engine] = lambda: engine
    overrides[get_provenance_navigator] = lambda: navigator
    overrides[get_request_context] = _make_request_context_dependency(
        engine,
        clock,
        identity_service,
        authorization_service,
        audit_log,
    )
    return engine, app


# ---------------------------------------------------------------------------
# Hypothesis strategy.
# ---------------------------------------------------------------------------


# Canonical lowercase hyphenated UUIDv7 strings, drawn *through* Hypothesis so
# the engine has real entropy to generate ≥ 100 distinct examples (minting the
# identifiers outside ``draw`` would collapse the input space to the four
# ``target_kind`` values and Hypothesis would stop after four cases). The
# target identifiers vary across cases while remaining identical across the two
# universes of a single case, so the message-wording / WHERE-clause behaviour
# is exercised over a broad identifier range. ``st.uuids`` only emits versions
# 1–5, so the version nibble is reshaped to ``7`` to keep the canonical
# UUIDv7 8-4-4-4-12 form (the variant nibble is already 8–b for a v4 draw).
def _reshape_to_v7(value) -> str:
    """Rewrite a drawn UUID string's version nibble to ``7`` (UUIDv7 form)."""
    text_value = str(value)
    return f"{text_value[:14]}7{text_value[15:]}"


_UUID7_STR: Final = st.uuids().map(_reshape_to_v7)

_ID_KEYS: Final[tuple[str, ...]] = (
    "outcome_review_id",
    "measurement_definition_id",
    "measurement_definition_revision_id",
    "measurement_record_id",
    "observed_outcome_id",
    "observed_outcome_revision_id",
    "objective_id",
    "intended_outcome_resource_id",
    "intended_outcome_revision_id",
)


@st.composite
def _scenario(draw) -> dict:
    """Draw one Property 54 scenario.

    A scenario carries the ``target_kind`` (drawn uniformly from the four
    implemented indistinguishable surfaces) and a bundle of UUIDv7 identifiers
    — drawn through Hypothesis — for every row seeded in Universe X (and for
    the URL path parameters in Universe Y). The target Identity is the *same*
    in both universes so the lookups produce a deterministic query, but it
    varies across cases so Hypothesis explores ≥ 100 distinct examples.
    """
    target_kind = draw(st.sampled_from(_TARGET_KINDS))
    ids = {key: draw(_UUID7_STR) for key in _ID_KEYS}
    return {"target_kind": target_kind, "ids": ids}


def _request_url_and_label(target_kind: str, ids: dict) -> tuple[str, str]:
    """Build the HTTP URL for the target Slice 4 read surface."""
    if target_kind == "outcome_review":
        return (
            f"/api/v1/outcome-reviews/{ids['outcome_review_id']}/provenance",
            "GET /outcome-reviews/{id}/provenance",
        )
    if target_kind == "measurement_record":
        return (
            f"/api/v1/measurement-records/{ids['measurement_record_id']}"
            f"/provenance",
            "GET /measurement-records/{id}/provenance",
        )
    if target_kind == "observed_outcome_revision":
        return (
            f"/api/v1/observed-outcomes/{ids['observed_outcome_id']}"
            f"/revisions/{ids['observed_outcome_revision_id']}/provenance",
            "GET /observed-outcomes/{id}/revisions/{rid}/provenance",
        )
    if target_kind == "intended_outcome_revision":
        return (
            f"/api/v1/intended-outcomes/{ids['intended_outcome_revision_id']}"
            f"/outcome-status",
            "GET /intended-outcomes/{rid}/outcome-status",
        )
    raise AssertionError(  # pragma: no cover - defensive
        f"Unknown target_kind: {target_kind!r}"
    )


def _drive_request(app: FastAPI, url: str) -> tuple[int, dict, bytes, float]:
    """Drive one GET request and return ``(status, body, raw_bytes, latency)``."""
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


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


@given(scenario=_scenario())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_indistinguishable_denial_across_slice4_endpoints(
    scenario: dict,
) -> None:
    """P'`s denial response on R is byte-equivalent to a non-existent universe.

    For each scenario the test:

    1. Constructs two on-disk SQLite engines, one per universe, and stands up
       a minimal FastAPI app over each (Universe X / Y).
    2. Seeds the same Parties and the same ``view`` role assignment on
       :data:`_REQUESTER_SCOPE` in both universes; Universe X additionally
       seeds the target Slice 4 entity ``R`` under :data:`_RESTRICTED_SCOPE`
       so the requesting Party lacks view authority on it.
    3. Drives the same GET request to both apps and captures the status code,
       the parsed JSON body, the raw response bytes, and the wall-clock
       latency.
    4. Asserts the dimensions from the property statement — error category,
       body keys, body content (covers identifier set, ordering, pagination
       cursor, error wording, and result count), response size, and latency
       within :data:`_LATENCY_TOLERANCE_SECONDS`.
    """
    target_kind = scenario["target_kind"]
    ids = scenario["ids"]
    url, endpoint_label = _request_url_and_label(target_kind, ids)

    with tempfile.TemporaryDirectory(prefix="walking_slice_prop54_") as raw_tmp:
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

            # ----- Error-category dimension --------------------------------
            # A restricted target and a non-existent target must produce the
            # same HTTP status code; any divergence is the canonical AD-WS-9
            # leak (Requirement 50.5 / 58.3).
            assert status_x == status_y, (
                f"Property 54 violated on the error-category dimension "
                f"({endpoint_label}): Universe X returned status {status_x}, "
                f"Universe Y returned status {status_y}. "
                f"X body={body_x!r}; Y body={body_y!r}."
            )

            # ----- Body-keys dimension -------------------------------------
            # The set of top-level keys in the JSON response body must be
            # identical; a divergence would mean one branch populated a field
            # the other did not (a restricted-only attribute leaking through).
            assert set(body_x.keys()) == set(body_y.keys()), (
                f"Property 54 violated on the body-keys dimension "
                f"({endpoint_label}): X-only keys="
                f"{sorted(set(body_x.keys()) - set(body_y.keys()))!r}, "
                f"Y-only keys="
                f"{sorted(set(body_y.keys()) - set(body_x.keys()))!r}."
            )

            # ----- Body content / identifier-set / ordering / cursor /
            # error-wording / count dimension ------------------------------
            # The denial body is a flat dict on the indistinguishable
            # branches so a deep equality covers every observable within the
            # body: the unresolvable identifier surfaced in the message, the
            # error code, the wording, and any optional pagination cursor.
            # The result count is implicit (a 404 carries no results array, so
            # the count is trivially zero in both universes).
            assert body_x == body_y, (
                f"Property 54 violated on the body-content dimension "
                f"({endpoint_label}): X={body_x!r}, Y={body_y!r}."
            )

            # ----- Response-size dimension ---------------------------------
            # Asserted on the raw byte stream so the failure message names the
            # dimension when the JSON bodies happen to be equal but their
            # serialized forms differ (Requirement 50.7 / 58.4).
            assert len(raw_x) == len(raw_y), (
                f"Property 54 violated on the response-size dimension "
                f"({endpoint_label}): "
                f"len(X.content)={len(raw_x)}, len(Y.content)={len(raw_y)}."
            )

            # ----- Latency dimension ---------------------------------------
            # The 100 ms tolerance is taken verbatim from Requirements 50.7 /
            # 58.4. Both universes return 404s with byte-equivalent bodies on
            # the indistinguishable branch; the latency delta therefore
            # reflects only request-handling jitter rather than work that
            # depends on the existence of ``R``.
            latency_delta = abs(latency_x - latency_y)
            assert latency_delta <= _LATENCY_TOLERANCE_SECONDS, (
                f"Property 54 violated on the latency dimension "
                f"({endpoint_label}): "
                f"X={latency_x * 1000:.1f} ms, Y={latency_y * 1000:.1f} ms, "
                f"|delta|={latency_delta * 1000:.1f} ms exceeds the "
                f"{_LATENCY_TOLERANCE_SECONDS * 1000:.0f} ms tolerance."
            )
        finally:
            if engine_x is not None:
                engine_x.dispose()
            if engine_y is not None:
                engine_y.dispose()
