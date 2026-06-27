"""Knowledge_Service — Findings, Finding_Revisions, and typed Relationships
(``Supports``, ``Contradicts``).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Knowledge_Service" + §"Findings and Finding_Revisions"
+ §"Relationships", AD-WS-4 (immutable Finding_Revisions and
Relationships rows), AD-WS-5 (audit append inside the originating
transaction), and AD-WS-7 (Relationships are immutable assertions —
``Supports``, ``Contradicts``, ``Derived From``, ``Addresses``).

Task scope (task 6.1):

- :meth:`KnowledgeService.create_finding` — record a Finding (Resource +
  first Revision) plus one ``Supports`` Relationship per cited Content
  Region Occurrence, inside one transaction (AD-WS-5). Non-hypothesis
  Findings *must* cite at least one Region Occurrence; hypothesis
  Findings may cite zero (Requirement 4.3). Each cited Region
  Occurrence is verified against the composite PK
  ``Region_Occurrences(region_id, document_revision_id)`` before any
  domain row is written — unresolved citations raise
  :class:`FindingNotResolvableError` and leave no partial state.
- :meth:`KnowledgeService.record_contradiction` — record a
  ``Contradicts`` Relationship between an existing Finding Revision and
  an existing Finding Resource. Both Finding records remain unchanged
  (the Finding_Revisions table is append-only, AD-WS-4); the
  contradiction is one new immutable Relationships row plus one
  consequential audit row.

Relationship payload (Requirement 4.2):

Every ``Supports`` or ``Contradicts`` Relationships row records:

- ``relationship_id`` — a fresh UUIDv7 from
  :class:`~walking_slice.identity.IdentityService`.
- ``relationship_type`` — ``'Supports'`` or ``'Contradicts'``.
- ``source_kind`` — always ``'finding_revision'`` for Findings in this
  task.
- ``source_id`` — the Finding's Resource Identity.
- ``source_revision_id`` — the Finding Revision Identity.
- ``target_kind`` — ``'region_occurrence'`` for ``Supports``,
  ``'finding'`` for ``Contradicts``.
- ``target_id`` — Region Identity (``Supports``) or target Finding
  Identity (``Contradicts``).
- ``target_revision_id`` — Document Revision Identity owning the
  Region Occurrence (``Supports``); ``NULL`` for ``Contradicts``
  because Requirement 4.4 keys the relationship on the Finding
  Resource, not a specific Finding Revision.
- ``authoring_party_id`` — the Party that recorded the relationship.
- ``recorded_at`` — UTC ISO-8601 with millisecond precision sourced
  from the injected :class:`~walking_slice.clock.Clock`.

Requirements satisfied:
    4.1 — non-hypothesis Findings require at least one ``Supports``
          Relationship targeting a Content Region Occurrence; hypothesis
          Findings may have zero supports.
    4.2 — every Relationship records source Resource Identity, source
          Revision Identity, target Identity, Relationship Type,
          authoring Party, and recorded time (millisecond precision).
    4.3 — non-hypothesis Finding with zero supports is rejected; no
          Findings, Finding_Revisions, or Relationships row is written.
    4.4 — ``record_contradiction`` records a ``Contradicts`` Relationship
          and leaves both source and target Finding records
          byte-equivalent to their prior state (Finding_Revisions is
          append-only per AD-WS-4).
    4.5 — a Finding citing multiple Region Occurrences produces one
          ``Supports`` Relationship per cited Occurrence.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Final, Literal, Optional, Sequence

import uuid_utils
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.identity import IdentityService
from walking_slice.manifests import (
    IncludedSource,
    ManifestValidationError,
    OmissionEntry as ManifestOmissionEntry,
    ProvenanceManifestWriter,
    StalenessError,
)
from walking_slice.models import AuthorityBasisRef


__all__ = [
    "CreateDecisionResult",
    "CreateFindingResult",
    "CreateRecommendationResult",
    "CreateRelationshipResult",
    "DecisionAuditFailureError",
    "DecisionAuthorizationError",
    "DecisionConflictError",
    "DecisionOmissionEntry",
    "DecisionRecord",
    "DecisionValidationError",
    "FindingNotFoundError",
    "FindingNotResolvableError",
    "FindingValidationError",
    "KnowledgeService",
    "RecommendationAuthorizationError",
    "RecommendationNotResolvableError",
    "RecommendationRevisionNotResolvableError",
    "RecommendationValidationError",
    "SupportRef",
]


# ---------------------------------------------------------------------------
# Constants — action names and relationship type names.
#
# These are pulled out as module-level ``Final`` constants so the action
# names that downstream property tests (Property 11 — audit completeness)
# look for in ``Audit_Records.action_type`` are textually stable and the
# Relationship Type strings stay aligned with the CHECK constraint on
# ``Relationships.relationship_type`` in
# :mod:`walking_slice.persistence`.
# ---------------------------------------------------------------------------


_AUDIT_ACTION_CREATE_FINDING: Final[str] = "create.finding"
_AUDIT_ACTION_RECORD_CONTRADICTION: Final[str] = "record.contradiction"
_AUDIT_ACTION_CREATE_RECOMMENDATION: Final[str] = "create.recommendation"
_AUDIT_ACTION_CREATE_DECISION: Final[str] = "create.decision"

# Authorization action name passed to ``AuthorizationService.evaluate`` for
# Decision creation per design §"Authorization_Service" (ActionType
# enumeration). The ``approve.*`` prefix maps to the ``approve`` authority
# type (Requirement 12.4 — no substitution between view/modify/approve).
# This is *also* the ``attempted_action`` recorded on the separate-
# transaction Denial Record written by
# :meth:`KnowledgeService._persist_decision_denial` so operators can
# correlate the denial row in the audit log with the action a Party was
# attempting (Requirement 7.2).
_AUTHORIZATION_ACTION_APPROVE_DECISION: Final[str] = "approve.decision"

# Exponential backoff sequence for retrying the Denial Record append in a
# separate transaction (Requirement 7.6 — "retry up to 3 times"). The
# three values give three retries after the initial attempt, for a total
# of four attempts; the *initial* attempt happens at index 0 of
# :meth:`KnowledgeService._persist_decision_denial`'s loop, and each
# successive index pauses by the corresponding entry in this tuple before
# trying again. If every attempt fails, the helper raises
# :class:`DecisionAuditFailureError` so the caller (and the HTTP layer in
# task 8.3) can surface the audit-failure indicator to the operator —
# Requirement 7.6 makes denial and audit divergence a first-class signal
# rather than a silent loss.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


_RELATIONSHIP_TYPE_SUPPORTS: Final[str] = "Supports"
_RELATIONSHIP_TYPE_CONTRADICTS: Final[str] = "Contradicts"
_RELATIONSHIP_TYPE_DERIVED_FROM: Final[str] = "Derived From"
_RELATIONSHIP_TYPE_ADDRESSES: Final[str] = "Addresses"


# Resource / Revision / target ``kind`` strings written to ``Relationships``.
# Centralized here so the strings cannot drift between the Supports and
# Contradicts writers, and so Property 3 (Backlink bidirectionality) can
# scan for inbound Relationships by a constant kind value.
_KIND_FINDING_REVISION: Final[str] = "finding_revision"
_KIND_FINDING: Final[str] = "finding"
_KIND_REGION_OCCURRENCE: Final[str] = "region_occurrence"
_KIND_RECOMMENDATION_REVISION: Final[str] = "recommendation_revision"
_KIND_DECISION: Final[str] = "decision"


# Recommendation validation limits per Requirement 5.1, 5.3, 5.4, 5.5.
# Centralized so unit tests (test_knowledge_recommendations.py) and the
# HTTP layer added by task 7.2 read from a single source of truth.
_DERIVED_FROM_MIN: Final[int] = 1
_DERIVED_FROM_MAX: Final[int] = 50
_RATIONALE_MAX_CHARS: Final[int] = 10_000
_ASSUMPTION_MAX_CHARS: Final[int] = 2_000
_ASSUMPTIONS_MAX_ENTRIES: Final[int] = 50
_CONFIDENCE_VALUES: Final[frozenset[str]] = frozenset({"Low", "Medium", "High"})

# Authorization action name passed to ``AuthorizationService.evaluate`` for
# Recommendation creation per design §"Authorization_Service" (ActionType
# enumeration). The ``create.*`` prefix maps to the ``modify`` authority
# type (Requirement 12.4 — no substitution between authority types); a
# Party with effective Analyst role for ``applicable_scope`` therefore
# permits a Recommendation create.
_AUTHORIZATION_ACTION_CREATE_RECOMMENDATION: Final[str] = "create.recommendation"

# Target ``kind`` recorded on the authorization evaluation row for a
# Recommendation creation. Distinct from ``recommendation_revision`` (used
# on Relationships) because at evaluation time the Revision Identity has
# not yet been minted; the audit row therefore records the kind of
# Resource being created rather than the Revision identity.
_AUTHORIZATION_TARGET_KIND_RECOMMENDATION: Final[str] = "recommendation"


# Decision validation limits per Requirement 6.2 and AD-WS-10 / AD-WS-11.
# ``Decisions.rationale`` is a NOT NULL TEXT column persisting 1..4,000
# characters; the schema does not encode the upper bound as a CHECK so
# the limit is enforced here (and surfaced through
# :class:`DecisionValidationError` so the HTTP layer added by task 8.3
# can render a precise 400 response).
_DECISION_RATIONALE_MIN_CHARS: Final[int] = 1
_DECISION_RATIONALE_MAX_CHARS: Final[int] = 4_000

# The slice's enumerated Decision outcomes per AD-WS-11 (closes Gap G-6).
# Supersession is intentionally excluded from this slice; widening this
# set requires updating the design document, the schema CHECK constraint
# on ``Decisions.outcome``, and this validator together.
_DECISION_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"Accept", "Reject", "Defer"}
)

# The slice's enumerated authority-basis types per AD-WS-10 (closes
# Gap G-5). Mirrors the CHECK constraint on
# ``Decisions.authority_basis_type`` and the ``Literal`` annotation on
# :class:`walking_slice.models.AuthorityBasisRef.type`. The three
# values name the kind of authority a Decision Maker invoked at the
# time of the Decision; widening this set requires updating the
# design document, the schema, the value object, and this validator
# together.
_AUTHORITY_BASIS_TYPES: Final[frozenset[str]] = frozenset(
    {"role-grant-id", "scope-id", "delegation-chain-id"}
)

# Provenance Manifest ``subject_kind`` for a Decision Manifest per the
# CHECK constraint on ``Provenance_Manifests.subject_kind`` and design
# §"Provenance_Manifests and Omission_Entries". The subject of a
# Decision manifest is the Decision Immutable Record itself —
# ``subject_revision_id`` is NULL because a Decision has no revisions
# (it is itself an Immutable Record, AD-WS-4).
_MANIFEST_SUBJECT_KIND_DECISION: Final[str] = "decision"

# Categories permitted on Omission Entries per the CHECK constraint on
# ``Omission_Entries.category`` and Requirement 10.3. Mirrored here so
# :meth:`KnowledgeService.create_decision` can reject malformed entries
# before the database round-trip and surface a precise constraint name.
_OMISSION_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"intentional", "unavailable", "restricted", "stale", "unresolved"}
)

# Per Requirement 10.2 and the schema constraint on
# ``Omission_Entries.rationale``: 1..2,000 characters. The lower bound
# (non-empty) is the operative one since the column is NOT NULL.
_OMISSION_RATIONALE_MIN_CHARS: Final[int] = 1
_OMISSION_RATIONALE_MAX_CHARS: Final[int] = 2_000


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class FindingValidationError(ValueError):
    """Raised when a Finding submission fails Requirement 4.1 / 4.3 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    added by task 6.2 can render a structured 400 response and tests
    can assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of ``"statement_empty"``,
            ``"authoring_party_id_missing"``,
            ``"supports_required_for_non_hypothesis"``.
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


class FindingNotResolvableError(LookupError):
    """Raised when a cited Region Occurrence does not exist.

    Requirement 4.1 demands that a ``Supports`` Relationship's target
    *be* a Content Region Occurrence — by composite PK
    ``Region_Occurrences(region_id, document_revision_id)``. A non-
    resolvable citation is rejected before any Findings,
    Finding_Revisions, or Relationships row is written so the database
    cannot end up with a dangling reference (and tests can assert no
    partial persistence).

    Attributes:
        region_id: The Region Identity supplied by the caller.
        document_revision_id: The Document Revision Identity supplied
            by the caller. The pair ``(region_id, document_revision_id)``
            is the composite PK that did not resolve.
    """

    def __init__(self, *, region_id: str, document_revision_id: str) -> None:
        super().__init__(
            f"No Region_Occurrences row for region_id={region_id!r}, "
            f"document_revision_id={document_revision_id!r}."
        )
        self.region_id = region_id
        self.document_revision_id = document_revision_id


class FindingNotFoundError(LookupError):
    """Raised by :meth:`KnowledgeService.record_contradiction` when one
    of the referenced Findings does not exist.

    ``role`` identifies which side of the contradiction failed to
    resolve: ``"source"`` when ``source_finding_revision_id`` does not
    name a Finding_Revisions row, ``"target"`` when
    ``target_finding_id`` does not name a Findings row. Tests use this
    to verify that a partial write never happens — neither side can be
    silently created.
    """

    def __init__(self, *, role: str, identifier: str) -> None:
        super().__init__(
            f"{role.capitalize()} Finding reference {identifier!r} does not "
            f"resolve to an existing record."
        )
        self.role = role
        self.identifier = identifier


