# Feature: second-walking-slice, Property 21: Slice 1 non-modification
"""Property 21 — Slice 1 non-modification (task 16.6).

**Property 21: Slice 1 non-modification**

*For all* test sessions exercising the Planning_Service, at every
observation point after any sequence of Slice 2 actions, every row
created by Slice 1 — ``Audit_Records``, ``Identifier_Registry`` (apart
from the additive ``resource_kind`` column populated for Slice 2
rows), ``Interim_ADR_Records``, ``Disclosure_Policies``, ``Decisions``,
``Role_Assignments``, ``Document_Revisions``, ``Region_Occurrences``,
``Finding_Revisions``, ``Recommendation_Revisions``, ``Relationships``
(apart from the additive ``semantic_role`` column with NULL on Slice 1
rows), ``Trail_Revisions``, ``Trail_Steps``, and
``Provenance_Manifests`` — is byte-equivalent to its state before the
Slice 2 actions began.

**Validates: Requirements 19.1, 19.2, 19.3, 19.4, 20.11**

Strategy
========

Each Hypothesis case:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path with both the Slice 1
   schema (:func:`walking_slice.persistence.create_schema`) and the
   Slice 2 schema
   (:func:`walking_slice.planning._persistence.create_planning_schema`).
   The ``slice-default-2026`` Disclosure_Policies row is seeded via
   :func:`walking_slice.disclosure.seed` so the snapshot has a real
   row to protect.
2. Seeds one representative row into every Slice 1 table named by
   Requirement 19.4 / 20.11 via direct ``INSERT``. Direct INSERTs
   keep the seed deterministic and shrink-friendly (a Slice 1
   service path would multiply the seed surface across each case and
   slow shrinking). The seeded shape covers a complete Decision →
   Recommendation → Finding → Region → Document chain plus a Trail
   with all five Pinned steps plus a Provenance Manifest and one
   ``Supports`` Relationship — the minimal pre-existing graph
   Property 21 protects across every Slice 1 table.
3. Captures the primary keys of every seeded row and takes a
   byte-level snapshot of every column (including the additive
   ``Relationships.semantic_role`` and
   ``Identifier_Registry.resource_kind`` columns — both NULL on
   pre-existing Slice 1 rows per AD-WS-17 / AD-WS-19) at those PKs.
4. Runs the full Slice 2 happy-path pipeline — Objective → Intended
   Outcome → Project → Deliverable Expectation → Activity Plan →
   Plan Revision → Plan Review → Plan Approval — through the real
   Planning_Service service classes, with Hypothesis-drawn textual
   inputs varying per case. After each Slice 2 operation, the test
   re-reads the same captured PKs from each Slice 1 table and
   asserts every column is byte-equivalent to the pre-Slice-2
   snapshot. This realises "at every observation point" from the
   property statement: any mutation by any Slice 2 action would
   surface on the very next snapshot diff.
5. Performs one final whole-Slice-1 snapshot after the complete
   pipeline and asserts byte-equivalence one more time, so a
   regression that only manifests at the very end (e.g., a deferred
   UPDATE inside the last transaction) still surfaces.

Note on Slice 1 tables that *do* receive new Slice 2 rows. The
``Audit_Records``, ``Identifier_Registry``, ``Relationships``, and
``Provenance_Manifests`` tables receive new rows from Slice 2 service
calls (audit appends, identifier registrations, ``Addresses`` /
``Relates To`` / ``Supersedes`` edges, the Plan Approval manifest).
Those new rows are *not* Slice 1 rows; Property 21 only protects the
pre-existing rows captured in the snapshot. The snapshot helper
re-reads by the captured PK set, which intentionally excludes the
new Slice 2 rows.
"""

from __future__ import annotations

