"""Outcome_Service.OutcomeReviews — Outcome Review Governance Decision
Immutable Records, each with one ``Addresses`` Relationship to the target
Intended Outcome Revision, one ``Cites`` Relationship per cited
Success-Condition Assessment, one per cited Slice 3 Completion Record, and one
per cited produced Deliverable Revision.

Design reference
================

``.kiro/specs/fourth-walking-slice/design.md``:

- §"Outcome_Service.OutcomeReviews" — the public dataclass surface
  (``create_outcome_review``), the authority mapping (``create.outcome_review``
  → ``issue_outcome_review`` per AD-WS-33 / Requirement 52.9), and the
  validation rules (Requirement 49): the review-outcome category is drawn from
  ``{Achieved, Partially_Achieved, Not_Achieved, Inconclusive}``; the
  attribution stance is drawn from ``{Asserted, Partial, Unattributed,
  Contradicted}``; the confidence indicator is drawn from ``{High, Moderate,
  Low}``; the review rationale is 1..4000 characters; the attribution-evidence
  reference is 0..4000 characters and at least 1 character when the stance is
  ``Asserted`` or ``Contradicted`` (Requirement 49.4); the authority basis type
  is in the AD-WS-10 set; the applicable scope is present; the target Intended
  Outcome Revision resolves and is ``intended``; at most one Outcome Review
  exists per target Intended Outcome Revision (Requirement 49.3); at least one
  Success-Condition Assessment and at least one Completion Record are cited;
  every cited Assessment resolves and its ``Addresses`` target equals the named
  target (Requirement 49.4); every cited Completion Record resolves; every
  cited produced Deliverable Revision resolves.
- §"Relationships rows written by Slice 4" — an Outcome Review Record writes
  one ``Addresses`` Relationship to the target Intended Outcome Revision
  (``semantic_role IS NULL``), one ``Cites`` Relationship per cited
  Success-Condition Assessment (``semantic_role = 'review_assessment'``), one
  per cited Completion Record (``semantic_role = 'review_completion'``), and one
  per cited produced Deliverable Revision (``semantic_role =
  'review_deliverable'``) (AD-WS-35).
- §"Cross-Cutting Concerns" — Transactionality (one recorded time shared by
  every row in the transaction), Identifiers (every new identity is a UUIDv7
  minted by :class:`IdentityService` and registered in ``Identifier_Registry``
  with the additive Slice 4 ``resource_kind`` tag per AD-WS-37; an Outcome
  Review Record is a Governance Decision Immutable Record so its registry
  binding uses ``kind='immutable_record'``), Authorization (the deny path
  reuses the cumulative separate-transaction Denial-Record pattern with the
  Requirement 50.6 three-attempt retry, and the AD-WS-9 view-authority gate on
  the duplicate-Review conflict response).
- AD-WS-40 — the cited Slice 3 Completion Records resolve via the additive
  read-only ``CompletionService.get_completion`` Execution_Service read, and
  the cited produced Deliverable Revisions resolve via
  ``DeliverableRepositoryService.get_revision``.
- AD-WS-41 — the authority basis recorded on the Record is an
  :class:`AuthorityBasisRef` whose ``type`` is in the AD-WS-10 set.

Task scope (task 9.1)
=====================

This module implements
:meth:`OutcomeReviewService.create_outcome_review`:

1. Screen the raw request body for prohibited intended-side keys
   (Requirement 53) and the Completion-as-Outcome intent markers
   (Requirements 54.1 / 54.4) and validate the required attributes: the
   review-outcome, attribution-stance, and confidence enumerations; the review
   rationale length (1..4000); the attribution-evidence rule (non-empty when
   ``Asserted`` / ``Contradicted``, Requirement 49.4); the authority basis
   (present, type in the AD-WS-10 set); the applicable scope; the reviewing
   Party Identity; at least one cited Success-Condition Assessment and at least
   one cited Completion Record.
2. Resolve the target Intended Outcome Revision via
   ``intended_outcome_reader.get_revision(...)`` (AD-WS-40); reject when it does
   not resolve or its ``outcome_kind != 'intended'`` (Requirement 49.4).
3. Pre-check the ``UNIQUE(target_intended_outcome_revision_id)`` constraint
   (Requirement 49.3); on conflict raise :class:`OutcomeReviewConflictError`
   carrying the existing ``outcome_review_id`` only when the caller holds view
   authority on it (AD-WS-9).
4. Resolve every cited Success-Condition Assessment via
   ``assessment_reader.get_assessment(...)`` (reject when any does not resolve
   or its ``Addresses`` target differs from the named target); resolve every
   cited Completion Record via ``completion_reader.get_completion(...)`` and
   every cited produced Deliverable Revision via
   ``deliverable_reader.get_revision(...)`` (reject any unresolvable
   identifier).
5. Evaluate ``Authorization_Service.evaluate(party, "create.outcome_review",
   target, at)`` on a *separate* transaction; on a deny outcome, persist a
   Denial Record in another separate transaction with the Requirement 50.6
   three-attempt retry, and raise :class:`OutcomeReviewAuthorizationError`
   carrying the AD-WS-9 indistinguishable-denial fields.
6. On a permit outcome, mint the Outcome Review Record Identity and the
   Relationship Identities, register the Record in ``Identifier_Registry`` with
   its Slice 4 ``resource_kind`` tag (AD-WS-37), INSERT the
   ``Outcome_Review_Records`` row, the single ``Addresses`` Relationship, one
   ``Cites`` Relationship per cited Assessment / Completion / produced
   Deliverable Revision, and the consequential ``Audit_Records`` row — all
   inside the caller's transaction so a failure anywhere rolls every row back
   (Requirement 49.6 / 57.1 / AD-WS-5).

The Review is created only by explicit request and never as a side effect of
any Slice 3 finalization (Requirements 49.9, 54.1): this module exposes only an
explicit ``create_outcome_review`` entry point and is never invoked from any
Slice 3 Completion / Milestone Acceptance / Deliverable Production code path.

Requirements satisfied
======================

    49.1 — authorized Outcome Review creation produces one immutable Outcome
           Review Record.
    49.2 — every Record records the Record identity, the target Intended
           Outcome Resource + Revision identities, the review-outcome category,
           the attribution stance, the confidence indicator, the review
           rationale (1..4000), the attribution-evidence reference (0..4000),
           the cited Success-Condition Assessment / Completion Record / produced
           Deliverable Revision identities, the reviewing Party Identity, the
           authority basis (type in the AD-WS-10 set), the applicable scope, the
           recorded time, one ``Addresses`` Relationship, and one ``Cites``
           Relationship per cited Assessment, Completion, and produced
           Deliverable Revision.
    49.3 — at most one Outcome Review Record per target Intended Outcome
           Revision; a second attempt is rejected with nothing persisted (the
           schema ``UNIQUE`` is the source of truth; the pre-check surfaces the
           AD-WS-9 view-authority gate on the existing Identity).
    49.4 — an unresolvable / non-``intended`` target, a duplicate target, an
           unresolvable cited Assessment, a cited Assessment whose ``Addresses``
           target differs from the named target, an unresolvable cited
           Completion Record, an unresolvable cited produced Deliverable
           Revision, an out-of-enumeration review-outcome / attribution-stance /
           confidence, an ``Asserted`` / ``Contradicted`` stance with an empty
           attribution-evidence reference, zero cited Assessments, zero cited
           Completion Records, an omitted review rationale / authority basis /
           applicable scope are rejected with nothing persisted and each
           invalid attribute identified.
    49.5 — unauthorized requests are denied; no Record is created and a Denial
           Record conforming to AD-WS-9 is appended.
    49.6 — every successful Record creation appends one immutable consequential
           audit row in the same transaction.
    49.7 — Outcome Review Records and their Relationships are immutable
           (enforced by the schema triggers).
    49.8 — the target Intended Outcome Revision and all cited prior-slice
           Records remain byte-equivalent (no UPDATE / INSERT / DELETE against
           them).
    49.9 — the Review is created only by explicit request and never as a side
           effect of any Slice 3 finalization.
    52.9 — ``create.outcome_review`` requires ``issue_outcome_review``
           (AD-WS-33).
    53.2 — no Outcome Review creation request may carry an intended-side
           attribute.
    54.1 / 54.4 — no Outcome Review is derived automatically from a Completion
           Record; any field asserting Outcome from Completion alone or
           aliasing a Completion Record as an Observed Outcome is rejected.
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
from typing import Any, Callable, Final, Literal, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._helpers import (
    OUTCOME_PROHIBITED_PREFIXES,
    OutcomeValidationError,
    _record_outcome_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.outcome.models import CreateOutcomeReviewResult
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.planning.intended_outcomes import IntendedOutcomeService


__all__ = [
    "OutcomeReviewAuditFailureError",
    "OutcomeReviewAuthorizationError",
    "OutcomeReviewCitationError",
    "OutcomeReviewConflictError",
    "OutcomeReviewService",
    "OutcomeReviewTargetNotResolvableError",
    "OutcomeReviewValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# ``create.outcome_review`` maps to the ``issue_outcome_review`` authority per
# AD-WS-33 / Requirement 52.9. The string is also the ``action_type`` recorded
# on the consequential audit row (Requirement 49.6) and on the
# separate-transaction Denial Record (Requirement 49.5).
_ACTION_CREATE_OUTCOME_REVIEW: Final[str] = "create.outcome_review"

# ``view.outcome_review`` is the action used by the duplicate-Review
# pre-check view-authority gate (AD-WS-9 / Requirement 49.3). Mapped to the
# ``view`` authority by the authorization layer's prefix fallback.
_ACTION_VIEW_OUTCOME_REVIEW: Final[str] = "view.outcome_review"

# Relationship Types and the AD-WS-35 ``semantic_role`` values written to the
# ``Relationships`` rows.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_CITES: Final[str] = "Cites"
_SEMANTIC_ROLE_REVIEW_ASSESSMENT: Final[str] = "review_assessment"
_SEMANTIC_ROLE_REVIEW_COMPLETION: Final[str] = "review_completion"
_SEMANTIC_ROLE_REVIEW_DELIVERABLE: Final[str] = "review_deliverable"

# ``Relationships.source_kind`` / ``target_kind`` strings.
_KIND_OUTCOME_REVIEW_RECORD: Final[str] = "outcome_review_record"
_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"
_KIND_ASSESSMENT_RECORD: Final[str] = "success_condition_assessment_record"
_KIND_COMPLETION_RECORD: Final[str] = "completion_record"
_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"

# Identifier_Registry registration kind (Slice 1 enumeration) and the Slice 4
# ``resource_kind`` tag (AD-WS-37). An Outcome Review Record is a Governance
# Decision Immutable Record so its registry binding uses
# ``kind='immutable_record'``.
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_OUTCOME_REVIEW_RECORD: Final[str] = "outcome_review_record"

# Slice 2 persistence-invariant value the resolved target Intended Outcome
# Revision must carry to be a valid Outcome Review target (Requirement 49.4).
_OUTCOME_KIND_INTENDED: Final[str] = "intended"

# Review-outcome, attribution-stance, and confidence closed enumerations
# (Requirement 49.2).
_REVIEW_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"}
)
_ATTRIBUTION_STANCES: Final[frozenset[str]] = frozenset(
    {"Asserted", "Partial", "Unattributed", "Contradicted"}
)
# The two attribution stances that require a non-empty attribution-evidence
# reference (Requirement 49.4).
_STANCES_REQUIRING_EVIDENCE: Final[frozenset[str]] = frozenset(
    {"Asserted", "Contradicted"}
)
_CONFIDENCE_LEVELS: Final[frozenset[str]] = frozenset(
    {"High", "Moderate", "Low"}
)

# Review-rationale and attribution-evidence length bounds (Requirement 49.2).
_RATIONALE_MIN_CHARS: Final[int] = 1
_RATIONALE_MAX_CHARS: Final[int] = 4_000
_EVIDENCE_MAX_CHARS: Final[int] = 4_000

# Authority-basis type closed enumeration (Slice 1 AD-WS-10).
_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Exponential backoff sequence for retrying the separate-transaction Denial
# Record append (Requirement 50.6, mirroring the Slice 1/2/3/4 pattern). Three
# retries after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class OutcomeReviewValidationError(ValueError):
    """Raised when an Outcome Review submission fails Requirement 49.4 / 53 /
    54 validation.

    ``failed_constraint`` names the first specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a stable
    identifier rather than the message text. ``invalid_attributes`` lists every
    rejected attribute name so the response can identify each one
    (Requirement 49.4).

    Attributes:
        failed_constraint: A stable discriminator such as
            ``"review_outcome_invalid"``, ``"attribution_stance_invalid"``,
            ``"confidence_invalid"``, ``"review_rationale_missing"``,
            ``"review_rationale_too_long"``,
            ``"attribution_evidence_reference_missing_for_stance"``,
            ``"attribution_evidence_reference_too_long"``,
            ``"authority_basis_missing"``, ``"authority_basis_type_invalid"``,
            ``"applicable_scope_missing"``, ``"reviewing_party_id_missing"``,
            ``"target_intended_outcome_revision_id_missing"``,
            ``"cited_assessment_ids_empty"``,
            ``"cited_completion_ids_empty"``, ``"cited_assessment_id_invalid"``,
            ``"cited_completion_id_invalid"``,
            ``"cited_produced_deliverable_revision_id_invalid"``,
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


