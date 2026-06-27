# Feature: second-walking-slice, Property 28: Planning relationship-structure invariants
"""Property 28 — Planning relationship-structure invariants (task 16.13).

**Property 28: Planning relationship-structure invariants**

For all Plan Approval Immutable Records, exactly one ``Relationships``
row exists with ``relationship_type = 'Addresses'``,
``source_kind = 'plan_approval'``, ``source_id = plan_approval_id``,
``target_kind = 'plan_revision'``, ``target_id =
target_plan_revision_id``, and ``semantic_role IS NULL``.

For all Plan Review Revisions, exactly one ``Relationships`` row exists
with ``relationship_type = 'Relates To'``,
``source_kind = 'plan_review_revision'``,
``target_kind = 'plan_revision'``, and ``semantic_role = 'review'``.

For all Plan Revisions created with a ``predecessor_revision_id``,
exactly one ``Relationships`` row exists with
``relationship_type = 'Supersedes'``, ``source_kind = 'plan_revision'``,
and ``target_kind = 'plan_revision'``. No additional rows of these
types exist for the same source.

**Validates: Requirements 7.3, 8.3, 9.3, 20.7**

Strategy
========

Three independent Hypothesis-driven property tests, one per
relationship-structure invariant. Each test exercises exactly the
Planning_Service operation that is required to emit the row under
inspection (no direct ``INSERT`` into ``Relationships``) so a failing
case points at a real service-level regression. The three tests share
the same per-case bootstrap conventions established by the Slice 2
property tests:

- :func:`test_plan_approval_persists_exactly_one_addresses_relationship_row`
  exercises :meth:`PlanApprovalService.create_plan_approval` against a
  freshly seeded draft Plan Revision. After the create call the test
  asserts (a) exactly one ``Relationships`` row has
  ``source_id = result.plan_approval_id``, (b) that row's tuple
  ``(relationship_type, source_kind, target_kind, semantic_role,
  target_id, relationship_id)`` matches Property 28 part 1 exactly
  (``'Addresses'``, ``'plan_approval'``, ``'plan_revision'``, NULL,
  the draft Plan Revision Identity, the
  ``CreatePlanApprovalResult.addresses_relationship_id`` value), and
  (c) no second ``Relationships`` row exists in the database with
  ``relationship_type = 'Addresses'`` and
  ``source_id = result.plan_approval_id``.
- :func:`test_plan_review_persists_exactly_one_relates_to_review_relationship_row`
  exercises :meth:`PlanReviewService.create_plan_review` against a
  freshly seeded draft Plan Revision. After the create call the test
  asserts (a) exactly one ``Relationships`` row has
  ``source_id = result.plan_review_id`` and
  ``source_revision_id = result.plan_review_revision_id``, (b) that
  row's tuple ``(relationship_type, source_kind, target_kind,
  semantic_role, target_id, relationship_id)`` matches Property 28
  part 2 exactly (``'Relates To'``, ``'plan_review_revision'``,
  ``'plan_revision'``, ``'review'``, the draft Plan Revision Identity,
  the ``CreatePlanReviewResult.relates_to_relationship_id`` value), and
  (c) no second ``Relationships`` row exists with the same
  ``source_id`` / ``source_revision_id`` pair.
- :func:`test_plan_revision_with_predecessor_persists_exactly_one_supersedes_relationship_row`
  exercises :meth:`PlanRevisionService.create_plan_revision` with a
  resolvable, ``'draft'``-state predecessor against a freshly seeded
  Activity Plan + predecessor Plan Revision. After the create call
  the test asserts (a) exactly one ``Relationships`` row has
  ``source_id = result.plan_revision_id``, (b) that row's tuple
  ``(relationship_type, source_kind, target_kind, semantic_role,
  target_id, relationship_id)`` matches Property 28 part 3 exactly
  (``'Supersedes'``, ``'plan_revision'``, ``'plan_revision'``, NULL,
  the predecessor Plan Revision Identity, the
  ``CreatePlanRevisionResult.supersedes_relationship_id`` value), and
  (c) no second ``Relationships`` row exists in the database with
  ``relationship_type = 'Supersedes'`` and
  ``source_id = result.plan_revision_id``.

Each Hypothesis case builds a fresh per-case SQLite engine on a unique
:class:`tempfile.TemporaryDirectory` path so cross-case identifier and
relationship state cannot leak across shrinks (design §"Testing
Strategy" — per-case database isolation). Prerequisites are seeded
through direct ``INSERT`` (rather than through their corresponding
Planning_Service) so each property test exercises exactly one
operation under test — the row Property 28 quantifies over — and a
shrunken counterexample stays actionable.
"""

