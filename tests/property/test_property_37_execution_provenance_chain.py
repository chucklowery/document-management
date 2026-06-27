# Feature: third-walking-slice, Property 37: Execution Provenance Chain end-to-end
"""Property 37 — Execution Provenance Chain end-to-end (task 16.7).

**Property 37: Execution Provenance Chain end-to-end**

*For all* full Slice 1 + Slice 2 + Slice 3 pipelines whose chain is
fully visible to the requesting Party, the Walking_Slice_System
satisfies:

(a) **Three ordered chains return (Requirement 31.2 / 35.1).** All
    three navigation entry points
    (:meth:`ProvenanceNavigator.navigate_completion`,
    :meth:`navigate_deliverable_production`, and
    :meth:`navigate_produced_deliverable_revision`) return an
    :class:`ExecutionProvenanceTree` carrying:

    - the Planning leg attached at
      :attr:`ExecutionProvenanceTree.plan_approval_chain` (non-None)
      that surfaces the full Slice 2 Plan Approval → Plan Revision →
      Activity Plan → Project → Objective Revision → Slice 1 Decision
      tail, AND
    - one or more entries on
      :attr:`ExecutionProvenanceTree.work_assignment_chains`
      (recording the Work Assignment + Work Events + Time Entries),
      AND, for the Completion anchor only,
    - one or more entries on
      :attr:`ExecutionProvenanceTree.milestone_acceptance_chains`
      (recording the Milestone Acceptance + Production + produced
      Deliverable Revision).

(b) **Every identity resolves (Requirement 31.2 / 35.1).** Every
    node carries the exact Identity (and Revision Identity, where
    applicable) recorded at seed time; intermediate nodes are
    concrete visible types (no :class:`RedactedNode`) under the
    wildcard view authority.

(c) **Content Region Occurrence span digest matches the recorded
    digest (Requirement 35.2 / 11.2).** Every Region Occurrence node
    surfaced through the delegated planning leg's Slice 1 Decision
    tail carries a ``span_content_digest_sha256`` that equals both
    the recomputed SHA-256 of the returned ``bounded_text`` and the
    SHA-256 of the scenario's original
    ``content_bytes[start:end]``.

(d) **Chain byte-equivalent across 5 repetitions (Requirement 31.4
    / 35.5).** Five independent invocations of each navigation entry
    point with the same arguments return byte-equivalent
    :class:`ExecutionProvenanceTree` instances; structural equality
    (``==``) on the frozen dataclass is the canonical check.

(e) **Restricted nodes appear as `{kind, redacted: True}` markers
    (Requirement 35.3 / AD-WS-9 rule 1).** An additional Work Event
    Record seeded with a distinct ``applicable_scope`` (outside the
    requester's view authority scope) surfaces as a
    :class:`RedactedNode` carrying only ``kind`` and ``redacted=True``
    inside the Work Assignment chain — while every sibling Work Event
    and Time Entry remains visible.

(f) **Unresolved / stale / unavailable nodes return gap descriptors
    (Requirement 31.3 / 35.4 / 41.7).** A Provenance Manifest seeded
    against the Plan Approval Record with a single unresolved
    Omission Entry surfaces a :class:`ChainGapDescriptor` (carrying
    ``stage`` ``category`` ``next_reachable_node_identity`` only) on
    the delegated planning leg's :attr:`gap_descriptors` tuple of
    every traversal.

**Validates: Requirements 31.2, 31.3, 31.4, 35.1, 35.2, 35.4, 35.5,
35.8, 41.7**

Strategy
========

Each Hypothesis case draws *one* full Slice 1 + Slice 2 + Slice 3
pipeline scenario carrying:

- Random ``content_bytes`` for one Source Document.
- One supporting span range anchored against those bytes.
- A non-empty Finding statement.
- Objective / Project / Activity Plan / Plan Revision / Plan
  Approval rationale and name draws.
- Produced Deliverable content bytes.
- A Hypothesis-drawn gap descriptor category from
  ``{unavailable, stale, unresolved}``.

Per generated case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state
   cannot contaminate the byte-equivalence checks. The engine carries
   the Slice 1 schema, the Slice 2 planning schema, the Slice 3
   execution schema, and the Slice 3 deliverable schema.
2. Seeds Parties (the wildcard-view requester, the Slice 3
   contributor, the assignment authority, the plan approver, and the
   resource steward who assigns the role) and grants the requester
   the wildcard ``view`` Role Assignment that satisfies the
   "fully visible" precondition.
3. Seeds the Slice 1 leg through the existing
   :class:`EvidenceRepository` and :class:`KnowledgeService` so the
   Region Occurrence ``span_content_digest_sha256`` recorded at
   occurrence-creation time is byte-equivalent to the digest the
   delegated :meth:`navigate_decision` tail surfaces. One Source
   Document, one Region Occurrence, one non-hypothesis Finding with
   one ``Supports`` Relationship, one Recommendation, and one
   ``Accept`` Decision.
4. Seeds the Slice 2 leg (Objective + Objective Revision + Project +
   Project Revision + Activity Plan + approved Plan Revision + Plan
   Approval Record) through direct INSERTs.
5. Seeds the Slice 3 leg (Work Assignment + Work Event + Time
   Entry + Deliverable Resource + Deliverable Revision + Deliverable
   Expectation + Deliverable Production + Accept-outcome Milestone
   Acceptance + Completion Record) through direct INSERTs.
6. Seeds an additional Work Event with a *restricted* scope
   (Requirement 35.3 redaction surface) and a Provenance Manifest +
   unresolved Omission Entry on the Plan Approval subject
   (Requirement 31.3 / 35.4 gap-descriptor surface).
7. Invokes each of the three navigation entry points five times as
   a *wildcard-view* requester (Requester A — ``view`` on the
   wildcard scope ``"*"`` so every node resolves, including the
   Deliverable Revision whose target scope is the Resource Identity
   rather than ``_SCOPE``). Asserts identity resolution, digest
   equivalence, byte-equivalence across the five repetitions, and
   gap-descriptor surfacing on the delegated planning leg.
8. Invokes :meth:`navigate_completion` once as a *narrow-view*
   requester (Requester B — ``view`` on ``_SCOPE`` only, so the
   restricted Work Event seeded under ``_RESTRICTED_SCOPE`` surfaces
   as a :class:`RedactedNode` while the visible Work Event and the
   Time Entry sibling remain concrete nodes). Asserts the redaction-
   marker shape per AD-WS-9 rule 1.

Hypothesis settings
===================

``@settings(max_examples=100, deadline=2000)`` per task 16.7's task
notes. ``suppress_health_check`` covers ``too_slow`` and
``data_too_large`` because each case allocates a fresh on-disk
SQLite database, layers four schemas, seeds a complete Slice 1 +
Slice 2 + Slice 3 pipeline plus the redaction and gap-descriptor
surfaces, and runs three traversals × five repetitions per case.
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
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    get_policy,
    seed as seed_disclosure_policies,
)
from walking_slice.evidence import (
    CreateDocumentResult,
    CreateRegionResult,
    EvidenceRepository,
)
from walking_slice.execution._persistence import create_execution_schema
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
    ChainGapDescriptor,
    CompletionNode,
    DeliverableProductionNode,
    DeliverableRevisionNode,
    DecisionNode,
    DocumentRevisionNode,
    ExecutionProvenanceTree,
    FindingRevisionNode,
    MilestoneAcceptanceNode,
    MilestoneAcceptanceProductionChain,
    PlanApprovalNode,
    PlanApprovalProvenance,
    ProvenanceNavigator,
    RecommendationRevisionNode,
    RedactedNode,
    RegionOccurrenceNode,
    TimeEntryNode,
    WorkAssignmentExecutionChain,
    WorkAssignmentNode,
    WorkEventNode,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# A single :class:`FixedClock` instant anchors every persisted
# ``recorded_at``. The navigation effective time ``_AT`` falls strictly
# after every seeded row's ``recorded_at`` (and after the requester's
# Role Assignment ``effective_start``) so the wildcard view authority
# is always effective at navigation time.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_AT: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"


# Number of repeated navigation invocations the byte-equivalence
# assertion runs per anchor. The task explicitly names "5 repetitions".
_REPETITIONS: Final[int] = 5


# Scopes. The main pipeline lives under ``_SCOPE`` so a single
# wildcard ``view`` Role Assignment for that scope unlocks every
# happy-path traversal. The ``_RESTRICTED_SCOPE`` is used for the
# extra Work Event seeded outside the requester's authority surface
# so it surfaces as a :class:`RedactedNode` (Requirement 35.3 /
# AD-WS-9 rule 1).
_SCOPE: Final[str] = "property-37-scope"
_RESTRICTED_SCOPE: Final[str] = "property-37-scope/restricted"


# Authority basis identifier persisted on every Slice 3 row's
# ``authority_basis_id`` column. The column has no FK constraint so
# any opaque string is acceptable; centralizing the value keeps the
# seed deterministic across Hypothesis shrinks.
_AUTHORITY_BASIS_UUID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-000000370001"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_UUID
)
_AUTHORITY_BASIS_ID_STR: Final[str] = str(_AUTHORITY_BASIS_UUID)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every schema.

    The navigator crosses four schemas: Slice 1, Slice 2 planning,
    Slice 3 execution, and Slice 3 deliverable_repository. They are
    layered in the same order :func:`walking_slice.app.create_app`
    uses so triggers and FK constraints match production.
    """
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
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


