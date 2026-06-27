"""Outcome_Service.MeasurementDefinitions — Measurement Definition Resources,
their initial immutable Revisions, and the ``Addresses`` Relationship to the
target Intended Outcome Revision.

Design reference
================

``.kiro/specs/fourth-walking-slice/design.md``:

- §"Outcome_Service.MeasurementDefinitions" — the public dataclass surface
  (``create_measurement_definition`` plus the
  ``get_definition_for_intended_outcome`` read), the authority mapping
  (``create.measurement_definition`` → ``define_measurement`` per AD-WS-33),
  the validation order (Pydantic + prohibited-attribute guard → Intended
  Outcome Revision resolution + ``outcome_kind = 'intended'`` gate →
  uniqueness pre-check → authorization evaluation → consequential write),
  and the persistence invariant that at most one Measurement Definition
  Resource exists per target Intended Outcome Resource
  (Requirement 44.3, ``UNIQUE(target_intended_outcome_resource_id)``).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time shared
  by every row in the transaction), Identifiers (every new identity is a
  UUIDv7 minted by :class:`IdentityService` and registered in
  ``Identifier_Registry`` with the additive Slice 4 ``resource_kind`` tag per
  AD-WS-37), Authorization (the deny path reuses the cumulative
  separate-transaction Denial-Record pattern with the Requirement 50.6
  three-attempt retry).
- AD-WS-35 — the ``Addresses`` Relationship from the Measurement Definition
  Revision to the target Intended Outcome Revision carries
  ``semantic_role IS NULL``.
- AD-WS-40 — the target Intended Outcome Revision is resolved through the
  additive read-only :meth:`IntendedOutcomeService.get_revision` Planning API.

Task scope (task 4.1)
=====================

This module implements
:meth:`MeasurementDefinitionService.create_measurement_definition` and the
:meth:`MeasurementDefinitionService.get_definition_for_intended_outcome` read:

1. Validate request inputs through a Pydantic
   :class:`MeasurementDefinitionCreationRequest` model with
   ``Config(extra='forbid')`` enforcing the Requirement 44.2 length ranges
   (measurand description 1..4000, unit-of-measure 1..200, observation window
   / cadence / data source 1..1000, applicable scope present), plus the
   shared :func:`_reject_prohibited_attributes` guard rejecting any
   intended-side request key (Requirement 53).
2. Resolve the target Intended Outcome Revision via
   ``intended_outcome_reader.get_revision(...)`` (AD-WS-40); reject when it
   does not resolve or its ``outcome_kind != 'intended'`` (Requirement 44.4);
   reject when more than one target Intended Outcome Revision is named.
3. Uniqueness pre-check: reject when a Measurement Definition Resource already
   addresses the same target Intended Outcome Resource (Requirement 44.3),
   backed at the database level by
   ``UNIQUE(target_intended_outcome_resource_id)``.
4. Evaluate ``Authorization_Service.evaluate(party=authoring_party_id,
   action="create.measurement_definition", target=intended_outcome_ref,
   at=now())`` on a *separate* transaction; on a deny outcome, persist a
   Denial Record in another separate transaction with the Requirement 50.6
   three-attempt exponential-backoff retry, and raise
   :class:`MeasurementDefinitionAuthorizationError` carrying the AD-WS-9
   indistinguishable-denial fields.
5. On a permit outcome, mint the Measurement Definition Resource Identity and
   the first Measurement Definition Revision Identity, register both in
   ``Identifier_Registry`` with their Slice 4 ``resource_kind`` tags
   (AD-WS-37), INSERT the ``Measurement_Definitions`` Resource header, the
   initial immutable ``Measurement_Definition_Revisions`` row, the single
   ``Addresses`` Relationship from the Revision to the target Intended Outcome
   Revision (``semantic_role IS NULL``), and the consequential
   ``Audit_Records`` row — all inside the caller's transaction so a failure
   anywhere rolls every row back (Requirement 44.6 / 57.1 / AD-WS-5).

Requirements satisfied
======================

    44.1 — authorized Measurement Definition creation produces one Resource
           and one initial immutable Revision.
    44.2 — every Measurement Definition Revision records the resource +
           revision identities, the target Intended Outcome Resource +
           Revision identities, the measurand description (1..4000), the
           unit-of-measure descriptor (1..200), the observation-window /
           cadence / data-source descriptors (1..1000), the authoring Party
           Identity, the applicable scope, the recorded time (UTC ms), and the
           single ``Addresses`` Relationship to the target Intended Outcome
           Revision.
    44.3 — at most one Measurement Definition Resource per target Intended
           Outcome Resource; a second attempt is rejected with nothing
           persisted and the first left byte-equivalent.
    44.4 — requests naming an unresolvable target Intended Outcome Revision, a
           target whose ``outcome_kind`` is not ``'intended'``, omitting any
           required attribute, or naming more than one target are rejected.
    44.5 — unauthorized requests are denied; no Resource or Revision is
           created and a Denial Record conforming to AD-WS-9 is appended.
    44.6 — every successful Revision insertion appends one immutable
           consequential audit row in the same transaction.
    44.7 — Measurement Definition Revisions and their ``Addresses``
           Relationship are immutable (enforced by the schema triggers).
    52.6 — ``create.measurement_definition`` requires the ``define_measurement``
           authority (AD-WS-33).
    53.2 — no Measurement Definition creation request may carry an
           intended-side attribute.
    57.1 — the consequential audit append participates in the same
           transaction as the domain rows.
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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
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
from walking_slice.outcome.models import (
    CreateMeasurementDefinitionResult,
    MeasurementDefinitionRow,
)


__all__ = [
    "MeasurementDefinitionAuditFailureError",
    "MeasurementDefinitionAuthorizationError",
    "MeasurementDefinitionCreationRequest",
    "MeasurementDefinitionDuplicateError",
    "MeasurementDefinitionService",
    "MeasurementDefinitionTargetNotResolvableError",
    "MeasurementDefinitionValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# ``create.measurement_definition`` maps to the ``define_measurement``
# authority per AD-WS-33. The string is also the ``action_type`` recorded
# on the consequential audit row (Requirement 44.6) and on the
# separate-transaction Denial Record (Requirement 44.5).
_ACTION_CREATE_MEASUREMENT_DEFINITION: Final[str] = "create.measurement_definition"

# Relationship Type and source/target ``kind`` strings written to
# ``Relationships`` rows (AD-WS-35).
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_MEASUREMENT_DEFINITION_REVISION: Final[str] = "measurement_definition_revision"
_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"

# Identifier_Registry registration kinds (Slice 1 enumeration) and the
# Slice 4 ``resource_kind`` tags (AD-WS-37).
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_MEASUREMENT_DEFINITION: Final[str] = "measurement_definition"
_RESOURCE_KIND_MEASUREMENT_DEFINITION_REVISION: Final[str] = (
    "measurement_definition_revision"
)

# Slice 2 persistence-invariant value the resolved target Intended Outcome
# Revision must carry to be a valid Measurement Definition target
# (Requirement 44.4).
_OUTCOME_KIND_INTENDED: Final[str] = "intended"

# Validation limits per Requirement 44.2.
_MEASURAND_MIN_CHARS: Final[int] = 1
_MEASURAND_MAX_CHARS: Final[int] = 4_000
_UNIT_MIN_CHARS: Final[int] = 1
_UNIT_MAX_CHARS: Final[int] = 200
_DESCRIPTOR_MIN_CHARS: Final[int] = 1
_DESCRIPTOR_MAX_CHARS: Final[int] = 1_000

# Exponential backoff sequence for retrying the separate-transaction Denial
# Record append (Requirement 50.6, mirroring the Slice 1/2/3 pattern). Three
# retries after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class MeasurementDefinitionValidationError(ValueError):
    """Raised when a Measurement Definition submission fails Requirement 44.2 /
    44.4 / 53 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer can
    render a structured 400 response and tests can assert against a stable
    identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"measurand_description_missing"``,
            ``"measurand_description_too_long"``,
            ``"unit_of_measure_missing"``,
            ``"unit_of_measure_too_long"``,
            ``"observation_window_missing"``,
            ``"observation_window_too_long"``,
            ``"cadence_missing"``,
            ``"cadence_too_long"``,
            ``"data_source_missing"``,
            ``"data_source_too_long"``,
            ``"target_intended_outcome_revision_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"multiple_targets_named"`` (more than one target Intended
                Outcome Revision was named — Requirement 44.4),
            ``"prohibited_attribute"`` (the request body carried at least one
                intended-side attribute — see :attr:`prohibited_keys`), or
            ``"invalid_request"``.
        prohibited_keys: Populated only when ``failed_constraint`` is
            ``"prohibited_attribute"``; lists every offending top-level key.
            Empty tuple in every other case.
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


