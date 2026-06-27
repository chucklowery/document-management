"""Unit tests for :mod:`walking_slice.models`.

These tests pin the frozen, additive contract established in
design §"In-Memory Value Objects": every value object MUST be immutable,
hashable on its fields, and reject unknown attributes. Subsequent service
modules (Identity_Service, Evidence_Repository, Knowledge_Service, etc.)
depend on these guarantees when they thread DTOs through transactional
boundaries.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from walking_slice.models import (
    AuthorityBasisRef,
    FindingRef,
    GapDescriptor,
    ProvenanceNode,
    RegionOccurrenceRef,
    ResourceRef,
    Span,
)


pytestmark = pytest.mark.unit


RESOURCE_ID = UUID("01890000-0000-7000-8000-000000000001")
REVISION_ID = UUID("01890000-0000-7000-8000-000000000002")
REGION_ID = UUID("01890000-0000-7000-8000-000000000003")
ROLE_GRANT_ID = UUID("01890000-0000-7000-8000-000000000004")


# ---------------------------------------------------------------------------
# ResourceRef
# ---------------------------------------------------------------------------


def test_resource_ref_accepts_revisionless_reference() -> None:
    ref = ResourceRef(kind="document", resource_id=RESOURCE_ID)
    assert ref.revision_id is None
    assert ref.kind == "document"


def test_resource_ref_accepts_revisioned_reference() -> None:
    ref = ResourceRef(
        kind="document_revision",
        resource_id=RESOURCE_ID,
        revision_id=REVISION_ID,
    )
    assert ref.revision_id == REVISION_ID


def test_resource_ref_rejects_empty_kind() -> None:
    with pytest.raises(ValidationError):
        ResourceRef(kind="", resource_id=RESOURCE_ID)


def test_resource_ref_is_frozen() -> None:
    ref = ResourceRef(kind="document", resource_id=RESOURCE_ID)
    with pytest.raises(ValidationError):
        ref.kind = "tampered"  # type: ignore[misc]


def test_resource_ref_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ResourceRef(kind="document", resource_id=RESOURCE_ID, extra="nope")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# FindingRef
# ---------------------------------------------------------------------------


def test_finding_ref_pins_kind_to_finding() -> None:
    ref = FindingRef(resource_id=RESOURCE_ID, revision_id=REVISION_ID)
    assert ref.kind == "finding"
    assert isinstance(ref, ResourceRef)


def test_finding_ref_rejects_alternative_kind() -> None:
    with pytest.raises(ValidationError):
        FindingRef(kind="document", resource_id=RESOURCE_ID)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RegionOccurrenceRef
# ---------------------------------------------------------------------------


def test_region_occurrence_ref_requires_both_identities() -> None:
    ref = RegionOccurrenceRef(region_id=REGION_ID, document_revision_id=REVISION_ID)
    assert ref.region_id == REGION_ID
    assert ref.document_revision_id == REVISION_ID


def test_region_occurrence_ref_is_frozen() -> None:
    ref = RegionOccurrenceRef(region_id=REGION_ID, document_revision_id=REVISION_ID)
    with pytest.raises(ValidationError):
        ref.region_id = RESOURCE_ID  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Span
# ---------------------------------------------------------------------------


def _make_span(**overrides: object) -> Span:
    defaults: dict[str, object] = {
        "start_offset_bytes": 0,
        "end_offset_bytes": 5,
        "bounded_text": b"hello",
        "content_digest_sha256": "a" * 64,
        "document_revision_id": REVISION_ID,
    }
    defaults.update(overrides)
    return Span(**defaults)  # type: ignore[arg-type]


def test_span_accepts_valid_inputs() -> None:
    span = _make_span()
    assert span.bounded_text == b"hello"
    assert span.start_offset_bytes == 0
    assert span.end_offset_bytes == 5


def test_span_rejects_negative_offsets() -> None:
    with pytest.raises(ValidationError):
        _make_span(start_offset_bytes=-1)
    with pytest.raises(ValidationError):
        _make_span(end_offset_bytes=-1)


def test_span_rejects_non_64_char_digest() -> None:
    with pytest.raises(ValidationError):
        _make_span(content_digest_sha256="a" * 63)
    with pytest.raises(ValidationError):
        _make_span(content_digest_sha256="a" * 65)


def test_span_is_frozen() -> None:
    span = _make_span()
    with pytest.raises(ValidationError):
        span.bounded_text = b"world"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AuthorityBasisRef
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "basis_type",
    ["role-grant-id", "scope-id", "delegation-chain-id"],
)
def test_authority_basis_accepts_enumerated_types(basis_type: str) -> None:
    ref = AuthorityBasisRef(type=basis_type, id=ROLE_GRANT_ID)  # type: ignore[arg-type]
    assert ref.type == basis_type
    assert ref.id == ROLE_GRANT_ID


def test_authority_basis_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        AuthorityBasisRef(type="party-id", id=ROLE_GRANT_ID)  # type: ignore[arg-type]


def test_authority_basis_is_frozen() -> None:
    ref = AuthorityBasisRef(type="role-grant-id", id=ROLE_GRANT_ID)
    with pytest.raises(ValidationError):
        ref.type = "scope-id"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProvenanceNode
# ---------------------------------------------------------------------------


def test_provenance_node_defaults_are_visible() -> None:
    node = ProvenanceNode(kind="finding")
    assert node.resource_id is None
    assert node.revision_id is None
    assert node.attributes == {}
    assert node.redacted is False


def test_provenance_node_can_be_marked_redacted() -> None:
    node = ProvenanceNode(kind="finding", redacted=True)
    assert node.redacted is True
    assert node.resource_id is None
    assert node.attributes == {}


def test_provenance_node_carries_authorized_attributes() -> None:
    attrs = {"statement": "X is true", "is_hypothesis": False}
    node = ProvenanceNode(
        kind="finding",
        resource_id=RESOURCE_ID,
        revision_id=REVISION_ID,
        attributes=attrs,
    )
    assert node.attributes == attrs


def test_provenance_node_is_frozen() -> None:
    node = ProvenanceNode(kind="finding")
    with pytest.raises(ValidationError):
        node.redacted = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# GapDescriptor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    ["unavailable", "restricted", "stale", "unresolved"],
)
def test_gap_descriptor_accepts_enumerated_categories(category: str) -> None:
    descriptor = GapDescriptor(stage="finding", category=category)  # type: ignore[arg-type]
    assert descriptor.category == category
    assert descriptor.next_reachable is None


def test_gap_descriptor_carries_next_reachable() -> None:
    next_node = ResourceRef(kind="recommendation", resource_id=RESOURCE_ID)
    descriptor = GapDescriptor(
        stage="finding",
        category="unresolved",
        next_reachable=next_node,
    )
    assert descriptor.next_reachable == next_node


def test_gap_descriptor_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        GapDescriptor(stage="finding", category="intentional")  # type: ignore[arg-type]


def test_gap_descriptor_is_frozen() -> None:
    descriptor = GapDescriptor(stage="finding", category="unavailable")
    with pytest.raises(ValidationError):
        descriptor.category = "restricted"  # type: ignore[misc]
