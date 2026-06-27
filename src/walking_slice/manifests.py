"""Provenance_Manifests writer — reusable across Finding, Recommendation,
Decision, and Trail Revision finalization.

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Provenance_Manifests and Omission_Entries", AD-WS-4 (immutable
Provenance_Manifests and Omission_Entries rows), AD-WS-5 (manifest
insertion runs in the originating write's transaction), and design
§"Persistence Invariants Summary" item 9 (``is_complete = 0`` when an
unresolved Omission Entry has a non-intentional category).

Task scope (task 9.1):

The slice's Finding, Recommendation, Decision, and Trail Revision flows
each must record a Provenance Manifest naming every material source
that contributed to the synthesis (Requirement 10.1) plus zero or more
Omission Entries naming sources the authoring Party deliberately
excluded or that fell into one of the four "incomplete" categories
{unavailable, restricted, stale, unresolved} (Requirements 10.2, 10.3).

:class:`KnowledgeService.create_decision` already writes the manifest
inline for Decisions because Decision finalization was the first
synthesis to land (task 8.1). This module pulls the manifest-write
logic out into a stand-alone :class:`ProvenanceManifestWriter` so the
remaining consequential writes (Finding finalization, Recommendation
finalization, Trail Revision finalization) wire to one canonical
implementation rather than re-deriving the rules in three places.

The writer adds two behaviours the inline Decision implementation does
not have, both required by Requirement 10:

1. **Source Freshness Window enforcement (Requirement 10.6).** The
   24-hour default refresh window from Requirement 10.3 is checked
   against every :class:`IncludedSource`. A source whose
   ``recorded_at`` is older than the window must either be refreshed
   or moved to an :class:`OmissionEntry` with ``category='stale'``;
   submitting it as an Included Source raises :class:`StalenessError`
   so the synthesis is rejected before any row is written. Callers
   wanting a non-default window pass ``freshness_window_seconds``
   explicitly.
2. **Generic ``subject_kind`` support.** Manifests for any of the four
   permitted subject kinds — ``'finding_revision'``,
   ``'recommendation_revision'``, ``'decision'``, ``'trail_revision'``
   — share one writer. The inline Decision implementation only knew
   about ``'decision'``.

The writer participates in the *caller's* transaction (AD-WS-5). The
``connection`` argument is a SQLAlchemy
:class:`~sqlalchemy.engine.Connection` opened by the originating
service (Knowledge, Trail, etc.); a failure on any INSERT here raises
through the connection and rolls the originating transaction back
(Requirements 2.7, 10.6, 13.6). No new connection or transaction is
opened by this module.

Requirements satisfied (per task 9.1):
    10.1 — every Finding, Recommendation, Decision, or Trail Revision
           manifest identifies every material source actually used,
           recorded in ``Provenance_Manifests.included_sources_json``
           as a JSON array of ``{kind, resource_id, revision_id,
           recorded_at}`` objects.
    10.2 — every Omission Entry records the excluded source Identity,
           the excluded source Revision Identity when known, the
           category, an exclusion rationale of 1..2,000 characters,
           the authoring Party Identity, and the recorded time.
    10.3 — when any unresolved Omission Entry has a non-intentional
           category (``unavailable``, ``restricted``, ``stale``, or
           ``unresolved``), the manifest's ``is_complete`` column is
           set to ``0``; intentional-only or no-omissions cases keep
           ``is_complete = 1``.
    10.6 — manifest persistence failures roll back the originating
           write through the shared transaction; the caller surfaces
           the manifest-persistence error. The 24-hour default Source
           Freshness Window is enforced via :class:`StalenessError`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, Optional, Sequence

import uuid_utils
from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.audit import format_iso8601_ms
from walking_slice.clock import Clock, truncate_to_milliseconds
from walking_slice.identity import IdentityService


__all__ = [
    "DEFAULT_FRESHNESS_WINDOW_SECONDS",
    "IncludedSource",
    "ManifestValidationError",
    "ManifestWriteResult",
    "OmissionEntry",
    "ProvenanceManifestWriter",
    "StalenessError",
]


# ---------------------------------------------------------------------------
# Constants.
#
# Each value mirrors a CHECK constraint or column-length rule from
# :mod:`walking_slice.persistence` so a caller's request is validated in
# Python before any database round-trip and the resulting error name
# matches the schema name the operator would otherwise see.
# ---------------------------------------------------------------------------


# Subject kinds permitted by the CHECK constraint on
# ``Provenance_Manifests.subject_kind``. The four Slice 1 kinds map to
# the four syntheses called out in Requirement 10.1 ("Finding,
# Recommendation, Decision, Trail Revision"). The fifth value
# ``'plan_approval'`` is the additive Slice 2 extension required by the
# second-walking-slice design §"Planning_Service.PlanApprovals", which
# specifies that the existing :class:`ProvenanceManifestWriter` is the
# one writer that records a Plan Approval's Provenance Manifest with
# ``subject_id`` set to the Plan Approval Immutable Record Identity.
# The schema CHECK constraint on ``Provenance_Manifests.subject_kind``
# in :mod:`walking_slice.persistence` is kept in lockstep with this
# set so the validator name and the SQL-level rejection cite the same
# value.
_SUBJECT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "finding_revision",
        "recommendation_revision",
        "decision",
        "trail_revision",
        "plan_approval",
    }
)

# Kinds permitted on entries inside ``included_sources_json``. The set is
# wider than ``_SUBJECT_KINDS`` because an Included Source may itself be
# the Document Revision or Region Occurrence that grounds a downstream
# Finding (Requirement 10.1). The slice's pipeline stages are the only
# meaningful values; widening the set requires a design change. The
# additive Slice 2 value ``'plan_revision'`` is permitted so the Plan
# Approval Provenance Manifest can record the target Plan Revision as
# the single material source (second-walking-slice design
# §"Planning_Service.PlanApprovals").
_INCLUDED_SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        "document_revision",
        "region_occurrence",
        "finding_revision",
        "recommendation_revision",
        "decision",
        "trail_revision",
        "plan_revision",
    }
)

# Categories permitted by the CHECK constraint on
# ``Omission_Entries.category`` and named in Requirement 10.3.
_OMISSION_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"intentional", "unavailable", "restricted", "stale", "unresolved"}
)

# Omission rationale length bounds — Requirement 10.2 and the schema
# ``NOT NULL TEXT`` column on ``Omission_Entries.rationale``.
_OMISSION_RATIONALE_MIN_CHARS: Final[int] = 1
_OMISSION_RATIONALE_MAX_CHARS: Final[int] = 2_000

# Default Source Freshness Window per Requirement 10.3 / 10.6 ("default
# 24 hours"). Exposed at module level so callers and tests can refer to
# the same constant rather than spelling out ``86_400``.
DEFAULT_FRESHNESS_WINDOW_SECONDS: Final[int] = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class ManifestValidationError(ValueError):
    """Raised when a manifest submission fails Requirement 10.x validation.

    ``failed_constraint`` names the specific violation so the HTTP layer
    (added by task 9.2) can render a structured 400 response and tests
    can assert against a stable identifier rather than the message text.

    Attributes:
        failed_constraint: One of
            ``"subject_kind_invalid"``,
            ``"authoring_party_id_missing"``,
            ``"subject_id_missing"``,
            ``"included_source_kind_invalid"``,
            ``"included_source_resource_id_missing"``,
            ``"included_source_recorded_at_missing"``,
            ``"omission_category_invalid"``,
            ``"omission_excluded_source_id_missing"``,
            ``"omission_rationale_missing"``,
            ``"omission_rationale_too_long"``,
            ``"freshness_window_invalid"``.
    """

    def __init__(self, message: str, *, failed_constraint: str) -> None:
        super().__init__(message)
        self.failed_constraint = failed_constraint


class StalenessError(ValueError):
    """Raised when an :class:`IncludedSource` falls outside the freshness window.

    Per Requirement 10.3 / 10.6: a material source whose ``recorded_at``
    is older than the configured Source Freshness Window (default 24
    hours) is *stale* and must either be refreshed before submission or
    moved to an :class:`OmissionEntry` with ``category='stale'``. A
    stale Included Source that the caller has not also recorded as a
    stale Omission Entry causes the manifest write to be rejected
    before any row is INSERTed so the originating synthesis cannot
    quietly claim freshness it does not have.

    Attributes:
        excluded_source_id: Resource Identity of the stale source.
        excluded_source_revision_id: Revision Identity of the stale
            source when known; ``None`` otherwise.
        source_recorded_at: The source's ``recorded_at`` as an
            ISO-8601 UTC millisecond timestamp.
        manifest_recorded_at: The manifest's ``recorded_at`` (the
            "current time" for the freshness comparison) as an
            ISO-8601 UTC millisecond timestamp.
        freshness_window_seconds: The window in effect for the check.
    """

    def __init__(
        self,
        *,
        excluded_source_id: str,
        excluded_source_revision_id: Optional[str],
        source_recorded_at: str,
        manifest_recorded_at: str,
        freshness_window_seconds: int,
    ) -> None:
        super().__init__(
            f"Included source resource_id={excluded_source_id!r}, "
            f"revision_id={excluded_source_revision_id!r} recorded at "
            f"{source_recorded_at!r} is older than the "
            f"{freshness_window_seconds}-second Source Freshness Window "
            f"(manifest recorded at {manifest_recorded_at!r}); move the "
            "source to an OmissionEntry with category='stale' or refresh "
            "the source before submission (Requirement 10.6)."
        )
        self.excluded_source_id = excluded_source_id
        self.excluded_source_revision_id = excluded_source_revision_id
        self.source_recorded_at = source_recorded_at
        self.manifest_recorded_at = manifest_recorded_at
        self.freshness_window_seconds = freshness_window_seconds
        self.failed_constraint = "source_stale"


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncludedSource:
    """One row in a Provenance Manifest's ``included_sources_json`` array.

    Each Included Source names a single material source that actually
    contributed to the synthesis (Requirement 10.1). The pair
    ``(resource_id, revision_id)`` resolves to the exact Revision or
    Immutable Record the synthesis consumed; ``recorded_at`` records
    when *that source itself* was recorded — not when the manifest is
    being written — so the writer can apply the Source Freshness
    Window check from Requirement 10.6.

    Frozen so the verify-then-write loop in
    :meth:`ProvenanceManifestWriter.write_manifest` cannot observe a
    different list than it just validated (design §"Cross-Cutting
    Concerns" — *Transactionality*).

    Attributes:
        kind: One of ``{'document_revision', 'region_occurrence',
            'finding_revision', 'recommendation_revision', 'decision',
            'trail_revision', 'plan_revision'}`` per Requirement 10.1
            and the slice's pipeline stages. ``'plan_revision'`` is the
            additive Slice 2 value used when the Plan Approval
            Provenance Manifest records the target Plan Revision as a
            material source (second-walking-slice design
            §"Planning_Service.PlanApprovals").
        resource_id: Resource Identity of the included source.
        revision_id: Revision Identity (or ``None`` for kinds that have
            no separate Revision Identity, e.g. an Immutable Record-
            shaped Decision).
        recorded_at: When the source was recorded, as a timezone-aware
            UTC :class:`datetime.datetime`. Used for the Source
            Freshness Window check; serialized to ISO-8601 millisecond
            text inside ``included_sources_json``.
    """

    kind: Literal[
        "document_revision",
        "region_occurrence",
        "finding_revision",
        "recommendation_revision",
        "decision",
        "trail_revision",
        "plan_revision",
    ]
    resource_id: str
    revision_id: Optional[str]
    recorded_at: datetime


@dataclass(frozen=True)
class OmissionEntry:
    """One row in ``Omission_Entries`` for a Provenance Manifest.

    An Omission Entry records that a material source was deliberately
    excluded from the synthesis (Requirement 10.2) or that a material
    source falls into one of the four "incomplete" categories
    {unavailable, restricted, stale, unresolved} (Requirement 10.3).
    The latter four categories cause the manifest's ``is_complete``
    column to be set to ``0`` until the entry is resolved by a later
    revision (manifests are append-only, AD-WS-4, so resolution is
    expressed by appending a new manifest rather than mutating this
    row).

    Frozen so the verify-then-write loop in
    :meth:`ProvenanceManifestWriter.write_manifest` cannot observe a
    different list than it just validated (design §"Cross-Cutting
    Concerns" — *Transactionality*).

    Attributes:
        excluded_source_id: Resource Identity of the omitted source.
        excluded_source_revision_id: Revision Identity of the omitted
            source when known; ``None`` when only the Resource
            Identity is known (Requirement 10.2: "the excluded source
            Revision Identity *when known*").
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
class ManifestWriteResult:
    """Result of :meth:`ProvenanceManifestWriter.write_manifest`.

    Returned so callers can correlate the manifest with its subject
    record and audit row in one round-trip without a second query.
    ``omission_entry_ids`` preserves the iteration order of the
    ``omissions`` argument so callers may zip the IDs back to the
    submitted entries.

    Attributes:
        manifest_id: The Provenance Manifest Identity (a fresh UUIDv7
            from :class:`IdentityService`).
        is_complete: ``True`` when ``Provenance_Manifests.is_complete``
            was written as ``1`` (no non-intentional unresolved
            omissions); ``False`` when written as ``0`` (Requirement
            10.3).
        omission_entry_ids: ``omission_entry_id`` values in the same
            order as the input ``omissions`` sequence.
        recorded_at: The ISO-8601 millisecond timestamp written to
            both ``Provenance_Manifests.recorded_at`` and every
            corresponding ``Omission_Entries.recorded_at`` row.
    """

    manifest_id: str
    is_complete: bool
    omission_entry_ids: tuple[str, ...]
    recorded_at: str


# ---------------------------------------------------------------------------
# Writer.
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceManifestWriter:
    """Insert ``Provenance_Manifests`` and ``Omission_Entries`` rows.

    The writer is a thin, dependency-injected helper rather than a
    free function so tests can substitute a deterministic
    :class:`~walking_slice.clock.Clock` and so the
    :class:`IdentityService` used to mint ``manifest_id`` values can
    share its in-process registry with the rest of the originating
    transaction (preserving the AD-WS-5 invariant that every artifact
    of one logical write shares one snapshot of issued identifiers).

    Instances are cheap to construct and safe to share across requests
    — every method takes the caller's connection and per-request
    arguments explicitly.

    Args:
        clock: Default source of ``recorded_at`` when the caller does
            not pass an explicit ``recorded_at`` to
            :meth:`write_manifest`.
        identity_service: Source of ``manifest_id`` UUIDv7 values per
            AD-WS-2. ``omission_entry_id`` values are minted from
            :func:`uuid_utils.uuid7` directly, matching the pattern
            used by :class:`AuditLog` for ``audit_record_id`` — these
            are row identifiers, not managed Resource Identities, so
            they do not consume ``Identifier_Registry`` rows.
    """

    clock: Clock
    identity_service: IdentityService

    def write_manifest(
        self,
        connection: Connection,
        *,
        subject_kind: Literal[
            "finding_revision",
            "recommendation_revision",
            "decision",
            "trail_revision",
            "plan_approval",
        ],
        subject_id: str,
        subject_revision_id: Optional[str],
        authoring_party_id: str,
        included_sources: Sequence[IncludedSource],
        omissions: Sequence[OmissionEntry] = (),
        freshness_window_seconds: int = DEFAULT_FRESHNESS_WINDOW_SECONDS,
        recorded_at: Optional[datetime] = None,
    ) -> ManifestWriteResult:
        """Persist one Provenance Manifest and its Omission Entries.

        The write order inside the caller's transaction is:

        1. Validate the manifest envelope (``subject_kind``,
           ``subject_id``, ``authoring_party_id``) against Requirement
           10.x and the schema CHECK constraints.
        2. Validate every :class:`IncludedSource` and every
           :class:`OmissionEntry` against the same rules. The first
           violating entry surfaces a :class:`ManifestValidationError`
           whose ``failed_constraint`` names the specific violation
           and (for omissions) the zero-based index of the offending
           entry.
        3. Enforce the Source Freshness Window: every
           :class:`IncludedSource` whose ``recorded_at`` is older than
           ``freshness_window_seconds`` before the manifest's
           ``recorded_at`` must be paired with an
           :class:`OmissionEntry` of category ``'stale'`` carrying the
           same ``excluded_source_id`` (and matching
           ``excluded_source_revision_id`` when supplied on the
           source); otherwise :class:`StalenessError` is raised
           (Requirement 10.6).
        4. Compute ``is_complete = 0`` when any supplied
           :class:`OmissionEntry` has a non-intentional category
           (Requirement 10.3 / design §"Persistence Invariants
           Summary" item 9). Newly inserted entries have
           ``resolved_at = NULL`` so the unresolved-check reduces to
           an iteration over the supplied categories.
        5. Mint ``manifest_id`` from :attr:`identity_service` and
           INSERT one ``Provenance_Manifests`` row. The
           ``included_sources_json`` column receives the JSON array
           described in Requirement 10.1 with each Source's
           ``recorded_at`` serialized to ISO-8601 millisecond text.
        6. INSERT one ``Omission_Entries`` row per supplied entry,
           reusing the manifest's ``recorded_at`` as the entry's
           ``recorded_at`` (Requirement 10.2 — both share the
           originating write's timestamp). ``resolved_at`` is left
           ``NULL`` because newly recorded entries are by definition
           unresolved.

        The connection is *not* committed by this method; the caller's
        ``engine.begin()`` block is responsible for the commit or the
        rollback. An INSERT failure raises through the connection so
        the originating transaction (Decision, Finding, Recommendation,
        or Trail Revision finalization) rolls back per Requirements
        2.7 / 10.6 / 13.6.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. The manifest and every omission row are
                INSERTed inside this transaction per AD-WS-5.
            subject_kind: One of ``{'finding_revision',
                'recommendation_revision', 'decision',
                'trail_revision'}``.
            subject_id: Identity of the subject (Finding Resource,
                Recommendation Resource, Decision Immutable Record, or
                Trail Resource).
            subject_revision_id: Revision Identity of the subject when
                applicable; ``None`` for ``'decision'`` because a
                Decision Immutable Record has no Revision Identity
                (AD-WS-3, AD-WS-4).
            authoring_party_id: The Party that authored the synthesis.
                FK-referenced by the ``Parties`` table.
            included_sources: Material sources that contributed to the
                synthesis. May be empty when the synthesis truly cites
                no source (e.g. a hypothesis Finding with no Region
                Occurrences). Each entry must satisfy the freshness
                check unless an aligned ``'stale'`` Omission Entry is
                supplied.
            omissions: Sources excluded from or unresolved for the
                synthesis. Defaults to the empty tuple. Iteration
                order is preserved on the returned
                ``omission_entry_ids``.
            freshness_window_seconds: Source Freshness Window applied
                to every Included Source. Defaults to 24 hours per
                Requirement 10.6; callers wanting a non-default window
                (e.g. an automation that only refreshes daily) pass
                their own value.
            recorded_at: Manifest's ``recorded_at`` as a
                timezone-aware UTC datetime. When omitted, the
                injected :attr:`clock` supplies the value.

        Returns:
            :class:`ManifestWriteResult` carrying the manifest
            identity, the computed completeness flag, the per-entry
            ``omission_entry_id`` values in submission order, and the
            ISO-8601 millisecond timestamp written to the row.

        Raises:
            ManifestValidationError: An envelope, Included Source, or
                Omission Entry fails Requirement 10.x validation.
            StalenessError: An Included Source falls outside the
                Source Freshness Window and is not paired with a
                ``'stale'`` Omission Entry (Requirement 10.6).
        """
        # 1. Resolve recorded_at and validate the envelope.
        manifest_recorded_at = self._resolve_recorded_at(recorded_at)
        manifest_recorded_at_iso = format_iso8601_ms(manifest_recorded_at)
        self._validate_envelope(
            subject_kind=subject_kind,
            subject_id=subject_id,
            authoring_party_id=authoring_party_id,
            freshness_window_seconds=freshness_window_seconds,
        )

        # 2. Validate every Included Source and Omission Entry. Both
        # loops run before any database round-trip so a validation
        # failure cannot leave the caller's transaction with a partial
        # manifest.
        included_tuple = tuple(included_sources)
        omissions_tuple = tuple(omissions)
        for index, source in enumerate(included_tuple):
            self._validate_included_source(source, index=index)
        for index, entry in enumerate(omissions_tuple):
            self._validate_omission_entry(entry, index=index)

        # 3. Enforce the Source Freshness Window. The check needs both
        # collections (Included Sources to scan; Omissions to look up
        # acknowledgments) so it runs after individual validation but
        # before any INSERT.
        self._enforce_freshness_window(
            included_sources=included_tuple,
            omissions=omissions_tuple,
            manifest_recorded_at=manifest_recorded_at,
            manifest_recorded_at_iso=manifest_recorded_at_iso,
            freshness_window_seconds=freshness_window_seconds,
        )

        # 4. Compute is_complete.
        is_complete = self._compute_is_complete(omissions_tuple)

        # 5. Mint manifest_id and INSERT the manifest row.
        manifest_id = str(self.identity_service.new_manifest_id())
        included_sources_json = json.dumps(
            [
                {
                    "kind": source.kind,
                    "resource_id": source.resource_id,
                    "revision_id": source.revision_id,
                    "recorded_at": format_iso8601_ms(source.recorded_at),
                }
                for source in included_tuple
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
                    :subject_revision_id, :authoring_party_id,
                    :recorded_at, :included_sources_json, :is_complete
                )
                """
            ),
            {
                "manifest_id": manifest_id,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
                "subject_revision_id": subject_revision_id,
                "authoring_party_id": authoring_party_id,
                "recorded_at": manifest_recorded_at_iso,
                "included_sources_json": included_sources_json,
                "is_complete": 1 if is_complete else 0,
            },
        )

        # 6. INSERT one Omission_Entries row per supplied entry.
        omission_entry_ids: list[str] = []
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
                    "authoring_party_id": authoring_party_id,
                    "recorded_at": manifest_recorded_at_iso,
                },
            )
            omission_entry_ids.append(omission_entry_id)

        return ManifestWriteResult(
            manifest_id=manifest_id,
            is_complete=is_complete,
            omission_entry_ids=tuple(omission_entry_ids),
            recorded_at=manifest_recorded_at_iso,
        )

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    def _resolve_recorded_at(
        self, recorded_at: Optional[datetime]
    ) -> datetime:
        """Return the manifest's ``recorded_at`` as a UTC ms-precision datetime.

        Precedence: an explicit ``recorded_at`` wins over the injected
        clock. Both paths are normalized to UTC and truncated to
        millisecond precision so the value persisted to
        ``Provenance_Manifests.recorded_at`` round-trips through the
        millisecond storage contract (Requirements 13.1, 6.2).
        """
        if recorded_at is not None:
            return truncate_to_milliseconds(recorded_at)
        return truncate_to_milliseconds(self.clock.now())

    @staticmethod
    def _validate_envelope(
        *,
        subject_kind: str,
        subject_id: str,
        authoring_party_id: str,
        freshness_window_seconds: int,
    ) -> None:
        """Reject malformed manifest envelopes before any INSERT runs."""
        if subject_kind not in _SUBJECT_KINDS:
            raise ManifestValidationError(
                f"subject_kind {subject_kind!r} is not one of "
                f"{sorted(_SUBJECT_KINDS)!r}; Requirement 10.1 names the "
                "four permitted syntheses.",
                failed_constraint="subject_kind_invalid",
            )
        if not subject_id:
            raise ManifestValidationError(
                "subject_id is required.",
                failed_constraint="subject_id_missing",
            )
        if not authoring_party_id:
            raise ManifestValidationError(
                "authoring_party_id is required (Requirement 10.2).",
                failed_constraint="authoring_party_id_missing",
            )
        if (
            not isinstance(freshness_window_seconds, int)
            or isinstance(freshness_window_seconds, bool)
            or freshness_window_seconds <= 0
        ):
            raise ManifestValidationError(
                f"freshness_window_seconds {freshness_window_seconds!r} "
                "must be a positive int (Requirement 10.6).",
                failed_constraint="freshness_window_invalid",
            )

    @staticmethod
    def _validate_included_source(
        source: IncludedSource, *, index: int
    ) -> None:
        """Reject malformed :class:`IncludedSource` entries."""
        if source.kind not in _INCLUDED_SOURCE_KINDS:
            raise ManifestValidationError(
                f"included_sources[{index}].kind {source.kind!r} is not "
                f"one of {sorted(_INCLUDED_SOURCE_KINDS)!r}.",
                failed_constraint="included_source_kind_invalid",
            )
        if not source.resource_id:
            raise ManifestValidationError(
                f"included_sources[{index}].resource_id is required.",
                failed_constraint="included_source_resource_id_missing",
            )
        if not isinstance(source.recorded_at, datetime):
            raise ManifestValidationError(
                f"included_sources[{index}].recorded_at must be a "
                "timezone-aware datetime.",
                failed_constraint="included_source_recorded_at_missing",
            )
        if source.recorded_at.tzinfo is None:
            raise ManifestValidationError(
                f"included_sources[{index}].recorded_at must be "
                "timezone-aware; received a naive datetime.",
                failed_constraint="included_source_recorded_at_missing",
            )

    @staticmethod
    def _validate_omission_entry(
        entry: OmissionEntry, *, index: int
    ) -> None:
        """Reject malformed :class:`OmissionEntry` instances.

        Each entry must carry a non-empty ``excluded_source_id``, one
        of the five permitted ``category`` values (mirroring the
        schema CHECK on ``Omission_Entries.category``), and a non-
        empty rationale of at most 2,000 characters (Requirement 10.2
        and the schema column ``NOT NULL`` constraint). The first
        violating entry surfaces a constraint name that includes its
        zero-based index so the HTTP layer can point the caller at
        the specific entry.
        """
        if not entry.excluded_source_id:
            raise ManifestValidationError(
                f"omissions[{index}].excluded_source_id is required.",
                failed_constraint="omission_excluded_source_id_missing",
            )
        if entry.category not in _OMISSION_CATEGORIES:
            raise ManifestValidationError(
                f"omissions[{index}].category {entry.category!r} is not "
                f"one of {sorted(_OMISSION_CATEGORIES)!r}; Requirement "
                "10.3 names the five permitted categories.",
                failed_constraint="omission_category_invalid",
            )
        if not isinstance(entry.rationale, str) or entry.rationale == "":
            raise ManifestValidationError(
                f"omissions[{index}].rationale is empty; Requirement 10.2 "
                f"requires {_OMISSION_RATIONALE_MIN_CHARS}.."
                f"{_OMISSION_RATIONALE_MAX_CHARS} characters.",
                failed_constraint="omission_rationale_missing",
            )
        if len(entry.rationale) > _OMISSION_RATIONALE_MAX_CHARS:
            raise ManifestValidationError(
                f"omissions[{index}].rationale length "
                f"{len(entry.rationale)} exceeds the "
                f"{_OMISSION_RATIONALE_MAX_CHARS}-character limit "
                "imposed by Requirement 10.2.",
                failed_constraint="omission_rationale_too_long",
            )

    @staticmethod
    def _enforce_freshness_window(
        *,
        included_sources: Sequence[IncludedSource],
        omissions: Sequence[OmissionEntry],
        manifest_recorded_at: datetime,
        manifest_recorded_at_iso: str,
        freshness_window_seconds: int,
    ) -> None:
        """Reject Included Sources outside the Source Freshness Window.

        Per Requirement 10.6 the default window is 24 hours; callers
        may pass a different positive ``freshness_window_seconds`` to
        widen or tighten the check. A source whose ``recorded_at`` is
        older than the window may still appear in ``included_sources``
        when an aligned Omission Entry with category ``'stale'``
        explicitly acknowledges the staleness (matched on
        ``excluded_source_id`` and, when present on the source,
        ``excluded_source_revision_id``). Otherwise
        :class:`StalenessError` is raised before any INSERT runs.

        The aligned-acknowledgment rule is the literal reading of the
        task description: *"Stale source rejected unless marked as
        'stale' omission."* It lets a caller carry a stale source in
        the synthesis (e.g. because no fresher version exists yet)
        while still declaring the staleness on the same manifest, so
        downstream readers see both the included source and the
        recorded-stale acknowledgment.
        """
        if not included_sources:
            return
        window = timedelta(seconds=freshness_window_seconds)
        # Build the set of acknowledged stale sources once. The match
        # is on ``(excluded_source_id, excluded_source_revision_id)``
        # but with the revision pair allowed to be ``None`` on either
        # side — a stale Omission Entry that omits the revision still
        # covers any Included Source whose Resource Identity matches,
        # because Requirement 10.2 makes the Revision Identity
        # optional ("when known").
        acknowledged: set[tuple[str, Optional[str]]] = set()
        for entry in omissions:
            if entry.category != "stale":
                continue
            acknowledged.add(
                (entry.excluded_source_id, entry.excluded_source_revision_id)
            )
        for source in included_sources:
            source_recorded = truncate_to_milliseconds(source.recorded_at)
            age = manifest_recorded_at - source_recorded
            if age <= window:
                continue
            if _is_stale_acknowledged(source, acknowledged):
                continue
            raise StalenessError(
                excluded_source_id=source.resource_id,
                excluded_source_revision_id=source.revision_id,
                source_recorded_at=format_iso8601_ms(source.recorded_at),
                manifest_recorded_at=manifest_recorded_at_iso,
                freshness_window_seconds=freshness_window_seconds,
            )

    @staticmethod
    def _compute_is_complete(omissions: Sequence[OmissionEntry]) -> bool:
        """Compute ``Provenance_Manifests.is_complete`` from the omissions.

        Per Requirement 10.3 and design §"Persistence Invariants
        Summary" item 9, a manifest is incomplete when any unresolved
        Omission Entry has a non-intentional category (``unavailable``,
        ``restricted``, ``stale``, ``unresolved``). Newly inserted
        entries have ``resolved_at = NULL`` (unresolved) so the check
        reduces to "are any of the supplied entries in a non-
        intentional category?".
        """
        for entry in omissions:
            if entry.category != "intentional":
                return False
        return True


def _is_stale_acknowledged(
    source: IncludedSource,
    acknowledged: set[tuple[str, Optional[str]]],
) -> bool:
    """Return ``True`` when *source* is covered by a ``'stale'`` Omission.

    The match accepts three shapes so a caller is not forced to repeat
    a Revision Identity on the Omission Entry when the Included Source
    already carries it (Requirement 10.2: the excluded source Revision
    Identity is optional, "when known"):

    1. ``(resource_id, revision_id)`` matches exactly.
    2. ``(resource_id, None)`` matches — the Omission Entry omitted
       the Revision Identity, which the slice reads as "this Resource
       is stale in every revision the synthesis would have considered".
    3. ``(resource_id, source.revision_id)`` matches — the Omission
       Entry pinpoints the specific Revision the Included Source
       refers to.
    """
    return (
        (source.resource_id, source.revision_id) in acknowledged
        or (source.resource_id, None) in acknowledged
    )