class MeasurementDefinitionTargetNotResolvableError(LookupError):
    """Raised when the target Intended Outcome Revision does not resolve or is
    not an Intended Outcome (Requirement 44.4).

    Requirement 44.4 requires the named target Intended Outcome Revision
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
            f"{target_intended_outcome_revision_id!r} is not a usable "
            f"Measurement Definition target ({failed_constraint}, "
            "Requirement 44.4)."
        )
        self.target_intended_outcome_revision_id = (
            target_intended_outcome_revision_id
        )
        self.failed_constraint = failed_constraint


class MeasurementDefinitionDuplicateError(ValueError):
    """Raised when a Measurement Definition Resource already addresses the same
    target Intended Outcome Resource (Requirement 44.3).

    At most one Measurement Definition Resource may exist per target Intended
    Outcome Resource in this slice. The exception carries both the offending
    target Intended Outcome Resource Identity and the existing Measurement
    Definition Resource Identity so the HTTP layer can render an actionable
    error; the existing first Measurement Definition is left byte-equivalent.
    """

    def __init__(
        self,
        *,
        target_intended_outcome_resource_id: str,
        existing_measurement_definition_id: str,
    ) -> None:
        super().__init__(
            f"A Measurement Definition Resource "
            f"({existing_measurement_definition_id!r}) already addresses "
            f"Intended Outcome Resource "
            f"{target_intended_outcome_resource_id!r}; at most one is "
            "permitted in this slice (Requirement 44.3)."
        )
        self.target_intended_outcome_resource_id = (
            target_intended_outcome_resource_id
        )
        self.existing_measurement_definition_id = (
            existing_measurement_definition_id
        )
        self.failed_constraint = "duplicate_measurement_definition"


class MeasurementDefinitionAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Measurement
    Definition attempt (Requirement 44.5).

    Carries only ``reason_code`` and ``correlation_id`` — the AD-WS-9
    indistinguishable-denial contract forbids leaking authorized Party
    identities, target contents, role-assignment details, or target existence
    beyond the requesting Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Measurement Definition creation denied: "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class MeasurementDefinitionAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 50.6).

    On total audit-append failure the exception is raised *in place of*
    :class:`MeasurementDefinitionAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's transaction
    still rolls back so no Resource, Revision, Relationship, or consequential
    audit row is persisted.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Measurement Definition failed "
            f"after {attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Request model (Pydantic).
# ---------------------------------------------------------------------------


class MeasurementDefinitionCreationRequest(BaseModel):
    """Validated request payload for
    :meth:`MeasurementDefinitionService.create_measurement_definition`.

    Enforces two layers of input discipline:

    1. **Field-level validation** — the five descriptor fields are constrained
       at parse time to the Requirement 44.2 ranges (measurand 1..4000, unit
       1..200, observation window / cadence / data source 1..1000), and the
       three required string identifiers (target Intended Outcome Revision,
       authoring Party, applicable scope) must be non-empty. The same ranges
       are enforced again at the schema layer by the
       ``Measurement_Definition_Revisions`` CHECK constraints, so a successful
       Pydantic validation guarantees a successful INSERT.
    2. **Structural forbid** — ``Config(extra='forbid')`` rejects any field
       not declared on the model. Together with the explicit prohibited-prefix
       screen run by :meth:`_validate_no_intended_attributes`, this is the
       structural side of Requirement 53.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_intended_outcome_revision_id: str = Field(
        min_length=1,
        description=(
            "Identity of the single target Intended Outcome Revision the "
            "Measurement Definition addresses (Requirement 44.4)."
        ),
    )
    measurand_description: str = Field(
        min_length=_MEASURAND_MIN_CHARS,
        max_length=_MEASURAND_MAX_CHARS,
        description=(
            f"Measurand description of {_MEASURAND_MIN_CHARS}.."
            f"{_MEASURAND_MAX_CHARS} characters (Requirement 44.2)."
        ),
    )
    unit_of_measure: str = Field(
        min_length=_UNIT_MIN_CHARS,
        max_length=_UNIT_MAX_CHARS,
        description=(
            f"Unit-of-measure descriptor of {_UNIT_MIN_CHARS}.."
            f"{_UNIT_MAX_CHARS} characters (Requirement 44.2)."
        ),
    )
    observation_window: str = Field(
        min_length=_DESCRIPTOR_MIN_CHARS,
        max_length=_DESCRIPTOR_MAX_CHARS,
        description=(
            f"Observation-window descriptor of {_DESCRIPTOR_MIN_CHARS}.."
            f"{_DESCRIPTOR_MAX_CHARS} characters (Requirement 44.2)."
        ),
    )
    cadence: str = Field(
        min_length=_DESCRIPTOR_MIN_CHARS,
        max_length=_DESCRIPTOR_MAX_CHARS,
        description=(
            f"Cadence descriptor of {_DESCRIPTOR_MIN_CHARS}.."
            f"{_DESCRIPTOR_MAX_CHARS} characters (Requirement 44.2)."
        ),
    )
    data_source: str = Field(
        min_length=_DESCRIPTOR_MIN_CHARS,
        max_length=_DESCRIPTOR_MAX_CHARS,
        description=(
            f"Data-source descriptor of {_DESCRIPTOR_MIN_CHARS}.."
            f"{_DESCRIPTOR_MAX_CHARS} characters (Requirement 44.2)."
        ),
    )
    authoring_party_id: str = Field(
        min_length=1,
        description="Identity of the authoring Party (Requirement 44.5).",
    )
    applicable_scope: str = Field(
        min_length=1,
        description=(
            "Scope identifier the Measurement Definition applies within "
            "(Requirement 44.2)."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_no_intended_attributes(cls, data: Any) -> Any:
        """Reject request bodies that carry intended-side attribute keys.

        Delegates the per-prefix matching to the shared
        :func:`walking_slice.outcome._helpers._reject_prohibited_attributes`
        which canonicalizes hyphen/underscore variants and is
        case-insensitive (Requirement 53). Runs in ``mode='before'`` so the
        screen executes against the raw input dict before unknown-field
        rejection by ``Config(extra='forbid')``, giving the more specific
        intended-side error when both apply.
        """
        if isinstance(data, Mapping):
            _reject_prohibited_attributes(data, OUTCOME_PROHIBITED_PREFIXES)
        return data


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementDefinitionService:
    """Persist Measurement Definition Resources, their initial immutable
    Revisions, and the ``Addresses`` Relationship to the target Intended
    Outcome Revision.

    Connection-scoped at call time: :meth:`create_measurement_definition`
    accepts the caller's :class:`sqlalchemy.engine.Connection` and writes
    inside the caller's transaction (AD-WS-5). The service instance therefore
    holds only the cross-request collaborators and can be shared across
    requests.

    Frozen because design §"Outcome_Service.MeasurementDefinitions" declares it
    ``@dataclass(frozen=True)``.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Measurement_Definitions``, ``Measurement_Definition_Revisions``,
            ``Relationships``, and ``Audit_Records`` rows. Consulted exactly
            once per write so every artifact of the transaction shares one
            timestamp.
        identity_service: Generates Measurement Definition Resource and
            Revision Identities and persists their ``Identifier_Registry``
            bindings with the Slice 4 ``resource_kind`` tag (AD-WS-37).
        audit_log: Appends the consequential audit row (Requirement 44.6)
            inside the caller's transaction.
        authorization_service: Evaluates ``create.measurement_definition``
            → ``define_measurement`` authority per AD-WS-33 / Requirement 44.5;
            the deny path is the cumulative separate-transaction Denial-Record
            pattern.
        intended_outcome_reader: The Slice 2 :class:`IntendedOutcomeService`
            used read-only (``get_revision``) to resolve the target Intended
            Outcome Revision and verify ``outcome_kind = 'intended'``
            (AD-WS-40 / Requirement 44.4).
        denial_audit_sleep: Sleep function used to pause between retries of
            the Denial Record append. Defaults to :func:`time.sleep`; tests
            inject a recording stub so the retry sequence is observable
            without spending real time.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: AuthorizationService
    intended_outcome_reader: IntendedOutcomeService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_measurement_definition(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
        measurand_description: str,
        unit_of_measure: str,
        observation_window: str,
        cadence: str,
        data_source: str,
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateMeasurementDefinitionResult:
        """Create a Measurement Definition Resource plus its first immutable
        Revision and ``Addresses`` Relationship to the target Intended Outcome
        Revision.

        Per Requirements 44.1 through 44.7, 52.6 (``define_measurement``),
        53.2, 57.1, AD-WS-9 (indistinguishable denial), AD-WS-33, AD-WS-35,
        AD-WS-37, and AD-WS-40.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_intended_outcome_revision_id: Identity of the single target
                Intended Outcome Revision (Requirement 44.4).
            measurand_description: Measurand description (1..4000 chars).
            unit_of_measure: Unit-of-measure descriptor (1..200 chars).
            observation_window: Observation-window descriptor (1..1000 chars).
            cadence: Cadence descriptor (1..1000 chars).
            data_source: Data-source descriptor (1..1000 chars).
            authoring_party_id: Identity of the authoring Party.
            applicable_scope: Scope identifier the Measurement Definition
                applies within; also passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`.
            engine: Required for the deny path's separate-transaction Denial
                Record write so the row survives the caller's rollback, and
                for the separate-transaction authorization evaluation.
            correlation_id: Optional correlation identifier shared by every
                audit row written in this operation. A UUIDv7 is generated
                when omitted.
            evaluation_at: Optional explicit effective time passed to
                :meth:`AuthorizationService.evaluate` as the ``at`` parameter.
                Defaults to the recorded time of this transaction.
            request_attributes: Optional mapping of the original top-level
                request body keys. When provided, the mapping is screened
                against the intended-side prefix list (Requirement 53). The
                HTTP layer forwards the raw request body here.

        Returns:
            :class:`CreateMeasurementDefinitionResult` carrying the persisted
            identifiers, attributes, the ``Addresses`` Relationship Identity,
            the recorded time, and the correlation identifier.

        Raises:
            MeasurementDefinitionValidationError: A required attribute is
                missing, a Requirement 44.2 range was violated, more than one
                target was named, or the request body carried a prohibited
                intended-side attribute (Requirement 53).
            MeasurementDefinitionTargetNotResolvableError: The target Intended
                Outcome Revision did not resolve, or its ``outcome_kind`` is
                not ``'intended'`` (Requirement 44.4).
            MeasurementDefinitionDuplicateError: A Measurement Definition
                Resource already addresses the same target Intended Outcome
                Resource (Requirement 44.3).
            MeasurementDefinitionAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 44.5). The Denial Record was appended
                successfully in a separate transaction.
            MeasurementDefinitionAuditFailureError: The attempt was denied
                *and* the separate-transaction Denial Record append failed on
                every retry (Requirement 50.6).
        """
        # 1. Screen the original request body when the route layer forwarded
        # it, plus guard against more-than-one-target. The typed kwargs cannot
        # carry a prohibited attribute (the signature declares none), but the
        # HTTP layer's raw body might (Requirement 53).
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, OUTCOME_PROHIBITED_PREFIXES
                )
            except OutcomeValidationError as exc:
                raise MeasurementDefinitionValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc
            self._reject_multiple_targets(request_attributes)

        # The typed kwarg itself may have been handed a list/tuple by a
        # non-HTTP caller; reject more than one named target (Requirement 44.4).
        if isinstance(
            target_intended_outcome_revision_id, (list, tuple, set)
        ):
            raise MeasurementDefinitionValidationError(
                "more than one target Intended Outcome Revision was named; "
                "a Measurement Definition addresses exactly one "
                "(Requirement 44.4).",
                failed_constraint="multiple_targets_named",
            )

        # 2. Validate kwargs through the Pydantic request model.
        try:
            request = MeasurementDefinitionCreationRequest(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                measurand_description=measurand_description,
                unit_of_measure=unit_of_measure,
                observation_window=observation_window,
                cadence=cadence,
                data_source=data_source,
                authoring_party_id=authoring_party_id,
                applicable_scope=applicable_scope,
            )
        except ValidationError as exc:
            raise self._translate_pydantic_error(exc) from exc

        # 3. Resolve the target Intended Outcome Revision via the additive
        # read-only Planning API (AD-WS-40). Reject when it does not resolve
        # or its outcome_kind is not 'intended' (Requirement 44.4). The check
        # runs before authorization evaluation so the deny path never reveals
        # whether a target exists for an unauthorized caller.
        revision_row = self.intended_outcome_reader.get_revision(
            connection, request.target_intended_outcome_revision_id
        )
        if revision_row is None:
            raise MeasurementDefinitionTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    request.target_intended_outcome_revision_id
                ),
                failed_constraint="target_intended_outcome_not_resolvable",
            )
        if revision_row.outcome_kind != _OUTCOME_KIND_INTENDED:
            raise MeasurementDefinitionTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    request.target_intended_outcome_revision_id
                ),
                failed_constraint="target_outcome_kind_not_intended",
            )
        target_intended_outcome_resource_id = revision_row.intended_outcome_id

        # 4. Uniqueness pre-check (Requirement 44.3). The DB enforces the same
        # invariant via UNIQUE(target_intended_outcome_resource_id); this
        # pre-check produces a structured error before any write is attempted
        # and leaves the existing first Measurement Definition byte-equivalent.
        existing = self.get_definition_for_intended_outcome(
            connection,
            intended_outcome_resource_id=target_intended_outcome_resource_id,
        )
        if existing is not None:
            raise MeasurementDefinitionDuplicateError(
                target_intended_outcome_resource_id=(
                    target_intended_outcome_resource_id
                ),
                existing_measurement_definition_id=(
                    existing.measurement_definition_id
                ),
            )

        # 5. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Measurement_Definitions row, the Measurement_Definition_Revisions
        # row, the Addresses Relationship row, and the consequential audit row
        # all share this timestamp.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 6. Run the authorization evaluation on a SEPARATE transaction. The
        # ``TargetRef`` is the target Intended Outcome Revision so the wired
        # role assignment must cover the same scope to permit the action.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=request.authoring_party_id,
                action=_ACTION_CREATE_MEASUREMENT_DEFINITION,
                target=TargetRef(
                    kind=_KIND_INTENDED_OUTCOME_REVISION,
                    id=target_intended_outcome_resource_id,
                    revision_id=request.target_intended_outcome_revision_id,
                    scope=request.applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_measurement_definition_denial(
                engine=engine,
                actor_party_id=request.authoring_party_id,
                target_intended_outcome_revision_id=(
                    request.target_intended_outcome_revision_id
                ),
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise MeasurementDefinitionAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 7. Mint identifiers (AD-WS-37). The Measurement Definition Resource
        # and its first Revision are bound to the same content digest derived
        # from the first Revision's payload.
        measurement_definition_id = str(self.identity_service.new_resource_id())
        measurement_definition_revision_id = str(
            self.identity_service.new_revision_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_intended_outcome_resource_id": (
                        target_intended_outcome_resource_id
                    ),
                    "target_intended_outcome_revision_id": (
                        request.target_intended_outcome_revision_id
                    ),
                    "measurand_description": request.measurand_description,
                    "unit_of_measure": request.unit_of_measure,
                    "observation_window": request.observation_window,
                    "cadence": request.cadence,
                    "data_source": request.data_source,
                    "authoring_party_id": request.authoring_party_id,
                    "applicable_scope": request.applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 8. Register both identifiers in ``Identifier_Registry`` carrying the
        # AD-WS-37 ``resource_kind`` tag.
        _record_outcome_artifact(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_MEASUREMENT_DEFINITION,
            measurement_definition_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=request.authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_MEASUREMENT_DEFINITION,
            recorded_time=recorded_time,
        )
        _record_outcome_artifact(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_MEASUREMENT_DEFINITION_REVISION,
            measurement_definition_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=request.authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_MEASUREMENT_DEFINITION,
            recorded_time=recorded_time,
        )

        # 9. Insert the Resource header. ``created_at`` carries the same
        # recorded time as the first Revision so the two rows' timestamps are
        # byte-equivalent.
        connection.execute(
            text(
                """
                INSERT INTO Measurement_Definitions (
                    measurement_definition_id,
                    target_intended_outcome_resource_id,
                    created_at
                ) VALUES (
                    :measurement_definition_id,
                    :target_intended_outcome_resource_id,
                    :created_at
                )
                """
            ),
            {
                "measurement_definition_id": measurement_definition_id,
                "target_intended_outcome_resource_id": (
                    target_intended_outcome_resource_id
                ),
                "created_at": recorded_at,
            },
        )

        # 10. Insert the first immutable Revision (Requirement 44.2).
        connection.execute(
            text(
                """
                INSERT INTO Measurement_Definition_Revisions (
                    measurement_definition_revision_id,
                    measurement_definition_id,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    measurand_description, unit_of_measure,
                    observation_window, cadence, data_source,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :measurement_definition_revision_id,
                    :measurement_definition_id,
                    :target_intended_outcome_resource_id,
                    :target_intended_outcome_revision_id,
                    :measurand_description, :unit_of_measure,
                    :observation_window, :cadence, :data_source,
                    :authoring_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "measurement_definition_revision_id": (
                    measurement_definition_revision_id
                ),
                "measurement_definition_id": measurement_definition_id,
                "target_intended_outcome_resource_id": (
                    target_intended_outcome_resource_id
                ),
                "target_intended_outcome_revision_id": (
                    request.target_intended_outcome_revision_id
                ),
                "measurand_description": request.measurand_description,
                "unit_of_measure": request.unit_of_measure,
                "observation_window": request.observation_window,
                "cadence": request.cadence,
                "data_source": request.data_source,
                "authoring_party_id": request.authoring_party_id,
                "applicable_scope": request.applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 11. Insert the single ``Addresses`` Relationship row (AD-WS-35).
        # ``semantic_role`` is NULL on Addresses rows.
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
                "source_kind": _KIND_MEASUREMENT_DEFINITION_REVISION,
                "source_id": measurement_definition_id,
                "source_revision_id": measurement_definition_revision_id,
                "target_kind": _KIND_INTENDED_OUTCOME_REVISION,
                "target_id": target_intended_outcome_resource_id,
                "target_revision_id": (
                    request.target_intended_outcome_revision_id
                ),
                "authoring_party_id": request.authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 12. Append the consequential audit row (Requirement 44.6 / 57.1 /
        # AD-WS-5). Participates in the caller's transaction so a failure here
        # rolls back the registry, Measurement_Definitions,
        # Measurement_Definition_Revisions, and Relationships rows together.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=request.authoring_party_id,
            action_type=_ACTION_CREATE_MEASUREMENT_DEFINITION,
            target_id=measurement_definition_id,
            target_revision_id=measurement_definition_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateMeasurementDefinitionResult(
            measurement_definition_id=measurement_definition_id,
            measurement_definition_revision_id=(
                measurement_definition_revision_id
            ),
            target_intended_outcome_revision_id=(
                request.target_intended_outcome_revision_id
            ),
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            measurand_description=request.measurand_description,
            unit_of_measure=request.unit_of_measure,
            observation_window=request.observation_window,
            cadence=request.cadence,
            data_source=request.data_source,
            authoring_party_id=request.authoring_party_id,
            applicable_scope=request.applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- read APIs ---------------------------------------------------------

    @staticmethod
    def get_definition_for_intended_outcome(
        connection: Connection,
        *,
        intended_outcome_resource_id: str,
    ) -> Optional[MeasurementDefinitionRow]:
        """Read-only lookup of the Measurement Definition addressing a given
        target Intended Outcome Resource.

        Backs the Requirement 44.3 uniqueness pre-check and the Observed
        Outcome anchoring rule (Requirement 47.2/47.4, AD-WS-40): the single
        Measurement Definition Resource that addresses a target Intended
        Outcome Resource is resolved by joining ``Measurement_Definitions`` to
        its initial ``Measurement_Definition_Revisions`` row on
        ``measurement_definition_id``. Because at most one Measurement
        Definition Resource exists per Intended Outcome Resource
        (``UNIQUE(target_intended_outcome_resource_id)``) and Measurement
        Definitions carry exactly one immutable Revision in this slice, the
        join yields at most one row.

        Introduces no write path. Returns ``None`` when no Measurement
        Definition addresses the supplied Intended Outcome Resource so the
        caller can treat the absent case without try/except.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context.
            intended_outcome_resource_id: The target Intended Outcome
                **Resource** Identity to resolve against.

        Returns:
            A :class:`MeasurementDefinitionRow` snapshot when a matching
            Measurement Definition exists; ``None`` otherwise.
        """
        row = connection.execute(
            text(
                "SELECT "
                "  d.measurement_definition_id AS measurement_definition_id, "
                "  r.measurement_definition_revision_id "
                "    AS measurement_definition_revision_id, "
                "  d.target_intended_outcome_resource_id "
                "    AS target_intended_outcome_resource_id, "
                "  r.target_intended_outcome_revision_id "
                "    AS target_intended_outcome_revision_id, "
                "  r.measurand_description AS measurand_description, "
                "  r.unit_of_measure AS unit_of_measure, "
                "  r.observation_window AS observation_window, "
                "  r.cadence AS cadence, "
                "  r.data_source AS data_source, "
                "  r.authoring_party_id AS authoring_party_id, "
                "  r.applicable_scope AS applicable_scope, "
                "  r.recorded_at AS recorded_at "
                "FROM Measurement_Definitions d "
                "JOIN Measurement_Definition_Revisions r "
                "  ON r.measurement_definition_id = d.measurement_definition_id "
                "WHERE d.target_intended_outcome_resource_id = "
                ":intended_outcome_resource_id"
            ),
            {"intended_outcome_resource_id": intended_outcome_resource_id},
        ).mappings().first()
        if row is None:
            return None
        return MeasurementDefinitionRow(
            measurement_definition_id=row["measurement_definition_id"],
            measurement_definition_revision_id=(
                row["measurement_definition_revision_id"]
            ),
            target_intended_outcome_resource_id=(
                row["target_intended_outcome_resource_id"]
            ),
            target_intended_outcome_revision_id=(
                row["target_intended_outcome_revision_id"]
            ),
            measurand_description=row["measurand_description"],
            unit_of_measure=row["unit_of_measure"],
            observation_window=row["observation_window"],
            cadence=row["cadence"],
            data_source=row["data_source"],
            authoring_party_id=row["authoring_party_id"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    @staticmethod
    def get_definition_revision(
        connection: Connection,
        *,
        measurement_definition_revision_id: str,
    ) -> Optional[MeasurementDefinitionRow]:
        """Read-only lookup of a Measurement Definition Revision by its
        Revision Identity.

        Backs the Measurement Record write path (Requirements 45 / 46): a
        native or imported Measurement Record names exactly one target
        Measurement Definition Revision Identity, and the
        :class:`~walking_slice.outcome.measurement_records.MeasurementRecordService`
        must resolve that Revision to (a) confirm it exists, (b) recover the
        target Measurement Definition **Resource** Identity it belongs to so
        the Record row can carry both, and (c) recover the
        ``unit_of_measure`` and ``observation_window`` descriptors the Record
        is validated against (Requirements 45.2/45.3, 46.2/46.4).

        Introduces no write path. Returns ``None`` when the supplied Revision
        Identity does not resolve so the caller can treat the absent case
        without try/except.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context.
            measurement_definition_revision_id: The Measurement Definition
                **Revision** Identity to resolve.

        Returns:
            A :class:`MeasurementDefinitionRow` snapshot when the Revision
            exists; ``None`` otherwise.
        """
        row = connection.execute(
            text(
                "SELECT "
                "  d.measurement_definition_id AS measurement_definition_id, "
                "  r.measurement_definition_revision_id "
                "    AS measurement_definition_revision_id, "
                "  d.target_intended_outcome_resource_id "
                "    AS target_intended_outcome_resource_id, "
                "  r.target_intended_outcome_revision_id "
                "    AS target_intended_outcome_revision_id, "
                "  r.measurand_description AS measurand_description, "
                "  r.unit_of_measure AS unit_of_measure, "
                "  r.observation_window AS observation_window, "
                "  r.cadence AS cadence, "
                "  r.data_source AS data_source, "
                "  r.authoring_party_id AS authoring_party_id, "
                "  r.applicable_scope AS applicable_scope, "
                "  r.recorded_at AS recorded_at "
                "FROM Measurement_Definition_Revisions r "
                "JOIN Measurement_Definitions d "
                "  ON d.measurement_definition_id = r.measurement_definition_id "
                "WHERE r.measurement_definition_revision_id = "
                ":measurement_definition_revision_id"
            ),
            {
                "measurement_definition_revision_id": (
                    measurement_definition_revision_id
                )
            },
        ).mappings().first()
        if row is None:
            return None
        return MeasurementDefinitionRow(
            measurement_definition_id=row["measurement_definition_id"],
            measurement_definition_revision_id=(
                row["measurement_definition_revision_id"]
            ),
            target_intended_outcome_resource_id=(
                row["target_intended_outcome_resource_id"]
            ),
            target_intended_outcome_revision_id=(
                row["target_intended_outcome_revision_id"]
            ),
            measurand_description=row["measurand_description"],
            unit_of_measure=row["unit_of_measure"],
            observation_window=row["observation_window"],
            cadence=row["cadence"],
            data_source=row["data_source"],
            authoring_party_id=row["authoring_party_id"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    # -- denial side-channel ----------------------------------------------

    def _persist_measurement_definition_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_intended_outcome_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Measurement Definition
        attempt.

        Implements the Requirement 50.6 retry contract verbatim (mirroring the
        Slice 1/2/3 pattern): each attempt opens a *new* :meth:`Engine.begin`
        transaction (so a previous attempt's rollback does not poison this
        one), tries :meth:`AuditLog.append_denial`, and either returns on
        success or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails,
          :class:`MeasurementDefinitionAuditFailureError` is raised.

        The separate transaction is essential: the caller's originating
        transaction is about to be rolled back when
        :meth:`create_measurement_definition` raises
        :class:`MeasurementDefinitionAuthorizationError` (or this method raises
        :class:`MeasurementDefinitionAuditFailureError`). The Denial Record
        must therefore live outside that scope to survive (AD-WS-9 /
        Requirement 50.6).
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=(
                            _ACTION_CREATE_MEASUREMENT_DEFINITION
                        ),
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

        raise MeasurementDefinitionAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _reject_multiple_targets(request_attributes: Mapping[str, Any]) -> None:
        """Reject a raw request body naming more than one target Intended
        Outcome Revision (Requirement 44.4).

        The route layer may forward a body whose
        ``target_intended_outcome_revision_id`` (or its hyphenated /
        pluralized variants) carries a list of identifiers. A Measurement
        Definition addresses exactly one Intended Outcome Revision, so any
        list/tuple/set value or a plural ``*_ids`` key is rejected here before
        the typed-kwarg path runs.
        """
        for key, value in request_attributes.items():
            if not isinstance(key, str):
                continue
            normalized = key.lower().replace("_", "-")
            if normalized in (
                "target-intended-outcome-revision-id",
                "target-intended-outcome-revision-ids",
            ) and isinstance(value, (list, tuple, set)):
                if len(value) > 1:
                    raise MeasurementDefinitionValidationError(
                        "more than one target Intended Outcome Revision was "
                        "named; a Measurement Definition addresses exactly "
                        "one (Requirement 44.4).",
                        failed_constraint="multiple_targets_named",
                    )

    @staticmethod
    def _translate_pydantic_error(
        exc: ValidationError,
    ) -> "MeasurementDefinitionValidationError":
        """Convert a Pydantic :class:`ValidationError` to a structured
        :class:`MeasurementDefinitionValidationError`.

        Service-level callers (e.g., unit tests) benefit from receiving the
        same exception class regardless of which validation layer caught the
        problem. The ``failed_constraint`` discriminator is derived from the
        first reported error so assertions remain stable. A wrapped
        :class:`OutcomeValidationError` (raised from the ``mode='before'``
        prohibited-attribute validator) retains its ``prohibited_keys`` tuple.
        """
        errors = exc.errors()
        first_error = errors[0] if errors else None
        if first_error is None:
            return MeasurementDefinitionValidationError(
                str(exc), failed_constraint="invalid_request"
            )

        # Unwrap a wrapped OutcomeValidationError so the prohibited-attribute
        # discriminator and the ``prohibited_keys`` tuple flow through.
        ctx = first_error.get("ctx") or {}
        cause = ctx.get("error") if isinstance(ctx, Mapping) else None
        if isinstance(cause, OutcomeValidationError):
            return MeasurementDefinitionValidationError(
                str(cause),
                failed_constraint="prohibited_attribute",
                prohibited_keys=cause.prohibited_keys,
            )

        location = first_error.get("loc", ())
        error_type = first_error.get("type", "")
        field_name = location[0] if location else ""
        constraint = _PYDANTIC_FAILED_CONSTRAINT_MAP.get(
            (str(field_name), str(error_type))
        )
        if constraint is None:
            constraint = (
                f"{field_name}_invalid" if field_name else "invalid_request"
            )
        return MeasurementDefinitionValidationError(
            str(exc), failed_constraint=constraint
        )


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


# Mapping of (field-name, Pydantic-error-type) pairs to the
# ``failed_constraint`` discriminator on
# :class:`MeasurementDefinitionValidationError`.
_PYDANTIC_FAILED_CONSTRAINT_MAP: Final[dict[tuple[str, str], str]] = {
    ("measurand_description", "string_too_short"): "measurand_description_missing",
    ("measurand_description", "missing"): "measurand_description_missing",
    ("measurand_description", "string_type"): "measurand_description_missing",
    ("measurand_description", "string_too_long"): "measurand_description_too_long",
    ("unit_of_measure", "string_too_short"): "unit_of_measure_missing",
    ("unit_of_measure", "missing"): "unit_of_measure_missing",
    ("unit_of_measure", "string_type"): "unit_of_measure_missing",
    ("unit_of_measure", "string_too_long"): "unit_of_measure_too_long",
    ("observation_window", "string_too_short"): "observation_window_missing",
    ("observation_window", "missing"): "observation_window_missing",
    ("observation_window", "string_type"): "observation_window_missing",
    ("observation_window", "string_too_long"): "observation_window_too_long",
    ("cadence", "string_too_short"): "cadence_missing",
    ("cadence", "missing"): "cadence_missing",
    ("cadence", "string_type"): "cadence_missing",
    ("cadence", "string_too_long"): "cadence_too_long",
    ("data_source", "string_too_short"): "data_source_missing",
    ("data_source", "missing"): "data_source_missing",
    ("data_source", "string_type"): "data_source_missing",
    ("data_source", "string_too_long"): "data_source_too_long",
    (
        "target_intended_outcome_revision_id",
        "string_too_short",
    ): "target_intended_outcome_revision_id_missing",
    (
        "target_intended_outcome_revision_id",
        "missing",
    ): "target_intended_outcome_revision_id_missing",
    (
        "target_intended_outcome_revision_id",
        "string_type",
    ): "target_intended_outcome_revision_id_missing",
    ("authoring_party_id", "string_too_short"): "authoring_party_id_missing",
    ("authoring_party_id", "missing"): "authoring_party_id_missing",
    ("authoring_party_id", "string_type"): "authoring_party_id_missing",
    ("applicable_scope", "string_too_short"): "applicable_scope_missing",
    ("applicable_scope", "missing"): "applicable_scope_missing",
    ("applicable_scope", "string_type"): "applicable_scope_missing",
}


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit row, the
    (separate-transaction) Denial Record, and the consequential audit row
    produced for the same logical Measurement Definition creation. They are
    not registered with :class:`IdentityService` because they do not name a
    domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Measurement Definition
    Resource Identity and the first Measurement Definition Revision Identity in
    ``Identifier_Registry``. Sharing one digest across both bindings mirrors
    the Slice 1/2/3 convention.
    """
    return hashlib.sha256(content).hexdigest()
