"""Shared in-memory value objects for the first walking slice.

These DTOs cross module boundaries inside the modular monolith and are the
authoritative source for request/response shapes consumed by services. The
definitions track design §"In-Memory Value Objects" verbatim — adding a field
here without updating the design document is a spec violation.

Every model is a *frozen* Pydantic v2 :class:`BaseModel`. Frozenness is
required because these values participate in immutability invariants (design
§"Persistence Invariants Summary", Properties 11 and 12): once a service has
been handed a :class:`ResourceRef` or :class:`Span` the receiver MUST be able
to rely on its bytes not changing while the in-flight transaction completes.

Requirements satisfied (per task 1.2):
    2.5  — recorded-time discipline for audit appends (consumed by services
           that hand off :class:`AuthorityBasisRef` and :class:`ProvenanceNode`).
    4.2  — Relationship attribute set (``source_revision_id``,
           ``target_id``, ``relationship_type``, ``authoring_party_id``,
           ``recorded_at``) bridged through :class:`ResourceRef`/`FindingRef`.
    6.2  — Decision authority basis enumeration via :class:`AuthorityBasisRef`.
    12.1 — Authorization basis identifier shape used by the
           Authorization_Service surface.
    13.1 — Stable, auditable references between modules; immutable DTOs
           prevent post-audit mutation.
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


__all__ = [
    "ResourceRef",
    "FindingRef",
    "RegionOccurrenceRef",
    "Span",
    "AuthorityBasisRef",
    "ProvenanceNode",
    "GapDescriptor",
]


class _FrozenModel(BaseModel):
    """Common configuration for every value object in this module.

    ``frozen=True`` makes instances hashable and prevents field assignment.
    ``extra="forbid"`` rejects unknown attributes so call-sites that pass a
    typo'd field name fail loudly instead of silently dropping data.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ResourceRef(_FrozenModel):
    """Reference to a managed Resource and (optionally) one of its Revisions.

    ``kind`` is the resource-kind discriminator used at module boundaries
    (see design §"Logical Schema Overview" for the enumerated kinds: e.g.
    ``document``, ``region_occurrence``, ``finding``, ``recommendation``,
    ``decision``, ``trail``, ``trail_step``).

    ``revision_id`` is required when the referent is revisioned (Document
    Revision, Finding Revision, Recommendation Revision, Trail Revision) and
    omitted when the referent is itself an Immutable Record (Decision) or a
    Resource header (Source Document, Finding, Recommendation, Trail). The
    caller is responsible for honoring this contract; validation occurs in
    the service that resolves the reference.
    """

    kind: str = Field(min_length=1)
    resource_id: UUID
    revision_id: Optional[UUID] = None


class FindingRef(ResourceRef):
    """Convenience refinement of :class:`ResourceRef` for Finding targets.

    ``kind`` is pinned to ``"finding"`` so a Finding reference is statically
    distinguishable from a generic :class:`ResourceRef` at type-check time
    and in JSON payloads. ``revision_id`` remains optional because some
    Finding references address the Resource header (e.g. ``Contradicts``
    Relationships per design §"Knowledge_Service") while others address an
    exact Finding Revision (e.g. ``Derived From`` per Requirement 5.1).
    """

    kind: Literal["finding"] = "finding"


class RegionOccurrenceRef(_FrozenModel):
    """Reference to a Content Region Occurrence inside a Document Revision.

    Pairs the durable Region Identity (stable across Document Revisions per
    Requirement 3.3) with the owning Document Revision Identity so the
    receiver can resolve the exact byte span without consulting the
    Region-occurrence index.
    """

    region_id: UUID
    document_revision_id: UUID


class Span(_FrozenModel):
    """Resolved byte-equivalent span returned by ``Provenance_Navigator``.

    Carries both the offsets and the *resolved bounded text* so callers do
    not need a second round-trip to verify digest equality. The interim
    byte-offset anchoring choice (AD-WS-6) is reflected by the byte-typed
    ``bounded_text`` and the ``content_digest_sha256`` computed over
    ``bounded_text``.

    Attributes:
        start_offset_bytes: Inclusive byte offset of the span's start.
        end_offset_bytes: Exclusive byte offset of the span's end.
        bounded_text: The raw UTF-8 bytes of the span, byte-equivalent to
            ``Document_Revisions.content[start:end]`` (Requirement 11.2).
        content_digest_sha256: Lowercase-hex SHA-256 digest of
            ``bounded_text`` (Requirement 3.2, Property 9).
        document_revision_id: Identity of the Document Revision that owns
            the span; required so the receiver can resolve back to the
            Source Document per Requirement 11.1.
    """

    start_offset_bytes: int = Field(ge=0)
    end_offset_bytes: int = Field(ge=0)
    bounded_text: bytes
    content_digest_sha256: str = Field(min_length=64, max_length=64)
    document_revision_id: UUID


class AuthorityBasisRef(_FrozenModel):
    """The authority basis recorded on a Decision or denial Audit Record.

    The ``type`` enumeration tracks AD-WS-10 (closing Gap G-5). The slice
    accepts exactly the three values listed; widening this enumeration
    requires updating both the design document and the
    ``Authority_Bases`` lookup table.
    """

    type: Literal["role-grant-id", "scope-id", "delegation-chain-id"]
    id: UUID


class ProvenanceNode(_FrozenModel):
    """A single node in an omission-aware provenance response.

    Returned by ``Provenance_Navigator`` for backlink, Decision-provenance,
    Finding-provenance, Recommendation-provenance, and Trail-provenance
    queries. ``attributes`` carries only the fields the requesting Party is
    authorized to view (design §"Provenance_Navigator", Property 4 —
    *Non-leakage of restricted information*); when the node is restricted
    for that Party, ``redacted`` is set to ``True``, ``resource_id`` and
    ``revision_id`` are omitted, and ``attributes`` is empty (per AD-WS-9).
    """

    kind: str = Field(min_length=1)
    resource_id: Optional[UUID] = None
    revision_id: Optional[UUID] = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    redacted: bool = False


class GapDescriptor(_FrozenModel):
    """Omission descriptor returned in place of a node that cannot be shown.

    Produced by ``Provenance_Navigator`` when a node is unavailable, stale,
    unresolved, or restricted (per Requirement 10.5 and AD-WS-9). The
    descriptor reports the affected pipeline ``stage`` and the omission
    ``category``; when the next reachable node is visible to the requesting
    Party, ``next_reachable`` carries its :class:`ResourceRef`.
    """

    stage: str = Field(min_length=1)
    category: Literal["unavailable", "restricted", "stale", "unresolved"]
    next_reachable: Optional[ResourceRef] = None
