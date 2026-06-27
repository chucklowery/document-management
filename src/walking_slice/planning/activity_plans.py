"""Planning_Service.ActivityPlans — Activity Plan Resources and the
``Addresses`` Relationship to a Project.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.ActivityPlans" — public dataclass surface, authority
  string (``create.activity_plan`` → ``modify`` per AD-WS-15), AD-WS-9
  separate-transaction Denial Record on deny, and the Activity Plan /
  Project identifier-set disjointness invariant enforced through the
  AD-WS-19 ``resource_kind`` tag on ``Identifier_Registry``.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction); Identifiers (every new
  identity is a UUIDv7 minted by :class:`IdentityService` and registered
  in ``Identifier_Registry`` with ``kind = 'resource'`` and
  ``resource_kind = 'activity_plan'``); Authorization (the action
  string ``create.activity_plan`` maps to the ``modify`` authority per
  AD-WS-15; the deny path uses the Slice 1 separate-transaction
  Denial-Record pattern reproduced from
  :class:`walking_slice.planning.projects.ProjectService`).
- AD-WS-15 — additive ``modify`` mapping for ``create.activity_plan``.
- AD-WS-19 — additive ``Identifier_Registry.resource_kind`` column the
  helper :func:`walking_slice.planning._helpers._record_planning_resource`
  populates so Requirement 4.5's identifier-set disjointness is
  inspectable at row level.

Activity Plans are SPECIAL within Slice 2: unlike every other Planning
Resource, they do not carry Revisions. Versioned planned content lives
on Plan Revisions; the Activity Plan is the *organizing* identity
under which Plan Revisions are grouped (design §"Planning_Service.ActivityPlans",
AD-WS-3). The schema is therefore a single header table
``Activity_Plans`` rather than the Resource + Revisions pair used by
Objectives, Intended Outcomes, Projects, and Deliverable Expectations.

Task scope (task 7.1)
=====================

This module implements :meth:`ActivityPlanService.create_activity_plan`:

1. Validate request inputs per Requirement 6.2 (title 1..200) and 6.3
   (rejection of missing or invalid attributes including authoring
   Party Identity and applicable scope).
2. Defensively reject any prohibited execution / observed-outcome /
   produced-deliverable attribute via
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   (Property 22) when the route layer forwards the raw request body.
3. Resolve the target Project Resource Identity through a single
   SELECT against the Slice 2 ``Projects`` table; reject when
   unresolvable (Requirement 6.3).
4. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (the Slice 1 single-writer accommodation); on a deny outcome,
   persist a Denial Record in another separate transaction with the
   Requirement 7.6 three-attempt exponential-backoff retry pattern,
   and raise :class:`ActivityPlanAuthorizationError` carrying the
   AD-WS-9 denial response fields (``reason_code``, ``correlation_id``).
5. On a permit outcome, mint the Activity Plan Resource Identity,
   register it in ``Identifier_Registry`` with the additive
   ``resource_kind`` tag ``'activity_plan'`` (AD-WS-19 — keeps the
   Activity Plan / Project identifier sets disjoint at row level per
   Requirement 4.5), INSERT the ``Activity_Plans`` row, INSERT the
   single ``Addresses`` Relationship from the Activity Plan to the
   target Project Resource (source_kind='activity_plan',
   target_kind='project' per the design §"Cross-Cutting Concerns"
   Transactionality contract), and append the consequential
   ``Audit_Records`` row (Requirement 6.5 / AD-WS-5) — all inside the
   caller's transaction so a failure anywhere rolls every row back.

Requirements satisfied
======================

    4.5 — Activity Plan Resource Identity is tagged with
          ``resource_kind = 'activity_plan'`` in
          ``Identifier_Registry``, keeping the Activity Plan Resource
          identifier set disjoint from the Project Resource
          identifier set at row granularity.
    6.1 — authorized Activity Plan creation produces exactly one
          Activity Plan Resource (no Revision — Activity Plans are
          headers; versioned content lives on Plan Revisions).
    6.2 — every Activity Plan records its title (1..200 chars), the
          parent Project Resource Identity, the authoring Party
          Identity, the applicable scope, and the recorded time in
          UTC with millisecond precision.
    6.3 — unresolvable target Projects, missing title, and missing
          applicable scope are rejected with no Activity Plan
          Resource created and a structured error identifying the
          missing or invalid attribute.
    6.4 — unauthorized requests are denied via
          :class:`AuthorizationService`; the Planning_Service
          declines to create any Activity Plan Resource and the
          Audit_Log appends a Denial Record conforming to AD-WS-9.
    6.5 — every successful Activity Plan creation appends one
          immutable consequential audit row in the same transaction.
    7.6 — the Denial Record append is retried up to three times with
          exponential backoff (0.01s, 0.02s, 0.04s); on total audit
          failure :class:`ActivityPlanAuditFailureError` is raised so
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
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    PlanningValidationError,
    _record_planning_resource,
    _reject_prohibited_attributes,
)


__all__ = [
    "ActivityPlanAuditFailureError",
    "ActivityPlanAuthorizationError",
    "ActivityPlanProjectNotResolvableError",
    "ActivityPlanService",
    "ActivityPlanValidationError",
    "CreateActivityPlanResult",
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


# ``create.activity_plan`` maps to the ``modify`` authority per
# AD-WS-15. The string is also the ``action_type`` recorded on the
# consequential audit row (Requirement 6.5) and on the
# separate-transaction Denial Record appended by
# :meth:`ActivityPlanService._persist_activity_plan_denial` so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_ACTIVITY_PLAN: Final[str] = "create.activity_plan"

# Relationship Type and source / target ``kind`` strings written to
# the single ``Addresses`` Relationship row inserted alongside the
# Activity Plan. Constants ensure the strings cannot drift between
# this module, the Provenance Navigator backlink algorithm that
# consumes ``Relationships`` rows verbatim, and the planning
# provenance walk that descends Plan Approval → Plan Revision →
# Activity Plan → Project → Objective (design §"Planning_Service.PlanApprovals").
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_ACTIVITY_PLAN: Final[str] = "activity_plan"
_KIND_PROJECT: Final[str] = "project"

# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tags (Slice 2 additive
# enumeration per AD-WS-19). Activity Plans bind only one tag —
# there is no Revision (AD-WS-3, design §"Planning_Service.ActivityPlans").
# This is the row-level discriminator that satisfies Requirement 4.5
# (Activity Plan identifier set disjoint from Project identifier set).
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_RESOURCE_KIND_ACTIVITY_PLAN: Final[str] = "activity_plan"

# Validation limits per Requirement 6.2 ("Activity Plan title of 1 to
# 200 characters"). The schema CHECK constraint on ``Activity_Plans``
# enforces the same values; centralizing them here surfaces precise,
# structured constraint names through
# :class:`ActivityPlanValidationError`.
_TITLE_MIN_CHARS: Final[int] = 1
_TITLE_MAX_CHARS: Final[int] = 200

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 7.6). Three retries after the
# initial attempt for a total of four attempts. The sequence is
# byte-equivalent to the one in
# :class:`walking_slice.planning.projects.ProjectService` so every
# Planning_Service module presents identical denial-side timing
# (which Property 18 — Indistinguishable denial — relies on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ActivityPlanValidationError(ValueError):
    """Raised when an Activity Plan submission fails Requirement 6.2 / 6.3
    validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"title_missing"`` (empty or non-string title),
            ``"title_too_long"``,
            ``"target_project_id_missing"``,
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