from __future__ import annotations

import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final

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
from walking_slice.identity import IdentityService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.plan_approvals import PlanApprovalService
from walking_slice.planning.plan_reviews import PlanReviewService
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# Property 28 quantifies over the persisted Relationship rows produced
# by the three Planning_Service create operations. Each case seeds a
# single actor Party, a single Activity Plan, and a single draft Plan
# Revision (plus, for the Supersedes test, a single predecessor draft
# Plan Revision); the clock is pinned via :class:`FixedClock` so every
# persisted row in the case carries the same recorded time and any
# Hypothesis-shrunken counterexample is deterministic across shrinks.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-28/scope"

_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_DRAFT_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"
# Predecessor Plan Revision Identity used by the Supersedes test.
# Kept distinct from ``_DRAFT_PLAN_REVISION_ID`` so the Supersedes
# row's source / target identifiers are unambiguous in counterexample
# output.
_PREDECESSOR_PLAN_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000c4"
)


# Relationship constants Property 28 quantifies over. Centralizing
# the strings here keeps the property's expected values aligned with
# the AD-WS-17 / AD-WS-19 schema additions and the service-level
# constants in :mod:`walking_slice.planning.plan_approvals`,
# :mod:`walking_slice.planning.plan_reviews`, and
# :mod:`walking_slice.planning.plan_revisions`.
_REL_TYPE_ADDRESSES: Final[str] = "Addresses"
_REL_TYPE_RELATES_TO: Final[str] = "Relates To"
_REL_TYPE_SUPERSEDES: Final[str] = "Supersedes"

_KIND_PLAN_APPROVAL: Final[str] = "plan_approval"
_KIND_PLAN_REVIEW_REVISION: Final[str] = "plan_review_revision"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"

_SEMANTIC_ROLE_REVIEW: Final[str] = "review"


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case rows in ``Relationships``, identity
# state, and audit rows cannot bleed between cases (design §"Testing
# Strategy"). :class:`tempfile.TemporaryDirectory` owns the per-case
# directory; function-scoped pytest fixtures cannot be used here
# because Hypothesis does not reset them between drawn inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    and both the Slice 1 and Slice 2 schemas installed."""
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
    return engine


# ---------------------------------------------------------------------------
# Seed helpers.
#
# Prerequisites are written through direct INSERT (rather than through
# the corresponding Planning_Service) so each property case exercises
# exactly one operation under test — the create call that emits the
# Relationship row Property 28 quantifies over — and a shrunken
# counterexample points at the operation under test, not at a
# tangential seed step.
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the actor Party and the assigning-authority Party."""
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_ID, "Property 28 Actor"),
            (_ASSIGNING_AUTHORITY_ID, "Property 28 Resource Steward"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, :ts)
                    """
                ),
                {"pid": party_id, "name": display, "ts": _NOW_ISO},
            )


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    authorities: tuple[str, ...],
    role_name: str,
) -> None:
    """Grant ``authorities`` over ``_SCOPE`` to the actor Party.

    Property 28 needs a different authority per relationship invariant:
    ``approve`` for Plan Approval (Requirement 11.5 / AD-WS-15),
    ``review`` for Plan Review (Requirement 11.4 / AD-WS-15), and
    ``modify`` for Plan Revision (AD-WS-15). The role-assignment
    effective period generously brackets the fixed clock instant so a
    shrunken case never fails on timing.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name=role_name,
        scope=_SCOPE,
        authorities_granted=authorities,
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_project(engine: Engine) -> None:
    """Insert one ``Projects`` row by direct INSERT."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _NOW_ISO},
        )


def _seed_activity_plan(engine: Engine) -> None:
    """Insert one ``Activity_Plans`` row by direct INSERT."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 28 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_draft_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str = _DRAFT_PLAN_REVISION_ID,
) -> None:
    """Insert one ``Plan_Revisions`` row with ``lifecycle_state='draft'``.

    ``INSERT`` into ``Plan_Revisions`` is not gated by the AD-WS-19
    lifecycle UPDATE trigger — that trigger fires only on UPDATE — so
    a fresh draft revision can be seeded with no session-pragma
    plumbing. The Supersedes test uses this helper twice with two
    distinct identifiers to seed the predecessor independently.
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
                    :rev, :aid, NULL, 'draft',
                    'Property 28 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Relationship-row probe helpers.
