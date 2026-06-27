"""Outcome Measurement Provenance Chain traversals (Fourth Walking Slice task 10.1).

Design reference: ``.kiro/specs/fourth-walking-slice/design.md``
§"Provenance_Navigator (extended)" — two additive ``navigate_*``
functions registered with the existing ``walking_slice.provenance``
module:

1. ``navigate_outcome_review(outcome_review_id, party, at)`` — walks the
   Outcome Measurement Provenance Chain rooted at an Outcome Review
   Record: Outcome Review → Success-Condition Assessment(s) → Observed
   Outcome Revision → Measurement Record(s) → Measurement Definition
   Revision → Intended Outcome Revision → Objective → Slice 1 Decision
   (delegating to the existing :meth:`ProvenanceNavigator.navigate_decision`
   for the Decision → Recommendation → Finding → Region → Document
   Revision tail). In parallel, walks Outcome Review → Cites Completion
   Record(s) → (delegating to the existing
   :meth:`ProvenanceNavigator.navigate_completion`) → Slice 3 Execution
   Provenance Chain → produced Deliverable Revision(s), and Outcome
   Review → Cites produced Deliverable Revision(s) directly.
2. ``navigate_outcome_node(node_kind, node_id, party, at)`` — short-form
   traversal beginning at any Success-Condition Assessment, Observed
   Outcome Revision, Measurement Record, or Measurement Definition
   Revision; returns the chain rooted lower (Requirement 55.1).

Both are **strictly additive** (Requirement 60): neither modifies the
Slice 1 :meth:`navigate_decision`, the Slice 2
:meth:`navigate_plan_approval`, nor the Slice 3
:meth:`navigate_completion`. They are attached to
:class:`~walking_slice.provenance.ProvenanceNavigator` at import time by
:func:`register_outcome_navigation`, mirroring the attachment pattern the
Slice 3 ``navigate_completion`` task uses so the diff against the
original class body remains empty.

Chain behaviour (design §"Chain behavior"):
    - When a node is restricted to the requesting Party, it is replaced
      with a redaction marker ``{kind, redacted: true}`` — the existing
      :class:`~walking_slice.provenance.RedactedNode` value object
      (Requirement 55.3, 58.2).
    - Unresolved / stale / unavailable links surface
      :class:`~walking_slice.provenance.ChainGapDescriptor` instances
      ``{stage, category, next_reachable_node?}`` with
      ``category ∈ {unavailable, restricted, stale, unresolved}``
      (Requirements 51.3, 55.4) via the existing
      :meth:`ProvenanceNavigator._collect_gap_descriptors_for_subject`
      helper.
    - Region Occurrence span fields digest-matching the recorded content
      digest are delivered by the delegated ``navigate_decision`` tail
      (Requirement 55.2), unchanged.
    - Measurement Record nodes carry the origin indicator and, for
      imported Records visible to the Party, the source-system
      identifier and authority designation (Requirement 55.8).
    - Every ``navigate_*`` function is read-only over append-only tables,
      so repeated invocations for the same ``(node_id, party_id, at)``
      return byte-equivalent trees (idempotent retrieval, Requirements
      51.4, 55.5). Every list is ordered ``(recorded_at ASC,
      relationship_id ASC)`` / ``(recorded_at ASC, primary_key ASC)``
      with a deterministic tiebreaker.

Requirements satisfied (per task 10.1): 51.1, 51.2, 51.3, 51.4, 55.1,
55.2, 55.4, 55.5, 55.8.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.authorization import TargetRef
from walking_slice.provenance import (
    ChainGapDescriptor,
    DecisionProvenanceChain,
    DecisionUnresolvableError,
    CompletionUnresolvableError,
    DeliverableRevisionNode,
    ExecutionProvenanceTree,
    ProvenanceNavigator,
    RedactedNode,
)


__all__ = [
    "OutcomeReviewNode",
    "SuccessConditionAssessmentNode",
    "ObservedOutcomeRevisionNode",
    "MeasurementRecordNode",
    "MeasurementDefinitionRevisionNode",
    "IntendedOutcomeRevisionNode",
    "MeasurementChain",
    "AssessmentObservationChain",
    "OutcomeCompletionChain",
    "OutcomeProvenanceTree",
    "OutcomeReviewUnresolvableError",
    "OutcomeNodeUnresolvableError",
    "navigate_outcome_review",
    "navigate_outcome_node",
    "register_outcome_navigation",
]


# ---------------------------------------------------------------------------
# Authorization-action and node-kind constants.
#
# Each ``view.<resource_kind>`` action maps to the ``view`` authority via
# the prefix fallback in :mod:`walking_slice.authorization`, so no new
# authorization mapping is required (AD-WS-33 added only the four write
# authorities; the ``view.*`` prefix fallback already covers every node
# kind). The node-kind constants are emitted on :class:`RedactedNode`
# markers and on the node dataclasses below, matching the Slice 4
# ``resource_kind`` enumeration (AD-WS-37).
# ---------------------------------------------------------------------------


_NODE_KIND_OUTCOME_REVIEW: Final[str] = "outcome_review_record"
_NODE_KIND_ASSESSMENT: Final[str] = "success_condition_assessment_record"
_NODE_KIND_OBSERVED_OUTCOME_REVISION: Final[str] = "observed_outcome_revision"
_NODE_KIND_MEASUREMENT_RECORD: Final[str] = "measurement_record"
_NODE_KIND_MEASUREMENT_DEFINITION_REVISION: Final[str] = (
    "measurement_definition_revision"
)
_NODE_KIND_INTENDED_OUTCOME_REVISION: Final[str] = "intended_outcome_revision"
_NODE_KIND_DELIVERABLE_REVISION: Final[str] = "deliverable_revision"

_ACTION_VIEW_OUTCOME_REVIEW: Final[str] = "view.outcome_review_record"
_ACTION_VIEW_ASSESSMENT: Final[str] = "view.success_condition_assessment_record"
_ACTION_VIEW_OBSERVED_OUTCOME_REVISION: Final[str] = (
    "view.observed_outcome_revision"
)
_ACTION_VIEW_MEASUREMENT_RECORD: Final[str] = "view.measurement_record"
_ACTION_VIEW_MEASUREMENT_DEFINITION_REVISION: Final[str] = (
    "view.measurement_definition_revision"
)
_ACTION_VIEW_INTENDED_OUTCOME_REVISION: Final[str] = (
    "view.intended_outcome_revision"
)
_ACTION_VIEW_DELIVERABLE_REVISION: Final[str] = "view.deliverable_revision"

# ``Relationships`` semantic roles written by the Slice 4 write services
# (AD-WS-35).
_RELATIONSHIP_TYPE_CITES: Final[str] = "Cites"
_SEMANTIC_ROLE_REVIEW_ASSESSMENT: Final[str] = "review_assessment"
_SEMANTIC_ROLE_REVIEW_COMPLETION: Final[str] = "review_completion"
_SEMANTIC_ROLE_REVIEW_DELIVERABLE: Final[str] = "review_deliverable"
_SEMANTIC_ROLE_OBSERVATION_BASIS: Final[str] = "observation_basis"

# Recognized short-form entry-point node kinds for
# :func:`navigate_outcome_node` (design item 2; Requirement 55.1).
_SHORT_FORM_NODE_KINDS: Final[frozenset[str]] = frozenset(
    {
        _NODE_KIND_ASSESSMENT,
        _NODE_KIND_OBSERVED_OUTCOME_REVISION,
        _NODE_KIND_MEASUREMENT_RECORD,
        _NODE_KIND_MEASUREMENT_DEFINITION_REVISION,
    }
)


# ---------------------------------------------------------------------------
# Node dataclasses.
#
# Each frozen dataclass mirrors the persisted row's columns so the tree
# carries every field a caller may need without a second round-trip.
# Frozen dataclasses give structural equality (``==``) for the
# byte-equivalent idempotence check (Requirements 51.4, 55.5).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutcomeReviewNode:
    """Serialized ``Outcome_Review_Records`` row at the head of the tree.

    The Outcome Review is an Immutable Record (AD-WS-36) so the row, once
    inserted, never changes; the node has no Revision concept.
    """

    outcome_review_id: str
    target_intended_outcome_resource_id: str
    target_intended_outcome_revision_id: str
    review_outcome: str
    attribution_stance: str
    confidence: str
    review_rationale: str
    attribution_evidence_reference: str
    reviewing_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class SuccessConditionAssessmentNode:
    """Serialized ``Success_Condition_Assessment_Records`` row.

    Record-grain (no Revision concept). ``sourced_observed_outcome_revision_id``
    is the link the chain walks down to the Observed Outcome Revision.
    """

    assessment_id: str
    target_intended_outcome_resource_id: str
    target_intended_outcome_revision_id: str
    sourced_observed_outcome_id: str
    sourced_observed_outcome_revision_id: str
    assessment_category: str
    assessment_rationale: str
    assessing_party_id: str
    authority_basis_type: str
    authority_basis_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class ObservedOutcomeRevisionNode:
    """Serialized ``Observed_Outcome_Revisions`` row.

    Revision-grain. ``outcome_kind`` is always ``'observed'`` (schema
    CHECK). ``predecessor_revision_id`` is the AD-WS-36 chain link
    (``None`` on the initial Revision).
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


