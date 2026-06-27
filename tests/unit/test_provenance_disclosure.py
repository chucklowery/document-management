"""Unit tests for :class:`walking_slice.provenance.ProvenanceNavigator`
disclosure-policy enforcement (task 12.4).

These tests pin the contract established by task 12.4, design §"AD-WS-9 —
Default Completeness Disclosure policy", and Requirements 10.5, 10.7,
11.3, 11.4, 11.7:

- The seeded ``slice-default-2026`` policy is applied to every
  provenance response (lookup via :func:`walking_slice.disclosure.get_policy`).
- Restricted intermediate nodes appear as :class:`RedactedNode` markers
  carrying only ``{kind, redacted: True}`` (AD-WS-9 rule 1). The wrapper
  surfaces no additional attributes for redacted nodes and does not
  emit gap descriptors for restricted categories.
- Unresolved Omission Entries with category in
  ``{unavailable, stale, unresolved}`` surface as
  :class:`ChainGapDescriptor` instances with only ``stage``,
  ``category``, and ``next_reachable_node_identity``
  (AD-WS-9 rule 2 / Requirement 11.4).
- Intentional Omission Entries do not surface as gap descriptors
  (intentional omissions are recorded on the manifest but not surfaced
  to navigation callers — AD-WS-9 rule 2).
- Resolved Omission Entries do not surface as gap descriptors.
- Restricted-category Omission Entries are handled by
  :class:`RedactedNode` per rule 1, not gap descriptors per rule 2.
- :meth:`ProvenanceNavigator.navigate_decision_with_disclosure` surfaces
  the policy identifier and name on the
  :class:`DisclosureAppliedChain` so audit consumers can correlate the
  response with the ``Disclosure_Policies`` row in effect.
- A navigator constructed without a ``disclosure_policy`` raises
  :class:`DisclosurePolicyUnavailableError` on the policy-enforced
  surface but still serves the raw chain via
  :meth:`navigate_decision`.

Property 4 (Non-leakage of restricted information, task 12.7) further
verifies the indistinguishability dimensions across pairs of Parties;
these tests pin the minimum example-based shape.
"""

