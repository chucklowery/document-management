"""Spec-coverage suite for Recommendation creation rules.

This module is the explicit "spec coverage" companion to task 7.3:

    Cover 1, 25, and 50 Derived From references (accepted); zero
    references (rejected); unresolved Finding reference (rejected);
    rationale and assumptions length boundaries.
    _Requirements: 5.1, 5.6_

The exhaustive contract pinning for ``KnowledgeService.create_recommendation``
already lives in :mod:`tests.unit.test_knowledge_recommendations` (task 7.1)
and covers every Requirement-5 acceptance criterion. This file does **not**
duplicate that work; it re-exercises the specific scenarios named in
task 7.3 against the same wired service so the acceptance criteria called
out by Requirements 5.1 and 5.6 (plus the rationale/assumptions length
boundaries flagged in the task body) each have a named, requirement-tagged
test that fails loudly if the underlying contract ever drifts.

Each test below is annotated **Validates: Requirements X.Y** so the
constitutional traceability ledger (Requirement 14) can crosswalk this
file to Requirement-5 acceptance criteria without re-reading the prose.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateFindingResult,
    KnowledgeService,
    RecommendationNotResolvableError,
    RecommendationValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Local constants and helpers (kept minimal — see test_knowledge_recommendations
# for the full suite of helpers).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS_FIXED = "2026-01-01T00:00:00.000Z"


def _seed_party(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Analyst', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _TS_FIXED},
    )


@pytest.fixture
def knowledge_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """Back-compat Knowledge_Service without authorization wiring.

    These spec-coverage tests target the input-validation and
    Finding-resolution paths of ``create_recommendation`` — i.e.
    Requirements 5.1 and 5.6 — and deliberately do not require an
    AuthorizationService. The authorization-side acceptance criterion
    (Requirement 5.7) is covered in
    :mod:`tests.unit.test_knowledge_recommendations`.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


def _create_hypothesis_finding(
    engine: Engine,
    knowledge_service: KnowledgeService,
    *,
    statement: str,
    seed_party: bool = False,
) -> CreateFindingResult:
    """Insert a hypothesis Finding usable as a Derived From target."""
    with engine.begin() as conn:
        if seed_party:
            _seed_party(conn)
        return knowledge_service.create_finding(
            conn,
            statement=statement,
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )


def _create_findings(
    engine: Engine, knowledge_service: KnowledgeService, *, count: int
) -> list[CreateFindingResult]:
    """Seed the Party once and create ``count`` distinct hypothesis Findings."""
    findings: list[CreateFindingResult] = []
    for index in range(count):
        findings.append(
            _create_hypothesis_finding(
                engine,
                knowledge_service,
                statement=f"derived-from finding {index}",
                seed_party=(index == 0),
            )
        )
    return findings


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


# ---------------------------------------------------------------------------
# Requirement 5.1 — between 1 and 50 ``Derived From`` Relationships.
# ---------------------------------------------------------------------------


def test_one_derived_from_reference_is_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1

    A Recommendation with exactly 1 resolvable Derived From Finding
    is accepted and produces exactly 1 ``Derived From`` Relationship row.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="lower-bound Derived From source",
        seed_party=True,
    )

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
        )

    assert len(result.derived_from_relationship_ids) == 1
    assert _count(engine, "Recommendations") == 1
    assert _count(engine, "Recommendation_Revisions") == 1
    with engine.connect() as conn:
        rel_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Derived From'"
            )
        ).scalar_one()
    assert rel_count == 1


def test_twenty_five_derived_from_references_are_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1

    A Recommendation with 25 distinct Derived From Findings — the
    interior of the 1..50 range — is accepted and produces exactly
    25 ``Derived From`` Relationship rows.
    """
    findings = _create_findings(engine, knowledge_service, count=25)
    finding_ids = [f.finding_id for f in findings]

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=finding_ids,
        )

    assert len(result.derived_from_relationship_ids) == 25
    with engine.connect() as conn:
        rel_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Derived From' "
                "AND source_id = :rid"
            ),
            {"rid": result.recommendation_id},
        ).scalar_one()
    assert rel_count == 25


def test_fifty_derived_from_references_are_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1

    The upper bound of Requirement 5.1 is 50 — exactly 50 distinct
    Derived From Findings is accepted and produces 50 ``Derived From``
    Relationship rows.
    """
    findings = _create_findings(engine, knowledge_service, count=50)
    finding_ids = [f.finding_id for f in findings]

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=finding_ids,
        )

    assert len(result.derived_from_relationship_ids) == 50
    with engine.connect() as conn:
        rel_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Derived From' "
                "AND source_id = :rid"
            ),
            {"rid": result.recommendation_id},
        ).scalar_one()
    assert rel_count == 50


# ---------------------------------------------------------------------------
# Requirement 5.6 — zero references and unresolved references are rejected.
# ---------------------------------------------------------------------------


