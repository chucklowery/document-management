"""Unit tests for :mod:`walking_slice.knowledge` — Findings + Contradicts.

These tests pin the contract established in task 6.1, design §"Knowledge_Service"
+ §"Findings and Finding_Revisions" + §"Relationships", and
Requirements 4.1, 4.2, 4.3, 4.4, and 4.5:

- A valid non-hypothesis Finding with one cited Region Occurrence inserts
  one ``Findings`` row, one ``Finding_Revisions`` row, one
  ``Relationships`` row of type ``Supports``, and one consequential
  ``Audit_Records`` row inside the caller's transaction (AD-WS-5).
- A Finding with ``is_hypothesis=True`` and zero supports succeeds
  (Requirement 4.1's hypothesis branch).
- A non-hypothesis Finding with zero supports is rejected with
  :class:`FindingValidationError`; no Findings, Finding_Revisions, or
  Relationships row is observable post-rejection (Requirement 4.3).
- A Finding citing a Region Occurrence that does not exist is rejected
  with :class:`FindingNotResolvableError`; no partial state remains.
- A Finding citing 3 distinct Region Occurrences creates 3 separate
  ``Supports`` Relationships rows (Requirement 4.5).
- ``record_contradiction`` creates one ``Contradicts`` Relationship and
  leaves both source and target Finding records byte-equivalent to
  their prior state (Requirement 4.4).
- ``record_contradiction`` rejects unresolved source Revision and
  unresolved target Finding references with
  :class:`FindingNotFoundError`.
- The audit row for ``create.finding`` and ``record.contradiction``
  carries the expected ``actor_party_id``, ``action_type``, ``outcome``,
  ``target_id``, ``target_revision_id``, ``correlation_id``, and
  ``recorded_at`` fields (Requirement 13.1).
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
    CreateRelationshipResult,
    FindingNotFoundError,
    FindingNotResolvableError,
    FindingValidationError,
    KnowledgeService,
    SupportRef,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Every Knowledge_Service test needs at least one Party (for the
# ``authoring_party_id`` FK on Finding_Revisions, Relationships, and
# Audit_Records) and — for non-hypothesis Findings — a real Region
# Occurrence the new Finding can ``Supports``. We build those by going
# through :class:`EvidenceRepository` so the Region Occurrence is anchored
# to a real Document Revision; this keeps the tests aligned with how the
# slice's other tests build up state.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
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
    """Evidence_Repository wired to the per-test fixtures."""
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
    """Knowledge_Service wired to the per-test fixtures."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


def _make_region_occurrence(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    *,
    content: bytes = b"hello, evidence for the finding to cite",
    start: int = 0,
    end: int = 5,
    seed_party: bool = True,
) -> tuple[CreateRegionResult, str]:
    """Seed a Party, create a Source Document, and anchor a Region Occurrence.

    Returns the :class:`CreateRegionResult` and the underlying
    ``document_revision_id`` so the caller can build a :class:`SupportRef`
    in one step. ``seed_party`` is exposed so multiple calls in the same
    test do not double-insert the Parties row.
    """
    with engine.begin() as conn:
        if seed_party:
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
            start_offset_bytes=start,
            end_offset_bytes=end,
            contributing_party_id=_PARTY_ID,
        )
    return region, doc.revision_id


def _make_support_refs(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    *,
    count: int,
) -> tuple[Sequence[SupportRef], list[CreateRegionResult]]:
    """Create ``count`` distinct Region Occurrences and return SupportRefs.

    Each Region Occurrence comes from a distinct Source Document so the
    Region Identities are guaranteed unique; the first call seeds the
    Party row and subsequent calls reuse it.
    """
    refs: list[SupportRef] = []
    results: list[CreateRegionResult] = []
    for index in range(count):
        # Each Source Document gets distinct bytes so its content_digest
        # never collides with another Document's digest — important for
        # Identifier_Registry non-reuse (AD-WS-2).
        content = f"document-{index}-bytes-{'x' * (index + 1)}".encode("utf-8")
        region, revision_id = _make_region_occurrence(
            engine,
            evidence_repository,
            content=content,
            start=0,
            end=min(5, len(content)),
            seed_party=(index == 0),
        )
        refs.append(
            SupportRef(
                region_id=region.region_id,
                document_revision_id=revision_id,
            )
        )
        results.append(region)
    return refs, results


def _fetch_audit_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT actor_party_id, action_type, outcome, target_id, "
                    "target_revision_id, correlation_id, recorded_at "
                    "FROM Audit_Records ORDER BY append_sequence"
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_relationships_by_source(engine: Engine, *, source_id: str) -> list[dict]:
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


