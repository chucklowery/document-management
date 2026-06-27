"""Shared helpers for the third-walking-slice Execution_Service modules.

Design references:

- ``.kiro/specs/third-walking-slice/design.md`` §"Cross-Cutting Concerns"
  (*Identifiers*) — every new identity is a UUIDv7 minted by the existing
  :class:`~walking_slice.identity.IdentityService` and registered in
  ``Identifier_Registry`` with ``kind ∈ {'immutable_record', 'resource',
  'revision'}`` and ``resource_kind`` set to one of the eight Slice 3
  values from AD-WS-28:
  ``{work_assignment_record, work_event_record, time_entry_record,
  deliverable_resource, deliverable_revision,
  deliverable_production_record, milestone_acceptance_record,
  completion_record}``.
- ``.kiro/specs/third-walking-slice/design.md`` §"Architectural
  Decisions — AD-WS-28" (per-Record-kind tables; one schema addition
  on ``Identifier_Registry``).
- ``.kiro/specs/third-walking-slice/design.md`` §"Error Handling —
  Error categories / Input validation" — Slice 3's input-validation
  rejection of prohibited planning-attribute prefixes
  ``{planned-, planning-assumption-, ordering-rationale-, plan-review-,
  plan-approval-}`` per Requirement 33 and prohibited observed-outcome
  prefixes ``{observed-, measurement-, outcome-review-,
  attribution-evidence-, success-condition-assessment-}`` per
  Requirement 34.
- Slice 2 sibling: :mod:`walking_slice.planning._helpers`. The
  :func:`_reject_prohibited_attributes` helper here is the additive
  Slice 3 extension of the Slice 2 helper of the same name; the
  matching contract (case-insensitive, hyphen/underscore-invariant,
  prefix-based, raises with the offending keys listed) is identical so
  every Slice 1 + Slice 2 + Slice 3 endpoint shares the same rejection
  shape (Property 35, Property 36).

Responsibility of this module (task 3.3):

1. Expose :func:`_record_execution_artifact`, the small helper every
   Execution_Service and Deliverable_Repository module calls when it
   has minted a new Immutable Record, produced Deliverable Resource,
   or produced Deliverable Revision identity. The helper:

   - validates canonical UUIDv7 form via
     :class:`~walking_slice.identity.IdentityService` (delegating to
     :meth:`IdentityService.reject_if_duplicate`),
   - on conflict (same identifier already bound to a different digest)
     re-raises :class:`~walking_slice.identity.IdentityConflictError`
     after the IdentityService has appended the separate-transaction
     Denial Record per Slice 1 design §"Error Handling — Identifier
     conflict",
   - on success INSERTs one row into ``Identifier_Registry`` carrying
     the Slice 3 ``resource_kind`` tag so the produced-Deliverable
     identifier set stays disjoint from the Slice 1 Source Evidence
     Document identifier set (Requirement 26.3) and so the eight
     Slice 3 identifier roles remain pairwise disjoint relative to
     every Slice 1 and Slice 2 identifier (Requirement 22.8).

2. Expose :func:`_reject_prohibited_attributes`, the cross-cutting
   request-body validator every Execution_Service and
   Deliverable_Repository endpoint uses to enforce Property 35
   (Plan / Execution separation enforced from the execution side) and
   Property 36 (Output / Outcome separation enforced from the execution
   side). The function raises :class:`ExecutionValidationError`
   identifying every top-level request key whose name begins with one
   of the prohibited prefix sets:

   - **planning-attribute prefixes** (Requirement 33.2, 33.3, 33.4):
     ``planned-``, ``planning-assumption-``, ``ordering-rationale-``,
     ``plan-review-``, ``plan-approval-``. Drawn verbatim from design
     §"Error Handling — Input validation" and Property 35.
   - **observed-outcome prefixes** (Requirement 34.1, 34.2, 34.5):
     ``observed-``, ``measurement-``, ``outcome-review-``,
     ``attribution-evidence-``, ``success-condition-assessment-``.
     Drawn verbatim from design §"Error Handling — Input validation"
     and Property 36.

   Field-name matching is invariant under hyphen/underscore swaps and
   case — the Execution_Service accepts both ``planned_scope`` (the
   Python/JSON snake-case convention) and ``planned-scope`` (the spec's
   hyphenated prose) and rejects both.

Requirements satisfied (per task 3.3):

    22.8  — every Slice 3 identifier carries a ``resource_kind`` tag
            drawn from the eight Slice 3 values so the eight Slice 3
            identifier roles remain pairwise disjoint relative to every
            Slice 1 and Slice 2 identifier (no Slice 3 identifier also
            identifies a Slice 1 or Slice 2 entity).
    26.3  — produced Deliverable Resource Identity is disjoint from
            Slice 1 Source Evidence Document Resource Identity at row
            level: the helper tags every produced Deliverable Resource
            with ``resource_kind = 'deliverable_resource'`` (and every
            Revision with ``resource_kind = 'deliverable_revision'``)
            so the disjointness invariant is inspectable in the
            registry, complementing the schema-level disjointness
            already established by the separate ``Deliverable_Resources``
            / ``Source_Documents`` tables.
    33.2  — Execution_Service request bodies that name any field whose
            key begins with a prohibited planning-attribute prefix are
            rejected before any row is persisted.
    33.3  — Deliverable_Repository request bodies that name any field
            whose key begins with a prohibited planning-attribute
            prefix are rejected before any row is persisted.
    33.4  — the response identifies every prohibited planning attribute
            via :attr:`ExecutionValidationError.prohibited_keys`.
    34.1  — Execution_Service request bodies that name any field whose
            key begins with a prohibited observed-outcome prefix are
            rejected before any row is persisted.
    34.2  — Deliverable_Repository request bodies that name any field
            whose key begins with a prohibited observed-outcome prefix
            are rejected before any row is persisted.
    34.5  — the response identifies every prohibited observed-outcome
            attribute via :attr:`ExecutionValidationError.prohibited_keys`.
    40.3  — every Slice 3 Relationship from an execution Record to a
            Slice 1 or Slice 2 Resource is tagged through this helper's
            registry insert without ever issuing an INSERT, UPDATE, or
            DELETE against a Slice 1 or Slice 2 row.
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
    "PLANNING_PROHIBITED_PREFIXES",
    "OBSERVED_OUTCOME_PROHIBITED_PREFIXES",
    "ALL_PROHIBITED_PREFIXES",
    "EXECUTION_RESOURCE_KINDS",
    "ExecutionValidationError",
    "_record_execution_artifact",
    "_reject_prohibited_attributes",
]


# ---------------------------------------------------------------------------
# Prohibited-attribute prefix sets.
#
# Sourced verbatim from:
#   - design §"Error Handling — Error categories / Input validation"
#     (the explicit prefix enumerations passed to
#     ``_reject_prohibited_attributes``),
#   - Property 35 (planning-attribute prefix list, Requirements 33.1,
#     33.2, 33.3, 33.4, 40.3, 40.4),
#   - Property 36 (observed-outcome prefix list, Requirements 34.1,
#     34.2, 34.3, 34.4, 34.5).
#
# All prefixes are stored in canonical hyphen-lowercase form. Matching is
# case-insensitive and hyphen/underscore-invariant — see
# :func:`_normalize_key`.
# ---------------------------------------------------------------------------


PLANNING_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    "planned-",
    "planning-assumption-",
    "ordering-rationale-",
    "plan-review-",
    "plan-approval-",
)
"""Planning-attribute prefixes rejected on every Execution_Service and
Deliverable_Repository request.

