"""Unit tests for :mod:`walking_slice.execution.completions` (task 11.2).

Pins the contract established in task 11.1, design
§"Execution_Service.Completions", AD-WS-9 (separate-transaction
Denial Record), AD-WS-26 (Relationship-Type / semantic-role table —
the Completion Record carries exactly one ``Addresses`` row to the
target Approved Plan Revision with ``semantic_role IS NULL``),
AD-WS-27 (append-only Slice 3 tables), AD-WS-28 (additive
``resource_kind`` values), AD-WS-30 (read-only Planning_Service
entry points), and Requirements 29.1, 29.3, 29.4, 29.7, 29.8, 32.9,
34.3:

- **29.4 — target Plan Revision outcomes.** Unresolvable, ``draft``,
  and ``approved`` (happy path) Plan Revisions exercise three
  distinct branches. The unresolvable and draft branches surface
  distinct exception types (:class:`CompletionPlanRevisionNotResolvableError`
  / :class:`CompletionPlanRevisionNotApprovedError`); both checks
  run before authorization evaluation so the deny path never leaks
  the Plan Revision's lifecycle state.

- **29.1 / 29.4 — accepted-Milestone existence check.** Zero accepted
  Milestones with an empty ``source_milestone_acceptance_ids`` list
  is rejected with :class:`CompletionNoAcceptedMilestonesError`. A
  supplied identifier list containing at least one entry that does
  not resolve to an ``Accept``-outcome Milestone Acceptance row for
  the target Plan Revision is rejected with
  :class:`CompletionSourceMilestoneAcceptanceNotResolvableError`.

- **29.3 — duplicate-Completion rejection.** A second Completion
  against the same target Plan Revision is rejected with
  :class:`CompletionConflictError`. Both layers are exercised: the
  application-level pre-check surfaces the structured error before
  the second INSERT; bypassing the service and inserting a second
  Completion Record directly against the same
  ``target_plan_revision_id`` fails on the schema-level
  ``UNIQUE(target_plan_revision_id)`` constraint
  (:class:`sqlalchemy.exc.IntegrityError`).

- **29.2 / 29.4 — outcome enumeration.** Each value in
  :data:`OUTCOME_VALUES` (``Completed``, ``Completed_With_Reservation``)
  is accepted and persisted byte-equivalent; any other string is
  rejected with ``failed_constraint='outcome_out_of_set'``; a
  missing / empty value is rejected with
  ``failed_constraint='outcome_missing'``.

- **29.2 / 29.4 — rationale length boundaries.** The 1-char and
  4000-char boundary values are accepted and persisted
  byte-equivalent; the 0-char value is rejected with
  ``'rationale_too_short'`` and the 4001-char value is rejected
  with ``'rationale_too_long'``.

- **29.7 / AD-WS-27 — immutability.** After a permitted Completion
  write, both ``Completion_Records`` and the ``Addresses``
  Relationship row reject UPDATE and DELETE via the append-only
  triggers (Slice 3 ``Completion_Records`` triggers installed in
  task 1.2; Slice 1 ``Relationships`` triggers installed in task
  1.x). Both surfaces raise :class:`sqlalchemy.exc.IntegrityError`.

- **29.7 / 29.8 / 40 §1 — Slice 1 / Slice 2 row byte-equivalence.**
  After a permitted Completion write every row the service is
  explicitly forbidden to mutate remains byte-equivalent to its
  pre-Completion state. The set captured here is the target Plan
  Revision, target Activity Plan, target Project, source Milestone
  Acceptance Record, source Deliverable Production Record, produced
  Deliverable Revision, target Deliverable Expectation Revision,
  and every seeded ``Parties`` row.

- **29.8 / 34.3 — Completion does not assert any observed Outcome.**
  Two complementary assertions pin this invariant: (1) the
  persisted ``Completion_Records`` row carries only the columns
  declared by the Slice 3 schema, none of which is an
  observed-outcome attribute (no ``observed_*``, no
  ``measurement_*``, no ``outcome_review_*``, no
  ``attribution_evidence_*``, no ``success_condition_assessment_*``
  column appears on the row); (2) a ``request_attributes`` mapping
  carrying any observed-outcome key on the request body is rejected
  with ``failed_constraint='prohibited_attribute'`` and the
  offending key listed on
  :attr:`CompletionValidationError.prohibited_keys`.

- **32.9** — the action ``create.completion`` maps to the
  ``complete`` authority; an effective Role Assignment granting
  ``complete`` over the requested scope is required to permit the
  write. The happy-path test seeds exactly that role.

The tests mirror the style of
``tests/unit/test_execution_milestone_acceptances.py`` (task 10.2)
and ``tests/unit/test_execution_work_assignments.py`` (task 5.2):
a per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas, a
real :class:`AuthorizationService` driven through a seeded role
assignment on happy paths, direct INSERTs to seed the Slice 2 /
Slice 3 dependency rows, and counter helpers that confirm nothing
was persisted on negative paths.

NOTE: ``create.completion`` requires the ``complete`` authority
(Requirement 32.9) rather than ``contribute``; the AD-WS-29
second-stage assignee-binding check does NOT apply (a Completion
Authority is by design a Party distinct from the assignees on the
Work Assignment Records that produced the rolled-up accepted
Milestones).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import (
    CompletionConflictError,
    CompletionNoAcceptedMilestonesError,
    CompletionPlanRevisionNotApprovedError,
    CompletionPlanRevisionNotResolvableError,
    CompletionService,
    CompletionSourceMilestoneAcceptanceNotResolvableError,
    CompletionValidationError,
    CreateCompletionResult,
    OUTCOME_VALUES,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


# Four Parties cover every test branch: the Completion Authority that
# drives the happy-path write; an alternate Party used as the
# Contributor / assignee on the source Work Assignment so the seeded
# graph is internally consistent; the Assignment Authority recorded
# on the seeded Work Assignment Record; and the assigning Resource
# Steward that signs the seeded Role Assignment.
_COMPLETING_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-000000a00002"
_ASSIGNMENT_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00003"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00004"

# The single Project the Slice 2 + Slice 3 dependency graph lives in.
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"

_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"

# Plan Revision identifiers covering each Requirement 29.4 branch.
_APPROVED_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00030"
_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00031"
_UNRESOLVABLE_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000deadbe01"

# A second approved Plan Revision used by the no-accepted-milestones
# test so the happy-path graph (which seeds an accepted Milestone
# rolling up to ``_APPROVED_PLAN_REVISION_ID``) is unaffected.
_LONELY_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00032"

# Work Assignment / Production / Acceptance / Deliverable identifiers.
_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00001"
_DELIVERABLE_ID = "00000000-0000-7000-8000-000000e00001"
_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000e00002"
_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-000000f00001"
_DELIVERABLE_EXPECTATION_REVISION_ID = (
    "00000000-0000-7000-8000-000000f00002"
)
_DELIVERABLE_PRODUCTION_ID = "00000000-0000-7000-8000-0000000d000a1"
_ACCEPT_ACCEPTANCE_ID = "00000000-0000-7000-8000-0000000d000b1"
_REJECT_ACCEPTANCE_ID = "00000000-0000-7000-8000-0000000d000b2"

_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def completion_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas.

    The Completion Service crosses three schemas:

    * Slice 1 (``Parties``, ``Identifier_Registry``, ``Audit_Records``,
      ``Role_Assignments``, ``Relationships``).
    * Slice 2 (``Projects``, ``Activity_Plans``, ``Plan_Revisions``,
      ``Deliverable_Expectations``,
      ``Deliverable_Expectation_Revisions``).
    * Slice 3 Execution_Service (``Work_Assignment_Records``,
      ``Deliverable_Production_Records``,
      ``Milestone_Acceptance_Records``, ``Completion_Records`` with
      the ``UNIQUE(target_plan_revision_id)`` constraint central to
      Requirement 29.3) and Deliverable_Repository
      (``Deliverable_Resources``, ``Deliverable_Revisions``) with
      their AD-WS-27 append-only triggers.
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


@pytest.fixture
def plan_revision_reader() -> PlanRevisionService:
    """Bare :class:`PlanRevisionService` instance for the AD-WS-30
    ``get_plan_revision`` read API.

    Only :meth:`PlanRevisionService.get_plan_revision` is consulted
    by the Completion Service; that method is a classmethod-style
    read so the instance does not need wired collaborators.
    """
    return PlanRevisionService(
        clock=None,  # type: ignore[arg-type]
        identity_service=None,  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        authorization_service=None,  # type: ignore[arg-type]
    )


@pytest.fixture
def project_resolver() -> ProjectResolver:
    """Bare :class:`ProjectResolver` for the AD-WS-30 read API.

    :meth:`ProjectResolver.resolve_project` is a pure indexed read
    against the caller-supplied connection; the resolver does not
    require wired collaborators.
    """
    return ProjectResolver()


@pytest.fixture
def completion_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    plan_revision_reader: PlanRevisionService,
    project_resolver: ProjectResolver,
) -> CompletionService:
    """:class:`CompletionService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test. The denial-audit
    sleep is replaced with a no-op so the deny-path retries do not
    spend real time.
    """
    return CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
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
    """Seed every Party identity referenced by the test surface.

    All four Parties are required: the Completion Authority (the
    recording Party), the Contributor (assignee on the source Work
    Assignment), the Assignment Authority (named on the seeded Work
    Assignment Record), and the assigning Resource Steward recorded
    on the seeded role.
    """
    with engine.begin() as conn:
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
        _seed_party(conn, _ASSIGNMENT_AUTHORITY_ID, "Assignment Authority")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_project(engine: Engine) -> None:
    """Seed one ``Projects`` row.

    Required as the FK target of ``Activity_Plans.target_project_id``
    and of ``Deliverable_Expectation_Revisions.target_project_id``.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIXED},
        )


def _seed_activity_plan(engine: Engine) -> None:
    """Seed one ``Activity_Plans`` row.

    The Plan Revision rows below carry this Activity Plan's Identity
    as their ``activity_plan_id`` foreign key. :class:`ProjectResolver`
    walks ``Activity_Plans.target_project_id`` to compute the owning
    Project Identity persisted on the Completion Record.
    """
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
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    lifecycle_state: str = "approved",
) -> None:
    """Seed one ``Plan_Revisions`` row by direct INSERT.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so a row
    with ``lifecycle_state = 'approved'`` may be inserted in one
    statement without driving the Plan Approval transaction
    (mirrors the pattern in
    ``tests/unit/test_execution_work_assignments.py``).
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
                "aid": _ACTIVITY_PLAN_ID,
                "state": lifecycle_state,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    target_plan_revision_id: str = _APPROVED_PLAN_REVISION_ID,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The Work Assignment names the Contributor as the assignee and
    the Assignment Authority as the assignment-authority. The
    Completion Service does not re-read this row (the
    ``create.completion`` action does not trigger the AD-WS-29
    second-stage assignee-binding check), but the source Deliverable
    Production Record below references it; the accepted-Milestone
    existence query joins through it to filter by
    ``target_plan_revision_id``.
    """
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
                    'Assigning the rollout.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_assignment_id,
                "prev": target_plan_revision_id,
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNMENT_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable(engine: Engine) -> None:
    """Insert one Deliverable Resource + Revision pair by direct INSERT.

    The Revision carries ``role_marker = 'generated_output'``
    (Requirement 26.2) and ``originating_work_assignment_id``
    pointing at the source Work Assignment. The Completion Service
    does not read these columns directly; the rows exist so that
    the Production Record's FKs and ``Produces`` Relationship have
    targets.
    """
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Mesh runbook', :ts)
                """
            ),
            {"did": _DELIVERABLE_ID, "ts": _TS_FIXED},
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
                "rev": _DELIVERABLE_REVISION_ID,
                "did": _DELIVERABLE_ID,
                "bytes": b"produced",
                "digest": digest,
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    """Insert one Deliverable Expectation header + first Revision row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _DELIVERABLE_EXPECTATION_ID, "ts": _TS_FIXED},
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
                    :rev, :did, NULL, :pid, 'Mesh Operations Runbook',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_production(engine: Engine) -> None:
    """Insert the source ``Deliverable_Production_Records`` row.

    The Completion Service's accepted-Milestone existence query
    joins through this row (Milestone Acceptance →
    Deliverable_Production_Records.source_work_assignment_id →
    Work Assignment.target_plan_revision_id).
    """
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
                    'Produced runbook for milestone one.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "wa": _WORK_ASSIGNMENT_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_milestone_acceptance(
    engine: Engine,
    *,
    milestone_acceptance_id: str = _ACCEPT_ACCEPTANCE_ID,
    outcome: str = "Accept",
) -> None:
    """Insert one ``Milestone_Acceptance_Records`` row by direct INSERT.

    The accepted-Milestone existence query in
    :meth:`CompletionService.create_completion` selects every
    ``Accept``-outcome Milestone Acceptance whose source Deliverable
    Production Record traces back through the requested Plan
    Revision. Seeding the row directly (rather than through the
    Milestone Acceptance Service) keeps the test scope narrow and
    avoids requiring an additional role assignment for the seed
    step.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Milestone_Acceptance_Records (
                    milestone_acceptance_id,
                    source_deliverable_production_id,
                    produced_deliverable_id,
                    produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    outcome, rationale, accepting_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :mid, :pid, :did, :rev, :exp_did, :exp_rev,
                    :outcome, 'Milestone one criteria satisfied.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": milestone_acceptance_id,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "outcome": outcome,
                "party": _COMPLETING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_complete_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _COMPLETING_PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Completion Authority (``complete``) to ``party_id``.

    Per Requirement 32.9 / AD-WS-24, ``create.completion`` maps to
    the ``complete`` authority. A Party with an effective Role
    Assignment carrying ``complete`` over ``scope`` is permitted to
    create Completion Records against that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="completion_authority",
        scope=scope,
        authorities_granted=("complete",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_happy_path(
    engine: Engine,
    authorization_service: AuthorizationService,
) -> None:
    """Seed every dependency required for a permitted
    :meth:`CompletionService.create_completion` call.

    The default arguments produce the canonical configuration:

    * one Project with one Activity Plan and one approved Plan
      Revision (``_APPROVED_PLAN_REVISION_ID``);
    * one Work Assignment targeting the approved Plan Revision;
    * one produced Deliverable Resource + Revision;
    * one Deliverable Expectation Revision targeting the Project;
    * one source Deliverable Production Record;
    * one ``Accept``-outcome Milestone Acceptance against the
      Production Record (so the accepted-Milestone existence
      query returns one row for the target Plan Revision);
    * one ``complete`` role assignment granting the Completion
      Authority over ``_SCOPE``.
    """
    _seed_required_parties(engine)
    _assign_complete_role(authorization_service, engine)
    _seed_project(engine)
    _seed_activity_plan(engine)
    _seed_plan_revision(engine, plan_revision_id=_APPROVED_PLAN_REVISION_ID)
    _seed_work_assignment(engine)
    _seed_deliverable(engine)
    _seed_deliverable_expectation(engine)
    _seed_deliverable_production(engine)
    _seed_milestone_acceptance(engine)


# ---------------------------------------------------------------------------
# Row counters and snapshot helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
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


def _snapshot_row(
    engine: Engine, table: str, key_column: str, key: str
) -> dict:
    """Return a dict-shaped snapshot of one row keyed by its PK.

    Used by the byte-equivalence assertions to capture row state
    before the Completion write and compare it after.
    """
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    f"SELECT * FROM {table} WHERE {key_column} = :k"
                ),
                {"k": key},
            ).mappings().one()
        )


def _snapshot_relationship_row(engine: Engine, relationship_id: str) -> dict:
    """Return a dict-shaped snapshot of one ``Relationships`` row."""
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT * FROM Relationships "
                    "WHERE relationship_id = :rid"
                ),
                {"rid": relationship_id},
            ).mappings().one()
        )


def _completion_columns(engine: Engine) -> list[str]:
    """Return the live ``Completion_Records`` column list.

    The list is read from SQLite's ``PRAGMA table_info`` so the test
    asserts against the *actual* persisted schema rather than a copy
    pasted from the source module. A regression that adds a column
    matching a prohibited observed-outcome prefix would surface
    here.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("PRAGMA table_info(Completion_Records)")
        ).mappings().all()
    return [row["name"] for row in rows]


def _create_permitted_completion(
    completion_engine: Engine,
    completion_service: CompletionService,
    *,
    outcome: str = "Completed",
    rationale: str = "Phase 1 work completed; success criteria met.",
    target_plan_revision_id: str = _APPROVED_PLAN_REVISION_ID,
    source_milestone_acceptance_ids=(),
    correlation_id: Optional[str] = None,
    request_attributes=None,
) -> CreateCompletionResult:
    """Drive a permitted ``create_completion`` call against the
    standard happy-path fixture.

    Encapsulated as a helper so the conflict, immutability, and
    byte-equivalence tests can share the same setup.
    """
    with completion_engine.begin() as conn:
        return completion_service.create_completion(
            conn,
            target_plan_revision_id=target_plan_revision_id,
            outcome=outcome,  # type: ignore[arg-type]
            rationale=rationale,
            source_milestone_acceptance_ids=source_milestone_acceptance_ids,
            completing_party_id=_COMPLETING_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=completion_engine,
            correlation_id=correlation_id,
            request_attributes=request_attributes,
        )


# ===========================================================================
# Happy-path baseline — confirms wiring and the AD-WS-26 Relationship
# cardinality contract (exactly one ``Addresses`` Relationship to the
# target Approved Plan Revision with ``semantic_role IS NULL``).
# ===========================================================================


def test_create_completion_permits_with_one_addresses(
    completion_engine: Engine,
    authorization_service: AuthorizationService,
    completion_service: CompletionService,
) -> None:
    """A permitted Completion write inserts exactly one
    ``Completion_Records`` row, exactly one ``Addresses``
    ``Relationships`` row to the target Approved Plan Revision, and
    exactly one consequential audit row inside one transaction per
    Requirements 29.1, 29.2, 29.6, AD-WS-26.

    The happy-path baseline anchors every subsequent rejection test:
    the same fixture, with one input varied to drive a single
    rejection branch, must persist nothing.
    """
    _seed_happy_path(completion_engine, authorization_service)

    result = _create_permitted_completion(
        completion_engine,
        completion_service,
        correlation_id="corr-permit",
    )

    assert isinstance(result, CreateCompletionResult)
    assert _CANONICAL_UUID7.match(result.completion_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.target_plan_revision_id == _APPROVED_PLAN_REVISION_ID
    assert result.target_activity_plan_id == _ACTIVITY_PLAN_ID
    assert result.target_project_id == _PROJECT_ID
    assert result.outcome == "Completed"
    assert result.correlation_id == "corr-permit"

    # Exactly one Completion Record persisted.
    assert _count(completion_engine, "Completion_Records") == 1

    # Exactly one consequential audit row participates in the same
    # transaction (Requirement 29.6).
    assert _count_consequential_audit_rows(
        completion_engine, "create.completion"
    ) == 1

    # Exactly one ``Addresses`` Relationship row sourced from the
    # Completion Record per AD-WS-26 (``semantic_role IS NULL``).
    with completion_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT relationship_id, source_kind, source_id,
                       source_revision_id, target_kind, target_id,
                       target_revision_id, semantic_role
                FROM Relationships
                WHERE relationship_type = 'Addresses'
                  AND source_id = :sid
                  AND source_kind = 'completion_record'
                """
            ),
            {"sid": result.completion_id},
        ).mappings().one()
    assert row["relationship_id"] == result.addresses_relationship_id
    assert row["source_kind"] == "completion_record"
    assert row["source_id"] == result.completion_id
    assert row["source_revision_id"] is None
    assert row["target_kind"] == "plan_revision"
    assert row["target_id"] == _APPROVED_PLAN_REVISION_ID
    assert row["target_revision_id"] is None
    assert row["semantic_role"] is None


# ===========================================================================
# Requirement 29.4 — target Plan Revision outcomes.
# ===========================================================================


class TestPlanRevisionResolutionOutcomes:
    """Unresolvable, ``draft``, and ``approved`` Plan Revisions
    exercise three distinct branches of Requirement 29.4. The
    rejections run before authorization evaluation so the deny path
    never leaks the Plan Revision's lifecycle state.
    """

    def test_unresolvable_plan_revision_raises_not_resolvable_error(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """An identifier that does not resolve raises
        :class:`CompletionPlanRevisionNotResolvableError`.

        The check runs before authorization evaluation so the deny
        path never leaks Plan Revision existence to an
        unauthorized caller. ``Completion_Records`` remains empty.
        """
        _seed_required_parties(completion_engine)
        _assign_complete_role(authorization_service, completion_engine)

        with pytest.raises(
            CompletionPlanRevisionNotResolvableError
        ) as exc_info:
            with completion_engine.begin() as conn:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="x",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _UNRESOLVABLE_PLAN_REVISION_ID
        )
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_not_resolvable"
        )
        assert _count(completion_engine, "Completion_Records") == 0

    def test_draft_plan_revision_raises_not_approved_error(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A Plan Revision whose ``lifecycle_state`` is ``'draft'``
        raises :class:`CompletionPlanRevisionNotApprovedError`
        carrying the observed lifecycle state verbatim.

        Requirement 29.4: the target Plan Revision must be
        ``'approved'`` at the recorded time. ``Completion_Records``
        remains empty.
        """
        _seed_required_parties(completion_engine)
        _assign_complete_role(authorization_service, completion_engine)
        _seed_project(completion_engine)
        _seed_activity_plan(completion_engine)
        _seed_plan_revision(
            completion_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with pytest.raises(
            CompletionPlanRevisionNotApprovedError
        ) as exc_info:
            with completion_engine.begin() as conn:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="x",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _DRAFT_PLAN_REVISION_ID
        )
        assert exc_info.value.observed_lifecycle_state == "draft"
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_not_approved"
        )
        assert _count(completion_engine, "Completion_Records") == 0

    def test_approved_plan_revision_is_accepted(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The ``approved`` branch is the happy path; the call
        succeeds and the result carries the requested identifier.

        The symmetric assertion to the ``draft`` rejection: the
        same seeded scenario with ``lifecycle_state='approved'``
        and an accepted Milestone succeeds.
        """
        _seed_happy_path(completion_engine, authorization_service)

        result = _create_permitted_completion(
            completion_engine, completion_service
        )

        assert result.target_plan_revision_id == _APPROVED_PLAN_REVISION_ID
        assert _count(completion_engine, "Completion_Records") == 1


# ===========================================================================
# Requirement 29.1 / 29.4 — accepted-Milestone existence check.
# ===========================================================================


class TestAcceptedMilestoneExistence:
    """Per Requirement 29.1 / 29.4 the accepted-Milestone existence
    check requires at least one ``Accept``-outcome Milestone
    Acceptance Record whose source Deliverable Production Record
    traces back through the requested Plan Revision. When the query
    returns zero rows, the request is rejected with no Completion
    Record persisted.

    Two distinct failure modes are pinned: (1) zero accepted
    Milestones with an empty ``source_milestone_acceptance_ids``
    list raises :class:`CompletionNoAcceptedMilestonesError`; (2) a
    supplied list with at least one entry that does not resolve to
    an ``Accept``-outcome row raises
    :class:`CompletionSourceMilestoneAcceptanceNotResolvableError`.
    """

    def test_zero_accepted_milestones_empty_list_rejected(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """An approved Plan Revision with zero accepted Milestone
        Acceptance Records is rejected with
        :class:`CompletionNoAcceptedMilestonesError`.

        The seeded scenario installs an approved Plan Revision
        (``_LONELY_PLAN_REVISION_ID``) with no Work Assignment,
        Production Record, or Milestone Acceptance — the existence
        query therefore returns zero rows. Calling with an empty
        ``source_milestone_acceptance_ids`` list exercises
        Requirement 29.1's zero-rows rejection branch.
        """
        _seed_required_parties(completion_engine)
        _assign_complete_role(authorization_service, completion_engine)
        _seed_project(completion_engine)
        _seed_activity_plan(completion_engine)
        _seed_plan_revision(
            completion_engine, plan_revision_id=_LONELY_PLAN_REVISION_ID
        )

        with pytest.raises(
            CompletionNoAcceptedMilestonesError
        ) as exc_info:
            with completion_engine.begin() as conn:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_LONELY_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="Rolling up zero milestones.",
                    source_milestone_acceptance_ids=(),
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _LONELY_PLAN_REVISION_ID
        )
        assert exc_info.value.failed_constraint == (
            "no_accepted_milestones_for_target_plan_revision"
        )
        assert _count(completion_engine, "Completion_Records") == 0

    def test_reject_outcome_milestone_does_not_satisfy_existence_check(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A Milestone Acceptance Record with ``outcome = 'Reject'``
        does not count toward the existence check.

        The covering SQL in
        :meth:`CompletionService.create_completion` filters
        explicitly on ``outcome = 'Accept'``; a single ``Reject``
        Acceptance against the only Production Record for the
        target Plan Revision therefore yields zero matching rows
        and the request is rejected.

        Seeding directly bypasses the
        ``UNIQUE(source_deliverable_production_id)`` constraint
        that would block a second Acceptance through the service;
        the test installs only the ``Reject``-outcome row so the
        accepted-count is zero.
        """
        _seed_required_parties(completion_engine)
        _assign_complete_role(authorization_service, completion_engine)
        _seed_project(completion_engine)
        _seed_activity_plan(completion_engine)
        _seed_plan_revision(
            completion_engine,
            plan_revision_id=_APPROVED_PLAN_REVISION_ID,
        )
        _seed_work_assignment(completion_engine)
        _seed_deliverable(completion_engine)
        _seed_deliverable_expectation(completion_engine)
        _seed_deliverable_production(completion_engine)
        _seed_milestone_acceptance(
            completion_engine,
            milestone_acceptance_id=_REJECT_ACCEPTANCE_ID,
            outcome="Reject",
        )

        with pytest.raises(CompletionNoAcceptedMilestonesError):
            with completion_engine.begin() as conn:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="Should reject — no accepted milestone.",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert _count(completion_engine, "Completion_Records") == 0

    def test_supplied_non_accept_identifier_rejected(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A ``source_milestone_acceptance_ids`` list naming an
        identifier that does not resolve to an ``Accept``-outcome
        row for the target Plan Revision raises
        :class:`CompletionSourceMilestoneAcceptanceNotResolvableError`.

        Requirement 29.4 requires every supplied source Milestone
        Acceptance Identity to appear in the accepted-Milestone
        existence-query result set. The seeded scenario has one
        ``Accept``-outcome Acceptance (``_ACCEPT_ACCEPTANCE_ID``);
        the caller names a different identifier that does not
        appear in that set. The offending identifier is reported
        on the exception so the route layer can identify it.
        """
        _seed_happy_path(completion_engine, authorization_service)
        bogus_id = "00000000-0000-7000-8000-0000bad00001"

        with pytest.raises(
            CompletionSourceMilestoneAcceptanceNotResolvableError
        ) as exc_info:
            with completion_engine.begin() as conn:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="Citing one bad source milestone.",
                    source_milestone_acceptance_ids=(bogus_id,),
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _APPROVED_PLAN_REVISION_ID
        )
        assert exc_info.value.offending_milestone_acceptance_id == (
            bogus_id
        )
        assert exc_info.value.failed_constraint == (
            "source_milestone_acceptance_not_resolvable"
        )
        assert _count(completion_engine, "Completion_Records") == 0

    def test_supplied_list_with_mix_of_accept_and_non_accept_rejected(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A list mixing a resolvable ``Accept``-outcome identifier
        with one that does not resolve is rejected on the first
        non-matching entry.

        Pins the deterministic "first offending entry reported"
        behavior promised by the service docstring; if a future
        implementation reversed iteration order or collected all
        offenders, this test would catch the change.
        """
        _seed_happy_path(completion_engine, authorization_service)
        bogus_id = "00000000-0000-7000-8000-0000bad00002"

        with pytest.raises(
            CompletionSourceMilestoneAcceptanceNotResolvableError
        ) as exc_info:
            with completion_engine.begin() as conn:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="Mixed list.",
                    source_milestone_acceptance_ids=(
                        _ACCEPT_ACCEPTANCE_ID,
                        bogus_id,
                    ),
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.offending_milestone_acceptance_id == bogus_id
        assert _count(completion_engine, "Completion_Records") == 0

    def test_supplied_list_with_all_accept_entries_accepted(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A list naming only resolvable ``Accept``-outcome
        identifiers is accepted and persisted byte-equivalent on
        the row.

        Pins the positive branch of Requirement 29.4's optional
        source-Milestone resolution: when every entry matches the
        existence query result set, the Completion is permitted
        and the list is persisted (JSON-encoded) on the row.
        """
        _seed_happy_path(completion_engine, authorization_service)

        result = _create_permitted_completion(
            completion_engine,
            completion_service,
            source_milestone_acceptance_ids=(_ACCEPT_ACCEPTANCE_ID,),
        )

        assert result.source_milestone_acceptance_ids == (
            _ACCEPT_ACCEPTANCE_ID,
        )
        assert _count(completion_engine, "Completion_Records") == 1


# ===========================================================================
# Requirement 29.3 — duplicate-Completion rejection (UNIQUE constraint).
# ===========================================================================


class TestDuplicateCompletionRejection:
    """Per Requirement 29.3, at most one Completion Record may exist
    per target Approved Plan Revision.

    The schema-level ``UNIQUE(target_plan_revision_id)`` constraint
    is the source of truth; the application-level pre-check surfaces
    a structured :class:`CompletionConflictError` carrying the
    existing Completion Identity (subject to AD-WS-9 view-authority
    gating). Both layers are pinned here so a regression at either
    layer is visible.
    """

    def test_second_completion_against_same_plan_raises_conflict(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A second Completion attempt against the same target Plan
        Revision raises :class:`CompletionConflictError`.

        The caller holds ``complete`` authority but not ``view``,
        so the conflict response carries
        ``existing_completion_id = None`` per the AD-WS-9
        view-authority gate.
        """
        _seed_happy_path(completion_engine, authorization_service)

        first = _create_permitted_completion(
            completion_engine,
            completion_service,
            correlation_id="corr-first",
        )
        assert first.outcome == "Completed"

        # Snapshot the row body so a successful second INSERT or any
        # in-place mutation would be caught.
        before = _snapshot_row(
            completion_engine,
            "Completion_Records",
            "completion_id",
            first.completion_id,
        )

        with pytest.raises(CompletionConflictError) as exc_info:
            _create_permitted_completion(
                completion_engine,
                completion_service,
                outcome="Completed_With_Reservation",
                rationale="Reverse the earlier completion.",
                correlation_id="corr-second",
            )

        assert exc_info.value.failed_constraint == "completion_already_exists"
        assert exc_info.value.target_plan_revision_id == (
            _APPROVED_PLAN_REVISION_ID
        )
        # Caller lacks ``view`` authority — existing Identity hidden.
        assert exc_info.value.existing_completion_id is None

        # Exactly one Completion row exists — the original.
        assert _count(completion_engine, "Completion_Records") == 1
        assert _count_consequential_audit_rows(
            completion_engine, "create.completion"
        ) == 1

        # The original row is byte-equivalent (no mutation, no second
        # row).
        after = _snapshot_row(
            completion_engine,
            "Completion_Records",
            "completion_id",
            first.completion_id,
        )
        assert before == after

    def test_existing_id_visible_when_caller_holds_view(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """When the caller holds ``view`` authority on the existing
        Completion, the conflict response carries the existing
        Completion Identity per AD-WS-9 / Slice 3 Requirement 30.4.
        """
        _seed_happy_path(completion_engine, authorization_service)
        # Grant the recording Party an additional ``view`` authority
        # so the AD-WS-9 conflict-visibility gate permits.
        view_request = AssignRoleRequest(
            party_id=_COMPLETING_PARTY_ID,
            role_name="completion_viewer",
            scope=_SCOPE,
            authorities_granted=("view",),
            effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            effective_end=None,
            assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
        )
        with completion_engine.begin() as conn:
            authorization_service.assign_role(conn, view_request)

        first = _create_permitted_completion(
            completion_engine, completion_service
        )

        with pytest.raises(CompletionConflictError) as exc_info:
            _create_permitted_completion(
                completion_engine,
                completion_service,
                outcome="Completed_With_Reservation",
                rationale="Second attempt.",
            )

        assert exc_info.value.existing_completion_id == first.completion_id

    def test_db_layer_unique_constraint_rejects_bypassed_insert(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """Bypassing the service and inserting a second Completion
        Record directly fails on the schema-level
        ``UNIQUE(target_plan_revision_id)`` constraint per
        Requirement 29.3.

        The pre-check is a convenience that surfaces a structured
        error in place of a raw :class:`IntegrityError`; the
        authoritative invariant lives in the schema. This test
        pins the schema layer so a regression that drops or
        weakens the UNIQUE constraint is visible even if the
        service code somehow forgets the pre-check.
        """
        _seed_happy_path(completion_engine, authorization_service)
        first = _create_permitted_completion(
            completion_engine, completion_service
        )

        # A direct second INSERT — same ``target_plan_revision_id``,
        # different Completion Identity — must fail on the schema
        # UNIQUE constraint rather than silently succeed.
        second_id = "00000000-0000-7000-8000-0000beef0002"
        with completion_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO Completion_Records (
                            completion_id,
                            target_plan_revision_id,
                            target_activity_plan_id,
                            target_project_id,
                            outcome, rationale,
                            source_milestone_acceptance_ids_json,
                            completing_party_id,
                            authority_basis_type, authority_basis_id,
                            applicable_scope, recorded_at
                        ) VALUES (
                            :cid, :prev, :apid, :proj,
                            'Completed', 'Direct insert.',
                            '[]', :party, 'role-grant-id', :abid,
                            :scope, :ts
                        )
                        """
                    ),
                    {
                        "cid": second_id,
                        "prev": _APPROVED_PLAN_REVISION_ID,
                        "apid": _ACTIVITY_PLAN_ID,
                        "proj": _PROJECT_ID,
                        "party": _COMPLETING_PARTY_ID,
                        "abid": str(_AUTHORITY_BASIS_ID),
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        # The original Completion survives; no second row.
        assert _count(completion_engine, "Completion_Records") == 1
        with completion_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT completion_id FROM Completion_Records "
                    "WHERE completion_id = :id"
                ),
                {"id": first.completion_id},
            ).scalar_one()
        assert still_present == first.completion_id


# ===========================================================================
# Requirement 29.2 / 29.4 — ``outcome`` enumeration.
# ===========================================================================


class TestOutcomeEnumeration:
    """Per Requirement 29.2 the ``outcome`` is drawn from the
    enumerated set ``{Completed, Completed_With_Reservation}``; per
    Requirement 29.4 a value outside the set is rejected with no
    Completion Record persisted.

    Validation runs in the static validator before any database
    read so a malformed request never touches the planning reader
    or the authorization service.
    """

    def test_outcome_constant_matches_requirement(self) -> None:
        """The :data:`OUTCOME_VALUES` constant pins exactly the
        Requirement 29.2 enumeration. A regression that adds or
        removes a value would be caught even before the service is
        consulted.
        """
        assert OUTCOME_VALUES == ("Completed", "Completed_With_Reservation")

    @pytest.mark.parametrize(
        "valid_outcome", ["Completed", "Completed_With_Reservation"]
    )
    def test_each_valid_outcome_accepted(
        self,
        valid_outcome: str,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """Each value in :data:`OUTCOME_VALUES` is accepted and
        persisted byte-equivalent on the row.

        Per Requirement 29.2 both ``Completed`` and
        ``Completed_With_Reservation`` are admissible outcomes —
        recording reserved completion is a first-class action.
        """
        _seed_happy_path(completion_engine, authorization_service)

        result = _create_permitted_completion(
            completion_engine,
            completion_service,
            outcome=valid_outcome,
            rationale="Outcome boundary test.",
        )

        assert result.outcome == valid_outcome
        with completion_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT outcome FROM Completion_Records "
                    "WHERE completion_id = :id"
                ),
                {"id": result.completion_id},
            ).scalar_one()
        assert stored == valid_outcome

    @pytest.mark.parametrize(
        "invalid_outcome",
        [
            "completed",
            "COMPLETED",
            "Accept",
            "Done",
            "Completed With Reservation",
            " Completed ",
        ],
    )
    def test_outcome_out_of_set_rejected(
        self,
        invalid_outcome: str,
        completion_engine: Engine,
        completion_service: CompletionService,
    ) -> None:
        """Any value outside :data:`OUTCOME_VALUES` raises
        :class:`CompletionValidationError` with
        ``failed_constraint='outcome_out_of_set'``.

        Case differences (``completed``, ``COMPLETED``),
        near-synonyms (``Accept``, ``Done``), space-substituted
        variants (``Completed With Reservation``), and surrounding
        whitespace (`` Completed ``) are each rejected — the
        enumeration is exact.
        """
        with completion_engine.begin() as conn:
            with pytest.raises(CompletionValidationError) as exc_info:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome=invalid_outcome,  # type: ignore[arg-type]
                    rationale="Boundary.",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.failed_constraint == "outcome_out_of_set"
        assert _count(completion_engine, "Completion_Records") == 0

    @pytest.mark.parametrize("missing_outcome", [None, ""])
    def test_missing_or_empty_outcome_rejected(
        self,
        missing_outcome,
        completion_engine: Engine,
        completion_service: CompletionService,
    ) -> None:
        """A missing or empty ``outcome`` raises with
        ``failed_constraint='outcome_missing'``.

        Distinct from the out-of-set rejection so the route layer
        can pinpoint whether the field was omitted or carried a
        wrong value.
        """
        with completion_engine.begin() as conn:
            with pytest.raises(CompletionValidationError) as exc_info:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome=missing_outcome,  # type: ignore[arg-type]
                    rationale="Boundary.",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.failed_constraint == "outcome_missing"
        assert _count(completion_engine, "Completion_Records") == 0


# ===========================================================================
# Requirement 29.2 / 29.4 — rationale length boundaries (1..4000).
# ===========================================================================


class TestRationaleLengthBoundaries:
    """Per Requirement 29.2 the rationale must be 1..4000 characters
    and is required.

    The ``Completion_Records.rationale`` CHECK constraint
    ``length(rationale) BETWEEN 1 AND 4000`` enforces the same range
    at the database layer; the application validator surfaces a
    precise ``failed_constraint`` for the HTTP layer.
    """

    def test_one_char_rationale_accepted(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A 1-char rationale sits at the lower boundary and is
        accepted and persisted byte-equivalent.
        """
        _seed_happy_path(completion_engine, authorization_service)

        result = _create_permitted_completion(
            completion_engine,
            completion_service,
            rationale="x",
        )

        assert result.rationale == "x"
        with completion_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT rationale FROM Completion_Records "
                    "WHERE completion_id = :id"
                ),
                {"id": result.completion_id},
            ).scalar_one()
        assert stored == "x"

    def test_four_thousand_char_rationale_accepted(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A 4000-char rationale sits at the upper boundary."""
        _seed_happy_path(completion_engine, authorization_service)

        rationale = "x" * 4_000
        result = _create_permitted_completion(
            completion_engine,
            completion_service,
            rationale=rationale,
        )

        assert result.rationale == rationale
        assert len(result.rationale) == 4_000
        assert _count(completion_engine, "Completion_Records") == 1

    def test_zero_length_rationale_rejected(
        self,
        completion_engine: Engine,
        completion_service: CompletionService,
    ) -> None:
        """``len(rationale) == 0`` raises
        :class:`CompletionValidationError` with
        ``failed_constraint='rationale_too_short'`` and persists
        nothing.
        """
        with completion_engine.begin() as conn:
            with pytest.raises(CompletionValidationError) as exc_info:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.failed_constraint == "rationale_too_short"
        assert _count(completion_engine, "Completion_Records") == 0

    def test_four_thousand_one_char_rationale_rejected(
        self,
        completion_engine: Engine,
        completion_service: CompletionService,
    ) -> None:
        """``len(rationale) == 4001`` raises
        :class:`CompletionValidationError` with
        ``failed_constraint='rationale_too_long'`` and persists
        nothing.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        with completion_engine.begin() as conn:
            with pytest.raises(CompletionValidationError) as exc_info:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="x" * 4_001,
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                )

        assert exc_info.value.failed_constraint == "rationale_too_long"
        assert _count(completion_engine, "Completion_Records") == 0


# ===========================================================================
# Requirement 29.7 / AD-WS-27 — immutability of UPDATE / DELETE.
# ===========================================================================


class TestCompletionImmutability:
    """Per Requirement 29.7 / AD-WS-27, after a Completion Record is
    finalized the row and its ``Addresses`` Relationship row reject
    every UPDATE / DELETE attempt via the append-only triggers
    installed by task 1.2 (Slice 3 Execution_Service tables) and the
    Slice 1 ``Relationships`` triggers.

    The triggers raise ``RAISE(ABORT, ...)`` which SQLAlchemy
    surfaces as :class:`sqlalchemy.exc.IntegrityError`.
    """

    def test_update_on_record_rejected_after_persistence(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """An UPDATE on ``Completion_Records`` after commit raises
        :class:`IntegrityError` and leaves the row unchanged.
        """
        _seed_happy_path(completion_engine, authorization_service)
        result = _create_permitted_completion(
            completion_engine, completion_service
        )

        before = _snapshot_row(
            completion_engine,
            "Completion_Records",
            "completion_id",
            result.completion_id,
        )

        with completion_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Completion_Records "
                        "SET rationale = 'tampered' "
                        "WHERE completion_id = :id"
                    ),
                    {"id": result.completion_id},
                )

        after = _snapshot_row(
            completion_engine,
            "Completion_Records",
            "completion_id",
            result.completion_id,
        )
        assert after == before

    def test_delete_on_record_rejected_after_persistence(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A DELETE on ``Completion_Records`` after commit raises
        :class:`IntegrityError` and the row remains in place.
        """
        _seed_happy_path(completion_engine, authorization_service)
        result = _create_permitted_completion(
            completion_engine, completion_service
        )

        with completion_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "DELETE FROM Completion_Records "
                        "WHERE completion_id = :id"
                    ),
                    {"id": result.completion_id},
                )

        assert _count(completion_engine, "Completion_Records") == 1
        with completion_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT completion_id FROM Completion_Records "
                    "WHERE completion_id = :id"
                ),
                {"id": result.completion_id},
            ).scalar_one()
        assert still_present == result.completion_id

    def test_update_on_addresses_relationship_rejected(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """An UPDATE on the ``Addresses`` ``Relationships`` row
        produced alongside a Completion Record raises
        :class:`IntegrityError`.

        The Slice 1 ``Relationships`` table is one of the AD-WS-4
        immutable tables; its UPDATE rejection trigger fires
        regardless of which Slice 3 source kind the row carries.
        """
        _seed_happy_path(completion_engine, authorization_service)
        result = _create_permitted_completion(
            completion_engine, completion_service
        )
        before = _snapshot_relationship_row(
            completion_engine, result.addresses_relationship_id
        )

        with completion_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Relationships "
                        "SET semantic_role = 'tampered' "
                        "WHERE relationship_id = :id"
                    ),
                    {"id": result.addresses_relationship_id},
                )

        after = _snapshot_relationship_row(
            completion_engine, result.addresses_relationship_id
        )
        assert after == before

    def test_delete_on_addresses_relationship_rejected(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A DELETE on the ``Addresses`` ``Relationships`` row
        produced alongside a Completion Record raises
        :class:`IntegrityError` and the row survives.
        """
        _seed_happy_path(completion_engine, authorization_service)
        result = _create_permitted_completion(
            completion_engine, completion_service
        )

        with completion_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "DELETE FROM Relationships "
                        "WHERE relationship_id = :id"
                    ),
                    {"id": result.addresses_relationship_id},
                )

        with completion_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT relationship_id FROM Relationships "
                    "WHERE relationship_id = :id"
                ),
                {"id": result.addresses_relationship_id},
            ).scalar_one()
        assert still_present == result.addresses_relationship_id


# ===========================================================================
# Requirement 29.7 / 29.8 / 40 §1 — Slice 1 + Slice 2 byte-equivalence.
#
# After a permitted Completion write, every row the service is
# explicitly forbidden to mutate must remain byte-equivalent to its
# pre-Completion state.
# ===========================================================================


class TestSlice1And2RowByteEquivalence:
    """Per Requirement 29.7 / 29.8 / Requirement 40 §1, the Completion
    write does not mutate any Slice 1 or Slice 2 row, nor the source
    Production Record, accepted Milestone Acceptance Record, or
    produced Deliverable Revision in Slice 3.

    Each row that is part of the seed graph is snapshotted before
    the Completion write and asserted byte-equivalent after.
    """

    def test_plan_revision_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The target Approved Plan Revision row remains
        byte-equivalent across the Completion write.

        Requirement 29.7 explicitly forbids modifying the target
        Plan Revision as a consequence of recording a Completion.
        The Plan Revision is owned by Slice 2; Requirement 40 §1
        forbids Slice 3 from mutating Slice 2 rows.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Plan_Revisions",
            "plan_revision_id",
            _APPROVED_PLAN_REVISION_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Plan_Revisions",
            "plan_revision_id",
            _APPROVED_PLAN_REVISION_ID,
        )
        assert after == before

    def test_activity_plan_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The target Activity Plan row remains byte-equivalent
        across the Completion write.

        The Activity Plan is owned by Slice 2; Requirement 29.7
        and Requirement 40 §1 forbid Slice 3 from mutating it.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Activity_Plans",
            "activity_plan_id",
            _ACTIVITY_PLAN_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Activity_Plans",
            "activity_plan_id",
            _ACTIVITY_PLAN_ID,
        )
        assert after == before

    def test_project_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The target Project row remains byte-equivalent across
        the Completion write.

        The Project is owned by Slice 2; Requirement 29.7 and
        Requirement 40 §1 forbid Slice 3 from mutating it.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine, "Projects", "project_id", _PROJECT_ID
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine, "Projects", "project_id", _PROJECT_ID
        )
        assert after == before

    def test_deliverable_expectation_revision_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The target Deliverable Expectation Revision row remains
        byte-equivalent across the Completion write.

        The Expectation Revision is owned by Slice 2; Requirement
        29.7 forbids Slice 3 from mutating it as a consequence of
        recording a Completion.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Deliverable_Expectation_Revisions",
            "deliverable_expectation_revision_id",
            _DELIVERABLE_EXPECTATION_REVISION_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Deliverable_Expectation_Revisions",
            "deliverable_expectation_revision_id",
            _DELIVERABLE_EXPECTATION_REVISION_ID,
        )
        assert after == before

    def test_milestone_acceptance_record_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The source Milestone Acceptance Record row remains
        byte-equivalent across the Completion write.

        Requirement 29.7 forbids modifying any rolled-up source
        Milestone Acceptance Record as a consequence of recording
        a Completion. Property 11 (Plan/Execution resource
        non-mutation) snapshots Slice 3 rows alongside Slice 1 /
        Slice 2 rows.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Milestone_Acceptance_Records",
            "milestone_acceptance_id",
            _ACCEPT_ACCEPTANCE_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Milestone_Acceptance_Records",
            "milestone_acceptance_id",
            _ACCEPT_ACCEPTANCE_ID,
        )
        assert after == before

    def test_deliverable_production_record_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The source Deliverable Production Record row remains
        byte-equivalent across the Completion write.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Deliverable_Production_Records",
            "deliverable_production_id",
            _DELIVERABLE_PRODUCTION_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Deliverable_Production_Records",
            "deliverable_production_id",
            _DELIVERABLE_PRODUCTION_ID,
        )
        assert after == before

    def test_produced_deliverable_revision_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The produced Deliverable Revision row remains
        byte-equivalent across the Completion write.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Deliverable_Revisions",
            "deliverable_revision_id",
            _DELIVERABLE_REVISION_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Deliverable_Revisions",
            "deliverable_revision_id",
            _DELIVERABLE_REVISION_ID,
        )
        assert after == before

    def test_work_assignment_record_byte_equivalent(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """The source Work Assignment Record row remains
        byte-equivalent across the Completion write.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine,
            "Work_Assignment_Records",
            "work_assignment_id",
            _WORK_ASSIGNMENT_ID,
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine,
            "Work_Assignment_Records",
            "work_assignment_id",
            _WORK_ASSIGNMENT_ID,
        )
        assert after == before

    @pytest.mark.parametrize(
        "party_id,party_label",
        [
            (_COMPLETING_PARTY_ID, "Completion Authority"),
            (_CONTRIBUTOR_PARTY_ID, "Contributor"),
            (_ASSIGNMENT_AUTHORITY_ID, "Assignment Authority"),
            (_ASSIGNING_AUTHORITY_ID, "Resource Steward"),
        ],
    )
    def test_party_rows_byte_equivalent(
        self,
        party_id: str,
        party_label: str,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """Every seeded ``Parties`` row remains byte-equivalent
        across the Completion write.

        The ``Parties`` table is owned by Slice 1; Requirement
        29.7 and Requirement 40 §1 forbid Slice 3 from mutating
        Slice 1 rows. Parameterized across all four seeded Parties
        to pin the invariant on each role independently.
        """
        _seed_happy_path(completion_engine, authorization_service)
        before = _snapshot_row(
            completion_engine, "Parties", "party_id", party_id
        )

        _create_permitted_completion(
            completion_engine, completion_service
        )

        after = _snapshot_row(
            completion_engine, "Parties", "party_id", party_id
        )
        assert after == before, (
            f"{party_label} Party row mutated by Completion write, "
            "violating Requirement 29.7 / 40 §1."
        )


# ===========================================================================
# Requirement 29.8 / 34.3 — Completion does not assert any observed Outcome.
# ===========================================================================


class TestCompletionDoesNotAssertObservedOutcome:
    """Per Requirement 29.8 / 34.3, a Completion Record records the
    *completion of planned work*; it does NOT assert, imply, or alias
    any observed Outcome, Measurement Definition, Measurement
    Record, Outcome Review, success-condition assessment, or
    attribution-evidence reference.

    Two complementary assertions pin the invariant:

    1. **Schema-level** — the persisted ``Completion_Records``
       columns are exactly the ones the design declares. None of
       the prohibited observed-outcome prefixes appears as a column
       name. A regression that adds an ``observed_outcome_id``,
       ``measurement_record_id``, ``outcome_review_id``,
       ``attribution_evidence_id``, or
       ``success_condition_assessment_id`` column to the schema
       would surface here.
    2. **Request-level** — when the route layer forwards a raw
       request body via ``request_attributes`` that carries any
       observed-outcome key, the request is rejected with
       ``failed_constraint='prohibited_attribute'`` and the
       offending keys are listed on
       :attr:`CompletionValidationError.prohibited_keys`.
    """

    def test_completion_record_schema_carries_no_observed_outcome_columns(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """No column on the persisted ``Completion_Records`` row
        carries an observed-outcome attribute.

        Reads the live column list from
        :func:`_completion_columns` (which calls
        ``PRAGMA table_info``) so the assertion runs against the
        actual schema in use. The forbidden prefix list mirrors
        :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES` from
        :mod:`walking_slice.execution._helpers` — translated to the
        snake_case form SQL columns actually use.

        The expected column list is also pinned positively against
        Requirement 29.2 (the explicit set of attributes a
        Completion Record records: target Plan Revision, target
        Activity Plan, target Project, outcome, rationale, source
        Milestone Acceptance Identities, completing Party,
        authority basis, applicable scope, recorded time).
        """
        _seed_happy_path(completion_engine, authorization_service)
        # Drive at least one row through so we know the schema is
        # the one the service actually writes against.
        _create_permitted_completion(
            completion_engine, completion_service
        )

        columns = _completion_columns(completion_engine)

        # Positive check: every Requirement 29.2 attribute is
        # present on the persisted row.
        required_columns = {
            "completion_id",
            "target_plan_revision_id",
            "target_activity_plan_id",
            "target_project_id",
            "outcome",
            "rationale",
            "source_milestone_acceptance_ids_json",
            "completing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        }
        assert required_columns <= set(columns), (
            "Completion_Records is missing a Requirement 29.2 "
            f"attribute; columns={sorted(columns)}."
        )

        # Negative check: no column starts with an observed-outcome
        # prefix. The prefix list mirrors
        # OBSERVED_OUTCOME_PROHIBITED_PREFIXES from
        # walking_slice.execution._helpers, normalized to the
        # snake_case form SQLite columns use.
        prohibited_substrings = (
            "observed",
            "measurement",
            "outcome_review",
            "outcome-review",
            "attribution_evidence",
            "attribution-evidence",
            "success_condition_assessment",
            "success-condition-assessment",
        )
        for column in columns:
            lower = column.lower()
            for prohibited in prohibited_substrings:
                assert prohibited not in lower, (
                    f"Completion_Records column {column!r} matches "
                    f"prohibited observed-outcome substring "
                    f"{prohibited!r}; Requirement 29.8 / 34.3 "
                    "forbid observed-outcome attributes on the "
                    "Completion Record."
                )

    def test_persisted_row_carries_only_requirement_29_2_columns(
        self,
        completion_engine: Engine,
        authorization_service: AuthorizationService,
        completion_service: CompletionService,
    ) -> None:
        """A permitted Completion write persists exactly the
        Requirement 29.2 attributes; no row-level value alludes to
        an observed Outcome.

        Reads the actual row back through ``SELECT *`` and pins
        every column name and its rough shape against Requirement
        29.2 so a future schema change that adds an
        ``observed_outcome_value`` or
        ``measurement_record_id`` column is caught.
        """
        _seed_happy_path(completion_engine, authorization_service)
        result = _create_permitted_completion(
            completion_engine,
            completion_service,
            source_milestone_acceptance_ids=(_ACCEPT_ACCEPTANCE_ID,),
        )

        with completion_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT * FROM Completion_Records "
                    "WHERE completion_id = :id"
                ),
                {"id": result.completion_id},
            ).mappings().one()

        # The persisted column set is exactly Requirement 29.2.
        # The list is closed (==, not <=) so a future addition of
        # an observed-outcome column would break the assertion.
        assert set(row.keys()) == {
            "completion_id",
            "target_plan_revision_id",
            "target_activity_plan_id",
            "target_project_id",
            "outcome",
            "rationale",
            "source_milestone_acceptance_ids_json",
            "completing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        }

    @pytest.mark.parametrize(
        "prohibited_key",
        [
            # ``observed-`` prefix
            "observed_outcome_value",
            "observed-outcome-value",
            # ``measurement-`` prefix
            "measurement_record_id",
            # ``outcome-review-`` prefix
            "outcome_review_id",
            # ``attribution-evidence-`` prefix
            "attribution_evidence_ref",
            # ``success-condition-assessment-`` prefix — the key must
            # carry something after the prefix because the prefix
            # itself ends in a hyphen / underscore separator.
            "success_condition_assessment_id",
        ],
    )
    def test_observed_outcome_attribute_on_request_rejected(
        self,
        prohibited_key: str,
        completion_engine: Engine,
        completion_service: CompletionService,
    ) -> None:
        """A ``request_attributes`` mapping carrying an
        observed-outcome key raises
        :class:`CompletionValidationError` with
        ``failed_constraint='prohibited_attribute'`` and lists the
        offending key.

        Requirement 34.3 forbids a Completion request from
        carrying any observed-outcome attribute; Requirement 34.5
        requires the response to identify every prohibited key.
        The screen runs before any database read so the rejection
        leaves ``Completion_Records`` empty.

        Both hyphen- and underscore-separated forms are tested
        because the helper normalizes hyphens and underscores
        before matching the prefix list.
        """
        request_attributes = {
            "target_plan_revision_id": _APPROVED_PLAN_REVISION_ID,
            "outcome": "Completed",
            "rationale": "Should be rejected.",
            prohibited_key: "should-not-be-allowed",
        }

        with completion_engine.begin() as conn:
            with pytest.raises(CompletionValidationError) as exc_info:
                completion_service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale="Should be rejected.",
                    completing_party_id=_COMPLETING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=completion_engine,
                    request_attributes=request_attributes,
                )

        assert exc_info.value.failed_constraint == "prohibited_attribute"
        # The offending key is reported verbatim so the route layer
        # can echo it in the response body per Requirement 34.5.
        assert prohibited_key in exc_info.value.prohibited_keys
        assert _count(completion_engine, "Completion_Records") == 0
