# Feature: fourth-walking-slice, Property 48: Authority non-substitution across twelve types
"""Property 48 — Authority non-substitution across twelve types (task 15.3).

**Property 48: Authority non-substitution across twelve types**

*For all* outcome-measurement entities, the twelve authority types

    {view, modify, review, approve, assign, contribute, accept_milestone,
     complete, define_measurement, record_measurement, assess_outcome,
     issue_outcome_review}

are pairwise distinct in the Role-Assignment evaluation function. No
Measurement Definition exists whose authoring Party held only a single
non-``define_measurement`` authority among the twelve; no Measurement
Record exists whose recording Party held only a single
non-``record_measurement`` authority; no Observed Outcome Revision or
Success-Condition Assessment exists whose authoring Party held only a
single non-``assess_outcome`` authority; no Outcome Review exists whose
reviewing Party held only a single non-``issue_outcome_review``
authority. No prior authority is substituted for a new authority and
vice versa, in either direction.

**Validates: Requirements 52.1, 52.2, 52.3, 52.4, 52.5, 52.10, 52.11, 61.3**

Strategy
========

The pairwise-distinctness invariant of the Role-Assignment evaluation
function reduces to a single universally-quantified statement over the
shared :func:`walking_slice.authorization._required_authority` predicate
that *every* Slice 1 — Slice 4 write path consults before persisting:

    For a generated ``(action, granted_authority)`` pair, where the
    granted authority is drawn from the cumulative twelve-value
    enumeration and the action ranges over the relevant Slice 4 actions
    (and, to exercise non-substitution in *both* directions, the prior
    Slice 1/2/3 actions), ``AuthorizationService.evaluate`` PERMITS the
    action *iff* the single granted authority is exactly the one the
    action's mapping requires.

Each Hypothesis case draws one ``(action, granted_authority)`` pair and:

1. spins up a fresh per-case SQLite engine carrying the Slice 1 schema
   (Parties, Role_Assignments, Audit_Records — the tables
   :meth:`AuthorizationService.assign_role` / :meth:`evaluate` touch);
2. seeds the subject Party and the assigning-authority Party;
3. assigns the subject a single Role Assignment granting *exactly* the
   drawn authority, whose scope exactly covers the target and whose
   effective period strictly encloses the evaluation instant — so the
   *only* free variable governing the decision is whether the granted
   authority matches the action's required authority;
4. evaluates the action and asserts ``is_permit`` iff
   ``granted_authority == required_authority``.

Because scope and effective-period gating are held valid in every case,
a deny can only arise from authority non-substitution, and a permit can
only arise from an exact authority match. Quantifying over all twelve
authorities crossed with actions requiring each of the twelve
authorities therefore establishes pairwise distinctness empirically and
in both directions (prior↔new), which is exactly Property 48.

Test conventions mirror :mod:`tests.property.test_property_32_execution_authority`
(per-case ``tempfile.TemporaryDirectory`` engine, ``FixedClock`` anchor,
fresh per-case services, ``@settings(max_examples=100, deadline=2000)``,
``pytestmark = pytest.mark.property`` so the conftest seed-capture hooks
record this property's invocations).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationDecision,
    AuthorizationService,
    TargetRef,
    _required_authority,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed instants and identities.
# ---------------------------------------------------------------------------

# Every artifact in a case carries this recorded time; the assignment
# effective period strictly encloses it so the period gate is always
# satisfied and the decision turns solely on authority matching.
_NOW: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_EFFECTIVE_START: Final[datetime] = datetime(2025, 1, 1, tzinfo=timezone.utc)
_EFFECTIVE_END: Final[datetime] = datetime(2027, 1, 1, tzinfo=timezone.utc)

_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000481"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000000482"
_TARGET_ID: Final[str] = "00000000-0000-7000-8000-000000000490"
_TARGET_REVISION_ID: Final[str] = "00000000-0000-7000-8000-000000000491"
_SCOPE: Final[str] = "pilot/team-a"


# The cumulative twelve-value authority enumeration (AD-WS-33). Pinned
# here verbatim so the test fails loudly if a future change drops, renames,
# or fails to add a value rather than silently shrinking the matrix.
_TWELVE_AUTHORITIES: Final[tuple[str, ...]] = (
    "view",
    "modify",
    "review",
    "approve",
    "assign",
    "contribute",
    "accept_milestone",
    "complete",
    "define_measurement",
    "record_measurement",
    "assess_outcome",
    "issue_outcome_review",
)


# Actions exercised by the property. The set spans every one of the twelve
# required authorities so the iff is quantified in BOTH directions
# (a prior authority must not satisfy a Slice 4 action, and a Slice 4
# authority must not satisfy a prior action). The five Slice 4 actions are
# all present (Requirements 52.6 — 52.9); the prior actions cover the
# remaining authorities to complete the pairwise-distinctness matrix.
_ACTIONS: Final[tuple[str, ...]] = (
    # view  (Slice 1, via prefix fallback)
    "view.document_revision",
    "view.decision",
    # modify  (Slice 1 / Slice 2 create.* and modify.*)
    "create.finding",
    "modify.recommendation",
    "create.objective",
    "create.intended_outcome",
    "create.plan_revision",
    # review  (Slice 2)
    "create.plan_review",
    # approve  (Slice 1 / Slice 2)
    "approve.decision",
    "create.plan_approval",
    # assign  (Slice 3)
    "create.work_assignment",
    # contribute  (Slice 3)
    "create.work_event",
    "create.time_entry",
    "create.produced_deliverable",
    "create.deliverable_production",
    # accept_milestone  (Slice 3)
    "create.milestone_acceptance",
    # complete  (Slice 3)
    "create.completion",
    # --- the five Slice 4 actions (the slice under test) ---
    "create.measurement_definition",      # define_measurement (52.6)
    "create.measurement_record",          # record_measurement (52.7)
    "create.observed_outcome",            # assess_outcome     (52.8)
    "create.success_condition_assessment",  # assess_outcome   (52.8)
    "create.outcome_review",              # issue_outcome_review (52.9)
)


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    pragmas and the Slice 1 schema installed.

    Only the Slice 1 schema is needed: ``assign_role`` writes
    ``Role_Assignments`` and a consequential ``Audit_Records`` row, and
    ``evaluate`` reads ``Role_Assignments`` and appends an evaluation
    ``Audit_Records`` row. Both reference ``Parties`` by FK. No Slice 4
    table participates in the authority decision, which is exactly the
    point: the non-substitution rule lives entirely in the shared
    Authorization_Service evaluation function (AD-WS-33).
    """
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
    return engine


