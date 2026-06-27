"""Planning_Service.PlanRevisions — immutable Draft Plan Revisions of an
Activity Plan, with optional ``Supersedes`` Relationship to a predecessor
Draft Plan Revision.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.PlanRevisions" — public dataclass surface, authority
  string (``create.plan_revision`` → ``modify`` per AD-WS-15), AD-WS-9
  separate-transaction Denial Record on deny, and the predecessor
  resolution / lifecycle-state contract (Requirement 7.4 — predecessors
  must exist, must belong to the same Activity Plan, and must
  themselves be ``draft``).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with ``kind = 'revision'`` and
  ``resource_kind = 'plan_revision'``); Authorization (the action
  string ``create.plan_revision`` maps to the ``modify`` authority per
  AD-WS-15; the deny path uses the Slice 1 separate-transaction
  Denial-Record pattern reproduced from
  :class:`walking_slice.planning.activity_plans.ActivityPlanService`).
- AD-WS-15 — additive ``modify`` mapping for ``create.plan_revision``.
- AD-WS-18 — Plan Revision lifecycle states are exactly
  ``{draft, approved}`` for this slice. ``create_plan_revision`` only
  ever produces ``draft``; the ``draft → approved`` transition is
  reserved for :class:`PlanApprovalService` (task 11) and is gated by
  the connection-scoped session pragma the AD-WS-19 trigger watches.
- AD-WS-19 — additive ``Identifier_Registry.resource_kind`` column the
  helper :func:`walking_slice.planning._helpers._record_planning_resource`
  populates with the value ``'plan_revision'``.

Plan Revisions are SPECIAL within Slice 2: there is no separate
Resource header table. The Plan Revision row IS the resource — every
Plan Revision of an Activity Plan lives directly in ``Plan_Revisions``
and references its parent through the ``activity_plan_id`` foreign
key (design §"Data Models — Schema Additions"). The
``predecessor_revision_id`` column models the previous Draft Plan
Revision the new one supersedes; when supplied, an additional
``Supersedes`` ``Relationships`` row is INSERTed so the Slice 1
backlink algorithm can return the supersession edge from both
endpoints.

Task scope (task 9.1)
=====================

This module implements :meth:`PlanRevisionService.create_plan_revision`:

1. Validate request inputs per Requirement 7.2 (planned_scope 1..10000,
   deliverable_expectation_refs 0..50 entries, planning_assumptions
   0..100 entries each 1..2000 chars, ordering_rationale 0..2000) and
   Requirement 7.4 (rejection of missing target Activity Plan,
   unresolvable Deliverable Expectation reference, unresolvable
   predecessor, predecessor on a different Activity Plan,
   approved-predecessor, missing planned scope, missing applicable
   scope).
2. Defensively reject any prohibited execution / observed-outcome /
   produced-deliverable attribute via
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   (Property 22) when the route layer forwards the raw request body.
3. Resolve the target Activity Plan Resource Identity through a
   single SELECT against the Slice 2 ``Activity_Plans`` table; reject
   when unresolvable (Requirement 7.4).
4. Resolve every Deliverable Expectation reference through a single
   parameterized SELECT against ``Deliverable_Expectations``; reject
   the request with the first unresolvable identifier and its index
   (Requirement 7.2 / 7.4).
5. When ``predecessor_plan_revision_id`` is supplied, resolve it
   against ``Plan_Revisions`` and verify both ``activity_plan_id``
   matches the new Plan Revision's target Activity Plan AND
   ``lifecycle_state`` is ``'draft'`` (Requirement 7.4 — approved
   predecessors rejected).
6. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (the Slice 1 single-writer accommodation); on a deny outcome,
   persist a Denial Record in another separate transaction with the
   Requirement 7.6 three-attempt exponential-backoff retry pattern,
   and raise :class:`PlanRevisionAuthorizationError` carrying the
   AD-WS-9 denial response fields (``reason_code``,
   ``correlation_id``).
7. On a permit outcome, mint the Plan Revision Identity, register it
   in ``Identifier_Registry`` with the additive ``resource_kind`` tag
   ``'plan_revision'`` (AD-WS-19), INSERT the ``Plan_Revisions`` row
   with ``lifecycle_state = 'draft'``, INSERT the single
   ``Supersedes`` ``Relationships`` row when a predecessor was
   supplied (source_kind='plan_revision' / target_kind='plan_revision'
   per the task brief), and append the consequential ``Audit_Records``
   row (Requirement 7.6 / AD-WS-5) — all inside the caller's
   transaction so a failure anywhere rolls every row back.

Requirements satisfied
======================

    7.1 — authorized Plan Revision creation produces exactly one
          immutable Plan Revision within nominal latency.
    7.2 — every Plan Revision records the parent Activity Plan
          Resource Identity, planned scope (1..10000), 0..50
          Deliverable Expectation references each of which resolves,
          0..100 planning-assumption entries each 1..2000 chars, the
          optional ordering rationale 0..2000 chars, the authoring
          Party Identity, the applicable scope, the recorded time in
          UTC with millisecond precision, the lifecycle state literal
          ``'draft'``, and an optional predecessor Plan Revision
          Identity.
    7.3 — when a Draft Plan Revision is named as predecessor, a
          ``Supersedes`` ``Relationships`` row is INSERTed binding the
          new Plan Revision to the predecessor, leaving the
          predecessor row byte-equivalent.
    7.4 — unresolvable target Activity Plan, unresolvable Deliverable
          Expectation reference, unresolvable predecessor, predecessor
          on a different Activity Plan, approved-predecessor, missing
          planned scope, and missing applicable scope are rejected
          with no Plan Revision row created and a structured error
          identifying the invalid attribute (the first offending
          attribute in source order).
    7.5 — unauthorized requests are denied via
          :class:`AuthorizationService`; the Planning_Service
          declines to create any Plan Revision and the Audit_Log
          appends a Denial Record conforming to AD-WS-9.
    7.6 — the Denial Record append is retried up to three times with
          exponential backoff (0.01s, 0.02s, 0.04s); on total audit
          failure :class:`PlanRevisionAuditFailureError` is raised so
          denial and audit cannot silently diverge. The consequential
          audit row participates in the caller's transaction so the
          creation row and its audit row are byte-equivalent.
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

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


__all__ = [
    "CreatePlanRevisionResult",
    "PlanRevisionActivityPlanNotResolvableError",
    "PlanRevisionAuditFailureError",
    "PlanRevisionAuthorizationError",
    "PlanRevisionDeliverableExpectationNotResolvableError",
    "PlanRevisionPredecessorApprovedError",
    "PlanRevisionPredecessorActivityPlanMismatchError",
    "PlanRevisionPredecessorNotResolvableError",
    "PlanRevisionRow",
    "PlanRevisionService",
    "PlanRevisionValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings and Relationship kind strings are pulled out as
# module-level ``Final`` so the names that downstream property tests
# look for in ``Audit_Records.action_type`` and ``Relationships`` are
# textually stable and the strings stay aligned with the
# :mod:`walking_slice.planning._persistence` schema and the AD-WS-15
# authority mapping in :mod:`walking_slice.authorization`.
# ---------------------------------------------------------------------------


# ``create.plan_revision`` maps to the ``modify`` authority per
# AD-WS-15. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 7.6) and on the
# separate-transaction Denial Record appended by
# :meth:`PlanRevisionService._persist_plan_revision_denial` so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_PLAN_REVISION: Final[str] = "create.plan_revision"

# Relationship Type and source / target ``kind`` strings written to
# the optional ``Supersedes`` Relationship row inserted alongside a
# new Plan Revision when a predecessor is supplied. Constants ensure
# the strings cannot drift between this module, the Provenance
# Navigator backlink algorithm that consumes ``Relationships`` rows
# verbatim, and the planning provenance walk that descends Plan
# Approval → Plan Revision → Activity Plan → Project → Objective
# (design §"Planning_Service.PlanApprovals").
_RELATIONSHIP_TYPE_SUPERSEDES: Final[str] = "Supersedes"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"
_KIND_ACTIVITY_PLAN: Final[str] = "activity_plan"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tag (Slice 2 additive
# enumeration per AD-WS-19). Plan Revisions are Revision-level by
# construction — a Plan Revision *is* a Revision of an Activity Plan,
# so there is no separate Resource-header binding. The
# ``'plan_revision'`` tag is the row-level discriminator that keeps
# the Plan Revision identifier set inspectably disjoint from every
# other Slice 2 resource_kind in ``Identifier_Registry``
# (Requirement 4.5 generalized).
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_PLAN_REVISION: Final[str] = "plan_revision"

# Lifecycle state literal written to every new Plan Revision row. The
# ``draft → approved`` transition is the responsibility of
# :class:`PlanApprovalService` (task 11) and is gated by the AD-WS-19
# / AD-WS-20 session pragma; this module never writes
# ``'approved'``.
_LIFECYCLE_DRAFT: Final[str] = "draft"

# Validation limits per Requirement 7.2 (Plan Revision contents).
# The schema CHECK constraints on ``Plan_Revisions`` enforce the same
# values; centralizing them here surfaces precise, structured
# constraint names through :class:`PlanRevisionValidationError`.
_PLANNED_SCOPE_MIN_CHARS: Final[int] = 1
_PLANNED_SCOPE_MAX_CHARS: Final[int] = 10_000
_DELIVERABLE_REFS_MIN_COUNT: Final[int] = 0
_DELIVERABLE_REFS_MAX_COUNT: Final[int] = 50
_PLANNING_ASSUMPTIONS_MIN_COUNT: Final[int] = 0
_PLANNING_ASSUMPTIONS_MAX_COUNT: Final[int] = 100
_ASSUMPTION_MIN_CHARS: Final[int] = 1
_ASSUMPTION_MAX_CHARS: Final[int] = 2_000
_ORDERING_RATIONALE_MIN_CHARS: Final[int] = 0
_ORDERING_RATIONALE_MAX_CHARS: Final[int] = 2_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 7.6). Three retries after the
# initial attempt for a total of four attempts. The sequence is
# byte-equivalent to the one in sibling Planning_Service modules so
# every Planning_Service module presents identical denial-side
# timing (which Property 18 — Indistinguishable denial — relies on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)



# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class PlanRevisionValidationError(ValueError):
    """Raised when a Plan Revision submission fails Requirement 7.2 / 7.4 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"planned_scope_missing"`` (empty / non-string planned scope),
            ``"planned_scope_too_long"``,
            ``"target_activity_plan_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"deliverable_expectation_refs_invalid_type"`` (not a list / tuple),
            ``"deliverable_expectation_refs_too_many"`` (> 50 entries),
            ``"deliverable_expectation_ref_invalid_type"`` (entry is not
                a non-empty string — see :attr:`invalid_index`),
            ``"deliverable_expectation_refs_duplicate"`` (the same
                identifier appears twice — see :attr:`invalid_index`),
            ``"planning_assumptions_invalid_type"`` (not a list / tuple),
            ``"planning_assumptions_too_many"`` (> 100 entries),
            ``"planning_assumption_invalid_type"`` (entry is not a
                non-empty string — see :attr:`invalid_index`),
            ``"planning_assumption_too_long"`` (entry exceeds 2000
                characters — see :attr:`invalid_index`),
            ``"ordering_rationale_invalid_type"`` (not str / None),
            ``"ordering_rationale_too_long"``,
            ``"predecessor_plan_revision_id_invalid_type"`` (non-string
                value while not None),
            ``"prohibited_attribute"`` (the request body carried at
                least one execution / observed-outcome /
                produced-deliverable attribute — see
                :attr:`prohibited_keys`).
        prohibited_keys: Populated only when ``failed_constraint`` is
            ``"prohibited_attribute"``; lists every offending top-level
            key in the original order from the request body. Empty
            tuple in every other case.
        invalid_index: Populated for the
            ``"deliverable_expectation_ref_*"`` and
            ``"planning_assumption_*"`` per-entry constraints; carries
            the zero-based index of the first offending entry. ``-1``
            when not applicable.
    """

    def __init__(
        self,
        message: str,
        *,
        failed_constraint: str,
        prohibited_keys: tuple[str, ...] = (),
        invalid_index: int = -1,
    ) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint
        self.prohibited_keys = prohibited_keys
        self.invalid_index = invalid_index


class PlanRevisionActivityPlanNotResolvableError(LookupError):
    """Raised when the target Activity Plan Identity does not resolve.

    Requirement 7.4 requires the target Activity Plan Resource Identity
    to resolve to an existing Activity Plan Resource. The check runs
    before authorization evaluation so the deny path never reveals
    whether an Activity Plan exists for an unauthorized caller.

    Attributes:
        target_activity_plan_id: The Activity Plan Identity the caller
            supplied.
        failed_constraint: ``"target_activity_plan_not_resolvable"`` when
            no ``Activity_Plans`` row matched the identifier.
    """

    def __init__(
        self,
        *,
        target_activity_plan_id: str,
        failed_constraint: str = "target_activity_plan_not_resolvable",
    ) -> None:
        super().__init__(
            f"Target Activity Plan {target_activity_plan_id!r} did not "
            f"resolve to an existing Activity Plan Resource "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_activity_plan_id = target_activity_plan_id
        self.failed_constraint = failed_constraint


class PlanRevisionDeliverableExpectationNotResolvableError(LookupError):
    """Raised when a Deliverable Expectation reference does not resolve.

    Requirement 7.2 requires each of the 0..50 Deliverable Expectation
    references on a Plan Revision to resolve to an existing
    Deliverable Expectation Resource at creation time. Requirement
    7.4 specifies the rejection path. The check runs before
    authorization evaluation so the deny path never reveals whether a
    Deliverable Expectation exists for an unauthorized caller.

    Attributes:
        deliverable_expectation_id: The first Deliverable Expectation
            Identity that did not resolve.
        invalid_index: Zero-based index of the offending entry in the
            input sequence so the route layer can identify the exact
            element to the client.
        failed_constraint: ``"deliverable_expectation_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        deliverable_expectation_id: str,
        invalid_index: int,
        failed_constraint: str = "deliverable_expectation_not_resolvable",
    ) -> None:
        super().__init__(
            f"Deliverable Expectation {deliverable_expectation_id!r} at "
            f"index {invalid_index} did not resolve to an existing "
            f"Deliverable Expectation Resource "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.deliverable_expectation_id = deliverable_expectation_id
        self.invalid_index = invalid_index
        self.failed_constraint = failed_constraint


class PlanRevisionPredecessorNotResolvableError(LookupError):
    """Raised when ``predecessor_plan_revision_id`` does not resolve.

    Requirement 7.4 requires the predecessor Plan Revision Identity
    (when supplied) to resolve to an existing Plan Revision. The
    check runs before authorization evaluation so the deny path never
    reveals whether a Plan Revision exists for an unauthorized
    caller.

    Attributes:
        predecessor_plan_revision_id: The predecessor Identity the
            caller supplied.
        failed_constraint: ``"predecessor_plan_revision_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        predecessor_plan_revision_id: str,
        failed_constraint: str = "predecessor_plan_revision_not_resolvable",
    ) -> None:
        super().__init__(
            f"Predecessor Plan Revision {predecessor_plan_revision_id!r} did "
            f"not resolve to an existing Plan Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.predecessor_plan_revision_id = predecessor_plan_revision_id
        self.failed_constraint = failed_constraint


class PlanRevisionPredecessorActivityPlanMismatchError(LookupError):
    """Raised when the predecessor belongs to a different Activity Plan.

    Requirement 7.4 requires the predecessor Plan Revision (when
    supplied) to be a Plan Revision *of the same Activity Plan*. A
    predecessor on a different Activity Plan is rejected with no
    Plan Revision created.

    Attributes:
        predecessor_plan_revision_id: The predecessor Identity the
            caller supplied.
        predecessor_activity_plan_id: The Activity Plan Identity the
            predecessor actually belongs to.
        target_activity_plan_id: The Activity Plan Identity the new
            Plan Revision targets.
        failed_constraint: ``"predecessor_activity_plan_mismatch"``.
    """

    def __init__(
        self,
        *,
        predecessor_plan_revision_id: str,
        predecessor_activity_plan_id: str,
        target_activity_plan_id: str,
        failed_constraint: str = "predecessor_activity_plan_mismatch",
    ) -> None:
        super().__init__(
            f"Predecessor Plan Revision {predecessor_plan_revision_id!r} "
            f"belongs to Activity Plan {predecessor_activity_plan_id!r} "
            f"but the new Plan Revision targets Activity Plan "
            f"{target_activity_plan_id!r} "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.predecessor_plan_revision_id = predecessor_plan_revision_id
        self.predecessor_activity_plan_id = predecessor_activity_plan_id
        self.target_activity_plan_id = target_activity_plan_id
        self.failed_constraint = failed_constraint


class PlanRevisionPredecessorApprovedError(LookupError):
    """Raised when the predecessor Plan Revision is already approved.

    Requirement 7.4 forbids naming an Approved Plan Revision as the
    predecessor of a new Plan Revision (the supersession edge is
    only meaningful between Draft Plan Revisions; the ``Supersedes``
    Relationship is the historical record of an unapproved superseded
    plan being replaced by another unapproved plan before approval).
    An attempt to name an Approved predecessor is rejected with no
    Plan Revision created and is the discriminator that distinguishes
    the Supersedes path from the Plan Approval path.

    Attributes:
        predecessor_plan_revision_id: The Approved predecessor
            Identity the caller supplied.
        predecessor_lifecycle_state: The lifecycle state observed on
            the predecessor row (always ``'approved'`` for this
            error; carried verbatim for debugging).
        failed_constraint: ``"predecessor_already_approved"``.
    """

    def __init__(
        self,
        *,
        predecessor_plan_revision_id: str,
        predecessor_lifecycle_state: str,
        failed_constraint: str = "predecessor_already_approved",
    ) -> None:
        super().__init__(
            f"Predecessor Plan Revision {predecessor_plan_revision_id!r} has "
            f"lifecycle_state={predecessor_lifecycle_state!r}; Requirement "
            "7.4 rejects naming an Approved Plan Revision as the "
            "predecessor of a new Plan Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.predecessor_plan_revision_id = predecessor_plan_revision_id
        self.predecessor_lifecycle_state = predecessor_lifecycle_state
        self.failed_constraint = failed_constraint


class PlanRevisionAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Plan Revision attempt.

    Mirrors
    :class:`walking_slice.planning.activity_plans.ActivityPlanAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 7.5 / 10.x). The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 10 forbids leaking authorized Party identities, target
    existence, or role assignment details beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Plan Revision creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class PlanRevisionAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 7.6).

    Mirrors
    :class:`walking_slice.planning.activity_plans.ActivityPlanAuditFailureError`.
    On total audit-append failure the exception is raised *in place
    of* :class:`PlanRevisionAuthorizationError` — denial and audit
    have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no ``Plan_Revisions``
    row, ``Supersedes`` Relationship, or consequential audit row is
    persisted.

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
            f"Denial Record append for a denied Plan Revision failed after "
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
class CreatePlanRevisionResult:
    """Result of :meth:`PlanRevisionService.create_plan_revision`.

    Returned so callers (the HTTP layer in task 15.1, tests, the
    downstream Plan Review service that targets this Plan Revision,
    and the downstream Plan Approval service that transitions its
    lifecycle) can correlate the created Plan Revision with its
    optional ``Supersedes`` Relationship and the consequential audit
    row in one round-trip.

    Attributes:
        plan_revision_id: The Plan Revision Identity (UUIDv7).
        target_activity_plan_id: The parent Activity Plan Identity;
            copied byte-equivalent from the request input.
        lifecycle_state: Always the literal ``"draft"`` — every new
            Plan Revision starts as ``draft`` (AD-WS-18). The
            ``draft → approved`` transition is the responsibility of
            :class:`PlanApprovalService` (task 11).
        planned_scope: The persisted planned scope (1..10000 chars).
        deliverable_expectation_refs: Tuple of the resolved Deliverable
            Expectation Identities in the same order the caller
            supplied them. Stored as the canonical JSON encoding in
            ``Plan_Revisions.deliverable_expectation_refs_json``.
        planning_assumptions: Tuple of the persisted planning
            assumptions in the same order the caller supplied them.
            Stored as the canonical JSON encoding in
            ``Plan_Revisions.planning_assumptions_json``.
        ordering_rationale: The persisted ordering rationale or
            ``None`` when omitted.
        predecessor_plan_revision_id: The predecessor Plan Revision
            Identity, or ``None`` when no predecessor was supplied.
        supersedes_relationship_id: Identity of the single
            ``Supersedes`` ``Relationships`` row inserted alongside
            the Plan Revision when a predecessor was supplied; ``None``
            otherwise.
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Plan Revision applies
            within.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Plan_Revisions`` row, the optional
            ``Supersedes`` Relationship row, and the consequential
            audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    plan_revision_id: str
    target_activity_plan_id: str
    lifecycle_state: str
    planned_scope: str
    deliverable_expectation_refs: tuple[str, ...]
    planning_assumptions: tuple[str, ...]
    ordering_rationale: Optional[str]
    predecessor_plan_revision_id: Optional[str]
    supersedes_relationship_id: Optional[str]
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str
    correlation_id: str



# ---------------------------------------------------------------------------
# Read-only value object (Slice 3 — AD-WS-30).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRevisionRow:
    """Read-only snapshot of a ``Plan_Revisions`` row.

    Returned by :meth:`PlanRevisionService.get_plan_revision`, the Slice 3
    additive Planning_Service read API introduced by AD-WS-30. The object
    carries the four columns the Execution_Service needs to enforce
    Requirement 23.2 (lifecycle state at the recorded time must be
    ``'approved'``) and Requirement 23.4 (the target Plan Revision must
    resolve and its applicable scope must be within the requesting Party's
    Assignment Authority scope) without exposing the full set of
    Requirement 7.2 columns or any write surface.

    Frozen because — like every Slice 2 / Slice 3 value object that crosses
    a module boundary — the receiver must be able to rely on the bytes not
    changing while the in-flight transaction completes. A
    :func:`dataclasses.dataclass(frozen=True)` is used (rather than a
    Pydantic model) for two reasons: ``PlanRevisionRow`` only ever flows
    *outward* from the read API (no untrusted-input validation surface is
    needed), and the sibling result type :class:`CreatePlanRevisionResult`
    in this module is itself a frozen dataclass — the two read/write result
    shapes therefore use the same conventions.

    The function the row represents does not introduce any write path
    (per task 2.1). Every field corresponds verbatim to a column read by
    the single indexed SELECT defined in AD-WS-30.

    Attributes:
        plan_revision_id: The Plan Revision Identity (UUIDv7). Echoes the
            request input on a successful lookup so callers can correlate
            the lookup with the requested identifier without re-binding it
            out of band.
        lifecycle_state: One of ``'draft'`` or ``'approved'`` (AD-WS-18).
            Slice 3's Work Assignment Service compares this against the
            literal ``'approved'`` to satisfy Requirement 23.2.
        activity_plan_id: The parent Activity Plan Resource Identity the
            Plan Revision belongs to (Requirement 7.2). Slice 3's
            ``ProjectResolver`` (task 2.2) follows this identifier to walk
            Activity Plan → Project for the Requirement 27.3
            project-membership check.
        applicable_scope: The scope identifier the Plan Revision applies
            within (Requirement 7.2). The Work Assignment Service uses this
            value to verify the requesting Party's Assignment Authority
            scope covers the Plan Revision per Requirement 23.4.
    """

    plan_revision_id: str
    lifecycle_state: str
    activity_plan_id: str
    applicable_scope: str



# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRevisionService:
    """Persist immutable Draft Plan Revisions and optional ``Supersedes``
    Relationships to their predecessor Draft Plan Revisions.

    Like :class:`walking_slice.planning.activity_plans.ActivityPlanService`,
    this service is connection-scoped at call time:
    :meth:`create_plan_revision` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (AD-WS-5). The service instance therefore
    holds only the cross-request collaborators and can be shared
    across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.PlanRevisions" declares it
    ``@dataclass(frozen=True)`` — Slice 2 service instances are
    immutable container objects that bundle the Slice 1 collaborators
    for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Plan_Revisions``, optional ``Relationships``, and
            ``Audit_Records`` rows. The clock is consulted exactly
            once per write so every artifact of the transaction
            shares one timestamp.
        identity_service: Generates the Plan Revision Identity and
            (when a predecessor is supplied) the ``Supersedes``
            Relationship Identity, plus persists their
            ``Identifier_Registry`` binding (the Plan Revision
            binding carries the Slice 2
            ``resource_kind = 'plan_revision'`` tag per AD-WS-19).
        audit_log: Appends the consequential audit row (Requirement
            7.6) inside the caller's transaction.
        authorization_service: Evaluates ``create.plan_revision``
            authority per AD-WS-15 / Requirement 7.5; the deny path
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

    def create_plan_revision(
        self,
        connection: Connection,
        *,
        target_activity_plan_id: str,
        planned_scope: str,
        deliverable_expectation_refs: Sequence[str] = (),
        planning_assumptions: Sequence[str] = (),
        ordering_rationale: Optional[str] = None,
        predecessor_plan_revision_id: Optional[str] = None,
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreatePlanRevisionResult:
        """Create an immutable Draft Plan Revision and, when a
        predecessor is supplied, the ``Supersedes`` Relationship that
        records the supersession edge.

        Per Requirements 7.1 through 7.6, AD-WS-9 (indistinguishable
        denial), AD-WS-15 (``create.plan_revision`` → ``modify``),
        AD-WS-18 (lifecycle states ``{draft, approved}``), and AD-WS-19
        (resource_kind tagged identifiers + append-only Slice 2
        tables):

        1. Optionally screen the original request body against the
           prohibited-attribute prefixes (Property 22 / Requirements
           12.1, 12.2, 13.1, 13.2, 13.5).
        2. Input validation (Requirement 7.2 / 7.4) — every range,
           collection-size, and required-attribute check runs before
           any database read so a malformed request never touches
           identity service, the ``Activity_Plans`` /
           ``Deliverable_Expectations`` / ``Plan_Revisions`` lookups,
           or the authorization service.
        3. Resolve the target Activity Plan via a single SELECT
           against ``Activity_Plans``. When the identifier does not
           resolve, raise
           :class:`PlanRevisionActivityPlanNotResolvableError`. The
           check runs before authorization evaluation so the deny
           path never reveals whether an Activity Plan exists for an
           unauthorized caller.
        4. Resolve every Deliverable Expectation reference through a
           single parameterized SELECT against
           ``Deliverable_Expectations`` (or zero SELECTs when the
           reference list is empty). When any identifier does not
           resolve, raise
           :class:`PlanRevisionDeliverableExpectationNotResolvableError`
           identifying the first offending entry. The check also
           runs before authorization evaluation for the same
           don't-leak-existence reason.
        5. When ``predecessor_plan_revision_id`` is supplied, SELECT
           the predecessor's ``activity_plan_id`` and
           ``lifecycle_state`` from ``Plan_Revisions``. Reject the
           unresolvable case
           (:class:`PlanRevisionPredecessorNotResolvableError`), the
           cross-Activity-Plan case
           (:class:`PlanRevisionPredecessorActivityPlanMismatchError`),
           and the approved-predecessor case
           (:class:`PlanRevisionPredecessorApprovedError`).
        6. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`PlanRevisionAuditFailureError` in place of
           :class:`PlanRevisionAuthorizationError`.
        7. On ``permit``, mint the Plan Revision Identity (and the
           ``Supersedes`` Relationship Identity when a predecessor
           was supplied) and register the Plan Revision Identity in
           ``Identifier_Registry`` (kind ``'revision'``, carrying the
           Slice 2 ``resource_kind = 'plan_revision'`` tag per
           AD-WS-19) via
           :func:`walking_slice.planning._helpers._record_planning_resource`.
        8. INSERT the ``Plan_Revisions`` row carrying every
           Requirement 7.2 attribute with
           ``lifecycle_state = 'draft'``.
        9. INSERT exactly one ``Relationships`` row when a
           predecessor was supplied with
           ``relationship_type='Supersedes'``,
           ``source_kind='plan_revision'`` /
           ``source_id=plan_revision_id`` /
           ``source_revision_id=NULL`` (the new Plan Revision is the
           source — Plan Revisions live in a single table so the
           ``source_id`` carries the revision's own identifier),
           ``target_kind='plan_revision'`` /
           ``target_id=predecessor_plan_revision_id`` /
           ``target_revision_id=NULL``, and
           ``semantic_role=NULL`` (the AD-WS-17 ``semantic_role``
           column is reserved for Plan Review's ``'review'``
           discriminator and is not used here).
        10. Append the consequential ``Audit_Records`` row with
            ``action_type='create.plan_revision'`` and
            ``target_id=plan_revision_id`` inside the same
            transaction (Requirement 7.6 / AD-WS-5).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_activity_plan_id: Identity of the parent Activity
                Plan Resource (Requirement 7.2). Must resolve to an
                existing row in ``Activity_Plans``.
            planned_scope: Planned scope statement of
                1..10000 characters (Requirement 7.2).
            deliverable_expectation_refs: 0..50 Deliverable
                Expectation Resource Identities each of which must
                resolve at creation time (Requirement 7.2). Order is
                preserved when persisted as JSON. May be the empty
                tuple.
            planning_assumptions: 0..100 planning-assumption text
                entries each of 1..2000 characters (Requirement 7.2).
                Order is preserved when persisted as JSON. May be
                the empty tuple.
            ordering_rationale: Optional ordering rationale of
                0..2000 characters, or ``None`` when omitted.
            predecessor_plan_revision_id: Optional predecessor Plan
                Revision Identity. When supplied, must resolve to an
                existing Plan Revision of the same Activity Plan and
                must itself be ``'draft'`` (Requirement 7.4).
            authoring_party_id: Identity of the authoring Party.
                Persisted on ``Plan_Revisions.authoring_party_id``
                and on the consequential audit row's
                ``actor_party_id``. The Slice 1 ``Parties`` foreign
                key is enforced by the database.
            applicable_scope: Scope identifier the Plan Revision
                applies within. Persisted on
                ``Plan_Revisions.applicable_scope`` and passed as
                ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback (Requirement 7.6).
                The same engine is used to open a fresh transaction
                for the authorization evaluation itself (Slice 1
                single-writer accommodation).
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
            :class:`CreatePlanRevisionResult` carrying the persisted
            Plan Revision Identity, the lifecycle state, every
            persisted attribute, the optional ``Supersedes``
            Relationship Identity, the recorded time, and the
            correlation identifier.

        Raises:
            PlanRevisionValidationError: A required attribute is
                missing or a Requirement 7.2 range / collection-size
                check was violated, or the request body carried a
                prohibited execution / observed-outcome /
                produced-deliverable attribute.
            PlanRevisionActivityPlanNotResolvableError: The target
                Activity Plan Identity did not resolve to an
                existing Activity Plan Resource (Requirement 7.4).
            PlanRevisionDeliverableExpectationNotResolvableError: At
                least one Deliverable Expectation reference did not
                resolve to an existing Deliverable Expectation
                Resource (Requirement 7.4).
            PlanRevisionPredecessorNotResolvableError: The
                predecessor Plan Revision Identity did not resolve
                to an existing Plan Revision (Requirement 7.4).
            PlanRevisionPredecessorActivityPlanMismatchError: The
                predecessor Plan Revision belongs to a different
                Activity Plan than the new Plan Revision targets
                (Requirement 7.4).
            PlanRevisionPredecessorApprovedError: The predecessor
                Plan Revision has ``lifecycle_state = 'approved'``;
                Requirement 7.4 forbids naming an Approved Plan
                Revision as a predecessor.
            PlanRevisionAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 7.5). The Denial Record was appended
                successfully in a separate transaction (Requirement
                7.6).
            PlanRevisionAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`PlanRevisionAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 7.6 /
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
                # Surface the prohibited keys through the structured
                # error type the route layer expects; preserves the
                # tuple of offending keys so the response can list
                # each per Requirement 13.5.
                raise PlanRevisionValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 7.2 / 7.4) before any
        # database read or authorization side-effect. Each validator
        # raises :class:`PlanRevisionValidationError` with a stable
        # ``failed_constraint`` so the HTTP layer (task 15.1) can
        # render structured 400 responses identifying the invalid
        # attribute (Requirement 7.4).
        self._validate_planned_scope(planned_scope)
        self._validate_required_strings(
            target_activity_plan_id=target_activity_plan_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
        )
        normalized_refs = self._validate_deliverable_refs(
            deliverable_expectation_refs
        )
        normalized_assumptions = self._validate_planning_assumptions(
            planning_assumptions
        )
        self._validate_ordering_rationale(ordering_rationale)
        self._validate_predecessor_id_type(predecessor_plan_revision_id)

        # 3. Resolve the target Activity Plan Resource Identity
        # through a single SELECT on ``Activity_Plans``. The lookup
        # runs on the caller's connection so it participates in the
        # caller's transactional view. Requirement 7.4 rejects the
        # unresolvable case before authorization evaluation so the
        # deny path never reveals whether an Activity Plan exists
        # for an unauthorized caller.
        resolved_activity_plan = connection.execute(
            text(
                "SELECT activity_plan_id FROM Activity_Plans "
                "WHERE activity_plan_id = :activity_plan_id"
            ),
            {"activity_plan_id": target_activity_plan_id},
        ).scalar_one_or_none()
        if resolved_activity_plan is None:
            raise PlanRevisionActivityPlanNotResolvableError(
                target_activity_plan_id=target_activity_plan_id,
            )

        # 4. Resolve every Deliverable Expectation reference through
        # a single parameterized SELECT (or skip the round trip when
        # the list is empty). Order is preserved in the persisted
        # JSON; we only need the set of resolved identifiers to
        # confirm each entry exists. Requirement 7.4 specifies the
        # rejection path naming the first offending entry.
        if normalized_refs:
            self._validate_deliverable_refs_resolve(
                connection, normalized_refs
            )

        # 5. When a predecessor is supplied, validate that it
        # resolves to a Plan Revision of the SAME Activity Plan
        # whose ``lifecycle_state`` is ``'draft'`` (Requirement 7.4).
        # The two attributes are read together so the predecessor
        # row is read exactly once.
        if predecessor_plan_revision_id is not None:
            self._validate_predecessor(
                connection,
                predecessor_plan_revision_id=predecessor_plan_revision_id,
                target_activity_plan_id=target_activity_plan_id,
            )

        # 6. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Plan_Revisions row, the optional Supersedes Relationship
        # row, and the consequential audit row all share this
        # timestamp; the optional ``evaluation_at`` parameter
        # changes only *when* authority is evaluated *as of*, not
        # the recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 7. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 documented accommodation for SQLite's
        # single-writer model; the deny path opens *another*
        # separate transaction for the Denial Record write, and the
        # caller's transaction stays a reader until step 8 below).
        # The authorization target is the parent Activity Plan —
        # ``create.plan_revision`` authority is scoped against the
        # Activity Plan the Plan Revision is being recorded under.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=authoring_party_id,
                action=_ACTION_CREATE_PLAN_REVISION,
                target=TargetRef(
                    kind=_KIND_ACTIVITY_PLAN,
                    id=target_activity_plan_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_plan_revision_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_activity_plan_id=target_activity_plan_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise PlanRevisionAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 8. Mint identifiers (AD-WS-2 / AD-WS-19). Plan Revisions
        # live in a single Revision-level table (no separate Resource
        # header) so only one Revision identifier is minted for the
        # Plan Revision itself; the Supersedes Relationship gets its
        # own identifier when a predecessor was supplied.
        plan_revision_id = str(self.identity_service.new_revision_id())
        supersedes_relationship_id: Optional[str] = None
        if predecessor_plan_revision_id is not None:
            supersedes_relationship_id = str(
                self.identity_service.new_relationship_id()
            )
        # ``content_digest`` is bound to the Plan Revision identifier
        # in ``Identifier_Registry``; the digest is the SHA-256 of
        # the canonical JSON payload of the new Revision so two
        # different Plan Revisions never collide on the same digest.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_activity_plan_id": target_activity_plan_id,
                    "lifecycle_state": _LIFECYCLE_DRAFT,
                    "planned_scope": planned_scope,
                    "deliverable_expectation_refs": list(normalized_refs),
                    "planning_assumptions": list(normalized_assumptions),
                    "ordering_rationale": ordering_rationale,
                    "predecessor_plan_revision_id": (
                        predecessor_plan_revision_id
                    ),
                    "authoring_party_id": authoring_party_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 9. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-19 ``resource_kind = 'plan_revision'``
        # tag. This is the row-level discriminator that keeps the
        # Plan Revision identifier set inspectably disjoint from
        # every other Slice 2 ``resource_kind``. The helper
        # delegates to :meth:`IdentityService.reject_if_duplicate`
        # so the Slice 1 identifier-conflict Denial Record pathway
        # fires on any collision; on success the helper INSERTs one
        # row inside the caller's transaction.
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_PLAN_REVISION,
            plan_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PLAN_REVISION,
            recorded_time=recorded_time,
        )

        # 10. Insert the Plan Revision row. Every Requirement 7.2
        # attribute lands here. ``lifecycle_state`` is the literal
        # ``'draft'`` (AD-WS-18) — the only other valid value
        # (``'approved'``) is reachable only via the gated AD-WS-19
        # trigger inside a Plan Approval transaction.
        # ``deliverable_expectation_refs_json`` and
        # ``planning_assumptions_json`` carry canonical JSON
        # encodings with sorted keys at the outer level (the lists
        # themselves are order-preserving so the request order is
        # preserved verbatim per Requirement 7.2's positional
        # semantics).
        connection.execute(
            text(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :plan_revision_id, :activity_plan_id,
                    :predecessor_revision_id, :lifecycle_state,
                    :planned_scope, :deliverable_expectation_refs_json,
                    :planning_assumptions_json, :ordering_rationale,
                    :authoring_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "plan_revision_id": plan_revision_id,
                "activity_plan_id": target_activity_plan_id,
                "predecessor_revision_id": predecessor_plan_revision_id,
                "lifecycle_state": _LIFECYCLE_DRAFT,
                "planned_scope": planned_scope,
                "deliverable_expectation_refs_json": json.dumps(
                    list(normalized_refs)
                ),
                "planning_assumptions_json": json.dumps(
                    list(normalized_assumptions)
                ),
                "ordering_rationale": ordering_rationale,
                "authoring_party_id": authoring_party_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 11. Insert the single ``Supersedes`` Relationship row when
        # a predecessor was supplied (Requirement 7.3). Plan
        # Revisions live in a single table so the source / target
        # identifiers are the Plan Revision row's primary key on
        # each side; ``source_revision_id`` and ``target_revision_id``
        # are NULL because the table itself is the Revision-level
        # table (no separate Revision identifier exists).
        # ``semantic_role`` is NULL — the AD-WS-17 column is
        # reserved for Plan Review's ``'review'`` discriminator.
        if (
            predecessor_plan_revision_id is not None
            and supersedes_relationship_id is not None
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
                        :target_kind, :target_id, :target_revision_id,
                        :authoring_party_id, :recorded_at, NULL
                    )
                    """
                ),
                {
                    "relationship_id": supersedes_relationship_id,
                    "relationship_type": _RELATIONSHIP_TYPE_SUPERSEDES,
                    "source_kind": _KIND_PLAN_REVISION,
                    "source_id": plan_revision_id,
                    "source_revision_id": None,
                    "target_kind": _KIND_PLAN_REVISION,
                    "target_id": predecessor_plan_revision_id,
                    "target_revision_id": None,
                    "authoring_party_id": authoring_party_id,
                    "recorded_at": recorded_at,
                },
            )

        # 12. Append the consequential audit row (Requirement 7.6 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Plan_Revisions, and
        # optional Relationships rows together. ``target_id`` is the
        # Plan Revision Identity; ``target_revision_id`` is also the
        # Plan Revision Identity because Plan Revisions ARE the
        # Revision-level resource (no separate Resource header).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_ACTION_CREATE_PLAN_REVISION,
            target_id=plan_revision_id,
            target_revision_id=plan_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreatePlanRevisionResult(
            plan_revision_id=plan_revision_id,
            target_activity_plan_id=target_activity_plan_id,
            lifecycle_state=_LIFECYCLE_DRAFT,
            planned_scope=planned_scope,
            deliverable_expectation_refs=normalized_refs,
            planning_assumptions=normalized_assumptions,
            ordering_rationale=ordering_rationale,
            predecessor_plan_revision_id=predecessor_plan_revision_id,
            supersedes_relationship_id=supersedes_relationship_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )


    # -- read APIs ---------------------------------------------------------

    @staticmethod
    def get_plan_revision(
        connection: Connection,
        plan_revision_id: str,
    ) -> Optional[PlanRevisionRow]:
        """Read-only lookup of a Plan Revision row by Identity (AD-WS-30).

        Performs a single indexed ``SELECT`` against ``Plan_Revisions`` and
        returns the four columns Slice 3's Execution_Service needs to
        enforce Requirements 23.2 and 23.4 when creating a Work Assignment
        Record: ``plan_revision_id``, ``lifecycle_state``,
        ``activity_plan_id``, ``applicable_scope``. No other column is
        read and no other table is touched; the SELECT hits the
        ``Plan_Revisions`` primary-key index.

        This function is *strictly* read-only — it issues exactly one
        ``SELECT`` and no ``INSERT``, ``UPDATE``, or ``DELETE``. Task 2.1
        explicitly prohibits any write path here, and AD-WS-30 designates
        the function as the Execution_Service's only valid entry point
        into ``Plan_Revisions`` (the Execution_Service does not query
        Slice 2 tables directly per Principle 5.2 and
        ``03-context-map.md`` Cross-Context Rule 2). The lookup is also
        the only Planning_Service read the Work Assignment Service uses
        to satisfy Requirement 23.4's pre-authorization-evaluation
        existence check.

        The function is a :func:`staticmethod` because the read does not
        consult any of the wired collaborators (clock, identity service,
        audit log, authorization service) — it only needs the caller's
        SQLAlchemy ``Connection``. Exposing it on
        :class:`PlanRevisionService` rather than as a module-level helper
        keeps the AD-WS-30 entry-point name (``PlanRevisionService.get_plan_revision``)
        textually stable across the Slice 2 design and the Slice 3 design,
        and aligns with the ``planning_reader: PlanRevisionService``
        attribute on the Execution_Service Work Assignment dataclass
        defined in the Slice 3 design.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                read context. The lookup participates in the caller's
                transactional view so the Execution_Service sees a
                consistent snapshot across its pre-authorization checks.
            plan_revision_id: The Plan Revision Identity to resolve.

        Returns:
            A :class:`PlanRevisionRow` snapshot of the requested Plan
            Revision when one exists. ``None`` when no
            ``Plan_Revisions`` row matches the supplied identifier — the
            caller (the Work Assignment Service per AD-WS-30) treats
            ``None`` as the "unresolvable" branch of Requirement 23.4
            and rejects the Work Assignment creation without revealing
            existence to unauthorized callers.

        Notes:
            The function does not validate ``plan_revision_id`` beyond
            passing it through to SQLAlchemy as a bound parameter; the
            calling Execution_Service module is responsible for
            structural validation of identifiers received from
            untrusted input (Slice 3 task 5.1). A non-resolving
            identifier returns ``None`` rather than raising, mirroring
            the ``scalar_one_or_none`` / ``one_or_none`` conventions
            already used by sibling lookups in this module
            (e.g., :meth:`_validate_predecessor`).
        """
        row = connection.execute(
            text(
                "SELECT plan_revision_id, lifecycle_state, "
                "activity_plan_id, applicable_scope "
                "FROM Plan_Revisions "
                "WHERE plan_revision_id = :plan_revision_id"
            ),
            {"plan_revision_id": plan_revision_id},
        ).one_or_none()
        if row is None:
            return None
        return PlanRevisionRow(
            plan_revision_id=row[0],
            lifecycle_state=row[1],
            activity_plan_id=row[2],
            applicable_scope=row[3],
        )


    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_planned_scope(planned_scope: Any) -> None:
        """Reject planned scope outside the Requirement 7.2 range.

        Empty or non-string planned scopes surface as
        ``planned_scope_missing`` since the actionable next step is
        the same in both cases (supply a non-empty string of
        1..10000 chars). An over-long scope surfaces as
        ``planned_scope_too_long``.
        """
        if (
            planned_scope is None
            or not isinstance(planned_scope, str)
            or planned_scope == ""
        ):
            raise PlanRevisionValidationError(
                "planned_scope is required and must be a non-empty string of "
                f"{_PLANNED_SCOPE_MIN_CHARS}..{_PLANNED_SCOPE_MAX_CHARS} "
                "characters; Requirement 7.2 / 7.4.",
                failed_constraint="planned_scope_missing",
            )
        if len(planned_scope) > _PLANNED_SCOPE_MAX_CHARS:
            raise PlanRevisionValidationError(
                f"planned_scope length {len(planned_scope)} exceeds the "
                f"{_PLANNED_SCOPE_MAX_CHARS}-character limit imposed by "
                "Requirement 7.2.",
                failed_constraint="planned_scope_too_long",
            )

    @staticmethod
    def _validate_required_strings(
        *,
        target_activity_plan_id: Any,
        authoring_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 7.4: a Plan Revision creation request that
        names a target Activity Plan Identity that does not resolve,
        omits the planned scope, or omits the applicable scope is
        rejected. The planned-scope check lives in
        :meth:`_validate_planned_scope`; this validator covers the
        other required strings plus ``authoring_party_id`` (which
        Requirement 7.5 implicitly requires — an unauthenticated
        request has no Party Identity to authorize against).
        """
        if not target_activity_plan_id or not isinstance(
            target_activity_plan_id, str
        ):
            raise PlanRevisionValidationError(
                "target_activity_plan_id is required; Requirement 7.4 "
                "rejects Plan Revisions missing the target Activity Plan "
                "Identity.",
                failed_constraint="target_activity_plan_id_missing",
            )
        if not authoring_party_id or not isinstance(authoring_party_id, str):
            raise PlanRevisionValidationError(
                "authoring_party_id is required; Requirement 7.5 rejects "
                "unauthenticated Plan Revision creation.",
                failed_constraint="authoring_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise PlanRevisionValidationError(
                "applicable_scope is required; Requirement 7.4 rejects "
                "Plan Revisions missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_deliverable_refs(
        deliverable_expectation_refs: Any,
    ) -> tuple[str, ...]:
        """Validate the Deliverable Expectation reference sequence.

        Per Requirement 7.2 the list holds 0..50 entries each of
        which must be a non-empty string identifier. Per Requirement
        7.4 the rejection path names the first offending entry. The
        validator also rejects duplicate references in the same
        request because storing the same Deliverable Expectation
        twice would silently double-count the reference for any
        downstream consumer that deduplicates by identifier set.

        Returns:
            The reference sequence as a tuple of strings preserving
            input order. The empty tuple is a valid return value.

        Raises:
            PlanRevisionValidationError: The sequence is not a list
                or tuple, exceeds the 50-entry maximum, contains a
                non-string / empty entry, or contains a duplicate.
        """
        if deliverable_expectation_refs is None:
            # ``None`` is treated as "no references"; the request
            # contract accepts an explicit ``()`` to mean the same
            # thing.
            return ()
        if not isinstance(deliverable_expectation_refs, (list, tuple)):
            raise PlanRevisionValidationError(
                "deliverable_expectation_refs must be a list or tuple; "
                f"received {type(deliverable_expectation_refs).__name__}.",
                failed_constraint="deliverable_expectation_refs_invalid_type",
            )
        count = len(deliverable_expectation_refs)
        if count > _DELIVERABLE_REFS_MAX_COUNT:
            raise PlanRevisionValidationError(
                f"deliverable_expectation_refs has {count} entries which "
                f"exceeds the {_DELIVERABLE_REFS_MAX_COUNT}-entry maximum "
                "imposed by Requirement 7.2.",
                failed_constraint="deliverable_expectation_refs_too_many",
            )
        seen: set[str] = set()
        normalized: list[str] = []
        for index, entry in enumerate(deliverable_expectation_refs):
            if not isinstance(entry, str) or entry == "":
                raise PlanRevisionValidationError(
                    f"deliverable_expectation_refs[{index}] must be a "
                    "non-empty string identifier; received "
                    f"{type(entry).__name__}.",
                    failed_constraint=(
                        "deliverable_expectation_ref_invalid_type"
                    ),
                    invalid_index=index,
                )
            if entry in seen:
                raise PlanRevisionValidationError(
                    f"deliverable_expectation_refs[{index}]={entry!r} is a "
                    "duplicate of an earlier entry; Requirement 7.2 binds "
                    "each Deliverable Expectation reference exactly once.",
                    failed_constraint=(
                        "deliverable_expectation_refs_duplicate"
                    ),
                    invalid_index=index,
                )
            seen.add(entry)
            normalized.append(entry)
        return tuple(normalized)

    @staticmethod
    def _validate_planning_assumptions(
        planning_assumptions: Any,
    ) -> tuple[str, ...]:
        """Validate the planning-assumption sequence.

        Per Requirement 7.2 the list holds 0..100 entries each of
        1..2000 characters. Per Requirement 7.4 the rejection path
        identifies the first offending entry.

        Returns:
            The assumption sequence as a tuple of strings preserving
            input order. The empty tuple is a valid return value.

        Raises:
            PlanRevisionValidationError: The sequence is not a list
                or tuple, exceeds the 100-entry maximum, contains a
                non-string / empty entry, or contains an entry
                longer than 2000 characters.
        """
        if planning_assumptions is None:
            return ()
        if not isinstance(planning_assumptions, (list, tuple)):
            raise PlanRevisionValidationError(
                "planning_assumptions must be a list or tuple; received "
                f"{type(planning_assumptions).__name__}.",
                failed_constraint="planning_assumptions_invalid_type",
            )
        count = len(planning_assumptions)
        if count > _PLANNING_ASSUMPTIONS_MAX_COUNT:
            raise PlanRevisionValidationError(
                f"planning_assumptions has {count} entries which exceeds "
                f"the {_PLANNING_ASSUMPTIONS_MAX_COUNT}-entry maximum "
                "imposed by Requirement 7.2.",
                failed_constraint="planning_assumptions_too_many",
            )
        normalized: list[str] = []
        for index, entry in enumerate(planning_assumptions):
            if not isinstance(entry, str) or len(entry) < _ASSUMPTION_MIN_CHARS:
                raise PlanRevisionValidationError(
                    f"planning_assumptions[{index}] must be a non-empty "
                    f"string of {_ASSUMPTION_MIN_CHARS}.."
                    f"{_ASSUMPTION_MAX_CHARS} characters; received "
                    f"{type(entry).__name__}.",
                    failed_constraint="planning_assumption_invalid_type",
                    invalid_index=index,
                )
            if len(entry) > _ASSUMPTION_MAX_CHARS:
                raise PlanRevisionValidationError(
                    f"planning_assumptions[{index}] length {len(entry)} "
                    f"exceeds the {_ASSUMPTION_MAX_CHARS}-character limit "
                    "imposed by Requirement 7.2.",
                    failed_constraint="planning_assumption_too_long",
                    invalid_index=index,
                )
            normalized.append(entry)
        return tuple(normalized)

    @staticmethod
    def _validate_ordering_rationale(ordering_rationale: Any) -> None:
        """Reject ordering rationale outside the Requirement 7.2 range.

        Per Requirement 7.2 the ordering rationale is 0..2000
        characters and optional. ``None`` is accepted (the column is
        NULLable) and persisted as SQL ``NULL``; the empty string is
        also accepted (length 0 satisfies the 0 lower bound) and
        persisted verbatim.
        """
        if ordering_rationale is None:
            return
        if not isinstance(ordering_rationale, str):
            raise PlanRevisionValidationError(
                "ordering_rationale must be a str or None; received "
                f"{type(ordering_rationale).__name__}.",
                failed_constraint="ordering_rationale_invalid_type",
            )
        if len(ordering_rationale) > _ORDERING_RATIONALE_MAX_CHARS:
            raise PlanRevisionValidationError(
                f"ordering_rationale length {len(ordering_rationale)} "
                f"exceeds the {_ORDERING_RATIONALE_MAX_CHARS}-character "
                "limit imposed by Requirement 7.2.",
                failed_constraint="ordering_rationale_too_long",
            )

    @staticmethod
    def _validate_predecessor_id_type(
        predecessor_plan_revision_id: Any,
    ) -> None:
        """Reject a non-string predecessor identifier.

        Per Requirement 7.2 the predecessor Plan Revision Identity is
        optional. The semantic checks (resolves / same Activity Plan
        / draft state) happen in
        :meth:`_validate_predecessor` against the database; this
        validator covers only the type contract so a malformed
        predecessor identifier is rejected before any database read.
        """
        if predecessor_plan_revision_id is None:
            return
        if (
            not isinstance(predecessor_plan_revision_id, str)
            or predecessor_plan_revision_id == ""
        ):
            raise PlanRevisionValidationError(
                "predecessor_plan_revision_id must be a non-empty string or "
                f"None; received {type(predecessor_plan_revision_id).__name__}.",
                failed_constraint=(
                    "predecessor_plan_revision_id_invalid_type"
                ),
            )

    @staticmethod
    def _validate_deliverable_refs_resolve(
        connection: Connection,
        deliverable_expectation_refs: Sequence[str],
    ) -> None:
        """Verify every Deliverable Expectation reference resolves.

        Per Requirement 7.2 every reference in
        ``deliverable_expectation_refs`` must resolve to an existing
        ``Deliverable_Expectations`` row at creation time.
        Requirement 7.4 specifies the rejection path naming the
        first offending entry. The lookup uses a single SELECT
        returning the set of resolved identifiers; the iteration in
        Python identifies which input index is missing so the
        ordering of the rejection matches the input ordering
        deterministically.
        """
        # ``expanding=True`` lets SQLAlchemy bind a variable-length
        # parameter list to the SQL ``IN`` clause without manual
        # string interpolation.
        statement = text(
            "SELECT deliverable_expectation_id FROM Deliverable_Expectations "
            "WHERE deliverable_expectation_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows = connection.execute(
            statement, {"ids": list(deliverable_expectation_refs)}
        ).all()
        resolved = {row[0] for row in rows}
        for index, ref in enumerate(deliverable_expectation_refs):
            if ref not in resolved:
                raise PlanRevisionDeliverableExpectationNotResolvableError(
                    deliverable_expectation_id=ref,
                    invalid_index=index,
                )

    @staticmethod
    def _validate_predecessor(
        connection: Connection,
        *,
        predecessor_plan_revision_id: str,
        target_activity_plan_id: str,
    ) -> None:
        """Verify the predecessor exists, matches Activity Plan, is draft.

        Per Requirement 7.4 the predecessor Plan Revision must
        resolve, must belong to the same Activity Plan as the new
        Plan Revision, and must itself be ``'draft'`` — an
        Approved Plan Revision can never be named as the predecessor
        of a new Plan Revision (the supersession edge is meaningful
        only between unapproved plans; once approved a plan is
        byte-equivalent forever per AD-WS-19 and there is nothing
        further to supersede).

        Reads ``activity_plan_id`` and ``lifecycle_state`` in a
        single SELECT so the predecessor row is fetched exactly
        once.
        """
        row = connection.execute(
            text(
                "SELECT activity_plan_id, lifecycle_state "
                "FROM Plan_Revisions "
                "WHERE plan_revision_id = :plan_revision_id"
            ),
            {"plan_revision_id": predecessor_plan_revision_id},
        ).one_or_none()
        if row is None:
            raise PlanRevisionPredecessorNotResolvableError(
                predecessor_plan_revision_id=predecessor_plan_revision_id,
            )
        predecessor_activity_plan_id, predecessor_lifecycle_state = row
        if predecessor_activity_plan_id != target_activity_plan_id:
            raise PlanRevisionPredecessorActivityPlanMismatchError(
                predecessor_plan_revision_id=predecessor_plan_revision_id,
                predecessor_activity_plan_id=predecessor_activity_plan_id,
                target_activity_plan_id=target_activity_plan_id,
            )
        if predecessor_lifecycle_state != _LIFECYCLE_DRAFT:
            raise PlanRevisionPredecessorApprovedError(
                predecessor_plan_revision_id=predecessor_plan_revision_id,
                predecessor_lifecycle_state=predecessor_lifecycle_state,
            )

    # -- denial side-channel ----------------------------------------------

    def _persist_plan_revision_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_activity_plan_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Plan Revision attempt.

        Implements the Requirement 7.6 retry contract verbatim
        (mirroring
        :meth:`walking_slice.planning.activity_plans.ActivityPlanService._persist_activity_plan_denial`):
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
          :class:`PlanRevisionAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_plan_revision` raises
        :class:`PlanRevisionAuthorizationError` (or this method
        raises :class:`PlanRevisionAuditFailureError`). The Denial
        Record must therefore live outside that scope to survive
        (AD-WS-9 / Requirement 7.6). The denial row's ``target_id``
        is the Activity Plan identifier rather than the (not-yet
        minted) Plan Revision identifier; this matches the AD-WS-9
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
                        attempted_action=_ACTION_CREATE_PLAN_REVISION,
                        target_id=target_activity_plan_id,
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

        raise PlanRevisionAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this
# module does not import private names from sibling planning modules.
# The functions are intentionally identical to their Activity Plans
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
    Revision creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Plan Revision
    Identity in ``Identifier_Registry``. Plan Revisions live in a
    single Revision-level table (no separate Resource header) so
    this digest is bound only once per Plan Revision creation —
    distinct from the Projects / Objectives pattern where the same
    digest is shared between Resource and first-Revision bindings.
    """
    return hashlib.sha256(content).hexdigest()
