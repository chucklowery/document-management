"""Unit tests for :mod:`walking_slice.authorization` (task 3.2).

These tests pin the contract established in
``.kiro/specs/first-walking-slice/design.md`` §"Authorization_Service" and
the task 3.2 description. Each test below validates one of the bullets
named on the task:

- ``assign_role`` records a ``Role_Assignments`` row and a
  ``'consequential'`` ``Audit_Records`` row inside the caller's
  transaction (Requirements 12.1, 13.1).
- ``evaluate`` returns ``permit`` with an ``authority_basis`` keyed to
  the granting ``Role_Assignments.role_assignment_id`` when a Party
  holds an effective role (Requirement 7.3, 12.1, 12.3).
- ``evaluate`` returns ``deny`` with each of the five Requirement 7.2
  reason codes: ``not-yet-effective``, ``expired``, ``revoked``,
  ``out-of-scope``, ``no-role-assignment``.
- ``evaluate`` appends an evaluation row to ``Audit_Records`` with
  ``outcome`` ∈ ``{permit, deny}`` (Requirement 12.5).
- ``view``/``modify``/``approve`` are not substituted for one another
  (Requirement 12.3, 12.4).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationDecision,
    AuthorizationService,
    InvalidRoleAssignmentError,
    TargetRef,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Two Parties are seeded by default: ``_PARTY_ID`` is the subject of the
# evaluation; ``_ASSIGNING_AUTHORITY_ID`` is the Party that records the role
# assignment (it is the ``actor_party_id`` on the consequential audit row).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000000002"
_TARGET_ID = "00000000-0000-7000-8000-000000000010"
_TARGET_REVISION_ID = "00000000-0000-7000-8000-000000000011"
_SCOPE = "pilot/team-a"
_OTHER_SCOPE = "pilot/team-b"


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
    authorities: Iterable[str] = ("view", "modify", "approve"),
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2026, 1, 1, tzinfo=timezone.utc),
    effective_end: datetime | None = None,
    party_id: str = _PARTY_ID,
    role_name: str = "decision_maker",
) -> str:
    """Helper: assign a role and return its role_assignment_id."""
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


def _revoke(engine: Engine, role_assignment_id: str, revoked_at: datetime) -> None:
    """Test-only helper: set ``revoked_at`` directly via SQL.

    The slice's HTTP revocation endpoint is task 3.3; here we exercise the
    one-shot ``revoked_at`` field via the trigger contract installed in
    :mod:`walking_slice.persistence` so the unit suite can test the
    ``revoked`` reason code in isolation.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(revoked_at), "rid": role_assignment_id},
        )


