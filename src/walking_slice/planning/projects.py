"""Planning_Service.Projects — Project Resources, Revisions, and the
``Addresses`` Relationship to an Objective.

Design reference
================

``.kiro/specs/second-walking-slice/design.md``:

- §"Planning_Service.Projects" — public dataclass surface, authority
  string (``create.project`` → ``modify``), AD-WS-9 separate-transaction
  Denial Record on deny, and the Project / Activity Plan identifier-set
  disjointness invariant enforced through the AD-WS-19 ``resource_kind``
  tag on ``Identifier_Registry``.
- §"Cross-Cutting Concerns" — Transactionality (single recorded time
  shared by every row in the transaction), Identifiers (every new identity
  is a UUIDv7 minted by :class:`IdentityService` and registered in
  ``Identifier_Registry`` with ``kind ∈ {'resource', 'revision'}`` and
  ``resource_kind`` set to ``'project'`` or ``'project_revision'``),
  Authorization (the action string ``create.project`` maps to the
  ``modify`` authority per AD-WS-15; the deny path uses the Slice 1
  separate-transaction Denial-Record pattern reproduced from
  :class:`walking_slice.planning.objectives.ObjectiveService`).
- AD-WS-15 — additive ``modify`` mapping for ``create.project``.
- AD-WS-19 — additive ``Identifier_Registry.resource_kind`` column the
  helper :func:`walking_slice.planning._helpers._record_planning_resource`
  populates so Requirement 4.5's identifier-set disjointness is
  inspectable at row level.

Task scope (task 5.1)
=====================

This module implements :meth:`ProjectService.create_project`:

1. Validate request inputs per Requirement 4.2 (name 1..200, summary
   optional 0..4000, planned dates as ISO-8601 calendar dates with
   ``planned_start_date <= planned_end_date``) and Requirement 4.3
   (rejection of missing or invalid attributes; rejection of malformed
   dates and an inverted date range).
2. Defensively reject any prohibited execution / observed-outcome /
   produced-deliverable attribute via
   :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
   (Property 22) when the route layer forwards the raw request body.
3. Resolve the target Objective Resource Identity through a single
   SELECT against the Slice 2 ``Objectives`` table; reject when
   unresolvable (Requirement 4.3).
4. Evaluate the authorization decision via
   :meth:`AuthorizationService.evaluate` on a *separate* transaction
   (the Slice 1 single-writer accommodation); on a deny outcome,
   persist a Denial Record in another separate transaction with the
   Requirement 7.6 three-attempt exponential-backoff retry pattern, and
   raise :class:`ProjectAuthorizationError` carrying the AD-WS-9 denial
   response fields (``reason_code``, ``correlation_id``).
5. On a permit outcome, mint the Project Resource and first Project
   Revision Identities, register both in ``Identifier_Registry`` with
   their additive ``resource_kind`` tags ``'project'`` and
   ``'project_revision'`` (AD-WS-19 — keeps the Project / Activity Plan
   identifier sets disjoint at row level per Requirement 4.5),
   INSERT the ``Projects`` and ``Project_Revisions`` rows, INSERT the
   single ``Addresses`` Relationship from the Project Revision to the
   target Objective Resource (Requirement 4.2), and append the
   consequential ``Audit_Records`` row (Requirement 4.6 / AD-WS-5) —
   all inside the caller's transaction so a failure anywhere rolls
   every row back.

Requirements satisfied
======================

    4.1 — authorized Project creation produces one Project Resource
          and one initial immutable Project Revision.
    4.2 — every Project Revision records name (1..200), summary
          (0..4000 or NULL), planned start date and planned end date as
          ISO-8601 calendar dates with start <= end, authoring Party
          Identity, applicable scope, recorded time (UTC ms-precision),
          and the ``Addresses`` Relationship to the target Objective.
    4.3 — unresolvable target Objectives, missing name, planned start
          later than planned end, and missing applicable scope are
          rejected with no Resource or Revision created and a
          structured error identifying the missing or invalid
          attribute.
    4.4 — unauthorized requests are denied via
          :class:`AuthorizationService`; the Planning_Service declines
          to create any Resource or Revision and the Audit_Log
          appends a Denial Record conforming to AD-WS-9.
    4.5 — Project Resource Identity is tagged with
          ``resource_kind = 'project'`` (and the first Revision with
          ``resource_kind = 'project_revision'``) in
          ``Identifier_Registry``, keeping the Project Resource
          identifier set disjoint from the Activity Plan Resource
          identifier set at row granularity.
    4.6 — every successful Project Revision insertion appends one
          immutable consequential audit row in the same transaction.
    7.6 — the Denial Record append is retried up to three times with
          exponential backoff (0.01s, 0.02s, 0.04s); on total audit
          failure :class:`ProjectAuditFailureError` is raised so
          denial and audit cannot silently diverge.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid_utils
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
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
    "CreateProjectResult",
    "ProjectAuditFailureError",
    "ProjectAuthorizationError",
    "ProjectObjectiveNotResolvableError",
    "ProjectService",
    "ProjectValidationError",
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


# ``create.project`` maps to the ``modify`` authority per AD-WS-15. The
# string is also the ``action_type`` recorded on the consequential audit
# row (Requirement 4.6) and on the separate-transaction Denial Record
# appended by :meth:`ProjectService._persist_project_denial` so audit
# consumers can correlate denial rows with the action a Party was
# attempting.
_ACTION_CREATE_PROJECT: Final[str] = "create.project"

# Relationship Type and source/target ``kind`` strings written to
# ``Relationships`` rows. Constants ensure the strings cannot drift
# between this module, the Objectives module that defines the same
# ``Addresses`` semantics for the Objective→Decision link, and the
# Slice 1 backlink algorithm that consumes ``Relationships`` rows
# verbatim.
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"
_KIND_PROJECT_REVISION: Final[str] = "project_revision"
_KIND_OBJECTIVE: Final[str] = "objective"

# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Planning_Service ``resource_kind`` tags (Slice 2 additive enumeration
# per AD-WS-19). The two ``resource_kind`` values bind the Project
# Resource identifier set disjoint from the Activity Plan Resource
# identifier set (Requirement 4.5).
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_PROJECT: Final[str] = "project"
_RESOURCE_KIND_PROJECT_REVISION: Final[str] = "project_revision"

# Validation limits per Requirement 4.2 ("Project name of 1 to 200
# characters" and "Project summary of 0 to 4,000 characters"). The
# schema CHECK constraints on ``Project_Revisions`` enforce the same
# values; centralizing them here surfaces precise, structured
# constraint names through :class:`ProjectValidationError`.
_NAME_MIN_CHARS: Final[int] = 1
_NAME_MAX_CHARS: Final[int] = 200
_SUMMARY_MIN_CHARS: Final[int] = 0
_SUMMARY_MAX_CHARS: Final[int] = 4_000

# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 7.6). Three retries after the
# initial attempt for a total of four attempts. The sequence is
# byte-equivalent to the one in
# :class:`walking_slice.planning.objectives.ObjectiveService` so every
# Planning_Service module presents identical denial-side timing
# (which Property 18 — Indistinguishable denial — relies on).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ProjectValidationError(ValueError):
    """Raised when a Project submission fails Requirement 4.2 / 4.3 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"name_missing"`` (empty or non-string name),
            ``"name_too_long"``,
            ``"summary_too_long"``,
            ``"summary_invalid_type"`` (non-string and non-None),
            ``"target_objective_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"planned_start_date_invalid_type"``,
            ``"planned_end_date_invalid_type"``,
            ``"planned_date_range_inverted"`` (start > end),
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


