"""Execution_Service.Completions — immutable Completion Records keyed
to an Approved Plan Revision.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Execution_Service.Completions" — public dataclass surface,
  authority string (``create.completion`` → ``complete`` per
  Requirement 32.9), AD-WS-9 separate-transaction Denial Record on
  deny, validation order, the Relationship-row contract (exactly one
  ``Addresses`` Relationship from the Completion Record to the target
  Approved Plan Revision with ``semantic_role IS NULL``), and the
  Accepted-Milestone existence covering query.
- §"Accepted-Milestone existence check (Requirement 29.1, 29.4)" —
  the covering ``SELECT COUNT(*)`` joining
  ``Milestone_Acceptance_Records`` → ``Deliverable_Production_Records``
  → ``Work_Assignment_Records`` filtered by
  ``wa.target_plan_revision_id`` and
  ``mar.outcome = 'Accept'``. The query result must be ``>= 1``; if
  ``source_milestone_acceptance_ids`` is supplied, every entry must
  appear in the result set as an ``Accept``-outcome row.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with
  ``kind = 'immutable_record'`` and
  ``resource_kind = 'completion_record'`` per AD-WS-28);
  Authorization (the action string ``create.completion`` maps to the
  ``complete`` authority per Requirement 32.9; the deny path uses the
  Slice 1 separate-transaction Denial-Record pattern with the
  Slice 3 Requirement 30.6 three-retry contract).
- §"Error Handling — Duplicate / uniqueness violations" — the
  ``UNIQUE(target_plan_revision_id)`` constraint on
  ``Completion_Records`` (Requirement 29.3) is pre-checked in the
  service and surfaced as a structured ``completion_already_exists``
  conflict. The existing Completion Identity is exposed on the
  conflict response only when the caller holds view authority on it
  (AD-WS-9 / Slice 3 Requirement 30.4); otherwise the conflict body
  is byte-equivalent to a response that lacks the field,
  indistinguishable from a non-existent endpoint per Slice 1 design
  §"Indistinguishable denial observability".
- AD-WS-26 — Completion Records carry exactly one ``Addresses``
  Relationship to the target Approved Plan Revision with
  ``semantic_role IS NULL`` (Requirement 29.2; consistent with
  Slice 1 §10.9).
- AD-WS-27 — ``Completion_Records`` is append-only; the target Plan
  Revision, Activity Plan, Project, Objective, Intended Outcome,
  Deliverable Expectation, Plan Approval Record, and every Slice 1 /
  Slice 2 row must remain byte-equivalent throughout this
  transaction (Requirement 29.7 / Requirement 40, Property 11).
- AD-WS-28 — additive ``Identifier_Registry.resource_kind`` value
  ``'completion_record'`` populated through
  :func:`walking_slice.execution._helpers._record_execution_artifact`.
- AD-WS-30 — the only Planning_Service entry points this module
  consults are :meth:`PlanRevisionService.get_plan_revision` (a
  single indexed SELECT on ``Plan_Revisions``) and
  :meth:`ProjectResolver.resolve_project` (a single indexed JOIN
  through ``Plan_Revisions`` → ``Activity_Plans``). Slice 2 tables
  are never written by this module.

This is **not** a Contributor write. ``create.completion`` requires
the ``complete`` authority (Requirement 32.9) and does **not**
trigger the AD-WS-29 second-stage assignee-binding check that
applies to Contributor writes (``create.work_event``,
``create.time_entry``, ``create.produced_deliverable``,
``create.deliverable_production``). A Completion Authority is by
design a Party distinct from the assignees on the Work Assignment
Records that produced the accepted Milestones being rolled up.

Task scope (task 11.1)
======================

This module implements
:meth:`CompletionService.create_completion`:

1. Defensively reject any prohibited planning-attribute or
   observed-outcome key in the original request body via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5; the
   observed-outcome screen is especially load-bearing here because a
   Completion Record is the entity most likely to be aliased into an
   observed Outcome — Requirement 29.8 / Requirement 34.3).
2. Validate request inputs per Requirement 29.2 / 29.4:
   ``target_plan_revision_id``, ``completing_party_id``, and
   ``applicable_scope`` are present;
   ``outcome ∈ {'Completed', 'Completed_With_Reservation'}``;
   ``rationale`` length is 1..4000 characters;
   ``authority_basis.type`` is drawn from the AD-WS-10 set
   ``{role-grant-id, scope-id, delegation-chain-id}``;
   ``source_milestone_acceptance_ids`` is a sequence of strings (may
   be empty).
3. Resolve the target Plan Revision through
   :meth:`PlanRevisionService.get_plan_revision`. Reject when
   unresolvable (Requirement 29.4) or when
   ``lifecycle_state != 'approved'`` (Requirement 29.4 — only an
   Approved Plan Revision may be completed). The rejection runs
   before authorization evaluation so the deny path cannot reveal
   whether the Plan Revision exists or its lifecycle state.
4. Pre-check the ``UNIQUE(target_plan_revision_id)`` constraint per
   Requirement 29.3 / 29.4. When a Completion already exists for the
   target Plan Revision:

   - Evaluate
     ``Authorization_Service.evaluate(party=completing_party_id,
     action="view.completion", target=existing_completion, at=now())``
     on a separate transaction.
   - When the evaluation permits view, raise
     :class:`CompletionConflictError` with the existing
     ``completion_id`` populated so the HTTP layer can return a
     structured 409 ``completion_already_exists`` response
     identifying the existing Identity.
   - When the evaluation denies view, raise
     :class:`CompletionConflictError` with the existing
     ``completion_id`` set to ``None`` so the conflict response is
     byte-equivalent to a response that lacks the existing-Identity
     field (AD-WS-9 / Slice 3 Requirement 30.4).

   The pre-check runs after target-resolution but before the main
   ``create.completion`` authorization evaluation so a subsequent
   unauthorized caller cannot distinguish a uniqueness conflict from
   a missing authority via the authorization audit trail.
5. Run the Accepted-Milestone existence check per Requirement 29.1
   / 29.4 using the covering SQL from design §"Accepted-Milestone
   existence check". The query joins
   ``Milestone_Acceptance_Records`` → ``Deliverable_Production_Records``
   → ``Work_Assignment_Records`` and counts (and lists) every
   ``Accept``-outcome Milestone Acceptance whose source Work
   Assignment targets the same Plan Revision. The query result row
   count must be ``>= 1``; when zero, reject with
   :class:`CompletionNoAcceptedMilestonesError`. When
   ``source_milestone_acceptance_ids`` is supplied, every supplied
   identifier must appear in the result set; an absent identifier
   rejects with
   :class:`CompletionSourceMilestoneAcceptanceNotResolvableError`
   identifying the first offending entry.
6. Resolve the target Activity Plan / Project Identities via
   :meth:`PlanRevisionService.get_plan_revision` (which already
   returns the Plan Revision's parent ``activity_plan_id``) and
   :meth:`ProjectResolver.resolve_project` (which walks Plan
   Revision → Activity Plan → Project). The Activity Plan and
   Project rows are not modified — only read — so Requirement 29.7
   / Requirement 40 byte-equivalence is preserved.
7. Evaluate ``Authorization_Service.evaluate(party=completing_party_id,
   action="create.completion", target=plan_revision_ref, at=now())``
   on a separate transaction. On ``deny``, persist a Denial Record
   from another separate transaction with the Slice 1 Requirement
   7.6 / Slice 3 Requirement 30.6 three-retry exponential-backoff
   pattern and raise :class:`CompletionAuthorizationError`.
8. On ``permit``, mint the Completion Record Identity (UUIDv7) and
   the ``Addresses`` Relationship Identity; register the Completion
   Identity in ``Identifier_Registry`` with
   ``kind='immutable_record'`` and
   ``resource_kind='completion_record'`` via
   :func:`_record_execution_artifact`.
9. INSERT the ``Completion_Records`` row carrying every Requirement
   29.2 attribute, including the target Activity Plan / Project
   Identities resolved in step 6 and the JSON-encoded
   ``source_milestone_acceptance_ids`` list.
10. INSERT exactly one ``Relationships`` row with
    ``relationship_type='Addresses'``,
    ``source_kind='completion_record'``,
    ``target_kind='plan_revision'``, and ``semantic_role IS NULL``
    per AD-WS-26 and Requirement 29.2 / Slice 1 §10.9. The
    ``target_id`` is the Plan Revision Identity and
    ``target_revision_id`` is ``NULL`` because Plan Revisions live
    in a single Revision-level table per Slice 2 (no separate
    Resource header / Revision identifier split).
11. Append the consequential ``Audit_Records`` row with
    ``action_type='create.completion'`` and
    ``target_id=completion_id`` inside the same transaction
    (Requirement 29.6 / Slice 1 AD-WS-5).

Rows are inserted in dependency order so a FK failure anywhere rolls
back the whole transaction (Requirement 29.7).

The target Plan Revision, Activity Plan, Project, Objective,
Intended Outcome, Deliverable Expectation, Plan Approval Record,
every Slice 1 row, and every Slice 2 row remain byte-equivalent
throughout the transaction. The service never issues an UPDATE,
INSERT, or DELETE against any of those rows (Requirement 29.7 /
Requirement 40, Property 11). The append-only triggers created in
task 1.2 enforce immutability of ``Completion_Records`` itself after
commit.

Per Requirement 29.8 / Requirement 34.3 a Completion Record never
asserts an observed Outcome. The Record carries only the planning
attribute ``target_plan_revision_id`` (the explicit Requirement 33.2
exception clause) and otherwise observes the same prohibited-prefix
contract every Execution_Service request enforces — observed-outcome
keys on the request body are rejected before any row is persisted
(Requirement 34.1 / 34.5).

Requirements satisfied
======================

    29.1 — authorized Completion creation produces exactly one
           immutable Completion Record within nominal latency,
           conditional on at least one accepted Milestone for the
           same target Plan Revision (covering query in step 5).
    29.2 — every Completion Record records the target Approved Plan
           Revision Identity, the target Activity Plan Resource
           Identity, the target Project Resource Identity, the
           completion outcome (``Completed`` /
           ``Completed_With_Reservation``), the completion rationale
           (1..4000 chars), the optional list of source Milestone
           Acceptance Identities, the completing Party Identity, the
           authority basis (AD-WS-10 enumeration), the applicable
           scope, the recorded time, and exactly one ``Addresses``
           Relationship to the target Plan Revision.
    29.3 — at most one Completion Record per target Approved Plan
           Revision. The schema-level
           ``UNIQUE(target_plan_revision_id)`` constraint is the
           source of truth; the application-level pre-check surfaces
           a structured ``CompletionConflictError`` and applies the
           AD-WS-9 view-authority gate on the existing Identity.
    29.4 — unresolvable target Plan Revision, non-approved Plan
           Revision, duplicate Completion against the same Plan
           Revision, ``outcome`` outside the enumerated set, missing
           rationale, missing authority basis, missing applicable
           scope, and zero accepted Milestones for the target Plan
           Revision (or a supplied
           ``source_milestone_acceptance_ids`` entry that does not
           resolve to an ``Accept``-outcome row in the existence
           query) are rejected with no Completion Record persisted.
    29.5 — unauthorized requests are denied via
           :class:`AuthorizationService`; the Execution_Service
           declines to create any Completion Record and the
           Audit_Log appends a Denial Record conforming to AD-WS-9.
    29.6 — the Audit_Log appends an immutable consequential audit
           row identifying the Completion Record Identity, target
           Approved Plan Revision Identity, completion outcome,
           completing Party Identity, authority basis, source
           Milestone Acceptance Identities, and recorded time within
           the same transaction.
    29.7 — the target Plan Revision, target Activity Plan, target
           Project, target Objective, target Intended Outcome,
           target Deliverable Expectation, Plan Approval Record,
           and every Slice 1 / Slice 2 row remain byte-equivalent
           throughout this transaction. The service issues no
           UPDATE, INSERT, or DELETE against any of those rows;
           the append-only triggers (created in task 1.2) enforce
           immutability of ``Completion_Records`` itself after the
           transaction commits.
    29.8 — the Completion Record does not assert, imply, or alias
           any observed Outcome, Measurement Record,
           success-condition assessment, or attribution-evidence
           reference. The prohibited-attribute screen in step 1
           rejects every observed-outcome prefix on the request
           body; the persisted columns carry no observed-outcome
           field.
    32.9 — ``create.completion`` requires the ``complete`` authority.
    41.1 — every consequential write is atomic with its consequential
           audit row.
    41.10 — every Slice 3 row that references an existing Slice 1 /
            Slice 2 row leaves those rows byte-equivalent.
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
from walking_slice.execution._helpers import (
    ALL_PROHIBITED_PREFIXES,
    ExecutionValidationError,
    _record_execution_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.planning._project_resolver import (
    PlanRevisionNotResolvableError,
    ProjectResolver,
)
from walking_slice.planning.plan_revisions import PlanRevisionService


__all__ = [
    "CompletionAuditFailureError",
    "CompletionAuthorizationError",
    "CompletionConflictError",
    "CompletionNoAcceptedMilestonesError",
    "CompletionPlanRevisionNotApprovedError",
    "CompletionPlanRevisionNotResolvableError",
    "CompletionService",
    "CompletionSourceMilestoneAcceptanceNotResolvableError",
    "CompletionValidationError",
    "CompletionRecordRow",
    "CreateCompletionResult",
    "OUTCOME_VALUES",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship Type / kind / semantic-role strings,
# registry kind / resource_kind strings, lifecycle literal, and
# validation limits are pulled out as module-level ``Final`` so the
# names downstream property tests look for in
# ``Audit_Records.action_type``, in
# ``Identifier_Registry.resource_kind``, and in
# ``Relationships.semantic_role`` are textually stable.
# ---------------------------------------------------------------------------


# ``create.completion`` maps to the ``complete`` authority per
# Requirement 32.9. The string is also the ``action_type`` recorded
# on the consequential audit row (Requirement 29.6) and on the
# separate-transaction Denial Record so audit consumers can correlate
# denial rows with the action a Party was attempting.
_ACTION_CREATE_COMPLETION: Final[str] = "create.completion"

# ``view.completion`` is the action used by the conflict-pre-check
# view-authority gate (AD-WS-9 / Slice 3 Requirement 30.4). Mapped to
# the ``view`` authority by
# :func:`walking_slice.authorization._required_authority`'s prefix
# fallback.
_ACTION_VIEW_COMPLETION: Final[str] = "view.completion"

# Relationship Type strings written to the ``Relationships`` rows
# this module reads and writes.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"

# ``Relationships.source_kind`` / ``target_kind`` strings per
# AD-WS-26. The Completion Record is the source of one ``Addresses``
# Relationship; the target is the target Approved Plan Revision and
# ``semantic_role`` is NULL (the ``Addresses`` Relationship Type
# carries no role discriminator per the AD-WS-26 table — consistent
# with Slice 1 ``Addresses`` rows and with the Milestone Acceptance
# Record's ``Addresses`` row to the produced Deliverable Revision).
_KIND_COMPLETION_RECORD: Final[str] = "completion_record"
_KIND_PLAN_REVISION: Final[str] = "plan_revision"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Execution_Service ``resource_kind`` tag (Slice 3 additive
# enumeration per AD-WS-28). Completion Records are Governance
# Decision Immutable Records (per ``02-domain-model.md`` §8.5) so the
# registry kind is ``'immutable_record'``; the ``resource_kind``
# value is ``'completion_record'`` and is the row-level discriminator
# that keeps the Completion Identity set inspectably disjoint from
# every other Slice 1 / Slice 2 / Slice 3 ``resource_kind``
# (Requirement 22.8).
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_COMPLETION: Final[str] = "completion_record"

# Authority-basis ``type`` enumeration per AD-WS-10. Mirrors the
# Slice 3 ``Completion_Records.authority_basis_type`` CHECK
# constraint values; centralizing the tuple here lets the validator
# reject malformed requests structurally before they touch SQL.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Completion outcome enumeration per Requirement 29.2. The
# schema-level CHECK on ``Completion_Records.outcome`` enforces the
# same membership as a defense in depth. The tuple preserves the
# declared order for use in error messages.
OUTCOME_VALUES: Final[tuple[str, ...]] = (
    "Completed",
    "Completed_With_Reservation",
)

# Milestone Acceptance outcome that satisfies the accepted-Milestone
# existence check per Requirement 29.1 / 29.4. Only Acceptances
# carrying ``outcome = 'Accept'`` count toward the >= 1 covering
# threshold; ``Reject``-outcome Acceptances are explicitly excluded
# by the design §"Accepted-Milestone existence check" SQL.
_ACCEPT_OUTCOME: Final[str] = "Accept"

# Lifecycle state literal the target Plan Revision must carry at the
# recorded time per Requirement 29.4 / Slice 2 Requirement 9.1. The
# Work Assignment Service uses the same constant value; centralizing
# it here keeps the comparison textually identical across Slice 3
# services that gate on Plan Revision lifecycle.
_LIFECYCLE_APPROVED: Final[str] = "approved"

# Validation limits for ``rationale`` per Requirement 29.2 (1..4000
# characters). The ``Completion_Records.rationale`` CHECK constraint
# enforces the same range; surfacing the limits here yields a
# precise ``failed_constraint`` for the HTTP layer rather than a raw
# SQL constraint violation.
_RATIONALE_MIN_CHARS: Final[int] = 1
_RATIONALE_MAX_CHARS: Final[int] = 4_000

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


class CompletionValidationError(ValueError):
    """Raised when a Completion submission fails Requirement 29.2 /
    29.4 validation.

    ``failed_constraint`` names the specific violation so the HTTP
    layer can render a structured 400 response and tests can assert
    against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_plan_revision_id_missing"``,
            ``"completing_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"outcome_missing"``,
            ``"outcome_out_of_set"``,
            ``"rationale_missing"`` (None / non-str / empty),
            ``"rationale_too_short"`` (< 1 character),
            ``"rationale_too_long"`` (> 4000 characters),
            ``"authority_basis_missing"``,
            ``"authority_basis_type_missing"``,
            ``"authority_basis_type_out_of_set"``,
            ``"authority_basis_id_missing"``,
            ``"source_milestone_acceptance_ids_not_sequence"``,
            ``"source_milestone_acceptance_id_not_string"``,
            ``"prohibited_attribute"``.
        prohibited_keys: Populated only when ``failed_constraint`` is
            ``"prohibited_attribute"``; lists every offending
            top-level key in the original order from the request
            body. Empty tuple in every other case.
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


