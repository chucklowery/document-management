"""Planning_Service.DeliverableExpectations — Deliverable Expectation
Resources, Revisions, and the ``Addresses`` Relationship to the parent
Project.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.DeliverableExpectations" — public dataclass
  surface, the request-model contract (Pydantic ``Config(extra='forbid')``
  plus an explicit ``_validate_no_produced_attributes`` validator
  rejecting any produced-deliverable attribute key per Requirement
  5.3 / 13.2), the authority string (``create.deliverable_expectation``
  → ``modify`` per AD-WS-15), and the ``deliverable_kind`` enumeration
  ``{Document, Artifact, Service, Other}`` enforced by both the Python
  request model and the ``Deliverable_Expectation_Revisions.deliverable_kind``
  schema CHECK.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction), Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and registered
  in ``Identifier_Registry`` with the additive ``resource_kind`` tag per
  AD-WS-19), Authorization (the action string
  ``create.deliverable_expectation`` maps to the ``modify`` authority
  per AD-WS-15; the deny path reuses the Slice 1 separate-transaction
  Denial-Record pattern from
  :class:`walking_slice.planning.intended_outcomes.IntendedOutcomeService`
  and :class:`walking_slice.planning.projects.ProjectService`).
- AD-WS-15 — additive ``modify`` mapping for
  ``create.deliverable_expectation``.

Task scope (task 6.1)
=====================

This module implements
:meth:`DeliverableExpectationService.create_deliverable_expectation`:

1. Validate request inputs through a Pydantic
   :class:`DeliverableExpectationCreationRequest` model with
   ``Config(extra='forbid')`` plus an explicit model validator that
   rejects any top-level key matching a produced-deliverable prefix
   (Requirement 5.3 / Requirement 13.2 / 13.5).
2. Validate ``deliverable_kind`` against the enumerated set
   ``{Document, Artifact, Service, Other}`` (Requirement 5.2 / 5.4).
   Any other value is rejected at the Pydantic layer with a structured
   ``deliverable_kind_invalid`` discriminator; the schema CHECK on
   ``Deliverable_Expectation_Revisions.deliverable_kind`` enforces the
   same enumeration as a defense in depth.
3. Resolve the target Project via a single ``SELECT`` against
   ``Projects`` by Identity (Requirement 5.4) — rejected when the
   identifier is unresolvable.
4. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (Slice 1 single-writer accommodation); on a deny outcome, persist a
   Denial Record in another separate transaction with the Requirement
   7.6 three-attempt exponential-backoff retry pattern, and raise
   :class:`DeliverableExpectationAuthorizationError` carrying the
   AD-WS-9 denial response fields (``reason_code``, ``correlation_id``).
5. On a permit outcome, mint the Deliverable Expectation Resource and
   first Revision Identities, register both in ``Identifier_Registry``
   with their additive ``resource_kind`` tags ``'deliverable_expectation'``
   and ``'deliverable_expectation_revision'`` (AD-WS-19), INSERT the
   ``Deliverable_Expectations`` and ``Deliverable_Expectation_Revisions``
   rows, INSERT the single ``Addresses`` Relationship from the
   Deliverable Expectation Revision to the target Project Resource
   (Requirement 5.2), and append the consequential ``Audit_Records`` row
   (Requirement 5.6 / AD-WS-5) — all inside the caller's transaction so
   a failure anywhere rolls every row back.

Requirements satisfied
======================

    5.1   — authorized Deliverable Expectation creation produces one
            Resource and one initial immutable Revision.
    5.2   — every Deliverable Expectation Revision records name
            (1..200), description (0..10000 or NULL), deliverable_kind
            (enumerated), acceptance criteria (0..10000 or NULL),
            authoring Party Identity, applicable scope, recorded time
            (UTC ms-precision), and the ``Addresses`` Relationship to
            the target Project.
    5.3   — Deliverable Expectation creation requests carrying any
            produced-Deliverable / hand-off / accepted-by attribute
            are rejected; no Resource or Revision is created.
    5.4   — Deliverable Expectation creation requests naming an
            unresolvable target Project, omitting the name, supplying
            a deliverable_kind outside the enumerated set, or omitting
            the applicable scope are rejected.
    5.5   — unauthorized requests are denied via
            :class:`AuthorizationService`; the Planning_Service
            declines to create any Resource or Revision and the
            Audit_Log appends a Denial Record conforming to AD-WS-9.
    5.6   — every successful Deliverable Expectation Revision insertion
            appends one immutable consequential audit row in the same
            transaction.
   13.2   — produced-deliverable attribute keys are rejected at the
            request boundary.
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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.planning._helpers import (
    PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


__all__ = [
    "CreateDeliverableExpectationResult",
    "DELIVERABLE_KIND_VALUES",
    "DeliverableExpectationAuditFailureError",
    "DeliverableExpectationAuthorizationError",
    "DeliverableExpectationCreationRequest",
    "DeliverableExpectationProjectNotResolvableError",
    "DeliverableExpectationRevisionNotResolvableError",
    "DeliverableExpectationRevisionRow",
    "DeliverableExpectationService",
    "DeliverableExpectationValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship type strings, registry kinds, and validation
# limits are pulled out as module-level Final so the names downstream tests
# look for in ``Audit_Records.action_type`` and ``Relationships`` are
# textually stable and stay aligned with the Slice 2 schema in
# :mod:`walking_slice.planning._persistence` and the AD-WS-15 authority
# mapping in :mod:`walking_slice.authorization`.
# ---------------------------------------------------------------------------


# ``create.deliverable_expectation`` maps to the ``modify`` authority
# per AD-WS-15. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 5.6) and on the
# separate-transaction Denial Record so audit consumers can correlate
# denial rows with the action a Party was attempting.
_ACTION_CREATE_DELIVERABLE_EXPECTATION: Final[str] = "create.deliverable_expectation"

# Relationship Type and source/target ``kind`` strings written to
# ``Relationships`` rows. Constants ensure the strings cannot drift
# between this module, sibling Planning_Service modules, and the
# Slice 1 backlink algorithm that consumes ``Relationships`` rows
# verbatim.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_DELIVERABLE_EXPECTATION_REVISION: Final[str] = "deliverable_expectation_revision"
_KIND_PROJECT: Final[str] = "project"

# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tags (Slice 2 additive enumeration
# per AD-WS-19).
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_DELIVERABLE_EXPECTATION: Final[str] = "deliverable_expectation"
_RESOURCE_KIND_DELIVERABLE_EXPECTATION_REVISION: Final[str] = (
    "deliverable_expectation_revision"
)

# Deliverable kind enumeration per Requirement 5.2. The
# ``Deliverable_Expectation_Revisions.deliverable_kind`` schema CHECK
# enforces the same membership as a defense in depth. The tuple
# preserves the declared order for use in error messages.
DELIVERABLE_KIND_VALUES: Final[tuple[str, ...]] = (
    "Document",
    "Artifact",
    "Service",
    "Other",
)

# Validation limits per Requirement 5.2 ("expected Deliverable name of
# 1 to 200 characters", "expected Deliverable description of 0 to
# 10,000 characters", "acceptance criteria text of 0 to 10,000
# characters"). The schema CHECK constraints on
# ``Deliverable_Expectation_Revisions`` enforce the same values;
# centralizing them here surfaces precise, structured constraint names
# through :class:`DeliverableExpectationValidationError`.
_NAME_MIN_CHARS: Final[int] = 1
_NAME_MAX_CHARS: Final[int] = 200
_DESCRIPTION_MIN_CHARS: Final[int] = 0
_DESCRIPTION_MAX_CHARS: Final[int] = 10_000
_ACCEPTANCE_CRITERIA_MIN_CHARS: Final[int] = 0
_ACCEPTANCE_CRITERIA_MAX_CHARS: Final[int] = 10_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 7.6). Three retries after the
# initial attempt for a total of four attempts. The sequence is
# byte-equivalent to the one in
# :class:`walking_slice.planning.objectives.ObjectiveService` and
# :class:`walking_slice.planning.intended_outcomes.IntendedOutcomeService`
# so every Planning_Service module presents identical denial-side
# timing (Property 18 — Indistinguishable denial — relies on this).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# Single indexed SELECT used by
# :meth:`DeliverableExpectationService.get_revision` (Slice 3 task 2.2).
# Reads only the columns Slice 3 callers need for Requirement 27.3's
# project-membership check (``target_project_id``), the Resource /
# Revision Identity echo (``deliverable_expectation_id``,
# ``deliverable_expectation_revision_id``), and a human-readable name
# plus kind for context. The query is keyed on the primary key of
# ``Deliverable_Expectation_Revisions``
# (``deliverable_expectation_revision_id``), so SQLite always uses the
# primary-key index — no separate index is required. The read is
# read-only (AD-WS-30, Requirement 40.1) and runs inside the caller's
# transaction so the result respects the caller's isolation view.
_GET_REVISION_SQL: Final[str] = """
    SELECT der.deliverable_expectation_revision_id  AS deliverable_expectation_revision_id,
           der.deliverable_expectation_id           AS deliverable_expectation_id,
           der.target_project_id                    AS target_project_id,
           der.name                                 AS name,
           der.deliverable_kind                     AS deliverable_kind,
           der.recorded_at                          AS recorded_at
      FROM Deliverable_Expectation_Revisions AS der
     WHERE der.deliverable_expectation_revision_id = :deliverable_expectation_revision_id
