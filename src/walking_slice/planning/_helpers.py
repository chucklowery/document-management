"""Shared helpers for the second-walking-slice Planning_Service modules.

Design references:

- ``.kiro/specs/second-walking-slice/design.md`` §"Components and Interfaces"
  (every Planning_Service module's request validation contract and the
  ``_validate_no_observed_attributes`` / ``_validate_no_produced_attributes``
  pattern named for IntendedOutcomes and DeliverableExpectations).
- ``.kiro/specs/second-walking-slice/design.md`` §"Cross-Cutting Concerns"
  (*Identifiers*) — every new identity is a UUIDv7 minted by the existing
  :class:`~walking_slice.identity.IdentityService` and registered in
  ``Identifier_Registry`` with ``kind ∈ {'resource', 'revision',
  'immutable_record'}`` and ``resource_kind`` set to one of
  ``{'objective', 'intended_outcome', 'project', 'deliverable_expectation',
  'activity_plan', 'plan_revision', 'plan_review', 'plan_approval'}``.
- AD-WS-19 — additive ``Identifier_Registry.resource_kind`` column that
  the helper here populates so Requirement 4.5 (Project / Activity Plan
  identifier-set disjointness) is enforceable at row level without
  altering the global ``UNIQUE(identifier)`` constraint.

Responsibility of this module (task 2.2):

1. Expose :func:`_record_planning_resource`, a small helper every
   Planning_Service module calls when it has minted a new Resource,
   Revision, or Immutable Record identity. The helper:
   - validates canonical UUIDv7 form via
     :class:`~walking_slice.identity.IdentityService` (delegating to
     :meth:`IdentityService.reject_if_duplicate`),
   - on conflict (same identifier already bound to a different digest)
     re-raises :class:`~walking_slice.identity.IdentityConflictError`
     after the IdentityService has appended the separate-transaction
     Denial Record per design §"Error Handling — Identifier conflict",
   - on success INSERTs one row into ``Identifier_Registry`` carrying
     the ``resource_kind`` tag so Slice 2 can keep the Project and
     Activity Plan identifier sets disjoint at row granularity
     (Requirement 4.5).

2. Expose :func:`_reject_prohibited_attributes`, the cross-cutting
   request-body validator every Planning_Service endpoint uses to
   enforce Property 22 (Plan / Execution and Output / Outcome
   separation). The function raises :class:`PlanningValidationError`
   identifying every top-level request key whose name begins with one
   of the prohibited prefix sets:

   - **execution prefixes** (Requirement 12.1, 12.2): ``work-``,
     ``time-``, ``milestone-``, ``deliverable-production-``,
     ``blockage-``, ``completion-``, ``actual-``, ``percent-complete-``,
     ``remaining-``.
   - **observed-outcome prefixes** (Requirement 13.1, 13.5):
     ``observed-``, ``observation-time-``,
     ``attribution-evidence-``.
   - **produced-deliverable prefixes** (Requirement 13.2, 13.5):
     ``produced-``, ``hand-off-``, ``accepted-by-``.

   Field-name matching is invariant under hyphen/underscore swaps and
   case — the Planning_Service accepts both ``work_started_at`` (the
   Python/JSON snake-case convention) and ``work-started-at`` (the
   spec's hyphenated prose) and rejects both.

Requirements satisfied (per task 2.2):

    4.5   — Project and Activity Plan Resource identifier sets are
            disjoint; the helper tags every Slice 2 identifier with its
            ``resource_kind`` so the disjointness invariant is
            inspectable in the registry.
    12.1  — every prohibited execution attribute is rejected at the API
            boundary.
    12.2  — Plan Revision, Activity Plan, Objective, Intended Outcome,
            Project, Deliverable Expectation, Plan Review, and Plan
            Approval creation requests are rejected when they carry a
            prohibited execution attribute.
    13.1  — every prohibited observed-outcome attribute is rejected on
            Intended Outcome requests.
    13.2  — every prohibited produced-deliverable attribute is rejected
            on Deliverable Expectation requests.
    13.5  — the response identifies every prohibited attribute.
    20.5, 20.6 — Property 22 verifies these prefixes by construction.
    20.12 — Identifier_Registry stays the source of truth for identifier
            non-reuse; the helper does not bypass the registry insert.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any, Final, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.audit import format_iso8601_ms
from walking_slice.identity import (
    ALLOWED_IDENTIFIER_KINDS,
    IdentityService,
)


__all__ = [
    "EXECUTION_PROHIBITED_PREFIXES",
    "OBSERVED_OUTCOME_PROHIBITED_PREFIXES",
    "PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES",
    "ALL_PROHIBITED_PREFIXES",
    "PLANNING_RESOURCE_KINDS",
    "PlanningValidationError",
    "_record_planning_resource",
    "_reject_prohibited_attributes",
]


# ---------------------------------------------------------------------------
# Prohibited-attribute prefix sets.
#
# Sourced from:
#   - Requirement 12.1 (Plan / Execution separation — execution facts)
#   - Requirement 12.2 (rejection of execution attributes on planning
#     creation requests)
#   - Requirement 13.1 (Output / Outcome separation — observed outcome
#     facts not accepted on Intended Outcome)
#   - Requirement 13.2 (Output / Outcome separation — produced-Deliverable
#     facts not accepted on Deliverable Expectation)
#   - Requirement 13.5 (rejection identifies each prohibited attribute)
#   - Property 22 (Plan/Execution and Output/Outcome separation) which
#     enumerates the exact prefix tokens this module rejects.
#
# All prefixes are stored in canonical hyphen-lowercase form. Matching is
# case-insensitive and hyphen/underscore-invariant — see
# :func:`_normalize_key`.
# ---------------------------------------------------------------------------


EXECUTION_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    "work-",
    "time-",
    "milestone-",
    "deliverable-production-",
    "blockage-",
    "completion-",
    "actual-",
    "percent-complete-",
    "remaining-",
)
"""Execution attribute prefixes rejected on every Planning_Service request.

