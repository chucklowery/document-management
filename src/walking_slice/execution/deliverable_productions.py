"""Execution_Service.DeliverableProductions — immutable Deliverable
Production Records that bind a produced Deliverable Revision to the
addressed Deliverable Expectation Revision under the authority of a
source Work Assignment Record.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Execution_Service.DeliverableProductions" — public dataclass
  surface, authority string (``create.deliverable_production`` →
  ``contribute`` AND assignee binding per AD-WS-24 and AD-WS-29),
  AD-WS-9 separate-transaction Denial Record on deny, validation
  order, the project-membership check (Requirement 27.3 — walk
  ``wa.target_plan_revision_id`` → Activity Plan → Project via
  :class:`ProjectResolver` and require equality with
  ``deliverable_expectation_revision.target_project_id``), the
  originating-binding check (Requirement 27.4 — require the produced
  Deliverable Revision's ``originating_work_assignment_id`` to equal
  the supplied ``source_work_assignment_id``), and the
  Relationship-row contract (AD-WS-26): one ``Produces`` Relationship
  to the produced Deliverable Revision with ``semantic_role IS NULL``,
  one ``Addresses`` Relationship to the target Deliverable Expectation
  Revision with ``semantic_role IS NULL``, and one ``Relates To``
  Relationship to the source Work Assignment Record with
  ``semantic_role = 'production_source'``.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and
  registered in ``Identifier_Registry`` with
  ``kind = 'immutable_record'`` and
  ``resource_kind = 'deliverable_production_record'`` per AD-WS-28);
  Authorization (the action string ``create.deliverable_production``
  maps to the ``contribute`` authority per AD-WS-24; the deny path
  uses the Slice 1 separate-transaction Denial-Record pattern with
  the Slice 3 Requirement 30.6 three-retry contract).
- AD-WS-24 — additive ``contribute`` mapping for
  ``create.deliverable_production``.
- AD-WS-26 — ``Deliverable Production Record`` carries three
  Relationships: ``Produces`` to the produced Deliverable Revision
  (``semantic_role IS NULL``), ``Addresses`` to the target Deliverable
  Expectation Revision (``semantic_role IS NULL``), and ``Relates To``
  to the source Work Assignment Record with
  ``semantic_role = 'production_source'``.
- AD-WS-27 — ``Deliverable_Production_Records`` is append-only; the
  source Work Assignment, the produced Deliverable Revision, and the
  target Deliverable Expectation Revision rows must remain
  byte-equivalent throughout this transaction.
- AD-WS-28 — additive ``Identifier_Registry.resource_kind`` value
  ``'deliverable_production_record'`` populated through
  :func:`walking_slice.execution._helpers._record_execution_artifact`.
- AD-WS-29 — two-stage authority evaluation for Contributor writes:
  the service first calls :meth:`AuthorizationService.evaluate` with
  the Work Assignment Record as the target, and then on a ``permit``
  outcome re-reads the persisted ``Work_Assignment_Records`` row
  inside the caller's transaction and requires
  ``assignee_party_id == recording_party_id``. Both stages must pass;
  a failure of either stage produces an AD-WS-9-conformant denial
  response (Slice 1 Requirement 7.2 ``reason_code = 'no-role-assignment'``
  for the assignee-binding failure).
- AD-WS-30 — Slice 3 callers read Planning_Service public APIs only.
  This module consults
  :meth:`DeliverableExpectationService.get_revision` (task 2.2),
  :meth:`DeliverableRepositoryService.get_revision` (task 4.2), and
  :meth:`ProjectResolver.resolve_project` (task 2.2). It never queries
  Slice 2 tables directly.

Task scope (task 9.1)
=====================

This module implements
:meth:`DeliverableProductionService.create_deliverable_production`:

1. Validate request inputs per Requirement 27.2 / 27.4:
   ``production_rationale`` length 0..4000;
   ``source_work_assignment_id``, ``produced_deliverable_revision_id``,
   ``target_deliverable_expectation_revision_id``,
   ``recording_party_id``, and ``applicable_scope`` are present;
   ``authority_basis.type`` is drawn from the AD-WS-10 set.
2. Defensively reject any prohibited planning-attribute or
   observed-outcome key in the original request body via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
3. Resolve the source Work Assignment by primary key on the caller's
   connection (Requirement 27.4 — unresolvable target rejected).
   Capture ``assignee_party_id``, ``target_plan_revision_id``, and
   ``applicable_scope`` for downstream stages.
4. Resolve the produced Deliverable Revision via
   :meth:`DeliverableRepositoryService.get_revision`. Reject when
   unresolvable.
5. Run the originating-binding check per Requirement 27.4: require
   the produced Deliverable Revision's
   ``originating_work_assignment_id`` to equal
   ``source_work_assignment_id``. On mismatch reject before any
   authorization evaluation so the deny path does not reveal the
   relationship.
6. Resolve the target Deliverable Expectation Revision via
   :meth:`DeliverableExpectationService.get_revision`. Reject when
   unresolvable.
7. Run the project-membership check per Requirement 27.3: walk
   ``wa.target_plan_revision_id`` → Activity Plan → Project via
   :meth:`ProjectResolver.resolve_project`; compare with the
   Deliverable Expectation Revision's ``target_project_id``. On
   mismatch reject with ``failed_constraint =
   'deliverable_expectation_project_mismatch'`` and persist no row.
8. Evaluate
   ``Authorization_Service.evaluate(party=recording_party_id,
   action="create.deliverable_production",
   target=work_assignment_ref, at=now())`` on a separate transaction.
   On ``deny``, persist a Denial Record from another separate
   transaction with the Slice 1 Requirement 7.6 / Slice 3
   Requirement 30.6 three-retry exponential-backoff pattern and raise
   :class:`DeliverableProductionAuthorizationError`.
9. On ``permit``, perform the AD-WS-29 second stage: the
   ``Work_Assignment_Records`` row was already loaded in step 3;
   require ``assignee_party_id == recording_party_id``. On mismatch
   append a Denial Record with ``reason_code = 'no-role-assignment'``
   in a separate transaction and raise
   :class:`DeliverableProductionAssigneeBindingError` so the caller's
   surrounding transaction rolls back without persisting any row.
10. Mint the Deliverable Production Record Identity (UUIDv7) and the
    three Relationship Identities; register the Deliverable Production
    Identity in ``Identifier_Registry`` with ``kind='immutable_record'``
    and ``resource_kind='deliverable_production_record'`` via
    :func:`_record_execution_artifact`.
11. INSERT the ``Deliverable_Production_Records`` row carrying every
    Requirement 27.2 attribute, including the produced Deliverable
    Resource Identity (looked up from the produced Revision row) and
    the target Deliverable Expectation Resource Identity (looked up
    from the Deliverable Expectation Revision row).
12. INSERT exactly one ``Relationships`` row with
    ``relationship_type='Produces'``,
    ``source_kind='deliverable_production_record'``,
    ``target_kind='deliverable_revision'``, and
    ``semantic_role IS NULL`` (AD-WS-26).
13. INSERT exactly one ``Relationships`` row with
    ``relationship_type='Addresses'``,
    ``source_kind='deliverable_production_record'``,
    ``target_kind='deliverable_expectation_revision'``, and
    ``semantic_role IS NULL`` (AD-WS-26).
14. INSERT exactly one ``Relationships`` row with
    ``relationship_type='Relates To'``,
    ``source_kind='deliverable_production_record'``,
    ``target_kind='work_assignment_record'``, and
    ``semantic_role='production_source'`` (AD-WS-26).
15. Append the consequential ``Audit_Records`` row with
    ``action_type='create.deliverable_production'`` and
    ``target_id=deliverable_production_id`` inside the same
    transaction (Requirement 27.6 / Slice 1 AD-WS-5).

Requirements satisfied
======================

    27.1 — authorized Deliverable Production creation produces exactly
           one immutable Deliverable Production Record within nominal
           latency.
    27.2 — every Deliverable Production Record records the source Work
           Assignment Record Identity, the produced Deliverable
           Resource and Revision Identities, the target Deliverable
           Expectation Resource and Revision Identities, the
           production rationale (0..4000 chars), the recording
           Contributor Party Identity, the applicable scope, the
           recorded time, exactly one ``Produces`` Relationship to the
           produced Deliverable Revision, exactly one ``Addresses``
           Relationship to the target Deliverable Expectation
           Revision, and exactly one ``Relates To`` Relationship with
           ``semantic_role='production_source'`` to the source Work
           Assignment Record.
    27.3 — the target Deliverable Expectation Revision's Project
           Identity must equal the Project Identity reached by walking
           the source Work Assignment's Plan Revision → Activity Plan
           → Project via :class:`ProjectResolver`. Mismatch is
           rejected with no Deliverable Production Record persisted.
    27.4 — unresolvable source Work Assignment, unresolvable produced
           Deliverable Revision, unresolvable target Deliverable
           Expectation Revision, and a produced Deliverable Revision
           whose ``originating_work_assignment_id`` does not match the
           supplied ``source_work_assignment_id`` are rejected with no
           Deliverable Production Record persisted.
    27.5 — unauthorized requests are denied via
           :class:`AuthorizationService`; the Execution_Service
           declines to create any Deliverable Production Record and
           the Audit_Log appends a Denial Record conforming to
           AD-WS-9.
    27.6 — the Audit_Log appends an immutable consequential audit row
           identifying the Deliverable Production Record Identity,
           source Work Assignment Record Identity, produced
           Deliverable Revision Identity, target Deliverable
           Expectation Revision Identity, recording Contributor Party
           Identity, and recorded time within the same transaction.
    27.7 — the append-only schema triggers (created in task 1.2)
           reject every UPDATE / DELETE attempt on
           ``Deliverable_Production_Records`` and the three
           ``Relationships`` rows after this transaction commits.
    32.7 — ``create.deliverable_production`` requires the
           ``contribute`` authority AND assignee binding on the source
           Work Assignment (AD-WS-24, AD-WS-29).
    41.1 — every consequential write is atomic with its consequential
           audit row.
    41.2 — every consequential write checks authority before
           persisting any domain row.
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
from walking_slice.deliverables.repository import (
    DeliverableRepositoryService,
    DeliverableRevisionRow,
)
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
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationRevisionNotResolvableError,
    DeliverableExpectationRevisionRow,
    DeliverableExpectationService,
)


__all__ = [
    "CreateDeliverableProductionResult",
    "DeliverableProductionAssigneeBindingError",
    "DeliverableProductionAuditFailureError",
    "DeliverableProductionAuthorizationError",
    "DeliverableProductionDeliverableExpectationNotResolvableError",
    "DeliverableProductionDeliverableRevisionNotResolvableError",
    "DeliverableProductionOriginatingBindingError",
    "DeliverableProductionProjectMismatchError",
    "DeliverableProductionService",
    "DeliverableProductionValidationError",
    "DeliverableProductionWorkAssignmentNotResolvableError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship kind / type strings, registry kind /
# resource_kind strings, and validation limits are pulled out as
# module-level ``Final`` so the names downstream property tests look
# for in ``Audit_Records.action_type``, in
# ``Identifier_Registry.resource_kind``, and in
# ``Relationships.semantic_role`` are textually stable.
# ---------------------------------------------------------------------------


# ``create.deliverable_production`` maps to the ``contribute`` authority
# per AD-WS-24. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 27.6) and on the
# separate-transaction Denial Record so audit consumers can correlate
# denial rows with the action a Party was attempting.
_ACTION_CREATE_DELIVERABLE_PRODUCTION: Final[str] = "create.deliverable_production"

# Relationship Type strings written to the three Relationship rows
# this service appends per AD-WS-26.
_RELATIONSHIP_TYPE_PRODUCES: Final[str] = "Produces"
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_RELATIONSHIP_TYPE_RELATES_TO: Final[str] = "Relates To"

# ``Relationships.source_kind`` / ``target_kind`` / ``semantic_role``
# strings per AD-WS-26. The Deliverable Production Record is the
# source of all three Relationships; the targets are the produced
# Deliverable Revision, the target Deliverable Expectation Revision,
# and the source Work Assignment Record respectively. Only the
# ``Relates To`` row carries a non-NULL ``semantic_role`` value.
_KIND_DELIVERABLE_PRODUCTION_RECORD: Final[str] = "deliverable_production_record"
_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"
_KIND_DELIVERABLE_EXPECTATION_REVISION: Final[str] = "deliverable_expectation_revision"
_KIND_WORK_ASSIGNMENT_RECORD: Final[str] = "work_assignment_record"
_SEMANTIC_ROLE_PRODUCTION_SOURCE: Final[str] = "production_source"

# Identifier_Registry registration kind (Slice 1 enumeration) and
# Execution_Service ``resource_kind`` tag (Slice 3 additive
# enumeration per AD-WS-28). Deliverable Production Records are
# Immutable Records (per ``02-domain-model.md`` §8.2 Execution
# Record) so the registry kind is ``'immutable_record'``; the
# ``resource_kind`` value is ``'deliverable_production_record'`` and
# is the row-level discriminator that keeps the Production Identity
# set inspectably disjoint from every other Slice 1 / Slice 2 /
# Slice 3 ``resource_kind`` (Requirement 22.8).
_REGISTRY_KIND_IMMUTABLE_RECORD: Final[str] = "immutable_record"
_RESOURCE_KIND_DELIVERABLE_PRODUCTION: Final[str] = "deliverable_production_record"

# Authority-basis ``type`` enumeration per AD-WS-10. Mirrors the
# Slice 3 ``Deliverable_Production_Records.authority_basis_type``
# CHECK constraint values; centralizing the tuple here lets the
# validator reject malformed requests structurally before they touch
# SQL.
_VALID_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Validation limit for ``production_rationale`` per Requirement 27.2.
# The ``Deliverable_Production_Records.production_rationale`` CHECK
# constraint enforces the same range; surfacing it here yields a
# precise ``failed_constraint`` for the HTTP layer rather than a raw
# SQL constraint violation.
_PRODUCTION_RATIONALE_MIN_CHARS: Final[int] = 0
_PRODUCTION_RATIONALE_MAX_CHARS: Final[int] = 4_000

# Denial-reason code used when authorization permits the action but
# the AD-WS-29 second stage rejects it because the recording Party is
# not the named assignee on the source Work Assignment. Slice 1
# Requirement 7.2 enumerates this value as ``'no-role-assignment'``.
_REASON_CODE_NO_ROLE_ASSIGNMENT: Final[str] = "no-role-assignment"

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


class DeliverableProductionValidationError(ValueError):
    """Raised when a Deliverable Production submission fails Requirement
    27.2 / 27.4 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    can render a structured 400 response and tests can assert against a
    stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"source_work_assignment_id_missing"``,
            ``"produced_deliverable_revision_id_missing"``,
            ``"target_deliverable_expectation_revision_id_missing"``,
            ``"recording_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"production_rationale_invalid_type"`` (not str / None),
            ``"production_rationale_too_long"`` (> 4000 characters),
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


