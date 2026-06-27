"""Outcome_Service.SuccessConditionAssessments — Success-Condition Assessment
Immutable Records, each with one ``Addresses`` Relationship to the target
Intended Outcome Revision and one ``Cites`` Relationship to the sourced
Observed Outcome Revision.

Design reference
================

``.kiro/specs/fourth-walking-slice/design.md``:

- §"Outcome_Service.SuccessConditionAssessments" — the public dataclass
  surface (``create_assessment``), the authority mapping
  (``create.success_condition_assessment`` → ``assess_outcome`` per AD-WS-33),
  and the validation rules (Requirement 48): the assessment category is drawn
  from ``{Satisfied, Partially_Satisfied, Not_Satisfied, Unassessable}``; the
  assessment rationale is 1..4000 characters and at least 200 characters when
  the category is ``Unassessable``; the authority basis type is in the
  AD-WS-10 set; the applicable scope is present; the target Intended Outcome
  Revision resolves and is ``intended``; the sourced Observed Outcome Revision
  resolves and its ``Addresses`` target equals the named target Intended
  Outcome Revision (Requirement 48.3).
- §"Relationships rows written by Slice 4" — a Success-Condition Assessment
  Record writes one ``Addresses`` Relationship to the target Intended Outcome
  Revision (``semantic_role IS NULL``) and one ``Cites`` Relationship to the
  sourced Observed Outcome Revision (``semantic_role = 'assessment_basis'``,
  AD-WS-35).
- §"Cross-Cutting Concerns" — Transactionality (one recorded time shared by
  every row in the transaction), Identifiers (every new identity is a UUIDv7
  minted by :class:`IdentityService` and registered in ``Identifier_Registry``
  with the additive Slice 4 ``resource_kind`` tag per AD-WS-37; a
  Success-Condition Assessment Record is an Immutable Record so its registry
  binding uses ``kind='immutable_record'``), Authorization (the deny path
  reuses the cumulative separate-transaction Denial-Record pattern with the
  Requirement 50.6 three-attempt retry).
- AD-WS-41 — the authority basis recorded on the Record is an
  :class:`AuthorityBasisRef` whose ``type`` is in the AD-WS-10 set.

Task scope (task 8.1)
=====================

This module implements
:meth:`SuccessConditionAssessmentService.create_assessment`:

1. Screen the raw request body for prohibited intended-side keys
   (Requirement 53) and validate the required attributes: the assessment
   category enumeration, the rationale length (1..4000, ``>= 200`` when
   ``Unassessable``), the authority basis (present, type in the AD-WS-10 set),
   the applicable scope, the assessing Party Identity, and the two named
   target Identities (Requirement 48.3).
2. Resolve the target Intended Outcome Revision via
   ``intended_outcome_reader.get_revision(...)`` (AD-WS-40); reject when it
   does not resolve or its ``outcome_kind != 'intended'`` (Requirement 48.3).
3. Resolve the sourced Observed Outcome Revision via
   ``observed_outcome_reader.get_observed_outcome_revision(...)``; reject when
   it does not resolve or its ``Addresses`` target Intended Outcome Revision
   Identity does not equal the named target Intended Outcome Revision Identity
   (Requirement 48.3).
4. Evaluate ``Authorization_Service.evaluate(party,
   "create.success_condition_assessment", target, at)`` on a *separate*
   transaction; on a deny outcome, persist a Denial Record in another separate
   transaction with the Requirement 50.6 three-attempt exponential-backoff
   retry, and raise :class:`SuccessConditionAssessmentAuthorizationError`
   carrying the AD-WS-9 indistinguishable-denial fields.
5. On a permit outcome, mint the Success-Condition Assessment Record Identity
   and the two Relationship Identities, register the Record in
   ``Identifier_Registry`` with its Slice 4 ``resource_kind`` tag (AD-WS-37),
   INSERT the ``Success_Condition_Assessment_Records`` row, the single
   ``Addresses`` Relationship to the target Intended Outcome Revision
   (``semantic_role IS NULL``), the single ``Cites`` Relationship to the
   sourced Observed Outcome Revision (``semantic_role = 'assessment_basis'``),
   and the consequential ``Audit_Records`` row — all inside the caller's
   transaction so a failure anywhere rolls every row back (Requirement 48.5 /
   57.1 / AD-WS-5). The addressed Intended Outcome Revision and the sourced
   Observed Outcome Revision remain byte-equivalent (Requirement 48.7).

Requirements satisfied
======================

    48.1 — authorized Success-Condition Assessment creation produces one
           immutable Success-Condition Assessment Record.
    48.2 — every Record records the Record identity, the target Intended
           Outcome Resource + Revision identities, the sourced Observed Outcome
           Resource + Revision identities, the assessment category, the
           assessment rationale (1..4000), the assessing Party Identity, the
           authority basis (type in the AD-WS-10 set), the applicable scope,
           the recorded time, one ``Addresses`` Relationship, and one ``Cites``
           Relationship.
    48.3 — an unresolvable / non-``intended`` target, an unresolvable sourced
           Observed Outcome Revision, a sourced Observed Outcome Revision whose
           ``Addresses`` target differs from the named target, an
           out-of-enumeration assessment category, an omitted rationale /
           authority basis / applicable scope, or an ``Unassessable`` category
           with a rationale shorter than 200 characters are rejected with
           nothing persisted and each invalid attribute identified.
    48.4 — unauthorized requests are denied; no Record is created and a Denial
           Record conforming to AD-WS-9 is appended.
    48.5 — every successful Record creation appends one immutable consequential
           audit row in the same transaction.
    48.6 — Success-Condition Assessment Records and their Relationships are
           immutable (enforced by the schema triggers).
    48.7 — the addressed Intended Outcome Revision and the sourced Observed
           Outcome Revision are never mutated.
    52.8 — ``create.success_condition_assessment`` requires ``assess_outcome``
           (AD-WS-33).
    53.2 — no Success-Condition Assessment creation request may carry an
           intended-side attribute.
    57.1 — the consequential audit append participates in the same transaction
           as the domain rows.
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
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.outcome._helpers import (
    OUTCOME_PROHIBITED_PREFIXES,
    OutcomeValidationError,
    _record_outcome_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.outcome.models import CreateAssessmentResult
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService


__all__ = [
    "SuccessConditionAssessmentAuditFailureError",
    "SuccessConditionAssessmentAuthorizationError",
    "SuccessConditionAssessmentRow",
    "SuccessConditionAssessmentService",
    "SuccessConditionAssessmentSourcingError",
    "SuccessConditionAssessmentTargetNotResolvableError",
    "SuccessConditionAssessmentValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# ``create.success_condition_assessment`` maps to the ``assess_outcome``
# authority per AD-WS-33. The string is also the ``action_type`` recorded on
# the consequential audit row (Requirement 48.5) and on the
# separate-transaction Denial Record (Requirement 48.4).
_ACTION_CREATE_ASSESSMENT: Final[str] = "create.success_condition_assessment"

# Relationship Types, source/target ``kind`` strings, and the AD-WS-35
# ``semantic_role`` values written to the ``Relationships`` rows.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_CITES: Final[str] = "Cites"
_SEMANTIC_ROLE_ASSESSMENT_BASIS: Final[str] = "assessment_basis"
_KIND_ASSESSMENT_RECORD: Final[str] = "success_condition_assessment_record"
_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"
_KIND_OBSERVED_OUTCOME_REVISION: Final[str] = "observed_outcome_revision"

# Identifier_Registry registration kind (Slice 1 enumeration) and the Slice 4
# ``resource_kind`` tag (AD-WS-37). A Success-Condition Assessment Record is an
# Immutable Record so its registry binding uses ``kind='immutable_record'``.
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_ASSESSMENT_RECORD: Final[str] = (
    "success_condition_assessment_record"
)

# Slice 2 persistence-invariant value the resolved target Intended Outcome
# Revision must carry to be a valid Assessment target (Requirement 48.3).
_OUTCOME_KIND_INTENDED: Final[str] = "intended"

# Assessment-category closed enumeration (Requirement 48.2).
_CATEGORY_UNASSESSABLE: Final[str] = "Unassessable"
_ASSESSMENT_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "Satisfied",
        "Partially_Satisfied",
        "Not_Satisfied",
        _CATEGORY_UNASSESSABLE,
    }
)

# Assessment-rationale length bounds (Requirement 48.2/48.3).
_RATIONALE_MIN_CHARS: Final[int] = 1
_RATIONALE_MAX_CHARS: Final[int] = 4_000
_RATIONALE_UNASSESSABLE_MIN_CHARS: Final[int] = 200

# Authority-basis type closed enumeration (Slice 1 AD-WS-10).
_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Exponential backoff sequence for retrying the separate-transaction Denial
# Record append (Requirement 50.6, mirroring the Slice 1/2/3 pattern). Three
# retries after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class SuccessConditionAssessmentValidationError(ValueError):
    """Raised when a Success-Condition Assessment submission fails Requirement
    48.3 / 53 validation.

    ``failed_constraint`` names the first specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a stable
    identifier rather than the message text. ``invalid_attributes`` lists every
    rejected attribute name so the response can identify each one
    (Requirement 48.3).

    Attributes:
        failed_constraint: A stable discriminator such as
            ``"assessment_category_invalid"``,
            ``"assessment_rationale_missing"``,
            ``"assessment_rationale_too_long"``,
            ``"assessment_rationale_too_short_for_unassessable"``,
            ``"authority_basis_missing"``,
            ``"authority_basis_type_invalid"``,
            ``"applicable_scope_missing"``,
            ``"assessing_party_id_missing"``,
            ``"target_intended_outcome_revision_id_missing"``,
            ``"sourced_observed_outcome_revision_id_missing"``,
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