Drawn verbatim from Property 22's execution prefix list (which is in
turn grounded in Requirement 12.1's enumeration of forbidden execution
facts: actor-assigned-time, work-started-time, work-completed-time,
time-entry quantity, actual-cost value, percent-complete value,
blockage-observation text, completion-evidence reference, plus the
Milestone Acceptance and Deliverable Production Record kinds from
Requirement 12.1).
"""


OBSERVED_OUTCOME_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    "observed-",
    "observation-time-",
    "attribution-evidence-",
)
"""Observed-outcome attribute prefixes rejected on Intended Outcome requests.

Drawn from Property 22's observed-outcome prefix list (grounded in
Requirement 13.1's enumeration of forbidden observed-outcome attributes
on Intended Outcome Revisions).
"""


PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    "produced-",
    "hand-off-",
    "accepted-by-",
)
"""Produced-deliverable attribute prefixes rejected on Deliverable
Expectation requests.

Drawn from Property 22's produced-deliverable prefix list (grounded in
Requirement 13.2's enumeration of forbidden produced-deliverable
attributes on Deliverable Expectation Revisions).
"""


ALL_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    EXECUTION_PROHIBITED_PREFIXES
    + OBSERVED_OUTCOME_PROHIBITED_PREFIXES
    + PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES
)
"""Union of every prohibited prefix.

