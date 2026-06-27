# Feature: second-walking-slice, Property 16: Planning-creation success
"""Property 16 — Planning-creation success (task 16.1).

**Property 16: Planning-creation success**

For any authorized planning creation request that passes input
validation (Objective, Intended Outcome, Project, Deliverable
Expectation, Activity Plan, Plan Revision, Plan Review, or Plan
Approval), exactly one Resource row (and where applicable, one first
Revision row) plus exactly one consequential ``Audit_Records`` row are
persisted in one transaction with byte-equivalent recorded times.

**Validates: Requirements 2.1, 2.7, 3.1, 3.6, 4.1, 4.6, 5.1, 5.6, 6.1,
6.5, 7.1, 7.6, 8.1, 8.4, 9.1, 9.7, 16.1, 20.1**

Strategy
========

Eight independent property tests, one per Planning_Service request
body, each driven by a Hypothesis strategy that generates a valid
request payload for that endpoint:

- :func:`test_objective_creation_persists_one_resource_one_revision_one_audit`
  exercises :meth:`ObjectiveService.create_objective` against a freshly
  seeded ``Accept`` Decision Immutable Record (Requirement 2.1, 2.7).
- :func:`test_intended_outcome_creation_persists_one_resource_one_revision_one_audit`
  exercises :meth:`IntendedOutcomeService.create_intended_outcome`
  against a seeded Objective (Requirement 3.1, 3.6).
- :func:`test_project_creation_persists_one_resource_one_revision_one_audit`
  exercises :meth:`ProjectService.create_project` against a seeded
  Objective (Requirement 4.1, 4.6).
- :func:`test_deliverable_expectation_creation_persists_one_resource_one_revision_one_audit`
  exercises
  :meth:`DeliverableExpectationService.create_deliverable_expectation`
  against a seeded Project (Requirement 5.1, 5.6).
- :func:`test_activity_plan_creation_persists_one_resource_one_audit`
  exercises :meth:`ActivityPlanService.create_activity_plan` against a
  seeded Project. Activity Plans are header-only — there is no
  Revision row (design §"Planning_Service.ActivityPlans") — so the
  byte-equivalence assertion only joins the Resource header and the
  audit row (Requirement 6.1, 6.5).
- :func:`test_plan_revision_creation_persists_one_resource_one_audit`
  exercises :meth:`PlanRevisionService.create_plan_revision` against a
  seeded Activity Plan. The Plan Revision row *is* the revision
  (single-table-per-Resource design — design §"Data Models"); the
  assertion treats the ``Plan_Revisions`` row as both the Resource and
  the first Revision (Requirement 7.1, 7.6).
- :func:`test_plan_review_creation_persists_one_resource_one_revision_one_audit`
  exercises :meth:`PlanReviewService.create_plan_review` against a
  seeded Draft Plan Revision (Requirement 8.1, 8.4).
- :func:`test_plan_approval_creation_persists_one_immutable_record_one_audit`
  exercises :meth:`PlanApprovalService.create_plan_approval` against a
  seeded Draft Plan Revision. Plan Approval Records are Immutable
  Records — there is no Revision row — so the byte-equivalence
  assertion joins the ``Plan_Approval_Records`` row and the audit row
  (Requirement 9.1, 9.7).

Per Hypothesis case, each test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case identifier,
   audit, and resource state cannot leak. The engine carries both the
   Slice 1 schema (with the additive ``Identifier_Registry.resource_kind``
   and ``Relationships.semantic_role`` columns from task 1.2) and the
   Slice 2 schema with its append-only triggers (task 1.3).
2. Seeds the actor Party and assigning-authority Party rows so the
   ``Audit_Records.actor_party_id`` and
   ``Role_Assignments.assigning_authority_id`` FKs resolve.
3. Assigns the precise required authority to the actor: ``modify``
   for the six Resource-creation actions (AD-WS-15), ``review`` for
   ``create.plan_review``, ``approve`` for ``create.plan_approval``.
4. Seeds the prerequisite chain needed by the action under test
   (e.g. ``Decisions`` row for Objectives, ``Objectives`` row for
   Projects / Intended Outcomes, ``Projects`` row for Deliverable
   Expectations / Activity Plans, ``Activity_Plans`` row for Plan
   Revisions, draft ``Plan_Revisions`` row for Plan Reviews and Plan
   Approvals). Prerequisites are seeded with direct ``INSERT`` rather
   than through their Planning_Service to keep the property scoped to
   the single action under test.
5. Invokes the create method with the Hypothesis-drawn body inside
   one ``engine.begin()`` block so the AD-WS-5 "audit-and-write
   atomic" contract participates in the test.
6. Asserts the three invariants of Property 16:
   - **Resource count** — exactly one row in the Resource header
     table (or, for header-less kinds like Activity Plans and Plan
     Revisions, exactly one row in the single table that backs the
     kind) named by ``target_id`` on the audit row.
   - **First Revision count** — for kinds with a Resource / Revision
     split (Objective, Intended Outcome, Project, Deliverable
     Expectation, Plan Review), exactly one row in the Revisions
     table whose Resource Identity matches the audit row's
     ``target_id``.
   - **Consequential audit count** — exactly one ``Audit_Records``
     row with ``outcome='consequential'`` and the action_type for the
     kind under test, joined to the resource by ``correlation_id`` /
     ``target_id``.
   - **Byte-equivalent recorded times** — the persisted Resource
     row's ``created_at`` (or ``recorded_at`` for header-less kinds),
     the persisted first Revision row's ``recorded_at`` (when one
     exists), and the consequential audit row's ``recorded_at`` are
     all the same string.

Setup follows the conventions established by Slice 1 property tests
(per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory
state cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps) and by
Slice 2 unit tests (per-test ``planning_engine`` fixture installs both
schemas; direct-INSERT helpers for Plan_Revisions / Activity_Plans
prerequisites that bypass services not under test).
"""

