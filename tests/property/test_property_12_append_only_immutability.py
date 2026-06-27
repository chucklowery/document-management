# Feature: first-walking-slice, Property 12: Append-only immutability
"""Property 12 — Append-only immutability across all immutable tables (task 15.4).

**Property 12: Append-only immutability across all immutable tables**

For all sequences of operations applied to the immutable tables
(``Document_Revisions``, ``Region_Occurrences``, ``Finding_Revisions``,
``Recommendation_Revisions``, ``Decisions``, ``Relationships``,
``Trail_Revisions``, ``Trail_Steps``, ``Provenance_Manifests``,
``Omission_Entries`` except their ``resolved_at`` one-shot field,
``Audit_Records``), at any two observation points in the test, the byte
content of every previously inserted row is identical. No operation
rewrites, reorders, or deletes an earlier row. ``Audit_Records``
additionally preserves a monotonically non-decreasing ``append_sequence``
ordered by ``recorded_at``.

The two one-shot fields ``Role_Assignments.revoked_at`` and
``Omission_Entries.resolved_at`` are tested adjunctively: a second
attempt to set or clear the one-shot column after it has been set must
also be rejected so the rest of the row remains byte-equivalent.

**Validates: Requirements 2.4, 2.7, 4.4, 6.6, 7.5, 13.3, 13.4, 13.5, 13.6, 15.12**

Strategy:

Each Hypothesis case (a) seeds a *full* slice graph — one Source
Document Revision, one Region Occurrence, one Finding Revision (with
its Supports Relationship), one Recommendation Revision (with its
Derived From Relationship), one Decision (with its Addresses
Relationship and inline Provenance Manifest), one Trail Revision (with
five Trail Steps), one supplementary Provenance Manifest with one
Omission Entry, one Role Assignment with ``revoked_at = NULL``, one
Role Assignment with ``revoked_at`` already set (inserted directly so
the one-shot trigger has both NULL and non-NULL targets to defend),
and one Omission Entry with ``resolved_at`` already set — then (b)
generates a sequence of UPDATE/DELETE *attempts* against every
immutable table and one-shot column.

Per case the test:

1. Builds a fresh per-test SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path (design §"Testing
   Strategy" — per-case database isolation).
2. Snapshots every row in every immutable table by SELECT-ing every
   column in stable PK order and storing the rows as ``tuple``
   objects keyed by table name. The snapshot is the byte-equivalence
   ground truth for the post-attack comparison.
3. Iterates the drawn attack list, issuing each UPDATE or DELETE
   against the table named in the attack tuple, and asserts that
   *every* attack raises a SQLAlchemy
   :class:`~sqlalchemy.exc.IntegrityError` — the trigger fired.
4. Re-snapshots the same rows and asserts byte-for-byte equality
   with the pre-attack snapshot (Property 12's universal quantifier).
5. Verifies ``Audit_Records.append_sequence`` is monotonically
   non-decreasing when the rows are ordered by ``recorded_at``
   (Requirement 13.5; design Persistence Invariants Summary item 8).
6. Additionally exercises the two one-shot triggers
   (``Role_Assignments.revoked_at_one_shot`` and
   ``Omission_Entries.resolved_at_one_shot``) by attempting to
   overwrite or clear an already-set one-shot value on the
   pre-seeded rows and asserting rejection. These checks are
   deterministic (not Hypothesis-driven) because the alphabet of
   "second one-shot transitions" is small and fixed.

Attack alphabet:

- ``update`` — ``UPDATE <table> SET <column> = <new_value> WHERE
  <pk> = <pk_value>``. ``column`` is drawn from a per-table allow-list
  that excludes the one-shot columns (so a NULL-to-value transition
  cannot accidentally satisfy the immutability guarantee). The
  allow-list contains both PK and non-PK columns so the strategy
  exercises the full append-only contract: PK rewrites and non-PK
  rewrites must both be rejected.
- ``delete`` — ``DELETE FROM <table> WHERE <pk> = <pk_value>``.

Per-table allow-lists are defined in
:data:`_IMMUTABLE_UPDATE_COLUMNS`. The lists are deliberately
*non-exhaustive*: they sample at least three columns per table
(including any composite-PK members) so each Hypothesis case
exercises a representative slice without forcing the case budget to
re-enumerate every schema column on every run.

Test scaffolding follows the conventions established by
``tests/property/test_property_1_evidence_support.py``,
``tests/property/test_property_5_trail_linearity.py``, and
``tests/property/test_property_7_provenance_non_omission.py``:

- :class:`tempfile.TemporaryDirectory` owns the per-case SQLite file
  (function-scoped pytest fixtures would not reset between
  Hypothesis cases).
- The :class:`~walking_slice.clock.FixedClock` is pinned to
  ``2026-01-01T00:00:00.000Z`` so every recorded timestamp is
  deterministic across shrinks.
- ``@settings(max_examples=50, deadline=5000)`` because per-case
  setup builds a full pipeline (Document Revision through Decision
  through Trail through Manifest) — slower than a pure in-memory
  property, but well within the 5 s per-case budget the task
  prescribes.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

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
from walking_slice.manifests import (
    IncludedSource,
    OmissionEntry,
    ProvenanceManifestWriter,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.trails import (
    TrailService,
    TrailStepInput,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# A single Party is sufficient for every FK reference in the seeded
# pipeline; Property 12 does not range over Parties. The authority
# basis is just the FK target required by :class:`AuthorityBasisRef`
# — Decision authority itself is covered by Property 2.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_PARTY_CREATED_AT: Final[str] = "2026-01-01T00:00:00.000Z"
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-00000000a012"
)
_SCOPE: Final[str] = "property-12/scope"
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FIXED_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"
# Pre-set one-shot values for the second Role_Assignment and second
# Omission_Entry; the post-attack assertions verify that further writes
# to these columns are rejected.
_PRE_REVOKED_AT_ISO: Final[str] = "2026-01-01T00:30:00.000Z"
_PRE_RESOLVED_AT_ISO: Final[str] = "2026-01-01T00:45:00.000Z"


# ---------------------------------------------------------------------------
# Per-table snapshot specifications.
#
# For each immutable table the test snapshots, name the columns to
# SELECT (in a stable order) and the columns to ORDER BY so the
# byte-equivalence comparison is deterministic. ``order_by`` is a
# stable composite of the PK columns so two snapshots taken before and
# after the attack list see rows in the same order.
# ---------------------------------------------------------------------------


_TABLE_SPECS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
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
        "order_by": ("revision_id",),
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
        "order_by": ("region_id", "document_revision_id"),
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
        "order_by": ("finding_revision_id",),
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
        "order_by": ("recommendation_revision_id",),
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
        "order_by": ("decision_id",),
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
        ),
        "order_by": ("relationship_id",),
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
        "order_by": ("trail_revision_id",),
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
        "order_by": ("trail_revision_id", "ordinal"),
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
        "order_by": ("manifest_id",),
    },
    "Omission_Entries": {
        # ``resolved_at`` is the one-shot field — exclude it from the
        # immutability snapshot so a permitted one-shot transition
        # doesn't trip the byte-equivalence check (Property 12's
        # explicit carve-out for ``Omission_Entries.resolved_at``).
        "columns": (
            "omission_entry_id",
            "manifest_id",
            "excluded_source_id",
            "excluded_source_revision_id",
            "category",
            "rationale",
            "authoring_party_id",
            "recorded_at",
        ),
        "order_by": ("omission_entry_id",),
    },
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
        "order_by": ("append_sequence",),
    },
    # Role_Assignments is not in the design's immutable-tables list
    # because its ``revoked_at`` column is one-shot, but every OTHER
    # column on the table is append-only and Property 12 requires
    # those columns to remain byte-equivalent across attacks. The
    # snapshot deliberately excludes ``revoked_at`` so a permitted
    # first-time NULL-to-value transition (which the attacker is
    # forbidden from issuing by ``_IMMUTABLE_UPDATE_COLUMNS``) would
    # also be tolerated here if any other code path made it.
    "Role_Assignments": {
        "columns": (
            "role_assignment_id",
            "party_id",
            "role_name",
            "scope",
            "authorities_granted",
            "effective_start",
            "effective_end",
            "assigning_authority_id",
            "recorded_at",
        ),
        "order_by": ("role_assignment_id",),
    },
}


# ---------------------------------------------------------------------------
# Per-table attack alphabets.
#
# ``pk_columns`` names the columns the attacker must supply in the
# WHERE clause to target one row. ``update_columns`` is the allow-list
# the Hypothesis attacker draws ``column_to_update`` from. The lists
# exclude one-shot columns (``Role_Assignments.revoked_at``,
# ``Omission_Entries.resolved_at``) so the attacker cannot
# accidentally issue a permitted NULL-to-value transition. The
# ``current_revision_id`` mutable pointer on ``Trails`` is also
# excluded — ``Trails`` is not in the immutable list, but its row
# would not satisfy byte-equivalence if mutated.
# ---------------------------------------------------------------------------


_IMMUTABLE_UPDATE_COLUMNS: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "Document_Revisions": {
        "pk_columns": ("revision_id",),
        "update_columns": (
            "revision_id",
            "resource_id",
            "content_digest_sha256",
            "recorded_at",
            "change_description",
        ),
    },
    "Region_Occurrences": {
        "pk_columns": ("region_id", "document_revision_id"),
        "update_columns": (
            "start_offset_bytes",
            "end_offset_bytes",
            "span_content_digest_sha256",
            "recorded_at",
        ),
    },
    "Finding_Revisions": {
        "pk_columns": ("finding_revision_id",),
        "update_columns": (
            "finding_revision_id",
            "statement",
            "is_hypothesis",
            "assumptions_json",
            "recorded_at",
        ),
    },
    "Recommendation_Revisions": {
        "pk_columns": ("recommendation_revision_id",),
        "update_columns": (
            "recommendation_revision_id",
            "rationale",
            "assumptions_json",
            "recorded_at",
        ),
    },
    "Decisions": {
        "pk_columns": ("decision_id",),
        "update_columns": (
            "decision_id",
            "outcome",
            "rationale",
            "applicable_scope",
            "recorded_at",
        ),
    },
    "Relationships": {
        "pk_columns": ("relationship_id",),
        "update_columns": (
            "relationship_id",
            "relationship_type",
            "target_id",
            "recorded_at",
        ),
    },
    "Trail_Revisions": {
        "pk_columns": ("trail_revision_id",),
        "update_columns": (
            "trail_revision_id",
            "purpose",
            "audience_id",
            "ordering_rationale",
            "recorded_at",
        ),
    },
    "Trail_Steps": {
        "pk_columns": ("trail_step_id",),
        "update_columns": (
            "trail_step_id",
            "ordinal",
            "selection_mode",
            "target_id",
            "annotation",
        ),
    },
    "Provenance_Manifests": {
        "pk_columns": ("manifest_id",),
        "update_columns": (
            "manifest_id",
            "subject_id",
            "included_sources_json",
            "is_complete",
            "recorded_at",
        ),
    },
    "Omission_Entries": {
        # ``resolved_at`` is intentionally excluded from
        # ``update_columns`` — the one-shot trigger allows the
        # NULL-to-value transition the first time, and the attacker
        # exercising that transition would (correctly) succeed,
        # invalidating the byte-equivalence assertion. The dedicated
        # one-shot tests below exercise the second-transition
        # rejection path.
        "pk_columns": ("omission_entry_id",),
        "update_columns": (
            "omission_entry_id",
            "category",
            "rationale",
            "excluded_source_id",
            "recorded_at",
        ),
    },
    "Audit_Records": {
        "pk_columns": ("audit_record_id",),
        "update_columns": (
            "audit_record_id",
            "append_sequence",
            "action_type",
            "outcome",
            "reason_code",
            "recorded_at",
        ),
    },
    "Role_Assignments": {
        # ``revoked_at`` is intentionally excluded — that column is
        # one-shot and the trigger allows the first transition. The
        # dedicated one-shot tests below exercise the
        # second-transition rejection path.
        "pk_columns": ("role_assignment_id",),
        "update_columns": (
            "role_assignment_id",
            "party_id",
            "role_name",
            "scope",
            "authorities_granted",
            "effective_start",
            "recorded_at",
        ),
    },
}


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case identifiers, audit rows, and seeded
# pipelines cannot leak between cases (design §"Testing Strategy" —
# per-case database isolation). The :class:`tempfile.TemporaryDirectory`
# context inside the test body owns the per-case directory; Hypothesis
# disallows function-scoped pytest fixtures for per-case state because
# they would not reset between generated inputs.
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


def _seed_party(conn) -> None:
    """Insert the test Party row required by every Party FK in the seed."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Property 12 Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Pipeline seeding.