def test_zero_derived_from_references_are_rejected(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.6

    A Recommendation submitted with zero ``Derived From`` references
    is rejected. No Recommendations, Recommendation_Revisions, or
    Relationships rows are persisted.
    """
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[],
            )
    assert exc_info.value.failed_constraint == "derived_from_too_few"

    assert _count(engine, "Recommendations") == 0
    assert _count(engine, "Recommendation_Revisions") == 0
    assert _count(engine, "Relationships") == 0


def test_unresolved_derived_from_reference_is_rejected(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.6

    A Derived From reference that does not resolve to an existing
    Finding row is rejected via :class:`RecommendationNotResolvableError`,
    and no Recommendation, Revision, or Relationship row is persisted.
    """
    bogus_finding = "00000000-0000-7000-8000-00000000beef"

    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(RecommendationNotResolvableError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[bogus_finding],
            )
    assert exc_info.value.finding_id == bogus_finding
    assert exc_info.value.failed_constraint == "invalid_derived_from"

    assert _count(engine, "Recommendations") == 0
    assert _count(engine, "Recommendation_Revisions") == 0
    assert _count(engine, "Relationships") == 0


def test_partially_unresolved_derived_from_list_is_rejected_atomically(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.6

    When a Derived From list mixes resolvable and unresolvable
    Findings, the entire write is rejected — no partial Recommendation
    or Relationships rows leak through (AD-WS-5 transaction atomicity
    in service of Requirement 5.6's "decline to create any Resource
    or Revision" clause).
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="resolvable finding alongside an unresolvable identity",
        seed_party=True,
    )
    bogus_finding = "00000000-0000-7000-8000-00000000fade"

    with engine.begin() as conn:
        with pytest.raises(RecommendationNotResolvableError):
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id, bogus_finding],
            )

    assert _count(engine, "Recommendations") == 0
    assert _count(engine, "Recommendation_Revisions") == 0
    # Hypothesis Findings have no Supports rows, so the only way a
    # Relationships row could appear here is from a partially applied
    # Recommendation write — which Requirement 5.6 forbids.
    assert _count(engine, "Relationships") == 0


# ---------------------------------------------------------------------------
# Rationale and assumptions length boundaries.
#
# Task 7.3 enumerates these boundaries under its _Requirements: 5.1, 5.6_
# tag because they belong to the same "creation rules" surface. The
# underlying acceptance criteria are 5.3 (rationale) and 5.4 (assumptions),
# both of which are co-validated on the same code path as 5.1 and 5.6 —
# Requirement 5.6 mandates that any rejected creation persists no Resource
# or Revision, which we re-verify here for each boundary failure.
# ---------------------------------------------------------------------------


def test_rationale_at_max_length_is_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1, 5.6

    Exactly 10,000 characters of rationale is accepted and persisted
    verbatim on the Recommendation_Revisions row, alongside a successful
    creation (Requirement 5.1) — i.e. the boundary case does not trip
    the rejection path Requirement 5.6 reserves for invalid input.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="rationale boundary - max length",
        seed_party=True,
    )
    rationale = "x" * 10_000

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale=rationale,
        )

    with engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT rationale FROM Recommendation_Revisions "
                "WHERE recommendation_revision_id = :rrid"
            ),
            {"rrid": result.recommendation_revision_id},
        ).scalar_one()
    assert stored == rationale
    assert len(stored) == 10_000


def test_empty_rationale_is_rejected(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1, 5.6

    An explicit empty rationale (lower bound − 1) is rejected with
    ``failed_constraint='rationale_empty'``. Requirement 5.6 demands
    no Resource or Revision rows be persisted on rejection — we
    re-verify that invariant here against the rationale boundary.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="rationale boundary - empty",
        seed_party=True,
    )

    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="",
            )
    assert exc_info.value.failed_constraint == "rationale_empty"

    assert _count(engine, "Recommendations") == 0
    assert _count(engine, "Recommendation_Revisions") == 0


def test_rationale_over_max_length_is_rejected(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1, 5.6

    Rationale of 10,001 characters (upper bound + 1) is rejected with
    ``failed_constraint='rationale_too_long'`` and persists nothing.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="rationale boundary - over max",
        seed_party=True,
    )

    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="x" * 10_001,
            )
    assert exc_info.value.failed_constraint == "rationale_too_long"

    assert _count(engine, "Recommendations") == 0
    assert _count(engine, "Recommendation_Revisions") == 0


def test_zero_assumptions_are_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1, 5.6

    Zero assumptions is the explicit lower bound — the Recommendation
    Revision persists ``assumptions_json='[]'`` and is accepted.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="assumptions boundary - zero",
        seed_party=True,
    )

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            assumptions=[],
        )

    assert result.assumptions == ()
    with engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT assumptions_json FROM Recommendation_Revisions "
                "WHERE recommendation_revision_id = :rrid"
            ),
            {"rrid": result.recommendation_revision_id},
        ).scalar_one()
    assert stored == "[]"


def test_fifty_assumptions_are_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1, 5.6

    Exactly 50 assumptions is the explicit upper bound and is
    accepted, with each entry preserved in array order.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="assumptions boundary - fifty",
        seed_party=True,
    )
    assumptions = [f"assumption {i}" for i in range(50)]

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            assumptions=assumptions,
        )

    assert len(result.assumptions) == 50
    with engine.connect() as conn:
        stored = conn.execute(
            text(
                "SELECT assumptions_json FROM Recommendation_Revisions "
                "WHERE recommendation_revision_id = :rrid"
            ),
            {"rrid": result.recommendation_revision_id},
        ).scalar_one()
    assert json.loads(stored) == assumptions


def test_fifty_one_assumptions_are_rejected(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Validates: Requirements 5.1, 5.6

    51 assumptions (upper bound + 1) is rejected with
    ``failed_constraint='assumptions_too_many'`` and persists nothing.
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="assumptions boundary - over max",
        seed_party=True,
    )
    assumptions = [f"assumption {i}" for i in range(51)]

    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                assumptions=assumptions,
            )
    assert exc_info.value.failed_constraint == "assumptions_too_many"

    assert _count(engine, "Recommendations") == 0
    assert _count(engine, "Recommendation_Revisions") == 0
