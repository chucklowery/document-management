"""Unit tests for the Slice 3 additive extensions of
:mod:`walking_slice.authorization` (third-walking-slice task 1.5).

These tests pin the contract established in
``.kiro/specs/third-walking-slice/design.md`` §"AD-WS-24 — Additive
authority enumeration extension" and task 1.1:

- The cumulative ``_VALID_AUTHORITIES`` constant has been additively
  extended from five values (Slice 1 ``view``/``modify``/``approve`` plus
  Slice 2 ``review``) to eight by including
  ``assign``/``contribute``/``accept_milestone``/``complete``;
  :meth:`AuthorizationService.assign_role` accepts every new value
  without raising :class:`InvalidRoleAssignmentError` and persists it
  byte-equivalent to the prior values (Requirement 32.1, 40.2).
- The cumulative ``_required_authority`` derivation has been additively
  extended with the seven Slice 3 action types; each one demands
  exactly the authority documented in Requirements 32.6 — 32.9:
    * ``create.work_assignment``       → ``assign``
    * ``create.work_event``            → ``contribute``
    * ``create.time_entry``            → ``contribute``
    * ``create.produced_deliverable``  → ``contribute``
    * ``create.deliverable_production``→ ``contribute``
    * ``create.milestone_acceptance``  → ``accept_milestone``
    * ``create.completion``            → ``complete``
- Every Slice 1 and Slice 2 action continues to require its
  pre-Slice-3 authority (Requirement 40.1 — Slice 1 and Slice 2
  non-modification).
- The eight authority types are pairwise non-substitutable
  (Requirement 32.10, 32.11).

Test conventions mirror :mod:`tests.unit.test_authorization_slice2_extensions`.
The ``engine``, ``audit_log``, ``authorization_service`` fixtures come
from ``tests/conftest.py``; this module seeds two Parties (subject +
assigning authority) and relies on the Slice 1 schema produced by
``audit_log``.
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
# Seed helpers (mirrored from tests.unit.test_authorization_slice2_extensions).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000301"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000000302"
_TARGET_ID = "00000000-0000-7000-8000-000000000310"
_TARGET_REVISION_ID = "00000000-0000-7000-8000-000000000311"
_SCOPE = "pilot/team-a"


# Canonical mapping of the seven Slice 3 action types to the authority
# value each one requires. Sourced verbatim from Requirements 32.6 — 32.9
# and the override table in :mod:`walking_slice.authorization`.
_SLICE_3_ACTION_AUTHORITY: tuple[tuple[str, str], ...] = (
    ("create.work_assignment", "assign"),
    ("create.work_event", "contribute"),
    ("create.time_entry", "contribute"),
    ("create.produced_deliverable", "contribute"),
    ("create.deliverable_production", "contribute"),
    ("create.milestone_acceptance", "accept_milestone"),
    ("create.completion", "complete"),
)


# Every authority value in the cumulative Slice 1 + Slice 2 + Slice 3
# enumeration. Used by the non-substitution parametrize matrix below so a
# new authority cannot be silently substituted for any other.
_ALL_AUTHORITIES: tuple[str, ...] = (
    "view",
    "modify",
    "review",
    "approve",
    "assign",
    "contribute",
    "accept_milestone",
    "complete",
)


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
    role_name: str = "execution_role",
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: datetime | None = datetime(2027, 1, 1, tzinfo=timezone.utc),
) -> str:
    """Assign a role to ``party_id`` and return its identifier."""
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
    """Engine with the Slice 1 schema and both test Parties seeded.

    ``audit_log`` is depended on so the schema is created via the same
    path the production startup hook uses.
    """
    _seed_parties(engine)
    return engine


_DEFAULT_TARGET = TargetRef(
    kind="work_assignment_record",
    id=_TARGET_ID,
    revision_id=_TARGET_REVISION_ID,
    scope=_SCOPE,
)


# ---------------------------------------------------------------------------
# assign_role — accepts each new Slice 3 authority value (AD-WS-24)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "authority",
    ["assign", "contribute", "accept_milestone", "complete"],
)
def test_assign_role_accepts_each_new_slice_3_authority_alone(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    authority: str,
) -> None:
    """A role granting only one of the four new authorities is accepted.

    Requirement 32.1 names ``assign``, ``contribute``, ``accept_milestone``,
    and ``complete`` as first-class authority values alongside the prior
    five. Slice 1's ``InvalidRoleAssignmentError`` path for unsupported
    authorities must not fire for any of them.

    Requirements 32.2 — 32.5 additionally state that recording each new
    authority value SHALL NOT require, infer, or auto-include any other
    value in the same Role Assignment: a Role Assignment carrying only
    ``assign`` round-trips through persistence with exactly
    ``["assign"]`` on its ``authorities_granted`` column.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name=f"{authority}_role",
        scope=_SCOPE,
        authorities_granted=(authority,),
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
    assert json.loads(granted) == [authority]


