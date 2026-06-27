"""Planning_Service.PlanReviews — immutable Plan Review Revisions targeting
a Draft Plan Revision.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.PlanReviews" — public dataclass surface, authority
  string (``create.plan_review`` → ``review`` per AD-WS-15), AD-WS-9
  separate-transaction Denial Record on deny, the
  ``relationship_type='Relates To'`` / ``semantic_role='review'``
  Relationship contract (AD-WS-17), and the Requirement 8.7
  byte-equivalence-of-target-lifecycle contract (recording a Plan
  Review does *not* mutate the target Plan Revision row).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with the appropriate ``kind``
  / ``resource_kind`` tag); Authorization (the action string
  ``create.plan_review`` maps to the ``review`` authority per AD-WS-15
  and Requirement 11.4; the deny path uses the Slice 1
  separate-transaction Denial-Record pattern reproduced from
  :class:`walking_slice.planning.plan_revisions.PlanRevisionService`).
- AD-WS-15 — additive ``review`` mapping for ``create.plan_review``.
- AD-WS-17 — additive ``Relationships.semantic_role`` column whose
  Slice 2 use is exclusively the ``'review'`` discriminator on the
  single ``Relates To`` row inserted by this module.
- AD-WS-19 — additive ``Identifier_Registry.resource_kind`` column the
  helper :func:`walking_slice.planning._helpers._record_planning_resource`
  populates with ``'plan_review'`` (Resource) and
  ``'plan_review_revision'`` (Revision).
- AD-WS-22 — the ``authority_basis.type`` value is drawn from the
  Slice 1 enumeration ``{role-grant-id, scope-id,
  delegation-chain-id}`` (Requirement 8.2 defers to AD-WS-10).

Plan Reviews follow the standard Slice 2 Resource / Revision split:
``Plan_Reviews`` carries the Resource header (Identity + created_at)
and ``Plan_Review_Revisions`` carries the immutable per-Revision
content (outcome, rationale, target Plan Revision Identity, reviewing
Party Identity, authority basis, applicable scope, recorded time).
Recording a Plan Review writes exactly one Resource row, one Revision
row, one ``Relates To`` Relationship row (AD-WS-17, distinguished by
``semantic_role='review'``), two ``Identifier_Registry`` rows (one
for the Resource, one for the Revision — each carrying its Slice 2
``resource_kind`` tag), and one consequential ``Audit_Records`` row.
The target Plan Revision row is read but never written: Requirement
8.7 forbids changing its ``lifecycle_state`` as a consequence of the
review.

Task scope (task 10.1)
======================

This module implements :meth:`PlanReviewService.create_plan_review`:

1. Validate request inputs per Requirement 8.2 / 8.6:
   ``outcome ∈ {Endorse, Changes_Requested, Reject}``,
   ``rationale`` 1..10000 characters, ``authority_basis.type ∈
   {role-grant-id, scope-id, delegation-chain-id}``, plus the
   required strings (``target_plan_revision_id``,
   ``reviewing_party_id``, ``applicable_scope``).
2. Defensively reject any prohibited execution / observed-outcome /
   produced-deliverable attribute via
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   (Property 22) when the route layer forwards the raw request body.
3. Resolve the target Plan Revision through a single SELECT against
   ``Plan_Revisions``; reject when unresolvable (Requirement 8.6) or
   when ``lifecycle_state != 'draft'`` (Requirement 8.6).
4. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (the Slice 1 single-writer accommodation); on a deny outcome,
   persist a Denial Record in another separate transaction with the
   AD-WS-9 three-attempt exponential-backoff retry pattern, and raise
   :class:`PlanReviewAuthorizationError` carrying the AD-WS-9 denial
   response fields (``reason_code``, ``correlation_id``).
5. On a permit outcome, mint the Plan Review Resource Identity, the
   Plan Review Revision Identity, and the ``Relates To`` Relationship
   Identity; register both Resource and Revision identifiers in
   ``Identifier_Registry`` with their additive ``resource_kind`` tags
   per AD-WS-19; INSERT the ``Plan_Reviews`` Resource header row, the
   ``Plan_Review_Revisions`` row carrying every Requirement 8.2
   attribute (including the destructured ``authority_basis_type`` and
   ``authority_basis_id`` columns), and the single ``Relationships``
   row with ``relationship_type='Relates To'`` /
   ``semantic_role='review'`` / ``source_kind='plan_review_revision'``
   / ``target_kind='plan_revision'`` (AD-WS-17).
6. Append the consequential ``Audit_Records`` row inside the same
   transaction (Requirement 8.4 / AD-WS-5).
7. Do *not* execute any UPDATE against the target Plan Revision row
   (Requirement 8.7) — the AD-WS-19 lifecycle trigger would also
   reject such an attempt, but the application-layer code never
   issues one.

Requirements satisfied
======================

    8.1 — authorized Plan Review creation produces exactly one Plan
          Review Resource and one immutable first Plan Review
          Revision within nominal latency.
    8.2 — every Plan Review Revision records target Plan Revision
          Identity, outcome ∈ {Endorse, Changes_Requested, Reject},
          rationale of 1..10000 chars, reviewing Party Identity,
          authority basis with type ∈ AD-WS-10 set, applicable scope,
          and recorded time in UTC with millisecond precision.
    8.3 — every Plan Review Revision is linked to its target Plan
          Revision through exactly one ``Relates To`` Relationship
          with ``semantic_role='review'`` (AD-WS-17).
    8.4 — the Audit_Log appends an immutable consequential record in
          the same transaction as the Plan Review Revision creation.
    8.5 — unauthorized requests are denied via
          :class:`AuthorizationService` (requires ``review``
          authority, AD-WS-15 / Requirement 11.4); the
          Planning_Service declines to create any Plan Review
          Resource or Revision and the Audit_Log appends a Denial
          Record conforming to AD-WS-9.
    8.6 — missing / unresolvable / non-draft target Plan Revision,
          out-of-set review outcome, missing rationale, and missing
          applicable scope are rejected with no Plan Review Resource
          or Revision created and a structured error identifying the
          invalid attribute (the first offending attribute in source
          order).
    8.7 — the target Plan Revision row's ``lifecycle_state`` is
          unchanged by recording a Plan Review; no UPDATE is issued
          against ``Plan_Revisions``.
    11.4 — the Authorization_Service requires the ``review``
          authority on the evaluated Role Assignment and does not
          permit the action on the basis of ``approve`` authority
          alone (enforced by AD-WS-15's mapping in
          :func:`walking_slice.authorization._required_authority`,
          which this module relies on by passing the action string
          ``"create.plan_review"`` to :meth:`evaluate`).
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid_utils
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final, Literal, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


__all__ = [
    "CreatePlanReviewResult",
    "PlanReviewAuditFailureError",
    "PlanReviewAuthorizationError",
    "PlanReviewService",
    "PlanReviewTargetNotDraftError",
    "PlanReviewTargetNotResolvableError",
    "PlanReviewValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship kind strings, ``semantic_role`` value,
# and enumerations are pulled out as module-level ``Final`` so the
# names downstream property tests look for in ``Audit_Records.action_type``
# and ``Relationships`` are textually stable and the strings stay
# aligned with the :mod:`walking_slice.planning._persistence` schema,
# the AD-WS-15 authority mapping in :mod:`walking_slice.authorization`,
# and the AD-WS-17 ``semantic_role`` column whose Slice 2 use is
# exclusively the ``'review'`` discriminator written here.
# ---------------------------------------------------------------------------


# ``create.plan_review`` maps to the ``review`` authority per AD-WS-15
# / Requirement 11.4. The string is also the ``action_type`` recorded
# on the consequential audit row (Requirement 8.4) and on the
# separate-transaction Denial Record appended by
# :meth:`PlanReviewService._persist_plan_review_denial` so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_PLAN_REVIEW: Final[str] = "create.plan_review"

# Relationship Type, source / target ``kind`` strings, and the
# additive AD-WS-17 ``semantic_role`` value written to the single
# ``Relates To`` Relationship row inserted alongside the Plan Review
# Revision. Constants ensure the strings cannot drift between this
# module, the Provenance Navigator backlink algorithm that consumes
# ``Relationships`` rows verbatim, and any future planning provenance
# walk that distinguishes review edges from other ``Relates To``
# rows on the basis of the ``semantic_role`` column (AD-WS-17).
_RELATIONSHIP_TYPE_RELATES_TO: Final[str] = "Relates To"
_SEMANTIC_ROLE_REVIEW: Final[str] = "review"
_KIND_PLAN_REVIEW_REVISION: Final[str] = "plan_review_revision"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"

# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tags (Slice 2 additive
# enumeration per AD-WS-19). Plan Reviews follow the standard
# Resource / Revision split, so two registry rows are inserted: one
# Resource-level row and one Revision-level row, each carrying its
# Slice 2 ``resource_kind`` tag for the row-level disjointness
# discriminator that Requirement 4.5 (generalized to every Slice 2
# Resource kind) relies on.
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_PLAN_REVIEW: Final[str] = "plan_review"
_RESOURCE_KIND_PLAN_REVIEW_REVISION: Final[str] = "plan_review_revision"

# Outcome enumeration per Requirement 8.2 / 8.6. The same set is
# enforced by the ``Plan_Review_Revisions.outcome`` CHECK constraint
# in :mod:`walking_slice.planning._persistence`; centralizing the
# tuple here surfaces a precise, structured ``failed_constraint`` on
# :class:`PlanReviewValidationError` before the SQL layer is reached.
_OUTCOME_ENDORSE: Final[str] = "Endorse"
_OUTCOME_CHANGES_REQUESTED: Final[str] = "Changes_Requested"
_OUTCOME_REJECT: Final[str] = "Reject"
_VALID_OUTCOMES: Final[frozenset[str]] = frozenset(
    {_OUTCOME_ENDORSE, _OUTCOME_CHANGES_REQUESTED, _OUTCOME_REJECT}
)

# Authority basis type enumeration per AD-WS-10 / Requirement 8.2 /
# AD-WS-22. The same set is enforced by the
# ``Plan_Review_Revisions.authority_basis_type`` CHECK constraint;
# centralizing the tuple here lets the validator reject malformed
# requests structurally before they touch SQL. The
# :class:`AuthorityBasisRef` Pydantic ``Literal`` already constrains
# Python-typed callers to the same enumeration, but the application
# layer may receive dict-shaped inputs from the HTTP layer that have
# not yet been bound to :class:`AuthorityBasisRef`; this check is the
# defense-in-depth that survives even those cases.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Lifecycle state value the target Plan Revision must hold for a
# Plan Review to be accepted. AD-WS-18 declares Plan Revision
# lifecycle states are exactly ``{draft, approved}``; Requirement
# 8.6 requires the rejection path when the lifecycle state is not
# ``'draft'``. Pulled out as a constant so the literal cannot drift
# from the schema CHECK in :mod:`walking_slice.planning._persistence`.
_LIFECYCLE_DRAFT: Final[str] = "draft"

# Validation limits per Requirement 8.2 (Plan Review rationale).
# The schema CHECK constraint on
# ``Plan_Review_Revisions.rationale`` enforces the same values;
# centralizing them here surfaces precise, structured constraint
# names through :class:`PlanReviewValidationError`.
_RATIONALE_MIN_CHARS: Final[int] = 1
_RATIONALE_MAX_CHARS: Final[int] = 10_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 8.5 / AD-WS-9 / Slice 1
# Requirement 7.6). Three retries after the initial attempt for a
# total of four attempts. The sequence is byte-equivalent to the one
# in sibling Planning_Service modules so every Planning_Service
# module presents identical denial-side timing (which Property 18 —
# Indistinguishable denial — relies on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)



# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class PlanReviewValidationError(ValueError):
    """Raised when a Plan Review submission fails Requirement 8.2 / 8.6 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_plan_revision_id_missing"``,
            ``"reviewing_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"outcome_missing"`` (empty / non-string outcome),
            ``"outcome_out_of_set"`` (outcome not in the AD-WS-15
                / Requirement 8.2 enumeration),
            ``"rationale_missing"`` (empty / non-string rationale),
            ``"rationale_too_long"``,
            ``"authority_basis_missing"`` (the supplied authority
                basis is ``None`` / not a Mapping / not an
                :class:`AuthorityBasisRef`),
            ``"authority_basis_type_missing"`` (the type field is
                absent / empty),
            ``"authority_basis_type_out_of_set"`` (the type value is
                not in the AD-WS-10 set),
            ``"authority_basis_id_missing"`` (the id field is absent
                / empty),
            ``"prohibited_attribute"`` (the request body carried at
                least one execution / observed-outcome /
                produced-deliverable attribute — see
                :attr:`prohibited_keys`).
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


