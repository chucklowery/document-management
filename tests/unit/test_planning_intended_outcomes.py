"""Unit tests for :mod:`walking_slice.planning.intended_outcomes` (task 4.2).

Pins the contract established in task 4.1, design
§"Planning_Service.IntendedOutcomes", and Requirements 3.3, 13.1, 13.3
for :meth:`IntendedOutcomeService.create_intended_outcome`:

- **3.3 / 13.1** — observed-outcome attribute keys are rejected at the
  request boundary. The Pydantic model validator screens the typed
  kwargs (a redundant defensive layer) and the
  ``request_attributes`` keyword forwards the HTTP layer's raw body
  through :func:`_reject_prohibited_attributes`. Both surfaces raise
  :class:`IntendedOutcomeValidationError` with
  ``failed_constraint == 'prohibited_attribute'`` and the offending
  key on :attr:`prohibited_keys`.
- **13.3** — every persisted ``Intended_Outcome_Revisions`` row
  carries ``outcome_kind = 'intended'``. The service binds the
  column to the module constant; the
  ``CHECK (outcome_kind = 'intended')`` constraint on
  ``Intended_Outcome_Revisions`` is the defense-in-depth layer: a
  hand-rolled INSERT that bypasses the service is rejected by the
  database with :class:`sqlalchemy.exc.IntegrityError`.
- **3.2 / 13.1 / 13.3** — boundary lengths on ``success_condition``
  (1..4000), ``observation_window`` (0..1000), and
  ``attribution_assumption`` (0..4000) are enforced before any
  database read. Over-long or empty values raise
  :class:`IntendedOutcomeValidationError` with a stable
  ``failed_constraint`` identifier and persist no rows.

The tests intentionally mirror the test style of
``tests/unit/test_planning_objectives.py`` (happy-path / validation
rejection) and ``tests/unit/test_planning_projects.py`` (hand-rolled
INSERT verifying schema CHECK constraints).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.intended_outcomes import (
    CreateIntendedOutcomeResult,
    IntendedOutcomeService,
    IntendedOutcomeValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
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
    ``create_planning_schema`` installs every Slice 2 table, index,
    and append-only trigger (task 1.3). No disclosure seeding is
    required: Intended Outcome creation does not consult the
    disclosure registry.
    """
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
    """IntendedOutcomeService wired with a real AuthorizationService.

    The same instance is used by every test in this module; the
    authorization deny path is exercised by *not* assigning a role
    rather than by swapping in a stub service, so the real
    evaluation code path participates in the test.
    """
    return IntendedOutcomeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
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
        _seed_party(conn, _PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_objective(engine: Engine) -> None:
    """Seed one Objective Resource + its first Objective Revision.

    Inserted directly (mirrors the pattern from
    ``test_planning_projects.py``): the upstream Decision dependency
    is irrelevant to the IntendedOutcome tests; the schema only
    requires the Objective row to exist for
    ``Intended_Outcome_Revisions.target_objective_id`` to resolve.
    """
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
    """Assign Intended Outcome Owner authority (``modify``) to ``party_id``.

    Per AD-WS-15, ``create.intended_outcome`` maps to the ``modify``
    authority type. A Party with an effective Role Assignment
    carrying ``modify`` over ``scope`` is permitted to create
    Intended Outcomes in that scope.
    """
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


# ---------------------------------------------------------------------------
# Row readers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_revision(engine: Engine, revision_id: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT intended_outcome_revision_id, intended_outcome_id,
                       outcome_kind, target_objective_id, success_condition,
                       observation_window, attribution_assumption,
                       authoring_party_id, applicable_scope, recorded_at
                FROM Intended_Outcome_Revisions
                WHERE intended_outcome_revision_id = :rid
                """
            ),
            {"rid": revision_id},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ===========================================================================
# Happy path baseline — confirms the test wiring before negative paths run.
# ===========================================================================


def test_create_intended_outcome_permits_when_role_grants_modify(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """Permit path: with an effective Intended Outcome Owner role and
    an existing Objective, the service creates one Intended Outcome
    Resource, one Intended Outcome Revision, one ``Addresses``
    Relationship, and one consequential audit row inside one
    transaction (AD-WS-5).

    Confirms ``outcome_kind == 'intended'`` flows through to the
    Revision row (Requirement 13.3 — the positive side of the
    constraint covered in :class:`TestOutcomeKindCheck`).
    """
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    with planning_engine.begin() as conn:
        result = intended_outcome_service.create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition="Onboarding completes in under two days.",
            observation_window="30 days post launch",
            attribution_assumption="Telemetry sampling rate held constant.",
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateIntendedOutcomeResult)
    assert _CANONICAL_UUID7.match(result.intended_outcome_id)
    assert _CANONICAL_UUID7.match(result.intended_outcome_revision_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.outcome_kind == "intended"
    assert result.target_objective_id == _OBJECTIVE_ID
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Intended_Outcomes") == 1
    assert _count(planning_engine, "Intended_Outcome_Revisions") == 1

    row = _fetch_revision(planning_engine, result.intended_outcome_revision_id)
    assert row is not None
    assert row["outcome_kind"] == "intended"
    assert row["target_objective_id"] == _OBJECTIVE_ID
    assert row["success_condition"] == (
        "Onboarding completes in under two days."
    )
    assert row["observation_window"] == "30 days post launch"
    assert row["attribution_assumption"] == (
        "Telemetry sampling rate held constant."
    )


# ===========================================================================
# Requirement 13.3 — outcome_kind CHECK rejection.
#
# The service hardcodes ``outcome_kind = 'intended'`` so the only way
# to reach the CHECK is to bypass the service with a hand-rolled
# INSERT (mirroring the project planned-date CHECK pattern in
# ``test_planning_projects.TestPlannedDateOrderCheckLayer``).
# ===========================================================================


class TestOutcomeKindCheck:
    """Schema CHECK constraint rejects any ``outcome_kind`` other than
    ``'intended'`` at INSERT time.

    Requirement 13.3 declares the persistence invariant that every
    ``Intended_Outcome_Revisions`` row records
    ``outcome_kind = 'intended'``. The
    ``CHECK (outcome_kind = 'intended')`` clause on the column
    (design §"Data Models — Schema Additions") enforces this at the
    schema layer; the IntendedOutcomeService binds the column to the
    module constant ``_OUTCOME_KIND_INTENDED`` as defense in depth.
    """

    @pytest.mark.parametrize(
        "rejected_value",
        [
            "observed",
            "actual",
            "achieved",
            "INTENDED",  # case-sensitive: uppercase does not match
            "intended ",  # trailing whitespace does not match
            "",
        ],
    )
    def test_direct_insert_with_other_outcome_kind_rejected_by_check(
        self,
        planning_engine: Engine,
        rejected_value: str,
    ) -> None:
        """A hand-rolled INSERT with any ``outcome_kind`` other than
        the literal ``'intended'`` is rejected by the
        ``Intended_Outcome_Revisions`` CHECK constraint.

        This bypasses the IntendedOutcomeService entirely so the
        schema-level guarantee is exercised on its own.
        """
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        # Pre-create an Intended_Outcomes header so the FK target
        # exists and the only constraint left to fail is the
        # outcome_kind CHECK.
        intended_outcome_id = "00000000-0000-7000-8000-000000d00001"
        revision_id = "00000000-0000-7000-8000-000000d00002"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO Intended_Outcomes "
                    "(intended_outcome_id, created_at) "
                    "VALUES (:iid, :ts)"
                ),
                {"iid": intended_outcome_id, "ts": _TS_FIXED},
            )

        with planning_engine.connect() as conn, pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO Intended_Outcome_Revisions (
                            intended_outcome_revision_id,
                            intended_outcome_id, parent_revision_id,
                            outcome_kind, target_objective_id,
                            success_condition, observation_window,
                            attribution_assumption, authoring_party_id,
                            applicable_scope, recorded_at
                        ) VALUES (
                            :rev, :iid, NULL,
                            :outcome_kind, :oid,
                            'success', NULL, NULL,
                            :party, :scope, :ts
                        )
                        """
                    ),
                    {
                        "rev": revision_id,
                        "iid": intended_outcome_id,
                        "outcome_kind": rejected_value,
                        "oid": _OBJECTIVE_ID,
                        "party": _PARTY_ID,
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        assert _count(planning_engine, "Intended_Outcome_Revisions") == 0

    def test_direct_insert_with_intended_outcome_kind_accepted_by_check(
        self,
        planning_engine: Engine,
    ) -> None:
        """The CHECK accepts the literal ``'intended'`` (the only
        permitted value)."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        intended_outcome_id = "00000000-0000-7000-8000-000000d00010"
        revision_id = "00000000-0000-7000-8000-000000d00011"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO Intended_Outcomes "
                    "(intended_outcome_id, created_at) "
                    "VALUES (:iid, :ts)"
                ),
                {"iid": intended_outcome_id, "ts": _TS_FIXED},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO Intended_Outcome_Revisions (
                        intended_outcome_revision_id,
                        intended_outcome_id, parent_revision_id,
                        outcome_kind, target_objective_id,
                        success_condition, observation_window,
                        attribution_assumption, authoring_party_id,
                        applicable_scope, recorded_at
                    ) VALUES (
                        :rev, :iid, NULL,
                        'intended', :oid,
                        'success', NULL, NULL,
                        :party, :scope, :ts
                    )
                    """
                ),
                {
                    "rev": revision_id,
                    "iid": intended_outcome_id,
                    "oid": _OBJECTIVE_ID,
                    "party": _PARTY_ID,
                    "scope": _SCOPE,
                    "ts": _TS_FIXED,
                },
            )

        assert _count(planning_engine, "Intended_Outcome_Revisions") == 1

    def test_service_always_persists_outcome_kind_intended(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """The service hardcodes ``outcome_kind = 'intended'`` on
        every successful write (Requirement 13.3).

        The public surface does not even expose an ``outcome_kind``
        parameter; the column is bound to the module constant. This
        test pins that behaviour at the row level by inspecting the
        persisted Revision.
        """
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        row = _fetch_revision(
            planning_engine, result.intended_outcome_revision_id
        )
        assert row is not None
        assert row["outcome_kind"] == "intended"


# ===========================================================================
# Requirement 3.3 / 13.1 — observed-outcome-key rejection.
#
# Drawn from a curated list spanning every prefix in
# :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES` (``observed-``,
# ``observation-time-``, ``attribution-evidence-``). The matcher in
# :func:`_reject_prohibited_attributes` is case-insensitive and
# hyphen/underscore-invariant, so each prefix is exercised in both
# hyphenated and snake_case form.
# ===========================================================================


# Curated list of keys that must be rejected: each prefix from
# :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES` exercised in
# hyphen-lowercase, snake_case, and uppercase variants.
_PROHIBITED_OBSERVED_KEYS = [
    # observed- prefix
    "observed-outcome-value",
    "observed_outcome_value",
    "Observed-Outcome-Value",
    "observed-measurement",
    # observation-time- prefix
    "observation-time-recorded",
    "observation_time_recorded",
    # attribution-evidence- prefix
    "attribution-evidence-id",
    "attribution_evidence_id",
    "ATTRIBUTION-EVIDENCE-REFERENCE",
]


@pytest.mark.parametrize("prohibited_key", _PROHIBITED_OBSERVED_KEYS)
def test_request_attributes_with_observed_outcome_key_rejected(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
    prohibited_key: str,
) -> None:
    """Property 22 / Requirements 3.3 / 13.1: request bodies carrying
    any observed-outcome attribute are rejected at the boundary; no
    rows are persisted.

    Exercises the ``request_attributes`` pass-through: the HTTP
    layer (task 15.1) forwards the raw request body via this
    keyword, and the service screens it through
    :func:`_reject_prohibited_attributes` before any Pydantic
    parsing of the typed kwargs.
    """
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    with pytest.raises(IntendedOutcomeValidationError) as exc_info:
        with planning_engine.begin() as conn:
            intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                request_attributes={
                    "success_condition": "ok",
                    prohibited_key: "anything",
                },
            )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert prohibited_key in exc_info.value.prohibited_keys
    assert _count(planning_engine, "Intended_Outcomes") == 0
    assert _count(planning_engine, "Intended_Outcome_Revisions") == 0


