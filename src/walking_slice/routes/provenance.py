"""HTTP routes for the Provenance_Navigator (task 12.3 / 12.5).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Provenance_Navigator" HTTP surface. This module is introduced by
task 12.3 to expose the Region Occurrence text resolution endpoint
required by Requirement 3.4 (resolve a Content Region reference to
the exact Document Revision, Region Identity, Region Occurrence, and
byte-equivalent bounded text span) and Requirement 11.2 (Region
Occurrence nodes in a provenance chain include the start anchor, end
anchor, and bounded text span byte-equivalent to the recorded text
and digest-matching against the recorded content digest).

Endpoints currently mounted (task 12.3):

| Method | Path                                                              | Purpose                                                                          |
|--------|-------------------------------------------------------------------|----------------------------------------------------------------------------------|
| `GET`  | `/api/v1/regions/{region_id}/occurrences/{revision_id}/text`      | Bounded text span + digest comparison for a Region Occurrence (Req. 3.4 / 11.2). |

The remaining five endpoints in design §"Provenance_Navigator" HTTP
surface (``/backlinks/{node_kind}/{node_id}``,
``/decisions/{id}/provenance``, ``/findings/{id}/provenance``,
``/recommendations/{id}/provenance``,
``/trails/{id}/revisions/{revision_id}/provenance``) land here as part
of task 12.5; the router is created in this module so that task only
needs to add more handlers to the existing :data:`router`.

Responsibilities:

- Expose a :class:`fastapi.APIRouter` that delegates to
  :class:`walking_slice.provenance.ProvenanceNavigator` for every
  resolution, wrapping each call in ``engine.begin()`` so the two
  evaluation ``Audit_Records`` rows appended by
  :class:`AuthorizationService` (Requirement 12.5) commit even though
  the read itself is non-consequential per design
  §"Provenance_Navigator".
- Map navigator exceptions to HTTP status codes:
  :class:`~walking_slice.provenance.RegionOccurrenceUnresolvableError`
  → 404 with the offending identifiers (Requirement 3.6 +
  design §"Error Categories");
  :class:`~walking_slice.provenance.RegionTextAuthorizationError`
  → 403 with the AD-WS-9 indistinguishable denial response shape
  carrying **only** ``generic_denial_indicator``, ``reason_code``,
  and ``correlation_id`` (Requirement 7.4).

**Binary text encoding.** The ``bounded_text`` field on the success
response is a base64-encoded JSON string so the wire format is
uniformly JSON for every endpoint and matches the symmetric encoding
used by the ``GET /api/v1/documents/{rid}/revisions/{rev}`` endpoint in
:mod:`walking_slice.routes.evidence`. Callers decode it with
``base64.b64decode`` to recover the raw bytes; the per-byte equivalence
required by Requirement 11.2 holds across the base64 round-trip
because base64 is a lossless byte→ASCII encoding.

**Authentication.** This module accepts the requesting Party Identity
from the temporary ``X-Actor-Party-Id`` header so the requesting party
can be threaded into the per-stage authorization checks. Task 15.1
will replace this placeholder with the bearer-token authenticated
``RequestContext`` described in design §"Application-Level Composition".

**Dependency injection.** The engine and the
:class:`ProvenanceNavigator` are reached through
:func:`fastapi.Depends` factories so the
:data:`fastapi.FastAPI.dependency_overrides` pattern used by the
other route modules works here unchanged.

Requirements satisfied (per task 12.3):
    3.4 — Resolving a Content Region reference returns the exact
          Document Revision Identity, Region Identity, Region
          Occurrence, and a bounded text span byte-equivalent to the
          span originally recorded for the Region Occurrence.
    11.2 — The bounded text in a provenance response is
          byte-equivalent to the recorded text and digest-matches
          against the recorded content digest. The
          ``span_content_digest_sha256`` from the persisted row,
          the SHA-256 computed at resolution time, and a boolean
          ``digest_matches`` flag are all surfaced on the response.
"""

from __future__ import annotations

import asyncio
import base64
from typing import Annotated, Final, Literal, Optional, Union

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from walking_slice.provenance import (
    BACKLINK_PAGE_LIMIT,
    BacklinkPage,
    DecisionProvenanceChain,
    DecisionUnresolvableError,
    DisclosureAppliedChain,
    FindingProvenanceChain,
    FindingUnresolvableError,
    ProvenanceNavigator,
    RecommendationProvenanceChain,
    RecommendationUnresolvableError,
    RedactedNode,
    RegionOccurrenceUnresolvableError,
    RegionTextAuthorizationError,
    TrailProvenanceChain,
    TrailRevisionUnresolvableError,
    decode_backlink_cursor,
    encode_backlink_cursor,
)


__all__ = [
    "BacklinkEntryBody",
    "BacklinkPageResponseBody",
    "DecisionProvenanceResponseBody",
    "DenialResponseBody",
    "DocumentRevisionNodeBody",
    "ErrorBody",
    "FindingProvenanceResponseBody",
    "FindingRevisionNodeBody",
    "GapDescriptorBody",
    "RecommendationProvenanceResponseBody",
    "RecommendationRevisionNodeBody",
    "RedactedNodeBody",
    "RegionOccurrenceNodeBody",
    "RegionTextResponseBody",
    "TrailProvenanceResponseBody",
    "TrailRevisionNodeBody",
    "TrailStepNodeBody",
    "get_engine",
    "get_provenance_navigator",
    "router",
]


# ---------------------------------------------------------------------------
# Router.
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1", tags=["provenance"])


# ---------------------------------------------------------------------------
# Dependency-injection placeholders.
#
# These factories are deliberately stubs; task 15.2 wires concrete
# implementations through ``walking_slice.app.create_app``. Tests override
# them on the per-test :class:`fastapi.FastAPI` instance via
# ``app.dependency_overrides[get_engine] = lambda: engine`` etc.
# ---------------------------------------------------------------------------


def get_engine() -> Engine:
    """Provide the SQLAlchemy engine bound to the slice's SQLite store.

    Overridden in tests and in the application composition layer
    (task 15.2). Never called unwrapped from a route handler.
    """
    raise NotImplementedError(
        "walking_slice.routes.provenance.get_engine must be overridden by "
        "app composition (task 15.2) or test fixtures."
    )