Drawn verbatim from Property 35's planning-attribute prefix list
(grounded in Requirement 33.2 / 33.3 enumeration of forbidden planning
facts on execution Records and produced Deliverable Revisions):

- ``planned-`` covers ``planned_scope``, ``planned_start_date``,
  ``planned_end_date``, ``planned_deliverable_*``, and every other
  attribute that asserts a planning fact rather than an execution fact.
- ``planning-assumption-`` covers Plan Revision assumption entries.
- ``ordering-rationale-`` covers Activity Plan ordering rationale
  values.
- ``plan-review-`` covers Plan Review outcomes (Slice 2 Requirement 7).
- ``plan-approval-`` covers Plan Approval outcomes (Slice 2 Requirement
  9). The target Approved Plan Revision Identity itself is the only
  explicit planning reference Slice 3 carries (Requirement 33.2's
  exception clause); it is not prefixed with any of these tokens.
"""


OBSERVED_OUTCOME_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    "observed-",
    "measurement-",
    "outcome-review-",
    "attribution-evidence-",
    "success-condition-assessment-",
)
"""Observed-outcome attribute prefixes rejected on every Execution_Service
and Deliverable_Repository request.

Drawn verbatim from Property 36's observed-outcome prefix list
(grounded in Requirement 34.1 / 34.2 enumeration of forbidden
observed-outcome attributes on execution Records and produced
Deliverable Revisions):

- ``observed-`` covers ``observed_outcome_value``,
  ``observed_outcome_time``, and every other Observed Outcome attribute.
- ``measurement-`` covers Measurement Definition and Measurement Record
  attributes.
- ``outcome-review-`` covers Outcome Review attributes.
- ``attribution-evidence-`` covers attribution-evidence references.
- ``success-condition-assessment-`` covers success-condition
  assessments.