class RecommendationValidationError(ValueError):
    """Raised when a Recommendation submission fails Requirement 5.x validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    added by task 7.2 can render a structured 400 response and tests
    can assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"authoring_party_id_missing"``,
            ``"derived_from_too_few"`` (zero entries supplied),
            ``"derived_from_too_many"`` (>50 entries supplied),
            ``"rationale_empty"`` (empty string explicitly supplied),
            ``"rationale_too_long"`` (>10,000 characters),
            ``"assumption_empty"`` (any entry is empty string),
            ``"assumption_too_long"`` (any entry >2,000 characters),
            ``"assumptions_too_many"`` (>50 entries),
            ``"confidence_invalid"`` (value not in {Low, Medium, High}),
            ``"invalid_derived_from"`` (a Finding ID does not resolve).
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


class RecommendationNotResolvableError(LookupError):
    """Raised when a ``derived_from`` Finding Identity does not resolve.

    Per Requirement 5.6: "IF an Analyst attempts to record a
    Recommendation with zero ``Derived From`` Relationships or with any
    ``Derived From`` reference that does not resolve to an existing
    Finding, THEN THE Knowledge_Service SHALL reject the action, decline
    to create any Resource or Revision…".

    Like :class:`FindingNotResolvableError`, this exception is raised
    *before* any Recommendations, Recommendation_Revisions, or
    Relationships row is written so the database cannot end up with a
    dangling reference. The exception is *also* a
    :class:`RecommendationValidationError` so callers that want to
    treat every Requirement 5.x rejection uniformly can catch a single
    base type — and the unique ``failed_constraint`` value
    ``"invalid_derived_from"`` (mandated by the task description)
    lets callers branch on it when they need to.

    Attributes:
        finding_id: The Finding Identity the caller supplied that did
            not resolve to an existing ``Findings`` row.
    """

    def __init__(self, *, finding_id: str) -> None:
        super().__init__(
            f"No Findings row for finding_id={finding_id!r}; the Derived "
            f"From reference cannot be resolved."
        )
        self.finding_id = finding_id
        # ``failed_constraint`` mirrors the
        # :class:`RecommendationValidationError` attribute so HTTP-layer
        # callers (task 7.2) can render every Requirement 5.x rejection
        # through one error envelope.
        self.failed_constraint = "invalid_derived_from"


class RecommendationAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies Recommendation creation.

    Per Requirement 5.7: "IF the requester is unauthenticated or does
    not hold an effective Analyst role for the applicable scope, THEN
    THE Knowledge_Service SHALL reject the Recommendation creation,
    decline to create any Resource or Revision, and return an
    authorization-denial response, per REQ-IG-002 and REQ-IG-003."

    The exception carries the ``reason_code`` and ``correlation_id``
    returned by :meth:`AuthorizationService.evaluate` so the HTTP layer
    (task 7.2) can render the indistinguishable denial response shape
    from design AD-WS-9 without re-deriving the values.

    Attributes:
        reason_code: One of
            ``{not-yet-effective, expired, revoked, out-of-scope,
            no-role-assignment}`` per Requirement 7.2 / 12.2.
        correlation_id: Operation correlation identifier shared with the
            audit row :meth:`AuthorizationService.evaluate` already
            appended for this denial.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Recommendation creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class DecisionValidationError(ValueError):
    """Raised when a Decision submission fails Requirement 6.x validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    added by task 8.3 can render a structured 400 response and tests
    can assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"target_recommendation_id_missing"``,
            ``"target_recommendation_revision_id_missing"``,
            ``"deciding_party_id_missing"``,
            ``"applicable_scope_missing"``,
            ``"outcome_invalid"`` (value not in {Accept, Reject, Defer}),
            ``"rationale_missing"`` (empty or non-string),
            ``"rationale_too_long"`` (>4,000 characters),
            ``"authority_basis_missing"``,
            ``"authority_basis_type_invalid"``,
            ``"omission_category_invalid"``,
            ``"omission_rationale_missing"``,
            ``"omission_rationale_too_long"``,
            ``"omission_excluded_source_id_missing"``.
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


class RecommendationRevisionNotResolvableError(LookupError):
    """Raised when the target Recommendation Revision does not exist.

    Requirement 6.1 requires a Decision to target an existing
    Recommendation Revision; if the supplied pair
    ``(target_recommendation_id, target_recommendation_revision_id)``
    does not resolve to a ``Recommendation_Revisions`` row whose
    ``recommendation_id`` matches the supplied Resource Identity, this
    exception is raised *before* any Decision, Addresses Relationship,
    Provenance Manifest, Omission Entry, or audit row is written. The
    caller's transaction is therefore untouched.

    Attributes:
        target_recommendation_id: The Recommendation Resource Identity
            the caller supplied.
        target_recommendation_revision_id: The Recommendation Revision
            Identity the caller supplied. The pair did not resolve in
            ``Recommendation_Revisions``.
    """

    def __init__(
        self,
        *,
        target_recommendation_id: str,
        target_recommendation_revision_id: str,
    ) -> None:
        super().__init__(
            f"No Recommendation_Revisions row for recommendation_id="
            f"{target_recommendation_id!r}, recommendation_revision_id="
            f"{target_recommendation_revision_id!r}."
        )
        self.target_recommendation_id = target_recommendation_id
        self.target_recommendation_revision_id = target_recommendation_revision_id


class DecisionConflictError(LookupError):
    """Raised when a Decision already exists for the target Recommendation Revision.

    Requirement 6.5 ("IF a Party submits a Decision for a Recommendation
    Revision that is already the target of a finalized Decision Record,
    THEN THE Knowledge_Service SHALL reject the submission, decline to
    create a Decision Record, and return an error indication identifying
    the duplicate-decision condition") motivates this dedicated error.

    The database also enforces the rule via the
    ``UNIQUE(target_recommendation_id, target_recommendation_revision_id)``
    constraint on ``Decisions`` — so a race that slips past the
    application-level check still surfaces as an
    :class:`sqlalchemy.exc.IntegrityError`. The application-level check
    runs first because it gives the caller a structured error with the
    existing ``decision_id`` (useful for the 409 response shape from
    task 8.3) instead of a generic constraint-violation message.

    Attributes:
        target_recommendation_id: The Recommendation Resource Identity
            the caller submitted.
        target_recommendation_revision_id: The Recommendation Revision
            Identity already addressed by an existing Decision.
        existing_decision_id: The ``decision_id`` of the prior Decision
            (so callers can render an actionable error referencing the
            existing record).
    """

    def __init__(
        self,
        *,
        target_recommendation_id: str,
        target_recommendation_revision_id: str,
        existing_decision_id: str,
    ) -> None:
        super().__init__(
            f"Recommendation Revision recommendation_id="
            f"{target_recommendation_id!r}, recommendation_revision_id="
            f"{target_recommendation_revision_id!r} is already the target "
            f"of Decision decision_id={existing_decision_id!r}."
        )
        self.target_recommendation_id = target_recommendation_id
        self.target_recommendation_revision_id = target_recommendation_revision_id
        self.existing_decision_id = existing_decision_id


class DecisionAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Decision attempt.

    Per Requirement 7.1: "IF a Party attempts to finalize a Decision while
    lacking effective Decision Maker authority for the applicable scope,
    THEN THE Authorization_Service SHALL reject the action within 2
    seconds and the Knowledge_Service SHALL ensure no Decision Record is
    created, modified, or persisted."

    The exception carries the ``reason_code`` and ``correlation_id``
    returned by :meth:`AuthorizationService.evaluate` so the HTTP layer
    (task 8.3) can render the indistinguishable denial response shape
    from design AD-WS-9 without re-deriving the values. By design the
    exception exposes *only* these two attributes — Requirement 7.4
    forbids leaking authorized Party identities, Recommendation
    contents, role assignment details, or target existence beyond the
    requesting Party's view authority through the denial response.

    The shape of the exception is therefore deliberately identical
    across every internal cause: two denials produced by different
    Requirement 7.2 reason codes (``not-yet-effective``, ``expired``,
    ``revoked``, ``out-of-scope``, ``no-role-assignment``) are
    structurally indistinguishable apart from the ``reason_code`` value
    and the per-call ``correlation_id``.

    Attributes:
        reason_code: One of
            ``{not-yet-effective, expired, revoked, out-of-scope,
            no-role-assignment}`` per Requirement 7.2 / 12.2.
        correlation_id: Operation correlation identifier shared with the
            audit row :meth:`AuthorizationService.evaluate` already
            appended for this denial (in the caller's transaction, which
            is rolled back) and with the separate-transaction Denial
            Record appended by
            :meth:`KnowledgeService._persist_decision_denial`.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Decision authorization denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class DecisionAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails.

    Per Requirement 7.6: "IF the Audit_Log append for a denied Decision
    attempt fails, THEN THE Authorization_Service SHALL retry up to 3
    times, keep the action denied, and surface an audit-failure
    indicator to the operator so that denial and audit cannot silently
    diverge."

    The Knowledge_Service implements Requirement 7.6 in
    :meth:`KnowledgeService._persist_decision_denial`. The helper opens
    a SEPARATE :meth:`Engine.begin` transaction for each attempt — the
    caller's originating transaction is going to be rolled back by the
    :class:`DecisionAuthorizationError` propagating from
    :meth:`KnowledgeService.create_decision`, so the denial row must
    live outside that transaction to survive. The helper retries up to
    three times after the initial attempt fails (four total attempts,
    sleeping ``0.01``, ``0.02``, and ``0.04`` seconds in between). If
    every attempt fails, the helper raises this exception in place of
    :class:`DecisionAuthorizationError` — denial-and-audit have
    silently diverged and the operator must be told.

    The exception preserves the same ``reason_code`` and
    ``correlation_id`` the authorization evaluation produced so an
    operator-facing surface can still render a Requirement 7.4
    indistinguishable response if it so chooses; ``attempts`` records
    how many times the audit append was attempted (always 4 in the
    current implementation, but exposed so the value cannot drift
    silently if the backoff sequence is widened in a later slice).

    The underlying SQLAlchemy / audit exception is preserved as
    ``__cause__`` for diagnostics.

    Attributes:
        reason_code: The denial reason code from the evaluation that
            triggered this denial path. Matching
            :class:`DecisionAuthorizationError`.
        correlation_id: The correlation identifier shared with the
            evaluation audit row (now rolled back) and the (failed)
            denial record attempts.
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
            f"Denial Record append for a denied Decision failed after "
            f"{attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupportRef:
    """Reference to a Content Region Occurrence cited by a ``Supports`` link.

    The pair ``(region_id, document_revision_id)`` is the composite
    primary key of ``Region_Occurrences``. Frozen so a list of
    SupportRefs cannot be mutated between the verify-existence loop and
    the write loop (AD-WS-5 — every artifact of one transaction shares
    one snapshot of the inputs).

    Attributes:
        region_id: Content Region Identity. Must reference an existing
            ``Content_Regions`` row; the existence check is performed
            via the composite PK on ``Region_Occurrences`` so a Region
            without an Occurrence in the named Revision is still
            rejected.
        document_revision_id: Document Revision Identity that owns the
            Region Occurrence (the second half of the composite PK).
    """

    region_id: str
    document_revision_id: str


@dataclass(frozen=True)
class CreateFindingResult:
    """Result of :meth:`KnowledgeService.create_finding`.

    Returned so callers (the HTTP layer in task 6.2, tests, downstream
    services that record a Trail Step on this Finding Revision) can
    correlate the created Finding with the audit row and the
    ``Supports`` Relationship rows in one round-trip.

    Attributes:
        finding_id: The Finding Resource Identity.
        finding_revision_id: The Finding Revision Identity.
        statement: The statement text persisted on the Finding Revision.
        is_hypothesis: Whether the Finding was created as a hypothesis.
        supporting_relationship_ids: The Relationship Identities of the
            ``Supports`` rows inserted alongside this Finding, in the
            order of the input ``supporting_region_occurrences``
            iterable. Empty when the Finding was created as a
            hypothesis with zero supports.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the Findings row, the Finding_Revisions row,
            every Relationships row, and the consequential audit row.
    """

    finding_id: str
    finding_revision_id: str
    statement: str
    is_hypothesis: bool
    supporting_relationship_ids: tuple[str, ...]
    recorded_at: str


@dataclass(frozen=True)
class CreateRelationshipResult:
    """Result of :meth:`KnowledgeService.record_contradiction`.

    Carries every field Requirement 4.2 demands on a Relationship plus
    the relationship-type discriminator so callers can verify the row
    was inserted with the expected ``relationship_type`` without a
    second round-trip. ``target_revision_id`` is ``None`` for
    ``Contradicts`` rows because Requirement 4.4 keys the relationship
    on the Finding Resource (not a specific Revision).
    """

    relationship_id: str
    relationship_type: str
    source_kind: str
    source_id: str
    source_revision_id: Optional[str]
    target_kind: str
    target_id: str
    target_revision_id: Optional[str]
    authoring_party_id: str
    recorded_at: str


@dataclass(frozen=True)
class CreateRecommendationResult:
    """Result of :meth:`KnowledgeService.create_recommendation`.

    Returned so callers (the HTTP layer in task 7.2, tests, downstream
    services that record a Trail Step on this Recommendation Revision)
    can correlate the created Recommendation with the audit row and the
    ``Derived From`` Relationship rows in one round-trip.

    Attributes:
        recommendation_id: The Recommendation Resource Identity.
        recommendation_revision_id: The Recommendation Revision
            Identity. Distinct from ``recommendation_id`` per AD-WS-3.
        rationale: The rationale text persisted on the Recommendation
            Revision (``None`` when the caller did not supply one).
        assumptions: Tuple of assumption strings persisted on the
            Recommendation Revision, in the order supplied by the
            caller.
        confidence: The confidence designation persisted on the
            Recommendation Revision (``None`` when the caller did not
            supply one). Always one of ``"Low"``, ``"Medium"``, or
            ``"High"`` when present.
        derived_from_relationship_ids: The Relationship Identities of
            the ``Derived From`` rows inserted alongside this
            Recommendation, in the order of the input
            ``derived_from_findings`` iterable. Always has length in
            ``[1, 50]`` because Requirement 5.1 rejects out-of-range
            counts before any write happens.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the Recommendations row, the Recommendation_Revisions
            row, every Relationships row, and the consequential audit
            row.
    """

    recommendation_id: str
    recommendation_revision_id: str
    rationale: Optional[str]
    assumptions: tuple[str, ...]
    confidence: Optional[str]
    derived_from_relationship_ids: tuple[str, ...]
    recorded_at: str


@dataclass(frozen=True)
class DecisionOmissionEntry:
    """Material-source omission recorded alongside a new Decision.

    Per Requirement 10.2 and Requirement 10.3, when a Decision Maker
    finalizes a Decision they may declare that one or more material
    sources are intentionally excluded, unavailable, restricted, stale,
    or unresolved. Each entry becomes one ``Omission_Entries`` row
    inside the same transaction as the Decision (AD-WS-5), linked to
    the Decision's Provenance Manifest by ``manifest_id``.

    Frozen so the verify-then-write loop in
    :meth:`KnowledgeService.create_decision` cannot observe a different
    list than it just validated (design §"Cross-Cutting Concerns" —
    *Transactionality*).

    Attributes:
        excluded_source_id: Resource Identity of the omitted source.
        excluded_source_revision_id: Revision Identity of the omitted
            source when known; ``None`` when only the Resource Identity
            is known (Requirement 10.2: "the excluded source Revision
            Identity *when known*").
        category: One of ``{intentional, unavailable, restricted,
            stale, unresolved}`` per Requirement 10.3 and the schema
            CHECK on ``Omission_Entries.category``.
        rationale: Exclusion rationale of 1..2,000 characters
            (Requirement 10.2 / schema column).
    """

    excluded_source_id: str
    excluded_source_revision_id: Optional[str]
    category: Literal[
        "intentional", "unavailable", "restricted", "stale", "unresolved"
    ]
    rationale: str


