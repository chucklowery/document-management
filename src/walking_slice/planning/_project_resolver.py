"""Planning_Service.ProjectResolver — additive Slice 3 read-only walker
from a Plan Revision Identity to its owning Project Identity.

Design reference
================

``.kiro/specs/third-walking-slice/design.md``:

- AD-WS-30 — Approved-Plan resolution uses the existing Planning_Service
  read API. Slice 3 services that need to perform the project-membership
  check (Requirement 27.3) call this resolver instead of querying the
  Slice 2 schema directly, keeping the Execution_Service decoupled from
  the Planning_Service's internal tables per Principle 5.2 (Bounded
  contexts preserve meaning) and ``03-context-map.md`` Cross-Context
  Rule 2 ("A context does not mutate another context's authoritative
  record").
- §"Execution_Service.DeliverableProductions" project-membership check
  bullet — ``wa_project_id =
  project_resolver.resolve_project(wa_plan_revision_id)`` is the exact
  call site that consumes this module.
- §"Execution_Service.Completions" — the Completion target Activity
  Plan / Project Identities are resolved via the same resolver.

Task scope (task 2.2 — additive ProjectResolver)
================================================

This module exposes a single immutable :class:`ProjectResolver` value
object with one read-only method
:meth:`ProjectResolver.resolve_project` that walks
``Plan Revision → Activity Plan → Project`` through the existing Slice
2 tables ``Plan_Revisions`` and ``Activity_Plans``. The traversal uses
one indexed ``SELECT`` joining the two tables on
``Activity_Plans.activity_plan_id``; the join order
(``Plan_Revisions`` → ``Activity_Plans``) matches the FK definition in
``walking_slice.planning._persistence`` and uses the
``Plan_Revisions(activity_plan_id, recorded_at)`` index already
created in Slice 2 for the row lookup.

Requirements satisfied
======================

    27.3 — Slice 3 callers that need to check the produced Deliverable
           Expectation belongs to the same Project as the source Work
           Assignment's Approved Plan Revision can compute the Project
           Identity of a Plan Revision through a single Planning_Service
           public-API read.
    27.4 — Provides the structured error path
           (:class:`PlanRevisionNotResolvableError`) the caller surfaces
           when a referenced Plan Revision Identity does not resolve;
           the caller decides whether to map the error to a 400 / 409
           / denial response.
    40.1 — Strictly additive: no Slice 1 or Slice 2 row, table, index,
           trigger, or function is mutated; this module reads only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


__all__ = [
    "PlanRevisionNotResolvableError",
    "ProjectResolver",
]


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


# Single indexed SELECT that follows Plan Revision -> Activity Plan ->
# Project. The query reads only the three columns Slice 3 callers
# need (the Plan Revision's identifier echoed back, the intervening
# Activity Plan Identity, and the owning Project Identity) so the
# resolver does not over-fetch and the result remains cheap to call
# in the project-membership hot path of
# :class:`walking_slice.execution.deliverable_productions.DeliverableProductionService`.
#
# The two-row JOIN walks the Slice 2 FK chain in source order:
#
#     Plan_Revisions(plan_revision_id) ->
#         Activity_Plans(activity_plan_id) ->
#             Projects(project_id)
#
# Both joins use indexed primary-key lookups on the right-hand side.
# SQLite chooses the ``Plan_Revisions`` PK index and then the
# ``Activity_Plans`` PK index automatically; no EXPLAIN-QUERY-PLAN
# tuning is required.
_RESOLVE_PROJECT_SQL: Final[str] = """
    SELECT pr.plan_revision_id   AS plan_revision_id,
           pr.activity_plan_id   AS activity_plan_id,
           ap.target_project_id  AS project_id
      FROM Plan_Revisions   AS pr
      JOIN Activity_Plans   AS ap
        ON ap.activity_plan_id = pr.activity_plan_id
     WHERE pr.plan_revision_id = :plan_revision_id
