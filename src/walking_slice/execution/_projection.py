"""Execution_Service execution-status Projection (task 13.1).

Design references
=================

- ``.kiro/specs/third-walking-slice/design.md`` §"Execution-status
  Projection (single explainable Projection)" — the seven projected
  status labels, the seven-step Projection Definition, the
  ``ProjectionEnvelope`` wrapping contract, and the
  withholding-on-unresolvable rule.
- ``.kiro/specs/third-walking-slice/requirements.md`` §"Requirement 39:
  Explainable Projection of Current Execution Status" — acceptance
  criteria 39.1 (envelope contents), 39.2 (derivation indicator),
  39.3 (no derived percent-complete / actual-cost / remaining-work /
  budget-variance / forecast-cost / outcome-attainment fields),
  39.4 (source-Record byte equivalence on corrections), 39.5
  (withholding on unresolvable Projection Definition or source
  Record), 39.6 (no labeling as Observed Outcome).
- ``.kiro/specs/first-walking-slice/design.md`` §"Constitutional
  Posture" (row "System health observable") — Slice 1 Requirement 14
  Projection envelope discipline. The Slice 3 module reuses the
  Slice 1 :class:`walking_slice.projection.ProjectionEnvelope`,
  :class:`walking_slice.projection.StatusProjector`, and
  :class:`walking_slice.projection.ExplanationUnavailableResponse`
  unchanged (Requirement 40.1 — additive only).
- Slice 2 sibling: :mod:`walking_slice.planning._projection` — the
  Planning_Service projection-envelope wrapper. Slice 3 mirrors its
  shape exactly: one Projection Definition name and version constant,
  one set of named projected status strings, one helper that resolves
  the source Records, derives the projected status, and wraps the
  result in a :class:`ProjectionEnvelope`.

Task scope (task 13.1)
======================

This module exposes the *single* explainable Projection the Slice 3
Execution_Service surfaces over its source Records:

1. :class:`ExecutionStatusProjection` — the frozen value object that
   pairs a projected status with its :class:`ProjectionEnvelope` (and,
   when a required source Record is unresolvable, an
   :class:`ExplanationUnavailableResponse` indicator naming the missing
   element per Requirement 39.5).
2. :func:`project_execution_status` — the helper that walks the
   Plan Revision → Work Assignment → Work Event → Deliverable
   Production → Milestone Acceptance → Completion Record chain in one
   read-only pass, derives the projected status per design
   §"Execution-status Projection" steps 1..7, and returns a fully
   populated :class:`ExecutionStatusProjection` (or an
   :class:`ExplanationUnavailableResponse` when the Projection
   Definition itself is unresolvable).
3. :data:`EXECUTION_PROJECTION_DEFINITION` and friends — the
   constants Slice 3 routes register with the singleton
   :class:`StatusProjector` so producer call sites and the projector
   match on the same name and version.
4. :data:`EXECUTION_PROJECTED_STATUSES` — the set of every known
   projected status string, exported so tests can iterate over the
   labels without re-spelling the literals.

The helper does not persist anything (Principle 5.23 — Projections
are derived). Every query is a SELECT against the Slice 3 tables and
the Slice 2 ``Plan_Revisions`` row through
:meth:`PlanRevisionService.get_plan_revision`. No row of any Slice 1,
Slice 2, or Slice 3 table is mutated by this module (Requirement 39.4
and 40.4).

Requirements satisfied (per task 13.1)
======================================

- 39.1 — Every status-bearing response carries the Projection
         Definition, source Resource Identities (the target Plan
         Revision), source Revision Identities (every Slice 3 Record
         the projection consulted), applicable temporal boundary, and
         generated time on the envelope. Both timestamps are produced
         at ISO-8601 second precision.
- 39.2 — The envelope's ``derivation`` indicator is fixed at
         ``"derived"`` by :class:`ProjectionEnvelope`; this module
         never overrides it.
- 39.3 — The projection response carries only the projected status
         label and the envelope; no percent-complete, actual-cost,
         remaining-work, budget-variance, forecast-cost, or
         outcome-attainment value is computed or returned. The
         :class:`ExecutionStatusProjection` shape is the only outward
         data shape and contains none of those fields.
- 39.4 — Every query is a SELECT; no INSERT, UPDATE, or DELETE is
         issued. Source Records are byte-equivalent after a call to
         :func:`project_execution_status`.
- 39.5 — When the Plan Revision Identity does not resolve, when its
         ``lifecycle_state != 'approved'``, or when the Projection
         Definition is not registered, the projection withholds the
         most-progressed status and returns an
         :class:`ExplanationUnavailableResponse` (on the
         Projection-Definition path) or marks the projection
         ``"Provenance incomplete"`` with the indicator attached (on
         the missing-source-Record path).
- 39.6 — The projected status labels are projections of *work
         performed* (Plan Revision approved, in execution, paused,
         deliverable produced, milestone accepted, completion
         recorded) or projection-incompleteness (Provenance
         incomplete). No label references an Observed Outcome, a
         Measurement, an attribution evidence reference, or a
         success-condition assessment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Optional, Union
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from walking_slice.planning.plan_revisions import (
    PlanRevisionRow,
    PlanRevisionService,
)
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
    StatusProjector,
)


__all__ = [
    "EXECUTION_PROJECTION_DEFINITION",
    "EXECUTION_PROJECTION_DEFINITION_NAME",
    "EXECUTION_PROJECTION_DEFINITION_VERSION",
    "EXECUTION_PROJECTED_STATUSES",
    "EXECUTION_STATUS_APPROVED",
    "EXECUTION_STATUS_IN_EXECUTION",
    "EXECUTION_STATUS_EXECUTION_PAUSED",
    "EXECUTION_STATUS_DELIVERABLE_PRODUCED",
    "EXECUTION_STATUS_MILESTONE_ACCEPTED",
    "EXECUTION_STATUS_COMPLETION_RECORDED",
    "EXECUTION_STATUS_PROVENANCE_INCOMPLETE",
    "ExecutionProjectedStatus",
    "ExecutionStatusProjection",
    "ExecutionStatusResponse",
    "execution_projection_registry",
    "project_execution_status",
]


# ---------------------------------------------------------------------------
# Projection Definition name and version (Requirement 39.1).
#
# Centralized so the producer (this module, plus the Slice 3 HTTP route
# layer in task 15.1) and the singleton :class:`StatusProjector` the
# composition step wires at startup match on the same string. A typo at
# either site trips the explanation-unavailable path (Requirement 39.5)
# instead of mislabeling an envelope.
# ---------------------------------------------------------------------------


EXECUTION_PROJECTION_DEFINITION_NAME: Final[str] = "execution.status"
"""Single Projection Definition name for the Slice 3 execution-status
Projection.