import json
import tempfile
import uuid as uuid_lib
from datetime import date, datetime, timedelta, timezone
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
from walking_slice.disclosure import seed as disclosure_seed
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.activity_plans import ActivityPlanService
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.objectives import ObjectiveService
from walking_slice.planning.plan_approvals import PlanApprovalService
from walking_slice.planning.plan_reviews import PlanReviewService
from walking_slice.planning.plan_revisions import PlanRevisionService
from walking_slice.planning.projects import ProjectService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants for the Slice 1 seed and the actor Party.
#
# UUIDv7-shaped strings keep the seed compatible with the
# ``Identifier_Registry`` row format and the FK targets each row needs.
# A single actor Party covers every consequential write — the Slice 1
# row gets its own ``recorded_at`` value distinct from the per-case
# clock so the post-Slice-2 snapshot can distinguish "pre-existing"
# rows from any new ones written by the planning pipeline.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

# Slice 1 seed timestamp is deliberately offset from the per-case clock
# so any row whose ``recorded_at`` accidentally changes to the Slice 2
# clock value would surface as a snapshot diff.
_SLICE1_SEED_TS: Final[str] = "2025-12-15T10:30:00.000Z"

_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"

# Slice 1 seed identifiers. Each follows the UUIDv7-shaped textual
# convention used by Slice 1 fixtures and unit tests.
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
# Each row carries one of the Slice 1 ``kind`` values; ``resource_kind``
# is left NULL on every pre-existing Slice 1 row per AD-WS-19 (the
# additive column is populated only for Slice 2 rows). The tuples below
# also act as the pre-existing PK set for the snapshot helper.
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

_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-21/scope"


# ---------------------------------------------------------------------------
# Slice 1 snapshot specifications.
#
# For each Slice 1 table protected by Requirement 19.4 / 20.11, name
# the columns to SELECT and the primary-key columns the snapshot keys
# by. The additive Slice 2 columns ``Relationships.semantic_role`` and
# ``Identifier_Registry.resource_kind`` are *included* in the column
# list so the post-Slice-2 snapshot diff covers them — they must
# remain NULL on every pre-existing Slice 1 row.
# ---------------------------------------------------------------------------


_SLICE1_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
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
}


# ---------------------------------------------------------------------------
# Per-case engine helper.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a per-case engine carrying both Slice 1 and Slice 2 schemas."""
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
    # Seed the slice-default-2026 Disclosure_Policies row so the
    # snapshot has one real row to protect (Requirement 19.4 names
    # Disclosure_Policies explicitly).
    disclosure_seed(engine)
    return engine


# ---------------------------------------------------------------------------
# Slice 1 seed.
#
# One representative row per Slice 1 table protected by Requirement
# 19.4 / 20.11. Direct INSERT keeps the seed deterministic — Slice 1
# service paths would multiply the per-case work and slow shrinking
# without changing the invariant under test.
# ---------------------------------------------------------------------------


