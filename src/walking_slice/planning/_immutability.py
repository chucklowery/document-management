"""Application-level enforcement of Approved Plan Revision immutability.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Error Handling — HTTP error category mapping": *"Immutability
  violations — return HTTP 409 with ``error_code =
  approved_plan_revision_immutable``. A Denial Record is appended
  (Requirement 9.6)."*
- §"Property 20: Approved Plan Revision immutability" — for every
  Plan Revision whose ``lifecycle_state`` has been ``'approved'`` at
  any observation point, every later observation point shows the
  Plan Revision row, every constituent field of the Plan Revision
  Revision, every ``Supports`` / ``Addresses`` / ``Supersedes``
  Relationship sourced from or targeting that Plan Revision, every
  Plan Review Revision targeting that Plan Revision, and the
  corresponding ``Plan_Approval_Records`` row byte-equivalent to
  their first-approval state.

``.kiro/specs/second-walking-slice/requirements.md``:

- **Requirement 9.4** — *"ONCE a Plan Revision's lifecycle state is
  ``approved``, THE Planning_Service SHALL leave the Plan Revision
  row, every constituent field of the Plan Revision Revision, every
  ``Supports`` and ``Addresses`` Relationship sourced from or
  targeting that Plan Revision, and every Plan Review Revision
  targeting that Plan Revision byte-equivalent to their state
  immediately before approval, indefinitely, until and unless a
  future slice introduces a governed supersession path."*
- **Requirement 9.6** — *"IF an actor attempts to modify or delete a
  previously created Plan Approval Immutable Record, or to modify or
  delete an Approved Plan Revision or any of its constituent rows or
  Relationships, THEN THE Planning_Service SHALL reject the
  operation, leave the affected records byte-equivalent to their
  prior state, return an error indication identifying the
  immutability violation, and append a Denial Record to the Audit_Log
  conforming to Slice 1 Requirement 13.5."*

Task scope
==========

This module is the application-level half of task 11.2: the database
triggers installed by :mod:`walking_slice.planning._persistence`
(task 1.3) already reject every UPDATE / DELETE against approved
planning rows by raising ``RAISE(ABORT, ...)`` which surfaces through
the DBAPI as :class:`sqlalchemy.exc.IntegrityError`. The triggers are
the *source of truth*; this module:

1. Defines the application-level exception class
   :class:`ApprovedPlanRevisionImmutableError` carrying the stable
   ``error_code = "approved_plan_revision_immutable"`` the HTTP layer
   (task 15.1) renders as the body of the AD-WS-9 ``HTTP 409`` shape.
2. Provides a *pre-check* helper
   :func:`enforce_approved_plan_revision_immutability` that callers
   invoke before attempting a mutation against any planning resource
   tied to a Plan Revision. The helper consults the live row to
   decide whether the target is approved, persists a Denial Record in
   a SEPARATE transaction (so the caller's transaction can roll back
   without losing the denial), and raises the application error.
3. Provides a *post-mutation* helper
   :func:`map_integrity_error_to_immutability` that callers invoke
   when they catch an :class:`IntegrityError` from a mutation. The
   helper recognises trigger-raised messages by the stable substring
   markers (``"AD-WS-4"`` / ``"AD-WS-19"``) the triggers emit and
   translates the violation to the application error, persisting the
   Denial Record along the way. Unrelated IntegrityErrors are
   re-raised unchanged so callers do not swallow unrelated failures.

Both helpers reuse the AD-WS-9 / Slice 1 Requirement 7.6 retry
contract reproduced verbatim from
:meth:`walking_slice.planning.plan_approvals.PlanApprovalService._persist_plan_approval_denial`:
three retries after the initial attempt with backoff
``0.01s / 0.02s / 0.04s``. On total audit failure
:class:`ApprovedPlanRevisionImmutableAuditFailureError` is raised
*in place of* :class:`ApprovedPlanRevisionImmutableError` so the
operator is told that denial and audit have silently diverged.

The two helpers compose: the typical call site is a pre-check via
:func:`enforce_approved_plan_revision_immutability` (cheap, catches
the violation before the SQL round-trip) plus a ``try / except
IntegrityError`` wrapper around the mutation that calls
:func:`map_integrity_error_to_immutability` (catches the race where a
concurrent ``Plan_Approval`` transitioned the lifecycle between the
pre-check and the mutation, or where the caller forgot the
pre-check). The HTTP layer (task 15.1) maps both raise paths to the
same ``HTTP 409`` body.

The ``error_code`` value is the public contract; tests pin
:data:`APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE` as the stable
identifier.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime
from typing import Callable, Final, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog


__all__ = [
    "APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE",
    "APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE",
    "ApprovedPlanRevisionImmutableAuditFailureError",
    "ApprovedPlanRevisionImmutableError",
    "enforce_approved_plan_revision_immutability",
    "is_plan_revision_approved",
    "is_planning_immutability_violation",
    "map_integrity_error_to_immutability",
]


# ---------------------------------------------------------------------------
# Public contract constants.
# ---------------------------------------------------------------------------


APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE: Final[str] = (
    "approved_plan_revision_immutable"
)
"""The ``error_code`` value the HTTP layer renders for this violation.

