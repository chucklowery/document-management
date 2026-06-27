"""Execution_Service.WorkEvents — immutable Work Event Records appended
to an existing Work Assignment Record by its named assignee.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Execution_Service.WorkEvents" — public dataclass surface, authority
  string (``create.work_event`` → ``contribute`` AND assignee binding
  per AD-WS-24 and AD-WS-29), AD-WS-9 separate-transaction Denial Record
  on deny, and the Relationship-row contract (``Relates To`` from the
  Work Event Record to the Work Assignment Record with
  ``semantic_role = 'work_event'`` per AD-WS-26).
- §"Event-kind state machine" — the per-Work-Assignment closed
  enumeration that bounds the legal sequence of event kinds:

    * ``started``: rejected if any prior ``Work_Event_Records`` row
      with ``event_kind = 'started'`` exists for the same
      ``target_work_assignment_id``.
    * ``progress_note`` / ``paused`` / ``deliverable_drafted``:
      rejected if no prior ``started`` exists for the same Work
      Assignment.
    * ``resumed``: rejected if no prior ``started`` exists OR if the
      most recent prior event in ``{paused, resumed}`` is not
      ``paused`` (resume requires a pause that has not been resumed).

  The state-machine check is implemented as a single covering query
  driven by the ``idx_work_events_by_wa_recent`` index seeded in task
  1.2; the application then evaluates deterministic Python logic over
  the returned ordered list. The check runs inside the caller's
  transaction so concurrent attempts at the same transition fail one
  of them via the SQLite write lock — and the partial UNIQUE index
  ``idx_work_events_one_started_per_wa`` provides a second-line guard
  for the ``started`` race (Requirement 24.3).

- §"Cross-Cutting Concerns" — Transactionality (one recorded time
  shared by every row in the transaction); Identifiers (Work Event
  Record Identity and the Relationship Identity are UUIDv7 minted by
  :class:`IdentityService` and registered in ``Identifier_Registry``
  with ``kind = 'immutable_record'`` and
  ``resource_kind = 'work_event_record'`` per AD-WS-28); Authorization
  (the action string ``create.work_event`` maps to the ``contribute``
  authority per AD-WS-24; the deny path uses the Slice 1
  separate-transaction Denial-Record pattern, and Contributor writes
  layer the AD-WS-29 second stage on top — see below).
- AD-WS-24 — additive ``contribute`` mapping for
  ``create.work_event``.
- AD-WS-26 — Relationship row: ``Work Event Record`` ``Relates To``
  ``Work Assignment Record`` with ``semantic_role = 'work_event'``.
- AD-WS-27 — ``Work_Event_Records`` is append-only; the
  ``Work_Assignment_Records`` row addressed by the Work Event must
  remain byte-equivalent throughout this transaction.
- AD-WS-28 — additive ``Identifier_Registry.resource_kind`` value
  ``'work_event_record'`` populated through
  :func:`walking_slice.execution._helpers._record_execution_artifact`.
- AD-WS-29 — two-stage authority evaluation for Contributor writes:
  the service first calls :meth:`AuthorizationService.evaluate` with
  the Work Assignment Record as the target, and then on a ``permit``
  outcome re-reads the persisted ``Work_Assignment_Records`` row
  inside the caller's transaction and requires
  ``assignee_party_id == recording_party_id``. Both stages must pass;
  a failure of either stage produces an AD-WS-9-conformant denial
  response (Slice 1 Requirement 7.2
  ``reason_code = 'no-role-assignment'`` for the assignee-binding
  failure).

Task scope (task 6.1)
=====================

This module implements :meth:`WorkEventService.create_work_event`:

1. Defensively reject any prohibited planning-attribute or
   observed-outcome key in the original request body via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
2. Validate request inputs per Requirement 24.2
   (``event_kind`` drawn from the five-value closed enumeration,
   ``event_note`` length 0..4000, ``authority_basis.type`` drawn from
   the AD-WS-10 set, ``applicable_scope`` present, every required
   identifier present and string-shaped).
3. Resolve the target Work Assignment Record by primary key on the
   caller's connection (Requirement 24.4 — unresolvable target
   rejected). Capture ``assignee_party_id`` and ``applicable_scope``
   for downstream stages.
4. Evaluate ``Authorization_Service.evaluate(party=recording_party_id,
   action="create.work_event", target=work_assignment_record,
   at=now())`` on a separate transaction (Slice 1 single-writer
   accommodation). On ``deny``, append the Denial Record in another
   separate transaction with the Requirement 7.6 / 30.6 retry sequence
   (0.01s / 0.02s / 0.04s exponential backoff). On total audit
   failure raise :class:`WorkEventAuditFailureError` in place of
   :class:`WorkEventAuthorizationError`.
5. On ``permit``, perform the AD-WS-29 second stage: re-read the
   persisted ``Work_Assignment_Records`` row and require
   ``assignee_party_id == recording_party_id``. On mismatch append a
   Denial Record (separate transaction) with
   ``reason_code = 'no-role-assignment'`` and raise
   :class:`WorkEventAssigneeBindingError`. The caller's surrounding
   ``engine.begin()`` context manager rolls back without persisting
   any Work Event Record.
6. Run the per-Work-Assignment event-kind state machine on the
   caller's connection using the indexed
   ``(target_work_assignment_id, recorded_at DESC)`` query against
   ``Work_Event_Records``. Violations are pure validation rejections
   per Requirement 24.4 (a Denial Record is NOT appended for
   state-machine failures because the action is structurally
   ill-formed, not an authority denial).
7. On permit AND assignee-binding AND state-machine OK, mint the
   Work Event Record Identity and the Relationship Identity (UUIDv7).
   Register the Work Event Identity in ``Identifier_Registry`` with
   ``kind='immutable_record'`` and
   ``resource_kind='work_event_record'`` per AD-WS-28 via
   :func:`_record_execution_artifact`.
8. INSERT the ``Work_Event_Records`` row carrying every Requirement
   24.2 attribute.
9. INSERT exactly one ``Relationships`` row with
   ``relationship_type='Relates To'``,
   ``source_kind='work_event_record'`` / ``source_id=work_event_id``
   / ``source_revision_id=NULL``,
   ``target_kind='work_assignment_record'`` /
   ``target_id=target_work_assignment_id`` /
   ``target_revision_id=NULL``, and ``semantic_role='work_event'``
   (AD-WS-26).
10. Append the consequential ``Audit_Records`` row with
    ``action_type='create.work_event'`` and
    ``target_id=work_event_id`` inside the same transaction
    (Requirement 24.6).

Requirements satisfied
======================

    24.1 — authorized Work Event creation by the named assignee
           produces exactly one immutable Work Event Record within
           nominal latency.
    24.2 — every Work Event Record records the target Work Assignment
           Record Identity, the event kind drawn from
           ``{started, progress_note, paused, resumed,
           deliverable_drafted}``, the event note text of 0..4000
           characters, the recording Contributor Party Identity, the
           authority basis (AD-WS-10 set), the applicable scope, the
           recorded time in UTC with millisecond precision, and the
           ``Relates To`` Relationship with
           ``semantic_role='work_event'`` from the Work Event to the
           Work Assignment.
    24.3 — at most one ``started`` event per Work Assignment Record
           (state-machine application check + partial UNIQUE index
           safety net); ``progress_note`` / ``paused`` / ``resumed`` /
           ``deliverable_drafted`` rejected without a prior
           ``started``; ``resumed`` rejected without a prior
           ``paused`` (most recent prior in ``{paused, resumed}``
           must be ``paused``).
    24.4 — structural rejections (unresolvable target, second
           ``started``, non-``started`` without ``started``,
           ``resumed`` without prior ``paused``, missing
           applicable scope, event-kind out of set, missing
           required field) are rejected with no Work Event Record
           created and a structured error listing each invalid
           attribute.
    24.5 — unauthenticated / lacking ``contribute`` authority /
           not the named assignee — :class:`AuthorizationService`
           denies the action; the Execution_Service declines to
           create any Work Event Record and the Audit_Log appends
           a Denial Record conforming to AD-WS-9.
    24.6 — the consequential audit row identifying the Work Event
           Record Identity, target Work Assignment Identity, event
           kind, recording Contributor Party Identity, and recorded
           time is appended within the same transaction.
    24.7 — the append-only schema triggers (created in task 1.2)
           reject every UPDATE / DELETE attempt on
           ``Work_Event_Records`` after this transaction commits.
    32.7 — ``create.work_event`` requires the ``contribute``
           authority (AD-WS-24); the non-substitution invariant is
           preserved by :class:`AuthorizationService`.
    33.4 — request bodies carrying prohibited planning-attribute
           prefixes are rejected with the offending keys identified.
    34.5 — request bodies carrying prohibited observed-outcome
           prefixes are rejected with the offending keys identified.
    41.1 — every consequential write is atomic with its consequential
           audit row.
    41.2 — every consequential write checks authority before
           persisting any domain row.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid_utils
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.execution._helpers import (
    ALL_PROHIBITED_PREFIXES,
    ExecutionValidationError,
    _record_execution_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef


__all__ = [
    "CreateWorkEventResult",
    "WorkEventAssigneeBindingError",
    "WorkEventAuditFailureError",
    "WorkEventAuthorizationError",
    "WorkEventNoPriorStartedError",
    "WorkEventResumeRequiresPausedError",
    "WorkEventService",
    "WorkEventStartedAlreadyExistsError",
    "WorkEventValidationError",
    "WorkEventWorkAssignmentNotResolvableError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship kind / type strings, registry kind /
# resource_kind strings, and validation limits are pulled out as
# module-level ``Final`` so the names downstream property tests look
# for in ``Audit_Records.action_type``, in
# ``Identifier_Registry.resource_kind``, and in
# ``Relationships.semantic_role`` are textually stable. The strings
# stay aligned with the :mod:`walking_slice.execution._persistence`
# schema, the AD-WS-24 authority mapping in
# :mod:`walking_slice.authorization`, and the AD-WS-26
# Relationship-Type / semantic-role table.
# ---------------------------------------------------------------------------


# ``create.work_event`` maps to the ``contribute`` authority per
# AD-WS-24 / Requirement 32.7. The string is also the ``action_type``
# recorded on the consequential audit row (Requirement 24.6) and on
# the separate-transaction Denial Record appended by
# :meth:`WorkEventService._persist_denial` so audit consumers can
# correlate denial rows with the action a Party was attempting.
_ACTION_CREATE_WORK_EVENT: Final[str] = "create.work_event"


# Authorization target kind. The target of a ``create.work_event``
# evaluation is the Work Assignment Record — the scope on which
# Contributor authority is held (AD-WS-29) — not the Work Event Record
# itself, which does not yet exist when authorization is evaluated.
_KIND_WORK_ASSIGNMENT_RECORD: Final[str] = "work_assignment_record"


# Relationship kind / type / semantic-role strings per AD-WS-26
# (row: ``Work Event Record`` ``Relates To`` ``Work Assignment
# Record`` with ``semantic_role = 'work_event'``).
_RELATIONSHIP_TYPE_RELATES_TO: Final[str] = "Relates To"
_KIND_WORK_EVENT_RECORD: Final[str] = "work_event_record"
_SEMANTIC_ROLE_WORK_EVENT: Final[str] = "work_event"


# Identifier_Registry registration kind (Slice 1 enumeration) and
# Execution_Service ``resource_kind`` tag (Slice 3 additive
# enumeration per AD-WS-28). Work Event Records are Immutable Records
# (per [`02-domain-model.md`](../../../documents/02-domain-model.md)
# §8.2 Execution Record) so the registry kind is
# ``'immutable_record'``; the ``resource_kind`` value is
# ``'work_event_record'`` and is the row-level discriminator that
# keeps the Work Event Identity set inspectably disjoint from every
# other Slice 1 / Slice 2 / Slice 3 ``resource_kind``
# (Requirement 22.8).
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_WORK_EVENT: Final[str] = "work_event_record"


# Event-kind enumeration per Requirement 24.2 — the closed set the
# Work Event state machine operates over. Mirrors the schema-level
# CHECK constraint on ``Work_Event_Records.event_kind`` so a
# misspelled enum value never escapes the application validator (and
# the schema CHECK is a safety net for the rare case the application
# is bypassed).
_VALID_EVENT_KINDS: Final[frozenset[str]] = frozenset(
    {"started", "progress_note", "paused", "resumed", "deliverable_drafted"}
)


# Authority-basis ``type`` enumeration per AD-WS-10. Mirrors the
# Slice 2 ``Plan_Review_Revisions`` / ``Plan_Approval_Records`` and
# Slice 3 ``Work_Event_Records.authority_basis_type`` CHECK
# constraint values; centralizing the tuple here lets the validator
# reject malformed requests structurally before they touch SQL.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)


# Validation limits for ``event_note`` per Requirement 24.2. The
# ``Work_Event_Records.event_note`` CHECK constraint enforces the same
# range; surfacing it here yields a precise ``failed_constraint`` for
# the HTTP layer rather than a raw SQL constraint violation.
_EVENT_NOTE_MIN_CHARS: Final[int] = 0
_EVENT_NOTE_MAX_CHARS: Final[int] = 4_000


# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (mirrors Slice 1 Requirement 7.6 and Slice 3
# Requirement 30.6). Three retries after the initial attempt for a
# total of four attempts. The sequence is byte-equivalent to the
# sibling Slice 2 / Slice 3 modules so every Slice 1 / Slice 2 /
# Slice 3 module presents identical denial-side timing (Property 8 —
# Indistinguishable denial — relies on this).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# Denial-reason code used when authorization permits the action but
# the AD-WS-29 second stage rejects it because the recording Party is
# not the named assignee on the target Work Assignment. Slice 1
# Requirement 7.2 enumerates this value as ``'no-role-assignment'``;
# the Slice 3 design §"Cross-Cutting Concerns" (*Authorization*)
# directs that the missing-assignee case be treated as "no effective
# Contributor role on this specific Work Assignment".
_REASON_CODE_NO_ROLE_ASSIGNMENT: Final[str] = "no-role-assignment"


# Event-kind set the ``resumed`` state-machine rule scans for the
# "most recent prior" check. Only ``paused`` and ``resumed`` are
# relevant for the pause-cycle bookkeeping; ``progress_note`` and
# ``deliverable_drafted`` events interleaved between a ``paused`` and
# its matching ``resumed`` do not invalidate the resume.
_PAUSE_CYCLE_KINDS: Final[frozenset[str]] = frozenset({"paused", "resumed"})


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class WorkEventValidationError(ValueError):
    """Raised when a Work Event submission fails Requirement 24.2 / 24.4 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_work_assignment_id_missing"``,
            ``"recording_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"event_kind_missing"``,
            ``"event_kind_out_of_set"``,
            ``"event_note_invalid_type"``,
            ``"event_note_too_long"``,
            ``"authority_basis_missing"``,
            ``"authority_basis_type_missing"``,
            ``"authority_basis_type_out_of_set"``,
            ``"authority_basis_id_missing"``,
            ``"prohibited_attribute"`` (the request body carried at
                least one planning-attribute or observed-outcome
                attribute — see :attr:`prohibited_keys`).
        prohibited_keys: Populated only when ``failed_constraint`` is
            ``"prohibited_attribute"``; lists every offending top-level
            key in the original order from the request body. Empty
            tuple in every other case.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        prohibited_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.prohibited_keys = prohibited_keys


