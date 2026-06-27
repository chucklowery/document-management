"""Outcome_Service.MeasurementRecords — native and imported Measurement
Records keyed to a Measurement Definition Revision, plus the ``Cites``
Relationship to that Revision.

Design reference
================

``.kiro/specs/fourth-walking-slice/design.md``:

- §"Outcome_Service.MeasurementRecords" — the public dataclass surface
  (``create_native_measurement`` and ``create_imported_measurement``), the
  authority mapping (``create.measurement_record`` → ``record_measurement``
  for both native and imported writes per AD-WS-33), the native validation
  rules (Requirement 45), the imported validation rules (Requirement 46),
  the AD-WS-38 native-vs-imported source-system-attribute discipline, the
  AD-WS-39 import idempotency key, and the AD-WS-35 single ``Cites``
  Relationship with ``semantic_role = 'measurement_basis'``.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time shared
  by every row in the transaction), Identifiers (every new identity is a
  UUIDv7 minted by :class:`IdentityService` and registered in
  ``Identifier_Registry`` with the additive Slice 4 ``measurement_record``
  ``resource_kind`` tag per AD-WS-37), Authorization (the deny path reuses
  the cumulative separate-transaction Denial-Record pattern with the
  Requirement 50.6 three-attempt retry).

Task scope (task 5.1)
=====================

This module implements
:meth:`MeasurementRecordService.create_native_measurement` and
:meth:`MeasurementRecordService.create_imported_measurement`:

**Native (Requirement 45).** Reject when the target Measurement Definition
Revision does not resolve, the observed value has more than six fractional
digits, the unit string does not match the Definition's ``unit_of_measure``,
the observation time is outside the observation-window descriptor or later
than the recorded time, any required attribute is omitted, or any
imported-only source-system attribute is supplied. Persist ``origin =
'native'`` with all source-system columns NULL (AD-WS-38). Normalize the
Decimal before persistence (Requirement 45.2).

**Imported (Requirement 46).** Reject when any source-system attribute is
omitted, ``source_system_authority`` is outside ``{authoritative, replica,
projection, index, federation}``, the observation time is later than the
retrieval time, the retrieval time is later than the recorded time, or the
origin is supplied as anything other than ``imported``. Never default the
authority designation to ``authoritative`` (reject if absent). Set
``import_at = recorded_at`` (Requirement 46.2). Enforce the AD-WS-39
idempotency key, rejecting a duplicate ``(source_system_id,
source_system_record_id)`` pair per Definition Revision with no second
Record persisted (Requirement 46.3).

Both write one ``Cites`` Relationship to the target Measurement Definition
Revision (``semantic_role = 'measurement_basis'``, AD-WS-35), evaluate
``Authorization_Service.evaluate(party, "create.measurement_record", ...)``
on a separate transaction, and on permit insert the ``Measurement_Records``
row, the ``Cites`` Relationship, and the consequential ``Audit_Records`` row
inside the caller's transaction (Requirements 45.5/45.7, 46.6, 57.1).

Requirements satisfied
======================

    45.1-45.7 — native Measurement Record creation, validation, denial,
                consequential audit, immutability (schema triggers), and
                audit-failure rollback.
    46.1-46.8 — imported Measurement Record creation, source-system
                attribute discipline, idempotency, ordering, explicit
                authority preservation, and non-modification of prior slices.
    52.7      — ``create.measurement_record`` requires ``record_measurement``.
    57.1      — the consequential audit append participates in the same
                transaction as the domain rows.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid_utils
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Final, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.outcome._helpers import (
    OUTCOME_PROHIBITED_PREFIXES,
    OutcomeValidationError,
    _record_outcome_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.models import CreateMeasurementRecordResult


__all__ = [
    "MeasurementRecordAuditFailureError",
    "MeasurementRecordAuthorizationError",
    "MeasurementRecordDuplicateError",
    "MeasurementRecordRow",
    "MeasurementRecordService",
    "MeasurementRecordTargetNotResolvableError",
    "MeasurementRecordValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# ``create.measurement_record`` maps to the ``record_measurement`` authority
# per AD-WS-33 for *both* native and imported writes. The string is also the
# ``action_type`` recorded on the consequential audit row (Requirements 45.5
# / 46.6) and on the separate-transaction Denial Record (Requirements 45.4 /
# 46.5).
_ACTION_CREATE_MEASUREMENT_RECORD: Final[str] = "create.measurement_record"

# Relationship Type, source/target ``kind`` strings, and the AD-WS-35
# ``semantic_role`` value written to the single ``Cites`` Relationship row.
_RELATIONSHIP_TYPE_CITES: Final[str] = "Cites"
_SEMANTIC_ROLE_MEASUREMENT_BASIS: Final[str] = "measurement_basis"
_KIND_MEASUREMENT_RECORD: Final[str] = "measurement_record"
_KIND_MEASUREMENT_DEFINITION_REVISION: Final[str] = (
    "measurement_definition_revision"
)

# Identifier_Registry registration kind (Slice 1 enumeration) and the Slice 4
# ``resource_kind`` tag (AD-WS-37). A Measurement Record is an Immutable
# Record so its registry binding uses ``kind='immutable_record'``.
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_MEASUREMENT_RECORD: Final[str] = "measurement_record"

# Origin enumeration (AD-WS-38).
_ORIGIN_NATIVE: Final[str] = "native"
_ORIGIN_IMPORTED: Final[str] = "imported"

# The enumerated source-system authority designations (Requirement 46.2 /
# Principle 5.27). The designation is recorded explicitly and is never
# defaulted to ``authoritative`` (Requirement 46.7).
_VALID_SOURCE_SYSTEM_AUTHORITIES: Final[frozenset[str]] = frozenset(
    {"authoritative", "replica", "projection", "index", "federation"}
)

# The maximum number of fractional digits permitted on an observed value
# (Requirement 45.2 / 46.2).
_MAX_FRACTIONAL_DIGITS: Final[int] = 6

# The 1..200 character bound on source-system identifiers (Requirement 46.1).
_SOURCE_SYSTEM_FIELD_MIN_CHARS: Final[int] = 1
_SOURCE_SYSTEM_FIELD_MAX_CHARS: Final[int] = 200

# Exponential backoff sequence for retrying the separate-transaction Denial
# Record append (Requirement 50.6, mirroring the Slice 1/2/3 pattern). Three
# retries after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class MeasurementRecordValidationError(ValueError):
    """Raised when a Measurement Record submission fails Requirement 45.3 /
    46.4 / 53 validation.

    ``failed_constraint`` names the first specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a stable
    identifier rather than the message text. ``invalid_attributes`` lists every
    rejected attribute name so the response can identify each one
    (Requirements 45.3 and 46.4 — "return an error indication identifying each
    invalid attribute").

    Attributes:
        failed_constraint: A stable discriminator such as
            ``"observed_value_too_many_fractional_digits"``,
            ``"observed_value_unit_mismatch"``,
            ``"observation_time_outside_window"``,
            ``"observation_time_after_recorded"``,
            ``"native_source_system_attribute_supplied"``,
            ``"source_system_attribute_missing"``,
            ``"source_system_authority_invalid"``,
            ``"observation_after_retrieval"``,
            ``"retrieval_after_recorded"``,
            ``"origin_indicator_invalid"``,
            ``"observed_value_missing"``,
            ``"observed_value_unit_missing"``,
            ``"observation_time_missing"``,
            ``"applicable_scope_missing"``,
            ``"prohibited_attribute"``, or ``"invalid_request"``.
        invalid_attributes: Every offending attribute name; empty tuple when
            the failure is not attributable to a specific field.
        prohibited_keys: Populated only when ``failed_constraint`` is
            ``"prohibited_attribute"``; lists every offending top-level key.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        invalid_attributes: tuple[str, ...] = (),
        prohibited_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.invalid_attributes = invalid_attributes
        self.prohibited_keys = prohibited_keys


class MeasurementRecordTargetNotResolvableError(LookupError):
    """Raised when the target Measurement Definition Revision does not resolve
    (Requirements 45.3 / 46.4).

    A native or imported Measurement Record names exactly one target
    Measurement Definition Revision Identity which must resolve to an existing
    Measurement Definition Revision. The exception carries the offending
    identifier so the HTTP layer can render a structured 404.
    """

    def __init__(
        self,
        *,
        target_measurement_definition_revision_id: str,
    ) -> None:
        super().__init__(
            f"Target Measurement Definition Revision "
            f"{target_measurement_definition_revision_id!r} does not resolve "
            "(Requirements 45.3 / 46.4)."
        )
        self.target_measurement_definition_revision_id = (
            target_measurement_definition_revision_id
        )
        self.failed_constraint = (
            "target_measurement_definition_revision_not_resolvable"
        )


class MeasurementRecordDuplicateError(ValueError):
    """Raised when an imported Measurement Record violates the AD-WS-39
    idempotency key (Requirement 46.3).

    The pair ``(source_system_id, source_system_record_id)`` is an idempotency
    key per target Measurement Definition Revision: a second imported
    Measurement Record whose pair matches an already-finalized imported Record
    against the same Definition Revision is rejected with no second Record
    persisted. The exception carries the existing Measurement Record Identity
    so the HTTP layer can surface it *only* when the caller holds view
    authority on it (AD-WS-9); the first Record is left byte-equivalent.
    """

    def __init__(
        self,
        *,
        target_measurement_definition_revision_id: str,
        source_system_id: str,
        source_system_record_id: str,
        existing_measurement_record_id: str,
    ) -> None:
        super().__init__(
            f"An imported Measurement Record "
            f"({existing_measurement_record_id!r}) already records the "
            f"source-system pair "
            f"({source_system_id!r}, {source_system_record_id!r}) against "
            f"Measurement Definition Revision "
            f"{target_measurement_definition_revision_id!r}; the pair is an "
            "idempotency key (Requirement 46.3 / AD-WS-39)."
        )
        self.target_measurement_definition_revision_id = (
            target_measurement_definition_revision_id
        )
        self.source_system_id = source_system_id
        self.source_system_record_id = source_system_record_id
        self.existing_measurement_record_id = existing_measurement_record_id
        self.failed_constraint = "imported_measurement_duplicate"


class MeasurementRecordAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Measurement Record
    attempt (Requirements 45.4 / 46.5).

    Carries only ``reason_code`` and ``correlation_id`` — the AD-WS-9
    indistinguishable-denial contract forbids leaking authorized Party
    identities, target contents, or target existence beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Measurement Record creation denied: "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class MeasurementRecordAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 50.6).

    On total audit-append failure the exception is raised *in place of*
    :class:`MeasurementRecordAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's transaction
    still rolls back so no Measurement Record, ``Cites`` Relationship, or
    consequential audit row is persisted.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Measurement Record failed "
            f"after {attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Read-model row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementRecordRow:
    """Read-model snapshot of a persisted ``Measurement_Records`` row.

    Returned by :meth:`MeasurementRecordService.get_measurement_record`. The
    Observed Outcome anchoring rule (Requirement 47.2 / 47.4, AD-WS-40) needs
    the cited Measurement Record's target Measurement Definition **Resource**
    Identity (``target_measurement_definition_id``) so the
    :class:`~walking_slice.outcome.observed_outcomes.ObservedOutcomeService`
    can confirm it matches the single Measurement Definition Resource that
    addresses the target Intended Outcome Resource. The remaining columns are
    surfaced for completeness and for the disclosure / provenance read paths.
    Identity values and timestamps are carried as ``str`` to match the
    persisted column form.
    """

    measurement_record_id: str
    target_measurement_definition_id: str
    target_measurement_definition_revision_id: str
    origin: str
    observed_value: str
    observed_value_unit: str
    observation_time: str
    recording_party_id: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementRecordService:
    """Persist native and imported Measurement Records keyed to a Measurement
    Definition Revision, plus the single ``Cites`` Relationship to that
    Revision.

    Connection-scoped at call time: both create methods accept the caller's
    :class:`sqlalchemy.engine.Connection` and write inside the caller's
    transaction (AD-WS-5). The service instance therefore holds only the
    cross-request collaborators and can be shared across requests.

    Frozen because design §"Outcome_Service.MeasurementRecords" declares it
    ``@dataclass(frozen=True)``.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Measurement_Records``, ``Relationships``, and ``Audit_Records``
            rows. Consulted exactly once per write.
        identity_service: Generates Measurement Record Identities and persists
            their ``Identifier_Registry`` bindings with the Slice 4
            ``measurement_record`` ``resource_kind`` tag (AD-WS-37).
        audit_log: Appends the consequential audit row (Requirements 45.5 /
            46.6) inside the caller's transaction.
        authorization_service: Evaluates ``create.measurement_record`` →
            ``record_measurement`` authority per AD-WS-33 / Requirements 45.4
            / 46.5; the deny path is the cumulative separate-transaction
            Denial-Record pattern.
        definition_reader: The :class:`MeasurementDefinitionService` used
            read-only (``get_definition_revision``) to resolve the target
            Measurement Definition Revision and recover its target Measurement
            Definition Resource Identity, ``unit_of_measure``, and
            ``observation_window``.
        denial_audit_sleep: Sleep function used to pause between retries of
            the Denial Record append. Defaults to :func:`time.sleep`; tests
            inject a recording stub so the retry sequence is observable
            without spending real time.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: AuthorizationService
    definition_reader: MeasurementDefinitionService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface: native -------------------------------------------

    def create_native_measurement(
        self,
        connection: Connection,
        *,
        target_measurement_definition_revision_id: str,
        observed_value: Any,
        observed_value_unit: str,
        observation_time: datetime,
        recording_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateMeasurementRecordResult:
        """Create a native Measurement Record plus its single ``Cites``
        Relationship to the target Measurement Definition Revision.

        Per Requirements 45.1 through 45.7, 52.7 (``record_measurement``),
        57.1, AD-WS-9 (indistinguishable denial), AD-WS-33, AD-WS-35,
        AD-WS-37, and AD-WS-38.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_measurement_definition_revision_id: Identity of the single
                target Measurement Definition Revision.
            observed_value: The observed value as a :class:`decimal.Decimal`
                (an ``int`` / ``str`` is coerced); at most six fractional
                digits (Requirement 45.2). Normalized before persistence.
            observed_value_unit: Unit string; must match the Definition's
                ``unit_of_measure`` (Requirement 45.3).
            observation_time: Timezone-aware UTC observation time; within the
                Definition's observation-window descriptor and not later than
                the recorded time (Requirement 45.3).
            recording_party_id: Identity of the recording Party.
            applicable_scope: Scope the Measurement Record applies within;
                also passed as ``target.scope`` to authorization.
            engine: Required for the deny path's separate-transaction Denial
                Record write and the separate-transaction authorization
                evaluation.
            correlation_id: Optional correlation identifier; a UUIDv7 is
                generated when omitted.
            evaluation_at: Optional explicit ``at`` for authorization;
                defaults to the recorded time.
            request_attributes: Optional raw request body screened against the
                intended-side prefix list (Requirement 53).

        Returns:
            :class:`CreateMeasurementRecordResult` with ``origin = 'native'``
            and all source-system attributes ``None``.

        Raises:
            MeasurementRecordValidationError: A required attribute is missing,
                a Requirement 45.3 rule was violated, or the request body
                carried a prohibited intended-side attribute.
            MeasurementRecordTargetNotResolvableError: The target Measurement
                Definition Revision did not resolve.
            MeasurementRecordAuthorizationError: The attempt was denied; the
                Denial Record was appended in a separate transaction.
            MeasurementRecordAuditFailureError: The attempt was denied and the
                Denial Record append failed on every retry.
        """
        self._screen_request_attributes(request_attributes)

        invalid: list[str] = []
        if not _is_present(observed_value):
            invalid.append("observed_value")
        if not _is_present_str(observed_value_unit):
            invalid.append("observed_value_unit")
        if observation_time is None:
            invalid.append("observation_time")
        if not _is_present_str(applicable_scope):
            invalid.append("applicable_scope")
        if not _is_present_str(recording_party_id):
            invalid.append("recording_party_id")
        if invalid:
            raise MeasurementRecordValidationError(
                f"required native Measurement Record attribute(s) {invalid!r} "
                "are missing (Requirement 45.3).",
                failed_constraint=f"{invalid[0]}_missing",
                invalid_attributes=tuple(invalid),
            )

        # Reject any imported-only source-system attribute supplied on a
        # native request (Requirement 45.3). The typed signature declares
        # none, so only a raw request body can carry one.
        self._reject_native_source_system_attributes(request_attributes)

        normalized_value = self._normalize_observed_value(observed_value)

        # Resolve the target Measurement Definition Revision (Requirement
        # 45.3). The resolution runs before authorization so the deny path
        # never reveals whether a target exists to an unauthorized caller.
        definition = self.definition_reader.get_definition_revision(
            connection,
            measurement_definition_revision_id=(
                target_measurement_definition_revision_id
            ),
        )
        if definition is None:
            raise MeasurementRecordTargetNotResolvableError(
                target_measurement_definition_revision_id=(
                    target_measurement_definition_revision_id
                ),
            )

        # Unit must match the Definition's unit_of_measure (Requirement 45.3).
        if observed_value_unit != definition.unit_of_measure:
            raise MeasurementRecordValidationError(
                f"observed-value unit {observed_value_unit!r} does not match "
                f"the Measurement Definition unit_of_measure "
                f"{definition.unit_of_measure!r} (Requirement 45.3).",
                failed_constraint="observed_value_unit_mismatch",
                invalid_attributes=("observed_value_unit",),
            )

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        observation_at = format_iso8601_ms(observation_time)

        # Observation time within the observation-window descriptor
        # (Requirement 45.3).
        if not _observation_within_window(
            observation_time, definition.observation_window
        ):
            raise MeasurementRecordValidationError(
                f"observation time {observation_at!r} is outside the "
                f"Measurement Definition observation-window descriptor "
                f"{definition.observation_window!r} (Requirement 45.3).",
                failed_constraint="observation_time_outside_window",
                invalid_attributes=("observation_time",),
            )

        # Observation time not later than the recorded time (Requirement
        # 45.3). Canonical ISO-8601 UTC strings compare lexicographically.
        if observation_at > recorded_at:
            raise MeasurementRecordValidationError(
                f"observation time {observation_at!r} is later than the "
                f"recorded time {recorded_at!r} (Requirement 45.3).",
                failed_constraint="observation_time_after_recorded",
                invalid_attributes=("observation_time",),
            )

        return self._persist_measurement_record(
            connection,
            engine=engine,
            origin=_ORIGIN_NATIVE,
            definition=definition,
            observed_value=normalized_value,
            observed_value_unit=observed_value_unit,
            observation_at=observation_at,
            recording_party_id=recording_party_id,
            applicable_scope=applicable_scope,
            recorded_time=recorded_time,
            recorded_at=recorded_at,
            correlation_id=correlation_id,
            evaluation_at=evaluation_at,
            source_system_id=None,
            source_system_record_id=None,
            source_system_authority=None,
            source_system_retrieval_at=None,
            import_at=None,
        )

    # -- public surface: imported -----------------------------------------

    def create_imported_measurement(
        self,
        connection: Connection,
        *,
        target_measurement_definition_revision_id: str,
        observed_value: Any,
        observed_value_unit: str,
        observation_time: datetime,
        source_system_id: str,
        source_system_record_id: str,
        source_system_authority: Optional[str],
        source_system_retrieval_time: datetime,
        importing_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        origin: Optional[str] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateMeasurementRecordResult:
        """Create an imported Measurement Record plus its single ``Cites``
        Relationship to the target Measurement Definition Revision.

        Per Requirements 46.1 through 46.8, 52.7 (``record_measurement``),
        57.1, AD-WS-9, AD-WS-33, AD-WS-35, AD-WS-37, AD-WS-38, and AD-WS-39.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_measurement_definition_revision_id: Identity of the single
                target Measurement Definition Revision.
            observed_value: The observed value as a :class:`decimal.Decimal`
                (an ``int`` / ``str`` is coerced); at most six fractional
                digits. Normalized before persistence.
            observed_value_unit: Unit string; must match the Definition's
                ``unit_of_measure`` (Requirement 46.4).
            observation_time: Timezone-aware UTC observation time; within the
                window and not later than the source-system retrieval time
                (Requirement 46.4).
            source_system_id: Source-system identifier (1..200 chars).
            source_system_record_id: Source-system record identifier
                (1..200 chars).
            source_system_authority: The external authority designation drawn
                from ``{authoritative, replica, projection, index,
                federation}``. Never defaulted; an absent value is rejected
                (Requirement 46.7).
            source_system_retrieval_time: Timezone-aware UTC retrieval time;
                not later than the recorded time (Requirement 46.4).
            importing_party_id: Identity of the importing Party.
            applicable_scope: Scope the Measurement Record applies within.
            engine: Required for the deny path and the separate-transaction
                authorization evaluation.
            correlation_id: Optional correlation identifier.
            evaluation_at: Optional explicit ``at`` for authorization.
            origin: Optional explicit origin indicator; rejected when supplied
                as anything other than ``imported`` (Requirement 46.4).
            request_attributes: Optional raw request body screened against the
                intended-side prefix list (Requirement 53).

        Returns:
            :class:`CreateMeasurementRecordResult` with ``origin =
            'imported'`` surfacing the source-system attributes explicitly.

        Raises:
            MeasurementRecordValidationError: A required attribute is missing
                or a Requirement 46.4 rule was violated.
            MeasurementRecordTargetNotResolvableError: The target Measurement
                Definition Revision did not resolve.
            MeasurementRecordDuplicateError: The ``(source_system_id,
                source_system_record_id)`` pair already exists against this
                Definition Revision (Requirement 46.3).
            MeasurementRecordAuthorizationError: The attempt was denied.
            MeasurementRecordAuditFailureError: The denial audit append failed.
        """
        self._screen_request_attributes(request_attributes)

        # Origin must be either omitted or the literal 'imported'
        # (Requirement 46.4).
        if origin is not None and origin != _ORIGIN_IMPORTED:
            raise MeasurementRecordValidationError(
                f"origin indicator {origin!r} is invalid for an imported "
                f"Measurement Record; only {_ORIGIN_IMPORTED!r} is permitted "
                "(Requirement 46.4).",
                failed_constraint="origin_indicator_invalid",
                invalid_attributes=("origin",),
            )

        # Collect every omitted required attribute (Requirement 46.4 —
        # "return an error indication identifying each invalid attribute").
        invalid: list[str] = []
        if not _is_present(observed_value):
            invalid.append("observed_value")
        if not _is_present_str(observed_value_unit):
            invalid.append("observed_value_unit")
        if observation_time is None:
            invalid.append("observation_time")
        if not _is_present_str(source_system_id):
            invalid.append("source_system_id")
        if not _is_present_str(source_system_record_id):
            invalid.append("source_system_record_id")
        # The source-system authority designation is NEVER defaulted to
        # 'authoritative'; an absent designation is rejected (Requirement
        # 46.7).
        if not _is_present_str(source_system_authority):
            invalid.append("source_system_authority")
        if source_system_retrieval_time is None:
            invalid.append("source_system_retrieval_time")
        if not _is_present_str(applicable_scope):
            invalid.append("applicable_scope")
        if not _is_present_str(importing_party_id):
            invalid.append("importing_party_id")
        if invalid:
            raise MeasurementRecordValidationError(
                f"required imported Measurement Record attribute(s) "
                f"{invalid!r} are missing (Requirement 46.4).",
                failed_constraint="source_system_attribute_missing",
                invalid_attributes=tuple(invalid),
            )

        # Source-system identifier length bounds (Requirement 46.1).
        for name, value in (
            ("source_system_id", source_system_id),
            ("source_system_record_id", source_system_record_id),
        ):
            if not (
                _SOURCE_SYSTEM_FIELD_MIN_CHARS
                <= len(value)
                <= _SOURCE_SYSTEM_FIELD_MAX_CHARS
            ):
                raise MeasurementRecordValidationError(
                    f"{name} must be "
                    f"{_SOURCE_SYSTEM_FIELD_MIN_CHARS}.."
                    f"{_SOURCE_SYSTEM_FIELD_MAX_CHARS} characters "
                    "(Requirement 46.1).",
                    failed_constraint=f"{name}_invalid",
                    invalid_attributes=(name,),
                )

        # Authority designation must be in the enumerated set (Requirement
        # 46.4). Never defaulted to 'authoritative' (Requirement 46.7).
        if source_system_authority not in _VALID_SOURCE_SYSTEM_AUTHORITIES:
            raise MeasurementRecordValidationError(
                f"source-system authority designation "
                f"{source_system_authority!r} is outside the enumerated set "
                f"{sorted(_VALID_SOURCE_SYSTEM_AUTHORITIES)} "
                "(Requirement 46.4).",
                failed_constraint="source_system_authority_invalid",
                invalid_attributes=("source_system_authority",),
            )

        normalized_value = self._normalize_observed_value(observed_value)

        definition = self.definition_reader.get_definition_revision(
            connection,
            measurement_definition_revision_id=(
                target_measurement_definition_revision_id
            ),
        )
        if definition is None:
            raise MeasurementRecordTargetNotResolvableError(
                target_measurement_definition_revision_id=(
                    target_measurement_definition_revision_id
                ),
            )

        if observed_value_unit != definition.unit_of_measure:
            raise MeasurementRecordValidationError(
                f"observed-value unit {observed_value_unit!r} does not match "
                f"the Measurement Definition unit_of_measure "
                f"{definition.unit_of_measure!r} (Requirement 46.4).",
                failed_constraint="observed_value_unit_mismatch",
                invalid_attributes=("observed_value_unit",),
            )

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        observation_at = format_iso8601_ms(observation_time)
        retrieval_at = format_iso8601_ms(source_system_retrieval_time)

        if not _observation_within_window(
            observation_time, definition.observation_window
        ):
            raise MeasurementRecordValidationError(
                f"observation time {observation_at!r} is outside the "
                f"Measurement Definition observation-window descriptor "
                f"{definition.observation_window!r} (Requirement 46.4).",
                failed_constraint="observation_time_outside_window",
                invalid_attributes=("observation_time",),
            )

        # observation_time <= source_system_retrieval_time (Requirement 46.4).
        if observation_at > retrieval_at:
            raise MeasurementRecordValidationError(
                f"observation time {observation_at!r} is later than the "
                f"source-system retrieval time {retrieval_at!r} "
                "(Requirement 46.4).",
                failed_constraint="observation_after_retrieval",
                invalid_attributes=("observation_time",),
            )

        # source_system_retrieval_time <= recorded_time (Requirement 46.4).
        if retrieval_at > recorded_at:
            raise MeasurementRecordValidationError(
                f"source-system retrieval time {retrieval_at!r} is later than "
                f"the recorded time {recorded_at!r} (Requirement 46.4).",
                failed_constraint="retrieval_after_recorded",
                invalid_attributes=("source_system_retrieval_time",),
            )

        # AD-WS-39 idempotency pre-check (Requirement 46.3). Reject a second
        # imported Record carrying the same (source_system_id,
        # source_system_record_id) pair against the same Definition Revision
        # with no second Record persisted; the existing Record is surfaced so
        # the HTTP layer can apply the AD-WS-9 view-authority gate. The DB
        # partial UNIQUE index enforces the same invariant as a backstop.
        existing_record_id = self._find_existing_imported_record(
            connection,
            target_measurement_definition_revision_id=(
                target_measurement_definition_revision_id
            ),
            source_system_id=source_system_id,
            source_system_record_id=source_system_record_id,
        )
        if existing_record_id is not None:
            raise MeasurementRecordDuplicateError(
                target_measurement_definition_revision_id=(
                    target_measurement_definition_revision_id
                ),
                source_system_id=source_system_id,
                source_system_record_id=source_system_record_id,
                existing_measurement_record_id=existing_record_id,
            )

        # import_at = recorded_at (Requirement 46.2).
        return self._persist_measurement_record(
            connection,
            engine=engine,
            origin=_ORIGIN_IMPORTED,
            definition=definition,
            observed_value=normalized_value,
            observed_value_unit=observed_value_unit,
            observation_at=observation_at,
            recording_party_id=importing_party_id,
            applicable_scope=applicable_scope,
            recorded_time=recorded_time,
            recorded_at=recorded_at,
            correlation_id=correlation_id,
            evaluation_at=evaluation_at,
            source_system_id=source_system_id,
            source_system_record_id=source_system_record_id,
            source_system_authority=source_system_authority,
            source_system_retrieval_at=retrieval_at,
            import_at=recorded_at,
        )

    # -- shared persistence ------------------------------------------------

    def _persist_measurement_record(
        self,
        connection: Connection,
        *,
        engine: Engine,
        origin: str,
        definition: Any,
        observed_value: str,
        observed_value_unit: str,
        observation_at: str,
        recording_party_id: str,
        applicable_scope: str,
        recorded_time: datetime,
        recorded_at: str,
        correlation_id: Optional[str],
        evaluation_at: Optional[datetime],
        source_system_id: Optional[str],
        source_system_record_id: Optional[str],
        source_system_authority: Optional[str],
        source_system_retrieval_at: Optional[str],
        import_at: Optional[str],
    ) -> CreateMeasurementRecordResult:
        """Authorize and persist a Measurement Record row, its ``Cites``
        Relationship, and the consequential audit row.

        Shared by both the native and imported create paths after their
        respective validation has run. The authorization evaluation runs on a
        SEPARATE transaction; on deny it drives the AD-WS-9 separate-
        transaction Denial-Record pattern with the Requirement 50.6 retry. On
        permit it inserts the registry binding, the ``Measurement_Records``
        row, the single ``Cites`` Relationship (``semantic_role =
        'measurement_basis'``), and the consequential audit row — all inside
        the caller's transaction so a failure anywhere rolls every row back
        (Requirements 45.5/45.7, 46.6, 57.1).
        """
        target_measurement_definition_revision_id = (
            definition.measurement_definition_revision_id
        )
        target_measurement_definition_id = definition.measurement_definition_id
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # Authorization evaluation on a SEPARATE transaction. The TargetRef is
        # the target Measurement Definition Revision so the wired role
        # assignment must cover the same scope to permit the action.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=recording_party_id,
                action=_ACTION_CREATE_MEASUREMENT_RECORD,
                target=TargetRef(
                    kind=_KIND_MEASUREMENT_DEFINITION_REVISION,
                    id=target_measurement_definition_id,
                    revision_id=target_measurement_definition_revision_id,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_measurement_record_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                target_measurement_definition_revision_id=(
                    target_measurement_definition_revision_id
                ),
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise MeasurementRecordAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # Mint identifiers (AD-WS-37). A Measurement Record is an Immutable
        # Record so the registry binding uses kind='immutable_record'.
        measurement_record_id = str(
            self.identity_service.new_immutable_record_id()
        )
        cites_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_measurement_definition_id": (
                        target_measurement_definition_id
                    ),
                    "target_measurement_definition_revision_id": (
                        target_measurement_definition_revision_id
                    ),
                    "origin": origin,
                    "observed_value": observed_value,
                    "observed_value_unit": observed_value_unit,
                    "observation_time": observation_at,
                    "source_system_id": source_system_id,
                    "source_system_record_id": source_system_record_id,
                    "source_system_authority": source_system_authority,
                    "source_system_retrieval_at": source_system_retrieval_at,
                    "import_at": import_at,
                    "recording_party_id": recording_party_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        _record_outcome_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_MEASUREMENT_RECORD,
            measurement_record_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=recording_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_MEASUREMENT_RECORD,
            recorded_time=recorded_time,
        )

        connection.execute(
            text(
                """
                INSERT INTO Measurement_Records (
                    measurement_record_id,
                    target_measurement_definition_id,
                    target_measurement_definition_revision_id,
                    origin, observed_value, observed_value_unit,
                    observation_time,
                    source_system_id, source_system_record_id,
                    source_system_authority, source_system_retrieval_at,
                    import_at,
                    recording_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :measurement_record_id,
                    :target_measurement_definition_id,
                    :target_measurement_definition_revision_id,
                    :origin, :observed_value, :observed_value_unit,
                    :observation_time,
                    :source_system_id, :source_system_record_id,
                    :source_system_authority, :source_system_retrieval_at,
                    :import_at,
                    :recording_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "measurement_record_id": measurement_record_id,
                "target_measurement_definition_id": (
                    target_measurement_definition_id
                ),
                "target_measurement_definition_revision_id": (
                    target_measurement_definition_revision_id
                ),
                "origin": origin,
                "observed_value": observed_value,
                "observed_value_unit": observed_value_unit,
                "observation_time": observation_at,
                "source_system_id": source_system_id,
                "source_system_record_id": source_system_record_id,
                "source_system_authority": source_system_authority,
                "source_system_retrieval_at": source_system_retrieval_at,
                "import_at": import_at,
                "recording_party_id": recording_party_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # Single ``Cites`` Relationship to the target Measurement Definition
        # Revision (AD-WS-35, semantic_role = 'measurement_basis'). The source
        # is the Measurement Record (an Immutable Record with no Revision, so
        # source_revision_id is NULL).
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
                    :source_kind, :source_id, NULL,
                    :target_kind, :target_id, :target_revision_id,
                    :authoring_party_id, :recorded_at, :semantic_role
                )
                """
            ),
            {
                "relationship_id": cites_relationship_id,
                "relationship_type": _RELATIONSHIP_TYPE_CITES,
                "source_kind": _KIND_MEASUREMENT_RECORD,
                "source_id": measurement_record_id,
                "target_kind": _KIND_MEASUREMENT_DEFINITION_REVISION,
                "target_id": target_measurement_definition_id,
                "target_revision_id": (
                    target_measurement_definition_revision_id
                ),
                "authoring_party_id": recording_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_MEASUREMENT_BASIS,
            },
        )

        # Consequential audit row (Requirements 45.5 / 46.6 / 57.1 /
        # AD-WS-5). Participates in the caller's transaction.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=recording_party_id,
            action_type=_ACTION_CREATE_MEASUREMENT_RECORD,
            target_id=measurement_record_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateMeasurementRecordResult(
            measurement_record_id=measurement_record_id,
            target_measurement_definition_revision_id=(
                target_measurement_definition_revision_id
            ),
            origin=origin,
            observed_value=observed_value,
            observed_value_unit=observed_value_unit,
            observation_time=observation_at,
            recording_party_id=recording_party_id,
            applicable_scope=applicable_scope,
            cites_relationship_id=cites_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
            source_system_id=source_system_id,
            source_system_record_id=source_system_record_id,
            source_system_authority=source_system_authority,
            source_system_retrieval_time=source_system_retrieval_at,
            import_at=import_at,
        )

    # -- read helper -------------------------------------------------------

    @staticmethod
    def get_measurement_record(
        connection: Connection,
        *,
        measurement_record_id: str,
    ) -> Optional[MeasurementRecordRow]:
        """Read-only lookup of a Measurement Record by its Identity.

        Backs the Observed Outcome anchoring rule (Requirement 47.2 / 47.4,
        AD-WS-40): the
        :class:`~walking_slice.outcome.observed_outcomes.ObservedOutcomeService`
        must (a) confirm every cited Measurement Record resolves and (b)
        recover each cited Record's target Measurement Definition **Resource**
        Identity so it can verify that Resource matches the single Measurement
        Definition Resource addressing the target Intended Outcome Resource.

        Introduces no write path. Returns ``None`` when the supplied Identity
        does not resolve so the caller can treat the absent case without
        try/except.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context.
            measurement_record_id: The Measurement Record Identity to resolve.

        Returns:
            A :class:`MeasurementRecordRow` snapshot when the Record exists;
            ``None`` otherwise.
        """
        row = connection.execute(
            text(
                "SELECT "
                "  measurement_record_id, "
                "  target_measurement_definition_id, "
                "  target_measurement_definition_revision_id, "
                "  origin, observed_value, observed_value_unit, "
                "  observation_time, recording_party_id, "
                "  applicable_scope, recorded_at "
                "FROM Measurement_Records "
                "WHERE measurement_record_id = :measurement_record_id"
            ),
            {"measurement_record_id": measurement_record_id},
        ).mappings().first()
        if row is None:
            return None
        return MeasurementRecordRow(
            measurement_record_id=row["measurement_record_id"],
            target_measurement_definition_id=(
                row["target_measurement_definition_id"]
            ),
            target_measurement_definition_revision_id=(
                row["target_measurement_definition_revision_id"]
            ),
            origin=row["origin"],
            observed_value=row["observed_value"],
            observed_value_unit=row["observed_value_unit"],
            observation_time=row["observation_time"],
            recording_party_id=row["recording_party_id"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    @staticmethod
    def _find_existing_imported_record(
        connection: Connection,
        *,
        target_measurement_definition_revision_id: str,
        source_system_id: str,
        source_system_record_id: str,
    ) -> Optional[str]:
        """Return the Identity of an existing imported Measurement Record
        matching the AD-WS-39 idempotency key, or ``None``.

        The idempotency key is the triple ``(target Measurement Definition
        Revision, source_system_id, source_system_record_id)`` scoped to
        ``origin = 'imported'`` (Requirement 46.3).
        """
        return connection.execute(
            text(
                "SELECT measurement_record_id FROM Measurement_Records "
                "WHERE origin = 'imported' "
                "  AND target_measurement_definition_revision_id = "
                "      :target_measurement_definition_revision_id "
                "  AND source_system_id = :source_system_id "
                "  AND source_system_record_id = :source_system_record_id"
            ),
            {
                "target_measurement_definition_revision_id": (
                    target_measurement_definition_revision_id
                ),
                "source_system_id": source_system_id,
                "source_system_record_id": source_system_record_id,
            },
        ).scalar_one_or_none()

    # -- denial side-channel ----------------------------------------------

    def _persist_measurement_record_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_measurement_definition_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Measurement Record attempt.

        Implements the Requirement 50.6 retry contract verbatim (mirroring the
        Slice 1/2/3 pattern and :class:`MeasurementDefinitionService`): each
        attempt opens a *new* :meth:`Engine.begin` transaction, tries
        :meth:`AuditLog.append_denial`, and either returns on success or pauses
        by the next entry in :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the
        next try. If every attempt fails,
        :class:`MeasurementRecordAuditFailureError` is raised.

        The separate transaction is essential: the caller's originating
        transaction is about to be rolled back when the create method raises
        :class:`MeasurementRecordAuthorizationError`. The Denial Record must
        therefore live outside that scope to survive (AD-WS-9 / Requirement
        50.6).
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=_ACTION_CREATE_MEASUREMENT_RECORD,
                        target_id=target_measurement_definition_revision_id,
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

        raise MeasurementRecordAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error

    # -- validation helpers ------------------------------------------------

    @staticmethod
    def _screen_request_attributes(
        request_attributes: Optional[Mapping[str, Any]],
    ) -> None:
        """Screen the raw request body for prohibited intended-side keys
        (Requirement 53) when the route layer forwarded it."""
        if request_attributes is None:
            return
        try:
            _reject_prohibited_attributes(
                request_attributes, OUTCOME_PROHIBITED_PREFIXES
            )
        except OutcomeValidationError as exc:
            raise MeasurementRecordValidationError(
                str(exc),
                failed_constraint="prohibited_attribute",
                invalid_attributes=exc.prohibited_keys,
                prohibited_keys=exc.prohibited_keys,
            ) from exc

    @staticmethod
    def _reject_native_source_system_attributes(
        request_attributes: Optional[Mapping[str, Any]],
    ) -> None:
        """Reject a native request body carrying any imported-only
        source-system attribute (Requirement 45.3).

        The typed ``create_native_measurement`` signature declares no
        source-system parameter, so only a raw request body forwarded by the
        HTTP layer can carry one. Matching is case-insensitive and
        hyphen/underscore-invariant.
        """
        if request_attributes is None:
            return
        reserved = {
            "source-system-id",
            "source-system-record-id",
            "source-system-authority",
            "source-system-authority-designation",
            "source-system-retrieval-time",
            "source-system-retrieval-at",
            "import-time",
            "import-at",
        }
        offending = [
            key
            for key in request_attributes.keys()
            if isinstance(key, str)
            and key.lower().replace("_", "-") in reserved
        ]
        if offending:
            raise MeasurementRecordValidationError(
                f"native Measurement Record request carries imported-only "
                f"source-system attribute(s) {offending!r}; these are "
                "reserved for imported Records (Requirement 45.3).",
                failed_constraint="native_source_system_attribute_supplied",
                invalid_attributes=tuple(offending),
            )

    @staticmethod
    def _normalize_observed_value(value: Any) -> str:
        """Validate the ≤ 6-fractional-digit rule and return the normalized
        canonical decimal string for persistence (Requirements 45.2 / 46.2).

        Coerces ``int`` / ``str`` (and ``float`` via its ``str`` form to avoid
        binary-float artifacts) to :class:`decimal.Decimal`. Rejects a value
        carrying more than six fractional digits. Normalizes via
        :meth:`Decimal.normalize` and formats with fixed-point notation
        (``format(..., 'f')``) so the persisted string never uses scientific
        notation — satisfying the ``Measurement_Records`` GLOB CHECK that the
        value begins with a digit or a leading minus sign.
        """
        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, bool):
            # bool is an int subclass; treat as invalid for an observed value.
            raise MeasurementRecordValidationError(
                "observed value must be a decimal number, not a boolean "
                "(Requirement 45.2).",
                failed_constraint="observed_value_invalid",
                invalid_attributes=("observed_value",),
            )
        elif isinstance(value, int):
            decimal_value = Decimal(value)
        elif isinstance(value, (str, float)):
            try:
                decimal_value = Decimal(str(value))
            except (InvalidOperation, ValueError) as exc:
                raise MeasurementRecordValidationError(
                    f"observed value {value!r} is not a valid decimal number "
                    "(Requirement 45.2).",
                    failed_constraint="observed_value_invalid",
                    invalid_attributes=("observed_value",),
                ) from exc
        else:
            raise MeasurementRecordValidationError(
                f"observed value {value!r} is not a valid decimal number "
                "(Requirement 45.2).",
                failed_constraint="observed_value_invalid",
                invalid_attributes=("observed_value",),
            )

        if not decimal_value.is_finite():
            raise MeasurementRecordValidationError(
                f"observed value {value!r} must be a finite decimal number "
                "(Requirement 45.2).",
                failed_constraint="observed_value_invalid",
                invalid_attributes=("observed_value",),
            )

        exponent = decimal_value.as_tuple().exponent
        fractional_digits = -exponent if exponent < 0 else 0
        if fractional_digits > _MAX_FRACTIONAL_DIGITS:
            raise MeasurementRecordValidationError(
                f"observed value {value!r} has {fractional_digits} fractional "
                f"digits; at most {_MAX_FRACTIONAL_DIGITS} are permitted "
                "(Requirement 45.2).",
                failed_constraint=(
                    "observed_value_too_many_fractional_digits"
                ),
                invalid_attributes=("observed_value",),
            )

        normalized = decimal_value.normalize()
        # Decimal('0').normalize() is Decimal('0'); negative-zero collapses to
        # '0' via format. format(..., 'f') never emits scientific notation.
        return format(normalized, "f")


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


def _is_present(value: Any) -> bool:
    """Return whether a required non-string attribute was supplied."""
    return value is not None


def _is_present_str(value: Any) -> bool:
    """Return whether a required string attribute was supplied and non-empty."""
    return isinstance(value, str) and len(value) > 0


def _observation_within_window(
    observation_time: datetime,
    observation_window: str,
) -> bool:
    """Return whether ``observation_time`` falls within the Definition's
    observation-window descriptor.

    The observation-window descriptor (Requirement 44.2) is free text of
    1..1000 characters. When it is expressed as an ISO-8601 closed interval
    of the form ``<start>/<end>`` (two UTC ISO-8601 timestamps separated by a
    single ``/``), the observation time must fall within ``[start, end]``
    inclusive — the window edges are accepted, values outside are rejected
    (design §"Testing Strategy" — observation-window boundary). A half-open
    descriptor naming only a start (``<start>/``) or only an end (``/<end>``)
    bounds that side alone.

    When the descriptor is not parseable as such an interval it is treated as
    a purely informational descriptor that imposes no machine-checkable bound,
    so the observation time is accepted. This keeps free-text windows (e.g.,
    "every quarter") from producing spurious rejections while still enforcing
    a structured interval when one is supplied.
    """
    raw = observation_window.strip()
    if "/" not in raw:
        return True

    start_text, _, end_text = raw.partition("/")
    start_dt = _try_parse_iso8601(start_text.strip())
    end_dt = _try_parse_iso8601(end_text.strip())

    # Neither bound parsed — descriptor is free text, impose no bound.
    if start_dt is None and end_dt is None:
        return True

    observation_utc = _to_utc(observation_time)
    if start_dt is not None and observation_utc < start_dt:
        return False
    if end_dt is not None and observation_utc > end_dt:
        return False
    return True


def _try_parse_iso8601(value: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, returning ``None`` on failure.

    Accepts a trailing ``Z`` (treated as ``+00:00``). A successfully parsed
    naive timestamp is assumed UTC. Returns a timezone-aware UTC datetime.
    """
    if not value:
        return None
    candidate = value
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    return _to_utc(parsed)


def _to_utc(value: datetime) -> datetime:
    """Normalize a datetime to a timezone-aware UTC datetime.

    A naive datetime is assumed to already be UTC (matching the slice-wide
    convention that all timestamps are UTC).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit row, the
    (separate-transaction) Denial Record, and the consequential audit row
    produced for the same logical Measurement Record creation.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Measurement Record
    Identity in ``Identifier_Registry``, mirroring the Slice 1/2/3 convention.
    """
    return hashlib.sha256(content).hexdigest()
