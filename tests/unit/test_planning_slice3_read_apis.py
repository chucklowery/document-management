"""Unit tests for the additive Slice 3 Planning read APIs (task 2.3).

Pins the contract established by task 2.1 (additive
:meth:`PlanRevisionService.get_plan_revision`) and task 2.2 (additive
:meth:`DeliverableExpectationService.get_revision` and the new
:class:`ProjectResolver`) per the third walking slice's design
§"Components and Interfaces" — AD-WS-30 ("Approved-Plan resolution
uses the existing Planning_Service read API").

The Execution_Service modules built later in the slice rely on these
three reads to satisfy Requirements 23.2 / 23.4 (Work Assignment must
target an approved Plan Revision within the requesting Party's scope)
and 27.3 (Deliverable Production must target a Deliverable Expectation
that belongs to the same Project as the source Work Assignment's
Approved Plan Revision). These tests pin the behavior of those reads
in isolation so a regression in any one of them surfaces here before
it cascades into a Slice 3 service test.

Covered behaviors
=================

* **23.2 / 23.4 — ``get_plan_revision``.** Resolves a ``Plan_Revisions``
  row by Identity and returns a frozen :class:`PlanRevisionRow`
  snapshot. The ``lifecycle_state`` column is returned verbatim for
  both ``draft`` and ``approved`` Plan Revisions so the Work Assignment
  Service can compare it against the literal ``'approved'`` for
  Requirement 23.2. ``activity_plan_id`` and ``applicable_scope`` are
  returned so the Work Assignment Service can route the row to
  :class:`ProjectResolver` and verify Requirement 23.4's scope
  containment. The method is read-only — no row is inserted, updated,
  or deleted and no consequential audit row appears.
* **27.3 — ``get_revision``.** Returns the target Project Identity (the
  ``Deliverable_Expectation_Revisions.target_project_id`` column) so
  the Deliverable Production Service can compare it against the
  Project Identity reached from the source Work Assignment's Plan
  Revision via :class:`ProjectResolver`. An unresolvable Revision
  Identity raises
  :class:`DeliverableExpectationRevisionNotResolvableError` carrying
  the stable ``failed_constraint`` discriminator.
* **27.3 — ``ProjectResolver.resolve_project``.** Walks
  ``Plan Revision → Activity Plan → Project`` via one indexed JOIN
  and returns the Project Identity. An unresolvable Plan Revision
  raises a structured :class:`PlanRevisionNotResolvableError` (the
  exception name preserved across the response-shaping boundary) so
  callers can branch on ``failed_constraint`` without parsing the
  message text. The same exception is raised when the matching Plan
  Revision references an Activity Plan that no longer exists — the
  resolver collapses both join-miss cases into one well-defined
  failure mode (defense in depth against ``foreign_keys=OFF``).

The test style mirrors ``tests/unit/test_planning_plan_revisions.py``
and ``tests/unit/test_planning_deliverable_expectations.py``: a
per-test engine carrying both the Slice 1 and Slice 2 schemas, direct
INSERTs to seed the dependency rows (each Slice 1 / Slice 2 service
under test exists in its own module test, so the read APIs do not
need to drive the write services to seed a row), and explicit
assertions against the frozen value-object shapes returned by each
read.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import (
    PlanRevisionNotResolvableError,
    ProjectResolver,
)
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationRevisionNotResolvableError,
    DeliverableExpectationRevisionRow,
    DeliverableExpectationService,
)
from walking_slice.planning.plan_revisions import (
    PlanRevisionRow,
    PlanRevisionService,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Identifiers and fixed seed values.
#
# Slice 2 / Slice 3 identifiers are UUIDv7 strings in canonical hex form;
# fixed values keep the tests fully deterministic so a failure points at
# the exact row that was misread rather than at a randomised input.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_OTHER_PROJECT_ID = "00000000-0000-7000-8000-000000c00011"
_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_OTHER_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00021"
_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00030"
_APPROVED_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00031"
_OTHER_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00032"
_UNRESOLVABLE_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000deadbe01"
_DANGLING_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000deadbe02"
_DANGLING_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-0000deadbe03"

_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-000000d00001"
_DELIVERABLE_EXPECTATION_REVISION_ID = "00000000-0000-7000-8000-000000d00002"
_UNRESOLVABLE_REVISION_ID = "00000000-0000-7000-8000-0000deadbe10"

_SCOPE_PILOT = "pilot/team-a"
_SCOPE_PRODUCTION = "production/team-b"
_TS_FIXED = "2026-01-01T00:00:00.000Z"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Per-test engine carrying both Slice 1 and Slice 2 schemas.

    ``create_schema`` installs Slice 1 plus the additive
    ``Identifier_Registry.resource_kind`` and
    ``Relationships.semantic_role`` columns; ``create_planning_schema``
    installs every Slice 2 table, index, and append-only trigger. No
    disclosure seeding is required: the three read APIs under test do
    not consult the disclosure registry (they are strictly read-only
    primary-key / join lookups).
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def deliverable_expectation_service(
    clock,
    identity_service,
    audit_log,
    authorization_service,
) -> DeliverableExpectationService:
    """A :class:`DeliverableExpectationService` wired with the per-test
    collaborators.

    The :meth:`get_revision` method under test is a pure read; the
    collaborator wiring is supplied so the service can be constructed
    without raising, mirroring the way the Slice 3 Execution_Service
    will instantiate the service in production.
    """
    return DeliverableExpectationService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Each read API under test resolves rows in the Slice 2 schema. The
# write surfaces (PlanRevisionService.create_plan_revision,
# DeliverableExpectationService.create_deliverable_expectation) have
# their own dedicated test modules, so the rows here are seeded
# directly via INSERT — matching the pattern in
# ``tests/unit/test_planning_immutability.py`` and
# ``tests/unit/test_planning_plan_revisions.py``. Direct INSERTs are
# permitted by the AD-WS-19 lifecycle trigger (which fires on UPDATE
# only), so an ``approved`` Plan Revision can be inserted in one
# statement without driving the Plan Approval transaction.
# ---------------------------------------------------------------------------


def _seed_party(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Parties (party_id, kind, display_name, "
                "created_at) VALUES (:pid, 'person', 'Planner', :ts)"
            ),
            {"pid": _PARTY_ID, "ts": _TS_FIXED},
        )


def _seed_objective(engine: Engine) -> None:
    """Seed one Objective Resource header row.

    Required as the FK target of ``Project_Revisions.target_objective_id``
    on the Slice 2 schema; the Plan Revision and Deliverable Expectation
    rows under test do not consult the Objective directly, but a Project
    Revision (created by the Deliverable Expectation seed) does.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _TS_FIXED},
        )