Parallel to :data:`walking_slice.trails.TRAIL_PROJECTION_DEFINITION_NAME`
and :data:`walking_slice.planning._projection.PLANNING_PROJECTION_DEFINITION_NAME`.
The name names the *computation* — "the Execution_Service derives this
status name from source Work Assignment, Work Event, Deliverable
Production, Milestone Acceptance, and Completion Records" — not any
specific status string. The status string travels on
:attr:`ExecutionStatusProjection.projected_status`.
"""


EXECUTION_PROJECTION_DEFINITION_VERSION: Final[str] = "2026.01"
"""Version of the Execution_Service projection.

Follows the year-month string convention already used by the Slice 1
Trail and Slice 2 Planning Projection Definitions. A breaking change
to the computation (a status string is renamed, a new source Record
kind is consulted, the precedence rules change) bumps this literal.
"""


EXECUTION_PROJECTION_DEFINITION: Final[ProjectionDefinition] = (
    ProjectionDefinition(
        name=EXECUTION_PROJECTION_DEFINITION_NAME,
        version=EXECUTION_PROJECTION_DEFINITION_VERSION,
    )
)
"""Convenience instance for the Execution_Service Projection Definition.

Composition (task 15.3) registers this instance with the singleton
:class:`StatusProjector` so producer call sites can pass the bare
name and the projector resolves the full
:class:`ProjectionDefinition` from its registry.
"""


# ---------------------------------------------------------------------------
# Status string constants (Requirement 39.1, design §"Execution-status
# Projection").
#
# Sourced verbatim from design.md §"Execution-status Projection" and
# tasks.md §13.1: ``Plan Revision approved``, ``Plan Revision in
# execution``, ``Plan Revision execution paused``, ``Plan Revision
# deliverable produced``, ``Plan Revision milestone accepted``,
# ``Plan Revision completion recorded``, ``Provenance incomplete``.
# The exact wording (capitalization, spacing) is preserved so the test
# surface and the design document refer to the same string.
# ---------------------------------------------------------------------------


EXECUTION_STATUS_APPROVED: Final[str] = "Plan Revision approved"
"""Baseline projected status: the target Plan Revision is approved but
no Work Assignment Record targets it yet, or every Work Assignment has
zero Work Event Records. Source: a SELECT against
``Work_Assignment_Records`` and ``Work_Event_Records``."""


EXECUTION_STATUS_IN_EXECUTION: Final[str] = "Plan Revision in execution"
"""Projected status surfaced when at least one Work Event Record
exists for a Work Assignment targeting the Plan Revision and no later
Deliverable Production / Milestone Acceptance / Completion Record has
been observed. The status is the design's fall-back per step 7."""


EXECUTION_STATUS_EXECUTION_PAUSED: Final[str] = "Plan Revision execution paused"
"""Projected status surfaced when every Work Assignment targeting the
Plan Revision has at least one Work Event Record AND the most-recent
Work Event on every Work Assignment is ``paused``. Design step 7:
*"the most-recent event on every Work Assignment is `paused`"*."""


EXECUTION_STATUS_DELIVERABLE_PRODUCED: Final[str] = "Plan Revision deliverable produced"
"""Projected status surfaced when at least one Deliverable Production
Record is sourced from a Work Assignment targeting the Plan Revision
(design step 4). Implies execution has progressed past the
in-execution state."""


EXECUTION_STATUS_MILESTONE_ACCEPTED: Final[str] = "Plan Revision milestone accepted"
"""Projected status surfaced when at least one Milestone Acceptance
Record with ``outcome = 'Accept'`` targets a Deliverable Production
sourced from a Work Assignment targeting the Plan Revision (design
step 5). Implies a Deliverable Production was also produced."""


EXECUTION_STATUS_COMPLETION_RECORDED: Final[str] = "Plan Revision completion recorded"
"""Projected status surfaced when a Completion Record targets the
Plan Revision (design step 6). The terminal projected status."""