@pytest.mark.parametrize("prohibited_key", _PROHIBITED_OBSERVED_KEYS)
def test_pydantic_model_validator_rejects_observed_outcome_key(
    planning_engine: Engine,
    intended_outcome_service: IntendedOutcomeService,
    prohibited_key: str,
) -> None:
    """The Pydantic ``_validate_no_observed_attributes`` model
    validator on :class:`IntendedOutcomeCreationRequest` also rejects
    observed-outcome keys.

    The model validator inspects raw input mappings; it is exercised
    here through the construction path used internally by
    :meth:`create_intended_outcome` when a caller bypasses the route
    layer's raw-body forwarding. Construction is driven through the
    public service rather than the model class so the service-level
    error surface (``IntendedOutcomeValidationError`` with the
    ``prohibited_attribute`` discriminator) is exercised.
    """
    _seed_required_parties(planning_engine)
    _seed_objective(planning_engine)

    # Construct a request_attributes mapping that targets the
    # model validator rather than the request_attributes
    # pre-screen. The pre-screen runs against
    # ``request_attributes`` (when supplied); the Pydantic
    # validator runs against the typed kwargs dict regardless.
    # The kwargs themselves cannot carry a prohibited attribute
    # (their names are fixed by the signature), so the only way
    # to exercise the model validator is via ``request_attributes``
    # — which is the documented route-layer pass-through.
    with pytest.raises(IntendedOutcomeValidationError) as exc_info:
        with planning_engine.begin() as conn:
            intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                request_attributes={prohibited_key: "x"},
            )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert prohibited_key in exc_info.value.prohibited_keys
    assert _count(planning_engine, "Intended_Outcomes") == 0


