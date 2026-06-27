"""Shared in-memory value objects for the third walking slice (Execution_Service).

These DTOs cross module boundaries inside the modular monolith and are the
authoritative source for request/response shapes consumed by the
Execution_Service modules. The definitions track
``.kiro/specs/third-walking-slice/design.md`` §"In-Memory Value Objects"
verbatim — adding a field here without updating the design document is a spec
violation.

Every model is a *frozen* Pydantic v2 :class:`BaseModel` so that once a
service has been handed a reference, the receiver can rely on its bytes not
changing while the in-flight transaction completes (mirroring the Slice 1
convention in :mod:`walking_slice.models` and the Slice 2 convention in
:mod:`walking_slice.planning.models`). ``extra="forbid"`` rejects unknown
attributes so typo'd field names fail loudly instead of silently dropping
data.

Reuse contract (task 3.1, design §"In-Memory Value Objects" final paragraph):
    ``AuthorityBasisRef``, ``ProvenanceNode``, and ``GapDescriptor`` are
        reused unchanged from :mod:`walking_slice.models`.
    ``TargetRef`` is reused unchanged from
        :mod:`walking_slice.authorization`.
    ``ProjectionEnvelope`` is reused unchanged from
        :mod:`walking_slice.projection`.
    ``Clock`` is reused unchanged from :mod:`walking_slice.clock`.
    None of these Slice 1 / Slice 2 types is redefined here.

Requirements satisfied (per task 3.1):
    22.1, 22.2 — Every Slice 3 reference object carries the durable UUID(s)
        minted by the Identity_Service so receivers can resolve the
        referent without embedding business meaning, and the eight Slice 3
        identifier roles remain pairwise disjoint at the type level
        (Work Assignment Record, Work Event Record, Time Entry Record,
        Deliverable Production Record, Milestone Acceptance Record, and
        Completion Record each get a distinct frozen reference type).
    23.3 — :class:`WorkAssignmentRef` is the post-write reference returned by
        ``WorkAssignmentService.create_work_assignment``; its single
        ``work_assignment_id`` field is the durable Identity that subsequent
        Slice 3 writes (Work Events, Time Entries, Deliverable Productions)
        address.
    24.2 — :class:`WorkEventRef` carries the Work Event Record Identity, the
        target Work Assignment Identity, and the ``event_kind`` discriminator
        drawn from the closed enumeration ``{started, progress_note, paused,
        resumed, deliverable_drafted}``.
    25.2 — :class:`TimeEntryRef` carries the Time Entry Record Identity and
        the target Work Assignment Identity; the per-record decimal-effort
        and time-ordering invariants are enforced at the persistence layer
        and on the inbound request shape, not on this reference.
    27.2 — :class:`DeliverableProductionRef` carries the Deliverable
        Production Record Identity together with the three durable
        identifiers it binds: the source Work Assignment Identity, the
        produced Deliverable Revision Identity, and the target Deliverable
        Expectation Revision Identity.
    28.2 — :class:`MilestoneAcceptanceRef` carries the Milestone Acceptance
        Record Identity and the source Deliverable Production Record
        Identity; the ``UNIQUE(source_deliverable_production_id)`` invariant
        (Requirement 28.3) is enforced at the persistence layer.
    29.2 — :class:`CompletionRef` carries the Completion Record Identity and
        the target Approved Plan Revision Identity; the
        ``UNIQUE(target_plan_revision_id)`` invariant (Requirement 29.3) is
        enforced at the persistence layer.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


__all__ = [
    "WorkAssignmentRef",
    "WorkEventRef",
    "TimeEntryRef",
    "DeliverableProductionRef",
    "MilestoneAcceptanceRef",
    "CompletionRef",
]


class _FrozenModel(BaseModel):
    """Common configuration for every Slice 3 Execution_Service value object.

    ``frozen=True`` makes instances hashable and prevents field assignment;
    ``extra="forbid"`` rejects unknown attributes so call-sites that pass a
    typo'd field name fail loudly instead of silently dropping data. This
    mirrors the ``_FrozenModel`` convention established by Slice 1 in
    :mod:`walking_slice.models` and Slice 2 in
    :mod:`walking_slice.planning.models`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class WorkAssignmentRef(_FrozenModel):
    """Reference to a Work Assignment Record by its durable Identity.

    Resolved against the ``Work_Assignment_Records`` table. The reference is
    Record-scoped (Requirement 22.2): the Work Assignment Record is itself
    an Immutable Record (per [`02-domain-model.md`](../../../documents/02-domain-model.md)
    §8.2 Execution Record), so a single durable identifier is sufficient — no
    Revision identifier is required.

    Receivers that need the target Plan Revision Identity, the assignee
    Party Identity, the assignment authority Party Identity, the assignment
    rationale, or the authority basis MUST re-read the row via
    ``WorkAssignmentService.get_work_assignment`` (or the equivalent
    indexed ``SELECT``). The reference does not embed those fields so it
    cannot be used to forge a Work Assignment's payload at any boundary.
    """

    work_assignment_id: UUID