@dataclass(frozen=True)
class MeasurementRecordNode:
    """Serialized ``Measurement_Records`` row.

    Record-grain. Carries the ``origin`` indicator (``∈ {native,
    imported}``) on every node and, for imported Records, the
    source-system identifier, source-system record identifier, authority
    designation, retrieval time, and import time (Requirement 55.8). The
    node is only constructed for Records the Party may view; a restricted
    Record is replaced by a :class:`RedactedNode` (Requirement 58.2 /
    AD-WS-34), so the source-system attributes never leak.
    """

    measurement_record_id: str
    target_measurement_definition_id: str
    target_measurement_definition_revision_id: str
    origin: str
    observed_value: str
    observed_value_unit: str
    observation_time: str
    source_system_id: Optional[str]
    source_system_record_id: Optional[str]
    source_system_authority: Optional[str]
    source_system_retrieval_at: Optional[str]
    import_at: Optional[str]
    recording_party_id: str
    applicable_scope: str
    recorded_at: str


@dataclass(frozen=True)
class MeasurementDefinitionRevisionNode:
    """Serialized ``Measurement_Definition_Revisions`` row.

    Revision-grain. ``target_intended_outcome_revision_id`` is the link
    the chain walks up to the Intended Outcome Revision before delegating
    the Objective → Slice 1 Decision tail to ``navigate_decision``.
    """

    measurement_definition_revision_id: str
    measurement_definition_id: str
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


@dataclass(frozen=True)
class IntendedOutcomeRevisionNode:
    """Serialized ``Intended_Outcome_Revisions`` row (Slice 2, read-only).

    ``target_objective_id`` is the bridge into the Objective → Slice 1
    Decision tail walked by the delegated ``navigate_decision``.
    """

    intended_outcome_revision_id: str
    intended_outcome_id: str
    parent_revision_id: Optional[str]
    outcome_kind: str
    target_objective_id: str
    success_condition: str
    observation_window: Optional[str]
    attribution_assumption: Optional[str]
    authoring_party_id: str
    applicable_scope: str
    recorded_at: str


# ---------------------------------------------------------------------------
# Chain-grouping dataclasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementChain:
    """One Measurement Record with its Measurement Definition Revision.

    The Measurement Record may be a :class:`RedactedNode` when the Party
    lacks ``view.measurement_record`` authority; in that case the
    downstream Measurement Definition Revision is not surfaced
    (``None``) so a redacted Record does not leak its definition link
    (cascade by parent restriction).
    """

    measurement_record: "MeasurementRecordNode | RedactedNode"
    measurement_definition_revision: (
        "MeasurementDefinitionRevisionNode | RedactedNode | None"
    )


@dataclass(frozen=True)
class AssessmentObservationChain:
    """One Success-Condition Assessment with its Observed Outcome Revision.

    The Assessment may be a :class:`RedactedNode`; in that case the
    Observed Outcome Revision and Measurement chains are not surfaced
    (cascade by parent restriction). Even when the Assessment is visible,
    the Observed Outcome Revision may be redacted independently
    (restrictions cascade by record, not by tree branch).
    """

    assessment: "SuccessConditionAssessmentNode | RedactedNode"
    observed_outcome_revision: (
        "ObservedOutcomeRevisionNode | RedactedNode | None"
    )
    measurement_chains: tuple


@dataclass(frozen=True)
class OutcomeCompletionChain:
    """One cited Completion Record with its delegated Execution chain.

    ``execution_tree`` is the :class:`ExecutionProvenanceTree` produced by
    delegating to :meth:`ProvenanceNavigator.navigate_completion`, or
    ``None`` when the Completion is unresolved or restricted (the two
    cases are indistinguishable per Requirement 35.7 / AD-WS-9 rule 3).
    """

    completion_id: str
    execution_tree: Optional[ExecutionProvenanceTree]


