# Feature: third-walking-slice, Property 43: Produced-Deliverable vs Source-Evidence disjointness
"""Property 43 — Produced-Deliverable vs Source-Evidence disjointness (task 16.13).

**Property 43: Produced-Deliverable vs Source-Evidence disjointness**

*For all* produced Deliverable Resource Identities created by the
:class:`~walking_slice.deliverables.repository.DeliverableRepositoryService`
in any test session, the produced Deliverable Resource Identity is not
also identifying any Source Evidence Document Resource recorded by the
Slice 1 :class:`~walking_slice.evidence.EvidenceRepository`; and
conversely, no Source Evidence Document Resource Identity is reissued
as a produced Deliverable Resource Identity. Every produced
Deliverable Revision carries ``role_marker = 'generated_output'``; no
Source Evidence Document Revision carries this column. Rename and
relocate operations on a produced Deliverable Resource preserve its
Resource Identity and every existing Revision Identity unchanged.

**Validates: Requirements 22.2, 22.3, 26.3, 35.8, 41.13**

Strategy
========

Each Hypothesis case draws a list of 1..6 *interleaved* operations.
Each operation is one of two action kinds:

- ``"source_document"`` — drives
  :meth:`~walking_slice.evidence.EvidenceRepository.create_document`
  to mint one Slice 1 Source Evidence Document Resource and its first
  Document Revision. The :class:`Identifier_Registry` rows inserted
  by Slice 1's :class:`~walking_slice.identity.IdentityService` carry
  ``resource_kind = NULL`` because the additive
  ``Identifier_Registry.resource_kind`` column (Slice 2 AD-WS-19)
  was not set by Slice 1 services — the pre-existing Slice 1 rows
  remain NULL per the Slice 2 design.
- ``"produced_deliverable"`` — drives
  :meth:`~walking_slice.deliverables.repository.DeliverableRepositoryService.create_produced_deliverable`
  to mint one Slice 3 produced Deliverable Resource and its first
  produced Deliverable Revision. The
  :class:`~walking_slice.execution._helpers._record_execution_artifact`
  helper tags each Slice 3 :class:`Identifier_Registry` row with
  ``resource_kind = 'deliverable_resource'`` (Resource header) or
  ``resource_kind = 'deliverable_revision'`` (first Revision) per
  AD-WS-28 / Requirement 26.3.

Operations are drawn in arbitrary order so the property is exercised
under interleaved sequences (Slice-1 row → Slice-3 row → Slice-1 row,
etc.) — a regression that depended on a particular ordering of inserts
would falsify the property.

Per case the test:

1. Spins up a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case
   ``Identifier_Registry``, ``Source_Documents``, ``Document_Revisions``,
   ``Deliverable_Resources``, and ``Deliverable_Revisions`` rows
   cannot leak between cases (design §"Testing Strategy" — "Each
   property and example test gets a fresh SQLite database"). The
   Slice 1, Slice 2, Slice 3 execution, and Slice 3 deliverable
   schemas are all installed so the full disjointness surface is
   exercised end-to-end.
2. Seeds four Party rows (acting Contributor, the Work Assignment's
   assignee — same Party as the actor so AD-WS-29 binds — plus an
   assignment-authority Party, plus a Source-Document-contributing
   Party reused as the FK target for the Slice 1 writes). Grants the
   actor wildcard ``contribute`` authority so every produced-
   Deliverable creation in the generated sequence permits. Seeds the
   Slice 2 prerequisite chain (Project → Activity Plan → approved
   Plan Revision) and one ``Work_Assignment_Records`` row whose
   ``assignee_party_id`` equals the actor so the AD-WS-29 second
   stage passes.
3. Executes every operation in the generated sequence through its
   public service surface inside an :meth:`Engine.begin` transaction,
   collecting the persisted ``resource_id`` / ``deliverable_id`` /
   ``deliverable_revision_id`` values for the post-condition checks.
4. Snapshots ``(deliverable_id, sorted tuple of revision_ids)`` for
   every produced Deliverable Resource created in step 3. The
   snapshot is the "before-rename" view of identity that the rename /
   relocate clause must preserve.
5. Attempts an UPDATE on ``Deliverable_Resources`` for every
   created produced Deliverable Resource. The AD-WS-27 UPDATE
   rejection trigger fires unconditionally (Slice 3 produced
   Deliverable Resources are insert-only, design §"Persistence
   Invariants Summary" rule 9 / Requirement 22.3 / Requirement
   26.4), so the attempt raises
   :class:`sqlalchemy.exc.IntegrityError`. The trigger-driven
   rollback is the durable persistence-layer guarantee that "rename
   and relocate operations preserve Resource Identity and every
   existing Revision Identity unchanged" — there is no path through
   the slice that can re-key a produced Deliverable.
6. Re-snapshots ``(deliverable_id, sorted tuple of revision_ids)``
   for every produced Deliverable Resource and asserts the snapshot
   matches step 4 byte-equivalently. The same property holds when
   no rename / relocate API exists at all (the slice intentionally
   omits one — Requirement 22.3); attempting and failing to UPDATE
   is the strongest available exercise of the "rename / relocate
   preserves identity" clause.
7. Asserts the four disjointness invariants Property 43 names:

   (a) The set of produced Deliverable Resource Identities
       (``Deliverable_Resources.deliverable_id``) is disjoint from
       the set of Source Evidence Document Resource Identities
       (``Source_Documents.resource_id``).
   (b) The set of ``Identifier_Registry`` rows tagged
       ``resource_kind = 'deliverable_resource'`` is exactly the
       produced-Deliverable Resource identifier set and is disjoint
       from every ``Identifier_Registry`` row that names a Source
       Evidence Document Resource (the Slice 1 rows registered
       with ``resource_kind IS NULL``). The
       ``Identifier_Registry.resource_kind`` column is the canonical
       discriminator the property names explicitly.
   (c) Every ``Deliverable_Revisions`` row carries
       ``role_marker = 'generated_output'`` (Requirement 26.2 /
       Persistence Invariants Summary rule 9). The schema-level
       CHECK constraint already enforces this at INSERT time; the
       property surfaces a fresh SELECT so a future regression that
       weakened the CHECK (or that swapped in a duplicate row via
       some other path) would falsify the property immediately.
   (d) The Slice 1 ``Document_Revisions`` table has no
       ``role_marker`` column at all — a SQLite
       ``PRAGMA table_info(Document_Revisions)`` returns the column
       list and the assertion is that ``'role_marker'`` is *not*
       among them. The combination of (c) and (d) is the structural
       discriminator that distinguishes a produced Deliverable
       Revision from a Source Evidence Document Revision
       (Requirement 26.3, Requirement 41 §13).

Requirement coverage notes
==========================

- **22.2** — produced Deliverable Resource Identity and produced
  Deliverable Revision Identity are persisted as two distinct
  values; the property exercises this implicitly by reading
  ``deliverable_id`` and ``deliverable_revision_id`` from separate
  columns and asserting the rename-preservation clause over the
  full ``(deliverable_id, sorted revision ids)`` tuple.
- **22.3** — produced Deliverable Resource Identity survives rename
  / relocation; the rename-attempt-and-snapshot loop in steps 4..6
  is the exercise of this clause. Because Slice 3 has no rename
  API and the AD-WS-27 trigger blocks any UPDATE, the strongest
  observable property is "every persisted Resource Identity is the
  identity that was minted at INSERT time and is preserved through
  any subsequent attempt to mutate the row".
- **26.3** — produced Deliverable Resource Identity is held disjoint
  from Source Evidence Document Resource Identity; the assertion in
  step 7(a) and step 7(b) is the direct test of this clause. The
  property's "verified via ``Identifier_Registry.resource_kind``"
  qualifier names the additive AD-WS-19 column as the inspectable
  discriminator.
- **35.8** — every produced Deliverable Revision carries
  ``role_marker = 'generated_output'`` so the Provenance_Navigator
  can surface the marker without a second lookup; step 7(c)
  asserts the column value across every persisted Revision.
- **41.13** — the slice-wide invariant Property 43 codifies; every
  step of this test contributes to its end-to-end exercise across
  the full Slice 1 + Slice 3 surface.

Hypothesis profile
==================

``@settings(max_examples=100, deadline=2000)`` per Requirement 41.15
/ AD-WS-13. The ``too_slow`` health-check is suppressed because
per-case setup spins up a fresh SQLite file with four schema installs
and several seeded prerequisite rows.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

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
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.evidence import EvidenceRepository
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the four Parties referenced by FK constraints in the
# Slice 1 + Slice 3 surface this property exercises.
#
# - ``_ACTOR_PARTY_ID`` is both the requesting Contributor for every
#   ``create.produced_deliverable`` call and the Work Assignment's
#   ``assignee_party_id`` so the AD-WS-29 second-stage assignee binding
#   passes by construction.
# - ``_ASSIGNMENT_AUTHORITY_PARTY_ID`` is the Work Assignment's
#   ``assignment_authority_party_id``. Distinct from the actor so the
#   schema-level CHECK constraint
#   ``assignee_party_id != assignment_authority_party_id`` (Requirement
#   23.5) is honored.
# - ``_ASSIGNING_AUTHORITY_PARTY_ID`` is the actor on the role-assignment
#   audit row.
# - ``_CONTRIBUTING_PARTY_ID`` is the FK target for every Slice 1
#   ``Document_Revisions.contributing_party_id`` value (passed through
#   :meth:`EvidenceRepository.create_document`'s
#   ``contributing_party_id`` kwarg).
# ---------------------------------------------------------------------------


_ACTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNMENT_AUTHORITY_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_ASSIGNING_AUTHORITY_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a3"
_CONTRIBUTING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a4"


# Slice 2 prerequisite identifiers (seeded directly via INSERT — the
# property is about Slice 1 / Slice 3 disjointness, so the Slice 2
# planning prerequisites are wiring rather than subject matter).
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_APPROVED_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"
_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-0000000000b1"


# The pinned :class:`FixedClock` instant. Role-assignment effective
# windows generously bracket this instant so a Hypothesis-shrunken
# case never misses on timing.
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_FIXED_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"


# Applicable scope shared by every authority grant and every Slice 2
# row seeded below. Constant per test so the wildcard ``contribute``
# grant covers every produced-Deliverable creation in the generated
# sequence.
_SCOPE: Final[str] = "property-43/scope"


# Slice 1 ``Source_Documents.authority`` enumeration value used for
# every drawn ``"source_document"`` operation. The property is not
# parameterized over the authority enumeration (Property 43 is about
# identifier-set disjointness, not the authority semantics); pinning
# one value keeps the case-level setup tight.
_SOURCE_DOCUMENT_AUTHORITY: Final[str] = "authoritative"


# The seven-value content-type enumeration accepted by Requirement
# 26.1 / 26.5 on every produced Deliverable.
_DELIVERABLE_CONTENT_TYPES: Final[tuple[str, ...]] = (
    "text/markdown",
    "text/plain",
    "application/pdf",
    "application/json",
    "image/png",
    "image/svg+xml",
    "application/octet-stream",
)


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# The property draws a list of 1..6 *interleaved* operations. Each
# operation is one of two action kinds (``"source_document"`` or
# ``"produced_deliverable"``) carrying its own payload.
#
# Sizing rationale:
#
# - ``min_size=1`` so every case exercises at least one operation
#   (an empty sequence would satisfy the disjointness invariants
#   vacuously and waste the iteration).
# - ``max_size=6`` keeps per-case wall time low (each produced
#   Deliverable creation performs a separate-transaction
#   authorization evaluation plus the caller's transaction with
#   four INSERTs, so 6 ops × 4-ish INSERTs per op is a comfortable
#   ceiling under the 2000 ms deadline).
#
# The content-bytes draws are bounded to 1..64 bytes so the suite
# stays memory-light; the schema bound is 1..100 MB but Property 43
# is about identifier disjointness rather than the content-length
# branch (Property 31 already exercises the boundary cases).
# ---------------------------------------------------------------------------


_source_document_payload = st.fixed_dictionaries(
    {
        "kind": st.just("source_document"),
        "content_bytes": st.binary(min_size=1, max_size=64),
    }
)


_produced_deliverable_payload = st.fixed_dictionaries(
    {
        "kind": st.just("produced_deliverable"),
        "content_bytes": st.binary(min_size=1, max_size=64),
        "content_type": st.sampled_from(_DELIVERABLE_CONTENT_TYPES),
        # 1..32 chars from the printable ASCII range so the
        # produced-Deliverable name validator (Requirement 26.5) is
        # always satisfied without spending bytes on stress-testing the
        # name field. ``min_codepoint=33`` skips the space and control
        # characters; ``max_codepoint=126`` stays inside printable
        # ASCII.
        "produced_deliverable_name": st.text(
            alphabet=st.characters(
                min_codepoint=33, max_codepoint=126,
            ),
            min_size=1,
            max_size=32,
        ),
    }
)


_operation = st.one_of(_source_document_payload, _produced_deliverable_payload)


_operation_sequence = st.lists(_operation, min_size=1, max_size=6)


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case ``Identifier_Registry``,
# ``Source_Documents``, ``Document_Revisions``,
# ``Deliverable_Resources``, and ``Deliverable_Revisions`` rows
# cannot leak between cases (design §"Testing Strategy"). The four
# cumulative schemas are installed so the full disjointness surface
# is exercised — Slice 1 (with the additive AD-WS-19
# ``Identifier_Registry.resource_kind`` column), Slice 2 planning
# (the prerequisite chain Work Assignment needs), Slice 3 execution
# (Work Assignment + the AD-WS-28 ``resource_kind`` enumeration the
# Slice 3 helper writes), and Slice 3 deliverable (the
# ``Deliverable_Resources`` and ``Deliverable_Revisions`` tables
# this property reads byte-equivalently).
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas.

    Installs the four cumulative schemas — Slice 1, Slice 2 planning,
    Slice 3 execution, and Slice 3 deliverable — so the property
    exercises the full Slice 1 + Slice 3 identifier-disjointness
    surface. The pragmas match Slice 1 AD-WS-1.
    """
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
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Seed helpers.
#
# The helpers below seed the minimum prerequisite chain each Hypothesis
# case needs. Prerequisites are written through direct INSERT (rather
# than through the corresponding planning / execution Service) so the
# property focuses on the Slice 1 / Slice 3 disjointness assertions
# rather than re-exercising the prerequisite-creation paths.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the four Party rows referenced by FK constraints."""
    with engine.begin() as conn:
        for party_id, display in (
            (_ACTOR_PARTY_ID, "Property 43 Actor"),
            (_ASSIGNMENT_AUTHORITY_PARTY_ID, "Property 43 Assignment Authority"),
            (_ASSIGNING_AUTHORITY_PARTY_ID, "Property 43 Resource Steward"),
            (_CONTRIBUTING_PARTY_ID, "Property 43 Source Contributor"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, :ts)
                    """
                ),
                {"pid": party_id, "name": display, "ts": _FIXED_NOW_ISO},
            )


def _assign_contribute_role(
    authorization_service: AuthorizationService, engine: Engine
) -> None:
    """Grant the actor wildcard ``contribute`` authority over ``_SCOPE``.

    AD-WS-24 maps ``create.produced_deliverable`` to ``contribute``;
    the wildcard ``_SCOPE`` covers every produced-Deliverable
    creation in the generated sequence. The effective window
    generously brackets :data:`_FIXED_NOW` so a Hypothesis-shrunken
    case never misses on timing.
    """
    request = AssignRoleRequest(
        party_id=_ACTOR_PARTY_ID,
        role_name="property_43_contributor",
        scope=_SCOPE,
        authorities_granted=("contribute",),
        effective_start=_FIXED_NOW - timedelta(days=30),
        effective_end=_FIXED_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_PARTY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_planning_chain(engine: Engine) -> None:
    """Seed the Slice 2 prerequisite chain by direct INSERT.

    Inserts one ``Projects`` row, one ``Activity_Plans`` row, one
    approved ``Plan_Revisions`` row, and one ``Work_Assignment_Records``
    row whose ``assignee_party_id`` is the actor (so the AD-WS-29
    second stage on :meth:`DeliverableRepositoryService.create_produced_deliverable`
    passes by construction). The AD-WS-19 lifecycle trigger fires on
    UPDATE only, so a row with ``lifecycle_state='approved'`` may be
    inserted directly without driving the full Plan Approval
    transaction.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _FIXED_NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 43 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _FIXED_NOW_ISO,
            },
        )
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
                    'Property 43 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _FIXED_NOW_ISO,
            },
        )
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
                    'Property 43 Work Assignment rationale.',
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _WORK_ASSIGNMENT_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
                # Actor is the assignee so AD-WS-29 passes.
                "assignee": _ACTOR_PARTY_ID,
                # Distinct Party for the assignment-authority slot so
                # the schema-level CHECK on
                # ``assignee_party_id != assignment_authority_party_id``
                # (Requirement 23.5) is honored.
                "authority": _ASSIGNMENT_AUTHORITY_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _FIXED_NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Operation-execution helpers.
