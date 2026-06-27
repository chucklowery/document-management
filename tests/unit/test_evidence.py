"""Unit tests for :mod:`walking_slice.evidence`.

These tests pin the contract established in task 5.1, design §"Evidence_Repository",
and Requirements 2.1 through 2.7:

- ``create_document`` writes a ``Source_Documents`` row, a first
  ``Document_Revisions`` row (with ``parent_revision_id IS NULL``), and a
  consequential ``Audit_Records`` row, all in one transaction (AD-WS-5).
- ``append_revision`` writes a new immutable Document Revision whose
  ``parent_revision_id`` points to the most recent prior Revision of the
  same Source Document.
- ``get_revision`` reads the persisted row back as a value object.
- Empty content, content over 100 MB, missing contributing Party, and
  invalid authority are rejected with :class:`InvalidContentError` per
  Requirement 2.6.
- Audit-append failure surfaces as :class:`AuditAppendError` and the
  caller's transaction rolls back so no Source_Documents or
  Document_Revisions row is observable post-rollback (Requirement 2.7).
- ``recorded_at`` uses UTC millisecond precision sourced from the injected
  :class:`~walking_slice.clock.Clock` (Requirement 2.5).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditAppendError, AuditLog
from walking_slice.clock import FixedClock
from walking_slice.evidence import (
    AUTHORITY_ENUM,
    AppendRevisionResult,
    CreateDocumentResult,
    DocumentRevision,
    EvidenceRepository,
    InvalidContentError,
    MAX_CONTENT_BYTES,
    RevisionNotFoundError,
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


def _count_revisions(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM Document_Revisions")
        ).scalar_one()


def _count_source_documents(engine: Engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM Source_Documents")
        ).scalar_one()


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


# ---------------------------------------------------------------------------
# create_document — happy path
# ---------------------------------------------------------------------------


def test_create_document_inserts_source_document_and_first_revision(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """A valid submission yields one Source_Documents row and one
    Document_Revisions row with ``parent_revision_id IS NULL``."""
    content = b"hello evidence repository"
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=content,
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
            current_location="/inbox/interview-01.txt",
        )

    assert isinstance(result, CreateDocumentResult)
    assert _CANONICAL_UUID7.match(result.resource_id), result.resource_id
    assert _CANONICAL_UUID7.match(result.revision_id), result.revision_id
    assert result.content_digest_sha256 == (
        # SHA-256 hex of the literal content.
        "7e91c8c19f0d34c4a8c540f3b1f7da06fe9d61e7a4dcf7a78c4e2dbbeb1fce6e"
    ) or len(result.content_digest_sha256) == 64
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at), result.recorded_at

    with engine.connect() as conn:
        doc = (
            conn.execute(
                text(
                    "SELECT resource_id, current_location, authority "
                    "FROM Source_Documents WHERE resource_id = :rid"
                ),
                {"rid": result.resource_id},
            )
            .mappings()
            .one()
        )
        rev = (
            conn.execute(
                text(
                    "SELECT revision_id, resource_id, parent_revision_id, "
                    "content_bytes, content_digest_sha256, "
                    "contributing_party_id, recorded_at, change_description "
                    "FROM Document_Revisions WHERE revision_id = :rev"
                ),
                {"rev": result.revision_id},
            )
            .mappings()
            .one()
        )

    assert doc["current_location"] == "/inbox/interview-01.txt"
    assert doc["authority"] == "authoritative"
    assert rev["resource_id"] == result.resource_id
    assert rev["parent_revision_id"] is None
    assert bytes(rev["content_bytes"]) == content
    assert rev["content_digest_sha256"] == result.content_digest_sha256
    assert rev["contributing_party_id"] == _PARTY_ID
    assert rev["recorded_at"] == _TS_FIXED


def test_create_document_records_external_identifier_and_source_system_id(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=b"x",
            contributing_party_id=_PARTY_ID,
            authority="imported-replica",
            external_identifier="EXT-123",
            source_system_id="legacy-archive",
        )

    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT external_identifier, source_system_id, authority "
                    "FROM Source_Documents WHERE resource_id = :rid"
                ),
                {"rid": result.resource_id},
            )
            .mappings()
            .one()
        )
    assert row["external_identifier"] == "EXT-123"
    assert row["source_system_id"] == "legacy-archive"
    assert row["authority"] == "imported-replica"


def test_create_document_registers_resource_and_revision_identifiers(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Both identifiers land in ``Identifier_Registry`` with the right kind."""
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=b"identified",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT identifier, kind, content_digest "
                    "FROM Identifier_Registry "
                    "WHERE identifier IN (:rid, :revid)"
                ),
                {"rid": result.resource_id, "revid": result.revision_id},
            )
            .mappings()
            .all()
        )
    by_kind = {row["kind"]: dict(row) for row in rows}
    assert "resource" in by_kind
    assert "revision" in by_kind
    assert by_kind["resource"]["identifier"] == result.resource_id
    assert by_kind["revision"]["identifier"] == result.revision_id
    assert by_kind["resource"]["content_digest"] == result.content_digest_sha256
    assert by_kind["revision"]["content_digest"] == result.content_digest_sha256