class ProjectObjectiveNotResolvableError(LookupError):
    """Raised when the target Objective Identity does not resolve.

    Requirement 4.3 requires the target Objective Resource Identity to
    resolve to an existing Objective Resource. The check runs before
    authorization evaluation so the deny path never reveals whether an
    Objective exists for an unauthorized caller.

    Attributes:
        target_objective_id: The Objective Identity the caller supplied.
        failed_constraint: ``"target_objective_not_resolvable"`` when no
            ``Objectives`` row matched the identifier.
    """

    def __init__(
        self,
        *,
        target_objective_id: str,
        failed_constraint: str = "target_objective_not_resolvable",
    ) -> None:
        super().__init__(
            f"Target Objective {target_objective_id!r} did not resolve to an "
            f"existing Objective Resource "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.target_objective_id = target_objective_id
        self.failed_constraint = failed_constraint


class ProjectAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Project attempt.

    Mirrors
    :class:`walking_slice.planning.objectives.ObjectiveAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9
    indistinguishable denial response shape ``{generic_denial_indicator,
    reason_code, correlation_id}`` (Requirement 4.4 / 10.x). The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 10 forbids leaking authorized Party identities,
    Objective contents, role assignment details, or target existence
    beyond the requesting Party's view authority through the denial
    response.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Project creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class ProjectAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails (Requirement 7.6).

    Mirrors
    :class:`walking_slice.planning.objectives.ObjectiveAuditFailureError`.
    On total audit-append failure the exception is raised *in place of*
    :class:`ProjectAuthorizationError` — denial and audit have silently
    diverged and the operator must be told. The caller's transaction
    still rolls back so no Project row, Project Revision row, Addresses
    Relationship, or consequential audit row is persisted.

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
            f"Denial Record append for a denied Project failed after "
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
class CreateProjectResult:
    """Result of :meth:`ProjectService.create_project`.

    Returned so callers (the HTTP layer in task 15.1, tests, downstream
    Planning_Service modules that target this Project) can correlate
    the created Project Resource with its first Revision, its
    ``Addresses`` Relationship to the Objective, and the consequential
    audit row in one round-trip.

    Attributes:
        project_id: The Project Resource Identity (UUIDv7).
        project_revision_id: The first Project Revision Identity.
        name: The persisted Project name (1..200 chars).
        summary: The persisted Project summary (or ``None``).
        target_objective_id: The Objective Identity the Project
            addresses; copied byte-equivalent from the request input.
        planned_start_date: The persisted planned start date as an
            ISO-8601 calendar date string (``YYYY-MM-DD``).
        planned_end_date: The persisted planned end date as an ISO-8601
            calendar date string (``YYYY-MM-DD``).
        authoring_party_id: Identity of the authoring Party.
        applicable_scope: Scope identifier the Project applies within.
        addresses_relationship_id: Identity of the single ``Addresses``
            Relationship row inserted alongside the Revision
            (Requirement 4.2).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the ``Projects`` row, the ``Project_Revisions``
            row, the ``Addresses`` Relationship row, and the
            consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join the
            two on a single value.
    """

    project_id: str
    project_revision_id: str
    name: str
    summary: Optional[str]
    target_objective_id: str
    planned_start_date: str
    planned_end_date: str
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectService:
    """Persist Project Resources, Revisions, and their ``Addresses`` links
    to authorized Objectives.

    Like :class:`walking_slice.planning.objectives.ObjectiveService`,
    this service is connection-scoped at call time:
    :meth:`create_project` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the
    caller's transaction (AD-WS-5). The service instance therefore
    holds only the cross-request collaborators and can be shared
    across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/second-walking-slice/design.md``
    §"Planning_Service.Projects" declares it ``@dataclass(frozen=True)``
    — Slice 2 service instances are immutable container objects that
    bundle the Slice 1 collaborators for the Planning_Service.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Projects``, ``Project_Revisions``, ``Relationships``,
            and ``Audit_Records`` rows. The clock is consulted exactly
            once per write so every artifact of the transaction shares
            one timestamp.
        identity_service: Generates Project Resource and Revision
            Identities and persists their ``Identifier_Registry``
            bindings (the bindings carry the Slice 2 ``resource_kind``
            tag per AD-WS-19 — ``'project'`` / ``'project_revision'``
            — making the Project identifier set inspectably disjoint
            from the Activity Plan identifier set per Requirement
            4.5).
        audit_log: Appends the consequential audit row (Requirement
            4.6) inside the caller's transaction.
        authorization_service: Evaluates ``create.project`` authority
            per AD-WS-15 / Requirement 4.4; the deny path is the
            Slice 1 separate-transaction Denial-Record pattern.
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

    def create_project(
        self,
        connection: Connection,
        *,
        target_objective_id: str,
        name: str,
        summary: Optional[str],
        planned_start_date: date,
        planned_end_date: date,
        authoring_party_id: str,
        applicable_scope: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateProjectResult:
        """Create a Project Resource plus its first immutable Revision and
        ``Addresses`` Relationship to the target Objective.

        Per Requirements 4.1 through 4.6, AD-WS-9 (indistinguishable
        denial), AD-WS-15 (``create.project`` → ``modify``), and
        AD-WS-19 (resource_kind tagged identifiers + append-only Slice
        2 tables):

        1. Optionally screen the original request body against the
           prohibited-attribute prefixes (Property 22 / Requirements
           12.1, 12.2, 13.1, 13.2, 13.5).
        2. Input validation (Requirement 4.2 / 4.3) — every range and
           required-attribute check runs before any database read so a
           malformed request never touches identity service, the
           ``Objectives`` lookup, or the authorization service.
        3. Resolve the target Objective via a single SELECT against
           ``Objectives``. When the identifier does not resolve, raise
           :class:`ProjectObjectiveNotResolvableError`. The check runs
           before authorization evaluation so the deny path never
           reveals whether an Objective exists for an unauthorized
           caller.
        4. Run the authorization evaluation on a *separate*
           transaction (Slice 1 single-writer accommodation). On
           ``deny``, append the Denial Record in another separate
           transaction with the Requirement 7.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`ProjectAuditFailureError` in place of
           :class:`ProjectAuthorizationError`.
        5. On ``permit``, mint the Project Resource and first Revision
           Identities and register them in ``Identifier_Registry``
           (kind ``'resource'`` and ``'revision'`` respectively, both
           carrying the Slice 2 ``resource_kind`` tag per AD-WS-19) via
           :func:`walking_slice.planning._helpers._record_planning_resource`
           — the tag is what makes Requirement 4.5's identifier-set
           disjointness inspectable at row granularity.
        6. INSERT the ``Projects`` row (Resource header).
        7. INSERT the ``Project_Revisions`` row carrying every
           Requirement 4.2 attribute.
        8. INSERT exactly one ``Relationships`` row with
           ``relationship_type='Addresses'``,
           ``source_kind='project_revision'`` /
           ``source_id=project_id`` /
           ``source_revision_id=project_revision_id``,
           ``target_kind='objective'`` /
           ``target_id=target_objective_id`` /
           ``target_revision_id=NULL`` (the Addresses Relationship
           binds the Project Revision to the Objective Resource;
           Objective Revision identity is not part of the link), and
           ``semantic_role=NULL`` (Requirement 4.2, AD-WS-17 — the
           ``semantic_role`` column is reserved for Plan Review's
           ``'review'`` discriminator and is unused on this row).
        9. Append the consequential ``Audit_Records`` row with
           ``action_type='create.project'``,
           ``target_id=project_id``, and
           ``target_revision_id=project_revision_id`` inside the
           same transaction (Requirement 4.6 / AD-WS-5).

        Rows are inserted in dependency order so a FK failure anywhere
        rolls back the whole transaction.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_objective_id: Identity of the target Objective
                Resource (Requirement 4.2). Must resolve to an
                existing row in ``Objectives``.
            name: Project name of 1..200 characters (Requirement 4.2).
            summary: Project summary of 0..4000 characters, or
                ``None`` when no summary is supplied. The empty
                string is a valid persisted value (length 0 satisfies
                the 0..4000 range); ``None`` is also valid and is
                persisted as SQL ``NULL``.
            planned_start_date: Planned start date as a
                :class:`datetime.date` instance. Must be a proper
                ``date`` value with ``planned_start_date <=
                planned_end_date`` (Requirement 4.2 / 4.3). The
                value is persisted as an ISO-8601 calendar date
                string (``YYYY-MM-DD``).
            planned_end_date: Planned end date as a
                :class:`datetime.date` instance. See
                ``planned_start_date`` for the ordering constraint.
            authoring_party_id: Identity of the authoring Party.
                Persisted on ``Project_Revisions.authoring_party_id``
                and on the consequential audit row's
                ``actor_party_id``. The Slice 1 ``Parties`` foreign
                key is enforced by the database.
            applicable_scope: Scope identifier the Project applies
                within. Persisted on
                ``Project_Revisions.applicable_scope`` and passed as
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
            :class:`CreateProjectResult` carrying the persisted
            identifiers, attributes, the ``Addresses`` Relationship
            Identity, the recorded time, and the correlation
            identifier.

        Raises:
            ProjectValidationError: A required attribute is missing
                or a Requirement 4.2 range was violated (including
                an inverted planned-date range), or the request body
                carried a prohibited execution / observed-outcome /
                produced-deliverable attribute.
            ProjectObjectiveNotResolvableError: The target Objective
                Identity did not resolve to an existing Objective
                Resource (Requirement 4.3).
            ProjectAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 4.4). The Denial Record was appended
                successfully in a separate transaction (Requirement
                7.6).
            ProjectAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`ProjectAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back per Requirement 4.6 /
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
                raise ProjectValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirement 4.2, 4.3) before any
        # database read or authorization side-effect.
        self._validate_name(name)
        self._validate_summary(summary)
        self._validate_required_strings(
            target_objective_id=target_objective_id,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_planned_dates(planned_start_date, planned_end_date)

        # 3. Resolve the target Objective Resource Identity through a
        # single SELECT on ``Objectives``. The lookup runs on the
        # caller's connection so it participates in the caller's
        # transactional view. Requirement 4.3 rejects the unresolvable
        # case; there is no equivalent of Decision's outcome guard
        # because an Objective is itself the planning Resource being
        # addressed.
        resolved = connection.execute(
            text(
                "SELECT objective_id FROM Objectives "
                "WHERE objective_id = :objective_id"
            ),
            {"objective_id": target_objective_id},
        ).scalar_one_or_none()
        if resolved is None:
            raise ProjectObjectiveNotResolvableError(
                target_objective_id=target_objective_id,
            )

        # 4. Shared clock reading (design §"Cross-Cutting Concerns" —
        # *Transactionality*). The authorization evaluation row, the
        # Projects row, the Project_Revisions row, the Addresses
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
        # transaction stays a reader until step 8 below). On
        # ``permit`` the evaluation row commits independently; on
        # ``deny`` the row rolls back with the evaluation transaction
        # and the durable record of the denial is the Denial Record
        # appended by :meth:`_persist_project_denial`.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=authoring_party_id,
                action=_ACTION_CREATE_PROJECT,
                target=TargetRef(
                    kind=_KIND_OBJECTIVE,
                    id=target_objective_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or "no-role-assignment"
            self._persist_project_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_objective_id=target_objective_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise ProjectAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 6. Mint identifiers (AD-WS-2 / AD-WS-19). The Project
        # Resource and its first Revision are bound to the same
        # content digest because the Resource has no separate
        # "natural content" — the digest is derived from the first
        # Revision's payload (the same pattern Objectives uses).
        project_id = str(self.identity_service.new_resource_id())
        project_revision_id = str(self.identity_service.new_revision_id())
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        planned_start_iso = planned_start_date.isoformat()
        planned_end_iso = planned_end_date.isoformat()
        content_digest = _sha256_hex(
            json.dumps(
                {
                    "name": name,
                    "summary": summary,
                    "target_objective_id": target_objective_id,
                    "planned_start_date": planned_start_iso,
                    "planned_end_date": planned_end_iso,
                    "authoring_party_id": authoring_party_id,
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        # 7. Register both identifiers in ``Identifier_Registry``
        # carrying the AD-WS-19 ``resource_kind`` tag. The
        # ``'project'`` and ``'project_revision'`` tags are what
        # make Requirement 4.5's identifier-set disjointness from
        # Activity Plan identifiers inspectable at row granularity.
        # The helper delegates to
        # :meth:`IdentityService.reject_if_duplicate` so the Slice 1
        # identifier-conflict Denial Record pathway fires on any
        # collision; on success the helper INSERTs one row per
        # identifier inside the caller's transaction.
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_PROJECT,
            project_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PROJECT,
            recorded_time=recorded_time,
        )
        _record_planning_resource(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_PROJECT_REVISION,
            project_revision_id,
            content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PROJECT,
            recorded_time=recorded_time,
        )

        # 8. Insert the Resource header. ``created_at`` carries the
        # same recorded time as the first Revision (design
        # §"Persistence Invariants Summary" item 5) so the two rows'
        # timestamps are byte-equivalent.
        connection.execute(
            text(
                """
                INSERT INTO Projects (project_id, created_at)
                VALUES (:project_id, :created_at)
                """
            ),
            {"project_id": project_id, "created_at": recorded_at},
        )

        # 9. Insert the first immutable Revision (Requirement 4.2).
        # ``parent_revision_id`` is NULL because this is the first
        # Revision; subsequent Revisions would link backwards via the
        # column (out of scope for Slice 2 — the create endpoint only
        # writes the first Revision).
        connection.execute(
            text(
                """
                INSERT INTO Project_Revisions (
                    project_revision_id, project_id, parent_revision_id,
                    name, summary, target_objective_id,
                    planned_start_date, planned_end_date,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :project_revision_id, :project_id, NULL,
                    :name, :summary, :target_objective_id,
                    :planned_start_date, :planned_end_date,
                    :authoring_party_id, :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "project_revision_id": project_revision_id,
                "project_id": project_id,
                "name": name,
                "summary": summary,
                "target_objective_id": target_objective_id,
                "planned_start_date": planned_start_iso,
                "planned_end_date": planned_end_iso,
                "authoring_party_id": authoring_party_id,
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # 10. Insert the single ``Addresses`` Relationship row
        # (Requirement 4.2). ``semantic_role`` is NULL — the additive
        # AD-WS-17 column is reserved for Plan Review's ``'review'``
        # discriminator and is not used here.
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
                "source_kind": _KIND_PROJECT_REVISION,
                "source_id": project_id,
                "source_revision_id": project_revision_id,
                "target_kind": _KIND_OBJECTIVE,
                "target_id": target_objective_id,
                "target_revision_id": None,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 11. Append the consequential audit row (Requirement 4.6 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Projects,
        # Project_Revisions, and Relationships rows together.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_ACTION_CREATE_PROJECT,
            target_id=project_id,
            target_revision_id=project_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateProjectResult(
            project_id=project_id,
            project_revision_id=project_revision_id,
            name=name,
            summary=summary,
            target_objective_id=target_objective_id,
            planned_start_date=planned_start_iso,
            planned_end_date=planned_end_iso,
            authoring_party_id=authoring_party_id,
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_name(name: Any) -> None:
        """Reject Project name outside the Requirement 4.2 range.

        Empty or non-string names surface as ``name_missing`` since
        the actionable next step is the same in both cases (supply a
        non-empty string). An over-long name surfaces as
        ``name_too_long``.
        """
        if name is None or not isinstance(name, str) or name == "":
            raise ProjectValidationError(
                "name is required and must be a non-empty string of "
                f"{_NAME_MIN_CHARS}..{_NAME_MAX_CHARS} characters; "
                "Requirement 4.2 / 4.3.",
                failed_constraint="name_missing",
            )
        if len(name) > _NAME_MAX_CHARS:
            raise ProjectValidationError(
                f"name length {len(name)} exceeds the "
                f"{_NAME_MAX_CHARS}-character limit imposed by "
                "Requirement 4.2.",
                failed_constraint="name_too_long",
            )

    @staticmethod
    def _validate_summary(summary: Any) -> None:
        """Reject summary outside the Requirement 4.2 range.

        Requirement 4.2 mandates 0..4000 characters. ``None`` is
        accepted (the column is NULLable) and persisted as SQL
        ``NULL``; the empty string is also accepted (length 0
        satisfies the 0 lower bound) and persisted as ``''``.
        """
        if summary is None:
            return
        if not isinstance(summary, str):
            raise ProjectValidationError(
                f"summary must be a str or None; received "
                f"{type(summary).__name__}.",
                failed_constraint="summary_invalid_type",
            )
        if len(summary) > _SUMMARY_MAX_CHARS:
            raise ProjectValidationError(
                f"summary length {len(summary)} exceeds the "
                f"{_SUMMARY_MAX_CHARS}-character limit imposed by "
                "Requirement 4.2.",
                failed_constraint="summary_too_long",
            )

    @staticmethod
    def _validate_required_strings(
        *,
        target_objective_id: Any,
        authoring_party_id: Any,
        applicable_scope: Any,
    ) -> None:
        """Reject submissions missing required string attributes.

        Per Requirement 4.3: a Project creation request that names a
        target Objective Identity that does not resolve, omits the
        Project name, supplies a planned start date later than the
        planned end date, or omits the applicable scope is rejected.
        The name check lives in :meth:`_validate_name` and the date
        check in :meth:`_validate_planned_dates`; this validator
        covers the other required strings plus ``authoring_party_id``
        (which Requirement 4.4 implicitly requires — an unauthenticated
        request has no Party Identity to authorize against).
        """
        if not target_objective_id or not isinstance(target_objective_id, str):
            raise ProjectValidationError(
                "target_objective_id is required; Requirement 4.3 rejects "
                "Projects missing the target Objective Identity.",
                failed_constraint="target_objective_id_missing",
            )
        if not authoring_party_id or not isinstance(authoring_party_id, str):
            raise ProjectValidationError(
                "authoring_party_id is required; Requirement 4.4 rejects "
                "unauthenticated Project creation.",
                failed_constraint="authoring_party_id_missing",
            )
        if not applicable_scope or not isinstance(applicable_scope, str):
            raise ProjectValidationError(
                "applicable_scope is required; Requirement 4.3 rejects "
                "Projects missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_planned_dates(
        planned_start_date: Any, planned_end_date: Any
    ) -> None:
        """Reject malformed or inverted planned date ranges.

        Per Requirement 4.2 each planned date must be an ISO-8601
        calendar date with ``planned_start_date <= planned_end_date``
        and per Requirement 4.3 an inverted range is rejected before
        any persistence happens. ``datetime.datetime`` instances are
        not accepted because the schema column type is calendar
        date — see the ``isinstance(x, date) and not isinstance(x,
        datetime)`` guard. The schema CHECK constraint enforces the
        same ordering as a defense in depth.
        """
        if not isinstance(planned_start_date, date) or isinstance(
            planned_start_date, datetime
        ):
            raise ProjectValidationError(
                "planned_start_date must be a datetime.date (not datetime); "
                f"received {type(planned_start_date).__name__}.",
                failed_constraint="planned_start_date_invalid_type",
            )
        if not isinstance(planned_end_date, date) or isinstance(
            planned_end_date, datetime
        ):
            raise ProjectValidationError(
                "planned_end_date must be a datetime.date (not datetime); "
                f"received {type(planned_end_date).__name__}.",
                failed_constraint="planned_end_date_invalid_type",
            )
        if planned_start_date > planned_end_date:
            raise ProjectValidationError(
                f"planned_start_date {planned_start_date.isoformat()!r} is "
                f"later than planned_end_date "
                f"{planned_end_date.isoformat()!r}; Requirement 4.2 / 4.3 "
                "require planned_start_date <= planned_end_date.",
                failed_constraint="planned_date_range_inverted",
            )

    # -- denial side-channel ----------------------------------------------

    def _persist_project_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_objective_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Project attempt.

        Implements the Requirement 7.6 retry contract verbatim
        (mirroring
        :meth:`walking_slice.planning.objectives.ObjectiveService._persist_objective_denial`):
        each attempt opens a *new* :meth:`Engine.begin` transaction
        (so a previous attempt's rollback does not poison this one),
        tries :meth:`AuditLog.append_denial`, and either returns on
        success or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails, :class:`ProjectAuditFailureError`
          is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_project` raises
        :class:`ProjectAuthorizationError` (or this method raises
        :class:`ProjectAuditFailureError`). The Denial Record must
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
                        attempted_action=_ACTION_CREATE_PROJECT,
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

        raise ProjectAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this module
# does not import private names from sibling planning modules. The
# functions are intentionally identical to their Objectives siblings:
# correlation identifiers are non-domain values and the digest is opaque
# to :class:`Identifier_Registry` — both implementations could be
# replaced with shared utility functions in a future refactor without
# changing observable behavior.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical Project
    creation. They are not registered with :class:`IdentityService`
    because they do not name a domain Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the Project Resource
    Identity and the first Project Revision Identity in
    ``Identifier_Registry``. Sharing one digest across both bindings
    mirrors the Slice 1 / Objectives convention and keeps the AD-WS-2
    non-reuse invariant exercised at the same granularity for
    Resource and first-Revision identifiers.
    """
    return hashlib.sha256(content).hexdigest()
