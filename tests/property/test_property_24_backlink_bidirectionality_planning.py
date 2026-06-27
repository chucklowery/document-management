# Feature: second-walking-slice, Property 24: Backlink bidirectionality for planning nodes
"""Property 24 — Backlink bidirectionality for planning nodes (task 16.9).

**Property 24: Backlink bidirectionality for planning nodes**

For all Relationships ``R`` recorded between in-scope endpoints across
Slice 1 + Slice 2 (Source Document Revisions, Content Region
Occurrences, Finding Revisions, Recommendation Revisions, Decision
Immutable Records, Trail Step identities, Objective Revisions,
Intended Outcome Revisions, Project Revisions, Deliverable Expectation
Revisions, Activity Plans, Plan Revisions, Plan Review Revisions, Plan
Approval Records), and for all requesting Parties ``P`` who hold view
authority on both ``R`` and its source endpoint, the
:class:`~walking_slice.provenance.ProvenanceNavigator` returns ``R``
from the target's backlink query *if and only if* ``R`` is returned
from the source's outbound query, and the Relationship attribute
values returned from both directions — *including the additive
``semantic_role`` column from AD-WS-17* — are identical.

**Validates: Requirements 1.5, 15.1, 15.2, 15.4, 15.6, 20.9**

Strategy
========

Each Hypothesis case draws a *relationship graph* whose endpoints
span the full Slice 1 + Slice 2 surface:

- 1..3 *target* endpoint descriptors. Each carries a fresh UUIDv7
  ``id``, a fresh ``revision_id`` (or ``None``), and a ``kind`` drawn
  from the union of Slice 1 source kinds and the eight Slice 2
  planning kinds that task 12.2 added to
  :data:`walking_slice.provenance._AUTHORIZED_SOURCE_KINDS`.
- 1..3 *source* endpoint descriptors with the same shape.
- 1..6 *relationship* descriptors. Each picks one source and one
  target from the pools, draws a ``relationship_type`` from the full
  six-member set permitted by the post-Slice-2 CHECK constraint on
  ``Relationships.relationship_type`` (``Supports``, ``Contradicts``,
  ``Derived From``, ``Addresses``, ``Supersedes``, ``Relates To``),
  draws a ``semantic_role`` value (``None``, ``'review'``, or one of a
  small curated set of arbitrary discriminators so the AD-WS-17
  round-trip is exercised across more than just the ``'review'``
  literal), draws a per-relationship timestamp offset (in seconds),
  and mints a fresh ``relationship_id``.

Per case the test:

1. Spins up a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state
   cannot contaminate the bidirectionality assertions (design
   §"Testing Strategy" — "Each property and example test gets a fresh
   SQLite database"). Both the Slice 1 schema (with the additive
   ``Relationships.semantic_role`` column from task 1.2) and the
   Slice 2 schema (task 1.3) are installed.
2. Seeds the requesting Party (FK target for
   ``Role_Assignments.party_id`` and the
   :meth:`AuthorizationService.evaluate` audit row), the assigning
   authority Party, and the authoring Party referenced on every
   inserted Relationship row.
3. Assigns one wildcard-scope ``view`` role to the requesting Party
   so the Party holds view authority on every Relationship and every
   source endpoint in the graph — satisfying the property's "for all
   requesting Parties ``P`` who hold view authority on both ``R`` and
   its source endpoint" precondition by construction. The wildcard
   scope ``'*'`` covers every planning source endpoint's identity via
   :meth:`AuthorizationService._scope_covers`, and the action-prefix
   fallback in
   :func:`walking_slice.authorization._required_authority` maps every
   ``view.<planning_kind>`` action to the ``view`` authority — so no
   per-kind authorization wiring is required for the property to hold
   across Slice 2 source kinds (task 12.2 additive coverage).
4. Inserts every Relationship row directly into the ``Relationships``
   table, including the additive ``semantic_role`` column. Direct
   INSERT (rather than routing through any Planning_Service) lets the
   strategy fabricate arbitrary
   ``(source_kind, target_kind, relationship_type, semantic_role,
   recorded_at)`` combinations independent of the slice's natural
   pipelines. The ``Relationships`` schema has no FK constraints on
   ``source_id`` / ``target_id`` / ``source_revision_id`` /
   ``target_revision_id`` (only on ``authoring_party_id``) so the
   fabricated identities round-trip cleanly.
5. For every persisted Relationship ``R``:

   - Calls :meth:`ProvenanceNavigator.list_backlinks` with
     ``(target_id=R.target_id, target_revision_id=R.target_revision_id,
     party_id=P)`` and asserts ``R.relationship_id`` is present in the
     returned :class:`~walking_slice.provenance.BacklinkPage.entries`,
     with every Requirement 15.2 attribute (``relationship_id``,
     ``relationship_type``, ``source_id``, ``source_kind``,
     ``source_revision_id``, ``authoring_party_id``) and
     ``recorded_at`` byte-equivalent to the inserted row.
   - Issues the outbound query directly against ``Relationships``
     filtered by ``source_id`` (and ``source_revision_id`` when
     applicable). The outbound query is intentionally a plain SELECT —
     :class:`~walking_slice.provenance.ProvenanceNavigator` does not
     expose a ``list_outbound`` method (task 12.1 / 12.2 scoped only
     the backlink surface), and the property statement's "from the
     source's outbound query" is read here as "the canonical truth
     about outbound Relationships, which the ``Relationships`` table
     is" — mirroring the Slice 1 Property 3 idiom
     (:mod:`tests.property.test_property_3_backlink_bidirectionality`).
     Authorization is uniform across both directions because the
     requesting Party holds wildcard view authority by construction.
   - Asserts both directions report the same Relationship Identity
     and the same attribute values, including the additive
     ``semantic_role`` column. Because
     :class:`~walking_slice.provenance.BacklinkEntry` does not yet
     expose ``semantic_role`` (the value object pre-dates AD-WS-17),
     the ``semantic_role`` round-trip is asserted against the
     underlying ``Relationships`` row resolved from each direction's
     ``relationship_id``: the row read while the backlink-direction
     ``relationship_id`` is in scope and the row read while the
     outbound-direction ``relationship_id`` is in scope must both
     match the inserted value byte-for-byte. The property's
     "identical Relationship attribute values, including the
     ``semantic_role`` column" clause is then satisfied because the
     two ``relationship_id`` values are equal (so they reference the
     same single row in the immutable ``Relationships`` table, by
     AD-WS-4) and that row's ``semantic_role`` is the inserted
     value.

Requirement coverage notes
==========================

- **1.5** — :class:`~walking_slice.identity.IdentityService` is
  exercised implicitly because every Relationship Identity returned
  from both directions is the same single authoritative canonical
  UUIDv7 — the inserted ``relationship_id``. The property's "same
  Relationship Identity from both source-direction and backlink
  queries" clause is asserted by string equality on
  ``relationship_id`` across the two projections.
- **15.1** — The :data:`~walking_slice.provenance.BACKLINK_PAGE_LIMIT`
  cap (500) is well above the per-case graph size (≤ 6
  Relationships), so every persisted Relationship is reachable in a
  single backlink page. Requirement 15.1's deterministic ordering is
  exercised by the navigator's
  ``ORDER BY (recorded_at, relationship_id)`` clause; the property
  test reads the page directly without pagination, so any ordering
  bug that drops or reorders a Relationship surfaces as a missing
  identity.
- **15.2** — Every Requirement 15.2 attribute is asserted equal across
  both directions. Substitution of one Relationship's attributes for
  another's — for example a target-side bug that swapped
  ``source_kind`` between two backlinks pointing at the same target,
  or an AD-WS-17 bug that overwrote ``semantic_role`` on read —
  would falsify the property immediately.
- **15.4** — The task 12.2 additive-coverage extension to
  :data:`walking_slice.provenance._AUTHORIZED_SOURCE_KINDS` ensures
  every Slice 2 source kind is recognized by the same algorithm; the
  strategy samples from the full union of Slice 1 + Slice 2 kinds,
  so any bug that drops Slice 2 source kinds from the authorized
  projection surfaces here.
- **15.6** — Backlink reads on planning Resources return the same
  Relationship Identities and attribute values that the outbound
  query against the corresponding source endpoint would. This is the
  symmetric guarantee of Property 24 specialized to Slice 2 nodes.
- **20.9** — The Hypothesis settings register ``max_examples=100``
  and ``deadline=2000`` per the slice's repeatable-runs operational
  contract (Requirement 20.13).
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, Optional

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import (
    BACKLINK_PAGE_LIMIT,
    BacklinkEntry,
    ProvenanceNavigator,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the three Parties referenced by FK constraints.
#
# - ``_REQUESTER_PARTY_ID`` is the requesting Party ``P`` from the
#   property statement; it receives the wildcard ``view`` role and is
#   passed to :meth:`ProvenanceNavigator.list_backlinks` on every
#   call.
# - ``_ASSIGNING_AUTHORITY_ID`` is the actor on the role-assignment
#   audit row.
# - ``_AUTHORING_PARTY_ID`` is recorded on every inserted Relationship
#   row (``Relationships.authoring_party_id`` is FK to ``Parties``).
# ---------------------------------------------------------------------------


_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000000002"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000003"


# The :class:`FixedClock` instant. Every Relationship's ``recorded_at``
# is derived from this baseline plus a per-relationship offset so two
# Relationships in the same case do not share a timestamp; the
# backlink query's ``ORDER BY (recorded_at, relationship_id)`` clause
# tolerates ties, but distinct timestamps make the assertion failure
# diagnostics easier to read when the property does falsify.
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)


# The six Relationship types permitted by the post-Slice-2 CHECK
# constraint on ``Relationships.relationship_type`` (Slice 1's five
# original values plus the AD-WS-17 ``'Relates To'`` addition). The
# property holds across every type so the strategy samples all six —
# any bug that drops the new ``'Relates To'`` value from one direction
# of the bidirectional surface falsifies the property immediately.
_RELATIONSHIP_TYPES: Final[tuple[str, ...]] = (
    "Supports",
    "Contradicts",
    "Derived From",
    "Addresses",
    "Supersedes",
    "Relates To",
)


# Endpoint kinds drawn from the slice's full Slice 1 + Slice 2 surface
# named by Property 24. The Slice 1 kinds match Slice 1 Property 3
# (:data:`tests.property.test_property_3_backlink_bidirectionality._ENDPOINT_KINDS`);
# the Slice 2 kinds match the additive coverage extension to
# :data:`walking_slice.provenance._AUTHORIZED_SOURCE_KINDS` from task
# 12.2. Both ``source_kind`` and ``target_kind`` columns are free-form
# TEXT in the ``Relationships`` schema so the strategy is free to draw
# any combination.
_ENDPOINT_KINDS: Final[tuple[str, ...]] = (
    # Slice 1 endpoint kinds.
    "document_revision",
    "region_occurrence",
    "finding_revision",
    "recommendation_revision",
    "decision",
    "trail_step",
    # Slice 2 planning endpoint kinds (task 12.2 additive coverage
    # extension; AD-WS-15 already maps every ``view.*`` planning
    # action to the ``view`` authority via the prefix fallback in
    # :func:`walking_slice.authorization._required_authority`).
    "objective_revision",
    "intended_outcome_revision",
    "project_revision",
    "deliverable_expectation_revision",
    "activity_plan",
    "plan_revision",
    "plan_review_revision",
    "plan_approval",
)


# Curated ``semantic_role`` values the strategy draws. ``None`` is the
# AD-WS-17 default for every non-Plan-Review row (Slice 1 rows and
# planning rows other than the single ``'review'`` discriminator); the
# explicit ``'review'`` literal is the Slice 2 Plan Review value; the
# remaining small set of arbitrary strings exercises the AD-WS-17
# round-trip across non-canonical values so any future bug that
# silently normalizes ``semantic_role`` on read would falsify the
# property. The schema column is ``TEXT NULL`` with no CHECK, so any
# string round-trips byte-equivalently when the bidirectional
# projections are correct.
_SEMANTIC_ROLES: Final[tuple[Optional[str], ...]] = (
    None,
    "review",
    "addresses",
    "supersedes",
    "custom-role-1",
    "custom-role-2",
)


def _fresh_uuid7() -> str:
    """Mint one fresh UUIDv7 string.

    Used as a one-off identity for every endpoint (target and source)
    and every Relationship in the graph. Each call returns a fresh
    value so generated endpoints do not collide across cases or
    across endpoints within one case.
    """
    return str(uuid_utils.uuid7())


def _seed_party(connection: Connection, *, party_id: str, display: str) -> None:
    """Insert one Party row required by the FK constraints."""
    connection.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {
            "pid": party_id,
            "name": display,
            "ts": format_iso8601_ms(_FIXED_NOW),
        },
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# A *graph* draws:
#
# - 1..3 *target* endpoint descriptors. Each carries a fresh UUIDv7
#   ``id``, a fresh ``revision_id`` (or ``None``), and a ``kind``.
# - 1..3 *source* endpoint descriptors with the same shape.
# - 1..6 *relationship* descriptors. Each picks one source index and
#   one target index from the pools, draws a ``relationship_type``,
#   a ``semantic_role`` (``None`` or one of the curated discriminator
#   values), and a per-relationship timestamp offset (in seconds),
#   and mints a fresh ``relationship_id``.
#
# Sharing endpoints across relationships exercises:
#
# - Multiple inbound Relationships to one target (backlink direction
#   returns a multi-entry page; the property still finds each
#   relationship under its specific identity).
# - Multiple outbound Relationships from one source (outbound
#   direction returns multiple rows; the property still finds the
#   target-side counterpart for each).
#
# The strategy intentionally does *not* deduplicate Relationships by
# ``(source, target, relationship_type, semantic_role)`` — two
# Relationships differing only in ``recorded_at`` are still distinct
# identities and the property must hold for each independently.
# ---------------------------------------------------------------------------


@st.composite
def _endpoint(draw) -> dict:
    """Draw one endpoint descriptor (target or source).

    Returns a dict with:

    - ``id`` (str): fresh UUIDv7.
    - ``revision_id`` (str | None): fresh UUIDv7 or ``None``. Drawn
      ``None`` for some endpoints so the property exercises both the
      "target carries a Revision Identity" and "target is a Resource
      header" branches of the backlink query.
    - ``kind`` (str): one of the Slice 1 + Slice 2 endpoint kinds.
    """
    return {
        "id": _fresh_uuid7(),
        "revision_id": draw(st.one_of(st.none(), st.builds(_fresh_uuid7))),
        "kind": draw(st.sampled_from(_ENDPOINT_KINDS)),
    }


@st.composite
def _relationship_graph(draw) -> dict:
    """Draw one relationship graph: target pool + source pool + relationships.

    Returns a dict with:

    - ``targets`` (list[dict]): 1..3 target endpoints.
    - ``sources`` (list[dict]): 1..3 source endpoints.
    - ``relationships`` (list[dict]): 1..6 Relationship descriptors.
      Each dict carries the nine columns needed to INSERT one row
      (the eight schema columns including the additive AD-WS-17
      ``semantic_role`` plus a precomputed ISO-8601 ``recorded_at``
      string). The ``recorded_at`` offset is per-relationship-distinct
      so the backlink ``ORDER BY (recorded_at, relationship_id)``
      clause has a stable, observable ordering.
    """
    num_targets = draw(st.integers(min_value=1, max_value=3))
    targets = [draw(_endpoint()) for _ in range(num_targets)]

    num_sources = draw(st.integers(min_value=1, max_value=3))
    sources = [draw(_endpoint()) for _ in range(num_sources)]

    num_relationships = draw(st.integers(min_value=1, max_value=6))
    relationships: list[dict] = []
    for index in range(num_relationships):
        target_index = draw(st.integers(min_value=0, max_value=num_targets - 1))
        source_index = draw(st.integers(min_value=0, max_value=num_sources - 1))
        target = targets[target_index]
        source = sources[source_index]
        # One second per relationship so timestamps are distinct
        # within the case. Hypothesis can still shrink the order by
        # shrinking the per-relationship target/source picks; the
        # offset only contributes to deterministic ordering, not to
        # the property under test.
        offset_seconds = index
        recorded_at = format_iso8601_ms(
            _FIXED_NOW + timedelta(seconds=offset_seconds)
        )
        relationships.append(
            {
                "relationship_id": _fresh_uuid7(),
                "relationship_type": draw(
                    st.sampled_from(_RELATIONSHIP_TYPES)
                ),
                "source_kind": source["kind"],
                "source_id": source["id"],
                "source_revision_id": source["revision_id"],
                "target_kind": target["kind"],
                "target_id": target["id"],
                "target_revision_id": target["revision_id"],
                "semantic_role": draw(st.sampled_from(_SEMANTIC_ROLES)),
                "recorded_at": recorded_at,
            }
        )
    return {
        "targets": targets,
        "sources": sources,
        "relationships": relationships,
    }


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# temp-dir path so cross-case ``Relationships`` rows, ``Role_Assignments``
# rows, and ``Audit_Records`` rows cannot leak between cases (design
# §"Testing Strategy" — "Each property and example test gets a fresh
# SQLite database"). The Slice 1 schema (including the additive
# ``Relationships.semantic_role`` column from task 1.2) and the Slice
# 2 schema (task 1.3) are both installed so the navigator's
# planning-aware backlink coverage (task 12.2) is exercised end-to-end.
# A :class:`tempfile.TemporaryDirectory` context inside the test body
# owns the per-case directory; Hypothesis disallows function-scoped
# pytest fixtures for per-case state because they would not reset
# between generated inputs.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas.

    Installs both the Slice 1 schema (which carries the additive
    ``Relationships.semantic_role`` column added by task 1.2) and the
    Slice 2 schema (task 1.3) so the property exercises the full
    Slice 1 + Slice 2 relationship surface named in the property
    statement.
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
    return engine


# ---------------------------------------------------------------------------
# Direct INSERT helper.
#
# Bypasses every Planning_Service so the strategy can fabricate
# arbitrary ``(source_kind, target_kind, relationship_type,
# semantic_role)`` combinations. The ``Relationships`` schema only
# constrains ``relationship_type`` (CHECK), ``authoring_party_id``
# (FK), and the AD-WS-17 ``semantic_role`` column is ``TEXT NULL``
# with no CHECK, so every drawn combination round-trips cleanly.
# ---------------------------------------------------------------------------


def _insert_relationship(connection: Connection, relationship: dict) -> None:
    """Insert one Relationship row into the open transaction.

    The ``semantic_role`` value (``None`` or a non-empty string) is
    bound through SQLAlchemy's named-parameter mechanism so SQLite
    stores ``NULL`` and ``'review'`` literally, preserving the
    AD-WS-17 byte-equivalence the property asserts.
    """
    connection.execute(
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
            "relationship_id": relationship["relationship_id"],
            "relationship_type": relationship["relationship_type"],
            "source_kind": relationship["source_kind"],
            "source_id": relationship["source_id"],
            "source_revision_id": relationship["source_revision_id"],
            "target_kind": relationship["target_kind"],
            "target_id": relationship["target_id"],
            "target_revision_id": relationship["target_revision_id"],
            "authoring_party_id": _AUTHORING_PARTY_ID,
            "recorded_at": relationship["recorded_at"],
            "semantic_role": relationship["semantic_role"],
        },
    )


# ---------------------------------------------------------------------------
# Outbound query helper.
#
# :class:`~walking_slice.provenance.ProvenanceNavigator` does not
# expose a ``list_outbound`` method (task 12.1 / 12.2 scoped only
# backlinks). The property's "from the source's outbound query" is
# asserted here against the canonical truth — a direct SELECT on
# ``Relationships`` filtered by ``source_id`` (and, when applicable,
# ``source_revision_id``). Both directions share the same
# authorization model in this test because the requesting Party holds
# wildcard ``view`` authority, so the two projections are
# byte-equivalent: the same Relationships in the same
# ``(recorded_at, relationship_id)`` order. The select also returns
# the additive AD-WS-17 ``semantic_role`` column so the property can
# verify Requirement 15.2's "identical Relationship attribute values"
# clause covers the new column.
# ---------------------------------------------------------------------------


def _fetch_outbound(
    engine: Engine,
    *,
    source_id: str,
    source_revision_id: Optional[str],
) -> list[dict]:
    """Return every Relationship sourced from ``(source_id, source_revision_id)``.

    The result is shaped as a list of plain dicts (rather than
    :class:`~walking_slice.provenance.BacklinkEntry`) because the
    value object pre-dates AD-WS-17 and does not yet carry
    ``semantic_role``; the property asserts the round-trip directly
    against the database row so the new column participates in the
    bidirectional-equality check.

    When ``source_revision_id`` is ``None`` the query matches any
    ``source_revision_id`` (including ``NULL``), mirroring the
    backlink query's behavior for ``target_revision_id=None``. When
    ``source_revision_id`` is supplied, only rows with the matching
    Revision Identity are returned.
    """
    params: dict = {"source_id": source_id}
    revision_clause = ""
    if source_revision_id is not None:
        revision_clause = "AND source_revision_id = :source_revision_id"
        params["source_revision_id"] = source_revision_id

    sql = f"""
        SELECT
            relationship_id,
            relationship_type,
            source_kind,
            source_id,
            source_revision_id,
            target_kind,
            target_id,
            target_revision_id,
            authoring_party_id,
            recorded_at,
            semantic_role
        FROM Relationships
        WHERE source_id = :source_id
          {revision_clause}
        ORDER BY recorded_at ASC, relationship_id ASC
    """

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    return [dict(row) for row in rows]


def _fetch_relationship_row(
    engine: Engine, *, relationship_id: str
) -> dict:
    """Return the full ``Relationships`` row for ``relationship_id``.

    The row is read on a fresh connection so the property test
    observes the persisted state independently of the navigator's
    connection. Used to surface the ``semantic_role`` value alongside
    the navigator's :class:`~walking_slice.provenance.BacklinkEntry`
    projection — the value object does not yet expose the new column,
    but the underlying immutable row (AD-WS-4) is the canonical
    source of truth for every attribute including AD-WS-17's
    ``semantic_role``.
    """
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT
                        relationship_id,
                        relationship_type,
                        source_kind,
                        source_id,
                        source_revision_id,
                        target_kind,
                        target_id,
                        target_revision_id,
                        authoring_party_id,
                        recorded_at,
                        semantic_role
                    FROM Relationships
                    WHERE relationship_id = :rid
                    """
                ),
                {"rid": relationship_id},
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 24: Backlink bidirectionality for planning nodes
@given(graph=_relationship_graph())
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup (fresh SQLite file, two schema installs, three
    # Party rows, one role assignment, plus a handful of INSERTs and
    # one backlink query per Relationship) is more expensive than a
    # pure in-memory property test. The setup is well under the 2000
    # ms deadline locally but we suppress the data-generation health
    # check so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_backlink_bidirectionality_planning(graph: dict) -> None:
    """For every Relationship in the generated Slice 1 + Slice 2 graph,
    the target-side backlink query and the source-side outbound query
    return the same Relationship Identity and the same attribute
    values, including the additive AD-WS-17 ``semantic_role`` column.

    **Validates: Requirements 1.5, 15.1, 15.2, 15.4, 15.6, 20.9**
    """
    relationships: list[dict] = graph["relationships"]

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop24_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh services per case so cross-case
        # :class:`IdentityService` and :class:`AuditLog` state cannot
        # bleed across cases. The pinned :class:`FixedClock` makes the
        # role-assignment ``effective_start`` and every
        # :meth:`AuthorizationService.evaluate` ``at`` deterministic
        # for shrinking diagnostics.
        clock = FixedClock(_FIXED_NOW)
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        authorization_service = AuthorizationService(
            clock=clock,
            audit_log=audit_log,
            identity_service=identity_service,
        )
        navigator = ProvenanceNavigator(
            clock=clock,
            authorization_service=authorization_service,
        )

        try:
            # 1. Seed all FK target Parties (requester, assigning
            #    authority, authoring Party). One transaction keeps
            #    the FK targets visible to every later write.
            with engine.begin() as conn:
                _seed_party(
                    conn,
                    party_id=_REQUESTER_PARTY_ID,
                    display="Property 24 Requester",
                )
                _seed_party(
                    conn,
                    party_id=_ASSIGNING_AUTHORITY_ID,
                    display="Property 24 Assigning Authority",
                )
                _seed_party(
                    conn,
                    party_id=_AUTHORING_PARTY_ID,
                    display="Property 24 Authoring Party",
                )

            # 2. Grant the requesting Party wildcard ``view`` authority.
            #    The wildcard scope ``"*"`` covers every source
            #    endpoint's ``source_id`` via
            #    :meth:`AuthorizationService._scope_covers`, so the
            #    Party holds view authority on every Relationship and
            #    every source endpoint in the graph by construction —
            #    the property's precondition. The action-prefix
            #    fallback in
            #    :func:`walking_slice.authorization._required_authority`
            #    maps every ``view.<planning_kind>`` action to the
            #    ``view`` authority, so no additional per-kind wiring
            #    is required for the property to hold across the eight
            #    Slice 2 source kinds.
            assign_request = AssignRoleRequest(
                party_id=_REQUESTER_PARTY_ID,
                role_name="bidirectional_reviewer",
                scope="*",
                authorities_granted=("view",),
                effective_start=_FIXED_NOW - timedelta(days=1),
                effective_end=None,
                assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
            )
            with engine.begin() as conn:
                authorization_service.assign_role(conn, assign_request)

            # 3. Insert every Relationship row. One transaction per
            #    case is fine because no per-Relationship invariants
            #    depend on transaction boundaries — the
            #    ``Relationships`` table is append-only by trigger
            #    regardless of how rows arrive, and AD-WS-17's
            #    ``semantic_role`` is part of the row at INSERT time.
            with engine.begin() as conn:
                for relationship in relationships:
                    _insert_relationship(conn, relationship)

            # 4. For every Relationship ``R`` in the graph, run both
            #    direction queries and assert bidirectional equality.
            #
            #    Per-Relationship sub-asserts:
            #
            #    (a) backlink query returns ``R`` with attributes
            #        equal to the inserted row;
            #    (b) outbound query returns ``R`` with attributes
            #        equal to the inserted row;
            #    (c) the backlink-side projection and the
            #        outbound-side projection of ``R`` are
            #        byte-equivalent — including the additive
            #        AD-WS-17 ``semantic_role`` column.
            for relationship in relationships:
                target_id = relationship["target_id"]
                target_revision_id = relationship["target_revision_id"]
                source_id = relationship["source_id"]
                source_revision_id = relationship["source_revision_id"]
                expected_relationship_id = relationship["relationship_id"]

                # ---- Backlink (target → source) direction ----
                with engine.connect() as conn:
                    page = navigator.list_backlinks(
                        conn,
                        target_id=target_id,
                        target_revision_id=target_revision_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_FIXED_NOW,
                    )

                # Requirement 15.1 sanity — the navigator never
                # returns more entries than the page cap. The graph
                # has at most 6 Relationships so this is loose, but
                # asserting it here closes the loop on the page-cap
                # reference (Requirement 15.1 constrains the page to
                # 500 entries).
                assert len(page.entries) <= BACKLINK_PAGE_LIMIT, (
                    "BacklinkPage exceeded the "
                    f"{BACKLINK_PAGE_LIMIT}-entry cap "
                    f"(returned {len(page.entries)}). Requirement "
                    "15.1 bounds each response."
                )

                backlink_matches = [
                    entry
                    for entry in page.entries
                    if entry.relationship_id == expected_relationship_id
                ]
                assert len(backlink_matches) == 1, (
                    "Backlink query for target "
                    f"(target_id={target_id!r}, "
                    f"target_revision_id={target_revision_id!r}, "
                    f"target_kind={relationship['target_kind']!r}) "
                    "did not return exactly one entry for "
                    f"relationship_id={expected_relationship_id!r}; "
                    f"got {len(backlink_matches)}. Requirement 1.5 / "
                    "15.2 require the same Relationship Identity to "
                    "surface from the backlink direction."
                )
                backlink_entry = backlink_matches[0]

                # Requirement 15.2 — every attribute on the returned
                # BacklinkEntry must equal the inserted row.
                _assert_entry_matches_row(
                    backlink_entry,
                    relationship,
                    direction="backlink",
                )

                # AD-WS-17 — the ``semantic_role`` column round-trips
                # byte-equivalently. The :class:`BacklinkEntry` value
                # object pre-dates AD-WS-17 so the round-trip is
                # asserted against the underlying ``Relationships``
                # row resolved from the navigator's returned
                # ``relationship_id``. AD-WS-4 immutability guarantees
                # the row's ``semantic_role`` is the value inserted
                # in step 3.
                backlink_row = _fetch_relationship_row(
                    engine, relationship_id=backlink_entry.relationship_id
                )
                assert backlink_row["semantic_role"] == relationship[
                    "semantic_role"
                ], (
                    "[backlink] semantic_role diverged for "
                    f"relationship_id={expected_relationship_id!r}: "
                    f"row={backlink_row['semantic_role']!r}, "
                    f"expected={relationship['semantic_role']!r}. "
                    "AD-WS-17 / Requirement 15.2 require the additive "
                    "semantic_role column to round-trip byte-"
                    "equivalently."
                )

                # ---- Outbound (source → target) direction ----
                outbound_rows = _fetch_outbound(
                    engine,
                    source_id=source_id,
                    source_revision_id=source_revision_id,
                )
                outbound_matches = [
                    row
                    for row in outbound_rows
                    if row["relationship_id"] == expected_relationship_id
                ]
                assert len(outbound_matches) == 1, (
                    "Outbound query for source "
                    f"(source_id={source_id!r}, "
                    f"source_revision_id={source_revision_id!r}, "
                    f"source_kind={relationship['source_kind']!r}) "
                    "did not return exactly one entry for "
                    f"relationship_id={expected_relationship_id!r}; "
                    f"got {len(outbound_matches)}. Requirement 1.5 "
                    "requires the same Relationship Identity to "
                    "surface from the source direction."
                )
                outbound_row = outbound_matches[0]

                _assert_row_matches_inserted(
                    outbound_row,
                    relationship,
                    direction="outbound",
                )

                # ---- Bidirectional equality ----
                # The property's "if and only if and identical
                # attributes" clause: every Requirement 15.2 attribute
                # — including the additive AD-WS-17 ``semantic_role``
                # column — must agree across the two directions.
                #
                # The backlink side projects through
                # :class:`BacklinkEntry`; the outbound side projects
                # through the raw row. Both reference the same single
                # immutable ``Relationships`` row (AD-WS-4) so the
                # projection's view of every column is identical to
                # the row's view of every column. Asserting both
                # equal the inserted descriptor (above) plus asserting
                # the two relationship_ids agree (here) closes the
                # bidirectional-equality contract for Property 24.
                assert (
                    backlink_entry.relationship_id
                    == outbound_row["relationship_id"]
                ), (
                    "Bidirectional Relationship Identity mismatch: "
                    f"backlink={backlink_entry.relationship_id!r}, "
                    f"outbound={outbound_row['relationship_id']!r}. "
                    "Property 24 / Requirement 1.5 require the same "
                    "Relationship Identity from both directions."
                )
                assert (
                    backlink_row["semantic_role"]
                    == outbound_row["semantic_role"]
                ), (
                    "Bidirectional semantic_role mismatch for "
                    f"relationship_id={expected_relationship_id!r}: "
                    f"backlink={backlink_row['semantic_role']!r}, "
                    f"outbound={outbound_row['semantic_role']!r}. "
                    "AD-WS-17 / Requirement 15.2 require identical "
                    "semantic_role values from both directions."
                )
                # The remaining schema columns must also agree across
                # both directions — re-projecting the outbound row
                # through :class:`BacklinkEntry` and asserting
                # equality is the same shape Slice 1 Property 3 uses,
                # extended here to confirm the Slice 2 planning kinds
                # round-trip identically.
                outbound_entry = BacklinkEntry(
                    relationship_id=outbound_row["relationship_id"],
                    relationship_type=outbound_row["relationship_type"],
                    source_id=outbound_row["source_id"],
                    source_kind=outbound_row["source_kind"],
                    source_revision_id=outbound_row["source_revision_id"],
                    authoring_party_id=outbound_row["authoring_party_id"],
                    recorded_at=outbound_row["recorded_at"],
                )
                assert backlink_entry == outbound_entry, (
                    "Bidirectional projection mismatch for "
                    f"relationship_id={expected_relationship_id!r}: "
                    f"backlink={backlink_entry!r}, "
                    f"outbound={outbound_entry!r}. Property 24 / "
                    "Requirement 15.2 require identical attribute "
                    "values from both directions across the full "
                    "Slice 1 + Slice 2 endpoint surface."
                )
        finally:
            engine.dispose()


def _assert_entry_matches_row(
    entry: BacklinkEntry,
    relationship: dict,
    *,
    direction: str,
) -> None:
    """Assert every Requirement-15.2 attribute on ``entry`` matches the inserted row.

    The helper takes ``direction`` for diagnostic context so a
    falsification surfaces "backlink direction diverged on
    ``source_kind``" rather than an opaque equality failure on the
    whole dataclass. Each per-attribute assertion is independent so
    the first divergence is the one reported.

    The additive AD-WS-17 ``semantic_role`` column is *not* asserted
    here because the :class:`BacklinkEntry` value object pre-dates
    AD-WS-17 and does not carry it; the round-trip for that column is
    asserted separately against the underlying immutable row.
    """
    expected_relationship_id = relationship["relationship_id"]

    assert entry.relationship_id == expected_relationship_id, (
        f"[{direction}] relationship_id diverged: "
        f"entry={entry.relationship_id!r}, "
        f"expected={expected_relationship_id!r}."
    )
    assert entry.relationship_type == relationship["relationship_type"], (
        f"[{direction}] relationship_type diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"entry={entry.relationship_type!r}, "
        f"expected={relationship['relationship_type']!r}."
    )
    assert entry.source_kind == relationship["source_kind"], (
        f"[{direction}] source_kind diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"entry={entry.source_kind!r}, "
        f"expected={relationship['source_kind']!r}."
    )
    assert entry.source_id == relationship["source_id"], (
        f"[{direction}] source_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"entry={entry.source_id!r}, "
        f"expected={relationship['source_id']!r}."
    )
    assert entry.source_revision_id == relationship["source_revision_id"], (
        f"[{direction}] source_revision_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"entry={entry.source_revision_id!r}, "
        f"expected={relationship['source_revision_id']!r}."
    )
    assert entry.authoring_party_id == _AUTHORING_PARTY_ID, (
        f"[{direction}] authoring_party_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"entry={entry.authoring_party_id!r}, "
        f"expected={_AUTHORING_PARTY_ID!r}."
    )
    assert entry.recorded_at == relationship["recorded_at"], (
        f"[{direction}] recorded_at diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"entry={entry.recorded_at!r}, "
        f"expected={relationship['recorded_at']!r}."
    )


def _assert_row_matches_inserted(
    row: dict,
    relationship: dict,
    *,
    direction: str,
) -> None:
    """Assert every column on the outbound ``row`` matches the inserted row.

    Mirrors :func:`_assert_entry_matches_row` for the outbound
    direction which projects through a raw ``Relationships`` row
    rather than a :class:`BacklinkEntry`. The raw projection lets the
    property verify the additive AD-WS-17 ``semantic_role`` column
    alongside the Slice 1 attributes; the helper takes ``direction``
    for diagnostic context.
    """
    expected_relationship_id = relationship["relationship_id"]

    assert row["relationship_id"] == expected_relationship_id, (
        f"[{direction}] relationship_id diverged: "
        f"row={row['relationship_id']!r}, "
        f"expected={expected_relationship_id!r}."
    )
    assert row["relationship_type"] == relationship["relationship_type"], (
        f"[{direction}] relationship_type diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['relationship_type']!r}, "
        f"expected={relationship['relationship_type']!r}."
    )
    assert row["source_kind"] == relationship["source_kind"], (
        f"[{direction}] source_kind diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['source_kind']!r}, "
        f"expected={relationship['source_kind']!r}."
    )
    assert row["source_id"] == relationship["source_id"], (
        f"[{direction}] source_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['source_id']!r}, "
        f"expected={relationship['source_id']!r}."
    )
    assert row["source_revision_id"] == relationship["source_revision_id"], (
        f"[{direction}] source_revision_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['source_revision_id']!r}, "
        f"expected={relationship['source_revision_id']!r}."
    )
    assert row["target_kind"] == relationship["target_kind"], (
        f"[{direction}] target_kind diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['target_kind']!r}, "
        f"expected={relationship['target_kind']!r}."
    )
    assert row["target_id"] == relationship["target_id"], (
        f"[{direction}] target_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['target_id']!r}, "
        f"expected={relationship['target_id']!r}."
    )
    assert row["target_revision_id"] == relationship["target_revision_id"], (
        f"[{direction}] target_revision_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['target_revision_id']!r}, "
        f"expected={relationship['target_revision_id']!r}."
    )
    assert row["authoring_party_id"] == _AUTHORING_PARTY_ID, (
        f"[{direction}] authoring_party_id diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['authoring_party_id']!r}, "
        f"expected={_AUTHORING_PARTY_ID!r}."
    )
    assert row["recorded_at"] == relationship["recorded_at"], (
        f"[{direction}] recorded_at diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['recorded_at']!r}, "
        f"expected={relationship['recorded_at']!r}."
    )
    assert row["semantic_role"] == relationship["semantic_role"], (
        f"[{direction}] semantic_role diverged for "
        f"relationship_id={expected_relationship_id!r}: "
        f"row={row['semantic_role']!r}, "
        f"expected={relationship['semantic_role']!r}. AD-WS-17 / "
        "Requirement 15.2 require the additive semantic_role column "
        "to round-trip byte-equivalently from the source direction."
    )