class DeliverableProductionWorkAssignmentNotResolvableError(LookupError):
    """Raised when the source Work Assignment Identity does not resolve.

    Requirement 27.4 requires the source Work Assignment Record
    Identity to resolve to an existing Work Assignment. The check runs
    before authorization evaluation so the deny path never reveals
    whether a Work Assignment exists for an unauthorized caller.

    Attributes:
        source_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        failed_constraint:
            ``"source_work_assignment_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        source_work_assignment_id: str,
        failed_constraint: str = "source_work_assignment_not_resolvable",
    ) -> None:
        super().__init__(
            f"Source Work Assignment {source_work_assignment_id!r} did "
            "not resolve to an existing Work Assignment "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.source_work_assignment_id = source_work_assignment_id
        self.failed_constraint = failed_constraint


class DeliverableProductionDeliverableRevisionNotResolvableError(LookupError):
    """Raised when the produced Deliverable Revision Identity does not
    resolve.

    Requirement 27.4 requires the produced Deliverable Revision
    Identity to resolve to an existing row in the Deliverable
    Repository. The check runs before authorization evaluation so the
    deny path never reveals whether a Revision exists for an
    unauthorized caller.

    Attributes:
        produced_deliverable_revision_id: The Revision Identity the
            caller supplied.
        failed_constraint:
            ``"produced_deliverable_revision_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        produced_deliverable_revision_id: str,
        failed_constraint: str = "produced_deliverable_revision_not_resolvable",
    ) -> None:
        super().__init__(
            f"Produced Deliverable Revision "
            f"{produced_deliverable_revision_id!r} did not resolve to an "
            "existing Deliverable Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.produced_deliverable_revision_id = produced_deliverable_revision_id
        self.failed_constraint = failed_constraint


class DeliverableProductionDeliverableExpectationNotResolvableError(LookupError):
    """Raised when the target Deliverable Expectation Revision Identity
    does not resolve.

    Requirement 27.4 requires the target Deliverable Expectation
    Revision Identity to resolve to an existing row in the
    Planning_Service. The check runs before authorization evaluation
    so the deny path never reveals whether an Expectation Revision
    exists for an unauthorized caller.

    Attributes:
        target_deliverable_expectation_revision_id: The Expectation
            Revision Identity the caller supplied.
        failed_constraint:
            ``"target_deliverable_expectation_revision_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        target_deliverable_expectation_revision_id: str,
        failed_constraint: str = (
            "target_deliverable_expectation_revision_not_resolvable"
        ),
    ) -> None:
        super().__init__(
            f"Target Deliverable Expectation Revision "
            f"{target_deliverable_expectation_revision_id!r} did not "
            "resolve to an existing Deliverable Expectation Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_deliverable_expectation_revision_id = (
            target_deliverable_expectation_revision_id
        )
        self.failed_constraint = failed_constraint


class DeliverableProductionOriginatingBindingError(ValueError):
    """Raised when the produced Deliverable Revision's
    ``originating_work_assignment_id`` does not match the supplied
    ``source_work_assignment_id`` (Requirement 27.4).

    The check rejects forged-production attempts: a Contributor cannot
    "claim" a peer's produced Deliverable Revision as their own
    production by naming a different source Work Assignment. The
    rejection runs before authorization evaluation so the deny path
    never reveals the produced Revision's authoring chain to an
    unauthorized caller.

    Attributes:
        produced_deliverable_revision_id: The Revision Identity the
            caller supplied.
        source_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        actual_originating_work_assignment_id: The Work Assignment
            Identity persisted on the produced Revision row.
        failed_constraint:
            ``"produced_revision_originating_work_assignment_mismatch"``.
    """

    def __init__(
        self,
        *,
        produced_deliverable_revision_id: str,
        source_work_assignment_id: str,
        actual_originating_work_assignment_id: str,
        failed_constraint: str = (
            "produced_revision_originating_work_assignment_mismatch"
        ),
    ) -> None:
        super().__init__(
            f"Produced Deliverable Revision "
            f"{produced_deliverable_revision_id!r} was authored under "
            f"originating Work Assignment "
            f"{actual_originating_work_assignment_id!r}, but the request "
            f"names source Work Assignment "
            f"{source_work_assignment_id!r}; Requirement 27.4 requires the "
            "two identifiers to match "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.produced_deliverable_revision_id = produced_deliverable_revision_id
        self.source_work_assignment_id = source_work_assignment_id
        self.actual_originating_work_assignment_id = (
            actual_originating_work_assignment_id
        )
        self.failed_constraint = failed_constraint


class DeliverableProductionProjectMismatchError(ValueError):
    """Raised when the target Deliverable Expectation Revision's Project
    Identity does not match the Project Identity reached by walking the
    source Work Assignment's Plan Revision (Requirement 27.3).

    The project-membership check walks
    ``wa.target_plan_revision_id`` → Activity Plan → Project via
    :class:`ProjectResolver` and compares the result with the
    Deliverable Expectation Revision's persisted ``target_project_id``.
    A mismatch rejects the request with no row persisted; the
    rejection runs before authorization evaluation so the deny path
    never reveals the Project linkage to an unauthorized caller.

    Attributes:
        source_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        target_deliverable_expectation_revision_id: The Expectation
            Revision Identity the caller supplied.
        work_assignment_project_id: The Project Identity reached from
            the source Work Assignment's Plan Revision.
        deliverable_expectation_project_id: The Project Identity
            persisted on the Deliverable Expectation Revision row.
        failed_constraint:
            ``"deliverable_expectation_project_mismatch"``.
    """

    def __init__(
        self,
        *,
        source_work_assignment_id: str,
        target_deliverable_expectation_revision_id: str,
        work_assignment_project_id: str,
        deliverable_expectation_project_id: str,
        failed_constraint: str = "deliverable_expectation_project_mismatch",
    ) -> None:
        super().__init__(
            f"Source Work Assignment {source_work_assignment_id!r} belongs to "
            f"Project {work_assignment_project_id!r}, but target "
            f"Deliverable Expectation Revision "
            f"{target_deliverable_expectation_revision_id!r} belongs to "
            f"Project {deliverable_expectation_project_id!r}; "
            "Requirement 27.3 requires the two Projects to match "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.source_work_assignment_id = source_work_assignment_id
        self.target_deliverable_expectation_revision_id = (
            target_deliverable_expectation_revision_id
        )
        self.work_assignment_project_id = work_assignment_project_id
        self.deliverable_expectation_project_id = (
            deliverable_expectation_project_id
        )
        self.failed_constraint = failed_constraint


class DeliverableProductionAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Deliverable
    Production attempt.

    Carries only ``reason_code`` and ``correlation_id`` — the
    indistinguishable-denial invariant forbids leaking authorized Party
    identities, target existence, or role-assignment details beyond
    the requesting Party's view authority through the denial response
    (Requirement 27.5 / AD-WS-9).

    The same exception type is raised when the AD-WS-29 second stage
    fails (the recording Party is not the named assignee on the source
    Work Assignment); in that case ``reason_code`` is fixed to
    ``'no-role-assignment'`` and the
    :class:`DeliverableProductionAssigneeBindingError` subclass is
    used so tests that need to discriminate the assignee-binding path
    can do so.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Deliverable Production creation denied: "
            f"reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class DeliverableProductionAssigneeBindingError(
    DeliverableProductionAuthorizationError
):
    """Specialised :class:`DeliverableProductionAuthorizationError` for
    AD-WS-29 assignee-binding failures.

    Subclass of :class:`DeliverableProductionAuthorizationError` so
    callers that catch the broader denial path continue to work, while
    tests that need to assert specifically on the AD-WS-29 path can
    catch this narrower type. The denial response shape is identical:
    ``{reason_code='no-role-assignment', correlation_id}``.

    Attributes:
        source_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        recording_party_id: The Party Identity the caller submitted as
            the recording Contributor.
        actual_assignee_party_id: The Party Identity actually persisted
            on ``Work_Assignment_Records.assignee_party_id``.
    """

    def __init__(
        self,
        *,
        source_work_assignment_id: str,
        recording_party_id: str,
        actual_assignee_party_id: str,
        correlation_id: str,
    ) -> None:
        super().__init__(
            reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
            correlation_id=correlation_id,
        )
        self.source_work_assignment_id = source_work_assignment_id
        self.recording_party_id = recording_party_id
        self.actual_assignee_party_id = actual_assignee_party_id


class DeliverableProductionAuditFailureError(RuntimeError):
    """Raised when every retry of the Denial Record append fails.

    On total audit-append failure the exception is raised *in place
    of* :class:`DeliverableProductionAuthorizationError` — denial and
    audit have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no
    ``Deliverable_Production_Records`` row, ``Relationships`` rows,
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
            f"Denial Record append for a denied Deliverable Production "
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
class CreateDeliverableProductionResult:
    """Result of
    :meth:`DeliverableProductionService.create_deliverable_production`.

    Returned so callers (the HTTP layer, tests, the Milestone
    Acceptance service that targets a Deliverable Production Record,
    the Provenance_Navigator that traverses the Execution Provenance
    Chain, and the execution-status Projection) can correlate the
    created Deliverable Production Record with its three Relationship
    rows and its consequential audit row in one round-trip.

    Attributes:
        deliverable_production_id: The Deliverable Production Record
            Identity (UUIDv7).
        source_work_assignment_id: The source Work Assignment Record
            Identity; copied byte-equivalent from the request input.
        produced_deliverable_id: The produced Deliverable Resource
            Identity resolved from the produced Revision row.
        produced_deliverable_revision_id: The produced Deliverable
            Revision Identity; copied byte-equivalent from the request
            input.
        target_deliverable_expectation_id: The target Deliverable
            Expectation Resource Identity resolved from the Expectation
            Revision row.
        target_deliverable_expectation_revision_id: The target
            Deliverable Expectation Revision Identity; copied
            byte-equivalent from the request input.
        production_rationale: The persisted production rationale
            (0..4000 chars) or ``None`` when omitted.
        recording_party_id: The recording Contributor Party Identity;
            copied byte-equivalent from the request input.
        authority_basis: The validated :class:`AuthorityBasisRef`
            recorded on the Deliverable Production Record.
        applicable_scope: Scope identifier the Production applies
            within.
        produces_relationship_id: Identity of the single ``Produces``
            ``Relationships`` row binding the Production to the
            produced Deliverable Revision.
        addresses_relationship_id: Identity of the single
            ``Addresses`` ``Relationships`` row binding the Production
            to the target Deliverable Expectation Revision.
        relates_to_relationship_id: Identity of the single
            ``Relates To`` ``Relationships`` row binding the Production
            to the source Work Assignment Record with
            ``semantic_role='production_source'``.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Deliverable_Production_Records`` row, the
            three ``Relationships`` rows, and the consequential audit
            row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            three on a single value.
    """

    deliverable_production_id: str
    source_work_assignment_id: str
    produced_deliverable_id: str
    produced_deliverable_revision_id: str
    target_deliverable_expectation_id: str
    target_deliverable_expectation_revision_id: str
    production_rationale: Optional[str]
    recording_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    produces_relationship_id: str
    addresses_relationship_id: str
    relates_to_relationship_id: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliverableProductionService:
    """Persist immutable Deliverable Production Records and their three
    Relationships per AD-WS-26.

    Like its Slice 3 siblings
    :class:`walking_slice.execution.work_assignments.WorkAssignmentService`,
    :class:`walking_slice.execution.work_events.WorkEventService`, and
    :class:`walking_slice.execution.time_entries.TimeEntryService`, this
    service is connection-scoped at call time:
    :meth:`create_deliverable_production` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (Slice 1 AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.DeliverableProductions" declares it
    ``@dataclass(frozen=True)`` — Slice 3 service instances follow the
    Slice 2 convention of being immutable container objects that
    bundle their collaborators.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Deliverable_Production_Records``, three
            ``Relationships``, and ``Audit_Records`` rows. The clock
            is consulted exactly once per write so every artifact of
            the transaction shares one timestamp.
        identity_service: Generates the Deliverable Production Record
            Identity (UUIDv7) and the three Relationship Identities,
            plus drives the ``Identifier_Registry`` binding via
            :func:`_record_execution_artifact` (the Production binding
            carries the Slice 3
            ``resource_kind='deliverable_production_record'`` tag per
            AD-WS-28).
        audit_log: Appends the consequential audit row
            (Requirement 27.6) inside the caller's transaction; the
            denial-side audit append (separate transaction) is driven
            by :meth:`_persist_denial`.
        authorization_service: Evaluates
            ``create.deliverable_production`` authority per AD-WS-24 /
            Requirement 27.5; the deny path is the Slice 1
            separate-transaction Denial-Record pattern with three
            retries per Requirement 30.6.
        deliverable_reader: The Slice 3
            :class:`DeliverableRepositoryService` whose
            :meth:`DeliverableRepositoryService.get_revision` (task
            4.2) is consulted to resolve the produced Deliverable
            Revision row, retrieve its ``deliverable_id`` for the
            Production Record column, and read its
            ``originating_work_assignment_id`` for the Requirement
            27.4 originating-binding check.
        planning_reader: The Slice 2
            :class:`DeliverableExpectationService` whose
            :meth:`DeliverableExpectationService.get_revision` (task
            2.2 — the AD-WS-30 read API) is consulted to resolve the
            target Deliverable Expectation Revision row, retrieve its
            ``deliverable_expectation_id`` for the Production Record
            column, and read its ``target_project_id`` for the
            Requirement 27.3 project-membership check.
        project_resolver: The Slice 2
            :class:`ProjectResolver` (task 2.2) used to walk the
            source Work Assignment's Plan Revision → Activity Plan →
            Project for the Requirement 27.3 project-membership check.
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
    deliverable_reader: DeliverableRepositoryService
    planning_reader: DeliverableExpectationService
    project_resolver: ProjectResolver
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)


    # -- public surface ----------------------------------------------------

    def create_deliverable_production(
        self,
        connection: Connection,
        *,
        source_work_assignment_id: str,
        produced_deliverable_revision_id: str,
        target_deliverable_expectation_revision_id: str,
        production_rationale: Optional[str],
        recording_party_id: str,
        authority_basis: Any,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateDeliverableProductionResult:
        """Create an immutable Deliverable Production Record and its
        three Relationships per AD-WS-26.

        Per Requirements 27.1 through 27.7, AD-WS-9 (indistinguishable
        denial), AD-WS-24 (``create.deliverable_production`` →
        ``contribute``), AD-WS-26 (Relationship-Type / semantic-role
        table), AD-WS-27 (append-only Slice 3 tables), AD-WS-28
        (additive ``resource_kind`` values), AD-WS-29 (two-stage
        assignee binding), and AD-WS-30 (Planning_Service public
        read APIs):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).
        2. Input validation (Requirement 27.2 / 27.4) — every range,
           required-attribute, and authority-basis-enumeration check
           runs before any database read so a malformed request never
           touches identity service, the Planning_Service, the
           Deliverable_Repository, or the authorization service.
        3. Resolve the source Work Assignment via a single indexed
           SELECT on ``Work_Assignment_Records``. Reject when
           unresolvable
           (:class:`DeliverableProductionWorkAssignmentNotResolvableError`).
           Capture ``assignee_party_id``,
           ``target_plan_revision_id``, and ``applicable_scope`` for
           the downstream stages.
        4. Resolve the produced Deliverable Revision via
           :meth:`DeliverableRepositoryService.get_revision`. Reject
           when unresolvable
           (:class:`DeliverableProductionDeliverableRevisionNotResolvableError`).
        5. Run the originating-binding check per Requirement 27.4:
           require the produced Revision's
           ``originating_work_assignment_id`` to equal
           ``source_work_assignment_id``. On mismatch reject with
           :class:`DeliverableProductionOriginatingBindingError`.
        6. Resolve the target Deliverable Expectation Revision via
           :meth:`DeliverableExpectationService.get_revision`. Reject
           when unresolvable
           (:class:`DeliverableProductionDeliverableExpectationNotResolvableError`).
        7. Run the project-membership check per Requirement 27.3:
           walk the source Work Assignment's
           ``target_plan_revision_id`` → Activity Plan → Project via
           :meth:`ProjectResolver.resolve_project`; compare with the
           Expectation Revision's ``target_project_id``. On mismatch
           reject with
           :class:`DeliverableProductionProjectMismatchError`.
        8. Run the authorization evaluation on a *separate*
           transaction. The authorization target is the source Work
           Assignment Record — ``create.deliverable_production``
           authority is scoped against the Work Assignment the
           Production was authored under (AD-WS-29). On ``deny``,
           append the Denial Record in another separate transaction
           with the Requirement 30.6 retry sequence and raise
           :class:`DeliverableProductionAuthorizationError`. On total
           audit failure raise
           :class:`DeliverableProductionAuditFailureError` in place
           of :class:`DeliverableProductionAuthorizationError`.
        9. AD-WS-29 second stage: re-read the persisted Work
           Assignment row's ``assignee_party_id`` (captured in step
           3) and require it to equal ``recording_party_id``. On
           mismatch append a Denial Record in a separate transaction
           and raise
           :class:`DeliverableProductionAssigneeBindingError` so the
           caller's surrounding transaction rolls back without
           persisting any row.
        10. Mint the Deliverable Production Record Identity and the
            three Relationship Identities and register the Production
            Identity in ``Identifier_Registry`` (kind
            ``'immutable_record'``, carrying the Slice 3
            ``resource_kind='deliverable_production_record'`` tag per
            AD-WS-28) via :func:`_record_execution_artifact`.
        11. INSERT the ``Deliverable_Production_Records`` row.
        12. INSERT the ``Produces`` Relationship to the produced
            Deliverable Revision (``semantic_role IS NULL``).
        13. INSERT the ``Addresses`` Relationship to the target
            Deliverable Expectation Revision (``semantic_role IS NULL``).
        14. INSERT the ``Relates To`` Relationship to the source Work
            Assignment Record (``semantic_role='production_source'``).
        15. Append the consequential ``Audit_Records`` row with
            ``action_type='create.deliverable_production'`` and
            ``target_id=deliverable_production_id`` inside the same
            transaction (Requirement 27.6).

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            source_work_assignment_id: Identity of the source Work
                Assignment Record (Requirement 27.2). Must resolve to
                an existing ``Work_Assignment_Records`` row whose
                ``assignee_party_id`` equals ``recording_party_id``
                per AD-WS-29.
            produced_deliverable_revision_id: Identity of the produced
                Deliverable Revision (Requirement 27.2). Must resolve
                to an existing ``Deliverable_Revisions`` row whose
                ``originating_work_assignment_id`` equals
                ``source_work_assignment_id`` per Requirement 27.4.
            target_deliverable_expectation_revision_id: Identity of
                the target Deliverable Expectation Revision
                (Requirement 27.2). Must resolve to an existing
                ``Deliverable_Expectation_Revisions`` row whose
                ``target_project_id`` matches the Project Identity
                reached from the source Work Assignment's Plan
                Revision per Requirement 27.3.
            production_rationale: Optional production rationale of
                0..4000 characters, or ``None`` when omitted
                (Requirement 27.2). The schema column is NULLable;
                empty strings are accepted and persisted verbatim.
            recording_party_id: Identity of the recording Contributor
                Party — the named assignee on the source Work
                Assignment per AD-WS-29.
            authority_basis: Authority basis recorded on the
                Production Record. Accepted as either
                :class:`AuthorityBasisRef` or a mapping convertible to
                one; the ``type`` must be drawn from
                ``{role-grant-id, scope-id, delegation-chain-id}``
                per AD-WS-10 / Requirement 27.2.
            applicable_scope: Scope identifier the Production applies
                within. Passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate`.
            engine: Required for the deny path's separate-transaction
                Denial Record write so the row survives the caller's
                rollback.
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
            :class:`CreateDeliverableProductionResult` carrying the
            persisted Production Identity, the resolved Deliverable
            Resource and Expectation Resource Identities, every
            persisted attribute, the three Relationship Identities,
            the recorded time, and the correlation identifier.

        Raises:
            DeliverableProductionValidationError: A required attribute
                is missing, a Requirement 27.2 range was violated, the
                authority basis is malformed, or the request body
                carried a prohibited planning-attribute or
                observed-outcome key.
            DeliverableProductionWorkAssignmentNotResolvableError: The
                source Work Assignment Identity did not resolve
                (Requirement 27.4).
            DeliverableProductionDeliverableRevisionNotResolvableError:
                The produced Deliverable Revision Identity did not
                resolve (Requirement 27.4).
            DeliverableProductionOriginatingBindingError: The produced
                Revision's ``originating_work_assignment_id`` does not
                match ``source_work_assignment_id`` (Requirement 27.4).
            DeliverableProductionDeliverableExpectationNotResolvableError:
                The target Deliverable Expectation Revision Identity
                did not resolve (Requirement 27.4).
            DeliverableProductionProjectMismatchError: The target
                Deliverable Expectation Revision's Project Identity
                does not match the Project Identity reached from the
                source Work Assignment's Plan Revision
                (Requirement 27.3).
            DeliverableProductionAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 27.5). The Denial Record was appended
                successfully in a separate transaction.
            DeliverableProductionAssigneeBindingError: Authorization
                permitted the attempt but the AD-WS-29 second stage
                rejected it because the recording Party is not the
                named assignee on the source Work Assignment.
                Subclass of
                :class:`DeliverableProductionAuthorizationError`.
            DeliverableProductionAuditFailureError: Authorization
                denied the attempt *and* the separate-transaction
                Denial Record append failed on every retry. Replaces
                :class:`DeliverableProductionAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed; the surrounding transaction MUST
                be allowed to roll back per Requirement 27.7.
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
                raise DeliverableProductionValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 27.2 / 27.4) before any
        # database read or authorization side-effect.
        self._validate_required_strings(
            source_work_assignment_id=source_work_assignment_id,
            produced_deliverable_revision_id=produced_deliverable_revision_id,
            target_deliverable_expectation_revision_id=(
                target_deliverable_expectation_revision_id
            ),
            recording_party_id=recording_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_production_rationale(production_rationale)
        normalized_basis = self._validate_authority_basis(authority_basis)

        # 3. Resolve the source Work Assignment via a single indexed
        # SELECT on ``Work_Assignment_Records``. The lookup runs on
        # the caller's connection so it participates in the caller's
        # transactional view. Requirement 27.4 rejects the
        # unresolvable case before authorization evaluates the
        # request so the deny path cannot reveal whether the Work
        # Assignment exists. The same row's ``assignee_party_id``
        # and ``target_plan_revision_id`` are captured here so the
        # AD-WS-29 second stage in step 9 and the Requirement 27.3
        # project-membership check in step 7 do not require
        # additional SELECTs.
        wa_row = connection.execute(
            text(
                "SELECT work_assignment_id, assignee_party_id, "
                "target_plan_revision_id, applicable_scope "
                "FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :work_assignment_id"
            ),
            {"work_assignment_id": source_work_assignment_id},
        ).mappings().first()
        if wa_row is None:
            raise DeliverableProductionWorkAssignmentNotResolvableError(
                source_work_assignment_id=source_work_assignment_id,
            )

        # 4. Resolve the produced Deliverable Revision via the
        # Deliverable_Repository public read API. The read runs on
        # the caller's connection so it participates in the caller's
        # transactional view. Requirement 27.4 rejects the
        # unresolvable case before authorization evaluates the
        # request.
        produced_revision: Optional[DeliverableRevisionRow] = (
            self.deliverable_reader.get_revision(
                connection, produced_deliverable_revision_id
            )
        )
        if produced_revision is None:
            raise DeliverableProductionDeliverableRevisionNotResolvableError(
                produced_deliverable_revision_id=(
                    produced_deliverable_revision_id
                ),
            )

        # 5. Originating-binding check per Requirement 27.4: the
        # produced Revision's ``originating_work_assignment_id`` (the
        # Work Assignment under whose authority it was authored) must
        # equal the supplied ``source_work_assignment_id``. The
        # check rejects forged-production attempts where a
        # Contributor names a peer's Revision as their own
        # production. The rejection runs before authorization
        # evaluation so the deny path cannot reveal the produced
        # Revision's authoring chain.
        if (
            produced_revision.originating_work_assignment_id
            != source_work_assignment_id
        ):
            raise DeliverableProductionOriginatingBindingError(
                produced_deliverable_revision_id=(
                    produced_deliverable_revision_id
                ),
                source_work_assignment_id=source_work_assignment_id,
                actual_originating_work_assignment_id=(
                    produced_revision.originating_work_assignment_id
                ),
            )

        # 6. Resolve the target Deliverable Expectation Revision via
        # the Planning_Service AD-WS-30 read API. Requirement 27.4
        # rejects the unresolvable case before authorization
        # evaluates the request.
        try:
            expectation_revision: DeliverableExpectationRevisionRow = (
                self.planning_reader.get_revision(
                    connection,
                    deliverable_expectation_revision_id=(
                        target_deliverable_expectation_revision_id
                    ),
                )
            )
        except DeliverableExpectationRevisionNotResolvableError as exc:
            raise DeliverableProductionDeliverableExpectationNotResolvableError(
                target_deliverable_expectation_revision_id=(
                    target_deliverable_expectation_revision_id
                ),
            ) from exc

        # 7. Project-membership check per Requirement 27.3. Walk the
        # source Work Assignment's Plan Revision → Activity Plan →
        # Project via the Slice 2 :class:`ProjectResolver` and
        # compare against the Expectation Revision's persisted
        # ``target_project_id``. The walk runs on the caller's
        # connection so the read is transactional with everything
        # else. A mismatch returns
        # ``deliverable_expectation_project_mismatch`` with no row
        # persisted. The rejection runs before authorization
        # evaluation so the deny path cannot reveal the Project
        # linkage of either the Work Assignment or the Expectation.
        #
        # An unresolvable Plan Revision propagates as a generic
        # not-resolvable for the source Work Assignment: the Plan
        # Revision was originally addressed by the Work Assignment,
        # so a missing row here means the Work Assignment row exists
        # but its Plan Revision linkage has been corrupted. Treating
        # the case as a Work-Assignment-not-resolvable error keeps
        # the response surface narrow and yields one well-defined
        # failure mode regardless of how the integrity invariant
        # could be bypassed.
        try:
            wa_project_id = self.project_resolver.resolve_project(
                connection,
                plan_revision_id=wa_row["target_plan_revision_id"],
            )
        except PlanRevisionNotResolvableError as exc:
            raise DeliverableProductionWorkAssignmentNotResolvableError(
                source_work_assignment_id=source_work_assignment_id,
            ) from exc

        if wa_project_id != expectation_revision.target_project_id:
            raise DeliverableProductionProjectMismatchError(
                source_work_assignment_id=source_work_assignment_id,
                target_deliverable_expectation_revision_id=(
                    target_deliverable_expectation_revision_id
                ),
                work_assignment_project_id=wa_project_id,
                deliverable_expectation_project_id=(
                    expectation_revision.target_project_id
                ),
            )

        # 8. Capture one recorded time for the entire write so the
        # Deliverable Production row, the three Relationship rows,
        # and the consequential audit row share a single timestamp
        # (design §"Cross-Cutting Concerns" — Transactionality).
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 9. Run the authorization evaluation on a SEPARATE
        # transaction. The authorization target is the source Work
        # Assignment Record — ``create.deliverable_production``
        # authority is scoped against the Work Assignment the
        # Production was authored under (AD-WS-29). The scope is
        # the persisted ``applicable_scope`` of the Work Assignment
        # so a forged request that names a different scope from the
        # Work Assignment cannot inflate its effective authority.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=recording_party_id,
                action=_ACTION_CREATE_DELIVERABLE_PRODUCTION,
                target=TargetRef(
                    kind=_KIND_WORK_ASSIGNMENT_RECORD,
                    id=source_work_assignment_id,
                    revision_id=None,
                    scope=wa_row["applicable_scope"],
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = (
                decision_outcome.reason_code
                or _REASON_CODE_NO_ROLE_ASSIGNMENT
            )
            self._persist_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                source_work_assignment_id=source_work_assignment_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise DeliverableProductionAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 10. AD-WS-29 second stage: the persisted Work Assignment
        # Record row's ``assignee_party_id`` must equal
        # ``recording_party_id``. The check uses the row already
        # loaded in step 3, which was read against the caller's
        # connection, so the bound is forge-proof. On mismatch,
        # append a Denial Record in a separate transaction (so the
        # row survives the caller-side rollback) and raise
        # :class:`DeliverableProductionAssigneeBindingError`;
        # raising the exception causes the caller's surrounding
        # ``engine.begin()`` context manager to roll back without
        # persisting any row.
        actual_assignee = wa_row["assignee_party_id"]
        if actual_assignee != recording_party_id:
            self._persist_denial(
                engine=engine,
                actor_party_id=recording_party_id,
                source_work_assignment_id=source_work_assignment_id,
                reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
                correlation_id=correlation,
                recorded_time=evaluate_at,
            )
            raise DeliverableProductionAssigneeBindingError(
                source_work_assignment_id=source_work_assignment_id,
                recording_party_id=recording_party_id,
                actual_assignee_party_id=actual_assignee,
                correlation_id=correlation,
            )

        # 11. Mint identifiers (AD-WS-2 / AD-WS-28). Deliverable
        # Production Records are Immutable Records (per
        # ``02-domain-model.md`` §8.2) so the Record identifier is
        # minted via :meth:`IdentityService.new_immutable_record_id`.
        # The three Relationship Identities are minted via
        # :meth:`IdentityService.new_relationship_id`.
        deliverable_production_id = str(
            self.identity_service.new_immutable_record_id()
        )
        produces_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        relates_to_relationship_id = str(
            self.identity_service.new_relationship_id()
        )

        # ``content_digest`` is bound to the Production identifier in
        # ``Identifier_Registry``; the digest is the SHA-256 of the
        # canonical JSON payload of the Record so two different
        # Production Records never collide on the same digest.
        # ``authority_basis.id`` is normalized to its string form for
        # the canonical payload because UUID objects are not natively
        # JSON-serializable.
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "source_work_assignment_id": source_work_assignment_id,
                    "produced_deliverable_id": produced_revision.deliverable_id,
                    "produced_deliverable_revision_id": (
                        produced_deliverable_revision_id
                    ),
                    "target_deliverable_expectation_id": (
                        expectation_revision.deliverable_expectation_id
                    ),
                    "target_deliverable_expectation_revision_id": (
                        target_deliverable_expectation_revision_id
                    ),
                    "production_rationale": production_rationale,
                    "recording_party_id": recording_party_id,
                    "authority_basis_type": normalized_basis.type,
                    "authority_basis_id": str(normalized_basis.id),
                    "applicable_scope": applicable_scope,
                    "recorded_at": recorded_at,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 12. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-28
        # ``resource_kind='deliverable_production_record'`` tag.
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_IMMUTABLE_RECORD,
            _RESOURCE_KIND_DELIVERABLE_PRODUCTION,
            deliverable_production_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=recording_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_DELIVERABLE_PRODUCTION,
            recorded_time=recorded_time,
        )

        # 13. Insert the Deliverable Production Record carrying every
        # Requirement 27.2 attribute. The produced Deliverable
        # Resource Identity and target Deliverable Expectation
        # Resource Identity are taken from the rows resolved above so
        # the persisted columns are byte-equivalent to the
        # authoritative Resource Identities (Requirement 27.2 — both
        # Resource and Revision Identities are recorded).
        connection.execute(
            text(
                """
                INSERT INTO Deliverable_Production_Records (
                    deliverable_production_id, source_work_assignment_id,
                    produced_deliverable_id, produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    production_rationale, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :deliverable_production_id, :source_work_assignment_id,
                    :produced_deliverable_id, :produced_deliverable_revision_id,
                    :target_deliverable_expectation_id,
                    :target_deliverable_expectation_revision_id,
                    :production_rationale, :recording_party_id,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "deliverable_production_id": deliverable_production_id,
                "source_work_assignment_id": source_work_assignment_id,
                "produced_deliverable_id": produced_revision.deliverable_id,
                "produced_deliverable_revision_id": (
                    produced_deliverable_revision_id
                ),
                "target_deliverable_expectation_id": (
                    expectation_revision.deliverable_expectation_id
                ),
                "target_deliverable_expectation_revision_id": (
                    target_deliverable_expectation_revision_id
                ),
                "production_rationale": production_rationale,
                "recording_party_id": recording_party_id,
                "authority_basis_type": normalized_basis.type,
                "authority_basis_id": str(normalized_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 14. Insert the ``Produces`` Relationship binding the
        # Production Record to the produced Deliverable Revision per
        # AD-WS-26. ``semantic_role`` is NULL — the ``Produces``
        # relationship type carries no role discriminator per the
        # AD-WS-26 table.
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
                "relationship_id": produces_relationship_id,
                "relationship_type": _RELATIONSHIP_TYPE_PRODUCES,
                "source_kind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
                "source_id": deliverable_production_id,
                "source_revision_id": None,
                "target_kind": _KIND_DELIVERABLE_REVISION,
                "target_id": produced_revision.deliverable_id,
                "target_revision_id": produced_deliverable_revision_id,
                "authoring_party_id": recording_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 15. Insert the ``Addresses`` Relationship binding the
        # Production Record to the target Deliverable Expectation
        # Revision per AD-WS-26. ``semantic_role`` is NULL — the
        # ``Addresses`` relationship type carries no role
        # discriminator per the AD-WS-26 table.
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
                "source_kind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
                "source_id": deliverable_production_id,
                "source_revision_id": None,
                "target_kind": _KIND_DELIVERABLE_EXPECTATION_REVISION,
                "target_id": expectation_revision.deliverable_expectation_id,
                "target_revision_id": (
                    target_deliverable_expectation_revision_id
                ),
                "authoring_party_id": recording_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 16. Insert the ``Relates To`` Relationship binding the
        # Production Record to the source Work Assignment Record with
        # ``semantic_role='production_source'`` per AD-WS-26. The
        # ``semantic_role`` discriminator is the value the
        # Provenance_Navigator backlink algorithm looks for to return
        # the source Work Assignment when given a Production identity;
        # it must match the AD-WS-26 table exactly.
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
                "source_kind": _KIND_DELIVERABLE_PRODUCTION_RECORD,
                "source_id": deliverable_production_id,
                "source_revision_id": None,
                "target_kind": _KIND_WORK_ASSIGNMENT_RECORD,
                "target_id": source_work_assignment_id,
                "target_revision_id": None,
                "authoring_party_id": recording_party_id,
                "recorded_at": recorded_at,
                "semantic_role": _SEMANTIC_ROLE_PRODUCTION_SOURCE,
            },
        )

        # 17. Append the consequential audit row (Requirement 27.6 /
        # Slice 1 AD-WS-5). Participates in the caller's transaction
        # so a failure here rolls back the registry, the
        # ``Deliverable_Production_Records`` row, and the three
        # ``Relationships`` rows together. ``target_id`` is the
        # Production Record Identity; ``target_revision_id`` is
        # ``None`` because Deliverable Production Records are
        # Record-scoped (Requirement 22.2 — no separate Revision).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=recording_party_id,
            action_type=_ACTION_CREATE_DELIVERABLE_PRODUCTION,
            target_id=deliverable_production_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateDeliverableProductionResult(
            deliverable_production_id=deliverable_production_id,
            source_work_assignment_id=source_work_assignment_id,
            produced_deliverable_id=produced_revision.deliverable_id,
            produced_deliverable_revision_id=produced_deliverable_revision_id,
            target_deliverable_expectation_id=(
                expectation_revision.deliverable_expectation_id
            ),
            target_deliverable_expectation_revision_id=(
                target_deliverable_expectation_revision_id
            ),
            production_rationale=production_rationale,
            recording_party_id=recording_party_id,
            authority_basis=normalized_basis,
            applicable_scope=applicable_scope,
            produces_relationship_id=produces_relationship_id,
            addresses_relationship_id=addresses_relationship_id,
            relates_to_relationship_id=relates_to_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )


    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_required_strings(
        *,
        source_work_assignment_id: Any,
        produced_deliverable_revision_id: Any,
        target_deliverable_expectation_revision_id: Any,
        recording_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 27.4 / 27.7: a Deliverable Production creation
        request that omits the source Work Assignment Identity, the
        produced Deliverable Revision Identity, the target Deliverable
        Expectation Revision Identity, the recording Contributor Party
        Identity, or the applicable scope is rejected with no
        Production Record created. Each missing attribute surfaces a
        distinct ``failed_constraint`` so the HTTP layer can identify
        the precise field to the client.
        """
        if not source_work_assignment_id or not isinstance(
            source_work_assignment_id, str
        ):
            raise DeliverableProductionValidationError(
                "source_work_assignment_id is required; Requirement 27.4 "
                "rejects Productions missing the source Work Assignment "
                "Identity.",
                failed_constraint="source_work_assignment_id_missing",
            )
        if not produced_deliverable_revision_id or not isinstance(
            produced_deliverable_revision_id, str
        ):
            raise DeliverableProductionValidationError(
                "produced_deliverable_revision_id is required; "
                "Requirement 27.4 rejects Productions missing the produced "
                "Deliverable Revision Identity.",
                failed_constraint="produced_deliverable_revision_id_missing",
            )
        if not target_deliverable_expectation_revision_id or not isinstance(
            target_deliverable_expectation_revision_id, str
        ):
            raise DeliverableProductionValidationError(
                "target_deliverable_expectation_revision_id is required; "
                "Requirement 27.4 rejects Productions missing the target "
                "Deliverable Expectation Revision Identity.",
                failed_constraint=(
                    "target_deliverable_expectation_revision_id_missing"
                ),
            )
        if not recording_party_id or not isinstance(recording_party_id, str):
            raise DeliverableProductionValidationError(
                "recording_party_id is required; Requirement 27.5 rejects "
                "unauthenticated Production creation.",
                failed_constraint="recording_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise DeliverableProductionValidationError(
                "applicable_scope is required; Requirement 27.4 rejects "
                "Productions missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_production_rationale(production_rationale: Any) -> None:
        """Reject production rationale outside the Requirement 27.2 range.

        Per Requirement 27.2 the production rationale is 0..4000
        characters and optional. ``None`` is accepted (the column is
        NULLable) and persisted as SQL ``NULL``; the empty string is
        also accepted (length 0 satisfies the 0 lower bound) and
        persisted verbatim. The schema-level CHECK constraint
        ``length(production_rationale) BETWEEN 0 AND 4000`` enforces
        the same range at the database layer.
        """
        if production_rationale is None:
            return
        if not isinstance(production_rationale, str):
            raise DeliverableProductionValidationError(
                "production_rationale must be a str or None; received "
                f"{type(production_rationale).__name__}.",
                failed_constraint="production_rationale_invalid_type",
            )
        if len(production_rationale) > _PRODUCTION_RATIONALE_MAX_CHARS:
            raise DeliverableProductionValidationError(
                f"production_rationale length {len(production_rationale)} "
                f"exceeds the {_PRODUCTION_RATIONALE_MAX_CHARS}-character "
                "limit imposed by Requirement 27.2.",
                failed_constraint="production_rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(authority_basis: Any) -> AuthorityBasisRef:
        """Validate the authority basis and return a normalized
        :class:`AuthorityBasisRef`.

        Per Requirement 27.2 / AD-WS-10: the authority basis ``type``
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
            raise DeliverableProductionValidationError(
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
            raise DeliverableProductionValidationError(
                "authority_basis.type is required and must be one of "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)} per AD-WS-10.",
                failed_constraint="authority_basis_type_missing",
            )
        if basis_type not in _VALID_AUTHORITY_BASIS_TYPES:
            raise DeliverableProductionValidationError(
                f"authority_basis.type {basis_type!r} is not in the "
                f"AD-WS-10 enumeration "
                f"{sorted(_VALID_AUTHORITY_BASIS_TYPES)}.",
                failed_constraint="authority_basis_type_out_of_set",
            )
        if basis_id is None or (isinstance(basis_id, str) and basis_id == ""):
            raise DeliverableProductionValidationError(
                "authority_basis.id is required per Requirement 27.2.",
                failed_constraint="authority_basis_id_missing",
            )

        try:
            return AuthorityBasisRef(type=basis_type, id=basis_id)
        except Exception as exc:  # pragma: no cover - Pydantic re-raises
            raise DeliverableProductionValidationError(
                f"authority_basis failed schema validation: {exc}",
                failed_constraint="authority_basis_id_missing",
            ) from exc

    # -- denial side-channel ----------------------------------------------

    def _persist_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        source_work_assignment_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Production attempt.

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
          :class:`DeliverableProductionAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_deliverable_production` raises an authorization
        or assignee-binding error. The Denial Record must therefore
        live outside that scope to survive (AD-WS-9 / Requirement
        30.6).

        ``target_id`` on the Denial Record points at the source Work
        Assignment Identity because the Production Identity has not
        yet been minted at the time the denial is recorded — the deny
        path explicitly refuses to mint an Immutable Record Identity
        for an unauthorized attempt (Requirement 27.5 / Requirement
        30.5 — no information leakage about the existence of
        restricted Records).

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
                        attempted_action=_ACTION_CREATE_DELIVERABLE_PRODUCTION,
                        target_id=source_work_assignment_id,
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

        raise DeliverableProductionAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this module
# does not import private names from sibling services. The functions
# are intentionally identical to their sibling implementations:
# correlation identifiers are non-domain values and the digest helper
# is opaque to :class:`Identifier_Registry`.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Deliverable
    Production creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Production
    Identity in ``Identifier_Registry``. Deliverable Production
    Records are Record-scoped (Requirement 22.2 — no separate
    Revision) so this digest is bound exactly once per Production
    creation.
    """
    return hashlib.sha256(content).hexdigest()