def get_provenance_navigator() -> ProvenanceNavigator:
    """Provide the slice's :class:`ProvenanceNavigator` singleton.

    The navigator is connection-scoped at call time — every method
    accepts the caller's SQLAlchemy connection — so a single instance
    serves all requests safely.
    """
    raise NotImplementedError(
        "walking_slice.routes.provenance.get_provenance_navigator must "
        "be overridden by app composition (task 15.2) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Pydantic v2 response models.
# ---------------------------------------------------------------------------


class RegionTextResponseBody(BaseModel):
    """200 response body for ``GET /.../regions/{rid}/occurrences/{rev}/text``.

    Mirrors :class:`walking_slice.provenance.RegionTextResolution` over
    the wire. ``bounded_text`` is base64-encoded so the JSON body
    remains valid for arbitrary byte content; the symmetric encoding
    matches the ``GET /api/v1/documents/{rid}/revisions/{rev}``
    endpoint's response shape.

    Attributes:
        region_id: Identity of the Content Region.
        revision_id: Identity of the Document Revision anchoring the
            Region Occurrence. Named ``revision_id`` (not
            ``document_revision_id``) so the wire shape matches the
            existing ``GET /api/v1/regions/{rid}/occurrences/{rev}``
            response from :mod:`walking_slice.routes.evidence`.
        start_offset_bytes: Persisted start anchor.
        end_offset_bytes: Persisted end anchor.
        span_byte_length: Persisted span length.
        span_content_digest_sha256: Hex-encoded SHA-256 of the span,
            taken from the ``Region_Occurrences`` row at write time
            (Requirement 3.2).
        computed_digest_sha256: Hex-encoded SHA-256 computed at
            resolution time over the byte-equivalent span. Per
            construction-time invariant this must equal
            ``span_content_digest_sha256`` for an unmutated row pair
            (Requirement 11.2 digest-matching).
        digest_matches: Equality flag for the two digests above.
        bounded_text: Base64-encoded byte-equivalent span. Decode
            with ``base64.b64decode`` to recover the raw bytes
            (Requirement 11.2 byte-equivalence).
        recorded_at: ISO-8601 UTC millisecond-precision timestamp
            from the ``Region_Occurrences`` row.
    """

    model_config = ConfigDict(extra="forbid")

    region_id: str
    revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    computed_digest_sha256: str
    digest_matches: bool
    bounded_text: str = Field(
        description=(
            "Base64-encoded byte-equivalent span "
            "Document_Revisions.content_bytes[start:end]. Decode with "
            "``base64.b64decode`` to recover the raw bytes."
        ),
    )
    recorded_at: str


class DenialResponseBody(BaseModel):
    """403 response body for a denied region text resolution (AD-WS-9).

    Requirement 7.4 forbids leaking authorized Party identities, target
    contents, role assignment details, or target existence beyond the
    requesting Party's view authority through the denial response.
    AD-WS-9 fixes the indistinguishable response shape to *exactly*
    three fields:

    - ``generic_denial_indicator`` — the constant string ``"denied"``.
    - ``reason_code`` — one of
      ``{not-yet-effective, expired, revoked, out-of-scope,
      no-role-assignment}`` per Requirement 7.2 / 12.2.
    - ``correlation_id`` — the operation correlation identifier
      shared with the evaluation audit row appended by the
      :class:`AuthorizationService`.

    ``extra="forbid"`` ensures no additional field can be added by
    accident; a model-validation failure here would surface as a 500
    rather than silently leaking information.
    """

    model_config = ConfigDict(extra="forbid")

    generic_denial_indicator: Literal["denied"] = "denied"
    reason_code: str
    correlation_id: str


class ErrorBody(BaseModel):
    """Structured error body for 400 / 404 / 503 responses.

    The shape mirrors :class:`walking_slice.routes.evidence.ErrorBody`
    so a single client-side error handler works across every route
    module. Fields are deliberately optional; only the ones relevant
    to the failure are populated.
    """

    model_config = ConfigDict(extra="forbid")

    error: str
    message: Optional[str] = None
    region_id: Optional[str] = None
    revision_id: Optional[str] = None
    missing: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


_HEADER_ACTOR: Final[str] = "X-Actor-Party-Id"


def _require_party_id(header_value: Optional[str]) -> str:
    """Extract the requesting Party Identity from the ``X-Actor-Party-Id`` header.

    The header is required for every endpoint in this module because
    every navigator call evaluates view authority per-Party. Missing
    or empty values are rejected with a 400 — the request shape is
    invalid before the navigator is reached, so a structured 400 is
    the right response (Requirement 7.4 only normalizes
    authorization-denial responses; a missing actor is a request-
    shape failure, not a denial).
    """
    actor = (header_value or "").strip()
    if not actor:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="actor_party_id_required",
                message=(
                    "X-Actor-Party-Id header is required to evaluate "
                    "view authority for this endpoint (placeholder until "
                    "task 15.1)."
                ),
                missing=["X-Actor-Party-Id"],
            ).model_dump(),
        )
    return actor


def _region_unresolvable_to_http(
    exc: RegionOccurrenceUnresolvableError,
) -> HTTPException:
    """Map a :class:`RegionOccurrenceUnresolvableError` to a 404.

    The response body surfaces the unresolved identifiers so callers
    can correct their request. The shape matches the
    ``region_occurrence_not_found`` 404 emitted by
    :mod:`walking_slice.routes.evidence` for the read-region endpoint
    so a single client error handler covers both endpoints.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="region_occurrence_not_found",
            message=str(exc),
            region_id=exc.region_id,
            revision_id=exc.document_revision_id,
        ).model_dump(),
    )


def _region_authorization_denial_to_http(
    exc: RegionTextAuthorizationError,
) -> HTTPException:
    """Map a :class:`RegionTextAuthorizationError` to a 403 (AD-WS-9).

    Per AD-WS-9 the denial response carries **only** the
    ``generic_denial_indicator``, ``reason_code``, and
    ``correlation_id`` fields — no information about *what* was being
    read, *which* role assignment was missing, or *whether* the
    target exists is leaked. Requirement 7.4 also calls this out for
    the read side.
    """
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=DenialResponseBody(
            reason_code=exc.reason_code,
            correlation_id=exc.correlation_id,
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get(
    "/regions/{region_id}/occurrences/{revision_id}/text",
    response_model=RegionTextResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_403_FORBIDDEN: {"model": DenialResponseBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary="Resolve the byte-equivalent text of a Region Occurrence.",
)
async def resolve_region_occurrence_text(
    region_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> RegionTextResponseBody:
    """Return the byte-equivalent text span for a Region Occurrence.

    The endpoint:

    1. Extracts the requesting Party Identity from the
       ``X-Actor-Party-Id`` header (400 on missing/empty).
    2. Opens a transaction (``engine.begin()``) so the two
       :meth:`AuthorizationService.evaluate` audit rows appended by
       the navigator (Requirement 12.5) commit alongside the read.
    3. Calls
       :meth:`ProvenanceNavigator.resolve_region_text`, which:
         - Loads the ``Region_Occurrences`` and
           ``Document_Revisions`` rows by composite key.
         - Evaluates ``view.region_occurrence`` and
           ``view.document_revision`` for the requesting Party.
         - Computes the byte-equivalent ``bounded_text =
           content_bytes[start:end]``.
         - Compares the SHA-256 of those bytes against the persisted
           ``span_content_digest_sha256``.
    4. Maps navigator exceptions to HTTP responses:
         - :class:`RegionOccurrenceUnresolvableError` → 404 with the
           offending identifiers (Requirement 3.6).
         - :class:`RegionTextAuthorizationError` → 403 with the
           AD-WS-9 indistinguishable denial shape (Requirement 7.4).
    5. Serializes the :class:`RegionTextResolution` to
       :class:`RegionTextResponseBody`, base64-encoding the bounded
       text for JSON-safe wire transport.
    """
    party_id = _require_party_id(x_actor_party_id)

    try:
        with engine.begin() as connection:
            resolution = navigator.resolve_region_text(
                connection,
                region_id=region_id,
                document_revision_id=revision_id,
                party_id=party_id,
            )
    except RegionOccurrenceUnresolvableError as exc:
        raise _region_unresolvable_to_http(exc) from exc
    except RegionTextAuthorizationError as exc:
        raise _region_authorization_denial_to_http(exc) from exc

    return RegionTextResponseBody(
        region_id=resolution.region_id,
        revision_id=resolution.document_revision_id,
        start_offset_bytes=resolution.start_offset_bytes,
        end_offset_bytes=resolution.end_offset_bytes,
        span_byte_length=resolution.span_byte_length,
        span_content_digest_sha256=resolution.span_content_digest_sha256,
        computed_digest_sha256=resolution.computed_digest_sha256,
        digest_matches=resolution.digest_matches,
        bounded_text=base64.b64encode(resolution.bounded_text).decode("ascii"),
        recorded_at=resolution.recorded_at,
    )


# ---------------------------------------------------------------------------
# Provenance traversal response models (task 12.5).
# ---------------------------------------------------------------------------


class BacklinkEntryBody(BaseModel):
    """One inbound Relationship in a backlinks page (Requirement 8.2).

    Mirrors :class:`walking_slice.provenance.BacklinkEntry`.
    """

    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    relationship_type: str
    source_id: str
    source_kind: str
    source_revision_id: Optional[str] = None
    authoring_party_id: str
    recorded_at: str


class BacklinkPageResponseBody(BaseModel):
    """200 response from ``GET /api/v1/backlinks``.

    Carries the authorized projection, the next-page cursor (or
    ``None`` when no more pages), the response size, and the
    deterministic latency baseline the navigator already shaped from
    the authorized projection alone (Requirements 8.1, 8.3, 8.6).
    """

    model_config = ConfigDict(extra="forbid")

    target_id: str
    target_revision_id: Optional[str] = None
    entries: list[BacklinkEntryBody]
    next_cursor: Optional[str] = None
    response_size: int = Field(
        ge=0,
        le=BACKLINK_PAGE_LIMIT,
        description=(
            "Authorized page size; depends only on the authorized "
            "projection (Requirement 8.3 / Property 4)."
        ),
    )


class RedactedNodeBody(BaseModel):
    """Generic AD-WS-9 redaction marker for a restricted node.

    Carries only ``kind`` and ``redacted=True`` — no identifiers,
    counts, or attribute values of the redacted node (Requirement
    11.3).
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    redacted: Literal[True] = True


class GapDescriptorBody(BaseModel):
    """One gap descriptor surfaced from a Provenance Manifest.

    Per AD-WS-9 rule 2 (Requirement 11.4) the body carries only
    ``stage``, ``category``, and (when visible) the next reachable
    node's identity. The ``restricted`` and ``intentional`` categories
    are intentionally excluded from this surface.
    """

    model_config = ConfigDict(extra="forbid")

    stage: str
    category: Literal["unavailable", "stale", "unresolved"]
    next_reachable_node_identity: Optional[str] = None


