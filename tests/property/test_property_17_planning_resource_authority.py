# Feature: second-walking-slice, Property 17: Planning-Resource authority correctness
"""Property 17 — Planning-Resource authority correctness (task 16.2).

**Property 17: Planning-Resource authority correctness**

For all persisted Planning Resources, Revisions, and Immutable Records
created by the Planning_Service, there exists a Role Assignment for the
authoring (or reviewing, or approving) Party whose granted authorities
include the *precise* authority required by the action — ``modify`` for
Objective / Intended Outcome / Project / Deliverable Expectation /
Activity Plan / Plan Revision creation; ``review`` for Plan Review
creation; ``approve`` for Plan Approval creation — whose scope covers
the request's ``applicable_scope``, and whose effective period encloses
the artifact's recorded time. The non-substitution rule covers all four
authority types ``{view, modify, review, approve}``: a Party holding
only ``view`` / ``modify`` / ``approve`` cannot author a Plan Review,
and a Party holding only ``review`` cannot author a Plan Approval.

**Validates: Requirements 2.5, 3.5, 4.4, 5.5, 6.4, 7.5, 8.5, 9.1, 10.1,
10.3, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 20.2, 20.3**

Strategy
========

Each Hypothesis case draws a *scenario* containing:

- a set of Parties (1..3 authoring Parties plus one fixed
  assigning-authority Party);
- for each Party, a list of 0..3 Role Assignments whose dimensions vary
  independently along the five gating axes called out by the task
  description — ``effective_start`` offset, ``effective_end`` offset
  (or ``None``), revocation offset (or ``None``), ``scope`` drawn from
  a small alphabet that includes the wildcard ``"*"``, and a subset of
  granted authorities drawn from the four-value enumeration
  ``{view, modify, review, approve}`` (AD-WS-15 / Requirement 11.1);
- 1..6 *attempts*, each picking one of the eight planning artifact
  kinds (Objective, Intended Outcome, Project, Deliverable Expectation,
  Activity Plan, Plan Revision, Plan Review, Plan Approval), a Party
  index, and a ``target_scope`` from ``{scope-a, scope-b, scope-c}``
  used as the request's ``applicable_scope`` (and therefore the
  ``target.scope`` passed to ``AuthorizationService.evaluate``).

Per case the test spins up a fresh per-test SQLite engine + Slice 1 and
Slice 2 schemas, a shared :class:`~walking_slice.clock.FixedClock`
pinned to ``2026-01-01T00:00:00.000Z`` (so every artifact in the case
carries the same recorded time, which keeps the assertion deterministic
across shrinks), and the full authorization-wired Planning_Service
stack. It then:

1. Seeds the authoring Parties plus the assigning-authority Party (FK
   targets for ``Role_Assignments``, every planning revision, every
   audit row).
2. Assigns every drawn Role Assignment via
   :meth:`AuthorizationService.assign_role`; assignments whose drawn
   parameters violate Requirement 12.6 (empty authorities, inverted
   effective period) are skipped at the strategy boundary rather than
   persisted.
3. Stamps ``revoked_at`` directly via UPDATE for any assignment whose
   strategy drew a revocation offset; the
   ``Role_Assignments_revoked_at_one_shot`` trigger guarantees the
   one-shot semantic regardless.
4. Seeds the prerequisite chain — one ``Accept`` Slice 1 Decision (via
   the unwired :class:`KnowledgeService`) plus one shared Objective,
   Project, and Activity Plan inserted directly into the Slice 2 tables
   so the wired Planning_Service authority check is the *only* gate the
   attempted-write paths exercise. For each Plan Review / Plan Approval
   attempt, a fresh ``draft`` Plan Revision is inserted by hand so the
   AD-WS-19 lifecycle trigger and Requirement 9.5 uniqueness constraint
   cannot accidentally reject a later attempt for reasons orthogonal to
   authority.
5. Attempts every drawn artifact creation through the wired service.
   Each attempt either persists the artifact (the wired
   :class:`AuthorizationService` permits the action) or raises a
   :class:`PermissionError`-derived authorization error (the service
   denies it); both outcomes are accepted by the property — the
   assertion holds over the rows that *did* land.

After every attempt is processed the test queries each of the eight
artifact tables and, for every persisted row written by the wired
services in this case, scans ``Role_Assignments`` directly for a row
that simultaneously:

- belongs to the artifact's authoring / reviewing / approving Party;
- carries the *precise* required authority (``modify`` for the first
  six kinds, ``review`` for Plan Review, ``approve`` for Plan Approval)
  in ``authorities_granted`` — substitution between authority types is
  forbidden by Requirement 11.6 / 12.4;
- covers ``applicable_scope`` (either ``"*"`` or an exact match);
- has ``effective_start <= recorded_at`` (not-yet-effective is not
  violated);
- has ``effective_end IS NULL`` *or* ``effective_end > recorded_at``
  (not expired);
- has ``revoked_at IS NULL`` *or* ``revoked_at > recorded_at`` (not
  revoked).

The predicate is the same one the
:class:`~walking_slice.authorization.AuthorizationService` itself
applies; the property is therefore a *post-hoc* end-to-end check that
the Planning_Service never persists an artifact when no such Role
Assignment exists. The test reads the database directly (rather than
through the service) so the assertion catches any future regression
that leaks an artifact past the authority gate — for example a code
path that forgets to call :meth:`AuthorizationService.evaluate`, or
that substitutes one authority type for another in violation of
Requirements 11.6 / 12.4 (notably the new ``review`` ↔ ``approve``
non-substitution rule introduced by Slice 2 / AD-WS-15).

Requirement coverage notes
==========================

- **2.5, 3.5, 4.4, 5.5, 6.4, 7.5, 8.5, 9.1** — the "no artifact without
  effective authority" clause asserts the absence of any persisted
  Objective / Intended Outcome / Project / Deliverable Expectation /
  Activity Plan / Plan Revision / Plan Review / Plan Approval whose
  authoring Party's authority was missing or not-in-effect at the
  recorded time.
- **10.1, 10.3** — the matching predicate enforces the effective-period
  and scope-coverage rules verbatim against ``Role_Assignments``; the
  five gating dimensions (``not-yet-effective``, ``expired``,
  ``revoked``, ``out-of-scope``, ``no-role-assignment``) are exercised
  by the strategy.
- **11.1** — the strategy draws authorities from the four-value
  enumeration ``{view, modify, review, approve}``; the persisted Role
  Assignments cover every Slice 2-required authority value.
- **11.2, 11.3** — the property does not require ``review`` to imply
  ``approve`` or vice versa; the predicate checks the *precise*
  required authority only, so a role granting ``review`` but not
  ``approve`` cannot satisfy a Plan Approval, and a role granting
  ``approve`` but not ``review`` cannot satisfy a Plan Review.
- **11.4, 11.5** — Plan Review requires ``review`` and Plan Approval
  requires ``approve``; both are checked positively in the matching
  predicate.
- **11.6** — non-substitution: ``required ∈ granted`` is the strict
  membership check; a role granting ``view``/``modify``/``approve``
  only cannot satisfy a Plan Review attempt because ``"review" ∉
  granted``, and symmetrically for the other authority types.
- **11.7** — every evaluation appends an ``Audit_Records`` row, but the
  property test asserts against persisted artifact rows directly
  rather than against the audit trail; the evaluation rows are a
  cross-property concern (Property 19) and are not re-validated here.
- **20.2** — Planning-Resource authority invariant verbatim for the
  six ``modify`` kinds.
- **20.3** — Reviewer/Approver authority non-substitution invariant
  verbatim for Plan Reviews and Plan Approvals.

Test scaffolding follows the conventions of
``tests/property/test_property_2_decision_authority.py``: a
:class:`tempfile.TemporaryDirectory` owns the per-case SQLite file (so
state cannot leak between Hypothesis cases the way a function-scoped
pytest fixture would), and pragma-aware engine setup matches the
conftest fixtures exactly.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
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
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.activity_plans import ActivityPlanService
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.objectives import ObjectiveService
from walking_slice.planning.plan_approvals import PlanApprovalService
from walking_slice.planning.plan_revisions import PlanRevisionService
from walking_slice.planning.plan_reviews import PlanReviewService
from walking_slice.planning.projects import ProjectService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants — the recorded-time anchor, the assigning-authority
# Party, and the eight artifact kinds (with the authority each kind
# requires per AD-WS-15). The clock is pinned to ``2026-01-01`` via the
# shared :class:`FixedClock` so every artifact in the case carries the
# same recorded time and the property assertion remains deterministic
# across Hypothesis shrinks.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

_PARTY_BASE: Final[str] = "00000000-0000-7000-8000-0000000a0"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000b0001"
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-0000000c0001"
)
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Prerequisite-chain identifiers. Inserted directly into the Slice 2
# tables (bypassing the wired services) so the authority check on the
# attempted-write paths is the only gate the property exercises.
_PREREQ_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000d0001"
_PREREQ_OBJECTIVE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000d0002"
)
_PREREQ_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000d0003"
_PREREQ_PROJECT_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000d0004"
)
_PREREQ_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000d0005"

# Each Plan Review / Plan Approval attempt consumes a fresh ``draft``
# Plan Revision so the AD-WS-19 lifecycle trigger and the Requirement
# 9.5 ``UNIQUE(target_plan_revision_id)`` constraint cannot reject a
# later attempt for reasons orthogonal to authority. The fixed-template
# UUID below is suffixed with the attempt index.
_DRAFT_PLAN_REVISION_TEMPLATE: Final[str] = (
    "00000000-0000-7000-8000-00000d{0:06x}"
)

# Scope alphabet. The wildcard ``"*"`` is included so the strategy
# exercises ``AuthorizationService._scope_covers``'s wildcard branch as
# well as the equality branch. The scope list is deliberately small so
# Hypothesis can cover the scope-mismatch axis without an explosion of
# unrelated alphabet variations.
_SCOPES: Final[tuple[str, ...]] = ("scope-a", "scope-b", "scope-c")
_ROLE_SCOPES: Final[tuple[str, ...]] = _SCOPES + ("*",)

# Authority alphabet — the four Requirement 11.1 / 12.3 / 12.4 authority
# types after the Slice 2 / AD-WS-15 additive extension. Subsets of
# this set are drawn to exercise the "authority does not include the
# required value" axis (Requirement 11.6 non-substitution) without
# substituting one authority for another.
_AUTHORITIES: Final[tuple[str, ...]] = ("view", "modify", "review", "approve")

# Eight planning artifact kinds tied to the AD-WS-15 action-string →
# required-authority mapping. The property tests all eight in one case
# so the non-substitution rule across all four authority types is
# exercised in one place.
_ARTIFACT_KINDS: Final[tuple[str, ...]] = (
    "objective",
    "intended_outcome",
    "project",
    "deliverable_expectation",
    "activity_plan",
    "plan_revision",
    "plan_review",
    "plan_approval",
)

# Per-artifact required authority per AD-WS-15 / Requirements 11.2 -
# 11.5. The property's headline assertion is membership of *this*
# value in the role's ``authorities_granted`` set; substitution between
# values is forbidden by Requirement 11.6.
_REQUIRED_AUTHORITY: Final[dict[str, str]] = {
    "objective": "modify",
    "intended_outcome": "modify",
    "project": "modify",
    "deliverable_expectation": "modify",
    "activity_plan": "modify",
    "plan_revision": "modify",
    "plan_review": "review",
    "plan_approval": "approve",
}

# Per-artifact table-and-column tuple used by the verification scan.
# ``table_name`` identifies which row carries the persisted artifact;
# ``party_column`` names the column whose value is the authoring /
# reviewing / approving Party Identity (the existential subject of
# Property 17). ``id_column`` identifies the row's primary key so the
# verification loop can report the offending row in error messages.
_VERIFY_TABLES: Final[dict[str, tuple[str, str, str]]] = {
    "objective": (
        "Objective_Revisions",
        "authoring_party_id",
        "objective_revision_id",
    ),
    "intended_outcome": (
        "Intended_Outcome_Revisions",
        "authoring_party_id",
        "intended_outcome_revision_id",
    ),
    "project": (
        "Project_Revisions",
        "authoring_party_id",
        "project_revision_id",
    ),
    "deliverable_expectation": (
        "Deliverable_Expectation_Revisions",
        "authoring_party_id",
        "deliverable_expectation_revision_id",
    ),
    "activity_plan": (
        "Activity_Plans",
        "authoring_party_id",
        "activity_plan_id",
    ),
    "plan_revision": (
        "Plan_Revisions",
        "authoring_party_id",
        "plan_revision_id",
    ),
    "plan_review": (
        "Plan_Review_Revisions",
        "reviewing_party_id",
        "plan_review_revision_id",
    ),
    "plan_approval": (
        "Plan_Approval_Records",
        "approving_party_id",
        "plan_approval_id",
    ),
}


def _party_id(index: int) -> str:
    """Stable UUIDv7-shaped Party Identity for a given index.

    The strategy draws 1..3 Parties per case; this helper formats a
    canonical UUIDv7 string (the regex in
    :data:`walking_slice.identity.CANONICAL_UUID7_REGEX`) by tacking
    the index onto a shared prefix. Stable IDs make shrinkage
    diagnostics easier to read.
    """
    return f"{_PARTY_BASE}{index:03d}"


def _draft_plan_revision_id(index: int) -> str:
    """Stable UUIDv7-shaped Plan Revision Identity for a given attempt index."""
    return _DRAFT_PLAN_REVISION_TEMPLATE.format(index)



# ---------------------------------------------------------------------------
# Seed helpers — pure SQL INSERTs that bypass the wired Planning_Service
# so the property exercises *only* the authority check on the
# attempted-write path.
# ---------------------------------------------------------------------------


def _seed_party(conn, party_id: str, display: str) -> None:
    """Insert a Party row required by every FK that names a Party
    Identity: ``Role_Assignments.party_id``,
    ``Role_Assignments.assigning_authority_id``,
    ``Objective_Revisions.authoring_party_id``,
    ``Intended_Outcome_Revisions.authoring_party_id``,
    ``Project_Revisions.authoring_party_id``,
    ``Deliverable_Expectation_Revisions.authoring_party_id``,
    ``Activity_Plans.authoring_party_id``,
    ``Plan_Revisions.authoring_party_id``,
    ``Plan_Review_Revisions.reviewing_party_id``,
    ``Plan_Approval_Records.approving_party_id``, and
    ``Audit_Records.actor_party_id``.
    """
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


def _seed_prerequisite_chain(
    engine: Engine,
    knowledge_unwired: KnowledgeService,
    *,
    seeding_party_id: str,
) -> str:
    """Insert one ``Accept`` Decision plus the Objective/Project/Activity
    Plan rows that the wired attempt paths require to reach the
    authorization check.

    The wired Planning_Service services would refuse to create an
    Objective without an ``Accept`` Decision (Requirement 2.4), an
    Intended Outcome / Project without an Objective (Requirements 3.4
    / 4.3), a Deliverable Expectation / Activity Plan without a
    Project (Requirements 5.4 / 6.3), or a Plan Revision without an
    Activity Plan (Requirement 7.4). Pre-seeding those rows directly
    keeps the property's failure mode focused: the only reason an
    attempt can fail is the authority check, which is exactly what
    Property 17 inspects.

    The Decision is created via the *unwired*
    :class:`~walking_slice.knowledge.KnowledgeService` (no
    ``authorization_service`` injected) so Decision creation is not
    itself gated by an authority check that would entangle Property 17
    with Slice 1 Property 2.

    Returns the seeded Decision Identity so the Objective creation
    attempts can target it via ``target_decision_id`` (Requirement
    2.2).
    """
    with engine.begin() as conn:
        finding = knowledge_unwired.create_finding(
            conn,
            statement="Property 17 prerequisite finding.",
            authoring_party_id=seeding_party_id,
            is_hypothesis=True,
        )
        recommendation = knowledge_unwired.create_recommendation(
            conn,
            authoring_party_id=seeding_party_id,
            derived_from_findings=[finding.finding_id],
            rationale="Property 17 prerequisite recommendation.",
        )
        decision = knowledge_unwired.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Property 17 prerequisite decision (Accept).",
            deciding_party_id=seeding_party_id,
            authority_basis=_BASIS,
            applicable_scope="scope-a",
        )

        # ----- Objectives + Objective_Revisions ------------------------
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _PREREQ_OBJECTIVE_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rid, :oid, NULL, :stmt, NULL, :did, :party,
                    :scope, :ts
                )
                """
            ),
            {
                "rid": _PREREQ_OBJECTIVE_REVISION_ID,
                "oid": _PREREQ_OBJECTIVE_ID,
                "stmt": "Property 17 prerequisite objective statement.",
                "did": decision.decision_id,
                "party": seeding_party_id,
                "scope": "scope-a",
                "ts": _NOW_ISO,
            },
        )

        # ----- Projects + Project_Revisions ----------------------------
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PREREQ_PROJECT_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Project_Revisions (
                    project_revision_id, project_id, parent_revision_id,
                    name, summary, target_objective_id,
                    planned_start_date, planned_end_date,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rid, :pid, NULL, :name, NULL, :oid,
                    '2026-01-01', '2026-12-31',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rid": _PREREQ_PROJECT_REVISION_ID,
                "pid": _PREREQ_PROJECT_ID,
                "name": "Property 17 prerequisite project.",
                "oid": _PREREQ_OBJECTIVE_ID,
                "party": seeding_party_id,
                "scope": "scope-a",
                "ts": _NOW_ISO,
            },
        )

        # ----- Activity_Plans ------------------------------------------
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, :title, :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _PREREQ_ACTIVITY_PLAN_ID,
                "pid": _PREREQ_PROJECT_ID,
                "title": "Property 17 prerequisite activity plan.",
                "party": seeding_party_id,
                "scope": "scope-a",
                "ts": _NOW_ISO,
            },
        )

    return decision.decision_id


