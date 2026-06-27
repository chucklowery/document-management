# Feature: second-walking-slice, Property 23: Planning Provenance Chain end-to-end
"""Property 23 — Planning Provenance Chain end-to-end (task 16.8).

**Property 23: Planning Provenance Chain end-to-end**

*For all* full Slice 1 + Slice 2 pipelines whose chain is fully
visible to the requesting Party:

- :meth:`ProvenanceNavigator.navigate_plan_approval` SHALL return the
  full ordered chain Plan Approval → Plan Revision → Activity Plan →
  Project (latest Project Revision at-or-before ``at``) → Objective
  (latest Objective Revision at-or-before ``at``) → Decision →
  Recommendation Revision → Finding Revision(s) → Region
  Occurrence(s) → Document Revision (Requirements 14.1, 14.2);
- every identity in the returned chain SHALL resolve to the
  corresponding row that was persisted at seed time (Requirement
  14.1);
- every returned :class:`RegionOccurrenceNode`'s
  ``span_content_digest_sha256`` SHALL equal the recomputed
  ``SHA-256`` of the returned ``bounded_text`` AND equal the digest
  that the :class:`EvidenceRepository` recorded on the
  ``Region_Occurrences`` row at occurrence-creation time (Requirement
  14.2 inherits Slice 1 Requirement 11.2 — Region Occurrence resolves
  to byte-equivalent bounded text and a matching digest);
- five independent invocations of :meth:`navigate_plan_approval` with
  the same ``(plan_approval_id, party_id, at)`` SHALL return
  byte-equivalent :class:`PlanApprovalProvenance` instances
  (Requirements 14.4, 14.5, 20.7).

**Validates: Requirements 14.1, 14.2, 14.4, 14.5, 20.7**

Strategy
========

Each Hypothesis case draws 1..2 *full-pipeline scenarios*. Each
scenario carries:

- Random ``content_bytes`` for one Source Document.
- 1..2 distinct supporting span ranges anchored against those bytes.
- A non-empty Finding statement.
- An Objective statement and an optional Objective rationale.
- A Project name, optional summary, and planned start / end dates
  (``start <= end``).
- An Activity Plan title and a Plan Revision planned-scope text.
- A Plan Approval rationale.

Per generated case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state
   cannot contaminate the byte-equivalence checks. The engine carries
   both the Slice 1 schema and the Slice 2 schema.
2. Seeds three Parties (an authoring Party, the requesting Party,
   and an assigning-authority Party) and grants the requesting Party
   a wildcard ``view`` Role Assignment so every chain node is
   unredacted — the "fully visible" precondition of the property is
   satisfied by this one assignment.
3. Seeds the Slice 1 leg through the existing :class:`EvidenceRepository`
   and :class:`KnowledgeService` (unwired — the Decision-Maker
   authority check is not under test): one Source Document, 1..2
   Region Occurrences (each carrying a ``span_content_digest_sha256``
   recorded by the Evidence repository at creation time), one
   non-hypothesis Finding with one ``Supports`` Relationship per
   Region Occurrence, one Recommendation, and one ``Accept`` Decision.
4. Seeds the Slice 2 leg through direct ``INSERT`` statements: one
   Objective (header) + one Objective_Revision targeting the
   ``Accept`` Decision, one Project (header) + one Project_Revision
   targeting the Objective, one Activity_Plan targeting the Project,
   one Plan_Revision with ``lifecycle_state='approved'`` targeting
   the Activity Plan, and one Plan_Approval_Record (``outcome =
   'Approve'``) targeting the Plan Revision. Direct INSERTs keep the
   property scoped to the read-side navigation; the planning
   Service-side persistence flow is covered by Property 16 (task
   16.1) and Property 20 (task 16.5).
5. For each Plan Approval Record, invokes
   :meth:`ProvenanceNavigator.navigate_plan_approval` *five times*
   with the same ``(plan_approval_id, party_id, at)`` and asserts:

   a. **Shape (Requirement 14.1).** The returned chain carries a
      visible :class:`PlanApprovalNode`, :class:`PlanRevisionNode`,
      :class:`ActivityPlanNode`, :class:`ProjectRevisionNode`, and
      :class:`ObjectiveRevisionNode` (none of them
      :class:`RedactedNode`) plus a non-``None`` Slice 1
      :class:`DecisionProvenanceChain` whose ``decision``,
      ``recommendation_revision``, ``findings``,
      ``region_occurrences``, and ``document_revisions`` are all
      visible nodes (no :class:`RedactedNode` in any position).

   b. **Identity resolution (Requirement 14.1).** Every identity on
      every node in the chain matches the identity persisted at seed
      time — for the Plan Approval, the Plan Revision, the Activity
      Plan, the latest Project Revision at-or-before ``at`` (the only
      Project Revision in this case), the latest Objective Revision
      at-or-before ``at``, the Decision, the Recommendation Revision,
      the Finding Revision, every Region Occurrence anchored by the
      scenario, and the Document Revision paired with each Region
      Occurrence.

   c. **Digest match (Requirement 14.2 / Slice 1 Requirement 11.2).**
      For every Region Occurrence node:
      ``span_content_digest_sha256 == sha256(bounded_text).hexdigest()``
      and ``span_content_digest_sha256`` equals the SHA-256 of the
      scenario's original ``content_bytes[start:end]`` (recomputed
      independently of the Evidence_Repository so the assertion does
      not trust the persisted value blindly).

   d. **Byte-equivalence across 5 repetitions (Requirements 14.4,
      14.5, 20.7).** ``PlanApprovalProvenance`` is a frozen
      dataclass; calling ``==`` on the result of the second-through-
      fifth invocation against the first is the canonical
      byte-equivalence check used here. The check exercises every
      stage of the planning prefix (Plan Approval Record,
      Plan_Revisions, Activity_Plans, latest Project_Revisions at
      ``at``, latest Objective_Revisions at ``at``) and the delegated
      Slice 1 tail (Decisions, Recommendation_Revisions,
      Finding_Revisions, Region_Occurrences, Document_Revisions) for
      idempotent retrieval.

Hypothesis settings
===================

``max_examples=50`` and ``deadline=5000`` keep the run inside the
slice's CI budget: each case allocates a fresh on-disk SQLite
database and seeds a complete Slice 1 + Slice 2 pipeline (1..2
Document Revisions, 1..4 Region Occurrences, 1..2 Findings,
Recommendations, and Decisions, plus the Slice 2 Objective →
Plan_Approval chain), so per-case setup is markedly heavier than a
pure in-memory property test. ``suppress_health_check`` covers the
``too_slow`` and ``data_too_large`` health checks the per-case work
would otherwise trip.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid as uuid_lib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
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
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import (
    ActivityPlanNode,
    DecisionNode,
    DocumentRevisionNode,
    FindingRevisionNode,
    ObjectiveRevisionNode,
    PlanApprovalNode,
    PlanApprovalProvenance,
    PlanRevisionNode,
    ProjectRevisionNode,
    ProvenanceNavigator,
    RecommendationRevisionNode,
    RedactedNode,
    RegionOccurrenceNode,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# A single :class:`FixedClock` instant anchors every persisted
# ``recorded_at`` (Documents, Regions, Findings, Recommendations,
# Decisions, Objectives, Objective Revisions, Projects, Project
# Revisions, Activity Plans, Plan Revisions, Plan Approval Records,
# Audit Records, Role Assignments). The navigation effective time
# ``_AT`` falls strictly after the role assignment's
# ``effective_start`` so the wildcard view authority is always
# effective at navigation time.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_AT: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"


# Authoring / requesting / assigning Party identities. The authoring
# Party contributes every Slice 1 Document, Finding, Recommendation,
# and Decision and is the ``authoring_party_id`` on every Slice 2
# row. The requesting Party is the navigator's caller and the holder
# of the wildcard view authority. The assigning-authority Party
# records the Role Assignment that grants that authority.
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000e0001"
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000e0002"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000e0003"

_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000e00a1"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)


# Applicable scope persisted on every Slice 1 Decision and every
# Slice 2 row. The wildcard Role Assignment covers every target
# scope, so the precise value here is irrelevant to Property 23 —
# but the columns are NOT NULL on the Slice 2 schema so a non-empty
# value must be supplied.
_SCOPE: Final[str] = "property-23-scope"


# Number of repeated :meth:`navigate_plan_approval` invocations the
# byte-equivalence assertion runs per generated Plan Approval. The
# task explicitly names "5 repetitions per generated case".
_REPETITIONS: Final[int] = 5


# Authority basis identifier persisted on Plan_Approval_Records. The
# column has no FK constraint so any opaque string is acceptable;
# centralizing the value keeps the seed deterministic across
# Hypothesis shrinks.
_PLAN_APPROVAL_AUTHORITY_BASIS_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000e00b1"
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case state cannot leak between generated inputs (design
# §"Testing Strategy" — per-case database isolation). The engine carries
# both the Slice 1 schema and the Slice 2 schema (the latter installs
# the AD-WS-19 lifecycle UPDATE trigger on every DBAPI connection).
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    create_planning_schema(engine)
    return engine


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string (matches AD-WS-2)."""
    return str(uuid_utils.uuid7())