"""


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class DeliverableExpectationValidationError(ValueError):
    """Raised when a Deliverable Expectation submission fails Requirement
    5.2 / 5.3 / 5.4 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"name_missing"`` (empty or non-string),
            ``"name_too_long"``,
            ``"description_too_long"``,
            ``"description_invalid_type"``,
            ``"deliverable_kind_invalid"`` (any value not in
                :data:`DELIVERABLE_KIND_VALUES`),
            ``"acceptance_criteria_too_long"``,
            ``"acceptance_criteria_invalid_type"``,
            ``"target_project_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"prohibited_attribute"`` (the request body carried at
                least one produced-deliverable attribute — see
                :attr:`prohibited_keys`).
        prohibited_keys: Populated only when ``failed_constraint`` is
            ``"prohibited_attribute"``; lists every offending top-level
            key in the order from the request body. Empty tuple in
            every other case.
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


class DeliverableExpectationProjectNotResolvableError(LookupError):
    """Raised when the target Project Identity does not resolve.

    Requirement 5.4 requires the named target Project Resource Identity
    to resolve to an existing Project Resource. The check runs before
    authorization evaluation so the deny path never reveals whether a
    Project exists for an unauthorized caller.

    Attributes:
        target_project_id: The Project Identity the caller supplied.
        failed_constraint: ``"target_project_not_resolvable"`` when no
            ``Projects`` row matched the identifier.
    """

    def __init__(
        self,
        *,
        target_project_id: str,
        failed_constraint: str = "target_project_not_resolvable",
    ) -> None:
        super().__init__(
            f"Target Project {target_project_id!r} did not resolve to an "
            f"existing Project Resource "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_project_id = target_project_id
        self.failed_constraint = failed_constraint


class DeliverableExpectationAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Deliverable
    Expectation attempt.

    Mirrors
    :class:`walking_slice.planning.projects.ProjectAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 5.5 / 10.x). The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 10 forbids leaking authorized Party identities,
    Project contents, role assignment details, or target existence
    beyond the requesting Party's view authority through the denial
    response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Deliverable Expectation creation denied: "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class DeliverableExpectationAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails (Requirement 7.6).

    Mirrors
    :class:`walking_slice.planning.projects.ProjectAuditFailureError`.
    On total audit-append failure the exception is raised *in place of*
    :class:`DeliverableExpectationAuthorizationError` — denial and
    audit have silently diverged and the operator must be told. The
    caller's transaction still rolls back so no Deliverable Expectation
    row, Revision row, Addresses Relationship, or consequential audit
    row is persisted.

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
            f"Denial Record append for a denied Deliverable Expectation "
            f"failed after {attempts} attempt(s): "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Request model (Pydantic).
#
# Design §"Planning_Service.DeliverableExpectations" explicitly requires
# the request model to combine ``Config(extra='forbid')`` (which
# structurally rejects any field not named below — task 6.1 bullet 2)
# with an explicit ``_validate_no_produced_attributes`` validator (which
# surfaces a precise rejection reason naming every offending
# produced-deliverable key per Requirement 13.5).
# ---------------------------------------------------------------------------


class DeliverableExpectationCreationRequest(BaseModel):
    """Validated request payload for
    :meth:`DeliverableExpectationService.create_deliverable_expectation`.

    The class enforces three layers of input discipline:

    1. **Field-level validation** — ``name`` (1..200), ``description``
       (0..10000 or None), ``deliverable_kind`` (literal enumeration),
       ``acceptance_criteria`` (0..10000 or None), and the three
       required string identifiers are constrained at parse time. The
       same ranges and enumeration membership are enforced again at
       the schema layer by ``Deliverable_Expectation_Revisions`` CHECK
       constraints (design §"Data Models — Schema Additions"), so a
       successful Pydantic validation guarantees a successful INSERT.
    2. **Structural forbid** — ``Config(extra='forbid')`` rejects any
       field not declared on the model. Together with the model's
       deliberate omission of any produced-deliverable field, this is
       the structural side of Requirement 5.3 / 13.2: even a
       produced-deliverable key the explicit validator below does not
       catch is still rejected by ``extra='forbid'``.
    3. **Explicit produced-deliverable screen** — the
       :meth:`_validate_no_produced_attributes` model validator runs
       *before* per-field validation and inspects the raw input dict
       for top-level keys matching any prefix in
       :data:`walking_slice.planning._helpers.PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES`
       (``produced-``, ``hand-off-``, ``accepted-by-``). Matching is
       case-insensitive and hyphen/underscore-invariant so both
       ``produced-deliverable-id`` and ``produced_deliverable_id`` are
       rejected. The validator surfaces every offending key on the
       raised
       :class:`walking_slice.planning._helpers.PlanningValidationError`
       so the response can name each per Requirement 13.5.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_project_id: str = Field(
        min_length=1,
        description="Identity of the target Project Resource (Requirement 5.4).",
    )
    name: str = Field(
        min_length=_NAME_MIN_CHARS,
        max_length=_NAME_MAX_CHARS,
        description=(
            "Expected Deliverable name of "
            f"{_NAME_MIN_CHARS}..{_NAME_MAX_CHARS} characters "
            "(Requirement 5.2)."
        ),
    )
    description: Optional[str] = Field(
        default=None,
        max_length=_DESCRIPTION_MAX_CHARS,
        description=(
            "Optional expected Deliverable description of "
            f"{_DESCRIPTION_MIN_CHARS}..{_DESCRIPTION_MAX_CHARS} characters "
            "(Requirement 5.2)."
        ),
    )
    deliverable_kind: Literal["Document", "Artifact", "Service", "Other"] = Field(
        description=(
            "Expected Deliverable kind drawn from the enumerated set "
            "{Document, Artifact, Service, Other} (Requirement 5.2)."
        ),
    )
    acceptance_criteria: Optional[str] = Field(
        default=None,
        max_length=_ACCEPTANCE_CRITERIA_MAX_CHARS,
        description=(
            "Optional acceptance criteria text of "
            f"{_ACCEPTANCE_CRITERIA_MIN_CHARS}.."
            f"{_ACCEPTANCE_CRITERIA_MAX_CHARS} characters (Requirement 5.2)."
        ),
    )
    authoring_party_id: str = Field(
        min_length=1,
        description="Identity of the authoring Party (Requirement 5.5).",
    )
    applicable_scope: str = Field(
        min_length=1,
        description="Scope identifier the Deliverable Expectation applies within (Requirement 5.2).",
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_no_produced_attributes(cls, data: Any) -> Any:
        """Reject request bodies that carry produced-deliverable attribute keys.

        Implements design §"Planning_Service.DeliverableExpectations" —
        "a ``_validate_no_produced_attributes`` validator (matching the
        IntendedOutcomes pattern) to reject any field naming a produced-
        Deliverable Identity, hand-off receipt, or acceptance-by-customer
        record (Requirement 5.3 / Requirement 13.2)". Delegates the
        per-prefix matching to
        :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
        which canonicalizes hyphen/underscore variants and is
        case-insensitive.

        Runs in ``mode='before'`` so the screen executes against the
        raw input dict — before unknown-field rejection by
        ``Config(extra='forbid')`` so the produced-deliverable
        rejection wins when both apply (giving the more specific error
        per Requirement 13.5). Non-dict inputs (e.g., already-validated
        :class:`DeliverableExpectationCreationRequest` instances passed
        through round-trip) are returned unchanged because they are
        already known to be safe.
        """
        if isinstance(data, Mapping):
            _reject_prohibited_attributes(
                data, PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES
            )
        return data


# ---------------------------------------------------------------------------
# Result value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateDeliverableExpectationResult:
    """Result of
    :meth:`DeliverableExpectationService.create_deliverable_expectation`.

    Returned so callers (the HTTP layer in task 15.1, tests, downstream
    Planning_Service modules that reference this Deliverable
    Expectation) can correlate the created Resource with its first
    Revision, its ``Addresses`` Relationship to the Project, and the
    consequential audit row in one round-trip.

    Attributes:
        deliverable_expectation_id: The Deliverable Expectation
            Resource Identity (UUIDv7).
        deliverable_expectation_revision_id: The first Deliverable
            Expectation Revision Identity.
        name: The persisted Deliverable name (1..200 chars).
        description: The persisted Deliverable description (or ``None``).
        deliverable_kind: The persisted Deliverable kind, drawn from
            :data:`DELIVERABLE_KIND_VALUES`.
        acceptance_criteria: The persisted acceptance criteria text
            (or ``None``).
        target_project_id: The Project Identity the Deliverable
            Expectation addresses; copied byte-equivalent from the
            request input.
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Deliverable Expectation
            applies within.
        addresses_relationship_id: Identity of the single ``Addresses``
            Relationship row inserted alongside the Revision
            (Requirement 5.2).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Deliverable_Expectations`` row, the
            ``Deliverable_Expectation_Revisions`` row, the ``Addresses``
            Relationship row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            two on a single value.
    """

    deliverable_expectation_id: str
    deliverable_expectation_revision_id: str
    name: str
    description: Optional[str]
    deliverable_kind: str
    acceptance_criteria: Optional[str]
    target_project_id: str
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


@dataclass(frozen=True)
class DeliverableExpectationRevisionRow:
    """Read-only snapshot of one ``Deliverable_Expectation_Revisions`` row
    returned by :meth:`DeliverableExpectationService.get_revision`.

    Slice 3 callers (notably
    :class:`walking_slice.execution.deliverable_productions.DeliverableProductionService`)
    use this value object to satisfy Requirement 27.3's project-membership
    check without reaching into the Slice 2 schema directly. The carried
    fields are the minimum a downstream caller needs to:

    1. Resolve the target Project Identity for the cross-Project
       comparison (`target_project_id`).
    2. Echo the Deliverable Expectation Resource and Revision Identities
       back to the caller of the downstream operation
       (`deliverable_expectation_id`, `deliverable_expectation_revision_id`).
    3. Display human-readable context where appropriate
       (`name`, `deliverable_kind`).
    4. Reproduce the recorded timestamp for downstream audit/correlation
       (`recorded_at`).

    The row is immutable (Slice 2 ``Deliverable_Expectation_Revisions``
    is insert-only per AD-WS-19), so a value-object snapshot is a faithful
    representation. The class is frozen to keep that contract explicit
    in Python.

    Attributes:
        deliverable_expectation_revision_id: The Revision Identity used
            as the primary key on the persisted row.
        deliverable_expectation_id: The owning Deliverable Expectation
            Resource Identity (a Revision belongs to exactly one
            Resource).
        target_project_id: The Project Identity this Deliverable
            Expectation Revision addresses (Requirement 5.2 / 5.4 /
            27.3). Slice 3 callers compare this value to the Project
            Identity reached from the source Work Assignment's Plan
            Revision via
            :class:`walking_slice.planning._project_resolver.ProjectResolver`.
        name: Persisted Deliverable name (1..200 chars, Requirement
            5.2).
        deliverable_kind: Persisted Deliverable kind, drawn from
            :data:`DELIVERABLE_KIND_VALUES`.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp of
            the original creation transaction (Requirement 5.2);
            byte-equivalent to the persisted ``recorded_at`` column.
    """

    deliverable_expectation_revision_id: str
    deliverable_expectation_id: str
    target_project_id: str
    name: str
    deliverable_kind: str
    recorded_at: str


class DeliverableExpectationRevisionNotResolvableError(LookupError):
    """Raised when ``deliverable_expectation_revision_id`` does not resolve.

    Surfaced by :meth:`DeliverableExpectationService.get_revision` when
    the supplied identifier matches no row in
    ``Deliverable_Expectation_Revisions``. Slice 3 callers map this
    exception to a structured 400 / 404 response or fold it into a
    denial response when AD-WS-9 indistinguishable denial applies.

    Carries only the offending identifier and a stable
    ``failed_constraint`` discriminator so route layers can branch on
    the cause without parsing message text.

    Attributes:
        deliverable_expectation_revision_id: The Revision Identity that
            did not resolve.
        failed_constraint: Stable discriminator. Always
            ``"deliverable_expectation_revision_not_resolvable"`` for
            this exception class.
    """

    def __init__(
        self,
        *,
        deliverable_expectation_revision_id: str,
        failed_constraint: str = (
            "deliverable_expectation_revision_not_resolvable"
        ),
    ) -> None:
        super().__init__(
            f"Deliverable Expectation Revision "
            f"{deliverable_expectation_revision_id!r} did not resolve to "
            f"an existing Deliverable Expectation Revision "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.deliverable_expectation_revision_id = (
            deliverable_expectation_revision_id
        )
        self.failed_constraint = failed_constraint


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliverableExpectationService:
    """Persist Deliverable Expectation Resources, Revisions, and their
    ``Addresses`` Relationships to parent Projects.

    Like the sibling Planning_Service services
    (:class:`walking_slice.planning.objectives.ObjectiveService`,
    :class:`walking_slice.planning.intended_outcomes.IntendedOutcomeService`,
    :class:`walking_slice.planning.projects.ProjectService`), this
    service is connection-scoped at call time:
    :meth:`create_deliverable_expectation` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the caller's
    transaction (AD-WS-5). The service instance therefore holds only
    the cross-request collaborators and can be shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.DeliverableExpectations" declares it
    ``@dataclass(frozen=True)``.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Deliverable_Expectations``, ``Deliverable_Expectation_Revisions``,
            ``Relationships``, and ``Audit_Records`` rows. The clock
            is consulted exactly once per write so every artifact of
            the transaction shares one timestamp.
        identity_service: Generates Deliverable Expectation Resource
            and Revision Identities and persists their
            ``Identifier_Registry`` bindings (the bindings carry the
            Slice 2 ``resource_kind`` tag per AD-WS-19).
        audit_log: Appends the consequential audit row (Requirement
            5.6) inside the caller's transaction.
        authorization_service: Evaluates
            ``create.deliverable_expectation`` authority per AD-WS-15 /
            Requirement 5.5; the deny path is the Slice 1
            separate-transaction Denial-Record pattern.
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
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_deliverable_expectation(
        self,
        connection: Connection,
        *,
        target_project_id: str,
        name: str,
        description: Optional[str],
        deliverable_kind: str,
        acceptance_criteria: Optional[str],
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateDeliverableExpectationResult:
        """Create a Deliverable Expectation Resource plus its first
        immutable Revision and ``Addresses`` Relationship to the target
        Project.

        Per Requirements 5.1 through 5.6, 13.2, AD-WS-9 (indistinguishable
        denial), AD-WS-15 (``create.deliverable_expectation`` →
        ``modify``), and AD-WS-19 (resource_kind tagged identifiers +
        append-only Slice 2 tables):

        1. When ``request_attributes`` is supplied, defensively screen
           the raw request body against the produced-deliverable prefix
           list (Property 22 / Requirements 13.2, 13.5). The typed
           kwargs themselves cannot carry a prohibited attribute, but
           the HTTP layer's raw body might.
        2. Build the :class:`DeliverableExpectationCreationRequest`
           Pydantic model from the typed kwargs. The model enforces
           every Requirement 5.2 range, the deliverable_kind
           enumeration, and runs the
           ``_validate_no_produced_attributes`` model validator against
           the kwargs dict — a redundant but cheap second check that
           hardens callers that bypass the route layer.
        3. Resolve the target Project via a single ``SELECT`` against
           ``Projects`` by Identity (Requirement 5.4). The check runs
           before authorization evaluation so the deny path never
           reveals whether a Project exists for an unauthorized
           caller.
        4. Run the authorization evaluation on a *separate*
           transaction (the Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`DeliverableExpectationAuditFailureError` in place
           of :class:`DeliverableExpectationAuthorizationError`.
        5. On ``permit``, mint the Deliverable Expectation Resource
           and first Revision Identities and register them in
           ``Identifier_Registry`` (kind ``'resource'`` and
           ``'revision'`` respectively, both carrying the Slice 2
           ``resource_kind`` tag per AD-WS-19).
        6. INSERT the ``Deliverable_Expectations`` Resource header,
           ``Deliverable_Expectation_Revisions`` Revision row carrying
           every Requirement 5.2 attribute (with ``deliverable_kind``
           validated against the enumerated set in Python and again by
           the schema CHECK), the single ``Addresses``
           ``Relationships`` row with ``semantic_role = NULL`` (AD-WS-17
           — the column is reserved for Plan Review's ``'review'``
           discriminator and is not used here), and the consequential
           ``Audit_Records`` row, all inside the caller's transaction.
           A FK or trigger failure anywhere rolls the whole
           transaction back.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_project_id: Identity of the target Project
                Resource (Requirement 5.4). Must resolve to an
                existing row in ``Projects``.
            name: Expected Deliverable name of 1..200 characters
                (Requirement 5.2).
            description: Expected Deliverable description of
                0..10000 characters, or ``None`` when no description
                is supplied. The empty string is a valid persisted
                value; ``None`` is persisted as SQL ``NULL``.
            deliverable_kind: Expected Deliverable kind, one of
                :data:`DELIVERABLE_KIND_VALUES`. Any other value is
                rejected with
                :class:`DeliverableExpectationValidationError` and
                ``failed_constraint = "deliverable_kind_invalid"``.
            acceptance_criteria: Acceptance criteria text of 0..10000
                characters, or ``None``. Same semantics as
                ``description``.
            authoring_party_id: Identity of the authoring Party.
                Persisted on
                ``Deliverable_Expectation_Revisions.authoring_party_id``
                and on the consequential audit row's
                ``actor_party_id``. The Slice 1 ``Parties`` foreign
                key is enforced by the database.
            applicable_scope: Scope identifier the Deliverable
                Expectation applies within. Persisted on
                ``Deliverable_Expectation_Revisions.applicable_scope``
                and passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            engine: Required for the deny path's separate-transaction
                Denial Record write so the row survives the caller's
                rollback (Requirement 7.6). The same engine is used
                to open a fresh transaction for the authorization
                evaluation itself.
            correlation_id: Optional correlation identifier shared by
                every audit row written in this operation. A UUIDv7
                is generated when omitted.
            evaluation_at: Optional explicit effective time passed to
                :meth:`AuthorizationService.evaluate` as the ``at``
                parameter. Defaults to the recorded time of this
                transaction so the evaluation row's recorded time
                aligns with the consequential write it authorized.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every produced-deliverable
                prefix (Property 22 / Requirement 13.2). The HTTP
                layer forwards the raw request body here; service-level
                callers (e.g., unit tests) may pass ``None`` to skip
                the screen since the typed kwargs themselves cannot
                carry a prohibited attribute.

        Returns:
            :class:`CreateDeliverableExpectationResult` carrying the
            persisted identifiers, attributes, the ``Addresses``
            Relationship Identity, the recorded time, and the
            correlation identifier.

        Raises:
            DeliverableExpectationValidationError: A required attribute
                is missing, a Requirement 5.2 range was violated, the
                ``deliverable_kind`` value is outside the enumerated
                set, or the request body carried a prohibited
                produced-deliverable attribute.
            DeliverableExpectationProjectNotResolvableError: The
                target Project Identity did not resolve to an
                existing Project Resource (Requirement 5.4).
            DeliverableExpectationAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 5.5). The Denial Record was appended
                successfully in a separate transaction (Requirement
                7.6).
            DeliverableExpectationAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`DeliverableExpectationAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 5.6 /
                Slice 1 Requirement 13.6.
        """
        # 1. Screen the original request body when the route layer has
        # forwarded it. The typed kwargs themselves cannot carry a
        # prohibited attribute (the signature does not declare any
        # such field), but the HTTP layer's raw body might —
        # Property 22 / Requirement 13.2 demands rejection at the API
        # boundary.
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES
                )
            except PlanningValidationError as exc:
                raise DeliverableExpectationValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate kwargs through the Pydantic request model. The
        # model's structural checks (extra='forbid', per-field length
        # and type constraints, deliverable_kind Literal enumeration)
        # and the ``_validate_no_produced_attributes`` model validator
        # are what task 6.1 mandates. Any ValidationError is converted
        # to a :class:`DeliverableExpectationValidationError` so the
        # public error surface of this service stays uniform with the
        # rest of the Planning_Service.
        try:
            request = DeliverableExpectationCreationRequest(
                target_project_id=target_project_id,
                name=name,
                description=description,
                deliverable_kind=deliverable_kind,
                acceptance_criteria=acceptance_criteria,
                authoring_party_id=authoring_party_id,
                applicable_scope=applicable_scope,
            )
        except ValidationError as exc:
            # Pydantic wraps any exception raised from a
            # ``mode='before'`` model validator (including
            # :class:`PlanningValidationError`) inside a
            # :class:`ValidationError`. The translator below unwraps
            # the ``ctx['error']`` slot and surfaces
            # :class:`DeliverableExpectationValidationError` with the
            # ``failed_constraint`` discriminator most appropriate to
            # the first reported error — including the
            # ``"prohibited_attribute"`` discriminator and the
            # original ``prohibited_keys`` tuple when the wrapped
            # cause is a :class:`PlanningValidationError`.
            raise self._translate_pydantic_error(exc) from exc

        # 3. Resolve the target Project via a single SELECT against
        # ``Projects`` by Identity (Requirement 5.4). The lookup runs
        # on the caller's connection so it participates in the
        # caller's transactional view.
        resolved = connection.execute(
            text(
                "SELECT project_id FROM Projects "
                "WHERE project_id = :project_id"
            ),
            {"project_id": request.target_project_id},
        ).scalar_one_or_none()
        if resolved is None:
            raise DeliverableExpectationProjectNotResolvableError(
                target_project_id=request.target_project_id
            )

        # 4. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Deliverable_Expectations row, the
        # Deliverable_Expectation_Revisions row, the Addresses
        # Relationship row, and the consequential audit row all share
        # this timestamp; the optional ``evaluation_at`` parameter
        # changes only *when* authority is evaluated *as of*, not the
        # recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 5. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 single-writer accommodation). The
        # ``TargetRef`` is the parent Project so the wired role
        # assignment must cover the same scope to permit the action.
        # On ``permit`` the evaluation row commits independently; on
        # ``deny`` the evaluation row rolls back with the evaluation
        # transaction and the durable record of the denial is the
        # Denial Record appended below.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=request.authoring_party_id,
                action=_ACTION_CREATE_DELIVERABLE_EXPECTATION,
                target=TargetRef(
                    kind=_KIND_PROJECT,
                    id=request.target_project_id,
                    revision_id=None,
                    scope=request.applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_deliverable_expectation_denial(
                engine=engine,
                actor_party_id=request.authoring_party_id,
                target_project_id=request.target_project_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise DeliverableExpectationAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. Mint identifiers (AD-WS-2 / AD-WS-19). The Deliverable
        # Expectation Resource and its first Revision are bound to the
        # same content digest because the Resource has no separate
        # "natural content" — the digest is derived from the first
        # Revision's payload (the same pattern Objectives / Projects /
        # IntendedOutcomes use).
        deliverable_expectation_id = str(self.identity_service.new_resource_id())
        deliverable_expectation_revision_id = str(
            self.identity_service.new_revision_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "name": request.name,
                    "description": request.description,
                    "deliverable_kind": request.deliverable_kind,
                    "acceptance_criteria": request.acceptance_criteria,
                    "target_project_id": request.target_project_id,
                    "authoring_party_id": request.authoring_party_id,
                    "applicable_scope": request.applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 7. Register both identifiers in ``Identifier_Registry``
        # carrying the AD-WS-19 ``resource_kind`` tag. The helper
        # delegates to :meth:`IdentityService.reject_if_duplicate` so
        # the Slice 1 identifier-conflict Denial Record pathway fires
        # on any collision; on success the helper INSERTs one row per
        # identifier inside the caller's transaction.
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_DELIVERABLE_EXPECTATION,
            deliverable_expectation_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=request.authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_DELIVERABLE_EXPECTATION,
            recorded_time=recorded_time,
        )
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_DELIVERABLE_EXPECTATION_REVISION,
            deliverable_expectation_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=request.authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_DELIVERABLE_EXPECTATION,
            recorded_time=recorded_time,
        )

        # 8. Insert the Resource header. ``created_at`` carries the
        # same recorded time as the first Revision (design
        # §"Persistence Invariants Summary") so the two rows'
        # timestamps are byte-equivalent.
        connection.execute(
            text(
                """
                INSERT INTO Deliverable_Expectations (
                    deliverable_expectation_id, created_at
                ) VALUES (
                    :deliverable_expectation_id, :created_at
                )
                """
            ),
            {
                "deliverable_expectation_id": deliverable_expectation_id,
                "created_at": recorded_at,
            },
        )

        # 9. Insert the first immutable Revision (Requirement 5.2).
        # ``parent_revision_id`` is NULL because this is the first
        # Revision; subsequent Revisions would link backwards via the
        # column (out of scope for Slice 2 — the create endpoint only
        # writes the first Revision).
        connection.execute(
            text(
                """
                INSERT INTO Deliverable_Expectation_Revisions (
                    deliverable_expectation_revision_id,
                    deliverable_expectation_id, parent_revision_id,
                    target_project_id, name, description,
                    deliverable_kind, acceptance_criteria,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :deliverable_expectation_revision_id,
                    :deliverable_expectation_id, NULL,
                    :target_project_id, :name, :description,
                    :deliverable_kind, :acceptance_criteria,
                    :authoring_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "deliverable_expectation_revision_id": deliverable_expectation_revision_id,
                "deliverable_expectation_id": deliverable_expectation_id,
                "target_project_id": request.target_project_id,
                "name": request.name,
                "description": request.description,
                "deliverable_kind": request.deliverable_kind,
                "acceptance_criteria": request.acceptance_criteria,
                "authoring_party_id": request.authoring_party_id,
                "applicable_scope": request.applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 10. Insert the single ``Addresses`` Relationship row
        # (Requirement 5.2). ``semantic_role`` is NULL — the AD-WS-17
        # additive column is reserved for Plan Review's ``'review'``
        # discriminator and is not used on Addresses rows.
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
                "source_kind": _KIND_DELIVERABLE_EXPECTATION_REVISION,
                "source_id": deliverable_expectation_id,
                "source_revision_id": deliverable_expectation_revision_id,
                "target_kind": _KIND_PROJECT,
                "target_id": request.target_project_id,
                "target_revision_id": None,
                "authoring_party_id": request.authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 11. Append the consequential audit row (Requirement 5.6 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Deliverable_Expectations,
        # Deliverable_Expectation_Revisions, and Relationships rows
        # together.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=request.authoring_party_id,
            action_type=_ACTION_CREATE_DELIVERABLE_EXPECTATION,
            target_id=deliverable_expectation_id,
            target_revision_id=deliverable_expectation_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateDeliverableExpectationResult(
            deliverable_expectation_id=deliverable_expectation_id,
            deliverable_expectation_revision_id=deliverable_expectation_revision_id,
            name=request.name,
            description=request.description,
            deliverable_kind=request.deliverable_kind,
            acceptance_criteria=request.acceptance_criteria,
            target_project_id=request.target_project_id,
            authoring_party_id=request.authoring_party_id,
            applicable_scope=request.applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    def get_revision(
        self,
        connection: Connection,
        *,
        deliverable_expectation_revision_id: str,
    ) -> DeliverableExpectationRevisionRow:
        """Return the persisted ``Deliverable_Expectation_Revisions`` row
        for ``deliverable_expectation_revision_id``.

        Implements the additive Slice 3 read API specified in
        ``.kiro/specs/third-walking-slice/design.md`` AD-WS-30:
        the Execution_Service resolves a target Deliverable Expectation
        Revision through this public Planning_Service read function
        rather than reaching into the Slice 2 schema directly. This
        keeps the Execution_Service decoupled from Slice 2's persistence
        per Principle 5.2 (Bounded contexts preserve meaning) and
        ``03-context-map.md`` Cross-Context Rule 2.

        Executes one indexed ``SELECT`` against the primary key of
        ``Deliverable_Expectation_Revisions``. The read is read-only
        (Requirement 40.1 — no Slice 1 or Slice 2 row is mutated by
        Slice 3 reads) and runs inside the caller's transaction so the
        result shares the caller's isolation view.

        The returned :class:`DeliverableExpectationRevisionRow` carries
        the target Project Identity Slice 3 callers compare against the
        Project Identity reached from the source Work Assignment's Plan
        Revision via
        :class:`walking_slice.planning._project_resolver.ProjectResolver`
        to satisfy Requirement 27.3's project-membership check.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. Used for one read-only ``SELECT``; the
                connection is not closed and no write is issued.
            deliverable_expectation_revision_id: Identity of the
                Deliverable Expectation Revision to read.

        Returns:
            A frozen :class:`DeliverableExpectationRevisionRow`
            snapshot of the persisted row.

        Raises:
            DeliverableExpectationRevisionNotResolvableError: When
                ``deliverable_expectation_revision_id`` does not match
                any row in ``Deliverable_Expectation_Revisions``. The
                exception carries the offending identifier and a stable
                ``failed_constraint`` discriminator
                (``"deliverable_expectation_revision_not_resolvable"``)
                so route layers can branch without parsing message
                text.
        """
        row = connection.execute(
            text(_GET_REVISION_SQL),
            {
                "deliverable_expectation_revision_id": (
                    deliverable_expectation_revision_id
                ),
            },
        ).mappings().first()

        if row is None:
            raise DeliverableExpectationRevisionNotResolvableError(
                deliverable_expectation_revision_id=(
                    deliverable_expectation_revision_id
                ),
            )

        return DeliverableExpectationRevisionRow(
            deliverable_expectation_revision_id=(
                row["deliverable_expectation_revision_id"]
            ),
            deliverable_expectation_id=row["deliverable_expectation_id"],
            target_project_id=row["target_project_id"],
            name=row["name"],
            deliverable_kind=row["deliverable_kind"],
            recorded_at=row["recorded_at"],
        )

    # -- Pydantic error translation ---------------------------------------

    @staticmethod
    def _translate_pydantic_error(
        exc: ValidationError,
    ) -> "DeliverableExpectationValidationError":
        """Convert a Pydantic :class:`ValidationError` to a structured
        :class:`DeliverableExpectationValidationError`.

        The HTTP layer (task 15.1) already maps Pydantic errors to
        HTTP 400 with a structured body, but service-level callers
        (e.g., property tests that call
        :meth:`create_deliverable_expectation` directly) benefit from
        receiving the same exception class regardless of which
        validation layer caught the problem. The ``failed_constraint``
        discriminator is derived from the first reported error so
        unit-test assertions remain stable.

        Pydantic wraps exceptions raised from ``mode='before'`` model
        validators (including the ``_validate_no_produced_attributes``
        validator on :class:`DeliverableExpectationCreationRequest`)
        inside a :class:`ValidationError` with ``type='value_error'``
        and the original exception preserved on ``ctx['error']``. This
        function inspects that slot first so a wrapped
        :class:`PlanningValidationError` retains its full
        ``prohibited_keys`` tuple on the surfaced
        :class:`DeliverableExpectationValidationError`.

        Args:
            exc: The :class:`ValidationError` raised by
                :class:`DeliverableExpectationCreationRequest`.

        Returns:
            A :class:`DeliverableExpectationValidationError` whose
            ``failed_constraint`` names the violated rule.
        """
        errors = exc.errors()
        first_error = errors[0] if errors else None
        if first_error is None:
            return DeliverableExpectationValidationError(
                str(exc), failed_constraint="invalid_request"
            )

        # Unwrap a wrapped PlanningValidationError so the
        # prohibited-attribute discriminator and the
        # ``prohibited_keys`` tuple flow through to the service-level
        # error type. Pydantic stores the original exception under
        # ``ctx['error']`` for ``value_error`` rows.
        ctx = first_error.get("ctx") or {}
        cause = ctx.get("error") if isinstance(ctx, Mapping) else None
        if isinstance(cause, PlanningValidationError):
            return DeliverableExpectationValidationError(
                str(cause),
                failed_constraint="prohibited_attribute",
                prohibited_keys=cause.prohibited_keys,
            )

        location = first_error.get("loc", ())
        error_type = first_error.get("type", "")
        field_name = location[0] if location else ""

        # Map the (field, pydantic-error-type) pair onto the structured
        # ``failed_constraint`` discriminators advertised on
        # :class:`DeliverableExpectationValidationError`. Anything not
        # in the table falls through to a generic discriminator
        # carrying the field name so callers can still distinguish
        # broken fields without parsing message text.
        constraint = _PYDANTIC_FAILED_CONSTRAINT_MAP.get(
            (field_name, error_type)
        )
        if constraint is None:
            constraint = f"{field_name}_invalid" if field_name else "invalid_request"
        return DeliverableExpectationValidationError(
            str(exc), failed_constraint=constraint
        )

    # -- denial side-channel ----------------------------------------------

    def _persist_deliverable_expectation_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_project_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Deliverable Expectation
        attempt.

        Implements the Requirement 7.6 retry contract verbatim
        (mirroring
        :meth:`walking_slice.planning.projects.ProjectService._persist_project_denial`
        and
        :meth:`walking_slice.planning.intended_outcomes.IntendedOutcomeService._persist_intended_outcome_denial`):
        each attempt opens a *new* :meth:`Engine.begin` transaction (so
        a previous attempt's rollback does not poison this one), tries
        :meth:`AuditLog.append_denial`, and either returns on success
        or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails,
          :class:`DeliverableExpectationAuditFailureError` is raised.

        The separate transaction is essential: the caller's originating
        transaction is about to be rolled back when
        :meth:`create_deliverable_expectation` raises
        :class:`DeliverableExpectationAuthorizationError` (or this
        method raises :class:`DeliverableExpectationAuditFailureError`).
        The Denial Record must therefore live outside that scope to
        survive (AD-WS-9 / Requirement 7.6).

        Both :class:`AuditAppendError` and :class:`SQLAlchemyError` are
        treated as retryable failures.
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=_ACTION_CREATE_DELIVERABLE_EXPECTATION,
                        target_id=target_project_id,
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

        raise DeliverableExpectationAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


# Mapping of (field-name, Pydantic-error-type) pairs to the
# ``failed_constraint`` discriminator on
# :class:`DeliverableExpectationValidationError`. The map keeps the
# error-translation function declarative; adding a new mapping is a
# one-line change. Pydantic v2 error type strings are stable across
# patch versions per the pydantic-core contract.
_PYDANTIC_FAILED_CONSTRAINT_MAP: Final[dict[tuple[str, str], str]] = {
    ("name", "string_too_short"): "name_missing",
    ("name", "missing"): "name_missing",
    ("name", "string_type"): "name_missing",
    ("name", "string_too_long"): "name_too_long",
    ("description", "string_too_long"): "description_too_long",
    ("description", "string_type"): "description_invalid_type",
    ("deliverable_kind", "literal_error"): "deliverable_kind_invalid",
    ("deliverable_kind", "missing"): "deliverable_kind_invalid",
    ("deliverable_kind", "string_type"): "deliverable_kind_invalid",
    ("acceptance_criteria", "string_too_long"): "acceptance_criteria_too_long",
    ("acceptance_criteria", "string_type"): "acceptance_criteria_invalid_type",
    ("target_project_id", "string_too_short"): "target_project_id_missing",
    ("target_project_id", "missing"): "target_project_id_missing",
    ("target_project_id", "string_type"): "target_project_id_missing",
    ("authoring_party_id", "string_too_short"): "authoring_party_id_missing",
    ("authoring_party_id", "missing"): "authoring_party_id_missing",
    ("authoring_party_id", "string_type"): "authoring_party_id_missing",
    ("applicable_scope", "string_too_short"): "applicable_scope_missing",
    ("applicable_scope", "missing"): "applicable_scope_missing",
    ("applicable_scope", "string_type"): "applicable_scope_missing",
}


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Deliverable
    Expectation creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Deliverable
    Expectation Resource Identity and the first Deliverable Expectation
    Revision Identity in ``Identifier_Registry``. Sharing one digest
    across both bindings mirrors the Slice 1 / sibling-Planning_Service
    pattern and keeps the AD-WS-2 non-reuse invariant exercised at the
    same granularity for Resource and first-Revision identifiers.
    """
    return hashlib.sha256(content).hexdigest()
