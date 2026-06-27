"""Unit tests for Finding finalization branch boundary and edge cases.

This file complements :mod:`tests.unit.test_knowledge_findings` by pinning
additional boundary and edge cases for the Finding finalization branches
called out in task 6.3 of the first walking slice. The "core" branch
matrix (hypothesis with zero supports accepted, non-hypothesis with zero
supports rejected, multiple supports producing one Relationship each, and
contradictions preserving both Finding records) is exercised by
:mod:`tests.unit.test_knowledge_findings`; this module extends that
coverage with:

- A boundary case where ``is_hypothesis=True`` and the Finding still cites
  *multiple* Region Occurrences — every cited Region Occurrence must
  persist as its own ``Supports`` Relationship (Requirements 4.1, 4.5).
- A boundary case where a Finding cites **5** distinct Region
  Occurrences — five ``Supports`` Relationships must persist, and the
  Relationship Identities returned in
  :attr:`CreateFindingResult.supporting_relationship_ids` must be in the
  same positional order as the input ``supporting_region_occurrences``
  iterable, paired with the matching ``(region_id, document_revision_id)``
  on each persisted Relationship row (Requirement 4.5).
- An edge case where **two** ``Contradicts`` Relationships target the same
  Finding from two distinct source Finding Revisions — both Relationship
  rows must persist and the target Finding/Finding_Revision rows must
  remain byte-equivalent to their pre-contradiction state (Requirements
  4.3, 4.4).

Validates: Requirements 4.1, 4.3, 4.4, 4.5
"""

from __future__ import annotations

import re
from typing import Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.evidence import CreateRegionResult, EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateFindingResult,
    KnowledgeService,
    SupportRef,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Local seed helpers.
#
# These mirror the helpers in tests/unit/test_knowledge_findings.py so this
# file can stand alone. Keeping the helpers private to this module avoids
# cross-file fixture coupling — if a future task moves the helpers to a
# shared conftest, both files can import from there without behavioural
# change.
# ---------------------------------------------------------------------------

_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _seed_party(conn, party_id: str = _PARTY_ID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Test Party', :ts)
            """
        ),
        {"pid": party_id, "ts": _TS_FIXED},
    )


@pytest.fixture
def evidence_repository(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> EvidenceRepository:
    """:class:`EvidenceRepository` wired to the per-test fixtures."""
    return EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def knowledge_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """:class:`KnowledgeService` wired to the per-test fixtures."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


def _make_distinct_supports(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    *,
    count: int,
) -> tuple[Sequence[SupportRef], list[CreateRegionResult]]:
    """Seed ``count`` Source Documents, each with one Region Occurrence.

    Each Document gets distinct byte content so its content digest never
    collides with another Document's digest — important for
    Identifier_Registry non-reuse (AD-WS-2). The first call seeds the
    Party row and subsequent calls reuse it.
    """
    refs: list[SupportRef] = []
    results: list[CreateRegionResult] = []
    for index in range(count):
        content = (
            f"branch-test-document-{index}-payload-"
            f"{'y' * (index + 1)}"
        ).encode("utf-8")
        with engine.begin() as conn:
            if index == 0:
                _seed_party(conn)
            doc = evidence_repository.create_document(
                conn,
                content_bytes=content,
                contributing_party_id=_PARTY_ID,
                authority="authoritative",
            )
            region = evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=0,
                end_offset_bytes=min(5, len(content)),
                contributing_party_id=_PARTY_ID,
            )
        refs.append(
            SupportRef(
                region_id=region.region_id,
                document_revision_id=doc.revision_id,
            )
        )
        results.append(region)
    return refs, results


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
                    FROM Relationships
                    WHERE source_id = :source_id
                    ORDER BY relationship_id
                    """
                ),
                {"source_id": source_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_relationships_by_target(
    engine: Engine, *, target_id: str, relationship_type: str
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
                    FROM Relationships
                    WHERE target_id = :target_id
                      AND relationship_type = :relationship_type
                    ORDER BY relationship_id
                    """
                ),
                {
                    "target_id": target_id,
                    "relationship_type": relationship_type,
                },
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_finding(engine: Engine, *, finding_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT finding_id, created_at FROM Findings "
                    "WHERE finding_id = :fid"
                ),
                {"fid": finding_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_finding_revision(
    engine: Engine, *, finding_revision_id: str
) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT finding_revision_id, finding_id, parent_revision_id,
                           statement, is_hypothesis, authoring_party_id,
                           assumptions_json, confidence_note, recorded_at
                    FROM Finding_Revisions
                    WHERE finding_revision_id = :frid
                    """
                ),
                {"frid": finding_revision_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Boundary: hypothesis=True with multiple supports.
#
# Requirement 4.1 says the hypothesis flag only *relaxes the lower bound* on
# the number of Supports Relationships ("OR a hypothesis designation
# explicitly set to true on the Finding by the authoring Analyst"). It does
# not cap how many supports a hypothesis Finding may cite. Requirement 4.5
# says each cited Region Occurrence must produce its own Supports
# Relationship — irrespective of the hypothesis flag.
# ---------------------------------------------------------------------------


def test_hypothesis_finding_with_two_supports_persists_both_relationships(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Boundary: a hypothesis Finding that cites two Region Occurrences
    persists both supports — one Supports Relationship per Occurrence."""
    refs, _ = _make_distinct_supports(engine, evidence_repository, count=2)

    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="Hypothesis with two supporting Region Occurrences.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
            supporting_region_occurrences=refs,
        )

    assert isinstance(result, CreateFindingResult)
    assert result.is_hypothesis is True
    # Per Requirement 4.5, each cited Occurrence produces its own
    # Supports row, even on a hypothesis Finding.
    assert len(result.supporting_relationship_ids) == 2
    assert len(set(result.supporting_relationship_ids)) == 2

    relationships = _fetch_relationships_by_source(
        engine, source_id=result.finding_id
    )
    assert len(relationships) == 2
    assert all(r["relationship_type"] == "Supports" for r in relationships)
    assert all(r["source_kind"] == "finding_revision" for r in relationships)
    assert all(r["target_kind"] == "region_occurrence" for r in relationships)
    assert all(r["source_revision_id"] == result.finding_revision_id for r in relationships)
    assert all(r["authoring_party_id"] == _PARTY_ID for r in relationships)

    # Every cited (region_id, document_revision_id) input pair must
    # appear in the persisted Supports targets exactly once.
    target_pairs = {
        (r["target_id"], r["target_revision_id"]) for r in relationships
    }
    expected_pairs = {(ref.region_id, ref.document_revision_id) for ref in refs}
    assert target_pairs == expected_pairs

    # The Finding Revision's is_hypothesis flag is persisted as 1 even
    # though supports are also present — the flag and the supports
    # branch are independent (Requirement 4.1).
    revision_row = _fetch_finding_revision(
        engine, finding_revision_id=result.finding_revision_id
    )
    assert revision_row is not None
    assert revision_row["is_hypothesis"] == 1