Note: the Slice 2 helper
:mod:`walking_slice.planning._helpers.OBSERVED_OUTCOME_PROHIBITED_PREFIXES`
carries the Slice 2 subset ``{observed-, observation-time-,
attribution-evidence-}`` because Slice 2's threat model targeted
Intended Outcome creation requests only. Slice 3's threat model
includes Measurement Records, Outcome Reviews, and success-condition
assessments because Completion Records and Milestone Acceptance
Records are the entities most likely to be aliased into observed
Outcomes (Requirement 34.3, 34.4). The Slice 3 set therefore extends
the Slice 2 set additively; both sets remain valid for their
respective callers.
"""


ALL_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    PLANNING_PROHIBITED_PREFIXES + OBSERVED_OUTCOME_PROHIBITED_PREFIXES
)
"""Union of every prohibited prefix.

Convenience constant for callers that need to reject every
non-execution attribute in one pass. Every Slice 3 service module
(:mod:`walking_slice.execution.work_assignments`,
:mod:`walking_slice.execution.work_events`,
:mod:`walking_slice.execution.time_entries`,
:mod:`walking_slice.execution.deliverable_productions`,
:mod:`walking_slice.execution.milestone_acceptances`,
:mod:`walking_slice.execution.completions`, and
:mod:`walking_slice.deliverables.repository`) passes this constant to
:func:`_reject_prohibited_attributes` so the same validation surface
applies uniformly.
"""


# ---------------------------------------------------------------------------
# Slice 3 resource_kind tag values.
#
# Sourced from AD-WS-28 (the eight Slice 3 ``resource_kind`` values
# emitted on the existing additive ``Identifier_Registry.resource_kind``
# column). The set is held as a ``frozenset`` so a typo at a call site
# fails fast with :class:`ValueError` before any SQL is issued.
# ---------------------------------------------------------------------------


EXECUTION_RESOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "work_assignment_record",
        "work_event_record",
        "time_entry_record",
        "deliverable_resource",
        "deliverable_revision",
        "deliverable_production_record",
        "milestone_acceptance_record",
        "completion_record",
    }
)


# ---------------------------------------------------------------------------
# Errors raised by the helpers.
# ---------------------------------------------------------------------------


class ExecutionValidationError(ValueError):
    """Raised when an Execution_Service or Deliverable_Repository request
    body fails shared validation.

    The primary use is :func:`_reject_prohibited_attributes`: when one
    or more top-level keys match a prohibited planning-attribute or
    observed-outcome prefix the error carries the offending keys
    verbatim on :attr:`prohibited_keys` so the route layer can return
    them in the response body per Requirement 33.4 ("…return an error
    indication identifying each prohibited planning attribute…") and
    Requirement 34.5 ("…return an error indication identifying each
    prohibited attribute…").

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

    Both ``planned_scope`` (snake_case, the Python/JSON convention) and
    ``planned-scope`` (the spec's hyphenated prose) must be rejected by
    the same prefix ``planned-``. The Execution_Service also treats
    keys case-insensitively. The canonical form for matching is
    lowercase with underscores rewritten as hyphens, matching the Slice 2
    convention in :func:`walking_slice.planning._helpers._normalize_key`.
    """
    return key.lower().replace("_", "-")


def _reject_prohibited_attributes(
    request_body: Mapping[str, Any],
    prefixes: Iterable[str],
) -> None:
    """Reject the request body if any top-level key matches any prefix.

    Per Property 35 / Property 36 / Requirements 33.2, 33.3, 33.4,
    34.1, 34.2, 34.5, every Execution_Service and Deliverable_Repository
    endpoint refuses requests that carry planning-attribute or
    observed-outcome attributes. Matching is case-insensitive and
    hyphen/underscore-invariant: ``planned_scope``,
    ``Planned-Scope``, and ``planned-scope`` are all rejected by the
    ``planned-`` prefix.

    This helper additively extends the Slice 2 helper
    :func:`walking_slice.planning._helpers._reject_prohibited_attributes`
    (Requirement 40.3): the matching algorithm and the
    :class:`ValueError` subclassing contract are identical, only the
    default prefix sets differ. Callers wishing the Slice 2 surface
    invoke the Slice 2 helper directly; callers wishing the Slice 3
    surface invoke this helper with
    :data:`PLANNING_PROHIBITED_PREFIXES`,
    :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES`, or their union
    :data:`ALL_PROHIBITED_PREFIXES`.

    Args:
        request_body: The top-level mapping the route layer received.
            Typically the result of ``request_model.model_dump()`` from
            a Pydantic ``Config(extra='forbid')`` request model, but any
            :class:`collections.abc.Mapping` is accepted so callers can
            pass raw JSON-decoded ``dict``s as well.
        prefixes: An iterable of prohibited prefix strings.
            :data:`PLANNING_PROHIBITED_PREFIXES`,
            :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES`, or their union
            :data:`ALL_PROHIBITED_PREFIXES`. Each prefix is normalized
            the same way as request keys before matching.

    Raises:
        ExecutionValidationError: At least one top-level key matched at
            least one prefix.
            :attr:`ExecutionValidationError.prohibited_keys` carries
            every offending key in the order they appeared in
            *request_body*; the route layer surfaces this tuple in the
            response per Requirements 33.4 and 34.5.
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
        raise ExecutionValidationError(
            f"request body contains prohibited attribute(s) {offending!r}; "
            "execution Records and produced Deliverables may not carry "
            "planning-attribute or observed-outcome fields "
            "(Requirements 33.2, 33.3, 33.4, 34.1, 34.2, 34.5).",
            prohibited_keys=tuple(offending),
        )


def _record_execution_artifact(
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
    """Register a Slice 3 execution artifact identifier in
    ``Identifier_Registry``.

    Performs two steps per design §"Cross-Cutting Concerns"
    (*Identifiers*) and AD-WS-28:

    1. **Conflict detection.** Calls
       :meth:`IdentityService.reject_if_duplicate` to validate the
       canonical UUIDv7 form and reject any attempt to re-bind an
       existing identifier to a different digest. On conflict the
       IdentityService appends a Denial Record from a separate
       transaction (Slice 1 design §"Error Handling — Identifier
       conflict") and raises
       :class:`~walking_slice.identity.IdentityConflictError`, which
       propagates out of this helper so the caller's originating
       transaction rolls back.

    2. **Tagged INSERT.** When the identifier is not yet bound, INSERTs
       one row into ``Identifier_Registry`` carrying the
       ``resource_kind`` tag (and the registry-level ``kind``
       discriminator the Slice 1 ``CHECK`` constraint validates) inside
       the caller's transaction per AD-WS-5. The Slice 3 ``resource_kind``
       value is the single row-level fact that lets Requirement 22.8
       (eight disjoint Slice 3 identifier roles) and Requirement 26.3
       (produced Deliverable Resource Identity disjoint from Slice 1
       Source Evidence Document Resource Identity) remain inspectable
       on the registry table without requiring a join.

    When the identifier is already bound to *content_digest*, the call
    is idempotent — no new row is INSERTed and no audit row is
    appended (matching the IdentityService re-confirmation contract
    and the Slice 2 :func:`_record_planning_resource` behavior).

    Args:
        connection: The caller's SQLAlchemy connection. The registry
            row is INSERTed inside this transaction so it rolls back
            with the caller's domain rows on failure (AD-WS-5).
        registry_kind: The value written to ``Identifier_Registry.kind``.
            Must be one of
            :data:`walking_slice.identity.ALLOWED_IDENTIFIER_KINDS`
            (in practice for Slice 3: ``'immutable_record'`` for the
            six Execution_Service Record kinds, ``'resource'`` for a
            produced Deliverable Resource, and ``'revision'`` for a
            produced Deliverable Revision).
        resource_kind: The value written to
            ``Identifier_Registry.resource_kind``. Must be one of
            :data:`EXECUTION_RESOURCE_KINDS`.
        identifier: The canonical UUIDv7 string to bind. The
            IdentityService validates the form before any SQL is
            issued.
        content_digest: The digest the identifier is being bound to.
            Typically a SHA-256 hex string over the canonical payload of
            the Record or Revision; opaque to this helper.
        identity_service: The Slice 1 :class:`IdentityService` instance
            that owns the in-memory and persistent registry surface.
            Keyword-only to keep call sites at the execution and
            deliverable modules explicit about which collaborator they
            are passing through.
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
            (e.g., ``"create.work_assignment"``,
            ``"create.produced_deliverable"``) so audit consumers can
            map the denial back to the originating Execution_Service or
            Deliverable_Repository request.
        recorded_time: Optional explicit timestamp for both the
            registry row's ``issued_at`` and the denial record's
            ``recorded_at``. When omitted, the IdentityService's
            injected clock supplies a value; when neither is available
            the system UTC clock is consulted as a last resort.

    Raises:
        ValueError: *registry_kind* is missing or not in
            :data:`ALLOWED_IDENTIFIER_KINDS`, or *resource_kind* is not
            in :data:`EXECUTION_RESOURCE_KINDS`.
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
    if resource_kind not in EXECUTION_RESOURCE_KINDS:
        raise ValueError(
            f"unknown execution resource_kind {resource_kind!r}; "
            f"expected one of {sorted(EXECUTION_RESOURCE_KINDS)}."
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

    Precedence mirrors :meth:`IdentityService._resolve_recorded_at` and
    the Slice 2 sibling
    :func:`walking_slice.planning._helpers._resolve_issued_at`:
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
