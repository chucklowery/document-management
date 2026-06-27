"""Deliverable_Repository — produced Deliverable Resources and Revisions.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- §"Deliverable_Repository" — public dataclass surface, authority string
  (``create.produced_deliverable`` → ``contribute`` AND assignee binding
  per AD-WS-24 and AD-WS-29), AD-WS-9 separate-transaction Denial Record
  on deny with the Requirement 30.6 three-retry contract, the SHA-256
  content digest computed at write time, the ``role_marker =
  'generated_output'`` invariant on every produced Deliverable Revision
  (Requirement 26.2, design §"Persistence Invariants Summary" rule 9),
  and the produced-Deliverable Resource Identity / Source Evidence
  Document Resource Identity disjointness enforced through the AD-WS-19
  ``resource_kind`` tag (Requirement 26.3, Requirement 41 §13).
- §"Cross-Cutting Concerns" — Transactionality (one transaction inserts
  the Deliverable_Resources row, the Deliverable_Revisions row, and the
  consequential ``Audit_Records`` row); Identifiers (every new identity
  is a UUIDv7 minted by :class:`IdentityService` and registered in
  ``Identifier_Registry`` with ``resource_kind`` ∈
  ``{deliverable_resource, deliverable_revision}``); Authorization (the
  action string ``create.produced_deliverable`` maps to the
  ``contribute`` authority per AD-WS-24; the deny path uses the
  separate-transaction Denial-Record pattern reproduced from
  :class:`walking_slice.planning.activity_plans.ActivityPlanService`).
- AD-WS-24 — additive ``contribute`` mapping for
  ``create.produced_deliverable``.
- AD-WS-27 — Slice 3 Records are append-only with no supersession path;
  the immutability of every produced Deliverable Revision is enforced
  by the schema-level UPDATE / DELETE rejection triggers from
  :mod:`walking_slice.deliverables._persistence`.
- AD-WS-28 — Slice 3 emits new ``resource_kind`` values on the existing
  additive ``Identifier_Registry.resource_kind`` column; the values
  ``'deliverable_resource'`` and ``'deliverable_revision'`` are used by
  this module via :func:`walking_slice.execution._helpers._record_execution_artifact`.
- AD-WS-29 — two-stage authority evaluation for Contributor writes: the
  service first calls :meth:`AuthorizationService.evaluate` with the
  Work Assignment Record as the target, and then on a ``permit``
  outcome re-reads the persisted ``Work_Assignment_Records`` row inside
  the caller's transaction and requires
  ``assignee_party_id == authoring_party_id``. Both stages must pass; a
  failure of either stage produces an AD-WS-9-conformant denial
  response (Slice 1 Requirement 7.2 ``reason_code = 'no-role-assignment'``
  for the assignee-binding failure).

Task scope (task 4.1)
=====================

This module implements
:meth:`DeliverableRepositoryService.create_produced_deliverable`:

1. Optionally screen the original request body against the prohibited
   planning-attribute and observed-outcome prefix sets via
   :func:`walking_slice.execution._helpers._reject_prohibited_attributes`
   (Requirements 33.3, 34.2).
2. Validate inputs per Requirement 26.1 / 26.5: ``content_bytes`` length
   in 1..100 MB, ``content_type`` against the seven-value enumerated
   set, ``produced_deliverable_name`` length 1..200, and the required
   ID-shaped strings.
3. Resolve the originating Work Assignment Record by primary key from
   ``Work_Assignment_Records`` on the caller's connection; reject when
   unresolvable (Requirement 26.5).
4. Evaluate authorization on a *separate* transaction (the Slice 1
   single-writer accommodation); on a deny outcome, persist a Denial
   Record in another separate transaction with the Requirement 30.6
   three-retry exponential-backoff pattern and raise
   :class:`DeliverableRepositoryAuthorizationError`.
5. On a ``permit`` outcome, perform the AD-WS-29 second stage: re-read
   the persisted Work Assignment Record on the caller's connection and
   require ``assignee_party_id == authoring_party_id``. On mismatch
   roll back the caller's transaction (by raising) and append a Denial
   Record with ``reason_code = 'no-role-assignment'`` in a separate
   transaction.
6. Compute the SHA-256 hex digest over ``content_bytes`` at write time
   (Requirement 26.2 / Slice 1 Requirement 2.2).
7. Mint the produced Deliverable Resource Identity and produced
   Deliverable Revision Identity, registering each in
   ``Identifier_Registry`` with its Slice 3 ``resource_kind`` tag
   (Requirement 22.8, Requirement 26.3).
8. INSERT the ``Deliverable_Resources`` row, INSERT the
   ``Deliverable_Revisions`` row with ``role_marker =
   'generated_output'`` (Requirement 26.2), and append the
   consequential ``Audit_Records`` row (Requirement 26.7) — all inside
   the caller's transaction so a failure anywhere rolls every row back
   (Requirement 26.8).

Requirements satisfied
======================

    22.1  — produced Deliverable Resource and Revision identifiers are
            UUIDv7 strings minted by :class:`IdentityService`.
    22.2  — produced Deliverable Resource Identity and Revision Identity
            are persisted as two distinct values
            (``Deliverable_Resources.deliverable_id`` and
            ``Deliverable_Revisions.deliverable_revision_id``).
    22.3  — produced Deliverable Resource Identity survives rename /
            relocation: the schema carries the durable identity on the
            Resource row and the AD-WS-27 UPDATE rejection trigger
            preserves it byte-equivalent.
    26.1  — content_bytes length validated against 1..100 MB; content
            type validated against the seven-value enumeration.
    26.2  — every Revision row carries the Resource Identity, the
            Revision Identity, the content type, the SHA-256 digest, the
            authoring Contributor Party Identity, the recorded time, the
            literal ``role_marker = 'generated_output'``, and the
            originating Work Assignment Record Identity.
    26.3  — produced Deliverable Resource Identity is tagged
            ``resource_kind = 'deliverable_resource'`` in
            ``Identifier_Registry``; produced Deliverable Revision
            Identity is tagged ``resource_kind = 'deliverable_revision'``;
            disjointness from Slice 1 Source Evidence is inspectable at
            row level.
    26.4  — produced Deliverable Revision is immutable: schema-level
            UPDATE / DELETE rejection triggers (AD-WS-27) prevent any
            after-the-fact mutation.
    26.5  — input validation rejects zero-byte content, oversized
            content, unenumerated content types, missing names, names
            outside the 1..200 range, and unresolvable
            originating-Work-Assignment IDs.
    26.6  — unauthorized callers are denied via AuthorizationService;
            the Repository declines to create any row and the Audit_Log
            appends a Denial Record (AD-WS-9, extended by AD-WS-25).
    26.7  — every successful Revision creation appends one immutable
            consequential audit row inside the same transaction.
    26.8  — audit append failure causes the caller's transaction to roll
            back; no Resource or Revision row becomes referenceable.
    32.7  — ``create.produced_deliverable`` requires the ``contribute``
            authority AND assignee binding on the originating Work
            Assignment (AD-WS-29).
    33.3  — Deliverable_Repository request bodies that carry any
            planning-attribute prefix are rejected with a structured
            error listing every offending key.
    34.2  — Deliverable_Repository request bodies that carry any
            observed-outcome prefix are rejected with a structured
            error listing every offending key.
    41.13 — produced-Deliverable vs Source-Evidence disjointness is
            preserved via the ``resource_kind`` tag on the registry
            row and the ``role_marker`` CHECK on every Revision row.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final, Mapping, Optional

import uuid_utils
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.audit import AuditAppendError, AuditLog, format_iso8601_ms
from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.execution._helpers import (
    ALL_PROHIBITED_PREFIXES,
    ExecutionValidationError,
    _record_execution_artifact,
    _reject_prohibited_attributes,
)
from walking_slice.identity import IdentityService


__all__ = [
    "CreateProducedDeliverableResult",
    "DeliverableContentValidationError",
    "DeliverableRepositoryAuditFailureError",
    "DeliverableRepositoryAuthorizationError",
    "DeliverableRepositoryService",
    "DeliverableRevisionDigestMismatchError",
    "DeliverableRevisionNotFoundError",
    "DeliverableRevisionRow",
    "WorkAssignmentAssigneeBindingError",
    "WorkAssignmentNotResolvableError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Action strings, Relationship kind labels, registry tag values, and
# validation limits are pulled out as module-level ``Final`` so the names
# downstream property tests look for in ``Audit_Records.action_type`` and
# in ``Identifier_Registry.resource_kind`` are textually stable and the
# strings stay aligned with the AD-WS-24 authority mapping in
# :mod:`walking_slice.authorization`, the AD-WS-28 ``resource_kind``
# enumeration in :mod:`walking_slice.execution._helpers`, and the
# schema-level CHECK constraints in
# :mod:`walking_slice.deliverables._persistence`.
# ---------------------------------------------------------------------------


# ``create.produced_deliverable`` maps to the ``contribute`` authority per
# AD-WS-24 / Requirement 32.7. The string is also the ``action_type``
# recorded on the consequential audit row (Requirement 26.7) and on the
# separate-transaction Denial Record appended by
# :meth:`DeliverableRepositoryService._persist_denial` so audit consumers
# can correlate denial rows with the action a Party was attempting.
_ACTION_CREATE_PRODUCED_DELIVERABLE: Final[str] = "create.produced_deliverable"


# Authorization target kind. The target of a
# ``create.produced_deliverable`` evaluation is the originating Work
# Assignment Record — the scope on which Contributor authority is held
# (AD-WS-29) — not the produced Deliverable itself, which does not yet
# exist when authorization is evaluated.
_KIND_WORK_ASSIGNMENT_RECORD: Final[str] = "work_assignment_record"


# Identifier_Registry registration kinds (Slice 1 enumeration) and
# Slice 3 ``resource_kind`` tags (AD-WS-28). Pairing every produced
# Deliverable Resource with ``resource_kind = 'deliverable_resource'``
# (and every Revision with ``'deliverable_revision'``) is the row-level
# discriminator that makes Requirement 26.3 (produced Deliverable
# Resource Identity disjoint from Slice 1 Source Evidence Document
# Resource Identity) inspectable on the registry table.
_REGISTRY_KIND_RESOURCE: Final[str] = "resource"
_REGISTRY_KIND_REVISION: Final[str] = "revision"
_RESOURCE_KIND_DELIVERABLE_RESOURCE: Final[str] = "deliverable_resource"
_RESOURCE_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"


# Role marker pinned on every produced Deliverable Revision row per
# Requirement 26.2, design §"Persistence Invariants Summary" rule 9,
# and Requirement 41 §13. The schema-level CHECK on
# ``Deliverable_Revisions.role_marker`` rejects any other value; this
# constant simply provides a single source of truth so the application
# code, the audit row, and the schema agree on the spelling.
_ROLE_MARKER_GENERATED_OUTPUT: Final[str] = "generated_output"


# Validation limits per Requirement 26.1 / 26.5. The schema-level CHECK
# on ``Deliverable_Revisions.content_bytes`` rejects out-of-range
# content as a defense in depth; centralizing the numeric bounds here
# surfaces precise, structured constraint names through
# :class:`DeliverableContentValidationError`.
_MIN_CONTENT_BYTES: Final[int] = 1
_MAX_CONTENT_BYTES: Final[int] = 100 * 1024 * 1024  # 100 MB = 104857600 bytes


# Enumerated content types accepted by Requirement 26.1. The schema-level
# CHECK on ``Deliverable_Revisions.content_type`` enforces the same set.
_VALID_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "text/markdown",
        "text/plain",
        "application/pdf",
        "application/json",
        "image/png",
        "image/svg+xml",
        "application/octet-stream",
    }
)


# Produced-Deliverable name length boundaries (Requirement 26.1 / 26.5).
_NAME_MIN_CHARS: Final[int] = 1
_NAME_MAX_CHARS: Final[int] = 200


# Exponential backoff sequence for retrying the separate-transaction
# Denial Record append (Requirement 30.6 — Slice 3 reaffirms the Slice 1
# / Slice 2 three-retry contract). Three retries after the initial
# attempt for a total of four attempts. The sequence is byte-equivalent
# to the one used by every Planning_Service module so that Slice 1 /
# Slice 2 / Slice 3 endpoints present identical denial-side timing
# (Property 8 — Indistinguishable denial — relies on this).
_DENIAL_AUDIT_BACKOFFS_SECONDS: Final[tuple[float, ...]] = (0.01, 0.02, 0.04)


# Denial-reason code used when authorization permits the action but the
# AD-WS-29 second stage rejects it because the authoring Party is not
# the named assignee on the originating Work Assignment. Slice 1
# Requirement 7.2 enumerates this value as ``'no-role-assignment'``;
# the Slice 3 design §"Cross-Cutting Concerns" (*Authorization*)
# explicitly directs that the missing-assignee case be treated as "no
# effective Contributor role on this specific Work Assignment".
_REASON_CODE_NO_ROLE_ASSIGNMENT: Final[str] = "no-role-assignment"


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class DeliverableContentValidationError(ValueError):
    """Raised when a produced Deliverable submission fails Requirement 26.1 / 26.5 validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (task 15.1) can render a structured 400 response and tests can
    assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"content_bytes_missing"`` (None or non-bytes value),
            ``"content_bytes_empty"`` (zero-byte content),
            ``"content_bytes_too_large"`` (exceeds 100 MB),
            ``"content_type_missing"``,
            ``"content_type_unsupported"`` (not in the seven-value set),
            ``"produced_deliverable_name_missing"``,
            ``"produced_deliverable_name_too_long"``,
            ``"originating_work_assignment_id_missing"``,
            ``"authoring_party_id_missing"``,
            ``"prohibited_attribute"`` (the request body carried at
                least one planning-attribute or observed-outcome key —
                see :attr:`prohibited_keys`).
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


