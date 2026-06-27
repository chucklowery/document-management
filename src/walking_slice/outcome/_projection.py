"""Outcome-status Projection — the single explainable Projection Slice 4 surfaces.

Design reference
================

``.kiro/specs/fourth-walking-slice/design.md`` §"Outcome-status Projection
(single explainable Projection)":

- Compute one explainable Projection of current outcome status for a given
  Intended Outcome Revision Identity, derived from source Slice 4 Records.
- The Projection Definition (design §"Outcome-status Projection" → *Projection
  Definition*) resolves the target Intended Outcome Revision, then walks the
  Measurement Definition → Measurement Record → Observed Outcome →
  Success-Condition Assessment → Outcome Review legs, marking the
  most-progressed label observed and falling back to ``Provenance incomplete``
  when a required source link is unresolved.
- The Projection is wrapped in the existing Slice 1
  :class:`walking_slice.projection.ProjectionEnvelope` carrying the Projection
  Definition, source Record Identities, source Revision Identities, applicable
  temporal boundary (ISO-8601 ≥ second precision), generated time, and a
  derivation indicator distinguishing the status from authoritative source
  Records and from the Outcome Review Record itself (Requirements 59.1 / 59.2,
  Principle 5.23).

Task scope (task 11.1)
=====================

This module exposes :class:`OutcomeStatusProjection` and
:func:`project_outcome_status`.

Authority (design §"Outcome-status Projection" → *Authority*). The Projection
requires ``view`` authority on the target Intended Outcome Revision; an
unresolvable target, a target whose ``outcome_kind`` is not ``'intended'``, and
a target the requesting Party may not view are all surfaced through the *same*
:class:`OutcomeStatusTargetUnresolvableError`, so the response is
indistinguishable per AD-WS-9 (Requirement 50.x / Slice 1 Requirement 11.7).

Withholding (Requirement 59.5). On an unresolvable Projection Definition the
status is withheld and an
:class:`walking_slice.projection.ExplanationUnavailableResponse` naming the
missing element is returned; no row is read or written that would alter a
source Record, so source Records remain byte-equivalent.

Prohibited derived values (Requirement 59.3). :class:`OutcomeStatusProjection`
carries only the projected status label and the envelope — it never carries a
percent-attainment, cost-per-outcome, ROI, budget-variance, forecast-attainment,
causal-attribution probability, or cross-Outcome aggregate value.

Distinction from Observed Outcome (Requirement 59.6). The Projection is labelled
as a *projection of outcome-measurement progress* via its dedicated type and
status enumeration; it is never aliased as an Observed Outcome Resource /
Revision, Success-Condition Assessment, or Outcome Review Record, and is never
persisted or cited from any Outcome Review.

Requirements satisfied (per task 11.1):
    59.1 — every exposed projected status carries the Projection Definition,
           source Record + Revision Identities, applicable temporal boundary,
           and generated time (ISO-8601 ≥ second precision).
    59.2 — every exposed projected status carries a derivation indicator
           distinguishing it from authoritative source Records and from the
           Outcome Review Record itself.
    59.3 — no prohibited derived value is surfaced.
    59.4 — the Projection is purely derived: it reads source Records and never
           mutates them, so corrected / late-arriving facts leave prior Records
           byte-equivalent.
    59.5 — an unresolvable Projection Definition withholds the status and
           returns an explanation-unavailable indicator naming the missing
           element.
    59.6 — the Projection is never aliased as an Observed Outcome, Assessment,
           or Outcome Review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final, Literal, Mapping, Optional, Union
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.authorization import AuthorizationService, TargetRef
from walking_slice.clock import Clock
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)


__all__ = [
    "OUTCOME_STATUS_PROJECTION_DEFINITION",
    "OutcomeStatusProjection",
    "OutcomeStatusTargetUnresolvableError",
    "project_outcome_status",
]


# ---------------------------------------------------------------------------
# Projection Definition (design §"Outcome-status Projection").
# ---------------------------------------------------------------------------


# The named, versioned computation this module implements. Carried verbatim on
# every :class:`ProjectionEnvelope` so a reviewer can read a response and
# immediately tell which Projection produced the status (Requirement 59.1).
_PROJECTION_DEFINITION_NAME: Final[str] = "outcome.status"
_PROJECTION_DEFINITION_VERSION: Final[str] = "1.0"

OUTCOME_STATUS_PROJECTION_DEFINITION: Final[ProjectionDefinition] = (
    ProjectionDefinition(
        name=_PROJECTION_DEFINITION_NAME,
        version=_PROJECTION_DEFINITION_VERSION,
    )
)
"""The Slice 4 outcome-status :class:`ProjectionDefinition`.