def _fetch_finding_revision(engine: Engine, *, finding_revision_id: str) -> dict | None:
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
# create_finding — happy path (non-hypothesis with one support).
# ---------------------------------------------------------------------------


def test_create_finding_with_one_support_inserts_finding_revision_and_relationship(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A non-hypothesis Finding with one cited Region Occurrence persists
    a Findings row, a Finding_Revisions row, and one ``Supports``
    Relationships row, all with the expected attributes."""
    region, revision_id = _make_region_occurrence(engine, evidence_repository)

    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="The interview reveals a process bottleneck.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=False,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=revision_id,
                )
            ],
            correlation_id="corr-create-finding",
        )

    assert isinstance(result, CreateFindingResult)
    assert _CANONICAL_UUID7.match(result.finding_id), result.finding_id
    assert _CANONICAL_UUID7.match(result.finding_revision_id), result.finding_revision_id
    assert result.finding_id != result.finding_revision_id
    assert result.is_hypothesis is False
    assert len(result.supporting_relationship_ids) == 1
    assert _CANONICAL_UUID7.match(result.supporting_relationship_ids[0])
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at), result.recorded_at

    finding_row = _fetch_finding(engine, finding_id=result.finding_id)
    assert finding_row is not None
    assert finding_row["created_at"] == _TS_FIXED

    revision_row = _fetch_finding_revision(
        engine, finding_revision_id=result.finding_revision_id
    )
    assert revision_row is not None
    assert revision_row["finding_id"] == result.finding_id
    assert revision_row["parent_revision_id"] is None
    assert revision_row["statement"] == "The interview reveals a process bottleneck."
    assert revision_row["is_hypothesis"] == 0
    assert revision_row["authoring_party_id"] == _PARTY_ID
    assert revision_row["assumptions_json"] == "[]"
    assert revision_row["confidence_note"] is None
    assert revision_row["recorded_at"] == _TS_FIXED

    relationships = _fetch_relationships_by_source(
        engine, source_id=result.finding_id
    )
    assert len(relationships) == 1
    rel = relationships[0]
    assert rel["relationship_id"] == result.supporting_relationship_ids[0]
    assert rel["relationship_type"] == "Supports"
    assert rel["source_kind"] == "finding_revision"
    assert rel["source_id"] == result.finding_id
    assert rel["source_revision_id"] == result.finding_revision_id
    assert rel["target_kind"] == "region_occurrence"
    assert rel["target_id"] == region.region_id
    assert rel["target_revision_id"] == revision_id
    assert rel["authoring_party_id"] == _PARTY_ID
    assert rel["recorded_at"] == _TS_FIXED


def test_create_finding_registers_resource_and_revision_identifiers(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """The Finding Resource Identity and Finding Revision Identity are
    registered in ``Identifier_Registry`` with the correct kinds."""
    region, revision_id = _make_region_occurrence(engine, evidence_repository)
    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="bottleneck finding",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=revision_id,
                )
            ],
        )

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT identifier, kind FROM Identifier_Registry
                    WHERE identifier IN (:fid, :frev)
                    """
                ),
                {"fid": result.finding_id, "frev": result.finding_revision_id},
            )
            .mappings()
            .all()
        )
    by_id = {row["identifier"]: row["kind"] for row in rows}
    assert by_id == {
        result.finding_id: "resource",
        result.finding_revision_id: "revision",
    }


# ---------------------------------------------------------------------------
# create_finding — hypothesis branch (Requirement 4.1).
# ---------------------------------------------------------------------------


