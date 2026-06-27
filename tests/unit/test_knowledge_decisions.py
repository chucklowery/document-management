"""Unit tests for :mod:`walking_slice.knowledge` — Decisions (task 8.1).

These tests pin the contract established in task 8.1, design
§"Knowledge_Service" + §"Decisions" + §"Provenance_Manifests and
Omission_Entries", and Requirements 6.1 through 6.7 plus the
authority-basis enumeration in AD-WS-10 and the outcome restriction in
AD-WS-11:

- 6.1 — a Party submitting a Decision on a decidable Recommendation
  Revision causes the Knowledge_Service to create one Decision
  Immutable Record.
- 6.2 — the Decision Record carries target Recommendation Identity and
  Revision Identity, outcome from ``{Accept, Reject, Defer}``,
  rationale of 1..4,000 characters, deciding Party Identity, authority
  basis from ``{role-grant-id, scope-id, delegation-chain-id}`` (per
  AD-WS-10), applicable scope, and recorded time at millisecond
  precision.
- 6.3 — the Knowledge_Service links the Decision to its target
  Recommendation Revision through exactly one ``Addresses``
  Relationship.
- 6.4 — the Audit_Log appends a ``create.decision`` consequential row
  in the same transaction as the Decision (AD-WS-5).
- 6.5 — submitting a Decision on a Recommendation Revision that
  already has a finalized Decision is rejected; the database also
  enforces the rule via a ``UNIQUE`` constraint.
- 6.6 — Decisions are immutable; UPDATE and DELETE are rejected by
  the persistence triggers installed in task 1.3 (reaffirmed here so
  the contract is exercised by Decision rows specifically).
- 6.7 — every required attribute (target Recommendation Identity,
  target Recommendation Revision Identity, outcome, rationale,
  deciding Party Identity, authority basis, applicable scope) is
  validated; the recorded time is sourced from the Clock so it is
  always present and is not validated here.

Provenance Manifest insertion (Requirement 10.1) is also exercised
here because task 8.1 makes the Decision flow responsible for
recording the manifest alongside the Decision itself (AD-WS-5):
the manifest's ``subject_kind`` is ``'decision'``, the manifest's
``included_sources_json`` lists the target Recommendation Revision,
and ``is_complete`` reflects whether any non-intentional Omission
Entry was supplied.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateDecisionResult,
    CreateFindingResult,
    CreateRecommendationResult,
    DecisionConflictError,
    DecisionOmissionEntry,
    DecisionValidationError,
    KnowledgeService,
    RecommendationRevisionNotResolvableError,
)
from walking_slice.models import AuthorityBasisRef


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seeding helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000000002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def _seed_party(conn, party_id: str = _PARTY_ID, display: str = "Decider") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """Knowledge_Service wired without authorization.

    Task 8.1 explicitly excludes the authorization check (task 8.2
    adds it). The service is wired without an
    :class:`AuthorizationService` so the persistence path is the only
    behavior exercised here.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def basis() -> AuthorityBasisRef:
    """A canonical role-grant authority basis used by happy-path tests."""
    return AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)


# ---------------------------------------------------------------------------
# Seed helpers — build a Recommendation Revision to target.
# ---------------------------------------------------------------------------


def _seed_recommendation(
    engine: Engine,
    knowledge_service: KnowledgeService,
    *,
    seed_party: bool = True,
) -> tuple[CreateFindingResult, CreateRecommendationResult]:
    """Seed a Party, a hypothesis Finding, and one Recommendation
    derived from it. Returns the Finding and Recommendation results
    so individual tests can target the Recommendation Revision they
    just created."""
    with engine.begin() as conn:
        if seed_party:
            _seed_party(conn)
        finding = knowledge_service.create_finding(
            conn,
            statement="Source finding for decision tests.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend action X based on hypothesis Finding.",
        )
    return finding, recommendation


# ---------------------------------------------------------------------------
# Row readers.
# ---------------------------------------------------------------------------