from __future__ import annotations

import re
import tempfile
import uuid as uuid_lib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.activity_plans import ActivityPlanService
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.objectives import ObjectiveService
from walking_slice.planning.plan_approvals import PlanApprovalService
from walking_slice.planning.plan_reviews import PlanReviewService
from walking_slice.planning.plan_revisions import PlanRevisionService
from walking_slice.planning.projects import ProjectService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants. The actor Party is the same across every property in
# this file so the helper seeders can hard-wire the foreign keys.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-16/scope"

# Deterministic prerequisite identifiers seeded by the helpers below.
# UUIDv7-shaped to satisfy the canonical-form CHECK on
# ``Identifier_Registry`` rows downstream services may insert.
_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_INTENDED_OUTCOME_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c4"
_DRAFT_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c5"


# Canonical UUIDv7 lowercase-hex pattern. Property 16's assertions only
# require uniqueness of the persisted identifiers, but checking the
# canonical form here keeps a sanity rail in place against any future
# refactor that swaps the identity generator.
_CANONICAL_UUID7: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case state cannot leak between generated inputs. The
# engine carries both the Slice 1 schema (with the additive Slice 2
# columns from task 1.2) and the Slice 2 schema (task 1.3). A
# :class:`tempfile.TemporaryDirectory` context inside each test body
# owns the per-case directory; function-scoped pytest fixtures would
# not reset between Hypothesis-generated inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys."""
    db_path = tmp_dir / "walking_slice.sqlite"
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
    return engine


# ---------------------------------------------------------------------------
# Seed helpers.
#
# The helpers below seed the minimum prerequisite chain each property
# test needs. Prerequisites are written through direct INSERT (rather
# than through the corresponding Planning_Service) so each property
# test exercises exactly one create operation — Property 16 is a
# single-action invariant and isolating the action under test keeps
# shrunken counterexamples actionable.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the actor Party and the assigning-authority Party."""
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_ID, "Property 16 Actor"),
            (_ASSIGNING_AUTHORITY_ID, "Property 16 Resource Steward"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, :ts)
                    """
                ),
                {"pid": party_id, "name": display, "ts": _NOW_ISO},
            )


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    authorities: tuple[str, ...],
    role_name: str,
) -> None:
    """Grant ``authorities`` over ``_SCOPE`` to the actor Party.

    Each property test selects the authority required by the action
    under test (``modify`` for the six Resource-creation actions per
    AD-WS-15, ``review`` for Plan Review per Requirement 11.4,
    ``approve`` for Plan Approval per Requirement 11.5). The
    role-assignment effective period generously brackets the fixed
    clock instant so a Hypothesis-shrunken case never misses on
    timing.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name=role_name,
        scope=_SCOPE,
        authorities_granted=authorities,
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_accept_decision(
    engine: Engine, knowledge_service: KnowledgeService
) -> str:
    """Seed a Finding → Recommendation → ``Accept`` Decision chain.

    Uses the unwired :class:`KnowledgeService` (no
    :class:`AuthorizationService` collaborator) so the seed step is
    not gated by Slice 1's authority check. Property 16 only asserts
    on the Planning_Service action under test.
    """
    with engine.begin() as conn:
        finding = knowledge_service.create_finding(
            conn,
            statement="Property 16 seed finding statement.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Property 16 seed recommendation rationale.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Property 16 seed decision rationale.",
            deciding_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
        )
    return decision.decision_id


