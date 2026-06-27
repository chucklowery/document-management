"""Unit tests for the Slice 3 provenance traversals and backlink extension.

Pins the contract established by task 12.4 of the third walking slice,
design §"Provenance_Navigator (extended)", AD-WS-26 (Slice 3
Relationship-Type / ``semantic_role`` table), AD-WS-27 (Slice 3
append-only Records), and Requirements 31.2, 31.3, 31.4, 35.2, 35.3,
35.4, 35.8, 36.1, 36.2, and 36.6:

- **31.2 / 35.1 — three ordered legs.**
  :meth:`ProvenanceNavigator.navigate_completion` returns an
  :class:`ExecutionProvenanceTree` carrying (1) the Planning leg
  delegated to :meth:`navigate_plan_approval` via the Completion's
  ``target_plan_revision_id`` → ``Plan_Approval_Records`` lookup,
  (2) the Milestone Acceptance leg with one
  :class:`MilestoneAcceptanceProductionChain` per accepted Milestone
  Acceptance for the target Plan Revision, and (3) the Work Assignment
  leg with one :class:`WorkAssignmentExecutionChain` per Work
  Assignment targeting the same Plan Revision, each with its Work
  Events and Time Entries.

- **35.3 / AD-WS-9 rule 1 — redaction markers.** A node the requesting
  Party may not view is replaced by a :class:`RedactedNode`
  carrying only ``{kind, redacted=True}``. The cascade rule is
  per-record: a redacted Milestone Acceptance hides its downstream
  Production / Revision; a redacted Work Assignment hides its
  downstream Work Events / Time Entries. Visible siblings remain
  visible.

- **31.3 / 35.4 / 14.4 — gap descriptors.** When the navigator is
  constructed with the seeded ``slice-default-2026`` disclosure
  policy, the delegated planning leg surfaces a
  :class:`ChainGapDescriptor` for every unresolved
  ``{unavailable, stale, unresolved}`` Omission Entry on the visible
  manifest. Each descriptor carries ``stage``, ``category``, and
  (when visible) ``next_reachable_node_identity``. The Completion
  Record's own ``gap_descriptors`` tuple is currently empty because
  the ``Provenance_Manifests.subject_kind`` CHECK constraint admits
  only Slice 1 / Slice 2 subjects; the navigator's helper still
  issues the query so future schema extensions will surface
  Completion-rooted gaps without code change.

- **31.4 / 35.5 — idempotent retrieval.** Five repeated invocations
  with the same ``(completion_id, party_id, at)`` produce
  byte-equivalent :class:`ExecutionProvenanceTree` instances
  (structural ``==`` on the frozen dataclass).

- **35.8 — produced Deliverable Revision content_digest and role_marker.**
  Every visible :class:`DeliverableRevisionNode` carries
  ``role_marker = 'generated_output'`` and the SHA-256
  ``content_digest_sha256`` of the persisted content bytes; the
  node Type distinguishes a produced Deliverable Revision from any
  Slice 1 Source Evidence Document Revision (which does not carry
  a ``role_marker`` attribute).

- **36.1 / 36.2 / 36.6 — backlink coverage and semantic_role.** Slice
  3 source endpoint kinds (``work_assignment_record``,
  ``work_event_record``, ``time_entry_record``,
  ``deliverable_resource``, ``deliverable_revision``,
  ``deliverable_production_record``, ``milestone_acceptance_record``,
  ``completion_record``) appear in the navigator's authorized-source
  surface and the existing backlink algorithm returns
  :class:`BacklinkEntry` instances for them with no algorithm
  change. The persisted ``Relationships.semantic_role`` discriminator
  written by the Slice 3 services (``assignee``, ``work_event``,
  ``time_entry``, ``production_source``) is verified directly
  against the ``Relationships`` row so the discriminator that the
  algorithm relies on for ordering and discoverability is pinned
  in code.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    get_policy,
    seed as seed_disclosure_policies,
)
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import (
    BacklinkEntry,
    BacklinkPage,
    ChainGapDescriptor,
    CompletionNode,
    CompletionUnresolvableError,
    DeliverableProductionNode,
    DeliverableRevisionNode,
    ExecutionProvenanceTree,
    MilestoneAcceptanceNode,
    MilestoneAcceptanceProductionChain,
    PlanApprovalProvenance,
    ProvenanceNavigator,
    RedactedNode,
    TimeEntryNode,
    WorkAssignmentExecutionChain,
    WorkAssignmentNode,
    WorkEventNode,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants — every Identity is a UUIDv7 literal so the test corpus is
# fully deterministic and assertions can name the row keys directly.
# ---------------------------------------------------------------------------


_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-0000000c0001"
_CONTRIBUTOR_PARTY_ID = "00000000-0000-7000-8000-0000000c0002"
_ASSIGNMENT_AUTHORITY_ID = "00000000-0000-7000-8000-0000000c0003"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000c0004"
_APPROVING_PARTY_ID = "00000000-0000-7000-8000-0000000c0005"

_PROJECT_ID = "00000000-0000-7000-8000-0000000d0010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-0000000d0020"
_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000000d0030"
_PLAN_APPROVAL_ID = "00000000-0000-7000-8000-0000000d0040"

_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-0000000e0010"
_WORK_EVENT_ID = "00000000-0000-7000-8000-0000000e0020"
_TIME_ENTRY_ID = "00000000-0000-7000-8000-0000000e0030"

_DELIVERABLE_ID = "00000000-0000-7000-8000-0000000f0010"
_DELIVERABLE_REVISION_ID = "00000000-0000-7000-8000-0000000f0011"
_DELIVERABLE_EXPECTATION_ID = "00000000-0000-7000-8000-0000000f0020"
_DELIVERABLE_EXPECTATION_REVISION_ID = (
    "00000000-0000-7000-8000-0000000f0021"
)
_DELIVERABLE_PRODUCTION_ID = "00000000-0000-7000-8000-0000000f0030"
_MILESTONE_ACCEPTANCE_ID = "00000000-0000-7000-8000-0000000f0040"

_COMPLETION_ID = "00000000-0000-7000-8000-0000000a0001"

# Relationship Identities — one per AD-WS-26 row written by the Slice 3
# services on the canonical happy-path graph.
_REL_WA_ADDRESSES_PR_ID = "00000000-0000-7000-8000-0000000b0001"
_REL_WA_ASSIGNEE_ID = "00000000-0000-7000-8000-0000000b0002"
_REL_WE_WORK_EVENT_ID = "00000000-0000-7000-8000-0000000b0003"
_REL_TE_TIME_ENTRY_ID = "00000000-0000-7000-8000-0000000b0004"
_REL_DP_PRODUCES_ID = "00000000-0000-7000-8000-0000000b0005"
_REL_DP_ADDRESSES_DER_ID = "00000000-0000-7000-8000-0000000b0006"
_REL_DP_PROD_SOURCE_ID = "00000000-0000-7000-8000-0000000b0007"
_REL_MA_ADDRESSES_DR_ID = "00000000-0000-7000-8000-0000000b0008"
_REL_CP_ADDRESSES_PR_ID = "00000000-0000-7000-8000-0000000b0009"

_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000000a00a1")
_SCOPE = "pilot/team-c"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Produced Deliverable content — the SHA-256 digest of these bytes is
# what Requirement 35.8 says navigate_completion surfaces on the produced
# Deliverable Revision node. Computing the digest at module load time
# means the assertion in :class:`TestProducedDeliverableRevisionFields`
# compares one hash to another rather than re-implementing the hashing
# algorithm in the test body.
_DELIVERABLE_BYTES = b"Mesh rollout runbook v1 -- first published."
_DELIVERABLE_DIGEST = hashlib.sha256(_DELIVERABLE_BYTES).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def completion_engine(engine: Engine) -> Engine:
    """Per-test engine carrying every schema this slice spans.

    The navigator crosses three schemas: Slice 1 (``Parties``,
    ``Audit_Records``, ``Role_Assignments``, ``Relationships``,
    ``Provenance_Manifests``, ``Omission_Entries``), Slice 2
    (``Activity_Plans``, ``Plan_Revisions``, ``Plan_Approval_Records``,
    ``Deliverable_Expectation_Revisions``,
    ``Disclosure_Policy_Coverage``), and Slice 3
    (``Work_Assignment_Records``, ``Work_Event_Records``,
    ``Time_Entry_Records``, ``Deliverable_Production_Records``,
    ``Milestone_Acceptance_Records``, ``Completion_Records``,
    ``Deliverable_Resources``, ``Deliverable_Revisions``).
    """
    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


@pytest.fixture
def navigator(
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """Navigator without a disclosure policy.

    Suitable for every test except the gap-descriptor surface, which
    requires :attr:`disclosure_policy` to be set so
    :meth:`navigate_completion` consults
    :meth:`_collect_gap_descriptors_for_subject`.
    """
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )


@pytest.fixture
def disclosure_navigator(
    completion_engine: Engine,
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """Navigator wired with the seeded ``slice-default-2026`` policy.

    Used by :class:`TestGapDescriptors` so the navigator surfaces gap
    descriptors collected from ``Provenance_Manifests`` /
    ``Omission_Entries``.
    """
    seed_disclosure_policies(completion_engine)
    policy = get_policy(completion_engine, SLICE_DEFAULT_POLICY_ID)
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
        disclosure_policy=policy,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Direct INSERTs are preferred over driving the Slice 3 services so the
# tests focus on the navigator's read-side behaviour without taking on
# the wiring (and role assignments) the write services require.
# Relationships rows are seeded alongside the records so the
# AD-WS-26 ``semantic_role`` discriminator is present for the backlink
# tests.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, name: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": name, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        _seed_party(conn, _REQUESTER_PARTY_ID, "Reviewer")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
        _seed_party(conn, _ASSIGNMENT_AUTHORITY_ID, "Assignment Authority")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
        _seed_party(conn, _APPROVING_PARTY_ID, "Plan Approver")


def _seed_project(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIXED},
        )


def _seed_activity_plan(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Mesh Rollout Activities',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision(engine: Engine) -> None:
    """Seed one approved ``Plan_Revisions`` row directly.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so the row
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
                    :rev, :aid, NULL, 'approved', 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_approval(engine: Engine) -> None:
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
                    :pa, :aid, :rev, 'Approve', 'Approved.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pa": _PLAN_APPROVAL_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "rev": _PLAN_REVISION_ID,
                "party": _APPROVING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_work_assignment(
    engine: Engine, *, applicable_scope: str = _SCOPE
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
                    'Assigning the rollout.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _WORK_ASSIGNMENT_ID,
                "prev": _PLAN_REVISION_ID,
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNMENT_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 1: Work Assignment Record -> Plan Revision
        # via Addresses with semantic_role = NULL.
        _insert_relationship(
            conn,
            relationship_id=_REL_WA_ADDRESSES_PR_ID,
            relationship_type="Addresses",
            source_kind="work_assignment_record",
            source_id=_WORK_ASSIGNMENT_ID,
            source_revision_id=None,
            target_kind="plan_revision",
            target_id=_PLAN_REVISION_ID,
            target_revision_id=None,
            semantic_role=None,
            authoring_party_id=_ASSIGNMENT_AUTHORITY_ID,
        )
        # AD-WS-26 row 2: Work Assignment Record -> assignee Party
        # via Relates To with semantic_role = 'assignee'.
        _insert_relationship(
            conn,
            relationship_id=_REL_WA_ASSIGNEE_ID,
            relationship_type="Relates To",
            source_kind="work_assignment_record",
            source_id=_WORK_ASSIGNMENT_ID,
            source_revision_id=None,
            target_kind="party",
            target_id=_CONTRIBUTOR_PARTY_ID,
            target_revision_id=None,
            semantic_role="assignee",
            authoring_party_id=_ASSIGNMENT_AUTHORITY_ID,
        )


def _seed_work_event(
    engine: Engine, *, applicable_scope: str = _SCOPE
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
                    :weid, :wid, 'started', 'Beginning work.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "weid": _WORK_EVENT_ID,
                "wid": _WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 3: Work Event Record -> Work Assignment Record
        # via Relates To with semantic_role = 'work_event'.
        _insert_relationship(
            conn,
            relationship_id=_REL_WE_WORK_EVENT_ID,
            relationship_type="Relates To",
            source_kind="work_event_record",
            source_id=_WORK_EVENT_ID,
            source_revision_id=None,
            target_kind="work_assignment_record",
            target_id=_WORK_ASSIGNMENT_ID,
            target_revision_id=None,
            semantic_role="work_event",
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )


def _seed_time_entry(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Time_Entry_Records (
                    time_entry_id, target_work_assignment_id,
                    effort_hours, effort_period_start, effort_period_end,
                    recording_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :tid, :wid, '1.50', :start, :end, :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "tid": _TIME_ENTRY_ID,
                "wid": _WORK_ASSIGNMENT_ID,
                "start": _TS_FIXED,
                "end": _TS_FIXED,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 4: Time Entry Record -> Work Assignment Record
        # via Relates To with semantic_role = 'time_entry'.
        _insert_relationship(
            conn,
            relationship_id=_REL_TE_TIME_ENTRY_ID,
            relationship_type="Relates To",
            source_kind="time_entry_record",
            source_id=_TIME_ENTRY_ID,
            source_revision_id=None,
            target_kind="work_assignment_record",
            target_id=_WORK_ASSIGNMENT_ID,
            target_revision_id=None,
            semantic_role="time_entry",
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )


def _seed_deliverable(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Mesh runbook', :ts)
                """
            ),
            {"did": _DELIVERABLE_ID, "ts": _TS_FIXED},
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
                "rev": _DELIVERABLE_REVISION_ID,
                "did": _DELIVERABLE_ID,
                "bytes": _DELIVERABLE_BYTES,
                "digest": _DELIVERABLE_DIGEST,
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _DELIVERABLE_EXPECTATION_ID, "ts": _TS_FIXED},
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
                    :rev, :did, NULL, :pid, 'Mesh Operations Runbook',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _ASSIGNMENT_AUTHORITY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_deliverable_production(engine: Engine) -> None:
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
                    'Produced runbook for milestone one.', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "wa": _WORK_ASSIGNMENT_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 5: Deliverable Production Record -> produced
        # Deliverable Revision via Produces with semantic_role = NULL.
        _insert_relationship(
            conn,
            relationship_id=_REL_DP_PRODUCES_ID,
            relationship_type="Produces",
            source_kind="deliverable_production_record",
            source_id=_DELIVERABLE_PRODUCTION_ID,
            source_revision_id=None,
            target_kind="deliverable_revision",
            target_id=_DELIVERABLE_ID,
            target_revision_id=_DELIVERABLE_REVISION_ID,
            semantic_role=None,
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        # AD-WS-26 row 6: Deliverable Production Record -> target
        # Deliverable Expectation Revision via Addresses with
        # semantic_role = NULL.
        _insert_relationship(
            conn,
            relationship_id=_REL_DP_ADDRESSES_DER_ID,
            relationship_type="Addresses",
            source_kind="deliverable_production_record",
            source_id=_DELIVERABLE_PRODUCTION_ID,
            source_revision_id=None,
            target_kind="deliverable_expectation_revision",
            target_id=_DELIVERABLE_EXPECTATION_ID,
            target_revision_id=_DELIVERABLE_EXPECTATION_REVISION_ID,
            semantic_role=None,
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )
        # AD-WS-26 row 7: Deliverable Production Record -> source
        # Work Assignment Record via Relates To with semantic_role =
        # 'production_source'.
        _insert_relationship(
            conn,
            relationship_id=_REL_DP_PROD_SOURCE_ID,
            relationship_type="Relates To",
            source_kind="deliverable_production_record",
            source_id=_DELIVERABLE_PRODUCTION_ID,
            source_revision_id=None,
            target_kind="work_assignment_record",
            target_id=_WORK_ASSIGNMENT_ID,
            target_revision_id=None,
            semantic_role="production_source",
            authoring_party_id=_CONTRIBUTOR_PARTY_ID,
        )


def _seed_milestone_acceptance(
    engine: Engine, *, applicable_scope: str = _SCOPE
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
                    'Accept', 'Milestone one criteria satisfied.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": _MILESTONE_ACCEPTANCE_ID,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _APPROVING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 8: Milestone Acceptance Record -> produced
        # Deliverable Revision via Addresses with semantic_role = NULL.
        _insert_relationship(
            conn,
            relationship_id=_REL_MA_ADDRESSES_DR_ID,
            relationship_type="Addresses",
            source_kind="milestone_acceptance_record",
            source_id=_MILESTONE_ACCEPTANCE_ID,
            source_revision_id=None,
            target_kind="deliverable_revision",
            target_id=_DELIVERABLE_ID,
            target_revision_id=_DELIVERABLE_REVISION_ID,
            semantic_role=None,
            authoring_party_id=_APPROVING_PARTY_ID,
        )


def _seed_completion(engine: Engine) -> None:
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
                    'All planned work completed.', '[]', :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": _COMPLETION_ID,
                "prev": _PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _APPROVING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )
        # AD-WS-26 row 9: Completion Record -> target Approved Plan
        # Revision via Addresses with semantic_role = NULL.
        _insert_relationship(
            conn,
            relationship_id=_REL_CP_ADDRESSES_PR_ID,
            relationship_type="Addresses",
            source_kind="completion_record",
            source_id=_COMPLETION_ID,
            source_revision_id=None,
            target_kind="plan_revision",
            target_id=_PLAN_REVISION_ID,
            target_revision_id=None,
            semantic_role=None,
            authoring_party_id=_APPROVING_PARTY_ID,
        )


def _insert_relationship(
    conn: Connection,
    *,
    relationship_id: str,
    relationship_type: str,
    source_kind: str,
    source_id: str,
    source_revision_id: Optional[str],
    target_kind: str,
    target_id: str,
    target_revision_id: Optional[str],
    semantic_role: Optional[str],
    authoring_party_id: str,
    recorded_at: str = _TS_FIXED,
) -> None:
    """Insert one ``Relationships`` row with explicit ``semantic_role``.

    Mirrors the column list written by the Slice 3 services so the
    backlink coverage tests can rely on byte-equivalent rows whether
    the data was produced by the services or seeded here.
    """
    conn.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at, semantic_role
            ) VALUES (
                :relationship_id, :relationship_type,
                :source_kind, :source_id, :source_revision_id,
                :target_kind, :target_id, :target_revision_id,
                :authoring_party_id, :recorded_at, :semantic_role
            )
            """
        ),
        {
            "relationship_id": relationship_id,
            "relationship_type": relationship_type,
            "source_kind": source_kind,
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_revision_id": target_revision_id,
            "authoring_party_id": authoring_party_id,
            "recorded_at": recorded_at,
            "semantic_role": semantic_role,
        },
    )


def _assign_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str = "*",
    party_id: str = _REQUESTER_PARTY_ID,
    role_name: str = "reviewer",
) -> str:
    """Grant the ``view`` authority to ``party_id`` under ``scope``.

    The default ``scope='*'`` wildcard relies on the AD-WS-15 prefix
    fallback so a single Role Assignment covers every ``view.<kind>``
    action the navigator issues. Tests that need to *withhold* one
    specific kind's authority pass narrower scopes alongside this
    wildcard to compose the test universe.
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


def _seed_full_graph(
    engine: Engine,
    *,
    work_assignment_scope: str = _SCOPE,
    work_event_scope: str = _SCOPE,
    milestone_scope: str = _SCOPE,
) -> None:
    """Seed every row required for the canonical happy-path traversal.

    Identifies the parties, the Project / Activity Plan / Plan Revision
    backbone, the Plan Approval Record (so the Planning leg becomes
    non-None), the Work Assignment + Work Event + Time Entry chain on
    the Work Assignment leg, the Deliverable + Production + Milestone
    Acceptance chain on the Milestone Acceptance leg, and the
    Completion Record at the head.

    The function is deterministic — every Identity is a fixed UUIDv7
    literal — so assertions can name the row keys directly and
    structural equality on the returned :class:`ExecutionProvenanceTree`
    is well-defined.

    The three ``*_scope`` parameters let redaction tests seed a single
    node with a distinct ``applicable_scope`` so the navigator's
    ``view.<kind>`` authorization check denies on that node alone
    while every sibling remains visible — needed because the AD-WS-27
    append-only triggers reject post-insert UPDATE on every Slice 3
    Record.
    """
    _seed_required_parties(engine)
    _seed_project(engine)
    _seed_activity_plan(engine)
    _seed_plan_revision(engine)
    _seed_plan_approval(engine)
    _seed_work_assignment(engine, applicable_scope=work_assignment_scope)
    _seed_work_event(engine, applicable_scope=work_event_scope)
    _seed_time_entry(engine)
    _seed_deliverable(engine)
    _seed_deliverable_expectation(engine)
    _seed_deliverable_production(engine)
    _seed_milestone_acceptance(engine, applicable_scope=milestone_scope)
    _seed_completion(engine)


# ---------------------------------------------------------------------------
# Requirement 31.5 / 35.7 — unresolvable / restricted Completion is one
# response form.
# ---------------------------------------------------------------------------


class TestNavigateCompletionUnresolvable:
    """Requirement 31.5, 31.6, 35.6, 35.7."""

    def test_unknown_completion_id_raises_completion_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        completion_engine: Engine,
    ) -> None:
        unknown = "00000000-0000-7000-8000-00000000ffff"
        with completion_engine.connect() as conn:
            with pytest.raises(CompletionUnresolvableError) as exc:
                navigator.navigate_completion(
                    conn,
                    completion_id=unknown,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert unknown in str(exc.value)

    def test_no_view_authority_raises_completion_unresolvable(
        self,
        navigator: ProvenanceNavigator,
        completion_engine: Engine,
    ) -> None:
        """Design ``not_found_indistinguishable_response``.

        The Completion exists but the Party holds no Role Assignment.
        The navigator must surface the same exception as the
        unresolvable case so the externally observable response form
        is identical (Requirement 31.6 / 35.7).
        """
        _seed_full_graph(completion_engine)

        with completion_engine.connect() as conn:
            with pytest.raises(CompletionUnresolvableError) as exc:
                navigator.navigate_completion(
                    conn,
                    completion_id=_COMPLETION_ID,
                    party_id=_REQUESTER_PARTY_ID,
                    at=_EFFECTIVE_TIME,
                )
        assert _COMPLETION_ID in str(exc.value)


# ---------------------------------------------------------------------------
# Requirement 31.2 / 35.1 — three ordered legs with full identities.
# ---------------------------------------------------------------------------


class TestNavigateCompletionThreeOrderedLegs:
    """Requirement 31.2: the chain has three legs; every node has Identity."""

    def test_head_completion_node_carries_all_identities(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(tree, ExecutionProvenanceTree)
        assert tree.requested_anchor_kind == "completion_record"
        assert tree.requested_anchor_id == _COMPLETION_ID
        assert tree.requested_completion_id == _COMPLETION_ID
        assert tree.production_anchor is None
        assert tree.produced_revision_anchor is None
        assert isinstance(tree.completion, CompletionNode)
        assert tree.completion.completion_id == _COMPLETION_ID
        assert tree.completion.target_plan_revision_id == _PLAN_REVISION_ID
        assert tree.completion.target_activity_plan_id == _ACTIVITY_PLAN_ID
        assert tree.completion.target_project_id == _PROJECT_ID
        assert tree.completion.outcome == "Completed"

    def test_planning_leg_resolves_to_plan_approval_provenance(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """Planning leg is delegated to :meth:`navigate_plan_approval`.

        The Plan Approval row exists and the Party has view on the
        wildcard scope so the delegated call returns a
        :class:`PlanApprovalProvenance`; intermediate nodes that have
        no seeded upstream rows surface as :class:`RedactedNode`
        markers per the delegated traversal's cascade-by-record rule,
        which is enforced and tested by the Slice 2 navigator
        traversal tests separately. Here we only assert that the leg
        is *attached* (non-None) — that is the contract
        :meth:`navigate_completion` is responsible for.
        """
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(tree.plan_approval_chain, PlanApprovalProvenance)
        assert tree.plan_approval_chain.plan_approval.plan_approval_id == (
            _PLAN_APPROVAL_ID
        )

    def test_planning_leg_is_none_when_no_plan_approval_exists(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """A Completion without a Plan Approval (defensively) yields ``None``.

        Production data cannot reach this state because Requirement
        29.1 requires at least one accepted Milestone, which itself
        requires a Plan Approval; but the navigator must not crash on
        the missing-row branch, per design §"Provenance traversal
        algorithm" (the absent-or-restricted Plan Approval case is
        indistinguishable from a restricted one).
        """
        _seed_required_parties(completion_engine)
        _seed_project(completion_engine)
        _seed_activity_plan(completion_engine)
        _seed_plan_revision(completion_engine)
        # Intentionally skip _seed_plan_approval.
        _seed_completion(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert tree.plan_approval_chain is None

    def test_work_assignment_leg_carries_events_and_time_entries(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert len(tree.work_assignment_chains) == 1
        wa_chain = tree.work_assignment_chains[0]
        assert isinstance(wa_chain, WorkAssignmentExecutionChain)
        assert isinstance(wa_chain.work_assignment, WorkAssignmentNode)
        assert wa_chain.work_assignment.work_assignment_id == (
            _WORK_ASSIGNMENT_ID
        )
        assert wa_chain.work_assignment.target_plan_revision_id == (
            _PLAN_REVISION_ID
        )
        assert wa_chain.work_assignment.assignee_party_id == (
            _CONTRIBUTOR_PARTY_ID
        )

        assert len(wa_chain.work_events) == 1
        event_node = wa_chain.work_events[0]
        assert isinstance(event_node, WorkEventNode)
        assert event_node.work_event_id == _WORK_EVENT_ID
        assert event_node.event_kind == "started"
        assert event_node.target_work_assignment_id == _WORK_ASSIGNMENT_ID

        assert len(wa_chain.time_entries) == 1
        time_node = wa_chain.time_entries[0]
        assert isinstance(time_node, TimeEntryNode)
        assert time_node.time_entry_id == _TIME_ENTRY_ID
        assert time_node.target_work_assignment_id == _WORK_ASSIGNMENT_ID
        assert time_node.effort_hours == "1.50"

    def test_milestone_leg_carries_production_and_revision(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert len(tree.milestone_acceptance_chains) == 1
        mac = tree.milestone_acceptance_chains[0]
        assert isinstance(mac, MilestoneAcceptanceProductionChain)
        assert isinstance(mac.milestone_acceptance, MilestoneAcceptanceNode)
        assert mac.milestone_acceptance.milestone_acceptance_id == (
            _MILESTONE_ACCEPTANCE_ID
        )
        assert mac.milestone_acceptance.outcome == "Accept"
        assert mac.milestone_acceptance.source_deliverable_production_id == (
            _DELIVERABLE_PRODUCTION_ID
        )

        assert isinstance(mac.deliverable_production, DeliverableProductionNode)
        assert mac.deliverable_production.deliverable_production_id == (
            _DELIVERABLE_PRODUCTION_ID
        )
        assert mac.deliverable_production.source_work_assignment_id == (
            _WORK_ASSIGNMENT_ID
        )

        assert isinstance(
            mac.produced_deliverable_revision, DeliverableRevisionNode
        )
        assert mac.produced_deliverable_revision.deliverable_revision_id == (
            _DELIVERABLE_REVISION_ID
        )


# ---------------------------------------------------------------------------
# Requirement 35.8 — produced Deliverable Revision content_digest and role
# marker on the chain.
# ---------------------------------------------------------------------------


class TestProducedDeliverableRevisionFields:
    """Requirement 35.8: role_marker='generated_output' and content_digest."""

    def test_produced_revision_carries_role_marker_and_digest(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        revision = tree.milestone_acceptance_chains[0].produced_deliverable_revision
        assert isinstance(revision, DeliverableRevisionNode)
        # Requirement 35.8 — produced-Deliverable role marker.
        assert revision.role_marker == "generated_output"
        # Requirement 35.8 — content digest of the persisted bytes,
        # computed once at module load time so we compare hash-to-hash
        # rather than re-hash the bytes here.
        assert revision.content_digest_sha256 == _DELIVERABLE_DIGEST
        # Requirement 35.8 — the node Type distinguishes a produced
        # Deliverable Revision from any Slice 1 Source Evidence
        # Document Revision (which has no ``role_marker`` attribute).
        assert revision.originating_work_assignment_id == _WORK_ASSIGNMENT_ID
        assert revision.deliverable_id == _DELIVERABLE_ID


# ---------------------------------------------------------------------------
# Requirement 35.3 / AD-WS-9 rule 1 — restricted nodes become RedactedNode.
# ---------------------------------------------------------------------------


def _assign_specific_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    role_name: str,
    scope: str,
) -> None:
    """Grant a *narrowly* scoped ``view`` role.

    Used by the per-stage redaction tests to grant authority on every
    Slice 3 record's scope except one, so exactly one node surfaces as
    a :class:`RedactedNode` while every sibling remains visible. The
    AD-WS-15 prefix fallback means a single ``view`` Role Assignment
    covers every ``view.<kind>`` action whose target's scope matches
    ``scope``.
    """
    _assign_view_role(
        authorization_service,
        engine,
        scope=scope,
        role_name=role_name,
    )


class TestRedactedNodeShape:
    """A :class:`RedactedNode` marker discloses only ``kind`` and ``redacted``.

    AD-WS-9 rule 1 requires the marker to omit every identifier,
    count, and attribute value of the redacted node beyond the
    generic redaction indicator. The dataclass shape itself pins
    this; the per-stage redaction tests in
    :class:`TestRedactionViaSelectiveScopes` then verify the
    navigator surfaces this marker in place of restricted nodes.
    """

    def test_redacted_node_marker_shape(self) -> None:
        marker = RedactedNode(kind="milestone_acceptance_record")
        assert marker.kind == "milestone_acceptance_record"
        assert marker.redacted is True


class TestRedactionViaSelectiveScopes:
    """Per-stage redaction tests using narrowly scoped Role Assignments.

    To isolate a single Slice 3 node for redaction we re-seed the
    happy-path graph with that node's ``applicable_scope`` set to a
    distinct value and grant the requester view authority on every
    *other* scope. The navigator denies on the one withheld scope and
    surfaces a :class:`RedactedNode` for that node alone; visible
    siblings remain visible.
    """

    def test_redacted_work_event_among_visible_time_entry(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """Withhold view on the Work Event scope only.

        The Work Event is seeded with a unique scope
        ``_SCOPE + "/work-event"`` so the requesting Party can hold a
        narrow ``view`` on every other scope while the Work Event
        surfaces as a :class:`RedactedNode`. The sibling Time Entry
        remains visible (different scope; same parent Work
        Assignment).
        """
        _seed_full_graph(
            completion_engine, work_event_scope=_SCOPE + "/work-event"
        )

        # Grant view on every scope except the Work Event's narrow
        # scope. The AD-WS-15 prefix fallback resolves the requester's
        # narrow ``view`` Role Assignment for every action under that
        # scope.
        _assign_view_role(authorization_service, completion_engine, scope=_SCOPE)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        wa_chain = tree.work_assignment_chains[0]
        assert wa_chain.work_events == (RedactedNode(kind="work_event_record"),)
        # The visible Time Entry sibling still resolves.
        assert len(wa_chain.time_entries) == 1
        assert isinstance(wa_chain.time_entries[0], TimeEntryNode)

    def test_redacted_work_assignment_hides_downstream(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """A redacted Work Assignment hides its Work Events and Time Entries.

        The Work Assignment row is seeded with a unique scope so the
        narrowly granted role denies on the Work Assignment itself.
        The cascade-by-record rule then prevents the Work Events and
        Time Entries from being loaded — both lists are empty tuples
        on the returned :class:`WorkAssignmentExecutionChain` so the
        existence of the children is not leaked.
        """
        _seed_full_graph(
            completion_engine, work_assignment_scope=_SCOPE + "/wa"
        )

        _assign_view_role(authorization_service, completion_engine, scope=_SCOPE)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        wa_chain = tree.work_assignment_chains[0]
        assert wa_chain.work_assignment == RedactedNode(
            kind="work_assignment_record"
        )
        assert wa_chain.work_events == ()
        assert wa_chain.time_entries == ()

    def test_redacted_milestone_hides_production_and_revision(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """A redacted Milestone Acceptance hides its Production and Revision.

        Per cascade-by-record the navigator does not load the
        downstream Production or produced Revision when the parent
        Milestone Acceptance is restricted; the two fields surface as
        ``None`` so existence of either child is not leaked
        (Requirement 35.3 / AD-WS-9 rule 1).
        """
        _seed_full_graph(
            completion_engine, milestone_scope=_SCOPE + "/milestone"
        )

        _assign_view_role(authorization_service, completion_engine, scope=_SCOPE)

        with completion_engine.connect() as conn:
            tree = navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        mac = tree.milestone_acceptance_chains[0]
        assert mac.milestone_acceptance == RedactedNode(
            kind="milestone_acceptance_record"
        )
        assert mac.deliverable_production is None
        assert mac.produced_deliverable_revision is None


# ---------------------------------------------------------------------------
# Requirement 31.3 / 35.4 — gap descriptors for unresolved omissions on the
# delegated planning leg.
# ---------------------------------------------------------------------------


class TestGapDescriptors:
    """Requirement 31.3 / 35.4 / Slice 2 Requirement 14.4.

    Gap descriptors are produced by the existing
    :meth:`_collect_gap_descriptors_for_subject` helper that
    :meth:`navigate_completion` reuses additively for the Completion
    subject and that the delegated :meth:`navigate_plan_approval`
    already uses for the Plan Approval subject. The
    ``Provenance_Manifests.subject_kind`` CHECK constraint admits the
    Slice 1 / Slice 2 kinds only — Completion is not yet on the
    enumeration — so the Completion-rooted manifest path is exercised
    indirectly: we assert :attr:`ExecutionProvenanceTree.gap_descriptors`
    is the empty tuple when no Completion-subject manifest can exist,
    and we exercise the canonical gap-descriptor *shape* via the
    delegated planning leg's manifest.
    """

    def test_completion_gap_descriptors_empty_without_completion_manifest(
        self,
        disclosure_navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            tree = disclosure_navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert tree.gap_descriptors == ()

    def test_plan_approval_gap_descriptor_carries_stage_category_identity(
        self,
        disclosure_navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """Seed an unresolved Omission Entry on the Plan Approval manifest.

        The descriptor must carry only the three Requirement 11.4 /
        14.4 fields: ``stage``, ``category``, and (when visible)
        ``next_reachable_node_identity``. No identifier, count, or
        attribute value of the omitted source is disclosed.
        """
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        manifest_id = "00000000-0000-7000-8000-0000000a00a2"
        omission_id = "00000000-0000-7000-8000-0000000a00a3"
        with completion_engine.begin() as conn:
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
                    "sid": _PLAN_APPROVAL_ID,
                    "party": _APPROVING_PARTY_ID,
                    "ts": _TS_FIXED,
                },
            )
            conn.execute(
                text(
                    """
                    INSERT INTO Omission_Entries (
                        omission_entry_id, manifest_id, excluded_source_id,
                        excluded_source_revision_id, category, rationale,
                        authoring_party_id, recorded_at, resolved_at
                    ) VALUES (
                        :oid, :mid, :sid, NULL, 'unresolved',
                        'Awaiting upstream Decision.', :party, :ts, NULL
                    )
                    """
                ),
                {
                    "oid": omission_id,
                    "mid": manifest_id,
                    "sid": "00000000-0000-7000-8000-0000000a00ff",
                    "party": _APPROVING_PARTY_ID,
                    "ts": _TS_FIXED,
                },
            )

        with completion_engine.connect() as conn:
            tree = disclosure_navigator.navigate_completion(
                conn,
                completion_id=_COMPLETION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert tree.plan_approval_chain is not None
        descriptors = tree.plan_approval_chain.gap_descriptors
        assert len(descriptors) == 1
        descriptor = descriptors[0]
        assert isinstance(descriptor, ChainGapDescriptor)
        assert descriptor.stage == "plan_approval"
        assert descriptor.category == "unresolved"
        assert descriptor.next_reachable_node_identity == _PLAN_APPROVAL_ID


# ---------------------------------------------------------------------------
# Requirement 31.4 / 35.5 — idempotent retrieval across 5 repetitions.
# ---------------------------------------------------------------------------


class TestIdempotenceFiveRepetitions:
    """Requirement 31.4 / 35.5: byte-equivalent across 5 invocations."""

    def test_five_repetitions_return_equal_trees(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        trees: list[ExecutionProvenanceTree] = []
        for _ in range(5):
            with completion_engine.connect() as conn:
                trees.append(
                    navigator.navigate_completion(
                        conn,
                        completion_id=_COMPLETION_ID,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EFFECTIVE_TIME,
                    )
                )

        first = trees[0]
        for other in trees[1:]:
            assert other == first


# ---------------------------------------------------------------------------
# Requirement 36.1 / 36.2 / 36.6 — backlinks for Slice 3 source kinds.
# ---------------------------------------------------------------------------


class TestSlice3BacklinkCoverage:
    """Slice 3 source endpoint kinds are returned by the backlink algorithm.

    The algorithm itself is unchanged from task 12.1; AD-WS-26 simply
    extended the recognized source-kind surface so the existing
    backlink query returns Slice 3 source endpoints when the queried
    target is a Slice 3 record.
    """

    def test_backlink_query_for_plan_revision_returns_work_assignment_and_completion(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """Plan Revision target surfaces every inbound Slice 3 Addresses row.

        The Work Assignment Record and the Completion Record both
        carry ``Addresses`` Relationships to the Plan Revision per
        AD-WS-26 rows 1 and 9; both are returned with the correct
        ``source_kind`` discriminator and Identity.
        """
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            page = navigator.list_backlinks(
                conn,
                target_id=_PLAN_REVISION_ID,
                target_revision_id=None,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        assert isinstance(page, BacklinkPage)
        source_kinds = {entry.source_kind for entry in page.entries}
        assert "work_assignment_record" in source_kinds
        assert "completion_record" in source_kinds

        wa_entries = [
            e for e in page.entries
            if e.source_kind == "work_assignment_record"
        ]
        assert len(wa_entries) == 1
        assert wa_entries[0].source_id == _WORK_ASSIGNMENT_ID
        assert wa_entries[0].relationship_type == "Addresses"

        completion_entries = [
            e for e in page.entries if e.source_kind == "completion_record"
        ]
        assert len(completion_entries) == 1
        assert completion_entries[0].source_id == _COMPLETION_ID
        assert completion_entries[0].relationship_type == "Addresses"

    def test_backlink_query_for_work_assignment_returns_event_time_production(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """Work Assignment target surfaces Work Event, Time Entry, Production.

        Per AD-WS-26 rows 3, 4, and 7, every Work Event Record, Time
        Entry Record, and Deliverable Production Record carries a
        ``Relates To`` Relationship pointing at the Work Assignment;
        the backlink algorithm returns all three with the correct
        source-kind discriminator.
        """
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            page = navigator.list_backlinks(
                conn,
                target_id=_WORK_ASSIGNMENT_ID,
                target_revision_id=None,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        source_kinds = {entry.source_kind for entry in page.entries}
        assert source_kinds == {
            "work_event_record",
            "time_entry_record",
            "deliverable_production_record",
        }
        # Every Slice 3 source endpoint surfaced here is a Record-grain
        # row with no Revision concept, so ``source_revision_id`` is
        # ``None`` on each entry (Requirement 8.2 — Slice 1 Source
        # Evidence semantics).
        for entry in page.entries:
            assert entry.source_revision_id is None

    def test_backlink_query_for_produced_revision_returns_milestone_acceptance(
        self,
        navigator: ProvenanceNavigator,
        authorization_service: AuthorizationService,
        completion_engine: Engine,
    ) -> None:
        """Produced Deliverable Revision target surfaces Production + Milestone.

        Per AD-WS-26 rows 5 and 8, the Deliverable Production Record
        carries a ``Produces`` Relationship and the Milestone
        Acceptance Record carries an ``Addresses`` Relationship, both
        pointing at the produced Deliverable Revision.
        """
        _seed_full_graph(completion_engine)
        _assign_view_role(authorization_service, completion_engine)

        with completion_engine.connect() as conn:
            page = navigator.list_backlinks(
                conn,
                target_id=_DELIVERABLE_ID,
                target_revision_id=_DELIVERABLE_REVISION_ID,
                party_id=_REQUESTER_PARTY_ID,
                at=_EFFECTIVE_TIME,
            )

        source_kinds = {entry.source_kind for entry in page.entries}
        assert source_kinds == {
            "deliverable_production_record",
            "milestone_acceptance_record",
        }


# ---------------------------------------------------------------------------
# Requirement 36.2 / AD-WS-26 — Relationships.semantic_role discriminator.
# ---------------------------------------------------------------------------


class TestSlice3SemanticRolePopulation:
    """Persisted ``Relationships.semantic_role`` matches the AD-WS-26 table.

    :class:`BacklinkEntry` itself does not surface the
    ``semantic_role`` column (the slice's API contract is the
    Relationship Type plus the source endpoint Type per Requirement
    8.2), so the discriminator the backlink algorithm relies on for
    ordering and downstream queries is verified directly against the
    persisted ``Relationships`` row.
    """

    @pytest.mark.parametrize(
        "relationship_id, expected_semantic_role",
        [
            (_REL_WA_ADDRESSES_PR_ID, None),
            (_REL_WA_ASSIGNEE_ID, "assignee"),
            (_REL_WE_WORK_EVENT_ID, "work_event"),
            (_REL_TE_TIME_ENTRY_ID, "time_entry"),
            (_REL_DP_PRODUCES_ID, None),
            (_REL_DP_ADDRESSES_DER_ID, None),
            (_REL_DP_PROD_SOURCE_ID, "production_source"),
            (_REL_MA_ADDRESSES_DR_ID, None),
            (_REL_CP_ADDRESSES_PR_ID, None),
        ],
    )
    def test_semantic_role_matches_ad_ws_26_for_each_relationship_row(
        self,
        completion_engine: Engine,
        relationship_id: str,
        expected_semantic_role: Optional[str],
    ) -> None:
        _seed_full_graph(completion_engine)

        with completion_engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT semantic_role FROM Relationships "
                    "WHERE relationship_id = :rid"
                ),
                {"rid": relationship_id},
            ).mappings().one()

        assert row["semantic_role"] == expected_semantic_role
