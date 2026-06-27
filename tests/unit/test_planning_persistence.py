"""Unit tests for :mod:`walking_slice.planning._persistence` (task 1.5).

These tests pin the contract established in
``.kiro/specs/second-walking-slice/design.md`` §"Data Models — Schema
Additions", AD-WS-19 (per-Resource-kind tables with append-only triggers),
and AD-WS-20 (Plan Approval atomic lifecycle transition):

- Every Slice 2 table in
  :data:`walking_slice.planning._persistence.PLANNING_IMMUTABLE_TABLES`
  rejects ``UPDATE`` and ``DELETE`` per AD-WS-4 / AD-WS-19
  (Requirements 9.4, 12.5, 13.6, 20.4).
- ``Plan_Revisions`` rejects every ``DELETE`` and rejects every
  ``UPDATE`` unless the only change is ``lifecycle_state`` transitioning
  from ``'draft'`` to ``'approved'`` AND the connection-scoped pragma
  ``walking_slice.plan_approval_in_progress`` is set
  (AD-WS-19 / AD-WS-20, Requirement 9.4 / 20.4).
- The Slice 1 contract for ``Relationships.semantic_role = NULL`` rows
  and ``Identifier_Registry.resource_kind = NULL`` rows remains intact
  after the Slice 2 schema has been installed (Requirement 19.2 / 19.4).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.disclosure import seed as seed_disclosure
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import (
    PLANNING_IMMUTABLE_TABLES,
    clear_plan_approval_in_progress,
    create_planning_schema,
    set_plan_approval_in_progress,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Identifiers — fixed per-test values so the row contents are predictable.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_OBJECTIVE_ID = "00000000-0000-7000-8000-000000000100"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000000101"
_INTENDED_OUTCOME_ID = "00000000-0000-7000-8000-000000000110"
_INTENDED_OUTCOME_REV_ID = "00000000-0000-7000-8000-000000000111"
_PROJECT_ID = "00000000-0000-7000-8000-000000000120"
_PROJECT_REV_ID = "00000000-0000-7000-8000-000000000121"
_DELIVERABLE_ID = "00000000-0000-7000-8000-000000000130"
_DELIVERABLE_REV_ID = "00000000-0000-7000-8000-000000000131"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000000140"
_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000000150"
_PLAN_REVIEW_ID = "00000000-0000-7000-8000-000000000160"
_PLAN_REVIEW_REV_ID = "00000000-0000-7000-8000-000000000161"
_PLAN_APPROVAL_ID = "00000000-0000-7000-8000-000000000170"
_DECISION_ID = "00000000-0000-7000-8000-000000000180"
_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-000000000181"
_SCOPE = "pilot/team-a"
_TS = "2026-01-01T00:00:00.000+00:00"


# ---------------------------------------------------------------------------
# Schema fixture (Slice 1 + Slice 2 + default disclosure policy).
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Per-test engine carrying both Slice 1 and Slice 2 schemas.

    ``create_schema`` installs the Slice 1 tables (and the additive
    ``Relationships.semantic_role`` / ``Identifier_Registry.resource_kind``
    columns added by task 1.2). ``create_planning_schema`` installs every
    Slice 2 table, index, and append-only trigger; it also registers the
    ``connect`` listener that recreates the connection-private temp table
    and the ``Plan_Revisions`` lifecycle trigger on every new DBAPI
    connection. ``seed_disclosure`` seeds the ``slice-default-2026`` row
    so the ``Disclosure_Policy_Coverage`` foreign key resolves.
    """
    create_schema(engine)
    create_planning_schema(engine)
    seed_disclosure(engine)
    return engine


# ---------------------------------------------------------------------------
# Seed helpers — insert just enough per table to exercise UPDATE/DELETE.
# ---------------------------------------------------------------------------


def _seed_party(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Planner', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _TS},
    )