class OutcomeReviewTargetNotResolvableError(LookupError):
    """Raised when the target Intended Outcome Revision does not resolve or is
    not an Intended Outcome (Requirement 49.4).

    Requirement 49.4 requires the named target Intended Outcome Revision
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
            f"{target_intended_outcome_revision_id!r} is not a usable Outcome "
            f"Review target ({failed_constraint}, Requirement 49.4)."
        )
        self.target_intended_outcome_revision_id = (
            target_intended_outcome_revision_id
        )
        self.failed_constraint = failed_constraint
        self.invalid_attributes = ("target_intended_outcome_revision_id",)


class OutcomeReviewCitationError(ValueError):
    """Raised when a cited Success-Condition Assessment, Completion Record, or
    produced Deliverable Revision does not resolve, or when a cited Assessment
    addresses a different Intended Outcome Revision than the named target
    (Requirement 49.4).

    ``failed_constraint`` is one of
    ``"cited_assessment_not_resolvable"``,
    ``"cited_assessment_addresses_mismatch"``,
    ``"cited_completion_not_resolvable"``, or
    ``"cited_produced_deliverable_revision_not_resolvable"``. ``offending_id``
    carries the specific identifier that failed so the HTTP layer can identify
    it in the response.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        offending_id: str,
        invalid_attributes: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.offending_id = offending_id
        self.invalid_attributes = invalid_attributes