@dataclass(frozen=True)
class OutcomeProvenanceTree:
    """The Outcome Measurement Provenance Chain returned to the Party.

    For :func:`navigate_outcome_review` the tree is rooted at the Outcome
    Review (``outcome_review`` non-``None``). For
    :func:`navigate_outcome_node` short-form traversals the tree is rooted
    lower (``outcome_review`` is ``None`` and only the sub-chains reachable
    from the entry node are populated).

    Attributes:
        outcome_review: The :class:`OutcomeReviewNode` head, or ``None``
            for short-form traversals.
        assessment_chains: One :class:`AssessmentObservationChain` per
            cited Success-Condition Assessment, ordered by the citing
            Relationship's ``(recorded_at ASC, relationship_id ASC)``.
        completion_chains: One :class:`OutcomeCompletionChain` per cited
            Completion Record (the parallel Slice 3 leg).
        cited_deliverable_revisions: One produced Deliverable Revision
            node (or :class:`RedactedNode`) per cited Deliverable
            Revision Relationship.
        intended_outcome_revision: The addressed Intended Outcome Revision
            node, a :class:`RedactedNode`, or ``None`` when unresolved.
        decision_chain: The Slice 1 :class:`DecisionProvenanceChain`
            produced by delegating to ``navigate_decision`` via the
            Objective's ``target_decision_id``, or ``None`` when the
            Decision is unresolved or restricted.
        gap_descriptors: Tuple of :class:`ChainGapDescriptor` for
            unresolved/stale/unavailable Omission Entries on the root
            node's Provenance Manifest.
        requested_node_kind: The node kind the caller asked for.
        requested_node_id: The node Identity the caller asked for.
    """

    outcome_review: Optional[OutcomeReviewNode]
    assessment_chains: tuple
    completion_chains: tuple
    cited_deliverable_revisions: tuple
    intended_outcome_revision: (
        "IntendedOutcomeRevisionNode | RedactedNode | None"
    )
    decision_chain: Optional[DecisionProvenanceChain]
    gap_descriptors: tuple
    requested_node_kind: str
    requested_node_id: str


# ---------------------------------------------------------------------------
# Head-node indistinguishability exceptions.
# ---------------------------------------------------------------------------


class OutcomeReviewUnresolvableError(Exception):
    """The requested Outcome Review Identity does not resolve.

    Raised by :func:`navigate_outcome_review` when ``outcome_review_id``
    does not match any ``Outcome_Review_Records`` row, or when the
    requesting Party lacks ``view.outcome_review_record`` authority on the
    resolved Record. The same exception is raised for both cases so the
    response form is indistinguishable (Requirements 55.3 / 55.4 /
    AD-WS-9). The message names only the unresolvable reference; no
    neighbouring identifiers are disclosed.
    """

    def __init__(self, outcome_review_id: str) -> None:
        super().__init__(
            f"Outcome Review identity {outcome_review_id!r} does not resolve "
            f"to an Outcome Review Record visible to the requesting Party."
        )
        self.outcome_review_id = outcome_review_id


class OutcomeNodeUnresolvableError(Exception):
    """The requested short-form entry node does not resolve.

    Raised by :func:`navigate_outcome_node` when ``node_kind`` is not a
    recognized short-form entry kind, when ``node_id`` does not resolve,
    or when the requesting Party lacks view authority on the resolved
    node. The same exception is raised for the unresolved and restricted
    cases so the response form is indistinguishable (Requirement 55.4 /
    AD-WS-9).
    """

    def __init__(self, node_kind: str, node_id: str) -> None:
        super().__init__(
            f"Outcome node ({node_kind!r}, {node_id!r}) does not resolve to a "
            f"node visible to the requesting Party."
        )
        self.node_kind = node_kind
        self.node_id = node_id


# ---------------------------------------------------------------------------
# Row-loading helpers.
#
# Each helper consults exactly one append-only table and returns ``None``
# (or an empty list) when no row matches. Every Slice 4 table is
# insert-only (AD-WS-36), so idempotence (Requirements 51.4, 55.5) is
# preserved by construction. Citation lookups read the immutable
# ``Relationships`` rows written by the Slice 4 write services and order
# by ``(recorded_at ASC, relationship_id ASC)`` for byte-equivalence.
# ---------------------------------------------------------------------------


