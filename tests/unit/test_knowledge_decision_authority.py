"""Unit tests for :mod:`walking_slice.knowledge` — Decision authority (task 8.2).

These tests pin the contract added in task 8.2, design
§"Decision authority evaluation flow", AD-WS-9 (indistinguishable
denial response), and Requirements 7.1 through 7.6 as they apply to
:meth:`KnowledgeService.create_decision`:

- **7.1** — a Party lacking effective Decision Maker authority for the
  applicable scope is rejected via
  :class:`DecisionAuthorizationError`, and no ``Decisions``,
  ``Relationships``, ``Provenance_Manifests``, ``Omission_Entries``, or
  consequential ``Audit_Records`` row is persisted (the caller's
  transaction rolls back when the exception propagates out of
  ``engine.begin()``).
- **7.2** — every denial appends exactly one immutable Denial Record to
  ``Audit_Records`` with the deciding-Party identity, the attempted
  action (``approve.decision``), the target Recommendation Identity and
  Revision Identity, a Requirement-7.2 reason code, and the correlation
  identifier from the evaluation. The Denial Record is committed in a
  *separate* transaction so it survives the caller's rollback.
- **7.4** — the :class:`DecisionAuthorizationError` carries only the
  reason code and correlation identifier. Two denials for different
  internal causes are structurally indistinguishable apart from those
  two values (Property 4 / AD-WS-9 conformance at the exception layer).
- **7.5** — the target Recommendation Resource and its Revisions are
  byte-equivalent to their pre-attempt state after a denial.
- **7.6** — the Denial Record append is retried up to three times with
  exponential backoff (0.01s, 0.02s, 0.04s). When two attempts fail and
  the third succeeds, the denial is still recorded. When every attempt
  fails, :class:`DecisionAuditFailureError` is raised instead so denial
  and audit cannot silently diverge.

Coverage scope
==============

These tests intentionally complement the task-8.1 tests in
:mod:`tests.unit.test_knowledge_decisions`. That file exercises the
back-compatible code path (``KnowledgeService`` without
``authorization_service`` wired) where the authority check is skipped;
this file exercises the wired path. Tests share the same canonical
``_PARTY_ID``, ``_ASSIGNING_AUTHORITY_ID``, and ``_SCOPE`` constants so
identifiers are stable across both files and the audit-record contents
read coherently when both suites run together.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import fields
from datetime import datetime, timezone
from typing import Iterable, Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditAppendError, AuditLog, AuditRecord
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateDecisionResult,
    CreateFindingResult,
    CreateRecommendationResult,
    DecisionAuditFailureError,
    DecisionAuthorizationError,
    KnowledgeService,
)
from walking_slice.models import AuthorityBasisRef


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seeding helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-0000008a0001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-0000008a0002"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000008a0003"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000008b0001")
_SCOPE = "pilot/team-a"
_OTHER_SCOPE = "pilot/team-b"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

# Hypothetical IDs used by the "engine missing" pre-flight test, which
# never touches the database (the engine-missing check fires first).
_PLACEHOLDER_REC_ID = "00000000-0000-7000-8000-0000007f0001"
_PLACEHOLDER_REV_ID = "00000000-0000-7000-8000-0000007f0002"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# Authority-basis used by every happy-path assertion; AD-WS-10 names this
# as one of the three permitted basis types and is exercised broadly by
# tests/unit/test_knowledge_decisions.py.
_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
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
    """Seed the deciding Party and the assigning-authority Party.

    Both Parties are required by every authorized test: the deciding
    Party is the actor on the Decision, and the assigning-authority
    Party is the actor on any Role_Assignments rows created by
    :func:`_assign_decision_maker_role`.
    """
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Decision Maker")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_service_authorized(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> KnowledgeService:
    """Knowledge_Service with authorization wired (Requirement 7.1 path)."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