#
# A fresh Source Document → Document Revision → Region Occurrence →
# Finding → Recommendation → Decision → Trail Revision + 5 Trail
# Steps → supplementary Provenance Manifest with one Omission Entry
# is produced per case so every immutable table holds at least one
# row. Two additional rows are seeded by direct INSERT to give the
# one-shot triggers concrete violation targets:
#
# - A second ``Role_Assignments`` row with ``revoked_at`` already
#   set, so the post-attack one-shot assertion can attempt to
#   overwrite or clear that value.
# - A second ``Omission_Entries`` row with ``resolved_at`` already
#   set, for the same reason.
# ---------------------------------------------------------------------------


def _seed_pipeline(
    engine: Engine,
    *,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
    trail_service: TrailService,
    authorization_service: AuthorizationService,
    manifest_writer: ProvenanceManifestWriter,
) -> dict[str, str]:
    """Seed one full pipeline and return the identifiers each table cites.

    Returns a dict carrying the identifiers needed by both the attack
    loop (which targets rows by PK) and the post-attack one-shot
    assertions (which need to address the pre-set ``revoked_at`` /
    ``resolved_at`` rows specifically).
    """
    with engine.begin() as conn:
        _seed_party(conn)

        # --- Role Assignments ---------------------------------------
        # First assignment exercises the AuthorizationService write
        # path so the consequential audit row lands too. Its
        # ``revoked_at`` is NULL — Property 12 does not test the
        # permitted NULL-to-value transition (covered by the dedicated
        # ``test_revoked_at_one_shot`` unit suite); the attacker only
        # tries to mutate columns OTHER than ``revoked_at`` on this
        # row.
        role_assignment_id = authorization_service.assign_role(
            conn,
            AssignRoleRequest(
                party_id=_PARTY_ID,
                role_name="Analyst",
                scope=_SCOPE,
                authorities_granted=("modify",),
                effective_start=_FIXED_NOW,
                assigning_authority_id=_PARTY_ID,
            ),
        )

        # Second assignment is INSERT-ed directly with
        # ``revoked_at`` already set so the dedicated one-shot
        # assertions can attempt a second transition (rejected) and
        # the byte-equivalence check still holds.
        pre_revoked_role_assignment_id = (
            "00000000-0000-7000-8000-00000000ra02"
        )
        conn.execute(
            text(
                """
                INSERT INTO Role_Assignments (
                    role_assignment_id, party_id, role_name, scope,
                    authorities_granted, effective_start, effective_end,
                    revoked_at, assigning_authority_id, recorded_at
                ) VALUES (
                    :raid, :pid, 'Analyst', :scope,
                    '["modify"]', :start, NULL,
                    :revoked, :pid, :recorded
                )
                """
            ),
            {
                "raid": pre_revoked_role_assignment_id,
                "pid": _PARTY_ID,
                "scope": _SCOPE,
                "start": _FIXED_NOW_ISO,
                "revoked": _PRE_REVOKED_AT_ISO,
                "recorded": _FIXED_NOW_ISO,
            },
        )

        # --- Source Document + Document Revision --------------------
        doc = evidence_repository.create_document(
            conn,
            content_bytes=b"Property 12 walking slice content.",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )

        # --- Region Occurrence --------------------------------------
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=8,
            contributing_party_id=_PARTY_ID,
        )

        # --- Finding (+ Supports Relationship + Finding_Revision) ---
        finding = knowledge_service.create_finding(
            conn,
            statement="Evidence-backed Property 12 claim.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                ),
            ],
        )

        # --- Recommendation (+ Derived From Relationship) -----------
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Property 12 recommendation rationale.",
        )

        # --- Decision (+ Addresses Relationship + inline Manifest) --
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Approve based on the recommendation.",
            deciding_party_id=_PARTY_ID,
            authority_basis=AuthorityBasisRef(
                type="role-grant-id", id=_AUTHORITY_BASIS_ID
            ),
            applicable_scope=_SCOPE,
        )

        # --- Trail (+ Trail_Revision + 5 Trail_Steps) ---------------
        trail = trail_service.create_trail(
            conn,
            purpose="Property 12 trail.",
            audience_id="property-12-audience",
            ordering_rationale="Linear pipeline order.",
            steps=[
                TrailStepInput(
                    ordinal=1,
                    target_kind="document_revision",
                    target_id=doc.resource_id,
                    target_revision_id=doc.revision_id,
                ),
                TrailStepInput(
                    ordinal=2,
                    target_kind="region_occurrence",
                    target_id=doc.revision_id,
                    region_id=region.region_id,
                ),
                TrailStepInput(
                    ordinal=3,
                    target_kind="finding_revision",
                    target_id=finding.finding_id,
                    target_revision_id=finding.finding_revision_id,
                ),
                TrailStepInput(
                    ordinal=4,
                    target_kind="recommendation_revision",
                    target_id=recommendation.recommendation_id,
                    target_revision_id=(
                        recommendation.recommendation_revision_id
                    ),
                ),
                TrailStepInput(
                    ordinal=5,
                    target_kind="decision",
                    target_id=decision.decision_id,
                ),
            ],
            authoring_party_id=_PARTY_ID,
        )

        # --- Supplementary Provenance Manifest with one Omission ---
        # The decision's inline manifest is already in the DB; this
        # extra manifest provides an Omission_Entries row whose
        # ``resolved_at`` stays NULL so the attacker can attempt to
        # mutate every non-one-shot column on it.
        manifest_result = manifest_writer.write_manifest(
            conn,
            subject_kind="trail_revision",
            subject_id=trail.trail_id,
            subject_revision_id=trail.trail_revision_id,
            authoring_party_id=_PARTY_ID,
            included_sources=[
                IncludedSource(
                    kind="document_revision",
                    resource_id=doc.resource_id,
                    revision_id=doc.revision_id,
                    recorded_at=_FIXED_NOW,
                ),
            ],
            omissions=[
                OmissionEntry(
                    excluded_source_id=doc.resource_id,
                    excluded_source_revision_id=None,
                    category="intentional",
                    rationale="Out of scope for Property 12 manifest.",
                ),
            ],
        )

        # --- Second Omission Entry with resolved_at already set ----
        pre_resolved_omission_entry_id = (
            "00000000-0000-7000-8000-00000000oe02"
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
                    :oeid, :mid, :sid, NULL,
                    'unresolved', 'Pre-set resolved_at one-shot fixture.',
                    :pid, :recorded, :resolved
                )
                """
            ),
            {
                "oeid": pre_resolved_omission_entry_id,
                "mid": manifest_result.manifest_id,
                "sid": doc.resource_id,
                "pid": _PARTY_ID,
                "recorded": _FIXED_NOW_ISO,
                "resolved": _PRE_RESOLVED_AT_ISO,
            },
        )

    return {
        "role_assignment_id": role_assignment_id,
        "pre_revoked_role_assignment_id": pre_revoked_role_assignment_id,
        "pre_resolved_omission_entry_id": pre_resolved_omission_entry_id,
    }


# ---------------------------------------------------------------------------
# Snapshot helper.
#
# Reads every row of every immutable table in stable PK order and
# returns the rows as a hashable bundle so the byte-equivalence
# comparison reduces to one ``==`` per table. Storing the full row
# tuple (rather than a hex digest) keeps a failing assertion's diff
# informative — Hypothesis prints the differing tuples directly.
# ---------------------------------------------------------------------------


def _snapshot(engine: Engine) -> dict[str, tuple[tuple[Any, ...], ...]]:
    """Snapshot every immutable table as ``{table_name: tuple_of_rows}``."""
    out: dict[str, tuple[tuple[Any, ...], ...]] = {}
    with engine.connect() as conn:
        for table_name, spec in _TABLE_SPECS.items():
            columns = ", ".join(spec["columns"])
            order_by = ", ".join(spec["order_by"])
            rows = conn.execute(
                text(f"SELECT {columns} FROM {table_name} ORDER BY {order_by}")
            ).all()
            # Coerce ``memoryview`` BLOBs (returned by some SQLite
            # drivers) to ``bytes`` so two snapshots taken from the
            # same row compare equal regardless of driver version.
            normalized = tuple(
                tuple(bytes(v) if isinstance(v, memoryview) else v for v in row)
                for row in rows
            )
            out[table_name] = normalized
    return out


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# Each attack draws a (table, kind, column, new_value) tuple. ``kind``
# is ``'update'`` or ``'delete'``; ``column`` is drawn from the table's
# allow-list when ``kind == 'update'``; ``new_value`` is drawn from a
# small bag of representative values (a benign string, an empty
# string, ``NULL``, an integer, a different identifier). The strategy
# values are deliberately drawn flat (not conditionally on ``kind``)
# so Hypothesis's shrinker can pick a smaller value when ``kind``
# happens to be ``'delete'`` without re-drawing the column.
# ---------------------------------------------------------------------------


_TABLE_NAMES: Final[tuple[str, ...]] = tuple(_IMMUTABLE_UPDATE_COLUMNS.keys())

# Candidate UPDATE values. Drawn from a closed bag so the strategy
# stays small and Hypothesis can enumerate the (kind, table, column,
# value) cube within the 50-case budget. The values cover the four
# representative shapes the schema columns accept (string, blank,
# NULL, integer).
_UPDATE_VALUE_BAG: Final[tuple[Any, ...]] = (
    "00000000-0000-7000-8000-0000000000ff",  # plausible-looking id
    "tampered",                                # short text
    "",                                        # blank text
    None,                                      # SQL NULL
    9999,                                      # integer
)


@st.composite
def _attack_strategy(draw) -> dict[str, Any]:
    """Draw one attack tuple: ``(table, kind, column?, new_value?)``."""
    table = draw(st.sampled_from(_TABLE_NAMES))
    kind: Literal["update", "delete"] = draw(
        st.sampled_from(("update", "delete"))
    )
    column = draw(
        st.sampled_from(_IMMUTABLE_UPDATE_COLUMNS[table]["update_columns"])
    )
    new_value = draw(st.sampled_from(_UPDATE_VALUE_BAG))
    return {
        "table": table,
        "kind": kind,
        "column": column,
        "new_value": new_value,
    }


# Each scenario is 1..30 attacks; ``min_size=1`` guarantees at least
# one attempt per case (a case with zero attacks would leave the
# rejection assertion vacuously satisfied and waste Hypothesis budget).
_scenario_strategy = st.lists(_attack_strategy(), min_size=1, max_size=30)


# ---------------------------------------------------------------------------
# Attack executor.
#
# For each attack tuple, fetch one target row's PK values from the
# table, build the UPDATE or DELETE statement, execute it inside a
# fresh ``engine.begin()`` block so a successful (incorrectly
# permitted) statement would commit and immediately break the
# byte-equivalence post-condition, and assert IntegrityError was
# raised. The fresh transaction per attack also matches the "any two
# observation points" wording in Property 12: each attack is one
# observation point.
# ---------------------------------------------------------------------------


def _first_pk(engine: Engine, *, table: str) -> Optional[dict[str, Any]]:
    """Return the PK column values of the first row in ``table``, or ``None``."""
    spec = _IMMUTABLE_UPDATE_COLUMNS[table]
    pk_columns = spec["pk_columns"]
    pk_select = ", ".join(pk_columns)
    order_by = ", ".join(pk_columns)
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {pk_select} FROM {table} ORDER BY {order_by} LIMIT 1")
        ).first()
    if row is None:
        return None
    return {col: row[i] for i, col in enumerate(pk_columns)}


def _apply_attack(engine: Engine, attack: dict[str, Any]) -> None:
    """Execute one attack against the engine and assert it was rejected."""
    table = attack["table"]
    kind = attack["kind"]
    pk_values = _first_pk(engine, table=table)
    if pk_values is None:
        # Seeded pipeline guarantees at least one row in every
        # immutable table, so a ``None`` here would indicate a seed
        # regression. Fail loudly rather than silently skipping the
        # attack.
        raise AssertionError(
            f"Seed regression: no row found in {table!r} for Property 12 "
            f"attack {attack!r}. The pipeline seed must insert at least "
            f"one row per immutable table."
        )

    where_clause = " AND ".join(
        f"{col} = :pk_{col}" for col in pk_values.keys()
    )
    params: dict[str, Any] = {
        f"pk_{col}": val for col, val in pk_values.items()
    }

    if kind == "delete":
        statement = f"DELETE FROM {table} WHERE {where_clause}"
    else:
        column = attack["column"]
        params["new_value"] = attack["new_value"]
        statement = (
            f"UPDATE {table} SET {column} = :new_value WHERE {where_clause}"
        )

    raised = False
    try:
        with engine.begin() as conn:
            conn.execute(text(statement), params)
    except IntegrityError:
        # Expected — the append-only / one-shot trigger fired.
        raised = True
    except Exception as exc:  # pragma: no cover - defensive
        # Any other exception type is a regression: the spec says
        # triggers ABORT, which SQLAlchemy surfaces as
        # IntegrityError. A different exception class would silently
        # let the byte-equivalence assertion still pass while hiding
        # a trigger regression.
        raise AssertionError(
            f"Attack {attack!r} raised {type(exc).__name__} instead of "
            f"sqlalchemy.exc.IntegrityError; trigger contract regressed."
        ) from exc

    assert raised, (
        f"Attack {attack!r} was NOT rejected — the immutability trigger "
        f"on {table!r} failed to fire. Property 12 requires every "
        "UPDATE/DELETE on an immutable table to raise IntegrityError "
        "(Requirements 2.4, 6.6, 13.3, 13.4)."
    )


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 12: Append-only immutability
@given(scenario=_scenario_strategy)
@settings(
    max_examples=50,
    deadline=5000,
    # Per-case setup runs a full pipeline (Document Revision through
    # Decision through Trail through supplementary Manifest) plus the
    # attack loop and the post-attack snapshot diff. The 5 s
    # deadline accommodates slower CI hosts; the data-generation
    # health check is suppressed because the per-case work is heavier
    # than a pure in-memory property test by design.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_append_only_immutability(scenario: list[dict[str, Any]]) -> None:
    """Every UPDATE/DELETE on an immutable table is rejected, every
    previously-inserted row remains byte-equivalent, and the
    Audit_Records append_sequence is monotonically non-decreasing by
    recorded_at."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop12_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # Fresh services per case so :class:`IdentityService`
            # in-memory state cannot bleed across cases. The
            # :class:`FixedClock` keeps every recorded timestamp
            # deterministic for Hypothesis shrinks.
            clock = FixedClock(_FIXED_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
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
            trail_service = TrailService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            authorization_service = AuthorizationService(
                clock=clock,
                audit_log=audit_log,
                identity_service=identity_service,
            )
            manifest_writer = ProvenanceManifestWriter(
                clock=clock,
                identity_service=identity_service,
            )

            seed_ids = _seed_pipeline(
                engine,
                evidence_repository=evidence_repository,
                knowledge_service=knowledge_service,
                trail_service=trail_service,
                authorization_service=authorization_service,
                manifest_writer=manifest_writer,
            )

            # --- Phase 1: snapshot ---------------------------------
            pre_snapshot = _snapshot(engine)

            # Sanity-check every immutable table has at least one
            # row — a seed regression that left a table empty would
            # make the attack loop silently no-op against that table.
            for table_name in _TABLE_NAMES:
                # ``Role_Assignments`` has *two* rows; every other
                # table has at least one. The check is uniform:
                # at-least-one is the precondition Property 12
                # quantifies over ("previously inserted rows").
                assert len(pre_snapshot.get(table_name, ())) >= 1, (
                    f"Seed regression: {table_name!r} has zero rows "
                    "after pipeline seeding."
                )

            # --- Phase 2: attack loop ------------------------------
            for attack in scenario:
                _apply_attack(engine, attack)

            # --- Phase 3: re-snapshot and byte-equivalence diff ---
            post_snapshot = _snapshot(engine)
            for table_name in _TABLE_SPECS.keys():
                assert post_snapshot[table_name] == pre_snapshot[table_name], (
                    f"Byte-equivalence violated on {table_name!r}: "
                    f"pre={pre_snapshot[table_name]!r}, "
                    f"post={post_snapshot[table_name]!r}. Property 12 "
                    "requires every previously-inserted row to remain "
                    "byte-equivalent across the attack sequence."
                )

            # --- Phase 4: audit append_sequence monotonicity -------
            # Requirement 13.5: append_sequence must be monotonically
            # non-decreasing when rows are ordered by recorded_at.
            # The schema also enforces a UNIQUE constraint on
            # append_sequence, so the assertion here is strict
            # monotonic increase under the (recorded_at, append_sequence)
            # ordering — but Property 12 only requires non-decreasing.
            with engine.connect() as conn:
                audit_rows = conn.execute(
                    text(
                        """
                        SELECT recorded_at, append_sequence
                          FROM Audit_Records
                         ORDER BY recorded_at, append_sequence
                        """
                    )
                ).all()
            prior_sequence: Optional[int] = None
            for recorded_at, append_sequence in audit_rows:
                if prior_sequence is not None:
                    assert append_sequence >= prior_sequence, (
                        f"Audit_Records.append_sequence is not "
                        f"monotonically non-decreasing under "
                        f"ORDER BY recorded_at: previous="
                        f"{prior_sequence!r}, current="
                        f"{append_sequence!r} at recorded_at="
                        f"{recorded_at!r}. Property 12 / Requirement "
                        "13.5 require non-decreasing ordering."
                    )
                prior_sequence = append_sequence

            # --- Phase 5: one-shot trigger spot checks -------------
            # The Hypothesis attack strategy excludes the one-shot
            # columns (``Role_Assignments.revoked_at``,
            # ``Omission_Entries.resolved_at``) so a permitted
            # NULL-to-value transition cannot accidentally satisfy
            # the byte-equivalence guarantee. The two deterministic
            # assertions below cover the *second* transition
            # specifically — once a one-shot column is set, the
            # trigger rejects any further write.
            #
            # The seed pre-set ``revoked_at`` on
            # ``pre_revoked_role_assignment_id`` and ``resolved_at``
            # on ``pre_resolved_omission_entry_id``, so these
            # statements exercise the value-to-value (and
            # value-to-NULL) rejection branches of the one-shot
            # triggers.
            for new_revoked in (_PRE_REVOKED_AT_ISO + ".tampered", None):
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                                UPDATE Role_Assignments
                                   SET revoked_at = :rv
                                 WHERE role_assignment_id = :raid
                                """
                            ),
                            {
                                "rv": new_revoked,
                                "raid": seed_ids[
                                    "pre_revoked_role_assignment_id"
                                ],
                            },
                        )
                    raised = False
                except IntegrityError:
                    raised = True
                assert raised, (
                    "Role_Assignments.revoked_at second-transition "
                    f"(new_value={new_revoked!r}) was NOT rejected; "
                    "the one-shot trigger failed to fire."
                )

            for new_resolved in (_PRE_RESOLVED_AT_ISO + ".tampered", None):
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                """
                                UPDATE Omission_Entries
                                   SET resolved_at = :rv
                                 WHERE omission_entry_id = :oeid
                                """
                            ),
                            {
                                "rv": new_resolved,
                                "oeid": seed_ids[
                                    "pre_resolved_omission_entry_id"
                                ],
                            },
                        )
                    raised = False
                except IntegrityError:
                    raised = True
                assert raised, (
                    "Omission_Entries.resolved_at second-transition "
                    f"(new_value={new_resolved!r}) was NOT rejected; "
                    "the one-shot trigger failed to fire."
                )
        finally:
            engine.dispose()