def _load_outcome_review_row(
    connection: Connection, outcome_review_id: str
) -> Optional[dict]:
    """Load an ``Outcome_Review_Records`` row by Identity (``None`` if absent)."""
    row = (
        connection.execute(
            text(
                """
                SELECT outcome_review_id, target_intended_outcome_resource_id,
                       target_intended_outcome_revision_id, review_outcome,
                       attribution_stance, confidence, review_rationale,
                       attribution_evidence_reference, reviewing_party_id,
                       authority_basis_type, authority_basis_id,
                       applicable_scope, recorded_at
                  FROM Outcome_Review_Records
                 WHERE outcome_review_id = :outcome_review_id
                """
            ),
            {"outcome_review_id": outcome_review_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_assessment_row(
    connection: Connection, assessment_id: str
) -> Optional[dict]:
    """Load a ``Success_Condition_Assessment_Records`` row by Identity."""
    row = (
        connection.execute(
            text(
                """
                SELECT assessment_id, target_intended_outcome_resource_id,
                       target_intended_outcome_revision_id,
                       sourced_observed_outcome_id,
                       sourced_observed_outcome_revision_id,
                       assessment_category, assessment_rationale,
                       assessing_party_id, authority_basis_type,
                       authority_basis_id, applicable_scope, recorded_at
                  FROM Success_Condition_Assessment_Records
                 WHERE assessment_id = :assessment_id
                """
            ),
            {"assessment_id": assessment_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_observed_outcome_revision_row(
    connection: Connection, observed_outcome_revision_id: str
) -> Optional[dict]:
    """Load an ``Observed_Outcome_Revisions`` row by Revision Identity."""
    row = (
        connection.execute(
            text(
                """
                SELECT observed_outcome_revision_id, observed_outcome_id,
                       outcome_kind, target_intended_outcome_resource_id,
                       target_intended_outcome_revision_id, assessment_summary,
                       predecessor_revision_id, authoring_party_id,
                       applicable_scope, recorded_at
                  FROM Observed_Outcome_Revisions
                 WHERE observed_outcome_revision_id
                       = :observed_outcome_revision_id
                """
            ),
            {"observed_outcome_revision_id": observed_outcome_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_measurement_record_row(
    connection: Connection, measurement_record_id: str
) -> Optional[dict]:
    """Load a ``Measurement_Records`` row by Identity."""
    row = (
        connection.execute(
            text(
                """
                SELECT measurement_record_id, target_measurement_definition_id,
                       target_measurement_definition_revision_id, origin,
                       observed_value, observed_value_unit, observation_time,
                       source_system_id, source_system_record_id,
                       source_system_authority, source_system_retrieval_at,
                       import_at, recording_party_id, applicable_scope,
                       recorded_at
                  FROM Measurement_Records
                 WHERE measurement_record_id = :measurement_record_id
                """
            ),
            {"measurement_record_id": measurement_record_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_measurement_definition_revision_row(
    connection: Connection, measurement_definition_revision_id: str
) -> Optional[dict]:
    """Load a ``Measurement_Definition_Revisions`` row by Revision Identity."""
    row = (
        connection.execute(
            text(
                """
                SELECT measurement_definition_revision_id,
                       measurement_definition_id,
                       target_intended_outcome_resource_id,
                       target_intended_outcome_revision_id,
                       measurand_description, unit_of_measure,
                       observation_window, cadence, data_source,
                       authoring_party_id, applicable_scope, recorded_at
                  FROM Measurement_Definition_Revisions
                 WHERE measurement_definition_revision_id
                       = :measurement_definition_revision_id
                """
            ),
            {
                "measurement_definition_revision_id": (
                    measurement_definition_revision_id
                )
            },
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_intended_outcome_revision_row(
    connection: Connection, intended_outcome_revision_id: str
) -> Optional[dict]:
    """Load an ``Intended_Outcome_Revisions`` row by Revision Identity.

    Read-only over the Slice 2 table (Requirement 60.1 — no write path).
    Mirrors the read shape of
    :meth:`walking_slice.planning.intended_outcomes.IntendedOutcomeService.get_revision`
    but reads the table directly, matching the navigator's established
    convention (the Slice 2 ``navigate_plan_approval`` reads
    ``Objective_Revisions`` directly rather than through a service).
    """
    row = (
        connection.execute(
            text(
                """
                SELECT intended_outcome_revision_id, intended_outcome_id,
                       parent_revision_id, outcome_kind, target_objective_id,
                       success_condition, observation_window,
                       attribution_assumption, authoring_party_id,
                       applicable_scope, recorded_at
                  FROM Intended_Outcome_Revisions
                 WHERE intended_outcome_revision_id
                       = :intended_outcome_revision_id
                """
            ),
            {"intended_outcome_revision_id": intended_outcome_revision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_cited_targets(
    connection: Connection,
    *,
    source_kind: str,
    source_id: str,
    source_revision_id: Optional[str],
    semantic_role: str,
) -> Sequence[dict]:
    """Load ``Cites`` Relationship targets for one source endpoint and role.

    Returns the ``target_id`` / ``target_revision_id`` pairs of every
    ``Cites`` Relationship whose source endpoint is
    ``(source_kind, source_id, source_revision_id)`` and whose
    ``semantic_role`` matches, ordered ``(recorded_at ASC,
    relationship_id ASC)`` for byte-equivalent idempotence. The
    ``Relationships`` table is immutable (Slice 1 invariant), so repeated
    invocations return identical tuples.
    """
    if source_revision_id is not None:
        sql = """
            SELECT target_id, target_revision_id, relationship_id, recorded_at
              FROM Relationships
             WHERE source_kind = :source_kind
               AND source_id = :source_id
               AND source_revision_id = :source_revision_id
               AND relationship_type = :relationship_type
               AND semantic_role = :semantic_role
             ORDER BY recorded_at ASC, relationship_id ASC
        """
        params = {
            "source_kind": source_kind,
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "relationship_type": _RELATIONSHIP_TYPE_CITES,
            "semantic_role": semantic_role,
        }
    else:
        sql = """
            SELECT target_id, target_revision_id, relationship_id, recorded_at
              FROM Relationships
             WHERE source_kind = :source_kind
               AND source_id = :source_id
               AND source_revision_id IS NULL
               AND relationship_type = :relationship_type
               AND semantic_role = :semantic_role
             ORDER BY recorded_at ASC, relationship_id ASC
        """
        params = {
            "source_kind": source_kind,
            "source_id": source_id,
            "relationship_type": _RELATIONSHIP_TYPE_CITES,
            "semantic_role": semantic_role,
        }
    rows = connection.execute(text(sql), params).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Sub-chain builders.
#
# Each builder takes the navigator instance (``self``) so it can issue the
# ``view.<kind>`` authorization checks via :meth:`_is_permitted`, returning
# either a populated node or a :class:`RedactedNode`. The builders are
# reused by both :func:`navigate_outcome_review` (full traversal) and
# :func:`navigate_outcome_node` (short-form traversals rooted lower), so a
# single source of truth backs every entry point (Requirement 55.1 / 55.5).
# ---------------------------------------------------------------------------


def _build_measurement_chain(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    measurement_record_id: str,
    party_id: str,
    at: datetime,
) -> MeasurementChain:
    """Build one :class:`MeasurementChain` rooted at a Measurement Record.

    Evaluates ``view.measurement_record`` authority. When restricted,
    emits a :class:`RedactedNode` and does not surface the downstream
    Measurement Definition Revision (cascade by parent restriction so the
    source-system attributes and the definition link never leak —
    Requirement 58.2 / AD-WS-34). When visible, builds the
    :class:`MeasurementRecordNode` (carrying the origin indicator and, for
    imported Records, the source-system identifier and authority
    designation — Requirement 55.8) and walks to the Measurement
    Definition Revision named on the Record, authorized independently.
    """
    mr_row = _load_measurement_record_row(connection, measurement_record_id)
    if mr_row is None:
        return MeasurementChain(
            measurement_record=RedactedNode(kind=_NODE_KIND_MEASUREMENT_RECORD),
            measurement_definition_revision=None,
        )

    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_MEASUREMENT_RECORD,
        target=TargetRef(
            kind=_NODE_KIND_MEASUREMENT_RECORD,
            id=measurement_record_id,
            revision_id=None,
            scope=mr_row["applicable_scope"],
        ),
        at=at,
    ):
        return MeasurementChain(
            measurement_record=RedactedNode(kind=_NODE_KIND_MEASUREMENT_RECORD),
            measurement_definition_revision=None,
        )

    measurement_record = MeasurementRecordNode(
        measurement_record_id=mr_row["measurement_record_id"],
        target_measurement_definition_id=mr_row[
            "target_measurement_definition_id"
        ],
        target_measurement_definition_revision_id=mr_row[
            "target_measurement_definition_revision_id"
        ],
        origin=mr_row["origin"],
        observed_value=mr_row["observed_value"],
        observed_value_unit=mr_row["observed_value_unit"],
        observation_time=mr_row["observation_time"],
        source_system_id=mr_row["source_system_id"],
        source_system_record_id=mr_row["source_system_record_id"],
        source_system_authority=mr_row["source_system_authority"],
        source_system_retrieval_at=mr_row["source_system_retrieval_at"],
        import_at=mr_row["import_at"],
        recording_party_id=mr_row["recording_party_id"],
        applicable_scope=mr_row["applicable_scope"],
        recorded_at=mr_row["recorded_at"],
    )

    definition_node = _build_measurement_definition_revision_node(
        self,
        connection,
        measurement_definition_revision_id=mr_row[
            "target_measurement_definition_revision_id"
        ],
        party_id=party_id,
        at=at,
    )
    return MeasurementChain(
        measurement_record=measurement_record,
        measurement_definition_revision=definition_node,
    )


def _build_measurement_definition_revision_node(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    measurement_definition_revision_id: str,
    party_id: str,
    at: datetime,
) -> "MeasurementDefinitionRevisionNode | RedactedNode":
    """Build a Measurement Definition Revision node (or redaction marker)."""
    mdr_row = _load_measurement_definition_revision_row(
        connection, measurement_definition_revision_id
    )
    if mdr_row is None:
        return RedactedNode(kind=_NODE_KIND_MEASUREMENT_DEFINITION_REVISION)
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_MEASUREMENT_DEFINITION_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_MEASUREMENT_DEFINITION_REVISION,
            id=mdr_row["measurement_definition_id"],
            revision_id=mdr_row["measurement_definition_revision_id"],
            scope=mdr_row["applicable_scope"],
        ),
        at=at,
    ):
        return RedactedNode(kind=_NODE_KIND_MEASUREMENT_DEFINITION_REVISION)
    return MeasurementDefinitionRevisionNode(
        measurement_definition_revision_id=mdr_row[
            "measurement_definition_revision_id"
        ],
        measurement_definition_id=mdr_row["measurement_definition_id"],
        target_intended_outcome_resource_id=mdr_row[
            "target_intended_outcome_resource_id"
        ],
        target_intended_outcome_revision_id=mdr_row[
            "target_intended_outcome_revision_id"
        ],
        measurand_description=mdr_row["measurand_description"],
        unit_of_measure=mdr_row["unit_of_measure"],
        observation_window=mdr_row["observation_window"],
        cadence=mdr_row["cadence"],
        data_source=mdr_row["data_source"],
        authoring_party_id=mdr_row["authoring_party_id"],
        applicable_scope=mdr_row["applicable_scope"],
        recorded_at=mdr_row["recorded_at"],
    )


def _build_observed_outcome_subtree(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    observed_outcome_revision_id: str,
    party_id: str,
    at: datetime,
) -> tuple["ObservedOutcomeRevisionNode | RedactedNode", tuple]:
    """Build the Observed Outcome Revision node and its Measurement chains.

    Returns ``(observed_outcome_node, measurement_chains)``. When the
    Observed Outcome Revision is restricted, the node is a
    :class:`RedactedNode` and ``measurement_chains`` is empty (cascade by
    parent restriction). When visible, walks every ``Cites`` /
    ``observation_basis`` Relationship to the cited Measurement Records,
    building one :class:`MeasurementChain` each.
    """
    oo_row = _load_observed_outcome_revision_row(
        connection, observed_outcome_revision_id
    )
    if oo_row is None:
        return (
            RedactedNode(kind=_NODE_KIND_OBSERVED_OUTCOME_REVISION),
            (),
        )
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_OBSERVED_OUTCOME_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_OBSERVED_OUTCOME_REVISION,
            id=oo_row["observed_outcome_id"],
            revision_id=oo_row["observed_outcome_revision_id"],
            scope=oo_row["applicable_scope"],
        ),
        at=at,
    ):
        return (
            RedactedNode(kind=_NODE_KIND_OBSERVED_OUTCOME_REVISION),
            (),
        )

    observed_outcome_node = ObservedOutcomeRevisionNode(
        observed_outcome_revision_id=oo_row["observed_outcome_revision_id"],
        observed_outcome_id=oo_row["observed_outcome_id"],
        outcome_kind=oo_row["outcome_kind"],
        target_intended_outcome_resource_id=oo_row[
            "target_intended_outcome_resource_id"
        ],
        target_intended_outcome_revision_id=oo_row[
            "target_intended_outcome_revision_id"
        ],
        assessment_summary=oo_row["assessment_summary"],
        predecessor_revision_id=oo_row["predecessor_revision_id"],
        authoring_party_id=oo_row["authoring_party_id"],
        applicable_scope=oo_row["applicable_scope"],
        recorded_at=oo_row["recorded_at"],
    )

    measurement_chains: list[MeasurementChain] = []
    for cited in _load_cited_targets(
        connection,
        source_kind=_NODE_KIND_OBSERVED_OUTCOME_REVISION,
        source_id=oo_row["observed_outcome_id"],
        source_revision_id=oo_row["observed_outcome_revision_id"],
        semantic_role=_SEMANTIC_ROLE_OBSERVATION_BASIS,
    ):
        measurement_chains.append(
            _build_measurement_chain(
                self,
                connection,
                measurement_record_id=cited["target_id"],
                party_id=party_id,
                at=at,
            )
        )
    return observed_outcome_node, tuple(measurement_chains)


def _build_assessment_chain(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    assessment_id: str,
    party_id: str,
    at: datetime,
) -> AssessmentObservationChain:
    """Build one :class:`AssessmentObservationChain` rooted at an Assessment.

    Evaluates ``view.success_condition_assessment_record`` authority. When
    restricted, emits a :class:`RedactedNode` and does not surface the
    Observed Outcome Revision or Measurement chains (cascade by parent
    restriction). When visible, walks to the sourced Observed Outcome
    Revision named on the Assessment row, authorized independently.
    """
    assessment_row = _load_assessment_row(connection, assessment_id)
    if assessment_row is None:
        return AssessmentObservationChain(
            assessment=RedactedNode(kind=_NODE_KIND_ASSESSMENT),
            observed_outcome_revision=None,
            measurement_chains=(),
        )
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_ASSESSMENT,
        target=TargetRef(
            kind=_NODE_KIND_ASSESSMENT,
            id=assessment_id,
            revision_id=None,
            scope=assessment_row["applicable_scope"],
        ),
        at=at,
    ):
        return AssessmentObservationChain(
            assessment=RedactedNode(kind=_NODE_KIND_ASSESSMENT),
            observed_outcome_revision=None,
            measurement_chains=(),
        )

    assessment_node = SuccessConditionAssessmentNode(
        assessment_id=assessment_row["assessment_id"],
        target_intended_outcome_resource_id=assessment_row[
            "target_intended_outcome_resource_id"
        ],
        target_intended_outcome_revision_id=assessment_row[
            "target_intended_outcome_revision_id"
        ],
        sourced_observed_outcome_id=assessment_row[
            "sourced_observed_outcome_id"
        ],
        sourced_observed_outcome_revision_id=assessment_row[
            "sourced_observed_outcome_revision_id"
        ],
        assessment_category=assessment_row["assessment_category"],
        assessment_rationale=assessment_row["assessment_rationale"],
        assessing_party_id=assessment_row["assessing_party_id"],
        authority_basis_type=assessment_row["authority_basis_type"],
        authority_basis_id=assessment_row["authority_basis_id"],
        applicable_scope=assessment_row["applicable_scope"],
        recorded_at=assessment_row["recorded_at"],
    )

    observed_outcome_node, measurement_chains = _build_observed_outcome_subtree(
        self,
        connection,
        observed_outcome_revision_id=assessment_row[
            "sourced_observed_outcome_revision_id"
        ],
        party_id=party_id,
        at=at,
    )
    return AssessmentObservationChain(
        assessment=assessment_node,
        observed_outcome_revision=observed_outcome_node,
        measurement_chains=measurement_chains,
    )


def _resolve_intended_outcome_and_decision(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    intended_outcome_revision_id: str,
    party_id: str,
    at: datetime,
) -> tuple["IntendedOutcomeRevisionNode | RedactedNode | None", Optional[DecisionProvenanceChain]]:
    """Resolve the Intended Outcome Revision node and the Slice 1 Decision tail.

    Reads the Slice 2 ``Intended_Outcome_Revisions`` row, builds the node
    (or a :class:`RedactedNode` when restricted), then bridges to the
    Slice 1 Decision tail by loading the latest Objective Revision for the
    Intended Outcome's ``target_objective_id`` at ``at`` and delegating to
    the existing :meth:`ProvenanceNavigator.navigate_decision` with the
    Objective Revision's ``target_decision_id`` (cascade by record: the
    Decision tail is walked even when the Intended Outcome node is
    redacted, mirroring the Slice 2 ``navigate_plan_approval``
    convention). ``navigate_decision`` is reused unchanged (Requirement
    60).
    """
    io_row = _load_intended_outcome_revision_row(
        connection, intended_outcome_revision_id
    )
    if io_row is None:
        return None, None

    intended_outcome_node: "IntendedOutcomeRevisionNode | RedactedNode"
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_INTENDED_OUTCOME_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_INTENDED_OUTCOME_REVISION,
            id=io_row["intended_outcome_id"],
            revision_id=io_row["intended_outcome_revision_id"],
            scope=io_row["applicable_scope"],
        ),
        at=at,
    ):
        intended_outcome_node = RedactedNode(
            kind=_NODE_KIND_INTENDED_OUTCOME_REVISION
        )
    else:
        intended_outcome_node = IntendedOutcomeRevisionNode(
            intended_outcome_revision_id=io_row["intended_outcome_revision_id"],
            intended_outcome_id=io_row["intended_outcome_id"],
            parent_revision_id=io_row["parent_revision_id"],
            outcome_kind=io_row["outcome_kind"],
            target_objective_id=io_row["target_objective_id"],
            success_condition=io_row["success_condition"],
            observation_window=io_row["observation_window"],
            attribution_assumption=io_row["attribution_assumption"],
            authoring_party_id=io_row["authoring_party_id"],
            applicable_scope=io_row["applicable_scope"],
            recorded_at=io_row["recorded_at"],
        )

    # Bridge to the Slice 1 Decision tail via the latest Objective Revision
    # at ``at`` (same latest-at-time rule the Slice 2 navigator uses). The
    # Objective Revision's ``target_decision_id`` is the link delegated to
    # ``navigate_decision``.
    decision_chain: Optional[DecisionProvenanceChain] = None
    objective_revision_row = self._load_latest_objective_revision_row(
        connection, objective_id=io_row["target_objective_id"], at=at
    )
    if objective_revision_row is not None:
        target_decision_id = objective_revision_row["target_decision_id"]
        try:
            decision_chain = self.navigate_decision(
                connection,
                decision_id=target_decision_id,
                party_id=party_id,
                at=at,
            )
        except DecisionUnresolvableError:
            decision_chain = None

    return intended_outcome_node, decision_chain


# ---------------------------------------------------------------------------
# Navigator methods (attached to ProvenanceNavigator by
# register_outcome_navigation below).
# ---------------------------------------------------------------------------


def _navigate_outcome_review(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    outcome_review_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> OutcomeProvenanceTree:
    """Walk the Outcome Measurement Provenance Chain rooted at an Outcome Review.

    Implements design §"Provenance_Navigator (extended)" item 1. The walk
    descends two parallel legs and one Intended Outcome → Decision tail:

    1. **Head (Requirements 55.3, 55.4).** Load the
       ``Outcome_Review_Records`` row for ``outcome_review_id``. When it
       does not exist, raise :class:`OutcomeReviewUnresolvableError`
       naming only the unresolvable reference. When it exists but the
       Party lacks ``view.outcome_review_record`` authority, raise the
       same exception so the response form is indistinguishable
       (AD-WS-9).
    2. **Assessment leg (Requirement 51.2).** Walk every ``Cites`` /
       ``review_assessment`` Relationship to the cited Success-Condition
       Assessments, then each Assessment → Observed Outcome Revision →
       Measurement Record(s) → Measurement Definition Revision, building
       one :class:`AssessmentObservationChain` each.
    3. **Completion leg (Requirement 51.2).** Walk every ``Cites`` /
       ``review_completion`` Relationship to the cited Completion Records
       and delegate each to the existing
       :meth:`ProvenanceNavigator.navigate_completion`, which yields the
       Slice 3 Execution Provenance Chain down to produced Deliverable
       Revisions. An unresolved or restricted Completion yields ``None``
       (indistinguishable).
    4. **Cited produced Deliverable Revisions.** Walk every ``Cites`` /
       ``review_deliverable`` Relationship to the directly-cited produced
       Deliverable Revisions, authorized independently.
    5. **Intended Outcome → Decision tail (Requirement 51.1).** Resolve
       the addressed Intended Outcome Revision and bridge through its
       Objective to the Slice 1 Decision via the delegated
       :meth:`navigate_decision`.
    6. **Gap descriptors (Requirements 51.3, 55.4).** Collect unresolved
       gap-category Omission Entries on the Outcome Review's Provenance
       Manifest via the existing
       :meth:`_collect_gap_descriptors_for_subject` helper.

    Idempotent retrieval (Requirements 51.4, 55.5): every row consulted
    lives on an append-only table and every list is ordered with a
    deterministic tiebreaker, so two invocations with the same
    ``(outcome_review_id, party_id, at)`` return byte-equivalent
    :class:`OutcomeProvenanceTree` instances.
    """
    effective_at = at if at is not None else self.clock.now()

    review_row = _load_outcome_review_row(connection, outcome_review_id)
    if review_row is None:
        raise OutcomeReviewUnresolvableError(outcome_review_id)
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_OUTCOME_REVIEW,
        target=TargetRef(
            kind=_NODE_KIND_OUTCOME_REVIEW,
            id=outcome_review_id,
            revision_id=None,
            scope=review_row["applicable_scope"],
        ),
        at=effective_at,
    ):
        raise OutcomeReviewUnresolvableError(outcome_review_id)

    outcome_review_node = OutcomeReviewNode(
        outcome_review_id=review_row["outcome_review_id"],
        target_intended_outcome_resource_id=review_row[
            "target_intended_outcome_resource_id"
        ],
        target_intended_outcome_revision_id=review_row[
            "target_intended_outcome_revision_id"
        ],
        review_outcome=review_row["review_outcome"],
        attribution_stance=review_row["attribution_stance"],
        confidence=review_row["confidence"],
        review_rationale=review_row["review_rationale"],
        attribution_evidence_reference=review_row[
            "attribution_evidence_reference"
        ],
        reviewing_party_id=review_row["reviewing_party_id"],
        authority_basis_type=review_row["authority_basis_type"],
        authority_basis_id=review_row["authority_basis_id"],
        applicable_scope=review_row["applicable_scope"],
        recorded_at=review_row["recorded_at"],
    )

    # Leg 1: cited Success-Condition Assessments.
    assessment_chains: list[AssessmentObservationChain] = []
    for cited in _load_cited_targets(
        connection,
        source_kind=_NODE_KIND_OUTCOME_REVIEW,
        source_id=outcome_review_id,
        source_revision_id=None,
        semantic_role=_SEMANTIC_ROLE_REVIEW_ASSESSMENT,
    ):
        assessment_chains.append(
            _build_assessment_chain(
                self,
                connection,
                assessment_id=cited["target_id"],
                party_id=party_id,
                at=effective_at,
            )
        )

    # Leg 2: cited Completion Records (delegated to navigate_completion).
    completion_chains: list[OutcomeCompletionChain] = []
    for cited in _load_cited_targets(
        connection,
        source_kind=_NODE_KIND_OUTCOME_REVIEW,
        source_id=outcome_review_id,
        source_revision_id=None,
        semantic_role=_SEMANTIC_ROLE_REVIEW_COMPLETION,
    ):
        completion_id = cited["target_id"]
        try:
            execution_tree: Optional[ExecutionProvenanceTree] = (
                self.navigate_completion(
                    connection,
                    completion_id=completion_id,
                    party_id=party_id,
                    at=effective_at,
                )
            )
        except CompletionUnresolvableError:
            execution_tree = None
        completion_chains.append(
            OutcomeCompletionChain(
                completion_id=completion_id,
                execution_tree=execution_tree,
            )
        )

    # Leg 3: cited produced Deliverable Revisions (directly cited).
    cited_deliverable_revisions: list = []
    for cited in _load_cited_targets(
        connection,
        source_kind=_NODE_KIND_OUTCOME_REVIEW,
        source_id=outcome_review_id,
        source_revision_id=None,
        semantic_role=_SEMANTIC_ROLE_REVIEW_DELIVERABLE,
    ):
        cited_deliverable_revisions.append(
            _build_deliverable_revision_node(
                self,
                connection,
                deliverable_revision_id=cited["target_revision_id"],
                party_id=party_id,
                at=effective_at,
            )
        )

    # Tail: Intended Outcome Revision → Objective → Slice 1 Decision.
    intended_outcome_node, decision_chain = (
        _resolve_intended_outcome_and_decision(
            self,
            connection,
            intended_outcome_revision_id=review_row[
                "target_intended_outcome_revision_id"
            ],
            party_id=party_id,
            at=effective_at,
        )
    )

    gap_descriptors = _collect_outcome_gap_descriptors(
        self,
        connection,
        subject_kind=_NODE_KIND_OUTCOME_REVIEW,
        subject_id=outcome_review_id,
        subject_revision_id=None,
        next_reachable_node_identity=outcome_review_id,
    )

    return OutcomeProvenanceTree(
        outcome_review=outcome_review_node,
        assessment_chains=tuple(assessment_chains),
        completion_chains=tuple(completion_chains),
        cited_deliverable_revisions=tuple(cited_deliverable_revisions),
        intended_outcome_revision=intended_outcome_node,
        decision_chain=decision_chain,
        gap_descriptors=gap_descriptors,
        requested_node_kind=_NODE_KIND_OUTCOME_REVIEW,
        requested_node_id=outcome_review_id,
    )


def _navigate_outcome_node(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    node_kind: str,
    node_id: str,
    party_id: str,
    at: Optional[datetime] = None,
) -> OutcomeProvenanceTree:
    """Short-form traversal beginning at a node lower than the Outcome Review.

    Implements design §"Provenance_Navigator (extended)" item 2
    (Requirement 55.1). Dispatches on ``node_kind`` to the matching
    sub-chain builder and returns an :class:`OutcomeProvenanceTree` rooted
    lower (``outcome_review`` is ``None``). The Intended Outcome → Decision
    tail is resolved from the entry node's addressed / target Intended
    Outcome Revision so a short-form chain still reaches the Slice 1
    Decision when authorized.

    Raises:
        OutcomeNodeUnresolvableError: ``node_kind`` is not a recognized
            short-form entry kind, ``node_id`` does not resolve, or the
            requesting Party lacks view authority on the resolved node
            (the unresolved and restricted cases are indistinguishable).
    """
    effective_at = at if at is not None else self.clock.now()

    if node_kind not in _SHORT_FORM_NODE_KINDS:
        raise OutcomeNodeUnresolvableError(node_kind, node_id)

    assessment_chains: tuple = ()
    intended_outcome_revision_id: Optional[str] = None

    if node_kind == _NODE_KIND_ASSESSMENT:
        chain = _build_assessment_chain(
            self,
            connection,
            assessment_id=node_id,
            party_id=party_id,
            at=effective_at,
        )
        if isinstance(chain.assessment, RedactedNode):
            raise OutcomeNodeUnresolvableError(node_kind, node_id)
        assessment_chains = (chain,)
        intended_outcome_revision_id = (
            chain.assessment.target_intended_outcome_revision_id
        )
    elif node_kind == _NODE_KIND_OBSERVED_OUTCOME_REVISION:
        observed_node, measurement_chains = _build_observed_outcome_subtree(
            self,
            connection,
            observed_outcome_revision_id=node_id,
            party_id=party_id,
            at=effective_at,
        )
        if isinstance(observed_node, RedactedNode):
            raise OutcomeNodeUnresolvableError(node_kind, node_id)
        assessment_chains = (
            AssessmentObservationChain(
                assessment=RedactedNode(kind=_NODE_KIND_ASSESSMENT),
                observed_outcome_revision=observed_node,
                measurement_chains=measurement_chains,
            ),
        )
        intended_outcome_revision_id = (
            observed_node.target_intended_outcome_revision_id
        )
    elif node_kind == _NODE_KIND_MEASUREMENT_RECORD:
        chain = _build_measurement_chain(
            self,
            connection,
            measurement_record_id=node_id,
            party_id=party_id,
            at=effective_at,
        )
        if isinstance(chain.measurement_record, RedactedNode):
            raise OutcomeNodeUnresolvableError(node_kind, node_id)
        assessment_chains = (
            AssessmentObservationChain(
                assessment=RedactedNode(kind=_NODE_KIND_ASSESSMENT),
                observed_outcome_revision=RedactedNode(
                    kind=_NODE_KIND_OBSERVED_OUTCOME_REVISION
                ),
                measurement_chains=(chain,),
            ),
        )
        definition_node = chain.measurement_definition_revision
        if isinstance(definition_node, MeasurementDefinitionRevisionNode):
            intended_outcome_revision_id = (
                definition_node.target_intended_outcome_revision_id
            )
    else:  # _NODE_KIND_MEASUREMENT_DEFINITION_REVISION
        definition_node = _build_measurement_definition_revision_node(
            self,
            connection,
            measurement_definition_revision_id=node_id,
            party_id=party_id,
            at=effective_at,
        )
        if isinstance(definition_node, RedactedNode):
            raise OutcomeNodeUnresolvableError(node_kind, node_id)
        assessment_chains = (
            AssessmentObservationChain(
                assessment=RedactedNode(kind=_NODE_KIND_ASSESSMENT),
                observed_outcome_revision=RedactedNode(
                    kind=_NODE_KIND_OBSERVED_OUTCOME_REVISION
                ),
                measurement_chains=(
                    MeasurementChain(
                        measurement_record=RedactedNode(
                            kind=_NODE_KIND_MEASUREMENT_RECORD
                        ),
                        measurement_definition_revision=definition_node,
                    ),
                ),
            ),
        )
        intended_outcome_revision_id = (
            definition_node.target_intended_outcome_revision_id
        )

    intended_outcome_node: "IntendedOutcomeRevisionNode | RedactedNode | None" = (
        None
    )
    decision_chain: Optional[DecisionProvenanceChain] = None
    if intended_outcome_revision_id is not None:
        intended_outcome_node, decision_chain = (
            _resolve_intended_outcome_and_decision(
                self,
                connection,
                intended_outcome_revision_id=intended_outcome_revision_id,
                party_id=party_id,
                at=effective_at,
            )
        )

    gap_descriptors = _collect_outcome_gap_descriptors(
        self,
        connection,
        subject_kind=node_kind,
        subject_id=node_id,
        subject_revision_id=None,
        next_reachable_node_identity=node_id,
    )

    return OutcomeProvenanceTree(
        outcome_review=None,
        assessment_chains=assessment_chains,
        completion_chains=(),
        cited_deliverable_revisions=(),
        intended_outcome_revision=intended_outcome_node,
        decision_chain=decision_chain,
        gap_descriptors=gap_descriptors,
        requested_node_kind=node_kind,
        requested_node_id=node_id,
    )


def _build_deliverable_revision_node(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    deliverable_revision_id: str,
    party_id: str,
    at: datetime,
) -> "DeliverableRevisionNode | RedactedNode":
    """Build a produced Deliverable Revision node (or redaction marker).

    Reuses the Slice 3 ``_load_deliverable_revision_row`` helper attached
    to :class:`ProvenanceNavigator` and the
    :class:`~walking_slice.provenance.DeliverableRevisionNode` value
    object so the directly-cited produced Deliverable Revisions surface
    with the same shape (``role_marker``, ``content_digest_sha256``,
    Requirement 35.8) as those reached through the delegated
    ``navigate_completion`` leg.
    """
    revision_row = self._load_deliverable_revision_row(
        connection, deliverable_revision_id
    )
    if revision_row is None:
        return RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
    if not self._is_permitted(
        connection,
        party_id=party_id,
        action=_ACTION_VIEW_DELIVERABLE_REVISION,
        target=TargetRef(
            kind=_NODE_KIND_DELIVERABLE_REVISION,
            id=revision_row["deliverable_id"],
            revision_id=revision_row["deliverable_revision_id"],
            scope=revision_row["deliverable_id"],
        ),
        at=at,
    ):
        return RedactedNode(kind=_NODE_KIND_DELIVERABLE_REVISION)
    return DeliverableRevisionNode(
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


def _collect_outcome_gap_descriptors(
    self: ProvenanceNavigator,
    connection: Connection,
    *,
    subject_kind: str,
    subject_id: str,
    subject_revision_id: Optional[str],
    next_reachable_node_identity: Optional[str],
) -> tuple:
    """Collect gap descriptors for one subject when a policy is configured.

    Thin wrapper over the existing
    :meth:`ProvenanceNavigator._collect_gap_descriptors_for_subject`
    (reused unchanged, Requirement 60) that returns an empty tuple when no
    :class:`~walking_slice.disclosure.DisclosurePolicy` was wired into the
    navigator — matching the Slice 3 ``navigate_completion`` convention.
    """
    if self.disclosure_policy is None:
        return ()
    return tuple(
        self._collect_gap_descriptors_for_subject(
            connection,
            subject_kind=subject_kind,
            subject_id=subject_id,
            subject_revision_id=subject_revision_id,
            next_reachable_node_identity=next_reachable_node_identity,
        )
    )


# ---------------------------------------------------------------------------
# Registration with walking_slice.provenance.
# ---------------------------------------------------------------------------


def register_outcome_navigation() -> None:
    """Attach the Slice 4 traversals to :class:`ProvenanceNavigator`.

    Strictly additive (Requirement 60): attaches ``navigate_outcome_review``
    and ``navigate_outcome_node`` (plus the row-load helpers used by the
    short-form variants) onto the existing class without modifying any
    Slice 1 / Slice 2 / Slice 3 method. Idempotent — safe to call more than
    once (the FastAPI startup hook in task 13.2 calls it explicitly; the
    module also calls it at import time so a bare import registers the
    surface).
    """
    ProvenanceNavigator.navigate_outcome_review = _navigate_outcome_review
    ProvenanceNavigator.navigate_outcome_node = _navigate_outcome_node


# Module-level public aliases mirroring the design's documented public
# surface. They are the unbound functions attached as methods above; the
# integration path is ``navigator.navigate_outcome_review(...)`` /
# ``navigator.navigate_outcome_node(...)`` after
# :func:`register_outcome_navigation` (or a bare import) has run.
navigate_outcome_review = _navigate_outcome_review
navigate_outcome_node = _navigate_outcome_node


# Register at import time so importing this module is sufficient to make
# the traversals available on every ProvenanceNavigator instance.
register_outcome_navigation()