@pytest.fixture
def knowledge_service_unwired(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """Knowledge_Service without authorization (back-compat path).

    Used by the ``_seed_recommendation`` helper so the Recommendation
    seeding step does not itself require an authorized caller — the
    authority check under test is the one on the Decision, not the
    one on the Recommendation.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


# ---------------------------------------------------------------------------
# Recommendation seeding (unauthorized side-channel — see fixture docs).
# ---------------------------------------------------------------------------


def _seed_recommendation(
    engine: Engine,
    knowledge_service_unwired: KnowledgeService,
) -> tuple[CreateFindingResult, CreateRecommendationResult]:
    """Seed a hypothesis Finding and a Recommendation derived from it.

    The seeding step uses the *unwired* KnowledgeService because the
    Recommendation's own authority check (Requirement 5.7) is not the
    focus of these tests — the Decision's authority check is. Both
    seeds run inside one transaction so the Recommendation is visible
    to the subsequent Decision attempt.
    """
    with engine.begin() as conn:
        finding = knowledge_service_unwired.create_finding(
            conn,
            statement="Source finding for decision authority tests.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        recommendation = knowledge_service_unwired.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend X based on hypothesis Finding.",
        )
    return finding, recommendation


# ---------------------------------------------------------------------------
# Role-assignment helpers.
# ---------------------------------------------------------------------------


def _assign_decision_maker_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
    authorities: Iterable[str] = ("approve",),
) -> str:
    """Insert a Decision-Maker Role Assignment and return its identifier.

    A Decision Maker's effective authority over Decision creation is the
    ``approve`` authority type per design §"Authorization_Service"
    (ActionType enumeration — ``approve.decision`` maps to ``approve``).
    Tests vary the ``effective_start`` / ``effective_end`` / scope /
    authorities-set parameters to drive each Requirement-7.2 reason
    code.
    """
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


def _revoke(engine: Engine, role_assignment_id: str, when: datetime) -> None:
    """Stamp ``revoked_at`` on a Role Assignment.

    Uses a direct UPDATE because the Authorization_Service exposes
    ``revoked_at`` as a one-shot field stamped through the schema's
    role-assignment revocation endpoint (task 3.3); these tests do not
    exercise that endpoint, so a direct UPDATE keeps the setup
    straightforward.
    """
    from walking_slice.audit import format_iso8601_ms

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(when), "rid": role_assignment_id},
        )


# ---------------------------------------------------------------------------
# Row readers — used by negative-path tests to confirm nothing was
# persisted (Requirements 7.1, 7.5).
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_audit_rows(
    engine: Engine, *, outcome: Optional[str] = None
) -> list[dict]:
    sql = (
        "SELECT actor_party_id, action_type, outcome, target_id, "
        "target_revision_id, reason_code, correlation_id, recorded_at, "
        "evaluated_role_assignment_id, authorities_required, "
        "authorities_held "
        "FROM Audit_Records "
    )
    params: dict[str, object] = {}
    if outcome is not None:
        sql += "WHERE outcome = :outcome "
        params["outcome"] = outcome
    sql += "ORDER BY append_sequence"
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def _fetch_denial_records(engine: Engine) -> list[dict]:
    """Return only the dedicated Denial Records (Requirement 7.2).

    The dedicated Denial Record is the row written by
    :meth:`KnowledgeService._persist_decision_denial` in a separate
    transaction so it survives the caller's rollback. It is
    distinguished from the evaluation row (written by
    :meth:`AuthorizationService.evaluate`'s
    ``append_evaluation``) by the fact that the Denial Record carries
    NULL in the ``authorities_required`` / ``authorities_held``
    columns — those columns are populated only by the evaluation
    row, which itself is a separate (and orthogonal) audit concept
    per Requirement 12.5.
    """
    deny_rows = _fetch_audit_rows(engine, outcome="deny")
    return [row for row in deny_rows if row["authorities_required"] is None]


def _fetch_evaluation_rows(engine: Engine) -> list[dict]:
    """Return only the evaluation rows (Requirement 12.5).

    Distinguished from the Denial Record by ``authorities_required``
    being non-NULL — :meth:`AuthorizationService.evaluate` always
    populates that column with the JSON-encoded authority required
    by the action.
    """
    return [
        row
        for row in _fetch_audit_rows(engine)
        if row["authorities_required"] is not None
    ]


# ---------------------------------------------------------------------------
# Permit path.
# ---------------------------------------------------------------------------


def test_create_decision_permits_when_decision_maker_role_grants_approve(
    engine: Engine,
    authorization_service: AuthorizationService,
    knowledge_service_authorized: KnowledgeService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Happy path: a Party with ``approve`` authority for the applicable
    scope and an effective Role Assignment is permitted to record the
    Decision, and the evaluation audit row appears alongside the
    consequential audit row in the same transaction (AD-WS-5)."""
    _seed_required_parties(engine)
    _assign_decision_maker_role(authorization_service, engine)
    _, recommendation = _seed_recommendation(engine, knowledge_service_unwired)

    with engine.begin() as conn:
        result = knowledge_service_authorized.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Permit-path Decision.",
            deciding_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateDecisionResult)
    assert _CANONICAL_UUID7.match(result.decision_id)

    # The Decision row was created (Requirement 6.1).
    assert _count(engine, "Decisions") == 1

    # Both audit rows landed in the same transaction (Requirement 12.5
    # for the evaluation row, Requirement 6.4 for the consequential
    # row). Both carry the same correlation identifier so they can be
    # joined.
    permit_rows = _fetch_audit_rows(engine, outcome="permit")
    assert len(permit_rows) == 1
    assert permit_rows[0]["action_type"] == "approve.decision"
    assert permit_rows[0]["actor_party_id"] == _PARTY_ID
    assert permit_rows[0]["correlation_id"] == "corr-permit"
    assert permit_rows[0]["target_id"] == recommendation.recommendation_id
    assert permit_rows[0]["target_revision_id"] == (
        recommendation.recommendation_revision_id
    )

    consequential_rows = _fetch_audit_rows(engine, outcome="consequential")
    create_decision_rows = [
        row for row in consequential_rows if row["action_type"] == "create.decision"
    ]
    assert len(create_decision_rows) == 1
    assert create_decision_rows[0]["correlation_id"] == "corr-permit"

    # No denial rows on the permit path.
    deny_rows = _fetch_audit_rows(engine, outcome="deny")
    assert deny_rows == []