EXECUTION_STATUS_PROVENANCE_INCOMPLETE: Final[str] = "Provenance incomplete"
"""Projected status surfaced when a required source Record (the Plan
Revision itself, or the lifecycle-state precondition) is unresolvable.
Per Requirement 39.5 the projection withholds the most-progressed
status and returns the explanation-unavailable indicator naming the
missing element."""


# Closed enumeration of every status string this module surfaces.
# ``Literal`` constrains :attr:`ExecutionStatusProjection.projected_status`
# at the type level so a typo on the producer side is caught at static
# analysis time. The values are exactly the design.md ``ExecutionStatusProjection``
# Literal members.
ExecutionProjectedStatus = Literal[
    "Plan Revision approved",
    "Plan Revision in execution",
    "Plan Revision execution paused",
    "Plan Revision deliverable produced",
    "Plan Revision milestone accepted",
    "Plan Revision completion recorded",
    "Provenance incomplete",
]


EXECUTION_PROJECTED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        EXECUTION_STATUS_APPROVED,
        EXECUTION_STATUS_IN_EXECUTION,
        EXECUTION_STATUS_EXECUTION_PAUSED,
        EXECUTION_STATUS_DELIVERABLE_PRODUCED,
        EXECUTION_STATUS_MILESTONE_ACCEPTED,
        EXECUTION_STATUS_COMPLETION_RECORDED,
        EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
    }
)
"""Set of every known execution-status projected status string.

Exported so tests and future producers can iterate over the known
statuses without re-spelling the string literals.
"""


# ---------------------------------------------------------------------------
# Reason-code constants for the explanation-unavailable indicator
# (Requirement 39.5).
#
# Used as the ``missing_element_kind`` for the missing-source-Record
# path. The Slice 1 :class:`ExplanationUnavailableResponse` constrains
# the kind to ``"projection_definition" | "source_revision"``; we use
# ``"source_revision"`` for the missing Plan Revision case (the Plan
# Revision *is* its only Revision per Slice 2) and use
# ``"projection_definition"`` when the Slice 3 Projection Definition is
# not registered on the projector.
# ---------------------------------------------------------------------------


_MISSING_KIND_SOURCE_REVISION: Final[str] = "source_revision"


# ---------------------------------------------------------------------------
# Value object: ExecutionStatusProjection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionStatusProjection:
    """Projected execution status paired with its
    :class:`ProjectionEnvelope` (Requirement 39.1, 39.2).

    Returned by :func:`project_execution_status` on the happy path
    (every source Record resolved, Projection Definition registered)
    and on the missing-source-Record withholding path
    (:attr:`projected_status` is ``"Provenance incomplete"`` and
    :attr:`explanation_unavailable` carries the indicator naming the
    unresolvable element).

    Per Requirement 39.2 the envelope's :attr:`derivation` indicator
    distinguishes the projection from authoritative source Records
    (Principle 5.23 — events vs. projections). The class deliberately
    does not carry any percent-complete, actual-cost, remaining-work,
    budget-variance, forecast-cost, or outcome-attainment field
    (Requirement 39.3 — those values are reserved for later slices and
    SHALL NOT be surfaced by Slice 3). Per Requirement 39.6 the class
    carries no Observed Outcome, Measurement, success-condition
    assessment, or attribution evidence reference; the projected
    status is a *projection of work performed*, never evidence of
    outcome.

    Frozen because every Slice 3 value object that crosses a module
    boundary must be byte-stable while the in-flight read completes;
    the same convention is used by :class:`CreatePlanRevisionResult`
    and :class:`PlanRevisionRow` in :mod:`walking_slice.planning.plan_revisions`.

    Attributes:
        plan_revision_id: The target Plan Revision Identity. Echoed
            on every projection (including the
            ``"Provenance incomplete"`` withholding path) so the
            caller can correlate the response with the request
            without re-binding the identifier.
        projected_status: One of the seven literal strings declared
            by :data:`ExecutionProjectedStatus`. The
            ``"Provenance incomplete"`` value is paired with a
            non-``None`` :attr:`explanation_unavailable` indicator
            identifying the missing element per Requirement 39.5.
        envelope: The :class:`ProjectionEnvelope` carrying the
            Projection Definition, source Resource Identities, source
            Revision Identities, applicable temporal boundary, and
            generated time, all per Requirement 39.1. The envelope's
            derivation indicator is fixed at ``"derived"`` per
            Requirement 39.2.
        explanation_unavailable: ``None`` on the happy path; an
            :class:`ExplanationUnavailableResponse` identifying the
            missing element on the missing-source-Record withholding
            path. Per Requirement 39.5 the indicator names the
            unresolvable element so the caller can re-issue the
            request once the missing source is recorded.
    """

    plan_revision_id: str
    projected_status: ExecutionProjectedStatus
    envelope: ProjectionEnvelope
    explanation_unavailable: Optional[ExplanationUnavailableResponse] = None


# Type alias mirroring the producer-facing return type of
# :func:`project_execution_status`. The HTTP layer (task 15.1) and the
# unit tests (task 13.2) discriminate on the runtime type to render or
# assert against the appropriate response shape:
#
# - :class:`ExecutionStatusProjection` — the Projection Definition was
#   resolved and the envelope was built. The projection may still
#   carry the ``"Provenance incomplete"`` status when a source Record
#   was unresolvable (Requirement 39.5 — withholding path B).
#
# - :class:`ExplanationUnavailableResponse` — the Projection Definition
#   itself was not registered on the projector, so no envelope could
#   be built (Requirement 39.5 — withholding path A).
ExecutionStatusResponse = Union[
    ExecutionStatusProjection, ExplanationUnavailableResponse
]


