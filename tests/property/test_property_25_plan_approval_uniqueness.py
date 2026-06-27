# Feature: second-walking-slice, Property 25: Plan Approval uniqueness
"""Property 25 — Plan Approval uniqueness (task 16.10).

**Property 25: Plan Approval uniqueness**

*For all* Plan Revision Identities created in any test session, at every
observation point at most one Plan Approval Immutable Record exists for
a given target Plan Revision Identity. A second Plan Approval attempt
against the same Plan Revision is rejected, leaves no second Plan
Approval Record persisted, and leaves the first Plan Approval Record
byte-equivalent to its prior state.

**Validates: Requirements 9.5, 20.10**

Strategy
========

Each Hypothesis case draws (a) the first Plan Approval's outcome ∈
``{Approve, Reject_Approval}`` and rationale text, and (b) the second
attempt's outcome and rationale. Per case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing Strategy"
   — per-case database isolation) carrying both the Slice 1 schema
   (:func:`walking_slice.persistence.create_schema`) and the Slice 2
   schema (:func:`walking_slice.planning._persistence.create_planning_schema`).
2. Seeds the actor Party plus the assigning-authority Party, the
   ``Project``, ``Activity_Plans``, and a single fresh ``draft``
   ``Plan_Revisions`` row by direct INSERT — Property 25 quantifies
   over *one* Plan Revision Identity per case.
3. Grants the actor the ``approve`` authority on the applicable scope
   (AD-WS-15 / Requirement 11.5).
4. Issues the first
   :meth:`PlanApprovalService.create_plan_approval` call. The first
   attempt is permitted by Requirement 9.5 (the target Plan Revision
   is fresh and in ``'draft'``); exactly one
   ``Plan_Approval_Records`` row is persisted.
5. Snapshots that row by SELECT-ing every persisted column in stable
   PK order. The snapshot is the byte-equivalence ground truth for
   the post-second-attempt comparison.
6. Issues a second :meth:`PlanApprovalService.create_plan_approval`
   call against the *same* Plan Revision Identity. Requirement 9.5
   rejects this attempt:

   - When the first outcome was ``'Approve'`` the Plan Revision's
     ``lifecycle_state`` has transitioned to ``'approved'`` and the
     service raises :class:`PlanApprovalTargetNotDraftError`.
   - When the first outcome was ``'Reject_Approval'`` the Plan
     Revision is still in ``'draft'`` but the UNIQUE constraint on
     ``Plan_Approval_Records.target_plan_revision_id`` (and the
     service's pre-check) rejects the second attempt with
     :class:`PlanApprovalConflictError`.

   Both rejections fall within Requirement 9.5 and Property 25's
   universal "second attempt is rejected with no second row
   persisted" clause.
7. Asserts exactly one row remains in ``Plan_Approval_Records`` after
   the second attempt and re-snapshots it. The post-snapshot is
   asserted byte-equivalent to the pre-snapshot — Property 25's
   universal quantifier.

Setup follows the conventions established by Slice 2 property tests
(per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory
state cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps).
"""

from __future__ import annotations