class FindingRevisionNodeBody(BaseModel):
    """Serialized :class:`FindingRevisionNode`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["finding_revision"] = "finding_revision"
    finding_id: str
    finding_revision_id: str
    parent_revision_id: Optional[str] = None
    statement: str
    is_hypothesis: bool
    authoring_party_id: str
    assumptions_json: str
    confidence_note: Optional[str] = None
    recorded_at: str


class RecommendationRevisionNodeBody(BaseModel):
    """Serialized :class:`RecommendationRevisionNode`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["recommendation_revision"] = "recommendation_revision"
    recommendation_id: str
    recommendation_revision_id: str
    parent_revision_id: Optional[str] = None
    rationale: Optional[str] = None
    assumptions_json: str
    confidence: Optional[str] = None
    authoring_party_id: str
    recorded_at: str


class DecisionNodeBody(BaseModel):
    """Serialized :class:`DecisionNode`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["decision"] = "decision"
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


class RegionOccurrenceNodeBody(BaseModel):
    """Serialized :class:`RegionOccurrenceNode`.

    ``bounded_text`` is base64-encoded so the JSON body remains valid
    for arbitrary byte content (Requirement 11.2 byte-equivalence;
    decode with ``base64.b64decode`` to recover the bytes).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["region_occurrence"] = "region_occurrence"
    region_id: str
    document_revision_id: str
    start_offset_bytes: int
    end_offset_bytes: int
    span_byte_length: int
    span_content_digest_sha256: str
    bounded_text: str = Field(
        description=(
            "Base64-encoded byte-equivalent span "
            "Document_Revisions.content_bytes[start:end]."
        ),
    )
    recorded_at: str


class DocumentRevisionNodeBody(BaseModel):
    """Serialized :class:`DocumentRevisionNode` (without ``content_bytes``)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["document_revision"] = "document_revision"
    resource_id: str
    revision_id: str
    parent_revision_id: Optional[str] = None
    content_digest_sha256: str
    contributing_party_id: str
    recorded_at: str
    change_description: Optional[str] = None


class TrailStepNodeBody(BaseModel):
    """Serialized :class:`TrailStepNode`."""

    model_config = ConfigDict(extra="forbid")

    trail_step_id: str
    ordinal: int
    target_kind: str
    target_id: str
    target_revision_id: Optional[str] = None
    region_id: Optional[str] = None
    selection_mode: str
    annotation: Optional[str] = None


class TrailRevisionNodeBody(BaseModel):
    """Serialized :class:`TrailRevisionNode`."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["trail_revision"] = "trail_revision"
    trail_id: str
    trail_revision_id: str
    predecessor_revision_id: Optional[str] = None
    purpose: str
    audience_id: str
    ordering_rationale: Optional[str] = None
    authoring_party_id: str
    recorded_at: str


# A node entry on the wire is either the visible node body or the
# generic redaction marker. Using ``Union`` here is enough for
# pydantic-v2 to discriminate on the ``kind`` Literal of each branch.
RecommendationOrRedacted = Union[RecommendationRevisionNodeBody, RedactedNodeBody]
FindingOrRedacted = Union[FindingRevisionNodeBody, RedactedNodeBody]
RegionOrRedacted = Union[RegionOccurrenceNodeBody, RedactedNodeBody]
DocumentOrRedacted = Union[DocumentRevisionNodeBody, RedactedNodeBody]


class DecisionProvenanceResponseBody(BaseModel):
    """200 response from ``GET /api/v1/decisions/{decision_id}/provenance``."""

    model_config = ConfigDict(extra="forbid")

    decision: DecisionNodeBody
    recommendation_revision: RecommendationOrRedacted
    findings: list[FindingOrRedacted]
    region_occurrences: list[RegionOrRedacted]
    document_revisions: list[DocumentOrRedacted]
    gap_descriptors: list[GapDescriptorBody]
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    requested_decision_id: str


class FindingProvenanceResponseBody(BaseModel):
    """200 response from ``GET /api/v1/findings/{finding_id}/provenance``."""

    model_config = ConfigDict(extra="forbid")

    finding_revision: FindingRevisionNodeBody
    region_occurrences: list[RegionOrRedacted]
    document_revisions: list[DocumentOrRedacted]
    gap_descriptors: list[GapDescriptorBody]
    requested_finding_id: str


class RecommendationProvenanceResponseBody(BaseModel):
    """200 response from ``GET /api/v1/recommendations/{rec_id}/provenance``."""

    model_config = ConfigDict(extra="forbid")

    recommendation_revision: RecommendationRevisionNodeBody
    findings: list[FindingOrRedacted]
    region_occurrences: list[RegionOrRedacted]
    document_revisions: list[DocumentOrRedacted]
    gap_descriptors: list[GapDescriptorBody]
    requested_recommendation_id: str


class TrailProvenanceResponseBody(BaseModel):
    """200 response from
    ``GET /api/v1/trails/{trail_id}/revisions/{revision_id}/provenance``."""

    model_config = ConfigDict(extra="forbid")

    trail_revision: TrailRevisionNodeBody
    steps: list[TrailStepNodeBody]
    decision_chain: Optional[DecisionProvenanceResponseBody] = None
    gap_descriptors: list[GapDescriptorBody]
    requested_trail_id: str
    requested_trail_revision_id: str


# ---------------------------------------------------------------------------
# Node → response body mappers.
# ---------------------------------------------------------------------------


def _redacted_to_body(node: RedactedNode) -> RedactedNodeBody:
    return RedactedNodeBody(kind=node.kind)


def _region_node_to_body(
    node,
) -> RegionOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return RegionOccurrenceNodeBody(
        region_id=node.region_id,
        document_revision_id=node.document_revision_id,
        start_offset_bytes=node.start_offset_bytes,
        end_offset_bytes=node.end_offset_bytes,
        span_byte_length=node.span_byte_length,
        span_content_digest_sha256=node.span_content_digest_sha256,
        bounded_text=base64.b64encode(node.bounded_text).decode("ascii"),
        recorded_at=node.recorded_at,
    )


def _document_node_to_body(node) -> DocumentOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return DocumentRevisionNodeBody(
        resource_id=node.resource_id,
        revision_id=node.revision_id,
        parent_revision_id=node.parent_revision_id,
        content_digest_sha256=node.content_digest_sha256,
        contributing_party_id=node.contributing_party_id,
        recorded_at=node.recorded_at,
        change_description=node.change_description,
    )


def _finding_node_to_body(node) -> FindingOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return FindingRevisionNodeBody(
        finding_id=node.finding_id,
        finding_revision_id=node.finding_revision_id,
        parent_revision_id=node.parent_revision_id,
        statement=node.statement,
        is_hypothesis=node.is_hypothesis,
        authoring_party_id=node.authoring_party_id,
        assumptions_json=node.assumptions_json,
        confidence_note=node.confidence_note,
        recorded_at=node.recorded_at,
    )


def _recommendation_node_to_body(node) -> RecommendationOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return RecommendationRevisionNodeBody(
        recommendation_id=node.recommendation_id,
        recommendation_revision_id=node.recommendation_revision_id,
        parent_revision_id=node.parent_revision_id,
        rationale=node.rationale,
        assumptions_json=node.assumptions_json,
        confidence=node.confidence,
        authoring_party_id=node.authoring_party_id,
        recorded_at=node.recorded_at,
    )


def _gap_descriptors_to_body(descriptors) -> list[GapDescriptorBody]:
    return [
        GapDescriptorBody(
            stage=d.stage,
            category=d.category,
            next_reachable_node_identity=d.next_reachable_node_identity,
        )
        for d in descriptors
    ]


def _decision_chain_to_body(
    chain: DecisionProvenanceChain,
    *,
    gap_descriptors=(),
    policy_id: Optional[str] = None,
    policy_name: Optional[str] = None,
) -> DecisionProvenanceResponseBody:
    return DecisionProvenanceResponseBody(
        decision=DecisionNodeBody(
            decision_id=chain.decision.decision_id,
            target_recommendation_id=chain.decision.target_recommendation_id,
            target_recommendation_revision_id=(
                chain.decision.target_recommendation_revision_id
            ),
            outcome=chain.decision.outcome,
            rationale=chain.decision.rationale,
            deciding_party_id=chain.decision.deciding_party_id,
            authority_basis_type=chain.decision.authority_basis_type,
            authority_basis_id=chain.decision.authority_basis_id,
            applicable_scope=chain.decision.applicable_scope,
            recorded_at=chain.decision.recorded_at,
        ),
        recommendation_revision=_recommendation_node_to_body(
            chain.recommendation_revision
        ),
        findings=[_finding_node_to_body(f) for f in chain.findings],
        region_occurrences=[
            _region_node_to_body(r) for r in chain.region_occurrences
        ],
        document_revisions=[
            _document_node_to_body(d) for d in chain.document_revisions
        ],
        gap_descriptors=_gap_descriptors_to_body(gap_descriptors),
        policy_id=policy_id,
        policy_name=policy_name,
        requested_decision_id=chain.requested_decision_id,
    )


