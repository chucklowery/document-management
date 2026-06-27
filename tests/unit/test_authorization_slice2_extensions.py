"""Unit tests for the Slice 2 additive extensions of
:mod:`walking_slice.authorization` (second-walking-slice task 1.5).

These tests pin the contract established in
``.kiro/specs/second-walking-slice/design.md`` §"AD-WS-15 — Additive
``review`` authority value" and task 1.1:

- The Slice 1 ``_VALID_AUTHORITIES`` constant has been additively
  extended to include ``"review"``; :meth:`AuthorizationService.assign_role`
  accepts the new value without raising
  :class:`InvalidRoleAssignmentError` and persists it byte-equivalent
  to the Slice 1 values (Requirement 11.1, 19.2).
- The Slice 1 ``_required_authority`` derivation has been additively
  extended with the eight Slice 2 planning actions; in particular,
  ``create.plan_review`` requires the new ``review`` authority and
  ``create.plan_approval`` continues to require ``approve``
  (Requirement 11.4, 11.5, 11.6 — non-substitution between any of
  the four authority types).
- Every Slice 1 action continues to require its pre-Slice-2 authority:
  ``view.*`` → ``view``, ``modify.*`` → ``modify``,
  ``create.*`` (Slice 1) → ``modify``, ``approve.*`` → ``approve``
  (Requirement 19.1 — Slice 1 non-modification).

Test conventions match :mod:`tests.unit.test_authorization`. The
``engine``, ``audit_log``, ``authorization_service`` fixtures come from
``tests/conftest.py``; this module seeds two Parties (subject + assigning
authority) and relies on the Slice 1 schema produced by ``audit_log``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationDecision,
    AuthorizationService,
    InvalidRoleAssignmentError,
    TargetRef,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers (mirrored from test_authorization.py for local clarity).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000000002"
_TARGET_ID = "00000000-0000-7000-8000-000000000010"
_TARGET_REVISION_ID = "00000000-0000-7000-8000-000000000011"
_SCOPE = "pilot/team-a"


def _seed_party(conn, party_id: str, display: str = "Test Party") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": "2026-01-01T00:00:00.000Z"},
    )


def _seed_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Subject")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _assign(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    authorities: Iterable[str],
    scope: str = _SCOPE,
    party_id: str = _PARTY_ID,
    role_name: str = "plan_reviewer",
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: datetime | None = datetime(2027, 1, 1, tzinfo=timezone.utc),
) -> str:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=tuple(authorities),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


@pytest.fixture
def seeded_engine(engine: Engine, audit_log: AuditLog) -> Engine:
    """Engine with the Slice 1 schema + the two test Parties seeded."""
    _seed_parties(engine)
    return engine


_DEFAULT_TARGET = TargetRef(
    kind="plan_revision",
    id=_TARGET_ID,
    revision_id=_TARGET_REVISION_ID,
    scope=_SCOPE,
)


# ---------------------------------------------------------------------------
# assign_role — accepts the new ``review`` authority value (AD-WS-15)
# ---------------------------------------------------------------------------


def test_assign_role_accepts_review_authority_alone(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """A role granting only ``review`` is accepted by ``assign_role``.

    Requirement 11.1 — ``review`` is a first-class authority value
    alongside ``view``/``modify``/``approve``. Slice 1's
    ``InvalidRoleAssignmentError`` path for unsupported authorities must
    not fire here.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="plan_reviewer",
        scope=_SCOPE,
        authorities_granted=("review",),
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with seeded_engine.begin() as conn:
        rid = authorization_service.assign_role(conn, request)

    with seeded_engine.connect() as conn:
        granted = conn.execute(
            text(
                "SELECT authorities_granted FROM Role_Assignments "
                "WHERE role_assignment_id = :rid"
            ),
            {"rid": str(rid)},
        ).scalar_one()
    assert json.loads(granted) == ["review"]


