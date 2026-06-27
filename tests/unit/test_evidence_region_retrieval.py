"""Unit tests for :meth:`walking_slice.evidence.EvidenceRepository.get_region_occurrence`
and :meth:`walking_slice.evidence.EvidenceRepository.resolve_region_text`.

These tests pin the contract established in task 5.5, design §"Evidence_Repository",
and Requirements 3.4, 3.5, and 3.6:

- Requirement 3.4 — "WHEN an authorized user resolves a Content Region
  reference that corresponds to a recorded Region Occurrence, THE
  Provenance_Navigator SHALL return the exact Document Revision Identity,
  Region Identity, Region Occurrence, and a bounded text span byte-equivalent
  to the span originally recorded for that Region Occurrence."
- Requirement 3.6 — "IF an authorized user resolves a Content Region
  reference that does not correspond to any recorded Region Occurrence,
  THEN THE Provenance_Navigator SHALL decline to return a bounded text
  span and return an error indication identifying the unresolvable
  reference."
- Requirement 3.5 — validation matrix recap for empty, out-of-range, and
  ``start >= end`` spans (covered more exhaustively in
  ``test_evidence_regions.py`` from task 5.2; here we keep a focused
  validation matrix so a regression in the rejection branch can never
  hide behind the retrieval tests).

The :class:`EvidenceRepository` methods exercised here are the
persistence-layer half of the Requirement 3.4 / 3.6 contract; the
``Provenance_Navigator`` (tasks 12.3 / 12.4) wraps them with authority
filtering and the AD-WS-9 Disclosure Policy. Unit testing the read path
in isolation lets us verify byte-equivalence and the unresolvable-error
shape without standing up the Authorization_Service.
"""

from __future__ import annotations

import hashlib
import re

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.evidence import (
    EvidenceRepository,
    InvalidSpanError,
    RegionOccurrence,
    RegionOccurrenceNotFoundError,
    ResolvedSpan,
)
from walking_slice.identity import IdentityService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers shared with test_evidence_regions.py. The seed helpers are
# kept local so the two test modules can evolve independently if their
# fixture needs diverge.
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


def _create_region(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    *,
    resource_id: str,
    revision_id: str,
    start: int,
    end: int,
):
    """Helper: create a Region Occurrence on an existing Revision."""
    with engine.begin() as conn:
        return evidence_repository.create_region_occurrence(
            conn,
            resource_id=resource_id,
            revision_id=revision_id,
            start_offset_bytes=start,
            end_offset_bytes=end,
            contributing_party_id=_PARTY_ID,
        )


# ---------------------------------------------------------------------------
# Requirement 3.4 — Successful retrieval returns byte-equivalent span and
# verified digest.
# ---------------------------------------------------------------------------


def test_get_region_occurrence_returns_persisted_row(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """``get_region_occurrence`` returns the persisted row's exact fields.

    Validates: Requirement 3.4 (the return value carries the exact
    Document Revision Identity, Region Identity, and Region Occurrence
    fields previously recorded).
    """
    content = b"the slice supports omission-aware provenance navigation."
    doc = _create_document(engine, evidence_repository, content=content)
    start, end = 4, 9  # "slice"
    expected_digest = hashlib.sha256(content[start:end]).hexdigest()
    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=start,
        end=end,
    )

    with engine.connect() as conn:
        occurrence = evidence_repository.get_region_occurrence(
            conn, region_id=region.region_id, revision_id=doc.revision_id
        )

    assert isinstance(occurrence, RegionOccurrence)
    assert occurrence.region_id == region.region_id
    assert occurrence.document_revision_id == doc.revision_id
    assert occurrence.start_offset_bytes == start
    assert occurrence.end_offset_bytes == end
    assert occurrence.span_byte_length == end - start
    assert occurrence.span_content_digest_sha256 == expected_digest
    assert occurrence.recorded_at == _TS_FIXED
    assert _CANONICAL_UUID7.match(occurrence.region_id), occurrence.region_id


def test_resolve_region_text_returns_byte_equivalent_span(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """``resolve_region_text`` returns bytes byte-equivalent to
    ``content_bytes[start:end]`` and verifies the digest matches.

    Validates: Requirement 3.4 (byte-equivalent bounded text span).
    """
    content = b"hello evidence repository, the span lives here."
    doc = _create_document(engine, evidence_repository, content=content)
    start, end = 7, 35  # "evidence repository, the spa"
    expected_span = content[start:end]
    expected_digest = hashlib.sha256(expected_span).hexdigest()
    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=start,
        end=end,
    )

    with engine.connect() as conn:
        resolved = evidence_repository.resolve_region_text(
            conn, region_id=region.region_id, revision_id=doc.revision_id
        )

    assert isinstance(resolved, ResolvedSpan)
    assert resolved.region_id == region.region_id
    assert resolved.revision_id == doc.revision_id
    assert resolved.start_offset_bytes == start
    assert resolved.end_offset_bytes == end
    assert resolved.span_byte_length == end - start
    assert resolved.span_content_digest_sha256 == expected_digest
    # Byte-equivalent — the central assertion of Requirement 3.4.
    assert resolved.bounded_text == expected_span
    # Digest verification is part of the contract: the returned digest
    # must equal a recomputation over ``bounded_text``.
    assert (
        hashlib.sha256(resolved.bounded_text).hexdigest()
        == resolved.span_content_digest_sha256
    )