def _fetch_decision(engine: Engine, *, decision_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT decision_id, target_recommendation_id,
                           target_recommendation_revision_id, outcome,
                           rationale, deciding_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Decisions WHERE decision_id = :did
                    """
                ),
                {"did": decision_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_relationships_by_source(
    engine: Engine, *, source_id: str
) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, source_kind,
                           source_id, source_revision_id, target_kind,
                           target_id, target_revision_id, authoring_party_id,
                           recorded_at
                    FROM Relationships WHERE source_id = :sid
                    ORDER BY relationship_id
                    """
                ),
                {"sid": source_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_manifests(engine: Engine, *, subject_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT manifest_id, subject_kind, subject_id,
                           subject_revision_id, authoring_party_id,
                           recorded_at, included_sources_json, is_complete
                    FROM Provenance_Manifests
                    WHERE subject_id = :sid
                    """
                ),
                {"sid": subject_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_omissions(engine: Engine, *, manifest_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT omission_entry_id, manifest_id, excluded_source_id,
                           excluded_source_revision_id, category, rationale,
                           authoring_party_id, recorded_at, resolved_at
                    FROM Omission_Entries WHERE manifest_id = :mid
                    ORDER BY omission_entry_id
                    """
                ),
                {"mid": manifest_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_audit_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT actor_party_id, action_type, outcome, target_id,
                           target_revision_id, correlation_id, recorded_at
                    FROM Audit_Records ORDER BY append_sequence
                    """
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# create_decision — happy paths.
# ---------------------------------------------------------------------------


def test_create_decision_persists_decision_addresses_manifest_and_audit(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """The happy path persists the Decision, the Addresses Relationship,
    the Provenance Manifest, and a consequential audit row in one
    transaction (AD-WS-5)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)

    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Decision rationale anchored on the recommendation.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
            correlation_id="corr-create-decision",
        )

    assert isinstance(result, CreateDecisionResult)
    assert _CANONICAL_UUID7.match(result.decision_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert _CANONICAL_UUID7.match(result.manifest_id)
    assert result.target_recommendation_id == recommendation.recommendation_id
    assert (
        result.target_recommendation_revision_id
        == recommendation.recommendation_revision_id
    )
    assert result.outcome == "Accept"
    assert result.rationale == (
        "Decision rationale anchored on the recommendation."
    )
    assert result.deciding_party_id == _PARTY_ID
    assert result.authority_basis_type == "role-grant-id"
    assert result.authority_basis_id == str(_AUTHORITY_BASIS_ID)
    assert result.applicable_scope == _SCOPE
    assert result.omission_entry_ids == ()
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at)

    decision_row = _fetch_decision(engine, decision_id=result.decision_id)
    assert decision_row is not None
    assert decision_row["target_recommendation_id"] == (
        recommendation.recommendation_id
    )
    assert decision_row["target_recommendation_revision_id"] == (
        recommendation.recommendation_revision_id
    )
    assert decision_row["outcome"] == "Accept"
    assert decision_row["rationale"] == (
        "Decision rationale anchored on the recommendation."
    )
    assert decision_row["deciding_party_id"] == _PARTY_ID
    assert decision_row["authority_basis_type"] == "role-grant-id"
    assert decision_row["authority_basis_id"] == str(_AUTHORITY_BASIS_ID)
    assert decision_row["applicable_scope"] == _SCOPE
    assert decision_row["recorded_at"] == _TS_FIXED

    relationships = _fetch_relationships_by_source(
        engine, source_id=result.decision_id
    )
    assert len(relationships) == 1
    rel = relationships[0]
    assert rel["relationship_id"] == result.addresses_relationship_id
    assert rel["relationship_type"] == "Addresses"
    assert rel["source_kind"] == "decision"
    assert rel["source_id"] == result.decision_id
    assert rel["source_revision_id"] is None
    assert rel["target_kind"] == "recommendation_revision"
    assert rel["target_id"] == recommendation.recommendation_id
    assert rel["target_revision_id"] == (
        recommendation.recommendation_revision_id
    )
    assert rel["authoring_party_id"] == _PARTY_ID
    assert rel["recorded_at"] == _TS_FIXED

    manifests = _fetch_manifests(engine, subject_id=result.decision_id)
    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest["manifest_id"] == result.manifest_id
    assert manifest["subject_kind"] == "decision"
    assert manifest["subject_revision_id"] is None
    assert manifest["authoring_party_id"] == _PARTY_ID
    assert manifest["recorded_at"] == _TS_FIXED
    assert manifest["is_complete"] == 1

    audit_rows = _fetch_audit_rows(engine)
    create_decision_rows = [
        row for row in audit_rows if row["action_type"] == "create.decision"
    ]
    assert len(create_decision_rows) == 1
    audit = create_decision_rows[0]
    assert audit["actor_party_id"] == _PARTY_ID
    assert audit["outcome"] == "consequential"
    assert audit["target_id"] == result.decision_id
    assert audit["target_revision_id"] is None
    assert audit["correlation_id"] == "corr-create-decision"
    assert audit["recorded_at"] == _TS_FIXED


