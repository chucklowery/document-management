"""Shared helpers for the fourth-walking-slice Outcome_Service modules.

Design references:

- ``.kiro/specs/fourth-walking-slice/design.md`` §"Cross-Cutting Concerns"
  (*Attribute guard*) — the shared
  :func:`_reject_prohibited_attributes` helper rejects any top-level
  request key matching a prohibited intended-side prefix
  (``success-condition-``, ``attribution-assumption-``, ``planned-``,
  ``plan-review-``, ``plan-approval-``, ``milestone-acceptance-outcome-``,
  ``completion-outcome-``, ``intended-``) or any field whose stated
  purpose is to assert Outcome from Completion or to alias a Completion
  Record as an Observed Outcome, returning a 400 with no row persisted
  (Requirements 53, 54).
- ``.kiro/specs/fourth-walking-slice/design.md`` §"Cross-Cutting Concerns"
  (*Identifiers*) — every new identity is a UUIDv7 minted by the existing
  :class:`~walking_slice.identity.IdentityService` and registered in
  ``Identifier_Registry`` with ``kind ∈ {'resource', 'revision',
  'immutable_record'}`` and ``resource_kind`` set to one of the seven
  Slice 4 values from AD-WS-37
  (:data:`walking_slice.outcome._persistence.OUTCOME_RESOURCE_KINDS`).
- ``.kiro/specs/fourth-walking-slice/design.md`` §"Architectural
  Decisions — AD-WS-37" (per-Record-kind tables; seven new
  ``resource_kind`` values on the existing ``Identifier_Registry``
  column).
- Slice 2 sibling: :mod:`walking_slice.planning._helpers`.
  Slice 3 sibling: :mod:`walking_slice.execution._helpers`.
  The :func:`_reject_prohibited_attributes` helper here is the additive
  Slice 4 analogue of those helpers; the matching contract
  (case-insensitive, hyphen/underscore-invariant, prefix-based, raises
  with the offending keys listed) is identical so every Slice 1 +
  Slice 2 + Slice 3 + Slice 4 endpoint shares the same rejection shape
  (Properties 50, 54).

Responsibility of this module (task 3.2):

1. Expose :func:`_reject_prohibited_attributes`, the cross-cutting
   request-body validator every Outcome_Service endpoint uses to
   enforce the Intended/Observed separation (Requirement 53) and the
   Output-is-not-Outcome separation (Requirement 54) from the outcome
   side. The function raises :class:`OutcomeValidationError` identifying
   every top-level request key whose name begins with one of the
   prohibited intended-side prefixes in
   :data:`OUTCOME_PROHIBITED_PREFIXES`, **or** whose name expresses the
   stated purpose of asserting Outcome from Completion alone or aliasing
   a Completion Record as an Observed Outcome
   (:data:`COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS`, Requirement 54.4).

   Field-name matching is invariant under hyphen/underscore swaps and
   case — the Outcome_Service accepts both ``planned_scope`` (the
   Python/JSON snake-case convention) and ``planned-scope`` (the spec's
   hyphenated prose) and rejects both.

2. Expose :func:`_record_outcome_artifact`, the small helper every
   Outcome_Service module calls when it has minted a new Resource,
   Revision, or Immutable Record identity. The helper:

   - validates canonical UUIDv7 form via
     :class:`~walking_slice.identity.IdentityService` (delegating to
     :meth:`IdentityService.reject_if_duplicate`),
   - on conflict (same identifier already bound to a different digest)
     re-raises :class:`~walking_slice.identity.IdentityConflictError`
     after the IdentityService has appended the separate-transaction
     Denial Record per Slice 1 design §"Error Handling — Identifier
     conflict",
   - on success INSERTs one row into ``Identifier_Registry`` carrying
     the Slice 4 ``resource_kind`` tag so the seven Slice 4 identifier
     roles remain pairwise disjoint relative to every Slice 1, Slice 2,
     and Slice 3 identifier (Requirement 43.8).

Requirements satisfied (per task 3.2):

    43.1  — every Slice 4 Resource, Revision, and Record receives a
            durable UUIDv7 identity minted by the existing
            :class:`IdentityService`; the helper drives the registry
            binding so the identity is durably recorded.
    43.4  — re-presenting an already-bound identifier with a different
            content digest is rejected (the duplicate-rejection path).
    43.8  — every Slice 4 identifier carries a ``resource_kind`` tag
            drawn from the seven Slice 4 values so the seven Slice 4
            identifier roles remain pairwise disjoint relative to every
            Slice 1, Slice 2, and Slice 3 identifier.
    53.2  — no Measurement Definition, Measurement Record, Observed
            Outcome, Success-Condition Assessment, or Outcome Review
            creation request may carry an intended-side attribute; the
            prefix guard rejects every such key at the API boundary.
    53.3  — the response identifies every prohibited intended-side
            attribute via :attr:`OutcomeValidationError.prohibited_keys`.
    54.1  — no Slice 4 entity is derived automatically from a Completion
            Record; this helper is the boundary guard that rejects any
            field whose stated purpose is to assert that completion
            alone satisfies the addressed Intended Outcome.
    54.4  — a creation request naming a field whose stated purpose is to
            assert Outcome from Completion alone, or to alias a
            Completion Record as an Observed Outcome, is rejected with no
            Resource, Revision, or Record created, identifying each
            prohibited attribute.
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
from walking_slice.outcome._persistence import OUTCOME_RESOURCE_KINDS


__all__ = [
    "OUTCOME_PROHIBITED_PREFIXES",
    "COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS",
    "OUTCOME_RESOURCE_KINDS",
    "OutcomeValidationError",
    "_record_outcome_artifact",
    "_reject_prohibited_attributes",
]


# ---------------------------------------------------------------------------
# Prohibited intended-side prefix set (Requirement 53).
#
# Sourced verbatim from design §"Cross-Cutting Concerns — Attribute guard":
#
#   "rejects any top-level request key matching a prohibited intended-side
#    prefix (success-condition-, attribution-assumption-, planned-,
#    plan-review-, plan-approval-, milestone-acceptance-outcome-,
#    completion-outcome-, intended-)"
#
# These eight prefixes cover the intended-side facts enumerated by
# Requirement 53.2 (an `intended` outcome_kind value, a success-condition
# statement, attribution-assumption text, a planned-deliverable reference,
# a plan-review outcome, a plan-approval outcome, a Milestone Acceptance
# outcome, and a Completion outcome) that must never be written to any
# outcome-measurement Resource, Revision, or Record other than via the
# explicit ``Addresses`` / ``Cites`` Identity references named in
# Requirements 44 through 49.
#
# All prefixes are stored in canonical hyphen-lowercase form. Matching is
# case-insensitive and hyphen/underscore-invariant — see
# :func:`_normalize_key`.
# ---------------------------------------------------------------------------


OUTCOME_PROHIBITED_PREFIXES: Final[tuple[str, ...]] = (
    "success-condition-",
    "attribution-assumption-",
    "planned-",
    "plan-review-",
    "plan-approval-",
    "milestone-acceptance-outcome-",
    "completion-outcome-",
    "intended-",
)
"""Intended-side attribute prefixes rejected on every Outcome_Service request.

