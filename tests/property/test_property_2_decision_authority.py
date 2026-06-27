# Feature: first-walking-slice, Property 2: Decision authority
"""Property 2 — Decision authority (task 8.5).

**Property 2: Decision authority**

For all Decision Immutable Records, there exists a Role Assignment for
the deciding Party whose granted authorities include ``approve``, whose
scope covers the target Recommendation Revision, and whose effective
period encloses the Decision's recorded time. No Decision Record exists
without a matching authority record.

**Validates: Requirements 6.1, 6.2, 7.1, 7.3, 7.5, 12.2, 12.3, 12.4,
15.2**

Strategy:

Each Hypothesis case draws a *scenario* containing:

- a set of Parties (1..3 deciding Parties plus one fixed
  assigning-authority Party);
- for each Party, a list of 0..3 Role Assignments whose dimensions
  vary independently along the five gating axes called out by the
  task description — ``effective_start`` offset, ``effective_end``
  offset (or ``None``), revocation offset (or ``None``), ``scope``
  drawn from a small alphabet that includes the wildcard ``"*"``, and
  a subset of granted authorities drawn from
  ``{"view", "modify", "approve"}``;
- a single ``target_scope`` used for every Decision attempt in the
  case;
- 1..5 Decision *attempts*, each picking a Party index and a fresh
  Recommendation to address.

Per case the test spins up a fresh per-test SQLite engine + schema, a
shared :class:`~walking_slice.clock.FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` (so every Decision in the case carries the
same recorded time, which keeps the assertion deterministic across
shrinks), and the full authorization-wired
:class:`~walking_slice.knowledge.KnowledgeService` pipeline. It then:

1. Seeds the deciding Parties and the assigning-authority Party
   (FK targets for Role Assignments, Findings, Recommendations,
   Decisions, and Audit Records).
2. Assigns every drawn Role Assignment via
   :meth:`AuthorizationService.assign_role`; assignments whose
   drawn parameters violate Requirement 12.6 (empty authorities) are
   skipped at the strategy boundary rather than persisted.
3. Stamps ``revoked_at`` directly via UPDATE for any assignment whose
   strategy drew a revocation offset (mirroring the helper in
   ``tests/unit/test_knowledge_decision_authority.py``); the
   ``Role_Assignments_revoked_at_one_shot`` trigger guarantees the
   one-shot semantics regardless.
4. Seeds one hypothesis Finding and one Recommendation per Decision
   attempt so the ``UNIQUE(target_recommendation_id,
   target_recommendation_revision_id)`` constraint on ``Decisions``
   (Requirement 6.5) cannot accidentally reject a later attempt for
   reasons orthogonal to authority.
5. Attempts every Decision in order. Each attempt either persists a
   Decision (the wired
   :class:`~walking_slice.authorization.AuthorizationService` permits
   the action) or raises :class:`DecisionAuthorizationError` (the
   service denies it); both outcomes are accepted by the property —
   the assertion holds over the rows that *did* land.

After every attempt is processed the test queries ``Decisions`` and,
for each persisted row, scans ``Role_Assignments`` directly for a row
that simultaneously:

- belongs to ``deciding_party_id``;
- carries ``"approve"`` in ``authorities_granted``;
- covers ``applicable_scope`` (either ``"*"`` or an exact match);
- has ``effective_start <= recorded_at``
  (not-yet-effective is not violated);
- has ``effective_end IS NULL`` *or* ``effective_end > recorded_at``
  (not expired);
- has ``revoked_at IS NULL`` *or* ``revoked_at > recorded_at``
  (not revoked).

The predicate is the same one the
:class:`~walking_slice.authorization.AuthorizationService` itself
applies; the property is therefore a *post-hoc* end-to-end check that
the service never persists a Decision when no such Role Assignment
exists. The test reads the database directly (rather than through the
service) so the assertion catches any future regression that leaks a
Decision past the authority gate — for example a code path that
forgets to call :meth:`AuthorizationService.evaluate`, or that
substitutes one authority type for another in violation of
Requirements 12.3 / 12.4.

Requirement coverage notes:

- **6.1** — every Decision row resolves to an existing Recommendation
  Revision (the strategy seeds one per attempt; the FK enforces the
  rest).
- **6.2** — every Decision row carries the four Requirement 6.2
  attributes; the strategy supplies non-empty ``rationale`` and
  ``applicable_scope``.
- **7.1, 7.5** — the property's "no Decision without a matching
  authority record" clause asserts the absence of any persisted
  Decision whose authority was missing or not-in-effect.
- **7.3** — the matching predicate enforces the effective-period and
  scope-coverage rules verbatim against ``Role_Assignments``.
- **12.2** — the strategy varies dimensions that drive every
  Requirement 7.2 / 12.2 reason code (``not-yet-effective``,
  ``expired``, ``revoked``, ``out-of-scope``, and the
  empty-authorities path that surfaces as
  ``no-role-assignment``); the property holds across all of them.
- **12.3, 12.4** — ``"approve"`` is checked positively in the
  matching predicate; assignments granting only ``view`` or
  ``modify`` cannot satisfy it, so a Decision permitted on the basis
  of a non-``approve`` role would falsify the property immediately.
- **15.2** — the Hypothesis settings register ``max_examples=100``
  and ``deadline=2000`` per Requirement 15.13.

Test scaffolding follows the conventions of
``tests/property/test_property_1_evidence_support.py`` and
``tests/property/test_property_14_identity_survives_rename.py``: a
:class:`tempfile.TemporaryDirectory` owns the per-case SQLite file (so
state cannot leak between Hypothesis cases the way a function-scoped
pytest fixture would), and pragma-aware engine setup matches the
conftest fixtures exactly.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    DecisionAuthorizationError,
    KnowledgeService,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants — the recorded-time anchor and the assigning-authority
# Party. The Decision's ``recorded_at`` is derived from
# :class:`FixedClock` so every Decision in a case shares the same
# recorded time, which keeps the property assertion deterministic across
# Hypothesis shrinks. The assigning-authority Party is the actor on
# every Role_Assignments row and on every assignment-audit row.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

_PARTY_BASE: Final[str] = "00000000-0000-7000-8000-0000000a0"
_ASSIGNING_AUTHORITY_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000b0001"
)
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-0000000c0001"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Scope alphabet. The wildcard ``"*"`` is included so the strategy
# exercises ``AuthorizationService._scope_covers``'s wildcard branch as
# well as the equality branch. The scope list is deliberately small so
# Hypothesis can cover the scope-mismatch axis without an explosion of
# unrelated alphabet variations.
_SCOPES: Final[tuple[str, ...]] = ("scope-a", "scope-b", "scope-c")
_ROLE_SCOPES: Final[tuple[str, ...]] = _SCOPES + ("*",)

# Authority alphabet — the three Requirement-12.3 / 12.4 authority
# types. Subsets of this set are drawn to exercise the
# "authority does not include approve" axis without substituting one
# authority for another.
_AUTHORITIES: Final[tuple[str, ...]] = ("view", "modify", "approve")


def _party_id(index: int) -> str:
    """Stable UUIDv7-shaped Party Identity for a given index.

    The strategy draws 1..3 Parties per case; this helper formats a
    canonical UUIDv7 string (the regex in
    :data:`walking_slice.identity.CANONICAL_UUID7_REGEX`) by tacking
    the index onto a shared prefix. Stable IDs make shrinkage
    diagnostics easier to read.
    """
    return f"{_PARTY_BASE}{index:03d}"


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert a Party row required by the FK constraints on
    ``Role_Assignments.party_id``,
    ``Document_Revisions.contributing_party_id``,
    ``Finding_Revisions.authoring_party_id``,
    ``Recommendation_Revisions.authoring_party_id``,
    ``Decisions.deciding_party_id``, and
    ``Audit_Records.actor_party_id``."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# The five axes called out by the task description map onto five
# independent draws per Role Assignment, sampled from small alphabets so
# Hypothesis can cover every combination over the course of a 100-case
# run without spending its budget on unrelated variations.
# ---------------------------------------------------------------------------


# Day offsets relative to :data:`_NOW`. ``effective_start`` ranges from
# -30 days (well in the past, so the assignment is effective) to +30
# days (in the future, triggering ``not-yet-effective``). Optional
# ``effective_end`` / ``revoked_at`` use the same span; ``None`` is
# drawn ~50% of the time so the strategy explores both the bounded and
# the open-ended cases evenly.
_offset_days_strategy = st.integers(min_value=-30, max_value=30)
_optional_offset_days_strategy = st.one_of(
    st.none(),
    st.integers(min_value=-30, max_value=30),
)


# Authorities are drawn as a non-empty subset of ``{view, modify,
# approve}``. ``min_size=1`` matches Requirement 12.6 — an empty
# ``authorities_granted`` list is rejected at the
# :meth:`AuthorizationService.assign_role` boundary, so the strategy
# would otherwise either crash with :class:`InvalidRoleAssignmentError`
# or never persist anything for these draws.
_authorities_subset_strategy = st.sets(
    st.sampled_from(_AUTHORITIES), min_size=1, max_size=3
)


_scope_strategy = st.sampled_from(_ROLE_SCOPES)


@st.composite
def _role_assignment_draw(draw) -> dict:
    """Draw one Role Assignment as a dict of strategy outputs.

    Returns a dict with keys ``scope``, ``authorities``,
    ``effective_start_offset``, ``effective_end_offset`` (or ``None``),
    and ``revoked_offset`` (or ``None``). The five fields independently
    drive the five gating dimensions named in the task description —
    every Role Assignment that ends up matching the persisted Decision
    must, by construction, satisfy *all five* simultaneously.
    """
    return {
        "scope": draw(_scope_strategy),
        "authorities": sorted(draw(_authorities_subset_strategy)),
        "effective_start_offset": draw(_offset_days_strategy),
        "effective_end_offset": draw(_optional_offset_days_strategy),
        "revoked_offset": draw(_optional_offset_days_strategy),
    }


@st.composite
def _scenario_strategy(draw) -> dict:
    """Draw a full scenario for one Hypothesis case.

    Bundles the deciding Parties, their Role Assignments, the target
    scope used by every Decision attempt, and the per-attempt Party
    indices into one value the test consumes top-to-bottom. Keeping
    the scenario in one strategy lets Hypothesis shrink the whole
    case coherently — for example, shrinking down to a single
    Party with a single Role Assignment and a single Decision attempt
    yields the smallest counterexample if the property is ever
    falsified.
    """
    num_parties = draw(st.integers(min_value=1, max_value=3))
    party_assignments = [
        draw(
            st.lists(_role_assignment_draw(), min_size=0, max_size=3)
        )
        for _ in range(num_parties)
    ]
    target_scope = draw(st.sampled_from(_SCOPES))
    num_attempts = draw(st.integers(min_value=1, max_value=5))
    attempt_party_indices = [
        draw(st.integers(min_value=0, max_value=num_parties - 1))
        for _ in range(num_attempts)
    ]
    return {
        "num_parties": num_parties,
        "party_assignments": party_assignments,
        "target_scope": target_scope,
        "attempt_party_indices": attempt_party_indices,
    }


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, audit rows, role assignments, and
# Decisions cannot leak between cases (design §"Testing Strategy" —
# "Each property and example test gets a fresh SQLite database").
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
# Role-assignment seeding helpers.
#
# Assignments are persisted via the public
# :meth:`AuthorizationService.assign_role` surface so the property is
# exercised against the same code path production uses. Revocation is
# stamped via direct UPDATE (matching the helper in
# ``tests/unit/test_knowledge_decision_authority.py``) because the
# revocation HTTP endpoint added by task 3.3 sits above the service
# layer this property test targets.
# ---------------------------------------------------------------------------


def _assign(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str,
    authorities: list[str],
    effective_start: datetime,
    effective_end: Optional[datetime],
) -> str:
    """Persist one Role Assignment and return its ``role_assignment_id``."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="decision_maker",
        scope=scope,
        authorities_granted=tuple(authorities),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _stamp_revoked_at(engine: Engine, role_assignment_id: str, when: datetime) -> None:
    """Stamp ``revoked_at`` on a Role Assignment via direct UPDATE.

    The ``Role_Assignments_revoked_at_one_shot`` trigger enforces the
    one-shot semantic regardless of how the column is mutated; using
    UPDATE here keeps the property test independent of the HTTP
    revocation endpoint added by task 3.3.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(when), "rid": role_assignment_id},
        )


# ---------------------------------------------------------------------------
# Database probe helpers used in the assertion loop.
# ---------------------------------------------------------------------------


def _fetch_decisions(engine: Engine) -> list[dict[str, Any]]:
    """Return every persisted Decision row in append order.

    Each Decision is the subject of one Property-2 invariant
    assertion. The columns returned mirror the
    :class:`~walking_slice.knowledge.CreateDecisionResult` payload but
    are sourced from the database directly so the property exercises
    the persisted-state contract rather than the in-memory return
    value.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT decision_id, target_recommendation_id,
                           target_recommendation_revision_id, outcome,
                           rationale, deciding_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Decisions
                    ORDER BY recorded_at, decision_id
                    """
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_role_assignments_for_party(
    engine: Engine, *, party_id: str
) -> list[dict[str, Any]]:
    """Return every Role Assignment row recorded for ``party_id``.

    Property 2's quantifier is "there exists a Role Assignment for the
    deciding Party"; this read fetches the candidate set for that
    existential check.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT role_assignment_id, party_id, role_name, scope,
                           authorities_granted, effective_start,
                           effective_end, revoked_at
                    FROM Role_Assignments
                    WHERE party_id = :pid
                    """
                ),
                {"pid": party_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _role_matches_decision(
    role: dict[str, Any],
    *,
    target_scope: str,
    recorded_at_iso: str,
) -> bool:
    """Return ``True`` iff ``role`` satisfies Property 2 for the Decision.

    The predicate mirrors :class:`AuthorizationService` exactly:

    - The role must grant ``"approve"`` (Requirement 12.3 / 12.4 —
      no substitution between view/modify/approve).
    - The role's ``scope`` must cover ``target_scope``
      (``"*"`` wildcard or exact equality).
    - The Decision's ``recorded_at`` must fall inside the role's
      effective period:
      ``effective_start <= recorded_at < effective_end_or_inf`` and
      either ``revoked_at`` is unset or ``revoked_at > recorded_at``.

    String comparisons are correct here because every timestamp
    column is stored in the lexicographically sortable
    ``YYYY-MM-DDTHH:MM:SS.mmmZ`` form used by
    :func:`walking_slice.audit.format_iso8601_ms`.
    """
    try:
        authorities = json.loads(role["authorities_granted"])
    except (TypeError, ValueError):
        return False
    if "approve" not in authorities:
        return False
    scope = role["scope"]
    if scope != "*" and scope != target_scope:
        return False
    effective_start = role["effective_start"]
    if effective_start > recorded_at_iso:
        # not-yet-effective
        return False
    effective_end = role["effective_end"]
    if effective_end is not None and effective_end <= recorded_at_iso:
        # expired
        return False
    revoked_at = role["revoked_at"]
    if revoked_at is not None and revoked_at <= recorded_at_iso:
        # revoked
        return False
    return True


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 2: Decision authority
@given(scenario=_scenario_strategy())
@settings(max_examples=100, deadline=2000)
def test_decision_authority(scenario: dict) -> None:
    """Every persisted Decision Immutable Record has a matching
    ``approve``-bearing Role Assignment whose scope covers the target
    and whose effective period encloses the Decision's recorded
    time."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop2_"
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh per-case services so cross-case IdentityService state
        # cannot leak. The FixedClock anchors every persisted
        # ``recorded_at`` to the same instant, which keeps the property
        # assertion deterministic and Hypothesis shrinkage tractable.
        clock = FixedClock(_NOW)
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        authorization_service = AuthorizationService(
            clock=clock,
            audit_log=audit_log,
            identity_service=identity_service,
        )
        # The unwired service seeds Findings and Recommendations
        # without invoking the Recommendation-creation authority check
        # — that check is Requirement 5.7's, not Property 2's, and we
        # do not want to entangle the two properties.
        knowledge_unwired = KnowledgeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )
        knowledge_authorized = KnowledgeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        )

        try:
            # 1. Seed all Parties (deciding Parties plus the assigning
            #    authority). One transaction keeps the FK targets
            #    visible to every later write.
            party_ids = [
                _party_id(i) for i in range(scenario["num_parties"])
            ]
            with engine.begin() as conn:
                _seed_party(
                    conn,
                    _ASSIGNING_AUTHORITY_ID,
                    "Property 2 Assigning Authority",
                )
                for index, pid in enumerate(party_ids):
                    _seed_party(conn, pid, f"Property 2 Party {index}")

            # 2. Persist every drawn Role Assignment. Skipping
            #    assignments where ``effective_end <= effective_start``
            #    keeps the input space valid without changing the
            #    property under test — such assignments are never
            #    permitting anyway because the auth service would
            #    evaluate them as both not-yet-effective and expired
            #    at every instant; pruning them upstream keeps the
            #    persisted Role_Assignments table free of provably
            #    dead rows.
            for party_index, assignments in enumerate(
                scenario["party_assignments"]
            ):
                pid = party_ids[party_index]
                for assignment in assignments:
                    eff_start = _NOW + timedelta(
                        days=assignment["effective_start_offset"]
                    )
                    eff_end: Optional[datetime] = None
                    if assignment["effective_end_offset"] is not None:
                        eff_end = _NOW + timedelta(
                            days=assignment["effective_end_offset"]
                        )
                        if eff_end <= eff_start:
                            # Skip — see the comment above.
                            continue
                    rid = _assign(
                        authorization_service,
                        engine,
                        party_id=pid,
                        scope=assignment["scope"],
                        authorities=assignment["authorities"],
                        effective_start=eff_start,
                        effective_end=eff_end,
                    )
                    if assignment["revoked_offset"] is not None:
                        revoked_at = _NOW + timedelta(
                            days=assignment["revoked_offset"]
                        )
                        _stamp_revoked_at(engine, rid, revoked_at)

            # 3. Seed one hypothesis Finding and one Recommendation per
            #    Decision attempt. Unique Recommendation Revisions
            #    sidestep the Requirement-6.5 UNIQUE constraint on
            #    ``Decisions(target_recommendation_id,
            #    target_recommendation_revision_id)``.
            num_attempts = len(scenario["attempt_party_indices"])
            recommendations = []
            with engine.begin() as conn:
                for attempt_index in range(num_attempts):
                    finding = knowledge_unwired.create_finding(
                        conn,
                        statement=(
                            f"Property 2 source finding {attempt_index}."
                        ),
                        # Any of the seeded Parties may be the
                        # authoring Party; party 0 is convenient and
                        # has no bearing on the Decision authority
                        # check (which is keyed on the *deciding*
                        # Party of the Decision, not the authoring
                        # Party of the Finding/Recommendation).
                        authoring_party_id=party_ids[0],
                        is_hypothesis=True,
                    )
                    recommendation = knowledge_unwired.create_recommendation(
                        conn,
                        authoring_party_id=party_ids[0],
                        derived_from_findings=[finding.finding_id],
                        rationale=(
                            f"Property 2 recommendation {attempt_index}."
                        ),
                    )
                    recommendations.append(recommendation)

            # 4. Attempt every Decision. The wired
            #    :class:`AuthorizationService` either permits the
            #    write (Decision row lands) or denies it (raises
            #    :class:`DecisionAuthorizationError`, the caller's
            #    transaction rolls back, and the denial record is
            #    persisted in a separate transaction). Both outcomes
            #    are accepted by Property 2 — the property only
            #    asserts over the rows that *did* land.
            target_scope = scenario["target_scope"]
            for attempt_index, party_index in enumerate(
                scenario["attempt_party_indices"]
            ):
                deciding_party_id = party_ids[party_index]
                recommendation = recommendations[attempt_index]
                try:
                    with engine.begin() as conn:
                        knowledge_authorized.create_decision(
                            conn,
                            target_recommendation_id=(
                                recommendation.recommendation_id
                            ),
                            target_recommendation_revision_id=(
                                recommendation.recommendation_revision_id
                            ),
                            outcome="Accept",
                            rationale=(
                                f"Property 2 decision {attempt_index}."
                            ),
                            deciding_party_id=deciding_party_id,
                            authority_basis=_BASIS,
                            applicable_scope=target_scope,
                            engine=engine,
                        )
                except DecisionAuthorizationError:
                    # Expected for any attempt the wired authorization
                    # service denies; the property holds vacuously for
                    # denied attempts because no Decision row exists.
                    pass

            # 5. Property assertions — for every persisted Decision,
            #    there exists a matching Role Assignment.
            decisions = _fetch_decisions(engine)
            for decision in decisions:
                candidates = _fetch_role_assignments_for_party(
                    engine, party_id=decision["deciding_party_id"]
                )
                recorded_at_iso = decision["recorded_at"]
                matching = [
                    role
                    for role in candidates
                    if _role_matches_decision(
                        role,
                        target_scope=decision["applicable_scope"],
                        recorded_at_iso=recorded_at_iso,
                    )
                ]
                assert matching, (
                    "Property 2 violated: Decision "
                    f"{decision['decision_id']!r} (deciding_party="
                    f"{decision['deciding_party_id']!r}, "
                    f"applicable_scope="
                    f"{decision['applicable_scope']!r}, "
                    f"recorded_at={recorded_at_iso!r}) has no matching "
                    "Role_Assignments row whose granted authorities "
                    "include 'approve', whose scope covers the target, "
                    "and whose effective period encloses the recorded "
                    f"time. Candidates were: {candidates!r}."
                )
        finally:
            engine.dispose()
