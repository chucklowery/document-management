"""walking_slice.planning — Second-walking-slice Planning_Service package.

Implements the additive Planning_Service specified in
``.kiro/specs/second-walking-slice/design.md``. Modules are added by the
sub-tasks in ``.kiro/specs/second-walking-slice/tasks.md``:

- ``_persistence``    — Slice 2 SQLite schema (tables, indexes, append-only
                        triggers, and the Plan Revision lifecycle trigger
                        gated by the ``walking_slice.plan_approval_in_progress``
                        session-scoped state).
- ``_disclosure``     — additive ``Disclosure_Policy_Coverage`` seeding for
                        Slice 2 node kinds (task 1.4).
- ``_immutability``   — application-level enforcement of Approved Plan
                        Revision immutability (task 11.2).
- ``_projection``     — Projection-envelope wrapper for status-bearing
                        Planning_Service responses (task 14.1).
- ``models``          — frozen Pydantic value objects (task 2.1).
- ``objectives``,
  ``intended_outcomes``,
  ``projects``,
  ``deliverable_expectations``,
  ``activity_plans``,
  ``plan_revisions``,
  ``plan_reviews``,
  ``plan_approvals``  — per-Resource Planning_Service modules (tasks 3..11).
- ``_routes``         — FastAPI router composition (task 15.1).

The package is strictly additive with respect to Slice 1 (Requirement 19):
no Slice 1 module is imported in a way that requires modification, and the
only Slice 1 touch-points are the additive enumeration value and two
additive NULLable columns already applied by ``walking_slice.persistence``.
"""

from walking_slice.planning._immutability import (
    APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE,
    APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE,
    ApprovedPlanRevisionImmutableAuditFailureError,
    ApprovedPlanRevisionImmutableError,
    enforce_approved_plan_revision_immutability,
    is_plan_revision_approved,
    is_planning_immutability_violation,
    map_integrity_error_to_immutability,
)
from walking_slice.planning._projection import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_ORPHANED,
    PLAN_STATUS_SUPERSEDED,
    PLANNING_PROJECTED_STATUSES,
    PLANNING_PROJECTION_DEFINITION,
    PLANNING_PROJECTION_DEFINITION_NAME,
    PLANNING_PROJECTION_DEFINITION_VERSION,
    PROVENANCE_STATUS_INCOMPLETE,
    planning_projection_registry,
    wrap_planning_status,
)


__all__: list[str] = [
    "APPROVED_PLAN_REVISION_IMMUTABLE_ERROR_CODE",
    "APPROVED_PLAN_REVISION_IMMUTABLE_REASON_CODE",
    "ApprovedPlanRevisionImmutableAuditFailureError",
    "ApprovedPlanRevisionImmutableError",
    "PLAN_STATUS_APPROVED",
    "PLAN_STATUS_DRAFT",
    "PLAN_STATUS_ORPHANED",
    "PLAN_STATUS_SUPERSEDED",
    "PLANNING_PROJECTED_STATUSES",
    "PLANNING_PROJECTION_DEFINITION",
    "PLANNING_PROJECTION_DEFINITION_NAME",
    "PLANNING_PROJECTION_DEFINITION_VERSION",
    "PROVENANCE_STATUS_INCOMPLETE",
    "enforce_approved_plan_revision_immutability",
    "is_plan_revision_approved",
    "is_planning_immutability_violation",
    "map_integrity_error_to_immutability",
    "planning_projection_registry",
    "wrap_planning_status",
]