class WorkEventWorkAssignmentNotResolvableError(LookupError):
    """Raised when the target Work Assignment Identity does not resolve.

    Requirement 24.4 requires the named target Work Assignment Record
    Identity to resolve to an existing ``Work_Assignment_Records`` row.
    The check runs before authorization evaluation so the deny path
    never reveals whether a Work Assignment exists for an
    unauthenticated caller.

    Attributes:
        target_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        failed_constraint: ``"target_work_assignment_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        target_work_assignment_id: str,
        failed_constraint: str = "target_work_assignment_not_resolvable",
    ) -> None:
        super().__init__(
            f"target_work_assignment_id {target_work_assignment_id!r} did "
            "not resolve to an existing Work Assignment Record "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_work_assignment_id = target_work_assignment_id
        self.failed_constraint = failed_constraint


class WorkEventStartedAlreadyExistsError(ValueError):
    """Raised by the state-machine check when ``event_kind='started'`` is
    submitted against a Work Assignment that already has a ``started``
    event.

    Requirement 24.3 requires at most one ``started`` event per Work
    Assignment. The application-level check fires first (so the
    structured error carries a stable ``failed_constraint`` and lists
    the prior Work Event Identity for debugging); the partial UNIQUE
    index ``idx_work_events_one_started_per_wa`` is the database-layer
    safety net that catches concurrent attempts which slip past the
    application check.

    Attributes:
        target_work_assignment_id: The Work Assignment Identity the
            second ``started`` was attempted against.
        failed_constraint: ``"started_already_exists"``.
    """

    def __init__(
        self,
        *,
        target_work_assignment_id: str,
        failed_constraint: str = "started_already_exists",
    ) -> None:
        super().__init__(
            f"Work Assignment {target_work_assignment_id!r} already has a "
            "'started' Work Event; Requirement 24.3 permits at most one "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_work_assignment_id = target_work_assignment_id
        self.failed_constraint = failed_constraint


class WorkEventNoPriorStartedError(ValueError):
    """Raised by the state-machine check when a non-``started`` event is
    submitted against a Work Assignment with no prior ``started``.

    Requirement 24.3 requires a ``started`` Work Event Record to exist
    on the same Work Assignment before any ``progress_note``,
    ``paused``, ``resumed``, or ``deliverable_drafted`` event is
    recorded.

    Attributes:
        target_work_assignment_id: The Work Assignment Identity the
            non-``started`` event was attempted against.
        event_kind: The event kind the request submitted (one of
            ``{progress_note, paused, resumed, deliverable_drafted}``).
        failed_constraint: ``"no_prior_started_event"``.
    """

    def __init__(
        self,
        *,
        target_work_assignment_id: str,
        event_kind: str,
        failed_constraint: str = "no_prior_started_event",
    ) -> None:
        super().__init__(
            f"Work Assignment {target_work_assignment_id!r} has no prior "
            f"'started' Work Event; Requirement 24.3 rejects "
            f"{event_kind!r} until a 'started' event has been recorded "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_work_assignment_id = target_work_assignment_id
        self.event_kind = event_kind
        self.failed_constraint = failed_constraint


class WorkEventResumeRequiresPausedError(ValueError):
    """Raised by the state-machine check when ``resumed`` is submitted
    against a Work Assignment whose most recent prior event in
    ``{paused, resumed}`` is not ``paused`` (or where no such event
    exists at all).

    Requirement 24.3 requires a ``paused`` Work Event to exist on the
    same Work Assignment before any later ``resumed`` event, AND
    requires the most recent prior event in ``{paused, resumed}`` to
    be ``paused`` (i.e., resume requires a pause that has not yet
    been resumed).

    Attributes:
        target_work_assignment_id: The Work Assignment Identity the
            ``resumed`` was attempted against.
        most_recent_pause_cycle_kind: The event kind observed at the
            top of the ``{paused, resumed}`` chronology, or ``None``
            when no such event exists.
        failed_constraint: ``"resume_requires_paused"``.
    """

    def __init__(
        self,
        *,
        target_work_assignment_id: str,
        most_recent_pause_cycle_kind: Optional[str],
        failed_constraint: str = "resume_requires_paused",
    ) -> None:
        super().__init__(
            f"Work Assignment {target_work_assignment_id!r} cannot accept a "
            "'resumed' Work Event: the most recent prior event in "
            f"{{paused, resumed}} is "
            f"{most_recent_pause_cycle_kind!r}; Requirement 24.3 requires "
            f"'paused' (failed_constraint={failed_constraint!r})."
        )
        self.target_work_assignment_id = target_work_assignment_id
        self.most_recent_pause_cycle_kind = most_recent_pause_cycle_kind
        self.failed_constraint = failed_constraint


class WorkEventAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Work Event creation attempt.

    Mirrors
    :class:`walking_slice.execution.work_assignments.WorkAssignmentAuthorizationError`
    and
    :class:`walking_slice.deliverables.repository.DeliverableRepositoryAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape
    ``{generic_denial_indicator, reason_code, correlation_id}``
    (Requirement 24.5 / Slice 1 Requirement 10). The exception carries
    only ``reason_code`` and ``correlation_id`` — the
    indistinguishable-denial invariant forbids leaking authorized
    Party identities, target existence, or role-assignment details
    beyond the requesting Party's view authority through the denial
    response.

    The same exception type is raised when the AD-WS-29 second stage
    fails (the recording Party is not the named assignee on the target
    Work Assignment); in that case ``reason_code`` is
    :data:`_REASON_CODE_NO_ROLE_ASSIGNMENT` so the HTTP layer can
    surface the same response shape it surfaces for an authorization
    deny.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Work Event creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class WorkEventAssigneeBindingError(WorkEventAuthorizationError):
    """Specialised :class:`WorkEventAuthorizationError` for the AD-WS-29
    assignee-binding failure.

    Subclass of :class:`WorkEventAuthorizationError` so callers that
    catch the broader denial path continue to work, while tests that
    need to assert specifically on the AD-WS-29 path can catch this
    narrower type. The denial response shape is identical:
    ``{reason_code = 'no-role-assignment', correlation_id}``.

    Attributes:
        target_work_assignment_id: The Work Assignment Identity against
            which the assignee binding was evaluated.
        recording_party_id: The Party Identity the caller submitted as
            the recording Contributor.
        actual_assignee_party_id: The Party Identity actually persisted
            on ``Work_Assignment_Records.assignee_party_id``.
    """

    def __init__(
        self,
        *,
        target_work_assignment_id: str,
        recording_party_id: str,
        actual_assignee_party_id: str,
        correlation_id: str,
    ) -> None:
        super().__init__(
            reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
            correlation_id=correlation_id,
        )
        self.target_work_assignment_id = target_work_assignment_id
        self.recording_party_id = recording_party_id
        self.actual_assignee_party_id = actual_assignee_party_id


class WorkEventAuditFailureError(RuntimeError):
    """Raised when every retry of the Denial Record append fails
    (Requirement 30.6).

    Mirrors
    :class:`walking_slice.execution.work_assignments.WorkAssignmentAuditFailureError`.
    On total audit-append failure the exception is raised *in place
    of* :class:`WorkEventAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's
    transaction still rolls back so no ``Work_Event_Records`` row,
    ``Relationships`` row, or consequential audit row is persisted.

    Attributes:
        reason_code: The denial reason code from the evaluation that
            triggered this denial path.
        correlation_id: The correlation identifier shared with the
            (rolled-back) evaluation row and with the (failed) denial
            record attempts.
        attempts: The total number of attempts made before giving up.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Work Event failed after "
            f"{attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Result value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateWorkEventResult:
    """Result of :meth:`WorkEventService.create_work_event`.

    Returned so callers (the HTTP layer in task 15.1, tests, the
    downstream Provenance_Navigator that traverses the Execution
    Provenance Chain, and audit consumers) can correlate the created
    Work Event Record with its Relationship row and its consequential
    audit row in one round-trip.

    Attributes:
        work_event_id: The Work Event Record Identity (UUIDv7).
        target_work_assignment_id: The target Work Assignment Identity;
            copied byte-equivalent from the request input.
        event_kind: The persisted event kind drawn from
            ``{started, progress_note, paused, resumed,
            deliverable_drafted}``.
        event_note: The persisted event note (0..4000 chars) or
            ``None`` when omitted.
        recording_party_id: Identity of the recording Contributor
            Party — the named assignee on the target Work Assignment
            per AD-WS-29.
        authority_basis: The validated :class:`AuthorityBasisRef`
            recorded on the Work Event Record.
        applicable_scope: Scope identifier the Work Event applies
            within; byte-equivalent to
            ``Work_Assignment_Records.applicable_scope`` per AD-WS-29.
        relates_to_relationship_id: Identity of the single
            ``Relates To`` ``Relationships`` row inserted alongside
            the Work Event binding it to the Work Assignment with
            ``semantic_role='work_event'``.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Work_Event_Records`` row, the
            ``Relationships`` row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    work_event_id: str
    target_work_assignment_id: str
    event_kind: str
    event_note: Optional[str]
    recording_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    relates_to_relationship_id: str
    recorded_at: str
    correlation_id: str



# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkEventService:
    """Persist immutable Work Event Records and their ``Relates To``
    Relationship to the target Work Assignment per AD-WS-26.

    Like :class:`walking_slice.execution.work_assignments.WorkAssignmentService`
    and
    :class:`walking_slice.deliverables.repository.DeliverableRepositoryService`,
    this service is connection-scoped at call time:
    :meth:`create_work_event` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (Slice 1 AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.WorkEvents" declares it
    ``@dataclass(frozen=True)`` — Slice 3 service instances follow the
    Slice 2 convention of being immutable container objects that
    bundle their collaborators.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Work_Event_Records``, ``Relationships``, and
            ``Audit_Records`` rows. The clock is consulted exactly
            once per write so every artifact of the transaction
            shares one timestamp.
        identity_service: Generates the Work Event Record Identity
            (UUIDv7) and the Relationship Identity, plus drives the
            ``Identifier_Registry`` binding via
            :func:`_record_execution_artifact` (the Work Event binding
            carries the Slice 3 ``resource_kind='work_event_record'``
            tag per AD-WS-28).
        audit_log: Appends the consequential audit row
            (Requirement 24.6) inside the caller's transaction.
        authorization_service: Evaluates ``create.work_event``
            authority per AD-WS-24 / Requirement 24.5; the deny path
            is the Slice 1 separate-transaction Denial-Record pattern.
        denial_audit_sleep: Sleep function used to pause between
            retries of the Denial Record append. Defaults to
            :func:`time.sleep`; tests that need deterministic timing
            inject a recording stub so the retry sequence is
            observable without spending real time.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: AuthorizationService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_work_event(
        self,
        connection: Connection,
        *,
        target_work_assignment_id: str,
        event_kind: str,
        event_note: Optional[str],
        recording_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateWorkEventResult:
        """Create an immutable Work Event Record and its ``Relates To``
        Relationship per AD-WS-26.

        Per Requirements 24.1 through 24.7, AD-WS-9 (indistinguishable
        denial), AD-WS-24 (``create.work_event`` → ``contribute``),
        AD-WS-26 (Relationship-Type / semantic-role table), AD-WS-27
        (append-only Slice 3 tables), AD-WS-28 (additive
        ``resource_kind`` values), and AD-WS-29 (two-stage assignee
        binding):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
        2. Input validation (Requirement 24.2 / 24.4) — every
           range, enumeration, and required-attribute check runs
           before any database read so a malformed request never
           touches identity service, the ``Work_Assignment_Records``
           lookup, or the authorization service.
        3. Resolve the target Work Assignment Record by primary key
           on the caller's connection. Reject when unresolvable
           (:class:`WorkEventWorkAssignmentNotResolvableError`) per
           Requirement 24.4.
        4. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). The
           authorization target is the Work Assignment Record — the
           scope on which Contributor authority is held (AD-WS-29).
           On ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 / 30.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`WorkEventAuditFailureError` in place of
           :class:`WorkEventAuthorizationError`.
        5. AD-WS-29 second stage: re-read the persisted Work
           Assignment Record and require
           ``assignee_party_id == recording_party_id``. On mismatch,
           append a Denial Record with
           ``reason_code = 'no-role-assignment'`` in a separate
           transaction and raise
           :class:`WorkEventAssigneeBindingError`. The caller's
           surrounding ``engine.begin()`` context manager rolls back
           without persisting any Work Event Record.
        6. Run the per-Work-Assignment event-kind state machine on
           the caller's connection using the indexed
           ``(target_work_assignment_id, recorded_at DESC)`` query
           against ``Work_Event_Records``. Violations are pure
           validation rejections per Requirement 24.4 — no Denial
           Record is appended.
        7. Mint identifiers (AD-WS-2 / AD-WS-28) and register the
           Work Event Identity in ``Identifier_Registry`` with
           ``kind='immutable_record'`` carrying the Slice 3
           ``resource_kind='work_event_record'`` tag via
           :func:`_record_execution_artifact`.
        8. INSERT the ``Work_Event_Records`` row carrying every
           Requirement 24.2 attribute.
        9. INSERT exactly one ``Relationships`` row with
           ``relationship_type='Relates To'``,
           ``source_kind='work_event_record'``,
           ``target_kind='work_assignment_record'``,
           ``semantic_role='work_event'`` (AD-WS-26).
        10. Append the consequential ``Audit_Records`` row with
            ``action_type='create.work_event'`` and
            ``target_id=work_event_id`` inside the same transaction
            (Requirement 24.6).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_work_assignment_id: Identity of the target Work
                Assignment Record (Requirement 24.2). Must resolve to
                an existing ``Work_Assignment_Records`` row AND the
                persisted ``assignee_party_id`` must equal
                ``recording_party_id`` per Requirement 24.5 /
                AD-WS-29.
            event_kind: Event kind drawn from
                ``{started, progress_note, paused, resumed,
                deliverable_drafted}`` (Requirement 24.2). The
                event-kind state machine in Requirement 24.3 is
                evaluated against the persisted Work Event history
                for the same Work Assignment.
            event_note: Optional event note of 0..4000 characters, or
                ``None`` when omitted (Requirement 24.2). The schema
                column is NULLable; empty strings are accepted and
                persisted verbatim.
            recording_party_id: Identity of the recording Contributor
                Party. Persisted on
                ``Work_Event_Records.recording_party_id``, used as
                the actor for the authorization evaluation, and
                required to match the persisted
                ``Work_Assignment_Records.assignee_party_id`` per
                AD-WS-29.
            authority_basis: Authority basis recorded on the Work
                Event Record. Accepted as either
                :class:`AuthorityBasisRef` or a mapping convertible
                to one; the ``type`` must be drawn from
                ``{role-grant-id, scope-id, delegation-chain-id}``
                per AD-WS-10 / Requirement 24.2.
            applicable_scope: Scope identifier the Work Event applies
                within (Requirement 24.2). Passed as ``target.scope``
                to :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            engine: Required for the deny path's separate-transaction
                Denial Record write so the row survives the caller's
                rollback (Slice 1 Requirement 7.6 / Requirement
                30.6). The same engine is used to open a fresh
                transaction for the authorization evaluation itself.
            correlation_id: Optional correlation identifier shared by
                every audit row written in this operation. A UUIDv7
                is generated when omitted.
            evaluation_at: Optional explicit effective time passed to
                :meth:`AuthorizationService.evaluate` as the ``at``
                parameter. Defaults to the recorded time of this
                transaction.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every prohibited
                planning-attribute and observed-outcome prefix
                (Requirements 33.4 / 34.5). The HTTP layer forwards
                the raw request body here; service-level callers
                (e.g., unit tests) may pass ``None`` to skip the
                screen since the typed kwargs themselves cannot
                carry a prohibited attribute.

        Returns:
            :class:`CreateWorkEventResult` carrying the persisted
            Work Event Identity, every persisted attribute, the
            Relationship Identity, the recorded time, and the
            correlation identifier.

        Raises:
            WorkEventValidationError: A required attribute is missing,
                a Requirement 24.2 range was violated, the authority
                basis is malformed, or the request body carried a
                prohibited planning-attribute or observed-outcome key.
            WorkEventWorkAssignmentNotResolvableError: The target
                Work Assignment Identity did not resolve
                (Requirement 24.4).
            WorkEventStartedAlreadyExistsError: A second ``started``
                event was attempted against a Work Assignment that
                already has a ``started`` event (Requirement 24.3).
            WorkEventNoPriorStartedError: A non-``started`` event was
                attempted against a Work Assignment that has no
                prior ``started`` event (Requirement 24.3).
            WorkEventResumeRequiresPausedError: A ``resumed`` event
                was attempted against a Work Assignment whose most
                recent prior event in ``{paused, resumed}`` is not
                ``paused`` (Requirement 24.3).
            WorkEventAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 24.5). The Denial Record was appended
                successfully in a separate transaction.
            WorkEventAssigneeBindingError: Authorization permitted
                the attempt but the AD-WS-29 second stage rejected
                it because the recording Party is not the named
                assignee on the target Work Assignment.
            WorkEventAuditFailureError: A denial path was taken AND
                the separate-transaction Denial Record append failed
                on every retry. Replaces
                :class:`WorkEventAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed. The surrounding transaction
                MUST be allowed to roll back per Slice 1 Requirement
                13.6 / Requirement 24.6.
            sqlalchemy.exc.IntegrityError: A concurrent ``started``
                attempt won the race; the partial UNIQUE index
                ``idx_work_events_one_started_per_wa`` rejected this
                INSERT. The error indicates the application-level
                state-machine check raced — the surrounding
                transaction rolls back so no Work Event Record
                becomes referenceable.
        """
        # 1. Screen the original request body when the route layer
        # has forwarded it. The typed kwargs themselves cannot carry
        # a prohibited attribute (the signature does not declare any
        # such field), but the HTTP layer's raw body might —
        # Requirements 33.4 and 34.5 demand rejection at the API
        # boundary.
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, ALL_PROHIBITED_PREFIXES
                )
            except ExecutionValidationError as exc:
                raise WorkEventValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 24.2 / 24.4) before any
        # database read or authorization side-effect. Each validator
        # raises :class:`WorkEventValidationError` with a stable
        # ``failed_constraint`` so the HTTP layer (task 15.1) can
        # render structured 400 responses identifying the invalid
        # attribute.
        self._validate_required_strings(
            target_work_assignment_id=target_work_assignment_id,
            recording_party_id=recording_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_event_kind(event_kind)
        self._validate_event_note(event_note)
        normalized_basis = self._validate_authority_basis(authority_basis)

        # 3. Resolve the target Work Assignment Record via a single
        # indexed SELECT on ``Work_Assignment_Records``. The lookup
        # runs on the caller's connection so it participates in the
        # caller's transactional view. Requirement 24.4 rejects the
        # unresolvable case before authorization evaluates the
        # request so the deny path cannot reveal whether the Work
        # Assignment exists. The persisted ``assignee_party_id`` is
        # captured for the AD-WS-29 second stage; the persisted
        # ``applicable_scope`` is captured as the scope on which
        # authorization is evaluated (the request's
        # ``applicable_scope`` is recorded on the Work Event Record
        # itself per Requirement 24.2).
        wa_row = connection.execute(
            text(
                "SELECT work_assignment_id, assignee_party_id, "
                "applicable_scope "
                "FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :work_assignment_id"
            ),
            {"work_assignment_id": target_work_assignment_id},
        ).mappings().first()
        if wa_row is None:
            raise WorkEventWorkAssignmentNotResolvableError(
                target_work_assignment_id=target_work_assignment_id,
            )
        wa_applicable_scope = wa_row["applicable_scope"]

        # 4. Shared clock reading (design §"Cross-Cutting Concerns"
        # — *Transactionality*). The authorization evaluation row,
        # the ``Work_Event_Records`` row, the ``Relationships`` row,
        # and the consequential audit row all share this timestamp;
        # the optional ``evaluation_at`` parameter changes only
        # *when* authority is evaluated *as of*, not the recorded
        # time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 5. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 single-writer accommodation; the deny
        # path opens *another* separate transaction for the Denial
        # Record write, and the caller's transaction stays a reader
        # until step 8 below). The authorization target is the Work
        # Assignment Record itself — Contributor authority is held
        # on the Work Assignment's scope, not on the Work Event
        # which does not yet exist when authorization is evaluated.
        # The scope passed to :meth:`AuthorizationService.evaluate`
        # is the persisted ``applicable_scope`` of the Work
        # Assignment Record, matching the AD-WS-29 contract — a
        # forged request that names a different scope from the
        # Work Assignment cannot inflate its effective authority.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=recording_party_id,
                action=_ACTION_CREATE_WORK_EVENT,
                target=TargetRef(
                    kind=_KIND_WORK_ASSIGNMENT_RECORD,
                    id=target_work_assignment_id,
                    revision_id=None,
                    scope=wa_applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or _REASON_CODE_NO_ROLE_ASSIGNMENT
            self._persist_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                target_work_assignment_id=target_work_assignment_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise WorkEventAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. AD-WS-29 second stage: re-read the persisted Work
        # Assignment Record's ``assignee_party_id`` and require it
        # to equal ``recording_party_id``. The re-read runs against
        # the persisted row (captured in step 3 from
        # ``Work_Assignment_Records``), not the request body, so the
        # bound is forge-proof. On mismatch, append a Denial Record
        # in a separate transaction (so the row survives the
        # caller-side rollback) and raise
        # :class:`WorkEventAssigneeBindingError`; raising the
        # exception causes the caller's surrounding
        # ``engine.begin()`` context manager to roll back without
        # persisting any Work Event Record.
        actual_assignee = wa_row["assignee_party_id"]
        if actual_assignee != recording_party_id:
            self._persist_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                target_work_assignment_id=target_work_assignment_id,
                reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
                correlation_id=correlation,
                recorded_time=evaluate_at,
            )
            raise WorkEventAssigneeBindingError(
                target_work_assignment_id=target_work_assignment_id,
                recording_party_id=recording_party_id,
                actual_assignee_party_id=actual_assignee,
                correlation_id=correlation,
            )

        # 7. Event-kind state machine (Requirement 24.3). The check
        # reads the prior Work Event Records for the target Work
        # Assignment ordered by ``recorded_at DESC`` using the
        # composite index ``idx_work_events_by_wa_recent`` seeded in
        # task 1.2, then evaluates deterministic Python logic. The
        # check runs INSIDE the caller's transaction so concurrent
        # attempts at the same transition fail one of them via the
        # SQLite write lock; the partial UNIQUE index
        # ``idx_work_events_one_started_per_wa`` is the database
        # safety net for the ``started`` race. State-machine
        # violations are pure validation rejections per Requirement
        # 24.4 — no Denial Record is appended.
        self._check_event_kind_state_machine(
            connection,
            target_work_assignment_id=target_work_assignment_id,
            event_kind=event_kind,
        )

        # 8. Mint identifiers (AD-WS-2 / AD-WS-28). Work Event
        # Records are Immutable Records (per
        # [`02-domain-model.md`](../../../documents/02-domain-model.md)
        # §8.2) so the Record identifier is minted via
        # :meth:`IdentityService.new_immutable_record_id`. The
        # Relationship Identity is minted via
        # :meth:`IdentityService.new_relationship_id`.
        work_event_id = str(self.identity_service.new_immutable_record_id())
        relates_to_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Work Event identifier in
        # ``Identifier_Registry``; the digest is the SHA-256 of the
        # canonical JSON payload of the Record so two different Work
        # Event Records never collide on the same digest.
        # ``authority_basis.id`` is normalized to its string form
        # because UUID objects are not natively JSON-serializable.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_work_assignment_id": target_work_assignment_id,
                    "event_kind": event_kind,
                    "event_note": event_note,
                    "recording_party_id": recording_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                    "recorded_at": recorded_at,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 9. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-28 ``resource_kind='work_event_record'``
        # tag. This is the row-level discriminator that keeps the
        # Work Event Identity set inspectably disjoint from every
        # other Slice 1 / Slice 2 / Slice 3 ``resource_kind``
        # (Requirement 22.8). The helper delegates to
        # :meth:`IdentityService.reject_if_duplicate` so the Slice 1
        # identifier-conflict Denial Record pathway fires on any
        # collision; on success the helper INSERTs one row inside
        # the caller's transaction.
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_WORK_EVENT,
            work_event_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=recording_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_WORK_EVENT,
            recorded_time=recorded_time,
        )

        # 10. INSERT the Work Event Record. Every Requirement 24.2
        # attribute lands here. The ``authority_basis`` is
        # destructured into ``authority_basis_type`` and
        # ``authority_basis_id`` columns so the schema-level CHECK on
        # ``authority_basis_type`` enforces the AD-WS-10 enumeration
        # at the database layer as well. The schema-level CHECK on
        # ``event_kind`` enforces the five-value closed enumeration
        # as a second safety net.
        connection.execute(
            text(
                """
                INSERT INTO Work_Event_Records (
                    work_event_id, target_work_assignment_id,
                    event_kind, event_note,
                    recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :work_event_id, :target_work_assignment_id,
                    :event_kind, :event_note,
                    :recording_party_id,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "work_event_id": work_event_id,
                "target_work_assignment_id": target_work_assignment_id,
                "event_kind": event_kind,
                "event_note": event_note,
                "recording_party_id": recording_party_id,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 11. INSERT the ``Relates To`` Relationship row binding the
        # Work Event to the target Work Assignment with
        # ``semantic_role='work_event'`` (AD-WS-26 row 3). The
        # ``semantic_role`` discriminator is the value the
        # Provenance_Navigator backlink algorithm looks for to
        # return the Work Event when given a Work Assignment
        # identity; it must match the AD-WS-26 table exactly.
        # ``source_revision_id`` and ``target_revision_id`` are
        # ``None`` because Work Event Records and Work Assignment
        # Records are both Record-scoped (Requirement 22.2 — no
        # separate Revision).
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
                "relationship_id": relates_to_relationship_id,
                "relationship_type": _RELATIONSHIP_TYPE_RELATES_TO,
                "source_kind": _KIND_WORK_EVENT_RECORD,
                "source_id": work_event_id,
                "source_revision_id": None,
                "target_kind": _KIND_WORK_ASSIGNMENT_RECORD,
                "target_id": target_work_assignment_id,
                "target_revision_id": None,
                "authoring_party_id": recording_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_WORK_EVENT,
            },
        )

        # 12. Append the consequential audit row (Requirement 24.6 /
        # Slice 1 AD-WS-5). Participates in the caller's transaction
        # so a failure here rolls back the registry, the
        # ``Work_Event_Records`` row, and the ``Relationships`` row
        # together. ``target_id`` is the Work Event Record Identity;
        # ``target_revision_id`` is ``None`` because Work Event
        # Records are Record-scoped (Requirement 22.2 — no separate
        # Revision).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=recording_party_id,
            action_type=_ACTION_CREATE_WORK_EVENT,
            target_id=work_event_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateWorkEventResult(
            work_event_id=work_event_id,
            target_work_assignment_id=target_work_assignment_id,
            event_kind=event_kind,
            event_note=event_note,
            recording_party_id=recording_party_id,
            authority_basis=normalized_basis,
            applicable_scope=applicable_scope,
            relates_to_relationship_id=relates_to_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_required_strings(
        *,
        target_work_assignment_id: Any,
        recording_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 24.4: a Work Event creation request that omits
        the target Work Assignment Record Identity, the recording
        Contributor Party Identity, or the applicable scope is
        rejected with no Work Event Record created. Each missing
        attribute surfaces a distinct ``failed_constraint`` so the
        HTTP layer can identify the precise field to the client.
        """
        if not target_work_assignment_id or not isinstance(
            target_work_assignment_id, str
        ):
            raise WorkEventValidationError(
                "target_work_assignment_id is required; Requirement 24.2 "
                "and 24.4 reject Work Events missing the target Work "
                "Assignment Identity.",
                failed_constraint="target_work_assignment_id_missing",
            )
        if not recording_party_id or not isinstance(recording_party_id, str):
            raise WorkEventValidationError(
                "recording_party_id is required; Requirement 24.5 rejects "
                "unauthenticated Work Event creation.",
                failed_constraint="recording_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise WorkEventValidationError(
                "applicable_scope is required; Requirement 24.4 rejects "
                "Work Events missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_event_kind(event_kind: Any) -> None:
        """Reject submissions whose ``event_kind`` is missing or outside
        the closed enumeration.

        Per Requirement 24.2 the ``event_kind`` is drawn from the
        closed set
        ``{started, progress_note, paused, resumed,
        deliverable_drafted}``. Per Requirement 24.4, an out-of-set
        value is rejected with no Work Event Record created. The
        schema-level CHECK on ``Work_Event_Records.event_kind``
        enforces the same set as a defense in depth.
        """
        if event_kind is None or not isinstance(event_kind, str) or event_kind == "":
            raise WorkEventValidationError(
                "event_kind is required and must be one of "
                f"{sorted(_VALID_EVENT_KINDS)} per Requirement 24.2.",
                failed_constraint="event_kind_missing",
            )
        if event_kind not in _VALID_EVENT_KINDS:
            raise WorkEventValidationError(
                f"event_kind {event_kind!r} is not in the closed "
                f"enumeration {sorted(_VALID_EVENT_KINDS)} required by "
                "Requirement 24.2.",
                failed_constraint="event_kind_out_of_set",
            )

    @staticmethod
    def _validate_event_note(event_note: Any) -> None:
        """Reject ``event_note`` values outside the Requirement 24.2 range.

        Per Requirement 24.2 the ``event_note`` is 0..4000 characters
        and optional. ``None`` is accepted (the column is NULLable)
        and persisted as SQL ``NULL``; the empty string is also
        accepted (length 0 satisfies the 0 lower bound) and persisted
        verbatim. The schema-level CHECK constraint
        ``length(event_note) BETWEEN 0 AND 4000`` enforces the same
        range at the database layer.
        """
        if event_note is None:
            return
        if not isinstance(event_note, str):
            raise WorkEventValidationError(
                "event_note must be a str or None; received "
                f"{type(event_note).__name__}.",
                failed_constraint="event_note_invalid_type",
            )
        if len(event_note) > _EVENT_NOTE_MAX_CHARS:
            raise WorkEventValidationError(
                f"event_note length {len(event_note)} exceeds the "
                f"{_EVENT_NOTE_MAX_CHARS}-character limit imposed by "
                "Requirement 24.2.",
                failed_constraint="event_note_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 24.2 / AD-WS-10: the authority basis ``type``
        is drawn from ``{role-grant-id, scope-id, delegation-chain-id}``.
        The Python-typed signature already constrains callers passing
        :class:`AuthorityBasisRef` directly; the HTTP layer may pass a
        dict if it has not yet bound the request to the typed model,
        so this validator coerces both shapes (mirroring the Slice 2
        plan_reviews / plan_approvals validator and the Slice 3
        work_assignments validator).
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise WorkEventValidationError(
                "authority_basis is required and must be an "
                "AuthorityBasisRef (or a mapping convertible to one); "
                f"received {type(authority_basis).__name__}.",
                failed_constraint="authority_basis_missing",
            )

        basis_type = authority_basis.get("type")
        basis_id = authority_basis.get("id")

        if (
            basis_type is None
            or not isinstance(basis_type, str)
            or basis_type == ""
        ):
            raise WorkEventValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise WorkEventValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise WorkEventValidationError(
                "authority_basis.id is required per Requirement 24.2.",
                failed_constraint="authority_basis_id_missing",
            )

        # Delegate canonical-form validation (UUID shape) to Pydantic
        # by constructing the typed model.
        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise WorkEventValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    # -- state machine -----------------------------------------------------

    @staticmethod
    def _check_event_kind_state_machine(
        connection: Connection,
        *,
        target_work_assignment_id: str,
        event_kind: str,
    ) -> None:
        """Enforce Requirement 24.3 against the persisted Work Event history.

        The check reads prior ``Work_Event_Records`` rows for the
        target Work Assignment ordered by ``recorded_at DESC`` using
        the composite index ``idx_work_events_by_wa_recent`` seeded
        in task 1.2. The application then evaluates deterministic
        Python logic over the returned ordered list:

        - ``started``: rejected if any prior ``Work_Event_Records``
          row with ``event_kind='started'`` exists for the same
          ``target_work_assignment_id``. The partial UNIQUE index
          ``idx_work_events_one_started_per_wa`` is the database-layer
          safety net for the concurrent-attempt race.
        - ``progress_note`` / ``paused`` / ``deliverable_drafted``:
          rejected if no prior ``started`` exists for the same Work
          Assignment.
        - ``resumed``: rejected if no prior ``started`` exists OR if
          the most recent prior event in ``{paused, resumed}`` is
          not ``paused`` (resume requires a pause that has not been
          resumed). Intervening ``progress_note`` /
          ``deliverable_drafted`` events between a ``paused`` and
          its matching ``resumed`` do not invalidate the resume —
          only ``paused`` / ``resumed`` events are considered for
          the pause-cycle bookkeeping.

        The check runs INSIDE the caller's transaction so the
        application-level pass + the eventual INSERT see a consistent
        view of the Work Event history. Concurrent attempts at the
        same transition will both observe the same prior history and
        could both pass the application check; the SQLite write lock
        then serializes the INSERTs so only one transaction commits.
        For the ``started`` race the partial UNIQUE index causes the
        second INSERT to fail with an :class:`IntegrityError`. For
        the ``resumed`` race there is no UNIQUE index — the second
        committed ``resumed`` is permitted by the state machine as a
        legitimate "resume after a later pause" sequence in the
        general case; the test for two concurrent ``resumed`` events
        is therefore that they appear in some serialized order and
        Requirement 24.3 is satisfied by the historical sequence,
        not by the absence of a second event.
        """
        # Indexed read by (target_work_assignment_id, recorded_at DESC).
        # Returns only ``event_kind`` (the index is covering for this
        # column projection per task 1.2's schema seeding) so the
        # query is O(rows_for_this_WA) and does not page through
        # event payload.
        rows = connection.execute(
            text(
                """
                SELECT event_kind FROM Work_Event_Records
                WHERE target_work_assignment_id = :work_assignment_id
                ORDER BY recorded_at DESC
                """
            ),
            {"work_assignment_id": target_work_assignment_id},
        ).all()
        prior_kinds: tuple[str, ...] = tuple(row[0] for row in rows)
        has_started = "started" in prior_kinds

        if event_kind == "started":
            if has_started:
                raise WorkEventStartedAlreadyExistsError(
                    target_work_assignment_id=target_work_assignment_id,
                )
            return

        # All non-``started`` kinds require a prior ``started``.
        if not has_started:
            raise WorkEventNoPriorStartedError(
                target_work_assignment_id=target_work_assignment_id,
                event_kind=event_kind,
            )

        if event_kind == "resumed":
            # Find the most recent prior event in {paused, resumed};
            # if there is none, or if there is one and it is
            # ``resumed`` (i.e., the pause has already been resumed),
            # reject. Intervening events outside the set are
            # ignored — they do not change which pause is the most
            # recent.
            most_recent: Optional[str] = None
            for kind in prior_kinds:
                if kind in _PAUSE_CYCLE_KINDS:
                    most_recent = kind
                    break
            if most_recent != "paused":
                raise WorkEventResumeRequiresPausedError(
                    target_work_assignment_id=target_work_assignment_id,
                    most_recent_pause_cycle_kind=most_recent,
                )
            return

        # ``progress_note`` / ``paused`` / ``deliverable_drafted``
        # only require a prior ``started`` — already checked above.
        return

    # -- denial side-channel ----------------------------------------------

    def _persist_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_work_assignment_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Work Event attempt.

        Implements the AD-WS-9 / Slice 1 Requirement 7.6 / Slice 3
        Requirement 30.6 retry contract verbatim (mirroring
        :meth:`walking_slice.execution.work_assignments.WorkAssignmentService._persist_work_assignment_denial`
        and
        :meth:`walking_slice.deliverables.repository.DeliverableRepositoryService._persist_denial`):
        each attempt opens a *new* :meth:`Engine.begin` transaction
        (so a previous attempt's rollback does not poison this one),
        tries :meth:`AuditLog.append_denial`, and either returns on
        success or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails,
          :class:`WorkEventAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_work_event` raises
        :class:`WorkEventAuthorizationError` /
        :class:`WorkEventAssigneeBindingError` (or this method raises
        :class:`WorkEventAuditFailureError`). The Denial Record must
        therefore live outside that scope to survive (AD-WS-9 /
        Requirement 30.6).

        ``target_id`` on the Denial Record points at the target Work
        Assignment Identity rather than at a (non-existent) Work
        Event Identity, because the Work Event does not exist at the
        time the denial is recorded — the deny path explicitly
        refuses to mint a Record Identity for an unauthorized attempt
        (Requirement 24.5 / Requirement 30.5 — no information leakage
        about the existence of restricted Resources).

        Both :class:`AuditAppendError` and :class:`SQLAlchemyError`
        are treated as retryable failures: the former wraps the
        latter for callers who use :class:`AuditLog`, but a
        transaction-management failure (e.g. ``engine.begin()``
        raising) surfaces as a bare :class:`SQLAlchemyError`.
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=_ACTION_CREATE_WORK_EVENT,
                        target_id=target_work_assignment_id,
                        target_revision_id=None,
                        reason_code=reason_code,
                        correlation_id=correlation_id,
                        recorded_time=recorded_time,
                    )
                return  # success — Denial Record committed in its own tx
            except (AuditAppendError, SQLAlchemyError) as exc:
                last_error = exc
                if attempt_index < len(_DENIAL_AUDIT_BACKOFFS_SECONDS):
                    self.denial_audit_sleep(
                        _DENIAL_AUDIT_BACKOFFS_SECONDS[attempt_index]
                    )

        raise WorkEventAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this module
# does not import private names from sibling services. The functions
# are intentionally identical to their Slice 2 / Slice 3 siblings:
# correlation identifiers are non-domain values and the digest is
# opaque to :class:`Identifier_Registry`.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Work Event
    creation. They are not registered with :class:`IdentityService`
    because they do not name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Work Event
    Identity in ``Identifier_Registry``. Work Event Records are
    Record-scoped (Requirement 22.2 — no separate Revision) so this
    digest is bound exactly once per Work Event creation.
    """
    return hashlib.sha256(content).hexdigest()