# ---------------------------------------------------------------------------
# Error mappers for the new traversal endpoints.
# ---------------------------------------------------------------------------


def _decision_unresolvable_to_http(
    exc: DecisionUnresolvableError,
) -> HTTPException:
    """Map a :class:`DecisionUnresolvableError` to a 404.

    Per Requirement 11.6 the body identifies the unresolvable
    Decision reference and discloses nothing about related Resources.
    The same exception is raised when the requesting Party lacks
    view authority on an existing Decision so the response is
    indistinguishable from the unresolvable case (Requirement 11.7).
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="decision_not_found",
            message=str(exc),
            revision_id=None,
            region_id=exc.decision_id,
        ).model_dump(),
    )


def _finding_unresolvable_to_http(
    exc: FindingUnresolvableError,
) -> HTTPException:
    """Map a :class:`FindingUnresolvableError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="finding_not_found",
            message=str(exc),
            region_id=exc.finding_id,
        ).model_dump(),
    )


def _recommendation_unresolvable_to_http(
    exc: RecommendationUnresolvableError,
) -> HTTPException:
    """Map a :class:`RecommendationUnresolvableError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="recommendation_not_found",
            message=str(exc),
            region_id=exc.recommendation_id,
        ).model_dump(),
    )


def _trail_revision_unresolvable_to_http(
    exc: TrailRevisionUnresolvableError,
) -> HTTPException:
    """Map a :class:`TrailRevisionUnresolvableError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="trail_revision_not_found",
            message=str(exc),
            region_id=exc.trail_id,
            revision_id=exc.trail_revision_id,
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Endpoint — GET /backlinks  (Requirement 8.1).
# ---------------------------------------------------------------------------


@router.get(
    "/backlinks",
    response_model=BacklinkPageResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
    },
    summary=(
        "List inbound Relationships for a target node, filtered by "
        "the requesting Party's view authority."
    ),
)
async def list_backlinks(
    target_id: Annotated[str, Query(min_length=1)],
    target_revision_id: Annotated[Optional[str], Query()] = None,
    after_cursor: Annotated[Optional[str], Query()] = None,
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> BacklinkPageResponseBody:
    """Return one authorized page of inbound Relationships.

    The endpoint:

    1. Reads the requesting Party Identity from the
       ``X-Actor-Party-Id`` header (400 on missing/empty).
    2. Decodes the optional pagination cursor (400 on malformed).
    3. Opens a transaction (``engine.begin()``) so the per-candidate
       ``view.relationship`` / ``view.<source_kind>`` audit appends
       commit alongside the read.
    4. Calls :meth:`ProvenanceNavigator.list_backlinks` which builds
       the authorized projection, computes the cursor and response
       size from that projection alone, and returns a deterministic
       latency baseline (Requirement 8.3 / Property 4).
    5. ``await``s ``asyncio.sleep`` shaped from the latency baseline
       so the latency observable to the caller is a function of the
       *authorized* size only (Requirement 8.3 indistinguishability
       within a 100 ms tolerance — Property 4 enforces the
       cross-Party comparison).
    6. Serializes the :class:`BacklinkPage` to the response body.
    """
    party_id = _require_party_id(x_actor_party_id)

    try:
        cursor = decode_backlink_cursor(after_cursor)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorBody(
                error="invalid_after_cursor",
                message=str(exc),
            ).model_dump(),
        ) from exc

    with engine.begin() as connection:
        page: BacklinkPage = navigator.list_backlinks(
            connection,
            target_id=target_id,
            target_revision_id=target_revision_id,
            party_id=party_id,
            after_cursor=cursor,
        )

    # Constant-time observability: wait out the latency baseline
    # shaped by :func:`compute_latency_baseline_seconds` from the
    # *authorized* response size only. Property 4 (task 12.7) compares
    # this latency across Party pairs differing only in view
    # authority and expects equality within 100 ms.
    await asyncio.sleep(page.latency_baseline_seconds)

    return BacklinkPageResponseBody(
        target_id=target_id,
        target_revision_id=target_revision_id,
        entries=[
            BacklinkEntryBody(
                relationship_id=entry.relationship_id,
                relationship_type=entry.relationship_type,
                source_id=entry.source_id,
                source_kind=entry.source_kind,
                source_revision_id=entry.source_revision_id,
                authoring_party_id=entry.authoring_party_id,
                recorded_at=entry.recorded_at,
            )
            for entry in page.entries
        ],
        next_cursor=encode_backlink_cursor(page.cursor),
        response_size=page.response_size,
    )


# ---------------------------------------------------------------------------
# Endpoint — GET /decisions/{decision_id}/provenance (Requirement 11.1).
# ---------------------------------------------------------------------------


@router.get(
    "/decisions/{decision_id}/provenance",
    response_model=DecisionProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Return the Decision → Recommendation → Finding(s) → Region "
        "Occurrence(s) → Document Revision provenance chain."
    ),
)
async def get_decision_provenance(
    decision_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> DecisionProvenanceResponseBody:
    """Return the full Decision-to-Evidence provenance chain.

    Uses :meth:`ProvenanceNavigator.navigate_decision_with_disclosure`
    when a disclosure policy is wired so the response carries the
    AD-WS-9 policy identifier and any gap descriptors loaded from the
    Provenance Manifests; falls back to the raw
    :meth:`navigate_decision` chain when no policy is configured
    (the response then carries an empty ``gap_descriptors`` list and
    ``policy_id == policy_name == None``).
    """
    party_id = _require_party_id(x_actor_party_id)

    try:
        with engine.begin() as connection:
            if navigator.disclosure_policy is not None:
                applied: DisclosureAppliedChain = (
                    navigator.navigate_decision_with_disclosure(
                        connection,
                        decision_id=decision_id,
                        party_id=party_id,
                    )
                )
                return _decision_chain_to_body(
                    applied.chain,
                    gap_descriptors=applied.gap_descriptors,
                    policy_id=applied.policy_id,
                    policy_name=applied.policy_name,
                )
            chain = navigator.navigate_decision(
                connection,
                decision_id=decision_id,
                party_id=party_id,
            )
    except DecisionUnresolvableError as exc:
        raise _decision_unresolvable_to_http(exc) from exc

    return _decision_chain_to_body(chain)


# ---------------------------------------------------------------------------
# Endpoint — GET /findings/{finding_id}/provenance (Requirement 10.4).
# ---------------------------------------------------------------------------


@router.get(
    "/findings/{finding_id}/provenance",
    response_model=FindingProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Return the Finding Revision → Region Occurrence(s) → Document "
        "Revision provenance chain."
    ),
)
async def get_finding_provenance(
    finding_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> FindingProvenanceResponseBody:
    """Return the provenance chain rooted at one Finding Revision.

    Loads the latest Finding Revision at-or-before the navigator's
    effective time, evaluates view authority, then walks every
    Supports Relationship to surface the cited Region Occurrence(s)
    and owning Document Revision(s). Unresolvable Finding identifiers
    and denied view authority both yield a 404 per the
    indistinguishable-not-found contract (Requirement 11.7).
    """
    party_id = _require_party_id(x_actor_party_id)

    try:
        with engine.begin() as connection:
            chain: FindingProvenanceChain = navigator.navigate_finding(
                connection,
                finding_id=finding_id,
                party_id=party_id,
            )
    except FindingUnresolvableError as exc:
        raise _finding_unresolvable_to_http(exc) from exc

    return FindingProvenanceResponseBody(
        finding_revision=FindingRevisionNodeBody(
            finding_id=chain.finding_revision.finding_id,
            finding_revision_id=chain.finding_revision.finding_revision_id,
            parent_revision_id=chain.finding_revision.parent_revision_id,
            statement=chain.finding_revision.statement,
            is_hypothesis=chain.finding_revision.is_hypothesis,
            authoring_party_id=chain.finding_revision.authoring_party_id,
            assumptions_json=chain.finding_revision.assumptions_json,
            confidence_note=chain.finding_revision.confidence_note,
            recorded_at=chain.finding_revision.recorded_at,
        ),
        region_occurrences=[
            _region_node_to_body(r) for r in chain.region_occurrences
        ],
        document_revisions=[
            _document_node_to_body(d) for d in chain.document_revisions
        ],
        gap_descriptors=_gap_descriptors_to_body(chain.gap_descriptors),
        requested_finding_id=chain.requested_finding_id,
    )


# ---------------------------------------------------------------------------
# Endpoint — GET /recommendations/{rec_id}/provenance (Requirement 10.4).
# ---------------------------------------------------------------------------


@router.get(
    "/recommendations/{recommendation_id}/provenance",
    response_model=RecommendationProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Return the Recommendation Revision → Finding(s) → Region "
        "Occurrence(s) → Document Revision provenance chain."
    ),
)
async def get_recommendation_provenance(
    recommendation_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> RecommendationProvenanceResponseBody:
    """Return the provenance chain rooted at one Recommendation Revision."""
    party_id = _require_party_id(x_actor_party_id)

    try:
        with engine.begin() as connection:
            chain: RecommendationProvenanceChain = (
                navigator.navigate_recommendation(
                    connection,
                    recommendation_id=recommendation_id,
                    party_id=party_id,
                )
            )
    except RecommendationUnresolvableError as exc:
        raise _recommendation_unresolvable_to_http(exc) from exc

    return RecommendationProvenanceResponseBody(
        recommendation_revision=RecommendationRevisionNodeBody(
            recommendation_id=chain.recommendation_revision.recommendation_id,
            recommendation_revision_id=(
                chain.recommendation_revision.recommendation_revision_id
            ),
            parent_revision_id=chain.recommendation_revision.parent_revision_id,
            rationale=chain.recommendation_revision.rationale,
            assumptions_json=chain.recommendation_revision.assumptions_json,
            confidence=chain.recommendation_revision.confidence,
            authoring_party_id=chain.recommendation_revision.authoring_party_id,
            recorded_at=chain.recommendation_revision.recorded_at,
        ),
        findings=[_finding_node_to_body(f) for f in chain.findings],
        region_occurrences=[
            _region_node_to_body(r) for r in chain.region_occurrences
        ],
        document_revisions=[
            _document_node_to_body(d) for d in chain.document_revisions
        ],
        gap_descriptors=_gap_descriptors_to_body(chain.gap_descriptors),
        requested_recommendation_id=chain.requested_recommendation_id,
    )


# ---------------------------------------------------------------------------
# Endpoint — GET /trails/{trail_id}/revisions/{revision_id}/provenance
# (Requirement 10.4).
# ---------------------------------------------------------------------------


@router.get(
    "/trails/{trail_id}/revisions/{revision_id}/provenance",
    response_model=TrailProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Return the Trail Revision, its five Trail Steps, and (when "
        "visible) the inline Decision provenance chain."
    ),
)
async def get_trail_revision_provenance(
    trail_id: Annotated[str, Path(min_length=1)],
    revision_id: Annotated[str, Path(min_length=1)],
    x_actor_party_id: Annotated[
        Optional[str], Header(alias=_HEADER_ACTOR, convert_underscores=False)
    ] = None,
    engine: Engine = Depends(get_engine),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> TrailProvenanceResponseBody:
    """Return the provenance for one Trail Revision.

    The response carries the Trail Revision, its five Trail Steps in
    ordinal order, and — when ordinal 5's Decision target resolves
    and the requesting Party holds ``view.decision`` authority on it
    — the nested :class:`DecisionProvenanceResponseBody` so callers
    see the full Decision → Document Revision provenance inline.
    """
    party_id = _require_party_id(x_actor_party_id)

    try:
        with engine.begin() as connection:
            chain: TrailProvenanceChain = navigator.navigate_trail_revision(
                connection,
                trail_id=trail_id,
                trail_revision_id=revision_id,
                party_id=party_id,
            )
    except TrailRevisionUnresolvableError as exc:
        raise _trail_revision_unresolvable_to_http(exc) from exc

    decision_chain_body: Optional[DecisionProvenanceResponseBody] = None
    if chain.decision_chain is not None:
        decision_chain_body = _decision_chain_to_body(chain.decision_chain)

    return TrailProvenanceResponseBody(
        trail_revision=TrailRevisionNodeBody(
            trail_id=chain.trail_revision.trail_id,
            trail_revision_id=chain.trail_revision.trail_revision_id,
            predecessor_revision_id=(
                chain.trail_revision.predecessor_revision_id
            ),
            purpose=chain.trail_revision.purpose,
            audience_id=chain.trail_revision.audience_id,
            ordering_rationale=chain.trail_revision.ordering_rationale,
            authoring_party_id=chain.trail_revision.authoring_party_id,
            recorded_at=chain.trail_revision.recorded_at,
        ),
        steps=[
            TrailStepNodeBody(
                trail_step_id=s.trail_step_id,
                ordinal=s.ordinal,
                target_kind=s.target_kind,
                target_id=s.target_id,
                target_revision_id=s.target_revision_id,
                region_id=s.region_id,
                selection_mode=s.selection_mode,
                annotation=s.annotation,
            )
            for s in chain.steps
        ],
        decision_chain=decision_chain_body,
        gap_descriptors=_gap_descriptors_to_body(chain.gap_descriptors),
        requested_trail_id=chain.requested_trail_id,
        requested_trail_revision_id=chain.requested_trail_revision_id,
    )


# ===========================================================================
# Slice 3 — additive Execution Provenance Chain endpoints (task 15.2).
#
# Design reference: ``.kiro/specs/third-walking-slice/design.md``
# §"Provenance_Navigator (extended)" HTTP surface. The endpoints
# expose the three new traversals
# :meth:`ProvenanceNavigator.navigate_completion`,
# :meth:`ProvenanceNavigator.navigate_deliverable_production`, and
# :meth:`ProvenanceNavigator.navigate_produced_deliverable_revision`
# added additively in task 12.1 / 12.2. The existing Slice 1 / Slice 2
# endpoints above are NOT modified (Requirement 40.1 — Reuse and
# Non-Modification of Slice 1 and Slice 2 Contexts); the additions
# below only ever append to :data:`router`.
#
# Wiring contract:
#
# - Routes resolve the requesting Party Identity via the Slice 1
#   :class:`RequestContext` dependency (orchestrator note for task
#   15.2 / Slice 2 pattern). The placeholder ``X-Actor-Party-Id``
#   header is left in place on the Slice 1 endpoints unchanged.
# - Each traversal is wrapped in ``ctx.engine.begin()`` so the
#   per-stage authorization-evaluation audit rows commit alongside
#   the non-consequential read.
# - Errors map to:
#     - :class:`CompletionUnresolvableError` → 404 with
#       ``error_code = 'completion_not_found'``.
#     - :class:`DeliverableProductionUnresolvableError` → 404 with
#       ``error_code = 'deliverable_production_not_found'``.
#     - :class:`DeliverableRevisionUnresolvableError` → 404 with
#       ``error_code = 'deliverable_revision_not_found'``.
#   The Provenance_Navigator raises the same exception for the
#   unresolved-Identity case and the restricted-but-existing case so
#   the response form is indistinguishable per AD-WS-9 rule 1
#   (Requirements 31.5, 31.6, 35.6, 35.7).
#
# Requirements satisfied (by the surface added below):
#     31.1, 31.2, 31.3 — Completion-rooted Execution Provenance Chain
#         exposed via a GET endpoint.
#     35.1, 35.2, 35.5, 35.8 — Short-form traversal endpoints rooted
#         at a Deliverable Production Record and a produced
#         Deliverable Revision.
#     31.5, 31.6, 35.6, 35.7 — Indistinguishable-not-found responses
#         carry only the unresolvable Identity reference, no
#         neighbouring identifiers, and use the same response shape
#         for unresolved and restricted cases.
# ===========================================================================

from walking_slice.auth_middleware import RequestContext
from walking_slice.provenance import (
    CompletionNode,
    CompletionUnresolvableError,
    DeliverableProductionNode,
    DeliverableProductionUnresolvableError,
    DeliverableRevisionNode,
    DeliverableRevisionUnresolvableError,
    ExecutionProvenanceTree,
    MilestoneAcceptanceNode,
    MilestoneAcceptanceProductionChain,
    PlanApprovalProvenance,
    TimeEntryNode,
    WorkAssignmentExecutionChain,
    WorkAssignmentNode,
    WorkEventNode,
)


__all__ = __all__ + [  # type: ignore[name-defined]
    "CompletionNodeBody",
    "CompletionProvenanceResponseBody",
    "DeliverableProductionNodeBody",
    "DeliverableProductionProvenanceResponseBody",
    "DeliverableRevisionNodeBody",
    "DeliverableRevisionProvenanceResponseBody",
    "MilestoneAcceptanceChainBody",
    "MilestoneAcceptanceNodeBody",
    "PlanApprovalProvenanceBody",
    "TimeEntryNodeBody",
    "WorkAssignmentChainBody",
    "WorkAssignmentNodeBody",
    "WorkEventNodeBody",
    "get_request_context",
]


# ---------------------------------------------------------------------------
# Dependency-injection placeholder for the Slice 3 RequestContext.
#
# ``walking_slice.routes.provenance`` is imported at top level by
# :mod:`walking_slice.app`, so importing ``get_request_context`` directly
# from :mod:`walking_slice.app` here would form a circular import (every
# Slice 1 / Slice 2 route module that needs the bundle reaches it through
# its own local placeholder; the planning routes break the cycle by
# being lazy-imported inside :func:`create_app`). Slice 3 follows the
# local-placeholder convention used by every other route module in this
# package — ``get_engine`` / ``get_provenance_navigator`` above —
# so app composition (task 15.3) just adds one more override to
# :attr:`fastapi.FastAPI.dependency_overrides` pointing this symbol at
# :class:`walking_slice.auth_middleware.RequestContextResolver`. Tests
# override the same symbol on their per-test FastAPI instances.
# ---------------------------------------------------------------------------


def get_request_context() -> RequestContext:
    """Placeholder dependency resolved to :class:`RequestContextResolver` by ``create_app`` (task 15.3).

    Mirrors :func:`walking_slice.app.get_request_context` semantically:
    the unwrapped function exists only to give FastAPI a stable symbol
    to key ``dependency_overrides`` against; calling it directly is a
    wiring error and raises immediately so the test surface fails
    loudly rather than silently returning ``None``.

    Task 15.3 wires this placeholder by adding
    ``overrides[provenance_routes.get_request_context] =
    services.request_context_resolver`` to :func:`create_app`.
    """
    raise NotImplementedError(
        "walking_slice.routes.provenance.get_request_context must be "
        "overridden by app composition (task 15.3) or test fixtures."
    )


# ---------------------------------------------------------------------------
# Slice 3 node body classes (additive — do not modify the Slice 1 / 2 bodies).
# ---------------------------------------------------------------------------


class CompletionNodeBody(BaseModel):
    """Serialized :class:`CompletionNode` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["completion_record"] = "completion_record"
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


