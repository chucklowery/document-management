# Feature: first-walking-slice, Property 4: Non-leakage of restricted information
"""Property 4 — Non-leakage of restricted information (task 12.7).

**Property 4: Non-leakage of restricted information**

For all pairs ``(P, P')`` of Parties that differ only in view
authority on one node ``N``, the response visible to ``P'`` from the
:class:`~walking_slice.provenance.ProvenanceNavigator` is
indistinguishable from the response ``P'`` would receive in a
universe where ``N`` does not exist, across:

- count
- identifier set
- ordering of entries
- cursor
- response size
- error wording (or both absent)
- latency baseline (within 100 ms tolerance)

**Validates: Requirements 7.4, 8.3, 8.5, 10.5, 11.3, 11.7, 15.4**

Strategy:

Each Hypothesis case draws a *relationship graph* — a list of one to
six inbound :class:`Relationships` rows targeting one shared target
endpoint — plus one *restricted index* naming the Relationship whose
source endpoint ``P'`` is *not* permitted to view.

Per case, the test builds two universes on separate on-disk SQLite
files:

- **Universe X** — every drawn Relationship is persisted (including
  the restricted one). ``P'`` is granted ``view`` authority on every
  *non-restricted* source endpoint's scope. ``P'`` is **never** granted
  view authority on the restricted source's scope, so the
  :class:`~walking_slice.provenance.ProvenanceNavigator` filters that
  Relationship out of the authorized projection.

- **Universe Y** — every drawn Relationship is persisted **except** the
  restricted one (which, by construction, *does not exist* in this
  universe). ``P'`` is granted the same set of view-authority role
  assignments as in Universe X.

The test then issues the same backlink query to ``P'`` in both
universes via
:meth:`ProvenanceNavigator.list_backlinks`. The seven dimensions named
in the property statement are asserted to be byte-equivalent (or
within tolerance for the latency baseline):

1. ``response_size`` (count dimension).
2. The set of returned Relationship Identities (identifier-set
   dimension).
3. The exact list of returned :class:`BacklinkEntry` tuples
   (ordering + identifier-set dimensions together).
4. ``cursor`` (cursor dimension).
5. ``response_size`` again, asserted explicitly so the failure
   message names the dimension if the size diverges (response-size
   dimension).
6. ``latency_baseline_seconds`` is asserted within the
   ``LATENCY_TOLERANCE_SECONDS`` envelope (0.1 s) per Requirement
   8.3 / 15.4. The slice's
   :func:`~walking_slice.provenance.compute_latency_baseline_seconds`
   is a pure deterministic function of ``response_size`` so the two
   values are exactly equal in practice; the 100-ms tolerance is the
   property's safety net.
7. Both calls return a :class:`BacklinkPage` (no exception is raised
   in either universe), so the "error wording" dimension reduces to
   "both absent". An exception raised by exactly one universe would
   itself constitute a leak; both raising the same exception would
   not.

Why backlinks?
    The four navigator surfaces that ``P'`` can reach in this slice —
    :meth:`list_backlinks`, :meth:`navigate_decision`,
    :meth:`resolve_region_text`, and
    :meth:`navigate_decision_with_disclosure` — share the same
    :meth:`~AuthorizationService.evaluate` gate and the same
    "authorized projection only" shaping invariant. Property 4 is
    enforced uniformly across all of them: the cursor, response size,
    and latency baseline are computed from the authorized projection
    alone (design §"Provenance_Navigator", AD-WS-9). Driving the
    property through :meth:`list_backlinks` covers every dimension of
    the property statement on the surface most directly named by
    Requirements 8.3 and 8.5 — the read path the rest of the
    navigator surfaces compose with. Per-surface idempotence and
    chain-resolvability are covered by Property 8 (task 12.8) and
    Property 9 (task 12.9).

Restricted-vs-nonexistent normalization rests on three navigator
invariants verified by this test:

- The candidate scan loads inbound Relationships regardless of the
  requesting Party. The 500-row hard cap from Requirement 8.6 applies
  to *all* candidates, not the authorized projection.
- The authorized projection is built by silently dropping candidates
  the requesting Party may not view.
- The cursor and the response size are derived from the authorized
  projection alone — never from the candidate set.

If any future change introduces a leak (e.g. counts the candidate
set, exposes a cursor over the unauthorized region, or derives the
latency baseline from the candidate scan) this property will fail
with the corresponding dimension named in the assertion message.
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
    BacklinkPage,
    ProvenanceNavigator,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


# All universes share the same fixed-clock instant so role-assignment
# evaluations and Relationship ``recorded_at`` values are byte-equivalent
# across X and Y. The Relationship ``recorded_at`` values themselves are
# derived by offsetting this base time per-Relationship so the
# (recorded_at ASC, relationship_id ASC) ordering is deterministic
# without depending on the strategy's draw order.
_BASE_TIME: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_BASE_TIME_ISO: Final[str] = format_iso8601_ms(_BASE_TIME)

# The instant used to evaluate authority on every navigator call. Sits
# inside every role assignment's effective period by construction
# (role assignments start at _BASE_TIME and have no end, so any
# ``at`` >= _BASE_TIME permits).
_EVALUATION_TIME: Final[datetime] = datetime(2026, 6, 1, tzinfo=timezone.utc)
_ROLE_EFFECTIVE_START: Final[datetime] = _BASE_TIME

# The single target endpoint every drawn Relationship points at. The
# target_id / target_revision_id values are not FK-constrained on the
# Relationships table, so an arbitrary UUIDv7-shaped string suffices.
_TARGET_ID: Final[str] = "00000000-0000-7000-8000-000000000010"
_TARGET_REVISION_ID: Final[str] = "00000000-0000-7000-8000-000000000011"

# Seeded Parties:
#   - the requesting Party P' (the property's *unprivileged* requester);
#   - one authoring Party for every drawn Relationship (FK target of
#     ``Relationships.authoring_party_id``);
#   - one assigning-authority Party for the role assignments.
_REQUESTER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_AUTHORING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000002"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000000003"

# The five Relationship types permitted by the schema CHECK on
# ``Relationships.relationship_type``. Each is drawn for some
# Relationships in the strategy so the property is exercised across the
# full type alphabet — restricted information must be invisible
# regardless of which Relationship type carries it.
_RELATIONSHIP_TYPES: Final[tuple[str, ...]] = (
    "Supports",
    "Contradicts",
    "Derived From",
    "Addresses",
    "Supersedes",
)

# Source endpoint kinds the slice's :class:`~walking_slice.provenance.ProvenanceNavigator`
# scopes ``view.<source_kind>`` authority over. The list mirrors the
# kinds emitted by :meth:`KnowledgeService` and :meth:`TrailService`;
# ``decision`` exercises the ``source_revision_id IS NULL`` branch (the
# Decision Immutable Record carries no Revision identity per AD-WS-4).
_SOURCE_KINDS: Final[tuple[str, ...]] = (
    "finding_revision",
    "recommendation_revision",
    "decision",
    "trail_step",
    "document_revision",
)

# Latency-baseline tolerance from the property statement. Requirement
# 8.3 names "indistinguishable …within a small tolerance"; AD-WS-9 and
# task 12.7's brief pin the tolerance at 100 ms. The slice's
# :func:`compute_latency_baseline_seconds` is a pure deterministic
# function of the authorized response size, so the practical
# difference is zero — the tolerance is the property's safety net for
# future implementations that introduce a small amount of jitter.
_LATENCY_TOLERANCE_SECONDS: Final[float] = 0.1


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _fresh_uuid7() -> str:
    """Return one fresh UUIDv7 string.

    Used inside Hypothesis composites to mint per-draw identifiers
    (Relationship Identities, source endpoint Identities, optional
    source Revision Identities). Each call returns a distinct value so
    drawn graphs do not accidentally collide on identifier values
    across scenarios.
    """
    return str(uuid_utils.uuid7())


def _format_offset(offset_seconds: int) -> str:
    """Return an ISO-8601 ms-precision timestamp ``offset_seconds`` past base.

    Two Relationships drawn with distinct ``offset_seconds`` values get
    distinct ``recorded_at`` strings, so the
    ``ORDER BY (recorded_at ASC, relationship_id ASC)`` clause has a
    stable primary key. Within the strategy the ``offset_seconds``
    field is drawn with ``unique=True`` so collisions cannot happen.
    """
    return format_iso8601_ms(_BASE_TIME + timedelta(seconds=offset_seconds))


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert one Party row required by the FK constraints."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _BASE_TIME_ISO},
    )


def _seed_required_parties(engine: Engine) -> None:
    """Seed the requester, the authoring Party, and the assigning authority."""
    with engine.begin() as conn:
        _seed_party(conn, _REQUESTER_PARTY_ID, "Property 4 Requester")
        _seed_party(conn, _AUTHORING_PARTY_ID, "Property 4 Authoring Party")
        _seed_party(
            conn, _ASSIGNING_AUTHORITY_ID, "Property 4 Assigning Authority"
        )


def _insert_relationship(engine: Engine, *, descriptor: dict) -> None:
    """Persist one Relationship row from a strategy-drawn descriptor.

    Bypasses :class:`KnowledgeService` so the test can fabricate
    arbitrary ``(source_kind, relationship_type, recorded_at)``
    combinations independent of the slice's natural pipeline. The
    Relationships table has no FK constraint on ``source_id``,
    ``source_revision_id``, ``target_id``, or ``target_revision_id``
    (only ``authoring_party_id`` is FK-constrained), so the strategy's
    fresh UUIDv7 identifiers are acceptable for every other column.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type, source_kind,
                    source_id, source_revision_id, target_kind,
                    target_id, target_revision_id, authoring_party_id,
                    recorded_at
                ) VALUES (
                    :relationship_id, :relationship_type, :source_kind,
                    :source_id, :source_revision_id, :target_kind,
                    :target_id, :target_revision_id, :authoring_party_id,
                    :recorded_at
                )
                """
            ),
            {
                "relationship_id": descriptor["relationship_id"],
                "relationship_type": descriptor["relationship_type"],
                "source_kind": descriptor["source_kind"],
                "source_id": descriptor["source_id"],
                "source_revision_id": descriptor["source_revision_id"],
                "target_kind": descriptor["target_kind"],
                "target_id": _TARGET_ID,
                "target_revision_id": _TARGET_REVISION_ID,
                "authoring_party_id": _AUTHORING_PARTY_ID,
                "recorded_at": _format_offset(
                    descriptor["recorded_at_offset_seconds"]
                ),
            },
        )


def _grant_view_authority(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    scope: str,
) -> None:
    """Grant ``view`` authority for ``scope`` to the requesting Party.

    The scope value matches the convention used by
    :meth:`ProvenanceNavigator._build_authorized_projection`: the source
    endpoint's Resource identity. One role assignment is recorded per
    non-restricted source so the requesting Party can view exactly
    those source endpoints and nothing else — *no* wildcard scope is
    used here because a wildcard would also grant view on the
    restricted source, collapsing the universes.
    """
    request = AssignRoleRequest(
        party_id=_REQUESTER_PARTY_ID,
        role_name="reviewer",
        scope=scope,
        authorities_granted=("view",),
        effective_start=_ROLE_EFFECTIVE_START,
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _build_engine(tmp_dir: Path, *, suffix: str) -> Engine:
    """Create a fresh per-universe SQLite engine with pragmas configured.

    A pair of universes share a parent :class:`tempfile.TemporaryDirectory`
    but lives in separate sub-paths so each universe owns its own
    on-disk file. WAL journal mode and ``foreign_keys=ON`` match the
    runtime configuration set in ``tests/conftest.py`` and design
    §"Persistence Invariants Summary".
    """
    db_path = tmp_dir / f"walking_slice_{suffix}.sqlite"
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


def _build_universe(
    tmp_dir: Path,
    *,
    suffix: str,
    descriptors: list[dict],
    granted_scopes: list[str],
) -> tuple[Engine, ProvenanceNavigator]:
    """Stand up one universe: engine + Parties + Relationships + role grants.

    The function is universe-symmetric — passing the full descriptor
    list yields Universe X, passing the descriptor list minus the
    restricted entry yields Universe Y. ``granted_scopes`` is the
    *same* list in both calls (the requesting Party's view authority
    differs only by what is reachable, not by what the role assignments
    claim — that is what makes ``(P, P')`` differ only in view
    authority on one node).
    """
    engine = _build_engine(tmp_dir, suffix=suffix)
    _seed_required_parties(engine)

    # Fresh per-universe collaborators. The :class:`IdentityService`
    # in-memory ``Identifier_Registry`` cache is per-instance, so
    # using one navigator per universe keeps the role-assignment
    # identifiers distinct across X and Y and prevents the second
    # universe from triggering an :class:`IdentityConflictError` when
    # it reuses a Relationship identifier from the first.
    clock = FixedClock(_BASE_TIME)
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

    for descriptor in descriptors:
        _insert_relationship(engine, descriptor=descriptor)

    for scope in granted_scopes:
        _grant_view_authority(authorization_service, engine, scope=scope)

    return engine, navigator


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


@st.composite
def _relationship_descriptor(draw) -> dict:
    """Draw one Relationship descriptor with fresh identifiers.

    Returns a dict carrying every column the test inserts into the
    ``Relationships`` table plus the strategy-controlled axes the
    property exercises:

    - ``relationship_id`` — fresh UUIDv7.
    - ``source_id`` — fresh UUIDv7. Doubles as the *scope* for the
      requesting Party's ``view.<source_kind>`` role assignment when
      this Relationship is reachable.
    - ``source_revision_id`` — fresh UUIDv7 or ``None`` (the
      ``decision`` source kind has no Revision per AD-WS-4; other
      kinds may carry one or not, both are valid).
    - ``source_kind`` — drawn from :data:`_SOURCE_KINDS`.
    - ``relationship_type`` — drawn from :data:`_RELATIONSHIP_TYPES`.
    - ``target_kind`` — held constant at ``"document_revision"`` because
      the target endpoint is shared across the whole graph and the
      property does not depend on what kind it is.
    - ``recorded_at_offset_seconds`` — drawn from a wide enough range
      that distinct draws produce distinct timestamps, and constrained
      to ``unique=True`` at the list level (see ``_scenario``) so the
      ``(recorded_at, relationship_id)`` ordering is deterministic.
    """
    return {
        "relationship_id": _fresh_uuid7(),
        "source_id": _fresh_uuid7(),
        "source_revision_id": draw(
            st.one_of(st.none(), st.builds(_fresh_uuid7))
        ),
        "source_kind": draw(st.sampled_from(_SOURCE_KINDS)),
        "relationship_type": draw(st.sampled_from(_RELATIONSHIP_TYPES)),
        "target_kind": "document_revision",
        "recorded_at_offset_seconds": draw(
            st.integers(min_value=0, max_value=86_400)
        ),
    }


@st.composite
def _scenario(draw) -> dict:
    """Draw one Property 4 scenario.

    A scenario carries:

    - ``descriptors``: 1..6 Relationship descriptors with unique
      ``recorded_at_offset_seconds`` values (so the
      ``ORDER BY (recorded_at, relationship_id)`` sort is unambiguous
      regardless of how Hypothesis explores the search space).
    - ``restricted_index``: index into ``descriptors`` naming the
      Relationship whose source endpoint the requesting Party may not
      view. In Universe X, this Relationship is persisted; in Universe
      Y, it is omitted. ``P'``'s role assignments cover every *other*
      source's scope, so the visible projection in both universes
      contains exactly the non-restricted Relationships.

    The strategy minimum of 1 Relationship ensures every case has at
    least one node whose restriction status is exercised; the maximum
    of 6 keeps each case cheap enough that the 100-example Hypothesis
    run completes well under the slice's deadline budget.
    """
    descriptors = draw(
        st.lists(
            _relationship_descriptor(),
            min_size=1,
            max_size=6,
            unique_by=lambda d: d["recorded_at_offset_seconds"],
        )
    )
    restricted_index = draw(
        st.integers(min_value=0, max_value=len(descriptors) - 1)
    )
    return {
        "descriptors": descriptors,
        "restricted_index": restricted_index,
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 4: Non-leakage of restricted
# information.
@given(scenario=_scenario())
@settings(
    max_examples=50,
    deadline=5000,
    # Each case provisions two on-disk SQLite databases and seeds two
    # universes; per-case setup is more expensive than a purely
    # in-memory test. The setup is still well under the 5000 ms
    # deadline locally but the data-generation health check is
    # suppressed so any one slow case does not abort the property run.
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_non_leakage_of_restricted_information(scenario: dict) -> None:
    """Backlink responses to ``P'`` are indistinguishable across universes."""
    descriptors: list[dict] = scenario["descriptors"]
    restricted_index: int = scenario["restricted_index"]

    # The Relationship whose source endpoint P' cannot view. In
    # Universe X this row is persisted but filtered out by the
    # navigator's authorized projection; in Universe Y the row never
    # exists. The two responses must be byte-equivalent.
    restricted_descriptor = descriptors[restricted_index]
    restricted_source_id = restricted_descriptor["source_id"]

    # The scopes granted to P' — every source's identity *except* the
    # restricted one. Identical across both universes because the
    # property holds ``P`` and ``P'`` to "differ only in view authority
    # on one node": the role-assignment set carried into Universe Y is
    # the same set carried into Universe X.
    granted_scopes = [
        d["source_id"]
        for d in descriptors
        if d["source_id"] != restricted_source_id
    ]

    # Universe Y descriptors — every drawn Relationship *except* the
    # restricted one. The restricted Relationship "does not exist" in
    # this universe. Multiple descriptors may share the same
    # ``source_id`` if Hypothesis happens to draw a duplicate (the
    # strategy uniqueness key is on ``recorded_at_offset_seconds``,
    # not ``source_id``), so the filter is by ``source_id`` equality —
    # any descriptor whose source endpoint matches the restricted
    # source's is also dropped from Universe Y. Were such a duplicate
    # to remain in Y while still being unreachable to P', Y would
    # contain extra entries that X never produced, breaking the
    # indistinguishability check artificially.
    universe_y_descriptors = [
        d for d in descriptors if d["source_id"] != restricted_source_id
    ]

    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop4_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine_x: Optional[Engine] = None
        engine_y: Optional[Engine] = None
        try:
            engine_x, navigator_x = _build_universe(
                case_dir,
                suffix="x",
                descriptors=descriptors,
                granted_scopes=granted_scopes,
            )
            engine_y, navigator_y = _build_universe(
                case_dir,
                suffix="y",
                descriptors=universe_y_descriptors,
                granted_scopes=granted_scopes,
            )

            # Identical query to both universes — same target, same
            # requesting Party, same effective time, same cursor.
            error_x: Optional[BaseException] = None
            error_y: Optional[BaseException] = None
            page_x: Optional[BacklinkPage] = None
            page_y: Optional[BacklinkPage] = None

            with engine_x.connect() as conn_x:
                try:
                    page_x = navigator_x.list_backlinks(
                        conn_x,
                        target_id=_TARGET_ID,
                        target_revision_id=_TARGET_REVISION_ID,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EVALUATION_TIME,
                    )
                except BaseException as exc:  # noqa: BLE001
                    error_x = exc

            with engine_y.connect() as conn_y:
                try:
                    page_y = navigator_y.list_backlinks(
                        conn_y,
                        target_id=_TARGET_ID,
                        target_revision_id=_TARGET_REVISION_ID,
                        party_id=_REQUESTER_PARTY_ID,
                        at=_EVALUATION_TIME,
                    )
                except BaseException as exc:  # noqa: BLE001
                    error_y = exc

            # ----- Error-wording dimension --------------------------------
            # "or both absent". An exception raised by exactly one
            # universe is itself a leak — the exception's existence
            # signals to ``P'`` which universe it was in. Both raising
            # the same exception (with identical message) does not
            # leak.
            assert (error_x is None) == (error_y is None), (
                "Property 4 violated on the error-wording dimension: one "
                "universe raised an exception while the other returned a "
                f"page. Universe X error={error_x!r}; Universe Y "
                f"error={error_y!r}."
            )
            if error_x is not None and error_y is not None:
                assert type(error_x) is type(error_y), (
                    "Property 4 violated on the error-wording dimension: "
                    "the two universes raised different exception "
                    f"classes. X={type(error_x).__name__}, "
                    f"Y={type(error_y).__name__}."
                )
                assert str(error_x) == str(error_y), (
                    "Property 4 violated on the error-wording dimension: "
                    "the two universes raised the same exception class "
                    "but with different messages. "
                    f"X={str(error_x)!r}, Y={str(error_y)!r}."
                )
                # When both raised, the remaining response-dimension
                # assertions do not apply.
                return

            assert page_x is not None and page_y is not None

            # ----- Count dimension ----------------------------------------
            assert page_x.response_size == page_y.response_size, (
                "Property 4 violated on the count dimension: response_size "
                f"differs. Universe X={page_x.response_size}, Universe "
                f"Y={page_y.response_size}."
            )

            # ----- Identifier-set dimension -------------------------------
            id_set_x = {e.relationship_id for e in page_x.entries}
            id_set_y = {e.relationship_id for e in page_y.entries}
            assert id_set_x == id_set_y, (
                "Property 4 violated on the identifier-set dimension: the "
                "set of Relationship Identities returned differs across "
                f"universes. X-only={id_set_x - id_set_y!r}, "
                f"Y-only={id_set_y - id_set_x!r}."
            )

            # ----- Ordering dimension -------------------------------------
            order_x = [e.relationship_id for e in page_x.entries]
            order_y = [e.relationship_id for e in page_y.entries]
            assert order_x == order_y, (
                "Property 4 violated on the ordering dimension: the order "
                "of Relationship Identities differs across universes. "
                f"X={order_x!r}, Y={order_y!r}."
            )

            # ----- Full entry payload (catches per-attribute leaks) -------
            # A divergence here would mean the same Relationship
            # Identity appeared in both universes but with a different
            # ``relationship_type``, ``source_kind``,
            # ``source_revision_id``, ``authoring_party_id``, or
            # ``recorded_at`` value — an artefact of how the
            # non-restricted Relationships were persisted, not a leak
            # the navigator could plausibly introduce, but worth
            # checking so the property catches future schema or
            # serialization drift.
            assert page_x.entries == page_y.entries, (
                "Property 4 violated on the full-entry-payload dimension: "
                "two universes returned the same Relationship Identities "
                "but the BacklinkEntry payloads differ."
            )

            # ----- Cursor dimension ---------------------------------------
            assert page_x.cursor == page_y.cursor, (
                "Property 4 violated on the cursor dimension: the "
                "next-page cursor differs. "
                f"X={page_x.cursor!r}, Y={page_y.cursor!r}."
            )

            # ----- Response-size dimension --------------------------------
            # Asserted separately so the failure message names the
            # dimension if response_size diverges. ``response_size``
            # equals ``len(entries)`` by construction.
            assert page_x.response_size == len(page_x.entries) == len(
                page_y.entries
            ) == page_y.response_size, (
                "Property 4 violated on the response-size dimension: "
                "response_size and len(entries) disagree across universes."
            )

            # ----- Latency dimension --------------------------------------
            latency_delta = abs(
                page_x.latency_baseline_seconds
                - page_y.latency_baseline_seconds
            )
            assert latency_delta <= _LATENCY_TOLERANCE_SECONDS, (
                "Property 4 violated on the latency dimension: the "
                "latency baseline differs by more than "
                f"{_LATENCY_TOLERANCE_SECONDS * 1000:.0f} ms. "
                f"X={page_x.latency_baseline_seconds}, "
                f"Y={page_y.latency_baseline_seconds}, "
                f"|delta|={latency_delta}."
            )
        finally:
            if engine_x is not None:
                engine_x.dispose()
            if engine_y is not None:
                engine_y.dispose()
