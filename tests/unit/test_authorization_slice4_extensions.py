"""Unit tests for the Slice 4 additive extensions of
:mod:`walking_slice.authorization` (fourth-walking-slice task 1.4).

These tests pin the contract established in
``.kiro/specs/fourth-walking-slice/design.md`` §"AD-WS-33 — Additive
``define_measurement``, ``record_measurement``, ``assess_outcome``,
``issue_outcome_review`` authority values" and task 1.1:

- The cumulative ``_VALID_AUTHORITIES`` constant has been additively
  extended from the Slice 3 eight-value set
  (``view``/``modify``/``review``/``approve``/``assign``/``contribute``/
  ``accept_milestone``/``complete``) to twelve values by including
  ``define_measurement``/``record_measurement``/``assess_outcome``/
  ``issue_outcome_review``; :meth:`AuthorizationService.assign_role`
  accepts every new value without raising
  :class:`InvalidRoleAssignmentError` and persists it byte-equivalent to
  the prior values (Requirement 52.1, 60.2).
- The cumulative ``_required_authority`` derivation has been additively
  extended with the five Slice 4 action types; each one demands exactly
  the authority documented in Requirements 52.6 — 52.9:
    * ``create.measurement_definition``       → ``define_measurement``
    * ``create.measurement_record``           → ``record_measurement``
    * ``create.observed_outcome``             → ``assess_outcome``
    * ``create.success_condition_assessment`` → ``assess_outcome``
    * ``create.outcome_review``               → ``issue_outcome_review``
- Every Slice 1, Slice 2, and Slice 3 action continues to require its
  pre-Slice-4 authority (Requirement 60.1, 60.2 — prior-slice
  non-modification).
- The twelve authority types are pairwise non-substitutable
  (Requirement 52.10, 52.11).

Test conventions mirror :mod:`tests.unit.test_authorization_slice3_extensions`.
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
# Seed helpers (mirrored from tests.unit.test_authorization_slice3_extensions).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000401"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000000402"
_TARGET_ID = "00000000-0000-7000-8000-000000000410"
_TARGET_REVISION_ID = "00000000-0000-7000-8000-000000000411"
_SCOPE = "pilot/team-a"


# The four authority values Slice 4 adds (AD-WS-33, Requirement 52.1).
_SLICE_4_AUTHORITIES: tuple[str, ...] = (
    "define_measurement",
    "record_measurement",
    "assess_outcome",
    "issue_outcome_review",
)


# Canonical mapping of the five Slice 4 action types to the authority
# value each one requires. Sourced verbatim from Requirements 52.6 — 52.9
# and the override table in :mod:`walking_slice.authorization`.
_SLICE_4_ACTION_AUTHORITY: tuple[tuple[str, str], ...] = (
    ("create.measurement_definition", "define_measurement"),
    ("create.measurement_record", "record_measurement"),
    ("create.observed_outcome", "assess_outcome"),
    ("create.success_condition_assessment", "assess_outcome"),
    ("create.outcome_review", "issue_outcome_review"),
)


# Every authority value in the cumulative Slice 1 + Slice 2 + Slice 3 +
# Slice 4 enumeration. Used by the non-substitution parametrize matrix
# below so a new authority cannot be silently substituted for any other.
_ALL_AUTHORITIES: tuple[str, ...] = (
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
    role_name: str = "outcome_role",
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
    kind="measurement_record",
    id=_TARGET_ID,
    revision_id=_TARGET_REVISION_ID,
    scope=_SCOPE,
)


# ---------------------------------------------------------------------------
# assign_role — accepts each new Slice 4 authority value (AD-WS-33)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("authority", list(_SLICE_4_AUTHORITIES))
def test_assign_role_accepts_each_new_slice_4_authority_alone(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    authority: str,
) -> None:
    """A role granting only one of the four new authorities is accepted.

    Requirement 52.1 names ``define_measurement``, ``record_measurement``,
    ``assess_outcome``, and ``issue_outcome_review`` as first-class
    authority values alongside the prior eight. Slice 1's
    ``InvalidRoleAssignmentError`` path for unsupported authorities must
    not fire for any of them, and a Role Assignment carrying only the new
    value round-trips through persistence with exactly ``[authority]`` on
    its ``authorities_granted`` column (no other value is inferred or
    auto-included).
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