def _audit_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT actor_party_id, action_type, outcome, target_id, "
                    "target_revision_id, evaluated_role_assignment_id, "
                    "authorities_required, authorities_held, reason_code, "
                    "correlation_id "
                    "FROM Audit_Records ORDER BY append_sequence"
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _role_assignment_row(engine: Engine, role_assignment_id: str) -> dict:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT role_assignment_id, party_id, role_name, scope, "
                    "authorities_granted, effective_start, effective_end, "
                    "revoked_at, assigning_authority_id, recorded_at "
                    "FROM Role_Assignments WHERE role_assignment_id = :rid"
                ),
                {"rid": role_assignment_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---------------------------------------------------------------------------
# Fixtures local to this test module.
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_engine(engine: Engine, audit_log: AuditLog) -> Engine:
    """Engine with schema + the two test Parties seeded."""
    _seed_parties(engine)
    return engine


_DEFAULT_TARGET = TargetRef(
    kind="recommendation_revision",
    id=_TARGET_ID,
    revision_id=_TARGET_REVISION_ID,
    scope=_SCOPE,
)


# ---------------------------------------------------------------------------
# assign_role — happy path
# ---------------------------------------------------------------------------


def test_assign_role_inserts_role_assignment_row(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """``assign_role`` records every Requirement 12.1 attribute on the row."""
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="decision_maker",
        scope=_SCOPE,
        authorities_granted=("view", "approve"),
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        effective_end=datetime(2026, 12, 31, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with seeded_engine.begin() as conn:
        rid = authorization_service.assign_role(conn, request)

    row = _role_assignment_row(seeded_engine, str(rid))
    assert row["party_id"] == _PARTY_ID
    assert row["role_name"] == "decision_maker"
    assert row["scope"] == _SCOPE
    assert sorted(json.loads(row["authorities_granted"])) == ["approve", "view"]
    assert row["effective_start"] == "2026-01-01T00:00:00.000Z"
    assert row["effective_end"] == "2026-12-31T00:00:00.000Z"
    assert row["assigning_authority_id"] == _ASSIGNING_AUTHORITY_ID
    assert row["revoked_at"] is None
    assert row["recorded_at"] == "2026-01-01T00:00:00.000Z"


def test_assign_role_appends_consequential_audit_row(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """``assign_role`` records a single consequential audit row per Requirement 13.1."""
    rid = _assign(authorization_service, seeded_engine)

    audit = _audit_rows(seeded_engine)
    assert len(audit) == 1
    record = audit[0]
    assert record["outcome"] == "consequential"
    assert record["action_type"] == "assign.role"
    assert record["actor_party_id"] == _ASSIGNING_AUTHORITY_ID
    assert record["target_id"] == rid
    assert record["target_revision_id"] is None
    assert record["reason_code"] is None
    assert json.loads(record["authorities_required"]) == ["modify"]


def test_assign_role_returns_canonical_uuidv7_role_assignment_id(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    import re

    canonical = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    rid = _assign(authorization_service, seeded_engine)
    assert canonical.match(rid), rid


def test_assign_role_rolls_back_on_missing_party_fk(
    authorization_service: AuthorizationService, engine: Engine, audit_log: AuditLog
) -> None:
    """An unseeded Party violates the FK on ``Role_Assignments.party_id``."""
    request = AssignRoleRequest(
        party_id="00000000-0000-7000-8000-00000000aaaa",  # not seeded
        role_name="decision_maker",
        scope=_SCOPE,
        authorities_granted=("modify",),
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            authorization_service.assign_role(conn, request)


# ---------------------------------------------------------------------------
# assign_role — validation (Requirement 12.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, missing_field",
    [
        ({"party_id": ""}, "party_id"),
        ({"role_name": ""}, "role_name"),
        ({"scope": ""}, "scope"),
        ({"authorities_granted": ()}, "authorities_granted"),
        ({"assigning_authority_id": ""}, "assigning_authority_id"),
    ],
)
def test_assign_role_rejects_missing_required_field(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    kwargs: dict,
    missing_field: str,
) -> None:
    base = dict(
        party_id=_PARTY_ID,
        role_name="decision_maker",
        scope=_SCOPE,
        authorities_granted=("modify",),
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    base.update(kwargs)
    request = AssignRoleRequest(**base)

    with pytest.raises(InvalidRoleAssignmentError) as excinfo:
        with seeded_engine.begin() as conn:
            authorization_service.assign_role(conn, request)

    assert missing_field in excinfo.value.missing


def test_assign_role_rejects_unsupported_authority(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """Authorities outside ``{view, modify, approve}`` are rejected per Requirement 12.1."""
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="decision_maker",
        scope=_SCOPE,
        authorities_granted=("approve", "publish"),  # 'publish' is not valid
        effective_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )

    with pytest.raises(InvalidRoleAssignmentError) as excinfo:
        with seeded_engine.begin() as conn:
            authorization_service.assign_role(conn, request)

    assert "publish" in excinfo.value.invalid


# ---------------------------------------------------------------------------
# evaluate — permit
# ---------------------------------------------------------------------------


def test_evaluate_returns_permit_for_effective_role(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    rid = _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        scope=_SCOPE,
        effective_start=datetime(2025, 6, 1, tzinfo=timezone.utc),
        effective_end=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        )

    assert isinstance(decision, AuthorizationDecision)
    assert decision.is_permit
    assert decision.reason_code is None
    assert decision.authority_basis is not None
    assert decision.authority_basis.type == "role-grant-id"
    assert str(decision.authority_basis.id) == rid
    assert decision.correlation_id


def test_evaluate_wildcard_scope_covers_any_target(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """Roles with scope ``"*"`` cover targets regardless of scope value."""
    _assign(
        authorization_service,
        seeded_engine,
        authorities=("view",),
        scope="*",
    )

    target = TargetRef(kind="finding_revision", id=_TARGET_ID, scope="any/team-xyz")
    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="view.finding_revision",
            target=target,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_permit


# ---------------------------------------------------------------------------
# evaluate — five denial reason codes (Requirement 7.2)
# ---------------------------------------------------------------------------


def test_evaluate_denies_no_role_assignment_when_party_has_no_role(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "no-role-assignment"
    assert decision.authority_basis is None


def test_evaluate_denies_no_role_assignment_when_authority_missing(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """A role granting only ``view`` does not count as a role for an ``approve`` action."""
    _assign(authorization_service, seeded_engine, authorities=("view",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "no-role-assignment"


def test_evaluate_denies_out_of_scope_when_role_scope_does_not_match(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        scope=_OTHER_SCOPE,  # not the target's scope
    )

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "out-of-scope"


def test_evaluate_denies_not_yet_effective_when_start_in_future(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        effective_start=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "not-yet-effective"


def test_evaluate_denies_expired_when_end_in_past(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "expired"


def test_evaluate_denies_revoked_when_revoked_at_in_past(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    rid = _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    _revoke(seeded_engine, rid, datetime(2026, 3, 1, tzinfo=timezone.utc))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "revoked"


# ---------------------------------------------------------------------------
# evaluate — audit row per evaluation (Requirement 12.5)
# ---------------------------------------------------------------------------


def test_evaluate_appends_permit_audit_row(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    rid = _assign(authorization_service, seeded_engine, authorities=("modify",))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="modify.recommendation",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
    assert decision.is_permit

    audit = _audit_rows(seeded_engine)
    # 1 row for assign_role + 1 row for the evaluation
    assert len(audit) == 2
    eval_row = audit[1]
    assert eval_row["outcome"] == "permit"
    assert eval_row["action_type"] == "modify.recommendation"
    assert eval_row["actor_party_id"] == _PARTY_ID
    assert eval_row["target_id"] == _TARGET_ID
    assert eval_row["target_revision_id"] == _TARGET_REVISION_ID
    assert eval_row["evaluated_role_assignment_id"] == rid
    assert eval_row["reason_code"] is None
    assert json.loads(eval_row["authorities_required"]) == ["modify"]
    assert "modify" in json.loads(eval_row["authorities_held"])
    assert eval_row["correlation_id"] == decision.correlation_id


def test_evaluate_appends_deny_audit_row_with_reason_code(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """A denied evaluation records ``outcome='deny'`` with the reason code (Requirement 12.5)."""
    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    audit = _audit_rows(seeded_engine)
    assert len(audit) == 1
    row = audit[0]
    assert row["outcome"] == "deny"
    assert row["reason_code"] == "no-role-assignment"
    assert row["action_type"] == "approve.decision"
    assert row["actor_party_id"] == _PARTY_ID
    assert row["correlation_id"] == decision.correlation_id
    assert row["evaluated_role_assignment_id"] is None


def test_evaluate_audit_row_participates_in_caller_transaction(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """Rolling back the caller's transaction discards the evaluation audit row (AD-WS-5)."""
    _assign(authorization_service, seeded_engine, authorities=("approve",))

    # Audit rows after assign_role: just the consequential row.
    audit_before = _audit_rows(seeded_engine)
    assert len(audit_before) == 1

    with seeded_engine.connect() as conn:
        trans = conn.begin()
        authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        trans.rollback()

    audit_after = _audit_rows(seeded_engine)
    assert audit_after == audit_before


# ---------------------------------------------------------------------------
# evaluate — view/modify/approve are not substituted (Requirement 12.3, 12.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "granted, action",
    [
        # An ``approve`` role does not satisfy ``modify.*``.
        (("approve",), "modify.recommendation"),
        # An ``approve`` role does not satisfy ``view.*``.
        (("approve",), "view.document_revision"),
        # A ``view`` role does not satisfy ``modify.*``.
        (("view",), "modify.recommendation"),
        # A ``view`` role does not satisfy ``approve.*``.
        (("view",), "approve.decision"),
        # A ``modify`` role does not satisfy ``approve.*``.
        (("modify",), "approve.decision"),
        # A ``modify`` role does not satisfy ``view.*``.
        (("modify",), "view.finding_revision"),
    ],
)
def test_evaluate_does_not_substitute_authority_types(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    granted: tuple[str, ...],
    action: str,
) -> None:
    """One authority type does not satisfy a different authority type."""
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
    assert decision.reason_code == "no-role-assignment", (
        f"role granting {granted} should not satisfy {action!r}"
    )


def test_evaluate_create_action_requires_modify_authority(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """``create.*`` actions require ``modify`` (creation is a write)."""
    _assign(authorization_service, seeded_engine, authorities=("modify",))

    target = TargetRef(kind="finding", scope=_SCOPE)
    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="create.finding",
            target=target,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_permit


def test_evaluate_rejects_malformed_action(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    with pytest.raises(ValueError, match="prefix"):
        with seeded_engine.begin() as conn:
            authorization_service.evaluate(
                conn,
                party_id=_PARTY_ID,
                action="delete.recommendation",  # unknown prefix
                target=_DEFAULT_TARGET,
                at=datetime(2026, 6, 1, tzinfo=timezone.utc),
            )


# ---------------------------------------------------------------------------
# evaluate — reason priority when multiple denial conditions apply.
# ---------------------------------------------------------------------------


def test_evaluate_picks_revoked_over_expired_across_role_assignments(
    authorization_service: AuthorizationService, seeded_engine: Engine
) -> None:
    """Revoked outranks expired in the denial reason priority."""
    expired_rid = _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=datetime(2026, 1, 1, tzinfo=timezone.utc),  # expired
    )
    revoked_rid = _assign(
        authorization_service,
        seeded_engine,
        authorities=("approve",),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    _revoke(seeded_engine, revoked_rid, datetime(2026, 3, 1, tzinfo=timezone.utc))

    with seeded_engine.begin() as conn:
        decision = authorization_service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action="approve.decision",
            target=_DEFAULT_TARGET,
            at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )

    assert decision.is_deny
    assert decision.reason_code == "revoked"
    # The first role assignment that exhibits the chosen reason is reported.
    audit = _audit_rows(seeded_engine)
    last = audit[-1]
    assert last["evaluated_role_assignment_id"] == revoked_rid
    # Make sure we didn't accidentally pick the expired-only assignment.
    assert last["evaluated_role_assignment_id"] != expired_rid