#
# Each helper SELECTs the rows Property 28 quantifies over by Property
# 28's predicates and returns them as ``Mapping``-shaped rows. The
# helpers project every column Property 28 names in its predicate so
# any drift surfaces as a precise tuple diff in the test assertion.
# ---------------------------------------------------------------------------


_RELATIONSHIP_PROJECTION_COLUMNS: Final[tuple[str, ...]] = (
    "relationship_id",
    "relationship_type",
    "source_kind",
    "source_id",
    "source_revision_id",
    "target_kind",
    "target_id",
    "target_revision_id",
    "semantic_role",
)


def _fetch_relationships_by_source_id(
    engine: Engine, *, source_id: str
) -> list[dict[str, Any]]:
    """Return every ``Relationships`` row whose ``source_id`` matches."""
    columns = ", ".join(_RELATIONSHIP_PROJECTION_COLUMNS)
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    f"SELECT {columns} FROM Relationships "
                    "WHERE source_id = :sid "
                    "ORDER BY relationship_id"
                ),
                {"sid": source_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Per-case service factory.
#
# Fresh services per Hypothesis case so :class:`IdentityService`'s
# in-memory issued-identifier set cannot bleed across shrinks and the
# minted identifiers carry the canonical UUIDv7 form Slice 2's tests
# rely on.
# ---------------------------------------------------------------------------


def _build_services() -> tuple[
    FixedClock,
    IdentityService,
    AuditLog,
    AuthorizationService,
    ProvenanceManifestWriter,
]:
    """Construct the per-case service bundle.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state and any audit-correlation accumulator cannot bleed
    across shrinks.
    """
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    manifest_writer = ProvenanceManifestWriter(
        clock=clock,
        identity_service=identity_service,
    )
    return (
        clock,
        identity_service,
        audit_log,
        authorization_service,
        manifest_writer,
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# Each test exercises a single create operation; the strategies stay
# narrow so a shrunken counterexample is readable. Property 28 is not
# about UTF-8 robustness or boundary-character handling (Slice 1
# Property 7 / Slice 2 Properties 16 / 22 cover those); the alphabet
# below is the same one used by :mod:`tests/property/test_property_25_plan_approval_uniqueness`.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Strategy for a non-control text run of ``min_size..max_size`` chars."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


# Plan Approval — Requirement 9.2.
_plan_approval_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(["Approve", "Reject_Approval"]),
        "rationale": _bounded_text(1, 200),
    }
)


# Plan Review — Requirement 8.2.
_plan_review_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(
            ["Endorse", "Changes_Requested", "Reject"]
        ),
        "rationale": _bounded_text(1, 200),
    }
)


# Plan Revision with predecessor — Requirement 7.2 / 7.3.
# ``planning_assumptions`` is kept narrow (0..5 short entries) and
# ``deliverable_expectation_refs`` is held empty so no extra
# prerequisite seeding is required. Property 28 part 3 only quantifies
# over the ``Supersedes`` Relationship row.
_plan_revision_strategy = st.fixed_dictionaries(
    {
        "planned_scope": _bounded_text(1, 200),
        "planning_assumptions": st.lists(
            _bounded_text(1, 200), min_size=0, max_size=5
        ),
        "ordering_rationale": st.one_of(
            st.none(),
            _bounded_text(0, 200),
        ),
    }
)


# ===========================================================================
# Property 28 part 1 — Plan Approval ``Addresses`` Relationship row shape.
# ===========================================================================