class WorkAssignmentNotResolvableError(LookupError):
    """Raised when the originating Work Assignment Identity does not resolve.

    Requirement 26.5 requires the named originating Work Assignment
    Record Identity to resolve to an existing
    ``Work_Assignment_Records`` row. The check runs before authorization
    evaluation so the deny path never reveals whether a Work Assignment
    exists for an unauthenticated caller.

    Attributes:
        originating_work_assignment_id: The Work Assignment Identity the
            caller supplied.
        failed_constraint: ``"originating_work_assignment_not_resolvable"``.
    """

    def __init__(
        self,
        *,
        originating_work_assignment_id: str,
        failed_constraint: str = "originating_work_assignment_not_resolvable",
    ) -> None:
        super().__init__(
            f"originating_work_assignment_id "
            f"{originating_work_assignment_id!r} did not resolve to an "
            "existing Work Assignment Record "
            f"(failed_constraint={failed_constraint!r})."
        )
        self.originating_work_assignment_id = originating_work_assignment_id
        self.failed_constraint = failed_constraint


class DeliverableRepositoryAuthorizationError(PermissionError):
    """Raised when :class:`AuthorizationService` denies the
    ``create.produced_deliverable`` action.

    Mirrors
    :class:`walking_slice.planning.activity_plans.ActivityPlanAuthorizationError`
    so the HTTP layer (task 15.1) can render the AD-WS-9 / Requirement
    30 indistinguishable denial response shape
    ``{generic_denial_indicator, reason_code, correlation_id}``. The
    exception carries only ``reason_code`` and ``correlation_id`` —
    Requirement 30 forbids leaking authorized Party identities, target
    existence, or role-assignment details beyond the requesting
    Party's view authority through the denial response.

    The same exception type is raised when the AD-WS-29 second stage
    fails (the authoring Party is not the named assignee on the
    originating Work Assignment); in that case ``reason_code`` is
    :data:`_REASON_CODE_NO_ROLE_ASSIGNMENT` so the HTTP layer can
    surface the same response shape it surfaces for an authorization
    deny.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Produced Deliverable creation denied: "
            f"reason_code={reason_code!r}, correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


class WorkAssignmentAssigneeBindingError(DeliverableRepositoryAuthorizationError):
    """Specialised :class:`DeliverableRepositoryAuthorizationError` for the
    AD-WS-29 assignee-binding failure.

    Subclass of :class:`DeliverableRepositoryAuthorizationError` so
    callers that catch the broader denial path continue to work, while
    tests that need to assert specifically on the AD-WS-29 path can
    catch this narrower type. The denial response shape is identical:
    ``{reason_code = 'no-role-assignment', correlation_id}``.

    Attributes:
        originating_work_assignment_id: The Work Assignment Identity
            against which the assignee binding was evaluated.
        authoring_party_id: The Party Identity the caller submitted as
            the authoring Contributor.
        actual_assignee_party_id: The Party Identity actually persisted
            on ``Work_Assignment_Records.assignee_party_id``.
    """

    def __init__(
        self,
        *,
        originating_work_assignment_id: str,
        authoring_party_id: str,
        actual_assignee_party_id: str,
        correlation_id: str,
    ) -> None:
        super().__init__(
            reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
            correlation_id=correlation_id,
        )
        self.originating_work_assignment_id = originating_work_assignment_id
        self.authoring_party_id = authoring_party_id
        self.actual_assignee_party_id = actual_assignee_party_id


class DeliverableRepositoryAuditFailureError(RuntimeError):
    """Raised when every retry of the denial-record append fails
    (Requirement 30.6).

    Mirrors
    :class:`walking_slice.planning.activity_plans.ActivityPlanAuditFailureError`.
    On total audit-append failure the exception is raised *in place of*
    :class:`DeliverableRepositoryAuthorizationError` — denial and audit
    have silently diverged and the operator must be told. The caller's
    transaction still rolls back so no ``Deliverable_Resources``,
    ``Deliverable_Revisions``, or consequential audit row is persisted.

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
            f"Denial Record append for a denied produced Deliverable failed "
            f"after {attempts} attempt(s): reason_code={reason_code!r}, "
            f"correlation_id={correlation_id!r}."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.attempts = attempts