class CompletionPlanRevisionNotResolvableError(LookupError):
    """Raised when the target Plan Revision Identity does not resolve.

    Requirement 29.4 requires the target Plan Revision Identity to
    resolve to an existing row in ``Plan_Revisions``. The check runs
    before authorization evaluation so the deny path never reveals
    whether a Plan Revision exists for an unauthorized caller.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the caller
            supplied.
        failed_constraint:
            ``"target_plan_revision_not_resolvable"``.
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


class CompletionPlanRevisionNotApprovedError(LookupError):
    """Raised when the target Plan Revision exists but its lifecycle
    state is not ``'approved'``.

    Requirement 29.4 requires the Plan Revision's lifecycle state at
    the recorded time to be ``'approved'`` (Slice 2 Requirement
    9.1). A draft (or otherwise non-approved) Plan Revision is
    rejected with no Completion Record created. The check runs
    before authorization evaluation so the deny path never reveals
    the lifecycle state to an unauthorized caller.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        observed_lifecycle_state: The lifecycle state observed on the
            Plan Revision row (typically ``'draft'``; carried
            verbatim for debugging).
        failed_constraint:
            ``"target_plan_revision_not_approved"``.
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
            "29.4 requires 'approved' "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.observed_lifecycle_state = observed_lifecycle_state
        self.failed_constraint = failed_constraint


