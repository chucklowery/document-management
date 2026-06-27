# Feature: third-walking-slice, Property 41: Slice 1 and Slice 2 non-modification under Slice 3 actions
"""Property 41 — Slice 1 and Slice 2 non-modification under Slice 3 actions (task 16.11).

**Property 41: Slice 1 and Slice 2 non-modification under Slice 3 actions**

*For all* test sessions exercising the Execution_Service and the
Deliverable_Repository, at every observation point after any sequence
of Slice 3 actions, every row created by Slice 1 or Slice 2 —
``Audit_Records``, ``Identifier_Registry`` (apart from the additive
``resource_kind`` column populated for Slice 3 rows and the one-shot
back-fill of ``'source_document'`` for existing NULL Slice 1 rows),
``Interim_ADR_Records``, ``Disclosure_Policies``,
``Disclosure_Policy_Coverage`` (apart from the additive coverage rows
seeded by AD-WS-25), ``Role_Assignments`` (apart from the additive
eight-value enumeration permitted by AD-WS-24), ``Document_Revisions``,
``Region_Occurrences``, ``Finding_Revisions``,
``Recommendation_Revisions``, ``Decisions``, ``Relationships`` (apart
from new rows inserted by Slice 3 actions and the additive
``semantic_role`` markers from AD-WS-26), ``Trail_Revisions``,
``Trail_Steps``, ``Provenance_Manifests``, ``Objective_Revisions``,
``Intended_Outcome_Revisions``, ``Project_Revisions``,
``Deliverable_Expectation_Revisions``, ``Activity_Plans``,
``Plan_Revisions``, ``Plan_Review_Revisions``, and
``Plan_Approval_Records`` — is byte-equivalent to its state before the
Slice 3 actions began.

**Validates: Requirements 22.4, 22.6, 22.7, 22.8, 28.8, 29.7, 33.1,
40.1, 40.2, 40.3, 40.4, 41.11, 41.12**

Strategy
========

Each Hypothesis case:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing
   Strategy" — per-case database isolation) carrying the Slice 1
   schema (:func:`walking_slice.persistence.create_schema`), the
   Slice 2 Planning schema
   (:func:`walking_slice.planning._persistence.create_planning_schema`),
   the Slice 3 Execution schema
   (:func:`walking_slice.execution._persistence.create_execution_schema`),
   and the Slice 3 Deliverable_Repository schema
   (:func:`walking_slice.deliverables._persistence.create_deliverable_schema`).

2. Seeds one representative row into every Slice 1 table named by
   Requirement 40.4 / Property 41 via direct ``INSERT`` (matching the
   convention in Property 21 and Property 40 — Slice 1 service paths
   would multiply the per-case work and slow shrinking without
   changing the invariant under test).

3. Seeds one representative row into every Slice 2 table named by
   Requirement 40.4 via direct ``INSERT`` (Objective_Revisions,
   Intended_Outcome_Revisions, Project_Revisions,
   Deliverable_Expectation_Revisions, Activity_Plans, Plan_Revisions
   with ``lifecycle_state='approved'``, Plan_Review_Revisions,
   Plan_Approval_Records). The Plan Revision is inserted directly
   with ``lifecycle_state='approved'`` (the AD-WS-19 trigger only
   fires on UPDATE).

4. Seeds the additive ``Disclosure_Policy_Coverage`` rows for both
   Slice 2 (via :func:`seed_planning_coverage`) and Slice 3 (via
   :func:`seed_execution_coverage`) before the snapshot is taken.
   The Slice 2 coverage rows are part of the "before any Slice 3
   action" baseline that Property 41 protects byte-for-byte; the
   Slice 3 coverage rows are the additive ``Disclosure_Policy_Coverage``
   surface the property explicitly excludes ("apart from the
   additive coverage rows seeded by AD-WS-25").

5. Grants the actor Party the four new Slice 3 authority values
   (``assign``, ``contribute``, ``accept_milestone``, ``complete``)
   plus the two Slice 1 / Slice 2 view-and-modify authorities via
   :meth:`AuthorizationService.assign_role`. The Role Assignment
   row created here is itself a Slice 1 row (the
   ``Role_Assignments`` table is owned by Slice 1); Property 41
   protects this row byte-for-byte across the Slice 3 pipeline,
   verifying that the additive eight-value enumeration permitted by
   AD-WS-24 does not mutate existing Role_Assignments rows.

6. Snapshots every Slice 1 + Slice 2 protected table at the captured
   primary-key set (Property 41's "before the Slice 3 actions began"
   ground truth).

7. Runs the full Slice 3 happy-path pipeline through the real
   Execution_Service and Deliverable_Repository service classes,
   with Hypothesis-drawn textual inputs varying per case:

       (a) Work Assignment Record (Assignment Authority writes
           against the approved Plan Revision)
       (b) Work Event Record (``started``)
       (c) Time Entry Record
       (d) Produced Deliverable Resource + first Revision
       (e) Deliverable Production Record (with the three AD-WS-26
           Relationships)
       (f) Milestone Acceptance Record (outcome=``Accept`` so the
           Completion existence check returns ≥1)
       (g) Completion Record

   After each Slice 3 operation, the test re-reads the same captured
   PKs from each Slice 1 + Slice 2 table and asserts every column is
   byte-equivalent to the pre-Slice-3 snapshot. This realises "at
   every observation point" from the property statement: any mutation
   by any Slice 3 action would surface on the very next snapshot
   diff.

8. Performs one final whole-snapshot byte-equivalence check after
   the complete pipeline so a regression that only manifests at
   end-of-pipeline (e.g., a deferred UPDATE inside the last
   transaction) still surfaces.

Tables that *do* receive new Slice 3 rows (``Audit_Records``,
``Identifier_Registry``, ``Relationships``) are still protected on
their captured Slice-1 / Slice-2 PKs — the snapshot helper re-reads
by the captured PK set, which intentionally excludes the new Slice 3
rows. ``Disclosure_Policy_Coverage`` receives additive coverage rows
for the eight Slice 3 node kinds (AD-WS-25); the snapshot helper
captures only the Slice 2 coverage PKs, so those additive rows are
excluded by construction (the property explicitly excludes them).
``Role_Assignments`` may have its enumeration JSON array extended to
include the four Slice 3 authority values, but the *pre-existing*
row carrying the eight-authority set must remain byte-equivalent —
the property only forbids mutation of pre-existing rows.

Notes
-----

- ``@settings(max_examples=100, deadline=2000)`` per Requirement
  41.15 / AD-WS-13.
- ``HealthCheck.too_slow`` and ``HealthCheck.data_too_large`` are
  suppressed because the per-case setup builds a complete Slice 1 +
  Slice 2 seed graph and runs the seven-step Slice 3 pipeline.
"""

from __future__ import annotations

