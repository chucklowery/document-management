"""Planning_Service.Objectives — Objective Resources, Revisions, and the
``Addresses`` Relationship to the Slice 1 Decision Immutable Record.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.Objectives" — public dataclass surface, authority
  string (``create.objective`` → ``modify``), AD-WS-9 separate-transaction
  Denial Record on deny.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction), Identifiers (every new identity
  is a UUIDv7 minted by :class:`IdentityService` and registered in
  ``Identifier_Registry`` with ``kind ∈ {'resource', 'revision'}`` and
  ``resource_kind`` set to ``'objective'`` or ``'objective_revision'``),
  Authorization (the action string ``create.objective`` maps to the
  ``modify`` authority per AD-WS-15; the deny path uses the Slice 1
  separate-transaction Denial-Record pattern from
  :class:`walking_slice.knowledge.KnowledgeService.create_decision`).
- AD-WS-15 — additive ``modify`` mapping for ``create.objective``.
- AD-WS-21 — resolution of the target Decision goes through the
  Knowledge_Service public read API (:meth:`KnowledgeService.get_decision`)
  rather than reading the Slice 1 ``Decisions`` table directly.

Task scope (task 3.1)
=====================

This module implements :meth:`ObjectiveService.create_objective`:

1. Validate request inputs per Requirement 2.3 (statement 1..4000,
   rationale optional 0..10000) and Requirement 2.6 (required attributes).
2. Defensively reject any prohibited execution / observed-outcome /
   produced-deliverable attribute via
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   (Property 22). The service signature is typed so only legitimate
   keyword arguments are accepted; the optional ``request_attributes``
   parameter lets the HTTP route forward the raw request body so its
   top-level keys are screened too.
3. Resolve the target Decision through
   :meth:`KnowledgeService.get_decision` (AD-WS-21); reject when the
   identifier is unresolvable or the resolved Decision's outcome is not
   ``'Accept'`` (Requirement 2.4).
4. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (the Slice 1 single-writer accommodation documented on
   :meth:`KnowledgeService.create_decision`); on a deny outcome,
   persist a Denial Record in another separate transaction with the
   Requirement 7.6 / Slice 1 three-attempt exponential-backoff retry
   pattern, and raise :class:`ObjectiveAuthorizationError` carrying the
   AD-WS-9 denial response fields (``reason_code``, ``correlation_id``).
5. On a permit outcome, mint the Objective Resource Identity and the
   first Objective Revision Identity, register both in
   ``Identifier_Registry`` with their additive ``resource_kind`` tags
   (AD-WS-19), INSERT the ``Objectives`` and ``Objective_Revisions``
   rows, INSERT the single ``Addresses`` Relationship from the
   Objective Revision to the target Decision Immutable Record
   (Requirement 2.3), and append the consequential ``Audit_Records``
   row (Requirement 2.7 / AD-WS-5) — all inside the caller's
   transaction so a failure anywhere rolls every row back.

Requirements satisfied
======================

    2.1 — authorized Objective creation produces one Objective Resource
          and one initial immutable Objective Revision.
    2.2 — every Objective creation requires the named target Decision
          Immutable Record Identity to resolve to a Decision whose
          ``outcome`` is ``'Accept'``.
    2.3 — every Objective Revision records statement (1..4000),
          rationale (0..10000), authoring Party Identity, applicable
          scope, recorded time (UTC ms-precision), and the
          ``Addresses`` Relationship to the target Decision.
    2.4 — unresolvable or non-``Accept`` target Decisions are rejected
          and no Resource or Revision is created.
    2.5 — unauthorized requests are denied via
          :class:`AuthorizationService`; the Planning_Service declines
          to create any Resource or Revision and the Audit_Log
          appends a Denial Record conforming to AD-WS-9.
    2.6 — missing required attributes are rejected with a structured
          error identifying the missing or invalid attribute.
    2.7 — every successful Objective Revision insertion appends one
          immutable consequential audit row in the same transaction.
    7.6 — the Denial Record append is retried up to three times with
          exponential backoff (0.01s, 0.02s, 0.04s); on total audit
          failure :class:`ObjectiveAuditFailureError` is raised so
          denial and audit cannot silently diverge.
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
from walking_slice.identity import IdentityService
from walking_slice.knowledge import DecisionRecord, KnowledgeService
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


__all__ = [
    "CreateObjectiveResult",
    "ObjectiveAuditFailureError",
    "ObjectiveAuthorizationError",
    "ObjectiveDecisionNotResolvableError",
    "ObjectiveService",
    "ObjectiveValidationError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings and Relationship kind strings are pulled out as module-level
# Final so the names that downstream property tests look for in
# ``Audit_Records.action_type`` and ``Relationships`` are textually stable
# and the strings stay aligned with the
# :mod:`walking_slice.planning._persistence` schema and the AD-WS-15
# authority mapping in :mod:`walking_slice.authorization`.
# ---------------------------------------------------------------------------


# ``create.objective`` maps to the ``modify`` authority per AD-WS-15. The
# string is also the ``action_type`` recorded on the consequential audit
# row (Requirement 2.7) and on the separate-transaction Denial Record
# appended by :meth:`ObjectiveService._persist_objective_denial` so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_OBJECTIVE: Final[str] = "create.objective"

# Relationship Type and source/target ``kind`` strings written to
# ``Relationships`` rows. Constants ensure the strings cannot drift
# between this module, future Planning_Service modules, and the existing
# Slice 1 backlink algorithm that consumes ``Relationships`` rows
# verbatim.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_OBJECTIVE_REVISION: Final[str] = "objective_revision"
_KIND_DECISION: Final[str] = "decision"

# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tags (Slice 2 additive enumeration
# per AD-WS-19). Pulled out so a future kind addition is a one-line
# change here rather than scattered across the module.
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_OBJECTIVE: Final[str] = "objective"
_RESOURCE_KIND_OBJECTIVE_REVISION: Final[str] = "objective_revision"

# Validation limits per Requirement 2.3 ("statement of 1 to 4,000
# characters" and "rationale text of 0 to 10,000 characters"). The
# schema CHECK constraints on ``Objective_Revisions`` enforce the same
# values; centralizing them here surfaces precise, structured
# constraint names through :class:`ObjectiveValidationError`.
_STATEMENT_MIN_CHARS: Final[int] = 1
_STATEMENT_MAX_CHARS: Final[int] = 4_000
_RATIONALE_MIN_CHARS: Final[int] = 0
_RATIONALE_MAX_CHARS: Final[int] = 10_000

# Decision outcomes the Slice 1 ``Decisions.outcome`` CHECK constraint
# admits (AD-WS-11). The only outcome that satisfies Requirement 2.2 is
# ``'Accept'``; ``'Reject'`` and ``'Defer'`` are rejected by Requirement
# 2.4.
_DECISION_OUTCOME_ACCEPT: Final[str] = "Accept"

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 7.6, mirroring the Slice 1
# pattern in :meth:`KnowledgeService._persist_decision_denial`). Three
# retries after the initial attempt for a total of four attempts.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ObjectiveValidationError(ValueError):
    """Raised when an Objective submission fails Requirement 2.3 / 2.6 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"statement_missing"`` (empty or non-string statement),
            ``"statement_too_long"``,
            ``"rationale_too_long"``,
            ``"rationale_invalid_type"`` (non-string and non-None),
            ``"target_decision_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"applicable_scope_missing"``,
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


