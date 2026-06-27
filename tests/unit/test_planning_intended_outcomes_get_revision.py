"""Unit tests for :meth:`IntendedOutcomeService.get_revision` (task 2.2).

Pins the contract of the additive Slice 4 Planning_Service read API
introduced by task 2.1 / AD-WS-40 (fourth walking slice design
§"AD-WS-40 — Cross-context resolution uses existing Planning_Service,
Execution_Service, and Deliverable_Repository read APIs").

The Outcome_Service resolves a Measurement Definition's, Observed
Outcome's, Success-Condition Assessment's, or Outcome Review's target
Intended Outcome Revision through this read and verifies its
``outcome_kind`` discriminator equals the literal ``'intended'``
(Requirements 44.4, 47.4) before recording any outcome-measurement
artifact. The read is a pure projection of the persisted Slice 2 row and
never mutates it (Requirement 60.1).

Coverage (task 2.2):

- **44.4 / 47.4** — ``get_revision`` returns the correct ``outcome_kind``
  (the literal ``'intended'``) and the target Intended Outcome Resource
  Identity (``intended_outcome_id``) for a known Intended Outcome
  Revision, along with every other persisted column byte-equivalent to
  the row written by the Slice 2 service.
- **44.4 / 47.4** — ``get_revision`` returns a structured not-found
  indication (``None``) for an unresolvable Revision identifier; the
  caller treats ``None`` as the unresolvable branch.
- **60.1** — ``get_revision`` performs no mutation of any Slice 2 row: a
  byte-equivalent snapshot of the Intended Outcome tables is unchanged
  across one and across repeated reads.

The tests mirror the fixture and seed-helper style of
``tests/unit/test_planning_intended_outcomes.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.intended_outcomes import (
    IntendedOutcomeRevisionRow,
    IntendedOutcomeService,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

# A syntactically valid UUIDv7 that is never minted into the schema, used
# to exercise the unresolvable-identifier branch.
_UNRESOLVABLE_REV_ID = "00000000-0000-7000-8000-0000000fffff"


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Per-test engine carrying both Slice 1 and Slice 2 schemas."""
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def intended_outcome_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> IntendedOutcomeService:
    """IntendedOutcomeService wired with a real AuthorizationService."""
    return IntendedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers (mirror tests/unit/test_planning_intended_outcomes.py).
# ---------------------------------------------------------------------------


