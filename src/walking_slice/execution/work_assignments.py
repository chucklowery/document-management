"""Execution_Service.WorkAssignments — immutable Work Assignment Records
from an Assignment Authority Party to an assignee Contributor, addressing
exactly one Approved Plan Revision.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Execution_Service.WorkAssignments" — public dataclass surface, authority
  string (``create.work_assignment`` → ``assign`` per AD-WS-24), AD-WS-9
  separate-transaction Denial Record on deny, validation order, and the
  Relationship-row contract (``Addresses`` to the Plan Revision with
  ``semantic_role IS NULL``; ``Relates To`` to the assignee Party with
  ``semantic_role = 'assignee'`` per AD-WS-26).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with
  ``kind = 'immutable_record'`` and
  ``resource_kind = 'work_assignment_record'`` per AD-WS-28);
  Authorization (the action string ``create.work_assignment`` maps to
  the ``assign`` authority per AD-WS-24; the deny path uses the Slice 1
  separate-transaction Denial-Record pattern reproduced from
  :class:`walking_slice.planning.plan_revisions.PlanRevisionService`).
- AD-WS-24 — additive ``assign`` mapping for ``create.work_assignment``.
- AD-WS-27 — ``Work_Assignment_Records`` is append-only; the
  ``Plan_Revisions`` row addressed by the Work Assignment must remain
  byte-equivalent throughout this transaction.
- AD-WS-28 — additive ``Identifier_Registry.resource_kind`` value
  ``'work_assignment_record'`` populated through
  :func:`walking_slice.execution._helpers._record_execution_artifact`.
- AD-WS-30 — the only Planning_Service entry point this module consults
  is :meth:`PlanRevisionService.get_plan_revision` (a single indexed
  SELECT). Slice 2 tables are never written by this module.

Task scope (task 5.1)
=====================

This module implements :meth:`WorkAssignmentService.create_work_assignment`:

1. Validate request inputs per Requirement 23.3
   (``assignment_rationale`` 0..4000 chars, ``authority_basis.type`` in
   the AD-WS-10 set, ``applicable_scope`` present) and Requirement 23.7
   (target Plan Revision Identity required, assignee Party Identity
   required, authority basis required, applicable scope required).
2. Defensively reject any prohibited planning-attribute or
   observed-outcome key in the original request body via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
3. Reject self-assignment (``assignment_authority_party_id ==
   assignee_party_id``) per Requirement 23.5. The schema-level
   ``CHECK (assignee_party_id != assignment_authority_party_id)``
   enforces the same invariant; surfacing the check at the service
   layer makes the rejection deterministic and gives a precise
   ``failed_constraint`` to the HTTP layer.
4. Resolve the target Plan Revision via
   :meth:`PlanRevisionService.get_plan_revision`. Reject when
   unresolvable, when ``lifecycle_state != 'approved'``, or when the
   Plan Revision's applicable scope is not covered by the requesting
   Party's applicable scope (Requirement 23.4). The check runs before
   authorization evaluation so the deny path never reveals whether an
   approved Plan Revision exists for an unauthorized caller.
5. Resolve the assignee Party via a single SELECT against ``Parties``.
   Reject when unresolvable (Requirement 23.5). The slice schema does
   not model a Party ``active``/``inactive`` flag; only existence is
   checked here. A future ADR may introduce a Party status column at
   which point the inactive branch will be added.
6. Evaluate ``Authorization_Service.evaluate(party=assignment_authority_party_id,
   action="create.work_assignment", target=plan_revision_ref, at=now())``;
   on deny, use the AD-WS-9 separate-transaction Denial-Record pattern
   with the Slice 1 / Slice 2 3-attempt retry sequence
   (0.01s / 0.02s / 0.04s exponential backoff). On total audit failure
   raise :class:`WorkAssignmentAuditFailureError` in place of
   :class:`WorkAssignmentAuthorizationError`.
7. On permit, mint the Work Assignment Record Identity (UUIDv7) and the
   two Relationship Identities; register the Work Assignment Identity
   in ``Identifier_Registry`` with ``kind='immutable_record'`` and
   ``resource_kind='work_assignment_record'`` via
   :func:`_record_execution_artifact`.
8. INSERT the ``Work_Assignment_Records`` row carrying every
   Requirement 23.3 attribute.
9. INSERT exactly one ``Relationships`` row with
   ``relationship_type='Addresses'``,
   ``source_kind='work_assignment_record'``,
   ``target_kind='plan_revision'`` /
   ``target_id=target_plan_revision_id`` /
   ``target_revision_id=NULL``, and ``semantic_role=NULL``
   (AD-WS-26 — the Plan Revision is itself a Revision-level row in
   ``Plan_Revisions`` so the binding lives on ``target_id`` only).
10. INSERT exactly one ``Relationships`` row with
    ``relationship_type='Relates To'``,
    ``source_kind='work_assignment_record'``,
    ``target_kind='party'`` / ``target_id=assignee_party_id`` /
    ``target_revision_id=NULL``, and ``semantic_role='assignee'``
    (AD-WS-26).
11. Append the consequential ``Audit_Records`` row with
    ``action_type='create.work_assignment'`` and
    ``target_id=work_assignment_id`` inside the same transaction
    (Requirement 23.8 / Slice 1 AD-WS-5).

Requirements satisfied
======================

    23.1 — authorized Work Assignment creation produces exactly one
           immutable Work Assignment Record within nominal latency.
    23.2 — the target Plan Revision must resolve and its lifecycle
           state at the recorded time must be ``'approved'``
           (consulted through :meth:`PlanRevisionService.get_plan_revision`).
    23.3 — every Work Assignment Record records the target Approved
           Plan Revision Identity, the assignee Party Identity, the
           Assignment Authority Party Identity, the assignment
           rationale (0..4000 chars), the authority basis (drawn from
           the AD-WS-10 set), the applicable scope, the recorded time
           in UTC with millisecond precision, the ``Addresses``
           Relationship to the target Plan Revision, and the
           ``Relates To`` Relationship with ``semantic_role='assignee'``
           to the assignee Party.
    23.4 — unresolvable target Plan Revision, non-approved Plan
           Revision, and scope-mismatched Plan Revision are rejected
           with no Work Assignment Record created.
    23.5 — unresolvable assignee Party and self-assignment
           (``assignment_authority_party_id == assignee_party_id``)
           are rejected with no Work Assignment Record created.
    23.6 — unauthorized requests are denied via
           :class:`AuthorizationService`; the Execution_Service
           declines to create any Work Assignment Record and the
           Audit_Log appends a Denial Record conforming to AD-WS-9.
    23.7 — missing target Plan Revision Identity, missing assignee
           Party Identity, missing applicable scope, missing
           authority basis, or extras-on-cardinality are rejected
           with a structured error identifying the missing attribute.
    23.8 — the Audit_Log appends an immutable consequential audit
           row identifying the Work Assignment Record Identity,
           target Approved Plan Revision Identity, assignee Party
           Identity, Assignment Authority Party Identity, authority
           basis, and recorded time within the same transaction as
           the Work Assignment Record creation.
    23.9 — the append-only schema triggers (created in task 1.2)
           reject every UPDATE / DELETE attempt on
           ``Work_Assignment_Records`` after this transaction commits.
    32.6 — ``create.work_assignment`` requires the ``assign``
           authority (AD-WS-24); the non-substitution invariant is
           preserved by :class:`AuthorizationService`.
    33.4 — request bodies that carry a prohibited planning-attribute
           prefix are rejected with the offending keys identified.
    34.5 — request bodies that carry a prohibited observed-outcome
           prefix are rejected with the offending keys identified.
    41.1 — every consequential write is atomic with its consequential
           audit row.
    41.2 — every consequential write checks authority before persisting
           any domain row.
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
from walking_slice.planning.plan_revisions import (
    PlanRevisionRow,
    PlanRevisionService,
)


__all__ = [
    "CreateWorkAssignmentResult",
    "WorkAssignmentAssigneeNotResolvableError",
    "WorkAssignmentAuditFailureError",
    "WorkAssignmentAuthorizationError",
    "WorkAssignmentPlanRevisionNotApprovedError",
    "WorkAssignmentPlanRevisionNotResolvableError",
    "WorkAssignmentPlanRevisionScopeMismatchError",
    "WorkAssignmentSelfAssignmentError",
    "WorkAssignmentService",
    "WorkAssignmentValidationError",
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


# ``create.work_assignment`` maps to the ``assign`` authority per
# AD-WS-24. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 23.8) and on the
# separate-transaction Denial Record appended by
# :meth:`WorkAssignmentService._persist_work_assignment_denial` so
# audit consumers can correlate denial rows with the action a Party
# was attempting.
_ACTION_CREATE_WORK_ASSIGNMENT: Final[str] = "create.work_assignment"

# Relationship Type strings written to the ``Addresses`` and
# ``Relates To`` rows that bind the Work Assignment Record to its
# target Plan Revision and its assignee Party, per AD-WS-26.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_RELATES_TO: Final[str] = "Relates To"

# ``Relationships.source_kind`` / ``target_kind`` / ``semantic_role``
# strings per AD-WS-26. The Plan Revision binding carries
# ``semantic_role=NULL``; the assignee Party binding carries
# ``semantic_role='assignee'``.
_KIND_WORK_ASSIGNMENT_RECORD: Final[str] = "work_assignment_record"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"
_KIND_PARTY: Final[str] = "party"
_SEMANTIC_ROLE_ASSIGNEE: Final[str] = "assignee"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Execution_Service ``resource_kind`` tag (Slice 3 additive
# enumeration per AD-WS-28). Work Assignment Records are Immutable
# Records (per [`02-domain-model.md`](../../../documents/02-domain-model.md)
# §8.2 Execution Record) so the registry kind is ``'immutable_record'``;
# the ``resource_kind`` value is ``'work_assignment_record'`` and is
# the row-level discriminator that keeps the Work Assignment Identity
# set inspectably disjoint from every other Slice 1 / Slice 2 /
# Slice 3 ``resource_kind`` (Requirement 22.8).
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_WORK_ASSIGNMENT: Final[str] = "work_assignment_record"

# Lifecycle state literal the target Plan Revision must carry at the
# recorded time per Requirement 23.2 / Slice 2 Requirement 9.1.
_LIFECYCLE_APPROVED: Final[str] = "approved"

# Authority-basis ``type`` enumeration per AD-WS-10. Mirrors the
# Slice 2 ``Plan_Review_Revisions`` / ``Plan_Approval_Records`` and
# Slice 3 ``Work_Assignment_Records.authority_basis_type`` CHECK
# constraint values; centralizing the tuple here lets the validator
# reject malformed requests structurally before they touch SQL. The
# :class:`AuthorityBasisRef` Pydantic ``Literal`` already constrains
# Python-typed callers to the same enumeration, but the application
# layer may receive dict-shaped inputs from the HTTP layer that have
# not yet been bound to :class:`AuthorityBasisRef`; this check is the
# defense-in-depth that survives even those cases.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Validation limit for ``assignment_rationale`` per Requirement 23.3.
# The ``Work_Assignment_Records.assignment_rationale`` CHECK
# constraint enforces the same range; surfacing it here yields a
# precise ``failed_constraint`` for the HTTP layer rather than a raw
# SQL constraint violation.
_ASSIGNMENT_RATIONALE_MIN_CHARS: Final[int] = 0
_ASSIGNMENT_RATIONALE_MAX_CHARS: Final[int] = 4_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (mirrors Slice 1 Requirement 7.6 and Slice 2
# Plan Revision retry pattern). Three retries after the initial
# attempt for a total of four attempts. The sequence is byte-equivalent
# to the sibling Slice 2 modules so every Slice 1 / Slice 2 / Slice 3
# module presents identical denial-side timing (which the
# indistinguishable-denial properties rely on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class WorkAssignmentValidationError(ValueError):
    """Raised when a Work Assignment submission fails Requirement 23.3 / 23.7 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_plan_revision_id_missing"`` (empty / non-string target),
            ``"assignee_party_id_missing"``,
            ``"assignment_authority_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"assignment_rationale_invalid_type"`` (not str / None),
            ``"assignment_rationale_too_long"`` (> 4000 characters),
            ``"authority_basis_missing"`` (the authority basis is
                ``None`` / not a Mapping / not an
                :class:`AuthorityBasisRef`),
            ``"authority_basis_type_missing"`` (the ``type`` field is
                absent / empty),
            ``"authority_basis_type_out_of_set"`` (the ``type`` value is
                not in the AD-WS-10 set),
            ``"authority_basis_id_missing"`` (the ``id`` field is absent
                / empty / malformed),
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


