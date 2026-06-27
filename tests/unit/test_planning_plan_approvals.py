"""Unit tests for :mod:`walking_slice.planning.plan_approvals` (task 11.3).

Pins the contract established in task 11.1, design
§"Planning_Service.PlanApprovals", AD-WS-15 / AD-WS-19 / AD-WS-20, and
Requirements 9.1, 9.4, 9.5, 9.6, 10.1, 10.6 for
:meth:`PlanApprovalService.create_plan_approval`:

- **9.5 — duplicate-approval rejection.** A second Plan Approval against
  a Plan Revision that already has a Plan Approval Record (here: a
  ``Reject_Approval`` outcome that leaves the Plan Revision in
  ``'draft'``) is rejected with :class:`PlanApprovalConflictError`
  carrying the existing Plan Approval Identity. The original
  ``Plan_Approval_Records`` row remains byte-equivalent.
- **10.1 / 11.5 — authority deny path with all five reason codes.**
  Every reason code in the Slice 1 enumeration
  ``{not-yet-effective, expired, revoked, out-of-scope,
  no-role-assignment}`` surfaces as
  :class:`PlanApprovalAuthorizationError` with the matching
  ``reason_code``; each denial appends exactly one Denial Record to
  ``Audit_Records`` in a separate transaction and leaves the target
  Plan Revision byte-equivalent.
- **9.1 / AD-WS-19 — session-pragma lifecycle trigger.** On
  ``outcome='Approve'`` the target Plan Revision transitions from
  ``'draft'`` to ``'approved'`` inside the Plan Approval transaction;
  on ``outcome='Reject_Approval'`` the lifecycle stays in ``'draft'``.
  After the Plan Approval transaction commits the connection-private
  pragma is cleared, so a fresh connection that attempts an UPDATE on
  any ``Plan_Revisions`` row (including a second draft revision) is
  rejected by the trigger.
- **9.6 / 10.6 — manifest persistence failure rolls back everything.**
  When the wired :class:`ProvenanceManifestWriter` raises during
  ``write_manifest``, the exception propagates and the caller's
  transaction rolls back: no ``Plan_Approval_Records`` row, no
  ``Addresses`` ``Relationships`` row, no consequential ``Audit_Records``
  row, no lifecycle UPDATE, and no ``Identifier_Registry`` binding for
  the would-be Plan Approval persists. The target Plan Revision's
  lifecycle state remains ``'draft'``.

The tests mirror the style of
``tests/unit/test_planning_plan_reviews.py`` for seed helpers and
``tests/unit/test_knowledge_decision_authority.py`` for the
all-five-reason-codes deny pattern; both files exercise the same Slice
1 :class:`AuthorizationService` + Slice 2 schema combination this file
needs.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

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
from walking_slice.identity import IdentityService
from walking_slice.manifests import (
    IncludedSource,
    OmissionEntry as ManifestOmissionEntry,
    ProvenanceManifestWriter,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.plan_approvals import (
    CreatePlanApprovalResult,
    PlanApprovalAuthorizationError,
    PlanApprovalConflictError,
    PlanApprovalService,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000a00099"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00040"
_SECOND_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00041"
_SCOPE = "pilot/team-a"
_OTHER_SCOPE = "pilot/team-b"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_RATIONALE = "Approver endorses Phase 1 scope per ADR-001."

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
    ``create_planning_schema`` installs every Slice 2 table, index, and
    append-only trigger plus the connection-private session-state TEMP
    table that backs the AD-WS-19 lifecycle pragma (task 1.3).
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def plan_approval_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    manifest_writer: ProvenanceManifestWriter,
) -> PlanApprovalService:
    """:class:`PlanApprovalService` wired with the production
    :class:`ProvenanceManifestWriter`.

    The deny-path tests exercise the real :class:`AuthorizationService`
    by deliberately omitting (or invalidating) the Plan Approver role
    assignment rather than substituting a stub. The manifest-failure
    test constructs its own service with a failing writer in the test
    body — see :class:`_FailingManifestWriter` below.
    """
    return PlanApprovalService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        manifest_writer=manifest_writer,
        # Tests do not exercise the audit-retry backoff timing path; the
        # default ``time.sleep`` is harmless when no retry fires.
    )


# ---------------------------------------------------------------------------
# Seed helpers.
#
# A Plan Approval depends on the target Plan Revision (and transitively
# on the parent Activity Plan / Project) existing. These helpers seed
# just enough header rows for the target-lookup SELECT and the
# Plan_Approval_Records foreign keys to succeed.
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
    """Seed the approving Party and the assigning-authority Party.

    Both rows are referenced by foreign keys on
    ``Plan_Approval_Records.approving_party_id`` (the approver) and
    ``Role_Assignments.assigning_authority_id`` (the role-granter)
    respectively.
    """
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Plan Approver")
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
    """Seed one Activity Plan row.

    The Plan Revision FK requires the Activity Plan row to exist;
    seeding directly bypasses :class:`ActivityPlanService` which is
    exercised by its own test module.
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
                "title": "Mesh Rollout Activities",
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision_directly(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    lifecycle_state: str = "draft",
    planned_scope: str = "Phase 1 scope.",
) -> None:
    """Seed a ``Plan_Revisions`` row by hand, bypassing the service.

    INSERTs into ``Plan_Revisions`` are not gated by the AD-WS-19
    lifecycle trigger (which only watches UPDATE), so seeding a row
    with ``lifecycle_state = 'draft'`` is a direct INSERT — no pragma
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
                    :rev, :aid, NULL, :state, :scope_text, '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "scope_text": planned_scope,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_plan_approver_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Plan Approver authority (``approve``) to ``party_id``.

    Per AD-WS-15 / Requirement 11.5, ``create.plan_approval`` maps to
    the ``approve`` authority type. A Party with an effective Role
    Assignment carrying ``approve`` over ``scope`` is permitted to
    record a Plan Approval against a Plan Revision in that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="plan_approver",
        scope=scope,
        authorities_granted=("approve",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _revoke_role(engine: Engine, role_assignment_id: str, when: datetime) -> None:
    """Stamp ``revoked_at`` on a Role Assignment.

    Mirrors the pattern in ``test_knowledge_decision_authority.py`` —
    :class:`AuthorizationService` does not yet expose a revocation
    method, so the deny-path test uses a direct UPDATE.
    """
    from walking_slice.audit import format_iso8601_ms

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(when), "rid": role_assignment_id},
        )


# ---------------------------------------------------------------------------
# Row readers — used by negative-path tests to confirm nothing was
# persisted and by positive-path tests to inspect inserted rows.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _lifecycle_state(engine: Engine, plan_revision_id: str) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text(
                    "SELECT lifecycle_state FROM Plan_Revisions "
                    "WHERE plan_revision_id = :id"
                ),
                {"id": plan_revision_id},
            ).scalar_one()
        )


def _snapshot_plan_approval_row(
    engine: Engine, plan_approval_id: str
) -> dict:
    """Return every persisted column of one ``Plan_Approval_Records``
    row as a plain dict.

    Used by the duplicate-approval test to assert byte-equivalence of
    the original Plan Approval row after the rejected second attempt
    (Requirement 9.4).
    """
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    """
                    SELECT plan_approval_id, target_activity_plan_id,
                           target_plan_revision_id, outcome, rationale,
                           approving_party_id, authority_basis_type,
                           authority_basis_id, applicable_scope,
                           recorded_at
                    FROM Plan_Approval_Records
                    WHERE plan_approval_id = :id
                    """
                ),
                {"id": plan_approval_id},
            )
            .mappings()
            .one()
        )


def _count_addresses_relationships_with_source(
    engine: Engine, source_id: str
) -> int:
    """Count ``Addresses`` rows whose ``source_id`` equals ``source_id``.

    Each Plan Approval persists exactly one such row per Requirement
    9.3; on a rolled-back transaction the count must be zero.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = 'Addresses' "
                    "AND source_id = :sid"
                ),
                {"sid": source_id},
            ).scalar_one()
        )


