"""Unit tests for :mod:`walking_slice.execution.work_assignments` (task 5.2).

Pins the contract established in task 5.1, design
§"Execution_Service.WorkAssignments", AD-WS-9 (separate-transaction
Denial Record), AD-WS-24 (``create.work_assignment`` → ``assign``),
AD-WS-26 (Relationship-Type / semantic-role table), AD-WS-27
(append-only Slice 3 tables), AD-WS-28 (additive ``resource_kind``
values), AD-WS-30 (Planning_Service read API), and Requirements 23.3
through 23.9 plus 32.6 and 33.4:

- **23.3 — assignment-rationale length boundaries.** A request must
  carry 0..4000 characters. Validation runs in the static validator
  before any database read so a malformed request never touches the
  Planning_Service read API, the ``Parties`` lookup, or the
  authorization service. The schema CHECK constraint on
  ``Work_Assignment_Records`` is the defense-in-depth layer.
- **23.3 — authority-basis-type enumeration validation.** Only
  ``{role-grant-id, scope-id, delegation-chain-id}`` (AD-WS-10) are
  accepted; any other value (or a missing ``type``) raises
  :class:`WorkAssignmentValidationError` with a precise
  ``failed_constraint``.
- **23.4 — target Plan Revision outcomes.** Unresolvable, ``draft``,
  ``approved`` (happy path), and scope-mismatched Plan Revisions
  surface distinct exception types; the four checks run before
  authorization evaluation so the deny path never leaks Plan Revision
  existence or lifecycle state to an unauthorized caller.
- **23.5 — assignee outcomes.** Unresolvable assignee Party and
  self-assignment (``assignment_authority_party_id == assignee_party_id``)
  raise dedicated exception types; the slice schema does not yet model
  a Party ``inactive`` flag so only the existence branch is exercised
  here (see the inactive-stub test for the deferred ADR note).
- **23.6 — authorization deny path.** A denied request appends exactly
  one Denial Record in a separate transaction and raises
  :class:`WorkAssignmentAuthorizationError`; no
  ``Work_Assignment_Records`` row, no ``Relationships`` row, and no
  consequential audit row is persisted.
- **23.7 — required attribute boundaries.** Empty / missing required
  string attributes raise :class:`WorkAssignmentValidationError` with
  the precise ``failed_constraint`` identifying the missing attribute.
- **23.9 — immutability.** ``Work_Assignment_Records`` rejects UPDATE
  and DELETE via the AD-WS-27 append-only triggers; the
  ``Relationships`` rows inserted alongside are governed by the
  Slice 1 / Slice 2 append-only trigger.
- **32.6** — the action ``create.work_assignment`` maps to the
  ``assign`` authority; an effective Role Assignment granting
  ``assign`` over the requested scope is required to permit the
  write.
- **33.4** — prohibited-attribute rejection identifies every offending
  top-level key in the original request body.

The tests mirror the style of
``tests/unit/test_planning_plan_revisions.py``: a per-test engine
carrying both the Slice 1 + Slice 2 + Slice 3 schemas, a real
:class:`AuthorizationService` driven through a seeded role assignment
on happy paths, direct INSERTs to seed Slice 2 dependency rows, and
counter helpers that confirm nothing was persisted on negative paths.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.work_assignments import (
    CreateWorkAssignmentResult,
    WorkAssignmentAssigneeNotResolvableError,
    WorkAssignmentAuthorizationError,
    WorkAssignmentPlanRevisionNotApprovedError,
    WorkAssignmentPlanRevisionNotResolvableError,
    WorkAssignmentPlanRevisionScopeMismatchError,
    WorkAssignmentSelfAssignmentError,
    WorkAssignmentService,
    WorkAssignmentValidationError,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Identifiers.
# ---------------------------------------------------------------------------


_AUTHORITY_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNEE_PARTY_ID = "00000000-0000-7000-8000-000000a00002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00003"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_APPROVED_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00030"
_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00031"
_OTHER_SCOPE_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00032"
_UNRESOLVABLE_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000deadbe01"
_UNRESOLVABLE_ASSIGNEE_PARTY_ID = "00000000-0000-7000-8000-0000deadbe02"

_SCOPE = "pilot/team-a"
_OTHER_SCOPE = "production/team-b"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def execution_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas.

    The Work Assignment Service does not interact with the
    Deliverable_Repository tables directly, so the Slice 3
    Deliverable_Repository schema is intentionally not installed
    here — task 1.3 owns its own dedicated test surface. The
    Slice 2 Planning schema is required because the service
    resolves the target Plan Revision via the AD-WS-30 read API
    against ``Plan_Revisions``.
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    return engine


@pytest.fixture
def plan_revision_reader() -> PlanRevisionService:
    """Bare :class:`PlanRevisionService` instance for the AD-WS-30 read API.

    Only :meth:`PlanRevisionService.get_plan_revision` is consulted by
    the Work Assignment Service; that method is a classmethod-style
    read so the instance does not need wired collaborators.
    """
    return PlanRevisionService(
        clock=None,  # type: ignore[arg-type]
        identity_service=None,  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        authorization_service=None,  # type: ignore[arg-type]
    )


@pytest.fixture
def work_assignment_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    plan_revision_reader: PlanRevisionService,
) -> WorkAssignmentService:
    """:class:`WorkAssignmentService` wired with a real
    :class:`AuthorizationService` and a sleep stub.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test. The denial-audit
    sleep is replaced with a no-op so the deny-path retries do not
    spend real time.
    """
    return WorkAssignmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        denial_audit_sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
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
    """Seed the Authority Party, the Assignee Party, and the
    Assigning-Authority Party so every FK and audit-row reference
    resolves.
    """
    with engine.begin() as conn:
        _seed_party(conn, _AUTHORITY_PARTY_ID, "Assignment Authority")
        _seed_party(conn, _ASSIGNEE_PARTY_ID, "Contributor")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_project(engine: Engine, project_id: str = _PROJECT_ID) -> None:
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
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Mesh Rollout Activities',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": activity_plan_id,
                "pid": project_id,
                "party": _AUTHORITY_PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    lifecycle_state: str = "approved",
    applicable_scope: str = _SCOPE,
) -> None:
    """Seed one ``Plan_Revisions`` row by direct INSERT.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so a row
    with ``lifecycle_state = 'approved'`` may be inserted in one
    statement without driving the Plan Approval transaction or
    setting any session pragma (mirrors the pattern in
    ``tests/unit/test_planning_slice3_read_apis.py``).
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
                    :rev, :aid, NULL, :state, 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "party": _AUTHORITY_PARTY_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _assign_assignment_authority_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _AUTHORITY_PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Assignment-Authority role granting ``assign`` to ``party_id``.

    Per AD-WS-24, ``create.work_assignment`` maps to the ``assign``
    authority. A Party with an effective Role Assignment carrying
    ``assign`` over ``scope`` is permitted to create Work Assignments
    addressing an Approved Plan Revision in that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="assignment_authority",
        scope=scope,
        authorities_granted=("assign",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


# ---------------------------------------------------------------------------
# Row counters and readers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_relationships_with_source(
    engine: Engine,
    *,
    relationship_type: str,
    source_id: str,
    semantic_role: Optional[str] = None,
) -> int:
    """Count ``Relationships`` rows whose ``source_id`` is ``source_id``.

    When ``semantic_role`` is supplied the match is exact;
    ``None`` matches rows whose ``semantic_role IS NULL``.
    """
    sql = (
        "SELECT COUNT(*) FROM Relationships "
        "WHERE relationship_type = :rt AND source_id = :sid "
    )
    params: dict = {"rt": relationship_type, "sid": source_id}
    if semantic_role is None:
        sql += "AND semantic_role IS NULL"
    else:
        sql += "AND semantic_role = :role"
        params["role"] = semantic_role
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def _count_denial_audit_rows(engine: Engine, action_type: str) -> int:
    """Count denial ``Audit_Records`` for ``action_type``.

    A Denial Record is distinguished from the authorization evaluation
    row (which also carries ``outcome='deny'``) by
    ``authorities_required`` being NULL — the evaluation row always
    populates that column with the JSON-encoded required authority.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE outcome = 'deny' "
                    "AND action_type = :a "
                    "AND authorities_required IS NULL"
                ),
                {"a": action_type},
            ).scalar_one()
        )


def _count_consequential_audit_rows(engine: Engine, action_type: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE outcome = 'consequential' "
                    "AND action_type = :a"
                ),
                {"a": action_type},
            ).scalar_one()
        )


# ===========================================================================
# Happy path baseline — confirms wiring and AD-WS-26 Relationship inserts.
# ===========================================================================


def _seed_happy_path(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
) -> None:
    """Seed every dependency for the happy-path Work Assignment.

    Encapsulated as a helper because the positive boundary tests
    (rationale 0 / 4000 chars; valid authority basis types) all run
    against the same set of pre-seeded rows.
    """
    _seed_required_parties(execution_engine)
    _assign_assignment_authority_role(authorization_service, execution_engine)
    _seed_project(execution_engine)
    _seed_activity_plan(execution_engine)
    _seed_plan_revision(
        execution_engine,
        plan_revision_id=_APPROVED_PLAN_REVISION_ID,
        lifecycle_state="approved",
        applicable_scope=_SCOPE,
    )


def test_create_work_assignment_permits_with_one_addresses_and_one_relates_to(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
    work_assignment_service: WorkAssignmentService,
) -> None:
    """Happy path: an authorized authority records exactly one Work
    Assignment, one ``Addresses`` Relationship to the Plan Revision,
    and one ``Relates To`` Relationship to the assignee Party with
    ``semantic_role = 'assignee'`` (AD-WS-26).

    This is the headline assertion for Requirements 23.1 and 23.8:
    the consequential audit row participates in the same transaction
    so the count is exactly one.
    """
    _seed_happy_path(execution_engine, authorization_service)

    with execution_engine.begin() as conn:
        result = work_assignment_service.create_work_assignment(
            conn,
            target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
            assignee_party_id=_ASSIGNEE_PARTY_ID,
            assignment_authority_party_id=_AUTHORITY_PARTY_ID,
            assignment_rationale="Assigning the rollout to the assignee.",
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=execution_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateWorkAssignmentResult)
    assert _CANONICAL_UUID7.match(result.work_assignment_id)
    assert result.target_plan_revision_id == _APPROVED_PLAN_REVISION_ID
    assert result.assignee_party_id == _ASSIGNEE_PARTY_ID
    assert result.assignment_authority_party_id == _AUTHORITY_PARTY_ID
    assert result.correlation_id == "corr-permit"

    # Exactly one Work Assignment Record.
    assert _count(execution_engine, "Work_Assignment_Records") == 1

    # Exactly one Addresses Relationship to the Plan Revision with
    # NULL semantic_role per AD-WS-26 row 1.
    assert _count_relationships_with_source(
        execution_engine,
        relationship_type="Addresses",
        source_id=result.work_assignment_id,
        semantic_role=None,
    ) == 1

    # Exactly one Relates To Relationship to the assignee Party
    # carrying semantic_role='assignee' per AD-WS-26 row 2.
    assert _count_relationships_with_source(
        execution_engine,
        relationship_type="Relates To",
        source_id=result.work_assignment_id,
        semantic_role="assignee",
    ) == 1

    # Exactly one consequential audit row.
    assert _count_consequential_audit_rows(
        execution_engine, "create.work_assignment"
    ) == 1


def test_relationship_rows_pin_target_columns_per_ad_ws_26(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
    work_assignment_service: WorkAssignmentService,
) -> None:
    """The two Relationship rows carry the AD-WS-26 column shape.

    - ``Addresses`` row: ``source_kind='work_assignment_record'``,
      ``target_kind='plan_revision'``, ``target_id=plan_revision_id``,
      ``target_revision_id IS NULL``, ``semantic_role IS NULL``.
    - ``Relates To`` row: ``source_kind='work_assignment_record'``,
      ``target_kind='party'``, ``target_id=assignee_party_id``,
      ``target_revision_id IS NULL``, ``semantic_role='assignee'``.
    """
    _seed_happy_path(execution_engine, authorization_service)

    with execution_engine.begin() as conn:
        result = work_assignment_service.create_work_assignment(
            conn,
            target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
            assignee_party_id=_ASSIGNEE_PARTY_ID,
            assignment_authority_party_id=_AUTHORITY_PARTY_ID,
            assignment_rationale="Assigning the rollout.",
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=execution_engine,
        )

    with execution_engine.connect() as conn:
        addresses_row = conn.execute(
            text(
                """
                SELECT relationship_id, source_kind, source_id,
                       source_revision_id, target_kind, target_id,
                       target_revision_id, semantic_role
                FROM Relationships
                WHERE relationship_type = 'Addresses'
                  AND source_id = :sid
                """
            ),
            {"sid": result.work_assignment_id},
        ).mappings().one()
        relates_to_row = conn.execute(
            text(
                """
                SELECT relationship_id, source_kind, source_id,
                       source_revision_id, target_kind, target_id,
                       target_revision_id, semantic_role
                FROM Relationships
                WHERE relationship_type = 'Relates To'
                  AND source_id = :sid
                """
            ),
            {"sid": result.work_assignment_id},
        ).mappings().one()

    assert addresses_row["relationship_id"] == result.addresses_relationship_id
    assert addresses_row["source_kind"] == "work_assignment_record"
    assert addresses_row["source_id"] == result.work_assignment_id
    assert addresses_row["source_revision_id"] is None
    assert addresses_row["target_kind"] == "plan_revision"
    assert addresses_row["target_id"] == _APPROVED_PLAN_REVISION_ID
    assert addresses_row["target_revision_id"] is None
    assert addresses_row["semantic_role"] is None

    assert relates_to_row["relationship_id"] == (
        result.relates_to_relationship_id
    )
    assert relates_to_row["source_kind"] == "work_assignment_record"
    assert relates_to_row["source_id"] == result.work_assignment_id
    assert relates_to_row["source_revision_id"] is None
    assert relates_to_row["target_kind"] == "party"
    assert relates_to_row["target_id"] == _ASSIGNEE_PARTY_ID
    assert relates_to_row["target_revision_id"] is None
    assert relates_to_row["semantic_role"] == "assignee"


# ===========================================================================
# Requirement 23.4 — target Plan Revision outcomes.
# ===========================================================================


class TestPlanRevisionResolutionOutcomes:
    """Unresolvable, draft, approved, and scope-mismatched Plan
    Revisions exercise distinct branches of Requirement 23.4.
    """

    def test_unresolvable_plan_revision_raises_dedicated_error(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """An identifier that does not resolve raises
        :class:`WorkAssignmentPlanRevisionNotResolvableError`.

        The check runs before authorization evaluation so the deny
        path never reveals whether the Plan Revision exists to an
        unauthorized caller.
        """
        _seed_required_parties(execution_engine)
        _assign_assignment_authority_role(
            authorization_service, execution_engine
        )
        # No Plan Revision seeded.

        with pytest.raises(
            WorkAssignmentPlanRevisionNotResolvableError
        ) as exc_info:
            with execution_engine.begin() as conn:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _UNRESOLVABLE_PLAN_REVISION_ID
        )
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_not_resolvable"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0

    def test_draft_plan_revision_raises_not_approved_error(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A Plan Revision whose ``lifecycle_state`` is ``'draft'``
        raises :class:`WorkAssignmentPlanRevisionNotApprovedError`
        carrying the observed lifecycle state verbatim.

        Requirement 23.2 / 23.4: the target Plan Revision must be
        ``'approved'`` at the recorded time.
        """
        _seed_required_parties(execution_engine)
        _assign_assignment_authority_role(
            authorization_service, execution_engine
        )
        _seed_project(execution_engine)
        _seed_activity_plan(execution_engine)
        _seed_plan_revision(
            execution_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with pytest.raises(
            WorkAssignmentPlanRevisionNotApprovedError
        ) as exc_info:
            with execution_engine.begin() as conn:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _DRAFT_PLAN_REVISION_ID
        )
        assert exc_info.value.observed_lifecycle_state == "draft"
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_not_approved"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0

    def test_approved_plan_revision_is_accepted(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """The ``approved`` branch is the happy path; the call
        succeeds and the result carries the requested identifier.

        The symmetric assertion to the ``draft`` rejection: the same
        seeded scenario with ``lifecycle_state='approved'`` succeeds.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with execution_engine.begin() as conn:
            result = work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale="Assigning.",
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        assert result.target_plan_revision_id == _APPROVED_PLAN_REVISION_ID
        assert _count(execution_engine, "Work_Assignment_Records") == 1

    def test_scope_mismatched_plan_revision_raises_dedicated_error(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A Plan Revision whose ``applicable_scope`` differs from the
        request's ``applicable_scope`` raises
        :class:`WorkAssignmentPlanRevisionScopeMismatchError`
        carrying both scopes verbatim.

        Requirement 23.4: the Plan Revision's applicable scope must
        be within the requesting Party's Assignment Authority scope.
        The slice uses exact-equality coverage.
        """
        _seed_required_parties(execution_engine)
        _assign_assignment_authority_role(
            authorization_service, execution_engine
        )
        _seed_project(execution_engine)
        _seed_activity_plan(execution_engine)
        _seed_plan_revision(
            execution_engine,
            plan_revision_id=_OTHER_SCOPE_PLAN_REVISION_ID,
            lifecycle_state="approved",
            applicable_scope=_OTHER_SCOPE,
        )

        with pytest.raises(
            WorkAssignmentPlanRevisionScopeMismatchError
        ) as exc_info:
            with execution_engine.begin() as conn:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_OTHER_SCOPE_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _OTHER_SCOPE_PLAN_REVISION_ID
        )
        assert exc_info.value.plan_revision_scope == _OTHER_SCOPE
        assert exc_info.value.requested_scope == _SCOPE
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_scope_mismatch"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0


