"""Execution_Service.MilestoneAcceptances — immutable Milestone
Acceptance Records keyed to a Deliverable Production Record.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Execution_Service.MilestoneAcceptances" — public dataclass
  surface, authority string (``create.milestone_acceptance`` →
  ``accept_milestone`` per Requirement 32.8), AD-WS-9
  separate-transaction Denial Record on deny, validation order, and
  the Relationship-row contract: exactly one ``Addresses``
  Relationship from the Milestone Acceptance Record to the produced
  Deliverable Revision (``semantic_role IS NULL``).
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with
  ``kind = 'immutable_record'`` and
  ``resource_kind = 'milestone_acceptance_record'`` per AD-WS-28);
  Authorization (the action string ``create.milestone_acceptance``
  maps to the ``accept_milestone`` authority per Requirement 32.8;
  the deny path uses the Slice 1 separate-transaction Denial-Record
  pattern with the Slice 3 Requirement 30.6 three-retry contract).
- §"Error Handling — Duplicate / uniqueness violations" — the
  ``UNIQUE(source_deliverable_production_id)`` constraint on
  ``Milestone_Acceptance_Records`` (Requirement 28.3) is pre-checked
  in the service and surfaced as a structured
  ``milestone_acceptance_already_exists`` conflict. The existing
  Milestone Acceptance Identity is exposed on the conflict response
  only when the caller holds view authority on it (AD-WS-9 / Slice 3
  Requirement 30.4); otherwise the conflict body is byte-equivalent
  to a response that lacks the field, indistinguishable from a
  non-existent endpoint per Slice 1 design §"Indistinguishable denial
  observability".
- AD-WS-26 — Milestone Acceptance Records carry exactly one
  ``Addresses`` Relationship to the produced Deliverable Revision
  with ``semantic_role IS NULL`` (Requirement 28.2; consistent with
  Slice 1 §10.9).
- AD-WS-27 — ``Milestone_Acceptance_Records`` is append-only; the
  source Deliverable Production Record, the produced Deliverable
  Revision, the target Deliverable Expectation Revision, and every
  Slice 1 / Slice 2 row must remain byte-equivalent throughout this
  transaction (Requirement 28.8 / Requirement 40, Property 11).
- AD-WS-28 — additive ``Identifier_Registry.resource_kind`` value
  ``'milestone_acceptance_record'`` populated through
  :func:`walking_slice.execution._helpers._record_execution_artifact`.

This is **not** a Contributor write. ``create.milestone_acceptance``
requires the ``accept_milestone`` authority (Requirement 32.8) and does
**not** trigger the AD-WS-29 second-stage assignee-binding check that
applies to Contributor writes (``create.work_event``,
``create.time_entry``, ``create.produced_deliverable``,
``create.deliverable_production``). A Milestone Acceptance Authority is
typically a Party distinct from the assignee on the source Work
Assignment.

Task scope (task 10.1)
======================

This module implements
:meth:`MilestoneAcceptanceService.create_milestone_acceptance`:

1. Defensively reject any prohibited planning-attribute or
   observed-outcome key in the original request body via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
2. Validate request inputs per Requirement 28.2 / 28.4:
   ``source_deliverable_production_id``, ``accepting_party_id``, and
   ``applicable_scope`` are present;
   ``outcome ∈ {'Accept', 'Reject'}``; ``rationale`` length is
   1..4000 characters; ``authority_basis.type`` is drawn from the
   AD-WS-10 set ``{role-grant-id, scope-id, delegation-chain-id}``.
3. Resolve the source Deliverable Production Record by primary key
   on the caller's connection (Requirement 28.4 — unresolvable target
   rejected). The rejection runs before authorization evaluation so
   the deny path cannot reveal whether the Production Record exists.
4. Resolve the produced Deliverable Revision and target Deliverable
   Expectation Revision from the source Deliverable Production
   Record's ``Produces`` and ``Addresses`` Relationships per design
   §"Execution_Service.MilestoneAcceptances" Responsibility. Reject
   any unresolvable Relationship — a Production Record exists in the
   schema with both Relationships present (Requirement 27.2), so an
   absent Relationship indicates an integrity violation that must
   not silently pollute the Milestone Acceptance Record.
5. Pre-check the ``UNIQUE(source_deliverable_production_id)``
   constraint per Requirement 28.3 / 28.4. When a Milestone Acceptance
   already exists for the source Production Record:

   - Evaluate
     ``Authorization_Service.evaluate(party=accepting_party_id,
     action="view.milestone_acceptance", target=existing_acceptance,
     at=now())`` on a separate transaction.
   - When the evaluation permits view, raise
     :class:`MilestoneAcceptanceConflictError` with the existing
     ``milestone_acceptance_id`` populated so the HTTP layer can
     return a structured 409 ``milestone_acceptance_already_exists``
     response identifying the existing Identity.
   - When the evaluation denies view, raise
     :class:`MilestoneAcceptanceConflictError` with the existing
     ``milestone_acceptance_id`` set to ``None`` so the conflict
     response is byte-equivalent to a response that lacks the
     existing-Identity field (AD-WS-9 / Slice 3 Requirement 30.4).

   The pre-check runs after target-resolution but before the main
   ``create.milestone_acceptance`` authorization evaluation so a
   subsequent unauthorized caller cannot distinguish a uniqueness
   conflict from a missing authority via the authorization audit
   trail. The schema-level UNIQUE constraint is the authoritative
   source of truth; the application-level pre-check surfaces a
   structured error in place of the bare IntegrityError that the
   constraint would otherwise produce.
6. Evaluate ``Authorization_Service.evaluate(party=accepting_party_id,
   action="create.milestone_acceptance", target=production_ref,
   at=now())`` on a separate transaction. On ``deny``, persist a
   Denial Record from another separate transaction with the Slice 1
   Requirement 7.6 / Slice 3 Requirement 30.6 three-retry
   exponential-backoff pattern and raise
   :class:`MilestoneAcceptanceAuthorizationError`.
7. On ``permit``, mint the Milestone Acceptance Record Identity
   (UUIDv7) and the ``Addresses`` Relationship Identity; register the
   Milestone Acceptance Identity in ``Identifier_Registry`` with
   ``kind='immutable_record'`` and
   ``resource_kind='milestone_acceptance_record'`` via
   :func:`_record_execution_artifact`.
8. INSERT the ``Milestone_Acceptance_Records`` row carrying every
   Requirement 28.2 attribute, including the produced Deliverable
   Resource and Revision Identities resolved in step 4 and the target
   Deliverable Expectation Resource and Revision Identities also
   resolved in step 4.
9. INSERT exactly one ``Relationships`` row with
   ``relationship_type='Addresses'``,
   ``source_kind='milestone_acceptance_record'``,
   ``target_kind='deliverable_revision'``, and
   ``semantic_role IS NULL`` per AD-WS-26 and Requirement 28.2 /
   Slice 1 §10.9.
10. Append the consequential ``Audit_Records`` row with
    ``action_type='create.milestone_acceptance'`` and
    ``target_id=milestone_acceptance_id`` inside the same transaction
    (Requirement 28.6 / Slice 1 AD-WS-5).

Rows are inserted in dependency order so a FK failure anywhere rolls
back the whole transaction (Requirement 28.7).

The source Deliverable Production Record, the produced Deliverable
Revision, the target Deliverable Expectation Revision, every Slice 1
row, and every Slice 2 row remain byte-equivalent throughout the
transaction. The service never issues an UPDATE, INSERT, or DELETE
against any of those rows (Requirement 28.8). The append-only triggers
created in task 1.2 enforce immutability of
``Milestone_Acceptance_Records`` itself after commit (Requirement
28.7).

Requirements satisfied
======================

    28.1 — authorized Milestone Acceptance creation produces exactly
           one immutable Milestone Acceptance Record within nominal
           latency, resolving the produced Deliverable Revision and
           target Deliverable Expectation Revision from the source
           Deliverable Production Record's ``Produces`` and
           ``Addresses`` Relationships.
    28.2 — every Milestone Acceptance Record records the source
           Deliverable Production Record Identity, the produced
           Deliverable Resource and Revision Identities, the target
           Deliverable Expectation Resource and Revision Identities,
           the milestone-acceptance outcome (``Accept`` / ``Reject``),
           the acceptance rationale (1..4000 chars), the accepting
           Party Identity, the authority basis (AD-WS-10 enumeration),
           the applicable scope, the recorded time, and exactly one
           ``Addresses`` Relationship to the produced Deliverable
           Revision.
    28.3 — at most one Milestone Acceptance Record per source
           Deliverable Production Record. The schema-level
           ``UNIQUE(source_deliverable_production_id)`` constraint is
           the source of truth; the application-level pre-check
           surfaces a structured ``MilestoneAcceptanceConflictError``
           and applies the AD-WS-9 view-authority gate on the
           existing Identity.
    28.4 — unresolvable source Production Record, duplicate
           Milestone Acceptance against the same Production Record,
           ``outcome`` outside the enumerated set, missing rationale,
           missing authority basis, and missing applicable scope are
           rejected with no Milestone Acceptance Record persisted.
    28.5 — unauthorized requests are denied via
           :class:`AuthorizationService`; the Execution_Service
           declines to create any Milestone Acceptance Record and
           the Audit_Log appends a Denial Record conforming to
           AD-WS-9.
    28.6 — the Audit_Log appends an immutable consequential audit
           row identifying the Milestone Acceptance Record Identity,
           source Production Record Identity, produced Deliverable
           Revision Identity, target Deliverable Expectation Revision
           Identity, accepting Party Identity, authority basis,
           outcome, and recorded time within the same transaction.
    28.7 — the append-only schema triggers (created in task 1.2)
           reject every UPDATE / DELETE attempt on
           ``Milestone_Acceptance_Records`` and the ``Addresses``
           Relationship row after this transaction commits.
    28.8 — the source Deliverable Production Record, produced
           Deliverable Revision, target Deliverable Expectation
           Revision, addressed Project Revision, addressed Plan
           Revision, and every Slice 1 / Slice 2 row remain
           byte-equivalent throughout this transaction.
    32.8 — ``create.milestone_acceptance`` requires the
           ``accept_milestone`` authority.
    41.1 — every consequential write is atomic with its consequential
           audit row.
    41.10 — every Slice 3 row that referenced an existing Slice 1 /
            Slice 2 row leaves those rows byte-equivalent.
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
from walking_slice.execution._helpers import (
    ALL_PROHIBITED_PREFIXES,
    ExecutionValidationError,
    _record_execution_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef


__all__ = [
    "CreateMilestoneAcceptanceResult",
    "MilestoneAcceptanceAuditFailureError",
    "MilestoneAcceptanceAuthorizationError",
    "MilestoneAcceptanceConflictError",
    "MilestoneAcceptanceProductionNotResolvableError",
    "MilestoneAcceptanceProductionRelationshipsCorruptError",
    "MilestoneAcceptanceService",
    "MilestoneAcceptanceValidationError",
    "OUTCOME_VALUES",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship Type / kind / semantic-role strings,
# registry kind / resource_kind strings, and validation limits are
# pulled out as module-level ``Final`` so the names downstream property
# tests look for in ``Audit_Records.action_type``, in
# ``Identifier_Registry.resource_kind``, and in
# ``Relationships.semantic_role`` are textually stable.
# ---------------------------------------------------------------------------


# ``create.milestone_acceptance`` maps to the ``accept_milestone``
# authority per Requirement 32.8. The string is also the
# ``action_type`` recorded on the consequential audit row (Requirement
# 28.6) and on the separate-transaction Denial Record so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_MILESTONE_ACCEPTANCE: Final[str] = "create.milestone_acceptance"

# ``view.milestone_acceptance`` is the action used by the
# conflict-pre-check view-authority gate (AD-WS-9 / Slice 3 Requirement
# 30.4). Mapped to the ``view`` authority by
# :func:`walking_slice.authorization._required_authority`'s prefix
# fallback.
_ACTION_VIEW_MILESTONE_ACCEPTANCE: Final[str] = "view.milestone_acceptance"

# Relationship Type strings written to the ``Relationships`` rows
# Slice 3 reads and writes for this Record kind.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_PRODUCES: Final[str] = "Produces"

# ``Relationships.source_kind`` / ``target_kind`` strings per
# AD-WS-26. The Milestone Acceptance Record is the source of one
# ``Addresses`` Relationship; the target is the produced Deliverable
# Revision and ``semantic_role`` is NULL (the ``Addresses``
# Relationship Type carries no role discriminator per the AD-WS-26
# table — consistent with Slice 1 ``Addresses`` rows and with the
# Deliverable Production Record's ``Addresses`` row to the target
# Deliverable Expectation Revision).
_KIND_MILESTONE_ACCEPTANCE_RECORD: Final[str] = "milestone_acceptance_record"
_KIND_DELIVERABLE_PRODUCTION_RECORD: Final[str] = "deliverable_production_record"
_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"
_KIND_DELIVERABLE_EXPECTATION_REVISION: Final[str] = "deliverable_expectation_revision"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Execution_Service ``resource_kind`` tag (Slice 3 additive
# enumeration per AD-WS-28). Milestone Acceptance Records are
# Immutable Records (per ``02-domain-model.md`` §8.5 Governance
# Decision Immutable Record) so the registry kind is
# ``'immutable_record'``; the ``resource_kind`` value is
# ``'milestone_acceptance_record'`` and is the row-level discriminator
# that keeps the Milestone Acceptance Identity set inspectably
# disjoint from every other Slice 1 / Slice 2 / Slice 3
# ``resource_kind`` (Requirement 22.8).
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_MILESTONE_ACCEPTANCE: Final[str] = "milestone_acceptance_record"

# Authority-basis ``type`` enumeration per AD-WS-10. Mirrors the
# Slice 3 ``Milestone_Acceptance_Records.authority_basis_type`` CHECK
# constraint values; centralizing the tuple here lets the validator
# reject malformed requests structurally before they touch SQL.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Milestone-acceptance outcome enumeration per Requirement 28.2. The
# schema-level CHECK on ``Milestone_Acceptance_Records.outcome``
# enforces the same membership as a defense in depth. The tuple
# preserves the declared order for use in error messages.
OUTCOME_VALUES: Final[tuple[str, ...]] = ("Accept", "Reject")

# Validation limits for ``rationale`` per Requirement 28.2 (1..4000
# characters). The
# ``Milestone_Acceptance_Records.rationale`` CHECK constraint enforces
# the same range; surfacing the limits here yields a precise
# ``failed_constraint`` for the HTTP layer rather than a raw SQL
# constraint violation.
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


class MilestoneAcceptanceValidationError(ValueError):
    """Raised when a Milestone Acceptance submission fails Requirement
    28.2 / 28.4 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a
    stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"source_deliverable_production_id_missing"``,
            ``"accepting_party_id_missing"``,
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
            ``"prohibited_attribute"``.
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