from __future__ import annotations

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
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    SLICE_DEFAULT_POLICY_NAME,
    get_policy,
    seed as seed_disclosure_policies,
)
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
from walking_slice.persistence import create_schema
from walking_slice.provenance import (
    ChainGapDescriptor,
    DecisionProvenanceChain,
    DisclosureAppliedChain,
    DisclosurePolicyUnavailableError,
    FindingRevisionNode,
    ProvenanceNavigator,
    RecommendationRevisionNode,
    RedactedNode,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_DECIDING_PARTY_ID = "00000000-0000-7000-8000-0000000d0001"
_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000d0002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000d0003"
_OMITTED_RESOURCE_ID = "00000000-0000-7000-8000-0000000d0010"
_OMITTED_REVISION_ID = "00000000-0000-7000-8000-0000000d0011"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000d00a1")

_SCOPE = "pilot/team-d"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # "quick brown fox"


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
        _seed_party(conn, _DECIDING_PARTY_ID, "Decision Maker")
        _seed_party(conn, _REQUESTER_PARTY_ID, "Reviewer")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _assign_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
    party_id: str = _REQUESTER_PARTY_ID,
    role_name: str = "reviewer",
) -> str:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=scope,
        authorities_granted=("view",),
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


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
def knowledge_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    manifest_writer: ProvenanceManifestWriter,
) -> KnowledgeService:
    """Knowledge_Service wired *with* the manifest writer.

    Wiring the writer means every create_finding /
    create_recommendation / create_decision call records a
    Provenance Manifest the disclosure-policy layer can read back.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        manifest_writer=manifest_writer,
    )


@pytest.fixture
def seeded_policy_navigator(
    engine: Engine,
    clock: Clock,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """ProvenanceNavigator wired with the seeded ``slice-default-2026`` policy.

    The ``audit_log`` fixture installs the schema; ``seed_disclosure_policies``
    inserts the policy row. Loading the policy via :func:`get_policy`
    returns the same value object the production startup hook hands the
    navigator (task 13.2 / 15.2).
    """
    seed_disclosure_policies(engine)
    policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
        disclosure_policy=policy,
    )


# ---------------------------------------------------------------------------
# Pipeline seeding.
# ---------------------------------------------------------------------------


class SeededPipeline:
    """Container for the identifiers returned by :func:`_seed_pipeline`."""

    def __init__(
        self,
        *,
        decision_id: str,
        recommendation_id: str,
        recommendation_revision_id: str,
        finding_id: str,
        finding_revision_id: str,
        document_resource_id: str,
        document_revision_id: str,
        region_id: str,
    ) -> None:
        self.decision_id = decision_id
        self.recommendation_id = recommendation_id
        self.recommendation_revision_id = recommendation_revision_id
        self.finding_id = finding_id
        self.finding_revision_id = finding_revision_id
        self.document_resource_id = document_resource_id
        self.document_revision_id = document_revision_id
        self.region_id = region_id


def _seed_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
    *,
    decision_omissions=(),
) -> SeededPipeline:
    """Seed Evidence → Finding → Recommendation → Decision.

    The Knowledge_Service has the manifest writer wired so every step
    records a Provenance Manifest. ``decision_omissions`` is forwarded
    to ``create_decision`` so tests can inject Omission Entries on the
    Decision-level manifest in a single call.
    """
    basis = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

    with engine.begin() as conn:
        document = evidence_repository.create_document(
            conn,
            content_bytes=_DOC_CONTENT,
            contributing_party_id=_DECIDING_PARTY_ID,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=document.resource_id,
            revision_id=document.revision_id,
            start_offset_bytes=_DOC_SPAN_START,
            end_offset_bytes=_DOC_SPAN_END,
            contributing_party_id=_DECIDING_PARTY_ID,
        )
        finding = knowledge_service.create_finding(
            conn,
            statement="Disclosure-test finding.",
            authoring_party_id=_DECIDING_PARTY_ID,
            supporting_region_occurrences=(
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=document.revision_id,
                ),
            ),
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_DECIDING_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend action.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Accept the recommendation.",
            deciding_party_id=_DECIDING_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
            omissions=decision_omissions,
        )

    return SeededPipeline(
        decision_id=decision.decision_id,
        recommendation_id=recommendation.recommendation_id,
        recommendation_revision_id=recommendation.recommendation_revision_id,
        finding_id=finding.finding_id,
        finding_revision_id=finding.finding_revision_id,
        document_resource_id=document.resource_id,
        document_revision_id=document.revision_id,
        region_id=region.region_id,
    )


def _grant_full_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    pipeline: SeededPipeline,
) -> None:
    _assign_view_role(authorization_service, engine, scope=_SCOPE)
    _assign_view_role(
        authorization_service, engine, scope=pipeline.recommendation_id
    )
    _assign_view_role(authorization_service, engine, scope=pipeline.finding_id)
    _assign_view_role(
        authorization_service, engine, scope=pipeline.document_resource_id
    )


def _insert_finding_manifest_omission(
    engine: Engine,
    pipeline: SeededPipeline,
    *,
    category: str,
    rationale: str = "Test omission rationale.",
    resolved_at=None,
) -> str:
    """Insert an Omission Entry on the Finding's existing manifest.

    Returns the inserted ``omission_entry_id``. Finds the manifest by
    (subject_kind='finding_revision', subject_id, subject_revision_id)
    so the test does not need to know the manifest id ahead of time.
    """
    omission_id = str(uuid.uuid4())
    with engine.begin() as conn:
        manifest_id = conn.execute(
            text(
                """
                SELECT manifest_id FROM Provenance_Manifests
                WHERE subject_kind = 'finding_revision'
                  AND subject_id = :fid
                  AND subject_revision_id = :frid
                """
            ),
            {
                "fid": pipeline.finding_id,
                "frid": pipeline.finding_revision_id,
            },
        ).scalar_one()
        conn.execute(
            text(
                """
                INSERT INTO Omission_Entries (
                    omission_entry_id, manifest_id,
                    excluded_source_id, excluded_source_revision_id,
                    category, rationale, authoring_party_id,
                    recorded_at, resolved_at
                ) VALUES (
                    :oid, :mid, :src, :srv, :cat, :rat, :pid, :ts, :resolved
                )
                """
            ),
            {
                "oid": omission_id,
                "mid": manifest_id,
                "src": _OMITTED_RESOURCE_ID,
                "srv": _OMITTED_REVISION_ID,
                "cat": category,
                "rat": rationale,
                "pid": _DECIDING_PARTY_ID,
                "ts": _TS_FIXED,
                "resolved": resolved_at,
            },
        )
    return omission_id


# ---------------------------------------------------------------------------
# Happy path: no omissions ⇒ no gap descriptors.
# ---------------------------------------------------------------------------


class TestNoOmissionsEmptyGapDescriptors:
    """A chain with no Omission Entries returns an empty descriptor tuple."""

    def test_no_manifest_omissions_yields_no_gap_descriptors(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(applied, DisclosureAppliedChain)
        assert applied.gap_descriptors == ()
        assert applied.policy_id == SLICE_DEFAULT_POLICY_ID
        assert applied.policy_name == SLICE_DEFAULT_POLICY_NAME
        # Chain itself is intact.
        assert applied.chain.decision.decision_id == pipeline.decision_id
        assert isinstance(
            applied.chain.recommendation_revision, RecommendationRevisionNode
        )


# ---------------------------------------------------------------------------
# Gap descriptors surface for unavailable/stale/unresolved categories.
# ---------------------------------------------------------------------------


class TestGapDescriptorsByCategory:
    """Each non-intentional, non-restricted category surfaces a descriptor."""

    @pytest.mark.parametrize("category", ["unavailable", "stale", "unresolved"])
    def test_each_gap_category_surfaces_descriptor(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
        category: str,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)
        _insert_finding_manifest_omission(
            engine, pipeline, category=category
        )

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert len(applied.gap_descriptors) == 1
        descriptor = applied.gap_descriptors[0]
        assert isinstance(descriptor, ChainGapDescriptor)
        assert descriptor.stage == "finding_revision"
        assert descriptor.category == category
        # Requirement 11.4: next reachable node identity (Finding identity).
        assert descriptor.next_reachable_node_identity == pipeline.finding_id


class TestIntentionalAndRestrictedNotSurfaced:
    """Intentional and restricted Omission Entries do not surface as gaps.

    AD-WS-9 rule 2: only ``unavailable``, ``stale``, and ``unresolved``
    are gap categories. ``intentional`` is recorded on the manifest but
    not surfaced to navigation; ``restricted`` is handled via
    :class:`RedactedNode` (AD-WS-9 rule 1).
    """

    def test_intentional_omission_does_not_surface(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)
        _insert_finding_manifest_omission(
            engine, pipeline, category="intentional"
        )

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert applied.gap_descriptors == ()

    def test_restricted_omission_does_not_surface_as_gap(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)
        _insert_finding_manifest_omission(
            engine, pipeline, category="restricted"
        )

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        # Restricted Omission Entries are handled by the marker policy
        # (rule 1), not the gap-descriptor policy (rule 2).
        assert applied.gap_descriptors == ()


class TestResolvedOmissionNotSurfaced:
    """A resolved Omission Entry no longer surfaces as a gap."""

    def test_resolved_omission_excluded(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)
        # Insert with a resolved_at timestamp so the gap is "closed".
        _insert_finding_manifest_omission(
            engine,
            pipeline,
            category="stale",
            resolved_at="2026-02-01T00:00:00.000Z",
        )

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert applied.gap_descriptors == ()


# ---------------------------------------------------------------------------
# Restricted Finding ⇒ no gap descriptors for that Finding's manifest.
# ---------------------------------------------------------------------------


class TestRedactedSubjectNotProbedForGaps:
    """A redacted Finding contributes no gap descriptors.

    The navigator already replaces the Finding Revision node with a
    :class:`RedactedNode` when the requesting Party lacks view authority.
    The disclosure-policy layer MUST NOT then probe that Finding's
    manifest for Omission Entries, because doing so could leak the
    presence of an omission on a Finding the requesting Party cannot
    view (Property 4, Non-leakage of restricted information).
    """

    def test_omissions_on_redacted_finding_not_surfaced(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        # Grant every scope EXCEPT the Finding.
        _assign_view_role(authorization_service, engine, scope=_SCOPE)
        _assign_view_role(
            authorization_service, engine, scope=pipeline.recommendation_id
        )
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.document_resource_id,
        )
        # Insert a gap-category Omission on the Finding's manifest.
        _insert_finding_manifest_omission(
            engine, pipeline, category="unavailable"
        )

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        # The Finding is redacted; its manifest is not probed.
        assert applied.chain.findings == (
            RedactedNode(kind="finding_revision"),
        )
        assert applied.gap_descriptors == ()


# ---------------------------------------------------------------------------
# Decision-level Omission Entries surface as gap descriptors.
# ---------------------------------------------------------------------------


class TestDecisionLevelGapDescriptor:
    """Omission Entries on the Decision's manifest surface as gaps.

    Decisions can be created with :class:`DecisionOmissionEntry`
    entries; the Knowledge_Service wires them onto the manifest with
    ``subject_kind='decision'`` and ``subject_revision_id=NULL``. The
    disclosure-policy layer must walk this manifest and surface
    descriptors with ``stage='decision'``.
    """

    def test_decision_unavailable_omission_surfaces_at_decision_stage(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        decision_omission = DecisionOmissionEntry(
            excluded_source_id=_OMITTED_RESOURCE_ID,
            excluded_source_revision_id=None,
            category="unavailable",
            rationale="Source upload service was offline at decision time.",
        )
        pipeline = _seed_pipeline(
            engine,
            evidence_repository,
            knowledge_service,
            decision_omissions=(decision_omission,),
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        # The descriptor should land at the decision stage with
        # next_reachable_node_identity = decision_id.
        decision_gaps = [
            g for g in applied.gap_descriptors if g.stage == "decision"
        ]
        assert len(decision_gaps) == 1
        gap = decision_gaps[0]
        assert gap.category == "unavailable"
        assert gap.next_reachable_node_identity == pipeline.decision_id


# ---------------------------------------------------------------------------
# Multiple stages.
# ---------------------------------------------------------------------------


class TestMultipleStageGapDescriptors:
    """A chain with omissions at multiple stages emits one descriptor each.

    The descriptors are ordered by stage (Decision → Recommendation
    Revision → Finding Revision) so the response shape is
    deterministic.
    """

    def test_decision_and_finding_omissions_both_surface(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        decision_omission = DecisionOmissionEntry(
            excluded_source_id=_OMITTED_RESOURCE_ID,
            excluded_source_revision_id=None,
            category="unresolved",
            rationale="Manual review pending for this Source.",
        )
        pipeline = _seed_pipeline(
            engine,
            evidence_repository,
            knowledge_service,
            decision_omissions=(decision_omission,),
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)
        _insert_finding_manifest_omission(
            engine, pipeline, category="stale"
        )

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        # One descriptor at decision stage, one at finding stage.
        stages = [g.stage for g in applied.gap_descriptors]
        assert stages.count("decision") == 1
        assert stages.count("finding_revision") == 1
        # Decision stage comes before finding stage (walk order).
        assert stages.index("decision") < stages.index("finding_revision")


# ---------------------------------------------------------------------------
# Policy identifier surfaced.
# ---------------------------------------------------------------------------


class TestPolicyIdentifierSurfaced:
    """The DisclosureAppliedChain carries the policy id/name."""

    def test_policy_id_and_name_match_seeded_policy(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        with engine.connect() as conn:
            applied = seeded_policy_navigator.navigate_decision_with_disclosure(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert applied.policy_id == SLICE_DEFAULT_POLICY_ID
        assert applied.policy_name == SLICE_DEFAULT_POLICY_NAME


# ---------------------------------------------------------------------------
# Idempotence at the policy-applied layer.
# ---------------------------------------------------------------------------


class TestIdempotencePolicyApplied:
    """Repeated invocations return equal :class:`DisclosureAppliedChain`."""

    def test_repeated_invocations_return_equal_applied_chains(
        self,
        seeded_policy_navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)
        _insert_finding_manifest_omission(
            engine, pipeline, category="stale"
        )

        chains = []
        for _ in range(5):
            with engine.connect() as conn:
                chains.append(
                    seeded_policy_navigator.navigate_decision_with_disclosure(
                        conn,
                        decision_id=pipeline.decision_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EFFECTIVE_TIME,
                    )
                )

        first = chains[0]
        for other in chains[1:]:
            assert other == first


# ---------------------------------------------------------------------------
# Missing policy ⇒ DisclosurePolicyUnavailableError.
# ---------------------------------------------------------------------------


class TestMissingPolicyRaises:
    """A navigator without a disclosure_policy raises on the policy surface.

    The raw :meth:`navigate_decision` continues to work because it does
    not need the policy; only the policy-enforced methods require it.
    """

    def test_navigate_decision_with_disclosure_requires_policy(
        self,
        clock: Clock,
        authorization_service: AuthorizationService,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        # No disclosure_policy passed.
        navigator = ProvenanceNavigator(
            clock=clock,
            authorization_service=authorization_service,
        )

        with engine.connect() as conn:
            with pytest.raises(DisclosurePolicyUnavailableError):
                navigator.navigate_decision_with_disclosure(
                    conn,
                    decision_id=pipeline.decision_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )

    def test_apply_disclosure_policy_requires_policy(
        self,
        clock: Clock,
        authorization_service: AuthorizationService,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        navigator = ProvenanceNavigator(
            clock=clock,
            authorization_service=authorization_service,
        )
        with engine.connect() as conn:
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        with engine.connect() as conn:
            with pytest.raises(DisclosurePolicyUnavailableError):
                navigator.apply_disclosure_policy(
                    conn,
                    chain=chain,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )


# ---------------------------------------------------------------------------
# Redacted node attributes are not leaked.
# ---------------------------------------------------------------------------


class TestRedactedNodeShape:
    """RedactedNode carries only ``{kind, redacted: True}``.

    Pins AD-WS-9 rule 1: the marker excludes ``identifier``,
    ``attributes``, and ``count``. The dataclass exposes ``kind`` and
    ``redacted`` as its only public fields; this test guards against
    field drift.
    """

    def test_redacted_node_exposes_only_kind_and_redacted(self) -> None:
        from dataclasses import fields

        node = RedactedNode(kind="finding_revision")
        names = {f.name for f in fields(node)}
        assert names == {"kind", "redacted"}
        assert node.kind == "finding_revision"
        assert node.redacted is True
