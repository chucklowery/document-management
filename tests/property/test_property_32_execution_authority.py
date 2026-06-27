# Feature: third-walking-slice, Property 32: Execution-Record authority correctness and non-substitution
"""Property 32 — Execution-Record authority correctness and non-substitution (task 16.2).

**Property 32: Execution-Record authority correctness and non-substitution**

For all persisted Slice 3 Records and produced Deliverable Revisions
created by the Execution_Service and Deliverable_Repository, the
recording Party held an effective Role Assignment at the recorded time
whose granted authorities include the precise authority required by
the action — ``assign`` for Work Assignment, ``contribute`` for Work
Event / Time Entry / produced Deliverable / Deliverable Production
(and the Party is also the named assignee on the referenced Work
Assignment Record at the recorded time), ``accept_milestone`` for
Milestone Acceptance, ``complete`` for Completion — whose scope
covers the target Resource's applicable scope, and whose effective
period encloses the recorded time. The eight authority types
``{view, modify, review, approve, assign, contribute,
accept_milestone, complete}`` are pairwise distinct in the evaluator;
no persisted execution Record exists whose recording Party held only
any non-matching authority among the eight. Contributor writes
additionally require the recording Party to be the named assignee on
the referenced Work Assignment Record (AD-WS-29).

**Validates: Requirements 23.6, 24.5, 25.4, 26.6, 27.5, 28.5, 29.5,
30.3, 32.2, 32.3, 32.4, 32.5, 32.6, 32.7, 32.8, 32.9, 32.10, 32.11,
41.2, 41.3**

Strategy
========

Each Hypothesis case draws a *scenario* containing:

- a set of Parties (1..3 candidate recording Parties plus three fixed
  prerequisite Parties — the Assignment Authority, the Assigning
  Authority, and the Approving / Pre-seeded Authority);
- for each candidate Party, a list of 0..3 Role Assignments whose
  dimensions vary independently along the five gating axes called out
  by the task description — ``effective_start`` offset,
  ``effective_end`` offset (or ``None``), revocation offset (or
  ``None``), ``scope`` drawn from a small alphabet that includes the
  wildcard ``"*"``, and a non-empty subset of granted authorities
  drawn from the eight-value enumeration ``{view, modify, review,
  approve, assign, contribute, accept_milestone, complete}``
  (AD-WS-24 / Requirement 32.1);
- 1..6 *attempts*, each picking one of the seven Slice 3 action
  kinds (Work Assignment, Work Event, Time Entry, Produced
  Deliverable, Deliverable Production, Milestone Acceptance,
  Completion), a Party index, and a ``target_scope`` drawn from
  ``{scope-a, scope-b, scope-c}`` used as the request's
  ``applicable_scope`` (and the ``target.scope`` passed to
  :meth:`AuthorizationService.evaluate`).

Per case the test spins up a fresh per-test SQLite engine carrying
Slice 1 + Slice 2 + Slice 3 schemas, a shared
:class:`~walking_slice.clock.FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` (so every artifact in the case carries
the same recorded time, which keeps the assertion deterministic
across shrinks), and the full authorization-wired Execution_Service
and Deliverable_Repository stack. It then:

1. Seeds every Party — the candidate recording Parties plus the
   prerequisite Parties — so every FK target resolves.
2. Persists every drawn Role Assignment via
   :meth:`AuthorizationService.assign_role`; assignments whose drawn
   parameters violate Requirement 12.6 (inverted effective period)
   are skipped at the strategy boundary rather than persisted.
3. Stamps ``revoked_at`` directly via UPDATE for any assignment
   whose strategy drew a revocation offset; the
   ``Role_Assignments_revoked_at_one_shot`` trigger guarantees the
   one-shot semantic regardless.
4. Seeds the prerequisite chain — Project, Activity Plan, and
   approved Plan Revisions sufficient for every attempt — and, for
   each candidate Party / scope combination, one Work Assignment
   Record whose ``assignee_party_id`` is the candidate Party. The
   Contributor-class action attempts use these pre-seeded Work
   Assignments so the AD-WS-29 second-stage assignee-binding check
   can pass when the candidate Party also holds ``contribute``
   authority.
5. Pre-seeds Deliverable Production Records and Accept-outcome
   Milestone Acceptance Records sufficient for every Milestone
   Acceptance and Completion attempt.
6. Attempts every drawn artifact creation through the wired
   Execution_Service / Deliverable_Repository service. Each attempt
   either persists the artifact (the wired
   :class:`AuthorizationService` permits the action and AD-WS-29
   passes when applicable) or raises a tolerated rejection
   (authorization deny, assignee-binding mismatch, validation
   error, state-machine violation, uniqueness conflict); both
   outcomes are accepted by the property — the assertion runs over
   the rows that *did* land.

After every attempt is processed the test queries each of the seven
Slice 3 tables that carry a recording Party Identity, and for every
persisted row written by the wired services in this case, scans
``Role_Assignments`` directly for a row that simultaneously:

- belongs to the artifact's recording Party (the
  ``assignment_authority_party_id`` for Work Assignment Records, the
  ``recording_party_id`` for Work Event / Time Entry / Deliverable
  Production Records, the ``authoring_party_id`` for Deliverable
  Revisions, the ``accepting_party_id`` for Milestone Acceptance
  Records, the ``completing_party_id`` for Completion Records);
- carries the *precise* required authority (``assign`` for Work
  Assignment, ``contribute`` for the four Contributor-class actions,
  ``accept_milestone`` for Milestone Acceptance, ``complete`` for
  Completion) in ``authorities_granted`` — substitution between any
  of the eight authority types is forbidden by Requirement 32.1 /
  32.10 / 32.11;
- covers ``applicable_scope`` (either ``"*"`` or an exact match);
- has ``effective_start <= recorded_at`` (not-yet-effective is not
  violated);
- has ``effective_end IS NULL`` *or* ``effective_end > recorded_at``
  (not expired);
- has ``revoked_at IS NULL`` *or* ``revoked_at > recorded_at`` (not
  revoked).

In addition, for every persisted Contributor-class row (Work Event,
Time Entry, Deliverable Production, Deliverable Revision), the test
re-reads the referenced Work Assignment Record and asserts the
``assignee_party_id`` equals the recording Party — the AD-WS-29
assignee-binding invariant.

The predicate is the same one the
:class:`~walking_slice.authorization.AuthorizationService` itself
applies; the property is therefore a *post-hoc* end-to-end check
that the Execution_Service and Deliverable_Repository never persist
an artifact when no such Role Assignment exists *or* when the
AD-WS-29 assignee-binding contract is violated.

Requirement coverage notes
==========================

- **23.6** — no Work Assignment Record exists whose
  ``assignment_authority_party_id`` lacks an effective ``assign``
  authority at the recorded time.
- **24.5, 25.4, 26.6, 27.5** — no Work Event / Time Entry /
  produced Deliverable Revision / Deliverable Production Record
  exists whose recording Party lacks an effective ``contribute``
  authority *or* fails the AD-WS-29 assignee-binding check.
- **28.5** — no Milestone Acceptance Record exists whose
  ``accepting_party_id`` lacks an effective ``accept_milestone``
  authority.
- **29.5** — no Completion Record exists whose
  ``completing_party_id`` lacks an effective ``complete``
  authority.
- **30.3** — denial responses do not leak; the property merely
  asserts that successful persistence implies authority. Denials
  return tolerated rejections (PermissionError / LookupError /
  ValueError) and the test accepts them without asserting on the
  denial body.
- **32.2 .. 32.9** — every action carries the precise required
  authority and is not interchangeable with any other authority
  type; the matching predicate enforces ``required ∈ granted``
  strictly.
- **32.10, 32.11** — the eight authority types are pairwise
  distinct; a Party holding only any other authority among the
  eight cannot satisfy any Slice 3 write.
- **41.2, 41.3** — every consequential write checks authority
  before persisting any domain row; the assignee-binding contract
  for Contributor writes is enforced before persistence.

Test scaffolding follows the conventions of
``tests/property/test_property_17_planning_resource_authority.py``:
a :class:`tempfile.TemporaryDirectory` owns the per-case SQLite file
(so state cannot leak between Hypothesis cases the way a
function-scoped pytest fixture would), and pragma-aware engine setup
matches the conftest fixtures exactly.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceService,
)
from walking_slice.execution.time_entries import TimeEntryService
from walking_slice.execution.work_assignments import WorkAssignmentService
from walking_slice.execution.work_events import WorkEventService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants — recorded-time anchor, Party identifiers, and the
# seven action kinds with their precise required authorities.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

# Recording-Party UUIDv7 template; each draw produces a stable Identity
# of the form ``..a0NNN`` so shrinkage diagnostics are easy to read.
_PARTY_BASE: Final[str] = "00000000-0000-7000-8000-0000000a0"

# Prerequisite Party Identities. These are seeded once per case and
# carry every FK that must resolve before a Slice 3 service can run:
# the Assigning-Authority (recorded on every drawn Role Assignment),
# the Assignment-Authority (recorded on every pre-seeded Work
# Assignment), and the Approving Party (recorded on the pre-seeded
# Plan Revision rows).
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000b0001"
_ASSIGNMENT_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000b0002"
_APPROVING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000b0003"

# Authority-basis identifier shared by every attempted write. The
# Slice 3 services destructure :class:`AuthorityBasisRef` into the
# ``authority_basis_type`` / ``authority_basis_id`` columns; a single
# fixed basis is sufficient because the property under test is
# orthogonal to AD-WS-10 enumeration validation.
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-0000000c0001"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Prerequisite chain — one Project + one Activity Plan + one
# Deliverable Expectation are shared across every attempt; multiple
# Plan Revisions are pre-allocated so a Completion attempt always
# finds a fresh approved Plan Revision (Requirement 29.3 UNIQUE
# constraint on ``Completion_Records.target_plan_revision_id``).
_PREREQ_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000d0001"
_PREREQ_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000d0002"
_PREREQ_DELIVERABLE_EXPECTATION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000d0003"
)
_PREREQ_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000d0004"
)

# Plan Revision pool templates. The Work Assignment / Work Event /
# Time Entry / Deliverable Production / Milestone Acceptance attempts
# all share one default approved Plan Revision (no per-attempt
# uniqueness constraint binds them); Completion attempts each consume
# a fresh approved Plan Revision from a per-attempt pool.
_DEFAULT_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000e0001"
_COMPLETION_PLAN_REVISION_TEMPLATE: Final[str] = (
    "00000000-0000-7000-8000-00000e2{0:05x}"
)
_COMPLETION_PRODUCTION_TEMPLATE: Final[str] = (
    "00000000-0000-7000-8000-00000e3{0:05x}"
)
_COMPLETION_MILESTONE_TEMPLATE: Final[str] = (
    "00000000-0000-7000-8000-00000e4{0:05x}"
)

# Milestone-Acceptance attempts consume a fresh Deliverable Production
# Record from a per-attempt pool (Requirement 28.3 UNIQUE constraint).
_MA_PRODUCTION_TEMPLATE: Final[str] = (
    "00000000-0000-7000-8000-00000f1{0:05x}"
)

# Work Assignments pre-seeded per (Party-index, scope) so Contributor
# writes can find a Work Assignment whose ``assignee_party_id`` is the
# recording Party. The deliverable-production pool also uses these.
_BOUND_WA_TEMPLATE: Final[str] = "00000000-0000-7000-8000-00000a1{0:05x}"

# Default Deliverable Resource + Revision shared by Work Event /
# Time Entry / Milestone Acceptance attempts when they need a
# pre-existing produced Deliverable.
_DEFAULT_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-0000000f0001"
_DEFAULT_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000f0002"
)

# Scope alphabet. The wildcard ``"*"`` exercises the wildcard branch
# of :meth:`AuthorizationService._scope_covers`; the three discrete
# scope identifiers exercise the equality branch and the
# scope-mismatch axis.
_SCOPES: Final[tuple[str, ...]] = ("scope-a", "scope-b", "scope-c")
_ROLE_SCOPES: Final[tuple[str, ...]] = _SCOPES + ("*",)

# The eight-value authority enumeration after the Slice 3 / AD-WS-24
# additive extension (Requirement 32.1). Drawn as subsets so the
# strategy can exercise the "authority does not include the required
# value" axis (Requirement 32.10 / 32.11 non-substitution) without
# substituting one authority for another.
_AUTHORITIES: Final[tuple[str, ...]] = (
    "view",
    "modify",
    "review",
    "approve",
    "assign",
    "contribute",
    "accept_milestone",
    "complete",
)

# Seven Slice 3 action kinds. The property tests all seven in one
# case so the non-substitution rule across the four new authority
# types ``{assign, contribute, accept_milestone, complete}`` plus the
# four prior types ``{view, modify, review, approve}`` is exercised
# in one place.
_ACTION_KINDS: Final[tuple[str, ...]] = (
    "work_assignment",
    "work_event",
    "time_entry",
    "produced_deliverable",
    "deliverable_production",
    "milestone_acceptance",
    "completion",
)

# Per-action required authority per Requirement 32.6 .. 32.9 /
# AD-WS-24. The property's headline assertion is membership of *this*
# value in the role's ``authorities_granted`` set; substitution
# between any of the eight values is forbidden by Requirement
# 32.10 / 32.11.
_REQUIRED_AUTHORITY: Final[dict[str, str]] = {
    "work_assignment": "assign",
    "work_event": "contribute",
    "time_entry": "contribute",
    "produced_deliverable": "contribute",
    "deliverable_production": "contribute",
    "milestone_acceptance": "accept_milestone",
    "completion": "complete",
}

# Action kinds that additionally require AD-WS-29 assignee binding —
# the recording Party must be the named assignee on the referenced
# Work Assignment Record at the recorded time. These are the four
# Contributor-class actions per AD-WS-29.
_CONTRIBUTOR_ACTIONS: Final[frozenset[str]] = frozenset(
    {"work_event", "time_entry", "produced_deliverable", "deliverable_production"}
)


def _party_id(index: int) -> str:
    """Stable UUIDv7-shaped Party Identity for a given index."""
    return f"{_PARTY_BASE}{index:03d}"


# Per-action table + party-column + id-column tuple used by the
# verification scan. Each tuple identifies (a) the table whose rows
# carry the persisted artifact, (b) the column naming the recording /
# authoring / approving Party (the existential subject of Property 32),
# and (c) the primary-key column so the verification loop can report
# the offending row in error messages.
#
# Note: for ``produced_deliverable`` the property's subject is the
# produced Deliverable *Revision* (the row written into
# ``Deliverable_Revisions``); the Resource header row carries no
# authority-relevant attributes.
_VERIFY_TABLES: Final[dict[str, tuple[str, str, str]]] = {
    "work_assignment": (
        "Work_Assignment_Records",
        "assignment_authority_party_id",
        "work_assignment_id",
    ),
    "work_event": (
        "Work_Event_Records",
        "recording_party_id",
        "work_event_id",
    ),
    "time_entry": (
        "Time_Entry_Records",
        "recording_party_id",
        "time_entry_id",
    ),
    "produced_deliverable": (
        "Deliverable_Revisions",
        "authoring_party_id",
        "deliverable_revision_id",
    ),
    "deliverable_production": (
        "Deliverable_Production_Records",
        "recording_party_id",
        "deliverable_production_id",
    ),
    "milestone_acceptance": (
        "Milestone_Acceptance_Records",
        "accepting_party_id",
        "milestone_acceptance_id",
    ),
    "completion": (
        "Completion_Records",
        "completing_party_id",
        "completion_id",
    ),
}

# Slice 3 tables that carry a ``target_work_assignment_id`` or
# equivalent column whose Work Assignment must satisfy the AD-WS-29
# assignee-binding contract. The mapping names the column on the
# persisted row that points at the referenced Work Assignment so the
# verification loop can re-read the binding's assignee Party for
# each persisted Contributor-class row.
_CONTRIBUTOR_WA_COLUMNS: Final[dict[str, str]] = {
    "work_event": "target_work_assignment_id",
    "time_entry": "target_work_assignment_id",
    "deliverable_production": "source_work_assignment_id",
    # For Deliverable Revisions the column is
    # ``originating_work_assignment_id``.
    "produced_deliverable": "originating_work_assignment_id",
}


def _completion_plan_revision_id(index: int) -> str:
    """Stable UUIDv7-shaped Plan Revision Identity for a Completion attempt."""
    return _COMPLETION_PLAN_REVISION_TEMPLATE.format(index)


def _completion_production_id(index: int) -> str:
    """Stable UUIDv7-shaped Deliverable Production Identity for a Completion attempt."""
    return _COMPLETION_PRODUCTION_TEMPLATE.format(index)


def _completion_milestone_id(index: int) -> str:
    """Stable UUIDv7-shaped Milestone Acceptance Identity for a Completion attempt."""
    return _COMPLETION_MILESTONE_TEMPLATE.format(index)


def _ma_production_id(index: int) -> str:
    """Stable UUIDv7-shaped Deliverable Production Identity for a Milestone Acceptance attempt."""
    return _MA_PRODUCTION_TEMPLATE.format(index)


def _bound_wa_id(party_index: int, scope_index: int) -> str:
    """Stable UUIDv7-shaped Work Assignment Identity for a (Party, scope) binding."""
    # Encode party_index in the upper nibble and scope_index in the
    # lower nibble so each (party_index, scope_index) tuple maps to a
    # distinct, easy-to-read Identity.
    return _BOUND_WA_TEMPLATE.format((party_index << 4) | scope_index)


# ---------------------------------------------------------------------------
# Seed helpers — pure SQL INSERTs that bypass the wired services so the
# property exercises only the authority check on the attempted-write
# path. Mirrors the patterns established in
# ``tests/unit/test_execution_completions.py`` and
# ``tests/unit/test_execution_deliverable_productions.py``.
# ---------------------------------------------------------------------------


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert a Party row required by every FK that names a Party Identity."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


def _seed_required_parties(engine: Engine, candidate_party_ids: list[str]) -> None:
    """Seed every Party Identity the case may reference.

    The three prerequisite Parties are always present:

    * Assigning Authority — signs every Role Assignment.
    * Assignment Authority — recorded on every pre-seeded Work
      Assignment Record.
    * Approving Party — recorded as the ``authoring_party_id`` on
      pre-seeded Plan Revisions / Activity Plans / Deliverable
      Expectations / Deliverable Productions / Milestone Acceptances.

    The variable candidate Parties are the recording-Party draws for
    the test's attempted writes.
    """
    with engine.begin() as conn:
        _seed_party(
            conn,
            _ASSIGNING_AUTHORITY_ID,
            "Property 32 Assigning Authority",
        )
        _seed_party(
            conn,
            _ASSIGNMENT_AUTHORITY_ID,
            "Property 32 Assignment Authority",
        )
        _seed_party(
            conn,
            _APPROVING_PARTY_ID,
            "Property 32 Approving Authority",
        )
        for index, pid in enumerate(candidate_party_ids):
            _seed_party(conn, pid, f"Property 32 Candidate Party {index}")


def _seed_project(engine: Engine) -> None:
    """Seed one shared ``Projects`` row.

    Required as the FK target of ``Activity_Plans.target_project_id``
    and ``Deliverable_Expectation_Revisions.target_project_id``.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PREREQ_PROJECT_ID, "ts": _NOW_ISO},
        )