#
# Each helper performs one drawn operation through its public service
# surface inside a fresh :meth:`Engine.begin` transaction. The helpers
# return the persisted identifier(s) so the post-condition loop can
# assemble the disjointness sets directly from the captured values
# rather than re-issuing a SELECT.
# ---------------------------------------------------------------------------


def _run_create_source_document(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Execute one ``"source_document"`` operation; return its identities.

    Returns
    -------
    tuple[str, str]
        ``(resource_id, revision_id)`` from the persisted
        ``Source_Documents`` / ``Document_Revisions`` rows.
    """
    with engine.begin() as conn:
        result = evidence_repository.create_document(
            conn,
            content_bytes=payload["content_bytes"],
            contributing_party_id=_CONTRIBUTING_PARTY_ID,
            authority=_SOURCE_DOCUMENT_AUTHORITY,
        )
    return result.resource_id, result.revision_id


def _run_create_produced_deliverable(
    engine: Engine,
    deliverable_repository: DeliverableRepositoryService,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Execute one ``"produced_deliverable"`` operation; return its identities.

    Returns
    -------
    tuple[str, str]
        ``(deliverable_id, deliverable_revision_id)`` from the
        persisted ``Deliverable_Resources`` / ``Deliverable_Revisions``
        rows.
    """
    with engine.begin() as conn:
        result = deliverable_repository.create_produced_deliverable(
            conn,
            content_bytes=payload["content_bytes"],
            content_type=payload["content_type"],
            produced_deliverable_name=payload["produced_deliverable_name"],
            originating_work_assignment_id=_WORK_ASSIGNMENT_ID,
            authoring_party_id=_ACTOR_PARTY_ID,
            engine=engine,
        )
    return result.deliverable_id, result.deliverable_revision_id


# ---------------------------------------------------------------------------
# Identity-snapshot helper.
#
# Step 4 / step 6 of the test body snapshot
# ``(deliverable_id, sorted tuple of revision_ids)`` for every
# produced Deliverable Resource that was created in the generated
# sequence. The snapshot is the byte-equivalent identity view that the
# rename / relocate clause must preserve (Requirement 22.3 / 26.3 /
# 41.13). The helper reads from the persisted tables rather than from
# the in-memory ``deliverable_ids`` set so a regression that
# fabricated an in-memory identity without persisting it (or that
# persisted a different identity than the service returned) would
# surface as a mismatch.
# ---------------------------------------------------------------------------


def _snapshot_deliverable_identities(
    engine: Engine,
) -> dict[str, tuple[str, ...]]:
    """Return ``{deliverable_id: (sorted revision_ids…)}`` from the DB.

    Reads from the persisted tables on a fresh connection so the
    snapshot reflects the byte-equivalent on-disk view of identity
    rather than any in-memory mirror. The Revision identifiers are
    sorted so the per-Resource tuple has a deterministic
    representation (the per-Resource ordering of inserts is not part
    of the identity contract; the *set* of Revision Identities is).
    """
    snapshot: dict[str, tuple[str, ...]] = {}
    with engine.connect() as conn:
        # Pre-populate the snapshot from ``Deliverable_Resources`` so
        # Resources with zero Revisions (a state Slice 3 prevents at
        # INSERT time but would still need to be observable here for
        # a regression analysis) surface as ``deliverable_id -> ()``.
        for row in conn.execute(
            text("SELECT deliverable_id FROM Deliverable_Resources")
        ).all():
            snapshot[row[0]] = ()

        # Group every Revision Identity under its owning Resource.
        for row in conn.execute(
            text(
                "SELECT deliverable_id, deliverable_revision_id "
                "FROM Deliverable_Revisions"
            )
        ).all():
            existing = snapshot.get(row[0], ())
            snapshot[row[0]] = tuple(sorted(existing + (row[1],)))
    return snapshot


# ---------------------------------------------------------------------------
# Schema-column probe.
#
# Step 7(d) asserts that the Slice 1 ``Document_Revisions`` table has
# no ``role_marker`` column. SQLite's ``PRAGMA table_info(<table>)``
# returns one row per column; the helper turns that into a Python set
# of column names so the absence assertion is a clear set-membership
# check rather than a list-scan loop.
# ---------------------------------------------------------------------------


def _table_columns(engine: Engine, table: str) -> frozenset[str]:
    """Return the set of column names for ``table`` via PRAGMA table_info."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk).
    return frozenset(row[1] for row in rows)


# ===========================================================================
# The property test.
# ===========================================================================


# Feature: third-walking-slice, Property 43: Produced-Deliverable vs Source-Evidence disjointness
@given(operations=_operation_sequence)
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup (fresh SQLite file, four schema installs, four
    # Party rows, the Slice 2 prerequisite chain, one role assignment,
    # plus one INSERT per generated operation) is more expensive than
    # a pure in-memory property test. The setup is well under the
    # 2000 ms deadline locally but we suppress the data-generation
    # health check so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_produced_deliverable_vs_source_evidence_disjoint(
    operations: list[dict[str, Any]],
) -> None:
    """Produced-Deliverable Resource and Revision identifiers are disjoint
    from Source Evidence Document Resource and Revision identifiers,
    every produced Deliverable Revision carries
    ``role_marker = 'generated_output'``, no Source Evidence Document
    Revision carries this column, and rename / relocate operations
    preserve Resource and Revision Identity unchanged.

    **Validates: Requirements 22.2, 22.3, 26.3, 35.8, 41.13**
    """
    with tempfile.TemporaryDirectory(prefix="prop43_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            # 1. Fresh per-case services. Identity / audit / authorization
            #    state cannot bleed across cases because every service
            #    is constructed inside the case body.
            clock = FixedClock(_FIXED_NOW)
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
            deliverable_repository = DeliverableRepositoryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                # No-op sleep so the (unused) deny-path retries do not
                # spend real time on shrinking; the property does not
                # exercise the deny path.
                denial_audit_sleep=lambda _seconds: None,
            )

            # 2. Seed Parties, grant the actor wildcard contribute
            #    authority, and seed the Slice 2 prerequisite chain
            #    (Project → Activity Plan → approved Plan Revision →
            #    Work Assignment whose assignee is the actor).
            _seed_parties(engine)
            _assign_contribute_role(authorization_service, engine)
            _seed_planning_chain(engine)

            # 3. Execute every operation in the generated sequence;
            #    collect the persisted identifiers in two parallel
            #    sets so the disjointness assertions in step 7 can be
            #    a single set difference.
            source_resource_ids: set[str] = set()
            source_revision_ids: set[str] = set()
            deliverable_resource_ids: set[str] = set()
            deliverable_revision_ids: set[str] = set()

            for op in operations:
                if op["kind"] == "source_document":
                    resource_id, revision_id = _run_create_source_document(
                        engine, evidence_repository, op
                    )
                    source_resource_ids.add(resource_id)
                    source_revision_ids.add(revision_id)
                else:
                    # ``op["kind"] == "produced_deliverable"`` — the
                    # only other action kind in the strategy.
                    (
                        deliverable_id,
                        deliverable_revision_id,
                    ) = _run_create_produced_deliverable(
                        engine, deliverable_repository, op
                    )
                    deliverable_resource_ids.add(deliverable_id)
                    deliverable_revision_ids.add(deliverable_revision_id)

            # 4. Snapshot every produced Deliverable's identity state
            #    BEFORE the rename / relocate attempt. Reads the
            #    persisted tables (rather than the in-memory mirror)
            #    so step 6's comparison reflects the byte-equivalent
            #    on-disk view of identity.
            pre_rename_snapshot = _snapshot_deliverable_identities(engine)

            # 5. Attempt one rename / relocate against every persisted
            #    produced Deliverable Resource. Slice 3 has no public
            #    rename API for produced Deliverables — Requirement
            #    22.3 / 26.4 / AD-WS-27 make the Resource row
            #    insert-only — so the attempt is a direct UPDATE on
            #    ``Deliverable_Resources``. The AD-WS-27 UPDATE
            #    rejection trigger fires unconditionally and the
            #    transaction rolls back; the property's "preserve
            #    Resource Identity and every existing Revision
            #    Identity unchanged" clause is then verified by the
            #    step 6 snapshot comparison.
            for deliverable_id in deliverable_resource_ids:
                with pytest.raises(IntegrityError):
                    with engine.begin() as conn:
                        # Attempt to "rename" the produced
                        # Deliverable by changing its
                        # ``produced_deliverable_name``. AD-WS-27
                        # rejects every UPDATE on
                        # ``Deliverable_Resources``; the schema
                        # carries no ``current_location`` column on
                        # the produced-Deliverable Resource (unlike
                        # ``Source_Documents``) so relocation is
                        # structurally impossible — the closest
                        # analogue is the UPDATE attempted here.
                        conn.execute(
                            text(
                                """
                                UPDATE Deliverable_Resources
                                SET produced_deliverable_name =
                                    'Property 43 rename attempt'
                                WHERE deliverable_id = :did
                                """
                            ),
                            {"did": deliverable_id},
                        )

            # 6. Re-snapshot identity state AFTER the rename attempts
            #    and assert byte-equivalence with the pre-snapshot.
            #    The two snapshots must be equal as dictionaries —
            #    same Resource Identities, same set of Revision
            #    Identities per Resource. Requirement 22.3 / 26.3 /
            #    41.13 are falsified if any pair of snapshots diverges
            #    on a single byte.
            post_rename_snapshot = _snapshot_deliverable_identities(engine)
            assert pre_rename_snapshot == post_rename_snapshot, (
                "Rename / relocate attempt mutated produced Deliverable "
                "identity. Requirement 22.3 / 26.3 / 41.13 require the "
                "Resource Identity and every existing Revision Identity "
                "to be preserved unchanged.\n"
                f"pre = {pre_rename_snapshot!r}\n"
                f"post = {post_rename_snapshot!r}"
            )

            # 7(a). Domain-table disjointness: produced Deliverable
            #       Resource Identity ∩ Source Evidence Document
            #       Resource Identity == ∅ (Requirement 26.3).
            #
            #       Read both sets from the persisted tables so a
            #       regression that wrote a Slice 1 row under a Slice
            #       3 identifier (or vice versa) surfaces as a set
            #       intersection.
            with engine.connect() as conn:
                persisted_source_resource_ids = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT resource_id FROM Source_Documents")
                    ).all()
                }
                persisted_deliverable_resource_ids = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT deliverable_id FROM Deliverable_Resources"
                        )
                    ).all()
                }

            # Sanity rail: the persisted sets must equal the
            # service-returned sets. A divergence here would indicate
            # the service silently re-keyed an identifier after
            # creation — a regression Property 43 must catch even
            # before the disjointness check fires.
            assert persisted_source_resource_ids == source_resource_ids, (
                "Source Document Resource Identity set drifted between "
                "service return values and persisted rows.\n"
                f"service = {source_resource_ids!r}\n"
                f"persisted = {persisted_source_resource_ids!r}"
            )
            assert (
                persisted_deliverable_resource_ids == deliverable_resource_ids
            ), (
                "Produced Deliverable Resource Identity set drifted "
                "between service return values and persisted rows.\n"
                f"service = {deliverable_resource_ids!r}\n"
                f"persisted = {persisted_deliverable_resource_ids!r}"
            )

            domain_overlap = (
                persisted_deliverable_resource_ids
                & persisted_source_resource_ids
            )
            assert domain_overlap == set(), (
                "Produced Deliverable Resource Identity set and "
                "Source Evidence Document Resource Identity set must "
                "be disjoint (Requirement 26.3); overlapping "
                f"identifiers: {sorted(domain_overlap)!r}."
            )

            # 7(b). Identifier_Registry disjointness verified via the
            #       additive AD-WS-19 ``resource_kind`` column (the
            #       column the property statement names explicitly).
            #
            #       - ``resource_kind = 'deliverable_resource'`` rows
            #         must equal the persisted Slice 3 produced
            #         Deliverable Resource Identity set.
            #       - ``resource_kind = 'deliverable_revision'`` rows
            #         must equal the persisted Slice 3 produced
            #         Deliverable Revision Identity set.
            #       - Neither set may overlap any
            #         ``Identifier_Registry`` identifier that
            #         identifies a Source Evidence Document Resource
            #         or Revision (the Slice 1 rows registered with
            #         ``resource_kind IS NULL``).
            with engine.connect() as conn:
                registry_deliverable_resource_ids = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT identifier FROM Identifier_Registry "
                            "WHERE resource_kind = 'deliverable_resource'"
                        )
                    ).all()
                }
                registry_deliverable_revision_ids = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT identifier FROM Identifier_Registry "
                            "WHERE resource_kind = 'deliverable_revision'"
                        )
                    ).all()
                }
                # Slice 1's :class:`IdentityService` does not populate
                # ``resource_kind`` (the column is the Slice 2 / Slice 3
                # additive surface); pre-existing Slice 1 rows remain
                # NULL. The property's disjointness clause is
                # inspectable via the column even so: the Slice 3
                # rows carry a non-NULL value and the Slice 1 rows do
                # not, so a row that wrote a Slice 3 ``resource_kind``
                # value against a Slice 1 identifier would surface in
                # the intersection below.
                registry_slice1_resource_ids = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT identifier FROM Identifier_Registry "
                            "WHERE kind = 'resource' "
                            "AND resource_kind IS NULL"
                        )
                    ).all()
                }
                registry_slice1_revision_ids = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT identifier FROM Identifier_Registry "
                            "WHERE kind = 'revision' "
                            "AND resource_kind IS NULL"
                        )
                    ).all()
                }

            # The ``resource_kind`` tagged registry rows must equal
            # the Resource / Revision identity sets the service
            # returned; this confirms the AD-WS-28 tag is being
            # written exactly once per identifier and that the
            # property is reading the right registry slice.
            assert registry_deliverable_resource_ids == (
                deliverable_resource_ids
            ), (
                "Identifier_Registry rows tagged "
                "resource_kind='deliverable_resource' must equal the "
                "produced Deliverable Resource Identity set.\n"
                f"registry = {registry_deliverable_resource_ids!r}\n"
                f"service  = {deliverable_resource_ids!r}"
            )
            assert registry_deliverable_revision_ids == (
                deliverable_revision_ids
            ), (
                "Identifier_Registry rows tagged "
                "resource_kind='deliverable_revision' must equal the "
                "produced Deliverable Revision Identity set.\n"
                f"registry = {registry_deliverable_revision_ids!r}\n"
                f"service  = {deliverable_revision_ids!r}"
            )

            # The Slice 1 Source Evidence registry rows (resource_kind
            # IS NULL) must contain every Slice 1 Resource / Revision
            # the property created — if they do not, the Slice 1
            # repository silently failed to register an identifier
            # and a downstream Slice 3 write could land on the same
            # value. Property 43 catches that regression too.
            assert source_resource_ids.issubset(
                registry_slice1_resource_ids
            ), (
                "Every Slice 1 Source Evidence Document Resource "
                "Identity must appear in Identifier_Registry tagged "
                "with kind='resource' and resource_kind IS NULL.\n"
                f"missing = "
                f"{sorted(source_resource_ids - registry_slice1_resource_ids)!r}"
            )
            assert source_revision_ids.issubset(
                registry_slice1_revision_ids
            ), (
                "Every Slice 1 Source Evidence Document Revision "
                "Identity must appear in Identifier_Registry tagged "
                "with kind='revision' and resource_kind IS NULL.\n"
                f"missing = "
                f"{sorted(source_revision_ids - registry_slice1_revision_ids)!r}"
            )

            registry_resource_overlap = (
                registry_deliverable_resource_ids
                & registry_slice1_resource_ids
            )
            assert registry_resource_overlap == set(), (
                "Identifier_Registry identifiers tagged "
                "resource_kind='deliverable_resource' must not also "
                "appear under any Slice 1 Source Evidence Document "
                "Resource registry binding (Requirement 26.3, verified "
                "via Identifier_Registry.resource_kind). Overlapping "
                f"identifiers: {sorted(registry_resource_overlap)!r}."
            )

            registry_revision_overlap = (
                registry_deliverable_revision_ids
                & registry_slice1_revision_ids
            )
            assert registry_revision_overlap == set(), (
                "Identifier_Registry identifiers tagged "
                "resource_kind='deliverable_revision' must not also "
                "appear under any Slice 1 Source Evidence Document "
                "Revision registry binding. Overlapping identifiers: "
                f"{sorted(registry_revision_overlap)!r}."
            )

            # 7(c). Every persisted produced Deliverable Revision
            #       carries ``role_marker = 'generated_output'``
            #       (Requirement 26.2 / Persistence Invariants Summary
            #       rule 9 / Requirement 41 §13). The schema-level
            #       CHECK on ``Deliverable_Revisions.role_marker``
            #       already enforces the literal at INSERT time;
            #       this assertion re-reads the persisted column so
            #       a future regression that weakened the CHECK (or
            #       that inserted a row through some other code path)
            #       would falsify the property immediately.
            with engine.connect() as conn:
                role_marker_values = {
                    row[0]
                    for row in conn.execute(
                        text("SELECT role_marker FROM Deliverable_Revisions")
                    ).all()
                }
            if deliverable_revision_ids:
                # Only assert the value set when at least one produced
                # Deliverable was created; an all-source-document
                # sequence trivially satisfies the clause because the
                # universal quantifier is vacuously true.
                assert role_marker_values == {"generated_output"}, (
                    "Every produced Deliverable Revision must carry "
                    "role_marker='generated_output' (Requirement 26.2 "
                    "/ Persistence Invariants Summary rule 9 / "
                    "Requirement 41 §13). Distinct values observed: "
                    f"{sorted(role_marker_values)!r}."
                )

            # 7(d). The Slice 1 ``Document_Revisions`` table has no
            #       ``role_marker`` column at all. The combination of
            #       (c) and (d) is the structural discriminator that
            #       distinguishes a produced Deliverable Revision
            #       from a Source Evidence Document Revision; without
            #       (d), an additive ``role_marker`` column on Slice
            #       1's table could silently weaken the disjointness
            #       contract by letting a Source Document Revision
            #       claim the produced-Deliverable role marker.
            document_revision_columns = _table_columns(
                engine, "Document_Revisions"
            )
            assert "role_marker" not in document_revision_columns, (
                "Source Evidence Document_Revisions table must not "
                "carry a role_marker column (Requirement 26.3, "
                "Requirement 41 §13). Columns present: "
                f"{sorted(document_revision_columns)!r}."
            )

            # Defensive cross-check: ``Deliverable_Revisions`` *must*
            # carry a ``role_marker`` column — without it the
            # produced-Deliverable side of the discriminator would be
            # missing too. Property 43 surfaces a regression on either
            # side of the discriminator.
            deliverable_revision_columns = _table_columns(
                engine, "Deliverable_Revisions"
            )
            assert "role_marker" in deliverable_revision_columns, (
                "Deliverable_Revisions table must carry a role_marker "
                "column (Requirement 26.2). Columns present: "
                f"{sorted(deliverable_revision_columns)!r}."
            )
        finally:
            engine.dispose()
