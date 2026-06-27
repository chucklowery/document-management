"""Unit tests for :meth:`walking_slice.evidence.EvidenceRepository.rename_document`.

These tests pin the contract established in task 5.3, design
§"Evidence_Repository" (Source_Documents row, ``current_location`` mutable,
identifiers stable across renames), and Requirements 1.3 and 13.1.

Behaviour covered:

- A successful rename updates ``Source_Documents.current_location`` while
  leaving ``resource_id`` and every existing ``Document_Revisions.revision_id``
  byte-equivalent to their pre-rename values (Requirement 1.3).
- An ``Audit_Records`` row with ``action_type='rename.document'``,
  ``outcome='consequential'``, ``target_id=resource_id``, and
  ``target_revision_id IS NULL`` is appended in the same transaction
  (Requirement 13.1, AD-WS-5).
- An unknown ``resource_id`` raises :class:`SourceDocumentNotFoundError`
  before any write happens, leaving the database untouched.
- Reading the document after rename via :meth:`EvidenceRepository.get_revision`
  returns the same revision bytes and digest (renames do not touch
  Document_Revisions).
- Region Occurrences anchored to a Document Revision before rename remain
  byte-equivalent and resolvable after rename (Property 14 / Requirement 1.3).
- Two successive renames append two audit rows but ``Source_Documents`` retains
  only the most recent ``current_location`` (the column is mutable).
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
    InvalidContentError,
    RenameDocumentResult,
    SourceDocumentNotFoundError,
)
from walking_slice.identity import IdentityService


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers — only the Parties row is required for the FK on
# ``Document_Revisions.contributing_party_id`` and ``Audit_Records.actor_party_id``.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
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
    content: bytes = b"initial document body",
    current_location: str | None = "/inbox/original.txt",
):
    """Helper: seed a Party + create a Source Document and return ids."""
    with engine.begin() as conn:
        _seed_party(conn)
        return evidence_repository.create_document(
            conn,
            content_bytes=content,
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
            current_location=current_location,
        )


def _fetch_source_document(engine: Engine, resource_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT resource_id, current_location, authority, "
                    "external_identifier, source_system_id, created_at "
                    "FROM Source_Documents WHERE resource_id = :rid"
                ),
                {"rid": resource_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _fetch_document_revision_ids(engine: Engine, resource_id: str) -> list[str]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT revision_id FROM Document_Revisions "
                    "WHERE resource_id = :rid ORDER BY recorded_at, revision_id"
                ),
                {"rid": resource_id},
            )
            .mappings()
            .all()
        )
    return [row["revision_id"] for row in rows]


def _fetch_audit_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT actor_party_id, action_type, outcome, target_id, "
                    "target_revision_id, correlation_id, recorded_at, "
                    "append_sequence "
                    "FROM Audit_Records ORDER BY append_sequence"
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Happy path — Requirement 1.3 (identity preserved) and 13.1 (audit appended)
# ---------------------------------------------------------------------------


def test_successful_rename_updates_current_location_only(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """A successful rename mutates ``current_location`` and leaves
    ``resource_id`` and every existing ``revision_id`` byte-equivalent."""
    doc = _create_document(engine, evidence_repository)
    pre_rename_doc = _fetch_source_document(engine, doc.resource_id)
    pre_rename_revisions = _fetch_document_revision_ids(engine, doc.resource_id)
    assert pre_rename_doc is not None
    assert pre_rename_doc["current_location"] == "/inbox/original.txt"
    assert pre_rename_revisions == [doc.revision_id]

    with engine.begin() as conn:
        result = evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/archive/renamed.txt",
            actor_party_id=_PARTY_ID,
        )

    assert isinstance(result, RenameDocumentResult)
    assert result.resource_id == doc.resource_id
    assert result.new_current_location == "/archive/renamed.txt"
    assert result.previous_location == "/inbox/original.txt"
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at), result.recorded_at

    post_rename_doc = _fetch_source_document(engine, doc.resource_id)
    assert post_rename_doc is not None
    assert post_rename_doc["resource_id"] == doc.resource_id
    assert post_rename_doc["current_location"] == "/archive/renamed.txt"
    # Other Source_Documents columns are byte-equivalent across the rename.
    assert post_rename_doc["authority"] == pre_rename_doc["authority"]
    assert (
        post_rename_doc["external_identifier"]
        == pre_rename_doc["external_identifier"]
    )
    assert post_rename_doc["source_system_id"] == pre_rename_doc["source_system_id"]
    assert post_rename_doc["created_at"] == pre_rename_doc["created_at"]

    # Requirement 1.3 — every existing Revision Identity is preserved.
    post_rename_revisions = _fetch_document_revision_ids(engine, doc.resource_id)
    assert post_rename_revisions == pre_rename_revisions


def test_rename_appends_consequential_audit_row(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """An audit row with action_type='rename.document', outcome='consequential',
    target_id=resource_id, and NULL target_revision_id is appended in the
    same transaction (Requirement 13.1, AD-WS-5)."""
    doc = _create_document(engine, evidence_repository)
    with engine.begin() as conn:
        evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/archive/renamed.txt",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-rename-1",
        )

    audit_rows = _fetch_audit_rows(engine)
    # First row was create.document_revision; second is our rename.
    assert len(audit_rows) == 2
    rename_row = audit_rows[1]
    assert rename_row["actor_party_id"] == _PARTY_ID
    assert rename_row["action_type"] == "rename.document"
    assert rename_row["outcome"] == "consequential"
    assert rename_row["target_id"] == doc.resource_id
    assert rename_row["target_revision_id"] is None
    assert rename_row["correlation_id"] == "corr-rename-1"
    assert rename_row["recorded_at"] == _TS_FIXED


def test_rename_to_none_clears_current_location(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Passing ``new_current_location=None`` clears the column (nullable)."""
    doc = _create_document(engine, evidence_repository)
    with engine.begin() as conn:
        result = evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location=None,
            actor_party_id=_PARTY_ID,
        )
    assert result.new_current_location is None
    row = _fetch_source_document(engine, doc.resource_id)
    assert row is not None
    assert row["current_location"] is None