class OutcomeReviewConflictError(LookupError):
    """Raised when an Outcome Review Record already exists for the target
    Intended Outcome Revision (Requirement 49.3).

    The schema-level ``UNIQUE(target_intended_outcome_revision_id)`` constraint
    is the source of truth; this application-level pre-check surfaces a
    structured ``outcome_review_already_exists`` response and applies the
    AD-WS-9 view-authority gate on the existing Identity.

    ``existing_outcome_review_id`` is populated only when the caller holds
    effective ``view`` authority on the existing Outcome Review Record
    (AD-WS-9). When the caller lacks view authority, the field is ``None`` so
    the conflict response is byte-equivalent to a response that lacks the
    existing-Identity field, keeping it indistinguishable from a non-existent
    endpoint.
    """

    def __init__(
        self,
        *,
        target_intended_outcome_revision_id: str,
        existing_outcome_review_id: Optional[str],
        failed_constraint: str = "outcome_review_already_exists",
    ) -> None:
        super().__init__(
            f"an Outcome Review Record already exists for target Intended "
            f"Outcome Revision {target_intended_outcome_revision_id!r} "
            "(Requirement 49.3)."
        )
        self.target_intended_outcome_revision_id = (
            target_intended_outcome_revision_id
        )
        self.existing_outcome_review_id = existing_outcome_review_id
        self.failed_constraint = failed_constraint


class OutcomeReviewAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies an Outcome Review
    attempt (Requirement 49.5).

    Carries only ``reason_code`` and ``correlation_id`` — the AD-WS-9
    indistinguishable-denial contract forbids leaking authorized Party
    identities, target contents, or target existence beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Outcome Review creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class OutcomeReviewAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 50.6).

    On total audit-append failure the exception is raised *in place of*
    :class:`OutcomeReviewAuthorizationError` — denial and audit have silently
    diverged and the operator must be told. The caller's transaction still
    rolls back so no Record, Relationship, or consequential audit row is
    persisted.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Outcome Review failed after "
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
class OutcomeReviewService:
    """Persist Outcome Review Governance Decision Immutable Records, each with
    one ``Addresses`` Relationship to the target Intended Outcome Revision and
    one ``Cites`` Relationship per cited Success-Condition Assessment, cited
    Completion Record, and cited produced Deliverable Revision.

    Connection-scoped at call time: :meth:`create_outcome_review` accepts the
    caller's :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (AD-WS-5). The service instance therefore holds only
    the cross-request collaborators and can be shared across requests.

    Frozen because design §"Outcome_Service.OutcomeReviews" declares it
    ``@dataclass(frozen=True)``.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Outcome_Review_Records``, ``Relationships``, and ``Audit_Records``
            rows. Consulted exactly once per write so every artifact of the
            transaction shares one timestamp.
        identity_service: Generates the Outcome Review Record Identity and the
            Relationship Identities and persists their ``Identifier_Registry``
            bindings with the Slice 4 ``resource_kind`` tag (AD-WS-37).
        audit_log: Appends the consequential audit row (Requirement 49.6)
            inside the caller's transaction.
        authorization_service: Evaluates ``create.outcome_review`` →
            ``issue_outcome_review`` authority per AD-WS-33 / Requirement 49.5;
            the deny path is the cumulative separate-transaction Denial-Record
            pattern and the view-authority gate on the duplicate-Review
            conflict response.
        intended_outcome_reader: The Slice 2 :class:`IntendedOutcomeService`
            used read-only (``get_revision``) to resolve the target Intended
            Outcome Revision and verify ``outcome_kind = 'intended'``
            (AD-WS-40 / Requirement 49.4).
        assessment_reader: The :class:`SuccessConditionAssessmentService` used
            read-only (``get_assessment``) to resolve each cited
            Success-Condition Assessment Record and recover its ``Addresses``
            target Intended Outcome Revision Identity for the Requirement 49.4
            citation check.
        completion_reader: The Slice 3 :class:`CompletionService` used
            read-only (``get_completion``) to resolve each cited Completion
            Record (AD-WS-40 / Requirement 49.4).
        deliverable_reader: The Slice 3 :class:`DeliverableRepositoryService`
            used read-only (``get_revision``) to resolve each cited produced
            Deliverable Revision (AD-WS-40 / Requirement 49.4).
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
    assessment_reader: SuccessConditionAssessmentService
    completion_reader: CompletionService
    deliverable_reader: DeliverableRepositoryService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_outcome_review(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
        review_outcome: Literal[
            "Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"
        ],
        attribution_stance: Literal[
            "Asserted", "Partial", "Unattributed", "Contradicted"
        ],
        confidence: Literal["High", "Moderate", "Low"],
        review_rationale: str,
        attribution_evidence_reference: str,
        cited_assessment_ids: Sequence[str],
        cited_completion_ids: Sequence[str],
        cited_produced_deliverable_revision_ids: Sequence[str],
        reviewing_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateOutcomeReviewResult:
        """Create an immutable Outcome Review Record plus its ``Addresses``
        Relationship to the target Intended Outcome Revision and one ``Cites``
        Relationship per cited Success-Condition Assessment, Completion Record,
        and produced Deliverable Revision.

        Per Requirements 49.1 through 49.9, 52.9 (``issue_outcome_review``),
        53.2, 54.1, 54.4, 57.1, AD-WS-9 (indistinguishable denial), AD-WS-33,
        AD-WS-35, AD-WS-37, AD-WS-40, and AD-WS-41.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_intended_outcome_revision_id: Identity of the single target
                Intended Outcome Revision the Review addresses
                (Requirement 49.4).
            review_outcome: One of ``Achieved``, ``Partially_Achieved``,
                ``Not_Achieved``, or ``Inconclusive`` (Requirement 49.2).
            attribution_stance: One of ``Asserted``, ``Partial``,
                ``Unattributed``, or ``Contradicted`` (Requirement 49.2).
            confidence: One of ``High``, ``Moderate``, or ``Low``
                (Requirement 49.2).
            review_rationale: Rationale text (1..4000 chars).
            attribution_evidence_reference: Evidence-reference text (0..4000
                chars; at least 1 char when the stance is ``Asserted`` or
                ``Contradicted``, Requirement 49.4).
            cited_assessment_ids: At least one Success-Condition Assessment
                Record Identity; each must resolve and its ``Addresses`` target
                must equal ``target_intended_outcome_revision_id``
                (Requirement 49.4).
            cited_completion_ids: At least one Completion Record Identity; each
                must resolve (Requirement 49.4).
            cited_produced_deliverable_revision_ids: Zero or more produced
                Deliverable Revision Identities; each must resolve
                (Requirement 49.4).
            reviewing_party_id: Identity of the reviewing Party.
            authority_basis: The :class:`AuthorityBasisRef` recorded on the
                Record; its ``type`` must be in the AD-WS-10 set (AD-WS-41).
            applicable_scope: Scope the Review applies within; also passed as
                ``target.scope`` to authorization.
            engine: Required for the deny path's separate-transaction Denial
                Record write, the separate-transaction authorization
                evaluation, and the conflict pre-check view evaluation.
            correlation_id: Optional correlation identifier; a UUIDv7 is
                generated when omitted.
            evaluation_at: Optional explicit ``at`` for authorization; defaults
                to the recorded time.
            request_attributes: Optional raw request body screened against the
                intended-side prefix list and the Completion-as-Outcome intent
                markers (Requirements 53, 54).

        Returns:
            :class:`CreateOutcomeReviewResult` carrying the persisted
            identifiers, attributes, every Relationship Identity, the recorded
            time, and the correlation identifier.

        Raises:
            OutcomeReviewValidationError: A required attribute is missing, an
                enumeration is out of range, the rationale violates its length
                rule, the attribution-evidence rule fails, the authority basis
                type is not in the AD-WS-10 set, zero Assessments or zero
                Completion Records are cited, or the request body carried a
                prohibited attribute.
            OutcomeReviewTargetNotResolvableError: The target Intended Outcome
                Revision did not resolve or its ``outcome_kind`` is not
                ``'intended'``.
            OutcomeReviewConflictError: An Outcome Review Record already exists
                for the target Intended Outcome Revision.
            OutcomeReviewCitationError: A cited Assessment / Completion /
                produced Deliverable Revision did not resolve, or a cited
                Assessment addresses a different Intended Outcome Revision.
            OutcomeReviewAuthorizationError: The attempt was denied; the Denial
                Record was appended in a separate transaction.
            OutcomeReviewAuditFailureError: The attempt was denied and the
                Denial Record append failed on every retry.
        """
        # 1. Validate inputs (Requirements 49.4 / 53 / 54).
        cited_assessments, cited_completions, cited_deliverables = (
            self._validate_inputs(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                review_outcome=review_outcome,
                attribution_stance=attribution_stance,
                confidence=confidence,
                review_rationale=review_rationale,
                attribution_evidence_reference=attribution_evidence_reference,
                cited_assessment_ids=cited_assessment_ids,
                cited_completion_ids=cited_completion_ids,
                cited_produced_deliverable_revision_ids=(
                    cited_produced_deliverable_revision_ids
                ),
                reviewing_party_id=reviewing_party_id,
                authority_basis=authority_basis,
                applicable_scope=applicable_scope,
                request_attributes=request_attributes,
            )
        )

        # 2. Resolve the target Intended Outcome Revision via the additive
        # read-only Planning API (AD-WS-40). Reject when it does not resolve or
        # its outcome_kind is not 'intended' (Requirement 49.4). The check runs
        # before authorization so the deny path never reveals whether a target
        # exists for an unauthorized caller.
        target_intended_outcome_resource_id = self._resolve_intended_outcome(
            connection,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
        )

        # 3. Pre-check the UNIQUE(target_intended_outcome_revision_id)
        # constraint (Requirement 49.3). On conflict, surface the AD-WS-9
        # view-authority-gated existing Identity.
        self._reject_if_duplicate(
            connection,
            engine=engine,
            reviewing_party_id=reviewing_party_id,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            applicable_scope=applicable_scope,
            evaluation_at=evaluation_at,
        )

        # 4. Resolve every cited Success-Condition Assessment, Completion
        # Record, and produced Deliverable Revision (Requirement 49.4).
        self._resolve_cited_assessments(
            connection,
            cited_assessment_ids=cited_assessments,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
        )
        self._resolve_cited_completions(
            connection, cited_completion_ids=cited_completions
        )
        self._resolve_cited_deliverable_revisions(
            connection,
            cited_produced_deliverable_revision_ids=cited_deliverables,
        )

        # 5 + 6. Authorize and persist.
        return self._persist_outcome_review(
            connection,
            engine=engine,
            target_intended_outcome_resource_id=(
                target_intended_outcome_resource_id
            ),
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            review_outcome=review_outcome,
            attribution_stance=attribution_stance,
            confidence=confidence,
            review_rationale=review_rationale,
            attribution_evidence_reference=attribution_evidence_reference,
            cited_assessment_ids=cited_assessments,
            cited_completion_ids=cited_completions,
            cited_produced_deliverable_revision_ids=cited_deliverables,
            reviewing_party_id=reviewing_party_id,
            authority_basis=authority_basis,
            applicable_scope=applicable_scope,
            correlation_id=correlation_id,
            evaluation_at=evaluation_at,
        )

    # -- validation --------------------------------------------------------

    def _validate_inputs(
        self,
        *,
        target_intended_outcome_revision_id: str,
        review_outcome: str,
        attribution_stance: str,
        confidence: str,
        review_rationale: str,
        attribution_evidence_reference: str,
        cited_assessment_ids: Sequence[str],
        cited_completion_ids: Sequence[str],
        cited_produced_deliverable_revision_ids: Sequence[str],
        reviewing_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        request_attributes: Optional[Mapping[str, Any]],
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        """Validate the request inputs (Requirements 49.4 / 53 / 54).

        Returns the de-duplicated-but-order-preserving normalized tuples of
        cited Assessment, Completion, and produced Deliverable Revision
        Identities so the caller resolves and persists each exactly once.
        """
        self._screen_request_attributes(request_attributes)

        # Required string identifiers (Requirement 49.4).
        missing: list[str] = []
        if not _is_present_str(target_intended_outcome_revision_id):
            missing.append("target_intended_outcome_revision_id")
        if not _is_present_str(reviewing_party_id):
            missing.append("reviewing_party_id")
        if not _is_present_str(applicable_scope):
            missing.append("applicable_scope")
        if missing:
            raise OutcomeReviewValidationError(
                f"required Outcome Review attribute(s) {missing!r} are missing "
                "(Requirement 49.4).",
                failed_constraint=f"{missing[0]}_missing",
                invalid_attributes=tuple(missing),
            )

        # Review-outcome / attribution-stance / confidence enumerations
        # (Requirement 49.2/49.4).
        if review_outcome not in _REVIEW_OUTCOMES:
            raise OutcomeReviewValidationError(
                f"review outcome {review_outcome!r} is outside the enumerated "
                f"set {sorted(_REVIEW_OUTCOMES)} (Requirement 49.4).",
                failed_constraint="review_outcome_invalid",
                invalid_attributes=("review_outcome",),
            )
        if attribution_stance not in _ATTRIBUTION_STANCES:
            raise OutcomeReviewValidationError(
                f"attribution stance {attribution_stance!r} is outside the "
                f"enumerated set {sorted(_ATTRIBUTION_STANCES)} "
                "(Requirement 49.4).",
                failed_constraint="attribution_stance_invalid",
                invalid_attributes=("attribution_stance",),
            )
        if confidence not in _CONFIDENCE_LEVELS:
            raise OutcomeReviewValidationError(
                f"confidence indicator {confidence!r} is outside the "
                f"enumerated set {sorted(_CONFIDENCE_LEVELS)} "
                "(Requirement 49.4).",
                failed_constraint="confidence_invalid",
                invalid_attributes=("confidence",),
            )

        # Review rationale presence + length (Requirement 49.2/49.4).
        if not _is_present_str(review_rationale):
            raise OutcomeReviewValidationError(
                "review rationale is missing (Requirement 49.4).",
                failed_constraint="review_rationale_missing",
                invalid_attributes=("review_rationale",),
            )
        if len(review_rationale) > _RATIONALE_MAX_CHARS:
            raise OutcomeReviewValidationError(
                f"review rationale is {len(review_rationale)} characters; at "
                f"most {_RATIONALE_MAX_CHARS} are permitted "
                "(Requirement 49.2).",
                failed_constraint="review_rationale_too_long",
                invalid_attributes=("review_rationale",),
            )

        # Attribution-evidence reference: 0..4000 chars, and >= 1 char when the
        # stance is Asserted or Contradicted (Requirement 49.4). A None value
        # is treated as the empty string per the 0..4000 lower bound.
        evidence = attribution_evidence_reference or ""
        if not isinstance(evidence, str):
            raise OutcomeReviewValidationError(
                "attribution-evidence reference must be a string "
                "(Requirement 49.2).",
                failed_constraint="attribution_evidence_reference_invalid",
                invalid_attributes=("attribution_evidence_reference",),
            )
        if len(evidence) > _EVIDENCE_MAX_CHARS:
            raise OutcomeReviewValidationError(
                f"attribution-evidence reference is {len(evidence)} "
                f"characters; at most {_EVIDENCE_MAX_CHARS} are permitted "
                "(Requirement 49.2).",
                failed_constraint="attribution_evidence_reference_too_long",
                invalid_attributes=("attribution_evidence_reference",),
            )
        if attribution_stance in _STANCES_REQUIRING_EVIDENCE and len(
            evidence
        ) < 1:
            raise OutcomeReviewValidationError(
                f"attribution stance {attribution_stance!r} requires a "
                "non-empty attribution-evidence reference (Requirement 49.4).",
                failed_constraint=(
                    "attribution_evidence_reference_missing_for_stance"
                ),
                invalid_attributes=("attribution_evidence_reference",),
            )

        # Authority basis present, type in the AD-WS-10 set (AD-WS-41).
        if authority_basis is None:
            raise OutcomeReviewValidationError(
                "authority basis is missing (Requirement 49.4).",
                failed_constraint="authority_basis_missing",
                invalid_attributes=("authority_basis",),
            )
        basis_type = getattr(authority_basis, "type", None)
        if basis_type not in _AUTHORITY_BASIS_TYPES:
            raise OutcomeReviewValidationError(
                f"authority basis type {basis_type!r} is outside the AD-WS-10 "
                f"set {sorted(_AUTHORITY_BASIS_TYPES)} (Requirement 49.4).",
                failed_constraint="authority_basis_type_invalid",
                invalid_attributes=("authority_basis",),
            )

        # At least one cited Assessment and at least one cited Completion
        # Record (Requirement 49.4).
        normalized_assessments = _normalize_id_sequence(
            cited_assessment_ids,
            attribute_name="cited_assessment_ids",
            element_constraint="cited_assessment_id_invalid",
        )
        if len(normalized_assessments) < 1:
            raise OutcomeReviewValidationError(
                "at least one cited Success-Condition Assessment Record "
                "Identity is required (Requirement 49.4).",
                failed_constraint="cited_assessment_ids_empty",
                invalid_attributes=("cited_assessment_ids",),
            )
        normalized_completions = _normalize_id_sequence(
            cited_completion_ids,
            attribute_name="cited_completion_ids",
            element_constraint="cited_completion_id_invalid",
        )
        if len(normalized_completions) < 1:
            raise OutcomeReviewValidationError(
                "at least one cited Completion Record Identity is required "
                "(Requirement 49.4).",
                failed_constraint="cited_completion_ids_empty",
                invalid_attributes=("cited_completion_ids",),
            )
        # Produced Deliverable Revisions are optional (0..N).
        normalized_deliverables = _normalize_id_sequence(
            cited_produced_deliverable_revision_ids,
            attribute_name="cited_produced_deliverable_revision_ids",
            element_constraint=(
                "cited_produced_deliverable_revision_id_invalid"
            ),
        )

        return (
            normalized_assessments,
            normalized_completions,
            normalized_deliverables,
        )

    # -- resolution --------------------------------------------------------

    def _resolve_intended_outcome(
        self,
        connection: Connection,
        *,
        target_intended_outcome_revision_id: str,
    ) -> str:
        """Resolve the target Intended Outcome Revision and return its target
        Intended Outcome **Resource** Identity.

        Rejects when the Revision does not resolve or its ``outcome_kind`` is
        not the literal ``'intended'`` (Requirement 49.4, AD-WS-40).
        """
        revision_row = self.intended_outcome_reader.get_revision(
            connection, target_intended_outcome_revision_id
        )
        if revision_row is None:
            raise OutcomeReviewTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                failed_constraint="target_intended_outcome_not_resolvable",
            )
        if revision_row.outcome_kind != _OUTCOME_KIND_INTENDED:
            raise OutcomeReviewTargetNotResolvableError(
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                failed_constraint="target_outcome_kind_not_intended",
            )
        return revision_row.intended_outcome_id

    def _resolve_cited_assessments(
        self,
        connection: Connection,
        *,
        cited_assessment_ids: tuple[str, ...],
        target_intended_outcome_revision_id: str,
    ) -> None:
        """Resolve every cited Success-Condition Assessment Record and confirm
        each addresses the named target Intended Outcome Revision.

        Rejects when any cited Assessment does not resolve (Requirement 49.4)
        or its ``Addresses`` target Intended Outcome Revision Identity does not
        equal the named target (Requirement 49.4).
        """
        for assessment_id in cited_assessment_ids:
            assessment_row = self.assessment_reader.get_assessment(
                connection, assessment_id
            )
            if assessment_row is None:
                raise OutcomeReviewCitationError(
                    f"cited Success-Condition Assessment Record "
                    f"{assessment_id!r} does not resolve (Requirement 49.4).",
                    failed_constraint="cited_assessment_not_resolvable",
                    offending_id=assessment_id,
                    invalid_attributes=("cited_assessment_ids",),
                )
            if (
                assessment_row.target_intended_outcome_revision_id
                != target_intended_outcome_revision_id
            ):
                raise OutcomeReviewCitationError(
                    f"cited Success-Condition Assessment Record "
                    f"{assessment_id!r} addresses Intended Outcome Revision "
                    f"{assessment_row.target_intended_outcome_revision_id!r}, "
                    f"which does not equal the named target Intended Outcome "
                    f"Revision {target_intended_outcome_revision_id!r} "
                    "(Requirement 49.4).",
                    failed_constraint="cited_assessment_addresses_mismatch",
                    offending_id=assessment_id,
                    invalid_attributes=("cited_assessment_ids",),
                )

    def _resolve_cited_completions(
        self,
        connection: Connection,
        *,
        cited_completion_ids: tuple[str, ...],
    ) -> None:
        """Resolve every cited Slice 3 Completion Record via the additive
        read-only Execution API (AD-WS-40).

        Rejects when any cited Completion Record does not resolve
        (Requirement 49.4).
        """
        for completion_id in cited_completion_ids:
            completion_row = self.completion_reader.get_completion(
                connection, completion_id
            )
            if completion_row is None:
                raise OutcomeReviewCitationError(
                    f"cited Completion Record {completion_id!r} does not "
                    "resolve (Requirement 49.4).",
                    failed_constraint="cited_completion_not_resolvable",
                    offending_id=completion_id,
                    invalid_attributes=("cited_completion_ids",),
                )

    def _resolve_cited_deliverable_revisions(
        self,
        connection: Connection,
        *,
        cited_produced_deliverable_revision_ids: tuple[str, ...],
    ) -> dict[str, str]:
        """Resolve every cited produced Deliverable Revision via the existing
        Slice 3 read-only API (AD-WS-40) and return a mapping from Revision
        Identity to its parent Deliverable **Resource** Identity.

        The Resource Identity is recovered so the ``Cites`` Relationship row
        can carry ``target_id`` (the Resource) and ``target_revision_id`` (the
        Revision) per AD-WS-35. Rejects when any cited produced Deliverable
        Revision does not resolve (Requirement 49.4).
        """
        revision_to_resource: dict[str, str] = {}
        for deliverable_revision_id in cited_produced_deliverable_revision_ids:
            revision_row = self.deliverable_reader.get_revision(
                connection, deliverable_revision_id
            )
            if revision_row is None:
                raise OutcomeReviewCitationError(
                    f"cited produced Deliverable Revision "
                    f"{deliverable_revision_id!r} does not resolve "
                    "(Requirement 49.4).",
                    failed_constraint=(
                        "cited_produced_deliverable_revision_not_resolvable"
                    ),
                    offending_id=deliverable_revision_id,
                    invalid_attributes=(
                        "cited_produced_deliverable_revision_ids",
                    ),
                )
            revision_to_resource[deliverable_revision_id] = (
                revision_row.deliverable_id
            )
        return revision_to_resource

    # -- duplicate pre-check (Requirement 49.3 / AD-WS-9) ------------------

    def _reject_if_duplicate(
        self,
        connection: Connection,
        *,
        engine: Engine,
        reviewing_party_id: str,
        target_intended_outcome_revision_id: str,
        applicable_scope: str,
        evaluation_at: Optional[datetime],
    ) -> None:
        """Reject when an Outcome Review Record already exists for the target
        Intended Outcome Revision (Requirement 49.3).

        The schema-level ``UNIQUE(target_intended_outcome_revision_id)``
        constraint is the source of truth; this pre-check surfaces a structured
        :class:`OutcomeReviewConflictError` and applies the AD-WS-9
        view-authority gate on the existing Identity. The pre-check runs before
        the ``create.outcome_review`` evaluation so an unauthorized caller
        cannot distinguish a duplicate from a denial.
        """
        existing_id_row = connection.execute(
            text(
                "SELECT outcome_review_id FROM Outcome_Review_Records "
                "WHERE target_intended_outcome_revision_id = "
                ":target_intended_outcome_revision_id"
            ),
            {
                "target_intended_outcome_revision_id": (
                    target_intended_outcome_revision_id
                )
            },
        ).mappings().first()
        if existing_id_row is None:
            return

        existing_outcome_review_id = existing_id_row["outcome_review_id"]
        visible_existing_id = self._resolve_conflict_visibility(
            engine=engine,
            reviewing_party_id=reviewing_party_id,
            existing_outcome_review_id=existing_outcome_review_id,
            applicable_scope=applicable_scope,
            evaluation_at=evaluation_at,
        )
        raise OutcomeReviewConflictError(
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            existing_outcome_review_id=visible_existing_id,
        )

    def _resolve_conflict_visibility(
        self,
        *,
        engine: Engine,
        reviewing_party_id: str,
        existing_outcome_review_id: str,
        applicable_scope: str,
        evaluation_at: Optional[datetime],
    ) -> Optional[str]:
        """Return the existing Outcome Review Identity when the caller holds
        view authority on it; otherwise return ``None``.

        Implements the AD-WS-9 view-authority gate on the
        :class:`OutcomeReviewConflictError` response: the conflict body carries
        the existing ``outcome_review_id`` only if the requesting Party would
        be permitted to view it. Otherwise the body is byte-equivalent to a
        response that lacks the existing-Identity field, keeping the HTTP
        response indistinguishable from a non-existent endpoint.

        Evaluates ``view.outcome_review`` on a *separate* transaction (same
        pattern as the main authorization evaluation) so the read does not
        pollute the caller's transactional view and so the AD-WS-9 evaluation
        audit row survives independently of the conflict path.
        """
        at_when = (
            evaluation_at if evaluation_at is not None else self.clock.now()
        )
        with engine.begin() as eval_conn:
            view_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=reviewing_party_id,
                action=_ACTION_VIEW_OUTCOME_REVIEW,
                target=TargetRef(
                    kind=_KIND_OUTCOME_REVIEW_RECORD,
                    id=existing_outcome_review_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=at_when,
            )
        if view_outcome.is_permit:
            return existing_outcome_review_id
        return None

    # -- persistence -------------------------------------------------------

    def _persist_outcome_review(
        self,
        connection: Connection,
        *,
        engine: Engine,
        target_intended_outcome_resource_id: str,
        target_intended_outcome_revision_id: str,
        review_outcome: str,
        attribution_stance: str,
        confidence: str,
        review_rationale: str,
        attribution_evidence_reference: str,
        cited_assessment_ids: tuple[str, ...],
        cited_completion_ids: tuple[str, ...],
        cited_produced_deliverable_revision_ids: tuple[str, ...],
        reviewing_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        correlation_id: Optional[str],
        evaluation_at: Optional[datetime],
    ) -> CreateOutcomeReviewResult:
        """Authorize and persist an Outcome Review Record, its ``Addresses``
        Relationship, every ``Cites`` Relationship, and the consequential audit
        row.

        The authorization evaluation runs on a SEPARATE transaction; on deny it
        drives the AD-WS-9 separate-transaction Denial-Record pattern with the
        Requirement 50.6 retry. On permit it inserts the registry binding, the
        ``Outcome_Review_Records`` row, the ``Addresses`` Relationship, one
        ``Cites`` Relationship per cited Assessment / Completion / produced
        Deliverable Revision, and the consequential audit row — all inside the
        caller's transaction so a failure anywhere rolls every row back
        (Requirements 49.1/49.2/49.6, 57.1).
        """
        correlation = correlation_id or _new_correlation_id()
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # Re-resolve the cited produced Deliverable Revisions to recover their
        # parent Resource Identities for the Cites Relationship rows. The
        # resolution already happened in create_outcome_review for rejection;
        # re-reading here keeps the Resource mapping local to persistence
        # without threading it through the public surface.
        deliverable_revision_to_resource = (
            self._resolve_cited_deliverable_revisions(
                connection,
                cited_produced_deliverable_revision_ids=(
                    cited_produced_deliverable_revision_ids
                ),
            )
        )

        # Authorization evaluation on a SEPARATE transaction. The TargetRef is
        # the target Intended Outcome Revision so the wired role assignment
        # must cover the same scope to permit the action.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=reviewing_party_id,
                action=_ACTION_CREATE_OUTCOME_REVIEW,
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
            self._persist_outcome_review_denial(
                engine=engine,
                actor_party_id=reviewing_party_id,
                target_intended_outcome_revision_id=(
                    target_intended_outcome_revision_id
                ),
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise OutcomeReviewAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # Mint identifiers (AD-WS-37). The Outcome Review Record is a Governance
        # Decision Immutable Record; each Relationship row takes a fresh
        # Relationship Identity.
        outcome_review_id = str(
            self.identity_service.new_immutable_record_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        cites_assessment_relationship_ids = tuple(
            str(self.identity_service.new_relationship_id())
            for _ in cited_assessment_ids
        )
        cites_completion_relationship_ids = tuple(
            str(self.identity_service.new_relationship_id())
            for _ in cited_completion_ids
        )
        cites_deliverable_relationship_ids = tuple(
            str(self.identity_service.new_relationship_id())
            for _ in cited_produced_deliverable_revision_ids
        )

        authority_basis_type = authority_basis.type
        authority_basis_id = str(authority_basis.id)

        content_digest = _sha256_hex(
            json.dumps(
                {
                    "outcome_review_id": outcome_review_id,
                    "target_intended_outcome_resource_id": (
                        target_intended_outcome_resource_id
                    ),
                    "target_intended_outcome_revision_id": (
                        target_intended_outcome_revision_id
                    ),
                    "review_outcome": review_outcome,
                    "attribution_stance": attribution_stance,
                    "confidence": confidence,
                    "review_rationale": review_rationale,
                    "attribution_evidence_reference": (
                        attribution_evidence_reference
                    ),
                    "cited_assessment_ids": list(cited_assessment_ids),
                    "cited_completion_ids": list(cited_completion_ids),
                    "cited_produced_deliverable_revision_ids": list(
                        cited_produced_deliverable_revision_ids
                    ),
                    "reviewing_party_id": reviewing_party_id,
                    "authority_basis_type": authority_basis_type,
                    "authority_basis_id": authority_basis_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # Register the Outcome Review Record Identity in ``Identifier_Registry``
        # carrying the AD-WS-37 ``resource_kind`` tag.
        _record_outcome_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_OUTCOME_REVIEW_RECORD,
            outcome_review_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=reviewing_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_OUTCOME_REVIEW,
            recorded_time=recorded_time,
        )

        # Insert the immutable Outcome Review Record (Requirement 49.2). The
        # schema CHECKs enforce the three enumerations, the 1..4000 rationale
        # bound, the 0..4000 evidence bound, and the Asserted/Contradicted
        # non-empty-evidence rule a second time.
        connection.execute(
            text(
                """
                INSERT INTO Outcome_Review_Records (
                    outcome_review_id,
                    target_intended_outcome_resource_id,
                    target_intended_outcome_revision_id,
                    review_outcome,
                    attribution_stance,
                    confidence,
                    review_rationale,
                    attribution_evidence_reference,
                    reviewing_party_id,
                    authority_basis_type,
                    authority_basis_id,
                    applicable_scope,
                    recorded_at
                ) VALUES (
                    :outcome_review_id,
                    :target_intended_outcome_resource_id,
                    :target_intended_outcome_revision_id,
                    :review_outcome,
                    :attribution_stance,
                    :confidence,
                    :review_rationale,
                    :attribution_evidence_reference,
                    :reviewing_party_id,
                    :authority_basis_type,
                    :authority_basis_id,
                    :applicable_scope,
                    :recorded_at
                )
                """
            ),
            {
                "outcome_review_id": outcome_review_id,
                "target_intended_outcome_resource_id": (
                    target_intended_outcome_resource_id
                ),
                "target_intended_outcome_revision_id": (
                    target_intended_outcome_revision_id
                ),
                "review_outcome": review_outcome,
                "attribution_stance": attribution_stance,
                "confidence": confidence,
                "review_rationale": review_rationale,
                "attribution_evidence_reference": (
                    attribution_evidence_reference
                ),
                "reviewing_party_id": reviewing_party_id,
                "authority_basis_type": authority_basis_type,
                "authority_basis_id": authority_basis_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # Single ``Addresses`` Relationship to the target Intended Outcome
        # Revision (AD-WS-35, semantic_role IS NULL). The source is the Outcome
        # Review Record, an Immutable Record with no Revision, so
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
                "source_kind": _KIND_OUTCOME_REVIEW_RECORD,
                "source_id": outcome_review_id,
                "target_kind": _KIND_INTENDED_OUTCOME_REVISION,
                "target_id": target_intended_outcome_resource_id,
                "target_revision_id": target_intended_outcome_revision_id,
                "authoring_party_id": reviewing_party_id,
                "recorded_at": recorded_at,
            },
        )

        # One ``Cites`` Relationship per cited Success-Condition Assessment
        # (AD-WS-35, semantic_role = 'review_assessment'). The Assessment is an
        # Immutable Record with no Revision, so target_revision_id is NULL.
        for relationship_id, assessment_id in zip(
            cites_assessment_relationship_ids, cited_assessment_ids
        ):
            connection.execute(
                _CITES_RELATIONSHIP_INSERT,
                {
                    "relationship_id": relationship_id,
                    "relationship_type": _RELATIONSHIP_TYPE_CITES,
                    "source_kind": _KIND_OUTCOME_REVIEW_RECORD,
                    "source_id": outcome_review_id,
                    "target_kind": _KIND_ASSESSMENT_RECORD,
                    "target_id": assessment_id,
                    "target_revision_id": None,
                    "authoring_party_id": reviewing_party_id,
                    "recorded_at": recorded_at,
                    "semantic_role": _SEMANTIC_ROLE_REVIEW_ASSESSMENT,
                },
            )

        # One ``Cites`` Relationship per cited Completion Record (AD-WS-35,
        # semantic_role = 'review_completion'). The Completion Record is an
        # Immutable Record with no Revision, so target_revision_id is NULL.
        for relationship_id, completion_id in zip(
            cites_completion_relationship_ids, cited_completion_ids
        ):
            connection.execute(
                _CITES_RELATIONSHIP_INSERT,
                {
                    "relationship_id": relationship_id,
                    "relationship_type": _RELATIONSHIP_TYPE_CITES,
                    "source_kind": _KIND_OUTCOME_REVIEW_RECORD,
                    "source_id": outcome_review_id,
                    "target_kind": _KIND_COMPLETION_RECORD,
                    "target_id": completion_id,
                    "target_revision_id": None,
                    "authoring_party_id": reviewing_party_id,
                    "recorded_at": recorded_at,
                    "semantic_role": _SEMANTIC_ROLE_REVIEW_COMPLETION,
                },
            )

        # One ``Cites`` Relationship per cited produced Deliverable Revision
        # (AD-WS-35, semantic_role = 'review_deliverable'). The target is a
        # Revision-bearing entity, so target_id is the Deliverable Resource
        # Identity and target_revision_id the Revision Identity.
        for relationship_id, deliverable_revision_id in zip(
            cites_deliverable_relationship_ids,
            cited_produced_deliverable_revision_ids,
        ):
            connection.execute(
                _CITES_RELATIONSHIP_INSERT,
                {
                    "relationship_id": relationship_id,
                    "relationship_type": _RELATIONSHIP_TYPE_CITES,
                    "source_kind": _KIND_OUTCOME_REVIEW_RECORD,
                    "source_id": outcome_review_id,
                    "target_kind": _KIND_DELIVERABLE_REVISION,
                    "target_id": deliverable_revision_to_resource[
                        deliverable_revision_id
                    ],
                    "target_revision_id": deliverable_revision_id,
                    "authoring_party_id": reviewing_party_id,
                    "recorded_at": recorded_at,
                    "semantic_role": _SEMANTIC_ROLE_REVIEW_DELIVERABLE,
                },
            )

        # Consequential audit row (Requirement 49.6 / 57.1 / AD-WS-5).
        # Participates in the caller's transaction. The Outcome Review Record is
        # an Immutable Record with no Revision, so target_revision_id is None.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=reviewing_party_id,
            action_type=_ACTION_CREATE_OUTCOME_REVIEW,
            target_id=outcome_review_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateOutcomeReviewResult(
            outcome_review_id=outcome_review_id,
            target_intended_outcome_revision_id=(
                target_intended_outcome_revision_id
            ),
            review_outcome=review_outcome,
            attribution_stance=attribution_stance,
            confidence=confidence,
            review_rationale=review_rationale,
            attribution_evidence_reference=attribution_evidence_reference,
            cited_assessment_ids=cited_assessment_ids,
            cited_completion_ids=cited_completion_ids,
            cited_produced_deliverable_revision_ids=(
                cited_produced_deliverable_revision_ids
            ),
            reviewing_party_id=reviewing_party_id,
            authority_basis=authority_basis,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            cites_assessment_relationship_ids=cites_assessment_relationship_ids,
            cites_completion_relationship_ids=cites_completion_relationship_ids,
            cites_deliverable_relationship_ids=(
                cites_deliverable_relationship_ids
            ),
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- denial side-channel ----------------------------------------------

    def _persist_outcome_review_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_intended_outcome_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Outcome Review attempt.

        Implements the Requirement 50.6 retry contract verbatim (mirroring the
        Slice 1/2/3/4 pattern and the sibling Outcome_Service services): each
        attempt opens a *new* :meth:`Engine.begin` transaction, tries
        :meth:`AuditLog.append_denial`, and either returns on success or pauses
        by the next entry in :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the
        next try. If every attempt fails,
        :class:`OutcomeReviewAuditFailureError` is raised.

        The separate transaction is essential: the caller's originating
        transaction is about to be rolled back when the create method raises
        :class:`OutcomeReviewAuthorizationError`. The Denial Record must
        therefore live outside that scope to survive (AD-WS-9 /
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
                        attempted_action=_ACTION_CREATE_OUTCOME_REVIEW,
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

        raise OutcomeReviewAuditFailureError(
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
        (Requirement 53) and Completion-as-Outcome intent markers
        (Requirements 54.1 / 54.4) when the route layer forwarded it."""
        if request_attributes is None:
            return
        try:
            _reject_prohibited_attributes(
                request_attributes, OUTCOME_PROHIBITED_PREFIXES
            )
        except OutcomeValidationError as exc:
            raise OutcomeReviewValidationError(
                str(exc),
                failed_constraint="prohibited_attribute",
                invalid_attributes=exc.prohibited_keys,
                prohibited_keys=exc.prohibited_keys,
            ) from exc


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


