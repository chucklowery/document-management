"""Unit tests for :mod:`walking_slice.planning._helpers` (task 2.2).

Covers the two shared Planning_Service helpers introduced by task 2.2:

- :func:`_record_planning_resource` — registers a Slice 2 identifier in
  ``Identifier_Registry`` with the additive ``resource_kind`` tag
  required by Requirement 4.5 (Project / Activity Plan identifier-set
  disjointness). On conflict it delegates to
  :meth:`IdentityService.reject_if_duplicate` so the Slice 1
  separate-transaction Denial Record pathway fires (design §"Error
  Handling — Identifier conflict").
- :func:`_reject_prohibited_attributes` — rejects any Planning_Service
  request body that carries an execution, observed-outcome, or
  produced-deliverable attribute (Property 22 / Requirements 12.1,
  12.2, 13.1, 13.2, 13.5).

These helpers are foundational for tasks 3.x..11.x; this module
verifies their contract in isolation.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.identity import (
    IDENTIFIER_CONFLICT_REASON_CODE,
    IdentityConflictError,
    IdentityFormatError,
    IdentityService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    EXECUTION_PROHIBITED_PREFIXES,
    OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
    PLANNING_RESOURCE_KINDS,
    PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures and seed helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS = "2026-01-01T00:00:00.000Z"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Engine with Slice 1 schema (which includes the additive
    ``Identifier_Registry.resource_kind`` column applied by task 1.2)."""
    create_schema(engine)
    return engine


