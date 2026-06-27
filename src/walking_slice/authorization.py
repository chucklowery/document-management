"""Authorization_Service — role assignment and authority evaluation.

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Authorization_Service", AD-WS-9 (denial response shape), and AD-WS-10
(authority-basis enumeration).

This module exposes the two public methods required by task 3.2:

- :meth:`AuthorizationService.assign_role` — records a contextual role
  assignment (Requirement 12.1) and appends a consequential
  ``Audit_Records`` row inside the caller's transaction (AD-WS-5,
  Requirement 13.1).
- :meth:`AuthorizationService.evaluate` — evaluates whether a Party holds
  the required authority for an attempted action against a target at a
  given time, and appends an evaluation ``Audit_Records`` row inside the
  caller's transaction (Requirement 12.5).

The evaluator distinguishes the authority types — ``view``, ``modify``,
``approve`` (Slice 1, Requirement 12.3 / 12.4) and ``review`` (additive
per Slice 2 AD-WS-15 / Requirement 11.1) — and never substitutes one for
another. The required authority for an action is derived from the
action's prefix or its per-action override per the mapping documented in
:func:`_required_authority`.

Denial reason codes follow the Requirement 7.2 enumeration:
``{not-yet-effective, expired, revoked, out-of-scope, no-role-assignment}``.
When more than one reason applies to a Party's role assignments, the
service returns the most informative one according to the priority order
``revoked > expired > not-yet-effective > out-of-scope > no-role-assignment``
(documented per the task description for 3.2).

Requirements satisfied (per task 3.2):
    7.3   — Role assignments out of effective period, revoked, or out of
            scope are treated as not in effect.
    12.1  — Role assignments record Party, role, scope, granted
            authorities, effective period, and assigning authority.
    12.2  — Denials report a reason code drawn from
            ``{not-yet-effective, expired, revoked, out-of-scope}``.
    12.3  — The three authority types (view, modify, approve) are
            distinct.
    12.4  — Evaluation chooses the required authority by action and never
            substitutes one authority type for another.
    12.5  — Every evaluation appends an ``Audit_Records`` row with actor,
            attempted action, target, evaluated role assignment,
            authorities required, authorities held, outcome, reason code
            (for deny), and recorded time.
    12.6  — Role assignments missing Party Identity, role, scope, granted
            authorities, or effective-start time are rejected.

Scope handling (interim for the slice):
    Role scope ``"*"`` covers every target scope. Otherwise the role's
    scope must equal the target scope for coverage. This is the simple
    deterministic comparison called out by the task description; it lives
    inside :meth:`AuthorizationService._scope_covers` so it can be widened
    (e.g. to prefix-based hierarchies) without changing the public surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal, NewType, Optional, Sequence
from uuid import UUID

import uuid_utils
from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef


__all__ = [
    "AssignRoleRequest",
    "AuthorizationDecision",
    "AuthorizationService",
    "InvalidRoleAssignmentError",
    "ReasonCode",
    "RoleAssignmentId",
    "TargetRef",
]


# ---------------------------------------------------------------------------
# Public types.
# ---------------------------------------------------------------------------


RoleAssignmentId = NewType("RoleAssignmentId", str)


# Authority enumeration — the values that may appear in
# ``Role_Assignments.authorities_granted``. Listed here so the assignment
# validation in :meth:`AuthorizationService.assign_role` and the
# authority-derivation in :func:`_required_authority` share one source of
# truth.
#
# The ``"review"`` value is the Slice 2 additive extension (AD-WS-15,
# second-walking-slice Requirement 11.1 — Distinct Plan Reviewer and Plan
# Approver Authority Types).
#
# The ``"assign"``, ``"contribute"``, ``"accept_milestone"``, and
# ``"complete"`` values are the Slice 3 additive extension (AD-WS-24,
# third-walking-slice Requirement 32 — Distinct Assignment, Contributor,
# Milestone Acceptance, and Completion Authority Types). They extend the
# cumulative enumeration from four to eight values.
#
# The ``"define_measurement"``, ``"record_measurement"``,
# ``"assess_outcome"``, and ``"issue_outcome_review"`` values are the
# Slice 4 additive extension (AD-WS-33, fourth-walking-slice Requirement 52
# — Distinct define_measurement, record_measurement, assess_outcome, and
# issue_outcome_review Authority Types; closes Gap G-17). They extend the
# cumulative enumeration from eight to twelve values.
#
# All extensions are additive: no existing value is removed, renamed, or
# re-mapped, preserving Slice 1 Requirement 12.4 non-substitution
# semantics, Slice 2 Requirement 19.2 (additive-only extension of Slice 1
# enumerations), Slice 3 Requirement 40.2 (additive-only extension of
# Slice 1 + Slice 2 enumerations), and Slice 4 Requirement 60.2
# (additive-only extension of Slice 1 + Slice 2 + Slice 3 enumerations).
_VALID_AUTHORITIES: Final[frozenset[str]] = frozenset(
    {
        "view",
        "modify",
        "review",
        "approve",
        "assign",
        "contribute",
        "accept_milestone",
        "complete",
        "define_measurement",
        "record_measurement",
        "assess_outcome",
        "issue_outcome_review",
    }
)


# Denial reason codes drawn from Requirement 7.2 and design §"Authorization_Service".
# Listed in priority order (most → least specific) per the task description.
ReasonCode = Literal[
    "revoked",
    "expired",
    "not-yet-effective",
    "out-of-scope",
    "no-role-assignment",
]

_REASON_PRIORITY: Final[tuple[str, ...]] = (
    "revoked",
    "expired",
    "not-yet-effective",
    "out-of-scope",
    "no-role-assignment",
)


# Action-prefix → required-authority mapping. AD-WS notes the three authority
# types are not interchangeable (Requirement 12.3, 12.4); the prefixes here
# follow the ActionType enumeration in design §"Authorization_Service":
#
#   view.<resource_kind>      → view
#   modify.<resource_kind>    → modify
#   create.<resource_kind>    → modify   (creation modifies the world)
#   approve.<resource_kind>   → approve
#
# Unknown prefixes raise :class:`ValueError` so that a typo in an action
# name surfaces at call time rather than silently defaulting to any
# authority type.
_ACTION_PREFIX_TO_AUTHORITY: Final[dict[str, str]] = {
    "view": "view",
    "modify": "modify",
    "create": "modify",
    "approve": "approve",
}


# Per-action override mapping. AD-WS-15 (second-walking-slice design) extends
# the Slice 1 ``_required_authority`` derivation with the eight Slice 2
# planning action types. Most follow the prefix rule (``create.*`` → modify)
# and are listed here explicitly for traceability with Requirement 11.1
# through 11.7; two require a non-default authority and cannot be derived
# from the prefix alone:
#
#   - ``create.plan_review`` requires ``review`` authority (Requirement 11.4)
#   - ``create.plan_approval`` requires ``approve`` authority (Requirement 11.5)
#
# AD-WS-24 (third-walking-slice design) extends the override table further
# with the seven Slice 3 execution and deliverable action types. None of
# these follow the Slice 1 prefix rule because ``create.*`` would map to
# ``modify`` by default; Slice 3 Requirement 32 demands distinct authority
# types (``assign``, ``contribute``, ``accept_milestone``, ``complete``)
# that are not substitutable for any prior authority and are not
# substitutable for each other:
#
#   - ``create.work_assignment``       → ``assign``           (Requirement 32.6)
#   - ``create.work_event``            → ``contribute``       (Requirement 32.7)
#   - ``create.time_entry``            → ``contribute``       (Requirement 32.7)
#   - ``create.produced_deliverable``  → ``contribute``       (Requirement 32.7)
#   - ``create.deliverable_production``→ ``contribute``       (Requirement 32.7)
#   - ``create.milestone_acceptance``  → ``accept_milestone`` (Requirement 32.8)
#   - ``create.completion``            → ``complete``         (Requirement 32.9)
#
# The override table is consulted first by :func:`_required_authority`; if
# the action is not present, the prefix-based fallback applies. This keeps
# the Slice 1 derivation behavior byte-equivalent for every existing action
# (Requirement 19.1, Requirement 40.2 — no Slice 1 or Slice 2 behavior
# change) while satisfying the non-substitution rule that the eight
# authority types are pairwise distinct (Slice 2 Requirement 11.6, Slice 3
# Requirement 32.1, 32.10, 32.11).
# AD-WS-33 (fourth-walking-slice design) extends the override table with
# the five Slice 4 outcome-measurement action types. As with Slice 3,
# none follow the Slice 1 prefix rule because ``create.*`` would map to
# ``modify`` by default; Slice 4 Requirement 52 demands distinct authority
# types (``define_measurement``, ``record_measurement``, ``assess_outcome``,
# ``issue_outcome_review``) that are not substitutable for any prior
# authority and are not substitutable for each other:
#
#   - ``create.measurement_definition``        → ``define_measurement``   (Requirement 52.6)
#   - ``create.measurement_record``            → ``record_measurement``   (Requirement 52.7)
#   - ``create.observed_outcome``              → ``assess_outcome``       (Requirement 52.8)
#   - ``create.success_condition_assessment``  → ``assess_outcome``       (Requirement 52.8)
#   - ``create.outcome_review``                → ``issue_outcome_review`` (Requirement 52.9)
#
# The override table is consulted first by :func:`_required_authority`; if
# the action is not present, the prefix-based fallback applies. This keeps
# the Slice 1 derivation behavior byte-equivalent for every existing action
# (Requirement 19.1, Requirement 40.2, Requirement 60.2 — no Slice 1,
# Slice 2, or Slice 3 behavior change) while satisfying the non-substitution
# rule that the twelve authority types are pairwise distinct (Slice 2
# Requirement 11.6, Slice 3 Requirement 32.1, 32.10, 32.11, Slice 4
# Requirement 52.10).
_ACTION_TO_AUTHORITY: Final[dict[str, str]] = {
    "create.objective": "modify",
    "create.intended_outcome": "modify",
    "create.project": "modify",
    "create.deliverable_expectation": "modify",
    "create.activity_plan": "modify",
    "create.plan_revision": "modify",
    "create.plan_review": "review",
    "create.plan_approval": "approve",
    "create.work_assignment": "assign",
    "create.work_event": "contribute",
    "create.time_entry": "contribute",
    "create.produced_deliverable": "contribute",
    "create.deliverable_production": "contribute",
    "create.milestone_acceptance": "accept_milestone",
    "create.completion": "complete",
    "create.measurement_definition": "define_measurement",
    "create.measurement_record": "record_measurement",
    "create.observed_outcome": "assess_outcome",
    "create.success_condition_assessment": "assess_outcome",
    "create.outcome_review": "issue_outcome_review",
}


@dataclass(frozen=True)
class TargetRef:
    """Reference to the target of an authorization evaluation.

    ``scope`` is the scope identifier that role assignments are compared
    against (slice uses opaque scope identifiers per design AD-WS-9). When
    the action is scope-independent (e.g. system-wide diagnostic reads) the
    caller may pass ``scope=None``; such evaluations always result in
    out-of-scope denial unless a role assignment carries the wildcard scope
    ``"*"``.

    ``kind`` and ``id``/``revision_id`` are recorded on the audit row so
    operators can reconstruct what was being acted upon. They are not used
    for the access decision itself; only ``scope`` participates.
    """

    kind: str
    id: Optional[str] = None
    revision_id: Optional[str] = None
    scope: Optional[str] = None


@dataclass(frozen=True)
class AssignRoleRequest:
    """Contract for :meth:`AuthorizationService.assign_role`.

    Mirrors design §"Authorization_Service". All five Requirement 12.1
    attributes are required (``party_id``, ``role_name``, ``scope``,
    ``authorities_granted``, ``effective_start``); ``effective_end`` is
    optional (open-ended assignment per the Role_Assignments schema) and
    ``assigning_authority_id`` records the Party identity that recorded
    the assignment, which is also the actor on the consequential audit row.

    ``authorities_granted`` is a sequence of values drawn from
    :data:`_VALID_AUTHORITIES`; duplicates are accepted but
    normalized to a sorted JSON array on persistence so two assignments
    granting the same authorities are byte-equivalent in the
    ``Role_Assignments`` table.
    """

    party_id: str
    role_name: str
    scope: str
    authorities_granted: Sequence[str]
    effective_start: datetime
    assigning_authority_id: str
    effective_end: Optional[datetime] = None


@dataclass(frozen=True)
class AuthorizationDecision:
    """Result of :meth:`AuthorizationService.evaluate`.

    Following the design surface, an :class:`AuthorizationDecision` is
    either ``permit(authority_basis)`` or ``deny(reason_code,
    correlation_id)``. The two constructors are exposed as classmethods
    so call sites read clearly:

        >>> AuthorizationDecision.permit(basis, correlation_id="...")
        >>> AuthorizationDecision.deny("expired", correlation_id="...")

    ``correlation_id`` is always present so denial responses carry the
    same correlation identifier appearing on the related audit row —
    Requirement 7.4 calls this out for the denied-Decision response, and
    Requirement 12.5 calls it out for the evaluation audit row.
    """

    kind: Literal["permit", "deny"]
    correlation_id: str
    authority_basis: Optional[AuthorityBasisRef] = None
    reason_code: Optional[str] = None

    @classmethod
    def permit(
        cls,
        authority_basis: AuthorityBasisRef,
        *,
        correlation_id: str,
    ) -> "AuthorizationDecision":
        return cls(
            kind="permit",
            correlation_id=correlation_id,
            authority_basis=authority_basis,
        )

    @classmethod
    def deny(
        cls,
        reason_code: ReasonCode,
        *,
        correlation_id: str,
    ) -> "AuthorizationDecision":
        return cls(
            kind="deny",
            correlation_id=correlation_id,
            reason_code=reason_code,
        )

    @property
    def is_permit(self) -> bool:
        return self.kind == "permit"

    @property
    def is_deny(self) -> bool:
        return self.kind == "deny"


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class InvalidRoleAssignmentError(ValueError):
    """Raised by :meth:`AuthorizationService.assign_role` per Requirement 12.6.

    Carries a ``missing`` and/or ``invalid`` field listing so callers (and
    the HTTP layer added by task 3.3) can map the exception to a structured
    400-shaped error response.
    """

    def __init__(
        self,
        message: str,
        *,
        missing: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.missing = tuple(missing)
        self.invalid = tuple(invalid)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _required_authority(action: str) -> str:
    """Map an action string to the authority type required to permit it.

    The action string is expected to be ``"<prefix>.<resource_kind>"`` per
    design §"Authorization_Service" ActionType enumeration. Lookup is
    two-stage: the per-action override table :data:`_ACTION_TO_AUTHORITY`
    is consulted first (covering the eight second-walking-slice planning
    actions per AD-WS-15); if the action is not present, the prefix
    fallback :data:`_ACTION_PREFIX_TO_AUTHORITY` is used:

    - ``view.*``    → ``"view"``
    - ``modify.*``  → ``"modify"``
    - ``create.*``  → ``"modify"``  (creation is a write)
    - ``approve.*`` → ``"approve"``

    The override table is required for ``create.plan_review`` and
    ``create.plan_approval`` which carry non-default authorities
    (``review`` and ``approve`` respectively) and cannot be inferred from
    the ``create`` prefix alone; Requirement 11.6 forbids substitution
    between any of the four authority types.

    Raises:
        ValueError: If ``action`` is empty, missing the ``.`` separator,
            or carries an unknown prefix and is not in the override table.
    """
    if not isinstance(action, str) or not action:
        raise ValueError(f"action must be a non-empty string; got {action!r}")
    if "." not in action:
        raise ValueError(
            f"action must be of the form '<prefix>.<resource_kind>'; got {action!r}"
        )
    override = _ACTION_TO_AUTHORITY.get(action)
    if override is not None:
        return override
    prefix = action.split(".", 1)[0]
    try:
        return _ACTION_PREFIX_TO_AUTHORITY[prefix]
    except KeyError as exc:
        raise ValueError(
            f"action prefix {prefix!r} is not one of "
            f"{sorted(_ACTION_PREFIX_TO_AUTHORITY)!r}; received {action!r}."
        ) from exc


def _new_correlation_id() -> str:
    """Generate a correlation identifier for an evaluation.

    Correlation identifiers are not managed by the durable identity
    service (they do not name a domain Resource); they are used purely to
    join an evaluation audit row to any consequential write that consumed
    it (Requirement 12.5, AD-WS-5). A canonical UUIDv7 string is used so
    correlation identifiers sort temporally and share the lowercase-form
    discipline of every other slice identifier.
    """
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass
class AuthorizationService:
    """Records role assignments and evaluates authority for the slice.

    The service is connection-scoped at call time: every public method
    accepts the caller's :class:`~sqlalchemy.engine.Connection` and writes
    inside the caller's transaction (AD-WS-5). Instances therefore hold
    only the cross-request collaborators — the :class:`Clock`, the
    :class:`AuditLog`, and the :class:`IdentityService` — and can be
    shared across requests and threads.

    Args:
        clock: Source of recorded timestamps for role assignment creation.
            The clock is consulted only when the caller does not supply an
            explicit ``recorded_time``; ``evaluate`` requires the caller to
            pass the effective-time ``at`` so authority is evaluated as of
            that instant (design §"Cross-Cutting Concerns", *Authorization*).
        audit_log: Receives one audit row per consequential write
            (``assign_role`` → ``'consequential'``) and one per evaluation
            (``evaluate`` → ``'permit'`` or ``'deny'``) per Requirement
            12.5.
        identity_service: Generates the role-assignment identifier so that
            Requirements 1.1 / 1.2 (UUIDv7 canonical form, distinct
            identities) apply uniformly across the slice.
    """

    clock: Clock
    audit_log: AuditLog
    identity_service: IdentityService = field(default_factory=IdentityService)

    # -- public surface ----------------------------------------------------

    def assign_role(
        self,
        connection: Connection,
        request: AssignRoleRequest,
        *,
        correlation_id: Optional[str] = None,
    ) -> RoleAssignmentId:
        """Record a contextual role assignment.

        Validates the request per Requirement 12.6, generates a fresh
        role-assignment identifier, inserts the ``Role_Assignments`` row,
        and appends a ``'consequential'`` audit row — all inside the
        caller's transaction.

        Args:
            connection: The SQLAlchemy connection bound to the caller's
                transaction. The insert and the audit append participate
                in this transaction.
            request: The validated assignment payload.
            correlation_id: Optional correlation identifier shared by every
                row written in this transaction. A UUIDv7 is generated when
                omitted.

        Returns:
            The new :data:`RoleAssignmentId`.

        Raises:
            InvalidRoleAssignmentError: If a required field is missing or
                any authority is not one of :data:`_VALID_AUTHORITIES`.
            walking_slice.audit.AuditAppendError: If the consequential
                audit append fails. The caller MUST allow the surrounding
                transaction to roll back per Requirements 2.7 and 13.6.
        """
        self._validate_assign_request(request)

        correlation = correlation_id or _new_correlation_id()
        role_assignment_id = str(self.identity_service.new_resource_id())
        recorded_at = self.clock.now()
        authorities_json = json.dumps(sorted(set(request.authorities_granted)))
        effective_start_iso = format_iso8601_ms(request.effective_start)
        effective_end_iso = (
            format_iso8601_ms(request.effective_end)
            if request.effective_end is not None
            else None
        )

        connection.execute(
            text(
                """
                INSERT INTO Role_Assignments (
                    role_assignment_id, party_id, role_name, scope,
                    authorities_granted, effective_start, effective_end,
                    assigning_authority_id, recorded_at
                ) VALUES (
                    :role_assignment_id, :party_id, :role_name, :scope,
                    :authorities_granted, :effective_start, :effective_end,
                    :assigning_authority_id, :recorded_at
                )
                """
            ),
            {
                "role_assignment_id": role_assignment_id,
                "party_id": request.party_id,
                "role_name": request.role_name,
                "scope": request.scope,
                "authorities_granted": authorities_json,
                "effective_start": effective_start_iso,
                "effective_end": effective_end_iso,
                "assigning_authority_id": request.assigning_authority_id,
                "recorded_at": format_iso8601_ms(recorded_at),
            },
        )

        # The recorded-time on the audit row matches the role assignment row
        # so every artifact of this consequential write shares one timestamp
        # (design §"Cross-Cutting Concerns", *Transactionality*).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=request.assigning_authority_id,
            action_type="assign.role",
            target_id=role_assignment_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_at,
            authorities_required=json.dumps(["modify"]),
        )

        return RoleAssignmentId(role_assignment_id)

    def evaluate(
        self,
        connection: Connection,
        party_id: str,
        action: str,
        target: TargetRef,
        at: datetime,
        *,
        correlation_id: Optional[str] = None,
    ) -> AuthorizationDecision:
        """Evaluate whether ``party_id`` may perform ``action`` on ``target`` at ``at``.

        Loads every role assignment recorded for ``party_id``, restricts to
        those whose ``authorities_granted`` includes the authority type
        required by ``action`` (Requirement 12.3, 12.4 — no substitution
        between view/modify/approve), and checks each one against the four
        gating conditions in Requirement 7.3:

        - revocation (``revoked_at <= at``),
        - expiration (``effective_end <= at``),
        - effective-start (``at < effective_start``),
        - scope coverage (role scope covers ``target.scope``).

        Returns ``permit(authority_basis=role-grant-id)`` on the first
        role assignment that satisfies all four. Otherwise returns
        ``deny(reason_code, correlation_id)`` where ``reason_code`` is the
        highest-priority denial reason found across the candidate
        assignments (revoked > expired > not-yet-effective > out-of-scope),
        or ``"no-role-assignment"`` when no assignment grants the required
        authority.

        Every call appends exactly one row to ``Audit_Records`` via
        :meth:`AuditLog.append_evaluation`, satisfying Requirement 12.5.
        The append participates in the caller's transaction (AD-WS-5);
        rolling back the transaction after a permit therefore also
        discards the evaluation record.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. Both the role-assignment read and the
                evaluation audit append run on this connection.
            party_id: Identity of the Party whose authority is being
                evaluated.
            action: An action string of the form
                ``"<prefix>.<resource_kind>"`` per
                design §"Authorization_Service".
            target: The :class:`TargetRef` for the resource being acted
                upon. ``target.scope`` is the value compared against role
                assignment scopes.
            at: The effective time at which authority is evaluated. Per
                design §"Cross-Cutting Concerns" (*Authorization*),
                authority is evaluated at the recorded time of the action,
                not at token-issue time.
            correlation_id: Optional correlation identifier; generated when
                omitted. The same value is recorded on the audit row and
                on the returned :class:`AuthorizationDecision`.

        Returns:
            An :class:`AuthorizationDecision` that is ``permit`` or
            ``deny``.

        Raises:
            ValueError: If ``action`` is malformed (see
                :func:`_required_authority`).
            walking_slice.audit.AuditAppendError: If the evaluation audit
                append fails.
        """
        required = _required_authority(action)
        correlation = correlation_id or _new_correlation_id()
        at_iso = format_iso8601_ms(at)
        authorities_required_json = json.dumps([required])

        # Load every role assignment for this Party. The slice's expected
        # cardinality per Party is small (handfuls, not thousands), so a
        # single per-Party SELECT is fine here; if profiling later shows
        # this hot, we can add a covering index on
        # ``Role_Assignments(party_id, role_assignment_id)``.
        rows = (
            connection.execute(
                text(
                    """
                    SELECT
                        role_assignment_id,
                        scope,
                        authorities_granted,
                        effective_start,
                        effective_end,
                        revoked_at
                    FROM Role_Assignments
                    WHERE party_id = :party_id
                    """
                ),
                {"party_id": party_id},
            )
            .mappings()
            .all()
        )

        # Filter to assignments that actually grant the required authority.
        # An assignment that does not grant the required authority is
        # irrelevant to the decision — Requirement 12.4 forbids substitution
        # between view/modify/approve, so a role granting only ``"approve"``
        # is not considered when evaluating a ``modify.*`` action.
        relevant: list[tuple[dict, list[str]]] = []
        for row in rows:
            try:
                granted = list(json.loads(row["authorities_granted"]))
            except (TypeError, ValueError):
                # A malformed row would have been rejected on insert; if
                # one slips through, treat it as granting no authorities.
                granted = []
            if required in granted:
                relevant.append((dict(row), granted))

        # Examine each relevant assignment. For each, identify every gating
        # condition it violates; if none, return permit. If at least one
        # condition is violated for every assignment, return deny with the
        # highest-priority reason code found across all candidates.
        observed_reasons: list[str] = []
        for row, granted in relevant:
            scope = row["scope"]
            revoked_at = row["revoked_at"]
            effective_end = row["effective_end"]
            effective_start = row["effective_start"]

            is_revoked = revoked_at is not None and revoked_at <= at_iso
            is_expired = effective_end is not None and at_iso >= effective_end
            is_not_yet_effective = at_iso < effective_start
            is_out_of_scope = not self._scope_covers(scope, target.scope)

            if not (is_revoked or is_expired or is_not_yet_effective or is_out_of_scope):
                authority_basis = AuthorityBasisRef(
                    type="role-grant-id",
                    id=UUID(row["role_assignment_id"]),
                )
                self.audit_log.append_evaluation(
                    connection,
                    actor_party_id=party_id,
                    action_type=action,
                    outcome="permit",
                    target_id=target.id,
                    target_revision_id=target.revision_id,
                    evaluated_role_assignment_id=row["role_assignment_id"],
                    authorities_required=authorities_required_json,
                    authorities_held=json.dumps(sorted(set(granted))),
                    correlation_id=correlation,
                    recorded_time=at,
                )
                return AuthorizationDecision.permit(
                    authority_basis,
                    correlation_id=correlation,
                )

            # Record every reason this assignment is non-permitting; the
            # priority filter below selects the most informative one
            # across all candidates.
            if is_revoked:
                observed_reasons.append("revoked")
            if is_expired:
                observed_reasons.append("expired")
            if is_not_yet_effective:
                observed_reasons.append("not-yet-effective")
            if is_out_of_scope:
                observed_reasons.append("out-of-scope")

        if not relevant:
            reason: str = "no-role-assignment"
            evaluated_role_assignment_id: Optional[str] = None
            authorities_held_json: Optional[str] = None
        else:
            reason = self._highest_priority_reason(observed_reasons)
            # Surface the first assignment that exhibited the chosen reason
            # so operators can investigate the specific role grant.
            evaluated_role_assignment_id = None
            for row, granted in relevant:
                if self._row_exhibits_reason(row, reason, at_iso, target.scope):
                    evaluated_role_assignment_id = row["role_assignment_id"]
                    authorities_held_json = json.dumps(sorted(set(granted)))
                    break
            else:  # pragma: no cover - defensive; reason came from these rows
                authorities_held_json = None

        self.audit_log.append_evaluation(
            connection,
            actor_party_id=party_id,
            action_type=action,
            outcome="deny",
            target_id=target.id,
            target_revision_id=target.revision_id,
            evaluated_role_assignment_id=evaluated_role_assignment_id,
            authorities_required=authorities_required_json,
            authorities_held=authorities_held_json,
            reason_code=reason,
            correlation_id=correlation,
            recorded_time=at,
        )
        return AuthorizationDecision.deny(reason, correlation_id=correlation)

    # -- internals ---------------------------------------------------------

    def _validate_assign_request(self, request: AssignRoleRequest) -> None:
        """Apply Requirement 12.6 validation to an :class:`AssignRoleRequest`.

        ``effective_start`` is the only timestamp the requirement names as
        required; ``effective_end`` remains optional. The five required
        attributes are listed in Requirement 12.1 / 12.6:

            Party Identity, role, scope, granted authorities, effective-start.

        We also reject any authority value outside :data:`_VALID_AUTHORITIES`
        so callers cannot record a role granting an unsupported authority
        that ``evaluate`` could never match.
        """
        missing: list[str] = []
        if not request.party_id:
            missing.append("party_id")
        if not request.role_name:
            missing.append("role_name")
        if not request.scope:
            missing.append("scope")
        if request.authorities_granted is None or len(request.authorities_granted) == 0:
            missing.append("authorities_granted")
        if request.effective_start is None:
            missing.append("effective_start")
        if not request.assigning_authority_id:
            missing.append("assigning_authority_id")

        if missing:
            raise InvalidRoleAssignmentError(
                f"Role assignment is missing required fields: {missing}",
                missing=missing,
            )

        invalid_authorities = [
            authority
            for authority in request.authorities_granted
            if authority not in _VALID_AUTHORITIES
        ]
        if invalid_authorities:
            raise InvalidRoleAssignmentError(
                "Role assignment carries authorities outside the "
                f"{sorted(_VALID_AUTHORITIES)} set: {invalid_authorities}",
                invalid=invalid_authorities,
            )

    @staticmethod
    def _scope_covers(role_scope: str, target_scope: Optional[str]) -> bool:
        """Return ``True`` iff ``role_scope`` covers ``target_scope``.

        Slice scope semantics (deliberately simple per the task brief):

        - The wildcard scope ``"*"`` covers every target scope, including
          ``None``.
        - Otherwise, coverage requires exact string equality between
          ``role_scope`` and ``target_scope``.

        This contract is intentionally narrow so the property tests can
        exercise out-of-scope denials without modelling a scope hierarchy.
        Widening the relation (e.g. ``"org/team-a"`` covered by ``"org/"``)
        is a future change inside this method only; the rest of the
        service does not assume any particular scope algebra.
        """
        if role_scope == "*":
            return True
        if target_scope is None:
            return False
        return role_scope == target_scope

    @staticmethod
    def _highest_priority_reason(observed: Sequence[str]) -> str:
        """Return the highest-priority reason present in ``observed``.

        Priority order, per the task description for 3.2:

            revoked > expired > not-yet-effective > out-of-scope > no-role-assignment

        If none of the priority values appears (e.g. because no relevant
        role assignment exists), ``"no-role-assignment"`` is returned —
        but this fallback is unused in practice because callers only
        invoke this helper when ``relevant`` is non-empty.
        """
        observed_set = set(observed)
        for candidate in _REASON_PRIORITY:
            if candidate in observed_set:
                return candidate
        return "no-role-assignment"

    @staticmethod
    def _row_exhibits_reason(
        row: dict,
        reason: str,
        at_iso: str,
        target_scope: Optional[str],
    ) -> bool:
        """Return ``True`` iff ``row`` exhibits ``reason`` at ``at_iso``.

        Used to choose which role-assignment identifier to record on the
        evaluation audit row when more than one assignment exhibits the
        chosen reason (e.g. two expired assignments). The first match wins,
        following the insertion order of ``Role_Assignments`` as returned
        by the per-Party SELECT.
        """
        revoked_at = row["revoked_at"]
        effective_end = row["effective_end"]
        effective_start = row["effective_start"]
        scope = row["scope"]

        if reason == "revoked":
            return revoked_at is not None and revoked_at <= at_iso
        if reason == "expired":
            return effective_end is not None and at_iso >= effective_end
        if reason == "not-yet-effective":
            return at_iso < effective_start
        if reason == "out-of-scope":
            return not AuthorizationService._scope_covers(scope, target_scope)
        return False