def test_assign_role_accepts_all_twelve_authorities_in_one_set(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """All twelve authority values may co-exist on one Role Assignment.

    The cumulative enumeration is the union of Slice 1, Slice 2, Slice 3,
    and Slice 4 values; the persistence layer canonicalizes them to a
    sorted JSON array, so all twelve round-trip in sorted order.
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
    assert len(_ALL_AUTHORITIES) == 12


def test_assign_role_still_rejects_authority_outside_extended_enumeration(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """A bogus value such as ``"measure"`` is still rejected after the additive extension.

    Confirms Requirement 60.2 — the enumeration was extended additively,
    not replaced. A value outside the twelve-value cumulative set is
    rejected per Slice 1 Requirement 12.6, while the four new Slice 4
    values are accepted.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="custom_role",
        scope=_SCOPE,
        authorities_granted=("define_measurement", "measure"),  # 'measure' is invalid
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with pytest.raises(InvalidRoleAssignmentError) as excinfo:
        with seeded_engine.begin() as conn:
            authorization_service.assign_role(conn, request)

    assert "measure" in excinfo.value.invalid
    assert "define_measurement" not in excinfo.value.invalid


# ---------------------------------------------------------------------------
# evaluate — each Slice 4 action requires exactly its named authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action, required_authority", _SLICE_4_ACTION_AUTHORITY)
def test_evaluate_slice_4_action_permits_with_required_authority(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """A role granting exactly the named authority permits the named action.

    Maps Requirement 52.6 (``define_measurement`` →
    ``create.measurement_definition``), 52.7 (``record_measurement`` →
    ``create.measurement_record``), 52.8 (``assess_outcome`` →
    ``create.observed_outcome`` and ``create.success_condition_assessment``),
    and 52.9 (``issue_outcome_review`` → ``create.outcome_review``).
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


@pytest.mark.parametrize("action, required_authority", _SLICE_4_ACTION_AUTHORITY)
def test_evaluate_slice_4_action_records_required_authority_on_audit_row(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Requirement 52.11 — the evaluation audit row identifies the
    specific authority required by the action.

    The Audit_Records row carries ``authorities_required = [<required>]``
    (a single-element JSON array) so audit consumers can read off which
    of the twelve authorities the request demanded without having to
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


@pytest.mark.parametrize("action, required_authority", _SLICE_4_ACTION_AUTHORITY)
def test_evaluate_slice_4_action_denies_when_only_other_authority_held(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Non-substitution — none of the other eleven authorities satisfies
    the action's required authority.

    Requirement 52.10 makes the twelve authority types pairwise distinct;
    a role granting every authority *except* the named one must NOT
    satisfy a Slice 4 action that requires its specific authority. The
    denial reason is ``no-role-assignment`` because, after filtering to
    roles that grant the required authority, the set is empty.
    """
    # Assign every other authority simultaneously to make this the
    # strongest possible non-substitution check: even holding all eleven
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
        f"(Requirement 52.10)"
    )
    assert decision.reason_code == "no-role-assignment"


# ---------------------------------------------------------------------------
# evaluate — pre-Slice-4 actions still demand their pre-Slice-4 authority
# (Requirement 60.1, 60.2 — Slice 1/2/3 non-modification of behavior)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action, required_authority",
    [
        # Slice 1 view.* requires view.
        ("view.document_revision", "view"),
        ("view.decision", "view"),
        # Slice 1 modify.* / create.* requires modify.
        ("modify.recommendation", "modify"),
        ("create.finding", "modify"),
        ("create.recommendation", "modify"),
        # Slice 1 approve.* requires approve.
        ("approve.decision", "approve"),
        # Slice 2 review/approve actions are unchanged.
        ("create.plan_review", "review"),
        ("create.plan_approval", "approve"),
        # Slice 2 create.* (default modify) actions are unchanged.
        ("create.objective", "modify"),
        ("create.intended_outcome", "modify"),
        ("create.plan_revision", "modify"),
        # Slice 3 actions are unchanged.
        ("create.work_assignment", "assign"),
        ("create.work_event", "contribute"),
        ("create.time_entry", "contribute"),
        ("create.produced_deliverable", "contribute"),
        ("create.deliverable_production", "contribute"),
        ("create.milestone_acceptance", "accept_milestone"),
        ("create.completion", "complete"),
    ],
)
def test_existing_actions_still_demand_pre_slice_4_authority(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    required_authority: str,
) -> None:
    """Regression — every Slice 1, Slice 2, and Slice 3 action is
    byte-equivalent to its prior derivation.

    A role granting exactly the required authority permits the action and
    the audit row records that single authority on ``authorities_required``.
    The Slice 4 extension is additive only: no prior action's
    required-authority mapping has changed (Requirement 60.1, 60.2).
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
        "create.work_assignment",
        "create.completion",
        "create.milestone_acceptance",
    ],
)
@pytest.mark.parametrize("slice_4_authority", list(_SLICE_4_AUTHORITIES))
def test_pre_slice_4_actions_not_substituted_by_slice_4_authorities(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    action: str,
    slice_4_authority: str,
) -> None:
    """No Slice 4 authority satisfies any Slice 1, Slice 2, or Slice 3 action.

    Requirement 52.10 / 60.1 — the four new authorities extend the
    enumeration without altering pre-existing derivation behavior; a
    Party holding only ``define_measurement``/``record_measurement``/
    ``assess_outcome``/``issue_outcome_review`` cannot perform a prior-slice
    action.
    """
    _assign(authorization_service, seeded_engine, authorities=(slice_4_authority,))

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