def _seed_objective_directly(
    engine: Engine, *, objective_id: str = _OBJECTIVE_ID
) -> None:
    """Insert one ``Objectives`` row by hand for the IntendedOutcome /
    Project property tests.

    Slice 2's CHECK / FK constraints on the prerequisite tables are
    satisfied by the minimal columns we INSERT here; Property 16 is
    not asserting on the prerequisite's structure, only the action
    under test.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": objective_id, "ts": _NOW_ISO},
        )


def _seed_project_directly(
    engine: Engine, *, project_id: str = _PROJECT_ID
) -> None:
    """Insert one ``Projects`` row by hand for the Deliverable
    Expectation / Activity Plan property tests."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": project_id, "ts": _NOW_ISO},
        )


def _seed_activity_plan_directly(
    engine: Engine,
    *,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    project_id: str = _PROJECT_ID,
) -> None:
    """Insert one ``Activity_Plans`` row by hand for the Plan Revision
    property test."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 16 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": activity_plan_id,
                "pid": project_id,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_draft_plan_revision_directly(
    engine: Engine,
    *,
    plan_revision_id: str = _DRAFT_PLAN_REVISION_ID,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
) -> None:
    """Insert one ``Plan_Revisions`` row with ``lifecycle_state='draft'``
    by hand for the Plan Review / Plan Approval property tests.

    ``INSERT`` into ``Plan_Revisions`` is not gated by the AD-WS-19
    lifecycle UPDATE trigger; seeding a draft revision is a direct
    INSERT with no session-pragma plumbing required.
    """
    with engine.begin() as conn:
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
                    :rev, :aid, NULL, 'draft',
                    'Property 16 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Audit-row + resource-row probe helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_consequential_audit_rows(
    engine: Engine, *, action_type: str
) -> list[dict[str, Any]]:
    """Return every ``outcome='consequential'`` audit row of one action.

    Only Property 16's consequential audit row matters here. The
    authorization evaluation row that
    :meth:`AuthorizationService.evaluate` writes carries
    ``outcome='permit'`` (not ``'consequential'``) and is therefore
    naturally excluded by the predicate.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT actor_party_id, action_type, outcome,
                           target_id, target_revision_id,
                           correlation_id, recorded_at
                      FROM Audit_Records
                     WHERE outcome = 'consequential'
                       AND action_type = :a
                    """
                ),
                {"a": action_type},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_single_column(
    engine: Engine,
    *,
    table: str,
    column: str,
    id_column: str,
    id_value: str,
) -> str:
    """Read one column from one row by primary-key identity."""
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    f"SELECT {column} FROM {table} "
                    f"WHERE {id_column} = :i"
                ),
                {"i": id_value},
            ).scalar_one()
        )


# ===========================================================================
# Property 16 strategies.
#
# Hypothesis text generators are restricted to printable Unicode below
# the surrogate block so the generated content round-trips through
# SQLite's UTF-8 TEXT columns without escape ambiguity. Each strategy
# stays within the per-attribute length range named by the design
# §"Components and Interfaces" surface (and re-enforced by the schema
# CHECK constraints in :mod:`walking_slice.planning._persistence`).
# ===========================================================================


# Use a narrow alphabet (printable ASCII plus a handful of common Latin
# extras) so Hypothesis text generation does not draw control-character
# strings that some SQLite drivers reject. Property 16 is not about
# UTF-8 robustness — Slice 1 Property 7 covers that — so a narrower
# alphabet keeps the shrunken counterexamples readable.
_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Strategy for a non-control text run of ``min_size..max_size`` chars."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


# Objective — Requirement 2.3.
_objective_strategy = st.fixed_dictionaries(
    {
        "statement": _bounded_text(1, 200),
        "rationale": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


# Intended Outcome — Requirement 3.2.
_intended_outcome_strategy = st.fixed_dictionaries(
    {
        "success_condition": _bounded_text(1, 200),
        "observation_window": st.one_of(
            st.none(),
            _bounded_text(0, 200),
        ),
        "attribution_assumption": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


@st.composite
def _project_payload(draw: Any) -> dict[str, Any]:
    """Project — Requirement 4.2.

    Draws a planned date range with ``start <= end`` (per the
    Requirement 4.2 / 4.3 ordering invariant) plus the name and
    optional summary.
    """
    start = draw(
        st.dates(
            min_value=date(2024, 1, 1),
            max_value=date(2028, 12, 31),
        )
    )
    span_days = draw(st.integers(min_value=0, max_value=365))
    end = start + timedelta(days=span_days)
    return {
        "name": draw(_bounded_text(1, 200)),
        "summary": draw(
            st.one_of(st.none(), _bounded_text(0, 500))
        ),
        "planned_start_date": start,
        "planned_end_date": end,
    }


# Deliverable Expectation — Requirement 5.2.
_deliverable_expectation_strategy = st.fixed_dictionaries(
    {
        "name": _bounded_text(1, 200),
        "description": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
        "deliverable_kind": st.sampled_from(
            ["Document", "Artifact", "Service", "Other"]
        ),
        "acceptance_criteria": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


# Activity Plan — Requirement 6.2.
_activity_plan_strategy = st.fixed_dictionaries(
    {
        "title": _bounded_text(1, 200),
    }
)


# Plan Revision — Requirement 7.2. ``deliverable_expectation_refs`` is
# held at empty so no extra prerequisite seeding is required (the
# happy-path resource-and-revision-count invariant under test is
# orthogonal to the references that would be persisted in
# ``deliverable_expectation_refs_json``). Property 28 — Planning
# relationship-structure invariants — covers the references shape.
_plan_revision_strategy = st.fixed_dictionaries(
    {
        "planned_scope": _bounded_text(1, 200),
        "planning_assumptions": st.lists(
            _bounded_text(1, 200), min_size=0, max_size=5
        ),
        "ordering_rationale": st.one_of(
            st.none(),
            _bounded_text(0, 200),
        ),
    }
)


# Plan Review — Requirement 8.2.
_plan_review_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(
            ["Endorse", "Changes_Requested", "Reject"]
        ),
        "rationale": _bounded_text(1, 200),
    }
)


# Plan Approval — Requirement 9.2.
_plan_approval_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(["Approve", "Reject_Approval"]),
        "rationale": _bounded_text(1, 200),
    }
)


# ---------------------------------------------------------------------------
# Per-case service factory.
# ---------------------------------------------------------------------------


def _build_services() -> tuple[
    FixedClock,
    IdentityService,
    AuditLog,
    AuthorizationService,
    KnowledgeService,
    ProvenanceManifestWriter,
]:
    """Construct the per-case service bundle.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state and any audit-correlation accumulator cannot bleed
    across shrinks.
    """
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    manifest_writer = ProvenanceManifestWriter(
        clock=clock,
        identity_service=identity_service,
    )
    return (
        clock,
        identity_service,
        audit_log,
        authorization_service,
        knowledge_service,
        manifest_writer,
    )


# ===========================================================================
# The eight property tests.
# ===========================================================================


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_objective_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_objective_creation_persists_one_resource_one_revision_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Objective creation request:

    - exactly one ``Objectives`` row exists named by the audit row's
      ``target_id``,
    - exactly one ``Objective_Revisions`` row exists named by the
      audit row's ``target_revision_id``,
    - exactly one consequential ``Audit_Records`` row exists with
      ``action_type='create.objective'``,
    - the three rows carry byte-equivalent recorded times.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_obj_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                _clock,
                _identity_service,
                audit_log,
                authorization_service,
                knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="objective_owner",
            )
            decision_id = _seed_accept_decision(engine, knowledge_service)

            service = ObjectiveService(
                clock=_clock,
                identity_service=_identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                knowledge_service=knowledge_service,
            )

            with engine.begin() as conn:
                result = service.create_objective(
                    conn,
                    statement=payload["statement"],
                    rationale=payload["rationale"],
                    target_decision_id=decision_id,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.objective_id)
            assert _CANONICAL_UUID7.match(result.objective_revision_id)
            assert _count(engine, "Objectives") == 1
            assert _count(engine, "Objective_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.objective"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.objective_id
            assert (
                audit_row["target_revision_id"]
                == result.objective_revision_id
            )
            assert audit_row["actor_party_id"] == _PARTY_ID

            objective_created_at = _fetch_single_column(
                engine,
                table="Objectives",
                column="created_at",
                id_column="objective_id",
                id_value=result.objective_id,
            )
            revision_recorded_at = _fetch_single_column(
                engine,
                table="Objective_Revisions",
                column="recorded_at",
                id_column="objective_revision_id",
                id_value=result.objective_revision_id,
            )
            assert objective_created_at == revision_recorded_at
            assert audit_row["recorded_at"] == revision_recorded_at
            assert result.recorded_at == revision_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_intended_outcome_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_intended_outcome_creation_persists_one_resource_one_revision_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Intended Outcome creation request:

    - exactly one ``Intended_Outcomes`` row,
    - exactly one ``Intended_Outcome_Revisions`` row,
    - exactly one ``create.intended_outcome`` consequential audit row,
    - byte-equivalent recorded times across the three rows.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_io_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="intended_outcome_owner",
            )
            _seed_objective_directly(engine)

            service = IntendedOutcomeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition=payload["success_condition"],
                    observation_window=payload["observation_window"],
                    attribution_assumption=payload["attribution_assumption"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.intended_outcome_id)
            assert _CANONICAL_UUID7.match(
                result.intended_outcome_revision_id
            )
            assert _count(engine, "Intended_Outcomes") == 1
            assert _count(engine, "Intended_Outcome_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.intended_outcome"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.intended_outcome_id
            assert (
                audit_row["target_revision_id"]
                == result.intended_outcome_revision_id
            )

            resource_created_at = _fetch_single_column(
                engine,
                table="Intended_Outcomes",
                column="created_at",
                id_column="intended_outcome_id",
                id_value=result.intended_outcome_id,
            )
            revision_recorded_at = _fetch_single_column(
                engine,
                table="Intended_Outcome_Revisions",
                column="recorded_at",
                id_column="intended_outcome_revision_id",
                id_value=result.intended_outcome_revision_id,
            )
            assert resource_created_at == revision_recorded_at
            assert audit_row["recorded_at"] == revision_recorded_at
            assert result.recorded_at == revision_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_project_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_project_creation_persists_one_resource_one_revision_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Project creation request:

    - exactly one ``Projects`` row,
    - exactly one ``Project_Revisions`` row,
    - exactly one ``create.project`` consequential audit row,
    - byte-equivalent recorded times across the three rows.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_proj_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="project_owner",
            )
            _seed_objective_directly(engine)

            service = ProjectService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_project(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    name=payload["name"],
                    summary=payload["summary"],
                    planned_start_date=payload["planned_start_date"],
                    planned_end_date=payload["planned_end_date"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.project_id)
            assert _CANONICAL_UUID7.match(result.project_revision_id)
            assert _count(engine, "Projects") == 1
            assert _count(engine, "Project_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.project"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.project_id
            assert (
                audit_row["target_revision_id"]
                == result.project_revision_id
            )

            resource_created_at = _fetch_single_column(
                engine,
                table="Projects",
                column="created_at",
                id_column="project_id",
                id_value=result.project_id,
            )
            revision_recorded_at = _fetch_single_column(
                engine,
                table="Project_Revisions",
                column="recorded_at",
                id_column="project_revision_id",
                id_value=result.project_revision_id,
            )
            assert resource_created_at == revision_recorded_at
            assert audit_row["recorded_at"] == revision_recorded_at
            assert result.recorded_at == revision_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_deliverable_expectation_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_deliverable_expectation_creation_persists_one_resource_one_revision_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Deliverable Expectation creation
    request:

    - exactly one ``Deliverable_Expectations`` row,
    - exactly one ``Deliverable_Expectation_Revisions`` row,
    - exactly one ``create.deliverable_expectation`` consequential
      audit row,
    - byte-equivalent recorded times across the three rows.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_de_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="deliverable_expectation_owner",
            )
            _seed_project_directly(engine)

            service = DeliverableExpectationService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_deliverable_expectation(
                    conn,
                    target_project_id=_PROJECT_ID,
                    name=payload["name"],
                    description=payload["description"],
                    deliverable_kind=payload["deliverable_kind"],
                    acceptance_criteria=payload["acceptance_criteria"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.deliverable_expectation_id)
            assert _CANONICAL_UUID7.match(
                result.deliverable_expectation_revision_id
            )
            assert _count(engine, "Deliverable_Expectations") == 1
            assert _count(engine, "Deliverable_Expectation_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.deliverable_expectation"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert (
                audit_row["target_id"] == result.deliverable_expectation_id
            )
            assert (
                audit_row["target_revision_id"]
                == result.deliverable_expectation_revision_id
            )

            resource_created_at = _fetch_single_column(
                engine,
                table="Deliverable_Expectations",
                column="created_at",
                id_column="deliverable_expectation_id",
                id_value=result.deliverable_expectation_id,
            )
            revision_recorded_at = _fetch_single_column(
                engine,
                table="Deliverable_Expectation_Revisions",
                column="recorded_at",
                id_column="deliverable_expectation_revision_id",
                id_value=result.deliverable_expectation_revision_id,
            )
            assert resource_created_at == revision_recorded_at
            assert audit_row["recorded_at"] == revision_recorded_at
            assert result.recorded_at == revision_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_activity_plan_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_activity_plan_creation_persists_one_resource_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Activity Plan creation request:

    - exactly one ``Activity_Plans`` row (the Activity Plan is
      header-only — design §"Planning_Service.ActivityPlans" — so no
      Revision row is involved),
    - exactly one ``create.activity_plan`` consequential audit row,
    - byte-equivalent recorded times across the two rows.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_ap_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="activity_plan_owner",
            )
            _seed_project_directly(engine)

            service = ActivityPlanService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_activity_plan(
                    conn,
                    target_project_id=_PROJECT_ID,
                    title=payload["title"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.activity_plan_id)
            assert _count(engine, "Activity_Plans") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.activity_plan"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.activity_plan_id
            # Activity Plans have no Revision row — Requirement 6.1
            # names the Resource as the only persistent artifact — so
            # the audit row's ``target_revision_id`` is NULL.
            assert audit_row["target_revision_id"] is None

            resource_recorded_at = _fetch_single_column(
                engine,
                table="Activity_Plans",
                column="recorded_at",
                id_column="activity_plan_id",
                id_value=result.activity_plan_id,
            )
            assert audit_row["recorded_at"] == resource_recorded_at
            assert result.recorded_at == resource_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_plan_revision_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_plan_revision_creation_persists_one_resource_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Plan Revision creation request:

    - exactly one ``Plan_Revisions`` row (the row IS the revision —
      Plan Revisions live in a single Revision-level table per design
      §"Planning_Service.PlanRevisions"; the row plays both the
      Resource and the first-Revision role),
    - exactly one ``create.plan_revision`` consequential audit row,
    - byte-equivalent recorded times across the two rows.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_pr_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="plan_revision_author",
            )
            _seed_project_directly(engine)
            _seed_activity_plan_directly(engine)

            service = PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope=payload["planned_scope"],
                    planning_assumptions=tuple(
                        payload["planning_assumptions"]
                    ),
                    ordering_rationale=payload["ordering_rationale"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.plan_revision_id)
            # The seeded prerequisite ``Activity_Plans`` row does not
            # produce a ``Plan_Revisions`` row, so the only Plan
            # Revision row in the database is the one this case
            # persisted.
            assert _count(engine, "Plan_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.plan_revision"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.plan_revision_id

            plan_revision_recorded_at = _fetch_single_column(
                engine,
                table="Plan_Revisions",
                column="recorded_at",
                id_column="plan_revision_id",
                id_value=result.plan_revision_id,
            )
            assert audit_row["recorded_at"] == plan_revision_recorded_at
            assert result.recorded_at == plan_revision_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_plan_review_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_plan_review_creation_persists_one_resource_one_revision_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Plan Review creation request:

    - exactly one ``Plan_Reviews`` row,
    - exactly one ``Plan_Review_Revisions`` row,
    - exactly one ``create.plan_review`` consequential audit row,
    - byte-equivalent recorded times across the three rows.

    The reviewing Party holds the ``review`` authority per AD-WS-15 /
    Requirement 11.4; the target Plan Revision is seeded as ``draft``
    so the Requirement 8.6 precondition is met.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_prv_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("review",),
                role_name="plan_reviewer",
            )
            _seed_project_directly(engine)
            _seed_activity_plan_directly(engine)
            _seed_draft_plan_revision_directly(engine)

            service = PlanReviewService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=payload["outcome"],
                    rationale=payload["rationale"],
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.plan_review_id)
            assert _CANONICAL_UUID7.match(result.plan_review_revision_id)
            assert _count(engine, "Plan_Reviews") == 1
            assert _count(engine, "Plan_Review_Revisions") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.plan_review"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.plan_review_id
            assert (
                audit_row["target_revision_id"]
                == result.plan_review_revision_id
            )

            resource_created_at = _fetch_single_column(
                engine,
                table="Plan_Reviews",
                column="created_at",
                id_column="plan_review_id",
                id_value=result.plan_review_id,
            )
            revision_recorded_at = _fetch_single_column(
                engine,
                table="Plan_Review_Revisions",
                column="recorded_at",
                id_column="plan_review_revision_id",
                id_value=result.plan_review_revision_id,
            )
            assert resource_created_at == revision_recorded_at
            assert audit_row["recorded_at"] == revision_recorded_at
            assert result.recorded_at == revision_recorded_at
        finally:
            engine.dispose()


# Feature: second-walking-slice, Property 16: Planning-creation success
@given(payload=_plan_approval_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_plan_approval_creation_persists_one_immutable_record_one_audit(
    payload: dict[str, Any],
) -> None:
    """For any authorized, valid Plan Approval creation request:

    - exactly one ``Plan_Approval_Records`` row (a Plan Approval is
      an Immutable Record — there is no Revision row),
    - exactly one ``create.plan_approval`` consequential audit row,
    - byte-equivalent recorded times across the two rows.

    The approving Party holds the ``approve`` authority per AD-WS-15
    / Requirement 11.5; the target Plan Revision is seeded as
    ``draft`` so both outcomes (``Approve`` transitions it to
    ``approved``; ``Reject_Approval`` leaves it in ``draft``) are
    accepted by the Requirement 9.5 precondition.
    """
    with tempfile.TemporaryDirectory(prefix="prop16_pa_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _knowledge_service,
                manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("approve",),
                role_name="plan_approver",
            )
            _seed_project_directly(engine)
            _seed_activity_plan_directly(engine)
            _seed_draft_plan_revision_directly(engine)

            service = PlanApprovalService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                manifest_writer=manifest_writer,
            )

            with engine.begin() as conn:
                result = service.create_plan_approval(
                    conn,
                    engine,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=payload["outcome"],
                    rationale=payload["rationale"],
                    approving_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                )

            assert _CANONICAL_UUID7.match(result.plan_approval_id)
            assert _count(engine, "Plan_Approval_Records") == 1

            audit_rows = _fetch_consequential_audit_rows(
                engine, action_type="create.plan_approval"
            )
            assert len(audit_rows) == 1
            audit_row = audit_rows[0]
            assert audit_row["target_id"] == result.plan_approval_id
            # Plan Approvals are Immutable Records — no Revision row,
            # so the audit row's ``target_revision_id`` is NULL.
            assert audit_row["target_revision_id"] is None

            approval_recorded_at = _fetch_single_column(
                engine,
                table="Plan_Approval_Records",
                column="recorded_at",
                id_column="plan_approval_id",
                id_value=result.plan_approval_id,
            )
            assert audit_row["recorded_at"] == approval_recorded_at
            assert result.recorded_at == approval_recorded_at
        finally:
            engine.dispose()