import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.plan_approvals import (
    PlanApprovalConflictError,
    PlanApprovalService,
    PlanApprovalTargetNotDraftError,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# Property 25 quantifies over one Plan Revision Identity per Hypothesis
# case — a single Party + Project + Activity Plan + draft Plan Revision
# row is sufficient.  The clock is pinned via :class:`FixedClock` so
# every persisted artifact in the case carries the same recorded time
# and the byte-equivalence comparison is deterministic across shrinks.
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
_SCOPE: Final[str] = "property-25/scope"

_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_DRAFT_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"


# Columns of ``Plan_Approval_Records`` snapshotted for the
# byte-equivalence check. Every persisted column is listed in stable
# order so any drift between the pre-second-attempt and the
# post-second-attempt snapshot surfaces a precise tuple diff.
_PLAN_APPROVAL_COLUMNS: Final[tuple[str, ...]] = (
    "plan_approval_id",
    "target_activity_plan_id",
    "target_plan_revision_id",
    "outcome",
    "rationale",
    "approving_party_id",
    "authority_basis_type",
    "authority_basis_id",
    "applicable_scope",
    "recorded_at",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case identifiers, audit rows, and seeded
# pipelines cannot leak between cases (design §"Testing Strategy").
# :class:`tempfile.TemporaryDirectory` owns the per-case directory;
# function-scoped pytest fixtures cannot be used here because
# Hypothesis does not reset them between drawn inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    and both the Slice 1 and Slice 2 schemas installed."""
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
# Prerequisites are written through direct INSERT (rather than through
# the corresponding Planning_Service) so each property case exercises
# exactly one operation under test — Property 25's uniqueness
# invariant — and a shrunken counterexample stays actionable.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the actor Party and the assigning-authority Party."""
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_ID, "Property 25 Actor"),
            (_ASSIGNING_AUTHORITY_ID, "Property 25 Resource Steward"),
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


def _assign_approve_role(
    authorization_service: AuthorizationService, engine: Engine
) -> None:
    """Grant ``approve`` authority over ``_SCOPE`` to the actor Party.

    AD-WS-15 / Requirement 11.5 maps ``create.plan_approval`` to the
    ``approve`` authority. The role-assignment effective period
    generously brackets the fixed clock instant so a shrunken case
    never fails on timing.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="plan_approver",
        scope=_SCOPE,
        authorities_granted=("approve",),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_project(engine: Engine) -> None:
    """Insert one ``Projects`` row by direct INSERT."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _NOW_ISO},
        )


def _seed_activity_plan(engine: Engine) -> None:
    """Insert one ``Activity_Plans`` row by direct INSERT."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 25 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_draft_plan_revision(engine: Engine) -> None:
    """Insert one ``Plan_Revisions`` row with ``lifecycle_state='draft'``.

    Plan_Revisions accepts an INSERT directly: the AD-WS-19 lifecycle
    UPDATE trigger fires only on UPDATE, so a fresh draft revision can
    be seeded with no session-pragma plumbing.
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
                    'Property 25 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DRAFT_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Snapshot helper.
#
# Reads every ``Plan_Approval_Records`` row in stable PK order and
# returns the rows as a tuple of column tuples so the
# byte-equivalence comparison reduces to one ``==``. Storing the
# full row tuple (rather than a hex digest) keeps a failing assertion
# informative — Hypothesis prints the differing tuples directly.
# ---------------------------------------------------------------------------


def _snapshot_plan_approval_rows(
    engine: Engine,
) -> tuple[tuple[Any, ...], ...]:
    """Return every ``Plan_Approval_Records`` row in PK order."""
    columns = ", ".join(_PLAN_APPROVAL_COLUMNS)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT {columns} FROM Plan_Approval_Records "
                "ORDER BY plan_approval_id"
            )
        ).all()
    return tuple(tuple(row) for row in rows)


def _count_rows(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


# ---------------------------------------------------------------------------
# Per-case service factory.
#
# Fresh services per Hypothesis case so :class:`IdentityService`'s
# in-memory issued-identifier set and any audit-correlation
# accumulator cannot bleed across shrinks.
# ---------------------------------------------------------------------------


def _build_plan_approval_service() -> tuple[
    PlanApprovalService, AuthorizationService
]:
    """Construct the per-case :class:`PlanApprovalService` + collaborators."""
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    manifest_writer = ProvenanceManifestWriter(
        clock=clock,
        identity_service=identity_service,
    )
    service = PlanApprovalService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        manifest_writer=manifest_writer,
    )
    return service, authorization_service


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# Each case draws a first-attempt outcome / rationale and a
# second-attempt outcome / rationale.  Outcome is drawn from the
# Requirement 9.2 enumeration; rationale is constrained to 1..200
# characters from a narrow alphabet so cases stay readable when
# Hypothesis shrinks (Property 25 is not about UTF-8 robustness).
# Both outcomes are independent — when the first is ``'Approve'`` the
# second rejection path is :class:`PlanApprovalTargetNotDraftError`;
# when the first is ``'Reject_Approval'`` the second rejection path
# is :class:`PlanApprovalConflictError`. Both fall within Requirement
# 9.5 and both satisfy Property 25.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Strategy for a non-control text run of ``min_size..max_size`` chars."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


_double_approval_strategy = st.fixed_dictionaries(
    {
        "first_outcome": st.sampled_from(["Approve", "Reject_Approval"]),
        "first_rationale": _bounded_text(1, 200),
        "second_outcome": st.sampled_from(["Approve", "Reject_Approval"]),
        "second_rationale": _bounded_text(1, 200),
    }
)