def test_resolve_region_text_handles_whole_content_span(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Boundary span ``[0, len(content))`` is byte-equivalent and verified."""
    content = b"all bytes belong to the span"
    doc = _create_document(engine, evidence_repository, content=content)
    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=0,
        end=len(content),
    )

    with engine.connect() as conn:
        resolved = evidence_repository.resolve_region_text(
            conn, region_id=region.region_id, revision_id=doc.revision_id
        )

    assert resolved.bounded_text == content
    assert resolved.span_byte_length == len(content)


def test_resolve_region_text_idempotent_across_repeated_calls(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Repeated calls return byte-equivalent results — Property 8 in miniature.

    Even though Property 8 (Provenance traversal idempotence) lives with
    the Provenance_Navigator (task 12.8), this read-path method must
    already satisfy the same invariant: the underlying Region Occurrence
    and Document Revision rows are immutable (AD-WS-4), so multiple
    reads return identical bytes and identical verified digests.
    """
    content = b"idempotence is required for Property 8 and Requirement 11.5."
    doc = _create_document(engine, evidence_repository, content=content)
    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=0,
        end=11,
    )

    with engine.connect() as conn:
        first = evidence_repository.resolve_region_text(
            conn, region_id=region.region_id, revision_id=doc.revision_id
        )
        second = evidence_repository.resolve_region_text(
            conn, region_id=region.region_id, revision_id=doc.revision_id
        )

    assert first == second


# ---------------------------------------------------------------------------
# Requirement 3.3 / 3.4 — Retrieval after a later Document Revision is
# appended still returns the original V1 span bytes.
# ---------------------------------------------------------------------------