import json
import tempfile
import uuid as uuid_lib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional

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
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.disclosure import seed as disclosure_seed
from walking_slice.execution._disclosure import seed_execution_coverage
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceService,
)
from walking_slice.execution.time_entries import TimeEntryService
from walking_slice.execution.work_assignments import WorkAssignmentService
from walking_slice.execution.work_events import WorkEventService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._disclosure import seed_planning_coverage
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants for the Slice 1 + Slice 2 seed and the actor Party.
#
# UUIDv7-shaped strings keep the seed compatible with the
# ``Identifier_Registry`` row format and the FK targets each row
# needs. A single actor Party covers every consequential write. The
# Slice 1 + Slice 2 seed timestamp is deliberately offset from the
# per-case clock so any row whose ``recorded_at`` accidentally
# changes to the Slice 3 clock value would surface as a snapshot
# diff.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

# Slice 1 + Slice 2 seed timestamp is offset from the per-case clock
# so any row whose ``recorded_at`` accidentally changes to the
# Slice 3 clock value surfaces as a snapshot diff.
_SLICE12_SEED_TS: Final[str] = "2025-12-15T10:30:00.000Z"

# Parties.
_ACTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_ASSIGNMENT_AUTHORITY_PARTY_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000a3"
)
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a4"
_APPROVING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a5"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a6"

# Slice 1 identifiers.
_DOCUMENT_RESOURCE_ID: Final[str] = "00000000-0000-7000-8000-0000000000b1"
_DOCUMENT_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b2"
_REGION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b3"
_FINDING_ID: Final[str] = "00000000-0000-7000-8000-0000000000b4"
_FINDING_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b5"
_RECOMMENDATION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b6"
_RECOMMENDATION_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b7"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000b8"
_RELATIONSHIP_ID: Final[str] = "00000000-0000-7000-8000-0000000000b9"
_TRAIL_ID: Final[str] = "00000000-0000-7000-8000-0000000000ba"
_TRAIL_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000bb"
_MANIFEST_ID: Final[str] = "00000000-0000-7000-8000-0000000000bc"
_ROLE_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000000000bd"
_AUDIT_RECORD_ID: Final[str] = "00000000-0000-7000-8000-0000000000be"
_INTERIM_ADR_RECORD_ID: Final[str] = "00000000-0000-7000-8000-0000000000bf"
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000c0"
)
_TRAIL_STEP_IDS: Final[tuple[str, ...]] = (
    "00000000-0000-7000-8000-0000000000c1",
    "00000000-0000-7000-8000-0000000000c2",
    "00000000-0000-7000-8000-0000000000c3",
    "00000000-0000-7000-8000-0000000000c4",
    "00000000-0000-7000-8000-0000000000c5",
)

# Slice 1 ``Identifier_Registry`` rows seeded by this property test.
# ``resource_kind`` is left NULL on every pre-existing Slice 1 row
# (matches the AD-WS-19 NULL default; Slice 3 may back-fill these to
# ``'source_document'`` per the property's documented exception, but
# the test does not exercise that back-fill — only the additive
# Slice 3 writes — so the seeded NULL stays NULL).
_SLICE1_IDENTIFIER_ROWS: Final[tuple[tuple[str, str], ...]] = (
    (_DOCUMENT_RESOURCE_ID, "resource"),
    (_DOCUMENT_REVISION_ID, "revision"),
    (_REGION_ID, "region"),
    (_FINDING_ID, "resource"),
    (_FINDING_REVISION_ID, "revision"),
    (_RECOMMENDATION_ID, "resource"),
    (_RECOMMENDATION_REVISION_ID, "revision"),
    (_DECISION_ID, "immutable_record"),
    (_RELATIONSHIP_ID, "relationship"),
    (_TRAIL_ID, "trail"),
    (_TRAIL_REVISION_ID, "trail_revision"),
    (_MANIFEST_ID, "manifest"),
)

# Slice 2 identifiers.
_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_OBJECTIVE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000d2"
_INTENDED_OUTCOME_ID: Final[str] = "00000000-0000-7000-8000-0000000000d3"
_INTENDED_OUTCOME_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d4"
)
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000d5"
_PROJECT_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000d6"
_DELIVERABLE_EXPECTATION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d7"
)
_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d8"
)
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000d9"
_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000da"
_PLAN_REVIEW_ID: Final[str] = "00000000-0000-7000-8000-0000000000db"
_PLAN_REVIEW_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000dc"
_PLAN_APPROVAL_ID: Final[str] = "00000000-0000-7000-8000-0000000000dd"

# Slice 2 ``Identifier_Registry`` rows. Each carries ``kind``
# matching the AD-WS-19 Slice 2 kinds and ``resource_kind`` set to
# the matching Slice 2 ``resource_kind`` value. Slice 3 actions must
# not mutate these rows.
_SLICE2_IDENTIFIER_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    (_OBJECTIVE_ID, "resource", "objective"),
    (_OBJECTIVE_REVISION_ID, "revision", "objective_revision"),
    (_INTENDED_OUTCOME_ID, "resource", "intended_outcome"),
    (
        _INTENDED_OUTCOME_REVISION_ID,
        "revision",
        "intended_outcome_revision",
    ),
    (_PROJECT_ID, "resource", "project"),
    (_PROJECT_REVISION_ID, "revision", "project_revision"),
    (_DELIVERABLE_EXPECTATION_ID, "resource", "deliverable_expectation"),
    (
        _DELIVERABLE_EXPECTATION_REVISION_ID,
        "revision",
        "deliverable_expectation_revision",
    ),
    (_ACTIVITY_PLAN_ID, "resource", "activity_plan"),
    (_PLAN_REVISION_ID, "revision", "plan_revision"),
    (_PLAN_REVIEW_ID, "resource", "plan_review"),
    (_PLAN_REVIEW_REVISION_ID, "revision", "plan_review_revision"),
    (_PLAN_APPROVAL_ID, "immutable_record", "plan_approval"),
)

# The Provenance Manifest written by the Plan Approval (AD-WS-21) is
# also seeded directly so the snapshot has one Slice 2 manifest row
# to protect alongside the Slice 1 manifest.
_PLAN_APPROVAL_MANIFEST_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000de"
)

_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-41/scope"


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 snapshot specifications.
#
# For each protected table named by Property 41, name the columns to
# SELECT and the primary-key columns the snapshot keys by. The additive
# Slice 2 column ``Relationships.semantic_role`` and the additive
# ``Identifier_Registry.resource_kind`` column are included in the
# column list so the post-Slice-3 snapshot diff covers them — for
# pre-existing Slice 1 / Slice 2 rows these columns must remain
# byte-equivalent.
# ---------------------------------------------------------------------------