# ===========================================================================
# Property 25 — Plan Approval uniqueness.
# ===========================================================================


# Feature: second-walking-slice, Property 25: Plan Approval uniqueness
@given(payload=_double_approval_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_double_plan_approval_rejected_and_first_record_byte_equivalent(
    payload: dict[str, Any],
) -> None:
    """**Validates: Requirements 9.5, 20.10**

    For any fresh Draft Plan Revision and any pair of authorized Plan
    Approval attempts against it:

    - The first attempt persists exactly one ``Plan_Approval_Records``
      row.
    - The second attempt is rejected (Requirement 9.5):
      :class:`PlanApprovalTargetNotDraftError` when the first outcome
      was ``'Approve'`` (the Plan Revision has transitioned to
      ``'approved'``); :class:`PlanApprovalConflictError` when the
      first outcome was ``'Reject_Approval'`` (the UNIQUE constraint
      on ``target_plan_revision_id`` fires).
    - No second ``Plan_Approval_Records`` row is persisted.
    - The first ``Plan_Approval_Records`` row is byte-equivalent
      before and after the second attempt — every persisted column
      (including ``recorded_at``) is unchanged.
    """
    first_outcome = payload["first_outcome"]
    first_rationale = payload["first_rationale"]
    second_outcome = payload["second_outcome"]
    second_rationale = payload["second_rationale"]

    with tempfile.TemporaryDirectory(prefix="prop25_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            service, authorization_service = _build_plan_approval_service()

            _seed_parties(engine)
            _assign_approve_role(authorization_service, engine)
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_draft_plan_revision(engine)

            # 1. First Plan Approval — must succeed and persist exactly
            # one row.
            with engine.begin() as conn:
                first_result = service.create_plan_approval(
                    conn,
                    engine,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=first_outcome,
                    rationale=first_rationale,
                    approving_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    correlation_id="prop25-first",
                )

            assert _count_rows(engine, "Plan_Approval_Records") == 1
            assert (
                first_result.target_plan_revision_id
                == _DRAFT_PLAN_REVISION_ID
            )

            # 2. Snapshot the persisted Plan Approval row before the
            # second attempt. This snapshot is the byte-equivalence
            # ground truth.
            pre_snapshot = _snapshot_plan_approval_rows(engine)
            assert len(pre_snapshot) == 1

            # 3. Second Plan Approval against the SAME Plan Revision —
            # must be rejected. Both rejection paths satisfy
            # Requirement 9.5 and Property 25's universal quantifier.
            with pytest.raises(
                (PlanApprovalConflictError, PlanApprovalTargetNotDraftError)
            ) as exc_info:
                with engine.begin() as conn:
                    service.create_plan_approval(
                        conn,
                        engine,
                        target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                        outcome=second_outcome,
                        rationale=second_rationale,
                        approving_party_id=_PARTY_ID,
                        authority_basis=_BASIS,
                        applicable_scope=_SCOPE,
                        correlation_id="prop25-second",
                    )

            # The rejection path matches the first outcome — Approve
            # transitioned the Plan Revision and surfaces the
            # not-draft check; Reject_Approval left the Plan Revision
            # in 'draft' and surfaces the uniqueness pre-check.
            if first_outcome == "Approve":
                assert isinstance(
                    exc_info.value, PlanApprovalTargetNotDraftError
                )
                assert exc_info.value.lifecycle_state == "approved"
            else:
                assert isinstance(
                    exc_info.value, PlanApprovalConflictError
                )
                assert (
                    exc_info.value.existing_plan_approval_id
                    == first_result.plan_approval_id
                )
                assert exc_info.value.failed_constraint == (
                    "plan_approval_already_recorded"
                )

            # 4. Still exactly one row in Plan_Approval_Records — no
            # second row was persisted.
            assert _count_rows(engine, "Plan_Approval_Records") == 1

            # 5. The first Plan Approval row is byte-equivalent
            # before and after the second attempt (Property 25's
            # universal quantifier).
            post_snapshot = _snapshot_plan_approval_rows(engine)
            assert post_snapshot == pre_snapshot
        finally:
            engine.dispose()