class WorkAssignmentSelfAssignmentError(ValueError):
    """Raised when the request names the requesting Party as its own assignee.

    Requirement 23.5 forbids self-assignment in this slice. The
    rejection runs before authorization evaluation so the deny path
    never reveals whether the requesting Party would have been able
    to assign the same Plan Revision to a different Party. The
    schema-level CHECK constraint
    ``assignee_party_id != assignment_authority_party_id`` on
    ``Work_Assignment_Records`` enforces the same invariant at the
    database layer.

    Attributes:
        party_id: The Party Identity that appears on both
            ``assignment_authority_party_id`` and
            ``assignee_party_id``.
        failed_constraint: Always ``"self_assignment_forbidden"``.
    """

    def __init__(
        self,
        *,
        party_id: str,
        failed_constraint: str = "self_assignment_forbidden",
    ) -> None:
        super().__init__(
            f"Party {party_id!r} cannot assign work to itself; "
            "Requirement 23.5 forbids self-assignment in this slice "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.party_id = party_id
        self.failed_constraint = failed_constraint


class WorkAssignmentPlanRevisionNotResolvableError(LookupError):
    """Raised when the target Plan Revision Identity does not resolve.

    Requirement 23.4 requires the target Plan Revision Resource Identity
    to resolve to an existing Plan Revision. The check runs before
    authorization evaluation so the deny path never reveals whether a
    Plan Revision exists for an unauthorized caller.

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


class WorkAssignmentPlanRevisionNotApprovedError(LookupError):
    """Raised when the target Plan Revision is not approved.

    Requirement 23.2 requires the Plan Revision's lifecycle state at
    the recorded time to be ``'approved'`` per Slice 2 Requirement
    9.1. A draft (or otherwise non-approved) Plan Revision is
    rejected with no Work Assignment Record created. The check runs
    before authorization evaluation so the deny path never reveals
    the lifecycle state to an unauthorized caller.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        observed_lifecycle_state: The lifecycle state observed on the
            Plan Revision row (typically ``'draft'``; carried verbatim
            for debugging).
        failed_constraint: ``"target_plan_revision_not_approved"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        observed_lifecycle_state: str,
        failed_constraint: str = "target_plan_revision_not_approved",
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} has "
            f"lifecycle_state={observed_lifecycle_state!r}; Requirement "
            "23.2 requires 'approved' "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.observed_lifecycle_state = observed_lifecycle_state
        self.failed_constraint = failed_constraint