Drawn verbatim from design §"Cross-Cutting Concerns — Attribute guard"
(grounded in Requirement 53.2's enumeration of forbidden intended-side
facts on outcome-measurement Resources, Revisions, and Records):

- ``success-condition-`` covers any attempt to assert or mutate a
  success-condition statement (a Slice 2 Intended Outcome attribute).
- ``attribution-assumption-`` covers attribution-assumption text (a
  Slice 2 Intended Outcome attribute), distinct from the
  ``attribution_stance`` / attribution-evidence the Outcome Review
  legitimately carries via Requirement 49.
- ``planned-`` covers ``planned_deliverable_*`` and every other planned
  reference (a Slice 2 planning fact).
- ``plan-review-`` covers Plan Review outcomes (Slice 2 Requirement 7).
- ``plan-approval-`` covers Plan Approval outcomes (Slice 2 Requirement
  9).
- ``milestone-acceptance-outcome-`` covers Slice 3 Milestone Acceptance
  outcomes.
- ``completion-outcome-`` covers Slice 3 Completion outcomes asserted as
  outcome-measurement facts.
- ``intended-`` covers any field naming an ``intended`` outcome_kind
  value (the addressed Intended Outcome Revision Identity itself is the
  only permitted reference and is carried through the explicit
  ``Addresses`` Identity reference, not a prefixed attribute).
"""


# ---------------------------------------------------------------------------
# Completion-as-Outcome intent detection (Requirement 54).
#
# Requirement 54.4 additionally requires rejecting "any field whose stated
# purpose is to assert that a Slice 3 Completion Record by itself satisfies
# the addressed Intended Outcome, or to alias a Slice 3 Completion Record as
# an Observed Outcome". Unlike the prefix guard (which matches the *start*
# of a key), these intent markers are matched as substrings anywhere in the
# normalized key so they catch the semantic regardless of how the field is
# qualified (e.g., ``mark_completion_as_observed_outcome``,
# ``treat-completion-satisfies-outcome``).
#
# Each marker is stored in canonical hyphen-lowercase form; matching is
# case-insensitive and hyphen/underscore-invariant via :func:`_normalize_key`.
#
# The permitted cross-slice reference — the explicit ``Cites`` Identity
# reference to a Completion Record an Outcome Review carries per Requirement
# 49 — is a plain identity field (e.g., ``cited_completion_record_ids``) and
# contains none of these intent markers, so it is never rejected.
# ---------------------------------------------------------------------------


COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS: Final[tuple[str, ...]] = (
    "completion-satisfies",
    "completion-as-outcome",
    "completion-as-observed",
    "completion-is-outcome",
    "completion-is-observed",
    "outcome-from-completion",
    "observed-from-completion",
    "satisfied-by-completion",
    "satisfies-intended-outcome",
    "alias-completion",
    "derive-outcome-from-completion",
)
"""Substring markers that express a forbidden Completion-as-Outcome intent.