# Feature: second-walking-slice, Property 28: Planning relationship-structure invariants
@given(payload=_plan_approval_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_plan_approval_persists_exactly_one_addresses_relationship_row(
    payload: dict[str, Any],
) -> None:
    """**Validates: Requirements 9.3, 20.7**

    For any authorized, valid Plan Approval creation request:

    - exactly one ``Relationships`` row exists with
      ``source_id = result.plan_approval_id``,
    - that row carries ``relationship_type = 'Addresses'``,
      ``source_kind = 'plan_approval'``,
      ``target_kind = 'plan_revision'``,
      ``target_id = target_plan_revision_id``,
      ``semantic_role IS NULL``, and
      ``relationship_id = result.addresses_relationship_id``,
    - no second ``Addresses`` row sourced from the same Plan Approval
      Identity is persisted.
    """
    with tempfile.TemporaryDirectory(prefix="prop28_pa_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("approve",),
                role_name="plan_approver",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_draft_plan_revision(engine)

            service = PlanApprovalService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                manifest_writer=manifest_writer,
            )

            with engine.begin() as conn:
                result = service.create_plan_approval(
                    conn,
                    engine,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=payload["outcome"],
                    rationale=payload["rationale"],
                    approving_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                )

            # 1. Exactly one Relationship row sourced from the new
            # Plan Approval Identity.
            rows = _fetch_relationships_by_source_id(
                engine, source_id=result.plan_approval_id
            )
            assert len(rows) == 1, (
                "Property 28 part 1 requires exactly one Relationship "
                "row sourced from a Plan Approval; observed rows: "
                f"{rows!r}"
            )

            # 2. That row matches Property 28 part 1's predicate
            # tuple exactly.
            row = rows[0]
            assert row["relationship_type"] == _REL_TYPE_ADDRESSES
            assert row["source_kind"] == _KIND_PLAN_APPROVAL
            assert row["source_id"] == result.plan_approval_id
            assert row["source_revision_id"] is None
            assert row["target_kind"] == _KIND_PLAN_REVISION
            assert row["target_id"] == _DRAFT_PLAN_REVISION_ID
            assert row["target_revision_id"] is None
            assert row["semantic_role"] is None
            assert (
                row["relationship_id"] == result.addresses_relationship_id
            )

            # 3. No second ``Addresses`` row sourced from the same
            # Plan Approval Identity exists anywhere in the database.
            # The query above already covered that — re-asserting via
            # a tighter predicate guards against any future schema
            # change that would change row keys without changing the
            # invariant.
            with engine.connect() as conn:
                duplicate_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM Relationships
                             WHERE relationship_type = :t
                               AND source_id = :sid
                            """
                        ),
                        {
                            "t": _REL_TYPE_ADDRESSES,
                            "sid": result.plan_approval_id,
                        },
                    ).scalar_one()
                )
            assert duplicate_count == 1
        finally:
            engine.dispose()


# ===========================================================================
# Property 28 part 2 — Plan Review ``Relates To`` Relationship row shape.
# ===========================================================================


# Feature: second-walking-slice, Property 28: Planning relationship-structure invariants
@given(payload=_plan_review_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_plan_review_persists_exactly_one_relates_to_review_relationship_row(
    payload: dict[str, Any],
) -> None:
    """**Validates: Requirements 8.3, 20.7**

    For any authorized, valid Plan Review creation request:

    - exactly one ``Relationships`` row exists with
      ``source_id = result.plan_review_id`` and
      ``source_revision_id = result.plan_review_revision_id``,
    - that row carries ``relationship_type = 'Relates To'``,
      ``source_kind = 'plan_review_revision'``,
      ``target_kind = 'plan_revision'``,
      ``target_id = target_plan_revision_id``,
      ``semantic_role = 'review'``, and
      ``relationship_id = result.relates_to_relationship_id``,
    - no second ``Relates To`` row sourced from the same Plan Review
      Revision is persisted.
    """
    with tempfile.TemporaryDirectory(prefix="prop28_prv_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("review",),
                role_name="plan_reviewer",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_draft_plan_revision(engine)

            service = PlanReviewService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=payload["outcome"],
                    rationale=payload["rationale"],
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            # 1. Exactly one Relationship row sourced from the new
            # Plan Review Revision (matched on both source_id —
            # the Plan Review Resource Identity — and
            # source_revision_id — the Plan Review Revision
            # Identity).
            rows = _fetch_relationships_by_source_id(
                engine, source_id=result.plan_review_id
            )
            assert len(rows) == 1, (
                "Property 28 part 2 requires exactly one Relationship "
                "row sourced from a Plan Review Revision; observed "
                f"rows: {rows!r}"
            )

            # 2. That row matches Property 28 part 2's predicate
            # tuple exactly.
            row = rows[0]
            assert row["relationship_type"] == _REL_TYPE_RELATES_TO
            assert row["source_kind"] == _KIND_PLAN_REVIEW_REVISION
            assert row["source_id"] == result.plan_review_id
            assert (
                row["source_revision_id"]
                == result.plan_review_revision_id
            )
            assert row["target_kind"] == _KIND_PLAN_REVISION
            assert row["target_id"] == _DRAFT_PLAN_REVISION_ID
            assert row["target_revision_id"] is None
            assert row["semantic_role"] == _SEMANTIC_ROLE_REVIEW
            assert (
                row["relationship_id"] == result.relates_to_relationship_id
            )

            # 3. No second ``Relates To`` with the ``'review'``
            # semantic role exists in the database for this Plan
            # Review Revision.
            with engine.connect() as conn:
                duplicate_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM Relationships
                             WHERE relationship_type = :t
                               AND source_id = :sid
                               AND source_revision_id = :srid
                               AND semantic_role = :sr
                            """
                        ),
                        {
                            "t": _REL_TYPE_RELATES_TO,
                            "sid": result.plan_review_id,
                            "srid": result.plan_review_revision_id,
                            "sr": _SEMANTIC_ROLE_REVIEW,
                        },
                    ).scalar_one()
                )
            assert duplicate_count == 1
        finally:
            engine.dispose()