def test_create_decision_outcome_reject_persists(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An outcome of ``Reject`` is persisted as-is."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Reject",
            rationale="Rejecting per analysis.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )
    decision_row = _fetch_decision(engine, decision_id=result.decision_id)
    assert decision_row is not None
    assert decision_row["outcome"] == "Reject"


def test_create_decision_outcome_defer_persists(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An outcome of ``Defer`` is persisted as-is."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Defer",
            rationale="Deferring per review.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )
    decision_row = _fetch_decision(engine, decision_id=result.decision_id)
    assert decision_row is not None
    assert decision_row["outcome"] == "Defer"


@pytest.mark.parametrize(
    "basis_type",
    ["role-grant-id", "scope-id", "delegation-chain-id"],
)
def test_create_decision_accepts_each_authority_basis_type(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis_type: str,
) -> None:
    """All three AD-WS-10 authority-basis types are accepted."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    chosen = AuthorityBasisRef(type=basis_type, id=_AUTHORITY_BASIS_ID)
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Authority-basis enumeration coverage.",
            deciding_party_id=_PARTY_ID,
            authority_basis=chosen,
            applicable_scope=_SCOPE,
        )
    decision_row = _fetch_decision(engine, decision_id=result.decision_id)
    assert decision_row is not None
    assert decision_row["authority_basis_type"] == basis_type


def test_create_decision_with_intentional_omissions_keeps_manifest_complete(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An intentional Omission Entry does not flip ``is_complete`` to 0
    (per design §"Persistence Invariants Summary" item 9 — only
    non-intentional categories mark the manifest incomplete)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    omissions = [
        DecisionOmissionEntry(
            excluded_source_id="00000000-0000-7000-8000-0000000000aa",
            excluded_source_revision_id=None,
            category="intentional",
            rationale="Out of scope for this Decision per Reviewer.",
        )
    ]
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Decision with one intentional omission.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
            omissions=omissions,
        )

    manifests = _fetch_manifests(engine, subject_id=result.decision_id)
    assert manifests[0]["is_complete"] == 1
    entries = _fetch_omissions(engine, manifest_id=result.manifest_id)
    assert len(entries) == 1
    assert entries[0]["category"] == "intentional"
    assert entries[0]["resolved_at"] is None
    assert result.omission_entry_ids == (entries[0]["omission_entry_id"],)


@pytest.mark.parametrize(
    "category",
    ["unavailable", "restricted", "stale", "unresolved"],
)
def test_create_decision_with_non_intentional_omission_marks_incomplete(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
    category: str,
) -> None:
    """Any non-intentional Omission Entry marks the manifest incomplete
    (Requirement 10.3 / design §"Persistence Invariants Summary" item 9)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    omissions = [
        DecisionOmissionEntry(
            excluded_source_id="00000000-0000-7000-8000-0000000000bb",
            excluded_source_revision_id=None,
            category=category,  # type: ignore[arg-type]
            rationale=f"Source omitted as {category}.",
        )
    ]
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Decision with non-intentional omission.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
            omissions=omissions,
        )
    manifests = _fetch_manifests(engine, subject_id=result.decision_id)
    assert manifests[0]["is_complete"] == 0


def test_create_decision_rationale_at_maximum_length_is_accepted(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """A rationale of exactly 4,000 characters is accepted (Requirement 6.2)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    rationale = "x" * 4_000
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale=rationale,
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )
    decision_row = _fetch_decision(engine, decision_id=result.decision_id)
    assert decision_row is not None
    assert len(decision_row["rationale"]) == 4_000


# ---------------------------------------------------------------------------
# create_decision — Requirement 6.7 (missing-attribute) rejection.
# ---------------------------------------------------------------------------


def test_create_decision_rejects_missing_target_recommendation_id(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Empty target_recommendation_id is rejected (Requirement 6.7)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id="",
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == (
        "target_recommendation_id_missing"
    )


def test_create_decision_rejects_missing_target_recommendation_revision_id(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Empty target_recommendation_revision_id is rejected."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id="",
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == (
        "target_recommendation_revision_id_missing"
    )


def test_create_decision_rejects_missing_deciding_party_id(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Empty deciding_party_id is rejected (Requirement 6.7)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id="",
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == "deciding_party_id_missing"


def test_create_decision_rejects_missing_applicable_scope(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Empty applicable_scope is rejected (Requirement 6.7)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope="",
            )
    assert exc_info.value.failed_constraint == "applicable_scope_missing"


def test_create_decision_rejects_missing_outcome(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An empty-string outcome is rejected with outcome_invalid."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="",  # type: ignore[arg-type]
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == "outcome_invalid"


def test_create_decision_rejects_missing_rationale(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An empty-string rationale is rejected with rationale_missing."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == "rationale_missing"


def test_create_decision_rejects_rationale_over_4000_chars(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """A rationale longer than 4,000 characters is rejected."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="x" * 4_001,
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == "rationale_too_long"


# ---------------------------------------------------------------------------
# create_decision — AD-WS-11 outcome enumeration.
# ---------------------------------------------------------------------------


def test_create_decision_rejects_supersede_outcome(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """``Supersede`` is excluded from the slice by AD-WS-11."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Supersede",  # type: ignore[arg-type]
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == "outcome_invalid"


# ---------------------------------------------------------------------------
# create_decision — AD-WS-10 authority-basis enumeration.
#
# AuthorityBasisRef enforces the Literal at construction time so the
# rejection happens before ``create_decision`` is even called; the test
# pins that contract here so any future change to the model is caught.
# A separate test asserts the validator's defensive check works when the
# Pydantic layer is bypassed via ``model_construct``.
# ---------------------------------------------------------------------------


def test_authority_basis_ref_rejects_party_id_type() -> None:
    """Pydantic rejects ``authority_basis.type='party-id'`` at construction."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        AuthorityBasisRef(type="party-id", id=_AUTHORITY_BASIS_ID)  # type: ignore[arg-type]


def test_create_decision_rejects_unenumerated_authority_basis_type(
    engine: Engine,
    knowledge_service: KnowledgeService,
) -> None:
    """The validator rejects an unenumerated type even when constructed
    bypassing Pydantic validation (e.g. ``model_construct``)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    bypass = AuthorityBasisRef.model_construct(
        type="party-id", id=_AUTHORITY_BASIS_ID
    )
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=bypass,
                applicable_scope=_SCOPE,
            )
    assert exc_info.value.failed_constraint == "authority_basis_type_invalid"


# ---------------------------------------------------------------------------
# create_decision — non-resolvable target Recommendation Revision.
# ---------------------------------------------------------------------------


def test_create_decision_rejects_unresolvable_recommendation_revision(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An unresolvable target Recommendation Revision is rejected and no
    Decision, Relationship, Manifest, or audit row is persisted."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    audit_before = _fetch_audit_rows(engine)

    with engine.begin() as conn:
        with pytest.raises(RecommendationRevisionNotResolvableError):
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    "00000000-0000-7000-8000-aaaaaaaaaaaa"
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )

    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
    assert decision_count == 0
    audit_after = _fetch_audit_rows(engine)
    # No new audit rows for the failed attempt — audit append happens
    # inside the originating transaction, which never ran.
    assert audit_after == audit_before


# ---------------------------------------------------------------------------
# Requirement 6.5 — duplicate Decision rejection.
# ---------------------------------------------------------------------------


def test_create_decision_rejects_duplicate_target(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """A second Decision targeting the same Recommendation Revision is
    rejected with :class:`DecisionConflictError` carrying the existing
    ``decision_id`` (Requirement 6.5)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        first = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="first decision",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )

    with engine.begin() as conn:
        with pytest.raises(DecisionConflictError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Reject",
                rationale="attempted second decision",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
            )

    assert exc_info.value.existing_decision_id == first.decision_id
    assert exc_info.value.target_recommendation_id == (
        recommendation.recommendation_id
    )
    assert exc_info.value.target_recommendation_revision_id == (
        recommendation.recommendation_revision_id
    )

    with engine.connect() as conn:
        decision_count = conn.execute(
            text("SELECT COUNT(*) FROM Decisions")
        ).scalar_one()
    assert decision_count == 1


def test_unique_constraint_enforces_one_decision_per_revision(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Bypassing the application-level check (by INSERTing directly) still
    triggers the UNIQUE constraint on
    ``Decisions(target_recommendation_id,
    target_recommendation_revision_id)``."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="first decision",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )

    # Direct INSERT skipping the create_decision pre-check should still
    # be rejected by the database.
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO Decisions (
                        decision_id, target_recommendation_id,
                        target_recommendation_revision_id, outcome,
                        rationale, deciding_party_id,
                        authority_basis_type, authority_basis_id,
                        applicable_scope, recorded_at
                    ) VALUES (
                        '00000000-0000-7000-8000-fffffffffffe',
                        :rid, :rrid, 'Reject', 'duplicate',
                        :pid, 'role-grant-id',
                        '00000000-0000-7000-8000-aaaaaaaaaaab',
                        :scope, :ts
                    )
                    """
                ),
                {
                    "rid": recommendation.recommendation_id,
                    "rrid": recommendation.recommendation_revision_id,
                    "pid": _PARTY_ID,
                    "scope": _SCOPE,
                    "ts": _TS_FIXED,
                },
            )


# ---------------------------------------------------------------------------
# Requirement 6.6 — Decision immutability (UPDATE/DELETE trigger).
# ---------------------------------------------------------------------------


def test_decision_row_rejects_update(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """UPDATE on Decisions is rejected by the persistence trigger (AD-WS-4)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="immutable check",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )

    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE Decisions SET outcome = 'Reject' "
                    "WHERE decision_id = :did"
                ),
                {"did": result.decision_id},
            )

    row = _fetch_decision(engine, decision_id=result.decision_id)
    assert row is not None
    assert row["outcome"] == "Accept"


