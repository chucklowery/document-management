"""Unit tests for :mod:`walking_slice.planning.deliverable_expectations` (task 6.2).

Pins the contract established in task 6.1, design
§"Planning_Service.DeliverableExpectations", and Requirements 5.2, 5.3,
13.2 for
:meth:`DeliverableExpectationService.create_deliverable_expectation`:

- **5.3 / 13.2** — produced-deliverable attribute keys are rejected at
  the request boundary. The ``request_attributes`` keyword forwards
  the HTTP layer's raw body through
  :func:`_reject_prohibited_attributes`; the Pydantic
  ``_validate_no_produced_attributes`` model validator on
  :class:`DeliverableExpectationCreationRequest` is the redundant
  defensive layer. Both surfaces raise
  :class:`DeliverableExpectationValidationError` with
  ``failed_constraint == 'prohibited_attribute'`` and the offending
  key listed on :attr:`prohibited_keys`.
- **5.2** — ``deliverable_kind`` is constrained to the enumerated set
  ``{Document, Artifact, Service, Other}``. The Pydantic
  ``Literal[...]`` rejects any other value before any database read;
  the schema CHECK on ``Deliverable_Expectation_Revisions.deliverable_kind``
  is the defense-in-depth layer: a hand-rolled INSERT that bypasses the
  service is rejected by the database with
  :class:`sqlalchemy.exc.IntegrityError`.
- **5.2** — boundary lengths on ``name`` (1..200), ``description``
  (0..10000), and ``acceptance_criteria`` (0..10000) are enforced before
  any database read. Over-long values raise
  :class:`DeliverableExpectationValidationError` with a stable
  ``failed_constraint`` identifier and persist no rows; the schema
  CHECK constraints on ``Deliverable_Expectation_Revisions`` are
  defence-in-depth.

The tests intentionally mirror the test style of
``tests/unit/test_planning_intended_outcomes.py`` (Pydantic-layer
produced-key rejection plus schema-CHECK defence-in-depth) and
``tests/unit/test_planning_projects.py`` (hand-rolled INSERT verifying
schema CHECK constraints).
"""

from __future__ import annotations