def _count_consequential_audit_rows(
    engine: Engine, action_type: str
) -> int:
    """Count consequential ``Audit_Records`` for ``action_type``.

    On a rolled-back Plan Approval transaction the count must be
    zero (Requirement 9.7 — the consequential audit row participates
    in the caller's transaction and rolls back with it).
    """
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


def _count_denial_audit_rows(
    engine: Engine, action_type: str
) -> int:
    """Count denial ``Audit_Records`` for ``action_type``.

    A Denial Record is distinguished from the authorization
    evaluation row (which also carries ``outcome='deny'``) by
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


# ---------------------------------------------------------------------------
# Test doubles.
# ---------------------------------------------------------------------------


class _ManifestWriteFailure(RuntimeError):
    """Sentinel exception raised by :class:`_FailingManifestWriter`.

    Subclassing :class:`RuntimeError` keeps the exception class outside
    every category the service catches (the deny-path retry handler
    catches :class:`AuditAppendError` / :class:`SQLAlchemyError`; a
    bare :class:`RuntimeError` propagates straight through the
    ``finally`` block that clears the session pragma, which is exactly
    the rollback scenario Requirement 9.6 / 10.6 demand).
    """


@dataclass
class _FailingManifestWriter:
    """:class:`ProvenanceManifestWriter` double whose ``write_manifest``
    always raises :class:`_ManifestWriteFailure`.

    The Plan Approval persistence flow calls ``write_manifest`` after
    inserting the ``Plan_Approval_Records`` row and the ``Addresses``
    ``Relationships`` row, and before the lifecycle UPDATE and the
    consequential audit append (per design §"Planning_Service.
    PlanApprovals" / AD-WS-20). Raising at this point exercises the
    Requirement 9.6 / 10.6 rollback path: the caller's
    :meth:`Engine.begin` block sees the exception, the transaction
    rolls back, and every row inserted so far disappears.

    Records each invocation so the test can assert the writer was
    actually called (catching the regression where the service skips
    the manifest entirely).
    """

    call_count: int = 0
    last_kwargs: dict[str, Any] = field(default_factory=dict)

    def write_manifest(
        self,
        connection: Connection,  # noqa: ARG002 - intentionally unused
        **kwargs: Any,
    ):  # pragma: no cover - signature-compatible with the real writer
        self.call_count += 1
        self.last_kwargs = dict(kwargs)
        raise _ManifestWriteFailure(
            "Injected manifest persistence failure for Requirement 9.6 / "
            "10.6 rollback test."
        )


# ===========================================================================
# Happy-path baseline — confirms the test wiring before the focus tests run.
#
# Without this, a regression in the role-seeding helpers or the
# planning schema fixture would surface as several mysterious
# negative-path failures rather than one focused diagnostic.
# ===========================================================================


def test_create_plan_approval_permits_when_approver_role_grants_approve(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    plan_approval_service: PlanApprovalService,
) -> None:
    """Permit path: with an effective Plan Approver role and a Draft
    target Plan Revision, the service writes one
    ``Plan_Approval_Records`` row, one ``Addresses`` Relationship,
    one Provenance Manifest, one lifecycle UPDATE, and one
    consequential audit row inside one transaction.
    """
    _seed_required_parties(planning_engine)
    _assign_plan_approver_role(authorization_service, planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    _seed_plan_revision_directly(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
        lifecycle_state="draft",
    )

    with planning_engine.begin() as conn:
        result = plan_approval_service.create_plan_approval(
            conn,
            planning_engine,
            target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            outcome="Approve",
            rationale=_RATIONALE,
            approving_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreatePlanApprovalResult)
    assert _CANONICAL_UUID7.match(result.plan_approval_id)
    assert result.target_plan_revision_id == _DRAFT_PLAN_REVISION_ID
    assert result.target_activity_plan_id == _ACTIVITY_PLAN_ID
    assert result.outcome == "Approve"
    assert result.new_lifecycle_state == "approved"
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Plan_Approval_Records") == 1
    assert _count_addresses_relationships_with_source(
        planning_engine, result.plan_approval_id
    ) == 1
    assert _count(planning_engine, "Provenance_Manifests") == 1
    assert _count_consequential_audit_rows(
        planning_engine, "create.plan_approval"
    ) == 1
    assert _lifecycle_state(
        planning_engine, _DRAFT_PLAN_REVISION_ID
    ) == "approved"


# ===========================================================================
# Requirement 9.5 — duplicate-approval rejection.
#
# At most one Plan Approval Record per Plan Revision. The UNIQUE
# constraint on ``Plan_Approval_Records.target_plan_revision_id`` is
# the source of truth; the service pre-check surfaces a structured
# :class:`PlanApprovalConflictError` carrying the existing Plan
# Approval Identity *before* the SQL round-trip so the route layer
# (task 15.1) can render a structured 409 without scraping
# IntegrityError messages.
# ===========================================================================


class TestDuplicateApprovalRejection:
    """A second Plan Approval against the same Plan Revision is rejected."""

    def test_second_plan_approval_against_same_revision_raises_conflict(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_approval_service: PlanApprovalService,
    ) -> None:
        """Requirement 9.5: a second Plan Approval against the same
        target Plan Revision raises :class:`PlanApprovalConflictError`
        carrying the existing Plan Approval Identity.

        The first approval uses ``Reject_Approval`` so the Plan
        Revision stays in ``'draft'`` — that keeps the not-draft
        check from firing first and exercises the dedicated
        conflict-pre-check code path.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_approver_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        # First Plan Approval — ``Reject_Approval`` so the Plan
        # Revision stays in ``'draft'``.
        with planning_engine.begin() as conn:
            first = plan_approval_service.create_plan_approval(
                conn,
                planning_engine,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Reject_Approval",
                rationale="Defer pending review.",
                approving_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                correlation_id="corr-first",
            )

        assert first.outcome == "Reject_Approval"
        assert first.new_lifecycle_state == "draft"
        before = _snapshot_plan_approval_row(
            planning_engine, first.plan_approval_id
        )

        # Second attempt — must raise PlanApprovalConflictError.
        with pytest.raises(PlanApprovalConflictError) as exc_info:
            with planning_engine.begin() as conn:
                plan_approval_service.create_plan_approval(
                    conn,
                    planning_engine,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome="Approve",
                    rationale="Reverse the earlier rejection.",
                    approving_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    correlation_id="corr-second",
                )

        # The exception identifies the conflicting target Plan
        # Revision and the *existing* Plan Approval Identity (per
        # Requirement 9.5 — surface a structured error rather than
        # a raw IntegrityError).
        assert exc_info.value.target_plan_revision_id == (
            _DRAFT_PLAN_REVISION_ID
        )
        assert exc_info.value.existing_plan_approval_id == (
            first.plan_approval_id
        )
        assert exc_info.value.failed_constraint == (
            "plan_approval_already_recorded"
        )

        # Exactly one Plan Approval row exists — the original.
        assert _count(planning_engine, "Plan_Approval_Records") == 1

        # The original Plan Approval row is byte-equivalent (no
        # mutation, no second row).
        after = _snapshot_plan_approval_row(
            planning_engine, first.plan_approval_id
        )
        assert before == after

        # The target Plan Revision's lifecycle state is unchanged —
        # ``'draft'`` from the first ``Reject_Approval`` outcome.
        assert _lifecycle_state(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == "draft"


# ===========================================================================
# Requirements 10.1, 11.5 — authority deny path with all 5 reason codes.
#
# Per AD-WS-15 the ``create.plan_approval`` action requires the
# ``approve`` authority; Slice 1 Requirement 12.2 / 7.6 prescribes the
# five reason codes the evaluator returns. Each reason exercises the
# Slice 1 separate-transaction Denial Record pattern reproduced in the
# Plan Approval service.
# ===========================================================================


def _trigger_plan_approval_denial(
    reason: str,
    *,
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    plan_approval_service: PlanApprovalService,
    correlation_id: str,
    evaluation_at: datetime = datetime(2026, 6, 1, tzinfo=timezone.utc),
) -> PlanApprovalAuthorizationError:
    """Drive ``create_plan_approval`` to the named reason code.

    Mirrors ``_trigger_denial`` in
    ``tests/unit/test_knowledge_decision_authority.py``. Seeds the
    required parties and the target Plan Revision (always Draft so
    the not-draft / conflict pre-checks pass through to the
    authorization step), then arranges the Role Assignment state
    required by the reason code.

    Returns the raised exception so the caller can assert on its
    fields.
    """
    _seed_required_parties(planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    _seed_plan_revision_directly(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
        lifecycle_state="draft",
    )

    if reason == "no-role-assignment":
        # No Role Assignment seeded.
        pass
    elif reason == "out-of-scope":
        _assign_plan_approver_role(
            authorization_service,
            planning_engine,
            scope=_OTHER_SCOPE,
        )
    elif reason == "not-yet-effective":
        _assign_plan_approver_role(
            authorization_service,
            planning_engine,
            effective_start=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
    elif reason == "expired":
        _assign_plan_approver_role(
            authorization_service,
            planning_engine,
            effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            effective_end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    elif reason == "revoked":
        rid = _assign_plan_approver_role(
            authorization_service, planning_engine
        )
        _revoke_role(
            planning_engine, rid, datetime(2026, 3, 1, tzinfo=timezone.utc)
        )
    else:  # pragma: no cover - guard against typos in parametrize lists
        raise AssertionError(f"unhandled reason code: {reason!r}")

    with pytest.raises(PlanApprovalAuthorizationError) as exc_info:
        with planning_engine.begin() as conn:
            plan_approval_service.create_plan_approval(
                conn,
                planning_engine,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Approve",
                rationale=f"Should be denied with {reason}.",
                approving_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                evaluation_at=evaluation_at,
                correlation_id=correlation_id,
            )
    return exc_info.value


class TestAuthorityDenyPathFiveReasonCodes:
    """Every reason code surfaces as
    :class:`PlanApprovalAuthorizationError` with that exact code, plus
    exactly one Denial Record.
    """

    @pytest.mark.parametrize(
        "reason",
        [
            "not-yet-effective",
            "expired",
            "revoked",
            "out-of-scope",
            "no-role-assignment",
        ],
    )
    def test_each_reason_code_produces_matching_denial(
        self,
        reason: str,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_approval_service: PlanApprovalService,
    ) -> None:
        """Every Requirement 12.2 / 7.6 reason code produces a denial
        carrying that reason code on both the exception and the
        Denial Record, with no Plan Approval row, no Addresses
        Relationship, no Provenance Manifest, no lifecycle UPDATE,
        and no consequential audit row persisted.
        """
        correlation = f"corr-plan-approval-{reason}"
        exc = _trigger_plan_approval_denial(
            reason,
            planning_engine=planning_engine,
            authorization_service=authorization_service,
            plan_approval_service=plan_approval_service,
            correlation_id=correlation,
        )

        assert exc.reason_code == reason
        assert exc.correlation_id == correlation

        # No Plan Approval side-effects persisted — Requirement 10.5 /
        # 10.1 (the caller's transaction rolled back when the
        # exception propagated out of ``engine.begin()``).
        assert _count(planning_engine, "Plan_Approval_Records") == 0
        assert _count_addresses_relationships_with_source(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == 0
        assert _count(planning_engine, "Provenance_Manifests") == 0
        assert _count_consequential_audit_rows(
            planning_engine, "create.plan_approval"
        ) == 0

        # The target Plan Revision is byte-equivalent (still draft).
        assert _lifecycle_state(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == "draft"

        # Exactly one Denial Record survives in its own separate
        # transaction (Requirement 10.6 / AD-WS-9).
        assert _count_denial_audit_rows(
            planning_engine, "create.plan_approval"
        ) == 1


# ===========================================================================
# Requirement 9.1 / AD-WS-19 — session-pragma lifecycle trigger permits
# exactly the one ``draft → approved`` transition.
#
# Three observations validate the contract:
#   1. outcome='Approve'         → state transitions to 'approved'.
#   2. outcome='Reject_Approval' → state stays at 'draft'.
#   3. after the Plan Approval transaction commits, a fresh connection
#      that attempts an UPDATE on Plan_Revisions without setting the
#      pragma is rejected — the pragma window is closed.
# ===========================================================================


class TestSessionPragmaLifecycleTrigger:
    """The trigger permits exactly the one
    ``('draft','approved')`` transition while the pragma is set.
    """

    def test_approve_outcome_transitions_lifecycle_to_approved(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_approval_service: PlanApprovalService,
    ) -> None:
        """``outcome='Approve'`` runs the one permitted lifecycle
        UPDATE inside the Plan Approval transaction; after commit the
        target Plan Revision's ``lifecycle_state`` reads ``'approved'``
        on every subsequent SELECT (Requirement 9.1)."""
        _seed_required_parties(planning_engine)
        _assign_plan_approver_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        assert _lifecycle_state(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == "draft"

        with planning_engine.begin() as conn:
            result = plan_approval_service.create_plan_approval(
                conn,
                planning_engine,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Approve",
                rationale=_RATIONALE,
                approving_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
            )

        assert result.new_lifecycle_state == "approved"
        # Verified on a fresh connection so the transition is
        # confirmed across the commit boundary (not just inside the
        # service's own connection view).
        assert _lifecycle_state(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == "approved"

    def test_reject_approval_outcome_leaves_lifecycle_in_draft(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_approval_service: PlanApprovalService,
    ) -> None:
        """``outcome='Reject_Approval'`` records the rejection but
        skips the lifecycle UPDATE entirely — the target Plan Revision
        stays in ``'draft'`` (Requirement 9.1's distinction between
        the two outcomes)."""
        _seed_required_parties(planning_engine)
        _assign_plan_approver_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with planning_engine.begin() as conn:
            result = plan_approval_service.create_plan_approval(
                conn,
                planning_engine,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Reject_Approval",
                rationale="Defer pending Phase 2 scope.",
                approving_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
            )

        assert result.outcome == "Reject_Approval"
        assert result.new_lifecycle_state == "draft"
        # The Plan Approval row itself was still written — the
        # rejection is durably recorded — but the lifecycle stays
        # in draft.
        assert _count(planning_engine, "Plan_Approval_Records") == 1
        assert _lifecycle_state(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == "draft"

    def test_pragma_window_closed_after_commit_rejects_subsequent_updates(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_approval_service: PlanApprovalService,
    ) -> None:
        """After the Plan Approval transaction commits, an UPDATE on
        any ``Plan_Revisions`` row from a fresh connection (where the
        connection-private pragma is unset) is rejected by the
        AD-WS-19 trigger.

        Seeds a second draft Plan Revision so the post-commit UPDATE
        targets a row that is still in ``'draft'`` (the approved
        target itself would also be rejected, but for an additional
        reason — Requirement 9.4's approved-immutability — making the
        assertion ambiguous). The second revision is left untouched
        by the service and is the unambiguous probe for the
        pragma-window contract.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_approver_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )
        # Second draft Plan Revision — the post-commit probe.
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_SECOND_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
            planned_scope="Phase 2 scope.",
        )

        # Drive one Plan Approval against the first draft revision.
        with planning_engine.begin() as conn:
            plan_approval_service.create_plan_approval(
                conn,
                planning_engine,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Approve",
                rationale=_RATIONALE,
                approving_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
            )

        # The pragma has been cleared (the service's ``finally``
        # block ran). A fresh DBAPI connection from the pool sees an
        # empty session-state TEMP table — the trigger's
        # ``NOT EXISTS`` clause fires and the UPDATE is rejected for
        # both the just-approved row *and* the still-draft second
        # row.
        with planning_engine.connect() as conn:
            with pytest.raises(IntegrityError):
                with conn.begin():
                    conn.execute(
                        text(
                            "UPDATE Plan_Revisions "
                            "SET lifecycle_state = 'approved' "
                            "WHERE plan_revision_id = :id"
                        ),
                        {"id": _SECOND_DRAFT_PLAN_REVISION_ID},
                    )

        # The second draft revision is byte-equivalent — the
        # pragma-less UPDATE rolled back.
        assert _lifecycle_state(
            planning_engine, _SECOND_DRAFT_PLAN_REVISION_ID
        ) == "draft"


# ===========================================================================
# Requirements 9.6 / 10.6 — manifest persistence failure rolls back the
# entire transaction.
#
# When the wired ProvenanceManifestWriter raises during
# ``write_manifest``, the exception propagates out of the service.
# The caller's ``engine.begin()`` block rolls back, so every row
# inserted earlier in the transaction (the registry binding, the
# Plan_Approval_Records row, and the Addresses Relationships row)
# disappears, the lifecycle UPDATE never runs, and the consequential
# audit row never lands.
# ===========================================================================


class TestManifestFailureRollsBackEntireTransaction:
    """A failing manifest write rolls back the whole Plan Approval flow."""

    def test_manifest_failure_propagates_and_rolls_back_all_writes(
        self,
        planning_engine: Engine,
        clock: Clock,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
    ) -> None:
        """Inject a :class:`_FailingManifestWriter`. The exception
        propagates; no ``Plan_Approval_Records`` row, no ``Addresses``
        Relationship row, no consequential audit row, no
        ``Identifier_Registry`` binding for the would-be Plan Approval,
        and no lifecycle UPDATE persists. The target Plan Revision's
        lifecycle state remains ``'draft'``.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_approver_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        failing_writer = _FailingManifestWriter()
        failing_service = PlanApprovalService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
            manifest_writer=failing_writer,
        )

        # Snapshot pre-attempt counts. Only the role-assignment audit
        # row exists at this point — every other table is empty.
        pre_plan_approvals = _count(planning_engine, "Plan_Approval_Records")
        pre_relationships = _count(planning_engine, "Relationships")
        pre_manifests = _count(planning_engine, "Provenance_Manifests")
        pre_identifier_registry = _count(
            planning_engine, "Identifier_Registry"
        )
        pre_consequential = _count_consequential_audit_rows(
            planning_engine, "create.plan_approval"
        )

        with pytest.raises(_ManifestWriteFailure):
            with planning_engine.begin() as conn:
                failing_service.create_plan_approval(
                    conn,
                    planning_engine,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome="Approve",
                    rationale=_RATIONALE,
                    approving_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    correlation_id="corr-manifest-fail",
                )

        # The manifest writer was reached — regression guard against a
        # service that skips manifest writes entirely.
        assert failing_writer.call_count == 1
        assert failing_writer.last_kwargs.get("subject_kind") == (
            "plan_approval"
        )

        # Every Plan Approval-side row rolled back.
        assert _count(
            planning_engine, "Plan_Approval_Records"
        ) == pre_plan_approvals
        assert _count(
            planning_engine, "Relationships"
        ) == pre_relationships
        assert _count(
            planning_engine, "Provenance_Manifests"
        ) == pre_manifests
        # The Identifier_Registry binding for the would-be Plan
        # Approval Identity also rolled back.
        assert _count(
            planning_engine, "Identifier_Registry"
        ) == pre_identifier_registry
        # No consequential audit row landed — Requirement 9.7 / AD-WS-5
        # (the consequential append participates in the caller's
        # transaction and rolls back with it).
        assert _count_consequential_audit_rows(
            planning_engine, "create.plan_approval"
        ) == pre_consequential

        # The lifecycle UPDATE never executed (it sequences after the
        # manifest write in the AD-WS-20 flow); the target Plan
        # Revision is byte-equivalent.
        assert _lifecycle_state(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        ) == "draft"