# ===========================================================================
# Property 28 part 3 — Plan Revision ``Supersedes`` Relationship row shape.
# ===========================================================================


# Feature: second-walking-slice, Property 28: Planning relationship-structure invariants
@given(payload=_plan_revision_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_plan_revision_with_predecessor_persists_exactly_one_supersedes_relationship_row(
    payload: dict[str, Any],
) -> None:
    """**Validates: Requirements 7.3, 20.7**

    For any authorized, valid Plan Revision creation request that
    names a draft predecessor Plan Revision Identity of the same
    Activity Plan:

    - exactly one ``Relationships`` row exists with
      ``source_id = result.plan_revision_id``,
    - that row carries ``relationship_type = 'Supersedes'``,
      ``source_kind = 'plan_revision'``,
      ``target_kind = 'plan_revision'``,
      ``target_id = predecessor_plan_revision_id``,
      ``semantic_role IS NULL``, and
      ``relationship_id = result.supersedes_relationship_id``,
    - no second ``Supersedes`` row sourced from the same Plan
      Revision Identity is persisted.

    The predecessor Plan Revision is seeded in ``'draft'`` state per
    Requirement 7.3 / 7.4 (Approved predecessors are rejected by the
    service). The new Plan Revision targets the same Activity Plan
    so the cross-Activity-Plan rejection path is not exercised here
    — Property 28 quantifies over the happy-path row shape.
    """
    with tempfile.TemporaryDirectory(prefix="prop28_prv_sup_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
                _manifest_writer,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("modify",),
                role_name="plan_revision_author",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            # Seed the predecessor Plan Revision as ``'draft'`` per
            # Requirement 7.4: an approved predecessor would be
            # rejected by :class:`PlanRevisionPredecessorApprovedError`.
            _seed_draft_plan_revision(
                engine, plan_revision_id=_PREDECESSOR_PLAN_REVISION_ID
            )

            service = PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )

            with engine.begin() as conn:
                result = service.create_plan_revision(
                    conn,
                    target_activity_plan_id=_ACTIVITY_PLAN_ID,
                    planned_scope=payload["planned_scope"],
                    planning_assumptions=tuple(
                        payload["planning_assumptions"]
                    ),
                    ordering_rationale=payload["ordering_rationale"],
                    predecessor_plan_revision_id=(
                        _PREDECESSOR_PLAN_REVISION_ID
                    ),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            # 1. Exactly one Relationship row sourced from the new
            # Plan Revision Identity.
            rows = _fetch_relationships_by_source_id(
                engine, source_id=result.plan_revision_id
            )
            assert len(rows) == 1, (
                "Property 28 part 3 requires exactly one Relationship "
                "row sourced from a Plan Revision created with a "
                f"predecessor; observed rows: {rows!r}"
            )

            # 2. That row matches Property 28 part 3's predicate
            # tuple exactly.
            row = rows[0]
            assert row["relationship_type"] == _REL_TYPE_SUPERSEDES
            assert row["source_kind"] == _KIND_PLAN_REVISION
            assert row["source_id"] == result.plan_revision_id
            assert row["source_revision_id"] is None
            assert row["target_kind"] == _KIND_PLAN_REVISION
            assert row["target_id"] == _PREDECESSOR_PLAN_REVISION_ID
            assert row["target_revision_id"] is None
            assert row["semantic_role"] is None
            assert (
                row["relationship_id"]
                == result.supersedes_relationship_id
            )

            # 3. No second ``Supersedes`` row sourced from the same
            # Plan Revision Identity exists in the database.
            with engine.connect() as conn:
                duplicate_count = int(
                    conn.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM Relationships
                             WHERE relationship_type = :t
                               AND source_id = :sid
                            """
                        ),
                        {
                            "t": _REL_TYPE_SUPERSEDES,
                            "sid": result.plan_revision_id,
                        },
                    ).scalar_one()
                )
            assert duplicate_count == 1
        finally:
            engine.dispose()
