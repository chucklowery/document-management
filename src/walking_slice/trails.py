"""Trail_Service — Trails, Trail_Revisions, and Trail_Steps persistence with
structural validators (task 10.1).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Trail_Service" + §"Table-by-Table Specification — Trails, Trail_Revisions,
Trail_Steps", AD-WS-4 (immutable Trail_Revisions and Trail_Steps rows),
AD-WS-5 (audit and manifest append inside the originating transaction),
and AD-WS-12 (slice restricts ``selection_mode`` to ``'Pinned'``).

Task scope (task 10.1):

- :meth:`TrailService.create_trail` — record a Trail Resource plus its
  initial immutable Trail Revision and exactly five Trail Steps in one
  transaction (AD-WS-5). The five-step shape mirrors the slice's
  pipeline stages:

    ordinal 1 → ``document_revision``        (Source Evidence)
    ordinal 2 → ``region_occurrence``        (Content Region)
    ordinal 3 → ``finding_revision``         (Finding)
    ordinal 4 → ``recommendation_revision``  (Recommendation)
    ordinal 5 → ``decision``                 (Authorized Decision)

- Structural validation per Requirement 9 / AD-WS-12 runs *before* any
  database round-trip:

    1. Exactly five steps (no more, no fewer).
    2. Ordinals are exactly the contiguous integers 1..5.
    3. Each step's ``target_kind`` matches the kind required for its
       ordinal.
    4. Each step's ``selection_mode`` is ``'Pinned'``.
    5. Purpose is 1..500 characters; audience identifier non-empty;
       ordering rationale (when supplied) 0..500 characters; each
       annotation (when supplied) 0..2,000 characters.

- Target resolvability per Requirement 9.5 runs *before* any INSERT.
  Each step's ``target_ref`` is checked against the appropriate table.
  If any step is unresolved the method raises
  :class:`TrailTargetUnresolvedError` carrying the per-ordinal list;
  no partial persistence occurs because no INSERT has run yet.

- The consequential write inserts (in dependency order):

    Identifier_Registry (trail_id, trail_revision_id, trail_step_id ×5)
    → Trails
    → Trail_Revisions
    → Trail_Steps × 5
    → Trails.current_revision_id update (mutable convenience pointer
      per the schema comment)
    → Provenance_Manifests + Omission_Entries (when a manifest writer
      is wired; the five step targets are the manifest's Included
      Sources per Requirement 10.1)
    → Audit_Records (action_type ``'create.trail'``)

  A failure on any step rolls the caller's transaction back (AD-WS-5,
  Requirements 2.7 and 13.6).

Authorization (optional in task 10.1):

When :attr:`authorization_service` is wired (production composition
in task 15.2 will wire a real :class:`AuthorizationService`), the
method evaluates the caller's authority before any write by calling
``AuthorizationService.evaluate(party, 'create.trail', target, at)``.
The evaluation runs on a SEPARATE transaction for the same reason
:meth:`KnowledgeService.create_decision` does — SQLite's single-writer
model would otherwise deadlock a subsequent Denial Record append. On
deny the method persists a Denial Record outside the caller's
transaction (Requirement 7.6 retry contract) and raises
:class:`TrailAuthorizationError`. When the authorization dependency
is not wired the authority check is skipped — task 10.1's unit tests
and the property tests in tasks 10.5 / 10.6 exercise the persistence
path directly.

Requirements satisfied (per task 10.1):
    9.1 — creates a Trail Resource (Resource Identity distinct from
          every referenced endpoint Identity) and an immutable Trail
          Revision containing exactly one ordered sequence of exactly
          five Trail Steps.
    9.2 — Trail Steps carry ordinals 1 through 5 in ascending order
          without gaps and the target kinds per ordinal match the
          pipeline stages.
    9.3 — Trail Steps record target reference identity,
          ``selection_mode='Pinned'``, optional annotation 0..2,000
          chars, and a unique ordinal 1..5.
    9.5 — Unresolved targets reject the entire Trail Revision request
          with no partial persistence; the response identifies each
          unresolved step by ordinal and target reference.
    9.7 — Submissions with fewer than 5 / more than 5 steps, with
          non-contiguous ordinals, or with target kinds that do not
          match their ordinal's pipeline stage are rejected and no
          Trail Revision is created.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Final, Mapping, Optional, Sequence
from uuid import UUID

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
    ProvenanceManifestWriter,
)
from walking_slice.projection import (
    ProjectedStatusResponse,
    StatusBearingResponse,
    StatusProjector,
)


__all__ = [
    "AppendTrailRevisionResult",
    "CreateTrailResult",
    "ORDINAL_TARGET_KIND",
    "TRAIL_PROJECTION_DEFINITION_NAME",
    "TRAIL_STATUS_RESOLVED",
    "TRAIL_STATUS_UNRESOLVED",
    "TrailAuditFailureError",
    "TrailAuthorizationError",
    "TrailNotFoundError",
    "TrailService",
    "TrailStepInput",
    "TrailStepResult",
    "TrailTargetUnresolvedError",
    "TrailValidationError",
    "UnresolvedTrailStep",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# Required step count for every slice Trail Revision per Requirement 9.1 / 9.7
# ("exactly one ordered sequence of exactly five Trail Steps").
_REQUIRED_STEP_COUNT: Final[int] = 5

# Selection mode permitted on Trail Steps per AD-WS-12 ("Live,
# Approval-Controlled, and Historical-As-Of modes are deferred"). Centralized
# so the structural validator and the schema CHECK constraint in
# :mod:`walking_slice.persistence` agree on the spelling.
_PINNED_SELECTION_MODE: Final[str] = "Pinned"

# Bounds drawn from Requirements 9.3 and 9.6 plus the schema column comments
# in :mod:`walking_slice.persistence`. Centralized so structural validation
# fails before any database round-trip with constraint names matching the
# schema's intent.
_PURPOSE_MIN_CHARS: Final[int] = 1
_PURPOSE_MAX_CHARS: Final[int] = 500
_AUDIENCE_MIN_CHARS: Final[int] = 1
_ORDERING_RATIONALE_MAX_CHARS: Final[int] = 500
_ANNOTATION_MAX_CHARS: Final[int] = 2_000

# Target-kind expected for each ordinal per Requirement 9.2 and the CHECK
# constraint on ``Trail_Steps`` in :mod:`walking_slice.persistence`. The
# mapping is the single source of truth — both the structural validator
# and the resolvability checker read from it. Exposed as part of the
# module's public surface so HTTP-layer code (task 10.3) and tests can
# refer to the same table.
ORDINAL_TARGET_KIND: Final[Mapping[int, str]] = {
    1: "document_revision",
    2: "region_occurrence",
    3: "finding_revision",
    4: "recommendation_revision",
    5: "decision",
}

# Required ordinal set (1..5). Pre-computed so structural validation can
# compare submitted ordinals against this set in O(1).
_REQUIRED_ORDINALS: Final[frozenset[int]] = frozenset(ORDINAL_TARGET_KIND)

# Authorization action name per design §"Authorization_Service" ActionType
# enumeration ("create.trail" → ``modify`` authority).
_AUTHORIZATION_ACTION_CREATE_TRAIL: Final[str] = "create.trail"

# Audit action name written to ``Audit_Records.action_type`` for both the
# consequential write (``'consequential'`` outcome) and the denial path
# (``'deny'`` outcome on the separate-transaction Denial Record). Property
# 11 (audit completeness) scans for this string in the audit log so the
# value is centralized to keep it textually stable.
_AUDIT_ACTION_CREATE_TRAIL: Final[str] = "create.trail"

# Exponential backoff sequence for retrying the Denial Record append in a
# separate transaction (Requirement 7.6 — "retry up to 3 times"). Mirrors
# the constant in :mod:`walking_slice.knowledge` so the two consequential
# write paths share one retry policy.
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)

# Provenance Manifest ``subject_kind`` for a Trail Revision manifest per
# the CHECK constraint on ``Provenance_Manifests.subject_kind`` and
# design §"Provenance_Manifests and Omission_Entries".
_MANIFEST_SUBJECT_KIND_TRAIL: Final[str] = "trail_revision"


# ---------------------------------------------------------------------------
# Projection definition constants (task 14.2).
# ---------------------------------------------------------------------------


# Name the slice uses to identify the Trail resolution projection.
# Resolved by :class:`walking_slice.projection.StatusProjector` against
# the definition registry the producer constructs at composition time
# (task 15.2). Centralized so the constant the producer registers and
# the constant the wrapper-producing call site looks up are textually
# identical — a typo at either site triggers the explanation-unavailable
# path (Requirement 14.4) instead of mis-labeling the envelope.
TRAIL_PROJECTION_DEFINITION_NAME: Final[str] = "trail.resolution"

# Status names surfaced inside :class:`ProjectedStatusResponse.status`
# for :meth:`TrailService.create_trail_projected`. Centralized so the
# HTTP layer (task 10.3) and tests match on a stable string rather
# than the human-readable message text.
TRAIL_STATUS_RESOLVED: Final[str] = "trail.resolved"
TRAIL_STATUS_UNRESOLVED: Final[str] = "trail.unresolved"


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class TrailValidationError(ValueError):
    """Raised when a Trail submission fails Requirement 9 structural validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 10.3) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"purpose_missing"``,
            ``"purpose_too_long"``,
            ``"audience_id_missing"``,
            ``"ordering_rationale_too_long"``,
            ``"authoring_party_id_missing"``,
            ``"step_count_invalid"``,
            ``"ordinals_not_contiguous_1_to_5"``,
            ``"target_kind_invalid_for_ordinal"``,
            ``"selection_mode_invalid"``,
            ``"target_id_missing"``,
            ``"target_revision_id_missing"``,
            ``"target_revision_id_unexpected"``,
            ``"region_id_missing"``,
            ``"region_id_unexpected"``,
            ``"annotation_too_long"``.
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