"""


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class PlanRevisionNotResolvableError(LookupError):
    """Raised when ``plan_revision_id`` does not resolve to a known Plan
    Revision.

    Slice 3 services that consume :meth:`ProjectResolver.resolve_project`
    surface this exception verbatim to their callers (typically rendering
    it as a structured 400 ``unresolvable_plan_revision`` response, or
    folding it into a denial response when AD-WS-9 indistinguishable
    denial applies). The exception carries only the offending identifier
    and a stable ``failed_constraint`` discriminator so route layers can
    branch on the cause without parsing message text.

    The error is also raised when the matching Plan Revision row exists
    but its referenced ``activity_plan_id`` does not appear in
    ``Activity_Plans`` (the JOIN returns zero rows). This should be
    impossible under the Slice 2 FK constraint
    ``Plan_Revisions.activity_plan_id REFERENCES Activity_Plans``, but
    treating both cases identically gives the resolver one well-defined
    failure mode regardless of how the integrity invariant could be
    bypassed (for example by a forced ``PRAGMA foreign_keys = OFF`` from
    outside the application).

    Attributes:
        plan_revision_id: The Plan Revision Identity that did not
            resolve (echoed back from the caller's argument so the
            response can name it).
        failed_constraint: Stable discriminator. Always
            ``"plan_revision_not_resolvable"`` for this exception
            class.
    """

    def __init__(
        self,
        *,
        plan_revision_id: str,
        failed_constraint: str = "plan_revision_not_resolvable",
    ) -> None:
        super().__init__(
            f"Plan Revision {plan_revision_id!r} did not resolve to an "
            f"existing Plan Revision with a known parent Activity Plan "
            f"and Project (failed_constraint={failed_constraint!r})."
        )
        self.plan_revision_id = plan_revision_id
        self.failed_constraint = failed_constraint


# ---------------------------------------------------------------------------
# Resolver.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectResolver:
    """Walks ``Plan Revision -> Activity Plan -> Project`` to compute
    the Project Identity that owns a given Plan Revision.

    The resolver is a thin, stateless adapter over the existing Slice 2
    schema. It exposes one method, :meth:`resolve_project`, and holds
    no per-request collaborators because the traversal is a pure
    indexed read against the caller-supplied connection. Slice 3
    services receive an instance via constructor injection (matching
    the dataclass-typed dependencies declared in
    ``.kiro/specs/third-walking-slice/design.md``
    §"Execution_Service.DeliverableProductions" and
    §"Execution_Service.Completions"); tests can substitute a stub by
    instantiating the same dataclass.

    Frozen because Slice 3 services depend on this resolver through a
    frozen dataclass field — assigning a fresh resolver per request
    is the explicit replacement path, not mutating fields in place.
    """

    # -- public surface ----------------------------------------------------

    def resolve_project(
        self,
        connection: Connection,
        *,
        plan_revision_id: str,
    ) -> str:
        """Return the Project Identity that owns ``plan_revision_id``.

        Executes one indexed ``SELECT`` joining ``Plan_Revisions`` and
        ``Activity_Plans`` through the Slice 2 FK chain
        ``Plan_Revisions.activity_plan_id`` →
        ``Activity_Plans.activity_plan_id`` →
        ``Activity_Plans.target_project_id``. The read is read-only and
        runs inside the caller's transaction so the resolver shares the
        caller's isolation view (Requirement 40.1 — no Slice 2 row is
        mutated; design AD-WS-30 — Slice 3 reads through Planning_Service
        public APIs).

        Args:
            connection: SQLAlchemy connection bound to the caller's
                transaction. Used for one read-only ``SELECT`` against
                the joined ``Plan_Revisions`` × ``Activity_Plans``
                view; the connection is not closed and no write is
                issued.
            plan_revision_id: Identity of the Plan Revision to walk.

        Returns:
            The Project Identity (the value of
            ``Activity_Plans.target_project_id`` on the row reached
            through the join). Returned as a plain ``str`` to match
            the rest of the Slice 2 / Slice 3 identifier surface,
            which carries identifiers as UUIDv7 strings rather than
            typed value objects.

        Raises:
            PlanRevisionNotResolvableError: When the ``plan_revision_id``
                does not match any row in ``Plan_Revisions``, or when
                the matching row's ``activity_plan_id`` does not appear
                in ``Activity_Plans`` (the join returns zero rows).
                The exception carries the offending identifier and a
                stable ``failed_constraint`` discriminator so route
                layers can branch without parsing message text.
        """
        row = connection.execute(
            text(_RESOLVE_PROJECT_SQL),
            {"plan_revision_id": plan_revision_id},
        ).mappings().first()

        if row is None:
            raise PlanRevisionNotResolvableError(
                plan_revision_id=plan_revision_id,
            )

        project_id: Optional[str] = row["project_id"]
        # The Slice 2 schema declares ``Activity_Plans.target_project_id``
        # as NOT NULL with a foreign key into ``Projects``. A row
        # returned by the JOIN therefore always carries a non-NULL
        # ``project_id``. Treat a None defensively the same as the
        # JOIN-miss case so the resolver has exactly one failure mode
        # regardless of how the invariant could be bypassed.
        if project_id is None:
            raise PlanRevisionNotResolvableError(
                plan_revision_id=plan_revision_id,
            )

        return project_id