Drawn from Requirement 54.4 / Requirement 54.1 and design §"Cross-Cutting
Concerns — Attribute guard" ("…or any field whose stated purpose is to
assert Outcome from Completion or to alias a Completion Record as an
Observed Outcome"). Matched as substrings (not prefixes) so a field's
semantic intent is caught regardless of any leading qualifier.
"""


# ---------------------------------------------------------------------------
# Errors raised by the helpers.
# ---------------------------------------------------------------------------


class OutcomeValidationError(ValueError):
    """Raised when an Outcome_Service request body fails shared validation.

    The primary use is :func:`_reject_prohibited_attributes`: when one
    or more top-level keys match a prohibited intended-side prefix
    (Requirement 53.2) or a Completion-as-Outcome intent marker
    (Requirement 54.4), the error carries the offending keys verbatim on
    :attr:`prohibited_keys` so the route layer can return them in the
    response body per Requirement 53.3 ("…return an error indication
    identifying each prohibited attribute…") and Requirement 54.4
    ("…return an error indication identifying each prohibited
    attribute…").

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
    the same prefix ``planned-``. The Outcome_Service also treats keys
    case-insensitively. The canonical form for matching is lowercase
    with underscores rewritten as hyphens, matching the Slice 2 / Slice 3
    convention in
    :func:`walking_slice.planning._helpers._normalize_key` and
    :func:`walking_slice.execution._helpers._normalize_key`.
    """
    return key.lower().replace("_", "-")


def _reject_prohibited_attributes(
    request_body: Mapping[str, Any],
    prefixes: Iterable[str],
) -> None:
    """Reject the request body if any top-level key is prohibited.

    A key is prohibited when it matches one of two rules:

    1. **Prefix rule (Requirement 53.2).** The normalized key begins
       with one of *prefixes* (typically
       :data:`OUTCOME_PROHIBITED_PREFIXES`). This rejects intended-side
       attributes — success-condition statements, attribution-assumption
       text, planned-deliverable references, plan-review / plan-approval
       outcomes, Milestone Acceptance / Completion outcomes, and any
       ``intended``-prefixed field — on every Outcome_Service request.

    2. **Intent rule (Requirement 54.4).** The normalized key contains
       one of :data:`COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS`. This
       rejects any field whose stated purpose is to assert that a
       Completion Record by itself satisfies the addressed Intended
       Outcome, or to alias a Completion Record as an Observed Outcome.
       The intent rule is always applied regardless of *prefixes* because
       the Output-is-not-Outcome separation (Requirement 54) holds for
       every Slice 4 creation request.

    Matching is case-insensitive and hyphen/underscore-invariant:
    ``planned_scope``, ``Planned-Scope``, and ``planned-scope`` are all
    rejected by the ``planned-`` prefix.

    Args:
        request_body: The top-level mapping the route layer received.
            Typically the result of ``request_model.model_dump()`` from
            a Pydantic ``Config(extra='forbid')`` request model, but any
            :class:`collections.abc.Mapping` is accepted so callers can
            pass raw JSON-decoded ``dict``s as well.
        prefixes: An iterable of prohibited prefix strings. Pass
            :data:`OUTCOME_PROHIBITED_PREFIXES` for the canonical Slice 4
            surface. Each prefix is normalized the same way as request
            keys before matching. An empty iterable disables the prefix
            rule but leaves the intent rule active.

    Raises:
        OutcomeValidationError: At least one top-level key matched the
            prefix rule or the intent rule.
            :attr:`OutcomeValidationError.prohibited_keys` carries every
            offending key in the order they appeared in *request_body*;
            the route layer surfaces this tuple in the response per
            Requirements 53.3 and 54.4.
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

    offending: list[str] = []
    for key in request_body.keys():
        if not isinstance(key, str):
            # Pydantic models produce ``str`` keys only; skip anything
            # else defensively so we do not raise a misleading TypeError
            # from .lower() on a non-str key.
            continue
        normalized_key = _normalize_key(key)

        matched = False
        for prefix in normalized_prefixes:
            if normalized_key.startswith(prefix):
                matched = True
                break
        if not matched:
            for marker in COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS:
                if marker in normalized_key:
                    matched = True
                    break

        if matched:
            offending.append(key)

    if offending:
        raise OutcomeValidationError(
            f"request body contains prohibited attribute(s) {offending!r}; "
            "outcome-measurement Resources, Revisions, and Records may not "
            "carry intended-side fields, nor any field asserting Outcome "
            "from Completion alone or aliasing a Completion Record as an "
            "Observed Outcome (Requirements 53.2, 53.3, 54.1, 54.4).",
            prohibited_keys=tuple(offending),
        )


def _record_outcome_artifact(
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
    """Register a Slice 4 outcome-measurement artifact identifier in
    ``Identifier_Registry``.

    Performs two steps per design §"Cross-Cutting Concerns"
    (*Identifiers*) and AD-WS-37:

    1. **Conflict detection.** Calls
       :meth:`IdentityService.reject_if_duplicate` to validate the
       canonical UUIDv7 form and reject any attempt to re-bind an
       existing identifier to a different digest. On conflict the
       IdentityService appends a Denial Record from a separate
       transaction (Slice 1 design §"Error Handling — Identifier
       conflict") and raises
       :class:`~walking_slice.identity.IdentityConflictError`, which
       propagates out of this helper so the caller's originating
       transaction rolls back (Requirement 43.4).

    2. **Tagged INSERT.** When the identifier is not yet bound, INSERTs
       one row into ``Identifier_Registry`` carrying the
       ``resource_kind`` tag (and the registry-level ``kind``
       discriminator the Slice 1 ``CHECK`` constraint validates) inside
       the caller's transaction per AD-WS-5. The Slice 4 ``resource_kind``
       value is the single row-level fact that lets Requirement 43.8
       (seven disjoint Slice 4 identifier roles, pairwise disjoint
       relative to every Slice 1, Slice 2, and Slice 3 identifier)
       remain inspectable on the registry table without requiring a
       join.

    When the identifier is already bound to *content_digest*, the call
    is idempotent — no new row is INSERTed and no audit row is appended
    (matching the IdentityService re-confirmation contract and the
    Slice 2 :func:`walking_slice.planning._helpers._record_planning_resource`
    and Slice 3
    :func:`walking_slice.execution._helpers._record_execution_artifact`
    behavior).

    Args:
        connection: The caller's SQLAlchemy connection. The registry
            row is INSERTed inside this transaction so it rolls back
            with the caller's domain rows on failure (AD-WS-5).
        registry_kind: The value written to ``Identifier_Registry.kind``.
            Must be one of
            :data:`walking_slice.identity.ALLOWED_IDENTIFIER_KINDS`
            (in practice for Slice 4: ``'resource'`` for a Measurement
            Definition or Observed Outcome Resource, ``'revision'`` for a
            Measurement Definition Revision or Observed Outcome Revision,
            and ``'immutable_record'`` for a Measurement Record,
            Success-Condition Assessment Record, or Outcome Review
            Record).
        resource_kind: The value written to
            ``Identifier_Registry.resource_kind``. Must be one of
            :data:`OUTCOME_RESOURCE_KINDS`.
        identifier: The canonical UUIDv7 string to bind. The
            IdentityService validates the form before any SQL is
            issued.
        content_digest: The digest the identifier is being bound to.
            Typically a SHA-256 hex string over the canonical payload of
            the Resource, Revision, or Record; opaque to this helper.
        identity_service: The Slice 1 :class:`IdentityService` instance
            that owns the in-memory and persistent registry surface.
            Keyword-only to keep call sites at the outcome modules
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
            (e.g., ``"create.measurement_definition"``,
            ``"create.outcome_review"``) so audit consumers can map the
            denial back to the originating Outcome_Service request.
        recorded_time: Optional explicit timestamp for both the
            registry row's ``issued_at`` and the denial record's
            ``recorded_at``. When omitted, the IdentityService's
            injected clock supplies a value; when neither is available
            the system UTC clock is consulted as a last resort.

    Raises:
        ValueError: *registry_kind* is missing or not in
            :data:`ALLOWED_IDENTIFIER_KINDS`, or *resource_kind* is not
            in :data:`OUTCOME_RESOURCE_KINDS`.
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
    if resource_kind not in OUTCOME_RESOURCE_KINDS:
        raise ValueError(
            f"unknown outcome resource_kind {resource_kind!r}; "
            f"expected one of {sorted(OUTCOME_RESOURCE_KINDS)}."
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
    the Slice 2 / Slice 3 siblings
    :func:`walking_slice.planning._helpers._resolve_issued_at` and
    :func:`walking_slice.execution._helpers._resolve_issued_at`:
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
