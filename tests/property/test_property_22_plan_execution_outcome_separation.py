# Feature: second-walking-slice, Property 22: Plan/Execution and Output/Outcome separation
"""Property 22 — Plan/Execution and Output/Outcome separation (task 16.7).

**Property 22: Plan/Execution and Output/Outcome separation**

For all request bodies submitted to any Planning_Service endpoint, if
the body contains any field whose name matches a prohibited execution
prefix (``work-``, ``time-``, ``milestone-``,
``deliverable-production-``, ``blockage-``, ``completion-``,
``actual-``, ``percent-complete-``, ``remaining-``) or a prohibited
observed-outcome prefix (``observed-``, ``observation-time-``,
``attribution-evidence-``) or a prohibited produced-deliverable prefix
(``produced-``, ``hand-off-``, ``accepted-by-``), the Planning_Service
rejects the request, declines to create any Resource or Revision, and
returns an error indication identifying each prohibited attribute. No
persisted Planning Resource carries any such attribute. Every Intended
Outcome Revision row carries ``outcome_kind = 'intended'``; every
response body for an Intended Outcome distinguishes it from an
Observed Outcome.

**Validates: Requirements 3.3, 5.3, 12.1, 12.2, 12.4, 13.1, 13.2,
13.3, 13.4, 13.5, 20.5, 20.6**

Strategy
========

The property statement bundles three sub-invariants. This module
exercises all three:

1. :func:`test_prohibited_attribute_request_is_rejected_with_no_row_persisted`
   — *the rejection invariant*. Hypothesis draws a planning service
   kind (one of the eight Planning_Service write surfaces) and a
   random prohibited-prefix key for that service. The service is
   invoked with the random key on its ``request_attributes`` pass-
   through. The test asserts that:
   - the service raises its ``*ValidationError`` carrying
     ``failed_constraint = 'prohibited_attribute'`` and the offending
     key on :attr:`prohibited_keys` (Requirements 12.2 / 13.5),
   - no row landed in any Slice 2 planning Resource or Revision
     table, and no row landed in ``Audit_Records`` for the action's
     ``action_type`` (Requirements 12.1, 12.2, 13.1, 13.2 / Property
     22's "no persisted Planning Resource carries any such
     attribute").

2. :func:`test_persisted_intended_outcome_revision_carries_outcome_kind_intended`
   — *the outcome-kind invariant* (Requirements 3.3, 13.3, 13.4 /
   Property 22's "Every Intended Outcome Revision row carries
   ``outcome_kind = 'intended'``"). Hypothesis draws a valid
   Intended Outcome payload. The Intended_Outcome_Revisions row that
   :meth:`IntendedOutcomeService.create_intended_outcome` writes is
   re-read from the database and the
   :class:`CreateIntendedOutcomeResult` is inspected; both surfaces
   must carry the literal string ``"intended"``.

3. :func:`test_planning_resource_result_carries_no_derived_execution_value`
   — *the response-body invariant* (Requirement 12.4 / Property 22's
   "no response body for a planning Resource includes a derived
   current-execution status, percent-complete value, actual-cost
   value, or remaining-work value"). Hypothesis draws a planning
   result dataclass; the test asserts none of its declared field
   names matches the four derived-execution prefixes the property
   explicitly forbids.

Setup follows the conventions established by Property 16
(:mod:`tests.property.test_property_16_planning_creation_success`):
per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory
state cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps. The
prohibited-attribute screen in every Planning_Service write fires
*before* any database read, role-assignment lookup, target
resolution, or identity minting (see step 1 of every
``create_<resource>`` method in
:mod:`walking_slice.planning.objectives` and siblings), so the
rejection-invariant test only needs the bare schema — no Parties,
roles, or prerequisite rows are required for the rejection path.
"""

from __future__ import annotations