class DeliverableRevisionNotFoundError(LookupError):
    """Raised by :meth:`DeliverableRepositoryService.get_revision` /
    :meth:`DeliverableRepositoryService.get_revision_text` when no
    ``Deliverable_Revisions`` row matches the supplied identifier.

    The Slice 3 read APIs (task 4.2) are consumed by the
    Provenance_Navigator's ``navigate_produced_deliverable_revision``
    traversal (task 12.2) and by the HTTP layer (task 15.1) so the
    "no matching row" branch needs a stable exception type. The
    exception is treated as the "unresolvable" branch of Requirement
    35.4 (gap descriptor with category ``unresolved``) when surfaced
    from the Provenance_Navigator; the HTTP layer maps it to a 404.

    Attributes:
        deliverable_revision_id: The Revision Identity the caller
            supplied that did not resolve.
    """

    def __init__(self, *, deliverable_revision_id: str) -> None:
        super().__init__(
            f"No Deliverable_Revisions row for deliverable_revision_id="
            f"{deliverable_revision_id!r}."
        )
        self.deliverable_revision_id = deliverable_revision_id


class DeliverableRevisionDigestMismatchError(RuntimeError):
    """Raised when a produced Deliverable Revision's recorded digest does
    not match the bytes returned by :meth:`DeliverableRepositoryService.get_revision_text`.

    :meth:`DeliverableRepositoryService.get_revision_text` recomputes
    SHA-256 over the persisted ``Deliverable_Revisions.content_bytes``
    blob and compares the result to the persisted
    ``Deliverable_Revisions.content_digest_sha256`` column value. The
    column was populated at INSERT time by
    :meth:`DeliverableRepositoryService.create_produced_deliverable`
    (Requirement 26.2 / Slice 1 Requirement 2.2) and the AD-WS-27
    UPDATE/DELETE rejection triggers preserve both columns
    byte-equivalent forever, so the equality is a database-level
    invariant. If the equality ever fails this exception is raised
    rather than returning bytes the caller might mistakenly trust;
    mirrors :class:`walking_slice.evidence.SpanDigestMismatchError`
    and serves Requirement 35.8 (the Provenance_Navigator must surface
    the digest of every produced Deliverable Revision it returns —
    a silent mismatch would break Property 7).

    The error is not recoverable by the caller; it indicates database
    corruption or an invariant violation. Tests should verify the
    happy path (digest verifies) rather than attempting to trigger a
    mismatch.

    Attributes:
        deliverable_revision_id: Revision Identity whose digest failed
            verification.
        recorded_digest: ``content_digest_sha256`` value from the
            persisted row.
        computed_digest: Lowercase-hex SHA-256 of the bytes read from
            the row.
    """

    def __init__(
        self,
        *,
        deliverable_revision_id: str,
        recorded_digest: str,
        computed_digest: str,
    ) -> None:
        super().__init__(
            f"Deliverable Revision {deliverable_revision_id!r} content digest "
            f"mismatch: recorded={recorded_digest!r}, "
            f"computed={computed_digest!r}. Indicates database corruption "
            "or an AD-WS-27 invariant violation."
        )
        self.deliverable_revision_id = deliverable_revision_id
        self.recorded_digest = recorded_digest
        self.computed_digest = computed_digest


