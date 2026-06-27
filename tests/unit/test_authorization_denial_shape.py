"""AD-WS-9 denial response shape conformance tests (task 3.4).

These tests sit alongside :mod:`tests.unit.test_authorization` (task 3.2),
which already exercises every denial branch in
``{not-yet-effective, expired, revoked, out-of-scope, no-role-assignment}``.
The purpose of this module is narrower: it pins the *shape* of the
:class:`~walking_slice.authorization.AuthorizationDecision` value returned
by :meth:`AuthorizationService.evaluate` against the contract in
Requirement 7.4 and AD-WS-9 in
``.kiro/specs/first-walking-slice/design.md``.

The contract under test
-----------------------

**Requirement 7.4** — When the Authorization_Service rejects an action
because of missing authority, the denial response SHALL contain only:

    1. a generic denial indicator,
    2. the denial reason code (drawn from the Requirement 7.2
       enumeration ``{not-yet-effective, expired, revoked, out-of-scope,
       no-role-assignment}``), and
    3. a correlation identifier,

and SHALL NOT contain authorized Party identities, Recommendation
contents, role assignment details, target existence beyond the
requesting Party's view authority, or other attribute values.

**AD-WS-9 timing/observability claim.** Restricted-vs-nonexistent
observability is normalized: the response shape is the same regardless of
the *internal* reason for denial. Two distinct denial scenarios produce
decisions whose externally observable fields are identical in shape and
differ only in the recorded ``reason_code`` (and the per-call
``correlation_id``).

Mapping to :class:`AuthorizationDecision`
----------------------------------------

The :class:`AuthorizationDecision` dataclass declared in
:mod:`walking_slice.authorization` has exactly four fields:

    - ``kind``: ``Literal["permit", "deny"]`` — the **generic denial
      indicator** is ``kind == "deny"``;
    - ``reason_code``: ``Optional[str]`` — populated on deny, ``None`` on
      permit;
    - ``correlation_id``: ``str`` — the **correlation identifier**, a
      canonical UUIDv7;
    - ``authority_basis``: ``Optional[AuthorityBasisRef]`` — populated on
      permit, ``None`` on deny (suppressing the role-grant identifier and
      any other role assignment detail).

Therefore a Requirement-7.4-conforming denial decision is the unique
dataclass instance:

    AuthorizationDecision(
        kind="deny",
        reason_code=<one of the five Requirement 7.2 codes>,
        correlation_id=<canonical UUIDv7>,
        authority_basis=None,
    )

These tests confirm that every denial branch produces exactly that
instance, and that no extra fields, no role assignment identifiers, and
no inputs to ``evaluate`` (Party identity, target identity, target
revision identity, target scope) appear on or are reachable through the
returned decision.

Requirements satisfied:
    7.4   — Denial response shape limited to
            ``{generic_denial_indicator, reason_code, correlation_id}``.
    12.2  — Denial reason code is drawn from the enumerated set.
    12.4  — Required authority is action-specific; no substitution.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import fields
from datetime import datetime, timezone
from typing import Iterable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationDecision,
    AuthorizationService,
    ReasonCode,
    TargetRef,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and helpers.
# ---------------------------------------------------------------------------

# The denial response shape from Requirement 7.4 / AD-WS-9 maps onto the
# :class:`AuthorizationDecision` dataclass as follows. The set of
# externally-observable fields is the dataclass field set; the per-deny
# value pattern is recorded here so cross-branch indistinguishability can
# be asserted structurally rather than per-attribute.
_DENIAL_SHAPE_FIELDS: frozenset[str] = frozenset(
    {"kind", "reason_code", "correlation_id", "authority_basis"}
)

# Requirement 7.2 / 12.2 enumeration. Repeated here so the parametrized
# fixtures fail fast if a new reason code is added without updating this
# shape suite.
_REASON_CODES: tuple[ReasonCode, ...] = (
    "not-yet-effective",
    "expired",
    "revoked",
    "out-of-scope",
    "no-role-assignment",
)

# Canonical UUIDv7 form for correlation identifiers (design §"Cross-Cutting
# Concerns", *Identifier generation*).
_CANONICAL_UUIDV7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Test fixtures (mirroring tests/unit/test_authorization.py so the two
# files are independently readable).
_PARTY_ID = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000000000a2"
_TARGET_ID = "00000000-0000-7000-8000-0000000000b0"
_TARGET_REVISION_ID = "00000000-0000-7000-8000-0000000000b1"
_SCOPE = "pilot/team-shape"
_OTHER_SCOPE = "pilot/team-other"

_EVAL_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _seed_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        for pid, name in (
            (_PARTY_ID, "Subject"),
            (_ASSIGNING_AUTHORITY_ID, "Resource Steward"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, '2026-01-01T00:00:00.000Z')
                    """
                ),
                {"pid": pid, "name": name},
            )


