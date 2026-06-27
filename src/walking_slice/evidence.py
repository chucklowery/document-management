"""Evidence_Repository — Source Documents and Document Revisions persistence.

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Evidence_Repository", §"Table-by-Table Specification" (``Source_Documents``,
``Document_Revisions``), AD-WS-4 (immutable Document Revisions), AD-WS-5
(audit append inside the originating transaction).

Responsibilities (per task 5.1):

- :meth:`EvidenceRepository.create_document` — assign a fresh Resource
  Identity, insert a ``Source_Documents`` row and the first
  ``Document_Revisions`` row, register both identifiers in
  ``Identifier_Registry``, and append a ``'consequential'`` audit row
  carrying ``action_type='create.document_revision'`` — all inside the
  caller's transaction (AD-WS-5).
- :meth:`EvidenceRepository.append_revision` — append a new immutable
  Document Revision to an existing Source Document, computing the SHA-256
  digest over the supplied bytes and linking the new Revision to the
  most recent prior Revision via ``parent_revision_id``.
- :meth:`EvidenceRepository.get_revision` — read a previously persisted
  Document Revision back as a :class:`DocumentRevision` value object.

Validation rules (Requirement 2.6):

- Empty content (``len(content_bytes) == 0``) is rejected with
  :class:`InvalidContentError`.
- Content over 100 MB (``len(content_bytes) > 100 * 1024 * 1024``) is
  rejected with :class:`InvalidContentError`.
- A missing or empty ``contributing_party_id`` is rejected with
  :class:`InvalidContentError`.
- ``authority`` values outside the AD-WS-1 enumeration
  (:data:`AUTHORITY_ENUM`) are rejected with :class:`InvalidContentError`.
- When supplied, ``external_identifier`` must be 1..256 characters and
  ``source_system_id`` must be 1..128 characters (Requirement 2.3).

Requirements satisfied:
    2.1 — Source Document and first Document Revision created in one
          consequential write.
    2.2 — Content digest, recorded time (UTC millisecond precision), and
          contributing Party are recorded on every Document Revision.
    2.3 — ``external_identifier``, ``source_system_id``, and
          ``authority`` are persisted on the Source Document.
    2.4 — Document Revisions are immutable; the schema triggers reject
          UPDATE/DELETE. This module never issues an UPDATE against
          ``Document_Revisions`` once a row has been inserted.
    2.5 — Audit row appended within the same transaction, recorded at
          UTC millisecond precision from the injected :class:`Clock`.
    2.6 — Empty, oversize, and party-less submissions are rejected
          before any database write.
    2.7 — If the audit append fails, the originating transaction rolls
          back; no ``Source_Documents`` or ``Document_Revisions`` row is
          observable post-rollback.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final, Optional

import uuid_utils
from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService


__all__ = [
    "AUTHORITY_ENUM",
    "AppendRevisionResult",
    "CreateDocumentResult",
    "CreateRegionResult",
    "DocumentRevision",
    "EvidenceRepository",
    "InvalidContentError",
    "InvalidSpanError",
    "MAX_CONTENT_BYTES",
    "RegionNotFoundError",
    "RegionOccurrence",
    "RegionOccurrenceNotFoundError",
    "RenameDocumentResult",
    "ResolvedSpan",
    "RevisionNotFoundError",
    "SourceDocumentNotFoundError",
    "SpanDigestMismatchError",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# Per Requirement 2.1 and 2.6: content must be 1..(100 * 1024 * 1024) bytes.
MAX_CONTENT_BYTES: Final[int] = 100 * 1024 * 1024


# Authority designations accepted by ``Source_Documents.authority`` per the
# AD-WS-1 enumeration and design §"Table-by-Table Specification". Centralized
# here so the Python validator and the schema CHECK constraint stay aligned;
# if either changes, both must change.
AUTHORITY_ENUM: Final[frozenset[str]] = frozenset(
    {
        "authoritative",
        "imported-replica",
        "imported-projection",
        "imported-index",
        "imported-federation-point",
        "reference-to-system-of-record",
    }
)


# Action name written to ``Audit_Records.action_type`` for every consequential
# Document Revision write per design §"Audit_Log" verbs.
_AUDIT_ACTION_CREATE_REVISION: Final[str] = "create.document_revision"


# Action name written to ``Audit_Records.action_type`` for every consequential
# Region Occurrence write per design §"Audit_Log" verbs (task 5.2).
_AUDIT_ACTION_CREATE_REGION_OCCURRENCE: Final[str] = "create.region_occurrence"


# Action name written to ``Audit_Records.action_type`` when a Source Document
# is renamed or relocated per task 5.3 and design §"Evidence_Repository" HTTP
# surface (``PATCH /api/v1/documents/{resource_id}/location``). A rename
# mutates only ``Source_Documents.current_location`` — Requirement 1.3 forbids
# changing ``resource_id`` or any existing Document Revision identifier — so
# the audit row's ``target_id`` is the Source Document's Resource Identity
# and ``target_revision_id`` is deliberately left NULL. The rename is still
# a *consequential* write because the display path is the user-visible
# attribute of the Source Document and downstream systems need an audit
# trail of every change to it (Requirement 13.1).
_AUDIT_ACTION_RENAME_DOCUMENT: Final[str] = "rename.document"


# Stable primary key for the AD-WS-6 row in ``Interim_ADR_Records``. Using a
# fixed identifier (rather than a fresh UUIDv7) makes the
# ``INSERT OR IGNORE`` lazy-seed in :meth:`EvidenceRepository.create_region_occurrence`
# idempotent across processes and across repeated calls within the same
# process: every attempt to insert collides on the PRIMARY KEY and silently
# becomes a no-op, exactly as required by task 5.2's "use INSERT OR IGNORE
# so multiple instances don't duplicate" note.
_AD_WS_6_RECORD_ID: Final[str] = "ad-ws-6"


# AD-WS-6 row contents — see design §"Architectural Decisions → AD-WS-6 —
# Interim Content Region anchoring (input to ADR-HT-003)". The seed exists so
# Property 15 ("Interim ADR records retrievable by backlog ADR identifier")
# can locate the record by ``backlog_adr_id = 'ADR-HT-003'`` even before
# task 13.1's startup seeding ships.
_AD_WS_6_MOTIVATING_REQUIREMENT: Final[str] = "Requirement 3.1, 3.2; Gap G-1"
_AD_WS_6_MOTIVATING_CRITERION: Final[str] = "byte-offset anchoring"
_AD_WS_6_OBSERVABLE_BEHAVIOR: Final[str] = (
    "spans are validated against the Document Revision's content_bytes length"
)
_AD_WS_6_BACKLOG_ADR_ID: Final[str] = "ADR-HT-003"


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class InvalidContentError(ValueError):
    """Raised when a submission fails the Requirement 2.6 validation.

    Carries the name of the failed constraint so the HTTP layer added by
    task 5.4 can map the exception to a structured 400 response with a
    field-pointing error code.

    Attributes:
        failed_constraint: One of ``"content_empty"``, ``"content_too_large"``,
            ``"contributing_party_id_missing"``, ``"authority_invalid"``,
            ``"external_identifier_invalid"``, ``"source_system_id_invalid"``.
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


class RevisionNotFoundError(LookupError):
    """Raised by :meth:`EvidenceRepository.get_revision` when no row matches.

    Maps to a 404 in the HTTP surface (task 5.4). Carries the requested
    ``resource_id``/``revision_id`` pair for diagnostics.
    """

    def __init__(self, *, resource_id: str, revision_id: str) -> None:
        super().__init__(
            f"No Document_Revisions row for resource_id={resource_id!r}, "
            f"revision_id={revision_id!r}."
        )
        self.resource_id = resource_id
        self.revision_id = revision_id


