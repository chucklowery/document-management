"""Execution_Service.TimeEntries — immutable Time Entry Records that record
reported Contributor effort against a single Work Assignment Record.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Execution_Service.TimeEntries" — public dataclass surface, authority
  string (``create.time_entry`` → ``contribute`` AND assignee binding per
  AD-WS-24 and AD-WS-29), AD-WS-9 separate-transaction Denial Record on
  deny with the Requirement 30.6 three-retry contract, effort-quantity
  validation (ISO-decimal regex ``^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$`` AND
  numeric range ``0.00..24.00`` AND Decimal normalization to
  two-decimal-place form before persistence), effort-period ordering
  (``effort_period_start <= effort_period_end <= recorded_at``), and the
  Relationship-row contract (``Relates To`` to the Work Assignment Record
  with ``semantic_role = 'time_entry'`` per AD-WS-26).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with
  ``kind = 'immutable_record'`` and
  ``resource_kind = 'time_entry_record'`` per AD-WS-28); Authorization
  (the action string ``create.time_entry`` maps to the ``contribute``
  authority per AD-WS-24; the deny path uses the Slice 1
  separate-transaction Denial-Record pattern reproduced verbatim from
  :class:`walking_slice.deliverables.repository.DeliverableRepositoryService`
  and :class:`walking_slice.execution.work_assignments.WorkAssignmentService`).
- AD-WS-24 — additive ``contribute`` mapping for ``create.time_entry``.
- AD-WS-26 — ``Time Entry Record`` → ``Work Assignment Record`` via
  ``Relates To`` Relationship with ``semantic_role = 'time_entry'``.
- AD-WS-27 — ``Time_Entry_Records`` is append-only; the
  ``Work_Assignment_Records`` row addressed by the Time Entry must
  remain byte-equivalent throughout this transaction.
- AD-WS-28 — additive ``Identifier_Registry.resource_kind`` value
  ``'time_entry_record'`` populated through
  :func:`walking_slice.execution._helpers._record_execution_artifact`.
- AD-WS-29 — two-stage authority evaluation for Contributor writes: the
  service first calls :meth:`AuthorizationService.evaluate` with the
  Work Assignment Record as the target, and then on a ``permit``
  outcome re-reads the persisted ``Work_Assignment_Records`` row
  inside the caller's transaction and requires
  ``assignee_party_id == recording_party_id``. Both stages must pass;
  a failure of either stage produces an AD-WS-9-conformant denial
  response (Slice 1 Requirement 7.2 ``reason_code = 'no-role-assignment'``
  for the assignee-binding failure).

Task scope (task 7.1)
=====================

This module implements :meth:`TimeEntryService.create_time_entry`:

1. Validate request inputs per Requirement 25.2 and 25.3:
   ``effort_hours`` is a non-negative :class:`~decimal.Decimal` with
   at most two fractional digits and at most 24.00 hours;
   ``effort_period_start`` and ``effort_period_end`` are UTC datetimes
   with ``effort_period_start <= effort_period_end``;
   ``effort_period_end <= recorded_at``; ``target_work_assignment_id``,
   ``recording_party_id``, ``applicable_scope``, and
   ``authority_basis`` are all present.
2. Defensively reject any prohibited planning-attribute or
   observed-outcome key in the original request body via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
3. Resolve the target Work Assignment via a single indexed SELECT on
   ``Work_Assignment_Records``. Reject when unresolvable
   (Requirement 25.3). The lookup runs before authorization
   evaluation so the deny path never reveals whether the Work
   Assignment exists for an unauthorized caller.
4. Evaluate ``Authorization_Service.evaluate(party=recording_party_id,
   action="create.time_entry", target=work_assignment_ref, at=now())``
   on a separate transaction (Slice 1 single-writer accommodation).
   On ``deny``, persist a Denial Record from another separate
   transaction with the Requirement 30.6 / Slice 1 Requirement 7.6
   three-retry exponential-backoff pattern and raise
   :class:`TimeEntryAuthorizationError`.
5. On a ``permit`` outcome, perform the AD-WS-29 second stage: the
   ``Work_Assignment_Records`` row was already loaded in step 3; its
   ``assignee_party_id`` must equal the supplied
   ``recording_party_id``. On mismatch, append a Denial Record with
   ``reason_code = 'no-role-assignment'`` in a separate transaction
   and raise :class:`TimeEntryAssigneeBindingError` so the caller's
   surrounding transaction rolls back without persisting any row.
6. Normalize the Decimal ``effort_hours`` to two-decimal-place form
   (Requirement 25.2) before any database write.
7. Mint the Time Entry Record Identity (UUIDv7) and the Relationship
   Identity; register the Time Entry Identity in
   ``Identifier_Registry`` with ``kind='immutable_record'`` and
   ``resource_kind='time_entry_record'`` via
   :func:`_record_execution_artifact`.
8. INSERT the ``Time_Entry_Records`` row carrying every Requirement
   25.2 attribute.
9. INSERT exactly one ``Relationships`` row with
   ``relationship_type='Relates To'``,
   ``source_kind='time_entry_record'``,
   ``target_kind='work_assignment_record'``, and
   ``semantic_role='time_entry'`` (AD-WS-26).
10. Append the consequential ``Audit_Records`` row with
    ``action_type='create.time_entry'`` and
    ``target_id=time_entry_id`` inside the same transaction
    (Requirement 25.5 / Slice 1 AD-WS-5).

Requirements satisfied
======================

    25.1 — authorized Time Entry creation produces exactly one
           immutable Time Entry Record within nominal latency.
    25.2 — every Time Entry Record records the target Work Assignment
           Record Identity, the reported effort quantity (non-negative
           Decimal with at most two fractional digits, at most
           24.00 hours), the effort-period start and end times in UTC
           with millisecond precision satisfying
           ``start <= end <= recorded_at``, the recording Contributor
           Party Identity, the authority basis (drawn from the
           AD-WS-10 set), the applicable scope, the recorded time, and
           the ``Relates To`` Relationship with
           ``semantic_role='time_entry'`` to the target Work
           Assignment Record.
    25.3 — unresolvable target Work Assignment Record, non-assignee
           requesting Party, malformed or out-of-range effort
           quantity, malformed effort period, missing effort quantity,
           missing effort period, and missing applicable scope are
           rejected with no Time Entry Record created.
    25.4 — unauthorized requests are denied via
           :class:`AuthorizationService`; the Execution_Service
           declines to create any Time Entry Record and the Audit_Log
           appends a Denial Record conforming to AD-WS-9.
    25.5 — the Audit_Log appends an immutable consequential audit row
           identifying the Time Entry Record Identity, target Work
           Assignment Record Identity, reported effort quantity,
           recording Contributor Party Identity, and recorded time
           within the same transaction as the Time Entry Record
           creation.
    25.6 — the append-only schema triggers (created in task 1.2)
           reject every UPDATE / DELETE attempt on
           ``Time_Entry_Records`` after this transaction commits.
    25.7 — Audit_Log append failure rolls back the Time Entry Record
           creation and surfaces a structured error.
    32.7 — ``create.time_entry`` requires the ``contribute`` authority
           AND assignee binding on the originating Work Assignment
           (AD-WS-24, AD-WS-29).
    41.1 — every consequential write is atomic with its consequential
           audit row.
    41.2 — every consequential write checks authority before
           persisting any domain row.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid_utils
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
    "CreateTimeEntryResult",
    "TimeEntryAssigneeBindingError",
    "TimeEntryAuditFailureError",
    "TimeEntryAuthorizationError",
    "TimeEntryService",
    "TimeEntryValidationError",
    "TimeEntryWorkAssignmentNotResolvableError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship kind / type strings, and registry kind
# strings are pulled out as module-level ``Final`` constants so the
# names downstream property tests look for in ``Audit_Records.action_type``
# and ``Relationships`` are textually stable. The strings stay aligned
# with the :mod:`walking_slice.execution._persistence` schema, the
# AD-WS-24 authority mapping in :mod:`walking_slice.authorization`, and
# the AD-WS-26 Relationship-Type / semantic-role table.
# ---------------------------------------------------------------------------


# ``create.time_entry`` maps to the ``contribute`` authority per
# AD-WS-24. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 25.5) and on the
# separate-transaction Denial Record appended by
# :meth:`TimeEntryService._persist_time_entry_denial` so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_TIME_ENTRY: Final[str] = "create.time_entry"

# Relationship Type strings written to the ``Relates To`` row that
# binds the Time Entry Record to its target Work Assignment Record,
# per AD-WS-26.
_RELATIONSHIP_TYPE_RELATES_TO: Final[str] = "Relates To"

# ``Relationships.source_kind`` / ``target_kind`` / ``semantic_role``
# strings per AD-WS-26. The Time Entry Record binding carries
# ``semantic_role='time_entry'``.
_KIND_TIME_ENTRY_RECORD: Final[str] = "time_entry_record"
_KIND_WORK_ASSIGNMENT_RECORD: Final[str] = "work_assignment_record"
_SEMANTIC_ROLE_TIME_ENTRY: Final[str] = "time_entry"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Execution_Service ``resource_kind`` tag (Slice 3 additive
# enumeration per AD-WS-28). Time Entry Records are Immutable
# Records (per ``02-domain-model.md`` §8.2 Execution Record) so the
# registry kind is ``'immutable_record'``; the ``resource_kind``
# value is ``'time_entry_record'`` and is the row-level
# discriminator that keeps the Time Entry Identity set inspectably
# disjoint from every other Slice 1 / Slice 2 / Slice 3
# ``resource_kind`` (Requirement 22.8).
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_TIME_ENTRY: Final[str] = "time_entry_record"

# Authority-basis ``type`` enumeration per AD-WS-10. Mirrors the
# Slice 2 ``Plan_Review_Revisions`` / ``Plan_Approval_Records`` and
# Slice 3 ``Time_Entry_Records.authority_basis_type`` CHECK
# constraint values; centralizing the tuple here lets the validator
# reject malformed requests structurally before they touch SQL.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Validation bounds for ``effort_hours`` per Requirement 25.2.
# ``effort_hours`` is a non-negative Decimal with at most two
# fractional digits and at most 24.00 hours per single Time Entry
# Record. The schema-level CHECK on
# ``Time_Entry_Records.effort_hours`` enforces both the ISO-decimal
# regex (via ``GLOB``) and the numeric range; surfacing the
# constraint at the application layer makes the rejection
# deterministic and yields a precise ``failed_constraint`` for the
# HTTP layer rather than a raw SQL constraint violation.
_EFFORT_HOURS_MIN: Final[Decimal] = Decimal("0.00")
_EFFORT_HOURS_MAX: Final[Decimal] = Decimal("24.00")

# ISO-decimal regex from design §"Execution_Service.TimeEntries":
# ``^(0|[1-9][0-9]?)(\.[0-9]{1,2})?$``. The regex matches the canonical
# textual form of ``effort_hours`` before normalization (e.g. ``"0"``,
# ``"0.5"``, ``"12.34"``, ``"24"``) so callers can submit unnormalized
# strings as well; the service normalizes the value to two-decimal-place
# form before persistence. The compiled pattern is shared across every
# call to keep the per-write cost negligible.
_EFFORT_HOURS_REGEX: Final[re.Pattern[str]] = re.compile(
    r"^(0|[1-9][0-9]?)(\.[0-9]{1,2})?$"
)

# Denial-reason code used when authorization permits the action but
# the AD-WS-29 second stage rejects it because the recording Party
# is not the named assignee on the originating Work Assignment.
# Slice 1 Requirement 7.2 enumerates this value as
# ``'no-role-assignment'``; the Slice 3 design §"Cross-Cutting
# Concerns" (*Authorization*) explicitly directs that the
# missing-assignee case be treated as "no effective Contributor role
# on this specific Work Assignment".
_REASON_CODE_NO_ROLE_ASSIGNMENT: Final[str] = "no-role-assignment"

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (mirrors Slice 1 Requirement 7.6 / Slice 3
# Requirement 30.6). Three retries after the initial attempt for a
# total of four attempts. The sequence is byte-equivalent to every
# sibling Slice 1 / Slice 2 / Slice 3 module so every endpoint
# presents identical denial-side timing (which the
# indistinguishable-denial properties rely on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class TimeEntryValidationError(ValueError):
    """Raised when a Time Entry submission fails Requirement 25.2 / 25.3 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a
    stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_work_assignment_id_missing"``,
            ``"recording_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"effort_hours_missing"``,
            ``"effort_hours_invalid_type"``,
            ``"effort_hours_not_finite"``,
            ``"effort_hours_format"`` (failed the ISO-decimal regex),
            ``"effort_hours_negative"``,
            ``"effort_hours_too_large"`` (> 24.00),
            ``"effort_period_start_missing"``,
            ``"effort_period_start_invalid_type"``,
            ``"effort_period_end_missing"``,
            ``"effort_period_end_invalid_type"``,
            ``"effort_period_start_after_end"``,
            ``"effort_period_end_after_recorded_at"``,
            ``"authority_basis_missing"``,
            ``"authority_basis_type_missing"``,
            ``"authority_basis_type_out_of_set"``,
            ``"authority_basis_id_missing"``,
            ``"prohibited_attribute"``.
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


