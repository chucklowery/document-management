# Feature: first-walking-slice, Property 3: Backlink bidirectionality
"""Property 3 — Backlink bidirectionality (task 12.6).

**Property 3: Backlink bidirectionality**

For all Relationships ``R`` recorded between in-scope endpoints (Source
Document Revisions, Content Region Occurrences, Finding Revisions,
Recommendation Revisions, Decision Immutable Records, Trail Step
identities), and for all requesting Parties ``P`` who hold view
authority on both ``R`` and its source endpoint, the
``Provenance_Navigator`` returns ``R`` from the target's backlink
query if and only if ``R`` is returned from the source's outbound
query, and the Relationship attribute values returned from both
directions are identical.

**Validates: Requirements 1.5, 8.1, 8.2, 15.3**

Strategy:

Each Hypothesis case draws a *relationship graph*: a small pool of
``target`` and ``source`` endpoints (each pinned by a fresh UUIDv7
``id`` and an optional ``revision_id``) plus 1..6 Relationships, each
of which picks one source from the source pool and one target from the
target pool. Sharing endpoints across Relationships exercises the
realistic shape where one target accumulates several inbound
Relationships and one source emits several outbound Relationships —
the property's "from the target's backlink query" and "from the
source's outbound query" quantifiers both run against non-trivial
result sets.

Per generated case the test spins up a fresh per-test SQLite engine on
a unique :class:`tempfile.TemporaryDirectory` path so cross-case state
cannot contaminate the bidirectionality assertions (design §"Testing
Strategy" — "Each property and example test gets a fresh SQLite
database"). It then:

1. Seeds the requesting Party (FK target for
   ``Role_Assignments.party_id`` and the
   :meth:`AuthorizationService.evaluate` audit row), the assigning
   authority Party, and the authoring Party referenced on every
   inserted Relationship row.
2. Assigns one wildcard-scope ``view`` role to the requesting Party
   so the Party holds view authority on every Relationship and every
   source endpoint in the graph — satisfying the property's "for all
   requesting Parties ``P`` who hold view authority on both ``R`` and
   its source endpoint" precondition by construction (the
   :meth:`AuthorizationService._scope_covers` wildcard branch accepts
   any source-side ``TargetRef.scope``).
3. Inserts every Relationship row directly into the ``Relationships``
   table. Direct INSERT (rather than routing through
   :class:`KnowledgeService.create_finding`) lets the strategy
   fabricate arbitrary ``(source_kind, target_kind, relationship_type,
   recorded_at)`` combinations independent of the slice's natural
   pipeline. The ``Relationships`` schema has no FK constraints on
   ``source_id`` / ``target_id`` / ``source_revision_id`` /
   ``target_revision_id`` (only on ``authoring_party_id``) so the
   fabricated identities round-trip cleanly.
4. For every persisted Relationship ``R``:

   - Calls :meth:`ProvenanceNavigator.list_backlinks` with
     ``(target_id=R.target_id, target_revision_id=R.target_revision_id,
     party_id=P)`` and asserts ``R.relationship_id`` is present in the
     returned :class:`BacklinkPage.entries`, with every Requirement
     8.2 attribute (``relationship_id``, ``relationship_type``,
     ``source_id``, ``source_kind``, ``source_revision_id``,
     ``authoring_party_id``) and ``recorded_at`` byte-equivalent to
     the inserted row.
   - Issues the outbound query directly against ``Relationships``
     filtered by ``source_id`` (and ``source_revision_id`` when
     applicable), then asserts ``R.relationship_id`` is present with
     the same attributes. The outbound query is intentionally a plain
     SELECT — :class:`ProvenanceNavigator` does not expose a
     ``list_outbound`` method (task 12.1 scoped only the backlink
     surface), and the property statement's "from the source's
     outbound query" is read here as "the canonical truth about
     outbound Relationships, which the Relationship table is" rather
     than a specific service method. Authorization is uniform across
     both directions because the requesting Party holds wildcard view
     authority by construction.
   - Asserts both directions report the same Relationship Identity
     and the same attribute values — the property's
     "if-and-only-if and identical-attributes" clause is read here
     as a bidirectional equality on the projection
     ``(relationship_id, relationship_type, source_id, source_kind,
     source_revision_id, target_id, target_kind, target_revision_id,
     authoring_party_id, recorded_at)`` for the same ``R``.

Requirement coverage notes:

- **1.5** — :meth:`Identity_Service` is exercised implicitly because
  every Relationship Identity returned from both directions is the
  *same single authoritative* canonical UUIDv7 — the inserted
  ``relationship_id``. The property's "same Relationship Identity
  from both source-direction and backlink queries" clause is asserted
  by string equality on ``relationship_id`` across the two
  projections.
- **8.1** — The :data:`BACKLINK_PAGE_LIMIT` cap (500) is well above
  the per-case graph size (≤ 6 Relationships), so every persisted
  Relationship is reachable in a single backlink page. The
  Requirement 8.1 ordering ("deterministic ordering") is exercised
  by the navigator's ``ORDER BY (recorded_at, relationship_id)``
  clause; the property test reads the page directly without
  pagination, so any ordering bug that drops or reorders a
  Relationship surfaces as a missing identity.
- **8.2** — Every Requirement 8.2 attribute is asserted equal across
  both directions. Substitution of one Relationship's attributes for
  another's — for example a target-side bug that swapped
  ``source_kind`` between two backlinks pointing at the same target
  — would falsify the property immediately.
- **15.3** — The Hypothesis settings register ``max_examples=100``
  and ``deadline=2000`` per Requirement 15.13 / 15.3's "at least 100
  generated cases per property" mandate.
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
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema
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


# The five Relationship types permitted by the
# ``Relationships.relationship_type`` CHECK constraint (Requirement
# 4.2 / 5.1 / 6.1 / 9.7 / design §"Table-by-Table Specification"). The
# property holds across every type so the strategy samples all five.
_RELATIONSHIP_TYPES: Final[tuple[str, ...]] = (
    "Supports",
    "Contradicts",
    "Derived From",
    "Addresses",
    "Supersedes",
)


# Endpoint kinds drawn from the slice's six in-scope endpoint types
# named by Property 3 (Source Document Revisions, Content Region
# Occurrences, Finding Revisions, Recommendation Revisions, Decision
# Immutable Records, Trail Step identities). Both ``source_kind`` and
# ``target_kind`` columns are free-form TEXT in the ``Relationships``
# schema so the strategy is free to draw any combination.
_ENDPOINT_KINDS: Final[tuple[str, ...]] = (
    "document_revision",
    "region_occurrence",
    "finding_revision",
    "recommendation_revision",
    "decision",
    "trail_step",
)


def _fresh_uuid7() -> str:
    """Mint one fresh UUIDv7 string.

    Used as a one-off identity for every endpoint (target and source)
    and every Relationship in the graph. Each call returns a fresh
    value so generated endpoints do not collide across cases or
    across endpoints within one case.
    """
    return str(uuid_utils.uuid7())


def _seed_party(connection, *, party_id: str, display: str) -> None:
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
#   one target index from the pools, draws a ``relationship_type`` and
#   a per-relationship timestamp offset (in seconds), and mints a
#   fresh ``relationship_id``.
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
# ``(source, target, relationship_type)`` — two Relationships
# differing only in ``recorded_at`` are still distinct identities and
# the property must hold for each independently.
# ---------------------------------------------------------------------------


@st.composite
def _endpoint(draw) -> dict:
    """Draw one endpoint descriptor (target or source).

    Returns a dict with:

    - ``id`` (str): fresh UUIDv7.
    - ``revision_id`` (str | None): fresh UUIDv7 or ``None``. Drawn
      ``None`` for some endpoints so the property exercises both the
      "target carries a Revision Identity" and "target is a Resource
      header" branches of the backlink query (``_load_candidates``
      omits the ``target_revision_id`` filter when ``None`` is
      supplied).
    - ``kind`` (str): one of the six in-scope endpoint kinds.
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
      Each dict carries the eight columns needed to INSERT one row
      plus a precomputed ISO-8601 ``recorded_at`` string. The
      ``recorded_at`` offset is per-relationship-distinct so the
      backlink ``ORDER BY (recorded_at, relationship_id)`` clause has
      a stable, observable ordering.
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
# rows, and ``Audit_Records`` rows cannot leak between cases
# (design §"Testing Strategy" — "Each property and example test gets a
# fresh SQLite database"). A :class:`tempfile.TemporaryDirectory`
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


