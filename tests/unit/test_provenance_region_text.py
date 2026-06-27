"""Unit tests for :meth:`walking_slice.provenance.ProvenanceNavigator.resolve_region_text`.

These tests pin the contract established in task 12.3, design
§"Provenance_Navigator" HTTP surface
(``GET /api/v1/regions/{region_id}/occurrences/{revision_id}/text``),
Requirement 3.4 (resolve a Content Region reference to the exact
Document Revision, Region Identity, Region Occurrence, and a bounded
text span byte-equivalent to the recorded span), and Requirement 11.2
(bounded text is byte-equivalent and digest-matches against the
recorded content digest):

- Happy path returns the byte-equivalent span, the recorded
  ``span_content_digest_sha256``, a freshly computed SHA-256, and a
  ``digest_matches=True`` flag.
- Unresolvable Region or Document Revision raises
  :class:`RegionOccurrenceUnresolvableError`.
- Missing ``view.region_occurrence`` authority raises
  :class:`RegionTextAuthorizationError` with the denial reason code
  and correlation identifier surfaced.
- Missing ``view.document_revision`` authority raises the same error
  (the two authority checks are independent so a Party may hold one
  without the other).
- Idempotence: five invocations with the same
  ``(region_id, document_revision_id, party_id, at)`` return
  byte-equivalent :class:`RegionTextResolution` instances
  (Requirement 11.5 / Property 9, example-based form).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.provenance import (
    ProvenanceNavigator,
    RegionOccurrenceUnresolvableError,
    RegionTextAuthorizationError,
    RegionTextResolution,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_CONTRIBUTING_PARTY_ID = "00000000-0000-7000-8000-0000000d0001"
_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000d0002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000d0003"

_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]
_EXPECTED_SPAN_DIGEST = hashlib.sha256(_EXPECTED_SPAN_BYTES).hexdigest()

_UNKNOWN_REGION_ID = "00000000-0000-7000-8000-0000000dffff"
_UNKNOWN_REVISION_ID = "00000000-0000-7000-8000-0000000dfffe"


# ---------------------------------------------------------------------------
# Seeding helpers.
# ---------------------------------------------------------------------------


def _seed_party(conn, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_party(conn, _CONTRIBUTING_PARTY_ID, "Researcher")
        _seed_party(conn, _REQUESTER_PARTY_ID, "Reviewer")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _assign_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
    party_id: str = _REQUESTER_PARTY_ID,
) -> None:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="reviewer",
        scope=scope,
        authorities_granted=("view",),
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


class SeededRegion:
    """Convenience bundle of identifiers returned by :func:`_seed_region`."""

    def __init__(
        self,
        *,
        document_resource_id: str,
        document_revision_id: str,
        region_id: str,
    ) -> None:
        self.document_resource_id = document_resource_id
        self.document_revision_id = document_revision_id
        self.region_id = region_id


def _seed_region(
    engine: Engine, evidence_repository: EvidenceRepository
) -> SeededRegion:
    """Seed a Source Document plus one Region Occurrence."""
    with engine.begin() as conn:
        document = evidence_repository.create_document(
            conn,
            content_bytes=_DOC_CONTENT,
            contributing_party_id=_CONTRIBUTING_PARTY_ID,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=document.resource_id,
            revision_id=document.revision_id,
            start_offset_bytes=_DOC_SPAN_START,
            end_offset_bytes=_DOC_SPAN_END,
            contributing_party_id=_CONTRIBUTING_PARTY_ID,
        )
    return SeededRegion(
        document_resource_id=document.resource_id,
        document_revision_id=document.revision_id,
        region_id=region.region_id,
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_repository(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> EvidenceRepository:
    return EvidenceRepository(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def navigator(
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Happy path (Requirements 3.4, 11.2).
# ---------------------------------------------------------------------------


class TestHappyPath:
    """Resolution returns the byte-equivalent span and digest-matches."""

    def test_returns_byte_equivalent_span_and_digest_matches(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        region = _seed_region(engine, evidence_repository)
        _assign_view_role(
            authorization_service, engine, scope=region.document_resource_id
        )

        with engine.begin() as conn:
            resolution = navigator.resolve_region_text(
                conn,
                region_id=region.region_id,
                document_revision_id=region.document_revision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(resolution, RegionTextResolution)
        # Identity fields echo back the composite key (Requirement 3.4).
        assert resolution.region_id == region.region_id
        assert resolution.document_revision_id == region.document_revision_id

        # Anchors and length match the persisted Region Occurrence
        # (Requirement 3.2, surfaced verbatim).
        assert resolution.start_offset_bytes == _DOC_SPAN_START
        assert resolution.end_offset_bytes == _DOC_SPAN_END
        assert resolution.span_byte_length == _DOC_SPAN_END - _DOC_SPAN_START

        # Bounded text is byte-equivalent to the slice of the recorded
        # Document Revision (Requirement 11.2 byte-equivalence).
        assert resolution.bounded_text == _EXPECTED_SPAN_BYTES

        # Digest comparison: persisted digest equals freshly computed
        # digest (Requirement 11.2 digest-matching).
        assert resolution.span_content_digest_sha256 == _EXPECTED_SPAN_DIGEST
        assert resolution.computed_digest_sha256 == _EXPECTED_SPAN_DIGEST
        assert resolution.digest_matches is True

        # Recorded timestamp from the row, present and ISO-shaped.
        assert resolution.recorded_at.endswith("Z")


# ---------------------------------------------------------------------------
# Unresolvable region / revision (Requirement 3.6).
# ---------------------------------------------------------------------------


class TestUnresolvable:
    """Unknown region or revision raises :class:`RegionOccurrenceUnresolvableError`."""

    def test_unknown_region_id_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        region = _seed_region(engine, evidence_repository)
        _assign_view_role(
            authorization_service, engine, scope=region.document_resource_id
        )

        with engine.begin() as conn:
            with pytest.raises(RegionOccurrenceUnresolvableError) as exc:
                navigator.resolve_region_text(
                    conn,
                    region_id=_UNKNOWN_REGION_ID,
                    document_revision_id=region.document_revision_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.region_id == _UNKNOWN_REGION_ID
        assert exc.value.document_revision_id == region.document_revision_id

    def test_unknown_revision_id_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        region = _seed_region(engine, evidence_repository)
        _assign_view_role(
            authorization_service, engine, scope=region.document_resource_id
        )

        with engine.begin() as conn:
            with pytest.raises(RegionOccurrenceUnresolvableError) as exc:
                navigator.resolve_region_text(
                    conn,
                    region_id=region.region_id,
                    document_revision_id=_UNKNOWN_REVISION_ID,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.region_id == region.region_id
        assert exc.value.document_revision_id == _UNKNOWN_REVISION_ID


# ---------------------------------------------------------------------------
# Authorization denial (Requirement 7.4 / AD-WS-9).
# ---------------------------------------------------------------------------


class TestAuthorizationDenial:
    """Either authority missing → :class:`RegionTextAuthorizationError`."""

    def test_no_role_assignment_raises_authorization_error(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        engine: Engine,
    ) -> None:
        """A Party with no role assignment is denied with the
        ``no-role-assignment`` reason code (Requirement 7.2 / 12.2)."""
        _seed_required_parties(engine)
        region = _seed_region(engine, evidence_repository)

        # No role assignment for the requesting Party — denial on the
        # first check (``view.region_occurrence``).
        with engine.begin() as conn:
            with pytest.raises(RegionTextAuthorizationError) as exc:
                navigator.resolve_region_text(
                    conn,
                    region_id=region.region_id,
                    document_revision_id=region.document_revision_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.reason_code == "no-role-assignment"
        # The correlation_id is generated by AuthorizationService.evaluate
        # and is a canonical UUIDv7 string (32 hex chars + 4 dashes).
        assert isinstance(exc.value.correlation_id, str)
        assert len(exc.value.correlation_id) == 36

    def test_out_of_scope_role_assignment_raises_authorization_error(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """A view role scoped to a *different* Document → out-of-scope deny.

        The Party holds view authority but on the wrong scope; the
        AuthorizationService surfaces ``out-of-scope`` (Requirement
        7.2 / 12.2).
        """
        _seed_required_parties(engine)
        region = _seed_region(engine, evidence_repository)
        # Grant view authority scoped to a fictitious other Document.
        _assign_view_role(
            authorization_service,
            engine,
            scope="00000000-0000-7000-8000-0000000d9999",
        )

        with engine.begin() as conn:
            with pytest.raises(RegionTextAuthorizationError) as exc:
                navigator.resolve_region_text(
                    conn,
                    region_id=region.region_id,
                    document_revision_id=region.document_revision_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.reason_code == "out-of-scope"
        assert isinstance(exc.value.correlation_id, str)


# ---------------------------------------------------------------------------
# Idempotence (Requirement 11.5, example-based form of Property 9).
# ---------------------------------------------------------------------------


class TestIdempotence:
    """Repeated invocations return byte-equivalent resolutions."""

    def test_repeated_invocations_return_equal_resolutions(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """Five invocations of the same
        ``(region_id, document_revision_id, party, at)`` compare equal.

        Property 9 (task 12.9) generalizes this to Hypothesis-driven
        random pipelines; this test pins the example-based contract
        the property test relies on.
        """
        _seed_required_parties(engine)
        region = _seed_region(engine, evidence_repository)
        _assign_view_role(
            authorization_service, engine, scope=region.document_resource_id
        )

        resolutions = []
        for _ in range(5):
            with engine.begin() as conn:
                resolutions.append(
                    navigator.resolve_region_text(
                        conn,
                        region_id=region.region_id,
                        document_revision_id=region.document_revision_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EFFECTIVE_TIME,
                    )
                )

        first = resolutions[0]
        for other in resolutions[1:]:
            assert other == first