class CompletionNoAcceptedMilestonesError(LookupError):
    """Raised when the accepted-Milestone existence check returns
    zero rows.

    Requirement 29.1 / 29.4 require that at least one Milestone
    Acceptance Record whose outcome is ``Accept`` exists against a
    Deliverable Production Record whose source Work Assignment
    Record's ``target_plan_revision_id`` equals the target Plan
    Revision Identity. When the covering query
    (design §"Accepted-Milestone existence check") returns zero
    rows, the request is rejected with no Completion Record
    persisted.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        failed_constraint:
            ``"no_accepted_milestones_for_target_plan_revision"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        failed_constraint: str = (
            "no_accepted_milestones_for_target_plan_revision"
        ),
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} has zero "
            "accepted Milestone Acceptance Records reachable through "
            "Deliverable Production → Work Assignment → "
            "target_plan_revision_id; Requirement 29.1 / 29.4 require "
            "at least one Accept-outcome Milestone Acceptance "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.failed_constraint = failed_constraint


class CompletionSourceMilestoneAcceptanceNotResolvableError(LookupError):
    """Raised when a supplied ``source_milestone_acceptance_ids``
    entry does not resolve to an ``Accept``-outcome Milestone
    Acceptance Record for the target Plan Revision.

    Requirement 29.4 requires every supplied source Milestone
    Acceptance Identity to resolve to an ``Accept``-outcome row in
    the accepted-Milestone existence query result set. An identifier
    that does not appear in that set is rejected with no Completion
    Record persisted. The first offending entry is named on the
    exception so the HTTP layer can identify it to the client.

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        offending_milestone_acceptance_id: The first supplied
            Milestone Acceptance Identity that did not resolve to an
            ``Accept``-outcome row for the target Plan Revision.
        failed_constraint:
            ``"source_milestone_acceptance_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        offending_milestone_acceptance_id: str,
        failed_constraint: str = (
            "source_milestone_acceptance_not_resolvable"
        ),
    ) -> None:
        super().__init__(
            f"Supplied source Milestone Acceptance "
            f"{offending_milestone_acceptance_id!r} did not resolve to an "
            f"Accept-outcome Milestone Acceptance Record for target Plan "
            f"Revision {target_plan_revision_id!r}; Requirement 29.4 "
            "rejects Completions naming an unresolvable or non-Accept "
            f"source Milestone (failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.offending_milestone_acceptance_id = (
            offending_milestone_acceptance_id
        )
        self.failed_constraint = failed_constraint