@dataclass(frozen=True)
class CreateDecisionResult:
    """Result of :meth:`KnowledgeService.create_decision`.

    Returned so callers (the HTTP layer in task 8.3, tests, downstream
    services that record a Trail Step targeting this Decision) can
    correlate the created Decision with its Addresses Relationship,
    Provenance Manifest, Omission Entries, and audit row in one
    round-trip without a second query.

    Attributes:
        decision_id: The Decision Immutable Record Identity.
        target_recommendation_id: The target Recommendation Resource
            Identity persisted on the Decisions row.
        target_recommendation_revision_id: The target Recommendation
            Revision Identity persisted on the Decisions row.
        outcome: The persisted outcome from
            ``{Accept, Reject, Defer}``.
        rationale: The persisted rationale text (1..4,000 chars).
        deciding_party_id: The deciding Party Identity persisted on
            the Decisions row.
        authority_basis_type: One of ``{role-grant-id, scope-id,
            delegation-chain-id}`` per AD-WS-10.
        authority_basis_id: Identifier of the specific role-grant,
            scope, or delegation chain that grants the authority,
            persisted on the Decisions row.
        applicable_scope: The scope identifier the Decision applies
            within.
        addresses_relationship_id: Relationship Identity of the single
            ``Addresses`` Relationship row written alongside the
            Decision (Requirement 6.3).
        manifest_id: Identity of the Provenance Manifest written
            alongside the Decision (Requirement 10.1).
        omission_entry_ids: Identities of the Omission Entries written
            alongside the Manifest, in the order supplied by the
            caller. Empty tuple when no omissions were declared.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the Decisions row, the Addresses Relationship
            row, the Provenance Manifest row, every Omission Entry
            row, and the consequential audit row.
    """

    decision_id: str
    target_recommendation_id: str
    target_recommendation_revision_id: str
    outcome: str
    rationale: str
    deciding_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    addresses_relationship_id: str
    manifest_id: str
    omission_entry_ids: tuple[str, ...]
    recorded_at: str


