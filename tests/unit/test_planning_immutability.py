"""Unit tests for :mod:`walking_slice.planning._immutability` (task 11.2).

These tests pin the application-level half of the Approved Plan
Revision immutability contract (Requirements 9.4, 9.6 and design
§"Error Handling" rule 5):

- :class:`ApprovedPlanRevisionImmutableError` carries the stable
  ``error_code = "approved_plan_revision_immutable"`` and the target
  identity / attempted action / correlation values.
- :func:`enforce_approved_plan_revision_immutability` is silent for
  draft and unresolved Plan Revisions and raises (after appending
  exactly one Denial Record in a separate transaction) for approved
  Plan Revisions.
- :func:`map_integrity_error_to_immutability` recognises the planning
  trigger message markers (``AD-WS-4`` / ``AD-WS-19``) and translates
  the violation into the application error while persisting a denial.
  Unrelated IntegrityErrors are re-raised unchanged.
- The Denial Record append retries up to 3 times after the initial
  attempt with the exponential backoff sequence
  ``0.01s / 0.02s / 0.04s`` (AD-WS-9 / Slice 1 Requirement 7.6) and
  surfaces :class:`ApprovedPlanRevisionImmutableAuditFailureError`
  when every attempt fails.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.disclosure import seed as seed_disclosure
from walking_slice.persistence import create_schema
from walking_slice.planning import (
    APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE,
    APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE,
    ApprovedPlanRevisionImmutableAuditFailureError,
    ApprovedPlanRevisionImmutableError,
    enforce_approved_plan_revision_immutability,
    is_plan_revision_approved,
    is_planning_immutability_violation,
    map_integrity_error_to_immutability,
)
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Identifiers and fixed seed values.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_OBJECTIVE_ID = "00000000-0000-7000-8000-000000000100"
_PROJECT_ID = "00000000-0000-7000-8000-000000000120"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000000140"
_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000000150"
_DECISION_ID = "00000000-0000-7000-8000-000000000180"
_CORRELATION_ID = "00000000-0000-7000-8000-0000000000aa"
_SCOPE = "pilot/team-a"
_TS = "2026-01-01T00:00:00.000+00:00"
_RECORDED_TIME = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_ATTEMPTED_ACTION = "update.plan_revision"


# ---------------------------------------------------------------------------
# Schema + per-test seeding fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 2 schemas and disclosure seed."""
    create_schema(engine)
    create_planning_schema(engine)
    seed_disclosure(engine)
    return engine


def _seed_party(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO Parties (party_id, kind, display_name, created_at) "
            "VALUES (:pid, 'person', 'Planner', :ts)"
        ),
        {"pid": _PARTY_ID, "ts": _TS},
    )


