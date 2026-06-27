"""Unit tests for :meth:`walking_slice.evidence.EvidenceRepository.create_region_occurrence`.

These tests pin the contract established in task 5.2, design §"Evidence_Repository"
+ AD-WS-6 (interim byte-offset Region anchoring), and Requirements 3.1, 3.2,
3.3, 3.5, and 16.3:

- A valid span inserts one ``Content_Regions`` row and one
  ``Region_Occurrences`` row whose ``span_byte_length`` and
  ``span_content_digest_sha256`` equal the validated arithmetic and the
  lowercase-hex SHA-256 of ``content_bytes[start:end]`` (Requirement 3.1,
  3.2).
- Requirement 3.5 rejects empty spans (``start == end``), negative
  offsets, ``end > len(content_bytes)``, and ``start > end``; the
  ``InvalidSpanError.failed_constraint`` names the violation.
- Resolving a Region Occurrence on Revision V1 still returns the original
  bytes after Revision V2 is appended (Requirement 3.3 — historical
  citations remain resolvable).
- The consequential audit row carries ``action_type='create.region_occurrence'``
  (Requirement 13.1, AD-WS-5).
- An ``Interim_ADR_Records`` row for AD-WS-6 (``ADR-HT-003`` / Gap G-1)
  exists after the first call and is idempotent across repeated calls
  (Requirement 16.3).
"""

from __future__ import annotations

import hashlib
import re

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.evidence import (
    CreateRegionResult,
    EvidenceRepository,
    InvalidContentError,
    InvalidSpanError,
    RegionNotFoundError,
    RevisionNotFoundError,
)
from walking_slice.identity import IdentityService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers shared with test_evidence.py (kept local so the two test
# modules can evolve independently if their fixture needs diverge).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
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
    """:class:`EvidenceRepository` wired to the per-test engine fixtures."""
    return EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


def _create_document(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    *,
    content: bytes,
):
    """Helper: seed a Party + create a Source Document and return ids."""
    with engine.begin() as conn:
        _seed_party(conn)
        return evidence_repository.create_document(
            conn,
            content_bytes=content,
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )


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


def _fetch_region_occurrence(engine: Engine, *, region_id: str, revision_id: str):
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    """
                    SELECT region_id, document_revision_id, start_offset_bytes,
                           end_offset_bytes, span_byte_length,
                           span_content_digest_sha256, recorded_at
                    FROM Region_Occurrences
                    WHERE region_id = :region_id
                      AND document_revision_id = :revision_id
                    """
                ),
                {"region_id": region_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )


# ---------------------------------------------------------------------------
# Happy-path: valid span creates Content_Regions + Region_Occurrences rows
# ---------------------------------------------------------------------------


def test_valid_span_creates_region_and_occurrence_rows(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """A valid span inserts one ``Content_Regions`` row and one
    ``Region_Occurrences`` row whose digest matches the bytes."""
    content = b"hello evidence repository, the span lives here."
    doc = _create_document(engine, evidence_repository, content=content)

    start, end = 7, 35  # "evidence repository, the spa"
    expected_span = content[start:end]
    expected_digest = hashlib.sha256(expected_span).hexdigest()

    with engine.begin() as conn:
        result = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=start,
            end_offset_bytes=end,
            contributing_party_id=_PARTY_ID,
        )

    assert isinstance(result, CreateRegionResult)
    assert _CANONICAL_UUID7.match(result.region_id), result.region_id
    assert result.revision_id == doc.revision_id
    assert result.start_offset_bytes == start
    assert result.end_offset_bytes == end
    assert result.span_byte_length == end - start
    assert result.span_content_digest_sha256 == expected_digest
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at), result.recorded_at

    with engine.connect() as conn:
        region_row = (
            conn.execute(
                text(
                    "SELECT region_id, parent_resource_id, created_at "
                    "FROM Content_Regions WHERE region_id = :rid"
                ),
                {"rid": result.region_id},
            )
            .mappings()
            .one()
        )
    assert region_row["region_id"] == result.region_id
    assert region_row["parent_resource_id"] == doc.resource_id
    assert region_row["created_at"] == _TS_FIXED

    occurrence_row = _fetch_region_occurrence(
        engine, region_id=result.region_id, revision_id=doc.revision_id
    )
    assert occurrence_row is not None
    assert occurrence_row["region_id"] == result.region_id
    assert occurrence_row["document_revision_id"] == doc.revision_id
    assert occurrence_row["start_offset_bytes"] == start
    assert occurrence_row["end_offset_bytes"] == end
    assert occurrence_row["span_byte_length"] == end - start
    assert occurrence_row["span_content_digest_sha256"] == expected_digest
    assert occurrence_row["recorded_at"] == _TS_FIXED


