"""Planning_Service.PlanApprovals — Plan Approval Immutable Records and
the atomic ``draft → approved`` lifecycle transition.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.PlanApprovals" — public dataclass surface, the
  authority string (``create.plan_approval`` → ``approve`` per
  AD-WS-15), the AD-WS-9 separate-transaction Denial Record on deny,
  the AD-WS-20 persistence flow (``Plan_Approval_Records`` →
  ``Addresses`` Relationship → Provenance Manifest → ``Omission_Entries``
  → ``Plan_Revisions.lifecycle_state`` UPDATE → consequential
  ``Audit_Records``), and the AD-WS-19 session-pragma gate on the
  one permitted ``Plan_Revisions`` lifecycle transition.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with the appropriate ``kind``
  / ``resource_kind`` tag); Authorization (``create.plan_approval``
  maps to ``approve`` per AD-WS-15 / Requirement 11.5; the deny path
  follows the Slice 1 separate-transaction Denial-Record pattern
  reproduced from :class:`walking_slice.knowledge.KnowledgeService`
  and :class:`walking_slice.planning.plan_revisions.PlanRevisionService`).
- AD-WS-15 — additive ``create.plan_approval → approve`` mapping.
- AD-WS-17 — additive ``Relationships.semantic_role`` column. Plan
  Approval's ``Addresses`` Relationship row does not require a
  ``semantic_role`` discriminator (the ``Addresses`` Relationship Type
  is already precise for Plan Approval → Plan Revision) and is
  written with ``semantic_role = NULL`` to match the convention used
  by Slice 1 ``Addresses`` rows.
- AD-WS-19 — additive ``Identifier_Registry.resource_kind`` column the
  helper :func:`walking_slice.planning._helpers._record_planning_resource`
  populates with ``'plan_approval'``; AD-WS-19 also names the
  connection-scoped pragma ``walking_slice.plan_approval_in_progress``
  that gates the single permitted ``Plan_Revisions`` UPDATE
  (``'draft' → 'approved'``).
- AD-WS-20 — the persistence-flow contract this module implements
  verbatim: every artifact of the Plan Approval is INSERTed (or, for
  the one lifecycle row, UPDATEd) inside one transaction, sharing
  one recorded time and one correlation identifier.
- AD-WS-22 — the ``authority_basis.type`` value is drawn unchanged
  from the Slice 1 enumeration ``{role-grant-id, scope-id,
  delegation-chain-id}`` (Requirement 9.2 defers to AD-WS-10).

Plan Approval Records are Immutable Records (AD-WS-4 / Requirement 9):
``Plan_Approval_Records`` carries every Requirement 9.2 attribute on a
single row; there is no separate Resource / Revision split because the
record itself is the durable governance decision and is byte-equivalent
forever once created. Recording a Plan Approval writes exactly one
``Plan_Approval_Records`` row, one ``Addresses`` Relationship row, one
``Provenance_Manifests`` row (via the existing
:class:`walking_slice.manifests.ProvenanceManifestWriter`), zero or
more ``Omission_Entries`` rows, optionally one UPDATE against the
target ``Plan_Revisions.lifecycle_state`` (only on outcome
``'Approve'``), and one consequential ``Audit_Records`` row.

Task scope (task 11.1 — happy path)
====================================

This module implements :meth:`PlanApprovalService.create_plan_approval`
for the permit-and-persist path:

1. Validate request inputs per Requirement 9.2 / 9.5:
   ``outcome ∈ {Approve, Reject_Approval}``, ``rationale`` 1..4000
   characters, ``authority_basis.type ∈ {role-grant-id, scope-id,
   delegation-chain-id}``, plus the required identifiers and the
   applicable scope. Defensively reject any prohibited execution /
   observed-outcome / produced-deliverable attribute on the optional
   ``request_attributes`` payload via
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   (Property 22).
2. Resolve the target Plan Revision through a single SELECT against
   ``Plan_Revisions``, reading both ``activity_plan_id`` and
   ``lifecycle_state``. Requirement 9.5 mandates rejection when the
   identifier does not resolve and when ``lifecycle_state != 'draft'``.
3. Pre-check Requirement 9.5: at most one Plan Approval exists per
   Plan Revision. The UNIQUE constraint on
   ``Plan_Approval_Records.target_plan_revision_id`` is the source of
   truth; the pre-check surfaces a structured error with the existing
   Plan Approval Identity before the database round-trip.
4. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (the Slice 1 single-writer accommodation mirrored from
   :meth:`KnowledgeService.create_decision`). On deny, persist a
   Denial Record in another separate transaction with the AD-WS-9 /
   Slice 1 Requirement 7.6 three-retry exponential-backoff sequence
   (``0.01s / 0.02s / 0.04s``), and raise
   :class:`PlanApprovalAuthorizationError`. On total audit failure
   raise :class:`PlanApprovalAuditFailureError` in place of
   :class:`PlanApprovalAuthorizationError`.
5. On permit, perform the AD-WS-20 persistence flow inside the
   caller's transaction:

   a. Set the SQLite session pragma
      ``walking_slice.plan_approval_in_progress`` to the correlation
      identifier (gates the Plan Revision lifecycle trigger from
      AD-WS-19).
   b. INSERT the ``Plan_Approval_Records`` row.
   c. INSERT the ``Addresses`` ``Relationships`` row binding the
      Plan Approval to its target Plan Revision (Requirement 9.3).
   d. INSERT the Provenance Manifest via the existing
      :class:`ProvenanceManifestWriter` (subject_kind
      ``'plan_approval'``; the single material source is the target
      Plan Revision recorded as kind ``'plan_revision'``).
   e. INSERT any supplied Omission Entries (the writer handles these
      alongside the Manifest).
   f. When ``outcome == 'Approve'``, execute the one permitted
      ``UPDATE`` against ``Plan_Revisions.lifecycle_state`` to
      ``'approved'`` (Requirement 9.1). When ``outcome ==
      'Reject_Approval'``, the lifecycle UPDATE is skipped — the
      Plan Revision stays in ``'draft'`` and only the
      ``Plan_Approval_Records`` row stands as the durable record of
      the rejection.
   g. Append the consequential ``Audit_Records`` row.
   h. Clear the session pragma so the lifecycle trigger goes back
      to rejecting every other UPDATE.

Requirements satisfied
======================

    9.1 — authorized Plan Approval creation produces exactly one Plan
          Approval Immutable Record and, on outcome ``'Approve'``,
          atomically transitions the target Plan Revision's
          ``lifecycle_state`` from ``'draft'`` to ``'approved'`` within
          the same transaction.
    9.2 — every Plan Approval Record records target Activity Plan
          Identity, target Plan Revision Identity, outcome ∈
          ``{Approve, Reject_Approval}``, rationale of 1..4000 chars,
          approving Party Identity, authority basis with type ∈ the
          AD-WS-10 set, applicable scope, and recorded time in UTC
          with millisecond precision.
    9.3 — every Plan Approval Record is linked to its target Plan
          Revision through exactly one ``Addresses`` Relationship.
    9.5 — unresolvable / non-draft target Plan Revision and duplicate
          Plan Approval against the same Plan Revision (per the
          UNIQUE constraint) are rejected before any write; missing
          outcome, missing rationale, missing authority basis, and
          missing applicable scope are rejected with a structured
          error identifying the invalid attribute.
    9.7 — the Audit_Log appends a consequential record inside the
          same transaction as the Plan Approval Record creation.
    10.1 — unauthorized requests cause no Plan Approval Record to be
           created, no lifecycle transition to be recorded, and no
           in-flight row to be persisted.
    10.2 — the Denial Record carries actor Party Identity, attempted
           action, target Activity Plan Identity, target Plan
           Revision Identity, recorded time, and denial reason code.
    10.4 — the denial response carries only ``generic_denial_indicator``,
           ``reason_code``, and ``correlation_id`` (AD-WS-9).
    10.5 — the targeted Plan Revision row, every constituent
           Relationship, every Plan Review Revision targeting that
           Plan Revision, and every related upstream Resource are
           left byte-equivalent on the deny path (the caller's
           transaction rolls back; the Denial Record lives in its
           own separate transaction).
    10.6 — the Denial Record append is retried up to three times with
           exponential backoff; on total audit failure
           :class:`PlanApprovalAuditFailureError` is raised so denial
           and audit cannot silently diverge.
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
from walking_slice.identity import IdentityService
from walking_slice.manifests import (
    IncludedSource,
    OmissionEntry as ManifestOmissionEntry,
    ProvenanceManifestWriter,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)
from walking_slice.planning._persistence import (
    clear_plan_approval_in_progress,
    set_plan_approval_in_progress,
)
from walking_slice.planning.models import PlanApprovalOmissionEntry


__all__ = [
    "CreatePlanApprovalResult",
    "PlanApprovalAuditFailureError",
    "PlanApprovalAuthorizationError",
    "PlanApprovalConflictError",
    "PlanApprovalService",
    "PlanApprovalTargetNotDraftError",
    "PlanApprovalTargetNotResolvableError",
    "PlanApprovalValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship kind strings, lifecycle values, manifest
# kinds, and enumerations are pulled out as module-level ``Final`` so the
# names downstream property tests look for in ``Audit_Records.action_type``
# and ``Relationships`` are textually stable and the strings stay
# aligned with the :mod:`walking_slice.planning._persistence` schema,
# the AD-WS-15 authority mapping in :mod:`walking_slice.authorization`,
# the AD-WS-17 ``Relationships.semantic_role`` column, and the AD-WS-19
# / AD-WS-20 lifecycle trigger gate.
# ---------------------------------------------------------------------------


# ``create.plan_approval`` maps to the ``approve`` authority per
# AD-WS-15 / Requirement 11.5. The same string is recorded as
# ``action_type`` on the consequential audit row (Requirement 9.7) and
# as ``attempted_action`` on the separate-transaction Denial Record
# appended by :meth:`PlanApprovalService._persist_plan_approval_denial`
# so audit consumers can correlate denial rows with the action a Party
# was attempting (Requirement 10.2).
_ACTION_CREATE_PLAN_APPROVAL: Final[str] = "create.plan_approval"

# Relationship Type and source / target ``kind`` strings for the single
# ``Addresses`` Relationship row inserted alongside the Plan Approval
# Record. The Plan Approval is the *source* endpoint of the edge (the
# governance decision that addresses the Plan Revision) and the Plan
# Revision is the *target* endpoint. ``semantic_role`` is left NULL —
# the ``Addresses`` Relationship Type is precise for this edge and
# AD-WS-17's ``semantic_role`` discriminator is reserved for ``Relates
# To`` rows that need to be distinguished from non-Slice-2 ``Relates
# To`` edges.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_PLAN_APPROVAL: Final[str] = "plan_approval"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tag (Slice 2 additive enumeration
# per AD-WS-19) for Plan Approval. A Plan Approval is an Immutable
# Record (AD-WS-4 / Requirement 9.4 — approved Plan Revisions and
# their finalized Plan Approval Records are byte-equivalent forever)
# so it is registered with ``kind='immutable_record'`` rather than the
# Resource/Revision pair used by Plan Reviews.
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_PLAN_APPROVAL: Final[str] = "plan_approval"

# Provenance Manifest ``subject_kind`` for a Plan Approval Manifest.
# The value mirrors the additive Slice 2 entry in
# :data:`walking_slice.manifests._SUBJECT_KINDS` and the Slice 1 schema
# CHECK extension in :mod:`walking_slice.persistence`. The subject of
# this manifest is the Plan Approval Immutable Record itself —
# ``subject_revision_id`` is NULL because a Plan Approval has no
# revisions (AD-WS-4).
_MANIFEST_SUBJECT_KIND_PLAN_APPROVAL: Final[str] = "plan_approval"

# Outcome enumeration per Requirement 9.2 / 9.5. The same set is
# enforced by the ``Plan_Approval_Records.outcome`` CHECK constraint
# in :mod:`walking_slice.planning._persistence`; centralizing the
# tuple here surfaces a precise, structured ``failed_constraint`` on
# :class:`PlanApprovalValidationError` before the SQL layer is reached.
_OUTCOME_APPROVE: Final[str] = "Approve"
_OUTCOME_REJECT_APPROVAL: Final[str] = "Reject_Approval"
_VALID_OUTCOMES: Final[frozenset[str]] = frozenset(
    {_OUTCOME_APPROVE, _OUTCOME_REJECT_APPROVAL}
)

# Authority basis type enumeration per AD-WS-10 / Requirement 9.2 /
# AD-WS-22. The same set is enforced by the
# ``Plan_Approval_Records.authority_basis_type`` CHECK constraint.
# Centralizing the tuple here lets the validator reject malformed
# requests structurally before they touch SQL. The
# :class:`AuthorityBasisRef` Pydantic ``Literal`` already constrains
# Python-typed callers to the same enumeration, but the application
# layer may receive dict-shaped inputs from the HTTP layer that have
# not yet been bound to :class:`AuthorityBasisRef`; this check is the
# defense-in-depth that survives even those cases.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Lifecycle state values per AD-WS-18 — exactly ``{draft, approved}``
# for this slice. The target Plan Revision must be ``'draft'`` for a
# Plan Approval to be accepted (Requirement 9.5); the
# ``'Approve'``-outcome lifecycle UPDATE writes ``'approved'`` and is
# the only permitted ``Plan_Revisions`` UPDATE in the system
# (AD-WS-19, gated by the session pragma).
_LIFECYCLE_DRAFT: Final[str] = "draft"
_LIFECYCLE_APPROVED: Final[str] = "approved"

# Validation limits per Requirement 9.2 (Plan Approval rationale —
# note the 4,000-char ceiling, distinct from Plan Review's 10,000).
# The schema CHECK constraint on ``Plan_Approval_Records.rationale``
# enforces the same values; centralizing them here surfaces precise,
# structured constraint names through
# :class:`PlanApprovalValidationError`.
_RATIONALE_MIN_CHARS: Final[int] = 1
_RATIONALE_MAX_CHARS: Final[int] = 4_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 10.6 / AD-WS-9 / Slice 1
# Requirement 7.6). Three retries after the initial attempt for a
# total of four attempts. The sequence is byte-equivalent to the one
# in sibling Planning_Service modules so every Planning_Service
# module presents identical denial-side timing (which Property 18 —
# Indistinguishable denial — relies on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class PlanApprovalValidationError(ValueError):
    """Raised when a Plan Approval submission fails Requirement 9.2 / 9.5 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_plan_revision_id_missing"``,
            ``"approving_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"outcome_missing"`` (empty / non-string outcome),
            ``"outcome_out_of_set"`` (outcome not in the AD-WS-15 /
                Requirement 9.2 enumeration),
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