Design §"Error Handling" pins this string verbatim. Tests reference
this constant rather than the literal so a future spelling change
flows through the suite without manual updates.
"""


APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE: Final[str] = (
    "approved-plan-revision-immutable"
)
"""The ``reason_code`` value recorded on the Denial Record.

Distinct from the AD-WS-9 / Slice 1 Requirement 7.2 authorization
reason codes (``not-yet-effective``, ``expired``, ``revoked``,
``out-of-scope``, ``no-role-assignment``, ``identifier-conflict``)
because the denial is not an authorization decision — the actor may
hold every relevant authority and still be rejected by the
immutability guarantee. The schema ``CHECK`` constraint on
``Audit_Records.reason_code`` is intentionally permissive
(Requirement 13.2 — *"reason_code values are extensible without
schema change"*) so this additive value is accepted without any
schema migration.
"""


_LIFECYCLE_APPROVED: Final[str] = "approved"
"""The single ``lifecycle_state`` value that triggers immutability.

Matches the literal used by
:mod:`walking_slice.planning._persistence` (the ``Plan_Revisions``
UPDATE trigger gates the ``'draft' → 'approved'`` transition) and
:mod:`walking_slice.planning.plan_approvals` (the
``CreatePlanApprovalResult.new_lifecycle_state`` returned on a
successful ``'Approve'`` outcome). Centralising the value here
guarantees the pre-check and the trigger speak the same string.
"""


# Trigger-message markers. ``walking_slice.planning._persistence``
# emits ``RAISE(ABORT, '<table> is append-only; UPDATE rejected per
# design AD-WS-4 / AD-WS-19.')`` for every immutable planning table
# and ``RAISE(ABORT, 'Plan_Revisions UPDATE rejected: only the
# draft->approved lifecycle transition is permitted, and only while
# the walking_slice.plan_approval_in_progress session pragma is set
# (AD-WS-19 / AD-WS-20).')`` for the Plan_Revisions lifecycle
# trigger. ``walking_slice.persistence`` emits ``RAISE(ABORT,
# '<table> is append-only; UPDATE rejected per design AD-WS-4.')``
# for every Slice 1 immutable table (including ``Relationships``
# which carries the constituent edges of an Approved Plan Revision).
# Either marker ("AD-WS-4" or "AD-WS-19") identifies the planning-
# trigger family with no false positives because no other RAISE
# message in the schema mentions those design identifiers.
_TRIGGER_MESSAGE_MARKERS: Final[tuple[str, ...]] = ("AD-WS-4", "AD-WS-19")


# Retry contract for the separate-transaction Denial Record append.
# Byte-equivalent to the sequences in
# :class:`walking_slice.planning.plan_approvals.PlanApprovalService`
# and :class:`walking_slice.planning.plan_revisions.PlanRevisionService`
# so every Planning_Service denial path presents identical timing
# (which Property 18 — Indistinguishable denial — relies on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ApprovedPlanRevisionImmutableError(Exception):
    """Raised when an actor attempts to mutate an Approved Plan Revision.

    The HTTP layer (task 15.1) maps this exception to ``HTTP 409``
    with body::

        {
          "error_code": "approved_plan_revision_immutable",
          "target_plan_revision_id": "...",
          "correlation_id": "..."
        }

    matching design §"Error Handling" rule 5. The ``error_code``
    field is sourced from
    :data:`APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE` so the wire
    contract has exactly one source of truth.

    Attributes:
        error_code: Always
            :data:`APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE`. The
            attribute exists on the instance (not only on the class)
            so callers reading ``exc.error_code`` always see the
            stable value without having to walk to the class.
        target_plan_revision_id: Identity of the Approved Plan
            Revision the mutation attempt targeted (directly or
            transitively through one of its constituent rows /
            Relationships).
        attempted_action: The action string recorded on the Denial
            Record (typically a verb identifying the mutation, e.g.
            ``"update.plan_revision"`` or ``"delete.plan_approval"``).
        correlation_id: The correlation identifier shared with the
            Denial Record so audit consumers can join the rejection
            response to the denial row.
    """

    error_code: str = APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        attempted_action: str,
        correlation_id: str,
    ) -> None:
        super().__init__(
            f"Approved Plan Revision {target_plan_revision_id!r} is "
            f"byte-equivalent forever per Requirement 9.4; the attempted "
            f"action {attempted_action!r} (correlation_id="
            f"{correlation_id!r}) was rejected with error_code="
            f"{APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE!r}."
        )
        self.error_code = APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE
        self.target_plan_revision_id = target_plan_revision_id
        self.attempted_action = attempted_action
        self.correlation_id = correlation_id


class ApprovedPlanRevisionImmutableAuditFailureError(RuntimeError):
    """Raised when every retry of the immutability Denial Record fails.

    Mirrors
    :class:`walking_slice.planning.plan_approvals.PlanApprovalAuditFailureError`
    and
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuditFailureError`.
    On total audit-append failure this exception is raised *in place
    of* :class:`ApprovedPlanRevisionImmutableError` so the operator
    is told that denial and audit have silently diverged.

    Attributes:
        target_plan_revision_id: Identity of the Approved Plan
            Revision the rejected mutation targeted.
        attempted_action: The action string the helper tried to
            record on the Denial Record.
        correlation_id: The correlation identifier shared with the
            (failed) Denial Record attempts.
        attempts: The total number of attempts the helper made
            before giving up (``len(_DENIAL_AUDIT_BACKOFFS_SECONDS)
            + 1``).
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        attempted_action: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for an Approved Plan Revision "
            f"immutability violation failed after {attempts} attempt(s): "
            f"target_plan_revision_id={target_plan_revision_id!r}, "
            f"attempted_action={attempted_action!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.attempted_action = attempted_action
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Plan Revision lifecycle inspection.
# ---------------------------------------------------------------------------


def is_plan_revision_approved(
    connection: Connection, plan_revision_id: str
) -> Optional[bool]:
    """Return whether *plan_revision_id* names an Approved Plan Revision.

    Reads ``Plan_Revisions.lifecycle_state`` for the named row using
    the caller's connection so the read participates in the caller's
    transactional view (a Plan Approval transaction that has set the
    lifecycle to ``'approved'`` but not yet committed is *not* visible
    to a separate connection — the pre-check therefore behaves
    coherently with respect to the in-flight transaction).

    Args:
        connection: SQLAlchemy connection bound to the same database
            instance as the Plan Revision row to inspect.
        plan_revision_id: Identity of the Plan Revision to inspect.

    Returns:
        ``True`` when the Plan Revision exists and its lifecycle
        state is ``'approved'``; ``False`` when the Plan Revision
        exists in any other lifecycle state (currently only
        ``'draft'``); ``None`` when no Plan Revision with that
        identifier exists. The tri-state return distinguishes "exists
        but not approved" from "does not exist" so the caller can
        decide whether to (a) proceed with the mutation (draft case),
        (b) raise the immutability error (approved case), or (c) let
        a separate not-found error surface (unresolved case).
    """
    row = connection.execute(
        text(
            "SELECT lifecycle_state FROM Plan_Revisions "
            "WHERE plan_revision_id = :plan_revision_id"
        ),
        {"plan_revision_id": plan_revision_id},
    ).scalar_one_or_none()
    if row is None:
        return None
    return row == _LIFECYCLE_APPROVED


# ---------------------------------------------------------------------------
# Pre-check enforcement helper.
# ---------------------------------------------------------------------------


def enforce_approved_plan_revision_immutability(
    *,
    engine: Engine,
    audit_log: AuditLog,
    target_plan_revision_id: str,
    actor_party_id: str,
    attempted_action: str,
    correlation_id: str,
    recorded_time: datetime,
    denial_audit_sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Reject and audit any mutation attempt against an Approved Plan Revision.

    Pre-check usage: callers invoke this helper *before* attempting a
    mutation that could affect the named Plan Revision's row, its
    constituent Plan Revision Revision fields, any Relationship
    sourced from or targeting it, any Plan Review Revision targeting
    it, or its Plan Approval Record. When the Plan Revision's
    ``lifecycle_state`` is ``'approved'`` the helper:

    1. Opens a SEPARATE :meth:`Engine.begin` transaction (so the
       Denial Record survives even if the caller is inside a
       transaction that will roll back) and calls
       :meth:`AuditLog.append_denial` with the immutability
       ``reason_code``.
    2. Retries the Denial Record append up to three times after the
       initial attempt with the same exponential backoff sequence
       (``0.01s / 0.02s / 0.04s``) used by the other Planning_Service
       denial paths — guaranteeing Property 18's indistinguishable-
       denial timing budget holds.
    3. On total audit failure raises
       :class:`ApprovedPlanRevisionImmutableAuditFailureError` *in
       place of* :class:`ApprovedPlanRevisionImmutableError` so the
       operator is told that denial and audit have silently
       diverged.
    4. On audit success raises
       :class:`ApprovedPlanRevisionImmutableError` carrying the
       stable ``error_code`` and the target identity.

    When the Plan Revision is not approved (lifecycle ``'draft'`` or
    unresolved), the helper returns silently. The caller's mutation
    may still fail on a downstream constraint — the helper
    intentionally does not opine on draft-state mutations because
    those are governed by other invariants (e.g. the ``Plan_Revisions``
    UPDATE trigger rejects every UPDATE that is not the gated
    ``'draft' → 'approved'`` transition, regardless of pre-check).

    Args:
        engine: The :class:`Engine` used to open the separate
            transaction for the pre-check SELECT and (on rejection)
            the Denial Record append. Both reads / writes run on
            their own transactions so the helper's behaviour does
            not depend on the caller having already opened one.
        audit_log: :class:`AuditLog` used to append the Denial
            Record.
        target_plan_revision_id: Identity of the Plan Revision the
            attempted mutation targets (directly or transitively).
        actor_party_id: Identity of the Party attempting the
            mutation. Recorded as ``actor_party_id`` on the Denial
            Record (Requirement 9.6 / 13.5).
        attempted_action: Action string identifying the mutation,
            e.g. ``"update.plan_revision"``,
            ``"delete.plan_approval"``,
            ``"update.relationship"``. Recorded as
            ``attempted_action`` on the Denial Record (Requirement
            10.2). Any non-empty string is accepted; the slice does
            not enforce an enumeration here because the value
            describes the application-layer operation, not an
            authorization action.
        correlation_id: Correlation identifier shared with the
            Denial Record and (when the HTTP layer renders the
            response) with the AD-WS-9 error body.
        recorded_time: UTC :class:`datetime` recorded on the Denial
            Record. Should be drawn from the
            :class:`walking_slice.clock.Clock` injected into the
            request context so the denial timestamp aligns with the
            other audit rows in the operation.
        denial_audit_sleep: Sleep function used to pause between
            retries. Defaults to :func:`time.sleep`; tests inject a
            recording stub so the retry sequence is observable
            without spending real time. Called with a single
            ``float`` argument naming the seconds to sleep, drawn
            from :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS`.

    Raises:
        ApprovedPlanRevisionImmutableError: The Plan Revision is
            approved and the Denial Record was appended successfully.
        ApprovedPlanRevisionImmutableAuditFailureError: The Plan
            Revision is approved *and* every retry of the Denial
            Record append failed. Replaces the immutability error so
            denial and audit cannot silently diverge.
    """
    # Pre-check uses its own connection because the helper is invoked
    # from contexts that may or may not have an open transaction — a
    # fresh connection is the cheapest correct choice. The read is
    # committed before any mutation, so any in-flight ``draft → approved``
    # transaction on another connection is invisible (SQLite WAL mode
    # readers see the last committed state); this is the correct
    # behaviour for a pre-check because the trigger on the same
    # connection that is *committing* the lifecycle transition would
    # catch the race anyway.
    with engine.connect() as conn:
        approved = is_plan_revision_approved(conn, target_plan_revision_id)
    if approved is not True:
        return

    _persist_immutability_denial(
        engine=engine,
        audit_log=audit_log,
        actor_party_id=actor_party_id,
        attempted_action=attempted_action,
        target_plan_revision_id=target_plan_revision_id,
        correlation_id=correlation_id,
        recorded_time=recorded_time,
        denial_audit_sleep=denial_audit_sleep,
    )
    raise ApprovedPlanRevisionImmutableError(
        target_plan_revision_id=target_plan_revision_id,
        attempted_action=attempted_action,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Post-mutation IntegrityError translator.
# ---------------------------------------------------------------------------


def is_planning_immutability_violation(error: IntegrityError) -> bool:
    """Return ``True`` if *error* originated from a planning immutability trigger.

    Detection is by stable substring markers (``"AD-WS-4"`` and
    ``"AD-WS-19"``) embedded in the ``RAISE(ABORT, ...)`` messages
    emitted by :mod:`walking_slice.planning._persistence` and the
    Slice 1 ``Relationships`` triggers in
    :mod:`walking_slice.persistence`. The markers are unique to the
    immutability triggers — no other RAISE message in the schema
    mentions either design identifier — so substring matching is
    sufficient and avoids brittle equality checks on the full
    message text.

    Args:
        error: The :class:`IntegrityError` to inspect.

    Returns:
        ``True`` when the error's message (read from ``error.orig``
        when present, falling back to ``str(error)``) contains any
        marker in :data:`_TRIGGER_MESSAGE_MARKERS`; ``False``
        otherwise.
    """
    # Try the underlying DBAPI exception first because it carries
    # the verbatim RAISE message; SQLAlchemy's IntegrityError adds a
    # statement preview wrapper which still contains the message but
    # checking ``.orig`` first is fractionally faster on the common
    # path. ``.orig`` may be ``None`` for synthesised
    # IntegrityErrors used in tests, so fall back to ``str(error)``
    # which always returns the SQLAlchemy-formatted message.
    message_sources: Iterable[object] = (
        error.orig if error.orig is not None else None,
        error,
    )
    for source in message_sources:
        if source is None:
            continue
        text_value = str(source)
        for marker in _TRIGGER_MESSAGE_MARKERS:
            if marker in text_value:
                return True
    return False


def map_integrity_error_to_immutability(
    error: IntegrityError,
    *,
    engine: Engine,
    audit_log: AuditLog,
    target_plan_revision_id: str,
    actor_party_id: str,
    attempted_action: str,
    correlation_id: str,
    recorded_time: datetime,
    denial_audit_sleep: Callable[[float], None] = time.sleep,
) -> ApprovedPlanRevisionImmutableError:
    """Translate a planning-trigger :class:`IntegrityError` into the application error.

    Intended to be called inside an ``except IntegrityError`` block
    around a mutation that could affect an Approved Plan Revision or
    one of its constituent rows / Relationships. The helper:

    1. Checks the IntegrityError against
       :func:`is_planning_immutability_violation`. If the marker is
       absent the helper *re-raises* the original error unchanged so
       callers do not silently swallow unrelated failures
       (FK violations, UNIQUE conflicts, CHECK failures from other
       slices, etc.).
    2. On a recognized immutability violation, appends a Denial
       Record in a SEPARATE transaction with the AD-WS-9 retry
       sequence (mirroring
       :func:`enforce_approved_plan_revision_immutability`).
    3. Returns an :class:`ApprovedPlanRevisionImmutableError`
       instance the caller raises. (Returning rather than raising
       lets the caller chain the application error from the
       original IntegrityError with ``raise ... from error`` so the
       stack trace preserves both.)

    Args:
        error: The :class:`IntegrityError` caught from the mutation.
        engine: :class:`Engine` used to open the separate transaction
            for the Denial Record append.
        audit_log: :class:`AuditLog` used to append the Denial Record.
        target_plan_revision_id: Identity of the Plan Revision whose
            approval state makes the rejected operation immutable.
            Recorded on the Denial Record as ``target_revision_id``.
        actor_party_id: Identity of the Party attempting the
            mutation.
        attempted_action: Action string identifying the mutation.
        correlation_id: Correlation identifier shared with the
            Denial Record and (downstream) with the HTTP error body.
        recorded_time: UTC :class:`datetime` recorded on the Denial
            Record.
        denial_audit_sleep: Sleep function used to pause between
            retries. Defaults to :func:`time.sleep`.

    Returns:
        :class:`ApprovedPlanRevisionImmutableError` carrying the
        ``error_code`` contract value and the supplied target /
        action / correlation values. The caller raises this from the
        original error to preserve the stack trace.

    Raises:
        sqlalchemy.exc.IntegrityError: When the supplied error does
            not match any planning immutability trigger marker. The
            original error is re-raised verbatim.
        ApprovedPlanRevisionImmutableAuditFailureError: When the
            error matches an immutability trigger marker *and* every
            retry of the Denial Record append failed.
    """
    if not is_planning_immutability_violation(error):
        raise error

    _persist_immutability_denial(
        engine=engine,
        audit_log=audit_log,
        actor_party_id=actor_party_id,
        attempted_action=attempted_action,
        target_plan_revision_id=target_plan_revision_id,
        correlation_id=correlation_id,
        recorded_time=recorded_time,
        denial_audit_sleep=denial_audit_sleep,
    )
    return ApprovedPlanRevisionImmutableError(
        target_plan_revision_id=target_plan_revision_id,
        attempted_action=attempted_action,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Shared denial-record helper.
# ---------------------------------------------------------------------------


def _persist_immutability_denial(
    *,
    engine: Engine,
    audit_log: AuditLog,
    actor_party_id: str,
    attempted_action: str,
    target_plan_revision_id: str,
    correlation_id: str,
    recorded_time: datetime,
    denial_audit_sleep: Callable[[float], None],
) -> None:
    """Append the immutability-violation Denial Record with retries.

    Implements the AD-WS-9 / Slice 1 Requirement 7.6 retry contract
    reproduced from
    :meth:`walking_slice.planning.plan_approvals.PlanApprovalService._persist_plan_approval_denial`:
    each attempt opens a *new* :meth:`Engine.begin` transaction (so
    a previous attempt's rollback does not poison this one), tries
    :meth:`AuditLog.append_denial`, and either returns on success or
    pauses by the next entry in
    :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

    - **Attempt 1** runs immediately.
    - **Attempt 2** runs after a 10-millisecond pause.
    - **Attempt 3** runs after a 20-millisecond pause.
    - **Attempt 4** runs after a 40-millisecond pause.
    - If attempt 4 also fails,
      :class:`ApprovedPlanRevisionImmutableAuditFailureError` is
      raised.

    Both :class:`AuditAppendError` and :class:`SQLAlchemyError` are
    treated as retryable failures: the former wraps the latter for
    callers who use :class:`AuditLog`, but a transaction-management
    failure (e.g. ``engine.begin()`` raising) surfaces as a bare
    :class:`SQLAlchemyError`. Unrelated exceptions propagate
    unchanged so genuine programming errors (e.g. a typo in
    ``attempted_action`` triggering an unrelated TypeError elsewhere
    in :meth:`AuditLog.append_denial`) are not silently retried.

    The denial row's ``target_id`` is left ``None`` because the
    rejected operation may have targeted any of several constituent
    rows (the Plan Revision itself, a Relationship, a Plan Review
    Revision, the Plan Approval Record); ``target_revision_id``
    carries the Plan Revision Identity so audit consumers can join
    the denial row back to the Approved Plan Revision that made the
    operation rejected. Requirement 10.2 names the target Plan
    Revision Identity as the correlating identifier for denial
    rows in this slice, so the chosen column placement matches
    that requirement.
    """
    last_error: Optional[BaseException] = None
    total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
    for attempt_index in range(total_attempts):
        try:
            with engine.begin() as denial_conn:
                audit_log.append_denial(
                    denial_conn,
                    actor_party_id=actor_party_id,
                    attempted_action=attempted_action,
                    target_id=None,
                    target_revision_id=target_plan_revision_id,
                    reason_code=APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE,
                    correlation_id=correlation_id,
                    recorded_time=recorded_time,
                )
            return  # success — Denial Record committed in its own tx
        except (AuditAppendError, SQLAlchemyError) as exc:
            last_error = exc
            if attempt_index < len(_DENIAL_AUDIT_BACKOFFS_SECONDS):
                denial_audit_sleep(
                    _DENIAL_AUDIT_BACKOFFS_SECONDS[attempt_index]
                )

    raise ApprovedPlanRevisionImmutableAuditFailureError(
        target_plan_revision_id=target_plan_revision_id,
        attempted_action=attempted_action,
        correlation_id=correlation_id,
        attempts=total_attempts,
    ) from last_error