def _seed_draft_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    authoring_party_id: str,
) -> None:
    """Insert one ``draft`` ``Plan_Revisions`` row directly.

    Plan Review and Plan Approval attempts each consume a fresh draft
    Plan Revision so the AD-WS-19 lifecycle trigger and the Requirement
    9.5 ``UNIQUE(target_plan_revision_id)`` constraint cannot reject a
    later attempt for reasons orthogonal to authority. Directly
    INSERTing the row (rather than calling
    :meth:`PlanRevisionService.create_plan_revision`) sidesteps the
    Plan Revision authority check that Property 17 already exercises
    through the ``plan_revision`` artifact kind — keeping the
    prerequisite seeding independent of the property under test.
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
                    :rev, :aid, NULL, 'draft', :scope_text,
                    '[]', '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _PREREQ_ACTIVITY_PLAN_ID,
                "scope_text": "Property 17 prerequisite plan revision scope.",
                "party": authoring_party_id,
                "scope": "scope-a",
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
        role_name="property_17_role",
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
    one-shot semantic regardless of how the column is mutated; using
    UPDATE here keeps the property test independent of the Slice 1
    revocation HTTP endpoint.
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
# The five gating axes called out by the task description map onto five
# independent draws per Role Assignment, sampled from small alphabets
# so Hypothesis can cover every combination over the course of a
# 100-case run without spending its budget on unrelated variations.
# ---------------------------------------------------------------------------


# Day offsets relative to :data:`_NOW`. ``effective_start`` ranges from
# -30 days (well in the past, so the assignment is effective) to +30
# days (in the future, triggering ``not-yet-effective``). Optional
# ``effective_end`` / ``revoked_at`` use the same span; ``None`` is
# drawn ~50% of the time so the strategy explores both the bounded and
# the open-ended cases evenly.
_offset_days_strategy = st.integers(min_value=-30, max_value=30)
_optional_offset_days_strategy = st.one_of(
    st.none(),
    st.integers(min_value=-30, max_value=30),
)


# Authorities are drawn as a non-empty subset of the four-value
# enumeration. ``min_size=1`` matches Requirement 12.6 — an empty
# ``authorities_granted`` list is rejected at the
# :meth:`AuthorizationService.assign_role` boundary, so the strategy
# would otherwise either crash with :class:`InvalidRoleAssignmentError`
# or never persist anything for these draws.
_authorities_subset_strategy = st.sets(
    st.sampled_from(_AUTHORITIES), min_size=1, max_size=4
)


_scope_strategy = st.sampled_from(_ROLE_SCOPES)


@st.composite
def _role_assignment_draw(draw) -> dict:
    """Draw one Role Assignment as a dict of strategy outputs.

    Returns a dict with keys ``scope``, ``authorities``,
    ``effective_start_offset``, ``effective_end_offset`` (or ``None``),
    and ``revoked_offset`` (or ``None``). The five fields independently
    drive the five gating dimensions named in the task description —
    every Role Assignment that ends up matching a persisted artifact
    must, by construction, satisfy *all five* simultaneously.
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
    """Draw one artifact-creation attempt.

    Each attempt names the artifact kind (one of the eight Slice 2
    planning kinds), the authoring Party index, and the
    ``applicable_scope`` the request will use. The latter is also the
    ``target.scope`` passed to ``AuthorizationService.evaluate`` so
    the wired role assignment must cover this exact scope to permit
    the action.
    """
    return {
        "artifact_kind": draw(st.sampled_from(_ARTIFACT_KINDS)),
        "party_index": draw(st.integers(min_value=0, max_value=num_parties - 1)),
        "target_scope": draw(st.sampled_from(_SCOPES)),
    }