class ObjectiveDecisionNotResolvableError(LookupError):
    """Raised when the target Decision does not resolve or is not ``Accept``.

    Requirement 2.4 requires the target Decision Immutable Record
    Identity to resolve to an existing Decision whose ``outcome`` at
    creation time is ``'Accept'``. The exception covers both the
    "no such Decision" and the "wrong outcome" cases; the
    ``failed_constraint`` attribute distinguishes them so the HTTP
    layer (task 15.1) can render an actionable error.

    Attributes:
        target_decision_id: The Decision Identity the caller supplied.
        failed_constraint: ``"target_decision_not_resolvable"`` when
            no ``Decisions`` row matched the identifier;
            ``"target_decision_outcome_not_accept"`` when the row
            existed but its ``outcome`` was not ``'Accept'``.
        outcome: When the Decision exists but its outcome is wrong,
            the persisted outcome value (``'Reject'`` or ``'Defer'``).
            ``None`` when no Decision resolved.
    """

    def __init__(
        self,
        *,
        target_decision_id: str,
        failed_constraint: str,
        outcome: Optional[str] = None,
    ) -> None:
        super().__init__(
            f"Target Decision {target_decision_id!r} did not resolve to an "
            f"accepting Decision Immutable Record "
            f"(failed_constraint={failed_constraint!r}, outcome={outcome!r})."
        )
        self.target_decision_id = target_decision_id
        self.failed_constraint = failed_constraint
        self.outcome = outcome


class ObjectiveAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies an Objective attempt.

    Mirrors :class:`walking_slice.knowledge.DecisionAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 2.5 / 10.x). The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 10 forbids leaking authorized Party identities,
    Decision contents, role assignment details, or target existence
    beyond the requesting Party's view authority through the denial
    response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Objective creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class ObjectiveAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails (Requirement 7.6).

    Mirrors :class:`walking_slice.knowledge.DecisionAuditFailureError`.
    On total audit-append failure the exception is raised *in place of*
    :class:`ObjectiveAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's
    transaction still rolls back so no Objective row, Objective
    Revision row, Addresses Relationship, or consequential audit row
    is persisted.

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
            f"Denial Record append for a denied Objective failed after "
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
class CreateObjectiveResult:
    """Result of :meth:`ObjectiveService.create_objective`.

    Returned so callers (the HTTP layer in task 15.1, tests, downstream
    Planning_Service modules that target this Objective) can correlate
    the created Objective Resource with its first Revision, its
    ``Addresses`` Relationship to the Decision, and the consequential
    audit row in one round-trip.

    Attributes:
        objective_id: The Objective Resource Identity (UUIDv7).
        objective_revision_id: The first Objective Revision Identity.
        statement: The persisted statement text (1..4000 chars).
        rationale: The persisted rationale text (or ``None``).
        target_decision_id: The Decision Identity the Objective
            addresses; copied byte-equivalent from the request input.
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Objective applies
            within.
        addresses_relationship_id: Identity of the single
            ``Addresses`` Relationship row inserted alongside the
            Revision (Requirement 2.3).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Objectives`` row, the
            ``Objective_Revisions`` row, the ``Addresses``
            Relationship row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            two on a single value.
    """

    objective_id: str
    objective_revision_id: str
    statement: str
    rationale: Optional[str]
    target_decision_id: str
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObjectiveService:
    """Persist Objective Resources, Revisions, and their ``Addresses`` links
    to authorized Slice 1 Decisions.

    Like :class:`walking_slice.knowledge.KnowledgeService`, this service
    is connection-scoped at call time: :meth:`create_objective` accepts
    the caller's :class:`sqlalchemy.engine.Connection` and writes
    inside the caller's transaction (AD-WS-5). The service instance
    therefore holds only the cross-request collaborators and can be
    shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.Objectives" declares it ``@dataclass(frozen=True)``
    — Slice 2 service instances are immutable container objects that
    bundle the Slice 1 collaborators for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Objectives``, ``Objective_Revisions``, ``Relationships``,
            and ``Audit_Records`` rows. The clock is consulted exactly
            once per write so every artifact of the transaction shares
            one timestamp.
        identity_service: Generates Objective Resource and Revision
            Identities and persists their ``Identifier_Registry``
            bindings (the bindings carry the Slice 2 ``resource_kind``
            tag per AD-WS-19).
        audit_log: Appends the consequential audit row (Requirement
            2.7) inside the caller's transaction.
        authorization_service: Evaluates ``create.objective`` authority
            per AD-WS-15 / Requirement 2.5; the deny path is the Slice
            1 separate-transaction Denial-Record pattern from
            :meth:`KnowledgeService.create_decision`.
        knowledge_service: Read-only consumer through
            :meth:`KnowledgeService.get_decision` for AD-WS-21. The
            Planning_Service does not query the Slice 1 ``Decisions``
            table directly; the access boundary remains the
            Knowledge_Service public API.
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
    knowledge_service: KnowledgeService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_objective(
        self,
        connection: Connection,
        *,
        statement: str,
        rationale: Optional[str],
        target_decision_id: str,
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateObjectiveResult:
        """Create an Objective Resource plus its first immutable Revision and
        ``Addresses`` Relationship to the target Decision.

        Per Requirements 2.1 through 2.7, AD-WS-9 (indistinguishable
        denial), AD-WS-15 (``create.objective`` → ``modify``), AD-WS-19
        (resource_kind tagged identifiers + append-only Slice 2
        tables), and AD-WS-21 (Decision resolution via
        Knowledge_Service):

        1. Optionally screen the original request body against the
           prohibited-attribute prefixes (Property 22 / Requirements
           12.1, 12.2, 13.1, 13.2, 13.5). This is the defensive
           boundary the HTTP route invokes when forwarding the raw
           request body; the typed kwargs themselves cannot carry a
           prohibited attribute.
        2. Input validation (Requirement 2.3 / 2.6) — every range and
           required-attribute check runs before any database read so a
           malformed request never touches identity service, the
           knowledge service, or the authorization service.
        3. Resolve the target Decision via
           :meth:`KnowledgeService.get_decision` (AD-WS-21). When the
           identifier does not resolve to any Decision, or resolves to
           a Decision whose ``outcome`` is not ``'Accept'``, raise
           :class:`ObjectiveDecisionNotResolvableError`. The check
           runs before authorization evaluation so the deny path
           never reveals whether a Decision exists for an
           unauthorized caller.
        4. Run the authorization evaluation on a *separate*
           transaction (the Slice 1 single-writer accommodation
           documented on :meth:`KnowledgeService.create_decision`).
           On ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`ObjectiveAuditFailureError` in place of
           :class:`ObjectiveAuthorizationError` so denial-and-audit
           divergence is unambiguous to the operator.
        5. On ``permit``, mint the Objective Resource and first
           Revision Identities and register them in
           ``Identifier_Registry`` (kind ``'resource'`` and ``'revision'``
           respectively, both carrying the Slice 2 ``resource_kind``
           tag per AD-WS-19) via
           :func:`walking_slice.planning._helpers._record_planning_resource`.
        6. INSERT the ``Objectives`` row (Resource header).
        7. INSERT the ``Objective_Revisions`` row carrying every
           Requirement 2.3 attribute.
        8. INSERT exactly one ``Relationships`` row with
           ``relationship_type='Addresses'``,
           ``source_kind='objective_revision'`` /
           ``source_id=objective_id`` /
           ``source_revision_id=objective_revision_id``,
           ``target_kind='decision'`` /
           ``target_id=target_decision_id`` /
           ``target_revision_id=NULL`` (a Decision is itself an
           Immutable Record with no revision), and
           ``semantic_role=NULL`` (Requirement 2.3, AD-WS-17 — the
           ``semantic_role`` column is reserved for Plan Review's
           ``'review'`` discriminator and is unused on this row).
        9. Append the consequential ``Audit_Records`` row with
           ``action_type='create.objective'``,
           ``target_id=objective_id``, and
           ``target_revision_id=objective_revision_id`` inside the
           same transaction (Requirement 2.7 / AD-WS-5).

        Rows are inserted in dependency order so a FK failure anywhere
        rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            statement: Objective statement of 1..4000 characters
                (Requirement 2.3).
            rationale: Objective rationale of 0..10000 characters, or
                ``None`` when no rationale is supplied. The empty
                string is a valid persisted value (length 0 satisfies
                the 0..10000 range); ``None`` is also valid and is
                persisted as SQL ``NULL``.
            target_decision_id: Identity of the target Decision
                Immutable Record (Requirement 2.2). Must resolve via
                :meth:`KnowledgeService.get_decision` and the
                resolved Decision's ``outcome`` must be ``'Accept'``.
            authoring_party_id: Identity of the authoring Party.
                Persisted on ``Objective_Revisions.authoring_party_id``
                and on the consequential audit row's
                ``actor_party_id``. The Slice 1 ``Parties`` foreign
                key is enforced by the database.
            applicable_scope: Scope identifier the Objective applies
                within. Persisted on
                ``Objective_Revisions.applicable_scope`` and passed as
                ``target.scope`` to :meth:`AuthorizationService.evaluate`
                so the wired role assignment must cover the same
                scope to permit the action.
            engine: Required for the deny path's separate-transaction
                Denial Record write so the row survives the caller's
                rollback (Requirement 7.6). The same engine is used to
                open a fresh transaction for the authorization
                evaluation itself (Slice 1 single-writer
                accommodation).
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
                mapping is screened against every prohibited
                attribute prefix (Property 22). The HTTP layer
                forwards the raw request body here; service-level
                callers (e.g., unit tests) may pass ``None`` to
                skip the screen since the typed kwargs themselves
                cannot carry a prohibited attribute.

        Returns:
            :class:`CreateObjectiveResult` carrying the persisted
            identifiers, attributes, the ``Addresses`` Relationship
            Identity, the recorded time, and the correlation
            identifier.

        Raises:
            ObjectiveValidationError: A required attribute is missing
                or a Requirement 2.3 range was violated, or the
                request body carried a prohibited execution /
                observed-outcome / produced-deliverable attribute.
            ObjectiveDecisionNotResolvableError: The target Decision
                Identity did not resolve, or the resolved Decision's
                outcome was not ``'Accept'`` (Requirement 2.4).
            ObjectiveAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 2.5). The Denial Record was appended
                successfully in a separate transaction (Requirement
                7.6).
            ObjectiveAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`ObjectiveAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 2.7 /
                Slice 1 Requirement 13.6.
        """
        # 1. Screen the original request body when the route layer has
        # forwarded it. The typed kwargs themselves cannot carry a
        # prohibited attribute (the signature does not declare any
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
                # each per Requirement 12.2 / 13.5.
                raise ObjectiveValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 2.3, 2.6) before any
        # database read or authorization side-effect.
        self._validate_statement(statement)
        self._validate_rationale(rationale)
        self._validate_required_strings(
            target_decision_id=target_decision_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
        )

        # 3. Resolve the target Decision through the Knowledge_Service
        # public read API (AD-WS-21). The lookup runs on the caller's
        # connection so it participates in the caller's transactional
        # view. Requirement 2.4 rejects both the unresolvable case and
        # the non-``Accept`` case.
        decision = self.knowledge_service.get_decision(
            connection, target_decision_id
        )
        if decision is None:
            raise ObjectiveDecisionNotResolvableError(
                target_decision_id=target_decision_id,
                failed_constraint="target_decision_not_resolvable",
                outcome=None,
            )
        if decision.outcome != _DECISION_OUTCOME_ACCEPT:
            raise ObjectiveDecisionNotResolvableError(
                target_decision_id=target_decision_id,
                failed_constraint="target_decision_outcome_not_accept",
                outcome=decision.outcome,
            )

        # 4. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Objectives row, the Objective_Revisions row, the Addresses
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
        # transaction (Slice 1 documented accommodation for SQLite's
        # single-writer model; the deny path opens *another* separate
        # transaction for the Denial Record write, and the caller's
        # transaction stays a reader until step 7 below). On
        # ``permit`` the evaluation row commits independently; on
        # ``deny`` the row rolls back with the evaluation transaction
        # and the durable record of the denial is the Denial Record
        # appended by :meth:`_persist_objective_denial`.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=authoring_party_id,
                action=_ACTION_CREATE_OBJECTIVE,
                target=TargetRef(
                    kind=_KIND_DECISION,
                    id=target_decision_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_objective_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_decision_id=target_decision_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise ObjectiveAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. Mint identifiers (AD-WS-2 / AD-WS-19). The Objective
        # Resource and its first Revision are bound to the same
        # content digest because the Resource has no separate
        # "natural content" — the digest is derived from the first
        # Revision's payload (the same pattern Slice 1's
        # :meth:`create_finding` uses for Finding Resource and first
        # Finding Revision).
        objective_id = str(self.identity_service.new_resource_id())
        objective_revision_id = str(self.identity_service.new_revision_id())
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "statement": statement,
                    "rationale": rationale,
                    "target_decision_id": target_decision_id,
                    "authoring_party_id": authoring_party_id,
                    "applicable_scope": applicable_scope,
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
            _RESOURCE_KIND_OBJECTIVE,
            objective_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_OBJECTIVE,
            recorded_time=recorded_time,
        )
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_OBJECTIVE_REVISION,
            objective_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_OBJECTIVE,
            recorded_time=recorded_time,
        )

        # 8. Insert the Resource header. ``created_at`` carries the
        # same recorded time as the first Revision (design
        # §"Persistence Invariants Summary" item 5) so the two rows'
        # timestamps are byte-equivalent.
        connection.execute(
            text(
                """
                INSERT INTO Objectives (objective_id, created_at)
                VALUES (:objective_id, :created_at)
                """
            ),
            {"objective_id": objective_id, "created_at": recorded_at},
        )

        # 9. Insert the first immutable Revision (Requirement 2.3).
        # ``parent_revision_id`` is NULL because this is the first
        # Revision; subsequent Revisions would link backwards via the
        # column (out of scope for Slice 2 — the create endpoint only
        # writes the first Revision).
        connection.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :objective_revision_id, :objective_id, NULL,
                    :statement, :rationale, :target_decision_id,
                    :authoring_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "objective_revision_id": objective_revision_id,
                "objective_id": objective_id,
                "statement": statement,
                "rationale": rationale,
                "target_decision_id": target_decision_id,
                "authoring_party_id": authoring_party_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 10. Insert the single ``Addresses`` Relationship row
        # (Requirement 2.3). ``semantic_role`` is NULL — the
        # additive AD-WS-17 column is reserved for Plan Review's
        # ``'review'`` discriminator and is not used here.
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
                "source_kind": _KIND_OBJECTIVE_REVISION,
                "source_id": objective_id,
                "source_revision_id": objective_revision_id,
                "target_kind": _KIND_DECISION,
                "target_id": target_decision_id,
                "target_revision_id": None,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 11. Append the consequential audit row (Requirement 2.7 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Objective,
        # Objective_Revisions, and Relationships rows together.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_ACTION_CREATE_OBJECTIVE,
            target_id=objective_id,
            target_revision_id=objective_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateObjectiveResult(
            objective_id=objective_id,
            objective_revision_id=objective_revision_id,
            statement=statement,
            rationale=rationale,
            target_decision_id=target_decision_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_statement(statement: Any) -> None:
        """Reject statements outside the Requirement 2.3 range.

        Empty or non-string statements surface as ``statement_missing``
        since the actionable next step is the same in both cases
        (supply a non-empty string). An over-long statement surfaces
        as ``statement_too_long``.
        """
        if statement is None or not isinstance(statement, str) or statement == "":
            raise ObjectiveValidationError(
                "statement is required and must be a non-empty string of "
                f"{_STATEMENT_MIN_CHARS}..{_STATEMENT_MAX_CHARS} characters; "
                "Requirement 2.3 / 2.6.",
                failed_constraint="statement_missing",
            )
        if len(statement) > _STATEMENT_MAX_CHARS:
            raise ObjectiveValidationError(
                f"statement length {len(statement)} exceeds the "
                f"{_STATEMENT_MAX_CHARS}-character limit imposed by "
                "Requirement 2.3.",
                failed_constraint="statement_too_long",
            )

    @staticmethod
    def _validate_rationale(rationale: Any) -> None:
        """Reject rationale outside the Requirement 2.3 range.

        Requirement 2.3 mandates 0..10000 characters. ``None`` is
        accepted (the column is NULLable) and persisted as SQL
        ``NULL``; the empty string is also accepted (length 0
        satisfies the 0 lower bound) and persisted as ``''``.
        """
        if rationale is None:
            return
        if not isinstance(rationale, str):
            raise ObjectiveValidationError(
                f"rationale must be a str or None; received "
                f"{type(rationale).__name__}.",
                failed_constraint="rationale_invalid_type",
            )
        if len(rationale) > _RATIONALE_MAX_CHARS:
            raise ObjectiveValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_RATIONALE_MAX_CHARS}-character limit imposed by "
                "Requirement 2.3.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_required_strings(
        *,
        target_decision_id: Any,
        authoring_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 2.6: "IF an Objective creation request omits
        the Objective statement, omits a target Decision Immutable
        Record Identity, names more than one target Decision
        Immutable Record Identity, or omits the applicable scope,
        THEN THE Planning_Service SHALL reject the action…". The
        statement check lives in :meth:`_validate_statement`; this
        validator covers the other three required strings plus
        ``authoring_party_id`` (which Requirement 2.5 implicitly
        requires — an unauthenticated request has no Party Identity
        to authorize against).
        """
        if not target_decision_id or not isinstance(target_decision_id, str):
            raise ObjectiveValidationError(
                "target_decision_id is required; Requirement 2.6 rejects "
                "Objectives missing the target Decision Identity.",
                failed_constraint="target_decision_id_missing",
            )
        if not authoring_party_id or not isinstance(authoring_party_id, str):
            raise ObjectiveValidationError(
                "authoring_party_id is required; Requirement 2.5 rejects "
                "unauthenticated Objective creation.",
                failed_constraint="authoring_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise ObjectiveValidationError(
                "applicable_scope is required; Requirement 2.6 rejects "
                "Objectives missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    # -- denial side-channel ----------------------------------------------

    def _persist_objective_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_decision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Objective attempt.

        Implements the Requirement 7.6 retry contract verbatim
        (mirroring
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
        - If attempt 4 also fails, :class:`ObjectiveAuditFailureError`
          is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_objective` raises
        :class:`ObjectiveAuthorizationError` (or this method raises
        :class:`ObjectiveAuditFailureError`). The Denial Record must
        therefore live outside that scope to survive (AD-WS-9 /
        Requirement 7.6).

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
                        attempted_action=_ACTION_CREATE_OBJECTIVE,
                        target_id=target_decision_id,
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

        raise ObjectiveAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the Slice 1 correlation-id and SHA-256 helpers so this
# module does not import private names from :mod:`walking_slice.knowledge`.
# The functions are intentionally identical to their Slice 1 siblings:
# correlation identifiers are non-domain values and the digest is opaque
# to :class:`Identifier_Registry` — both implementations could be replaced
# with shared utility functions in a future refactor without changing
# observable behavior.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Objective
    creation. They are not registered with :class:`IdentityService`
    because they do not name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Objective
    Resource Identity and the first Objective Revision Identity in
    ``Identifier_Registry``. Sharing one digest across both bindings
    mirrors the Slice 1 pattern in
    :meth:`walking_slice.knowledge.KnowledgeService.create_finding`
    and keeps the AD-WS-2 non-reuse invariant exercised at the same
    granularity for Resource and first-Revision identifiers.
    """
    return hashlib.sha256(content).hexdigest()
