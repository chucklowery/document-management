"""Outcome_Service.ObservedOutcomes — Observed Outcome Resources and their
successive immutable Revisions (predecessor chain, AD-WS-36), each with an
``Addresses`` Relationship to the target Intended Outcome Revision and one
``Cites`` Relationship per cited Measurement Record.

Design reference
================

``.kiro/specs/fourth-walking-slice/design.md``:

- §"Outcome_Service.ObservedOutcomes" — the public dataclass surface
  (``create_observed_outcome`` and ``revise_observed_outcome``), the authority
  mapping (``create.observed_outcome`` → ``assess_outcome`` per AD-WS-33), the
  validation rules (Requirement 47), and the AD-WS-36 append-only predecessor
  chain (optimistic concurrency on ``predecessor_revision_id``).
- §"Relationships rows written by Slice 4" — an Observed Outcome Revision
  writes one ``Addresses`` Relationship to the target Intended Outcome Revision
  (``semantic_role IS NULL``) and one ``Cites`` Relationship per cited
  Measurement Record (``semantic_role = 'observation_basis'``, AD-WS-35).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time shared by
  every row in the transaction), Identifiers (every new identity is a UUIDv7
  minted by :class:`IdentityService` and registered in ``Identifier_Registry``
  with the additive Slice 4 ``resource_kind`` tag per AD-WS-37), Authorization
  (the deny path reuses the cumulative separate-transaction Denial-Record
  pattern with the Requirement 50.6 three-attempt retry).

Task scope (task 7.1)
=====================

This module implements
:meth:`ObservedOutcomeService.create_observed_outcome` and
:meth:`ObservedOutcomeService.revise_observed_outcome`.

**Validation (Requirement 47.2/47.4).** Reject when: the assessment summary is
omitted or outside 1..4000 characters; the applicable scope is omitted; zero
Measurement Records are cited; the request body carries a prohibited
intended-side attribute (Requirement 53) or an ``outcome_kind`` value other
than ``'observed'``; the target Intended Outcome Revision does not resolve or
its ``outcome_kind != 'intended'``; any cited Measurement Record does not
resolve or its target Measurement Definition **Resource** does not match the
single Measurement Definition Resource that addresses the target Intended
Outcome Resource (anchored via the single Measurement Definition per AD-WS-40);
and, on revise, the supplied ``predecessor_revision_id`` does not equal the
current most-recent Revision for the Resource (optimistic concurrency,
AD-WS-36).

**Persistence (Requirement 47.1/47.2/47.3/47.6/57.1).** On a permit outcome,
inside the caller's transaction: on create, INSERT the ``Observed_Outcomes``
Resource header plus the initial ``Observed_Outcome_Revisions`` row
(``predecessor_revision_id`` NULL); on revise, INSERT the next
``Observed_Outcome_Revisions`` row with ``predecessor_revision_id`` set to the
prior most-recent Revision and leave every prior Revision byte-equivalent.
Every Revision records ``outcome_kind = 'observed'`` (Requirement 47.2). Also
INSERT one ``Addresses`` Relationship to the target Intended Outcome Revision
(``semantic_role IS NULL``), one ``Cites`` Relationship per cited Measurement
Record (``semantic_role = 'observation_basis'``), and the consequential
``Audit_Records`` row — all in one transaction so a failure anywhere rolls
every row back. The addressed Intended Outcome Revision is never mutated
(Requirement 47.8).

Requirements satisfied
======================

    47.1 — authorized Observed Outcome creation produces one Resource and one
           initial immutable Revision.
    47.2 — every Observed Outcome Revision records the resource + revision
           identities, ``outcome_kind = 'observed'``, the target Intended
           Outcome Resource + Revision identities, the assessment summary
           (1..4000), the cited Measurement Record identities (each anchored to
           the addressing Measurement Definition Resource), the predecessor
           Revision Identity, the authoring Party Identity, the applicable
           scope, the recorded time, one ``Addresses`` Relationship, and one
           ``Cites`` Relationship per cited Measurement Record.
    47.3 — a new Revision is appended via the predecessor chain; prior
           Revisions are left byte-equivalent.
    47.4 — unresolvable / non-``intended`` target, zero citations, an
           unresolvable or mismatched cited Record, a stale
           ``predecessor_revision_id``, an omitted summary / scope, or an
           ``outcome_kind`` other than ``'observed'`` are rejected with nothing
           persisted and each invalid attribute identified.
    47.5 — unauthorized requests are denied; no Resource or Revision is created
           and a Denial Record conforming to AD-WS-9 is appended.
    47.6 — every successful Revision insertion appends one immutable
           consequential audit row in the same transaction.
    47.7 — Observed Outcome Revisions and their Relationships are immutable
           (enforced by the schema triggers).
    47.8 — the addressed Intended Outcome Revision is never mutated.
    52.8 — ``create.observed_outcome`` requires ``assess_outcome`` (AD-WS-33).
    53.2 — no Observed Outcome creation request may carry an intended-side
           attribute.
    57.1 — the consequential audit append participates in the same transaction
           as the domain rows.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid_utils
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.outcome._helpers import (
    OUTCOME_PROHIBITED_PREFIXES,
    OutcomeValidationError,
    _record_outcome_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.models import CreateObservedOutcomeResult, ObservedOutcomeRevisionRow


__all__ = [
    "ObservedOutcomeAuditFailureError",
    "ObservedOutcomeAuthorizationError",
    "ObservedOutcomeCitationError",
    "ObservedOutcomeConcurrencyError",
    "ObservedOutcomeService",
    "ObservedOutcomeTargetNotResolvableError",
    "ObservedOutcomeValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# ``create.observed_outcome`` maps to the ``assess_outcome`` authority per
# AD-WS-33. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 47.6) and on the separate-transaction
# Denial Record (Requirement 47.5).
_ACTION_CREATE_OBSERVED_OUTCOME: Final[str] = "create.observed_outcome"

# Relationship Type, source/target ``kind`` strings, and the AD-WS-35
# ``semantic_role`` values written to the ``Relationships`` rows.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_CITES: Final[str] = "Cites"
_SEMANTIC_ROLE_OBSERVATION_BASIS: Final[str] = "observation_basis"
_KIND_OBSERVED_OUTCOME_REVISION: Final[str] = "observed_outcome_revision"
_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"
_KIND_MEASUREMENT_RECORD: Final[str] = "measurement_record"

# Identifier_Registry registration kinds (Slice 1 enumeration) and the Slice 4
# ``resource_kind`` tags (AD-WS-37).
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_OBSERVED_OUTCOME: Final[str] = "observed_outcome"
_RESOURCE_KIND_OBSERVED_OUTCOME_REVISION: Final[str] = (
    "observed_outcome_revision"
)

# Outcome-kind discriminators (domain-model §7.4 invariant 6).
_OUTCOME_KIND_OBSERVED: Final[str] = "observed"
_OUTCOME_KIND_INTENDED: Final[str] = "intended"

# Assessment-summary length bounds (Requirement 47.2).
_ASSESSMENT_SUMMARY_MIN_CHARS: Final[int] = 1
_ASSESSMENT_SUMMARY_MAX_CHARS: Final[int] = 4_000

# Exponential backoff sequence for retrying the separate-transaction Denial
# Record append (Requirement 50.6, mirroring the Slice 1/2/3 pattern). Three
# retries after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ObservedOutcomeValidationError(ValueError):
    """Raised when an Observed Outcome submission fails Requirement 47.4 / 53
    validation.

    ``failed_constraint`` names the first specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a stable
    identifier rather than the message text. ``invalid_attributes`` lists every
    rejected attribute name so the response can identify each one
    (Requirement 47.4 — "return an error indication identifying each invalid
    attribute").

    Attributes:
        failed_constraint: A stable discriminator such as
            ``"assessment_summary_missing"``,
            ``"assessment_summary_too_long"``,
            ``"applicable_scope_missing"``,
            ``"authoring_party_id_missing"``,
            ``"no_cited_measurement_records"``,
            ``"outcome_kind_invalid"``,
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


class ObservedOutcomeTargetNotResolvableError(LookupError):
    """Raised when the target Intended Outcome Revision does not resolve or is
    not an Intended Outcome (Requirement 47.4).

    Requirement 47.4 requires the named target Intended Outcome Revision
    Identity to resolve to an existing Intended Outcome Revision whose
    ``outcome_kind`` is the literal ``'intended'``. The exception carries the
    offending identifier and a ``failed_constraint`` discriminator
    distinguishing the unresolvable case from the wrong-kind case.
    """

    def __init__(
        self,
        *,
        target_intended_outcome_revision_id: str,
        failed_constraint: str,
    ) -> None:
        super().__init__(
            f"Target Intended Outcome Revision "
            f"{target_intended_outcome_revision_id!r} is not a usable Observed "
            f"Outcome target ({failed_constraint}, Requirement 47.4)."
        )
        self.target_intended_outcome_revision_id = (
            target_intended_outcome_revision_id
        )
        self.failed_constraint = failed_constraint


class ObservedOutcomeCitationError(ValueError):
    """Raised when a cited Measurement Record does not resolve or is not
    anchored to the addressing Measurement Definition (Requirement 47.2/47.4).

    Every cited Measurement Record must resolve and its target Measurement
    Definition **Resource** Identity must equal the single Measurement
    Definition Resource that addresses the target Intended Outcome Resource
    (AD-WS-40). The exception lists every offending cited Identity so the HTTP
    layer can identify each invalid attribute. ``failed_constraint`` is
    ``"cited_measurement_record_unresolvable"`` when one or more cited Records
    do not resolve, ``"cited_measurement_record_definition_mismatch"`` when one
    or more cited Records resolve but are anchored to a different Measurement
    Definition Resource, or ``"no_addressing_measurement_definition"`` when no
    Measurement Definition addresses the target Intended Outcome Resource at
    all (so no citation could ever match).
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        invalid_measurement_record_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.invalid_measurement_record_ids = invalid_measurement_record_ids
        self.invalid_attributes = ("cited_measurement_record_ids",)


class ObservedOutcomeConcurrencyError(ValueError):
    """Raised when a revision request fails the AD-WS-36 optimistic-concurrency
    check (Requirement 47.4).

    A new Observed Outcome Revision must supply a ``predecessor_revision_id``
    equal to the current most-recent Revision for the named Observed Outcome
    Resource. The exception carries the supplied predecessor and the actual
    most-recent Revision (when the Resource resolves) so the HTTP layer can
    render an actionable 409; no new Revision is appended and every prior
    Revision is left byte-equivalent. ``failed_constraint`` is
    ``"observed_outcome_not_resolvable"`` when the named Resource does not
    exist, or ``"stale_predecessor_revision"`` when the supplied predecessor is
    not the current most-recent Revision.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        observed_outcome_id: str,
        supplied_predecessor_revision_id: Optional[str] = None,
        current_revision_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.observed_outcome_id = observed_outcome_id
        self.supplied_predecessor_revision_id = (
            supplied_predecessor_revision_id
        )
        self.current_revision_id = current_revision_id
        self.invalid_attributes = ("predecessor_revision_id",)


class ObservedOutcomeAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies an Observed Outcome
    attempt (Requirement 47.5).

    Carries only ``reason_code`` and ``correlation_id`` — the AD-WS-9
    indistinguishable-denial contract forbids leaking authorized Party
    identities, target contents, or target existence beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Observed Outcome creation denied: "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class ObservedOutcomeAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 50.6).

    On total audit-append failure the exception is raised *in place of*
    :class:`ObservedOutcomeAuthorizationError` — denial and audit have silently
    diverged and the operator must be told. The caller's transaction still
    rolls back so no Resource, Revision, Relationship, or consequential audit
    row is persisted.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Observed Outcome failed after "
            f"{attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedOutcomeService:
    """Persist Observed Outcome Resources and their successive immutable
    Revisions (predecessor chain, AD-WS-36), each with an ``Addresses``
    Relationship to the target Intended Outcome Revision and one ``Cites``
    Relationship per cited Measurement Record.

    Connection-scoped at call time: both write methods accept the caller's
    :class:`sqlalchemy.engine.Connection` and write inside the caller's
    transaction (AD-WS-5). The service instance therefore holds only the
    cross-request collaborators and can be shared across requests.

    Frozen because design §"Outcome_Service.ObservedOutcomes" declares it
    ``@dataclass(frozen=True)``.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Observed_Outcomes`` (on create), ``Observed_Outcome_Revisions``,
            ``Relationships``, and ``Audit_Records`` rows. Consulted exactly
            once per write so every artifact of the transaction shares one
            timestamp.
        identity_service: Generates Observed Outcome Resource and Revision
            Identities and persists their ``Identifier_Registry`` bindings with
            the Slice 4 ``resource_kind`` tags (AD-WS-37).
        audit_log: Appends the consequential audit row (Requirement 47.6)
            inside the caller's transaction.
        authorization_service: Evaluates ``create.observed_outcome`` →
            ``assess_outcome`` authority per AD-WS-33 / Requirement 47.5; the
            deny path is the cumulative separate-transaction Denial-Record
            pattern.
        intended_outcome_reader: The Slice 2 :class:`IntendedOutcomeService`
            used read-only (``get_revision``) to resolve the target Intended
            Outcome Revision and verify ``outcome_kind = 'intended'`` (AD-WS-40
            / Requirement 47.4).
        measurement_reader: The :class:`MeasurementRecordService` used
            read-only (``get_measurement_record``) to resolve each cited
            Measurement Record and recover its target Measurement Definition
            Resource Identity for the anchoring check (Requirement 47.2/47.4).
        definition_reader: The :class:`MeasurementDefinitionService` used
            read-only (``get_definition_for_intended_outcome``) to resolve the
            single Measurement Definition Resource that addresses the target
            Intended Outcome Resource — the anchor every cited Measurement
            Record must match (AD-WS-40).
        denial_audit_sleep: Sleep function used to pause between retries of the
            Denial Record append. Defaults to :func:`time.sleep`; tests inject
            a recording stub so the retry sequence is observable without
            spending real time.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: AuthorizationService
    intended_outcome_reader: IntendedOutcomeService
    measurement_reader: MeasurementRecordService
    definition_reader: MeasurementDefinitionService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface: create -------------------------------------------

    def create_observed_outcome(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
        assessment_summary: str,
        cited_measurement_record_ids: Sequence[str],
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        outcome_kind: Optional[str] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateObservedOutcomeResult:
        """Create an Observed Outcome Resource plus its initial immutable
        Revision, its ``Addresses`` Relationship to the target Intended Outcome
        Revision, and one ``Cites`` Relationship per cited Measurement Record.

        Per Requirements 47.1 through 47.8, 52.8 (``assess_outcome``), 53.2,
        57.1, AD-WS-9 (indistinguishable denial), AD-WS-33, AD-WS-35, AD-WS-36,
        AD-WS-37, and AD-WS-40.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_intended_outcome_revision_id: Identity of the single target
                Intended Outcome Revision (Requirement 47.4).
            assessment_summary: Assessment-summary text (1..4000 chars).
            cited_measurement_record_ids: At least one cited Measurement Record
                Identity; each must resolve and be anchored to the Measurement
                Definition Resource that addresses the target Intended Outcome
                Resource (Requirement 47.2/47.4).
            authoring_party_id: Identity of the authoring Party.
            applicable_scope: Scope the Observed Outcome applies within; also
                passed as ``target.scope`` to authorization.
            engine: Required for the deny path's separate-transaction Denial
                Record write and the separate-transaction authorization
                evaluation.
            correlation_id: Optional correlation identifier; a UUIDv7 is
                generated when omitted.
            evaluation_at: Optional explicit ``at`` for authorization; defaults
                to the recorded time.
            outcome_kind: Optional explicit outcome-kind indicator; rejected
                when supplied as anything other than ``'observed'``
                (Requirement 47.4).
            request_attributes: Optional raw request body screened against the
                intended-side prefix list (Requirement 53).

        Returns:
            :class:`CreateObservedOutcomeResult` with ``predecessor_revision_id
            = None`` (the initial Revision).

        Raises:
            ObservedOutcomeValidationError: A required attribute is missing,
                zero Records are cited, ``outcome_kind`` was supplied with a
                value other than ``'observed'``, or the request body carried a
                prohibited intended-side attribute.
            ObservedOutcomeTargetNotResolvableError: The target Intended
                Outcome Revision did not resolve or its ``outcome_kind`` is not
                ``'intended'``.
            ObservedOutcomeCitationError: A cited Measurement Record did not
                resolve or is anchored to a different Measurement Definition
                Resource.
            ObservedOutcomeAuthorizationError: The attempt was denied; the
                Denial Record was appended in a separate transaction.
            ObservedOutcomeAuditFailureError: The attempt was denied and the
                Denial Record append failed on every retry.
        """
        cited_ids = self._validate_common_inputs(
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_measurement_record_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            outcome_kind=outcome_kind,
            request_attributes=request_attributes,
        )

        # Resolve the target Intended Outcome Revision via the additive
        # read-only Planning API (AD-WS-40). Reject when it does not resolve or
        # its outcome_kind is not 'intended' (Requirement 47.4). The check runs
        # before authorization so the deny path never reveals whether a target
        # exists for an unauthorized caller.
        target_intended_outcome_resource_id = self._resolve_intended_outcome(
            connection,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
        )

        # Anchor + resolve every cited Measurement Record (Requirement
        # 47.2/47.4).
        self._validate_citations(
            connection,
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            cited_measurement_record_ids=cited_ids,
        )

        return self._persist_observed_outcome(
            connection,
            engine=engine,
            observed_outcome_id=None,
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            predecessor_revision_id=None,
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            correlation_id=correlation_id,
            evaluation_at=evaluation_at,
        )

    # -- public surface: revise -------------------------------------------

    def revise_observed_outcome(
        self,
        connection: Connection,
        *,
        observed_outcome_id: str,
        predecessor_revision_id: str,
        assessment_summary: str,
        cited_measurement_record_ids: Sequence[str],
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        outcome_kind: Optional[str] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateObservedOutcomeResult:
        """Append a new immutable Observed Outcome Revision to an existing
        Observed Outcome Resource (predecessor chain, AD-WS-36).

        Per Requirements 47.3 through 47.8, 52.8 (``assess_outcome``), 53.2,
        57.1, AD-WS-9, AD-WS-33, AD-WS-35, AD-WS-36, AD-WS-37, and AD-WS-40.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            observed_outcome_id: Identity of the existing Observed Outcome
                Resource being revised.
            predecessor_revision_id: Must equal the current most-recent
                Observed Outcome Revision Identity for the Resource (optimistic
                concurrency, AD-WS-36); rejected otherwise (Requirement 47.4).
            assessment_summary: Revised assessment-summary text (1..4000
                chars).
            cited_measurement_record_ids: At least one cited Measurement Record
                Identity; each must resolve and be anchored to the Measurement
                Definition Resource that addresses the target Intended Outcome
                Resource (Requirement 47.2/47.4).
            authoring_party_id: Identity of the authoring Party.
            applicable_scope: Scope the Observed Outcome applies within.
            engine: Required for the deny path and the separate-transaction
                authorization evaluation.
            correlation_id: Optional correlation identifier.
            evaluation_at: Optional explicit ``at`` for authorization.
            outcome_kind: Optional explicit outcome-kind indicator; rejected
                when supplied as anything other than ``'observed'``.
            request_attributes: Optional raw request body screened against the
                intended-side prefix list (Requirement 53).

        Returns:
            :class:`CreateObservedOutcomeResult` with
            ``predecessor_revision_id`` set to the prior most-recent Revision.

        Raises:
            ObservedOutcomeValidationError: A required attribute is missing,
                zero Records are cited, ``outcome_kind`` was supplied with a
                value other than ``'observed'``, or the request body carried a
                prohibited intended-side attribute.
            ObservedOutcomeConcurrencyError: The named Resource does not
                resolve, or the supplied ``predecessor_revision_id`` is not the
                current most-recent Revision (Requirement 47.4 / AD-WS-36).
            ObservedOutcomeTargetNotResolvableError: The inherited target
                Intended Outcome Revision no longer resolves or is not
                ``'intended'``.
            ObservedOutcomeCitationError: A cited Measurement Record did not
                resolve or is anchored to a different Measurement Definition
                Resource.
            ObservedOutcomeAuthorizationError: The attempt was denied.
            ObservedOutcomeAuditFailureError: The denial audit append failed.
        """
        cited_ids = self._validate_common_inputs(
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_measurement_record_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            outcome_kind=outcome_kind,
            request_attributes=request_attributes,
        )

        # Resolve the existing Resource and its current most-recent Revision.
        # Enforce the AD-WS-36 optimistic-concurrency rule: the supplied
        # predecessor must equal the current tail of the chain (Requirement
        # 47.4). The inherited target Intended Outcome Revision is recovered
        # from the current Revision so the new Revision addresses the same
        # Intended Outcome Revision.
        current = self._resolve_current_revision(
            connection, observed_outcome_id=observed_outcome_id
        )
        if current is None:
            raise ObservedOutcomeConcurrencyError(
                f"Observed Outcome Resource {observed_outcome_id!r} does not "
                "resolve; no Revision can be appended (Requirement 47.4).",
                failed_constraint="observed_outcome_not_resolvable",
                observed_outcome_id=observed_outcome_id,
                supplied_predecessor_revision_id=predecessor_revision_id,
            )
        current_revision_id, inherited_target_revision_id = current
        if predecessor_revision_id != current_revision_id:
            raise ObservedOutcomeConcurrencyError(
                f"supplied predecessor Revision "
                f"{predecessor_revision_id!r} is not the current most-recent "
                f"Revision {current_revision_id!r} for Observed Outcome "
                f"{observed_outcome_id!r}; the Revision is stale (optimistic "
                "concurrency, AD-WS-36 / Requirement 47.4).",
                failed_constraint="stale_predecessor_revision",
                observed_outcome_id=observed_outcome_id,
                supplied_predecessor_revision_id=predecessor_revision_id,
                current_revision_id=current_revision_id,
            )

        # Re-resolve the inherited target Intended Outcome Revision (AD-WS-40 /
        # Requirement 47.4) to recover its Resource Identity and re-verify
        # outcome_kind = 'intended'.
        target_intended_outcome_resource_id = self._resolve_intended_outcome(
            connection,
            target_intended_outcome_revision_id=inherited_target_revision_id,
        )

        self._validate_citations(
            connection,
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            cited_measurement_record_ids=cited_ids,
        )

        return self._persist_observed_outcome(
            connection,
            engine=engine,
            observed_outcome_id=observed_outcome_id,
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            target_intended_outcome_revision_id=inherited_target_revision_id,
            predecessor_revision_id=predecessor_revision_id,
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            correlation_id=correlation_id,
            evaluation_at=evaluation_at,
        )

    # -- shared validation -------------------------------------------------

    def _validate_common_inputs(
        self,
        *,
        assessment_summary: str,
        cited_measurement_record_ids: Sequence[str],
        authoring_party_id: str,
        applicable_scope: str,
        outcome_kind: Optional[str],
        request_attributes: Optional[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        """Validate the request inputs shared by create and revise.

        Screens the raw request body for prohibited intended-side keys
        (Requirement 53) and for an ``outcome_kind`` value other than
        ``'observed'`` (Requirement 47.4), then validates the required
        attributes and the at-least-one-citation rule. Returns the normalized
        tuple of cited Measurement Record Identities preserving caller order.
        """
        self._screen_request_attributes(request_attributes)

        # outcome_kind, when supplied at all, must be the literal 'observed'
        # (Requirement 47.4). The typed kwarg and the raw request body are both
        # screened.
        self._reject_invalid_outcome_kind(outcome_kind, request_attributes)

        invalid: list[str] = []
        if not _is_present_str(assessment_summary):
            invalid.append("assessment_summary")
        if not _is_present_str(applicable_scope):
            invalid.append("applicable_scope")
        if not _is_present_str(authoring_party_id):
            invalid.append("authoring_party_id")
        if invalid:
            raise ObservedOutcomeValidationError(
                f"required Observed Outcome attribute(s) {invalid!r} are "
                "missing (Requirement 47.4).",
                failed_constraint=f"{invalid[0]}_missing",
                invalid_attributes=tuple(invalid),
            )

        # Assessment summary upper bound (Requirement 47.2). The lower bound is
        # covered by the presence check above.
        if len(assessment_summary) > _ASSESSMENT_SUMMARY_MAX_CHARS:
            raise ObservedOutcomeValidationError(
                f"assessment summary is {len(assessment_summary)} characters; "
                f"at most {_ASSESSMENT_SUMMARY_MAX_CHARS} are permitted "
                "(Requirement 47.2).",
                failed_constraint="assessment_summary_too_long",
                invalid_attributes=("assessment_summary",),
            )

        # At least one cited Measurement Record (Requirement 47.4).
        if cited_measurement_record_ids is None:
            cited_ids: tuple[str, ...] = ()
        else:
            cited_ids = tuple(cited_measurement_record_ids)
        if len(cited_ids) == 0:
            raise ObservedOutcomeValidationError(
                "an Observed Outcome must cite at least one Measurement "
                "Record (Requirement 47.4).",
                failed_constraint="no_cited_measurement_records",
                invalid_attributes=("cited_measurement_record_ids",),
            )
        # Each cited Identity must be a non-empty string.
        if any(not _is_present_str(cid) for cid in cited_ids):
            raise ObservedOutcomeValidationError(
                "every cited Measurement Record Identity must be a non-empty "
                "string (Requirement 47.4).",
                failed_constraint="no_cited_measurement_records",
                invalid_attributes=("cited_measurement_record_ids",),
            )

        return cited_ids

    def _resolve_intended_outcome(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
    ) -> str:
        """Resolve the target Intended Outcome Revision and return its target
        Intended Outcome **Resource** Identity.

        Rejects when the Revision does not resolve or its ``outcome_kind`` is
        not the literal ``'intended'`` (Requirement 47.4, AD-WS-40).
        """
        revision_row = self.intended_outcome_reader.get_revision(
            connection, target_intended_outcome_revision_id
        )
        if revision_row is None:
            raise ObservedOutcomeTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                failed_constraint="target_intended_outcome_not_resolvable",
            )
        if revision_row.outcome_kind != _OUTCOME_KIND_INTENDED:
            raise ObservedOutcomeTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                failed_constraint="target_outcome_kind_not_intended",
            )
        return revision_row.intended_outcome_id

    def _validate_citations(
        self,
        connection: Connection,
        *,
        target_intended_outcome_resource_id: str,
        cited_measurement_record_ids: tuple[str, ...],
    ) -> None:
        """Resolve every cited Measurement Record and confirm each is anchored
        to the Measurement Definition Resource that addresses the target
        Intended Outcome Resource (Requirement 47.2/47.4, AD-WS-40).

        The single Measurement Definition Resource that addresses the target
        Intended Outcome Resource is the anchor (at most one exists per
        Intended Outcome Resource, Requirement 44.3). When no such Definition
        exists, no citation could ever match, so the whole request is rejected.
        """
        anchor = self.definition_reader.get_definition_for_intended_outcome(
            connection,
            intended_outcome_resource_id=target_intended_outcome_resource_id,
        )
        if anchor is None:
            raise ObservedOutcomeCitationError(
                "no Measurement Definition addresses the target Intended "
                f"Outcome Resource {target_intended_outcome_resource_id!r}; no "
                "cited Measurement Record can be anchored to it "
                "(Requirement 47.2/47.4).",
                failed_constraint="no_addressing_measurement_definition",
                invalid_measurement_record_ids=cited_measurement_record_ids,
            )
        anchor_definition_id = anchor.measurement_definition_id

        unresolvable: list[str] = []
        mismatched: list[str] = []
        for cited_id in cited_measurement_record_ids:
            record = self.measurement_reader.get_measurement_record(
                connection, measurement_record_id=cited_id
            )
            if record is None:
                unresolvable.append(cited_id)
                continue
            if record.target_measurement_definition_id != anchor_definition_id:
                mismatched.append(cited_id)

        # Unresolvable citations are reported first (they are the more
        # fundamental failure), then the anchor mismatch.
        if unresolvable:
            raise ObservedOutcomeCitationError(
                f"cited Measurement Record(s) {unresolvable!r} do not resolve "
                "(Requirement 47.4).",
                failed_constraint="cited_measurement_record_unresolvable",
                invalid_measurement_record_ids=tuple(unresolvable),
            )
        if mismatched:
            raise ObservedOutcomeCitationError(
                f"cited Measurement Record(s) {mismatched!r} are not anchored "
                f"to the Measurement Definition Resource "
                f"{anchor_definition_id!r} that addresses the target Intended "
                "Outcome Resource (Requirement 47.2/47.4).",
                failed_constraint=(
                    "cited_measurement_record_definition_mismatch"
                ),
                invalid_measurement_record_ids=tuple(mismatched),
            )

    # -- shared persistence ------------------------------------------------

    def _persist_observed_outcome(
        self,
        connection: Connection,
        *,
        engine: Engine,
        observed_outcome_id: Optional[str],
        target_intended_outcome_resource_id: str,
        target_intended_outcome_revision_id: str,
        predecessor_revision_id: Optional[str],
        assessment_summary: str,
        cited_measurement_record_ids: tuple[str, ...],
        authoring_party_id: str,
        applicable_scope: str,
        correlation_id: Optional[str],
        evaluation_at: Optional[datetime],
    ) -> CreateObservedOutcomeResult:
        """Authorize and persist an Observed Outcome Revision, its
        ``Addresses`` Relationship, one ``Cites`` Relationship per cited
        Measurement Record, and the consequential audit row.

        Shared by both create (``observed_outcome_id is None`` → mint a new
        Resource Identity and INSERT the ``Observed_Outcomes`` header) and
        revise (``observed_outcome_id`` supplied → append to the existing
        Resource). The authorization evaluation runs on a SEPARATE transaction;
        on deny it drives the AD-WS-9 separate-transaction Denial-Record
        pattern with the Requirement 50.6 retry. On permit it inserts the
        registry binding(s), the Resource header (create only), the
        ``Observed_Outcome_Revisions`` row, the ``Addresses`` and ``Cites``
        Relationships, and the consequential audit row — all inside the
        caller's transaction so a failure anywhere rolls every row back
        (Requirements 47.1/47.2/47.3/47.6, 57.1).
        """
        is_create = observed_outcome_id is None
        correlation = correlation_id or _new_correlation_id()
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # Authorization evaluation on a SEPARATE transaction. The TargetRef is
        # the target Intended Outcome Revision so the wired role assignment
        # must cover the same scope to permit the action.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=authoring_party_id,
                action=_ACTION_CREATE_OBSERVED_OUTCOME,
                target=TargetRef(
                    kind=_KIND_INTENDED_OUTCOME_REVISION,
                    id=target_intended_outcome_resource_id,
                    revision_id=target_intended_outcome_revision_id,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_observed_outcome_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise ObservedOutcomeAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # Mint identifiers (AD-WS-37). On create, mint the Resource Identity;
        # always mint the new Revision Identity and the Relationship Identities.
        if is_create:
            observed_outcome_id = str(self.identity_service.new_resource_id())
        assert observed_outcome_id is not None  # narrowed for the type checker
        observed_outcome_revision_id = str(
            self.identity_service.new_revision_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        cites_relationship_ids = tuple(
            str(self.identity_service.new_relationship_id())
            for _ in cited_measurement_record_ids
        )

        content_digest = _sha256_hex(
            json.dumps(
                {
                    "observed_outcome_id": observed_outcome_id,
                    "outcome_kind": _OUTCOME_KIND_OBSERVED,
                    "target_intended_outcome_resource_id": (
                        target_intended_outcome_resource_id
                    ),
                    "target_intended_outcome_revision_id": (
                        target_intended_outcome_revision_id
                    ),
                    "assessment_summary": assessment_summary,
                    "cited_measurement_record_ids": list(
                        cited_measurement_record_ids
                    ),
                    "predecessor_revision_id": predecessor_revision_id,
                    "authoring_party_id": authoring_party_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # Register the Resource Identity (create only) and the new Revision
        # Identity in ``Identifier_Registry`` carrying the AD-WS-37
        # ``resource_kind`` tags.
        if is_create:
            _record_outcome_artifact(
                connection,
                _REGISTRY_KIND_RESOURCE,
                _RESOURCE_KIND_OBSERVED_OUTCOME,
                observed_outcome_id,
                content_digest,
                identity_service=self.identity_service,
                actor_party_id=authoring_party_id,
                correlation_id=correlation,
                attempted_action=_ACTION_CREATE_OBSERVED_OUTCOME,
                recorded_time=recorded_time,
            )
        _record_outcome_artifact(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_OBSERVED_OUTCOME_REVISION,
            observed_outcome_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_OBSERVED_OUTCOME,
            recorded_time=recorded_time,
        )

        # Insert the Resource header on create. ``created_at`` carries the same
        # recorded time as the initial Revision so the two rows' timestamps are
        # byte-equivalent.
        if is_create:
            connection.execute(
                text(
                    """
                    INSERT INTO Observed_Outcomes (
                        observed_outcome_id,
                        target_intended_outcome_resource_id,
                        created_at
                    ) VALUES (
                        :observed_outcome_id,
                        :target_intended_outcome_resource_id,
                        :created_at
                    )
                    """
                ),
                {
                    "observed_outcome_id": observed_outcome_id,
                    "target_intended_outcome_resource_id": (
                        target_intended_outcome_resource_id
                    ),
                    "created_at": recorded_at,
                },
            )

        # Insert the immutable Observed Outcome Revision (Requirement 47.2).
        # Every Revision records outcome_kind = 'observed'.
        connection.execute(
            text(
                """
                INSERT INTO Observed_Outcome_Revisions (
                    observed_outcome_revision_id,
                    observed_outcome_id,
                    outcome_kind,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    assessment_summary,
                    predecessor_revision_id,
                    authoring_party_id,
                    applicable_scope,
                    recorded_at
                ) VALUES (
                    :observed_outcome_revision_id,
                    :observed_outcome_id,
                    :outcome_kind,
                    :target_intended_outcome_resource_id,
                    :target_intended_outcome_revision_id,
                    :assessment_summary,
                    :predecessor_revision_id,
                    :authoring_party_id,
                    :applicable_scope,
                    :recorded_at
                )
                """
            ),
            {
                "observed_outcome_revision_id": observed_outcome_revision_id,
                "observed_outcome_id": observed_outcome_id,
                "outcome_kind": _OUTCOME_KIND_OBSERVED,
                "target_intended_outcome_resource_id": (
                    target_intended_outcome_resource_id
                ),
                "target_intended_outcome_revision_id": (
                    target_intended_outcome_revision_id
                ),
                "assessment_summary": assessment_summary,
                "predecessor_revision_id": predecessor_revision_id,
                "authoring_party_id": authoring_party_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # Single ``Addresses`` Relationship to the target Intended Outcome
        # Revision (AD-WS-35, semantic_role IS NULL). The source is the
        # Observed Outcome Revision (a Revision-bearing source, so source_id is
        # the Resource Identity and source_revision_id the Revision Identity).
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
                    :authoring_party_id, :recorded_at, NULL
                )
                """
            ),
            {
                "relationship_id": addresses_relationship_id,
                "relationship_type": _RELATIONSHIP_TYPE_ADDRESSES,
                "source_kind": _KIND_OBSERVED_OUTCOME_REVISION,
                "source_id": observed_outcome_id,
                "source_revision_id": observed_outcome_revision_id,
                "target_kind": _KIND_INTENDED_OUTCOME_REVISION,
                "target_id": target_intended_outcome_resource_id,
                "target_revision_id": target_intended_outcome_revision_id,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # One ``Cites`` Relationship per cited Measurement Record (AD-WS-35,
        # semantic_role = 'observation_basis'). The target Measurement Record
        # is an Immutable Record with no Revision, so target_revision_id is
        # NULL.
        for cites_relationship_id, cited_id in zip(
            cites_relationship_ids, cited_measurement_record_ids
        ):
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
                        :target_kind, :target_id, NULL,
                        :authoring_party_id, :recorded_at, :semantic_role
                    )
                    """
                ),
                {
                    "relationship_id": cites_relationship_id,
                    "relationship_type": _RELATIONSHIP_TYPE_CITES,
                    "source_kind": _KIND_OBSERVED_OUTCOME_REVISION,
                    "source_id": observed_outcome_id,
                    "source_revision_id": observed_outcome_revision_id,
                    "target_kind": _KIND_MEASUREMENT_RECORD,
                    "target_id": cited_id,
                    "authoring_party_id": authoring_party_id,
                    "recorded_at": recorded_at,
                    "semantic_role": _SEMANTIC_ROLE_OBSERVATION_BASIS,
                },
            )

        # Consequential audit row (Requirement 47.6 / 57.1 / AD-WS-5).
        # Participates in the caller's transaction.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_ACTION_CREATE_OBSERVED_OUTCOME,
            target_id=observed_outcome_id,
            target_revision_id=observed_outcome_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateObservedOutcomeResult(
            observed_outcome_id=observed_outcome_id,
            observed_outcome_revision_id=observed_outcome_revision_id,
            outcome_kind=_OUTCOME_KIND_OBSERVED,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            predecessor_revision_id=predecessor_revision_id,
            assessment_summary=assessment_summary,
            cited_measurement_record_ids=cited_measurement_record_ids,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            cites_relationship_ids=cites_relationship_ids,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- read helper -------------------------------------------------------

    @staticmethod
    def get_observed_outcome_revision(
        connection: Connection,
        *,
        observed_outcome_revision_id: str,
    ) -> Optional[ObservedOutcomeRevisionRow]:
        """Read-only lookup of an Observed Outcome Revision by its Identity.

        Backs the Success-Condition Assessment sourcing rule (Requirement 48.3,
        AD-WS-40): the
        :class:`~walking_slice.outcome.success_condition_assessments.SuccessConditionAssessmentService`
        must (a) confirm the named sourced Observed Outcome Revision resolves,
        (b) recover its parent Observed Outcome **Resource** Identity, and
        (c) recover the Revision's ``Addresses`` target Intended Outcome
        Revision Identity so it can verify that target equals the named target
        Intended Outcome Revision.

        Introduces no write path. Returns ``None`` when the supplied Identity
        does not resolve so the caller can treat the absent case without
        try/except, mirroring the
        :meth:`MeasurementRecordService.get_measurement_record` and
        :meth:`IntendedOutcomeService.get_revision` ``one_or_none``
        convention.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context.
            observed_outcome_revision_id: The Observed Outcome Revision
                Identity to resolve.

        Returns:
            An :class:`ObservedOutcomeRevisionRow` snapshot when the Revision
            exists; ``None`` otherwise.
        """
        row = connection.execute(
            text(
                "SELECT "
                "  observed_outcome_revision_id, "
                "  observed_outcome_id, "
                "  outcome_kind, "
                "  target_intended_outcome_resource_id, "
                "  target_intended_outcome_revision_id, "
                "  assessment_summary, "
                "  predecessor_revision_id, "
                "  authoring_party_id, "
                "  applicable_scope, "
                "  recorded_at "
                "FROM Observed_Outcome_Revisions "
                "WHERE observed_outcome_revision_id = "
                ":observed_outcome_revision_id"
            ),
            {"observed_outcome_revision_id": observed_outcome_revision_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        return ObservedOutcomeRevisionRow(
            observed_outcome_revision_id=row["observed_outcome_revision_id"],
            observed_outcome_id=row["observed_outcome_id"],
            outcome_kind=row["outcome_kind"],
            target_intended_outcome_resource_id=(
                row["target_intended_outcome_resource_id"]
            ),
            target_intended_outcome_revision_id=(
                row["target_intended_outcome_revision_id"]
            ),
            assessment_summary=row["assessment_summary"],
            predecessor_revision_id=row["predecessor_revision_id"],
            authoring_party_id=row["authoring_party_id"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    # -- read helper (predecessor chain) ----------------------------------

    @staticmethod
    def _resolve_current_revision(
        connection: Connection,
        *,
        observed_outcome_id: str,
    ) -> Optional[tuple[str, str]]:
        """Return ``(current_revision_id, target_intended_outcome_revision_id)``
        for the most-recent Revision of the named Observed Outcome Resource, or
        ``None`` when the Resource has no Revisions / does not resolve.

        The most-recent Revision is the **tail** of the append-only predecessor
        chain (AD-WS-36): the single Revision that is not named as any other
        Revision's ``predecessor_revision_id``. The
        ``idx_oo_revisions_one_successor`` partial UNIQUE index keeps the chain
        linear, so at most one such tail exists. Identifying the tail by
        chain-position (rather than by ``recorded_at`` ordering) is robust to
        equal timestamps.
        """
        row = connection.execute(
            text(
                "SELECT oor.observed_outcome_revision_id "
                "       AS observed_outcome_revision_id, "
                "       oor.target_intended_outcome_revision_id "
                "       AS target_intended_outcome_revision_id "
                "FROM Observed_Outcome_Revisions oor "
                "WHERE oor.observed_outcome_id = :observed_outcome_id "
                "  AND NOT EXISTS ( "
                "    SELECT 1 FROM Observed_Outcome_Revisions successor "
                "    WHERE successor.predecessor_revision_id = "
                "          oor.observed_outcome_revision_id "
                "  )"
            ),
            {"observed_outcome_id": observed_outcome_id},
        ).mappings().first()
        if row is None:
            return None
        return (
            row["observed_outcome_revision_id"],
            row["target_intended_outcome_revision_id"],
        )

    # -- denial side-channel ----------------------------------------------

    def _persist_observed_outcome_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_intended_outcome_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Observed Outcome attempt.

        Implements the Requirement 50.6 retry contract verbatim (mirroring the
        Slice 1/2/3 pattern and the sibling Outcome_Service services): each
        attempt opens a *new* :meth:`Engine.begin` transaction, tries
        :meth:`AuditLog.append_denial`, and either returns on success or pauses
        by the next entry in :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the
        next try. If every attempt fails,
        :class:`ObservedOutcomeAuditFailureError` is raised.

        The separate transaction is essential: the caller's originating
        transaction is about to be rolled back when the write method raises
        :class:`ObservedOutcomeAuthorizationError`. The Denial Record must
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
                        attempted_action=_ACTION_CREATE_OBSERVED_OUTCOME,
                        target_id=target_intended_outcome_revision_id,
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

        raise ObservedOutcomeAuditFailureError(
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
            raise ObservedOutcomeValidationError(
                str(exc),
                failed_constraint="prohibited_attribute",
                invalid_attributes=exc.prohibited_keys,
                prohibited_keys=exc.prohibited_keys,
            ) from exc

    @staticmethod
    def _reject_invalid_outcome_kind(
        outcome_kind: Optional[str],
        request_attributes: Optional[Mapping[str, Any]],
    ) -> None:
        """Reject an ``outcome_kind`` supplied with any value other than the
        literal ``'observed'`` (Requirement 47.4).

        Both the typed ``outcome_kind`` kwarg and a raw request body key
        (``outcome_kind`` / ``outcome-kind``, case-insensitive,
        hyphen/underscore-invariant) are screened. An omitted value is
        accepted — the service always records ``outcome_kind = 'observed'``.
        """
        if outcome_kind is not None and outcome_kind != _OUTCOME_KIND_OBSERVED:
            raise ObservedOutcomeValidationError(
                f"outcome_kind {outcome_kind!r} is invalid for an Observed "
                f"Outcome; only {_OUTCOME_KIND_OBSERVED!r} is permitted "
                "(Requirement 47.4).",
                failed_constraint="outcome_kind_invalid",
                invalid_attributes=("outcome_kind",),
            )
        if request_attributes is None:
            return
        for key, value in request_attributes.items():
            if not isinstance(key, str):
                continue
            if key.lower().replace("_", "-") == "outcome-kind":
                if value is not None and value != _OUTCOME_KIND_OBSERVED:
                    raise ObservedOutcomeValidationError(
                        f"outcome_kind {value!r} is invalid for an Observed "
                        f"Outcome; only {_OUTCOME_KIND_OBSERVED!r} is "
                        "permitted (Requirement 47.4).",
                        failed_constraint="outcome_kind_invalid",
                        invalid_attributes=("outcome_kind",),
                    )


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


def _is_present_str(value: Any) -> bool:
    """Return whether a required string attribute was supplied and non-empty."""
    return isinstance(value, str) and len(value) > 0


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit row, the
    (separate-transaction) Denial Record, and the consequential audit row
    produced for the same logical Observed Outcome write. They are not
    registered with :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Observed Outcome Resource
    Identity (on create) and the Observed Outcome Revision Identity in
    ``Identifier_Registry``. Sharing one digest across both bindings mirrors
    the Slice 1/2/3 convention.
    """
    return hashlib.sha256(content).hexdigest()
