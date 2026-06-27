"""Planning_Service.IntendedOutcomes — Intended Outcome Resources, Revisions,
and the ``Addresses`` Relationship to the parent Objective.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.IntendedOutcomes" — public dataclass surface, the
  request-model contract (Pydantic ``Config(extra='forbid')`` plus an
  explicit ``_validate_no_observed_attributes`` validator rejecting any
  observed-outcome attribute key), the authority string
  (``create.intended_outcome`` → ``modify``), and the persistence
  invariant ``outcome_kind = 'intended'`` enforced by the
  ``Intended_Outcome_Revisions`` CHECK constraint.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction), Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and registered
  in ``Identifier_Registry`` with the additive ``resource_kind`` tag per
  AD-WS-19), Authorization (the action string ``create.intended_outcome``
  maps to the ``modify`` authority per AD-WS-15; the deny path reuses
  the Slice 1 separate-transaction Denial-Record pattern from
  :class:`walking_slice.planning.objectives.ObjectiveService`).
- AD-WS-15 — additive ``modify`` mapping for
  ``create.intended_outcome``.

Task scope (task 4.1)
=====================

This module implements
:meth:`IntendedOutcomeService.create_intended_outcome`:

1. Validate request inputs through a Pydantic
   :class:`IntendedOutcomeCreationRequest` model with
   ``Config(extra='forbid')`` plus an explicit model validator that
   rejects any top-level key matching an observed-outcome prefix
   (Requirement 3.3 / Requirement 13.1 / 13.5).
2. Resolve the target Objective via a single ``SELECT`` against
   ``Objectives`` by Identity (Requirement 3.4) — rejected when the
   identifier is unresolvable.
3. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (Slice 1 single-writer accommodation); on a deny outcome, persist a
   Denial Record in another separate transaction with the Requirement
   7.6 three-attempt exponential-backoff retry pattern, and raise
   :class:`IntendedOutcomeAuthorizationError` carrying the AD-WS-9
   denial response fields. Reuses the structure of
   :class:`walking_slice.planning.objectives.ObjectiveService` so the
   deny shape is byte-equivalent.
4. On a permit outcome, mint the Intended Outcome Resource Identity
   and the first Intended Outcome Revision Identity, register both in
   ``Identifier_Registry`` with their additive ``resource_kind`` tags
   (AD-WS-19), INSERT the ``Intended_Outcomes`` and
   ``Intended_Outcome_Revisions`` rows (the latter with
   ``outcome_kind = 'intended'``), INSERT the single ``Addresses``
   Relationship from the Revision to the target Objective, and append
   the consequential ``Audit_Records`` row — all inside the caller's
   transaction so a failure anywhere rolls every row back
   (Requirement 3.6 / AD-WS-5).

Requirements satisfied
======================

    3.1 — authorized Intended Outcome creation produces one Resource
          and one initial immutable Revision.
    3.2 — every Intended Outcome Revision records ``outcome_kind =
          'intended'``, success condition (1..4000), observation
          window (0..1000), attribution assumption (0..4000),
          authoring Party Identity, applicable scope, recorded time
          (UTC ms-precision), and the ``Addresses`` Relationship to
          the target Objective.
    3.3 — Intended Outcome creation requests carrying any observed
          measurement / observed outcome value / observed outcome
          time / attribution-evidence reference are rejected; no
          Resource or Revision is created.
    3.4 — Intended Outcome creation requests naming an unresolvable
          target Objective, more than one target, or omitting the
          success condition are rejected.
    3.5 — unauthorized requests are denied via
          :class:`AuthorizationService`; the Planning_Service
          declines to create any Resource or Revision and the
          Audit_Log appends a Denial Record conforming to AD-WS-9.
    3.6 — every successful Intended Outcome Revision insertion
          appends one immutable consequential audit row in the same
          transaction.
   13.1 — observed-outcome attribute keys are rejected at the
          request boundary.
   13.3 — every persisted ``Intended_Outcome_Revisions`` row carries
          ``outcome_kind = 'intended'``.
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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.planning._helpers import (
    OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


__all__ = [
    "CreateIntendedOutcomeResult",
    "IntendedOutcomeAuditFailureError",
    "IntendedOutcomeAuthorizationError",
    "IntendedOutcomeCreationRequest",
    "IntendedOutcomeObjectiveNotResolvableError",
    "IntendedOutcomeRevisionRow",
    "IntendedOutcomeService",
    "IntendedOutcomeValidationError",
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


# ``create.intended_outcome`` maps to the ``modify`` authority per
# AD-WS-15. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 3.6) and on the
# separate-transaction Denial Record so audit consumers can correlate
# denial rows with the action a Party was attempting.
_ACTION_CREATE_INTENDED_OUTCOME: Final[str] = "create.intended_outcome"

# Relationship Type and source/target ``kind`` strings written to
# ``Relationships`` rows. Constants ensure the strings cannot drift
# between this module, future Planning_Service modules, and the existing
# Slice 1 backlink algorithm that consumes ``Relationships`` rows
# verbatim.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"
_KIND_OBJECTIVE: Final[str] = "objective"

# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tags (Slice 2 additive enumeration
# per AD-WS-19).
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_INTENDED_OUTCOME: Final[str] = "intended_outcome"
_RESOURCE_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"

# Persistence-invariant value enforced by the
# ``Intended_Outcome_Revisions.outcome_kind`` CHECK constraint (design
# §"Planning_Service.IntendedOutcomes" — Persistence invariant,
# Requirement 13.3). Slice 2 never inserts any other value.
_OUTCOME_KIND_INTENDED: Final[str] = "intended"

# Validation limits per Requirement 3.2 ("success condition statement of
# 1 to 4,000 characters", "observation-window descriptor of 0 to 1,000
# characters", "attribution-assumption text of 0 to 4,000 characters").
# The schema CHECK constraints on ``Intended_Outcome_Revisions`` enforce
# the same values; centralizing them here surfaces precise, structured
# constraint names through :class:`IntendedOutcomeValidationError`.
_SUCCESS_CONDITION_MIN_CHARS: Final[int] = 1
_SUCCESS_CONDITION_MAX_CHARS: Final[int] = 4_000
_OBSERVATION_WINDOW_MIN_CHARS: Final[int] = 0
_OBSERVATION_WINDOW_MAX_CHARS: Final[int] = 1_000
_ATTRIBUTION_ASSUMPTION_MIN_CHARS: Final[int] = 0
_ATTRIBUTION_ASSUMPTION_MAX_CHARS: Final[int] = 4_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 7.6, mirroring the Slice 1 pattern
# in :meth:`KnowledgeService._persist_decision_denial` and the
# Slice 2 pattern in
# :meth:`ObjectiveService._persist_objective_denial`). Three retries
# after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class IntendedOutcomeValidationError(ValueError):
    """Raised when an Intended Outcome submission fails Requirement 3.2 / 3.3 / 3.4
    validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"success_condition_missing"`` (empty or non-string),
            ``"success_condition_too_long"``,
            ``"observation_window_too_long"``,
            ``"observation_window_invalid_type"``,
            ``"attribution_assumption_too_long"``,
            ``"attribution_assumption_invalid_type"``,
            ``"target_objective_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"outcome_kind_invalid"`` (any value other than the
                literal ``'intended'`` was attempted), or
            ``"prohibited_attribute"`` (the request body carried at
                least one observed-outcome attribute — see
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


class IntendedOutcomeObjectiveNotResolvableError(LookupError):
    """Raised when the target Objective Identity does not resolve.

    Requirement 3.4 requires the named target Objective Resource
    Identity to resolve to an existing Objective Resource. The
    exception carries the offending identifier so the HTTP layer
    (task 15.1) can render an actionable error.
    """

    def __init__(self, *, target_objective_id: str) -> None:
        super().__init__(
            f"Target Objective {target_objective_id!r} did not resolve to "
            "an existing Objective Resource (Requirement 3.4)."
        )
        self.target_objective_id = target_objective_id
        self.failed_constraint = "target_objective_not_resolvable"


class IntendedOutcomeAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies an Intended Outcome
    attempt.

    Mirrors :class:`walking_slice.planning.objectives.ObjectiveAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 3.5). The exception
    carries only ``reason_code`` and ``correlation_id`` — Requirement 10
    forbids leaking authorized Party identities, Objective contents,
    role assignment details, or target existence beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Intended Outcome creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class IntendedOutcomeAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails (Requirement 7.6).

    Mirrors :class:`walking_slice.planning.objectives.ObjectiveAuditFailureError`.
    On total audit-append failure the exception is raised *in place of*
    :class:`IntendedOutcomeAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's
    transaction still rolls back so no Intended Outcome row, Intended
    Outcome Revision row, Addresses Relationship row, or consequential
    audit row is persisted.

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
            f"Denial Record append for a denied Intended Outcome failed "
            f"after {attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Request model (Pydantic).
#
# Design §"Planning_Service.IntendedOutcomes" explicitly requires the
# request model to combine ``Config(extra='forbid')`` (which structurally
# rejects any field not named below — task 4.1 bullet 2) with an explicit
# ``_validate_no_observed_attributes`` validator (which surfaces a
# precise rejection reason naming every offending observed-outcome key
# per Requirement 13.5).
# ---------------------------------------------------------------------------


class IntendedOutcomeCreationRequest(BaseModel):
    """Validated request payload for
    :meth:`IntendedOutcomeService.create_intended_outcome`.

    The class enforces three layers of input discipline:

    1. **Field-level validation** — ``success_condition`` (1..4000),
       ``observation_window`` (0..1000), ``attribution_assumption``
       (0..4000), and the three required string identifiers are
       constrained at parse time. The same ranges are enforced again
       at the schema layer by ``Intended_Outcome_Revisions`` CHECK
       constraints (design §"Data Models — Schema Additions"), so a
       successful Pydantic validation guarantees a successful INSERT.
    2. **Structural forbid** — ``Config(extra='forbid')`` rejects any
       field not declared on the model. Together with the model's
       deliberate omission of any observed-outcome field, this is the
       structural side of Requirement 3.3 / 13.1: even an observed-
       outcome key the explicit validator below does not catch is
       still rejected by ``extra='forbid'``.
    3. **Explicit observed-outcome screen** — the
       :meth:`_validate_no_observed_attributes` model validator runs
       *before* per-field validation and inspects the raw input dict
       for top-level keys matching any prefix in
       :data:`walking_slice.planning._helpers.OBSERVED_OUTCOME_PROHIBITED_PREFIXES`
       (``observed-``, ``observation-time-``,
       ``attribution-evidence-``). Matching is case-insensitive and
       hyphen/underscore-invariant so both ``observed-value`` and
       ``observed_value`` are rejected. The validator surfaces every
       offending key on the raised
       :class:`walking_slice.planning._helpers.PlanningValidationError`
       so the response can name each per Requirement 13.5.

    The legitimate field ``observation_window`` does **not** match any
    observed-outcome prefix (``observed-`` requires a literal ``d``
    after ``observe``, so ``observation-window-...`` is not matched).
    The prefix is intentionally narrow so the descriptor field
    Requirement 3.2 mandates is not falsely rejected.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_objective_id: str = Field(
        min_length=1,
        description="Identity of the target Objective Resource (Requirement 3.4).",
    )
    success_condition: str = Field(
        min_length=_SUCCESS_CONDITION_MIN_CHARS,
        max_length=_SUCCESS_CONDITION_MAX_CHARS,
        description=(
            "Success condition statement of "
            f"{_SUCCESS_CONDITION_MIN_CHARS}..{_SUCCESS_CONDITION_MAX_CHARS} "
            "characters (Requirement 3.2)."
        ),
    )
    observation_window: Optional[str] = Field(
        default=None,
        max_length=_OBSERVATION_WINDOW_MAX_CHARS,
        description=(
            "Optional observation-window descriptor of "
            f"{_OBSERVATION_WINDOW_MIN_CHARS}..{_OBSERVATION_WINDOW_MAX_CHARS} "
            "characters (Requirement 3.2)."
        ),
    )
    attribution_assumption: Optional[str] = Field(
        default=None,
        max_length=_ATTRIBUTION_ASSUMPTION_MAX_CHARS,
        description=(
            "Optional attribution-assumption text of "
            f"{_ATTRIBUTION_ASSUMPTION_MIN_CHARS}.."
            f"{_ATTRIBUTION_ASSUMPTION_MAX_CHARS} characters (Requirement 3.2)."
        ),
    )
    authoring_party_id: str = Field(
        min_length=1,
        description="Identity of the authoring Party (Requirement 3.5).",
    )
    applicable_scope: str = Field(
        min_length=1,
        description="Scope identifier the Intended Outcome applies within (Requirement 3.2).",
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_no_observed_attributes(cls, data: Any) -> Any:
        """Reject request bodies that carry observed-outcome attribute keys.

        Implements design §"Planning_Service.IntendedOutcomes" —
        "explicit validator rejecting any incoming request body that
        names any of the prohibited keys named in Requirement 3.3 /
        Requirement 13". Delegates the per-prefix matching to
        :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
        which canonicalizes hyphen/underscore variants and is
        case-insensitive.

        Runs in ``mode='before'`` so the screen executes against the
        raw input dict — before unknown-field rejection by
        ``Config(extra='forbid')`` so the observed-outcome rejection
        wins when both apply (giving the more specific error per
        Requirement 13.5). Non-dict inputs (e.g., already-validated
        :class:`IntendedOutcomeCreationRequest` instances passed
        through round-trip) are returned unchanged because they are
        already known to be safe.
        """
        if isinstance(data, Mapping):
            _reject_prohibited_attributes(
                data, OBSERVED_OUTCOME_PROHIBITED_PREFIXES
            )
        return data


# ---------------------------------------------------------------------------
# Result value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateIntendedOutcomeResult:
    """Result of :meth:`IntendedOutcomeService.create_intended_outcome`.

    Returned so callers (the HTTP layer in task 15.1, tests,
    downstream Planning_Service modules that target this Intended
    Outcome) can correlate the created Resource with its first
    Revision, its ``Addresses`` Relationship to the Objective, and
    the consequential audit row in one round-trip.

    Attributes:
        intended_outcome_id: The Intended Outcome Resource Identity
            (UUIDv7).
        intended_outcome_revision_id: The first Intended Outcome
            Revision Identity.
        outcome_kind: Always the literal string ``"intended"``
            (Requirement 13.3); included so callers do not need to
            assume the persistence invariant.
        success_condition: The persisted success condition text
            (1..4000 chars).
        observation_window: The persisted observation-window
            descriptor (or ``None``).
        attribution_assumption: The persisted attribution-assumption
            text (or ``None``).
        target_objective_id: The Objective Identity the Intended
            Outcome addresses; copied byte-equivalent from the
            request input.
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Intended Outcome
            applies within.
        addresses_relationship_id: Identity of the single
            ``Addresses`` Relationship row inserted alongside the
            Revision (Requirement 3.2).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Intended_Outcomes`` row, the
            ``Intended_Outcome_Revisions`` row, the ``Addresses``
            Relationship row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            two on a single value.
    """

    intended_outcome_id: str
    intended_outcome_revision_id: str
    outcome_kind: str
    success_condition: str
    observation_window: Optional[str]
    attribution_assumption: Optional[str]
    target_objective_id: str
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


@dataclass(frozen=True)
class IntendedOutcomeRevisionRow:
    """Read-only snapshot of an ``Intended_Outcome_Revisions`` row.

    Returned by :meth:`IntendedOutcomeService.get_revision`, the additive
    Slice 4 Planning_Service read API introduced by AD-WS-40 (fourth walking
    slice design). The Outcome_Service resolves a Measurement Definition's,
    Observed Outcome's, Success-Condition Assessment's, or Outcome Review's
    target Intended Outcome Revision through this read and verifies the
    ``outcome_kind`` discriminator equals the literal ``'intended'``
    (Requirements 44.4, 47.4, 48.3, 49.4) before recording any
    outcome-measurement artifact.

    The row carries every column of the addressed Intended Outcome Revision
    so a single read serves both the ``outcome_kind`` gate and the
    "target Intended Outcome Resource Identity" anchor (``intended_outcome_id``)
    the Outcome_Service uses to match a cited Measurement Record to an
    Intended Outcome (AD-WS-40). No write surface is exposed: the read is a
    pure projection of the persisted Slice 2 row and never mutates it
    (Requirement 60.1).

    Frozen because — like every Slice 2 / Slice 3 / Slice 4 value object that
    crosses a module boundary — the receiver must be able to rely on the
    bytes not changing while the in-flight transaction completes. A
    :func:`dataclasses.dataclass(frozen=True)` is used (rather than a Pydantic
    model) to match the sibling result type :class:`CreateIntendedOutcomeResult`
    in this module and the Slice 3
    :class:`walking_slice.planning.plan_revisions.PlanRevisionRow` /
    :class:`walking_slice.deliverables.repository.DeliverableRevisionRow`
    read-API conventions.

    Attributes:
        intended_outcome_revision_id: The Intended Outcome Revision Identity
            (UUIDv7). Echoes the request input on a successful lookup.
        intended_outcome_id: The target Intended Outcome **Resource** Identity
            the Revision belongs to (Requirement 43.2 — Resource Identity and
            Revision Identity are distinct). This is the anchor the
            Outcome_Service matches against a Measurement Definition's
            ``target_intended_outcome_resource_id`` (AD-WS-40).
        parent_revision_id: The immediately prior Intended Outcome Revision
            Identity, or ``None`` on the initial Revision.
        outcome_kind: Always the literal string ``"intended"`` for any row
            persisted by Slice 2 (enforced by the
            ``Intended_Outcome_Revisions.outcome_kind`` CHECK). Surfaced so
            the Outcome_Service can gate on it without a second lookup.
        target_objective_id: The parent Objective Resource Identity the
            Intended Outcome addresses (Slice 2 Requirement 3.2).
        success_condition: The persisted success-condition statement
            (1..4000 chars).
        observation_window: The persisted observation-window descriptor, or
            ``None``.
        attribution_assumption: The persisted attribution-assumption text, or
            ``None``.
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Intended Outcome applies
            within.
        recorded_at: UTC ISO-8601 millisecond-precision recorded time of the
            Revision.
    """

    intended_outcome_revision_id: str
    intended_outcome_id: str
    parent_revision_id: Optional[str]
    outcome_kind: str
    target_objective_id: str
    success_condition: str
    observation_window: Optional[str]
    attribution_assumption: Optional[str]
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntendedOutcomeService:
    """Persist Intended Outcome Resources, Revisions, and their ``Addresses``
    Relationships to parent Objectives.

    Like :class:`walking_slice.planning.objectives.ObjectiveService`,
    this service is connection-scoped at call time:
    :meth:`create_intended_outcome` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (AD-WS-5). The service instance therefore
    holds only the cross-request collaborators and can be shared
    across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.IntendedOutcomes" declares it
    ``@dataclass(frozen=True)``. Slice 2 service instances are
    immutable container objects that bundle Slice 1 collaborators
    for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Intended_Outcomes``, ``Intended_Outcome_Revisions``,
            ``Relationships``, and ``Audit_Records`` rows. The clock
            is consulted exactly once per write so every artifact of
            the transaction shares one timestamp.
        identity_service: Generates Intended Outcome Resource and
            Revision Identities and persists their
            ``Identifier_Registry`` bindings (the bindings carry the
            Slice 2 ``resource_kind`` tag per AD-WS-19).
        audit_log: Appends the consequential audit row (Requirement
            3.6) inside the caller's transaction.
        authorization_service: Evaluates
            ``create.intended_outcome`` authority per AD-WS-15 /
            Requirement 3.5; the deny path is the Slice 1
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

    def create_intended_outcome(
        self,
        connection: Connection,
        *,
        target_objective_id: str,
        success_condition: str,
        observation_window: Optional[str],
        attribution_assumption: Optional[str],
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateIntendedOutcomeResult:
        """Create an Intended Outcome Resource plus its first immutable
        Revision and ``Addresses`` Relationship to the target Objective.

        Per Requirements 3.1 through 3.6, 13.1, 13.3, AD-WS-9
        (indistinguishable denial), AD-WS-15
        (``create.intended_outcome`` → ``modify``), and AD-WS-19
        (resource_kind tagged identifiers + append-only Slice 2
        tables):

        1. When ``request_attributes`` is supplied, defensively
           screen the raw request body against the observed-outcome
           prefix list (Property 22 / Requirements 13.1, 13.5). The
           typed kwargs themselves cannot carry a prohibited
           attribute, but the HTTP layer's raw body might.
        2. Build the :class:`IntendedOutcomeCreationRequest` Pydantic
           model from the typed kwargs. The model enforces every
           Requirement 3.2 range and runs the
           ``_validate_no_observed_attributes`` model validator
           against the kwargs dict — a redundant but cheap second
           check that hardens callers that bypass the route layer.
        3. Resolve the target Objective via a single ``SELECT``
           against ``Objectives`` by Identity (Requirement 3.4). The
           check runs before authorization evaluation so the deny
           path never reveals whether an Objective exists for an
           unauthorized caller.
        4. Run the authorization evaluation on a *separate*
           transaction (the Slice 1 single-writer accommodation
           documented on
           :meth:`KnowledgeService.create_decision`). On ``deny``,
           append the Denial Record in another separate transaction
           with the Requirement 7.6 retry sequence (0.01s / 0.02s /
           0.04s exponential backoff, three retries after the
           initial attempt). On total audit failure raise
           :class:`IntendedOutcomeAuditFailureError` in place of
           :class:`IntendedOutcomeAuthorizationError` so denial-and-
           audit divergence is unambiguous to the operator.
        5. On ``permit``, mint the Intended Outcome Resource and
           first Revision Identities and register them in
           ``Identifier_Registry`` (kind ``'resource'`` and
           ``'revision'`` respectively, both carrying the Slice 2
           ``resource_kind`` tag per AD-WS-19).
        6. INSERT the ``Intended_Outcomes`` Resource header,
           ``Intended_Outcome_Revisions`` Revision row carrying every
           Requirement 3.2 attribute (with ``outcome_kind =
           'intended'`` enforced by both the Python constant and the
           schema CHECK), the single ``Addresses`` ``Relationships``
           row with ``semantic_role = NULL`` (AD-WS-17 — the column
           is reserved for Plan Review's ``'review'`` discriminator
           and is not used here), and the consequential
           ``Audit_Records`` row, all inside the caller's
           transaction. A FK or trigger failure anywhere rolls the
           whole transaction back.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_objective_id: Identity of the parent Objective
                (Requirement 3.4).
            success_condition: Success condition of 1..4000
                characters (Requirement 3.2).
            observation_window: Optional observation-window
                descriptor of 0..1000 characters, or ``None``. The
                empty string is a valid persisted value; ``None`` is
                persisted as SQL ``NULL``.
            attribution_assumption: Optional attribution-assumption
                text of 0..4000 characters, or ``None``. Same
                semantics as ``observation_window``.
            authoring_party_id: Identity of the authoring Party.
                Persisted on
                ``Intended_Outcome_Revisions.authoring_party_id`` and
                on the consequential audit row's
                ``actor_party_id``.
            applicable_scope: Scope identifier the Intended Outcome
                applies within. Persisted on
                ``Intended_Outcome_Revisions.applicable_scope`` and
                passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            engine: Required for the deny path's separate-
                transaction Denial Record write so the row survives
                the caller's rollback (Requirement 7.6). The same
                engine is used to open a fresh transaction for the
                authorization evaluation itself.
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
                mapping is screened against every observed-outcome
                attribute prefix (Property 22 / Requirement 13.1).
                The HTTP layer forwards the raw request body here;
                service-level callers (e.g., unit tests) may pass
                ``None`` to skip the screen since the typed kwargs
                cannot carry a prohibited attribute.

        Returns:
            :class:`CreateIntendedOutcomeResult` carrying the
            persisted identifiers, attributes, the ``Addresses``
            Relationship Identity, the recorded time, and the
            correlation identifier.

        Raises:
            IntendedOutcomeValidationError: A required attribute is
                missing, a Requirement 3.2 range was violated, or
                the request body carried a prohibited observed-
                outcome attribute (Requirement 13.1 / 13.5).
            IntendedOutcomeObjectiveNotResolvableError: The target
                Objective Identity did not resolve (Requirement 3.4).
            IntendedOutcomeAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 3.5). The Denial Record was appended
                successfully in a separate transaction
                (Requirement 7.6).
            IntendedOutcomeAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`IntendedOutcomeAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 3.6.
        """
        # 1. Screen the original request body when the route layer
        # has forwarded it. The typed kwargs cannot carry a
        # prohibited attribute (the signature does not declare any
        # such field), but the HTTP layer's raw body might —
        # Requirement 13.1 / Property 22 demands rejection at the
        # API boundary.
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, OBSERVED_OUTCOME_PROHIBITED_PREFIXES
                )
            except PlanningValidationError as exc:
                raise IntendedOutcomeValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate kwargs through the Pydantic request model. The
        # model's structural checks (extra='forbid', per-field length
        # and type constraints) and the
        # ``_validate_no_observed_attributes`` model validator are
        # what task 4.1 mandates. Any ValidationError is converted to
        # an :class:`IntendedOutcomeValidationError` so the public
        # error surface of this service stays uniform with the rest
        # of the Planning_Service.
        try:
            request = IntendedOutcomeCreationRequest(
                target_objective_id=target_objective_id,
                success_condition=success_condition,
                observation_window=observation_window,
                attribution_assumption=attribution_assumption,
                authoring_party_id=authoring_party_id,
                applicable_scope=applicable_scope,
            )
        except ValidationError as exc:
            # Pydantic wraps any exception raised from a
            # ``mode='before'`` model validator (including
            # :class:`PlanningValidationError`) inside a
            # :class:`ValidationError`. The translator below
            # unwraps the ``ctx['error']`` slot and surfaces
            # :class:`IntendedOutcomeValidationError` with the
            # ``failed_constraint`` discriminator most appropriate
            # to the first reported error — including the
            # ``"prohibited_attribute"`` discriminator and the
            # original ``prohibited_keys`` tuple when the wrapped
            # cause is a :class:`PlanningValidationError`.
            raise self._translate_pydantic_error(exc) from exc

        # 3. Resolve the target Objective via a single SELECT
        # against ``Objectives`` by Identity (Requirement 3.4). The
        # lookup runs on the caller's connection so it participates
        # in the caller's transactional view.
        resolved = connection.execute(
            text(
                "SELECT objective_id FROM Objectives "
                "WHERE objective_id = :objective_id"
            ),
            {"objective_id": request.target_objective_id},
        ).scalar_one_or_none()
        if resolved is None:
            raise IntendedOutcomeObjectiveNotResolvableError(
                target_objective_id=request.target_objective_id
            )

        # 4. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Intended_Outcomes row, the Intended_Outcome_Revisions row,
        # the Addresses Relationship row, and the consequential
        # audit row all share this timestamp; the optional
        # ``evaluation_at`` parameter changes only *when* authority
        # is evaluated *as of*, not the recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 5. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 single-writer accommodation). The
        # ``TargetRef`` is the parent Objective so the wired role
        # assignment must cover the same scope to permit the action.
        # On ``permit`` the evaluation row commits independently;
        # on ``deny`` the evaluation row rolls back with the
        # evaluation transaction and the durable record of the
        # denial is the Denial Record appended below.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=request.authoring_party_id,
                action=_ACTION_CREATE_INTENDED_OUTCOME,
                target=TargetRef(
                    kind=_KIND_OBJECTIVE,
                    id=request.target_objective_id,
                    revision_id=None,
                    scope=request.applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_intended_outcome_denial(
                engine=engine,
                actor_party_id=request.authoring_party_id,
                target_objective_id=request.target_objective_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise IntendedOutcomeAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. Mint identifiers (AD-WS-2 / AD-WS-19). The Intended
        # Outcome Resource and its first Revision are bound to the
        # same content digest because the Resource has no separate
        # "natural content" — the digest is derived from the first
        # Revision's payload (mirroring the Slice 1 pattern in
        # :meth:`KnowledgeService.create_finding` and the Slice 2
        # pattern in :meth:`ObjectiveService.create_objective`).
        intended_outcome_id = str(self.identity_service.new_resource_id())
        intended_outcome_revision_id = str(
            self.identity_service.new_revision_id()
        )
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "outcome_kind": _OUTCOME_KIND_INTENDED,
                    "target_objective_id": request.target_objective_id,
                    "success_condition": request.success_condition,
                    "observation_window": request.observation_window,
                    "attribution_assumption": request.attribution_assumption,
                    "authoring_party_id": request.authoring_party_id,
                    "applicable_scope": request.applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 7. Register both identifiers in ``Identifier_Registry``
        # carrying the AD-WS-19 ``resource_kind`` tag. The helper
        # delegates to :meth:`IdentityService.reject_if_duplicate`
        # so the Slice 1 identifier-conflict Denial Record pathway
        # fires on any collision; on success the helper INSERTs one
        # row per identifier inside the caller's transaction.
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_INTENDED_OUTCOME,
            intended_outcome_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=request.authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_INTENDED_OUTCOME,
            recorded_time=recorded_time,
        )
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_INTENDED_OUTCOME_REVISION,
            intended_outcome_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=request.authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_INTENDED_OUTCOME,
            recorded_time=recorded_time,
        )

        # 8. Insert the Resource header. ``created_at`` carries the
        # same recorded time as the first Revision (design
        # §"Persistence Invariants Summary") so the two rows'
        # timestamps are byte-equivalent.
        connection.execute(
            text(
                """
                INSERT INTO Intended_Outcomes (intended_outcome_id, created_at)
                VALUES (:intended_outcome_id, :created_at)
                """
            ),
            {
                "intended_outcome_id": intended_outcome_id,
                "created_at": recorded_at,
            },
        )

        # 9. Insert the first immutable Revision (Requirement 3.2).
        # ``outcome_kind`` is bound to the module constant so any
        # future drift from the literal ``'intended'`` is a single-
        # site change rather than a scattered one; the schema CHECK
        # additionally rejects any other value as a defense in
        # depth (Requirement 13.3 / design §"Planning_Service.
        # IntendedOutcomes" — Persistence invariant).
        connection.execute(
            text(
                """
                INSERT INTO Intended_Outcome_Revisions (
                    intended_outcome_revision_id, intended_outcome_id,
                    parent_revision_id, outcome_kind, target_objective_id,
                    success_condition, observation_window,
                    attribution_assumption, authoring_party_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :intended_outcome_revision_id, :intended_outcome_id,
                    NULL, :outcome_kind, :target_objective_id,
                    :success_condition, :observation_window,
                    :attribution_assumption, :authoring_party_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "intended_outcome_revision_id": intended_outcome_revision_id,
                "intended_outcome_id": intended_outcome_id,
                "outcome_kind": _OUTCOME_KIND_INTENDED,
                "target_objective_id": request.target_objective_id,
                "success_condition": request.success_condition,
                "observation_window": request.observation_window,
                "attribution_assumption": request.attribution_assumption,
                "authoring_party_id": request.authoring_party_id,
                "applicable_scope": request.applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 10. Insert the single ``Addresses`` Relationship row
        # (Requirement 3.2). ``semantic_role`` is NULL — the AD-WS-17
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
                "source_kind": _KIND_INTENDED_OUTCOME_REVISION,
                "source_id": intended_outcome_id,
                "source_revision_id": intended_outcome_revision_id,
                "target_kind": _KIND_OBJECTIVE,
                "target_id": request.target_objective_id,
                "target_revision_id": None,
                "authoring_party_id": request.authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 11. Append the consequential audit row (Requirement 3.6 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Intended_Outcomes,
        # Intended_Outcome_Revisions, and Relationships rows
        # together.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=request.authoring_party_id,
            action_type=_ACTION_CREATE_INTENDED_OUTCOME,
            target_id=intended_outcome_id,
            target_revision_id=intended_outcome_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateIntendedOutcomeResult(
            intended_outcome_id=intended_outcome_id,
            intended_outcome_revision_id=intended_outcome_revision_id,
            outcome_kind=_OUTCOME_KIND_INTENDED,
            success_condition=request.success_condition,
            observation_window=request.observation_window,
            attribution_assumption=request.attribution_assumption,
            target_objective_id=request.target_objective_id,
            authoring_party_id=request.authoring_party_id,
            applicable_scope=request.applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- read APIs ---------------------------------------------------------

    @staticmethod
    def get_revision(
        connection: Connection,
        intended_outcome_revision_id: str,
    ) -> Optional[IntendedOutcomeRevisionRow]:
        """Read-only lookup of an Intended Outcome Revision row by Identity.

        Implements the additive Slice 4 Planning_Service read API mandated by
        AD-WS-40 (fourth walking slice design §"AD-WS-40"). The Outcome_Service
        calls this read to resolve a Measurement Definition's, Observed
        Outcome's, Success-Condition Assessment's, or Outcome Review's target
        Intended Outcome Revision and to verify its ``outcome_kind`` equals the
        literal ``'intended'`` (Requirements 44.4, 47.4, 48.3, 49.4) before
        recording any outcome-measurement artifact.

        The lookup is a single indexed ``SELECT`` against
        ``Intended_Outcome_Revisions`` keyed on the primary key
        ``intended_outcome_revision_id``, returning the row including its
        ``outcome_kind`` discriminator and the target Intended Outcome
        **Resource** Identity (``intended_outcome_id``) — the anchor the
        Outcome_Service matches against a Measurement Definition's
        ``target_intended_outcome_resource_id`` (AD-WS-40).

        This read introduces **no write path** on the Planning_Service
        (Requirement 60.1): it neither mutates the resolved Slice 2 row nor
        any other row. It is a :func:`staticmethod` because the read consults
        none of the wired collaborators (clock, identity service, audit log,
        authorization service) — it needs only the caller's
        :class:`~sqlalchemy.engine.Connection`. Exposing it on
        :class:`IntendedOutcomeService` keeps the design-pinned entry-point
        name (``IntendedOutcomeService.get_revision``) textually stable and
        matches the convention established by
        :meth:`walking_slice.planning.plan_revisions.PlanRevisionService.get_plan_revision`
        and :meth:`walking_slice.deliverables.repository.DeliverableRepositoryService.get_revision`.

        Args:
            connection: SQLAlchemy connection bound to the caller's read
                context. The lookup participates in the caller's
                transactional view so consumers see a consistent snapshot
                across multiple reads.
            intended_outcome_revision_id: The Intended Outcome Revision
                Identity to resolve.

        Returns:
            An :class:`IntendedOutcomeRevisionRow` snapshot when a matching
            row exists. ``None`` when no ``Intended_Outcome_Revisions`` row
            matches the supplied identifier; the caller treats ``None`` as
            the unresolvable branch (e.g., the Outcome_Service rejects the
            create attempt; the HTTP layer maps to a 404). Returning ``None``
            rather than raising mirrors the
            :meth:`PlanRevisionService.get_plan_revision` and
            :meth:`DeliverableRepositoryService.get_revision` ``one_or_none``
            convention and lets the caller decide how to handle the absent
            case without try/except in the hot path.

        Notes:
            The function does not validate ``intended_outcome_revision_id``
            beyond passing it through to SQLAlchemy as a bound parameter; the
            calling layer is responsible for structural validation of
            identifiers received from untrusted input. A non-resolving
            identifier returns ``None`` rather than raising.
        """
        row = connection.execute(
            text(
                "SELECT intended_outcome_revision_id, intended_outcome_id, "
                "parent_revision_id, outcome_kind, target_objective_id, "
                "success_condition, observation_window, "
                "attribution_assumption, authoring_party_id, "
                "applicable_scope, recorded_at "
                "FROM Intended_Outcome_Revisions "
                "WHERE intended_outcome_revision_id = "
                ":intended_outcome_revision_id"
            ),
            {"intended_outcome_revision_id": intended_outcome_revision_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        return IntendedOutcomeRevisionRow(
            intended_outcome_revision_id=row["intended_outcome_revision_id"],
            intended_outcome_id=row["intended_outcome_id"],
            parent_revision_id=row["parent_revision_id"],
            outcome_kind=row["outcome_kind"],
            target_objective_id=row["target_objective_id"],
            success_condition=row["success_condition"],
            observation_window=row["observation_window"],
            attribution_assumption=row["attribution_assumption"],
            authoring_party_id=row["authoring_party_id"],
            applicable_scope=row["applicable_scope"],
            recorded_at=row["recorded_at"],
        )

    # -- Pydantic error translation ---------------------------------------

    @staticmethod
    def _translate_pydantic_error(
        exc: ValidationError,
    ) -> "IntendedOutcomeValidationError":
        """Convert a Pydantic :class:`ValidationError` to a structured
        :class:`IntendedOutcomeValidationError`.

        The HTTP layer (task 15.1) already maps Pydantic errors to
        HTTP 400 with a structured body, but service-level callers
        (e.g., property tests that call
        :meth:`create_intended_outcome` directly) benefit from
        receiving the same exception class regardless of which
        validation layer caught the problem. The
        ``failed_constraint`` discriminator is derived from the
        first reported error so unit-test assertions remain
        stable.

        Pydantic wraps exceptions raised from ``mode='before'``
        model validators (including the
        ``_validate_no_observed_attributes`` validator on
        :class:`IntendedOutcomeCreationRequest`) inside a
        :class:`ValidationError` with ``type='value_error'`` and the
        original exception preserved on ``ctx['error']``. This
        function inspects that slot first so a wrapped
        :class:`PlanningValidationError` retains its full
        ``prohibited_keys`` tuple on the surfaced
        :class:`IntendedOutcomeValidationError`.

        Args:
            exc: The :class:`ValidationError` raised by
                :class:`IntendedOutcomeCreationRequest`.

        Returns:
            A :class:`IntendedOutcomeValidationError` whose
            ``failed_constraint`` names the violated rule.
        """
        errors = exc.errors()
        first_error = errors[0] if errors else None
        if first_error is None:
            return IntendedOutcomeValidationError(
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
            return IntendedOutcomeValidationError(
                str(cause),
                failed_constraint="prohibited_attribute",
                prohibited_keys=cause.prohibited_keys,
            )

        location = first_error.get("loc", ())
        error_type = first_error.get("type", "")
        field_name = location[0] if location else ""

        # Map the (field, pydantic-error-type) pair onto the
        # structured ``failed_constraint`` discriminators advertised
        # on :class:`IntendedOutcomeValidationError`. Anything not in
        # the table falls through to a generic discriminator carrying
        # the field name so callers can still distinguish broken
        # fields without parsing message text.
        constraint = _PYDANTIC_FAILED_CONSTRAINT_MAP.get(
            (field_name, error_type)
        )
        if constraint is None:
            constraint = f"{field_name}_invalid" if field_name else "invalid_request"
        return IntendedOutcomeValidationError(
            str(exc), failed_constraint=constraint
        )

    # -- denial side-channel ----------------------------------------------

    def _persist_intended_outcome_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_objective_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Intended Outcome attempt.

        Implements the Requirement 7.6 retry contract verbatim
        (mirroring
        :meth:`ObjectiveService._persist_objective_denial` and
        :meth:`walking_slice.knowledge.KnowledgeService._persist_decision_denial`):
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
          :class:`IntendedOutcomeAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_intended_outcome` raises
        :class:`IntendedOutcomeAuthorizationError` (or this method
        raises :class:`IntendedOutcomeAuditFailureError`). The
        Denial Record must therefore live outside that scope to
        survive (AD-WS-9 / Requirement 7.6).

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
                        attempted_action=_ACTION_CREATE_INTENDED_OUTCOME,
                        target_id=target_objective_id,
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

        raise IntendedOutcomeAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
# ---------------------------------------------------------------------------


# Mapping of (field-name, Pydantic-error-type) pairs to the
# ``failed_constraint`` discriminator on
# :class:`IntendedOutcomeValidationError`. The map keeps the error-
# translation function declarative; adding a new mapping is a one-line
# change. Pydantic v2 error type strings are stable across patch
# versions per the pydantic-core contract.
_PYDANTIC_FAILED_CONSTRAINT_MAP: Final[dict[tuple[str, str], str]] = {
    ("success_condition", "string_too_short"): "success_condition_missing",
    ("success_condition", "missing"): "success_condition_missing",
    ("success_condition", "string_type"): "success_condition_missing",
    ("success_condition", "string_too_long"): "success_condition_too_long",
    ("observation_window", "string_too_long"): "observation_window_too_long",
    ("observation_window", "string_type"): "observation_window_invalid_type",
    ("attribution_assumption", "string_too_long"): "attribution_assumption_too_long",
    ("attribution_assumption", "string_type"): "attribution_assumption_invalid_type",
    ("target_objective_id", "string_too_short"): "target_objective_id_missing",
    ("target_objective_id", "missing"): "target_objective_id_missing",
    ("target_objective_id", "string_type"): "target_objective_id_missing",
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
    consequential audit row produced for the same logical Intended
    Outcome creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Intended
    Outcome Resource Identity and the first Intended Outcome
    Revision Identity in ``Identifier_Registry``. Sharing one
    digest across both bindings mirrors the Slice 1 pattern in
    :meth:`walking_slice.knowledge.KnowledgeService.create_finding`
    and the Slice 2 pattern in
    :meth:`ObjectiveService.create_objective`.
    """
    return hashlib.sha256(content).hexdigest()
