# Feature: fourth-walking-slice, Property 55: Backlink bidirectionality for outcome-measurement Resources
"""Property 55 — Backlink bidirectionality for outcome-measurement Resources (task 15.10).

**Property 55: Backlink bidirectionality for outcome-measurement Resources**

*For all* Relationships ``R`` recorded between outcome-measurement
Resources or Records, between outcome-measurement Records and Slice 3
execution Records or produced Deliverable Revisions, between
outcome-measurement entities and Slice 2 planning Resources, or between
outcome-measurement entities and Slice 1 Resources, and *for all*
requesting Parties ``P`` holding view authority on both ``R`` and its
source endpoint, the
:class:`~walking_slice.provenance.ProvenanceNavigator` returns ``R``
from the target's backlink query if and only if ``R`` is returned from
the source's outbound query, and the Relationship attribute values
(Relationship Identity, Relationship Type, ``semantic_role``, source
endpoint Identity, source endpoint Type, source endpoint Revision
Identity when applicable, authoring Party Identity, ``recorded_at``)
returned from both directions are identical. Returning a backlink grants
``P`` no authority on the source endpoint, the Relationship Identity, or
any traversed Record, and the response is bounded to at most 500
Relationships with a continuation reference whose presence and values do
not vary based on Relationships ``P`` lacks authority to know.

**Validates: Requirements 43.5, 56.1, 56.2, 56.4, 56.6, 61.10**

Strategy
========

Each Hypothesis case draws a *relationship graph* whose endpoints span
the full Slice 1 + Slice 2 + Slice 3 + Slice 4 surface:

- 1..3 *target* endpoint descriptors. Each carries a fresh UUIDv7
  ``id``, a fresh ``revision_id`` (or ``None``), and a ``kind`` drawn
  from the union of the Slice 1 source kinds, the Slice 2 planning
  kinds, the Slice 3 execution + produced-Deliverable kinds, and the
  seven Slice 4 outcome-measurement kinds added to
  :data:`walking_slice.provenance._AUTHORIZED_SOURCE_KINDS` by task
  10.2.
- 1..3 *source* endpoint descriptors with the same shape.
- 1..6 *relationship* descriptors. Each picks one source and one target
  from the pools, draws a ``relationship_type`` from the six-member set
  permitted by the post-Slice-2 CHECK constraint on
  ``Relationships.relationship_type`` (``Supports``, ``Contradicts``,
  ``Derived From``, ``Addresses``, ``Supersedes``, ``Relates To``),
  draws a ``semantic_role`` value (``None``, one of the AD-WS-35 Slice 4
  markers ``measurement_basis`` / ``observation_basis`` /
  ``assessment_basis`` / ``review_assessment`` / ``review_completion`` /
  ``review_deliverable``, the AD-WS-26 Slice 3 markers, the AD-WS-17
  ``review`` marker, or a small curated set of arbitrary discriminators
  so the round-trip is exercised across more than just the canonical
  literals), draws a per-relationship timestamp offset (in seconds), and
  mints a fresh ``relationship_id``.

Per case the test:

1. Spins up a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state cannot
   contaminate the bidirectionality assertions (design §"Testing
   Strategy" — "Each property and example test gets a fresh SQLite
   database"). The Slice 1 schema (with the additive
   ``Relationships.semantic_role`` column from Slice 2 AD-WS-19), the
   Slice 2 planning schema, the Slice 3 execution schema, the Slice 3
   deliverable schema, and the Slice 4 outcome schema (task 1.2 of this
   slice) are all installed so the navigator's Slice-4-aware backlink
   coverage (task 10.2) is exercised end-to-end.
2. Seeds the requesting Party (FK target for ``Role_Assignments.party_id``
   and the :meth:`AuthorizationService.evaluate` audit row), the
   assigning authority Party, the authoring Party referenced on every
   inserted Relationship row, and a *restricted* Party who holds no
   authority at all.
3. Assigns one wildcard-scope ``view`` role to the requesting Party so
   the Party holds view authority on every Relationship and every source
   endpoint in the graph — satisfying the property's "for all requesting
   Parties ``P`` holding view authority on both ``R`` and its source
   endpoint" precondition by construction. The wildcard scope ``'*'``
   covers every endpoint's identity via
   :meth:`AuthorizationService._scope_covers`, and the action-prefix
   fallback in :func:`walking_slice.authorization._required_authority`
   maps every ``view.<source_kind>`` action to the ``view`` authority —
   so no per-kind authorization wiring is required for the property to
   hold across the seven Slice 4 source kinds added to
   :data:`walking_slice.provenance._AUTHORIZED_SOURCE_KINDS` by task
   10.2.
4. Inserts every Relationship row directly into the ``Relationships``
   table, including the additive ``semantic_role`` column. Direct INSERT
   (rather than routing through any Outcome_Service) lets the strategy
   fabricate arbitrary ``(source_kind, target_kind, relationship_type,
   semantic_role, recorded_at)`` combinations independent of the slice's
   natural pipelines. The ``Relationships`` schema has no FK constraints
   on ``source_id`` / ``target_id`` / ``source_revision_id`` /
   ``target_revision_id`` (only on ``authoring_party_id``) so the
   fabricated identities round-trip cleanly across the full
   Slice 1 + Slice 2 + Slice 3 + Slice 4 surface.
5. For every persisted Relationship ``R``:

   - Calls :meth:`ProvenanceNavigator.list_backlinks` with
     ``(target_id=R.target_id, target_revision_id=R.target_revision_id,
     party_id=P)`` and asserts ``R.relationship_id`` is present in the
     returned :class:`~walking_slice.provenance.BacklinkPage.entries`,
     with every Requirement 56.2 attribute byte-equivalent to the
     inserted row.
   - Issues the outbound query directly against ``Relationships``
     filtered by ``source_id`` (and ``source_revision_id`` when
     applicable). The outbound query is intentionally a plain SELECT —
     :class:`~walking_slice.provenance.ProvenanceNavigator` does not
     expose a ``list_outbound`` method (task 10.2 scoped only the
     backlink-coverage extension), and the property statement's "from
     the source's outbound query" is read here as "the canonical truth
     about outbound Relationships, which the ``Relationships`` table is"
     — mirroring the Slice 1 Property 3, Slice 2 Property 24, and Slice 3
     Property 39 idioms.
   - Asserts both directions report the same Relationship Identity and
     the same attribute values, including the additive AD-WS-35
     ``semantic_role`` column.

6. Confirms Requirement 56.6 / 43.5 / 56.4: the authorized backlink page
   is bounded at :data:`BACKLINK_PAGE_LIMIT` and its continuation cursor
   is ``None`` when every visible Relationship fits on one page; the
   *restricted* Party — who lacks view authority on every endpoint —
   receives an empty page with a ``None`` cursor for the same targets, so
   the continuation reference's presence does not vary based on
   Relationships the Party lacks authority to know. The requesting Party
   receives only the wildcard ``view`` grant and exercises no other
   authority, so returning a backlink grants no authority on the source
   endpoint, the Relationship Identity, or any traversed Record.

Requirement coverage notes
==========================

- **43.5** — Returning a backlink does not grant the requesting Party
  any authority on the source endpoint or any traversed Record. The test
  holds this clause by construction: the wildcard ``view`` grant is the
  *only* authority the requesting Party receives, and no other authority
  is exercised in the property body; the restricted Party receives
  nothing and observes nothing.
- **56.1** — The navigator returns every inbound Relationship for which
  the requesting Party holds applicable view authority on both the
  Relationship and its source endpoint, in deterministic ordering, within
  the 500-entry page cap.
- **56.2** — Every Requirement 56.2 attribute is asserted equal across
  both directions, including the additive AD-WS-35 ``semantic_role``
  column. A target-side bug that swapped ``source_kind`` between two
  backlinks pointing at the same target, or an AD-WS-35 bug that
  overwrote ``semantic_role`` on read for a Slice 4 source kind, would
  falsify the property immediately.
- **56.4** — Returning a backlink does not grant the requesting Party any
  view, modify, assess_outcome, issue_outcome_review, define_measurement,
  or record_measurement authority. A bug that silently auto-granted any
  other authority would be invisible at this layer but is caught by
  Property 47 (outcome-record authority correctness) — Property 55
  ensures the backlink projection itself does not leak new authority
  claims.
- **56.6** — Backlink responses are bounded at 500 Relationships and the
  continuation cursor's presence does not vary with restricted
  Relationships. The graphs here sit far under the page cap so
  ``len(page.entries) <= BACKLINK_PAGE_LIMIT`` confirms the bound and the
  authorized page's cursor is ``None``; the restricted Party's page is
  empty with a ``None`` cursor, so the continuation reference is
  identical whether or not hidden Relationships exist.
- **61.10** — The property is the Slice 4 enumeration of the cross-slice
  bidirectionality verification target: backlink and outbound projections
  agree on every attribute, including the additive AD-WS-35
  ``semantic_role`` column, across the outcome-measurement node kinds.
- **Hypothesis profile** — ``max_examples=100, deadline=2000`` per
  Requirement 61.15 / AD-WS-13. The ``too_slow`` health-check is
  suppressed because per-case setup spins up a fresh SQLite file with
  five schema installs.
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
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.identity import IdentityService
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.provenance import (
    BACKLINK_PAGE_LIMIT,
    BacklinkEntry,
    ProvenanceNavigator,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the four Parties referenced by FK constraints.
#
# - ``_REQUESTER_PARTY_ID`` is the requesting Party ``P`` from the
#   property statement; it receives the wildcard ``view`` role and is
#   passed to :meth:`ProvenanceNavigator.list_backlinks` on every call.
# - ``_ASSIGNING_AUTHORITY_ID`` is the actor on the role-assignment
#   audit row.
# - ``_AUTHORING_PARTY_ID`` is recorded on every inserted Relationship
#   row (``Relationships.authoring_party_id`` is FK to ``Parties``).
# - ``_RESTRICTED_PARTY_ID`` holds no authority at all; it exercises the
#   Requirement 56.6 / 43.5 clause that the continuation reference's
#   presence does not vary with Relationships the Party cannot see.
# ---------------------------------------------------------------------------


_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000000002"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000003"
_RESTRICTED_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000004"


# The :class:`FixedClock` instant. Every Relationship's ``recorded_at``
# is derived from this baseline plus a per-relationship offset so two
# Relationships in the same case do not share a timestamp.
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)


# The six Relationship types permitted by the post-Slice-2 CHECK
# constraint on ``Relationships.relationship_type``. Slice 4 AD-WS-35
# reuses these same six values for the Slice 4 Relationships, so the
# strategy samples all six — any bug that drops a value from one
# direction of the bidirectional surface falsifies the property
# immediately.
_RELATIONSHIP_TYPES: Final[tuple[str, ...]] = (
    "Supports",
    "Contradicts",
    "Derived From",
    "Addresses",
    "Supersedes",
    "Relates To",
)


# Endpoint kinds drawn from the slice's full Slice 1 + Slice 2 + Slice 3
# + Slice 4 surface named by Property 55. Both ``source_kind`` and
# ``target_kind`` columns are free-form TEXT in the ``Relationships``
# schema so the strategy is free to draw any combination.
_ENDPOINT_KINDS: Final[tuple[str, ...]] = (
    # Slice 1 endpoint kinds.
    "document_revision",
    "region_occurrence",
    "finding_revision",
    "recommendation_revision",
    "decision",
    "trail_step",
    # Slice 2 planning endpoint kinds.
    "objective_revision",
    "intended_outcome_revision",
    "project_revision",
    "deliverable_expectation_revision",
    "activity_plan",
    "plan_revision",
    "plan_review_revision",
    "plan_approval",
    # Slice 3 execution + produced-Deliverable endpoint kinds.
    "work_assignment_record",
    "work_event_record",
    "time_entry_record",
    "deliverable_resource",
    "deliverable_revision",
    "deliverable_production_record",
    "milestone_acceptance_record",
    "completion_record",
    # Slice 4 outcome-measurement endpoint kinds (task 10.2 additive
    # coverage extension; Requirement 56.1, 56.2, 56.4, 56.6, 43.5). The
    # Slice 1 prefix-based ``_required_authority`` fallback maps every
    # ``view.*`` action to the ``view`` authority, so no new
    # authorization mapping is required.
    "measurement_definition",
    "measurement_definition_revision",
    "measurement_record",
    "observed_outcome",
    "observed_outcome_revision",
    "success_condition_assessment_record",
    "outcome_review_record",
)


# Curated ``semantic_role`` values the strategy draws. ``None`` is the
# default for every Slice 1 row, every ``Addresses`` row (Slice 4
# ``semantic_role IS NULL``), and any row whose mapping records ``NULL``.
# The six Slice 4 markers are the AD-WS-35 ``Cites`` discriminators:
# ``measurement_basis`` (Measurement Record → Measurement Definition
# Revision), ``observation_basis`` (Observed Outcome → Measurement
# Record), ``assessment_basis`` (Assessment → Observed Outcome Revision),
# ``review_assessment`` / ``review_completion`` / ``review_deliverable``
# (Outcome Review → Assessment / Completion / produced Deliverable). The
# AD-WS-17 ``review`` and AD-WS-26 Slice 3 markers and a small set of
# arbitrary strings exercise the round-trip across non-canonical values
# so any future bug that silently normalizes ``semantic_role`` on read
# would falsify the property. The schema column is ``TEXT NULL`` with no
# CHECK, so any string round-trips byte-equivalently when the
# bidirectional projections are correct.
_SEMANTIC_ROLES: Final[tuple[Optional[str], ...]] = (
    None,
    # Slice 4 AD-WS-35 markers.
    "measurement_basis",
    "observation_basis",
    "assessment_basis",
    "review_assessment",
    "review_completion",
    "review_deliverable",
    # Prior-slice markers.
    "review",
    "assignee",
    "work_event",
    "time_entry",
    "production_source",
    # Arbitrary discriminators.
    "custom-role-1",
    "custom-role-2",
)


def _fresh_uuid7() -> str:
    """Mint one fresh UUIDv7 string.

    Used as a one-off identity for every endpoint (target and source)
    and every Relationship in the graph. Each call returns a fresh value
    so generated endpoints do not collide across cases or across
    endpoints within one case.
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
# ---------------------------------------------------------------------------