def test_span_at_revision_boundaries_is_accepted(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """``0 <= start < end <= len(content_bytes)`` allows the boundary values."""
    content = b"boundary"
    doc = _create_document(engine, evidence_repository, content=content)

    with engine.begin() as conn:
        # Whole-content span: start=0, end=len.
        whole = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=len(content),
            contributing_party_id=_PARTY_ID,
        )
    assert whole.span_byte_length == len(content)
    assert whole.span_content_digest_sha256 == hashlib.sha256(content).hexdigest()


def test_region_identifier_registered_in_identifier_registry(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """The new ``region_id`` is registered with ``kind='region'``."""
    content = b"identifiers must register"
    doc = _create_document(engine, evidence_repository, content=content)

    with engine.begin() as conn:
        result = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=11,
            contributing_party_id=_PARTY_ID,
        )

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT identifier, kind, content_digest "
                    "FROM Identifier_Registry WHERE identifier = :rid"
                ),
                {"rid": result.region_id},
            )
            .mappings()
            .one()
        )
    assert row["identifier"] == result.region_id
    assert row["kind"] == "region"
    assert row["content_digest"] == result.span_content_digest_sha256


# ---------------------------------------------------------------------------
# Requirement 3.5 validation
# ---------------------------------------------------------------------------


def test_zero_length_span_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    doc = _create_document(engine, evidence_repository, content=b"nonempty")
    with engine.begin() as conn:
        with pytest.raises(InvalidSpanError) as exc_info:
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=3,
                end_offset_bytes=3,
                contributing_party_id=_PARTY_ID,
            )
    assert (
        exc_info.value.failed_constraint
        == "start_offset_not_less_than_end_offset"
    )


def test_negative_start_offset_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    doc = _create_document(engine, evidence_repository, content=b"nonempty")
    with engine.begin() as conn:
        with pytest.raises(InvalidSpanError) as exc_info:
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=-1,
                end_offset_bytes=4,
                contributing_party_id=_PARTY_ID,
            )
    assert exc_info.value.failed_constraint == "start_offset_negative"


def test_end_greater_than_content_length_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    content = b"short"
    doc = _create_document(engine, evidence_repository, content=content)
    with engine.begin() as conn:
        with pytest.raises(InvalidSpanError) as exc_info:
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=0,
                end_offset_bytes=len(content) + 1,
                contributing_party_id=_PARTY_ID,
            )
    assert (
        exc_info.value.failed_constraint == "end_offset_exceeds_content_length"
    )


def test_start_greater_than_end_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    doc = _create_document(engine, evidence_repository, content=b"abcdefgh")
    with engine.begin() as conn:
        with pytest.raises(InvalidSpanError) as exc_info:
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=6,
                end_offset_bytes=2,
                contributing_party_id=_PARTY_ID,
            )
    assert (
        exc_info.value.failed_constraint
        == "start_offset_not_less_than_end_offset"
    )


def test_unknown_revision_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    doc = _create_document(engine, evidence_repository, content=b"valid")
    unknown_revision_id = "00000000-0000-7000-8000-00000000ffff"
    with engine.begin() as conn:
        with pytest.raises(RevisionNotFoundError):
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=unknown_revision_id,
                start_offset_bytes=0,
                end_offset_bytes=3,
                contributing_party_id=_PARTY_ID,
            )


def test_missing_contributing_party_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    doc = _create_document(engine, evidence_repository, content=b"valid")
    with engine.begin() as conn:
        with pytest.raises(InvalidContentError) as exc_info:
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=0,
                end_offset_bytes=3,
                contributing_party_id="",
            )
    assert (
        exc_info.value.failed_constraint == "contributing_party_id_missing"
    )