def _seed_party(conn, party_id: str = _PARTY_ID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Test Party', :ts)
            """
        ),
        {"pid": party_id, "ts": _TS},
    )


def _fetch_registry_row(engine: Engine, identifier: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT identifier, kind, content_digest, resource_kind "
                "FROM Identifier_Registry WHERE identifier = :id"
            ),
            {"id": identifier},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ===========================================================================
# _reject_prohibited_attributes
# ===========================================================================


class TestRejectProhibitedAttributes:
    """Property 22 / Requirements 12.1, 12.2, 13.1, 13.2, 13.5."""

    def test_empty_body_passes(self) -> None:
        """An empty mapping never matches any prefix; no exception."""
        _reject_prohibited_attributes({}, EXECUTION_PROHIBITED_PREFIXES)

    def test_non_prohibited_body_passes(self) -> None:
        """A body whose keys do not match any prefix is accepted."""
        _reject_prohibited_attributes(
            {"statement": "Reduce onboarding time", "rationale": "ok"},
            ALL_PROHIBITED_PREFIXES,
        )

    @pytest.mark.parametrize(
        "key",
        [
            "work-started-at",
            "time-spent",
            "milestone-acceptance",
            "deliverable-production-record",
            "blockage-observation",
            "completion-evidence",
            "actual-cost",
            "percent-complete-value",
            "remaining-work",
        ],
    )
    def test_execution_prefix_rejected(self, key: str) -> None:
        """Every execution prefix from design §Property 22 is rejected."""
        with pytest.raises(PlanningValidationError) as exc_info:
            _reject_prohibited_attributes(
                {key: "anything"}, EXECUTION_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == (key,)

    @pytest.mark.parametrize(
        "key",
        [
            "observed-outcome",
            "observation-time-window",
            "attribution-evidence-ref",
        ],
    )
    def test_observed_outcome_prefix_rejected(self, key: str) -> None:
        with pytest.raises(PlanningValidationError) as exc_info:
            _reject_prohibited_attributes(
                {key: "x"}, OBSERVED_OUTCOME_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == (key,)

    @pytest.mark.parametrize(
        "key",
        ["produced-deliverable-id", "hand-off-receipt", "accepted-by-customer"],
    )
    def test_produced_deliverable_prefix_rejected(self, key: str) -> None:
        with pytest.raises(PlanningValidationError) as exc_info:
            _reject_prohibited_attributes(
                {key: "x"}, PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == (key,)

    def test_snake_case_keys_normalized(self) -> None:
        """``work_started_at`` matches the ``work-`` prefix (Requirement 13.5)."""
        with pytest.raises(PlanningValidationError) as exc_info:
            _reject_prohibited_attributes(
                {"work_started_at": "now"}, EXECUTION_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == ("work_started_at",)

    def test_case_insensitive_matching(self) -> None:
        """``Work-Started-At`` matches the ``work-`` prefix."""
        with pytest.raises(PlanningValidationError) as exc_info:
            _reject_prohibited_attributes(
                {"Work-Started-At": "now"}, EXECUTION_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == ("Work-Started-At",)

    def test_multiple_prohibited_keys_collected(self) -> None:
        """Every offending key surfaces in ``prohibited_keys`` per Requirement 13.5."""
        body = {
            "statement": "ok",
            "work-started-at": "2026-01-01",
            "actual-cost": 100,
        }
        with pytest.raises(PlanningValidationError) as exc_info:
            _reject_prohibited_attributes(body, ALL_PROHIBITED_PREFIXES)
        # Order preserved as they appear in the mapping.
        assert exc_info.value.prohibited_keys == (
            "work-started-at",
            "actual-cost",
        )

    def test_empty_prefix_list_passes(self) -> None:
        """An empty prefix tuple disables matching entirely."""
        _reject_prohibited_attributes({"work-started-at": "x"}, ())

    def test_non_mapping_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            _reject_prohibited_attributes(  # type: ignore[arg-type]
                ["not-a-mapping"], EXECUTION_PROHIBITED_PREFIXES
            )

    def test_prefix_prefix_match_only(self) -> None:
        """Keys that merely contain the substring elsewhere are not rejected."""
        # 'rework-summary' starts with 'rework-', not 'work-'.
        _reject_prohibited_attributes(
            {"rework-summary": "not-execution"}, EXECUTION_PROHIBITED_PREFIXES
        )


# ===========================================================================
# _record_planning_resource — first binding inserts a tagged row.
# ===========================================================================


def test_record_planning_resource_inserts_row_with_resource_kind(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    """A first-time binding INSERTs a row carrying the ``resource_kind`` tag.

    Validates Requirement 4.5: Project / Activity Plan identifier sets
    are disjoint at row granularity through the additive
    ``resource_kind`` column.
    """
    identifier = identity_service.new_resource_id()

    with planning_engine.begin() as conn:
        _record_planning_resource(
            connection=conn,
            registry_kind="resource",
            resource_kind="project",
            identifier=identifier,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )

    row = _fetch_registry_row(planning_engine, identifier)
    assert row is not None
    assert row["identifier"] == identifier
    assert row["kind"] == "resource"
    assert row["content_digest"] == _DIGEST_A
    assert row["resource_kind"] == "project"


def test_record_planning_resource_disjoint_kinds_distinguishable(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    """Project vs Activity Plan rows are distinguishable by ``resource_kind``.

    Requirement 4.5 — disjoint identifier sets enforced at row level.
    """
    project_id = identity_service.new_resource_id()
    activity_plan_id = identity_service.new_resource_id()

    with planning_engine.begin() as conn:
        _record_planning_resource(
            connection=conn,
            registry_kind="resource",
            resource_kind="project",
            identifier=project_id,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )
        _record_planning_resource(
            connection=conn,
            registry_kind="resource",
            resource_kind="activity_plan",
            identifier=activity_plan_id,
            content_digest=_DIGEST_B,
            identity_service=identity_service,
        )

    project_row = _fetch_registry_row(planning_engine, project_id)
    activity_row = _fetch_registry_row(planning_engine, activity_plan_id)
    assert project_row is not None and project_row["resource_kind"] == "project"
    assert (
        activity_row is not None
        and activity_row["resource_kind"] == "activity_plan"
    )


def test_record_planning_resource_idempotent_same_digest(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    """Re-binding the same identifier to the same digest is a no-op."""
    identifier = identity_service.new_resource_id()

    with planning_engine.begin() as conn:
        _record_planning_resource(
            connection=conn,
            registry_kind="resource",
            resource_kind="objective",
            identifier=identifier,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )

    with planning_engine.begin() as conn:
        # Same digest — should return silently, no second row.
        _record_planning_resource(
            connection=conn,
            registry_kind="resource",
            resource_kind="objective",
            identifier=identifier,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )

    with planning_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Identifier_Registry "
                "WHERE identifier = :id"
            ),
            {"id": identifier},
        ).scalar_one()
    assert count == 1


def test_record_planning_resource_conflict_raises_and_appends_denial(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    """A different-digest re-bind raises and appends a Denial Record.

    Mirrors design §"Error Handling — Identifier conflict (Requirement 1.4)":
    the conflict path drives through ``IdentityService.reject_if_duplicate``
    which writes a Denial Record in a separate transaction before
    re-raising :class:`IdentityConflictError`.
    """
    identifier = identity_service.new_resource_id()
    correlation_id = "00000000-0000-7000-8000-0000000000cc"

    with planning_engine.begin() as conn:
        _seed_party(conn)
        _record_planning_resource(
            connection=conn,
            registry_kind="resource",
            resource_kind="project",
            identifier=identifier,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )

    with pytest.raises(IdentityConflictError):
        with planning_engine.begin() as conn:
            _record_planning_resource(
                connection=conn,
                registry_kind="resource",
                resource_kind="project",
                identifier=identifier,
                content_digest=_DIGEST_B,
                identity_service=identity_service,
                actor_party_id=_PARTY_ID,
                correlation_id=correlation_id,
                attempted_action="create.project",
            )

    # Original row remains byte-equivalent.
    row = _fetch_registry_row(planning_engine, identifier)
    assert row is not None
    assert row["content_digest"] == _DIGEST_A

    # Denial Record was appended from the separate audit transaction.
    with planning_engine.connect() as conn:
        denials = conn.execute(
            text(
                "SELECT reason_code, target_id, action_type, correlation_id "
                "FROM Audit_Records "
                "WHERE reason_code = :reason"
            ),
            {"reason": IDENTIFIER_CONFLICT_REASON_CODE},
        ).mappings().all()
    assert len(denials) == 1
    assert denials[0]["target_id"] == identifier
    assert denials[0]["action_type"] == "create.project"
    assert denials[0]["correlation_id"] == correlation_id


def test_record_planning_resource_rejects_malformed_identifier(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    """Non-canonical UUIDv7 strings are rejected before any SQL is issued."""
    with pytest.raises(IdentityFormatError):
        with planning_engine.begin() as conn:
            _record_planning_resource(
                connection=conn,
                registry_kind="resource",
                resource_kind="project",
                identifier="not-a-uuid",
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )


def test_record_planning_resource_rejects_unknown_registry_kind(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    identifier = identity_service.new_resource_id()
    with pytest.raises(ValueError, match="unknown registry kind"):
        with planning_engine.begin() as conn:
            _record_planning_resource(
                connection=conn,
                registry_kind="nonsense",
                resource_kind="project",
                identifier=identifier,
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )


def test_record_planning_resource_rejects_unknown_resource_kind(
    planning_engine: Engine, identity_service: IdentityService
) -> None:
    identifier = identity_service.new_resource_id()
    with pytest.raises(ValueError, match="unknown planning resource_kind"):
        with planning_engine.begin() as conn:
            _record_planning_resource(
                connection=conn,
                registry_kind="resource",
                resource_kind="document",  # Slice 1 kind, not in PLANNING_RESOURCE_KINDS
                identifier=identifier,
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )


def test_planning_resource_kinds_constant_matches_design() -> None:
    """The kinds tuple covers every Slice 2 Resource and Revision."""
    expected = {
        "objective",
        "objective_revision",
        "intended_outcome",
        "intended_outcome_revision",
        "project",
        "project_revision",
        "deliverable_expectation",
        "deliverable_expectation_revision",
        "activity_plan",
        "plan_revision",
        "plan_review",
        "plan_review_revision",
        "plan_approval",
    }
    assert PLANNING_RESOURCE_KINDS == expected