def test_rename_when_initial_location_was_none_reports_previous_none(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """``previous_location`` is ``None`` when the Source Document had none."""
    doc = _create_document(
        engine, evidence_repository, current_location=None
    )
    with engine.begin() as conn:
        result = evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/first/path.txt",
            actor_party_id=_PARTY_ID,
        )
    assert result.previous_location is None
    assert result.new_current_location == "/first/path.txt"


# ---------------------------------------------------------------------------
# Unknown resource_id rejection
# ---------------------------------------------------------------------------


def test_unknown_resource_id_raises_source_document_not_found(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """An unknown resource_id raises SourceDocumentNotFoundError and
    leaves the database untouched."""
    unknown = "00000000-0000-7000-8000-00000000ffff"
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(SourceDocumentNotFoundError) as exc_info:
            evidence_repository.rename_document(
                conn,
                resource_id=unknown,
                new_current_location="/anywhere.txt",
                actor_party_id=_PARTY_ID,
            )
    assert exc_info.value.resource_id == unknown

    # No Source_Documents row was created and no audit row was appended.
    with engine.connect() as conn:
        sd_count = conn.execute(
            text("SELECT COUNT(*) FROM Source_Documents")
        ).scalar_one()
        audit_count = conn.execute(
            text("SELECT COUNT(*) FROM Audit_Records")
        ).scalar_one()
    assert sd_count == 0
    assert audit_count == 0


def test_missing_actor_party_id_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Empty ``actor_party_id`` is rejected with the same constraint name
    used elsewhere in the Evidence_Repository."""
    doc = _create_document(engine, evidence_repository)
    with engine.begin() as conn:
        with pytest.raises(InvalidContentError) as exc_info:
            evidence_repository.rename_document(
                conn,
                resource_id=doc.resource_id,
                new_current_location="/no-actor.txt",
                actor_party_id="",
            )
    assert exc_info.value.failed_constraint == "contributing_party_id_missing"

    # The Source Document still has its pre-rename current_location.
    row = _fetch_source_document(engine, doc.resource_id)
    assert row is not None
    assert row["current_location"] == "/inbox/original.txt"


# ---------------------------------------------------------------------------
# Reading the document after rename returns the same Revision content
# ---------------------------------------------------------------------------


def test_get_revision_after_rename_returns_same_bytes_and_digest(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Renames do not touch Document_Revisions; ``get_revision`` returns
    the same revision_id, content bytes, and content digest."""
    content = b"persisted body that survives rename"
    doc = _create_document(engine, evidence_repository, content=content)

    with engine.begin() as conn:
        evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/post-rename.txt",
            actor_party_id=_PARTY_ID,
        )

    with engine.connect() as conn:
        revision = evidence_repository.get_revision(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
        )
    assert revision.resource_id == doc.resource_id
    assert revision.revision_id == doc.revision_id
    assert revision.parent_revision_id is None
    assert revision.content_bytes == content
    assert revision.content_digest_sha256 == doc.content_digest_sha256
    assert revision.contributing_party_id == _PARTY_ID