# ---------------------------------------------------------------------------
# Deny path — Requirements 7.1, 7.2, 7.5.
# ---------------------------------------------------------------------------


def test_create_decision_denies_when_party_has_no_role(
    engine: Engine,
    authorization_service: AuthorizationService,
    knowledge_service_authorized: KnowledgeService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Requirement 7.1: a caller without any Role Assignment is denied,
    :class:`DecisionAuthorizationError` is raised, no Decision row is
    persisted (Requirement 7.5), and exactly one Denial Record is
    appended in a separate transaction (Requirement 7.2)."""
    _seed_required_parties(engine)
    _, recommendation = _seed_recommendation(engine, knowledge_service_unwired)

    pre_decisions = _count(engine, "Decisions")
    pre_addresses = _count(engine, "Relationships")
    pre_manifests = _count(engine, "Provenance_Manifests")

    with pytest.raises(DecisionAuthorizationError) as exc_info:
        with engine.begin() as conn:
            knowledge_service_authorized.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="Should be denied.",
                deciding_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=engine,
                evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                correlation_id="corr-no-role",
            )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-no-role"

    # No Decision-side rows persisted (Requirement 7.5: Recommendation
    # state is byte-equivalent).
    assert _count(engine, "Decisions") == pre_decisions
    assert _count(engine, "Relationships") == pre_addresses
    assert _count(engine, "Provenance_Manifests") == pre_manifests

    # Exactly one Denial Record survived the rollback (Requirement 7.2).
    # The evaluation row (committed in the separate eval transaction)
    # also lives in ``Audit_Records`` with ``outcome='deny'``; the
    # helper below filters it out so we count only the dedicated
    # Denial Record (per Requirement 7.2 "exactly one immutable
    # Denial Record").
    deny_rows = _fetch_denial_records(engine)
    assert len(deny_rows) == 1
    denial = deny_rows[0]
    assert denial["actor_party_id"] == _PARTY_ID
    assert denial["action_type"] == "approve.decision"
    assert denial["target_id"] == recommendation.recommendation_id
    assert denial["target_revision_id"] == (
        recommendation.recommendation_revision_id
    )
    assert denial["reason_code"] == "no-role-assignment"
    assert denial["correlation_id"] == "corr-no-role"

    # The evaluation row (Requirement 12.5) was committed in its own
    # transaction (separate from the caller's, which rolled back),
    # so it survives too. The two rows together provide the
    # Requirement 12.5 + Requirement 7.2 trail; both carry the same
    # ``correlation_id``.
    eval_rows = _fetch_evaluation_rows(engine)
    deny_eval_rows = [row for row in eval_rows if row["outcome"] == "deny"]
    assert len(deny_eval_rows) == 1
    assert deny_eval_rows[0]["correlation_id"] == "corr-no-role"
    assert deny_eval_rows[0]["reason_code"] == "no-role-assignment"

    # No permit-outcome rows expected on this path.
    permit_rows = _fetch_audit_rows(engine, outcome="permit")
    assert permit_rows == []


# ---------------------------------------------------------------------------
# Each Requirement-7.2 reason code produces a deny with that reason.
# ---------------------------------------------------------------------------


def _trigger_denial(
    reason: str,
    *,
    engine: Engine,
    authorization_service: AuthorizationService,
    knowledge_service_authorized: KnowledgeService,
    knowledge_service_unwired: KnowledgeService,
    correlation_id: str,
    evaluation_at: datetime = datetime(2026, 6, 1, tzinfo=timezone.utc),
) -> DecisionAuthorizationError:
    """Drive ``create_decision`` to the named reason code.

    Returns the raised :class:`DecisionAuthorizationError` so the
    caller can assert on its fields. The helper seeds the Parties and
    the Recommendation once per call, then arranges the Role
    Assignment state (or absence) required by the reason code.
    """
    _seed_required_parties(engine)
    _, recommendation = _seed_recommendation(engine, knowledge_service_unwired)

    if reason == "no-role-assignment":
        # No Role Assignment seeded; the absence drives the denial.
        pass
    elif reason == "out-of-scope":
        _assign_decision_maker_role(
            authorization_service, engine, scope=_OTHER_SCOPE
        )
    elif reason == "not-yet-effective":
        _assign_decision_maker_role(
            authorization_service,
            engine,
            effective_start=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
    elif reason == "expired":
        _assign_decision_maker_role(
            authorization_service,
            engine,
            effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            effective_end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    elif reason == "revoked":
        rid = _assign_decision_maker_role(authorization_service, engine)
        _revoke(engine, rid, datetime(2026, 3, 1, tzinfo=timezone.utc))
    else:  # pragma: no cover - guard against typos
        raise AssertionError(f"unhandled reason code in helper: {reason!r}")

    with pytest.raises(DecisionAuthorizationError) as exc_info:
        with engine.begin() as conn:
            knowledge_service_authorized.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale=f"Should be denied with {reason}.",
                deciding_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=engine,
                evaluation_at=evaluation_at,
                correlation_id=correlation_id,
            )
    return exc_info.value


@pytest.mark.parametrize(
    "reason",
    [
        "not-yet-effective",
        "expired",
        "revoked",
        "out-of-scope",
        "no-role-assignment",
    ],
)
def test_create_decision_each_reason_code_produces_matching_denial(
    reason: str,
    engine: Engine,
    authorization_service: AuthorizationService,
    knowledge_service_authorized: KnowledgeService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Requirement 7.2: every reason code in the enumerated set produces
    a denial carrying that reason code on both the exception and the
    Denial Record."""
    correlation = f"corr-{reason}"
    exc = _trigger_denial(
        reason,
        engine=engine,
        authorization_service=authorization_service,
        knowledge_service_authorized=knowledge_service_authorized,
        knowledge_service_unwired=knowledge_service_unwired,
        correlation_id=correlation,
    )
    assert exc.reason_code == reason
    assert exc.correlation_id == correlation

    deny_rows = _fetch_denial_records(engine)
    assert len(deny_rows) == 1
    assert deny_rows[0]["reason_code"] == reason
    assert deny_rows[0]["correlation_id"] == correlation


# ---------------------------------------------------------------------------
# Denial response shape: Requirement 7.4 / AD-WS-9.
# ---------------------------------------------------------------------------


def test_decision_authorization_error_exposes_only_reason_and_correlation(
    engine: Engine,
    authorization_service: AuthorizationService,
    knowledge_service_authorized: KnowledgeService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Requirement 7.4: the :class:`DecisionAuthorizationError` exposes
    only ``reason_code`` and ``correlation_id``. No authorized-Party
    identities, Recommendation contents, role-assignment details, or
    target identifiers leak through any public attribute."""
    exc = _trigger_denial(
        "no-role-assignment",
        engine=engine,
        authorization_service=authorization_service,
        knowledge_service_authorized=knowledge_service_authorized,
        knowledge_service_unwired=knowledge_service_unwired,
        correlation_id="corr-shape",
    )

    # The exception declares exactly two domain-specific public
    # attributes — ``reason_code`` and ``correlation_id``. Any other
    # value the exception carries (the args tuple, the message) is
    # derived from these two and contains no additional information.
    public_attrs = {
        name
        for name in vars(exc)
        if not name.startswith("_")
    }
    assert public_attrs == {"reason_code", "correlation_id"}, (
        f"unexpected public attributes: {public_attrs}"
    )

    # Sanity: the two values are present and well-formed.
    assert exc.reason_code == "no-role-assignment"
    assert exc.correlation_id == "corr-shape"


def test_denial_exception_shape_indistinguishable_across_reasons(
    engine: Engine,
    authorization_service: AuthorizationService,
    knowledge_service_authorized: KnowledgeService,
    knowledge_service_unwired: KnowledgeService,
    sqlite_path,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> None:
    """AD-WS-9: deny responses for different internal causes produce
    identical exception shapes (differ only in ``reason_code`` and
    ``correlation_id``).

    Each reason code is exercised on an *isolated* engine so the
    Role-Assignments state from one branch does not bleed into the
    next.
    """
    from sqlalchemy import create_engine, event

    from walking_slice.persistence import create_schema

    def _make_engine(tag: str) -> Engine:
        path = sqlite_path.parent / f"{sqlite_path.stem}-{tag}.sqlite"
        url = f"sqlite:///{path.as_posix()}"
        eng = create_engine(url, future=True)

        @event.listens_for(eng, "connect")
        def _set_pragmas(dbapi_connection, _record):  # pragma: no cover
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

        create_schema(eng)
        return eng

    reasons = (
        "not-yet-effective",
        "expired",
        "revoked",
        "out-of-scope",
        "no-role-assignment",
    )

    exceptions: list[DecisionAuthorizationError] = []
    for reason in reasons:
        eng = _make_engine(reason)
        try:
            exc = _trigger_denial(
                reason,
                engine=eng,
                authorization_service=authorization_service,
                knowledge_service_authorized=knowledge_service_authorized,
                knowledge_service_unwired=knowledge_service_unwired,
                correlation_id=f"corr-iso-{reason}",
            )
        finally:
            eng.dispose()
        exceptions.append(exc)

    # Every exception declares the same public attribute set …
    public_attr_sets = [
        frozenset(name for name in vars(exc) if not name.startswith("_"))
        for exc in exceptions
    ]
    assert all(
        attrs == public_attr_sets[0] for attrs in public_attr_sets[1:]
    ), f"public-attribute drift across reason codes: {public_attr_sets}"
    assert public_attr_sets[0] == {"reason_code", "correlation_id"}

    # … and the *only* permitted variation between branches is the
    # reason code (mandated by AD-WS-9) and the correlation id
    # (per-call value).
    observed_reasons = [exc.reason_code for exc in exceptions]
    assert observed_reasons == list(reasons)
    correlation_ids = [exc.correlation_id for exc in exceptions]
    assert len(set(correlation_ids)) == len(correlation_ids)


# ---------------------------------------------------------------------------
# Retry path — Requirement 7.6.
# ---------------------------------------------------------------------------


class _FailingDenialAuditLog:
    """Audit log double that fails ``append_denial`` ``fail_count`` times.

    Delegates every other method to a real :class:`AuditLog` (so the
    evaluation row and the consequential row written inside the
    caller's transaction continue to function exactly as in
    production). Only ``append_denial`` is wrapped: the first
    ``fail_count`` calls raise :class:`AuditAppendError`; subsequent
    calls delegate to the inner :class:`AuditLog`.

    Records each call to ``append_denial`` in ``calls`` so tests can
    assert on the attempt sequence and the recorded times. The
    backoff sleeps observed by :func:`record_sleep` are recorded
    separately on a list passed by the caller.
    """

    def __init__(self, inner: AuditLog, *, fail_count: int) -> None:
        self._inner = inner
        self._remaining_failures = fail_count
        self.calls: list[dict[str, object]] = []

    # The KnowledgeService only invokes ``append_consequential``,
    # ``append_denial``, and (via AuthorizationService) ``append_evaluation``;
    # we delegate each to the inner AuditLog.
    def append_consequential(self, *args, **kwargs):
        return self._inner.append_consequential(*args, **kwargs)

    def append_evaluation(self, *args, **kwargs):
        return self._inner.append_evaluation(*args, **kwargs)

    def append_denial(self, *args, **kwargs) -> AuditRecord:
        # Capture the kwargs every call so tests can confirm each
        # attempt carried the same payload (reason code, correlation
        # id, target identifiers).
        self.calls.append(dict(kwargs))
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise AuditAppendError(
                f"simulated audit append failure "
                f"(remaining failures after this raise: "
                f"{self._remaining_failures})"
            )
        return self._inner.append_denial(*args, **kwargs)


def test_denial_audit_retries_succeed_on_third_attempt(
    engine: Engine,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Requirement 7.6: when the denial audit append fails twice and
    succeeds on the third attempt, the Denial Record is still recorded
    (one and only one row lands in ``Audit_Records``)."""
    _seed_required_parties(engine)
    _, recommendation = _seed_recommendation(engine, knowledge_service_unwired)

    # Build a KnowledgeService whose AuditLog wrapper fails the first
    # two append_denial calls. The third call delegates to the real
    # AuditLog and persists the Denial Record. We also inject a
    # recording sleep so the retry pauses are observable without
    # spending real wall-clock time.
    sleeps: list[float] = []
    failing_log = _FailingDenialAuditLog(audit_log, fail_count=2)
    service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=failing_log,  # type: ignore[arg-type] - quacks like AuditLog
        authorization_service=authorization_service,
        denial_audit_sleep=sleeps.append,
    )

    with pytest.raises(DecisionAuthorizationError) as exc_info:
        with engine.begin() as conn:
            service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="Decision attempted while audit retries fire.",
                deciding_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=engine,
                evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                correlation_id="corr-retry",
            )

    # The deny still surfaces as DecisionAuthorizationError because
    # the audit eventually succeeded.
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-retry"

    # Exactly three append_denial attempts were made — two failures
    # followed by one success — and every attempt carried the same
    # payload.
    assert len(failing_log.calls) == 3
    first_call = failing_log.calls[0]
    for call in failing_log.calls[1:]:
        assert call == first_call, "retry payload drifted across attempts"

    # Sleeps fired between attempts — first 0.01s, then 0.02s (the
    # 0.04s sleep would only run if the third attempt also failed).
    assert sleeps == [0.01, 0.02]

    # Exactly one Denial Record landed in Audit_Records.
    deny_rows = _fetch_denial_records(engine)
    assert len(deny_rows) == 1
    assert deny_rows[0]["reason_code"] == "no-role-assignment"
    assert deny_rows[0]["correlation_id"] == "corr-retry"