class SuccessConditionAssessmentTargetNotResolvableError(LookupError):
    """Raised when the target Intended Outcome Revision does not resolve or is
    not an Intended Outcome (Requirement 48.3).

    Requirement 48.3 requires the named target Intended Outcome Revision
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
            f"Success-Condition Assessment target ({failed_constraint}, "
            "Requirement 48.3)."
        )
        self.target_intended_outcome_revision_id = (
            target_intended_outcome_revision_id
        )
        self.failed_constraint = failed_constraint
        self.invalid_attributes = ("target_intended_outcome_revision_id",)


class SuccessConditionAssessmentSourcingError(ValueError):
    """Raised when the sourced Observed Outcome Revision does not resolve or
    does not address the named target Intended Outcome Revision
    (Requirement 48.3).

    Every Success-Condition Assessment sources exactly one Observed Outcome
    Revision, and that Revision's ``Addresses`` target Intended Outcome
    Revision Identity must equal the named target Intended Outcome Revision
    Identity. ``failed_constraint`` is
    ``"sourced_observed_outcome_revision_not_resolvable"`` when the named
    sourced Revision does not resolve, or
    ``"sourced_observed_outcome_addresses_mismatch"`` when it resolves but
    addresses a different Intended Outcome Revision.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        sourced_observed_outcome_revision_id: str,
        sourced_addresses_target_revision_id: Optional[str] = None,
        named_target_revision_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.sourced_observed_outcome_revision_id = (
            sourced_observed_outcome_revision_id
        )
        self.sourced_addresses_target_revision_id = (
            sourced_addresses_target_revision_id
        )
        self.named_target_revision_id = named_target_revision_id
        self.invalid_attributes = ("sourced_observed_outcome_revision_id",)


class SuccessConditionAssessmentAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Success-Condition
    Assessment attempt (Requirement 48.4).

    Carries only ``reason_code`` and ``correlation_id`` — the AD-WS-9
    indistinguishable-denial contract forbids leaking authorized Party
    identities, target contents, or target existence beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Success-Condition Assessment creation denied: "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class SuccessConditionAssessmentAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 50.6).

    On total audit-append failure the exception is raised *in place of*
    :class:`SuccessConditionAssessmentAuthorizationError` — denial and audit
    have silently diverged and the operator must be told. The caller's
    transaction still rolls back so no Record, Relationship, or consequential
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
            f"Denial Record append for a denied Success-Condition Assessment "
            f"failed after {attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Read-model row.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuccessConditionAssessmentRow:
    """Read-model snapshot of a persisted ``Success_Condition_Assessment_Records``
    row.

    Returned by :meth:`SuccessConditionAssessmentService.get_assessment`.
    Backs the Outcome Review citation rule (Requirement 49.4, AD-WS-40):
    the
    :class:`~walking_slice.outcome.outcome_reviews.OutcomeReviewService`
    must (a) confirm every cited Success-Condition Assessment Record
    resolves and (b) recover each cited Assessment's ``Addresses`` target
    Intended Outcome Revision Identity
    (``target_intended_outcome_revision_id``) so it can verify that target
    equals the Outcome Review's named target Intended Outcome Revision. The
    remaining columns are surfaced for completeness and the provenance read
    paths. Identity values and the timestamp are carried as ``str`` to match
    the persisted column form.
    """

    assessment_id: str
    target_intended_outcome_resource_id: str
    target_intended_outcome_revision_id: str
    sourced_observed_outcome_id: str
    sourced_observed_outcome_revision_id: str
    assessment_category: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuccessConditionAssessmentService:
    """Persist Success-Condition Assessment Immutable Records, each with one
    ``Addresses`` Relationship to the target Intended Outcome Revision and one
    ``Cites`` Relationship to the sourced Observed Outcome Revision.

    Connection-scoped at call time: :meth:`create_assessment` accepts the
    caller's :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (AD-WS-5). The service instance therefore holds only
    the cross-request collaborators and can be shared across requests.

    Frozen because design §"Outcome_Service.SuccessConditionAssessments"
    declares it ``@dataclass(frozen=True)``.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Success_Condition_Assessment_Records``, ``Relationships``, and
            ``Audit_Records`` rows. Consulted exactly once per write so every
            artifact of the transaction shares one timestamp.
        identity_service: Generates the Success-Condition Assessment Record
            Identity and the two Relationship Identities and persists their
            ``Identifier_Registry`` bindings with the Slice 4 ``resource_kind``
            tag (AD-WS-37).
        audit_log: Appends the consequential audit row (Requirement 48.5)
            inside the caller's transaction.
        authorization_service: Evaluates
            ``create.success_condition_assessment`` → ``assess_outcome``
            authority per AD-WS-33 / Requirement 48.4; the deny path is the
            cumulative separate-transaction Denial-Record pattern.
        intended_outcome_reader: The Slice 2 :class:`IntendedOutcomeService`
            used read-only (``get_revision``) to resolve the target Intended
            Outcome Revision and verify ``outcome_kind = 'intended'`` (AD-WS-40
            / Requirement 48.3).
        observed_outcome_reader: The :class:`ObservedOutcomeService` used
            read-only (``get_observed_outcome_revision``) to resolve the
            sourced Observed Outcome Revision and recover its ``Addresses``
            target Intended Outcome Revision Identity for the Requirement 48.3
            sourcing check.
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
    observed_outcome_reader: ObservedOutcomeService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_assessment(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
        sourced_observed_outcome_revision_id: str,
        assessment_category: str,
        assessment_rationale: str,
        assessing_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateAssessmentResult:
        """Create an immutable Success-Condition Assessment Record plus its
        ``Addresses`` Relationship to the target Intended Outcome Revision and
        its ``Cites`` Relationship to the sourced Observed Outcome Revision.

        Per Requirements 48.1 through 48.7, 52.8 (``assess_outcome``), 53.2,
        57.1, AD-WS-9 (indistinguishable denial), AD-WS-33, AD-WS-35, AD-WS-37,
        AD-WS-40, and AD-WS-41.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_intended_outcome_revision_id: Identity of the single target
                Intended Outcome Revision the Assessment addresses
                (Requirement 48.3).
            sourced_observed_outcome_revision_id: Identity of the single
                sourced Observed Outcome Revision the Assessment cites; its
                ``Addresses`` target must equal
                ``target_intended_outcome_revision_id`` (Requirement 48.3).
            assessment_category: One of ``Satisfied``, ``Partially_Satisfied``,
                ``Not_Satisfied``, or ``Unassessable`` (Requirement 48.2).
            assessment_rationale: Rationale text (1..4000 chars; at least 200
                chars when the category is ``Unassessable``).
            assessing_party_id: Identity of the assessing Party.
            authority_basis: The :class:`AuthorityBasisRef` recorded on the
                Record; its ``type`` must be in the AD-WS-10 set (AD-WS-41).
            applicable_scope: Scope the Assessment applies within; also passed
                as ``target.scope`` to authorization.
            engine: Required for the deny path's separate-transaction Denial
                Record write and the separate-transaction authorization
                evaluation.
            correlation_id: Optional correlation identifier; a UUIDv7 is
                generated when omitted.
            evaluation_at: Optional explicit ``at`` for authorization; defaults
                to the recorded time.
            request_attributes: Optional raw request body screened against the
                intended-side prefix list (Requirement 53).

        Returns:
            :class:`CreateAssessmentResult` carrying the persisted identifiers,
            attributes, both Relationship Identities, the recorded time, and
            the correlation identifier.

        Raises:
            SuccessConditionAssessmentValidationError: A required attribute is
                missing, the category is out of enumeration, the rationale
                violates its length rule, the authority basis type is not in
                the AD-WS-10 set, or the request body carried a prohibited
                intended-side attribute.
            SuccessConditionAssessmentTargetNotResolvableError: The target
                Intended Outcome Revision did not resolve or its
                ``outcome_kind`` is not ``'intended'``.
            SuccessConditionAssessmentSourcingError: The sourced Observed
                Outcome Revision did not resolve or addresses a different
                Intended Outcome Revision.
            SuccessConditionAssessmentAuthorizationError: The attempt was
                denied; the Denial Record was appended in a separate
                transaction.
            SuccessConditionAssessmentAuditFailureError: The attempt was denied
                and the Denial Record append failed on every retry.
        """
        # 1. Validate inputs (Requirement 48.3 / 53).
        self._validate_inputs(
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            assessment_category=assessment_category,
            assessment_rationale=assessment_rationale,
            assessing_party_id=assessing_party_id,
            authority_basis=authority_basis,
            applicable_scope=applicable_scope,
            request_attributes=request_attributes,
        )

        # 2. Resolve the target Intended Outcome Revision via the additive
        # read-only Planning API (AD-WS-40). Reject when it does not resolve
        # or its outcome_kind is not 'intended' (Requirement 48.3). The check
        # runs before authorization so the deny path never reveals whether a
        # target exists for an unauthorized caller.
        target_intended_outcome_resource_id = self._resolve_intended_outcome(
            connection,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
        )

        # 3. Resolve the sourced Observed Outcome Revision and confirm its
        # Addresses target equals the named target Intended Outcome Revision
        # (Requirement 48.3).
        sourced_observed_outcome_id = self._resolve_sourced_observed_outcome(
            connection,
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
        )

        # 4 + 5. Authorize and persist.
        return self._persist_assessment(
            connection,
            engine=engine,
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            sourced_observed_outcome_id=sourced_observed_outcome_id,
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            assessment_category=assessment_category,
            assessment_rationale=assessment_rationale,
            assessing_party_id=assessing_party_id,
            authority_basis=authority_basis,
            applicable_scope=applicable_scope,
            correlation_id=correlation_id,
            evaluation_at=evaluation_at,
        )

    # -- read helper -------------------------------------------------------

    @staticmethod
    def get_assessment(
        connection: Connection,
        assessment_id: str,
    ) -> Optional[SuccessConditionAssessmentRow]:
        """Read-only lookup of a Success-Condition Assessment Record by its
        Identity.

        Backs the Outcome Review citation rule (Requirement 49.4, AD-WS-40):
        the
        :class:`~walking_slice.outcome.outcome_reviews.OutcomeReviewService`
        must (a) confirm every cited Success-Condition Assessment Record
        resolves and (b) recover each cited Assessment's ``Addresses`` target
        Intended Outcome Revision Identity so it can verify that target equals
        the Outcome Review's named target Intended Outcome Revision; a cited
        Assessment that does not resolve, or whose ``Addresses`` target
        differs from the named target, is rejected with nothing persisted.

        Introduces no write path. It is a :func:`staticmethod` because the
        read consults none of the wired collaborators — it needs only the
        caller's :class:`~sqlalchemy.engine.Connection`. Returns ``None`` when
        the supplied Identity does not resolve so the caller can treat the
        absent case without try/except.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context.
            assessment_id: The Success-Condition Assessment Record Identity to
                resolve.

        Returns:
            A :class:`SuccessConditionAssessmentRow` snapshot when the Record
            exists; ``None`` otherwise.
        """
        row = connection.execute(
            text(
                "SELECT assessment_id, target_intended_outcome_resource_id, "
                "target_intended_outcome_revision_id, "
                "sourced_observed_outcome_id, "
                "sourced_observed_outcome_revision_id, "
                "assessment_category, applicable_scope, recorded_at "
                "FROM Success_Condition_Assessment_Records "
                "WHERE assessment_id = :assessment_id"
            ),
            {"assessment_id": assessment_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        return SuccessConditionAssessmentRow(
            assessment_id=row["assessment_id"],
            target_intended_outcome_resource_id=(
                row["target_intended_outcome_resource_id"]
            ),
            target_intended_outcome_revision_id=(
                row["target_intended_outcome_revision_id"]
            ),
            sourced_observed_outcome_id=row["sourced_observed_outcome_id"],
            sourced_observed_outcome_revision_id=(
                row["sourced_observed_outcome_revision_id"]
            ),
            assessment_category=row["assessment_category"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    # -- validation --------------------------------------------------------

    def _validate_inputs(
        self,
        *,
        target_intended_outcome_revision_id: str,
        sourced_observed_outcome_revision_id: str,
        assessment_category: str,
        assessment_rationale: str,
        assessing_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        request_attributes: Optional[Mapping[str, Any]],
    ) -> None:
        """Validate the request inputs (Requirement 48.3 / 53).

        Screens the raw request body for prohibited intended-side keys
        (Requirement 53) then validates the required attributes, the category
        enumeration, the rationale length rule (including the Unassessable
        ``>= 200`` rule), and the authority basis.
        """
        self._screen_request_attributes(request_attributes)

        # Required string identifiers (Requirement 48.3).
        missing: list[str] = []
        if not _is_present_str(target_intended_outcome_revision_id):
            missing.append("target_intended_outcome_revision_id")
        if not _is_present_str(sourced_observed_outcome_revision_id):
            missing.append("sourced_observed_outcome_revision_id")
        if not _is_present_str(assessing_party_id):
            missing.append("assessing_party_id")
        if not _is_present_str(applicable_scope):
            missing.append("applicable_scope")
        if missing:
            raise SuccessConditionAssessmentValidationError(
                f"required Success-Condition Assessment attribute(s) "
                f"{missing!r} are missing (Requirement 48.3).",
                failed_constraint=f"{missing[0]}_missing",
                invalid_attributes=tuple(missing),
            )

        # Assessment category drawn from the closed enumeration
        # (Requirement 48.2/48.3).
        if assessment_category not in _ASSESSMENT_CATEGORIES:
            raise SuccessConditionAssessmentValidationError(
                f"assessment category {assessment_category!r} is outside the "
                f"enumerated set {sorted(_ASSESSMENT_CATEGORIES)} "
                "(Requirement 48.3).",
                failed_constraint="assessment_category_invalid",
                invalid_attributes=("assessment_category",),
            )

        # Assessment rationale presence + length (Requirement 48.2/48.3).
        if not _is_present_str(assessment_rationale):
            raise SuccessConditionAssessmentValidationError(
                "assessment rationale is missing (Requirement 48.3).",
                failed_constraint="assessment_rationale_missing",
                invalid_attributes=("assessment_rationale",),
            )
        if len(assessment_rationale) > _RATIONALE_MAX_CHARS:
            raise SuccessConditionAssessmentValidationError(
                f"assessment rationale is {len(assessment_rationale)} "
                f"characters; at most {_RATIONALE_MAX_CHARS} are permitted "
                "(Requirement 48.2).",
                failed_constraint="assessment_rationale_too_long",
                invalid_attributes=("assessment_rationale",),
            )
        # Unassessable requires a >= 200-character rationale (Requirement 48.3).
        if (
            assessment_category == _CATEGORY_UNASSESSABLE
            and len(assessment_rationale) < _RATIONALE_UNASSESSABLE_MIN_CHARS
        ):
            raise SuccessConditionAssessmentValidationError(
                f"an {_CATEGORY_UNASSESSABLE!r} assessment requires a "
                f"rationale of at least {_RATIONALE_UNASSESSABLE_MIN_CHARS} "
                f"characters; received {len(assessment_rationale)} "
                "(Requirement 48.3).",
                failed_constraint=(
                    "assessment_rationale_too_short_for_unassessable"
                ),
                invalid_attributes=("assessment_rationale",),
            )

        # Authority basis present, type in the AD-WS-10 set (AD-WS-41).
        if authority_basis is None:
            raise SuccessConditionAssessmentValidationError(
                "authority basis is missing (Requirement 48.3).",
                failed_constraint="authority_basis_missing",
                invalid_attributes=("authority_basis",),
            )
        basis_type = getattr(authority_basis, "type", None)
        if basis_type not in _AUTHORITY_BASIS_TYPES:
            raise SuccessConditionAssessmentValidationError(
                f"authority basis type {basis_type!r} is outside the AD-WS-10 "
                f"set {sorted(_AUTHORITY_BASIS_TYPES)} (Requirement 48.3).",
                failed_constraint="authority_basis_type_invalid",
                invalid_attributes=("authority_basis",),
            )

    def _resolve_intended_outcome(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
    ) -> str:
        """Resolve the target Intended Outcome Revision and return its target
        Intended Outcome **Resource** Identity.

        Rejects when the Revision does not resolve or its ``outcome_kind`` is
        not the literal ``'intended'`` (Requirement 48.3, AD-WS-40).
        """
        revision_row = self.intended_outcome_reader.get_revision(
            connection, target_intended_outcome_revision_id
        )
        if revision_row is None:
            raise SuccessConditionAssessmentTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                failed_constraint="target_intended_outcome_not_resolvable",
            )
        if revision_row.outcome_kind != _OUTCOME_KIND_INTENDED:
            raise SuccessConditionAssessmentTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                failed_constraint="target_outcome_kind_not_intended",
            )
        return revision_row.intended_outcome_id

    def _resolve_sourced_observed_outcome(
        self,
        connection: Connection,
        *,
        sourced_observed_outcome_revision_id: str,
        target_intended_outcome_revision_id: str,
    ) -> str:
        """Resolve the sourced Observed Outcome Revision and confirm its
        ``Addresses`` target equals the named target Intended Outcome Revision.

        Returns the sourced Observed Outcome **Resource** Identity for
        persistence. Rejects when the Revision does not resolve
        (Requirement 48.3) or addresses a different Intended Outcome Revision
        (Requirement 48.3).
        """
        revision_row = (
            self.observed_outcome_reader.get_observed_outcome_revision(
                connection,
                observed_outcome_revision_id=(
                    sourced_observed_outcome_revision_id
                ),
            )
        )
        if revision_row is None:
            raise SuccessConditionAssessmentSourcingError(
                f"sourced Observed Outcome Revision "
                f"{sourced_observed_outcome_revision_id!r} does not resolve "
                "(Requirement 48.3).",
                failed_constraint=(
                    "sourced_observed_outcome_revision_not_resolvable"
                ),
                sourced_observed_outcome_revision_id=(
                    sourced_observed_outcome_revision_id
                ),
            )
        if (
            revision_row.target_intended_outcome_revision_id
            != target_intended_outcome_revision_id
        ):
            raise SuccessConditionAssessmentSourcingError(
                f"sourced Observed Outcome Revision "
                f"{sourced_observed_outcome_revision_id!r} addresses Intended "
                f"Outcome Revision "
                f"{revision_row.target_intended_outcome_revision_id!r}, which "
                f"does not equal the named target Intended Outcome Revision "
                f"{target_intended_outcome_revision_id!r} "
                "(Requirement 48.3).",
                failed_constraint=(
                    "sourced_observed_outcome_addresses_mismatch"
                ),
                sourced_observed_outcome_revision_id=(
                    sourced_observed_outcome_revision_id
                ),
                sourced_addresses_target_revision_id=(
                    revision_row.target_intended_outcome_revision_id
                ),
                named_target_revision_id=target_intended_outcome_revision_id,
            )
        return revision_row.observed_outcome_id

    # -- persistence -------------------------------------------------------

    def _persist_assessment(
        self,
        connection: Connection,
        *,
        engine: Engine,
        target_intended_outcome_resource_id: str,
        target_intended_outcome_revision_id: str,
        sourced_observed_outcome_id: str,
        sourced_observed_outcome_revision_id: str,
        assessment_category: str,
        assessment_rationale: str,
        assessing_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        correlation_id: Optional[str],
        evaluation_at: Optional[datetime],
    ) -> CreateAssessmentResult:
        """Authorize and persist a Success-Condition Assessment Record, its
        ``Addresses`` Relationship, its ``Cites`` Relationship, and the
        consequential audit row.

        The authorization evaluation runs on a SEPARATE transaction; on deny it
        drives the AD-WS-9 separate-transaction Denial-Record pattern with the
        Requirement 50.6 retry. On permit it inserts the registry binding, the
        ``Success_Condition_Assessment_Records`` row, the ``Addresses`` and
        ``Cites`` Relationships, and the consequential audit row — all inside
        the caller's transaction so a failure anywhere rolls every row back
        (Requirements 48.1/48.2/48.5, 57.1).
        """
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
                party_id=assessing_party_id,
                action=_ACTION_CREATE_ASSESSMENT,
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
            self._persist_assessment_denial(
                engine=engine,
                actor_party_id=assessing_party_id,
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise SuccessConditionAssessmentAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # Mint identifiers (AD-WS-37). The Assessment Record is an Immutable
        # Record; the two Relationship rows each take a fresh Relationship
        # Identity.
        assessment_id = str(self.identity_service.new_immutable_record_id())
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        cites_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        authority_basis_type = authority_basis.type
        authority_basis_id = str(authority_basis.id)

        content_digest = _sha256_hex(
            json.dumps(
                {
                    "assessment_id": assessment_id,
                    "target_intended_outcome_resource_id": (
                        target_intended_outcome_resource_id
                    ),
                    "target_intended_outcome_revision_id": (
                        target_intended_outcome_revision_id
                    ),
                    "sourced_observed_outcome_id": sourced_observed_outcome_id,
                    "sourced_observed_outcome_revision_id": (
                        sourced_observed_outcome_revision_id
                    ),
                    "assessment_category": assessment_category,
                    "assessment_rationale": assessment_rationale,
                    "assessing_party_id": assessing_party_id,
                    "authority_basis_type": authority_basis_type,
                    "authority_basis_id": authority_basis_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # Register the Assessment Record Identity in ``Identifier_Registry``
        # carrying the AD-WS-37 ``resource_kind`` tag.
        _record_outcome_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_ASSESSMENT_RECORD,
            assessment_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=assessing_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_ASSESSMENT,
            recorded_time=recorded_time,
        )

        # Insert the immutable Success-Condition Assessment Record
        # (Requirement 48.2). The schema CHECK enforces the category
        # enumeration, the 1..4000 rationale bound, and the Unassessable
        # >= 200 rule a second time.
        connection.execute(
            text(
                """
                INSERT INTO Success_Condition_Assessment_Records (
                    assessment_id,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    sourced_observed_outcome_id,
                    sourced_observed_outcome_revision_id,
                    assessment_category,
                    assessment_rationale,
                    assessing_party_id,
                    authority_basis_type,
                    authority_basis_id,
                    applicable_scope,
                    recorded_at
                ) VALUES (
                    :assessment_id,
                    :target_intended_outcome_resource_id,
                    :target_intended_outcome_revision_id,
                    :sourced_observed_outcome_id,
                    :sourced_observed_outcome_revision_id,
                    :assessment_category,
                    :assessment_rationale,
                    :assessing_party_id,
                    :authority_basis_type,
                    :authority_basis_id,
                    :applicable_scope,
                    :recorded_at
                )
                """
            ),
            {
                "assessment_id": assessment_id,
                "target_intended_outcome_resource_id": (
                    target_intended_outcome_resource_id
                ),
                "target_intended_outcome_revision_id": (
                    target_intended_outcome_revision_id
                ),
                "sourced_observed_outcome_id": sourced_observed_outcome_id,
                "sourced_observed_outcome_revision_id": (
                    sourced_observed_outcome_revision_id
                ),
                "assessment_category": assessment_category,
                "assessment_rationale": assessment_rationale,
                "assessing_party_id": assessing_party_id,
                "authority_basis_type": authority_basis_type,
                "authority_basis_id": authority_basis_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # Single ``Addresses`` Relationship to the target Intended Outcome
        # Revision (AD-WS-35, semantic_role IS NULL). The source is the
        # Assessment Record, an Immutable Record with no Revision, so
        # source_revision_id is NULL. The target is a Revision-bearing entity,
        # so target_id is the Resource Identity and target_revision_id the
        # Revision Identity.
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
                    :authoring_party_id, :recorded_at, NULL
                )
                """
            ),
            {
                "relationship_id": addresses_relationship_id,
                "relationship_type": _RELATIONSHIP_TYPE_ADDRESSES,
                "source_kind": _KIND_ASSESSMENT_RECORD,
                "source_id": assessment_id,
                "target_kind": _KIND_INTENDED_OUTCOME_REVISION,
                "target_id": target_intended_outcome_resource_id,
                "target_revision_id": target_intended_outcome_revision_id,
                "authoring_party_id": assessing_party_id,
                "recorded_at": recorded_at,
            },
        )

        # Single ``Cites`` Relationship to the sourced Observed Outcome
        # Revision (AD-WS-35, semantic_role = 'assessment_basis'). The target
        # is a Revision-bearing entity, so target_id is the Observed Outcome
        # Resource Identity and target_revision_id the Revision Identity.
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
                "source_kind": _KIND_ASSESSMENT_RECORD,
                "source_id": assessment_id,
                "target_kind": _KIND_OBSERVED_OUTCOME_REVISION,
                "target_id": sourced_observed_outcome_id,
                "target_revision_id": sourced_observed_outcome_revision_id,
                "authoring_party_id": assessing_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_ASSESSMENT_BASIS,
            },
        )

        # Consequential audit row (Requirement 48.5 / 57.1 / AD-WS-5).
        # Participates in the caller's transaction. The Assessment Record is an
        # Immutable Record with no Revision, so target_revision_id is None.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=assessing_party_id,
            action_type=_ACTION_CREATE_ASSESSMENT,
            target_id=assessment_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateAssessmentResult(
            assessment_id=assessment_id,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            assessment_category=assessment_category,
            assessment_rationale=assessment_rationale,
            assessing_party_id=assessing_party_id,
            authority_basis=authority_basis,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            cites_relationship_id=cites_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- denial side-channel ----------------------------------------------

    def _persist_assessment_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_intended_outcome_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Success-Condition Assessment
        attempt.

        Implements the Requirement 50.6 retry contract verbatim (mirroring the
        Slice 1/2/3 pattern and the sibling Outcome_Service services): each
        attempt opens a *new* :meth:`Engine.begin` transaction, tries
        :meth:`AuditLog.append_denial`, and either returns on success or pauses
        by the next entry in :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the
        next try. If every attempt fails,
        :class:`SuccessConditionAssessmentAuditFailureError` is raised.

        The separate transaction is essential: the caller's originating
        transaction is about to be rolled back when the create method raises
        :class:`SuccessConditionAssessmentAuthorizationError`. The Denial
        Record must therefore live outside that scope to survive (AD-WS-9 /
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
                        attempted_action=_ACTION_CREATE_ASSESSMENT,
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

        raise SuccessConditionAssessmentAuditFailureError(
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
            raise SuccessConditionAssessmentValidationError(
                str(exc),
                failed_constraint="prohibited_attribute",
                invalid_attributes=exc.prohibited_keys,
                prohibited_keys=exc.prohibited_keys,
            ) from exc


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
    produced for the same logical Success-Condition Assessment write. They are
    not registered with :class:`IdentityService` because they do not name a
    domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Success-Condition
    Assessment Record Identity in ``Identifier_Registry``, mirroring the
    Slice 1/2/3/4 convention.
    """
    return hashlib.sha256(content).hexdigest()