def _seed_party(connection, party_id: str, display: str) -> None:
    """Insert a Party row required by the FK constraints.

    Every persisted row in the seed chain that references a Party
    Identity (Document Revisions, Region Occurrences, Finding
    Revisions, Recommendation Revisions, Decisions, Objective
    Revisions, Project Revisions, Activity Plans, Plan Revisions,
    Plan Approval Records, Role Assignments, Audit Records, and
    Relationships) FKs back to this table.
    """
    connection.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )



# ---------------------------------------------------------------------------
# Slice 2 direct-INSERT seeders.
#
# Property 23 exercises the read-side navigator, not the planning
# Service-side persistence flow (Property 16 covers that). Direct
# INSERTs keep the per-case setup compact, deterministic, and free of
# the role-grant plumbing that the Slice 2 creation services require.
# Each helper writes exactly the columns the navigator reads.
# ---------------------------------------------------------------------------


def _seed_objective(
    engine: Engine,
    *,
    objective_id: str,
    objective_revision_id: str,
    statement: str,
    rationale: str | None,
    target_decision_id: str,
) -> None:
    """Insert one Objectives row + one Objective_Revisions row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": objective_id, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id,
                    parent_revision_id, statement, rationale,
                    target_decision_id, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :oid, NULL, :statement, :rationale,
                    :did, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": objective_revision_id,
                "oid": objective_id,
                "statement": statement,
                "rationale": rationale,
                "did": target_decision_id,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_project(
    engine: Engine,
    *,
    project_id: str,
    project_revision_id: str,
    name: str,
    summary: str | None,
    target_objective_id: str,
    planned_start_date: date,
    planned_end_date: date,
) -> None:
    """Insert one Projects row + one Project_Revisions row.

    The Project Revision's ``recorded_at`` is fixed at ``_NOW_ISO``,
    which is strictly less than the navigation effective time ``_AT``
    so the navigator's latest-at-time selection returns this row.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": project_id, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Project_Revisions (
                    project_revision_id, project_id,
                    parent_revision_id, name, summary,
                    target_objective_id, planned_start_date,
                    planned_end_date, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pid, NULL, :name, :summary, :oid,
                    :start_d, :end_d, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": project_revision_id,
                "pid": project_id,
                "name": name,
                "summary": summary,
                "oid": target_objective_id,
                "start_d": planned_start_date.isoformat(),
                "end_d": planned_end_date.isoformat(),
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_activity_plan(
    engine: Engine,
    *,
    activity_plan_id: str,
    target_project_id: str,
    title: str,
) -> None:
    """Insert one Activity_Plans row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, :title, :party, :scope, :ts
                )
                """
            ),
            {
                "aid": activity_plan_id,
                "pid": target_project_id,
                "title": title,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_approved_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str,
    planned_scope: str,
) -> None:
    """Insert one Plan_Revisions row with ``lifecycle_state='approved'``.

    ``INSERT`` is permitted regardless of lifecycle state — the
    AD-WS-19 trigger fires only on ``UPDATE``. Seeding the approved
    revision directly mirrors the pattern used by Property 20
    (:mod:`tests.property.test_property_20_approved_plan_revision_immutability`)
    so the chain rooted at the Plan Approval reads
    ``lifecycle_state='approved'`` without needing the session pragma.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :aid, NULL, 'approved',
                    :scope_text, '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "scope_text": planned_scope,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_plan_approval(
    engine: Engine,
    *,
    plan_approval_id: str,
    target_activity_plan_id: str,
    target_plan_revision_id: str,
    rationale: str,
) -> None:
    """Insert one Plan_Approval_Records row with ``outcome='Approve'``."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :pa, :aid, :rev, 'Approve', :rationale,
                    :party, 'role-grant-id', :basis,
                    :scope, :ts
                )
                """
            ),
            {
                "pa": plan_approval_id,
                "aid": target_activity_plan_id,
                "rev": target_plan_revision_id,
                "rationale": rationale,
                "party": _AUTHORING_PARTY_ID,
                "basis": _PLAN_APPROVAL_AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )



# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# A *pipeline scenario* describes one full Slice 1 + Slice 2 chain:
# the Slice 1 leg seeds a Source Document, 1..2 supporting spans,
# one Finding, one Recommendation, and one ``Accept`` Decision; the
# Slice 2 leg seeds one Objective + Objective_Revision, one Project +
# Project_Revision, one Activity Plan, one approved Plan Revision, and
# one Plan Approval Record. The strategy draws every text payload and
# every span endpoint so the digest, byte-equivalence, and identity-
# resolution assertions cover the full input surface.
# ---------------------------------------------------------------------------


# Restricted alphabet keeps the shrunken counterexamples readable and
# avoids drawing control characters some SQLite drivers reject. The
# Property 23 assertions are about byte-equivalence and digest
# correctness, not UTF-8 robustness (Property 7 covers that), so the
# narrow alphabet is appropriate.
_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Text run of ``min_size..max_size`` chars from the narrow alphabet."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


@st.composite
def _span_strategy(draw, *, content_length: int) -> tuple[int, int]:
    """Draw a valid ``(start, end)`` span inside a ``content_length`` buffer.

    Constraints (Requirement 3.5 / AD-WS-6):

    - ``0 <= start_offset_bytes``
    - ``start_offset_bytes < end_offset_bytes``
    - ``end_offset_bytes <= content_length``
    """
    start = draw(st.integers(min_value=0, max_value=content_length - 1))
    end = draw(st.integers(min_value=start + 1, max_value=content_length))
    return (start, end)


@st.composite
def _pipeline_strategy(draw) -> dict:
    """Draw one full Slice 1 + Slice 2 pipeline scenario.

    Returns a dict with keys describing both legs of the chain.

    Slice 1 leg:

    - ``content_bytes``: Source Document content; 1..128 bytes drawn
      from the full SQLite BLOB alphabet so the digest assertion is
      exercised against non-printable byte values too.
    - ``spans``: 1..2 distinct supporting span ranges, each valid for
      ``content_bytes`` by construction. ``unique=True`` keeps Region
      identities distinct.
    - ``finding_statement``: Non-empty Finding statement.

    Slice 2 leg:

    - ``objective_statement``: 1..200 chars (Requirement 2.3 ceiling
      is 4000 but a smaller draw keeps the case fast).
    - ``objective_rationale``: ``None`` or 0..200 chars.
    - ``project_name``: 1..200 chars (Requirement 4.2).
    - ``project_summary``: ``None`` or 0..200 chars.
    - ``planned_start_date`` / ``planned_end_date``: ``start <= end``
      drawn from a slice-window range (Requirement 4.2 / 4.3).
    - ``activity_plan_title``: 1..200 chars (Requirement 6.2).
    - ``planned_scope``: 1..200 chars (Requirement 7.2 ceiling is
      10000 but a smaller draw keeps the case fast).
    - ``approval_rationale``: 1..200 chars (Requirement 9.2 ceiling
      is 4000).
    """
    content_length = draw(st.integers(min_value=1, max_value=128))
    content_bytes = draw(
        st.binary(min_size=content_length, max_size=content_length)
    )
    spans = draw(
        st.lists(
            _span_strategy(content_length=content_length),
            min_size=1,
            max_size=2,
            unique=True,
        )
    )
    start_d = draw(
        st.dates(min_value=date(2025, 1, 1), max_value=date(2028, 12, 31))
    )
    span_days = draw(st.integers(min_value=0, max_value=365))
    end_d = start_d + timedelta(days=span_days)
    return {
        "content_bytes": content_bytes,
        "spans": spans,
        "finding_statement": draw(_bounded_text(1, 120)),
        "objective_statement": draw(_bounded_text(1, 200)),
        "objective_rationale": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        "project_name": draw(_bounded_text(1, 200)),
        "project_summary": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        "planned_start_date": start_d,
        "planned_end_date": end_d,
        "activity_plan_title": draw(_bounded_text(1, 200)),
        "planned_scope": draw(_bounded_text(1, 200)),
        "approval_rationale": draw(_bounded_text(1, 200)),
    }


# 1..2 pipelines per case. Two pipelines exercise the multi-chain
# case (the navigator must isolate each Plan Approval's traversal
# from every other) while one pipeline shrinks to a minimal
# counterexample if one breaks. Larger ``max_size`` values are
# unnecessary because Property 23 quantifies over each Plan Approval
# independently, not over chain combinations.
_pipeline_scenarios = st.lists(
    _pipeline_strategy(),
    min_size=1,
    max_size=2,
)


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 23: Planning Provenance Chain end-to-end
@given(scenarios=_pipeline_scenarios)
@settings(
    max_examples=50,
    deadline=5000,
    # Per-case setup builds two complete Slice 1 + Slice 2 pipelines
    # in the worst case (one Source Document with up to 2 Region
    # Occurrences plus the Objective → Plan Approval planning chain
    # per pipeline) plus the five-repetition navigation loop. The
    # cumulative per-case work is markedly heavier than an in-memory
    # property test, so the ``too_slow`` and ``data_too_large``
    # health checks are suppressed.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_planning_provenance_chain_end_to_end(
    scenarios: list[dict],
) -> None:
    """Every navigated Plan Approval surfaces the full ordered chain
    Plan Approval → Plan Revision → Activity Plan → Project →
    Objective → Decision → Recommendation → Finding → Region
    Occurrence → Document Revision; every identity resolves; the
    Region Occurrence span digest matches the recorded value; and
    five independent invocations return byte-equivalent results.

    Validates Requirements 14.1, 14.2, 14.4, 14.5, 20.7.
    """
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop23_"
    ) as raw_tmp:
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
        # authority check is not under test. Property 23 only asserts
        # on the navigator's view-authority gate, which is driven by
        # the wildcard Role Assignment seeded below.
        knowledge_service = KnowledgeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        navigator = ProvenanceNavigator(
            clock=clock,
            authorization_service=authorization_service,
        )

        # Per-pipeline expectations the post-seed assertions read.
        #
        # Each entry: {
        #   "plan_approval_id":        str,
        #   "plan_revision_id":        str,
        #   "activity_plan_id":        str,
        #   "project_id":              str,
        #   "project_revision_id":     str,
        #   "objective_id":            str,
        #   "objective_revision_id":   str,
        #   "decision_id":             str,
        #   "recommendation_id":       str,
        #   "recommendation_revision_id": str,
        #   "finding_id":              str,
        #   "finding_revision_id":     str,
        #   "document_resource_id":    str,
        #   "document_revision_id":    str,
        #   "content_bytes":           bytes,
        #   "objective_statement":     str,
        #   "objective_rationale":     Optional[str],
        #   "project_name":            str,
        #   "project_summary":         Optional[str],
        #   "planned_start_date":      str,  # ISO-8601 date
        #   "planned_end_date":        str,  # ISO-8601 date
        #   "activity_plan_title":     str,
        #   "planned_scope":           str,
        #   "approval_rationale":      str,
        #   "expected_regions": list[{
        #       "region_id":          str,
        #       "start":              int,
        #       "end":                int,
        #       "expected_bytes":     bytes,
        #       "expected_digest":    str,  # lowercase-hex SHA-256
        #   }],
        # }
        persisted: list[dict] = []

        try:
            # ----- 1. Seed Parties + wildcard view authority --------
            with engine.begin() as conn:
                _seed_party(conn, _AUTHORING_PARTY_ID, "Property 23 Author")
                _seed_party(conn, _REQUESTER_PARTY_ID, "Property 23 Reader")
                _seed_party(
                    conn,
                    _ASSIGNING_AUTHORITY_ID,
                    "Property 23 Assigning Authority",
                )

            # One wildcard ``view`` Role Assignment grants the
            # requesting Party authority over every target scope —
            # Slice 1 evidence kinds and Slice 2 planning kinds alike.
            # This satisfies the "fully visible" precondition of
            # Property 23; redaction is exercised by Property 18.
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

            # ----- 2. Seed every pipeline ---------------------------
            for scenario in scenarios:
                content_bytes: bytes = scenario["content_bytes"]
                spans: list[tuple[int, int]] = scenario["spans"]

                # --- 2a. Slice 1 leg: Document → Region(s) →
                # Finding → Recommendation → Decision -----------------
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
                            statement=scenario["finding_statement"],
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
                            rationale="Property 23 recommendation.",
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
                            rationale="Property 23 decision.",
                            deciding_party_id=_AUTHORING_PARTY_ID,
                            authority_basis=_AUTHORITY_BASIS,
                            applicable_scope=_SCOPE,
                        )
                    )

                # --- 2b. Slice 2 leg: Objective → Project → Activity
                #         Plan → Plan Revision → Plan Approval ------
                # Direct INSERTs keep the seed scoped to the read-side
                # navigator and avoid the role-grant plumbing the
                # Planning_Service creation methods require.
                objective_id = _new_uuid7()
                objective_revision_id = _new_uuid7()
                project_id = _new_uuid7()
                project_revision_id = _new_uuid7()
                activity_plan_id = _new_uuid7()
                plan_revision_id = _new_uuid7()
                plan_approval_id = _new_uuid7()

                _seed_objective(
                    engine,
                    objective_id=objective_id,
                    objective_revision_id=objective_revision_id,
                    statement=scenario["objective_statement"],
                    rationale=scenario["objective_rationale"],
                    target_decision_id=decision.decision_id,
                )
                _seed_project(
                    engine,
                    project_id=project_id,
                    project_revision_id=project_revision_id,
                    name=scenario["project_name"],
                    summary=scenario["project_summary"],
                    target_objective_id=objective_id,
                    planned_start_date=scenario["planned_start_date"],
                    planned_end_date=scenario["planned_end_date"],
                )
                _seed_activity_plan(
                    engine,
                    activity_plan_id=activity_plan_id,
                    target_project_id=project_id,
                    title=scenario["activity_plan_title"],
                )
                _seed_approved_plan_revision(
                    engine,
                    plan_revision_id=plan_revision_id,
                    activity_plan_id=activity_plan_id,
                    planned_scope=scenario["planned_scope"],
                )
                _seed_plan_approval(
                    engine,
                    plan_approval_id=plan_approval_id,
                    target_activity_plan_id=activity_plan_id,
                    target_plan_revision_id=plan_revision_id,
                    rationale=scenario["approval_rationale"],
                )

                # Record per-Region expectations independently of the
                # Evidence_Repository: compute the digest from the
                # scenario bytes so the assertion does not trust the
                # persisted value as a source of truth.
                expected_regions = [
                    {
                        "region_id": region.region_id,
                        "start": region.start_offset_bytes,
                        "end": region.end_offset_bytes,
                        "expected_bytes": content_bytes[
                            region.start_offset_bytes : region.end_offset_bytes
                        ],
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
                        "plan_approval_id": plan_approval_id,
                        "plan_revision_id": plan_revision_id,
                        "activity_plan_id": activity_plan_id,
                        "project_id": project_id,
                        "project_revision_id": project_revision_id,
                        "objective_id": objective_id,
                        "objective_revision_id": objective_revision_id,
                        "decision_id": decision.decision_id,
                        "recommendation_id": recommendation.recommendation_id,
                        "recommendation_revision_id": (
                            recommendation.recommendation_revision_id
                        ),
                        "finding_id": finding.finding_id,
                        "finding_revision_id": finding.finding_revision_id,
                        "document_resource_id": doc.resource_id,
                        "document_revision_id": doc.revision_id,
                        "content_bytes": content_bytes,
                        "objective_statement": scenario["objective_statement"],
                        "objective_rationale": scenario["objective_rationale"],
                        "project_name": scenario["project_name"],
                        "project_summary": scenario["project_summary"],
                        "planned_start_date": (
                            scenario["planned_start_date"].isoformat()
                        ),
                        "planned_end_date": (
                            scenario["planned_end_date"].isoformat()
                        ),
                        "activity_plan_title": scenario["activity_plan_title"],
                        "planned_scope": scenario["planned_scope"],
                        "approval_rationale": scenario["approval_rationale"],
                        "expected_regions": expected_regions,
                    }
                )

            # ----- 3. Navigate every Plan Approval, 5 times each ----
            for entry in persisted:
                chains: list[PlanApprovalProvenance] = []
                for _ in range(_REPETITIONS):
                    with engine.connect() as conn:
                        chain = navigator.navigate_plan_approval(
                            conn,
                            plan_approval_id=entry["plan_approval_id"],
                            party_id=_REQUESTER_PARTY_ID,
                            at=_AT,
                        )
                    chains.append(chain)

                first = chains[0]

                # ---- 3a. Shape and identity (Requirement 14.1) -----
                _assert_planning_chain_shape_and_identities(first, entry)

                # ---- 3b. Region digest match (Requirement 14.2) ----
                assert first.decision_chain is not None
                _assert_region_occurrence_digests(
                    first.decision_chain, entry
                )

                # ---- 3c. Byte-equivalence across 5 reps
                #         (Requirements 14.4, 14.5, 20.7) ------------
                for index, repeat in enumerate(chains[1:], start=2):
                    assert repeat == first, (
                        f"Property 23 violated on plan_approval_id="
                        f"{entry['plan_approval_id']!r}: repetition "
                        f"#{index} returned a different "
                        "PlanApprovalProvenance than the first "
                        "invocation. Requirements 14.4, 14.5, 20.7 "
                        "require byte-equivalent results across "
                        "repeated invocations with the same "
                        "(plan_approval_id, party_id, at).\n"
                        f"first={first!r}\n"
                        f"repeat#{index}={repeat!r}"
                    )
        finally:
            engine.dispose()



# ---------------------------------------------------------------------------
# Assertion helpers.
#
# Two helpers split the per-Plan-Approval invariants into independently
# diagnosable buckets:
#
#   - :func:`_assert_planning_chain_shape_and_identities` — Requirement
#     14.1: every node in the chain is a *visible* node (no
#     :class:`RedactedNode`) and every identity matches the persisted
#     row.
#   - :func:`_assert_region_occurrence_digests` — Requirement 14.2 /
#     Slice 1 Requirement 11.2: every Region Occurrence node carries a
#     ``span_content_digest_sha256`` that equals both the recomputed
#     SHA-256 of the returned ``bounded_text`` and the SHA-256 of the
#     scenario's original ``content_bytes[start:end]``.
#
# Both helpers raise :class:`AssertionError` with messages that name
# the violated invariant and the Requirement clause, so a Hypothesis
# shrunk counterexample points directly at the failing dimension.
# ---------------------------------------------------------------------------


def _assert_planning_chain_shape_and_identities(
    chain: PlanApprovalProvenance, entry: dict[str, Any]
) -> None:
    """Assert every node in ``chain`` is visible and resolves to the
    persisted identity recorded in ``entry``.

    Covers the full ordered chain Plan Approval → Plan Revision →
    Activity Plan → Project → Objective → Decision → Recommendation
    Revision → Finding Revision → Region Occurrence(s) → Document
    Revision (Requirement 14.1). Every node must be a concrete
    visible type (not :class:`RedactedNode`) because Property 23
    quantifies over chains that are fully visible to the requesting
    Party.
    """
    # ---- Plan Approval (head) ------------------------------------------
    assert isinstance(chain.plan_approval, PlanApprovalNode), (
        "Plan Approval head must be a visible PlanApprovalNode under "
        f"wildcard view authority; got {type(chain.plan_approval).__name__}."
    )
    assert chain.plan_approval.plan_approval_id == entry["plan_approval_id"]
    assert (
        chain.plan_approval.target_activity_plan_id
        == entry["activity_plan_id"]
    )
    assert (
        chain.plan_approval.target_plan_revision_id
        == entry["plan_revision_id"]
    )
    assert chain.plan_approval.outcome == "Approve"
    assert chain.plan_approval.rationale == entry["approval_rationale"]
    assert chain.requested_plan_approval_id == entry["plan_approval_id"]

    # ---- Plan Revision -------------------------------------------------
    assert isinstance(chain.plan_revision, PlanRevisionNode), (
        "Plan Revision must be a visible PlanRevisionNode under "
        f"wildcard view authority; got {type(chain.plan_revision).__name__}."
    )
    assert chain.plan_revision.plan_revision_id == entry["plan_revision_id"]
    assert chain.plan_revision.activity_plan_id == entry["activity_plan_id"]
    assert chain.plan_revision.lifecycle_state == "approved"
    assert chain.plan_revision.planned_scope == entry["planned_scope"]

    # ---- Activity Plan -------------------------------------------------
    assert isinstance(chain.activity_plan, ActivityPlanNode), (
        "Activity Plan must be a visible ActivityPlanNode under "
        f"wildcard view authority; got {type(chain.activity_plan).__name__}."
    )
    assert chain.activity_plan.activity_plan_id == entry["activity_plan_id"]
    assert chain.activity_plan.target_project_id == entry["project_id"]
    assert chain.activity_plan.title == entry["activity_plan_title"]

    # ---- Project Revision (latest at-or-before ``at``) -----------------
    assert isinstance(chain.project_revision, ProjectRevisionNode), (
        "Project Revision must be a visible ProjectRevisionNode under "
        f"wildcard view authority; got "
        f"{type(chain.project_revision).__name__}."
    )
    assert chain.project_revision.project_id == entry["project_id"]
    assert (
        chain.project_revision.project_revision_id
        == entry["project_revision_id"]
    )
    assert chain.project_revision.name == entry["project_name"]
    assert chain.project_revision.summary == entry["project_summary"]
    assert (
        chain.project_revision.target_objective_id == entry["objective_id"]
    )
    assert (
        chain.project_revision.planned_start_date
        == entry["planned_start_date"]
    )
    assert (
        chain.project_revision.planned_end_date == entry["planned_end_date"]
    )

    # ---- Objective Revision (latest at-or-before ``at``) ---------------
    assert isinstance(chain.objective_revision, ObjectiveRevisionNode), (
        "Objective Revision must be a visible ObjectiveRevisionNode "
        "under wildcard view authority; got "
        f"{type(chain.objective_revision).__name__}."
    )
    assert chain.objective_revision.objective_id == entry["objective_id"]
    assert (
        chain.objective_revision.objective_revision_id
        == entry["objective_revision_id"]
    )
    assert (
        chain.objective_revision.statement == entry["objective_statement"]
    )
    assert (
        chain.objective_revision.rationale == entry["objective_rationale"]
    )
    assert (
        chain.objective_revision.target_decision_id
        == entry["decision_id"]
    )

    # ---- Slice 1 Decision tail -----------------------------------------
    assert chain.decision_chain is not None, (
        "Decision tail must be present under wildcard view authority; "
        "got decision_chain=None. Requirement 14.1 requires the full "
        "ordered chain Plan Approval → ... → Document Revision to "
        "return when every node is authorized."
    )
    decision_chain = chain.decision_chain

    # Decision head.
    assert isinstance(decision_chain.decision, DecisionNode), (
        "Decision head must be a visible DecisionNode under wildcard "
        f"view authority; got {type(decision_chain.decision).__name__}."
    )
    assert decision_chain.decision.decision_id == entry["decision_id"]

    # Recommendation Revision.
    assert isinstance(
        decision_chain.recommendation_revision,
        RecommendationRevisionNode,
    ), (
        "Recommendation Revision must be a visible "
        "RecommendationRevisionNode under wildcard view authority; "
        f"got {type(decision_chain.recommendation_revision).__name__}."
    )
    assert (
        decision_chain.recommendation_revision.recommendation_id
        == entry["recommendation_id"]
    )
    assert (
        decision_chain.recommendation_revision.recommendation_revision_id
        == entry["recommendation_revision_id"]
    )

    # Exactly one Finding per scenario (the seed creates one Finding
    # citing every Region Occurrence as a ``Supports`` reference).
    assert len(decision_chain.findings) == 1, (
        "Property 23 scenario seeds one Finding per pipeline; the "
        f"chain should carry exactly one Finding entry, got "
        f"{len(decision_chain.findings)}."
    )
    finding_node = decision_chain.findings[0]
    assert isinstance(finding_node, FindingRevisionNode), (
        "Finding Revision must be a visible FindingRevisionNode under "
        f"wildcard view authority; got {type(finding_node).__name__}."
    )
    assert finding_node.finding_id == entry["finding_id"]
    assert finding_node.finding_revision_id == entry["finding_revision_id"]

    # One Region Occurrence node per ``Supports`` Relationship; one
    # Document Revision entry per Region Occurrence node.
    expected_regions = entry["expected_regions"]
    assert len(decision_chain.region_occurrences) == len(expected_regions), (
        "Property 23 expects one Region Occurrence entry per scenario "
        f"span (got {len(expected_regions)} spans, "
        f"{len(decision_chain.region_occurrences)} region nodes)."
    )
    assert len(decision_chain.document_revisions) == len(
        decision_chain.region_occurrences
    ), (
        "DecisionProvenanceChain invariant: "
        "len(document_revisions) == len(region_occurrences)."
    )

    for region_node, doc_node in zip(
        decision_chain.region_occurrences,
        decision_chain.document_revisions,
    ):
        assert isinstance(region_node, RegionOccurrenceNode), (
            "Region Occurrence must be a visible RegionOccurrenceNode "
            "under wildcard view authority; got "
            f"{type(region_node).__name__}."
        )
        assert isinstance(doc_node, DocumentRevisionNode), (
            "Document Revision must be a visible DocumentRevisionNode "
            "under wildcard view authority; got "
            f"{type(doc_node).__name__}."
        )
        assert doc_node.revision_id == entry["document_revision_id"]
        assert doc_node.resource_id == entry["document_resource_id"]


def _assert_region_occurrence_digests(
    decision_chain, entry: dict[str, Any]
) -> None:
    """Assert every Region Occurrence node carries a matching digest.

    Three independent equalities are checked per Region Occurrence:

    1. ``span_content_digest_sha256`` equals the SHA-256 of the
       returned ``bounded_text`` (Requirement 14.2 — digest matches
       at navigation time, inheriting Slice 1 Requirement 11.2).
    2. ``span_content_digest_sha256`` equals the SHA-256 of the
       scenario's original ``content_bytes[start:end]`` (Property 23
       digest-equivalence guarantee — the navigated digest matches
       the digest the Evidence_Repository recorded at occurrence
       creation, recomputed independently).
    3. ``bounded_text`` equals ``content_bytes[start:end]`` of the
       resolved Document Revision (Requirement 14.2 inherits Slice 1
       Requirement 3.4 — byte-equivalent span resolution).
    """
    region_node_by_id: dict[str, RegionOccurrenceNode] = {
        node.region_id: node for node in decision_chain.region_occurrences
    }

    for expected in entry["expected_regions"]:
        region_id = expected["region_id"]
        assert region_id in region_node_by_id, (
            f"Region Occurrence region_id={region_id!r} anchored by "
            "the scenario is missing from the navigated chain; "
            "Requirement 14.1 / 14.2 requires every Supports-cited "
            "Occurrence to surface in the planning provenance chain."
        )
        region_node = region_node_by_id[region_id]

        # 1. Span fields consistent with the scenario.
        assert region_node.start_offset_bytes == expected["start"]
        assert region_node.end_offset_bytes == expected["end"]
        assert region_node.span_byte_length == (
            expected["end"] - expected["start"]
        )

        # 2. Bounded text equals content_bytes[start:end].
        assert region_node.bounded_text == expected["expected_bytes"], (
            f"Region Occurrence region_id={region_id!r} returned "
            "bounded_text that diverges from content_bytes[start:end]; "
            "Requirement 14.2 inherits Slice 1 Requirement 11.2 / "
            "3.4 byte-equivalence."
        )

        # 3. Digest equals SHA-256(bounded_text).
        computed = hashlib.sha256(region_node.bounded_text).hexdigest()
        assert computed == region_node.span_content_digest_sha256, (
            f"Region Occurrence region_id={region_id!r} "
            "span_content_digest_sha256 does not equal "
            "SHA-256(bounded_text); Requirement 14.2 (inheriting "
            "Slice 1 Requirement 11.2) requires digest-equivalence at "
            "navigation time. "
            f"computed={computed!r}, "
            f"node={region_node.span_content_digest_sha256!r}."
        )

        # 4. Digest equals SHA-256(content_bytes[start:end]) recomputed
        #    independently of the persisted value.
        assert (
            region_node.span_content_digest_sha256
            == expected["expected_digest"]
        ), (
            f"Region Occurrence region_id={region_id!r} "
            "span_content_digest_sha256 diverges from SHA-256 of the "
            "scenario span bytes; Property 23 requires the navigated "
            "digest to match the digest the Evidence_Repository "
            "recorded at occurrence creation. "
            f"navigated={region_node.span_content_digest_sha256!r}, "
            f"expected={expected['expected_digest']!r}."
        )