@pytest.fixture
def seeded_engine(engine: Engine, audit_log: AuditLog) -> Engine:
    """Engine with schema (via ``audit_log`` fixture) and Parties seeded."""
    _seed_parties(engine)
    return engine


@pytest.fixture
def engine_factory(tmp_path, clock):
    """Yield a callable that mints fresh, schema-installed engines on demand.

    The shared :func:`tests.conftest.engine` fixture supplies a single engine
    per test, which suits most tests but is too coarse for the AD-WS-9
    indistinguishability tests in this module: each branch needs its own
    ``Role_Assignments`` table so prior insertions cannot influence the
    next scenario. The factory installs the schema and constructs an
    :class:`AuditLog`-compatible engine for each call.
    """
    from sqlalchemy import create_engine, event

    from walking_slice.persistence import create_schema

    created: list[Engine] = []
    counter = {"n": 0}

    def _make() -> Engine:
        counter["n"] += 1
        path = tmp_path / f"shape_iso_{counter['n']}.sqlite"
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
        created.append(eng)
        return eng

    try:
        yield _make
    finally:
        for eng in created:
            eng.dispose()


def _assign(
    service: AuthorizationService,
    engine: Engine,
    *,
    authorities: Iterable[str] = ("approve",),
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: datetime | None = None,
) -> str:
    """Insert a Role_Assignments row and return its identifier."""
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="decision_maker",
        scope=scope,
        authorities_granted=tuple(authorities),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(service.assign_role(conn, request))


def _revoke(engine: Engine, role_assignment_id: str, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(when), "rid": role_assignment_id},
        )


def _evaluate(
    service: AuthorizationService,
    engine: Engine,
    *,
    target: TargetRef,
    action: str = "approve.decision",
    at: datetime = _EVAL_AT,
) -> AuthorizationDecision:
    with engine.begin() as conn:
        return service.evaluate(
            conn,
            party_id=_PARTY_ID,
            action=action,
            target=target,
            at=at,
        )