def _new_uuid7() -> str:
    """Mint one canonical-form UUIDv7 string (matches AD-WS-2)."""
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Slice 1 / 2 / 3 seed helpers.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


def _seed_objective(
    engine: Engine,
    *,
    objective_id: str,
    objective_revision_id: str,
    statement: str,
    target_decision_id: str,
    authoring_party_id: str,
) -> None:
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
                    :rev, :oid, NULL, :statement, NULL,
                    :did, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": objective_revision_id,
                "oid": objective_id,
                "statement": statement,
                "did": target_decision_id,
                "party": authoring_party_id,
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
    target_objective_id: str,
    authoring_party_id: str,
) -> None:
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
                    :rev, :pid, NULL, :name, NULL, :oid,
                    :start_d, :end_d, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": project_revision_id,
                "pid": project_id,
                "name": name,
                "oid": target_objective_id,
                "start_d": "2026-01-01",
                "end_d": "2026-12-31",
                "party": authoring_party_id,
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
    authoring_party_id: str,
) -> None:
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
                "party": authoring_party_id,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_approved_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str,
    planned_scope_text: str,
    authoring_party_id: str,
) -> None:
    """Direct INSERT of an approved Plan Revision.

    The AD-WS-19 lifecycle trigger fires only on UPDATE, so the row
    may be inserted with ``lifecycle_state='approved'`` in one
    statement without running the Plan Approval transaction.
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
                "scope_text": planned_scope_text,
                "party": authoring_party_id,
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
    approving_party_id: str,
) -> None:
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
                "party": approving_party_id,
                "basis": _AUTHORITY_BASIS_ID_STR,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str,
    plan_revision_id: str,
    assignee_party_id: str,
    assignment_authority_party_id: str,
    rationale: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Assignment_Records (
                    work_assignment_id, target_plan_revision_id,
                    assignee_party_id, assignment_authority_party_id,
                    assignment_rationale, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :wid, :prev, :assignee, :authority,
                    :rationale, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_assignment_id,
                "prev": plan_revision_id,
                "assignee": assignee_party_id,
                "authority": assignment_authority_party_id,
                "rationale": rationale,
                "abid": _AUTHORITY_BASIS_ID_STR,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_event(
    engine: Engine,
    *,
    work_event_id: str,
    work_assignment_id: str,
    event_kind: str,
    note: str,
    recording_party_id: str,
    applicable_scope: str = _SCOPE,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Event_Records (
                    work_event_id, target_work_assignment_id,
                    event_kind, event_note, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :weid, :wid, :kind, :note,
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "weid": work_event_id,
                "wid": work_assignment_id,
                "kind": event_kind,
                "note": note,
                "party": recording_party_id,
                "abid": _AUTHORITY_BASIS_ID_STR,
                "scope": applicable_scope,
                "ts": _NOW_ISO,
            },
        )


def _seed_time_entry(
    engine: Engine,
    *,
    time_entry_id: str,
    work_assignment_id: str,
    effort_hours: str,
    recording_party_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Time_Entry_Records (
                    time_entry_id, target_work_assignment_id,
                    effort_hours, effort_period_start,
                    effort_period_end, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :tid, :wid, :hours, :start, :end, :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "tid": time_entry_id,
                "wid": work_assignment_id,
                "hours": effort_hours,
                "start": _NOW_ISO,
                "end": _NOW_ISO,
                "party": recording_party_id,
                "abid": _AUTHORITY_BASIS_ID_STR,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable(
    engine: Engine,
    *,
    deliverable_id: str,
    deliverable_revision_id: str,
    work_assignment_id: str,
    content_bytes: bytes,
    digest_hex: str,
    name: str,
    authoring_party_id: str,
) -> None:
    """Insert one Deliverable Resource + Revision pair by direct INSERT.

    The Revision carries ``role_marker = 'generated_output'``
    (Requirement 26.2) and ``originating_work_assignment_id`` so the
    Slice 3 traversal can hop from a produced Revision back through
    the originating Work Assignment.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, :name, :ts)
                """
            ),
            {"did": deliverable_id, "name": name, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes, content_digest_sha256,
                    role_marker, originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :did, 'text/markdown', :bytes, :digest,
                    'generated_output', :wa, :party, :ts
                )
                """
            ),
            {
                "rev": deliverable_revision_id,
                "did": deliverable_id,
                "bytes": content_bytes,
                "digest": digest_hex,
                "wa": work_assignment_id,
                "party": authoring_party_id,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_expectation(
    engine: Engine,
    *,
    deliverable_expectation_id: str,
    deliverable_expectation_revision_id: str,
    project_id: str,
    name: str,
    authoring_party_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectations
                    (deliverable_expectation_id, created_at)
                VALUES (:did, :ts)
                """
            ),
            {"did": deliverable_expectation_id, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectation_Revisions (
                    deliverable_expectation_revision_id,
                    deliverable_expectation_id, parent_revision_id,
                    target_project_id, name, description,
                    deliverable_kind, acceptance_criteria,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :did, NULL, :pid, :name,
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": deliverable_expectation_revision_id,
                "did": deliverable_expectation_id,
                "pid": project_id,
                "name": name,
                "party": authoring_party_id,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production(
    engine: Engine,
    *,
    deliverable_production_id: str,
    work_assignment_id: str,
    deliverable_id: str,
    deliverable_revision_id: str,
    deliverable_expectation_id: str,
    deliverable_expectation_revision_id: str,
    recording_party_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Production_Records (
                    deliverable_production_id, source_work_assignment_id,
                    produced_deliverable_id, produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    production_rationale, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :pid, :wa, :did, :rev, :exp_did, :exp_rev,
                    'Property 37 production.', :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": deliverable_production_id,
                "wa": work_assignment_id,
                "did": deliverable_id,
                "rev": deliverable_revision_id,
                "exp_did": deliverable_expectation_id,
                "exp_rev": deliverable_expectation_revision_id,
                "party": recording_party_id,
                "abid": _AUTHORITY_BASIS_ID_STR,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_milestone_acceptance(
    engine: Engine,
    *,
    milestone_acceptance_id: str,
    deliverable_production_id: str,
    deliverable_id: str,
    deliverable_revision_id: str,
    deliverable_expectation_id: str,
    deliverable_expectation_revision_id: str,
    accepting_party_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Milestone_Acceptance_Records (
                    milestone_acceptance_id,
                    source_deliverable_production_id,
                    produced_deliverable_id,
                    produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    outcome, rationale, accepting_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :mid, :pid, :did, :rev, :exp_did, :exp_rev,
                    'Accept', 'Property 37 acceptance.', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": milestone_acceptance_id,
                "pid": deliverable_production_id,
                "did": deliverable_id,
                "rev": deliverable_revision_id,
                "exp_did": deliverable_expectation_id,
                "exp_rev": deliverable_expectation_revision_id,
                "party": accepting_party_id,
                "abid": _AUTHORITY_BASIS_ID_STR,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_completion(
    engine: Engine,
    *,
    completion_id: str,
    plan_revision_id: str,
    activity_plan_id: str,
    project_id: str,
    completing_party_id: str,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id, target_plan_revision_id,
                    target_activity_plan_id, target_project_id,
                    outcome, rationale,
                    source_milestone_acceptance_ids_json,
                    completing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :cid, :prev, :aid, :pid, 'Completed',
                    'Property 37 completion.', '[]', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": completion_id,
                "prev": plan_revision_id,
                "aid": activity_plan_id,
                "pid": project_id,
                "party": completing_party_id,
                "abid": _AUTHORITY_BASIS_ID_STR,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_plan_approval_gap_descriptor(
    engine: Engine,
    *,
    plan_approval_id: str,
    authoring_party_id: str,
    omission_category: str,
) -> None:
    """Seed one unresolved Omission Entry on the Plan Approval manifest.

    The manifest is recorded with ``subject_kind='plan_approval'`` so
    the ``Provenance_Manifests.subject_kind`` CHECK constraint
    accepts it. The Omission Entry carries ``resolved_at=NULL`` and a
    category from ``{unavailable, stale, unresolved}`` so the
    navigator's :meth:`_collect_gap_descriptors_for_subject` helper
    surfaces it on every traversal that delegates to
    :meth:`navigate_plan_approval`.
    """
    manifest_id = _new_uuid7()
    omission_id = _new_uuid7()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Provenance_Manifests (
                    manifest_id, subject_kind, subject_id,
                    subject_revision_id, authoring_party_id,
                    recorded_at, included_sources_json, is_complete
                ) VALUES (
                    :mid, 'plan_approval', :sid, NULL, :party,
                    :ts, '[]', 0
                )
                """
            ),
            {
                "mid": manifest_id,
                "sid": plan_approval_id,
                "party": authoring_party_id,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Omission_Entries (
                    omission_entry_id, manifest_id,
                    excluded_source_id, excluded_source_revision_id,
                    category, rationale, authoring_party_id,
                    recorded_at, resolved_at
                ) VALUES (
                    :oid, :mid, :sid, NULL, :category,
                    'Property 37 omission.', :party, :ts, NULL
                )
                """
            ),
            {
                "oid": omission_id,
                "mid": manifest_id,
                "sid": _new_uuid7(),
                "category": omission_category,
                "party": authoring_party_id,
                "ts": _NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


# Restricted alphabet keeps shrunken counterexamples readable and
# avoids drawing control characters some SQLite drivers reject. The
# Property 37 assertions are about identity resolution, digest
# correctness, byte-equivalent retrieval, redaction, and gap-descriptor
# surfacing — not UTF-8 robustness.
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
def _pipeline_strategy(draw) -> dict[str, Any]:
    """Draw one full Slice 1 + Slice 2 + Slice 3 pipeline scenario.

    Returns a dict carrying:

    - ``content_bytes``: Source Document content; 1..96 bytes drawn
      from the full SQLite BLOB alphabet so the digest assertion is
      exercised against non-printable byte values too.
    - ``span``: One valid ``(start, end)`` range inside
      ``content_bytes``.
    - ``finding_statement``: Non-empty Finding statement.
    - ``objective_statement`` / ``project_name`` /
      ``activity_plan_title`` / ``planned_scope`` /
      ``approval_rationale`` / ``work_assignment_rationale`` /
      ``work_event_note`` / ``effort_hours`` /
      ``deliverable_name``: text fields persisted on Slice 2 / Slice 3
      rows.
    - ``deliverable_bytes``: Produced Deliverable content payload.
    - ``omission_category``: One of
      ``{"unavailable", "stale", "unresolved"}`` — drawn so the
      gap-descriptor surface covers each of the three categories
      Property 37 names.
    """
    content_length = draw(st.integers(min_value=1, max_value=96))
    content_bytes = draw(
        st.binary(min_size=content_length, max_size=content_length)
    )
    start = draw(st.integers(min_value=0, max_value=content_length - 1))
    end = draw(st.integers(min_value=start + 1, max_value=content_length))
    deliverable_bytes = draw(st.binary(min_size=1, max_size=128))
    # ``effort_hours`` is persisted as a decimal-string with two
    # digits of precision (Requirement 25.2). Draw the integer
    # component in 0..24 and the fractional component in 0..99 so the
    # resulting string is always valid against the schema CHECK.
    int_hours = draw(st.integers(min_value=0, max_value=23))
    frac_hours = draw(st.integers(min_value=0, max_value=99))
    effort_hours = f"{int_hours}.{frac_hours:02d}"
    return {
        "content_bytes": content_bytes,
        "span": (start, end),
        "finding_statement": draw(_bounded_text(1, 80)),
        "objective_statement": draw(_bounded_text(1, 120)),
        "project_name": draw(_bounded_text(1, 120)),
        "activity_plan_title": draw(_bounded_text(1, 120)),
        "planned_scope": draw(_bounded_text(1, 120)),
        "approval_rationale": draw(_bounded_text(1, 120)),
        "work_assignment_rationale": draw(_bounded_text(1, 120)),
        "work_event_note": draw(_bounded_text(1, 120)),
        "effort_hours": effort_hours,
        "deliverable_name": draw(_bounded_text(1, 120)),
        "deliverable_bytes": deliverable_bytes,
        "omission_category": draw(
            st.sampled_from(("unavailable", "stale", "unresolved"))
        ),
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 37: Execution Provenance Chain end-to-end
@given(scenario=_pipeline_strategy())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_execution_provenance_chain_end_to_end(
    scenario: dict[str, Any],
) -> None:
    """Every navigation entry point returns the full ordered chain,
    every identity resolves, the Region Occurrence digest matches,
    five repetitions are byte-equivalent, restricted nodes surface as
    redaction markers, and unresolved Omission Entries surface as
    gap descriptors.

    Validates Requirements 31.2, 31.3, 31.4, 35.1, 35.2, 35.4, 35.5,
    35.8, 41.7.
    """
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop37_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # ----- Services (fresh per case so IdentityService state
            # cannot bleed across cases).
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
            knowledge_service = KnowledgeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )

            # Disclosure policy seeded once so the navigator can
            # surface gap descriptors via
            # :meth:`_collect_gap_descriptors_for_subject`.
            seed_disclosure_policies(engine)
            policy = get_policy(engine, SLICE_DEFAULT_POLICY_ID)
            navigator = ProvenanceNavigator(
                clock=clock,
                authorization_service=authorization_service,
                disclosure_policy=policy,
            )

            # ----- 1. Mint identities and seed Parties --------------
            requester_a_party_id = _new_uuid7()
            requester_b_party_id = _new_uuid7()
            authoring_party_id = _new_uuid7()
            contributor_party_id = _new_uuid7()
            assignment_authority_id = _new_uuid7()
            approving_party_id = _new_uuid7()
            assigning_authority_id = _new_uuid7()

            with engine.begin() as conn:
                _seed_party(
                    conn,
                    requester_a_party_id,
                    "Property 37 Reader (wildcard view)",
                )
                _seed_party(
                    conn,
                    requester_b_party_id,
                    "Property 37 Reader (narrow view)",
                )
                _seed_party(
                    conn, authoring_party_id, "Property 37 Author"
                )
                _seed_party(
                    conn, contributor_party_id, "Property 37 Contributor"
                )
                _seed_party(
                    conn,
                    assignment_authority_id,
                    "Property 37 Assignment Authority",
                )
                _seed_party(
                    conn,
                    approving_party_id,
                    "Property 37 Plan Approver",
                )
                _seed_party(
                    conn,
                    assigning_authority_id,
                    "Property 37 Resource Steward",
                )

            # ----- 2. Grant the two requesting Parties ``view`` ----
            #
            # Requester A holds the wildcard ``view`` Role Assignment
            # so every Slice 1 / Slice 2 / Slice 3 node resolves —
            # including the Deliverable Revision whose target scope
            # is the Deliverable Resource Identity rather than
            # ``_SCOPE``. This satisfies the "fully visible to the
            # requesting Party" precondition of Property 37 for the
            # identity-resolution, digest, byte-equivalence, and
            # gap-descriptor assertions.
            #
            # Requester B holds a narrow ``view`` Role Assignment on
            # ``_SCOPE`` so the Work Event seeded under
            # ``_RESTRICTED_SCOPE`` falls outside the covered scope
            # and surfaces as a :class:`RedactedNode`
            # (Requirement 35.3 / AD-WS-9 rule 1). Every other
            # ``_SCOPE``-anchored Slice 3 row remains visible to
            # Requester B; the Deliverable Revision (target scope =
            # Resource Identity) is also denied, but that does not
            # affect the redaction assertion because the Work Event
            # check uses ``navigate_completion`` which surfaces the
            # redacted Revision as a marker under its Milestone
            # Acceptance chain rather than raising.
            with engine.begin() as conn:
                authorization_service.assign_role(
                    conn,
                    AssignRoleRequest(
                        party_id=requester_a_party_id,
                        role_name="reviewer-wildcard",
                        scope="*",
                        authorities_granted=("view",),
                        effective_start=_NOW,
                        effective_end=None,
                        assigning_authority_id=assigning_authority_id,
                    ),
                )
                authorization_service.assign_role(
                    conn,
                    AssignRoleRequest(
                        party_id=requester_b_party_id,
                        role_name="reviewer-narrow",
                        scope=_SCOPE,
                        authorities_granted=("view",),
                        effective_start=_NOW,
                        effective_end=None,
                        assigning_authority_id=assigning_authority_id,
                    ),
                )

            # ----- 3. Slice 1 leg via EvidenceRepository +
            # KnowledgeService so the Region Occurrence digest
            # recorded at occurrence-creation time is byte-equivalent
            # to the digest the delegated navigate_decision tail
            # surfaces.
            content_bytes: bytes = scenario["content_bytes"]
            start, end = scenario["span"]
            with engine.begin() as conn:
                doc: CreateDocumentResult = (
                    evidence_repository.create_document(
                        conn,
                        content_bytes=content_bytes,
                        contributing_party_id=authoring_party_id,
                        authority="authoritative",
                    )
                )
                region: CreateRegionResult = (
                    evidence_repository.create_region_occurrence(
                        conn,
                        resource_id=doc.resource_id,
                        revision_id=doc.revision_id,
                        start_offset_bytes=start,
                        end_offset_bytes=end,
                        contributing_party_id=authoring_party_id,
                    )
                )
                finding: CreateFindingResult = (
                    knowledge_service.create_finding(
                        conn,
                        statement=scenario["finding_statement"],
                        authoring_party_id=authoring_party_id,
                        is_hypothesis=False,
                        supporting_region_occurrences=(
                            SupportRef(
                                region_id=region.region_id,
                                document_revision_id=doc.revision_id,
                            ),
                        ),
                    )
                )
                recommendation: CreateRecommendationResult = (
                    knowledge_service.create_recommendation(
                        conn,
                        authoring_party_id=authoring_party_id,
                        derived_from_findings=[finding.finding_id],
                        rationale="Property 37 recommendation.",
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
                        rationale="Property 37 decision.",
                        deciding_party_id=authoring_party_id,
                        authority_basis=_AUTHORITY_BASIS,
                        applicable_scope=_SCOPE,
                    )
                )

            # Recompute the expected Region digest independently of
            # the Evidence_Repository so the assertion does not trust
            # the persisted value blindly.
            expected_span_bytes = content_bytes[start:end]
            expected_span_digest = hashlib.sha256(
                expected_span_bytes
            ).hexdigest()

            # ----- 4. Slice 2 leg via direct INSERTs ----------------
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
                target_decision_id=decision.decision_id,
                authoring_party_id=authoring_party_id,
            )
            _seed_project(
                engine,
                project_id=project_id,
                project_revision_id=project_revision_id,
                name=scenario["project_name"],
                target_objective_id=objective_id,
                authoring_party_id=authoring_party_id,
            )
            _seed_activity_plan(
                engine,
                activity_plan_id=activity_plan_id,
                target_project_id=project_id,
                title=scenario["activity_plan_title"],
                authoring_party_id=authoring_party_id,
            )
            _seed_approved_plan_revision(
                engine,
                plan_revision_id=plan_revision_id,
                activity_plan_id=activity_plan_id,
                planned_scope_text=scenario["planned_scope"],
                authoring_party_id=authoring_party_id,
            )
            _seed_plan_approval(
                engine,
                plan_approval_id=plan_approval_id,
                target_activity_plan_id=activity_plan_id,
                target_plan_revision_id=plan_revision_id,
                rationale=scenario["approval_rationale"],
                approving_party_id=approving_party_id,
            )

            # ----- 5. Slice 3 leg via direct INSERTs ----------------
            work_assignment_id = _new_uuid7()
            visible_work_event_id = _new_uuid7()
            restricted_work_event_id = _new_uuid7()
            time_entry_id = _new_uuid7()
            deliverable_id = _new_uuid7()
            deliverable_revision_id = _new_uuid7()
            deliverable_expectation_id = _new_uuid7()
            deliverable_expectation_revision_id = _new_uuid7()
            deliverable_production_id = _new_uuid7()
            milestone_acceptance_id = _new_uuid7()
            completion_id = _new_uuid7()

            deliverable_bytes: bytes = scenario["deliverable_bytes"]
            expected_deliverable_digest = hashlib.sha256(
                deliverable_bytes
            ).hexdigest()

            _seed_work_assignment(
                engine,
                work_assignment_id=work_assignment_id,
                plan_revision_id=plan_revision_id,
                assignee_party_id=contributor_party_id,
                assignment_authority_party_id=assignment_authority_id,
                rationale=scenario["work_assignment_rationale"],
            )
            # Visible Work Event under ``_SCOPE`` — confirms identity
            # resolution and byte-equivalence.
            _seed_work_event(
                engine,
                work_event_id=visible_work_event_id,
                work_assignment_id=work_assignment_id,
                event_kind="progress_note",
                note=scenario["work_event_note"],
                recording_party_id=contributor_party_id,
                applicable_scope=_SCOPE,
            )
            # Restricted Work Event under ``_RESTRICTED_SCOPE`` —
            # confirms redaction marker shape.
            _seed_work_event(
                engine,
                work_event_id=restricted_work_event_id,
                work_assignment_id=work_assignment_id,
                event_kind="progress_note",
                note="Restricted progress note.",
                recording_party_id=contributor_party_id,
                applicable_scope=_RESTRICTED_SCOPE,
            )
            _seed_time_entry(
                engine,
                time_entry_id=time_entry_id,
                work_assignment_id=work_assignment_id,
                effort_hours=scenario["effort_hours"],
                recording_party_id=contributor_party_id,
            )
            _seed_deliverable(
                engine,
                deliverable_id=deliverable_id,
                deliverable_revision_id=deliverable_revision_id,
                work_assignment_id=work_assignment_id,
                content_bytes=deliverable_bytes,
                digest_hex=expected_deliverable_digest,
                name=scenario["deliverable_name"],
                authoring_party_id=contributor_party_id,
            )
            _seed_deliverable_expectation(
                engine,
                deliverable_expectation_id=deliverable_expectation_id,
                deliverable_expectation_revision_id=(
                    deliverable_expectation_revision_id
                ),
                project_id=project_id,
                name=scenario["deliverable_name"],
                authoring_party_id=authoring_party_id,
            )
            _seed_deliverable_production(
                engine,
                deliverable_production_id=deliverable_production_id,
                work_assignment_id=work_assignment_id,
                deliverable_id=deliverable_id,
                deliverable_revision_id=deliverable_revision_id,
                deliverable_expectation_id=deliverable_expectation_id,
                deliverable_expectation_revision_id=(
                    deliverable_expectation_revision_id
                ),
                recording_party_id=contributor_party_id,
            )
            _seed_milestone_acceptance(
                engine,
                milestone_acceptance_id=milestone_acceptance_id,
                deliverable_production_id=deliverable_production_id,
                deliverable_id=deliverable_id,
                deliverable_revision_id=deliverable_revision_id,
                deliverable_expectation_id=deliverable_expectation_id,
                deliverable_expectation_revision_id=(
                    deliverable_expectation_revision_id
                ),
                accepting_party_id=approving_party_id,
            )
            _seed_completion(
                engine,
                completion_id=completion_id,
                plan_revision_id=plan_revision_id,
                activity_plan_id=activity_plan_id,
                project_id=project_id,
                completing_party_id=approving_party_id,
            )

            # ----- 6. Gap descriptor seed on the Plan Approval ----
            _seed_plan_approval_gap_descriptor(
                engine,
                plan_approval_id=plan_approval_id,
                authoring_party_id=approving_party_id,
                omission_category=scenario["omission_category"],
            )

            # ----- 7. Navigate each anchor 5 times and assert -----
            expected = {
                "decision_id": decision.decision_id,
                "recommendation_id": (
                    recommendation.recommendation_id
                ),
                "recommendation_revision_id": (
                    recommendation.recommendation_revision_id
                ),
                "finding_id": finding.finding_id,
                "finding_revision_id": finding.finding_revision_id,
                "document_resource_id": doc.resource_id,
                "document_revision_id": doc.revision_id,
                "region_id": region.region_id,
                "expected_span_bytes": expected_span_bytes,
                "expected_span_digest": expected_span_digest,
                "objective_id": objective_id,
                "objective_revision_id": objective_revision_id,
                "project_id": project_id,
                "project_revision_id": project_revision_id,
                "activity_plan_id": activity_plan_id,
                "plan_revision_id": plan_revision_id,
                "plan_approval_id": plan_approval_id,
                "work_assignment_id": work_assignment_id,
                "visible_work_event_id": visible_work_event_id,
                "restricted_work_event_id": (
                    restricted_work_event_id
                ),
                "time_entry_id": time_entry_id,
                "deliverable_id": deliverable_id,
                "deliverable_revision_id": deliverable_revision_id,
                "deliverable_expectation_id": (
                    deliverable_expectation_id
                ),
                "deliverable_expectation_revision_id": (
                    deliverable_expectation_revision_id
                ),
                "deliverable_production_id": (
                    deliverable_production_id
                ),
                "milestone_acceptance_id": milestone_acceptance_id,
                "completion_id": completion_id,
                "expected_deliverable_digest": (
                    expected_deliverable_digest
                ),
                "approval_rationale": scenario["approval_rationale"],
                "omission_category": scenario["omission_category"],
            }

            completion_trees = _navigate_repeated(
                navigator,
                engine,
                anchor_kind="completion_record",
                anchor_id=completion_id,
                party_id=requester_a_party_id,
            )
            production_trees = _navigate_repeated(
                navigator,
                engine,
                anchor_kind="deliverable_production_record",
                anchor_id=deliverable_production_id,
                party_id=requester_a_party_id,
            )
            revision_trees = _navigate_repeated(
                navigator,
                engine,
                anchor_kind="deliverable_revision",
                anchor_id=deliverable_revision_id,
                party_id=requester_a_party_id,
            )

            _assert_completion_anchor(completion_trees[0], expected)
            _assert_production_anchor(production_trees[0], expected)
            _assert_revision_anchor(revision_trees[0], expected)

            for trees, anchor in (
                (completion_trees, "completion"),
                (production_trees, "production"),
                (revision_trees, "revision"),
            ):
                _assert_byte_equivalent(trees, anchor=anchor)
                _assert_planning_leg_identities_and_digest(
                    trees[0], expected, anchor=anchor
                )
                _assert_work_assignment_fully_visible(
                    trees[0], expected, anchor=anchor
                )
                _assert_gap_descriptor(trees[0], expected, anchor=anchor)

            # ----- 8. Redaction pass: Requester B (narrow view) --
            #
            # The restricted Work Event seeded under
            # ``_RESTRICTED_SCOPE`` is denied for Requester B and
            # surfaces as a :class:`RedactedNode`. The visible Work
            # Event under ``_SCOPE`` remains a concrete
            # :class:`WorkEventNode`. Property 37 (e) only requires
            # that a restricted node surface as a redaction marker;
            # this pass exercises that exactly once per case. We use
            # the :meth:`navigate_completion` entry point because the
            # Completion record is covered by Requester B's scope so
            # the navigation does not raise.
            with engine.connect() as conn:
                redacted_tree = navigator.navigate_completion(
                    conn,
                    completion_id=completion_id,
                    party_id=requester_b_party_id,
                    at=_AT,
                )
            _assert_redaction_marker(redacted_tree, expected)
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Navigation runner.
# ---------------------------------------------------------------------------


def _navigate_repeated(
    navigator: ProvenanceNavigator,
    engine: Engine,
    *,
    anchor_kind: str,
    anchor_id: str,
    party_id: str,
) -> list[ExecutionProvenanceTree]:
    """Invoke the navigator entry point named by ``anchor_kind`` 5 times.

    Each invocation runs on its own read-only ``connect()`` so the
    five trees are byte-equivalent across distinct connections, not
    just within one cached cursor (Requirement 31.4 / 35.5).
    """
    trees: list[ExecutionProvenanceTree] = []
    for _ in range(_REPETITIONS):
        with engine.connect() as conn:
            if anchor_kind == "completion_record":
                trees.append(
                    navigator.navigate_completion(
                        conn,
                        completion_id=anchor_id,
                        party_id=party_id,
                        at=_AT,
                    )
                )
            elif anchor_kind == "deliverable_production_record":
                trees.append(
                    navigator.navigate_deliverable_production(
                        conn,
                        deliverable_production_id=anchor_id,
                        party_id=party_id,
                        at=_AT,
                    )
                )
            elif anchor_kind == "deliverable_revision":
                trees.append(
                    navigator.navigate_produced_deliverable_revision(
                        conn,
                        deliverable_revision_id=anchor_id,
                        party_id=party_id,
                        at=_AT,
                    )
                )
            else:  # pragma: no cover - defensive
                raise AssertionError(
                    f"unsupported anchor_kind={anchor_kind!r}"
                )
    return trees


# ---------------------------------------------------------------------------
# Per-anchor invariants.
# ---------------------------------------------------------------------------


def _assert_completion_anchor(
    tree: ExecutionProvenanceTree, exp: dict[str, Any]
) -> None:
    """Requirement 31.2 / 35.1 — Completion anchor head and three legs."""
    assert tree.requested_anchor_kind == "completion_record"
    assert tree.requested_anchor_id == exp["completion_id"]
    assert tree.production_anchor is None
    assert tree.produced_revision_anchor is None

    assert isinstance(tree.completion, CompletionNode)
    completion = tree.completion
    assert completion.completion_id == exp["completion_id"]
    assert completion.target_plan_revision_id == exp["plan_revision_id"]
    assert completion.target_activity_plan_id == exp["activity_plan_id"]
    assert completion.target_project_id == exp["project_id"]
    assert completion.outcome == "Completed"

    # Planning leg attached.
    assert isinstance(tree.plan_approval_chain, PlanApprovalProvenance)
    # Work Assignment leg carries the one Work Assignment.
    assert len(tree.work_assignment_chains) == 1
    wa_chain = tree.work_assignment_chains[0]
    assert isinstance(wa_chain.work_assignment, WorkAssignmentNode)
    assert wa_chain.work_assignment.work_assignment_id == (
        exp["work_assignment_id"]
    )
    # Milestone Acceptance leg carries the accepted Milestone.
    assert len(tree.milestone_acceptance_chains) == 1
    mac = tree.milestone_acceptance_chains[0]
    assert isinstance(mac, MilestoneAcceptanceProductionChain)
    assert isinstance(mac.milestone_acceptance, MilestoneAcceptanceNode)
    assert mac.milestone_acceptance.milestone_acceptance_id == (
        exp["milestone_acceptance_id"]
    )
    assert mac.milestone_acceptance.outcome == "Accept"
    assert isinstance(
        mac.deliverable_production, DeliverableProductionNode
    )
    assert mac.deliverable_production.deliverable_production_id == (
        exp["deliverable_production_id"]
    )
    # Requirement 35.8 — produced Revision carries role_marker and digest.
    assert isinstance(
        mac.produced_deliverable_revision, DeliverableRevisionNode
    )
    revision_node = mac.produced_deliverable_revision
    assert revision_node.deliverable_id == exp["deliverable_id"]
    assert revision_node.deliverable_revision_id == (
        exp["deliverable_revision_id"]
    )
    assert revision_node.role_marker == "generated_output"
    assert revision_node.content_digest_sha256 == (
        exp["expected_deliverable_digest"]
    )
    assert revision_node.originating_work_assignment_id == (
        exp["work_assignment_id"]
    )


def _assert_production_anchor(
    tree: ExecutionProvenanceTree, exp: dict[str, Any]
) -> None:
    """Requirement 31.2 / 35.1 — Production anchor head and chain."""
    assert tree.requested_anchor_kind == "deliverable_production_record"
    assert tree.requested_anchor_id == exp["deliverable_production_id"]
    assert tree.completion is None
    assert tree.milestone_acceptance_chains == ()

    assert isinstance(
        tree.production_anchor, DeliverableProductionNode
    )
    production = tree.production_anchor
    assert production.deliverable_production_id == (
        exp["deliverable_production_id"]
    )
    assert production.source_work_assignment_id == (
        exp["work_assignment_id"]
    )
    assert production.produced_deliverable_revision_id == (
        exp["deliverable_revision_id"]
    )

    # produced Revision surfaced as anchor sibling.
    assert isinstance(
        tree.produced_revision_anchor, DeliverableRevisionNode
    )
    revision = tree.produced_revision_anchor
    assert revision.deliverable_id == exp["deliverable_id"]
    assert revision.deliverable_revision_id == (
        exp["deliverable_revision_id"]
    )
    # Requirement 35.8 — role_marker and digest on the Production-anchor
    # revision sibling.
    assert revision.role_marker == "generated_output"
    assert revision.content_digest_sha256 == (
        exp["expected_deliverable_digest"]
    )

    # Planning leg attached.
    assert isinstance(tree.plan_approval_chain, PlanApprovalProvenance)
    # Source Work Assignment leg.
    assert len(tree.work_assignment_chains) == 1
    wa_chain = tree.work_assignment_chains[0]
    assert isinstance(wa_chain.work_assignment, WorkAssignmentNode)
    assert wa_chain.work_assignment.work_assignment_id == (
        exp["work_assignment_id"]
    )


def _assert_revision_anchor(
    tree: ExecutionProvenanceTree, exp: dict[str, Any]
) -> None:
    """Requirement 31.2 / 35.1 / 35.8 — Revision anchor head and chain."""
    assert tree.requested_anchor_kind == "deliverable_revision"
    assert tree.requested_anchor_id == exp["deliverable_revision_id"]
    assert tree.completion is None
    assert tree.production_anchor is None
    assert tree.milestone_acceptance_chains == ()

    assert isinstance(
        tree.produced_revision_anchor, DeliverableRevisionNode
    )
    revision = tree.produced_revision_anchor
    assert revision.deliverable_id == exp["deliverable_id"]
    assert revision.deliverable_revision_id == (
        exp["deliverable_revision_id"]
    )
    # Requirement 35.8 — role_marker and digest on the head node.
    assert revision.role_marker == "generated_output"
    assert revision.content_digest_sha256 == (
        exp["expected_deliverable_digest"]
    )
    assert revision.originating_work_assignment_id == (
        exp["work_assignment_id"]
    )

    # Planning leg attached.
    assert isinstance(tree.plan_approval_chain, PlanApprovalProvenance)
    # Originating Work Assignment leg.
    assert len(tree.work_assignment_chains) == 1
    wa_chain = tree.work_assignment_chains[0]
    assert isinstance(wa_chain.work_assignment, WorkAssignmentNode)
    assert wa_chain.work_assignment.work_assignment_id == (
        exp["work_assignment_id"]
    )


# ---------------------------------------------------------------------------
# Cross-anchor invariants.
# ---------------------------------------------------------------------------


def _assert_byte_equivalent(
    trees: list[ExecutionProvenanceTree], *, anchor: str
) -> None:
    """Requirement 31.4 / 35.5 — 5 repetitions yield equal trees."""
    first = trees[0]
    for index, repeat in enumerate(trees[1:], start=2):
        assert repeat == first, (
            f"Property 37 ({anchor} anchor): repetition #{index} "
            "returned a different ExecutionProvenanceTree than the "
            "first invocation. Requirements 31.4 / 35.5 require "
            "byte-equivalent results across repeated invocations "
            f"with the same arguments.\nfirst={first!r}\n"
            f"repeat#{index}={repeat!r}"
        )


def _assert_planning_leg_identities_and_digest(
    tree: ExecutionProvenanceTree,
    exp: dict[str, Any],
    *,
    anchor: str,
) -> None:
    """Requirement 31.2 / 35.1 / 35.2 — planning leg identities + digest."""
    chain = tree.plan_approval_chain
    assert isinstance(chain, PlanApprovalProvenance), (
        f"Property 37 ({anchor} anchor): plan_approval_chain must be "
        "a PlanApprovalProvenance under wildcard view authority; got "
        f"{type(chain).__name__}."
    )

    # Plan Approval head.
    assert isinstance(chain.plan_approval, PlanApprovalNode)
    pa = chain.plan_approval
    assert pa.plan_approval_id == exp["plan_approval_id"]
    assert pa.target_activity_plan_id == exp["activity_plan_id"]
    assert pa.target_plan_revision_id == exp["plan_revision_id"]
    assert pa.outcome == "Approve"
    assert pa.rationale == exp["approval_rationale"]
    assert chain.requested_plan_approval_id == exp["plan_approval_id"]

    # Slice 2 prefix identities.
    assert chain.plan_revision.plan_revision_id == (
        exp["plan_revision_id"]
    )
    assert chain.plan_revision.activity_plan_id == (
        exp["activity_plan_id"]
    )
    assert chain.plan_revision.lifecycle_state == "approved"
    assert chain.activity_plan.activity_plan_id == (
        exp["activity_plan_id"]
    )
    assert chain.activity_plan.target_project_id == exp["project_id"]
    assert chain.project_revision.project_id == exp["project_id"]
    assert chain.project_revision.project_revision_id == (
        exp["project_revision_id"]
    )
    assert chain.objective_revision.objective_id == exp["objective_id"]
    assert chain.objective_revision.objective_revision_id == (
        exp["objective_revision_id"]
    )
    assert chain.objective_revision.target_decision_id == (
        exp["decision_id"]
    )

    # Slice 1 Decision tail.
    decision_chain = chain.decision_chain
    assert decision_chain is not None, (
        f"Property 37 ({anchor} anchor): Decision tail must be "
        "present under wildcard view authority."
    )
    assert isinstance(decision_chain.decision, DecisionNode)
    assert decision_chain.decision.decision_id == exp["decision_id"]
    assert isinstance(
        decision_chain.recommendation_revision,
        RecommendationRevisionNode,
    )
    assert decision_chain.recommendation_revision.recommendation_id == (
        exp["recommendation_id"]
    )
    assert (
        decision_chain.recommendation_revision.recommendation_revision_id
        == exp["recommendation_revision_id"]
    )

    assert len(decision_chain.findings) == 1
    finding_node = decision_chain.findings[0]
    assert isinstance(finding_node, FindingRevisionNode)
    assert finding_node.finding_id == exp["finding_id"]
    assert finding_node.finding_revision_id == (
        exp["finding_revision_id"]
    )

    # Region Occurrence + Document Revision pairing.
    assert len(decision_chain.region_occurrences) == 1
    assert len(decision_chain.document_revisions) == 1
    region_node = decision_chain.region_occurrences[0]
    doc_node = decision_chain.document_revisions[0]
    assert isinstance(region_node, RegionOccurrenceNode)
    assert isinstance(doc_node, DocumentRevisionNode)
    assert region_node.region_id == exp["region_id"]
    assert doc_node.resource_id == exp["document_resource_id"]
    assert doc_node.revision_id == exp["document_revision_id"]

    # Requirement 35.2 — Region span digest matches the recorded
    # digest (recomputed independently from the scenario bytes).
    expected_bytes: bytes = exp["expected_span_bytes"]
    expected_digest: str = exp["expected_span_digest"]
    assert region_node.bounded_text == expected_bytes, (
        f"Property 37 ({anchor} anchor): Region bounded_text "
        "diverges from content_bytes[start:end]; Requirement 35.2 "
        "(inheriting Slice 1 Requirement 11.2) requires byte-"
        "equivalent span resolution."
    )
    computed_from_text = hashlib.sha256(
        region_node.bounded_text
    ).hexdigest()
    assert region_node.span_content_digest_sha256 == computed_from_text, (
        f"Property 37 ({anchor} anchor): "
        "span_content_digest_sha256 does not equal "
        "SHA-256(bounded_text); Requirement 35.2 requires digest-"
        "equivalence at navigation time."
    )
    assert (
        region_node.span_content_digest_sha256 == expected_digest
    ), (
        f"Property 37 ({anchor} anchor): "
        "span_content_digest_sha256 diverges from the SHA-256 of "
        "the scenario span bytes; Requirement 35.2 requires the "
        "navigated digest to match the digest the "
        "Evidence_Repository recorded at occurrence creation."
    )


def _assert_work_assignment_fully_visible(
    tree: ExecutionProvenanceTree,
    exp: dict[str, Any],
    *,
    anchor: str,
) -> None:
    """Requirement 31.2 / 35.1 — under wildcard view, every Work Event
    and the Time Entry sibling resolve to concrete visible nodes.

    Both Work Events seeded by the test (visible under ``_SCOPE`` and
    ``restricted`` under ``_RESTRICTED_SCOPE``) are covered by
    Requester A's wildcard ``view`` Role Assignment so the navigator
    must surface both as :class:`WorkEventNode` instances rather than
    redaction markers. The Time Entry sibling is also visible.

    All three anchors load exactly the same single Work Assignment
    chain — the Completion anchor walks every Work Assignment for
    the Plan Revision, and the Production / Revision anchors walk
    the single source / originating Work Assignment — so the
    assertion applies uniformly.
    """
    assert len(tree.work_assignment_chains) == 1
    wa_chain = tree.work_assignment_chains[0]
    assert isinstance(wa_chain, WorkAssignmentExecutionChain)
    assert isinstance(wa_chain.work_assignment, WorkAssignmentNode)
    assert wa_chain.work_assignment.work_assignment_id == (
        exp["work_assignment_id"]
    )

    # Both Work Events visible (no RedactedNode in any position).
    assert len(wa_chain.work_events) == 2, (
        f"Property 37 ({anchor} anchor) wildcard view: both seeded "
        f"Work Events must resolve; got {len(wa_chain.work_events)}."
    )
    visible_ids: list[str] = []
    for event_node in wa_chain.work_events:
        assert isinstance(event_node, WorkEventNode), (
            f"Property 37 ({anchor} anchor): Work Event must be a "
            "concrete WorkEventNode under wildcard view authority; "
            f"got {type(event_node).__name__}."
        )
        visible_ids.append(event_node.work_event_id)
    assert sorted(visible_ids) == sorted(
        [exp["visible_work_event_id"], exp["restricted_work_event_id"]]
    )

    # Time Entry visible.
    assert len(wa_chain.time_entries) == 1
    time_node = wa_chain.time_entries[0]
    assert isinstance(time_node, TimeEntryNode)
    assert time_node.time_entry_id == exp["time_entry_id"]


def _assert_redaction_marker(
    tree: ExecutionProvenanceTree,
    exp: dict[str, Any],
) -> None:
    """Requirement 35.3 / AD-WS-9 rule 1 — restricted node is a marker.

    The restricted Work Event was seeded under ``_RESTRICTED_SCOPE``,
    outside Requester B's narrow ``view`` authority scope, so it
    surfaces as a :class:`RedactedNode` carrying only ``kind`` and
    ``redacted=True``. The sibling visible Work Event remains a
    concrete :class:`WorkEventNode`. The Time Entry sibling (under
    ``_SCOPE``) is also visible.
    """
    assert len(tree.work_assignment_chains) == 1
    wa_chain = tree.work_assignment_chains[0]
    assert isinstance(wa_chain, WorkAssignmentExecutionChain)
    # The Work Assignment itself is visible under ``_SCOPE``.
    assert isinstance(wa_chain.work_assignment, WorkAssignmentNode)

    visible_count = 0
    redacted_count = 0
    visible_ids: list[str] = []
    for event_node in wa_chain.work_events:
        if isinstance(event_node, WorkEventNode):
            visible_count += 1
            visible_ids.append(event_node.work_event_id)
        elif isinstance(event_node, RedactedNode):
            redacted_count += 1
            # AD-WS-9 rule 1: marker discloses only kind and redacted.
            assert event_node.kind == "work_event_record"
            assert event_node.redacted is True
        else:  # pragma: no cover - defensive
            raise AssertionError(
                "Property 37 redaction pass: unexpected work_event "
                f"node type {type(event_node).__name__}."
            )

    assert visible_count == 1, (
        "Property 37 redaction pass: exactly one Work Event must be "
        f"visible under _SCOPE; got {visible_count}."
    )
    assert redacted_count == 1, (
        "Property 37 redaction pass: exactly one Work Event must "
        "surface as a RedactedNode under _RESTRICTED_SCOPE; got "
        f"{redacted_count}."
    )
    # The visible Work Event resolves to the seeded identity.
    assert visible_ids == [exp["visible_work_event_id"]]

    # The Time Entry remains visible (sibling under ``_SCOPE``).
    assert len(wa_chain.time_entries) == 1
    assert isinstance(wa_chain.time_entries[0], TimeEntryNode)
    assert wa_chain.time_entries[0].time_entry_id == exp["time_entry_id"]


def _assert_gap_descriptor(
    tree: ExecutionProvenanceTree,
    exp: dict[str, Any],
    *,
    anchor: str,
) -> None:
    """Requirement 31.3 / 35.4 / 41.7 — gap descriptor surfaces.

    The seeded unresolved Omission Entry on the Plan Approval
    manifest surfaces as a :class:`ChainGapDescriptor` on the
    delegated planning leg's ``gap_descriptors`` tuple. The
    descriptor carries only ``stage``, ``category``, and (when
    visible) ``next_reachable_node_identity`` per AD-WS-9 rule 2.
    """
    plan_chain = tree.plan_approval_chain
    assert plan_chain is not None
    descriptors = plan_chain.gap_descriptors
    assert len(descriptors) == 1, (
        f"Property 37 ({anchor} anchor): exactly one gap descriptor "
        "must surface on the delegated planning leg from the seeded "
        f"unresolved Omission Entry; got {len(descriptors)}."
    )
    descriptor = descriptors[0]
    assert isinstance(descriptor, ChainGapDescriptor)
    assert descriptor.stage == "plan_approval"
    assert descriptor.category == exp["omission_category"]
    assert descriptor.next_reachable_node_identity == (
        exp["plan_approval_id"]
    )