@dataclass(frozen=True)
class DecisionRecord:
    """Snapshot of one ``Decisions`` row returned by :meth:`KnowledgeService.get_decision`.

    Slice 2 (AD-WS-21) resolves a Decision Immutable Record through the
    Knowledge_Service public API rather than reading the ``Decisions``
    table directly. The :meth:`KnowledgeService.get_decision` read
    returns this immutable snapshot so that callers (notably
    :meth:`walking_slice.planning.objectives.ObjectiveService.create_objective`,
    which validates Requirement 2.2 — "the named target Decision
    Immutable Record Identity resolves to an existing Decision
    Immutable Record in the Knowledge_Service whose outcome at creation
    time is `Accept`") receive every column they need in one
    round-trip without coupling to the ``Decisions`` schema.

    The dataclass is frozen so a returned record cannot be mutated by a
    caller between resolution and the consequential write; every column
    is rendered exactly as persisted (the ``authority_basis_id`` is
    returned as the canonical UUID string, not a :class:`uuid.UUID`, so
    the round-trip is byte-equivalent with the on-disk value).

    Attributes:
        decision_id: The Decision Immutable Record Identity.
        target_recommendation_id: Identity of the target Recommendation
            Resource the Decision addresses.
        target_recommendation_revision_id: Identity of the target
            Recommendation Revision.
        outcome: One of ``{'Accept', 'Reject', 'Defer'}`` per AD-WS-11.
        rationale: The persisted rationale text (1..4000 chars).
        deciding_party_id: Identity of the deciding Party.
        authority_basis_type: One of ``{'role-grant-id', 'scope-id',
            'delegation-chain-id'}`` per AD-WS-10.
        authority_basis_id: Canonical UUID string identifying the
            specific role grant, scope, or delegation chain invoked.
        applicable_scope: Scope identifier the Decision applies within.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp.
    """

    decision_id: str
    target_recommendation_id: str
    target_recommendation_revision_id: str
    outcome: str
    rationale: str
    deciding_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join an originating-write audit row to any
    downstream audit row produced for the same logical operation. They
    are not registered with :class:`IdentityService` because they do
    not name a domain Resource — they exist purely to correlate audit
    rows.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to a Finding's
    identifiers in ``Identifier_Registry``. Hashing the statement text
    keeps the digest a stable function of the Finding's natural
    content (the statement) — sharing the same digest across the
    Finding Resource Identity and the first Finding Revision Identity
    mirrors how :mod:`walking_slice.evidence` digests a Document and
    its first Revision.
    """
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeService:
    """Persist Findings and the typed Relationships that connect them.

    Like :class:`~walking_slice.evidence.EvidenceRepository`, this
    service is connection-scoped at call time: every public method
    accepts the caller's :class:`~sqlalchemy.engine.Connection` and
    writes inside the caller's transaction (AD-WS-5). Instances
    therefore hold only the cross-request collaborators
    (:class:`Clock`, :class:`IdentityService`, :class:`AuditLog`) and
    can be shared across requests.

    Args:
        clock: Source of the recorded timestamp shared by the
            Findings/Finding_Revisions/Relationships rows and the
            consequential audit row. The clock is consulted exactly
            once per write so every artifact of the transaction shares
            one timestamp (design §"Cross-Cutting Concerns" —
            *Transactionality*).
        identity_service: Generates Finding, Finding Revision, and
            Relationship identifiers and persists Finding /
            Finding-Revision bindings to ``Identifier_Registry``.
            Relationship identifiers are minted but not registered —
            mirroring the pattern in :mod:`walking_slice.audit` where
            an internally-generated UUIDv7 is sufficient for
            non-domain-content rows.
        audit_log: Appends the ``'consequential'`` audit row inside the
            caller's transaction. Failures propagate as
            :class:`walking_slice.audit.AuditAppendError`; the caller's
            transaction context manager rolls back automatically.
        authorization_service: Optional
            :class:`~walking_slice.authorization.AuthorizationService`
            used by :meth:`create_recommendation` (Requirement 5.7)
            and :meth:`create_decision` (Requirement 7.1) to enforce
            authority on consequential writes. The dependency is
            optional so existing ``create_finding`` and
            ``record_contradiction`` callers — and their unit tests —
            that pre-date the authorization wiring continue to work
            unchanged. When ``None``, :meth:`create_recommendation`
            and :meth:`create_decision` skip the authority check;
            production composition (task 15.2) wires a real
            :class:`AuthorizationService` so the check is always
            performed. **When wired,** :meth:`create_decision`
            additionally requires the caller to pass an ``engine``
            argument so the Denial Record for a denied attempt can be
            written in a separate transaction that survives the
            caller's rollback (Requirement 7.6).
        denial_audit_sleep: Sleep function used by
            :meth:`_persist_decision_denial` to pause between retries
            of the Denial Record append. Defaults to :func:`time.sleep`
            so production code paths use real wall-clock backoff;
            tests that need deterministic timing inject a recording
            stub so the retry sequence is observable without spending
            real time. The function is called with a single ``float``
            argument naming the seconds to sleep, drawn from
            :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS`.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: Optional[AuthorizationService] = None
    manifest_writer: Optional[ProvenanceManifestWriter] = None
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_finding(
        self,
        connection: Connection,
        *,
        statement: str,
        authoring_party_id: str,
        is_hypothesis: bool = False,
        supporting_region_occurrences: Sequence[SupportRef] = (),
        assumptions: Sequence[str] = (),
        confidence_note: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> CreateFindingResult:
        """Create a Finding (Resource + first Revision) plus its supports.

        Per Requirements 4.1, 4.2, 4.3, 4.5 and design §"Knowledge_Service":

        1. Reject an empty ``statement`` (Requirement 4.1's "submits a
           Finding for finalization" implies a statement).
        2. Reject when ``not is_hypothesis`` and
           ``supporting_region_occurrences`` is empty (Requirement 4.3).
        3. Verify every cited Region Occurrence exists via the composite
           PK on ``Region_Occurrences``. Any non-resolvable citation
           raises :class:`FindingNotResolvableError` before any domain
           row is written.
        4. Generate a Finding Resource Identity and a Finding Revision
           Identity (AD-WS-3 — separate columns and separate factory
           methods).
        5. Register both identifiers in ``Identifier_Registry`` inside
           the caller's transaction (AD-WS-2, AD-WS-5).
        6. Insert one ``Findings`` row, one ``Finding_Revisions`` row,
           and one ``Relationships`` row per cited Region Occurrence —
           each Relationship is one ``Supports`` row (Requirement 4.5).
        7. Append a consequential ``Audit_Records`` row with
           ``action_type='create.finding'`` inside the same transaction
           (Requirement 13.1, AD-WS-5).

        The rows are inserted in the order
        ``Identifier_Registry → Findings → Finding_Revisions →
        Relationships → Audit_Records`` so the FK from
        ``Finding_Revisions.finding_id`` to ``Findings.finding_id`` is
        satisfied when each row hits the database and the audit row's
        FK on ``actor_party_id → Parties`` is checked last (a FK
        failure here rolls back the whole transaction per Requirement
        2.7 / 13.6, which is the desired behaviour).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            statement: Non-empty text of the Finding (Requirement 4.1).
            authoring_party_id: Identity of the recording Party.
                Persisted on ``Finding_Revisions.authoring_party_id``,
                every ``Relationships.authoring_party_id``, and the
                consequential audit row's ``actor_party_id``. Must
                reference an existing ``Parties`` row; the FK is
                enforced by the database.
            is_hypothesis: When ``True`` the Finding may have zero
                supports (Requirement 4.1's "hypothesis designation
                explicitly set to true").
            supporting_region_occurrences: Iterable of
                :class:`SupportRef`. May be empty when
                ``is_hypothesis`` is ``True``. Iteration order is
                preserved in the returned
                ``supporting_relationship_ids`` so callers can
                correlate inputs to outputs without re-querying.
            assumptions: Iterable of assumption strings stored as a
                JSON array on ``Finding_Revisions.assumptions_json``.
                Empty by default.
            confidence_note: Optional free-form confidence note stored
                on ``Finding_Revisions.confidence_note``. ``None`` is
                persisted as SQL NULL.
            correlation_id: Optional correlation identifier shared by
                every audit row written in this transaction. A UUIDv7
                is generated when omitted.

        Returns:
            :class:`CreateFindingResult` carrying the issued Finding
            identifiers, the persisted statement, the hypothesis flag,
            the ordered Relationship Identities, and the recorded time.

        Raises:
            FindingValidationError: Statement empty, authoring Party
                missing, or non-hypothesis with zero supports.
            FindingNotResolvableError: Any cited Region Occurrence is
                not present in ``Region_Occurrences``.
            walking_slice.audit.AuditAppendError: Audit append failed
                (typically because ``authoring_party_id`` does not name
                an existing ``Parties`` row). The surrounding
                transaction MUST be allowed to roll back.
            walking_slice.identity.IdentityConflictError: A freshly
                generated Finding identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
        """
        # Input validation (Requirements 4.1 and 4.3). All Python-side
        # validation runs first so a request that violates any
        # acceptance criterion is rejected before we touch the database
        # or the identity service.
        self._validate_statement(statement)
        self._validate_authoring_party(authoring_party_id)
        supports = tuple(supporting_region_occurrences)
        if not is_hypothesis and len(supports) == 0:
            raise FindingValidationError(
                "Non-hypothesis Findings must cite at least one Content "
                "Region Occurrence via a Supports Relationship "
                "(Requirement 4.3).",
                failed_constraint="supports_required_for_non_hypothesis",
            )

        # Verify each Region Occurrence exists *before* any write
        # (Requirement 4.1's "Content Region Occurrence identified by
        # Content Region Identity"). Doing this check first means a
        # FindingNotResolvableError never leaves a partial Findings or
        # Finding_Revisions row behind — the caller's transaction is
        # untouched when the function raises.
        for support in supports:
            self._require_region_occurrence_exists(connection, support)

        # Single clock reading shared by every row in the transaction
        # (design §"Cross-Cutting Concerns" — *Transactionality*).
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()

        finding_id = str(self.identity_service.new_resource_id())
        finding_revision_id = str(self.identity_service.new_revision_id())

        # Bind both identifiers to the statement digest. Findings and
        # their first Revision share one digest, mirroring the
        # Document / first-Revision pattern in
        # :mod:`walking_slice.evidence` so the Identifier_Registry's
        # non-reuse invariant (AD-WS-2) is exercised at the same
        # granularity for both resource types.
        statement_digest = _sha256_hex(statement.encode("utf-8"))
        self.identity_service.reject_if_duplicate(
            finding_id,
            statement_digest,
            connection=connection,
            kind="resource",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_FINDING,
            recorded_time=recorded_time,
        )
        self.identity_service.reject_if_duplicate(
            finding_revision_id,
            statement_digest,
            connection=connection,
            kind="revision",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_FINDING,
            recorded_time=recorded_time,
        )

        # Insert the Findings header. The schema only carries
        # ``finding_id`` and ``created_at`` — interpretive content lives
        # on the Revision row.
        connection.execute(
            text(
                """
                INSERT INTO Findings (finding_id, created_at)
                VALUES (:finding_id, :created_at)
                """
            ),
            {"finding_id": finding_id, "created_at": recorded_at},
        )

        # Insert the immutable Finding_Revisions row. ``assumptions``
        # is serialized as a JSON array per the schema column type
        # (``assumptions_json TEXT NOT NULL``). The empty case is
        # serialized as ``"[]"`` so the NOT NULL constraint holds.
        connection.execute(
            text(
                """
                INSERT INTO Finding_Revisions (
                    finding_revision_id, finding_id, parent_revision_id,
                    statement, is_hypothesis, authoring_party_id,
                    assumptions_json, confidence_note, recorded_at
                ) VALUES (
                    :finding_revision_id, :finding_id, NULL,
                    :statement, :is_hypothesis, :authoring_party_id,
                    :assumptions_json, :confidence_note, :recorded_at
                )
                """
            ),
            {
                "finding_revision_id": finding_revision_id,
                "finding_id": finding_id,
                "statement": statement,
                "is_hypothesis": 1 if is_hypothesis else 0,
                "authoring_party_id": authoring_party_id,
                "assumptions_json": json.dumps(list(assumptions)),
                "confidence_note": confidence_note,
                "recorded_at": recorded_at,
            },
        )

        # One ``Supports`` Relationship per cited Region Occurrence
        # (Requirement 4.5). Iteration order from the input iterable is
        # preserved so callers can correlate inputs to outputs by
        # position in the returned tuple.
        supporting_relationship_ids: list[str] = []
        for support in supports:
            relationship_id = str(self.identity_service.new_relationship_id())
            self._insert_relationship(
                connection,
                relationship_id=relationship_id,
                relationship_type=_RELATIONSHIP_TYPE_SUPPORTS,
                source_kind=_KIND_FINDING_REVISION,
                source_id=finding_id,
                source_revision_id=finding_revision_id,
                target_kind=_KIND_REGION_OCCURRENCE,
                target_id=support.region_id,
                target_revision_id=support.document_revision_id,
                authoring_party_id=authoring_party_id,
                recorded_at=recorded_at,
            )
            supporting_relationship_ids.append(relationship_id)

        # Provenance Manifest (Requirement 10.1). When the slice's
        # :class:`ProvenanceManifestWriter` is wired the Finding's
        # supporting Region Occurrences are recorded as the manifest's
        # Included Sources; an unwired writer leaves the manifest path
        # unchanged (back-compat for tests that pre-date this wiring).
        # The manifest INSERT participates in the caller's transaction
        # so a manifest-write failure rolls the Findings,
        # Finding_Revisions, Relationships, and Identifier_Registry
        # rows back together (Requirements 2.7, 10.6, 13.6 / AD-WS-5).
        if self.manifest_writer is not None:
            included_sources = tuple(
                IncludedSource(
                    kind=_KIND_REGION_OCCURRENCE,
                    resource_id=support.region_id,
                    revision_id=support.document_revision_id,
                    recorded_at=recorded_time,
                )
                for support in supports
            )
            self.manifest_writer.write_manifest(
                connection,
                subject_kind="finding_revision",
                subject_id=finding_id,
                subject_revision_id=finding_revision_id,
                authoring_party_id=authoring_party_id,
                included_sources=included_sources,
                recorded_at=recorded_time,
            )

        # Audit append participates in the caller's transaction so a
        # failure here rolls back the Findings, Finding_Revisions, and
        # Relationships rows together (AD-WS-5, Requirement 13.6).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_AUDIT_ACTION_CREATE_FINDING,
            target_id=finding_id,
            target_revision_id=finding_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateFindingResult(
            finding_id=finding_id,
            finding_revision_id=finding_revision_id,
            statement=statement,
            is_hypothesis=is_hypothesis,
            supporting_relationship_ids=tuple(supporting_relationship_ids),
            recorded_at=recorded_at,
        )

    def record_contradiction(
        self,
        connection: Connection,
        *,
        source_finding_revision_id: str,
        target_finding_id: str,
        authoring_party_id: str,
        correlation_id: Optional[str] = None,
    ) -> CreateRelationshipResult:
        """Record a ``Contradicts`` Relationship between two Findings.

        Per Requirement 4.4 ("When an Analyst asserts that a newly
        created Finding contradicts an existing Finding, the
        Knowledge_Service SHALL preserve both Finding records unchanged
        and SHALL record a ``Contradicts`` Relationship between them
        capturing source Finding Identity, target Finding Identity,
        authoring Party, and recorded time"):

        1. Verify ``source_finding_revision_id`` resolves to a
           ``Finding_Revisions`` row; raise
           :class:`FindingNotFoundError` (``role='source'``) otherwise.
        2. Verify ``target_finding_id`` resolves to a ``Findings``
           row; raise :class:`FindingNotFoundError` (``role='target'``)
           otherwise.
        3. Insert one ``Relationships`` row with
           ``relationship_type='Contradicts'`` (AD-WS-7 — every
           Relationship is an immutable assertion).
        4. Append a consequential ``Audit_Records`` row with
           ``action_type='record.contradiction'``.

        The source-Finding Identity is *derived* from the source
        Revision row's ``finding_id`` column — callers do not pass it.
        This keeps the Relationship's source consistent with the named
        Revision and prevents a caller from claiming a Revision belongs
        to a different Finding than it actually does.

        Both source and target Finding records are left byte-equivalent
        to their prior state because:

        - ``Finding_Revisions`` is append-only (AD-WS-4 trigger).
        - ``Findings`` is touched only via INSERT in this module —
          this method does not write to it at all.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            source_finding_revision_id: Identity of the source Finding
                Revision asserting the contradiction. Must resolve to
                an existing ``Finding_Revisions`` row.
            target_finding_id: Identity of the target Finding Resource
                being contradicted. Must resolve to an existing
                ``Findings`` row.
            authoring_party_id: Identity of the recording Party.
            correlation_id: Optional correlation identifier. A UUIDv7
                is generated when omitted.

        Returns:
            :class:`CreateRelationshipResult` carrying the Relationship
            identifiers and every Requirement-4.2 attribute of the
            inserted row.

        Raises:
            FindingNotFoundError: Either side does not resolve.
            FindingValidationError: ``authoring_party_id`` is empty.
            walking_slice.audit.AuditAppendError: Audit append failed.
        """
        self._validate_authoring_party(authoring_party_id)

        # Resolve the source Revision -> source Finding Identity. We
        # *derive* source_finding_id from the Revision row rather than
        # accepting it from the caller so the Relationship's source
        # pair (source_id, source_revision_id) is internally consistent
        # by construction.
        source_finding_id = connection.execute(
            text(
                "SELECT finding_id FROM Finding_Revisions "
                "WHERE finding_revision_id = :finding_revision_id"
            ),
            {"finding_revision_id": source_finding_revision_id},
        ).scalar_one_or_none()
        if source_finding_id is None:
            raise FindingNotFoundError(
                role="source", identifier=source_finding_revision_id
            )

        target_exists = connection.execute(
            text(
                "SELECT 1 FROM Findings WHERE finding_id = :finding_id"
            ),
            {"finding_id": target_finding_id},
        ).scalar_one_or_none()
        if target_exists is None:
            raise FindingNotFoundError(
                role="target", identifier=target_finding_id
            )

        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()

        relationship_id = str(self.identity_service.new_relationship_id())
        self._insert_relationship(
            connection,
            relationship_id=relationship_id,
            relationship_type=_RELATIONSHIP_TYPE_CONTRADICTS,
            source_kind=_KIND_FINDING_REVISION,
            source_id=source_finding_id,
            source_revision_id=source_finding_revision_id,
            target_kind=_KIND_FINDING,
            target_id=target_finding_id,
            # Requirement 4.4 keys the relationship on Finding Identity,
            # not a specific Revision, so target_revision_id is NULL.
            target_revision_id=None,
            authoring_party_id=authoring_party_id,
            recorded_at=recorded_at,
        )

        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_AUDIT_ACTION_RECORD_CONTRADICTION,
            target_id=target_finding_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateRelationshipResult(
            relationship_id=relationship_id,
            relationship_type=_RELATIONSHIP_TYPE_CONTRADICTS,
            source_kind=_KIND_FINDING_REVISION,
            source_id=source_finding_id,
            source_revision_id=source_finding_revision_id,
            target_kind=_KIND_FINDING,
            target_id=target_finding_id,
            target_revision_id=None,
            authoring_party_id=authoring_party_id,
            recorded_at=recorded_at,
        )

    # -- internal helpers --------------------------------------------------

    def create_recommendation(
        self,
        connection: Connection,
        *,
        authoring_party_id: str,
        derived_from_findings: Sequence[str],
        rationale: Optional[str] = None,
        assumptions: Sequence[str] = (),
        confidence: Optional[Literal["Low", "Medium", "High"]] = None,
        applicable_scope: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> CreateRecommendationResult:
        """Create a Recommendation (Resource + first Revision) plus its derivations.

        Per Requirements 5.1 through 5.7 and design §"Knowledge_Service":

        1. Reject a missing or empty ``authoring_party_id`` (Requirement
           5.7 — unauthenticated callers are rejected).
        2. Validate the inputs against the Requirement-5 ranges:
           ``derived_from_findings`` must contain 1..50 entries
           (Requirement 5.1); when supplied, ``rationale`` must be
           1..10,000 characters (Requirement 5.3); ``assumptions``
           must contain 0..50 entries of 1..2,000 characters each
           (Requirement 5.4); ``confidence`` must be one of
           ``{Low, Medium, High}`` when supplied (Requirement 5.5).
        3. When :attr:`authorization_service` is wired, evaluate
           authority for ``create.recommendation`` at ``evaluation_at``
           (Requirement 5.7). The action's ``create.*`` prefix maps to
           the ``modify`` authority type per the ActionType enumeration
           in design §"Authorization_Service" (Requirement 12.3, 12.4 —
           no substitution between view/modify/approve); an effective
           Analyst role for ``applicable_scope`` grants ``modify`` and
           therefore permits the action. On ``deny``, raise
           :class:`RecommendationAuthorizationError` with the denial's
           ``reason_code`` and ``correlation_id`` — the authorization
           service has already appended the evaluation audit row, and
           the surrounding transaction rolls back without persisting
           any Recommendations, Recommendation_Revisions, or
           Relationships row.
        4. Verify every ``derived_from`` Finding Identity resolves to
           an existing ``Findings`` row (Requirement 5.6). Any
           unresolved Finding raises
           :class:`RecommendationNotResolvableError` *before* any
           domain row is written so the database cannot end up with a
           dangling reference.
        5. Generate a Recommendation Resource Identity and a
           Recommendation Revision Identity (AD-WS-3 — separate
           columns and separate factory methods). Register both in
           ``Identifier_Registry`` inside the caller's transaction
           (AD-WS-2, AD-WS-5).
        6. Insert one ``Recommendations`` row, one
           ``Recommendation_Revisions`` row (carrying the rationale,
           JSON-encoded assumptions array, confidence, authoring
           Party, and recorded time), and one ``Relationships`` row
           per supplied Finding Identity with
           ``relationship_type='Derived From'``,
           ``source_kind='recommendation_revision'``,
           ``target_kind='finding'``, and ``target_revision_id=NULL``
           (Requirement 5.6 keys the relationship on the Finding
           Resource — the Finding Revision identity is *not* part of
           the contract).
        7. Append a consequential ``Audit_Records`` row with
           ``action_type='create.recommendation'`` inside the same
           transaction (Requirement 13.1, AD-WS-5).

        Rows are inserted in dependency order:
        ``Identifier_Registry → Recommendations → Recommendation_Revisions
        → Relationships → Audit_Records``. A FK failure anywhere along
        the chain rolls back the entire transaction per Requirement
        2.7 / 13.6, which is the desired behaviour.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            authoring_party_id: Identity of the Party creating the
                Recommendation. Empty is rejected as Requirement-5.7
                unauthenticated. Must reference an existing
                ``Parties`` row; the FK is enforced by the database.
            derived_from_findings: Sequence of Finding Identities the
                new Recommendation derives from. Iteration order is
                preserved in the returned
                ``derived_from_relationship_ids`` so callers can
                correlate inputs to outputs by position. Duplicates
                are allowed — Requirement 5.1 names a count range and
                does not forbid duplicates — and produce one
                ``Derived From`` Relationship row per occurrence.
            rationale: Optional rationale text. When supplied, the
                length is constrained to 1..10,000 characters
                (Requirement 5.3); ``None`` is persisted as SQL NULL.
            assumptions: Optional sequence of assumption strings.
                When supplied, the count is constrained to 0..50 and
                each entry to 1..2,000 characters (Requirement 5.4).
                Persisted as a JSON array on
                ``Recommendation_Revisions.assumptions_json``;
                empty input is persisted as ``"[]"`` so the NOT NULL
                column constraint holds.
            confidence: Optional confidence designation from
                ``{"Low", "Medium", "High"}`` (Requirement 5.5).
                ``None`` is persisted as SQL NULL.
            applicable_scope: Optional scope identifier passed to
                :meth:`AuthorizationService.evaluate` as
                ``target.scope``. Used only when
                :attr:`authorization_service` is wired.
            evaluation_at: Optional time at which authority is
                evaluated. Defaults to ``self.clock.now()`` so the
                evaluation timestamp matches the recorded time written
                to every row in this transaction (design
                §"Cross-Cutting Concerns" — *Transactionality*). Used
                only when :attr:`authorization_service` is wired.
            correlation_id: Optional correlation identifier shared by
                every audit row written in this transaction. A UUIDv7
                is generated when omitted.

        Returns:
            :class:`CreateRecommendationResult` carrying the issued
            Recommendation identifiers, the persisted rationale,
            assumptions, and confidence, the ordered Relationship
            Identities, and the recorded time.

        Raises:
            RecommendationValidationError: ``authoring_party_id`` is
                missing or any of the Requirement-5.1/5.3/5.4/5.5
                range constraints is violated.
            RecommendationNotResolvableError: Any
                ``derived_from`` Finding Identity does not resolve to
                an existing ``Findings`` row (Requirement 5.6).
            RecommendationAuthorizationError: The wired
                :class:`AuthorizationService` denied the action
                (Requirement 5.7).
            walking_slice.audit.AuditAppendError: Audit append failed
                (typically because ``authoring_party_id`` does not
                name an existing ``Parties`` row). The surrounding
                transaction MUST be allowed to roll back per
                Requirement 13.6.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
        """
        # Python-side validation runs first — a request that violates any
        # Requirement-5 range is rejected before we touch the database,
        # the authorization service, or the identity service. This keeps
        # the rejection path cheap and ensures no audit row is appended
        # for malformed input (the authorization audit row is *only*
        # appended when the authorization evaluation is invoked, which
        # requires us to first know the request is at least structurally
        # valid).
        self._validate_authoring_party_for_recommendation(authoring_party_id)
        derived_from = tuple(derived_from_findings)
        self._validate_derived_from_count(derived_from)
        self._validate_rationale(rationale)
        assumptions_tuple = tuple(assumptions)
        self._validate_assumptions(assumptions_tuple)
        self._validate_confidence(confidence)

        # Single clock reading shared by the audit evaluation row, the
        # Recommendations row, the Recommendation_Revisions row, every
        # Relationships row, and the consequential audit row (design
        # §"Cross-Cutting Concerns" — *Transactionality*). The
        # evaluation timestamp defaults to the same instant so the
        # authorization audit row aligns with the consequential write.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()

        # Authority check (Requirement 5.7). The check runs before we
        # verify Finding existence so an unauthorized caller cannot
        # probe which Findings exist by inspecting the error response.
        if self.authorization_service is not None:
            decision = self.authorization_service.evaluate(
                connection,
                party_id=authoring_party_id,
                action=_AUTHORIZATION_ACTION_CREATE_RECOMMENDATION,
                target=TargetRef(
                    kind=_AUTHORIZATION_TARGET_KIND_RECOMMENDATION,
                    scope=applicable_scope,
                ),
                at=evaluation_at if evaluation_at is not None else recorded_time,
                correlation_id=correlation,
            )
            if decision.is_deny:
                raise RecommendationAuthorizationError(
                    reason_code=decision.reason_code or "no-role-assignment",
                    correlation_id=decision.correlation_id,
                )

        # Verify every Finding Identity exists *before* any write
        # (Requirement 5.6). Doing this after authority but before any
        # write means an authorized caller learns *which* Finding does
        # not resolve, while an unauthorized caller has already been
        # rejected without that information.
        for finding_id in derived_from:
            self._require_finding_exists(connection, finding_id)

        recommendation_id = str(self.identity_service.new_resource_id())
        recommendation_revision_id = str(self.identity_service.new_revision_id())

        # Bind both identifiers to a digest derived from the
        # Recommendation Revision's natural content (rationale +
        # confidence + assumptions list). Recommendations and their
        # first Revision share one digest, mirroring the Document /
        # first-Revision pattern in :mod:`walking_slice.evidence` so
        # the Identifier_Registry non-reuse invariant (AD-WS-2) is
        # exercised at the same granularity for both Resource kinds.
        revision_digest = _sha256_hex(
            json.dumps(
                {
                    "rationale": rationale,
                    "assumptions": list(assumptions_tuple),
                    "confidence": confidence,
                    "derived_from": list(derived_from),
                },
                sort_keys=True,
            ).encode("utf-8")
        )
        self.identity_service.reject_if_duplicate(
            recommendation_id,
            revision_digest,
            connection=connection,
            kind="resource",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_RECOMMENDATION,
            recorded_time=recorded_time,
        )
        self.identity_service.reject_if_duplicate(
            recommendation_revision_id,
            revision_digest,
            connection=connection,
            kind="revision",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_RECOMMENDATION,
            recorded_time=recorded_time,
        )

        # Insert the Recommendations header. The schema only carries
        # ``recommendation_id`` and ``created_at`` — interpretive content
        # lives on the Revision row.
        connection.execute(
            text(
                """
                INSERT INTO Recommendations (recommendation_id, created_at)
                VALUES (:recommendation_id, :created_at)
                """
            ),
            {
                "recommendation_id": recommendation_id,
                "created_at": recorded_at,
            },
        )

        # Insert the immutable Recommendation_Revisions row.
        # ``assumptions`` is serialized as a JSON array per the schema
        # column type (``assumptions_json TEXT NOT NULL``). The empty
        # case is serialized as ``"[]"`` so the NOT NULL constraint
        # holds. ``confidence`` is persisted as SQL NULL when omitted —
        # the schema CHECK constraint permits NULL.
        connection.execute(
            text(
                """
                INSERT INTO Recommendation_Revisions (
                    recommendation_revision_id, recommendation_id,
                    parent_revision_id, rationale, assumptions_json,
                    confidence, authoring_party_id, recorded_at
                ) VALUES (
                    :recommendation_revision_id, :recommendation_id,
                    NULL, :rationale, :assumptions_json,
                    :confidence, :authoring_party_id, :recorded_at
                )
                """
            ),
            {
                "recommendation_revision_id": recommendation_revision_id,
                "recommendation_id": recommendation_id,
                "rationale": rationale,
                "assumptions_json": json.dumps(list(assumptions_tuple)),
                "confidence": confidence,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # One ``Derived From`` Relationship per cited Finding
        # Identity (Requirement 5.1). Iteration order from the input
        # iterable is preserved so callers can correlate inputs to
        # outputs by position in the returned tuple. Duplicates are
        # allowed and produce one Relationships row each — Requirement
        # 5.1 names a count range and does not forbid duplicates.
        derived_from_relationship_ids: list[str] = []
        for finding_id in derived_from:
            relationship_id = str(self.identity_service.new_relationship_id())
            self._insert_relationship(
                connection,
                relationship_id=relationship_id,
                relationship_type=_RELATIONSHIP_TYPE_DERIVED_FROM,
                source_kind=_KIND_RECOMMENDATION_REVISION,
                source_id=recommendation_id,
                source_revision_id=recommendation_revision_id,
                target_kind=_KIND_FINDING,
                target_id=finding_id,
                # Requirement 5.6 keys the relationship on Finding
                # Identity, not a specific Revision, so
                # target_revision_id is NULL — matching the pattern
                # used by ``Contradicts``.
                target_revision_id=None,
                authoring_party_id=authoring_party_id,
                recorded_at=recorded_at,
            )
            derived_from_relationship_ids.append(relationship_id)

        # Provenance Manifest (Requirement 10.1). When the slice's
        # :class:`ProvenanceManifestWriter` is wired the Recommendation's
        # Derived From Findings are recorded as the manifest's Included
        # Sources; an unwired writer leaves the manifest path unchanged
        # (back-compat for tests that pre-date this wiring). The
        # ``Derived From`` Relationship keys on Finding Resource not
        # Revision (Requirement 5.6) so the corresponding Included
        # Source carries ``revision_id=None``. The manifest INSERT
        # participates in the caller's transaction so a manifest-write
        # failure rolls the Recommendations, Recommendation_Revisions,
        # Relationships, and Identifier_Registry rows back together
        # (Requirements 2.7, 10.6, 13.6 / AD-WS-5).
        if self.manifest_writer is not None:
            included_sources = tuple(
                IncludedSource(
                    kind=_KIND_FINDING_REVISION,
                    resource_id=finding_id,
                    revision_id=None,
                    recorded_at=recorded_time,
                )
                for finding_id in derived_from
            )
            self.manifest_writer.write_manifest(
                connection,
                subject_kind="recommendation_revision",
                subject_id=recommendation_id,
                subject_revision_id=recommendation_revision_id,
                authoring_party_id=authoring_party_id,
                included_sources=included_sources,
                recorded_at=recorded_time,
            )

        # Audit append participates in the caller's transaction so a
        # failure here rolls back the Recommendations,
        # Recommendation_Revisions, and Relationships rows together
        # (AD-WS-5, Requirement 13.6).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_AUDIT_ACTION_CREATE_RECOMMENDATION,
            target_id=recommendation_id,
            target_revision_id=recommendation_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateRecommendationResult(
            recommendation_id=recommendation_id,
            recommendation_revision_id=recommendation_revision_id,
            rationale=rationale,
            assumptions=assumptions_tuple,
            confidence=confidence,
            derived_from_relationship_ids=tuple(derived_from_relationship_ids),
            recorded_at=recorded_at,
        )

    def create_decision(
        self,
        connection: Connection,
        *,
        target_recommendation_id: str,
        target_recommendation_revision_id: str,
        outcome: Literal["Accept", "Reject", "Defer"],
        rationale: str,
        deciding_party_id: str,
        authority_basis: AuthorityBasisRef,
        applicable_scope: str,
        omissions: Sequence[DecisionOmissionEntry] = (),
        engine: Optional[Engine] = None,
        evaluation_at: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> CreateDecisionResult:
        """Create a Decision Immutable Record plus its Addresses Relationship,
        Provenance Manifest, and any Omission Entries.

        Per Requirements 6.1 through 6.7, AD-WS-4 (Decisions are
        Immutable Records), AD-WS-5 (audit and manifest append in the
        same transaction as the originating write), AD-WS-10 (authority
        basis enumeration), and AD-WS-11 (slice-restricted outcomes
        ``{Accept, Reject, Defer}``):

        1. Reject any submission missing a required attribute
           (Requirement 6.7): ``target_recommendation_id``,
           ``target_recommendation_revision_id``, ``outcome``,
           ``rationale``, ``deciding_party_id``, ``authority_basis``,
           or ``applicable_scope``. The ``recorded_at`` attribute is
           sourced from :attr:`clock`.now() and so is always present.
        2. Reject ``outcome`` not in ``{Accept, Reject, Defer}``
           (AD-WS-11). Supersession is intentionally excluded from
           the slice — Pilot feedback may reintroduce it in a later
           slice; the design document and schema CHECK constraint
           must be updated together with this validator if so.
        3. Reject ``authority_basis.type`` not in
           ``{role-grant-id, scope-id, delegation-chain-id}``
           (AD-WS-10). The :class:`AuthorityBasisRef` Pydantic model
           already enforces this Literal, but the validator runs
           independently so callers that bypass the Pydantic layer
           (e.g. ad-hoc tests, future migrations) still receive the
           same rejection.
        4. Reject ``rationale`` outside 1..4,000 characters
           (Requirement 6.2). Empty or non-string rationale surfaces
           as ``rationale_missing``; over-long rationale as
           ``rationale_too_long``.
        5. Validate every supplied :class:`DecisionOmissionEntry`
           against the Omission_Entries schema before any write.
        6. Verify the target Recommendation Revision exists in
           ``Recommendation_Revisions`` and its ``recommendation_id``
           matches the supplied ``target_recommendation_id``. A
           non-resolvable pair raises
           :class:`RecommendationRevisionNotResolvableError` before
           any domain row is written.
        7. Check no Decision already exists for the pair
           ``(target_recommendation_id, target_recommendation_revision_id)``
           and raise :class:`DecisionConflictError` if one does
           (Requirement 6.5). The database also enforces this with a
           ``UNIQUE`` constraint — checking first gives a structured
           409-shaped error with the existing ``decision_id``.
        8. Generate a Decision Identity (an Immutable Record Identity
           per AD-WS-3 / design §"Identity_Service") and register it
           in ``Identifier_Registry`` with kind ``immutable_record``
           inside the caller's transaction (AD-WS-2, AD-WS-5).
        9. Insert one ``Decisions`` row.
        10. Insert one ``Relationships`` row with
            ``relationship_type='Addresses'``,
            ``source_kind='decision'`` /
            ``source_id=decision_id`` / ``source_revision_id=NULL``
            (a Decision has no revisions; it is itself the Immutable
            Record), and
            ``target_kind='recommendation_revision'`` /
            ``target_id=target_recommendation_id`` /
            ``target_revision_id=target_recommendation_revision_id``
            (Requirement 6.3).
        11. Insert one ``Provenance_Manifests`` row with
            ``subject_kind='decision'``,
            ``subject_id=decision_id``, ``subject_revision_id=NULL``,
            and an ``included_sources_json`` array listing the target
            Recommendation Revision as the single material source
            (Requirement 10.1). ``is_complete`` is set to ``0`` when
            any omission entry has a non-intentional category
            (design §"Persistence Invariants Summary" item 9 and
            Requirement 10.3); intentional-only or no-omissions cases
            keep ``is_complete = 1``.
        12. Insert one ``Omission_Entries`` row per supplied
            :class:`DecisionOmissionEntry`. Iteration order is
            preserved in the returned ``omission_entry_ids`` tuple so
            callers can correlate inputs to outputs by position.
        13. Append a consequential ``Audit_Records`` row with
            ``action_type='create.decision'``, ``target_id=decision_id``,
            and ``target_revision_id=NULL`` (a Decision has no
            Revision Identity — AD-WS-3, AD-WS-4) inside the same
            transaction (Requirement 6.4, 13.1, AD-WS-5).

        Rows are inserted in dependency order so a FK failure anywhere
        rolls back the whole transaction (Requirement 2.7 / 13.6):
        ``Identifier_Registry → Decisions → Relationships →
        Provenance_Manifests → Omission_Entries → Audit_Records``.

        When :attr:`authorization_service` is wired (task 8.2), the
        method evaluates Decision-Maker authority before any write:

        a. After the input-validation, target-existence, and
           existing-Decision pre-checks, the method opens a
           **separate** transaction on ``engine`` and calls
           ``AuthorizationService.evaluate(party, 'approve.decision',
           target=recommendation_revision, at=evaluation_at or now())``
           on that fresh connection (Requirement 7.1, design
           §"Decision authority evaluation flow"). The evaluation
           writes one row to ``Audit_Records`` in *its own*
           transaction — separated from the caller's transaction
           so the caller's connection does not become SQLite's
           exclusive writer (which would otherwise deadlock the
           Denial-Record write on the deny path). This is a
           deliberate, documented accommodation for SQLite's
           single-writer model; Requirement 12.5's "same
           transaction" wording is honored *correlationally* via the
           shared ``correlation_id`` rather than atomically.
        b. On ``permit`` the method continues to the persistence flow
           below and creates the Decision. The evaluation audit row
           has already committed; the consequential audit row
           (``action_type='create.decision'``) is written inside the
           caller's transaction alongside the Decision per AD-WS-5.
        c. On ``deny`` the method opens a SEPARATE
           :meth:`Engine.begin` transaction and appends a Denial
           Record via :meth:`AuditLog.append_denial` with
           ``attempted_action='approve.decision'``,
           ``target_id=target_recommendation_id``,
           ``target_revision_id=target_recommendation_revision_id``,
           and the ``reason_code`` / ``correlation_id`` returned by
           the authorization evaluation. The separate transaction is
           required because the caller's transaction is about to be
           rolled back by the :class:`DecisionAuthorizationError`
           this method is about to raise — the denial row must live
           outside that scope to survive (Requirement 7.6).
        d. The Denial Record append is retried up to three times
           with exponential backoff (0.01s, 0.02s, 0.04s) per
           Requirement 7.6. If every attempt fails the method raises
           :class:`DecisionAuditFailureError` *in place of*
           :class:`DecisionAuthorizationError` — denial and audit
           have silently diverged and the operator must be told. In
           either failure path the caller's transaction rolls back so
           the targeted Recommendation Resource and its Revisions
           remain byte-equivalent (Requirement 7.5); no Decision row,
           Addresses Relationship, Provenance Manifest, or Omission
           Entry is created.
        e. The exception raised on ``deny`` carries only
           ``reason_code`` and ``correlation_id`` so the HTTP layer
           (task 8.3) can render the AD-WS-9 indistinguishable
           denial response shape ``{generic_denial_indicator,
           reason_code, correlation_id}`` (Requirement 7.4).

        When :attr:`authorization_service` is ``None`` the
        authorization check is skipped entirely — the method is
        therefore safe to call from contexts that have already
        evaluated authority (or, in tests that pre-date task 8.2,
        have no authority check at all).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            target_recommendation_id: Identity of the target
                Recommendation Resource. Must reference an existing
                row whose ``recommendation_id`` matches the supplied
                value (verified via the composite check against
                ``Recommendation_Revisions``).
            target_recommendation_revision_id: Identity of the target
                Recommendation Revision. Must reference an existing
                ``Recommendation_Revisions`` row.
            outcome: One of ``{Accept, Reject, Defer}`` per AD-WS-11.
            rationale: Decision rationale of 1..4,000 characters
                (Requirement 6.2). The schema column is NOT NULL —
                the empty string is therefore rejected.
            deciding_party_id: Identity of the deciding Party.
                Persisted on ``Decisions.deciding_party_id`` and the
                consequential audit row's ``actor_party_id``. Must
                reference an existing ``Parties`` row; the FK is
                enforced by the database.
            authority_basis: :class:`AuthorityBasisRef` carrying the
                authority-basis ``type`` (AD-WS-10) and ``id`` of the
                specific role-grant / scope / delegation chain the
                Party invokes.
            applicable_scope: Scope identifier the Decision applies
                within. Persisted on ``Decisions.applicable_scope``,
                and passed as ``target.scope`` to
                :meth:`AuthorizationService.evaluate` so the wired
                role assignment must cover the same scope to permit
                the action.
            omissions: Optional iterable of
                :class:`DecisionOmissionEntry`. Each entry becomes
                one ``Omission_Entries`` row.
            engine: Required when :attr:`authorization_service` is
                wired. The Denial Record for a denied attempt is
                written in a fresh transaction on this engine so the
                row survives the caller's rollback (Requirement
                7.6). Ignored when :attr:`authorization_service` is
                ``None``. A clear ``ValueError`` is raised at the
                top of the method when the dependency is wired but
                ``engine`` is omitted, rather than letting the deny
                path discover the missing dependency mid-flight.
            evaluation_at: Optional explicit effective time passed
                to :meth:`AuthorizationService.evaluate` as the
                ``at`` parameter. Defaults to the same instant as
                the recorded time of this transaction so the
                evaluation row's recorded time aligns with the
                consequential write it authorized (design
                §"Cross-Cutting Concerns" — *Authorization*).
                Ignored when :attr:`authorization_service` is
                ``None``.
            correlation_id: Optional correlation identifier shared by
                every audit row written in this transaction. A
                UUIDv7 is generated when omitted.

        Returns:
            :class:`CreateDecisionResult` carrying the Decision
            identifiers, the persisted attributes, the
            ``Addresses`` Relationship Identity, the Provenance
            Manifest Identity, the ordered Omission Entry Identities,
            and the recorded time.

        Raises:
            DecisionValidationError: A required attribute is missing
                or any of the Requirement-6.2 / 6.7 / AD-WS-10 /
                AD-WS-11 ranges is violated.
            RecommendationRevisionNotResolvableError: The supplied
                ``(target_recommendation_id,
                target_recommendation_revision_id)`` pair does not
                resolve to a ``Recommendation_Revisions`` row whose
                ``recommendation_id`` matches the Resource Identity.
            DecisionConflictError: A Decision already addresses the
                supplied target Recommendation Revision (Requirement
                6.5).
            DecisionAuthorizationError: The wired
                :class:`AuthorizationService` denied the Decision
                attempt (Requirement 7.1). The Denial Record was
                appended successfully in a separate transaction
                (Requirement 7.6).
            DecisionAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt
                *and* the separate-transaction Denial Record append
                failed on every retry (Requirement 7.6). Replaces
                :class:`DecisionAuthorizationError` so the
                audit-failure indicator is unambiguous to the
                operator.
            ValueError: :attr:`authorization_service` is wired but
                ``engine`` was not supplied.
            walking_slice.audit.AuditAppendError: Consequential audit
                append failed (typically because ``deciding_party_id``
                does not name an existing ``Parties`` row). The
                surrounding transaction MUST be allowed to roll back
                per Requirement 13.6.
            walking_slice.identity.IdentityConflictError: A freshly
                generated Decision identifier collides with an
                existing ``Identifier_Registry`` binding (vanishingly
                rare for UUIDv7 within a single instance).
        """
        # Fail-fast configuration check (Requirement 7.6). When the
        # service is wired with an :class:`AuthorizationService` the
        # deny path needs an :class:`Engine` to open a *separate*
        # transaction for the Denial Record (so the row survives the
        # caller's rollback). Failing here — before any validation or
        # database read — surfaces the missing wiring with one
        # actionable error rather than letting the deny path
        # discover it mid-flight.
        if self.authorization_service is not None and engine is None:
            raise ValueError(
                "engine is required when authorization_service is "
                "wired on KnowledgeService.create_decision: the "
                "Denial Record for a denied attempt must be written "
                "in a separate transaction so it survives the "
                "caller's rollback (Requirement 7.6)."
            )

        # Input validation runs first — a request that violates any
        # Requirement-6.2 / 6.7 range or any AD-WS-10 / AD-WS-11
        # enumeration is rejected before we touch the database, the
        # identity service, or the authorization service (task 8.2).
        # Validators that examine the request itself run before any
        # validator that examines ``omissions`` so that a request
        # missing ``target_recommendation_id`` is rejected with the
        # most actionable constraint name even when the omissions
        # list is also malformed.
        self._validate_decision_required_strings(
            target_recommendation_id=target_recommendation_id,
            target_recommendation_revision_id=target_recommendation_revision_id,
            deciding_party_id=deciding_party_id,
            applicable_scope=applicable_scope,
        )
        self._validate_decision_outcome(outcome)
        self._validate_decision_rationale(rationale)
        self._validate_authority_basis(authority_basis)
        omissions_tuple = tuple(omissions)
        for index, entry in enumerate(omissions_tuple):
            self._validate_omission_entry(entry, index=index)

        # Verify the target Recommendation Revision resolves *before*
        # any write. The composite check confirms the
        # ``recommendation_id`` column on the named Revision matches
        # the supplied Resource Identity — a malicious or buggy
        # caller cannot pair a real Revision with a different
        # Resource and have it accepted.
        self._require_recommendation_revision_exists(
            connection,
            target_recommendation_id=target_recommendation_id,
            target_recommendation_revision_id=target_recommendation_revision_id,
        )

        # Pre-check Requirement 6.5. The UNIQUE constraint on
        # ``Decisions(target_recommendation_id,
        # target_recommendation_revision_id)`` is the source of
        # truth; this read gives a structured error with the
        # existing ``decision_id`` instead of a generic
        # IntegrityError.
        existing_decision_id = self._existing_decision_for(
            connection,
            target_recommendation_id=target_recommendation_id,
            target_recommendation_revision_id=target_recommendation_revision_id,
        )
        if existing_decision_id is not None:
            raise DecisionConflictError(
                target_recommendation_id=target_recommendation_id,
                target_recommendation_revision_id=target_recommendation_revision_id,
                existing_decision_id=existing_decision_id,
            )

        # Single clock reading shared by every row in the transaction
        # (design §"Cross-Cutting Concerns" — *Transactionality*). The
        # authorization evaluation row, the Decisions row, the
        # Addresses Relationship row, the Provenance Manifest row,
        # every Omission Entry row, and the consequential audit row
        # all share this timestamp; the optional ``evaluation_at``
        # parameter only changes which *effective time* authority is
        # evaluated *as of*, not the recorded time of any row.
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()

        # Authorization check (Requirement 7.1). The check runs after
        # we have confirmed the target exists and is not already
        # decided (matching design §"Decision authority evaluation
        # flow") but before any write. On ``permit`` the evaluation
        # audit row appended by
        # :meth:`AuthorizationService.evaluate` participates in this
        # transaction and commits alongside the Decision; on ``deny``
        # the evaluation row rolls back with the caller's transaction
        # and the durable record of the denial is written by
        # :meth:`_persist_decision_denial` in a SEPARATE transaction
        # (Requirement 7.6).
        if self.authorization_service is not None:
            # The fail-fast precondition at the top of the method
            # guarantees ``engine`` is non-``None`` here when
            # ``authorization_service`` is wired; the
            # ``assert engine is not None`` is a static-typing-friendly
            # narrowing rather than a runtime check.
            assert engine is not None  # noqa: S101 - narrowing for type-checkers
            evaluate_at = (
                evaluation_at if evaluation_at is not None else recorded_time
            )
            # Run the authority evaluation on a SEPARATE transaction
            # rather than the caller's. Two reasons:
            #
            # 1. SQLite single-writer constraint. The evaluation
            #    appends one row to ``Audit_Records`` (Requirement
            #    12.5). If that write went into the caller's
            #    transaction, the caller's connection would become
            #    SQLite's exclusive writer — and the subsequent
            #    Denial-Record write (Requirement 7.6) would
            #    deadlock waiting for the caller's transaction to
            #    release the lock. The caller can never release that
            #    lock until ``create_decision`` returns (which
            #    requires the denial write to complete), producing a
            #    classic ABA dependency. Opening a fresh transaction
            #    keeps the caller's connection a reader so the
            #    denial transaction can proceed.
            #
            # 2. Design alignment. Design §"Decision authority
            #    evaluation flow" already places the evaluation
            #    *before* step 5 ("begin a transaction that inserts
            #    the Decision row…") — i.e. outside the consequential
            #    transaction. This change makes the implementation
            #    match the design's ordering directly.
            #
            # Trade-off: Requirement 12.5 ("append the evaluation
            # record in the same transaction as the consequential
            # write that consumed it") is honored *correlationally*
            # via the shared ``correlation_id`` rather than
            # atomically. For an in-memory single-writer database
            # this is the practical accommodation; on a database
            # that supports concurrent writers (PostgreSQL/MySQL)
            # the evaluation could be folded back into the caller's
            # transaction without any other change to the flow.
            with engine.begin() as eval_conn:
                decision = self.authorization_service.evaluate(
                    eval_conn,
                    party_id=deciding_party_id,
                    action=_AUTHORIZATION_ACTION_APPROVE_DECISION,
                    target=TargetRef(
                        kind=_KIND_RECOMMENDATION_REVISION,
                        id=target_recommendation_id,
                        revision_id=target_recommendation_revision_id,
                        scope=applicable_scope,
                    ),
                    at=evaluate_at,
                    correlation_id=correlation,
                )
            if decision.is_deny:
                # Persist the Denial Record outside the caller's
                # transaction so it survives the rollback the
                # ``DecisionAuthorizationError`` is about to trigger
                # (Requirement 7.6, AD-WS-9). On total audit failure
                # the helper raises :class:`DecisionAuditFailureError`
                # which propagates here unchanged — the caller's
                # transaction still rolls back (Requirement 7.5) but
                # the operator is told that denial-and-audit silently
                # diverged.
                reason_code = decision.reason_code or "no-role-assignment"
                self._persist_decision_denial(
                    engine=engine,
                    actor_party_id=deciding_party_id,
                    target_recommendation_id=target_recommendation_id,
                    target_recommendation_revision_id=(
                        target_recommendation_revision_id
                    ),
                    reason_code=reason_code,
                    correlation_id=decision.correlation_id,
                    recorded_time=evaluate_at,
                )
                raise DecisionAuthorizationError(
                    reason_code=reason_code,
                    correlation_id=decision.correlation_id,
                )

        # Mint identifiers up-front so they are available for the
        # registry write, the Decisions row, the Addresses
        # Relationship, and the audit row. The Provenance Manifest
        # identifier is minted by the wired
        # :class:`ProvenanceManifestWriter` so the writer owns the
        # full manifest write contract (Requirement 10.1, AD-WS-5).
        # When the writer is *not* wired (back-compat for callers that
        # pre-date task 9.2) the manifest identifier is minted here so
        # the inline INSERT path below still has a value to use.
        decision_id = str(self.identity_service.new_immutable_record_id())
        addresses_relationship_id = str(
            self.identity_service.new_relationship_id()
        )
        inline_manifest_id = (
            None
            if self.manifest_writer is not None
            else str(self.identity_service.new_manifest_id())
        )

        # Bind the Decision identifier to a digest derived from the
        # Decision's natural content. A Decision has no revisions
        # (AD-WS-3 / AD-WS-4 — it is the Immutable Record itself), so
        # there is exactly one binding to register here (mirroring the
        # Resource / first-Revision pattern in
        # :meth:`create_finding` and :meth:`create_recommendation`
        # but collapsed to a single registration because the Decision
        # has no separate Resource-vs-Revision identity).
        decision_digest = _sha256_hex(
            json.dumps(
                {
                    "target_recommendation_id": target_recommendation_id,
                    "target_recommendation_revision_id": (
                        target_recommendation_revision_id
                    ),
                    "outcome": outcome,
                    "rationale": rationale,
                    "deciding_party_id": deciding_party_id,
                    "authority_basis_type": authority_basis.type,
                    "authority_basis_id": str(authority_basis.id),
                    "applicable_scope": applicable_scope,
                },
                sort_keys=True,
            ).encode("utf-8")
        )
        self.identity_service.reject_if_duplicate(
            decision_id,
            decision_digest,
            connection=connection,
            kind="immutable_record",
            actor_party_id=deciding_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_DECISION,
            recorded_time=recorded_time,
        )

        # Insert the immutable Decisions row.
        connection.execute(
            text(
                """
                INSERT INTO Decisions (
                    decision_id, target_recommendation_id,
                    target_recommendation_revision_id, outcome,
                    rationale, deciding_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :decision_id, :target_recommendation_id,
                    :target_recommendation_revision_id, :outcome,
                    :rationale, :deciding_party_id,
                    :authority_basis_type, :authority_basis_id,
                    :applicable_scope, :recorded_at
                )
                """
            ),
            {
                "decision_id": decision_id,
                "target_recommendation_id": target_recommendation_id,
                "target_recommendation_revision_id": (
                    target_recommendation_revision_id
                ),
                "outcome": outcome,
                "rationale": rationale,
                "deciding_party_id": deciding_party_id,
                "authority_basis_type": authority_basis.type,
                "authority_basis_id": str(authority_basis.id),
                "applicable_scope": applicable_scope,
                "recorded_at": recorded_at,
            },
        )

        # Insert the single ``Addresses`` Relationship row
        # (Requirement 6.3). ``source_revision_id`` is NULL because a
        # Decision has no revisions; ``target_revision_id`` is the
        # Recommendation Revision the Decision addresses.
        self._insert_relationship(
            connection,
            relationship_id=addresses_relationship_id,
            relationship_type=_RELATIONSHIP_TYPE_ADDRESSES,
            source_kind=_KIND_DECISION,
            source_id=decision_id,
            source_revision_id=None,
            target_kind=_KIND_RECOMMENDATION_REVISION,
            target_id=target_recommendation_id,
            target_revision_id=target_recommendation_revision_id,
            authoring_party_id=deciding_party_id,
            recorded_at=recorded_at,
        )

        # Insert the Provenance Manifest plus its Omission Entries.
        # When the slice's :class:`ProvenanceManifestWriter` is wired
        # (task 9.2) the manifest write is delegated to the writer so
        # one canonical implementation covers Finding, Recommendation,
        # Decision, and Trail Revision finalization. The writer's
        # INSERTs participate in the caller's transaction, so a
        # manifest-persistence failure rolls back the Decision,
        # Addresses Relationship, and (when raised) the Identifier
        # Registry binding (Requirements 2.7, 10.6, 13.6 / AD-WS-5).
        # When the writer is *not* wired the original inline path
        # runs unchanged so callers that pre-date task 9.2 (and the
        # tests that exercise them) continue to work.
        if self.manifest_writer is not None:
            manifest_omissions = tuple(
                ManifestOmissionEntry(
                    excluded_source_id=entry.excluded_source_id,
                    excluded_source_revision_id=(
                        entry.excluded_source_revision_id
                    ),
                    category=entry.category,
                    rationale=entry.rationale,
                )
                for entry in omissions_tuple
            )
            manifest_result = self.manifest_writer.write_manifest(
                connection,
                subject_kind=_MANIFEST_SUBJECT_KIND_DECISION,
                subject_id=decision_id,
                subject_revision_id=None,
                authoring_party_id=deciding_party_id,
                included_sources=(
                    IncludedSource(
                        kind=_KIND_RECOMMENDATION_REVISION,
                        resource_id=target_recommendation_id,
                        revision_id=target_recommendation_revision_id,
                        recorded_at=recorded_time,
                    ),
                ),
                omissions=manifest_omissions,
                recorded_at=recorded_time,
            )
            manifest_id = manifest_result.manifest_id
            omission_entry_ids = list(manifest_result.omission_entry_ids)
        else:
            # Back-compat inline path. The Decision's single material
            # source is the target Recommendation Revision —
            # downstream provenance traversals (task 12.2) walk from
            # this node to the Findings and Region Occurrences that
            # justify the Recommendation. ``is_complete`` is 0 when
            # any omission entry has a non-intentional category, per
            # design item 9 of §"Persistence Invariants Summary".
            assert inline_manifest_id is not None  # narrowed by the if branch
            manifest_id = inline_manifest_id
            is_complete = self._manifest_is_complete(omissions_tuple)
            included_sources_json = json.dumps(
                [
                    {
                        "kind": _KIND_RECOMMENDATION_REVISION,
                        "resource_id": target_recommendation_id,
                        "revision_id": target_recommendation_revision_id,
                        "recorded_at": recorded_at,
                    }
                ]
            )
            connection.execute(
                text(
                    """
                    INSERT INTO Provenance_Manifests (
                        manifest_id, subject_kind, subject_id,
                        subject_revision_id, authoring_party_id,
                        recorded_at, included_sources_json, is_complete
                    ) VALUES (
                        :manifest_id, :subject_kind, :subject_id,
                        NULL, :authoring_party_id,
                        :recorded_at, :included_sources_json, :is_complete
                    )
                    """
                ),
                {
                    "manifest_id": manifest_id,
                    "subject_kind": _MANIFEST_SUBJECT_KIND_DECISION,
                    "subject_id": decision_id,
                    "authoring_party_id": deciding_party_id,
                    "recorded_at": recorded_at,
                    "included_sources_json": included_sources_json,
                    "is_complete": 1 if is_complete else 0,
                },
            )

            # Insert one Omission_Entries row per supplied entry. The
            # ``omission_entry_id`` is a fresh UUIDv7 — it is a row
            # identifier rather than a managed Identity in
            # ``Identifier_Registry`` (mirroring the pattern used by
            # AuditLog for ``audit_record_id``).
            omission_entry_ids = []
            for entry in omissions_tuple:
                omission_entry_id = str(uuid_utils.uuid7())
                connection.execute(
                    text(
                        """
                        INSERT INTO Omission_Entries (
                            omission_entry_id, manifest_id,
                            excluded_source_id, excluded_source_revision_id,
                            category, rationale, authoring_party_id,
                            recorded_at, resolved_at
                        ) VALUES (
                            :omission_entry_id, :manifest_id,
                            :excluded_source_id, :excluded_source_revision_id,
                            :category, :rationale, :authoring_party_id,
                            :recorded_at, NULL
                        )
                        """
                    ),
                    {
                        "omission_entry_id": omission_entry_id,
                        "manifest_id": manifest_id,
                        "excluded_source_id": entry.excluded_source_id,
                        "excluded_source_revision_id": (
                            entry.excluded_source_revision_id
                        ),
                        "category": entry.category,
                        "rationale": entry.rationale,
                        "authoring_party_id": deciding_party_id,
                        "recorded_at": recorded_at,
                    },
                )
                omission_entry_ids.append(omission_entry_id)

        # Audit append participates in the caller's transaction so a
        # failure here rolls back the Decision, Addresses
        # Relationship, Manifest, and Omission Entries together
        # (AD-WS-5, Requirement 13.6). ``target_revision_id`` is
        # NULL because a Decision is an Immutable Record with no
        # Revision Identity (AD-WS-3, AD-WS-4).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=deciding_party_id,
            action_type=_AUDIT_ACTION_CREATE_DECISION,
            target_id=decision_id,
            target_revision_id=None,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateDecisionResult(
            decision_id=decision_id,
            target_recommendation_id=target_recommendation_id,
            target_recommendation_revision_id=(
                target_recommendation_revision_id
            ),
            outcome=outcome,
            rationale=rationale,
            deciding_party_id=deciding_party_id,
            authority_basis_type=authority_basis.type,
            authority_basis_id=str(authority_basis.id),
            applicable_scope=applicable_scope,
            addresses_relationship_id=addresses_relationship_id,
            manifest_id=manifest_id,
            omission_entry_ids=tuple(omission_entry_ids),
            recorded_at=recorded_at,
        )

    # -- internal helpers --------------------------------------------------

    def _insert_relationship(
        self,
        connection: Connection,
        *,
        relationship_id: str,
        relationship_type: str,
        source_kind: str,
        source_id: str,
        source_revision_id: Optional[str],
        target_kind: str,
        target_id: str,
        target_revision_id: Optional[str],
        authoring_party_id: str,
        recorded_at: str,
    ) -> None:
        """Insert one ``Relationships`` row.

        Centralizing the INSERT here keeps the column list in one
        place — if a future task adds (or renames) a Relationships
        column both the ``Supports`` and ``Contradicts`` writers pick
        it up automatically.
        """
        connection.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :relationship_id, :relationship_type,
                    :source_kind, :source_id, :source_revision_id,
                    :target_kind, :target_id, :target_revision_id,
                    :authoring_party_id, :recorded_at
                )
                """
            ),
            {
                "relationship_id": relationship_id,
                "relationship_type": relationship_type,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_revision_id": source_revision_id,
                "target_kind": target_kind,
                "target_id": target_id,
                "target_revision_id": target_revision_id,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

    def _require_region_occurrence_exists(
        self, connection: Connection, support: SupportRef
    ) -> None:
        """Verify the composite PK ``(region_id, document_revision_id)``.

        A non-resolvable citation raises
        :class:`FindingNotResolvableError` so the caller of
        :meth:`create_finding` learns *which* Region Occurrence is
        missing without having to inspect the database itself.
        Performing the check via a single SELECT against the composite
        PK keeps the validator cheap (the schema's
        ``PRIMARY KEY (region_id, document_revision_id)`` covers the
        lookup) and consistent with how AD-WS-8's backlink index
        targets ``Relationships.target_id`` /
        ``Relationships.target_revision_id``.
        """
        existing = connection.execute(
            text(
                """
                SELECT 1 FROM Region_Occurrences
                WHERE region_id = :region_id
                  AND document_revision_id = :document_revision_id
                """
            ),
            {
                "region_id": support.region_id,
                "document_revision_id": support.document_revision_id,
            },
        ).scalar_one_or_none()
        if existing is None:
            raise FindingNotResolvableError(
                region_id=support.region_id,
                document_revision_id=support.document_revision_id,
            )

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_statement(statement: str) -> None:
        """Reject empty or non-string statements (Requirement 4.1)."""
        if not isinstance(statement, str):
            raise FindingValidationError(
                f"statement must be a str; received "
                f"{type(statement).__name__}.",
                failed_constraint="statement_empty",
            )
        if statement == "":
            raise FindingValidationError(
                "statement is empty; Requirement 4.1 implies a Finding "
                "carries an interpretive statement.",
                failed_constraint="statement_empty",
            )

    @staticmethod
    def _validate_authoring_party(authoring_party_id: Optional[str]) -> None:
        """Reject missing authoring Party (Requirement 4.2).

        The same constraint name (``authoring_party_id_missing``) is
        used by both ``create_finding`` and ``record_contradiction`` so
        the HTTP layer added by task 6.2 surfaces one consistent error
        code for "missing acting Party" across every Knowledge_Service
        write.
        """
        if not authoring_party_id:
            raise FindingValidationError(
                "authoring_party_id is required; Requirement 4.2 demands "
                "every Relationship records the authoring Party.",
                failed_constraint="authoring_party_id_missing",
            )

    @staticmethod
    def _validate_authoring_party_for_recommendation(
        authoring_party_id: Optional[str],
    ) -> None:
        """Reject missing authoring Party for Recommendation creation.

        Per Requirement 5.7's "IF the requester is unauthenticated …
        THEN THE Knowledge_Service SHALL reject the Recommendation
        creation, decline to create any Resource or Revision". The
        empty-party case is the simplest form of "unauthenticated" —
        no Party Identity to authorize against — so we surface it as a
        :class:`RecommendationValidationError` with constraint
        ``"authoring_party_id_missing"``. Tests assert against the
        constraint name rather than the message text so the wording
        can evolve without breaking them.
        """
        if not authoring_party_id:
            raise RecommendationValidationError(
                "authoring_party_id is required; Requirement 5.7 forbids "
                "unauthenticated Recommendation creation.",
                failed_constraint="authoring_party_id_missing",
            )

    @staticmethod
    def _validate_derived_from_count(derived_from: Sequence[str]) -> None:
        """Reject out-of-range ``derived_from`` counts per Requirement 5.1.

        Requirement 5.1 mandates "between 1 and 50 ``Derived From``
        Relationships". We report the out-of-range condition with two
        distinct ``failed_constraint`` values so the HTTP layer (task
        7.2) can render a precise 400 response — Requirement 5.6 also
        names the zero-entries case explicitly, hence the dedicated
        ``derived_from_too_few`` value.
        """
        count = len(derived_from)
        if count < _DERIVED_FROM_MIN:
            raise RecommendationValidationError(
                "derived_from_findings must contain at least "
                f"{_DERIVED_FROM_MIN} entry; Requirement 5.1/5.6 reject "
                "Recommendations with zero Derived From references.",
                failed_constraint="derived_from_too_few",
            )
        if count > _DERIVED_FROM_MAX:
            raise RecommendationValidationError(
                f"derived_from_findings contains {count} entries; "
                f"Requirement 5.1 caps the count at {_DERIVED_FROM_MAX}.",
                failed_constraint="derived_from_too_many",
            )

    @staticmethod
    def _validate_rationale(rationale: Optional[str]) -> None:
        """Reject rationale text outside the Requirement 5.3 range.

        Requirement 5.3 mandates 1..10,000 characters *when rationale
        is provided* (``WHERE the Analyst provides rationale``). Passing
        ``None`` is therefore valid (rationale was not provided);
        passing the empty string is rejected because it is a positive
        statement of "no rationale" that the schema would persist as
        ``''`` — distinct from "rationale not provided". An over-long
        rationale is also rejected.
        """
        if rationale is None:
            return
        if not isinstance(rationale, str):
            raise RecommendationValidationError(
                f"rationale must be a str or None; received "
                f"{type(rationale).__name__}.",
                failed_constraint="rationale_empty",
            )
        if len(rationale) == 0:
            raise RecommendationValidationError(
                "rationale is empty; Requirement 5.3 requires 1..10,000 "
                "characters when rationale is supplied.",
                failed_constraint="rationale_empty",
            )
        if len(rationale) > _RATIONALE_MAX_CHARS:
            raise RecommendationValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_RATIONALE_MAX_CHARS}-character limit imposed by "
                "Requirement 5.3.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_assumptions(assumptions: Sequence[str]) -> None:
        """Reject assumptions outside the Requirement 5.4 ranges.

        Requirement 5.4 mandates "between 0 and 50 assumption entries,
        each of 1 to 2,000 characters". The zero-count case is the
        default (caller omits ``assumptions=``); the upper count bound
        and the per-entry length range are checked here. The first
        violating entry surfaces the constraint, which is enough for
        the HTTP layer (task 7.2) to point the caller at the failing
        index.
        """
        count = len(assumptions)
        if count > _ASSUMPTIONS_MAX_ENTRIES:
            raise RecommendationValidationError(
                f"assumptions contains {count} entries; Requirement 5.4 "
                f"caps the count at {_ASSUMPTIONS_MAX_ENTRIES}.",
                failed_constraint="assumptions_too_many",
            )
        for index, entry in enumerate(assumptions):
            if not isinstance(entry, str):
                raise RecommendationValidationError(
                    f"assumptions[{index}] must be a str; received "
                    f"{type(entry).__name__}.",
                    failed_constraint="assumption_empty",
                )
            if len(entry) == 0:
                raise RecommendationValidationError(
                    f"assumptions[{index}] is empty; Requirement 5.4 "
                    "requires every entry to carry 1..2,000 characters.",
                    failed_constraint="assumption_empty",
                )
            if len(entry) > _ASSUMPTION_MAX_CHARS:
                raise RecommendationValidationError(
                    f"assumptions[{index}] length {len(entry)} exceeds "
                    f"the {_ASSUMPTION_MAX_CHARS}-character per-entry "
                    "limit imposed by Requirement 5.4.",
                    failed_constraint="assumption_too_long",
                )

    @staticmethod
    def _validate_confidence(confidence: Optional[str]) -> None:
        """Reject confidence values outside ``{Low, Medium, High}``.

        Requirement 5.5 mandates "a confidence value drawn from the
        enumerated set {Low, Medium, High}" *when supplied*; ``None``
        is therefore valid (confidence was not provided). The
        comparison is case-sensitive — the schema's CHECK constraint
        also accepts only these exact spellings, so coercing here
        would only push the rejection deeper and produce a less
        actionable error message.
        """
        if confidence is None:
            return
        if confidence not in _CONFIDENCE_VALUES:
            raise RecommendationValidationError(
                f"confidence {confidence!r} is not one of "
                f"{sorted(_CONFIDENCE_VALUES)!r}; Requirement 5.5 limits "
                "the value to the named enumeration.",
                failed_constraint="confidence_invalid",
            )

    def _require_finding_exists(
        self, connection: Connection, finding_id: str
    ) -> None:
        """Verify ``finding_id`` resolves to a ``Findings`` row.

        A non-resolvable Derived From reference raises
        :class:`RecommendationNotResolvableError` so the caller of
        :meth:`create_recommendation` learns *which* Finding is missing
        without having to inspect the database itself. The check is a
        single PK lookup against ``Findings.finding_id``; the table's
        PRIMARY KEY covers the read.
        """
        existing = connection.execute(
            text("SELECT 1 FROM Findings WHERE finding_id = :finding_id"),
            {"finding_id": finding_id},
        ).scalar_one_or_none()
        if existing is None:
            raise RecommendationNotResolvableError(finding_id=finding_id)

    # -- Decision validators and helpers -----------------------------------

    @staticmethod
    def _validate_decision_required_strings(
        *,
        target_recommendation_id: Optional[str],
        target_recommendation_revision_id: Optional[str],
        deciding_party_id: Optional[str],
        applicable_scope: Optional[str],
    ) -> None:
        """Reject Decision submissions missing required string attributes.

        Per Requirement 6.7: "IF the submitted Decision omits a required
        attribute (target Recommendation Identity, target Recommendation
        Revision Identity, decision outcome, rationale, deciding Party
        Identity, authority basis, applicable scope, or recorded time),
        THEN THE Knowledge_Service SHALL reject the submission …".
        Each missing attribute surfaces a distinct ``failed_constraint``
        so the HTTP layer (task 8.3) can point the caller at the
        specific field. ``outcome``, ``rationale``, and
        ``authority_basis`` get their own validators (the outcome
        validator covers the missing-and-invalid cases together; the
        rationale validator distinguishes empty from over-long; the
        authority-basis validator covers the missing object case).
        ``recorded_at`` is sourced from :attr:`clock`.now() so it is
        always present and is not validated here.
        """
        if not target_recommendation_id:
            raise DecisionValidationError(
                "target_recommendation_id is required; Requirement 6.7 "
                "rejects Decisions missing the target Recommendation "
                "Identity.",
                failed_constraint="target_recommendation_id_missing",
            )
        if not target_recommendation_revision_id:
            raise DecisionValidationError(
                "target_recommendation_revision_id is required; "
                "Requirement 6.7 rejects Decisions missing the target "
                "Recommendation Revision Identity.",
                failed_constraint="target_recommendation_revision_id_missing",
            )
        if not deciding_party_id:
            raise DecisionValidationError(
                "deciding_party_id is required; Requirement 6.7 rejects "
                "Decisions missing the deciding Party Identity.",
                failed_constraint="deciding_party_id_missing",
            )
        if not applicable_scope:
            raise DecisionValidationError(
                "applicable_scope is required; Requirement 6.7 rejects "
                "Decisions missing the applicable scope.",
                failed_constraint="applicable_scope_missing",
            )

    @staticmethod
    def _validate_decision_outcome(outcome: Optional[str]) -> None:
        """Reject ``outcome`` outside ``{Accept, Reject, Defer}`` (AD-WS-11).

        Requirement 6.2 names the slice's three permitted outcomes; the
        upstream Requirement REQ-KP-008 permits a fourth value
        (``Supersede``) which AD-WS-11 explicitly excludes for the
        slice. A missing or non-string outcome surfaces as
        ``outcome_invalid`` rather than a dedicated "missing" code
        because the caller's actionable next step in both cases is
        "supply one of {Accept, Reject, Defer}".
        """
        if outcome is None or not isinstance(outcome, str) or outcome == "":
            raise DecisionValidationError(
                "outcome is required and must be one of "
                f"{sorted(_DECISION_OUTCOMES)!r}; Requirement 6.7 / AD-WS-11.",
                failed_constraint="outcome_invalid",
            )
        if outcome not in _DECISION_OUTCOMES:
            raise DecisionValidationError(
                f"outcome {outcome!r} is not one of "
                f"{sorted(_DECISION_OUTCOMES)!r}; AD-WS-11 restricts the "
                "slice to the three named outcomes.",
                failed_constraint="outcome_invalid",
            )

    @staticmethod
    def _validate_decision_rationale(rationale: Optional[str]) -> None:
        """Reject rationale outside the Requirement 6.2 range.

        Requirement 6.2 mandates "rationale text of 1 to 4,000
        characters" on every Decision Immutable Record. ``Decisions``'
        schema column is ``NOT NULL``; the empty string is therefore
        rejected too. The constraint name ``rationale_missing`` covers
        both the empty and the non-string cases because the caller's
        actionable next step is the same: supply non-empty text.
        """
        if rationale is None or not isinstance(rationale, str):
            raise DecisionValidationError(
                "rationale is required and must be a non-empty string of "
                f"{_DECISION_RATIONALE_MIN_CHARS}.."
                f"{_DECISION_RATIONALE_MAX_CHARS} characters; "
                "Requirement 6.2 / 6.7.",
                failed_constraint="rationale_missing",
            )
        if len(rationale) < _DECISION_RATIONALE_MIN_CHARS:
            raise DecisionValidationError(
                "rationale is empty; Requirement 6.2 requires "
                f"{_DECISION_RATIONALE_MIN_CHARS}.."
                f"{_DECISION_RATIONALE_MAX_CHARS} characters.",
                failed_constraint="rationale_missing",
            )
        if len(rationale) > _DECISION_RATIONALE_MAX_CHARS:
            raise DecisionValidationError(
                f"rationale length {len(rationale)} exceeds the "
                f"{_DECISION_RATIONALE_MAX_CHARS}-character limit imposed "
                "by Requirement 6.2.",
                failed_constraint="rationale_too_long",
            )

    @staticmethod
    def _validate_authority_basis(
        authority_basis: Optional[AuthorityBasisRef],
    ) -> None:
        """Reject missing or out-of-enumeration authority basis (AD-WS-10).

        Requirement 6.7 demands an authority basis on every Decision;
        AD-WS-10 enumerates the slice's three permitted basis types
        (``role-grant-id``, ``scope-id``, ``delegation-chain-id``).
        The :class:`AuthorityBasisRef` Pydantic model declares the
        ``type`` field as a ``Literal`` of those three values and so
        normally rejects bad input at construction time — the
        defensive check here covers callers that obtain the value
        through a path that does not go through Pydantic validation
        (e.g. ``model_construct``, deserialization replay, or tests
        that bypass the model). The ``id`` field's presence is
        enforced by the model's ``UUID`` annotation.
        """
        if authority_basis is None:
            raise DecisionValidationError(
                "authority_basis is required; Requirement 6.7 / AD-WS-10 "
                "demand a recorded authority basis on every Decision.",
                failed_constraint="authority_basis_missing",
            )
        if authority_basis.type not in _AUTHORITY_BASIS_TYPES:
            raise DecisionValidationError(
                f"authority_basis.type {authority_basis.type!r} is not one "
                f"of {sorted(_AUTHORITY_BASIS_TYPES)!r}; AD-WS-10 restricts "
                "the slice to the three named basis types.",
                failed_constraint="authority_basis_type_invalid",
            )

    @staticmethod
    def _validate_omission_entry(
        entry: DecisionOmissionEntry, *, index: int
    ) -> None:
        """Reject malformed :class:`DecisionOmissionEntry` instances.

        Each entry must carry a non-empty ``excluded_source_id``, one
        of the five permitted ``category`` values (mirroring the
        schema CHECK on ``Omission_Entries.category``), and a
        non-empty rationale of at most 2,000 characters (Requirement
        10.2 and the schema column ``NOT NULL`` constraint). The
        first violating entry surfaces a constraint name that
        includes its zero-based index so the HTTP layer can point the
        caller at the specific entry.
        """
        if not entry.excluded_source_id:
            raise DecisionValidationError(
                f"omissions[{index}].excluded_source_id is required.",
                failed_constraint="omission_excluded_source_id_missing",
            )
        if entry.category not in _OMISSION_CATEGORIES:
            raise DecisionValidationError(
                f"omissions[{index}].category {entry.category!r} is not "
                f"one of {sorted(_OMISSION_CATEGORIES)!r}; Requirement "
                "10.3 names the five permitted categories.",
                failed_constraint="omission_category_invalid",
            )
        if not isinstance(entry.rationale, str) or entry.rationale == "":
            raise DecisionValidationError(
                f"omissions[{index}].rationale is empty; Requirement 10.2 "
                f"requires {_OMISSION_RATIONALE_MIN_CHARS}.."
                f"{_OMISSION_RATIONALE_MAX_CHARS} characters.",
                failed_constraint="omission_rationale_missing",
            )
        if len(entry.rationale) > _OMISSION_RATIONALE_MAX_CHARS:
            raise DecisionValidationError(
                f"omissions[{index}].rationale length "
                f"{len(entry.rationale)} exceeds the "
                f"{_OMISSION_RATIONALE_MAX_CHARS}-character limit "
                "imposed by Requirement 10.2.",
                failed_constraint="omission_rationale_too_long",
            )

    @staticmethod
    def _manifest_is_complete(
        omissions: Sequence[DecisionOmissionEntry],
    ) -> bool:
        """Compute ``Provenance_Manifests.is_complete`` from the omissions.

        Per design §"Persistence Invariants Summary" item 9 and
        Requirement 10.3, a manifest is incomplete when any
        unresolved Omission Entry has a non-intentional category
        (``unavailable``, ``restricted``, ``stale``, ``unresolved``).
        Newly inserted entries have ``resolved_at = NULL`` (i.e.
        unresolved) so the check reduces to "are any of the supplied
        entries in a non-intentional category?". The caller can
        materialize a complete manifest by passing only intentional
        (or zero) omissions; that is the common case for the slice's
        happy path.
        """
        for entry in omissions:
            if entry.category != "intentional":
                return False
        return True

    def _require_recommendation_revision_exists(
        self,
        connection: Connection,
        *,
        target_recommendation_id: str,
        target_recommendation_revision_id: str,
    ) -> None:
        """Verify the target Recommendation Revision resolves.

        The composite check confirms the named Revision exists *and*
        its ``recommendation_id`` matches the supplied Resource
        Identity. A caller cannot pair a real Revision with a
        different Resource and have it accepted — the check protects
        against both the missing-Revision case and the
        mismatched-Resource case.
        """
        existing = connection.execute(
            text(
                """
                SELECT 1 FROM Recommendation_Revisions
                WHERE recommendation_revision_id = :rrid
                  AND recommendation_id = :rid
                """
            ),
            {
                "rrid": target_recommendation_revision_id,
                "rid": target_recommendation_id,
            },
        ).scalar_one_or_none()
        if existing is None:
            raise RecommendationRevisionNotResolvableError(
                target_recommendation_id=target_recommendation_id,
                target_recommendation_revision_id=(
                    target_recommendation_revision_id
                ),
            )

    @staticmethod
    def _existing_decision_for(
        connection: Connection,
        *,
        target_recommendation_id: str,
        target_recommendation_revision_id: str,
    ) -> Optional[str]:
        """Return the ``decision_id`` already addressing the target, if any.

        The ``UNIQUE`` constraint on
        ``Decisions(target_recommendation_id,
        target_recommendation_revision_id)`` guarantees at most one
        match. Returning the existing ``decision_id`` lets the
        caller render a structured 409-shaped error pointing at the
        prior Decision (Requirement 6.5).
        """
        row = connection.execute(
            text(
                """
                SELECT decision_id FROM Decisions
                WHERE target_recommendation_id = :rid
                  AND target_recommendation_revision_id = :rrid
                """
            ),
            {
                "rid": target_recommendation_id,
                "rrid": target_recommendation_revision_id,
            },
        ).scalar_one_or_none()
        return row if row is None else str(row)

    def get_decision(
        self,
        connection: Connection,
        decision_id: str,
    ) -> Optional[DecisionRecord]:
        """Return the Decision Immutable Record matching ``decision_id``, or ``None``.

        Slice 2 AD-WS-21 names this method as the *public read API*
        through which the Planning_Service resolves the target Decision
        of a new Objective (Requirement 2.2). Keeping the access
        boundary at the Knowledge_Service preserves the bounded-context
        invariant from Principle 5.2 — the Planning_Service never reads
        the ``Decisions`` table directly.

        The method runs as a simple primary-key SELECT against
        ``Decisions``; no audit row is appended because read access is
        non-consequential in this slice (matching the
        ``GET /api/v1/decisions/{decision_id}`` route handler in
        :mod:`walking_slice.routes.decisions`). The read participates
        in the caller's transaction so a caller that is preparing to
        write inside the same transaction sees a consistent view.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction (or any read-capable connection).
            decision_id: The Decision Immutable Record Identity to
                resolve.

        Returns:
            A frozen :class:`DecisionRecord` snapshot when the row
            exists; ``None`` when no ``Decisions`` row matches
            ``decision_id``. The caller decides whether a missing
            Decision is a validation failure (e.g.,
            :class:`walking_slice.planning.objectives.ObjectiveDecisionNotResolvableError`)
            or some other condition.
        """
        row = (
            connection.execute(
                text(
                    """
                    SELECT decision_id, target_recommendation_id,
                           target_recommendation_revision_id, outcome,
                           rationale, deciding_party_id,
                           authority_basis_type, authority_basis_id,
                           applicable_scope, recorded_at
                    FROM Decisions
                    WHERE decision_id = :decision_id
                    """
                ),
                {"decision_id": decision_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return DecisionRecord(
            decision_id=str(row["decision_id"]),
            target_recommendation_id=str(row["target_recommendation_id"]),
            target_recommendation_revision_id=str(
                row["target_recommendation_revision_id"]
            ),
            outcome=str(row["outcome"]),
            rationale=str(row["rationale"]),
            deciding_party_id=str(row["deciding_party_id"]),
            authority_basis_type=str(row["authority_basis_type"]),
            authority_basis_id=str(row["authority_basis_id"]),
            applicable_scope=str(row["applicable_scope"]),
            recorded_at=str(row["recorded_at"]),
        )
    def _persist_decision_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_recommendation_id: str,
        target_recommendation_revision_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Decision attempt.

        Implements the Requirement 7.6 retry contract: each attempt
        opens a *new* :meth:`Engine.begin` transaction (so the
        previous attempt's rollback does not poison this one), tries
        :meth:`AuditLog.append_denial`, and either returns on success
        or pauses by the next entry in
        :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` before the next try.

        Attempt accounting:

        - **Attempt 1** runs immediately.
        - **Attempt 2** runs after a 10-millisecond pause.
        - **Attempt 3** runs after a 20-millisecond pause.
        - **Attempt 4** runs after a 40-millisecond pause.
        - If attempt 4 also fails, :class:`DecisionAuditFailureError`
          is raised; the action remains denied (Requirement 7.6 — the
          caller will still raise
          :class:`DecisionAuthorizationError` if this helper returns
          successfully, but on failure this helper raises in its place
          so denial and audit cannot silently diverge).

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_decision` raises
        :class:`DecisionAuthorizationError` (or this method raises
        :class:`DecisionAuditFailureError`). The Denial Record must
        therefore live outside that scope to survive — see design
        AD-WS-5 and the ``_append_identifier_conflict_denial`` helper
        in :mod:`walking_slice.identity` for the same pattern applied
        to the identifier-conflict denial path.

        Both :class:`AuditAppendError` and :class:`SQLAlchemyError`
        are treated as retryable failures: the former wraps the
        latter for callers who use :class:`AuditLog`, but a
        transaction-management failure (e.g. ``engine.begin()`` itself
        raising) surfaces as a bare :class:`SQLAlchemyError` so we
        catch both. Unrelated exceptions propagate unchanged.

        Args:
            engine: The :class:`Engine` used to open the separate
                transaction. Must be non-``None`` — the fail-fast
                precondition at the top of :meth:`create_decision`
                guarantees this.
            actor_party_id: The deciding Party identity to record on
                the Denial Record (Requirement 7.2).
            target_recommendation_id: Identity of the target
                Recommendation Resource the attempt was against.
            target_recommendation_revision_id: Identity of the
                target Recommendation Revision.
            reason_code: One of the Requirement 7.2 reason codes
                (``not-yet-effective``, ``expired``, ``revoked``,
                ``out-of-scope``, ``no-role-assignment``).
            correlation_id: The correlation identifier from the
                evaluation that produced the deny; recorded on the
                Denial Record so it can be joined to the (rolled-back)
                evaluation row.
            recorded_time: The recorded time written to the Denial
                Record. Aligns with the time used by the evaluation
                so the audit trail reads coherently.

        Raises:
            DecisionAuditFailureError: All four attempts failed.
                Preserves the underlying SQLAlchemy / audit exception
                as ``__cause__``.
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=_AUTHORIZATION_ACTION_APPROVE_DECISION,
                        target_id=target_recommendation_id,
                        target_revision_id=target_recommendation_revision_id,
                        reason_code=reason_code,
                        correlation_id=correlation_id,
                        recorded_time=recorded_time,
                    )
                return  # success — Denial Record committed in its own tx
            except (AuditAppendError, SQLAlchemyError) as exc:
                last_error = exc
                # Sleep only when another attempt remains. The final
                # iteration falls through to the error raise below.
                if attempt_index < len(_DENIAL_AUDIT_BACKOFFS_SECONDS):
                    self.denial_audit_sleep(
                        _DENIAL_AUDIT_BACKOFFS_SECONDS[attempt_index]
                    )

        raise DecisionAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error