Convenience constant for callers that need to reject every
non-planning attribute in one pass (e.g., the FastAPI Pydantic
request models defined in task 15.1).
"""


# ---------------------------------------------------------------------------
# Planning resource_kind tag values.
#
# Sourced from design §"Persistence Invariants Summary" item 6 and
# Requirement 4.5 (Project / Activity Plan disjointness, generalized to
# every Slice 2 Resource kind). Includes Revision-level kinds because the
# helper is reused by every Planning_Service module to record both
# Resource and Revision identifiers.
# ---------------------------------------------------------------------------


PLANNING_RESOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "objective",
        "objective_revision",
        "intended_outcome",
        "intended_outcome_revision",
        "project",
        "project_revision",
        "deliverable_expectation",
        "deliverable_expectation_revision",
        "activity_plan",
        "plan_revision",
        "plan_review",
        "plan_review_revision",
        "plan_approval",
    }
)


# ---------------------------------------------------------------------------
# Errors raised by the helpers.
# ---------------------------------------------------------------------------


class PlanningValidationError(ValueError):
    """Raised when a Planning_Service request body fails shared validation.

    The primary use is :func:`_reject_prohibited_attributes`: when one
    or more top-level keys match a prohibited execution, observed-
    outcome, or produced-deliverable prefix the error carries the
    offending keys verbatim on :attr:`prohibited_keys` so the route
    layer can return them in the response body per Requirement 13.5
    ("…return an error indication identifying each prohibited
    attribute…") and Requirement 12.2 ("…identifying each prohibited
    execution attribute…").

    Subclass of :class:`ValueError` so callers that already catch
    request-validation failures continue to work; downstream code may
    catch this type specifically to surface the list of prohibited
    keys.
    """

    def __init__(
        self,
        message: str,
        *,
        prohibited_keys: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.prohibited_keys = prohibited_keys


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _normalize_key(key: str) -> str:
    """Normalize a request body key for prohibited-prefix matching.

    Both ``work_started_at`` (snake_case, the Python/JSON convention)
    and ``work-started-at`` (the spec's hyphenated prose) must be
    rejected by the same prefix ``work-``. The Planning_Service also
    treats keys case-insensitively. The canonical form for matching is
    lowercase with underscores rewritten as hyphens.
    """
    return key.lower().replace("_", "-")


def _reject_prohibited_attributes(
    request_body: Mapping[str, Any],
    prefixes: Iterable[str],
) -> None:
    """Reject the request body if any top-level key matches any prefix.

    Per Property 22 / Requirements 12.1, 12.2, 13.1, 13.2, 13.5, every
    Planning_Service endpoint refuses requests that carry execution,
    observed-outcome, or produced-deliverable attributes. Matching is
    case-insensitive and hyphen/underscore-invariant: ``work_started_at``,
    ``Work-Started-At``, and ``work-started-at`` are all rejected by the
    ``work-`` prefix.

    Args:
        request_body: The top-level mapping the route layer received.
            Typically the result of ``request_model.model_dump()`` from
            a Pydantic ``Config(extra='forbid')`` request model, but any
            :class:`collections.abc.Mapping` is accepted so callers can
            pass raw JSON-decoded ``dict``s as well.
        prefixes: An iterable of prohibited prefix strings.
            :data:`EXECUTION_PROHIBITED_PREFIXES`,
            :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES`,
            :data:`PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES`, or their
            union :data:`ALL_PROHIBITED_PREFIXES`. Each prefix is
            normalized the same way as request keys before matching.

    Raises:
        PlanningValidationError: At least one top-level key matched at
            least one prefix. :attr:`PlanningValidationError.prohibited_keys`
            carries every offending key in the order they appeared in
            *request_body*; the route layer surfaces this tuple in the
            response per Requirement 13.5.
        TypeError: *request_body* is not a :class:`Mapping`.
    """
    if not isinstance(request_body, Mapping):
        raise TypeError(
            "request_body must be a Mapping (e.g., a dict from "
            f"model_dump()); received {type(request_body).__name__}."
        )

    normalized_prefixes: tuple[str, ...] = tuple(
        _normalize_key(p) for p in prefixes
    )
    if not normalized_prefixes:
        return

    offending: list[str] = []
    for key in request_body.keys():
        if not isinstance(key, str):
            # Pydantic models produce ``str`` keys only; skip anything
            # else defensively so we do not raise a misleading TypeError
            # from .lower() on a non-str key.
            continue
        normalized_key = _normalize_key(key)
        for prefix in normalized_prefixes:
            if normalized_key.startswith(prefix):
                offending.append(key)
                break

    if offending:
        raise PlanningValidationError(
            f"request body contains prohibited attribute(s) {offending!r}; "
            "planning resources may not carry execution, observed-outcome, "
            "or produced-deliverable fields (Requirements 12.1, 12.2, "
            "13.1, 13.2, 13.5).",
            prohibited_keys=tuple(offending),
        )


def _record_planning_resource(
    connection: Connection,
    registry_kind: str,
    resource_kind: str,
    identifier: str,
    content_digest: str,
    *,
    identity_service: IdentityService,
    actor_party_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    attempted_action: str = "bind.identifier",
    recorded_time: Optional[datetime] = None,
) -> None:
    """Register a Slice 2 Planning Resource identifier in ``Identifier_Registry``.

    Performs two steps per design §"Cross-Cutting Concerns"
    (*Identifiers*) and AD-WS-19:

    1. **Conflict detection.** Calls
       :meth:`IdentityService.reject_if_duplicate` to validate the
       canonical UUIDv7 form and reject any attempt to re-bind an
       existing identifier to a different digest. On conflict the
       IdentityService appends a Denial Record from a separate
       transaction (design §"Error Handling — Identifier conflict") and
       raises :class:`~walking_slice.identity.IdentityConflictError`,
       which propagates out of this helper so the caller's originating
       transaction rolls back.

    2. **Tagged INSERT.** When the identifier is not yet bound, INSERTs
       one row into ``Identifier_Registry`` carrying the ``resource_kind``
       tag (and the registry-level ``kind`` discriminator the Slice 1
       ``CHECK`` constraint validates) inside the caller's transaction
       per AD-WS-5. The same row that the registry insert produces is
       the one row that satisfies Requirement 4.5's Project /
       Activity Plan disjointness check.

    When the identifier is already bound to *content_digest*, the call
    is idempotent — no new row is INSERTed and no audit row is
    appended (matching the IdentityService re-confirmation contract).

    Args:
        connection: The caller's SQLAlchemy connection. The registry
            row is INSERTed inside this transaction so it rolls back
            with the caller's domain rows on failure (AD-WS-5).
        registry_kind: The value written to ``Identifier_Registry.kind``.
            Must be one of :data:`walking_slice.identity.ALLOWED_IDENTIFIER_KINDS`
            (in practice for Slice 2: ``'resource'``, ``'revision'``,
            or ``'immutable_record'``).
        resource_kind: The value written to
            ``Identifier_Registry.resource_kind``. Must be one of
            :data:`PLANNING_RESOURCE_KINDS`.
        identifier: The canonical UUIDv7 string to bind. The
            IdentityService validates the form before any SQL is
            issued.
        content_digest: The digest the identifier is being bound to.
            Typically a SHA-256 hex string over the canonical payload of
            the Resource or Revision; opaque to this helper.
        identity_service: The Slice 1 :class:`IdentityService` instance
            that owns the in-memory and persistent registry surface.
            Keyword-only to keep call sites at the planning modules
            explicit about which collaborator they are passing through.
        actor_party_id: Forwarded to
            :meth:`IdentityService.reject_if_duplicate` so a Denial
            Record on conflict carries the originating Party's
            Identity.
        correlation_id: Forwarded to
            :meth:`IdentityService.reject_if_duplicate` so the Denial
            Record on conflict shares the originating operation's
            correlation identifier.
        attempted_action: ``action_type`` written to the denial record
            on conflict. Callers should pass their domain action name
            (e.g., ``"create.objective"``) so audit consumers can map
            the denial back to the Planning_Service request.
        recorded_time: Optional explicit timestamp for both the
            registry row's ``issued_at`` and the denial record's
            ``recorded_at``. When omitted, the IdentityService's
            injected clock supplies a value; when neither is available
            the system UTC clock is consulted as a last resort.

    Raises:
        ValueError: *registry_kind* is missing or not in
            :data:`ALLOWED_IDENTIFIER_KINDS`, or *resource_kind* is not
            in :data:`PLANNING_RESOURCE_KINDS`.
        walking_slice.identity.IdentityFormatError: *identifier* is not
            in canonical UUIDv7 form.
        walking_slice.identity.IdentityConflictError: *identifier* is
            already bound to a different content digest. The Denial
            Record has been appended in a separate transaction before
            this exception was raised.
    """
    if registry_kind not in ALLOWED_IDENTIFIER_KINDS:
        raise ValueError(
            f"unknown registry kind {registry_kind!r}; "
            f"expected one of {sorted(ALLOWED_IDENTIFIER_KINDS)}."
        )
    if resource_kind not in PLANNING_RESOURCE_KINDS:
        raise ValueError(
            f"unknown planning resource_kind {resource_kind!r}; "
            f"expected one of {sorted(PLANNING_RESOURCE_KINDS)}."
        )

    # Look up the existing binding (if any) before delegating to the
    # IdentityService. Three cases follow:
    #   * Row missing       → fresh INSERT with resource_kind below.
    #   * Row same digest   → idempotent — no INSERT, no audit.
    #   * Row other digest  → IdentityService writes the Denial Record
    #                         and raises IdentityConflictError.
    existing_digest = connection.execute(
        text(
            "SELECT content_digest FROM Identifier_Registry "
            "WHERE identifier = :identifier"
        ),
        {"identifier": identifier},
    ).scalar_one_or_none()

    if existing_digest is not None:
        # Delegate to IdentityService.reject_if_duplicate so the
        # conflict path drives through the existing Denial-Record
        # side-channel (separate transaction, reason_code
        # 'identifier-conflict'); the idempotent same-digest case
        # returns silently from the IdentityService as well.
        identity_service.reject_if_duplicate(
            identifier=identifier,
            content_digest=content_digest,
            connection=connection,
            kind=registry_kind,
            actor_party_id=actor_party_id,
            correlation_id=correlation_id,
            attempted_action=attempted_action,
            recorded_time=recorded_time,
        )
        return

    # Fresh identifier — validate canonical form via the in-memory entry
    # point of reject_if_duplicate (no connection supplied so the SQL
    # INSERT path is not taken; we drive the INSERT ourselves below to
    # include the additive resource_kind column). This also records the
    # binding in the IdentityService's in-memory registry so subsequent
    # in-memory calls remain consistent.
    identity_service.reject_if_duplicate(identifier, content_digest)

    issued_at = _resolve_issued_at(identity_service, recorded_time)

    connection.execute(
        text(
            """
            INSERT INTO Identifier_Registry
                (identifier, kind, content_digest, issued_at, resource_kind)
            VALUES
                (:identifier, :kind, :content_digest, :issued_at, :resource_kind)
            """
        ),
        {
            "identifier": identifier,
            "kind": registry_kind,
            "content_digest": content_digest,
            "issued_at": issued_at,
            "resource_kind": resource_kind,
        },
    )


def _resolve_issued_at(
    identity_service: IdentityService,
    recorded_time: Optional[datetime],
) -> str:
    """Resolve the ISO-8601 millisecond timestamp for ``issued_at``.

    Precedence mirrors :meth:`IdentityService._resolve_recorded_at`:
    explicit *recorded_time* wins over the IdentityService's clock,
    which wins over the system UTC clock. Keeping the precedence aligned
    means the registry row this helper INSERTs carries the same
    ``issued_at`` it would have carried had the IdentityService driven
    the INSERT itself.
    """
    if recorded_time is not None:
        return format_iso8601_ms(recorded_time)
    clock = identity_service.clock
    if clock is not None:
        return format_iso8601_ms(clock.now())
    return format_iso8601_ms(datetime.now(timezone.utc))