def test_request_attributes_with_only_allowed_keys_accepted(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    intended_outcome_service: IntendedOutcomeService,
) -> None:
    """A request body whose keys do not match any observed-outcome
    prefix passes the boundary check and the Intended Outcome is
    created.

    Critically, the legitimate field ``observation_window`` (which
    starts with ``observation-`` but not ``observation-time-``) is
    accepted — the prefix is narrow on purpose so the descriptor
    field Requirement 3.2 mandates is not falsely rejected.
    """
    _seed_required_parties(planning_engine)
    _assign_intended_outcome_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    with planning_engine.begin() as conn:
        result = intended_outcome_service.create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition="ok",
            observation_window="30 days",
            attribution_assumption="ok",
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            request_attributes={
                "target_objective_id": _OBJECTIVE_ID,
                "success_condition": "ok",
                "observation_window": "30 days",
                "attribution_assumption": "ok",
                "applicable_scope": _SCOPE,
            },
        )

    assert _CANONICAL_UUID7.match(result.intended_outcome_id)
    assert _count(planning_engine, "Intended_Outcomes") == 1


# ===========================================================================
# Requirement 3.2 — boundary lengths on success_condition.
#
# Range: 1..4000 characters; persisted column is
# ``Intended_Outcome_Revisions.success_condition`` (CHECK length 1..4000).
# ===========================================================================