# ===========================================================================
# Requirement 23.5 — assignee outcomes.
# ===========================================================================


class TestAssigneeResolutionAndSelfAssignment:
    """Self-assignment and unresolvable-assignee paths raise distinct
    exception types per Requirement 23.5.

    The slice schema does not yet model a Party ``inactive`` flag, so
    only the existence branch of Requirement 23.5 is exercised here.
    A future ADR may introduce a status column at which point the
    inactive branch will be added with a distinct
    ``failed_constraint``.
    """

    def test_self_assignment_rejected_before_authorization(
        self,
        execution_engine: Engine,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A request where the assignment authority and the assignee
        are the same Party raises
        :class:`WorkAssignmentSelfAssignmentError` with
        ``failed_constraint='self_assignment_forbidden'``.

        The rejection runs before authorization evaluation and before
        any database read so the deny path never reveals whether the
        requesting Party would have been able to assign the Plan
        Revision to a different Party.
        """
        # No seeding required — the rejection fires before any DB read.

        with pytest.raises(WorkAssignmentSelfAssignmentError) as exc_info:
            with execution_engine.begin() as conn:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_AUTHORITY_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Self-assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.party_id == _AUTHORITY_PARTY_ID
        assert exc_info.value.failed_constraint == "self_assignment_forbidden"
        assert _count(execution_engine, "Work_Assignment_Records") == 0

    def test_unresolvable_assignee_raises_dedicated_error(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """An assignee Party Identity that does not resolve raises
        :class:`WorkAssignmentAssigneeNotResolvableError` with
        ``failed_constraint='assignee_party_not_resolvable'``.

        The rejection runs before authorization evaluation so the
        deny path never reveals whether a Party exists for an
        unauthorized caller.
        """
        _seed_required_parties(execution_engine)
        _assign_assignment_authority_role(
            authorization_service, execution_engine
        )
        _seed_project(execution_engine)
        _seed_activity_plan(execution_engine)
        _seed_plan_revision(
            execution_engine,
            plan_revision_id=_APPROVED_PLAN_REVISION_ID,
            lifecycle_state="approved",
        )

        with pytest.raises(
            WorkAssignmentAssigneeNotResolvableError
        ) as exc_info:
            with execution_engine.begin() as conn:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_UNRESOLVABLE_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.assignee_party_id == (
            _UNRESOLVABLE_ASSIGNEE_PARTY_ID
        )
        assert exc_info.value.failed_constraint == (
            "assignee_party_not_resolvable"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0


# ===========================================================================
# Requirement 23.3 — assignment-rationale length boundaries.
# ===========================================================================


class TestAssignmentRationaleBoundaries:
    """Per Requirement 23.3 the rationale must be 0..4000 characters
    or omitted (the column is NULLable).
    """

    def test_zero_length_rationale_accepted(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """An empty string sits at the lower boundary (length 0) and
        is persisted verbatim.

        The schema CHECK constraint
        ``length(assignment_rationale) BETWEEN 0 AND 4000`` admits the
        empty string; this test pins the service surface to the same
        admission policy.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with execution_engine.begin() as conn:
            result = work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale="",
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        assert result.assignment_rationale == ""
        with execution_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT assignment_rationale FROM Work_Assignment_Records "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": result.work_assignment_id},
            ).scalar_one()
        assert stored == ""

    def test_four_thousand_char_rationale_accepted(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A 4000-char rationale sits at the upper boundary."""
        _seed_happy_path(execution_engine, authorization_service)

        rationale = "x" * 4_000
        with execution_engine.begin() as conn:
            result = work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale=rationale,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        assert result.assignment_rationale == rationale
        assert len(result.assignment_rationale) == 4_000
        assert _count(execution_engine, "Work_Assignment_Records") == 1

    def test_four_thousand_one_char_rationale_rejected(
        self,
        execution_engine: Engine,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """``len(rationale) == 4001`` raises with stable constraint
        ``'assignment_rationale_too_long'`` and persists nothing.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        _seed_required_parties(execution_engine)

        with execution_engine.begin() as conn:
            with pytest.raises(WorkAssignmentValidationError) as exc_info:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="x" * 4_001,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == (
            "assignment_rationale_too_long"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0
        assert _count(execution_engine, "Relationships") == 0


# ===========================================================================
# Requirement 23.3 — authority-basis-type enumeration validation.
# ===========================================================================


class TestAuthorityBasisTypeEnumeration:
    """Per AD-WS-10 / Requirement 23.3 the ``type`` is drawn from
    ``{role-grant-id, scope-id, delegation-chain-id}``.
    """

    @pytest.mark.parametrize(
        "valid_type",
        ["role-grant-id", "scope-id", "delegation-chain-id"],
    )
    def test_valid_authority_basis_types_accepted(
        self,
        valid_type: str,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """Each of the three AD-WS-10 ``type`` values is accepted.

        Mapping-shaped input (the form the HTTP layer may forward
        before the request is bound to the typed model) is normalized
        through :class:`AuthorityBasisRef` and persisted on the row.
        """
        _seed_happy_path(execution_engine, authorization_service)
        basis_mapping = {"type": valid_type, "id": str(_AUTHORITY_BASIS_ID)}

        with execution_engine.begin() as conn:
            result = work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale="Assigning.",
                authority_basis=basis_mapping,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        assert result.authority_basis.type == valid_type
        with execution_engine.connect() as conn:
            stored_type = conn.execute(
                text(
                    "SELECT authority_basis_type FROM Work_Assignment_Records "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": result.work_assignment_id},
            ).scalar_one()
        assert stored_type == valid_type

    def test_unknown_authority_basis_type_rejected(
        self,
        execution_engine: Engine,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A ``type`` value outside the AD-WS-10 set raises with
        ``failed_constraint='authority_basis_type_out_of_set'``.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        _seed_required_parties(execution_engine)
        basis_mapping = {
            "type": "not-a-valid-type",
            "id": str(_AUTHORITY_BASIS_ID),
        }

        with execution_engine.begin() as conn:
            with pytest.raises(WorkAssignmentValidationError) as exc_info:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=basis_mapping,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == (
            "authority_basis_type_out_of_set"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0

    def test_missing_authority_basis_type_rejected(
        self,
        execution_engine: Engine,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A mapping with no ``type`` key raises with
        ``failed_constraint='authority_basis_type_missing'``.

        Distinct from the out-of-set rejection so the route layer
        can pinpoint which authority-basis field is malformed.
        """
        _seed_required_parties(execution_engine)
        basis_mapping = {"id": str(_AUTHORITY_BASIS_ID)}

        with execution_engine.begin() as conn:
            with pytest.raises(WorkAssignmentValidationError) as exc_info:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=basis_mapping,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == (
            "authority_basis_type_missing"
        )
        assert _count(execution_engine, "Work_Assignment_Records") == 0


# ===========================================================================
# Requirement 33.4 / 34.5 — prohibited-attribute rejection.
# ===========================================================================


class TestProhibitedAttributeRejection:
    """The request body is screened against every prohibited
    planning-attribute and observed-outcome prefix per Requirements
    33.2/33.3/33.4 and 34.1/34.2/34.5.

    The ``request_attributes`` keyword argument carries the original
    top-level mapping forwarded by the route layer; the screen runs
    *before* validation of any required attribute so the rejection
    surfaces even when the typed kwargs themselves are well-formed.
    """

    def test_planning_attribute_rejected_with_offending_key(
        self,
        execution_engine: Engine,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A key starting with ``planned-`` raises
        :class:`WorkAssignmentValidationError` with
        ``failed_constraint='prohibited_attribute'`` and the
        offending key listed.

        Requirement 33.4 demands the response identify every
        prohibited attribute; the service surfaces them through the
        ``prohibited_keys`` tuple in source order.
        """
        _seed_required_parties(execution_engine)
        forbidden_body = {
            "target_plan_revision_id": _APPROVED_PLAN_REVISION_ID,
            "assignee_party_id": _ASSIGNEE_PARTY_ID,
            "planned_scope": "should be rejected",
        }

        with execution_engine.begin() as conn:
            with pytest.raises(WorkAssignmentValidationError) as exc_info:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                    request_attributes=forbidden_body,
                )

        assert exc_info.value.failed_constraint == "prohibited_attribute"
        assert exc_info.value.prohibited_keys == ("planned_scope",)
        assert _count(execution_engine, "Work_Assignment_Records") == 0

    def test_observed_outcome_attribute_rejected_with_offending_key(
        self,
        execution_engine: Engine,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A key starting with ``observed-`` raises with
        ``failed_constraint='prohibited_attribute'`` and the
        offending key listed.

        Requirement 34.5: the service must reject observed-outcome
        attributes on every execution Record write.
        """
        _seed_required_parties(execution_engine)
        forbidden_body = {
            "assignee_party_id": _ASSIGNEE_PARTY_ID,
            "observed_outcome_value": 42,
        }

        with execution_engine.begin() as conn:
            with pytest.raises(WorkAssignmentValidationError) as exc_info:
                work_assignment_service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                    assignment_rationale="Assigning.",
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                    request_attributes=forbidden_body,
                )

        assert exc_info.value.failed_constraint == "prohibited_attribute"
        assert exc_info.value.prohibited_keys == ("observed_outcome_value",)
        assert _count(execution_engine, "Work_Assignment_Records") == 0


# ===========================================================================
# Requirement 23.6 — authorization deny path.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    execution_engine: Engine,
    work_assignment_service: WorkAssignmentService,
) -> None:
    """A denied request appends exactly one Denial Record in a
    separate transaction and raises
    :class:`WorkAssignmentAuthorizationError`.

    Requirement 23.6 / AD-WS-9: the authorization deny path uses the
    Slice 1 separate-transaction Denial-Record pattern. The
    caller's transaction rolls back so no
    ``Work_Assignment_Records`` row, no ``Relationships`` row, and
    no consequential audit row is persisted; exactly one denial row
    (``outcome='deny'`` with ``authorities_required IS NULL``)
    survives in its own transaction.
    """
    # Seed everything required for the request to reach the
    # authorization evaluation step. Crucially, no Role Assignment is
    # seeded so the evaluator returns ``deny('no-role-assignment')``.
    _seed_required_parties(execution_engine)
    _seed_project(execution_engine)
    _seed_activity_plan(execution_engine)
    _seed_plan_revision(
        execution_engine,
        plan_revision_id=_APPROVED_PLAN_REVISION_ID,
        lifecycle_state="approved",
    )

    correlation = "corr-work-assignment-deny"
    with pytest.raises(WorkAssignmentAuthorizationError) as exc_info:
        with execution_engine.begin() as conn:
            work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale="Assigning.",
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
                correlation_id=correlation,
            )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation

    # Caller's transaction rolled back: no Work Assignment / Relationship
    # / consequential audit row was persisted.
    assert _count(execution_engine, "Work_Assignment_Records") == 0
    assert _count_relationships_with_source(
        execution_engine,
        relationship_type="Addresses",
        source_id=_APPROVED_PLAN_REVISION_ID,
    ) == 0
    assert _count_consequential_audit_rows(
        execution_engine, "create.work_assignment"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (Requirement 23.6 / AD-WS-9).
    assert _count_denial_audit_rows(
        execution_engine, "create.work_assignment"
    ) == 1


# ===========================================================================
# Requirement 23.9 / AD-WS-27 — immutability rejection of UPDATE / DELETE.
# ===========================================================================


class TestWorkAssignmentRecordImmutability:
    """Per Requirement 23.9 / AD-WS-27, after a Work Assignment
    Record is finalized the row rejects every UPDATE / DELETE
    attempt via the append-only triggers installed by task 1.2.

    The triggers raise ``RAISE(ABORT, ...)`` which SQLAlchemy
    surfaces as :class:`sqlalchemy.exc.IntegrityError`.
    """

    def test_update_rejected_after_persistence(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """An UPDATE on ``Work_Assignment_Records`` after commit
        raises ``IntegrityError`` and leaves the row unchanged.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with execution_engine.begin() as conn:
            result = work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale="Assigning.",
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        # Snapshot the row body so a successful UPDATE attempt would
        # be caught by the equality check below.
        with execution_engine.connect() as conn:
            before = conn.execute(
                text(
                    "SELECT * FROM Work_Assignment_Records "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": result.work_assignment_id},
            ).mappings().one()

        with execution_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Work_Assignment_Records "
                        "SET assignment_rationale = 'tampered' "
                        "WHERE work_assignment_id = :id"
                    ),
                    {"id": result.work_assignment_id},
                )

        with execution_engine.connect() as conn:
            after = conn.execute(
                text(
                    "SELECT * FROM Work_Assignment_Records "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": result.work_assignment_id},
            ).mappings().one()
        assert dict(after) == dict(before)

    def test_delete_rejected_after_persistence(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_assignment_service: WorkAssignmentService,
    ) -> None:
        """A DELETE on ``Work_Assignment_Records`` after commit raises
        ``IntegrityError`` and the row remains in place.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with execution_engine.begin() as conn:
            result = work_assignment_service.create_work_assignment(
                conn,
                target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                assignee_party_id=_ASSIGNEE_PARTY_ID,
                assignment_authority_party_id=_AUTHORITY_PARTY_ID,
                assignment_rationale="Assigning.",
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        with execution_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "DELETE FROM Work_Assignment_Records "
                        "WHERE work_assignment_id = :id"
                    ),
                    {"id": result.work_assignment_id},
                )

        # Row survives the rejected DELETE.
        assert _count(execution_engine, "Work_Assignment_Records") == 1
        with execution_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT work_assignment_id FROM Work_Assignment_Records "
                    "WHERE work_assignment_id = :id"
                ),
                {"id": result.work_assignment_id},
            ).scalar_one()
        assert still_present == result.work_assignment_id