def test_resolve_region_text_returns_v1_span_after_v2_appended(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Requirement 3.3 preserves prior Region Occurrences across new Revisions.

    Combined with Requirement 3.4, this means resolving the V1 Region
    Occurrence after a completely different V2 has been appended must
    still return the V1 span bytes and the V1-recorded digest.
    """
    v1_content = b"version one content with span here"
    doc = _create_document(engine, evidence_repository, content=v1_content)
    start = v1_content.index(b"with span")
    end = start + len(b"with span")
    expected_span = v1_content[start:end]
    expected_digest = hashlib.sha256(expected_span).hexdigest()

    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=start,
        end=end,
    )

    # Append a completely different V2 payload — Requirement 3.3
    # promises this does not perturb the V1 Region Occurrence.
    with engine.begin() as conn:
        v2 = evidence_repository.append_revision(
            conn,
            resource_id=doc.resource_id,
            content_bytes=b"a completely different version two payload",
            contributing_party_id=_PARTY_ID,
        )

    with engine.connect() as conn:
        resolved = evidence_repository.resolve_region_text(
            conn, region_id=region.region_id, revision_id=doc.revision_id
        )

    assert resolved.revision_id == doc.revision_id
    # Still pointing at V1, not V2.
    assert resolved.revision_id != v2.revision_id
    assert resolved.bounded_text == expected_span
    assert resolved.span_content_digest_sha256 == expected_digest


# ---------------------------------------------------------------------------
# Requirement 3.6 — Unresolvable references raise
# :class:`RegionOccurrenceNotFoundError`.
# ---------------------------------------------------------------------------


def test_resolve_region_text_unknown_region_id_raises(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """An unknown ``region_id`` raises ``RegionOccurrenceNotFoundError``.

    Validates: Requirement 3.6.
    """
    doc = _create_document(engine, evidence_repository, content=b"valid bytes")
    unknown_region_id = "00000000-0000-7000-8000-0000000000aa"

    with engine.connect() as conn:
        with pytest.raises(RegionOccurrenceNotFoundError) as exc_info:
            evidence_repository.resolve_region_text(
                conn,
                region_id=unknown_region_id,
                revision_id=doc.revision_id,
            )

    assert exc_info.value.region_id == unknown_region_id
    assert exc_info.value.revision_id == doc.revision_id
    # The error indication identifies both halves of the unresolvable
    # reference (Requirement 3.6 — "identifying the unresolvable
    # reference").
    assert unknown_region_id in str(exc_info.value)
    assert doc.revision_id in str(exc_info.value)


def test_resolve_region_text_unknown_revision_id_raises(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """An unknown ``revision_id`` raises ``RegionOccurrenceNotFoundError``."""
    doc = _create_document(engine, evidence_repository, content=b"valid bytes")
    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=0,
        end=5,
    )
    unknown_revision_id = "00000000-0000-7000-8000-0000000000bb"

    with engine.connect() as conn:
        with pytest.raises(RegionOccurrenceNotFoundError):
            evidence_repository.resolve_region_text(
                conn,
                region_id=region.region_id,
                revision_id=unknown_revision_id,
            )


def test_resolve_region_text_known_region_but_no_occurrence_in_this_revision_raises(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """A known ``region_id`` paired with a Revision that has no Occurrence
    for it raises ``RegionOccurrenceNotFoundError``.

    This is the "Region exists but not in this Revision" branch of
    Requirement 3.6: the caller asked for a Content Region reference
    that the system genuinely cannot resolve to a Region Occurrence,
    even though both halves of the composite key are individually
    known to the system.
    """
    v1_content = b"version one content"
    doc = _create_document(engine, evidence_repository, content=v1_content)
    region = _create_region(
        engine,
        evidence_repository,
        resource_id=doc.resource_id,
        revision_id=doc.revision_id,
        start=0,
        end=5,
    )
    # Append V2 but do NOT create a Region Occurrence on it.
    with engine.begin() as conn:
        v2 = evidence_repository.append_revision(
            conn,
            resource_id=doc.resource_id,
            content_bytes=b"version two content",
            contributing_party_id=_PARTY_ID,
        )

    with engine.connect() as conn:
        with pytest.raises(RegionOccurrenceNotFoundError) as exc_info:
            evidence_repository.resolve_region_text(
                conn,
                region_id=region.region_id,
                revision_id=v2.revision_id,
            )

    assert exc_info.value.region_id == region.region_id
    assert exc_info.value.revision_id == v2.revision_id


def test_get_region_occurrence_unknown_region_id_raises(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """The same Requirement 3.6 error type is raised from the read-only
    ``get_region_occurrence`` surface (used by Provenance_Navigator to
    inspect metadata without resolving bytes)."""
    doc = _create_document(engine, evidence_repository, content=b"valid bytes")
    unknown_region_id = "00000000-0000-7000-8000-0000000000cc"

    with engine.connect() as conn:
        with pytest.raises(RegionOccurrenceNotFoundError):
            evidence_repository.get_region_occurrence(
                conn,
                region_id=unknown_region_id,
                revision_id=doc.revision_id,
            )


# ---------------------------------------------------------------------------
# Requirement 3.5 validation matrix recap. Existing examples in
# ``test_evidence_regions.py`` are exhaustive; the recap here keeps the
# rejection branches under direct test in the same module as the
# retrieval tests so a regression in span validation can never hide
# behind retrieval-path coverage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "start, end, expected_constraint",
    [
        # Empty span — start == end, explicitly named in Requirement 3.5.
        (3, 3, "start_offset_not_less_than_end_offset"),
        # Inverted span — start > end, also explicitly named.
        (5, 2, "start_offset_not_less_than_end_offset"),
        # Negative start — Requirement 3.5 requires 0 <= start.
        (-1, 4, "start_offset_negative"),
        # Out-of-range end — span extends beyond the bounded text.
        (0, 999, "end_offset_exceeds_content_length"),
    ],
    ids=[
        "empty_span_start_equals_end",
        "inverted_span_start_greater_than_end",
        "negative_start_offset",
        "end_offset_exceeds_content_length",
    ],
)
def test_invalid_span_matrix_rejected_with_named_constraint(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    start: int,
    end: int,
    expected_constraint: str,
) -> None:
    """Recap matrix for Requirement 3.5: empty, inverted, negative-start,
    and out-of-range spans are rejected with named constraints."""
    doc = _create_document(engine, evidence_repository, content=b"valid bytes")

    with engine.begin() as conn:
        with pytest.raises(InvalidSpanError) as exc_info:
            evidence_repository.create_region_occurrence(
                conn,
                resource_id=doc.resource_id,
                revision_id=doc.revision_id,
                start_offset_bytes=start,
                end_offset_bytes=end,
                contributing_party_id=_PARTY_ID,
            )

    assert exc_info.value.failed_constraint == expected_constraint


def test_invalid_span_matrix_leaves_no_region_or_occurrence_rows(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Per Requirement 3.5 ('decline to assign a Region Identity or record
    a Region Occurrence'): an invalid span observed via a retrieval-test
    setup must not leak partial rows that subsequent retrievals could
    incorrectly resolve. We try every rejection branch and then assert
    the persistence remains empty."""
    doc = _create_document(engine, evidence_repository, content=b"valid bytes")

    # Walk the matrix in a single test so the post-state check sees the
    # cumulative effect of all rejections.
    cases = [
        (3, 3),       # empty
        (5, 2),       # inverted
        (-1, 4),      # negative start
        (0, 999),     # out of range
    ]
    for start, end in cases:
        with engine.begin() as conn:
            with pytest.raises(InvalidSpanError):
                evidence_repository.create_region_occurrence(
                    conn,
                    resource_id=doc.resource_id,
                    revision_id=doc.revision_id,
                    start_offset_bytes=start,
                    end_offset_bytes=end,
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
