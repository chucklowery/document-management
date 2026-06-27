"""Shared in-memory value objects for the Slice 3 Deliverable_Repository.

These DTOs cross module boundaries inside the modular monolith and are the
authoritative source for request / response shapes consumed by the
Deliverable_Repository service. The definitions track
``.kiro/specs/third-walking-slice/design.md`` §"In-Memory Value Objects"
verbatim — adding a field here without updating the design document is a
spec violation.

Every model is a *frozen* Pydantic v2 :class:`BaseModel` so that once a
service has been handed a reference the receiver can rely on its bytes not
changing while the in-flight transaction completes (mirroring the Slice 1
:mod:`walking_slice.models` and Slice 2 :mod:`walking_slice.planning.models`
conventions). ``extra="forbid"`` rejects unknown attributes so typo'd field
names fail loudly instead of silently dropping data.

Reuse contract (task 3.2):
    ``AuthorityBasisRef``, ``TargetRef``, ``ProvenanceNode``,
    ``GapDescriptor``, ``ProjectionEnvelope``, and ``Clock`` are reused
    unchanged from existing Slice 1 / Slice 2 modules
    (:mod:`walking_slice.models`, :mod:`walking_slice.authorization`,
    :mod:`walking_slice.projection`, :mod:`walking_slice.clock`). None of
    those types is redefined here.

Requirements satisfied (per task 3.2):
    22.1 — produced Deliverable Resource and Revision identifiers carry the
           canonical UUIDv7 strings minted by the existing Identity_Service.
           The frozen :class:`DeliverableRef` and :class:`DeliverableRevisionRef`
           carry the UUID values typed as :class:`uuid.UUID` so receivers can
           round-trip them to the ``Identifier_Registry`` without re-parsing.
    22.2 — produced Deliverable Resource Identity and produced Deliverable
           Revision Identity are held as two distinct values:
           :class:`DeliverableRef` carries the Resource Identity alone, and
           :class:`DeliverableRevisionRef` carries the Resource Identity
           plus the Revision Identity as two separate fields (one Resource
           to many Revisions; the parent ``deliverable_id`` is repeated on
           the Revision reference so receivers do not need a second
           round-trip to look up the owning Resource).
    26.2 — every produced Deliverable Revision reference carries the
           Slice-3-specific columns mandated by Requirement 26.2 / design
           §"Persistence Invariants Summary" rule 9: ``content_digest_sha256``
           (64-character lowercase hex), ``role_marker`` pinned to the
           literal ``"generated_output"``, and ``originating_work_assignment_id``
           (the Slice 3 Work Assignment Record Identity under whose authority
           the Revision was authored).
    41.13 — produced-Deliverable vs Source-Evidence disjointness: the
           ``role_marker`` field's :class:`typing.Literal` type fixes the
           sole admissible value to ``"generated_output"``. Combined with
           the Slice 1 :class:`walking_slice.models.ResourceRef` carrying
           no ``role_marker`` field, the two reference shapes are
           statically distinguishable at type-check time and at JSON-parse
           time, mirroring the schema-level discriminator in
           :mod:`walking_slice.deliverables._persistence`.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


__all__ = [
    "DeliverableRef",
    "DeliverableRevisionRef",
]


class _FrozenModel(BaseModel):
    """Common configuration for every Slice 3 Deliverable_Repository value object.

    ``frozen=True`` makes instances hashable and prevents field assignment so
    a Revision reference handed across module boundaries cannot be mutated
    after the receiver has begun acting on it (Requirement 26.4 — produced
    Deliverable Revision immutability extends into the in-memory contract,
    not only the on-disk row).

    ``extra="forbid"`` rejects unknown attributes so call-sites that pass a
    typo'd field name (for example ``deliverable_revison_id``) fail loudly
    instead of silently dropping data. This mirrors the
    :class:`walking_slice.models._FrozenModel` and
    :class:`walking_slice.planning.models._FrozenModel` conventions
    established by Slices 1 and 2.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class DeliverableRef(_FrozenModel):
    """Reference to a produced Deliverable Resource by its durable Identity.

    The receiver resolves ``deliverable_id`` against the
    ``Deliverable_Resources`` table created by
    :func:`walking_slice.deliverables._persistence.create_deliverable_schema`.
    The reference is Resource-scoped: produced Deliverable Revision Identity
    is conveyed separately by :class:`DeliverableRevisionRef`, in keeping
    with Requirement 22.2 — produced Deliverable Resource Identity and
    produced Deliverable Revision Identity are held as two distinct values
    (one Resource to many Revisions per AD-WS-27).

    Per Requirement 22.3 the produced Deliverable Resource Identity survives
    rename and relocation; this reference therefore carries only the durable
    Identity and no transient name / location fields. Receivers that need
    the produced-Deliverable name read it from the persisted
    ``Deliverable_Resources`` row, where the Slice 3 AD-WS-27 UPDATE
    rejection trigger guarantees byte-equivalence.
    """

    deliverable_id: UUID


class DeliverableRevisionRef(_FrozenModel):
    """Reference to a produced Deliverable Revision and its produced Deliverable.

    Pairs the durable produced Deliverable Resource Identity (Requirement
    22.1 / 22.2) with the produced Deliverable Revision Identity so the
    receiver can resolve the exact Revision row without an additional
    round-trip to the parent Resource. The four trailing fields mirror the
    Revision-row columns mandated by Requirement 26.2 / design
    §"Persistence Invariants Summary" rule 9 so that callers needing the
    digest, the role marker, or the originating Work Assignment Record
    Identity do not have to re-query the row.

    Attributes:
        deliverable_id: Identity of the owning produced Deliverable Resource;
            equal to ``Deliverable_Revisions.deliverable_id`` for the
            Revision row referenced by ``deliverable_revision_id``.
        deliverable_revision_id: Identity of the produced Deliverable
            Revision row in ``Deliverable_Revisions``.
        content_digest_sha256: Lowercase-hex SHA-256 digest of the
            Revision's full byte content; byte-equivalent to the persisted
            ``Deliverable_Revisions.content_digest_sha256`` value, which the
            schema's length CHECK constraint fixes at exactly 64 hex
            characters (Slice 1 Requirement 2.2's SHA-256 digest length
            applied to produced Deliverables per Requirement 26.2).
        role_marker: Fixed literal ``"generated_output"`` — the
            schema-level discriminator that distinguishes a produced
            Deliverable Revision from a Slice 1 Source Evidence Document
            Revision (Requirement 26.2, Requirement 41 §13, design
            §"Persistence Invariants Summary" rule 9).
        originating_work_assignment_id: Identity of the Slice 3 Work
            Assignment Record under whose authority the Revision was
            authored (Requirement 26.2). The corresponding FK on
            ``Deliverable_Revisions.originating_work_assignment_id`` is
            enforced at INSERT time per AD-WS-1 once ``PRAGMA
            foreign_keys=ON`` is in effect.
    """

    deliverable_id: UUID
    deliverable_revision_id: UUID
    content_digest_sha256: str = Field(min_length=64, max_length=64)
    role_marker: Literal["generated_output"]
    originating_work_assignment_id: UUID