def test_invalid_span_rejection_leaves_no_partial_rows(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Per Requirement 3.5 'decline to assign a Region Identity or record a
    Region Occurrence': no Content_Regions or Region_Occurrences row is
    observable after a span violation."""
    doc = _create_document(engine, evidence_repository, content=b"valid")
    with engine.begin() as conn:
        with pytest.raises(InvalidSpanError):
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=2,
                end_offset_bytes=2,
                contributing_party_id=_PARTY_ID,
            )

    with engine.connect() as conn:
        regions = conn.execute(
            text("SELECT COUNT(*) FROM Content_Regions")
        ).scalar_one()
        occurrences = conn.execute(
            text("SELECT COUNT(*) FROM Region_Occurrences")
        ).scalar_one()
    assert regions == 0
    assert occurrences == 0


# ---------------------------------------------------------------------------
# Audit row (Requirement 13.1, AD-WS-5)
# ---------------------------------------------------------------------------


def test_create_region_occurrence_appends_consequential_audit_row(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    content = b"audited region content"
    doc = _create_document(engine, evidence_repository, content=content)
    with engine.begin() as conn:
        result = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=7,
            contributing_party_id=_PARTY_ID,
            correlation_id="corr-region-create",
        )

    audit_rows = _fetch_audit_rows(engine)
    # First row was the create.document_revision; second is ours.
    assert len(audit_rows) == 2
    create_region_row = audit_rows[1]
    assert create_region_row["actor_party_id"] == _PARTY_ID
    assert create_region_row["action_type"] == "create.region_occurrence"
    assert create_region_row["outcome"] == "consequential"
    assert create_region_row["target_id"] == result.region_id
    assert create_region_row["target_revision_id"] == doc.revision_id
    assert create_region_row["correlation_id"] == "corr-region-create"
    assert create_region_row["recorded_at"] == _TS_FIXED


# ---------------------------------------------------------------------------
# Interim_ADR_Records seeding (Requirement 16.3, AD-WS-6, ADR-HT-003)
# ---------------------------------------------------------------------------


def _fetch_interim_adr_for(engine: Engine, *, backlog_adr_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT record_id, motivating_requirement,
                           motivating_criterion, observable_behavior,
                           recorded_at, backlog_adr_id
                    FROM Interim_ADR_Records
                    WHERE backlog_adr_id = :id
                    """
                ),
                {"id": backlog_adr_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def test_interim_adr_record_for_ad_ws_6_exists_after_create(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """The AD-WS-6 / ADR-HT-003 / Gap G-1 row is retrievable after the
    first Region Occurrence creation (Requirement 16.3)."""
    doc = _create_document(engine, evidence_repository, content=b"valid")
    with engine.begin() as conn:
        evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=5,
            contributing_party_id=_PARTY_ID,
        )

    row = _fetch_interim_adr_for(engine, backlog_adr_id="ADR-HT-003")
    assert row is not None
    assert row["backlog_adr_id"] == "ADR-HT-003"
    assert row["motivating_criterion"] == "byte-offset anchoring"
    # The motivating_requirement names both Requirement 3.1/3.2 and Gap G-1
    # (the task's "referencing AD-WS-6 and Gap G-1" requirement).
    assert "3.1" in row["motivating_requirement"]
    assert "3.2" in row["motivating_requirement"]
    assert "G-1" in row["motivating_requirement"]
    assert row["observable_behavior"] != ""


def test_interim_adr_seed_is_idempotent_across_calls(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Multiple Region Occurrence creations leave exactly one AD-WS-6 row."""
    doc = _create_document(engine, evidence_repository, content=b"valid bytes here")
    with engine.begin() as conn:
        evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=5,
            contributing_party_id=_PARTY_ID,
        )
    with engine.begin() as conn:
        evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=6,
            end_offset_bytes=11,
            contributing_party_id=_PARTY_ID,
        )

    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Interim_ADR_Records "
                "WHERE backlog_adr_id = 'ADR-HT-003'"
            )
        ).scalar_one()
    assert count == 1


# ---------------------------------------------------------------------------
# Requirement 3.3 — historical citations survive later Revisions
# ---------------------------------------------------------------------------


def test_region_occurrence_on_v1_remains_resolvable_after_v2_appended(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Per Requirement 3.3 ("when a later Document Revision of the same
    Source Document is recorded, the Evidence_Repository SHALL preserve
    every prior Region Occurrence ... unchanged"), the Region Occurrence
    anchored to V1 must remain byte-equivalent after V2 is appended."""
    v1_content = b"version one content with span here"
    doc = _create_document(engine, evidence_repository, content=v1_content)

    start = v1_content.index(b"with span")
    end = start + len(b"with span")
    expected_span = v1_content[start:end]
    expected_digest = hashlib.sha256(expected_span).hexdigest()

    with engine.begin() as conn:
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=start,
            end_offset_bytes=end,
            contributing_party_id=_PARTY_ID,
        )

    # Append a new Document Revision with completely different bytes.
    with engine.begin() as conn:
        v2 = evidence_repository.append_revision(
            conn,
            resource_id=doc.resource_id,
            content_bytes=b"a completely different version two payload",
            contributing_party_id=_PARTY_ID,
        )

    occurrence_after = _fetch_region_occurrence(
        engine, region_id=region.region_id, revision_id=doc.revision_id
    )
    assert occurrence_after is not None
    assert occurrence_after["start_offset_bytes"] == start
    assert occurrence_after["end_offset_bytes"] == end
    assert occurrence_after["span_byte_length"] == end - start
    assert occurrence_after["span_content_digest_sha256"] == expected_digest

    # The V1 Document Revision content is still resolvable byte-for-byte,
    # so the span bytes themselves remain reconstructable.
    with engine.connect() as conn:
        v1_blob = conn.execute(
            text(
                "SELECT content_bytes FROM Document_Revisions "
                "WHERE revision_id = :rev"
            ),
            {"rev": doc.revision_id},
        ).scalar_one()
    assert bytes(v1_blob)[start:end] == expected_span
    # V2 exists in addition to V1, not in place of it.
    assert v2.revision_id != doc.revision_id


def test_create_region_occurrence_reuses_region_in_new_revision(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Anchoring the same Region in a different Revision via ``region_id``
    keeps the Region Identity byte-equivalent (Requirement 3.3)."""
    v1_content = b"version one content with span here"
    doc = _create_document(engine, evidence_repository, content=v1_content)
    # Compute the byte offsets directly from the content so the test
    # does not drift if the seed string is edited.
    start_v1 = v1_content.index(b"with span")
    end_v1 = start_v1 + len(b"with span")

    with engine.begin() as conn:
        v1_region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=start_v1,
            end_offset_bytes=end_v1,
            contributing_party_id=_PARTY_ID,
        )

    v2_content = b"prefix-with span-suffix-version two bytes"
    with engine.begin() as conn:
        v2 = evidence_repository.append_revision(
            conn,
            resource_id=doc.resource_id,
            content_bytes=v2_content,
            contributing_party_id=_PARTY_ID,
        )

    # The same "with span" substring appears at a different byte offset in V2.
    start_v2 = v2_content.index(b"with span")
    end_v2 = start_v2 + len(b"with span")

    with engine.begin() as conn:
        v2_occurrence = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=v2.revision_id,
            start_offset_bytes=start_v2,
            end_offset_bytes=end_v2,
            contributing_party_id=_PARTY_ID,
            region_id=v1_region.region_id,
        )

    # Same Region Identity, two Occurrences (one per Revision).
    assert v2_occurrence.region_id == v1_region.region_id
    # Same span text, same digest, but anchored to a different Revision.
    assert (
        v2_occurrence.span_content_digest_sha256
        == v1_region.span_content_digest_sha256
    )
    assert v2_occurrence.revision_id == v2.revision_id

    with engine.connect() as conn:
        occurrence_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Region_Occurrences "
                "WHERE region_id = :rid"
            ),
            {"rid": v1_region.region_id},
        ).scalar_one()
        region_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Content_Regions WHERE region_id = :rid"
            ),
            {"rid": v1_region.region_id},
        ).scalar_one()
    assert occurrence_count == 2
    assert region_count == 1  # Region Identity stays unique.


def test_create_region_occurrence_rejects_unknown_region_id(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Passing a ``region_id`` that does not exist surfaces
    :class:`RegionNotFoundError` before any write occurs."""
    doc = _create_document(engine, evidence_repository, content=b"valid")
    unknown_region_id = "00000000-0000-7000-8000-00000000beef"
    with engine.begin() as conn:
        with pytest.raises(RegionNotFoundError):
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=0,
                end_offset_bytes=3,
                contributing_party_id=_PARTY_ID,
                region_id=unknown_region_id,
            )


def test_create_region_occurrence_rejects_region_id_for_other_resource(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """A Region anchored against Source Document A cannot be silently
    re-parented to Source Document B (AD-WS-3)."""
    content_a = b"document A content"
    doc_a = _create_document(engine, evidence_repository, content=content_a)
    # Create a second Source Document; its first Revision becomes the
    # target the test will try to re-anchor the Region against.
    with engine.begin() as conn:
        doc_b = evidence_repository.create_document(
            conn,
            content_bytes=b"document B content",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

    with engine.begin() as conn:
        region_in_a = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc_a.resource_id,
            revision_id=doc_a.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=5,
            contributing_party_id=_PARTY_ID,
        )

    with engine.begin() as conn:
        with pytest.raises(RegionNotFoundError):
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc_b.resource_id,
                revision_id=doc_b.revision_id,
                start_offset_bytes=0,
                end_offset_bytes=5,
                contributing_party_id=_PARTY_ID,
                region_id=region_in_a.region_id,
            )