def _seed_project(engine: Engine, project_id: str = _PROJECT_ID) -> None:
    """Seed one Project Resource header row.

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
    title: str = "Mesh Rollout Activities",
) -> None:
    """Seed one Activity Plan row pointing at ``project_id``.

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
                ) VALUES (:aid, :pid, :title, :party, :scope, :ts)
                """
            ),
            {
                "aid": activity_plan_id,
                "pid": project_id,
                "title": title,
                "party": _PARTY_ID,
                "scope": _SCOPE_PILOT,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    lifecycle_state: str = "draft",
    applicable_scope: str = _SCOPE_PILOT,
) -> None:
    """Insert one ``Plan_Revisions`` row in the requested lifecycle state.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so the row
    can be inserted directly with ``lifecycle_state = 'approved'``
    without driving the Plan Approval transaction or setting the
    ``walking_slice.plan_approval_in_progress`` session pragma.
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
                "party": _PARTY_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_expectation_revision(
    engine: Engine,
    *,
    deliverable_expectation_id: str = _DELIVERABLE_EXPECTATION_ID,
    deliverable_expectation_revision_id: str = (
        _DELIVERABLE_EXPECTATION_REVISION_ID
    ),
    target_project_id: str = _PROJECT_ID,
    name: str = "Mesh Operations Runbook",
    deliverable_kind: str = "Document",
) -> None:
    """Insert one Deliverable Expectation header + first Revision row.

    The Slice 2 schema requires the header row in
    ``Deliverable_Expectations`` to exist before the Revision row in
    ``Deliverable_Expectation_Revisions`` can be inserted; both are
    seeded here so :meth:`DeliverableExpectationService.get_revision`
    can read the Revision in one SELECT.
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
                    :rev, :did, NULL, :pid, :name, NULL,
                    :kind, NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": deliverable_expectation_revision_id,
                "did": deliverable_expectation_id,
                "pid": target_project_id,
                "name": name,
                "kind": deliverable_kind,
                "party": _PARTY_ID,
                "scope": _SCOPE_PILOT,
                "ts": _TS_FIXED,
            },
        )


def _count(engine: Engine, table: str) -> int:
    """Row-count helper for the post-read no-side-effect assertions.

    The read APIs under test must not insert, update, or delete any
    row. Counting before and after each read gives a cheap structural
    proof that the read is side-effect free.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