def _seed_objective(conn) -> None:
    conn.execute(
        text("INSERT INTO Objectives (objective_id, created_at) VALUES (:oid, :ts)"),
        {"oid": _OBJECTIVE_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Objective_Revisions (
                objective_revision_id, objective_id, parent_revision_id,
                statement, rationale, target_decision_id, authoring_party_id,
                applicable_scope, recorded_at
            ) VALUES (
                :rev, :oid, NULL, 'Adopt service-mesh telemetry.',
                'Because we said so.', :did, :pid, :scope, :ts
            )
            """
        ),
        {
            "rev": _OBJECTIVE_REV_ID,
            "oid": _OBJECTIVE_ID,
            "did": _DECISION_ID,
            "pid": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_intended_outcome(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO Intended_Outcomes (intended_outcome_id, created_at) "
            "VALUES (:iid, :ts)"
        ),
        {"iid": _INTENDED_OUTCOME_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Intended_Outcome_Revisions (
                intended_outcome_revision_id, intended_outcome_id,
                parent_revision_id, outcome_kind, target_objective_id,
                success_condition, observation_window, attribution_assumption,
                authoring_party_id, applicable_scope, recorded_at
            ) VALUES (
                :rev, :iid, NULL, 'intended', :oid,
                'p95 latency below 200ms', '90 days', 'no other rollouts',
                :pid, :scope, :ts
            )
            """
        ),
        {
            "rev": _INTENDED_OUTCOME_REV_ID,
            "iid": _INTENDED_OUTCOME_ID,
            "oid": _OBJECTIVE_ID,
            "pid": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_project(conn) -> None:
    conn.execute(
        text("INSERT INTO Projects (project_id, created_at) VALUES (:pid_, :ts)"),
        {"pid_": _PROJECT_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Project_Revisions (
                project_revision_id, project_id, parent_revision_id, name,
                summary, target_objective_id, planned_start_date,
                planned_end_date, authoring_party_id, applicable_scope,
                recorded_at
            ) VALUES (
                :rev, :pid_, NULL, 'Mesh Rollout',
                'Roll out the service mesh.', :oid,
                '2026-01-15', '2026-06-30', :party, :scope, :ts
            )
            """
        ),
        {
            "rev": _PROJECT_REV_ID,
            "pid_": _PROJECT_ID,
            "oid": _OBJECTIVE_ID,
            "party": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_deliverable_expectation(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO Deliverable_Expectations "
            "(deliverable_expectation_id, created_at) VALUES (:did, :ts)"
        ),
        {"did": _DELIVERABLE_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Deliverable_Expectation_Revisions (
                deliverable_expectation_revision_id, deliverable_expectation_id,
                parent_revision_id, target_project_id, name, description,
                deliverable_kind, acceptance_criteria, authoring_party_id,
                applicable_scope, recorded_at
            ) VALUES (
                :rev, :did, NULL, :pid_, 'Runbook',
                'Operational runbook for the mesh.', 'Document',
                'Approved by SRE lead.', :party, :scope, :ts
            )
            """
        ),
        {
            "rev": _DELIVERABLE_REV_ID,
            "did": _DELIVERABLE_ID,
            "pid_": _PROJECT_ID,
            "party": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_activity_plan(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Activity_Plans (
                activity_plan_id, target_project_id, title,
                authoring_party_id, applicable_scope, recorded_at
            ) VALUES (
                :aid, :pid_, 'Mesh Rollout — Phase 1', :party, :scope, :ts
            )
            """
        ),
        {
            "aid": _ACTIVITY_PLAN_ID,
            "pid_": _PROJECT_ID,
            "party": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_plan_revision(conn, lifecycle: str = "draft") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Plan_Revisions (
                plan_revision_id, activity_plan_id, predecessor_revision_id,
                lifecycle_state, planned_scope,
                deliverable_expectation_refs_json, planning_assumptions_json,
                ordering_rationale, authoring_party_id, applicable_scope,
                recorded_at
            ) VALUES (
                :rev, :aid, NULL, :state, 'Phase 1 scope', '[]', '[]',
                'Sequenced because dependencies.', :party, :scope, :ts
            )
            """
        ),
        {
            "rev": _PLAN_REVISION_ID,
            "aid": _ACTIVITY_PLAN_ID,
            "state": lifecycle,
            "party": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_plan_review(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO Plan_Reviews (plan_review_id, created_at) "
            "VALUES (:rid, :ts)"
        ),
        {"rid": _PLAN_REVIEW_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Plan_Review_Revisions (
                plan_review_revision_id, plan_review_id,
                target_plan_revision_id, outcome, rationale,
                reviewing_party_id, authority_basis_type, authority_basis_id,
                applicable_scope, recorded_at
            ) VALUES (
                :rev, :rid, :prev, 'Endorse',
                'Looks good — sequencing matches the rollout playbook.',
                :party, 'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "rev": _PLAN_REVIEW_REV_ID,
            "rid": _PLAN_REVIEW_ID,
            "prev": _PLAN_REVISION_ID,
            "party": _PARTY_ID,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_plan_approval(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Plan_Approval_Records (
                plan_approval_id, target_activity_plan_id,
                target_plan_revision_id, outcome, rationale,
                approving_party_id, authority_basis_type, authority_basis_id,
                applicable_scope, recorded_at
            ) VALUES (
                :aid_, :aid, :prev, 'Approve', 'Approved per ADR-001.',
                :party, 'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "aid_": _PLAN_APPROVAL_ID,
            "aid": _ACTIVITY_PLAN_ID,
            "prev": _PLAN_REVISION_ID,
            "party": _PARTY_ID,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )


def _seed_disclosure_coverage(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Disclosure_Policy_Coverage (
                policy_id, node_kind, recorded_at, backlog_adr_id
            ) VALUES (
                'slice-default-2026', 'plan_approval', :ts, 'ADR-HT-009'
            )
            """
        ),
        {"ts": _TS},
    )


def _seed_full_planning_graph(conn) -> None:
    """Seed every Slice 2 table with one valid row.

    Inserts Parties first (FK target for authoring_party_id), then walks
    the planning dependency graph: Objective → Project →
    Deliverable_Expectation → Activity_Plan → Plan_Revision →
    Plan_Review → Plan_Approval_Record. Intended_Outcome and the
    Disclosure_Policy_Coverage row are also seeded so every table in
    PLANNING_IMMUTABLE_TABLES has at least one row to mutate.
    """
    _seed_party(conn)
    _seed_objective(conn)
    _seed_intended_outcome(conn)
    _seed_project(conn)
    _seed_deliverable_expectation(conn)
    _seed_activity_plan(conn)
    _seed_plan_revision(conn)
    _seed_plan_review(conn)
    _seed_plan_approval(conn)
    _seed_disclosure_coverage(conn)


# ---------------------------------------------------------------------------
# Append-only triggers for every PLANNING_IMMUTABLE_TABLES row.
# ---------------------------------------------------------------------------


# Each entry pairs a table with a sample UPDATE/DELETE that targets the
# row inserted by ``_seed_full_planning_graph``. The UPDATE statement
# mutates a single column other than the table's primary key so the
# trigger's ``BEFORE UPDATE`` semantics are exercised on a real change.
_IMMUTABLE_TABLE_CASES: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "Objectives",
        "UPDATE Objectives SET created_at = '2099-01-01T00:00:00.000+00:00' "
        "WHERE objective_id = :id",
        {"id": _OBJECTIVE_ID},
        "DELETE FROM Objectives WHERE objective_id = :id",
    ),
    (
        "Objective_Revisions",
        "UPDATE Objective_Revisions SET statement = 'changed' "
        "WHERE objective_revision_id = :id",
        {"id": _OBJECTIVE_REV_ID},
        "DELETE FROM Objective_Revisions WHERE objective_revision_id = :id",
    ),
    (
        "Intended_Outcomes",
        "UPDATE Intended_Outcomes SET created_at = '2099-01-01T00:00:00.000+00:00' "
        "WHERE intended_outcome_id = :id",
        {"id": _INTENDED_OUTCOME_ID},
        "DELETE FROM Intended_Outcomes WHERE intended_outcome_id = :id",
    ),
    (
        "Intended_Outcome_Revisions",
        "UPDATE Intended_Outcome_Revisions SET success_condition = 'changed' "
        "WHERE intended_outcome_revision_id = :id",
        {"id": _INTENDED_OUTCOME_REV_ID},
        "DELETE FROM Intended_Outcome_Revisions WHERE intended_outcome_revision_id = :id",
    ),
    (
        "Projects",
        "UPDATE Projects SET created_at = '2099-01-01T00:00:00.000+00:00' "
        "WHERE project_id = :id",
        {"id": _PROJECT_ID},
        "DELETE FROM Projects WHERE project_id = :id",
    ),
    (
        "Project_Revisions",
        "UPDATE Project_Revisions SET name = 'changed' "
        "WHERE project_revision_id = :id",
        {"id": _PROJECT_REV_ID},
        "DELETE FROM Project_Revisions WHERE project_revision_id = :id",
    ),
    (
        "Deliverable_Expectations",
        "UPDATE Deliverable_Expectations SET created_at = '2099-01-01T00:00:00.000+00:00' "
        "WHERE deliverable_expectation_id = :id",
        {"id": _DELIVERABLE_ID},
        "DELETE FROM Deliverable_Expectations WHERE deliverable_expectation_id = :id",
    ),
    (
        "Deliverable_Expectation_Revisions",
        "UPDATE Deliverable_Expectation_Revisions SET name = 'changed' "
        "WHERE deliverable_expectation_revision_id = :id",
        {"id": _DELIVERABLE_REV_ID},
        "DELETE FROM Deliverable_Expectation_Revisions "
        "WHERE deliverable_expectation_revision_id = :id",
    ),
    (
        "Activity_Plans",
        "UPDATE Activity_Plans SET title = 'changed' WHERE activity_plan_id = :id",
        {"id": _ACTIVITY_PLAN_ID},
        "DELETE FROM Activity_Plans WHERE activity_plan_id = :id",
    ),
    (
        "Plan_Reviews",
        "UPDATE Plan_Reviews SET created_at = '2099-01-01T00:00:00.000+00:00' "
        "WHERE plan_review_id = :id",
        {"id": _PLAN_REVIEW_ID},
        "DELETE FROM Plan_Reviews WHERE plan_review_id = :id",
    ),
    (
        "Plan_Review_Revisions",
        "UPDATE Plan_Review_Revisions SET rationale = 'changed' "
        "WHERE plan_review_revision_id = :id",
        {"id": _PLAN_REVIEW_REV_ID},
        "DELETE FROM Plan_Review_Revisions WHERE plan_review_revision_id = :id",
    ),
    (
        "Plan_Approval_Records",
        "UPDATE Plan_Approval_Records SET rationale = 'changed' "
        "WHERE plan_approval_id = :id",
        {"id": _PLAN_APPROVAL_ID},
        "DELETE FROM Plan_Approval_Records WHERE plan_approval_id = :id",
    ),
    (
        "Disclosure_Policy_Coverage",
        "UPDATE Disclosure_Policy_Coverage SET backlog_adr_id = 'ADR-XXX-999' "
        "WHERE node_kind = 'plan_approval'",
        {},
        "DELETE FROM Disclosure_Policy_Coverage WHERE node_kind = 'plan_approval'",
    ),
)


def test_immutable_tables_constant_lists_every_planning_table_except_plan_revisions() -> None:
    """``PLANNING_IMMUTABLE_TABLES`` covers every Slice 2 table except ``Plan_Revisions``.

    The exclusion is intentional — ``Plan_Revisions`` has its own pair of
    triggers (a persistent DELETE-rejector plus a connection-private
    UPDATE trigger gating the AD-WS-19 lifecycle transition). Every
    other Slice 2 table is rejected unconditionally for both UPDATE and
    DELETE per AD-WS-4.
    """
    expected = {
        "Objectives",
        "Objective_Revisions",
        "Intended_Outcomes",
        "Intended_Outcome_Revisions",
        "Projects",
        "Project_Revisions",
        "Deliverable_Expectations",
        "Deliverable_Expectation_Revisions",
        "Activity_Plans",
        "Plan_Reviews",
        "Plan_Review_Revisions",
        "Plan_Approval_Records",
        "Disclosure_Policy_Coverage",
    }
    assert set(PLANNING_IMMUTABLE_TABLES) == expected
    assert "Plan_Revisions" not in PLANNING_IMMUTABLE_TABLES


@pytest.mark.parametrize(
    "table, update_sql, params, delete_sql", _IMMUTABLE_TABLE_CASES
)
def test_planning_immutable_table_rejects_update(
    planning_engine: Engine,
    table: str,
    update_sql: str,
    params: dict,
    delete_sql: str,
) -> None:
    """Every immutable Slice 2 table rejects UPDATE per AD-WS-19."""
    del delete_sql  # exercised in the companion test
    with planning_engine.begin() as conn:
        _seed_full_planning_graph(conn)

    with planning_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(update_sql), params)


@pytest.mark.parametrize(
    "table, update_sql, params, delete_sql", _IMMUTABLE_TABLE_CASES
)
def test_planning_immutable_table_rejects_delete(
    planning_engine: Engine,
    table: str,
    update_sql: str,
    params: dict,
    delete_sql: str,
) -> None:
    """Every immutable Slice 2 table rejects DELETE per AD-WS-19."""
    del update_sql
    with planning_engine.begin() as conn:
        _seed_full_planning_graph(conn)

    with planning_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(delete_sql), params)


def test_rejected_update_leaves_planning_row_byte_equivalent(
    planning_engine: Engine,
) -> None:
    """A rejected UPDATE must not mutate the row (AD-WS-19 / Property 12)."""
    with planning_engine.begin() as conn:
        _seed_full_planning_graph(conn)

    with planning_engine.connect() as conn:
        before = conn.execute(
            text("SELECT * FROM Plan_Approval_Records WHERE plan_approval_id=:id"),
            {"id": _PLAN_APPROVAL_ID},
        ).one()

    with planning_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Plan_Approval_Records SET rationale='changed' "
                    "WHERE plan_approval_id=:id"
                ),
                {"id": _PLAN_APPROVAL_ID},
            )

    with planning_engine.connect() as conn:
        after = conn.execute(
            text("SELECT * FROM Plan_Approval_Records WHERE plan_approval_id=:id"),
            {"id": _PLAN_APPROVAL_ID},
        ).one()
    assert before == after


# ---------------------------------------------------------------------------
# Plan_Revisions lifecycle trigger (AD-WS-19 / AD-WS-20)
#
# The trigger permits exactly the transition ``('draft','approved')``
# while the connection-scoped pragma is set, and rejects every other
# UPDATE. DELETE is always rejected. All assertions below run inside a
# single ``engine.connect()`` block because the pragma is a TEMP table
# private to that DBAPI connection — pool checkout for a fresh
# connection would lose the pragma.
# ---------------------------------------------------------------------------


def test_plan_revisions_rejects_delete(planning_engine: Engine) -> None:
    with planning_engine.begin() as conn:
        _seed_full_planning_graph(conn)

    with planning_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text("DELETE FROM Plan_Revisions WHERE plan_revision_id=:id"),
                {"id": _PLAN_REVISION_ID},
            )


def test_plan_revisions_update_rejected_without_pragma(
    planning_engine: Engine,
) -> None:
    """An UPDATE without the session pragma is rejected even for the
    documented draft→approved transition (AD-WS-19)."""
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="draft")

    # A fresh DBAPI connection starts with the connection-private
    # ``temp.walking_slice_session_state`` table empty (the connect
    # listener creates the table but seeds no rows), so the trigger's
    # ``NOT EXISTS`` clause fires and the UPDATE is rejected.
    with planning_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Plan_Revisions SET lifecycle_state='approved' "
                    "WHERE plan_revision_id=:id"
                ),
                {"id": _PLAN_REVISION_ID},
            )


def test_plan_revisions_update_permits_draft_to_approved_with_pragma(
    planning_engine: Engine,
) -> None:
    """The single permitted transition succeeds when the pragma is set."""
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="draft")

    correlation_id = "00000000-0000-7000-8000-0000000000aa"

    with planning_engine.connect() as conn:
        with conn.begin():
            set_plan_approval_in_progress(conn, correlation_id)
            conn.execute(
                text(
                    "UPDATE Plan_Revisions SET lifecycle_state='approved' "
                    "WHERE plan_revision_id=:id"
                ),
                {"id": _PLAN_REVISION_ID},
            )
            clear_plan_approval_in_progress(conn)

        # Verified inside the same connection so we are not relying on
        # uncommitted state from a different connection.
        state = conn.execute(
            text(
                "SELECT lifecycle_state FROM Plan_Revisions "
                "WHERE plan_revision_id=:id"
            ),
            {"id": _PLAN_REVISION_ID},
        ).scalar_one()
    assert state == "approved"


def test_plan_revisions_update_rejects_wrong_transition_with_pragma(
    planning_engine: Engine,
) -> None:
    """Even with the pragma set, only ``('draft','approved')`` is permitted.

    Verifies the reverse transition ``approved → draft`` is rejected
    (Requirement 9.4 — Approved Plan Revisions are byte-equivalent
    forever).
    """
    # Seed a Plan_Revision already in 'approved' state by inserting it
    # directly with the approved lifecycle_state.
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="approved")

    with planning_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            with conn.begin():
                set_plan_approval_in_progress(conn, "correlation-rev")
                conn.execute(
                    text(
                        "UPDATE Plan_Revisions SET lifecycle_state='draft' "
                        "WHERE plan_revision_id=:id"
                    ),
                    {"id": _PLAN_REVISION_ID},
                )


def test_plan_revisions_update_rejects_no_op_lifecycle_change_with_pragma(
    planning_engine: Engine,
) -> None:
    """A no-op ``draft → draft`` UPDATE is rejected even with the pragma set.

    The trigger requires both that ``OLD.lifecycle_state = 'draft'`` and
    ``NEW.lifecycle_state = 'approved'``. Anything else aborts.
    """
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="draft")

    with planning_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            with conn.begin():
                set_plan_approval_in_progress(conn, "correlation-noop")
                conn.execute(
                    text(
                        "UPDATE Plan_Revisions SET lifecycle_state='draft' "
                        "WHERE plan_revision_id=:id"
                    ),
                    {"id": _PLAN_REVISION_ID},
                )


def test_plan_revisions_update_rejects_non_lifecycle_column_change_with_pragma(
    planning_engine: Engine,
) -> None:
    """An UPDATE that mutates any column other than ``lifecycle_state``
    is rejected, even while the pragma is set (AD-WS-19)."""
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="draft")

    with planning_engine.connect() as conn:
        with pytest.raises(IntegrityError):
            with conn.begin():
                set_plan_approval_in_progress(conn, "correlation-otherfield")
                conn.execute(
                    text(
                        "UPDATE Plan_Revisions SET planned_scope='different' "
                        "WHERE plan_revision_id=:id"
                    ),
                    {"id": _PLAN_REVISION_ID},
                )


def test_plan_revisions_lifecycle_state_check_rejects_other_values(
    planning_engine: Engine,
) -> None:
    """The CHECK constraint restricts lifecycle_state to {draft, approved}.

    Even attempting to insert a Plan_Revision with a third value (e.g.
    ``'superseded'``) fails at INSERT time — AD-WS-18 defers other
    lifecycle states to a later slice.
    """
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)

    with planning_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_plan_revision(conn, lifecycle="superseded")


def test_pragma_is_connection_private(planning_engine: Engine) -> None:
    """The pragma value set on one connection is invisible to another.

    Confirms the trigger gating is per-connection: a second concurrent
    connection cannot observe an in-flight Plan Approval and exploit it
    to slip its own lifecycle transition past the trigger.
    """
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="draft")

    # Open one connection and set the pragma — but do NOT issue the
    # privileged UPDATE on it. Then open a *second* connection and try
    # to issue the UPDATE. The second connection sees no pragma value
    # and must be rejected.
    with planning_engine.connect() as setter_conn:
        set_plan_approval_in_progress(setter_conn, "correlation-other")

        with planning_engine.connect() as attacker_conn:
            with pytest.raises(IntegrityError):
                with attacker_conn.begin():
                    attacker_conn.execute(
                        text(
                            "UPDATE Plan_Revisions SET lifecycle_state='approved' "
                            "WHERE plan_revision_id=:id"
                        ),
                        {"id": _PLAN_REVISION_ID},
                    )

        # Clean up the setter connection's pragma so it doesn't leak.
        clear_plan_approval_in_progress(setter_conn)


def test_clearing_pragma_re_enables_rejection(planning_engine: Engine) -> None:
    """After ``clear_plan_approval_in_progress``, the trigger rejects again.

    Confirms the pragma window is tight: only the precise UPDATE the
    Planning_Service issues between ``set`` and ``clear`` succeeds.
    """
    with planning_engine.begin() as conn:
        _seed_party(conn)
        _seed_objective(conn)
        _seed_project(conn)
        _seed_activity_plan(conn)
        _seed_plan_revision(conn, lifecycle="draft")

    with planning_engine.connect() as conn:
        # First the privileged UPDATE goes through.
        with conn.begin():
            set_plan_approval_in_progress(conn, "correlation-first")
            conn.execute(
                text(
                    "UPDATE Plan_Revisions SET lifecycle_state='approved' "
                    "WHERE plan_revision_id=:id"
                ),
                {"id": _PLAN_REVISION_ID},
            )
            clear_plan_approval_in_progress(conn)

        # Any subsequent UPDATE on the same row is rejected — both
        # because the pragma is cleared and because the row is already
        # approved.
        with pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        "UPDATE Plan_Revisions SET lifecycle_state='approved' "
                        "WHERE plan_revision_id=:id"
                    ),
                    {"id": _PLAN_REVISION_ID},
                )


# ---------------------------------------------------------------------------
# Slice 1 non-modification spot-checks after Slice 2 schema installation
# (Requirement 19.2 / 19.4).
#
# Task 1.2 already pins ``Relationships.semantic_role`` and
# ``Identifier_Registry.resource_kind`` defaulting to NULL on Slice 1
# rows. The two tests below re-verify the same contract *after*
# ``create_planning_schema`` has run, confirming the Slice 2 schema
# installation does not retroactively populate those columns.
# ---------------------------------------------------------------------------


def test_slice_1_relationships_rows_remain_readable_with_null_semantic_role(
    planning_engine: Engine,
) -> None:
    """A Slice 1 ``Relationships`` row inserted without ``semantic_role``
    is readable unchanged after Slice 2 schema installation."""
    # Seed Slice 1 dependencies required by the Relationships FKs.
    with planning_engine.begin() as conn:
        _seed_party(conn)
        # Source_Document → Document_Revision is enough to support a
        # finding_revision-style Relationship; we keep the seed minimal
        # by inserting only the rows the FKs require.
        conn.execute(
            text(
                """
                INSERT INTO Source_Documents
                    (resource_id, current_location, authority, created_at)
                VALUES ('00000000-0000-7000-8000-0000000000d1',
                        '/path', 'authoritative', :ts)
                """
            ),
            {"ts": _TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Document_Revisions
                    (revision_id, resource_id, content_bytes,
                     content_digest_sha256, contributing_party_id,
                     recorded_at, change_description)
                VALUES ('00000000-0000-7000-8000-0000000000d2',
                        '00000000-0000-7000-8000-0000000000d1',
                        :body, :digest, :pid, :ts, 'init')
                """
            ),
            {
                "body": b"hello",
                "digest": "0" * 64,
                "pid": _PARTY_ID,
                "ts": _TS,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Content_Regions
                    (region_id, parent_resource_id, created_at)
                VALUES ('00000000-0000-7000-8000-0000000000d3',
                        '00000000-0000-7000-8000-0000000000d1', :ts)
                """
            ),
            {"ts": _TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Region_Occurrences
                    (region_id, document_revision_id, start_offset_bytes,
                     end_offset_bytes, span_byte_length,
                     span_content_digest_sha256, recorded_at)
                VALUES ('00000000-0000-7000-8000-0000000000d3',
                        '00000000-0000-7000-8000-0000000000d2',
                        0, 5, 5, :digest, :ts)
                """
            ),
            {"digest": "f" * 64, "ts": _TS},
        )
        conn.execute(
            text(
                "INSERT INTO Findings (finding_id, created_at) "
                "VALUES ('00000000-0000-7000-8000-0000000000d4', :ts)"
            ),
            {"ts": _TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Finding_Revisions
                    (finding_revision_id, finding_id, statement, is_hypothesis,
                     authoring_party_id, assumptions_json, recorded_at)
                VALUES ('00000000-0000-7000-8000-0000000000d5',
                        '00000000-0000-7000-8000-0000000000d4',
                        'A finding.', 0, :pid, '[]', :ts)
                """
            ),
            {"pid": _PARTY_ID, "ts": _TS},
        )
        # The Slice 1 row deliberately does NOT name semantic_role; the
        # NULL default applies.
        conn.execute(
            text(
                """
                INSERT INTO Relationships
                    (relationship_id, relationship_type, source_kind, source_id,
                     source_revision_id, target_kind, target_id,
                     target_revision_id, authoring_party_id, recorded_at)
                VALUES ('00000000-0000-7000-8000-0000000000d6', 'Supports',
                        'finding_revision', '00000000-0000-7000-8000-0000000000d4',
                        '00000000-0000-7000-8000-0000000000d5',
                        'region_occurrence', '00000000-0000-7000-8000-0000000000d3',
                        '00000000-0000-7000-8000-0000000000d2', :pid, :ts)
                """
            ),
            {"pid": _PARTY_ID, "ts": _TS},
        )

    with planning_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT relationship_type, source_kind, target_kind, semantic_role "
                "FROM Relationships WHERE relationship_id = :rid"
            ),
            {"rid": "00000000-0000-7000-8000-0000000000d6"},
        ).one()

    assert row.relationship_type == "Supports"
    assert row.source_kind == "finding_revision"
    assert row.target_kind == "region_occurrence"
    assert row.semantic_role is None


def test_slice_1_identifier_registry_rows_remain_readable_with_null_resource_kind(
    planning_engine: Engine,
) -> None:
    """A Slice 1 ``Identifier_Registry`` row inserted without ``resource_kind``
    is readable unchanged after Slice 2 schema installation."""
    new_id = "00000000-0000-7000-8000-0000000000e1"
    with planning_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Identifier_Registry "
                "(identifier, kind, content_digest, issued_at) "
                "VALUES (:id, 'resource', 'digest', :ts)"
            ),
            {"id": new_id, "ts": _TS},
        )

    with planning_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT identifier, kind, resource_kind "
                "FROM Identifier_Registry WHERE identifier = :id"
            ),
            {"id": new_id},
        ).one()
    assert row.identifier == new_id
    assert row.kind == "resource"
    assert row.resource_kind is None