class PlanReviewTargetNotResolvableError(LookupError):
    """Raised when the target Plan Revision Identity does not resolve.

    Requirement 8.6 requires the target Plan Revision Identity to
    resolve to an existing Plan Revision. The check runs before
    authorization evaluation so the deny path never reveals whether a
    Plan Revision exists for an unauthorized caller (AD-WS-9 /
    Requirement 10.7 indistinguishable-denial requirement).

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the caller
            supplied.
        failed_constraint: ``"target_plan_revision_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        failed_constraint: str = "target_plan_revision_not_resolvable",
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} did not "
            f"resolve to an existing Plan Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.failed_constraint = failed_constraint


class PlanReviewTargetNotDraftError(LookupError):
    """Raised when the target Plan Revision is not in ``'draft'``.

    Requirement 8.6 rejects Plan Review submissions whose target Plan
    Revision's ``lifecycle_state`` is not ``'draft'`` (i.e. the only
    other valid value: ``'approved'``). Approved Plan Revisions are
    byte-equivalent forever per Requirement 9.4 — accepting a Plan
    Review against one would have no observable effect, but would
    leak the existence of the approved revision through the denial
    path. The check runs before authorization evaluation for the same
    don't-leak-existence reason as
    :class:`PlanReviewTargetNotResolvableError`.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        lifecycle_state: The actual lifecycle state observed on the
            target row (always something other than ``'draft'``;
            carried verbatim for debugging).
        failed_constraint: ``"target_plan_revision_not_draft"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        lifecycle_state: str,
        failed_constraint: str = "target_plan_revision_not_draft",
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} has "
            f"lifecycle_state={lifecycle_state!r}; Requirement 8.6 rejects "
            "Plan Reviews against any target whose lifecycle state is not "
            f"{_LIFECYCLE_DRAFT!r} "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.lifecycle_state = lifecycle_state
        self.failed_constraint = failed_constraint


class PlanReviewAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Plan Review attempt.

    Mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 8.5 / 10.x). The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 10 forbids leaking authorized Party identities, target
    existence, or role assignment details beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Plan Review creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class PlanReviewAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 8.5 / Slice 1 Requirement 7.6).

    Mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuditFailureError`.
    On total audit-append failure the exception is raised *in place
    of* :class:`PlanReviewAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's
    transaction still rolls back so no ``Plan_Reviews``,
    ``Plan_Review_Revisions``, ``Relationships``, or consequential
    audit row is persisted.

    Attributes:
        reason_code: The denial reason code from the evaluation that
            triggered this denial path.
        correlation_id: The correlation identifier shared with the
            (rolled-back) evaluation row and with the (failed) denial
            record attempts.
        attempts: The total number of attempts made before giving
            up.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Plan Review failed after "
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
class CreatePlanReviewResult:
    """Result of :meth:`PlanReviewService.create_plan_review`.

    Returned so callers (the HTTP layer in task 15.1, tests, and any
    downstream code that correlates a Plan Review with its target
    Plan Revision) can read the persisted identifiers, the
    ``Relates To`` Relationship Identity, the recorded time, and the
    correlation identifier in one round-trip.

    Attributes:
        plan_review_id: The Plan Review Resource Identity (UUIDv7).
        plan_review_revision_id: The first Plan Review Revision
            Identity (UUIDv7).
        target_plan_revision_id: The Plan Revision Identity the
            review targets; copied byte-equivalent from the request
            input.
        outcome: One of ``"Endorse"``, ``"Changes_Requested"``,
            ``"Reject"`` (Requirement 8.2).
        rationale: The persisted rationale text (1..10000 chars).
        reviewing_party_id: Identity of the reviewing Party.
        authority_basis: The :class:`AuthorityBasisRef` carried into
            the request, returned verbatim so callers can correlate
            the basis type and identifier with the persisted columns.
        applicable_scope: Scope identifier the Plan Review applies
            within.
        relates_to_relationship_id: Identity of the single
            ``Relates To`` ``Relationships`` row inserted alongside
            the Plan Review Revision (AD-WS-17, ``semantic_role =
            'review'``).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Plan_Reviews``,
            ``Plan_Review_Revisions``, ``Relationships``, and
            consequential audit rows.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    plan_review_id: str
    plan_review_revision_id: str
    target_plan_revision_id: str
    outcome: str
    rationale: str
    reviewing_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    relates_to_relationship_id: str
    recorded_at: str
    correlation_id: str



# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanReviewService:
    """Persist immutable Plan Review Revisions and the single ``Relates To``
    Relationship that binds each to its target Plan Revision.

    Like its sibling Planning_Service classes, this service is
    connection-scoped at call time: :meth:`create_plan_review`
    accepts the caller's :class:`sqlalchemy.engine.Connection` and
    writes inside the caller's transaction (AD-WS-5). The service
    instance therefore holds only the cross-request collaborators and
    can be shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.PlanReviews" declares it
    ``@dataclass(frozen=True)`` — Slice 2 service instances are
    immutable container objects that bundle the Slice 1 collaborators
    for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Plan_Reviews``, ``Plan_Review_Revisions``,
            ``Relationships``, and ``Audit_Records`` rows. The clock
            is consulted once per write so every artifact of the
            transaction shares one timestamp.
        identity_service: Generates the Plan Review Resource Identity,
            the Plan Review Revision Identity, and the ``Relates To``
            Relationship Identity, plus persists their
            ``Identifier_Registry`` bindings (both bindings carry
            their Slice 2 ``resource_kind`` tags per AD-WS-19).
        audit_log: Appends the consequential audit row (Requirement
            8.4) inside the caller's transaction.
        authorization_service: Evaluates ``create.plan_review``
            authority per AD-WS-15 / Requirement 11.4; the deny path
            is the Slice 1 separate-transaction Denial-Record
            pattern.
        denial_audit_sleep: Sleep function used to pause between
            retries of the Denial Record append. Defaults to
            :func:`time.sleep`; tests that need deterministic timing
            inject a recording stub so the retry sequence is
            observable without spending real time. The function is
            called with a single ``float`` argument naming the
            seconds to sleep, drawn from
            :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS`.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: AuthorizationService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_plan_review(
        self,
        connection: Connection,
        *,
        target_plan_revision_id: str,
        outcome: Literal["Endorse", "Changes_Requested", "Reject"],
        rationale: str,
        reviewing_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreatePlanReviewResult:
        """Create a Plan Review Resource, its first Revision, and the
        single ``Relates To`` Relationship binding the Revision to its
        target Plan Revision (AD-WS-17, ``semantic_role='review'``).

        Per Requirements 8.1 through 8.7, 11.4 (review authority is
        the required authority), AD-WS-9 (indistinguishable denial),
        AD-WS-15 (``create.plan_review`` → ``review``), and AD-WS-17
        (``semantic_role='review'`` discriminator on the single
        ``Relates To`` Relationship row):

        1. Optionally screen the original request body against the
           prohibited-attribute prefixes (Property 22 / Requirements
           12.1, 12.2, 13.1, 13.2, 13.5).
        2. Input validation (Requirement 8.2 / 8.6) — every
           enumeration check, length check, and required-attribute
           check runs before any database read so a malformed request
           never touches identity service, the ``Plan_Revisions``
           lookup, or the authorization service.
        3. Resolve the target Plan Revision via a single SELECT
           against ``Plan_Revisions``, reading both
           ``plan_revision_id`` and ``lifecycle_state``. When the
           identifier does not resolve, raise
           :class:`PlanReviewTargetNotResolvableError`. When the
           lifecycle state is not ``'draft'``, raise
           :class:`PlanReviewTargetNotDraftError`. Both checks run
           before authorization evaluation so the deny path never
           reveals whether a Plan Revision exists / is in draft for
           an unauthorized caller.
        4. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the AD-WS-9 / Slice 1 Requirement 7.6
           retry sequence (0.01s / 0.02s / 0.04s exponential backoff,
           three retries after the initial attempt). On total audit
           failure raise :class:`PlanReviewAuditFailureError` in
           place of :class:`PlanReviewAuthorizationError`.
        5. On ``permit``, mint the Plan Review Resource Identity, the
           Plan Review Revision Identity, and the ``Relates To``
           Relationship Identity; register the Resource and Revision
           identifiers in ``Identifier_Registry`` (kinds
           ``'resource'`` / ``'revision'``, carrying the Slice 2
           ``resource_kind`` tags ``'plan_review'`` /
           ``'plan_review_revision'`` per AD-WS-19) via
           :func:`walking_slice.planning._helpers._record_planning_resource`.
        6. INSERT the ``Plan_Reviews`` Resource header row.
        7. INSERT the ``Plan_Review_Revisions`` row carrying every
           Requirement 8.2 attribute, with ``authority_basis_type``
           and ``authority_basis_id`` destructured from
           :class:`AuthorityBasisRef` to match the schema columns.
        8. INSERT exactly one ``Relationships`` row with
           ``relationship_type='Relates To'`` (AD-WS-17 — the
           Relationship type that binds Plan Review Revisions to
           their target Plan Revisions),
           ``semantic_role='review'`` (AD-WS-17 — the row-level
           discriminator distinguishing review edges from other
           ``Relates To`` rows),
           ``source_kind='plan_review_revision'`` /
           ``source_id=plan_review_id`` /
           ``source_revision_id=plan_review_revision_id`` (the new
           Plan Review Revision is the source endpoint of the edge),
           ``target_kind='plan_revision'`` /
           ``target_id=target_plan_revision_id`` /
           ``target_revision_id=NULL`` (Plan Revisions live in a
           single table with no separate Revision header, so the
           ``target_id`` carries the revision's own identifier and
           ``target_revision_id`` is NULL — matching the convention
           used by :class:`PlanRevisionService` for the
           ``Supersedes`` Relationship).
        9. Append the consequential ``Audit_Records`` row with
           ``action_type='create.plan_review'``,
           ``target_id=plan_review_id``, and
           ``target_revision_id=plan_review_revision_id`` inside the
           same transaction (Requirement 8.4 / AD-WS-5).
        10. Do **not** issue any UPDATE against ``Plan_Revisions``
            (Requirement 8.7). The AD-WS-19 lifecycle trigger would
            also reject such an attempt, but the application-layer
            code never even tries — the target Plan Revision row is
            read in step 3 and is otherwise untouched.

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_plan_revision_id: Identity of the target Plan
                Revision (Requirement 8.2). Must resolve to an
                existing row in ``Plan_Revisions`` whose
                ``lifecycle_state`` is ``'draft'``.
            outcome: One of ``"Endorse"``, ``"Changes_Requested"``,
                ``"Reject"`` (Requirement 8.2 / 8.6).
            rationale: Review rationale text of 1..10000 characters
                (Requirement 8.2 / 8.6).
            reviewing_party_id: Identity of the reviewing Party.
                Persisted on
                ``Plan_Review_Revisions.reviewing_party_id`` and on
                the consequential audit row's ``actor_party_id``.
                The Slice 1 ``Parties`` foreign key is enforced by
                the database.
            authority_basis: :class:`AuthorityBasisRef` carrying the
                ``type`` (AD-WS-10 set) and ``id`` of the authority
                basis. Destructured into the
                ``authority_basis_type`` and ``authority_basis_id``
                columns of ``Plan_Review_Revisions`` (Requirement
                8.2 / AD-WS-22).
            applicable_scope: Scope identifier the Plan Review
                applies within. Persisted on
                ``Plan_Review_Revisions.applicable_scope`` and
                passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback (Requirement 8.5 /
                AD-WS-9). The same engine is used to open a fresh
                transaction for the authorization evaluation itself
                (Slice 1 single-writer accommodation).
            correlation_id: Optional correlation identifier shared
                by every audit row written in this operation. A
                UUIDv7 is generated when omitted.
            evaluation_at: Optional explicit effective time passed
                to :meth:`AuthorizationService.evaluate` as the
                ``at`` parameter. Defaults to the recorded time of
                this transaction so the evaluation row's recorded
                time aligns with the consequential write it
                authorized.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every prohibited
                attribute prefix (Property 22). The HTTP layer
                forwards the raw request body here; service-level
                callers (e.g., unit tests) may pass ``None`` to
                skip the screen since the typed kwargs themselves
                cannot carry a prohibited attribute.

        Returns:
            :class:`CreatePlanReviewResult` carrying the persisted
            Plan Review Resource Identity, the Plan Review Revision
            Identity, the ``Relates To`` Relationship Identity, the
            persisted attributes, the recorded time, and the
            correlation identifier.

        Raises:
            PlanReviewValidationError: A required attribute is
                missing or a Requirement 8.2 enumeration / length
                check was violated, or the request body carried a
                prohibited execution / observed-outcome /
                produced-deliverable attribute.
            PlanReviewTargetNotResolvableError: The target Plan
                Revision Identity did not resolve to an existing
                Plan Revision (Requirement 8.6).
            PlanReviewTargetNotDraftError: The target Plan Revision
                exists but its ``lifecycle_state`` is not
                ``'draft'`` (Requirement 8.6).
            PlanReviewAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 8.5 / 11.4). The Denial Record was
                appended successfully in a separate transaction
                (AD-WS-9 / Slice 1 Requirement 7.6).
            PlanReviewAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry. Replaces
                :class:`PlanReviewAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``reviewing_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 8.4 /
                Slice 1 Requirement 13.6.
        """
        # 1. Screen the original request body when the route layer
        # has forwarded it. The typed kwargs themselves cannot carry
        # a prohibited attribute (the signature does not declare any
        # such field), but the HTTP layer's raw body might — Property
        # 22 demands rejection at the API boundary.
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, ALL_PROHIBITED_PREFIXES
                )
            except PlanningValidationError as exc:
                raise PlanReviewValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 8.2 / 8.6) before any
        # database read or authorization side-effect. Each validator
        # raises :class:`PlanReviewValidationError` with a stable
        # ``failed_constraint`` so the HTTP layer (task 15.1) can
        # render structured 400 responses identifying the invalid
        # attribute (Requirement 8.6).
        self._validate_required_strings(
            target_plan_revision_id=target_plan_revision_id,
            reviewing_party_id=reviewing_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_outcome(outcome)
        self._validate_rationale(rationale)
        normalized_basis = self._validate_authority_basis(authority_basis)

        # 3. Resolve the target Plan Revision Identity through a
        # single SELECT on ``Plan_Revisions``, reading both the
        # identifier and the lifecycle state in one row fetch. The
        # lookup runs on the caller's connection so it participates
        # in the caller's transactional view. Requirement 8.6
        # rejects (a) the unresolvable case and (b) the non-draft
        # case before authorization evaluation so the deny path
        # never reveals whether a Plan Revision exists / is in
        # draft for an unauthorized caller.
        row = connection.execute(
            text(
                "SELECT lifecycle_state FROM Plan_Revisions "
                "WHERE plan_revision_id = :plan_revision_id"
            ),
            {"plan_revision_id": target_plan_revision_id},
        ).one_or_none()
        if row is None:
            raise PlanReviewTargetNotResolvableError(
                target_plan_revision_id=target_plan_revision_id,
            )
        (target_lifecycle_state,) = row
        if target_lifecycle_state != _LIFECYCLE_DRAFT:
            raise PlanReviewTargetNotDraftError(
                target_plan_revision_id=target_plan_revision_id,
                lifecycle_state=target_lifecycle_state,
            )

        # 4. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Plan_Reviews row, the Plan_Review_Revisions row, the
        # Relates To Relationship row, and the consequential audit
        # row all share this timestamp; the optional
        # ``evaluation_at`` parameter changes only *when* authority
        # is evaluated *as of*, not the recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 5. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 documented accommodation for SQLite's
        # single-writer model; the deny path opens *another*
        # separate transaction for the Denial Record write, and the
        # caller's transaction stays a reader until step 6 below).
        # The authorization target is the target Plan Revision —
        # ``create.plan_review`` authority is scoped against the
        # Plan Revision the review is being recorded against, not
        # the parent Activity Plan. AD-WS-15's mapping of
        # ``create.plan_review`` to the ``review`` authority means
        # the wired role assignment must carry ``review`` in its
        # ``authorities_granted`` (Requirement 11.4); ``approve``
        # alone is not sufficient.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=reviewing_party_id,
                action=_ACTION_CREATE_PLAN_REVIEW,
                target=TargetRef(
                    kind=_KIND_PLAN_REVISION,
                    id=target_plan_revision_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_plan_review_denial(
                engine=engine,
                actor_party_id=reviewing_party_id,
                target_plan_revision_id=target_plan_revision_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise PlanReviewAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. Mint identifiers (AD-WS-2 / AD-WS-19). Plan Reviews
        # follow the standard Resource / Revision split so three
        # identifiers are minted: one Resource, one Revision, and
        # one Relationship for the ``Relates To`` row.
        plan_review_id = str(self.identity_service.new_resource_id())
        plan_review_revision_id = str(self.identity_service.new_revision_id())
        relates_to_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        # ``content_digest`` is bound to both the Plan Review
        # Resource and the Plan Review Revision identifier in
        # ``Identifier_Registry``; the digest is the SHA-256 of the
        # canonical JSON payload of the new Revision so two
        # different Plan Reviews never collide on the same digest.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_plan_revision_id": target_plan_revision_id,
                    "outcome": outcome,
                    "rationale": rationale,
                    "reviewing_party_id": reviewing_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 7. Register both identifiers in ``Identifier_Registry``
        # carrying their AD-WS-19 ``resource_kind`` tags. Two rows
        # are inserted: one Resource-level row tagged
        # ``'plan_review'`` and one Revision-level row tagged
        # ``'plan_review_revision'``. The helper delegates to
        # :meth:`IdentityService.reject_if_duplicate` so the Slice 1
        # identifier-conflict Denial Record pathway fires on any
        # collision; on success the helper INSERTs each row inside
        # the caller's transaction.
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_PLAN_REVIEW,
            plan_review_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=reviewing_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PLAN_REVIEW,
            recorded_time=recorded_time,
        )
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_PLAN_REVIEW_REVISION,
            plan_review_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=reviewing_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PLAN_REVIEW,
            recorded_time=recorded_time,
        )

        # 8. Insert the Plan Review Resource header row. The header
        # carries only the Resource Identity and the created_at
        # timestamp; all per-Revision content lives on
        # ``Plan_Review_Revisions`` below. The shared
        # ``recorded_at`` ensures the Resource and its first
        # Revision agree on creation time byte-for-byte.
        connection.execute(
            text(
                """
                INSERT INTO Plan_Reviews (plan_review_id, created_at)
                VALUES (:plan_review_id, :created_at)
                """
            ),
            {
                "plan_review_id": plan_review_id,
                "created_at": recorded_at,
            },
        )

        # 9. Insert the first immutable Plan Review Revision row.
        # Every Requirement 8.2 attribute lands here.
        # ``authority_basis_type`` and ``authority_basis_id`` are
        # destructured from :class:`AuthorityBasisRef` to match the
        # schema columns (AD-WS-22 — the Slice 1 enumeration is
        # reused unchanged so the CHECK constraint on
        # ``authority_basis_type`` accepts exactly the AD-WS-10
        # values).
        connection.execute(
            text(
                """
                INSERT INTO Plan_Review_Revisions (
                    plan_review_revision_id, plan_review_id,
                    target_plan_revision_id, outcome, rationale,
                    reviewing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :plan_review_revision_id, :plan_review_id,
                    :target_plan_revision_id, :outcome, :rationale,
                    :reviewing_party_id, :authority_basis_type,
                    :authority_basis_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "plan_review_revision_id": plan_review_revision_id,
                "plan_review_id": plan_review_id,
                "target_plan_revision_id": target_plan_revision_id,
                "outcome": outcome,
                "rationale": rationale,
                "reviewing_party_id": reviewing_party_id,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 10. Insert the single ``Relates To`` Relationship row
        # binding the new Plan Review Revision to its target Plan
        # Revision (Requirement 8.3 / AD-WS-17). The
        # ``semantic_role`` column carries the ``'review'``
        # discriminator that distinguishes this edge from any
        # future non-review ``Relates To`` rows in the slice.
        # ``source_kind='plan_review_revision'`` with
        # ``source_id=plan_review_id`` /
        # ``source_revision_id=plan_review_revision_id`` so the
        # backlink algorithm can return the edge from both endpoints
        # using the Revision Identity; ``target_kind='plan_revision'``
        # with ``target_id=target_plan_revision_id`` /
        # ``target_revision_id=NULL`` mirrors the convention used by
        # :class:`PlanRevisionService` (Plan Revisions live in a
        # single Revision-level table with no separate Resource
        # header).
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
                "source_kind": _KIND_PLAN_REVIEW_REVISION,
                "source_id": plan_review_id,
                "source_revision_id": plan_review_revision_id,
                "target_kind": _KIND_PLAN_REVISION,
                "target_id": target_plan_revision_id,
                "target_revision_id": None,
                "authoring_party_id": reviewing_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_REVIEW,
            },
        )

        # 11. Append the consequential audit row (Requirement 8.4 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Plan_Reviews,
        # Plan_Review_Revisions, and Relationships rows together.
        # ``target_id`` is the Plan Review Resource Identity;
        # ``target_revision_id`` is the Plan Review Revision
        # Identity — matching the audit-row convention used by
        # :class:`ObjectiveService` for Resource/Revision artifacts.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=reviewing_party_id,
            action_type=_ACTION_CREATE_PLAN_REVIEW,
            target_id=plan_review_id,
            target_revision_id=plan_review_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        # 12. Requirement 8.7: the target Plan Revision row's
        # ``lifecycle_state`` is unchanged by this transaction. No
        # UPDATE is issued against Plan_Revisions anywhere in this
        # method; the row was read in step 3 and is otherwise
        # untouched. The AD-WS-19 lifecycle trigger would also
        # reject any UPDATE attempt — the application-layer
        # restraint here is belt-and-braces with the database
        # trigger.

        return CreatePlanReviewResult(
            plan_review_id=plan_review_id,
            plan_review_revision_id=plan_review_revision_id,
            target_plan_revision_id=target_plan_revision_id,
            outcome=outcome,
            rationale=rationale,
            reviewing_party_id=reviewing_party_id,
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
        target_plan_revision_id: Any,
        reviewing_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 8.6: a Plan Review submission that omits the
        target Plan Revision Identity or the applicable scope is
        rejected. This validator additionally covers
        ``reviewing_party_id`` (which Requirement 8.5 implicitly
        requires — an unauthenticated request has no Party Identity
        to authorize against).
        """
        if not target_plan_revision_id or not isinstance(
            target_plan_revision_id, str
        ):
            raise PlanReviewValidationError(
                "target_plan_revision_id is required; Requirement 8.6 "
                "rejects Plan Reviews missing the target Plan Revision "
                "Identity.",
                failed_constraint="target_plan_revision_id_missing",
            )
        if not reviewing_party_id or not isinstance(reviewing_party_id, str):
            raise PlanReviewValidationError(
                "reviewing_party_id is required; Requirement 8.5 rejects "
                "unauthenticated Plan Review creation.",
                failed_constraint="reviewing_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise PlanReviewValidationError(
                "applicable_scope is required; Requirement 8.6 rejects "
                "Plan Reviews missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_outcome(outcome: Any) -> None:
        """Reject outcome outside the Requirement 8.2 enumeration.

        Per Requirement 8.2 the outcome is drawn from the enumerated
        set ``{Endorse, Changes_Requested, Reject}``; Requirement 8.6
        specifies the rejection path with the ``outcome_out_of_set``
        constraint name. The same set is enforced by the
        ``Plan_Review_Revisions.outcome`` CHECK constraint; this
        validator surfaces a precise error before the SQL layer.
        """
        if outcome is None or not isinstance(outcome, str) or outcome == "":
            raise PlanReviewValidationError(
                "outcome is required and must be one of "
                f"{sorted(_VALID_OUTCOMES)}; Requirement 8.2 / 8.6.",
                failed_constraint="outcome_missing",
            )
        if outcome not in _VALID_OUTCOMES:
            raise PlanReviewValidationError(
                f"outcome {outcome!r} is not in the Requirement 8.2 "
                f"enumeration {sorted(_VALID_OUTCOMES)}.",
                failed_constraint="outcome_out_of_set",
            )

    @staticmethod
    def _validate_rationale(rationale: Any) -> None:
        """Reject rationale outside the Requirement 8.2 range.

        Per Requirement 8.2 the rationale is 1..10000 characters and
        required. Empty or non-string rationales surface as
        ``rationale_missing`` since the actionable next step is the
        same in both cases (supply a non-empty string). An over-long
        rationale surfaces as ``rationale_too_long``.
        """
        if (
            rationale is None
            or not isinstance(rationale, str)
            or rationale == ""
        ):
            raise PlanReviewValidationError(
                "rationale is required and must be a non-empty string of "
                f"{_RATIONALE_MIN_CHARS}..{_RATIONALE_MAX_CHARS} "
                "characters; Requirement 8.2 / 8.6.",
                failed_constraint="rationale_missing",
            )
        if len(rationale) > _RATIONALE_MAX_CHARS:
            raise PlanReviewValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_RATIONALE_MAX_CHARS}-character limit imposed by "
                "Requirement 8.2.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 8.2 / AD-WS-22: the authority basis is drawn
        from the Slice 1 enumeration ``{role-grant-id, scope-id,
        delegation-chain-id}`` (AD-WS-10). The Python-typed signature
        already constrains callers to pass an
        :class:`AuthorityBasisRef` whose ``type`` Literal restricts
        the enumeration; the HTTP layer (task 15.1) may pass a dict
        if it has not yet bound the request to the typed model, so
        this validator coerces both shapes:

        - If *authority_basis* is already an
          :class:`AuthorityBasisRef`, return it unchanged.
        - If *authority_basis* is a :class:`Mapping`, attempt to
          construct an :class:`AuthorityBasisRef`; Pydantic
          validation enforces the AD-WS-10 enumeration and rejects
          unknown fields. Validation errors surface as
          :class:`PlanReviewValidationError` with the
          appropriate ``failed_constraint``.
        - Any other shape is rejected as
          ``"authority_basis_missing"``.

        Returns:
            The validated :class:`AuthorityBasisRef`. Always a fresh
            instance (or the original, when the input was already
            one) — never ``None``.

        Raises:
            PlanReviewValidationError: The authority basis is
                missing, malformed, has an out-of-set ``type``, or
                is missing its ``id``.
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            # The Pydantic Literal already constrains type to the
            # AD-WS-10 set, and Pydantic UUID validation ensures id
            # is well-formed. Return as-is.
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise PlanReviewValidationError(
                "authority_basis is required and must be an "
                "AuthorityBasisRef (or a mapping convertible to one); "
                f"received {type(authority_basis).__name__}.",
                failed_constraint="authority_basis_missing",
            )

        basis_type = authority_basis.get("type")
        basis_id = authority_basis.get("id")

        if basis_type is None or not isinstance(basis_type, str) or basis_type == "":
            raise PlanReviewValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise PlanReviewValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration {sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise PlanReviewValidationError(
                "authority_basis.id is required per Requirement 8.2.",
                failed_constraint="authority_basis_id_missing",
            )

        # Delegate canonical-form validation (UUID shape) to Pydantic
        # by constructing the typed model. Any malformed value will
        # raise a Pydantic ValidationError which we surface as a
        # PlanReviewValidationError with the same constraint name as
        # the manual checks above for the closest applicable failure.
        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise PlanReviewValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    # -- denial side-channel ----------------------------------------------

    def _persist_plan_review_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_plan_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Plan Review attempt.

        Implements the AD-WS-9 / Slice 1 Requirement 7.6 retry
        contract verbatim (mirroring
        :meth:`walking_slice.planning.plan_revisions.PlanRevisionService._persist_plan_revision_denial`):
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
          :class:`PlanReviewAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_plan_review` raises
        :class:`PlanReviewAuthorizationError` (or this method raises
        :class:`PlanReviewAuditFailureError`). The Denial Record
        must therefore live outside that scope to survive (AD-WS-9 /
        Requirement 8.5). The denial row's ``target_id`` is the
        target Plan Revision identifier rather than the (not-yet
        minted) Plan Review identifier; this matches the AD-WS-9
        contract that denial rows reference the resolved Resource
        the unauthorized action was attempted against.

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
                        attempted_action=_ACTION_CREATE_PLAN_REVIEW,
                        target_id=target_plan_revision_id,
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

        raise PlanReviewAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this
# module does not import private names from sibling planning modules.
# The functions are intentionally identical to their Plan Revisions
# siblings: correlation identifiers are non-domain values and the
# digest is opaque to :class:`Identifier_Registry` — both
# implementations could be replaced with shared utility functions in
# a future refactor without changing observable behavior.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Plan
    Review creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to both the Plan
    Review Resource and the Plan Review Revision identifiers in
    ``Identifier_Registry``. Plan Reviews follow the standard
    Resource / Revision split, so this digest is bound twice per
    Plan Review creation — once for the Resource header and once
    for the first Revision — matching the convention used by
    :class:`ObjectiveService`.
    """
    return hashlib.sha256(content).hexdigest()