import dataclasses
import tempfile
import uuid as uuid_lib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Mapping

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
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
from walking_slice.planning._helpers import (
    ALL_PROHIBITED_PREFIXES,
    OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
    PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
)
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.activity_plans import (
    ActivityPlanService,
    ActivityPlanValidationError,
    CreateActivityPlanResult,
)
from walking_slice.planning.deliverable_expectations import (
    CreateDeliverableExpectationResult,
    DeliverableExpectationService,
    DeliverableExpectationValidationError,
)
from walking_slice.planning.intended_outcomes import (
    CreateIntendedOutcomeResult,
    IntendedOutcomeService,
    IntendedOutcomeValidationError,
)
from walking_slice.planning.objectives import (
    CreateObjectiveResult,
    ObjectiveService,
    ObjectiveValidationError,
)
from walking_slice.planning.plan_approvals import (
    CreatePlanApprovalResult,
    PlanApprovalService,
    PlanApprovalValidationError,
)
from walking_slice.planning.plan_reviews import (
    CreatePlanReviewResult,
    PlanReviewService,
    PlanReviewValidationError,
)
from walking_slice.planning.plan_revisions import (
    CreatePlanRevisionResult,
    PlanRevisionService,
    PlanRevisionValidationError,
)
from walking_slice.planning.projects import (
    CreateProjectResult,
    ProjectService,
    ProjectValidationError,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
#
# Identifiers used for setup-only artifacts (Parties, prerequisite
# Resource rows, authority-basis ids). Per-case fresh services mean
# these identifiers never collide across Hypothesis examples.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-22/scope"

# Deterministic prerequisite identifiers seeded by the helpers below.
# UUIDv7-shaped to satisfy the canonical-form CHECK on
# ``Identifier_Registry`` rows downstream services may insert. Reused
# across the rejection-path and outcome-kind tests where seeding is
# needed.
_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c4"
_DRAFT_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c5"

# A placeholder UUIDv7 used for kwargs whose value the service never
# inspects on the rejection path (the prohibited-attribute screen
# fires before target resolution, so a never-resolved identifier is
# fine here).
_PLACEHOLDER_UUID7: Final[str] = "00000000-0000-7000-8000-0000000000ff"


# ---------------------------------------------------------------------------
# Slice 2 tables that the rejection path MUST NOT populate.
#
# Property 22's "no persisted Planning Resource carries any such
# attribute" clause is verified by asserting every one of these
# tables is empty after a rejected request. Listed in
# Resource → Revision order so a counterexample reads naturally.
# ---------------------------------------------------------------------------


_PLANNING_RESOURCE_TABLES: Final[tuple[str, ...]] = (
    "Objectives",
    "Objective_Revisions",
    "Intended_Outcomes",
    "Intended_Outcome_Revisions",
    "Projects",
    "Project_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    "Activity_Plans",
    "Plan_Revisions",
    "Plan_Reviews",
    "Plan_Review_Revisions",
    "Plan_Approval_Records",
)


# ---------------------------------------------------------------------------
# Derived-execution attribute prefixes (Requirement 12.4 / Property 22).
#
# These four prefixes name the categories of derived values the
# property explicitly forbids in any planning Resource response body.
# The matcher used in
# :func:`test_planning_resource_result_carries_no_derived_execution_value`
# canonicalizes hyphen/underscore variants and is case-insensitive,
# mirroring the API-boundary screen in
# :func:`walking_slice.planning._helpers._reject_prohibited_attributes`.
# ---------------------------------------------------------------------------


_DERIVED_EXECUTION_FIELD_PREFIXES: Final[tuple[str, ...]] = (
    "current-execution-status",
    "percent-complete",
    "actual-cost",
    "remaining-work",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys.

    Mirrors :func:`tests.property.test_property_16_planning_creation_success._build_engine`.
    Installs both schemas (Slice 1 + Slice 2) so the rejection path's
    ``SELECT COUNT(*)`` probes always find a real table.
    """
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


def _count(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_audit_rows(engine: Engine, action_type: str) -> int:
    """Return the count of ``Audit_Records`` rows for one ``action_type``.

    Includes every outcome (``permit``, ``deny``, ``consequential``)
    so the assertion catches both consequential rows and any
    evaluation rows the service might have prematurely committed in
    a rejection path.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE action_type = :a"
                ),
                {"a": action_type},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Per-case service factory.
# ---------------------------------------------------------------------------


def _build_services() -> dict[str, Any]:
    """Construct the per-case service bundle.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state and any audit-correlation accumulator cannot
    bleed across shrinks. Returns a mapping keyed by service-kind
    label so the dispatch table below can look the relevant service
    up directly.
    """
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    knowledge_service = KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    manifest_writer = ProvenanceManifestWriter(
        clock=clock,
        identity_service=identity_service,
    )
    return {
        "clock": clock,
        "identity_service": identity_service,
        "audit_log": audit_log,
        "authorization_service": authorization_service,
        "knowledge_service": knowledge_service,
        "manifest_writer": manifest_writer,
        "objective": ObjectiveService(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
            knowledge_service=knowledge_service,
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


# ---------------------------------------------------------------------------
# Per-service rejection-path drivers.
#
# Each entry tells the rejection-invariant test how to invoke one
# Planning_Service write with a generated prohibited attribute on its
# ``request_attributes`` parameter:
#
# - ``prefixes``: which prefix set the service screens (see step 1
#   of the corresponding ``create_<resource>`` docstring). The
#   strategy draws prohibited keys from this set.
# - ``error_class``: the ``*ValidationError`` the service raises on
#   the prohibited-attribute path. The test asserts both the class
#   and the ``failed_constraint`` / ``prohibited_keys`` attributes.
# - ``action_type``: the ``Audit_Records.action_type`` to verify is
#   absent after rejection.
# - ``call``: closure that drives one service call with the supplied
#   ``request_attributes`` mapping. Receives the service bundle, the
#   engine, the connection, and the request_attributes mapping; the
#   typed kwargs the closure passes are valid-looking but never
#   inspected because the prohibited-attribute screen fires first
#   (see step 1 of every ``create_<resource>`` method).
# ---------------------------------------------------------------------------


def _call_objective(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["objective"].create_objective(
        conn,
        statement="x",
        rationale=None,
        target_decision_id=_PLACEHOLDER_UUID7,
        authoring_party_id=_PARTY_ID,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_intended_outcome(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["intended_outcome"].create_intended_outcome(
        conn,
        target_objective_id=_PLACEHOLDER_UUID7,
        success_condition="x",
        observation_window=None,
        attribution_assumption=None,
        authoring_party_id=_PARTY_ID,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_project(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["project"].create_project(
        conn,
        target_objective_id=_PLACEHOLDER_UUID7,
        name="x",
        summary=None,
        planned_start_date=date(2026, 1, 1),
        planned_end_date=date(2026, 12, 31),
        authoring_party_id=_PARTY_ID,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_deliverable_expectation(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["deliverable_expectation"].create_deliverable_expectation(
        conn,
        target_project_id=_PLACEHOLDER_UUID7,
        name="x",
        description=None,
        deliverable_kind="Document",
        acceptance_criteria=None,
        authoring_party_id=_PARTY_ID,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_activity_plan(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["activity_plan"].create_activity_plan(
        conn,
        target_project_id=_PLACEHOLDER_UUID7,
        title="x",
        authoring_party_id=_PARTY_ID,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_plan_revision(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["plan_revision"].create_plan_revision(
        conn,
        target_activity_plan_id=_PLACEHOLDER_UUID7,
        planned_scope="x",
        deliverable_expectation_refs=(),
        planning_assumptions=(),
        ordering_rationale=None,
        predecessor_plan_revision_id=None,
        authoring_party_id=_PARTY_ID,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_plan_review(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["plan_review"].create_plan_review(
        conn,
        target_plan_revision_id=_PLACEHOLDER_UUID7,
        outcome="Endorse",
        rationale="x",
        reviewing_party_id=_PARTY_ID,
        authority_basis=_AUTHORITY_BASIS,
        applicable_scope=_SCOPE,
        engine=engine,
        request_attributes=request_attributes,
    )


def _call_plan_approval(
    services: dict[str, Any],
    engine: Engine,
    conn,
    request_attributes: Mapping[str, Any],
) -> None:
    services["plan_approval"].create_plan_approval(
        conn,
        engine,
        target_plan_revision_id=_PLACEHOLDER_UUID7,
        outcome="Approve",
        rationale="x",
        approving_party_id=_PARTY_ID,
        authority_basis=_AUTHORITY_BASIS,
        applicable_scope=_SCOPE,
        omissions=(),
        request_attributes=request_attributes,
    )


_SERVICE_DISPATCH: Final[dict[str, dict[str, Any]]] = {
    "objective": {
        "prefixes": ALL_PROHIBITED_PREFIXES,
        "error_class": ObjectiveValidationError,
        "action_type": "create.objective",
        "call": _call_objective,
    },
    "intended_outcome": {
        # The IntendedOutcomeService's request_attributes screen is
        # scoped to the observed-outcome prefix set (Requirements
        # 13.1 / 13.5); execution and produced-deliverable keys are
        # rejected at the route layer's Pydantic ``extra='forbid'``
        # guard rather than at the service's prohibited-attribute
        # screen. Property 22's "any prohibited prefix" claim still
        # holds end-to-end — see the route-layer screen in
        # :mod:`walking_slice.planning._routes` — but only the
        # observed-outcome subset is reachable through the service
        # surface this test drives, so the strategy draws from that
        # set.
        "prefixes": OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
        "error_class": IntendedOutcomeValidationError,
        "action_type": "create.intended_outcome",
        "call": _call_intended_outcome,
    },
    "project": {
        "prefixes": ALL_PROHIBITED_PREFIXES,
        "error_class": ProjectValidationError,
        "action_type": "create.project",
        "call": _call_project,
    },
    "deliverable_expectation": {
        # Mirrors the IntendedOutcome rationale: the service-level
        # screen is scoped to the produced-deliverable prefix set
        # (Requirements 13.2 / 13.5).
        "prefixes": PRODUCED_DELIVERABLE_PROHIBITED_PREFIXES,
        "error_class": DeliverableExpectationValidationError,
        "action_type": "create.deliverable_expectation",
        "call": _call_deliverable_expectation,
    },
    "activity_plan": {
        "prefixes": ALL_PROHIBITED_PREFIXES,
        "error_class": ActivityPlanValidationError,
        "action_type": "create.activity_plan",
        "call": _call_activity_plan,
    },
    "plan_revision": {
        "prefixes": ALL_PROHIBITED_PREFIXES,
        "error_class": PlanRevisionValidationError,
        "action_type": "create.plan_revision",
        "call": _call_plan_revision,
    },
    "plan_review": {
        "prefixes": ALL_PROHIBITED_PREFIXES,
        "error_class": PlanReviewValidationError,
        "action_type": "create.plan_review",
        "call": _call_plan_review,
    },
    "plan_approval": {
        "prefixes": ALL_PROHIBITED_PREFIXES,
        "error_class": PlanApprovalValidationError,
        "action_type": "create.plan_approval",
        "call": _call_plan_approval,
    },
}


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


# Tail characters appended to a chosen prohibited prefix. ASCII
# alphanumerics plus ``-`` and ``_`` so the matcher's
# hyphen/underscore canonicalization is exercised by both variants.
# Length 0..32 keeps cases cheap while still spanning the full prefix
# matching surface.
_TAIL_ALPHABET: Final[str] = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_"
)


@st.composite
def _prohibited_attribute(draw: Any) -> dict[str, Any]:
    """Draw one ``(service_kind, prohibited_key)`` rejection scenario.

    Steps:

    1. Pick a service kind from the eight Planning_Service write
       surfaces.
    2. Look up the prefix set the service's
       ``request_attributes`` pass-through screens; draw one prefix
       from that set.
    3. Generate a random tail of 0..32 alphanumeric / hyphen /
       underscore characters and concatenate to the prefix.
    4. Optionally swap the case of the resulting key
       (case-insensitive matching is part of the screen contract —
       see :func:`walking_slice.planning._helpers._normalize_key`).

    The returned dict is consumed by
    :func:`test_prohibited_attribute_request_is_rejected_with_no_row_persisted`.
    """
    service_kind = draw(st.sampled_from(sorted(_SERVICE_DISPATCH.keys())))
    spec = _SERVICE_DISPATCH[service_kind]
    prefix = draw(st.sampled_from(spec["prefixes"]))
    tail = draw(
        st.text(alphabet=_TAIL_ALPHABET, min_size=0, max_size=32)
    )
    key = prefix + tail
    case_mode = draw(st.sampled_from(("lower", "upper", "title")))
    if case_mode == "upper":
        key = key.upper()
    elif case_mode == "title":
        key = key.title()
    return {
        "service_kind": service_kind,
        "prohibited_key": key,
    }


# ---------------------------------------------------------------------------
# Test 1 — rejection invariant.
#
# Property 22: every prohibited-prefix request is rejected with no row
# persisted (Requirements 12.1, 12.2, 13.1, 13.2, 13.5, 20.5, 20.6).
# ---------------------------------------------------------------------------


# Feature: second-walking-slice, Property 22: Plan/Execution and Output/Outcome separation
# Validates: Requirements 12.1, 12.2, 13.1, 13.2, 13.5, 20.5, 20.6
@given(scenario=_prohibited_attribute())
@settings(
    max_examples=100,
    deadline=2000,
    # Each case provisions a fresh on-disk SQLite database and
    # builds the full Slice 2 service bundle; per-case setup is
    # slower than a purely in-memory test. The Hypothesis
    # data-generation / slow-test health checks are suppressed so a
    # single slow case does not abort the property run (matching
    # the Property 16 / Property 18 convention).
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_prohibited_attribute_request_is_rejected_with_no_row_persisted(
    scenario: dict[str, Any],
) -> None:
    """For all Planning_Service write surfaces and all prohibited-prefix
    keys, the request is rejected and no Slice 2 row is persisted.

    The prohibited-attribute screen (step 1 of every
    ``create_<resource>`` method) raises a ``*ValidationError`` with
    ``failed_constraint='prohibited_attribute'`` and the offending
    key on :attr:`prohibited_keys`. The caller's transaction rolls
    back; every Slice 2 planning Resource / Revision table remains
    empty and ``Audit_Records`` carries no row for the action's
    ``action_type``.
    """
    service_kind: str = scenario["service_kind"]
    prohibited_key: str = scenario["prohibited_key"]
    spec = _SERVICE_DISPATCH[service_kind]

    with tempfile.TemporaryDirectory(prefix="prop22_reject_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            services = _build_services()

            # The request_attributes mapping the route layer would
            # forward to the service. The ``statement`` key (and its
            # value) is an arbitrary non-prohibited placeholder — the
            # screen iterates keys, not values, so only the
            # ``prohibited_key`` entry matters.
            request_attributes: dict[str, Any] = {
                "statement": "ignored",
                prohibited_key: "prohibited-value",
            }

            with pytest.raises(spec["error_class"]) as exc_info:
                with engine.begin() as conn:
                    spec["call"](
                        services, engine, conn, request_attributes
                    )

            # The error must carry the structured discriminator and
            # the offending key so Requirements 12.2 / 13.5 hold.
            assert exc_info.value.failed_constraint == (
                "prohibited_attribute"
            ), (
                "Property 22 violated: the service raised "
                f"{type(exc_info.value).__name__} but with "
                f"failed_constraint="
                f"{exc_info.value.failed_constraint!r} (expected "
                "'prohibited_attribute'). The rejected request was: "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}."
            )
            assert prohibited_key in exc_info.value.prohibited_keys, (
                "Property 22 violated: the prohibited key was not "
                "surfaced on the error's prohibited_keys attribute. "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}, "
                f"reported={exc_info.value.prohibited_keys!r}."
            )

            # No row landed in any Slice 2 planning Resource or
            # Revision table — Property 22's "no persisted Planning
            # Resource carries any such attribute" clause.
            for table in _PLANNING_RESOURCE_TABLES:
                assert _count(engine, table) == 0, (
                    f"Property 22 violated: rejected request "
                    f"persisted a row in {table!r}. "
                    f"service_kind={service_kind!r}, "
                    f"prohibited_key={prohibited_key!r}."
                )

            # No audit row was appended for this action_type. The
            # prohibited-attribute screen runs before authorization
            # evaluation, so neither a ``permit`` row nor a
            # ``consequential`` row exists; the deny path is not
            # reached either because the screen short-circuits.
            assert (
                _count_audit_rows(engine, spec["action_type"]) == 0
            ), (
                "Property 22 violated: rejected request appended an "
                f"Audit_Records row with action_type="
                f"{spec['action_type']!r}. "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}."
            )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Test 2 — outcome_kind invariant.
#
# Property 22: every persisted Intended Outcome Revision carries
# ``outcome_kind = 'intended'`` (Requirements 3.3, 13.3, 13.4).
# ---------------------------------------------------------------------------


# Alphabet used for valid (non-prohibited) text fields in the
# happy-path Intended Outcome strategy. Mirrors the convention from
# Property 16 (printable ASCII plus a handful of common Latin
# extras); the property is not about UTF-8 robustness, so a narrower
# alphabet keeps shrunken counterexamples readable.
_VALID_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


_intended_outcome_strategy = st.fixed_dictionaries(
    {
        "success_condition": st.text(
            alphabet=_VALID_TEXT_ALPHABET, min_size=1, max_size=200
        ),
        "observation_window": st.one_of(
            st.none(),
            st.text(
                alphabet=_VALID_TEXT_ALPHABET, min_size=0, max_size=200
            ),
        ),
        "attribution_assumption": st.one_of(
            st.none(),
            st.text(
                alphabet=_VALID_TEXT_ALPHABET, min_size=0, max_size=500
            ),
        ),
    }
)


def _seed_party(engine: Engine, party_id: str, display: str) -> None:
    """Insert one Party row required by the FK constraints."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Parties (party_id, kind, display_name, created_at)
                VALUES (:pid, 'person', :name, :ts)
                """
            ),
            {"pid": party_id, "name": display, "ts": _NOW_ISO},
        )


def _assign_modify_role(
    authorization_service: AuthorizationService, engine: Engine
) -> None:
    """Grant ``modify`` over ``_SCOPE`` to the actor Party.

    ``modify`` is the authority required for
    ``create.intended_outcome`` per AD-WS-15. The role-assignment
    effective period generously brackets the fixed clock instant.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="intended_outcome_owner",
        scope=_SCOPE,
        authorities_granted=("modify",),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_objective_row(engine: Engine) -> None:
    """Insert one ``Objectives`` row by hand for the happy-path test."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _NOW_ISO},
        )


# Feature: second-walking-slice, Property 22: Plan/Execution and Output/Outcome separation
# Validates: Requirements 3.3, 13.3, 13.4
@given(payload=_intended_outcome_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_persisted_intended_outcome_revision_carries_outcome_kind_intended(
    payload: dict[str, Any],
) -> None:
    """For all authorized, valid Intended Outcome creation requests:

    - the persisted ``Intended_Outcome_Revisions`` row carries
      ``outcome_kind = 'intended'`` (Requirements 3.3, 13.3 /
      Property 22's "every persisted Intended Outcome Revision row
      carries ``outcome_kind = 'intended'``"),
    - the returned :class:`CreateIntendedOutcomeResult` carries the
      same literal so the response body distinguishes the row from
      an Observed Outcome (Requirement 13.4 / Property 22's "every
      response body for an Intended Outcome distinguishes it from
      an Observed Outcome").
    """
    with tempfile.TemporaryDirectory(prefix="prop22_kind_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            services = _build_services()
            # Seed only the rows the create call's prerequisites
            # need: the actor Party, the assigning-authority Party,
            # the modify-authority role assignment, and the target
            # Objective.
            _seed_party(engine, _PARTY_ID, "Property 22 Actor")
            _seed_party(
                engine,
                _ASSIGNING_AUTHORITY_ID,
                "Property 22 Resource Steward",
            )
            _assign_modify_role(services["authorization_service"], engine)
            _seed_objective_row(engine)

            with engine.begin() as conn:
                result = services["intended_outcome"].create_intended_outcome(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    success_condition=payload["success_condition"],
                    observation_window=payload["observation_window"],
                    attribution_assumption=payload["attribution_assumption"],
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            # The returned result distinguishes the Intended Outcome
            # from an Observed Outcome (Requirement 13.4).
            assert result.outcome_kind == "intended", (
                "Property 22 violated: CreateIntendedOutcomeResult."
                "outcome_kind is not the literal 'intended'. "
                f"got={result.outcome_kind!r}."
            )

            # The persisted row carries the same literal
            # (Requirement 13.3 / persistence invariant).
            with engine.connect() as conn:
                persisted_kind = conn.execute(
                    text(
                        "SELECT outcome_kind FROM "
                        "Intended_Outcome_Revisions "
                        "WHERE intended_outcome_revision_id = :rid"
                    ),
                    {"rid": result.intended_outcome_revision_id},
                ).scalar_one()
            assert persisted_kind == "intended", (
                "Property 22 violated: persisted "
                "Intended_Outcome_Revisions.outcome_kind is not "
                f"'intended'. got={persisted_kind!r}, "
                f"revision_id={result.intended_outcome_revision_id!r}."
            )
        finally:
            engine.dispose()


# ---------------------------------------------------------------------------
# Test 3 — response-body invariant.
#
# Property 22: no response body for a planning Resource includes a
# derived current-execution status, percent-complete value,
# actual-cost value, or remaining-work value (Requirement 12.4).
# ---------------------------------------------------------------------------


# The eight result dataclasses returned by every Planning_Service
# write. The response-body invariant test asserts none of these
# carries a field whose canonicalized name starts with a
# derived-execution prefix from
# :data:`_DERIVED_EXECUTION_FIELD_PREFIXES`.
_PLANNING_RESULT_CLASSES: Final[tuple[type, ...]] = (
    CreateObjectiveResult,
    CreateIntendedOutcomeResult,
    CreateProjectResult,
    CreateDeliverableExpectationResult,
    CreateActivityPlanResult,
    CreatePlanRevisionResult,
    CreatePlanReviewResult,
    CreatePlanApprovalResult,
)


def _canonicalize_field_name(name: str) -> str:
    """Hyphen/underscore-invariant, lowercase canonical form.

    Matches the convention used by
    :func:`walking_slice.planning._helpers._normalize_key` so the
    matcher in :func:`test_planning_resource_result_carries_no_derived_execution_value`
    behaves identically to the API-boundary screen.
    """
    return name.lower().replace("_", "-")


# Feature: second-walking-slice, Property 22: Plan/Execution and Output/Outcome separation
# Validates: Requirement 12.4
@given(
    result_class=st.sampled_from(_PLANNING_RESULT_CLASSES),
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_planning_resource_result_carries_no_derived_execution_value(
    result_class: type,
) -> None:
    """For all planning Resource result dataclasses, no declared field
    name matches a derived-execution prefix (current-execution-status,
    percent-complete, actual-cost, remaining-work).

    The result dataclasses are the literal response bodies the HTTP
    layer serializes (the route handlers in
    :mod:`walking_slice.planning._routes` call
    :func:`dataclasses.asdict` or rely on Pydantic
    ``model_validate(result)`` on the matching response model). The
    response body carries no derived-execution value iff the
    underlying result dataclass declares no such field.
    """
    fields = dataclasses.fields(result_class)
    canonical_names = [
        _canonicalize_field_name(field.name) for field in fields
    ]
    for name in canonical_names:
        for prefix in _DERIVED_EXECUTION_FIELD_PREFIXES:
            assert not name.startswith(prefix), (
                "Property 22 violated: "
                f"{result_class.__name__} declares a field "
                f"{name!r} matching derived-execution prefix "
                f"{prefix!r}. No response body for a planning "
                "Resource may include a derived current-execution "
                "status, percent-complete value, actual-cost value, "
                "or remaining-work value (Requirement 12.4)."
            )