@st.composite
def _endpoint(draw) -> dict:
    """Draw one endpoint descriptor (target or source).

    Returns a dict with:

    - ``id`` (str): fresh UUIDv7.
    - ``revision_id`` (str | None): fresh UUIDv7 or ``None``. Drawn
      ``None`` for some endpoints so the property exercises both the
      "endpoint carries a Revision Identity" and "endpoint is a Resource
      header" branches of the backlink query.
    - ``kind`` (str): one of the Slice 1 + Slice 2 + Slice 3 + Slice 4
      endpoint kinds.
    """
    return {
        "id": _fresh_uuid7(),
        "revision_id": draw(st.one_of(st.none(), st.builds(_fresh_uuid7))),
        "kind": draw(st.sampled_from(_ENDPOINT_KINDS)),
    }


@st.composite
def _relationship_graph(draw) -> dict:
    """Draw one relationship graph: target pool + source pool + relationships.

    Returns a dict with ``targets`` (1..3), ``sources`` (1..3), and
    ``relationships`` (1..6). Each relationship dict carries the eight
    schema columns (including the additive AD-WS-35 ``semantic_role``)
    plus a precomputed ISO-8601 ``recorded_at`` string. The
    ``recorded_at`` offset is per-relationship-distinct so the backlink
    ``ORDER BY (recorded_at, relationship_id)`` clause has a stable,
    observable ordering.
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
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas.

    Installs the five cumulative schemas — Slice 1 (with the additive
    ``Relationships.semantic_role`` column added by Slice 2 AD-WS-19),
    Slice 2 planning, Slice 3 execution, Slice 3 deliverable, and Slice 4
    outcome — so the property exercises the full Slice 1 + Slice 2 +
    Slice 3 + Slice 4 relationship surface named in the property
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
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    create_outcome_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Direct INSERT helper.
# ---------------------------------------------------------------------------


def _insert_relationship(connection: Connection, relationship: dict) -> None:
    """Insert one Relationship row into the open transaction.

    The ``semantic_role`` value (``None`` or a non-empty string) is bound
    through SQLAlchemy's named-parameter mechanism so SQLite stores
    ``NULL`` and the AD-WS-35 markers literally, preserving the
    byte-equivalence the property asserts.
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
# :class:`~walking_slice.provenance.ProvenanceNavigator` does not expose
# a ``list_outbound`` method (task 10.2 scoped only the additive backlink
# coverage extension). The property's "from the source's outbound query"
# is asserted here against the canonical truth — a direct SELECT on
# ``Relationships`` filtered by ``source_id`` (and, when applicable,
# ``source_revision_id``). The select also returns the additive AD-WS-35
# ``semantic_role`` column so the property can verify Requirement 56.2's
# "identical Relationship attribute values" clause covers the new column.
# ---------------------------------------------------------------------------