class CompletionConflictError(LookupError):
    """Raised when a Completion Record already exists for the target
    Plan Revision.

    Surfaces the Requirement 29.3 / 29.4 uniqueness invariant in
    structured form. The schema-level
    ``UNIQUE(target_plan_revision_id)`` constraint on
    ``Completion_Records`` is the source of truth; this pre-check
    produces a precise error in place of the bare
    :class:`sqlalchemy.exc.IntegrityError` that the constraint would
    otherwise raise.

    ``existing_completion_id`` is populated only when the caller
    holds effective ``view`` authority on the existing Completion
    Record (AD-WS-9 / Slice 3 Requirement 30.4). When the caller
    lacks view authority, the field is ``None`` and the conflict
    response is byte-equivalent to one that does not reveal the
    existing Identity, keeping the HTTP response indistinguishable
    from a non-existent endpoint per Slice 1 design
    §"Indistinguishable denial observability".

    Attributes:
        target_plan_revision_id: The Plan Revision Identity the
            caller supplied.
        existing_completion_id: The Completion Identity that already
            targets the same Plan Revision — populated only when the
            caller holds ``view`` authority on it (AD-WS-9).
            ``None`` otherwise.
        failed_constraint: ``"completion_already_exists"``.
    """

    def __init__(
        self,
        *,
        target_plan_revision_id: str,
        existing_completion_id: Optional[str],
        failed_constraint: str = "completion_already_exists",
    ) -> None:
        super().__init__(
            f"Target Plan Revision {target_plan_revision_id!r} is already "
            "the target of a Completion Record; Requirement 29.3 permits "
            "at most one Completion per target Plan Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_plan_revision_id = target_plan_revision_id
        self.existing_completion_id = existing_completion_id
        self.failed_constraint = failed_constraint


class CompletionAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Completion
    creation attempt.

    Carries only ``reason_code`` and ``correlation_id`` — the
    indistinguishable-denial invariant forbids leaking authorized
    Party identities, target existence, or role-assignment details
    beyond the requesting Party's view authority through the denial
    response (Requirement 29.5 / AD-WS-9 / Slice 3 Requirement 30.4).
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Completion creation denied: "
            f"reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class CompletionAuditFailureError(RuntimeError):
    """Raised when every retry of the Denial Record append fails.

    On total audit-append failure the exception is raised *in place
    of* :class:`CompletionAuthorizationError` — denial and audit
    have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no
    ``Completion_Records`` row, ``Relationships`` row, or
    consequential audit row is persisted.

    Attributes:
        reason_code: The denial reason code from the evaluation that
            triggered this denial path.
        correlation_id: The correlation identifier shared with the
            (rolled-back) evaluation row and with the (failed)
            denial record attempts.
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
            f"Denial Record append for a denied Completion attempt "
            f"failed after {attempts} attempt(s): "
            f"reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Result value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateCompletionResult:
    """Result of :meth:`CompletionService.create_completion`.

    Returned so callers (the HTTP layer, tests, the
    Provenance_Navigator that traverses the Execution Provenance
    Chain from Completion back to Source Evidence, and the
    execution-status Projection) can correlate the created
    Completion Record with its ``Addresses`` Relationship and its
    consequential audit row in one round-trip.

    Attributes:
        completion_id: The Completion Record Identity (UUIDv7).
        target_plan_revision_id: The target Approved Plan Revision
            Identity; copied byte-equivalent from the request input.
        target_activity_plan_id: The target Activity Plan Resource
            Identity reached from the Plan Revision's
            ``activity_plan_id`` (read via
            :meth:`PlanRevisionService.get_plan_revision`).
        target_project_id: The target Project Resource Identity
            reached by walking Plan Revision → Activity Plan →
            Project via :meth:`ProjectResolver.resolve_project`.
        outcome: The persisted completion outcome, drawn from
            :data:`OUTCOME_VALUES`.
        rationale: The persisted completion rationale (1..4000
            chars).
        source_milestone_acceptance_ids: The persisted list of
            source Milestone Acceptance Identities the Completion
            rolled up. Order is preserved from the request input.
            May be the empty tuple (per Requirement 29.4 — an empty
            list is permitted when the accepted-Milestone existence
            query still returns at least one row for the target
            Plan Revision).
        completing_party_id: The completing Party Identity; copied
            byte-equivalent from the request input.
        authority_basis: The validated :class:`AuthorityBasisRef`
            recorded on the Completion Record.
        applicable_scope: Scope identifier the Completion applies
            within.
        addresses_relationship_id: Identity of the single
            ``Addresses`` ``Relationships`` row binding the
            Completion to the target Plan Revision.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Completion_Records`` row, the
            ``Relationships`` row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    completion_id: str
    target_plan_revision_id: str
    target_activity_plan_id: str
    target_project_id: str
    outcome: str
    rationale: str
    source_milestone_acceptance_ids: tuple[str, ...]
    completing_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


@dataclass(frozen=True)
class CompletionRecordRow:
    """Read-model snapshot of a persisted ``Completion_Records`` row.

    Returned by :meth:`CompletionService.get_completion`. Backs the
    additive Slice 3 read API (AD-WS-40) the Slice 4
    :class:`~walking_slice.outcome.outcome_reviews.OutcomeReviewService`
    calls to confirm that each cited Completion Record resolves before
    recording an Outcome Review (Requirement 49.4). The Outcome Review
    only needs to confirm resolution; the remaining columns are surfaced
    for completeness and for the provenance read paths. Identity values
    and the timestamp are carried as ``str`` to match the persisted
    column form.
    """

    completion_id: str
    target_plan_revision_id: str
    target_activity_plan_id: str
    target_project_id: str
    outcome: str
    rationale: str
    completing_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionService:
    """Persist immutable Completion Records and the ``Addresses``
    Relationship to the target Approved Plan Revision per AD-WS-26.

    Like its Slice 3 siblings (e.g.,
    :class:`walking_slice.execution.milestone_acceptances.MilestoneAcceptanceService`),
    this service is connection-scoped at call time:
    :meth:`create_completion` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (Slice 1 AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.Completions" declares it
    ``@dataclass(frozen=True)`` — Slice 3 service instances follow
    the Slice 2 convention of being immutable container objects that
    bundle their collaborators.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Completion_Records`` row, the ``Addresses``
            ``Relationships`` row, and the consequential
            ``Audit_Records`` row. The clock is consulted exactly
            once per write so every artifact of the transaction
            shares one timestamp (design §"Cross-Cutting Concerns" —
            Transactionality).
        identity_service: Generates the Completion Record Identity
            (UUIDv7) and the single ``Addresses`` Relationship
            Identity, plus drives the ``Identifier_Registry``
            binding via :func:`_record_execution_artifact` (the
            Completion binding carries the Slice 3
            ``resource_kind='completion_record'`` tag per AD-WS-28).
        audit_log: Appends the consequential audit row (Requirement
            29.6) inside the caller's transaction; the denial-side
            audit append (separate transaction) is driven by
            :meth:`_persist_denial`.
        authorization_service: Evaluates ``create.completion``
            authority (mapped to ``complete`` per Requirement 32.9)
            and ``view.completion`` authority (mapped to ``view``
            via the action-prefix fallback). The
            ``create.completion`` deny path is the Slice 1
            separate-transaction Denial-Record pattern with three
            retries per Requirement 30.6; the ``view.completion``
            evaluation drives the AD-WS-9 conflict-existing-Identity
            gate in :meth:`_resolve_conflict_visibility`.
        planning_reader: Slice 2 :class:`PlanRevisionService` whose
            :meth:`PlanRevisionService.get_plan_revision` read API
            (the AD-WS-30 entry point) is consulted to resolve the
            target Plan Revision and verify its lifecycle state
            (Requirement 29.4) before authorization evaluation. The
            method's return shape also carries the Plan Revision's
            parent ``activity_plan_id`` which the service persists
            on the Completion Record's ``target_activity_plan_id``
            column (Requirement 29.2).
        project_resolver: Slice 2 :class:`ProjectResolver` whose
            :meth:`ProjectResolver.resolve_project` walks Plan
            Revision → Activity Plan → Project (the AD-WS-30 entry
            point) so the service can persist the Plan Revision's
            owning Project Identity on the Completion Record's
            ``target_project_id`` column (Requirement 29.2). The
            walk is a read-only indexed JOIN; the Activity Plan and
            Project rows are not modified.
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
    project_resolver: ProjectResolver
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_completion(
        self,
        connection: Connection,
        *,
        target_plan_revision_id: str,
        outcome: Literal["Completed", "Completed_With_Reservation"],
        rationale: str,
        source_milestone_acceptance_ids: Sequence[str] = (),
        completing_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateCompletionResult:
        """Create an immutable Completion Record and its ``Addresses``
        Relationship per AD-WS-26.

        Per Requirements 29.1 through 29.8, AD-WS-9 (indistinguishable
        denial), AD-WS-26 (Relationship-Type / semantic-role table),
        AD-WS-27 (append-only Slice 3 tables), AD-WS-28 (additive
        ``resource_kind`` values), AD-WS-30 (Planning_Service
        read-only entry points), and Requirement 32.9
        (``create.completion`` → ``complete``):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5; the
           observed-outcome screen is especially load-bearing here
           because a Completion Record is the entity most likely to
           be aliased into an observed Outcome — Requirement 29.8 /
           Requirement 34.3).
        2. Input validation (Requirement 29.2 / 29.4) — every range,
           required-attribute, authority-basis-enumeration, and
           outcome-enumeration check runs before any database read
           so a malformed request never touches identity service,
           the planning reader, or the authorization service.
        3. Resolve the target Plan Revision via
           :meth:`PlanRevisionService.get_plan_revision`. Reject
           when unresolvable
           (:class:`CompletionPlanRevisionNotResolvableError`) or
           when ``lifecycle_state != 'approved'``
           (:class:`CompletionPlanRevisionNotApprovedError`). Both
           rejections run before authorization evaluation so the
           deny path never reveals whether the Plan Revision exists
           or its lifecycle state.
        4. Pre-check the ``UNIQUE(target_plan_revision_id)``
           constraint per Requirement 29.3. When a Completion
           already exists for the target Plan Revision, evaluate
           ``view.completion`` for the requesting Party and raise
           :class:`CompletionConflictError` with the existing
           Identity populated only when view authority is held
           (AD-WS-9 / Slice 3 Requirement 30.4).
        5. Run the accepted-Milestone existence check per
           Requirement 29.1 / 29.4 using the design covering SQL
           (single JOIN through
           ``Milestone_Acceptance_Records`` →
           ``Deliverable_Production_Records`` →
           ``Work_Assignment_Records`` filtered by
           ``wa.target_plan_revision_id`` and
           ``mar.outcome = 'Accept'``). Reject with
           :class:`CompletionNoAcceptedMilestonesError` when zero
           rows match. When ``source_milestone_acceptance_ids`` is
           supplied, require every entry to appear in the result
           set; reject the first non-matching entry with
           :class:`CompletionSourceMilestoneAcceptanceNotResolvableError`.
        6. Resolve the target Activity Plan Identity (from the Plan
           Revision row already fetched in step 3) and the target
           Project Identity via
           :meth:`ProjectResolver.resolve_project`. Both reads are
           read-only and participate in the caller's transactional
           view; the Activity Plan and Project rows are not
           modified.
        7. Run the ``create.completion`` authorization evaluation
           on a *separate* transaction. The authorization target is
           the target Plan Revision — Completion authority is
           scoped against the Plan Revision being completed. On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 30.6 retry sequence and
           raise :class:`CompletionAuthorizationError`. On total
           audit failure raise :class:`CompletionAuditFailureError`
           in place of :class:`CompletionAuthorizationError`.
        8. Mint the Completion Record Identity and the ``Addresses``
           Relationship Identity and register the Completion
           Identity in ``Identifier_Registry`` (kind
           ``'immutable_record'``, carrying the Slice 3
           ``resource_kind='completion_record'`` tag per AD-WS-28)
           via :func:`_record_execution_artifact`.
        9. INSERT the ``Completion_Records`` row carrying every
           Requirement 29.2 attribute (including the resolved
           Activity Plan / Project Identities and the JSON-encoded
           ``source_milestone_acceptance_ids`` list).
        10. INSERT exactly one ``Relationships`` row with
            ``relationship_type='Addresses'``,
            ``source_kind='completion_record'``,
            ``target_kind='plan_revision'``, and
            ``semantic_role IS NULL`` per AD-WS-26 / Requirement
            29.2 / Slice 1 §10.9.
        11. Append the consequential ``Audit_Records`` row with
            ``action_type='create.completion'`` and
            ``target_id=completion_id`` inside the same transaction
            (Requirement 29.6 / Slice 1 AD-WS-5).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_plan_revision_id: Identity of the target Approved
                Plan Revision (Requirement 29.2). Must resolve to
                an existing ``Plan_Revisions`` row whose lifecycle
                state is ``'approved'`` (Requirement 29.4).
            outcome: Completion outcome drawn from
                :data:`OUTCOME_VALUES` (Requirement 29.2).
            rationale: Completion rationale of 1..4000 characters
                (Requirement 29.2).
            source_milestone_acceptance_ids: Optional sequence of
                source Milestone Acceptance Identities the
                Completion rolls up (Requirement 29.2). When
                supplied, every entry must resolve to an
                ``Accept``-outcome Milestone Acceptance Record for
                the target Plan Revision (Requirement 29.4); an
                entry that does not resolve causes the request to
                be rejected with no Completion Record persisted.
                May be the empty sequence; the
                accepted-Milestone existence query must still
                return ``>= 1`` row regardless.
            completing_party_id: Identity of the completing
                Completion Authority Party (Requirement 29.2 /
                29.5).
            authority_basis: Authority basis recorded on the
                Completion Record. Accepted as either
                :class:`AuthorityBasisRef` or a mapping convertible
                to one; the ``type`` must be drawn from
                ``{role-grant-id, scope-id, delegation-chain-id}``
                per AD-WS-10 / Requirement 29.2.
            applicable_scope: Scope identifier the Completion
                applies within. Passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback, and for the
                conflict-pre-check view-authority evaluation, and
                for the main ``create.completion`` evaluation
                itself (Slice 1 single-writer accommodation).
            correlation_id: Optional correlation identifier shared
                by every audit row written in this operation. A
                UUIDv7 is generated when omitted.
            evaluation_at: Optional explicit effective time passed
                to :meth:`AuthorizationService.evaluate`. Defaults
                to the recorded time of this transaction.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every prohibited
                planning-attribute and observed-outcome prefix.

        Returns:
            :class:`CreateCompletionResult` carrying the persisted
            Completion Identity, the resolved Activity Plan /
            Project Identities, every persisted attribute, the
            ``Addresses`` Relationship Identity, the recorded time,
            and the correlation identifier.

        Raises:
            CompletionValidationError: A required attribute is
                missing, a Requirement 29.2 range was violated, the
                outcome / authority basis / source-Milestone
                identifier list is malformed, or the request body
                carried a prohibited planning-attribute or
                observed-outcome key.
            CompletionPlanRevisionNotResolvableError: The target
                Plan Revision Identity did not resolve to an
                existing ``Plan_Revisions`` row (Requirement 29.4).
            CompletionPlanRevisionNotApprovedError: The target Plan
                Revision exists but its lifecycle state is not
                ``'approved'`` at the recorded time (Requirement
                29.4).
            CompletionConflictError: A Completion Record already
                exists for the target Plan Revision (Requirement
                29.3 / 29.4). The exception exposes the existing
                Completion Identity only when the caller holds
                ``view`` authority on it (AD-WS-9).
            CompletionNoAcceptedMilestonesError: Zero
                ``Accept``-outcome Milestone Acceptance Records
                exist for the target Plan Revision at the recorded
                time (Requirement 29.1 / 29.4).
            CompletionSourceMilestoneAcceptanceNotResolvableError:
                At least one supplied
                ``source_milestone_acceptance_ids`` entry did not
                resolve to an ``Accept``-outcome row in the
                existence query (Requirement 29.4).
            CompletionAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 29.5). The Denial Record was appended
                successfully in a separate transaction.
            CompletionAuditFailureError: Authorization denied the
                attempt *and* the separate-transaction Denial
                Record append failed on every retry. Replaces
                :class:`CompletionAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed; the surrounding transaction
                MUST be allowed to roll back per Requirement 29.7.
        """
        # 1. Screen the original request body when the route layer
        # has forwarded it. The typed kwargs themselves cannot carry
        # a prohibited attribute (the signature does not declare any
        # such field), but the HTTP layer's raw body might —
        # Requirements 33.4 and 34.5 demand rejection at the API
        # boundary. The observed-outcome screen is especially
        # load-bearing here per Requirement 29.8 / Requirement 34.3:
        # a Completion Record must never carry an observed-outcome
        # attribute, since recording completion of planned work is
        # not the same as observing an outcome.
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, ALL_PROHIBITED_PREFIXES
                )
            except ExecutionValidationError as exc:
                raise CompletionValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 29.2 / 29.4) before any
        # database read or authorization side-effect.
        self._validate_required_strings(
            target_plan_revision_id=target_plan_revision_id,
            completing_party_id=completing_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_outcome(outcome)
        self._validate_rationale(rationale)
        normalized_basis = self._validate_authority_basis(authority_basis)
        normalized_source_ids = self._validate_source_milestone_ids(
            source_milestone_acceptance_ids
        )

        # 3. Resolve the target Plan Revision via
        # :meth:`PlanRevisionService.get_plan_revision`. The lookup
        # runs on the caller's connection so it participates in the
        # caller's transactional view. Requirement 29.4 rejects the
        # unresolvable case and the non-approved case before
        # authorization evaluation so the deny path never reveals
        # the Plan Revision's existence or lifecycle state to an
        # unauthorized caller.
        plan_revision = self.planning_reader.get_plan_revision(
            connection, target_plan_revision_id
        )
        if plan_revision is None:
            raise CompletionPlanRevisionNotResolvableError(
                target_plan_revision_id=target_plan_revision_id,
            )
        if plan_revision.lifecycle_state != _LIFECYCLE_APPROVED:
            raise CompletionPlanRevisionNotApprovedError(
                target_plan_revision_id=target_plan_revision_id,
                observed_lifecycle_state=plan_revision.lifecycle_state,
            )

        # 4. Pre-check the Requirement 29.3 uniqueness invariant.
        # The ``UNIQUE(target_plan_revision_id)`` constraint on
        # ``Completion_Records`` is the source of truth; the
        # pre-check surfaces a structured
        # :class:`CompletionConflictError` with the existing
        # Completion Identity in place of a generic
        # :class:`IntegrityError`. AD-WS-9 / Slice 3 Requirement
        # 30.4 require the existing Identity to be exposed only
        # when the caller holds ``view`` authority on it; the
        # service therefore evaluates ``view.completion`` against
        # the existing row on a separate transaction and nulls out
        # ``existing_completion_id`` when view authority is denied.
        # The pre-check runs before the ``create.completion``
        # evaluation so an unauthorized caller cannot distinguish a
        # uniqueness conflict from a missing authority via the
        # authorization audit trail.
        existing_id_row = connection.execute(
            text(
                "SELECT completion_id FROM Completion_Records "
                "WHERE target_plan_revision_id = :target_plan_revision_id"
            ),
            {"target_plan_revision_id": target_plan_revision_id},
        ).mappings().first()
        if existing_id_row is not None:
            existing_completion_id = existing_id_row["completion_id"]
            visible_existing_id = self._resolve_conflict_visibility(
                engine=engine,
                completing_party_id=completing_party_id,
                existing_completion_id=existing_completion_id,
                applicable_scope=applicable_scope,
                evaluation_at=evaluation_at,
            )
            raise CompletionConflictError(
                target_plan_revision_id=target_plan_revision_id,
                existing_completion_id=visible_existing_id,
            )

        # 5. Run the accepted-Milestone existence check per
        # Requirement 29.1 / 29.4. The covering query is reproduced
        # verbatim from design §"Accepted-Milestone existence check"
        # except that the SELECT projects the
        # ``milestone_acceptance_id`` of every matching row rather
        # than only ``COUNT(*)`` — the row list is needed to
        # validate the optional ``source_milestone_acceptance_ids``
        # list per Requirement 29.4. The row count is then >= 1
        # iff at least one ``Accept``-outcome Milestone Acceptance
        # exists for a Deliverable Production whose source Work
        # Assignment targets the requested Plan Revision.
        accepted_rows = connection.execute(
            text(
                """
                SELECT mar.milestone_acceptance_id AS milestone_acceptance_id
                  FROM Milestone_Acceptance_Records AS mar
                  JOIN Deliverable_Production_Records AS dpr
                    ON mar.source_deliverable_production_id =
                       dpr.deliverable_production_id
                  JOIN Work_Assignment_Records AS wa
                    ON dpr.source_work_assignment_id = wa.work_assignment_id
                 WHERE wa.target_plan_revision_id = :target_plan_revision_id
                   AND mar.outcome = :accept_outcome
                """
            ),
            {
                "target_plan_revision_id": target_plan_revision_id,
                "accept_outcome": _ACCEPT_OUTCOME,
            },
        ).mappings().all()

        accepted_ids: frozenset[str] = frozenset(
            row["milestone_acceptance_id"] for row in accepted_rows
        )
        if not accepted_ids:
            raise CompletionNoAcceptedMilestonesError(
                target_plan_revision_id=target_plan_revision_id,
            )

        # When the caller named specific source Milestone Acceptance
        # Identities, every named entry must appear in the
        # ``Accept``-outcome result set. Iterating the request order
        # makes the rejection deterministic — the *first* offending
        # entry is reported, matching the Slice 2
        # Deliverable-Expectation-not-resolvable convention.
        for milestone_acceptance_id in normalized_source_ids:
            if milestone_acceptance_id not in accepted_ids:
                raise CompletionSourceMilestoneAcceptanceNotResolvableError(
                    target_plan_revision_id=target_plan_revision_id,
                    offending_milestone_acceptance_id=(
                        milestone_acceptance_id
                    ),
                )

        # 6. Resolve the target Activity Plan / Project Identities.
        # The Plan Revision row already carries
        # ``activity_plan_id`` from step 3; walking to the Project
        # is one indexed JOIN through
        # :meth:`ProjectResolver.resolve_project`. Both reads are
        # read-only and participate in the caller's transactional
        # view; the Activity Plan and Project rows are not modified
        # (Requirement 29.7). A defensive guard around
        # :class:`PlanRevisionNotResolvableError` translates the
        # resolver's failure mode to this module's namespace so
        # callers do not need to import the Slice 2 exception type
        # — the lifecycle check in step 3 guarantees the Plan
        # Revision exists at that point, so this branch only fires
        # if the underlying Activity Plan FK has been corrupted.
        target_activity_plan_id = plan_revision.activity_plan_id
        try:
            target_project_id = self.project_resolver.resolve_project(
                connection,
                plan_revision_id=target_plan_revision_id,
            )
        except PlanRevisionNotResolvableError as exc:  # pragma: no cover
            raise CompletionPlanRevisionNotResolvableError(
                target_plan_revision_id=target_plan_revision_id,
            ) from exc

        # 7. Capture one recorded time for the entire write so the
        # Completion row, the ``Addresses`` Relationship row, and
        # the consequential audit row share a single timestamp
        # (design §"Cross-Cutting Concerns" — Transactionality).
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 8. Run the ``create.completion`` authorization evaluation
        # on a SEPARATE transaction. The authorization target is
        # the target Plan Revision — Completion authority is
        # scoped against the Plan Revision being completed. NOTE:
        # this service does NOT perform the AD-WS-29 second-stage
        # assignee-binding check because ``create.completion``
        # requires the ``complete`` authority (Requirement 32.9)
        # rather than ``contribute``; a Completion Authority is by
        # design a Party distinct from the assignees on the Work
        # Assignment Records that produced the rolled-up accepted
        # Milestones.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=completing_party_id,
                action=_ACTION_CREATE_COMPLETION,
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
            self._persist_denial(
                engine=engine,
                actor_party_id=completing_party_id,
                target_plan_revision_id=target_plan_revision_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise CompletionAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 9. Mint identifiers (AD-WS-2 / AD-WS-28). Completion
        # Records are Governance Decision Immutable Records
        # (per ``02-domain-model.md`` §8.5) so the Record identifier
        # is minted via
        # :meth:`IdentityService.new_immutable_record_id`. The
        # ``Addresses`` Relationship Identity is minted via
        # :meth:`IdentityService.new_relationship_id`.
        completion_id = str(
            self.identity_service.new_immutable_record_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Completion identifier
        # in ``Identifier_Registry``; the digest is the SHA-256 of
        # the canonical JSON payload of the Record so two different
        # Completion Records never collide on the same digest.
        # ``authority_basis.id`` is normalized to its string form
        # for the canonical payload because UUID objects are not
        # natively JSON-serializable.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "target_plan_revision_id": target_plan_revision_id,
                    "target_activity_plan_id": target_activity_plan_id,
                    "target_project_id": target_project_id,
                    "outcome": outcome,
                    "rationale": rationale,
                    "source_milestone_acceptance_ids": list(
                        normalized_source_ids
                    ),
                    "completing_party_id": completing_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                    "recorded_at": recorded_at,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 10. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-28
        # ``resource_kind='completion_record'`` tag.
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_COMPLETION,
            completion_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=completing_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_COMPLETION,
            recorded_time=recorded_time,
        )

        # 11. Insert the Completion Record carrying every Requirement
        # 29.2 attribute. ``source_milestone_acceptance_ids_json``
        # is the canonical JSON encoding of the (possibly empty)
        # request-order list of source Milestone Acceptance
        # Identities. The Activity Plan / Project Identities come
        # from steps 3 / 6 — they are derived from the persisted
        # Plan Revision row and the read-only ProjectResolver walk,
        # not from the request body, so a forged request that named
        # a different Activity Plan or Project Identity cannot
        # bypass the Requirement 29.2 / 29.7 byte-equivalence
        # contract.
        connection.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id,
                    target_plan_revision_id,
                    target_activity_plan_id,
                    target_project_id,
                    outcome, rationale,
                    source_milestone_acceptance_ids_json,
                    completing_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :completion_id,
                    :target_plan_revision_id,
                    :target_activity_plan_id,
                    :target_project_id,
                    :outcome, :rationale,
                    :source_milestone_acceptance_ids_json,
                    :completing_party_id,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "completion_id": completion_id,
                "target_plan_revision_id": target_plan_revision_id,
                "target_activity_plan_id": target_activity_plan_id,
                "target_project_id": target_project_id,
                "outcome": outcome,
                "rationale": rationale,
                "source_milestone_acceptance_ids_json": json.dumps(
                    list(normalized_source_ids)
                ),
                "completing_party_id": completing_party_id,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 12. Insert the ``Addresses`` Relationship binding the
        # Completion Record to the target Plan Revision per
        # Requirement 29.2 / Slice 1 §10.9. ``semantic_role`` is
        # NULL — the ``Addresses`` relationship type carries no role
        # discriminator per the AD-WS-26 table. The ``target_id`` is
        # the Plan Revision Identity and ``target_revision_id`` is
        # NULL because Plan Revisions live in a single
        # Revision-level table per Slice 2 (no separate Resource
        # header / Revision identifier split).
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
                "source_kind": _KIND_COMPLETION_RECORD,
                "source_id": completion_id,
                "source_revision_id": None,
                "target_kind": _KIND_PLAN_REVISION,
                "target_id": target_plan_revision_id,
                "target_revision_id": None,
                "authoring_party_id": completing_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 13. Append the consequential audit row (Requirement 29.6 /
        # Slice 1 AD-WS-5). Participates in the caller's
        # transaction so a failure here rolls back the registry,
        # the ``Completion_Records`` row, and the ``Relationships``
        # row together. ``target_id`` is the Completion Record
        # Identity; ``target_revision_id`` is ``None`` because
        # Completion Records are Record-scoped (Requirement 22.2 —
        # no separate Revision).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=completing_party_id,
            action_type=_ACTION_CREATE_COMPLETION,
            target_id=completion_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateCompletionResult(
            completion_id=completion_id,
            target_plan_revision_id=target_plan_revision_id,
            target_activity_plan_id=target_activity_plan_id,
            target_project_id=target_project_id,
            outcome=outcome,
            rationale=rationale,
            source_milestone_acceptance_ids=normalized_source_ids,
            completing_party_id=completing_party_id,
            authority_basis=normalized_basis,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_required_strings(
        *,
        target_plan_revision_id: Any,
        completing_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 29.2 / 29.4: a Completion creation request
        that omits the target Plan Revision Identity, the completing
        Party Identity, or the applicable scope is rejected with no
        Completion Record created. Each missing attribute surfaces a
        distinct ``failed_constraint`` so the HTTP layer can
        identify the precise field to the client.
        """
        if not target_plan_revision_id or not isinstance(
            target_plan_revision_id, str
        ):
            raise CompletionValidationError(
                "target_plan_revision_id is required; Requirement 29.4 "
                "rejects Completions missing the target Plan Revision "
                "Identity.",
                failed_constraint="target_plan_revision_id_missing",
            )
        if not completing_party_id or not isinstance(
            completing_party_id, str
        ):
            raise CompletionValidationError(
                "completing_party_id is required; Requirement 29.5 "
                "rejects unauthenticated Completion creation.",
                failed_constraint="completing_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise CompletionValidationError(
                "applicable_scope is required; Requirement 29.4 rejects "
                "Completions missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_outcome(outcome: Any) -> None:
        """Reject submissions whose ``outcome`` is missing or outside
        the Requirement 29.2 enumeration.

        Per Requirement 29.2 the outcome is drawn from the enumerated
        set ``{Completed, Completed_With_Reservation}``. The
        schema-level CHECK on ``Completion_Records.outcome`` enforces
        the same membership; surfacing the limit here yields a
        precise ``failed_constraint`` for the HTTP layer rather than
        a raw SQL constraint violation.
        """
        if outcome is None or not isinstance(outcome, str) or outcome == "":
            raise CompletionValidationError(
                "outcome is required and must be one of "
                f"{list(OUTCOME_VALUES)} per Requirement 29.2.",
                failed_constraint="outcome_missing",
            )
        if outcome not in OUTCOME_VALUES:
            raise CompletionValidationError(
                f"outcome {outcome!r} is not in the Requirement 29.2 "
                f"enumeration {list(OUTCOME_VALUES)}.",
                failed_constraint="outcome_out_of_set",
            )

    @staticmethod
    def _validate_rationale(rationale: Any) -> None:
        """Reject completion rationale outside the Requirement 29.2
        range.

        Per Requirement 29.2 the rationale is 1..4000 characters and
        is *required*. The ``Completion_Records.rationale`` CHECK
        constraint ``length(rationale) BETWEEN 1 AND 4000`` enforces
        the same range at the database layer.
        """
        if rationale is None or not isinstance(rationale, str):
            raise CompletionValidationError(
                "rationale is required and must be a string of "
                f"{_RATIONALE_MIN_CHARS}..{_RATIONALE_MAX_CHARS} "
                "characters per Requirement 29.2.",
                failed_constraint="rationale_missing",
            )
        if len(rationale) < _RATIONALE_MIN_CHARS:
            raise CompletionValidationError(
                f"rationale length {len(rationale)} is below the "
                f"{_RATIONALE_MIN_CHARS}-character minimum imposed by "
                "Requirement 29.2.",
                failed_constraint="rationale_too_short",
            )
        if len(rationale) > _RATIONALE_MAX_CHARS:
            raise CompletionValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_RATIONALE_MAX_CHARS}-character limit imposed by "
                "Requirement 29.2.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 29.2 / AD-WS-10: the authority basis ``type``
        is drawn from
        ``{role-grant-id, scope-id, delegation-chain-id}``. The
        Python-typed signature already constrains callers to pass an
        :class:`AuthorityBasisRef` whose ``type`` Literal restricts
        the enumeration; the HTTP layer may pass a dict if it has
        not yet bound the request to the typed model, so this
        validator coerces both shapes (mirroring the Slice 3 sibling
        validators).
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise CompletionValidationError(
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
            raise CompletionValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise CompletionValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise CompletionValidationError(
                "authority_basis.id is required per Requirement 29.2.",
                failed_constraint="authority_basis_id_missing",
            )

        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise CompletionValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    @staticmethod
    def _validate_source_milestone_ids(
        source_milestone_acceptance_ids: Any,
    ) -> tuple[str, ...]:
        """Validate the optional source Milestone Acceptance ID list
        and return a normalized tuple.

        Per Requirement 29.2 the list is optional and may be empty.
        Strings are not :class:`Sequence`-rejected here despite being
        formally :class:`Sequence` because a raw string is virtually
        always a caller error (e.g., forgetting to wrap a single
        identifier in a list); the validator therefore rejects bare
        strings explicitly to give a clear ``failed_constraint``
        rather than silently splitting the string into characters.
        Bytes are rejected for the same reason.
        """
        if source_milestone_acceptance_ids is None:
            return ()
        if isinstance(
            source_milestone_acceptance_ids, (str, bytes, bytearray)
        ):
            raise CompletionValidationError(
                "source_milestone_acceptance_ids must be a sequence of "
                "Milestone Acceptance Identity strings (e.g., a list or "
                "tuple); received a bare "
                f"{type(source_milestone_acceptance_ids).__name__}.",
                failed_constraint=(
                    "source_milestone_acceptance_ids_not_sequence"
                ),
            )
        if not isinstance(source_milestone_acceptance_ids, Sequence):
            raise CompletionValidationError(
                "source_milestone_acceptance_ids must be a sequence of "
                "Milestone Acceptance Identity strings; received "
                f"{type(source_milestone_acceptance_ids).__name__}.",
                failed_constraint=(
                    "source_milestone_acceptance_ids_not_sequence"
                ),
            )
        normalized: list[str] = []
        for entry in source_milestone_acceptance_ids:
            if not isinstance(entry, str) or entry == "":
                raise CompletionValidationError(
                    "every source_milestone_acceptance_ids entry must "
                    "be a non-empty Milestone Acceptance Identity "
                    f"string; received {entry!r}.",
                    failed_constraint=(
                        "source_milestone_acceptance_id_not_string"
                    ),
                )
            normalized.append(entry)
        return tuple(normalized)

    # -- read helper -------------------------------------------------------

    @staticmethod
    def get_completion(
        connection: Connection,
        completion_id: str,
    ) -> Optional[CompletionRecordRow]:
        """Read-only lookup of a Completion Record by its Identity.

        Implements the additive Slice 3 read API mandated by AD-WS-40
        (fourth walking slice design §"AD-WS-40"). The Slice 4
        :class:`~walking_slice.outcome.outcome_reviews.OutcomeReviewService`
        calls this read to confirm that every cited Completion Record
        Identity resolves before recording an Outcome Review Record
        (Requirement 49.4); an unresolvable cited Completion Identity is
        rejected with nothing persisted.

        The lookup is a single indexed ``SELECT`` against
        ``Completion_Records`` keyed on the primary key
        ``completion_id``. It introduces **no write path** on the
        Execution_Service (Requirement 29.7 / 60.1): it neither mutates
        the resolved Slice 3 row nor any other row. It is a
        :func:`staticmethod` because the read consults none of the wired
        collaborators (clock, identity service, audit log, authorization
        service) — it needs only the caller's
        :class:`~sqlalchemy.engine.Connection`. Exposing it on
        :class:`CompletionService` keeps the design-pinned entry-point
        name (``CompletionService.get_completion``) textually stable and
        matches the convention established by
        :meth:`walking_slice.outcome.measurement_records.MeasurementRecordService.get_measurement_record`
        and
        :meth:`walking_slice.planning.plan_revisions.PlanRevisionService.get_plan_revision`.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context. The lookup participates in the caller's
                transactional view so consumers see a consistent
                snapshot across multiple reads.
            completion_id: The Completion Record Identity to resolve.

        Returns:
            A :class:`CompletionRecordRow` snapshot when a matching row
            exists. ``None`` when no ``Completion_Records`` row matches
            the supplied identifier; the caller treats ``None`` as the
            unresolvable branch (e.g., the Outcome_Service rejects the
            create attempt). Returning ``None`` rather than raising
            mirrors the ``one_or_none`` convention used elsewhere in the
            slice and lets the caller decide how to handle the absent
            case without try/except in the hot path.
        """
        row = connection.execute(
            text(
                "SELECT completion_id, target_plan_revision_id, "
                "target_activity_plan_id, target_project_id, "
                "outcome, rationale, completing_party_id, "
                "authority_basis_type, authority_basis_id, "
                "applicable_scope, recorded_at "
                "FROM Completion_Records "
                "WHERE completion_id = :completion_id"
            ),
            {"completion_id": completion_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        return CompletionRecordRow(
            completion_id=row["completion_id"],
            target_plan_revision_id=row["target_plan_revision_id"],
            target_activity_plan_id=row["target_activity_plan_id"],
            target_project_id=row["target_project_id"],
            outcome=row["outcome"],
            rationale=row["rationale"],
            completing_party_id=row["completing_party_id"],
            authority_basis_type=row["authority_basis_type"],
            authority_basis_id=row["authority_basis_id"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    # -- AD-WS-9 conflict-visibility helper -------------------------------

    def _resolve_conflict_visibility(
        self,
        *,
        engine: Engine,
        completing_party_id: str,
        existing_completion_id: str,
        applicable_scope: str,
        evaluation_at: Optional[datetime],
    ) -> Optional[str]:
        """Return the existing Completion Identity when the caller
        holds view authority on it; otherwise return ``None``.

        Implements the AD-WS-9 / Slice 3 Requirement 30.4
        view-authority gate on the
        :class:`CompletionConflictError` response: when a Completion
        Record already exists for the supplied target Plan Revision,
        the conflict body carries the existing ``completion_id``
        only if the requesting Party would be permitted to view it.
        Otherwise the body is byte-equivalent to a response that
        lacks the existing-Identity field, keeping the HTTP response
        indistinguishable from a non-existent endpoint.

        Evaluates ``view.completion`` on a *separate* transaction
        (same pattern as :meth:`create_completion`'s main
        authorization evaluation) so the read does not pollute the
        caller's transactional view and so the AD-WS-9 evaluation
        audit row survives independently of the conflict path.

        Args:
            engine: SQLAlchemy engine used to open the separate
                evaluation transaction.
            completing_party_id: Identity of the requesting Party.
            existing_completion_id: The Completion Identity that
                already targets the target Plan Revision.
            applicable_scope: Scope passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`.
            evaluation_at: Optional explicit effective time; falls
                back to the AuthorizationService's clock when
                omitted.

        Returns:
            ``existing_completion_id`` when the view evaluation
            returns ``permit``; ``None`` otherwise.
        """
        at_when = (
            evaluation_at if evaluation_at is not None else self.clock.now()
        )
        with engine.begin() as eval_conn:
            view_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=completing_party_id,
                action=_ACTION_VIEW_COMPLETION,
                target=TargetRef(
                    kind=_KIND_COMPLETION_RECORD,
                    id=existing_completion_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=at_when,
            )
        if view_outcome.is_permit:
            return existing_completion_id
        return None

    # -- denial side-channel ----------------------------------------------

    def _persist_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_plan_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Completion attempt.

        Implements the AD-WS-9 / Slice 1 Requirement 7.6 / Slice 3
        Requirement 30.6 retry contract verbatim (mirroring the
        sibling Slice 3 services). Each attempt opens a *new*
        :meth:`Engine.begin` transaction so a previous attempt's
        rollback does not poison this one, tries
        :meth:`AuditLog.append_denial`, and either returns on
        success or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails,
          :class:`CompletionAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_completion` raises an authorization error. The
        Denial Record must therefore live outside that scope to
        survive (AD-WS-9 / Requirement 30.6).

        ``target_id`` on the Denial Record points at the target
        Plan Revision Identity because the Completion Identity has
        not yet been minted at the time the denial is recorded — the
        deny path explicitly refuses to mint an Immutable Record
        Identity for an unauthorized attempt (Requirement 29.5 /
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
                        attempted_action=_ACTION_CREATE_COMPLETION,
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

        raise CompletionAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this
# module does not import private names from sibling services. The
# functions are intentionally identical to their sibling
# implementations: correlation identifiers are non-domain values and
# the digest helper is opaque to :class:`Identifier_Registry`.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Completion
    creation. They are not registered with :class:`IdentityService`
    because they do not name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Completion
    Identity in ``Identifier_Registry``. Completion Records are
    Record-scoped (Requirement 22.2 — no separate Revision) so this
    digest is bound exactly once per Completion creation.
    """
    return hashlib.sha256(content).hexdigest()