def test_denial_audit_failure_after_all_retries_raises_dedicated_error(
    engine: Engine,
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """Requirement 7.6: when every retry fails, the service raises
    :class:`DecisionAuditFailureError` in place of
    :class:`DecisionAuthorizationError` so the operator is told the
    denial-and-audit have silently diverged."""
    _seed_required_parties(engine)
    _, recommendation = _seed_recommendation(engine, knowledge_service_unwired)

    sleeps: list[float] = []
    failing_log = _FailingDenialAuditLog(audit_log, fail_count=100)
    service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=failing_log,  # type: ignore[arg-type]
        authorization_service=authorization_service,
        denial_audit_sleep=sleeps.append,
    )

    with pytest.raises(DecisionAuditFailureError) as exc_info:
        with engine.begin() as conn:
            service.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="Decision attempted while audit retries always fail.",
                deciding_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=engine,
                evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                correlation_id="corr-all-fail",
            )

    # The DecisionAuditFailureError carries the same reason code and
    # correlation identifier the evaluation produced so an
    # operator-facing surface can still render the AD-WS-9
    # indistinguishable shape if it chooses.
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-all-fail"
    assert exc_info.value.attempts == 4

    # Four attempts were made (initial + three retries) and three
    # backoff sleeps fired (0.01, 0.02, 0.04).
    assert len(failing_log.calls) == 4
    assert sleeps == [0.01, 0.02, 0.04]

    # No Denial Record persisted; this is the diverged state
    # Requirement 7.6 warns about, and the dedicated exception type is
    # the operator-visible signal. (The evaluation row from the
    # ``evaluate()`` call still landed in its own transaction — only
    # the dedicated Denial Record write failed every retry.)
    assert _fetch_denial_records(engine) == []