@dataclass(frozen=True)
class UnresolvedTrailStep:
    """Description of one Trail Step whose target could not be resolved.

    Returned (in a list) inside :class:`TrailTargetUnresolvedError` per
    Requirement 9.5: "return an error indication identifying each
    unresolved Trail Step by ordinal and target reference". The HTTP
    layer (task 10.3) renders the per-step descriptors as a JSON array.

    Attributes:
        ordinal: The ordinal of the unresolved step (1..5).
        target_kind: The expected target kind for the ordinal.
        target_id: The Resource / Document Revision / Decision Identity
            the caller supplied.
        target_revision_id: The Revision Identity the caller supplied
            (``None`` when not applicable to the ordinal).
        region_id: The Region Identity supplied for ordinal 2; ``None``
            for every other ordinal.
    """

    ordinal: int
    target_kind: str
    target_id: str
    target_revision_id: Optional[str]
    region_id: Optional[str]


class TrailTargetUnresolvedError(LookupError):
    """Raised by :meth:`TrailService.create_trail` when one or more Trail
    Step targets cannot be resolved (Requirement 9.5).

    The exception carries the full per-step list — Requirement 9.5
    demands the response identify *each* unresolved step, not just the
    first one — so callers (and the HTTP layer in task 10.3) can render
    all failed lookups in a single 400 response.

    The exception is raised *before* any INSERT runs, so no partial
    Trail, Trail Revision, Trail Step, manifest, or audit row is left
    behind.

    Attributes:
        unresolved_steps: Tuple of :class:`UnresolvedTrailStep`,
            preserving submission order so a caller's offset-by-ordinal
            scan returns the steps the caller already iterated over.
        error_code: Always ``'trail_target_unresolved'``. Exposed as a
            class attribute so the HTTP layer can match on a stable
            string instead of the message text.
    """

    error_code: Final[str] = "trail_target_unresolved"

    def __init__(self, *, unresolved_steps: Sequence[UnresolvedTrailStep]) -> None:
        super().__init__(
            f"{len(unresolved_steps)} Trail Step target(s) failed to resolve "
            "(Requirement 9.5); see ``unresolved_steps`` for the per-ordinal "
            "detail."
        )
        self.unresolved_steps: tuple[UnresolvedTrailStep, ...] = tuple(
            unresolved_steps
        )


class TrailAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies a Trail creation.

    Carries ``reason_code`` and ``correlation_id`` so the HTTP layer
    (task 10.3) can render the AD-WS-9 indistinguishable denial response
    shape without re-deriving the values. Mirrors the shape of
    :class:`walking_slice.knowledge.DecisionAuthorizationError`.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Trail creation denied: reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class TrailAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails (Requirement 7.6).

    Mirrors :class:`walking_slice.knowledge.DecisionAuditFailureError` so
    operators see one shape of audit-failure indicator across the two
    consequential write paths.
    """

    def __init__(
        self,
        *,
        reason_code: str,
        correlation_id: str,
        attempts: int,
    ) -> None:
        super().__init__(
            f"Denial Record append for a denied Trail creation failed after "
            f"{attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


class TrailNotFoundError(LookupError):
    """Raised by :meth:`TrailService.append_revision` when ``trail_id``
    does not name an existing Trail.

    Requirement 9.4 frames the material-change detection as an *update*
    to an existing Trail: "WHEN a Trail Author updates a Trail by
    recording changes ... THE Trail_Service SHALL create a new Trail
    Revision that links to its immutable predecessor Revision by
    identity". An attempt to append a revision to a non-existent Trail
    cannot satisfy that contract and is rejected before any INSERT.

    Attributes:
        trail_id: The unknown Trail Identity the caller supplied.
        error_code: Always ``'trail_not_found'`` — exposed as a class
            attribute so the HTTP layer (task 10.3) can match on a
            stable string instead of the message text.
    """

    error_code: Final[str] = "trail_not_found"

    def __init__(self, *, trail_id: str) -> None:
        super().__init__(
            f"Trail {trail_id!r} does not exist; append_revision requires "
            "the Trail to have been created first via create_trail "
            "(Requirement 9.4)."
        )
        self.trail_id = trail_id


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrailStepInput:
    """One step in a :meth:`TrailService.create_trail` submission.

    Frozen so the verify-then-write loop cannot observe a different
    list than it just validated (design §"Cross-Cutting Concerns" —
    *Transactionality*).

    Field interpretation per ordinal (Requirement 9.2, the schema
    CHECK constraint on ``Trail_Steps``, and design §"Table-by-Table
    Specification"):

        ordinal 1 (``document_revision``):
            ``target_id`` = Source Document Resource Identity.
            ``target_revision_id`` = Document Revision Identity.
            ``region_id`` = None.
        ordinal 2 (``region_occurrence``):
            ``target_id`` = owning Document Revision Identity.
            ``target_revision_id`` = None (the occurrence's composite
            PK is ``(region_id, document_revision_id)`` — the schema
            comment says "NULL for region_occurrence" because the
            occurrence already carries its own composite key).
            ``region_id`` = Content Region Identity.
        ordinal 3 (``finding_revision``):
            ``target_id`` = Finding Resource Identity.
            ``target_revision_id`` = Finding Revision Identity.
            ``region_id`` = None.
        ordinal 4 (``recommendation_revision``):
            ``target_id`` = Recommendation Resource Identity.
            ``target_revision_id`` = Recommendation Revision Identity.
            ``region_id`` = None.
        ordinal 5 (``decision``):
            ``target_id`` = Decision Immutable Record Identity.
            ``target_revision_id`` = None (Decisions have no Revision
            Identity per AD-WS-3 / AD-WS-4).
            ``region_id`` = None.

    Attributes:
        ordinal: The step's ordinal position (1..5 — uniqueness within
            the request is enforced by structural validation).
        target_kind: One of the five values listed in
            :data:`ORDINAL_TARGET_KIND` matching the ordinal.
        target_id: The primary identifier of the target row (see the
            per-ordinal table above).
        target_revision_id: The Revision Identity of the target, when
            applicable.
        region_id: The Region Identity for ordinal 2; ``None``
            otherwise.
        selection_mode: Always ``'Pinned'`` for the slice (AD-WS-12);
            other values are rejected by structural validation.
        annotation: Optional 0..2,000-character annotation per
            Requirement 9.3.
    """

    ordinal: int
    target_kind: str
    target_id: str
    target_revision_id: Optional[str] = None
    region_id: Optional[str] = None
    selection_mode: str = _PINNED_SELECTION_MODE
    annotation: Optional[str] = None


@dataclass(frozen=True)
class TrailStepResult:
    """One step in :class:`CreateTrailResult`.

    Returned in ordinal order so callers can correlate the issued
    ``trail_step_id`` values back to the submitted
    :class:`TrailStepInput` entries.
    """

    trail_step_id: str
    ordinal: int
    target_kind: str
    target_id: str
    target_revision_id: Optional[str]
    region_id: Optional[str]
    selection_mode: str
    annotation: Optional[str]


@dataclass(frozen=True)
class CreateTrailResult:
    """Result of :meth:`TrailService.create_trail`.

    Carries every identity the HTTP layer (task 10.3) needs to render
    the response and every identity tests need to assert on the
    persisted rows.

    Attributes:
        trail_id: The Trail Resource Identity (distinct from every
            referenced endpoint Identity per Requirement 9.1).
        trail_revision_id: The Trail Revision Identity. Distinct from
            ``trail_id`` per AD-WS-3.
        purpose: The persisted purpose text.
        audience_id: The persisted audience identifier.
        ordering_rationale: The persisted ordering rationale (``None``
            when none was supplied).
        steps: The five :class:`TrailStepResult` entries in ordinal
            order.
        manifest_id: The Provenance Manifest Identity when a manifest
            writer was wired; ``None`` otherwise.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the Trail Revision, every Trail Step, the
            Provenance Manifest, and the consequential audit row.
    """

    trail_id: str
    trail_revision_id: str
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str]
    steps: tuple[TrailStepResult, ...]
    manifest_id: Optional[str]
    recorded_at: str


@dataclass(frozen=True)
class AppendTrailRevisionResult:
    """Result of :meth:`TrailService.append_revision`.

    Identical in shape to :class:`CreateTrailResult` except that:

    - ``predecessor_revision_id`` is the Trail Revision Identity of
      the prior Revision (always populated — :meth:`append_revision`
      requires the Trail to already exist).
    - ``created_new_revision`` is ``True`` when the new submission
      differs in canonical form from the prior Revision (so a new
      immutable Trail Revision was inserted with
      ``predecessor_revision_id`` set per Requirement 9.4), and
      ``False`` when the canonical form is byte-equivalent to the
      prior Revision (no new Revision was created and the existing
      Revision is returned verbatim).

    When ``created_new_revision`` is ``False``, every identifier field
    (``trail_revision_id``, every ``trail_step_id``) names the
    *existing* prior Revision's rows; the caller can treat the response
    as a no-op acknowledgement that nothing material changed.

    When ``created_new_revision`` is ``True``, the fields name the
    *new* Revision's rows and ``manifest_id`` names a new manifest
    (when a manifest writer is wired). The prior Revision is preserved
    unchanged per Requirement 9.4 and Principle 5.6.

    Attributes:
        trail_id: The Trail Resource Identity (unchanged across
            Revisions per AD-WS-3).
        trail_revision_id: Either the new Trail Revision Identity
            (``created_new_revision=True``) or the prior one
            (``created_new_revision=False``).
        predecessor_revision_id: The prior Trail Revision Identity.
            Always populated because :meth:`append_revision` requires
            the Trail to already exist with at least one Revision.
        purpose: The persisted purpose text on the returned Revision.
        audience_id: The persisted audience identifier.
        ordering_rationale: The persisted ordering rationale, or
            ``None``.
        steps: The five :class:`TrailStepResult` entries in ordinal
            order, naming the returned Revision's steps.
        manifest_id: The newly written Provenance Manifest Identity
            when a manifest writer was wired and a new Revision was
            created; ``None`` otherwise (including when the
            submission matched the prior Revision — no new manifest
            is written for a no-op).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp.
            For ``created_new_revision=True``, this is the new
            Revision's recorded time. For ``created_new_revision=False``,
            this is the *prior* Revision's recorded time so callers
            see the same timestamp they would see from a follow-up
            GET (Requirement 9.4 — "preserve the prior Trail
            Revision unchanged").
        created_new_revision: ``True`` iff a new immutable Trail
            Revision was inserted.
    """

    trail_id: str
    trail_revision_id: str
    predecessor_revision_id: str
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str]
    steps: tuple[TrailStepResult, ...]
    manifest_id: Optional[str]
    recorded_at: str
    created_new_revision: bool


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join an originating-write audit row to any
    downstream audit row produced for the same logical operation. They
    are not registered with :class:`IdentityService` because they do
    not name a domain Resource. Mirrors the helper of the same name in
    :mod:`walking_slice.knowledge`.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to a Trail's identifiers
    in ``Identifier_Registry``. Hashing the canonical-form Trail
    payload keeps the digest a stable function of the Trail's natural
    content.
    """
    return hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Projection-envelope source collection (task 14.2).
#
# These helpers walk a Trail submission and collect the unique source
# Resource and Revision Identities the Trail consults. The lists feed the
# :class:`~walking_slice.projection.ProjectionEnvelope` so a consumer of a
# wrapped status response can identify exactly which source Records the
# projection consulted (Requirement 14.1).
#
# Identifiers are converted from the slice's string form to
# :class:`~uuid.UUID` because the envelope's type contract is
# ``tuple[UUID, ...]``. Strings that fail to parse — for example, when a
# producer supplies non-canonical fake input during a unit test — are
# skipped silently because the envelope only documents *which* source
# revisions were consulted, not which ones failed to resolve (that is the
# "Trail unresolved" status payload's job).
# ---------------------------------------------------------------------------


def _coerce_uuid(value: Optional[str]) -> Optional[UUID]:
    """Parse a UUID string; return ``None`` if the value is missing/invalid.

    The envelope's strict-UUID contract is enforced by
    :class:`ProjectionEnvelope`'s validators. Here we only need to
    decide whether a step's target identifier should appear in the
    envelope's source list — silent skipping is acceptable because the
    accompanying status payload (``trail.unresolved``) already carries
    the per-ordinal detail when a target failed to resolve.
    """
    if value is None or value == "":
        return None
    try:
        return UUID(value)
    except (ValueError, AttributeError):  # pragma: no cover - defensive
        return None


def _collect_source_resource_ids(
    steps: Sequence[TrailStepInput],
) -> tuple[UUID, ...]:
    """Return the unique Resource Identities a Trail submission consults.

    The Resource Identity is the ``target_id`` field of every step — for
    ordinal 2 (region_occurrence) this is the owning Document Revision
    Identity (which is itself a Revision Identity, not a Resource one);
    we deliberately keep it in the Resource-ID list so the envelope
    documents that the projection consulted that Document Revision when
    deciding the region's resolvability. The ordering is the ordinal
    order of the steps so the envelope is stable across submissions
    that supplied the same steps in a different field order.
    """
    seen: list[UUID] = []
    seen_set: set[UUID] = set()
    for step in steps:
        uid = _coerce_uuid(step.target_id)
        if uid is not None and uid not in seen_set:
            seen.append(uid)
            seen_set.add(uid)
    return tuple(seen)


def _collect_source_revision_ids(
    steps: Sequence[TrailStepInput],
) -> tuple[UUID, ...]:
    """Return the unique Revision Identities a Trail submission consults.

    The Revision Identity is the ``target_revision_id`` field of steps
    1, 3, and 4 (Document Revision, Finding Revision, Recommendation
    Revision). Ordinals 2 and 5 do not carry a ``target_revision_id``
    — ordinal 2's "revision" is captured by the ``region_id`` +
    ``target_id`` (Document Revision) pair already in the Resource list,
    and ordinal 5 (Decision) is an Immutable Record without a separate
    Revision Identity (AD-WS-3 / AD-WS-4).
    """
    seen: list[UUID] = []
    seen_set: set[UUID] = set()
    for step in steps:
        uid = _coerce_uuid(step.target_revision_id)
        if uid is not None and uid not in seen_set:
            seen.append(uid)
            seen_set.add(uid)
    return tuple(seen)


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass
class TrailService:
    """Persist Trails, Trail Revisions, and Trail Steps for the slice.

    Like :class:`~walking_slice.knowledge.KnowledgeService`, the service
    is connection-scoped at call time: :meth:`create_trail` accepts the
    caller's :class:`~sqlalchemy.engine.Connection` and writes inside
    the caller's transaction (AD-WS-5). Instances therefore hold only
    the cross-request collaborators (:class:`Clock`,
    :class:`IdentityService`, :class:`AuditLog`, optional
    :class:`AuthorizationService`, optional
    :class:`ProvenanceManifestWriter`) and can be shared across
    requests.

    Args:
        clock: Source of the recorded timestamp shared by the
            Trails / Trail_Revisions / Trail_Steps / manifest / audit
            rows. Consulted exactly once per write so every artifact
            of the transaction shares one timestamp
            (design §"Cross-Cutting Concerns" — *Transactionality*).
        identity_service: Generates Trail, Trail Revision, and Trail
            Step identifiers and persists Trail / Trail-Revision
            bindings to ``Identifier_Registry``. Trail Step
            identifiers are minted but the registry binding is
            shared with the Trail Revision digest because a Trail
            Step's natural content is the (target, annotation,
            selection_mode) triple which is fully captured by the
            canonical Trail Revision payload.
        audit_log: Appends the ``'consequential'`` audit row inside
            the caller's transaction. Failures propagate as
            :class:`walking_slice.audit.AuditAppendError`; the caller's
            transaction context manager rolls back automatically.
        authorization_service: Optional
            :class:`~walking_slice.authorization.AuthorizationService`
            used to enforce ``create.trail`` authority. When ``None``
            the authority check is skipped — convenient for unit
            tests and for callers that have already evaluated
            authority. **When wired,** :meth:`create_trail`
            additionally requires the caller to pass an ``engine``
            argument so the Denial Record for a denied attempt can
            be written in a separate transaction that survives the
            caller's rollback (Requirement 7.6).
        manifest_writer: Optional
            :class:`~walking_slice.manifests.ProvenanceManifestWriter`
            used to record the Trail Revision's Provenance Manifest
            inside the caller's transaction (Requirement 10.1). When
            ``None`` the manifest write is skipped — convenient for
            tests that exercise persistence in isolation. Production
            composition (task 15.2) always wires a writer.
        denial_audit_sleep: Sleep function used by
            :meth:`_persist_denial` to pause between retries of the
            Denial Record append. Defaults to :func:`time.sleep`;
            tests inject a recording stub for deterministic timing.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: Optional[AuthorizationService] = None
    manifest_writer: Optional[ProvenanceManifestWriter] = None
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_trail(
        self,
        connection: Connection,
        *,
        purpose: str,
        audience_id: str,
        steps: Sequence[TrailStepInput],
        authoring_party_id: str,
        ordering_rationale: Optional[str] = None,
        scope: Optional[str] = None,
        engine: Optional[Engine] = None,
        evaluation_at: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> CreateTrailResult:
        """Create a Trail Resource plus its initial Trail Revision and steps.

        See module docstring for the full validation / resolvability /
        persistence flow. The high-level order is:

        1. Structural validation (Requirement 9.1, 9.2, 9.3, 9.6, 9.7
           and AD-WS-12). Runs before any database round-trip; raises
           :class:`TrailValidationError` on any violation.
        2. Target resolvability (Requirement 9.5). Each step's target
           is verified against the appropriate table; unresolved
           steps are collected and surfaced as a single
           :class:`TrailTargetUnresolvedError`.
        3. Optional authorization check (when
           :attr:`authorization_service` is wired). On deny, a
           Denial Record is persisted in a separate transaction
           and :class:`TrailAuthorizationError` is raised; on total
           audit failure :class:`TrailAuditFailureError` is raised.
        4. Identifier minting + ``Identifier_Registry`` binding
           inside the caller's transaction (AD-WS-2, AD-WS-5).
        5. INSERTs: ``Trails`` → ``Trail_Revisions`` → 5×
           ``Trail_Steps``. The ``Trails.current_revision_id``
           pointer is then updated to point at the new Revision (a
           mutable convenience pointer per the schema comment;
           Principle 5.23).
        6. Optional Provenance Manifest write (when
           :attr:`manifest_writer` is wired). The five step targets
           are recorded as Included Sources per Requirement 10.1.
        7. ``Audit_Records`` row with
           ``action_type='create.trail'`` and
           ``outcome='consequential'`` (Requirement 13.1).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction.
            purpose: Trail purpose text, 1..500 characters
                (Requirement 9.6).
            audience_id: Audience identifier (non-empty, Requirement
                9.6).
            steps: Exactly five :class:`TrailStepInput` entries
                (Requirement 9.1 / 9.7). The order need not be
                pre-sorted by ``ordinal`` — structural validation
                sorts the entries before checking; the returned
                :class:`CreateTrailResult.steps` is always in ordinal
                order.
            authoring_party_id: Identity of the recording Party.
                Persisted on ``Trail_Revisions.authoring_party_id``
                and the consequential audit row's
                ``actor_party_id``. Must reference an existing
                ``Parties`` row; the FK is enforced by the database.
            ordering_rationale: Optional 0..500-character rationale
                (Requirement 9.6).
            scope: Scope identifier passed to
                :meth:`AuthorizationService.evaluate` as
                ``target.scope``. Ignored when
                :attr:`authorization_service` is ``None``. When
                wired, an unwired scope (``None``) is evaluated
                against the wildcard ``"*"`` rule in
                :meth:`AuthorizationService._scope_covers`.
            engine: Required when :attr:`authorization_service` is
                wired. The Denial Record for a denied attempt is
                written in a fresh transaction on this engine so
                the row survives the caller's rollback (Requirement
                7.6). Ignored when :attr:`authorization_service` is
                ``None``.
            evaluation_at: Optional explicit effective time passed
                to :meth:`AuthorizationService.evaluate` as the
                ``at`` parameter. Defaults to the same instant as
                the recorded time of this transaction. Ignored
                when :attr:`authorization_service` is ``None``.
            correlation_id: Optional correlation identifier. A
                UUIDv7 is generated when omitted.

        Returns:
            :class:`CreateTrailResult` carrying the Trail identifiers,
            the persisted attributes, every Trail Step Identity (in
            ordinal order), the optional manifest Identity, and the
            recorded time.

        Raises:
            TrailValidationError: A structural rule was violated.
            TrailTargetUnresolvedError: At least one step's target
                did not resolve.
            TrailAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt;
                the Denial Record was persisted in a separate
                transaction (Requirement 7.6).
            TrailAuditFailureError: The denial was issued and the
                separate-transaction Denial Record append failed on
                every retry (Requirement 7.6).
            ValueError: :attr:`authorization_service` is wired but
                ``engine`` was not supplied.
            walking_slice.audit.AuditAppendError: Consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST
                be allowed to roll back.
            walking_slice.identity.IdentityConflictError: A freshly
                generated Trail identifier collides with an
                existing ``Identifier_Registry`` binding (vanishingly
                rare for UUIDv7 within a single instance).
        """
        # Fail-fast configuration check (Requirement 7.6). When
        # authorization is wired, the deny path needs an Engine to
        # open a SEPARATE transaction for the Denial Record so the
        # row survives the caller's rollback.
        if self.authorization_service is not None and engine is None:
            raise ValueError(
                "engine is required when authorization_service is wired "
                "on TrailService.create_trail: the Denial Record for a "
                "denied attempt must be written in a separate transaction "
                "so it survives the caller's rollback (Requirement 7.6)."
            )

        # 1. Structural validation. All Python-side checks run before
        # any database access so a malformed request is rejected
        # before we touch the database, identity service, or
        # authorization service.
        self._validate_required_strings(
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            authoring_party_id=authoring_party_id,
        )
        steps_in_order = self._validate_steps(tuple(steps))

        # 2. Target resolvability. Each step's target is checked
        # against the appropriate table BEFORE any INSERT; unresolved
        # steps are collected into a single error per Requirement 9.5.
        unresolved = self._resolve_targets(connection, steps_in_order)
        if unresolved:
            raise TrailTargetUnresolvedError(unresolved_steps=unresolved)

        # 3. Authorization check (optional). The check runs after the
        # structural and resolvability checks so a malformed or
        # unresolvable request is rejected with a structured 400
        # rather than a 403 — Requirement 7.4's indistinguishable
        # denial shape is only invoked when the request would
        # *otherwise* have been writable.
        correlation = correlation_id or _new_correlation_id()
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        if self.authorization_service is not None:
            assert engine is not None  # narrowed by the fail-fast check
            evaluate_at = (
                evaluation_at if evaluation_at is not None else recorded_time
            )
            # Run the authority evaluation on a SEPARATE transaction
            # so SQLite's single-writer constraint cannot deadlock a
            # subsequent Denial-Record write — same reasoning as
            # :meth:`KnowledgeService.create_decision`.
            with engine.begin() as eval_conn:
                decision = self.authorization_service.evaluate(
                    eval_conn,
                    party_id=authoring_party_id,
                    action=_AUTHORIZATION_ACTION_CREATE_TRAIL,
                    target=TargetRef(
                        kind="trail",
                        scope=scope,
                    ),
                    at=evaluate_at,
                    correlation_id=correlation,
                )
            if decision.is_deny:
                reason_code = decision.reason_code or "no-role-assignment"
                self._persist_denial(
                    engine=engine,
                    actor_party_id=authoring_party_id,
                    reason_code=reason_code,
                    correlation_id=decision.correlation_id,
                    recorded_time=evaluate_at,
                )
                raise TrailAuthorizationError(
                    reason_code=reason_code,
                    correlation_id=decision.correlation_id,
                )

        # 4. Mint identifiers and bind them to the canonical Trail
        # payload digest. The Trail Resource and the Trail Revision
        # share one digest, mirroring the Resource / first-Revision
        # pattern in :mod:`walking_slice.knowledge`.
        trail_id = str(self.identity_service.new_trail_id())
        trail_revision_id = str(self.identity_service.new_trail_revision_id())
        step_ids = tuple(
            str(self.identity_service.new_trail_step_id())
            for _ in range(_REQUIRED_STEP_COUNT)
        )

        canonical_payload = self._canonical_payload(
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            steps=steps_in_order,
        )
        trail_digest = _sha256_hex(canonical_payload.encode("utf-8"))
        self.identity_service.reject_if_duplicate(
            trail_id,
            trail_digest,
            connection=connection,
            kind="trail",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_TRAIL,
            recorded_time=recorded_time,
        )
        self.identity_service.reject_if_duplicate(
            trail_revision_id,
            trail_digest,
            connection=connection,
            kind="trail_revision",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_TRAIL,
            recorded_time=recorded_time,
        )
        # Trail Step identifiers share the parent Trail Revision's
        # digest because a Step's natural content is fully captured
        # by the Trail Revision's canonical payload (the same
        # collapsed-binding pattern :meth:`KnowledgeService.create_decision`
        # uses for Decision identifiers).
        for step_id in step_ids:
            self.identity_service.reject_if_duplicate(
                step_id,
                trail_digest,
                connection=connection,
                kind="trail_step",
                actor_party_id=authoring_party_id,
                correlation_id=correlation,
                attempted_action=_AUDIT_ACTION_CREATE_TRAIL,
                recorded_time=recorded_time,
            )

        # 5. INSERT the Trails header, the immutable Trail_Revisions
        # row, and the five immutable Trail_Steps rows. The
        # ``Trails.current_revision_id`` pointer is left NULL on the
        # initial insert and then updated below — the schema permits
        # this mutable convenience field (Principle 5.23 — a
        # projection, not authoritative; the immutable Revisions are
        # the source of truth).
        connection.execute(
            text(
                """
                INSERT INTO Trails (trail_id, created_at, current_revision_id)
                VALUES (:trail_id, :created_at, NULL)
                """
            ),
            {"trail_id": trail_id, "created_at": recorded_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO Trail_Revisions (
                    trail_revision_id, trail_id, predecessor_revision_id,
                    purpose, audience_id, ordering_rationale,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :trev, :tid, NULL,
                    :purpose, :audience_id, :ordering_rationale,
                    :authoring_party_id, :recorded_at
                )
                """
            ),
            {
                "trev": trail_revision_id,
                "tid": trail_id,
                "purpose": purpose,
                "audience_id": audience_id,
                "ordering_rationale": ordering_rationale,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )
        for step_id, step in zip(step_ids, steps_in_order):
            connection.execute(
                text(
                    """
                    INSERT INTO Trail_Steps (
                        trail_step_id, trail_revision_id, ordinal,
                        selection_mode, target_kind, target_id,
                        target_revision_id, region_id, annotation
                    ) VALUES (
                        :tsid, :trev, :ordinal,
                        :selection_mode, :target_kind, :target_id,
                        :target_revision_id, :region_id, :annotation
                    )
                    """
                ),
                {
                    "tsid": step_id,
                    "trev": trail_revision_id,
                    "ordinal": step.ordinal,
                    "selection_mode": step.selection_mode,
                    "target_kind": step.target_kind,
                    "target_id": step.target_id,
                    "target_revision_id": step.target_revision_id,
                    "region_id": step.region_id,
                    "annotation": step.annotation,
                },
            )
        # Point Trails.current_revision_id at the new Trail Revision.
        # This is a one-time UPDATE on the Trails header, which is a
        # mutable convenience field — not on the immutable
        # Trail_Revisions rows (those are protected by the AD-WS-4
        # trigger installed in :mod:`walking_slice.persistence`).
        connection.execute(
            text(
                """
                UPDATE Trails
                   SET current_revision_id = :trev
                 WHERE trail_id = :tid
                """
            ),
            {"trev": trail_revision_id, "tid": trail_id},
        )

        # 6. Provenance Manifest (optional). The five step targets are
        # the manifest's Included Sources per Requirement 10.1; the
        # manifest write participates in the caller's transaction so
        # a failure rolls every preceding INSERT back (Requirements
        # 2.7, 10.6, 13.6 / AD-WS-5).
        manifest_id: Optional[str] = None
        if self.manifest_writer is not None:
            included_sources = tuple(
                IncludedSource(
                    kind=step.target_kind,  # type: ignore[arg-type]
                    resource_id=step.target_id,
                    revision_id=step.target_revision_id,
                    recorded_at=recorded_time,
                )
                for step in steps_in_order
            )
            manifest_result = self.manifest_writer.write_manifest(
                connection,
                subject_kind=_MANIFEST_SUBJECT_KIND_TRAIL,
                subject_id=trail_id,
                subject_revision_id=trail_revision_id,
                authoring_party_id=authoring_party_id,
                included_sources=included_sources,
                recorded_at=recorded_time,
            )
            manifest_id = manifest_result.manifest_id

        # 7. Consequential audit row. ``target_id`` is the Trail
        # Resource Identity; ``target_revision_id`` is the Trail
        # Revision Identity (Requirement 13.1 — every consequential
        # row records actor, action, target, target revision,
        # outcome, recorded time, correlation).
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_AUDIT_ACTION_CREATE_TRAIL,
            target_id=trail_id,
            target_revision_id=trail_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateTrailResult(
            trail_id=trail_id,
            trail_revision_id=trail_revision_id,
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            steps=tuple(
                TrailStepResult(
                    trail_step_id=step_id,
                    ordinal=step.ordinal,
                    target_kind=step.target_kind,
                    target_id=step.target_id,
                    target_revision_id=step.target_revision_id,
                    region_id=step.region_id,
                    selection_mode=step.selection_mode,
                    annotation=step.annotation,
                )
                for step_id, step in zip(step_ids, steps_in_order)
            ),
            manifest_id=manifest_id,
            recorded_at=recorded_at,
        )

    def append_revision(
        self,
        connection: Connection,
        *,
        trail_id: str,
        purpose: str,
        audience_id: str,
        steps: Sequence[TrailStepInput],
        authoring_party_id: str,
        ordering_rationale: Optional[str] = None,
        scope: Optional[str] = None,
        engine: Optional[Engine] = None,
        evaluation_at: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> AppendTrailRevisionResult:
        """Append a Trail Revision when the submission differs materially from the prior one.

        Material-change detection per Requirement 9.4 and design
        §"Trail_Service — Material-change detection". The submission's
        canonical form (purpose, audience_id, ordering_rationale, and
        the ordered list of ``(ordinal, target_ref, annotation)`` step
        tuples) is compared byte-equivalent against the canonical form
        of the prior Trail Revision. On a difference a new immutable
        Trail Revision is inserted with ``predecessor_revision_id``
        pointing at the prior Revision; on byte equivalence the
        existing Revision is returned and no new row is inserted.

        Flow:

        1. Validate required strings and the five-step structural
           rules in the same way :meth:`create_trail` does
           (Requirements 9.1, 9.2, 9.3, 9.6, 9.7 / AD-WS-12). A
           malformed submission is rejected before any database
           round-trip so no Trail Revision, Trail Step, manifest, or
           audit row is created.
        2. Resolve every step's target (Requirement 9.5). Unresolved
           targets surface as a single
           :class:`TrailTargetUnresolvedError` listing each
           unresolved step by ordinal.
        3. Load the Trail's current Revision (and its steps). When
           the Trail does not exist :class:`TrailNotFoundError` is
           raised before any further work; the trail must already
           exist with a prior Revision because Requirement 9.4 is
           framed as an update to an existing Trail.
        4. Build the canonical-form payload of the new submission and
           the canonical-form payload of the prior Revision via
           :meth:`_canonical_payload`. The same helper is used for
           both, so a no-op submission produces a byte-equivalent
           string by construction.
        5. If the two canonical payloads are byte-equivalent, return
           an :class:`AppendTrailRevisionResult` whose
           ``created_new_revision=False`` field tells the caller
           nothing was written. No INSERT, no UPDATE, no manifest
           write, and no audit row run on the connection (Requirement
           9.4 — "preserve the prior Trail Revision unchanged"; AD-WS-4).
        6. Otherwise, run the optional authorization check (using a
           SEPARATE transaction on ``engine`` the same way
           :meth:`create_trail` does), mint a new Trail Revision and
           five Trail Step identifiers, INSERT the new
           ``Trail_Revisions`` row with ``predecessor_revision_id``
           set to the prior Revision Identity, INSERT five
           ``Trail_Steps`` rows, UPDATE ``Trails.current_revision_id``
           to point at the new Revision, write an optional Provenance
           Manifest, and append the consequential audit row.

        Note that step 6 mirrors :meth:`create_trail` for everything
        except: (a) the Trail Resource Identity is not minted (the
        Trail already exists), (b) ``predecessor_revision_id`` is
        populated, and (c) no ``Trails`` INSERT runs.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. When no material change is detected the
                method makes no writes on this connection.
            trail_id: The Trail Resource Identity to append a
                Revision to. Must reference an existing ``Trails``
                row (:class:`TrailNotFoundError` is raised otherwise).
            purpose: Trail purpose text for the candidate Revision
                (1..500 characters, Requirement 9.6).
            audience_id: Audience identifier (non-empty,
                Requirement 9.6).
            steps: Exactly five :class:`TrailStepInput` entries.
                Need not be pre-sorted by ordinal.
            authoring_party_id: Identity of the recording Party.
                Persisted on the new
                ``Trail_Revisions.authoring_party_id`` (when a new
                Revision is created) and the consequential audit
                row's ``actor_party_id``. Must reference an
                existing ``Parties`` row.
            ordering_rationale: Optional 0..500-character rationale
                (Requirement 9.6).
            scope: Scope identifier passed to
                :meth:`AuthorizationService.evaluate`. See
                :meth:`create_trail`.
            engine: Required when :attr:`authorization_service` is
                wired AND a new Revision will be created. See
                :meth:`create_trail`. May be ``None`` when no
                material change is detected (the deny path is
                never reached because no consequential write
                occurs).
            evaluation_at: Optional explicit effective time.
            correlation_id: Optional correlation identifier.

        Returns:
            :class:`AppendTrailRevisionResult` whose
            ``created_new_revision`` field tells the caller whether
            a new Revision was inserted.

        Raises:
            TrailNotFoundError: ``trail_id`` does not name an
                existing Trail.
            TrailValidationError: Structural rule violation.
            TrailTargetUnresolvedError: At least one step's target
                did not resolve.
            TrailAuthorizationError: A material change was detected
                and :class:`AuthorizationService` denied it.
            TrailAuditFailureError: Same as :meth:`create_trail`.
            ValueError: :attr:`authorization_service` is wired but
                ``engine`` was not supplied AND a material change
                was detected (no-op submissions do not require
                ``engine``).
            walking_slice.audit.AuditAppendError: Consequential
                audit append failed.
            walking_slice.identity.IdentityConflictError: Identifier
                collision on a freshly minted Trail Revision or
                Trail Step Identity.
        """
        # 1. Structural validation. Runs before any database access.
        self._validate_required_strings(
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            authoring_party_id=authoring_party_id,
        )
        steps_in_order = self._validate_steps(tuple(steps))

        # 2. Target resolvability (Requirement 9.5).
        unresolved = self._resolve_targets(connection, steps_in_order)
        if unresolved:
            raise TrailTargetUnresolvedError(unresolved_steps=unresolved)

        # 3. Load the prior Revision. The Trail must already exist
        # because Requirement 9.4 frames this method as an update.
        prior = self._load_current_revision(connection, trail_id)
        if prior is None:
            raise TrailNotFoundError(trail_id=trail_id)
        prior_revision_id, prior_purpose, prior_audience_id, \
            prior_ordering_rationale, prior_recorded_at, prior_step_inputs, \
            prior_step_ids = prior

        # 4. Compare canonical forms. Using the SAME helper for both
        # payloads guarantees byte-equivalent canonical strings for
        # byte-equivalent inputs (the helper's ``json.dumps`` with
        # ``sort_keys=True`` is deterministic).
        new_canonical = self._canonical_payload(
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            steps=steps_in_order,
        )
        prior_canonical = self._canonical_payload(
            purpose=prior_purpose,
            audience_id=prior_audience_id,
            ordering_rationale=prior_ordering_rationale,
            steps=prior_step_inputs,
        )

        if new_canonical == prior_canonical:
            # 5. No material change — return the prior Revision
            # unchanged. No INSERT, UPDATE, manifest, or audit row
            # is written; AD-WS-4 (Trail_Revisions immutability) is
            # preserved because the prior row is not touched.
            return AppendTrailRevisionResult(
                trail_id=trail_id,
                trail_revision_id=prior_revision_id,
                predecessor_revision_id=prior_revision_id,
                purpose=prior_purpose,
                audience_id=prior_audience_id,
                ordering_rationale=prior_ordering_rationale,
                steps=tuple(
                    TrailStepResult(
                        trail_step_id=step_id,
                        ordinal=step.ordinal,
                        target_kind=step.target_kind,
                        target_id=step.target_id,
                        target_revision_id=step.target_revision_id,
                        region_id=step.region_id,
                        selection_mode=step.selection_mode,
                        annotation=step.annotation,
                    )
                    for step_id, step in zip(prior_step_ids, prior_step_inputs)
                ),
                manifest_id=None,
                recorded_at=prior_recorded_at,
                created_new_revision=False,
            )

        # 6. Material change detected — create a new immutable Trail
        # Revision linked to the prior one via
        # ``predecessor_revision_id`` (Requirement 9.4).

        # Fail-fast configuration check for the deny path
        # (Requirement 7.6). Only required when a new Revision will
        # actually be written.
        if self.authorization_service is not None and engine is None:
            raise ValueError(
                "engine is required when authorization_service is wired "
                "on TrailService.append_revision and a material change is "
                "detected: the Denial Record for a denied attempt must be "
                "written in a separate transaction so it survives the "
                "caller's rollback (Requirement 7.6)."
            )

        correlation = correlation_id or _new_correlation_id()
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)

        # Authorization check (optional). Mirrors :meth:`create_trail`.
        if self.authorization_service is not None:
            assert engine is not None  # narrowed by the fail-fast check
            evaluate_at = (
                evaluation_at if evaluation_at is not None else recorded_time
            )
            with engine.begin() as eval_conn:
                decision = self.authorization_service.evaluate(
                    eval_conn,
                    party_id=authoring_party_id,
                    action=_AUTHORIZATION_ACTION_CREATE_TRAIL,
                    target=TargetRef(
                        kind="trail",
                        scope=scope,
                    ),
                    at=evaluate_at,
                    correlation_id=correlation,
                )
            if decision.is_deny:
                reason_code = decision.reason_code or "no-role-assignment"
                self._persist_denial(
                    engine=engine,
                    actor_party_id=authoring_party_id,
                    reason_code=reason_code,
                    correlation_id=decision.correlation_id,
                    recorded_time=evaluate_at,
                )
                raise TrailAuthorizationError(
                    reason_code=reason_code,
                    correlation_id=decision.correlation_id,
                )

        # Mint new identifiers. The Trail Resource Identity is
        # *not* minted — the Trail already exists.
        trail_revision_id = str(self.identity_service.new_trail_revision_id())
        step_ids = tuple(
            str(self.identity_service.new_trail_step_id())
            for _ in range(_REQUIRED_STEP_COUNT)
        )

        # Bind the new identifiers to the canonical digest.
        trail_digest = _sha256_hex(new_canonical.encode("utf-8"))
        self.identity_service.reject_if_duplicate(
            trail_revision_id,
            trail_digest,
            connection=connection,
            kind="trail_revision",
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_AUDIT_ACTION_CREATE_TRAIL,
            recorded_time=recorded_time,
        )
        for step_id in step_ids:
            self.identity_service.reject_if_duplicate(
                step_id,
                trail_digest,
                connection=connection,
                kind="trail_step",
                actor_party_id=authoring_party_id,
                correlation_id=correlation,
                attempted_action=_AUDIT_ACTION_CREATE_TRAIL,
                recorded_time=recorded_time,
            )

        # INSERT the new Trail Revision with predecessor link.
        connection.execute(
            text(
                """
                INSERT INTO Trail_Revisions (
                    trail_revision_id, trail_id, predecessor_revision_id,
                    purpose, audience_id, ordering_rationale,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :trev, :tid, :prev,
                    :purpose, :audience_id, :ordering_rationale,
                    :authoring_party_id, :recorded_at
                )
                """
            ),
            {
                "trev": trail_revision_id,
                "tid": trail_id,
                "prev": prior_revision_id,
                "purpose": purpose,
                "audience_id": audience_id,
                "ordering_rationale": ordering_rationale,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )
        for step_id, step in zip(step_ids, steps_in_order):
            connection.execute(
                text(
                    """
                    INSERT INTO Trail_Steps (
                        trail_step_id, trail_revision_id, ordinal,
                        selection_mode, target_kind, target_id,
                        target_revision_id, region_id, annotation
                    ) VALUES (
                        :tsid, :trev, :ordinal,
                        :selection_mode, :target_kind, :target_id,
                        :target_revision_id, :region_id, :annotation
                    )
                    """
                ),
                {
                    "tsid": step_id,
                    "trev": trail_revision_id,
                    "ordinal": step.ordinal,
                    "selection_mode": step.selection_mode,
                    "target_kind": step.target_kind,
                    "target_id": step.target_id,
                    "target_revision_id": step.target_revision_id,
                    "region_id": step.region_id,
                    "annotation": step.annotation,
                },
            )

        # Update the mutable convenience pointer on the Trails header
        # to the new Revision Identity. The prior Trail_Revisions row
        # is left byte-equivalent (Requirement 9.4 — "preserve the
        # prior Trail Revision unchanged" / AD-WS-4).
        connection.execute(
            text(
                """
                UPDATE Trails
                   SET current_revision_id = :trev
                 WHERE trail_id = :tid
                """
            ),
            {"trev": trail_revision_id, "tid": trail_id},
        )

        # Optional Provenance Manifest for the new Revision.
        manifest_id: Optional[str] = None
        if self.manifest_writer is not None:
            included_sources = tuple(
                IncludedSource(
                    kind=step.target_kind,  # type: ignore[arg-type]
                    resource_id=step.target_id,
                    revision_id=step.target_revision_id,
                    recorded_at=recorded_time,
                )
                for step in steps_in_order
            )
            manifest_result = self.manifest_writer.write_manifest(
                connection,
                subject_kind=_MANIFEST_SUBJECT_KIND_TRAIL,
                subject_id=trail_id,
                subject_revision_id=trail_revision_id,
                authoring_party_id=authoring_party_id,
                included_sources=included_sources,
                recorded_at=recorded_time,
            )
            manifest_id = manifest_result.manifest_id

        # Consequential audit row for the new Revision.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_AUDIT_ACTION_CREATE_TRAIL,
            target_id=trail_id,
            target_revision_id=trail_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return AppendTrailRevisionResult(
            trail_id=trail_id,
            trail_revision_id=trail_revision_id,
            predecessor_revision_id=prior_revision_id,
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            steps=tuple(
                TrailStepResult(
                    trail_step_id=step_id,
                    ordinal=step.ordinal,
                    target_kind=step.target_kind,
                    target_id=step.target_id,
                    target_revision_id=step.target_revision_id,
                    region_id=step.region_id,
                    selection_mode=step.selection_mode,
                    annotation=step.annotation,
                )
                for step_id, step in zip(step_ids, steps_in_order)
            ),
            manifest_id=manifest_id,
            recorded_at=recorded_at,
            created_new_revision=True,
        )

    # -- projection-envelope wrapping (task 14.2) -------------------------

    def create_trail_projected(
        self,
        connection: Connection,
        *,
        status_projector: StatusProjector,
        purpose: str,
        audience_id: str,
        steps: Sequence[TrailStepInput],
        authoring_party_id: str,
        ordering_rationale: Optional[str] = None,
        scope: Optional[str] = None,
        engine: Optional[Engine] = None,
        evaluation_at: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> StatusBearingResponse:
        """Create a Trail and return its status wrapped with a
        :class:`~walking_slice.projection.ProjectionEnvelope`.

        Task 14.2 calls for status-bearing responses from
        ``Trail_Service`` (today the "Trail resolved" / "Trail
        unresolved" pair from :meth:`create_trail`) to be wrapped with
        a Projection Envelope (Requirements 14.1, 14.2) and for the
        wrapping to be additive — the existing :meth:`create_trail`
        surface continues to raise the typed errors the HTTP layer
        (task 10.3) and tests already depend on. This method is the
        additive entry point.

        Behavior:

        - Happy path (every Trail Step target resolves): calls
          :meth:`create_trail` and returns a
          :class:`~walking_slice.projection.ProjectedStatusResponse`
          carrying ``status="trail.resolved"`` and the trail / revision
          identifiers in :attr:`details` together with the envelope.
        - "Trail unresolved" path: catches the
          :class:`TrailTargetUnresolvedError` produced by
          :meth:`create_trail` BEFORE any partial write occurs (per
          Requirement 9.5 the resolvability check runs before any
          INSERT) and returns a
          :class:`~walking_slice.projection.ProjectedStatusResponse`
          carrying ``status="trail.unresolved"`` and a per-ordinal
          unresolved-step list in :attr:`details`. Source Records
          remain unchanged (Requirement 14.3) because the underlying
          resolvability check is read-only.
        - Explanation-unavailable path: when the Projection Definition
          name is not registered with the supplied
          :class:`~walking_slice.projection.StatusProjector` the
          projector returns an
          :class:`~walking_slice.projection.ExplanationUnavailableResponse`
          and this method short-circuits without calling
          :meth:`create_trail` so no source Record is touched
          (Requirement 14.4).

        Structural-validation errors (``TrailValidationError``) and
        authorization denials (``TrailAuthorizationError`` /
        ``TrailAuditFailureError``) are NOT wrapped — they are not
        Requirement-14 projected statuses, they are the per-action
        denial responses defined by Requirements 9.7 and 7.4. The
        HTTP layer (task 10.3) renders them with their own response
        shape. Wrapping them would conflate Authorization denials
        with Projection withholdings, which Requirement 14.4 calls
        out as separate observables.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. Forwarded to :meth:`create_trail`.
            status_projector: The
                :class:`~walking_slice.projection.StatusProjector`
                that resolves the Trail Projection Definition and
                stamps the envelope. Production composition (task
                15.2) constructs one and wires it through
                ``RequestContext``.
            purpose: Forwarded to :meth:`create_trail`.
            audience_id: Forwarded to :meth:`create_trail`.
            steps: Forwarded to :meth:`create_trail`.
            authoring_party_id: Forwarded to :meth:`create_trail`.
            ordering_rationale: Forwarded to :meth:`create_trail`.
            scope: Forwarded to :meth:`create_trail`.
            engine: Forwarded to :meth:`create_trail`.
            evaluation_at: Forwarded to :meth:`create_trail`.
            correlation_id: Forwarded to :meth:`create_trail`.

        Returns:
            A :class:`~walking_slice.projection.StatusBearingResponse`
            — either a
            :class:`~walking_slice.projection.ProjectedStatusResponse`
            (happy path or "trail.unresolved") or an
            :class:`~walking_slice.projection.ExplanationUnavailableResponse`
            (Projection Definition unresolvable).

        Raises:
            TrailValidationError: A structural rule was violated.
                Not wrapped — see method docstring.
            TrailAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt.
                Not wrapped — see method docstring.
            TrailAuditFailureError: The denial Record append failed
                on every retry. Not wrapped — see method docstring.
            ValueError: :attr:`authorization_service` is wired but
                ``engine`` was not supplied.
        """
        # Path 1 — Requirement 14.4 (explanation-unavailable for an
        # unresolvable Projection Definition). Probed BEFORE
        # :meth:`create_trail` runs so an unresolvable definition
        # cannot leave a Trail row behind. Source Records are left
        # unchanged because no INSERT has run.
        if not status_projector.has_definition(
            TRAIL_PROJECTION_DEFINITION_NAME
        ):
            return status_projector.project_status(
                definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
                # ``status`` value here is irrelevant — the projector
                # will return an :class:`ExplanationUnavailableResponse`
                # without consulting the status name. We pass a stable
                # placeholder so the validator (``min_length=1``) is
                # satisfied even on the withholding path.
                status=TRAIL_STATUS_UNRESOLVED,
                applicable_temporal_boundary=self.clock.now().replace(
                    microsecond=0
                ),
            )

        # Path 2 — call :meth:`create_trail`. Structural-validation,
        # resolvability, and authorization errors propagate to the
        # caller (see method docstring for the rationale).
        try:
            result = self.create_trail(
                connection,
                purpose=purpose,
                audience_id=audience_id,
                steps=steps,
                authoring_party_id=authoring_party_id,
                ordering_rationale=ordering_rationale,
                scope=scope,
                engine=engine,
                evaluation_at=evaluation_at,
                correlation_id=correlation_id,
            )
        except TrailTargetUnresolvedError as exc:
            # "Trail unresolved" status-bearing response per
            # Requirements 9.5 and 14.1. The per-ordinal unresolved
            # step list rides inside :attr:`details` so the envelope
            # remains a pure metadata wrapper; consumers that only
            # need the status name continue to read ``.status``.
            return status_projector.project_status(
                definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
                status=TRAIL_STATUS_UNRESOLVED,
                source_resource_ids=_collect_source_resource_ids(steps),
                source_revision_ids=_collect_source_revision_ids(steps),
                applicable_temporal_boundary=self.clock.now().replace(
                    microsecond=0
                ),
                details={
                    "error_code": exc.error_code,
                    "unresolved_steps": [
                        {
                            "ordinal": u.ordinal,
                            "target_kind": u.target_kind,
                            "target_id": u.target_id,
                            "target_revision_id": u.target_revision_id,
                            "region_id": u.region_id,
                        }
                        for u in exc.unresolved_steps
                    ],
                },
            )

        # Path 3 — Requirement 14.1 + 14.2 (happy path). Carry the
        # persisted Trail and Trail Revision identifiers alongside
        # the envelope so callers that need to follow up (for example,
        # render a confirmation page) do not need a second round trip.
        return status_projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_RESOLVED,
            source_resource_ids=_collect_source_resource_ids(steps),
            source_revision_ids=_collect_source_revision_ids(steps),
            applicable_temporal_boundary=self.clock.now().replace(
                microsecond=0
            ),
            details={
                "trail_id": result.trail_id,
                "trail_revision_id": result.trail_revision_id,
                "manifest_id": result.manifest_id,
                "recorded_at": result.recorded_at,
            },
        )

    # -- internal helpers --------------------------------------------------

    @staticmethod
    def _validate_required_strings(
        *,
        purpose: Optional[str],
        audience_id: Optional[str],
        ordering_rationale: Optional[str],
        authoring_party_id: Optional[str],
    ) -> None:
        """Reject Trail submissions missing or over-long required strings.

        Bounds drawn from Requirement 9.6 ("purpose 1..500, audience
        identifier, ordering rationale 0..500"). The
        ``Trail_Revisions`` schema marks ``purpose``, ``audience_id``,
        and ``authoring_party_id`` ``NOT NULL`` and leaves
        ``ordering_rationale`` nullable; the Python validators here
        run before the database round-trip so callers receive a
        structured constraint name instead of a generic
        IntegrityError.
        """
        if (
            purpose is None
            or not isinstance(purpose, str)
            or len(purpose) < _PURPOSE_MIN_CHARS
        ):
            raise TrailValidationError(
                "purpose is required and must be a non-empty string of "
                f"{_PURPOSE_MIN_CHARS}..{_PURPOSE_MAX_CHARS} characters "
                "(Requirement 9.6).",
                failed_constraint="purpose_missing",
            )
        if len(purpose) > _PURPOSE_MAX_CHARS:
            raise TrailValidationError(
                f"purpose length {len(purpose)} exceeds the "
                f"{_PURPOSE_MAX_CHARS}-character limit (Requirement 9.6).",
                failed_constraint="purpose_too_long",
            )
        if (
            audience_id is None
            or not isinstance(audience_id, str)
            or len(audience_id) < _AUDIENCE_MIN_CHARS
        ):
            raise TrailValidationError(
                "audience_id is required and must be a non-empty string "
                "(Requirement 9.6).",
                failed_constraint="audience_id_missing",
            )
        if ordering_rationale is not None:
            if not isinstance(ordering_rationale, str):
                raise TrailValidationError(
                    "ordering_rationale, when supplied, must be a string of "
                    f"0..{_ORDERING_RATIONALE_MAX_CHARS} characters "
                    "(Requirement 9.6).",
                    failed_constraint="ordering_rationale_too_long",
                )
            if len(ordering_rationale) > _ORDERING_RATIONALE_MAX_CHARS:
                raise TrailValidationError(
                    f"ordering_rationale length {len(ordering_rationale)} "
                    f"exceeds the {_ORDERING_RATIONALE_MAX_CHARS}-character "
                    "limit (Requirement 9.6).",
                    failed_constraint="ordering_rationale_too_long",
                )
        if not authoring_party_id:
            raise TrailValidationError(
                "authoring_party_id is required.",
                failed_constraint="authoring_party_id_missing",
            )

    @staticmethod
    def _validate_steps(
        steps: tuple[TrailStepInput, ...],
    ) -> tuple[TrailStepInput, ...]:
        """Apply Requirement 9.1 / 9.2 / 9.3 / 9.7 + AD-WS-12 to ``steps``.

        Returns the steps in ordinal order so the caller can iterate
        without re-sorting. The returned tuple is a *new* tuple — the
        caller's input is not mutated.

        Validation rules:

        1. Exactly :data:`_REQUIRED_STEP_COUNT` steps
           (Requirement 9.1, 9.7).
        2. Ordinals are exactly :data:`_REQUIRED_ORDINALS`
           (Requirement 9.2, 9.7).
        3. Each step's ``target_kind`` matches the kind for its
           ordinal (Requirement 9.2, 9.7).
        4. Each step's ``selection_mode`` is ``'Pinned'`` (AD-WS-12,
           Requirement 9.3).
        5. Each step's ``target_id`` is a non-empty string.
        6. ``target_revision_id`` is present for ordinals that need
           a Revision Identity (1, 3, 4) and absent for ordinals
           that do not (2, 5) — see :class:`TrailStepInput`.
        7. ``region_id`` is present iff ordinal == 2.
        8. ``annotation`` length, when supplied, is at most
           :data:`_ANNOTATION_MAX_CHARS` (Requirement 9.3).
        """
        # 1. Step count (Requirement 9.1 / 9.7).
        if len(steps) != _REQUIRED_STEP_COUNT:
            raise TrailValidationError(
                f"steps must contain exactly {_REQUIRED_STEP_COUNT} entries; "
                f"received {len(steps)} (Requirement 9.1, 9.7).",
                failed_constraint="step_count_invalid",
            )
        # 2. Ordinal set (Requirement 9.2 / 9.7).
        ordinals = [step.ordinal for step in steps]
        if frozenset(ordinals) != _REQUIRED_ORDINALS:
            raise TrailValidationError(
                "Trail Step ordinals must be exactly the contiguous "
                f"integers {sorted(_REQUIRED_ORDINALS)}; received "
                f"{sorted(set(ordinals))} (duplicates allowed in the "
                "input but rejected by this check). Requirement 9.2 / 9.7.",
                failed_constraint="ordinals_not_contiguous_1_to_5",
            )
        # Sort so downstream iteration walks 1 → 5 deterministically.
        sorted_steps: tuple[TrailStepInput, ...] = tuple(
            sorted(steps, key=lambda s: s.ordinal)
        )
        # 3. Per-step validators.
        for step in sorted_steps:
            TrailService._validate_step_target_kind(step)
            TrailService._validate_step_selection_mode(step)
            TrailService._validate_step_identifiers(step)
            TrailService._validate_step_annotation(step)
        return sorted_steps

    @staticmethod
    def _validate_step_target_kind(step: TrailStepInput) -> None:
        """Reject a step whose ``target_kind`` does not match its ordinal.

        Requirement 9.2 fixes the target kind per ordinal; the schema
        CHECK constraint on ``Trail_Steps`` enforces the same pairing.
        Failing here surfaces a precise constraint name instead of a
        generic IntegrityError.
        """
        expected = ORDINAL_TARGET_KIND.get(step.ordinal)
        if expected is None or step.target_kind != expected:
            raise TrailValidationError(
                f"steps[ordinal={step.ordinal}].target_kind "
                f"{step.target_kind!r} does not match the expected kind "
                f"{expected!r} for ordinal {step.ordinal} (Requirement "
                "9.2, 9.7).",
                failed_constraint="target_kind_invalid_for_ordinal",
            )

    @staticmethod
    def _validate_step_selection_mode(step: TrailStepInput) -> None:
        """Reject a step with ``selection_mode != 'Pinned'`` (AD-WS-12)."""
        if step.selection_mode != _PINNED_SELECTION_MODE:
            raise TrailValidationError(
                f"steps[ordinal={step.ordinal}].selection_mode "
                f"{step.selection_mode!r} is not {_PINNED_SELECTION_MODE!r}; "
                "AD-WS-12 restricts the slice to Pinned Trail Steps.",
                failed_constraint="selection_mode_invalid",
            )

    @staticmethod
    def _validate_step_identifiers(step: TrailStepInput) -> None:
        """Reject a step with missing or unexpected identifier fields.

        The rules track :class:`TrailStepInput`'s per-ordinal field
        interpretation table. The validator runs before the database
        round-trip so each violation surfaces with a precise
        constraint name rather than a generic NOT NULL or CHECK
        IntegrityError.
        """
        ordinal = step.ordinal
        if not step.target_id:
            raise TrailValidationError(
                f"steps[ordinal={ordinal}].target_id is required.",
                failed_constraint="target_id_missing",
            )

        # target_revision_id is required for ordinals 1, 3, 4 and
        # absent for ordinals 2, 5 (see TrailStepInput docstring).
        needs_revision = ordinal in (1, 3, 4)
        has_revision = step.target_revision_id is not None and step.target_revision_id != ""
        if needs_revision and not has_revision:
            raise TrailValidationError(
                f"steps[ordinal={ordinal}].target_revision_id is required "
                f"for ordinal {ordinal} ({ORDINAL_TARGET_KIND[ordinal]}).",
                failed_constraint="target_revision_id_missing",
            )
        if not needs_revision and has_revision:
            raise TrailValidationError(
                f"steps[ordinal={ordinal}].target_revision_id must be "
                f"omitted for ordinal {ordinal} "
                f"({ORDINAL_TARGET_KIND[ordinal]}); the schema requires "
                "NULL there.",
                failed_constraint="target_revision_id_unexpected",
            )

        # region_id is required for ordinal 2 and forbidden everywhere
        # else (schema CHECK constraint on Trail_Steps).
        if ordinal == 2:
            if not step.region_id:
                raise TrailValidationError(
                    f"steps[ordinal={ordinal}].region_id is required for "
                    "ordinal 2 (region_occurrence).",
                    failed_constraint="region_id_missing",
                )
        else:
            if step.region_id:
                raise TrailValidationError(
                    f"steps[ordinal={ordinal}].region_id must be omitted "
                    f"for ordinal {ordinal} ({ORDINAL_TARGET_KIND[ordinal]});"
                    " the schema requires NULL there.",
                    failed_constraint="region_id_unexpected",
                )

    @staticmethod
    def _validate_step_annotation(step: TrailStepInput) -> None:
        """Reject a step annotation exceeding :data:`_ANNOTATION_MAX_CHARS`."""
        if step.annotation is None:
            return
        if not isinstance(step.annotation, str):
            raise TrailValidationError(
                f"steps[ordinal={step.ordinal}].annotation, when supplied, "
                f"must be a string of 0..{_ANNOTATION_MAX_CHARS} characters "
                "(Requirement 9.3).",
                failed_constraint="annotation_too_long",
            )
        if len(step.annotation) > _ANNOTATION_MAX_CHARS:
            raise TrailValidationError(
                f"steps[ordinal={step.ordinal}].annotation length "
                f"{len(step.annotation)} exceeds the "
                f"{_ANNOTATION_MAX_CHARS}-character limit "
                "(Requirement 9.3).",
                failed_constraint="annotation_too_long",
            )

    @staticmethod
    def _resolve_targets(
        connection: Connection,
        steps: tuple[TrailStepInput, ...],
    ) -> tuple[UnresolvedTrailStep, ...]:
        """Check each step's target against the database; return unresolved.

        Per Requirement 9.5 the entire request is rejected if any
        target fails to resolve. The resolution runs BEFORE any
        INSERT and uses the caller's connection in a read-only fashion
        (SELECT 1 …) so the caller's transaction is not poisoned.

        Returns:
            A tuple of :class:`UnresolvedTrailStep` for every step
            whose target did not resolve. Empty when all five
            targets exist. The order matches the (already sorted by
            ordinal) ``steps`` argument so the response surfaces the
            steps in the order the Trail walks.
        """
        unresolved: list[UnresolvedTrailStep] = []
        for step in steps:
            if not TrailService._step_target_exists(connection, step):
                unresolved.append(
                    UnresolvedTrailStep(
                        ordinal=step.ordinal,
                        target_kind=step.target_kind,
                        target_id=step.target_id,
                        target_revision_id=step.target_revision_id,
                        region_id=step.region_id,
                    )
                )
        return tuple(unresolved)

    @staticmethod
    def _step_target_exists(
        connection: Connection, step: TrailStepInput
    ) -> bool:
        """Return ``True`` iff the step's target resolves.

        Each branch issues one SELECT keyed on the appropriate
        composite key. The queries are read-only and run on the
        caller's connection so no transaction boundary is created
        here.
        """
        ordinal = step.ordinal
        if ordinal == 1:
            row = connection.execute(
                text(
                    """
                    SELECT 1 FROM Document_Revisions
                     WHERE revision_id = :rev
                       AND resource_id = :rid
                    """
                ),
                {
                    "rev": step.target_revision_id,
                    "rid": step.target_id,
                },
            ).scalar_one_or_none()
            return row is not None
        if ordinal == 2:
            row = connection.execute(
                text(
                    """
                    SELECT 1 FROM Region_Occurrences
                     WHERE region_id = :region_id
                       AND document_revision_id = :doc
                    """
                ),
                {
                    "region_id": step.region_id,
                    "doc": step.target_id,
                },
            ).scalar_one_or_none()
            return row is not None
        if ordinal == 3:
            row = connection.execute(
                text(
                    """
                    SELECT 1 FROM Finding_Revisions
                     WHERE finding_revision_id = :rev
                       AND finding_id = :rid
                    """
                ),
                {
                    "rev": step.target_revision_id,
                    "rid": step.target_id,
                },
            ).scalar_one_or_none()
            return row is not None
        if ordinal == 4:
            row = connection.execute(
                text(
                    """
                    SELECT 1 FROM Recommendation_Revisions
                     WHERE recommendation_revision_id = :rev
                       AND recommendation_id = :rid
                    """
                ),
                {
                    "rev": step.target_revision_id,
                    "rid": step.target_id,
                },
            ).scalar_one_or_none()
            return row is not None
        if ordinal == 5:
            row = connection.execute(
                text(
                    "SELECT 1 FROM Decisions WHERE decision_id = :did"
                ),
                {"did": step.target_id},
            ).scalar_one_or_none()
            return row is not None
        # Defensive: structural validation should have rejected
        # anything outside 1..5 before reaching here.
        return False  # pragma: no cover

    @staticmethod
    def _load_current_revision(
        connection: Connection, trail_id: str
    ) -> Optional[
        tuple[
            str,                          # current trail_revision_id
            str,                          # purpose
            str,                          # audience_id
            Optional[str],                # ordering_rationale
            str,                          # recorded_at
            tuple[TrailStepInput, ...],   # prior steps in ordinal order
            tuple[str, ...],              # prior step ids in ordinal order
        ]
    ]:
        """Load the current Trail Revision plus its five steps.

        Returns ``None`` when ``trail_id`` does not name a row in
        ``Trails``. When the row exists but ``current_revision_id``
        is NULL (impossible after a successful :meth:`create_trail`,
        but defensible against schema-level partial inserts) the
        function also returns ``None`` so the caller raises
        :class:`TrailNotFoundError` rather than a confusing
        ``NoneType`` attribute error.

        Used by :meth:`append_revision` to (a) reject submissions
        against unknown Trails and (b) reconstruct the prior
        Revision's canonical payload for material-change detection
        (Requirement 9.4).

        The returned ``prior steps`` tuple is sorted by ordinal so
        the canonical-form helper :meth:`_canonical_payload`
        produces a byte-equivalent string against the same set of
        steps regardless of submission order — the same property
        :meth:`_validate_steps` enforces for new submissions.
        """
        revision_row = (
            connection.execute(
                text(
                    """
                    SELECT t.current_revision_id  AS current_revision_id,
                           tr.purpose             AS purpose,
                           tr.audience_id         AS audience_id,
                           tr.ordering_rationale  AS ordering_rationale,
                           tr.recorded_at         AS recorded_at
                      FROM Trails t
                      LEFT JOIN Trail_Revisions tr
                        ON tr.trail_revision_id = t.current_revision_id
                     WHERE t.trail_id = :tid
                    """
                ),
                {"tid": trail_id},
            )
            .mappings()
            .one_or_none()
        )
        if revision_row is None:
            return None
        current_revision_id = revision_row["current_revision_id"]
        if current_revision_id is None:
            return None

        step_rows = (
            connection.execute(
                text(
                    """
                    SELECT trail_step_id, ordinal, target_kind, target_id,
                           target_revision_id, region_id, selection_mode,
                           annotation
                      FROM Trail_Steps
                     WHERE trail_revision_id = :trev
                     ORDER BY ordinal
                    """
                ),
                {"trev": current_revision_id},
            )
            .mappings()
            .all()
        )
        prior_steps = tuple(
            TrailStepInput(
                ordinal=row["ordinal"],
                target_kind=row["target_kind"],
                target_id=row["target_id"],
                target_revision_id=row["target_revision_id"],
                region_id=row["region_id"],
                selection_mode=row["selection_mode"],
                annotation=row["annotation"],
            )
            for row in step_rows
        )
        prior_step_ids = tuple(row["trail_step_id"] for row in step_rows)
        return (
            current_revision_id,
            revision_row["purpose"],
            revision_row["audience_id"],
            revision_row["ordering_rationale"],
            revision_row["recorded_at"],
            prior_steps,
            prior_step_ids,
        )

    @staticmethod
    def _canonical_payload(
        *,
        purpose: str,
        audience_id: str,
        ordering_rationale: Optional[str],
        steps: tuple[TrailStepInput, ...],
    ) -> str:
        """Build the canonical form of a Trail Revision for content digesting.

        The canonical form combines the Trail Revision's natural
        attributes plus the ordered Trail Step targets and
        annotations. Material-change detection (task 10.2) will use
        the same canonical form to decide whether a follow-up
        ``POST /trails/{id}/revisions`` should mint a new Revision —
        keeping the digest function here means both tasks share one
        source of truth.

        The JSON is generated with ``sort_keys=True`` on every nested
        object and an explicit step ordering by ``ordinal`` so the
        digest is stable across submissions that supply the same
        content in a different field order.
        """
        return json.dumps(
            {
                "purpose": purpose,
                "audience_id": audience_id,
                "ordering_rationale": ordering_rationale,
                "steps": [
                    {
                        "ordinal": step.ordinal,
                        "target_kind": step.target_kind,
                        "target_id": step.target_id,
                        "target_revision_id": step.target_revision_id,
                        "region_id": step.region_id,
                        "selection_mode": step.selection_mode,
                        "annotation": step.annotation,
                    }
                    for step in steps
                ],
            },
            sort_keys=True,
        )

    def _persist_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied Trail creation.

        Mirrors :meth:`walking_slice.knowledge.KnowledgeService._persist_decision_denial`
        — each attempt opens a *new* :meth:`Engine.begin` transaction
        and tries :meth:`AuditLog.append_denial`. On total failure
        :class:`TrailAuditFailureError` is raised so denial-and-audit
        cannot silently diverge (Requirement 7.6).
        """
        last_error: Optional[BaseException] = None
        total_attempts = len(_DENIAL_AUDIT_BACKOFFS_SECONDS) + 1
        for attempt_index in range(total_attempts):
            try:
                with engine.begin() as denial_conn:
                    self.audit_log.append_denial(
                        denial_conn,
                        actor_party_id=actor_party_id,
                        attempted_action=_AUTHORIZATION_ACTION_CREATE_TRAIL,
                        target_id=None,
                        target_revision_id=None,
                        reason_code=reason_code,
                        correlation_id=correlation_id,
                        recorded_time=recorded_time,
                    )
                return
            except (AuditAppendError, SQLAlchemyError) as exc:
                last_error = exc
                if attempt_index < len(_DENIAL_AUDIT_BACKOFFS_SECONDS):
                    self.denial_audit_sleep(
                        _DENIAL_AUDIT_BACKOFFS_SECONDS[attempt_index]
                    )

        raise TrailAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error
