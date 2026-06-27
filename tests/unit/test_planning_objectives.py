"""Unit tests for :mod:`walking_slice.planning.objectives` (task 3.2).

Pins the contract established in task 3.1, design
§"Planning_Service.Objectives", and Requirements 2.3, 2.4, 2.5, 2.6
for :meth:`ObjectiveService.create_objective`:

- **2.3** — boundary lengths on ``statement`` (1..4000) and
  ``rationale`` (0..10000) are enforced before any database read; over
  long or empty values raise :class:`ObjectiveValidationError` with a
  stable ``failed_constraint`` identifier and persist no rows.
- **2.4** — the target Decision Identity must resolve to an existing
  Decision Immutable Record whose ``outcome`` is ``'Accept'``.
  ``Reject``, ``Defer``, and unresolved identifiers raise
  :class:`ObjectiveDecisionNotResolvableError`; no Resource, Revision,
  Addresses Relationship, or audit row is persisted.
- **2.5** — when :class:`AuthorizationService` denies the attempt, the
  service raises :class:`ObjectiveAuthorizationError`, no
  Objectives / Objective_Revisions / Relationships / consequential
  audit rows survive, and exactly one Denial Record is appended in a
  separate transaction (AD-WS-9 / Requirement 7.6 pattern).
- **2.6** — missing required attributes (target_decision_id,
  authoring_party_id, applicable_scope) raise
  :class:`ObjectiveValidationError` with the matching
  ``failed_constraint``, before the Knowledge_Service or
  Authorization_Service is consulted.

The tests intentionally mirror the test style of
``tests/unit/test_knowledge_decisions.py`` (happy-path / validation
rejection) and ``tests/unit/test_knowledge_decision_authority.py``
(separate-transaction Denial Record after rollback), keeping the
audit-row reading helpers and the Recommendation seeding pattern.
"""

from __future__ import annotations