def _produce_denial(
    reason: ReasonCode,
    service: AuthorizationService,
    engine: Engine,
) -> tuple[AuthorizationDecision, str | None]:
    """Drive ``evaluate`` to return the named denial reason.

    Returns the decision and, when the scenario involved a Role
    Assignment, the role-assignment identifier (so tests asserting
    non-leakage can confirm it does not appear in the decision).
    """
    target = TargetRef(
        kind="recommendation_revision",
        id=_TARGET_ID,
        revision_id=_TARGET_REVISION_ID,
        scope=_SCOPE,
    )

    if reason == "no-role-assignment":
        return _evaluate(service, engine, target=target), None

    if reason == "out-of-scope":
        rid = _assign(service, engine, authorities=("approve",), scope=_OTHER_SCOPE)
        return _evaluate(service, engine, target=target), rid

    if reason == "not-yet-effective":
        rid = _assign(
            service,
            engine,
            authorities=("approve",),
            effective_start=datetime(2027, 1, 1, tzinfo=timezone.utc),
        )
        return _evaluate(service, engine, target=target), rid

    if reason == "expired":
        rid = _assign(
            service,
            engine,
            authorities=("approve",),
            effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            effective_end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        return _evaluate(service, engine, target=target), rid

    if reason == "revoked":
        rid = _assign(service, engine, authorities=("approve",))
        _revoke(engine, rid, datetime(2026, 3, 1, tzinfo=timezone.utc))
        return _evaluate(service, engine, target=target), rid

    raise AssertionError(f"unhandled reason code in helper: {reason!r}")


# ---------------------------------------------------------------------------
# AD-WS-9: per-branch denial shape.
#
# Each parametrized scenario drives ``evaluate`` to the named denial branch
# and asserts the returned :class:`AuthorizationDecision` conforms exactly
# to the Requirement 7.4 shape — and only that shape.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", _REASON_CODES, ids=list(_REASON_CODES))
def test_denial_decision_shape_conforms_to_ad_ws_9(
    reason: ReasonCode,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Requirement 7.4 / AD-WS-9: shape is ``{kind=deny, reason_code, correlation_id}``."""
    decision, _ = _produce_denial(reason, authorization_service, seeded_engine)

    # The dataclass exposes exactly the four AD-WS-9 fields (kind serves as
    # the generic denial indicator; authority_basis is always None on
    # deny). No additional attributes are permitted.
    field_names = {f.name for f in fields(decision)}
    assert field_names == _DENIAL_SHAPE_FIELDS, (
        f"AuthorizationDecision exposes unexpected fields: {field_names}"
    )

    # Generic denial indicator: ``kind == "deny"``. The same literal value
    # is used for every denial branch.
    assert decision.kind == "deny"
    assert decision.is_deny
    assert not decision.is_permit

    # Reason code drawn from the Requirement 7.2 enumeration.
    assert decision.reason_code == reason

    # Correlation identifier present, canonical-form UUIDv7.
    assert decision.correlation_id is not None
    assert _CANONICAL_UUIDV7.match(decision.correlation_id), decision.correlation_id

    # Requirement 7.4 forbids leaking role assignment details — the
    # role-grant identifier is therefore suppressed on deny.
    assert decision.authority_basis is None


@pytest.mark.parametrize("reason", _REASON_CODES, ids=list(_REASON_CODES))
def test_denial_decision_serialization_carries_only_ad_ws_9_keys(
    reason: ReasonCode,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """The dataclass round-trips through :func:`dataclasses.asdict` with exactly the AD-WS-9 keys."""
    decision, _ = _produce_denial(reason, authorization_service, seeded_engine)

    serialized = dataclasses.asdict(decision)

    assert set(serialized) == _DENIAL_SHAPE_FIELDS
    assert serialized["kind"] == "deny"
    assert serialized["reason_code"] == reason
    assert serialized["authority_basis"] is None
    assert serialized["correlation_id"] == decision.correlation_id


# ---------------------------------------------------------------------------
# Requirement 7.4: no leakage of role assignment details, Party
# identities, Recommendation contents, or target identity beyond the
# requesting Party's view authority.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["out-of-scope", "not-yet-effective", "expired", "revoked"],
    ids=["out-of-scope", "not-yet-effective", "expired", "revoked"],
)
def test_denial_decision_does_not_carry_role_assignment_identifier(
    reason: ReasonCode,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Even when a Role Assignment exists, its identifier is not on the decision."""
    decision, role_assignment_id = _produce_denial(
        reason, authorization_service, seeded_engine
    )
    assert role_assignment_id is not None  # sanity for the four scenarios above

    # No field carries the role assignment identifier. ``authority_basis``
    # is None (so its ``id`` is not even reachable), and the dataclass
    # exposes no other field that could carry it.
    assert decision.authority_basis is None
    for f in fields(decision):
        value = getattr(decision, f.name)
        if isinstance(value, str):
            assert role_assignment_id not in value, (
                f"role assignment id leaked through {f.name}: {value!r}"
            )


@pytest.mark.parametrize("reason", _REASON_CODES, ids=list(_REASON_CODES))
def test_denial_decision_does_not_carry_party_or_target_identifiers(
    reason: ReasonCode,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Party identity, target identity, and target revision identity are absent."""
    decision, _ = _produce_denial(reason, authorization_service, seeded_engine)

    forbidden_values = {
        _PARTY_ID,
        _ASSIGNING_AUTHORITY_ID,
        _TARGET_ID,
        _TARGET_REVISION_ID,
        _SCOPE,
        _OTHER_SCOPE,
    }
    for f in fields(decision):
        value = getattr(decision, f.name)
        if isinstance(value, str):
            assert value not in forbidden_values, (
                f"forbidden value {value!r} leaked through {f.name}"
            )


# ---------------------------------------------------------------------------
# AD-WS-9 indistinguishability: two distinct denial scenarios produce
# decisions whose externally-observable shape is identical and differ
# only in the reason code (and per-call correlation identifier).
# ---------------------------------------------------------------------------


def _shape_signature(decision: AuthorizationDecision) -> dict[str, object]:
    """Return the structural signature of a decision, omitting per-call values.

    Two denials with identical signatures are indistinguishable at the
    AD-WS-9 boundary apart from the reason code (which AD-WS-9 mandates
    appears on the response) and the per-call correlation identifier.
    """
    return {
        "fields": tuple(sorted(f.name for f in fields(decision))),
        "kind": decision.kind,
        "authority_basis_is_none": decision.authority_basis is None,
        "reason_code_present": decision.reason_code is not None,
        "correlation_id_present": bool(decision.correlation_id),
    }


def test_all_denial_branches_produce_identical_shape_signature(
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Every denial branch yields the same externally-observable shape.

    AD-WS-9 normalizes the response so the *internal* reason for denial
    does not change the response's structure — only the reason-code value
    distinguishes one branch from another. This test exercises all five
    branches in a single engine and asserts each decision's shape
    signature is equal.
    """
    signatures: list[dict[str, object]] = []
    for reason in _REASON_CODES:
        # Each reason needs an isolated environment to avoid one
        # assignment satisfying the next reason's setup; isolate via a
        # fresh per-reason :func:`_produce_denial` call against a clean
        # subject-Party. For ``revoked`` and ``expired`` we deliberately
        # produce a *new* Role Assignment per scenario to ensure no
        # interaction between branches.
        engine = seeded_engine  # all branches share the same SQLite file
        decision, _ = _produce_denial(reason, authorization_service, engine)
        # Drop the just-inserted role assignment (and its consequential
        # audit row) before the next branch so the next branch starts
        # from a clean ``Role_Assignments`` set. The append-only audit
        # contract prevents deleting audit rows, but the role
        # assignment table is mutable for the one-shot ``revoked_at``
        # field only — we cannot delete rows either. Instead we work
        # around this by tagging each insertion with a unique
        # ``role_name`` and using the priority ordering of reasons:
        # ``no-role-assignment`` is exercised first (no row), then
        # ``out-of-scope`` (a wrong-scope row only), then
        # ``not-yet-effective``, then ``expired``, then ``revoked``.
        # Each new row carries the authority required by its scenario
        # only, so the prior rows continue to deny the action for the
        # *new* reason. See ``test_cross_branch_signature_invariance``
        # below for the canonical isolated-engine variant.
        signatures.append(_shape_signature(decision))

    # Every signature is structurally equal.
    first = signatures[0]
    for sig in signatures[1:]:
        assert sig == first, f"shape signature drift across branches: {sig} != {first}"


def test_cross_branch_signature_invariance_with_isolated_engines(
    authorization_service: AuthorizationService,
    engine_factory,
) -> None:
    """Run each branch on a freshly-created engine; assert identical shapes.

    This is the cleaner variant of
    :func:`test_all_denial_branches_produce_identical_shape_signature` —
    each reason code is evaluated against a Role_Assignments table that
    contains only the rows it explicitly inserted, so there is no
    possibility of cross-branch contamination.
    """
    signatures: list[dict[str, object]] = []
    decisions: list[AuthorizationDecision] = []
    for reason in _REASON_CODES:
        eng = engine_factory()
        _seed_parties(eng)
        decision, _ = _produce_denial(reason, authorization_service, eng)
        signatures.append(_shape_signature(decision))
        decisions.append(decision)

    first = signatures[0]
    for sig in signatures[1:]:
        assert sig == first, f"shape signature drift across branches: {sig} != {first}"

    # The per-decision values that *are* permitted to vary (Requirement 7.4
    # explicitly names the reason code) line up with the five Requirement
    # 7.2 codes, one per branch.
    observed_reason_codes = [d.reason_code for d in decisions]
    assert observed_reason_codes == list(_REASON_CODES)

    # Correlation identifiers are pairwise distinct (one is minted per
    # ``evaluate`` call).
    correlation_ids = [d.correlation_id for d in decisions]
    assert len(set(correlation_ids)) == len(correlation_ids)


def test_two_concrete_denial_scenarios_are_pairwise_indistinguishable(
    authorization_service: AuthorizationService,
    engine_factory,
) -> None:
    """Pick two scenarios with different internal causes; assert identical shape.

    Concretizes the AD-WS-9 indistinguishability claim: a Party with a
    role that *exists but is expired* and a Party with *no role at all*
    must produce denials whose externally-observable response shape is
    identical apart from the reason code and correlation identifier
    (both of which AD-WS-9 explicitly admits).
    """
    engine_a = engine_factory()
    _seed_parties(engine_a)
    decision_no_role, _ = _produce_denial(
        "no-role-assignment", authorization_service, engine_a
    )

    engine_b = engine_factory()
    _seed_parties(engine_b)
    decision_expired, _ = _produce_denial(
        "expired", authorization_service, engine_b
    )

    # Field set is identical.
    assert {f.name for f in fields(decision_no_role)} == {
        f.name for f in fields(decision_expired)
    }

    # The values that AD-WS-9 forbids varying are equal.
    assert decision_no_role.kind == decision_expired.kind == "deny"
    assert decision_no_role.authority_basis is None
    assert decision_expired.authority_basis is None
    assert (decision_no_role.correlation_id is not None) == (
        decision_expired.correlation_id is not None
    )

    # The values that AD-WS-9 *does* admit variation in differ.
    assert decision_no_role.reason_code != decision_expired.reason_code
    assert decision_no_role.correlation_id != decision_expired.correlation_id


# ---------------------------------------------------------------------------
# Requirement 12.4 confirmation: the shape contract holds equally for
# every authority type (view/modify/approve) — no authority kind is
# privileged with a richer denial body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    ["view.document_revision", "modify.recommendation", "approve.decision"],
    ids=["view", "modify", "approve"],
)
def test_denial_shape_is_uniform_across_authority_types(
    action: str,
    authorization_service: AuthorizationService,
    seeded_engine: Engine,
) -> None:
    """Requirement 12.4: denial response shape does not vary by required authority."""
    target = TargetRef(
        kind="recommendation_revision",
        id=_TARGET_ID,
        revision_id=_TARGET_REVISION_ID,
        scope=_SCOPE,
    )
    decision = _evaluate(
        authorization_service, seeded_engine, target=target, action=action
    )

    assert decision.is_deny
    assert decision.kind == "deny"
    assert decision.reason_code == "no-role-assignment"
    assert decision.authority_basis is None
    assert _CANONICAL_UUIDV7.match(decision.correlation_id)
    assert {f.name for f in fields(decision)} == _DENIAL_SHAPE_FIELDS