def test_create_finding_hypothesis_with_no_supports_succeeds(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """A Finding with ``is_hypothesis=True`` may cite zero Region
    Occurrences (Requirement 4.1's hypothesis branch)."""
    with engine.begin() as conn:
        _seed_party(conn)
        result = knowledge_service.create_finding(
            conn,
            statement="Hypothesis: process is bottlenecked at intake.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )

    assert result.is_hypothesis is True
    assert result.supporting_relationship_ids == ()
    revision_row = _fetch_finding_revision(
        engine, finding_revision_id=result.finding_revision_id
    )
    assert revision_row is not None
    assert revision_row["is_hypothesis"] == 1

    # No Supports Relationships should exist for this Finding.
    relationships = _fetch_relationships_by_source(
        engine, source_id=result.finding_id
    )
    assert relationships == []


def test_create_finding_hypothesis_can_still_have_supports(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A hypothesis Finding *may* cite Region Occurrences — the hypothesis
    flag only relaxes the lower bound, it does not forbid supports."""
    region, revision_id = _make_region_occurrence(engine, evidence_repository)
    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="Hypothesis with one support.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=revision_id,
                )
            ],
        )
    assert result.is_hypothesis is True
    assert len(result.supporting_relationship_ids) == 1


# ---------------------------------------------------------------------------
# create_finding — Requirement 4.3 rejection.
# ---------------------------------------------------------------------------


def test_create_finding_non_hypothesis_without_supports_is_rejected(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Per Requirement 4.3, a non-hypothesis Finding with zero supports
    is rejected and no Findings, Finding_Revisions, or Relationships row
    is written."""
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(FindingValidationError) as exc_info:
            knowledge_service.create_finding(
                conn,
                statement="Some finding without evidence.",
                authoring_party_id=_PARTY_ID,
                is_hypothesis=False,
            )
    assert (
        exc_info.value.failed_constraint
        == "supports_required_for_non_hypothesis"
    )

    with engine.connect() as conn:
        findings_count = conn.execute(
            text("SELECT COUNT(*) FROM Findings")
        ).scalar_one()
        revisions_count = conn.execute(
            text("SELECT COUNT(*) FROM Finding_Revisions")
        ).scalar_one()
        relationships_count = conn.execute(
            text("SELECT COUNT(*) FROM Relationships")
        ).scalar_one()
    assert findings_count == 0
    assert revisions_count == 0
    assert relationships_count == 0


def test_create_finding_rejects_empty_statement(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 4.1 implies a Finding carries a statement; the empty
    string is rejected with the ``statement_empty`` constraint."""
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(FindingValidationError) as exc_info:
            knowledge_service.create_finding(
                conn,
                statement="",
                authoring_party_id=_PARTY_ID,
                is_hypothesis=True,  # so the zero-supports rule isn't the trigger
            )
    assert exc_info.value.failed_constraint == "statement_empty"


def test_create_finding_rejects_missing_authoring_party(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 4.2 demands every Relationship records the authoring
    Party; the same validator applies to Finding creation."""
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(FindingValidationError) as exc_info:
            knowledge_service.create_finding(
                conn,
                statement="finding text",
                authoring_party_id="",
                is_hypothesis=True,
            )
    assert exc_info.value.failed_constraint == "authoring_party_id_missing"


# ---------------------------------------------------------------------------
# create_finding — unresolved Region Occurrence rejection.
# ---------------------------------------------------------------------------


def test_create_finding_rejects_unresolved_region_occurrence(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """A cited Region Occurrence that does not exist in
    ``Region_Occurrences`` raises :class:`FindingNotResolvableError` and
    leaves no partial state."""
    bogus_region = "00000000-0000-7000-8000-00000000beef"
    bogus_revision = "00000000-0000-7000-8000-00000000cafe"

    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(FindingNotResolvableError) as exc_info:
            knowledge_service.create_finding(
                conn,
                statement="finding citing missing occurrence",
                authoring_party_id=_PARTY_ID,
                supporting_region_occurrences=[
                    SupportRef(
                        region_id=bogus_region,
                        document_revision_id=bogus_revision,
                    )
                ],
            )
    assert exc_info.value.region_id == bogus_region
    assert exc_info.value.document_revision_id == bogus_revision

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM Findings")).scalar_one() == 0
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Finding_Revisions")).scalar_one()
            == 0
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Relationships")).scalar_one() == 0
        )


def test_create_finding_rejects_when_any_one_support_is_unresolved(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """When the citation list contains one resolvable and one
    unresolvable Region Occurrence, the entire write is rejected — no
    partial ``Supports`` Relationships are inserted."""
    region, revision_id = _make_region_occurrence(engine, evidence_repository)
    bogus_region = "00000000-0000-7000-8000-00000000fade"

    with engine.begin() as conn:
        with pytest.raises(FindingNotResolvableError):
            knowledge_service.create_finding(
                conn,
                statement="finding with one good and one bad support",
                authoring_party_id=_PARTY_ID,
                supporting_region_occurrences=[
                    SupportRef(
                        region_id=region.region_id,
                        document_revision_id=revision_id,
                    ),
                    SupportRef(
                        region_id=bogus_region,
                        document_revision_id=revision_id,
                    ),
                ],
            )

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM Findings")).scalar_one() == 0
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Relationships")).scalar_one() == 0
        )


# ---------------------------------------------------------------------------
# create_finding — Requirement 4.5: one Relationship per cited Occurrence.
# ---------------------------------------------------------------------------


def test_create_finding_with_three_supports_creates_three_supports_relationships(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Requirement 4.5: 'a separate Supports Relationship for each cited
    Content Region Occurrence'."""
    refs, _ = _make_support_refs(engine, evidence_repository, count=3)

    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="finding citing three distinct occurrences",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=refs,
        )

    assert len(result.supporting_relationship_ids) == 3
    assert len(set(result.supporting_relationship_ids)) == 3

    relationships = _fetch_relationships_by_source(
        engine, source_id=result.finding_id
    )
    assert len(relationships) == 3
    assert all(r["relationship_type"] == "Supports" for r in relationships)

    # Every targeted Region Identity from the inputs appears as a
    # Relationship target, paired with the correct document_revision_id.
    target_pairs = {
        (r["target_id"], r["target_revision_id"]) for r in relationships
    }
    expected_pairs = {(ref.region_id, ref.document_revision_id) for ref in refs}
    assert target_pairs == expected_pairs


# ---------------------------------------------------------------------------
# record_contradiction — happy path + preservation of source/target.
# ---------------------------------------------------------------------------


def _make_two_findings(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> tuple[CreateFindingResult, CreateFindingResult]:
    """Helper: create two hypothesis Findings (no supports needed) so the
    contradiction test can wire them together."""
    with engine.begin() as conn:
        _seed_party(conn)
        finding_a = knowledge_service.create_finding(
            conn,
            statement="Finding A asserts X.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
    with engine.begin() as conn:
        finding_b = knowledge_service.create_finding(
            conn,
            statement="Finding B asserts not-X.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
    return finding_a, finding_b


def test_record_contradiction_creates_contradicts_relationship(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Requirement 4.4: record a ``Contradicts`` Relationship between two
    Findings. The Relationship carries source Finding Identity, source
    Revision Identity, target Finding Identity, authoring Party, and
    recorded time."""
    finding_a, finding_b = _make_two_findings(
        engine, evidence_repository, knowledge_service
    )

    with engine.begin() as conn:
        result = knowledge_service.record_contradiction(
            conn,
            source_finding_revision_id=finding_b.finding_revision_id,
            target_finding_id=finding_a.finding_id,
            authoring_party_id=_PARTY_ID,
            correlation_id="corr-contradiction",
        )

    assert isinstance(result, CreateRelationshipResult)
    assert result.relationship_type == "Contradicts"
    assert _CANONICAL_UUID7.match(result.relationship_id)
    assert result.source_kind == "finding_revision"
    assert result.source_id == finding_b.finding_id
    assert result.source_revision_id == finding_b.finding_revision_id
    assert result.target_kind == "finding"
    assert result.target_id == finding_a.finding_id
    assert result.target_revision_id is None
    assert result.authoring_party_id == _PARTY_ID
    assert result.recorded_at == _TS_FIXED

    relationships = _fetch_relationships_by_source(
        engine, source_id=finding_b.finding_id
    )
    # Only one row exists for the source — the Contradicts row.
    assert len(relationships) == 1
    rel = relationships[0]
    assert rel["relationship_id"] == result.relationship_id
    assert rel["relationship_type"] == "Contradicts"
    assert rel["source_kind"] == "finding_revision"
    assert rel["source_id"] == finding_b.finding_id
    assert rel["source_revision_id"] == finding_b.finding_revision_id
    assert rel["target_kind"] == "finding"
    assert rel["target_id"] == finding_a.finding_id
    assert rel["target_revision_id"] is None
    assert rel["authoring_party_id"] == _PARTY_ID
    assert rel["recorded_at"] == _TS_FIXED


def test_record_contradiction_leaves_both_findings_unchanged(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Requirement 4.4: both Finding records remain unchanged after the
    contradiction is recorded."""
    finding_a, finding_b = _make_two_findings(
        engine, evidence_repository, knowledge_service
    )

    a_before = _fetch_finding(engine, finding_id=finding_a.finding_id)
    b_before = _fetch_finding(engine, finding_id=finding_b.finding_id)
    a_rev_before = _fetch_finding_revision(
        engine, finding_revision_id=finding_a.finding_revision_id
    )
    b_rev_before = _fetch_finding_revision(
        engine, finding_revision_id=finding_b.finding_revision_id
    )

    with engine.begin() as conn:
        knowledge_service.record_contradiction(
            conn,
            source_finding_revision_id=finding_b.finding_revision_id,
            target_finding_id=finding_a.finding_id,
            authoring_party_id=_PARTY_ID,
        )

    a_after = _fetch_finding(engine, finding_id=finding_a.finding_id)
    b_after = _fetch_finding(engine, finding_id=finding_b.finding_id)
    a_rev_after = _fetch_finding_revision(
        engine, finding_revision_id=finding_a.finding_revision_id
    )
    b_rev_after = _fetch_finding_revision(
        engine, finding_revision_id=finding_b.finding_revision_id
    )

    assert a_after == a_before
    assert b_after == b_before
    assert a_rev_after == a_rev_before
    assert b_rev_after == b_rev_before


def test_record_contradiction_rejects_unresolved_source_revision(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """If the source Finding Revision does not exist, the contradiction
    is rejected and no Relationships row is written."""
    finding_a, _ = _make_two_findings(
        engine, evidence_repository, knowledge_service
    )
    bogus_revision = "00000000-0000-7000-8000-00000000aaaa"

    with engine.begin() as conn:
        with pytest.raises(FindingNotFoundError) as exc_info:
            knowledge_service.record_contradiction(
                conn,
                source_finding_revision_id=bogus_revision,
                target_finding_id=finding_a.finding_id,
                authoring_party_id=_PARTY_ID,
            )
    assert exc_info.value.role == "source"
    assert exc_info.value.identifier == bogus_revision

    with engine.connect() as conn:
        relationship_count = conn.execute(
            text("SELECT COUNT(*) FROM Relationships")
        ).scalar_one()
    assert relationship_count == 0


def test_record_contradiction_rejects_unresolved_target_finding(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """If the target Finding does not exist, the contradiction is rejected
    and no Relationships row is written."""
    _, finding_b = _make_two_findings(
        engine, evidence_repository, knowledge_service
    )
    bogus_finding = "00000000-0000-7000-8000-00000000bbbb"

    with engine.begin() as conn:
        with pytest.raises(FindingNotFoundError) as exc_info:
            knowledge_service.record_contradiction(
                conn,
                source_finding_revision_id=finding_b.finding_revision_id,
                target_finding_id=bogus_finding,
                authoring_party_id=_PARTY_ID,
            )
    assert exc_info.value.role == "target"
    assert exc_info.value.identifier == bogus_finding

    with engine.connect() as conn:
        relationship_count = conn.execute(
            text("SELECT COUNT(*) FROM Relationships")
        ).scalar_one()
    assert relationship_count == 0


# ---------------------------------------------------------------------------
# Audit row coverage (Requirement 13.1).
# ---------------------------------------------------------------------------


def test_create_finding_appends_audit_row_with_create_finding_action(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Every consequential create.finding write leaves an audit row with
    the matching action_type, target identifiers, and correlation_id."""
    region, revision_id = _make_region_occurrence(engine, evidence_repository)
    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="audited finding",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=revision_id,
                )
            ],
            correlation_id="corr-create-finding-audit",
        )

    audit_rows = _fetch_audit_rows(engine)
    # The pre-test setup created two audit rows (create.document_revision
    # and create.region_occurrence); the create.finding row is the third.
    finding_audit_rows = [
        row for row in audit_rows if row["action_type"] == "create.finding"
    ]
    assert len(finding_audit_rows) == 1
    audit = finding_audit_rows[0]
    assert audit["actor_party_id"] == _PARTY_ID
    assert audit["action_type"] == "create.finding"
    assert audit["outcome"] == "consequential"
    assert audit["target_id"] == result.finding_id
    assert audit["target_revision_id"] == result.finding_revision_id
    assert audit["correlation_id"] == "corr-create-finding-audit"
    assert audit["recorded_at"] == _TS_FIXED


def test_record_contradiction_appends_audit_row_with_record_contradiction_action(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Every consequential record.contradiction write leaves an audit row
    with the matching action_type and target Finding identifier."""
    finding_a, finding_b = _make_two_findings(
        engine, evidence_repository, knowledge_service
    )

    with engine.begin() as conn:
        result = knowledge_service.record_contradiction(
            conn,
            source_finding_revision_id=finding_b.finding_revision_id,
            target_finding_id=finding_a.finding_id,
            authoring_party_id=_PARTY_ID,
            correlation_id="corr-contradiction-audit",
        )

    audit_rows = _fetch_audit_rows(engine)
    contradiction_rows = [
        row for row in audit_rows if row["action_type"] == "record.contradiction"
    ]
    assert len(contradiction_rows) == 1
    audit = contradiction_rows[0]
    assert audit["actor_party_id"] == _PARTY_ID
    assert audit["action_type"] == "record.contradiction"
    assert audit["outcome"] == "consequential"
    assert audit["target_id"] == finding_a.finding_id
    assert audit["target_revision_id"] is None
    assert audit["correlation_id"] == "corr-contradiction-audit"
    assert audit["recorded_at"] == _TS_FIXED
    # Sanity check — the returned relationship_id is canonical.
    assert _CANONICAL_UUID7.match(result.relationship_id)