class WorkAssignmentNodeBody(BaseModel):
    """Serialized :class:`WorkAssignmentNode` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["work_assignment_record"] = "work_assignment_record"
    work_assignment_id: str
    target_plan_revision_id: str
    assignee_party_id: str
    assignment_authority_party_id: str
    assignment_rationale: Optional[str] = None
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


class WorkEventNodeBody(BaseModel):
    """Serialized :class:`WorkEventNode` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["work_event_record"] = "work_event_record"
    work_event_id: str
    target_work_assignment_id: str
    event_kind: str
    event_note: Optional[str] = None
    recording_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


class TimeEntryNodeBody(BaseModel):
    """Serialized :class:`TimeEntryNode` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["time_entry_record"] = "time_entry_record"
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


class MilestoneAcceptanceNodeBody(BaseModel):
    """Serialized :class:`MilestoneAcceptanceNode` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["milestone_acceptance_record"] = "milestone_acceptance_record"
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


class DeliverableProductionNodeBody(BaseModel):
    """Serialized :class:`DeliverableProductionNode` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "deliverable_production_record"
    ] = "deliverable_production_record"
    deliverable_production_id: str
    source_work_assignment_id: str
    produced_deliverable_id: str
    produced_deliverable_revision_id: str
    target_deliverable_expectation_id: str
    target_deliverable_expectation_revision_id: str
    production_rationale: Optional[str] = None
    recording_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


class DeliverableRevisionNodeBody(BaseModel):
    """Serialized :class:`DeliverableRevisionNode` for HTTP responses.

    Per Requirement 35.8 the node carries both
    ``role_marker = 'generated_output'`` and the
    ``content_digest_sha256`` so callers can distinguish a produced
    Deliverable Revision from any Slice 1 Source Evidence Document
    Revision without a second lookup.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["deliverable_revision"] = "deliverable_revision"
    deliverable_id: str
    deliverable_revision_id: str
    content_type: str
    content_digest_sha256: str
    role_marker: str
    originating_work_assignment_id: str
    authoring_party_id: str
    recorded_at: str