# ---------------------------------------------------------------------------
# Public entry point — registry for composition (task 15.3).
# ---------------------------------------------------------------------------


def execution_projection_registry() -> dict[str, ProjectionDefinition]:
    """Return the Execution_Service Projection Definition registry.

    Production composition (task 15.3) merges this dict with the
    Slice 1 Trail registry and the Slice 2 Planning registry, then
    hands the union to a single :class:`StatusProjector` instance.
    Returning a fresh ``dict`` on every call lets callers mutate the
    result without affecting other callers (the :class:`StatusProjector`
    copies its registry on construction).

    The function is small enough to inline at the composition site;
    it lives here so the *single* place that names the Slice 3
    Projection Definition is :data:`EXECUTION_PROJECTION_DEFINITION`
    above.
    """
    return {
        EXECUTION_PROJECTION_DEFINITION_NAME: EXECUTION_PROJECTION_DEFINITION,
    }


# ---------------------------------------------------------------------------
# Internal helpers — read-only SELECTs against Slice 3 tables.
#
# Every query below is a read-only SELECT against the Slice 3 tables
# whose schema is defined in :mod:`walking_slice.execution._persistence`.
# Indexes consulted:
#
# - ``idx_work_assignments_by_plan`` — covers the Work Assignment
#   lookup by ``target_plan_revision_id``.
# - ``idx_work_events_by_wa_recent`` — covers the most-recent Work
#   Event lookup per Work Assignment.
# - ``idx_deliverable_productions_by_wa`` — covers the Deliverable
#   Production lookup by ``source_work_assignment_id``.
# - the implicit UNIQUE index on
#   ``Milestone_Acceptance_Records.source_deliverable_production_id``
#   — covers the Milestone Acceptance lookup.
# - the implicit UNIQUE index on
#   ``Completion_Records.target_plan_revision_id`` — covers the
#   Completion lookup.
#
# Each helper returns plain Python value types (tuples of UUID strings,
# dicts, or ``None``) so the orchestration logic in
# :func:`project_execution_status` is straightforward and unit-testable
# against an in-memory SQLite database.
# ---------------------------------------------------------------------------


def _load_work_assignment_ids(
    connection: Connection, plan_revision_id: str
) -> tuple[str, ...]:
    """Return every ``work_assignment_id`` targeting ``plan_revision_id``.

    Hits ``idx_work_assignments_by_plan``. Returns an empty tuple when
    no Work Assignment Record targets the Plan Revision (design step 2
    — *"If zero, return Plan Revision approved"*).
    """
    rows = connection.execute(
        text(
            "SELECT work_assignment_id "
            "FROM Work_Assignment_Records "
            "WHERE target_plan_revision_id = :plan_revision_id "
            "ORDER BY recorded_at ASC, work_assignment_id ASC"
        ),
        {"plan_revision_id": plan_revision_id},
    ).all()
    return tuple(row[0] for row in rows)


def _load_most_recent_event_per_wa(
    connection: Connection, work_assignment_ids: tuple[str, ...]
) -> dict[str, tuple[str, str]]:
    """Return the most recent ``(work_event_id, event_kind)`` per Work
    Assignment.

    The map is keyed by ``work_assignment_id``; Work Assignments with
    zero Work Event Records are absent from the result. Hits
    ``idx_work_events_by_wa_recent`` which is keyed
    ``(target_work_assignment_id, recorded_at DESC, event_kind)`` so
    the per-Work-Assignment lookup is index-only.

    The ``MAX(recorded_at)`` correlated subquery is the idiomatic
    SQLite expression of *"most recent event per Work Assignment"*;
    every Slice 3 timestamp is stored as an ISO-8601 millisecond
    string so lexicographic ``MAX`` matches chronological ``MAX``.
    Ties on ``recorded_at`` are broken by ``work_event_id`` (UUIDv7,
    monotonically increasing) so the result is deterministic across
    repeated invocations even when two events landed in the same
    millisecond (Requirement 39.4 / Property 7 — byte-equivalent
    retrieval).
    """
    if not work_assignment_ids:
        return {}
    # Use a parameterised IN list. SQLAlchemy's text() does not expand
    # bind parameter lists, so we build the bind list explicitly. The
    # number of Work Assignments per Plan Revision is bounded by the
    # per-plan working set; production volumes are well under SQLite's
    # default variable-binding limit (999 per statement).
    placeholders = ", ".join(f":wa_{i}" for i in range(len(work_assignment_ids)))
    params: dict[str, str] = {
        f"wa_{i}": wa_id for i, wa_id in enumerate(work_assignment_ids)
    }
    rows = connection.execute(
        text(
            "SELECT we.target_work_assignment_id, we.work_event_id, we.event_kind "
            "FROM Work_Event_Records AS we "
            f"WHERE we.target_work_assignment_id IN ({placeholders}) "
            "AND we.recorded_at = ("
            "  SELECT MAX(inner_we.recorded_at) "
            "  FROM Work_Event_Records AS inner_we "
            "  WHERE inner_we.target_work_assignment_id = we.target_work_assignment_id"
            ") "
            "ORDER BY we.target_work_assignment_id ASC, we.work_event_id ASC"
        ),
        params,
    ).all()
    # When two events share the same recorded_at (millisecond
    # collision), the SELECT above returns both rows for the same
    # Work Assignment. The ORDER BY trailing on ``work_event_id``
    # makes the *last* row deterministic; building the dict via
    # assignment lets the final row win. UUIDv7 strings sort
    # lexicographically by encoded time, so the latest event by
    # ULID-style ordering is the winner.
    most_recent: dict[str, tuple[str, str]] = {}
    for wa_id, work_event_id, event_kind in rows:
        most_recent[wa_id] = (work_event_id, event_kind)
    return most_recent