class InvalidSpanError(ValueError):
    """Raised when a Region Occurrence submission fails Requirement 3.5.

    Requirement 3.5 demands the ``Evidence_Repository`` reject any span
    that is empty, that extends beyond the bounded text of the target
    Document Revision, or whose start anchor is positioned at or after
    its end anchor. The exception carries the name of the failed
    constraint so the HTTP layer (task 5.4) can render a structured
    400 response identifying the specific violation.

    Attributes:
        failed_constraint: One of
            ``"start_offset_negative"``,
            ``"start_offset_not_integer"``,
            ``"end_offset_not_integer"``,
            ``"start_offset_not_less_than_end_offset"`` (covers both
                the empty-span case ``start == end`` and the inverted
                ``start > end`` case),
            ``"end_offset_exceeds_content_length"``.
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


class RegionNotFoundError(LookupError):
    """Raised when a caller-supplied ``region_id`` is not registered.

    :meth:`EvidenceRepository.create_region_occurrence` accepts an
    optional ``region_id`` so callers can anchor a span of an existing
    Region in a different Document Revision (Requirement 3.3). When the
    supplied identifier does not match an existing ``Content_Regions``
    row, the call fails with this exception so the persistence write is
    never attempted with a dangling foreign key.
    """

    def __init__(self, *, region_id: str) -> None:
        super().__init__(f"No Content_Regions row for region_id={region_id!r}.")
        self.region_id = region_id


class SourceDocumentNotFoundError(LookupError):
    """Raised when a ``resource_id`` does not name an existing Source Document.

    :meth:`EvidenceRepository.rename_document` raises this when the caller
    supplies a ``resource_id`` that is not present in ``Source_Documents``.
    The HTTP layer (task 5.4 — ``PATCH /api/v1/documents/{resource_id}/location``)
    maps this to a 404; tests use the exception type to verify that the
    rename never silently created a new Source Document or mutated an
    unrelated row when the caller asked for a non-existent one.

    Carries the requested ``resource_id`` for diagnostics.
    """

    def __init__(self, *, resource_id: str) -> None:
        super().__init__(
            f"No Source_Documents row for resource_id={resource_id!r}."
        )
        self.resource_id = resource_id


class RegionOccurrenceNotFoundError(LookupError):
    """Raised when ``(region_id, revision_id)`` does not name a Region Occurrence.

    Requirement 3.6: "IF an authorized user resolves a Content Region
    reference that does not correspond to any recorded Region Occurrence,
    THEN THE Provenance_Navigator SHALL decline to return a bounded text
    span and return an error indication identifying the unresolvable
    reference."

    Raised from :meth:`EvidenceRepository.get_region_occurrence` and
    :meth:`EvidenceRepository.resolve_region_text` whenever the composite
    key ``(region_id, document_revision_id)`` does not match an existing
    ``Region_Occurrences`` row. The exception is the single error type
    for every unresolvable shape — unknown ``region_id``, unknown
    ``revision_id``, and known ``region_id`` paired with a ``revision_id``
    that has no Occurrence for that Region — so the HTTP layer (task
    12.3) and the Provenance_Navigator (task 12.4) can map one exception
    type to one error response without leaking which dimension was
    missing (Property 4, Requirement 8.5).

    Attributes:
        region_id: The caller-supplied Region Identity.
        revision_id: The caller-supplied Document Revision Identity.
    """

    def __init__(self, *, region_id: str, revision_id: str) -> None:
        super().__init__(
            f"No Region_Occurrences row for region_id={region_id!r}, "
            f"document_revision_id={revision_id!r}."
        )
        self.region_id = region_id
        self.revision_id = revision_id


class SpanDigestMismatchError(RuntimeError):
    """Raised when a Region Occurrence's recorded digest does not match the bytes.

    :meth:`EvidenceRepository.resolve_region_text` recomputes the SHA-256
    of ``content_bytes[start:end]`` and compares it to the persisted
    ``Region_Occurrences.span_content_digest_sha256``. AD-WS-4 makes every
    Document Revision and Region Occurrence row immutable via triggers,
    so a mismatch indicates database corruption or a schema invariant
    violation — neither of which is recoverable by the caller. The
    exception carries the offending identifiers and both digests for
    diagnostics; HTTP callers (task 12.3) map it to a 500 rather than a
    404 because it signals the server's view of its own state is
    internally inconsistent.

    Attributes:
        region_id: Region Identity whose Occurrence failed integrity.
        revision_id: Document Revision Identity owning the Occurrence.
        recorded_digest: ``span_content_digest_sha256`` from the row.
        computed_digest: Lowercase-hex SHA-256 of the resolved bytes.
    """

    def __init__(
        self,
        *,
        region_id: str,
        revision_id: str,
        recorded_digest: str,
        computed_digest: str,
    ) -> None:
        super().__init__(
            f"Region Occurrence digest mismatch for region_id={region_id!r}, "
            f"document_revision_id={revision_id!r}: recorded="
            f"{recorded_digest!r}, computed={computed_digest!r}."
        )
        self.region_id = region_id
        self.revision_id = revision_id
        self.recorded_digest = recorded_digest
        self.computed_digest = computed_digest


@dataclass(frozen=True)
class CreateDocumentResult:
    """Result of :meth:`EvidenceRepository.create_document`.

    Returned to the caller so they can correlate the created Source Document
    with downstream operations (Region creation, Finding citation) without
    a second round-trip.
    """

    resource_id: str
    revision_id: str
    content_digest_sha256: str
    recorded_at: str


@dataclass(frozen=True)
class AppendRevisionResult:
    """Result of :meth:`EvidenceRepository.append_revision`.

    ``parent_revision_id`` is always populated — append_revision requires
    the target Source Document already exist, so a prior Revision is
    always present.
    """

    resource_id: str
    revision_id: str
    parent_revision_id: str
    content_digest_sha256: str
    recorded_at: str


@dataclass(frozen=True)
class DocumentRevision:
    """Persisted Document Revision row read by :meth:`EvidenceRepository.get_revision`.

    Mirrors the ``Document_Revisions`` schema columns exactly so callers
    can verify byte-equivalence against what was inserted (Property 12).
    """

    resource_id: str
    revision_id: str
    parent_revision_id: Optional[str]
    content_bytes: bytes
    content_digest_sha256: str
    contributing_party_id: str
    recorded_at: str
    change_description: Optional[str]


@dataclass(frozen=True)
class RenameDocumentResult:
    """Result of :meth:`EvidenceRepository.rename_document`.

    The rename touches one ``Source_Documents`` column (``current_location``)
    and appends one ``Audit_Records`` row. The result echoes the Resource
    Identity (Requirement 1.3 — preserved unchanged across the operation),
    the new and previous display paths, and the audit row's recorded time
    so callers can correlate a rename with its audit entry without an
    extra round-trip.

    Attributes:
        resource_id: Identity of the renamed Source Document. Byte-equal to
            the ``resource_id`` supplied by the caller. Requirement 1.3
            forbids changing it during a rename.
        new_current_location: The post-rename value of
            ``Source_Documents.current_location``. May be ``None`` when the
            caller asked to clear the display path (the column is nullable).
        previous_location: The pre-rename value of
            ``Source_Documents.current_location`` as read inside the same
            transaction. ``None`` when the Source Document was created
            without a ``current_location``.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp shared by
            the UPDATE and the consequential audit row (AD-WS-5 — every
            artifact of one transaction shares one clock reading).
    """

    resource_id: str
    new_current_location: Optional[str]
    previous_location: Optional[str]
    recorded_at: str


@dataclass(frozen=True)
class CreateRegionResult:
    """Result of :meth:`EvidenceRepository.create_region_occurrence`.

    Returned so the caller can persist the Region Occurrence's anchors
    and digest into a Finding's ``Supports`` Relationship without a
    second round-trip, and so tests can verify Requirement 3.2 (the
    Region Occurrence records owning Revision Identity, start anchor,
    end anchor, and content digest of the bounded span).

    Attributes:
        region_id: Identity of the Content Region. New when the
            originating call generated it; equal to the caller-supplied
            value when the call reused an existing Region for a
            different Revision (Requirement 3.3).
        revision_id: Identity of the Document Revision this Occurrence
            anchors against (the second half of the
            ``Region_Occurrences`` composite key).
        start_offset_bytes: Byte offset of the first byte of the span,
            ``0`` ≤ ``start`` < ``end`` (AD-WS-6).
        end_offset_bytes: Byte offset one past the last byte of the
            span, ``start`` < ``end`` ≤ ``len(content_bytes)``.
        span_byte_length: ``end_offset_bytes - start_offset_bytes``.
            Stored explicitly on the row so the schema CHECK constraint
            can verify the arithmetic.
        span_content_digest_sha256: Lowercase-hex SHA-256 of
            ``content_bytes[start:end]``. Used by
            ``Provenance_Navigator`` for the Region resolvability
            check (Requirement 11.2, Property 9).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Content_Regions`` insert (when a new
            Region was created), the ``Region_Occurrences`` insert,
            and the consequential audit row.
    """

    region_id: str
    revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    recorded_at: str


@dataclass(frozen=True)
class RegionOccurrence:
    """Persisted ``Region_Occurrences`` row read by
    :meth:`EvidenceRepository.get_region_occurrence`.

    Mirrors the ``Region_Occurrences`` schema columns exactly so tests
    and the ``Provenance_Navigator`` can verify byte-equivalence against
    what was inserted (Requirement 3.2, Property 12 — immutability). The
    composite primary key ``(region_id, document_revision_id)`` is
    surfaced as two separate attributes so callers do not need to
    reconstruct the tuple to compare against a caller-supplied
    reference.

    Attributes:
        region_id: Identity of the Content Region.
        document_revision_id: Identity of the Document Revision this
            Occurrence anchors against.
        start_offset_bytes: Inclusive byte offset of the span's start.
        end_offset_bytes: Exclusive byte offset of the span's end.
        span_byte_length: ``end_offset_bytes - start_offset_bytes``;
            persisted explicitly per AD-WS-6.
        span_content_digest_sha256: Lowercase-hex SHA-256 of the
            bounded span recorded at occurrence-creation time.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp at
            which the Occurrence was created.
    """

    region_id: str
    document_revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    recorded_at: str


@dataclass(frozen=True)
class ResolvedSpan:
    """Byte-equivalent text span returned by
    :meth:`EvidenceRepository.resolve_region_text`.

    Bundles the Region Occurrence identity, anchors, verified digest,
    and the resolved bytes so the caller does not need a second
    round-trip to extract ``content_bytes[start:end]`` from the owning
    Document Revision.

    Requirement 3.4 demands the returned span be *byte-equivalent to
    the span originally recorded for that Region Occurrence*; the
    ``bounded_text`` attribute carries exactly those bytes and the
    ``span_content_digest_sha256`` attribute is the digest verified
    against the persisted value before this object is constructed
    (see :meth:`EvidenceRepository.resolve_region_text`). The caller
    therefore does not need to recompute the digest itself — equality
    with the persisted ``Region_Occurrences.span_content_digest_sha256``
    is an invariant of the constructor.

    Attributes:
        region_id: Identity of the Content Region.
        revision_id: Identity of the resolved Document Revision (the
            ``Region_Occurrences.document_revision_id`` for this
            occurrence).
        start_offset_bytes: Inclusive byte offset of the span's start.
        end_offset_bytes: Exclusive byte offset of the span's end.
        span_byte_length: ``end_offset_bytes - start_offset_bytes``.
        span_content_digest_sha256: Lowercase-hex SHA-256 of
            ``bounded_text``, equal by construction to the persisted
            ``Region_Occurrences.span_content_digest_sha256``.
        bounded_text: The raw bytes of the span, byte-equivalent to
            ``Document_Revisions.content_bytes[start:end]``.
    """

    region_id: str
    revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    bounded_text: bytes


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join an originating-write audit row to any
    downstream audit row produced for the same logical operation. They are
    not registered with the :class:`IdentityService` because they do not
    name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content_bytes: bytes) -> str:
    """Compute the lowercase-hex SHA-256 digest of ``content_bytes``."""
    return hashlib.sha256(content_bytes).hexdigest()


@dataclass
class EvidenceRepository:
    """Persists Source Documents and immutable Document Revisions.

    The service is connection-scoped at call time: each public method
    accepts the caller's :class:`~sqlalchemy.engine.Connection` and writes
    inside the caller's transaction (AD-WS-5). Instances therefore hold
    only the cross-request collaborators (:class:`Clock`,
    :class:`IdentityService`, :class:`AuditLog`) and can be shared across
    requests.

    Args:
        clock: Source of recorded timestamps for both the Document Revision
            row and the consequential audit row. The clock is consulted
            exactly once per write so every artifact of the transaction
            shares one timestamp (design §"Cross-Cutting Concerns",
            *Transactionality*).
        identity_service: Generates Resource and Revision identifiers and
            persists their bindings to ``Identifier_Registry``. The
            persistent path (with ``connection=`` supplied) is used so
            non-reuse is enforced at the database level (Requirement 1.6).
        audit_log: Appends the ``'consequential'`` audit row inside the
            caller's transaction. Failures propagate as
            :class:`walking_slice.audit.AuditAppendError`; the caller's
            transaction context manager rolls back automatically
            (Requirement 2.7).
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog

    # -- public surface ----------------------------------------------------

    def create_document(
        self,
        connection: Connection,
        *,
        content_bytes: bytes,
        contributing_party_id: str,
        authority: str,
        external_identifier: Optional[str] = None,
        source_system_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        current_location: Optional[str] = None,
    ) -> CreateDocumentResult:
        """Create a Source Document and its first Document Revision.

        See module docstring for the validation rules and persistence
        ordering. The five rows touched by this method (Source_Documents,
        Document_Revisions, two Identifier_Registry rows, Audit_Records)
        all participate in the caller's transaction; the caller is
        responsible for committing via ``engine.begin()`` / explicit
        ``Transaction.commit()``.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            content_bytes: The raw bytes of the document, 1..100 MB.
            contributing_party_id: Identity of the Party submitting the
                document. Must reference an existing ``Parties`` row;
                the FK is enforced by the database.
            authority: Authority designation from :data:`AUTHORITY_ENUM`.
            external_identifier: Optional external system identifier
                (1..256 chars when present, per Requirement 2.3).
            source_system_id: Optional source system identifier
                (1..128 chars when present, per Requirement 2.3).
            correlation_id: Correlation identifier shared by every row
                written in this transaction. A UUIDv7 is generated when
                omitted.
            current_location: Optional initial display path; may be
                changed later by :meth:`rename_document` (task 5.3)
                without changing the Resource Identity.

        Returns:
            :class:`CreateDocumentResult` with the issued Resource and
            Revision identifiers, content digest, and recorded time.

        Raises:
            InvalidContentError: Per Requirement 2.6.
            walking_slice.audit.AuditAppendError: If the consequential
                audit append fails. The surrounding transaction MUST be
                allowed to roll back per Requirement 2.7.
            walking_slice.identity.IdentityConflictError: If a generated
                identifier is already bound to different content; the
                identity service appends an Identifier-conflict denial
                in a separate transaction (see
                :meth:`IdentityService.reject_if_duplicate`).
        """
        self._validate_content_bytes(content_bytes)
        self._validate_contributing_party(contributing_party_id)
        self._validate_authority(authority)
        self._validate_external_identifier(external_identifier)
        self._validate_source_system_id(source_system_id)

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        content_digest = _sha256_hex(content_bytes)
        correlation = correlation_id or _new_correlation_id()

        resource_id = str(self.identity_service.new_resource_id())
        revision_id = str(self.identity_service.new_revision_id())

        # Register both identifiers in Identifier_Registry inside the
        # caller's transaction (AD-WS-5). The Resource is bound to the
        # first Revision's digest so a re-issue collision (vanishingly
        # rare for UUIDv7 within a single instance) is rejected as an
        # identifier-conflict per Requirement 1.4.
        self.identity_service.reject_if_duplicate(
            resource_id,
            content_digest,
            connection=connection,
            kind="resource",
            actor_party_id=contributing_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_REVISION,
            recorded_time=recorded_time,
        )
        self.identity_service.reject_if_duplicate(
            revision_id,
            content_digest,
            connection=connection,
            kind="revision",
            actor_party_id=contributing_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_REVISION,
            recorded_time=recorded_time,
        )

        connection.execute(
            text(
                """
                INSERT INTO Source_Documents (
                    resource_id, current_location, external_identifier,
                    source_system_id, authority, created_at
                ) VALUES (
                    :resource_id, :current_location, :external_identifier,
                    :source_system_id, :authority, :created_at
                )
                """
            ),
            {
                "resource_id": resource_id,
                "current_location": current_location,
                "external_identifier": external_identifier,
                "source_system_id": source_system_id,
                "authority": authority,
                "created_at": recorded_at,
            },
        )

        connection.execute(
            text(
                """
                INSERT INTO Document_Revisions (
                    revision_id, resource_id, parent_revision_id,
                    content_bytes, content_digest_sha256,
                    contributing_party_id, recorded_at, change_description
                ) VALUES (
                    :revision_id, :resource_id, NULL,
                    :content_bytes, :content_digest_sha256,
                    :contributing_party_id, :recorded_at, NULL
                )
                """
            ),
            {
                "revision_id": revision_id,
                "resource_id": resource_id,
                "content_bytes": content_bytes,
                "content_digest_sha256": content_digest,
                "contributing_party_id": contributing_party_id,
                "recorded_at": recorded_at,
            },
        )

        # Audit append participates in the caller's transaction so a
        # failure here causes the Source_Documents and Document_Revisions
        # rows to roll back as well (Requirement 2.7).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=contributing_party_id,
            action_type=_AUDIT_ACTION_CREATE_REVISION,
            target_id=resource_id,
            target_revision_id=revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateDocumentResult(
            resource_id=resource_id,
            revision_id=revision_id,
            content_digest_sha256=content_digest,
            recorded_at=recorded_at,
        )

    def append_revision(
        self,
        connection: Connection,
        *,
        resource_id: str,
        content_bytes: bytes,
        contributing_party_id: str,
        change_description: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> AppendRevisionResult:
        """Append a new immutable Document Revision to an existing Source Document.

        The new Revision's ``parent_revision_id`` is set to the most recent
        prior Revision of ``resource_id`` (ordered by ``recorded_at`` and
        then by ``revision_id`` for deterministic tie-breaking on
        millisecond-equal timestamps).

        See :meth:`create_document` for the validation rules and
        transaction participation contract.

        Raises:
            InvalidContentError: Per Requirement 2.6.
            RevisionNotFoundError: If no prior Document Revision exists
                for ``resource_id`` (the Source Document must be created
                via :meth:`create_document` first).
            walking_slice.audit.AuditAppendError: If the audit append
                fails (Requirement 2.7).
        """
        self._validate_content_bytes(content_bytes)
        self._validate_contributing_party(contributing_party_id)

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        content_digest = _sha256_hex(content_bytes)
        correlation = correlation_id or _new_correlation_id()

        parent_revision_id = self._latest_revision_id(connection, resource_id)
        if parent_revision_id is None:
            # No prior Revision means the Source Document does not exist
            # or has no Revisions; in this slice both states are reported
            # via the same indicator since a Source Document without a
            # first Revision is unreachable through the public API.
            raise RevisionNotFoundError(
                resource_id=resource_id, revision_id="<latest>"
            )

        revision_id = str(self.identity_service.new_revision_id())
        self.identity_service.reject_if_duplicate(
            revision_id,
            content_digest,
            connection=connection,
            kind="revision",
            actor_party_id=contributing_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_REVISION,
            recorded_time=recorded_time,
        )

        connection.execute(
            text(
                """
                INSERT INTO Document_Revisions (
                    revision_id, resource_id, parent_revision_id,
                    content_bytes, content_digest_sha256,
                    contributing_party_id, recorded_at, change_description
                ) VALUES (
                    :revision_id, :resource_id, :parent_revision_id,
                    :content_bytes, :content_digest_sha256,
                    :contributing_party_id, :recorded_at, :change_description
                )
                """
            ),
            {
                "revision_id": revision_id,
                "resource_id": resource_id,
                "parent_revision_id": parent_revision_id,
                "content_bytes": content_bytes,
                "content_digest_sha256": content_digest,
                "contributing_party_id": contributing_party_id,
                "recorded_at": recorded_at,
                "change_description": change_description,
            },
        )

        self.audit_log.append_consequential(
            connection,
            actor_party_id=contributing_party_id,
            action_type=_AUDIT_ACTION_CREATE_REVISION,
            target_id=resource_id,
            target_revision_id=revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return AppendRevisionResult(
            resource_id=resource_id,
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            content_digest_sha256=content_digest,
            recorded_at=recorded_at,
        )

    def get_revision(
        self,
        connection: Connection,
        *,
        resource_id: str,
        revision_id: str,
    ) -> DocumentRevision:
        """Read a persisted Document Revision row.

        Looks up the row by composite key ``(resource_id, revision_id)``
        so a caller cannot accidentally read another Source Document's
        Revision by Revision Identity alone — the composite key check is
        defensive and never produces a row mismatch because Revision
        Identity is globally unique (AD-WS-2).

        Raises:
            RevisionNotFoundError: If no matching row exists.
        """
        row = (
            connection.execute(
                text(
                    """
                    SELECT
                        resource_id,
                        revision_id,
                        parent_revision_id,
                        content_bytes,
                        content_digest_sha256,
                        contributing_party_id,
                        recorded_at,
                        change_description
                    FROM Document_Revisions
                    WHERE resource_id = :resource_id
                      AND revision_id = :revision_id
                    """
                ),
                {"resource_id": resource_id, "revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RevisionNotFoundError(
                resource_id=resource_id, revision_id=revision_id
            )
        return DocumentRevision(
            resource_id=row["resource_id"],
            revision_id=row["revision_id"],
            parent_revision_id=row["parent_revision_id"],
            content_bytes=bytes(row["content_bytes"]),
            content_digest_sha256=row["content_digest_sha256"],
            contributing_party_id=row["contributing_party_id"],
            recorded_at=row["recorded_at"],
            change_description=row["change_description"],
        )

    def get_region_occurrence(
        self,
        connection: Connection,
        *,
        region_id: str,
        revision_id: str,
    ) -> RegionOccurrence:
        """Read a persisted Region Occurrence by composite key.

        Looks up the ``Region_Occurrences`` row identified by
        ``(region_id, document_revision_id)`` and returns it as a
        :class:`RegionOccurrence` value object. The method does not
        consult ``Document_Revisions`` — callers that need the
        resolved bytes must call :meth:`resolve_region_text` instead.

        Requirement 3.6 demands that Provenance_Navigator decline to
        return a bounded text span for an unresolvable Content Region
        reference. This method is the persistence-layer counterpart:
        every unresolvable shape (unknown ``region_id``, unknown
        ``revision_id``, known ``region_id`` paired with a
        ``revision_id`` that has no Occurrence for that Region)
        surfaces as :class:`RegionOccurrenceNotFoundError`. The
        Provenance_Navigator (task 12.4) and the HTTP layer (task
        12.3) consume the single exception type so the resulting
        error response does not leak which dimension was missing
        (Property 4, Requirement 8.5).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction (read-only here, but participating in the
                caller's isolation level).
            region_id: Region Identity to look up.
            revision_id: Document Revision Identity the Occurrence
                anchors against.

        Returns:
            :class:`RegionOccurrence` mirroring the persisted row.

        Raises:
            RegionOccurrenceNotFoundError: No ``Region_Occurrences``
                row matches the composite key.
        """
        row = (
            connection.execute(
                text(
                    """
                    SELECT
                        region_id,
                        document_revision_id,
                        start_offset_bytes,
                        end_offset_bytes,
                        span_byte_length,
                        span_content_digest_sha256,
                        recorded_at
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
        if row is None:
            raise RegionOccurrenceNotFoundError(
                region_id=region_id, revision_id=revision_id
            )
        return RegionOccurrence(
            region_id=row["region_id"],
            document_revision_id=row["document_revision_id"],
            start_offset_bytes=row["start_offset_bytes"],
            end_offset_bytes=row["end_offset_bytes"],
            span_byte_length=row["span_byte_length"],
            span_content_digest_sha256=row["span_content_digest_sha256"],
            recorded_at=row["recorded_at"],
        )

    def resolve_region_text(
        self,
        connection: Connection,
        *,
        region_id: str,
        revision_id: str,
    ) -> ResolvedSpan:
        """Resolve a Content Region reference to its bounded text span.

        Implements the Evidence_Repository half of Requirement 3.4:
        given a Content Region reference (``region_id``,
        ``revision_id``), return the exact bounded text span
        byte-equivalent to the span originally recorded for that
        Region Occurrence. Requirement 3.6 requires that an
        unresolvable reference decline to return a bounded text span;
        every unresolvable shape (unknown ``region_id``, unknown
        ``revision_id``, or a known ``region_id`` paired with a
        ``revision_id`` that has no Occurrence for that Region)
        surfaces as :class:`RegionOccurrenceNotFoundError`. The
        Provenance_Navigator (task 12.3 / 12.4) wraps this method
        with authority filtering and the AD-WS-9 Disclosure Policy;
        no authority check is performed here so unit tests can
        exercise the read path without standing up the
        Authorization_Service.

        The bounded text returned is derived from the immutable
        ``Document_Revisions.content_bytes`` blob and the persisted
        ``Region_Occurrences.start_offset_bytes`` /
        ``Region_Occurrences.end_offset_bytes`` offsets. The method
        recomputes SHA-256 over the resolved slice and asserts the
        digest equals the persisted
        ``Region_Occurrences.span_content_digest_sha256`` — AD-WS-4
        makes both rows insert-only via triggers so the equality is
        a database-level invariant. If the equality ever fails the
        method raises :class:`SpanDigestMismatchError` rather than
        returning a span the caller might mistakenly trust
        (Property 9, Requirement 11.2).

        Because the original Document Revision is immutable
        (AD-WS-4) and prior Region Occurrences are preserved across
        later Document Revisions (Requirement 3.3), this method
        returns the same bytes for the same ``(region_id,
        revision_id)`` pair on every call, regardless of how many
        later Document Revisions of the same Source Document have
        been appended.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction (read-only, but participating in the
                caller's isolation level).
            region_id: Region Identity to resolve.
            revision_id: Document Revision Identity to resolve the
                Occurrence against. The Occurrence and the Document
                Revision are both looked up here; callers do not need
                to supply the owning Source Document's
                ``resource_id`` because the
                ``Region_Occurrences`` table is keyed on
                ``(region_id, document_revision_id)`` and the
                ``Document_Revisions`` table is keyed on
                ``revision_id`` alone.

        Returns:
            :class:`ResolvedSpan` with the verified digest and the
            byte-equivalent ``bounded_text``.

        Raises:
            RegionOccurrenceNotFoundError: ``(region_id,
                revision_id)`` does not name an existing Region
                Occurrence, or the owning Document Revision could not
                be located (the latter should not happen given the
                schema's foreign-key constraint; treating it as
                "unresolvable" rather than raising a different error
                preserves the Requirement 3.6 contract that one
                error type identifies every unresolvable reference).
            SpanDigestMismatchError: The recomputed SHA-256 of the
                resolved bytes does not equal the persisted
                ``span_content_digest_sha256``. Indicates database
                corruption or an invariant violation; not recoverable
                by the caller.
        """
        occurrence = self.get_region_occurrence(
            connection,
            region_id=region_id,
            revision_id=revision_id,
        )

        # Resolve the owning Source Document by joining through
        # ``Content_Regions``. The ``Region_Occurrences`` row alone
        # does not carry ``resource_id`` because ``Document_Revisions``
        # already binds ``revision_id -> resource_id`` and the
        # ``Content_Regions`` row binds ``region_id ->
        # parent_resource_id``; either is sufficient. We use
        # ``Document_Revisions`` here because the slice's
        # :meth:`get_revision` is the canonical reader of immutable
        # Document Revision bytes (AD-WS-4) and exercising it keeps
        # the test surface symmetric with the rest of the module.
        resource_id_row = connection.execute(
            text(
                """
                SELECT resource_id
                FROM Document_Revisions
                WHERE revision_id = :revision_id
                """
            ),
            {"revision_id": revision_id},
        ).scalar_one_or_none()
        if resource_id_row is None:
            # Should be unreachable: the Region_Occurrences row above
            # carries a FK to Document_Revisions(revision_id) so the
            # parent row must exist. We surface this as an
            # unresolvable-reference error to keep the Requirement 3.6
            # contract intact (one exception type for every
            # unresolvable shape).
            raise RegionOccurrenceNotFoundError(
                region_id=region_id, revision_id=revision_id
            )

        revision = self.get_revision(
            connection,
            resource_id=resource_id_row,
            revision_id=revision_id,
        )

        start = occurrence.start_offset_bytes
        end = occurrence.end_offset_bytes
        bounded_text = bytes(revision.content_bytes[start:end])
        computed_digest = _sha256_hex(bounded_text)
        if computed_digest != occurrence.span_content_digest_sha256:
            # AD-WS-4 makes both Document Revisions and Region
            # Occurrences insert-only; a digest mismatch therefore
            # indicates database corruption rather than a recoverable
            # caller-side error. Refusing to return the span here is
            # the safer default (Property 9 demands byte-equivalence
            # against the recorded digest).
            raise SpanDigestMismatchError(
                region_id=region_id,
                revision_id=revision_id,
                recorded_digest=occurrence.span_content_digest_sha256,
                computed_digest=computed_digest,
            )

        return ResolvedSpan(
            region_id=region_id,
            revision_id=revision_id,
            start_offset_bytes=start,
            end_offset_bytes=end,
            span_byte_length=occurrence.span_byte_length,
            span_content_digest_sha256=occurrence.span_content_digest_sha256,
            bounded_text=bounded_text,
        )

    def rename_document(
        self,
        connection: Connection,
        *,
        resource_id: str,
        new_current_location: Optional[str],
        actor_party_id: str,
        correlation_id: Optional[str] = None,
    ) -> RenameDocumentResult:
        """Rename or relocate a Source Document, preserving Identity.

        Requirement 1.3: when an authorized actor renames or relocates a
        Source Document, every existing Resource Identity and Resource
        Revision Identity SHALL be preserved unchanged, and no new
        Resource Identity SHALL be generated. The mutable
        ``Source_Documents.current_location`` column is the only state
        that changes; ``Document_Revisions`` rows are immutable
        (AD-WS-4) and are never touched by this method.

        The operation persists two artifacts inside the caller's
        transaction (AD-WS-5):

        1. An ``UPDATE`` of ``Source_Documents.current_location`` for
           the given ``resource_id``. ``current_location`` is the only
           non-PK column on ``Source_Documents`` that the slice mutates
           after creation; the rest of the row (``authority``,
           ``external_identifier``, ``source_system_id``, ``created_at``)
           remains untouched. The ``Source_Documents`` table has no
           append-only trigger (per Principle 5.5 the display path is
           explicitly mutable), so this UPDATE is permitted.
        2. A consequential ``Audit_Records`` row with
           ``action_type='rename.document'`` (Requirement 13.1).
           ``target_id`` is the Resource Identity; ``target_revision_id``
           is ``NULL`` because a rename addresses the Source Document
           Resource, not any one Revision.

        Both writes share one timestamp obtained from the injected
        :class:`Clock` so the audit row's ``recorded_at`` matches the
        rename's effective time exactly.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            resource_id: Identity of the Source Document to rename. Must
                reference an existing row in ``Source_Documents``;
                otherwise :class:`SourceDocumentNotFoundError` is raised
                before any write occurs.
            new_current_location: The new display path. May be ``None``
                to clear the column; the schema allows ``NULL`` and the
                column has no length constraint in this slice (path
                length is enforced at the HTTP boundary in task 5.4 to
                avoid double-validation).
            actor_party_id: Identity of the Party performing the rename;
                recorded as the audit row's ``actor_party_id``. Empty
                strings are rejected with the same
                ``contributing_party_id_missing`` constraint name used
                elsewhere in this module so callers see one consistent
                error code for "missing acting Party".
            correlation_id: Optional correlation identifier shared by
                every audit row written in this transaction. A UUIDv7
                is generated when omitted.

        Returns:
            :class:`RenameDocumentResult` carrying the Resource Identity
            (unchanged), the new and previous ``current_location``
            values, and the recorded timestamp.

        Raises:
            SourceDocumentNotFoundError: ``resource_id`` does not name
                an existing Source Document.
            InvalidContentError: ``actor_party_id`` is empty.
            walking_slice.audit.AuditAppendError: If the consequential
                audit append fails. The surrounding transaction MUST be
                allowed to roll back per Requirement 2.7 / 13.6, which
                also reverts the ``UPDATE`` so the previous
                ``current_location`` survives intact.
        """
        # Validate the acting Party first — the same constraint name used
        # by create_document / create_region_occurrence so HTTP callers
        # see one consistent error code for "missing actor" across every
        # Evidence_Repository write (task 5.4 will surface this as a 400
        # with a stable failed_constraint).
        self._validate_contributing_party(actor_party_id)

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()

        # Read the pre-rename row inside the caller's transaction so the
        # ``previous_location`` echoed back to the caller is the value
        # the UPDATE is about to replace, even if a concurrent writer
        # had touched it (SQLite serializes writers, so the SELECT-then-
        # UPDATE here is race-free). Returning a row at all also proves
        # the Source Document exists; otherwise we raise before any
        # write happens.
        previous_row = (
            connection.execute(
                text(
                    "SELECT current_location FROM Source_Documents "
                    "WHERE resource_id = :resource_id"
                ),
                {"resource_id": resource_id},
            )
            .mappings()
            .one_or_none()
        )
        if previous_row is None:
            raise SourceDocumentNotFoundError(resource_id=resource_id)
        previous_location = previous_row["current_location"]

        # Mutate only ``current_location`` — every other column on
        # ``Source_Documents`` is untouched so the rename cannot change
        # ``authority`` or the immutable identifiers (Requirement 1.3,
        # design §"Table-by-Table Specification" — "Stable across
        # renames").
        connection.execute(
            text(
                """
                UPDATE Source_Documents
                SET current_location = :new_current_location
                WHERE resource_id = :resource_id
                """
            ),
            {
                "new_current_location": new_current_location,
                "resource_id": resource_id,
            },
        )

        # Audit append participates in the caller's transaction so a
        # failure here rolls back the UPDATE as well — Requirement 2.7
        # / 13.6 — and the Source Document's display path stays at its
        # previous value.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=actor_party_id,
            action_type=_AUDIT_ACTION_RENAME_DOCUMENT,
            target_id=resource_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return RenameDocumentResult(
            resource_id=resource_id,
            new_current_location=new_current_location,
            previous_location=previous_location,
            recorded_at=recorded_at,
        )

    def create_region_occurrence(
        self,
        connection: Connection,
        *,
        resource_id: str,
        revision_id: str,
        start_offset_bytes: int,
        end_offset_bytes: int,
        contributing_party_id: str,
        region_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> CreateRegionResult:
        """Create a Content Region Occurrence anchored to a Document Revision.

        Per design §"AD-WS-6 — Interim Content Region anchoring", a Region
        Occurrence stores ``start_offset_bytes``, ``end_offset_bytes``,
        ``span_byte_length``, and ``span_content_digest_sha256`` of the
        bounded UTF-8 byte span inside the owning Document Revision.
        Offsets are byte offsets — not codepoint or grapheme indices —
        because they are deterministic against the immutable
        ``content_bytes`` blob (Requirement 3.1, 3.2).

        Two call shapes are supported:

        - ``region_id`` omitted — generate a fresh Region Identity,
          insert a ``Content_Regions`` row, and insert the first
          ``Region_Occurrences`` row that anchors it to ``revision_id``.
          The new Region Identity is registered in
          ``Identifier_Registry`` with ``kind='region'`` so identifier
          non-reuse (AD-WS-2) is enforced at the database level.

        - ``region_id`` supplied — reuse an existing Region for a
          different Document Revision of the same Source Document
          (Requirement 3.3). The Region must already exist; otherwise
          :class:`RegionNotFoundError` is raised before any write. No
          new ``Content_Regions`` row is created and the
          ``Identifier_Registry`` is not re-checked because the Region
          was bound when it was originally created.

        Every successful call inserts a consequential ``Audit_Records``
        row with ``action_type='create.region_occurrence'`` inside the
        caller's transaction (AD-WS-5) and lazily seeds an
        ``Interim_ADR_Records`` row referencing AD-WS-6 / Gap G-1 /
        ``ADR-HT-003`` via ``INSERT OR IGNORE`` so multiple instances
        (and repeated calls within one instance) never produce
        duplicates (Requirement 16.3).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            resource_id: Identity of the Source Document the Region
                belongs to. Required by the ``Content_Regions`` schema
                (``parent_resource_id`` NOT NULL) when generating a new
                Region; on the reuse path it is still validated against
                the existing Region's ``parent_resource_id`` so a Region
                cannot be silently re-parented.
            revision_id: Identity of the Document Revision to anchor
                the Occurrence against. Must reference an existing
                ``Document_Revisions`` row whose ``resource_id``
                matches; otherwise :class:`RevisionNotFoundError`
                is raised.
            start_offset_bytes: Byte offset of the first byte of the
                span. Must be a non-negative integer.
            end_offset_bytes: Byte offset one past the last byte of the
                span. Must be a positive integer strictly greater than
                ``start_offset_bytes`` and at most
                ``len(content_bytes)``.
            contributing_party_id: Party recording the Occurrence;
                written to the consequential audit row's
                ``actor_party_id``. Empty values are rejected with the
                same ``contributing_party_id_missing`` constraint name
                used by :meth:`create_document` because Requirement
                2.6's "missing contributing Party identity" branch
                applies to every Evidence_Repository write.
            region_id: Optional Region Identity. Omit on first
                creation; supply on subsequent anchoring of an
                existing Region in another Revision.
            correlation_id: Operation correlation identifier shared by
                every audit row written in this transaction. A UUIDv7
                is generated when omitted.

        Returns:
            :class:`CreateRegionResult` carrying the Region Identity,
            revision Identity, validated offsets, span length, span
            digest, and recorded timestamp.

        Raises:
            InvalidSpanError: Per Requirement 3.5 — empty span, span
                extending beyond ``len(content_bytes)``, or
                non-strictly-increasing offsets.
            RevisionNotFoundError: ``(resource_id, revision_id)`` does
                not identify an existing Document Revision.
            RegionNotFoundError: ``region_id`` was supplied but does
                not name an existing Region (or names a Region whose
                ``parent_resource_id`` differs from ``resource_id``).
            InvalidContentError: ``contributing_party_id`` is empty.
            walking_slice.audit.AuditAppendError: If the consequential
                audit append fails. The surrounding transaction MUST
                be allowed to roll back per AD-WS-5.
            walking_slice.identity.IdentityConflictError: A freshly
                generated ``region_id`` (vanishingly rare for UUIDv7
                within a single instance) collides with an already
                bound identifier.
        """
        self._validate_contributing_party(contributing_party_id)
        self._validate_offset_shape(start_offset_bytes, end_offset_bytes)

        # Resolve the Document Revision first so we can validate the
        # span against the actual byte content. ``get_revision`` raises
        # RevisionNotFoundError if the row does not exist — exactly the
        # behavior demanded by Requirement 3.5's "extends outside the
        # bounded text of the target Document Revision" branch when the
        # target Revision itself is unknown.
        revision = self.get_revision(
            connection,
            resource_id=resource_id,
            revision_id=revision_id,
        )
        content_length = len(revision.content_bytes)
        if end_offset_bytes > content_length:
            raise InvalidSpanError(
                f"end_offset_bytes={end_offset_bytes} exceeds the Document "
                f"Revision content length of {content_length} bytes "
                f"(Requirement 3.5).",
                failed_constraint="end_offset_exceeds_content_length",
            )

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        span_bytes = bytes(
            revision.content_bytes[start_offset_bytes:end_offset_bytes]
        )
        span_byte_length = end_offset_bytes - start_offset_bytes
        span_content_digest_sha256 = _sha256_hex(span_bytes)

        # On the new-Region path generate the identifier, register it,
        # and insert the ``Content_Regions`` row. On the reuse path
        # validate the supplied identifier exists and points at the same
        # Source Document.
        if region_id is None:
            new_region_id = str(self.identity_service.new_region_id())
            self.identity_service.reject_if_duplicate(
                new_region_id,
                span_content_digest_sha256,
                connection=connection,
                kind="region",
                actor_party_id=contributing_party_id,
                correlation_id=correlation,
                attempted_action=_AUDIT_ACTION_CREATE_REGION_OCCURRENCE,
                recorded_time=recorded_time,
            )
            connection.execute(
                text(
                    """
                    INSERT INTO Content_Regions (
                        region_id, parent_resource_id, created_at
                    ) VALUES (
                        :region_id, :parent_resource_id, :created_at
                    )
                    """
                ),
                {
                    "region_id": new_region_id,
                    "parent_resource_id": resource_id,
                    "created_at": recorded_at,
                },
            )
            effective_region_id = new_region_id
        else:
            effective_region_id = self._resolve_existing_region(
                connection, region_id=region_id, resource_id=resource_id
            )

        # Insert the immutable Region Occurrence row. The schema enforces
        # the (region_id, document_revision_id) composite PK and the
        # CHECK constraints (end > start, span_byte_length == end - start);
        # the Python-side validation above keeps the database error path
        # purely defensive.
        connection.execute(
            text(
                """
                INSERT INTO Region_Occurrences (
                    region_id, document_revision_id, start_offset_bytes,
                    end_offset_bytes, span_byte_length,
                    span_content_digest_sha256, recorded_at
                ) VALUES (
                    :region_id, :document_revision_id, :start_offset_bytes,
                    :end_offset_bytes, :span_byte_length,
                    :span_content_digest_sha256, :recorded_at
                )
                """
            ),
            {
                "region_id": effective_region_id,
                "document_revision_id": revision_id,
                "start_offset_bytes": start_offset_bytes,
                "end_offset_bytes": end_offset_bytes,
                "span_byte_length": span_byte_length,
                "span_content_digest_sha256": span_content_digest_sha256,
                "recorded_at": recorded_at,
            },
        )

        # Lazy-seed the AD-WS-6 Interim_ADR_Records row inside the
        # caller's transaction. INSERT OR IGNORE keeps the call
        # idempotent across processes and repeated invocations — task
        # 13.1 will move this seed to startup but until then this lazy
        # seed satisfies Requirement 16.3 (the AD-WS-6 record is
        # retrievable by backlog ADR identifier the moment any Region
        # Occurrence has been created).
        self._seed_ad_ws_6(connection, recorded_at=recorded_at)

        # Audit append participates in the caller's transaction so a
        # failure rolls back the Content_Regions, Region_Occurrences,
        # Identifier_Registry, and Interim_ADR_Records writes together
        # (AD-WS-5, Requirement 13.6).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=contributing_party_id,
            action_type=_AUDIT_ACTION_CREATE_REGION_OCCURRENCE,
            target_id=effective_region_id,
            target_revision_id=revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateRegionResult(
            region_id=effective_region_id,
            revision_id=revision_id,
            start_offset_bytes=start_offset_bytes,
            end_offset_bytes=end_offset_bytes,
            span_byte_length=span_byte_length,
            span_content_digest_sha256=span_content_digest_sha256,
            recorded_at=recorded_at,
        )

    # -- internal helpers --------------------------------------------------

    def _latest_revision_id(
        self, connection: Connection, resource_id: str
    ) -> Optional[str]:
        """Return the most recent ``revision_id`` for ``resource_id`` or None.

        Order is ``recorded_at DESC, revision_id DESC`` so two revisions
        recorded at the same millisecond timestamp still produce a
        deterministic parent pointer (UUIDv7 sorting is monotonic with
        creation time, so the higher-sorting Revision is the more
        recently issued).
        """
        return connection.execute(
            text(
                """
                SELECT revision_id
                FROM Document_Revisions
                WHERE resource_id = :resource_id
                ORDER BY recorded_at DESC, revision_id DESC
                LIMIT 1
                """
            ),
            {"resource_id": resource_id},
        ).scalar_one_or_none()

    def _resolve_existing_region(
        self,
        connection: Connection,
        *,
        region_id: str,
        resource_id: str,
    ) -> str:
        """Verify ``region_id`` exists and is owned by ``resource_id``.

        On the reuse path of :meth:`create_region_occurrence` (Requirement
        3.3) the caller hands us an existing Region Identity. We refuse
        to anchor an Occurrence against a Region that does not exist
        (would create a dangling FK) or that belongs to a different
        Source Document (would silently re-parent the Region — a
        violation of AD-WS-3, where Region Identity is owned by exactly
        one Source Document Resource).
        """
        row = connection.execute(
            text(
                """
                SELECT parent_resource_id
                FROM Content_Regions
                WHERE region_id = :region_id
                """
            ),
            {"region_id": region_id},
        ).scalar_one_or_none()
        if row is None:
            raise RegionNotFoundError(region_id=region_id)
        if row != resource_id:
            # Treat a mismatched parent_resource_id as "no such Region for
            # this Source Document" so callers cannot probe for the
            # existence of a Region they should not see (Property 4).
            raise RegionNotFoundError(region_id=region_id)
        return region_id

    def _seed_ad_ws_6(
        self, connection: Connection, *, recorded_at: str
    ) -> None:
        """Lazy-seed the AD-WS-6 row in ``Interim_ADR_Records``.

        Uses ``INSERT OR IGNORE`` against the fixed primary key
        :data:`_AD_WS_6_RECORD_ID` so repeated invocations across
        instances and across calls within a single instance never
        produce duplicates. Task 13.1 will move this seeding to
        application startup once all five Gap-G rows are written; the
        lazy seed here closes the gap for tasks 5.2 → 13.0 so
        Requirement 16.3 holds the moment any Region Occurrence is
        created.
        """
        connection.execute(
            text(
                """
                INSERT OR IGNORE INTO Interim_ADR_Records (
                    record_id, motivating_requirement, motivating_criterion,
                    observable_behavior, recorded_at, backlog_adr_id
                ) VALUES (
                    :record_id, :motivating_requirement, :motivating_criterion,
                    :observable_behavior, :recorded_at, :backlog_adr_id
                )
                """
            ),
            {
                "record_id": _AD_WS_6_RECORD_ID,
                "motivating_requirement": _AD_WS_6_MOTIVATING_REQUIREMENT,
                "motivating_criterion": _AD_WS_6_MOTIVATING_CRITERION,
                "observable_behavior": _AD_WS_6_OBSERVABLE_BEHAVIOR,
                "recorded_at": recorded_at,
                "backlog_adr_id": _AD_WS_6_BACKLOG_ADR_ID,
            },
        )

    @staticmethod
    def _validate_offset_shape(
        start_offset_bytes: int, end_offset_bytes: int
    ) -> None:
        """Enforce Requirement 3.5 conditions that do not need the content.

        The byte content is consulted only after these checks pass; that
        keeps the rejection of obviously-malformed inputs cheap (no
        SELECT) and lets the more expensive
        ``end_offset_exceeds_content_length`` check fire only when the
        caller's offsets are at least structurally valid.

        Booleans are deliberately rejected even though Python's
        :func:`isinstance` reports ``True`` for ``isinstance(True, int)``
        — accepting them would let ``start_offset_bytes=True`` (which
        evaluates to ``1``) silently anchor a Region Occurrence, which
        is almost certainly a bug at the call site.
        """
        if isinstance(start_offset_bytes, bool) or not isinstance(
            start_offset_bytes, int
        ):
            raise InvalidSpanError(
                f"start_offset_bytes must be a non-negative int; received "
                f"{type(start_offset_bytes).__name__}.",
                failed_constraint="start_offset_not_integer",
            )
        if isinstance(end_offset_bytes, bool) or not isinstance(
            end_offset_bytes, int
        ):
            raise InvalidSpanError(
                f"end_offset_bytes must be a positive int; received "
                f"{type(end_offset_bytes).__name__}.",
                failed_constraint="end_offset_not_integer",
            )
        if start_offset_bytes < 0:
            raise InvalidSpanError(
                f"start_offset_bytes={start_offset_bytes} is negative; "
                f"Requirement 3.5 requires 0 <= start.",
                failed_constraint="start_offset_negative",
            )
        if start_offset_bytes >= end_offset_bytes:
            # Covers both ``start == end`` (empty span, explicitly named
            # in Requirement 3.5) and ``start > end`` (inverted, also
            # explicitly named).
            raise InvalidSpanError(
                f"start_offset_bytes={start_offset_bytes} must be strictly "
                f"less than end_offset_bytes={end_offset_bytes}; "
                f"Requirement 3.5 rejects empty and inverted spans.",
                failed_constraint="start_offset_not_less_than_end_offset",
            )

    @staticmethod
    def _validate_content_bytes(content_bytes: bytes) -> None:
        if not isinstance(content_bytes, (bytes, bytearray, memoryview)):
            raise InvalidContentError(
                f"content_bytes must be bytes-like; received "
                f"{type(content_bytes).__name__}.",
                failed_constraint="content_type_invalid",
            )
        length = len(content_bytes)
        if length == 0:
            raise InvalidContentError(
                "content_bytes is empty; Requirement 2.6 requires "
                "between 1 byte and 100 MB.",
                failed_constraint="content_empty",
            )
        if length > MAX_CONTENT_BYTES:
            raise InvalidContentError(
                f"content_bytes is {length} bytes; Requirement 2.6 caps "
                f"size at {MAX_CONTENT_BYTES} bytes (100 MB).",
                failed_constraint="content_too_large",
            )

    @staticmethod
    def _validate_contributing_party(contributing_party_id: Optional[str]) -> None:
        if not contributing_party_id:
            # Treat ``None`` and empty string identically; both fail
            # Requirement 2.6's "missing contributing Party identity" branch.
            raise InvalidContentError(
                "contributing_party_id is required; Requirement 2.6 rejects "
                "submissions without a contributing Party identity.",
                failed_constraint="contributing_party_id_missing",
            )

    @staticmethod
    def _validate_authority(authority: str) -> None:
        if authority not in AUTHORITY_ENUM:
            raise InvalidContentError(
                f"authority {authority!r} is not in the AD-WS-1 enumeration "
                f"{sorted(AUTHORITY_ENUM)!r}.",
                failed_constraint="authority_invalid",
            )

    @staticmethod
    def _validate_external_identifier(value: Optional[str]) -> None:
        if value is None:
            return
        if not (1 <= len(value) <= 256):
            raise InvalidContentError(
                "external_identifier must be 1..256 characters when supplied "
                "(Requirement 2.3).",
                failed_constraint="external_identifier_invalid",
            )

    @staticmethod
    def _validate_source_system_id(value: Optional[str]) -> None:
        if value is None:
            return
        if not (1 <= len(value) <= 128):
            raise InvalidContentError(
                "source_system_id must be 1..128 characters when supplied "
                "(Requirement 2.3).",
                failed_constraint="source_system_id_invalid",
            )
