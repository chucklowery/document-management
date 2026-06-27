"""Shared in-memory value objects for the fourth walking slice (Outcome_Service).

These DTOs cross module boundaries inside the modular monolith and are the
authoritative source for request/response shapes consumed by the
Outcome_Service modules (Measurement Definitions, Measurement Records,
Observed Outcomes, Success-Condition Assessments, and Outcome Reviews). The
definitions track ``.kiro/specs/fourth-walking-slice/design.md``
§"In-Memory Value Objects" verbatim for the reference (``*Ref``) objects, and
extend that section with the per-service ``Create*Result`` objects and the
``MeasurementDefinitionRow`` read-model the service public surfaces in design
§"Components and Interfaces — Outcome_Service.*" return. Adding a field here
without updating the design document is a spec violation.

Every model is a *frozen* Pydantic v2 :class:`BaseModel` so that once a service
has been handed a reference, the receiver can rely on its bytes not changing
while the in-flight transaction completes (mirroring the Slice 1 convention in
:mod:`walking_slice.models`, the Slice 2 convention in
:mod:`walking_slice.planning.models`, and the Slice 3 convention in
:mod:`walking_slice.execution.models`). ``extra="forbid"`` rejects unknown
attributes so typo'd field names fail loudly instead of silently dropping data.

Reuse contract (task 3.1, design §"In-Memory Value Objects" final paragraph and
AD-WS-32 / AD-WS-40 / AD-WS-41):
    ``AuthorityBasisRef`` is reused unchanged from :mod:`walking_slice.models`
        (it types the authority basis recorded on Success-Condition Assessment
        Records and Outcome Review Records — AD-WS-41).
    ``ProvenanceNode`` and ``GapDescriptor`` are reused unchanged from
        :mod:`walking_slice.models` (omission-aware provenance traversal).
    ``TargetRef`` is reused unchanged from :mod:`walking_slice.authorization`.
    ``ProjectionEnvelope`` is reused unchanged from
        :mod:`walking_slice.projection` (the outcome-status Projection wrapper).
    ``RequestContext`` is reused unchanged from
        :mod:`walking_slice.auth_middleware`.
    ``Clock`` is reused unchanged from :mod:`walking_slice.clock`.
    None of these Slice 1 / Slice 2 / Slice 3 types is redefined here.

Requirements satisfied (per task 3.1):
    43.2 — Every Slice 4 reference object carries the durable UUID(s) minted by
        the Identity_Service so receivers can resolve the referent without
        embedding business meaning; the seven Slice 4 identifier roles
        (Measurement Definition Resource/Revision, Measurement Record, Observed
        Outcome Resource/Revision, Success-Condition Assessment Record, Outcome
        Review Record) remain pairwise disjoint at the type level.
    44.2 — :class:`MeasurementDefinitionRef` / :class:`CreateMeasurementDefinitionResult`
        carry the Measurement Definition Resource + Revision identities and the
        target Intended Outcome Revision the Definition addresses.
    45.1, 46.1 — :class:`MeasurementRecordRef` / :class:`CreateMeasurementRecordResult`
        carry the Measurement Record identity, the target Measurement
        Definition Revision, the ``origin`` discriminator, and (for imported
        Records) the source-system authority designation, which is never
        defaulted to ``authoritative``.
    47.1 — :class:`ObservedOutcomeRef` / :class:`CreateObservedOutcomeResult`
        carry the Observed Outcome Resource + Revision identities, the
        ``outcome_kind = 'observed'`` discriminator, the addressed Intended
        Outcome Revision, and the predecessor-chain link (AD-WS-36).
    48.1 — :class:`SuccessConditionAssessmentRef` / :class:`CreateAssessmentResult`
        carry the Assessment Record identity, the target Intended Outcome
        Revision, the sourced Observed Outcome Revision, and the assessment
        category.
    49.1 — :class:`OutcomeReviewRef` / :class:`CreateOutcomeReviewResult` carry
        the Outcome Review Record identity, the target Intended Outcome
        Revision, and the review outcome / attribution stance / confidence
        enumerations.
"""