def test_decision_row_rejects_delete(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """DELETE on Decisions is rejected by the persistence trigger (AD-WS-4)."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="immutable check delete",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )

    with pytest.raises(Exception):
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM Decisions WHERE decision_id = :did"),
                {"did": result.decision_id},
            )

    row = _fetch_decision(engine, decision_id=result.decision_id)
    assert row is not None


# ---------------------------------------------------------------------------
# create_decision — Omission Entry validation.
# ---------------------------------------------------------------------------


def test_create_decision_rejects_empty_omission_rationale(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An Omission Entry with an empty rationale is rejected."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    omissions = [
        DecisionOmissionEntry(
            excluded_source_id="00000000-0000-7000-8000-0000000000aa",
            excluded_source_revision_id=None,
            category="intentional",
            rationale="",
        )
    ]
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
                omissions=omissions,
            )
    assert exc_info.value.failed_constraint == "omission_rationale_missing"


def test_create_decision_rejects_invalid_omission_category(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """An Omission Entry with an out-of-enumeration category is rejected."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    omissions = [
        DecisionOmissionEntry(
            excluded_source_id="00000000-0000-7000-8000-0000000000aa",
            excluded_source_revision_id=None,
            category="bogus",  # type: ignore[arg-type]
            rationale="invalid category",
        )
    ]
    with engine.begin() as conn:
        with pytest.raises(DecisionValidationError) as exc_info:
            knowledge_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="ok",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
                omissions=omissions,
            )
    assert exc_info.value.failed_constraint == "omission_category_invalid"


# ---------------------------------------------------------------------------
# Clock fixture override — we want a fresh recorded_at for some tests but
# share the default with most. The default conftest.py fixture pins the
# clock to ``2026-01-01T00:00:00Z``, which is what these tests rely on
# for the recorded_at assertions above.
# ---------------------------------------------------------------------------


def test_recorded_at_uses_clock_with_millisecond_precision(
    engine: Engine,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """The recorded_at value is the Clock's now() in ISO-8601 millisecond form."""
    _, recommendation = _seed_recommendation(engine, knowledge_service)
    with engine.begin() as conn:
        result = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="recorded_at check",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at)
    # Using FixedClock default in conftest.py at 2026-01-01T00:00:00.000Z.
    assert result.recorded_at == _TS_FIXED


# ---------------------------------------------------------------------------
# Suppress unused-warning for datetime/timezone imports used elsewhere.
# ---------------------------------------------------------------------------

_ = (datetime, timezone, _OTHER_PARTY_ID)