class MilestoneAcceptanceProductionNotResolvableError(LookupError):
    """Raised when the source Deliverable Production Identity does not
    resolve.

    Requirement 28.4 requires the source Deliverable Production Record
    Identity to resolve to an existing row. The check runs before
    authorization evaluation so the deny path never reveals whether a
    Production Record exists for an unauthorized caller.

    Attributes:
        source_deliverable_production_id: The Production Identity the
            caller supplied.
        failed_constraint:
            ``"source_deliverable_production_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        source_deliverable_production_id: str,
        failed_constraint: str = "source_deliverable_production_not_resolvable",
    ) -> None:
        super().__init__(
            f"Source Deliverable Production "
            f"{source_deliverable_production_id!r} did not resolve to an "
            "existing Deliverable Production Record "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.source_deliverable_production_id = (
            source_deliverable_production_id
        )
        self.failed_constraint = failed_constraint


class MilestoneAcceptanceProductionRelationshipsCorruptError(LookupError):
    """Raised when the source Deliverable Production Record's
    ``Produces`` or ``Addresses`` Relationship cannot be located.

    Requirement 27.2 mandates that every Deliverable Production Record
    is persisted with exactly one ``Produces`` Relationship to its
    produced Deliverable Revision and exactly one ``Addresses``
    Relationship to its target Deliverable Expectation Revision. The
    Milestone Acceptance Service resolves the produced Revision and
    Expectation Revision Identities by reading those Relationship rows
    rather than the Production Record's denormalized columns
    (preserving the design contract that "the produced Deliverable
    Revision and target Deliverable Expectation Revision are resolved
    from the source Deliverable Production Record's ``Produces`` and
    ``Addresses`` Relationships").

    An absent Relationship row therefore indicates a database
    integrity violation that must surface as a distinct exception
    rather than silently polluting the Milestone Acceptance Record
    with corrupted Identities. The check runs before authorization
    evaluation so the deny path never reveals integrity state to an
    unauthorized caller.

    Attributes:
        source_deliverable_production_id: The Production Identity the
            caller supplied.
        missing_relationship_type: ``"Produces"`` when the produced
            Deliverable Revision could not be resolved; ``"Addresses"``
            when the target Deliverable Expectation Revision could
            not be resolved.
        failed_constraint:
            ``"deliverable_production_relationships_corrupt"``.
    """

    def __init__(
        self,
        *,
        source_deliverable_production_id: str,
        missing_relationship_type: str,
        failed_constraint: str = (
            "deliverable_production_relationships_corrupt"
        ),
    ) -> None:
        super().__init__(
            f"Source Deliverable Production "
            f"{source_deliverable_production_id!r} is missing its "
            f"{missing_relationship_type!r} Relationship row; "
            "Requirement 27.2 requires every Deliverable Production "
            "Record to carry both a Produces and an Addresses Relationship "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.source_deliverable_production_id = (
            source_deliverable_production_id
        )
        self.missing_relationship_type = missing_relationship_type
        self.failed_constraint = failed_constraint


class MilestoneAcceptanceConflictError(LookupError):
    """Raised when a Milestone Acceptance already exists for the source
    Deliverable Production Record.

    Surfaces the Requirement 28.3 / 28.4 uniqueness invariant in
    structured form. The schema-level
    ``UNIQUE(source_deliverable_production_id)`` constraint on
    ``Milestone_Acceptance_Records`` is the source of truth; this
    pre-check produces a precise error in place of the bare
    :class:`sqlalchemy.exc.IntegrityError` that the constraint would
    otherwise raise.

    ``existing_milestone_acceptance_id`` is populated only when the
    caller holds effective ``view`` authority on the existing
    Milestone Acceptance Record (AD-WS-9 / Slice 3 Requirement 30.4).
    When the caller lacks view authority, the field is ``None`` and
    the conflict response is byte-equivalent to one that does not
    reveal the existing Identity, keeping the HTTP response
    indistinguishable from a non-existent endpoint per Slice 1 design
    §"Indistinguishable denial observability".

    Attributes:
        source_deliverable_production_id: The Production Identity the
            caller supplied.
        existing_milestone_acceptance_id: The Milestone Acceptance
            Identity that already targets the same Production Record
            — populated only when the caller holds ``view`` authority
            on it (AD-WS-9). ``None`` otherwise.
        failed_constraint:
            ``"milestone_acceptance_already_recorded"``.
    """

    def __init__(
        self,
        *,
        source_deliverable_production_id: str,
        existing_milestone_acceptance_id: Optional[str],
        failed_constraint: str = "milestone_acceptance_already_recorded",
    ) -> None:
        super().__init__(
            f"Source Deliverable Production "
            f"{source_deliverable_production_id!r} is already the source of "
            "a Milestone Acceptance Record; Requirement 28.3 permits at most "
            "one Milestone Acceptance per source Production Record "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.source_deliverable_production_id = (
            source_deliverable_production_id
        )
        self.existing_milestone_acceptance_id = (
            existing_milestone_acceptance_id
        )
        self.failed_constraint = failed_constraint


class MilestoneAcceptanceAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Milestone
    Acceptance attempt.

    Carries only ``reason_code`` and ``correlation_id`` — the
    indistinguishable-denial invariant forbids leaking authorized
    Party identities, target existence, or role-assignment details
    beyond the requesting Party's view authority through the denial
    response (Requirement 28.5 / AD-WS-9 / Slice 3 Requirement 30.4).
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Milestone Acceptance creation denied: "
            f"reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class MilestoneAcceptanceAuditFailureError(RuntimeError):
    """Raised when every retry of the Denial Record append fails.

    On total audit-append failure the exception is raised *in place
    of* :class:`MilestoneAcceptanceAuthorizationError` — denial and
    audit have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no
    ``Milestone_Acceptance_Records`` row, ``Relationships`` row,
    or consequential audit row is persisted.

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
            f"Denial Record append for a denied Milestone Acceptance "
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
class CreateMilestoneAcceptanceResult:
    """Result of
    :meth:`MilestoneAcceptanceService.create_milestone_acceptance`.

    Returned so callers (the HTTP layer, tests, the Completion service
    that consumes accepted Milestones per Requirement 29.1, the
    Provenance_Navigator that traverses the Execution Provenance
    Chain, and the execution-status Projection) can correlate the
    created Milestone Acceptance Record with its ``Addresses``
    Relationship and its consequential audit row in one round-trip.

    Attributes:
        milestone_acceptance_id: The Milestone Acceptance Record
            Identity (UUIDv7).
        source_deliverable_production_id: The source Deliverable
            Production Record Identity; copied byte-equivalent from
            the request input.
        produced_deliverable_id: The produced Deliverable Resource
            Identity resolved from the source Production Record's
            ``Produces`` Relationship.
        produced_deliverable_revision_id: The produced Deliverable
            Revision Identity resolved from the same ``Produces``
            Relationship.
        target_deliverable_expectation_id: The target Deliverable
            Expectation Resource Identity resolved from the source
            Production Record's ``Addresses`` Relationship.
        target_deliverable_expectation_revision_id: The target
            Deliverable Expectation Revision Identity resolved from
            the same ``Addresses`` Relationship.
        outcome: The persisted milestone-acceptance outcome, drawn
            from :data:`OUTCOME_VALUES`.
        rationale: The persisted acceptance rationale (1..4000 chars).
        accepting_party_id: The accepting Party Identity; copied
            byte-equivalent from the request input.
        authority_basis: The validated :class:`AuthorityBasisRef`
            recorded on the Milestone Acceptance Record.
        applicable_scope: Scope identifier the Acceptance applies
            within.
        addresses_relationship_id: Identity of the single ``Addresses``
            ``Relationships`` row binding the Acceptance to the
            produced Deliverable Revision.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Milestone_Acceptance_Records`` row, the
            ``Relationships`` row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    milestone_acceptance_id: str
    source_deliverable_production_id: str
    produced_deliverable_id: str
    produced_deliverable_revision_id: str
    target_deliverable_expectation_id: str
    target_deliverable_expectation_revision_id: str
    outcome: str
    rationale: str
    accepting_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str



# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MilestoneAcceptanceService:
    """Persist immutable Milestone Acceptance Records and the
    ``Addresses`` Relationship to the produced Deliverable Revision
    per AD-WS-26.

    Like its Slice 3 siblings (e.g.,
    :class:`walking_slice.execution.deliverable_productions.DeliverableProductionService`),
    this service is connection-scoped at call time:
    :meth:`create_milestone_acceptance` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (Slice 1 AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.MilestoneAcceptances" declares it
    ``@dataclass(frozen=True)`` — Slice 3 service instances follow the
    Slice 2 convention of being immutable container objects that
    bundle their collaborators.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Milestone_Acceptance_Records`` row, the ``Addresses``
            ``Relationships`` row, and the consequential
            ``Audit_Records`` row. The clock is consulted exactly
            once per write so every artifact of the transaction
            shares one timestamp (design §"Cross-Cutting Concerns" —
            Transactionality).
        identity_service: Generates the Milestone Acceptance Record
            Identity (UUIDv7) and the single ``Addresses``
            Relationship Identity, plus drives the
            ``Identifier_Registry`` binding via
            :func:`_record_execution_artifact` (the Acceptance
            binding carries the Slice 3
            ``resource_kind='milestone_acceptance_record'`` tag per
            AD-WS-28).
        audit_log: Appends the consequential audit row
            (Requirement 28.6) inside the caller's transaction; the
            denial-side audit append (separate transaction) is
            driven by :meth:`_persist_denial`.
        authorization_service: Evaluates
            ``create.milestone_acceptance`` authority (mapped to
            ``accept_milestone`` per Requirement 32.8) and
            ``view.milestone_acceptance`` authority (mapped to
            ``view`` via the action-prefix fallback). The
            ``create.milestone_acceptance`` deny path is the Slice 1
            separate-transaction Denial-Record pattern with three
            retries per Requirement 30.6; the
            ``view.milestone_acceptance`` evaluation drives the
            AD-WS-9 conflict-existing-Identity gate in
            :meth:`_resolve_conflict_visibility`.
        production_reader: The Slice 3
            :class:`DeliverableProductionService` retained as a
            collaborator for design alignment (design
            §"Execution_Service.MilestoneAcceptances" declares it on
            the dataclass surface). The current implementation
            resolves Deliverable Production rows and their
            ``Produces`` / ``Addresses`` Relationships via direct
            SQL on the caller's connection (per design's
            "resolve... from the source Deliverable Production
            Record's ``Produces`` and ``Addresses`` Relationships");
            holding the reader as a field preserves the option of
            delegating to it without changing the public
            dataclass surface.
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
    production_reader: DeliverableProductionService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)


    # -- public surface ----------------------------------------------------

    def create_milestone_acceptance(
        self,
        connection: Connection,
        *,
        source_deliverable_production_id: str,
        outcome: Literal["Accept", "Reject"],
        rationale: str,
        accepting_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateMilestoneAcceptanceResult:
        """Create an immutable Milestone Acceptance Record and its
        ``Addresses`` Relationship per AD-WS-26.

        Per Requirements 28.1 through 28.8, AD-WS-9 (indistinguishable
        denial), AD-WS-26 (Relationship-Type / semantic-role table),
        AD-WS-27 (append-only Slice 3 tables), AD-WS-28 (additive
        ``resource_kind`` values), and Requirement 32.8
        (``create.milestone_acceptance`` → ``accept_milestone``):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
        2. Input validation (Requirement 28.2 / 28.4) — every range,
           required-attribute, and authority-basis-enumeration check
           runs before any database read so a malformed request never
           touches identity service or the authorization service.
        3. Resolve the source Deliverable Production Record via a
           single indexed SELECT on
           ``Deliverable_Production_Records``. Reject when
           unresolvable
           (:class:`MilestoneAcceptanceProductionNotResolvableError`).
           Capture ``applicable_scope`` for downstream stages.
        4. Resolve the produced Deliverable Revision and target
           Deliverable Expectation Revision Identities from the
           source Production Record's ``Produces`` and ``Addresses``
           Relationship rows respectively (design
           §"Execution_Service.MilestoneAcceptances" Responsibility).
           Reject when either Relationship row is absent
           (:class:`MilestoneAcceptanceProductionRelationshipsCorruptError`).
        5. Pre-check the
           ``UNIQUE(source_deliverable_production_id)`` constraint per
           Requirement 28.3. When a Milestone Acceptance already
           exists for the source Production Record, evaluate
           ``view.milestone_acceptance`` for the requesting Party and
           raise :class:`MilestoneAcceptanceConflictError` with the
           existing Identity populated only when view authority is
           held (AD-WS-9 / Slice 3 Requirement 30.4).
        6. Run the ``create.milestone_acceptance`` authorization
           evaluation on a *separate* transaction. The authorization
           target is the source Deliverable Production Record — the
           Acceptance is scoped against the Production it accepts. On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 30.6 retry sequence and
           raise :class:`MilestoneAcceptanceAuthorizationError`. On
           total audit failure raise
           :class:`MilestoneAcceptanceAuditFailureError` in place of
           :class:`MilestoneAcceptanceAuthorizationError`.
        7. Mint the Milestone Acceptance Record Identity and the
           ``Addresses`` Relationship Identity and register the
           Acceptance Identity in ``Identifier_Registry`` (kind
           ``'immutable_record'``, carrying the Slice 3
           ``resource_kind='milestone_acceptance_record'`` tag per
           AD-WS-28) via :func:`_record_execution_artifact`.
        8. INSERT the ``Milestone_Acceptance_Records`` row.
        9. INSERT the ``Addresses`` Relationship to the produced
           Deliverable Revision (``semantic_role IS NULL``).
        10. Append the consequential ``Audit_Records`` row with
            ``action_type='create.milestone_acceptance'`` and
            ``target_id=milestone_acceptance_id`` inside the same
            transaction (Requirement 28.6).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            source_deliverable_production_id: Identity of the source
                Deliverable Production Record (Requirement 28.2).
                Must resolve to an existing
                ``Deliverable_Production_Records`` row, and the row's
                ``Produces`` and ``Addresses`` Relationship rows
                must both be present.
            outcome: Milestone-acceptance outcome drawn from
                :data:`OUTCOME_VALUES` (Requirement 28.2).
            rationale: Acceptance rationale of 1..4000 characters
                (Requirement 28.2).
            accepting_party_id: Identity of the accepting Milestone
                Acceptance Authority Party (Requirement 28.2 / 28.5).
            authority_basis: Authority basis recorded on the
                Acceptance Record. Accepted as either
                :class:`AuthorityBasisRef` or a mapping convertible
                to one; the ``type`` must be drawn from
                ``{role-grant-id, scope-id, delegation-chain-id}``
                per AD-WS-10 / Requirement 28.2.
            applicable_scope: Scope identifier the Acceptance applies
                within. Passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`.
            engine: Required for the deny path's
                separate-transaction Denial Record write so the row
                survives the caller's rollback, and for the
                conflict-pre-check view-authority evaluation.
            correlation_id: Optional correlation identifier shared by
                every audit row written in this operation. A UUIDv7
                is generated when omitted.
            evaluation_at: Optional explicit effective time passed to
                :meth:`AuthorizationService.evaluate`. Defaults to
                the recorded time of this transaction.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every prohibited
                planning-attribute and observed-outcome prefix.

        Returns:
            :class:`CreateMilestoneAcceptanceResult` carrying the
            persisted Acceptance Identity, the resolved Deliverable
            Resource / Revision / Expectation Resource / Expectation
            Revision Identities, every persisted attribute, the
            ``Addresses`` Relationship Identity, the recorded time,
            and the correlation identifier.

        Raises:
            MilestoneAcceptanceValidationError: A required attribute
                is missing, a Requirement 28.2 range was violated,
                the outcome / authority basis is malformed, or the
                request body carried a prohibited planning-attribute
                or observed-outcome key.
            MilestoneAcceptanceProductionNotResolvableError: The
                source Deliverable Production Identity did not
                resolve (Requirement 28.4).
            MilestoneAcceptanceProductionRelationshipsCorruptError:
                The source Production Record exists but its
                ``Produces`` or ``Addresses`` Relationship row is
                absent (database integrity violation surfaced as a
                distinct failure mode).
            MilestoneAcceptanceConflictError: A Milestone Acceptance
                Record already exists for the source Production
                Record (Requirement 28.3 / 28.4). The exception
                exposes the existing Acceptance Identity only when
                the caller holds ``view`` authority on it (AD-WS-9).
            MilestoneAcceptanceAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 28.5). The Denial Record was appended
                successfully in a separate transaction.
            MilestoneAcceptanceAuditFailureError: Authorization
                denied the attempt *and* the separate-transaction
                Denial Record append failed on every retry. Replaces
                :class:`MilestoneAcceptanceAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed; the surrounding transaction
                MUST be allowed to roll back per Requirement 28.7.
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
                raise MilestoneAcceptanceValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 28.2 / 28.4) before any
        # database read or authorization side-effect.
        self._validate_required_strings(
            source_deliverable_production_id=source_deliverable_production_id,
            accepting_party_id=accepting_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_outcome(outcome)
        self._validate_rationale(rationale)
        normalized_basis = self._validate_authority_basis(authority_basis)

        # 3. Resolve the source Deliverable Production Record via a
        # single indexed SELECT on
        # ``Deliverable_Production_Records``. The lookup runs on the
        # caller's connection so it participates in the caller's
        # transactional view. Requirement 28.4 rejects the
        # unresolvable case before authorization evaluates the
        # request so the deny path cannot reveal whether the
        # Production Record exists. The Production Record's
        # ``applicable_scope`` is captured here so the authorization
        # evaluation in step 6 can target the Production with its
        # persisted scope rather than a forged value from the
        # request.
        production_row = connection.execute(
            text(
                "SELECT deliverable_production_id, applicable_scope "
                "FROM Deliverable_Production_Records "
                "WHERE deliverable_production_id = "
                ":deliverable_production_id"
            ),
            {
                "deliverable_production_id": (
                    source_deliverable_production_id
                ),
            },
        ).mappings().first()
        if production_row is None:
            raise MilestoneAcceptanceProductionNotResolvableError(
                source_deliverable_production_id=(
                    source_deliverable_production_id
                ),
            )

        # 4. Resolve the produced Deliverable Revision and target
        # Deliverable Expectation Revision Identities from the
        # source Production Record's ``Produces`` and ``Addresses``
        # Relationship rows respectively. Per AD-WS-26 the
        # ``Produces`` row has ``source_kind =
        # 'deliverable_production_record'`` and ``target_kind =
        # 'deliverable_revision'``; the ``Addresses`` row has
        # ``target_kind = 'deliverable_expectation_revision'``. The
        # ``target_id`` column holds the Resource Identity and the
        # ``target_revision_id`` column holds the Revision Identity
        # (both are populated for Revision-scoped Relationships per
        # AD-WS-26). An absent row indicates a database integrity
        # violation and surfaces as
        # :class:`MilestoneAcceptanceProductionRelationshipsCorruptError`
        # rather than a downstream NULL FK constraint failure on
        # the INSERT.
        produces_row = connection.execute(
            text(
                "SELECT target_id, target_revision_id "
                "FROM Relationships "
                "WHERE source_kind = :source_kind "
                "  AND source_id = :source_id "
                "  AND relationship_type = :relationship_type "
                "  AND target_kind = :target_kind"
            ),
            {
                "source_kind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
                "source_id": source_deliverable_production_id,
                "relationship_type": _RELATIONSHIP_TYPE_PRODUCES,
                "target_kind": _KIND_DELIVERABLE_REVISION,
            },
        ).mappings().first()
        if produces_row is None:
            raise MilestoneAcceptanceProductionRelationshipsCorruptError(
                source_deliverable_production_id=(
                    source_deliverable_production_id
                ),
                missing_relationship_type=_RELATIONSHIP_TYPE_PRODUCES,
            )
        produced_deliverable_id = produces_row["target_id"]
        produced_deliverable_revision_id = produces_row["target_revision_id"]

        addresses_row = connection.execute(
            text(
                "SELECT target_id, target_revision_id "
                "FROM Relationships "
                "WHERE source_kind = :source_kind "
                "  AND source_id = :source_id "
                "  AND relationship_type = :relationship_type "
                "  AND target_kind = :target_kind"
            ),
            {
                "source_kind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
                "source_id": source_deliverable_production_id,
                "relationship_type": _RELATIONSHIP_TYPE_ADDRESSES,
                "target_kind": _KIND_DELIVERABLE_EXPECTATION_REVISION,
            },
        ).mappings().first()
        if addresses_row is None:
            raise MilestoneAcceptanceProductionRelationshipsCorruptError(
                source_deliverable_production_id=(
                    source_deliverable_production_id
                ),
                missing_relationship_type=_RELATIONSHIP_TYPE_ADDRESSES,
            )
        target_deliverable_expectation_id = addresses_row["target_id"]
        target_deliverable_expectation_revision_id = (
            addresses_row["target_revision_id"]
        )

        # 5. Pre-check the Requirement 28.3 uniqueness invariant. The
        # ``UNIQUE(source_deliverable_production_id)`` constraint on
        # ``Milestone_Acceptance_Records`` is the source of truth;
        # the pre-check surfaces a structured
        # :class:`MilestoneAcceptanceConflictError` with the
        # existing Acceptance Identity in place of a generic
        # :class:`IntegrityError`. AD-WS-9 / Slice 3 Requirement
        # 30.4 require the existing Identity to be exposed only
        # when the caller holds ``view`` authority on it; the
        # service therefore evaluates ``view.milestone_acceptance``
        # against the existing row on a separate transaction and
        # nulls out ``existing_milestone_acceptance_id`` when view
        # authority is denied. The pre-check runs before the
        # ``create.milestone_acceptance`` evaluation so an
        # unauthorized caller cannot distinguish a uniqueness
        # conflict from a missing authority via the authorization
        # audit trail.
        existing_id_row = connection.execute(
            text(
                "SELECT milestone_acceptance_id "
                "FROM Milestone_Acceptance_Records "
                "WHERE source_deliverable_production_id = "
                ":source_deliverable_production_id"
            ),
            {
                "source_deliverable_production_id": (
                    source_deliverable_production_id
                ),
            },
        ).mappings().first()
        if existing_id_row is not None:
            existing_milestone_acceptance_id = existing_id_row[
                "milestone_acceptance_id"
            ]
            visible_existing_id = self._resolve_conflict_visibility(
                engine=engine,
                accepting_party_id=accepting_party_id,
                existing_milestone_acceptance_id=(
                    existing_milestone_acceptance_id
                ),
                applicable_scope=production_row["applicable_scope"],
                evaluation_at=evaluation_at,
            )
            raise MilestoneAcceptanceConflictError(
                source_deliverable_production_id=(
                    source_deliverable_production_id
                ),
                existing_milestone_acceptance_id=visible_existing_id,
            )

        # 6. Capture one recorded time for the entire write so the
        # Milestone Acceptance row, the ``Addresses`` Relationship
        # row, and the consequential audit row share a single
        # timestamp (design §"Cross-Cutting Concerns" —
        # Transactionality).
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 7. Run the ``create.milestone_acceptance`` authorization
        # evaluation on a SEPARATE transaction. The authorization
        # target is the source Deliverable Production Record — the
        # Acceptance is scoped against the Production it accepts.
        # The scope is the persisted ``applicable_scope`` of the
        # Production Record so a forged request that names a
        # different scope from the Production cannot inflate its
        # effective authority. NOTE: this service does NOT perform
        # the AD-WS-29 second-stage assignee-binding check because
        # ``create.milestone_acceptance`` requires the
        # ``accept_milestone`` authority (Requirement 32.8) rather
        # than ``contribute``; a Milestone Acceptance Authority is
        # by design a Party distinct from the assignee on the
        # source Work Assignment.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=accepting_party_id,
                action=_ACTION_CREATE_MILESTONE_ACCEPTANCE,
                target=TargetRef(
                    kind=_KIND_DELIVERABLE_PRODUCTION_RECORD,
                    id=source_deliverable_production_id,
                    revision_id=None,
                    scope=production_row["applicable_scope"],
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_denial(
                engine=engine,
                actor_party_id=accepting_party_id,
                source_deliverable_production_id=(
                    source_deliverable_production_id
                ),
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise MilestoneAcceptanceAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 8. Mint identifiers (AD-WS-2 / AD-WS-28). Milestone
        # Acceptance Records are Immutable Records (Governance
        # Decision Immutable Records per ``02-domain-model.md`` §8.5)
        # so the Record identifier is minted via
        # :meth:`IdentityService.new_immutable_record_id`. The
        # ``Addresses`` Relationship Identity is minted via
        # :meth:`IdentityService.new_relationship_id`.
        milestone_acceptance_id = str(
            self.identity_service.new_immutable_record_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Acceptance identifier in
        # ``Identifier_Registry``; the digest is the SHA-256 of the
        # canonical JSON payload of the Record so two different
        # Milestone Acceptance Records never collide on the same
        # digest. ``authority_basis.id`` is normalized to its string
        # form for the canonical payload because UUID objects are
        # not natively JSON-serializable.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "source_deliverable_production_id": (
                        source_deliverable_production_id
                    ),
                    "produced_deliverable_id": produced_deliverable_id,
                    "produced_deliverable_revision_id": (
                        produced_deliverable_revision_id
                    ),
                    "target_deliverable_expectation_id": (
                        target_deliverable_expectation_id
                    ),
                    "target_deliverable_expectation_revision_id": (
                        target_deliverable_expectation_revision_id
                    ),
                    "outcome": outcome,
                    "rationale": rationale,
                    "accepting_party_id": accepting_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                    "recorded_at": recorded_at,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 9. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-28
        # ``resource_kind='milestone_acceptance_record'`` tag.
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_MILESTONE_ACCEPTANCE,
            milestone_acceptance_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=accepting_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_MILESTONE_ACCEPTANCE,
            recorded_time=recorded_time,
        )

        # 10. Insert the Milestone Acceptance Record carrying every
        # Requirement 28.2 attribute, including the produced
        # Deliverable Resource and Revision Identities and the
        # target Deliverable Expectation Resource and Revision
        # Identities resolved in step 4 from the Production
        # Record's Relationships. Persisting both Resource and
        # Revision Identities matches the schema columns and
        # mirrors the Deliverable Production Record's own column
        # contract.
        connection.execute(
            text(
                """
                INSERT INTO Milestone_Acceptance_Records (
                    milestone_acceptance_id,
                    source_deliverable_production_id,
                    produced_deliverable_id,
                    produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    outcome, rationale, accepting_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :milestone_acceptance_id,
                    :source_deliverable_production_id,
                    :produced_deliverable_id,
                    :produced_deliverable_revision_id,
                    :target_deliverable_expectation_id,
                    :target_deliverable_expectation_revision_id,
                    :outcome, :rationale, :accepting_party_id,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "milestone_acceptance_id": milestone_acceptance_id,
                "source_deliverable_production_id": (
                    source_deliverable_production_id
                ),
                "produced_deliverable_id": produced_deliverable_id,
                "produced_deliverable_revision_id": (
                    produced_deliverable_revision_id
                ),
                "target_deliverable_expectation_id": (
                    target_deliverable_expectation_id
                ),
                "target_deliverable_expectation_revision_id": (
                    target_deliverable_expectation_revision_id
                ),
                "outcome": outcome,
                "rationale": rationale,
                "accepting_party_id": accepting_party_id,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 11. Insert the ``Addresses`` Relationship binding the
        # Milestone Acceptance Record to the produced Deliverable
        # Revision per Requirement 28.2 / Slice 1 §10.9.
        # ``semantic_role`` is NULL — the ``Addresses`` relationship
        # type carries no role discriminator per the AD-WS-26 table.
        # The ``target_id`` is the produced Deliverable Resource
        # Identity and the ``target_revision_id`` is the produced
        # Deliverable Revision Identity (both populated for
        # Revision-scoped Addresses rows per AD-WS-26).
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
                "source_kind": _KIND_MILESTONE_ACCEPTANCE_RECORD,
                "source_id": milestone_acceptance_id,
                "source_revision_id": None,
                "target_kind": _KIND_DELIVERABLE_REVISION,
                "target_id": produced_deliverable_id,
                "target_revision_id": produced_deliverable_revision_id,
                "authoring_party_id": accepting_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 12. Append the consequential audit row (Requirement 28.6 /
        # Slice 1 AD-WS-5). Participates in the caller's
        # transaction so a failure here rolls back the registry,
        # the ``Milestone_Acceptance_Records`` row, and the
        # ``Relationships`` row together. ``target_id`` is the
        # Acceptance Record Identity; ``target_revision_id`` is
        # ``None`` because Milestone Acceptance Records are
        # Record-scoped (Requirement 22.2 — no separate Revision).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=accepting_party_id,
            action_type=_ACTION_CREATE_MILESTONE_ACCEPTANCE,
            target_id=milestone_acceptance_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateMilestoneAcceptanceResult(
            milestone_acceptance_id=milestone_acceptance_id,
            source_deliverable_production_id=(
                source_deliverable_production_id
            ),
            produced_deliverable_id=produced_deliverable_id,
            produced_deliverable_revision_id=(
                produced_deliverable_revision_id
            ),
            target_deliverable_expectation_id=(
                target_deliverable_expectation_id
            ),
            target_deliverable_expectation_revision_id=(
                target_deliverable_expectation_revision_id
            ),
            outcome=outcome,
            rationale=rationale,
            accepting_party_id=accepting_party_id,
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
        source_deliverable_production_id: Any,
        accepting_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 28.2 / 28.4: a Milestone Acceptance creation
        request that omits the source Deliverable Production Record
        Identity, the accepting Party Identity, or the applicable
        scope is rejected with no Acceptance Record created. Each
        missing attribute surfaces a distinct ``failed_constraint``
        so the HTTP layer can identify the precise field to the
        client.
        """
        if not source_deliverable_production_id or not isinstance(
            source_deliverable_production_id, str
        ):
            raise MilestoneAcceptanceValidationError(
                "source_deliverable_production_id is required; "
                "Requirement 28.4 rejects Acceptances missing the source "
                "Deliverable Production Identity.",
                failed_constraint="source_deliverable_production_id_missing",
            )
        if not accepting_party_id or not isinstance(accepting_party_id, str):
            raise MilestoneAcceptanceValidationError(
                "accepting_party_id is required; Requirement 28.5 rejects "
                "unauthenticated Milestone Acceptance creation.",
                failed_constraint="accepting_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise MilestoneAcceptanceValidationError(
                "applicable_scope is required; Requirement 28.4 rejects "
                "Acceptances missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_outcome(outcome: Any) -> None:
        """Reject submissions whose ``outcome`` is missing or outside the
        Requirement 28.2 enumeration.

        Per Requirement 28.2 the outcome is drawn from the enumerated
        set ``{Accept, Reject}``. The schema-level CHECK on
        ``Milestone_Acceptance_Records.outcome`` enforces the same
        membership; surfacing the limit here yields a precise
        ``failed_constraint`` for the HTTP layer rather than a raw
        SQL constraint violation.
        """
        if outcome is None or not isinstance(outcome, str) or outcome == "":
            raise MilestoneAcceptanceValidationError(
                "outcome is required and must be one of "
                f"{list(OUTCOME_VALUES)} per Requirement 28.2.",
                failed_constraint="outcome_missing",
            )
        if outcome not in OUTCOME_VALUES:
            raise MilestoneAcceptanceValidationError(
                f"outcome {outcome!r} is not in the Requirement 28.2 "
                f"enumeration {list(OUTCOME_VALUES)}.",
                failed_constraint="outcome_out_of_set",
            )

    @staticmethod
    def _validate_rationale(rationale: Any) -> None:
        """Reject acceptance rationale outside the Requirement 28.2 range.

        Per Requirement 28.2 the rationale is 1..4000 characters and
        is *required* (unlike Deliverable Production's optional
        rationale). The
        ``Milestone_Acceptance_Records.rationale`` CHECK constraint
        ``length(rationale) BETWEEN 1 AND 4000`` enforces the same
        range at the database layer.
        """
        if rationale is None or not isinstance(rationale, str):
            raise MilestoneAcceptanceValidationError(
                "rationale is required and must be a string of "
                f"{_RATIONALE_MIN_CHARS}..{_RATIONALE_MAX_CHARS} characters "
                "per Requirement 28.2.",
                failed_constraint="rationale_missing",
            )
        if len(rationale) < _RATIONALE_MIN_CHARS:
            raise MilestoneAcceptanceValidationError(
                f"rationale length {len(rationale)} is below the "
                f"{_RATIONALE_MIN_CHARS}-character minimum imposed by "
                "Requirement 28.2.",
                failed_constraint="rationale_too_short",
            )
        if len(rationale) > _RATIONALE_MAX_CHARS:
            raise MilestoneAcceptanceValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_RATIONALE_MAX_CHARS}-character limit imposed by "
                "Requirement 28.2.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 28.2 / AD-WS-10: the authority basis ``type``
        is drawn from ``{role-grant-id, scope-id, delegation-chain-id}``.
        The Python-typed signature already constrains callers to pass
        an :class:`AuthorityBasisRef` whose ``type`` Literal restricts
        the enumeration; the HTTP layer may pass a dict if it has not
        yet bound the request to the typed model, so this validator
        coerces both shapes (mirroring the Slice 3 sibling validators).
        """
        if isinstance(authority_basis, AuthorityBasisRef):
            return authority_basis

        if not isinstance(authority_basis, Mapping):
            raise MilestoneAcceptanceValidationError(
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
            raise MilestoneAcceptanceValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise MilestoneAcceptanceValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise MilestoneAcceptanceValidationError(
                "authority_basis.id is required per Requirement 28.2.",
                failed_constraint="authority_basis_id_missing",
            )

        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise MilestoneAcceptanceValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    # -- AD-WS-9 conflict-visibility helper -------------------------------

    def _resolve_conflict_visibility(
        self,
        *,
        engine: Engine,
        accepting_party_id: str,
        existing_milestone_acceptance_id: str,
        applicable_scope: str,
        evaluation_at: Optional[datetime],
    ) -> Optional[str]:
        """Return the existing Milestone Acceptance Identity when the
        caller holds view authority on it; otherwise return ``None``.

        Implements the AD-WS-9 / Slice 3 Requirement 30.4
        view-authority gate on the
        :class:`MilestoneAcceptanceConflictError` response: when a
        Milestone Acceptance Record already exists for the supplied
        source Deliverable Production Record, the conflict body
        carries the existing ``milestone_acceptance_id`` only if the
        requesting Party would be permitted to view it. Otherwise the
        body is byte-equivalent to a response that lacks the
        existing-Identity field, keeping the HTTP response
        indistinguishable from a non-existent endpoint.

        Evaluates ``view.milestone_acceptance`` on a *separate*
        transaction (same pattern as
        :meth:`create_milestone_acceptance`'s main authorization
        evaluation) so the read does not pollute the caller's
        transactional view and so the AD-WS-9 evaluation audit row
        survives independently of the conflict path.

        Args:
            engine: SQLAlchemy engine used to open the separate
                evaluation transaction.
            accepting_party_id: Identity of the requesting Party.
            existing_milestone_acceptance_id: The Acceptance Identity
                that already targets the source Production Record.
            applicable_scope: Scope passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`. Pulled from
                the Production Record's persisted
                ``applicable_scope`` so a forged request cannot
                inflate effective authority through scope
                substitution.
            evaluation_at: Optional explicit effective time; falls
                back to the AuthorizationService's clock when
                omitted.

        Returns:
            ``existing_milestone_acceptance_id`` when the view
            evaluation returns ``permit``; ``None`` otherwise.
        """
        at_when = evaluation_at if evaluation_at is not None else self.clock.now()
        with engine.begin() as eval_conn:
            view_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=accepting_party_id,
                action=_ACTION_VIEW_MILESTONE_ACCEPTANCE,
                target=TargetRef(
                    kind=_KIND_MILESTONE_ACCEPTANCE_RECORD,
                    id=existing_milestone_acceptance_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=at_when,
            )
        if view_outcome.is_permit:
            return existing_milestone_acceptance_id
        return None

    # -- denial side-channel ----------------------------------------------

    def _persist_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        source_deliverable_production_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Milestone Acceptance
        attempt.

        Implements the AD-WS-9 / Slice 1 Requirement 7.6 / Slice 3
        Requirement 30.6 retry contract verbatim (mirroring the
        sibling Slice 3 services). Each attempt opens a *new*
        :meth:`Engine.begin` transaction so a previous attempt's
        rollback does not poison this one, tries
        :meth:`AuditLog.append_denial`, and either returns on success
        or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails,
          :class:`MilestoneAcceptanceAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_milestone_acceptance` raises an authorization
        error. The Denial Record must therefore live outside that
        scope to survive (AD-WS-9 / Requirement 30.6).

        ``target_id`` on the Denial Record points at the source
        Deliverable Production Identity because the Milestone
        Acceptance Identity has not yet been minted at the time the
        denial is recorded — the deny path explicitly refuses to mint
        an Immutable Record Identity for an unauthorized attempt
        (Requirement 28.5 / Requirement 30.5 — no information leakage
        about the existence of restricted Records).

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
                        attempted_action=(
                            _ACTION_CREATE_MILESTONE_ACCEPTANCE
                        ),
                        target_id=source_deliverable_production_id,
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

        raise MilestoneAcceptanceAuditFailureError(
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
    consequential audit row produced for the same logical Milestone
    Acceptance creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Acceptance
    Identity in ``Identifier_Registry``. Milestone Acceptance Records
    are Record-scoped (Requirement 22.2 — no separate Revision) so
    this digest is bound exactly once per Acceptance creation.
    """
    return hashlib.sha256(content).hexdigest()
