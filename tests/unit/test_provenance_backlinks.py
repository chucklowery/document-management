"""Unit tests for :mod:`walking_slice.provenance` — backlink discovery (task 12.1).

These tests pin the contract established in task 12.1, design §"Provenance_
Navigator" (the backlink algorithm), AD-WS-8 (interim backlink indexing),
and Requirements 8.1, 8.2, 8.4, and 8.6:

- The candidate scan loads inbound Relationships keyed on
  ``(target_id, target_revision_id)`` ordered by ``(recorded_at,
  relationship_id)`` ASC with a 500-row hard cap (Requirements 8.1, 8.6).
- The authorized projection is built by evaluating
  ``view.relationship`` and ``view.<source_kind>`` for the requesting
  Party; candidates failing either check are silently dropped.
- The cursor, the response size, and the latency baseline are computed
  from the authorized projection alone — never from the candidate set.
  This is the Property 4 (Non-leakage of restricted information) shaping
  invariant in unit-test form.
- Every returned :class:`BacklinkEntry` carries the six attributes named
  by Requirement 8.2 (Relationship Identity, Relationship Type, source
  endpoint Identity, source endpoint Type, source endpoint Revision
  Identity, authoring Party Identity).
- The method does not write any new ``Role_Assignments`` row, satisfying
  Requirement 8.4 (returning a backlink does not grant authority).

The tests work entirely at the service level (no HTTP yet — that is task
12.5) and use the per-test ``engine``, ``audit_log``, ``identity_service``,
and ``authorization_service`` fixtures from ``tests/conftest.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.persistence import create_schema
from walking_slice.provenance import (
    BACKLINK_LATENCY_BASELINE_CEILING_SECONDS,
    BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS,
    BACKLINK_PAGE_LIMIT,
    BacklinkCursor,
    BacklinkEntry,
    BacklinkPage,
    ProvenanceNavigator,
    compute_latency_baseline_seconds,
    decode_backlink_cursor,
    encode_backlink_cursor,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seed helpers.
# ---------------------------------------------------------------------------


_REQUESTER_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_AUTHORING_PARTY_ID = "00000000-0000-7000-8000-000000000002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000000003"
_TARGET_ID = "00000000-0000-7000-8000-000000000010"
_TARGET_REVISION_ID = "00000000-0000-7000-8000-000000000011"

# Source endpoints used in the tests. Each ``source_id`` doubles as the
# scope identifier for ``view.*`` role assignments — matching the scope
# convention adopted in ``ProvenanceNavigator._build_authorized_projection``
# (the source endpoint's Resource identity is the scope).
_SOURCE_A_ID = "00000000-0000-7000-8000-0000000000a0"
_SOURCE_A_REVISION_ID = "00000000-0000-7000-8000-0000000000a1"
_SOURCE_B_ID = "00000000-0000-7000-8000-0000000000b0"
_SOURCE_B_REVISION_ID = "00000000-0000-7000-8000-0000000000b1"
_SOURCE_C_ID = "00000000-0000-7000-8000-0000000000c0"
_SOURCE_C_REVISION_ID = "00000000-0000-7000-8000-0000000000c1"

_TS_FIXED = "2026-01-01T00:00:00.000Z"
_EFFECTIVE_TIME = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _seed_party(conn, party_id: str, display: str = "Test Party") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    """Seed every Party referenced by the test relationships.

    Authoring Parties are FK-referenced from ``Relationships.authoring_
    party_id`` so they must exist in ``Parties`` before any inbound
    Relationship row is inserted; the requesting Party and the assigning
    authority must also exist so :class:`AuthorizationService.assign_role`
    can record the role assignment and its consequential audit row.
    """
    with engine.begin() as conn:
        _seed_party(conn, _REQUESTER_PARTY_ID, "Requesting Party")
        _seed_party(conn, _AUTHORING_PARTY_ID, "Authoring Party")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _insert_relationship(
    engine: Engine,
    *,
    relationship_id: str,
    source_id: str,
    source_revision_id: Optional[str],
    source_kind: str,
    target_id: str = _TARGET_ID,
    target_revision_id: Optional[str] = _TARGET_REVISION_ID,
    relationship_type: str = "Supports",
    recorded_at: str = _TS_FIXED,
    authoring_party_id: str = _AUTHORING_PARTY_ID,
) -> None:
    """Insert one Relationship row.

    Bypasses :class:`KnowledgeService` so the test can fabricate
    arbitrary ``(source_kind, recorded_at)`` combinations independent
    of the slice's natural Finding/Recommendation/Decision pipeline.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type, source_kind,
                    source_id, source_revision_id, target_kind, target_id,
                    target_revision_id, authoring_party_id, recorded_at
                ) VALUES (
                    :relationship_id, :relationship_type, :source_kind,
                    :source_id, :source_revision_id, 'document_revision',
                    :target_id, :target_revision_id, :authoring_party_id,
                    :recorded_at
                )
                """
            ),
            {
                "relationship_id": relationship_id,
                "relationship_type": relationship_type,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_revision_id": source_revision_id,
                "target_id": target_id,
                "target_revision_id": target_revision_id,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )


def _assign_view_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
    party_id: str = _REQUESTER_PARTY_ID,
) -> str:
    """Grant ``view`` authority to ``party_id`` for ``scope``.

    The scope value matches the convention used by
    :meth:`ProvenanceNavigator._build_authorized_projection`: the
    source endpoint's Resource identity (or the wildcard ``"*"``).
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="reviewer",
        scope=scope,
        authorities_granted=("view",),
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _ts(offset_seconds: int) -> str:
    """Build an ISO-8601 ms-precision timestamp ``offset_seconds`` past 2026-01-01."""
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return format_iso8601_ms(
        base.replace(
            second=offset_seconds % 60,
            minute=(offset_seconds // 60) % 60,
            hour=(offset_seconds // 3600) % 24,
        )
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_engine(engine: Engine, audit_log: AuditLog) -> Engine:
    """Engine with schema and the three test Parties seeded.

    Depending on ``audit_log`` ensures the schema is created (the
    ``audit_log`` fixture in ``conftest.py`` calls ``create_schema``)
    before any test attempts to insert a Relationship.
    """
    _seed_required_parties(engine)
    return engine


@pytest.fixture
def navigator(
    clock: Clock,
    authorization_service: AuthorizationService,
) -> ProvenanceNavigator:
    """ProvenanceNavigator wired to the per-test clock and AuthorizationService."""
    return ProvenanceNavigator(
        clock=clock,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Latency baseline pure function.
# ---------------------------------------------------------------------------


class TestLatencyBaseline:
    """``compute_latency_baseline_seconds`` is a deterministic pure function.

    Property 4 (Non-leakage of restricted information) requires the
    latency baseline to depend only on the authorized response size.
    These tests pin the function's shape so future refactors cannot
    inadvertently introduce a non-deterministic input.
    """

    def test_zero_entries_returns_floor(self) -> None:
        assert compute_latency_baseline_seconds(0) == pytest.approx(
            BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS
        )

    def test_full_page_returns_ceiling_or_below(self) -> None:
        result = compute_latency_baseline_seconds(BACKLINK_PAGE_LIMIT)
        assert result <= BACKLINK_LATENCY_BASELINE_CEILING_SECONDS
        # And bigger than the floor — a full page must take longer than
        # an empty page, otherwise an attacker could distinguish them.
        assert result > BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS

    def test_monotonic_in_response_size(self) -> None:
        # For sizes up to the ceiling-clamp threshold the function is
        # strictly non-decreasing; once the ceiling is reached it stays
        # flat. The combination is what Property 4 needs.
        previous = compute_latency_baseline_seconds(0)
        for size in (1, 10, 100, 250, 500, 1000):
            current = compute_latency_baseline_seconds(size)
            assert current >= previous, (
                f"baseline decreased between sizes {size - 1} and {size}: "
                f"{previous} -> {current}"
            )
            previous = current

    def test_deterministic_for_same_size(self) -> None:
        # Two calls with the same size must return byte-equivalent
        # floats. This is the foundation for the indistinguishability
        # property — the function must not consult any external state.
        for size in (0, 1, 50, 500):
            assert compute_latency_baseline_seconds(
                size
            ) == compute_latency_baseline_seconds(size)

    def test_rejects_negative_size(self) -> None:
        with pytest.raises(ValueError):
            compute_latency_baseline_seconds(-1)


# ---------------------------------------------------------------------------
# Cursor encode/decode round-trip.
# ---------------------------------------------------------------------------


class TestCursorRoundTrip:
    """Cursor encoding is a stable, reversible string transformation."""

    def test_encode_none_returns_none(self) -> None:
        assert encode_backlink_cursor(None) is None

    def test_decode_none_returns_none(self) -> None:
        assert decode_backlink_cursor(None) is None
        assert decode_backlink_cursor("") is None

    def test_round_trip_preserves_values(self) -> None:
        cursor = BacklinkCursor(
            recorded_at="2026-01-01T00:00:00.000Z",
            relationship_id="00000000-0000-7000-8000-000000000aaa",
        )
        encoded = encode_backlink_cursor(cursor)
        assert encoded == (
            "2026-01-01T00:00:00.000Z|"
            "00000000-0000-7000-8000-000000000aaa"
        )
        assert decode_backlink_cursor(encoded) == cursor

    def test_decode_malformed_cursor_raises(self) -> None:
        with pytest.raises(ValueError):
            decode_backlink_cursor("no-delimiter-here")
        with pytest.raises(ValueError):
            decode_backlink_cursor("|missing-recorded-at")
        with pytest.raises(ValueError):
            decode_backlink_cursor("missing-relationship-id|")


# ---------------------------------------------------------------------------
# list_backlinks — empty / no candidates.
# ---------------------------------------------------------------------------


def test_list_backlinks_returns_empty_page_when_no_inbound_relationships(
    navigator: ProvenanceNavigator,
    seeded_engine: Engine,
) -> None:
    """No inbound Relationships → empty page with floor latency baseline."""
    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.entries == ()
    assert page.cursor is None
    assert page.response_size == 0
    assert page.latency_baseline_seconds == pytest.approx(
        BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS
    )


# ---------------------------------------------------------------------------
# list_backlinks — authorized projection (Requirement 8.1, 8.2).
# ---------------------------------------------------------------------------


def test_list_backlinks_returns_authorized_relationships_with_all_attributes(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """A Party with view authority on every source endpoint sees every backlink.

    The returned :class:`BacklinkEntry` carries the six attributes
    Requirement 8.2 mandates: Relationship Identity, Relationship Type,
    source endpoint Identity, source endpoint Type, source endpoint
    Revision Identity, and authoring Party Identity.
    """
    # Three inbound Relationships from three distinct source endpoints,
    # recorded at three distinct timestamps so the ordering assertion
    # below is deterministic.
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000bb1",
        source_id=_SOURCE_B_ID,
        source_revision_id=_SOURCE_B_REVISION_ID,
        source_kind="recommendation_revision",
        recorded_at=_ts(20),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000cc1",
        source_id=_SOURCE_C_ID,
        source_revision_id=None,
        source_kind="decision",
        recorded_at=_ts(30),
        relationship_type="Addresses",
    )

    # Wildcard view authority — sees every source endpoint and every
    # Relationship.
    _assign_view_role(authorization_service, seeded_engine, scope="*")

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.response_size == 3
    assert len(page.entries) == 3
    # Ordering is (recorded_at ASC, relationship_id ASC).
    assert [entry.relationship_id for entry in page.entries] == [
        "00000000-0000-7000-8000-000000000aa1",
        "00000000-0000-7000-8000-000000000bb1",
        "00000000-0000-7000-8000-000000000cc1",
    ]

    # Requirement 8.2: every entry carries the six mandated attributes.
    first = page.entries[0]
    assert first.relationship_id == "00000000-0000-7000-8000-000000000aa1"
    assert first.relationship_type == "Supports"
    assert first.source_id == _SOURCE_A_ID
    assert first.source_kind == "finding_revision"
    assert first.source_revision_id == _SOURCE_A_REVISION_ID
    assert first.authoring_party_id == _AUTHORING_PARTY_ID

    # The Decision-source backlink shows that a NULL source_revision_id
    # is preserved by the projection (Decisions are Immutable Records
    # with no Revision concept).
    decision_entry = page.entries[2]
    assert decision_entry.relationship_type == "Addresses"
    assert decision_entry.source_kind == "decision"
    assert decision_entry.source_revision_id is None

    # Cursor points at the last visible Relationship.
    assert page.cursor == BacklinkCursor(
        recorded_at=_ts(30),
        relationship_id="00000000-0000-7000-8000-000000000cc1",
    )


# ---------------------------------------------------------------------------
# list_backlinks — restricted-vs-nonexistent indistinguishability.
# ---------------------------------------------------------------------------


def test_restricted_source_is_dropped_from_visible_projection(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """A Party without view authority on a source sees that backlink dropped.

    This is the core of Property 4: the response shape is built from
    the authorized projection alone. The dropped Relationship does not
    appear in ``entries``, does not influence ``cursor``, does not
    influence ``response_size``, and does not influence
    ``latency_baseline_seconds``.
    """
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000bb1",
        source_id=_SOURCE_B_ID,
        source_revision_id=_SOURCE_B_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(20),
    )

    # The Party can see only source A.
    _assign_view_role(authorization_service, seeded_engine, scope=_SOURCE_A_ID)

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.response_size == 1
    assert [entry.source_id for entry in page.entries] == [_SOURCE_A_ID]
    # Cursor names source A's Relationship — not source B's — because
    # source B was never in the authorized projection.
    assert page.cursor is not None
    assert page.cursor.relationship_id == "00000000-0000-7000-8000-000000000aa1"
    assert page.latency_baseline_seconds == compute_latency_baseline_seconds(1)


def test_response_shape_matches_universe_without_restricted_relationship(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Property 4 in unit-test form.

    Two universes:

    - Universe X: Relationships A (visible) and B (restricted to the
      requesting Party) both exist.
    - Universe Y: Relationship A exists; Relationship B never existed.

    The :class:`BacklinkPage` returned to the requesting Party must be
    indistinguishable across the two universes along count, identifier
    set, ordering, cursor, response size, and latency baseline.
    """
    # ---- Universe X: A visible to requester, B restricted. -------------
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000bb1",
        source_id=_SOURCE_B_ID,
        source_revision_id=_SOURCE_B_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(20),
    )
    # Grant view authority only on source A.
    _assign_view_role(authorization_service, seeded_engine, scope=_SOURCE_A_ID)

    with seeded_engine.connect() as conn:
        page_x = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    # ---- Universe Y: only A exists. -----------------------------------
    # Build a second, isolated database to host universe Y; the
    # requester has identical view authority (scope = source A's id).
    # Using ``seeded_engine``'s file would require deleting B, but the
    # AD-WS-4 trigger forbids DELETE on Relationships. A fresh engine
    # is the cleanest representation of "B never existed".
    universe_y_url = "sqlite:///:memory:"
    from sqlalchemy import create_engine
    engine_y = create_engine(universe_y_url, future=True)
    create_schema(engine_y)
    audit_y = AuditLog(navigator.clock)
    authz_y = authorization_service.__class__(
        clock=navigator.clock,
        audit_log=audit_y,
        identity_service=navigator.authorization_service.identity_service,
    )
    navigator_y = ProvenanceNavigator(
        clock=navigator.clock,
        authorization_service=authz_y,
    )
    _seed_required_parties(engine_y)
    _insert_relationship(
        engine_y,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    _assign_view_role(authz_y, engine_y, scope=_SOURCE_A_ID)

    with engine_y.connect() as conn:
        page_y = navigator_y.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    # ---- Indistinguishability assertions. ----------------------------
    # Count dimension.
    assert page_x.response_size == page_y.response_size == 1
    # Identifier set dimension.
    assert [e.relationship_id for e in page_x.entries] == [
        e.relationship_id for e in page_y.entries
    ]
    # Ordering dimension — implied by the equal list above, but
    # asserted explicitly so the test reads as the property states.
    assert page_x.entries == page_y.entries
    # Cursor dimension.
    assert page_x.cursor == page_y.cursor
    # Latency baseline dimension — exact equality because the baseline
    # is a pure function of response_size.
    assert page_x.latency_baseline_seconds == page_y.latency_baseline_seconds


def test_no_view_authority_returns_empty_page_indistinguishable_from_nonexistent(
    navigator: ProvenanceNavigator,
    seeded_engine: Engine,
) -> None:
    """A Party with no role assignment sees the same page as a non-existent target.

    Requirement 8.5 specifies that an unauthenticated/unauthorized
    request returns a response indistinguishable from one for a
    non-existent endpoint. The page returned here matches the page
    returned by
    :func:`test_list_backlinks_returns_empty_page_when_no_inbound_relationships`.
    """
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    # No ``_assign_view_role`` call — the requester has no role.

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.entries == ()
    assert page.cursor is None
    assert page.response_size == 0
    assert page.latency_baseline_seconds == pytest.approx(
        BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS
    )


# ---------------------------------------------------------------------------
# list_backlinks — 500-row limit and pagination (Requirement 8.6).
# ---------------------------------------------------------------------------


def test_list_backlinks_enforces_500_row_page_limit(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Insert more than 500 inbound Relationships; first page returns 500.

    Per Requirement 8.6 the response is bounded to ≤ 500 Relationships
    per page. With wildcard view authority every Relationship is
    visible; the candidate ``LIMIT 500`` and the authorized projection
    therefore both equal 500.
    """
    total = BACKLINK_PAGE_LIMIT + 50  # 550 inbound Relationships
    for index in range(total):
        # Distinct relationship_id values and distinct recorded_at
        # values so the ORDER BY is deterministic.
        _insert_relationship(
            seeded_engine,
            relationship_id=f"00000000-0000-7000-8000-{index:012d}",
            source_id=_SOURCE_A_ID,
            source_revision_id=_SOURCE_A_REVISION_ID,
            source_kind="finding_revision",
            recorded_at=f"2026-01-01T00:00:00.{index % 1000:03d}Z"
            if index < 1000
            else f"2026-01-01T00:00:{index // 1000:02d}.{index % 1000:03d}Z",
        )

    _assign_view_role(authorization_service, seeded_engine, scope="*")

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.response_size == BACKLINK_PAGE_LIMIT
    assert len(page.entries) == BACKLINK_PAGE_LIMIT
    # Cursor names the 500th Relationship so the next page begins
    # strictly after it.
    assert page.cursor is not None
    assert page.cursor.relationship_id == page.entries[-1].relationship_id
    assert page.cursor.recorded_at == page.entries[-1].recorded_at


def test_list_backlinks_pagination_picks_up_after_cursor(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """The ``after_cursor`` argument loads the next page strictly past the cursor."""
    # Insert three Relationships at distinct timestamps so the cursor
    # boundary is unambiguous.
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000bb1",
        source_id=_SOURCE_B_ID,
        source_revision_id=_SOURCE_B_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(20),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000cc1",
        source_id=_SOURCE_C_ID,
        source_revision_id=_SOURCE_C_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(30),
    )
    _assign_view_role(authorization_service, seeded_engine, scope="*")

    # Resume from the first Relationship's position; the second and
    # third should be returned.
    cursor = BacklinkCursor(
        recorded_at=_ts(10),
        relationship_id="00000000-0000-7000-8000-000000000aa1",
    )
    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
            after_cursor=cursor,
        )

    assert [entry.relationship_id for entry in page.entries] == [
        "00000000-0000-7000-8000-000000000bb1",
        "00000000-0000-7000-8000-000000000cc1",
    ]


def test_list_backlinks_filters_by_target_revision_id(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """``target_revision_id`` narrows the candidate set to one Revision."""
    other_revision_id = "00000000-0000-7000-8000-000000000099"

    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        target_revision_id=_TARGET_REVISION_ID,
        recorded_at=_ts(10),
    )
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000bb1",
        source_id=_SOURCE_B_ID,
        source_revision_id=_SOURCE_B_REVISION_ID,
        source_kind="finding_revision",
        target_revision_id=other_revision_id,
        recorded_at=_ts(20),
    )
    _assign_view_role(authorization_service, seeded_engine, scope="*")

    with seeded_engine.connect() as conn:
        page_for_target_revision = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )
        page_for_other_revision = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=other_revision_id,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert [e.relationship_id for e in page_for_target_revision.entries] == [
        "00000000-0000-7000-8000-000000000aa1",
    ]
    assert [e.relationship_id for e in page_for_other_revision.entries] == [
        "00000000-0000-7000-8000-000000000bb1",
    ]


# ---------------------------------------------------------------------------
# list_backlinks — Requirement 8.4 (no authority transfer).
# ---------------------------------------------------------------------------


def test_list_backlinks_does_not_insert_role_assignments(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Requirement 8.4: returning a backlink does not grant any new authority.

    The method must not insert any ``Role_Assignments`` row as a side
    effect. We compare row counts before and after the call.
    """
    _insert_relationship(
        seeded_engine,
        relationship_id="00000000-0000-7000-8000-000000000aa1",
        source_id=_SOURCE_A_ID,
        source_revision_id=_SOURCE_A_REVISION_ID,
        source_kind="finding_revision",
        recorded_at=_ts(10),
    )
    _assign_view_role(authorization_service, seeded_engine, scope="*")

    with seeded_engine.connect() as conn:
        before = conn.execute(
            text("SELECT COUNT(*) FROM Role_Assignments")
        ).scalar_one()

    with seeded_engine.connect() as conn:
        navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    with seeded_engine.connect() as conn:
        after = conn.execute(
            text("SELECT COUNT(*) FROM Role_Assignments")
        ).scalar_one()

    assert before == after

# ---------------------------------------------------------------------------
# list_backlinks — Slice 2 planning source-kind coverage (task 12.2).
#
# Task 12.2 of the second walking slice extends the recognized backlink
# source kinds to cover the eight planning Resource kinds introduced by
# Slice 2 (objective_revision, intended_outcome_revision, project_revision,
# deliverable_expectation_revision, activity_plan, plan_revision,
# plan_review_revision, plan_approval) without modifying the backlink
# algorithm itself. The algorithm already handles any source_kind because
# the SQL candidate scan does not filter on source_kind and the
# ``view.<source_kind>`` action falls back to the ``view`` authority via
# :func:`walking_slice.authorization._required_authority`'s prefix-based
# fallback (AD-WS-15).
#
# These tests assert two things:
#
#   1. The :data:`_AUTHORIZED_SOURCE_KINDS` constant lists every Slice 1
#      and Slice 2 source endpoint kind the algorithm has known coverage
#      for, so a future slice adding a kind that scatters across the
#      codebase will fail this assertion until the constant is updated.
#   2. The existing algorithm returns inbound Relationships sourced from
#      each Slice 2 planning kind, with the same attribute surface (per
#      Requirement 15.2 / Slice 1 Requirement 8.2) and the same
#      authorization gating (Requirement 15.1, 15.4, 15.6) as Slice 1
#      sources.
#
# _Validates: Requirements 15.1, 15.2, 15.4, 15.6._
# ---------------------------------------------------------------------------


# The seven planning kinds named by task 12.2 plus ``activity_plan``,
# which is also a Slice 2 source endpoint (Activity Plans are recorded as
# the source endpoint of an ``Addresses`` Relationship targeting the
# parent Project per ``walking_slice.planning.activity_plans``). Listed in
# the order the task brief names them so a diff against the spec is
# trivial.
_SLICE2_PLANNING_SOURCE_KINDS: tuple[str, ...] = (
    "objective_revision",
    "intended_outcome_revision",
    "project_revision",
    "deliverable_expectation_revision",
    "activity_plan",
    "plan_revision",
    "plan_review_revision",
    "plan_approval",
)


def test_authorized_source_kinds_includes_every_slice2_planning_kind() -> None:
    """:data:`_AUTHORIZED_SOURCE_KINDS` covers every Slice 2 planning kind.

    The constant is the documented coverage surface of the backlink
    algorithm; this test pins that the seven planning kinds named by
    task 12.2 plus ``activity_plan`` (the eighth Slice 2 source endpoint
    kind, contributed by :mod:`walking_slice.planning.activity_plans`)
    are present.
    """
    from walking_slice.provenance import _AUTHORIZED_SOURCE_KINDS

    for kind in _SLICE2_PLANNING_SOURCE_KINDS:
        assert kind in _AUTHORIZED_SOURCE_KINDS, (
            f"Slice 2 planning source kind {kind!r} is missing from "
            f"_AUTHORIZED_SOURCE_KINDS; the backlink coverage surface must "
            f"name every recognized source endpoint kind."
        )


def test_authorized_source_kinds_preserves_slice1_kinds() -> None:
    """The Slice 1 source kinds remain present (Requirement 19.1 — additive only).

    Task 12.2 is strictly additive; this test asserts that adding the
    planning kinds did not accidentally drop a Slice 1 kind from the
    coverage surface.
    """
    from walking_slice.provenance import _AUTHORIZED_SOURCE_KINDS

    slice1_kinds = {
        "decision",
        "recommendation_revision",
        "finding_revision",
        "trail_step",
    }
    missing = slice1_kinds - set(_AUTHORIZED_SOURCE_KINDS)
    assert missing == set(), (
        f"Slice 1 source kinds dropped from _AUTHORIZED_SOURCE_KINDS: "
        f"{sorted(missing)!r}. Task 12.2 must be additive only."
    )


@pytest.mark.parametrize("source_kind", _SLICE2_PLANNING_SOURCE_KINDS)
def test_list_backlinks_returns_relationships_for_each_planning_source_kind(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    source_kind: str,
) -> None:
    """Each Slice 2 planning ``source_kind`` is returned through the existing algorithm.

    Inserts one inbound Relationship per planning kind and asserts the
    authorized projection contains the Relationship with all six
    attributes mandated by Requirement 15.2 (Relationship Identity,
    Relationship Type, source endpoint Identity, source endpoint Type,
    source endpoint Revision Identity when applicable, authoring Party
    Identity).

    The wildcard ``view`` role assignment exercises the prefix-based
    fallback in :func:`walking_slice.authorization._required_authority`:
    ``view.<planning_kind>`` resolves to the ``view`` authority for every
    Slice 2 source kind without any new mapping row, satisfying
    Requirement 19.1 (additive-only extension of Slice 1 contexts).

    Validates: Requirements 15.1, 15.2.
    """
    # ``plan_approval`` and ``activity_plan`` are revisionless source
    # kinds (Immutable Records and revisionless Resources respectively),
    # mirroring the Slice 1 ``decision`` source kind. The other six
    # planning kinds are Revision-bearing; their inbound Relationships
    # carry a non-NULL ``source_revision_id``. The fixture choice here
    # mirrors how the planning services persist their ``Addresses``
    # rows in production.
    revisionless_kinds = {"plan_approval", "activity_plan"}
    relationship_id = f"00000000-0000-7000-8000-{abs(hash(source_kind)) % 10**11:011d}"
    source_id = f"00000000-0000-7000-8000-{abs(hash(source_kind + 's')) % 10**11:011d}"
    source_revision_id: Optional[str] = (
        None
        if source_kind in revisionless_kinds
        else f"00000000-0000-7000-8000-{abs(hash(source_kind + 'r')) % 10**11:011d}"
    )

    _insert_relationship(
        seeded_engine,
        relationship_id=relationship_id,
        source_id=source_id,
        source_revision_id=source_revision_id,
        source_kind=source_kind,
        # ``Addresses`` is the relationship type the eight planning
        # services use when binding their source endpoint to a parent
        # target (the one exception is ``plan_review_revision`` which
        # uses ``Relates To``; either is in the schema's CHECK list so
        # both are valid here — picking ``Addresses`` keeps the
        # parametrized cases uniform).
        relationship_type=(
            "Relates To" if source_kind == "plan_review_revision" else "Addresses"
        ),
        recorded_at=_ts(10),
    )

    # Wildcard view authority — the requester holds ``view`` on every
    # scope, so the authorization check for both ``view.relationship``
    # and ``view.<source_kind>`` is permitted via the prefix fallback.
    _assign_view_role(authorization_service, seeded_engine, scope="*")

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.response_size == 1, (
        f"expected one authorized backlink for source_kind={source_kind!r}, "
        f"got {page.response_size}; the existing algorithm should already "
        f"handle every planning source kind via the prefix fallback."
    )
    entry = page.entries[0]
    # Requirement 15.2: every entry carries the six mandated attributes.
    assert entry.relationship_id == relationship_id
    assert entry.source_kind == source_kind
    assert entry.source_id == source_id
    assert entry.source_revision_id == source_revision_id
    assert entry.authoring_party_id == _AUTHORING_PARTY_ID
    # Relationship type is one of the schema's enumerated values.
    assert entry.relationship_type in {"Addresses", "Relates To"}


@pytest.mark.parametrize("source_kind", _SLICE2_PLANNING_SOURCE_KINDS)
def test_list_backlinks_drops_planning_source_when_party_lacks_view_authority(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
    source_kind: str,
) -> None:
    """A Party without view authority on a planning source endpoint sees it dropped.

    Requirement 15.4 forbids the Provenance_Navigator from granting any
    authority by returning a backlink, and Requirement 15.6 (via the
    referenced Slice 1 Requirement 8.3 and Requirement 8.5) requires the
    response to remain indistinguishable from a universe in which the
    restricted Relationship does not exist. Together they imply that a
    Party with view authority on one source endpoint but not another
    should see only the authorized Relationship.

    This parametrized test inserts two inbound Relationships per case:
    one whose source is a planning kind under test, and one whose source
    is a Slice 1 ``finding_revision``. The requester is granted view
    authority on the ``finding_revision`` source only; the planning
    Relationship must be dropped, the Slice 1 Relationship must be
    returned, and the response size, cursor, and latency baseline must
    reflect only the authorized projection.

    Validates: Requirements 15.4, 15.6.
    """
    revisionless_kinds = {"plan_approval", "activity_plan"}
    planning_rel_id = "00000000-0000-7000-8000-0000000aa001"
    planning_source_id = "00000000-0000-7000-8000-0000000aa002"
    planning_source_revision_id: Optional[str] = (
        None
        if source_kind in revisionless_kinds
        else "00000000-0000-7000-8000-0000000aa003"
    )

    # Insert the planning Relationship — the requester will lack view
    # authority on this source endpoint.
    _insert_relationship(
        seeded_engine,
        relationship_id=planning_rel_id,
        source_id=planning_source_id,
        source_revision_id=planning_source_revision_id,
        source_kind=source_kind,
        relationship_type=(
            "Relates To" if source_kind == "plan_review_revision" else "Addresses"
        ),
        recorded_at=_ts(10),
    )

    # Insert a Slice 1 finding_revision Relationship — the requester
    # will hold view authority on this source endpoint.
    finding_rel_id = "00000000-0000-7000-8000-0000000bb001"
    finding_source_id = "00000000-0000-7000-8000-0000000bb002"
    finding_source_revision_id = "00000000-0000-7000-8000-0000000bb003"
    _insert_relationship(
        seeded_engine,
        relationship_id=finding_rel_id,
        source_id=finding_source_id,
        source_revision_id=finding_source_revision_id,
        source_kind="finding_revision",
        relationship_type="Supports",
        recorded_at=_ts(20),
    )

    # Grant view authority scoped only to the Slice 1 finding source.
    _assign_view_role(
        authorization_service, seeded_engine, scope=finding_source_id
    )

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    # Only the authorized Slice 1 Relationship is visible. The Slice 2
    # planning Relationship is silently dropped, and the response shape
    # is computed from the authorized projection only.
    assert page.response_size == 1
    assert [entry.relationship_id for entry in page.entries] == [finding_rel_id]
    assert page.cursor is not None
    assert page.cursor.relationship_id == finding_rel_id
    assert page.latency_baseline_seconds == compute_latency_baseline_seconds(1)


def test_list_backlinks_mixed_slice1_and_slice2_sources_returns_all_authorized(
    navigator: ProvenanceNavigator,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """A target with mixed Slice 1 + Slice 2 inbound Relationships returns every authorized backlink.

    Exercises the additive coverage end-to-end: every planning source
    kind plus every Slice 1 source kind is inserted as an inbound
    Relationship and the wildcard-view Party sees them all in
    deterministic order. Validates Requirement 15.1's deterministic
    ordering plus Requirement 15.2's full attribute surface across the
    mixed source-kind set.

    Validates: Requirements 15.1, 15.2.
    """
    revisionless_kinds = {"plan_approval", "activity_plan", "decision"}
    # Slice 1 + Slice 2 source kinds, ordered so the timestamp-based
    # ordering assertion below is deterministic.
    source_kinds_in_order = (
        "decision",
        "recommendation_revision",
        "finding_revision",
        "trail_step",
    ) + _SLICE2_PLANNING_SOURCE_KINDS

    inserted: list[tuple[str, str]] = []
    for index, source_kind in enumerate(source_kinds_in_order):
        relationship_id = f"00000000-0000-7000-8000-{index:012d}"
        source_id = f"00000000-0000-7000-8000-1{index:011d}"
        source_revision_id = (
            None
            if source_kind in revisionless_kinds
            else f"00000000-0000-7000-8000-2{index:011d}"
        )
        _insert_relationship(
            seeded_engine,
            relationship_id=relationship_id,
            source_id=source_id,
            source_revision_id=source_revision_id,
            source_kind=source_kind,
            relationship_type=(
                "Relates To"
                if source_kind == "plan_review_revision"
                else "Addresses"
            ),
            # Distinct timestamps so the (recorded_at ASC,
            # relationship_id ASC) ordering is unambiguous.
            recorded_at=_ts(10 + index),
        )
        inserted.append((relationship_id, source_kind))

    _assign_view_role(authorization_service, seeded_engine, scope="*")

    with seeded_engine.connect() as conn:
        page = navigator.list_backlinks(
            conn,
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REVISION_ID,
            party_id=_REQUESTER_PARTY_ID,
            at=_EFFECTIVE_TIME,
        )

    assert page.response_size == len(inserted)
    # Deterministic ordering: timestamp-ascending. Because each insert
    # used ``_ts(10 + index)``, the returned order matches the insertion
    # order.
    assert [entry.relationship_id for entry in page.entries] == [
        rid for rid, _ in inserted
    ]
    # Every Slice 2 planning kind appears in the projection.
    returned_kinds = {entry.source_kind for entry in page.entries}
    for kind in _SLICE2_PLANNING_SOURCE_KINDS:
        assert kind in returned_kinds, (
            f"planning source_kind {kind!r} missing from authorized "
            f"projection across mixed Slice 1 + Slice 2 sources."
        )