from __future__ import annotations

from typing import Literal, Optional, Tuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from walking_slice.models import AuthorityBasisRef


__all__ = [
    # Reference objects (design §"In-Memory Value Objects").
    "MeasurementDefinitionRef",
    "MeasurementRecordRef",
    "ObservedOutcomeRef",
    "SuccessConditionAssessmentRef",
    "OutcomeReviewRef",
    # Per-service result objects.
    "CreateMeasurementDefinitionResult",
    "CreateMeasurementRecordResult",
    "CreateObservedOutcomeResult",
    "CreateAssessmentResult",
    "CreateOutcomeReviewResult",
    # Read-model row objects.
    "MeasurementDefinitionRow",
    "ObservedOutcomeRevisionRow",
]


class _FrozenModel(BaseModel):
    """Common configuration for every Slice 4 Outcome_Service value object.

    ``frozen=True`` makes instances hashable and prevents field assignment;
    ``extra="forbid"`` rejects unknown attributes so call-sites that pass a
    typo'd field name fail loudly instead of silently dropping data. This
    mirrors the ``_FrozenModel`` convention established by Slice 1 in
    :mod:`walking_slice.models`, Slice 2 in
    :mod:`walking_slice.planning.models`, and Slice 3 in
    :mod:`walking_slice.execution.models`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Reference objects (design §"In-Memory Value Objects").
# ---------------------------------------------------------------------------


class MeasurementDefinitionRef(_FrozenModel):
    """Reference to a Measurement Definition Resource and its Revision.

    Resolved against the ``Measurement_Definitions`` /
    ``Measurement_Definition_Revisions`` tables. Carries the addressed target
    Intended Outcome Revision Identity so receivers can confirm the Definition
    addresses the Intended Outcome they hold without a second round-trip
    (design AD-WS-35 ``Addresses`` Relationship). At most one Measurement
    Definition Resource exists per target Intended Outcome Resource
    (Requirement 44.3), enforced by ``UNIQUE(target_intended_outcome_resource_id)``.
    """

    measurement_definition_id: UUID
    measurement_definition_revision_id: UUID
    target_intended_outcome_revision_id: UUID


class MeasurementRecordRef(_FrozenModel):
    """Reference to a Measurement Record and its provenance discriminators.

    Resolved against the ``Measurement_Records`` table. ``origin`` distinguishes
    a ``native`` measurement from an ``imported`` one (AD-WS-38). For imported
    Records, ``source_system_authority`` surfaces the external authority
    designation explicitly and is **never** defaulted to ``authoritative``
    (Requirement 46.7, Principle 5.27); for native Records it is ``None``.
    The full source-system attribute set is restricted per AD-WS-34 and is not
    carried on this reference.
    """

    measurement_record_id: UUID
    target_measurement_definition_revision_id: UUID
    origin: Literal["native", "imported"]
    source_system_authority: Optional[
        Literal["authoritative", "replica", "projection", "index", "federation"]
    ] = None


class ObservedOutcomeRef(_FrozenModel):
    """Reference to an Observed Outcome Resource and one of its Revisions.

    Resolved against the ``Observed_Outcomes`` / ``Observed_Outcome_Revisions``
    tables. ``outcome_kind`` is always ``'observed'`` (Requirement 47, the
    outcome-side counterpart to the Slice 2 ``'intended'`` discriminator).
    ``predecessor_revision_id`` is ``None`` on the initial Revision and equal
    to the immediately prior Revision Identity on later Revisions, forming the
    append-only predecessor chain (AD-WS-36).
    """

    observed_outcome_id: UUID
    observed_outcome_revision_id: UUID
    outcome_kind: Literal["observed"]
    target_intended_outcome_revision_id: UUID
    predecessor_revision_id: Optional[UUID] = None


class SuccessConditionAssessmentRef(_FrozenModel):
    """Reference to a Success-Condition Assessment Immutable Record.

    Resolved against the ``Success_Condition_Assessment_Records`` table. Carries
    the target Intended Outcome Revision the Assessment addresses and the
    sourced Observed Outcome Revision it cites (AD-WS-35). ``assessment_category``
    is drawn from the closed enumeration validated by the
    SuccessConditionAssessmentService (Requirement 48.3).
    """

    assessment_id: UUID
    target_intended_outcome_revision_id: UUID
    sourced_observed_outcome_revision_id: UUID
    assessment_category: Literal[
        "Satisfied", "Partially_Satisfied", "Not_Satisfied", "Unassessable"
    ]


class OutcomeReviewRef(_FrozenModel):
    """Reference to an Outcome Review Governance Decision Immutable Record.

    Resolved against the ``Outcome_Review_Records`` table. Carries the target
    Intended Outcome Revision and the three review enumerations
    (``review_outcome``, ``attribution_stance``, ``confidence``). At most one
    Outcome Review Record exists per target Intended Outcome Revision
    (Requirement 49.3), enforced by ``UNIQUE(target_intended_outcome_revision_id)``.
    The explicit ``attribution_stance`` re-asserts that Output is not Outcome
    from the outcome side (Requirement 54, Principle 5.21).
    """

    outcome_review_id: UUID
    target_intended_outcome_revision_id: UUID
    review_outcome: Literal[
        "Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"
    ]
    attribution_stance: Literal[
        "Asserted", "Partial", "Unattributed", "Contradicted"
    ]
    confidence: Literal["High", "Moderate", "Low"]


# ---------------------------------------------------------------------------
# Per-service result objects.
# ---------------------------------------------------------------------------
#
# Each ``Create*Result`` is the post-write shape returned by its service so
# callers (the HTTP layer, tests, the Provenance_Navigator, and the
# outcome-status Projection) can correlate the created Resource/Revision/Record
# with its ``Addresses`` / ``Cites`` Relationship row(s) and its consequential
# audit row in one round-trip. Identity values and timestamps are carried as
# ``str`` (matching the Slice 1–3 ``Create*Result`` convention in
# :mod:`walking_slice.knowledge`, :mod:`walking_slice.planning`, and
# :mod:`walking_slice.execution`); the ``*Ref`` reference objects above carry
# ``UUID`` because they cross the typed in-process boundary. ``recorded_at`` is
# the UTC ISO-8601 millisecond-precision timestamp shared byte-equivalent by
# the domain row(s), every Relationship row, and the consequential audit row
# (AD-WS-5). ``correlation_id`` is shared with the authorization evaluation row
# and the consequential audit row so audit consumers can join on a single value.


class CreateMeasurementDefinitionResult(_FrozenModel):
    """Result of ``MeasurementDefinitionService.create_measurement_definition``.

    Persisted in one transaction: a ``Measurement_Definitions`` Resource row,
    the initial immutable ``Measurement_Definition_Revisions`` row, one
    ``Addresses`` Relationship to the target Intended Outcome Revision
    (``semantic_role IS NULL``, AD-WS-35), and the consequential audit row
    (Requirements 44.1, 44.6, 57.1).
    """

    measurement_definition_id: str
    measurement_definition_revision_id: str
    target_intended_outcome_revision_id: str
    target_intended_outcome_resource_id: str
    measurand_description: str
    unit_of_measure: str
    observation_window: str
    cadence: str
    data_source: str
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    recorded_at: str
    correlation_id: str


class CreateMeasurementRecordResult(_FrozenModel):
    """Result of ``MeasurementRecordService.create_native_measurement`` and
    ``MeasurementRecordService.create_imported_measurement``.

    Persisted in one transaction: a ``Measurement_Records`` row, one ``Cites``
    Relationship to the target Measurement Definition Revision
    (``semantic_role = 'measurement_basis'``, AD-WS-35), and the consequential
    audit row (Requirements 45.1, 45.5, 46.1, 46.6, 57.1).

    ``origin`` is ``'native'`` for native measurements (all source-system
    attributes ``None``) and ``'imported'`` for imported measurements, which
    additionally surface the source-system attributes. ``source_system_authority``
    is never defaulted to ``authoritative`` (Requirement 46.7). ``observed_value``
    is carried as the normalized canonical Decimal string the service persists
    (≤ 6 fractional digits, Requirement 45.2). ``import_at`` equals
    ``recorded_at`` for imported Records and is ``None`` for native Records
    (Requirement 46.2).
    """

    measurement_record_id: str
    target_measurement_definition_revision_id: str
    origin: Literal["native", "imported"]
    observed_value: str
    observed_value_unit: str
    observation_time: str
    recording_party_id: str
    applicable_scope: str
    cites_relationship_id: str
    recorded_at: str
    correlation_id: str
    # Imported-only source-system attributes (all ``None`` for native Records).
    source_system_id: Optional[str] = None
    source_system_record_id: Optional[str] = None
    source_system_authority: Optional[
        Literal["authoritative", "replica", "projection", "index", "federation"]
    ] = None
    source_system_retrieval_time: Optional[str] = None
    import_at: Optional[str] = None


class CreateObservedOutcomeResult(_FrozenModel):
    """Result of ``ObservedOutcomeService.create_observed_outcome`` and
    ``ObservedOutcomeService.revise_observed_outcome``.

    Persisted in one transaction: on create, an ``Observed_Outcomes`` Resource
    row plus the initial ``Observed_Outcome_Revisions`` row; on revise, the
    next ``Observed_Outcome_Revisions`` row with ``predecessor_revision_id``
    set to the prior most-recent Revision (AD-WS-36). Also persists one
    ``Addresses`` Relationship to the target Intended Outcome Revision
    (``semantic_role IS NULL``), one ``Cites`` Relationship per cited
    Measurement Record (``semantic_role = 'observation_basis'``), and the
    consequential audit row (Requirements 47.1, 47.6, 57.1). Every Revision
    records ``outcome_kind = 'observed'`` (Requirement 47).
    """

    observed_outcome_id: str
    observed_outcome_revision_id: str
    outcome_kind: Literal["observed"]
    target_intended_outcome_revision_id: str
    predecessor_revision_id: Optional[str]
    assessment_summary: str
    cited_measurement_record_ids: Tuple[str, ...]
    authoring_party_id: str
    applicable_scope: str
    addresses_relationship_id: str
    cites_relationship_ids: Tuple[str, ...]
    recorded_at: str
    correlation_id: str


class CreateAssessmentResult(_FrozenModel):
    """Result of ``SuccessConditionAssessmentService.create_assessment``.

    Persisted in one transaction: a ``Success_Condition_Assessment_Records``
    row, one ``Addresses`` Relationship to the target Intended Outcome Revision
    (``semantic_role IS NULL``), one ``Cites`` Relationship to the sourced
    Observed Outcome Revision (``semantic_role = 'assessment_basis'``, AD-WS-35),
    and the consequential audit row (Requirements 48.1, 48.5, 57.1). The
    ``authority_basis`` is the validated :class:`AuthorityBasisRef` recorded on
    the Record (type in the AD-WS-10 set, AD-WS-41).
    """

    assessment_id: str
    target_intended_outcome_revision_id: str
    sourced_observed_outcome_revision_id: str
    assessment_category: Literal[
        "Satisfied", "Partially_Satisfied", "Not_Satisfied", "Unassessable"
    ]
    assessment_rationale: str
    assessing_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    addresses_relationship_id: str
    cites_relationship_id: str
    recorded_at: str
    correlation_id: str


class CreateOutcomeReviewResult(_FrozenModel):
    """Result of ``OutcomeReviewService.create_outcome_review``.

    Persisted in one transaction: an ``Outcome_Review_Records`` Governance
    Decision Immutable Record row, one ``Addresses`` Relationship to the target
    Intended Outcome Revision (``semantic_role IS NULL``), one ``Cites``
    Relationship per cited Success-Condition Assessment
    (``semantic_role = 'review_assessment'``), per cited Completion Record
    (``semantic_role = 'review_completion'``), and per cited produced
    Deliverable Revision (``semantic_role = 'review_deliverable'``), and the
    consequential audit row (Requirements 49.1, 49.6, 57.1). The explicit
    ``attribution_stance`` and ``attribution_evidence_reference`` re-assert that
    Output is not Outcome (Requirement 54). The ``authority_basis`` type is in
    the AD-WS-10 set (AD-WS-41).
    """

    outcome_review_id: str
    target_intended_outcome_revision_id: str
    review_outcome: Literal[
        "Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"
    ]
    attribution_stance: Literal[
        "Asserted", "Partial", "Unattributed", "Contradicted"
    ]
    confidence: Literal["High", "Moderate", "Low"]
    review_rationale: str
    attribution_evidence_reference: str
    cited_assessment_ids: Tuple[str, ...]
    cited_completion_ids: Tuple[str, ...]
    cited_produced_deliverable_revision_ids: Tuple[str, ...]
    reviewing_party_id: str
    authority_basis: AuthorityBasisRef
    applicable_scope: str
    addresses_relationship_id: str
    cites_assessment_relationship_ids: Tuple[str, ...]
    cites_completion_relationship_ids: Tuple[str, ...]
    cites_deliverable_relationship_ids: Tuple[str, ...]
    recorded_at: str
    correlation_id: str


# ---------------------------------------------------------------------------
# Read-model row objects.
# ---------------------------------------------------------------------------


class MeasurementDefinitionRow(_FrozenModel):
    """Read-model row returned by
    ``MeasurementDefinitionService.get_definition_for_intended_outcome``.

    Mirrors the persisted ``Measurement_Definitions`` Resource joined to its
    initial ``Measurement_Definition_Revisions`` row. Used by the uniqueness
    pre-check (Requirement 44.3) and by the Observed Outcome anchoring rule
    (Requirement 47.2/47.4, AD-WS-40) to resolve the single Measurement
    Definition Resource that addresses a given target Intended Outcome Resource.
    Identity values and the timestamp are carried as ``str`` to match the
    persisted column form.
    """

    measurement_definition_id: str
    measurement_definition_revision_id: str
    target_intended_outcome_resource_id: str
    target_intended_outcome_revision_id: str
    measurand_description: str
    unit_of_measure: str
    observation_window: str
    cadence: str
    data_source: str
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


class ObservedOutcomeRevisionRow(_FrozenModel):
    """Read-model row returned by
    ``ObservedOutcomeService.get_observed_outcome_revision``.

    Mirrors a persisted ``Observed_Outcome_Revisions`` row. Backs the
    Success-Condition Assessment sourcing rule (Requirement 48.3, AD-WS-40):
    the :class:`~walking_slice.outcome.success_condition_assessments.SuccessConditionAssessmentService`
    must (a) confirm the named sourced Observed Outcome Revision resolves,
    (b) recover its parent Observed Outcome **Resource** Identity so the
    ``Cites`` Relationship and the persisted Record can carry it, and
    (c) recover the Revision's ``Addresses`` target Intended Outcome Revision
    Identity so it can verify that target equals the named target Intended
    Outcome Revision. Identity values and the timestamp are carried as ``str``
    to match the persisted column form.
    """

    observed_outcome_revision_id: str
    observed_outcome_id: str
    outcome_kind: str
    target_intended_outcome_resource_id: str
    target_intended_outcome_revision_id: str
    assessment_summary: str
    predecessor_revision_id: Optional[str]
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str
