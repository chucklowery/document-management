"""Shared in-memory value objects for the second walking slice (Planning_Service).

These DTOs cross module boundaries inside the modular monolith and are the
authoritative source for request/response shapes consumed by the Planning_Service
modules. The definitions track ``.kiro/specs/second-walking-slice/design.md``
§"In-Memory Value Objects" verbatim — adding a field here without updating the
design document is a spec violation.

Every model is a *frozen* Pydantic v2 :class:`BaseModel` so that once a service
has been handed a reference, the receiver can rely on its bytes not changing
while the in-flight transaction completes (mirroring the Slice 1 convention in
``walking_slice.models``). ``extra="forbid"`` rejects unknown attributes so
typo'd field names fail loudly instead of silently dropping data.

Reuse contract (task 2.1):
    ``AuthorityBasisRef`` is reused unchanged from
    :mod:`walking_slice.models`.
    ``TargetRef`` is reused unchanged from
    :mod:`walking_slice.authorization`.
    ``Clock`` is reused unchanged from
    :mod:`walking_slice.clock`.
    None of these Slice 1 types is redefined here.

Requirements satisfied (per task 2.1):
    1.1, 1.2 — Resource / Revision / Immutable Record identity discipline:
        every reference object carries the durable UUID(s) assigned by the
        Identity_Service so receivers can resolve the referent without
        embedding business meaning.
    8.2     — Plan Review authority basis enumeration consumed via the reused
        :class:`AuthorityBasisRef` (no redefinition here).
    9.2     — Plan Approval authority basis enumeration consumed via the reused
        :class:`AuthorityBasisRef`; Plan Approval Omission Entries are typed
        here via :class:`PlanApprovalOmissionEntry` and bound to the
        Requirement 9.2 / design §"Planning_Service.PlanApprovals" contract.
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


__all__ = [
    "ObjectiveRef",
    "IntendedOutcomeRef",
    "ProjectRef",
    "DeliverableExpectationRef",
    "ActivityPlanRef",
    "PlanRevisionRef",
    "PlanApprovalRef",
    "PlanApprovalOmissionEntry",
]


class _FrozenModel(BaseModel):
    """Common configuration for every Slice 2 planning value object.

    ``frozen=True`` makes instances hashable and prevents field assignment.
    ``extra="forbid"`` rejects unknown attributes so call-sites that pass a
    typo'd field name fail loudly instead of silently dropping data. This
    mirrors the ``_FrozenModel`` convention established by Slice 1 in
    :mod:`walking_slice.models`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class ObjectiveRef(_FrozenModel):
    """Reference to an Objective Resource by its durable Identity.

    The receiver resolves ``objective_id`` against the ``Objectives`` table
    or, where Revision-level granularity is required, by joining onto
    ``Objective_Revisions``. The reference is Resource-scoped (Requirement
    1.2) — Objective Revision Identity is conveyed by a separate field on
    the carrying request/response model when needed.
    """

    objective_id: UUID


class IntendedOutcomeRef(_FrozenModel):
    """Reference to an Intended Outcome Resource by its durable Identity.

    Resolved against the ``Intended_Outcomes`` table. The ``outcome_kind``
    discriminator (always ``'intended'`` for Slice 2 — Requirement 3.2,
    Requirement 13) is enforced at the persistence layer; this reference
    does not carry it because every reference returned by the Planning_Service
    is, by construction, to an intended-kind outcome.
    """

    intended_outcome_id: UUID


class ProjectRef(_FrozenModel):
    """Reference to a Project Resource by its durable Identity.

    Resolved against the ``Projects`` table. Project Resource Identity and
    Activity Plan Resource Identity are disjoint identifier sets per
    Requirement 4.5; this reference carries only the Project identifier
    and does not embed any Activity Plan reference.
    """

    project_id: UUID


class DeliverableExpectationRef(_FrozenModel):
    """Reference to a Deliverable Expectation Resource by its durable Identity.

    Resolved against the ``Deliverable_Expectations`` table. The Deliverable
    Expectation declares *expected* output of a Project and is strictly
    distinguished from a produced Deliverable (Requirement 13.2); the
    distinction is enforced at the persistence and API boundary, not on this
    reference.
    """

    deliverable_expectation_id: UUID


class ActivityPlanRef(_FrozenModel):
    """Reference to an Activity Plan Resource by its durable Identity.

    Resolved against the ``Activity_Plans`` table. The Activity Plan Resource
    Identity is the organizing identity for one or more Plan Revisions
    (Requirement 6, Requirement 1.2); Plan Revision Identity is conveyed by
    :class:`PlanRevisionRef`.
    """

    activity_plan_id: UUID


class PlanRevisionRef(_FrozenModel):
    """Reference to an immutable Plan Revision of an Activity Plan.

    Carries both the parent ``activity_plan_id`` and the ``plan_revision_id``
    so receivers can validate the parent-child relationship (Requirement 7)
    without a second round-trip to ``Plan_Revisions``. Plan Revisions are
    insert-only with the single permitted ``draft → approved`` lifecycle
    transition gated by the Plan Approval transaction (AD-WS-19, AD-WS-20).
    """

    activity_plan_id: UUID
    plan_revision_id: UUID


class PlanApprovalRef(_FrozenModel):
    """Reference to a Plan Approval Immutable Record and its target Plan Revision.

    Carries both the ``plan_approval_id`` (the Governance Decision Immutable
    Record's Identity per [`02-domain-model.md`](../../../documents/02-domain-model.md)
    §8.5) and the ``target_plan_revision_id`` it finalizes as immutable
    (Requirement 9.4 — Approved Plan Revisions are byte-equivalent forever).
    At most one Plan Approval Record may target a given Plan Revision per
    Requirement 9.5; that uniqueness is enforced by a ``UNIQUE`` constraint
    on ``Plan_Approval_Records.target_plan_revision_id``.
    """

    plan_approval_id: UUID
    target_plan_revision_id: UUID


class PlanApprovalOmissionEntry(_FrozenModel):
    """One omission entry recorded on the Plan Approval Provenance Manifest.

    Plan Approval omissions surface the reason a particular upstream source
    was excluded from the approval's recorded provenance. ``category`` is
    the omission discriminator drawn from the Slice-2 enumeration; each
    category has the meaning given in design §"Planning_Service.PlanApprovals":

    - ``intentional``  — the Plan Approver deliberately excluded the source.
    - ``unavailable``  — the source existed at evaluation time but could not
      be retrieved.
    - ``restricted``   — the source was not visible to the approving Party.
    - ``stale``        — the source has been superseded by a later Revision.
    - ``unresolved``   — the source identifier did not resolve to any node.

    ``excluded_source_id`` is the durable Identity of the excluded upstream
    Resource. ``excluded_source_revision_id`` may be omitted when the
    omission targets a Resource header rather than an exact Revision (e.g.
    Source Document or Project Resource). ``rationale`` carries the approver's
    plain-text explanation, bounded to 1..2000 characters per design
    §"In-Memory Value Objects".
    """

    category: Literal[
        "intentional", "unavailable", "restricted", "stale", "unresolved"
    ]
    excluded_source_id: UUID
    excluded_source_revision_id: Optional[UUID] = None
    rationale: str = Field(min_length=1, max_length=2000)