def _load_deliverable_production_ids(
    connection: Connection, work_assignment_ids: tuple[str, ...]
) -> tuple[str, ...]:
    """Return every ``deliverable_production_id`` sourced from one of
    the supplied Work Assignments.

    Hits ``idx_deliverable_productions_by_wa``. Returns an empty tuple
    when no Production Record is sourced from any of the Work
    Assignments (design step 4 — Deliverable Production absent means
    the projection does not mark ``"deliverable produced"``).
    """
    if not work_assignment_ids:
        return ()
    placeholders = ", ".join(f":wa_{i}" for i in range(len(work_assignment_ids)))
    params: dict[str, str] = {
        f"wa_{i}": wa_id for i, wa_id in enumerate(work_assignment_ids)
    }
    rows = connection.execute(
        text(
            "SELECT deliverable_production_id "
            "FROM Deliverable_Production_Records "
            f"WHERE source_work_assignment_id IN ({placeholders}) "
            "ORDER BY recorded_at ASC, deliverable_production_id ASC"
        ),
        params,
    ).all()
    return tuple(row[0] for row in rows)


def _load_accept_milestone_acceptance_ids(
    connection: Connection,
    deliverable_production_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Return every Milestone Acceptance Identity targeting one of the
    supplied Deliverable Productions with ``outcome = 'Accept'``.

    Hits the implicit UNIQUE index on
    ``Milestone_Acceptance_Records.source_deliverable_production_id``.
    Design step 5: *"Mark `Plan Revision milestone accepted` when ≥ 1
    exists"* — only Accept-outcome Milestone Acceptances qualify.
    """
    if not deliverable_production_ids:
        return ()
    placeholders = ", ".join(
        f":dp_{i}" for i in range(len(deliverable_production_ids))
    )
    params: dict[str, str] = {
        f"dp_{i}": dp_id for i, dp_id in enumerate(deliverable_production_ids)
    }
    rows = connection.execute(
        text(
            "SELECT milestone_acceptance_id "
            "FROM Milestone_Acceptance_Records "
            f"WHERE source_deliverable_production_id IN ({placeholders}) "
            "AND outcome = 'Accept' "
            "ORDER BY recorded_at ASC, milestone_acceptance_id ASC"
        ),
        params,
    ).all()
    return tuple(row[0] for row in rows)


def _load_completion_id(
    connection: Connection, plan_revision_id: str
) -> Optional[str]:
    """Return the Completion Record Identity targeting the Plan Revision
    when one exists.

    Hits the implicit UNIQUE index on
    ``Completion_Records.target_plan_revision_id``. Per Requirement
    29.3 at most one Completion Record may target a given Plan
    Revision, so the lookup returns at most one row.
    """
    row = connection.execute(
        text(
            "SELECT completion_id "
            "FROM Completion_Records "
            "WHERE target_plan_revision_id = :plan_revision_id"
        ),
        {"plan_revision_id": plan_revision_id},
    ).one_or_none()
    if row is None:
        return None
    return row[0]


# ---------------------------------------------------------------------------
# Internal helpers — projected status derivation.
# ---------------------------------------------------------------------------


def _derive_projected_status(
    *,
    work_assignment_ids: tuple[str, ...],
    most_recent_event_per_wa: dict[str, tuple[str, str]],
    deliverable_production_ids: tuple[str, ...],
    accept_milestone_ids: tuple[str, ...],
    completion_id: Optional[str],
) -> ExecutionProjectedStatus:
    """Derive the projected status per design §"Execution-status Projection"
    steps 2..7.

    The precedence rule is *"most-progressed label observed"*: the
    first matching condition (scanned from the terminal state
    backward) wins. The order below is exactly the design's:

    1. Completion Record present → ``EXECUTION_STATUS_COMPLETION_RECORDED``
       (design step 6).
    2. Accept-outcome Milestone Acceptance present →
       ``EXECUTION_STATUS_MILESTONE_ACCEPTED`` (design step 5).
    3. Deliverable Production present →
       ``EXECUTION_STATUS_DELIVERABLE_PRODUCED`` (design step 4).
    4. At least one Work Event recorded:
       a. Every Work Assignment's most-recent event is ``paused`` and
          every Work Assignment has at least one Work Event →
          ``EXECUTION_STATUS_EXECUTION_PAUSED`` (design step 7
          *"the most-recent event on every Work Assignment is
          `paused`"*).
       b. Otherwise → ``EXECUTION_STATUS_IN_EXECUTION`` (design step 7
          fall-back).
    5. No Work Assignment OR no Work Event observed →
       ``EXECUTION_STATUS_APPROVED`` (design step 2 — *"If zero,
       return Plan Revision approved"*; extended to cover the case
       where Work Assignments exist but execution has not begun).

    The function does not surface ``"Provenance incomplete"``; that
    status is set by the calling
    :func:`project_execution_status` on the missing-source-Record
    withholding path.
    """
    # Step 1 — most progressed: Completion Record.
    if completion_id is not None:
        return EXECUTION_STATUS_COMPLETION_RECORDED

    # Step 2 — next-most progressed: Accept-outcome Milestone
    # Acceptance. Per Requirement 29.1 a Completion only exists when
    # at least one Accept Milestone exists, so this branch is only
    # reachable when no Completion Record is present.
    if accept_milestone_ids:
        return EXECUTION_STATUS_MILESTONE_ACCEPTED

    # Step 3 — Deliverable Production Record. Per Requirement 28.x a
    # Milestone Acceptance only exists when a Production exists, so
    # this branch is only reachable when no Accept Milestone is
    # present.
    if deliverable_production_ids:
        return EXECUTION_STATUS_DELIVERABLE_PRODUCED

    # Step 4 — Work Event Records exist (execution has begun) but no
    # Production yet. Distinguish paused-on-every-WA from in-execution.
    if most_recent_event_per_wa:
        # Paused requires every Work Assignment to have at least one
        # Work Event AND every most-recent event to be ``paused``.
        # ``len`` comparison covers the "every WA has events" half;
        # the ``all(...)`` covers the "every most-recent event is
        # paused" half.
        every_wa_has_event = (
            len(most_recent_event_per_wa) == len(work_assignment_ids)
        )
        every_most_recent_is_paused = all(
            event_kind == "paused"
            for _wa_id, (_we_id, event_kind) in most_recent_event_per_wa.items()
        )
        if every_wa_has_event and every_most_recent_is_paused:
            return EXECUTION_STATUS_EXECUTION_PAUSED
        return EXECUTION_STATUS_IN_EXECUTION

    # Step 5 — baseline: no Work Assignment, or Work Assignments
    # exist but none has any Work Event yet. Plan Revision is
    # approved and execution has not begun.
    return EXECUTION_STATUS_APPROVED


def _to_second_precision(value: datetime) -> datetime:
    """Truncate a UTC datetime to ISO-8601 second precision.

    The :class:`ProjectionEnvelope` validator requires
    ``microsecond == 0`` and a UTC tzinfo. The caller-supplied ``at``
    parameter on :func:`project_execution_status` may carry millisecond
    precision (the slice-wide convention from
    :func:`walking_slice.clock.truncate_to_milliseconds`); this helper
    discards sub-second components so the envelope validator accepts
    the value.

    Naive datetimes are passed through unchanged so the envelope
    validator produces its precise error message rather than this
    helper silently coercing the value.
    """
    if value.tzinfo is None:
        return value
    return value.replace(microsecond=0)


# ---------------------------------------------------------------------------
# Public entry point — project_execution_status.
# ---------------------------------------------------------------------------


def project_execution_status(
    connection: Connection,
    *,
    plan_revision_id: str,
    party_id: str,
    at: datetime,
    status_projector: StatusProjector,
) -> ExecutionStatusResponse:
    """Derive the projected execution status of a Plan Revision and
    wrap it in a :class:`ProjectionEnvelope` (Requirement 39).

    The function implements the seven-step Projection Definition from
    design §"Execution-status Projection":

    1. Resolve the target Plan Revision via the AD-WS-30 read API
       :meth:`PlanRevisionService.get_plan_revision`. Require
       ``lifecycle_state = 'approved'`` (design step 1). When the
       Plan Revision is unresolvable or not approved, mark the
       projection ``"Provenance incomplete"`` and attach the
       :class:`ExplanationUnavailableResponse` indicator naming the
       missing element per Requirement 39.5.
    2. Find every Work Assignment Record targeting the Plan Revision.
    3. For each Work Assignment, load its most-recent Work Event
       Record (design step 3).
    4. Find every Deliverable Production Record sourced from one of
       those Work Assignments (design step 4).
    5. Find every Accept-outcome Milestone Acceptance Record sourced
       from one of those Deliverable Productions (design step 5).
    6. Find the Completion Record (if any) targeting the Plan
       Revision (design step 6).
    7. Compute the most-progressed projected status label per
       :func:`_derive_projected_status` (design step 7).

    The function then wraps the result in a
    :class:`ProjectionEnvelope` carrying the Projection Definition
    (resolved by name against the projector's registry), the source
    Resource Identities (the Plan Revision Identity), the source
    Revision Identities (every Slice 3 Record consulted), the
    applicable temporal boundary (the supplied ``at`` truncated to
    ISO-8601 second precision), and the generated time (stamped by
    the projector's clock at second precision). On unresolvable
    Projection Definition the projector returns the
    :class:`ExplanationUnavailableResponse` directly, identifying
    the Projection Definition as the missing element (Requirement
    39.5).

    Per Requirement 39.3 the response carries only the projected
    status label and the envelope — no percent-complete,
    actual-cost, remaining-work, budget-variance, forecast-cost, or
    outcome-attainment value is computed or returned. Per
    Requirement 39.6 the projected status labels are projections of
    *work performed*, never aliases for Observed Outcome,
    Measurement, success-condition assessment, or attribution
    evidence.

    Per Requirement 39.4 every database access is a read-only
    SELECT; no row of any Slice 1, Slice 2, or Slice 3 table is
    mutated by this function. Per Requirement 40.4 the function
    does not touch any Slice 1 or Slice 2 row at all (the single
    Slice 2 read is the AD-WS-30 ``Plan_Revisions`` lookup that
    Slice 3 is explicitly authorised to perform).

    Args:
        connection: SQLAlchemy connection bound to the caller's
            read context. The lookup participates in the caller's
            transactional view so the projection is computed against
            a consistent snapshot of the Slice 1 / Slice 2 / Slice 3
            tables.
        plan_revision_id: The Plan Revision Identity whose execution
            status is being projected. Passed through to
            :meth:`PlanRevisionService.get_plan_revision` as a bound
            parameter; the AD-WS-30 read API is the sole Slice 2
            entry point this module consults.
        party_id: Identity of the requesting Party. Carried through
            for future authority evaluation (the HTTP route layer in
            task 15.1 will evaluate ``view`` authority on the target
            Plan Revision before invoking this helper). The current
            implementation does not consult ``party_id`` directly;
            disclosure-policy enforcement for restricted Plan
            Revisions lives at the route layer (AD-WS-9
            indistinguishable-denial response).
        at: The applicable temporal boundary of the projection — the
            instant up to which the source Records were considered
            effective. The value is truncated to ISO-8601 second
            precision before passing to the envelope (Requirement
            39.1). The current implementation does not filter source
            Records by ``recorded_at <= at``; every visible Slice 3
            Record participates in the projection regardless of
            recorded time. A later slice may tighten this contract;
            this slice surfaces the boundary verbatim on the
            envelope for explainability.
        status_projector: The Slice 1
            :class:`walking_slice.projection.StatusProjector` whose
            registry contains :data:`EXECUTION_PROJECTION_DEFINITION`.
            Production composition (task 15.3) constructs a single
            projector registered with the Slice 1 Trail, the Slice 2
            Planning, and the Slice 3 Execution Projection Definitions
            and shares it across requests; tests construct ad-hoc
            projectors. Keyword-only to keep call sites explicit.

    Returns:
        On the happy path and the missing-source-Record path: an
        :class:`ExecutionStatusProjection` carrying the derived status
        and the populated envelope. On the missing-source-Record
        path the projected status is
        :data:`EXECUTION_STATUS_PROVENANCE_INCOMPLETE` and
        :attr:`ExecutionStatusProjection.explanation_unavailable`
        names the unresolvable element.

        On the unresolvable-Projection-Definition path: an
        :class:`ExplanationUnavailableResponse` identifying the
        missing Projection Definition by name.
    """
    # Step 0 — short-circuit when the Projection Definition itself is
    # not registered on the projector. Per Requirement 39.5 we cannot
    # build an envelope without a Projection Definition, so the
    # projection is withheld and the indicator names the missing
    # element. Returning early here also avoids running any of the
    # downstream SELECTs on a configuration error path.
    if not status_projector.has_definition(EXECUTION_PROJECTION_DEFINITION_NAME):
        return ExplanationUnavailableResponse(
            missing_element_kind="projection_definition",
            missing_element_identifier=EXECUTION_PROJECTION_DEFINITION_NAME,
        )

    # Normalize the applicable temporal boundary to second precision
    # ahead of every envelope construction below. Doing it once
    # ensures every withholding branch and the happy path stamp the
    # same boundary; building the envelope twice with different
    # boundary values would violate Property 7 (idempotent
    # retrieval).
    applicable_temporal_boundary = _to_second_precision(at)

    # Step 1 — resolve the target Plan Revision via the AD-WS-30 read
    # API. The check runs before any Slice 3 SELECT so the unresolvable
    # path returns immediately without consulting Work Assignment or
    # downstream tables. Per Requirement 40 the read is the sole
    # Slice 2 entry point this module consults.
    plan_revision: Optional[PlanRevisionRow] = (
        PlanRevisionService.get_plan_revision(connection, plan_revision_id)
    )
    if plan_revision is None:
        return _withheld_projection(
            plan_revision_id=plan_revision_id,
            status_projector=status_projector,
            applicable_temporal_boundary=applicable_temporal_boundary,
            missing_element_identifier=plan_revision_id,
        )

    # Per design step 1 the projection only applies to Approved Plan
    # Revisions. A draft Plan Revision is treated as an unresolvable
    # source Record — the projection cannot speak about execution of
    # an unapproved plan. The missing-element identifier carries the
    # Plan Revision Identity so the caller can correlate the
    # withholding response with the request.
    if plan_revision.lifecycle_state != "approved":
        return _withheld_projection(
            plan_revision_id=plan_revision_id,
            status_projector=status_projector,
            applicable_temporal_boundary=applicable_temporal_boundary,
            missing_element_identifier=plan_revision_id,
        )

    # Steps 2..6 — collect every Slice 3 source Record consulted by
    # the projection. Each query is index-backed (see the module
    # docstring for the index names). The ``ORDER BY`` clauses are
    # deterministic so the resulting tuples are byte-equivalent
    # across repeated invocations (Property 7 — idempotent
    # retrieval, Requirement 41.7).
    work_assignment_ids = _load_work_assignment_ids(connection, plan_revision_id)
    most_recent_event_per_wa = _load_most_recent_event_per_wa(
        connection, work_assignment_ids
    )
    deliverable_production_ids = _load_deliverable_production_ids(
        connection, work_assignment_ids
    )
    accept_milestone_ids = _load_accept_milestone_acceptance_ids(
        connection, deliverable_production_ids
    )
    completion_id = _load_completion_id(connection, plan_revision_id)

    # Step 7 — derive the most-progressed projected status label.
    projected_status = _derive_projected_status(
        work_assignment_ids=work_assignment_ids,
        most_recent_event_per_wa=most_recent_event_per_wa,
        deliverable_production_ids=deliverable_production_ids,
        accept_milestone_ids=accept_milestone_ids,
        completion_id=completion_id,
    )

    # Build the envelope through the projector so the generated time
    # is stamped from the projector's clock (truncated to second
    # precision by the projector internally) and the Projection
    # Definition is resolved from the projector's registry. The
    # source Record Identities are split into resource and revision
    # lists per the planning module convention:
    #
    # - The Plan Revision Identity is recorded as the single source
    #   Resource Identity (the Plan Revision is the addressed
    #   Resource for the projection).
    # - Every Slice 3 Record consulted (Work Assignments, Work Events,
    #   Deliverable Productions, Milestone Acceptances, Completion)
    #   is recorded as a source Revision Identity. Slice 3 Records
    #   are Resource-level Immutable Records (Requirement 22.2) — they
    #   carry no separate Revision — but the envelope's
    #   ``source_revision_ids`` list is the producer's expressive
    #   surface for "exact source identities the projection
    #   consulted" so we use it for every consulted Record Identity.
    source_resource_ids = (UUID(plan_revision_id),)
    consulted_record_ids: list[UUID] = []
    for wa_id in work_assignment_ids:
        consulted_record_ids.append(UUID(wa_id))
    for _wa_id, (we_id, _event_kind) in most_recent_event_per_wa.items():
        consulted_record_ids.append(UUID(we_id))
    for dp_id in deliverable_production_ids:
        consulted_record_ids.append(UUID(dp_id))
    for ma_id in accept_milestone_ids:
        consulted_record_ids.append(UUID(ma_id))
    if completion_id is not None:
        consulted_record_ids.append(UUID(completion_id))
    source_revision_ids = tuple(consulted_record_ids)

    response = status_projector.project_status(
        definition_name=EXECUTION_PROJECTION_DEFINITION_NAME,
        status=projected_status,
        source_resource_ids=source_resource_ids,
        source_revision_ids=source_revision_ids,
        applicable_temporal_boundary=applicable_temporal_boundary,
    )

    # The projector returns ``ProjectedStatusResponse`` on the happy
    # path. The Projection-Definition-missing path was already short-
    # circuited above via :meth:`StatusProjector.has_definition`, so
    # the response below is guaranteed to be the wrapped-status shape.
    # We extract the envelope and re-wrap it in
    # :class:`ExecutionStatusProjection` — the Slice 3 surface — so
    # the caller receives a value object whose ``projected_status``
    # is typed as :data:`ExecutionProjectedStatus` rather than the
    # generic ``str`` on :class:`ProjectedStatusResponse`.
    #
    # ``response`` here is a :class:`ProjectedStatusResponse` because
    # the registry check above guarantees the happy path; the
    # ``isinstance`` assertion is defensive against a future change
    # to :class:`StatusProjector` rather than expected to ever fire.
    assert hasattr(response, "envelope"), (
        "StatusProjector returned an unexpected response shape; the "
        "registry check above should have ruled out the explanation-"
        "unavailable path."
    )

    return ExecutionStatusProjection(
        plan_revision_id=plan_revision_id,
        projected_status=projected_status,
        envelope=response.envelope,
        explanation_unavailable=None,
    )


def _withheld_projection(
    *,
    plan_revision_id: str,
    status_projector: StatusProjector,
    applicable_temporal_boundary: datetime,
    missing_element_identifier: str,
) -> ExecutionStatusProjection:
    """Build the ``"Provenance incomplete"`` withholding response.

    Per Requirement 39.5 the projection withholds the most-progressed
    status when a required source Record is unresolvable and returns
    an :class:`ExplanationUnavailableResponse` indicator identifying
    the missing element. The Slice 3 surface keeps the response
    wrapped in :class:`ExecutionStatusProjection` so callers can
    discriminate withholding on a single status string
    (``"Provenance incomplete"``) and follow up via
    :attr:`ExecutionStatusProjection.explanation_unavailable` for the
    indicator.

    The envelope is built through the projector so the generated
    time is stamped from the projector's clock and the source
    identities reflect only the resolvable parts of the request: the
    Plan Revision Identity is recorded as the single source Resource
    Identity (the caller's request, echoed for correlation); the
    source Revision Identities list is empty because no Slice 3
    Record could be consulted on the withholding path.
    """
    # The ``project_status`` happy path requires the Projection
    # Definition to be registered. The caller of this helper has
    # already verified that via
    # :meth:`StatusProjector.has_definition` (the very first guard in
    # :func:`project_execution_status`); the call below therefore
    # always returns a wrapped-status response with our envelope.
    response = status_projector.project_status(
        definition_name=EXECUTION_PROJECTION_DEFINITION_NAME,
        status=EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
        source_resource_ids=(UUID(plan_revision_id),),
        source_revision_ids=(),
        applicable_temporal_boundary=applicable_temporal_boundary,
    )
    explanation = ExplanationUnavailableResponse(
        missing_element_kind=_MISSING_KIND_SOURCE_REVISION,
        missing_element_identifier=missing_element_identifier,
    )
    return ExecutionStatusProjection(
        plan_revision_id=plan_revision_id,
        projected_status=EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
        envelope=response.envelope,
        explanation_unavailable=explanation,
    )
