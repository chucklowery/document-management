"""Unit tests for the manifest-writer wiring on
:mod:`walking_slice.knowledge` (task 9.2).

These tests pin the contract task 9.2 introduces: when the
:class:`~walking_slice.knowledge.KnowledgeService` is constructed with
a wired :class:`~walking_slice.manifests.ProvenanceManifestWriter`, the
service's three consequential synthesis writes (:meth:`create_finding`,
:meth:`create_recommendation`, :meth:`create_decision`) each persist
one ``Provenance_Manifests`` row inside the originating transaction
(AD-WS-5, Requirements 10.1, 10.2, 10.6). On manifest persistence
failure, the originating finalization rolls back so no domain row is
observable post-failure (Requirement 10.6 / design §"Provenance
manifest persistence failure").

The four named test behaviours from the task description:

1. :meth:`create_finding` records a Provenance Manifest with
   ``subject_kind='finding_revision'`` whose ``included_sources_json``
   lists every supporting Region Occurrence.
2. :meth:`create_recommendation` records a Provenance Manifest with
   ``subject_kind='recommendation_revision'`` whose
   ``included_sources_json`` lists every Derived From Finding.
3. :meth:`create_decision` still records a Provenance Manifest with
   ``subject_kind='decision'``, *now via the writer rather than the
   inline INSERT path* — including conversion of
   :class:`DecisionOmissionEntry` to the writer's
   :class:`OmissionEntry`.
4. A manifest persistence failure rolls the originating finalization
   back; no Finding (or Recommendation, or Decision) row is observable
   after the failure.

The tests use the per-test SQLite ``engine`` fixture from
:mod:`tests.conftest` so every persisted row is observable through a
read query against the same database the service just used.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Sequence

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    DecisionOmissionEntry,
    KnowledgeService,
    SupportRef,
)
from walking_slice.manifests import (
    IncludedSource,
    OmissionEntry as ManifestOmissionEntry,
    ProvenanceManifestWriter,
)
from walking_slice.models import AuthorityBasisRef


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seeding helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _seed_party(conn: Connection, party_id: str = _PARTY_ID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Tester', :ts)
            """
        ),
        {"pid": party_id, "ts": _TS_FIXED},
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_repository(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> EvidenceRepository:
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
    manifest_writer: ProvenanceManifestWriter,
) -> KnowledgeService:
    """Knowledge_Service wired *with* the manifest writer.

    This is the task 9.2 path: every consequential write delegates the
    Provenance Manifest INSERT to the writer.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=manifest_writer,
    )


@pytest.fixture
def basis() -> AuthorityBasisRef:
    return AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)


# ---------------------------------------------------------------------------
# Row readers.
# ---------------------------------------------------------------------------


def _fetch_manifest_by_subject(
    engine: Engine, *, subject_kind: str, subject_id: str
) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT manifest_id, subject_kind, subject_id,
                           subject_revision_id, authoring_party_id,
                           recorded_at, included_sources_json, is_complete
                    FROM Provenance_Manifests
                    WHERE subject_kind = :sk AND subject_id = :sid
                    """
                ),
                {"sk": subject_kind, "sid": subject_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_omissions(engine: Engine, *, manifest_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT omission_entry_id, manifest_id, excluded_source_id,
                           excluded_source_revision_id, category, rationale,
                           authoring_party_id, recorded_at, resolved_at
                    FROM Omission_Entries
                    WHERE manifest_id = :mid
                    ORDER BY omission_entry_id
                    """
                ),
                {"mid": manifest_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()


# ---------------------------------------------------------------------------
# Seed helpers — build supporting Region Occurrences via the
# Evidence_Repository so manifests can list real Included Sources.
# ---------------------------------------------------------------------------


def _make_support_refs(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    *,
    count: int,
) -> Sequence[SupportRef]:
    """Seed ``count`` Region Occurrences and return their SupportRefs.

    Each Region Occurrence is anchored to a distinct Source Document so
    the Region Identities are guaranteed unique. The first call seeds
    the Party row so subsequent calls do not double-insert.
    """
    refs: list[SupportRef] = []
    for index in range(count):
        content = f"doc-{index}-bytes-{'x' * (index + 1)}".encode("utf-8")
        with engine.begin() as conn:
            if index == 0:
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
                start_offset_bytes=0,
                end_offset_bytes=min(5, len(content)),
                contributing_party_id=_PARTY_ID,
            )
            refs.append(
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                )
            )
    return refs


# ---------------------------------------------------------------------------
# Test 1 — create_finding wires a manifest with subject_kind='finding_revision'.
# ---------------------------------------------------------------------------


def test_create_finding_wires_manifest_listing_supporting_region_occurrences(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Requirement 10.1 / task 9.2: ``create_finding`` records a
    Provenance Manifest whose ``subject_kind`` is
    ``'finding_revision'`` and whose Included Sources list each
    supporting Region Occurrence (kind ``'region_occurrence'``,
    resource_id = ``region_id``, revision_id = owning Document
    Revision)."""
    supports = _make_support_refs(engine, evidence_repository, count=2)

    with engine.begin() as conn:
        result = knowledge_service.create_finding(
            conn,
            statement="Anchored finding for manifest wiring test.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=supports,
        )

    manifest = _fetch_manifest_by_subject(
        engine,
        subject_kind="finding_revision",
        subject_id=result.finding_id,
    )
    assert manifest is not None
    assert _CANONICAL_UUID7.match(manifest["manifest_id"])
    assert manifest["subject_kind"] == "finding_revision"
    assert manifest["subject_id"] == result.finding_id
    assert manifest["subject_revision_id"] == result.finding_revision_id
    assert manifest["authoring_party_id"] == _PARTY_ID
    assert manifest["recorded_at"] == result.recorded_at
    # No omissions supplied → manifest is complete.
    assert manifest["is_complete"] == 1
    assert _fetch_omissions(engine, manifest_id=manifest["manifest_id"]) == []

    included = json.loads(manifest["included_sources_json"])
    assert len(included) == len(supports)
    for source_entry, support in zip(included, supports):
        assert source_entry["kind"] == "region_occurrence"
        assert source_entry["resource_id"] == support.region_id
        assert source_entry["revision_id"] == support.document_revision_id


def test_hypothesis_finding_with_zero_supports_wires_empty_manifest(
    engine: Engine,
    knowledge_service: KnowledgeService,
) -> None:
    """A hypothesis Finding with zero supports records a Provenance
    Manifest whose ``included_sources_json`` is an empty list — the
    manifest still exists so downstream provenance traversals find a
    landing node, but it claims no material sources."""
    with engine.begin() as conn:
        _seed_party(conn)
        result = knowledge_service.create_finding(
            conn,
            statement="Speculative hypothesis with no supports.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )

    manifest = _fetch_manifest_by_subject(
        engine,
        subject_kind="finding_revision",
        subject_id=result.finding_id,
    )
    assert manifest is not None
    assert manifest["is_complete"] == 1
    assert json.loads(manifest["included_sources_json"]) == []


# ---------------------------------------------------------------------------
# Test 2 — create_recommendation wires a manifest with
# subject_kind='recommendation_revision'.
# ---------------------------------------------------------------------------


def test_create_recommendation_wires_manifest_listing_derived_from_findings(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Requirement 10.1 / task 9.2: ``create_recommendation`` records a
    Provenance Manifest whose ``subject_kind`` is
    ``'recommendation_revision'`` and whose Included Sources list each
    Derived From Finding (kind ``'finding_revision'``, resource_id =
    ``finding_id``, revision_id = ``None`` because Derived From keys on
    Finding Resource per Requirement 5.6)."""
    supports = _make_support_refs(engine, evidence_repository, count=1)

    with engine.begin() as conn:
        finding_a = knowledge_service.create_finding(
            conn,
            statement="Source Finding A.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=supports,
        )
        finding_b = knowledge_service.create_finding(
            conn,
            statement="Source Finding B (hypothesis).",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[
                finding_a.finding_id,
                finding_b.finding_id,
            ],
            rationale="Synthesize the two source Findings.",
        )

    manifest = _fetch_manifest_by_subject(
        engine,
        subject_kind="recommendation_revision",
        subject_id=result.recommendation_id,
    )
    assert manifest is not None
    assert manifest["subject_kind"] == "recommendation_revision"
    assert manifest["subject_id"] == result.recommendation_id
    assert manifest["subject_revision_id"] == result.recommendation_revision_id
    assert manifest["authoring_party_id"] == _PARTY_ID
    assert manifest["is_complete"] == 1

    included = json.loads(manifest["included_sources_json"])
    assert [entry["kind"] for entry in included] == [
        "finding_revision",
        "finding_revision",
    ]
    assert [entry["resource_id"] for entry in included] == [
        finding_a.finding_id,
        finding_b.finding_id,
    ]
    # The ``Derived From`` Relationship keys on Finding Resource per
    # Requirement 5.6, so the included sources carry revision_id=None.
    assert [entry["revision_id"] for entry in included] == [None, None]


# ---------------------------------------------------------------------------
# Test 3 — create_decision still records a manifest, now via the writer.
# ---------------------------------------------------------------------------


def test_create_decision_wires_manifest_with_subject_kind_decision(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Requirement 10.1 / task 9.2: ``create_decision`` records a
    Provenance Manifest with ``subject_kind='decision'`` via the
    writer rather than the inline INSERT path. The Manifest's single
    Included Source is the target Recommendation Revision."""
    supports = _make_support_refs(engine, evidence_repository, count=1)

    with engine.begin() as conn:
        finding = knowledge_service.create_finding(
            conn,
            statement="Source Finding for Decision test.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=supports,
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend action based on Finding.",
        )

    with engine.begin() as conn:
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Approve the recommended action.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )

    manifest = _fetch_manifest_by_subject(
        engine,
        subject_kind="decision",
        subject_id=decision.decision_id,
    )
    assert manifest is not None
    assert manifest["manifest_id"] == decision.manifest_id
    assert manifest["subject_kind"] == "decision"
    assert manifest["subject_id"] == decision.decision_id
    # A Decision has no Revision Identity (AD-WS-3 / AD-WS-4).
    assert manifest["subject_revision_id"] is None
    assert manifest["authoring_party_id"] == _PARTY_ID
    assert manifest["is_complete"] == 1
    assert manifest["recorded_at"] == decision.recorded_at

    included = json.loads(manifest["included_sources_json"])
    assert len(included) == 1
    assert included[0]["kind"] == "recommendation_revision"
    assert included[0]["resource_id"] == recommendation.recommendation_id
    assert included[0]["revision_id"] == (
        recommendation.recommendation_revision_id
    )


def test_create_decision_omissions_are_converted_and_persisted_through_writer(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
    basis: AuthorityBasisRef,
) -> None:
    """Requirement 10.2 / 10.3 / task 9.2: DecisionOmissionEntry values
    are converted to the writer's :class:`OmissionEntry` and persisted
    in ``Omission_Entries`` inside the Decision transaction. A
    non-intentional category causes the manifest's ``is_complete`` to
    be ``0``."""
    supports = _make_support_refs(engine, evidence_repository, count=1)

    with engine.begin() as conn:
        finding = knowledge_service.create_finding(
            conn,
            statement="Finding for omission test.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=supports,
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Rec for omission test.",
        )

    omissions = (
        DecisionOmissionEntry(
            excluded_source_id="00000000-0000-7000-8000-0000000000c1",
            excluded_source_revision_id=None,
            category="unavailable",
            rationale="Source server was offline during synthesis.",
        ),
    )
    with engine.begin() as conn:
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Defer",
            rationale="Defer pending the omitted source.",
            deciding_party_id=_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
            omissions=omissions,
        )

    manifest = _fetch_manifest_by_subject(
        engine,
        subject_kind="decision",
        subject_id=decision.decision_id,
    )
    assert manifest is not None
    # The ``unavailable`` category is non-intentional, so the manifest
    # is incomplete per Requirement 10.3.
    assert manifest["is_complete"] == 0

    persisted = _fetch_omissions(engine, manifest_id=manifest["manifest_id"])
    assert len(persisted) == 1
    assert persisted[0]["omission_entry_id"] == decision.omission_entry_ids[0]
    assert persisted[0]["excluded_source_id"] == (
        "00000000-0000-7000-8000-0000000000c1"
    )
    assert persisted[0]["category"] == "unavailable"
    assert persisted[0]["rationale"] == (
        "Source server was offline during synthesis."
    )
    assert persisted[0]["authoring_party_id"] == _PARTY_ID
    assert persisted[0]["recorded_at"] == decision.recorded_at
    assert persisted[0]["resolved_at"] is None


# ---------------------------------------------------------------------------
# Test 4 — manifest persistence failure rolls back the originating
# finalization.
# ---------------------------------------------------------------------------


class _RaisingManifestWriter(ProvenanceManifestWriter):
    """Test double that fails on every :meth:`write_manifest` call.

    Used to exercise Requirement 10.6's "manifest persistence failure
    rolls back the originating finalization" contract. The writer
    raises *after* the surrounding transaction has begun and before
    any audit row would be appended, so a successful rollback means
    no Finding (or Recommendation, or Decision) row is observable
    afterwards.
    """

    def write_manifest(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError("simulated manifest persistence failure")


@pytest.fixture
def raising_manifest_writer(
    clock, identity_service: IdentityService
) -> _RaisingManifestWriter:
    return _RaisingManifestWriter(
        clock=clock, identity_service=identity_service
    )


@pytest.fixture
def knowledge_service_failing_manifest(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    raising_manifest_writer: _RaisingManifestWriter,
) -> KnowledgeService:
    """A KnowledgeService wired with a manifest writer that always
    raises — Requirement 10.6 must roll the originating transaction
    back."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=raising_manifest_writer,
    )


def test_manifest_failure_rolls_back_create_finding(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service_failing_manifest: KnowledgeService,
) -> None:
    """Requirement 10.6: a manifest persistence failure during
    :meth:`create_finding` rolls the whole transaction back; no
    Finding, Finding_Revisions, Relationships, or consequential
    Audit_Records row survives."""
    supports = _make_support_refs(engine, evidence_repository, count=1)

    findings_before = _count(engine, "Findings")
    revisions_before = _count(engine, "Finding_Revisions")
    relationships_before = _count(engine, "Relationships")
    audit_before = _count(engine, "Audit_Records")
    manifests_before = _count(engine, "Provenance_Manifests")

    with pytest.raises(RuntimeError, match="simulated manifest"):
        with engine.begin() as conn:
            knowledge_service_failing_manifest.create_finding(
                conn,
                statement="Finding that should be rolled back.",
                authoring_party_id=_PARTY_ID,
                supporting_region_occurrences=supports,
            )

    # No new Finding / Finding_Revisions / Relationships /
    # Provenance_Manifests rows were committed, and the consequential
    # audit row never had a chance to append because the writer
    # raised *before* the audit_log.append_consequential() call. The
    # transaction rolled back everything that *did* execute.
    assert _count(engine, "Findings") == findings_before
    assert _count(engine, "Finding_Revisions") == revisions_before
    assert _count(engine, "Relationships") == relationships_before
    assert _count(engine, "Audit_Records") == audit_before
    assert _count(engine, "Provenance_Manifests") == manifests_before


def test_manifest_failure_rolls_back_create_recommendation(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    manifest_writer: ProvenanceManifestWriter,
    raising_manifest_writer: _RaisingManifestWriter,
) -> None:
    """Requirement 10.6: a manifest persistence failure during
    :meth:`create_recommendation` rolls the whole transaction back; no
    Recommendation, Recommendation_Revisions, Relationships, or
    consequential Audit_Records row survives.

    To seed a Finding we use a *working* manifest writer; we then
    swap in the raising writer for the Recommendation create call so
    only the Recommendation transaction fails.
    """
    supports = _make_support_refs(engine, evidence_repository, count=1)

    # Working KnowledgeService for seeding the Finding.
    working_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=manifest_writer,
    )
    with engine.begin() as conn:
        finding = working_service.create_finding(
            conn,
            statement="Source Finding.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=supports,
        )

    # Now swap in the raising writer for the Recommendation call.
    failing_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=raising_manifest_writer,
    )

    recs_before = _count(engine, "Recommendations")
    rec_revs_before = _count(engine, "Recommendation_Revisions")
    relationships_before = _count(engine, "Relationships")
    audit_before = _count(engine, "Audit_Records")
    manifests_before = _count(engine, "Provenance_Manifests")

    with pytest.raises(RuntimeError, match="simulated manifest"):
        with engine.begin() as conn:
            failing_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="Rec that should be rolled back.",
            )

    assert _count(engine, "Recommendations") == recs_before
    assert _count(engine, "Recommendation_Revisions") == rec_revs_before
    assert _count(engine, "Relationships") == relationships_before
    assert _count(engine, "Audit_Records") == audit_before
    assert _count(engine, "Provenance_Manifests") == manifests_before


def test_manifest_failure_rolls_back_create_decision(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    manifest_writer: ProvenanceManifestWriter,
    raising_manifest_writer: _RaisingManifestWriter,
    basis: AuthorityBasisRef,
) -> None:
    """Requirement 10.6: a manifest persistence failure during
    :meth:`create_decision` rolls the whole transaction back; no
    Decision, Addresses Relationship, Provenance Manifest, Omission
    Entry, or consequential Audit_Records row survives."""
    supports = _make_support_refs(engine, evidence_repository, count=1)
    working_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=manifest_writer,
    )
    with engine.begin() as conn:
        finding = working_service.create_finding(
            conn,
            statement="Finding for Decision rollback test.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=supports,
        )
        recommendation = working_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Rec for Decision rollback test.",
        )

    failing_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=raising_manifest_writer,
    )

    decisions_before = _count(engine, "Decisions")
    relationships_before = _count(engine, "Relationships")
    manifests_before = _count(engine, "Provenance_Manifests")
    omissions_before = _count(engine, "Omission_Entries")
    audit_before = _count(engine, "Audit_Records")

    with pytest.raises(RuntimeError, match="simulated manifest"):
        with engine.begin() as conn:
            failing_service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="Decision that should be rolled back.",
                deciding_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
                omissions=(
                    DecisionOmissionEntry(
                        excluded_source_id="00000000-0000-7000-8000-0000000000d1",
                        excluded_source_revision_id=None,
                        category="intentional",
                        rationale="Out of scope for this synthesis.",
                    ),
                ),
            )

    assert _count(engine, "Decisions") == decisions_before
    assert _count(engine, "Relationships") == relationships_before
    assert _count(engine, "Provenance_Manifests") == manifests_before
    assert _count(engine, "Omission_Entries") == omissions_before
    assert _count(engine, "Audit_Records") == audit_before