# ---------------------------------------------------------------------------
# Configuration error: authorization wired but engine omitted.
# ---------------------------------------------------------------------------


def test_create_decision_raises_value_error_when_engine_missing(
    knowledge_service_authorized: KnowledgeService,
) -> None:
    """Requirement 7.6: when ``authorization_service`` is wired, the
    caller MUST supply an ``engine`` so the Denial Record can be
    persisted in a separate transaction. The pre-flight check fires
    before any database read, so the test does not even need to seed
    Parties.
    """
    with pytest.raises(ValueError) as exc_info:
        knowledge_service_authorized.create_decision(
            connection=None,  # type: ignore[arg-type] - never read
            target_recommendation_id=_PLACEHOLDER_REC_ID,
            target_recommendation_revision_id=_PLACEHOLDER_REV_ID,
            outcome="Accept",
            rationale="Will not reach the database.",
            deciding_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=None,
        )
    assert "engine is required" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Back-compat sanity: when no authorization service is wired, the
# Decision flow does not require ``engine`` and still works exactly as
# task 8.1 specified. Pins that task 8.2's additions are non-breaking.
# ---------------------------------------------------------------------------


def test_create_decision_without_authorization_service_still_works_without_engine(
    engine: Engine,
    knowledge_service_unwired: KnowledgeService,
) -> None:
    """When :attr:`authorization_service` is ``None`` the engine
    parameter is ignored and the existing task-8.1 path is followed
    unchanged."""
    _seed_required_parties(engine)
    _, recommendation = _seed_recommendation(engine, knowledge_service_unwired)

    with engine.begin() as conn:
        result = knowledge_service_unwired.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Backwards-compatible no-authorization path.",
            deciding_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
        )

    assert isinstance(result, CreateDecisionResult)
    assert _count(engine, "Decisions") == 1
    # No evaluation row (authorization was not invoked).
    assert _fetch_audit_rows(engine, outcome="permit") == []
    assert _fetch_audit_rows(engine, outcome="deny") == []
    # And no dedicated Denial Records either.
    assert _fetch_denial_records(engine) == []