def _seed_slice1(engine: Engine) -> None:
    """Populate one row in every Slice 1 table named by Requirement 19.4."""
    with engine.begin() as conn:
        # ---- Parties (FK target for many rows) ----
        for party_id, display in (
            (_PARTY_ID, "Property 21 Actor"),
            (_ASSIGNING_AUTHORITY_ID, "Property 21 Resource Steward"),
        ):
            conn.execute(
                text(
                    "INSERT INTO Parties (party_id, kind, display_name, created_at) "
                    "VALUES (:pid, 'person', :name, :ts)"
                ),
                {"pid": party_id, "name": display, "ts": _SLICE1_SEED_TS},
            )

        # ---- Identifier_Registry (12 rows; one per identifier kind) ----
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
                    "issued_at": _SLICE1_SEED_TS,
                },
            )

        # ---- Role_Assignments (grants the actor every authority the
        # Slice 2 pipeline below needs: modify/review/approve plus
        # view for completeness). Slice 2 services never touch the
        # row; Property 21 protects it byte-for-byte. ----
        conn.execute(
            text(
                """
                INSERT INTO Role_Assignments (
                    role_assignment_id, party_id, role_name, scope,
                    authorities_granted, effective_start, effective_end,
                    revoked_at, assigning_authority_id, recorded_at
                ) VALUES (
                    :ra, :party, 'planning_actor', :scope,
                    :authorities, :start, :end, NULL,
                    :assigner, :ts
                )
                """
            ),
            {
                "ra": _ROLE_ASSIGNMENT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "authorities": json.dumps(
                    sorted(("view", "modify", "review", "approve"))
                ),
                # Effective period brackets both the Slice 1 seed time
                # and the Slice 2 ``_NOW`` clock so every Slice 2
                # evaluation is in-window.
                "start": "2025-01-01T00:00:00.000Z",
                "end": "2027-01-01T00:00:00.000Z",
                "assigner": _ASSIGNING_AUTHORITY_ID,
                "ts": _SLICE1_SEED_TS,
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
            {"rid": _DOCUMENT_RESOURCE_ID, "ts": _SLICE1_SEED_TS},
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
                "party": _PARTY_ID,
                "ts": _SLICE1_SEED_TS,
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
                "ts": _SLICE1_SEED_TS,
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
                "ts": _SLICE1_SEED_TS,
            },
        )

        # ---- Findings + Finding_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Findings (finding_id, created_at) "
                "VALUES (:fid, :ts)"
            ),
            {"fid": _FINDING_ID, "ts": _SLICE1_SEED_TS},
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
                    'Pre-Slice-2 finding statement.', 0, :party,
                    '[]', NULL, :ts
                )
                """
            ),
            {
                "rev": _FINDING_REVISION_ID,
                "fid": _FINDING_ID,
                "party": _PARTY_ID,
                "ts": _SLICE1_SEED_TS,
            },
        )

        # ---- Recommendations + Recommendation_Revisions ----
        conn.execute(
            text(
                "INSERT INTO Recommendations (recommendation_id, created_at) "
                "VALUES (:rid, :ts)"
            ),
            {"rid": _RECOMMENDATION_ID, "ts": _SLICE1_SEED_TS},
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
                    'Pre-Slice-2 recommendation rationale.', '[]',
                    'Medium', :party, :ts
                )
                """
            ),
            {
                "rev": _RECOMMENDATION_REVISION_ID,
                "rid": _RECOMMENDATION_ID,
                "party": _PARTY_ID,
                "ts": _SLICE1_SEED_TS,
            },
        )

        # ---- Decisions (Accept outcome so the ObjectiveService
        # AD-WS-21 resolution can succeed) ----
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
                    'Pre-Slice-2 decision rationale.', :party,
                    'role-grant-id', :ab, :scope, :ts
                )
                """
            ),
            {
                "did": _DECISION_ID,
                "rid": _RECOMMENDATION_ID,
                "rev": _RECOMMENDATION_REVISION_ID,
                "party": _PARTY_ID,
                "ab": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _SLICE1_SEED_TS,
            },
        )

        # ---- Relationships (one Slice 1 ``Supports`` edge linking
        # a Finding Revision to a Region Occurrence; semantic_role
        # is NULL — the AD-WS-17 column is reserved for Slice 2
        # Plan Review's 'review' discriminator). ----
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
                "party": _PARTY_ID,
                "ts": _SLICE1_SEED_TS,
            },
        )

        # ---- Trails + Trail_Revisions + Trail_Steps (5 Pinned steps
        # covering all five ordinal/target_kind CHECK alternatives). ----
        conn.execute(
            text(
                "INSERT INTO Trails (trail_id, created_at, current_revision_id) "
                "VALUES (:tid, :ts, :rev)"
            ),
            {
                "tid": _TRAIL_ID,
                "ts": _SLICE1_SEED_TS,
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
                    'Pre-Slice-2 trail purpose.', :aud,
                    NULL, :party, :ts
                )
                """
            ),
            {
                "rev": _TRAIL_REVISION_ID,
                "tid": _TRAIL_ID,
                "aud": _ASSIGNING_AUTHORITY_ID,
                "party": _PARTY_ID,
                "ts": _SLICE1_SEED_TS,
            },
        )
        # Trail_Steps CHECK requires (ordinal, target_kind) pairing.
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

        # ---- Provenance_Manifests (one Decision manifest) ----
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
                "party": _PARTY_ID,
                "ts": _SLICE1_SEED_TS,
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
                    'Pre-Slice-2 interim ADR observable behavior.',
                    :ts, 'ADR-HT-001', NULL, NULL
                )
                """
            ),
            {"rid": _INTERIM_ADR_RECORD_ID, "ts": _SLICE1_SEED_TS},
        )

        # ---- Audit_Records (one Slice 1 consequential row recording
        # the seeded Decision). Slice 2 services append their own
        # consequential and evaluation rows; the snapshot helper only
        # re-reads this single PK. ----
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
                    'pre-slice2-correlation', :ts
                )
                """
            ),
            {
                "aid": _AUDIT_RECORD_ID,
                "party": _PARTY_ID,
                "did": _DECISION_ID,
                "ts": _SLICE1_SEED_TS,
            },
        )