_PROTECTED_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    # ----- Slice 1 tables -----
    "Audit_Records": {
        "columns": (
            "audit_record_id",
            "append_sequence",
            "actor_party_id",
            "action_type",
            "outcome",
            "target_id",
            "target_revision_id",
            "evaluated_role_assignment_id",
            "authorities_required",
            "authorities_held",
            "reason_code",
            "correlation_id",
            "recorded_at",
        ),
        "pk": ("audit_record_id",),
    },
    "Identifier_Registry": {
        "columns": (
            "identifier",
            "kind",
            "content_digest",
            "issued_at",
            "resource_kind",
        ),
        "pk": ("identifier",),
    },
    "Interim_ADR_Records": {
        "columns": (
            "record_id",
            "motivating_requirement",
            "motivating_criterion",
            "observable_behavior",
            "recorded_at",
            "backlog_adr_id",
            "resolved_by_adr_id",
            "resolved_at",
        ),
        "pk": ("record_id",),
    },
    "Disclosure_Policies": {
        "columns": (
            "policy_id",
            "policy_name",
            "ruleset_json",
            "effective_start",
            "superseded_by",
        ),
        "pk": ("policy_id",),
    },
    "Disclosure_Policy_Coverage": {
        "columns": (
            "policy_id",
            "node_kind",
            "recorded_at",
            "backlog_adr_id",
        ),
        "pk": ("policy_id", "node_kind"),
    },
    "Decisions": {
        "columns": (
            "decision_id",
            "target_recommendation_id",
            "target_recommendation_revision_id",
            "outcome",
            "rationale",
            "deciding_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("decision_id",),
    },
    "Role_Assignments": {
        "columns": (
            "role_assignment_id",
            "party_id",
            "role_name",
            "scope",
            "authorities_granted",
            "effective_start",
            "effective_end",
            "revoked_at",
            "assigning_authority_id",
            "recorded_at",
        ),
        "pk": ("role_assignment_id",),
    },
    "Document_Revisions": {
        "columns": (
            "revision_id",
            "resource_id",
            "parent_revision_id",
            "content_bytes",
            "content_digest_sha256",
            "contributing_party_id",
            "recorded_at",
            "change_description",
        ),
        "pk": ("revision_id",),
    },
    "Region_Occurrences": {
        "columns": (
            "region_id",
            "document_revision_id",
            "start_offset_bytes",
            "end_offset_bytes",
            "span_byte_length",
            "span_content_digest_sha256",
            "recorded_at",
        ),
        "pk": ("region_id", "document_revision_id"),
    },
    "Finding_Revisions": {
        "columns": (
            "finding_revision_id",
            "finding_id",
            "parent_revision_id",
            "statement",
            "is_hypothesis",
            "authoring_party_id",
            "assumptions_json",
            "confidence_note",
            "recorded_at",
        ),
        "pk": ("finding_revision_id",),
    },
    "Recommendation_Revisions": {
        "columns": (
            "recommendation_revision_id",
            "recommendation_id",
            "parent_revision_id",
            "rationale",
            "assumptions_json",
            "confidence",
            "authoring_party_id",
            "recorded_at",
        ),
        "pk": ("recommendation_revision_id",),
    },
    "Relationships": {
        "columns": (
            "relationship_id",
            "relationship_type",
            "source_kind",
            "source_id",
            "source_revision_id",
            "target_kind",
            "target_id",
            "target_revision_id",
            "authoring_party_id",
            "recorded_at",
            "semantic_role",
        ),
        "pk": ("relationship_id",),
    },
    "Trail_Revisions": {
        "columns": (
            "trail_revision_id",
            "trail_id",
            "predecessor_revision_id",
            "purpose",
            "audience_id",
            "ordering_rationale",
            "authoring_party_id",
            "recorded_at",
        ),
        "pk": ("trail_revision_id",),
    },
    "Trail_Steps": {
        "columns": (
            "trail_step_id",
            "trail_revision_id",
            "ordinal",
            "selection_mode",
            "target_kind",
            "target_id",
            "target_revision_id",
            "region_id",
            "annotation",
        ),
        "pk": ("trail_step_id",),
    },
    "Provenance_Manifests": {
        "columns": (
            "manifest_id",
            "subject_kind",
            "subject_id",
            "subject_revision_id",
            "authoring_party_id",
            "recorded_at",
            "included_sources_json",
            "is_complete",
        ),
        "pk": ("manifest_id",),
    },
    # ----- Slice 2 tables -----
    "Objective_Revisions": {
        "columns": (
            "objective_revision_id",
            "objective_id",
            "parent_revision_id",
            "statement",
            "rationale",
            "target_decision_id",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("objective_revision_id",),
    },
    "Intended_Outcome_Revisions": {
        "columns": (
            "intended_outcome_revision_id",
            "intended_outcome_id",
            "parent_revision_id",
            "outcome_kind",
            "target_objective_id",
            "success_condition",
            "observation_window",
            "attribution_assumption",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("intended_outcome_revision_id",),
    },
    "Project_Revisions": {
        "columns": (
            "project_revision_id",
            "project_id",
            "parent_revision_id",
            "name",
            "summary",
            "target_objective_id",
            "planned_start_date",
            "planned_end_date",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("project_revision_id",),
    },
    "Deliverable_Expectation_Revisions": {
        "columns": (
            "deliverable_expectation_revision_id",
            "deliverable_expectation_id",
            "parent_revision_id",
            "target_project_id",
            "name",
            "description",
            "deliverable_kind",
            "acceptance_criteria",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("deliverable_expectation_revision_id",),
    },
    "Activity_Plans": {
        "columns": (
            "activity_plan_id",
            "target_project_id",
            "title",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("activity_plan_id",),
    },
    "Plan_Revisions": {
        "columns": (
            "plan_revision_id",
            "activity_plan_id",
            "predecessor_revision_id",
            "lifecycle_state",
            "planned_scope",
            "deliverable_expectation_refs_json",
            "planning_assumptions_json",
            "ordering_rationale",
            "authoring_party_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("plan_revision_id",),
    },
    "Plan_Review_Revisions": {
        "columns": (
            "plan_review_revision_id",
            "plan_review_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "reviewing_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("plan_review_revision_id",),
    },
    "Plan_Approval_Records": {
        "columns": (
            "plan_approval_id",
            "target_activity_plan_id",
            "target_plan_revision_id",
            "outcome",
            "rationale",
            "approving_party_id",
            "authority_basis_type",
            "authority_basis_id",
            "applicable_scope",
            "recorded_at",
        ),
        "pk": ("plan_approval_id",),
    },
}


# ---------------------------------------------------------------------------
# Per-case engine helper.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a per-case engine carrying every protected schema.

    The Slice 1 schema is required for Audit_Records, Identifier_Registry,
    Disclosure_Policies, Role_Assignments, Document_Revisions,
    Region_Occurrences, Finding_Revisions, Recommendation_Revisions,
    Decisions, Relationships, Trail_Revisions, Trail_Steps,
    Provenance_Manifests, Parties, and Interim_ADR_Records. The Slice 2
    schema is required for Objective_Revisions, Intended_Outcome_Revisions,
    Project_Revisions, Deliverable_Expectation_Revisions, Activity_Plans,
    Plan_Revisions, Plan_Review_Revisions, Plan_Approval_Records, and
    the additive Disclosure_Policy_Coverage table. The Slice 3 schemas
    are required so the Slice 3 services under test can persist their
    rows.
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
    # Seed the slice-default-2026 Disclosure_Policies row so the
    # snapshot has the real Slice 1 policy row to protect.
    disclosure_seed(engine)
    return engine


# ---------------------------------------------------------------------------
# Slice 1 + Slice 2 seed.
#
# Direct INSERTs keep the seed deterministic; Slice 1 / Slice 2
# service paths would multiply the per-case work without changing the
# invariant under test.
# ---------------------------------------------------------------------------


def _seed_slice1(engine: Engine) -> None:
    """Populate one row in every Slice 1 table named by Requirement 40.4.

    Mirrors the Property 21 seed (task 16.6) one-for-one — every row
    is identical in shape so the protected PK set is the same across
    the Slice 1 surface.
    """
    with engine.begin() as conn:
        # ---- Parties ----
        for party_id, display in (
            (_ACTOR_PARTY_ID, "Property 41 Actor"),
            (_CONTRIBUTOR_PARTY_ID, "Property 41 Contributor"),
            (
                _ASSIGNMENT_AUTHORITY_PARTY_ID,
                "Property 41 Assignment Authority",
            ),
            (_ASSIGNING_AUTHORITY_ID, "Property 41 Resource Steward"),
            (_APPROVING_PARTY_ID, "Property 41 Plan Approver"),
            (_AUTHORING_PARTY_ID, "Property 41 Knowledge Author"),
        ):
            conn.execute(
                text(
                    "INSERT INTO Parties (party_id, kind, display_name, created_at) "
                    "VALUES (:pid, 'person', :name, :ts)"
                ),
                {"pid": party_id, "name": display, "ts": _SLICE12_SEED_TS},
            )

        # ---- Identifier_Registry (Slice 1 — resource_kind NULL) ----
        for identifier, kind in _SLICE1_IDENTIFIER_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO Identifier_Registry
                        (identifier, kind, content_digest, issued_at, resource_kind)
                    VALUES
                        (:identifier, :kind, NULL, :issued_at, NULL)
                    """
                ),
                {
                    "identifier": identifier,
                    "kind": kind,
                    "issued_at": _SLICE12_SEED_TS,
                },
            )

        # ---- Source_Documents + Document_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Source_Documents "
                "(resource_id, current_location, external_identifier, "
                " source_system_id, authority, created_at) "
                "VALUES (:rid, 'file:///doc.txt', NULL, NULL, "
                " 'authoritative', :ts)"
            ),
            {"rid": _DOCUMENT_RESOURCE_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Document_Revisions (
                    revision_id, resource_id, parent_revision_id,
                    content_bytes, content_digest_sha256,
                    contributing_party_id, recorded_at, change_description
                ) VALUES (
                    :rev, :res, NULL,
                    :bytes, :digest, :party, :ts, 'initial'
                )
                """
            ),
            {
                "rev": _DOCUMENT_REVISION_ID,
                "res": _DOCUMENT_RESOURCE_ID,
                "bytes": b"hello slice one",
                "digest": "a" * 64,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Content_Regions + Region_Occurrences ----
        conn.execute(
            text(
                "INSERT INTO Content_Regions "
                "(region_id, parent_resource_id, created_at) "
                "VALUES (:rid, :pid, :ts)"
            ),
            {
                "rid": _REGION_ID,
                "pid": _DOCUMENT_RESOURCE_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Region_Occurrences (
                    region_id, document_revision_id,
                    start_offset_bytes, end_offset_bytes, span_byte_length,
                    span_content_digest_sha256, recorded_at
                ) VALUES (
                    :rid, :rev, 0, 5, 5, :digest, :ts
                )
                """
            ),
            {
                "rid": _REGION_ID,
                "rev": _DOCUMENT_REVISION_ID,
                "digest": "b" * 64,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Findings + Finding_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Findings (finding_id, created_at) "
                "VALUES (:fid, :ts)"
            ),
            {"fid": _FINDING_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Finding_Revisions (
                    finding_revision_id, finding_id, parent_revision_id,
                    statement, is_hypothesis, authoring_party_id,
                    assumptions_json, confidence_note, recorded_at
                ) VALUES (
                    :rev, :fid, NULL,
                    'Pre-Slice-3 finding statement.', 0, :party,
                    '[]', NULL, :ts
                )
                """
            ),
            {
                "rev": _FINDING_REVISION_ID,
                "fid": _FINDING_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Recommendations + Recommendation_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Recommendations (recommendation_id, created_at) "
                "VALUES (:rid, :ts)"
            ),
            {"rid": _RECOMMENDATION_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Recommendation_Revisions (
                    recommendation_revision_id, recommendation_id,
                    parent_revision_id, rationale, assumptions_json,
                    confidence, authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :rid, NULL,
                    'Pre-Slice-3 recommendation rationale.', '[]',
                    'Medium', :party, :ts
                )
                """
            ),
            {
                "rev": _RECOMMENDATION_REVISION_ID,
                "rid": _RECOMMENDATION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Decisions (Accept outcome, AD-WS-21 compatible) ----
        conn.execute(
            text(
                """
                INSERT INTO Decisions (
                    decision_id, target_recommendation_id,
                    target_recommendation_revision_id, outcome, rationale,
                    deciding_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :did, :rid, :rev, 'Accept',
                    'Pre-Slice-3 decision rationale.', :party,
                    'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "did": _DECISION_ID,
                "rid": _RECOMMENDATION_ID,
                "rev": _RECOMMENDATION_REVISION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Relationships (one Slice 1 ``Supports`` edge) ----
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Supports',
                    'finding_revision', :fid, :frev,
                    'region_occurrence', :region, NULL,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": _RELATIONSHIP_ID,
                "fid": _FINDING_ID,
                "frev": _FINDING_REVISION_ID,
                "region": _REGION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Trails + Trail_Revisions + Trail_Steps ----
        conn.execute(
            text(
                "INSERT INTO Trails (trail_id, created_at, current_revision_id) "
                "VALUES (:tid, :ts, :rev)"
            ),
            {
                "tid": _TRAIL_ID,
                "ts": _SLICE12_SEED_TS,
                "rev": _TRAIL_REVISION_ID,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Trail_Revisions (
                    trail_revision_id, trail_id, predecessor_revision_id,
                    purpose, audience_id, ordering_rationale,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :tid, NULL,
                    'Pre-Slice-3 trail purpose.', :aud,
                    NULL, :party, :ts
                )
                """
            ),
            {
                "rev": _TRAIL_REVISION_ID,
                "tid": _TRAIL_ID,
                "aud": _ASSIGNING_AUTHORITY_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )
        _STEP_SPECS: tuple[tuple[int, str, str, Optional[str], Optional[str]], ...] = (
            (1, "document_revision", _DOCUMENT_REVISION_ID, None, None),
            (2, "region_occurrence", _REGION_ID, _DOCUMENT_REVISION_ID, _REGION_ID),
            (3, "finding_revision", _FINDING_REVISION_ID, None, None),
            (4, "recommendation_revision", _RECOMMENDATION_REVISION_ID, None, None),
            (5, "decision", _DECISION_ID, None, None),
        )
        for step_id, (ordinal, target_kind, target_id, target_rev, region) in zip(
            _TRAIL_STEP_IDS, _STEP_SPECS
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Trail_Steps (
                        trail_step_id, trail_revision_id, ordinal,
                        selection_mode, target_kind, target_id,
                        target_revision_id, region_id, annotation
                    ) VALUES (
                        :sid, :trev, :ord, 'Pinned',
                        :kind, :tid, :trgrev, :rgn, NULL
                    )
                    """
                ),
                {
                    "sid": step_id,
                    "trev": _TRAIL_REVISION_ID,
                    "ord": ordinal,
                    "kind": target_kind,
                    "tid": target_id,
                    "trgrev": target_rev,
                    "rgn": region,
                },
            )

        # ---- Provenance_Manifests (Slice 1 — Decision manifest) ----
        conn.execute(
            text(
                """
                INSERT INTO Provenance_Manifests (
                    manifest_id, subject_kind, subject_id, subject_revision_id,
                    authoring_party_id, recorded_at, included_sources_json,
                    is_complete
                ) VALUES (
                    :mid, 'decision', :sid, NULL,
                    :party, :ts, '[]', 1
                )
                """
            ),
            {
                "mid": _MANIFEST_ID,
                "sid": _DECISION_ID,
                "party": _AUTHORING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Interim_ADR_Records (one Slice 1 row) ----
        conn.execute(
            text(
                """
                INSERT INTO Interim_ADR_Records (
                    record_id, motivating_requirement, motivating_criterion,
                    observable_behavior, recorded_at, backlog_adr_id,
                    resolved_by_adr_id, resolved_at
                ) VALUES (
                    :rid, 'Slice1.R16', 'Slice1.R16.3',
                    'Pre-Slice-3 interim ADR observable behavior.',
                    :ts, 'ADR-HT-001', NULL, NULL
                )
                """
            ),
            {"rid": _INTERIM_ADR_RECORD_ID, "ts": _SLICE12_SEED_TS},
        )

        # ---- Audit_Records (one Slice 1 consequential row) ----
        conn.execute(
            text(
                """
                INSERT INTO Audit_Records (
                    audit_record_id, append_sequence, actor_party_id,
                    action_type, outcome, target_id, target_revision_id,
                    evaluated_role_assignment_id, authorities_required,
                    authorities_held, reason_code, correlation_id,
                    recorded_at
                ) VALUES (
                    :aid, 1, :party,
                    'create.decision', 'consequential', :did, NULL,
                    NULL, NULL, NULL, NULL,
                    'pre-slice3-correlation', :ts
                )
                """
            ),
            {
                "aid": _AUDIT_RECORD_ID,
                "party": _AUTHORING_PARTY_ID,
                "did": _DECISION_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )


def _seed_slice2(engine: Engine) -> None:
    """Populate one row in every Slice 2 table named by Requirement 40.4.

    Each row is inserted directly so the seeded baseline is
    deterministic across Hypothesis shrinks; the Slice 2 Planning
    services would multiply the seed surface per case without
    changing the invariant under test (Property 21 verifies their
    behaviour in isolation; Property 41 only needs the rows to
    exist so Slice 3 actions can see them).
    """
    with engine.begin() as conn:
        # ---- Identifier_Registry (Slice 2 — resource_kind populated) ----
        for identifier, kind, resource_kind in _SLICE2_IDENTIFIER_ROWS:
            conn.execute(
                text(
                    """
                    INSERT INTO Identifier_Registry
                        (identifier, kind, content_digest, issued_at, resource_kind)
                    VALUES
                        (:identifier, :kind, NULL, :issued_at, :resource_kind)
                    """
                ),
                {
                    "identifier": identifier,
                    "kind": kind,
                    "issued_at": _SLICE12_SEED_TS,
                    "resource_kind": resource_kind,
                },
            )

        # ---- Objectives + Objective_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :oid, NULL,
                    'Pre-Slice-3 objective statement.', NULL,
                    :did, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REVISION_ID,
                "oid": _OBJECTIVE_ID,
                "did": _DECISION_ID,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Intended_Outcomes + Intended_Outcome_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Intended_Outcomes (intended_outcome_id, created_at) "
                "VALUES (:iid, :ts)"
            ),
            {"iid": _INTENDED_OUTCOME_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Intended_Outcome_Revisions (
                    intended_outcome_revision_id, intended_outcome_id,
                    parent_revision_id, outcome_kind, target_objective_id,
                    success_condition, observation_window,
                    attribution_assumption, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :rev, :iid, NULL, 'intended', :oid,
                    'Pre-Slice-3 success condition.', NULL,
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _INTENDED_OUTCOME_REVISION_ID,
                "iid": _INTENDED_OUTCOME_ID,
                "oid": _OBJECTIVE_ID,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Projects + Project_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Project_Revisions (
                    project_revision_id, project_id, parent_revision_id,
                    name, summary, target_objective_id,
                    planned_start_date, planned_end_date,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pid, NULL,
                    'Pre-Slice-3 project name.', NULL, :oid,
                    '2026-01-01', '2026-12-31',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PROJECT_REVISION_ID,
                "pid": _PROJECT_ID,
                "oid": _OBJECTIVE_ID,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Deliverable_Expectations + Revisions ----
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {
                "did": _DELIVERABLE_EXPECTATION_ID,
                "ts": _SLICE12_SEED_TS,
            },
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
                    :rev, :did, NULL, :pid,
                    'Pre-Slice-3 expected Deliverable name.',
                    NULL, 'Document', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Activity_Plans ----
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Pre-Slice-3 activity plan title.',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Plan_Revisions (lifecycle_state='approved' directly) ----
        # The AD-WS-19 lifecycle trigger only fires on UPDATE, so a
        # row with ``lifecycle_state='approved'`` may be inserted in
        # one statement without driving the full Plan Approval
        # transaction.
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
                    'Pre-Slice-3 planned scope.', :de_refs,
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "de_refs": json.dumps([_DELIVERABLE_EXPECTATION_REVISION_ID]),
                "party": _AUTHORING_PARTY_ID,
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Plan_Reviews + Plan_Review_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Plan_Reviews (plan_review_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PLAN_REVIEW_ID, "ts": _SLICE12_SEED_TS},
        )
        conn.execute(
            text(
                """
                INSERT INTO Plan_Review_Revisions (
                    plan_review_revision_id, plan_review_id,
                    target_plan_revision_id, outcome, rationale,
                    reviewing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :pid, :prev, 'Endorse',
                    'Pre-Slice-3 review rationale.', :party,
                    'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "rev": _PLAN_REVIEW_REVISION_ID,
                "pid": _PLAN_REVIEW_ID,
                "prev": _PLAN_REVISION_ID,
                "party": _APPROVING_PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Plan_Approval_Records ----
        conn.execute(
            text(
                """
                INSERT INTO Plan_Approval_Records (
                    plan_approval_id, target_activity_plan_id,
                    target_plan_revision_id, outcome, rationale,
                    approving_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :pid, :aid, :prev, 'Approve',
                    'Pre-Slice-3 approval rationale.', :party,
                    'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "pid": _PLAN_APPROVAL_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "prev": _PLAN_REVISION_ID,
                "party": _APPROVING_PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SLICE12_SEED_TS,
            },
        )

        # ---- Slice 2 Plan Approval Provenance Manifest (AD-WS-21) ----
        conn.execute(
            text(
                """
                INSERT INTO Provenance_Manifests (
                    manifest_id, subject_kind, subject_id, subject_revision_id,
                    authoring_party_id, recorded_at, included_sources_json,
                    is_complete
                ) VALUES (
                    :mid, 'plan_approval', :sid, NULL,
                    :party, :ts, '[]', 1
                )
                """
            ),
            {
                "mid": _PLAN_APPROVAL_MANIFEST_ID,
                "sid": _PLAN_APPROVAL_ID,
                "party": _APPROVING_PARTY_ID,
                "ts": _SLICE12_SEED_TS,
            },
        )

    # ---- Disclosure_Policy_Coverage seeding ----
    # Slice 2 AD-WS-16 adds one Disclosure_Policy_Coverage row per
    # Slice 2 node kind; Slice 3 AD-WS-25 adds one per Slice 3 node
    # kind. Both seeders run before the snapshot is taken so the
    # Slice 2 rows are part of the protected baseline and the Slice 3
    # rows are present (the property explicitly excludes those from
    # the byte-equivalence check via the snapshot PK set).
    with engine.begin() as conn:
        seed_planning_coverage(conn)
        seed_execution_coverage(conn)


# ---------------------------------------------------------------------------
# Role assignment.
#
# The actor Party receives the four new Slice 3 authority values
# (``assign``, ``contribute``, ``accept_milestone``, ``complete``)
# plus ``view`` so the Provenance_Navigator subqueries the conflict
# pre-check uses succeed. The Role Assignment row is itself a Slice 1
# row written through the Slice 1 AuthorizationService — it sits in
# the protected snapshot and must remain byte-equivalent across the
# Slice 3 pipeline. AD-WS-24 permits the additive eight-value
# enumeration so a row carrying the four new values is well-formed
# under the post-Slice-3 schema.
# ---------------------------------------------------------------------------


def _assign_actor_role(
    authorization_service: AuthorizationService,
    engine: Engine,
) -> None:
    """Grant the actor every authority the Slice 3 pipeline needs.

    A second assignment grants the Contributor Party the
    ``contribute`` authority — the AD-WS-29 assignee-binding rule
    (Requirement 24.5 / 25.4 / 26.4 / 27.4) requires the recording
    Party of every Contributor write to be the named assignee on the
    Work Assignment, not the Assignment Authority who issued the
    Work Assignment.
    """
    actor_request = AssignRoleRequest(
        party_id=_ACTOR_PARTY_ID,
        role_name="execution_authority",
        scope=_SCOPE,
        authorities_granted=(
            "view",
            "modify",
            "review",
            "approve",
            "assign",
            "accept_milestone",
            "complete",
        ),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    contributor_request = AssignRoleRequest(
        party_id=_CONTRIBUTOR_PARTY_ID,
        role_name="execution_contributor",
        scope=_SCOPE,
        authorities_granted=("view", "contribute"),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, actor_request)
    with engine.begin() as conn:
        authorization_service.assign_role(conn, contributor_request)


# ---------------------------------------------------------------------------
# Snapshot helper.
#
# Reads each protected row by the primary-key set captured during
# seed and returns the rows as a hashable mapping. Storing the full
# row tuple (rather than a hex digest) keeps a failing assertion
# informative — Hypothesis prints the differing tuples directly.
# ---------------------------------------------------------------------------


def _capture_pks(engine: Engine) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Read the PK set of every protected row that exists right now.

    Property 41 protects every Slice 1 + Slice 2 row "before the
    Slice 3 actions began". This helper is called at the snapshot
    point — once after the Slice 1 + Slice 2 seed and the Slice 1
    Role Assignment have been written, before any Slice 3 service
    is invoked. Every PK returned identifies a pre-existing Slice 1
    or Slice 2 row.

    Reading the PK set from the live database (rather than from a
    hard-coded constant) is necessary for ``Role_Assignments``,
    whose primary-key Identity is minted by
    :class:`AuthorizationService` rather than known in advance.
    Reading every other table's PKs the same way keeps the helper
    symmetric and produces a precise snapshot of the seeded
    baseline regardless of which row in each table was inserted by
    which seeder.

    Because Slice 3 only inserts rows (Slice 3 AD-WS-27 — every
    Slice 3 table is append-only and every prior-slice append-only
    trigger remains in force), the captured PK set is naturally a
    subset of the post-Slice-3 PK set for any table Slice 3 writes
    to. Re-reading the captured PKs after every Slice 3 operation
    yields exactly the protected rows, with the new Slice 3 rows
    (new audit entries, new identifier rows, new Slice 3
    Relationships, new Slice 3 Disclosure_Policy_Coverage rows)
    excluded by construction.
    """
    captured: dict[str, tuple[tuple[Any, ...], ...]] = {}
    with engine.connect() as conn:
        for table_name, spec in _PROTECTED_TABLE_SPECS.items():
            pk_columns = spec["pk"]
            pk_cols_sql = ", ".join(pk_columns)
            order_sql = ", ".join(pk_columns)
            rows = conn.execute(
                text(
                    f"SELECT {pk_cols_sql} FROM {table_name} "
                    f"ORDER BY {order_sql}"
                )
            ).all()
            captured[table_name] = tuple(tuple(row) for row in rows)
    return captured



def _snapshot(
    engine: Engine,
    pk_set: dict[str, tuple[tuple[Any, ...], ...]],
) -> dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]:
    """Snapshot every captured Slice 1 + Slice 2 row, keyed by PK tuple."""
    out: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]] = {}
    with engine.connect() as conn:
        for table_name, spec in _PROTECTED_TABLE_SPECS.items():
            columns = ", ".join(spec["columns"])
            pk_columns = spec["pk"]
            where = " AND ".join(f"{col} = :{col}" for col in pk_columns)
            table_snapshot: dict[tuple[Any, ...], tuple[Any, ...]] = {}
            for pk_values in pk_set[table_name]:
                params = {col: val for col, val in zip(pk_columns, pk_values)}
                row = conn.execute(
                    text(
                        f"SELECT {columns} FROM {table_name} WHERE {where}"
                    ),
                    params,
                ).first()
                assert row is not None, (
                    f"Pre-existing Slice 1 / Slice 2 row missing from "
                    f"{table_name!r}: pk={pk_values!r}. Property 41 / "
                    f"Requirement 40.4 forbids Slice 3 actions from "
                    f"deleting Slice 1 or Slice 2 rows."
                )
                normalized = tuple(
                    bytes(v) if isinstance(v, memoryview) else v for v in row
                )
                table_snapshot[pk_values] = normalized
            out[table_name] = table_snapshot
    return out


def _assert_byte_equivalent(
    *,
    pre: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]],
    post: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]],
    observation_label: str,
) -> None:
    """Assert ``pre`` and ``post`` snapshot rows are byte-equal."""
    for table_name in _PROTECTED_TABLE_SPECS:
        pre_rows = pre[table_name]
        post_rows = post[table_name]
        assert pre_rows.keys() == post_rows.keys(), (
            f"At observation {observation_label!r}: pre-existing "
            f"Slice 1 / Slice 2 PK set on {table_name!r} changed "
            f"across Slice 3 actions. "
            f"Pre={sorted(pre_rows.keys())!r}, "
            f"Post={sorted(post_rows.keys())!r}. Property 41 / "
            f"Requirement 40.4 forbids deletion of Slice 1 / "
            f"Slice 2 rows."
        )
        for pk, pre_row in pre_rows.items():
            post_row = post_rows[pk]
            assert pre_row == post_row, (
                f"At observation {observation_label!r}: byte-"
                f"equivalence violated on {table_name!r} pk={pk!r}. "
                f"pre={pre_row!r}, post={post_row!r}. Property 41 / "
                f"Requirements 22.4, 22.6, 22.7, 22.8, 28.8, 29.7, "
                f"33.1, 40.1, 40.2, 40.3, 40.4, 41.11, 41.12 forbid "
                f"mutation of pre-existing Slice 1 or Slice 2 rows "
                f"by any Slice 3 action."
            )


# ---------------------------------------------------------------------------
# Service factory.
#
# Fresh services per Hypothesis case so :class:`IdentityService`
# in-memory state and any audit-correlation accumulator cannot bleed
# across shrinks. The denial-audit sleep is replaced with a no-op so
# the (unused on the happy path) deny-path retries do not spend real
# time.
# ---------------------------------------------------------------------------


def _build_services(
    engine: Engine,
) -> dict[str, Any]:
    """Construct the per-case Slice 3 service bundle."""
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    plan_revision_reader = PlanRevisionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    deliverable_expectation_reader = DeliverableExpectationService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    project_resolver = ProjectResolver()

    work_assignment_service = WorkAssignmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        denial_audit_sleep=lambda _seconds: None,
    )
    work_event_service = WorkEventService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    time_entry_service = TimeEntryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    deliverable_repository = DeliverableRepositoryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    deliverable_production_service = DeliverableProductionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        deliverable_reader=deliverable_repository,
        planning_reader=deliverable_expectation_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )
    milestone_acceptance_service = MilestoneAcceptanceService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        production_reader=deliverable_production_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    completion_service = CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )
    return {
        "clock": clock,
        "identity_service": identity_service,
        "audit_log": audit_log,
        "authorization_service": authorization_service,
        "work_assignment": work_assignment_service,
        "work_event": work_event_service,
        "time_entry": time_entry_service,
        "deliverable_repository": deliverable_repository,
        "deliverable_production": deliverable_production_service,
        "milestone_acceptance": milestone_acceptance_service,
        "completion": completion_service,
    }


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# Inputs to each Slice 3 operation. Lengths stay within the per-
# attribute range named by design §"Components and Interfaces" (and
# re-enforced by the CHECK constraints in
# :mod:`walking_slice.execution._persistence` and
# :mod:`walking_slice.deliverables._persistence`). The alphabet is
# narrow so shrunken counterexamples stay readable — Property 41 is
# not about UTF-8 robustness.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


@st.composite
def _pipeline_inputs(draw) -> dict[str, Any]:
    """Draw the textual / scalar inputs for the seven Slice 3 operations."""
    # Requirement 25.3 requires effort_period_end <= recorded_at;
    # _NOW (2026-01-01T00:00:00Z) is the per-case recorded time, so
    # both the start and end stay strictly before _NOW. The span is
    # bounded so the start + minutes never crosses the clock instant.
    effort_period_start = draw(
        st.datetimes(
            min_value=datetime(2025, 12, 30, tzinfo=None),
            max_value=datetime(2025, 12, 31, 22, 0, tzinfo=None),
        )
    )
    effort_period_start = effort_period_start.replace(tzinfo=timezone.utc)
    span_minutes = draw(st.integers(min_value=0, max_value=60))
    effort_period_end = effort_period_start + timedelta(minutes=span_minutes)
    # Effort hours kept small (the schema CHECK enforces 0.00..24.00);
    # the Decimal is constructed with two-decimal-place form so the
    # SQLite GLOB CHECK passes.
    effort_hours_int = draw(st.integers(min_value=0, max_value=24))
    effort_hours_frac = draw(st.integers(min_value=0, max_value=99))
    if effort_hours_int == 24 and effort_hours_frac > 0:
        effort_hours_frac = 0
    effort_hours = Decimal(f"{effort_hours_int}.{effort_hours_frac:02d}")
    return {
        # Work Assignment
        "wa_rationale": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Work Event (started)
        "we_event_note": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Time Entry
        "te_effort_hours": effort_hours,
        "te_effort_start": effort_period_start,
        "te_effort_end": effort_period_end,
        # Produced Deliverable
        "pd_name": draw(_bounded_text(1, 100)),
        "pd_content_bytes": draw(
            st.binary(min_size=1, max_size=256)
        ),
        # Deliverable Production
        "dp_rationale": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Milestone Acceptance
        "ma_rationale": draw(_bounded_text(1, 200)),
        # Completion
        "cp_rationale": draw(_bounded_text(1, 200)),
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 41: Slice 1 and Slice 2 non-modification under Slice 3 actions
@given(inputs=_pipeline_inputs())
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup builds a complete Slice 1 + Slice 2 seed graph,
    # registers two Role Assignments, then runs the seven-step
    # Slice 3 pipeline with snapshots after each operation. The data
    # generation and too-slow health checks are suppressed because
    # the per-case work is heavier than a pure in-memory property
    # test.
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
    ],
)
def test_slice1_slice2_rows_byte_equivalent_after_full_slice3_pipeline(
    inputs: dict[str, Any],
) -> None:
    """Every Slice 1 and Slice 2 row created before the Slice 3
    actions remains byte-equivalent after each of the seven Slice 3
    operations.

    The test runs the full Slice 3 happy-path pipeline (Work
    Assignment → Work Event[started] → Time Entry → Produced
    Deliverable → Deliverable Production → Milestone Acceptance →
    Completion) through the real Execution_Service and
    Deliverable_Repository service classes with Hypothesis-drawn
    inputs, and snapshots every protected Slice 1 + Slice 2 row
    after each operation. Any mutation by any Slice 3 action
    surfaces on the very next snapshot diff (Property 41's "every
    observation point" clause).

    The additive Slice 3 surfaces are excluded by construction:

    - New ``Audit_Records`` rows appended by every Slice 3 write
      have new PKs not captured at snapshot time.
    - New ``Identifier_Registry`` rows minted for every Slice 3
      Record / Resource / Revision have new PKs not captured at
      snapshot time.
    - New ``Disclosure_Policy_Coverage`` rows seeded by
      :func:`seed_execution_coverage` for the eight Slice 3 node
      kinds are excluded by construction (the seeder runs *before*
      the snapshot is taken so the Slice 3 coverage rows are part
      of the database state but not part of the captured Slice 2
      PK set — this is the AD-WS-25 additive surface the property
      explicitly excludes).
    - New ``Relationships`` rows written by every Slice 3 action
      (``Addresses`` / ``Relates To`` / ``Produces`` per AD-WS-26)
      have new PKs not captured at snapshot time. The additive
      ``semantic_role`` markers from AD-WS-26 are columns on those
      *new* rows, never on the pre-existing Slice 1 Relationship.

    The Role Assignment row carrying the four new Slice 3 authority
    values (``assign``, ``contribute``, ``accept_milestone``,
    ``complete``) is itself captured in the snapshot and verified
    byte-equivalent across the pipeline. AD-WS-24 permits the row
    to carry the additive enumeration values; Property 41 verifies
    that no Slice 3 action mutates the row once it exists.
    """
    with tempfile.TemporaryDirectory(prefix="prop41_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            _seed_slice1(engine)
            _seed_slice2(engine)

            services = _build_services(engine)

            # Assign the actor and contributor roles. The Role
            # Assignment rows are themselves Slice 1 rows that the
            # snapshot captures and protects.
            _assign_actor_role(services["authorization_service"], engine)

            # Snapshot the pre-Slice-3 ground truth.
            pk_set = _capture_pks(engine)
            pre_snapshot = _snapshot(engine, pk_set)

            # --- Op 1: create.work_assignment -----------------------
            with engine.begin() as conn:
                wa_result = services["work_assignment"].create_work_assignment(
                    conn,
                    target_plan_revision_id=_PLAN_REVISION_ID,
                    assignee_party_id=_CONTRIBUTOR_PARTY_ID,
                    assignment_authority_party_id=_ACTOR_PARTY_ID,
                    assignment_rationale=inputs["wa_rationale"],
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop41-wa",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.work_assignment",
            )

            # --- Op 2: create.work_event (started) ------------------
            with engine.begin() as conn:
                services["work_event"].create_work_event(
                    conn,
                    target_work_assignment_id=wa_result.work_assignment_id,
                    event_kind="started",
                    event_note=inputs["we_event_note"],
                    recording_party_id=_CONTRIBUTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop41-we",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.work_event",
            )

            # --- Op 3: create.time_entry ----------------------------
            with engine.begin() as conn:
                services["time_entry"].create_time_entry(
                    conn,
                    target_work_assignment_id=wa_result.work_assignment_id,
                    effort_hours=inputs["te_effort_hours"],
                    effort_period_start=inputs["te_effort_start"],
                    effort_period_end=inputs["te_effort_end"],
                    recording_party_id=_CONTRIBUTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop41-te",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.time_entry",
            )

            # --- Op 4: create.produced_deliverable ------------------
            with engine.begin() as conn:
                pd_result = services[
                    "deliverable_repository"
                ].create_produced_deliverable(
                    conn,
                    content_bytes=inputs["pd_content_bytes"],
                    content_type="application/octet-stream",
                    produced_deliverable_name=inputs["pd_name"],
                    originating_work_assignment_id=wa_result.work_assignment_id,
                    authoring_party_id=_CONTRIBUTOR_PARTY_ID,
                    engine=engine,
                    correlation_id="prop41-pd",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.produced_deliverable",
            )

            # --- Op 5: create.deliverable_production ----------------
            with engine.begin() as conn:
                dp_result = services[
                    "deliverable_production"
                ].create_deliverable_production(
                    conn,
                    source_work_assignment_id=wa_result.work_assignment_id,
                    produced_deliverable_revision_id=(
                        pd_result.deliverable_revision_id
                    ),
                    target_deliverable_expectation_revision_id=(
                        _DELIVERABLE_EXPECTATION_REVISION_ID
                    ),
                    production_rationale=inputs["dp_rationale"],
                    recording_party_id=_CONTRIBUTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop41-dp",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.deliverable_production",
            )

            # --- Op 6: create.milestone_acceptance (Accept) ---------
            with engine.begin() as conn:
                services["milestone_acceptance"].create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        dp_result.deliverable_production_id
                    ),
                    outcome="Accept",
                    rationale=inputs["ma_rationale"],
                    accepting_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop41-ma",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.milestone_acceptance",
            )

            # --- Op 7: create.completion ----------------------------
            with engine.begin() as conn:
                services["completion"].create_completion(
                    conn,
                    target_plan_revision_id=_PLAN_REVISION_ID,
                    outcome="Completed",
                    rationale=inputs["cp_rationale"],
                    source_milestone_acceptance_ids=(),
                    completing_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                    correlation_id="prop41-cp",
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="after create.completion",
            )

            # --- Final whole-pipeline snapshot ----------------------
            # Belt-and-suspenders re-check after every Slice 3
            # operation has completed, in case a deferred mutation
            # only surfaces at end-of-pipeline.
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot(engine, pk_set),
                observation_label="end-of-pipeline",
            )
        finally:
            engine.dispose()