class TimeEntryWorkAssignmentNotResolvableError(LookupError):
    """Raised when the target Work Assignment Identity does not resolve.

    Requirement 25.3 requires the target Work Assignment Record
    Identity to resolve to an existing Work Assignment. The check
    runs before authorization evaluation so the deny path never
    reveals whether a Work Assignment exists for an unauthorized
    caller.

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
            f"Target Work Assignment {target_work_assignment_id!r} did "
            f"not resolve to an existing Work Assignment "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_work_assignment_id = target_work_assignment_id
        self.failed_constraint = failed_constraint


class TimeEntryAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Time Entry attempt.

    Carries only ``reason_code`` and ``correlation_id`` — the
    indistinguishable-denial invariant forbids leaking authorized
    Party identities, target existence, or role-assignment details
    beyond the requesting Party's view authority through the denial
    response (Requirement 25.4 / AD-WS-9).

    The same exception type is raised when the AD-WS-29 second stage
    fails (the recording Party is not the named assignee on the
    originating Work Assignment); in that case ``reason_code`` is
    fixed to ``'no-role-assignment'`` and the
    :class:`TimeEntryAssigneeBindingError` subclass is used so tests
    that need to discriminate the assignee-binding path can do so.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Time Entry creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class TimeEntryAssigneeBindingError(TimeEntryAuthorizationError):
    """Specialised :class:`TimeEntryAuthorizationError` for AD-WS-29
    assignee-binding failures.

    Subclass of :class:`TimeEntryAuthorizationError` so callers that
    catch the broader denial path continue to work, while tests that
    need to assert specifically on the AD-WS-29 path can catch this
    narrower type. The denial response shape is identical:
    ``{reason_code='no-role-assignment', correlation_id}``.

    Attributes:
        target_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        recording_party_id: The Party Identity the caller submitted
            as the recording Contributor.
        actual_assignee_party_id: The Party Identity actually
            persisted on
            ``Work_Assignment_Records.assignee_party_id``.
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