def _seed_activity_plan(engine: Engine) -> None:
    """Seed one shared ``Activity_Plans`` row pointing at the Project."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 32 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _PREREQ_ACTIVITY_PLAN_ID,
                "pid": _PREREQ_PROJECT_ID,
                "party": _APPROVING_PARTY_ID,
                "scope": "scope-a",
                "ts": _NOW_ISO,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str = _PREREQ_ACTIVITY_PLAN_ID,
    applicable_scope: str = "scope-a",
    lifecycle_state: str = "approved",
) -> None:
    """Insert one ``Plan_Revisions`` row by direct INSERT.

    The AD-WS-19 lifecycle trigger only fires on UPDATE, so a row
    with ``lifecycle_state = 'approved'`` may be inserted in one
    statement without driving the Plan Approval transaction.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Plan_Revisions (
                    plan_revision_id, activity_plan_id,
                    predecessor_revision_id, lifecycle_state,
                    planned_scope, deliverable_expectation_refs_json,
                    planning_assumptions_json, ordering_rationale,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :aid, NULL, :state, 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "party": _APPROVING_PARTY_ID,
                "scope": applicable_scope,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    """Seed one shared Deliverable Expectation + first Revision pair."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _PREREQ_DELIVERABLE_EXPECTATION_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Expectation_Revisions (
                    deliverable_expectation_revision_id,
                    deliverable_expectation_id, parent_revision_id,
                    target_project_id, name, description,
                    deliverable_kind, acceptance_criteria,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :did, NULL, :pid,
                    'Property 32 Deliverable Expectation',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _PREREQ_DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _PREREQ_DELIVERABLE_EXPECTATION_ID,
                "pid": _PREREQ_PROJECT_ID,
                "party": _APPROVING_PARTY_ID,
                "scope": "scope-a",
                "ts": _NOW_ISO,
            },
        )


def _seed_bound_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str,
    target_plan_revision_id: str,
    assignee_party_id: str,
    applicable_scope: str,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The Work Assignment names the candidate Party as the assignee so
    the AD-WS-29 second-stage assignee-binding check can pass when
    that Party also holds an effective ``contribute`` Role
    Assignment over the requested scope. The Assignment Authority is
    a separate fixed Party so the CHECK constraint
    ``assignee_party_id != assignment_authority_party_id``
    (Requirement 23.5) is honored.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Assignment_Records (
                    work_assignment_id, target_plan_revision_id,
                    assignee_party_id, assignment_authority_party_id,
                    assignment_rationale, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :wid, :prev, :assignee, :authority,
                    'Property 32 prerequisite work assignment.',
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_assignment_id,
                "prev": target_plan_revision_id,
                "assignee": assignee_party_id,
                "authority": _ASSIGNMENT_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": applicable_scope,
                "ts": _NOW_ISO,
            },
        )


def _seed_default_deliverable(engine: Engine) -> None:
    """Insert one default Deliverable Resource + Revision shared by the
    Milestone Acceptance attempt pool.

    The shared pair carries ``originating_work_assignment_id`` =
    NULL-equivalent (we use the first bound Work Assignment Identity
    for backref). The pre-seeded Deliverable Productions for
    Milestone Acceptance attempts all reference this Revision, so the
    Revision's authoring Party does not participate in any property
    invariant under Property 32.
    """
    digest = "a" * 64
    # Use a synthetic originating Work Assignment that resolves; the
    # first bound WA is good enough — but to keep the FK consistent
    # across cases we just inline a known WA Identity. In practice
    # the first bound WA is created before this seed runs.
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Property 32 produced runbook', :ts)
                """
            ),
            {"did": _DEFAULT_DELIVERABLE_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes,
                    content_digest_sha256, role_marker,
                    originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :did, 'text/markdown', :bytes, :digest,
                    'generated_output', :wa, :party, :ts
                )
                """
            ),
            {
                "rev": _DEFAULT_DELIVERABLE_REVISION_ID,
                "did": _DEFAULT_DELIVERABLE_ID,
                "bytes": b"produced",
                "digest": digest,
                # The originating WA is the first bound WA; the
                # property assertion treats the *Slice 3 services*
                # writes as in-scope. This seed row is a pre-seeded
                # fixture that the property loop filters out by id.
                "wa": _bound_wa_id(0, 0),
                "party": _APPROVING_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production(
    engine: Engine,
    *,
    deliverable_production_id: str,
    source_work_assignment_id: str,
) -> None:
    """Insert one ``Deliverable_Production_Records`` row directly.

    Used to pre-seed the source Production Record consumed by a
    Milestone Acceptance or Completion attempt. The recording Party
    is the Approving Authority (not the candidate Party), so this
    pre-seeded row is *excluded* from Property 32's scan by filtering
    on the pre-seeded identifier set.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Production_Records (
                    deliverable_production_id, source_work_assignment_id,
                    produced_deliverable_id, produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    production_rationale, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :pid, :wa, :did, :rev, :exp_did, :exp_rev,
                    'Property 32 prerequisite production.',
                    :party, 'role-grant-id', :abid, 'scope-a', :ts
                )
                """
            ),
            {
                "pid": deliverable_production_id,
                "wa": source_work_assignment_id,
                "did": _DEFAULT_DELIVERABLE_ID,
                "rev": _DEFAULT_DELIVERABLE_REVISION_ID,
                "exp_did": _PREREQ_DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _PREREQ_DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _APPROVING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "ts": _NOW_ISO,
            },
        )


def _seed_accept_milestone_acceptance(
    engine: Engine,
    *,
    milestone_acceptance_id: str,
    source_deliverable_production_id: str,
) -> None:
    """Insert one Accept-outcome ``Milestone_Acceptance_Records`` row.

    Used to satisfy the accepted-Milestone existence check
    (Requirement 29.1 / 29.4) for Completion attempts. The accepting
    Party is the Approving Authority so this pre-seeded row is
    *excluded* from Property 32's scan by filtering on the
    pre-seeded identifier set.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Milestone_Acceptance_Records (
                    milestone_acceptance_id,
                    source_deliverable_production_id,
                    produced_deliverable_id,
                    produced_deliverable_revision_id,
                    target_deliverable_expectation_id,
                    target_deliverable_expectation_revision_id,
                    outcome, rationale, accepting_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :mid, :pid, :did, :rev, :exp_did, :exp_rev,
                    'Accept', 'Property 32 prerequisite acceptance.',
                    :party, 'role-grant-id', :abid, 'scope-a', :ts
                )
                """
            ),
            {
                "mid": milestone_acceptance_id,
                "pid": source_deliverable_production_id,
                "did": _DEFAULT_DELIVERABLE_ID,
                "rev": _DEFAULT_DELIVERABLE_REVISION_ID,
                "exp_did": _PREREQ_DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _PREREQ_DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _APPROVING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "ts": _NOW_ISO,
            },
        )


def _assign(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    scope: str,
    authorities: list[str],
    effective_start: datetime,
    effective_end: Optional[datetime],
) -> str:
    """Persist one Role Assignment and return its ``role_assignment_id``."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="property_32_role",
        scope=scope,
        authorities_granted=tuple(authorities),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _stamp_revoked_at(engine: Engine, role_assignment_id: str, when: datetime) -> None:
    """Stamp ``revoked_at`` on a Role Assignment via direct UPDATE.

    The ``Role_Assignments_revoked_at_one_shot`` trigger enforces the
    one-shot semantic regardless of how the column is mutated.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at = :rev "
                "WHERE role_assignment_id = :rid"
            ),
            {"rev": format_iso8601_ms(when), "rid": role_assignment_id},
        )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# The five gating axes called out by the task description map onto
# five independent draws per Role Assignment, sampled from small
# alphabets so Hypothesis can cover every combination over the course
# of a 100-case run without spending its budget on unrelated
# variations.
# ---------------------------------------------------------------------------


# Day offsets relative to :data:`_NOW`. ``effective_start`` ranges
# from -30 days (well in the past, so the assignment is effective) to
# +30 days (in the future, triggering ``not-yet-effective``). The
# optional ``effective_end`` / ``revoked_at`` offsets use the same
# span; ``None`` is drawn ~50% of the time so the strategy explores
# both the bounded and the open-ended cases evenly.
_offset_days_strategy = st.integers(min_value=-30, max_value=30)
_optional_offset_days_strategy = st.one_of(
    st.none(),
    st.integers(min_value=-30, max_value=30),
)


# Authorities are drawn as a non-empty subset of the eight-value
# enumeration. ``min_size=1`` matches Requirement 12.6 — an empty
# ``authorities_granted`` list is rejected at the
# :meth:`AuthorizationService.assign_role` boundary.
_authorities_subset_strategy = st.sets(
    st.sampled_from(_AUTHORITIES), min_size=1, max_size=8
)


_scope_strategy = st.sampled_from(_ROLE_SCOPES)


@st.composite
def _role_assignment_draw(draw) -> dict:
    """Draw one Role Assignment as a dict of strategy outputs.

    Returns a dict with keys ``scope``, ``authorities``,
    ``effective_start_offset``, ``effective_end_offset`` (or
    ``None``), and ``revoked_offset`` (or ``None``). The five fields
    independently drive the five gating dimensions named in the task
    description — every Role Assignment that ends up matching a
    persisted artifact must, by construction, satisfy *all five*
    simultaneously.
    """
    return {
        "scope": draw(_scope_strategy),
        "authorities": sorted(draw(_authorities_subset_strategy)),
        "effective_start_offset": draw(_offset_days_strategy),
        "effective_end_offset": draw(_optional_offset_days_strategy),
        "revoked_offset": draw(_optional_offset_days_strategy),
    }


@st.composite
def _attempt_draw(draw, *, num_parties: int) -> dict:
    """Draw one Slice 3 artifact-creation attempt.

    Each attempt names the action kind (one of seven), the recording
    Party index, and the ``applicable_scope`` the request will use.
    The ``applicable_scope`` is also the ``target.scope`` passed to
    :meth:`AuthorizationService.evaluate`.
    """
    return {
        "action_kind": draw(st.sampled_from(_ACTION_KINDS)),
        "party_index": draw(st.integers(min_value=0, max_value=num_parties - 1)),
        "target_scope": draw(st.sampled_from(_SCOPES)),
    }


@st.composite
def _scenario_strategy(draw) -> dict:
    """Draw a full scenario for one Hypothesis case.

    Bundles the candidate Parties, their Role Assignments, and the
    per-attempt action-kind / party-index / target-scope tuples into
    one value the test consumes top-to-bottom. Keeping the scenario
    in one strategy lets Hypothesis shrink the whole case coherently.
    """
    num_parties = draw(st.integers(min_value=1, max_value=3))
    party_assignments = [
        draw(st.lists(_role_assignment_draw(), min_size=0, max_size=3))
        for _ in range(num_parties)
    ]
    num_attempts = draw(st.integers(min_value=1, max_value=6))
    attempts = [
        draw(_attempt_draw(num_parties=num_parties)) for _ in range(num_attempts)
    ]
    return {
        "num_parties": num_parties,
        "party_assignments": party_assignments,
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers cannot leak between cases.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    pragmas and all three slice schemas installed."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    create_schema(engine)
    create_planning_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Database probe helpers used in the assertion loop.
# ---------------------------------------------------------------------------


def _fetch_artifact_rows(
    engine: Engine, *, table_name: str, party_column: str, id_column: str
) -> list[dict[str, Any]]:
    """Return every persisted artifact row in append order.

    Each row is the subject of one Property 32 invariant assertion.
    Returned columns are ``id`` (the primary-key value), ``party_id``
    (the recording / authoring / approving Party — the existential
    subject of the property), ``applicable_scope`` when present, and
    ``recorded_at``. The Deliverable_Revisions table has no
    ``applicable_scope`` column; the read uses the originating Work
    Assignment Record's scope via JOIN for that case.
    """
    if table_name == "Deliverable_Revisions":
        # Produced Deliverable Revisions inherit their applicable
        # scope from the originating Work Assignment per AD-WS-29 /
        # design §"Execution_Service.DeliverableProductions" —
        # ``contribute`` authority is evaluated against the Work
        # Assignment's scope, which the Deliverable Repository
        # service passes as ``applicable_scope`` to
        # ``AuthorizationService.evaluate``.
        sql = (
            "SELECT dr.deliverable_revision_id AS id, "
            "       dr.authoring_party_id AS party_id, "
            "       wa.applicable_scope AS applicable_scope, "
            "       dr.recorded_at AS recorded_at, "
            "       dr.originating_work_assignment_id "
            "           AS target_work_assignment_id "
            "FROM Deliverable_Revisions dr "
            "JOIN Work_Assignment_Records wa "
            "    ON wa.work_assignment_id = "
            "       dr.originating_work_assignment_id "
            "ORDER BY dr.recorded_at, dr.deliverable_revision_id"
        )
        with engine.connect() as conn:
            rows = conn.execute(text(sql)).mappings().all()
        return [dict(row) for row in rows]

    sql = (
        f"SELECT {id_column} AS id, "
        f"       {party_column} AS party_id, "
        f"       applicable_scope, recorded_at "
        f"FROM {table_name} "
        f"ORDER BY recorded_at, {id_column}"
    )
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    return [dict(row) for row in rows]


def _fetch_role_assignments_for_party(
    engine: Engine, *, party_id: str
) -> list[dict[str, Any]]:
    """Return every Role Assignment row recorded for ``party_id``.

    Property 32's quantifier is "there exists a Role Assignment for
    the recording Party"; this read fetches the candidate set for
    that existential check.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT role_assignment_id, party_id, role_name, scope,
                           authorities_granted, effective_start,
                           effective_end, revoked_at
                    FROM Role_Assignments
                    WHERE party_id = :pid
                    """
                ),
                {"pid": party_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_wa_assignee(
    engine: Engine, *, work_assignment_id: str
) -> Optional[str]:
    """Return the persisted ``assignee_party_id`` for a Work Assignment
    Record, or ``None`` if no row resolves.
    """
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT assignee_party_id FROM Work_Assignment_Records "
                "WHERE work_assignment_id = :wid"
            ),
            {"wid": work_assignment_id},
        ).scalar_one_or_none()


def _fetch_contributor_target_wa(
    engine: Engine, *, table_name: str, row_id: str, id_column: str
) -> Optional[str]:
    """Return the Work Assignment Identity that a Contributor-class
    persisted row references.

    For Work Event / Time Entry rows the column is
    ``target_work_assignment_id``; for Deliverable Production rows
    the column is ``source_work_assignment_id``; for Deliverable
    Revisions the column is ``originating_work_assignment_id``.
    """
    if table_name == "Deliverable_Revisions":
        column = "originating_work_assignment_id"
    elif table_name == "Deliverable_Production_Records":
        column = "source_work_assignment_id"
    else:
        column = "target_work_assignment_id"
    with engine.connect() as conn:
        return conn.execute(
            text(
                f"SELECT {column} FROM {table_name} "
                f"WHERE {id_column} = :id"
            ),
            {"id": row_id},
        ).scalar_one_or_none()


def _role_matches_artifact(
    role: dict[str, Any],
    *,
    required_authority: str,
    applicable_scope: str,
    recorded_at_iso: str,
) -> bool:
    """Return ``True`` iff ``role`` satisfies Property 32 for the artifact.

    The predicate mirrors :class:`AuthorizationService` exactly:

    - The role must grant ``required_authority`` (Requirement 32.2
      .. 32.9; no substitution between any of the eight authority
      types per Requirement 32.10 / 32.11).
    - The role's ``scope`` must cover ``applicable_scope`` (``"*"``
      wildcard or exact equality).
    - The artifact's ``recorded_at`` must fall inside the role's
      effective period:
      ``effective_start <= recorded_at < effective_end_or_inf`` and
      either ``revoked_at`` is unset or
      ``revoked_at > recorded_at``.

    String comparisons are correct because every timestamp column is
    stored in the lexicographically sortable
    ``YYYY-MM-DDTHH:MM:SS.mmmZ`` form used by
    :func:`walking_slice.audit.format_iso8601_ms`.
    """
    try:
        authorities = json.loads(role["authorities_granted"])
    except (TypeError, ValueError):
        return False
    if required_authority not in authorities:
        return False
    scope = role["scope"]
    if scope != "*" and scope != applicable_scope:
        return False
    effective_start = role["effective_start"]
    if effective_start > recorded_at_iso:
        return False  # not-yet-effective
    effective_end = role["effective_end"]
    if effective_end is not None and effective_end <= recorded_at_iso:
        return False  # expired
    revoked_at = role["revoked_at"]
    if revoked_at is not None and revoked_at <= recorded_at_iso:
        return False  # revoked
    return True


# ---------------------------------------------------------------------------
# Per-attempt dispatch.
#
# Each attempt is dispatched to the wired Slice 3 service for its
# action kind. Any tolerated rejection (PermissionError, LookupError,
# ValueError, IntegrityError) is caught and treated as "the attempt
# did not persist an artifact" — both outcomes are accepted by
# Property 32, which only quantifies over rows that *did* land.
# ---------------------------------------------------------------------------


# Errors that mean "the attempt was rejected without persisting an
# artifact" and are therefore expected outcomes the property
# tolerates.
_TOLERATED_REJECTIONS: Final[tuple[type[BaseException], ...]] = (
    PermissionError,
    LookupError,
    ValueError,
    RuntimeError,
)


def _run_attempt(
    *,
    engine: Engine,
    attempt: dict,
    party_ids: list[str],
    scope_index_map: dict[str, int],
    completion_pool: list[str],
    ma_production_pool: list[str],
    services: dict[str, Any],
) -> None:
    """Dispatch one attempt to its wired Slice 3 service.

    Each attempt either persists the artifact (the wired
    :class:`AuthorizationService` permits the action and AD-WS-29
    passes when applicable) or raises a tolerated rejection (deny,
    assignee-binding mismatch, validation, state-machine,
    uniqueness conflict); both outcomes are accepted.

    ``completion_pool`` is mutated for Completion attempts so each
    attempt consumes a fresh approved Plan Revision Identity (and
    its pre-seeded Accept Milestone Acceptance) — avoiding the
    Requirement 29.3 uniqueness constraint as a confounding
    rejection reason.

    ``ma_production_pool`` is mutated for Milestone Acceptance
    attempts so each attempt consumes a fresh Deliverable
    Production Record Identity — avoiding the Requirement 28.3
    uniqueness constraint similarly.
    """
    kind = attempt["action_kind"]
    party_index = attempt["party_index"]
    target_scope = attempt["target_scope"]
    party_id = party_ids[party_index]
    scope_index = scope_index_map[target_scope]

    try:
        if kind == "work_assignment":
            # The recording Party is the Assignment Authority for
            # this attempt. The CHECK constraint
            # ``assignee_party_id != assignment_authority_party_id``
            # forbids self-assignment; pick a *different* candidate
            # Party as the assignee. With only one candidate Party,
            # the schema CHECK will reject the attempt — but that
            # rejection is a tolerated rejection (ValueError /
            # IntegrityError) and the property holds vacuously
            # because no row is persisted.
            other_index = (party_index + 1) % len(party_ids)
            if other_index == party_index:
                # Single-party case — there is no other Party to
                # name as assignee; the schema CHECK would reject.
                # Skip silently; Property 32 holds vacuously.
                return
            assignee_party_id = party_ids[other_index]
            with engine.begin() as conn:
                services["work_assignment"].create_work_assignment(
                    conn,
                    target_plan_revision_id=_DEFAULT_PLAN_REVISION_ID,
                    assignee_party_id=assignee_party_id,
                    assignment_authority_party_id=party_id,
                    assignment_rationale="Property 32 work assignment.",
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "work_event":
            bound_wa = _bound_wa_id(party_index, scope_index)
            with engine.begin() as conn:
                services["work_event"].create_work_event(
                    conn,
                    target_work_assignment_id=bound_wa,
                    event_kind="started",
                    event_note="Property 32 work event.",
                    recording_party_id=party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "time_entry":
            bound_wa = _bound_wa_id(party_index, scope_index)
            # ``effort_period_end`` must be <= ``recorded_at``; pick
            # a window that sits at or before _NOW.
            window_start = _NOW - timedelta(hours=2)
            window_end = _NOW - timedelta(hours=1)
            with engine.begin() as conn:
                services["time_entry"].create_time_entry(
                    conn,
                    target_work_assignment_id=bound_wa,
                    effort_hours=Decimal("1.00"),
                    effort_period_start=window_start,
                    effort_period_end=window_end,
                    recording_party_id=party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "produced_deliverable":
            bound_wa = _bound_wa_id(party_index, scope_index)
            with engine.begin() as conn:
                services["deliverable_repository"].create_produced_deliverable(
                    conn,
                    content_bytes=b"Property 32 produced bytes.",
                    content_type="text/markdown",
                    produced_deliverable_name=(
                        "Property 32 produced deliverable."
                    ),
                    originating_work_assignment_id=bound_wa,
                    authoring_party_id=party_id,
                    engine=engine,
                )
        elif kind == "deliverable_production":
            # Deliverable Production requires a pre-existing produced
            # Deliverable Revision whose
            # ``originating_work_assignment_id`` matches the source
            # Work Assignment. Create one inline via the
            # Deliverable_Repository service so the Production
            # attempt has a candidate Revision to reference. The
            # inline call itself is also gated by ``contribute``
            # authority + AD-WS-29 assignee binding; any rejection
            # there cascades into the Production rejection chain
            # which is tolerated.
            bound_wa = _bound_wa_id(party_index, scope_index)
            try:
                with engine.begin() as conn:
                    produced_result = services[
                        "deliverable_repository"
                    ].create_produced_deliverable(
                        conn,
                        content_bytes=b"Property 32 produced for production.",
                        content_type="text/markdown",
                        produced_deliverable_name=(
                            "Property 32 produced for production."
                        ),
                        originating_work_assignment_id=bound_wa,
                        authoring_party_id=party_id,
                        engine=engine,
                    )
            except _TOLERATED_REJECTIONS:
                # Without an underlying produced Revision, the
                # Production attempt cannot proceed; treat the
                # composite attempt as a tolerated rejection.
                return
            with engine.begin() as conn:
                services[
                    "deliverable_production"
                ].create_deliverable_production(
                    conn,
                    source_work_assignment_id=bound_wa,
                    produced_deliverable_revision_id=(
                        produced_result.deliverable_revision_id
                    ),
                    target_deliverable_expectation_revision_id=(
                        _PREREQ_DELIVERABLE_EXPECTATION_REVISION_ID
                    ),
                    production_rationale=(
                        "Property 32 deliverable production."
                    ),
                    recording_party_id=party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "milestone_acceptance":
            # Each MA attempt consumes a fresh pre-seeded
            # Deliverable Production Record from the per-attempt
            # pool, avoiding the Requirement 28.3 UNIQUE constraint.
            if not ma_production_pool:
                return  # pool exhausted
            production_id = ma_production_pool.pop()
            with engine.begin() as conn:
                services["milestone_acceptance"].create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=production_id,
                    outcome="Accept",
                    rationale="Property 32 milestone acceptance.",
                    accepting_party_id=party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "completion":
            # Each Completion attempt consumes a fresh pre-seeded
            # approved Plan Revision (with its own pre-seeded Accept
            # Milestone Acceptance), avoiding the Requirement 29.3
            # UNIQUE constraint.
            if not completion_pool:
                return  # pool exhausted
            plan_revision_id = completion_pool.pop()
            with engine.begin() as conn:
                services["completion"].create_completion(
                    conn,
                    target_plan_revision_id=plan_revision_id,
                    outcome="Completed",
                    rationale="Property 32 completion.",
                    source_milestone_acceptance_ids=(),
                    completing_party_id=party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        else:  # pragma: no cover - defensive
            raise AssertionError(f"Unknown action kind: {kind!r}")
    except _TOLERATED_REJECTIONS:
        # Expected for any attempt the wired services reject; the
        # property holds vacuously for denied / rejected attempts
        # because no artifact row exists.
        pass


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 32: Execution-Record authority correctness and non-substitution
@given(scenario=_scenario_strategy())
@settings(max_examples=100, deadline=2000)
def test_execution_record_authority_correctness_and_non_substitution(
    scenario: dict,
) -> None:
    """Every persisted Slice 3 Record and produced Deliverable Revision
    created by the Execution_Service or Deliverable_Repository has a
    matching Role Assignment for its recording Party whose granted
    authorities include the precise required authority, whose scope
    covers the target, and whose effective period encloses the
    recorded time; Contributor writes additionally require the
    recording Party to be the named assignee on the referenced Work
    Assignment Record (AD-WS-29)."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop32_",
        ignore_cleanup_errors=True,
    ) as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh per-case services so cross-case IdentityService state
        # cannot leak. The FixedClock anchors every persisted
        # ``recorded_at`` to the same instant, which keeps the
        # property assertion deterministic and Hypothesis shrinkage
        # tractable.
        clock = FixedClock(_NOW)
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        authorization_service = AuthorizationService(
            clock=clock,
            audit_log=audit_log,
            identity_service=identity_service,
        )

        # Read-only collaborators wired into the Slice 3 services.
        # PlanRevisionService.get_plan_revision is a connection-scoped
        # read; the instance does not need wired write collaborators.
        plan_revision_reader = PlanRevisionService(
            clock=None,  # type: ignore[arg-type]
            identity_service=None,  # type: ignore[arg-type]
            audit_log=None,  # type: ignore[arg-type]
            authorization_service=None,  # type: ignore[arg-type]
        )
        expectation_reader = DeliverableExpectationService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        )
        project_resolver = ProjectResolver()

        deliverable_repository = DeliverableRepositoryService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
            denial_audit_sleep=lambda _seconds: None,
        )

        production_service = DeliverableProductionService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
            deliverable_reader=deliverable_repository,
            planning_reader=expectation_reader,
            project_resolver=project_resolver,
            denial_audit_sleep=lambda _seconds: None,
        )

        services: dict[str, Any] = {
            "work_assignment": WorkAssignmentService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=plan_revision_reader,
                denial_audit_sleep=lambda _seconds: None,
            ),
            "work_event": WorkEventService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            ),
            "time_entry": TimeEntryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            ),
            "deliverable_repository": deliverable_repository,
            "deliverable_production": production_service,
            "milestone_acceptance": MilestoneAcceptanceService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                production_reader=production_service,
                denial_audit_sleep=lambda _seconds: None,
            ),
            "completion": CompletionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=plan_revision_reader,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            ),
        }

        try:
            party_ids = [
                _party_id(i) for i in range(scenario["num_parties"])
            ]

            # 1. Seed all Parties — candidate recording Parties plus
            #    the three fixed prerequisite Parties.
            _seed_required_parties(engine, party_ids)

            # 2. Persist every drawn Role Assignment. Skipping
            #    assignments where ``effective_end <= effective_start``
            #    keeps the input space valid: such assignments are
            #    never permitting at any instant, and the
            #    ``AssignRoleRequest`` validator would otherwise
            #    reject them.
            for party_index, assignments in enumerate(
                scenario["party_assignments"]
            ):
                pid = party_ids[party_index]
                for assignment in assignments:
                    eff_start = _NOW + timedelta(
                        days=assignment["effective_start_offset"]
                    )
                    eff_end: Optional[datetime] = None
                    if assignment["effective_end_offset"] is not None:
                        eff_end = _NOW + timedelta(
                            days=assignment["effective_end_offset"]
                        )
                        if eff_end <= eff_start:
                            continue
                    rid = _assign(
                        authorization_service,
                        engine,
                        party_id=pid,
                        scope=assignment["scope"],
                        authorities=assignment["authorities"],
                        effective_start=eff_start,
                        effective_end=eff_end,
                    )
                    if assignment["revoked_offset"] is not None:
                        revoked_at = _NOW + timedelta(
                            days=assignment["revoked_offset"]
                        )
                        _stamp_revoked_at(engine, rid, revoked_at)

            # 3. Seed the shared prerequisite chain: one Project,
            #    one Activity Plan, one Deliverable Expectation, one
            #    default approved Plan Revision shared by every
            #    attempt that does not require its own.
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_deliverable_expectation(engine)
            _seed_plan_revision(
                engine,
                plan_revision_id=_DEFAULT_PLAN_REVISION_ID,
                applicable_scope="scope-a",
            )

            # 4. Pre-seed one Work Assignment per (candidate Party,
            #    scope) combination — the assignee is the candidate
            #    Party so AD-WS-29's second stage can pass for
            #    Contributor writes by that Party against that
            #    scope. The Work Assignments are recorded with
            #    ``applicable_scope`` matching the iteration's
            #    scope, which is also the scope on which a matching
            #    Role Assignment is required.
            scope_index_map: dict[str, int] = {
                scope: idx for idx, scope in enumerate(_SCOPES)
            }
            for party_index, pid in enumerate(party_ids):
                for scope in _SCOPES:
                    scope_index = scope_index_map[scope]
                    _seed_bound_work_assignment(
                        engine,
                        work_assignment_id=_bound_wa_id(
                            party_index, scope_index
                        ),
                        target_plan_revision_id=_DEFAULT_PLAN_REVISION_ID,
                        assignee_party_id=pid,
                        applicable_scope=scope,
                    )

            # 5. Seed the default Deliverable Resource + Revision and
            #    pre-allocate the Milestone Acceptance Production
            #    pool. Each MA attempt consumes one fresh Production
            #    Record so the Requirement 28.3 UNIQUE constraint
            #    never blocks a later attempt for reasons orthogonal
            #    to authority.
            _seed_default_deliverable(engine)
            num_attempts = len(scenario["attempts"])
            ma_production_pool: list[str] = []
            for i in range(num_attempts):
                pid = _ma_production_id(i)
                _seed_deliverable_production(
                    engine,
                    deliverable_production_id=pid,
                    source_work_assignment_id=_bound_wa_id(0, 0),
                )
                ma_production_pool.append(pid)
            ma_production_pool.reverse()  # pop from the front in order

            # 6. Pre-allocate the Completion pool. Each Completion
            #    attempt consumes one fresh approved Plan Revision
            #    (with its own pre-seeded Accept Milestone
            #    Acceptance) so the Requirement 29.3 UNIQUE
            #    constraint never blocks a later attempt for reasons
            #    orthogonal to authority.
            completion_pool: list[str] = []
            for i in range(num_attempts):
                plan_rev_id = _completion_plan_revision_id(i)
                production_id = _completion_production_id(i)
                milestone_id = _completion_milestone_id(i)
                _seed_plan_revision(
                    engine,
                    plan_revision_id=plan_rev_id,
                    applicable_scope="scope-a",
                )
                # The Production Record for the Completion path
                # must target the same Plan Revision so the
                # accepted-Milestone existence check joins back.
                # Use a Work Assignment that targets this fresh
                # Plan Revision; we seed one ad hoc.
                wa_id = f"00000000-0000-7000-8000-00000e1{i:05x}"
                _seed_bound_work_assignment(
                    engine,
                    work_assignment_id=wa_id,
                    target_plan_revision_id=plan_rev_id,
                    assignee_party_id=_APPROVING_PARTY_ID,
                    applicable_scope="scope-a",
                )
                _seed_deliverable_production(
                    engine,
                    deliverable_production_id=production_id,
                    source_work_assignment_id=wa_id,
                )
                _seed_accept_milestone_acceptance(
                    engine,
                    milestone_acceptance_id=milestone_id,
                    source_deliverable_production_id=production_id,
                )
                completion_pool.append(plan_rev_id)
            completion_pool.reverse()  # pop from the front in order

            # 7. Run every attempt. The wired services either
            #    persist the artifact (permit + AD-WS-29 pass when
            #    applicable) or raise a tolerated rejection; both
            #    outcomes are accepted by Property 32 — the
            #    property only asserts over the rows that *did*
            #    land.
            for attempt in scenario["attempts"]:
                _run_attempt(
                    engine=engine,
                    attempt=attempt,
                    party_ids=party_ids,
                    scope_index_map=scope_index_map,
                    completion_pool=completion_pool,
                    ma_production_pool=ma_production_pool,
                    services=services,
                )

            # 8. Property assertions — for every persisted artifact
            #    in every Slice 3 table, there exists a matching
            #    Role Assignment for the recording / authoring /
            #    approving Party that satisfies all four gating
            #    dimensions and grants the *precise* required
            #    authority. Contributor-class rows additionally
            #    require the recording Party to be the named
            #    assignee on the referenced Work Assignment Record.
            #
            #    We exclude pre-seeded rows (the canonical Work
            #    Assignments, the default Deliverable Revision, the
            #    pre-seeded Productions and Milestone Acceptances)
            #    from the scan by filtering on their well-known
            #    identifiers; only rows written by the wired
            #    Slice 3 services participate in the property
            #    invariant.
            pre_seeded_work_assignments: set[str] = set()
            for party_index in range(len(party_ids)):
                for scope_idx in range(len(_SCOPES)):
                    pre_seeded_work_assignments.add(
                        _bound_wa_id(party_index, scope_idx)
                    )
            # Also exclude the Completion-pool Work Assignments.
            for i in range(num_attempts):
                pre_seeded_work_assignments.add(
                    f"00000000-0000-7000-8000-00000e1{i:05x}"
                )

            pre_seeded_ids: dict[str, frozenset[str]] = {
                "work_assignment": frozenset(pre_seeded_work_assignments),
                "produced_deliverable": frozenset(
                    {_DEFAULT_DELIVERABLE_REVISION_ID}
                ),
                "deliverable_production": frozenset(
                    _ma_production_id(i) for i in range(num_attempts)
                )
                | frozenset(
                    _completion_production_id(i)
                    for i in range(num_attempts)
                ),
                "milestone_acceptance": frozenset(
                    _completion_milestone_id(i)
                    for i in range(num_attempts)
                ),
            }

            for kind, (
                table_name,
                party_column,
                id_column,
            ) in _VERIFY_TABLES.items():
                required_authority = _REQUIRED_AUTHORITY[kind]
                rows = _fetch_artifact_rows(
                    engine,
                    table_name=table_name,
                    party_column=party_column,
                    id_column=id_column,
                )
                skip = pre_seeded_ids.get(kind, frozenset())
                for row in rows:
                    if row["id"] in skip:
                        # Pre-seeded prerequisite row — not created
                        # by the Slice 3 services under test.
                        continue
                    party_id = row["party_id"]
                    applicable_scope = row["applicable_scope"]
                    recorded_at_iso = row["recorded_at"]

                    # 8a. Authority correctness — Property 32 core
                    #     invariant.
                    candidates = _fetch_role_assignments_for_party(
                        engine, party_id=party_id
                    )
                    matching = [
                        role
                        for role in candidates
                        if _role_matches_artifact(
                            role,
                            required_authority=required_authority,
                            applicable_scope=applicable_scope,
                            recorded_at_iso=recorded_at_iso,
                        )
                    ]
                    assert matching, (
                        f"Property 32 violated (authority): {kind} "
                        f"artifact {row['id']!r} in {table_name} "
                        f"(party={party_id!r}, applicable_scope="
                        f"{applicable_scope!r}, recorded_at="
                        f"{recorded_at_iso!r}) has no matching "
                        f"Role_Assignments row whose granted "
                        f"authorities include "
                        f"{required_authority!r}, whose scope covers "
                        f"the target, and whose effective period "
                        f"encloses the recorded time. Candidates "
                        f"were: {candidates!r}."
                    )

                    # 8b. AD-WS-29 assignee-binding invariant for
                    #     Contributor-class persisted rows.
                    if kind in _CONTRIBUTOR_ACTIONS:
                        target_wa_id = _fetch_contributor_target_wa(
                            engine,
                            table_name=table_name,
                            row_id=row["id"],
                            id_column=id_column,
                        )
                        assert target_wa_id is not None, (
                            f"Property 32 violated (AD-WS-29 target "
                            f"WA missing): {kind} artifact "
                            f"{row['id']!r} in {table_name} has no "
                            f"referenced Work Assignment Identity."
                        )
                        wa_assignee = _fetch_wa_assignee(
                            engine, work_assignment_id=target_wa_id
                        )
                        assert wa_assignee == party_id, (
                            f"Property 32 violated (AD-WS-29 "
                            f"assignee binding): {kind} artifact "
                            f"{row['id']!r} in {table_name} was "
                            f"recorded by party={party_id!r} but "
                            f"the referenced Work Assignment "
                            f"{target_wa_id!r} has "
                            f"assignee_party_id={wa_assignee!r}. "
                            f"Contributor writes require the "
                            f"recording Party to be the named "
                            f"assignee per AD-WS-29."
                        )
        finally:
            engine.dispose()