def _seed_party(conn, party_id: str, display: str) -> None:
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
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_objective(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id,
                    parent_revision_id, statement, rationale,
                    target_decision_id, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :oid, NULL,
                    'Adopt service-mesh telemetry.',
                    'Anchored on the accepted decision.',
                    :did, :pid, :scope, :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REV_ID,
                "oid": _OBJECTIVE_ID,
                "did": _DECISION_ID,
                "pid": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_intended_outcome_owner_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Intended Outcome Owner authority (``modify``) to ``party_id``."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="intended_outcome_owner",
        scope=scope,
        authorities_granted=("modify",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _create_intended_outcome(
    intended_outcome_service: IntendedOutcomeService,
    engine: Engine,
    *,
    success_condition: str = "Onboarding completes in under two days.",
    observation_window: Optional[str] = "30 days post launch",
    attribution_assumption: Optional[str] = "Sampling rate held constant.",
):
    """Create one Intended Outcome through the Slice 2 service.

    Returns the :class:`CreateIntendedOutcomeResult` so each test can use
    the real persisted Resource and Revision Identities.
    """
    with engine.begin() as conn:
        return intended_outcome_service.create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition=success_condition,
            observation_window=observation_window,
            attribution_assumption=attribution_assumption,
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )


# ---------------------------------------------------------------------------
# No-mutation snapshot helper (Requirement 60.1).
# ---------------------------------------------------------------------------

# The two Slice 2 tables the read touches / belongs to. Snapshotting the
# full content of both lets the no-mutation tests assert byte-equivalence
# across a read.
_SLICE2_OUTCOME_TABLES = ("Intended_Outcomes", "Intended_Outcome_Revisions")


def _snapshot(engine: Engine) -> dict[str, list[tuple]]:
    """Return a deterministic, ordered snapshot of the outcome tables."""
    snapshot: dict[str, list[tuple]] = {}
    with engine.connect() as conn:
        for table in _SLICE2_OUTCOME_TABLES:
            rows = conn.execute(
                text(f"SELECT * FROM {table} ORDER BY 1")
            ).all()
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


# ===========================================================================
# Requirement 44.4 / 47.4 — resolved Revision returns outcome_kind +
# target Intended Outcome Resource Identity.
# ===========================================================================


def test_get_revision_returns_intended_outcome_kind_and_resource_identity(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """For a known Intended Outcome Revision, ``get_revision`` returns a
    row whose ``outcome_kind`` is the literal ``'intended'`` and whose
    ``intended_outcome_id`` is the target Intended Outcome **Resource**
    Identity (Requirements 44.4, 47.4)."""
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    created = _create_intended_outcome(
        intended_outcome_service, planning_engine
    )

    with planning_engine.connect() as conn:
        row = IntendedOutcomeService.get_revision(
            conn, created.intended_outcome_revision_id
        )

    assert isinstance(row, IntendedOutcomeRevisionRow)
    # The outcome_kind discriminator the Outcome_Service gates on.
    assert row.outcome_kind == "intended"
    # Resource Identity is distinct from the Revision Identity and is the
    # anchor the Outcome_Service matches against (AD-WS-40).
    assert row.intended_outcome_id == created.intended_outcome_id
    assert (
        row.intended_outcome_revision_id
        == created.intended_outcome_revision_id
    )
    assert row.intended_outcome_id != row.intended_outcome_revision_id


def test_get_revision_returns_all_persisted_columns_byte_equivalent(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """Every column the read projects matches the row the Slice 2 service
    persisted (Requirements 44.4, 47.4)."""
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    created = _create_intended_outcome(
        intended_outcome_service,
        planning_engine,
        success_condition="Latency p99 under 200ms after rollout.",
        observation_window="14 days",
        attribution_assumption="No competing rollout in the window.",
    )

    with planning_engine.connect() as conn:
        row = IntendedOutcomeService.get_revision(
            conn, created.intended_outcome_revision_id
        )

    assert row is not None
    assert row.target_objective_id == _OBJECTIVE_ID
    assert row.parent_revision_id is None
    assert row.success_condition == "Latency p99 under 200ms after rollout."
    assert row.observation_window == "14 days"
    assert row.attribution_assumption == "No competing rollout in the window."
    assert row.authoring_party_id == _PARTY_ID
    assert row.applicable_scope == _SCOPE
    assert row.recorded_at == created.recorded_at


def test_get_revision_handles_null_optional_columns(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """Optional descriptor columns persisted as NULL surface as ``None``
    on the read row rather than raising."""
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    created = _create_intended_outcome(
        intended_outcome_service,
        planning_engine,
        observation_window=None,
        attribution_assumption=None,
    )

    with planning_engine.connect() as conn:
        row = IntendedOutcomeService.get_revision(
            conn, created.intended_outcome_revision_id
        )

    assert row is not None
    assert row.observation_window is None
    assert row.attribution_assumption is None


# ===========================================================================
# Requirement 44.4 / 47.4 — unresolvable identifier returns a structured
# not-found indication (None).
# ===========================================================================


def test_get_revision_returns_none_for_unresolvable_identifier(
    planning_engine: Engine,
) -> None:
    """An identifier that resolves to no ``Intended_Outcome_Revisions``
    row returns ``None`` — the structured not-found branch the caller
    treats as unresolvable (Requirements 44.4, 47.4)."""
    # No Intended Outcome has been created; the schema is empty.
    with planning_engine.connect() as conn:
        row = IntendedOutcomeService.get_revision(
            conn, _UNRESOLVABLE_REV_ID
        )

    assert row is None


def test_get_revision_returns_none_when_other_revisions_exist(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """An unresolvable identifier returns ``None`` even when other
    Intended Outcome Revisions are present, confirming the lookup is
    keyed on the supplied identifier and does not fall through to an
    arbitrary row."""
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)
    _create_intended_outcome(intended_outcome_service, planning_engine)

    with planning_engine.connect() as conn:
        # A freshly minted UUID never registered as a Revision Identity.
        row = IntendedOutcomeService.get_revision(
            conn, str(uuid.uuid4())
        )

    assert row is None


# ===========================================================================
# Requirement 60.1 — the read performs no mutation of any Slice 2 row.
# ===========================================================================


def test_get_revision_does_not_mutate_any_slice2_row(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """Calling ``get_revision`` leaves the Intended Outcome tables
    byte-equivalent (Requirement 60.1 — no write path on the
    Planning_Service)."""
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)
    created = _create_intended_outcome(
        intended_outcome_service, planning_engine
    )

    before = _snapshot(planning_engine)

    with planning_engine.connect() as conn:
        IntendedOutcomeService.get_revision(
            conn, created.intended_outcome_revision_id
        )
        # An unresolvable lookup must also be side-effect free.
        IntendedOutcomeService.get_revision(conn, _UNRESOLVABLE_REV_ID)

    after = _snapshot(planning_engine)
    assert after == before


def test_get_revision_is_idempotent_across_repeated_reads(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """Repeated reads of the same Revision return equal rows and never
    perturb the underlying Slice 2 tables (Requirement 60.1)."""
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)
    created = _create_intended_outcome(
        intended_outcome_service, planning_engine
    )

    before = _snapshot(planning_engine)

    rows = []
    with planning_engine.connect() as conn:
        for _ in range(5):
            rows.append(
                IntendedOutcomeService.get_revision(
                    conn, created.intended_outcome_revision_id
                )
            )

    after = _snapshot(planning_engine)

    assert all(row == rows[0] for row in rows)
    assert after == before
