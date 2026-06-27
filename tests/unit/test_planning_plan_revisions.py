"""Unit tests for :mod:`walking_slice.planning.plan_revisions` (task 9.2).

Pins the contract established in task 9.1, design
§"Planning_Service.PlanRevisions", and Requirements 7.2, 7.3, 7.4 for
:meth:`PlanRevisionService.create_plan_revision`:

- **7.2 — count and length boundaries.** Every Plan Revision creation
  request must carry a planned scope of 1..10000 characters, 0..50
  Deliverable Expectation references, 0..100 planning-assumption
  entries each 1..2000 characters, and an optional ordering rationale
  of 0..2000 characters. Validation runs in the static validator
  before any database read so a malformed request never touches
  identifier minting, the dependency resolution SELECTs, or the
  authorization service. The schema CHECK constraints on
  ``Plan_Revisions`` are the defense-in-depth layer.
- **7.4 — unresolved references and approved predecessors.** A
  Deliverable Expectation reference that does not resolve raises
  :class:`PlanRevisionDeliverableExpectationNotResolvableError`
  identifying the first offending entry; a predecessor Plan Revision
  whose ``lifecycle_state`` is ``'approved'`` raises
  :class:`PlanRevisionPredecessorApprovedError`. Both checks run
  before authorization evaluation so the deny path never leaks
  whether a referenced Resource exists.
- **7.3 — Supersedes Relationship inserted exactly once.** When a
  predecessor is supplied, the service INSERTs exactly one
  ``Relationships`` row with ``relationship_type='Supersedes'``,
  ``source_id`` = new Plan Revision Identity, and ``target_id`` =
  predecessor Plan Revision Identity. When no predecessor is
  supplied, zero ``Supersedes`` rows are inserted.

The tests mirror the style of ``tests/unit/test_planning_activity_plans.py``:
a per-test engine carrying both the Slice 1 and Slice 2 schemas, a real
:class:`AuthorizationService` driven through a seeded role assignment on
happy paths, and counter helpers that confirm nothing was persisted on
negative paths.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.plan_revisions import (
    CreatePlanRevisionResult,
    PlanRevisionActivityPlanNotResolvableError,
    PlanRevisionDeliverableExpectationNotResolvableError,
    PlanRevisionPredecessorActivityPlanMismatchError,
    PlanRevisionPredecessorApprovedError,
    PlanRevisionPredecessorNotResolvableError,
    PlanRevisionService,
    PlanRevisionValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_DELIVERABLE_ID_A = "00000000-0000-7000-8000-000000c00031"
_DELIVERABLE_ID_B = "00000000-0000-7000-8000-000000c00032"
_DELIVERABLE_ID_C = "00000000-0000-7000-8000-000000c00033"
_UNRESOLVABLE_DELIVERABLE_ID = "00000000-0000-7000-8000-0000deadbe01"
_UNRESOLVABLE_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-0000deadbe02"
_UNRESOLVABLE_PREDECESSOR_ID = "00000000-0000-7000-8000-0000deadbe03"
_OTHER_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00021"
_APPROVED_PREDECESSOR_ID = "00000000-0000-7000-8000-000000c00050"
_DRAFT_PREDECESSOR_ID = "00000000-0000-7000-8000-000000c00051"
_OTHER_PLAN_PREDECESSOR_ID = "00000000-0000-7000-8000-000000c00052"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Per-test engine carrying both Slice 1 and Slice 2 schemas.

    ``create_schema`` installs Slice 1 plus the additive
    ``Identifier_Registry.resource_kind`` and
    ``Relationships.semantic_role`` columns (task 1.2);
    ``create_planning_schema`` installs every Slice 2 table, index,
    and append-only trigger (task 1.3). No disclosure seeding is
    required: Plan Revision creation does not consult the disclosure
    registry.
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def plan_revision_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> PlanRevisionService:
    """:class:`PlanRevisionService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test.
    """
    return PlanRevisionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Each Plan Revision creation depends on the parent Activity Plan
# resolving and any referenced Deliverable Expectations resolving.
# These helpers seed just enough header rows for those resolution
# SELECTs to succeed.
# ---------------------------------------------------------------------------


def _seed_party(conn, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Project Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_project(engine: Engine, project_id: str = _PROJECT_ID) -> None:
    """Seed one Project Resource header row.

    The Activity Plan FK requires the Project row to exist. The
    upstream Objective dependency is irrelevant to these tests
    because Plan Revisions resolve against ``Activity_Plans`` and
    ``Deliverable_Expectations`` directly, not against the Objective.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": project_id, "ts": _TS_FIXED},
        )


def _seed_activity_plan(
    engine: Engine,
    *,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    project_id: str = _PROJECT_ID,
    title: str = "Mesh Rollout Activities",
) -> None:
    """Seed one Activity Plan row.

    The Plan Revision service resolves the Activity Plan through a
    SELECT on this table; seeding directly bypasses
    :class:`ActivityPlanService` which is exercised by its own test
    module.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, :title, :party, :scope, :ts
                )
                """
            ),
            {
                "aid": activity_plan_id,
                "pid": project_id,
                "title": title,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_expectation(
    engine: Engine, deliverable_expectation_id: str
) -> None:
    """Seed one Deliverable Expectation header row.

    The Plan Revision service resolves references through a SELECT on
    ``Deliverable_Expectations`` (header table); the
    ``Deliverable_Expectation_Revisions`` table is not consulted, so
    only the header is needed here.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": deliverable_expectation_id, "ts": _TS_FIXED},
        )


def _seed_plan_revision_directly(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    lifecycle_state: str = "draft",
) -> None:
    """Seed a ``Plan_Revisions`` row by hand, bypassing the service.

    Used to build the predecessor scenarios. INSERTs into
    ``Plan_Revisions`` are not gated by the AD-WS-19 lifecycle
    trigger (which only watches UPDATE), so seeding a row with
    ``lifecycle_state = 'approved'`` is a direct INSERT — no pragma
    plumbing required (mirrors the pattern in
    ``tests/unit/test_planning_persistence.py``).
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
                    :rev, :aid, NULL, :state, 'Phase 1 scope', '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_project_owner_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Project Owner authority (``modify``) to ``party_id``.

    Per AD-WS-15, ``create.plan_revision`` maps to the ``modify``
    authority type. A Party with an effective Role Assignment
    carrying ``modify`` over ``scope`` is permitted to create Plan
    Revisions against an Activity Plan in that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="project_owner",
        scope=scope,
        authorities_granted=("modify",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


# ---------------------------------------------------------------------------
# Row readers — used by negative-path tests to confirm nothing was persisted
# and by positive-path tests to inspect inserted rows.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_supersedes_with_source(engine: Engine, source_id: str) -> int:
    """Count ``Supersedes`` ``Relationships`` rows whose source is
    ``source_id``.

    The test brief calls this assertion out explicitly: exactly one
    ``Supersedes`` row whose ``source_id`` equals the new Plan
    Revision Identity.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = 'Supersedes' "
                    "AND source_id = :sid"
                ),
                {"sid": source_id},
            ).scalar_one()
        )


# ===========================================================================
# Happy path baseline — confirms the test wiring before the focus tests run.
# ===========================================================================


def test_create_plan_revision_permits_when_project_owner_role_grants_modify(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    plan_revision_service: PlanRevisionService,
) -> None:
    """Permit path: with an effective Project Owner role and an
    existing Activity Plan, the service creates exactly one
    ``Plan_Revisions`` row (lifecycle ``'draft'``) plus one
    consequential audit row inside one transaction. No ``Supersedes``
    Relationship is inserted because no predecessor was supplied.
    """
    _seed_required_parties(planning_engine)
    _assign_project_owner_role(authorization_service, planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)

    with planning_engine.begin() as conn:
        result = plan_revision_service.create_plan_revision(
            conn,
            target_activity_plan_id=_ACTIVITY_PLAN_ID,
            planned_scope="Phase 1 scope.",
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreatePlanRevisionResult)
    assert _CANONICAL_UUID7.match(result.plan_revision_id)
    assert result.lifecycle_state == "draft"
    assert result.target_activity_plan_id == _ACTIVITY_PLAN_ID
    assert result.predecessor_plan_revision_id is None
    assert result.supersedes_relationship_id is None
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Plan_Revisions") == 1
    # No predecessor → zero Supersedes Relationship rows whose source
    # is the new Plan Revision.
    assert _count_supersedes_with_source(
        planning_engine, result.plan_revision_id
    ) == 0


# ===========================================================================
# Requirement 7.2 — count and length boundaries.
#
# The static validator in :class:`PlanRevisionService` rejects values
# outside Requirement 7.2's ranges *before* any database read or
# authorization side-effect — so a malformed request never touches the
# database. Each constraint surfaces a stable ``failed_constraint`` so
# the route layer maps it to a structured 400.
# ===========================================================================


class TestPlannedScopeBoundaries:
    """planned_scope must be a non-empty string of 1..10000 characters."""

    def test_one_char_planned_scope_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A 1-character planned scope sits at the lower boundary."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="X",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.planned_scope == "X"
        assert _count(planning_engine, "Plan_Revisions") == 1

    def test_ten_thousand_char_planned_scope_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A 10000-character planned scope sits at the upper boundary."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        scope = "x" * 10_000
        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope=scope,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert len(result.planned_scope) == 10_000
        assert _count(planning_engine, "Plan_Revisions") == 1

    def test_ten_thousand_one_char_planned_scope_rejected(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """``len(planned_scope) == 10001`` raises with the stable constraint
        identifier ``'planned_scope_too_long'``."""
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="x" * 10_001,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "planned_scope_too_long"
        # Validator runs before identifier minting, lookups, INSERTs.
        assert _count(planning_engine, "Plan_Revisions") == 0
        assert _count(planning_engine, "Relationships") == 0

    def test_empty_planned_scope_rejected_as_missing(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """An empty planned scope surfaces ``'planned_scope_missing'``.

        Requirement 7.4 explicitly rejects requests omitting the
        planned scope statement; the validator collapses "empty" and
        "wrong type" into one actionable constraint.
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "planned_scope_missing"
        assert _count(planning_engine, "Plan_Revisions") == 0


class TestDeliverableExpectationRefsCountBoundaries:
    """deliverable_expectation_refs must be 0..50 entries."""

    def test_zero_refs_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """Zero references is the explicit lower boundary; the empty
        tuple is a valid input and the persisted JSON is ``'[]'``."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                deliverable_expectation_refs=(),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.deliverable_expectation_refs == ()
        with planning_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT deliverable_expectation_refs_json "
                    "FROM Plan_Revisions WHERE plan_revision_id=:id"
                ),
                {"id": result.plan_revision_id},
            ).scalar_one()
        assert stored == "[]"

    def test_fifty_one_refs_rejected_with_stable_constraint(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """51 references raises ``'deliverable_expectation_refs_too_many'``
        before any Activity Plan lookup."""
        _seed_required_parties(planning_engine)

        refs = tuple(
            f"00000000-0000-7000-8000-{i:012x}" for i in range(51)
        )
        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    deliverable_expectation_refs=refs,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "deliverable_expectation_refs_too_many"
        )
        assert _count(planning_engine, "Plan_Revisions") == 0


class TestPlanningAssumptionsBoundaries:
    """planning_assumptions: 0..100 entries each 1..2000 characters."""

    def test_zero_assumptions_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """Zero assumptions is the lower boundary; persisted JSON is
        ``'[]'``."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                planning_assumptions=(),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.planning_assumptions == ()

    def test_one_hundred_assumptions_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """100 assumptions sits at the upper count boundary."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        assumptions = tuple(f"assumption #{i}" for i in range(100))
        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                planning_assumptions=assumptions,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert len(result.planning_assumptions) == 100
        assert _count(planning_engine, "Plan_Revisions") == 1

    def test_one_hundred_one_assumptions_rejected(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """101 assumptions raises ``'planning_assumptions_too_many'``."""
        _seed_required_parties(planning_engine)

        assumptions = tuple(f"assumption #{i}" for i in range(101))
        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    planning_assumptions=assumptions,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "planning_assumptions_too_many"
        )
        assert _count(planning_engine, "Plan_Revisions") == 0

    def test_two_thousand_char_assumption_entry_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A 2000-char assumption entry sits at the upper length boundary."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        assumptions = ("x" * 2_000,)
        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                planning_assumptions=assumptions,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert len(result.planning_assumptions[0]) == 2_000
        assert _count(planning_engine, "Plan_Revisions") == 1

    def test_two_thousand_one_char_assumption_entry_rejected(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A 2001-char assumption entry raises ``'planning_assumption_too_long'``."""
        _seed_required_parties(planning_engine)

        assumptions = ("x" * 2_001,)
        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    planning_assumptions=assumptions,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "planning_assumption_too_long"
        # ``invalid_index`` identifies the first offending entry so
        # the route layer can pinpoint the bad index in a 400 response.
        assert exc_info.value.invalid_index == 0
        assert _count(planning_engine, "Plan_Revisions") == 0

    def test_empty_assumption_entry_rejected(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """An empty-string entry violates the 1-char lower boundary."""
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    planning_assumptions=("",),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "planning_assumption_invalid_type"
        )
        assert exc_info.value.invalid_index == 0


class TestOrderingRationaleBoundaries:
    """ordering_rationale: optional, 0..2000 chars when present."""

    def test_none_ordering_rationale_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """``None`` is accepted; persisted as SQL NULL."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                ordering_rationale=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.ordering_rationale is None
        with planning_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT ordering_rationale FROM Plan_Revisions "
                    "WHERE plan_revision_id=:id"
                ),
                {"id": result.plan_revision_id},
            ).scalar_one()
        assert stored is None

    def test_two_thousand_char_ordering_rationale_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """2000-character ordering rationale sits at the upper boundary."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        rationale = "x" * 2_000
        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                ordering_rationale=rationale,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.ordering_rationale == rationale
        assert _count(planning_engine, "Plan_Revisions") == 1

    def test_two_thousand_one_char_ordering_rationale_rejected(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """2001 characters raises ``'ordering_rationale_too_long'``."""
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(PlanRevisionValidationError) as exc_info:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    ordering_rationale="x" * 2_001,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "ordering_rationale_too_long"
        assert _count(planning_engine, "Plan_Revisions") == 0


# ===========================================================================
# Requirement 7.4 — unresolvable Deliverable Expectation reference.
#
# Even when every per-entry type / length check passes, the service
# verifies each reference resolves to an existing
# ``Deliverable_Expectations`` row. The rejection identifies the
# first offending entry (Requirement 7.4) and runs before
# authorization evaluation so the deny path never reveals existence
# to an unauthorized caller.
# ===========================================================================


class TestUnresolvedDeliverableExpectationRejection:
    """Unresolvable Deliverable Expectation references raise the
    dedicated error type."""

    def test_unresolved_ref_raises_dedicated_error(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """One unresolvable reference raises
        :class:`PlanRevisionDeliverableExpectationNotResolvableError`."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        # Note: no Deliverable Expectations seeded.

        with pytest.raises(
            PlanRevisionDeliverableExpectationNotResolvableError
        ) as exc_info:
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    deliverable_expectation_refs=(
                        _UNRESOLVABLE_DELIVERABLE_ID,
                    ),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.deliverable_expectation_id == (
            _UNRESOLVABLE_DELIVERABLE_ID
        )
        assert exc_info.value.invalid_index == 0
        assert exc_info.value.failed_constraint == (
            "deliverable_expectation_not_resolvable"
        )

    def test_unresolved_ref_persists_nothing(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """When a reference fails to resolve, no ``Plan_Revisions``,
        ``Relationships``, ``Audit_Records``, or
        ``Identifier_Registry`` row is added.

        The resolution SELECT runs before identifier minting, so an
        unresolvable reference leaves no Slice 2 row behind.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        # Seed one valid Deliverable Expectation so the *second*
        # entry below is the one that fails to resolve — the test
        # then asserts the rejection identifies the second index
        # specifically.
        _seed_deliverable_expectation(planning_engine, _DELIVERABLE_ID_A)

        # Snapshot row counts after role assignment / seeding so the
        # comparison isolates Plan-Revision-side inserts.
        registry_before = _count(planning_engine, "Identifier_Registry")
        audit_before = _count(planning_engine, "Audit_Records")
        relationships_before = _count(planning_engine, "Relationships")
        plan_revisions_before = _count(planning_engine, "Plan_Revisions")

        with pytest.raises(
            PlanRevisionDeliverableExpectationNotResolvableError
        ) as exc_info:
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    deliverable_expectation_refs=(
                        _DELIVERABLE_ID_A,
                        _UNRESOLVABLE_DELIVERABLE_ID,
                    ),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        # The error names the second (offending) entry; the index is
        # the input index in source order.
        assert exc_info.value.deliverable_expectation_id == (
            _UNRESOLVABLE_DELIVERABLE_ID
        )
        assert exc_info.value.invalid_index == 1

        assert _count(planning_engine, "Plan_Revisions") == plan_revisions_before
        assert _count(planning_engine, "Identifier_Registry") == registry_before
        assert _count(planning_engine, "Relationships") == relationships_before
        assert _count(planning_engine, "Audit_Records") == audit_before

    def test_unresolved_ref_check_runs_before_authorization(
        self,
        planning_engine: Engine,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """The Deliverable Expectation resolution check fires even
        when the caller holds no role assignment.

        Requirement 7.4 (unresolved reference) and Requirement 7.5
        (unauthorized caller) are distinct denial paths. The
        implementation surfaces
        :class:`PlanRevisionDeliverableExpectationNotResolvableError`
        first so the route layer error mapping is stable: an
        unresolvable-reference failure cannot be silently rewritten
        into an authorization denial.
        """
        _seed_required_parties(planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        # No role assignment, no Deliverable Expectations — but the
        # reference resolution check should fire before any
        # authorization evaluation.

        with pytest.raises(
            PlanRevisionDeliverableExpectationNotResolvableError
        ):
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    deliverable_expectation_refs=(
                        _UNRESOLVABLE_DELIVERABLE_ID,
                    ),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )


# ===========================================================================
# Requirement 7.4 — approved-predecessor rejection.
#
# Requirement 7.4 forbids naming an Approved Plan Revision as the
# predecessor of a new Plan Revision. The supersession edge is only
# meaningful between Draft Plan Revisions; once approved a Plan
# Revision is byte-equivalent forever (Requirement 9.4) and there is
# nothing further to supersede.
# ===========================================================================


class TestApprovedPredecessorRejection:
    """Approved predecessors raise the dedicated error type and
    persist nothing."""

    def test_approved_predecessor_raises_dedicated_error(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A predecessor whose ``lifecycle_state`` is ``'approved'``
        raises :class:`PlanRevisionPredecessorApprovedError`.

        The approved row is seeded directly via INSERT — the
        AD-WS-19 lifecycle trigger only gates UPDATEs, so seeding a
        Plan Revision in ``'approved'`` state needs no pragma
        plumbing (matches the pattern in
        ``tests/unit/test_planning_persistence.py``).
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_APPROVED_PREDECESSOR_ID,
            activity_plan_id=_ACTIVITY_PLAN_ID,
            lifecycle_state="approved",
        )

        with pytest.raises(
            PlanRevisionPredecessorApprovedError
        ) as exc_info:
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1 — replacement.",
                    predecessor_plan_revision_id=_APPROVED_PREDECESSOR_ID,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.predecessor_plan_revision_id == (
            _APPROVED_PREDECESSOR_ID
        )
        assert exc_info.value.predecessor_lifecycle_state == "approved"
        assert exc_info.value.failed_constraint == "predecessor_already_approved"

    def test_approved_predecessor_persists_nothing(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """When the predecessor is approved, no new Plan Revision,
        Relationship, audit row, or registry binding is created.

        The predecessor row itself remains byte-equivalent — the
        predecessor check is read-only (no UPDATE attempted) so
        Requirement 9.4 (Approved Plan Revisions are byte-equivalent
        forever) is preserved by construction.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_APPROVED_PREDECESSOR_ID,
            activity_plan_id=_ACTIVITY_PLAN_ID,
            lifecycle_state="approved",
        )

        # Snapshot Plan_Revisions, Relationships, Audit_Records,
        # Identifier_Registry counts and the predecessor row body
        # *after* seeding so the comparison isolates the
        # rejected-creation path.
        plan_revisions_before = _count(planning_engine, "Plan_Revisions")
        registry_before = _count(planning_engine, "Identifier_Registry")
        audit_before = _count(planning_engine, "Audit_Records")
        relationships_before = _count(planning_engine, "Relationships")
        with planning_engine.connect() as conn:
            pred_before = conn.execute(
                text(
                    "SELECT plan_revision_id, lifecycle_state, planned_scope "
                    "FROM Plan_Revisions WHERE plan_revision_id=:id"
                ),
                {"id": _APPROVED_PREDECESSOR_ID},
            ).mappings().one()

        with pytest.raises(PlanRevisionPredecessorApprovedError):
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1 — replacement.",
                    predecessor_plan_revision_id=_APPROVED_PREDECESSOR_ID,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert _count(planning_engine, "Plan_Revisions") == plan_revisions_before
        assert _count(planning_engine, "Identifier_Registry") == registry_before
        assert _count(planning_engine, "Relationships") == relationships_before
        assert _count(planning_engine, "Audit_Records") == audit_before
        # Predecessor row is byte-equivalent to its prior state.
        with planning_engine.connect() as conn:
            pred_after = conn.execute(
                text(
                    "SELECT plan_revision_id, lifecycle_state, planned_scope "
                    "FROM Plan_Revisions WHERE plan_revision_id=:id"
                ),
                {"id": _APPROVED_PREDECESSOR_ID},
            ).mappings().one()
        assert dict(pred_after) == dict(pred_before)

    def test_unresolvable_predecessor_raises_distinct_error(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A predecessor identifier that does not resolve at all
        raises :class:`PlanRevisionPredecessorNotResolvableError`,
        not the approved-predecessor error.

        The two negative paths surface distinct error types so the
        route layer can present distinct error messages
        (Requirement 7.4).
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        # No predecessor row seeded.

        with pytest.raises(
            PlanRevisionPredecessorNotResolvableError
        ) as exc_info:
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1 — replacement.",
                    predecessor_plan_revision_id=_UNRESOLVABLE_PREDECESSOR_ID,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.predecessor_plan_revision_id == (
            _UNRESOLVABLE_PREDECESSOR_ID
        )
        assert exc_info.value.failed_constraint == (
            "predecessor_plan_revision_not_resolvable"
        )

    def test_predecessor_on_different_activity_plan_raises_mismatch(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A predecessor that resolves but belongs to a different
        Activity Plan raises
        :class:`PlanRevisionPredecessorActivityPlanMismatchError`.

        Requirement 7.4 requires the predecessor to be a Plan
        Revision *of the same Activity Plan*; a mismatch is rejected
        with no Plan Revision created.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        # Seed a second Activity Plan and a draft predecessor in it.
        _seed_activity_plan(
            planning_engine,
            activity_plan_id=_OTHER_ACTIVITY_PLAN_ID,
            title="Other Activities",
        )
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_OTHER_PLAN_PREDECESSOR_ID,
            activity_plan_id=_OTHER_ACTIVITY_PLAN_ID,
            lifecycle_state="draft",
        )

        with pytest.raises(
            PlanRevisionPredecessorActivityPlanMismatchError
        ) as exc_info:
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1 — replacement.",
                    predecessor_plan_revision_id=_OTHER_PLAN_PREDECESSOR_ID,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.predecessor_plan_revision_id == (
            _OTHER_PLAN_PREDECESSOR_ID
        )
        assert exc_info.value.predecessor_activity_plan_id == (
            _OTHER_ACTIVITY_PLAN_ID
        )
        assert exc_info.value.target_activity_plan_id == _ACTIVITY_PLAN_ID

    def test_unresolved_activity_plan_raises_dedicated_error(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """A target Activity Plan that does not resolve raises
        :class:`PlanRevisionActivityPlanNotResolvableError`
        (Requirement 7.4 — distinct from the predecessor case).
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        # No Activity Plan seeded.

        with pytest.raises(
            PlanRevisionActivityPlanNotResolvableError
        ) as exc_info:
            with planning_engine.begin() as conn:
                plan_revision_service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_UNRESOLVABLE_ACTIVITY_PLAN_ID,
                    planned_scope="Phase 1.",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.target_activity_plan_id == (
            _UNRESOLVABLE_ACTIVITY_PLAN_ID
        )
        assert exc_info.value.failed_constraint == (
            "target_activity_plan_not_resolvable"
        )


# ===========================================================================
# Requirement 7.3 — Supersedes Relationship inserted exactly once for
# the predecessor case.
#
# When a Draft predecessor is supplied, the service INSERTs exactly
# one ``Relationships`` row with ``relationship_type='Supersedes'``,
# ``source_id`` = new Plan Revision Identity, and ``target_id`` =
# predecessor Plan Revision Identity. When no predecessor is supplied,
# zero ``Supersedes`` rows are inserted. The predecessor Plan Revision
# row remains byte-equivalent.
# ===========================================================================


class TestSupersedesRelationshipInsertion:
    """``Supersedes`` Relationship is INSERTed exactly once per
    predecessor case."""

    def test_supersedes_row_inserted_exactly_once_with_predecessor(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """``COUNT(*) FROM Relationships WHERE relationship_type='Supersedes'
        AND source_id = new_plan_revision_id`` is exactly 1.

        This is the headline assertion of Requirement 7.3: a Draft
        predecessor produces exactly one ``Supersedes`` Relationship
        row binding the new Plan Revision (source) to the predecessor
        (target).
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PREDECESSOR_ID,
            activity_plan_id=_ACTIVITY_PLAN_ID,
            lifecycle_state="draft",
        )

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1 — revision 2.",
                predecessor_plan_revision_id=_DRAFT_PREDECESSOR_ID,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        # Exactly one Supersedes row whose source is the new Plan
        # Revision.
        assert _count_supersedes_with_source(
            planning_engine, result.plan_revision_id
        ) == 1
        # The result carries the Identity of that Supersedes row.
        assert result.supersedes_relationship_id is not None
        assert _CANONICAL_UUID7.match(result.supersedes_relationship_id)

    def test_supersedes_row_columns_pin_source_target_and_type(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """The single inserted ``Supersedes`` row carries
        ``source_id = new_id``, ``target_id = predecessor_id``,
        ``source_kind = target_kind = 'plan_revision'``, and
        ``semantic_role IS NULL`` (the AD-WS-17 column is reserved
        for Plan Review's ``'review'`` discriminator).
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PREDECESSOR_ID,
            activity_plan_id=_ACTIVITY_PLAN_ID,
            lifecycle_state="draft",
        )

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1 — revision 2.",
                predecessor_plan_revision_id=_DRAFT_PREDECESSOR_ID,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        with planning_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, source_kind,
                           source_id, target_kind, target_id, semantic_role
                    FROM Relationships
                    WHERE relationship_type = 'Supersedes'
                      AND source_id = :sid
                    """
                ),
                {"sid": result.plan_revision_id},
            ).mappings().one()

        assert row["relationship_id"] == result.supersedes_relationship_id
        assert row["relationship_type"] == "Supersedes"
        assert row["source_kind"] == "plan_revision"
        assert row["source_id"] == result.plan_revision_id
        assert row["target_kind"] == "plan_revision"
        assert row["target_id"] == _DRAFT_PREDECESSOR_ID
        # ``semantic_role`` is NULL on Supersedes rows; the AD-WS-17
        # column is reserved for Plan Review's ``'review'`` marker.
        assert row["semantic_role"] is None

    def test_no_supersedes_row_when_predecessor_omitted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """Without a predecessor, zero ``Supersedes`` rows are
        inserted; the result's ``supersedes_relationship_id`` is
        ``None``.

        This is the symmetric assertion to the headline Requirement
        7.3 case: the ``Supersedes`` INSERT is gated on the
        predecessor argument, so an omitted predecessor results in
        no edge.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)

        # Snapshot before so the assertion stays meaningful even if a
        # future test fixture seeds Relationships rows.
        all_supersedes_before = _count_supersedes_relationships(planning_engine)

        with planning_engine.begin() as conn:
            result = plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1.",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.supersedes_relationship_id is None
        assert _count_supersedes_with_source(
            planning_engine, result.plan_revision_id
        ) == 0
        assert (
            _count_supersedes_relationships(planning_engine)
            == all_supersedes_before
        )

    def test_predecessor_row_byte_equivalent_after_supersession(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_revision_service: PlanRevisionService,
    ) -> None:
        """Recording a ``Supersedes`` Relationship leaves the
        predecessor Plan Revision row byte-equivalent to its prior
        state (Requirement 7.3).

        The supersession edge is recorded in ``Relationships`` only;
        the Plan Revision row itself is never UPDATEd.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PREDECESSOR_ID,
            activity_plan_id=_ACTIVITY_PLAN_ID,
            lifecycle_state="draft",
        )

        with planning_engine.connect() as conn:
            pred_before = conn.execute(
                text(
                    "SELECT plan_revision_id, activity_plan_id, "
                    "predecessor_revision_id, lifecycle_state, planned_scope, "
                    "deliverable_expectation_refs_json, "
                    "planning_assumptions_json, ordering_rationale, "
                    "authoring_party_id, applicable_scope, recorded_at "
                    "FROM Plan_Revisions WHERE plan_revision_id=:id"
                ),
                {"id": _DRAFT_PREDECESSOR_ID},
            ).mappings().one()

        with planning_engine.begin() as conn:
            plan_revision_service.create_plan_revision(
                conn,
                target_activity_plan_id=_ACTIVITY_PLAN_ID,
                planned_scope="Phase 1 — revision 2.",
                predecessor_plan_revision_id=_DRAFT_PREDECESSOR_ID,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        with planning_engine.connect() as conn:
            pred_after = conn.execute(
                text(
                    "SELECT plan_revision_id, activity_plan_id, "
                    "predecessor_revision_id, lifecycle_state, planned_scope, "
                    "deliverable_expectation_refs_json, "
                    "planning_assumptions_json, ordering_rationale, "
                    "authoring_party_id, applicable_scope, recorded_at "
                    "FROM Plan_Revisions WHERE plan_revision_id=:id"
                ),
                {"id": _DRAFT_PREDECESSOR_ID},
            ).mappings().one()

        assert dict(pred_after) == dict(pred_before)


def _count_supersedes_relationships(engine: Engine) -> int:
    """Count every ``Supersedes`` Relationship row, regardless of source.

    Used by the no-predecessor symmetric assertion in
    :class:`TestSupersedesRelationshipInsertion`.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = 'Supersedes'"
                )
            ).scalar_one()
        )