def _seed_party(conn, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": "2026-01-01T00:00:00.000Z"},
    )


_TARGET: Final[TargetRef] = TargetRef(
    kind="measurement_record",
    id=_TARGET_ID,
    revision_id=_TARGET_REVISION_ID,
    scope=_SCOPE,
)


@st.composite
def _pair_strategy(draw: st.DrawFn) -> dict:
    """Draw one ``(action, granted_authority)`` pair.

    ``action`` ranges over every action in :data:`_ACTIONS` (covering all
    twelve required authorities); ``granted`` ranges over the cumulative
    twelve-value enumeration. The cross product exercises both the
    matching case (permit expected) and the eleven non-matching cases per
    action (deny expected) — i.e. non-substitution in both directions.
    """
    action = draw(st.sampled_from(_ACTIONS))
    granted = draw(st.sampled_from(_TWELVE_AUTHORITIES))
    return {"action": action, "granted": granted}


# Feature: fourth-walking-slice, Property 48: Authority non-substitution across twelve types
@given(case=_pair_strategy())
@settings(max_examples=100, deadline=2000)
def test_authority_non_substitution_across_twelve_types(case: dict) -> None:
    """``evaluate`` permits the drawn action iff the single granted
    authority is exactly the action's required authority.

    Scope coverage and effective-period enclosure are held valid in every
    case, so the decision turns solely on authority matching. A permit can
    therefore only arise from an exact match and a deny only from
    non-substitution. Quantifying over all twelve authorities crossed with
    actions requiring each of the twelve establishes that the twelve types
    are pairwise distinct in the evaluation function and that no authority
    is substituted for any other in either direction
    (Requirements 52.1 — 52.5, 52.10, 52.11)."""
    # Static guard: the enumeration the test quantifies over is exactly the
    # twelve pairwise-distinct values (Requirement 52.1 / 52.10).
    assert len(_TWELVE_AUTHORITIES) == 12
    assert len(set(_TWELVE_AUTHORITIES)) == 12

    action: str = case["action"]
    granted: str = case["granted"]
    required = _required_authority(action)

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop48_",
        ignore_cleanup_errors=True,
    ) as raw_tmp:
        engine = _build_engine(Path(raw_tmp))

        # Fresh per-case services so cross-case IdentityService state cannot
        # leak. The FixedClock anchors recorded times to a single instant.
        clock = FixedClock(_NOW)
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        authorization_service = AuthorizationService(
            clock=clock,
            audit_log=audit_log,
            identity_service=identity_service,
        )

        with engine.begin() as conn:
            _seed_party(conn, _PARTY_ID, "Subject")
            _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")

        # Assign a role granting EXACTLY the single drawn authority, with a
        # scope that exactly covers the target and an effective period that
        # strictly encloses the evaluation instant.
        assign_request = AssignRoleRequest(
            party_id=_PARTY_ID,
            role_name="prop48_role",
            scope=_SCOPE,
            authorities_granted=(granted,),
            effective_start=_EFFECTIVE_START,
            effective_end=_EFFECTIVE_END,
            assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
        )
        with engine.begin() as conn:
            authorization_service.assign_role(conn, assign_request)

        with engine.begin() as conn:
            decision = authorization_service.evaluate(
                conn,
                party_id=_PARTY_ID,
                action=action,
                target=_TARGET,
                at=_NOW,
            )

        assert isinstance(decision, AuthorizationDecision)

        expected_permit = granted == required
        if expected_permit:
            assert decision.is_permit, (
                f"action {action!r} requires {required!r}; a role granting "
                f"exactly {granted!r} must PERMIT it, but the decision was a "
                f"deny with reason {decision.reason_code!r}."
            )
        else:
            assert decision.is_deny, (
                f"action {action!r} requires {required!r}; a role granting "
                f"only {granted!r} must NOT satisfy it under the "
                f"non-substitution rule (Requirements 52.10 / 52.11), but the "
                f"decision was a permit — {granted!r} was substituted for "
                f"{required!r}."
            )
            # With scope and effective period valid, the sole reason the
            # candidate role fails to satisfy the action is that it does not
            # grant the required authority.
            assert decision.reason_code == "no-role-assignment", (
                f"expected non-substitution denial reason "
                f"'no-role-assignment' for action {action!r} with granted "
                f"{granted!r}; got {decision.reason_code!r}."
            )