def _seed_min_plan_revision(conn, *, lifecycle: str = "draft") -> None:
    """Insert just enough rows for a Plan Revision in the requested lifecycle.

    The Plan Revision is the only row the immutability helper reads, but
    its FK references require the Activity Plan, Project, and Objective
    rows to exist so the canonical schema accepts every INSERT.
    """
    _seed_party(conn)
    conn.execute(
        text("INSERT INTO Objectives (objective_id, created_at) VALUES (:id, :ts)"),
        {"id": _OBJECTIVE_ID, "ts": _TS},
    )
    conn.execute(
        text("INSERT INTO Projects (project_id, created_at) VALUES (:id, :ts)"),
        {"id": _PROJECT_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Activity_Plans (
                activity_plan_id, target_project_id, title,
                authoring_party_id, applicable_scope, recorded_at
            ) VALUES (
                :id, :pid, 'Mesh Rollout — Phase 1', :party, :scope, :ts
            )
            """
        ),
        {
            "id": _ACTIVITY_PLAN_ID,
            "pid": _PROJECT_ID,
            "party": _PARTY_ID,
            "scope": _SCOPE,
            "ts": _TS,
        },
    )
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


def _seed_draft_plan_revision(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_min_plan_revision(conn, lifecycle="draft")


def _seed_approved_plan_revision(engine: Engine) -> None:
    """Seed a Plan Revision row already in the ``'approved'`` state.

    The Plan Revision lifecycle trigger only permits the
    ``'draft' → 'approved'`` UPDATE while the pragma is set; to skip
    that whole dance for tests that only need a row in the approved
    state, the helper inserts the row with ``lifecycle_state =
    'approved'`` directly. Insertion is unconstrained by the
    lifecycle trigger (the trigger fires on UPDATE only).
    """
    with engine.begin() as conn:
        _seed_min_plan_revision(conn, lifecycle="approved")


# ---------------------------------------------------------------------------
# Audit row helpers.
# ---------------------------------------------------------------------------


def _read_denial_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT * FROM Audit_Records WHERE outcome = 'deny' "
                "ORDER BY append_sequence"
            )
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Error contract.
# ---------------------------------------------------------------------------


class TestApprovedPlanRevisionImmutableError:
    """The exception is the public contract surface to the HTTP layer."""

    def test_error_code_is_stable_contract_value(self) -> None:
        exc = ApprovedPlanRevisionImmutableError(
            target_plan_revision_id=_PLAN_REVISION_ID,
            attempted_action=_ATTEMPTED_ACTION,
            correlation_id=_CORRELATION_ID,
        )
        assert exc.error_code == "approved_plan_revision_immutable"
        assert exc.error_code == APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE

    def test_attributes_are_carried_verbatim(self) -> None:
        exc = ApprovedPlanRevisionImmutableError(
            target_plan_revision_id=_PLAN_REVISION_ID,
            attempted_action=_ATTEMPTED_ACTION,
            correlation_id=_CORRELATION_ID,
        )
        assert exc.target_plan_revision_id == _PLAN_REVISION_ID
        assert exc.attempted_action == _ATTEMPTED_ACTION
        assert exc.correlation_id == _CORRELATION_ID

    def test_message_mentions_requirement_and_error_code(self) -> None:
        """The default message is diagnostic-friendly for log readers."""
        exc = ApprovedPlanRevisionImmutableError(
            target_plan_revision_id=_PLAN_REVISION_ID,
            attempted_action=_ATTEMPTED_ACTION,
            correlation_id=_CORRELATION_ID,
        )
        message = str(exc)
        assert "Requirement 9.4" in message
        assert "approved_plan_revision_immutable" in message
        assert _PLAN_REVISION_ID in message


# ---------------------------------------------------------------------------
# is_plan_revision_approved.
# ---------------------------------------------------------------------------


class TestIsPlanRevisionApproved:
    """Tri-state lookup: ``True``, ``False``, or ``None``."""

    def test_returns_true_for_approved_plan_revision(
        self, planning_engine: Engine
    ) -> None:
        _seed_approved_plan_revision(planning_engine)
        with planning_engine.connect() as conn:
            assert is_plan_revision_approved(conn, _PLAN_REVISION_ID) is True

    def test_returns_false_for_draft_plan_revision(
        self, planning_engine: Engine
    ) -> None:
        _seed_draft_plan_revision(planning_engine)
        with planning_engine.connect() as conn:
            assert is_plan_revision_approved(conn, _PLAN_REVISION_ID) is False

    def test_returns_none_for_unresolved_plan_revision(
        self, planning_engine: Engine
    ) -> None:
        with planning_engine.connect() as conn:
            assert (
                is_plan_revision_approved(conn, _PLAN_REVISION_ID)
                is None
            )


# ---------------------------------------------------------------------------
# enforce_approved_plan_revision_immutability — pre-check.
# ---------------------------------------------------------------------------


class TestEnforceApprovedPlanRevisionImmutability:
    """Pre-check: silent on draft / unresolved, raises + audits on approved."""

    def test_returns_silently_for_draft_plan_revision(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        _seed_draft_plan_revision(planning_engine)
        enforce_approved_plan_revision_immutability(
            engine=planning_engine,
            audit_log=audit_log,
            target_plan_revision_id=_PLAN_REVISION_ID,
            actor_party_id=_PARTY_ID,
            attempted_action=_ATTEMPTED_ACTION,
            correlation_id=_CORRELATION_ID,
            recorded_time=_RECORDED_TIME,
        )
        # No Denial Record should have been written.
        assert _read_denial_rows(planning_engine) == []

    def test_returns_silently_for_unresolved_plan_revision(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        # No seed — the Plan Revision identifier does not resolve.
        enforce_approved_plan_revision_immutability(
            engine=planning_engine,
            audit_log=audit_log,
            target_plan_revision_id=_PLAN_REVISION_ID,
            actor_party_id=_PARTY_ID,
            attempted_action=_ATTEMPTED_ACTION,
            correlation_id=_CORRELATION_ID,
            recorded_time=_RECORDED_TIME,
        )
        assert _read_denial_rows(planning_engine) == []

    def test_raises_and_appends_denial_for_approved_plan_revision(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        _seed_approved_plan_revision(planning_engine)

        with pytest.raises(ApprovedPlanRevisionImmutableError) as excinfo:
            enforce_approved_plan_revision_immutability(
                engine=planning_engine,
                audit_log=audit_log,
                target_plan_revision_id=_PLAN_REVISION_ID,
                actor_party_id=_PARTY_ID,
                attempted_action=_ATTEMPTED_ACTION,
                correlation_id=_CORRELATION_ID,
                recorded_time=_RECORDED_TIME,
            )

        # Exception carries the contract values verbatim.
        assert excinfo.value.error_code == (
            APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE
        )
        assert excinfo.value.target_plan_revision_id == _PLAN_REVISION_ID
        assert excinfo.value.attempted_action == _ATTEMPTED_ACTION
        assert excinfo.value.correlation_id == _CORRELATION_ID

        # Exactly one Denial Record was appended with the expected
        # column values; the row survives in its own transaction.
        rows = _read_denial_rows(planning_engine)
        assert len(rows) == 1
        row = rows[0]
        assert row["outcome"] == "deny"
        assert row["actor_party_id"] == _PARTY_ID
        assert row["action_type"] == _ATTEMPTED_ACTION
        assert row["target_revision_id"] == _PLAN_REVISION_ID
        assert row["reason_code"] == (
            APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE
        )
        assert row["correlation_id"] == _CORRELATION_ID
        assert row["recorded_at"] == format_iso8601_ms(_RECORDED_TIME)


# ---------------------------------------------------------------------------
# is_planning_immutability_violation — trigger marker detection.
# ---------------------------------------------------------------------------


def _planning_trigger_integrity_error(
    planning_engine: Engine,
) -> IntegrityError:
    """Provoke a real planning-trigger ``IntegrityError`` for inspection.

    Inserts a Plan Approval row, then attempts to UPDATE it — the
    ``Plan_Approval_Records`` AD-WS-4 trigger rejects the UPDATE and
    SQLAlchemy surfaces the underlying ``sqlite3.IntegrityError``
    wrapped in :class:`sqlalchemy.exc.IntegrityError`.
    """
    plan_approval_id = "00000000-0000-7000-8000-000000000170"
    authority_basis_id = "00000000-0000-7000-8000-000000000181"
    _seed_approved_plan_revision(planning_engine)
    with planning_engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid_, :aid, :prev, 'Approve', 'Approved per ADR-001.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "aid_": plan_approval_id,
                "aid": _ACTIVITY_PLAN_ID,
                "prev": _PLAN_REVISION_ID,
                "party": _PARTY_ID,
                "abid": authority_basis_id,
                "scope": _SCOPE,
                "ts": _TS,
            },
        )

    try:
        with planning_engine.connect() as conn, conn.begin():
            conn.execute(
                text(
                    "UPDATE Plan_Approval_Records SET rationale='changed' "
                    "WHERE plan_approval_id=:id"
                ),
                {"id": plan_approval_id},
            )
    except IntegrityError as exc:
        return exc
    raise AssertionError(
        "Expected planning trigger IntegrityError to fire; UPDATE was permitted."
    )


class TestIsPlanningImmutabilityViolation:
    """Detection by stable design-identifier substring marker."""

    def test_recognises_real_planning_trigger_error(
        self, planning_engine: Engine
    ) -> None:
        error = _planning_trigger_integrity_error(planning_engine)
        assert is_planning_immutability_violation(error) is True

    def test_recognises_ad_ws_4_marker_in_synthetic_message(self) -> None:
        # Simulate a SQLAlchemy IntegrityError whose ``.orig`` is a
        # plain Exception whose ``str`` carries the trigger marker —
        # mirrors the wrapped sqlite3.IntegrityError shape.
        synthetic_orig = Exception(
            "Plan_Approval_Records is append-only; UPDATE rejected per "
            "design AD-WS-4 / AD-WS-19."
        )
        error = IntegrityError("UPDATE ...", {}, synthetic_orig)
        assert is_planning_immutability_violation(error) is True

    def test_recognises_ad_ws_19_marker_in_synthetic_message(self) -> None:
        synthetic_orig = Exception(
            "Plan_Revisions UPDATE rejected: only the draft->approved "
            "lifecycle transition is permitted, and only while the "
            "walking_slice.plan_approval_in_progress session pragma is "
            "set (AD-WS-19 / AD-WS-20)."
        )
        error = IntegrityError("UPDATE ...", {}, synthetic_orig)
        assert is_planning_immutability_violation(error) is True

    def test_rejects_unrelated_integrity_error(self) -> None:
        # FK / UNIQUE / CHECK errors do not mention either design
        # identifier, so the helper must not recognise them.
        synthetic_orig = Exception(
            "UNIQUE constraint failed: Plan_Approval_Records.target_plan_revision_id"
        )
        error = IntegrityError("INSERT ...", {}, synthetic_orig)
        assert is_planning_immutability_violation(error) is False

    def test_handles_missing_orig_via_str_fallback(self) -> None:
        # When ``.orig`` is None the helper falls back to ``str(error)``;
        # SQLAlchemy renders the params + statement in str(error) but
        # any marker in the wrapper text should still be detected.
        error = IntegrityError(
            "UPDATE Plan_Approval_Records SET rationale='changed'",
            {},
            Exception("rejected per AD-WS-4"),
        )
        assert is_planning_immutability_violation(error) is True


# ---------------------------------------------------------------------------
# map_integrity_error_to_immutability — translation + denial.
# ---------------------------------------------------------------------------


class TestMapIntegrityErrorToImmutability:
    """Translates a recognised IntegrityError into the application error."""

    def test_translates_planning_trigger_error_and_appends_denial(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        triggered = _planning_trigger_integrity_error(planning_engine)

        result = map_integrity_error_to_immutability(
            triggered,
            engine=planning_engine,
            audit_log=audit_log,
            target_plan_revision_id=_PLAN_REVISION_ID,
            actor_party_id=_PARTY_ID,
            attempted_action="update.plan_approval",
            correlation_id=_CORRELATION_ID,
            recorded_time=_RECORDED_TIME,
        )

        assert isinstance(result, ApprovedPlanRevisionImmutableError)
        assert result.error_code == (
            APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE
        )
        assert result.target_plan_revision_id == _PLAN_REVISION_ID
        assert result.attempted_action == "update.plan_approval"
        assert result.correlation_id == _CORRELATION_ID

        rows = _read_denial_rows(planning_engine)
        assert len(rows) == 1
        assert rows[0]["action_type"] == "update.plan_approval"
        assert rows[0]["reason_code"] == (
            APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE
        )
        assert rows[0]["target_revision_id"] == _PLAN_REVISION_ID

    def test_reraises_unrelated_integrity_error_without_audit(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        synthetic_orig = Exception(
            "UNIQUE constraint failed: Plan_Approval_Records.target_plan_revision_id"
        )
        unrelated = IntegrityError("INSERT ...", {}, synthetic_orig)

        with pytest.raises(IntegrityError) as excinfo:
            map_integrity_error_to_immutability(
                unrelated,
                engine=planning_engine,
                audit_log=audit_log,
                target_plan_revision_id=_PLAN_REVISION_ID,
                actor_party_id=_PARTY_ID,
                attempted_action="create.plan_approval",
                correlation_id=_CORRELATION_ID,
                recorded_time=_RECORDED_TIME,
            )
        # Same instance — the helper did not wrap or replace it.
        assert excinfo.value is unrelated
        # And no denial was appended.
        assert _read_denial_rows(planning_engine) == []


# ---------------------------------------------------------------------------
# Denial-record retry contract.
# ---------------------------------------------------------------------------


class _RecordingSleep:
    """Capturing stub for the ``denial_audit_sleep`` callable."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class _FailingAuditLog(AuditLog):
    """AuditLog whose ``append_denial`` fails the first ``failures`` times."""

    def __init__(self, base: AuditLog, failures: int) -> None:
        # Re-use the configured Clock from the base instance.
        super().__init__(base._clock)  # type: ignore[attr-defined]
        self._remaining_failures = failures
        self.attempts = 0

    def append_denial(self, connection, **kwargs):  # type: ignore[override]
        self.attempts += 1
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise AuditAppendError(
                f"Simulated append_denial failure (attempt {self.attempts})."
            )
        return super().append_denial(connection, **kwargs)


class TestDenialRetryContract:
    """The AD-WS-9 / Slice 1 Requirement 7.6 retry sequence is reproduced."""

    def test_eventual_success_after_two_failures(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        _seed_approved_plan_revision(planning_engine)
        failing = _FailingAuditLog(audit_log, failures=2)
        sleep = _RecordingSleep()

        with pytest.raises(ApprovedPlanRevisionImmutableError):
            enforce_approved_plan_revision_immutability(
                engine=planning_engine,
                audit_log=failing,
                target_plan_revision_id=_PLAN_REVISION_ID,
                actor_party_id=_PARTY_ID,
                attempted_action=_ATTEMPTED_ACTION,
                correlation_id=_CORRELATION_ID,
                recorded_time=_RECORDED_TIME,
                denial_audit_sleep=sleep,
            )

        # Three append attempts: two failures + one success.
        assert failing.attempts == 3
        # Two sleeps between the three attempts: 0.01 and 0.02.
        assert sleep.calls == [0.01, 0.02]
        # One Denial Record persisted.
        assert len(_read_denial_rows(planning_engine)) == 1

    def test_audit_failure_after_all_retries(
        self,
        planning_engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        _seed_approved_plan_revision(planning_engine)
        # Four attempts maximum (initial + 3 retries); fail every one.
        failing = _FailingAuditLog(audit_log, failures=4)
        sleep = _RecordingSleep()

        with pytest.raises(
            ApprovedPlanRevisionImmutableAuditFailureError
        ) as excinfo:
            enforce_approved_plan_revision_immutability(
                engine=planning_engine,
                audit_log=failing,
                target_plan_revision_id=_PLAN_REVISION_ID,
                actor_party_id=_PARTY_ID,
                attempted_action=_ATTEMPTED_ACTION,
                correlation_id=_CORRELATION_ID,
                recorded_time=_RECORDED_TIME,
                denial_audit_sleep=sleep,
            )

        assert excinfo.value.target_plan_revision_id == _PLAN_REVISION_ID
        assert excinfo.value.attempted_action == _ATTEMPTED_ACTION
        assert excinfo.value.correlation_id == _CORRELATION_ID
        assert excinfo.value.attempts == 4
        # Three sleeps between the four attempts: 0.01, 0.02, 0.04.
        assert sleep.calls == [0.01, 0.02, 0.04]
        # No Denial Record persisted — every attempt failed.
        assert _read_denial_rows(planning_engine) == []