# ---------------------------------------------------------------------------
# Property 14 / Requirement 1.3 — Region Occurrences remain resolvable
# ---------------------------------------------------------------------------


def test_region_occurrence_remains_resolvable_after_rename(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """A Region Occurrence anchored before a rename remains byte-equivalent
    and resolvable after rename — Requirement 1.3 demands every existing
    Revision Identity (and therefore every Occurrence anchored to it) is
    preserved unchanged."""
    content = b"the quick brown fox jumps over the lazy dog"
    doc = _create_document(engine, evidence_repository, content=content)

    start = content.index(b"brown")
    end = start + len(b"brown fox")
    expected_span = content[start:end]
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

    with engine.begin() as conn:
        evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/archived/fox.txt",
            actor_party_id=_PARTY_ID,
        )

    # Region_Occurrences row is unchanged — same Region Identity, same
    # offsets, same digest.
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT region_id, document_revision_id, start_offset_bytes,
                           end_offset_bytes, span_byte_length,
                           span_content_digest_sha256
                    FROM Region_Occurrences
                    WHERE region_id = :region_id
                      AND document_revision_id = :revision_id
                    """
                ),
                {
                    "region_id": region.region_id,
                    "revision_id": doc.revision_id,
                },
            )
            .mappings()
            .one()
        )
    assert row["region_id"] == region.region_id
    assert row["document_revision_id"] == doc.revision_id
    assert row["start_offset_bytes"] == start
    assert row["end_offset_bytes"] == end
    assert row["span_byte_length"] == end - start
    assert row["span_content_digest_sha256"] == expected_digest

    # And the bytes themselves are reconstructable from the Document
    # Revision, which is what "resolvable" means in §"Provenance_Navigator".
    with engine.connect() as conn:
        blob = conn.execute(
            text(
                "SELECT content_bytes FROM Document_Revisions "
                "WHERE revision_id = :rev"
            ),
            {"rev": doc.revision_id},
        ).scalar_one()
    assert bytes(blob)[start:end] == expected_span


# ---------------------------------------------------------------------------
# Two renames produce two audit rows but one persisted current_location
# ---------------------------------------------------------------------------


def test_two_renames_produce_two_audit_rows_one_current_location(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """``current_location`` is mutable; two renames append two audit rows
    but ``Source_Documents`` retains only the most recent value."""
    doc = _create_document(engine, evidence_repository)

    with engine.begin() as conn:
        first = evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/archive/v1.txt",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-rename-1",
        )
    with engine.begin() as conn:
        second = evidence_repository.rename_document(
            conn,
            resource_id=doc.resource_id,
            new_current_location="/archive/v2.txt",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-rename-2",
        )

    # The second rename's previous_location is the first rename's new path.
    assert first.previous_location == "/inbox/original.txt"
    assert first.new_current_location == "/archive/v1.txt"
    assert second.previous_location == "/archive/v1.txt"
    assert second.new_current_location == "/archive/v2.txt"

    # Only the most recent current_location is persisted.
    row = _fetch_source_document(engine, doc.resource_id)
    assert row is not None
    assert row["current_location"] == "/archive/v2.txt"

    # Two rename.document audit rows exist in addition to the original
    # create.document_revision row.
    audit_rows = _fetch_audit_rows(engine)
    rename_rows = [r for r in audit_rows if r["action_type"] == "rename.document"]
    assert len(rename_rows) == 2
    assert {r["correlation_id"] for r in rename_rows} == {
        "corr-rename-1",
        "corr-rename-2",
    }
    # Append sequence monotonically increases across the three rows.
    sequences = [r["append_sequence"] for r in audit_rows]
    assert sequences == sorted(sequences)
    assert len(sequences) == 3