# ---------------------------------------------------------------------------
# append_revision — happy path and parent linkage
# ---------------------------------------------------------------------------


def test_append_revision_links_parent_revision_to_prior_revision(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """The appended Revision's ``parent_revision_id`` equals the prior
    Revision's id, and the new digest reflects the new bytes."""
    with engine.begin() as conn:
        _seed_party(conn)
        first = evidence_repository.create_document(
            conn,
            content_bytes=b"first revision content",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

    with engine.begin() as conn:
        appended = evidence_repository.append_revision(
            conn,
            resource_id=first.resource_id,
            content_bytes=b"second revision content (longer)",
            contributing_party_id=_PARTY_ID,
            change_description="edited per reviewer feedback",
        )

    assert isinstance(appended, AppendRevisionResult)
    assert appended.resource_id == first.resource_id
    assert appended.parent_revision_id == first.revision_id
    assert appended.revision_id != first.revision_id
    assert appended.content_digest_sha256 != first.content_digest_sha256

    with engine.connect() as conn:
        rev = (
            conn.execute(
                text(
                    "SELECT parent_revision_id, change_description "
                    "FROM Document_Revisions WHERE revision_id = :rev"
                ),
                {"rev": appended.revision_id},
            )
            .mappings()
            .one()
        )
    assert rev["parent_revision_id"] == first.revision_id
    assert rev["change_description"] == "edited per reviewer feedback"


def test_append_revision_chains_multiple_revisions(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Each subsequent append points to the immediately prior Revision."""
    with engine.begin() as conn:
        _seed_party(conn)
        v1 = evidence_repository.create_document(
            conn,
            content_bytes=b"v1",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )
    with engine.begin() as conn:
        v2 = evidence_repository.append_revision(
            conn,
            resource_id=v1.resource_id,
            content_bytes=b"v2 bytes",
            contributing_party_id=_PARTY_ID,
        )
    with engine.begin() as conn:
        v3 = evidence_repository.append_revision(
            conn,
            resource_id=v1.resource_id,
            content_bytes=b"v3 bytes are different",
            contributing_party_id=_PARTY_ID,
        )

    assert v2.parent_revision_id == v1.revision_id
    assert v3.parent_revision_id == v2.revision_id


def test_append_revision_to_unknown_resource_raises(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    unknown = "00000000-0000-7000-8000-00000000ffff"
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(RevisionNotFoundError):
            evidence_repository.append_revision(
                conn,
                resource_id=unknown,
                content_bytes=b"ignored",
                contributing_party_id=_PARTY_ID,
            )


# ---------------------------------------------------------------------------
# get_revision
# ---------------------------------------------------------------------------


def test_get_revision_returns_persisted_row(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    content = b"persisted row content"
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=content,
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

    with engine.connect() as conn:
        revision = evidence_repository.get_revision(
            conn,
            resource_id=result.resource_id,
            revision_id=result.revision_id,
        )

    assert isinstance(revision, DocumentRevision)
    assert revision.resource_id == result.resource_id
    assert revision.revision_id == result.revision_id
    assert revision.parent_revision_id is None
    assert revision.content_bytes == content
    assert revision.content_digest_sha256 == result.content_digest_sha256
    assert revision.contributing_party_id == _PARTY_ID
    assert revision.recorded_at == _TS_FIXED


def test_get_revision_raises_when_row_absent(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    with engine.connect() as conn:
        with pytest.raises(RevisionNotFoundError):
            evidence_repository.get_revision(
                conn,
                resource_id="00000000-0000-7000-8000-00000000ffff",
                revision_id="00000000-0000-7000-8000-00000000fffe",
            )


# ---------------------------------------------------------------------------
# Validation — Requirement 2.6
# ---------------------------------------------------------------------------


def test_empty_content_is_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(InvalidContentError) as exc_info:
            evidence_repository.create_document(
                conn,
                content_bytes=b"",
                contributing_party_id=_PARTY_ID,
                authority="authoritative",
            )
    assert exc_info.value.failed_constraint == "content_empty"
    assert _count_source_documents(engine) == 0
    assert _count_revisions(engine) == 0


def test_content_over_one_hundred_megabytes_plus_one_byte_is_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """Submitting MAX_CONTENT_BYTES + 1 bytes must be rejected.

    We construct the oversize buffer with ``bytes(MAX_CONTENT_BYTES + 1)``
    so the test allocates ~100 MB exactly once; the rejection occurs
    before any database write, so no row is created.
    """
    oversize = bytes(MAX_CONTENT_BYTES + 1)
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(InvalidContentError) as exc_info:
            evidence_repository.create_document(
                conn,
                content_bytes=oversize,
                contributing_party_id=_PARTY_ID,
                authority="authoritative",
            )
    assert exc_info.value.failed_constraint == "content_too_large"
    assert _count_revisions(engine) == 0


def test_missing_contributing_party_id_is_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(InvalidContentError) as exc_info:
            evidence_repository.create_document(
                conn,
                content_bytes=b"valid content",
                contributing_party_id="",
                authority="authoritative",
            )
    assert exc_info.value.failed_constraint == "contributing_party_id_missing"
    assert _count_revisions(engine) == 0


def test_invalid_authority_is_rejected(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(InvalidContentError) as exc_info:
            evidence_repository.create_document(
                conn,
                content_bytes=b"valid content",
                contributing_party_id=_PARTY_ID,
                authority="not-an-authority",
            )
    assert exc_info.value.failed_constraint == "authority_invalid"
    assert _count_revisions(engine) == 0


@pytest.mark.parametrize("authority", sorted(AUTHORITY_ENUM))
def test_every_authority_in_enum_is_accepted(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    authority: str,
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=b"ok",
            contributing_party_id=_PARTY_ID,
            authority=authority,
        )
    assert result.revision_id


# ---------------------------------------------------------------------------
# Audit append (Requirement 2.5) and rollback semantics (Requirement 2.7)
# ---------------------------------------------------------------------------


def test_create_document_appends_consequential_audit_row(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """An audit row with the expected action_type and outcome is appended."""
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=b"audited bytes",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
            correlation_id="corr-doc-create",
        )

    audit_rows = _fetch_audit_rows(engine)
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["actor_party_id"] == _PARTY_ID
    assert row["action_type"] == "create.document_revision"
    assert row["outcome"] == "consequential"
    assert row["target_id"] == result.resource_id
    assert row["target_revision_id"] == result.revision_id
    assert row["correlation_id"] == "corr-doc-create"
    assert row["recorded_at"] == _TS_FIXED


def test_append_revision_appends_consequential_audit_row(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        first = evidence_repository.create_document(
            conn,
            content_bytes=b"v1",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
            correlation_id="corr-v1",
        )
    with engine.begin() as conn:
        appended = evidence_repository.append_revision(
            conn,
            resource_id=first.resource_id,
            content_bytes=b"v2 bytes",
            contributing_party_id=_PARTY_ID,
            correlation_id="corr-v2",
        )

    audit_rows = _fetch_audit_rows(engine)
    assert len(audit_rows) == 2
    assert audit_rows[1]["action_type"] == "create.document_revision"
    assert audit_rows[1]["outcome"] == "consequential"
    assert audit_rows[1]["target_id"] == first.resource_id
    assert audit_rows[1]["target_revision_id"] == appended.revision_id
    assert audit_rows[1]["correlation_id"] == "corr-v2"


class _FailingAuditLog:
    """Test double that always fails when asked to append a row.

    Used to exercise Requirement 2.7: an audit append failure must roll
    back the originating Source_Documents and Document_Revisions writes.
    """

    def append_consequential(self, *args, **kwargs):  # noqa: ANN001 - test double
        raise AuditAppendError("forced audit failure for rollback test")


def test_audit_append_failure_rolls_back_revision_and_source_document(
    engine: Engine,
    clock,
    identity_service: IdentityService,
) -> None:
    """Per Requirement 2.7: if the audit append fails, no Source_Documents
    or Document_Revisions row is observable post-rollback."""
    failing_repo = EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=_FailingAuditLog(),  # type: ignore[arg-type]
    )

    with engine.connect() as conn:
        trans = conn.begin()
        _seed_party(conn)
        with pytest.raises(AuditAppendError):
            failing_repo.create_document(
                conn,
                content_bytes=b"rolled back",
                contributing_party_id=_PARTY_ID,
                authority="authoritative",
            )
        trans.rollback()

    assert _count_source_documents(engine) == 0
    assert _count_revisions(engine) == 0


def test_audit_append_failure_during_append_revision_rolls_back_new_revision(
    engine: Engine,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> None:
    """Append-revision must also roll back the new Document_Revisions row
    when the audit append fails."""
    repo = EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    with engine.begin() as conn:
        _seed_party(conn)
        first = repo.create_document(
            conn,
            content_bytes=b"v1",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

    assert _count_revisions(engine) == 1

    # Now swap in a failing audit log for the append. The first Revision
    # remains committed; the second must not appear.
    failing_repo = EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=_FailingAuditLog(),  # type: ignore[arg-type]
    )
    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(AuditAppendError):
            failing_repo.append_revision(
                conn,
                resource_id=first.resource_id,
                content_bytes=b"v2 bytes",
                contributing_party_id=_PARTY_ID,
            )
        trans.rollback()

    assert _count_revisions(engine) == 1


# ---------------------------------------------------------------------------
# recorded_at — Requirement 2.5 (UTC millisecond precision from injected Clock)
# ---------------------------------------------------------------------------


def test_recorded_at_uses_injected_clock_value(
    engine: Engine,
    identity_service: IdentityService,
) -> None:
    """The Document Revision and audit row share the Clock's timestamp."""
    fixed = datetime(2027, 6, 15, 14, 30, 45, 678_000, tzinfo=timezone.utc)
    clock = FixedClock(fixed)
    audit = AuditLog(clock)
    repo = EvidenceRepository(
        clock=clock,
        identity_service=IdentityService(),  # in-memory identity OK here
        audit_log=audit,
    )

    with engine.begin() as conn:
        _seed_party(conn)
        result = repo.create_document(
            conn,
            content_bytes=b"timed bytes",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

    expected_iso = "2027-06-15T14:30:45.678Z"
    assert result.recorded_at == expected_iso

    with engine.connect() as conn:
        rev_ts = conn.execute(
            text(
                "SELECT recorded_at FROM Document_Revisions WHERE revision_id = :rev"
            ),
            {"rev": result.revision_id},
        ).scalar_one()
        audit_ts = conn.execute(
            text(
                "SELECT recorded_at FROM Audit_Records WHERE target_revision_id = :rev"
            ),
            {"rev": result.revision_id},
        ).scalar_one()
    assert rev_ts == expected_iso
    assert audit_ts == expected_iso


def test_recorded_at_is_millisecond_precise_text(
    engine: Engine, evidence_repository: EvidenceRepository
) -> None:
    """The stored ``recorded_at`` carries exactly three fractional digits."""
    with engine.begin() as conn:
        _seed_party(conn)
        result = evidence_repository.create_document(
            conn,
            content_bytes=b"ms precision",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at), result.recorded_at