# A node entry on the wire is either the visible node body or the
# generic AD-WS-9 redaction marker.
WorkAssignmentOrRedacted = Union[WorkAssignmentNodeBody, RedactedNodeBody]
WorkEventOrRedacted = Union[WorkEventNodeBody, RedactedNodeBody]
TimeEntryOrRedacted = Union[TimeEntryNodeBody, RedactedNodeBody]
MilestoneAcceptanceOrRedacted = Union[MilestoneAcceptanceNodeBody, RedactedNodeBody]
DeliverableProductionOrRedacted = Union[
    DeliverableProductionNodeBody, RedactedNodeBody
]
DeliverableRevisionOrRedacted = Union[DeliverableRevisionNodeBody, RedactedNodeBody]


class WorkAssignmentChainBody(BaseModel):
    """Serialized :class:`WorkAssignmentExecutionChain` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    work_assignment: WorkAssignmentOrRedacted
    work_events: list[WorkEventOrRedacted]
    time_entries: list[TimeEntryOrRedacted]


class MilestoneAcceptanceChainBody(BaseModel):
    """Serialized :class:`MilestoneAcceptanceProductionChain` for HTTP responses."""

    model_config = ConfigDict(extra="forbid")

    milestone_acceptance: MilestoneAcceptanceOrRedacted
    deliverable_production: Optional[DeliverableProductionOrRedacted] = None
    produced_deliverable_revision: Optional[DeliverableRevisionOrRedacted] = None


class PlanApprovalProvenanceBody(BaseModel):
    """Minimal serialized envelope of the delegated Slice 2 Planning chain.

    The full Slice 2 Plan Approval provenance is exposed by
    :func:`get_plan_approval_provenance` in
    :mod:`walking_slice.planning._routes`; this envelope carries only
    the Plan Approval Identity (and the requested Plan Approval
    Identity) so callers can follow up with a dedicated GET to the
    Planning provenance route when they want the full chain. Keeping
    the embedded body small bounds the Slice 3 traversal response
    size and avoids duplicating the full Slice 2 wire shape on every
    Completion-rooted walk.
    """

    model_config = ConfigDict(extra="forbid")

    plan_approval_id: str
    target_activity_plan_id: str
    target_plan_revision_id: str
    requested_plan_approval_id: str


class CompletionProvenanceResponseBody(BaseModel):
    """200 response body for ``GET /completions/{id}/provenance``."""

    model_config = ConfigDict(extra="forbid")

    completion: CompletionNodeBody
    plan_approval_chain: Optional[PlanApprovalProvenanceBody] = None
    milestone_acceptance_chains: list[MilestoneAcceptanceChainBody]
    work_assignment_chains: list[WorkAssignmentChainBody]
    gap_descriptors: list[GapDescriptorBody]
    requested_anchor_kind: str
    requested_anchor_id: str
    requested_completion_id: str


class DeliverableProductionProvenanceResponseBody(BaseModel):
    """200 response body for ``GET /deliverable-productions/{id}/provenance``."""

    model_config = ConfigDict(extra="forbid")

    production_anchor: DeliverableProductionNodeBody
    produced_revision_anchor: Optional[DeliverableRevisionOrRedacted] = None
    plan_approval_chain: Optional[PlanApprovalProvenanceBody] = None
    work_assignment_chains: list[WorkAssignmentChainBody]
    gap_descriptors: list[GapDescriptorBody]
    requested_anchor_kind: str
    requested_anchor_id: str


class DeliverableRevisionProvenanceResponseBody(BaseModel):
    """200 response body for the produced-Revision provenance walk."""

    model_config = ConfigDict(extra="forbid")

    produced_revision_anchor: DeliverableRevisionNodeBody
    plan_approval_chain: Optional[PlanApprovalProvenanceBody] = None
    work_assignment_chains: list[WorkAssignmentChainBody]
    gap_descriptors: list[GapDescriptorBody]
    requested_anchor_kind: str
    requested_anchor_id: str


# ---------------------------------------------------------------------------
# Node → response body mappers (Slice 3).
# ---------------------------------------------------------------------------


def _completion_node_to_body(node: CompletionNode) -> CompletionNodeBody:
    return CompletionNodeBody(
        completion_id=node.completion_id,
        target_plan_revision_id=node.target_plan_revision_id,
        target_activity_plan_id=node.target_activity_plan_id,
        target_project_id=node.target_project_id,
        outcome=node.outcome,
        rationale=node.rationale,
        source_milestone_acceptance_ids_json=(
            node.source_milestone_acceptance_ids_json
        ),
        completing_party_id=node.completing_party_id,
        authority_basis_type=node.authority_basis_type,
        authority_basis_id=node.authority_basis_id,
        applicable_scope=node.applicable_scope,
        recorded_at=node.recorded_at,
    )


def _work_assignment_node_to_body(node) -> WorkAssignmentOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return WorkAssignmentNodeBody(
        work_assignment_id=node.work_assignment_id,
        target_plan_revision_id=node.target_plan_revision_id,
        assignee_party_id=node.assignee_party_id,
        assignment_authority_party_id=node.assignment_authority_party_id,
        assignment_rationale=node.assignment_rationale,
        authority_basis_type=node.authority_basis_type,
        authority_basis_id=node.authority_basis_id,
        applicable_scope=node.applicable_scope,
        recorded_at=node.recorded_at,
    )


def _work_event_node_to_body(node) -> WorkEventOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return WorkEventNodeBody(
        work_event_id=node.work_event_id,
        target_work_assignment_id=node.target_work_assignment_id,
        event_kind=node.event_kind,
        event_note=node.event_note,
        recording_party_id=node.recording_party_id,
        authority_basis_type=node.authority_basis_type,
        authority_basis_id=node.authority_basis_id,
        applicable_scope=node.applicable_scope,
        recorded_at=node.recorded_at,
    )


def _time_entry_node_to_body(node) -> TimeEntryOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return TimeEntryNodeBody(
        time_entry_id=node.time_entry_id,
        target_work_assignment_id=node.target_work_assignment_id,
        effort_hours=node.effort_hours,
        effort_period_start=node.effort_period_start,
        effort_period_end=node.effort_period_end,
        recording_party_id=node.recording_party_id,
        authority_basis_type=node.authority_basis_type,
        authority_basis_id=node.authority_basis_id,
        applicable_scope=node.applicable_scope,
        recorded_at=node.recorded_at,
    )


def _milestone_acceptance_node_to_body(node) -> MilestoneAcceptanceOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return MilestoneAcceptanceNodeBody(
        milestone_acceptance_id=node.milestone_acceptance_id,
        source_deliverable_production_id=node.source_deliverable_production_id,
        produced_deliverable_id=node.produced_deliverable_id,
        produced_deliverable_revision_id=node.produced_deliverable_revision_id,
        target_deliverable_expectation_id=node.target_deliverable_expectation_id,
        target_deliverable_expectation_revision_id=(
            node.target_deliverable_expectation_revision_id
        ),
        outcome=node.outcome,
        rationale=node.rationale,
        accepting_party_id=node.accepting_party_id,
        authority_basis_type=node.authority_basis_type,
        authority_basis_id=node.authority_basis_id,
        applicable_scope=node.applicable_scope,
        recorded_at=node.recorded_at,
    )


def _deliverable_production_node_to_body(node) -> DeliverableProductionOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return DeliverableProductionNodeBody(
        deliverable_production_id=node.deliverable_production_id,
        source_work_assignment_id=node.source_work_assignment_id,
        produced_deliverable_id=node.produced_deliverable_id,
        produced_deliverable_revision_id=node.produced_deliverable_revision_id,
        target_deliverable_expectation_id=node.target_deliverable_expectation_id,
        target_deliverable_expectation_revision_id=(
            node.target_deliverable_expectation_revision_id
        ),
        production_rationale=node.production_rationale,
        recording_party_id=node.recording_party_id,
        authority_basis_type=node.authority_basis_type,
        authority_basis_id=node.authority_basis_id,
        applicable_scope=node.applicable_scope,
        recorded_at=node.recorded_at,
    )


def _deliverable_revision_node_to_body(node) -> DeliverableRevisionOrRedacted:
    if isinstance(node, RedactedNode):
        return _redacted_to_body(node)
    return DeliverableRevisionNodeBody(
        deliverable_id=node.deliverable_id,
        deliverable_revision_id=node.deliverable_revision_id,
        content_type=node.content_type,
        content_digest_sha256=node.content_digest_sha256,
        role_marker=node.role_marker,
        originating_work_assignment_id=node.originating_work_assignment_id,
        authoring_party_id=node.authoring_party_id,
        recorded_at=node.recorded_at,
    )


def _work_assignment_chain_to_body(
    chain: WorkAssignmentExecutionChain,
) -> WorkAssignmentChainBody:
    return WorkAssignmentChainBody(
        work_assignment=_work_assignment_node_to_body(chain.work_assignment),
        work_events=[_work_event_node_to_body(e) for e in chain.work_events],
        time_entries=[_time_entry_node_to_body(e) for e in chain.time_entries],
    )


def _milestone_chain_to_body(
    chain: MilestoneAcceptanceProductionChain,
) -> MilestoneAcceptanceChainBody:
    production: Optional[DeliverableProductionOrRedacted] = None
    if chain.deliverable_production is not None:
        production = _deliverable_production_node_to_body(
            chain.deliverable_production
        )
    revision: Optional[DeliverableRevisionOrRedacted] = None
    if chain.produced_deliverable_revision is not None:
        revision = _deliverable_revision_node_to_body(
            chain.produced_deliverable_revision
        )
    return MilestoneAcceptanceChainBody(
        milestone_acceptance=_milestone_acceptance_node_to_body(
            chain.milestone_acceptance
        ),
        deliverable_production=production,
        produced_deliverable_revision=revision,
    )


def _plan_approval_chain_to_body(
    chain: Optional[PlanApprovalProvenance],
) -> Optional[PlanApprovalProvenanceBody]:
    """Serialize the delegated Slice 2 chain envelope.

    Returns ``None`` for both the unresolved-Plan-Approval and the
    restricted-Plan-Approval cases so the response is indistinguishable
    per Requirement 35.7 / AD-WS-9 rule 3.
    """
    if chain is None:
        return None
    return PlanApprovalProvenanceBody(
        plan_approval_id=chain.plan_approval.plan_approval_id,
        target_activity_plan_id=chain.plan_approval.target_activity_plan_id,
        target_plan_revision_id=chain.plan_approval.target_plan_revision_id,
        requested_plan_approval_id=chain.requested_plan_approval_id,
    )


# ---------------------------------------------------------------------------
# Error mappers — Slice 3 unresolvable cases.
# ---------------------------------------------------------------------------


def _completion_unresolvable_to_http(
    exc: CompletionUnresolvableError,
) -> HTTPException:
    """Map :class:`CompletionUnresolvableError` to a 404 (indistinguishable shape).

    Per Requirement 31.5 / 31.6 the body identifies only the
    unresolvable Completion reference. The same exception is raised
    for the restricted case so the response is byte-equivalent to
    the unresolved one.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="completion_not_found",
            message=str(exc),
            region_id=exc.completion_id,
        ).model_dump(),
    )


