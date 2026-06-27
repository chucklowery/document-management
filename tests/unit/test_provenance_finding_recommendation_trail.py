"""Unit tests for the task 12.5 navigator traversal methods.

Covers :meth:`ProvenanceNavigator.navigate_finding`,
:meth:`ProvenanceNavigator.navigate_recommendation`, and
:meth:`ProvenanceNavigator.navigate_trail_revision`, exercising the
contract established in task 12.5, design §"Provenance_Navigator" HTTP
surface, Requirement 10.4 (provenance of Finding, Recommendation,
Decision, or Trail Revision returns recorded sources and Omission
Entries), Requirement 11.1 (end-to-end traversal from synthesis nodes
back to exact Evidence), and Requirement 11.7 (indistinguishable
not-found response for unresolvable or restricted subjects).

Test scope:
    - Happy path: full chain returned with all stages populated.
    - Unresolvable head subject raises the typed
      ``...UnresolvableError`` exception.
    - Authorization denial at the head raises the same exception so
      the response form is indistinguishable from the unresolvable
      case (Requirement 11.7 / design pseudocode
      ``not_found_indistinguishable_response``).
    - Idempotence: repeated invocations return byte-equivalent chains
      (Property 8 example form; the full Hypothesis-driven version is
      task 12.8).

Authorization filtering of intermediate nodes (Region Occurrence,
Document Revision) is already exercised by
``test_provenance_navigate_decision.py``'s ``TestRedactionPerStage``;
this file pins only the new head-subject traversals.
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
    DecisionProvenanceChain,
    DocumentRevisionNode,
    FindingProvenanceChain,
    FindingRevisionNode,
    FindingUnresolvableError,
    ProvenanceNavigator,
    RecommendationProvenanceChain,
    RecommendationRevisionNode,
    RecommendationUnresolvableError,
    RegionOccurrenceNode,
    TrailProvenanceChain,
    TrailRevisionUnresolvableError,
)
from walking_slice.trails import TrailService, TrailStepInput


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_DECIDING_PARTY_ID = "00000000-0000-7000-8000-0000000f0001"
_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000f0002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000f0003"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000f00a1")

_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

_DOC_CONTENT = b"the quick brown fox jumps over the lazy dog repeatedly."
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
) -> KnowledgeService:
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
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Pipeline builder.
# ---------------------------------------------------------------------------


class SeededPipeline:
    """Bundle of identifiers for one Evidence → Decision pipeline."""

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
) -> SeededPipeline:
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
            statement="The quick brown fox is documented.",
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
            rationale="Accept recommendation.",
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
    """Grant the requesting Party view authority on every chain stage."""
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
# navigate_finding (Requirement 10.4 / 11.1 / 11.7).
# ---------------------------------------------------------------------------


class TestNavigateFinding:
    """Surface returns the Finding Revision plus its supporting Evidence."""

    def test_happy_path_returns_finding_with_regions_and_documents(
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
            chain = navigator.navigate_finding(
                conn,
                finding_id=pipeline.finding.finding_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(chain, FindingProvenanceChain)
        assert isinstance(chain.finding_revision, FindingRevisionNode)
        assert chain.finding_revision.finding_id == pipeline.finding.finding_id
        assert chain.finding_revision.finding_revision_id == (
            pipeline.finding.finding_revision_id
        )
        assert len(chain.region_occurrences) == 1
        assert isinstance(chain.region_occurrences[0], RegionOccurrenceNode)
        assert chain.region_occurrences[0].region_id == pipeline.region_id
        assert len(chain.document_revisions) == 1
        assert isinstance(chain.document_revisions[0], DocumentRevisionNode)
        assert chain.document_revisions[0].revision_id == (
            pipeline.document_revision_id
        )
        assert chain.requested_finding_id == pipeline.finding.finding_id

    def test_unknown_finding_id_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        engine: Engine,
        audit_log: AuditLog,  # forces schema creation
    ) -> None:
        unknown = "00000000-0000-7000-8000-00000000ffff"
        with engine.connect() as conn:
            with pytest.raises(FindingUnresolvableError) as exc:
                navigator.navigate_finding(
                    conn,
                    finding_id=unknown,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.finding_id == unknown

    def test_finding_level_denial_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        """Indistinguishable not-found response per Requirement 11.7."""
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        # No role assignments — denial on the head subject.
        with engine.connect() as conn:
            with pytest.raises(FindingUnresolvableError) as exc:
                navigator.navigate_finding(
                    conn,
                    finding_id=pipeline.finding.finding_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.finding_id == pipeline.finding.finding_id

    def test_repeated_invocations_return_equal_chains(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        engine: Engine,
    ) -> None:
        """Idempotence (Requirement 11.5 / Property 8, example form)."""
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        _grant_full_view_authority(authorization_service, engine, pipeline)

        chains = []
        for _ in range(5):
            with engine.connect() as conn:
                chains.append(
                    navigator.navigate_finding(
                        conn,
                        finding_id=pipeline.finding.finding_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EFFECTIVE_TIME,
                    )
                )
        first = chains[0]
        for other in chains[1:]:
            assert other == first


# ---------------------------------------------------------------------------
# navigate_recommendation (Requirement 10.4 / 11.1 / 11.7).
# ---------------------------------------------------------------------------


class TestNavigateRecommendation:
    """Surface returns the Recommendation Revision plus the deeper chain."""

    def test_happy_path_returns_recommendation_with_findings_and_evidence(
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
            chain = navigator.navigate_recommendation(
                conn,
                recommendation_id=pipeline.recommendation.recommendation_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(chain, RecommendationProvenanceChain)
        assert isinstance(
            chain.recommendation_revision, RecommendationRevisionNode
        )
        assert chain.recommendation_revision.recommendation_id == (
            pipeline.recommendation.recommendation_id
        )
        # One Derived From → one Finding → one Supports → one
        # Region Occurrence → one Document Revision.
        assert len(chain.findings) == 1
        assert isinstance(chain.findings[0], FindingRevisionNode)
        assert chain.findings[0].finding_id == pipeline.finding.finding_id
        assert len(chain.region_occurrences) == 1
        assert isinstance(chain.region_occurrences[0], RegionOccurrenceNode)
        assert len(chain.document_revisions) == 1
        assert isinstance(chain.document_revisions[0], DocumentRevisionNode)
        assert chain.requested_recommendation_id == (
            pipeline.recommendation.recommendation_id
        )

    def test_unknown_recommendation_id_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        unknown = "00000000-0000-7000-8000-00000000ffff"
        with engine.connect() as conn:
            with pytest.raises(RecommendationUnresolvableError) as exc:
                navigator.navigate_recommendation(
                    conn,
                    recommendation_id=unknown,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.recommendation_id == unknown

    def test_recommendation_level_denial_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        with engine.connect() as conn:
            with pytest.raises(RecommendationUnresolvableError) as exc:
                navigator.navigate_recommendation(
                    conn,
                    recommendation_id=pipeline.recommendation.recommendation_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.recommendation_id == (
            pipeline.recommendation.recommendation_id
        )


# ---------------------------------------------------------------------------
# navigate_trail_revision (Requirement 10.4 / 11.1 / 11.7).
# ---------------------------------------------------------------------------


class TestNavigateTrailRevision:
    """Surface returns the Trail Revision, five steps, and Decision chain."""

    def _seed_trail(
        self,
        *,
        clock: Clock,
        identity_service: IdentityService,
        audit_log: AuditLog,
        engine: Engine,
        pipeline: SeededPipeline,
    ):
        trail_service = TrailService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        with engine.begin() as conn:
            result = trail_service.create_trail(
                conn,
                purpose="Walk Evidence to Decision.",
                audience_id="reviewers",
                steps=(
                    TrailStepInput(
                        ordinal=1,
                        target_kind="document_revision",
                        target_id=pipeline.document_resource_id,
                        target_revision_id=pipeline.document_revision_id,
                    ),
                    TrailStepInput(
                        ordinal=2,
                        target_kind="region_occurrence",
                        target_id=pipeline.document_revision_id,
                        region_id=pipeline.region_id,
                    ),
                    TrailStepInput(
                        ordinal=3,
                        target_kind="finding_revision",
                        target_id=pipeline.finding.finding_id,
                        target_revision_id=pipeline.finding.finding_revision_id,
                    ),
                    TrailStepInput(
                        ordinal=4,
                        target_kind="recommendation_revision",
                        target_id=pipeline.recommendation.recommendation_id,
                        target_revision_id=(
                            pipeline.recommendation.recommendation_revision_id
                        ),
                    ),
                    TrailStepInput(
                        ordinal=5,
                        target_kind="decision",
                        target_id=pipeline.decision.decision_id,
                    ),
                ),
                authoring_party_id=_DECIDING_PARTY_ID,
            )
        return result

    def test_happy_path_returns_trail_with_steps_and_decision_chain(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        authorization_service: AuthorizationService,
        clock: Clock,
        identity_service: IdentityService,
        audit_log: AuditLog,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        trail = self._seed_trail(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            engine=engine,
            pipeline=pipeline,
        )

        # Grant view authority on the Trail itself plus every stage so
        # the inner Decision chain populates.
        _assign_view_role(authorization_service, engine, scope=trail.trail_id)
        _grant_full_view_authority(authorization_service, engine, pipeline)

        with engine.connect() as conn:
            chain = navigator.navigate_trail_revision(
                conn,
                trail_id=trail.trail_id,
                trail_revision_id=trail.trail_revision_id,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(chain, TrailProvenanceChain)
        assert chain.trail_revision.trail_id == trail.trail_id
        assert chain.trail_revision.trail_revision_id == (
            trail.trail_revision_id
        )
        # Five steps in ordinal order.
        assert len(chain.steps) == 5
        assert [s.ordinal for s in chain.steps] == [1, 2, 3, 4, 5]
        # Inner Decision chain populated because the requesting Party
        # holds every needed view authority.
        assert isinstance(chain.decision_chain, DecisionProvenanceChain)
        assert chain.decision_chain.decision.decision_id == (
            pipeline.decision.decision_id
        )
        assert chain.requested_trail_id == trail.trail_id
        assert chain.requested_trail_revision_id == trail.trail_revision_id

    def test_unknown_trail_revision_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        engine: Engine,
        audit_log: AuditLog,
    ) -> None:
        unknown_trail = "00000000-0000-7000-8000-0000000affff"
        unknown_revision = "00000000-0000-7000-8000-0000000afff0"
        with engine.connect() as conn:
            with pytest.raises(TrailRevisionUnresolvableError) as exc:
                navigator.navigate_trail_revision(
                    conn,
                    trail_id=unknown_trail,
                    trail_revision_id=unknown_revision,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.trail_id == unknown_trail
        assert exc.value.trail_revision_id == unknown_revision

    def test_trail_level_denial_raises_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        clock: Clock,
        identity_service: IdentityService,
        audit_log: AuditLog,
        engine: Engine,
    ) -> None:
        _seed_required_parties(engine)
        pipeline = _seed_pipeline(
            engine, evidence_repository, knowledge_service
        )
        trail = self._seed_trail(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            engine=engine,
            pipeline=pipeline,
        )
        # No role assignment on the Trail scope → denial.
        with engine.connect() as conn:
            with pytest.raises(TrailRevisionUnresolvableError) as exc:
                navigator.navigate_trail_revision(
                    conn,
                    trail_id=trail.trail_id,
                    trail_revision_id=trail.trail_revision_id,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert exc.value.trail_id == trail.trail_id
        assert exc.value.trail_revision_id == trail.trail_revision_id