@st.composite
def _scenario_strategy(draw) -> dict:
    """Draw a full scenario for one Hypothesis case.

    Bundles the authoring Parties, their Role Assignments, and the
    per-attempt artifact-kind / party-index / target-scope tuples into
    one value the test consumes top-to-bottom. Keeping the scenario in
    one strategy lets Hypothesis shrink the whole case coherently —
    for example, shrinking down to a single Party with a single Role
    Assignment and a single attempt yields the smallest counterexample
    if the property is ever falsified.
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
# path so cross-case identifiers, audit rows, role assignments, and
# planning artifacts cannot leak between cases (design §"Testing
# Strategy" — "Each property and example test gets a fresh SQLite
# database").
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys
    pragmas and both Slice 1 and Slice 2 schemas installed."""
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
    return engine


# ---------------------------------------------------------------------------
# Database probe helpers used in the assertion loop.
# ---------------------------------------------------------------------------


def _fetch_artifact_rows(
    engine: Engine, *, table_name: str, party_column: str, id_column: str
) -> list[dict[str, Any]]:
    """Return every persisted artifact row in append order.

    Each row is the subject of one Property 17 invariant assertion.
    Returned columns are ``id`` (the primary-key value), ``party_id``
    (the authoring / reviewing / approving Party — the existential
    subject of the property), ``applicable_scope``, and ``recorded_at``
    so the predicate can be evaluated row-by-row.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT {id_column} AS id,
                           {party_column} AS party_id,
                           applicable_scope,
                           recorded_at
                    FROM {table_name}
                    ORDER BY recorded_at, {id_column}
                    """
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_role_assignments_for_party(
    engine: Engine, *, party_id: str
) -> list[dict[str, Any]]:
    """Return every Role Assignment row recorded for ``party_id``.

    Property 17's quantifier is "there exists a Role Assignment for the
    authoring Party"; this read fetches the candidate set for that
    existential check.
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


def _role_matches_artifact(
    role: dict[str, Any],
    *,
    required_authority: str,
    applicable_scope: str,
    recorded_at_iso: str,
) -> bool:
    """Return ``True`` iff ``role`` satisfies Property 17 for the artifact.

    The predicate mirrors :class:`AuthorizationService` exactly:

    - The role must grant ``required_authority`` (Requirement 11.2
      through 11.5; no substitution between view/modify/review/approve
      per Requirement 11.6 / 12.4).
    - The role's ``scope`` must cover ``applicable_scope`` (``"*"``
      wildcard or exact equality).
    - The artifact's ``recorded_at`` must fall inside the role's
      effective period:
      ``effective_start <= recorded_at < effective_end_or_inf`` and
      either ``revoked_at`` is unset or ``revoked_at > recorded_at``.

    String comparisons are correct here because every timestamp column
    is stored in the lexicographically sortable
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
        # not-yet-effective
        return False
    effective_end = role["effective_end"]
    if effective_end is not None and effective_end <= recorded_at_iso:
        # expired
        return False
    revoked_at = role["revoked_at"]
    if revoked_at is not None and revoked_at <= recorded_at_iso:
        # revoked
        return False
    return True