# ---------------------------------------------------------------------------
# Snapshot helper.
#
# Reads each protected Slice 1 row by the primary-key set captured
# during seed and returns the rows as a hashable mapping. Storing the
# full row tuple (rather than a hex digest) keeps a failing assertion
# informative — Hypothesis prints the differing tuples directly.
# ---------------------------------------------------------------------------


def _captured_pks() -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Return the set of PKs the seed installed for each protected table."""
    return {
        "Audit_Records": ((_AUDIT_RECORD_ID,),),
        "Identifier_Registry": tuple(
            (identifier,) for identifier, _kind in _SLICE1_IDENTIFIER_ROWS
        ),
        "Interim_ADR_Records": ((_INTERIM_ADR_RECORD_ID,),),
        "Disclosure_Policies": (("slice-default-2026",),),
        "Decisions": ((_DECISION_ID,),),
        "Role_Assignments": ((_ROLE_ASSIGNMENT_ID,),),
        "Document_Revisions": ((_DOCUMENT_REVISION_ID,),),
        "Region_Occurrences": ((_REGION_ID, _DOCUMENT_REVISION_ID),),
        "Finding_Revisions": ((_FINDING_REVISION_ID,),),
        "Recommendation_Revisions": ((_RECOMMENDATION_REVISION_ID,),),
        "Relationships": ((_RELATIONSHIP_ID,),),
        "Trail_Revisions": ((_TRAIL_REVISION_ID,),),
        "Trail_Steps": tuple((sid,) for sid in _TRAIL_STEP_IDS),
        "Provenance_Manifests": ((_MANIFEST_ID,),),
    }


def _snapshot_slice1(
    engine: Engine,
    pk_set: dict[str, tuple[tuple[Any, ...], ...]],
) -> dict[str, dict[tuple[Any, ...], tuple[Any, ...]]]:
    """Snapshot every captured Slice 1 row, keyed by PK tuple."""
    out: dict[str, dict[tuple[Any, ...], tuple[Any, ...]]] = {}
    with engine.connect() as conn:
        for table_name, spec in _SLICE1_TABLE_SPECS.items():
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
                    f"Pre-existing Slice 1 row missing from {table_name!r}: "
                    f"pk={pk_values!r}. Property 21 / Requirement 19.4 "
                    f"forbids Slice 2 actions from deleting Slice 1 rows."
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
    for table_name in _SLICE1_TABLE_SPECS:
        pre_rows = pre[table_name]
        post_rows = post[table_name]
        assert pre_rows.keys() == post_rows.keys(), (
            f"At observation {observation_label!r}: pre-existing Slice 1 "
            f"PK set on {table_name!r} changed across Slice 2 actions. "
            f"Pre={sorted(pre_rows.keys())!r}, "
            f"Post={sorted(post_rows.keys())!r}. Property 21 / "
            f"Requirement 19.4 forbids deletion of Slice 1 rows."
        )
        for pk, pre_row in pre_rows.items():
            post_row = post_rows[pk]
            assert pre_row == post_row, (
                f"At observation {observation_label!r}: byte-equivalence "
                f"violated on {table_name!r} pk={pk!r}. "
                f"pre={pre_row!r}, post={post_row!r}. "
                f"Property 21 / Requirements 19.1, 19.2, 19.3, 19.4, 20.11 "
                f"forbid mutation of pre-existing Slice 1 rows by any "
                f"Slice 2 action."
            )


# ---------------------------------------------------------------------------
# Service factory.
#
# Fresh services per Hypothesis case so :class:`IdentityService`
# in-memory state and any audit-correlation accumulator cannot bleed
# across shrinks. Built without the Slice 1 ``engine``/``audit_log``
# wiring on :class:`IdentityService` so identifier conflicts surface
# as exceptions inside the caller's transaction (rather than via the
# separate-transaction denial path) — Property 21 does not exercise
# the conflict path.
# ---------------------------------------------------------------------------


def _build_services() -> dict[str, Any]:
    """Construct the per-case service bundle for the Slice 2 pipeline."""
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    manifest_writer = ProvenanceManifestWriter(
        clock=clock,
        identity_service=identity_service,
    )
    return {
        "clock": clock,
        "identity_service": identity_service,
        "audit_log": audit_log,
        "authorization_service": authorization_service,
        "knowledge_service": knowledge_service,
        "manifest_writer": manifest_writer,
        "objectives": ObjectiveService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
            knowledge_service=knowledge_service,
        ),
        "intended_outcomes": IntendedOutcomeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        ),
        "projects": ProjectService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        ),
        "deliverable_expectations": DeliverableExpectationService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        ),
        "activity_plans": ActivityPlanService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        ),
        "plan_revisions": PlanRevisionService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        ),
        "plan_reviews": PlanReviewService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        ),
        "plan_approvals": PlanApprovalService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
            manifest_writer=manifest_writer,
        ),
    }


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# Inputs to each Slice 2 operation. Lengths stay within the per-attribute
# range named by design §"Components and Interfaces" (and re-enforced by
# the CHECK constraints in :mod:`walking_slice.planning._persistence`).
# Narrow alphabet keeps shrunken counterexamples readable — Property 21
# is not about UTF-8 robustness.
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
    """Draw the textual inputs for the eight Slice 2 operations."""
    start = draw(
        st.dates(
            min_value=date(2026, 1, 1),
            max_value=date(2026, 12, 31),
        )
    )
    span_days = draw(st.integers(min_value=0, max_value=120))
    end = start + timedelta(days=span_days)
    return {
        # Objective
        "objective_statement": draw(_bounded_text(1, 200)),
        "objective_rationale": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Intended Outcome
        "io_success_condition": draw(_bounded_text(1, 200)),
        "io_observation_window": draw(
            st.one_of(st.none(), _bounded_text(0, 100))
        ),
        "io_attribution_assumption": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Project
        "project_name": draw(_bounded_text(1, 200)),
        "project_summary": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        "project_planned_start": start,
        "project_planned_end": end,
        # Deliverable Expectation
        "de_name": draw(_bounded_text(1, 200)),
        "de_description": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        "de_kind": draw(
            st.sampled_from(["Document", "Artifact", "Service", "Other"])
        ),
        "de_acceptance_criteria": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Activity Plan
        "ap_title": draw(_bounded_text(1, 200)),
        # Plan Revision
        "pr_planned_scope": draw(_bounded_text(1, 200)),
        "pr_planning_assumptions": draw(
            st.lists(_bounded_text(1, 100), min_size=0, max_size=3)
        ),
        "pr_ordering_rationale": draw(
            st.one_of(st.none(), _bounded_text(0, 200))
        ),
        # Plan Review
        "prv_outcome": draw(
            st.sampled_from(["Endorse", "Changes_Requested", "Reject"])
        ),
        "prv_rationale": draw(_bounded_text(1, 200)),
        # Plan Approval
        "pa_rationale": draw(_bounded_text(1, 200)),
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 21: Slice 1 non-modification
@given(inputs=_pipeline_inputs())
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup builds a complete Slice 1 seed graph plus the
    # eight Slice 2 operations and snapshots after each. The data
    # generation health check is suppressed because the per-case work
    # is heavier than a pure in-memory property test.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_slice1_rows_byte_equivalent_after_full_slice2_pipeline(
    inputs: dict[str, Any],
) -> None:
    """Every Slice 1 row created before the Slice 2 actions remains
    byte-equivalent after each of the eight Slice 2 operations.

    The test runs the full Slice 2 happy-path pipeline (Objective →
    Intended Outcome → Project → Deliverable Expectation → Activity
    Plan → Plan Revision → Plan Review → Plan Approval) through the
    real Planning_Service service classes with Hypothesis-drawn
    inputs, and snapshots every protected Slice 1 row after each
    operation. Any mutation by any Slice 2 action surfaces on the
    very next snapshot diff (Requirement 19 / Property 21's "every
    observation point" clause).
    """
    with tempfile.TemporaryDirectory(prefix="prop21_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            _seed_slice1(engine)
            pk_set = _captured_pks()
            pre_snapshot = _snapshot_slice1(engine, pk_set)

            services = _build_services()

            # --- Op 1: create.objective ----------------------------
            with engine.begin() as conn:
                obj_result = services["objectives"].create_objective(
                    conn,
                    statement=inputs["objective_statement"],
                    rationale=inputs["objective_rationale"],
                    target_decision_id=_DECISION_ID,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.objective",
            )

            # --- Op 2: create.intended_outcome ---------------------
            with engine.begin() as conn:
                services["intended_outcomes"].create_intended_outcome(
                    conn,
                    target_objective_id=obj_result.objective_id,
                    success_condition=inputs["io_success_condition"],
                    observation_window=inputs["io_observation_window"],
                    attribution_assumption=inputs["io_attribution_assumption"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.intended_outcome",
            )

            # --- Op 3: create.project ------------------------------
            with engine.begin() as conn:
                project_result = services["projects"].create_project(
                    conn,
                    target_objective_id=obj_result.objective_id,
                    name=inputs["project_name"],
                    summary=inputs["project_summary"],
                    planned_start_date=inputs["project_planned_start"],
                    planned_end_date=inputs["project_planned_end"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.project",
            )

            # --- Op 4: create.deliverable_expectation --------------
            with engine.begin() as conn:
                services["deliverable_expectations"].create_deliverable_expectation(
                    conn,
                    target_project_id=project_result.project_id,
                    name=inputs["de_name"],
                    description=inputs["de_description"],
                    deliverable_kind=inputs["de_kind"],
                    acceptance_criteria=inputs["de_acceptance_criteria"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.deliverable_expectation",
            )

            # --- Op 5: create.activity_plan ------------------------
            with engine.begin() as conn:
                ap_result = services["activity_plans"].create_activity_plan(
                    conn,
                    target_project_id=project_result.project_id,
                    title=inputs["ap_title"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.activity_plan",
            )

            # --- Op 6: create.plan_revision ------------------------
            with engine.begin() as conn:
                pr_result = services["plan_revisions"].create_plan_revision(
                    conn,
                    target_activity_plan_id=ap_result.activity_plan_id,
                    planned_scope=inputs["pr_planned_scope"],
                    planning_assumptions=inputs["pr_planning_assumptions"],
                    ordering_rationale=inputs["pr_ordering_rationale"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.plan_revision",
            )

            # --- Op 7: create.plan_review --------------------------
            with engine.begin() as conn:
                services["plan_reviews"].create_plan_review(
                    conn,
                    target_plan_revision_id=pr_result.plan_revision_id,
                    outcome=inputs["prv_outcome"],
                    rationale=inputs["prv_rationale"],
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.plan_review",
            )

            # --- Op 8: create.plan_approval ------------------------
            # ``Approve`` flips the Plan Revision lifecycle state to
            # ``approved`` inside the same transaction (AD-WS-20).
            # The lifecycle flip is on the Slice 2 ``Plan_Revisions``
            # table, which is not part of the Slice 1 snapshot — the
            # invariant under test is that no Slice 1 row mutates.
            with engine.begin() as conn:
                services["plan_approvals"].create_plan_approval(
                    conn,
                    engine,
                    target_plan_revision_id=pr_result.plan_revision_id,
                    outcome="Approve",
                    rationale=inputs["pa_rationale"],
                    approving_party_id=_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                )
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="after create.plan_approval",
            )

            # --- Final whole-pipeline snapshot ---------------------
            # Belt-and-suspenders re-check after every Slice 2
            # operation has completed, in case a deferred mutation
            # only surfaces at end-of-pipeline.
            _assert_byte_equivalent(
                pre=pre_snapshot,
                post=_snapshot_slice1(engine, pk_set),
                observation_label="end-of-pipeline",
            )
        finally:
            engine.dispose()