class WorkEventRef(_FrozenModel):
    """Reference to a Work Event Record together with its event-kind discriminator.

    Resolved against the ``Work_Event_Records`` table. The reference carries
    three durable values:

    - ``work_event_id`` — the Work Event Record Identity (Requirement 22.2).
    - ``target_work_assignment_id`` — the Work Assignment Record Identity the
      Work Event addresses (Requirement 24.2). Receivers compare this value
      against the Work Assignment Record they hold; mismatches indicate a
      caller is attempting to record a Work Event against the wrong Work
      Assignment.
    - ``event_kind`` — the closed enumeration of Work Event kinds permitted
      by Requirement 24.2. The per-Work-Assignment state machine described in
      design §"Event-kind state machine" (at most one ``started`` per Work
      Assignment, ``paused`` → ``resumed`` ordering) is enforced by
      ``WorkEventService.create_work_event``; this reference is the
      post-write shape of the discriminator and is not a source of truth for
      the state machine.
    """

    work_event_id: UUID
    target_work_assignment_id: UUID
    event_kind: Literal[
        "started", "progress_note", "paused", "resumed", "deliverable_drafted"
    ]


class TimeEntryRef(_FrozenModel):
    """Reference to a Time Entry Record by its durable Identity and target.

    Resolved against the ``Time_Entry_Records`` table. The reference carries
    the Time Entry Record Identity (Requirement 22.2) and the Work Assignment
    Record Identity the Time Entry addresses (Requirement 25.2).

    The decimal ``effort_hours`` value, the ``effort_period_start`` /
    ``effort_period_end`` window, and the ``recorded_at`` audit timestamp are
    not carried on this reference. Receivers needing those values MUST
    re-read the row via ``TimeEntryService.get_time_entry`` (or the
    equivalent indexed ``SELECT``) so the ISO-decimal regex, the numeric
    range, and the ``effort_period_start <= effort_period_end <= recorded_at``
    ordering are validated against the persisted bytes rather than the
    boundary-time payload.
    """

    time_entry_id: UUID
    target_work_assignment_id: UUID


class DeliverableProductionRef(_FrozenModel):
    """Reference to a Deliverable Production Record and the three identities it binds.

    Resolved against the ``Deliverable_Production_Records`` table. A
    Deliverable Production Record binds three durable identifiers per
    Requirement 27.2:

    - ``source_work_assignment_id`` — the Work Assignment Record under whose
      authority the Deliverable was produced. ``DeliverableProductionService``
      requires this Identity to match the ``originating_work_assignment_id``
      column on the produced Deliverable Revision (Requirement 27.4), so the
      reference exposes the value its consumers compare against.
    - ``produced_deliverable_revision_id`` — the produced Deliverable Revision
      Identity reachable through the Slice 3 ``Produces`` Relationship
      (design §"Architectural Decisions — AD-WS-26").
    - ``target_deliverable_expectation_revision_id`` — the Deliverable
      Expectation Revision Identity reachable through the Slice 3
      ``Addresses`` Relationship; Project-membership compatibility against
      the source Work Assignment's Plan Revision is enforced by
      ``DeliverableProductionService`` (Requirement 27.3).

    The Deliverable Production Record itself is Record-scoped
    (Requirement 22.2); no Revision identifier is required.
    """

    deliverable_production_id: UUID
    source_work_assignment_id: UUID
    produced_deliverable_revision_id: UUID
    target_deliverable_expectation_revision_id: UUID


class MilestoneAcceptanceRef(_FrozenModel):
    """Reference to a Milestone Acceptance Record and the Production it accepts.

    Resolved against the ``Milestone_Acceptance_Records`` table. The
    reference carries the Milestone Acceptance Record Identity
    (Requirement 22.2) and the source Deliverable Production Record Identity
    (Requirement 28.2) it addresses.

    Per Requirement 28.3, at most one Milestone Acceptance Record may target
    a given Deliverable Production Record; that uniqueness is enforced by a
    ``UNIQUE`` constraint on
    ``Milestone_Acceptance_Records.source_deliverable_production_id``, not
    on this reference type. The ``outcome`` (Accept / Reject) and
    ``rationale`` fields are not carried on this reference; receivers
    needing them MUST re-read the row.
    """

    milestone_acceptance_id: UUID
    source_deliverable_production_id: UUID


class CompletionRef(_FrozenModel):
    """Reference to a Completion Record and the Approved Plan Revision it completes.

    Resolved against the ``Completion_Records`` table. The reference carries
    the Completion Record Identity (Requirement 22.2) and the target
    Approved Plan Revision Identity (Requirement 29.2).

    Per Requirement 29.3, at most one Completion Record may target a given
    Plan Revision; that uniqueness is enforced by a ``UNIQUE`` constraint on
    ``Completion_Records.target_plan_revision_id``, not on this reference
    type. Per Requirement 29.4, the target Plan Revision's
    ``lifecycle_state`` must be ``approved`` at the recorded time; that
    invariant is enforced by ``CompletionService.create_completion`` against
    the persisted Plan Revision row, not against this reference.

    Per Requirement 34 (Output / Outcome separation), a Completion Record
    does NOT assert any observed Outcome. The reference therefore carries
    no observed-outcome attribute, no Measurement Record identifier, and no
    success-condition assessment identifier; attempts to attach such fields
    are rejected at the API boundary by the prohibited-attribute helper
    introduced in task 3.3.
    """

    completion_id: UUID
    target_plan_revision_id: UUID