# ---------------------------------------------------------------------------
# Result value object.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreateProducedDeliverableResult:
    """Result of :meth:`DeliverableRepositoryService.create_produced_deliverable`.

    Returned so callers (the HTTP layer in task 15.1, tests, and the
    downstream Deliverable Production service that links a produced
    Deliverable Revision to a Deliverable Expectation) can correlate the
    created Resource and Revision with the consequential audit row in
    one round-trip.

    Attributes:
        deliverable_id: The produced Deliverable Resource Identity
            (UUIDv7).
        deliverable_revision_id: The produced Deliverable Revision
            Identity (UUIDv7).
        produced_deliverable_name: The persisted produced-Deliverable
            name (1..200 chars).
        content_type: The persisted content type (one of the seven
            enumerated values).
        content_digest_sha256: Lowercase-hex SHA-256 digest of the full
            ``content_bytes`` payload; byte-equivalent to the value
            persisted in ``Deliverable_Revisions.content_digest_sha256``.
        content_length_bytes: Length in bytes of the persisted content;
            convenience field so callers do not need to re-measure the
            payload they just submitted.
        role_marker: Always the literal ``"generated_output"`` —
            every produced Deliverable Revision carries this marker
            (Requirement 26.2, Requirement 41 §13).
        originating_work_assignment_id: The Work Assignment Record
            Identity under whose authority this Revision was authored;
            copied byte-equivalent from the request.
        authoring_party_id: Identity of the authoring Contributor
            Party (the named assignee on the originating Work
            Assignment per AD-WS-29).
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the Resource row, the Revision row, and the
            consequential audit row.
        correlation_id: The correlation identifier of this write —
            shared with the authorization evaluation row and the
            consequential audit row so audit consumers can join them
            on a single value.
    """

    deliverable_id: str
    deliverable_revision_id: str
    produced_deliverable_name: str
    content_type: str
    content_digest_sha256: str
    content_length_bytes: int
    role_marker: str
    originating_work_assignment_id: str
    authoring_party_id: str
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Read-only value object (Slice 3 — task 4.2 read APIs).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliverableRevisionRow:
    """Read-only snapshot of a ``Deliverable_Revisions`` row.

    Returned by :meth:`DeliverableRepositoryService.get_revision`, the
    Slice 3 read API introduced by task 4.2. The object carries every
    column persisted on the Revision row *except* ``content_bytes`` —
    callers needing the byte content invoke
    :meth:`DeliverableRepositoryService.get_revision_text` separately
    so the metadata read does not load a (potentially 100 MB) BLOB into
    memory.

    Frozen because — like every Slice 2 / Slice 3 value object that
    crosses a module boundary — the receiver must be able to rely on
    the bytes not changing while the in-flight transaction completes.
    A :func:`dataclasses.dataclass(frozen=True)` is used (rather than
    a Pydantic model) for two reasons: ``DeliverableRevisionRow`` only
    ever flows *outward* from the read API and never participates in
    input validation, and the AD-WS-27 UPDATE/DELETE rejection
    triggers already guarantee the underlying row is byte-equivalent
    forever — the frozen dataclass surfaces that on-disk invariant
    into the in-memory contract without a second validator layer.
    The pattern mirrors
    :class:`walking_slice.planning.plan_revisions.PlanRevisionRow`
    (task 2.1).

    Attributes:
        deliverable_revision_id: Identity of the produced Deliverable
            Revision row (UUIDv7). Primary key of
            ``Deliverable_Revisions``.
        deliverable_id: Identity of the owning produced Deliverable
            Resource (UUIDv7). Foreign key to
            ``Deliverable_Resources``; one Resource to many Revisions
            per Requirement 22.2.
        content_type: IANA-style content type, one of the seven
            enumerated values from Requirement 26.1.
        content_digest_sha256: Lowercase-hex SHA-256 digest of the
            full ``content_bytes`` payload (Requirement 26.2 / Slice
            1 Requirement 2.2). Always exactly 64 hex characters —
            the schema-level CHECK on
            ``Deliverable_Revisions.content_digest_sha256`` enforces
            this length, and :meth:`get_revision_text` verifies the
            digest against the persisted ``content_bytes`` on every
            read (Requirement 35.8, Property 7).
        role_marker: Always the literal ``"generated_output"`` —
            every produced Deliverable Revision carries this marker
            (Requirement 26.2, design §"Persistence Invariants
            Summary" rule 9, Requirement 41 §13). The schema-level
            CHECK rejects any other value at INSERT time, so this
            field is also a database-level invariant; the read API
            surfaces the marker so consumers like the
            Provenance_Navigator (Requirement 35.8) can distinguish
            a produced Deliverable Revision from a Slice 1 Source
            Evidence Document Revision without a second lookup.
        originating_work_assignment_id: Identity of the Slice 3 Work
            Assignment Record under whose authority the Revision was
            authored (Requirement 26.2). The
            Deliverable_Production_Records.originating-binding check
            (Requirement 27.4) consumes this field to reject forged
            productions that "claim" a peer's produced Deliverable
            Revision; surfacing it here keeps the originating-binding
            check a single read.
        authoring_party_id: Identity of the authoring Contributor
            Party (the named assignee on the originating Work
            Assignment per AD-WS-29). Persisted unchanged from
            creation; the AD-WS-27 UPDATE/DELETE rejection triggers
            preserve byte-equivalence.
        recorded_at: UTC ISO-8601 millisecond-precision timestamp
            shared by the Resource row, the Revision row, and the
            consequential audit row written at creation time (design
            §"Cross-Cutting Concerns" — Transactionality).
        content_length_bytes: Length in bytes of the persisted
            ``content_bytes`` blob. Computed by ``LENGTH(content_bytes)``
            at SELECT time so the metadata-only read does not load
            the blob into memory; the value always falls in the
            Requirement 26.1 range ``1..104857600`` because the
            schema-level CHECK enforces the bound at INSERT time.
    """

    deliverable_revision_id: str
    deliverable_id: str
    content_type: str
    content_digest_sha256: str
    role_marker: str
    originating_work_assignment_id: str
    authoring_party_id: str
    recorded_at: str
    content_length_bytes: int


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliverableRepositoryService:
    """Persist produced Deliverable Resources and their Generated-Output Revisions.

    Like
    :class:`walking_slice.planning.activity_plans.ActivityPlanService`,
    this service is connection-scoped at call time:
    :meth:`create_produced_deliverable` accepts the caller's
    :class:`sqlalchemy.engine.Connection` and writes inside the caller's
    transaction (AD-WS-5). The service instance therefore holds only the
    cross-request collaborators and can be shared across requests.

    Frozen because the dataclass design in
    ``.kiro/specs/third-walking-slice/design.md`` §"Deliverable_Repository"
    declares it ``@dataclass(frozen=True)`` — Slice 3 service instances
    are immutable container objects that bundle the Slice 1 and Slice 2
    collaborators for the Deliverable_Repository.

    Args:
        clock: Source of the recorded timestamp shared by the
            ``Deliverable_Resources``, ``Deliverable_Revisions``, and
            ``Audit_Records`` rows. The clock is consulted exactly once
            per write so every artifact of the transaction shares one
            timestamp.
        identity_service: Generates the produced Deliverable Resource
            Identity and Revision Identity and drives the
            :class:`Identifier_Registry` binding through
            :func:`walking_slice.execution._helpers._record_execution_artifact`.
        audit_log: Appends the consequential audit row (Requirement
            26.7) inside the caller's transaction.
        authorization_service: Evaluates
            ``create.produced_deliverable`` authority per AD-WS-24 /
            Requirement 26.6; the deny path is the AD-WS-9
            separate-transaction Denial-Record pattern with the
            Requirement 30.6 three-retry contract.
        denial_audit_sleep: Sleep function used to pause between retries
            of the Denial Record append. Defaults to :func:`time.sleep`;
            tests that need deterministic timing inject a recording stub
            so the retry sequence is observable without spending real
            time. The function is called with a single ``float``
            argument naming the seconds to sleep, drawn from
            :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS`.
    """

    clock: Clock
    identity_service: IdentityService
    audit_log: AuditLog
    authorization_service: AuthorizationService
    denial_audit_sleep: Callable[[float], None] = field(default=time.sleep)

    # -- public surface ----------------------------------------------------

    def create_produced_deliverable(
        self,
        connection: Connection,
        *,
        content_bytes: bytes,
        content_type: str,
        produced_deliverable_name: str,
        originating_work_assignment_id: str,
        authoring_party_id: str,
        engine: Engine,
        correlation_id: Optional[str] = None,
        evaluation_at: Optional[datetime] = None,
        request_attributes: Optional[Mapping[str, Any]] = None,
    ) -> CreateProducedDeliverableResult:
        """Create a produced Deliverable Resource and its first Revision.

        Per Requirements 26.1 through 26.8, 32.7, 33.3, 34.2, AD-WS-9
        (indistinguishable denial), AD-WS-24
        (``create.produced_deliverable`` → ``contribute``), AD-WS-27
        (insert-only with append-only triggers), AD-WS-28
        (``resource_kind`` tag on every identifier), and AD-WS-29
        (two-stage assignee binding):

        1. Optionally screen the original request body against every
           prohibited planning-attribute and observed-outcome prefix
           (Requirements 33.3, 34.2).
        2. Input validation (Requirement 26.1 / 26.5) — every range,
           enumeration, and required-attribute check runs before any
           database read so a malformed request never touches the
           identity service, the ``Work_Assignment_Records`` lookup,
           or the authorization service.
        3. Resolve the originating Work Assignment Record via a single
           indexed SELECT on ``Work_Assignment_Records``. When the
           identifier does not resolve, raise
           :class:`WorkAssignmentNotResolvableError`. The check runs
           before authorization evaluation so the deny path never
           reveals whether a Work Assignment exists for an
           unauthenticated caller.
        4. Run the authorization evaluation on a *separate*
           transaction (the Slice 1 single-writer accommodation). On
           ``deny``, append a Denial Record in another separate
           transaction with the Requirement 30.6 retry sequence
           (0.01s / 0.02s / 0.04s exponential backoff, three retries
           after the initial attempt). On total audit failure raise
           :class:`DeliverableRepositoryAuditFailureError` in place of
           :class:`DeliverableRepositoryAuthorizationError`.
        5. On ``permit``, perform the AD-WS-29 second stage: re-read
           the persisted Work Assignment Record on the caller's
           connection and require
           ``assignee_party_id == authoring_party_id``. On mismatch,
           append a Denial Record with
           ``reason_code = 'no-role-assignment'`` in a separate
           transaction and raise
           :class:`WorkAssignmentAssigneeBindingError` so the caller's
           originating transaction rolls back without persisting any
           Resource or Revision row.
        6. Compute the SHA-256 hex digest of ``content_bytes`` at
           write time. The same digest is used both as the
           Identifier_Registry binding value for the Revision Identity
           and as the persisted ``Deliverable_Revisions.content_digest_sha256``
           column value (Requirement 26.2 / Slice 1 Requirement 2.2).
        7. Mint the produced Deliverable Resource Identity and produced
           Deliverable Revision Identity, then call
           :func:`walking_slice.execution._helpers._record_execution_artifact`
           twice to register each in ``Identifier_Registry`` with the
           appropriate ``kind`` and Slice 3 ``resource_kind`` tag
           (Requirement 22.8, Requirement 26.3).
        8. INSERT the ``Deliverable_Resources`` row (Resource header).
        9. INSERT the ``Deliverable_Revisions`` row with
           ``role_marker = 'generated_output'`` (Requirement 26.2),
           the computed SHA-256 digest, the recorded time, and the
           originating Work Assignment Record Identity.
        10. Append the consequential ``Audit_Records`` row with
            ``action_type = 'create.produced_deliverable'`` and
            ``target_id = deliverable_id`` / ``target_revision_id =
            deliverable_revision_id`` (Requirement 26.7) inside the
            same transaction (AD-WS-5).

        Rows are inserted in dependency order so a FK failure anywhere
        rolls back the whole transaction (Requirement 26.8).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. The Resource row, the Revision row, the
                two ``Identifier_Registry`` rows, and the consequential
                audit row are all inserted on this connection.
            content_bytes: The full byte content of the produced
                Deliverable Revision (Requirement 26.1: length in
                1..100 MB).
            content_type: The IANA-style content type of the produced
                content. Must be one of :data:`_VALID_CONTENT_TYPES`.
            produced_deliverable_name: The produced-Deliverable name
                of 1..200 characters (Requirement 26.1 / 26.5).
            originating_work_assignment_id: Identity of the Work
                Assignment Record under whose authority this Revision
                is being authored. Must resolve to an existing row in
                ``Work_Assignment_Records`` AND the persisted row's
                ``assignee_party_id`` must equal ``authoring_party_id``
                (AD-WS-29).
            authoring_party_id: Identity of the authoring Contributor
                Party. Persisted on
                ``Deliverable_Revisions.authoring_party_id`` and on the
                consequential audit row's ``actor_party_id``. The
                Slice 1 ``Parties`` foreign key is enforced by the
                database; AD-WS-29 additionally requires the Party be
                the named assignee on the originating Work Assignment.
            engine: Required for the deny path's separate-transaction
                Denial Record write so the row survives the caller's
                rollback (Requirement 30.6). The same engine is used
                to open a fresh transaction for the authorization
                evaluation itself (Slice 1 single-writer accommodation).
            correlation_id: Optional correlation identifier shared by
                every audit row written in this operation. A UUIDv7 is
                generated when omitted.
            evaluation_at: Optional explicit effective time passed to
                :meth:`AuthorizationService.evaluate` as the ``at``
                parameter. Defaults to the recorded time of this
                transaction so the evaluation row's recorded time
                aligns with the consequential write it authorized.
            request_attributes: Optional mapping of the original
                top-level request body keys. When provided, the
                mapping is screened against every prohibited prefix
                (Requirements 33.3, 34.2). The HTTP layer forwards
                the raw request body here; service-level callers
                (e.g., unit tests) may pass ``None`` to skip the
                screen since the typed kwargs themselves cannot
                carry a prohibited attribute.

        Returns:
            :class:`CreateProducedDeliverableResult` carrying the
            persisted Resource and Revision Identities, the content
            digest, the recorded time, and the correlation identifier.

        Raises:
            DeliverableContentValidationError: A required attribute is
                missing or a Requirement 26.1 / 26.5 range / enumeration
                was violated, or the request body carried a prohibited
                planning-attribute or observed-outcome key.
            WorkAssignmentNotResolvableError: The originating Work
                Assignment Identity did not resolve to an existing
                ``Work_Assignment_Records`` row (Requirement 26.5).
            DeliverableRepositoryAuthorizationError: The wired
                :class:`AuthorizationService` denied the attempt
                (Requirement 26.6). The Denial Record was appended
                successfully in a separate transaction.
            WorkAssignmentAssigneeBindingError: Authorization
                permitted the attempt but the AD-WS-29 second stage
                rejected it because the authoring Party is not the
                named assignee on the originating Work Assignment.
                Subclass of
                :class:`DeliverableRepositoryAuthorizationError`.
            DeliverableRepositoryAuditFailureError: The wired
                :class:`AuthorizationService` denied the attempt *and*
                the separate-transaction Denial Record append failed
                on every retry (Requirement 30.6). Replaces
                :class:`DeliverableRepositoryAuthorizationError`.
            walking_slice.identity.IdentityConflictError: A freshly
                generated identifier collides with an existing
                ``Identifier_Registry`` binding (vanishingly rare for
                UUIDv7 within a single instance).
            walking_slice.audit.AuditAppendError: The consequential
                audit append failed (typically because
                ``authoring_party_id`` does not name an existing
                ``Parties`` row). The surrounding transaction MUST be
                allowed to roll back per Requirement 26.8 / Slice 1
                Requirement 13.6.
        """
        # 1. Screen the original request body when the route layer has
        # forwarded it. The typed kwargs themselves cannot carry a
        # prohibited attribute (the signature does not declare any such
        # field), but the HTTP layer's raw body might — Requirements
        # 33.3 and 34.2 demand rejection at the API boundary.
        if request_attributes is not None:
            try:
                _reject_prohibited_attributes(
                    request_attributes, ALL_PROHIBITED_PREFIXES
                )
            except ExecutionValidationError as exc:
                raise DeliverableContentValidationError(
                    str(exc),
                    failed_constraint="prohibited_attribute",
                    prohibited_keys=exc.prohibited_keys,
                ) from exc

        # 2. Validate inputs (Requirements 26.1, 26.5) before any
        # database read or authorization side-effect. Every validator
        # raises :class:`DeliverableContentValidationError` with a
        # stable ``failed_constraint`` so callers can map the
        # exception to a structured 400 response.
        self._validate_content_bytes(content_bytes)
        self._validate_content_type(content_type)
        self._validate_produced_deliverable_name(produced_deliverable_name)
        self._validate_required_strings(
            originating_work_assignment_id=originating_work_assignment_id,
            authoring_party_id=authoring_party_id,
        )

        # 3. Resolve the originating Work Assignment Record via a
        # single indexed SELECT on ``Work_Assignment_Records``. The
        # lookup runs on the caller's connection so it participates in
        # the caller's transactional view. Requirement 26.5 rejects
        # the unresolvable case before authorization evaluates the
        # request so the deny path cannot reveal whether the Work
        # Assignment exists.
        wa_row = connection.execute(
            text(
                "SELECT work_assignment_id, assignee_party_id, applicable_scope "
                "FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :work_assignment_id"
            ),
            {"work_assignment_id": originating_work_assignment_id},
        ).mappings().first()
        if wa_row is None:
            raise WorkAssignmentNotResolvableError(
                originating_work_assignment_id=originating_work_assignment_id,
            )

        applicable_scope = wa_row["applicable_scope"]

        # Capture one recorded time for the entire write so the
        # Resource, Revision, registry, and audit rows share a single
        # timestamp (design §"Cross-Cutting Concerns" — Transactionality).
        recorded_time = self.clock.now()
        recorded_at = format_iso8601_ms(recorded_time)
        correlation = correlation_id or _new_correlation_id()
        evaluate_at = (
            evaluation_at if evaluation_at is not None else recorded_time
        )

        # 4. Run the authorization evaluation on a SEPARATE
        # transaction (Slice 1 documented accommodation for SQLite's
        # single-writer model; the deny path opens *another* separate
        # transaction for the Denial Record write, and the caller's
        # transaction stays a reader until step 6 below). On
        # ``permit`` the evaluation row commits independently; on
        # ``deny`` the row rolls back with the evaluation transaction
        # and the durable record of the denial is the Denial Record
        # appended by :meth:`_persist_denial`.
        with engine.begin() as eval_conn:
            decision_outcome = self.authorization_service.evaluate(
                eval_conn,
                party_id=authoring_party_id,
                action=_ACTION_CREATE_PRODUCED_DELIVERABLE,
                target=TargetRef(
                    kind=_KIND_WORK_ASSIGNMENT_RECORD,
                    id=originating_work_assignment_id,
                    revision_id=None,
                    scope=applicable_scope,
                ),
                at=evaluate_at,
                correlation_id=correlation,
            )
        if decision_outcome.is_deny:
            reason_code = decision_outcome.reason_code or _REASON_CODE_NO_ROLE_ASSIGNMENT
            self._persist_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_work_assignment_id=originating_work_assignment_id,
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
                recorded_time=evaluate_at,
            )
            raise DeliverableRepositoryAuthorizationError(
                reason_code=reason_code,
                correlation_id=decision_outcome.correlation_id,
            )

        # 5. AD-WS-29 second stage: re-read the persisted Work
        # Assignment Record on the caller's connection and require
        # ``assignee_party_id == authoring_party_id``. The re-read
        # runs against the persisted row, not the request body, so the
        # bound is forge-proof. On mismatch, append a Denial Record in
        # a separate transaction (so the row survives the caller-side
        # rollback) and raise
        # :class:`WorkAssignmentAssigneeBindingError`; raising the
        # exception causes the caller's surrounding ``engine.begin()``
        # context manager to roll back without persisting any
        # Resource or Revision row.
        actual_assignee = wa_row["assignee_party_id"]
        if actual_assignee != authoring_party_id:
            self._persist_denial(
                engine=engine,
                actor_party_id=authoring_party_id,
                target_work_assignment_id=originating_work_assignment_id,
                reason_code=_REASON_CODE_NO_ROLE_ASSIGNMENT,
                correlation_id=correlation,
                recorded_time=evaluate_at,
            )
            raise WorkAssignmentAssigneeBindingError(
                originating_work_assignment_id=originating_work_assignment_id,
                authoring_party_id=authoring_party_id,
                actual_assignee_party_id=actual_assignee,
                correlation_id=correlation,
            )

        # 6. Compute the SHA-256 hex digest of the full content_bytes
        # payload at write time (Requirement 26.2 / Slice 1 Requirement
        # 2.2). The digest is bound to the Revision Identity in
        # ``Identifier_Registry`` AND persisted on the
        # ``Deliverable_Revisions.content_digest_sha256`` column so
        # the same digest verification logic Slice 1 uses for Region
        # Occurrences applies (Requirement 35.8, Property 7).
        revision_content_digest = hashlib.sha256(content_bytes).hexdigest()

        # 7. Mint the produced Deliverable Resource Identity and
        # produced Deliverable Revision Identity (AD-WS-2 / AD-WS-3 /
        # AD-WS-28) and register each in ``Identifier_Registry``.
        # Each registration carries the appropriate Slice 3
        # ``resource_kind`` tag so the eight Slice 3 identifier roles
        # remain pairwise disjoint relative to every Slice 1 and Slice
        # 2 identifier (Requirement 22.8) and so produced Deliverable
        # Resource Identity is inspectably disjoint from Slice 1
        # Source Evidence Document Resource Identity (Requirement
        # 26.3).
        deliverable_id = str(self.identity_service.new_resource_id())
        deliverable_revision_id = str(
            self.identity_service.new_revision_id()
        )

        # Resource-level content digest binds the durable Resource
        # Identity to the byte-equivalent identifying attributes of
        # the Resource header row (name, originating Work Assignment,
        # authoring Party). The Resource Identity is *not* bound to
        # the content bytes — Resource Identity survives across
        # multiple Revisions whose content differs (Requirement 22.3
        # / Slice 1 AD-WS-3); only the Revision Identity is bound to
        # the content digest.
        resource_content_digest = _sha256_hex(
            json.dumps(
                {
                    "produced_deliverable_name": produced_deliverable_name,
                    "originating_work_assignment_id": originating_work_assignment_id,
                    "authoring_party_id": authoring_party_id,
                    "recorded_at": recorded_at,
                },
                sort_keys=True,
            ).encode("utf-8")
        )

        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_RESOURCE,
            _RESOURCE_KIND_DELIVERABLE_RESOURCE,
            deliverable_id,
            resource_content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PRODUCED_DELIVERABLE,
            recorded_time=recorded_time,
        )
        _record_execution_artifact(
            connection,
            _REGISTRY_KIND_REVISION,
            _RESOURCE_KIND_DELIVERABLE_REVISION,
            deliverable_revision_id,
            revision_content_digest,
            identity_service=self.identity_service,
            actor_party_id=authoring_party_id,
            correlation_id=correlation,
            attempted_action=_ACTION_CREATE_PRODUCED_DELIVERABLE,
            recorded_time=recorded_time,
        )

        # 8. INSERT the produced Deliverable Resource header row. The
        # ``Deliverable_Resources`` table is the durable identity
        # carrier (Requirement 22.3 — Resource Identity survives
        # rename / relocation) and is insert-only per AD-WS-27.
        connection.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (
                    :deliverable_id, :produced_deliverable_name, :created_at
                )
                """
            ),
            {
                "deliverable_id": deliverable_id,
                "produced_deliverable_name": produced_deliverable_name,
                "created_at": recorded_at,
            },
        )

        # 9. INSERT the produced Deliverable Revision row. Every
        # column mandated by Requirement 26.2 is populated:
        # Resource Identity, Revision Identity, content_type,
        # content_bytes, content_digest_sha256, role_marker
        # ('generated_output'), originating_work_assignment_id,
        # authoring_party_id, and recorded_at. The schema-level CHECK
        # on ``role_marker`` enforces the literal value as a
        # defense-in-depth complement to the application-level
        # constant.
        connection.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes, content_digest_sha256,
                    role_marker, originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :deliverable_revision_id, :deliverable_id,
                    :content_type, :content_bytes, :content_digest_sha256,
                    :role_marker, :originating_work_assignment_id,
                    :authoring_party_id, :recorded_at
                )
                """
            ),
            {
                "deliverable_revision_id": deliverable_revision_id,
                "deliverable_id": deliverable_id,
                "content_type": content_type,
                "content_bytes": content_bytes,
                "content_digest_sha256": revision_content_digest,
                "role_marker": _ROLE_MARKER_GENERATED_OUTPUT,
                "originating_work_assignment_id": originating_work_assignment_id,
                "authoring_party_id": authoring_party_id,
                "recorded_at": recorded_at,
            },
        )

        # 10. Append the consequential audit row (Requirement 26.7 /
        # AD-WS-5). Participates in the caller's transaction so a
        # failure here rolls back the registry, Resource, and Revision
        # rows together (Requirement 26.8). The audit row points at the
        # Resource Identity as ``target_id`` and the Revision Identity
        # as ``target_revision_id`` so an auditor can correlate the
        # consequential write with both the durable Resource Identity
        # and the specific Revision created in the same transaction.
        self.audit_log.append_consequential(
            connection,
            actor_party_id=authoring_party_id,
            action_type=_ACTION_CREATE_PRODUCED_DELIVERABLE,
            target_id=deliverable_id,
            target_revision_id=deliverable_revision_id,
            correlation_id=correlation,
            recorded_time=recorded_time,
        )

        return CreateProducedDeliverableResult(
            deliverable_id=deliverable_id,
            deliverable_revision_id=deliverable_revision_id,
            produced_deliverable_name=produced_deliverable_name,
            content_type=content_type,
            content_digest_sha256=revision_content_digest,
            content_length_bytes=len(content_bytes),
            role_marker=_ROLE_MARKER_GENERATED_OUTPUT,
            originating_work_assignment_id=originating_work_assignment_id,
            authoring_party_id=authoring_party_id,
            recorded_at=recorded_at,
            correlation_id=correlation,
        )

    # -- read APIs ---------------------------------------------------------

    @staticmethod
    def get_revision(
        connection: Connection,
        deliverable_revision_id: str,
    ) -> Optional[DeliverableRevisionRow]:
        """Read-only lookup of a produced Deliverable Revision row by Identity.

        Implements the metadata half of task 4.2. Performs a single
        indexed ``SELECT`` against ``Deliverable_Revisions`` and
        returns the columns Slice 3 consumers need to reason about a
        produced Deliverable Revision *without* loading the
        potentially-100-MB ``content_bytes`` blob into memory:

        - ``deliverable_revision_id``, ``deliverable_id`` — the
          Identity pair (Requirement 22.2);
        - ``content_type`` and the ``LENGTH(content_bytes)``-derived
          ``content_length_bytes`` for shape descriptors;
        - ``content_digest_sha256`` (Requirement 26.2 / Slice 1
          Requirement 2.2) — the digest the Provenance_Navigator
          surfaces on every produced Deliverable Revision node per
          Requirement 35.8;
        - ``role_marker`` — always the literal ``'generated_output'``
          (the schema-level CHECK enforces this at INSERT time;
          surfaced here so consumers like the Provenance_Navigator
          can distinguish a produced Deliverable Revision from a
          Slice 1 Source Evidence Document Revision without a second
          lookup, per Requirement 35.8);
        - ``originating_work_assignment_id`` (Requirement 26.2) — the
          Slice 3 Work Assignment Record under whose authority the
          Revision was authored, consumed by the Deliverable
          Production Service's originating-binding check
          (Requirement 27.4) to reject forged productions;
        - ``authoring_party_id`` and ``recorded_at`` — auditing
          information for downstream consumers (Provenance_Navigator,
          HTTP layer).

        Task 4.2 explicitly prohibits any write path here. The
        function is :func:`staticmethod` because the read does not
        consult any of the wired collaborators (clock, identity
        service, audit log, authorization service) — it only needs
        the caller's SQLAlchemy ``Connection``. Exposing it on
        :class:`DeliverableRepositoryService` rather than as a
        module-level helper keeps the design-pinned entry-point
        name (``DeliverableRepositoryService.get_revision``)
        textually stable across the Slice 3 design and matches the
        convention established by
        :meth:`walking_slice.planning.plan_revisions.PlanRevisionService.get_plan_revision`
        (task 2.1) for additive read APIs.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                read context. The lookup participates in the caller's
                transactional view so consumers see a consistent
                snapshot across multiple reads (the Provenance
                Navigator's traversal in particular reads the
                Revision row and then the associated Deliverable
                Production Record in the same connection scope).
            deliverable_revision_id: The Revision Identity to
                resolve.

        Returns:
            A :class:`DeliverableRevisionRow` snapshot when a matching
            row exists. ``None`` when no ``Deliverable_Revisions`` row
            matches the supplied identifier; the caller treats
            ``None`` as the unresolvable branch (e.g., the
            Provenance_Navigator surfaces a Requirement 35.4 gap
            descriptor; the HTTP layer maps to a 404). Returning
            ``None`` rather than raising mirrors the
            :meth:`PlanRevisionService.get_plan_revision` convention
            and lets the caller decide how to handle the absent case
            without try/except in the hot path.

        Notes:
            The function does not validate
            ``deliverable_revision_id`` beyond passing it through to
            SQLAlchemy as a bound parameter; the calling layer is
            responsible for structural validation of identifiers
            received from untrusted input. A non-resolving identifier
            returns ``None`` rather than raising, mirroring the
            ``one_or_none`` convention already used elsewhere in
            this slice (see
            :meth:`walking_slice.planning.plan_revisions.PlanRevisionService.get_plan_revision`).
            The ``LENGTH(content_bytes)`` expression in the SELECT
            asks SQLite to return only the BLOB's byte length — not
            the bytes themselves — so the metadata read is
            constant-cost regardless of the Revision's content size.
        """
        row = connection.execute(
            text(
                "SELECT deliverable_revision_id, deliverable_id, "
                "content_type, content_digest_sha256, role_marker, "
                "originating_work_assignment_id, authoring_party_id, "
                "recorded_at, LENGTH(content_bytes) AS content_length_bytes "
                "FROM Deliverable_Revisions "
                "WHERE deliverable_revision_id = :deliverable_revision_id"
            ),
            {"deliverable_revision_id": deliverable_revision_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        return DeliverableRevisionRow(
            deliverable_revision_id=row["deliverable_revision_id"],
            deliverable_id=row["deliverable_id"],
            content_type=row["content_type"],
            content_digest_sha256=row["content_digest_sha256"],
            role_marker=row["role_marker"],
            originating_work_assignment_id=row["originating_work_assignment_id"],
            authoring_party_id=row["authoring_party_id"],
            recorded_at=row["recorded_at"],
            content_length_bytes=int(row["content_length_bytes"]),
        )

    @staticmethod
    def get_revision_text(
        connection: Connection,
        deliverable_revision_id: str,
    ) -> bytes:
        """Read the byte-equivalent content of a produced Deliverable Revision
        and verify its persisted digest.

        Implements the content half of task 4.2. Reads
        ``content_bytes`` and ``content_digest_sha256`` from the
        ``Deliverable_Revisions`` row in a single SELECT, recomputes
        SHA-256 over the returned bytes, and compares the result to
        the persisted digest before returning the bytes to the
        caller. The verification is the Slice 3 analogue of the
        Slice 1 :meth:`walking_slice.evidence.EvidenceRepository.resolve_region_text`
        check (Property 9, Requirement 11.2) and serves Requirement
        35.8 — the Provenance_Navigator must surface the digest of
        every produced Deliverable Revision it returns, so a silent
        digest mismatch here would break Property 7.

        Because :func:`walking_slice.deliverables._persistence.create_deliverable_schema`
        installs AD-WS-27 UPDATE/DELETE rejection triggers on
        ``Deliverable_Revisions``, both ``content_bytes`` and
        ``content_digest_sha256`` are byte-equivalent forever once
        the row is INSERTed (Requirement 26.4); the equality is a
        database-level invariant and a mismatch indicates corruption
        rather than a recoverable caller-side error. The method
        therefore raises :class:`DeliverableRevisionDigestMismatchError`
        on mismatch rather than returning bytes the caller might
        mistakenly trust.

        The method is :func:`staticmethod` for the same reasons given
        for :meth:`get_revision`: the read does not consult any of
        the wired collaborators and only needs the caller's
        ``Connection``. Exposing it on
        :class:`DeliverableRepositoryService` keeps the design-pinned
        entry-point name (``DeliverableRepositoryService.get_revision_text``)
        textually stable.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                read context.
            deliverable_revision_id: The Revision Identity whose
                content bytes are to be returned.

        Returns:
            The byte-equivalent ``content_bytes`` payload — the same
            bytes that were SHA-256 hashed at INSERT time to produce
            the persisted ``content_digest_sha256``. By the equality
            check performed in this method, recomputing SHA-256 over
            the returned bytes equals the persisted digest column,
            so the caller can treat the bytes as digest-verified
            without recomputing themselves.

        Raises:
            DeliverableRevisionNotFoundError: No
                ``Deliverable_Revisions`` row matches
                ``deliverable_revision_id``. Returning ``None`` would
                be ambiguous (zero bytes is a syntactically valid
                ``bytes`` value, but Requirement 26.1 rejects
                zero-byte content at INSERT time so it can never
                appear in the table — yet relying on that invariant
                would make the API harder to reason about). The
                explicit exception keeps the return type a
                non-Optional ``bytes`` and lets callers map the
                unresolved branch to a 404 / gap descriptor without
                ambiguity.
            DeliverableRevisionDigestMismatchError: The recomputed
                SHA-256 of the persisted ``content_bytes`` does not
                equal the persisted ``content_digest_sha256``. Not
                recoverable by the caller; indicates database
                corruption or an AD-WS-27 invariant violation.
        """
        row = connection.execute(
            text(
                "SELECT content_bytes, content_digest_sha256 "
                "FROM Deliverable_Revisions "
                "WHERE deliverable_revision_id = :deliverable_revision_id"
            ),
            {"deliverable_revision_id": deliverable_revision_id},
        ).mappings().one_or_none()
        if row is None:
            raise DeliverableRevisionNotFoundError(
                deliverable_revision_id=deliverable_revision_id,
            )

        # SQLite returns BLOB columns as ``bytes`` already; the
        # ``bytes(...)`` cast is defensive in case a future SQLAlchemy
        # release returns ``memoryview`` for performance and keeps
        # the public return type a concrete ``bytes`` value (which is
        # what every existing Slice 1 / Slice 2 consumer expects).
        content_bytes = bytes(row["content_bytes"])
        recorded_digest = row["content_digest_sha256"]
        computed_digest = hashlib.sha256(content_bytes).hexdigest()
        if computed_digest != recorded_digest:
            # AD-WS-27 makes Deliverable_Revisions insert-only via
            # triggers; a digest mismatch therefore indicates
            # database corruption rather than a recoverable
            # caller-side error. Refusing to return the bytes here
            # is the safer default (Requirement 35.8 / Property 7
            # demand byte-equivalence against the recorded digest).
            raise DeliverableRevisionDigestMismatchError(
                deliverable_revision_id=deliverable_revision_id,
                recorded_digest=recorded_digest,
                computed_digest=computed_digest,
            )
        return content_bytes

    # -- input validators --------------------------------------------------

    @staticmethod
    def _validate_content_bytes(content_bytes: Any) -> None:
        """Reject ``content_bytes`` outside the Requirement 26.1 range.

        Splits the failure modes into three distinct
        ``failed_constraint`` values so callers can present a precise
        error to the user: ``content_bytes_missing`` (None or non-bytes
        value — the actionable next step is "supply bytes"),
        ``content_bytes_empty`` (zero-byte content — Requirement 26.5
        zero-bytes rejection), and ``content_bytes_too_large``
        (exceeding 100 MB).
        """
        if content_bytes is None or not isinstance(content_bytes, (bytes, bytearray)):
            raise DeliverableContentValidationError(
                "content_bytes is required and must be a bytes-like value; "
                "Requirement 26.1 / 26.5.",
                failed_constraint="content_bytes_missing",
            )
        length = len(content_bytes)
        if length < _MIN_CONTENT_BYTES:
            raise DeliverableContentValidationError(
                f"content_bytes length {length} is below the "
                f"{_MIN_CONTENT_BYTES}-byte minimum imposed by Requirement 26.1.",
                failed_constraint="content_bytes_empty",
            )
        if length > _MAX_CONTENT_BYTES:
            raise DeliverableContentValidationError(
                f"content_bytes length {length} exceeds the "
                f"{_MAX_CONTENT_BYTES}-byte (100 MB) maximum imposed by "
                "Requirement 26.1.",
                failed_constraint="content_bytes_too_large",
            )

    @staticmethod
    def _validate_content_type(content_type: Any) -> None:
        """Reject ``content_type`` outside the Requirement 26.1 enumeration."""
        if content_type is None or not isinstance(content_type, str) or content_type == "":
            raise DeliverableContentValidationError(
                "content_type is required and must be one of "
                f"{sorted(_VALID_CONTENT_TYPES)!r}; Requirement 26.1.",
                failed_constraint="content_type_missing",
            )
        if content_type not in _VALID_CONTENT_TYPES:
            raise DeliverableContentValidationError(
                f"content_type {content_type!r} is not one of the enumerated "
                f"values {sorted(_VALID_CONTENT_TYPES)!r} required by "
                "Requirement 26.1 / 26.5.",
                failed_constraint="content_type_unsupported",
            )

    @staticmethod
    def _validate_produced_deliverable_name(produced_deliverable_name: Any) -> None:
        """Reject ``produced_deliverable_name`` outside the 1..200 range.

        Empty or non-string names surface as
        ``produced_deliverable_name_missing`` since the actionable next
        step is the same in both cases (supply a non-empty string). An
        over-long name surfaces as
        ``produced_deliverable_name_too_long``.
        """
        if (
            produced_deliverable_name is None
            or not isinstance(produced_deliverable_name, str)
            or produced_deliverable_name == ""
        ):
            raise DeliverableContentValidationError(
                "produced_deliverable_name is required and must be a "
                f"non-empty string of {_NAME_MIN_CHARS}..{_NAME_MAX_CHARS} "
                "characters; Requirement 26.1 / 26.5.",
                failed_constraint="produced_deliverable_name_missing",
            )
        if len(produced_deliverable_name) > _NAME_MAX_CHARS:
            raise DeliverableContentValidationError(
                f"produced_deliverable_name length "
                f"{len(produced_deliverable_name)} exceeds the "
                f"{_NAME_MAX_CHARS}-character limit imposed by "
                "Requirement 26.1.",
                failed_constraint="produced_deliverable_name_too_long",
            )

    @staticmethod
    def _validate_required_strings(
        *,
        originating_work_assignment_id: Any,
        authoring_party_id: Any,
    ) -> None:
        """Reject submissions missing required ID-shaped strings.

        Per Requirements 26.5 and 26.6 the Repository requires both the
        originating Work Assignment Identity (so AD-WS-29 has a target
        to bind against) and the authoring Party Identity (so
        authorization has a subject to evaluate). Both must be
        non-empty strings; resolution against the persisted rows runs
        later in the workflow.
        """
        if (
            not originating_work_assignment_id
            or not isinstance(originating_work_assignment_id, str)
        ):
            raise DeliverableContentValidationError(
                "originating_work_assignment_id is required; "
                "Requirement 26.5 rejects produced Deliverables missing "
                "the originating Work Assignment Identity.",
                failed_constraint="originating_work_assignment_id_missing",
            )
        if not authoring_party_id or not isinstance(authoring_party_id, str):
            raise DeliverableContentValidationError(
                "authoring_party_id is required; Requirement 26.6 rejects "
                "unauthenticated produced Deliverable creation.",
                failed_constraint="authoring_party_id_missing",
            )

    # -- denial side-channel ----------------------------------------------

    def _persist_denial(
        self,
        *,
        engine: Engine,
        actor_party_id: str,
        target_work_assignment_id: str,
        reason_code: str,
        correlation_id: str,
        recorded_time: datetime,
    ) -> None:
        """Persist the Denial Record for a denied produced-Deliverable attempt.

        Implements the AD-WS-9 / Slice 1 Requirement 7.6 / Slice 3
        Requirement 30.6 retry contract verbatim (mirroring
        :meth:`walking_slice.planning.activity_plans.ActivityPlanService._persist_activity_plan_denial`):
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
          :class:`DeliverableRepositoryAuditFailureError` is raised.

        The separate transaction is essential: the caller's
        originating transaction is about to be rolled back when
        :meth:`create_produced_deliverable` raises
        :class:`DeliverableRepositoryAuthorizationError` or
        :class:`WorkAssignmentAssigneeBindingError` (or this method
        raises :class:`DeliverableRepositoryAuditFailureError`). The
        Denial Record must therefore live outside that scope to
        survive (AD-WS-9 / Requirement 30.6).

        ``target_id`` on the Denial Record points at the originating
        Work Assignment Identity rather than at a (non-existent)
        produced Deliverable Identity, because the produced Deliverable
        does not exist at the time the denial is recorded — the deny
        path explicitly refuses to mint a Resource Identity for an
        unauthorized attempt (Requirement 26.6 / Requirement 30.5 — no
        information leakage about the existence of restricted
        Resources).

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
                        attempted_action=_ACTION_CREATE_PRODUCED_DELIVERABLE,
                        target_id=target_work_assignment_id,
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

        raise DeliverableRepositoryAuditFailureError(
            reason_code=reason_code,
            correlation_id=correlation_id,
            attempts=total_attempts,
        ) from last_error


# ---------------------------------------------------------------------------
# Module-private helpers.
#
# Local copies of the correlation-id and SHA-256 helpers so this module
# does not import private names from sibling Planning / Execution
# modules. The functions are intentionally identical to their Planning
# siblings: correlation identifiers are non-domain values and the
# digest helper is opaque to :class:`Identifier_Registry`.
# ---------------------------------------------------------------------------


def _new_correlation_id() -> str:
    """Generate a UUIDv7 correlation identifier.

    Correlation identifiers join the authorization-evaluation audit
    row, the (separate-transaction) Denial Record, and the
    consequential audit row produced for the same logical produced
    Deliverable creation. They are not registered with
    :class:`IdentityService` because they do not name a domain
    Resource.
    """
    return str(uuid_utils.uuid7())


def _sha256_hex(content: bytes) -> str:
    """Lowercase-hex SHA-256 digest of ``content``.

    Used as the ``content_digest`` value bound to the produced
    Deliverable *Resource* Identity in ``Identifier_Registry`` (the
    Resource binding is digested over the Resource's identifying
    attributes; the Revision binding is digested over the content
    bytes themselves and is computed inline in
    :meth:`DeliverableRepositoryService.create_produced_deliverable`).
    """
    return hashlib.sha256(content).hexdigest()