def _deliverable_production_unresolvable_to_http(
    exc: DeliverableProductionUnresolvableError,
) -> HTTPException:
    """Map :class:`DeliverableProductionUnresolvableError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="deliverable_production_not_found",
            message=str(exc),
            region_id=exc.deliverable_production_id,
        ).model_dump(),
    )


def _deliverable_revision_unresolvable_to_http(
    exc: DeliverableRevisionUnresolvableError,
) -> HTTPException:
    """Map :class:`DeliverableRevisionUnresolvableError` to a 404."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=ErrorBody(
            error="deliverable_revision_not_found",
            message=str(exc),
            region_id=exc.deliverable_revision_id,
        ).model_dump(),
    )


# ---------------------------------------------------------------------------
# Tree → response body mappers.
# ---------------------------------------------------------------------------


def _completion_tree_to_body(
    tree: ExecutionProvenanceTree,
) -> CompletionProvenanceResponseBody:
    assert tree.completion is not None, (
        "navigate_completion must return a Completion anchor"
    )
    return CompletionProvenanceResponseBody(
        completion=_completion_node_to_body(tree.completion),
        plan_approval_chain=_plan_approval_chain_to_body(tree.plan_approval_chain),
        milestone_acceptance_chains=[
            _milestone_chain_to_body(chain)
            for chain in tree.milestone_acceptance_chains
        ],
        work_assignment_chains=[
            _work_assignment_chain_to_body(chain)
            for chain in tree.work_assignment_chains
        ],
        gap_descriptors=_gap_descriptors_to_body(tree.gap_descriptors),
        requested_anchor_kind=tree.requested_anchor_kind or "completion_record",
        requested_anchor_id=(
            tree.requested_anchor_id or tree.requested_completion_id
        ),
        requested_completion_id=tree.requested_completion_id,
    )


