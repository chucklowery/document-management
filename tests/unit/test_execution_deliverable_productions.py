"""Unit tests for :mod:`walking_slice.execution.deliverable_productions` (task 9.2).

Pins the contract established in task 9.1, design
§"Execution_Service.DeliverableProductions", AD-WS-9
(separate-transaction Denial Record), AD-WS-24
(``create.deliverable_production`` → ``contribute``), AD-WS-26
(Relationship-Type / semantic-role table), AD-WS-27 (append-only
Slice 3 tables), AD-WS-28 (additive ``resource_kind`` values), AD-WS-29
(two-stage Contributor authority evaluation), AD-WS-30
(Planning_Service public read APIs), and Requirements 27.3, 27.4, 27.5,
27.7, 32.7:

- **27.3 — cross-Project mismatch.** When the target Deliverable
  Expectation Revision's ``target_project_id`` does not match the
  Project Identity reached by walking the source Work Assignment's
  Plan Revision → Activity Plan → Project via
  :class:`ProjectResolver`, the request is rejected with
  :class:`DeliverableProductionProjectMismatchError` carrying
  ``failed_constraint = 'deliverable_expectation_project_mismatch'``;
  no Deliverable Production Record / Relationship rows / consequential
  audit row is persisted. The rejection runs before authorization
  evaluation so the deny path cannot reveal the Project linkage of
  either the Work Assignment or the Expectation to an unauthorized
  caller (Requirement 30 — indistinguishable denials).

- **27.4 — forged production.** When the produced Deliverable
  Revision's persisted ``originating_work_assignment_id`` does not
  match the supplied ``source_work_assignment_id``, the request is
  rejected with :class:`DeliverableProductionOriginatingBindingError`
  carrying ``failed_constraint =
  'produced_revision_originating_work_assignment_mismatch'``; no row
  is persisted. The check rejects forged-production attempts where a
  Contributor names a peer's produced Deliverable Revision as their
  own production. The rejection runs before authorization evaluation
  so the deny path cannot reveal the produced Revision's authoring
  chain to an unauthorized caller.

- **32.7 / AD-WS-29 — assignee-binding rejection.** Even when
  authorization permits the ``create.deliverable_production`` action
  (the recording Party holds the ``contribute`` authority over the
  relevant scope), the request is rejected unless the persisted
  ``Work_Assignment_Records.assignee_party_id`` matches the supplied
  ``recording_party_id``. The rejection surfaces as
  :class:`DeliverableProductionAssigneeBindingError` (a subclass of
  :class:`DeliverableProductionAuthorizationError`) with
  ``reason_code = 'no-role-assignment'`` and persists exactly one
  Denial Record in a separate transaction; the caller's transaction
  rolls back so no Deliverable Production Record / Relationship rows
  / consequential audit row is persisted.

- **27.7 / AD-WS-27 — immutability.**
  ``Deliverable_Production_Records`` rejects UPDATE and DELETE via the
  AD-WS-27 append-only triggers; the three ``Relationships`` rows
  inserted alongside are governed by the Slice 1 ``Relationships``
  append-only triggers (every Slice 1 immutable table rejects UPDATE
  and DELETE per AD-WS-4). The triggers raise ``RAISE(ABORT, ...)``
  which SQLAlchemy surfaces as
  :class:`sqlalchemy.exc.IntegrityError`.

- **27.5 / AD-WS-26 — one Produces, one Addresses, one Relates To.**
  A permitted Deliverable Production write inserts exactly three
  ``Relationships`` rows: one ``Produces`` to the produced Deliverable
  Revision (``semantic_role IS NULL``), one ``Addresses`` to the
  target Deliverable Expectation Revision (``semantic_role IS NULL``),
  and one ``Relates To`` to the source Work Assignment Record with
  ``semantic_role = 'production_source'``. The cardinality is exactly
  one of each per Production Record.

The tests mirror the style of
``tests/unit/test_execution_work_assignments.py`` (task 5.2),
``tests/unit/test_execution_work_events.py`` (task 6.2), and
``tests/unit/test_deliverables_repository.py`` (task 4.3): a per-test
engine carrying the Slice 1 + Slice 2 + Slice 3 schemas, a real
:class:`AuthorizationService` driven through a seeded role assignment
on happy paths, direct INSERTs to seed the Slice 2 / Slice 3 dependency
rows, and counter helpers that confirm nothing was persisted on
negative paths.
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
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.deliverable_productions import (
    CreateDeliverableProductionResult,
    DeliverableProductionAssigneeBindingError,
    DeliverableProductionAuthorizationError,
    DeliverableProductionOriginatingBindingError,
    DeliverableProductionProjectMismatchError,
    DeliverableProductionService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


_RECORDING_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000a00002"
_ASSIGNMENT_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00003"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00004"

# The two Projects exercised by the cross-Project mismatch path.
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_OTHER_PROJECT_ID = "00000000-0000-7000-8000-000000c00011"

_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00030"

_BOUND_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00001"
# Used by the forged-production test as the originating Work Assignment
# of the produced Deliverable Revision, distinct from the
# ``source_work_assignment_id`` named in the request.
_OTHER_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00002"

_DELIVERABLE_ID = "00000000-0000-7000-8000-000000e00001"
_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-000000e00002"

# Deliverable Expectation that lives in ``_PROJECT_ID`` (matching the
# Work Assignment's Plan Revision → Project) — the happy-path target.
_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-000000f00001"
_DELIVERABLE_EXPECTATION_REVISION_ID = (
    "00000000-0000-7000-8000-000000f00002"
)
# Deliverable Expectation that lives in ``_OTHER_PROJECT_ID`` — used
# to drive the cross-Project mismatch path.
_OTHER_DELIVERABLE_EXPECTATION_ID = (
    "00000000-0000-7000-8000-000000f00010"
)
_OTHER_DELIVERABLE_EXPECTATION_REVISION_ID = (
    "00000000-0000-7000-8000-000000f00011"
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
def production_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 2 + Slice 3 schemas.

    The Deliverable Production Service crosses three schemas:

    * Slice 1 (``Parties``, ``Identifier_Registry``, ``Audit_Records``,
      ``Role_Assignments``, ``Relationships``, plus the additive
      ``Identifier_Registry.resource_kind`` and
      ``Relationships.semantic_role`` columns from task 1.2).
    * Slice 2 (``Projects``, ``Activity_Plans``, ``Plan_Revisions``,
      ``Deliverable_Expectations``,
      ``Deliverable_Expectation_Revisions``, plus the AD-WS-19
      lifecycle trigger on ``Plan_Revisions``) — the source for
      the AD-WS-30 ``DeliverableExpectationService.get_revision`` and
      :class:`ProjectResolver` reads.
    * Slice 3 Execution_Service (``Work_Assignment_Records``,
      ``Deliverable_Production_Records``, ...) and
      Deliverable_Repository (``Deliverable_Resources``,
      ``Deliverable_Revisions``) with their AD-WS-27 append-only
      triggers.
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


@pytest.fixture
def deliverable_reader(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> DeliverableRepositoryService:
    """:class:`DeliverableRepositoryService` for the Slice 3 read API.

    Only :meth:`DeliverableRepositoryService.get_revision` (task 4.2)
    is consulted by the Deliverable Production Service to resolve the
    produced Deliverable Revision row and read its
    ``originating_work_assignment_id`` for the Requirement 27.4
    originating-binding check.
    """
    return DeliverableRepositoryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )


@pytest.fixture
def expectation_reader(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> DeliverableExpectationService:
    """:class:`DeliverableExpectationService` for the AD-WS-30 read API.

    Only :meth:`DeliverableExpectationService.get_revision` is
    consulted by the Deliverable Production Service to resolve the
    target Deliverable Expectation Revision row and retrieve its
    ``target_project_id`` for the Requirement 27.3 project-membership
    check.
    """
    return DeliverableExpectationService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
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
def production_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    deliverable_reader: DeliverableRepositoryService,
    expectation_reader: DeliverableExpectationService,
    project_resolver: ProjectResolver,
) -> DeliverableProductionService:
    """:class:`DeliverableProductionService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test. The denial-audit
    sleep is replaced with a no-op so the deny-path retries do not
    spend real time.
    """
    return DeliverableProductionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        deliverable_reader=deliverable_reader,
        planning_reader=expectation_reader,
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

    All four Parties are required: the recording Contributor, an
    alternate Party used to exercise the AD-WS-29 mismatch path, the
    Assignment-Authority Party (named on
    ``Work_Assignment_Records.assignment_authority_party_id``), and
    the Assigning-Authority Party recorded on the seeded role.
    """
    with engine.begin() as conn:
        _seed_party(conn, _RECORDING_PARTY_ID, "Contributor")
        _seed_party(conn, _OTHER_PARTY_ID, "Other Contributor")
        _seed_party(conn, _ASSIGNMENT_AUTHORITY_ID, "Assignment Authority")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_project(engine: Engine, project_id: str = _PROJECT_ID) -> None:
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
            {"pid": project_id, "ts": _TS_FIXED},
        )


def _seed_activity_plan(
    engine: Engine,
    *,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    project_id: str = _PROJECT_ID,
) -> None:
    """Seed one ``Activity_Plans`` row pointing at ``project_id``.

    :class:`ProjectResolver` follows
    ``Activity_Plans.target_project_id`` from this row to compute the
    owning Project Identity of the parent Plan Revision.
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
                "aid": activity_plan_id,
                "pid": project_id,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str = _PLAN_REVISION_ID,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    lifecycle_state: str = "approved",
    applicable_scope: str = _SCOPE,
) -> None:
    """Insert one ``Plan_Revisions`` row by direct INSERT.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so a row
    with ``lifecycle_state = 'approved'`` may be inserted in one
    statement without driving the Plan Approval transaction
    (mirrors the pattern in
    ``tests/unit/test_planning_slice3_read_apis.py`` and
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
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str = _BOUND_WORK_ASSIGNMENT_ID,
    target_plan_revision_id: str = _PLAN_REVISION_ID,
    assignee_party_id: str = _RECORDING_PARTY_ID,
    applicable_scope: str = _SCOPE,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The AD-WS-27 UPDATE/DELETE rejection triggers only fire on UPDATE
    and DELETE, so an INSERT may proceed in one statement without
    driving the full :class:`WorkAssignmentService`. The
    ``assignee_party_id != assignment_authority_party_id`` CHECK
    constraint (Requirement 23.5) is honored by the default values.
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
                "assignee": assignee_party_id,
                "authority": _ASSIGNMENT_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable(
    engine: Engine,
    *,
    deliverable_id: str = _DELIVERABLE_ID,
    deliverable_revision_id: str = _DELIVERABLE_REVISION_ID,
    originating_work_assignment_id: str = _BOUND_WORK_ASSIGNMENT_ID,
    authoring_party_id: str = _RECORDING_PARTY_ID,
) -> None:
    """Insert one ``Deliverable_Resources`` + first ``Deliverable_Revisions``
    pair by direct INSERT.

    The produced Deliverable Resource carries the produced-Deliverable
    name; the Revision carries the ``role_marker = 'generated_output'``
    (Requirement 26.2 / Persistence Invariants Summary rule 9) and the
    ``originating_work_assignment_id`` consumed by the Requirement 27.4
    originating-binding check.
    """
    # Digest of the literal byte string ``b"produced"``; the schema
    # CHECK requires a 64-char SHA-256 hex string but the read paths
    # exercised here never recompute the digest from
    # ``content_bytes``, so any 64-char hex value is sufficient.
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, :name, :ts)
                """
            ),
            {
                "did": deliverable_id,
                "name": "Mesh runbook",
                "ts": _TS_FIXED,
            },
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
                "rev": deliverable_revision_id,
                "did": deliverable_id,
                "bytes": b"produced",
                "digest": digest,
                "wa": originating_work_assignment_id,
                "party": authoring_party_id,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_expectation(
    engine: Engine,
    *,
    deliverable_expectation_id: str = _DELIVERABLE_EXPECTATION_ID,
    deliverable_expectation_revision_id: str = (
        _DELIVERABLE_EXPECTATION_REVISION_ID
    ),
    target_project_id: str = _PROJECT_ID,
) -> None:
    """Insert one ``Deliverable_Expectations`` header + first Revision row.

    The Slice 2 schema requires the header row in
    ``Deliverable_Expectations`` to exist before the Revision row in
    ``Deliverable_Expectation_Revisions`` can be inserted; both are
    seeded here so
    :meth:`DeliverableExpectationService.get_revision` can read the
    Revision in one SELECT.
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
                "rev": deliverable_expectation_revision_id,
                "did": deliverable_expectation_id,
                "pid": target_project_id,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_contribute_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _RECORDING_PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Contributor authority (``contribute``) to ``party_id``.

    Per AD-WS-24, ``create.deliverable_production`` maps to the
    ``contribute`` authority. A Party with an effective Role
    Assignment carrying ``contribute`` over ``scope`` plus the
    AD-WS-29 assignee binding may create Deliverable Production
    Records against that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="contributor",
        scope=scope,
        authorities_granted=("contribute",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_happy_path(
    engine: Engine,
    authorization_service: AuthorizationService,
    *,
    assignee_party_id: str = _RECORDING_PARTY_ID,
    originating_work_assignment_id: str = _BOUND_WORK_ASSIGNMENT_ID,
    target_project_id: str = _PROJECT_ID,
) -> None:
    """Seed every dependency required for a permitted
    :meth:`DeliverableProductionService.create_deliverable_production`
    call.

    The default arguments produce the canonical configuration:

    * one Project (``_PROJECT_ID``) with one Activity Plan and one
      approved Plan Revision;
    * one Work Assignment whose ``assignee_party_id`` matches the
      recording Party so AD-WS-29's second stage passes;
    * one produced Deliverable Resource + Revision whose
      ``originating_work_assignment_id`` matches the Work Assignment
      so Requirement 27.4's originating-binding check passes;
    * one Deliverable Expectation Revision targeting
      ``_PROJECT_ID`` so Requirement 27.3's project-membership check
      passes.

    Negative-path tests vary one of the configurable inputs (the
    assignee Party, the originating Work Assignment, or the
    Expectation's target Project) to drive a single rejection
    branch.
    """
    _seed_required_parties(engine)
    _assign_contribute_role(authorization_service, engine)
    _seed_project(engine, project_id=_PROJECT_ID)
    _seed_activity_plan(engine, project_id=_PROJECT_ID)
    _seed_plan_revision(engine)
    _seed_work_assignment(
        engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=assignee_party_id,
    )
    _seed_deliverable(
        engine,
        originating_work_assignment_id=originating_work_assignment_id,
    )
    _seed_deliverable_expectation(
        engine,
        target_project_id=target_project_id,
    )


# ---------------------------------------------------------------------------
# Row counters.
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
    semantic_role_is_null: bool = False,
) -> int:
    """Count ``Relationships`` rows whose ``source_id`` is ``source_id``.

    When ``semantic_role_is_null`` is ``True`` the match selects rows
    whose ``semantic_role IS NULL``; otherwise ``semantic_role`` is
    matched exactly.
    """
    sql = (
        "SELECT COUNT(*) FROM Relationships "
        "WHERE relationship_type = :rt AND source_id = :sid "
    )
    params: dict = {"rt": relationship_type, "sid": source_id}
    if semantic_role_is_null:
        sql += "AND semantic_role IS NULL"
    else:
        sql += "AND semantic_role = :role"
        params["role"] = semantic_role
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar_one())


def _count_denial_audit_rows(engine: Engine, action_type: str) -> int:
    """Count Denial Record rows for ``action_type``.

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


def _create_permitted_production(
    production_engine: Engine,
    production_service: DeliverableProductionService,
) -> CreateDeliverableProductionResult:
    """Drive a permitted ``create_deliverable_production`` call against
    the standard happy-path fixture.

    Encapsulated as a helper so the relationship-cardinality and
    immutability tests can share the same setup. The fixture is
    seeded by the test caller before invocation.
    """
    with production_engine.begin() as conn:
        return production_service.create_deliverable_production(
            conn,
            source_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
            produced_deliverable_revision_id=_DELIVERABLE_REVISION_ID,
            target_deliverable_expectation_revision_id=(
                _DELIVERABLE_EXPECTATION_REVISION_ID
            ),
            production_rationale="Produced runbook for milestone one.",
            recording_party_id=_RECORDING_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=production_engine,
            correlation_id="corr-permit",
        )


# ===========================================================================
# Happy-path baseline — confirms wiring and Requirement 27.5 / AD-WS-26
# Relationship cardinality contract.
# ===========================================================================


def test_create_deliverable_production_permits_with_three_relationships(
    production_engine: Engine,
    authorization_service: AuthorizationService,
    production_service: DeliverableProductionService,
) -> None:
    """A permitted Deliverable Production write inserts exactly one
    ``Deliverable_Production_Records`` row, exactly three
    ``Relationships`` rows (one ``Produces`` to the produced
    Deliverable Revision, one ``Addresses`` to the target Deliverable
    Expectation Revision, one ``Relates To`` to the source Work
    Assignment Record with ``semantic_role = 'production_source'``),
    and exactly one consequential audit row inside one transaction
    per Requirements 27.1, 27.5, 27.6, AD-WS-26.

    The happy-path baseline anchors every subsequent rejection test:
    the same fixture, with one input varied to drive a single
    rejection branch, must persist nothing.
    """
    _seed_happy_path(production_engine, authorization_service)

    result = _create_permitted_production(production_engine, production_service)

    assert isinstance(result, CreateDeliverableProductionResult)
    assert _CANONICAL_UUID7.match(result.deliverable_production_id)
    assert _CANONICAL_UUID7.match(result.produces_relationship_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert _CANONICAL_UUID7.match(result.relates_to_relationship_id)
    assert result.source_work_assignment_id == _BOUND_WORK_ASSIGNMENT_ID
    assert result.produced_deliverable_id == _DELIVERABLE_ID
    assert result.produced_deliverable_revision_id == _DELIVERABLE_REVISION_ID
    assert result.target_deliverable_expectation_id == (
        _DELIVERABLE_EXPECTATION_ID
    )
    assert result.target_deliverable_expectation_revision_id == (
        _DELIVERABLE_EXPECTATION_REVISION_ID
    )
    assert result.correlation_id == "corr-permit"

    # Exactly one Deliverable Production Record.
    assert _count(production_engine, "Deliverable_Production_Records") == 1

    # Exactly one consequential audit row participating in the same
    # transaction (Requirement 27.6).
    assert _count_consequential_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 1


class TestRelationshipCardinalityPerAdWs26:
    """Per Requirement 27.5 / AD-WS-26, every Deliverable Production
    write inserts exactly three Relationship rows: one ``Produces``,
    one ``Addresses``, one ``Relates To``.

    Each row carries the AD-WS-26 column shape (source kind, target
    kind, target identifier, and the discriminating ``semantic_role``
    value).
    """

    def test_exactly_one_produces_relationship_to_revision(
        self,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """Per AD-WS-26 row 5: the Deliverable Production Record is
        the source of exactly one ``Produces`` Relationship whose
        target is the produced Deliverable Revision and whose
        ``semantic_role`` is NULL.
        """
        _seed_happy_path(production_engine, authorization_service)

        result = _create_permitted_production(
            production_engine, production_service
        )

        assert _count_relationships_with_source(
            production_engine,
            relationship_type="Produces",
            source_id=result.deliverable_production_id,
            semantic_role_is_null=True,
        ) == 1

        with production_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT relationship_id, source_kind, source_id,
                           source_revision_id, target_kind, target_id,
                           target_revision_id, semantic_role
                    FROM Relationships
                    WHERE relationship_type = 'Produces'
                      AND source_id = :sid
                    """
                ),
                {"sid": result.deliverable_production_id},
            ).mappings().one()
        assert row["relationship_id"] == result.produces_relationship_id
        assert row["source_kind"] == "deliverable_production_record"
        assert row["source_id"] == result.deliverable_production_id
        assert row["source_revision_id"] is None
        assert row["target_kind"] == "deliverable_revision"
        assert row["target_id"] == _DELIVERABLE_ID
        assert row["target_revision_id"] == _DELIVERABLE_REVISION_ID
        assert row["semantic_role"] is None

    def test_exactly_one_addresses_relationship_to_expectation(
        self,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """Per AD-WS-26 row 6: the Deliverable Production Record is
        the source of exactly one ``Addresses`` Relationship whose
        target is the target Deliverable Expectation Revision and
        whose ``semantic_role`` is NULL.
        """
        _seed_happy_path(production_engine, authorization_service)

        result = _create_permitted_production(
            production_engine, production_service
        )

        assert _count_relationships_with_source(
            production_engine,
            relationship_type="Addresses",
            source_id=result.deliverable_production_id,
            semantic_role_is_null=True,
        ) == 1

        with production_engine.connect() as conn:
            row = conn.execute(
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
                {"sid": result.deliverable_production_id},
            ).mappings().one()
        assert row["relationship_id"] == result.addresses_relationship_id
        assert row["source_kind"] == "deliverable_production_record"
        assert row["source_id"] == result.deliverable_production_id
        assert row["source_revision_id"] is None
        assert row["target_kind"] == "deliverable_expectation_revision"
        assert row["target_id"] == _DELIVERABLE_EXPECTATION_ID
        assert row["target_revision_id"] == (
            _DELIVERABLE_EXPECTATION_REVISION_ID
        )
        assert row["semantic_role"] is None

    def test_exactly_one_relates_to_relationship_to_work_assignment(
        self,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """Per AD-WS-26 row 7: the Deliverable Production Record is
        the source of exactly one ``Relates To`` Relationship whose
        target is the source Work Assignment Record and whose
        ``semantic_role`` is the literal ``'production_source'``.

        The ``semantic_role`` discriminator is the value the
        Provenance_Navigator backlink algorithm looks for to return
        the source Work Assignment when given a Production Identity;
        it must match the AD-WS-26 table exactly.
        """
        _seed_happy_path(production_engine, authorization_service)

        result = _create_permitted_production(
            production_engine, production_service
        )

        assert _count_relationships_with_source(
            production_engine,
            relationship_type="Relates To",
            source_id=result.deliverable_production_id,
            semantic_role="production_source",
        ) == 1

        with production_engine.connect() as conn:
            row = conn.execute(
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
                {"sid": result.deliverable_production_id},
            ).mappings().one()
        assert row["relationship_id"] == result.relates_to_relationship_id
        assert row["source_kind"] == "deliverable_production_record"
        assert row["source_id"] == result.deliverable_production_id
        assert row["source_revision_id"] is None
        assert row["target_kind"] == "work_assignment_record"
        assert row["target_id"] == _BOUND_WORK_ASSIGNMENT_ID
        assert row["target_revision_id"] is None
        assert row["semantic_role"] == "production_source"

    def test_total_relationship_count_is_exactly_three(
        self,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """The aggregate cardinality is exactly three Relationship
        rows sourced from the Deliverable Production Record — one of
        each type.

        Catches a regression that doubles a row (for example by
        re-issuing the same INSERT under retry) without changing the
        per-type counts.
        """
        _seed_happy_path(production_engine, authorization_service)

        result = _create_permitted_production(
            production_engine, production_service
        )

        with production_engine.connect() as conn:
            total = int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM Relationships "
                        "WHERE source_id = :sid"
                    ),
                    {"sid": result.deliverable_production_id},
                ).scalar_one()
            )
        assert total == 3


# ===========================================================================
# Requirement 27.3 — cross-Project mismatch rejection.
# ===========================================================================


def test_cross_project_mismatch_rejects_and_persists_nothing(
    production_engine: Engine,
    authorization_service: AuthorizationService,
    production_service: DeliverableProductionService,
) -> None:
    """When the target Deliverable Expectation Revision's
    ``target_project_id`` does not match the Project Identity reached
    from the source Work Assignment's Plan Revision via
    :class:`ProjectResolver`, the request is rejected with
    :class:`DeliverableProductionProjectMismatchError`.

    Per Requirement 27.3 the rejection runs *before* authorization
    evaluation so the deny path never reveals the Project linkage of
    the Work Assignment or the Expectation to an unauthorized
    caller. No Deliverable Production Record / Relationship rows /
    consequential audit row is persisted, and no Denial Record is
    appended either (the project-membership check is a pure
    validation rejection, distinct from the AD-WS-9 / AD-WS-29
    denial paths).
    """
    # Seed the happy-path setup against ``_PROJECT_ID`` (the Work
    # Assignment's Plan Revision → Project chain points there), then
    # *override* the Deliverable Expectation Revision to target a
    # different Project (``_OTHER_PROJECT_ID``) so the
    # project-membership check fails.
    _seed_required_parties(production_engine)
    _assign_contribute_role(authorization_service, production_engine)
    _seed_project(production_engine, project_id=_PROJECT_ID)
    _seed_project(production_engine, project_id=_OTHER_PROJECT_ID)
    _seed_activity_plan(production_engine, project_id=_PROJECT_ID)
    _seed_plan_revision(production_engine)
    _seed_work_assignment(production_engine)
    _seed_deliverable(production_engine)
    # The Expectation lives in ``_OTHER_PROJECT_ID``, which does NOT
    # match the Work Assignment's Plan Revision → Project chain.
    _seed_deliverable_expectation(
        production_engine,
        target_project_id=_OTHER_PROJECT_ID,
    )

    with pytest.raises(
        DeliverableProductionProjectMismatchError
    ) as exc_info:
        with production_engine.begin() as conn:
            production_service.create_deliverable_production(
                conn,
                source_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                produced_deliverable_revision_id=_DELIVERABLE_REVISION_ID,
                target_deliverable_expectation_revision_id=(
                    _DELIVERABLE_EXPECTATION_REVISION_ID
                ),
                production_rationale="Cross-project attempt.",
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=production_engine,
            )

    assert exc_info.value.failed_constraint == (
        "deliverable_expectation_project_mismatch"
    )
    assert exc_info.value.source_work_assignment_id == (
        _BOUND_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.target_deliverable_expectation_revision_id == (
        _DELIVERABLE_EXPECTATION_REVISION_ID
    )
    assert exc_info.value.work_assignment_project_id == _PROJECT_ID
    assert exc_info.value.deliverable_expectation_project_id == (
        _OTHER_PROJECT_ID
    )

    # No Deliverable Production Record / Relationship rows /
    # consequential audit row persisted.
    assert _count(production_engine, "Deliverable_Production_Records") == 0
    assert _count_consequential_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 0

    # The project-membership rejection runs before authorization
    # evaluation, so no Denial Record is appended either (the
    # rejection is a pure validation rejection, distinct from the
    # AD-WS-9 / AD-WS-29 denial paths).
    assert _count_denial_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 0


# ===========================================================================
# Requirement 27.4 — forged-production rejection.
# ===========================================================================


def test_forged_production_rejected_when_originating_wa_id_mismatch(
    production_engine: Engine,
    authorization_service: AuthorizationService,
    production_service: DeliverableProductionService,
) -> None:
    """When the produced Deliverable Revision's persisted
    ``originating_work_assignment_id`` does not match the supplied
    ``source_work_assignment_id``, the request is rejected with
    :class:`DeliverableProductionOriginatingBindingError`.

    Per Requirement 27.4 the originating-binding check rejects
    forged-production attempts where a Contributor names a peer's
    produced Deliverable Revision as their own production. The
    rejection runs *before* authorization evaluation so the deny
    path cannot reveal the produced Revision's authoring chain to
    an unauthorized caller. No Deliverable Production Record /
    Relationship rows / consequential audit row is persisted, and
    no Denial Record is appended either (the originating-binding
    check is a pure validation rejection).

    The test seeds two Work Assignments — both naming the recording
    Party as the assignee — and produces the Deliverable under
    ``_OTHER_WORK_ASSIGNMENT_ID`` while the request names
    ``_BOUND_WORK_ASSIGNMENT_ID`` as the source. The recording Party
    has the ``contribute`` authority on the relevant scope so the
    AD-WS-9 first stage would permit, and is the named assignee on
    both Work Assignments so the AD-WS-29 second stage would also
    permit. The only failure path left is Requirement 27.4's
    originating-binding check.
    """
    _seed_required_parties(production_engine)
    _assign_contribute_role(authorization_service, production_engine)
    _seed_project(production_engine, project_id=_PROJECT_ID)
    _seed_activity_plan(production_engine, project_id=_PROJECT_ID)
    _seed_plan_revision(production_engine)
    # Seed both Work Assignments with the recording Party as the
    # assignee so the AD-WS-29 second stage is not the deciding gate.
    _seed_work_assignment(
        production_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_RECORDING_PARTY_ID,
    )
    _seed_work_assignment(
        production_engine,
        work_assignment_id=_OTHER_WORK_ASSIGNMENT_ID,
        assignee_party_id=_RECORDING_PARTY_ID,
    )
    # Produce the Deliverable under ``_OTHER_WORK_ASSIGNMENT_ID``;
    # the request below names ``_BOUND_WORK_ASSIGNMENT_ID`` as the
    # source, which Requirement 27.4 rejects as a forged-production
    # attempt.
    _seed_deliverable(
        production_engine,
        originating_work_assignment_id=_OTHER_WORK_ASSIGNMENT_ID,
    )
    _seed_deliverable_expectation(production_engine)

    with pytest.raises(
        DeliverableProductionOriginatingBindingError
    ) as exc_info:
        with production_engine.begin() as conn:
            production_service.create_deliverable_production(
                conn,
                source_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                produced_deliverable_revision_id=_DELIVERABLE_REVISION_ID,
                target_deliverable_expectation_revision_id=(
                    _DELIVERABLE_EXPECTATION_REVISION_ID
                ),
                production_rationale="Forged production attempt.",
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=production_engine,
            )

    assert exc_info.value.failed_constraint == (
        "produced_revision_originating_work_assignment_mismatch"
    )
    assert exc_info.value.produced_deliverable_revision_id == (
        _DELIVERABLE_REVISION_ID
    )
    assert exc_info.value.source_work_assignment_id == (
        _BOUND_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.actual_originating_work_assignment_id == (
        _OTHER_WORK_ASSIGNMENT_ID
    )

    # No Deliverable Production Record / Relationship rows /
    # consequential audit row persisted.
    assert _count(production_engine, "Deliverable_Production_Records") == 0
    assert _count_consequential_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 0

    # The originating-binding rejection runs before authorization
    # evaluation, so no Denial Record is appended either.
    assert _count_denial_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 0


# ===========================================================================
# Requirement 32.7 / AD-WS-29 — assignee-binding rejection.
# ===========================================================================


def test_ad_ws_29_rejects_when_recording_party_is_not_named_assignee(
    production_engine: Engine,
    authorization_service: AuthorizationService,
    production_service: DeliverableProductionService,
) -> None:
    """Per AD-WS-29 / Requirement 32.7, the recording Party must be
    the named assignee on the source Work Assignment Record.

    Even when authorization permits the
    ``create.deliverable_production`` action (the recording Party
    holds the ``contribute`` authority over the relevant scope), the
    request is rejected unless the persisted
    ``Work_Assignment_Records.assignee_party_id`` matches the
    supplied ``recording_party_id``. The rejection surfaces as
    :class:`DeliverableProductionAssigneeBindingError` (a subclass of
    :class:`DeliverableProductionAuthorizationError`) with
    ``reason_code = 'no-role-assignment'`` (Slice 1 Requirement
    7.2's denial enumeration) and persists exactly one Denial Record
    in a separate transaction; the caller's transaction rolls back
    so no Deliverable Production Record / Relationship rows /
    consequential audit row is persisted.

    The test seeds a Work Assignment whose ``assignee_party_id`` is a
    Party *other* than the recording Party; the recording Party
    independently holds the ``contribute`` authority on the relevant
    scope so the AD-WS-9 first stage permits, leaving the AD-WS-29
    second stage as the deciding gate. Because the produced
    Deliverable Revision's ``originating_work_assignment_id`` must
    pass the Requirement 27.4 originating-binding check before the
    AD-WS-29 stage runs, the Revision is also seeded under the
    *other* Party's Work Assignment so both Requirement 27.4 and the
    AD-WS-29 stage are exercised through the same realistic
    scenario.
    """
    _seed_required_parties(production_engine)
    # Grant the recording Party the ``contribute`` authority over
    # the relevant scope so the AD-WS-9 first stage permits the
    # action; the AD-WS-29 second stage is then the only gate left.
    _assign_contribute_role(
        authorization_service,
        production_engine,
        party_id=_RECORDING_PARTY_ID,
    )
    _seed_project(production_engine, project_id=_PROJECT_ID)
    _seed_activity_plan(production_engine, project_id=_PROJECT_ID)
    _seed_plan_revision(production_engine)
    # Seed the source Work Assignment with the *other* Party as the
    # named assignee so the AD-WS-29 second stage fails.
    _seed_work_assignment(
        production_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_OTHER_PARTY_ID,
    )
    # Seed the Deliverable under the same source Work Assignment so
    # the Requirement 27.4 originating-binding check passes and the
    # AD-WS-29 stage is reached. The authoring Party on the Revision
    # row is the other Party to keep the seed consistent — note that
    # the AD-WS-29 check on the Production Service compares the
    # *Work Assignment's* assignee against the *recording* Party,
    # not the Revision's authoring Party.
    _seed_deliverable(
        production_engine,
        originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        authoring_party_id=_OTHER_PARTY_ID,
    )
    _seed_deliverable_expectation(production_engine)

    correlation = "corr-deliverable-production-ad-ws-29"
    with pytest.raises(
        DeliverableProductionAssigneeBindingError
    ) as exc_info:
        with production_engine.begin() as conn:
            production_service.create_deliverable_production(
                conn,
                source_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                produced_deliverable_revision_id=_DELIVERABLE_REVISION_ID,
                target_deliverable_expectation_revision_id=(
                    _DELIVERABLE_EXPECTATION_REVISION_ID
                ),
                production_rationale="Production attempt by non-assignee.",
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=production_engine,
                correlation_id=correlation,
            )

    # AD-WS-29 reuses Slice 1 Requirement 7.2's denial enumeration.
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation
    assert exc_info.value.source_work_assignment_id == (
        _BOUND_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.recording_party_id == _RECORDING_PARTY_ID
    assert exc_info.value.actual_assignee_party_id == _OTHER_PARTY_ID

    # Caller's transaction rolled back: no Deliverable Production
    # Record / Relationship rows / consequential audit row persisted.
    assert _count(production_engine, "Deliverable_Production_Records") == 0
    assert _count_consequential_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (AD-WS-9 / Requirement 30.6).
    assert _count_denial_audit_rows(
        production_engine, "create.deliverable_production"
    ) == 1


def test_ad_ws_29_error_is_subclass_of_authorization_error(
    production_engine: Engine,
    authorization_service: AuthorizationService,
    production_service: DeliverableProductionService,
) -> None:
    """The AD-WS-29 assignee-binding rejection surfaces as a subclass
    of :class:`DeliverableProductionAuthorizationError` so callers
    that catch the broader denial path continue to work, while tests
    that need to assert specifically on the AD-WS-29 path can catch
    the narrower :class:`DeliverableProductionAssigneeBindingError`.

    This is a complement to the AD-WS-9 / AD-WS-29 disambiguation
    asserted in
    ``tests/unit/test_execution_work_events.py::
    test_authorization_deny_appends_exactly_one_denial_record``:
    catching the broader exception type must also catch the
    assignee-binding case.
    """
    _seed_happy_path(
        production_engine,
        authorization_service,
        assignee_party_id=_OTHER_PARTY_ID,
    )

    with pytest.raises(
        DeliverableProductionAuthorizationError
    ) as exc_info:
        with production_engine.begin() as conn:
            production_service.create_deliverable_production(
                conn,
                source_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                produced_deliverable_revision_id=_DELIVERABLE_REVISION_ID,
                target_deliverable_expectation_revision_id=(
                    _DELIVERABLE_EXPECTATION_REVISION_ID
                ),
                production_rationale="Production attempt by non-assignee.",
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=production_engine,
            )

    # The narrower :class:`DeliverableProductionAssigneeBindingError`
    # must be observable from the catch site as well.
    assert isinstance(
        exc_info.value, DeliverableProductionAssigneeBindingError
    )


# ===========================================================================
# Requirement 27.7 / AD-WS-27 — immutability rejection of UPDATE / DELETE.
# ===========================================================================


class TestDeliverableProductionImmutability:
    """Per Requirement 27.7 / AD-WS-27, after a Deliverable Production
    Record is finalized the row and its three Relationship rows
    reject every UPDATE / DELETE attempt via the append-only
    triggers installed by tasks 1.2 (Slice 3 Execution_Service tables)
    and Slice 1 ``Relationships`` triggers.

    The triggers raise ``RAISE(ABORT, ...)`` which SQLAlchemy
    surfaces as :class:`sqlalchemy.exc.IntegrityError`.
    """

    def test_update_on_record_rejected_after_persistence(
        self,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """An UPDATE on ``Deliverable_Production_Records`` after commit
        raises ``IntegrityError`` and leaves the row unchanged.
        """
        _seed_happy_path(production_engine, authorization_service)
        result = _create_permitted_production(
            production_engine, production_service
        )

        # Snapshot the row body so a successful UPDATE attempt would
        # be caught by the equality check below.
        with production_engine.connect() as conn:
            before = conn.execute(
                text(
                    "SELECT * FROM Deliverable_Production_Records "
                    "WHERE deliverable_production_id = :id"
                ),
                {"id": result.deliverable_production_id},
            ).mappings().one()

        with production_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Deliverable_Production_Records "
                        "SET production_rationale = 'tampered' "
                        "WHERE deliverable_production_id = :id"
                    ),
                    {"id": result.deliverable_production_id},
                )

        with production_engine.connect() as conn:
            after = conn.execute(
                text(
                    "SELECT * FROM Deliverable_Production_Records "
                    "WHERE deliverable_production_id = :id"
                ),
                {"id": result.deliverable_production_id},
            ).mappings().one()
        assert dict(after) == dict(before)

    def test_delete_on_record_rejected_after_persistence(
        self,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """A DELETE on ``Deliverable_Production_Records`` after commit
        raises ``IntegrityError`` and the row remains in place.
        """
        _seed_happy_path(production_engine, authorization_service)
        result = _create_permitted_production(
            production_engine, production_service
        )

        with production_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "DELETE FROM Deliverable_Production_Records "
                        "WHERE deliverable_production_id = :id"
                    ),
                    {"id": result.deliverable_production_id},
                )

        assert _count(
            production_engine, "Deliverable_Production_Records"
        ) == 1
        with production_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT deliverable_production_id "
                    "FROM Deliverable_Production_Records "
                    "WHERE deliverable_production_id = :id"
                ),
                {"id": result.deliverable_production_id},
            ).scalar_one()
        assert still_present == result.deliverable_production_id

    @pytest.mark.parametrize(
        "relationship_attr,relationship_label",
        [
            ("produces_relationship_id", "Produces"),
            ("addresses_relationship_id", "Addresses"),
            ("relates_to_relationship_id", "Relates To"),
        ],
    )
    def test_update_on_each_relationship_row_rejected(
        self,
        relationship_attr: str,
        relationship_label: str,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """An UPDATE on any of the three ``Relationships`` rows
        produced alongside a Deliverable Production Record raises
        ``IntegrityError``.

        The Slice 1 ``Relationships`` table is one of the AD-WS-4
        immutable tables; its UPDATE rejection trigger fires
        regardless of which Slice 3 source kind the row carries.
        Parameterizing over the three Relationship rows pins
        AD-WS-26 row 5 / 6 / 7 immutability in one test.
        """
        _seed_happy_path(production_engine, authorization_service)
        result = _create_permitted_production(
            production_engine, production_service
        )
        relationship_id = getattr(result, relationship_attr)

        with production_engine.connect() as conn:
            before = conn.execute(
                text(
                    "SELECT * FROM Relationships "
                    "WHERE relationship_id = :id"
                ),
                {"id": relationship_id},
            ).mappings().one()

        with production_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Relationships "
                        "SET semantic_role = 'tampered' "
                        "WHERE relationship_id = :id"
                    ),
                    {"id": relationship_id},
                )

        with production_engine.connect() as conn:
            after = conn.execute(
                text(
                    "SELECT * FROM Relationships "
                    "WHERE relationship_id = :id"
                ),
                {"id": relationship_id},
            ).mappings().one()
        assert dict(after) == dict(before), (
            f"{relationship_label} Relationship row mutated through "
            "UPDATE despite the AD-WS-4 append-only trigger."
        )

    @pytest.mark.parametrize(
        "relationship_attr,relationship_label",
        [
            ("produces_relationship_id", "Produces"),
            ("addresses_relationship_id", "Addresses"),
            ("relates_to_relationship_id", "Relates To"),
        ],
    )
    def test_delete_on_each_relationship_row_rejected(
        self,
        relationship_attr: str,
        relationship_label: str,
        production_engine: Engine,
        authorization_service: AuthorizationService,
        production_service: DeliverableProductionService,
    ) -> None:
        """A DELETE on any of the three ``Relationships`` rows
        produced alongside a Deliverable Production Record raises
        ``IntegrityError`` and the row survives.

        Parameterized over the three Relationship rows so AD-WS-26
        row 5 / 6 / 7 deletion-rejection is asserted in one test.
        """
        _seed_happy_path(production_engine, authorization_service)
        result = _create_permitted_production(
            production_engine, production_service
        )
        relationship_id = getattr(result, relationship_attr)

        with production_engine.connect() as conn, pytest.raises(
            IntegrityError
        ):
            with conn.begin():
                conn.execute(
                    text(
                        "DELETE FROM Relationships "
                        "WHERE relationship_id = :id"
                    ),
                    {"id": relationship_id},
                )

        with production_engine.connect() as conn:
            still_present = conn.execute(
                text(
                    "SELECT relationship_id FROM Relationships "
                    "WHERE relationship_id = :id"
                ),
                {"id": relationship_id},
            ).scalar_one()
        assert still_present == relationship_id, (
            f"{relationship_label} Relationship row vanished through "
            "DELETE despite the AD-WS-4 append-only trigger."
        )