# Prepared ``Cites`` Relationship INSERT reused for every cited Assessment,
# Completion Record, and produced Deliverable Revision so the three citation
# kinds write byte-identical rows differing only in target_kind, target_id,
# target_revision_id, and semantic_role.
_CITES_RELATIONSHIP_INSERT: Final = text(
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
)


def _is_present_str(value: Any) -> bool:
    """Return whether a required string attribute was supplied and non-empty."""
    return isinstance(value, str) and len(value) > 0


def _normalize_id_sequence(
    values: Any,
    *,
    attribute_name: str,
    element_constraint: str,
) -> tuple[str, ...]:
    """Validate and normalize a cited-identifier sequence.

    Confirms ``values`` is a non-string sequence and every element is a
    non-empty string, preserving order while dropping duplicates so each cited
    identifier is resolved and a single ``Cites`` Relationship written exactly
    once. Returns the normalized tuple.

    Raises:
        OutcomeReviewValidationError: ``values`` is not a sequence, is a bare
            string, or contains a non-string / empty element.
    """
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise OutcomeReviewValidationError(
            f"{attribute_name} must be a sequence of identifier strings "
            "(Requirement 49.4).",
            failed_constraint=f"{attribute_name}_invalid",
            invalid_attributes=(attribute_name,),
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for entry in values:
        if not _is_present_str(entry):
            raise OutcomeReviewValidationError(
                f"every {attribute_name} entry must be a non-empty identifier "
                f"string; received {entry!r} (Requirement 49.4).",
                failed_constraint=element_constraint,
                invalid_attributes=(attribute_name,),
            )
        if entry in seen:
            continue
        seen.add(entry)
        normalized.append(entry)
    return tuple(normalized)


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit row, the
    (separate-transaction) Denial Record, and the consequential audit row
    produced for the same logical Outcome Review write. They are not registered
    with :class:`IdentityService` because they do not name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Outcome Review Record
    Identity in ``Identifier_Registry``, mirroring the Slice 1/2/3/4
    convention.
    """
    return hashlib.sha256(content).hexdigest()
