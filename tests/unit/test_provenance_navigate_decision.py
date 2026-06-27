"""Unit tests for :meth:`walking_slice.provenance.ProvenanceNavigator.navigate_decision`.

These tests pin the contract established in task 12.2, design
§"Provenance traversal algorithm", and Requirements 11.1, 11.2, 11.5,
and 11.6:

- 11.1 — The chain has the five stages Decision → Recommendation
  Revision → Finding Revision(s) → Region Occurrence(s) → Document
  Revision, each node identified by its Identity and (where
  applicable) Revision Identity.
- 11.2 — Each Region Occurrence node carries the start anchor, end
  anchor, and byte-equivalent bounded text from the originating
  Document Revision; the persisted ``span_content_digest_sha256``
  equals the SHA-256 of ``bounded_text``.
- 11.5 — Repeated invocations with the same ``(decision_id, party_id,
  at)`` return byte-equivalent :class:`DecisionProvenanceChain`
  instances (Property 8 idempotence as an example-based test;
  Property 8's full Hypothesis-driven version is task 12.8).
- 11.6 — An unresolvable Decision identity surfaces
  :class:`DecisionUnresolvableError` and discloses nothing about
  related Resources.

Authorization filtering at each stage is exercised in the
``TestRedactionPerStage`` class. Full Completeness Disclosure
policy enforcement (Requirements 11.3, 11.4, 11.7) is task 12.4;
these tests pin only the minimum behaviour required by task 12.2
(replace restricted nodes with a generic redaction marker carrying
only the node kind, raise the unresolvable error on Decision-level
denial).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

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
from walking_slice.knowledge import (
    CreateDecisionResult,
    CreateFindingResult,
    CreateRecommendationResult,
    KnowledgeService,
    SupportRef,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.provenance import (
    DecisionNode,
    DecisionProvenanceChain,
    DecisionUnresolvableError,
    DocumentRevisionNode,
    FindingRevisionNode,
    ProvenanceNavigator,
    RecommendationRevisionNode,
    RedactedNode,
    RegionOccurrenceNode,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_DECIDING_PARTY_ID = "00000000-0000-7000-8000-0000000c0001"
_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000c0002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000c0003"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000c00a1")

_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
_DOC_SPAN_START = 4
_DOC_SPAN_END = 19  # "quick brown fox"
_EXPECTED_SPAN_BYTES = _DOC_CONTENT[_DOC_SPAN_START:_DOC_SPAN_END]


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
    """Seed Parties referenced by the test pipeline.

    The deciding Party authors the Decision, the requesting Party
    requests provenance, and the assigning-authority Party records
    every Role Assignment used in the redaction-per-stage tests.
    """
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
    """Grant ``view`` authority to ``party_id`` for ``scope``.

    Scope semantics match
    :meth:`ProvenanceNavigator.navigate_decision`'s
    per-stage authorization scope choices:

    - Decision: ``Decisions.applicable_scope`` (the
      ``_SCOPE`` constant).
    - Recommendation Revision: ``recommendation_id`` of the Rec.
    - Finding Revision: ``finding_id``.
    - Region Occurrence and Document Revision:
      ``Document_Revisions.resource_id`` (the owning Document).

    Tests pass the appropriate scope so the assignment grants the
    intended view authority and nothing more.
    """
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
    """Evidence_Repository wired to the per-test fixtures."""
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
) -> KnowledgeService:
    """Knowledge_Service without authorization wired (back-compat path).

    The Decision-Maker authority check (task 8.2) is not under test
    here; the requesting Party's view authority is. Seeding the
    pipeline with an unwired KnowledgeService keeps the seeding step
    independent of the role assignments that drive the
    redaction-per-stage assertions.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def navigator(
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """ProvenanceNavigator wired to the per-test fixtures."""
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Pipeline builder.
# ---------------------------------------------------------------------------


class SeededPipeline:
    """Convenience bundle of the identifiers returned by :func:`_seed_pipeline`.

    The pipeline is one Decision pointing at one Recommendation
    Revision derived from one Finding Revision, which Supports one
    Region Occurrence on one Document Revision. The single-arm
    pipeline is enough to exercise every Requirement 11.1 stage and
    every per-stage redaction case; multi-finding and
    multi-occurrence shapes are added on top of this baseline in
    :class:`TestMultipleFindings` and :class:`TestMultipleSupports`.
    """

    def __init__(
        self,
        *,
        decision: CreateDecisionResult,
        recommendation: CreateRecommendationResult,
        finding: CreateFindingResult,
        document_resource_id: str,
        document_revision_id: str,
        region_id: str,
    ) -> None:
        self.decision = decision
        self.recommendation = recommendation
        self.finding = finding
        self.document_resource_id = document_resource_id
        self.document_revision_id = document_revision_id
        self.region_id = region_id


def _seed_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
    *,
    statement: str = "Quick brown fox is documented.",
    rationale: str = "Recommend action based on observed phrase.",
    decision_rationale: str = "Accept Recommendation.",
) -> SeededPipeline:
    """Seed a full one-arm Evidence → Decision pipeline.

    Returns a :class:`SeededPipeline` bundling every identifier the
    tests assert against. The seed uses the unwired Knowledge_Service
    (the Decision authority check is not under test here) so no
    Role Assignments are required for the seeding step.
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
            statement=statement,
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
            rationale=rationale,
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale=decision_rationale,
            deciding_party_id=_DECIDING_PARTY_ID,
            authority_basis=basis,
            applicable_scope=_SCOPE,
        )

    return SeededPipeline(
        decision=decision,
        recommendation=recommendation,
        finding=finding,
        document_resource_id=document.resource_id,
        document_revision_id=document.revision_id,
        region_id=region.region_id,
    )


def _grant_full_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    pipeline: SeededPipeline,
) -> None:
    """Grant the requesting Party every scope needed for an unrestricted chain.

    Five Role Assignments are created so the Party has ``view``
    authority on every node the traversal will check. Each
    assignment is scope-narrowed so a later test that *omits* one
    of them can confirm the corresponding stage is redacted.
    """
    _assign_view_role(authorization_service, engine, scope=_SCOPE)
    _assign_view_role(
        authorization_service,
        engine,
        scope=pipeline.recommendation.recommendation_id,
    )
    _assign_view_role(
        authorization_service,
        engine,
        scope=pipeline.finding.finding_id,
    )
    _assign_view_role(
        authorization_service,
        engine,
        scope=pipeline.document_resource_id,
    )


# ---------------------------------------------------------------------------
# Unresolvable Decision (Requirement 11.6).
# ---------------------------------------------------------------------------


class TestUnresolvableDecision:
    """Requirement 11.6: unresolvable Decision identifier is an error."""

    def test_unknown_decision_id_raises_decision_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        engine: Engine,
        audit_log: AuditLog,  # forces schema creation
    ) -> None:
        unknown = "00000000-0000-7000-8000-00000000ffff"
        with engine.connect() as conn:
            with pytest.raises(DecisionUnresolvableError) as exc:
                navigator.navigate_decision(
                    conn,
                    decision_id=unknown,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.decision_id == unknown
        # Requirement 11.6 forbids disclosing existence of related
        # Resources; the message names only the unresolvable
        # Decision reference.
        assert unknown in str(exc.value)

    def test_decision_level_denial_raises_decision_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        """Design pseudocode ``not_found_indistinguishable_response``.

        When the Decision exists but the requesting Party lacks
        ``view.decision`` authority, the navigator raises the same
        :class:`DecisionUnresolvableError` so the response is
        indistinguishable from the unresolvable case. Full Requirement
        11.7 enforcement (timing indistinguishability, redaction
        policy shape) is task 12.4 — this test pins only the
        exception-shape invariant.
        """
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )

        # No role assignments — the requesting Party has no view
        # authority on anything.
        with engine.connect() as conn:
            with pytest.raises(DecisionUnresolvableError) as exc:
                navigator.navigate_decision(
                    conn,
                    decision_id=pipeline.decision.decision_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.decision_id == pipeline.decision.decision_id


# ---------------------------------------------------------------------------
# Happy path (Requirements 11.1, 11.2).
# ---------------------------------------------------------------------------


class TestHappyPathChainShape:
    """Requirement 11.1: the chain has all five stages with full identities."""

    def test_full_chain_returns_every_stage_with_identity_attributes(
        self,
        navigator: ProvenanceNavigator,
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
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        # Decision (head of chain).
        assert isinstance(chain.decision, DecisionNode)
        assert chain.decision.decision_id == pipeline.decision.decision_id
        assert chain.decision.outcome == "Accept"
        assert chain.decision.target_recommendation_id == (
            pipeline.recommendation.recommendation_id
        )
        assert chain.decision.target_recommendation_revision_id == (
            pipeline.recommendation.recommendation_revision_id
        )

        # Recommendation Revision.
        assert isinstance(chain.recommendation_revision, RecommendationRevisionNode)
        assert chain.recommendation_revision.recommendation_id == (
            pipeline.recommendation.recommendation_id
        )
        assert chain.recommendation_revision.recommendation_revision_id == (
            pipeline.recommendation.recommendation_revision_id
        )

        # Finding Revision (exactly one).
        assert len(chain.findings) == 1
        finding_node = chain.findings[0]
        assert isinstance(finding_node, FindingRevisionNode)
        assert finding_node.finding_id == pipeline.finding.finding_id
        assert finding_node.finding_revision_id == (
            pipeline.finding.finding_revision_id
        )
        assert finding_node.is_hypothesis is False

        # Region Occurrence (one per Supports, here exactly one).
        assert len(chain.region_occurrences) == 1
        region_node = chain.region_occurrences[0]
        assert isinstance(region_node, RegionOccurrenceNode)
        assert region_node.region_id == pipeline.region_id
        assert region_node.document_revision_id == pipeline.document_revision_id

        # Document Revision (positionally aligned with region_occurrences).
        assert len(chain.document_revisions) == 1
        doc_node = chain.document_revisions[0]
        assert isinstance(doc_node, DocumentRevisionNode)
        assert doc_node.resource_id == pipeline.document_resource_id
        assert doc_node.revision_id == pipeline.document_revision_id

        # Requested decision id is echoed.
        assert chain.requested_decision_id == pipeline.decision.decision_id


class TestRegionOccurrenceText:
    """Requirement 11.2: bounded text is byte-equivalent and digest matches."""

    def test_region_occurrence_carries_byte_equivalent_bounded_text(
        self,
        navigator: ProvenanceNavigator,
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
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        region_node = chain.region_occurrences[0]
        assert isinstance(region_node, RegionOccurrenceNode)

        # Byte-equivalent span and matching digest (Requirement 11.2).
        assert region_node.bounded_text == _EXPECTED_SPAN_BYTES
        assert region_node.start_offset_bytes == _DOC_SPAN_START
        assert region_node.end_offset_bytes == _DOC_SPAN_END
        assert region_node.span_byte_length == _DOC_SPAN_END - _DOC_SPAN_START

        digest = hashlib.sha256(region_node.bounded_text).hexdigest()
        assert digest == region_node.span_content_digest_sha256


# ---------------------------------------------------------------------------
# Idempotence (Requirement 11.5 / Property 8 example-based).
# ---------------------------------------------------------------------------


class TestIdempotence:
    """Requirement 11.5: repeated invocations return byte-equivalent chains."""

    def test_repeated_invocations_return_equal_chains(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """Five invocations of the same ``(D, P, t)`` compare equal.

        Property 8 (task 12.8) generalizes this to Hypothesis-driven
        random pipelines; this test pins the example-based contract
        the property test relies on.
        """
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        chains = []
        for _ in range(5):
            with engine.connect() as conn:
                chains.append(
                    navigator.navigate_decision(
                        conn,
                        decision_id=pipeline.decision.decision_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EFFECTIVE_TIME,
                    )
                )

        first = chains[0]
        for other in chains[1:]:
            assert other == first


# ---------------------------------------------------------------------------
# Redaction per stage (authorization filtering).
# ---------------------------------------------------------------------------


class TestRedactionPerStage:
    """Each authorization check independently controls one stage's visibility.

    These tests pin the minimum redaction shape from task 12.2: a
    restricted node is replaced by a :class:`RedactedNode` carrying
    only the node kind. Full policy shape (gap descriptors, AD-WS-9
    policy markers) is task 12.4 — these tests therefore intentionally
    do *not* assert on disclosure-policy attributes.
    """

    def test_missing_recommendation_view_redacts_recommendation_only(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        # Grant every scope EXCEPT the Recommendation.
        _assign_view_role(authorization_service, engine, scope=_SCOPE)
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.finding.finding_id,
        )
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.document_resource_id,
        )

        with engine.connect() as conn:
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        # Decision visible; Recommendation Revision redacted;
        # downstream visible (restrictions cascade by record, not by
        # branch).
        assert isinstance(chain.decision, DecisionNode)
        assert chain.recommendation_revision == RedactedNode(
            kind="recommendation_revision"
        )
        assert isinstance(chain.findings[0], FindingRevisionNode)
        assert isinstance(chain.region_occurrences[0], RegionOccurrenceNode)
        assert isinstance(chain.document_revisions[0], DocumentRevisionNode)

    def test_missing_finding_view_redacts_finding_and_drops_supports(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """A redacted Finding Revision contributes nothing downstream.

        Per the navigator's branch-restriction semantics, a Party
        without authority on a Finding Revision sees the Finding
        replaced by a redaction marker AND does not receive the
        Region Occurrence or Document Revision rows the Finding's
        Supports links would have produced. This keeps the
        per-Finding sub-chain identifiable as a coherent unit.
        """
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        # Grant every scope EXCEPT the Finding.
        _assign_view_role(authorization_service, engine, scope=_SCOPE)
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.recommendation.recommendation_id,
        )
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.document_resource_id,
        )

        with engine.connect() as conn:
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert chain.findings == (RedactedNode(kind="finding_revision"),)
        assert chain.region_occurrences == ()
        assert chain.document_revisions == ()

    def test_missing_document_view_redacts_region_and_document(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """Missing view on the Document scope redacts both leaves.

        The scope chosen by the navigator for both the Region
        Occurrence and the Document Revision is the
        ``Document_Revisions.resource_id`` of the owning Source
        Document. Withholding that scope therefore redacts both
        leaves of the chain.
        """
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _assign_view_role(authorization_service, engine, scope=_SCOPE)
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.recommendation.recommendation_id,
        )
        _assign_view_role(
            authorization_service,
            engine,
            scope=pipeline.finding.finding_id,
        )

        with engine.connect() as conn:
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert chain.region_occurrences == (
            RedactedNode(kind="region_occurrence"),
        )
        assert chain.document_revisions == (
            RedactedNode(kind="document_revision"),
        )


# ---------------------------------------------------------------------------
# Wildcard authority.
# ---------------------------------------------------------------------------


class TestWildcardAuthority:
    """A wildcard role assignment yields the same chain as fine-grained roles."""

    def test_wildcard_view_sees_full_chain(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _assign_view_role(authorization_service, engine, scope="*")

        with engine.connect() as conn:
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(chain.decision, DecisionNode)
        assert isinstance(chain.recommendation_revision, RecommendationRevisionNode)
        assert isinstance(chain.findings[0], FindingRevisionNode)
        assert isinstance(chain.region_occurrences[0], RegionOccurrenceNode)
        assert isinstance(chain.document_revisions[0], DocumentRevisionNode)


# ---------------------------------------------------------------------------
# Clock fallback when `at` omitted.
# ---------------------------------------------------------------------------


class TestClockFallback:
    """When ``at`` is omitted the navigator consults the injected Clock."""

    def test_omitted_at_uses_navigator_clock(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """The conftest ``clock`` fixture is fixed at 2026-01-01.

        The seeded role assignments use ``effective_start = 2026-01-01``
        so the clock-supplied ``at`` falls exactly at the boundary
        (not-yet-effective would surface as redaction). The happy
        path proves the clock-injection path works.
        """
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _assign_view_role(authorization_service, engine, scope="*")

        with engine.connect() as conn:
            chain = navigator.navigate_decision(
                conn,
                decision_id=pipeline.decision.decision_id,
                party_id=_REQUESTER_PARTY_ID,
                # No ``at`` — must be sourced from navigator.clock.
            )

        assert isinstance(chain.decision, DecisionNode)
        assert chain.decision.decision_id == pipeline.decision.decision_id