# ---------------------------------------------------------------------------
# Boundary: five supports — count and order both matter.
#
# The 3-support test in tests/unit/test_knowledge_findings.py uses a set
# comparison and so does not pin the ordering contract documented on
# :class:`CreateFindingResult.supporting_relationship_ids` ("in the order
# of the input ``supporting_region_occurrences`` iterable"). This test
# fixes that ordering contract at the upper boundary of the typical
# walking-slice citation count and verifies the row count and per-target
# pairing at the same time.
# ---------------------------------------------------------------------------


def test_finding_with_five_supports_preserves_input_order_in_result(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Boundary: five supports → five Supports Relationships, and the
    returned Relationship Identities follow the input order (Requirement
    4.5; ordering contract on CreateFindingResult)."""
    refs, _ = _make_distinct_supports(engine, evidence_repository, count=5)

    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="Finding citing five distinct Region Occurrences.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=False,
            supporting_region_occurrences=refs,
        )

    # Count: five inputs → five Relationships, all canonical UUIDv7, all
    # distinct.
    assert len(result.supporting_relationship_ids) == 5
    assert len(set(result.supporting_relationship_ids)) == 5
    for rid in result.supporting_relationship_ids:
        assert _CANONICAL_UUID7.match(rid), rid

    # Ordering contract: the i-th Relationship Identity returned must
    # correspond to the i-th SupportRef in the input. We verify this by
    # reading each Relationship row by primary key and asserting its
    # (target_id, target_revision_id) matches the input at the same
    # position.
    with engine.connect() as conn:
        for index, (rid, ref) in enumerate(
            zip(result.supporting_relationship_ids, refs)
        ):
            row = (
                conn.execute(
                    text(
                        """
                        SELECT relationship_type, source_kind, source_id,
                               source_revision_id, target_kind, target_id,
                               target_revision_id
                        FROM Relationships
                        WHERE relationship_id = :rid
                        """
                    ),
                    {"rid": rid},
                )
                .mappings()
                .one()
            )
            assert row["relationship_type"] == "Supports", (
                f"position {index} relationship_type"
            )
            assert row["source_kind"] == "finding_revision"
            assert row["source_id"] == result.finding_id
            assert row["source_revision_id"] == result.finding_revision_id
            assert row["target_kind"] == "region_occurrence"
            assert row["target_id"] == ref.region_id, (
                f"position {index} target_id mismatch — expected "
                f"{ref.region_id!r}, got {row['target_id']!r}"
            )
            assert row["target_revision_id"] == ref.document_revision_id, (
                f"position {index} target_revision_id mismatch"
            )

    # Bulk count verification — the source-indexed query agrees with the
    # per-id verification above.
    relationships = _fetch_relationships_by_source(
        engine, source_id=result.finding_id
    )
    assert len(relationships) == 5
    assert {r["relationship_type"] for r in relationships} == {"Supports"}


# ---------------------------------------------------------------------------
# Edge: two contradictions targeting the same Finding from different sources.
#
# Requirement 4.3's "competing interpretations may coexist" invariant
# (echoed in the user-story commentary on Requirement 4) requires that
# multiple Contradicts Relationships can point at the same target Finding.
# Requirement 4.4 requires the target Finding's records to remain unchanged
# when a contradiction is recorded — this property must hold across
# multiple contradiction events, not just one.
# ---------------------------------------------------------------------------


def test_two_contradictions_against_same_target_persist_both_and_preserve_target(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Edge: two distinct Findings each contradict the same target
    Finding; both Contradicts Relationships persist and the target's
    Findings/Finding_Revisions rows are unchanged by either write."""
    # Create the target Finding plus two distinct source Findings. All
    # three are hypothesis Findings so they can be created without any
    # Region Occurrence setup — the contradiction Relationship is the
    # subject under test, not the supporting evidence.
    with engine.begin() as conn:
        _seed_party(conn)
        target = knowledge_service.create_finding(
            conn,
            statement="Target Finding asserts X.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
    with engine.begin() as conn:
        source_one = knowledge_service.create_finding(
            conn,
            statement="Source one Finding asserts not-X.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
    with engine.begin() as conn:
        source_two = knowledge_service.create_finding(
            conn,
            statement="Source two Finding also asserts not-X, on different grounds.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )

    # Snapshot the target Finding rows before either contradiction. The
    # post-condition is that these snapshots are byte-equivalent to the
    # rows after both contradictions are recorded (Requirement 4.4).
    target_before = _fetch_finding(engine, finding_id=target.finding_id)
    target_revision_before = _fetch_finding_revision(
        engine, finding_revision_id=target.finding_revision_id
    )

    with engine.begin() as conn:
        contradiction_one = knowledge_service.record_contradiction(
            conn,
            source_finding_revision_id=source_one.finding_revision_id,
            target_finding_id=target.finding_id,
            authoring_party_id=_PARTY_ID,
            correlation_id="corr-contradiction-1",
        )
    with engine.begin() as conn:
        contradiction_two = knowledge_service.record_contradiction(
            conn,
            source_finding_revision_id=source_two.finding_revision_id,
            target_finding_id=target.finding_id,
            authoring_party_id=_PARTY_ID,
            correlation_id="corr-contradiction-2",
        )

    # Both Contradicts Relationships are persisted, distinct, and point
    # at the same target. Each carries its source's Revision Identity
    # and the shared target Finding Identity.
    assert contradiction_one.relationship_id != contradiction_two.relationship_id

    contradicts_rows = _fetch_relationships_by_target(
        engine,
        target_id=target.finding_id,
        relationship_type="Contradicts",
    )
    assert len(contradicts_rows) == 2

    by_relationship_id = {row["relationship_id"]: row for row in contradicts_rows}
    assert set(by_relationship_id) == {
        contradiction_one.relationship_id,
        contradiction_two.relationship_id,
    }

    row_one = by_relationship_id[contradiction_one.relationship_id]
    assert row_one["source_kind"] == "finding_revision"
    assert row_one["source_id"] == source_one.finding_id
    assert row_one["source_revision_id"] == source_one.finding_revision_id
    assert row_one["target_kind"] == "finding"
    assert row_one["target_id"] == target.finding_id
    # Requirement 4.4 keys Contradicts on the Finding Resource, not a
    # specific Revision — the target_revision_id is NULL.
    assert row_one["target_revision_id"] is None
    assert row_one["authoring_party_id"] == _PARTY_ID

    row_two = by_relationship_id[contradiction_two.relationship_id]
    assert row_two["source_kind"] == "finding_revision"
    assert row_two["source_id"] == source_two.finding_id
    assert row_two["source_revision_id"] == source_two.finding_revision_id
    assert row_two["target_kind"] == "finding"
    assert row_two["target_id"] == target.finding_id
    assert row_two["target_revision_id"] is None
    assert row_two["authoring_party_id"] == _PARTY_ID

    # Target Finding records are byte-equivalent to their pre-
    # contradiction state — neither contradiction touched the target's
    # Findings or Finding_Revisions row (Requirement 4.4).
    target_after = _fetch_finding(engine, finding_id=target.finding_id)
    target_revision_after = _fetch_finding_revision(
        engine, finding_revision_id=target.finding_revision_id
    )
    assert target_after == target_before
    assert target_revision_after == target_revision_before

    # The target Finding has zero outbound Relationships — every
    # Contradicts row originates from a *different* Finding, so a
    # source-indexed query on the target's identity returns nothing.
    outbound_from_target = _fetch_relationships_by_source(
        engine, source_id=target.finding_id
    )
    assert outbound_from_target == []