def test_assign_role_accepts_review_in_mixed_authority_set(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """``review`` may be combined with any of the other three values."""
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="plan_lead",
        scope=_SCOPE,
        authorities_granted=("view", "modify", "review", "approve"),
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with seeded_engine.begin() as conn:
        rid = authorization_service.assign_role(conn, request)

    with seeded_engine.connect() as conn:
        granted = conn.execute(
            text(
                "SELECT authorities_granted FROM Role_Assignments "
                "WHERE role_assignment_id = :rid"
            ),
            {"rid": str(rid)},
        ).scalar_one()
    # The persistence layer canonicalizes to a sorted JSON array.
    assert sorted(json.loads(granted)) == ["approve", "modify", "review", "view"]


def test_assign_role_still_rejects_authority_outside_enumeration(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """Authorities outside the four-value set continue to be rejected.

    Confirms the enumeration was extended additively, not replaced; a
    bogus value such as ``"publish"`` is still rejected per Slice 1
    Requirement 12.6.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="custom_role",
        scope=_SCOPE,
        authorities_granted=("review", "publish"),  # 'publish' is invalid
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with pytest.raises(InvalidRoleAssignmentError) as excinfo:
        with seeded_engine.begin() as conn:
            authorization_service.assign_role(conn, request)

    assert "publish" in excinfo.value.invalid
    assert "review" not in excinfo.value.invalid


# ---------------------------------------------------------------------------
# evaluate — ``create.plan_review`` requires ``review`` (AD-WS-15, Req 11.4)
# ---------------------------------------------------------------------------


def test_evaluate_create_plan_review_permits_with_review_authority(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """A role granting ``review`` permits ``create.plan_review``."""
    _assign(authorization_service, seeded_engine, authorities=("review",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="create.plan_review",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert isinstance(decision, AuthorizationDecision)
    assert decision.is_permit, decision.reason_code


def test_evaluate_create_plan_review_records_review_on_authorities_required(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """The evaluation audit row records ``["review"]`` as the required authority."""
    _assign(authorization_service, seeded_engine, authorities=("review",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="create.plan_review",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    assert decision.is_permit

    with seeded_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT authorities_required FROM Audit_Records "
                    "WHERE action_type = 'create.plan_review' "
                    "ORDER BY append_sequence DESC LIMIT 1"
                )
            )
            .mappings()
            .one()
        )
    assert json.loads(row["authorities_required"]) == ["review"]


@pytest.mark.parametrize("granted", [("view",), ("modify",), ("approve",)])
def test_evaluate_create_plan_review_denies_when_only_other_authority_held(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    granted: tuple[str, ...],
) -> None:
    """Non-substitution — ``view``/``modify``/``approve`` do not satisfy ``review``.

    Requirement 11.6 makes Plan Reviewer authority non-interchangeable
    with any other authority type. A role granting only ``approve``
    must NOT satisfy ``create.plan_review``.
    """
    _assign(authorization_service, seeded_engine, authorities=granted)

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="create.plan_review",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "no-role-assignment", (
        f"role granting {granted} should not satisfy 'create.plan_review' "
        f"under the non-substitution rule"
    )


# ---------------------------------------------------------------------------
# evaluate — ``create.plan_approval`` requires ``approve``; non-substitution
# ---------------------------------------------------------------------------


def test_evaluate_create_plan_approval_permits_with_approve_authority(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    _assign(authorization_service, seeded_engine, authorities=("approve",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="create.plan_approval",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_permit, decision.reason_code


def test_evaluate_create_plan_approval_denies_review_only_role(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """Requirement 11.5/11.6 — ``review`` does not substitute for ``approve``.

    A Plan Reviewer cannot finalize a Plan Approval; this is the
    inverse non-substitution direction of the previous test set.
    """
    _assign(authorization_service, seeded_engine, authorities=("review",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="create.plan_approval",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "no-role-assignment"


# ---------------------------------------------------------------------------
# evaluate — Slice 2 ``create.*`` actions requiring ``modify``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        "create.objective",
        "create.intended_outcome",
        "create.project",
        "create.deliverable_expectation",
        "create.activity_plan",
        "create.plan_revision",
    ],
)
def test_evaluate_slice2_create_actions_require_modify_authority(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
) -> None:
    """The six Slice 2 ``create.*`` actions that do not need ``review``/``approve``
    require ``modify`` (AD-WS-15, Requirements 11.1–11.3, 11.7)."""
    _assign(authorization_service, seeded_engine, authorities=("modify",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action=action,
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    assert decision.is_permit, decision.reason_code


@pytest.mark.parametrize(
    "action",
    [
        "create.objective",
        "create.intended_outcome",
        "create.project",
        "create.deliverable_expectation",
        "create.activity_plan",
        "create.plan_revision",
    ],
)
@pytest.mark.parametrize("granted", [("view",), ("review",), ("approve",)])
def test_evaluate_slice2_modify_actions_reject_other_authorities(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    granted: tuple[str, ...],
) -> None:
    """Non-substitution — the six Slice 2 ``create.*`` modify actions
    are not satisfied by ``view``/``review``/``approve``."""
    _assign(authorization_service, seeded_engine, authorities=granted)

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action=action,
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "no-role-assignment"


# ---------------------------------------------------------------------------
# evaluate — Slice 1 actions still demand their pre-Slice-2 authority
# (Requirement 19.1 — Slice 1 non-modification of derivation behavior)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action, required_authority",
    [
        # Slice 1 view.* requires view.
        ("view.document_revision", "view"),
        ("view.finding_revision", "view"),
        ("view.recommendation_revision", "view"),
        ("view.decision", "view"),
        ("view.trail_revision", "view"),
        # Slice 1 modify.* requires modify.
        ("modify.recommendation", "modify"),
        ("modify.finding", "modify"),
        # Slice 1 create.* requires modify (creation is a write).
        ("create.finding", "modify"),
        ("create.recommendation", "modify"),
        ("create.document_revision", "modify"),
        ("create.trail", "modify"),
        # Slice 1 approve.* requires approve.
        ("approve.decision", "approve"),
    ],
)
def test_existing_slice_1_actions_still_demand_pre_slice_2_authority(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Regression — Slice 1 actions are byte-equivalent to their pre-Slice-2 derivation.

    A role granting exactly the required authority permits the action;
    the audit row records that single authority on
    ``authorities_required``. The extension is additive only: no Slice 1
    action's required-authority mapping has changed.
    """
    _assign(authorization_service, seeded_engine, authorities=(required_authority,))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action=action,
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_permit, (
        f"Slice 1 action {action!r} must continue to be permitted by "
        f"a role granting {required_authority!r}"
    )

    with seeded_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT authorities_required FROM Audit_Records "
                    "WHERE action_type = :a "
                    "ORDER BY append_sequence DESC LIMIT 1"
                ),
                {"a": action},
            )
            .mappings()
            .one()
        )
    assert json.loads(row["authorities_required"]) == [required_authority]


@pytest.mark.parametrize(
    "action, required_authority",
    [
        ("view.document_revision", "view"),
        ("modify.recommendation", "modify"),
        ("create.finding", "modify"),
        ("approve.decision", "approve"),
    ],
)
def test_existing_slice_1_actions_not_substituted_by_review(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """``review`` does not satisfy any Slice 1 action.

    Slice 1's authority enumeration was three values; Slice 2 adds a
    fourth. Requirement 11.6 makes all four mutually non-substitutable,
    so a ``review``-only role cannot satisfy a Slice 1 ``view``/
    ``modify``/``approve`` action.
    """
    del required_authority  # Documented for parametrize readability only.
    _assign(authorization_service, seeded_engine, authorities=("review",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action=action,
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "no-role-assignment"