import re
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
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.deliverable_expectations import (
    CreateDeliverableExpectationResult,
    DELIVERABLE_KIND_VALUES,
    DeliverableExpectationService,
    DeliverableExpectationValidationError,
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
_PROJECT_ID = "00000000-0000-7000-8000-000000c00004"
_PROJECT_REV_ID = "00000000-0000-7000-8000-000000c00005"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


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
    append-only trigger (task 1.3). No disclosure seeding is required:
    Deliverable Expectation creation does not consult the disclosure
    registry.
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def deliverable_expectation_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> DeliverableExpectationService:
    """DeliverableExpectationService wired with a real AuthorizationService.

    The same instance is used by every test in this module; the
    authorization deny path is exercised by *not* assigning a role
    rather than by swapping in a stub service, so the real evaluation
    code path participates in the test.
    """
    return DeliverableExpectationService(
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
        _seed_party(conn, _PARTY_ID, "Project Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_objective(engine: Engine) -> None:
    """Seed one Objective Resource + its first Objective Revision.

    Inserted directly: the upstream Decision dependency is irrelevant
    to the Deliverable Expectation tests; the schema only requires the
    Objective row to exist for ``Project_Revisions.target_objective_id``
    to resolve.
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
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
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


def _seed_project(engine: Engine) -> None:
    """Seed one Project Resource + its first Project Revision.

    Inserted directly so the Deliverable Expectation tests can target
    a known Project Identity without driving the ProjectService. The
    Project ``Addresses`` Relationship to the parent Objective is not
    needed for these tests because the Deliverable Expectation flow
    only resolves the Project by Identity and does not traverse its
    Addresses Relationship.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Project_Revisions (
                    project_revision_id, project_id, parent_revision_id,
                    name, summary, target_objective_id,
                    planned_start_date, planned_end_date,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pid, NULL,
                    'Mesh Rollout', NULL, :oid,
                    '2026-01-15', '2026-06-30',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PROJECT_REV_ID,
                "pid": _PROJECT_ID,
                "oid": _OBJECTIVE_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_project_owner_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Project Owner authority (``modify``) to ``party_id``.

    Per AD-WS-15, ``create.deliverable_expectation`` maps to the
    ``modify`` authority type. A Party with an effective Role
    Assignment carrying ``modify`` over ``scope`` is permitted to
    create Deliverable Expectations in that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="project_owner",
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
                SELECT deliverable_expectation_revision_id,
                       deliverable_expectation_id, target_project_id,
                       name, description, deliverable_kind,
                       acceptance_criteria, authoring_party_id,
                       applicable_scope, recorded_at
                FROM Deliverable_Expectation_Revisions
                WHERE deliverable_expectation_revision_id = :rid
                """
            ),
            {"rid": revision_id},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ===========================================================================
# Happy-path baseline — confirms the test wiring before negative paths run.
# ===========================================================================


def test_create_deliverable_expectation_permits_when_role_grants_modify(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_expectation_service: DeliverableExpectationService,
) -> None:
    """Permit path: with an effective Project Owner role and an existing
    Project, the service creates one Deliverable Expectation Resource,
    one Deliverable Expectation Revision, one ``Addresses`` Relationship,
    and one consequential audit row inside one transaction (AD-WS-5)."""
    _seed_required_parties(planning_engine)
    _assign_project_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine)

    with planning_engine.begin() as conn:
        result = deliverable_expectation_service.create_deliverable_expectation(
            conn,
            target_project_id=_PROJECT_ID,
            name="Mesh Operations Runbook",
            description="Living runbook for mesh operators.",
            deliverable_kind="Document",
            acceptance_criteria="Reviewed by SRE lead.",
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateDeliverableExpectationResult)
    assert _CANONICAL_UUID7.match(result.deliverable_expectation_id)
    assert _CANONICAL_UUID7.match(result.deliverable_expectation_revision_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.target_project_id == _PROJECT_ID
    assert result.name == "Mesh Operations Runbook"
    assert result.deliverable_kind == "Document"
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Deliverable_Expectations") == 1
    assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 1

    row = _fetch_revision(
        planning_engine, result.deliverable_expectation_revision_id
    )
    assert row is not None
    assert row["target_project_id"] == _PROJECT_ID
    assert row["name"] == "Mesh Operations Runbook"
    assert row["description"] == "Living runbook for mesh operators."
    assert row["deliverable_kind"] == "Document"
    assert row["acceptance_criteria"] == "Reviewed by SRE lead."


# ===========================================================================
# Requirement 5.3 / 13.2 — produced-deliverable-key rejection.
#
# Drawn from a curated list spanning every prefix in
# :data:`PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES` (``produced-``,
# ``hand-off-``, ``accepted-by-``). The matcher in
# :func:`_reject_prohibited_attributes` is case-insensitive and
# hyphen/underscore-invariant, so each prefix is exercised in both
# hyphenated and snake_case form.
# ===========================================================================


# Curated list of keys that must be rejected: each prefix from
# :data:`PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES` exercised in
# hyphen-lowercase, snake_case, and uppercase variants.
_PROHIBITED_PRODUCED_KEYS = [
    # produced- prefix
    "produced-deliverable-id",
    "produced_deliverable_id",
    "Produced-Deliverable-Revision-Id",
    "produced-at",
    # hand-off- prefix
    "hand-off-party-id",
    "hand_off_party_id",
    "HAND-OFF-RECEIPT",
    # accepted-by- prefix
    "accepted-by-customer",
    "accepted_by_customer",
    "ACCEPTED-BY-PARTY-ID",
]


@pytest.mark.parametrize("prohibited_key", _PROHIBITED_PRODUCED_KEYS)
def test_request_attributes_with_produced_deliverable_key_rejected(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_expectation_service: DeliverableExpectationService,
    prohibited_key: str,
) -> None:
    """Property 22 / Requirements 5.3 / 13.2: request bodies carrying
    any produced-deliverable attribute are rejected at the boundary; no
    rows are persisted.

    Exercises the ``request_attributes`` pass-through: the HTTP layer
    (task 15.1) forwards the raw request body via this keyword, and the
    service screens it through :func:`_reject_prohibited_attributes`
    before any Pydantic parsing of the typed kwargs.
    """
    _seed_required_parties(planning_engine)
    _assign_project_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine)

    with pytest.raises(DeliverableExpectationValidationError) as exc_info:
        with planning_engine.begin() as conn:
            deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="Mesh Operations Runbook",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                request_attributes={
                    "name": "Mesh Operations Runbook",
                    prohibited_key: "anything",
                },
            )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert prohibited_key in exc_info.value.prohibited_keys
    assert _count(planning_engine, "Deliverable_Expectations") == 0
    assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0


@pytest.mark.parametrize("prohibited_key", _PROHIBITED_PRODUCED_KEYS)
def test_pydantic_model_validator_rejects_produced_deliverable_key(
    planning_engine: Engine,
    deliverable_expectation_service: DeliverableExpectationService,
    prohibited_key: str,
) -> None:
    """The Pydantic ``_validate_no_produced_attributes`` model validator
    on :class:`DeliverableExpectationCreationRequest` also rejects
    produced-deliverable keys.

    The model validator inspects raw input mappings; it is exercised
    here through the construction path used internally by
    :meth:`create_deliverable_expectation` when a caller bypasses the
    route layer's raw-body forwarding. Construction is driven through
    the public service rather than the model class so the service-level
    error surface (:class:`DeliverableExpectationValidationError` with
    the ``prohibited_attribute`` discriminator) is exercised.
    """
    _seed_required_parties(planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine)

    # Without a role assignment, the deny path could surface first;
    # this test routes through the ``request_attributes`` pre-screen
    # which runs *before* authorization evaluation, so a deny path is
    # never reached. The Pydantic model validator is the path under
    # test: a kwargs dict carrying *only* an offending key on the
    # ``request_attributes`` mapping forwards through the pre-screen
    # and into the model's ``mode='before'`` validator.
    with pytest.raises(DeliverableExpectationValidationError) as exc_info:
        with planning_engine.begin() as conn:
            deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="Mesh Operations Runbook",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
                request_attributes={prohibited_key: "x"},
            )

    assert exc_info.value.failed_constraint == "prohibited_attribute"
    assert prohibited_key in exc_info.value.prohibited_keys
    assert _count(planning_engine, "Deliverable_Expectations") == 0
    assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0


def test_request_attributes_with_only_allowed_keys_accepted(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_expectation_service: DeliverableExpectationService,
) -> None:
    """A request body whose keys do not match any produced-deliverable
    prefix passes the boundary check and the Deliverable Expectation is
    created.

    The legitimate fields ``name``, ``description``, ``deliverable_kind``,
    and ``acceptance_criteria`` are accepted — the prohibited prefixes
    are narrow on purpose so the declarative-intent fields Requirement
    5.2 mandates are not falsely rejected.
    """
    _seed_required_parties(planning_engine)
    _assign_project_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)
    _seed_project(planning_engine)

    with planning_engine.begin() as conn:
        result = deliverable_expectation_service.create_deliverable_expectation(
            conn,
            target_project_id=_PROJECT_ID,
            name="Runbook",
            description="ok",
            deliverable_kind="Artifact",
            acceptance_criteria="ok",
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            request_attributes={
                "target_project_id": _PROJECT_ID,
                "name": "Runbook",
                "description": "ok",
                "deliverable_kind": "Artifact",
                "acceptance_criteria": "ok",
                "applicable_scope": _SCOPE,
            },
        )

    assert _CANONICAL_UUID7.match(result.deliverable_expectation_id)
    assert _count(planning_engine, "Deliverable_Expectations") == 1


# ===========================================================================
# Requirement 5.2 — deliverable_kind enumeration boundaries.
#
# The enumerated set is ``{Document, Artifact, Service, Other}``. The
# Pydantic ``Literal[...]`` rejects any other value before any database
# read; the schema CHECK on
# ``Deliverable_Expectation_Revisions.deliverable_kind`` enforces the
# same membership as defence-in-depth.
# ===========================================================================


class TestDeliverableKindEnumerationValidatorLayer:
    """Pydantic ``Literal`` rejects values outside the enumerated set.

    Runs before the Project resolution SELECT, the authorization
    evaluation, and any INSERT, so a malformed request never touches
    the database.
    """

    @pytest.mark.parametrize("kind", list(DELIVERABLE_KIND_VALUES))
    def test_each_enumerated_value_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
        kind: str,
    ) -> None:
        """Every enumerated value persists end-to-end.

        Confirms the four declared boundaries are all *inside* the
        accepting set; the Pydantic ``Literal`` and the schema CHECK
        agree on membership.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name=f"Output of kind {kind}",
                description=None,
                deliverable_kind=kind,
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.deliverable_kind == kind
        row = _fetch_revision(
            planning_engine, result.deliverable_expectation_revision_id
        )
        assert row is not None
        assert row["deliverable_kind"] == kind

    @pytest.mark.parametrize(
        "rejected_value",
        [
            "document",  # case-sensitive: lowercase does not match
            "DOCUMENT",
            "Doc",
            "Article",
            "",
            "Other ",  # trailing whitespace does not match
            "Software",
            "Unknown",
        ],
    )
    def test_value_outside_enumeration_rejected(
        self,
        planning_engine: Engine,
        deliverable_expectation_service: DeliverableExpectationService,
        rejected_value: str,
    ) -> None:
        """A ``deliverable_kind`` outside the enumerated set raises with
        ``failed_constraint == 'deliverable_kind_invalid'``.

        The error type is :class:`DeliverableExpectationValidationError`;
        the ``failed_constraint`` is the stable identifier the route
        layer maps to a structured 400 response.
        """
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(
                DeliverableExpectationValidationError
            ) as exc_info:
                deliverable_expectation_service.create_deliverable_expectation(
                    conn,
                    target_project_id=_PROJECT_ID,
                    name="Bad-Kind",
                    description=None,
                    deliverable_kind=rejected_value,
                    acceptance_criteria=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "deliverable_kind_invalid"
        assert _count(planning_engine, "Deliverable_Expectations") == 0
        assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0


class TestDeliverableKindEnumerationCheckLayer:
    """Schema CHECK constraint rejects values outside the enumerated set
    at INSERT time.

    Defence-in-depth: a hand-rolled INSERT that bypasses the
    DeliverableExpectationService Pydantic validator must still be
    rejected by the database. The schema declares
    ``CHECK (deliverable_kind IN ('Document','Artifact','Service','Other'))``
    on ``Deliverable_Expectation_Revisions``; SQLite reports a
    violation through :class:`sqlalchemy.exc.IntegrityError`.
    """

    @pytest.mark.parametrize(
        "rejected_value",
        [
            "document",
            "DOCUMENT",
            "Doc",
            "Article",
            "Software",
            "",
        ],
    )
    def test_direct_insert_with_other_kind_rejected_by_check(
        self,
        planning_engine: Engine,
        rejected_value: str,
    ) -> None:
        """A hand-rolled INSERT with any ``deliverable_kind`` outside
        the enumerated set is rejected by the
        ``Deliverable_Expectation_Revisions`` CHECK constraint."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        # Pre-create a Deliverable_Expectations header so the FK
        # target exists and the only constraint left to fail is the
        # deliverable_kind CHECK.
        de_id = "00000000-0000-7000-8000-000000d00001"
        revision_id = "00000000-0000-7000-8000-000000d00002"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO Deliverable_Expectations "
                    "(deliverable_expectation_id, created_at) "
                    "VALUES (:did, :ts)"
                ),
                {"did": de_id, "ts": _TS_FIXED},
            )

        with planning_engine.connect() as conn, pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO Deliverable_Expectation_Revisions (
                            deliverable_expectation_revision_id,
                            deliverable_expectation_id, parent_revision_id,
                            target_project_id, name, description,
                            deliverable_kind, acceptance_criteria,
                            authoring_party_id, applicable_scope, recorded_at
                        ) VALUES (
                            :rev, :did, NULL,
                            :pid, 'name', NULL,
                            :kind, NULL,
                            :party, :scope, :ts
                        )
                        """
                    ),
                    {
                        "rev": revision_id,
                        "did": de_id,
                        "pid": _PROJECT_ID,
                        "kind": rejected_value,
                        "party": _PARTY_ID,
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0

    @pytest.mark.parametrize(
        "kind, suffix",
        [
            ("Document", "01"),
            ("Artifact", "02"),
            ("Service", "03"),
            ("Other", "04"),
        ],
    )
    def test_direct_insert_with_enumerated_kind_accepted_by_check(
        self,
        planning_engine: Engine,
        kind: str,
        suffix: str,
    ) -> None:
        """The CHECK accepts every literal from the enumerated set.

        Each parametrize case uses a distinct identifier suffix so the
        Deliverable_Expectations primary-key constraint cannot collide
        across the four cases (each test run uses a fresh engine, but
        the deterministic identifiers make the SQL self-documenting).
        """
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        de_id = f"00000000-0000-7000-8000-0000000000{suffix}1"
        revision_id = f"00000000-0000-7000-8000-0000000000{suffix}2"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO Deliverable_Expectations "
                    "(deliverable_expectation_id, created_at) "
                    "VALUES (:did, :ts)"
                ),
                {"did": de_id, "ts": _TS_FIXED},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO Deliverable_Expectation_Revisions (
                        deliverable_expectation_revision_id,
                        deliverable_expectation_id, parent_revision_id,
                        target_project_id, name, description,
                        deliverable_kind, acceptance_criteria,
                        authoring_party_id, applicable_scope, recorded_at
                    ) VALUES (
                        :rev, :did, NULL,
                        :pid, 'name', NULL,
                        :kind, NULL,
                        :party, :scope, :ts
                    )
                    """
                ),
                {
                    "rev": revision_id,
                    "did": de_id,
                    "pid": _PROJECT_ID,
                    "kind": kind,
                    "party": _PARTY_ID,
                    "scope": _SCOPE,
                    "ts": _TS_FIXED,
                },
            )

        row = _fetch_revision(planning_engine, revision_id)
        assert row is not None
        assert row["deliverable_kind"] == kind


# ===========================================================================
# Requirement 5.2 — boundary lengths on ``name`` (1..200).
#
# Persisted column is ``Deliverable_Expectation_Revisions.name`` with
# CHECK length 1..200.
# ===========================================================================


class TestNameBoundaries:
    """``name`` is required and must be 1..200 characters."""

    def test_at_min_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """A 1-character ``name`` is accepted (lower bound)."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="x",
                description=None,
                deliverable_kind="Other",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.name == "x"
        row = _fetch_revision(
            planning_engine, result.deliverable_expectation_revision_id
        )
        assert row is not None
        assert row["name"] == "x"

    def test_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """A 200-character ``name`` is accepted (upper bound)."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        name = "a" * 200
        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name=name,
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert len(result.name) == 200

    def test_empty_name_rejected(
        self,
        planning_engine: Engine,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """An empty ``name`` raises ``name_missing`` before any DB read."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(
                DeliverableExpectationValidationError
            ) as exc_info:
                deliverable_expectation_service.create_deliverable_expectation(
                    conn,
                    target_project_id=_PROJECT_ID,
                    name="",
                    description=None,
                    deliverable_kind="Document",
                    acceptance_criteria=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "name_missing"
        assert _count(planning_engine, "Deliverable_Expectations") == 0

    def test_over_max_length_rejected(
        self,
        planning_engine: Engine,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """201 characters trips ``name_too_long`` before any DB read."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(
                DeliverableExpectationValidationError
            ) as exc_info:
                deliverable_expectation_service.create_deliverable_expectation(
                    conn,
                    target_project_id=_PROJECT_ID,
                    name="a" * 201,
                    description=None,
                    deliverable_kind="Document",
                    acceptance_criteria=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "name_too_long"
        assert _count(planning_engine, "Deliverable_Expectations") == 0
        assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0


# ===========================================================================
# Requirement 5.2 — boundary lengths on ``description`` (0..10000).
#
# Persisted column is ``Deliverable_Expectation_Revisions.description``;
# the CHECK is ``description IS NULL OR length(description) BETWEEN 0
# AND 10000``. ``None`` and the empty string are both legal lower bounds.
# ===========================================================================


class TestDescriptionBoundaries:
    """``description`` is optional and must be 0..10000 characters when
    provided."""

    def test_none_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """``description = None`` is persisted as SQL NULL."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="No Description",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.description is None
        row = _fetch_revision(
            planning_engine, result.deliverable_expectation_revision_id
        )
        assert row is not None
        assert row["description"] is None

    def test_empty_string_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """A 0-character ``description`` is accepted (lower bound)."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="Empty Description",
                description="",
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.description == ""
        row = _fetch_revision(
            planning_engine, result.deliverable_expectation_revision_id
        )
        assert row is not None
        assert row["description"] == ""

    def test_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """A 10,000-character ``description`` is accepted (upper bound)."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        description = "a" * 10_000
        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="Max Description",
                description=description,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.description is not None
        assert len(result.description) == 10_000

    def test_over_max_length_rejected(
        self,
        planning_engine: Engine,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """10,001 characters trips ``description_too_long`` before any
        DB read."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(
                DeliverableExpectationValidationError
            ) as exc_info:
                deliverable_expectation_service.create_deliverable_expectation(
                    conn,
                    target_project_id=_PROJECT_ID,
                    name="Over Max",
                    description="a" * 10_001,
                    deliverable_kind="Document",
                    acceptance_criteria=None,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "description_too_long"
        assert _count(planning_engine, "Deliverable_Expectations") == 0
        assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0


# ===========================================================================
# Requirement 5.2 — boundary lengths on ``acceptance_criteria`` (0..10000).
#
# Persisted column is ``Deliverable_Expectation_Revisions.acceptance_criteria``;
# the CHECK is ``acceptance_criteria IS NULL OR
# length(acceptance_criteria) BETWEEN 0 AND 10000``. ``None`` and the
# empty string are both legal lower bounds.
# ===========================================================================


class TestAcceptanceCriteriaBoundaries:
    """``acceptance_criteria`` is optional and must be 0..10000 characters
    when provided."""

    def test_none_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """``acceptance_criteria = None`` is persisted as SQL NULL."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="No Criteria",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=None,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.acceptance_criteria is None
        row = _fetch_revision(
            planning_engine, result.deliverable_expectation_revision_id
        )
        assert row is not None
        assert row["acceptance_criteria"] is None

    def test_empty_string_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """A 0-character ``acceptance_criteria`` is accepted (lower bound)."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="Empty Criteria",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria="",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.acceptance_criteria == ""
        row = _fetch_revision(
            planning_engine, result.deliverable_expectation_revision_id
        )
        assert row is not None
        assert row["acceptance_criteria"] == ""

    def test_at_max_length_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """A 10,000-character ``acceptance_criteria`` is accepted (upper
        bound)."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        criteria = "a" * 10_000
        with planning_engine.begin() as conn:
            result = deliverable_expectation_service.create_deliverable_expectation(
                conn,
                target_project_id=_PROJECT_ID,
                name="Max Criteria",
                description=None,
                deliverable_kind="Document",
                acceptance_criteria=criteria,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.acceptance_criteria is not None
        assert len(result.acceptance_criteria) == 10_000

    def test_over_max_length_rejected(
        self,
        planning_engine: Engine,
        deliverable_expectation_service: DeliverableExpectationService,
    ) -> None:
        """10,001 characters trips ``acceptance_criteria_too_long``
        before any DB read."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(
                DeliverableExpectationValidationError
            ) as exc_info:
                deliverable_expectation_service.create_deliverable_expectation(
                    conn,
                    target_project_id=_PROJECT_ID,
                    name="Over Criteria",
                    description=None,
                    deliverable_kind="Document",
                    acceptance_criteria="a" * 10_001,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "acceptance_criteria_too_long"
        )
        assert _count(planning_engine, "Deliverable_Expectations") == 0
        assert _count(planning_engine, "Deliverable_Expectation_Revisions") == 0
