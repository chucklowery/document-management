# Feature: second-walking-slice, Property 18: Indistinguishable denial across planning endpoints
"""Property 18 — Indistinguishable denial across planning endpoints (task 16.3).

**Property 18: Indistinguishable denial across planning endpoints**

For all pairs ``(P, P')`` of Parties differing only in authority on a
planning Resource ``R``, the response visible to ``P'`` for *backlink*
and *plan-approval-provenance* attempts on ``R`` is indistinguishable
from the response ``P'`` would receive in a universe where ``R`` does
not exist, across:

- count
- identifier set
- ordering positions
- pagination cursors
- response size
- response body keys
- error category
- error wording
- latency baseline (within 100 ms tolerance)

**Validates: Requirements 10.1, 10.4, 10.5, 10.7, 14.3, 14.7, 15.3,
15.5, 17.2, 17.3, 17.4, 20.8**

Strategy
--------

Each Hypothesis case draws a *graph* that targets one of the seven
Slice 2 planning Resource kinds (Plan Approval, Plan Revision,
Activity Plan, Project Revision, Objective Revision, Intended Outcome
Revision, Deliverable Expectation Revision). One to six inbound
``Relationships`` rows reference the target, plus one *restricted
index* naming the Relationship whose source endpoint ``R`` is the
node ``P'`` lacks effective authority on. The case also draws an
*operation kind* — ``"backlink"`` or ``"provenance"`` — selecting
which navigator surface is exercised; the provenance branch only
applies when the target kind is ``plan_approval`` because
:meth:`ProvenanceNavigator.navigate_plan_approval` is rooted at a
``Plan_Approval_Records`` row by definition.

Per case the test stands up two universes on separate on-disk SQLite
files, mirroring the Slice 1 Property 4 idiom
(:mod:`tests.property.test_property_4_non_leakage`). The two
universes differ *only* in whether ``R`` (the node ``P'`` lacks
authority on) exists:

- **Universe X** — every drawn Relationship is persisted, *including*
  the one whose source endpoint is the restricted ``R``. When the
  operation is ``provenance``, the target ``Plan_Approval_Records``
  row is persisted as well. ``P'`` is granted ``view`` authority on
  every *non-restricted* source endpoint's scope. ``P'`` is **never**
  granted view authority on ``R`` itself (nor — for the ``provenance``
  branch — on the target Plan Approval's scope), so the
  :class:`~walking_slice.provenance.ProvenanceNavigator` filters the
  ``R``-sourced Relationship out of the authorized projection
  (backlinks) or raises :class:`PlanApprovalUnresolvableError` with
  the head node invisible (provenance).

- **Universe Y** — every drawn Relationship is persisted **except**
  the one whose source endpoint is ``R`` (which, by construction,
  *does not exist* in this universe); for the ``provenance`` branch,
  the target ``Plan_Approval_Records`` row is *also* not persisted.
  ``P'`` is granted the same set of view-authority role assignments
  as in Universe X. The target Identity is the same canonical
  UUIDv7 in both universes so the navigator's ``WHERE`` clauses
  produce a single, deterministic query.

The test then exercises one of two navigator surfaces:

- ``backlink``: :meth:`ProvenanceNavigator.list_backlinks` against the
  drawn target endpoint. The :class:`BacklinkPage` returned must be
  byte-equivalent across the universes along every dimension
  (Requirements 15.3 / 15.5).

- ``provenance``: :meth:`ProvenanceNavigator.navigate_plan_approval`
  against the drawn Plan Approval Identity. Both universes must raise
  :class:`PlanApprovalUnresolvableError` carrying the same
  unresolvable reference and the same message (Requirement 14.7) —
  Universe X because ``P'`` lacks ``view.plan_approval`` authority on
  the persisted row, Universe Y because the row does not exist.

The seven AD-WS-9 dimensions named in the property statement are
checked explicitly for backlink responses (count, identifier set,
ordering, cursor, response size, latency within 100 ms) and for
provenance responses (error category, error wording, latency).

Why focus on backlinks and plan-approval provenance
---------------------------------------------------

The "indistinguishable denial" discipline lives at the
:class:`ProvenanceNavigator` layer for the read paths
(:meth:`list_backlinks`, :meth:`navigate_plan_approval`). These are
the read-surface counterparts of Slice 1 Property 4 / Requirement 8.3
extended to Slice 2 planning Resource kinds — they are the *only*
Slice 2 surfaces where one navigator call alone produces a complete
"response" whose dimensions (count, identifier set, ordering, cursor,
response size, error category, error wording, latency) are fully
defined. The creation, review, and approval write paths
(:meth:`ObjectiveService.create_objective`,
:meth:`PlanReviewService.create_plan_review`,
:meth:`PlanApprovalService.create_plan_approval`, etc.) emit denial
responses through the AD-WS-9 shape-stable
:class:`~walking_slice.planning._routes.DenialResponseBody` whose
*per-call* indistinguishability across reason codes is pinned by
:mod:`tests.unit.test_authorization_denial_shape` and
:mod:`tests.unit.test_unauthorized_decision_denial`; their HTTP-layer
*against-non-existent-target* indistinguishability awaits the
``walking_slice.provenance._shape_response_constant_time`` helper
described in design §"Disclosure policy enforcement on error responses",
which is not yet implemented (see backlog ADR ``ADR-HT-009`` / Gap
G-7). Property 18 as stated here pins the dimensions that the
implemented surfaces *do* normalize today; the design-mandated full
HTTP-layer normalization is tracked separately.

Restricted-vs-nonexistent normalization rests on the same navigator
invariants Property 4 verifies for Slice 1 source kinds and which
:func:`walking_slice.planning._disclosure.seed_planning_coverage`
(task 1.4) extends to every Slice 2 planning node kind via the
``slice-default-2026`` ``Disclosure_Policy_Coverage`` rows.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Optional

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.disclosure import seed as seed_disclosure_policies
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import seed_planning_coverage
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import (
    BacklinkPage,
    PlanApprovalUnresolvableError,
    ProvenanceNavigator,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


# All universes share the same fixed-clock instant so role-assignment
# evaluations and Relationship ``recorded_at`` values are byte-equivalent
# across X and Y.
_BASE_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_BASE_TIME_ISO: Final[str] = format_iso8601_ms(_BASE_TIME)

# The instant used to evaluate authority on every navigator call. Sits
# inside every role assignment's effective period by construction.
_EVALUATION_TIME: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START: Final[datetime] = _BASE_TIME

# Pre-fixed actor / authoring / steward Identities. The target Identity
# is drawn per case so two cases never collide on the same target.
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000002"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000000003"
_APPROVING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000004"

# Fixed scope on every Slice 2 planning row seeded in the universe.
# ``P'`` is *not* granted ``view`` on this scope, so authority lookups
# for the target row (provenance branch) fall through to the
# unresolvable path; ``P'`` *is* granted view on every per-source scope
# drawn by the strategy (one per non-restricted Relationship), so the
# authorized projection contains the non-restricted Relationships only.
_TARGET_SCOPE: Final[str] = "pilot/team-target"


# The five Relationship types permitted by the schema CHECK on
# ``Relationships.relationship_type``. The strategy samples from this
# set so the property is exercised across the full type alphabet —
# restricted information must be invisible regardless of which
# Relationship type carries it.
_RELATIONSHIP_TYPES: Final[tuple[str, ...]] = (
    "Supports",
    "Contradicts",
    "Derived From",
    "Addresses",
    "Supersedes",
)

# Source endpoint kinds the slice's :class:`ProvenanceNavigator` scopes
# ``view.<source_kind>`` authority over. These are the Slice 2 planning
# kinds plus a few Slice 1 source kinds so cross-context Relationships
# are also exercised. The list mirrors :data:`_AUTHORIZED_SOURCE_KINDS`
# in :mod:`walking_slice.provenance`.
_SOURCE_KINDS: Final[tuple[str, ...]] = (
    "objective_revision",
    "intended_outcome_revision",
    "project_revision",
    "deliverable_expectation_revision",
    "activity_plan",
    "plan_revision",
    "plan_review_revision",
    "plan_approval",
    # Slice 1 kinds — Property 24 (Slice 2 backlinks) explicitly covers
    # cross-context Relationships from Slice 1 sources into Slice 2
    # targets, so include them here to exercise the same indistinguishability
    # contract over those edges.
    "finding_revision",
    "recommendation_revision",
    "decision",
)

# Target endpoint kinds drawn per case. Each maps onto either an
# inbound-Relationship backlink test (every kind) or the
# plan-approval-provenance test (``plan_approval`` only).
_TARGET_KINDS: Final[tuple[str, ...]] = (
    "plan_approval",
    "plan_revision",
    "activity_plan",
    "project_revision",
    "objective_revision",
    "intended_outcome_revision",
    "deliverable_expectation_revision",
)

# Latency-baseline tolerance from the property statement. Requirement
# 14.7 / 15.5 / 17.4 pin the tolerance at 100 ms. The slice's
# :func:`compute_latency_baseline_seconds` is a pure deterministic
# function of the authorized response size, so the practical
# difference is zero — the tolerance is the property's safety net for
# future implementations that introduce a small amount of jitter.
_LATENCY_TOLERANCE_SECONDS: Final[float] = 0.1


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _fresh_uuid7() -> str:
    """Return one fresh UUIDv7 string.

    Used inside Hypothesis composites to mint per-draw identifiers
    (Relationship Identities, source endpoint Identities, target
    endpoint Identities). Each call returns a distinct value so drawn
    graphs do not accidentally collide on identifier values across
    scenarios.
    """
    return str(uuid_utils.uuid7())


def _format_offset(offset_seconds: int) -> str:
    """Return an ISO-8601 ms-precision timestamp ``offset_seconds`` past base."""
    return format_iso8601_ms(_BASE_TIME + timedelta(seconds=offset_seconds))


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert one Party row required by the FK constraints."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _BASE_TIME_ISO},
    )


def _seed_required_parties(engine: Engine) -> None:
    """Seed the requester, the authoring Party, the steward, and the approver."""
    with engine.begin() as conn:
        _seed_party(conn, _REQUESTER_PARTY_ID, "Property 18 Requester")
        _seed_party(conn, _AUTHORING_PARTY_ID, "Property 18 Authoring Party")
        _seed_party(
            conn, _ASSIGNING_AUTHORITY_ID, "Property 18 Assigning Authority"
        )
        _seed_party(conn, _APPROVING_PARTY_ID, "Property 18 Approving Party")



def _insert_relationship(engine: Engine, *, descriptor: dict, target_kind: str, target_id: str) -> None:
    """Persist one Relationship row from a strategy-drawn descriptor.

    Bypasses :class:`KnowledgeService` and the Planning_Service write
    paths so the test can fabricate arbitrary
    ``(source_kind, relationship_type, recorded_at, target_kind)``
    combinations independent of the slice's natural pipelines. The
    ``Relationships`` table has no FK constraint on ``source_id``,
    ``source_revision_id``, ``target_id``, or ``target_revision_id``
    (only ``authoring_party_id`` is FK-constrained), so the strategy's
    fresh UUIDv7 identifiers are acceptable for every other column.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type, source_kind,
                    source_id, source_revision_id, target_kind,
                    target_id, target_revision_id, authoring_party_id,
                    recorded_at
                ) VALUES (
                    :relationship_id, :relationship_type, :source_kind,
                    :source_id, :source_revision_id, :target_kind,
                    :target_id, :target_revision_id, :authoring_party_id,
                    :recorded_at
                )
                """
            ),
            {
                "relationship_id": descriptor["relationship_id"],
                "relationship_type": descriptor["relationship_type"],
                "source_kind": descriptor["source_kind"],
                "source_id": descriptor["source_id"],
                "source_revision_id": descriptor["source_revision_id"],
                "target_kind": target_kind,
                "target_id": target_id,
                "target_revision_id": None,
                "authoring_party_id": _AUTHORING_PARTY_ID,
                "recorded_at": _format_offset(
                    descriptor["recorded_at_offset_seconds"]
                ),
            },
        )


def _seed_plan_approval_row(
    engine: Engine,
    *,
    plan_approval_id: str,
    activity_plan_id: str,
    plan_revision_id: str,
    project_id: str,
) -> None:
    """Seed a minimal Plan Approval Record + its three ancestor rows.

    Used only by the ``provenance`` operation branch: the navigator's
    :meth:`navigate_plan_approval` loads ``Plan_Approval_Records`` by
    ``plan_approval_id``, then evaluates ``view.plan_approval``
    authority on the resolved row's ``applicable_scope``. The ancestor
    rows (Project, Activity Plan, Plan Revision) are seeded so the
    Plan Approval row's FKs resolve; only the head node's
    authorization gate is exercised because both universes raise the
    same :class:`PlanApprovalUnresolvableError` *before* the chain
    walks past the head.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": project_id, "ts": _BASE_TIME_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 18 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": activity_plan_id,
                "pid": project_id,
                "party": _AUTHORING_PARTY_ID,
                "scope": _TARGET_SCOPE,
                "ts": _BASE_TIME_ISO,
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
                    'Property 18 planned scope.', '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "party": _AUTHORING_PARTY_ID,
                "scope": _TARGET_SCOPE,
                "ts": _BASE_TIME_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :pa, :aid, :rev, 'Approve',
                    'Property 18 Plan Approval rationale.',
                    :party, 'role-grant-id',
                    '00000000-0000-7000-8000-00000000000a',
                    :scope, :ts
                )
                """
            ),
            {
                "pa": plan_approval_id,
                "aid": activity_plan_id,
                "rev": plan_revision_id,
                "party": _APPROVING_PARTY_ID,
                "scope": _TARGET_SCOPE,
                "ts": _BASE_TIME_ISO,
            },
        )


def _grant_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
) -> None:
    """Grant ``view`` authority for ``scope`` to the requesting Party.

    The scope value matches the convention used by
    :meth:`ProvenanceNavigator._build_authorized_projection`: the source
    endpoint's Resource identity. One role assignment is recorded per
    non-restricted source so the requesting Party can view exactly
    those source endpoints and nothing else — *no* wildcard scope is
    used here because a wildcard would also grant view on the
    restricted source (and on the target Plan Approval), collapsing
    the universes.
    """
    request = AssignRoleRequest(
        party_id=_REQUESTER_PARTY_ID,
        role_name="reviewer",
        scope=scope,
        authorities_granted=("view",),
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _build_engine(tmp_dir: Path, *, suffix: str) -> Engine:
    """Create a fresh per-universe SQLite engine with the Slice 2 schema seeded.

    The two universes per case share a parent
    :class:`tempfile.TemporaryDirectory` but live in separate
    sub-paths so each universe owns its own on-disk file. WAL journal
    mode and ``foreign_keys=ON`` match the runtime configuration set
    in ``tests/conftest.py`` and design §"Persistence Invariants
    Summary".

    The Slice 1 schema (``create_schema``), the Slice 2 schema
    (``create_planning_schema``), the ``slice-default-2026``
    Disclosure_Policies row (``seed_disclosure_policies``), and the
    Slice 2 Disclosure_Policy_Coverage rows
    (``seed_planning_coverage``) are all installed so the navigator's
    AD-WS-9 / Requirement 17 lookups resolve.
    """
    db_path = tmp_dir / f"walking_slice_{suffix}.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    create_planning_schema(engine)
    seed_disclosure_policies(engine)
    with engine.begin() as conn:
        seed_planning_coverage(conn)
    return engine


def _build_universe(
    tmp_dir: Path,
    *,
    suffix: str,
    descriptors: list[dict],
    granted_scopes: list[str],
    target_kind: str,
    target_id: str,
    seed_target_row: bool,
    plan_approval_ancestor_ids: Optional[dict] = None,
) -> tuple[Engine, ProvenanceNavigator]:
    """Stand up one universe: engine + Parties + Relationships + role grants.

    The function is universe-symmetric — passing the full descriptor
    list with ``seed_target_row=True`` yields Universe X, passing an
    empty descriptor list with ``seed_target_row=False`` yields
    Universe Y. ``granted_scopes`` is the *same* list in both calls
    (the requesting Party's view authority differs only by what is
    reachable, not by what the role assignments claim — that is what
    makes ``(P, P')`` differ only in view authority on one node).
    """
    engine = _build_engine(tmp_dir, suffix=suffix)
    _seed_required_parties(engine)

    # Fresh per-universe collaborators. The :class:`IdentityService`
    # in-memory ``Identifier_Registry`` cache is per-instance, so
    # using one navigator per universe keeps the role-assignment
    # identifiers distinct across X and Y.
    clock = FixedClock(_BASE_TIME)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    navigator = ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )

    if seed_target_row and target_kind == "plan_approval":
        assert plan_approval_ancestor_ids is not None
        _seed_plan_approval_row(
            engine,
            plan_approval_id=target_id,
            activity_plan_id=plan_approval_ancestor_ids["activity_plan_id"],
            plan_revision_id=plan_approval_ancestor_ids["plan_revision_id"],
            project_id=plan_approval_ancestor_ids["project_id"],
        )

    for descriptor in descriptors:
        _insert_relationship(
            engine,
            descriptor=descriptor,
            target_kind=target_kind,
            target_id=target_id,
        )

    for scope in granted_scopes:
        _grant_view_authority(authorization_service, engine, scope=scope)

    return engine, navigator



# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


@st.composite
def _relationship_descriptor(draw) -> dict:
    """Draw one Relationship descriptor with fresh identifiers.

    The descriptor mirrors the structure used by Property 4
    (:mod:`tests.property.test_property_4_non_leakage`) and adds no
    target-side fields — the target endpoint is supplied by the
    scenario-level draw so each Relationship in a case shares the
    same target Identity.
    """
    return {
        "relationship_id": _fresh_uuid7(),
        "source_id": _fresh_uuid7(),
        "source_revision_id": draw(
            st.one_of(st.none(), st.builds(_fresh_uuid7))
        ),
        "source_kind": draw(st.sampled_from(_SOURCE_KINDS)),
        "relationship_type": draw(st.sampled_from(_RELATIONSHIP_TYPES)),
        "recorded_at_offset_seconds": draw(
            st.integers(min_value=0, max_value=86_400)
        ),
    }


@st.composite
def _scenario(draw) -> dict:
    """Draw one Property 18 scenario.

    A scenario carries:

    - ``target_kind``: the planning Resource kind that ``R`` belongs
      to (and that every Relationship in ``descriptors`` targets via
      its ``target_kind`` column).
    - ``target_id``: the canonical UUIDv7 used for ``R`` in both
      universes. Identical across X and Y so the navigator's
      ``WHERE target_id = :target_id`` clause produces a deterministic
      query.
    - ``operation``: ``"backlink"`` (drives
      :meth:`ProvenanceNavigator.list_backlinks`) or ``"provenance"``
      (drives :meth:`ProvenanceNavigator.navigate_plan_approval`,
      only valid when ``target_kind == 'plan_approval'``).
    - ``descriptors``: 1..6 Relationship descriptors with unique
      ``recorded_at_offset_seconds`` values so the
      ``ORDER BY (recorded_at, relationship_id)`` sort is unambiguous.
    - ``restricted_index``: index into ``descriptors`` naming the
      Relationship whose source endpoint the requesting Party may not
      view. In Universe X every Relationship is persisted; in Universe
      Y no Relationships are persisted, so the test compares
      "restricted in X" against "none exist in Y" — the property
      asserts these are indistinguishable.

    The minimum draw size of 1 ensures every case has at least one
    Relationship whose restriction status is exercised; the maximum
    of 6 keeps each case cheap enough that the 100-example Hypothesis
    run completes well under the slice's deadline budget.
    """
    target_kind = draw(st.sampled_from(_TARGET_KINDS))
    operation = draw(
        st.sampled_from(("backlink", "provenance"))
        if target_kind == "plan_approval"
        else st.just("backlink")
    )
    descriptors = draw(
        st.lists(
            _relationship_descriptor(),
            min_size=1,
            max_size=6,
            unique_by=lambda d: d["recorded_at_offset_seconds"],
        )
    )
    restricted_index = draw(
        st.integers(min_value=0, max_value=len(descriptors) - 1)
    )
    target_id = _fresh_uuid7()
    plan_approval_ancestor_ids = (
        {
            "activity_plan_id": _fresh_uuid7(),
            "plan_revision_id": _fresh_uuid7(),
            "project_id": _fresh_uuid7(),
        }
        if target_kind == "plan_approval"
        else None
    )
    return {
        "target_kind": target_kind,
        "target_id": target_id,
        "operation": operation,
        "descriptors": descriptors,
        "restricted_index": restricted_index,
        "plan_approval_ancestor_ids": plan_approval_ancestor_ids,
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


@given(scenario=_scenario())
@settings(
    max_examples=100,
    deadline=2000,
    # Each case provisions two on-disk SQLite databases and seeds two
    # universes; per-case setup is more expensive than a purely
    # in-memory test. The setup is well under the 2000 ms deadline
    # locally but the data-generation and slow-test health checks are
    # suppressed so any one slow case does not abort the property run.
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_indistinguishable_denial_across_planning_endpoints(scenario: dict) -> None:
    """``P'``'s response on ``R`` is indistinguishable from a non-existent universe."""
    descriptors: list[dict] = scenario["descriptors"]
    restricted_index: int = scenario["restricted_index"]
    target_kind: str = scenario["target_kind"]
    target_id: str = scenario["target_id"]
    operation: str = scenario["operation"]
    ancestor_ids = scenario["plan_approval_ancestor_ids"]

    # The Relationship whose source endpoint ``R`` does not exist in
    # Universe Y. The strategy guarantees ``descriptors`` has at least
    # one entry so the index is always valid.
    restricted_descriptor = descriptors[restricted_index]
    restricted_source_id = restricted_descriptor["source_id"]

    # The scopes granted to ``P'`` — every source's identity *except*
    # the restricted one, identical across both universes (because the
    # property holds ``P`` and ``P'`` to differ only in authority on
    # the single node ``R``: the role-assignment set carried into
    # Universe Y is the same set carried into Universe X). ``P'`` is
    # *not* granted view authority on the target Plan Approval's
    # scope, so the provenance branch raises
    # :class:`PlanApprovalUnresolvableError` in Universe X for the
    # same reason it raises in Universe Y (Requirement 14.7).
    granted_scopes = [
        d["source_id"]
        for d in descriptors
        if d["source_id"] != restricted_source_id
    ]

    # Universe Y descriptors — every drawn Relationship *except* the
    # one whose source endpoint is the restricted ``R``. Multiple
    # descriptors may share the same ``source_id`` if Hypothesis
    # happens to draw a duplicate (the strategy uniqueness key is on
    # ``recorded_at_offset_seconds``, not ``source_id``), so the
    # filter is by ``source_id`` equality — any descriptor whose
    # source endpoint matches ``R``'s is dropped from Universe Y.
    # Were such a duplicate to remain in Y while still being
    # unreachable to ``P'``, Y would contain extra entries that X
    # never produced, breaking the indistinguishability check
    # artificially.
    universe_y_descriptors = [
        d for d in descriptors if d["source_id"] != restricted_source_id
    ]

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop18_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine_x: Optional[Engine] = None
        engine_y: Optional[Engine] = None
        try:
            engine_x, navigator_x = _build_universe(
                case_dir,
                suffix="x",
                descriptors=descriptors,
                granted_scopes=granted_scopes,
                target_kind=target_kind,
                target_id=target_id,
                seed_target_row=(operation == "provenance"),
                plan_approval_ancestor_ids=ancestor_ids,
            )
            # Universe Y omits the restricted descriptor (and, for the
            # provenance branch, also omits the target
            # ``Plan_Approval_Records`` row). Every other Relationship
            # is persisted so the non-restricted projection ``P'`` is
            # entitled to see is byte-equivalent across the universes.
            engine_y, navigator_y = _build_universe(
                case_dir,
                suffix="y",
                descriptors=universe_y_descriptors,
                granted_scopes=granted_scopes,
                target_kind=target_kind,
                target_id=target_id,
                seed_target_row=False,
                plan_approval_ancestor_ids=None,
            )

            if operation == "backlink":
                _assert_backlink_indistinguishability(
                    engine_x,
                    navigator_x,
                    engine_y,
                    navigator_y,
                    target_id=target_id,
                )
            elif operation == "provenance":
                _assert_provenance_indistinguishability(
                    engine_x,
                    navigator_x,
                    engine_y,
                    navigator_y,
                    plan_approval_id=target_id,
                )
            else:  # pragma: no cover - defensive
                raise AssertionError(f"Unknown operation: {operation!r}")
        finally:
            if engine_x is not None:
                engine_x.dispose()
            if engine_y is not None:
                engine_y.dispose()



# ---------------------------------------------------------------------------
# Per-operation assertion helpers.
# ---------------------------------------------------------------------------


def _assert_backlink_indistinguishability(
    engine_x: Engine,
    navigator_x: ProvenanceNavigator,
    engine_y: Engine,
    navigator_y: ProvenanceNavigator,
    *,
    target_id: str,
) -> None:
    """Drive :meth:`list_backlinks` against both universes and compare results.

    Asserts every dimension named in Property 18: count, identifier
    set, ordering positions, pagination cursors, response size, body
    keys, error category, and latency (within
    :data:`_LATENCY_TOLERANCE_SECONDS`). Both universes are expected
    to return a :class:`BacklinkPage` (no exception); a divergence on
    that axis alone would itself constitute a leak.
    """
    error_x: Optional[BaseException] = None
    error_y: Optional[BaseException] = None
    page_x: Optional[BacklinkPage] = None
    page_y: Optional[BacklinkPage] = None

    with engine_x.connect() as conn_x:
        try:
            page_x = navigator_x.list_backlinks(
                conn_x,
                target_id=target_id,
                target_revision_id=None,
                party_id=_REQUESTER_PARTY_ID,
                at=_EVALUATION_TIME,
            )
        except BaseException as exc:  # noqa: BLE001
            error_x = exc

    with engine_y.connect() as conn_y:
        try:
            page_y = navigator_y.list_backlinks(
                conn_y,
                target_id=target_id,
                target_revision_id=None,
                party_id=_REQUESTER_PARTY_ID,
                at=_EVALUATION_TIME,
            )
        except BaseException as exc:  # noqa: BLE001
            error_y = exc

    # ----- Error-category dimension --------------------------------
    # An exception raised by exactly one universe is itself a leak —
    # the exception's existence signals to ``P'`` which universe it
    # was in. Both raising the same exception (with identical
    # message) does not leak.
    assert (error_x is None) == (error_y is None), (
        "Property 18 violated on the error-category dimension for "
        "list_backlinks: one universe raised an exception while the "
        f"other returned a page. Universe X error={error_x!r}; "
        f"Universe Y error={error_y!r}."
    )
    if error_x is not None and error_y is not None:
        assert type(error_x) is type(error_y), (
            "Property 18 violated on the error-category dimension: "
            "list_backlinks raised different exception classes across "
            f"universes. X={type(error_x).__name__}, "
            f"Y={type(error_y).__name__}."
        )
        assert str(error_x) == str(error_y), (
            "Property 18 violated on the error-wording dimension: "
            "list_backlinks raised the same exception class but with "
            f"different messages. X={str(error_x)!r}, Y={str(error_y)!r}."
        )
        # When both raised, no page exists to compare.
        return

    assert page_x is not None and page_y is not None

    # ----- Count dimension ----------------------------------------
    assert page_x.response_size == page_y.response_size, (
        "Property 18 violated on the count dimension: list_backlinks "
        "response_size differs. "
        f"X={page_x.response_size}, Y={page_y.response_size}."
    )

    # ----- Identifier-set dimension -------------------------------
    id_set_x = {e.relationship_id for e in page_x.entries}
    id_set_y = {e.relationship_id for e in page_y.entries}
    assert id_set_x == id_set_y, (
        "Property 18 violated on the identifier-set dimension: the set "
        "of Relationship Identities returned by list_backlinks differs "
        f"across universes. X-only={id_set_x - id_set_y!r}, "
        f"Y-only={id_set_y - id_set_x!r}."
    )

    # ----- Ordering-positions dimension ---------------------------
    order_x = [e.relationship_id for e in page_x.entries]
    order_y = [e.relationship_id for e in page_y.entries]
    assert order_x == order_y, (
        "Property 18 violated on the ordering-positions dimension: the "
        "order of Relationship Identities in the list_backlinks page "
        f"differs across universes. X={order_x!r}, Y={order_y!r}."
    )

    # ----- Full-entry body-keys dimension -------------------------
    # ``BacklinkEntry`` is a frozen dataclass with a fixed field set,
    # so byte-equivalent entries imply byte-equivalent body keys at
    # the HTTP layer (which serializes each entry verbatim).
    assert page_x.entries == page_y.entries, (
        "Property 18 violated on the body-keys / per-attribute "
        "dimension: list_backlinks returned the same Relationship "
        "Identities but the BacklinkEntry payloads differ."
    )

    # ----- Pagination-cursors dimension ---------------------------
    assert page_x.cursor == page_y.cursor, (
        "Property 18 violated on the pagination-cursors dimension: the "
        "next-page cursor returned by list_backlinks differs. "
        f"X={page_x.cursor!r}, Y={page_y.cursor!r}."
    )

    # ----- Response-size dimension --------------------------------
    # Asserted separately so the failure message names the dimension
    # if ``response_size`` and ``len(entries)`` disagree.
    assert page_x.response_size == len(page_x.entries) == len(
        page_y.entries
    ) == page_y.response_size, (
        "Property 18 violated on the response-size dimension: "
        "list_backlinks ``response_size`` and ``len(entries)`` disagree "
        "across universes."
    )

    # ----- Latency dimension --------------------------------------
    latency_delta = abs(
        page_x.latency_baseline_seconds - page_y.latency_baseline_seconds
    )
    assert latency_delta <= _LATENCY_TOLERANCE_SECONDS, (
        "Property 18 violated on the latency dimension: the "
        "list_backlinks latency baseline differs by more than "
        f"{_LATENCY_TOLERANCE_SECONDS * 1000:.0f} ms. "
        f"X={page_x.latency_baseline_seconds}, "
        f"Y={page_y.latency_baseline_seconds}, "
        f"|delta|={latency_delta}."
    )


def _assert_provenance_indistinguishability(
    engine_x: Engine,
    navigator_x: ProvenanceNavigator,
    engine_y: Engine,
    navigator_y: ProvenanceNavigator,
    *,
    plan_approval_id: str,
) -> None:
    """Drive :meth:`navigate_plan_approval` against both universes and compare.

    Per Requirement 14.7 / design §"Provenance traversal algorithm"
    ``not_found_indistinguishable_response``, both the
    "Plan Approval exists but P' lacks view.plan_approval" case
    (Universe X) and the "Plan Approval Identity does not resolve"
    case (Universe Y) must raise the same
    :class:`PlanApprovalUnresolvableError` carrying the same
    unresolvable reference and the same wording.

    The dimensions that apply to the provenance error path are:
    error category (exception class), error wording (str(exc)), body
    keys (the exception attributes that the HTTP layer projects onto
    the 404 body), response size (a constant for a single error
    body), and latency (compared structurally — both paths perform
    the same ``Plan_Approval_Records`` lookup and head-node authority
    evaluation).
    """
    error_x: Optional[BaseException] = None
    error_y: Optional[BaseException] = None
    body_x = None
    body_y = None

    with engine_x.connect() as conn_x:
        try:
            body_x = navigator_x.navigate_plan_approval(
                conn_x,
                plan_approval_id=plan_approval_id,
                party_id=_REQUESTER_PARTY_ID,
            )
        except BaseException as exc:  # noqa: BLE001
            error_x = exc

    with engine_y.connect() as conn_y:
        try:
            body_y = navigator_y.navigate_plan_approval(
                conn_y,
                plan_approval_id=plan_approval_id,
                party_id=_REQUESTER_PARTY_ID,
            )
        except BaseException as exc:  # noqa: BLE001
            error_y = exc

    # ----- Error-category dimension --------------------------------
    # Both universes must surface the same outcome shape: either both
    # raised the same exception class, or both returned a chain. Any
    # asymmetry would itself be the leak Property 18 forbids.
    assert (error_x is None) == (error_y is None), (
        "Property 18 violated on the error-category dimension for "
        "navigate_plan_approval: one universe raised an exception "
        "while the other returned a chain. "
        f"Universe X error={error_x!r}, body={body_x!r}; "
        f"Universe Y error={error_y!r}, body={body_y!r}."
    )

    if error_x is not None and error_y is not None:
        assert type(error_x) is type(error_y), (
            "Property 18 violated on the error-category dimension: "
            "navigate_plan_approval raised different exception classes "
            "across universes. "
            f"X={type(error_x).__name__}, Y={type(error_y).__name__}."
        )
        # ----- Error-wording dimension ----------------------------
        assert str(error_x) == str(error_y), (
            "Property 18 violated on the error-wording dimension: "
            "navigate_plan_approval raised the same exception class "
            "but with different messages. "
            f"X={str(error_x)!r}, Y={str(error_y)!r}."
        )

        # ----- Body-keys dimension --------------------------------
        # :class:`PlanApprovalUnresolvableError` carries a single
        # public attribute (``plan_approval_id``) and a single
        # ``args`` tuple; both must match across universes so the
        # HTTP layer's 404 body is byte-equivalent.
        if isinstance(error_x, PlanApprovalUnresolvableError):
            assert isinstance(error_y, PlanApprovalUnresolvableError)
            attrs_x = {
                "plan_approval_id": getattr(
                    error_x, "plan_approval_id", None
                ),
                "args": error_x.args,
            }
            attrs_y = {
                "plan_approval_id": getattr(
                    error_y, "plan_approval_id", None
                ),
                "args": error_y.args,
            }
            assert attrs_x == attrs_y, (
                "Property 18 violated on the body-keys dimension: "
                "PlanApprovalUnresolvableError attributes differ across "
                f"universes. X={attrs_x!r}, Y={attrs_y!r}."
            )

        # ----- Response-size dimension ----------------------------
        # A constant for a single-line error body — the str(exc)
        # comparison above already pins it. Asserting the length
        # explicitly so a future divergence names this dimension.
        assert len(str(error_x)) == len(str(error_y)), (
            "Property 18 violated on the response-size dimension: "
            "navigate_plan_approval error messages have different "
            f"lengths. X={len(str(error_x))}, Y={len(str(error_y))}."
        )
        return

    # Neither raised — the navigator returned a chain in both
    # universes. By construction P' lacks view.plan_approval on the
    # target's scope in Universe X, so this branch is only reached if
    # the navigator misclassified the head node's restriction. Fail
    # loudly with the chain in the message.
    assert error_x is None and error_y is None
    raise AssertionError(
        "Property 18 violated: navigate_plan_approval returned a chain "
        "in both universes, but Universe X's Plan Approval is "
        "supposed to be restricted to ``P'``. The navigator's head-node "
        f"authorization gate is leaking. body_x={body_x!r}, "
        f"body_y={body_y!r}."
    )
