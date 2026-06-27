# Feature: first-walking-slice, Property 8: Provenance traversal idempotence
"""Property 8 — Provenance traversal idempotence (task 12.8).

**Property 8: Provenance traversal idempotence**

For all Decision Records ``D``, requesting Parties ``P``, and
effective-time inputs ``t``, repeated invocations of provenance
traversal ``navigate(D, P, t)`` return equal results across at least 5
repetitions per generated case, where "equal results" means identical
node identities, identical node attribute values, and identical
ordering, provided the underlying Records and authority assignments
are unchanged.

**Validates: Requirements 11.5, 15.8**

Strategy:

The property is verified directly against
:meth:`walking_slice.provenance.ProvenanceNavigator.navigate_decision`,
the canonical Decision-to-Evidence traversal surface delivered by
task 12.2. Per Requirement 11.5 / design §"Provenance traversal
algorithm" the navigator is a deterministic function of
``(decision_id, party_id, at)`` over the immutable rows in
``Decisions``, ``Recommendation_Revisions``, ``Finding_Revisions``,
``Relationships``, ``Region_Occurrences``, and ``Document_Revisions``
plus the time-bounded effective-period semantics in
``Role_Assignments``; with all of those tables frozen for the
duration of one Hypothesis case the navigator's output is required
to be byte-equivalent across any number of repetitions.

Per case the test draws one scenario:

- ``content_bytes`` — a small bytes string (1..200 bytes) used as
  the source Document's ``content_bytes`` blob. The byte content
  drives :class:`RegionOccurrenceNode.bounded_text` and
  :class:`RegionOccurrenceNode.span_content_digest_sha256`; both
  must round-trip identically across invocations.
- ``start_offset_bytes`` / ``end_offset_bytes`` — a valid span
  ``0 <= start < end <= len(content_bytes)``.
- ``statement`` / ``rec_rationale`` / ``decision_rationale`` —
  non-empty free-text strings driving each pipeline node's
  attribute values.
- ``outcome`` — drawn from ``{"Accept", "Reject", "Defer"}`` per
  AD-WS-11.
- ``authority_axes`` — a subset of the five authority dimensions
  the navigator consults (Decision scope, Recommendation
  Revision, Finding Revision, Document resource, wildcard). The
  draw determines which Role Assignments are seeded for the
  requesting Party; varying this axis exercises every combination
  of visible and :class:`RedactedNode`-replaced stages, as well as
  the Decision-level denial path that raises
  :class:`DecisionUnresolvableError`.
- ``at`` — the effective time passed to ``navigate_decision``,
  drawn from a window spanning before, equal to, and after the
  pipeline's ``recorded_at`` so the test exercises the
  ``recorded_at <= at`` Finding-Revision selection rule and the
  ``effective_start <= at < effective_end`` Role-Assignment gate.

Per scenario the test:

1. Builds a fresh per-case SQLite engine + schema and seeds the
   minimum Parties (a deciding/contributing Party plus the
   requesting Party plus a Role-assigning authority Party).
2. Drives the full pipeline through the same Evidence_Repository
   and Knowledge_Service surfaces production uses, with a
   :class:`FixedClock` pinned to ``2026-01-01T00:00:00.000Z`` so
   every ``recorded_at`` is deterministic across shrinks.
3. Assigns the requesting Party's Role Assignments per
   ``authority_axes``.
4. Calls :meth:`ProvenanceNavigator.navigate_decision` exactly
   five times with the same ``(decision_id, party_id, at)``,
   collecting either the returned
   :class:`DecisionProvenanceChain` or the raised
   :class:`DecisionUnresolvableError`.
5. Asserts that all five outcomes compare equal — chains via the
   frozen-dataclass ``__eq__`` (which recursively compares every
   node identity, attribute value, and tuple ordering); exceptions
   via the carried ``decision_id`` and exception type. Mixing the
   two outcomes (one invocation returns a chain, another raises)
   is itself a falsification of Property 8.

Edge cases the strategy exercises automatically:

- Decision-level denial (``at`` before role effective start, or no
  Role Assignment drawn) → all five invocations raise
  :class:`DecisionUnresolvableError` with the same ``decision_id``.
- Partial visibility (one of Recommendation, Finding, Document
  scopes withheld) → :class:`RedactedNode` substituted at the
  corresponding stage; downstream nodes either remain visible
  (Recommendation withheld) or cascade to empty tuples (Finding
  withheld) per the navigator's branch-restriction semantics, and
  the substitution is identical across invocations.
- Wildcard authority (``scope='*'``) → full chain visible; the
  five invocations must still compare equal byte-for-byte.
- ``at`` falling on the exact ``effective_start`` boundary —
  Hypothesis chooses ``at == _NOW`` so the half-open
  ``[effective_start, effective_end)`` window's lower bound is
  exercised.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    KnowledgeService,
    SupportRef,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.provenance import (
    DecisionProvenanceChain,
    DecisionUnresolvableError,
    ProvenanceNavigator,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants — the clock anchor, the Party identities, and the
# Decision's ``applicable_scope``. Anchoring every Decision in the case
# to the same instant via :class:`FixedClock` keeps the property
# assertion deterministic across Hypothesis shrinks.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_TS_NOW: Final[str] = "2026-01-01T00:00:00.000Z"

_DECIDING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000d0001"
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000d0002"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000d0003"
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-0000000d00a1"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# The Decision's ``applicable_scope`` is a fixed string for the case
# because Property 8 quantifies over Decisions, requesting Parties, and
# ``at`` — not over the scope itself. Varying the scope orthogonally
# would not exercise additional idempotence behaviour: the navigator
# loads the Decision by ID, then evaluates view authority once at the
# Decision scope. Fixing the scope keeps the strategy small without
# losing coverage.
_DECISION_SCOPE: Final[str] = "pilot/property-8"

# Role-assignment effective period. ``effective_start = _NOW`` makes
# the role active for ``at >= _NOW`` and not-yet-effective for
# ``at < _NOW``; the strategy varies ``at`` so both branches are
# exercised. ``effective_end = None`` keeps the half-open window
# open-ended on the right — the navigator's expired branch is
# already exercised by ``tests/unit`` example tests, and reintroducing
# it here would not test idempotence as such.
_ROLE_EFFECTIVE_START: Final[datetime] = _NOW

# The five authority axes the navigator's per-stage authorization
# evaluation consults. Each axis maps to a Role Assignment scope (or
# the wildcard ``"*"``) that, when granted, makes the corresponding
# stage visible:
#
# - ``decision_scope``: the Decision's ``applicable_scope`` → enables
#   ``view.decision``.
# - ``recommendation``: the Recommendation's ``recommendation_id`` →
#   enables ``view.recommendation_revision``.
# - ``finding``: the Finding's ``finding_id`` → enables
#   ``view.finding_revision``.
# - ``document``: the Document's ``resource_id`` → enables both
#   ``view.region_occurrence`` and ``view.document_revision`` (both
#   leaves share the same scope per task 12.2).
# - ``wildcard``: a single ``"*"`` assignment that covers every
#   per-stage check at once; redundant with the four narrow axes but
#   useful for exercising the wildcard branch deterministically.
_AUTHORITY_AXES: Final[tuple[str, ...]] = (
    "decision_scope",
    "recommendation",
    "finding",
    "document",
    "wildcard",
)


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert a Party row required by every FK touching ``Parties``."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_NOW},
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# One scenario per case. The strategy is deliberately compact — the
# property under test is idempotence, not pipeline shape coverage,
# so a single-arm pipeline (one Document, one Region Occurrence, one
# Finding, one Recommendation, one Decision) is sufficient to exercise
# every stage of the navigator's traversal. Multi-arm shapes are
# covered by the example-based tests in
# ``tests/unit/test_provenance_navigate_decision.py``.
# ---------------------------------------------------------------------------


# Document content: 1..200 bytes. Bounded above so per-case persistence
# stays well under the 2000 ms Hypothesis deadline; bounded below at 1
# because :class:`EvidenceRepository.create_document` rejects empty
# content per Requirement 2.6.
_content_strategy = st.binary(min_size=1, max_size=200)


# ``at`` offset relative to :data:`_NOW`, in seconds. The window spans
# one day in either direction so the strategy exercises:
#
# - ``at < _NOW`` (Role-Assignment not-yet-effective → Decision-level
#   denial → :class:`DecisionUnresolvableError`).
# - ``at == _NOW`` (boundary of the half-open ``[start, end)`` window).
# - ``at > _NOW`` (Role-Assignment effective, Finding-Revision
#   ``recorded_at <= at`` rule satisfied, full chain visible subject
#   to per-stage authority).
_at_offset_seconds_strategy = st.integers(
    min_value=-86_400, max_value=86_400
)


@st.composite
def _scenario(draw) -> dict:
    """Draw one full scenario for a Hypothesis case.

    Returns a dict carrying the Document content, a valid Region
    span inside that content, the three non-empty text fields the
    pipeline writes to (statement, recommendation rationale,
    Decision rationale), the Decision outcome, the subset of
    authority axes the requesting Party will be granted, and the
    effective-time offset ``at`` relative to :data:`_NOW`.
    """
    content_bytes = draw(_content_strategy)
    content_length = len(content_bytes)

    # Span: 0 <= start < end <= content_length. Drawn so the bounded
    # text is at least one byte (Requirement 3.1 forbids zero-length
    # spans).
    start_offset = draw(
        st.integers(min_value=0, max_value=content_length - 1)
    )
    end_offset = draw(
        st.integers(min_value=start_offset + 1, max_value=content_length)
    )

    # Non-empty free-text fields. ``max_size`` is kept small so
    # Hypothesis shrinks aggressively toward the simplest
    # counterexample.
    statement = draw(st.text(min_size=1, max_size=50))
    rec_rationale = draw(st.text(min_size=1, max_size=50))
    decision_rationale = draw(st.text(min_size=1, max_size=50))

    outcome = draw(st.sampled_from(("Accept", "Reject", "Defer")))

    # Authority axes — subset of the five dimensions. Empty subset
    # exercises the Decision-level denial path; the full set
    # exercises the unrestricted-chain path; in-between subsets
    # exercise per-stage redaction.
    authority_axes = draw(
        st.lists(
            st.sampled_from(_AUTHORITY_AXES),
            min_size=0,
            max_size=len(_AUTHORITY_AXES),
            unique=True,
        )
    )

    at_offset_seconds = draw(_at_offset_seconds_strategy)

    return {
        "content_bytes": content_bytes,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "statement": statement,
        "rec_rationale": rec_rationale,
        "decision_rationale": decision_rationale,
        "outcome": outcome,
        "authority_axes": tuple(authority_axes),
        "at_offset_seconds": at_offset_seconds,
    }


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers and rows cannot leak between cases
# (design §"Testing Strategy" — "Each property and example test gets a
# fresh SQLite database").
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
# Role-assignment helper.
# ---------------------------------------------------------------------------


def _assign_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
) -> None:
    """Grant ``view`` authority to the requesting Party for ``scope``.

    Uses the same :meth:`AuthorizationService.assign_role` surface
    production uses so the property is exercised against the exact
    code path the runtime authorization gate consults.
    """
    request = AssignRoleRequest(
        party_id=_REQUESTER_PARTY_ID,
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
# Outcome representation.
#
# A navigate_decision invocation either returns a
# :class:`DecisionProvenanceChain` or raises
# :class:`DecisionUnresolvableError`. The five-repetition comparison
# treats the two outcomes as a tagged union: chains compare via the
# frozen-dataclass ``__eq__``; exceptions compare on type and on the
# carried ``decision_id``. Mixing the two outcomes across the five
# repetitions falsifies the property.
# ---------------------------------------------------------------------------


def _outcomes_equal(outcomes: list[object]) -> bool:
    """Return ``True`` iff every entry in *outcomes* compares equal to outcomes[0].

    Each entry is either a :class:`DecisionProvenanceChain` (the
    success path) or a :class:`DecisionUnresolvableError` (the
    Decision-level denial path).
    """
    first = outcomes[0]
    for other in outcomes[1:]:
        if isinstance(first, DecisionProvenanceChain):
            if not isinstance(other, DecisionProvenanceChain):
                return False
            if other != first:
                return False
        elif isinstance(first, DecisionUnresolvableError):
            if not isinstance(other, DecisionUnresolvableError):
                return False
            if other.decision_id != first.decision_id:
                return False
        else:  # pragma: no cover — defensive, should not happen
            return False
    return True


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 8: Provenance traversal idempotence
@given(scenario=_scenario())
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup builds a fresh SQLite database and seeds five
    # tables, which is more expensive than a pure in-memory property
    # test. The setup stays well under the 2000 ms deadline locally
    # but the data-generation health check is suppressed so the run
    # does not abort on the occasional slow case.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_provenance_traversal_idempotence(scenario: dict) -> None:
    """Five invocations of ``navigate_decision`` with the same
    ``(decision_id, party_id, at)`` yield equal results (identical
    node identities, attribute values, and ordering)."""
    content_bytes: bytes = scenario["content_bytes"]
    start_offset: int = scenario["start_offset"]
    end_offset: int = scenario["end_offset"]
    statement: str = scenario["statement"]
    rec_rationale: str = scenario["rec_rationale"]
    decision_rationale: str = scenario["decision_rationale"]
    outcome: str = scenario["outcome"]
    authority_axes: tuple[str, ...] = scenario["authority_axes"]
    at_offset_seconds: int = scenario["at_offset_seconds"]

    effective_at = _NOW + timedelta(seconds=at_offset_seconds)

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop8_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh per-case services so in-memory IdentityService state
        # cannot leak across Hypothesis cases. The FixedClock anchors
        # every persisted ``recorded_at`` to the same instant, which
        # keeps the assertion deterministic across shrinks.
        clock = FixedClock(_NOW)
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        authorization_service = AuthorizationService(
            clock=clock,
            audit_log=audit_log,
            identity_service=IdentityService(),
        )
        evidence_repository = EvidenceRepository(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        # Unwired Knowledge_Service so the property does not entangle
        # with the Recommendation-creation authority check
        # (Requirement 5.7) — that gate is exercised by Property 2.
        # The Decision-Maker authority check (Requirement 7.1) is
        # similarly not under test here; the deciding Party seeds the
        # pipeline without a Role Assignment and the unwired service
        # accepts the Decision write.
        knowledge_service = KnowledgeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        navigator = ProvenanceNavigator(
            clock=clock,
            authorization_service=authorization_service,
        )

        try:
            # 1. Seed required Parties: the deciding Party (authors
            #    every pipeline write and is the contributing Party
            #    on Document / Region rows), the requesting Party
            #    (subject of every authorization evaluation), and the
            #    Role-assigning authority Party (recorded on every
            #    Role Assignment plus the assignment audit row).
            with engine.begin() as conn:
                _seed_party(
                    conn, _DECIDING_PARTY_ID, "Property 8 Deciding Party"
                )
                _seed_party(
                    conn, _REQUESTER_PARTY_ID, "Property 8 Requesting Party"
                )
                _seed_party(
                    conn,
                    _ASSIGNING_AUTHORITY_ID,
                    "Property 8 Resource Steward",
                )

            # 2. Build the single-arm pipeline. One Document → one
            #    Region Occurrence → one Finding (Supports the
            #    Region) → one Recommendation (Derived From the
            #    Finding) → one Decision (Addresses the
            #    Recommendation Revision). Every write uses the
            #    same ``clock.now()`` so ``recorded_at`` is
            #    homogeneous across the chain.
            with engine.begin() as conn:
                document = evidence_repository.create_document(
                    conn,
                    content_bytes=content_bytes,
                    contributing_party_id=_DECIDING_PARTY_ID,
                    authority="authoritative",
                )
                region = evidence_repository.create_region_occurrence(
                    conn,
                    resource_id=document.resource_id,
                    revision_id=document.revision_id,
                    start_offset_bytes=start_offset,
                    end_offset_bytes=end_offset,
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
                    rationale=rec_rationale,
                )
                decision = knowledge_service.create_decision(
                    conn,
                    target_recommendation_id=(
                        recommendation.recommendation_id
                    ),
                    target_recommendation_revision_id=(
                        recommendation.recommendation_revision_id
                    ),
                    outcome=outcome,
                    rationale=decision_rationale,
                    deciding_party_id=_DECIDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_DECISION_SCOPE,
                )

            # 3. Assign view authority on the axes the scenario drew.
            #    Each axis maps to one ``view``-bearing Role
            #    Assignment for the requesting Party at the
            #    corresponding scope. Axes not in ``authority_axes``
            #    are simply not granted, so the navigator will
            #    redact the corresponding stage (or, for the Decision
            #    scope axis, raise :class:`DecisionUnresolvableError`).
            axis_scopes: dict[str, str] = {
                "decision_scope": _DECISION_SCOPE,
                "recommendation": recommendation.recommendation_id,
                "finding": finding.finding_id,
                "document": document.resource_id,
                "wildcard": "*",
            }
            for axis in authority_axes:
                _assign_view_role(
                    authorization_service,
                    engine,
                    scope=axis_scopes[axis],
                )

            # 4. Invoke ``navigate_decision`` exactly five times with
            #    the same ``(decision_id, party_id, at)`` tuple.
            #    Each invocation gets its own connection so the
            #    test exercises the navigator's per-connection
            #    contract (each call opens, reads, and closes — no
            #    state leaks between calls).
            outcomes: list[object] = []
            for _ in range(5):
                try:
                    with engine.connect() as conn:
                        chain = navigator.navigate_decision(
                            conn,
                            decision_id=decision.decision_id,
                            party_id=_REQUESTER_PARTY_ID,
                            at=effective_at,
                        )
                    outcomes.append(chain)
                except DecisionUnresolvableError as exc:
                    outcomes.append(exc)

            # 5. Property assertion — all five invocations compare
            #    equal. ``DecisionProvenanceChain`` is a frozen
            #    dataclass so its ``__eq__`` recurses into every
            #    nested node (``DecisionNode``,
            #    ``RecommendationRevisionNode`` / ``RedactedNode``,
            #    ``FindingRevisionNode`` / ``RedactedNode``,
            #    ``RegionOccurrenceNode`` / ``RedactedNode``,
            #    ``DocumentRevisionNode`` / ``RedactedNode``) and
            #    every attribute value, and tuples compare positionally
            #    so node ordering is also checked. A
            #    :class:`DecisionUnresolvableError` outcome compares
            #    on type and on the carried ``decision_id``.
            assert _outcomes_equal(outcomes), (
                "Provenance traversal idempotence violated: five "
                f"invocations of navigate_decision("
                f"decision_id={decision.decision_id!r}, "
                f"party_id={_REQUESTER_PARTY_ID!r}, "
                f"at={effective_at.isoformat()}) produced "
                f"non-equal outcomes. outcomes={outcomes!r}"
            )
        finally:
            engine.dispose()