def _fetch_outbound(
    engine: Engine,
    *,
    source_id: str,
    source_revision_id: Optional[str],
) -> list[dict]:
    """Return every Relationship sourced from ``(source_id, source_revision_id)``.

    When ``source_revision_id`` is ``None`` the query matches any
    ``source_revision_id`` (including ``NULL``), mirroring the backlink
    query's behavior for ``target_revision_id=None``. When
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


def _fetch_relationship_row(engine: Engine, *, relationship_id: str) -> dict:
    """Return the full ``Relationships`` row for ``relationship_id``.

    The row is read on a fresh connection so the property test observes
    the persisted state independently of the navigator's connection. Used
    to surface the ``semantic_role`` value alongside the navigator's
    :class:`~walking_slice.provenance.BacklinkEntry` projection — the
    value object does not yet expose the new column, but the underlying
    immutable row (AD-WS-4 / AD-WS-36) is the canonical source of truth
    for every attribute including AD-WS-35's ``semantic_role``.
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


# Feature: fourth-walking-slice, Property 55: Backlink bidirectionality for outcome-measurement Resources
@given(graph=_relationship_graph())
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup (fresh SQLite file, five schema installs, four
    # Party rows, one role assignment, plus a handful of INSERTs and one
    # backlink query per Relationship) is more expensive than a pure
    # in-memory property test. The setup is well under the 2000 ms
    # deadline locally but we suppress the data-generation health check
    # so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_backlink_bidirectionality_outcome(graph: dict) -> None:
    """For every Relationship in the generated Slice 1 + Slice 2 + Slice 3
    + Slice 4 graph, the target-side backlink query and the source-side
    outbound query return the same Relationship Identity and the same
    attribute values, including the additive AD-WS-35 ``semantic_role``
    column; the response is bounded and its continuation reference does
    not vary with Relationships the Party cannot see.

    **Validates: Requirements 43.5, 56.1, 56.2, 56.4, 56.6, 61.10**
    """
    relationships: list[dict] = graph["relationships"]

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop55_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh services per case so cross-case :class:`IdentityService`
        # and :class:`AuditLog` state cannot bleed across cases. The
        # pinned :class:`FixedClock` makes the role-assignment
        # ``effective_start`` and every
        # :meth:`AuthorizationService.evaluate` ``at`` deterministic for
        # shrinking diagnostics.
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
            #    authority, authoring Party, restricted Party).
            with engine.begin() as conn:
                _seed_party(
                    conn,
                    party_id=_REQUESTER_PARTY_ID,
                    display="Property 55 Requester",
                )
                _seed_party(
                    conn,
                    party_id=_ASSIGNING_AUTHORITY_ID,
                    display="Property 55 Assigning Authority",
                )
                _seed_party(
                    conn,
                    party_id=_AUTHORING_PARTY_ID,
                    display="Property 55 Authoring Party",
                )
                _seed_party(
                    conn,
                    party_id=_RESTRICTED_PARTY_ID,
                    display="Property 55 Restricted Party",
                )

            # 2. Grant the requesting Party wildcard ``view`` authority.
            #    The wildcard scope ``"*"`` covers every source
            #    endpoint's ``source_id`` via
            #    :meth:`AuthorizationService._scope_covers`, so the Party
            #    holds view authority on every Relationship and every
            #    source endpoint in the graph by construction — the
            #    property's precondition. The action-prefix fallback in
            #    :func:`walking_slice.authorization._required_authority`
            #    maps every ``view.<source_kind>`` action to the ``view``
            #    authority, so no additional per-kind wiring is required
            #    for the property to hold across the seven Slice 4 source
            #    kinds added to
            #    :data:`walking_slice.provenance._AUTHORIZED_SOURCE_KINDS`
            #    by task 10.2. The restricted Party deliberately receives
            #    no role assignment.
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

            # 3. Insert every Relationship row.
            with engine.begin() as conn:
                for relationship in relationships:
                    _insert_relationship(conn, relationship)

            # 4. For every Relationship ``R`` in the graph, run both
            #    direction queries and assert bidirectional equality.
            for relationship in relationships:
                target_id = relationship["target_id"]
                target_revision_id = relationship["target_revision_id"]
                source_id = relationship["source_id"]
                source_revision_id = relationship["source_revision_id"]
                expected_relationship_id = relationship["relationship_id"]

                # ---- Backlink (target -> source) direction ----
                with engine.connect() as conn:
                    page = navigator.list_backlinks(
                        conn,
                        target_id=target_id,
                        target_revision_id=target_revision_id,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_FIXED_NOW,
                    )

                # Requirement 56.6 — the navigator never returns more
                # entries than the page cap, and the continuation cursor
                # is derived from the authorized projection *alone* (the
                # last visible Relationship's ``(recorded_at,
                # relationship_id)`` pair, or ``None`` when the
                # projection is empty). Asserting the cursor is a pure
                # function of ``page.entries`` is the foundation for the
                # "continuation reference whose presence and values do
                # not vary based on Relationships ``P`` lacks authority
                # to know" clause — see the restricted-Party check below.
                assert len(page.entries) <= BACKLINK_PAGE_LIMIT, (
                    "BacklinkPage exceeded the "
                    f"{BACKLINK_PAGE_LIMIT}-entry cap "
                    f"(returned {len(page.entries)}). Requirement 56.6 "
                    "bounds each response."
                )
                if page.entries:
                    last_entry = page.entries[-1]
                    assert page.cursor is not None, (
                        "Authorized backlink page with visible entries "
                        f"for target_id={target_id!r} carried a None "
                        "continuation cursor; Requirement 56.6 derives "
                        "the cursor from the authorized projection."
                    )
                    assert (
                        page.cursor.recorded_at == last_entry.recorded_at
                        and page.cursor.relationship_id
                        == last_entry.relationship_id
                    ), (
                        "Authorized backlink continuation cursor "
                        f"{page.cursor!r} for target_id={target_id!r} did "
                        "not match the last visible entry "
                        f"(recorded_at={last_entry.recorded_at!r}, "
                        f"relationship_id={last_entry.relationship_id!r}); "
                        "Requirement 56.6 requires the cursor to depend "
                        "only on the authorized projection."
                    )
                else:
                    assert page.cursor is None, (
                        "Authorized backlink page with no visible entries "
                        f"for target_id={target_id!r} carried a non-None "
                        f"continuation cursor {page.cursor!r}; Requirement "
                        "56.6 requires an empty projection to yield no "
                        "continuation reference."
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
                    f"target_kind={relationship['target_kind']!r}) did "
                    "not return exactly one entry for "
                    f"relationship_id={expected_relationship_id!r}; got "
                    f"{len(backlink_matches)}. Requirement 56.1 / 56.2 "
                    "require the same Relationship Identity to surface "
                    "from the backlink direction."
                )
                backlink_entry = backlink_matches[0]

                # Requirement 56.2 — every attribute on the returned
                # BacklinkEntry must equal the inserted row.
                _assert_entry_matches_row(
                    backlink_entry,
                    relationship,
                    direction="backlink",
                )

                # AD-WS-35 — the ``semantic_role`` column round-trips
                # byte-equivalently. The :class:`BacklinkEntry` value
                # object does not carry ``semantic_role`` so the
                # round-trip is asserted against the underlying immutable
                # ``Relationships`` row resolved from the navigator's
                # returned ``relationship_id``.
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
                    "AD-WS-35 / Requirement 56.2 require the additive "
                    "semantic_role column to round-trip byte-"
                    "equivalently across the Slice 4 source/target kinds."
                )

                # ---- Outbound (source -> target) direction ----
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
                    f"source_kind={relationship['source_kind']!r}) did "
                    "not return exactly one entry for "
                    f"relationship_id={expected_relationship_id!r}; got "
                    f"{len(outbound_matches)}. Requirement 56.1 requires "
                    "the same Relationship Identity to surface from the "
                    "source direction."
                )
                outbound_row = outbound_matches[0]

                _assert_row_matches_inserted(
                    outbound_row,
                    relationship,
                    direction="outbound",
                )

                # ---- Bidirectional equality ----
                assert (
                    backlink_entry.relationship_id
                    == outbound_row["relationship_id"]
                ), (
                    "Bidirectional Relationship Identity mismatch: "
                    f"backlink={backlink_entry.relationship_id!r}, "
                    f"outbound={outbound_row['relationship_id']!r}. "
                    "Property 55 / Requirement 56.1 require the same "
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
                    "AD-WS-35 / Requirement 56.2 require identical "
                    "semantic_role values from both directions."
                )
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
                    f"outbound={outbound_entry!r}. Property 55 / "
                    "Requirement 56.2 require identical attribute values "
                    "from both directions across the full Slice 1 + "
                    "Slice 2 + Slice 3 + Slice 4 endpoint surface."
                )

                # ---- Requirement 56.6 / 43.5 / 56.4: continuation
                #      reference does not vary with hidden Relationships,
                #      and a backlink grants no authority. The restricted
                #      Party (no role assignment) sees an empty page with
                #      a ``None`` cursor for the same target — identical
                #      to the page it would receive if the target had no
                #      Relationships at all — so the continuation
                #      reference's presence does not leak the existence of
                #      Relationships the Party lacks authority to know.
                with engine.connect() as conn:
                    restricted_page = navigator.list_backlinks(
                        conn,
                        target_id=target_id,
                        target_revision_id=target_revision_id,
                        party_id=_RESTRICTED_PARTY_ID,
                        at=_FIXED_NOW,
                    )
                assert restricted_page.entries == (), (
                    "Restricted Party (no view authority) received "
                    f"{len(restricted_page.entries)} backlink entries "
                    f"for target_id={target_id!r}; Requirement 43.5 / "
                    "56.4 require a backlink to grant no visibility on "
                    "Relationships the Party cannot view."
                )
                assert restricted_page.cursor is None, (
                    "Restricted Party received a non-None continuation "
                    f"cursor {restricted_page.cursor!r} for "
                    f"target_id={target_id!r}; Requirement 56.6 requires "
                    "the continuation reference's presence not to vary "
                    "with Relationships the Party lacks authority to know."
                )
        finally:
            engine.dispose()


def _assert_entry_matches_row(
    entry: BacklinkEntry,
    relationship: dict,
    *,
    direction: str,
) -> None:
    """Assert every Requirement-56.2 attribute on ``entry`` matches the inserted row.

    The helper takes ``direction`` for diagnostic context so a
    falsification surfaces "backlink direction diverged on
    ``source_kind``" rather than an opaque equality failure on the whole
    dataclass. Each per-attribute assertion is independent so the first
    divergence is the one reported.

    The additive AD-WS-35 ``semantic_role`` column is *not* asserted here
    because the :class:`BacklinkEntry` value object does not carry it; the
    round-trip for that column is asserted separately against the
    underlying immutable row.
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

    Mirrors :func:`_assert_entry_matches_row` for the outbound direction
    which projects through a raw ``Relationships`` row rather than a
    :class:`BacklinkEntry`. The raw projection lets the property verify
    the additive AD-WS-35 ``semantic_role`` column alongside the Slice 1
    attributes; the helper takes ``direction`` for diagnostic context.
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
        f"expected={relationship['semantic_role']!r}. AD-WS-35 / "
        "Requirement 56.2 require the additive semantic_role column to "
        "round-trip byte-equivalently from the source direction."
    )