class TimeEntryAuditFailureError(RuntimeError):
    """Raised when every retry of the Denial Record append fails.

    On total audit-append failure the exception is raised *in place
    of* :class:`TimeEntryAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's
    transaction still rolls back so no ``Time_Entry_Records`` row,
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
            f"Denial Record append for a denied Time Entry failed after "
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
class CreateTimeEntryResult:
    """Result of :meth:`TimeEntryService.create_time_entry`.

    Returned so callers (the HTTP layer, tests, the
    Provenance_Navigator that traverses the Execution Provenance
    Chain, and the execution-status Projection) can correlate the
    created Time Entry Record with its Relationship row and its
    consequential audit row in one round-trip.

    Attributes:
        time_entry_id: The Time Entry Record Identity (UUIDv7).
        target_work_assignment_id: The target Work Assignment Record
            Identity; copied byte-equivalent from the request input.
        effort_hours: The normalized two-decimal-place Decimal value
            of reported effort; matches the persisted
            ``Time_Entry_Records.effort_hours`` TEXT column.
        effort_period_start: UTC ISO-8601 millisecond-precision
            string of the effort-period start time. Stored verbatim
            in the ``Time_Entry_Records.effort_period_start`` column.
        effort_period_end: UTC ISO-8601 millisecond-precision string
            of the effort-period end time. Stored verbatim in the
            ``Time_Entry_Records.effort_period_end`` column.
        recording_party_id: The recording Contributor Party
            Identity; copied byte-equivalent from the request input.
        authority_basis: The validated :class:`AuthorityBasisRef`
            recorded on the Time Entry Record.
        applicable_scope: Scope identifier the Time Entry applies
            within.
        relates_to_relationship_id: Identity of the single
            ``Relates To`` ``Relationships`` row inserted alongside
            the Time Entry binding it to the target Work Assignment
            Record with ``semantic_role='time_entry'``.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Time_Entry_Records`` row, the
            ``Relationships`` row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    time_entry_id: str
    target_work_assignment_id: str
    effort_hours: Decimal
    effort_period_start: str
    effort_period_end: str
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
class TimeEntryService:
    """Persist immutable Time Entry Records and their ``Relates To``
    Relationship per AD-WS-26.

    Like its Slice 3 siblings
    :class:`walking_slice.execution.work_assignments.WorkAssignmentService`
    and
    :class:`walking_slice.deliverables.repository.DeliverableRepositoryService`,
    this service is connection-scoped at call time:
    :meth:`create_time_entry` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (Slice 1 AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.TimeEntries" declares it
    ``@dataclass(frozen=True)`` — Slice 3 service instances follow
    the Slice 2 convention of being immutable container objects that
    bundle their collaborators.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Time_Entry_Records``, ``Relationships``, and
            ``Audit_Records`` rows. The clock is consulted exactly
            once per write so every artifact of the transaction
            shares one timestamp.
        identity_service: Generates the Time Entry Record Identity
            (UUIDv7) and the Relationship Identity, plus drives the
            ``Identifier_Registry`` binding via
            :func:`_record_execution_artifact` (the Time Entry
            binding carries the Slice 3
            ``resource_kind='time_entry_record'`` tag per AD-WS-28).
        audit_log: Appends the consequential audit row
            (Requirement 25.5) inside the caller's transaction; the
            denial-side audit append (separate transaction) is
            driven by :meth:`_persist_time_entry_denial`.
        authorization_service: Evaluates ``create.time_entry``
            authority per AD-WS-24 / Requirement 25.4; the deny path
            is the Slice 1 separate-transaction Denial-Record
            pattern with three retries per Requirement 30.6.
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

    def create_time_entry(
        self,
        connection: Connection,
        *,
        target_work_assignment_id: str,
        effort_hours: Any,
        effort_period_start: datetime,
        effort_period_end: datetime,
        recording_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateTimeEntryResult:
        """Create an immutable Time Entry Record and its ``Relates To``
        Relationship per AD-WS-26.

        Per Requirements 25.1 through 25.7, AD-WS-9 (indistinguishable
        denial), AD-WS-24 (``create.time_entry`` → ``contribute``),
        AD-WS-26 (Relationship-Type / semantic-role table), AD-WS-27
        (append-only Slice 3 tables), AD-WS-28 (additive
        ``resource_kind`` values), and AD-WS-29 (two-stage assignee
        binding):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
        2. Input validation (Requirement 25.2 / 25.3) — every range,
           required-attribute, regex, and authority-basis-enumeration
           check runs before any database read so a malformed
           request never touches identity service, the
           ``Work_Assignment_Records`` lookup, or the authorization
           service.
        3. Resolve the target Work Assignment via a single indexed
           SELECT on ``Work_Assignment_Records``. Reject when
           unresolvable
           (:class:`TimeEntryWorkAssignmentNotResolvableError`) per
           Requirement 25.3. The lookup runs before authorization
           evaluation so the deny path never reveals whether the
           Work Assignment exists for an unauthorized caller.
        4. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 30.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`TimeEntryAuditFailureError` in place of
           :class:`TimeEntryAuthorizationError`.
        5. On ``permit``, perform the AD-WS-29 second stage: the
           ``Work_Assignment_Records`` row was already loaded in
           step 3; require ``assignee_party_id == recording_party_id``.
           On mismatch, append a Denial Record with
           ``reason_code='no-role-assignment'`` in a separate
           transaction and raise
           :class:`TimeEntryAssigneeBindingError` so the caller's
           surrounding ``engine.begin()`` context manager rolls back
           without persisting any row.
        6. Normalize the Decimal ``effort_hours`` to two-decimal-place
           form (Requirement 25.2) — e.g. ``Decimal("12")`` is
           rendered as ``"12.00"`` and ``Decimal("12.5")`` is
           rendered as ``"12.50"``. The normalized string matches
           the SQLite ``GLOB`` patterns enforced by the
           ``Time_Entry_Records.effort_hours`` CHECK constraint so
           the row insert never fails on a well-validated input.
        7. Validate the runtime ordering invariant
           ``effort_period_end <= recorded_at``. Steps 1–6 cannot
           know the recorded time (the clock has not yet been
           consulted), so this check runs immediately after the
           clock read.
        8. Mint the Time Entry Record Identity and Relationship
           Identity and register the Time Entry Identity in
           ``Identifier_Registry`` (kind ``'immutable_record'``,
           carrying the Slice 3
           ``resource_kind='time_entry_record'`` tag per AD-WS-28)
           via :func:`_record_execution_artifact`.
        9. INSERT the ``Time_Entry_Records`` row carrying every
           Requirement 25.2 attribute.
        10. INSERT exactly one ``Relationships`` row with
            ``relationship_type='Relates To'``,
            ``source_kind='time_entry_record'``,
            ``target_kind='work_assignment_record'``, and
            ``semantic_role='time_entry'``.
        11. Append the consequential ``Audit_Records`` row with
            ``action_type='create.time_entry'`` and
            ``target_id=time_entry_id`` inside the same transaction
            (Requirement 25.5).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_work_assignment_id: Identity of the target Work
                Assignment Record. Must resolve to an existing
                ``Work_Assignment_Records`` row whose
                ``assignee_party_id`` equals ``recording_party_id``.
            effort_hours: Reported effort quantity as
                :class:`~decimal.Decimal`. The service accepts any
                value :class:`Decimal` accepts as a constructor
                argument (including ``str`` and ``int``); the
                normalized two-decimal-place form must satisfy the
                ISO-decimal regex and lie in ``[0.00, 24.00]``.
            effort_period_start: UTC datetime of the effort-period
                start. Naive datetimes are interpreted as UTC.
            effort_period_end: UTC datetime of the effort-period end.
                Must satisfy
                ``effort_period_start <= effort_period_end``.
            recording_party_id: Identity of the recording
                Contributor Party. Must equal the assignee Party on
                the target Work Assignment (AD-WS-29).
            authority_basis: Authority basis recorded on the Time
                Entry Record. Accepted as either
                :class:`AuthorityBasisRef` or a mapping convertible
                to one; the ``type`` must be drawn from
                ``{role-grant-id, scope-id, delegation-chain-id}``.
            applicable_scope: Scope identifier the Time Entry
                applies within. Passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback.
            correlation_id: Optional correlation identifier shared
                by every audit row written in this operation. A
                UUIDv7 is generated when omitted.
            evaluation_at: Optional explicit effective time passed
                to :meth:`AuthorizationService.evaluate`. Defaults to
                the recorded time of this transaction.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every prohibited
                planning-attribute and observed-outcome prefix.

        Returns:
            :class:`CreateTimeEntryResult` carrying the persisted
            Time Entry Identity, the normalized effort, the formatted
            period times, the Relationship Identity, the recorded
            time, and the correlation identifier.

        Raises:
            TimeEntryValidationError: A required attribute is
                missing, an effort or period range was violated, the
                authority basis is malformed, or the request body
                carried a prohibited planning-attribute or
                observed-outcome key.
            TimeEntryWorkAssignmentNotResolvableError: The target
                Work Assignment Identity did not resolve to an
                existing Work Assignment (Requirement 25.3).
            TimeEntryAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 25.4). The Denial Record was appended
                successfully in a separate transaction.
            TimeEntryAssigneeBindingError: Authorization permitted
                the attempt but the AD-WS-29 second stage rejected
                it because the recording Party is not the named
                assignee on the originating Work Assignment.
                Subclass of :class:`TimeEntryAuthorizationError`.
            TimeEntryAuditFailureError: Authorization denied the
                attempt *and* the separate-transaction Denial Record
                append failed on every retry. Replaces
                :class:`TimeEntryAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``recording_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 25.7.
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
                raise TimeEntryValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 25.2 / 25.3) before any
        # database read or authorization side-effect. Each validator
        # raises :class:`TimeEntryValidationError` with a stable
        # ``failed_constraint`` so the HTTP layer can render
        # structured 400 responses identifying the invalid attribute.
        self._validate_required_strings(
            target_work_assignment_id=target_work_assignment_id,
            recording_party_id=recording_party_id,
            applicable_scope=applicable_scope,
        )
        normalized_effort_text = self._validate_and_normalize_effort_hours(
            effort_hours
        )
        normalized_effort = Decimal(normalized_effort_text)
        period_start_str, period_end_str = self._validate_effort_period(
            effort_period_start=effort_period_start,
            effort_period_end=effort_period_end,
        )
        normalized_basis = self._validate_authority_basis(authority_basis)

        # 3. Resolve the target Work Assignment via a single indexed
        # SELECT on ``Work_Assignment_Records``. The lookup runs on
        # the caller's connection so it participates in the caller's
        # transactional view. Requirement 25.3 rejects the
        # unresolvable case before authorization evaluates the
        # request so the deny path cannot reveal whether the Work
        # Assignment exists. The same row's ``assignee_party_id`` is
        # read here so the AD-WS-29 second stage in step 5 does not
        # require a second SELECT.
        wa_row = connection.execute(
            text(
                "SELECT work_assignment_id, assignee_party_id, applicable_scope "
                "FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :work_assignment_id"
            ),
            {"work_assignment_id": target_work_assignment_id},
        ).mappings().first()
        if wa_row is None:
            raise TimeEntryWorkAssignmentNotResolvableError(
                target_work_assignment_id=target_work_assignment_id,
            )

        # 4. Capture one recorded time for the entire write so the
        # Time Entry row, the Relationship row, and the consequential
        # audit row share a single timestamp (design §"Cross-Cutting
        # Concerns" — Transactionality).
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 5. Validate the runtime ordering invariant
        # ``effort_period_end <= recorded_at`` (Requirement 25.3).
        # Steps 1–4 cannot perform this check because the clock has
        # not yet been consulted; the schema-level CHECK constraint
        # provides defense-in-depth at the database layer.
        if period_end_str > recorded_at:
            raise TimeEntryValidationError(
                f"effort_period_end {period_end_str!r} is later than "
                f"recorded_at {recorded_at!r}; Requirement 25.3 requires "
                "the effort-period end time to be at or before the "
                "recorded time.",
                failed_constraint="effort_period_end_after_recorded_at",
            )

        # 6. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 single-writer accommodation; the
        # deny path opens *another* separate transaction for the
        # Denial Record write, and the caller's transaction stays a
        # reader until step 7 below). The authorization target is
        # the Work Assignment Record — ``create.time_entry``
        # authority is scoped against the Work Assignment the Time
        # Entry is being recorded against.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=recording_party_id,
                action=_ACTION_CREATE_TIME_ENTRY,
                target=TargetRef(
                    kind=_KIND_WORK_ASSIGNMENT_RECORD,
                    id=target_work_assignment_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = (
                decision_outcome.reason_code
                or _REASON_CODE_NO_ROLE_ASSIGNMENT
            )
            self._persist_time_entry_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                target_work_assignment_id=target_work_assignment_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise TimeEntryAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 7. AD-WS-29 second stage: the persisted Work Assignment
        # Record row's ``assignee_party_id`` must equal
        # ``recording_party_id``. The check uses the row already
        # loaded in step 3, which was read against the caller's
        # connection, so the bound is forge-proof. On mismatch,
        # append a Denial Record in a separate transaction (so the
        # row survives the caller-side rollback) and raise
        # :class:`TimeEntryAssigneeBindingError`; raising the
        # exception causes the caller's surrounding
        # ``engine.begin()`` context manager to roll back without
        # persisting any row.
        actual_assignee = wa_row["assignee_party_id"]
        if actual_assignee != recording_party_id:
            self._persist_time_entry_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                target_work_assignment_id=target_work_assignment_id,
                reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
                correlation_id=correlation,
                recorded_time=evaluate_at,
            )
            raise TimeEntryAssigneeBindingError(
                target_work_assignment_id=target_work_assignment_id,
                recording_party_id=recording_party_id,
                actual_assignee_party_id=actual_assignee,
                correlation_id=correlation,
            )

        # 8. Mint identifiers (AD-WS-2 / AD-WS-28). Time Entry
        # Records are Immutable Records (per ``02-domain-model.md``
        # §8.2) so the Record identifier is minted via
        # :meth:`IdentityService.new_immutable_record_id`. The
        # Relationship Identity is minted via
        # :meth:`IdentityService.new_relationship_id`.
        time_entry_id = str(
            self.identity_service.new_immutable_record_id()
        )
        relates_to_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Time Entry identifier
        # in ``Identifier_Registry``; the digest is the SHA-256 of
        # the canonical JSON payload of the Record so two different
        # Time Entry Records never collide on the same digest.
        # ``authority_basis.id`` is normalized to its string form
        # for the canonical payload because UUID objects are not
        # natively JSON-serializable.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_work_assignment_id": target_work_assignment_id,
                    "effort_hours": normalized_effort_text,
                    "effort_period_start": period_start_str,
                    "effort_period_end": period_end_str,
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
        # carrying the AD-WS-28 ``resource_kind='time_entry_record'``
        # tag. The helper delegates to
        # :meth:`IdentityService.reject_if_duplicate` so the Slice 1
        # identifier-conflict Denial Record pathway fires on any
        # collision; on success the helper INSERTs one row inside
        # the caller's transaction.
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_TIME_ENTRY,
            time_entry_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=recording_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_TIME_ENTRY,
            recorded_time=recorded_time,
        )

        # 10. Insert the Time Entry Record. Every Requirement 25.2
        # attribute lands here. The ``authority_basis`` is
        # destructured into ``authority_basis_type`` and
        # ``authority_basis_id`` columns so the schema-level CHECK
        # on ``authority_basis_type`` enforces the AD-WS-10
        # enumeration at the database layer as well. The normalized
        # two-decimal-place ``effort_hours`` text satisfies both the
        # GLOB regex and the numeric range CHECK constraints.
        connection.execute(
            text(
                """
                INSERT INTO Time_Entry_Records (
                    time_entry_id, target_work_assignment_id,
                    effort_hours, effort_period_start, effort_period_end,
                    recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :time_entry_id, :target_work_assignment_id,
                    :effort_hours, :effort_period_start, :effort_period_end,
                    :recording_party_id,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "time_entry_id": time_entry_id,
                "target_work_assignment_id": target_work_assignment_id,
                "effort_hours": normalized_effort_text,
                "effort_period_start": period_start_str,
                "effort_period_end": period_end_str,
                "recording_party_id": recording_party_id,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 11. Insert the ``Relates To`` Relationship row binding the
        # Time Entry to the target Work Assignment Record with
        # ``semantic_role='time_entry'`` (AD-WS-26). The
        # ``semantic_role`` discriminator is the value the
        # Provenance_Navigator backlink algorithm looks for to
        # return the Time Entry chain when given a Work Assignment
        # identity; it must match the AD-WS-26 table exactly.
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
                "source_kind": _KIND_TIME_ENTRY_RECORD,
                "source_id": time_entry_id,
                "source_revision_id": None,
                "target_kind": _KIND_WORK_ASSIGNMENT_RECORD,
                "target_id": target_work_assignment_id,
                "target_revision_id": None,
                "authoring_party_id": recording_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_TIME_ENTRY,
            },
        )

        # 12. Append the consequential audit row (Requirement 25.5 /
        # Slice 1 AD-WS-5). Participates in the caller's transaction
        # so a failure here rolls back the registry, the
        # ``Time_Entry_Records`` row, and the ``Relationships`` row
        # together (Requirement 25.7). ``target_id`` is the Time
        # Entry Record Identity; ``target_revision_id`` is ``None``
        # because Time Entry Records are Record-scoped (Requirement
        # 22.2 — no separate Revision).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=recording_party_id,
            action_type=_ACTION_CREATE_TIME_ENTRY,
            target_id=time_entry_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateTimeEntryResult(
            time_entry_id=time_entry_id,
            target_work_assignment_id=target_work_assignment_id,
            effort_hours=normalized_effort,
            effort_period_start=period_start_str,
            effort_period_end=period_end_str,
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

        Per Requirement 25.3: a Time Entry creation request that
        omits the target Work Assignment Identity, the recording
        Party Identity, or the applicable scope is rejected with no
        Time Entry Record created. Each missing attribute surfaces
        a distinct ``failed_constraint`` so the HTTP layer can
        identify the precise field to the client.
        """
        if not target_work_assignment_id or not isinstance(
            target_work_assignment_id, str
        ):
            raise TimeEntryValidationError(
                "target_work_assignment_id is required; Requirement 25.3 "
                "rejects Time Entries missing the target Work Assignment "
                "Identity.",
                failed_constraint="target_work_assignment_id_missing",
            )
        if not recording_party_id or not isinstance(recording_party_id, str):
            raise TimeEntryValidationError(
                "recording_party_id is required; Requirement 25.4 rejects "
                "unauthenticated Time Entry creation.",
                failed_constraint="recording_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise TimeEntryValidationError(
                "applicable_scope is required; Requirement 25.3 rejects "
                "Time Entries missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_and_normalize_effort_hours(effort_hours: Any) -> str:
        """Validate ``effort_hours`` and return the two-decimal-place form.

        Per Requirement 25.2 / design §"Execution_Service.TimeEntries":
        reported effort is a non-negative :class:`~decimal.Decimal`
        with at most two fractional digits and at most 24.00 hours.
        The validator:

        1. Rejects ``None`` and non-Decimal-convertible types.
        2. Rejects non-finite values (``NaN`` / ``Infinity``).
        3. Coerces the value to :class:`Decimal` (accepting strings,
           ints, and existing Decimals) and rejects values that fail
           the ISO-decimal regex when serialized.
        4. Rejects negative values and values exceeding 24.00.
        5. Returns the canonical two-decimal-place string form (e.g.
           ``Decimal("12")`` → ``"12.00"``, ``Decimal("0.5")`` →
           ``"0.50"``) that matches the SQLite ``GLOB`` patterns in
           the ``Time_Entry_Records.effort_hours`` CHECK constraint.

        The returned string is the byte-equivalent value that lands
        in the database; the service uses it both as the column
        value and as the canonical contribution to the
        ``Identifier_Registry`` content digest.
        """
        if effort_hours is None:
            raise TimeEntryValidationError(
                "effort_hours is required; Requirement 25.2 requires a "
                "non-negative decimal hours value on every Time Entry.",
                failed_constraint="effort_hours_missing",
            )

        # Coerce to Decimal accepting Decimal, int, and str. ``float`` is
        # explicitly rejected because float→Decimal coercion can produce
        # unexpected binary-floating-point artifacts (e.g.
        # ``Decimal(0.1)`` → ``"0.1000000000000000055..."``); the design
        # contract expects callers to pass Decimal or a textual form.
        if isinstance(effort_hours, Decimal):
            decimal_value: Decimal = effort_hours
        elif isinstance(effort_hours, int) and not isinstance(
            effort_hours, bool
        ):
            decimal_value = Decimal(effort_hours)
        elif isinstance(effort_hours, str):
            try:
                decimal_value = Decimal(effort_hours)
            except InvalidOperation as exc:
                raise TimeEntryValidationError(
                    f"effort_hours {effort_hours!r} is not a valid Decimal: "
                    f"{exc}; Requirement 25.2 requires a decimal hours "
                    "value matching ^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$.",
                    failed_constraint="effort_hours_format",
                ) from exc
        else:
            raise TimeEntryValidationError(
                f"effort_hours must be a Decimal, int, or str; received "
                f"{type(effort_hours).__name__}.",
                failed_constraint="effort_hours_invalid_type",
            )

        if not decimal_value.is_finite():
            raise TimeEntryValidationError(
                f"effort_hours {decimal_value!r} is not a finite Decimal; "
                "Requirement 25.2 requires a finite non-negative value.",
                failed_constraint="effort_hours_not_finite",
            )

        if decimal_value < _EFFORT_HOURS_MIN:
            raise TimeEntryValidationError(
                f"effort_hours {decimal_value!r} is negative; "
                "Requirement 25.2 requires a non-negative value.",
                failed_constraint="effort_hours_negative",
            )
        if decimal_value > _EFFORT_HOURS_MAX:
            raise TimeEntryValidationError(
                f"effort_hours {decimal_value!r} exceeds the per-entry "
                f"maximum of {_EFFORT_HOURS_MAX} per Requirement 25.2.",
                failed_constraint="effort_hours_too_large",
            )

        # Reject values whose fractional part exceeds two digits.
        # ``Decimal.as_tuple().exponent`` is the negative of the
        # number of fractional digits for finite values; -3 means
        # three fractional digits (e.g. ``Decimal("0.123")``), which
        # is rejected. Non-fractional values (e.g. ``Decimal("12")``)
        # have non-negative exponent and trivially satisfy the
        # two-digit upper bound.
        exponent = decimal_value.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise TimeEntryValidationError(
                f"effort_hours {decimal_value!r} has more than two "
                "fractional digits; Requirement 25.2 requires the "
                "ISO-decimal regex ^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$.",
                failed_constraint="effort_hours_format",
            )

        # Normalize to two-decimal-place form. ``quantize`` raises
        # :class:`InvalidOperation` if the result would not be exact,
        # which is impossible here because we have already checked
        # the exponent is >= -2; any value with two or fewer
        # fractional digits is exactly representable at the
        # ``"0.01"`` quantum.
        normalized = decimal_value.quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        normalized_text = format(normalized, "f")

        # Confirm the normalized form satisfies the ISO-decimal regex
        # before returning. The regex accepts ``"0"``, ``"0.5"``,
        # ``"12.34"``, ``"24"``, etc. — the normalized form always
        # carries two fractional digits and either a one-digit or
        # two-digit integer part (since the value is <= 24.00), so
        # the match always succeeds for valid inputs. The defensive
        # check guards against future changes to the normalization
        # rule that might emit a non-conforming form.
        if not _EFFORT_HOURS_REGEX.match(normalized_text):
            raise TimeEntryValidationError(
                f"normalized effort_hours {normalized_text!r} does not "
                "match the ISO-decimal regex "
                "^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$ from "
                "design §Execution_Service.TimeEntries.",
                failed_constraint="effort_hours_format",
            )

        return normalized_text

    @staticmethod
    def _validate_effort_period(
        *,
        effort_period_start: Any,
        effort_period_end: Any,
    ) -> tuple[str, str]:
        """Validate the effort period and return formatted boundary strings.

        Per Requirement 25.2 / 25.3: ``effort_period_start`` and
        ``effort_period_end`` are required UTC datetimes with
        millisecond precision satisfying
        ``effort_period_start <= effort_period_end``. The
        ``effort_period_end <= recorded_at`` ordering is checked
        separately in :meth:`create_time_entry` once the recorded
        time is available.

        Naive datetimes are interpreted as UTC; aware datetimes are
        converted to UTC. The returned strings are the
        ISO-8601 millisecond-precision forms that land in
        ``Time_Entry_Records.effort_period_start`` and
        ``effort_period_end``; because all timestamps in the slice
        are stored as ISO-8601 strings with millisecond precision,
        lexicographic ``<=`` matches chronological ``<=`` and the
        schema-level CHECK constraints share the same comparison.
        """
        if effort_period_start is None:
            raise TimeEntryValidationError(
                "effort_period_start is required; Requirement 25.2 / 25.3 "
                "require the effort-period start time on every Time Entry.",
                failed_constraint="effort_period_start_missing",
            )
        if not isinstance(effort_period_start, datetime):
            raise TimeEntryValidationError(
                f"effort_period_start must be a datetime; received "
                f"{type(effort_period_start).__name__}.",
                failed_constraint="effort_period_start_invalid_type",
            )
        if effort_period_end is None:
            raise TimeEntryValidationError(
                "effort_period_end is required; Requirement 25.2 / 25.3 "
                "require the effort-period end time on every Time Entry.",
                failed_constraint="effort_period_end_missing",
            )
        if not isinstance(effort_period_end, datetime):
            raise TimeEntryValidationError(
                f"effort_period_end must be a datetime; received "
                f"{type(effort_period_end).__name__}.",
                failed_constraint="effort_period_end_invalid_type",
            )

        # Normalize timezone awareness. Naive datetimes are
        # interpreted as UTC to match the Slice 1 / Slice 2
        # convention; aware datetimes are converted to UTC so the
        # persisted string carries the canonical UTC suffix.
        start_utc = _coerce_to_utc(effort_period_start)
        end_utc = _coerce_to_utc(effort_period_end)

        start_str = format_iso8601_ms(start_utc)
        end_str = format_iso8601_ms(end_utc)

        if start_str > end_str:
            raise TimeEntryValidationError(
                f"effort_period_start {start_str!r} is later than "
                f"effort_period_end {end_str!r}; Requirement 25.3 requires "
                "the start time to be at or before the end time.",
                failed_constraint="effort_period_start_after_end",
            )

        return start_str, end_str

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 25.2 / AD-WS-10: the authority basis ``type``
        is drawn from ``{role-grant-id, scope-id, delegation-chain-id}``.
        The Python-typed signature already constrains callers to
        pass an :class:`AuthorityBasisRef` whose ``type`` Literal
        restricts the enumeration; the HTTP layer may pass a dict if
        it has not yet bound the request to the typed model, so this
        validator coerces both shapes (mirroring the Slice 2 / Slice 3
        sibling validators).
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise TimeEntryValidationError(
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
            raise TimeEntryValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise TimeEntryValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise TimeEntryValidationError(
                "authority_basis.id is required per Requirement 25.2.",
                failed_constraint="authority_basis_id_missing",
            )

        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise TimeEntryValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    # -- denial side-channel ----------------------------------------------

    def _persist_time_entry_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_work_assignment_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Time Entry attempt.

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
          :class:`TimeEntryAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_time_entry` raises
        :class:`TimeEntryAuthorizationError` /
        :class:`TimeEntryAssigneeBindingError` (or this method
        raises :class:`TimeEntryAuditFailureError`). The Denial
        Record must therefore live outside that scope to survive
        (AD-WS-9 / Requirement 30.6).

        ``target_id`` on the Denial Record points at the target Work
        Assignment Identity because the Time Entry Identity has not
        yet been minted at the time the denial is recorded — the
        deny path explicitly refuses to mint an Immutable Record
        Identity for an unauthorized attempt (Requirement 25.4 /
        Requirement 30.5 — no information leakage about the
        existence of restricted Records).

        Both :class:`AuditAppendError` and :class:`SQLAlchemyError`
        are treated as retryable failures.
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=_ACTION_CREATE_TIME_ENTRY,
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

        raise TimeEntryAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id, SHA-256, and UTC-coercion helpers
# so this module does not import private names from sibling services.
# The functions are intentionally identical to their sibling
# implementations: correlation identifiers are non-domain values and
# the digest helper is opaque to :class:`Identifier_Registry`.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Time Entry
    creation. They are not registered with :class:`IdentityService`
    because they do not name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Time Entry
    Identity in ``Identifier_Registry``. Time Entry Records are
    Record-scoped (Requirement 22.2 — no separate Revision) so this
    digest is bound exactly once per Time Entry creation.
    """
    return hashlib.sha256(content).hexdigest()


def _coerce_to_utc(value: datetime) -> datetime:
    """Coerce a datetime to UTC, treating naive values as UTC.

    Slice 1 / Slice 2 / Slice 3 timestamps are stored as ISO-8601
    strings with millisecond precision and the canonical UTC suffix.
    A naive datetime (no tzinfo) is interpreted as UTC by attaching
    :class:`datetime.timezone.utc`; an aware datetime in a non-UTC
    timezone is converted via :meth:`datetime.astimezone`. The result
    is always a tz-aware UTC datetime suitable for
    :func:`walking_slice.audit.format_iso8601_ms`.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