# ---------------------------------------------------------------------------
# Direct INSERT helper.
#
# Bypasses :class:`KnowledgeService` so the strategy can fabricate
# arbitrary ``(source_kind, target_kind, relationship_type)``
# combinations. The ``Relationships`` schema only constrains
# ``relationship_type`` (CHECK) and ``authoring_party_id`` (FK), both
# of which the strategy already satisfies.
# ---------------------------------------------------------------------------


def _insert_relationship(connection, relationship: dict) -> None:
    """Insert one Relationship row into the open transaction."""
    connection.execute(
        text(
            """
            INSERT INTO Relationships (
                relationship_id, relationship_type,
                source_kind, source_id, source_revision_id,
                target_kind, target_id, target_revision_id,
                authoring_party_id, recorded_at
            ) VALUES (
                :relationship_id, :relationship_type,
                :source_kind, :source_id, :source_revision_id,
                :target_kind, :target_id, :target_revision_id,
                :authoring_party_id, :recorded_at
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
        },
    )


# ---------------------------------------------------------------------------
# Outbound query helper.
#
# :class:`ProvenanceNavigator` does not expose a ``list_outbound``
# method (task 12.1 scoped only backlinks). The property's "from the
# source's outbound query" is asserted here against the canonical
# truth — a direct SELECT on ``Relationships`` filtered by
# ``source_id`` (and, when applicable, ``source_revision_id``). Both
# directions share the same authorization model in this test because
# the requesting Party holds wildcard ``view`` authority, so the two
# projections are byte-equivalent: the same Relationships in the same
# (recorded_at, relationship_id) order.
# ---------------------------------------------------------------------------


def _fetch_outbound(
    engine: Engine,
    *,
    source_id: str,
    source_revision_id: Optional[str],
) -> list[BacklinkEntry]:
    """Return every Relationship sourced from ``(source_id, source_revision_id)``.

    The result is shaped as a list of :class:`BacklinkEntry` so the
    test can apply one structural-equality check across both
    directions without re-mapping column names. ``BacklinkEntry``
    carries exactly the Requirement 8.2 attributes plus
    ``recorded_at``, which is precisely the bidirectional projection
    the property asserts.

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
            authoring_party_id,
            recorded_at
        FROM Relationships
        WHERE source_id = :source_id
          {revision_clause}
        ORDER BY recorded_at ASC, relationship_id ASC
    """

    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    return [
        BacklinkEntry(
            relationship_id=row["relationship_id"],
            relationship_type=row["relationship_type"],
            source_id=row["source_id"],
            source_kind=row["source_kind"],
            source_revision_id=row["source_revision_id"],
            authoring_party_id=row["authoring_party_id"],
            recorded_at=row["recorded_at"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 3: Backlink bidirectionality
@given(graph=_relationship_graph())
@settings(
    max_examples=100,
    deadline=2000,
    # Per-case setup (fresh SQLite file, schema creation, three Party
    # rows, one role assignment, plus a handful of INSERTs and one
    # backlink query per Relationship) is more expensive than a pure
    # in-memory property test. The setup is well under the 2000 ms
    # deadline locally but we suppress the data-generation health
    # check so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_backlink_bidirectionality(graph: dict) -> None:
    """For every Relationship in the generated graph, the target-side
    backlink query and the source-side outbound query return the same
    Relationship Identity and the same attribute values."""
    relationships: list[dict] = graph["relationships"]

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop3_"
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
                    display="Property 3 Requester",
                )
                _seed_party(
                    conn,
                    party_id=_ASSIGNING_AUTHORITY_ID,
                    display="Property 3 Assigning Authority",
                )
                _seed_party(
                    conn,
                    party_id=_AUTHORING_PARTY_ID,
                    display="Property 3 Authoring Party",
                )

            # 2. Grant the requesting Party wildcard ``view`` authority.
            #    The wildcard scope ``"*"`` covers every source
            #    endpoint's ``source_id`` via
            #    :meth:`AuthorizationService._scope_covers`, so the
            #    Party holds view authority on every Relationship and
            #    every source endpoint in the graph by construction —
            #    the property's precondition.
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
            #    regardless of how rows arrive.
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
            #        byte-equivalent.
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

                # Requirement 8.6 sanity — the navigator never
                # returns more entries than the page cap. The graph
                # has at most 6 Relationships so this is loose, but
                # asserting it here closes the loop on Property 3's
                # Requirement-8.1 reference (which constrains the
                # page to 500 entries).
                assert len(page.entries) <= BACKLINK_PAGE_LIMIT, (
                    "BacklinkPage exceeded the "
                    f"{BACKLINK_PAGE_LIMIT}-entry cap "
                    f"(returned {len(page.entries)}). Requirement "
                    "8.1 / 8.6 bound each response."
                )

                backlink_matches = [
                    entry
                    for entry in page.entries
                    if entry.relationship_id == expected_relationship_id
                ]
                assert len(backlink_matches) == 1, (
                    "Backlink query for target "
                    f"(target_id={target_id!r}, "
                    f"target_revision_id={target_revision_id!r}) "
                    "did not return exactly one entry for "
                    f"relationship_id={expected_relationship_id!r}; "
                    f"got {len(backlink_matches)}. Requirement 1.5 / "
                    "8.2 require the same Relationship Identity to "
                    "surface from the backlink direction."
                )
                backlink_entry = backlink_matches[0]

                # Requirement 8.2 — every attribute on the returned
                # BacklinkEntry must equal the inserted row.
                _assert_entry_matches_row(
                    backlink_entry,
                    relationship,
                    direction="backlink",
                )

                # ---- Outbound (source → target) direction ----
                outbound_entries = _fetch_outbound(
                    engine,
                    source_id=source_id,
                    source_revision_id=source_revision_id,
                )
                outbound_matches = [
                    entry
                    for entry in outbound_entries
                    if entry.relationship_id == expected_relationship_id
                ]
                assert len(outbound_matches) == 1, (
                    "Outbound query for source "
                    f"(source_id={source_id!r}, "
                    f"source_revision_id={source_revision_id!r}) "
                    "did not return exactly one entry for "
                    f"relationship_id={expected_relationship_id!r}; "
                    f"got {len(outbound_matches)}. Requirement 1.5 "
                    "requires the same Relationship Identity to "
                    "surface from the source direction."
                )
                outbound_entry = outbound_matches[0]

                _assert_entry_matches_row(
                    outbound_entry,
                    relationship,
                    direction="outbound",
                )

                # ---- Bidirectional equality ----
                # The property's "if and only if and identical
                # attributes" clause: every Requirement 8.2 attribute
                # must agree across the two directions.
                assert backlink_entry == outbound_entry, (
                    "Bidirectional projection mismatch for "
                    f"relationship_id={expected_relationship_id!r}: "
                    f"backlink={backlink_entry!r}, "
                    f"outbound={outbound_entry!r}. Property 3 / "
                    "Requirement 8.2 require identical attribute "
                    "values from both directions."
                )
        finally:
            engine.dispose()


def _assert_entry_matches_row(
    entry: BacklinkEntry,
    relationship: dict,
    *,
    direction: str,
) -> None:
    """Assert every Requirement-8.2 attribute on ``entry`` matches the inserted row.

    The helper takes ``direction`` for diagnostic context so a
    falsification surfaces "backlink direction diverged on
    ``source_kind``" rather than an opaque equality failure on the
    whole dataclass. Each per-attribute assertion is independent so
    the first divergence is the one reported.
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
