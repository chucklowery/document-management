"""Unit tests for :mod:`walking_slice.execution.milestone_acceptances` (task 10.2).

Pins the contract established in task 10.1, design
§"Execution_Service.MilestoneAcceptances", AD-WS-9
(separate-transaction Denial Record), AD-WS-26 (Relationship-Type /
semantic-role table — the Milestone Acceptance Record carries exactly
one ``Addresses`` row to the produced Deliverable Revision with
``semantic_role IS NULL``), AD-WS-27 (append-only Slice 3 tables),
AD-WS-28 (additive ``resource_kind`` values), and Requirements 28.3,
28.4, 28.7, 28.8, 32.8:

- **28.3 / 28.4 — duplicate-Milestone-Acceptance rejection.** A
  second Milestone Acceptance against the same source Deliverable
  Production Record is rejected with
  :class:`MilestoneAcceptanceConflictError` carrying
  ``failed_constraint = 'milestone_acceptance_already_recorded'``.
  The schema-level ``UNIQUE(source_deliverable_production_id)``
  constraint is the source of truth; the application-level pre-check
  surfaces a structured error. Both layers are exercised:

  * **Pre-check layer.** When a Milestone Acceptance already exists,
    the service raises :class:`MilestoneAcceptanceConflictError`
    before the second INSERT is even attempted. The existing
    Milestone Acceptance Identity is populated on the exception only
    when the caller holds ``view`` authority on it (AD-WS-9 / Slice
    3 Requirement 30.4); otherwise the exception carries ``None``.
  * **Database layer.** Bypassing the service (an INSERT directly
    against ``Milestone_Acceptance_Records``) fails with
    :class:`sqlalchemy.exc.IntegrityError` raised by the UNIQUE
    constraint.

- **28.4 — outcome / rationale / authority-basis validation.**
  Every Requirement 28.2 input boundary is pinned through the
  service surface:

  * ``outcome`` must be drawn from the enumerated set
    ``{Accept, Reject}``. Each valid value is accepted; any other
    string raises :class:`MilestoneAcceptanceValidationError` with
    ``failed_constraint='outcome_out_of_set'`` and an absent /
    empty value raises with ``'outcome_missing'``.
  * ``rationale`` must be 1..4000 characters. The 1-char and
    4000-char boundary values are accepted and persisted
    byte-equivalent; ``0`` and ``4001`` raise
    :class:`MilestoneAcceptanceValidationError` with
    ``'rationale_too_short'`` and ``'rationale_too_long'``
    respectively.
  * ``authority_basis.type`` must be drawn from the AD-WS-10
    enumeration ``{role-grant-id, scope-id, delegation-chain-id}``.
    Each valid value is accepted and persisted; any other value
    raises with ``'authority_basis_type_out_of_set'``.

- **28.7 / AD-WS-27 — immutability.**
  ``Milestone_Acceptance_Records`` rejects UPDATE and DELETE via the
  AD-WS-27 append-only triggers; the ``Addresses`` ``Relationships``
  row inserted alongside is governed by the Slice 1 ``Relationships``
  append-only triggers (every Slice 1 immutable table rejects UPDATE
  and DELETE per AD-WS-4). Both surfaces raise
  :class:`sqlalchemy.exc.IntegrityError`.

- **28.8 — Slice 1 / Slice 2 row byte-equivalence.** After a
  permitted Milestone Acceptance write, every dependency row touched
  in the seed graph is byte-equivalent to its pre-Acceptance state.
  The set captured here covers the rows the service is explicitly
  forbidden to mutate (Requirement 28.8 / Requirement 40 §1 /
  Property 11): the source Deliverable Production Record, the
  produced Deliverable Revision (Slice 3 Deliverable_Repository),
  the target Deliverable Expectation Revision (Slice 2), the
  addressed Plan Revision (Slice 2), and the Slice 1 ``Parties``
  rows seeded for the test surface.

- **32.8** — the action ``create.milestone_acceptance`` maps to the
  ``accept_milestone`` authority; an effective Role Assignment
  granting ``accept_milestone`` over the requested scope is required
  to permit the write. The happy-path test seeds exactly that role.

The tests mirror the style of
``tests/unit/test_execution_work_assignments.py`` (task 5.2) and
``tests/unit/test_execution_deliverable_productions.py`` (task 9.2):
a per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas, a
real :class:`AuthorizationService` driven through a seeded role
assignment on happy paths, direct INSERTs to seed the Slice 2 /
Slice 3 dependency rows, and counter helpers that confirm nothing
was persisted on negative paths.

NOTE: ``create.milestone_acceptance`` requires the
``accept_milestone`` authority (Requirement 32.8) rather than
``contribute``; the AD-WS-29 second-stage assignee-binding check
does NOT apply (a Milestone Acceptance Authority is by design a
Party distinct from the assignee on the source Work Assignment).
This file therefore does not assert against AD-WS-29 — the closest
analog in the Slice 3 test suite is
``test_execution_deliverable_productions.py`` which covers AD-WS-29
explicitly for Contributor writes.
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
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
)
from walking_slice.execution.milestone_acceptances import (
    CreateMilestoneAcceptanceResult,
    MilestoneAcceptanceConflictError,
    MilestoneAcceptanceService,
    MilestoneAcceptanceValidationError,
    OUTCOME_VALUES,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


# Three Parties cover every test branch: the Milestone Acceptance
# Authority that drives the happy-path write; an alternate Party used
# to seed the contributing-assignee on the source Work Assignment so
# the seeded graph is internally consistent; and the assigning
# Resource Steward that signs the seeded Role Assignment.
_ACCEPTING_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-000000a00002"
_ASSIGNMENT_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00003"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00004"

# The single Project the Slice 2 + Slice 3 dependency graph lives in.
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"

_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00030"

# The source Work Assignment whose contributing Party produced the
# Deliverable that is now being Milestone-Accepted.
_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00001"

_DELIVERABLE_ID = "00000000-0000-7000-8000-000000e00001"
_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000e00002"

_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-000000f00001"
_DELIVERABLE_EXPECTATION_REVISION_ID = (
    "00000000-0000-7000-8000-000000f00002"
)

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
def acceptance_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas.

    The Milestone Acceptance Service crosses three schemas:

    * Slice 1 (``Parties``, ``Identifier_Registry``, ``Audit_Records``,
      ``Role_Assignments``, ``Relationships``, plus the additive
      ``Identifier_Registry.resource_kind`` and
      ``Relationships.semantic_role`` columns from task 1.2).
    * Slice 2 (``Projects``, ``Activity_Plans``, ``Plan_Revisions``,
      ``Deliverable_Expectations``,
      ``Deliverable_Expectation_Revisions``).
    * Slice 3 Execution_Service (``Work_Assignment_Records``,
      ``Deliverable_Production_Records``,
      ``Milestone_Acceptance_Records`` with the
      ``UNIQUE(source_deliverable_production_id)`` constraint
      central to Requirement 28.3) and Deliverable_Repository
      (``Deliverable_Resources``, ``Deliverable_Revisions``) with
      their AD-WS-27 append-only triggers.
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


@pytest.fixture
def production_reader(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> DeliverableProductionService:
    """:class:`DeliverableProductionService` retained as a collaborator.

    Per design §"Execution_Service.MilestoneAcceptances", the
    Milestone Acceptance Service declares the production reader on
    its public dataclass surface but the current implementation
    resolves the source Production row and its ``Produces`` /
    ``Addresses`` Relationships via direct SQL on the caller's
    connection. Holding the reader as a field preserves the option
    of delegating to it without changing the public dataclass
    surface. The constructor is invoked with only the dependencies
    it requires for instantiation; the methods the Milestone
    Acceptance Service does not call are not exercised here.
    """
    return DeliverableProductionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        deliverable_reader=None,  # type: ignore[arg-type]
        planning_reader=None,  # type: ignore[arg-type]
        project_resolver=None,  # type: ignore[arg-type]
        denial_audit_sleep=lambda _seconds: None,
    )


@pytest.fixture
def milestone_acceptance_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    production_reader: DeliverableProductionService,
) -> MilestoneAcceptanceService:
    """:class:`MilestoneAcceptanceService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test. The denial-audit
    sleep is replaced with a no-op so the deny-path retries do not
    spend real time.
    """
    return MilestoneAcceptanceService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        production_reader=production_reader,
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

    All four Parties are required: the Milestone Acceptance Authority
    (the recording Party), the Contributor (assignee on the source
    Work Assignment), the Assignment Authority (named on the seeded
    Work Assignment Record), and the assigning Resource Steward
    recorded on the seeded role.
    """
    with engine.begin() as conn:
        _seed_party(conn, _ACCEPTING_PARTY_ID, "Milestone Acceptor")
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

    The Plan Revision row below carries this Activity Plan's Identity
    as its ``activity_plan_id`` foreign key.
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


def _seed_plan_revision(engine: Engine) -> None:
    """Insert one approved ``Plan_Revisions`` row by direct INSERT.

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
                    :rev, :aid, NULL, 'approved', 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_work_assignment(engine: Engine) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The Work Assignment names the Contributor as the assignee and the
    Assignment Authority as the assignment-authority. The Milestone
    Acceptance Service does not re-read this row (the
    ``accept_milestone`` action does not trigger the AD-WS-29
    second-stage assignee-binding check), but the source Deliverable
    Production Record below references it via its ``Relates To``
    Relationship.
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
                "wid": _WORK_ASSIGNMENT_ID,
                "prev": _PLAN_REVISION_ID,
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
    (Requirement 26.2) and ``originating_work_assignment_id`` pointing
    at the source Work Assignment seeded above. The Milestone
    Acceptance Service does not read these columns directly (it
    resolves the produced-Deliverable Resource and Revision Identities
    from the source Production Record's ``Produces`` Relationship);
    the rows exist so that the Production Record's ``Produces``
    Relationship has FK targets.
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
    """Insert one Deliverable Expectation header + first Revision row.

    The Slice 2 schema requires the header row in
    ``Deliverable_Expectations`` to exist before the Revision row in
    ``Deliverable_Expectation_Revisions`` can be inserted; both are
    seeded here. The Milestone Acceptance Service resolves the
    target Deliverable Expectation Resource and Revision Identities
    from the source Production Record's ``Addresses`` Relationship.
    """
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


def _seed_deliverable_production(engine: Engine) -> str:
    """Insert one ``Deliverable_Production_Records`` row plus its
    ``Produces`` and ``Addresses`` Relationships by direct INSERT.

    The Milestone Acceptance Service reads the Production Record by
    primary key and resolves the produced-Deliverable Revision and
    target-Expectation Revision Identities from the Production
    Record's ``Produces`` and ``Addresses`` Relationship rows
    respectively (design
    §"Execution_Service.MilestoneAcceptances" Responsibility). The
    Relationships are therefore inserted with the exact AD-WS-26
    column shape the service queries:

    * ``Produces`` — ``source_kind = 'deliverable_production_record'``,
      ``target_kind = 'deliverable_revision'``, ``target_id`` is the
      Deliverable Resource Identity, ``target_revision_id`` is the
      Deliverable Revision Identity, ``semantic_role IS NULL``.
    * ``Addresses`` —
      ``target_kind = 'deliverable_expectation_revision'``,
      ``target_id`` is the Expectation Resource Identity,
      ``target_revision_id`` is the Expectation Revision Identity,
      ``semantic_role IS NULL``.

    Returns the Production Record Identity so the test body can pass
    it through as ``source_deliverable_production_id``.
    """
    production_id = "00000000-0000-7000-8000-0000000d000a1"
    produces_id = "00000000-0000-7000-8000-0000000d000a2"
    addresses_id = "00000000-0000-7000-8000-0000000d000a3"
    relates_to_id = "00000000-0000-7000-8000-0000000d000a4"
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
                "pid": production_id,
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
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Produces',
                    'deliverable_production_record', :pid, NULL,
                    'deliverable_revision', :did, :rev,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": produces_id,
                "pid": production_id,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Addresses',
                    'deliverable_production_record', :pid, NULL,
                    'deliverable_expectation_revision',
                    :exp_did, :exp_rev,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": addresses_id,
                "pid": production_id,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )
        # The Production Record's third Relationship (``Relates To``
        # the source Work Assignment Record with
        # ``semantic_role = 'production_source'``) is seeded for
        # completeness even though the Milestone Acceptance Service
        # does not consult it. Keeping the seed graph
        # spec-compliant means the byte-equivalence assertion in
        # ``TestSlice1And2RowByteEquivalence`` can include this row
        # without further setup.
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Relates To',
                    'deliverable_production_record', :pid, NULL,
                    'work_assignment_record', :wa, NULL,
                    :party, :ts, 'production_source'
                )
                """
            ),
            {
                "rid": relates_to_id,
                "pid": production_id,
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )
    return production_id


def _assign_accept_milestone_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _ACCEPTING_PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Milestone Acceptance Authority (``accept_milestone``) to
    ``party_id``.

    Per Requirement 32.8 / AD-WS-24, ``create.milestone_acceptance``
    maps to the ``accept_milestone`` authority. A Party with an
    effective Role Assignment carrying ``accept_milestone`` over
    ``scope`` is permitted to create Milestone Acceptance Records
    against that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="milestone_acceptor",
        scope=scope,
        authorities_granted=("accept_milestone",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_happy_path(
    engine: Engine,
    authorization_service: AuthorizationService,
) -> str:
    """Seed every dependency required for a permitted
    :meth:`MilestoneAcceptanceService.create_milestone_acceptance`
    call.

    The default arguments produce the canonical configuration:

    * one Project with one Activity Plan and one approved Plan
      Revision;
    * one Work Assignment whose ``assignee_party_id`` is the
      Contributor Party (separate from the recording Milestone
      Acceptance Authority — the action does not trigger AD-WS-29);
    * one produced Deliverable Resource + Revision whose
      ``originating_work_assignment_id`` matches the Work Assignment;
    * one Deliverable Expectation Revision targeting the Project;
    * one source Deliverable Production Record with all three
      AD-WS-26 Relationship rows already present.

    Returns the source Deliverable Production Record Identity so the
    test body can pass it through as
    ``source_deliverable_production_id``.
    """
    _seed_required_parties(engine)
    _assign_accept_milestone_role(authorization_service, engine)
    _seed_project(engine)
    _seed_activity_plan(engine)
    _seed_plan_revision(engine)
    _seed_work_assignment(engine)
    _seed_deliverable(engine)
    _seed_deliverable_expectation(engine)
    return _seed_deliverable_production(engine)


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
    """Return a dict-shaped snapshot of one row keyed by its primary key.

    Used by the byte-equivalence assertions to capture row state
    before the Milestone Acceptance write and compare it after.
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


def _create_permitted_milestone_acceptance(
    acceptance_engine: Engine,
    milestone_acceptance_service: MilestoneAcceptanceService,
    source_production_id: str,
    *,
    outcome: str = "Accept",
    rationale: str = "Milestone one criteria satisfied.",
    correlation_id: Optional[str] = None,
) -> CreateMilestoneAcceptanceResult:
    """Drive a permitted ``create_milestone_acceptance`` call against
    the standard happy-path fixture.

    Encapsulated as a helper so the conflict, immutability, and
    byte-equivalence tests can share the same setup.
    """
    with acceptance_engine.begin() as conn:
        return milestone_acceptance_service.create_milestone_acceptance(
            conn,
            source_deliverable_production_id=source_production_id,
            outcome=outcome,  # type: ignore[arg-type]
            rationale=rationale,
            accepting_party_id=_ACCEPTING_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=acceptance_engine,
            correlation_id=correlation_id,
        )


# ===========================================================================
# Happy-path baseline — confirms wiring and the AD-WS-26 Relationship
# cardinality contract (exactly one ``Addresses`` Relationship to the
# produced Deliverable Revision with ``semantic_role IS NULL``).
# ===========================================================================


def test_create_milestone_acceptance_permits_with_one_addresses(
    acceptance_engine: Engine,
    authorization_service: AuthorizationService,
    milestone_acceptance_service: MilestoneAcceptanceService,
) -> None:
    """A permitted Milestone Acceptance write inserts exactly one
    ``Milestone_Acceptance_Records`` row, exactly one ``Addresses``
    ``Relationships`` row to the produced Deliverable Revision, and
    exactly one consequential audit row inside one transaction per
    Requirements 28.1, 28.2, 28.6, AD-WS-26.

    The happy-path baseline anchors every subsequent rejection test:
    the same fixture, with one input varied to drive a single
    rejection branch, must persist nothing.
    """
    production_id = _seed_happy_path(acceptance_engine, authorization_service)

    result = _create_permitted_milestone_acceptance(
        acceptance_engine,
        milestone_acceptance_service,
        production_id,
        correlation_id="corr-permit",
    )

    assert isinstance(result, CreateMilestoneAcceptanceResult)
    assert _CANONICAL_UUID7.match(result.milestone_acceptance_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.source_deliverable_production_id == production_id
    assert result.produced_deliverable_id == _DELIVERABLE_ID
    assert result.produced_deliverable_revision_id == _DELIVERABLE_REVISION_ID
    assert result.target_deliverable_expectation_id == (
        _DELIVERABLE_EXPECTATION_ID
    )
    assert result.target_deliverable_expectation_revision_id == (
        _DELIVERABLE_EXPECTATION_REVISION_ID
    )
    assert result.outcome == "Accept"
    assert result.correlation_id == "corr-permit"

    # Exactly one Milestone Acceptance Record persisted.
    assert _count(acceptance_engine, "Milestone_Acceptance_Records") == 1

    # Exactly one consequential audit row participates in the same
    # transaction (Requirement 28.6).
    assert _count_consequential_audit_rows(
        acceptance_engine, "create.milestone_acceptance"
    ) == 1

    # Exactly one ``Addresses`` Relationship row sourced from the
    # Milestone Acceptance Record per AD-WS-26 (``semantic_role IS
    # NULL``).
    with acceptance_engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT relationship_id, source_kind, source_id,
                       source_revision_id, target_kind, target_id,
                       target_revision_id, semantic_role
                FROM Relationships
                WHERE relationship_type = 'Addresses'
                  AND source_id = :sid
                  AND source_kind = 'milestone_acceptance_record'
                """
            ),
            {"sid": result.milestone_acceptance_id},
        ).mappings().one()
    assert row["relationship_id"] == result.addresses_relationship_id
    assert row["source_kind"] == "milestone_acceptance_record"
    assert row["source_id"] == result.milestone_acceptance_id
    assert row["source_revision_id"] is None
    assert row["target_kind"] == "deliverable_revision"
    assert row["target_id"] == _DELIVERABLE_ID
    assert row["target_revision_id"] == _DELIVERABLE_REVISION_ID
    assert row["semantic_role"] is None


# ===========================================================================
# Requirement 28.3 / 28.4 — duplicate-Milestone-Acceptance rejection.
#
# Both the application-level pre-check and the schema-level
# ``UNIQUE(source_deliverable_production_id)`` constraint are exercised.
# ===========================================================================


class TestDuplicateAcceptanceRejection:
    """Per Requirement 28.3 / 28.4, at most one Milestone Acceptance
    Record may exist per source Deliverable Production Record.

    The schema-level UNIQUE constraint is the source of truth; the
    application-level pre-check surfaces a structured
    :class:`MilestoneAcceptanceConflictError` carrying the existing
    Acceptance Identity (subject to AD-WS-9 view-authority gating).
    Both layers are pinned here so a regression at either layer is
    visible.
    """

    def test_second_acceptance_against_same_production_raises_conflict(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A second Milestone Acceptance attempt against the same
        source Production Record raises
        :class:`MilestoneAcceptanceConflictError` carrying the
        existing Acceptance Identity.

        The caller holds ``accept_milestone`` authority which the
        :func:`_required_authority` prefix rule maps ``view.*``
        actions to ``view`` from — but the seeded role assignment
        grants only ``accept_milestone``, not ``view``, so the
        conflict response carries ``existing_milestone_acceptance_id
        = None`` per the AD-WS-9 view-authority gate. The companion
        test ``test_existing_id_visible_when_caller_holds_view``
        below covers the other branch.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )

        first = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
            correlation_id="corr-first",
        )
        assert first.outcome == "Accept"

        # Snapshot the row body so a successful second INSERT or any
        # in-place mutation would be caught.
        before = _snapshot_row(
            acceptance_engine,
            "Milestone_Acceptance_Records",
            "milestone_acceptance_id",
            first.milestone_acceptance_id,
        )

        with pytest.raises(MilestoneAcceptanceConflictError) as exc_info:
            _create_permitted_milestone_acceptance(
                acceptance_engine,
                milestone_acceptance_service,
                production_id,
                outcome="Reject",
                rationale="Reverse the earlier acceptance.",
                correlation_id="corr-second",
            )

        assert exc_info.value.failed_constraint == (
            "milestone_acceptance_already_recorded"
        )
        assert exc_info.value.source_deliverable_production_id == (
            production_id
        )

        # Exactly one Milestone Acceptance row exists — the original.
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 1
        assert _count_consequential_audit_rows(
            acceptance_engine, "create.milestone_acceptance"
        ) == 1

        # The original row is byte-equivalent (no mutation, no second
        # row).
        after = _snapshot_row(
            acceptance_engine,
            "Milestone_Acceptance_Records",
            "milestone_acceptance_id",
            first.milestone_acceptance_id,
        )
        assert before == after

    def test_existing_id_visible_when_caller_holds_view(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """When the caller holds ``view`` authority on the existing
        Milestone Acceptance, the conflict response carries the
        existing Acceptance Identity per AD-WS-9 / Slice 3
        Requirement 30.4.

        The happy-path role grants only ``accept_milestone``; an
        additional Role Assignment carrying ``view`` over the same
        scope is seeded here so the conflict pre-check's
        ``view.milestone_acceptance`` evaluation permits.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        # Grant the recording Party an additional ``view`` authority
        # so the AD-WS-9 conflict-visibility gate permits.
        view_request = AssignRoleRequest(
            party_id=_ACCEPTING_PARTY_ID,
            role_name="milestone_viewer",
            scope=_SCOPE,
            authorities_granted=("view",),
            effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            effective_end=None,
            assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
        )
        with acceptance_engine.begin() as conn:
            authorization_service.assign_role(conn, view_request)

        first = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        with pytest.raises(MilestoneAcceptanceConflictError) as exc_info:
            _create_permitted_milestone_acceptance(
                acceptance_engine,
                milestone_acceptance_service,
                production_id,
                outcome="Reject",
                rationale="Second attempt.",
            )

        assert exc_info.value.existing_milestone_acceptance_id == (
            first.milestone_acceptance_id
        )

    def test_db_layer_unique_constraint_rejects_bypassed_insert(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """Bypassing the service and inserting a second Milestone
        Acceptance Record directly fails on the schema-level
        ``UNIQUE(source_deliverable_production_id)`` constraint per
        Requirement 28.3.

        The pre-check is a *convenience* that surfaces a structured
        error in place of a raw :class:`IntegrityError`; the
        authoritative invariant lives in the schema. This test pins
        the schema layer so a regression that drops or weakens the
        UNIQUE constraint is visible even if the service code
        somehow forgets the pre-check.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )

        first = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        # A direct second INSERT — same
        # ``source_deliverable_production_id``, different Acceptance
        # Identity — must fail on the schema UNIQUE constraint
        # rather than silently succeed.
        second_id = "00000000-0000-7000-8000-0000beef0002"
        with acceptance_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
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
                            'Reject', 'Direct insert.',
                            :party, 'role-grant-id', :abid, :scope, :ts
                        )
                        """
                    ),
                    {
                        "mid": second_id,
                        "pid": production_id,
                        "did": _DELIVERABLE_ID,
                        "rev": _DELIVERABLE_REVISION_ID,
                        "exp_did": _DELIVERABLE_EXPECTATION_ID,
                        "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                        "party": _ACCEPTING_PARTY_ID,
                        "abid": str(_AUTHORITY_BASIS_ID),
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        # The original Milestone Acceptance survives; no second row.
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 1
        with acceptance_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT milestone_acceptance_id "
                    "FROM Milestone_Acceptance_Records "
                    "WHERE milestone_acceptance_id = :id"
                ),
                {"id": first.milestone_acceptance_id},
            ).scalar_one()
        assert still_present == first.milestone_acceptance_id


# ===========================================================================
# Requirement 28.4 — ``outcome`` enumeration boundaries.
# ===========================================================================


class TestOutcomeEnumeration:
    """Per Requirement 28.2 the ``outcome`` is drawn from the
    enumerated set ``{Accept, Reject}``; per Requirement 28.4 a value
    outside the set is rejected with no Milestone Acceptance Record
    persisted.

    Validation runs in the static validator before any database read
    so a malformed request never touches the production reader or
    the authorization service.
    """

    def test_outcome_constant_matches_requirement(self) -> None:
        """The :data:`OUTCOME_VALUES` constant pins exactly the
        Requirement 28.2 enumeration. A regression that adds or
        removes a value would be caught even before the service is
        consulted.
        """
        assert OUTCOME_VALUES == ("Accept", "Reject")

    @pytest.mark.parametrize("valid_outcome", ["Accept", "Reject"])
    def test_each_valid_outcome_accepted(
        self,
        valid_outcome: str,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """Each value in :data:`OUTCOME_VALUES` is accepted and
        persisted byte-equivalent on the row.

        Per Requirement 28.2 both ``Accept`` and ``Reject`` are
        admissible outcomes of a Milestone Acceptance attempt
        (recording rejection is a first-class action).
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )

        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
            outcome=valid_outcome,
            rationale="Outcome boundary test.",
        )

        assert result.outcome == valid_outcome
        with acceptance_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT outcome FROM Milestone_Acceptance_Records "
                    "WHERE milestone_acceptance_id = :id"
                ),
                {"id": result.milestone_acceptance_id},
            ).scalar_one()
        assert stored == valid_outcome

    @pytest.mark.parametrize(
        "invalid_outcome",
        ["accept", "ACCEPT", "Approved", "Rejected", "Maybe", " Accept "],
    )
    def test_outcome_out_of_set_rejected(
        self,
        invalid_outcome: str,
        acceptance_engine: Engine,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """Any value outside :data:`OUTCOME_VALUES` raises
        :class:`MilestoneAcceptanceValidationError` with
        ``failed_constraint='outcome_out_of_set'``.

        Case differences (``accept``, ``ACCEPT``), near-synonyms
        (``Approved``, ``Rejected``), and surrounding whitespace
        (`` Accept ``) are each rejected — the enumeration is exact.
        """
        with acceptance_engine.begin() as conn:
            with pytest.raises(
                MilestoneAcceptanceValidationError
            ) as exc_info:
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        "00000000-0000-7000-8000-0000000d000a1"
                    ),
                    outcome=invalid_outcome,  # type: ignore[arg-type]
                    rationale="Boundary.",
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )

        assert exc_info.value.failed_constraint == "outcome_out_of_set"
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 0

    @pytest.mark.parametrize("missing_outcome", [None, ""])
    def test_missing_or_empty_outcome_rejected(
        self,
        missing_outcome,
        acceptance_engine: Engine,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A missing or empty ``outcome`` raises with
        ``failed_constraint='outcome_missing'``.

        Distinct from the out-of-set rejection so the route layer can
        pinpoint whether the field was omitted or carried a wrong
        value.
        """
        with acceptance_engine.begin() as conn:
            with pytest.raises(
                MilestoneAcceptanceValidationError
            ) as exc_info:
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        "00000000-0000-7000-8000-0000000d000a1"
                    ),
                    outcome=missing_outcome,  # type: ignore[arg-type]
                    rationale="Boundary.",
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )

        assert exc_info.value.failed_constraint == "outcome_missing"
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 0


# ===========================================================================
# Requirement 28.4 — rationale length boundaries (1..4000).
# ===========================================================================


class TestRationaleLengthBoundaries:
    """Per Requirement 28.2 the rationale must be 1..4000 characters.

    Distinct from Work Assignment's 0..4000 range — Milestone
    Acceptances must carry a non-empty rationale because the
    rationale is the canonical record of *why* an Authority accepted
    (or rejected) a produced Deliverable Revision. The schema
    CHECK constraint
    ``length(rationale) BETWEEN 1 AND 4000`` enforces the same range
    at the database layer.
    """

    def test_one_char_rationale_accepted(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A 1-char rationale sits at the lower boundary and is
        accepted and persisted byte-equivalent.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )

        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
            rationale="x",
        )

        assert result.rationale == "x"
        with acceptance_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT rationale FROM Milestone_Acceptance_Records "
                    "WHERE milestone_acceptance_id = :id"
                ),
                {"id": result.milestone_acceptance_id},
            ).scalar_one()
        assert stored == "x"

    def test_four_thousand_char_rationale_accepted(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A 4000-char rationale sits at the upper boundary."""
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )

        rationale = "x" * 4_000
        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
            rationale=rationale,
        )

        assert result.rationale == rationale
        assert len(result.rationale) == 4_000
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 1

    def test_zero_length_rationale_rejected(
        self,
        acceptance_engine: Engine,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """``len(rationale) == 0`` raises
        :class:`MilestoneAcceptanceValidationError` with
        ``failed_constraint='rationale_too_short'`` and persists
        nothing.

        Distinct from the Work Assignment branch (which accepts the
        empty string) because Requirement 28.2's lower bound is 1
        rather than 0.
        """
        with acceptance_engine.begin() as conn:
            with pytest.raises(
                MilestoneAcceptanceValidationError
            ) as exc_info:
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        "00000000-0000-7000-8000-0000000d000a1"
                    ),
                    outcome="Accept",
                    rationale="",
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )

        assert exc_info.value.failed_constraint == "rationale_too_short"
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 0

    def test_four_thousand_one_char_rationale_rejected(
        self,
        acceptance_engine: Engine,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """``len(rationale) == 4001`` raises
        :class:`MilestoneAcceptanceValidationError` with
        ``failed_constraint='rationale_too_long'`` and persists
        nothing.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        with acceptance_engine.begin() as conn:
            with pytest.raises(
                MilestoneAcceptanceValidationError
            ) as exc_info:
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        "00000000-0000-7000-8000-0000000d000a1"
                    ),
                    outcome="Accept",
                    rationale="x" * 4_001,
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )

        assert exc_info.value.failed_constraint == "rationale_too_long"
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 0


# ===========================================================================
# Requirement 28.4 — authority-basis-type enumeration (AD-WS-10).
# ===========================================================================


class TestAuthorityBasisEnumeration:
    """Per Requirement 28.2 / AD-WS-10, the authority basis ``type``
    is drawn from ``{role-grant-id, scope-id, delegation-chain-id}``.

    Both happy-path acceptance and out-of-set rejection are pinned
    here for each value. Slice 3 reuses the Slice 1 enumeration
    unchanged per AD-WS-31; this test prevents a regression from
    inadvertently extending or narrowing the set on the Milestone
    Acceptance surface.
    """

    @pytest.mark.parametrize(
        "valid_type",
        ["role-grant-id", "scope-id", "delegation-chain-id"],
    )
    def test_valid_authority_basis_types_accepted(
        self,
        valid_type: str,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """Each of the three AD-WS-10 ``type`` values is accepted.

        Mapping-shaped input (the form the HTTP layer may forward
        before the request is bound to the typed model) is normalized
        through :class:`AuthorityBasisRef` and persisted on the row.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        basis_mapping = {
            "type": valid_type,
            "id": str(_AUTHORITY_BASIS_ID),
        }

        with acceptance_engine.begin() as conn:
            result = (
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=production_id,
                    outcome="Accept",
                    rationale="Authority basis boundary.",
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=basis_mapping,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )
            )

        assert result.authority_basis.type == valid_type
        with acceptance_engine.connect() as conn:
            stored_type = conn.execute(
                text(
                    "SELECT authority_basis_type "
                    "FROM Milestone_Acceptance_Records "
                    "WHERE milestone_acceptance_id = :id"
                ),
                {"id": result.milestone_acceptance_id},
            ).scalar_one()
        assert stored_type == valid_type

    def test_unknown_authority_basis_type_rejected(
        self,
        acceptance_engine: Engine,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A ``type`` outside the AD-WS-10 set raises with
        ``failed_constraint='authority_basis_type_out_of_set'`` and
        persists nothing.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        basis_mapping = {
            "type": "not-a-valid-type",
            "id": str(_AUTHORITY_BASIS_ID),
        }

        with acceptance_engine.begin() as conn:
            with pytest.raises(
                MilestoneAcceptanceValidationError
            ) as exc_info:
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        "00000000-0000-7000-8000-0000000d000a1"
                    ),
                    outcome="Accept",
                    rationale="Authority basis boundary.",
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=basis_mapping,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )

        assert exc_info.value.failed_constraint == (
            "authority_basis_type_out_of_set"
        )
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 0

    def test_missing_authority_basis_type_rejected(
        self,
        acceptance_engine: Engine,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A mapping with no ``type`` key raises with
        ``failed_constraint='authority_basis_type_missing'``.

        Distinct from the out-of-set rejection so the route layer
        can pinpoint which authority-basis field is malformed.
        """
        basis_mapping = {"id": str(_AUTHORITY_BASIS_ID)}

        with acceptance_engine.begin() as conn:
            with pytest.raises(
                MilestoneAcceptanceValidationError
            ) as exc_info:
                milestone_acceptance_service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        "00000000-0000-7000-8000-0000000d000a1"
                    ),
                    outcome="Accept",
                    rationale="Authority basis boundary.",
                    accepting_party_id=_ACCEPTING_PARTY_ID,
                    authority_basis=basis_mapping,
                    applicable_scope=_SCOPE,
                    engine=acceptance_engine,
                )

        assert exc_info.value.failed_constraint == (
            "authority_basis_type_missing"
        )
        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 0


# ===========================================================================
# Requirement 28.7 / AD-WS-27 — immutability rejection of UPDATE / DELETE.
# ===========================================================================


class TestMilestoneAcceptanceImmutability:
    """Per Requirement 28.7 / AD-WS-27, after a Milestone Acceptance
    Record is finalized the row and its ``Addresses`` Relationship
    row reject every UPDATE / DELETE attempt via the append-only
    triggers installed by task 1.2 (Slice 3 Execution_Service tables)
    and Slice 1 ``Relationships`` triggers.

    The triggers raise ``RAISE(ABORT, ...)`` which SQLAlchemy
    surfaces as :class:`sqlalchemy.exc.IntegrityError`.
    """

    def test_update_on_record_rejected_after_persistence(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """An UPDATE on ``Milestone_Acceptance_Records`` after commit
        raises ``IntegrityError`` and leaves the row unchanged.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        before = _snapshot_row(
            acceptance_engine,
            "Milestone_Acceptance_Records",
            "milestone_acceptance_id",
            result.milestone_acceptance_id,
        )

        with acceptance_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Milestone_Acceptance_Records "
                        "SET rationale = 'tampered' "
                        "WHERE milestone_acceptance_id = :id"
                    ),
                    {"id": result.milestone_acceptance_id},
                )

        after = _snapshot_row(
            acceptance_engine,
            "Milestone_Acceptance_Records",
            "milestone_acceptance_id",
            result.milestone_acceptance_id,
        )
        assert after == before

    def test_delete_on_record_rejected_after_persistence(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A DELETE on ``Milestone_Acceptance_Records`` after commit
        raises ``IntegrityError`` and the row remains in place.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        with acceptance_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "DELETE FROM Milestone_Acceptance_Records "
                        "WHERE milestone_acceptance_id = :id"
                    ),
                    {"id": result.milestone_acceptance_id},
                )

        assert _count(
            acceptance_engine, "Milestone_Acceptance_Records"
        ) == 1
        with acceptance_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT milestone_acceptance_id "
                    "FROM Milestone_Acceptance_Records "
                    "WHERE milestone_acceptance_id = :id"
                ),
                {"id": result.milestone_acceptance_id},
            ).scalar_one()
        assert still_present == result.milestone_acceptance_id

    def test_update_on_addresses_relationship_rejected(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """An UPDATE on the ``Addresses`` ``Relationships`` row
        produced alongside a Milestone Acceptance Record raises
        ``IntegrityError``.

        The Slice 1 ``Relationships`` table is one of the AD-WS-4
        immutable tables; its UPDATE rejection trigger fires
        regardless of which Slice 3 source kind the row carries.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )
        before = _snapshot_relationship_row(
            acceptance_engine, result.addresses_relationship_id
        )

        with acceptance_engine.connect() as conn, pytest.raises(
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
            acceptance_engine, result.addresses_relationship_id
        )
        assert after == before

    def test_delete_on_addresses_relationship_rejected(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """A DELETE on the ``Addresses`` ``Relationships`` row
        produced alongside a Milestone Acceptance Record raises
        ``IntegrityError`` and the row survives.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        result = _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        with acceptance_engine.connect() as conn, pytest.raises(
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

        with acceptance_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT relationship_id FROM Relationships "
                    "WHERE relationship_id = :id"
                ),
                {"id": result.addresses_relationship_id},
            ).scalar_one()
        assert still_present == result.addresses_relationship_id


# ===========================================================================
# Requirement 28.8 / 40 §1 / Property 11 — Slice 1 / Slice 2 byte-equivalence.
#
# After a permitted Milestone Acceptance write, every row the service is
# explicitly forbidden to mutate must remain byte-equivalent to its
# pre-Acceptance state. The set captured here is: the source Deliverable
# Production Record, the produced Deliverable Revision (Slice 3
# Deliverable_Repository), the target Deliverable Expectation Revision
# (Slice 2), the addressed Plan Revision (Slice 2), and the seeded
# ``Parties`` rows (Slice 1). The Production Record's three Relationship
# rows are also asserted byte-equivalent.
# ===========================================================================


class TestSlice1And2RowByteEquivalence:
    """Per Requirement 28.8 / Requirement 40 §1 / Property 11, the
    Milestone Acceptance write does not mutate any Slice 1 or Slice 2
    row, nor the source Production Record or produced Deliverable
    Revision in Slice 3 Deliverable_Repository.

    Each row that is part of the seed graph is snapshotted before the
    Acceptance write and asserted byte-equivalent after.
    """

    def test_source_production_record_byte_equivalent(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """The source Deliverable Production Record row remains
        byte-equivalent across the Milestone Acceptance write.

        Requirement 28.8 explicitly forbids modifying the source
        Production Record as a consequence of recording an
        Acceptance.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        before = _snapshot_row(
            acceptance_engine,
            "Deliverable_Production_Records",
            "deliverable_production_id",
            production_id,
        )

        _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        after = _snapshot_row(
            acceptance_engine,
            "Deliverable_Production_Records",
            "deliverable_production_id",
            production_id,
        )
        assert after == before

    def test_produced_deliverable_revision_byte_equivalent(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """The produced Deliverable Revision row remains
        byte-equivalent across the Milestone Acceptance write.

        Requirement 28.8 forbids modifying the produced Deliverable
        Revision as a consequence of recording an Acceptance.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        before = _snapshot_row(
            acceptance_engine,
            "Deliverable_Revisions",
            "deliverable_revision_id",
            _DELIVERABLE_REVISION_ID,
        )

        _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        after = _snapshot_row(
            acceptance_engine,
            "Deliverable_Revisions",
            "deliverable_revision_id",
            _DELIVERABLE_REVISION_ID,
        )
        assert after == before

    def test_target_deliverable_expectation_revision_byte_equivalent(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """The target Deliverable Expectation Revision row remains
        byte-equivalent across the Milestone Acceptance write.

        The Expectation Revision is owned by Slice 2; Requirement
        40 §1 and Requirement 28.8 forbid Slice 3 from mutating
        Slice 2 rows.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        before = _snapshot_row(
            acceptance_engine,
            "Deliverable_Expectation_Revisions",
            "deliverable_expectation_revision_id",
            _DELIVERABLE_EXPECTATION_REVISION_ID,
        )

        _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        after = _snapshot_row(
            acceptance_engine,
            "Deliverable_Expectation_Revisions",
            "deliverable_expectation_revision_id",
            _DELIVERABLE_EXPECTATION_REVISION_ID,
        )
        assert after == before

    def test_plan_revision_byte_equivalent(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """The addressed Plan Revision row remains byte-equivalent
        across the Milestone Acceptance write.

        Even though the Plan Revision is only transitively
        referenced (via the source Work Assignment), Requirement
        40 §1 forbids any Slice 3 write from mutating any Slice 2
        row.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        before = _snapshot_row(
            acceptance_engine,
            "Plan_Revisions",
            "plan_revision_id",
            _PLAN_REVISION_ID,
        )

        _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        after = _snapshot_row(
            acceptance_engine,
            "Plan_Revisions",
            "plan_revision_id",
            _PLAN_REVISION_ID,
        )
        assert after == before

    @pytest.mark.parametrize(
        "party_id,party_label",
        [
            (_ACCEPTING_PARTY_ID, "Milestone Acceptor"),
            (_CONTRIBUTOR_PARTY_ID, "Contributor"),
            (_ASSIGNMENT_AUTHORITY_ID, "Assignment Authority"),
            (_ASSIGNING_AUTHORITY_ID, "Resource Steward"),
        ],
    )
    def test_party_rows_byte_equivalent(
        self,
        party_id: str,
        party_label: str,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """Every seeded ``Parties`` row remains byte-equivalent across
        the Milestone Acceptance write.

        The ``Parties`` table is owned by Slice 1; Requirement 40 §1
        and Requirement 28.8 forbid Slice 3 from mutating Slice 1
        rows. Parameterized across all four seeded Parties to pin
        the invariant on each role independently.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )
        before = _snapshot_row(
            acceptance_engine, "Parties", "party_id", party_id
        )

        _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        after = _snapshot_row(
            acceptance_engine, "Parties", "party_id", party_id
        )
        assert after == before, (
            f"{party_label} Party row mutated by Milestone Acceptance "
            "write, violating Requirement 28.8 / 40 §1."
        )

    def test_production_record_relationships_byte_equivalent(
        self,
        acceptance_engine: Engine,
        authorization_service: AuthorizationService,
        milestone_acceptance_service: MilestoneAcceptanceService,
    ) -> None:
        """The three ``Relationships`` rows sourced from the source
        Deliverable Production Record (``Produces``, ``Addresses``,
        ``Relates To``) remain byte-equivalent across the Milestone
        Acceptance write.

        Requirement 28.8 forbids modifying the Production Record's
        constituent Relationships as a consequence of recording an
        Acceptance. Snapshotting every row sourced from the
        Production Record at once means a regression that touches
        any of the three is caught.
        """
        production_id = _seed_happy_path(
            acceptance_engine, authorization_service
        )

        with acceptance_engine.connect() as conn:
            before = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT * FROM Relationships "
                        "WHERE source_kind = "
                        "'deliverable_production_record' "
                        "  AND source_id = :sid "
                        "ORDER BY relationship_type"
                    ),
                    {"sid": production_id},
                ).mappings().all()
            ]

        _create_permitted_milestone_acceptance(
            acceptance_engine,
            milestone_acceptance_service,
            production_id,
        )

        with acceptance_engine.connect() as conn:
            after = [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT * FROM Relationships "
                        "WHERE source_kind = "
                        "'deliverable_production_record' "
                        "  AND source_id = :sid "
                        "ORDER BY relationship_type"
                    ),
                    {"sid": production_id},
                ).mappings().all()
            ]
        # Sanity check: all three Production-sourced Relationships
        # exist in the snapshot.
        assert len(before) == 3
        assert after == before