Exported so the HTTP layer (task 13.1) and tests share one authoritative
``(name, version)`` pair. Seeded into the default definition registry consulted
by :func:`project_outcome_status`; an empty / alternate registry that does not
contain :data:`_PROJECTION_DEFINITION_NAME` drives the Requirement 59.5
explanation-unavailable path.
"""


_DEFAULT_DEFINITION_REGISTRY: Final[Mapping[str, ProjectionDefinition]] = (
    MappingProxyType(
        {_PROJECTION_DEFINITION_NAME: OUTCOME_STATUS_PROJECTION_DEFINITION}
    )
)


# ---------------------------------------------------------------------------
# Authorization (design §"Outcome-status Projection" → *Authority*).
# ---------------------------------------------------------------------------


# ``view.<resource_kind>`` maps to ``view`` authority per
# :func:`walking_slice.authorization._required_authority`. The target node kind
# matches the Slice 2 Intended Outcome Revision node kind used elsewhere.
_AUTHORIZATION_ACTION_VIEW_INTENDED_OUTCOME_REVISION: Final[str] = (
    "view.intended_outcome_revision"
)
_NODE_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"


# The Slice 2 ``outcome_kind`` discriminator the resolved target Intended
# Outcome Revision must carry (design step 1 / Requirement 44.4 reuse).
_OUTCOME_KIND_INTENDED: Final[str] = "intended"


# ---------------------------------------------------------------------------
# Status labels (design §"Outcome-status Projection" → *Public surface*).
# ---------------------------------------------------------------------------


_STATUS_UNMEASURED: Final[str] = "Intended Outcome unmeasured"
_STATUS_MEASUREMENT_DEFINED: Final[str] = "Intended Outcome measurement defined"
_STATUS_MEASURED: Final[str] = "Intended Outcome measured"
_STATUS_OBSERVED: Final[str] = "Intended Outcome observed"
_STATUS_REVIEWED: Final[str] = "Intended Outcome reviewed"
_STATUS_PROVENANCE_INCOMPLETE: Final[str] = "Provenance incomplete"

# Success-Condition Assessment category → projected status label
# (design step 5). The four categories mirror the
# ``Success_Condition_Assessment_Records.assessment_category`` CHECK.
_ASSESSMENT_CATEGORY_TO_STATUS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Satisfied": "Intended Outcome success condition satisfied",
        "Partially_Satisfied": (
            "Intended Outcome success condition partially satisfied"
        ),
        "Not_Satisfied": "Intended Outcome success condition not satisfied",
        "Unassessable": "Intended Outcome success condition unassessable",
    }
)


# ---------------------------------------------------------------------------
# Public value object.
# ---------------------------------------------------------------------------


OutcomeStatusLabel = Literal[
    "Intended Outcome unmeasured",
    "Intended Outcome measurement defined",
    "Intended Outcome measured",
    "Intended Outcome observed",
    "Intended Outcome success condition satisfied",
    "Intended Outcome success condition partially satisfied",
    "Intended Outcome success condition not satisfied",
    "Intended Outcome success condition unassessable",
    "Intended Outcome reviewed",
    "Provenance incomplete",
]


@dataclass(frozen=True)
class OutcomeStatusProjection:
    """The outcome-status Projection wrapped in its :class:`ProjectionEnvelope`.

    Mirrors design §"Outcome-status Projection" → *Public surface* verbatim.
    Carries *only* the target Intended Outcome Revision Identity, the projected
    status label, and the envelope — never a prohibited derived value
    (Requirement 59.3), and never a field that would constitute an observed
    measurement, Observed Outcome value, Success-Condition Assessment, or
    Outcome Review (Requirement 59.6). The envelope's derivation indicator marks
    the status as *derived*, distinguishing it from authoritative source Records
    and from the Outcome Review Record itself (Requirement 59.2).

    Attributes:
        intended_outcome_revision_id: The target Intended Outcome Revision
            Identity the Projection was computed for.
        projected_status: The most-progressed status label observed, or
            ``"Provenance incomplete"`` when a required source link is
            unresolved (design step 7).
        envelope: The :class:`ProjectionEnvelope` carrying the Projection
            Definition, source Record + Revision Identities, applicable
            temporal boundary, generated time, and derivation indicator
            (Requirement 59.1 / 59.2).
    """

    intended_outcome_revision_id: str
    projected_status: OutcomeStatusLabel
    envelope: ProjectionEnvelope


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class OutcomeStatusTargetUnresolvableError(LookupError):
    """Raised when the target Intended Outcome Revision cannot be projected.

    A single exception type covers three cases so the externally observable
    response is identical (AD-WS-9 indistinguishability, design §"Outcome-status
    Projection" → *Authority*):

    1. the named ``intended_outcome_revision_id`` does not resolve;
    2. it resolves but its ``outcome_kind`` is not ``'intended'``; and
    3. it resolves but the requesting Party lacks ``view`` authority on it.

    The exception carries only the offending identifier. It deliberately
    discloses nothing about whether the target exists or what it contains, so a
    Party without view authority cannot distinguish case 3 from cases 1 and 2
    (Requirement 50.x / Slice 1 Requirement 11.7).
    """

    def __init__(self, intended_outcome_revision_id: str) -> None:
        super().__init__(
            "Intended Outcome Revision "
            f"{intended_outcome_revision_id!r} is not a projectable "
            "outcome-status target (unresolvable, not an Intended Outcome, or "
            "view authority denied; AD-WS-9 indistinguishable)."
        )
        self.intended_outcome_revision_id = intended_outcome_revision_id


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _to_second_precision(value: datetime) -> datetime:
    """Truncate ``value`` to ISO-8601 second precision for envelope use.

    The :class:`ProjectionEnvelope` validators require ``microsecond == 0``.
    Clock and caller-supplied datetimes arrive at millisecond precision; this
    discards the sub-second component while preserving ``tzinfo`` so a naive
    value still trips the envelope validator with a precise error rather than
    being silently coerced.
    """
    if value.tzinfo is None:
        return value
    return value.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------


def project_outcome_status(
    connection: Connection,
    *,
    intended_outcome_revision_id: str,
    party_id: str,
    at: datetime,
    authorization_service: AuthorizationService,
    clock: Clock,
    definition_registry: Optional[Mapping[str, ProjectionDefinition]] = None,
) -> Union[OutcomeStatusProjection, ExplanationUnavailableResponse]:
    """Compute the outcome-status Projection for a target Intended Outcome Revision.

    Implements design §"Outcome-status Projection" → *Projection Definition*
    steps 1–7 and the surrounding authority / withholding / non-aliasing rules
    (Requirements 59.1 through 59.6, Principle 5.23, AD-WS-9).

    The collaborators ``authorization_service`` and ``clock`` are passed as
    keyword arguments (rather than bound on a service instance) so the function
    matches the design-pinned free-function entry point name while still
    evaluating per-Party ``view`` authority (design §"Outcome-status Projection"
    → *Authority*) and stamping the envelope's generated time. The Intended
    Outcome Revision and Measurement Definition reads use the additive Slice 2 /
    Slice 4 read APIs (``IntendedOutcomeService.get_revision`` per AD-WS-40 and
    ``MeasurementDefinitionService.get_definition_for_intended_outcome``); the
    remaining legs use direct indexed ``SELECT``s against the Slice 4 tables.
    No statement issued by this function mutates any row (Requirement 59.4).

    Args:
        connection: SQLAlchemy connection bound to the caller's read context.
        intended_outcome_revision_id: Identity of the target Intended Outcome
            Revision to project.
        party_id: Identity of the requesting Party; its ``view`` authority on
            the target Intended Outcome Revision is evaluated (AD-WS-9).
        at: The applicable temporal boundary — the instant up to which the
            Projection's sources are considered effective. Recorded on the
            envelope at second precision; must be a UTC, timezone-aware
            datetime.
        authorization_service: Evaluates ``view.intended_outcome_revision``
            authority on the target.
        clock: Source of the envelope's ``generated_at`` timestamp.
        definition_registry: Optional Projection-Definition registry. Defaults
            to a registry carrying only the outcome-status definition. An
            alternate registry omitting :data:`_PROJECTION_DEFINITION_NAME`
            drives the Requirement 59.5 explanation-unavailable path.

    Returns:
        An :class:`OutcomeStatusProjection` on the happy path; an
        :class:`ExplanationUnavailableResponse` naming the missing element when
        the Projection Definition cannot be resolved (Requirement 59.5).

    Raises:
        OutcomeStatusTargetUnresolvableError: The target does not resolve, is
            not an Intended Outcome, or the requesting Party lacks ``view``
            authority on it (the three cases are indistinguishable per
            AD-WS-9).
    """
    registry = (
        definition_registry
        if definition_registry is not None
        else _DEFAULT_DEFINITION_REGISTRY
    )

    # Step 1 — resolve the target Intended Outcome Revision and require
    # outcome_kind = 'intended' (AD-WS-40). The row is loaded before the
    # authority check so the TargetRef carries the row's applicable scope; the
    # row contents are never disclosed on the deny path (AD-WS-9).
    revision_row = IntendedOutcomeService.get_revision(
        connection, intended_outcome_revision_id
    )
    if revision_row is None or revision_row.outcome_kind != _OUTCOME_KIND_INTENDED:
        raise OutcomeStatusTargetUnresolvableError(intended_outcome_revision_id)

    # Authority — require view on the target Intended Outcome Revision. A deny
    # raises the same exception as the unresolvable / wrong-kind cases so the
    # response is indistinguishable (AD-WS-9).
    decision = authorization_service.evaluate(
        connection,
        party_id=party_id,
        action=_AUTHORIZATION_ACTION_VIEW_INTENDED_OUTCOME_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_INTENDED_OUTCOME_REVISION,
            id=revision_row.intended_outcome_id,
            revision_id=revision_row.intended_outcome_revision_id,
            scope=revision_row.applicable_scope,
        ),
        at=at,
    )
    if not decision.is_permit:
        raise OutcomeStatusTargetUnresolvableError(intended_outcome_revision_id)

    # Requirement 59.5 — withhold the status when the Projection Definition
    # cannot be resolved, naming the missing element. No source Record is read
    # for derivation on this path, so stored Records remain byte-equivalent.
    definition = registry.get(_PROJECTION_DEFINITION_NAME)
    if definition is None:
        return ExplanationUnavailableResponse(
            missing_element_kind="projection_definition",
            missing_element_identifier=(
                f"{_PROJECTION_DEFINITION_NAME}/{_PROJECTION_DEFINITION_VERSION}"
            ),
        )

    # Source identity accumulators (Requirement 59.1). Resource / Record
    # Identities and Revision Identities are tracked separately so the envelope
    # surfaces both lists. The target Intended Outcome Revision is the root
    # source on every path.
    source_resource_ids: list[UUID] = [UUID(revision_row.intended_outcome_id)]
    source_revision_ids: list[UUID] = [
        UUID(revision_row.intended_outcome_revision_id)
    ]

    status = _derive_status(
        connection,
        intended_outcome_resource_id=revision_row.intended_outcome_id,
        intended_outcome_revision_id=revision_row.intended_outcome_revision_id,
        source_resource_ids=source_resource_ids,
        source_revision_ids=source_revision_ids,
    )

    envelope = ProjectionEnvelope(
        definition=definition,
        source_resource_ids=tuple(source_resource_ids),
        source_revision_ids=tuple(source_revision_ids),
        applicable_temporal_boundary=_to_second_precision(at),
        generated_at=_to_second_precision(clock.now()),
    )

    return OutcomeStatusProjection(
        intended_outcome_revision_id=revision_row.intended_outcome_revision_id,
        projected_status=status,  # type: ignore[arg-type]
        envelope=envelope,
    )


def _derive_status(
    connection: Connection,
    *,
    intended_outcome_resource_id: str,
    intended_outcome_revision_id: str,
    source_resource_ids: list[UUID],
    source_revision_ids: list[UUID],
) -> str:
    """Derive the most-progressed status label (design steps 2–7).

    Walks the pipeline from least to most progressed, recording each consulted
    source identity onto the accumulators, then selects the most-progressed
    label observed. Falls back to ``"Provenance incomplete"`` when a
    more-progressed artifact exists but a required intermediate source link is
    unresolved (design step 7).
    """
    # Step 2 — the single Measurement Definition addressing O's Resource.
    definition_row = (
        MeasurementDefinitionService.get_definition_for_intended_outcome(
            connection,
            intended_outcome_resource_id=intended_outcome_resource_id,
        )
    )
    if definition_row is None:
        return _STATUS_UNMEASURED
    source_resource_ids.append(UUID(definition_row.measurement_definition_id))
    source_revision_ids.append(
        UUID(definition_row.measurement_definition_revision_id)
    )

    # Step 3 — Measurement Records citing any Revision of that Definition.
    measurement_record_ids = [
        row["measurement_record_id"]
        for row in connection.execute(
            text(
                "SELECT measurement_record_id "
                "FROM Measurement_Records "
                "WHERE target_measurement_definition_id "
                "= :measurement_definition_id "
                "ORDER BY recorded_at, measurement_record_id"
            ),
            {
                "measurement_definition_id": (
                    definition_row.measurement_definition_id
                )
            },
        )
        .mappings()
        .all()
    ]
    for record_id in measurement_record_ids:
        source_resource_ids.append(UUID(record_id))
    has_measurements = len(measurement_record_ids) >= 1

    # Step 4 — Observed Outcome Revisions addressing O.
    observed_rows = (
        connection.execute(
            text(
                "SELECT observed_outcome_revision_id, observed_outcome_id "
                "FROM Observed_Outcome_Revisions "
                "WHERE target_intended_outcome_revision_id "
                "= :intended_outcome_revision_id "
                "ORDER BY recorded_at, observed_outcome_revision_id"
            ),
            {"intended_outcome_revision_id": intended_outcome_revision_id},
        )
        .mappings()
        .all()
    )
    for row in observed_rows:
        source_resource_ids.append(UUID(row["observed_outcome_id"]))
        source_revision_ids.append(UUID(row["observed_outcome_revision_id"]))
    has_observed = len(observed_rows) >= 1

    # Step 5 — Success-Condition Assessments addressing O; the most-recent
    # Assessment's category supplies the success-condition label.
    assessment_rows = (
        connection.execute(
            text(
                "SELECT assessment_id, assessment_category, recorded_at "
                "FROM Success_Condition_Assessment_Records "
                "WHERE target_intended_outcome_revision_id "
                "= :intended_outcome_revision_id "
                "ORDER BY recorded_at DESC, assessment_id DESC"
            ),
            {"intended_outcome_revision_id": intended_outcome_revision_id},
        )
        .mappings()
        .all()
    )
    for row in assessment_rows:
        source_resource_ids.append(UUID(row["assessment_id"]))
    has_assessment = len(assessment_rows) >= 1
    most_recent_assessment_category: Optional[str] = (
        assessment_rows[0]["assessment_category"] if has_assessment else None
    )

    # Step 6 — the Outcome Review (if any) addressing O.
    review_row = (
        connection.execute(
            text(
                "SELECT outcome_review_id "
                "FROM Outcome_Review_Records "
                "WHERE target_intended_outcome_revision_id "
                "= :intended_outcome_revision_id"
            ),
            {"intended_outcome_revision_id": intended_outcome_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    if review_row is not None:
        source_resource_ids.append(UUID(review_row["outcome_review_id"]))
    has_review = review_row is not None

    # Step 7 — the most-progressed label observed, with the
    # "Provenance incomplete" fall-back when a required source link in the
    # chain is unresolved (e.g. a Review without a backing Assessment, an
    # Assessment without a backing Observed Outcome, or an Observed Outcome
    # without a backing Measurement Record).
    if has_review:
        if not has_assessment:
            return _STATUS_PROVENANCE_INCOMPLETE
        return _STATUS_REVIEWED
    if has_assessment:
        if not has_observed:
            return _STATUS_PROVENANCE_INCOMPLETE
        return _ASSESSMENT_CATEGORY_TO_STATUS[most_recent_assessment_category]
    if has_observed:
        if not has_measurements:
            return _STATUS_PROVENANCE_INCOMPLETE
        return _STATUS_OBSERVED
    if has_measurements:
        return _STATUS_MEASURED
    return _STATUS_MEASUREMENT_DEFINED