def test_assign_role_accepts_all_eight_authorities_in_one_set(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """All eight authority values may co-exist on one Role Assignment.

    The cumulative enumeration is the union of Slice 1, Slice 2, and
    Slice 3 values; the persistence layer canonicalizes them to a
    sorted JSON array, so all eight round-trip in sorted order.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="super_role",
        scope=_SCOPE,
        authorities_granted=tuple(_ALL_AUTHORITIES),
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
    assert sorted(json.loads(granted)) == sorted(_ALL_AUTHORITIES)


def test_assign_role_still_rejects_authority_outside_extended_enumeration(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """A bogus value such as ``"deploy"`` is still rejected after the additive extension.

    Confirms Requirement 40.2 — the enumeration was extended additively,
    not replaced. A value outside the eight-value cumulative set is
    rejected per Slice 1 Requirement 12.6, while the four new Slice 3
    values are accepted.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="custom_role",
        scope=_SCOPE,
        authorities_granted=("assign", "deploy"),  # 'deploy' is invalid
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with pytest.raises(InvalidRoleAssignmentError) as excinfo:
        with seeded_engine.begin() as conn:
            authorization_service.assign_role(conn, request)

    assert "deploy" in excinfo.value.invalid
    assert "assign" not in excinfo.value.invalid


# ---------------------------------------------------------------------------
# evaluate — each Slice 3 action requires exactly its named authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action, required_authority", _SLICE_3_ACTION_AUTHORITY)
def test_evaluate_slice_3_action_permits_with_required_authority(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """A role granting exactly the named authority permits the named action.

    Maps Requirement 32.6 (``assign`` → ``create.work_assignment``),
    32.7 (``contribute`` → Contributor writes), 32.8 (``accept_milestone``
    → ``create.milestone_acceptance``), and 32.9 (``complete`` →
    ``create.completion``).
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

    assert isinstance(decision, AuthorizationDecision)
    assert decision.is_permit, (
        f"action {action!r} should be permitted by a role granting "
        f"{required_authority!r}; decision reason was {decision.reason_code!r}"
    )


@pytest.mark.parametrize("action, required_authority", _SLICE_3_ACTION_AUTHORITY)
def test_evaluate_slice_3_action_records_required_authority_on_audit_row(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Requirement 32.11 — the evaluation audit row identifies the
    specific authority required by the action.

    The Audit_Records row carries ``authorities_required = [<required>]``
    (a single-element JSON array) so audit consumers can read off which
    of the eight authorities the request demanded without having to
    re-derive it from the action string.
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
    assert decision.is_permit

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


def _other_authorities(required: str) -> tuple[str, ...]:
    """Return every authority value except ``required``."""
    return tuple(a for a in _ALL_AUTHORITIES if a != required)


@pytest.mark.parametrize("action, required_authority", _SLICE_3_ACTION_AUTHORITY)
def test_evaluate_slice_3_action_denies_when_only_other_authority_held(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Non-substitution — none of the other seven authorities satisfies
    the action's required authority.

    Requirement 32.10 makes the eight authority types pairwise distinct;
    a role granting only ``view``/``modify``/``review``/``approve`` or
    any of the *other* three Slice 3 authorities must NOT satisfy a
    Slice 3 action that requires its specific authority. The denial
    reason is ``no-role-assignment`` because, after filtering to roles
    that grant the required authority, the set is empty.
    """
    # Assign every other authority simultaneously to make this the
    # strongest possible non-substitution check: even holding all seven
    # other authorities together must not satisfy the named action.
    other = _other_authorities(required_authority)
    _assign(authorization_service, seeded_engine, authorities=other)

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action=action,
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny, (
        f"action {action!r} required {required_authority!r}; a role granting "
        f"{other!r} must not satisfy it under the non-substitution rule "
        f"(Requirement 32.10)"
    )
    assert decision.reason_code == "no-role-assignment"


# ---------------------------------------------------------------------------
# evaluate — pre-Slice-3 actions still demand their pre-Slice-3 authority
# (Requirement 40.1 — Slice 1 and Slice 2 non-modification of behavior)
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
        # Slice 1 modify.* / create.* requires modify.
        ("modify.recommendation", "modify"),
        ("modify.finding", "modify"),
        ("create.finding", "modify"),
        ("create.recommendation", "modify"),
        ("create.document_revision", "modify"),
        ("create.trail", "modify"),
        # Slice 1 approve.* requires approve.
        ("approve.decision", "approve"),
        # Slice 2 review/approve actions are unchanged.
        ("create.plan_review", "review"),
        ("create.plan_approval", "approve"),
        # Slice 2 create.* (default modify) actions are unchanged.
        ("create.objective", "modify"),
        ("create.intended_outcome", "modify"),
        ("create.project", "modify"),
        ("create.deliverable_expectation", "modify"),
        ("create.activity_plan", "modify"),
        ("create.plan_revision", "modify"),
    ],
)
def test_existing_actions_still_demand_pre_slice_3_authority(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Regression — every Slice 1 and Slice 2 action is byte-equivalent to its prior derivation.

    A role granting exactly the required authority permits the action and
    the audit row records that single authority on ``authorities_required``.
    The Slice 3 extension is additive only: no prior action's
    required-authority mapping has changed (Requirement 40.1 — Slice 1
    and Slice 2 non-modification).
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
        f"action {action!r} must continue to be permitted by a role granting "
        f"{required_authority!r}; got deny with reason {decision.reason_code!r}"
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
    "action",
    [
        "view.document_revision",
        "modify.recommendation",
        "create.finding",
        "approve.decision",
        "create.plan_review",
        "create.plan_approval",
        "create.objective",
        "create.plan_revision",
    ],
)
@pytest.mark.parametrize(
    "slice_3_authority",
    ["assign", "contribute", "accept_milestone", "complete"],
)
def test_pre_slice_3_actions_not_substituted_by_slice_3_authorities(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    slice_3_authority: str,
) -> None:
    """No Slice 3 authority satisfies any Slice 1 or Slice 2 action.

    Requirement 32.10 / 40.1 — the four new authorities extend the
    enumeration without altering pre-existing derivation behavior; a
    Party holding only ``assign``/``contribute``/``accept_milestone``/
    ``complete`` cannot perform a Slice 1 ``view``/``modify``/
    ``approve`` action or a Slice 2 ``review``/``modify``/``approve``
    action.
    """
    _assign(authorization_service, seeded_engine, authorities=(slice_3_authority,))

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
