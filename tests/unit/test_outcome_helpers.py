"""Unit tests for :mod:`walking_slice.outcome._helpers` (fourth-walking-slice
task 3.2).

Covers the two shared Outcome_Service helpers introduced by task 3.2:

- :func:`_reject_prohibited_attributes` — rejects any Outcome_Service
  request body that carries an intended-side attribute (Requirement 53.2,
  53.3) or any field whose stated purpose is to assert Outcome from
  Completion alone or to alias a Completion Record as an Observed Outcome
  (Requirement 54.1, 54.4).
- :func:`_record_outcome_artifact` — registers a Slice 4 identifier in
  ``Identifier_Registry`` with the additive ``resource_kind`` tag
  required by Requirement 43.8 (seven disjoint Slice 4 identifier roles).
  On conflict it delegates to
  :meth:`IdentityService.reject_if_duplicate` so the Slice 1
  separate-transaction Denial Record pathway fires (Requirement 43.4).

These helpers are foundational for tasks 4.x..11.x; this module verifies
their contract in isolation.
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
from walking_slice.outcome._helpers import (
    COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS,
    OUTCOME_PROHIBITED_PREFIXES,
    OUTCOME_RESOURCE_KINDS,
    OutcomeValidationError,
    _record_outcome_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures and seed helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS = "2026-01-01T00:00:00.000Z"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@pytest.fixture
def outcome_engine(engine: Engine) -> Engine:
    """Engine with Slice 1 schema (which includes the additive
    ``Identifier_Registry.resource_kind`` column the helper populates)."""
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
        row = (
            conn.execute(
                text(
                    "SELECT identifier, kind, content_digest, resource_kind "
                    "FROM Identifier_Registry WHERE identifier = :id"
                ),
                {"id": identifier},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


# ===========================================================================
# _reject_prohibited_attributes — prefix rule (Requirement 53)
# ===========================================================================


class TestRejectProhibitedAttributes:
    """Requirements 53.2, 53.3, 54.1, 54.4."""

    def test_empty_body_passes(self) -> None:
        """An empty mapping never matches any prefix or marker; no exception."""
        _reject_prohibited_attributes({}, OUTCOME_PROHIBITED_PREFIXES)

    def test_non_prohibited_body_passes(self) -> None:
        """A body whose keys are legitimate outcome-measurement fields passes."""
        _reject_prohibited_attributes(
            {
                "measurand_description": "Time to first commit",
                "unit_of_measure": "hours",
                "target_intended_outcome_revision_id": _PARTY_ID,
                "cited_completion_record_ids": [_PARTY_ID],
            },
            OUTCOME_PROHIBITED_PREFIXES,
        )

    @pytest.mark.parametrize(
        "key",
        [
            "success-condition-statement",
            "attribution-assumption-text",
            "planned-deliverable-id",
            "plan-review-outcome",
            "plan-approval-outcome",
            "milestone-acceptance-outcome-value",
            "completion-outcome-value",
            "intended-outcome-kind",
        ],
    )
    def test_intended_side_prefix_rejected(self, key: str) -> None:
        """Every intended-side prefix from the design is rejected."""
        with pytest.raises(OutcomeValidationError) as exc_info:
            _reject_prohibited_attributes({key: "x"}, OUTCOME_PROHIBITED_PREFIXES)
        assert exc_info.value.prohibited_keys == (key,)

    @pytest.mark.parametrize("marker", list(COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS))
    def test_completion_intent_marker_rejected(self, marker: str) -> None:
        """Each Completion-as-Outcome intent marker is rejected as a substring.

        Requirement 54.4 — a field whose stated purpose is to assert
        Outcome from Completion alone, or to alias a Completion Record as
        an Observed Outcome, is rejected. The marker is matched anywhere
        in the key, so a leading qualifier does not evade detection.
        """
        key = f"mark_{marker.replace('-', '_')}_flag"
        with pytest.raises(OutcomeValidationError) as exc_info:
            _reject_prohibited_attributes({key: True}, OUTCOME_PROHIBITED_PREFIXES)
        assert exc_info.value.prohibited_keys == (key,)

    def test_completion_intent_active_even_with_empty_prefixes(self) -> None:
        """The intent rule applies regardless of the prefix list (Requirement 54)."""
        with pytest.raises(OutcomeValidationError) as exc_info:
            _reject_prohibited_attributes(
                {"completion_satisfies_outcome": True}, ()
            )
        assert exc_info.value.prohibited_keys == ("completion_satisfies_outcome",)

    def test_snake_case_keys_normalized(self) -> None:
        """``planned_deliverable_id`` matches the ``planned-`` prefix."""
        with pytest.raises(OutcomeValidationError) as exc_info:
            _reject_prohibited_attributes(
                {"planned_deliverable_id": "x"}, OUTCOME_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == ("planned_deliverable_id",)

    def test_case_insensitive_matching(self) -> None:
        """``Intended-Outcome-Kind`` matches the ``intended-`` prefix."""
        with pytest.raises(OutcomeValidationError) as exc_info:
            _reject_prohibited_attributes(
                {"Intended-Outcome-Kind": "intended"}, OUTCOME_PROHIBITED_PREFIXES
            )
        assert exc_info.value.prohibited_keys == ("Intended-Outcome-Kind",)

    def test_multiple_prohibited_keys_collected_in_order(self) -> None:
        """Every offending key surfaces in ``prohibited_keys`` per Requirement 53.3."""
        body = {
            "measurand_description": "ok",
            "success-condition-statement": "x",
            "completion_as_observed_outcome": True,
            "plan-approval-outcome": "approved",
        }
        with pytest.raises(OutcomeValidationError) as exc_info:
            _reject_prohibited_attributes(body, OUTCOME_PROHIBITED_PREFIXES)
        assert exc_info.value.prohibited_keys == (
            "success-condition-statement",
            "completion_as_observed_outcome",
            "plan-approval-outcome",
        )

    def test_prefix_match_only_for_prefix_rule(self) -> None:
        """A key that merely contains a prefix substring elsewhere is accepted.

        ``unplanned-note`` does not *start* with ``planned-`` and carries
        no intent marker, so it passes.
        """
        _reject_prohibited_attributes(
            {"unplanned-note": "ok"}, OUTCOME_PROHIBITED_PREFIXES
        )

    def test_legitimate_completion_citation_passes(self) -> None:
        """The permitted ``Cites`` Completion-Record identity field passes.

        The only permitted cross-slice reference to a Completion Record
        (Requirement 49) is a plain identity field; it carries no intent
        marker and is not rejected.
        """
        _reject_prohibited_attributes(
            {"cited_completion_record_ids": [_PARTY_ID]},
            OUTCOME_PROHIBITED_PREFIXES,
        )

    def test_non_mapping_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            _reject_prohibited_attributes(  # type: ignore[arg-type]
                ["not-a-mapping"], OUTCOME_PROHIBITED_PREFIXES
            )


def test_outcome_prohibited_prefixes_match_design() -> None:
    """The prefix tuple lists exactly the eight intended-side prefixes."""
    assert OUTCOME_PROHIBITED_PREFIXES == (
        "success-condition-",
        "attribution-assumption-",
        "planned-",
        "plan-review-",
        "plan-approval-",
        "milestone-acceptance-outcome-",
        "completion-outcome-",
        "intended-",
    )


# ===========================================================================
# _record_outcome_artifact — first binding inserts a tagged row.
# ===========================================================================


def test_record_outcome_artifact_inserts_row_with_resource_kind(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    """A first-time binding INSERTs a row carrying the ``resource_kind`` tag.

    Validates Requirement 43.8: the seven Slice 4 identifier roles are
    inspectable at row granularity through the additive ``resource_kind``
    column.
    """
    identifier = identity_service.new_resource_id()

    with outcome_engine.begin() as conn:
        _record_outcome_artifact(
            connection=conn,
            registry_kind="resource",
            resource_kind="measurement_definition",
            identifier=identifier,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )

    row = _fetch_registry_row(outcome_engine, identifier)
    assert row is not None
    assert row["identifier"] == identifier
    assert row["kind"] == "resource"
    assert row["content_digest"] == _DIGEST_A
    assert row["resource_kind"] == "measurement_definition"


def test_record_outcome_artifact_disjoint_kinds_distinguishable(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    """Distinct Slice 4 kinds are distinguishable by ``resource_kind``.

    Requirement 43.8 — seven disjoint identifier roles enforced at row
    level.
    """
    definition_id = identity_service.new_resource_id()
    record_id = identity_service.new_immutable_record_id()

    with outcome_engine.begin() as conn:
        _record_outcome_artifact(
            connection=conn,
            registry_kind="resource",
            resource_kind="measurement_definition",
            identifier=definition_id,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )
        _record_outcome_artifact(
            connection=conn,
            registry_kind="immutable_record",
            resource_kind="measurement_record",
            identifier=record_id,
            content_digest=_DIGEST_B,
            identity_service=identity_service,
        )

    definition_row = _fetch_registry_row(outcome_engine, definition_id)
    record_row = _fetch_registry_row(outcome_engine, record_id)
    assert (
        definition_row is not None
        and definition_row["resource_kind"] == "measurement_definition"
    )
    assert (
        record_row is not None
        and record_row["resource_kind"] == "measurement_record"
    )


def test_record_outcome_artifact_idempotent_same_digest(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    """Re-binding the same identifier to the same digest is a no-op."""
    identifier = identity_service.new_revision_id()

    for _ in range(2):
        with outcome_engine.begin() as conn:
            _record_outcome_artifact(
                connection=conn,
                registry_kind="revision",
                resource_kind="observed_outcome_revision",
                identifier=identifier,
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )

    with outcome_engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Identifier_Registry WHERE identifier = :id"
            ),
            {"id": identifier},
        ).scalar_one()
    assert count == 1


def test_record_outcome_artifact_conflict_raises_and_appends_denial(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    """A different-digest re-bind raises and appends a Denial Record.

    The conflict path drives through ``IdentityService.reject_if_duplicate``
    which writes a Denial Record in a separate transaction before
    re-raising :class:`IdentityConflictError` (Requirement 43.4).
    """
    identifier = identity_service.new_immutable_record_id()
    correlation_id = "00000000-0000-7000-8000-0000000000cc"

    with outcome_engine.begin() as conn:
        _seed_party(conn)
        _record_outcome_artifact(
            connection=conn,
            registry_kind="immutable_record",
            resource_kind="outcome_review_record",
            identifier=identifier,
            content_digest=_DIGEST_A,
            identity_service=identity_service,
        )

    with pytest.raises(IdentityConflictError):
        with outcome_engine.begin() as conn:
            _record_outcome_artifact(
                connection=conn,
                registry_kind="immutable_record",
                resource_kind="outcome_review_record",
                identifier=identifier,
                content_digest=_DIGEST_B,
                identity_service=identity_service,
                actor_party_id=_PARTY_ID,
                correlation_id=correlation_id,
                attempted_action="create.outcome_review",
            )

    # Original row remains byte-equivalent.
    row = _fetch_registry_row(outcome_engine, identifier)
    assert row is not None
    assert row["content_digest"] == _DIGEST_A

    # Denial Record was appended from the separate audit transaction.
    with outcome_engine.connect() as conn:
        denials = (
            conn.execute(
                text(
                    "SELECT reason_code, target_id, action_type, correlation_id "
                    "FROM Audit_Records WHERE reason_code = :reason"
                ),
                {"reason": IDENTIFIER_CONFLICT_REASON_CODE},
            )
            .mappings()
            .all()
        )
    assert len(denials) == 1
    assert denials[0]["target_id"] == identifier
    assert denials[0]["action_type"] == "create.outcome_review"
    assert denials[0]["correlation_id"] == correlation_id


def test_record_outcome_artifact_rejects_malformed_identifier(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    """Non-canonical UUIDv7 strings are rejected before any SQL is issued."""
    with pytest.raises(IdentityFormatError):
        with outcome_engine.begin() as conn:
            _record_outcome_artifact(
                connection=conn,
                registry_kind="resource",
                resource_kind="measurement_definition",
                identifier="not-a-uuid",
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )


def test_record_outcome_artifact_rejects_unknown_registry_kind(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    identifier = identity_service.new_resource_id()
    with pytest.raises(ValueError, match="unknown registry kind"):
        with outcome_engine.begin() as conn:
            _record_outcome_artifact(
                connection=conn,
                registry_kind="nonsense",
                resource_kind="measurement_definition",
                identifier=identifier,
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )


def test_record_outcome_artifact_rejects_unknown_resource_kind(
    outcome_engine: Engine, identity_service: IdentityService
) -> None:
    identifier = identity_service.new_resource_id()
    with pytest.raises(ValueError, match="unknown outcome resource_kind"):
        with outcome_engine.begin() as conn:
            _record_outcome_artifact(
                connection=conn,
                registry_kind="resource",
                # Slice 3 kind, not in OUTCOME_RESOURCE_KINDS
                resource_kind="completion_record",
                identifier=identifier,
                content_digest=_DIGEST_A,
                identity_service=identity_service,
            )


def test_outcome_resource_kinds_constant_matches_design() -> None:
    """The kinds set covers exactly the seven Slice 4 identifier roles."""
    assert OUTCOME_RESOURCE_KINDS == frozenset(
        {
            "measurement_definition",
            "measurement_definition_revision",
            "measurement_record",
            "observed_outcome",
            "observed_outcome_revision",
            "success_condition_assessment_record",
            "outcome_review_record",
        }
    )