class WorkAssignmentPlanRevisionScopeMismatchError(LookupError):
    """Raised when the target Plan Revision's scope is outside the
    requesting Party's applicable Assignment Authority scope.

    Requirement 23.4 requires the Plan Revision's applicable scope to
    be within the requesting Party's effective Assignment Authority
    scope. The slice's scope-coverage relation is exact equality (the
    same relation :meth:`AuthorizationService._scope_covers` enforces
    for ``out-of-scope`` denials); the check runs before authorization
    evaluation so the deny path never reveals the Plan Revision's
    scope to an unauthorized caller.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        plan_revision_scope: The applicable scope observed on the
            Plan Revision row.
        requested_scope: The applicable scope the request submitted.
        failed_constraint: ``"target_plan_revision_scope_mismatch"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        plan_revision_scope: str,
        requested_scope: str,
        failed_constraint: str = "target_plan_revision_scope_mismatch",
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} has "
            f"applicable_scope={plan_revision_scope!r}, which is not "
            f"covered by the requested applicable_scope={requested_scope!r}; "
            "Requirement 23.4 requires the Plan Revision's scope to be "
            "within the requesting Party's Assignment Authority scope "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.plan_revision_scope = plan_revision_scope
        self.requested_scope = requested_scope
        self.failed_constraint = failed_constraint


class WorkAssignmentAssigneeNotResolvableError(LookupError):
    """Raised when the assignee Party Identity does not resolve.

    Requirement 23.5 requires the named assignee Party Identity to
    resolve to an existing Party. The check runs before authorization
    evaluation so the deny path never reveals whether a Party exists
    for an unauthorized caller. The slice schema does not yet model a
    Party ``inactive`` flag; only the existence branch of Requirement
    23.5 is exercised here. A future ADR may introduce a status column
    at which point an inactive branch will be added with a distinct
    ``failed_constraint``.

    Attributes:
        assignee_party_id: The Party Identity the caller supplied.
        failed_constraint: ``"assignee_party_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        assignee_party_id: str,
        failed_constraint: str = "assignee_party_not_resolvable",
    ) -> None:
        super().__init__(
            f"Assignee Party {assignee_party_id!r} did not resolve to an "
            f"existing Party (failed_constraint={failed_constraint!r})."
        )
        self.assignee_party_id = assignee_party_id
        self.failed_constraint = failed_constraint


class WorkAssignmentAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Work Assignment attempt.

    Mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 23.6 / Slice 1
    Requirement 10). The exception carries only ``reason_code`` and
    ``correlation_id`` — the indistinguishable-denial invariant forbids
    leaking authorized Party identities, target existence, or role
    assignment details beyond the requesting Party's view authority
    through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Work Assignment creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class WorkAssignmentAuditFailureError(RuntimeError):
    """Raised when every retry of the Denial Record append fails.

    Mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionAuditFailureError`.
    On total audit-append failure the exception is raised *in place
    of* :class:`WorkAssignmentAuthorizationError` — denial and audit
    have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no
    ``Work_Assignment_Records`` row, ``Relationships`` rows, or
    consequential audit row is persisted.

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
            f"Denial Record append for a denied Work Assignment failed after "
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
class CreateWorkAssignmentResult:
    """Result of :meth:`WorkAssignmentService.create_work_assignment`.

    Returned so callers (the HTTP layer in task 15.1, tests, the
    downstream Work Event / Time Entry / Deliverable Production
    services that target this Work Assignment, and the
    Provenance_Navigator that traverses the Execution Provenance
    Chain) can correlate the created Work Assignment Record with
    its two Relationship rows and its consequential audit row in
    one round-trip.

    Attributes:
        work_assignment_id: The Work Assignment Record Identity
            (UUIDv7).
        target_plan_revision_id: The target Approved Plan Revision
            Identity; copied byte-equivalent from the request input.
        assignee_party_id: The assignee Party Identity; copied
            byte-equivalent from the request input.
        assignment_authority_party_id: The Assignment Authority
            Party Identity; copied byte-equivalent from the request
            input.
        assignment_rationale: The persisted assignment rationale
            (0..4000 chars) or ``None`` when omitted.
        authority_basis: The validated :class:`AuthorityBasisRef`
            recorded on the Work Assignment Record.
        applicable_scope: Scope identifier the Work Assignment
            applies within.
        addresses_relationship_id: Identity of the single
            ``Addresses`` ``Relationships`` row inserted alongside
            the Work Assignment binding it to the target Plan
            Revision.
        relates_to_relationship_id: Identity of the single
            ``Relates To`` ``Relationships`` row inserted alongside
            the Work Assignment binding it to the assignee Party
            with ``semantic_role='assignee'``.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Work_Assignment_Records`` row, the two
            ``Relationships`` rows, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    work_assignment_id: str
    target_plan_revision_id: str
    assignee_party_id: str
    assignment_authority_party_id: str
    assignment_rationale: Optional[str]
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    addresses_relationship_id: str
    relates_to_relationship_id: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkAssignmentService:
    """Persist immutable Work Assignment Records and their ``Addresses``
    + ``Relates To`` Relationships per AD-WS-26.

    Like :class:`walking_slice.planning.plan_revisions.PlanRevisionService`,
    this service is connection-scoped at call time:
    :meth:`create_work_assignment` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (Slice 1 AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.WorkAssignments" declares it
    ``@dataclass(frozen=True)`` — Slice 3 service instances follow the
    Slice 2 convention of being immutable container objects that
    bundle their collaborators.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Work_Assignment_Records``, two ``Relationships``, and
            ``Audit_Records`` rows. The clock is consulted exactly
            once per write so every artifact of the transaction
            shares one timestamp.
        identity_service: Generates the Work Assignment Record
            Identity (UUIDv7) and the two Relationship Identities,
            plus drives the ``Identifier_Registry`` binding via
            :func:`_record_execution_artifact` (the Work Assignment
            binding carries the Slice 3
            ``resource_kind='work_assignment_record'`` tag per
            AD-WS-28).
        audit_log: Appends the consequential audit row
            (Requirement 23.8) inside the caller's transaction.
        authorization_service: Evaluates ``create.work_assignment``
            authority per AD-WS-24 / Requirement 23.6; the deny path
            is the Slice 1 separate-transaction Denial-Record pattern.
        planning_reader: Slice 2 :class:`PlanRevisionService` whose
            :meth:`PlanRevisionService.get_plan_revision` read API
            (the AD-WS-30 entry point) is consulted to resolve the
            target Plan Revision and verify its lifecycle state and
            applicable scope. The service is consulted in
            read-only mode; no write surface is exercised.
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
    planning_reader: PlanRevisionService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_work_assignment(
        self,
        connection: Connection,
        *,
        target_plan_revision_id: str,
        assignee_party_id: str,
        assignment_authority_party_id: str,
        assignment_rationale: Optional[str],
        authority_basis: Any,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateWorkAssignmentResult:
        """Create an immutable Work Assignment Record and its two
        Relationships per AD-WS-26.

        Per Requirements 23.1 through 23.9, AD-WS-9 (indistinguishable
        denial), AD-WS-24 (``create.work_assignment`` → ``assign``),
        AD-WS-26 (Relationship-Type / semantic-role table), AD-WS-27
        (append-only Slice 3 tables), AD-WS-28 (additive
        ``resource_kind`` values), and AD-WS-30 (Planning_Service
        read API):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
        2. Input validation (Requirement 23.3 / 23.7) — every range,
           required-attribute, and authority-basis-enumeration check
           runs before any database read so a malformed request never
           touches identity service, the Planning_Service, the
           ``Parties`` lookup, or the authorization service.
        3. Reject self-assignment when
           ``assignment_authority_party_id == assignee_party_id``
           (Requirement 23.5).
        4. Resolve the target Plan Revision via
           :meth:`PlanRevisionService.get_plan_revision`. Reject when
           unresolvable
           (:class:`WorkAssignmentPlanRevisionNotResolvableError`),
           when ``lifecycle_state != 'approved'``
           (:class:`WorkAssignmentPlanRevisionNotApprovedError`), or
           when the Plan Revision's applicable scope is not covered
           by the requesting Party's applicable scope
           (:class:`WorkAssignmentPlanRevisionScopeMismatchError`).
           Requirement 23.4 specifies the rejection path.
        5. Resolve the assignee Party via a single SELECT against
           ``Parties``. Reject when unresolvable
           (:class:`WorkAssignmentAssigneeNotResolvableError`) per
           Requirement 23.5.
        6. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`WorkAssignmentAuditFailureError` in place of
           :class:`WorkAssignmentAuthorizationError`.
        7. On ``permit``, mint the Work Assignment Record Identity
           and the two Relationship Identities and register the Work
           Assignment Identity in ``Identifier_Registry`` (kind
           ``'immutable_record'``, carrying the Slice 3
           ``resource_kind='work_assignment_record'`` tag per
           AD-WS-28) via :func:`_record_execution_artifact`.
        8. INSERT the ``Work_Assignment_Records`` row carrying every
           Requirement 23.3 attribute (the schema-level
           ``assignee_party_id != assignment_authority_party_id``
           CHECK constraint provides the database-layer guard for
           Requirement 23.5).
        9. INSERT exactly one ``Relationships`` row with
           ``relationship_type='Addresses'``,
           ``source_kind='work_assignment_record'`` /
           ``source_id=work_assignment_id`` /
           ``source_revision_id=NULL``,
           ``target_kind='plan_revision'`` /
           ``target_id=target_plan_revision_id`` /
           ``target_revision_id=NULL`` (Plan Revisions live in a
           single Revision-level table per Slice 2; the binding
           lives on ``target_id`` only), and ``semantic_role=NULL``.
        10. INSERT exactly one ``Relationships`` row with
            ``relationship_type='Relates To'``,
            ``source_kind='work_assignment_record'``,
            ``target_kind='party'`` / ``target_id=assignee_party_id``
            / ``target_revision_id=NULL``, and
            ``semantic_role='assignee'``.
        11. Append the consequential ``Audit_Records`` row with
            ``action_type='create.work_assignment'`` and
            ``target_id=work_assignment_id`` inside the same
            transaction (Requirement 23.8).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_plan_revision_id: Identity of the target Approved
                Plan Revision (Requirement 23.3). Must resolve to an
                existing ``Plan_Revisions`` row whose lifecycle state
                is ``'approved'`` and whose applicable scope matches
                the requested scope.
            assignee_party_id: Identity of the assignee Contributor
                Party (Requirement 23.3). Must resolve to an existing
                ``Parties`` row and must not equal
                ``assignment_authority_party_id`` (Requirement 23.5).
            assignment_authority_party_id: Identity of the Assignment
                Authority Party initiating this Work Assignment.
                Persisted on
                ``Work_Assignment_Records.assignment_authority_party_id``
                and used as the actor for the authorization evaluation
                and the consequential audit row.
            assignment_rationale: Optional assignment rationale of
                0..4000 characters, or ``None`` when omitted
                (Requirement 23.3). The schema column is NULLable;
                empty strings are accepted and persisted verbatim.
            authority_basis: Authority basis recorded on the Work
                Assignment Record. Accepted as either
                :class:`AuthorityBasisRef` or a mapping convertible to
                one; the ``type`` must be drawn from
                ``{role-grant-id, scope-id, delegation-chain-id}``
                per AD-WS-10 / Requirement 23.3.
            applicable_scope: Scope identifier the Work Assignment
                applies within (Requirement 23.3). Persisted on
                ``Work_Assignment_Records.applicable_scope`` and
                passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback (Slice 1 Requirement
                7.6). The same engine is used to open a fresh
                transaction for the authorization evaluation itself.
            correlation_id: Optional correlation identifier shared
                by every audit row written in this operation. A
                UUIDv7 is generated when omitted.
            evaluation_at: Optional explicit effective time passed
                to :meth:`AuthorizationService.evaluate` as the
                ``at`` parameter. Defaults to the recorded time of
                this transaction.
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
            :class:`CreateWorkAssignmentResult` carrying the
            persisted Work Assignment Identity, every persisted
            attribute, the two Relationship Identities, the recorded
            time, and the correlation identifier.

        Raises:
            WorkAssignmentValidationError: A required attribute is
                missing, a Requirement 23.3 range was violated, the
                authority basis is malformed, or the request body
                carried a prohibited planning-attribute or
                observed-outcome key.
            WorkAssignmentSelfAssignmentError: The request named the
                requesting Party as its own assignee (Requirement
                23.5).
            WorkAssignmentPlanRevisionNotResolvableError: The target
                Plan Revision Identity did not resolve to an
                existing Plan Revision (Requirement 23.4).
            WorkAssignmentPlanRevisionNotApprovedError: The target
                Plan Revision exists but its lifecycle state is not
                ``'approved'`` at the recorded time (Requirement
                23.2 / 23.4).
            WorkAssignmentPlanRevisionScopeMismatchError: The target
                Plan Revision's applicable scope is not covered by
                the requesting Party's applicable scope
                (Requirement 23.4).
            WorkAssignmentAssigneeNotResolvableError: The assignee
                Party Identity did not resolve to an existing Party
                (Requirement 23.5).
            WorkAssignmentAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 23.6). The Denial Record was appended
                successfully in a separate transaction.
            WorkAssignmentAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry. Replaces
                :class:`WorkAssignmentAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``assignment_authority_party_id`` does not name an
                existing ``Parties`` row). The surrounding
                transaction MUST be allowed to roll back per
                Slice 1 Requirement 13.6 / Requirement 23.8.
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
                # Surface the prohibited keys through the structured
                # error type the route layer expects; preserves the
                # tuple of offending keys so the response can list
                # each per Requirements 33.4 / 34.5.
                raise WorkAssignmentValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 23.3 / 23.7) before any
        # database read or authorization side-effect. Each validator
        # raises :class:`WorkAssignmentValidationError` with a stable
        # ``failed_constraint`` so the HTTP layer (task 15.1) can
        # render structured 400 responses identifying the invalid
        # attribute.
        self._validate_required_strings(
            target_plan_revision_id=target_plan_revision_id,
            assignee_party_id=assignee_party_id,
            assignment_authority_party_id=assignment_authority_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_assignment_rationale(assignment_rationale)
        normalized_basis = self._validate_authority_basis(authority_basis)

        # 3. Reject self-assignment per Requirement 23.5. The
        # schema-level CHECK constraint on
        # ``Work_Assignment_Records`` would also reject this at
        # INSERT time, but surfacing the rejection here yields a
        # deterministic structured error before any database read
        # and lets the deny path not reveal authorization or
        # existence information for non-self requests.
        if assignment_authority_party_id == assignee_party_id:
            raise WorkAssignmentSelfAssignmentError(
                party_id=assignment_authority_party_id,
            )

        # 4. Resolve the target Plan Revision via the AD-WS-30 read
        # API. The Planning_Service is the only authoritative source
        # for Plan Revision lifecycle state; the Execution_Service
        # never queries ``Plan_Revisions`` directly per Principle
        # 5.2 and the Slice 3 cross-context rule. The lookup runs on
        # the caller's connection so it participates in the caller's
        # transactional view.
        plan_revision = self.planning_reader.get_plan_revision(
            connection, target_plan_revision_id
        )
        if plan_revision is None:
            raise WorkAssignmentPlanRevisionNotResolvableError(
                target_plan_revision_id=target_plan_revision_id,
            )
        self._validate_plan_revision(
            plan_revision,
            requested_scope=applicable_scope,
        )

        # 5. Resolve the assignee Party. The slice schema does not
        # carry a Party ``inactive`` flag, so only the existence
        # branch of Requirement 23.5 is exercised here. The lookup
        # is a single indexed SELECT against ``Parties``.
        assignee_resolved = connection.execute(
            text(
                "SELECT party_id FROM Parties "
                "WHERE party_id = :party_id"
            ),
            {"party_id": assignee_party_id},
        ).scalar_one_or_none()
        if assignee_resolved is None:
            raise WorkAssignmentAssigneeNotResolvableError(
                assignee_party_id=assignee_party_id,
            )

        # 6. Shared clock reading (design §"Cross-Cutting Concerns"
        # — *Transactionality*). The authorization evaluation row,
        # the ``Work_Assignment_Records`` row, the two
        # ``Relationships`` rows, and the consequential audit row
        # all share this timestamp; the optional ``evaluation_at``
        # parameter changes only *when* authority is evaluated *as
        # of*, not the recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 7. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 single-writer accommodation; the
        # deny path opens *another* separate transaction for the
        # Denial Record write, and the caller's transaction stays a
        # reader until step 8 below). The authorization target is
        # the target Plan Revision — ``create.work_assignment``
        # authority is scoped against the Plan Revision the Work
        # Assignment is being recorded against.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=assignment_authority_party_id,
                action=_ACTION_CREATE_WORK_ASSIGNMENT,
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
            self._persist_work_assignment_denial(
                engine=engine,
                actor_party_id=assignment_authority_party_id,
                target_plan_revision_id=target_plan_revision_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise WorkAssignmentAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 8. Mint identifiers (AD-WS-2 / AD-WS-28). Work Assignment
        # Records are Immutable Records (per [`02-domain-model.md`]
        # §8.2) so the Record identifier is minted via
        # :meth:`IdentityService.new_immutable_record_id`. The two
        # Relationship Identities are minted via
        # :meth:`IdentityService.new_relationship_id`.
        work_assignment_id = str(
            self.identity_service.new_immutable_record_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        relates_to_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Work Assignment
        # identifier in ``Identifier_Registry``; the digest is the
        # SHA-256 of the canonical JSON payload of the Record so two
        # different Work Assignment Records never collide on the
        # same digest. ``authority_basis.id`` is normalized to its
        # string form for the canonical payload because UUID
        # objects are not natively JSON-serializable.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_plan_revision_id": target_plan_revision_id,
                    "assignee_party_id": assignee_party_id,
                    "assignment_authority_party_id": (
                        assignment_authority_party_id
                    ),
                    "assignment_rationale": assignment_rationale,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 9. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-28 ``resource_kind='work_assignment_record'``
        # tag. This is the row-level discriminator that keeps the
        # Work Assignment Identity set inspectably disjoint from
        # every other Slice 1 / Slice 2 / Slice 3 ``resource_kind``
        # (Requirement 22.8). The helper delegates to
        # :meth:`IdentityService.reject_if_duplicate` so the Slice 1
        # identifier-conflict Denial Record pathway fires on any
        # collision; on success the helper INSERTs one row inside
        # the caller's transaction.
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_WORK_ASSIGNMENT,
            work_assignment_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=assignment_authority_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_WORK_ASSIGNMENT,
            recorded_time=recorded_time,
        )

        # 10. Insert the Work Assignment Record. Every Requirement
        # 23.3 attribute lands here. The
        # ``authority_basis`` is destructured into
        # ``authority_basis_type`` and ``authority_basis_id`` columns
        # so the schema-level CHECK on ``authority_basis_type``
        # enforces the AD-WS-10 enumeration at the database layer as
        # well.
        connection.execute(
            text(
                """
                INSERT INTO Work_Assignment_Records (
                    work_assignment_id, target_plan_revision_id,
                    assignee_party_id, assignment_authority_party_id,
                    assignment_rationale,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :work_assignment_id, :target_plan_revision_id,
                    :assignee_party_id, :assignment_authority_party_id,
                    :assignment_rationale,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "work_assignment_id": work_assignment_id,
                "target_plan_revision_id": target_plan_revision_id,
                "assignee_party_id": assignee_party_id,
                "assignment_authority_party_id": (
                    assignment_authority_party_id
                ),
                "assignment_rationale": assignment_rationale,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 11. Insert the ``Addresses`` Relationship row binding the
        # Work Assignment to the target Plan Revision (AD-WS-26 row
        # 1). Plan Revisions live in a single Revision-level table
        # in Slice 2 so the target ``target_revision_id`` is NULL —
        # the Plan Revision *is* the Revision-level row and its
        # primary key on ``target_id`` is sufficient.
        # ``semantic_role`` is NULL per AD-WS-26.
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
                "source_kind": _KIND_WORK_ASSIGNMENT_RECORD,
                "source_id": work_assignment_id,
                "source_revision_id": None,
                "target_kind": _KIND_PLAN_REVISION,
                "target_id": target_plan_revision_id,
                "target_revision_id": None,
                "authoring_party_id": assignment_authority_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 12. Insert the ``Relates To`` Relationship row binding the
        # Work Assignment to the assignee Party with
        # ``semantic_role='assignee'`` (AD-WS-26 row 2). The
        # ``semantic_role`` discriminator is the value the
        # Provenance_Navigator backlink algorithm looks for to
        # return the assignee Party when given a Work Assignment
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
                "source_kind": _KIND_WORK_ASSIGNMENT_RECORD,
                "source_id": work_assignment_id,
                "source_revision_id": None,
                "target_kind": _KIND_PARTY,
                "target_id": assignee_party_id,
                "target_revision_id": None,
                "authoring_party_id": assignment_authority_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_ASSIGNEE,
            },
        )

        # 13. Append the consequential audit row (Requirement 23.8 /
        # Slice 1 AD-WS-5). Participates in the caller's transaction
        # so a failure here rolls back the registry, the
        # ``Work_Assignment_Records`` row, and the two
        # ``Relationships`` rows together. ``target_id`` is the
        # Work Assignment Record Identity; ``target_revision_id`` is
        # ``None`` because Work Assignment Records are
        # Record-scoped (Requirement 22.2 — no separate Revision).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=assignment_authority_party_id,
            action_type=_ACTION_CREATE_WORK_ASSIGNMENT,
            target_id=work_assignment_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateWorkAssignmentResult(
            work_assignment_id=work_assignment_id,
            target_plan_revision_id=target_plan_revision_id,
            assignee_party_id=assignee_party_id,
            assignment_authority_party_id=assignment_authority_party_id,
            assignment_rationale=assignment_rationale,
            authority_basis=normalized_basis,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            relates_to_relationship_id=relates_to_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_required_strings(
        *,
        target_plan_revision_id: Any,
        assignee_party_id: Any,
        assignment_authority_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 23.7: a Work Assignment creation request that
        omits the target Plan Revision Identity, the assignee Party
        Identity, the Assignment Authority Party Identity, or the
        applicable scope is rejected with no Work Assignment Record
        created. Each missing attribute surfaces a distinct
        ``failed_constraint`` so the HTTP layer can identify the
        precise field to the client.
        """
        if not target_plan_revision_id or not isinstance(
            target_plan_revision_id, str
        ):
            raise WorkAssignmentValidationError(
                "target_plan_revision_id is required; Requirement 23.7 "
                "rejects Work Assignments missing the target Plan Revision "
                "Identity.",
                failed_constraint="target_plan_revision_id_missing",
            )
        if not assignee_party_id or not isinstance(assignee_party_id, str):
            raise WorkAssignmentValidationError(
                "assignee_party_id is required; Requirement 23.7 rejects "
                "Work Assignments missing the assignee Party Identity.",
                failed_constraint="assignee_party_id_missing",
            )
        if not assignment_authority_party_id or not isinstance(
            assignment_authority_party_id, str
        ):
            raise WorkAssignmentValidationError(
                "assignment_authority_party_id is required; Requirement 23.6 "
                "rejects unauthenticated Work Assignment creation.",
                failed_constraint="assignment_authority_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise WorkAssignmentValidationError(
                "applicable_scope is required; Requirement 23.7 rejects "
                "Work Assignments missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_assignment_rationale(assignment_rationale: Any) -> None:
        """Reject assignment rationale outside the Requirement 23.3 range.

        Per Requirement 23.3 the assignment rationale is 0..4000
        characters and optional. ``None`` is accepted (the column is
        NULLable) and persisted as SQL ``NULL``; the empty string is
        also accepted (length 0 satisfies the 0 lower bound) and
        persisted verbatim. The schema-level CHECK constraint
        ``length(assignment_rationale) BETWEEN 0 AND 4000`` enforces
        the same range at the database layer.
        """
        if assignment_rationale is None:
            return
        if not isinstance(assignment_rationale, str):
            raise WorkAssignmentValidationError(
                "assignment_rationale must be a str or None; received "
                f"{type(assignment_rationale).__name__}.",
                failed_constraint="assignment_rationale_invalid_type",
            )
        if len(assignment_rationale) > _ASSIGNMENT_RATIONALE_MAX_CHARS:
            raise WorkAssignmentValidationError(
                f"assignment_rationale length {len(assignment_rationale)} "
                f"exceeds the {_ASSIGNMENT_RATIONALE_MAX_CHARS}-character "
                "limit imposed by Requirement 23.3.",
                failed_constraint="assignment_rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 23.3 / AD-WS-10: the authority basis ``type``
        is drawn from ``{role-grant-id, scope-id, delegation-chain-id}``.
        The Python-typed signature already constrains callers to pass
        an :class:`AuthorityBasisRef` whose ``type`` Literal restricts
        the enumeration; the HTTP layer (task 15.1) may pass a dict
        if it has not yet bound the request to the typed model, so
        this validator coerces both shapes (mirroring the Slice 2
        plan_reviews / plan_approvals validator).
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            # The Pydantic Literal already constrains type to the
            # AD-WS-10 set, and Pydantic UUID validation ensures id
            # is well-formed. Return as-is.
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise WorkAssignmentValidationError(
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
            raise WorkAssignmentValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise WorkAssignmentValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise WorkAssignmentValidationError(
                "authority_basis.id is required per Requirement 23.3.",
                failed_constraint="authority_basis_id_missing",
            )

        # Delegate canonical-form validation (UUID shape) to Pydantic
        # by constructing the typed model. Any malformed value will
        # raise a Pydantic ValidationError which we surface as a
        # :class:`WorkAssignmentValidationError` with the closest
        # applicable failed_constraint.
        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise WorkAssignmentValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    @staticmethod
    def _validate_plan_revision(
        plan_revision: PlanRevisionRow,
        *,
        requested_scope: str,
    ) -> None:
        """Verify the Plan Revision is approved and scope-covered.

        Per Requirement 23.2: the Plan Revision's lifecycle state at
        the recorded time must be ``'approved'`` per Slice 2
        Requirement 9.1. Per Requirement 23.4: the Plan Revision's
        applicable scope must be within the requesting Party's
        applicable scope. The slice's scope-coverage relation is exact
        equality (the same relation
        :meth:`AuthorizationService._scope_covers` enforces for
        ``out-of-scope`` denials); widening the relation is a future
        change inside the AuthorizationService alone.
        """
        if plan_revision.lifecycle_state != _LIFECYCLE_APPROVED:
            raise WorkAssignmentPlanRevisionNotApprovedError(
                target_plan_revision_id=plan_revision.plan_revision_id,
                observed_lifecycle_state=plan_revision.lifecycle_state,
            )
        if plan_revision.applicable_scope != requested_scope:
            raise WorkAssignmentPlanRevisionScopeMismatchError(
                target_plan_revision_id=plan_revision.plan_revision_id,
                plan_revision_scope=plan_revision.applicable_scope,
                requested_scope=requested_scope,
            )

    # -- denial side-channel ----------------------------------------------

    def _persist_work_assignment_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_plan_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Work Assignment attempt.

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
          :class:`WorkAssignmentAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_work_assignment` raises
        :class:`WorkAssignmentAuthorizationError` (or this method
        raises :class:`WorkAssignmentAuditFailureError`). The Denial
        Record must therefore live outside that scope to survive
        (AD-WS-9). The denial row's ``target_id`` is the Plan
        Revision identifier rather than the (not-yet-minted) Work
        Assignment identifier; this matches the AD-WS-9 contract
        that denial rows reference the resolved Resource the
        unauthorized action was attempted against.

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
                        attempted_action=_ACTION_CREATE_WORK_ASSIGNMENT,
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

        raise WorkAssignmentAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this
# module does not import private names from sibling services. The
# functions are intentionally identical to their Slice 2 siblings:
# correlation identifiers are non-domain values and the digest is
# opaque to :class:`Identifier_Registry` — both implementations could
# be replaced with shared utility functions in a future refactor
# without changing observable behavior.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Work
    Assignment creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Work Assignment
    Identity in ``Identifier_Registry``. Work Assignment Records are
    Record-scoped (Requirement 22.2 — no separate Revision) so this
    digest is bound exactly once per Work Assignment creation.
    """
    return hashlib.sha256(content).hexdigest()