class PlanApprovalTargetNotResolvableError(LookupError):
    """Raised when the target Plan Revision Identity does not resolve.

    Requirement 9.5 requires the target Plan Revision Identity to
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


class PlanApprovalTargetNotDraftError(LookupError):
    """Raised when the target Plan Revision is not in ``'draft'``.

    Requirement 9.5 rejects Plan Approval submissions whose target
    Plan Revision's ``lifecycle_state`` is not ``'draft'`` (i.e. the
    only other valid value: ``'approved'``). An approved Plan
    Revision is byte-equivalent forever per Requirement 9.4 —
    accepting a Plan Approval against one would either (a) attempt a
    forbidden second lifecycle transition or (b) silently create a
    second Plan Approval Record (which the UNIQUE constraint would
    then reject with a less actionable error). The check runs before
    authorization evaluation for the same don't-leak-existence
    reason as :class:`PlanApprovalTargetNotResolvableError`.

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
            f"lifecycle_state={lifecycle_state!r}; Requirement 9.5 "
            "rejects Plan Approvals against any target whose lifecycle "
            f"state is not {_LIFECYCLE_DRAFT!r} "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.lifecycle_state = lifecycle_state
        self.failed_constraint = failed_constraint


class PlanApprovalConflictError(LookupError):
    """Raised when a Plan Approval already targets the same Plan Revision.

    Requirement 9.5 enforces at most one Plan Approval per Plan
    Revision; the UNIQUE constraint on
    ``Plan_Approval_Records.target_plan_revision_id`` is the source
    of truth, and this pre-check surfaces a structured error with
    the existing Plan Approval Identity before the SQL layer is
    reached. The pre-check runs after target-resolution and
    not-draft checks so the deny path's information-leakage profile
    is unchanged (a caller whose target is already approved sees the
    not-draft rejection first; the conflict path is reachable only
    when the lifecycle is still ``'draft'`` and a previous
    ``Reject_Approval`` Plan Approval row exists, which keeps the
    Plan Revision in draft yet consumes the UNIQUE slot).

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        existing_plan_approval_id: The Plan Approval Identity that
            already targets the same Plan Revision.
        failed_constraint: ``"plan_approval_already_recorded"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        existing_plan_approval_id: str,
        failed_constraint: str = "plan_approval_already_recorded",
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} is already "
            f"the subject of Plan Approval {existing_plan_approval_id!r}; "
            "Requirement 9.5 permits at most one Plan Approval per Plan "
            f"Revision (failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.existing_plan_approval_id = existing_plan_approval_id
        self.failed_constraint = failed_constraint


class PlanApprovalAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Plan Approval attempt.

    Mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 10.4). The exception
    carries only ``reason_code`` and ``correlation_id`` — Requirement
    10.4 forbids leaking authorized Party identities, target
    existence, role assignment details, Plan Revision contents,
    Activity Plan titles, Project names, or Objective statements
    through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Plan Approval creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class PlanApprovalAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 10.6 / Slice 1 Requirement 7.6).

    Mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuditFailureError`.
    On total audit-append failure the exception is raised *in place
    of* :class:`PlanApprovalAuthorizationError` — denial and audit
    have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no
    ``Plan_Approval_Records``, ``Addresses`` Relationship,
    ``Provenance_Manifests``, ``Omission_Entries``, lifecycle UPDATE,
    or consequential audit row is persisted.

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
            f"Denial Record append for a denied Plan Approval failed "
            f"after {attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Result value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreatePlanApprovalResult:
    """Result of :meth:`PlanApprovalService.create_plan_approval`.

    Returned so callers (the HTTP layer in task 15.1, tests, and any
    downstream code that correlates a Plan Approval with its target
    Plan Revision or its Provenance Manifest) can read every
    persisted identifier, the new lifecycle state, the recorded time,
    and the correlation identifier in one round-trip.

    Attributes:
        plan_approval_id: The Plan Approval Immutable Record Identity
            (UUIDv7). Registered in ``Identifier_Registry`` with
            ``kind='immutable_record'`` and
            ``resource_kind='plan_approval'`` per AD-WS-19.
        target_activity_plan_id: The Activity Plan Resource Identity
            owning the target Plan Revision. Read from the
            ``Plan_Revisions`` row at pre-check time and copied to
            ``Plan_Approval_Records.target_activity_plan_id``.
        target_plan_revision_id: The Plan Revision Identity the
            approval targets; copied byte-equivalent from the request
            input.
        outcome: One of ``"Approve"``, ``"Reject_Approval"``
            (Requirement 9.2).
        rationale: The persisted rationale text (1..4000 chars).
        approving_party_id: Identity of the approving Party.
        authority_basis: The :class:`AuthorityBasisRef` carried into
            the request, returned verbatim so callers can correlate
            the basis type and identifier with the persisted columns.
        applicable_scope: Scope identifier the Plan Approval applies
            within.
        addresses_relationship_id: Identity of the single
            ``Addresses`` ``Relationships`` row inserted alongside
            the Plan Approval (Requirement 9.3).
        manifest_id: Identity of the Provenance Manifest inserted by
            the wired :class:`ProvenanceManifestWriter` with
            ``subject_kind='plan_approval'``.
        omission_entry_ids: Identities of the ``Omission_Entries``
            rows written by the manifest writer, in the order the
            caller supplied them.
        new_lifecycle_state: The Plan Revision's lifecycle state after
            the transaction commits — ``'approved'`` when ``outcome
            == 'Approve'`` (the one permitted lifecycle transition
            was executed) and ``'draft'`` when ``outcome ==
            'Reject_Approval'`` (the Plan Approval row records the
            rejection but the Plan Revision stays in draft per
            Requirement 9.5's distinction between the two outcomes).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Plan_Approval_Records``, ``Relationships``,
            ``Provenance_Manifests``, ``Omission_Entries``, optional
            lifecycle UPDATE, and consequential audit rows.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row, the
            consequential audit row, and the session pragma value
            used to gate the lifecycle UPDATE trigger (AD-WS-19).
    """

    plan_approval_id: str
    target_activity_plan_id: str
    target_plan_revision_id: str
    outcome: str
    rationale: str
    approving_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    addresses_relationship_id: str
    manifest_id: str
    omission_entry_ids: tuple[str, ...]
    new_lifecycle_state: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanApprovalService:
    """Persist Plan Approval Immutable Records and the atomic
    ``draft → approved`` lifecycle transition.

    Like its sibling Planning_Service classes, this service is
    connection-scoped at call time: :meth:`create_plan_approval`
    accepts the caller's :class:`sqlalchemy.engine.Connection` and
    writes inside the caller's transaction (AD-WS-5, AD-WS-20). The
    service instance therefore holds only the cross-request
    collaborators and can be shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.PlanApprovals" declares it
    ``@dataclass(frozen=True)`` — Slice 2 service instances are
    immutable container objects that bundle the Slice 1 collaborators
    for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Plan_Approval_Records``, ``Relationships``,
            ``Provenance_Manifests``, ``Omission_Entries``, optional
            ``Plan_Revisions`` UPDATE, and ``Audit_Records`` rows.
            The clock is consulted once per write so every artifact
            of the transaction shares one timestamp.
        identity_service: Generates the Plan Approval Immutable
            Record Identity and the ``Addresses`` Relationship
            Identity, plus persists their ``Identifier_Registry``
            bindings (the Plan Approval binding carries its Slice 2
            ``resource_kind='plan_approval'`` tag per AD-WS-19).
            The wired :class:`ProvenanceManifestWriter` reuses the
            same identity service to mint the manifest identifier.
        audit_log: Appends the consequential audit row (Requirement
            9.7) inside the caller's transaction.
        authorization_service: Evaluates ``create.plan_approval``
            authority per AD-WS-15 / Requirement 11.5; the deny path
            is the Slice 1 separate-transaction Denial-Record
            pattern.
        manifest_writer: Persists the Plan Approval Provenance
            Manifest and any supplied Omission Entries inside the
            caller's transaction (AD-WS-5 / AD-WS-20). The writer's
            ``_SUBJECT_KINDS`` set has been additively extended to
            permit ``'plan_approval'`` and ``_INCLUDED_SOURCE_KINDS``
            to permit ``'plan_revision'``; the writer is otherwise
            unchanged from Slice 1.
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
    manifest_writer: ProvenanceManifestWriter
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_plan_approval(
        self,
        connection: Connection,
        engine: Engine,
        *,
        target_plan_revision_id: str,
        outcome: Literal["Approve", "Reject_Approval"],
        rationale: str,
        approving_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        omissions: Sequence[PlanApprovalOmissionEntry] = (),
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreatePlanApprovalResult:
        """Create a Plan Approval Immutable Record and (on ``'Approve'``)
        atomically transition the target Plan Revision's lifecycle
        state from ``'draft'`` to ``'approved'``.

        Per Requirements 9.1 through 9.7, 10.1 through 10.6, 11.5
        (``approve`` authority is required), AD-WS-9 (indistinguishable
        denial), AD-WS-15 (``create.plan_approval → approve``),
        AD-WS-19 (lifecycle trigger gated by the session pragma), and
        AD-WS-20 (atomic Plan Approval persistence flow):

        1. Optionally screen the original request body against the
           prohibited-attribute prefixes (Property 22 / Requirements
           12.1, 12.2, 13.1, 13.2, 13.5).
        2. Input validation (Requirement 9.2 / 9.5) — every
           enumeration check, length check, and required-attribute
           check runs before any database read so a malformed request
           never touches identity service, the ``Plan_Revisions``
           lookup, or the authorization service.
        3. Resolve the target Plan Revision via a single SELECT
           against ``Plan_Revisions``, reading ``activity_plan_id``
           and ``lifecycle_state``. When the identifier does not
           resolve, raise :class:`PlanApprovalTargetNotResolvableError`.
           When the lifecycle state is not ``'draft'``, raise
           :class:`PlanApprovalTargetNotDraftError`. Both checks run
           before authorization evaluation so the deny path never
           reveals whether a Plan Revision exists / is in draft for
           an unauthorized caller.
        4. Pre-check Requirement 9.5's uniqueness invariant via a
           SELECT against ``Plan_Approval_Records``. The UNIQUE
           constraint on ``target_plan_revision_id`` is the source of
           truth; the pre-check produces a structured
           :class:`PlanApprovalConflictError` with the existing
           Plan Approval Identity instead of a generic IntegrityError.
        5. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the AD-WS-9 / Slice 1 Requirement 7.6
           retry sequence (0.01s / 0.02s / 0.04s exponential backoff,
           three retries after the initial attempt). On total audit
           failure raise :class:`PlanApprovalAuditFailureError` in
           place of :class:`PlanApprovalAuthorizationError`.
        6. On ``permit``, perform the AD-WS-20 persistence flow
           inside the caller's transaction:

           a. Mint the Plan Approval Identity (an Immutable Record
              identifier) and the ``Addresses`` Relationship Identity.
              Register the Plan Approval Identity in
              ``Identifier_Registry`` with ``kind='immutable_record'``
              and ``resource_kind='plan_approval'``.
           b. Set the session pragma
              ``walking_slice.plan_approval_in_progress`` to the
              correlation identifier so the AD-WS-19 lifecycle
              trigger will permit exactly the one
              ``draft → approved`` UPDATE later in the flow.
           c. INSERT the ``Plan_Approval_Records`` row carrying every
              Requirement 9.2 attribute (target Activity Plan
              Identity, target Plan Revision Identity, outcome,
              rationale, approving Party Identity, destructured
              authority basis, applicable scope, recorded time).
           d. INSERT the single ``Relationships`` row with
              ``relationship_type='Addresses'``,
              ``source_kind='plan_approval'`` /
              ``source_id=plan_approval_id`` /
              ``source_revision_id=NULL`` (a Plan Approval has no
              revisions),
              ``target_kind='plan_revision'`` /
              ``target_id=target_plan_revision_id`` /
              ``target_revision_id=NULL`` (Plan Revisions live in a
              single Revision-level table with no separate Resource
              header), and ``semantic_role=NULL`` (the ``Addresses``
              Relationship Type is precise for this edge).
           e. Persist the Provenance Manifest via the wired
              :class:`ProvenanceManifestWriter` with
              ``subject_kind='plan_approval'``,
              ``subject_id=plan_approval_id``,
              ``subject_revision_id=None``, and a single Included
              Source of kind ``'plan_revision'`` carrying the target
              Plan Revision Identity and its ``recorded_at``. The
              writer also INSERTs any supplied Omission Entries
              (converted from :class:`PlanApprovalOmissionEntry` to
              the writer's :class:`OmissionEntry` shape) inside the
              same transaction.
           f. When ``outcome == 'Approve'`` and only then, execute
              the one permitted UPDATE against
              ``Plan_Revisions.lifecycle_state`` to ``'approved'``
              (Requirement 9.1). The session pragma set in step (b)
              is the AD-WS-19 trigger's signal to permit this exact
              transition; every other UPDATE on ``Plan_Revisions``
              (different value, different column, or no pragma)
              would be rejected by the trigger. When ``outcome ==
              'Reject_Approval'`` the UPDATE is skipped — the Plan
              Approval row records the rejection but the Plan
              Revision stays in ``'draft'`` (per the AD-WS-20
              persistence flow's distinction between the two
              outcomes).
           g. Append the consequential ``Audit_Records`` row with
              ``action_type='create.plan_approval'``,
              ``target_id=plan_approval_id``, and
              ``target_revision_id=NULL`` inside the same transaction
              (Requirement 9.7 / AD-WS-5).
           h. Clear the session pragma immediately so the trigger
              window is as small as possible — any subsequent UPDATE
              on the same connection (within or outside the
              transaction) is rejected by the trigger from this
              point on. The clear is invoked in a ``try / finally``
              guard so an exception between steps (b) and (h) does
              not leak the pragma into a subsequent statement on the
              same connection.

        Rows are inserted in the AD-WS-20 dependency order so a FK
        failure anywhere rolls back the whole transaction:
        ``Identifier_Registry → Plan_Approval_Records → Relationships
        → Provenance_Manifests → Omission_Entries →
        Plan_Revisions UPDATE → Audit_Records``.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback (Requirement 10.6 /
                AD-WS-9). The same engine is used to open a fresh
                transaction for the authorization evaluation itself
                (Slice 1 single-writer accommodation).
            target_plan_revision_id: Identity of the target Plan
                Revision (Requirement 9.2). Must resolve to an
                existing row in ``Plan_Revisions`` whose
                ``lifecycle_state`` is ``'draft'``.
            outcome: One of ``"Approve"``, ``"Reject_Approval"``
                (Requirement 9.2 / 9.5). On ``'Approve'`` the
                target Plan Revision's lifecycle state transitions
                to ``'approved'``; on ``'Reject_Approval'`` the
                Plan Revision stays in ``'draft'`` and only the
                ``Plan_Approval_Records`` row is written.
            rationale: Approval rationale text of 1..4000 characters
                (Requirement 9.2 / 9.5; note the 4,000-char ceiling,
                distinct from Plan Review's 10,000).
            approving_party_id: Identity of the approving Party.
                Persisted on
                ``Plan_Approval_Records.approving_party_id`` and on
                the consequential audit row's ``actor_party_id``.
                The Slice 1 ``Parties`` foreign key is enforced by
                the database.
            authority_basis: :class:`AuthorityBasisRef` carrying the
                ``type`` (AD-WS-10 set) and ``id`` of the authority
                basis. Destructured into the
                ``authority_basis_type`` and ``authority_basis_id``
                columns of ``Plan_Approval_Records`` (Requirement
                9.2 / AD-WS-22).
            applicable_scope: Scope identifier the Plan Approval
                applies within. Persisted on
                ``Plan_Approval_Records.applicable_scope`` and
                passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            omissions: Optional sequence of
                :class:`PlanApprovalOmissionEntry`. Each entry
                becomes one ``Omission_Entries`` row written by the
                wired :class:`ProvenanceManifestWriter` inside the
                same transaction. Defaults to the empty tuple.
            correlation_id: Optional correlation identifier shared
                by every audit row written in this operation, the
                authorization evaluation row, and the session
                pragma value. A UUIDv7 is generated when omitted.
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
            :class:`CreatePlanApprovalResult` carrying the persisted
            Plan Approval Identity, the target Activity Plan
            Identity, the ``Addresses`` Relationship Identity, the
            Provenance Manifest Identity, the ordered Omission
            Entry Identities, the new lifecycle state, the recorded
            time, and the correlation identifier.

        Raises:
            PlanApprovalValidationError: A required attribute is
                missing or a Requirement 9.2 enumeration / length
                check was violated, or the request body carried a
                prohibited execution / observed-outcome /
                produced-deliverable attribute.
            PlanApprovalTargetNotResolvableError: The target Plan
                Revision Identity did not resolve to an existing
                Plan Revision (Requirement 9.5).
            PlanApprovalTargetNotDraftError: The target Plan
                Revision exists but its ``lifecycle_state`` is not
                ``'draft'`` (Requirement 9.5).
            PlanApprovalConflictError: A Plan Approval already
                targets the same Plan Revision (Requirement 9.5).
            PlanApprovalAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 10.1 / 11.5). The Denial Record was
                appended successfully in a separate transaction
                (AD-WS-9 / Slice 1 Requirement 7.6).
            PlanApprovalAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record
                append failed on every retry (Requirement 10.6).
                Replaces :class:`PlanApprovalAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``approving_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 9.7 /
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
                raise PlanApprovalValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 9.2 / 9.5) before any
        # database read or authorization side-effect. Each validator
        # raises :class:`PlanApprovalValidationError` with a stable
        # ``failed_constraint`` so the HTTP layer (task 15.1) can
        # render structured 400 responses identifying the invalid
        # attribute (Requirement 9.5).
        self._validate_required_strings(
            target_plan_revision_id=target_plan_revision_id,
            approving_party_id=approving_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_outcome(outcome)
        self._validate_rationale(rationale)
        normalized_basis = self._validate_authority_basis(authority_basis)
        omissions_tuple = tuple(omissions)

        # 3. Resolve the target Plan Revision Identity through a
        # single SELECT on ``Plan_Revisions``. Read both the parent
        # Activity Plan Identity (denormalized onto the Plan Approval
        # row per design §"Data Models" — ``target_activity_plan_id``
        # is a FK on ``Plan_Approval_Records``) and the lifecycle
        # state. The lookup runs on the caller's connection so it
        # participates in the caller's transactional view.
        # Requirement 9.5 rejects (a) the unresolvable case and (b)
        # the non-draft case before authorization evaluation so the
        # deny path never reveals whether a Plan Revision exists /
        # is in draft for an unauthorized caller.
        row = connection.execute(
            text(
                "SELECT activity_plan_id, lifecycle_state, recorded_at "
                "FROM Plan_Revisions "
                "WHERE plan_revision_id = :plan_revision_id"
            ),
            {"plan_revision_id": target_plan_revision_id},
        ).one_or_none()
        if row is None:
            raise PlanApprovalTargetNotResolvableError(
                target_plan_revision_id=target_plan_revision_id,
            )
        target_activity_plan_id, target_lifecycle_state, target_recorded_at_iso = (
            row
        )
        if target_lifecycle_state != _LIFECYCLE_DRAFT:
            raise PlanApprovalTargetNotDraftError(
                target_plan_revision_id=target_plan_revision_id,
                lifecycle_state=target_lifecycle_state,
            )

        # 4. Pre-check Requirement 9.5's uniqueness invariant. The
        # UNIQUE(target_plan_revision_id) constraint on
        # ``Plan_Approval_Records`` is the source of truth; the
        # pre-check surfaces a structured ``PlanApprovalConflictError``
        # with the existing Plan Approval Identity instead of a
        # generic IntegrityError. Note: when the lifecycle is
        # ``'draft'`` and a previous Plan Approval already exists,
        # the previous approval must have been a ``Reject_Approval``
        # outcome (an ``Approve`` outcome would have transitioned
        # the lifecycle to ``'approved'`` and the previous step
        # would have rejected the request with
        # :class:`PlanApprovalTargetNotDraftError`).
        existing_plan_approval_id = connection.execute(
            text(
                "SELECT plan_approval_id FROM Plan_Approval_Records "
                "WHERE target_plan_revision_id = :plan_revision_id"
            ),
            {"plan_revision_id": target_plan_revision_id},
        ).scalar_one_or_none()
        if existing_plan_approval_id is not None:
            raise PlanApprovalConflictError(
                target_plan_revision_id=target_plan_revision_id,
                existing_plan_approval_id=existing_plan_approval_id,
            )

        # 5. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Plan_Approval_Records row, the Addresses Relationship row,
        # the Provenance Manifest row, every Omission_Entries row,
        # the lifecycle UPDATE (when executed), and the consequential
        # audit row all share this timestamp; the optional
        # ``evaluation_at`` parameter changes only *when* authority
        # is evaluated *as of*, not the recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 6. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 documented accommodation for SQLite's
        # single-writer model; the deny path opens *another*
        # separate transaction for the Denial Record write, and the
        # caller's transaction stays a reader until the persistence
        # flow below). The authorization target is the target Plan
        # Revision — ``create.plan_approval`` authority is scoped
        # against the Plan Revision the approval is being recorded
        # against. AD-WS-15's mapping of ``create.plan_approval`` to
        # the ``approve`` authority means the wired role assignment
        # must carry ``approve`` in its ``authorities_granted``
        # (Requirement 11.5); ``review`` alone is not sufficient.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=approving_party_id,
                action=_ACTION_CREATE_PLAN_APPROVAL,
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
            self._persist_plan_approval_denial(
                engine=engine,
                actor_party_id=approving_party_id,
                target_activity_plan_id=target_activity_plan_id,
                target_plan_revision_id=target_plan_revision_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise PlanApprovalAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 7. Mint identifiers (AD-WS-2 / AD-WS-3 / AD-WS-19). A Plan
        # Approval is an Immutable Record (Requirement 9.4 — approved
        # Plan Revisions are byte-equivalent forever and so are the
        # Plan Approval Records that closed them) so the registry
        # binding uses ``kind='immutable_record'``. The Addresses
        # Relationship Identity is a separate Relationship identifier.
        plan_approval_id = str(self.identity_service.new_immutable_record_id())
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Plan Approval identifier
        # in ``Identifier_Registry``; the digest is the SHA-256 of
        # the canonical JSON payload of the new record so two
        # different Plan Approvals never collide on the same digest.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_activity_plan_id": target_activity_plan_id,
                    "target_plan_revision_id": target_plan_revision_id,
                    "outcome": outcome,
                    "rationale": rationale,
                    "approving_party_id": approving_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 8. Open the AD-WS-20 persistence flow inside the caller's
        # transaction. The session pragma is set immediately so the
        # AD-WS-19 lifecycle trigger will permit the one
        # draft→approved UPDATE later in the flow; the pragma is
        # cleared in a ``finally`` block so any exception between
        # here and the consequential audit append does not leak the
        # pragma onto a subsequent statement on the same connection.
        # ``set_plan_approval_in_progress`` writes to the
        # connection-private temp table — its insert participates in
        # the caller's transaction and rolls back automatically if
        # the transaction rolls back; the ``finally`` clear is
        # belt-and-braces for the case where the caller has already
        # committed or is using a long-lived connection.
        set_plan_approval_in_progress(connection, correlation)
        try:
            # 9. Register the Plan Approval identifier in
            # ``Identifier_Registry`` carrying its AD-WS-19
            # ``resource_kind='plan_approval'`` tag. The helper
            # delegates to :meth:`IdentityService.reject_if_duplicate`
            # so the Slice 1 identifier-conflict Denial Record
            # pathway fires on any collision; on success the helper
            # INSERTs the row inside the caller's transaction.
            _record_planning_resource(
                connection,
                _REGISTRY_KIND_IMMUTABLE_RECORD,
                _RESOURCE_KIND_PLAN_APPROVAL,
                plan_approval_id,
                content_digest,
                identity_service=self.identity_service,
                actor_party_id=approving_party_id,
                correlation_id=correlation,
                attempted_action=_ACTION_CREATE_PLAN_APPROVAL,
                recorded_time=recorded_time,
            )

            # 10. INSERT the immutable Plan_Approval_Records row.
            # Every Requirement 9.2 attribute lands here.
            # ``authority_basis_type`` and ``authority_basis_id``
            # are destructured from :class:`AuthorityBasisRef` to
            # match the schema columns (AD-WS-22 — the Slice 1
            # enumeration is reused unchanged so the CHECK
            # constraint on ``authority_basis_type`` accepts
            # exactly the AD-WS-10 values).
            connection.execute(
                text(
                    """
                    INSERT INTO Plan_Approval_Records (
                        plan_approval_id, target_activity_plan_id,
                        target_plan_revision_id, outcome, rationale,
                        approving_party_id, authority_basis_type,
                        authority_basis_id, applicable_scope, recorded_at
                    ) VALUES (
                        :plan_approval_id, :target_activity_plan_id,
                        :target_plan_revision_id, :outcome, :rationale,
                        :approving_party_id, :authority_basis_type,
                        :authority_basis_id, :applicable_scope, :recorded_at
                    )
                    """
                ),
                {
                    "plan_approval_id": plan_approval_id,
                    "target_activity_plan_id": target_activity_plan_id,
                    "target_plan_revision_id": target_plan_revision_id,
                    "outcome": outcome,
                    "rationale": rationale,
                    "approving_party_id": approving_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                    "recorded_at": recorded_at,
                },
            )

            # 11. INSERT the single ``Addresses`` Relationship row
            # binding the new Plan Approval to its target Plan
            # Revision (Requirement 9.3). ``source_revision_id`` is
            # NULL because a Plan Approval has no revisions
            # (Immutable Record per AD-WS-4); ``target_revision_id``
            # is NULL because Plan Revisions live in a single
            # Revision-level table with no separate Resource header
            # (matching the convention used by
            # :class:`PlanReviewService` for its ``Relates To``
            # Relationship). ``semantic_role`` is NULL — the
            # ``Addresses`` Relationship Type is precise for this
            # edge and AD-WS-17's ``semantic_role`` discriminator is
            # reserved for ``Relates To`` rows that need to be
            # distinguished from non-Slice-2 ``Relates To`` edges.
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
                    "relationship_id": addresses_relationship_id,
                    "relationship_type": _RELATIONSHIP_TYPE_ADDRESSES,
                    "source_kind": _KIND_PLAN_APPROVAL,
                    "source_id": plan_approval_id,
                    "source_revision_id": None,
                    "target_kind": _KIND_PLAN_REVISION,
                    "target_id": target_plan_revision_id,
                    "target_revision_id": None,
                    "authoring_party_id": approving_party_id,
                    "recorded_at": recorded_at,
                    "semantic_role": None,
                },
            )

            # 12. Persist the Provenance Manifest via the wired
            # :class:`ProvenanceManifestWriter`. The writer also
            # INSERTs each Omission_Entries row inside the same
            # transaction (AD-WS-5 / AD-WS-20). The single material
            # source is the target Plan Revision recorded as kind
            # ``'plan_revision'``; its ``recorded_at`` is the
            # Plan Revision row's own ``recorded_at`` (parsed from
            # the ISO-8601 millisecond string read in step 3) so
            # the Source Freshness Window check (Requirement 10.6)
            # has the actual source-creation time rather than
            # ``now()``.
            manifest_omissions = tuple(
                ManifestOmissionEntry(
                    excluded_source_id=str(entry.excluded_source_id),
                    excluded_source_revision_id=(
                        str(entry.excluded_source_revision_id)
                        if entry.excluded_source_revision_id is not None
                        else None
                    ),
                    category=entry.category,
                    rationale=entry.rationale,
                )
                for entry in omissions_tuple
            )
            manifest_result = self.manifest_writer.write_manifest(
                connection,
                subject_kind=_MANIFEST_SUBJECT_KIND_PLAN_APPROVAL,
                subject_id=plan_approval_id,
                subject_revision_id=None,
                authoring_party_id=approving_party_id,
                included_sources=(
                    IncludedSource(
                        kind=_KIND_PLAN_REVISION,
                        resource_id=target_plan_revision_id,
                        revision_id=None,
                        recorded_at=_parse_iso8601_ms(target_recorded_at_iso),
                    ),
                ),
                omissions=manifest_omissions,
                recorded_at=recorded_time,
            )
            manifest_id = manifest_result.manifest_id
            omission_entry_ids = tuple(manifest_result.omission_entry_ids)

            # 13. On outcome ``'Approve'`` and only then, execute the
            # one permitted UPDATE against
            # ``Plan_Revisions.lifecycle_state`` to ``'approved'``
            # (Requirement 9.1). The session pragma set in step 8
            # is the AD-WS-19 trigger's signal to permit this exact
            # transition. The trigger checks (a) the pragma is set,
            # (b) only the ``lifecycle_state`` column is being
            # changed, and (c) the change is exactly
            # ``'draft' → 'approved'``; any other UPDATE is rejected
            # with an ABORT.
            #
            # On outcome ``'Reject_Approval'`` the UPDATE is skipped
            # entirely — the Plan Approval row records the rejection
            # but the Plan Revision stays in ``'draft'``. The
            # ``Plan_Approval_Records`` UNIQUE constraint then keeps
            # this Plan Revision from receiving a second Plan
            # Approval (Requirement 9.5).
            if outcome == _OUTCOME_APPROVE:
                connection.execute(
                    text(
                        "UPDATE Plan_Revisions "
                        "SET lifecycle_state = :new_state "
                        "WHERE plan_revision_id = :plan_revision_id"
                    ),
                    {
                        "new_state": _LIFECYCLE_APPROVED,
                        "plan_revision_id": target_plan_revision_id,
                    },
                )
                new_lifecycle_state = _LIFECYCLE_APPROVED
            else:
                new_lifecycle_state = _LIFECYCLE_DRAFT

            # 14. Append the consequential audit row (Requirement
            # 9.7 / AD-WS-5). Participates in the caller's
            # transaction so a failure here rolls back the registry,
            # ``Plan_Approval_Records``, ``Relationships``,
            # ``Provenance_Manifests``, ``Omission_Entries``, and
            # (when executed) the lifecycle UPDATE together.
            # ``target_id`` is the Plan Approval Immutable Record
            # Identity; ``target_revision_id`` is NULL because a
            # Plan Approval has no Revision Identity
            # (AD-WS-3, AD-WS-4 — matching the audit-row convention
            # used by :class:`KnowledgeService` for Decisions).
            self.audit_log.append_consequential(
                connection,
                actor_party_id=approving_party_id,
                action_type=_ACTION_CREATE_PLAN_APPROVAL,
                target_id=plan_approval_id,
                target_revision_id=None,
                correlation_id=correlation,
                recorded_time=recorded_time,
            )
        finally:
            # 15. Clear the session pragma immediately so the
            # AD-WS-19 trigger window is as small as possible. Any
            # subsequent UPDATE on ``Plan_Revisions`` on the same
            # connection (within or outside the surrounding
            # transaction) is rejected by the trigger from this
            # point on. The clear runs in a ``finally`` so it
            # executes whether the persistence flow succeeded or
            # raised — the pragma must not survive past the Plan
            # Approval transaction. (If the surrounding transaction
            # is rolled back by the caller, the pragma DELETE rolls
            # back with it; the trigger checks the current pragma
            # value on every UPDATE attempt so the temporary
            # presence during a rolled-back transaction is harmless.)
            clear_plan_approval_in_progress(connection)

        return CreatePlanApprovalResult(
            plan_approval_id=plan_approval_id,
            target_activity_plan_id=target_activity_plan_id,
            target_plan_revision_id=target_plan_revision_id,
            outcome=outcome,
            rationale=rationale,
            approving_party_id=approving_party_id,
            authority_basis=normalized_basis,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            manifest_id=manifest_id,
            omission_entry_ids=omission_entry_ids,
            new_lifecycle_state=new_lifecycle_state,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )


    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_required_strings(
        *,
        target_plan_revision_id: Any,
        approving_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 9.5: a Plan Approval submission that omits the
        target Plan Revision Identity or the applicable scope is
        rejected. This validator additionally covers
        ``approving_party_id`` (which Requirement 10.1 implicitly
        requires — an unauthenticated request has no Party Identity
        to authorize against).
        """
        if not target_plan_revision_id or not isinstance(
            target_plan_revision_id, str
        ):
            raise PlanApprovalValidationError(
                "target_plan_revision_id is required; Requirement 9.5 "
                "rejects Plan Approvals missing the target Plan Revision "
                "Identity.",
                failed_constraint="target_plan_revision_id_missing",
            )
        if not approving_party_id or not isinstance(approving_party_id, str):
            raise PlanApprovalValidationError(
                "approving_party_id is required; Requirement 10.1 rejects "
                "unauthenticated Plan Approval creation.",
                failed_constraint="approving_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise PlanApprovalValidationError(
                "applicable_scope is required; Requirement 9.5 rejects "
                "Plan Approvals missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_outcome(outcome: Any) -> None:
        """Reject outcome outside the Requirement 9.2 enumeration.

        Per Requirement 9.2 the outcome is drawn from the enumerated
        set ``{Approve, Reject_Approval}``; Requirement 9.5 specifies
        the rejection path with the ``outcome_out_of_set`` constraint
        name. The same set is enforced by the
        ``Plan_Approval_Records.outcome`` CHECK constraint; this
        validator surfaces a precise error before the SQL layer.
        """
        if outcome is None or not isinstance(outcome, str) or outcome == "":
            raise PlanApprovalValidationError(
                "outcome is required and must be one of "
                f"{sorted(_VALID_OUTCOMES)}; Requirement 9.2 / 9.5.",
                failed_constraint="outcome_missing",
            )
        if outcome not in _VALID_OUTCOMES:
            raise PlanApprovalValidationError(
                f"outcome {outcome!r} is not in the Requirement 9.2 "
                f"enumeration {sorted(_VALID_OUTCOMES)}.",
                failed_constraint="outcome_out_of_set",
            )

    @staticmethod
    def _validate_rationale(rationale: Any) -> None:
        """Reject rationale outside the Requirement 9.2 range.

        Per Requirement 9.2 the rationale is 1..4000 characters and
        required (note: distinct from Plan Review's 1..10000 ceiling).
        Empty or non-string rationales surface as
        ``rationale_missing`` since the actionable next step is the
        same in both cases (supply a non-empty string). An over-long
        rationale surfaces as ``rationale_too_long``.
        """
        if (
            rationale is None
            or not isinstance(rationale, str)
            or rationale == ""
        ):
            raise PlanApprovalValidationError(
                "rationale is required and must be a non-empty string of "
                f"{_RATIONALE_MIN_CHARS}..{_RATIONALE_MAX_CHARS} "
                "characters; Requirement 9.2 / 9.5.",
                failed_constraint="rationale_missing",
            )
        if len(rationale) > _RATIONALE_MAX_CHARS:
            raise PlanApprovalValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_RATIONALE_MAX_CHARS}-character limit imposed by "
                "Requirement 9.2.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 9.2 / AD-WS-22: the authority basis is drawn
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
          :class:`PlanApprovalValidationError` with the appropriate
          ``failed_constraint``.
        - Any other shape is rejected as
          ``"authority_basis_missing"``.

        Returns:
            The validated :class:`AuthorityBasisRef`. Always a fresh
            instance (or the original, when the input was already
            one) — never ``None``.

        Raises:
            PlanApprovalValidationError: The authority basis is
                missing, malformed, has an out-of-set ``type``, or
                is missing its ``id``.
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            # The Pydantic Literal already constrains type to the
            # AD-WS-10 set, and Pydantic UUID validation ensures id
            # is well-formed. Return as-is.
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise PlanApprovalValidationError(
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
            raise PlanApprovalValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise PlanApprovalValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (
            isinstance(basis_id, str) and basis_id == ""
        ):
            raise PlanApprovalValidationError(
                "authority_basis.id is required per Requirement 9.2.",
                failed_constraint="authority_basis_id_missing",
            )

        # Delegate canonical-form validation (UUID shape) to Pydantic
        # by constructing the typed model. Any malformed value will
        # raise a Pydantic ValidationError which we surface as a
        # PlanApprovalValidationError with the same constraint name
        # as the manual checks above for the closest applicable
        # failure.
        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise PlanApprovalValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    # -- denial side-channel ----------------------------------------------

    def _persist_plan_approval_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_activity_plan_id: str,
        target_plan_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Plan Approval attempt.

        Implements the AD-WS-9 / Slice 1 Requirement 7.6 retry
        contract verbatim (mirroring
        :meth:`walking_slice.planning.plan_revisions.PlanRevisionService._persist_plan_revision_denial`
        and :meth:`walking_slice.knowledge.KnowledgeService._persist_decision_denial`):
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
          :class:`PlanApprovalAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_plan_approval` raises
        :class:`PlanApprovalAuthorizationError` (or this method
        raises :class:`PlanApprovalAuditFailureError`). The Denial
        Record must therefore live outside that scope to survive
        (AD-WS-9 / Requirement 10.6). The denial row's ``target_id``
        is the target Activity Plan identifier rather than the
        (not-yet minted) Plan Approval identifier; this matches the
        AD-WS-9 contract that denial rows reference the resolved
        Resource the unauthorized action was attempted against, and
        Requirement 10.2 specifically calls out the target Activity
        Plan Resource Identity and the target Plan Revision Identity
        as the denial row's correlating identifiers.

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
                        attempted_action=_ACTION_CREATE_PLAN_APPROVAL,
                        target_id=target_activity_plan_id,
                        target_revision_id=target_plan_revision_id,
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

        raise PlanApprovalAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id, SHA-256, and ISO-8601 parsing
# helpers so this module does not import private names from sibling
# planning modules. The correlation-id and digest helpers are
# intentionally identical to their Plan Reviews / Plan Revisions
# siblings: correlation identifiers are non-domain values and the
# digest is opaque to :class:`Identifier_Registry` — both
# implementations could be replaced with shared utility functions in
# a future refactor without changing observable behavior.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, the consequential
    audit row, and the session-pragma value produced for the same
    logical Plan Approval creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Plan Approval
    identifier in ``Identifier_Registry``. A Plan Approval is an
    Immutable Record so this digest is bound exactly once per Plan
    Approval creation — matching the convention used by
    :class:`KnowledgeService.create_decision` for the analogous
    Decision Immutable Record.
    """
    return hashlib.sha256(content).hexdigest()


def _parse_iso8601_ms(value: str) -> datetime:
    """Parse an ISO-8601 millisecond-precision timestamp into UTC.

    The Plan Revision row's ``recorded_at`` is stored as a string in
    the format produced by :func:`walking_slice.audit.format_iso8601_ms`
    (``YYYY-MM-DDTHH:MM:SS.fffZ``). The Provenance Manifest writer
    accepts an :class:`Included Source`'s ``recorded_at`` as a
    timezone-aware UTC :class:`datetime.datetime`, so this helper
    inverts the formatter. The ``Z`` suffix is replaced with
    ``+00:00`` before delegating to :func:`datetime.fromisoformat`.
    """
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)