def _deliverable_production_tree_to_body(
    tree: ExecutionProvenanceTree,
) -> DeliverableProductionProvenanceResponseBody:
    assert tree.production_anchor is not None, (
        "navigate_deliverable_production must return a Production anchor"
    )
    # ``production_anchor`` is guaranteed to be a visible
    # :class:`DeliverableProductionNode` (restricted Production targets
    # raise :class:`DeliverableProductionUnresolvableError`), so the
    # cast below is safe.
    production_body = _deliverable_production_node_to_body(tree.production_anchor)
    assert isinstance(production_body, DeliverableProductionNodeBody), (
        "production anchor must be visible per AD-WS-9 indistinguishable-"
        "not-found contract"
    )
    revision_body: Optional[DeliverableRevisionOrRedacted] = None
    if tree.produced_revision_anchor is not None:
        revision_body = _deliverable_revision_node_to_body(
            tree.produced_revision_anchor
        )
    return DeliverableProductionProvenanceResponseBody(
        production_anchor=production_body,
        produced_revision_anchor=revision_body,
        plan_approval_chain=_plan_approval_chain_to_body(tree.plan_approval_chain),
        work_assignment_chains=[
            _work_assignment_chain_to_body(chain)
            for chain in tree.work_assignment_chains
        ],
        gap_descriptors=_gap_descriptors_to_body(tree.gap_descriptors),
        requested_anchor_kind=(
            tree.requested_anchor_kind or "deliverable_production_record"
        ),
        requested_anchor_id=tree.requested_anchor_id,
    )


def _deliverable_revision_tree_to_body(
    tree: ExecutionProvenanceTree,
) -> DeliverableRevisionProvenanceResponseBody:
    assert tree.produced_revision_anchor is not None, (
        "navigate_produced_deliverable_revision must return a Revision anchor"
    )
    revision_body = _deliverable_revision_node_to_body(
        tree.produced_revision_anchor
    )
    assert isinstance(revision_body, DeliverableRevisionNodeBody), (
        "revision anchor must be visible per AD-WS-9 indistinguishable-"
        "not-found contract"
    )
    return DeliverableRevisionProvenanceResponseBody(
        produced_revision_anchor=revision_body,
        plan_approval_chain=_plan_approval_chain_to_body(tree.plan_approval_chain),
        work_assignment_chains=[
            _work_assignment_chain_to_body(chain)
            for chain in tree.work_assignment_chains
        ],
        gap_descriptors=_gap_descriptors_to_body(tree.gap_descriptors),
        requested_anchor_kind=tree.requested_anchor_kind or "deliverable_revision",
        requested_anchor_id=tree.requested_anchor_id,
    )


# ---------------------------------------------------------------------------
# Endpoints (Slice 3).
# ---------------------------------------------------------------------------


@router.get(
    "/completions/{completion_id}/provenance",
    response_model=CompletionProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Walk the Execution Provenance Chain rooted at a Completion "
        "Record back to the originating Decision and Document Revision."
    ),
)
async def get_completion_provenance(
    completion_id: Annotated[str, Path(min_length=1)],
    ctx: "RequestContext" = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> CompletionProvenanceResponseBody:
    """Return the full Execution Provenance Chain rooted at ``completion_id``.

    Delegates to :meth:`ProvenanceNavigator.navigate_completion`,
    which walks the three legs (Planning leg via
    :meth:`navigate_plan_approval`, Milestone Acceptance leg, and
    Work Assignment leg) and surfaces a single
    :class:`ExecutionProvenanceTree`. The walk is wrapped in
    ``ctx.engine.begin()`` so the per-stage authorization-evaluation
    audit rows commit alongside the (non-consequential) read.

    Errors:

    - :class:`CompletionUnresolvableError` → 404. The same exception
      is raised for both the unresolved-Identity case and the
      restricted-but-existing case so the response is indistinguishable
      per Requirement 31.6 / AD-WS-9 rule 1.
    """
    try:
        with ctx.engine.begin() as connection:
            tree: ExecutionProvenanceTree = navigator.navigate_completion(
                connection,
                completion_id=completion_id,
                party_id=ctx.party_id,
            )
    except CompletionUnresolvableError as exc:
        raise _completion_unresolvable_to_http(exc) from exc

    return _completion_tree_to_body(tree)


@router.get(
    "/deliverable-productions/{deliverable_production_id}/provenance",
    response_model=DeliverableProductionProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Walk the Execution Provenance Chain rooted at a Deliverable "
        "Production Record (short-form upward traversal)."
    ),
)
async def get_deliverable_production_provenance(
    deliverable_production_id: Annotated[str, Path(min_length=1)],
    ctx: "RequestContext" = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> DeliverableProductionProvenanceResponseBody:
    """Return the provenance chain rooted at a Deliverable Production Record.

    Delegates to
    :meth:`ProvenanceNavigator.navigate_deliverable_production`. The
    short-form traversal walks the Planning leg (via the resolved
    source Work Assignment's Plan Revision → Plan Approval delegation)
    and the single-Work-Assignment leg; the Milestone Acceptance leg
    is empty for this anchor by design (the Production sits *below*
    the Milestone Acceptance fan; the anchor itself is surfaced on
    :attr:`production_anchor` and the produced Revision on
    :attr:`produced_revision_anchor`).

    Errors:

    - :class:`DeliverableProductionUnresolvableError` → 404 with the
      indistinguishable response shape (Requirements 35.6, 35.7).
    """
    try:
        with ctx.engine.begin() as connection:
            tree: ExecutionProvenanceTree = (
                navigator.navigate_deliverable_production(
                    connection,
                    deliverable_production_id=deliverable_production_id,
                    party_id=ctx.party_id,
                )
            )
    except DeliverableProductionUnresolvableError as exc:
        raise _deliverable_production_unresolvable_to_http(exc) from exc

    return _deliverable_production_tree_to_body(tree)


@router.get(
    "/deliverables/{deliverable_id}/revisions/"
    "{deliverable_revision_id}/provenance",
    response_model=DeliverableRevisionProvenanceResponseBody,
    status_code=status.HTTP_200_OK,
    responses={
        status.HTTP_400_BAD_REQUEST: {"model": ErrorBody},
        status.HTTP_404_NOT_FOUND: {"model": ErrorBody},
    },
    summary=(
        "Walk the Execution Provenance Chain rooted at a produced "
        "Deliverable Revision (short-form upward traversal)."
    ),
)
async def get_produced_deliverable_revision_provenance(
    deliverable_id: Annotated[str, Path(min_length=1)],
    deliverable_revision_id: Annotated[str, Path(min_length=1)],
    ctx: "RequestContext" = Depends(get_request_context),
    navigator: ProvenanceNavigator = Depends(get_provenance_navigator),
) -> DeliverableRevisionProvenanceResponseBody:
    """Return the provenance chain rooted at a produced Deliverable Revision.

    Delegates to
    :meth:`ProvenanceNavigator.navigate_produced_deliverable_revision`.
    The traversal walks back through the originating Work Assignment
    Record to the Plan Approval and on through the Slice 2 / Slice 1
    chain. The produced Revision anchor on the response body carries
    both ``role_marker = 'generated_output'`` and the
    ``content_digest_sha256`` per Requirement 35.8 so an auditor can
    verify the byte-equivalence of the content separately via
    ``GET /deliverables/{id}/revisions/{rid}/content``.

    The endpoint additionally requires the path's ``deliverable_id``
    to match the resolved Revision's Resource Identity. Mismatched
    composite keys surface as a 404 with the same shape as the
    unresolved case so the response is uniform; the navigator's
    :class:`DeliverableRevisionUnresolvableError` covers the
    not-found and restricted cases identically.
    """
    try:
        with ctx.engine.begin() as connection:
            tree: ExecutionProvenanceTree = (
                navigator.navigate_produced_deliverable_revision(
                    connection,
                    deliverable_revision_id=deliverable_revision_id,
                    party_id=ctx.party_id,
                )
            )
    except DeliverableRevisionUnresolvableError as exc:
        raise _deliverable_revision_unresolvable_to_http(exc) from exc

    # Defense in depth — surface a 404 in the same shape as the
    # unresolved-Identity branch when the path's ``deliverable_id``
    # does not match the resolved Revision's Resource Identity. The
    # navigator already verifies the Revision exists; this extra
    # check protects callers from accidentally walking a Revision
    # under the wrong Resource URL.
    anchor = tree.produced_revision_anchor
    if (
        anchor is None
        or isinstance(anchor, RedactedNode)
        or getattr(anchor, "deliverable_id", None) != deliverable_id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ErrorBody(
                error="deliverable_revision_not_found",
                message=(
                    f"Deliverable Revision {deliverable_revision_id!r} does "
                    f"not resolve under Deliverable Resource "
                    f"{deliverable_id!r}."
                ),
                region_id=deliverable_revision_id,
                revision_id=deliverable_id,
            ).model_dump(),
        )

    return _deliverable_revision_tree_to_body(tree)
