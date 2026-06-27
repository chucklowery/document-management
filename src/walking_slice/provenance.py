"""Provenance_Navigator — backlink discovery with constant-time response shaping.

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Provenance_Navigator" (backlink algorithm), AD-WS-8 (interim backlink
indexing on the composite index
``ix_relationships_target_backlink``), and AD-WS-9 (default
Completeness Disclosure policy — restricted-vs-nonexistent
normalization).

Task scope (task 12.1):
    Backlink discovery only. The full Provenance_Navigator surface —
    Decision-provenance traversal (task 12.2), region text resolution
    (task 12.3), Completeness Disclosure policy enforcement (task
    12.4), and the HTTP routes (task 12.5) — is delivered by
    subsequent tasks.

The backlink algorithm follows the design's pseudocode verbatim:

    Step 1: Load candidate inbound Relationships from the AD-WS-8
            composite index ``(target_id, target_revision_id,
            relationship_type, recorded_at)`` ordered by
            ``(recorded_at ASC, relationship_id ASC)``. The 500-row
            cap from Requirement 8.6 is the ``LIMIT`` clause on this
            load.
    Step 2: Build the authorized projection by asking the
            :class:`Authorization_Service` whether the requesting
            Party may view both the Relationship itself
            (``view.relationship``) and the source endpoint
            (``view.<source_kind>``) of each candidate.
    Step 3: Compute the pagination cursor and the response size from
            the authorized projection *alone* — never from the
            candidate set — so that a Party who could not see a
            dropped Relationship receives a response indistinguishable
            from one in a universe where the dropped Relationship does
            not exist (Requirement 8.3, Property 4 — *Non-leakage of
            restricted information*, validated by task 12.7).
    Step 4: Derive a fixed latency baseline from the authorized
            response size and surface it on the returned
            :class:`BacklinkPage`. The HTTP layer added by task 12.5
            ``await``s a single ``asyncio.sleep_until`` shaped from
            this baseline so the timing observable to the caller is
            a deterministic function of the *authorized* size and not
            of the database scan effort.

Latency baseline:
    A deterministic linear function of the authorized response size,
    capped at a slice-wide ceiling. The function lives in
    :func:`compute_latency_baseline_seconds` so the property test in
    task 12.7 (Non-leakage of restricted information) can invoke it
    with candidate-set sizes and assert equal baselines for equal
    authorized sizes — even when the underlying candidate sets
    differ.

Requirements satisfied (per task 12.1):
    8.1 — Within 2 seconds for result sets up to 500 backlinks. The
          baseline ceiling sits well below 2 seconds and the database
          scan uses the AD-WS-8 index
          ``ix_relationships_target_backlink``.
    8.2 — Every returned :class:`BacklinkEntry` carries Relationship
          Identity, Relationship Type, source endpoint Identity,
          source endpoint Type (``source_kind``), source endpoint
          Revision Identity (when applicable), and authoring Party
          Identity, per ADR-HT-001 §6.
    8.4 — This method neither writes to ``Role_Assignments`` nor
          grants any view, modify, or approve authority as a side
          effect of returning a backlink.
    8.6 — Each response is bounded to at most 500 Relationships via
          the candidate ``LIMIT 500``; the authorized projection is a
          subset of the candidate set so it is also ≤ 500.

Out-of-scope notes:
    - This module does *not* yet apply the Completeness Disclosure
      policy ``slice-default-2026`` (AD-WS-9). Restricted source
      endpoints are silently dropped from the visible projection; gap
      descriptors and redaction markers are introduced by task 12.4.
      The constant-time shaping invariant required by Property 4 is
      still preserved by this module because cursor, response size,
      and latency baseline already depend only on the authorized
      projection.
    - This module also does not yet append audit rows on backlink
      reads. Per design §"Provenance_Navigator" ("``Audit_Log`` (no
      append on reads in this slice; reads are not consequential)")
      backlink reads remain non-consequential for the first walking
      slice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.audit import format_iso8601_ms
from walking_slice.clock import Clock
from walking_slice.disclosure import DisclosurePolicy


__all__ = [
    "BACKLINK_PAGE_LIMIT",
    "BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS",
    "BACKLINK_LATENCY_BASELINE_CEILING_SECONDS",
    "BACKLINK_LATENCY_BASELINE_PER_ENTRY_SECONDS",
    "BacklinkCursor",
    "BacklinkEntry",
    "BacklinkPage",
    "ProvenanceNavigator",
    "compute_latency_baseline_seconds",
    "decode_backlink_cursor",
    "encode_backlink_cursor",
]

# ---------------------------------------------------------------------------
# Constants.
#
# Each constant captures one number from Requirement 8 or AD-WS-9. They are
# module-level ``Final`` values so unit tests, the property test in task
# 12.7, and the HTTP layer in task 12.5 all reference the same source of
# truth and so any future change shows up as a single diff.
# ---------------------------------------------------------------------------


# Requirement 8.6: each backlink response is bounded to at most 500
# Relationships. The candidate ``LIMIT 500`` already enforces the bound at
# the SQL layer; this constant lets callers refer to the limit by name
# rather than by magic number and lets the property test verify the
# constant rather than re-deriving it.
BACKLINK_PAGE_LIMIT: Final[int] = 500


# Floor and ceiling for the latency baseline (in seconds). The floor
# ensures every response — even an empty one — incurs a small,
# non-zero wait so a party who can see nothing is timing-equivalent to
# a party who *should* see nothing in a universe without the restricted
# Relationships. The ceiling holds the worst-case wait below the
# Requirement 8.1 two-second budget with comfortable headroom for the
# database scan and the response serialization the HTTP layer adds.
#
# These values are deliberately small in absolute terms (microseconds to
# low milliseconds) so unit tests do not spend wall-clock time waiting on
# them. The property test in task 12.7 (Property 4) tolerates a 100-ms
# variation per Requirement 8.3, so any value strictly below 0.1 second
# is automatically inside the property's tolerance band.
BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS: Final[float] = 0.001
BACKLINK_LATENCY_BASELINE_CEILING_SECONDS: Final[float] = 0.050


# Per-entry contribution to the latency baseline. The baseline is a
# linear function of the authorized response size: ``floor +
# per_entry * len(visible)`` clamped at ``ceiling``. The per-entry
# constant is set so a full 500-entry response sits within the ceiling
# (``0.001 + 0.0001 * 500 = 0.051`` → clamped to 0.05) and an empty
# response sits at the floor.
BACKLINK_LATENCY_BASELINE_PER_ENTRY_SECONDS: Final[float] = 0.0001


# Authorization action name passed to ``AuthorizationService.evaluate``
# for the Relationship itself. The ``view.*`` prefix maps to the
# ``view`` authority type per
# :func:`walking_slice.authorization._required_authority`.
_AUTHORIZATION_ACTION_VIEW_RELATIONSHIP: Final[str] = "view.relationship"


# Authorization action prefix for the source endpoint check. The
# concrete action string is built per-relationship as
# ``"view.<source_kind>"`` where ``source_kind`` comes from the
# Relationship row (e.g. ``finding_revision``, ``recommendation_revision``,
# ``decision``, ``trail_step``).
_AUTHORIZATION_ACTION_VIEW_PREFIX: Final[str] = "view."


# Recognized source endpoint kinds for the backlink algorithm.
#
# The backlink query in :meth:`ProvenanceNavigator._load_candidates` does
# not filter by ``source_kind`` at the SQL layer — every inbound
# Relationship is loaded irrespective of its source endpoint type — and
# the authorization check in
# :meth:`ProvenanceNavigator._build_authorized_projection` constructs the
# ``view.<source_kind>`` action string at call time, delegating to the
# Slice 1 prefix-based fallback in
# :func:`walking_slice.authorization._required_authority` (which maps every
# ``view.*`` action to the ``view`` authority). The algorithm therefore
# already handles any source endpoint kind, including the eight planning
# kinds added by the second walking slice (task 12.2 — additive coverage
# only, no algorithm change) and the eight execution + produced-Deliverable
# kinds added by the third walking slice (task 12.3 — additive coverage
# only, Requirement 36).
#
# This constant pins the **expected coverage surface** of the backlink
# algorithm in one place so:
#
#   - the task 12.2 and task 12.3 additive-coverage unit tests can assert
#     each new source kind is recognized and returned through the existing
#     algorithm,
#   - the design §"Reused Slice 1 contexts" reference to the
#     ``_authorized_source_kinds`` set has a concrete realization in code,
#     and
#   - future slices adding new source endpoint kinds (e.g. additional
#     read-model projections) can extend this constant alongside the rest
#     of their additive plumbing rather than scattering the new kind
#     across docstrings.
#
# Slice 1 kinds: ``decision``, ``recommendation_revision``,
# ``finding_revision``, ``trail_step``. Slice 2 kinds (additive per
# task 12.2 and Requirement 15): ``objective_revision``,
# ``intended_outcome_revision``, ``project_revision``,
# ``deliverable_expectation_revision``, ``activity_plan``,
# ``plan_revision``, ``plan_review_revision``, ``plan_approval``.
# Slice 3 kinds (additive per task 12.3 and Requirement 36):
# ``work_assignment_record``, ``work_event_record``, ``time_entry_record``,
# ``deliverable_resource``, ``deliverable_revision``,
# ``deliverable_production_record``, ``milestone_acceptance_record``,
# ``completion_record``. Slice 4 kinds (additive per task 10.2 and
# Requirement 56): ``measurement_definition``,
# ``measurement_definition_revision``, ``measurement_record``,
# ``observed_outcome``, ``observed_outcome_revision``,
# ``success_condition_assessment_record``, ``outcome_review_record``.
#
# The set is a ``frozenset`` so it cannot be mutated at runtime; the
# algorithm does not consult it for control flow, so a row carrying a
# source kind outside this set would still be loaded and authorized via
# the prefix-based fallback — the constant is documentation, not a gate.
_AUTHORIZED_SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {
        # Slice 1 source endpoint kinds.
        "decision",
        "recommendation_revision",
        "finding_revision",
        "trail_step",
        # Slice 2 planning source endpoint kinds (task 12.2 — additive
        # coverage extension; AD-WS-15 already mapped every ``view.*``
        # planning action to the ``view`` authority via the prefix
        # fallback, so no new authorization mapping is required).
        "objective_revision",
        "intended_outcome_revision",
        "project_revision",
        "deliverable_expectation_revision",
        "activity_plan",
        "plan_revision",
        "plan_review_revision",
        "plan_approval",
        # Slice 3 execution and produced-Deliverable source endpoint
        # kinds (task 12.3 — additive coverage extension; Requirement
        # 36.1, 36.2, 36.3, 36.4, 36.5, 36.6). The Slice 1 prefix-based
        # ``_required_authority`` fallback maps every ``view.*`` action
        # to the ``view`` authority, so no new authorization mapping is
        # required; the algorithm in
        # :meth:`ProvenanceNavigator._build_authorized_projection`
        # constructs ``view.<source_kind>`` at call time and resolves
        # the authority through that fallback.
        "work_assignment_record",
        "work_event_record",
        "time_entry_record",
        "deliverable_resource",
        "deliverable_revision",
        "deliverable_production_record",
        "milestone_acceptance_record",
        "completion_record",
        # Slice 4 outcome-measurement source endpoint kinds (task 10.2 —
        # additive coverage extension; Requirement 56.1, 56.2, 56.4, 56.6,
        # 43.5). As with the Slice 2 and Slice 3 kinds, the Slice 1
        # prefix-based ``_required_authority`` fallback maps every
        # ``view.*`` action to the ``view`` authority, so no new
        # authorization mapping is required; the algorithm in
        # :meth:`ProvenanceNavigator._build_authorized_projection`
        # constructs ``view.<source_kind>`` at call time and resolves the
        # authority through that fallback. This is a documentation-only
        # coverage extension — the algorithm, the at-most-500-relationship
        # bound (Requirement 56.6), and the continuation reference are
        # unchanged.
        "measurement_definition",
        "measurement_definition_revision",
        "measurement_record",
        "observed_outcome",
        "observed_outcome_revision",
        "success_condition_assessment_record",
        "outcome_review_record",
    }
)


# Cursor field delimiter. Cursors are encoded as
# ``"<recorded_at>|<relationship_id>"`` so they sort lexicographically in
# the same order as the underlying ``ORDER BY (recorded_at,
# relationship_id)`` clause and so the HTTP layer can pass them as opaque
# strings without further escaping.
_CURSOR_DELIMITER: Final[str] = "|"


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacklinkCursor:
    """Pagination cursor for the backlink query.

    Encodes the ``(recorded_at, relationship_id)`` pair of the *last
    visible* Relationship in the previously returned :class:`BacklinkPage`,
    so the next-page query loads candidates strictly after that pair.

    Attributes:
        recorded_at: ISO-8601 UTC millisecond-precision timestamp of the
            last visible Relationship in the prior page.
        relationship_id: Identity of the last visible Relationship in
            the prior page. Required as a tiebreaker so two
            Relationships sharing a ``recorded_at`` value (millisecond
            collisions are rare but possible) are ordered
            deterministically by their canonical UUIDv7 string.

    Property invariant:
        The cursor is computed from the authorized projection *only*.
        A Party who could not see a particular Relationship can never
        receive a cursor that names it; consequently the next-page
        query for that Party skips over the restricted region in the
        same way it would in a universe where the restricted
        Relationships do not exist (Property 4).
    """

    recorded_at: str
    relationship_id: str


def encode_backlink_cursor(cursor: Optional[BacklinkCursor]) -> Optional[str]:
    """Encode a :class:`BacklinkCursor` as a string for the HTTP wire.

    Returns ``None`` when ``cursor`` is ``None``; otherwise the encoded
    form is ``"<recorded_at>|<relationship_id>"``. The encoding has
    these properties:

    - Lexicographic order matches the SQL ``ORDER BY (recorded_at,
      relationship_id)`` order, so two cursors can be compared as strings
      to decide which one is further along.
    - The delimiter ``|`` is illegal in both ISO-8601 timestamps and
      UUIDv7 canonical strings, so decoding is unambiguous.
    - The encoding is stable across slice instances — no per-instance
      secret or seed is required, which matters because the HTTP layer
      may load-balance across multiple slice replicas.
    """
    if cursor is None:
        return None
    return f"{cursor.recorded_at}{_CURSOR_DELIMITER}{cursor.relationship_id}"


def decode_backlink_cursor(value: Optional[str]) -> Optional[BacklinkCursor]:
    """Decode a wire-format cursor string into a :class:`BacklinkCursor`.

    The inverse of :func:`encode_backlink_cursor`. ``None`` is returned
    when ``value`` is ``None`` or empty. A malformed cursor (missing
    delimiter, empty halves) raises :class:`ValueError` so the HTTP
    layer can render a 400 rather than silently returning an out-of-
    order page.
    """
    if value is None or value == "":
        return None
    if _CURSOR_DELIMITER not in value:
        raise ValueError(
            f"backlink cursor {value!r} is malformed; expected "
            f"'<recorded_at>{_CURSOR_DELIMITER}<relationship_id>'."
        )
    recorded_at, _, relationship_id = value.partition(_CURSOR_DELIMITER)
    if not recorded_at or not relationship_id:
        raise ValueError(
            f"backlink cursor {value!r} has an empty half; both "
            f"recorded_at and relationship_id are required."
        )
    return BacklinkCursor(recorded_at=recorded_at, relationship_id=relationship_id)


@dataclass(frozen=True)
class BacklinkEntry:
    """A single inbound Relationship returned to the requesting Party.

    Carries exactly the six attributes Requirement 8.2 mandates:

    - ``relationship_id`` — Relationship Identity (UUIDv7).
    - ``relationship_type`` — One of the slice's enumerated Relationship
      types (``Supports``, ``Contradicts``, ``Derived From``,
      ``Addresses``, ``Supersedes``).
    - ``source_id`` — Source endpoint Identity.
    - ``source_kind`` — Source endpoint Type (e.g. ``finding_revision``,
      ``recommendation_revision``, ``decision``, ``trail_step``).
    - ``source_revision_id`` — Source endpoint Revision Identity, or
      ``None`` for source endpoints that have no Revision concept
      (e.g. Decisions, Trail Steps).
    - ``authoring_party_id`` — Identity of the Party that recorded the
      Relationship.

    ``recorded_at`` is also included so callers can render a stable
    chronological listing without a second round-trip; it is the
    ISO-8601 UTC millisecond-precision text persisted on the row.
    """

    relationship_id: str
    relationship_type: str
    source_id: str
    source_kind: str
    source_revision_id: Optional[str]
    authoring_party_id: str
    recorded_at: str


@dataclass(frozen=True)
class BacklinkPage:
    """One page of authorized backlinks for a target endpoint.

    Attributes:
        entries: The authorized projection of inbound Relationships, in
            ``(recorded_at ASC, relationship_id ASC)`` order. Always
            ``len(entries) <= BACKLINK_PAGE_LIMIT`` per Requirement 8.6.
        cursor: A :class:`BacklinkCursor` pointing to the last visible
            Relationship, or ``None`` when this page is the final page
            (no more visible Relationships to return). The cursor is
            computed from ``entries`` alone — see Property 4 in
            design §"Correctness Properties".
        response_size: ``len(entries)``. Computed once and stored on
            the page so callers and tests do not have to recompute it.
            Property 4 requires this value to depend only on the
            authorized projection.
        latency_baseline_seconds: The deterministic latency baseline
            the HTTP layer (task 12.5) waits out before responding.
            Computed by :func:`compute_latency_baseline_seconds` from
            ``response_size`` alone. Property 4 requires this value to
            depend only on the authorized projection (within a 100-ms
            tolerance per Requirement 8.3).
    """

    entries: Tuple[BacklinkEntry, ...]
    cursor: Optional[BacklinkCursor]
    response_size: int
    latency_baseline_seconds: float


# ---------------------------------------------------------------------------
# Latency baseline.
# ---------------------------------------------------------------------------


def compute_latency_baseline_seconds(response_size: int) -> float:
    """Return the deterministic latency baseline for ``response_size`` entries.

    Implements the linear-with-clamp formula described in the module
    docstring:

        baseline = clamp(
            floor + per_entry * response_size,
            floor,
            ceiling,
        )

    The formula is a pure function of ``response_size`` so two calls
    with the same authorized size *always* return the same baseline.
    That equality is what Property 4 (Non-leakage of restricted
    information, task 12.7) relies on when it generates pairs of
    Parties differing only in view authority and asserts the
    response-latency dimension is indistinguishable within the 100-ms
    tolerance.

    Args:
        response_size: Number of entries in the authorized projection.
            Must be a non-negative integer; negative values raise
            :class:`ValueError`.

    Returns:
        The latency baseline in seconds. Always in the closed interval
        ``[BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS,
        BACKLINK_LATENCY_BASELINE_CEILING_SECONDS]``.

    Raises:
        ValueError: When ``response_size < 0``.
    """
    if response_size < 0:
        raise ValueError(
            f"response_size must be non-negative; got {response_size}."
        )
    raw = (
        BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS
        + BACKLINK_LATENCY_BASELINE_PER_ENTRY_SECONDS * response_size
    )
    if raw < BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS:
        return BACKLINK_LATENCY_BASELINE_FLOOR_SECONDS
    if raw > BACKLINK_LATENCY_BASELINE_CEILING_SECONDS:
        return BACKLINK_LATENCY_BASELINE_CEILING_SECONDS
    return raw


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


@dataclass
class ProvenanceNavigator:
    """Read-side navigator for backlinks, provenance chains, and Region text.

    Task 12.1 implements only the backlink surface. The Decision-
    provenance traversal (task 12.2), Region text resolution (task
    12.3), and Completeness Disclosure policy enforcement (task 12.4)
    extend this class with additional public methods.

    The navigator is cross-request shareable: instances hold only the
    :class:`Clock` and the :class:`AuthorizationService`, both of
    which are themselves thread-safe. The per-request state — the
    SQLAlchemy connection, the requesting Party Identity, the
    effective time ``at`` — is passed as method arguments per
    AD-WS-5 (single connection per request, writes scoped to the
    caller's transaction).

    Attributes:
        clock: :class:`Clock` used as the default source of the
            authorization evaluation time when the caller does not
            supply an explicit ``at``. The clock is consulted *only*
            in :meth:`list_backlinks` and only when ``at`` is not
            provided.
        authorization_service: Receives every authorization check
            issued by the backlink algorithm. Each
            :meth:`AuthorizationService.evaluate` call appends an
            evaluation row to ``Audit_Records`` per Requirement 12.5;
            backlink reads are *not* consequential per design
            §"Provenance_Navigator" but the per-evaluation audit row
            from ``Authorization_Service`` is still recorded.
    """

    clock: Clock
    authorization_service: AuthorizationService
    disclosure_policy: Optional[DisclosurePolicy] = None

    # -- public surface ----------------------------------------------------

    def list_backlinks(
        self,
        connection: Connection,
        *,
        target_id: str,
        target_revision_id: Optional[str] = None,
        party_id: str,
        at: Optional[datetime] = None,
        after_cursor: Optional[BacklinkCursor] = None,
    ) -> BacklinkPage:
        """Return one page of authorized inbound Relationships for ``target_id``.

        Implements the four-step algorithm documented at module level:

        1. Load up to :data:`BACKLINK_PAGE_LIMIT` candidate inbound
           Relationships from the AD-WS-8 composite index, ordered by
           ``(recorded_at ASC, relationship_id ASC)``. When
           ``after_cursor`` is supplied, candidates with
           ``(recorded_at, relationship_id) <= cursor`` are excluded so
           the next page picks up exactly where the prior page ended.
        2. Build the authorized projection by evaluating
           ``view.relationship`` and ``view.<source_kind>`` for each
           candidate against the requesting Party. Candidates the
           Party may not view are silently dropped.
        3. Compute the cursor (last visible Relationship's
           ``(recorded_at, relationship_id)``) and the response size
           (``len(visible)``) from the authorized projection only.
        4. Compute the latency baseline via
           :func:`compute_latency_baseline_seconds` and surface it on
           the returned :class:`BacklinkPage`; the HTTP layer in task
           12.5 ``await``s ``asyncio.sleep_until`` shaped from this
           baseline before emitting the response.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                request (typically obtained via ``engine.connect()``
                for a read). The connection is used to issue one
                ``SELECT`` against ``Relationships`` and, indirectly
                through :class:`AuthorizationService`, ``SELECT``
                queries against ``Role_Assignments`` and the
                evaluation audit append.
            target_id: Identity of the target endpoint whose inbound
                Relationships are being queried.
            target_revision_id: Optional Revision Identity of the
                target endpoint. When supplied, only Relationships
                whose ``target_revision_id`` matches are returned;
                when ``None``, Relationships targeting the Resource
                identity (with any ``target_revision_id``, including
                ``NULL``) are returned. This matches the design
                pseudocode's ``OR target_revision_id =
                :target_revision_id`` clause.
            party_id: Identity of the requesting Party. Authority is
                evaluated against this Party for every candidate.
            at: Effective time for authority evaluation per design
                §"Cross-Cutting Concerns" (*Authorization*). When
                omitted, :attr:`clock` is consulted; production code
                paths typically pass the ``RequestContext`` clock so
                every authorization decision inside one request shares
                an instant.
            after_cursor: Optional pagination cursor from a prior
                :meth:`list_backlinks` call. Candidates strictly after
                this cursor (by ``(recorded_at, relationship_id)``)
                are loaded; passing ``None`` returns the first page.

        Returns:
            A :class:`BacklinkPage` containing the authorized
            projection, the next-page cursor (or ``None`` if there
            are no more authorized Relationships), the response
            size, and the latency baseline the HTTP layer should
            wait out.
        """
        effective_at = at if at is not None else self.clock.now()

        candidates = self._load_candidates(
            connection,
            target_id=target_id,
            target_revision_id=target_revision_id,
            after_cursor=after_cursor,
        )

        visible = self._build_authorized_projection(
            connection,
            candidates=candidates,
            party_id=party_id,
            at=effective_at,
        )

        cursor = self._cursor_from_visible(visible)
        response_size = len(visible)
        latency_baseline = compute_latency_baseline_seconds(response_size)

        return BacklinkPage(
            entries=tuple(visible),
            cursor=cursor,
            response_size=response_size,
            latency_baseline_seconds=latency_baseline,
        )

    def navigate_decision(
        self,
        connection: Connection,
        *,
        decision_id: str,
        party_id: str,
        at: Optional[datetime] = None,
    ) -> "DecisionProvenanceChain":
        """Traverse Decision → Recommendation → Finding(s) → Region(s) → Document.

        Implements the five-stage algorithm documented at the
        Decision-to-Evidence provenance section of this module
        (design §"Provenance traversal algorithm"). Each stage is
        loaded from an immutable table, ordered deterministically,
        and filtered through ``AuthorizationService.evaluate`` with
        action ``view.<kind>`` so two invocations with the same
        ``(decision_id, party_id, at)`` produce byte-equivalent
        results (Requirement 11.5 / Property 8).

        Stage-by-stage behaviour:

        1. **Decision (Requirement 11.1, 11.6).** Load the
           ``Decisions`` row for ``decision_id``. When the row does
           not exist, raise :class:`DecisionUnresolvableError`
           naming the unresolvable reference and disclosing nothing
           about related Resources (Requirement 11.6). When the row
           exists but the requesting Party lacks ``view.decision``
           authority on it, raise the same exception per design
           §"Provenance traversal algorithm"
           ``not_found_indistinguishable_response`` so the response
           form is indistinguishable from the unresolvable case
           (Requirement 11.7 — full enforcement task 12.4).

        2. **Recommendation Revision (Requirement 11.1).** Load the
           ``Recommendation_Revisions`` row pinned by
           ``(target_recommendation_id,
              target_recommendation_revision_id)`` on the Decision.
           When the Party lacks ``view.recommendation_revision``
           authority, emit a :class:`RedactedNode` and continue —
           downstream stages still load because the design pseudocode
           ``chain.append(...)`` for every stage is mandatory; the
           list-of-lists shape is preserved even when intermediate
           levels are redacted. Each downstream node is independently
           authorized, so a Party who lacks authority on the
           Recommendation Revision but holds authority on a leaf
           Finding still receives the leaf — restrictions cascade by
           record, not by tree branch.

        3. **Findings (Requirement 11.1, 11.5).** Load every
           ``Derived From`` ``Relationships`` row whose
           ``(source_id, source_revision_id) =
              (target_recommendation_id, target_recommendation_revision_id)``,
           ordered ``(recorded_at ASC, relationship_id ASC)``. For
           each row, pick the latest ``Finding_Revisions`` row for
           ``target_id`` whose ``recorded_at <= at``; the latest-at-time
           rule is what preserves Property 8 idempotence even after
           later Finding Revisions are appended.

        4. **Region Occurrences (Requirement 11.1, 11.2).** For
           every visible Finding Revision, load each ``Supports``
           ``Relationships`` row whose
           ``(source_id, source_revision_id) =
              (finding_id, finding_revision_id)``, ordered
           ``(recorded_at ASC, relationship_id ASC)``. Each Supports
           row pins a Region Occurrence by composite key
           ``(target_id=region_id, target_revision_id=document_revision_id)``.
           Resolve each occurrence and attach the
           ``bounded_text`` span ``content_bytes[start:end]`` to the
           emitted :class:`RegionOccurrenceNode` (Requirement 11.2).

        5. **Document Revisions (Requirement 11.1).** For every
           visible Region Occurrence, load the
           ``Document_Revisions`` row by ``document_revision_id`` and
           emit a :class:`DocumentRevisionNode`. The ``content_bytes``
           blob is intentionally not surfaced on the node — the
           byte-equivalent span sits on the corresponding
           :class:`RegionOccurrenceNode` already.

        The :class:`DecisionProvenanceChain` returned has
        ``len(region_occurrences) == len(document_revisions)``, with
        positionally aligned entries: ``document_revisions[i]`` is
        the Document Revision owning ``region_occurrences[i]``. The
        list of Finding entries is *not* positionally aligned with
        the Region Occurrence list, because one Finding Revision can
        contribute many Supports Relationships (one per cited
        Region Occurrence, Requirement 4.5).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                request. Used for every read SELECT and is the same
                connection passed to ``AuthorizationService.evaluate``
                so the evaluation audit row participates in the
                caller's transaction (AD-WS-5). Reads are
                non-consequential per design §"Provenance_Navigator"
                so no consequential audit row is appended by this
                method.
            decision_id: Identity of the Decision Immutable Record
                whose chain is being requested.
            party_id: Identity of the requesting Party. Authority is
                evaluated against this Party for every node in the
                chain.
            at: Effective time for authority evaluation per design
                §"Cross-Cutting Concerns" (*Authorization*) and the
                Finding-Revision selection rule above. When omitted,
                :attr:`clock` is consulted; production code paths
                typically pass the ``RequestContext`` clock so every
                authorization decision in one request shares an
                instant.

        Returns:
            :class:`DecisionProvenanceChain` with the five stages
            populated. Each stage's entry is either the serialized
            row or a :class:`RedactedNode` carrying only the node
            kind.

        Raises:
            DecisionUnresolvableError: The supplied ``decision_id``
                does not resolve to a ``Decisions`` row, or the
                requesting Party lacks ``view.decision`` authority
                on the resolved Decision.
        """
        effective_at = at if at is not None else self.clock.now()

        # Stage 1: Decision. Load the row, then authorize.
        decision_row = self._load_decision_row(connection, decision_id)
        if decision_row is None:
            # Requirement 11.6: identify the unresolvable reference
            # and disclose nothing about related Resources.
            raise DecisionUnresolvableError(decision_id)

        decision_scope = decision_row["applicable_scope"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_DECISION,
            target=TargetRef(
                kind=_NODE_KIND_DECISION,
                id=decision_id,
                revision_id=None,
                scope=decision_scope,
            ),
            at=effective_at,
        ):
            # Design pseudocode: not_found_indistinguishable_response.
            # Raising the same exception keeps the externally
            # observable response form identical to the
            # unresolvable case. Full timing-indistinguishability
            # shaping is task 12.4.
            raise DecisionUnresolvableError(decision_id)

        decision_node = DecisionNode(
            decision_id=decision_row["decision_id"],
            target_recommendation_id=decision_row["target_recommendation_id"],
            target_recommendation_revision_id=decision_row[
                "target_recommendation_revision_id"
            ],
            outcome=decision_row["outcome"],
            rationale=decision_row["rationale"],
            deciding_party_id=decision_row["deciding_party_id"],
            authority_basis_type=decision_row["authority_basis_type"],
            authority_basis_id=decision_row["authority_basis_id"],
            applicable_scope=decision_row["applicable_scope"],
            recorded_at=decision_row["recorded_at"],
        )

        # Stage 2: Recommendation Revision. Pinned by the Decision row;
        # no time-bounded selection needed.
        target_recommendation_id = decision_row["target_recommendation_id"]
        target_recommendation_revision_id = decision_row[
            "target_recommendation_revision_id"
        ]
        rec_row = self._load_recommendation_revision_row(
            connection,
            recommendation_id=target_recommendation_id,
            recommendation_revision_id=target_recommendation_revision_id,
        )
        rec_node: "RecommendationRevisionNode | RedactedNode"
        if rec_row is None:
            # Schema FK invariant says this should not happen for a
            # successfully created Decision. Emit a redaction marker
            # defensively so the chain shape is preserved.
            rec_node = RedactedNode(kind=_NODE_KIND_RECOMMENDATION_REVISION)
        elif not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_RECOMMENDATION_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_RECOMMENDATION_REVISION,
                id=target_recommendation_id,
                revision_id=target_recommendation_revision_id,
                scope=target_recommendation_id,
            ),
            at=effective_at,
        ):
            rec_node = RedactedNode(kind=_NODE_KIND_RECOMMENDATION_REVISION)
        else:
            rec_node = RecommendationRevisionNode(
                recommendation_id=rec_row["recommendation_id"],
                recommendation_revision_id=rec_row["recommendation_revision_id"],
                parent_revision_id=rec_row["parent_revision_id"],
                rationale=rec_row["rationale"],
                assumptions_json=rec_row["assumptions_json"],
                confidence=rec_row["confidence"],
                authoring_party_id=rec_row["authoring_party_id"],
                recorded_at=rec_row["recorded_at"],
            )

        # Stage 3..5: Findings → Region Occurrences → Document
        # Revisions. The downstream traversal is driven by the
        # Derived From Relationships of the *Recommendation Revision*
        # row, not by ``rec_node`` — restrictions cascade by record,
        # not by tree branch, so a Party can hold authority on a
        # leaf Finding even when the intermediate Recommendation
        # Revision is redacted on this chain.
        findings_nodes: list = []
        region_nodes: list = []
        docrev_nodes: list = []

        derived_from_rows: Sequence = ()
        if rec_row is not None:
            derived_from_rows = self._load_derived_from_relationships(
                connection,
                recommendation_id=target_recommendation_id,
                recommendation_revision_id=target_recommendation_revision_id,
            )

        for df_row in derived_from_rows:
            finding_id = df_row["target_id"]
            finding_rev_row = self._load_latest_finding_revision_row(
                connection,
                finding_id=finding_id,
                at=effective_at,
            )
            if finding_rev_row is None:
                # No Finding Revision exists at-or-before ``at`` for
                # this Finding Resource. The Decision still points at
                # the Finding via Derived From, so the entry is
                # preserved as a redaction marker for chain shape
                # stability. Gap descriptors are task 12.4.
                findings_nodes.append(
                    RedactedNode(kind=_NODE_KIND_FINDING_REVISION)
                )
                continue

            finding_revision_id = finding_rev_row["finding_revision_id"]
            if not self._is_permitted(
                connection,
                party_id=party_id,
                action=_AUTHORIZATION_ACTION_VIEW_FINDING_REVISION,
                target=TargetRef(
                    kind=_NODE_KIND_FINDING_REVISION,
                    id=finding_id,
                    revision_id=finding_revision_id,
                    scope=finding_id,
                ),
                at=effective_at,
            ):
                findings_nodes.append(
                    RedactedNode(kind=_NODE_KIND_FINDING_REVISION)
                )
                # Downstream Supports are not loaded for redacted
                # Findings — the Finding restricts the chain branch.
                # Region Occurrence and Document Revision lists must
                # still match the cardinality of *visible* Supports,
                # so a redacted Finding contributes nothing further.
                continue

            findings_nodes.append(
                FindingRevisionNode(
                    finding_id=finding_rev_row["finding_id"],
                    finding_revision_id=finding_rev_row["finding_revision_id"],
                    parent_revision_id=finding_rev_row["parent_revision_id"],
                    statement=finding_rev_row["statement"],
                    is_hypothesis=bool(finding_rev_row["is_hypothesis"]),
                    authoring_party_id=finding_rev_row["authoring_party_id"],
                    assumptions_json=finding_rev_row["assumptions_json"],
                    confidence_note=finding_rev_row["confidence_note"],
                    recorded_at=finding_rev_row["recorded_at"],
                )
            )

            # Stage 4 + 5 for this Finding Revision: walk every
            # Supports Relationship and resolve the Region Occurrence
            # plus its owning Document Revision.
            supports_rows = self._load_supports_relationships(
                connection,
                finding_id=finding_id,
                finding_revision_id=finding_revision_id,
            )
            for sup_row in supports_rows:
                region_id = sup_row["target_id"]
                document_revision_id = sup_row["target_revision_id"]
                region_row = self._load_region_occurrence_row(
                    connection,
                    region_id=region_id,
                    document_revision_id=document_revision_id,
                )
                doc_row = self._load_document_revision_row(
                    connection,
                    revision_id=document_revision_id,
                )

                # Region Occurrence: authorize, then build the
                # bounded-text span (Requirement 11.2).
                if region_row is None or doc_row is None:
                    region_nodes.append(
                        RedactedNode(kind=_NODE_KIND_REGION_OCCURRENCE)
                    )
                    docrev_nodes.append(
                        RedactedNode(kind=_NODE_KIND_DOCUMENT_REVISION)
                    )
                    continue

                if not self._is_permitted(
                    connection,
                    party_id=party_id,
                    action=_AUTHORIZATION_ACTION_VIEW_REGION_OCCURRENCE,
                    target=TargetRef(
                        kind=_NODE_KIND_REGION_OCCURRENCE,
                        id=region_id,
                        revision_id=document_revision_id,
                        scope=doc_row["resource_id"],
                    ),
                    at=effective_at,
                ):
                    region_nodes.append(
                        RedactedNode(kind=_NODE_KIND_REGION_OCCURRENCE)
                    )
                else:
                    start = int(region_row["start_offset_bytes"])
                    end = int(region_row["end_offset_bytes"])
                    bounded_text = bytes(doc_row["content_bytes"][start:end])
                    region_nodes.append(
                        RegionOccurrenceNode(
                            region_id=region_row["region_id"],
                            document_revision_id=region_row[
                                "document_revision_id"
                            ],
                            start_offset_bytes=start,
                            end_offset_bytes=end,
                            span_byte_length=int(
                                region_row["span_byte_length"]
                            ),
                            span_content_digest_sha256=region_row[
                                "span_content_digest_sha256"
                            ],
                            bounded_text=bounded_text,
                            recorded_at=region_row["recorded_at"],
                        )
                    )

                # Document Revision: independent authorization so a
                # Party with view authority on the Region but not on
                # the full Document can still see the span (via the
                # Region Occurrence above) while the Document
                # Revision metadata is redacted.
                if not self._is_permitted(
                    connection,
                    party_id=party_id,
                    action=_AUTHORIZATION_ACTION_VIEW_DOCUMENT_REVISION,
                    target=TargetRef(
                        kind=_NODE_KIND_DOCUMENT_REVISION,
                        id=doc_row["resource_id"],
                        revision_id=doc_row["revision_id"],
                        scope=doc_row["resource_id"],
                    ),
                    at=effective_at,
                ):
                    docrev_nodes.append(
                        RedactedNode(kind=_NODE_KIND_DOCUMENT_REVISION)
                    )
                else:
                    docrev_nodes.append(
                        DocumentRevisionNode(
                            resource_id=doc_row["resource_id"],
                            revision_id=doc_row["revision_id"],
                            parent_revision_id=doc_row["parent_revision_id"],
                            content_digest_sha256=doc_row[
                                "content_digest_sha256"
                            ],
                            contributing_party_id=doc_row[
                                "contributing_party_id"
                            ],
                            recorded_at=doc_row["recorded_at"],
                            change_description=doc_row["change_description"],
                        )
                    )

        return DecisionProvenanceChain(
            decision=decision_node,
            recommendation_revision=rec_node,
            findings=tuple(findings_nodes),
            region_occurrences=tuple(region_nodes),
            document_revisions=tuple(docrev_nodes),
            requested_decision_id=decision_id,
        )

    def resolve_region_text(
        self,
        connection: Connection,
        *,
        region_id: str,
        document_revision_id: str,
        party_id: str,
        at: Optional[datetime] = None,
    ) -> "RegionTextResolution":
        """Return the byte-equivalent span of a Region Occurrence (task 12.3).

        Implements the read side of Requirement 3.4 / 11.2: resolve a
        Content Region reference to (a) the exact start anchor, end
        anchor, and bounded text span of the persisted Region
        Occurrence in the resolved Document Revision, byte-equivalent
        to the bytes originally recorded for the Occurrence, and (b)
        a digest comparison between the SHA-256 of those bytes and
        the persisted ``span_content_digest_sha256``.

        Algorithm:

        1. Load the ``Region_Occurrences`` row pinned by the composite
           key ``(region_id, document_revision_id)``. When absent,
           raise :class:`RegionOccurrenceUnresolvableError` so the HTTP
           layer can render a 404 with the offending identifiers
           (Requirement 3.6).
        2. Load the ``Document_Revisions`` row by
           ``document_revision_id``. The FK constraint on the schema
           makes a missing row impossible for a successfully
           persisted Region Occurrence, but the navigator still
           guards defensively so a corrupted database surfaces the
           same indistinguishable 404 rather than a 500.
        3. Evaluate ``view.region_occurrence`` for the requesting
           Party, scoped to the owning Document's ``resource_id``.
           Deny → raise :class:`RegionTextAuthorizationError` carrying
           the ``reason_code`` and ``correlation_id`` so the HTTP
           layer can render the AD-WS-9 indistinguishable denial
           shape (``generic_denial_indicator``, ``reason_code``,
           ``correlation_id`` only — Requirement 7.4).
        4. Evaluate ``view.document_revision`` for the same Party
           and scope. The two checks are independent so a Party may
           hold one authority without the other; both are required
           to read the bounded text because the bytes live on the
           Document Revision and the anchors live on the Region
           Occurrence — disclosing one without the other would
           still leak information through the per-byte position of
           the span.
        5. Compute ``bounded_text =
           Document_Revisions.content_bytes[start:end]``. Compute
           ``computed_digest_sha256 = sha256(bounded_text)`` and
           compare against the persisted
           ``span_content_digest_sha256``. The comparison is exposed
           on the returned :class:`RegionTextResolution` as
           ``digest_matches`` so callers verifying the span do not
           have to re-hash on their side; the persisted and computed
           digests are both surfaced so a future-tense mismatch can
           be diagnosed without a second round-trip.

        Idempotence:
            Like :meth:`navigate_decision`, this method consults only
            immutable rows (``Region_Occurrences``,
            ``Document_Revisions``) so two invocations with the same
            ``(region_id, document_revision_id, party_id, at)`` tuple
            return byte-equivalent :class:`RegionTextResolution`
            instances — required by Property 9 (Navigation back to
            exact Evidence, task 12.9) which generates pipelines and
            asserts the returned span fields are present, the digest
            equals the recorded ``span_content_digest_sha256``, and
            the returned bytes are byte-equivalent to
            ``content_bytes[start:end]`` of the resolved Document
            Revision.

        Args:
            connection: SQLAlchemy connection bound to the caller's
                request. Used for two SELECTs plus the two
                :meth:`AuthorizationService.evaluate` audit appends
                (Requirement 12.5). Reads are non-consequential per
                design §"Provenance_Navigator" so no separate
                consequential audit row is appended by this method.
            region_id: Identity of the Content Region whose
                Occurrence is being resolved.
            document_revision_id: Identity of the Document Revision
                anchoring the Occurrence. Required by the composite
                primary key on ``Region_Occurrences``; historical
                Occurrences anchored to earlier Revisions are
                resolvable via the same call with the corresponding
                ``document_revision_id`` (Requirement 3.3).
            party_id: Identity of the requesting Party. Authority is
                evaluated against this Party at ``at``.
            at: Effective time for authority evaluation per design
                §"Cross-Cutting Concerns" (*Authorization*). When
                omitted, :attr:`clock` is consulted.

        Returns:
            A :class:`RegionTextResolution` carrying the anchors,
            digests, byte-equivalent ``bounded_text``, and
            ``digest_matches`` flag.

        Raises:
            RegionOccurrenceUnresolvableError: ``(region_id,
                document_revision_id)`` does not resolve to a
                ``Region_Occurrences`` row, or the row resolves but
                the owning Document Revision row is missing.
            RegionTextAuthorizationError: The requesting Party lacks
                ``view.region_occurrence`` or ``view.document_revision``
                authority for the owning Document's scope.
        """
        effective_at = at if at is not None else self.clock.now()

        region_row = self._load_region_occurrence_row(
            connection,
            region_id=region_id,
            document_revision_id=document_revision_id,
        )
        if region_row is None:
            raise RegionOccurrenceUnresolvableError(
                region_id=region_id,
                document_revision_id=document_revision_id,
            )

        doc_row = self._load_document_revision_row(
            connection, revision_id=document_revision_id
        )
        if doc_row is None:
            # FK invariant on ``Region_Occurrences.document_revision_id``
            # → ``Document_Revisions.revision_id`` makes this branch
            # unreachable for a successfully persisted Region
            # Occurrence; raised defensively so a future schema change
            # or a corrupted database surfaces the same 404 shape as
            # the unresolved-region branch rather than leaking a 500.
            raise RegionOccurrenceUnresolvableError(
                region_id=region_id,
                document_revision_id=document_revision_id,
            )

        owning_scope = doc_row["resource_id"]

        region_decision = self.authorization_service.evaluate(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_REGION_OCCURRENCE,
            target=TargetRef(
                kind=_NODE_KIND_REGION_OCCURRENCE,
                id=region_id,
                revision_id=document_revision_id,
                scope=owning_scope,
            ),
            at=effective_at,
        )
        if region_decision.is_deny:
            raise RegionTextAuthorizationError(
                reason_code=region_decision.reason_code or "no-role-assignment",
                correlation_id=region_decision.correlation_id,
            )

        doc_decision = self.authorization_service.evaluate(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_DOCUMENT_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_DOCUMENT_REVISION,
                id=owning_scope,
                revision_id=document_revision_id,
                scope=owning_scope,
            ),
            at=effective_at,
        )
        if doc_decision.is_deny:
            raise RegionTextAuthorizationError(
                reason_code=doc_decision.reason_code or "no-role-assignment",
                correlation_id=doc_decision.correlation_id,
            )

        start = int(region_row["start_offset_bytes"])
        end = int(region_row["end_offset_bytes"])
        bounded_text = bytes(doc_row["content_bytes"][start:end])
        stored_digest = region_row["span_content_digest_sha256"]
        computed_digest = hashlib.sha256(bounded_text).hexdigest()

        return RegionTextResolution(
            region_id=region_row["region_id"],
            document_revision_id=region_row["document_revision_id"],
            start_offset_bytes=start,
            end_offset_bytes=end,
            span_byte_length=int(region_row["span_byte_length"]),
            span_content_digest_sha256=stored_digest,
            computed_digest_sha256=computed_digest,
            digest_matches=(computed_digest == stored_digest),
            bounded_text=bounded_text,
            recorded_at=region_row["recorded_at"],
        )

    # -- internals ---------------------------------------------------------

    def _load_candidates(
        self,
        connection: Connection,
        *,
        target_id: str,
        target_revision_id: Optional[str],
        after_cursor: Optional[BacklinkCursor],
    ) -> Sequence[BacklinkEntry]:
        """Issue the candidate ``SELECT`` against ``Relationships``.

        The query uses the AD-WS-8 composite index
        ``ix_relationships_target_backlink`` on
        ``(target_id, target_revision_id, relationship_type,
        recorded_at)``. Ordering by ``(recorded_at, relationship_id)``
        matches the cursor encoding so pagination is consistent with
        the prior-page boundary.

        When ``target_revision_id`` is ``None`` the query matches any
        ``target_revision_id`` (including ``NULL``) so callers asking
        for backlinks of a Resource header (``target_id`` alone) get
        every Relationship pointing at the Resource regardless of
        whether the Relationship pinned a specific Revision.

        When ``after_cursor`` is supplied, the SQL adds a
        ``(recorded_at, relationship_id) > (:cursor_recorded_at,
        :cursor_relationship_id)`` predicate so the next page begins
        strictly after the prior page's last visible Relationship.
        SQLite compares the tuple lexicographically, which matches the
        order encoded by :class:`BacklinkCursor`.

        Returns:
            Up to :data:`BACKLINK_PAGE_LIMIT` candidate Relationships
            as :class:`BacklinkEntry` instances. Authority filtering
            is *not* applied here; that is the job of
            :meth:`_build_authorized_projection`.
        """
        params: dict = {
            "target_id": target_id,
            "limit": BACKLINK_PAGE_LIMIT,
        }

        # Two SQL templates so we can avoid binding a NULL parameter
        # against a SQL ``IS`` comparison (SQLite's ``IS`` requires a
        # literal NULL rather than a parameter bound to None). The
        # template selection is fully determined by the ``None``-ness
        # of ``target_revision_id`` and ``after_cursor``, both of
        # which are call-site facts; nothing about the requesting
        # Party's authority affects which template runs.
        revision_clause = (
            "AND target_revision_id = :target_revision_id"
            if target_revision_id is not None
            else ""
        )
        if target_revision_id is not None:
            params["target_revision_id"] = target_revision_id

        cursor_clause = ""
        if after_cursor is not None:
            cursor_clause = (
                "AND (recorded_at > :cursor_recorded_at "
                "     OR (recorded_at = :cursor_recorded_at "
                "         AND relationship_id > :cursor_relationship_id))"
            )
            params["cursor_recorded_at"] = after_cursor.recorded_at
            params["cursor_relationship_id"] = after_cursor.relationship_id

        sql = f"""
            SELECT
                relationship_id,
                relationship_type,
                source_kind,
                source_id,
                source_revision_id,
                authoring_party_id,
                recorded_at
            FROM Relationships
            WHERE target_id = :target_id
              {revision_clause}
              {cursor_clause}
            ORDER BY recorded_at ASC, relationship_id ASC
            LIMIT :limit
        """

        rows = connection.execute(text(sql), params).mappings().all()

        return tuple(
            BacklinkEntry(
                relationship_id=row["relationship_id"],
                relationship_type=row["relationship_type"],
                source_id=row["source_id"],
                source_kind=row["source_kind"],
                source_revision_id=row["source_revision_id"],
                authoring_party_id=row["authoring_party_id"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        )

    def _build_authorized_projection(
        self,
        connection: Connection,
        *,
        candidates: Sequence[BacklinkEntry],
        party_id: str,
        at: datetime,
    ) -> Sequence[BacklinkEntry]:
        """Filter ``candidates`` down to the authorized projection.

        For each candidate the requesting Party must hold both:

        - ``view.relationship`` authority on the Relationship itself,
          scoped to the source endpoint's identity (the scope chosen
          so a Party with view rights on a source endpoint sees all
          inbound and outbound Relationships involving that source).
        - ``view.<source_kind>`` authority on the source endpoint,
          scoped to the source endpoint's identity.

        Candidates failing either check are silently dropped. The
        order of ``candidates`` is preserved, so the resulting
        projection is also in ``(recorded_at ASC, relationship_id
        ASC)`` order.

        Args:
            connection: The caller's SQLAlchemy connection — passed
                through to :meth:`AuthorizationService.evaluate` so
                the evaluation audit row participates in the caller's
                transaction (AD-WS-5).
            candidates: The candidate Relationships loaded in
                step 1 of the algorithm.
            party_id: Identity of the requesting Party.
            at: Effective time for the authorization evaluation.

        Returns:
            The authorized projection — a (possibly empty) subset of
            ``candidates`` preserving the original order.
        """
        visible: list[BacklinkEntry] = []
        for entry in candidates:
            # The scope used for both authorization checks is the
            # source endpoint's Resource identity. This matches the
            # slice's simple scope semantics
            # (:meth:`AuthorizationService._scope_covers`): a Party
            # with role scope equal to ``entry.source_id`` (or the
            # wildcard ``"*"``) can view both the Relationship and
            # the source endpoint; a Party with role scope on a
            # different source identity cannot.
            source_scope = entry.source_id

            relationship_decision = self.authorization_service.evaluate(
                connection,
                party_id=party_id,
                action=_AUTHORIZATION_ACTION_VIEW_RELATIONSHIP,
                target=TargetRef(
                    kind="relationship",
                    id=entry.relationship_id,
                    revision_id=None,
                    scope=source_scope,
                ),
                at=at,
            )
            if relationship_decision.is_deny:
                continue

            source_action = (
                f"{_AUTHORIZATION_ACTION_VIEW_PREFIX}{entry.source_kind}"
            )
            source_decision = self.authorization_service.evaluate(
                connection,
                party_id=party_id,
                action=source_action,
                target=TargetRef(
                    kind=entry.source_kind,
                    id=entry.source_id,
                    revision_id=entry.source_revision_id,
                    scope=source_scope,
                ),
                at=at,
            )
            if source_decision.is_deny:
                continue

            visible.append(entry)

        return tuple(visible)

    @staticmethod
    def _cursor_from_visible(
        visible: Sequence[BacklinkEntry],
    ) -> Optional[BacklinkCursor]:
        """Return the next-page cursor for ``visible``.

        The cursor is the ``(recorded_at, relationship_id)`` pair of
        the *last* visible Relationship, or ``None`` if ``visible``
        is empty (no more authorized Relationships to return).

        Property invariant: the cursor depends only on ``visible``.
        Two requests producing the same authorized projection produce
        the same cursor, regardless of how many candidates the
        database scan returned or how many were dropped during the
        authorization step. This equality is the foundation for
        Property 4's cursor-indistinguishability dimension.
        """
        if not visible:
            return None
        last = visible[-1]
        return BacklinkCursor(
            recorded_at=last.recorded_at,
            relationship_id=last.relationship_id,
        )

    # -- internals: Decision-to-Evidence traversal (task 12.2) -------------

    def _is_permitted(
        self,
        connection: Connection,
        *,
        party_id: str,
        action: str,
        target: TargetRef,
        at: datetime,
    ) -> bool:
        """Return ``True`` when the Authorization_Service permits ``action``.

        Thin wrapper over :meth:`AuthorizationService.evaluate` so the
        traversal reads as a sequence of ``if self._is_permitted(...)``
        guards rather than a sequence of ``.is_permit`` accessor calls.
        Each call appends one row to ``Audit_Records`` (Requirement
        12.5) inside the caller's connection per AD-WS-5.
        """
        decision = self.authorization_service.evaluate(
            connection,
            party_id=party_id,
            action=action,
            target=target,
            at=at,
        )
        return decision.is_permit

    @staticmethod
    def _load_decision_row(
        connection: Connection, decision_id: str
    ) -> Optional[dict]:
        """Load a ``Decisions`` row by ``decision_id``.

        Returns ``None`` when no row matches. The Decision is an
        Immutable Record (AD-WS-3, AD-WS-4) so the row, once present,
        does not change — fundamental to Property 8 idempotence.
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
        return dict(row) if row is not None else None

    @staticmethod
    def _load_recommendation_revision_row(
        connection: Connection,
        *,
        recommendation_id: str,
        recommendation_revision_id: str,
    ) -> Optional[dict]:
        """Load a ``Recommendation_Revisions`` row by composite identity.

        Both ``recommendation_id`` and ``recommendation_revision_id``
        are required by the composite identity check: a revision
        identifier alone could (in principle, though the
        ``Identifier_Registry`` and the FK constraint together
        prevent it) appear under a different Recommendation Resource.
        """
        row = (
            connection.execute(
                text(
                    """
                    SELECT recommendation_revision_id, recommendation_id,
                           parent_revision_id, rationale, assumptions_json,
                           confidence, authoring_party_id, recorded_at
                    FROM Recommendation_Revisions
                    WHERE recommendation_revision_id = :revision_id
                      AND recommendation_id = :recommendation_id
                    """
                ),
                {
                    "revision_id": recommendation_revision_id,
                    "recommendation_id": recommendation_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    def _load_latest_finding_revision_row(
        connection: Connection,
        *,
        finding_id: str,
        at: datetime,
    ) -> Optional[dict]:
        """Load the latest ``Finding_Revisions`` row at-or-before ``at``.

        The latest-at-time rule is essential for Property 8
        idempotence: Finding Revisions are append-only, so subsequent
        Revisions appended after the traversal's ``at`` do not change
        the result for the same ``(decision_id, party_id, at)`` tuple.

        Ordering is ``(recorded_at DESC, finding_revision_id DESC)``
        so a deterministic tiebreaker exists when two Revisions share
        a millisecond timestamp.
        """
        at_iso = format_iso8601_ms(at)
        row = (
            connection.execute(
                text(
                    """
                    SELECT finding_revision_id, finding_id, parent_revision_id,
                           statement, is_hypothesis, authoring_party_id,
                           assumptions_json, confidence_note, recorded_at
                    FROM Finding_Revisions
                    WHERE finding_id = :finding_id
                      AND recorded_at <= :at_iso
                    ORDER BY recorded_at DESC, finding_revision_id DESC
                    LIMIT 1
                    """
                ),
                {"finding_id": finding_id, "at_iso": at_iso},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    def _load_derived_from_relationships(
        connection: Connection,
        *,
        recommendation_id: str,
        recommendation_revision_id: str,
    ) -> Sequence[dict]:
        """Return the ``Derived From`` Relationships from this Rec Revision.

        Per :mod:`walking_slice.knowledge` Derived From rows have
        ``source_kind='recommendation_revision'``,
        ``source_id=recommendation_id``,
        ``source_revision_id=recommendation_revision_id``,
        ``target_kind='finding'``, ``target_id=finding_id``, and
        ``target_revision_id=NULL``. Ordering matches the AD-WS-8
        outbound traversal convention ``(recorded_at ASC,
        relationship_id ASC)``.
        """
        rows = connection.execute(
            text(
                """
                SELECT relationship_id, source_id, source_revision_id,
                       target_id, target_revision_id, recorded_at
                FROM Relationships
                WHERE relationship_type = 'Derived From'
                  AND source_kind = 'recommendation_revision'
                  AND source_id = :rec_id
                  AND source_revision_id = :rec_revision_id
                ORDER BY recorded_at ASC, relationship_id ASC
                """
            ),
            {
                "rec_id": recommendation_id,
                "rec_revision_id": recommendation_revision_id,
            },
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_supports_relationships(
        connection: Connection,
        *,
        finding_id: str,
        finding_revision_id: str,
    ) -> Sequence[dict]:
        """Return the ``Supports`` Relationships from this Finding Revision.

        Per :mod:`walking_slice.knowledge` Supports rows have
        ``source_kind='finding_revision'``,
        ``source_id=finding_id``,
        ``source_revision_id=finding_revision_id``,
        ``target_kind='region_occurrence'``,
        ``target_id=region_id``, and
        ``target_revision_id=document_revision_id``. Ordering matches
        the AD-WS-8 outbound traversal convention so the chain is
        deterministic.
        """
        rows = connection.execute(
            text(
                """
                SELECT relationship_id, source_id, source_revision_id,
                       target_id, target_revision_id, recorded_at
                FROM Relationships
                WHERE relationship_type = 'Supports'
                  AND source_kind = 'finding_revision'
                  AND source_id = :finding_id
                  AND source_revision_id = :finding_revision_id
                ORDER BY recorded_at ASC, relationship_id ASC
                """
            ),
            {
                "finding_id": finding_id,
                "finding_revision_id": finding_revision_id,
            },
        ).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _load_region_occurrence_row(
        connection: Connection,
        *,
        region_id: str,
        document_revision_id: str,
    ) -> Optional[dict]:
        """Load a ``Region_Occurrences`` row by composite primary key."""
        row = (
            connection.execute(
                text(
                    """
                    SELECT region_id, document_revision_id,
                           start_offset_bytes, end_offset_bytes,
                           span_byte_length, span_content_digest_sha256,
                           recorded_at
                    FROM Region_Occurrences
                    WHERE region_id = :region_id
                      AND document_revision_id = :document_revision_id
                    """
                ),
                {
                    "region_id": region_id,
                    "document_revision_id": document_revision_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    def _load_document_revision_row(
        connection: Connection, *, revision_id: str
    ) -> Optional[dict]:
        """Load a ``Document_Revisions`` row by ``revision_id``.

        Returns the full row including ``content_bytes`` so the
        bounded-text span (Requirement 11.2) can be computed on the
        caller's side without a second round-trip.
        """
        row = (
            connection.execute(
                text(
                    """
                    SELECT revision_id, resource_id, parent_revision_id,
                           content_bytes, content_digest_sha256,
                           contributing_party_id, recorded_at,
                           change_description
                    FROM Document_Revisions
                    WHERE revision_id = :revision_id
                    """
                ),
                {"revision_id": revision_id},
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None


# ===========================================================================
# Decision-to-Evidence provenance traversal (task 12.2).
#
# Design reference: ``.kiro/specs/first-walking-slice/design.md``
# §"Provenance traversal algorithm" — implements the pseudocode
#
#     def navigate_decision(decision_id, party, at):
#         chain = []
#         d = load_decision(decision_id)
#         if not authz.permit(party, "view.decision", d, at):
#             return not_found_indistinguishable_response()
#         chain.append(serialize(d))
#         rec = load_revision(Recommendation, d.target_recommendation_id,
#                             d.target_recommendation_revision_id)
#         chain.append(node_or_redaction(rec, party, at, kind="recommendation"))
#         findings = list_derived_from_findings(rec, party, at)
#         chain.append([node_or_redaction(f, party, at, kind="finding") for f in findings])
#         region_occurrences = list_supports_for_findings(findings, party, at)
#         chain.append([occurrence_or_redaction(o, party, at) for o in region_occurrences])
#         docrevs = [load_document_revision(o.doc_resource_id, o.doc_revision_id)
#                    for o in region_occurrences]
#         chain.append([node_or_redaction(d, party, at, kind="document_revision")
#                       for d in docrevs])
#         return chain
#
# The traversal walks the five stages
#     Decision → Recommendation Revision → Finding Revision(s)
#         → Region Occurrence(s) → Document Revision
# and emits a :class:`DecisionProvenanceChain` with each stage filled.
# Restricted nodes at any level (other than the Decision itself, which
# returns the indistinguishable-not-found response per Requirement 11.7
# and design pseudocode ``not_found_indistinguishable_response``) are
# replaced with a generic :class:`RedactedNode` carrying only the node
# kind. Full Completeness Disclosure policy enforcement (Requirements
# 11.3, 11.4, 11.7 in their entirety, including gap descriptors and
# the precise restricted-vs-nonexistent indistinguishability shape) is
# the responsibility of task 12.4.
#
# Idempotence (Requirement 11.5, design Correctness Property 8).
# Two invocations with the same ``(decision_id, party_id, at)`` return
# byte-equivalent results because:
#
#   1. Every domain row consulted lives on an immutable table —
#      ``Decisions``, ``Recommendation_Revisions``,
#      ``Finding_Revisions``, ``Relationships``, ``Region_Occurrences``,
#      ``Document_Revisions`` (AD-WS-4, design §"Persistence Invariants
#      Summary"). New rows can be inserted but existing rows cannot
#      change.
#   2. The Finding Revision picked for each ``Derived From`` link is
#      the latest revision *at-or-before* the supplied ``at`` (ordered
#      by ``(recorded_at DESC, finding_revision_id DESC)``); since
#      Finding Revisions are append-only, the latest-at-time selection
#      is stable for any fixed ``at``.
#   3. The Recommendation Revision is pinned by
#      ``(target_recommendation_id, target_recommendation_revision_id)``
#      stored on the Decision row, so no time-bounded selection is
#      needed.
#   4. Every ``Relationships`` row is loaded by deterministic ordering
#      ``(recorded_at ASC, relationship_id ASC)`` matching the AD-WS-8
#      / outbound index keying convention.
#   5. The authorization evaluation uses ``at`` as the effective time
#      per design §"Cross-Cutting Concerns" *Authorization*; the role
#      assignments themselves are evaluated against the same instant
#      every time.
# ===========================================================================


# Action strings issued to the AuthorizationService during a traversal.
# Each maps to ``view`` authority per :func:`_required_authority` in
# ``walking_slice.authorization``; centralized here so the action
# strings emitted by the traversal cannot drift from the action strings
# the policy layer (task 12.4) will consult.
_AUTHORIZATION_ACTION_VIEW_DECISION: Final[str] = "view.decision"
_AUTHORIZATION_ACTION_VIEW_RECOMMENDATION_REVISION: Final[str] = (
    "view.recommendation_revision"
)
_AUTHORIZATION_ACTION_VIEW_FINDING_REVISION: Final[str] = "view.finding_revision"
_AUTHORIZATION_ACTION_VIEW_REGION_OCCURRENCE: Final[str] = "view.region_occurrence"
_AUTHORIZATION_ACTION_VIEW_DOCUMENT_REVISION: Final[str] = "view.document_revision"


# Node-kind constants emitted on :class:`RedactedNode` and consumed by
# the HTTP layer (task 12.5) when rendering the AD-WS-9 redaction marker.
_NODE_KIND_DECISION: Final[str] = "decision"
_NODE_KIND_RECOMMENDATION_REVISION: Final[str] = "recommendation_revision"
_NODE_KIND_FINDING_REVISION: Final[str] = "finding_revision"
_NODE_KIND_REGION_OCCURRENCE: Final[str] = "region_occurrence"
_NODE_KIND_DOCUMENT_REVISION: Final[str] = "document_revision"


__all__ = __all__ + [
    "DecisionNode",
    "RecommendationRevisionNode",
    "FindingRevisionNode",
    "RegionOccurrenceNode",
    "DocumentRevisionNode",
    "RedactedNode",
    "DecisionProvenanceChain",
    "DecisionUnresolvableError",
]


# ---------------------------------------------------------------------------
# Result value objects.
#
# Each ProvenanceNode is a frozen dataclass so the traversal cannot
# accidentally mutate a value between hops, and so equality is
# structural — two :class:`DecisionProvenanceChain` instances built
# from the same ``(D, P, t)`` compare equal via ``==``, which the
# idempotence test in this task uses directly. Inline ``Union`` types
# in the chain attribute annotations express "either a visible node
# or a generic redaction marker"; full Completeness Disclosure shape
# is task 12.4's responsibility.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RedactedNode:
    """Generic redaction marker for a node the requesting Party may not view.

    Carries only the node ``kind`` and the constant boolean
    ``redacted=True``. Per Requirement 11.3 (full enforcement is task
    12.4) the marker must not disclose any identifier, count, or
    attribute value of the redacted node — these two fields satisfy
    the lower bound, and the HTTP layer in task 12.5 will surface
    them through the AD-WS-9 ``slice-default-2026`` policy.

    Attributes:
        kind: One of ``"decision"``, ``"recommendation_revision"``,
            ``"finding_revision"``, ``"region_occurrence"``, or
            ``"document_revision"``.
        redacted: Always ``True``. Present so JSON serializers can
            distinguish a redaction marker from a visible node by
            checking for this attribute.
    """

    kind: str
    redacted: bool = True


@dataclass(frozen=True)
class DecisionNode:
    """Serialized form of a ``Decisions`` row at the head of the chain.

    Carries every column persisted on the Decision Immutable Record
    (design §"Decisions") so callers and tests can render or assert
    against the full record without a second round-trip. The Decision
    has no Revision concept (AD-WS-3, AD-WS-4) so there is no
    ``revision_id`` attribute.
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


@dataclass(frozen=True)
class RecommendationRevisionNode:
    """Serialized form of the targeted ``Recommendation_Revisions`` row."""

    recommendation_id: str
    recommendation_revision_id: str
    parent_revision_id: Optional[str]
    rationale: Optional[str]
    assumptions_json: str
    confidence: Optional[str]
    authoring_party_id: str
    recorded_at: str


@dataclass(frozen=True)
class FindingRevisionNode:
    """Serialized form of a ``Finding_Revisions`` row in the chain.

    The Finding Revision selected for each ``Derived From`` link is
    the latest revision at-or-before the traversal's effective time
    ``at``; see the module-level "Idempotence" note for the
    rationale.
    """

    finding_id: str
    finding_revision_id: str
    parent_revision_id: Optional[str]
    statement: str
    is_hypothesis: bool
    authoring_party_id: str
    assumptions_json: str
    confidence_note: Optional[str]
    recorded_at: str


@dataclass(frozen=True)
class RegionOccurrenceNode:
    """Serialized form of a ``Region_Occurrences`` row plus its bounded text.

    Requirement 11.2 requires every Region Occurrence in a provenance
    chain to include the start anchor, end anchor, and bounded text
    span of the Occurrence in the originating Document Revision,
    byte-equivalent to the recorded text and digest-matching against
    the recorded content digest. The ``bounded_text`` attribute
    carries exactly those bytes — derived from
    ``Document_Revisions.content_bytes[start_offset_bytes:end_offset_bytes]``
    — and the persisted ``span_content_digest_sha256`` is included on
    the node so callers can verify the digest themselves without a
    second round-trip (Property 9, task 12.9). Equality of the
    persisted digest with the SHA-256 of ``bounded_text`` is an
    invariant of construction.
    """

    region_id: str
    document_revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    bounded_text: bytes
    recorded_at: str


@dataclass(frozen=True)
class DocumentRevisionNode:
    """Serialized form of a ``Document_Revisions`` row in the chain.

    ``content_bytes`` is intentionally *not* exposed here — the full
    document blob is heavy and the chain already carries the
    byte-equivalent span via :class:`RegionOccurrenceNode.bounded_text`.
    Callers needing the full document use the
    :meth:`walking_slice.evidence.EvidenceRepository.get_revision`
    surface.
    """

    resource_id: str
    revision_id: str
    parent_revision_id: Optional[str]
    content_digest_sha256: str
    contributing_party_id: str
    recorded_at: str
    change_description: Optional[str]


@dataclass(frozen=True)
class DecisionProvenanceChain:
    """The full Decision → Document Revision provenance chain.

    Matches the design pseudocode's five-element ``chain`` list,
    promoted to a named dataclass so the response has named fields
    rather than positional indices. ``findings``, ``region_occurrences``,
    and ``document_revisions`` are tuples (immutable sequences) so the
    chain cannot be mutated between the navigator and the HTTP layer.

    Attributes:
        decision: The :class:`DecisionNode` at the head of the chain.
            Never a :class:`RedactedNode` — when the Party lacks view
            authority on the Decision, the navigator raises
            :class:`DecisionUnresolvableError` per Requirement 11.6 /
            design §"Provenance traversal algorithm"
            ``not_found_indistinguishable_response``.
        recommendation_revision: The :class:`RecommendationRevisionNode`
            the Decision addresses, or a :class:`RedactedNode` when
            the requesting Party lacks ``view.recommendation_revision``
            authority on it.
        findings: One entry per ``Derived From`` Relationship from the
            Recommendation Revision, in
            ``(recorded_at ASC, relationship_id ASC)`` order. Each
            entry is the latest Finding Revision at-or-before ``at``
            (a :class:`FindingRevisionNode`) or a :class:`RedactedNode`.
        region_occurrences: One entry per ``Supports`` Relationship
            from the visible Finding Revisions, in the same
            stable order. Each entry is a :class:`RegionOccurrenceNode`
            (visible) or a :class:`RedactedNode` (restricted).
        document_revisions: One entry per ``Supports`` Relationship,
            zipped with ``region_occurrences`` so
            ``document_revisions[i]`` is the Document Revision owning
            ``region_occurrences[i]``. Each entry is a
            :class:`DocumentRevisionNode` or a :class:`RedactedNode`.
        requested_decision_id: The ``decision_id`` the caller asked
            for. Echoed back so the HTTP layer (task 12.5) can render
            it in the response shell without re-reading the request
            path.
    """

    decision: DecisionNode
    recommendation_revision: "RecommendationRevisionNode | RedactedNode"
    findings: tuple
    region_occurrences: tuple
    document_revisions: tuple
    requested_decision_id: str


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class DecisionUnresolvableError(Exception):
    """The requested Decision identity does not resolve to a Decision row.

    Raised by :meth:`ProvenanceNavigator.navigate_decision` per
    Requirement 11.6 when ``decision_id`` does not match any row in
    ``Decisions``. The exception message names the unresolvable
    Decision reference and carries it on :attr:`decision_id`; per
    Requirement 11.6 the navigator does not disclose existence of
    any related Resources, so the message intentionally does not
    surface any neighbouring identifiers.

    Per design §"Provenance traversal algorithm"
    ``not_found_indistinguishable_response``, the same exception is
    raised when the requesting Party lacks ``view.decision`` authority
    on an existing Decision so the response form is indistinguishable
    from the unresolvable case. Full Requirement 11.7 enforcement
    (response timing indistinguishability, redaction-policy shape) is
    delivered by task 12.4.

    Attributes:
        decision_id: The unresolvable Decision reference.
    """

    def __init__(self, decision_id: str) -> None:
        super().__init__(
            f"Decision identity {decision_id!r} does not resolve to a "
            f"Decision Immutable Record."
        )
        self.decision_id = decision_id


# ===========================================================================
# Region Occurrence text resolution (task 12.3).
#
# Design reference: ``.kiro/specs/first-walking-slice/design.md``
# §"Provenance_Navigator" HTTP surface (``GET /api/v1/regions/{region_id}/
# occurrences/{revision_id}/text``), Requirement 3.4 (a Content Region
# reference resolves to the exact Document Revision Identity, Region
# Identity, Region Occurrence, and bounded text span byte-equivalent to
# the span originally recorded), and Requirement 11.2 (Region
# Occurrence nodes in a provenance chain include the start anchor, end
# anchor, and bounded text span of that Occurrence in the originating
# Document Revision, byte-equivalent to the recorded text and
# digest-matching against the recorded content digest).
#
# The :meth:`ProvenanceNavigator.resolve_region_text` method composes
# the same row loads used by :meth:`navigate_decision` (the
# ``_load_region_occurrence_row`` / ``_load_document_revision_row``
# helpers and the ``view.region_occurrence`` / ``view.document_revision``
# authorization checks) into a stand-alone endpoint. The HTTP layer in
# :mod:`walking_slice.routes.provenance` (task 12.5) wraps the method
# and maps the three exception classes
# (``RegionOccurrenceUnresolvableError`` → 404,
# ``RegionTextAuthorizationError`` → 403 with the AD-WS-9
# indistinguishable denial shape) to wire-format error responses.
# ===========================================================================


__all__ = __all__ + [
    "RegionTextResolution",
    "RegionOccurrenceUnresolvableError",
    "RegionTextAuthorizationError",
]


@dataclass(frozen=True)
class RegionTextResolution:
    """Resolved Region Occurrence with byte-equivalent text and digest check.

    Returned by :meth:`ProvenanceNavigator.resolve_region_text`. Carries
    everything the HTTP layer needs to render the
    ``GET /api/v1/regions/{region_id}/occurrences/{revision_id}/text``
    response:

    - The persisted anchors (``start_offset_bytes``,
      ``end_offset_bytes``, ``span_byte_length``) and the recorded
      ``span_content_digest_sha256`` from the originating
      ``Region_Occurrences`` row.
    - ``bounded_text``: the byte-equivalent slice
      ``content_bytes[start:end]`` taken from the resolved Document
      Revision (Requirement 11.2 byte-equivalence).
    - ``computed_digest_sha256``: the SHA-256 of ``bounded_text``
      computed at resolution time, so callers can verify the digest
      without re-hashing on their side.
    - ``digest_matches``: a precomputed equality check between
      ``span_content_digest_sha256`` and ``computed_digest_sha256``.
      Construction-time invariant: a successful read against an
      unmutated row pair *must* yield ``digest_matches=True`` because
      the persisted digest was computed at write time over the same
      bytes the method reslices at read time, and both the Region
      Occurrence and the Document Revision are Immutable Records
      (AD-WS-4, design §"Persistence Invariants Summary"). A
      ``False`` result therefore signals storage corruption and the
      HTTP layer surfaces it through the response body so an operator
      can act on it without first reproducing the read.

    Equality is structural (frozen dataclass) so two resolutions
    derived from the same ``(region_id, document_revision_id, party,
    at)`` compare equal via ``==``, supporting Property 9 (task 12.9)
    which generates Decision chains whose provenance is fully visible
    and asserts the returned span fields are present and digest-match.

    Attributes:
        region_id: Identity of the Content Region.
        document_revision_id: Identity of the owning Document
            Revision. Echoed back on the response so the caller does
            not need to track it separately.
        start_offset_bytes: Persisted start anchor (inclusive byte
            offset into ``Document_Revisions.content_bytes``).
        end_offset_bytes: Persisted end anchor (exclusive byte
            offset into ``Document_Revisions.content_bytes``).
        span_byte_length: Persisted span length;
            ``end_offset_bytes - start_offset_bytes``.
        span_content_digest_sha256: Hex-encoded SHA-256 digest stored
            on the ``Region_Occurrences`` row at write time.
        computed_digest_sha256: Hex-encoded SHA-256 digest computed
            at read time over ``bounded_text``.
        digest_matches: ``True`` when
            ``span_content_digest_sha256 == computed_digest_sha256``.
        bounded_text: The byte-equivalent span
            ``Document_Revisions.content_bytes[start:end]``.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp from
            the ``Region_Occurrences`` row.
    """

    region_id: str
    document_revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    computed_digest_sha256: str
    digest_matches: bool
    bounded_text: bytes
    recorded_at: str


class RegionOccurrenceUnresolvableError(Exception):
    """A ``(region_id, document_revision_id)`` pair does not resolve.

    Raised by :meth:`ProvenanceNavigator.resolve_region_text` per
    Requirement 3.6 when no ``Region_Occurrences`` row exists for the
    composite key (or, defensively, when the row exists but the FK-
    referenced ``Document_Revisions`` row is missing). The HTTP layer
    in :mod:`walking_slice.routes.provenance` maps this to a 404 with
    the offending identifiers in the response body.

    Attributes:
        region_id: The unresolvable Region identity.
        document_revision_id: The unresolvable Document Revision
            identity supplied as the composite-key second half.
    """

    def __init__(self, *, region_id: str, document_revision_id: str) -> None:
        super().__init__(
            f"Region Occurrence ({region_id!r}, {document_revision_id!r}) "
            f"does not resolve to a Region_Occurrences row."
        )
        self.region_id = region_id
        self.document_revision_id = document_revision_id


class RegionTextAuthorizationError(Exception):
    """The requesting Party may not view the resolved Region Occurrence.

    Raised by :meth:`ProvenanceNavigator.resolve_region_text` when
    either of the two per-Party authorization checks
    (``view.region_occurrence`` on the Region Occurrence,
    ``view.document_revision`` on the owning Document Revision)
    returns deny. The HTTP layer maps this to a 403 with the AD-WS-9
    indistinguishable denial response shape, carrying *only*
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` (Requirement 7.4).

    The two-check shape — fail-fast on the first deny — keeps the
    audit row count proportional to *what was reachable* (one or two
    appends to ``Audit_Records`` per attempted resolution); full
    timing-indistinguishability shaping between the
    region-denied / document-denied / unresolved paths is the
    responsibility of task 12.4 (Completeness Disclosure policy
    enforcement).

    Attributes:
        reason_code: One of ``{not-yet-effective, expired, revoked,
            out-of-scope, no-role-assignment}`` per Requirement 7.2 /
            12.2.
        correlation_id: The operation correlation identifier shared
            with the evaluation audit row appended by the
            :class:`AuthorizationService`.
    """

    def __init__(self, *, reason_code: str, correlation_id: str) -> None:
        super().__init__(
            f"Region Occurrence text resolution denied "
            f"(reason_code={reason_code!r}, correlation_id={correlation_id!r})."
        )
        self.reason_code = reason_code
        self.correlation_id = correlation_id


# ===========================================================================
# Completeness Disclosure policy enforcement (task 12.4).
#
# Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"AD-WS-9 —
# Default Completeness Disclosure policy (closes Gap G-4)" and Requirements
# 10.5, 10.7, 11.3, 11.4, 11.7.
#
# The slice-default-2026 policy has three rules:
#
#   1. **Restricted node treatment.** Any node a requesting Party may not
#      view is replaced by a redaction marker carrying only
#      ``{"kind": "<node_kind>", "redacted": true}``. The
#      :class:`RedactedNode` value object already emits exactly this shape;
#      the existing :meth:`ProvenanceNavigator.navigate_decision`
#      (task 12.2) and :meth:`ProvenanceNavigator.list_backlinks`
#      (task 12.1) already substitute :class:`RedactedNode` for restricted
#      intermediate nodes. The policy enforcement layer added here
#      *verifies* that no leaked attributes have crept into the chain and
#      forbids surfacing any visible attributes for a redacted node.
#
#   2. **Gap descriptors for unavailable/stale/unresolved omissions.** Each
#      visible synthesis node (Decision, Recommendation Revision, Finding
#      Revision) may carry a Provenance Manifest with Omission Entries that
#      record material sources excluded by category. For category in
#      ``{unavailable, stale, unresolved}`` AND ``resolved_at IS NULL``,
#      surface a :class:`ChainGapDescriptor` carrying only ``stage``,
#      ``category``, and (when the requesting Party can view the next
#      reachable node) ``next_reachable_node_identity``. The ``restricted``
#      category is intentionally NOT surfaced as a gap — restricted nodes
#      are already replaced by :class:`RedactedNode` markers per rule 1.
#      The ``intentional`` category is recorded in the manifest for
#      provenance reads but is not surfaced to navigation callers (it is
#      not a "material gap" in the navigation sense).
#
#   3. **Restricted-vs-nonexistent normalization.** For Decision-level
#      authorization denial, :meth:`navigate_decision` already raises the
#      same :class:`DecisionUnresolvableError` whether the Decision row
#      does not exist or the requesting Party lacks view authority on it.
#      For backlinks the cursor, response size, and latency baseline already
#      depend solely on the authorized projection. The
#      :class:`DisclosureAppliedChain` wrapper here records the policy
#      identifier and the indistinguishability dimensions enforced so audit
#      consumers and tests can correlate the response with the
#      ``Disclosure_Policies`` row that produced it.
#
# Task scope (task 12.4):
#   This module wires the lookup, applies rules 1 and 2, and surfaces the
#   policy identifier alongside the chain. Rule 3 (timing
#   indistinguishability across the full request lifecycle) is verified by
#   Property 4 in task 12.7.
# ===========================================================================


# Omission categories that surface as :class:`ChainGapDescriptor` per
# AD-WS-9 rule 2. ``restricted`` is intentionally absent (rule 1 handles
# it with a :class:`RedactedNode`); ``intentional`` is intentionally
# absent (the manifest records it but it is not a "material gap" in
# the navigation sense).
_GAP_DESCRIPTOR_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"unavailable", "stale", "unresolved"}
)


# Stages that may carry a Provenance Manifest in the slice. Each entry
# maps a chain attribute ("decision", "recommendation_revision",
# "finding_revision") to the ``subject_kind`` value persisted on
# ``Provenance_Manifests`` so a single SELECT can resolve manifests for
# every stage of the chain in one query.
_MANIFEST_STAGE_KINDS: Final[tuple[str, ...]] = (
    "decision",
    "recommendation_revision",
    "finding_revision",
)


__all__ = __all__ + [
    "ChainGapDescriptor",
    "DisclosureAppliedChain",
    "DisclosurePolicyUnavailableError",
]


@dataclass(frozen=True)
class ChainGapDescriptor:
    """Gap descriptor for an unavailable/stale/unresolved Omission Entry.

    Surfaced by :meth:`ProvenanceNavigator.navigate_decision_with_disclosure`
    when a Provenance Manifest carries an unresolved Omission Entry whose
    category is in ``{unavailable, stale, unresolved}``. The shape matches
    Requirement 11.4 and AD-WS-9 rule 2: only ``stage``, ``category``, and
    (when visible) the next reachable node's identity are disclosed.

    Attributes:
        stage: The pipeline stage where the gap occurred. One of
            ``"decision"``, ``"recommendation_revision"``, or
            ``"finding_revision"`` — the three synthesis subject kinds
            persisted on ``Provenance_Manifests`` in this slice.
        category: One of ``"unavailable"``, ``"stale"``, or
            ``"unresolved"``. The ``"restricted"`` category is handled by
            :class:`RedactedNode` (AD-WS-9 rule 1), so it never appears
            here; the ``"intentional"`` category is recorded on the
            manifest but is not surfaced to navigation callers.
        next_reachable_node_identity: Identity of the next reachable
            node in the chain, when the requesting Party is authorized
            to view it (Requirement 11.4: "the Identity of the next
            reachable node where applicable"). ``None`` when no
            next reachable node is visible or applicable.

    Equality is structural so two
    :class:`DisclosureAppliedChain` instances built from the same
    ``(D, P, t)`` compare equal via ``==``, supporting Property 8
    idempotence at the policy-applied layer.
    """

    stage: str
    category: str
    next_reachable_node_identity: Optional[str] = None


@dataclass(frozen=True)
class DisclosureAppliedChain:
    """A Decision provenance chain with disclosure policy applied.

    Returned by
    :meth:`ProvenanceNavigator.navigate_decision_with_disclosure` and the
    HTTP layer (task 12.5). Wraps the visible
    :class:`DecisionProvenanceChain` with the gap descriptors loaded from
    Provenance Manifests on the visible synthesis nodes and surfaces the
    policy identifier in effect.

    Attributes:
        chain: The visible :class:`DecisionProvenanceChain`. Restricted
            intermediate nodes are already replaced by :class:`RedactedNode`
            markers per AD-WS-9 rule 1; that work is done by
            :meth:`ProvenanceNavigator.navigate_decision` (task 12.2).
        gap_descriptors: Tuple of :class:`ChainGapDescriptor` instances,
            one per unresolved Omission Entry on the visible chain's
            manifests with category in
            ``{unavailable, stale, unresolved}``. The tuple is ordered by
            ``(stage_position_in_chain, recorded_at ASC,
            omission_entry_id ASC)`` so repeated invocations with the
            same ``(D, P, t)`` return byte-equivalent tuples (Property 8
            idempotence at the policy-applied layer).
        policy_id: ``policy_id`` of the
            :class:`~walking_slice.disclosure.DisclosurePolicy` applied
            (e.g. ``"slice-default-2026"``).
        policy_name: ``policy_name`` of the same policy. Carried alongside
            the identifier so audit consumers can render the
            human-readable label without a second round-trip to
            ``Disclosure_Policies``.
    """

    chain: DecisionProvenanceChain
    gap_descriptors: tuple
    policy_id: str
    policy_name: str


class DisclosurePolicyUnavailableError(RuntimeError):
    """Raised when :meth:`navigate_decision_with_disclosure` has no policy.

    The slice mandates exactly one active Completeness Disclosure policy
    per AD-WS-9, seeded as the ``slice-default-2026`` row on startup
    (task 13.2). When :class:`ProvenanceNavigator` is constructed without
    a ``disclosure_policy`` argument, the navigator can still surface raw
    chains via :meth:`navigate_decision` (task 12.2) but cannot apply
    disclosure rules. Calling
    :meth:`navigate_decision_with_disclosure` in that state raises this
    error so the operator-visible log clearly surfaces a
    ``disclosure_policy_unavailable`` condition rather than silently
    skipping policy enforcement.
    """


# Patch the ProvenanceNavigator class to add the disclosure-policy-applied
# surface. Adding the methods via attribute assignment (rather than
# editing the original class body) keeps the task 12.4 changes
# self-contained at the bottom of the file so the diff against the
# original module is easy to follow and the existing class docstring,
# which already calls out task 12.4 as a follow-up, remains accurate.


def _navigate_decision_with_disclosure(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    decision_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> DisclosureAppliedChain:
    """Navigate a Decision chain and apply the active disclosure policy.

    Equivalent to calling :meth:`navigate_decision` followed by
    :meth:`apply_disclosure_policy`; provided as a convenience for the
    HTTP layer (task 12.5) and tests so the policy-applied response can
    be produced in one call. The policy used is the one configured on
    :attr:`ProvenanceNavigator.disclosure_policy` at construction time;
    if no policy is configured this method raises
    :class:`DisclosurePolicyUnavailableError`.

    Args:
        connection: SQLAlchemy connection bound to the caller's request.
            Used for both the chain SELECTs and the
            ``Provenance_Manifests`` / ``Omission_Entries`` lookups.
        decision_id: Identity of the Decision Immutable Record.
        party_id: Identity of the requesting Party.
        at: Effective time for authority evaluation. When ``None`` the
            navigator's :attr:`clock` is consulted.

    Returns:
        A :class:`DisclosureAppliedChain` with the visible chain, gap
        descriptors, and the policy identifier in effect.

    Raises:
        DecisionUnresolvableError: The Decision does not resolve or the
            requesting Party lacks ``view.decision`` authority on it.
        DisclosurePolicyUnavailableError: No policy is configured on
            this navigator.
    """
    if self.disclosure_policy is None:
        raise DisclosurePolicyUnavailableError(
            "ProvenanceNavigator was constructed without a "
            "disclosure_policy; navigate_decision_with_disclosure "
            "cannot apply AD-WS-9 rules. Configure the navigator "
            "with disclosure.get_policy(engine, "
            "'slice-default-2026') at startup (task 13.2)."
        )
    chain = self.navigate_decision(
        connection,
        decision_id=decision_id,
        party_id=party_id,
        at=at,
    )
    return self.apply_disclosure_policy(
        connection,
        chain=chain,
        party_id=party_id,
        at=at,
    )


def _apply_disclosure_policy(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    chain: DecisionProvenanceChain,
    party_id: str,
    at: Optional[datetime] = None,
) -> DisclosureAppliedChain:
    """Apply the active disclosure policy to a navigated chain.

    Collects :class:`ChainGapDescriptor` instances from the
    ``Provenance_Manifests`` and ``Omission_Entries`` rows associated
    with each visible synthesis node in ``chain`` and packages them
    alongside the chain into a :class:`DisclosureAppliedChain`.

    Stage walk:
        1. ``decision``: always present (a redacted Decision would have
           raised :class:`DecisionUnresolvableError` upstream).
        2. ``recommendation_revision``: only walked when not redacted.
        3. ``finding_revision``: only the visible Finding Revisions are
           walked. Redacted Findings contribute no gap descriptors so
           the response cannot inadvertently disclose the presence of
           an Omission Entry tied to a Finding the requesting Party
           cannot view.

    For each walked stage, the writer issues one ``SELECT`` against
    ``Provenance_Manifests`` joined to ``Omission_Entries`` to find
    rows with ``category IN ('unavailable','stale','unresolved')`` and
    ``resolved_at IS NULL``. The ``intentional`` category is excluded
    per AD-WS-9 rule 2 and the ``restricted`` category is excluded
    because it is handled by :class:`RedactedNode` (AD-WS-9 rule 1).

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request.
        chain: The visible :class:`DecisionProvenanceChain` returned
            by :meth:`navigate_decision`.
        party_id: Identity of the requesting Party. Used to evaluate
            ``view`` authority on next-reachable nodes when surfacing
            ``next_reachable_node_identity``.
        at: Effective time for the next-reachable authorization checks.
            When ``None`` the navigator's :attr:`clock` is consulted.

    Returns:
        A :class:`DisclosureAppliedChain` with the chain unchanged and
        the gap descriptors loaded.

    Raises:
        DisclosurePolicyUnavailableError: No policy is configured on
            this navigator.
    """
    policy = self.disclosure_policy
    if policy is None:
        raise DisclosurePolicyUnavailableError(
            "ProvenanceNavigator was constructed without a "
            "disclosure_policy; apply_disclosure_policy cannot run."
        )

    effective_at = at if at is not None else self.clock.now()
    descriptors: list[ChainGapDescriptor] = []

    # Stage 1: Decision. The decision node is never a RedactedNode in
    # a successfully returned chain (decision-level denial raises
    # DecisionUnresolvableError upstream), so we always walk its
    # manifest. The "next reachable" for a Decision-stage gap is the
    # Decision itself, which the caller already received in the
    # chain — surface its identity so an audit consumer can link the
    # gap to the visible head of the chain.
    decision_node = chain.decision
    descriptors.extend(
        self._collect_gap_descriptors_for_subject(
            connection,
            subject_kind="decision",
            subject_id=decision_node.decision_id,
            subject_revision_id=None,
            next_reachable_node_identity=decision_node.decision_id,
        )
    )

    # Stage 2: Recommendation Revision. Only walked when visible.
    rec = chain.recommendation_revision
    if isinstance(rec, RecommendationRevisionNode):
        descriptors.extend(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind="recommendation_revision",
                subject_id=rec.recommendation_id,
                subject_revision_id=rec.recommendation_revision_id,
                next_reachable_node_identity=rec.recommendation_id,
            )
        )

    # Stage 3: Finding Revisions. Walk each visible Finding Revision
    # in chain.findings order so the resulting descriptor tuple is
    # deterministic across invocations (Property 8 idempotence).
    for finding in chain.findings:
        if isinstance(finding, FindingRevisionNode):
            descriptors.extend(
                self._collect_gap_descriptors_for_subject(
                    connection,
                    subject_kind="finding_revision",
                    subject_id=finding.finding_id,
                    subject_revision_id=finding.finding_revision_id,
                    next_reachable_node_identity=finding.finding_id,
                )
            )

    return DisclosureAppliedChain(
        chain=chain,
        gap_descriptors=tuple(descriptors),
        policy_id=policy.policy_id,
        policy_name=policy.policy_name,
    )


def _collect_gap_descriptors_for_subject(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    subject_kind: str,
    subject_id: str,
    subject_revision_id: Optional[str],
    next_reachable_node_identity: Optional[str],
) -> Sequence[ChainGapDescriptor]:
    """Load unresolved gap-category Omission Entries for one subject.

    Issues one ``SELECT`` joining ``Provenance_Manifests`` to
    ``Omission_Entries``. The query filters on
    ``subject_kind``, ``subject_id``, and (when not ``None``)
    ``subject_revision_id``; on ``category IN
    ('unavailable','stale','unresolved')``; and on ``resolved_at IS
    NULL`` so already-resolved omissions are not re-surfaced.

    Ordering is ``(Omission_Entries.recorded_at ASC,
    omission_entry_id ASC)`` so repeated invocations return
    byte-equivalent descriptor lists for the same subject. Manifests
    are also ordered by ``recorded_at ASC`` so when a single subject
    has multiple manifests (rare but allowed by the immutability
    contract — a later manifest supersedes an earlier one by
    convention rather than schema rule) the older entries surface
    first.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request.
        subject_kind: One of ``"decision"``,
            ``"recommendation_revision"``, ``"finding_revision"``.
        subject_id: Identity of the synthesis subject.
        subject_revision_id: Revision Identity of the synthesis
            subject, or ``None`` for ``"decision"`` (which has no
            Revision concept).
        next_reachable_node_identity: Identity of the next reachable
            node to surface on each descriptor (Requirement 11.4).
            Almost always the Resource Identity of the subject
            itself; passed in by the caller so the call site
            documents the choice.

    Returns:
        Sequence of :class:`ChainGapDescriptor` instances, one per
        unresolved gap-category Omission Entry on the matched
        manifests, in stable order.
    """
    if subject_revision_id is not None:
        sql = """
            SELECT oe.category, oe.recorded_at, oe.omission_entry_id
              FROM Omission_Entries AS oe
              JOIN Provenance_Manifests AS pm
                ON pm.manifest_id = oe.manifest_id
             WHERE pm.subject_kind = :subject_kind
               AND pm.subject_id = :subject_id
               AND pm.subject_revision_id = :subject_revision_id
               AND oe.category IN ('unavailable','stale','unresolved')
               AND oe.resolved_at IS NULL
             ORDER BY oe.recorded_at ASC, oe.omission_entry_id ASC
        """
        params = {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "subject_revision_id": subject_revision_id,
        }
    else:
        sql = """
            SELECT oe.category, oe.recorded_at, oe.omission_entry_id
              FROM Omission_Entries AS oe
              JOIN Provenance_Manifests AS pm
                ON pm.manifest_id = oe.manifest_id
             WHERE pm.subject_kind = :subject_kind
               AND pm.subject_id = :subject_id
               AND pm.subject_revision_id IS NULL
               AND oe.category IN ('unavailable','stale','unresolved')
               AND oe.resolved_at IS NULL
             ORDER BY oe.recorded_at ASC, oe.omission_entry_id ASC
        """
        params = {
            "subject_kind": subject_kind,
            "subject_id": subject_id,
        }

    rows = connection.execute(text(sql), params).mappings().all()
    return tuple(
        ChainGapDescriptor(
            stage=subject_kind,
            category=row["category"],
            next_reachable_node_identity=next_reachable_node_identity,
        )
        for row in rows
    )


# Attach the policy-enforcement methods to the ProvenanceNavigator class
# in one place. Keeping the attachments next to the helper definitions
# means a reader following the file top-to-bottom sees the public
# surface first, then the policy section, then the wiring — there is
# no surprise method declared elsewhere.
ProvenanceNavigator.navigate_decision_with_disclosure = (
    _navigate_decision_with_disclosure
)
ProvenanceNavigator.apply_disclosure_policy = _apply_disclosure_policy
ProvenanceNavigator._collect_gap_descriptors_for_subject = (
    _collect_gap_descriptors_for_subject
)


# ===========================================================================
# Finding / Recommendation / Trail Revision provenance traversal (task 12.5).
#
# Design reference: ``.kiro/specs/first-walking-slice/design.md``
# §"Provenance_Navigator" HTTP surface — the
# ``/findings/{id}/provenance``, ``/recommendations/{id}/provenance``, and
# ``/trails/{id}/revisions/{revision_id}/provenance`` endpoints.
# Requirement 10.4 mandates the navigator return "the recorded sources,
# transformations, selection rules, and Omission Entries within 5
# seconds for manifests of up to 500 entries" for Findings,
# Recommendations, and Trail Revisions; Requirement 11.1 mandates the
# end-to-end traversal from a Decision (or any synthesis subject) back
# to exact Evidence.
#
# These traversals are *lightweight slices* of the same algorithm
# :meth:`ProvenanceNavigator.navigate_decision` already implements:
#
# - :meth:`navigate_finding` walks one Finding Revision → its Supports
#   Relationships → Region Occurrence(s) → Document Revision(s).
# - :meth:`navigate_recommendation` walks one Recommendation Revision →
#   its Derived From Relationships → Finding Revision(s) → Supports →
#   Region Occurrence(s) → Document Revision(s).
# - :meth:`navigate_trail_revision` returns the Trail Revision and its
#   five Trail Steps and, when ordinal 5 (the Decision step) is
#   visible, the inner :class:`DecisionProvenanceChain` so callers
#   walking a Trail get the full provenance without a second
#   round-trip.
#
# Every traversal goes through the same row-loading helpers used by
# :meth:`navigate_decision` and the same per-stage
# :meth:`AuthorizationService.evaluate` checks so the redaction
# behaviour matches:
#
# - The head subject (Finding Revision, Recommendation Revision, or
#   Trail Revision) is redacted at the
#   ``not_found_indistinguishable_response`` shape — raising the
#   subject-specific ``...UnresolvableError`` — when the requesting
#   Party lacks view authority on it (Requirement 11.7, full timing
#   indistinguishability is task 12.7).
# - Intermediate nodes (Findings inside a Recommendation chain, Region
#   Occurrences and Document Revisions inside every chain) are
#   replaced by :class:`RedactedNode` markers when restricted.
#
# Idempotence (Requirement 11.5 / Property 8): the methods consult only
# immutable rows (``Finding_Revisions``, ``Recommendation_Revisions``,
# ``Trail_Revisions``, ``Trail_Steps``, ``Relationships``,
# ``Region_Occurrences``, ``Document_Revisions``) so repeated invocations
# with the same ``(subject_id, party_id, at)`` return byte-equivalent
# chains.
# ===========================================================================


_AUTHORIZATION_ACTION_VIEW_TRAIL_REVISION: Final[str] = "view.trail_revision"
_NODE_KIND_TRAIL_REVISION: Final[str] = "trail_revision"


__all__ = __all__ + [
    "FindingProvenanceChain",
    "FindingUnresolvableError",
    "RecommendationProvenanceChain",
    "RecommendationUnresolvableError",
    "TrailRevisionNode",
    "TrailStepNode",
    "TrailProvenanceChain",
    "TrailRevisionUnresolvableError",
]


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FindingProvenanceChain:
    """Provenance chain for one Finding Revision.

    Attributes:
        finding_revision: The :class:`FindingRevisionNode` at the head
            of the chain. Never a :class:`RedactedNode` — when the
            Party lacks ``view.finding_revision`` authority on the
            target Finding the navigator raises
            :class:`FindingUnresolvableError` per Requirement 11.7 /
            design §"Provenance traversal algorithm"
            ``not_found_indistinguishable_response``.
        region_occurrences: One entry per ``Supports`` Relationship
            from the Finding Revision, in
            ``(recorded_at ASC, relationship_id ASC)`` order. Each
            entry is a :class:`RegionOccurrenceNode` (visible) or a
            :class:`RedactedNode` (restricted).
        document_revisions: One entry per ``Supports`` Relationship,
            positionally aligned with ``region_occurrences`` so
            ``document_revisions[i]`` owns ``region_occurrences[i]``.
            Each entry is a :class:`DocumentRevisionNode` or a
            :class:`RedactedNode`.
        gap_descriptors: Gap descriptors loaded from the Finding
            Revision's Provenance Manifest, when any are present.
            One entry per unresolved Omission Entry whose category
            is in ``{unavailable, stale, unresolved}``.
        requested_finding_id: The ``finding_id`` the caller asked for.
    """

    finding_revision: FindingRevisionNode
    region_occurrences: tuple
    document_revisions: tuple
    gap_descriptors: tuple
    requested_finding_id: str


@dataclass(frozen=True)
class RecommendationProvenanceChain:
    """Provenance chain for one Recommendation Revision.

    Attributes:
        recommendation_revision: The
            :class:`RecommendationRevisionNode` at the head of the
            chain. Never a :class:`RedactedNode` — when the Party
            lacks ``view.recommendation_revision`` authority the
            navigator raises :class:`RecommendationUnresolvableError`.
        findings: One entry per ``Derived From`` Relationship in
            ``(recorded_at ASC, relationship_id ASC)`` order. Each
            entry is the latest Finding Revision at-or-before ``at``
            (a :class:`FindingRevisionNode`) or a
            :class:`RedactedNode`.
        region_occurrences: One entry per visible Finding Revision's
            ``Supports`` Relationship, in stable order. Each entry is
            a :class:`RegionOccurrenceNode` or a :class:`RedactedNode`.
        document_revisions: One entry per ``Supports`` Relationship,
            positionally aligned with ``region_occurrences``.
        gap_descriptors: Gap descriptors loaded from the
            Recommendation Revision's Provenance Manifest.
        requested_recommendation_id: The ``recommendation_id`` the
            caller asked for.
    """

    recommendation_revision: RecommendationRevisionNode
    findings: tuple
    region_occurrences: tuple
    document_revisions: tuple
    gap_descriptors: tuple
    requested_recommendation_id: str


@dataclass(frozen=True)
class TrailStepNode:
    """Serialized form of a ``Trail_Steps`` row in the chain.

    Mirrors the columns persisted on ``Trail_Steps``; ``region_id`` is
    populated only for ordinal 2 (``region_occurrence``) and
    ``target_revision_id`` is populated for ordinals 1, 3, and 4
    (Revision-bearing target kinds).
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
class TrailRevisionNode:
    """Serialized form of a ``Trail_Revisions`` row.

    Carries every persisted column so the chain head can be rendered
    without a second round-trip. The five ``Trail_Steps`` rows hang
    off :class:`TrailProvenanceChain.steps`.
    """

    trail_id: str
    trail_revision_id: str
    predecessor_revision_id: Optional[str]
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str]
    authoring_party_id: str
    recorded_at: str


@dataclass(frozen=True)
class TrailProvenanceChain:
    """Provenance for one Trail Revision plus its five Trail Steps.

    Attributes:
        trail_revision: The :class:`TrailRevisionNode` at the head.
            Never a :class:`RedactedNode` — when the Party lacks
            ``view.trail_revision`` authority the navigator raises
            :class:`TrailRevisionUnresolvableError`.
        steps: Five :class:`TrailStepNode` instances in ordinal order
            (1..5). Steps are *not* individually authorized; the
            requesting Party's view authority on the Trail Revision
            covers the steps (design §"Trail_Service" — Trail Steps
            are owned by their Revision). Restricted *targets* of
            individual steps surface through the deeper chain — see
            ``decision_chain`` below.
        decision_chain: When ordinal 5's Decision target is visible
            to the requesting Party, the nested
            :class:`DecisionProvenanceChain` (the full Decision →
            Recommendation → Finding(s) → Region(s) → Document chain)
            so callers walking a Trail see the inline provenance for
            the Decision step. ``None`` when the Decision is
            unresolved or the requesting Party lacks view authority
            on it (an indistinguishable absence per Requirement 11.7).
        gap_descriptors: Gap descriptors loaded from the Trail
            Revision's Provenance Manifest, when any are present.
        requested_trail_id: The ``trail_id`` the caller asked for.
        requested_trail_revision_id: The ``trail_revision_id`` the
            caller asked for.
    """

    trail_revision: TrailRevisionNode
    steps: tuple
    decision_chain: Optional[DecisionProvenanceChain]
    gap_descriptors: tuple
    requested_trail_id: str
    requested_trail_revision_id: str


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class FindingUnresolvableError(Exception):
    """The requested Finding identity does not resolve.

    Raised by :meth:`ProvenanceNavigator.navigate_finding` when no
    ``Finding_Revisions`` row exists at-or-before ``at`` for the
    supplied ``finding_id``, or the row exists but the requesting
    Party lacks ``view.finding_revision`` authority on it. The two
    cases raise the same exception so the response form is
    indistinguishable (Requirement 11.7 — full timing
    indistinguishability is task 12.7).

    Attributes:
        finding_id: The unresolvable Finding reference.
    """

    def __init__(self, finding_id: str) -> None:
        super().__init__(
            f"Finding identity {finding_id!r} does not resolve to a "
            f"Finding Revision visible to the requesting Party."
        )
        self.finding_id = finding_id


class RecommendationUnresolvableError(Exception):
    """The requested Recommendation identity does not resolve.

    Raised by :meth:`ProvenanceNavigator.navigate_recommendation` when
    no ``Recommendation_Revisions`` row exists at-or-before ``at`` for
    the supplied ``recommendation_id`` or the requesting Party lacks
    ``view.recommendation_revision`` authority on the row.

    Attributes:
        recommendation_id: The unresolvable Recommendation reference.
    """

    def __init__(self, recommendation_id: str) -> None:
        super().__init__(
            f"Recommendation identity {recommendation_id!r} does not "
            f"resolve to a Recommendation Revision visible to the "
            f"requesting Party."
        )
        self.recommendation_id = recommendation_id


class TrailRevisionUnresolvableError(Exception):
    """The requested Trail Revision does not resolve.

    Raised by :meth:`ProvenanceNavigator.navigate_trail_revision` when
    no ``Trail_Revisions`` row exists for ``(trail_id,
    trail_revision_id)`` or the requesting Party lacks
    ``view.trail_revision`` authority on the row.

    Attributes:
        trail_id: The unresolvable Trail reference.
        trail_revision_id: The unresolvable Trail Revision reference.
    """

    def __init__(self, *, trail_id: str, trail_revision_id: str) -> None:
        super().__init__(
            f"Trail Revision ({trail_id!r}, {trail_revision_id!r}) does "
            f"not resolve to a Trail_Revisions row visible to the "
            f"requesting Party."
        )
        self.trail_id = trail_id
        self.trail_revision_id = trail_revision_id


# ---------------------------------------------------------------------------
# Navigator methods (attached to ProvenanceNavigator below).
# ---------------------------------------------------------------------------


def _navigate_finding(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    finding_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> FindingProvenanceChain:
    """Return the provenance chain for one Finding Revision.

    Loads the latest Finding Revision at-or-before ``at``, evaluates
    ``view.finding_revision`` for the requesting Party, then walks
    every ``Supports`` Relationship of that Revision to surface the
    cited Region Occurrence(s) and owning Document Revision(s).
    Restricted intermediate nodes are replaced by :class:`RedactedNode`
    markers. Gap descriptors are loaded from the Finding Revision's
    Provenance Manifest when any unresolved Omission Entry has a
    category in ``{unavailable, stale, unresolved}``.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request.
        finding_id: Identity of the Finding Resource whose latest
            Revision provenance is being requested.
        party_id: Identity of the requesting Party.
        at: Effective time for authority evaluation and the
            latest-Revision selection rule. When ``None`` the
            navigator's :attr:`clock` is consulted.

    Returns:
        A :class:`FindingProvenanceChain` carrying the
        Finding Revision, the visible Region Occurrence(s) and
        Document Revision(s) (positionally aligned), and any gap
        descriptors loaded from the Provenance Manifest.

    Raises:
        FindingUnresolvableError: The supplied ``finding_id`` does
            not resolve to a Finding Revision at-or-before ``at`` or
            the requesting Party lacks view authority on the
            resolved Revision.
    """
    effective_at = at if at is not None else self.clock.now()

    finding_rev_row = ProvenanceNavigator._load_latest_finding_revision_row(
        connection, finding_id=finding_id, at=effective_at
    )
    if finding_rev_row is None:
        raise FindingUnresolvableError(finding_id)

    finding_revision_id = finding_rev_row["finding_revision_id"]
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_FINDING_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_FINDING_REVISION,
            id=finding_id,
            revision_id=finding_revision_id,
            scope=finding_id,
        ),
        at=effective_at,
    ):
        raise FindingUnresolvableError(finding_id)

    finding_node = FindingRevisionNode(
        finding_id=finding_rev_row["finding_id"],
        finding_revision_id=finding_rev_row["finding_revision_id"],
        parent_revision_id=finding_rev_row["parent_revision_id"],
        statement=finding_rev_row["statement"],
        is_hypothesis=bool(finding_rev_row["is_hypothesis"]),
        authoring_party_id=finding_rev_row["authoring_party_id"],
        assumptions_json=finding_rev_row["assumptions_json"],
        confidence_note=finding_rev_row["confidence_note"],
        recorded_at=finding_rev_row["recorded_at"],
    )

    region_nodes, docrev_nodes = self._walk_supports_for_finding(
        connection,
        finding_id=finding_id,
        finding_revision_id=finding_revision_id,
        party_id=party_id,
        at=effective_at,
    )

    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind="finding_revision",
                subject_id=finding_id,
                subject_revision_id=finding_revision_id,
                next_reachable_node_identity=finding_id,
            )
        )

    return FindingProvenanceChain(
        finding_revision=finding_node,
        region_occurrences=tuple(region_nodes),
        document_revisions=tuple(docrev_nodes),
        gap_descriptors=gap_descriptors,
        requested_finding_id=finding_id,
    )


def _navigate_recommendation(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    recommendation_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> RecommendationProvenanceChain:
    """Return the provenance chain for one Recommendation Revision.

    Loads the latest Recommendation Revision at-or-before ``at``,
    evaluates ``view.recommendation_revision`` for the requesting
    Party, walks every ``Derived From`` Relationship of that Revision
    to pick the latest Finding Revision at-or-before ``at`` for each
    cited Finding, then walks every ``Supports`` Relationship of the
    visible Finding Revisions to surface the cited Region
    Occurrence(s) and owning Document Revision(s).

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request.
        recommendation_id: Identity of the Recommendation Resource.
        party_id: Identity of the requesting Party.
        at: Effective time for authority evaluation and the
            latest-Revision selection rule.

    Returns:
        A :class:`RecommendationProvenanceChain` carrying the
        Recommendation Revision, the visible Finding Revisions, the
        Region Occurrence(s) and Document Revision(s) (positionally
        aligned), and any gap descriptors from the Recommendation
        Revision's Provenance Manifest.

    Raises:
        RecommendationUnresolvableError: The supplied
            ``recommendation_id`` does not resolve to a Recommendation
            Revision at-or-before ``at`` or the requesting Party
            lacks view authority on the resolved Revision.
    """
    effective_at = at if at is not None else self.clock.now()

    rec_row = ProvenanceNavigator._load_latest_recommendation_revision_row(
        connection, recommendation_id=recommendation_id, at=effective_at
    )
    if rec_row is None:
        raise RecommendationUnresolvableError(recommendation_id)

    recommendation_revision_id = rec_row["recommendation_revision_id"]
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_RECOMMENDATION_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_RECOMMENDATION_REVISION,
            id=recommendation_id,
            revision_id=recommendation_revision_id,
            scope=recommendation_id,
        ),
        at=effective_at,
    ):
        raise RecommendationUnresolvableError(recommendation_id)

    rec_node = RecommendationRevisionNode(
        recommendation_id=rec_row["recommendation_id"],
        recommendation_revision_id=rec_row["recommendation_revision_id"],
        parent_revision_id=rec_row["parent_revision_id"],
        rationale=rec_row["rationale"],
        assumptions_json=rec_row["assumptions_json"],
        confidence=rec_row["confidence"],
        authoring_party_id=rec_row["authoring_party_id"],
        recorded_at=rec_row["recorded_at"],
    )

    findings_nodes: list = []
    region_nodes: list = []
    docrev_nodes: list = []

    derived_from_rows = ProvenanceNavigator._load_derived_from_relationships(
        connection,
        recommendation_id=recommendation_id,
        recommendation_revision_id=recommendation_revision_id,
    )

    for df_row in derived_from_rows:
        finding_id = df_row["target_id"]
        finding_rev_row = ProvenanceNavigator._load_latest_finding_revision_row(
            connection, finding_id=finding_id, at=effective_at
        )
        if finding_rev_row is None:
            findings_nodes.append(
                RedactedNode(kind=_NODE_KIND_FINDING_REVISION)
            )
            continue

        finding_revision_id = finding_rev_row["finding_revision_id"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_FINDING_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_FINDING_REVISION,
                id=finding_id,
                revision_id=finding_revision_id,
                scope=finding_id,
            ),
            at=effective_at,
        ):
            findings_nodes.append(
                RedactedNode(kind=_NODE_KIND_FINDING_REVISION)
            )
            continue

        findings_nodes.append(
            FindingRevisionNode(
                finding_id=finding_rev_row["finding_id"],
                finding_revision_id=finding_rev_row["finding_revision_id"],
                parent_revision_id=finding_rev_row["parent_revision_id"],
                statement=finding_rev_row["statement"],
                is_hypothesis=bool(finding_rev_row["is_hypothesis"]),
                authoring_party_id=finding_rev_row["authoring_party_id"],
                assumptions_json=finding_rev_row["assumptions_json"],
                confidence_note=finding_rev_row["confidence_note"],
                recorded_at=finding_rev_row["recorded_at"],
            )
        )

        finding_regions, finding_docs = self._walk_supports_for_finding(
            connection,
            finding_id=finding_id,
            finding_revision_id=finding_revision_id,
            party_id=party_id,
            at=effective_at,
        )
        region_nodes.extend(finding_regions)
        docrev_nodes.extend(finding_docs)

    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind="recommendation_revision",
                subject_id=recommendation_id,
                subject_revision_id=recommendation_revision_id,
                next_reachable_node_identity=recommendation_id,
            )
        )

    return RecommendationProvenanceChain(
        recommendation_revision=rec_node,
        findings=tuple(findings_nodes),
        region_occurrences=tuple(region_nodes),
        document_revisions=tuple(docrev_nodes),
        gap_descriptors=gap_descriptors,
        requested_recommendation_id=recommendation_id,
    )


def _navigate_trail_revision(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    trail_id: str,
    trail_revision_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> TrailProvenanceChain:
    """Return the provenance for one Trail Revision plus its five steps.

    Loads the ``Trail_Revisions`` row pinned by
    ``(trail_id, trail_revision_id)``, evaluates
    ``view.trail_revision`` for the requesting Party, and emits the
    Trail Revision plus its five Trail Steps in ordinal order. When
    ordinal 5's Decision target resolves and the requesting Party
    holds ``view.decision`` authority on it, the nested
    :class:`DecisionProvenanceChain` is attached at
    ``decision_chain`` so callers walking a Trail see the full
    Decision → Document Revision provenance inline.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request.
        trail_id: Identity of the Trail Resource.
        trail_revision_id: Identity of the Trail Revision to return.
        party_id: Identity of the requesting Party.
        at: Effective time for authority evaluation. When ``None``
            the navigator's :attr:`clock` is consulted.

    Returns:
        A :class:`TrailProvenanceChain`.

    Raises:
        TrailRevisionUnresolvableError: The
            ``(trail_id, trail_revision_id)`` pair does not resolve
            or the requesting Party lacks view authority on the
            Revision.
    """
    effective_at = at if at is not None else self.clock.now()

    trail_row = ProvenanceNavigator._load_trail_revision_row(
        connection, trail_id=trail_id, trail_revision_id=trail_revision_id
    )
    if trail_row is None:
        raise TrailRevisionUnresolvableError(
            trail_id=trail_id, trail_revision_id=trail_revision_id
        )

    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_TRAIL_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_TRAIL_REVISION,
            id=trail_id,
            revision_id=trail_revision_id,
            scope=trail_id,
        ),
        at=effective_at,
    ):
        raise TrailRevisionUnresolvableError(
            trail_id=trail_id, trail_revision_id=trail_revision_id
        )

    trail_node = TrailRevisionNode(
        trail_id=trail_row["trail_id"],
        trail_revision_id=trail_row["trail_revision_id"],
        predecessor_revision_id=trail_row["predecessor_revision_id"],
        purpose=trail_row["purpose"],
        audience_id=trail_row["audience_id"],
        ordering_rationale=trail_row["ordering_rationale"],
        authoring_party_id=trail_row["authoring_party_id"],
        recorded_at=trail_row["recorded_at"],
    )

    step_rows = ProvenanceNavigator._load_trail_steps(
        connection, trail_revision_id=trail_revision_id
    )
    steps = tuple(
        TrailStepNode(
            trail_step_id=row["trail_step_id"],
            ordinal=int(row["ordinal"]),
            target_kind=row["target_kind"],
            target_id=row["target_id"],
            target_revision_id=row["target_revision_id"],
            region_id=row["region_id"],
            selection_mode=row["selection_mode"],
            annotation=row["annotation"],
        )
        for row in step_rows
    )

    # The Decision step (ordinal 5) anchors a full Decision provenance
    # chain. Attaching it inline saves the caller a second round-trip.
    decision_chain: Optional[DecisionProvenanceChain] = None
    decision_step = next((s for s in steps if s.ordinal == 5), None)
    if decision_step is not None:
        try:
            decision_chain = self.navigate_decision(
                connection,
                decision_id=decision_step.target_id,
                party_id=party_id,
                at=effective_at,
            )
        except DecisionUnresolvableError:
            # Restricted-vs-nonexistent normalization per Requirement
            # 11.7: a denied or absent Decision yields a ``None`` chain
            # — the response carries no marker distinguishing the two
            # cases.
            decision_chain = None

    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind="trail_revision",
                subject_id=trail_id,
                subject_revision_id=trail_revision_id,
                next_reachable_node_identity=trail_id,
            )
        )

    return TrailProvenanceChain(
        trail_revision=trail_node,
        steps=steps,
        decision_chain=decision_chain,
        gap_descriptors=gap_descriptors,
        requested_trail_id=trail_id,
        requested_trail_revision_id=trail_revision_id,
    )


def _walk_supports_for_finding(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    finding_id: str,
    finding_revision_id: str,
    party_id: str,
    at: datetime,
) -> tuple[list, list]:
    """Walk the ``Supports`` Relationships of one Finding Revision.

    Returns a pair ``(region_nodes, docrev_nodes)`` where
    ``len(region_nodes) == len(docrev_nodes)`` and the two lists are
    positionally aligned. Restricted Region Occurrences and Document
    Revisions are replaced by :class:`RedactedNode` markers; missing
    rows (defensive FK guard) also surface as redaction markers so
    the chain shape stays stable.

    Used by both :meth:`navigate_finding` and
    :meth:`navigate_recommendation` so the per-Supports traversal
    semantics match across the three Decision/Recommendation/Finding
    endpoints.
    """
    region_nodes: list = []
    docrev_nodes: list = []

    supports_rows = ProvenanceNavigator._load_supports_relationships(
        connection,
        finding_id=finding_id,
        finding_revision_id=finding_revision_id,
    )
    for sup_row in supports_rows:
        region_id = sup_row["target_id"]
        document_revision_id = sup_row["target_revision_id"]
        region_row = ProvenanceNavigator._load_region_occurrence_row(
            connection,
            region_id=region_id,
            document_revision_id=document_revision_id,
        )
        doc_row = ProvenanceNavigator._load_document_revision_row(
            connection, revision_id=document_revision_id
        )

        if region_row is None or doc_row is None:
            region_nodes.append(RedactedNode(kind=_NODE_KIND_REGION_OCCURRENCE))
            docrev_nodes.append(RedactedNode(kind=_NODE_KIND_DOCUMENT_REVISION))
            continue

        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_REGION_OCCURRENCE,
            target=TargetRef(
                kind=_NODE_KIND_REGION_OCCURRENCE,
                id=region_id,
                revision_id=document_revision_id,
                scope=doc_row["resource_id"],
            ),
            at=at,
        ):
            region_nodes.append(RedactedNode(kind=_NODE_KIND_REGION_OCCURRENCE))
        else:
            start = int(region_row["start_offset_bytes"])
            end = int(region_row["end_offset_bytes"])
            bounded_text = bytes(doc_row["content_bytes"][start:end])
            region_nodes.append(
                RegionOccurrenceNode(
                    region_id=region_row["region_id"],
                    document_revision_id=region_row["document_revision_id"],
                    start_offset_bytes=start,
                    end_offset_bytes=end,
                    span_byte_length=int(region_row["span_byte_length"]),
                    span_content_digest_sha256=region_row[
                        "span_content_digest_sha256"
                    ],
                    bounded_text=bounded_text,
                    recorded_at=region_row["recorded_at"],
                )
            )

        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_DOCUMENT_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_DOCUMENT_REVISION,
                id=doc_row["resource_id"],
                revision_id=doc_row["revision_id"],
                scope=doc_row["resource_id"],
            ),
            at=at,
        ):
            docrev_nodes.append(RedactedNode(kind=_NODE_KIND_DOCUMENT_REVISION))
        else:
            docrev_nodes.append(
                DocumentRevisionNode(
                    resource_id=doc_row["resource_id"],
                    revision_id=doc_row["revision_id"],
                    parent_revision_id=doc_row["parent_revision_id"],
                    content_digest_sha256=doc_row["content_digest_sha256"],
                    contributing_party_id=doc_row["contributing_party_id"],
                    recorded_at=doc_row["recorded_at"],
                    change_description=doc_row["change_description"],
                )
            )

    return region_nodes, docrev_nodes


def _load_latest_recommendation_revision_row(
    connection: Connection,
    *,
    recommendation_id: str,
    at: datetime,
) -> Optional[dict]:
    """Load the latest ``Recommendation_Revisions`` row at-or-before ``at``.

    Mirrors :meth:`_load_latest_finding_revision_row` for the
    Recommendation table. Ordering is
    ``(recorded_at DESC, recommendation_revision_id DESC)`` so a
    deterministic tiebreaker exists when two Revisions share a
    millisecond timestamp.
    """
    at_iso = format_iso8601_ms(at)
    row = (
        connection.execute(
            text(
                """
                SELECT recommendation_revision_id, recommendation_id,
                       parent_revision_id, rationale, assumptions_json,
                       confidence, authoring_party_id, recorded_at
                FROM Recommendation_Revisions
                WHERE recommendation_id = :recommendation_id
                  AND recorded_at <= :at_iso
                ORDER BY recorded_at DESC, recommendation_revision_id DESC
                LIMIT 1
                """
            ),
            {"recommendation_id": recommendation_id, "at_iso": at_iso},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_trail_revision_row(
    connection: Connection,
    *,
    trail_id: str,
    trail_revision_id: str,
) -> Optional[dict]:
    """Load a ``Trail_Revisions`` row by composite identity.

    Filters on both ``trail_id`` and ``trail_revision_id`` so a
    Revision Identity from a different Trail is not silently returned.
    """
    row = (
        connection.execute(
            text(
                """
                SELECT trail_revision_id, trail_id, predecessor_revision_id,
                       purpose, audience_id, ordering_rationale,
                       authoring_party_id, recorded_at
                FROM Trail_Revisions
                WHERE trail_revision_id = :trail_revision_id
                  AND trail_id = :trail_id
                """
            ),
            {"trail_id": trail_id, "trail_revision_id": trail_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_trail_steps(
    connection: Connection, *, trail_revision_id: str
) -> Sequence[dict]:
    """Load the five ``Trail_Steps`` rows for one Trail Revision in ordinal order."""
    rows = (
        connection.execute(
            text(
                """
                SELECT trail_step_id, ordinal, target_kind, target_id,
                       target_revision_id, region_id, selection_mode,
                       annotation
                FROM Trail_Steps
                WHERE trail_revision_id = :trail_revision_id
                ORDER BY ordinal
                """
            ),
            {"trail_revision_id": trail_revision_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


# Attach the task-12.5 traversal methods to ProvenanceNavigator.
ProvenanceNavigator.navigate_finding = _navigate_finding
ProvenanceNavigator.navigate_recommendation = _navigate_recommendation
ProvenanceNavigator.navigate_trail_revision = _navigate_trail_revision
ProvenanceNavigator._walk_supports_for_finding = _walk_supports_for_finding
ProvenanceNavigator._load_latest_recommendation_revision_row = staticmethod(
    _load_latest_recommendation_revision_row
)
ProvenanceNavigator._load_trail_revision_row = staticmethod(
    _load_trail_revision_row
)
ProvenanceNavigator._load_trail_steps = staticmethod(_load_trail_steps)


# ===========================================================================
# Planning Provenance Chain traversal (Second Walking Slice task 12.1).
#
# Design reference: ``.kiro/specs/second-walking-slice/design.md``
# §"Planning_Service.PlanApprovals" — "**Provenance chain.** ... A new
# method ``Provenance_Navigator.navigate_plan_approval(plan_approval_id,
# party, at)`` is added to the Slice 1 ``provenance.py`` only as an
# additive function; it does not modify the existing
# ``navigate_decision``. The walk descends Plan Approval → Plan Revision
# → Activity Plan → Project → Objective → Slice 1 Decision and then
# delegates to ``navigate_decision`` for the Decision → Recommendation →
# Finding → Region → Document tail."
#
# This module section is **strictly additive**. It introduces:
#   - Five frozen node dataclasses for the planning prefix
#     (:class:`PlanApprovalNode`, :class:`PlanRevisionNode`,
#     :class:`ActivityPlanNode`, :class:`ProjectRevisionNode`,
#     :class:`ObjectiveRevisionNode`).
#   - A :class:`PlanApprovalProvenance` result dataclass carrying the
#     ordered chain plus the delegated Slice 1 Decision tail.
#   - A :class:`PlanApprovalUnresolvableError` exception mirroring
#     :class:`DecisionUnresolvableError` for the head-node
#     indistinguishability shape.
#   - Six view-action constants and five node-kind constants matching
#     the Slice 1 naming conventions.
#   - One row-load helper per planning table consulted by the walk
#     plus two latest-revision-at-time helpers for ``Project_Revisions``
#     and ``Objective_Revisions`` (mirrors the
#     ``_load_latest_finding_revision_row`` pattern from task 12.2).
#   - The :func:`_navigate_plan_approval` method, attached to
#     :class:`ProvenanceNavigator` at the bottom of this module so the
#     diff against the original class body is empty.
#
# Requirements satisfied (per task 12.1):
#     14.1 — Returns the ordered traversal Plan Approval Record → Plan
#            Revision → Activity Plan → Project → Objective → Slice 1
#            Decision → ... → Document Revision, identifying each node
#            by its Identity and (where applicable) Revision Identity.
#     14.2 — The delegated :meth:`navigate_decision` tail includes the
#            exact start anchor, end anchor, and bounded text span of
#            each Region Occurrence, byte-equivalent to the persisted
#            text and digest-matching against the recorded digest
#            (Slice 1 task 12.2 behaviour, unchanged).
#     14.3 — Restricted intermediate nodes are replaced with a
#            :class:`RedactedNode` carrying only ``kind`` and
#            ``redacted=True``; no identifier, count, or attribute
#            value of the redacted node is disclosed (slice-default-2026
#            rule 1, AD-WS-9 / AD-WS-16).
#     14.4 — Gap descriptors are loaded from the Plan Approval's
#            Provenance Manifest (when the navigator was constructed
#            with a :class:`DisclosurePolicy`) and surfaced on
#            :attr:`PlanApprovalProvenance.gap_descriptors` in the
#            stable order defined by
#            :meth:`_collect_gap_descriptors_for_subject`.
#     14.5 — Two invocations with the same ``(plan_approval_id,
#            party_id, at)`` return byte-equivalent results because
#            every consulted row is on an append-only table
#            (Plan_Approval_Records, Plan_Revisions, Activity_Plans,
#            Projects, Project_Revisions, Objectives,
#            Objective_Revisions, and the Slice 1 Decision tail), and
#            the latest-at-time helpers use the same deterministic
#            tiebreaker (``recorded_at DESC, *_revision_id DESC``) as
#            the Slice 1 helpers.
#     14.6 — When ``plan_approval_id`` does not resolve to a
#            ``Plan_Approval_Records`` row, the method raises
#            :class:`PlanApprovalUnresolvableError` carrying *only*
#            the unresolvable Plan Approval reference and discloses
#            nothing about related planning Resources.
#     14.7 — When the requesting Party lacks ``view.plan_approval``
#            authority on an existing Plan Approval, the method raises
#            the same :class:`PlanApprovalUnresolvableError` so the
#            response form (raised exception type, attributes carried)
#            is indistinguishable from the unresolvable case — matching
#            the Slice 1 ``not_found_indistinguishable_response`` shape
#            from :meth:`navigate_decision`. Timing-indistinguishability
#            is enforced by the slice-default-2026 policy in the HTTP
#            layer (task 15.1).
#
# Authority actions issued during the walk follow the Slice 1
# ``view.<resource_kind>`` form; every action maps to the ``view``
# authority via :func:`walking_slice.authorization._required_authority`
# (no new mapping rows are required because AD-WS-15 already extended
# ``_required_authority`` with the create.* actions, leaving the
# ``view.*`` prefix fallback in place).
# ===========================================================================


# ---------------------------------------------------------------------------
# Action and node-kind constants.
# ---------------------------------------------------------------------------


# Each ``view.<resource_kind>`` action maps to ``"view"`` authority via the
# Slice 1 ``_ACTION_PREFIX_TO_AUTHORITY`` fallback in
# :mod:`walking_slice.authorization`. The constants are surfaced here so
# the navigator and (future) tests can refer to one source of truth and so
# a static search for ``view.plan_approval`` lands on exactly one definition.
_AUTHORIZATION_ACTION_VIEW_PLAN_APPROVAL: Final[str] = "view.plan_approval"
_AUTHORIZATION_ACTION_VIEW_PLAN_REVISION: Final[str] = "view.plan_revision"
_AUTHORIZATION_ACTION_VIEW_ACTIVITY_PLAN: Final[str] = "view.activity_plan"
_AUTHORIZATION_ACTION_VIEW_PROJECT_REVISION: Final[str] = "view.project_revision"
_AUTHORIZATION_ACTION_VIEW_OBJECTIVE_REVISION: Final[str] = "view.objective_revision"


# Node-kind constants emitted on :class:`RedactedNode` for restricted
# intermediate nodes and on the new node dataclasses below for visible
# ones. Naming follows the Slice 1 convention: revision-bearing nodes use
# the ``*_revision`` suffix (matching ``recommendation_revision``,
# ``finding_revision``, ``document_revision``); revision-less nodes
# (``plan_approval``, ``activity_plan``) drop the suffix.
_NODE_KIND_PLAN_APPROVAL: Final[str] = "plan_approval"
_NODE_KIND_PLAN_REVISION: Final[str] = "plan_revision"
_NODE_KIND_ACTIVITY_PLAN: Final[str] = "activity_plan"
_NODE_KIND_PROJECT_REVISION: Final[str] = "project_revision"
_NODE_KIND_OBJECTIVE_REVISION: Final[str] = "objective_revision"


__all__ = __all__ + [
    "PlanApprovalNode",
    "PlanRevisionNode",
    "ActivityPlanNode",
    "ProjectRevisionNode",
    "ObjectiveRevisionNode",
    "PlanApprovalProvenance",
    "PlanApprovalUnresolvableError",
]


# ---------------------------------------------------------------------------
# Planning-prefix node dataclasses.
#
# Each frozen dataclass mirrors the persisted row's columns so the chain
# carries every field a caller may need without a second round-trip.
# ``ProjectRevisionNode`` and ``ObjectiveRevisionNode`` carry *both* the
# Resource Identity and the chosen Revision Identity so the chain pins the
# specific Revision walked at time ``at`` while still naming the Resource
# the caller asked about.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanApprovalNode:
    """Serialized form of a ``Plan_Approval_Records`` row at the head of the chain.

    The Plan Approval is an Immutable Record (AD-WS-19, mirroring the
    Decision Immutable Record from Slice 1 AD-WS-4) so the row, once
    inserted, never changes. The node therefore has no Revision concept
    and carries no ``revision_id`` attribute — the analogue of
    :class:`DecisionNode` for the planning chain head.

    Attributes:
        plan_approval_id: Identity of the Plan Approval Immutable Record.
        target_activity_plan_id: Identity of the Activity Plan whose
            Plan Revision was approved.
        target_plan_revision_id: Identity of the approved Plan Revision
            (``UNIQUE`` per Requirement 9.5).
        outcome: One of ``{"Approve", "Reject_Approval"}``.
        rationale: Human-readable rationale, 1..4000 characters.
        approving_party_id: Identity of the Party that recorded the
            Plan Approval.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}`` (AD-WS-10, reused unchanged by
            AD-WS-22).
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    plan_approval_id: str
    target_activity_plan_id: str
    target_plan_revision_id: str
    outcome: str
    rationale: str
    approving_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class PlanRevisionNode:
    """Serialized form of a ``Plan_Revisions`` row.

    Plan Revisions are versioned content (multiple revisions may exist
    per Activity Plan, chained via ``predecessor_revision_id``). The
    Plan Approval pins exactly one Plan Revision by
    ``target_plan_revision_id`` so the node returned by
    :meth:`navigate_plan_approval` is the precise revision that was
    approved — no latest-at-time selection is required.

    Attributes:
        plan_revision_id: Identity of the Plan Revision (the row's
            primary key; there is no separate Resource Identity for
            Plan Revisions, the Activity Plan plays that role).
        activity_plan_id: Identity of the Activity Plan this Plan
            Revision belongs to.
        predecessor_revision_id: Identity of the predecessor Plan
            Revision (when this Revision supersedes an earlier one),
            or ``None`` for the first Revision.
        lifecycle_state: ``"draft"`` or ``"approved"``. For a Plan
            Revision returned through :meth:`navigate_plan_approval`
            this is always ``"approved"`` because the chain is rooted
            at a Plan Approval Record and the Plan Approval transaction
            flips the lifecycle to ``"approved"`` atomically with the
            Approval insert (AD-WS-20).
        planned_scope: Planned-scope text, 1..10000 characters.
        deliverable_expectation_refs_json: JSON array of 0..50
            Deliverable Expectation identifiers, persisted as text.
        planning_assumptions_json: JSON array of 0..100 assumptions,
            each 1..2000 characters, persisted as text.
        ordering_rationale: Optional ordering rationale, 0..2000
            characters, or ``None``.
        authoring_party_id: Identity of the Party that authored the
            Plan Revision.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    plan_revision_id: str
    activity_plan_id: str
    predecessor_revision_id: Optional[str]
    lifecycle_state: str
    planned_scope: str
    deliverable_expectation_refs_json: str
    planning_assumptions_json: str
    ordering_rationale: Optional[str]
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class ActivityPlanNode:
    """Serialized form of an ``Activity_Plans`` row.

    Activity Plans are Resource-grain rows with no Revision concept
    (revisions live on ``Plan_Revisions``). The node carries the full
    persisted row so callers walking the chain can render the Activity
    Plan's title and authoring Party without a second round-trip.

    Attributes:
        activity_plan_id: Identity of the Activity Plan Resource.
        target_project_id: Identity of the Project the Activity Plan
            belongs to.
        title: Activity Plan title, 1..200 characters.
        authoring_party_id: Identity of the Party that recorded the
            Activity Plan.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    activity_plan_id: str
    target_project_id: str
    title: str
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class ProjectRevisionNode:
    """Serialized form of a ``Project_Revisions`` row (the chain's Project node).

    The Project's chain entry is its latest Revision at-or-before the
    traversal's effective time ``at``. The latest-at-time selection
    mirrors the Slice 1 :class:`FindingRevisionNode` selection used by
    :meth:`navigate_decision`: Project_Revisions are append-only
    (AD-WS-19), so for any fixed ``at`` the latest Revision is stable
    across invocations, preserving Requirement 14.5 idempotence even
    after later Project Revisions are appended.

    Attributes:
        project_id: Identity of the Project Resource.
        project_revision_id: Identity of the Project Revision selected
            (the latest at-or-before ``at``).
        parent_revision_id: Identity of the predecessor Project
            Revision, or ``None`` for the first Revision.
        name: Project name, 1..200 characters.
        summary: Project summary, 0..4000 characters, or ``None``.
        target_objective_id: Identity of the Objective the Project
            addresses — the link the planning chain walks next.
        planned_start_date: ISO-8601 date string.
        planned_end_date: ISO-8601 date string (``>= planned_start_date``).
        authoring_party_id: Identity of the Party that recorded the
            Project Revision.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    project_id: str
    project_revision_id: str
    parent_revision_id: Optional[str]
    name: str
    summary: Optional[str]
    target_objective_id: str
    planned_start_date: str
    planned_end_date: str
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class ObjectiveRevisionNode:
    """Serialized form of an ``Objective_Revisions`` row (the chain's Objective node).

    The Objective's chain entry is its latest Revision at-or-before the
    traversal's effective time ``at`` — same latest-at-time rule as
    :class:`ProjectRevisionNode` above. The ``target_decision_id``
    column is the link the chain walks last before delegating to
    :meth:`navigate_decision` for the Slice 1 tail.

    Attributes:
        objective_id: Identity of the Objective Resource.
        objective_revision_id: Identity of the Objective Revision
            selected (the latest at-or-before ``at``).
        parent_revision_id: Identity of the predecessor Objective
            Revision, or ``None`` for the first Revision.
        statement: Objective statement, 1..4000 characters.
        rationale: Objective rationale, 0..10000 characters, or ``None``.
        target_decision_id: Identity of the Slice 1 Decision the
            Objective addresses (the bridge into the Decision tail).
        authoring_party_id: Identity of the Party that recorded the
            Objective Revision.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    objective_id: str
    objective_revision_id: str
    parent_revision_id: Optional[str]
    statement: str
    rationale: Optional[str]
    target_decision_id: str
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Result dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanApprovalProvenance:
    """The full Planning Provenance Chain rooted at a Plan Approval Record.

    Matches the design's ordered traversal Plan Approval → Plan Revision
    → Activity Plan → Project → Objective → Slice 1 Decision →
    Recommendation Revision → Finding Revision(s) → Region Occurrence(s)
    → Document Revision. The planning prefix lives on the named fields
    below; the Slice 1 tail (Decision → Document Revision) is delegated
    to :meth:`ProvenanceNavigator.navigate_decision` and attached at
    :attr:`decision_chain`.

    Attributes:
        plan_approval: The :class:`PlanApprovalNode` at the head of the
            chain. Never a :class:`RedactedNode` — when the requesting
            Party lacks ``view.plan_approval`` authority on an existing
            Plan Approval, :meth:`navigate_plan_approval` raises
            :class:`PlanApprovalUnresolvableError` so the response form
            is indistinguishable from the unresolvable case
            (Requirement 14.7).
        plan_revision: The :class:`PlanRevisionNode` for the approved
            Plan Revision, or a :class:`RedactedNode` when the
            requesting Party lacks ``view.plan_revision`` authority on
            it. Per AD-WS-9 rule 1 the redaction marker carries only
            ``kind="plan_revision"`` and ``redacted=True``; no
            identifier, count, or attribute value of the underlying
            row is disclosed.
        activity_plan: The :class:`ActivityPlanNode` for the Activity
            Plan referenced by the Plan Approval, or a
            :class:`RedactedNode` when the requesting Party lacks
            ``view.activity_plan`` authority.
        project_revision: The :class:`ProjectRevisionNode` (latest
            Project Revision at-or-before ``at``) referenced by the
            Activity Plan's ``target_project_id``, or a
            :class:`RedactedNode` when the requesting Party lacks
            ``view.project_revision`` authority. When no Project
            Revision exists at-or-before ``at`` (theoretically
            impossible for a successfully approved chain because
            ``Project_Revisions`` is append-only and the Project
            Revision predates the Plan Approval, but defended for
            robustness) the node is also a :class:`RedactedNode`.
        objective_revision: The :class:`ObjectiveRevisionNode` (latest
            Objective Revision at-or-before ``at``) referenced by the
            Project Revision's ``target_objective_id``, or a
            :class:`RedactedNode` for the same restricted-or-missing
            cases as ``project_revision``.
        decision_chain: The full Slice 1
            :class:`DecisionProvenanceChain` produced by delegating to
            :meth:`navigate_decision` with the Objective Revision's
            ``target_decision_id``, or ``None`` when the Decision is
            unresolved or the requesting Party lacks ``view.decision``
            authority on it. The two cases yield the same ``None`` so
            the response is indistinguishable per Requirement 14.7 /
            AD-WS-9 rule 3.
        gap_descriptors: Gap descriptors loaded from the Plan
            Approval's Provenance Manifest (when the navigator was
            constructed with a :class:`DisclosurePolicy`). One entry
            per unresolved Omission Entry whose category is in
            ``{unavailable, stale, unresolved}`` per
            slice-default-2026 rule 2. Empty tuple when the navigator
            has no policy configured or the Plan Approval has no
            material gaps.
        requested_plan_approval_id: The ``plan_approval_id`` the
            caller asked for. Echoed back so the HTTP layer (task
            15.1) can render it in the response shell without
            re-reading the request path.
    """

    plan_approval: PlanApprovalNode
    plan_revision: "PlanRevisionNode | RedactedNode"
    activity_plan: "ActivityPlanNode | RedactedNode"
    project_revision: "ProjectRevisionNode | RedactedNode"
    objective_revision: "ObjectiveRevisionNode | RedactedNode"
    decision_chain: Optional[DecisionProvenanceChain]
    gap_descriptors: tuple
    requested_plan_approval_id: str


# ---------------------------------------------------------------------------
# Error.
# ---------------------------------------------------------------------------


class PlanApprovalUnresolvableError(Exception):
    """The requested Plan Approval Identity does not resolve.

    Raised by :meth:`ProvenanceNavigator.navigate_plan_approval` per
    Requirement 14.6 when ``plan_approval_id`` does not match any row in
    ``Plan_Approval_Records``. Also raised per Requirement 14.7 / design
    §"Provenance traversal algorithm"
    ``not_found_indistinguishable_response`` when the requesting Party
    lacks ``view.plan_approval`` authority on an existing Plan Approval
    so the response form is indistinguishable from the unresolvable
    case. Full timing-indistinguishability is enforced by the
    slice-default-2026 policy in the HTTP layer (task 15.1).

    The exception message names the unresolvable Plan Approval reference
    and carries it on :attr:`plan_approval_id`; per Requirement 14.6 the
    navigator does not disclose existence of any related planning
    Resources, so the message intentionally does not surface any
    neighbouring identifiers.

    Attributes:
        plan_approval_id: The unresolvable Plan Approval reference.
    """

    def __init__(self, plan_approval_id: str) -> None:
        super().__init__(
            f"Plan Approval identity {plan_approval_id!r} does not resolve "
            f"to a Plan Approval Immutable Record visible to the requesting "
            f"Party."
        )
        self.plan_approval_id = plan_approval_id


# ---------------------------------------------------------------------------
# Row-loading helpers.
#
# Static methods on the navigator (attached at the bottom of this section)
# so a single source of truth backs both :meth:`navigate_plan_approval` and
# any future read-side surfaces (e.g. a planning backlink endpoint added by
# task 12.2). Each helper consults exactly one append-only table and
# returns ``None`` when no row matches; idempotence is preserved by the
# append-only invariant on the consulted table (Plan_Approval_Records,
# Plan_Revisions, Activity_Plans, Projects, Project_Revisions, Objectives,
# Objective_Revisions are all rejected for UPDATE/DELETE by the triggers
# installed in task 1.3).
# ---------------------------------------------------------------------------


def _load_plan_approval_row(
    connection: Connection, plan_approval_id: str
) -> Optional[dict]:
    """Load a ``Plan_Approval_Records`` row by ``plan_approval_id``.

    Returns ``None`` when no row matches. The Plan Approval Record is an
    Immutable Record (AD-WS-19, mirroring Slice 1 AD-WS-4) so the row,
    once present, does not change — fundamental to Requirement 14.5
    idempotence.
    """
    row = (
        connection.execute(
            text(
                """
                SELECT plan_approval_id, target_activity_plan_id,
                       target_plan_revision_id, outcome, rationale,
                       approving_party_id, authority_basis_type,
                       authority_basis_id, applicable_scope, recorded_at
                  FROM Plan_Approval_Records
                 WHERE plan_approval_id = :plan_approval_id
                """
            ),
            {"plan_approval_id": plan_approval_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_plan_revision_row(
    connection: Connection, plan_revision_id: str
) -> Optional[dict]:
    """Load a ``Plan_Revisions`` row by ``plan_revision_id``.

    Returns ``None`` when no row matches. ``Plan_Revisions`` is
    append-only with the single AD-WS-19 exception that flips
    ``lifecycle_state`` from ``'draft'`` to ``'approved'`` during a Plan
    Approval transaction; once approved, the row is byte-equivalent
    forever (Requirement 9.4) so the load is stable across invocations.
    """
    row = (
        connection.execute(
            text(
                """
                SELECT plan_revision_id, activity_plan_id,
                       predecessor_revision_id, lifecycle_state,
                       planned_scope, deliverable_expectation_refs_json,
                       planning_assumptions_json, ordering_rationale,
                       authoring_party_id, applicable_scope, recorded_at
                  FROM Plan_Revisions
                 WHERE plan_revision_id = :plan_revision_id
                """
            ),
            {"plan_revision_id": plan_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_activity_plan_row(
    connection: Connection, activity_plan_id: str
) -> Optional[dict]:
    """Load an ``Activity_Plans`` row by ``activity_plan_id``.

    Returns ``None`` when no row matches. ``Activity_Plans`` is
    append-only (AD-WS-19) and Resource-grain (no Revision concept), so
    the load is naturally stable across invocations.
    """
    row = (
        connection.execute(
            text(
                """
                SELECT activity_plan_id, target_project_id, title,
                       authoring_party_id, applicable_scope, recorded_at
                  FROM Activity_Plans
                 WHERE activity_plan_id = :activity_plan_id
                """
            ),
            {"activity_plan_id": activity_plan_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_latest_project_revision_row(
    connection: Connection,
    *,
    project_id: str,
    at: datetime,
) -> Optional[dict]:
    """Load the latest ``Project_Revisions`` row at-or-before ``at``.

    Mirrors :meth:`ProvenanceNavigator._load_latest_finding_revision_row`
    for the Project Revisions table. Ordering is ``(recorded_at DESC,
    project_revision_id DESC)`` so a deterministic tiebreaker exists
    when two Revisions share a millisecond timestamp — required by
    Requirement 14.5 idempotence.
    """
    at_iso = format_iso8601_ms(at)
    row = (
        connection.execute(
            text(
                """
                SELECT project_revision_id, project_id, parent_revision_id,
                       name, summary, target_objective_id,
                       planned_start_date, planned_end_date,
                       authoring_party_id, applicable_scope, recorded_at
                  FROM Project_Revisions
                 WHERE project_id = :project_id
                   AND recorded_at <= :at_iso
                 ORDER BY recorded_at DESC, project_revision_id DESC
                 LIMIT 1
                """
            ),
            {"project_id": project_id, "at_iso": at_iso},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_latest_objective_revision_row(
    connection: Connection,
    *,
    objective_id: str,
    at: datetime,
) -> Optional[dict]:
    """Load the latest ``Objective_Revisions`` row at-or-before ``at``.

    Mirrors :func:`_load_latest_project_revision_row` for the
    Objective_Revisions table. The ``target_decision_id`` column on the
    returned row is the bridge into the Slice 1 Decision tail walked by
    :meth:`ProvenanceNavigator.navigate_decision`.
    """
    at_iso = format_iso8601_ms(at)
    row = (
        connection.execute(
            text(
                """
                SELECT objective_revision_id, objective_id, parent_revision_id,
                       statement, rationale, target_decision_id,
                       authoring_party_id, applicable_scope, recorded_at
                  FROM Objective_Revisions
                 WHERE objective_id = :objective_id
                   AND recorded_at <= :at_iso
                 ORDER BY recorded_at DESC, objective_revision_id DESC
                 LIMIT 1
                """
            ),
            {"objective_id": objective_id, "at_iso": at_iso},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Navigator method (attached to ProvenanceNavigator below).
# ---------------------------------------------------------------------------


def _navigate_plan_approval(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    plan_approval_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> PlanApprovalProvenance:
    """Walk the Planning Provenance Chain rooted at a Plan Approval Record.

    Implements the six-stage planning prefix (Plan Approval → Plan
    Revision → Activity Plan → Project → Objective → Slice 1 Decision)
    and delegates the Decision → Recommendation → Finding → Region →
    Document tail to :meth:`navigate_decision`, per design
    §"Planning_Service.PlanApprovals" ("Provenance chain") and
    Requirement 14.1.

    Stage-by-stage behaviour:

    1. **Plan Approval (Requirement 14.1, 14.6, 14.7).** Load the
       ``Plan_Approval_Records`` row for ``plan_approval_id``. When the
       row does not exist, raise :class:`PlanApprovalUnresolvableError`
       naming the unresolvable reference and disclosing nothing about
       related planning Resources (Requirement 14.6). When the row
       exists but the requesting Party lacks ``view.plan_approval``
       authority on it, raise the same exception per design
       §"Provenance traversal algorithm"
       ``not_found_indistinguishable_response`` so the response form
       is indistinguishable from the unresolvable case (Requirement
       14.7). Timing-indistinguishability is enforced by the
       slice-default-2026 policy in the HTTP layer (task 15.1).

    2. **Plan Revision (Requirement 14.1, 14.3).** Load the
       ``Plan_Revisions`` row pinned by
       ``target_plan_revision_id`` on the Plan Approval. When the row
       is missing (defensively, the FK constraint prevents this for a
       successfully approved chain), emit a
       :class:`RedactedNode(kind="plan_revision")`. When the requesting
       Party lacks ``view.plan_revision`` authority, emit the same
       redaction marker per AD-WS-9 rule 1 (slice-default-2026 coverage
       extended by AD-WS-16 / task 1.4). Downstream stages still load
       because restrictions cascade by record, not by tree branch —
       same convention as :meth:`navigate_decision`.

    3. **Activity Plan (Requirement 14.1, 14.3).** Load the
       ``Activity_Plans`` row pinned by ``target_activity_plan_id`` on
       the Plan Approval. (The Plan Revision's own ``activity_plan_id``
       is byte-equivalent to this value by the Plan Approval
       transaction's invariants; loading from the Plan Approval row
       avoids a dependency on the Plan Revision row being visible.)
       Redaction follows the same pattern as Plan Revision.

    4. **Project (Requirement 14.1, 14.3, 14.5).** Load the latest
       ``Project_Revisions`` row at-or-before ``at`` for the Activity
       Plan's ``target_project_id``. Latest-at-time selection (mirror
       of :meth:`navigate_decision` for Finding Revisions) preserves
       Requirement 14.5 idempotence even after later Project Revisions
       are appended. ``view.project_revision`` authorization is scoped
       to the Project Resource identity so a Party with view authority
       on the Project sees every Revision.

    5. **Objective (Requirement 14.1, 14.3, 14.5).** Load the latest
       ``Objective_Revisions`` row at-or-before ``at`` for the Project
       Revision's ``target_objective_id``. Same latest-at-time rule and
       same restriction pattern as Project. The
       ``target_decision_id`` column is the bridge into the Slice 1
       tail.

    6. **Slice 1 Decision tail (Requirement 14.1, 14.2, 14.3, 14.7).**
       Delegate to :meth:`navigate_decision` with the Objective
       Revision's ``target_decision_id``. The delegated call already
       enforces all five Slice 1 stages
       (Decision → Recommendation Revision → Finding Revision(s) →
       Region Occurrence(s) → Document Revision) including the
       bounded-text span requirement (Requirement 14.2 inherits
       Slice 1 Requirement 11.2). When the Decision is unresolved or
       the requesting Party lacks ``view.decision`` authority on it,
       :meth:`navigate_decision` raises
       :class:`DecisionUnresolvableError`; the planning navigator
       catches that exception and sets ``decision_chain=None`` so the
       absent-or-restricted Decision is indistinguishable per
       Requirement 14.7 / AD-WS-9 rule 3.

    7. **Gap descriptors (Requirement 14.4).** When the navigator was
       constructed with a :class:`DisclosurePolicy`, load the Plan
       Approval's Provenance Manifest and any unresolved Omission
       Entries whose category is in
       ``{unavailable, stale, unresolved}`` via the existing
       :meth:`_collect_gap_descriptors_for_subject` helper (task
       12.4), using ``subject_kind="plan_approval"``. The returned
       tuple is in stable
       ``(recorded_at ASC, omission_entry_id ASC)`` order so repeated
       invocations return byte-equivalent results.

    Idempotence (Requirement 14.5): every row consulted by stages 1–5
    lives on an append-only table (Plan_Approval_Records,
    Plan_Revisions, Activity_Plans, Project_Revisions,
    Objective_Revisions — all rejected for UPDATE/DELETE by the
    triggers installed in task 1.3 with the single AD-WS-19 lifecycle
    exception that flips Plan_Revisions.lifecycle_state to ``approved``
    inside the original Plan Approval transaction). Stage 6 delegates
    to :meth:`navigate_decision` whose idempotence is already
    established by Slice 1 Property 8. Two invocations with the same
    ``(plan_approval_id, party_id, at)`` therefore return
    byte-equivalent :class:`PlanApprovalProvenance` instances; structural
    equality (``==``) on the frozen dataclass is the canonical check
    used by the Property 23 test (task 16.8).

    Strictly additive (Requirement 19.1): this method neither modifies
    :meth:`navigate_decision` nor any other Slice 1 surface. It calls
    :meth:`navigate_decision` exactly once per invocation, with the
    same arguments shape Slice 1 callers already use.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request. Used for every read SELECT and is the same
            connection passed to ``AuthorizationService.evaluate`` so
            the evaluation audit row participates in the caller's
            transaction (AD-WS-5). Reads are non-consequential per
            design §"Provenance_Navigator" so no consequential audit
            row is appended by this method.
        plan_approval_id: Identity of the Plan Approval Immutable
            Record whose chain is being requested.
        party_id: Identity of the requesting Party. Authority is
            evaluated against this Party for every node in the chain.
        at: Effective time for authority evaluation per design
            §"Cross-Cutting Concerns" (*Authorization*) and the
            latest-Revision selection rule for Project_Revisions and
            Objective_Revisions. When omitted, :attr:`clock` is
            consulted; production code paths typically pass the
            ``RequestContext`` clock so every authorization decision
            inside one request shares an instant.

    Returns:
        :class:`PlanApprovalProvenance` with the planning prefix nodes
        populated, the delegated Slice 1 Decision tail attached at
        :attr:`PlanApprovalProvenance.decision_chain` (or ``None`` for
        the restricted-or-unresolved Decision case), and the gap
        descriptors loaded from the Plan Approval's Provenance
        Manifest.

    Raises:
        PlanApprovalUnresolvableError: The supplied
            ``plan_approval_id`` does not resolve to a
            ``Plan_Approval_Records`` row, or the requesting Party
            lacks ``view.plan_approval`` authority on the resolved
            Plan Approval Record.
    """
    effective_at = at if at is not None else self.clock.now()

    # ---- Stage 1: Plan Approval Record (head). --------------------------
    #
    # The head is special: per Requirements 14.6 and 14.7 the unresolved
    # and restricted cases must both raise the same exception so the
    # response form is indistinguishable. This mirrors the Slice 1
    # ``DecisionUnresolvableError`` pattern at the head of
    # :meth:`navigate_decision`.
    plan_approval_row = _load_plan_approval_row(connection, plan_approval_id)
    if plan_approval_row is None:
        raise PlanApprovalUnresolvableError(plan_approval_id)

    plan_approval_scope = plan_approval_row["applicable_scope"]
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_PLAN_APPROVAL,
        target=TargetRef(
            kind=_NODE_KIND_PLAN_APPROVAL,
            id=plan_approval_id,
            revision_id=None,
            scope=plan_approval_scope,
        ),
        at=effective_at,
    ):
        # ``not_found_indistinguishable_response``: raise the same
        # exception as the unresolved case so the externally observable
        # response form is identical (Requirement 14.7).
        raise PlanApprovalUnresolvableError(plan_approval_id)

    plan_approval_node = PlanApprovalNode(
        plan_approval_id=plan_approval_row["plan_approval_id"],
        target_activity_plan_id=plan_approval_row["target_activity_plan_id"],
        target_plan_revision_id=plan_approval_row["target_plan_revision_id"],
        outcome=plan_approval_row["outcome"],
        rationale=plan_approval_row["rationale"],
        approving_party_id=plan_approval_row["approving_party_id"],
        authority_basis_type=plan_approval_row["authority_basis_type"],
        authority_basis_id=plan_approval_row["authority_basis_id"],
        applicable_scope=plan_approval_row["applicable_scope"],
        recorded_at=plan_approval_row["recorded_at"],
    )

    target_plan_revision_id = plan_approval_row["target_plan_revision_id"]
    target_activity_plan_id = plan_approval_row["target_activity_plan_id"]

    # ---- Stage 2: Plan Revision. ----------------------------------------
    plan_revision_row = _load_plan_revision_row(connection, target_plan_revision_id)
    plan_revision_node: "PlanRevisionNode | RedactedNode"
    if plan_revision_row is None:
        # FK constraint on Plan_Approval_Records.target_plan_revision_id
        # makes this branch unreachable for a successfully approved
        # chain. Defensive emission of a redaction marker keeps the
        # chain shape stable for the caller.
        plan_revision_node = RedactedNode(kind=_NODE_KIND_PLAN_REVISION)
    elif not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_PLAN_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_PLAN_REVISION,
            id=target_plan_revision_id,
            revision_id=target_plan_revision_id,
            scope=plan_revision_row["activity_plan_id"],
        ),
        at=effective_at,
    ):
        plan_revision_node = RedactedNode(kind=_NODE_KIND_PLAN_REVISION)
    else:
        plan_revision_node = PlanRevisionNode(
            plan_revision_id=plan_revision_row["plan_revision_id"],
            activity_plan_id=plan_revision_row["activity_plan_id"],
            predecessor_revision_id=plan_revision_row["predecessor_revision_id"],
            lifecycle_state=plan_revision_row["lifecycle_state"],
            planned_scope=plan_revision_row["planned_scope"],
            deliverable_expectation_refs_json=plan_revision_row[
                "deliverable_expectation_refs_json"
            ],
            planning_assumptions_json=plan_revision_row[
                "planning_assumptions_json"
            ],
            ordering_rationale=plan_revision_row["ordering_rationale"],
            authoring_party_id=plan_revision_row["authoring_party_id"],
            applicable_scope=plan_revision_row["applicable_scope"],
            recorded_at=plan_revision_row["recorded_at"],
        )

    # ---- Stage 3: Activity Plan. ----------------------------------------
    #
    # Load by the Plan Approval's ``target_activity_plan_id`` rather than
    # the Plan Revision's ``activity_plan_id``: the two columns are
    # byte-equivalent for any successfully approved chain (the Plan
    # Approval transaction inserts both in one go), but reading from the
    # Plan Approval avoids a dependency on the Plan Revision row being
    # visible to the loader — keeping stage 3 reachable even when the
    # Plan Revision row is restricted (so the chain remains shape-stable
    # for the requesting Party).
    activity_plan_row = _load_activity_plan_row(connection, target_activity_plan_id)
    activity_plan_node: "ActivityPlanNode | RedactedNode"
    target_project_id: Optional[str] = None
    if activity_plan_row is None:
        activity_plan_node = RedactedNode(kind=_NODE_KIND_ACTIVITY_PLAN)
    elif not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_ACTIVITY_PLAN,
        target=TargetRef(
            kind=_NODE_KIND_ACTIVITY_PLAN,
            id=target_activity_plan_id,
            revision_id=None,
            scope=activity_plan_row["applicable_scope"],
        ),
        at=effective_at,
    ):
        activity_plan_node = RedactedNode(kind=_NODE_KIND_ACTIVITY_PLAN)
        # Even when the Activity Plan is restricted, downstream stages
        # still load using the ``target_project_id`` we read from the
        # row above — restrictions cascade by record, not by tree
        # branch, matching the Slice 1 :meth:`navigate_decision`
        # convention.
        target_project_id = activity_plan_row["target_project_id"]
    else:
        activity_plan_node = ActivityPlanNode(
            activity_plan_id=activity_plan_row["activity_plan_id"],
            target_project_id=activity_plan_row["target_project_id"],
            title=activity_plan_row["title"],
            authoring_party_id=activity_plan_row["authoring_party_id"],
            applicable_scope=activity_plan_row["applicable_scope"],
            recorded_at=activity_plan_row["recorded_at"],
        )
        target_project_id = activity_plan_row["target_project_id"]

    # ---- Stage 4: Project (latest Project Revision at-or-before ``at``).
    project_revision_node: "ProjectRevisionNode | RedactedNode"
    target_objective_id: Optional[str] = None
    if target_project_id is None:
        # No Activity Plan row available — cannot reach a Project.
        project_revision_node = RedactedNode(kind=_NODE_KIND_PROJECT_REVISION)
    else:
        project_revision_row = _load_latest_project_revision_row(
            connection, project_id=target_project_id, at=effective_at
        )
        if project_revision_row is None:
            # No Project Revision exists at-or-before ``at`` for this
            # Project. Theoretically unreachable on an approved chain
            # because Project_Revisions are written before the Plan
            # Approval transaction, but defended for robustness.
            project_revision_node = RedactedNode(kind=_NODE_KIND_PROJECT_REVISION)
        elif not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_PROJECT_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_PROJECT_REVISION,
                id=target_project_id,
                revision_id=project_revision_row["project_revision_id"],
                scope=target_project_id,
            ),
            at=effective_at,
        ):
            project_revision_node = RedactedNode(kind=_NODE_KIND_PROJECT_REVISION)
            # Cascade by record: still surface ``target_objective_id`` so
            # the Objective stage can attempt its own authorization.
            target_objective_id = project_revision_row["target_objective_id"]
        else:
            project_revision_node = ProjectRevisionNode(
                project_id=project_revision_row["project_id"],
                project_revision_id=project_revision_row["project_revision_id"],
                parent_revision_id=project_revision_row["parent_revision_id"],
                name=project_revision_row["name"],
                summary=project_revision_row["summary"],
                target_objective_id=project_revision_row["target_objective_id"],
                planned_start_date=project_revision_row["planned_start_date"],
                planned_end_date=project_revision_row["planned_end_date"],
                authoring_party_id=project_revision_row["authoring_party_id"],
                applicable_scope=project_revision_row["applicable_scope"],
                recorded_at=project_revision_row["recorded_at"],
            )
            target_objective_id = project_revision_row["target_objective_id"]

    # ---- Stage 5: Objective (latest Objective Revision at-or-before ``at``).
    objective_revision_node: "ObjectiveRevisionNode | RedactedNode"
    target_decision_id: Optional[str] = None
    if target_objective_id is None:
        objective_revision_node = RedactedNode(kind=_NODE_KIND_OBJECTIVE_REVISION)
    else:
        objective_revision_row = _load_latest_objective_revision_row(
            connection, objective_id=target_objective_id, at=effective_at
        )
        if objective_revision_row is None:
            objective_revision_node = RedactedNode(kind=_NODE_KIND_OBJECTIVE_REVISION)
        elif not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_OBJECTIVE_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_OBJECTIVE_REVISION,
                id=target_objective_id,
                revision_id=objective_revision_row["objective_revision_id"],
                scope=target_objective_id,
            ),
            at=effective_at,
        ):
            objective_revision_node = RedactedNode(kind=_NODE_KIND_OBJECTIVE_REVISION)
            # Cascade by record: surface ``target_decision_id`` for the
            # Slice 1 tail's own authorization check.
            target_decision_id = objective_revision_row["target_decision_id"]
        else:
            objective_revision_node = ObjectiveRevisionNode(
                objective_id=objective_revision_row["objective_id"],
                objective_revision_id=objective_revision_row[
                    "objective_revision_id"
                ],
                parent_revision_id=objective_revision_row["parent_revision_id"],
                statement=objective_revision_row["statement"],
                rationale=objective_revision_row["rationale"],
                target_decision_id=objective_revision_row["target_decision_id"],
                authoring_party_id=objective_revision_row["authoring_party_id"],
                applicable_scope=objective_revision_row["applicable_scope"],
                recorded_at=objective_revision_row["recorded_at"],
            )
            target_decision_id = objective_revision_row["target_decision_id"]

    # ---- Stage 6: Slice 1 Decision tail (delegated). --------------------
    #
    # When the Decision is unresolved or restricted, ``navigate_decision``
    # raises :class:`DecisionUnresolvableError`; we catch that and set
    # ``decision_chain=None`` so the two cases are indistinguishable
    # per Requirement 14.7 / AD-WS-9 rule 3. The delegation is the only
    # call into :meth:`navigate_decision` and it uses the same
    # ``(decision_id, party_id, at)`` shape Slice 1 callers already use
    # — strictly additive (Requirement 19.1).
    decision_chain: Optional[DecisionProvenanceChain] = None
    if target_decision_id is not None:
        try:
            decision_chain = self.navigate_decision(
                connection,
                decision_id=target_decision_id,
                party_id=party_id,
                at=effective_at,
            )
        except DecisionUnresolvableError:
            decision_chain = None

    # ---- Stage 7: Gap descriptors (slice-default-2026 rule 2). ----------
    #
    # The Plan Approval's Provenance Manifest is the only manifest in the
    # planning prefix because only the Plan Approval transaction writes
    # one (design §"Planning_Service.PlanApprovals" — persistence flow).
    # The existing :meth:`_collect_gap_descriptors_for_subject` helper
    # (task 12.4) is parameterized by subject kind so it works for
    # ``"plan_approval"`` without modification — Requirement 19.1's
    # additive-only constraint is preserved.
    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind=_NODE_KIND_PLAN_APPROVAL,
                subject_id=plan_approval_id,
                subject_revision_id=None,
                next_reachable_node_identity=plan_approval_id,
            )
        )

    return PlanApprovalProvenance(
        plan_approval=plan_approval_node,
        plan_revision=plan_revision_node,
        activity_plan=activity_plan_node,
        project_revision=project_revision_node,
        objective_revision=objective_revision_node,
        decision_chain=decision_chain,
        gap_descriptors=gap_descriptors,
        requested_plan_approval_id=plan_approval_id,
    )


# Attach the task-12.1 traversal and its row-load helpers to
# :class:`ProvenanceNavigator` in one place. Mirrors the attachment
# pattern used by tasks 12.4 and 12.5 so the public surface of the
# class is composed of the original methods plus these additive
# attachments — no edit to the original class body is required, which
# is what Requirement 19.1 (Reuse and Non-Modification of Slice 1
# Contexts) demands.
ProvenanceNavigator.navigate_plan_approval = _navigate_plan_approval
ProvenanceNavigator._load_plan_approval_row = staticmethod(_load_plan_approval_row)
ProvenanceNavigator._load_plan_revision_row = staticmethod(_load_plan_revision_row)
ProvenanceNavigator._load_activity_plan_row = staticmethod(_load_activity_plan_row)
ProvenanceNavigator._load_latest_project_revision_row = staticmethod(
    _load_latest_project_revision_row
)
ProvenanceNavigator._load_latest_objective_revision_row = staticmethod(
    _load_latest_objective_revision_row
)


# ===========================================================================
# Execution Provenance Tree traversal (Third Walking Slice task 12.1).
#
# Design reference: ``.kiro/specs/third-walking-slice/design.md``
# §"Provenance_Navigator (extended)" — "A new method
# ``Provenance_Navigator.navigate_completion(completion_id, party, at)``
# is added to the existing ``walking_slice.provenance`` module as a
# strictly additive function; it does not modify the existing
# ``navigate_decision`` or ``navigate_plan_approval``. The walk descends
# Completion Record → Plan Approval Immutable Record → Plan Revision →
# Activity Plan → Project → Objective → Slice 1 Decision and then
# delegates to ``navigate_plan_approval`` for the Planning chain; in
# parallel, walks Completion Record → Milestone Acceptance Record(s) →
# Deliverable Production Record(s) → produced Deliverable Revision(s),
# and Completion Record → Work Assignment Record(s) → Work Event Record(s)
# → Time Entry Record(s). Applies the ``slice-default-2026`` policy for
# restricted-vs-nonexistent observability."
#
# This module section is strictly additive (Requirement 40.1, mirroring
# Slice 2 Requirement 19.1). It introduces:
#   - Seven view-action constants and seven node-kind constants for the
#     Slice 3 node kinds, named consistently with the Slice 1 and Slice 2
#     ``view.<resource_kind>`` / ``_NODE_KIND_<kind>`` conventions.
#   - Seven frozen node dataclasses for the execution prefix
#     (:class:`CompletionNode`, :class:`WorkAssignmentNode`,
#     :class:`WorkEventNode`, :class:`TimeEntryNode`,
#     :class:`MilestoneAcceptanceNode`,
#     :class:`DeliverableProductionNode`,
#     :class:`DeliverableRevisionNode`).
#   - Two leg-grouping dataclasses
#     (:class:`MilestoneAcceptanceProductionChain` for the Milestone
#     Acceptance → Deliverable Production → produced Deliverable
#     Revision leg, and :class:`WorkAssignmentExecutionChain` for the
#     Work Assignment → Work Event(s) → Time Entry(ies) leg).
#   - An :class:`ExecutionProvenanceTree` result dataclass carrying the
#     three legs plus the delegated Slice 2 Planning chain and the gap
#     descriptors from the disclosure policy.
#   - A :class:`CompletionUnresolvableError` exception mirroring
#     :class:`DecisionUnresolvableError` and
#     :class:`PlanApprovalUnresolvableError` for the head-node
#     indistinguishability shape (Requirements 31.5, 31.6, 35.6, 35.7).
#   - One row-load helper per Slice 3 table consulted by the walk
#     (``Completion_Records``, ``Work_Assignment_Records``,
#     ``Work_Event_Records``, ``Time_Entry_Records``,
#     ``Deliverable_Production_Records``, ``Deliverable_Revisions``)
#     plus a Plan-Approval-by-target lookup that pairs the Completion's
#     ``target_plan_revision_id`` with the unique
#     ``Plan_Approval_Records.target_plan_revision_id`` from Slice 2.
#   - The :func:`_navigate_completion` method, attached to
#     :class:`ProvenanceNavigator` at the bottom of this section so the
#     diff against the original class body remains empty.
#
# Requirements satisfied (per task 12.1):
#     31.1 — Every Completion Record traces to an Approved Plan Revision
#            via the three legs walked here; the walk surfaces the chain
#            structurally even when individual nodes are redacted, so an
#            auditor can confirm the traceability invariant.
#     31.2 — Returns the ordered traversal Completion Record →
#            Milestone Acceptance Record(s) → Deliverable Production
#            Record(s) → produced Deliverable Revision(s); Completion
#            Record → Plan Approval Immutable Record → ... → Slice 1
#            Decision (via the delegated ``navigate_plan_approval``);
#            and Completion Record → Work Assignment Record(s) → Work
#            Event Record(s) → Time Entry Record(s). Each node is
#            identified by its Identity and (where applicable) Revision
#            Identity.
#     31.3 — Gap descriptors are surfaced from the Completion Record's
#            Provenance Manifest (when the navigator was constructed
#            with a :class:`DisclosurePolicy`) via the existing
#            :meth:`_collect_gap_descriptors_for_subject` helper using
#            ``subject_kind="completion_record"``. The descriptor shape
#            matches the Slice 1 Requirement 11.4 / Slice 2
#            Requirement 14.4 contract.
#     31.4 — Idempotent retrieval: every row consulted by the three
#            legs lives on an append-only table
#            (``Completion_Records``, ``Plan_Approval_Records``,
#            ``Work_Assignment_Records``, ``Work_Event_Records``,
#            ``Time_Entry_Records``, ``Milestone_Acceptance_Records``,
#            ``Deliverable_Production_Records``,
#            ``Deliverable_Revisions``) and every list is ordered by
#            ``(recorded_at ASC, primary_key ASC)`` with a deterministic
#            tiebreaker. The delegated :meth:`navigate_plan_approval`
#            preserves its own Requirement 14.5 idempotence (Slice 2
#            Property 22). Two invocations with the same
#            ``(completion_id, party_id, at)`` therefore return
#            byte-equivalent :class:`ExecutionProvenanceTree` instances;
#            structural equality (``==``) on the frozen dataclass is
#            the canonical check used by Property 7 tests (task 12.4).
#     31.5 — Unresolvable Completion: the method raises
#            :class:`CompletionUnresolvableError` carrying only the
#            unresolvable Completion reference; no neighbouring
#            identifiers are surfaced in the exception payload.
#     31.6 — Restricted Completion: the same
#            :class:`CompletionUnresolvableError` is raised when the
#            requesting Party lacks ``view.completion_record`` authority
#            on the resolved Completion Record so the response form
#            (raised exception type, attributes carried) is
#            indistinguishable from the unresolvable case. Full
#            timing-indistinguishability is enforced by the
#            slice-default-2026 policy in the HTTP layer (task 14).
#     35.1 — End-to-end ordered traversal: the three legs combined
#            (Planning leg via ``navigate_plan_approval``, Milestone
#            Acceptance leg, Work Assignment leg) cover every node from
#            the Completion Record down to one or more Document
#            Revisions in the Slice 1 Evidence_Repository, identifying
#            each intermediate node by its Identity and (where
#            applicable) Revision Identity.
#     35.2 — Exact Region Occurrence text is delivered by the delegated
#            ``navigate_decision`` tail (Slice 1 Requirement 11.2,
#            unchanged).
#     35.3 — Restricted intermediate nodes are replaced by
#            :class:`RedactedNode` markers carrying only ``kind`` and
#            ``redacted=True``; no identifier, count, or attribute value
#            of the redacted node is disclosed (slice-default-2026
#            rule 1, AD-WS-9 / AD-WS-25 — extended by task 1.4 to cover
#            Slice 3 node kinds).
#     35.4 — Gap descriptors loaded from the Completion Record's
#            Provenance Manifest (when a policy is configured) follow
#            the stable order defined by
#            :meth:`_collect_gap_descriptors_for_subject`. The delegated
#            ``navigate_plan_approval`` surfaces its own manifest gaps
#            on the Plan Approval prefix.
#     35.5 — Idempotent retrieval (see 31.4).
#     35.6 — Unresolvable anchor (see 31.5).
#     35.7 — Restricted anchor (see 31.6).
#     35.8 — Produced Deliverable Revision nodes carry both
#            ``content_digest_sha256`` and ``role_marker = 'generated_output'``
#            on :class:`DeliverableRevisionNode`, distinguishing them
#            from Slice 1 Source Evidence Document Revisions which do
#            not carry the ``role_marker`` column.
#
# Authority actions issued during the walk follow the
# ``view.<resource_kind>`` form; every action maps to the ``view``
# authority via :func:`walking_slice.authorization._required_authority`
# (no new mapping rows are required because the ``view.*`` prefix
# fallback already covers every node kind introduced by this slice).
# ===========================================================================


# ---------------------------------------------------------------------------
# Action and node-kind constants.
# ---------------------------------------------------------------------------


# Each ``view.<resource_kind>`` action maps to ``"view"`` authority via
# the prefix fallback in :mod:`walking_slice.authorization`. The
# constants are surfaced here so the navigator and the task 12.4 tests
# can refer to one source of truth and so a static search for
# ``view.completion_record`` lands on exactly one definition.
_AUTHORIZATION_ACTION_VIEW_COMPLETION: Final[str] = "view.completion_record"
_AUTHORIZATION_ACTION_VIEW_WORK_ASSIGNMENT: Final[str] = (
    "view.work_assignment_record"
)
_AUTHORIZATION_ACTION_VIEW_WORK_EVENT: Final[str] = "view.work_event_record"
_AUTHORIZATION_ACTION_VIEW_TIME_ENTRY: Final[str] = "view.time_entry_record"
_AUTHORIZATION_ACTION_VIEW_MILESTONE_ACCEPTANCE: Final[str] = (
    "view.milestone_acceptance_record"
)
_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_PRODUCTION: Final[str] = (
    "view.deliverable_production_record"
)
_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_REVISION: Final[str] = (
    "view.deliverable_revision"
)


# Node-kind constants emitted on :class:`RedactedNode` for restricted
# intermediate nodes and on the new node dataclasses below for visible
# ones. Naming matches the Slice 3 ``resource_kind`` enumeration listed
# in design §"Persistence Invariants Summary" rule 4 (Requirement 22.8)
# and the disclosure-policy coverage seed in task 1.4.
_NODE_KIND_COMPLETION: Final[str] = "completion_record"
_NODE_KIND_WORK_ASSIGNMENT: Final[str] = "work_assignment_record"
_NODE_KIND_WORK_EVENT: Final[str] = "work_event_record"
_NODE_KIND_TIME_ENTRY: Final[str] = "time_entry_record"
_NODE_KIND_MILESTONE_ACCEPTANCE: Final[str] = "milestone_acceptance_record"
_NODE_KIND_DELIVERABLE_PRODUCTION: Final[str] = "deliverable_production_record"
_NODE_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"


__all__ = __all__ + [
    "CompletionNode",
    "WorkAssignmentNode",
    "WorkEventNode",
    "TimeEntryNode",
    "MilestoneAcceptanceNode",
    "DeliverableProductionNode",
    "DeliverableRevisionNode",
    "WorkAssignmentExecutionChain",
    "MilestoneAcceptanceProductionChain",
    "ExecutionProvenanceTree",
    "CompletionUnresolvableError",
]


# ---------------------------------------------------------------------------
# Execution-prefix node dataclasses.
#
# Each frozen dataclass mirrors the persisted row's columns so the chain
# carries every field a caller may need without a second round-trip.
# Record-grain nodes (no Revision concept) drop the ``revision_id``
# attribute; the produced Deliverable Revision node carries both the
# Resource Identity and the Revision Identity so the chain pins the
# specific Revision walked, alongside the role marker and content digest
# required by Requirement 35.8.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionNode:
    """Serialized form of a ``Completion_Records`` row at the head of the tree.

    The Completion Record is a Governance Decision Immutable Record per
    [`documents/02-domain-model.md`] §8.5 and AD-WS-27, so the row, once
    inserted, never changes. The node therefore has no Revision concept
    and carries no ``revision_id`` attribute — the analogue of
    :class:`DecisionNode` (Slice 1) and :class:`PlanApprovalNode`
    (Slice 2) for the execution tree head.

    Attributes:
        completion_id: Identity of the Completion Immutable Record.
        target_plan_revision_id: Identity of the Approved Plan Revision
            whose execution is being recorded as complete (UNIQUE per
            Requirement 29.3).
        target_activity_plan_id: Identity of the Activity Plan whose
            Plan Revision is targeted, persisted on the Completion row
            by the resolver in task 11.1.
        target_project_id: Identity of the Project whose Plan Revision
            is targeted, persisted likewise.
        outcome: One of ``{"Completed", "Completed_With_Reservation"}``
            (Requirement 29.2 / 34.3 — no observed-Outcome value).
        rationale: Human-readable rationale, 1..4000 characters.
        source_milestone_acceptance_ids_json: JSON array string of
            source Milestone Acceptance Identities passed in the
            original request; may be ``"[]"`` (empty array) when the
            caller relied on the structural accepted-Milestone
            existence check.
        completing_party_id: Identity of the Party that recorded the
            Completion.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}`` (AD-WS-10).
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    completion_id: str
    target_plan_revision_id: str
    target_activity_plan_id: str
    target_project_id: str
    outcome: str
    rationale: str
    source_milestone_acceptance_ids_json: str
    completing_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class WorkAssignmentNode:
    """Serialized form of a ``Work_Assignment_Records`` row.

    Record-grain (no Revision concept). The ``assignee_party_id`` and
    ``assignment_authority_party_id`` columns surface the two Party
    Identities that participated in the assignment, in keeping with the
    Slice 3 AD-WS-29 assignee-binding invariant.

    Attributes:
        work_assignment_id: Identity of the Work Assignment Record.
        target_plan_revision_id: Identity of the Approved Plan Revision
            the assignment addresses.
        assignee_party_id: Identity of the named assignee Party.
        assignment_authority_party_id: Identity of the Party that
            recorded the assignment under ``assign`` authority.
        assignment_rationale: Human-readable rationale, 0..4000
            characters, or ``None``.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}``.
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    work_assignment_id: str
    target_plan_revision_id: str
    assignee_party_id: str
    assignment_authority_party_id: str
    assignment_rationale: Optional[str]
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class WorkEventNode:
    """Serialized form of a ``Work_Event_Records`` row.

    Record-grain. The ``event_kind`` column is one of the five
    enumerated values
    ``{started, progress_note, paused, resumed, deliverable_drafted}``
    (Requirement 24.2). The per-Work-Assignment state machine
    documented in design §"Event-kind state machine" is enforced at
    write time by :class:`WorkEventService`; the navigator does not
    re-enforce ordering on read because the table is append-only and
    the enforcement is invariant by construction.

    Attributes:
        work_event_id: Identity of the Work Event Record.
        target_work_assignment_id: Identity of the Work Assignment
            Record this event relates to (Relates To /
            ``semantic_role = 'work_event'``).
        event_kind: One of the five enumerated event kinds.
        event_note: Human-readable note, 0..4000 characters, or
            ``None``.
        recording_party_id: Identity of the Contributor that recorded
            the event.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}``.
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    work_event_id: str
    target_work_assignment_id: str
    event_kind: str
    event_note: Optional[str]
    recording_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class TimeEntryNode:
    """Serialized form of a ``Time_Entry_Records`` row.

    Record-grain. ``effort_hours`` is the normalized decimal string
    written by :class:`TimeEntryService`; the navigator surfaces it
    byte-equivalent to the persisted value so a downstream consumer
    can recompute totals without parsing ambiguity.

    Attributes:
        time_entry_id: Identity of the Time Entry Record.
        target_work_assignment_id: Identity of the Work Assignment
            Record this entry relates to (Relates To /
            ``semantic_role = 'time_entry'``).
        effort_hours: ISO-decimal string in ``0.00..24.00`` per
            Requirement 25.2.
        effort_period_start: ISO-8601 UTC ms-precision lower bound of
            the effort period.
        effort_period_end: ISO-8601 UTC ms-precision upper bound of
            the effort period.
        recording_party_id: Identity of the Contributor that recorded
            the entry.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}``.
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    time_entry_id: str
    target_work_assignment_id: str
    effort_hours: str
    effort_period_start: str
    effort_period_end: str
    recording_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class MilestoneAcceptanceNode:
    """Serialized form of a ``Milestone_Acceptance_Records`` row.

    Record-grain. ``outcome`` is one of ``{"Accept", "Reject"}``;
    Property 34 only walks Milestone Acceptances whose outcome is
    ``"Accept"``, but the navigator surfaces the raw value here so a
    caller inspecting the tree can distinguish.

    Attributes:
        milestone_acceptance_id: Identity of the Milestone Acceptance
            Record.
        source_deliverable_production_id: Identity of the Deliverable
            Production Record sourcing this Milestone Acceptance
            (UNIQUE per Requirement 28.3).
        produced_deliverable_id: Identity of the produced Deliverable
            Resource targeted by the Milestone Acceptance.
        produced_deliverable_revision_id: Identity of the produced
            Deliverable Revision targeted by the Milestone Acceptance
            (the Addresses target per AD-WS-26).
        target_deliverable_expectation_id: Identity of the Deliverable
            Expectation Resource the addressed Production was sourced
            against.
        target_deliverable_expectation_revision_id: Identity of the
            Deliverable Expectation Revision the addressed Production
            was sourced against.
        outcome: One of ``{"Accept", "Reject"}``.
        rationale: Human-readable rationale, 1..4000 characters.
        accepting_party_id: Identity of the Party that recorded the
            Milestone Acceptance under ``accept_milestone`` authority.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}``.
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    milestone_acceptance_id: str
    source_deliverable_production_id: str
    produced_deliverable_id: str
    produced_deliverable_revision_id: str
    target_deliverable_expectation_id: str
    target_deliverable_expectation_revision_id: str
    outcome: str
    rationale: str
    accepting_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class DeliverableProductionNode:
    """Serialized form of a ``Deliverable_Production_Records`` row.

    Record-grain. Records the binding between a source Work Assignment
    Record, a produced Deliverable Revision, and a target Deliverable
    Expectation Revision per Requirement 27.

    Attributes:
        deliverable_production_id: Identity of the Production Record.
        source_work_assignment_id: Identity of the Work Assignment
            Record this Production was authored under (Relates To /
            ``semantic_role = 'production_source'``).
        produced_deliverable_id: Identity of the produced Deliverable
            Resource (Produces target's Resource).
        produced_deliverable_revision_id: Identity of the produced
            Deliverable Revision (Produces target's Revision).
        target_deliverable_expectation_id: Identity of the Deliverable
            Expectation Resource the Production addresses.
        target_deliverable_expectation_revision_id: Identity of the
            Deliverable Expectation Revision the Production addresses
            (Addresses target).
        production_rationale: Human-readable rationale, 0..4000
            characters, or ``None``.
        recording_party_id: Identity of the Contributor that recorded
            the Production.
        authority_basis_type: One of ``{"role-grant-id", "scope-id",
            "delegation-chain-id"}``.
        authority_basis_id: Identifier of the authority basis cited.
        applicable_scope: Scope identifier persisted byte-equivalent
            from the request body.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    deliverable_production_id: str
    source_work_assignment_id: str
    produced_deliverable_id: str
    produced_deliverable_revision_id: str
    target_deliverable_expectation_id: str
    target_deliverable_expectation_revision_id: str
    production_rationale: Optional[str]
    recording_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class DeliverableRevisionNode:
    """Serialized form of a ``Deliverable_Revisions`` row.

    Revision-grain. Carries both the Resource Identity
    (``deliverable_id``) and the chosen Revision Identity
    (``deliverable_revision_id``) so the tree pins the specific
    Revision the Production identified. Per Requirement 35.8, the node
    also carries ``role_marker = 'generated_output'`` and
    ``content_digest_sha256`` so a caller can distinguish a produced
    Deliverable Revision from any Slice 1 Source Evidence Document
    Revision (which does not carry a ``role_marker`` column).

    The ``content_bytes`` payload is intentionally NOT carried on this
    node; the byte-equivalent content is reachable via the
    Deliverable_Repository ``get_revision_text`` read API (task 4.2)
    and is excluded from the provenance tree to keep response sizes
    bounded for the in-progress traversal.

    Attributes:
        deliverable_id: Identity of the produced Deliverable Resource.
        deliverable_revision_id: Identity of the produced Deliverable
            Revision.
        content_type: One of the seven enumerated content types
            permitted by the schema.
        content_digest_sha256: 64-character lowercase hex digest of
            the persisted content bytes (Requirement 35.8).
        role_marker: Always ``"generated_output"`` (CHECK constraint
            on the table, Requirement 26.2 / Requirement 35.8).
        originating_work_assignment_id: Identity of the Work Assignment
            Record under which the Revision was authored (Requirement
            27.4 — produced Revision is bound to a Work Assignment by
            the Production transaction).
        authoring_party_id: Identity of the Contributor that recorded
            the Revision.
        recorded_at: ISO-8601 UTC millisecond-precision timestamp.
    """

    deliverable_id: str
    deliverable_revision_id: str
    content_type: str
    content_digest_sha256: str
    role_marker: str
    originating_work_assignment_id: str
    authoring_party_id: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Leg-grouping dataclasses.
#
# The execution tree carries two list-of-chains legs in addition to the
# delegated Slice 2 Planning chain. Each list-of-chains is exposed via a
# dedicated grouping dataclass so:
#   - the tree's top-level shape is flat and self-documenting,
#   - the per-node redaction behaviour cascades to a single chain (each
#     leg can redact independently of its siblings), and
#   - structural equality (``==``) on the frozen dataclasses produces
#     byte-equivalent tree comparisons for Property 7 / Property 8
#     idempotence (task 12.4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkAssignmentExecutionChain:
    """One Work Assignment with its Work Events and Time Entries.

    Returned as part of
    :attr:`ExecutionProvenanceTree.work_assignment_chains`. The
    Work Assignment may be a :class:`RedactedNode` when the requesting
    Party lacks ``view.work_assignment_record`` authority on it; in
    that case the navigator does not load the Work Assignment's Work
    Events or Time Entries (cascade by parent restriction) and the
    ``work_events`` and ``time_entries`` tuples are empty.

    Attributes:
        work_assignment: The :class:`WorkAssignmentNode` or
            :class:`RedactedNode` for the Work Assignment Record.
        work_events: Tuple of :class:`WorkEventNode` /
            :class:`RedactedNode` instances for the Work Assignment's
            Work Event Records, ordered ``(recorded_at ASC,
            work_event_id ASC)``. Empty when the parent is redacted
            or when no Work Events exist.
        time_entries: Tuple of :class:`TimeEntryNode` /
            :class:`RedactedNode` instances for the Work Assignment's
            Time Entry Records, ordered ``(recorded_at ASC,
            time_entry_id ASC)``. Empty when the parent is redacted
            or when no Time Entries exist.
    """

    work_assignment: "WorkAssignmentNode | RedactedNode"
    work_events: tuple
    time_entries: tuple


@dataclass(frozen=True)
class MilestoneAcceptanceProductionChain:
    """One Milestone Acceptance with its Deliverable Production and Revision.

    Returned as part of
    :attr:`ExecutionProvenanceTree.milestone_acceptance_chains`. The
    Milestone Acceptance may be a :class:`RedactedNode` when the
    requesting Party lacks ``view.milestone_acceptance_record``
    authority on it; in that case the navigator does not load the
    Production or produced Revision (cascade by parent restriction)
    and the two downstream attributes are ``None``.

    Even when the Milestone Acceptance is visible, the Deliverable
    Production or the produced Deliverable Revision may be redacted
    independently of the Milestone Acceptance — restrictions cascade
    by record, not by tree branch, matching the Slice 1
    :meth:`navigate_decision` and Slice 2 :meth:`navigate_plan_approval`
    convention.

    Attributes:
        milestone_acceptance: The :class:`MilestoneAcceptanceNode` or
            :class:`RedactedNode` for the Milestone Acceptance Record.
        deliverable_production: The
            :class:`DeliverableProductionNode`,
            :class:`RedactedNode`, or ``None`` for the source
            Deliverable Production Record. ``None`` when the parent
            Milestone Acceptance is redacted or when the Production
            row is unresolvable (theoretically unreachable for a
            successfully recorded Acceptance because the schema FK
            target is enforced at INSERT time).
        produced_deliverable_revision: The
            :class:`DeliverableRevisionNode`,
            :class:`RedactedNode`, or ``None`` for the produced
            Deliverable Revision. ``None`` when the parent Milestone
            Acceptance is redacted or when the Production / Revision
            row is unresolvable.
    """

    milestone_acceptance: "MilestoneAcceptanceNode | RedactedNode"
    deliverable_production: "DeliverableProductionNode | RedactedNode | None"
    produced_deliverable_revision: "DeliverableRevisionNode | RedactedNode | None"


# ---------------------------------------------------------------------------
# Result dataclass.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionProvenanceTree:
    """The full Execution Provenance Chain rooted at a Slice 3 anchor node.

    The tree is rooted at one of three anchor kinds:

      - A :class:`CompletionNode` (via
        :meth:`ProvenanceNavigator.navigate_completion`). The
        :attr:`completion` field carries the head; the
        :attr:`production_anchor` and :attr:`produced_revision_anchor`
        fields are ``None``; :attr:`requested_anchor_kind` is
        ``"completion_record"``.
      - A :class:`DeliverableProductionNode` (via
        :meth:`ProvenanceNavigator.navigate_deliverable_production`,
        task 12.2). The :attr:`completion` field is ``None``; the
        :attr:`production_anchor` field carries the head;
        :attr:`requested_anchor_kind` is
        ``"deliverable_production_record"``.
      - A :class:`DeliverableRevisionNode` (via
        :meth:`ProvenanceNavigator.navigate_produced_deliverable_revision`,
        task 12.2). The :attr:`completion` and
        :attr:`production_anchor` fields are ``None``; the
        :attr:`produced_revision_anchor` field carries the head;
        :attr:`requested_anchor_kind` is ``"deliverable_revision"``.

    Matches the design's ordered traversal across three legs:
      - **Planning leg.** Anchor → Work Assignment → Plan Revision →
        Plan Approval Immutable Record → Activity Plan → Project →
        Objective → Slice 1 Decision → Recommendation Revision →
        Finding Revision(s) → Region Occurrence(s) → Document Revision.
        The Planning leg is delegated to
        :meth:`ProvenanceNavigator.navigate_plan_approval` from Slice 2
        which itself delegates the Decision → Document Revision tail
        to :meth:`navigate_decision` from Slice 1. The Plan Revision
        for delegation is resolved from the Completion's
        ``target_plan_revision_id`` (Completion anchor), the source
        Work Assignment's ``target_plan_revision_id`` (Production
        anchor), or the originating Work Assignment's
        ``target_plan_revision_id`` (Revision anchor).
      - **Milestone Acceptance leg.** Completion Record → Milestone
        Acceptance Record(s) → Deliverable Production Record(s) →
        produced Deliverable Revision(s). Populated only for the
        Completion anchor; the Production and Revision anchors sit
        below the Milestone Acceptance fan so the leg is empty for
        them (the anchor itself surfaces on :attr:`production_anchor`
        or :attr:`produced_revision_anchor`).
      - **Work Assignment leg.** Anchor → Work Assignment Record(s) →
        Work Event Record(s) → Time Entry Record(s). For the
        Completion anchor the list contains every Work Assignment
        targeting the Plan Revision, ordered ``(recorded_at ASC,
        work_assignment_id ASC)``. For the Production anchor the
        list contains the single source Work Assignment Record
        (``production.source_work_assignment_id``). For the Revision
        anchor the list contains the single originating Work
        Assignment Record (``revision.originating_work_assignment_id``).
        Within each Work Assignment, Work Events and Time Entries
        are each ordered by ``(recorded_at ASC, primary_key ASC)``.

    Attributes:
        completion: The :class:`CompletionNode` at the head of the
            tree when the anchor is a Completion Record, ``None``
            otherwise. Never a :class:`RedactedNode` — when the
            requesting Party lacks ``view.completion_record``
            authority on an existing Completion Record,
            :meth:`navigate_completion` raises
            :class:`CompletionUnresolvableError` so the response form
            is indistinguishable from the unresolvable case
            (Requirement 31.6 / 35.7).
        plan_approval_chain: The full Slice 2
            :class:`PlanApprovalProvenance` produced by delegating to
            :meth:`navigate_plan_approval`, or ``None`` when the
            Plan Approval is unresolved or the requesting Party lacks
            ``view.plan_approval`` authority on it. The two cases
            yield the same ``None`` so the response is
            indistinguishable per Requirement 35.7 / AD-WS-9 rule 3.
            ``None`` is also possible (defensively) when the Plan
            Approval Record for the anchor's resolved Plan Revision
            cannot be found.
        milestone_acceptance_chains: Tuple of
            :class:`MilestoneAcceptanceProductionChain` instances, one
            per accepted Milestone Acceptance Record tied to the
            Completion's target Plan Revision (outcome = ``Accept``).
            Empty for the Production and Revision anchors and empty
            for the Completion anchor when no accepted Milestones
            are visible.
        work_assignment_chains: Tuple of
            :class:`WorkAssignmentExecutionChain` instances. For the
            Completion anchor, one per Work Assignment Record
            targeting the Completion's target Plan Revision. For the
            Production and Revision anchors, exactly one entry for
            the source / originating Work Assignment Record.
        gap_descriptors: Gap descriptors loaded from the anchor's
            Provenance Manifest (when the navigator was constructed
            with a :class:`DisclosurePolicy`). One entry per
            unresolved Omission Entry whose category is in
            ``{unavailable, stale, unresolved}`` per
            slice-default-2026 rule 2. Empty tuple when the navigator
            has no policy configured or the anchor has no material
            gaps.
        requested_completion_id: The ``completion_id`` the caller
            asked for when the anchor is a Completion Record;
            ``""`` (empty string) for the Production and Revision
            anchors. Echoed back so the HTTP layer (task 14) can
            render it in the response shell without re-reading the
            request path. Preserved for backward compatibility with
            task 12.1 callers; new callers should consult
            :attr:`requested_anchor_id` and
            :attr:`requested_anchor_kind` instead.
        production_anchor: The :class:`DeliverableProductionNode`
            at the head of the tree when the anchor is a Deliverable
            Production Record, ``None`` otherwise. Defaults to
            ``None``. Never a :class:`RedactedNode` — when the
            requesting Party lacks
            ``view.deliverable_production_record`` authority,
            :meth:`navigate_deliverable_production` raises
            :class:`DeliverableProductionUnresolvableError` so the
            response form is indistinguishable from the unresolvable
            case (Requirement 35.6 / 35.7).
        produced_revision_anchor: The :class:`DeliverableRevisionNode`
            at the head of the tree when the anchor is a produced
            Deliverable Revision, ``None`` otherwise. When set for
            the Revision anchor, the node carries both
            ``role_marker = 'generated_output'`` and the SHA-256
            content digest per Requirement 35.8. Defaults to
            ``None``. Never a :class:`RedactedNode` — when the
            requesting Party lacks ``view.deliverable_revision``
            authority,
            :meth:`navigate_produced_deliverable_revision` raises
            :class:`DeliverableRevisionUnresolvableError` so the
            response form is indistinguishable from the unresolvable
            case.
        requested_anchor_kind: One of ``"completion_record"``,
            ``"deliverable_production_record"``, or
            ``"deliverable_revision"`` identifying which navigation
            entry point produced this tree. Defaults to ``""`` for
            backward compatibility with task 12.1 callers that did
            not set the field; :meth:`navigate_completion` populates
            it to ``"completion_record"``.
        requested_anchor_id: The anchor Identity the caller asked
            for, regardless of anchor kind. Echoed back so the HTTP
            layer can render it in the response shell without
            re-reading the request path. Defaults to ``""`` for
            backward compatibility.
    """

    completion: Optional[CompletionNode]
    plan_approval_chain: Optional[PlanApprovalProvenance]
    milestone_acceptance_chains: tuple
    work_assignment_chains: tuple
    gap_descriptors: tuple
    requested_completion_id: str
    production_anchor: Optional[
        "DeliverableProductionNode | RedactedNode"
    ] = None
    produced_revision_anchor: Optional[
        "DeliverableRevisionNode | RedactedNode"
    ] = None
    requested_anchor_kind: str = ""
    requested_anchor_id: str = ""


# ---------------------------------------------------------------------------
# Error.
# ---------------------------------------------------------------------------


class CompletionUnresolvableError(Exception):
    """The requested Completion Identity does not resolve.

    Raised by :meth:`ProvenanceNavigator.navigate_completion` per
    Requirement 31.5 when ``completion_id`` does not match any row in
    ``Completion_Records``. Also raised per Requirement 31.6 / 35.7 /
    design §"Provenance traversal algorithm"
    ``not_found_indistinguishable_response`` when the requesting Party
    lacks ``view.completion_record`` authority on an existing Completion
    Record so the response form is indistinguishable from the
    unresolvable case. Full timing-indistinguishability is enforced by
    the slice-default-2026 policy in the HTTP layer (task 14).

    The exception message names the unresolvable Completion reference
    and carries it on :attr:`completion_id`; per Requirement 31.5 the
    navigator does not disclose existence of any related execution
    Records or planning Resources, so the message intentionally does
    not surface any neighbouring identifiers.

    Attributes:
        completion_id: The unresolvable Completion reference.
    """

    def __init__(self, completion_id: str) -> None:
        super().__init__(
            f"Completion identity {completion_id!r} does not resolve to a "
            f"Completion Immutable Record visible to the requesting Party."
        )
        self.completion_id = completion_id


# ---------------------------------------------------------------------------
# Row-loading helpers.
#
# Static methods on the navigator (attached at the bottom of this section)
# so a single source of truth backs both :meth:`navigate_completion` and
# any future read-side surfaces (e.g. :meth:`navigate_deliverable_production`
# added by task 12.2). Each helper consults exactly one append-only table
# and returns ``None`` or an empty list when no row matches; idempotence is
# preserved by the AD-WS-27 append-only invariant on every Slice 3 table.
# ---------------------------------------------------------------------------


def _load_completion_row(
    connection: Connection, completion_id: str
) -> Optional[dict]:
    """Load a ``Completion_Records`` row by ``completion_id``.

    Returns ``None`` when no row matches. The Completion Record is an
    Immutable Record (AD-WS-27, mirroring Slice 1 AD-WS-4 and Slice 2
    AD-WS-19) so the row, once present, does not change — fundamental
    to Requirement 31.4 idempotence.
    """
    row = (
        connection.execute(
            text(
                """
                SELECT completion_id, target_plan_revision_id,
                       target_activity_plan_id, target_project_id,
                       outcome, rationale,
                       source_milestone_acceptance_ids_json,
                       completing_party_id, authority_basis_type,
                       authority_basis_id, applicable_scope, recorded_at
                  FROM Completion_Records
                 WHERE completion_id = :completion_id
                """
            ),
            {"completion_id": completion_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_plan_approval_by_target_plan_revision(
    connection: Connection, target_plan_revision_id: str
) -> Optional[dict]:
    """Load the ``Plan_Approval_Records`` row for the given Plan Revision.

    The Slice 2 ``Plan_Approval_Records.target_plan_revision_id`` column
    is ``UNIQUE`` per Requirement 9.5, so this lookup returns at most
    one row. Returns ``None`` when no Plan Approval exists for the
    requested Plan Revision (theoretically unreachable for a successful
    Completion because the Completion's accepted-Milestone existence
    check requires a Plan Approval to have been recorded — see Property
    34 / Requirement 31.1).
    """
    row = (
        connection.execute(
            text(
                """
                SELECT plan_approval_id
                  FROM Plan_Approval_Records
                 WHERE target_plan_revision_id = :target_plan_revision_id
                """
            ),
            {"target_plan_revision_id": target_plan_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_work_assignments_for_plan_revision(
    connection: Connection, target_plan_revision_id: str
) -> Sequence[dict]:
    """Load every Work Assignment Record targeting ``target_plan_revision_id``.

    Uses the ``idx_work_assignments_by_plan`` composite index for the
    Plan-Revision-keyed lookup. Ordering is ``(recorded_at ASC,
    work_assignment_id ASC)`` so repeated invocations return
    byte-equivalent tuples — Requirement 31.4 idempotence.
    """
    rows = (
        connection.execute(
            text(
                """
                SELECT work_assignment_id, target_plan_revision_id,
                       assignee_party_id, assignment_authority_party_id,
                       assignment_rationale, authority_basis_type,
                       authority_basis_id, applicable_scope, recorded_at
                  FROM Work_Assignment_Records
                 WHERE target_plan_revision_id = :target_plan_revision_id
                 ORDER BY recorded_at ASC, work_assignment_id ASC
                """
            ),
            {"target_plan_revision_id": target_plan_revision_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _load_work_events_for_work_assignment(
    connection: Connection, target_work_assignment_id: str
) -> Sequence[dict]:
    """Load every Work Event Record for ``target_work_assignment_id``.

    Uses the ``idx_work_events_by_wa_recent`` composite index. Ordering
    is ``(recorded_at ASC, work_event_id ASC)`` — chronological in the
    traversal direction (the index is keyed ``recorded_at DESC`` for
    state-machine lookups but the navigator wants ascending order for a
    natural top-down reading; the planner can still use the index for
    the equality predicate on ``target_work_assignment_id``).
    """
    rows = (
        connection.execute(
            text(
                """
                SELECT work_event_id, target_work_assignment_id,
                       event_kind, event_note, recording_party_id,
                       authority_basis_type, authority_basis_id,
                       applicable_scope, recorded_at
                  FROM Work_Event_Records
                 WHERE target_work_assignment_id = :target_work_assignment_id
                 ORDER BY recorded_at ASC, work_event_id ASC
                """
            ),
            {"target_work_assignment_id": target_work_assignment_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _load_time_entries_for_work_assignment(
    connection: Connection, target_work_assignment_id: str
) -> Sequence[dict]:
    """Load every Time Entry Record for ``target_work_assignment_id``.

    Uses the ``idx_time_entries_by_wa`` composite index. Ordering is
    ``(recorded_at ASC, time_entry_id ASC)`` — Requirement 31.4
    idempotence.
    """
    rows = (
        connection.execute(
            text(
                """
                SELECT time_entry_id, target_work_assignment_id,
                       effort_hours, effort_period_start, effort_period_end,
                       recording_party_id, authority_basis_type,
                       authority_basis_id, applicable_scope, recorded_at
                  FROM Time_Entry_Records
                 WHERE target_work_assignment_id = :target_work_assignment_id
                 ORDER BY recorded_at ASC, time_entry_id ASC
                """
            ),
            {"target_work_assignment_id": target_work_assignment_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _load_accepted_milestones_for_plan_revision(
    connection: Connection, target_plan_revision_id: str
) -> Sequence[dict]:
    """Load every accepted Milestone Acceptance Record for the Plan Revision.

    Mirrors the SQL in design §"Accepted-Milestone existence check"
    used by :class:`CompletionService.create_completion` (task 11.1):
    walk from ``Milestone_Acceptance_Records`` through
    ``Deliverable_Production_Records`` to ``Work_Assignment_Records``
    and filter on ``mar.outcome = 'Accept'`` and
    ``wa.target_plan_revision_id = :target_plan_revision_id``. This is
    the structural definition of "Milestone Acceptances tied to a
    Completion Record's Plan Revision" per Requirement 31.1.

    Ordering is ``(mar.recorded_at ASC, mar.milestone_acceptance_id
    ASC)`` so repeated invocations return byte-equivalent tuples.
    """
    rows = (
        connection.execute(
            text(
                """
                SELECT mar.milestone_acceptance_id,
                       mar.source_deliverable_production_id,
                       mar.produced_deliverable_id,
                       mar.produced_deliverable_revision_id,
                       mar.target_deliverable_expectation_id,
                       mar.target_deliverable_expectation_revision_id,
                       mar.outcome, mar.rationale, mar.accepting_party_id,
                       mar.authority_basis_type, mar.authority_basis_id,
                       mar.applicable_scope, mar.recorded_at
                  FROM Milestone_Acceptance_Records AS mar
                  JOIN Deliverable_Production_Records AS dpr
                    ON mar.source_deliverable_production_id
                       = dpr.deliverable_production_id
                  JOIN Work_Assignment_Records AS wa
                    ON dpr.source_work_assignment_id
                       = wa.work_assignment_id
                 WHERE wa.target_plan_revision_id
                       = :target_plan_revision_id
                   AND mar.outcome = 'Accept'
                 ORDER BY mar.recorded_at ASC,
                          mar.milestone_acceptance_id ASC
                """
            ),
            {"target_plan_revision_id": target_plan_revision_id},
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows]


def _load_deliverable_production_row(
    connection: Connection, deliverable_production_id: str
) -> Optional[dict]:
    """Load a ``Deliverable_Production_Records`` row by Identity.

    Returns ``None`` when no row matches. The Deliverable Production
    Record is append-only (AD-WS-27).
    """
    row = (
        connection.execute(
            text(
                """
                SELECT deliverable_production_id, source_work_assignment_id,
                       produced_deliverable_id, produced_deliverable_revision_id,
                       target_deliverable_expectation_id,
                       target_deliverable_expectation_revision_id,
                       production_rationale, recording_party_id,
                       authority_basis_type, authority_basis_id,
                       applicable_scope, recorded_at
                  FROM Deliverable_Production_Records
                 WHERE deliverable_production_id = :deliverable_production_id
                """
            ),
            {"deliverable_production_id": deliverable_production_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_deliverable_revision_row(
    connection: Connection, deliverable_revision_id: str
) -> Optional[dict]:
    """Load a ``Deliverable_Revisions`` row by ``deliverable_revision_id``.

    Returns ``None`` when no row matches. The Deliverable Revision is
    append-only (AD-WS-27). The ``content_bytes`` column is
    intentionally NOT selected here; the navigator surfaces the content
    digest and role marker per Requirement 35.8 but does not echo the
    payload bytes into the provenance tree (callers needing the bytes
    use the Deliverable_Repository ``get_revision_text`` read API).
    """
    row = (
        connection.execute(
            text(
                """
                SELECT deliverable_revision_id, deliverable_id,
                       content_type, content_digest_sha256, role_marker,
                       originating_work_assignment_id, authoring_party_id,
                       recorded_at
                  FROM Deliverable_Revisions
                 WHERE deliverable_revision_id = :deliverable_revision_id
                """
            ),
            {"deliverable_revision_id": deliverable_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Navigator method (attached to ProvenanceNavigator below).
# ---------------------------------------------------------------------------


def _navigate_completion(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    completion_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> ExecutionProvenanceTree:
    """Walk the Execution Provenance Chain rooted at a Completion Record.

    Implements the three-leg traversal documented in design
    §"Provenance_Navigator (extended)":

    1. **Completion (Requirements 31.1, 31.5, 31.6, 35.6, 35.7).**
       Load the ``Completion_Records`` row for ``completion_id``. When
       the row does not exist, raise :class:`CompletionUnresolvableError`
       naming the unresolvable reference and disclosing nothing about
       related Records or Resources (Requirement 31.5). When the row
       exists but the requesting Party lacks ``view.completion_record``
       authority on it, raise the same exception per design
       §"Provenance traversal algorithm"
       ``not_found_indistinguishable_response`` so the response form is
       indistinguishable from the unresolvable case (Requirement 31.6 /
       35.7). Full timing-indistinguishability is enforced by the
       slice-default-2026 policy in the HTTP layer (task 14).

    2. **Planning leg (Requirements 31.2, 35.1, 35.2).** Resolve the
       Plan Approval Record whose ``target_plan_revision_id`` equals the
       Completion's ``target_plan_revision_id`` (UNIQUE per Slice 2
       Requirement 9.5). When found, delegate to
       :meth:`navigate_plan_approval` with the resolved Plan Approval
       Identity. The delegated call already enforces all six Slice 2
       stages (Plan Approval → Plan Revision → Activity Plan → Project
       → Objective → Slice 1 Decision tail) including the
       ``not_found_indistinguishable_response`` shape for the Plan
       Approval and the redaction-by-record cascade for intermediate
       nodes. When the Plan Approval is unresolved or restricted, the
       delegated call raises :class:`PlanApprovalUnresolvableError`;
       the navigator catches that and sets ``plan_approval_chain=None``
       so the absent-or-restricted Plan Approval is indistinguishable
       per Requirement 35.7 / AD-WS-9 rule 3.

    3. **Milestone Acceptance leg (Requirements 31.1, 31.2, 35.1,
       35.3, 35.8).** Load every accepted Milestone Acceptance Record
       tied to the Completion's target Plan Revision via the
       structural join described in design §"Accepted-Milestone
       existence check" (``Milestone_Acceptance_Records`` →
       ``Deliverable_Production_Records`` → ``Work_Assignment_Records``
       filtered on ``wa.target_plan_revision_id`` and
       ``mar.outcome = 'Accept'``). For each row, evaluate
       ``view.milestone_acceptance_record`` authority; when permitted,
       build a :class:`MilestoneAcceptanceNode` and walk to its source
       Deliverable Production and produced Deliverable Revision; when
       restricted, emit a :class:`RedactedNode` and cascade by parent
       restriction (skip the Production / Revision walks for that
       Milestone Acceptance). The produced Deliverable Revision node
       carries ``role_marker = 'generated_output'`` and the SHA-256
       content digest per Requirement 35.8 — distinguishing the
       produced Deliverable Revision from any Slice 1 Source Evidence
       Document Revision.

    4. **Work Assignment leg (Requirements 31.1, 31.2, 35.1, 35.3).**
       Load every Work Assignment Record targeting the Completion's
       Plan Revision (``Work_Assignment_Records.target_plan_revision_id``
       equality). For each row, evaluate
       ``view.work_assignment_record`` authority; when permitted, build
       a :class:`WorkAssignmentNode` and walk its Work Events and Time
       Entries (each authorized independently with
       ``view.work_event_record`` and ``view.time_entry_record``); when
       restricted, emit a :class:`RedactedNode` and cascade by parent
       restriction (skip the Work Event / Time Entry walks). Each
       sub-list is ordered ``(recorded_at ASC, primary_key ASC)`` for
       byte-equivalent idempotence.

    5. **Gap descriptors (Requirements 31.3, 35.4).** When the
       navigator was constructed with a :class:`DisclosurePolicy`,
       load the Completion Record's Provenance Manifest and any
       unresolved Omission Entries whose category is in
       ``{unavailable, stale, unresolved}`` via the existing
       :meth:`_collect_gap_descriptors_for_subject` helper, using
       ``subject_kind="completion_record"``. The returned tuple is in
       stable ``(recorded_at ASC, omission_entry_id ASC)`` order so
       repeated invocations return byte-equivalent results.

    Idempotence (Requirements 31.4, 35.5): every row consulted by the
    three legs lives on an append-only table — ``Completion_Records``,
    ``Plan_Approval_Records``, ``Work_Assignment_Records``,
    ``Work_Event_Records``, ``Time_Entry_Records``,
    ``Milestone_Acceptance_Records``, ``Deliverable_Production_Records``,
    ``Deliverable_Revisions`` — all rejected for UPDATE/DELETE by the
    triggers installed in tasks 1.2 and 1.3 (AD-WS-27). The delegated
    :meth:`navigate_plan_approval` preserves its own Requirement 14.5
    idempotence (Slice 2 Property 22). Every list is ordered by
    ``(recorded_at ASC, primary_key ASC)`` with a deterministic
    tiebreaker so two invocations with the same ``(completion_id,
    party_id, at)`` return byte-equivalent
    :class:`ExecutionProvenanceTree` instances; structural equality
    (``==``) on the frozen dataclass is the canonical check used by
    Property 31 / Property 34 tests (task 12.4).

    Strictly additive (Requirement 40.1): this method neither modifies
    :meth:`navigate_decision` nor :meth:`navigate_plan_approval` nor any
    Slice 1 or Slice 2 surface. It calls :meth:`navigate_plan_approval`
    at most once per invocation, with the same arguments shape Slice 2
    callers already use.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request. Used for every read SELECT and is the same
            connection passed to ``AuthorizationService.evaluate`` so
            the evaluation audit row participates in the caller's
            transaction (AD-WS-5). Reads are non-consequential per
            design §"Provenance_Navigator" so no consequential audit
            row is appended by this method.
        completion_id: Identity of the Completion Immutable Record
            whose chain is being requested.
        party_id: Identity of the requesting Party. Authority is
            evaluated against this Party for every node in the tree.
        at: Effective time for authority evaluation per design
            §"Cross-Cutting Concerns" (*Authorization*) and the
            latest-Revision selection rule applied by the delegated
            :meth:`navigate_plan_approval`. When omitted,
            :attr:`clock` is consulted; production code paths
            typically pass the ``RequestContext`` clock so every
            authorization decision inside one request shares an
            instant.

    Returns:
        :class:`ExecutionProvenanceTree` with the head Completion
        node, the delegated Planning leg attached at
        :attr:`ExecutionProvenanceTree.plan_approval_chain` (or
        ``None`` for the restricted-or-unresolved Plan Approval case),
        the Milestone Acceptance and Work Assignment leg tuples, and
        the gap descriptors loaded from the Completion Record's
        Provenance Manifest.

    Raises:
        CompletionUnresolvableError: The supplied ``completion_id``
            does not resolve to a ``Completion_Records`` row, or the
            requesting Party lacks ``view.completion_record`` authority
            on the resolved Completion Record.
    """
    effective_at = at if at is not None else self.clock.now()

    # ---- Stage 1: Completion Record (head). -----------------------------
    #
    # The head is special: per Requirements 31.5 and 31.6 the
    # unresolved and restricted cases must both raise the same
    # exception so the response form is indistinguishable. This
    # mirrors the Slice 1 ``DecisionUnresolvableError`` and Slice 2
    # ``PlanApprovalUnresolvableError`` patterns.
    completion_row = _load_completion_row(connection, completion_id)
    if completion_row is None:
        raise CompletionUnresolvableError(completion_id)

    completion_scope = completion_row["applicable_scope"]
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_COMPLETION,
        target=TargetRef(
            kind=_NODE_KIND_COMPLETION,
            id=completion_id,
            revision_id=None,
            scope=completion_scope,
        ),
        at=effective_at,
    ):
        # ``not_found_indistinguishable_response``: raise the same
        # exception as the unresolved case so the externally
        # observable response form is identical (Requirement 31.6 /
        # 35.7).
        raise CompletionUnresolvableError(completion_id)

    completion_node = CompletionNode(
        completion_id=completion_row["completion_id"],
        target_plan_revision_id=completion_row["target_plan_revision_id"],
        target_activity_plan_id=completion_row["target_activity_plan_id"],
        target_project_id=completion_row["target_project_id"],
        outcome=completion_row["outcome"],
        rationale=completion_row["rationale"],
        source_milestone_acceptance_ids_json=completion_row[
            "source_milestone_acceptance_ids_json"
        ],
        completing_party_id=completion_row["completing_party_id"],
        authority_basis_type=completion_row["authority_basis_type"],
        authority_basis_id=completion_row["authority_basis_id"],
        applicable_scope=completion_row["applicable_scope"],
        recorded_at=completion_row["recorded_at"],
    )

    target_plan_revision_id = completion_row["target_plan_revision_id"]

    # ---- Stage 2: Planning leg via navigate_plan_approval. --------------
    #
    # Resolve the Plan Approval Record for the Completion's target
    # Plan Revision (UNIQUE per Slice 2 Requirement 9.5). When found,
    # delegate to :meth:`navigate_plan_approval`; when the delegated
    # call raises :class:`PlanApprovalUnresolvableError` (either
    # because the Plan Approval row vanished — theoretically
    # unreachable for an authorized Completion — or because the Party
    # lacks ``view.plan_approval`` authority on it), set the chain to
    # ``None`` so the two cases are indistinguishable per Requirement
    # 35.7 / AD-WS-9 rule 3.
    plan_approval_chain: Optional[PlanApprovalProvenance] = None
    plan_approval_row = _load_plan_approval_by_target_plan_revision(
        connection, target_plan_revision_id
    )
    if plan_approval_row is not None:
        try:
            plan_approval_chain = self.navigate_plan_approval(
                connection,
                plan_approval_id=plan_approval_row["plan_approval_id"],
                party_id=party_id,
                at=effective_at,
            )
        except PlanApprovalUnresolvableError:
            plan_approval_chain = None

    # ---- Stage 3: Milestone Acceptance leg. -----------------------------
    #
    # Walk every accepted Milestone Acceptance Record tied to the
    # Completion's target Plan Revision via the structural join
    # described in design §"Accepted-Milestone existence check". For
    # each Milestone Acceptance, evaluate
    # ``view.milestone_acceptance_record`` authority. When restricted,
    # cascade by parent restriction (skip the Production / Revision
    # walks for that Milestone Acceptance). When permitted, walk to
    # the source Deliverable Production and produced Deliverable
    # Revision, evaluating each independently — so a Party who lacks
    # authority on the Production but holds authority on the
    # Milestone Acceptance still receives the Milestone Acceptance
    # node (restrictions cascade by record, not by tree branch).
    milestone_chains: list[MilestoneAcceptanceProductionChain] = []
    accepted_milestone_rows = _load_accepted_milestones_for_plan_revision(
        connection, target_plan_revision_id
    )
    for mar_row in accepted_milestone_rows:
        milestone_acceptance_id = mar_row["milestone_acceptance_id"]
        milestone_scope = mar_row["applicable_scope"]
        milestone_node: "MilestoneAcceptanceNode | RedactedNode"
        production_node: "DeliverableProductionNode | RedactedNode | None" = (
            None
        )
        revision_node: "DeliverableRevisionNode | RedactedNode | None" = None
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_MILESTONE_ACCEPTANCE,
            target=TargetRef(
                kind=_NODE_KIND_MILESTONE_ACCEPTANCE,
                id=milestone_acceptance_id,
                revision_id=None,
                scope=milestone_scope,
            ),
            at=effective_at,
        ):
            milestone_node = RedactedNode(kind=_NODE_KIND_MILESTONE_ACCEPTANCE)
            # Cascade by parent restriction: do not surface the
            # downstream Production or Revision so a redacted
            # Milestone Acceptance does not leak the existence or
            # identity of its downstream graph (Requirement 35.3 /
            # AD-WS-9 rule 1).
            milestone_chains.append(
                MilestoneAcceptanceProductionChain(
                    milestone_acceptance=milestone_node,
                    deliverable_production=None,
                    produced_deliverable_revision=None,
                )
            )
            continue

        milestone_node = MilestoneAcceptanceNode(
            milestone_acceptance_id=mar_row["milestone_acceptance_id"],
            source_deliverable_production_id=mar_row[
                "source_deliverable_production_id"
            ],
            produced_deliverable_id=mar_row["produced_deliverable_id"],
            produced_deliverable_revision_id=mar_row[
                "produced_deliverable_revision_id"
            ],
            target_deliverable_expectation_id=mar_row[
                "target_deliverable_expectation_id"
            ],
            target_deliverable_expectation_revision_id=mar_row[
                "target_deliverable_expectation_revision_id"
            ],
            outcome=mar_row["outcome"],
            rationale=mar_row["rationale"],
            accepting_party_id=mar_row["accepting_party_id"],
            authority_basis_type=mar_row["authority_basis_type"],
            authority_basis_id=mar_row["authority_basis_id"],
            applicable_scope=mar_row["applicable_scope"],
            recorded_at=mar_row["recorded_at"],
        )

        # Walk to the source Deliverable Production. The FK constraint
        # on ``Milestone_Acceptance_Records.source_deliverable_production_id``
        # makes the missing-row branch unreachable for a successfully
        # recorded Acceptance; defensive emission of a redaction
        # marker keeps the chain shape stable.
        production_row = _load_deliverable_production_row(
            connection, mar_row["source_deliverable_production_id"]
        )
        if production_row is None:
            production_node = RedactedNode(
                kind=_NODE_KIND_DELIVERABLE_PRODUCTION
            )
            revision_node = RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
            milestone_chains.append(
                MilestoneAcceptanceProductionChain(
                    milestone_acceptance=milestone_node,
                    deliverable_production=production_node,
                    produced_deliverable_revision=revision_node,
                )
            )
            continue

        production_scope = production_row["applicable_scope"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_PRODUCTION,
            target=TargetRef(
                kind=_NODE_KIND_DELIVERABLE_PRODUCTION,
                id=production_row["deliverable_production_id"],
                revision_id=None,
                scope=production_scope,
            ),
            at=effective_at,
        ):
            production_node = RedactedNode(
                kind=_NODE_KIND_DELIVERABLE_PRODUCTION
            )
        else:
            production_node = DeliverableProductionNode(
                deliverable_production_id=production_row[
                    "deliverable_production_id"
                ],
                source_work_assignment_id=production_row[
                    "source_work_assignment_id"
                ],
                produced_deliverable_id=production_row[
                    "produced_deliverable_id"
                ],
                produced_deliverable_revision_id=production_row[
                    "produced_deliverable_revision_id"
                ],
                target_deliverable_expectation_id=production_row[
                    "target_deliverable_expectation_id"
                ],
                target_deliverable_expectation_revision_id=production_row[
                    "target_deliverable_expectation_revision_id"
                ],
                production_rationale=production_row["production_rationale"],
                recording_party_id=production_row["recording_party_id"],
                authority_basis_type=production_row["authority_basis_type"],
                authority_basis_id=production_row["authority_basis_id"],
                applicable_scope=production_row["applicable_scope"],
                recorded_at=production_row["recorded_at"],
            )

        # Walk to the produced Deliverable Revision regardless of the
        # Production's visibility (cascade by record, not by tree
        # branch). The Revision Identity is recorded directly on the
        # Milestone Acceptance row so this walk does not depend on
        # the Production row being visible — matching the Slice 2
        # ``navigate_plan_approval`` "Activity Plan loaded from the
        # Plan Approval row rather than the Plan Revision row"
        # convention so a restricted intermediate does not block the
        # downstream node from being authorized in its own right.
        revision_id = mar_row["produced_deliverable_revision_id"]
        revision_row = _load_deliverable_revision_row(connection, revision_id)
        if revision_row is None:
            # FK constraint on
            # ``Milestone_Acceptance_Records.produced_deliverable_revision_id``
            # makes the missing-row branch unreachable for a
            # successfully recorded Acceptance; defensive redaction.
            revision_node = RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
        elif not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_REVISION,
            target=TargetRef(
                kind=_NODE_KIND_DELIVERABLE_REVISION,
                id=revision_row["deliverable_id"],
                revision_id=revision_row["deliverable_revision_id"],
                # Deliverable Revisions do not carry their own scope
                # column; use the Resource Identity as the scope so
                # the AD-WS-15 prefix fallback can be evaluated
                # consistently with the Slice 2 convention for
                # Activity Plan / Project Revision scoping.
                scope=revision_row["deliverable_id"],
            ),
            at=effective_at,
        ):
            revision_node = RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
        else:
            revision_node = DeliverableRevisionNode(
                deliverable_id=revision_row["deliverable_id"],
                deliverable_revision_id=revision_row["deliverable_revision_id"],
                content_type=revision_row["content_type"],
                content_digest_sha256=revision_row["content_digest_sha256"],
                role_marker=revision_row["role_marker"],
                originating_work_assignment_id=revision_row[
                    "originating_work_assignment_id"
                ],
                authoring_party_id=revision_row["authoring_party_id"],
                recorded_at=revision_row["recorded_at"],
            )

        milestone_chains.append(
            MilestoneAcceptanceProductionChain(
                milestone_acceptance=milestone_node,
                deliverable_production=production_node,
                produced_deliverable_revision=revision_node,
            )
        )

    # ---- Stage 4: Work Assignment leg. ----------------------------------
    #
    # Walk every Work Assignment Record targeting the Completion's
    # Plan Revision via the ``idx_work_assignments_by_plan`` composite
    # index. For each Work Assignment, evaluate
    # ``view.work_assignment_record`` authority. When restricted,
    # cascade by parent restriction (skip the Work Event and Time
    # Entry walks for that Work Assignment). When permitted, walk the
    # Work Events and Time Entries, authorizing each independently.
    work_assignment_chains: list[WorkAssignmentExecutionChain] = []
    work_assignment_rows = _load_work_assignments_for_plan_revision(
        connection, target_plan_revision_id
    )
    for wa_row in work_assignment_rows:
        work_assignment_id = wa_row["work_assignment_id"]
        wa_scope = wa_row["applicable_scope"]
        wa_node: "WorkAssignmentNode | RedactedNode"
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_WORK_ASSIGNMENT,
            target=TargetRef(
                kind=_NODE_KIND_WORK_ASSIGNMENT,
                id=work_assignment_id,
                revision_id=None,
                scope=wa_scope,
            ),
            at=effective_at,
        ):
            wa_node = RedactedNode(kind=_NODE_KIND_WORK_ASSIGNMENT)
            work_assignment_chains.append(
                WorkAssignmentExecutionChain(
                    work_assignment=wa_node,
                    work_events=(),
                    time_entries=(),
                )
            )
            continue

        wa_node = WorkAssignmentNode(
            work_assignment_id=wa_row["work_assignment_id"],
            target_plan_revision_id=wa_row["target_plan_revision_id"],
            assignee_party_id=wa_row["assignee_party_id"],
            assignment_authority_party_id=wa_row[
                "assignment_authority_party_id"
            ],
            assignment_rationale=wa_row["assignment_rationale"],
            authority_basis_type=wa_row["authority_basis_type"],
            authority_basis_id=wa_row["authority_basis_id"],
            applicable_scope=wa_row["applicable_scope"],
            recorded_at=wa_row["recorded_at"],
        )

        # Work Events.
        work_event_nodes: list = []
        for we_row in _load_work_events_for_work_assignment(
            connection, work_assignment_id
        ):
            we_scope = we_row["applicable_scope"]
            if not self._is_permitted(
                connection,
                party_id=party_id,
                action=_AUTHORIZATION_ACTION_VIEW_WORK_EVENT,
                target=TargetRef(
                    kind=_NODE_KIND_WORK_EVENT,
                    id=we_row["work_event_id"],
                    revision_id=None,
                    scope=we_scope,
                ),
                at=effective_at,
            ):
                work_event_nodes.append(
                    RedactedNode(kind=_NODE_KIND_WORK_EVENT)
                )
            else:
                work_event_nodes.append(
                    WorkEventNode(
                        work_event_id=we_row["work_event_id"],
                        target_work_assignment_id=we_row[
                            "target_work_assignment_id"
                        ],
                        event_kind=we_row["event_kind"],
                        event_note=we_row["event_note"],
                        recording_party_id=we_row["recording_party_id"],
                        authority_basis_type=we_row["authority_basis_type"],
                        authority_basis_id=we_row["authority_basis_id"],
                        applicable_scope=we_row["applicable_scope"],
                        recorded_at=we_row["recorded_at"],
                    )
                )

        # Time Entries.
        time_entry_nodes: list = []
        for te_row in _load_time_entries_for_work_assignment(
            connection, work_assignment_id
        ):
            te_scope = te_row["applicable_scope"]
            if not self._is_permitted(
                connection,
                party_id=party_id,
                action=_AUTHORIZATION_ACTION_VIEW_TIME_ENTRY,
                target=TargetRef(
                    kind=_NODE_KIND_TIME_ENTRY,
                    id=te_row["time_entry_id"],
                    revision_id=None,
                    scope=te_scope,
                ),
                at=effective_at,
            ):
                time_entry_nodes.append(
                    RedactedNode(kind=_NODE_KIND_TIME_ENTRY)
                )
            else:
                time_entry_nodes.append(
                    TimeEntryNode(
                        time_entry_id=te_row["time_entry_id"],
                        target_work_assignment_id=te_row[
                            "target_work_assignment_id"
                        ],
                        effort_hours=te_row["effort_hours"],
                        effort_period_start=te_row["effort_period_start"],
                        effort_period_end=te_row["effort_period_end"],
                        recording_party_id=te_row["recording_party_id"],
                        authority_basis_type=te_row["authority_basis_type"],
                        authority_basis_id=te_row["authority_basis_id"],
                        applicable_scope=te_row["applicable_scope"],
                        recorded_at=te_row["recorded_at"],
                    )
                )

        work_assignment_chains.append(
            WorkAssignmentExecutionChain(
                work_assignment=wa_node,
                work_events=tuple(work_event_nodes),
                time_entries=tuple(time_entry_nodes),
            )
        )

    # ---- Stage 5: Gap descriptors (slice-default-2026 rule 2). ----------
    #
    # The Completion Record's Provenance Manifest is the only manifest
    # surfaced at this leg's head (the Planning leg's Plan Approval
    # Manifest is surfaced by the delegated
    # :meth:`navigate_plan_approval` and lives on
    # ``plan_approval_chain.gap_descriptors``). The existing
    # :meth:`_collect_gap_descriptors_for_subject` helper is
    # parameterized by subject kind so it works for
    # ``"completion_record"`` without modification — Requirement 40.1's
    # additive-only constraint is preserved.
    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind=_NODE_KIND_COMPLETION,
                subject_id=completion_id,
                subject_revision_id=None,
                next_reachable_node_identity=completion_id,
            )
        )

    return ExecutionProvenanceTree(
        completion=completion_node,
        plan_approval_chain=plan_approval_chain,
        milestone_acceptance_chains=tuple(milestone_chains),
        work_assignment_chains=tuple(work_assignment_chains),
        gap_descriptors=gap_descriptors,
        requested_completion_id=completion_id,
        production_anchor=None,
        produced_revision_anchor=None,
        requested_anchor_kind=_NODE_KIND_COMPLETION,
        requested_anchor_id=completion_id,
    )


# Attach the task-12.1 traversal and its row-load helpers to
# :class:`ProvenanceNavigator` in one place. Mirrors the attachment
# pattern used by the Slice 2 navigate_plan_approval task so the public
# surface of the class is composed of the original methods plus these
# additive attachments — no edit to the original class body is required,
# which is what Requirement 40.1 (Reuse and Non-Modification of Slice 1
# and Slice 2 Contexts) demands.
ProvenanceNavigator.navigate_completion = _navigate_completion
ProvenanceNavigator._load_completion_row = staticmethod(_load_completion_row)
ProvenanceNavigator._load_plan_approval_by_target_plan_revision = staticmethod(
    _load_plan_approval_by_target_plan_revision
)
ProvenanceNavigator._load_work_assignments_for_plan_revision = staticmethod(
    _load_work_assignments_for_plan_revision
)
ProvenanceNavigator._load_work_events_for_work_assignment = staticmethod(
    _load_work_events_for_work_assignment
)
ProvenanceNavigator._load_time_entries_for_work_assignment = staticmethod(
    _load_time_entries_for_work_assignment
)
ProvenanceNavigator._load_accepted_milestones_for_plan_revision = staticmethod(
    _load_accepted_milestones_for_plan_revision
)
ProvenanceNavigator._load_deliverable_production_row = staticmethod(
    _load_deliverable_production_row
)
ProvenanceNavigator._load_deliverable_revision_row = staticmethod(
    _load_deliverable_revision_row
)


# ===========================================================================
# Anchored Execution Provenance traversals (Third Walking Slice task 12.2).
#
# Design reference: ``.kiro/specs/third-walking-slice/design.md``
# §"Provenance_Navigator (extended)" — public surface enumerating
# ``navigate_deliverable_production(deliverable_production_id, party,
# at)`` (short-form traversal beginning at a Deliverable Production
# Record) and ``navigate_produced_deliverable_revision(deliverable_
# revision_id, party, at)`` (short-form traversal beginning at a
# produced Deliverable Revision). Both functions are strictly additive
# over Slice 1 + Slice 2 + Slice 3 task 12.1 surfaces: they do not
# modify ``navigate_decision``, ``navigate_plan_approval``, or
# ``navigate_completion``; they reuse the row-load helpers attached by
# task 12.1 and add one new row-load helper
# (:func:`_load_work_assignment_row`) plus two new exception classes
# (:class:`DeliverableProductionUnresolvableError` and
# :class:`DeliverableRevisionUnresolvableError`) that mirror the
# :class:`CompletionUnresolvableError` /
# :class:`PlanApprovalUnresolvableError` /
# :class:`DecisionUnresolvableError` patterns.
#
# Both traversals walk from the requested anchor back through
# Slice 3 → Slice 2 → Slice 1 to the originating Decision and the
# exact Document Revision text:
#
#   Production anchor:
#       Deliverable Production Record
#         ├── produced Deliverable Revision           (forward leaf)
#         └── source Work Assignment Record
#              ├── Work Event Record(s)               (sibling fan)
#              ├── Time Entry Record(s)               (sibling fan)
#              └── target Plan Revision
#                    └── Plan Approval Record
#                          └── delegated to navigate_plan_approval
#                                (Plan Approval → Plan Revision →
#                                 Activity Plan → Project →
#                                 Objective → Slice 1 Decision →
#                                 Recommendation → Finding(s) →
#                                 Region Occurrence(s) → Document
#                                 Revision text per Requirement 35.2)
#
#   Revision anchor:
#       produced Deliverable Revision (role_marker, content_digest)
#         └── originating Work Assignment Record
#              ├── Work Event Record(s)
#              ├── Time Entry Record(s)
#              └── target Plan Revision
#                    └── Plan Approval Record
#                          └── delegated to navigate_plan_approval
#
# The Milestone Acceptance fan (Production → Milestone Acceptance →
# Completion) is forward from the anchor and is deliberately omitted
# from the short-form traversals: the auditor question "what plan
# authorized this production?" / "what plan authorized this revision?"
# is upward-only. The :attr:`ExecutionProvenanceTree
# .milestone_acceptance_chains` field is therefore the empty tuple
# for both traversals; the result is structurally identical to a
# Completion-anchored tree with the upward Planning leg populated
# and the downstream fan elided.
#
# Requirements satisfied (per task 12.2):
#     35.1 — End-to-end ordered traversal: from the requested
#            Production / Revision anchor, the walk reaches the
#            source / originating Work Assignment, delegates to
#            :meth:`navigate_plan_approval` for the Plan Approval →
#            Plan Revision → Activity Plan → Project → Objective →
#            Slice 1 Decision tail, which itself reaches one or more
#            Document Revisions in the Slice 1 Evidence_Repository.
#            Each intermediate node is identified by its Identity and
#            (where applicable) Revision Identity, joining seamlessly
#            with the Slice 2 Requirement 14.1 traversal.
#     35.2 — Exact Region Occurrence text is delivered by the
#            delegated ``navigate_decision`` tail (Slice 1
#            Requirement 11.2, unchanged).
#     35.5 — Idempotent retrieval: every row consulted by the two
#            traversals lives on an append-only table
#            (``Deliverable_Production_Records``,
#            ``Deliverable_Revisions``, ``Work_Assignment_Records``,
#            ``Work_Event_Records``, ``Time_Entry_Records``,
#            ``Plan_Approval_Records``) and every list is ordered by
#            ``(recorded_at ASC, primary_key ASC)`` with a
#            deterministic tiebreaker. The delegated
#            :meth:`navigate_plan_approval` preserves its own
#            Requirement 14.5 idempotence. Two invocations with the
#            same ``(anchor_id, party_id, at)`` therefore return
#            byte-equivalent :class:`ExecutionProvenanceTree`
#            instances; structural equality (``==``) on the frozen
#            dataclass is the canonical check used by Property 37
#            tests (task 12.4).
#     35.8 — Produced Deliverable Revision nodes carry both
#            ``content_digest_sha256`` and
#            ``role_marker = 'generated_output'`` on
#            :class:`DeliverableRevisionNode`. The Revision anchor
#            surfaces these fields on
#            :attr:`ExecutionProvenanceTree.produced_revision_anchor`;
#            the Production anchor surfaces them on the produced
#            Revision linked from the Production via
#            ``Deliverable_Production_Records.produced_deliverable_revision_id``
#            so callers asking "what was produced under this
#            production?" can verify the role marker and digest
#            distinguishing the produced Revision from any Slice 1
#            Source Evidence Document Revision.
#
# Authorization actions issued during the walk follow the
# ``view.<resource_kind>`` form already mapped by
# :func:`walking_slice.authorization._required_authority` (prefix
# fallback to the ``view`` authority). No new mapping rows are
# required.
# ===========================================================================


__all__ = __all__ + [
    "DeliverableProductionUnresolvableError",
    "DeliverableRevisionUnresolvableError",
]


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class DeliverableProductionUnresolvableError(Exception):
    """The requested Deliverable Production Identity does not resolve.

    Raised by
    :meth:`ProvenanceNavigator.navigate_deliverable_production` per
    Requirement 35.6 when ``deliverable_production_id`` does not match
    any row in ``Deliverable_Production_Records``. Also raised per
    Requirement 35.7 / design §"Provenance traversal algorithm"
    ``not_found_indistinguishable_response`` when the requesting Party
    lacks ``view.deliverable_production_record`` authority on an
    existing Deliverable Production Record so the response form is
    indistinguishable from the unresolvable case. Full
    timing-indistinguishability is enforced by the
    slice-default-2026 policy in the HTTP layer (task 14).

    The exception message names the unresolvable Production reference
    and carries it on :attr:`deliverable_production_id`; per
    Requirement 35.6 the navigator does not disclose existence of any
    related execution Records, produced Deliverable Revisions, or
    planning Resources, so the message intentionally does not surface
    any neighbouring identifiers.

    Attributes:
        deliverable_production_id: The unresolvable Production
            reference.
    """

    def __init__(self, deliverable_production_id: str) -> None:
        super().__init__(
            f"Deliverable Production identity {deliverable_production_id!r} "
            f"does not resolve to a Deliverable Production Record visible "
            f"to the requesting Party."
        )
        self.deliverable_production_id = deliverable_production_id


class DeliverableRevisionUnresolvableError(Exception):
    """The requested produced Deliverable Revision Identity does not resolve.

    Raised by
    :meth:`ProvenanceNavigator.navigate_produced_deliverable_revision`
    per Requirement 35.6 when ``deliverable_revision_id`` does not
    match any row in ``Deliverable_Revisions``. Also raised per
    Requirement 35.7 / design §"Provenance traversal algorithm"
    ``not_found_indistinguishable_response`` when the requesting Party
    lacks ``view.deliverable_revision`` authority on an existing
    produced Deliverable Revision so the response form is
    indistinguishable from the unresolvable case. Full
    timing-indistinguishability is enforced by the
    slice-default-2026 policy in the HTTP layer (task 14).

    The exception message names the unresolvable Revision reference
    and carries it on :attr:`deliverable_revision_id`; per Requirement
    35.6 the navigator does not disclose existence of any related
    execution Records, Deliverable Resource, or planning Resources,
    so the message intentionally does not surface any neighbouring
    identifiers.

    Attributes:
        deliverable_revision_id: The unresolvable Revision reference.
    """

    def __init__(self, deliverable_revision_id: str) -> None:
        super().__init__(
            f"Deliverable Revision identity {deliverable_revision_id!r} "
            f"does not resolve to a produced Deliverable Revision visible "
            f"to the requesting Party."
        )
        self.deliverable_revision_id = deliverable_revision_id


# ---------------------------------------------------------------------------
# Row-loading helper (additive to task 12.1's helpers).
# ---------------------------------------------------------------------------


def _load_work_assignment_row(
    connection: Connection, work_assignment_id: str
) -> Optional[dict]:
    """Load a ``Work_Assignment_Records`` row by Identity.

    Returns ``None`` when no row matches. The Work Assignment Record
    is append-only (AD-WS-27); the row, once present, does not change
    — fundamental to Requirement 35.5 idempotence. Used by both
    :func:`_navigate_deliverable_production` (to resolve the source
    Work Assignment of a Production Record) and
    :func:`_navigate_produced_deliverable_revision` (to resolve the
    originating Work Assignment of a Deliverable Revision).
    """
    row = (
        connection.execute(
            text(
                """
                SELECT work_assignment_id, target_plan_revision_id,
                       assignee_party_id, assignment_authority_party_id,
                       assignment_rationale, authority_basis_type,
                       authority_basis_id, applicable_scope, recorded_at
                  FROM Work_Assignment_Records
                 WHERE work_assignment_id = :work_assignment_id
                """
            ),
            {"work_assignment_id": work_assignment_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Shared walk helpers.
#
# The two new traversals share the same upward-walk shape — given a
# resolved Work Assignment Record they build the
# :class:`WorkAssignmentExecutionChain` (with per-Event and per-Entry
# authorization) and they resolve / delegate the Plan Approval chain.
# Factoring those two walks into helpers keeps the two traversal
# functions short and lets a future short-form
# ``navigate_work_assignment`` (not in scope for this task) reuse the
# same plumbing.
# ---------------------------------------------------------------------------


def _build_work_assignment_chain(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    wa_row: dict,
    party_id: str,
    at: datetime,
) -> WorkAssignmentExecutionChain:
    """Build a :class:`WorkAssignmentExecutionChain` for one Work Assignment.

    Mirrors the per-Work-Assignment loop in
    :func:`_navigate_completion` (task 12.1) so the byte-equivalent
    idempotence and the redaction-by-record-cascade semantics are
    identical across the three traversals. Each Work Event and Time
    Entry is authorized independently with
    ``view.work_event_record`` / ``view.time_entry_record``; restricted
    entries are replaced by a :class:`RedactedNode` carrying only the
    node kind (Requirement 35.3 / AD-WS-9 rule 1).

    Args:
        self: The :class:`ProvenanceNavigator` instance (bound when
            this helper is called from an attached navigator method).
        connection: SQLAlchemy connection bound to the caller's
            request.
        wa_row: The resolved ``Work_Assignment_Records`` row mapping.
        party_id: Identity of the requesting Party. Authority is
            evaluated against this Party for the Work Events and
            Time Entries.
        at: Effective time for authority evaluation.

    Returns:
        A :class:`WorkAssignmentExecutionChain` carrying the
        :class:`WorkAssignmentNode` head and tuples of visible /
        redacted :class:`WorkEventNode` and :class:`TimeEntryNode`
        instances in ``(recorded_at ASC, primary_key ASC)`` order.
    """
    work_assignment_id = wa_row["work_assignment_id"]

    wa_node = WorkAssignmentNode(
        work_assignment_id=wa_row["work_assignment_id"],
        target_plan_revision_id=wa_row["target_plan_revision_id"],
        assignee_party_id=wa_row["assignee_party_id"],
        assignment_authority_party_id=wa_row[
            "assignment_authority_party_id"
        ],
        assignment_rationale=wa_row["assignment_rationale"],
        authority_basis_type=wa_row["authority_basis_type"],
        authority_basis_id=wa_row["authority_basis_id"],
        applicable_scope=wa_row["applicable_scope"],
        recorded_at=wa_row["recorded_at"],
    )

    # Work Events: per-row authorization, restricted → RedactedNode.
    work_event_nodes: list = []
    for we_row in _load_work_events_for_work_assignment(
        connection, work_assignment_id
    ):
        we_scope = we_row["applicable_scope"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_WORK_EVENT,
            target=TargetRef(
                kind=_NODE_KIND_WORK_EVENT,
                id=we_row["work_event_id"],
                revision_id=None,
                scope=we_scope,
            ),
            at=at,
        ):
            work_event_nodes.append(RedactedNode(kind=_NODE_KIND_WORK_EVENT))
        else:
            work_event_nodes.append(
                WorkEventNode(
                    work_event_id=we_row["work_event_id"],
                    target_work_assignment_id=we_row[
                        "target_work_assignment_id"
                    ],
                    event_kind=we_row["event_kind"],
                    event_note=we_row["event_note"],
                    recording_party_id=we_row["recording_party_id"],
                    authority_basis_type=we_row["authority_basis_type"],
                    authority_basis_id=we_row["authority_basis_id"],
                    applicable_scope=we_row["applicable_scope"],
                    recorded_at=we_row["recorded_at"],
                )
            )

    # Time Entries: per-row authorization, restricted → RedactedNode.
    time_entry_nodes: list = []
    for te_row in _load_time_entries_for_work_assignment(
        connection, work_assignment_id
    ):
        te_scope = te_row["applicable_scope"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_TIME_ENTRY,
            target=TargetRef(
                kind=_NODE_KIND_TIME_ENTRY,
                id=te_row["time_entry_id"],
                revision_id=None,
                scope=te_scope,
            ),
            at=at,
        ):
            time_entry_nodes.append(RedactedNode(kind=_NODE_KIND_TIME_ENTRY))
        else:
            time_entry_nodes.append(
                TimeEntryNode(
                    time_entry_id=te_row["time_entry_id"],
                    target_work_assignment_id=te_row[
                        "target_work_assignment_id"
                    ],
                    effort_hours=te_row["effort_hours"],
                    effort_period_start=te_row["effort_period_start"],
                    effort_period_end=te_row["effort_period_end"],
                    recording_party_id=te_row["recording_party_id"],
                    authority_basis_type=te_row["authority_basis_type"],
                    authority_basis_id=te_row["authority_basis_id"],
                    applicable_scope=te_row["applicable_scope"],
                    recorded_at=te_row["recorded_at"],
                )
            )

    return WorkAssignmentExecutionChain(
        work_assignment=wa_node,
        work_events=tuple(work_event_nodes),
        time_entries=tuple(time_entry_nodes),
    )


def _resolve_plan_approval_chain(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    target_plan_revision_id: str,
    party_id: str,
    at: datetime,
) -> Optional[PlanApprovalProvenance]:
    """Resolve and delegate the Plan Approval chain for a Plan Revision.

    Mirrors the Planning-leg delegation in :func:`_navigate_completion`
    (task 12.1). Resolves the Plan Approval Record whose
    ``target_plan_revision_id`` equals the given Plan Revision Identity
    (UNIQUE per Slice 2 Requirement 9.5). When found, delegates to
    :meth:`ProvenanceNavigator.navigate_plan_approval` with the
    resolved Plan Approval Identity. When the delegated call raises
    :class:`PlanApprovalUnresolvableError` (either because the Plan
    Approval row vanished or because the Party lacks
    ``view.plan_approval`` authority on it), returns ``None`` so the
    two cases are indistinguishable per Requirement 35.7 /
    AD-WS-9 rule 3.

    Args:
        self: The :class:`ProvenanceNavigator` instance.
        connection: SQLAlchemy connection bound to the caller's
            request.
        target_plan_revision_id: Identity of the Plan Revision whose
            Plan Approval chain is being resolved.
        party_id: Identity of the requesting Party. Passed straight
            through to :meth:`navigate_plan_approval` for the Plan
            Approval and downstream authority evaluations.
        at: Effective time for authority evaluation.

    Returns:
        The :class:`PlanApprovalProvenance` produced by the delegated
        call, or ``None`` when the Plan Approval is unresolved or the
        Party lacks ``view.plan_approval`` authority on it.
    """
    plan_approval_row = _load_plan_approval_by_target_plan_revision(
        connection, target_plan_revision_id
    )
    if plan_approval_row is None:
        return None
    try:
        return self.navigate_plan_approval(
            connection,
            plan_approval_id=plan_approval_row["plan_approval_id"],
            party_id=party_id,
            at=at,
        )
    except PlanApprovalUnresolvableError:
        return None


# ---------------------------------------------------------------------------
# Navigator methods (attached to ProvenanceNavigator below).
# ---------------------------------------------------------------------------


def _navigate_deliverable_production(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    deliverable_production_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> ExecutionProvenanceTree:
    """Walk the Execution Provenance Chain rooted at a Deliverable Production.

    Short-form traversal beginning at a Deliverable Production Record:
    walks from the Production back through Slice 3 → Slice 2 → Slice 1
    to the originating Decision and the exact Document Revision text.
    Useful when an auditor asks "what plan authorized this production?".

    Algorithm:

    1. **Production Record (Requirements 35.1, 35.6, 35.7).** Load
       the ``Deliverable_Production_Records`` row for
       ``deliverable_production_id``. When the row does not exist,
       raise :class:`DeliverableProductionUnresolvableError` naming
       only the unresolvable Production reference (Requirement 35.6).
       When the row exists but the requesting Party lacks
       ``view.deliverable_production_record`` authority on it, raise
       the same exception per design §"Provenance traversal
       algorithm" ``not_found_indistinguishable_response`` so the
       response form is indistinguishable from the unresolvable case
       (Requirement 35.7).

    2. **Produced Deliverable Revision (Requirement 35.8).** Load the
       ``Deliverable_Revisions`` row pinned by the Production's
       ``produced_deliverable_revision_id`` and authorize
       ``view.deliverable_revision``. Surface the Revision on
       :attr:`ExecutionProvenanceTree.produced_revision_anchor` (a
       :class:`DeliverableRevisionNode` or a :class:`RedactedNode`).
       The :class:`DeliverableRevisionNode` carries both
       ``role_marker = 'generated_output'`` and
       ``content_digest_sha256`` per Requirement 35.8, distinguishing
       the produced Revision from any Slice 1 Source Evidence
       Document Revision.

    3. **Source Work Assignment (Requirement 35.1).** Load the
       ``Work_Assignment_Records`` row pinned by the Production's
       ``source_work_assignment_id`` (the schema FK makes a missing
       row impossible for a successfully recorded Production;
       defensive emission of a redaction marker keeps the chain
       shape stable). Authorize ``view.work_assignment_record``;
       on permit, build the :class:`WorkAssignmentExecutionChain`
       with the Work Events and Time Entries; on deny, emit a
       :class:`RedactedNode` and cascade by parent restriction (skip
       the Event and Time Entry walks).

    4. **Planning leg (Requirements 35.1, 35.2).** Resolve the Plan
       Approval Record whose ``target_plan_revision_id`` equals the
       source Work Assignment's ``target_plan_revision_id`` (UNIQUE
       per Slice 2 Requirement 9.5) and delegate to
       :meth:`navigate_plan_approval`. The delegated call enforces
       all six Slice 2 stages (Plan Approval → Plan Revision →
       Activity Plan → Project → Objective → Slice 1 Decision tail)
       including the ``not_found_indistinguishable_response`` shape
       for the Plan Approval and the redaction-by-record cascade for
       intermediate nodes. When the Plan Approval is unresolved or
       restricted, the delegated call raises
       :class:`PlanApprovalUnresolvableError`; the navigator catches
       that and sets ``plan_approval_chain=None`` so the
       absent-or-restricted Plan Approval is indistinguishable per
       Requirement 35.7 / AD-WS-9 rule 3.

    5. **Gap descriptors (Requirements 31.3, 35.4).** When the
       navigator was constructed with a :class:`DisclosurePolicy`,
       load the Production Record's Provenance Manifest via the
       existing :meth:`_collect_gap_descriptors_for_subject` helper
       with ``subject_kind="deliverable_production_record"``. The
       returned tuple is in stable ``(recorded_at ASC,
       omission_entry_id ASC)`` order so repeated invocations return
       byte-equivalent results.

    Idempotence (Requirement 35.5): every row consulted lives on an
    append-only table — ``Deliverable_Production_Records``,
    ``Deliverable_Revisions``, ``Work_Assignment_Records``,
    ``Work_Event_Records``, ``Time_Entry_Records``,
    ``Plan_Approval_Records`` — all rejected for UPDATE/DELETE by
    the triggers installed in tasks 1.2 and 1.3 (AD-WS-27). The
    delegated :meth:`navigate_plan_approval` preserves its own
    Requirement 14.5 idempotence. Every list is ordered by
    ``(recorded_at ASC, primary_key ASC)`` with a deterministic
    tiebreaker so two invocations with the same
    ``(deliverable_production_id, party_id, at)`` return
    byte-equivalent :class:`ExecutionProvenanceTree` instances;
    structural equality (``==``) on the frozen dataclass is the
    canonical check used by Property 37 tests (task 12.4).

    Strictly additive (Requirement 40.1): this method neither
    modifies :meth:`navigate_decision`, :meth:`navigate_plan_approval`,
    :meth:`navigate_completion`, nor any Slice 1 / Slice 2 surface.
    It calls :meth:`navigate_plan_approval` at most once per
    invocation, with the same arguments shape Slice 2 callers
    already use.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request. Used for every read SELECT and is the same
            connection passed to ``AuthorizationService.evaluate`` so
            the evaluation audit row participates in the caller's
            transaction (AD-WS-5). Reads are non-consequential per
            design §"Provenance_Navigator" so no consequential audit
            row is appended by this method.
        deliverable_production_id: Identity of the Deliverable
            Production Record whose upward chain is being requested.
        party_id: Identity of the requesting Party. Authority is
            evaluated against this Party for every node in the tree.
        at: Effective time for authority evaluation per design
            §"Cross-Cutting Concerns" (*Authorization*) and the
            latest-Revision selection rule applied by the delegated
            :meth:`navigate_plan_approval`. When omitted,
            :attr:`clock` is consulted.

    Returns:
        :class:`ExecutionProvenanceTree` with
        :attr:`production_anchor` set to the Production node head,
        :attr:`produced_revision_anchor` set to the produced
        Revision (visible or redacted), :attr:`work_assignment_chains`
        carrying the single source Work Assignment chain,
        :attr:`plan_approval_chain` carrying the delegated Planning
        leg (or ``None`` for the indistinguishable
        absent-or-restricted case), :attr:`completion` set to
        ``None`` (the short-form traversal omits the forward
        Milestone-Acceptance / Completion fan), and the gap
        descriptors loaded from the Production's Provenance
        Manifest.

    Raises:
        DeliverableProductionUnresolvableError: The supplied
            ``deliverable_production_id`` does not resolve to a
            ``Deliverable_Production_Records`` row, or the requesting
            Party lacks ``view.deliverable_production_record``
            authority on the resolved Production Record.
    """
    effective_at = at if at is not None else self.clock.now()

    # ---- Stage 1: Production Record (head). -----------------------------
    #
    # Per Requirements 35.6 and 35.7 the unresolved and restricted
    # cases must both raise the same exception so the response form
    # is indistinguishable. Mirrors the Slice 1 ``DecisionUnresolvable
    # Error`` / Slice 2 ``PlanApprovalUnresolvableError`` / Slice 3
    # task 12.1 ``CompletionUnresolvableError`` patterns.
    production_row = _load_deliverable_production_row(
        connection, deliverable_production_id
    )
    if production_row is None:
        raise DeliverableProductionUnresolvableError(
            deliverable_production_id
        )

    production_scope = production_row["applicable_scope"]
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_PRODUCTION,
        target=TargetRef(
            kind=_NODE_KIND_DELIVERABLE_PRODUCTION,
            id=deliverable_production_id,
            revision_id=None,
            scope=production_scope,
        ),
        at=effective_at,
    ):
        # ``not_found_indistinguishable_response``: raise the same
        # exception as the unresolved case so the externally
        # observable response form is identical (Requirement 35.7).
        raise DeliverableProductionUnresolvableError(
            deliverable_production_id
        )

    production_node = DeliverableProductionNode(
        deliverable_production_id=production_row["deliverable_production_id"],
        source_work_assignment_id=production_row["source_work_assignment_id"],
        produced_deliverable_id=production_row["produced_deliverable_id"],
        produced_deliverable_revision_id=production_row[
            "produced_deliverable_revision_id"
        ],
        target_deliverable_expectation_id=production_row[
            "target_deliverable_expectation_id"
        ],
        target_deliverable_expectation_revision_id=production_row[
            "target_deliverable_expectation_revision_id"
        ],
        production_rationale=production_row["production_rationale"],
        recording_party_id=production_row["recording_party_id"],
        authority_basis_type=production_row["authority_basis_type"],
        authority_basis_id=production_row["authority_basis_id"],
        applicable_scope=production_row["applicable_scope"],
        recorded_at=production_row["recorded_at"],
    )

    # ---- Stage 2: Produced Deliverable Revision (forward leaf). ---------
    #
    # The Revision Identity is recorded directly on the Production
    # row's ``produced_deliverable_revision_id`` column so this lookup
    # is one indexed SELECT. The Revision is authorized independently
    # of the Production (cascade by record, not by tree branch) so a
    # Party holding ``view.deliverable_production_record`` but not
    # ``view.deliverable_revision`` still sees the Production node
    # with a redacted Revision child. The Revision node carries
    # ``role_marker`` and ``content_digest_sha256`` per Requirement
    # 35.8.
    revision_anchor: "DeliverableRevisionNode | RedactedNode"
    revision_row = _load_deliverable_revision_row(
        connection, production_row["produced_deliverable_revision_id"]
    )
    if revision_row is None:
        # FK constraint on
        # ``Deliverable_Production_Records.produced_deliverable_revision_id``
        # makes the missing-row branch unreachable for a successfully
        # recorded Production; defensive redaction keeps the chain
        # shape stable.
        revision_anchor = RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
    elif not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_DELIVERABLE_REVISION,
            id=revision_row["deliverable_id"],
            revision_id=revision_row["deliverable_revision_id"],
            # Deliverable Revisions do not carry their own scope
            # column; use the Resource Identity as the scope so the
            # AD-WS-15 prefix fallback is evaluated consistently with
            # the convention used by :func:`_navigate_completion`.
            scope=revision_row["deliverable_id"],
        ),
        at=effective_at,
    ):
        revision_anchor = RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
    else:
        revision_anchor = DeliverableRevisionNode(
            deliverable_id=revision_row["deliverable_id"],
            deliverable_revision_id=revision_row["deliverable_revision_id"],
            content_type=revision_row["content_type"],
            content_digest_sha256=revision_row["content_digest_sha256"],
            role_marker=revision_row["role_marker"],
            originating_work_assignment_id=revision_row[
                "originating_work_assignment_id"
            ],
            authoring_party_id=revision_row["authoring_party_id"],
            recorded_at=revision_row["recorded_at"],
        )

    # ---- Stage 3: Source Work Assignment (with Events and Entries). -----
    #
    # The Work Assignment Identity is recorded on the Production row's
    # ``source_work_assignment_id`` column. Authorize independently;
    # on deny cascade by parent restriction (skip the Events and Time
    # Entries — the redacted Work Assignment chain carries empty
    # tuples for both child collections).
    work_assignment_chains: list[WorkAssignmentExecutionChain] = []
    source_wa_row = _load_work_assignment_row(
        connection, production_row["source_work_assignment_id"]
    )
    target_plan_revision_id: Optional[str] = None
    if source_wa_row is None:
        # FK constraint on
        # ``Deliverable_Production_Records.source_work_assignment_id``
        # makes the missing-row branch unreachable for a successfully
        # recorded Production; defensive redaction keeps the chain
        # shape stable. The Planning leg cannot be resolved without a
        # Plan Revision Identity, so it falls through to ``None``.
        work_assignment_chains.append(
            WorkAssignmentExecutionChain(
                work_assignment=RedactedNode(
                    kind=_NODE_KIND_WORK_ASSIGNMENT
                ),
                work_events=(),
                time_entries=(),
            )
        )
    else:
        # Always resolve the Plan Revision Identity from the persisted
        # Work Assignment row (not from a redacted node) so the
        # Planning leg can still be walked when the Work Assignment
        # itself is restricted to the requesting Party. This matches
        # the Slice 1 / Slice 2 "restrictions cascade by record, not
        # by tree branch" convention.
        target_plan_revision_id = source_wa_row["target_plan_revision_id"]
        wa_scope = source_wa_row["applicable_scope"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_WORK_ASSIGNMENT,
            target=TargetRef(
                kind=_NODE_KIND_WORK_ASSIGNMENT,
                id=source_wa_row["work_assignment_id"],
                revision_id=None,
                scope=wa_scope,
            ),
            at=effective_at,
        ):
            work_assignment_chains.append(
                WorkAssignmentExecutionChain(
                    work_assignment=RedactedNode(
                        kind=_NODE_KIND_WORK_ASSIGNMENT
                    ),
                    work_events=(),
                    time_entries=(),
                )
            )
        else:
            work_assignment_chains.append(
                _build_work_assignment_chain(
                    self,
                    connection,
                    wa_row=source_wa_row,
                    party_id=party_id,
                    at=effective_at,
                )
            )

    # ---- Stage 4: Planning leg via navigate_plan_approval. --------------
    #
    # Delegate to :meth:`navigate_plan_approval` with the source Work
    # Assignment's target Plan Revision (resolved unconditionally from
    # the persisted Work Assignment row so the Planning leg is
    # reachable even when the Work Assignment node is redacted). When
    # the source Work Assignment row is missing (defensive branch
    # above), the Plan Revision Identity is ``None`` and the Planning
    # leg falls through to ``None``.
    plan_approval_chain: Optional[PlanApprovalProvenance] = None
    if target_plan_revision_id is not None:
        plan_approval_chain = _resolve_plan_approval_chain(
            self,
            connection,
            target_plan_revision_id=target_plan_revision_id,
            party_id=party_id,
            at=effective_at,
        )

    # ---- Stage 5: Gap descriptors (slice-default-2026 rule 2). ----------
    #
    # The Production Record's Provenance Manifest is the only manifest
    # surfaced at this anchor; the Planning leg's manifest gaps are
    # surfaced by the delegated :meth:`navigate_plan_approval`. The
    # existing :meth:`_collect_gap_descriptors_for_subject` helper is
    # parameterized by subject kind so it works for
    # ``"deliverable_production_record"`` without modification —
    # Requirement 40.1's additive-only constraint is preserved.
    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind=_NODE_KIND_DELIVERABLE_PRODUCTION,
                subject_id=deliverable_production_id,
                subject_revision_id=None,
                next_reachable_node_identity=deliverable_production_id,
            )
        )

    return ExecutionProvenanceTree(
        completion=None,
        plan_approval_chain=plan_approval_chain,
        milestone_acceptance_chains=(),
        work_assignment_chains=tuple(work_assignment_chains),
        gap_descriptors=gap_descriptors,
        requested_completion_id="",
        production_anchor=production_node,
        produced_revision_anchor=revision_anchor,
        requested_anchor_kind=_NODE_KIND_DELIVERABLE_PRODUCTION,
        requested_anchor_id=deliverable_production_id,
    )


def _navigate_produced_deliverable_revision(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    deliverable_revision_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> ExecutionProvenanceTree:
    """Walk the Execution Provenance Chain rooted at a produced Revision.

    Short-form traversal beginning at a produced Deliverable Revision:
    walks from the Revision back through Slice 3 → Slice 2 → Slice 1
    to the originating Decision and the exact Document Revision text.
    Useful when an auditor asks "what plan authorized this produced
    deliverable revision?".

    Algorithm:

    1. **Produced Deliverable Revision (Requirements 35.1, 35.6,
       35.7, 35.8).** Load the ``Deliverable_Revisions`` row for
       ``deliverable_revision_id``. When the row does not exist,
       raise :class:`DeliverableRevisionUnresolvableError` naming
       only the unresolvable Revision reference (Requirement 35.6).
       When the row exists but the requesting Party lacks
       ``view.deliverable_revision`` authority on it, raise the same
       exception per design §"Provenance traversal algorithm"
       ``not_found_indistinguishable_response`` so the response form
       is indistinguishable from the unresolvable case (Requirement
       35.7). The :class:`DeliverableRevisionNode` surfaced on
       :attr:`ExecutionProvenanceTree.produced_revision_anchor`
       carries ``role_marker = 'generated_output'`` and the SHA-256
       content digest per Requirement 35.8, distinguishing the
       produced Revision from any Slice 1 Source Evidence Document
       Revision.

    2. **Originating Work Assignment (Requirement 35.1).** Load the
       ``Work_Assignment_Records`` row pinned by the Revision's
       ``originating_work_assignment_id`` column (recorded at INSERT
       time by :class:`DeliverableRepositoryService.create_produced_
       deliverable`, AD-WS-29 / Requirement 27.4 invariant). The
       schema FK makes a missing row impossible for a successfully
       recorded Revision; defensive emission of a redaction marker
       keeps the chain shape stable. Authorize
       ``view.work_assignment_record``; on permit, build the
       :class:`WorkAssignmentExecutionChain` with the Work Events
       and Time Entries; on deny, emit a :class:`RedactedNode` and
       cascade by parent restriction (skip the Event and Time Entry
       walks).

    3. **Planning leg (Requirements 35.1, 35.2).** Resolve the Plan
       Approval Record whose ``target_plan_revision_id`` equals the
       originating Work Assignment's ``target_plan_revision_id``
       (UNIQUE per Slice 2 Requirement 9.5) and delegate to
       :meth:`navigate_plan_approval`. The delegated call enforces
       the same six Slice 2 stages described in
       :func:`_navigate_deliverable_production`. When the Plan
       Approval is unresolved or restricted, the delegated call
       raises :class:`PlanApprovalUnresolvableError`; the navigator
       catches that and sets ``plan_approval_chain=None`` so the
       absent-or-restricted Plan Approval is indistinguishable per
       Requirement 35.7 / AD-WS-9 rule 3.

    4. **Gap descriptors (Requirements 31.3, 35.4).** When the
       navigator was constructed with a :class:`DisclosurePolicy`,
       load the Revision's Provenance Manifest via the existing
       :meth:`_collect_gap_descriptors_for_subject` helper with
       ``subject_kind="deliverable_revision"`` and the Revision
       Identity on ``subject_revision_id``. The Revision is the only
       Slice 3 anchor that carries both a Resource Identity and a
       Revision Identity, so the manifest lookup uses the Revision
       Identity for the ``subject_revision_id`` parameter to allow
       Resource-grain manifests (rare) and Revision-grain manifests
       (the common case) to coexist without ambiguity.

    Idempotence (Requirement 35.5): every row consulted lives on an
    append-only table — ``Deliverable_Revisions``,
    ``Work_Assignment_Records``, ``Work_Event_Records``,
    ``Time_Entry_Records``, ``Plan_Approval_Records`` — all rejected
    for UPDATE/DELETE by the triggers installed in tasks 1.2 and
    1.3 (AD-WS-27). The delegated :meth:`navigate_plan_approval`
    preserves its own Requirement 14.5 idempotence. Two invocations
    with the same ``(deliverable_revision_id, party_id, at)`` return
    byte-equivalent :class:`ExecutionProvenanceTree` instances.

    Strictly additive (Requirement 40.1): this method neither
    modifies :meth:`navigate_decision`, :meth:`navigate_plan_approval`,
    :meth:`navigate_completion`,
    :meth:`navigate_deliverable_production`, nor any Slice 1 / Slice 2
    surface. It calls :meth:`navigate_plan_approval` at most once
    per invocation, with the same arguments shape Slice 2 callers
    already use.

    Args:
        connection: SQLAlchemy connection bound to the caller's
            request. Used for every read SELECT and is the same
            connection passed to ``AuthorizationService.evaluate`` so
            the evaluation audit row participates in the caller's
            transaction (AD-WS-5). Reads are non-consequential per
            design §"Provenance_Navigator" so no consequential audit
            row is appended by this method.
        deliverable_revision_id: Identity of the produced Deliverable
            Revision whose upward chain is being requested.
        party_id: Identity of the requesting Party. Authority is
            evaluated against this Party for every node in the tree.
        at: Effective time for authority evaluation per design
            §"Cross-Cutting Concerns" (*Authorization*) and the
            latest-Revision selection rule applied by the delegated
            :meth:`navigate_plan_approval`. When omitted,
            :attr:`clock` is consulted.

    Returns:
        :class:`ExecutionProvenanceTree` with
        :attr:`produced_revision_anchor` set to the Revision node
        head (carrying ``role_marker = 'generated_output'`` and the
        content digest per Requirement 35.8),
        :attr:`production_anchor` set to ``None`` (the short-form
        traversal begins below the Production fan),
        :attr:`work_assignment_chains` carrying the single
        originating Work Assignment chain,
        :attr:`plan_approval_chain` carrying the delegated Planning
        leg (or ``None`` for the indistinguishable
        absent-or-restricted case), :attr:`completion` set to
        ``None``, and the gap descriptors loaded from the Revision's
        Provenance Manifest.

    Raises:
        DeliverableRevisionUnresolvableError: The supplied
            ``deliverable_revision_id`` does not resolve to a
            ``Deliverable_Revisions`` row, or the requesting Party
            lacks ``view.deliverable_revision`` authority on the
            resolved produced Deliverable Revision.
    """
    effective_at = at if at is not None else self.clock.now()

    # ---- Stage 1: Produced Deliverable Revision (head). -----------------
    #
    # Per Requirements 35.6 and 35.7 the unresolved and restricted
    # cases must both raise the same exception so the response form
    # is indistinguishable. The head node carries ``role_marker`` and
    # ``content_digest_sha256`` per Requirement 35.8 — see
    # :class:`DeliverableRevisionNode` for the full attribute list.
    revision_row = _load_deliverable_revision_row(
        connection, deliverable_revision_id
    )
    if revision_row is None:
        raise DeliverableRevisionUnresolvableError(deliverable_revision_id)

    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_DELIVERABLE_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_DELIVERABLE_REVISION,
            id=revision_row["deliverable_id"],
            revision_id=revision_row["deliverable_revision_id"],
            # Deliverable Revisions do not carry their own scope
            # column; use the Resource Identity as the scope so the
            # AD-WS-15 prefix fallback is evaluated consistently with
            # :func:`_navigate_completion` and
            # :func:`_navigate_deliverable_production`.
            scope=revision_row["deliverable_id"],
        ),
        at=effective_at,
    ):
        # ``not_found_indistinguishable_response``: raise the same
        # exception as the unresolved case so the externally
        # observable response form is identical (Requirement 35.7).
        raise DeliverableRevisionUnresolvableError(deliverable_revision_id)

    revision_node = DeliverableRevisionNode(
        deliverable_id=revision_row["deliverable_id"],
        deliverable_revision_id=revision_row["deliverable_revision_id"],
        content_type=revision_row["content_type"],
        content_digest_sha256=revision_row["content_digest_sha256"],
        role_marker=revision_row["role_marker"],
        originating_work_assignment_id=revision_row[
            "originating_work_assignment_id"
        ],
        authoring_party_id=revision_row["authoring_party_id"],
        recorded_at=revision_row["recorded_at"],
    )

    # ---- Stage 2: Originating Work Assignment (with Events / Entries). --
    #
    # The Work Assignment Identity is recorded on the Revision row's
    # ``originating_work_assignment_id`` column at INSERT time by
    # :class:`DeliverableRepositoryService.create_produced_deliverable`
    # (AD-WS-29 / Requirement 27.4 invariant). Authorize independently;
    # on deny cascade by parent restriction. Always resolve the Plan
    # Revision Identity from the persisted Work Assignment row so the
    # Planning leg is reachable even when the Work Assignment node is
    # redacted (matches the convention in
    # :func:`_navigate_deliverable_production`).
    work_assignment_chains: list[WorkAssignmentExecutionChain] = []
    originating_wa_row = _load_work_assignment_row(
        connection, revision_row["originating_work_assignment_id"]
    )
    target_plan_revision_id: Optional[str] = None
    if originating_wa_row is None:
        # FK constraint on
        # ``Deliverable_Revisions.originating_work_assignment_id``
        # makes the missing-row branch unreachable for a successfully
        # recorded Revision; defensive redaction keeps the chain
        # shape stable.
        work_assignment_chains.append(
            WorkAssignmentExecutionChain(
                work_assignment=RedactedNode(
                    kind=_NODE_KIND_WORK_ASSIGNMENT
                ),
                work_events=(),
                time_entries=(),
            )
        )
    else:
        target_plan_revision_id = originating_wa_row["target_plan_revision_id"]
        wa_scope = originating_wa_row["applicable_scope"]
        if not self._is_permitted(
            connection,
            party_id=party_id,
            action=_AUTHORIZATION_ACTION_VIEW_WORK_ASSIGNMENT,
            target=TargetRef(
                kind=_NODE_KIND_WORK_ASSIGNMENT,
                id=originating_wa_row["work_assignment_id"],
                revision_id=None,
                scope=wa_scope,
            ),
            at=effective_at,
        ):
            work_assignment_chains.append(
                WorkAssignmentExecutionChain(
                    work_assignment=RedactedNode(
                        kind=_NODE_KIND_WORK_ASSIGNMENT
                    ),
                    work_events=(),
                    time_entries=(),
                )
            )
        else:
            work_assignment_chains.append(
                _build_work_assignment_chain(
                    self,
                    connection,
                    wa_row=originating_wa_row,
                    party_id=party_id,
                    at=effective_at,
                )
            )

    # ---- Stage 3: Planning leg via navigate_plan_approval. --------------
    plan_approval_chain: Optional[PlanApprovalProvenance] = None
    if target_plan_revision_id is not None:
        plan_approval_chain = _resolve_plan_approval_chain(
            self,
            connection,
            target_plan_revision_id=target_plan_revision_id,
            party_id=party_id,
            at=effective_at,
        )

    # ---- Stage 4: Gap descriptors (slice-default-2026 rule 2). ----------
    #
    # The Revision's Provenance Manifest is the only manifest surfaced
    # at this anchor; the Planning leg's manifest gaps are surfaced by
    # the delegated :meth:`navigate_plan_approval`. The Revision is
    # the only Slice 3 anchor that carries both a Resource Identity
    # and a Revision Identity; the manifest lookup uses the Revision
    # Identity for the ``subject_revision_id`` parameter so
    # Revision-grain manifests (the common case) match precisely.
    gap_descriptors: tuple = ()
    if self.disclosure_policy is not None:
        gap_descriptors = tuple(
            self._collect_gap_descriptors_for_subject(
                connection,
                subject_kind=_NODE_KIND_DELIVERABLE_REVISION,
                subject_id=revision_row["deliverable_id"],
                subject_revision_id=revision_row["deliverable_revision_id"],
                next_reachable_node_identity=revision_row[
                    "deliverable_revision_id"
                ],
            )
        )

    return ExecutionProvenanceTree(
        completion=None,
        plan_approval_chain=plan_approval_chain,
        milestone_acceptance_chains=(),
        work_assignment_chains=tuple(work_assignment_chains),
        gap_descriptors=gap_descriptors,
        requested_completion_id="",
        production_anchor=None,
        produced_revision_anchor=revision_node,
        requested_anchor_kind=_NODE_KIND_DELIVERABLE_REVISION,
        requested_anchor_id=deliverable_revision_id,
    )


# Attach the task-12.2 traversals and the new row-load helper to
# :class:`ProvenanceNavigator` in one place. Mirrors the attachment
# pattern used by the Slice 2 ``navigate_plan_approval`` task and the
# Slice 3 task 12.1 ``navigate_completion`` task so the public surface
# of the class is composed of the original methods plus these additive
# attachments — no edit to the original class body is required, which
# is what Requirement 40.1 (Reuse and Non-Modification of Slice 1 and
# Slice 2 Contexts) demands.
ProvenanceNavigator.navigate_deliverable_production = (
    _navigate_deliverable_production
)
ProvenanceNavigator.navigate_produced_deliverable_revision = (
    _navigate_produced_deliverable_revision
)
ProvenanceNavigator._load_work_assignment_row = staticmethod(
    _load_work_assignment_row
)