# ---------------------------------------------------------------------------
# Per-attempt dispatch.
#
# Each attempt is dispatched to the wired service for its artifact
# kind. Any :class:`PermissionError` (the common base class of every
# Planning_Service authorization error) or :class:`LookupError` (e.g.
# Plan Approval conflict on an already-approved revision) is caught
# and treated as "the attempt did not persist an artifact" — both
# outcomes are accepted by Property 17, which only quantifies over
# rows that *did* land.
# ---------------------------------------------------------------------------


# Errors that mean "the attempt was rejected without persisting an
# artifact" and are therefore expected outcomes the property tolerates.
# Every Planning_Service authorization error inherits from
# :class:`PermissionError`; conflict errors (Plan Approval targeting an
# already-approved revision) inherit from :class:`LookupError`;
# validation errors inherit from :class:`ValueError`. We catch all
# three so the test focuses on the property invariant rather than on
# the precise rejection path each service uses.
_TOLERATED_REJECTIONS = (PermissionError, LookupError, ValueError)


def _run_attempt(
    *,
    engine: Engine,
    attempt: dict,
    party_ids: list[str],
    decision_id: str,
    plan_revision_pool: list[str],
    services: dict[str, Any],
) -> None:
    """Dispatch one attempt to its wired Planning_Service service.

    Each attempt either persists the artifact (the wired
    :class:`AuthorizationService` permits the action) or raises a
    tolerated rejection (the service denies it or rejects the input
    for a reason orthogonal to the property). Both outcomes are
    accepted; the property assertion runs after every attempt has
    been processed.

    ``plan_revision_pool`` is mutated for Plan Review / Plan Approval
    attempts so each attempt consumes a unique draft Plan Revision —
    avoiding the Requirement 9.5 uniqueness constraint and the
    AD-WS-19 lifecycle trigger as a confounding rejection reason.
    """
    kind = attempt["artifact_kind"]
    party_index = attempt["party_index"]
    target_scope = attempt["target_scope"]
    authoring_party_id = party_ids[party_index]

    try:
        if kind == "objective":
            with engine.begin() as conn:
                services["objective"].create_objective(
                    conn,
                    statement=f"Property 17 objective statement.",
                    rationale=None,
                    target_decision_id=decision_id,
                    authoring_party_id=authoring_party_id,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "intended_outcome":
            with engine.begin() as conn:
                services["intended_outcome"].create_intended_outcome(
                    conn,
                    target_objective_id=_PREREQ_OBJECTIVE_ID,
                    success_condition="Property 17 intended outcome condition.",
                    observation_window=None,
                    attribution_assumption=None,
                    authoring_party_id=authoring_party_id,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "project":
            with engine.begin() as conn:
                services["project"].create_project(
                    conn,
                    target_objective_id=_PREREQ_OBJECTIVE_ID,
                    name="Property 17 project name.",
                    summary=None,
                    planned_start_date=date(2026, 1, 1),
                    planned_end_date=date(2026, 12, 31),
                    authoring_party_id=authoring_party_id,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "deliverable_expectation":
            with engine.begin() as conn:
                services["deliverable_expectation"].create_deliverable_expectation(
                    conn,
                    target_project_id=_PREREQ_PROJECT_ID,
                    name="Property 17 deliverable name.",
                    description=None,
                    deliverable_kind="Document",
                    acceptance_criteria=None,
                    authoring_party_id=authoring_party_id,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "activity_plan":
            with engine.begin() as conn:
                services["activity_plan"].create_activity_plan(
                    conn,
                    target_project_id=_PREREQ_PROJECT_ID,
                    title="Property 17 activity plan title.",
                    authoring_party_id=authoring_party_id,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "plan_revision":
            with engine.begin() as conn:
                services["plan_revision"].create_plan_revision(
                    conn,
                    target_activity_plan_id=_PREREQ_ACTIVITY_PLAN_ID,
                    planned_scope="Property 17 planned scope.",
                    deliverable_expectation_refs=(),
                    planning_assumptions=(),
                    ordering_rationale=None,
                    predecessor_plan_revision_id=None,
                    authoring_party_id=authoring_party_id,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "plan_review":
            # Plan Reviews require a fresh draft Plan Revision so the
            # Requirement 8.6 ``lifecycle_state == 'draft'`` precheck
            # cannot reject the attempt for reasons orthogonal to
            # authority.
            target_plan_revision_id = plan_revision_pool.pop()
            _seed_draft_plan_revision(
                engine,
                plan_revision_id=target_plan_revision_id,
                authoring_party_id=authoring_party_id,
            )
            with engine.begin() as conn:
                services["plan_review"].create_plan_review(
                    conn,
                    target_plan_revision_id=target_plan_revision_id,
                    outcome="Endorse",
                    rationale="Property 17 plan review rationale.",
                    reviewing_party_id=authoring_party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                    engine=engine,
                )
        elif kind == "plan_approval":
            # Plan Approvals require a fresh draft Plan Revision so
            # the Requirement 9.5 uniqueness constraint cannot reject
            # the attempt for reasons orthogonal to authority.
            target_plan_revision_id = plan_revision_pool.pop()
            _seed_draft_plan_revision(
                engine,
                plan_revision_id=target_plan_revision_id,
                authoring_party_id=authoring_party_id,
            )
            with engine.begin() as conn:
                services["plan_approval"].create_plan_approval(
                    conn,
                    engine,
                    target_plan_revision_id=target_plan_revision_id,
                    outcome="Approve",
                    rationale="Property 17 plan approval rationale.",
                    approving_party_id=authoring_party_id,
                    authority_basis=_BASIS,
                    applicable_scope=target_scope,
                )
        else:  # pragma: no cover - defensive
            raise AssertionError(f"Unknown artifact kind: {kind!r}")
    except _TOLERATED_REJECTIONS:
        # Expected for any attempt the wired services reject; the
        # property holds vacuously for denied attempts because no
        # artifact row exists.
        pass


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 17: Planning-Resource authority correctness
@given(scenario=_scenario_strategy())
@settings(max_examples=100, deadline=2000)
def test_planning_resource_authority_correctness(scenario: dict) -> None:
    """Every persisted Planning Resource, Revision, and Immutable Record
    created by the Planning_Service has a matching Role Assignment for
    its authoring Party whose granted authorities include the precise
    required authority, whose scope covers the target, and whose
    effective period encloses the recorded time."""
    with tempfile.TemporaryDirectory(
        prefix="walking_slice_prop17_"
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
        manifest_writer = ProvenanceManifestWriter(
            clock=clock,
            identity_service=identity_service,
        )
        # The unwired KnowledgeService seeds the prerequisite
        # Decision / Recommendation / Finding without invoking the
        # Slice 1 Decision authority check — that check is Property
        # 2's concern, not Property 17's, and we do not want to
        # entangle the two properties.
        knowledge_unwired = KnowledgeService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )

        services: dict[str, Any] = {
            "objective": ObjectiveService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                knowledge_service=knowledge_unwired,
            ),
            "intended_outcome": IntendedOutcomeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            ),
            "project": ProjectService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            ),
            "deliverable_expectation": DeliverableExpectationService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            ),
            "activity_plan": ActivityPlanService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            ),
            "plan_revision": PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            ),
            "plan_review": PlanReviewService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            ),
            "plan_approval": PlanApprovalService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                manifest_writer=manifest_writer,
            ),
        }

        try:
            # 1. Seed all Parties (authoring Parties plus the
            #    assigning authority). One transaction keeps the FK
            #    targets visible to every later write.
            party_ids = [
                _party_id(i) for i in range(scenario["num_parties"])
            ]
            with engine.begin() as conn:
                _seed_party(
                    conn,
                    _ASSIGNING_AUTHORITY_ID,
                    "Property 17 Assigning Authority",
                )
                for index, pid in enumerate(party_ids):
                    _seed_party(conn, pid, f"Property 17 Party {index}")

            # 2. Persist every drawn Role Assignment. Skipping
            #    assignments where ``effective_end <= effective_start``
            #    keeps the input space valid without changing the
            #    property under test — such assignments are never
            #    permitting anyway because the auth service would
            #    evaluate them as both not-yet-effective and expired
            #    at every instant; pruning them upstream keeps the
            #    persisted ``Role_Assignments`` table free of provably
            #    dead rows.
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
                            # Skip — see the comment above.
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

            # 3. Seed the prerequisite chain so each attempted-write
            #    path can reach the authorization check. Uses the
            #    assigning-authority Party as the seeding actor — the
            #    seeding Decision and prerequisite Objective /
            #    Project / Activity Plan rows are not under test.
            decision_id = _seed_prerequisite_chain(
                engine,
                knowledge_unwired,
                seeding_party_id=_ASSIGNING_AUTHORITY_ID,
            )

            # 4. Pre-allocate one fresh draft Plan Revision Identity
            #    per attempt so plan_review / plan_approval attempts
            #    never collide on the Requirement 9.5 ``UNIQUE``
            #    constraint. The pool is consumed in attempt order
            #    by ``_run_attempt``.
            plan_revision_pool: list[str] = [
                _draft_plan_revision_id(i)
                for i in range(len(scenario["attempts"]))
            ]
            # ``list.pop()`` consumes from the end; reversing keeps
            # the consumed order aligned with the attempt order for
            # clearer diagnostics on shrinks.
            plan_revision_pool.reverse()

            # 5. Run every attempt. The wired services either persist
            #    the artifact (permit) or raise a tolerated rejection
            #    (deny / validation / conflict); both outcomes are
            #    accepted by Property 17 — the property only asserts
            #    over the rows that *did* land.
            for attempt in scenario["attempts"]:
                _run_attempt(
                    engine=engine,
                    attempt=attempt,
                    party_ids=party_ids,
                    decision_id=decision_id,
                    plan_revision_pool=plan_revision_pool,
                    services=services,
                )

            # 6. Property assertions — for every persisted artifact in
            #    every Slice 2 table, there exists a matching Role
            #    Assignment for the authoring / reviewing / approving
            #    Party that satisfies all four gating dimensions and
            #    grants the *precise* required authority.
            #
            #    We exclude pre-seeded rows (the prerequisite
            #    Objective / Project / Activity Plan / draft Plan
            #    Revisions) from the scan by filtering on the
            #    well-known seeded identifiers; only rows written by
            #    the wired Planning_Service services participate in
            #    the property invariant.
            pre_seeded_ids: dict[str, frozenset[str]] = {
                "objective": frozenset({_PREREQ_OBJECTIVE_REVISION_ID}),
                "project": frozenset({_PREREQ_PROJECT_REVISION_ID}),
                "activity_plan": frozenset({_PREREQ_ACTIVITY_PLAN_ID}),
                "plan_revision": frozenset(
                    _draft_plan_revision_id(i)
                    for i in range(len(scenario["attempts"]))
                ),
            }
            for kind, (table_name, party_column, id_column) in (
                _VERIFY_TABLES.items()
            ):
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
                        # by the Planning_Service; not subject to
                        # Property 17.
                        continue
                    candidates = _fetch_role_assignments_for_party(
                        engine, party_id=row["party_id"]
                    )
                    matching = [
                        role
                        for role in candidates
                        if _role_matches_artifact(
                            role,
                            required_authority=required_authority,
                            applicable_scope=row["applicable_scope"],
                            recorded_at_iso=row["recorded_at"],
                        )
                    ]
                    assert matching, (
                        f"Property 17 violated: {kind} artifact "
                        f"{row['id']!r} in {table_name} (party="
                        f"{row['party_id']!r}, applicable_scope="
                        f"{row['applicable_scope']!r}, recorded_at="
                        f"{row['recorded_at']!r}) has no matching "
                        f"Role_Assignments row whose granted "
                        f"authorities include "
                        f"{required_authority!r}, whose scope "
                        f"covers the target, and whose effective "
                        f"period encloses the recorded time. "
                        f"Candidates were: {candidates!r}."
                    )
        finally:
            engine.dispose()
