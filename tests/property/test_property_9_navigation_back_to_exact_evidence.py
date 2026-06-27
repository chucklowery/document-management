# Feature: first-walking-slice, Property 9: Navigation back to exact Evidence
"""Property 9 — Navigation back to exact Evidence (task 12.9).

**Property 9: Navigation back to exact Evidence**

For all Decision Immutable Records whose provenance is fully visible
to the requesting Party, the
:meth:`walking_slice.provenance.ProvenanceNavigator.navigate_decision`
result SHALL surface each Region Occurrence node with:

- the start anchor, end anchor, and bounded-text span carrying the
  bytes byte-equivalent to ``content_bytes[start:end]`` of the
  resolved Document Revision (Requirement 11.2, 3.4);
- a ``span_content_digest_sha256`` equal to the SHA-256 of
  ``bounded_text`` and equal to the value recorded on the
  ``Region_Occurrences`` row at occurrence-creation time
  (Requirement 11.2 — Region Occurrence resolves to byte-equivalent
  bounded text and a matching digest);
- a ``span_byte_length`` equal to ``end - start``.

The chain itself MUST contain the full five stages — Decision →
Recommendation Revision → Finding Revision(s) → Region Occurrence(s)
→ Document Revision — with no :class:`RedactedNode` in any position
(Requirement 11.1 — provenance is "fully visible" by hypothesis of
this property).

**Validates: Requirements 3.4, 11.1, 11.2, 15.9**

Strategy:

Each Hypothesis case draws 1..3 *Decision-chain scenarios*. Each
scenario carries one Source Document's random ``content_bytes``,
1..3 supporting span ranges ``(start, end)`` against those bytes,
and a non-empty Finding statement. The Finding is always non-
hypothesis so it requires at least one ``Supports`` Relationship
(Requirement 4.1 / 4.3); every span in the scenario becomes one
``Supports`` Relationship, and the resulting Recommendation is
finalized by one Decision.

Per generated case the test spins up a fresh per-test SQLite engine
on a unique :class:`tempfile.TemporaryDirectory` path so cross-case
state cannot contaminate the byte-equivalence checks. For each
scenario it:

1. Creates one Source Document via :class:`EvidenceRepository`
   (one row in ``Source_Documents`` + one in ``Document_Revisions``).
2. Anchors one Region Occurrence per supporting span via
   :meth:`EvidenceRepository.create_region_occurrence` (each
   Occurrence records ``start_offset_bytes``, ``end_offset_bytes``,
   ``span_byte_length``, and ``span_content_digest_sha256``).
3. Records one non-hypothesis Finding citing every Occurrence as a
   ``Supports`` reference (Requirement 4.5 — one Supports
   Relationship per cited Occurrence).
4. Records one Recommendation derived from that Finding.
5. Records one Decision addressing that Recommendation Revision
   (no Decision-level authorization wired so seeding is independent
   of role assignments; the requesting Party's view authority is the
   authority dimension this property exercises).

A single wildcard ``view`` Role Assignment (``scope='*'``) is granted
to the requesting Party so every navigation node is unredacted —
"fully visible" by construction.

Then for every persisted Decision the test invokes
:meth:`ProvenanceNavigator.navigate_decision` and asserts the chain
shape and per-Region-Occurrence byte-equivalence invariants:

- The chain's ``decision``, ``recommendation_revision``, and every
  entry in ``findings``, ``region_occurrences``, and
  ``document_revisions`` is a *visible* node (not a
  :class:`RedactedNode`).
- The cardinalities match the scenario: one Recommendation, one
  Finding, ``len(spans)`` Region Occurrences, and
  ``len(spans)`` Document Revision entries (one per ``Supports``).
- For every :class:`RegionOccurrenceNode`:
  - ``bounded_text == content_bytes[start_offset_bytes:end_offset_bytes]``
    of the originating Document Revision (the bytes the scenario
    fed into ``create_document``).
  - ``sha256(bounded_text).hexdigest() ==
    span_content_digest_sha256`` (Requirement 11.2 / 15.9 — digest
    matches at navigation time).
  - The ``span_content_digest_sha256`` on the node also equals the
    ``Region_Occurrences.span_content_digest_sha256`` persisted at
    creation time, recomputed independently from the scenario
    inputs.
  - ``span_byte_length == end_offset_bytes - start_offset_bytes``.

The property is read-only after seeding — it never modifies a
persisted Region Occurrence or Document Revision — so it relies on
the AD-WS-4 immutability of those tables for its byte-equivalence
guarantees.

Test scaffolding follows the conventions of
:mod:`tests.property.test_property_1_evidence_support` and
:mod:`tests.property.test_property_2_decision_authority`: a
:class:`tempfile.TemporaryDirectory` owns the per-case SQLite file
(so state cannot leak between Hypothesis cases the way a
function-scoped pytest fixture would), and pragma-aware engine setup
matches the :mod:`tests.conftest` fixtures exactly.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.evidence import (
    CreateDocumentResult,
    CreateRegionResult,
    EvidenceRepository,
)
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateDecisionResult,
    CreateFindingResult,
    CreateRecommendationResult,
    KnowledgeService,
    SupportRef,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.provenance import (
    DecisionNode,
    DocumentRevisionNode,
    FindingRevisionNode,
    ProvenanceNavigator,
    RecommendationRevisionNode,
    RedactedNode,
    RegionOccurrenceNode,
)
from sqlalchemy import text


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# A single :class:`FixedClock` instant anchors every persisted
# ``recorded_at`` (Documents, Regions, Findings, Recommendations,
# Decisions, Audit Records, Role Assignments). The Decision
# evaluation time used by the navigator falls *after* the role
# assignment's ``effective_start`` so the wildcard view authority is
# always effective at navigation time.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_EFFECTIVE_TIME: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_PARTY_CREATED_AT: Final[str] = "2026-01-01T00:00:00.000Z"


# Authoring / requesting / assigning Party identities. The authoring
# Party contributes every Document, Finding, Recommendation, and
# Decision in the scenario. The requesting Party is the navigator's
# caller and the holder of the wildcard view authority. The
# assigning-authority Party records the Role Assignment.
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000d0001"
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000d0002"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000d0003"

_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-0000000d00a1"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)


# Applicable scope recorded on every Decision in the case. The scope
# itself is irrelevant to Property 9 (the requesting Party holds a
# wildcard view authority that covers every scope), but the column
# is NOT NULL on ``Decisions.applicable_scope`` so a non-empty value
# must be supplied.
_DECISION_SCOPE: Final[str] = "property-9-scope"


def _seed_party(connection, party_id: str, display: str) -> None:
    """Insert a Party row required by the FK constraints.

    The schema FKs on ``Document_Revisions.contributing_party_id``,
    ``Finding_Revisions.authoring_party_id``,
    ``Recommendation_Revisions.authoring_party_id``,
    ``Decisions.deciding_party_id``, ``Role_Assignments.party_id``,
    ``Relationships.authoring_party_id``, and
    ``Audit_Records.actor_party_id`` all resolve back to ``Parties``.
    Every Party used by the scenario is seeded before any other
    write.
    """
    connection.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# A *Decision-chain scenario* is one full Evidence → Decision pipeline:
# one Source Document with random bytes, 1..3 supporting spans against
# those bytes, one non-hypothesis Finding citing every span, one
# Recommendation derived from that Finding, and one Decision
# addressing that Recommendation Revision. The shape mirrors
# :mod:`tests.unit.test_provenance_navigate_decision`'s ``_seed_pipeline``
# helper (which seeds exactly one chain) extended to draw the document
# bytes, span ranges, and statement randomly.
# ---------------------------------------------------------------------------


@st.composite
def _span_strategy(draw, *, content_length: int) -> tuple[int, int]:
    """Draw a valid ``(start, end)`` span inside a ``content_length``-byte buffer.

    Constraints (Requirement 3.5 and AD-WS-6):

    - ``0 <= start_offset_bytes``
    - ``start_offset_bytes < end_offset_bytes``
    - ``end_offset_bytes <= content_length``

    The strategy first draws ``start`` from ``[0, content_length - 1]``
    (so at least one byte remains for ``end``), then draws ``end`` from
    ``[start + 1, content_length]``. Both endpoints are inclusive in
    Hypothesis's ``integers`` bounds so the test exercises boundary
    cases (1-byte span at offset 0, 1-byte span at the end, full-length
    span) when Hypothesis explores them.
    """
    start = draw(st.integers(min_value=0, max_value=content_length - 1))
    end = draw(st.integers(min_value=start + 1, max_value=content_length))
    return (start, end)


@st.composite
def _chain_scenario(draw) -> dict:
    """Draw a single Decision-chain scenario.

    Returns a dict with keys:

    - ``content_bytes`` (``bytes``): Source Document content; 1..256
      bytes drawn from arbitrary byte values so the property is
      exercised against the full SQLite BLOB alphabet (not just
      printable ASCII).
    - ``spans`` (``list[tuple[int, int]]``): 1..3 distinct supporting
      span ranges, each valid for ``content_bytes`` by construction.
      Distinct spans within one scenario keep Region Identities
      distinct (the Evidence Repository deduplicates Regions by
      ``span_content_digest_sha256`` per AD-WS-2; byte-distinct spans
      always produce distinct digests for non-empty content). See
      :mod:`tests.property.test_property_1_evidence_support` for the
      same constraint and rationale.
    - ``statement`` (``str``): non-empty Finding statement (1..128
      chars). The statement value is irrelevant to Property 9 — only
      the bytes anchored by the Region Occurrences are asserted on —
      but the column is NOT NULL on ``Finding_Revisions.statement``
      so a non-empty value is supplied.
    """
    content_length = draw(st.integers(min_value=1, max_value=256))
    content_bytes = draw(
        st.binary(min_size=content_length, max_size=content_length)
    )
    spans = draw(
        st.lists(
            _span_strategy(content_length=content_length),
            min_size=1,
            max_size=3,
            unique=True,
        )
    )
    statement = draw(st.text(min_size=1, max_size=128))
    return {
        "content_bytes": content_bytes,
        "spans": spans,
        "statement": statement,
    }


_chain_scenarios = st.lists(
    _chain_scenario(),
    min_size=1,
    max_size=3,
)


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, Source_Documents rows, Findings rows,
# Recommendations rows, Decisions rows, Relationships rows, and audit
# rows cannot leak between cases (design §"Testing Strategy" — "Each
# property and example test gets a fresh SQLite database"). A
# :class:`tempfile.TemporaryDirectory` context inside the test body
# owns the per-case directory; Hypothesis disallows function-scoped
# pytest fixtures for per-case state because they would not reset
# between generated inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 9: Navigation back to exact Evidence
@given(scenarios=_chain_scenarios)
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates a fresh temp directory and a fresh SQLite
    # database so per-case setup is more expensive than a pure
    # in-memory property test. The setup is still well under the
    # 2000 ms deadline locally but we suppress the data-generation
    # health check so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_navigation_back_to_exact_evidence(scenarios: list[dict]) -> None:
    """Every navigated Decision chain surfaces Region Occurrence nodes
    whose ``bounded_text`` equals ``content_bytes[start:end]`` of the
    resolved Document Revision and whose digest equals the recorded
    ``span_content_digest_sha256``.

    Validates Requirements 3.4, 11.1, 11.2, and 15.9.
    """
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop9_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh services per case so :class:`IdentityService` in-memory
        # state cannot bleed across cases. The pinned :class:`FixedClock`
        # makes every persisted ``recorded_at`` deterministic across
        # Hypothesis shrinks.
        clock = FixedClock(_NOW)
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        authorization_service = AuthorizationService(
            clock=clock,
            audit_log=audit_log,
            identity_service=identity_service,
        )
        evidence_repository = EvidenceRepository(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        # Unwired :class:`KnowledgeService` — the Decision-Maker
        # authority check (task 8.2) is not under test here. The
        # requesting Party's view authority is. Using the unwired path
        # keeps seeding independent of the role assignments that drive
        # the visibility precondition the property requires.
        knowledge_service = KnowledgeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        navigator = ProvenanceNavigator(
            clock=clock,
            authorization_service=authorization_service,
        )

        # Track per-scenario state so post-persist assertions can verify
        # each Decision's chain against the inputs that produced it.
        #
        # Each entry: {
        #   "decision": CreateDecisionResult,
        #   "recommendation": CreateRecommendationResult,
        #   "finding": CreateFindingResult,
        #   "document_resource_id": str,
        #   "document_revision_id": str,
        #   "content_bytes": bytes,
        #   "expected_regions": list[{
        #       "region_id": str,
        #       "start": int,
        #       "end": int,
        #       "expected_bytes": bytes,
        #       "expected_digest": str,  # lowercase-hex SHA-256
        #   }],
        # }
        persisted: list[dict] = []

        try:
            # 1. Seed all Parties (authoring + requester + assigning
            #    authority). One transaction keeps the FK targets
            #    visible to every later write.
            with engine.begin() as conn:
                _seed_party(conn, _AUTHORING_PARTY_ID, "Property 9 Author")
                _seed_party(conn, _REQUESTER_PARTY_ID, "Property 9 Reader")
                _seed_party(
                    conn,
                    _ASSIGNING_AUTHORITY_ID,
                    "Property 9 Assigning Authority",
                )

            # 2. Grant the requesting Party a wildcard view authority
            #    so the navigator sees every node unredacted. The
            #    "fully visible" precondition of the property is
            #    satisfied by this one Role Assignment. ``view`` is
            #    the only authority granted — Requirement 12.4 forbids
            #    substituting view for modify or approve, so this
            #    assignment cannot accidentally authorize Decisions
            #    elsewhere in the slice.
            with engine.begin() as conn:
                authorization_service.assign_role(
                    conn,
                    AssignRoleRequest(
                        party_id=_REQUESTER_PARTY_ID,
                        role_name="reviewer",
                        scope="*",
                        authorities_granted=("view",),
                        effective_start=_NOW,
                        effective_end=None,
                        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
                    ),
                )

            # 3. Seed every Decision-chain scenario. Each scenario's
            #    full pipeline (Document + Region Occurrences +
            #    Finding + Recommendation + Decision + Audit rows +
            #    Provenance Manifest) happens in one transaction so
            #    the AD-WS-5 "consequential audit appends in the
            #    originating transaction" invariant is exercised
            #    end-to-end and the property assertions read a
            #    committed state.
            for scenario in scenarios:
                content_bytes: bytes = scenario["content_bytes"]
                spans: list[tuple[int, int]] = scenario["spans"]
                statement: str = scenario["statement"]

                with engine.begin() as conn:
                    doc: CreateDocumentResult = (
                        evidence_repository.create_document(
                            conn,
                            content_bytes=content_bytes,
                            contributing_party_id=_AUTHORING_PARTY_ID,
                            authority="authoritative",
                        )
                    )

                    region_results: list[CreateRegionResult] = []
                    for start, end in spans:
                        region = evidence_repository.create_region_occurrence(
                            conn,
                            resource_id=doc.resource_id,
                            revision_id=doc.revision_id,
                            start_offset_bytes=start,
                            end_offset_bytes=end,
                            contributing_party_id=_AUTHORING_PARTY_ID,
                        )
                        region_results.append(region)

                    supports = tuple(
                        SupportRef(
                            region_id=region.region_id,
                            document_revision_id=doc.revision_id,
                        )
                        for region in region_results
                    )

                    finding: CreateFindingResult = (
                        knowledge_service.create_finding(
                            conn,
                            statement=statement,
                            authoring_party_id=_AUTHORING_PARTY_ID,
                            is_hypothesis=False,
                            supporting_region_occurrences=supports,
                        )
                    )

                    recommendation: CreateRecommendationResult = (
                        knowledge_service.create_recommendation(
                            conn,
                            authoring_party_id=_AUTHORING_PARTY_ID,
                            derived_from_findings=[finding.finding_id],
                            rationale="Property 9 recommendation.",
                        )
                    )

                    decision: CreateDecisionResult = (
                        knowledge_service.create_decision(
                            conn,
                            target_recommendation_id=(
                                recommendation.recommendation_id
                            ),
                            target_recommendation_revision_id=(
                                recommendation.recommendation_revision_id
                            ),
                            outcome="Accept",
                            rationale="Property 9 decision.",
                            deciding_party_id=_AUTHORING_PARTY_ID,
                            authority_basis=_AUTHORITY_BASIS,
                            applicable_scope=_DECISION_SCOPE,
                        )
                    )

                expected_regions = [
                    {
                        "region_id": region.region_id,
                        "start": region.start_offset_bytes,
                        "end": region.end_offset_bytes,
                        # Slice the original scenario bytes
                        # independently of the Evidence_Repository so
                        # the assertion below catches any divergence
                        # between what the navigator returns and what
                        # the caller submitted.
                        "expected_bytes": content_bytes[
                            region.start_offset_bytes : region.end_offset_bytes
                        ],
                        # Recompute the digest from the scenario bytes
                        # so the assertion does not trust the
                        # Evidence_Repository's recorded value as a
                        # source of truth.
                        "expected_digest": hashlib.sha256(
                            content_bytes[
                                region.start_offset_bytes : region.end_offset_bytes
                            ]
                        ).hexdigest(),
                    }
                    for region in region_results
                ]
                persisted.append(
                    {
                        "decision": decision,
                        "recommendation": recommendation,
                        "finding": finding,
                        "document_resource_id": doc.resource_id,
                        "document_revision_id": doc.revision_id,
                        "content_bytes": content_bytes,
                        "expected_regions": expected_regions,
                    }
                )

            # ----- Property assertions -------------------------------
            #
            # For every persisted Decision, navigate the chain with
            # full view authority and assert the Region Occurrence
            # nodes return byte-equivalent spans whose digests match
            # the recorded ``span_content_digest_sha256``.
            for entry in persisted:
                decision: CreateDecisionResult = entry["decision"]
                recommendation: CreateRecommendationResult = entry[
                    "recommendation"
                ]
                finding: CreateFindingResult = entry["finding"]
                expected_regions = entry["expected_regions"]

                with engine.connect() as conn:
                    chain = navigator.navigate_decision(
                        conn,
                        decision_id=decision.decision_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EFFECTIVE_TIME,
                    )

                # ---- Chain shape (Requirement 11.1) -----------------
                # Requirement 11.1 names five stages; the property's
                # "fully visible" hypothesis means none of them is a
                # :class:`RedactedNode`.
                assert isinstance(chain.decision, DecisionNode), (
                    "Decision head of chain must be a visible "
                    "DecisionNode under wildcard view authority; "
                    f"got {type(chain.decision).__name__}."
                )
                assert chain.decision.decision_id == decision.decision_id

                assert isinstance(
                    chain.recommendation_revision,
                    RecommendationRevisionNode,
                ), (
                    "Recommendation Revision must be a visible "
                    "RecommendationRevisionNode under wildcard view "
                    "authority; got "
                    f"{type(chain.recommendation_revision).__name__}."
                )
                assert chain.recommendation_revision.recommendation_id == (
                    recommendation.recommendation_id
                )
                assert (
                    chain.recommendation_revision.recommendation_revision_id
                    == recommendation.recommendation_revision_id
                )

                # The scenario seeded exactly one Finding, so the
                # chain carries exactly one Finding Revision node.
                assert len(chain.findings) == 1, (
                    "Property 9 scenario seeds one Finding per "
                    "Decision; the chain should carry exactly one "
                    f"Finding entry, got {len(chain.findings)}."
                )
                finding_node = chain.findings[0]
                assert isinstance(finding_node, FindingRevisionNode), (
                    "Finding Revision must be a visible "
                    "FindingRevisionNode under wildcard view "
                    f"authority; got {type(finding_node).__name__}."
                )
                assert finding_node.finding_id == finding.finding_id
                assert (
                    finding_node.finding_revision_id
                    == finding.finding_revision_id
                )

                # One Region Occurrence node per ``Supports``
                # Relationship (Requirement 4.5); each Supports row
                # corresponds to one scenario span.
                assert len(chain.region_occurrences) == len(
                    expected_regions
                ), (
                    "Property 9 expects one Region Occurrence entry "
                    f"per scenario span (got {len(expected_regions)} "
                    "spans, "
                    f"{len(chain.region_occurrences)} region nodes)."
                )
                # Document Revision entries are positionally aligned
                # with Region Occurrence entries (design §"Provenance
                # traversal algorithm").
                assert len(chain.document_revisions) == len(
                    chain.region_occurrences
                ), (
                    "DecisionProvenanceChain invariant: "
                    "len(document_revisions) == len(region_occurrences)."
                )

                # ---- Index the chain's Region Occurrences by
                # ``region_id`` so the per-span assertions below can
                # locate each node without depending on iteration
                # order. The scenario draws distinct spans
                # (``unique=True`` on the span strategy), so the
                # ``Region_Occurrences`` rows have distinct
                # ``region_id`` values and the indexing is
                # unambiguous.
                region_node_by_id: dict[str, RegionOccurrenceNode] = {}
                doc_node_by_region_id: dict[str, DocumentRevisionNode] = {}
                for region_node, doc_node in zip(
                    chain.region_occurrences,
                    chain.document_revisions,
                ):
                    assert isinstance(region_node, RegionOccurrenceNode), (
                        "Region Occurrence must be a visible "
                        "RegionOccurrenceNode under wildcard view "
                        f"authority; got {type(region_node).__name__}."
                    )
                    assert isinstance(doc_node, DocumentRevisionNode), (
                        "Document Revision must be a visible "
                        "DocumentRevisionNode under wildcard view "
                        f"authority; got {type(doc_node).__name__}."
                    )
                    region_node_by_id[region_node.region_id] = region_node
                    doc_node_by_region_id[region_node.region_id] = doc_node

                # ---- Per-Region Occurrence assertions ----------------
                # Requirement 11.2 / Requirement 3.4 / Property 9:
                # for every Region Occurrence node the navigator
                # returns, the span fields are present, the digest
                # equals the recorded ``span_content_digest_sha256``,
                # and the returned bytes are byte-equivalent to
                # ``content_bytes[start:end]`` of the resolved
                # Document Revision.
                for expected in expected_regions:
                    region_id = expected["region_id"]
                    assert region_id in region_node_by_id, (
                        f"Region Occurrence region_id={region_id!r} "
                        "anchored by the scenario is missing from the "
                        "navigated chain; Requirement 11.1 / 11.2 "
                        "require every Supports-cited Occurrence to "
                        "surface in the provenance chain."
                    )
                    region_node = region_node_by_id[region_id]

                    # 1. Span fields are present and consistent with
                    #    the scenario inputs. ``span_byte_length``
                    #    must equal ``end - start`` so the on-the-wire
                    #    arithmetic matches the Region_Occurrences
                    #    schema CHECK.
                    assert region_node.start_offset_bytes == expected["start"]
                    assert region_node.end_offset_bytes == expected["end"]
                    assert region_node.span_byte_length == (
                        expected["end"] - expected["start"]
                    )

                    # 2. Returned bytes are byte-equivalent to
                    #    ``content_bytes[start:end]`` of the resolved
                    #    Document Revision. The expected bytes were
                    #    sliced directly from the scenario's original
                    #    ``content_bytes`` (independently of the
                    #    Evidence_Repository) so this assertion
                    #    catches any divergence between submitted and
                    #    navigated bytes — Requirement 3.4 / 11.2.
                    assert region_node.bounded_text == expected[
                        "expected_bytes"
                    ], (
                        f"Region Occurrence region_id={region_id!r} "
                        "returned bounded_text that diverges from "
                        "content_bytes[start:end] of the resolved "
                        "Document Revision; Requirement 3.4 / 11.2 "
                        "require byte-equivalence."
                    )

                    # 3. Digest equals SHA-256 of the returned bytes
                    #    (Requirement 11.2 / 15.9). Compute the
                    #    digest on the spot rather than trusting the
                    #    node's surfaced value blindly.
                    computed_digest = hashlib.sha256(
                        region_node.bounded_text
                    ).hexdigest()
                    assert (
                        computed_digest
                        == region_node.span_content_digest_sha256
                    ), (
                        f"Region Occurrence region_id={region_id!r} "
                        "span_content_digest_sha256 does not equal "
                        "SHA-256(bounded_text); Requirement 11.2 / "
                        "15.9 require digest-equivalence at "
                        "navigation time. "
                        f"computed={computed_digest!r}, "
                        f"node={region_node.span_content_digest_sha256!r}."
                    )

                    # 4. Digest equals the value recorded on the
                    #    ``Region_Occurrences`` row at occurrence-
                    #    creation time — recomputed independently
                    #    from the scenario inputs above so the
                    #    assertion does not transit through any
                    #    persisted column.
                    assert (
                        region_node.span_content_digest_sha256
                        == expected["expected_digest"]
                    ), (
                        f"Region Occurrence region_id={region_id!r} "
                        "span_content_digest_sha256 diverges from "
                        "SHA-256 of the scenario span bytes; "
                        "Property 9 requires the navigated digest to "
                        "match the digest the Evidence_Repository "
                        "recorded at occurrence creation. "
                        f"navigated="
                        f"{region_node.span_content_digest_sha256!r}, "
                        f"expected={expected['expected_digest']!r}."
                    )

                    # 5. The paired Document Revision node points at
                    #    the Document the scenario seeded. Each
                    #    Supports row anchors a Region Occurrence to
                    #    its owning Document Revision; the chain's
                    #    positional alignment between
                    #    ``region_occurrences`` and
                    #    ``document_revisions`` is part of the
                    #    Requirement-11.1 contract.
                    doc_node = doc_node_by_region_id[region_id]
                    assert doc_node.revision_id == (
                        entry["document_revision_id"]
                    )
                    assert doc_node.resource_id == (
                        entry["document_resource_id"]
                    )
        finally:
            engine.dispose()