class ActivityPlanProjectNotResolvableError(LookupError):
    """Raised when the target Project Identity does not resolve.

    Requirement 6.3 requires the target Project Resource Identity to
    resolve to an existing Project Resource. The check runs before
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


class ActivityPlanAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies an Activity Plan attempt.

    Mirrors
    :class:`walking_slice.planning.projects.ProjectAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 6.4 / 10.x). The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 10 forbids leaking authorized Party identities, target
    existence, or role assignment details beyond the requesting
    Party's view authority through the denial response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Activity Plan creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class ActivityPlanAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 7.6).

    Mirrors
    :class:`walking_slice.planning.projects.ProjectAuditFailureError`.
    On total audit-append failure the exception is raised *in place
    of* :class:`ActivityPlanAuthorizationError` — denial and audit have
    silently diverged and the operator must be told. The caller's
    transaction still rolls back so no ``Activity_Plans`` row,
    ``Addresses`` Relationship, or consequential audit row is
    persisted.

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
            f"Denial Record append for a denied Activity Plan failed after "
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
class CreateActivityPlanResult:
    """Result of :meth:`ActivityPlanService.create_activity_plan`.

    Returned so callers (the HTTP layer in task 15.1, tests, and the
    downstream Plan Revision service that targets this Activity Plan)
    can correlate the created Activity Plan Resource with its
    ``Addresses`` Relationship to the Project and the consequential
    audit row in one round-trip.

    Attributes:
        activity_plan_id: The Activity Plan Resource Identity (UUIDv7).
        title: The persisted Activity Plan title (1..200 chars).
        target_project_id: The Project Identity the Activity Plan
            addresses; copied byte-equivalent from the request input.
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Activity Plan applies
            within.
        addresses_relationship_id: Identity of the single
            ``Addresses`` Relationship row inserted alongside the
            Activity Plan (design §"Cross-Cutting Concerns" —
            Transactionality).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Activity_Plans`` row, the ``Addresses``
            Relationship row, and the consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            two on a single value.
    """

    activity_plan_id: str
    title: str
    target_project_id: str
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActivityPlanService:
    """Persist Activity Plan Resources and their ``Addresses`` links to
    authorized Projects.

    Like :class:`walking_slice.planning.projects.ProjectService`, this
    service is connection-scoped at call time:
    :meth:`create_activity_plan` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (AD-WS-5). The service instance therefore
    holds only the cross-request collaborators and can be shared
    across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.ActivityPlans" declares it
    ``@dataclass(frozen=True)`` — Slice 2 service instances are
    immutable container objects that bundle the Slice 1 collaborators
    for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Activity_Plans``, ``Relationships``, and ``Audit_Records``
            rows. The clock is consulted exactly once per write so
            every artifact of the transaction shares one timestamp.
        identity_service: Generates the Activity Plan Resource
            Identity and persists its ``Identifier_Registry`` binding
            (the binding carries the Slice 2 ``resource_kind = 'activity_plan'``
            tag per AD-WS-19 — making the Activity Plan identifier set
            inspectably disjoint from the Project identifier set per
            Requirement 4.5).
        audit_log: Appends the consequential audit row (Requirement
            6.5) inside the caller's transaction.
        authorization_service: Evaluates ``create.activity_plan``
            authority per AD-WS-15 / Requirement 6.4; the deny path
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

    def create_activity_plan(
        self,
        connection: Connection,
        *,
        target_project_id: str,
        title: str,
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateActivityPlanResult:
        """Create an Activity Plan Resource and its ``Addresses``
        Relationship to the target Project.

        Per Requirements 6.1 through 6.5, AD-WS-9 (indistinguishable
        denial), AD-WS-15 (``create.activity_plan`` → ``modify``), and
        AD-WS-19 (resource_kind tagged identifiers + append-only
        Slice 2 tables):

        1. Optionally screen the original request body against the
           prohibited-attribute prefixes (Property 22 / Requirements
           12.1, 12.2, 13.1, 13.2, 13.5).
        2. Input validation (Requirement 6.2 / 6.3) — every range and
           required-attribute check runs before any database read so
           a malformed request never touches identity service, the
           ``Projects`` lookup, or the authorization service.
        3. Resolve the target Project via a single SELECT against
           ``Projects``. When the identifier does not resolve, raise
           :class:`ActivityPlanProjectNotResolvableError`. The check
           runs before authorization evaluation so the deny path
           never reveals whether a Project exists for an unauthorized
           caller.
        4. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`ActivityPlanAuditFailureError` in place of
           :class:`ActivityPlanAuthorizationError`.
        5. On ``permit``, mint the Activity Plan Resource Identity
           and register it in ``Identifier_Registry`` (kind
           ``'resource'``, carrying the Slice 2
           ``resource_kind = 'activity_plan'`` tag per AD-WS-19) via
           :func:`walking_slice.planning._helpers._record_planning_resource`
           — the tag is what makes Requirement 4.5's identifier-set
           disjointness inspectable at row granularity.
        6. INSERT the ``Activity_Plans`` row (Resource header — the
           single-table form, Activity Plans have no Revisions per
           AD-WS-3 / design §"Planning_Service.ActivityPlans").
        7. INSERT exactly one ``Relationships`` row with
           ``relationship_type='Addresses'``,
           ``source_kind='activity_plan'`` /
           ``source_id=activity_plan_id`` /
           ``source_revision_id=NULL``,
           ``target_kind='project'`` /
           ``target_id=target_project_id`` /
           ``target_revision_id=NULL`` (the Addresses Relationship
           binds the Activity Plan Resource to the Project Resource;
           neither side carries a Revision in this link — Activity
           Plans have none, and the Project Revision identity is
           not part of the Activity-Plan → Project link), and
           ``semantic_role=NULL`` (the AD-WS-17 ``semantic_role``
           column is reserved for Plan Review's ``'review'``
           discriminator and is unused on this row).
        8. Append the consequential ``Audit_Records`` row with
           ``action_type='create.activity_plan'`` and
           ``target_id=activity_plan_id`` inside the same
           transaction (Requirement 6.5 / AD-WS-5). The Activity
           Plan has no Revision so ``target_revision_id`` is
           ``None``.

        Rows are inserted in dependency order so a FK failure
        anywhere rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_project_id: Identity of the target Project
                Resource (Requirement 6.2). Must resolve to an
                existing row in ``Projects``.
            title: Activity Plan title of 1..200 characters
                (Requirement 6.2).
            authoring_party_id: Identity of the authoring Party.
                Persisted on ``Activity_Plans.authoring_party_id``
                and on the consequential audit row's
                ``actor_party_id``. The Slice 1 ``Parties`` foreign
                key is enforced by the database.
            applicable_scope: Scope identifier the Activity Plan
                applies within. Persisted on
                ``Activity_Plans.applicable_scope`` and passed as
                ``target.scope`` to :meth:`AuthorizationService.evaluate`
                so the wired role assignment must cover the same
                scope to permit the action.
            engine: Required for the deny path's separate-transaction
                Denial Record write so the row survives the caller's
                rollback (Requirement 7.6). The same engine is used
                to open a fresh transaction for the authorization
                evaluation itself (Slice 1 single-writer
                accommodation).
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
            :class:`CreateActivityPlanResult` carrying the persisted
            Activity Plan Identity, the title, the target Project
            Identity, the ``Addresses`` Relationship Identity, the
            recorded time, and the correlation identifier.

        Raises:
            ActivityPlanValidationError: A required attribute is
                missing or a Requirement 6.2 range was violated, or
                the request body carried a prohibited execution /
                observed-outcome / produced-deliverable attribute.
            ActivityPlanProjectNotResolvableError: The target
                Project Identity did not resolve to an existing
                Project Resource (Requirement 6.3).
            ActivityPlanAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 6.4). The Denial Record was appended
                successfully in a separate transaction (Requirement
                7.6).
            ActivityPlanAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`ActivityPlanAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare
                for UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 6.5 /
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
                raise ActivityPlanValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 6.2, 6.3) before any
        # database read or authorization side-effect.
        self._validate_title(title)
        self._validate_required_strings(
            target_project_id=target_project_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
        )

        # 3. Resolve the target Project Resource Identity through a
        # single SELECT on ``Projects``. The lookup runs on the
        # caller's connection so it participates in the caller's
        # transactional view. Requirement 6.3 rejects the
        # unresolvable case.
        resolved = connection.execute(
            text(
                "SELECT project_id FROM Projects "
                "WHERE project_id = :project_id"
            ),
            {"project_id": target_project_id},
        ).scalar_one_or_none()
        if resolved is None:
            raise ActivityPlanProjectNotResolvableError(
                target_project_id=target_project_id,
            )

        # 4. Shared clock reading (design §"Cross-Cutting Concerns"
        # — *Transactionality*). The authorization evaluation row,
        # the Activity_Plans row, the Addresses Relationship row,
        # and the consequential audit row all share this timestamp;
        # the optional ``evaluation_at`` parameter changes only
        # *when* authority is evaluated *as of*, not the recorded
        # time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 5. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 documented accommodation for
        # SQLite's single-writer model; the deny path opens
        # *another* separate transaction for the Denial Record
        # write, and the caller's transaction stays a reader until
        # step 6 below). On ``permit`` the evaluation row commits
        # independently; on ``deny`` the row rolls back with the
        # evaluation transaction and the durable record of the
        # denial is the Denial Record appended by
        # :meth:`_persist_activity_plan_denial`.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=authoring_party_id,
                action=_ACTION_CREATE_ACTIVITY_PLAN,
                target=TargetRef(
                    kind=_KIND_PROJECT,
                    id=target_project_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_activity_plan_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_project_id=target_project_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise ActivityPlanAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. Mint the Activity Plan Resource Identity (AD-WS-2 /
        # AD-WS-19). Activity Plans have no Revision (AD-WS-3 /
        # design §"Planning_Service.ActivityPlans") so only one
        # identifier is minted — distinct from the Projects pattern
        # which mints both a Resource and a first Revision identity.
        activity_plan_id = str(self.identity_service.new_resource_id())
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "title": title,
                    "target_project_id": target_project_id,
                    "authoring_party_id": authoring_party_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 7. Register the identifier in ``Identifier_Registry``
        # carrying the AD-WS-19 ``resource_kind = 'activity_plan'``
        # tag. This is the row-level discriminator that makes
        # Requirement 4.5's identifier-set disjointness from Project
        # identifiers inspectable: a global UNIQUE(identifier) plus a
        # ``resource_kind`` tag per Slice 2 Resource kind.
        #
        # The helper delegates to
        # :meth:`IdentityService.reject_if_duplicate` so the Slice 1
        # identifier-conflict Denial Record pathway fires on any
        # collision; on success the helper INSERTs one row inside
        # the caller's transaction.
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_ACTIVITY_PLAN,
            activity_plan_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_ACTIVITY_PLAN,
            recorded_time=recorded_time,
        )

        # 8. Insert the Activity Plan row. There is no separate
        # Resource-header / Revision split — the design specifies a
        # single header table (design §"Planning_Service.ActivityPlans"
        # / AD-WS-3). Every Requirement 6.2 attribute (title, parent
        # Project Identity, authoring Party Identity, applicable
        # scope, recorded time) lands on this row.
        connection.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :activity_plan_id, :target_project_id, :title,
                    :authoring_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "activity_plan_id": activity_plan_id,
                "target_project_id": target_project_id,
                "title": title,
                "authoring_party_id": authoring_party_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 9. Insert the single ``Addresses`` Relationship row
        # binding the Activity Plan to its parent Project (design
        # §"Cross-Cutting Concerns" — Transactionality: every
        # Planning_Service consequential write inserts the
        # ``Addresses`` or ``Relates To`` Relationship row alongside
        # the domain rows). ``source_revision_id`` and
        # ``target_revision_id`` are both NULL — Activity Plans have
        # no Revision and the Activity-Plan → Project link binds at
        # the Resource level. ``semantic_role`` is NULL — the
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
                "source_kind": _KIND_ACTIVITY_PLAN,
                "source_id": activity_plan_id,
                "source_revision_id": None,
                "target_kind": _KIND_PROJECT,
                "target_id": target_project_id,
                "target_revision_id": None,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 10. Append the consequential audit row (Requirement 6.5 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Activity_Plans, and
        # Relationships rows together. ``target_revision_id`` is
        # ``None`` because Activity Plans have no Revision; the
        # audit row points at the Resource Identity only.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_ACTION_CREATE_ACTIVITY_PLAN,
            target_id=activity_plan_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateActivityPlanResult(
            activity_plan_id=activity_plan_id,
            title=title,
            target_project_id=target_project_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_title(title: Any) -> None:
        """Reject Activity Plan title outside the Requirement 6.2 range.

        Empty or non-string titles surface as ``title_missing`` since
        the actionable next step is the same in both cases (supply a
        non-empty string). An over-long title surfaces as
        ``title_too_long``.
        """
        if title is None or not isinstance(title, str) or title == "":
            raise ActivityPlanValidationError(
                "title is required and must be a non-empty string of "
                f"{_TITLE_MIN_CHARS}..{_TITLE_MAX_CHARS} characters; "
                "Requirement 6.2 / 6.3.",
                failed_constraint="title_missing",
            )
        if len(title) > _TITLE_MAX_CHARS:
            raise ActivityPlanValidationError(
                f"title length {len(title)} exceeds the "
                f"{_TITLE_MAX_CHARS}-character limit imposed by "
                "Requirement 6.2.",
                failed_constraint="title_too_long",
            )

    @staticmethod
    def _validate_required_strings(
        *,
        target_project_id: Any,
        authoring_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 6.3: an Activity Plan creation request that
        names a target Project Identity that does not resolve, omits
        the Activity Plan title, or omits the applicable scope is
        rejected. The title check lives in :meth:`_validate_title`;
        this validator covers the other required strings plus
        ``authoring_party_id`` (which Requirement 6.4 implicitly
        requires — an unauthenticated request has no Party Identity
        to authorize against).
        """
        if not target_project_id or not isinstance(target_project_id, str):
            raise ActivityPlanValidationError(
                "target_project_id is required; Requirement 6.3 rejects "
                "Activity Plans missing the target Project Identity.",
                failed_constraint="target_project_id_missing",
            )
        if not authoring_party_id or not isinstance(authoring_party_id, str):
            raise ActivityPlanValidationError(
                "authoring_party_id is required; Requirement 6.4 rejects "
                "unauthenticated Activity Plan creation.",
                failed_constraint="authoring_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise ActivityPlanValidationError(
                "applicable_scope is required; Requirement 6.3 rejects "
                "Activity Plans missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    # -- denial side-channel ----------------------------------------------

    def _persist_activity_plan_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_project_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Activity Plan attempt.

        Implements the Requirement 7.6 retry contract verbatim
        (mirroring
        :meth:`walking_slice.planning.projects.ProjectService._persist_project_denial`):
        each attempt opens a *new* :meth:`Engine.begin` transaction
        (so a previous attempt's rollback does not poison this one),
        tries :meth:`AuditLog.append_denial`, and either returns on
        success or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails, :class:`ActivityPlanAuditFailureError`
          is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_activity_plan` raises
        :class:`ActivityPlanAuthorizationError` (or this method
        raises :class:`ActivityPlanAuditFailureError`). The Denial
        Record must therefore live outside that scope to survive
        (AD-WS-9 / Requirement 7.6).

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
                        attempted_action=_ACTION_CREATE_ACTIVITY_PLAN,
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

        raise ActivityPlanAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this module
# does not import private names from sibling planning modules. The
# functions are intentionally identical to their Projects siblings:
# correlation identifiers are non-domain values and the digest is
# opaque to :class:`Identifier_Registry` — both implementations could
# be replaced with shared utility functions in a future refactor
# without changing observable behavior.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Activity
    Plan creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Activity Plan
    Resource Identity in ``Identifier_Registry``. Activity Plans
    have no Revision so this digest is bound only once per Activity
    Plan creation — distinct from the Projects pattern where the
    same digest is shared between the Resource and first-Revision
    bindings.
    """
    return hashlib.sha256(content).hexdigest()