# ===========================================================================
# PlanRevisionService.get_plan_revision (Requirements 23.2 / 23.4)
# ===========================================================================


class TestGetPlanRevisionReturnsLifecycleState:
    """The Work Assignment Service compares the returned
    ``lifecycle_state`` against the literal ``'approved'`` to enforce
    Requirement 23.2.

    Two cases pin the contract: a ``draft`` Plan Revision and an
    ``approved`` Plan Revision must both round-trip through
    :meth:`PlanRevisionService.get_plan_revision`, returning the
    matching string verbatim.
    """

    def test_get_plan_revision_returns_draft_lifecycle_state(
        self,
        planning_engine: Engine,
    ) -> None:
        """A Plan Revision inserted with ``lifecycle_state = 'draft'`` is
        returned with the same literal on the
        :class:`PlanRevisionRow`."""
        _seed_party(planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with planning_engine.connect() as conn:
            row = PlanRevisionService.get_plan_revision(
                conn, _DRAFT_PLAN_REVISION_ID
            )

        assert isinstance(row, PlanRevisionRow)
        assert row.plan_revision_id == _DRAFT_PLAN_REVISION_ID
        assert row.lifecycle_state == "draft"
        assert row.activity_plan_id == _ACTIVITY_PLAN_ID
        assert row.applicable_scope == _SCOPE_PILOT

    def test_get_plan_revision_returns_approved_lifecycle_state(
        self,
        planning_engine: Engine,
    ) -> None:
        """A Plan Revision inserted with ``lifecycle_state = 'approved'``
        is returned with the same literal — the value Work Assignment
        Service compares against per Requirement 23.2.

        The row is inserted directly with the approved lifecycle state
        because the AD-WS-19 lifecycle trigger only fires on UPDATE,
        and the Plan Approval write path is exercised by its own test
        module.
        """
        _seed_party(planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision(
            planning_engine,
            plan_revision_id=_APPROVED_PLAN_REVISION_ID,
            lifecycle_state="approved",
            applicable_scope=_SCOPE_PRODUCTION,
        )

        with planning_engine.connect() as conn:
            row = PlanRevisionService.get_plan_revision(
                conn, _APPROVED_PLAN_REVISION_ID
            )

        assert isinstance(row, PlanRevisionRow)
        assert row.plan_revision_id == _APPROVED_PLAN_REVISION_ID
        assert row.lifecycle_state == "approved"
        assert row.activity_plan_id == _ACTIVITY_PLAN_ID
        assert row.applicable_scope == _SCOPE_PRODUCTION


def test_get_plan_revision_returns_none_for_unresolvable_identifier(
    planning_engine: Engine,
) -> None:
    """Per AD-WS-30 the read returns ``None`` (not an exception) when
    the Plan Revision Identity does not resolve.

    The Work Assignment Service branches on the ``None`` return to
    surface Requirement 23.4's "unresolvable target Plan Revision"
    rejection without leaking existence of any Plan Revision to an
    unauthorized caller.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)

    with planning_engine.connect() as conn:
        row = PlanRevisionService.get_plan_revision(
            conn, _UNRESOLVABLE_PLAN_REVISION_ID
        )

    assert row is None


def test_get_plan_revision_is_read_only(
    planning_engine: Engine,
) -> None:
    """The read API issues exactly one SELECT and no INSERT / UPDATE /
    DELETE — Requirement 40.1 (Slice 3 reads do not mutate Slice 1 /
    Slice 2 rows) and AD-WS-30 (the read is strictly read-only).

    Row counts on every Slice 2 table read by the API are equal before
    and after the call; the consequential ``Audit_Records`` count is
    unchanged because a read API does not append an audit row.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    _seed_plan_revision(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
        lifecycle_state="draft",
    )

    before = {
        "Plan_Revisions": _count(planning_engine, "Plan_Revisions"),
        "Activity_Plans": _count(planning_engine, "Activity_Plans"),
        "Projects": _count(planning_engine, "Projects"),
        "Audit_Records": _count(planning_engine, "Audit_Records"),
        "Relationships": _count(planning_engine, "Relationships"),
    }

    with planning_engine.connect() as conn:
        PlanRevisionService.get_plan_revision(conn, _DRAFT_PLAN_REVISION_ID)
        PlanRevisionService.get_plan_revision(
            conn, _UNRESOLVABLE_PLAN_REVISION_ID
        )

    after = {
        "Plan_Revisions": _count(planning_engine, "Plan_Revisions"),
        "Activity_Plans": _count(planning_engine, "Activity_Plans"),
        "Projects": _count(planning_engine, "Projects"),
        "Audit_Records": _count(planning_engine, "Audit_Records"),
        "Relationships": _count(planning_engine, "Relationships"),
    }
    assert before == after


# ===========================================================================
# DeliverableExpectationService.get_revision (Requirement 27.3)
# ===========================================================================


def test_get_revision_returns_target_project_identity(
    planning_engine: Engine,
    deliverable_expectation_service: DeliverableExpectationService,
) -> None:
    """:meth:`DeliverableExpectationService.get_revision` returns a
    frozen :class:`DeliverableExpectationRevisionRow` whose
    ``target_project_id`` matches the seeded value verbatim.

    The Deliverable Production Service compares this Project Identity
    against the Project Identity reached from the source Work
    Assignment's Plan Revision via :class:`ProjectResolver` to satisfy
    Requirement 27.3's project-membership check; any drift between
    the seeded row and the returned value would silently break the
    cross-Project comparison.
    """
    _seed_party(planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine)
    _seed_deliverable_expectation_revision(
        planning_engine,
        target_project_id=_PROJECT_ID,
        name="Mesh Operations Runbook",
        deliverable_kind="Document",
    )

    with planning_engine.connect() as conn:
        row = deliverable_expectation_service.get_revision(
            conn,
            deliverable_expectation_revision_id=(
                _DELIVERABLE_EXPECTATION_REVISION_ID
            ),
        )

    assert isinstance(row, DeliverableExpectationRevisionRow)
    assert row.deliverable_expectation_revision_id == (
        _DELIVERABLE_EXPECTATION_REVISION_ID
    )
    assert row.deliverable_expectation_id == _DELIVERABLE_EXPECTATION_ID
    assert row.target_project_id == _PROJECT_ID
    assert row.name == "Mesh Operations Runbook"
    assert row.deliverable_kind == "Document"
    assert row.recorded_at == _TS_FIXED


def test_get_revision_returns_distinct_project_for_distinct_revision(
    planning_engine: Engine,
    deliverable_expectation_service: DeliverableExpectationService,
) -> None:
    """Two Deliverable Expectation Revisions targeting different
    Projects must each round-trip with their own ``target_project_id``.

    This guards against any stray caching or shared-state mistake in
    the read API: looking up Revision A must return Project A and
    looking up Revision B must return Project B, regardless of which
    is queried first.
    """
    _seed_party(planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine, project_id=_PROJECT_ID)
    _seed_project(planning_engine, project_id=_OTHER_PROJECT_ID)
    _seed_deliverable_expectation_revision(
        planning_engine,
        deliverable_expectation_id=_DELIVERABLE_EXPECTATION_ID,
        deliverable_expectation_revision_id=(
            _DELIVERABLE_EXPECTATION_REVISION_ID
        ),
        target_project_id=_PROJECT_ID,
        name="Runbook A",
    )
    second_expectation_id = "00000000-0000-7000-8000-000000d00011"
    second_revision_id = "00000000-0000-7000-8000-000000d00012"
    _seed_deliverable_expectation_revision(
        planning_engine,
        deliverable_expectation_id=second_expectation_id,
        deliverable_expectation_revision_id=second_revision_id,
        target_project_id=_OTHER_PROJECT_ID,
        name="Runbook B",
        deliverable_kind="Artifact",
    )

    with planning_engine.connect() as conn:
        row_a = deliverable_expectation_service.get_revision(
            conn,
            deliverable_expectation_revision_id=(
                _DELIVERABLE_EXPECTATION_REVISION_ID
            ),
        )
        row_b = deliverable_expectation_service.get_revision(
            conn,
            deliverable_expectation_revision_id=second_revision_id,
        )

    assert row_a.target_project_id == _PROJECT_ID
    assert row_a.name == "Runbook A"
    assert row_b.target_project_id == _OTHER_PROJECT_ID
    assert row_b.name == "Runbook B"
    assert row_b.deliverable_kind == "Artifact"


def test_get_revision_raises_structured_error_when_revision_unresolvable(
    planning_engine: Engine,
    deliverable_expectation_service: DeliverableExpectationService,
) -> None:
    """An unresolvable Revision Identity raises
    :class:`DeliverableExpectationRevisionNotResolvableError` carrying
    the offending identifier and the stable
    ``failed_constraint`` discriminator
    ``"deliverable_expectation_revision_not_resolvable"``.

    Slice 3 callers branch on the discriminator to decide whether to
    surface a structured 400 / 404 response or fold the error into the
    AD-WS-9 indistinguishable denial path.
    """
    _seed_party(planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine)

    with planning_engine.connect() as conn:
        with pytest.raises(
            DeliverableExpectationRevisionNotResolvableError
        ) as exc_info:
            deliverable_expectation_service.get_revision(
                conn,
                deliverable_expectation_revision_id=(
                    _UNRESOLVABLE_REVISION_ID
                ),
            )

    assert exc_info.value.deliverable_expectation_revision_id == (
        _UNRESOLVABLE_REVISION_ID
    )
    assert exc_info.value.failed_constraint == (
        "deliverable_expectation_revision_not_resolvable"
    )


# ===========================================================================
# ProjectResolver.resolve_project (Requirement 27.3, AD-WS-30)
# ===========================================================================


def test_resolve_project_returns_owning_project_for_known_plan_revision(
    planning_engine: Engine,
) -> None:
    """:meth:`ProjectResolver.resolve_project` walks ``Plan Revision →
    Activity Plan → Project`` and returns the owning Project Identity.

    This is the project-membership lookup the Deliverable Production
    Service uses to satisfy Requirement 27.3. The seeded Plan
    Revision targets ``_ACTIVITY_PLAN_ID``, which in turn targets
    ``_PROJECT_ID``; the resolver must return ``_PROJECT_ID`` verbatim.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    _seed_plan_revision(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
    )
    resolver = ProjectResolver()

    with planning_engine.connect() as conn:
        project_id = resolver.resolve_project(
            conn, plan_revision_id=_DRAFT_PLAN_REVISION_ID
        )

    assert project_id == _PROJECT_ID


def test_resolve_project_distinguishes_plan_revisions_in_different_projects(
    planning_engine: Engine,
) -> None:
    """Two Plan Revisions whose Activity Plans target different Projects
    each resolve to their own Project Identity.

    The Deliverable Production Service relies on this discrimination
    to detect cross-Project Deliverable-Expectation references
    (Requirement 27.3) — if the resolver collapsed the two Plan
    Revisions into the same Project the cross-Project rejection path
    would silently break.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine, project_id=_PROJECT_ID)
    _seed_project(planning_engine, project_id=_OTHER_PROJECT_ID)
    _seed_activity_plan(
        planning_engine,
        activity_plan_id=_ACTIVITY_PLAN_ID,
        project_id=_PROJECT_ID,
    )
    _seed_activity_plan(
        planning_engine,
        activity_plan_id=_OTHER_ACTIVITY_PLAN_ID,
        project_id=_OTHER_PROJECT_ID,
        title="Other Plan Activities",
    )
    _seed_plan_revision(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
        activity_plan_id=_ACTIVITY_PLAN_ID,
    )
    _seed_plan_revision(
        planning_engine,
        plan_revision_id=_OTHER_PLAN_REVISION_ID,
        activity_plan_id=_OTHER_ACTIVITY_PLAN_ID,
        lifecycle_state="approved",
    )
    resolver = ProjectResolver()

    with planning_engine.connect() as conn:
        first = resolver.resolve_project(
            conn, plan_revision_id=_DRAFT_PLAN_REVISION_ID
        )
        second = resolver.resolve_project(
            conn, plan_revision_id=_OTHER_PLAN_REVISION_ID
        )

    assert first == _PROJECT_ID
    assert second == _OTHER_PROJECT_ID


def test_resolve_project_raises_structured_error_for_unresolvable_plan_revision(
    planning_engine: Engine,
) -> None:
    """An unresolvable Plan Revision Identity raises
    :class:`PlanRevisionNotResolvableError` carrying the offending
    identifier and the stable ``failed_constraint`` discriminator
    ``"plan_revision_not_resolvable"``.

    The exception is the structured rejection path AD-WS-30 / the task
    brief calls for: the Slice 3 caller maps the discriminator into
    its own response shape without parsing the message text.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    resolver = ProjectResolver()

    with planning_engine.connect() as conn:
        with pytest.raises(PlanRevisionNotResolvableError) as exc_info:
            resolver.resolve_project(
                conn,
                plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
            )

    assert exc_info.value.plan_revision_id == _UNRESOLVABLE_PLAN_REVISION_ID
    assert exc_info.value.failed_constraint == "plan_revision_not_resolvable"


def test_resolve_project_raises_structured_error_when_activity_plan_dangling(
    planning_engine: Engine,
) -> None:
    """When the matching Plan Revision row references an Activity Plan
    that does not exist, the JOIN returns zero rows and the resolver
    raises :class:`PlanRevisionNotResolvableError` — collapsing the
    "missing Plan Revision" and "missing Activity Plan" cases into one
    well-defined failure mode (defense in depth against an environment
    where ``PRAGMA foreign_keys = OFF`` allowed a dangling FK to be
    written).

    The row is inserted with ``PRAGMA foreign_keys = OFF`` on a
    dedicated connection so the dangling FK is allowed; every other
    connection inherits the engine-level ``foreign_keys=ON`` pragma
    from ``conftest.py``.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine)

    # Disable FK enforcement on a raw DBAPI connection so the
    # dangling ``activity_plan_id`` can be written. The pragma is
    # connection-scoped and SQLAlchemy's auto-begin would otherwise
    # promote the ``PRAGMA`` call into a transaction before the FK
    # toggle takes effect; using ``raw_connection()`` sidesteps the
    # auto-begin and lets us issue the pragma before any SQL begins
    # a transaction. Every other test connection retains the
    # engine-level ``foreign_keys=ON`` from ``conftest.py``.
    raw = planning_engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys = OFF")
            cursor.execute(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    ?, ?, NULL, 'draft', 'Phase 1',
                    '[]', '[]', NULL, ?, ?, ?
                )
                """,
                (
                    _DANGLING_PLAN_REVISION_ID,
                    _DANGLING_ACTIVITY_PLAN_ID,
                    _PARTY_ID,
                    _SCOPE_PILOT,
                    _TS_FIXED,
                ),
            )
            raw.commit()
        finally:
            cursor.close()
    finally:
        raw.close()

    resolver = ProjectResolver()
    with planning_engine.connect() as conn:
        with pytest.raises(PlanRevisionNotResolvableError) as exc_info:
            resolver.resolve_project(
                conn, plan_revision_id=_DANGLING_PLAN_REVISION_ID
            )

    assert exc_info.value.plan_revision_id == _DANGLING_PLAN_REVISION_ID
    assert exc_info.value.failed_constraint == "plan_revision_not_resolvable"


def test_resolve_project_is_read_only(
    planning_engine: Engine,
) -> None:
    """The resolver issues exactly one SELECT — no INSERT, UPDATE,
    DELETE, or audit append — per AD-WS-30 / Requirement 40.1.

    Row counts on every table the resolver touches (``Plan_Revisions``,
    ``Activity_Plans``, ``Projects``) and on ``Audit_Records`` are
    equal before and after a successful call and an unresolvable call.
    """
    _seed_party(planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    _seed_plan_revision(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
    )
    resolver = ProjectResolver()

    before = {
        "Plan_Revisions": _count(planning_engine, "Plan_Revisions"),
        "Activity_Plans": _count(planning_engine, "Activity_Plans"),
        "Projects": _count(planning_engine, "Projects"),
        "Audit_Records": _count(planning_engine, "Audit_Records"),
    }

    with planning_engine.connect() as conn:
        resolver.resolve_project(
            conn, plan_revision_id=_DRAFT_PLAN_REVISION_ID
        )
        with pytest.raises(PlanRevisionNotResolvableError):
            resolver.resolve_project(
                conn,
                plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
            )

    after = {
        "Plan_Revisions": _count(planning_engine, "Plan_Revisions"),
        "Activity_Plans": _count(planning_engine, "Activity_Plans"),
        "Projects": _count(planning_engine, "Projects"),
        "Audit_Records": _count(planning_engine, "Audit_Records"),
    }
    assert before == after