import re
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
from walking_slice.knowledge import (
    CreateDecisionResult,
    CreateFindingResult,
    CreateRecommendationResult,
    KnowledgeService,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.objectives import (
    CreateObjectiveResult,
    ObjectiveAuthorizationError,
    ObjectiveDecisionNotResolvableError,
    ObjectiveService,
    ObjectiveValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

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
    append-only trigger (task 1.3). No disclosure seeding is required
    here: Objective creation does not consult the disclosure registry.
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def knowledge_service_unwired(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """Knowledge_Service without authorization for prerequisite seeding.

    The Recommendation / Decision rows used to back Objective tests
    are seeded through the unwired Knowledge_Service so the
    seeding step does not itself need a role assignment. Only the
    authority check under test (on :meth:`ObjectiveService.create_objective`)
    must be exercised.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def objective_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    knowledge_service_unwired: KnowledgeService,
) -> ObjectiveService:
    """ObjectiveService wired with a real AuthorizationService.

    The same instance is used by every test in this module; the
    authorization deny path is exercised by *not* assigning a role
    rather than by swapping in a stub service, so the real evaluation
    code path (and its evaluation-row audit append) participates in
    the test.
    """
    return ObjectiveService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        knowledge_service=knowledge_service_unwired,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
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
        _seed_party(conn, _PARTY_ID, "Objective Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_decision(
    engine: Engine,
    knowledge_service_unwired: KnowledgeService,
    *,
    outcome: str,
) -> CreateDecisionResult:
    """Seed a Finding, a Recommendation, and one Decision with the given outcome.

    Uses the unwired KnowledgeService so the seeding is not gated by
    the Slice 1 authority check; the Objective tests only exercise the
    Planning_Service authority check on Objective creation.
    """
    with engine.begin() as conn:
        finding: CreateFindingResult = knowledge_service_unwired.create_finding(
            conn,
            statement="Source finding backing the objective decision.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        recommendation: CreateRecommendationResult = (
            knowledge_service_unwired.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="Recommend action based on hypothesis Finding.",
            )
        )
        decision = knowledge_service_unwired.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome=outcome,
            rationale=f"Decision with outcome {outcome}.",
            deciding_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
        )
    return decision


def _assign_objective_owner_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Objective Owner authority (``modify``) to ``party_id``.

    Per AD-WS-15, ``create.objective`` maps to the ``modify`` authority
    type. A Party with an effective Role Assignment carrying
    ``modify`` over ``scope`` is permitted to create Objectives in
    that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="objective_owner",
        scope=scope,
        authorities_granted=("modify",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


# ---------------------------------------------------------------------------
# Row readers — used by negative-path tests to confirm nothing was persisted.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_audit_rows(
    engine: Engine, *, outcome: Optional[str] = None
) -> list[dict]:
    sql = (
        "SELECT actor_party_id, action_type, outcome, target_id, "
        "target_revision_id, reason_code, correlation_id, "
        "authorities_required, authorities_held "
        "FROM Audit_Records "
    )
    params: dict[str, object] = {}
    if outcome is not None:
        sql += "WHERE outcome = :outcome "
        params["outcome"] = outcome
    sql += "ORDER BY append_sequence"
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def _fetch_denial_records(engine: Engine) -> list[dict]:
    """Return only the dedicated Denial Records (mirrors the Slice 1 helper).

    The dedicated Denial Record (from
    :meth:`ObjectiveService._persist_objective_denial`) is the row with
    ``outcome='deny'`` and NULL in ``authorities_required`` /
    ``authorities_held``; the evaluation row written by
    :meth:`AuthorizationService.evaluate` carries non-NULL values in
    those columns and is filtered out here.
    """
    deny_rows = _fetch_audit_rows(engine, outcome="deny")
    return [row for row in deny_rows if row["authorities_required"] is None]


# ===========================================================================
# Happy path baseline — confirms the test wiring before negative paths run.
# ===========================================================================


def test_create_objective_permits_when_objective_owner_role_grants_modify(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Permit path: with an effective Objective Owner role and an Accept
    Decision, the service creates one Objective Resource, one
    Objective Revision, one Addresses Relationship, and one
    consequential audit row inside one transaction (AD-WS-5)."""
    _seed_required_parties(planning_engine)
    _assign_objective_owner_role(authorization_service, planning_engine)
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome="Accept"
    )

    with planning_engine.begin() as conn:
        result = objective_service.create_objective(
            conn,
            statement="Reduce onboarding time by 50%.",
            rationale="Anchored on the accepted decision.",
            target_decision_id=decision.decision_id,
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateObjectiveResult)
    assert _CANONICAL_UUID7.match(result.objective_id)
    assert _CANONICAL_UUID7.match(result.objective_revision_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.target_decision_id == decision.decision_id
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Objectives") == 1
    assert _count(planning_engine, "Objective_Revisions") == 1
    consequential = _fetch_audit_rows(planning_engine, outcome="consequential")
    create_obj_rows = [
        row for row in consequential if row["action_type"] == "create.objective"
    ]
    assert len(create_obj_rows) == 1
    assert create_obj_rows[0]["correlation_id"] == "corr-permit"
    assert _fetch_denial_records(planning_engine) == []


# ===========================================================================
# Requirement 2.3 — boundary lengths on statement and rationale.
# ===========================================================================


class TestStatementBoundaries:
    """Statement is required and must be 1..4000 characters (Requirement 2.3)."""

    def test_statement_at_min_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        objective_service: ObjectiveService,
        knowledge_service_unwired: KnowledgeService,
    ) -> None:
        """A 1-character statement is accepted (lower bound of Requirement 2.3)."""
        _seed_required_parties(planning_engine)
        _assign_objective_owner_role(authorization_service, planning_engine)
        decision = _seed_decision(
            planning_engine, knowledge_service_unwired, outcome="Accept"
        )

        with planning_engine.begin() as conn:
            result = objective_service.create_objective(
                conn,
                statement="x",
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.statement == "x"
        row = planning_engine.connect().execute(
            text(
                "SELECT statement FROM Objective_Revisions "
                "WHERE objective_revision_id = :rid"
            ),
            {"rid": result.objective_revision_id},
        ).scalar_one()
        assert row == "x"

    def test_statement_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        objective_service: ObjectiveService,
        knowledge_service_unwired: KnowledgeService,
    ) -> None:
        """A 4,000-character statement is accepted (upper bound of Requirement 2.3)."""
        _seed_required_parties(planning_engine)
        _assign_objective_owner_role(authorization_service, planning_engine)
        decision = _seed_decision(
            planning_engine, knowledge_service_unwired, outcome="Accept"
        )

        statement = "a" * 4_000
        with planning_engine.begin() as conn:
            result = objective_service.create_objective(
                conn,
                statement=statement,
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert len(result.statement) == 4_000

    def test_statement_over_max_length_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """4,001 characters trip the validator with ``statement_too_long``.

        The validator runs before any database read; nothing is
        persisted and the failed_constraint is stable text the route
        layer can map to a structured response.
        """
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="a" * 4_001,
                    rationale=None,
                    target_decision_id="00000000-0000-7000-8000-0000000000aa",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "statement_too_long"
        assert _count(planning_engine, "Objectives") == 0

    def test_empty_statement_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """The empty string trips ``statement_missing`` (Requirement 2.3 / 2.6)."""
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="",
                    rationale=None,
                    target_decision_id="00000000-0000-7000-8000-0000000000aa",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "statement_missing"
        assert _count(planning_engine, "Objectives") == 0


class TestRationaleBoundaries:
    """Rationale is optional and (when present) must be 0..10000 chars."""

    def test_rationale_none_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        objective_service: ObjectiveService,
        knowledge_service_unwired: KnowledgeService,
    ) -> None:
        """``None`` rationale is persisted as SQL NULL."""
        _seed_required_parties(planning_engine)
        _assign_objective_owner_role(authorization_service, planning_engine)
        decision = _seed_decision(
            planning_engine, knowledge_service_unwired, outcome="Accept"
        )

        with planning_engine.begin() as conn:
            result = objective_service.create_objective(
                conn,
                statement="stmt",
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.rationale is None
        row = planning_engine.connect().execute(
            text(
                "SELECT rationale FROM Objective_Revisions "
                "WHERE objective_revision_id = :rid"
            ),
            {"rid": result.objective_revision_id},
        ).scalar_one()
        assert row is None

    def test_rationale_empty_string_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        objective_service: ObjectiveService,
        knowledge_service_unwired: KnowledgeService,
    ) -> None:
        """The empty string satisfies the 0-character lower bound."""
        _seed_required_parties(planning_engine)
        _assign_objective_owner_role(authorization_service, planning_engine)
        decision = _seed_decision(
            planning_engine, knowledge_service_unwired, outcome="Accept"
        )

        with planning_engine.begin() as conn:
            result = objective_service.create_objective(
                conn,
                statement="stmt",
                rationale="",
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.rationale == ""

    def test_rationale_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        objective_service: ObjectiveService,
        knowledge_service_unwired: KnowledgeService,
    ) -> None:
        """A 10,000-character rationale satisfies the upper bound."""
        _seed_required_parties(planning_engine)
        _assign_objective_owner_role(authorization_service, planning_engine)
        decision = _seed_decision(
            planning_engine, knowledge_service_unwired, outcome="Accept"
        )

        rationale = "r" * 10_000
        with planning_engine.begin() as conn:
            result = objective_service.create_objective(
                conn,
                statement="stmt",
                rationale=rationale,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.rationale is not None
        assert len(result.rationale) == 10_000

    def test_rationale_over_max_length_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """10,001 characters trip ``rationale_too_long``."""
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="stmt",
                    rationale="r" * 10_001,
                    target_decision_id="00000000-0000-7000-8000-0000000000aa",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "rationale_too_long"
        assert _count(planning_engine, "Objectives") == 0

    def test_rationale_non_string_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """A non-string, non-None rationale trips ``rationale_invalid_type``."""
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="stmt",
                    rationale=12345,  # type: ignore[arg-type]
                    target_decision_id="00000000-0000-7000-8000-0000000000aa",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "rationale_invalid_type"


# ===========================================================================
# Requirement 2.4 — Decision outcome variants (Accept / Reject / Defer /
# unresolved).
# ===========================================================================


def test_target_decision_with_accept_outcome_permits_creation(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """An ``Accept`` Decision is the only outcome that allows creation
    to proceed (Requirement 2.4)."""
    _seed_required_parties(planning_engine)
    _assign_objective_owner_role(authorization_service, planning_engine)
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome="Accept"
    )

    with planning_engine.begin() as conn:
        result = objective_service.create_objective(
            conn,
            statement="Objective targeting accepted decision.",
            rationale=None,
            target_decision_id=decision.decision_id,
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
        )
    assert _CANONICAL_UUID7.match(result.objective_id)
    assert _count(planning_engine, "Objectives") == 1


@pytest.mark.parametrize("outcome", ["Reject", "Defer"])
def test_target_decision_with_non_accept_outcome_rejected(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
    outcome: str,
) -> None:
    """``Reject`` and ``Defer`` Decisions are not eligible targets
    (Requirement 2.4); no Resource, Revision, Addresses Relationship,
    or audit row is persisted."""
    _seed_required_parties(planning_engine)
    _assign_objective_owner_role(authorization_service, planning_engine)
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome=outcome
    )

    pre_objectives = _count(planning_engine, "Objectives")
    pre_relationships = _count(planning_engine, "Relationships")
    pre_consequential = len(
        _fetch_audit_rows(planning_engine, outcome="consequential")
    )

    with pytest.raises(ObjectiveDecisionNotResolvableError) as exc_info:
        with planning_engine.begin() as conn:
            objective_service.create_objective(
                conn,
                statement="Should be rejected by outcome check.",
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

    assert exc_info.value.target_decision_id == decision.decision_id
    assert exc_info.value.failed_constraint == (
        "target_decision_outcome_not_accept"
    )
    assert exc_info.value.outcome == outcome
    assert _count(planning_engine, "Objectives") == pre_objectives
    # Only the Addresses row from the Decision-to-Recommendation seed
    # exists; no Addresses row from the Objective creation was added.
    assert _count(planning_engine, "Relationships") == pre_relationships
    create_obj_rows = [
        row
        for row in _fetch_audit_rows(planning_engine, outcome="consequential")
        if row["action_type"] == "create.objective"
    ]
    assert create_obj_rows == []
    assert len(
        _fetch_audit_rows(planning_engine, outcome="consequential")
    ) == pre_consequential


def test_unresolved_target_decision_rejected(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
) -> None:
    """An identifier that resolves to no Decision is rejected with the
    ``target_decision_not_resolvable`` constraint (Requirement 2.4).

    The check runs before authorization evaluation, so no Denial
    Record is appended either: an unknown identifier is a validation
    failure, not a denial.
    """
    _seed_required_parties(planning_engine)
    _assign_objective_owner_role(authorization_service, planning_engine)

    fake_decision_id = "00000000-0000-7000-8000-0000deadbeef"
    with pytest.raises(ObjectiveDecisionNotResolvableError) as exc_info:
        with planning_engine.begin() as conn:
            objective_service.create_objective(
                conn,
                statement="Should be rejected — no such Decision.",
                rationale=None,
                target_decision_id=fake_decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

    assert exc_info.value.target_decision_id == fake_decision_id
    assert exc_info.value.failed_constraint == "target_decision_not_resolvable"
    assert exc_info.value.outcome is None
    assert _count(planning_engine, "Objectives") == 0
    assert _fetch_denial_records(planning_engine) == []


# ===========================================================================
# Prohibited-attribute rejection — Property 22 / Requirements 12.x, 13.x
# (still gated through Requirement 2.6's "invalid attribute" path).
# ===========================================================================


@pytest.mark.parametrize(
    "prohibited_key",
    [
        "work-started-at",          # execution prefix
        "work_started_at",          # snake_case variant
        "actual-cost",              # execution prefix
        "percent-complete-value",   # execution prefix
        "observed-outcome-value",   # observed-outcome prefix
        "produced-deliverable-id",  # produced-deliverable prefix
        "hand-off-receipt-id",      # produced-deliverable prefix
    ],
)
def test_request_attributes_with_prohibited_keys_rejected(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
    prohibited_key: str,
) -> None:
    """Property 22: request bodies carrying any execution,
    observed-outcome, or produced-deliverable attribute are rejected
    at the boundary; no rows are persisted.

    The Objective service forwards :func:`_reject_prohibited_attributes`
    via the ``request_attributes`` parameter (the HTTP layer's
    raw-body pass-through); the error surfaces as an
    :class:`ObjectiveValidationError` with the
    ``prohibited_attribute`` constraint and the offending key on
    :attr:`prohibited_keys`.
    """
    _seed_required_parties(planning_engine)
    _assign_objective_owner_role(authorization_service, planning_engine)
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome="Accept"
    )

    with pytest.raises(ObjectiveValidationError) as exc_info:
        with planning_engine.begin() as conn:
            objective_service.create_objective(
                conn,
                statement="Objective with a prohibited attribute attached.",
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                request_attributes={
                    "statement": "ignored",
                    prohibited_key: "anything",
                },
            )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert prohibited_key in exc_info.value.prohibited_keys
    assert _count(planning_engine, "Objectives") == 0
    assert _count(planning_engine, "Objective_Revisions") == 0


def test_request_attributes_with_only_allowed_keys_accepted(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """A request body whose keys do not match any prohibited prefix
    passes the boundary check and the Objective is created."""
    _seed_required_parties(planning_engine)
    _assign_objective_owner_role(authorization_service, planning_engine)
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome="Accept"
    )

    with planning_engine.begin() as conn:
        result = objective_service.create_objective(
            conn,
            statement="Allowed-attributes-only request.",
            rationale="ok",
            target_decision_id=decision.decision_id,
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            request_attributes={
                "statement": "ok",
                "rationale": "ok",
                "target_decision_id": decision.decision_id,
                "applicable_scope": _SCOPE,
            },
        )

    assert _CANONICAL_UUID7.match(result.objective_id)
    assert _count(planning_engine, "Objectives") == 1


# ===========================================================================
# Requirement 2.6 — missing required attributes.
# ===========================================================================


class TestMissingRequiredAttributes:
    """Each required attribute, missing in turn, raises with a stable constraint name."""

    def test_missing_target_decision_id_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """Empty target_decision_id → ``target_decision_id_missing``."""
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="stmt",
                    rationale=None,
                    target_decision_id="",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "target_decision_id_missing"
        assert _count(planning_engine, "Objectives") == 0

    def test_missing_authoring_party_id_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """Empty authoring_party_id → ``authoring_party_id_missing``."""
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="stmt",
                    rationale=None,
                    target_decision_id="00000000-0000-7000-8000-0000000000aa",
                    authoring_party_id="",
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == (
            "authoring_party_id_missing"
        )

    def test_missing_applicable_scope_rejected(
        self,
        planning_engine: Engine,
        objective_service: ObjectiveService,
    ) -> None:
        """Empty applicable_scope → ``applicable_scope_missing``."""
        _seed_required_parties(planning_engine)
        with planning_engine.begin() as conn:
            with pytest.raises(ObjectiveValidationError) as exc_info:
                objective_service.create_objective(
                    conn,
                    statement="stmt",
                    rationale=None,
                    target_decision_id="00000000-0000-7000-8000-0000000000aa",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope="",
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "applicable_scope_missing"


# ===========================================================================
# Requirement 2.5 — authorization deny path produces exactly one Denial Record.
# ===========================================================================


def test_authorization_deny_path_produces_exactly_one_denial_record(
    planning_engine: Engine,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Requirement 2.5: a Party without an Objective Owner role is
    denied, :class:`ObjectiveAuthorizationError` is raised, no
    Objectives/Objective_Revisions/Addresses/consequential audit rows
    survive, and exactly one Denial Record is appended in a separate
    transaction (so it survives the caller's rollback).

    The deciding Party deliberately holds *no* Role Assignment; the
    Authorization_Service therefore reaches the
    ``no-role-assignment`` reason code per Requirement 7.2.
    """
    _seed_required_parties(planning_engine)
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome="Accept"
    )

    pre_objectives = _count(planning_engine, "Objectives")
    pre_revisions = _count(planning_engine, "Objective_Revisions")
    pre_relationships = _count(planning_engine, "Relationships")
    pre_consequential_create_objective = len(
        [
            row
            for row in _fetch_audit_rows(planning_engine, outcome="consequential")
            if row["action_type"] == "create.objective"
        ]
    )
    pre_denial_records = len(_fetch_denial_records(planning_engine))

    with pytest.raises(ObjectiveAuthorizationError) as exc_info:
        with planning_engine.begin() as conn:
            objective_service.create_objective(
                conn,
                statement="Should be denied — no role assignment.",
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                correlation_id="corr-deny",
            )

    # AD-WS-9: the exception carries only reason_code and correlation_id.
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-deny"

    # No Objective-side rows persisted: the caller's transaction was
    # rolled back when the exception propagated through engine.begin().
    assert _count(planning_engine, "Objectives") == pre_objectives
    assert _count(planning_engine, "Objective_Revisions") == pre_revisions
    assert _count(planning_engine, "Relationships") == pre_relationships
    post_consequential_create_objective = len(
        [
            row
            for row in _fetch_audit_rows(planning_engine, outcome="consequential")
            if row["action_type"] == "create.objective"
        ]
    )
    assert (
        post_consequential_create_objective
        == pre_consequential_create_objective
    )

    # Exactly one Denial Record was appended in a separate transaction.
    denial_records = _fetch_denial_records(planning_engine)
    assert len(denial_records) == pre_denial_records + 1
    new_denial = denial_records[-1]
    assert new_denial["actor_party_id"] == _PARTY_ID
    assert new_denial["action_type"] == "create.objective"
    assert new_denial["reason_code"] == "no-role-assignment"
    assert new_denial["correlation_id"] == "corr-deny"
    # The Denial Record's target is the Decision under attempt
    # (matches the Slice 1 Decision-denial pattern in
    # KnowledgeService._persist_decision_denial).
    assert new_denial["target_id"] == decision.decision_id
    assert new_denial["target_revision_id"] is None


def test_authorization_deny_path_for_out_of_scope_role(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    objective_service: ObjectiveService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """A Role Assignment that does not cover ``applicable_scope`` also
    yields exactly one Denial Record (Requirement 2.5)."""
    _seed_required_parties(planning_engine)
    # Grant ``modify`` over a *different* scope.
    _assign_objective_owner_role(
        authorization_service, planning_engine, scope="pilot/team-b"
    )
    decision = _seed_decision(
        planning_engine, knowledge_service_unwired, outcome="Accept"
    )

    pre_denial_records = len(_fetch_denial_records(planning_engine))

    with pytest.raises(ObjectiveAuthorizationError) as exc_info:
        with planning_engine.begin() as conn:
            objective_service.create_objective(
                conn,
                statement="Should be denied — out of scope.",
                rationale=None,
                target_decision_id=decision.decision_id,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                correlation_id="corr-deny-oos",
            )

    assert exc_info.value.reason_code == "out-of-scope"
    assert _count(planning_engine, "Objectives") == 0
    denial_records = _fetch_denial_records(planning_engine)
    assert len(denial_records) == pre_denial_records + 1
    assert denial_records[-1]["reason_code"] == "out-of-scope"
    assert denial_records[-1]["correlation_id"] == "corr-deny-oos"