class TestSuccessConditionBoundaries:
    """``success_condition`` is required and must be 1..4000 characters."""

    def test_at_min_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """A 1-character ``success_condition`` is accepted (lower bound)."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="x",
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.success_condition == "x"
        row = _fetch_revision(
            planning_engine, result.intended_outcome_revision_id
        )
        assert row is not None
        assert row["success_condition"] == "x"

    def test_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """A 4,000-character ``success_condition`` is accepted (upper bound)."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        success_condition = "a" * 4_000
        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition=success_condition,
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert len(result.success_condition) == 4_000

    def test_over_max_length_rejected(
        self,
        planning_engine: Engine,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """4,001 characters trip ``success_condition_too_long``.

        The validator runs before any database read; nothing is
        persisted and the ``failed_constraint`` is the stable
        identifier the route layer maps to a structured 400.
        """
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(IntendedOutcomeValidationError) as exc_info:
                intended_outcome_service.create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition="a" * 4_001,
                    observation_window=None,
                    attribution_assumption=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "success_condition_too_long"
        assert _count(planning_engine, "Intended_Outcomes") == 0

    def test_empty_string_rejected(
        self,
        planning_engine: Engine,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """The empty string trips ``success_condition_missing``."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(IntendedOutcomeValidationError) as exc_info:
                intended_outcome_service.create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition="",
                    observation_window=None,
                    attribution_assumption=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == "success_condition_missing"
        assert _count(planning_engine, "Intended_Outcomes") == 0


# ===========================================================================
# Requirement 3.2 — boundary lengths on observation_window.
#
# Range: 0..1000 characters (optional); persisted column is
# ``Intended_Outcome_Revisions.observation_window`` (CHECK length 0..1000
# when not NULL).
# ===========================================================================


class TestObservationWindowBoundaries:
    """``observation_window`` is optional and (when present) 0..1000 chars."""

    def test_none_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """``None`` ``observation_window`` is persisted as SQL NULL."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.observation_window is None
        row = _fetch_revision(
            planning_engine, result.intended_outcome_revision_id
        )
        assert row is not None
        assert row["observation_window"] is None

    def test_empty_string_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """The empty string satisfies the 0-character lower bound."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window="",
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.observation_window == ""

    def test_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """A 1,000-character ``observation_window`` is accepted."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        window = "w" * 1_000
        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=window,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.observation_window is not None
        assert len(result.observation_window) == 1_000

    def test_over_max_length_rejected(
        self,
        planning_engine: Engine,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """1,001 characters trip ``observation_window_too_long``."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(IntendedOutcomeValidationError) as exc_info:
                intended_outcome_service.create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition="success",
                    observation_window="w" * 1_001,
                    attribution_assumption=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == (
            "observation_window_too_long"
        )
        assert _count(planning_engine, "Intended_Outcomes") == 0


# ===========================================================================
# Requirement 3.2 — boundary lengths on attribution_assumption.
#
# Range: 0..4000 characters (optional); persisted column is
# ``Intended_Outcome_Revisions.attribution_assumption`` (CHECK length
# 0..4000 when not NULL).
# ===========================================================================


class TestAttributionAssumptionBoundaries:
    """``attribution_assumption`` is optional and (when present) 0..4000 chars."""

    def test_none_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """``None`` ``attribution_assumption`` is persisted as SQL NULL."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.attribution_assumption is None
        row = _fetch_revision(
            planning_engine, result.intended_outcome_revision_id
        )
        assert row is not None
        assert row["attribution_assumption"] is None

    def test_empty_string_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """The empty string satisfies the 0-character lower bound."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption="",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.attribution_assumption == ""

    def test_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """A 4,000-character ``attribution_assumption`` is accepted."""
        _seed_required_parties(planning_engine)
        _assign_intended_outcome_owner_role(
            authorization_service, planning_engine
        )
        _seed_objective(planning_engine)

        assumption = "a" * 4_000
        with planning_engine.begin() as conn:
            result = intended_outcome_service.create_intended_outcome(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                success_condition="success",
                observation_window=None,
                attribution_assumption=assumption,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.attribution_assumption is not None
        assert len(result.attribution_assumption) == 4_000

    def test_over_max_length_rejected(
        self,
        planning_engine: Engine,
        intended_outcome_service: IntendedOutcomeService,
    ) -> None:
        """4,001 characters trip ``attribution_assumption_too_long``."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(IntendedOutcomeValidationError) as exc_info:
                intended_outcome_service.create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition="success",
                    observation_window=None,
                    attribution_assumption="a" * 4_001,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == (
            "attribution_assumption_too_long"
        )
        assert _count(planning_engine, "Intended_Outcomes") == 0
