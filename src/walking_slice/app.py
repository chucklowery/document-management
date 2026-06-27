"""FastAPI application composition for the first walking slice (task 15.2).

Design reference: ``.kiro/specs/first-walking-slice/design.md``
§"Application-Level Composition" and AD-WS-1 (single SQLite engine,
constructor injection through a ``RequestContext`` bundle). The
Slice 2 wiring (task 15.2 in ``.kiro/specs/second-walking-slice``)
extends this module additively to load the Planning_Service router
and singletons; the Slice 1 startup behavior is otherwise unchanged.

Responsibilities (per task 15.2):

1. Expose :func:`create_app` — a factory that returns a fully wired
   :class:`fastapi.FastAPI` instance. The factory accepts an optional
   ``engine``; when omitted, a SQLAlchemy Core engine is constructed
   from the ``WALKING_SLICE_DATABASE_URL`` environment variable
   (default ``sqlite:///./walking_slice.db``).
2. On startup, install the SQLite pragmas (``journal_mode=WAL`` and
   ``foreign_keys=ON``) via :func:`walking_slice.persistence.install_pragmas`,
   create every table / trigger / index by calling
   :func:`walking_slice.persistence.create_schema`, then seed the
   Interim ADR records (:func:`walking_slice.interim_adr.seed`) and
   the default Completeness Disclosure policy
   (:func:`walking_slice.disclosure.seed`).
3. Build every service the routes depend on — :class:`Clock`,
   :class:`AuditLog`, :class:`IdentityService`, :class:`AuthorizationService`,
   :class:`EvidenceRepository`, :class:`KnowledgeService`,
   :class:`TrailService`, :class:`ProvenanceManifestWriter`, and
   :class:`ProvenanceNavigator` (wired with the
   ``slice-default-2026`` :class:`DisclosurePolicy` from
   :mod:`walking_slice.disclosure`) — sharing the single
   :class:`Clock` and :class:`AuditLog` instances across all of them
   so every artifact of one transaction shares one timestamp.
4. Mount the :class:`fastapi.APIRouter` from every ``routes/*.py``
   module and wire each module's local ``get_engine`` /
   ``get_<service>`` factories through
   :attr:`fastapi.FastAPI.dependency_overrides` so the same engine
   and service singletons reach every endpoint.
5. Mount the :class:`RequestContextResolver` (task 15.1) as a
   FastAPI dependency at the constant
   :data:`walking_slice.app.get_request_context` symbol so routes
   migrated off the placeholder ``X-Actor-Party-Id`` header in a
   later task can resolve a ``RequestContext`` from a bearer token
   without re-wiring the app.
6. Expose a simple ``GET /healthz`` endpoint so smoke tests and
   container orchestration can confirm the app is up before exercising
   the heavier routes.

Requirements satisfied (per task 15.2):
    16.1 — the slice runs as a single FastAPI service backed by a
           SQLite database (WAL journal mode) accessed through
           SQLAlchemy Core.
    16.3 — Interim ADR records are seeded on startup and remain
           retrievable by backlog ADR identifier (the seed itself is
           implemented in :mod:`walking_slice.interim_adr`; this
           module invokes it from the FastAPI startup hook).

Production deployments wire the resulting app behind ``uvicorn``::

    uvicorn walking_slice.app:create_app --factory --host 0.0.0.0

Tests construct ``create_app(engine=<per-test engine>)`` so the
per-test SQLite file fixture remains the source of truth for state
isolation (the same pattern used by every existing route end-to-end
test).
"""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator, Final, Optional

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.auth_middleware import RequestContext, RequestContextResolver
from walking_slice.authorization import AuthorizationService
from walking_slice.clock import Clock, SystemClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.disclosure import (
    SLICE_DEFAULT_POLICY_ID,
    get_policy as get_disclosure_policy,
    seed as seed_disclosure_policy,
)
from walking_slice.evidence import EvidenceRepository
from walking_slice.execution._disclosure import seed_execution_coverage
from walking_slice.execution._interim_adr import seed_execution_interim_adr
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution._projection import execution_projection_registry
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
from walking_slice.knowledge import KnowledgeService
from walking_slice.manifests import ProvenanceManifestWriter
from walking_slice.persistence import create_schema, install_pragmas
from walking_slice.planning._disclosure import seed_planning_coverage
from walking_slice.planning._interim_adr import seed_planning_interim_adr
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning._projection import planning_projection_registry
from walking_slice.planning.activity_plans import ActivityPlanService
from walking_slice.planning.deliverable_expectations import (
    DeliverableExpectationService,
)
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.objectives import ObjectiveService
from walking_slice.planning.plan_approvals import PlanApprovalService
from walking_slice.planning.plan_reviews import PlanReviewService
from walking_slice.planning.plan_revisions import PlanRevisionService
from walking_slice.planning.projects import ProjectService
from walking_slice.outcome._disclosure import seed_outcome_coverage
from walking_slice.outcome._interim_adr import seed_outcome_interim_adr
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome._provenance import register_outcome_navigation
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import OutcomeReviewService
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.projection import StatusProjector
from walking_slice.provenance import ProvenanceNavigator
from walking_slice.routes import (
    decisions as decisions_routes,
    evidence as evidence_routes,
    findings as findings_routes,
    provenance as provenance_routes,
    recommendations as recommendations_routes,
    roles as roles_routes,
    trails as trails_routes,
)
from walking_slice.trails import TrailService


__all__ = [
    "DATABASE_URL_ENV",
    "DEFAULT_DATABASE_URL",
    "JWT_SECRET_ENV",
    "SliceServices",
    "create_app",
    "create_default_engine",
    "get_request_context",
]


# ---------------------------------------------------------------------------
# Public constants and environment variable names.
#
# The default database URL points at an on-disk SQLite file so a container
# restart does not silently wipe state. Tests always pass ``engine=`` so
# this default is only consulted by production-like deployments.
# ---------------------------------------------------------------------------

#: Name of the environment variable consulted when ``engine`` is omitted.
DATABASE_URL_ENV: Final[str] = "WALKING_SLICE_DATABASE_URL"

#: Fallback database URL used when neither ``engine`` nor the environment
#: variable supplies one. An on-disk SQLite file so process restarts do
#: not silently lose state; production deployments should override.
DEFAULT_DATABASE_URL: Final[str] = "sqlite:///./walking_slice.db"

#: Name of the environment variable carrying the slice-local JWT signing
#: key. When unset the factory mints a fresh 32-byte secret per process —
#: that secret is *not* persisted, so tokens minted by one process cannot
#: be verified by another. Production deployments must set the variable
#: to a stable value.
JWT_SECRET_ENV: Final[str] = "WALKING_SLICE_JWT_SECRET"


# ---------------------------------------------------------------------------
# Dependency placeholder for the RequestContext.
#
# Routes migrated off the placeholder ``X-Actor-Party-Id`` header in a
# later task can write ``ctx: RequestContext = Depends(get_request_context)``
# in their handler signatures; ``create_app`` overrides this symbol to a
# fully-wired :class:`RequestContextResolver` so the dependency resolves
# without any per-route re-wiring.
# ---------------------------------------------------------------------------


def get_request_context() -> RequestContext:
    """Placeholder dependency resolved to :class:`RequestContextResolver` by ``create_app``.

    The unwrapped function exists only to give FastAPI a stable symbol to
    key ``dependency_overrides`` against; calling it directly is a wiring
    error and raises immediately so the test surface fails loudly rather
    than silently returning ``None``.
    """
    raise NotImplementedError(
        "walking_slice.app.get_request_context must be overridden by "
        "create_app; do not depend on the unwrapped function directly."
    )


# ---------------------------------------------------------------------------
# Service bundle.
# ---------------------------------------------------------------------------


class SliceServices:
    """Per-app singletons constructed once by :func:`create_app`.

    Holding them on a single object lets tests reach into the app to
    grab, say, the :class:`KnowledgeService` for direct assertions
    without re-deriving it from the engine. The instance is attached
    to ``app.state.services`` so route handlers and tests both reach
    the same object.

    Attributes match the wiring graph in design §"Application-Level
    Composition": one :class:`Clock`, one :class:`AuditLog`, one
    :class:`IdentityService`, one :class:`AuthorizationService`, then
    the per-bounded-context services that depend on them.
    """

    __slots__ = (
        "engine",
        "clock",
        "audit_log",
        "identity_service",
        "authorization_service",
        "manifest_writer",
        "evidence_repository",
        "knowledge_service",
        "trail_service",
        "provenance_navigator",
        "request_context_resolver",
        # Slice 2 (Planning_Service) singletons. Each service is wired
        # with the shared :class:`Clock`, :class:`AuditLog`,
        # :class:`IdentityService`, and :class:`AuthorizationService`
        # so every Planning_Service artifact of a transaction shares
        # one timestamp and one audit row sequence with the Slice 1
        # artifacts that the same transaction may touch.
        "objective_service",
        "intended_outcome_service",
        "project_service",
        "deliverable_expectation_service",
        "activity_plan_service",
        "plan_revision_service",
        "plan_review_service",
        "plan_approval_service",
        # Slice 3 (Execution_Service + Deliverable_Repository)
        # singletons. Each service is wired with the same shared
        # :class:`Clock`, :class:`AuditLog`, :class:`IdentityService`,
        # and :class:`AuthorizationService` so Slice 3 writes share
        # one timestamp and one audit row sequence with any Slice 1
        # / Slice 2 collaborator (AD-WS-26: every consequential
        # artifact of a transaction shares the same ``recorded_at``).
        # The seven Execution_Service singletons and the one
        # Deliverable_Repository singleton are connection-scoped at
        # call time, so a single instance per service safely serves
        # every request.
        "project_resolver",
        "deliverable_repository_service",
        "work_assignment_service",
        "work_event_service",
        "time_entry_service",
        "deliverable_production_service",
        "milestone_acceptance_service",
        "completion_service",
        # Slice 4 (Outcome_Service) singletons. Each service is wired
        # with the same shared :class:`Clock`, :class:`AuditLog`,
        # :class:`IdentityService`, and :class:`AuthorizationService`
        # as the prior slices (AD-WS-32 / design §"Cross-Cutting
        # Concerns": every consequential artifact of a transaction
        # shares one ``recorded_at`` and one audit row sequence). Each
        # Outcome_Service is connection-scoped at call time (AD-WS-5),
        # so a single instance per service safely serves every request.
        # The five services are constructed in dependency order:
        # MeasurementDefinition → MeasurementRecord → ObservedOutcome →
        # SuccessConditionAssessment → OutcomeReview, each taking the
        # prior-slice readers (IntendedOutcomeService, CompletionService,
        # DeliverableRepositoryService) and earlier Slice 4 services as
        # read-only collaborators (AD-WS-40).
        "measurement_definition_service",
        "measurement_record_service",
        "observed_outcome_service",
        "success_condition_assessment_service",
        "outcome_review_service",
        # Shared :class:`StatusProjector` carrying the union of every
        # registered Projection Definition across slices (currently
        # Planning + Execution). The trails service constructs its
        # own ad-hoc projector when needed; this one is the
        # production singleton consumed by the Slice 3
        # execution-status endpoint.
        "status_projector",
    )

    def __init__(
        self,
        *,
        engine: Engine,
        clock: Clock,
        audit_log: AuditLog,
        identity_service: IdentityService,
        authorization_service: AuthorizationService,
        manifest_writer: ProvenanceManifestWriter,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
        trail_service: TrailService,
        provenance_navigator: ProvenanceNavigator,
        request_context_resolver: RequestContextResolver,
        objective_service: ObjectiveService,
        intended_outcome_service: IntendedOutcomeService,
        project_service: ProjectService,
        deliverable_expectation_service: DeliverableExpectationService,
        activity_plan_service: ActivityPlanService,
        plan_revision_service: PlanRevisionService,
        plan_review_service: PlanReviewService,
        plan_approval_service: PlanApprovalService,
        project_resolver: ProjectResolver,
        deliverable_repository_service: DeliverableRepositoryService,
        work_assignment_service: WorkAssignmentService,
        work_event_service: WorkEventService,
        time_entry_service: TimeEntryService,
        deliverable_production_service: DeliverableProductionService,
        milestone_acceptance_service: MilestoneAcceptanceService,
        completion_service: CompletionService,
        measurement_definition_service: MeasurementDefinitionService,
        measurement_record_service: MeasurementRecordService,
        observed_outcome_service: ObservedOutcomeService,
        success_condition_assessment_service: SuccessConditionAssessmentService,
        outcome_review_service: OutcomeReviewService,
        status_projector: StatusProjector,
    ) -> None:
        self.engine = engine
        self.clock = clock
        self.audit_log = audit_log
        self.identity_service = identity_service
        self.authorization_service = authorization_service
        self.manifest_writer = manifest_writer
        self.evidence_repository = evidence_repository
        self.knowledge_service = knowledge_service
        self.trail_service = trail_service
        self.provenance_navigator = provenance_navigator
        self.request_context_resolver = request_context_resolver
        self.objective_service = objective_service
        self.intended_outcome_service = intended_outcome_service
        self.project_service = project_service
        self.deliverable_expectation_service = deliverable_expectation_service
        self.activity_plan_service = activity_plan_service
        self.plan_revision_service = plan_revision_service
        self.plan_review_service = plan_review_service
        self.plan_approval_service = plan_approval_service
        self.project_resolver = project_resolver
        self.deliverable_repository_service = deliverable_repository_service
        self.work_assignment_service = work_assignment_service
        self.work_event_service = work_event_service
        self.time_entry_service = time_entry_service
        self.deliverable_production_service = deliverable_production_service
        self.milestone_acceptance_service = milestone_acceptance_service
        self.completion_service = completion_service
        self.measurement_definition_service = measurement_definition_service
        self.measurement_record_service = measurement_record_service
        self.observed_outcome_service = observed_outcome_service
        self.success_condition_assessment_service = (
            success_condition_assessment_service
        )
        self.outcome_review_service = outcome_review_service
        self.status_projector = status_projector


# ---------------------------------------------------------------------------
# Engine construction helper.
# ---------------------------------------------------------------------------


def create_default_engine(database_url: Optional[str] = None) -> Engine:
    """Construct the default SQLAlchemy engine for the slice.

    Resolves the connection string in this order:

    1. The explicit ``database_url`` argument.
    2. The ``WALKING_SLICE_DATABASE_URL`` environment variable.
    3. :data:`DEFAULT_DATABASE_URL`.

    The returned engine has :func:`install_pragmas` registered so every
    connection opened against it applies ``journal_mode=WAL`` and
    ``foreign_keys=ON`` per AD-WS-1 / Requirement 16.2. Callers that
    construct their own engine (e.g. tests) should call
    :func:`install_pragmas` themselves or use the per-test ``engine``
    fixture in :mod:`tests.conftest`, which sets the pragmas via its own
    ``connect`` event listener.
    """
    url = database_url or os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
    engine = create_engine(url, future=True)
    install_pragmas(engine)
    return engine


# ---------------------------------------------------------------------------
# Service wiring.
# ---------------------------------------------------------------------------


def _build_services(
    engine: Engine,
    *,
    clock: Optional[Clock] = None,
    jwt_secret: Optional[bytes] = None,
) -> SliceServices:
    """Construct every service singleton bound to ``engine``.

    The function intentionally accepts an explicit ``clock`` and
    ``jwt_secret`` so tests can pin both for deterministic behaviour.
    Production callers leave them as ``None`` and the factory falls
    back to :class:`SystemClock` and (when ``WALKING_SLICE_JWT_SECRET``
    is unset) a per-process random secret.
    """
    chosen_clock: Clock = clock if clock is not None else SystemClock()

    audit_log = AuditLog(chosen_clock)
    identity_service = IdentityService(
        engine=engine,
        audit_log=audit_log,
        clock=chosen_clock,
    )
    authorization_service = AuthorizationService(
        clock=chosen_clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    manifest_writer = ProvenanceManifestWriter(
        clock=chosen_clock,
        identity_service=identity_service,
    )
    evidence_repository = EvidenceRepository(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )
    knowledge_service = KnowledgeService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        manifest_writer=manifest_writer,
    )
    trail_service = TrailService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        manifest_writer=manifest_writer,
    )
    # Look up the seeded ``slice-default-2026`` policy so the navigator
    # can apply the AD-WS-9 redaction marker and gap descriptor rules
    # without a per-request database round-trip. The policy table is
    # insert-and-supersede (never updated in place), so caching one
    # row at app construction is safe.
    disclosure_policy = get_disclosure_policy(engine, SLICE_DEFAULT_POLICY_ID)
    provenance_navigator = ProvenanceNavigator(
        clock=chosen_clock,
        authorization_service=authorization_service,
        disclosure_policy=disclosure_policy,
    )

    # Resolve the JWT signing secret. Production deployments set the
    # environment variable; tests pin it explicitly. When neither is
    # supplied we mint a per-process value so the resolver remains
    # functional in a smoke-test deployment, but with the obvious
    # caveat that tokens minted by one process cannot be verified by
    # another.
    chosen_secret: bytes
    if jwt_secret is not None:
        chosen_secret = jwt_secret
    else:
        env_secret = os.environ.get(JWT_SECRET_ENV)
        if env_secret:
            chosen_secret = env_secret.encode("utf-8")
        else:
            chosen_secret = secrets.token_bytes(32)

    request_context_resolver = RequestContextResolver(
        secret=chosen_secret,
        clock=chosen_clock,
        engine=engine,
        ids=identity_service,
        authz=authorization_service,
        audit=audit_log,
    )

    # ------------------------------------------------------------------
    # Slice 2 (Planning_Service) singletons.
    #
    # Each service is a frozen dataclass per design §"Planning_Service.*"
    # that bundles the Slice 1 cross-request collaborators wired above.
    # Constructing them once at app composition keeps the wiring single-
    # writer-safe (AD-WS-5): the routes pass per-request connections
    # into the service methods, so the singletons themselves do not own
    # any connection state.
    #
    # ObjectiveService additionally takes :class:`KnowledgeService` for
    # the AD-WS-21 Decision resolution path; PlanApprovalService
    # additionally takes :class:`ProvenanceManifestWriter` for the
    # AD-WS-20 manifest emission.
    # ------------------------------------------------------------------
    objective_service = ObjectiveService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        knowledge_service=knowledge_service,
    )
    intended_outcome_service = IntendedOutcomeService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    project_service = ProjectService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    deliverable_expectation_service = DeliverableExpectationService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    activity_plan_service = ActivityPlanService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    plan_revision_service = PlanRevisionService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    plan_review_service = PlanReviewService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    plan_approval_service = PlanApprovalService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        manifest_writer=manifest_writer,
    )

    # ------------------------------------------------------------------
    # Slice 3 (Execution_Service + Deliverable_Repository) singletons.
    #
    # The seven Execution_Service services and the one
    # Deliverable_Repository service follow the same connection-scoped
    # pattern as the Slice 2 services (AD-WS-5): each public method
    # accepts the caller's :class:`sqlalchemy.engine.Connection` and
    # writes inside the caller's transaction, so the service instances
    # themselves hold no per-request state and a single instance per
    # service safely serves every request.
    #
    # Construction order matches the dependency graph from design
    # §"Execution_Service.*":
    #
    # 1. :class:`ProjectResolver` is stateless and depends on nothing.
    # 2. :class:`DeliverableRepositoryService` only needs the Slice 1
    #    cross-request collaborators.
    # 3. :class:`WorkAssignmentService` additionally needs the
    #    :class:`PlanRevisionService` constructed above (AD-WS-30 —
    #    Slice 3 reads through Planning_Service public APIs).
    # 4. :class:`WorkEventService` and :class:`TimeEntryService` only
    #    need the Slice 1 cross-request collaborators.
    # 5. :class:`DeliverableProductionService` needs the
    #    :class:`DeliverableRepositoryService` (to resolve produced
    #    Deliverable Revisions), the :class:`DeliverableExpectationService`
    #    (to resolve target Expectation Revisions), and the
    #    :class:`ProjectResolver` (to walk Plan Revision → Project).
    # 6. :class:`MilestoneAcceptanceService` needs the
    #    :class:`DeliverableProductionService` constructed above.
    # 7. :class:`CompletionService` needs the
    #    :class:`PlanRevisionService` and the
    #    :class:`ProjectResolver`.
    # ------------------------------------------------------------------
    project_resolver = ProjectResolver()
    deliverable_repository_service = DeliverableRepositoryService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    work_assignment_service = WorkAssignmentService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_service,
    )
    work_event_service = WorkEventService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    time_entry_service = TimeEntryService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )
    deliverable_production_service = DeliverableProductionService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        deliverable_reader=deliverable_repository_service,
        planning_reader=deliverable_expectation_service,
        project_resolver=project_resolver,
    )
    milestone_acceptance_service = MilestoneAcceptanceService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        production_reader=deliverable_production_service,
    )
    completion_service = CompletionService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_service,
        project_resolver=project_resolver,
    )

    # ------------------------------------------------------------------
    # Slice 4 (Outcome_Service) singletons.
    #
    # The five Outcome_Service services follow the same connection-scoped
    # pattern as the Slice 2 / Slice 3 services (AD-WS-5): each public
    # method accepts the caller's :class:`sqlalchemy.engine.Connection`
    # and writes inside the caller's transaction, so the service
    # instances hold no per-request state and a single instance per
    # service safely serves every request. Every service shares the same
    # :class:`Clock`, :class:`AuditLog`, :class:`IdentityService`, and
    # :class:`AuthorizationService` as the prior slices so every Slice 4
    # artifact of a transaction shares one ``recorded_at`` and one audit
    # row sequence with any Slice 1 / Slice 2 / Slice 3 collaborator the
    # same transaction touches (AD-WS-32).
    #
    # Construction order matches the dependency graph from design
    # §"Outcome_Service.*" — each service takes the earlier Slice 4
    # services and the prior-slice readers as read-only collaborators
    # (AD-WS-40: Slice 4 reads through prior-slice public APIs):
    #
    # 1. :class:`MeasurementDefinitionService` reads the Slice 2
    #    :class:`IntendedOutcomeService` to resolve / verify the target
    #    Intended Outcome Revision (``outcome_kind = 'intended'``).
    # 2. :class:`MeasurementRecordService` reads the
    #    :class:`MeasurementDefinitionService` to resolve the target
    #    Measurement Definition Revision.
    # 3. :class:`ObservedOutcomeService` reads the
    #    :class:`IntendedOutcomeService`, the
    #    :class:`MeasurementRecordService`, and the
    #    :class:`MeasurementDefinitionService`.
    # 4. :class:`SuccessConditionAssessmentService` reads the
    #    :class:`IntendedOutcomeService` and the
    #    :class:`ObservedOutcomeService`.
    # 5. :class:`OutcomeReviewService` reads the
    #    :class:`IntendedOutcomeService`, the
    #    :class:`SuccessConditionAssessmentService`, the Slice 3
    #    :class:`CompletionService`, and the Slice 3
    #    :class:`DeliverableRepositoryService`.
    # ------------------------------------------------------------------
    measurement_definition_service = MeasurementDefinitionService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
    )
    measurement_record_service = MeasurementRecordService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        definition_reader=measurement_definition_service,
    )
    observed_outcome_service = ObservedOutcomeService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        measurement_reader=measurement_record_service,
        definition_reader=measurement_definition_service,
    )
    success_condition_assessment_service = SuccessConditionAssessmentService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        observed_outcome_reader=observed_outcome_service,
    )
    outcome_review_service = OutcomeReviewService(
        clock=chosen_clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        intended_outcome_reader=intended_outcome_service,
        assessment_reader=success_condition_assessment_service,
        completion_reader=completion_service,
        deliverable_reader=deliverable_repository_service,
    )

    # ------------------------------------------------------------------
    # Shared :class:`StatusProjector`.
    #
    # The projector carries the union of every registered Projection
    # Definition: the Slice 2 Planning registry plus the Slice 3
    # Execution registry. Both registries return a fresh dict on each
    # call so merging them locally is safe — the projector internally
    # copies the mapping on construction so a later mutation on either
    # source dict cannot leak into the projector's registry.
    #
    # The Slice 1 Trail Projection Definition is not registered here:
    # the trails route module does not depend on the projector
    # (only the ``create_trail_projected`` service method does, and
    # tests construct an ad-hoc projector for that path). Future work
    # that mounts a Trail-projected HTTP endpoint will extend this
    # union with the Trail registry without changing any other site.
    # ------------------------------------------------------------------
    projection_definitions = {
        **planning_projection_registry(),
        **execution_projection_registry(),
    }
    status_projector = StatusProjector(
        clock=chosen_clock,
        definition_registry=projection_definitions,
    )

    return SliceServices(
        engine=engine,
        clock=chosen_clock,
        audit_log=audit_log,
        identity_service=identity_service,
        authorization_service=authorization_service,
        manifest_writer=manifest_writer,
        evidence_repository=evidence_repository,
        knowledge_service=knowledge_service,
        trail_service=trail_service,
        provenance_navigator=provenance_navigator,
        request_context_resolver=request_context_resolver,
        objective_service=objective_service,
        intended_outcome_service=intended_outcome_service,
        project_service=project_service,
        deliverable_expectation_service=deliverable_expectation_service,
        activity_plan_service=activity_plan_service,
        plan_revision_service=plan_revision_service,
        plan_review_service=plan_review_service,
        plan_approval_service=plan_approval_service,
        project_resolver=project_resolver,
        deliverable_repository_service=deliverable_repository_service,
        work_assignment_service=work_assignment_service,
        work_event_service=work_event_service,
        time_entry_service=time_entry_service,
        deliverable_production_service=deliverable_production_service,
        milestone_acceptance_service=milestone_acceptance_service,
        completion_service=completion_service,
        measurement_definition_service=measurement_definition_service,
        measurement_record_service=measurement_record_service,
        observed_outcome_service=observed_outcome_service,
        success_condition_assessment_service=(
            success_condition_assessment_service
        ),
        outcome_review_service=outcome_review_service,
        status_projector=status_projector,
    )


# ---------------------------------------------------------------------------
# App factory.
# ---------------------------------------------------------------------------


def create_app(
    engine: Optional[Engine] = None,
    *,
    clock: Optional[Clock] = None,
    jwt_secret: Optional[bytes] = None,
    database_url: Optional[str] = None,
) -> FastAPI:
    """Build and return the slice's FastAPI application.

    The factory performs five jobs in order:

    1. Construct (or accept) the SQLAlchemy :class:`Engine`. When
       ``engine`` is ``None`` the factory consults
       ``WALKING_SLICE_DATABASE_URL`` (and falls back to
       :data:`DEFAULT_DATABASE_URL`); when an engine is supplied,
       :func:`install_pragmas` is called so the ``WAL`` + foreign-key
       pragmas apply even if the caller did not set them.
    2. Call :func:`create_schema` to apply every ``CREATE TABLE``,
       ``CREATE INDEX``, and ``CREATE TRIGGER`` statement from
       :mod:`walking_slice.persistence` (idempotent — safe to call
       against an already-initialised database).
    3. Seed the Interim ADR records (G-1..G-5) via
       :func:`walking_slice.interim_adr.seed` and the default
       Completeness Disclosure policy via
       :func:`walking_slice.disclosure.seed`. Both seeds use
       ``INSERT OR IGNORE`` so repeat invocations are safe (production
       containers may bring multiple processes up against the same
       SQLite file).
    4. Build every service singleton — sharing the single :class:`Clock`
       and :class:`AuditLog` instances across them — and capture them
       on ``app.state.services`` so tests and operator tooling can
       reach them without re-deriving the wiring.
    5. Mount every ``routes/*.py`` router and override each module's
       local ``get_engine`` / ``get_<service>`` factories on the app's
       dependency-override map. Each route module declares its own
       ``get_engine`` symbol, so the overrides target seven distinct
       callables (one per ``routes/*.py``); listing them explicitly
       keeps the wiring grep-friendly.

    Args:
        engine: Optional SQLAlchemy engine. When ``None``, the factory
            constructs one via :func:`create_default_engine`.
        clock: Optional :class:`Clock` used by every service. When
            ``None``, defaults to :class:`SystemClock`.
        jwt_secret: Optional HMAC-SHA256 signing key for the bearer
            token surface. When ``None``, the factory consults
            ``WALKING_SLICE_JWT_SECRET`` and falls back to a fresh
            per-process random secret.
        database_url: Optional connection URL used only when ``engine``
            is also ``None``. Provided as a convenience for tests that
            want to point the factory at a temp-file URL without
            constructing the engine themselves.

    Returns:
        A :class:`fastapi.FastAPI` instance with every route mounted
        and every dependency override wired. The instance carries the
        :class:`SliceServices` bundle on ``app.state.services``.
    """
    active_engine = engine if engine is not None else create_default_engine(database_url)
    # ``install_pragmas`` is idempotent: every call after the first is a
    # no-op via the module-level ``WeakSet`` registry, so re-installing
    # against an engine the caller already configured cannot stack
    # duplicate ``connect`` event handlers.
    install_pragmas(active_engine)

    # Import the planning router lazily — :mod:`walking_slice.planning._routes`
    # imports :func:`get_request_context` from this module, so a
    # module-level import here would form a circular dependency. The
    # local import is resolved exactly once per Python interpreter (the
    # module is cached on :data:`sys.modules`) so subsequent
    # ``create_app`` calls pay no additional cost.
    from walking_slice.planning import _routes as planning_routes
    # Slice 3 routers are imported lazily for the same reason:
    # :mod:`walking_slice.execution._routes` imports
    # :func:`get_request_context` from this module. The provenance
    # routes module is already imported at module scope (it is a
    # Slice 1 module) and exposes its own Slice 3
    # :func:`provenance_routes.get_request_context` placeholder that
    # we override below.
    from walking_slice.deliverables import _routes as deliverables_routes
    from walking_slice.execution import _routes as execution_routes
    # Slice 4 router is imported lazily for the same reason:
    # :mod:`walking_slice.outcome._routes` imports
    # :func:`get_request_context` from this module. Importing the module
    # also transitively imports :mod:`walking_slice.outcome._provenance`,
    # whose module body runs :func:`register_outcome_navigation` so the
    # additive ``navigate_outcome_review`` / ``navigate_outcome_node``
    # methods are attached to :class:`ProvenanceNavigator`.
    from walking_slice.outcome import _routes as outcome_routes

    # Schema + seeds run synchronously at app construction so a startup
    # failure is surfaced before the server begins accepting requests.
    # All three calls are idempotent (``CREATE TABLE IF NOT EXISTS`` and
    # ``INSERT OR IGNORE``), so a second invocation against the same
    # database — common in dev hot-reload loops — is harmless.
    create_schema(active_engine)
    # Slice 2 schema is additive: the Slice 1 schema must exist first so
    # the foreign keys from ``Objectives.target_decision_id`` to
    # ``Decision_Revisions`` and from ``Disclosure_Policy_Coverage`` to
    # ``Disclosure_Policies`` resolve.
    create_planning_schema(active_engine)
    # The Slice 2 Interim ADR seeder invokes the Slice 1 seeder first
    # (Gaps G-1..G-5 / AD-WS-6..AD-WS-10) and then inserts the Slice 2
    # rows for Gaps G-6..G-10 (AD-WS-15..AD-WS-19). Both stages use
    # ``INSERT OR IGNORE`` against stable primary keys so repeated
    # startups are byte-equivalent.
    seed_planning_interim_adr(active_engine, clock=clock)
    seed_disclosure_policy(active_engine)
    # The ``Disclosure_Policy_Coverage`` rows are inserted in a separate
    # transaction so they participate in the same atomic-or-nothing
    # window as the schema creation that produced the coverage table.
    # The ``slice-default-2026`` row must already be present in
    # ``Disclosure_Policies`` (seeded above) for the coverage foreign
    # key to resolve.
    with active_engine.begin() as conn:
        seed_planning_coverage(conn)

    # ------------------------------------------------------------------
    # Slice 3 (Execution_Service + Deliverable_Repository) bootstrap.
    #
    # Slice 3 schema creation is additive over Slice 1 + Slice 2: the
    # Execution_Service tables reference Slice 1 ``Parties`` and the
    # Deliverable_Repository tables they FK to, while the
    # Deliverable_Repository tables reference Slice 1 ``Parties``.
    # SQLite resolves foreign-key targets lazily at INSERT time
    # (with ``PRAGMA foreign_keys=ON``), so the two Slice 3 schemas
    # may be created in either order — we create the Execution_Service
    # schema first because the comment in ``execution/_persistence.py``
    # uses that order, but the inverse would also work.
    #
    # The Slice 3 ``Disclosure_Policy_Coverage`` rows and the Slice 3
    # ``Interim_ADR_Records`` rows are seeded inside one
    # ``engine.begin()`` block so a partial bootstrap is rolled back
    # together. Both seeds use ``INSERT OR IGNORE`` against stable
    # primary keys so repeated invocations are byte-equivalent.
    create_execution_schema(active_engine)
    create_deliverable_schema(active_engine)
    with active_engine.begin() as conn:
        seed_execution_coverage(conn, clock=clock)
        seed_execution_interim_adr(conn, clock=clock)

    # ------------------------------------------------------------------
    # Slice 4 (Outcome_Service) bootstrap.
    #
    # Slice 4 schema creation is additive over Slice 1 + Slice 2 +
    # Slice 3: the seven Outcome_Service tables reference Slice 1
    # ``Parties`` / ``Identifier_Registry`` and the Slice 2 Intended
    # Outcome Revisions they ``Addresses``. The Slice 4
    # ``Disclosure_Policy_Coverage`` rows (AD-WS-34) require the
    # ``slice-default-2026`` policy row seeded above, and the Slice 4
    # ``Interim_ADR_Records`` rows (AD-WS-33..AD-WS-38) follow the
    # Slice 1–3 Interim ADR contract. Both seeds run inside one
    # ``engine.begin()`` block — after the Slice 1/2/3 schema,
    # disclosure, and interim-ADR seeding above so every FK target and
    # the ``slice-default-2026`` policy row already exist — so a partial
    # bootstrap is rolled back together. Both seeds use ``INSERT OR
    # IGNORE`` against stable primary keys so repeated invocations are
    # byte-equivalent.
    #
    # :func:`register_outcome_navigation` is invoked explicitly here so
    # the additive ``navigate_outcome_review`` / ``navigate_outcome_node``
    # methods are attached to :class:`ProvenanceNavigator` even if the
    # outcome modules were imported in an order that skipped the import-
    # time registration; it is idempotent. The extended backlink source
    # kinds are a module-level ``frozenset`` constant in
    # :mod:`walking_slice.provenance`, so importing that module (already
    # done at module scope) is sufficient to cover the Slice 4 node
    # kinds — no runtime registration call is needed for them.
    create_outcome_schema(active_engine)
    register_outcome_navigation()
    with active_engine.begin() as conn:
        seed_outcome_coverage(conn, clock=clock)
        seed_outcome_interim_adr(conn, clock=clock)

    services = _build_services(
        active_engine,
        clock=clock,
        jwt_secret=jwt_secret,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """ASGI lifespan hook.

        Schema and seeding already ran synchronously above so the
        lifespan body is presently empty; we keep the hook in place so
        future startup wiring (e.g. background tasks for stale-source
        sweeps, cf. Requirement 10.3) has a single place to land.
        Disposing the engine on shutdown is deferred to the caller —
        tests reuse the engine across multiple ``create_app`` calls and
        disposing it here would break them.
        """
        try:
            yield
        finally:
            # Intentionally no engine.dispose() — see docstring.
            pass

    app = FastAPI(
        title="First Walking Slice",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    app.state.engine = active_engine

    # ------------------------------------------------------------------
    # Dependency overrides.
    #
    # Each ``routes/*.py`` module declares its own local ``get_engine``
    # and ``get_<service>`` placeholders (every placeholder raises
    # NotImplementedError when called unwrapped). We override each
    # callable explicitly so the wiring is grep-friendly: a developer
    # looking for "where does the trails router get its engine from"
    # finds exactly one match below.
    # ------------------------------------------------------------------

    overrides = app.dependency_overrides

    # roles.py
    overrides[roles_routes.get_engine] = lambda: active_engine
    overrides[roles_routes.get_authorization_service] = (
        lambda: services.authorization_service
    )
    overrides[roles_routes.get_audit_log] = lambda: services.audit_log
    overrides[roles_routes.get_clock] = lambda: services.clock

    # evidence.py
    overrides[evidence_routes.get_engine] = lambda: active_engine
    overrides[evidence_routes.get_evidence_repository] = (
        lambda: services.evidence_repository
    )

    # findings.py
    overrides[findings_routes.get_engine] = lambda: active_engine
    overrides[findings_routes.get_knowledge_service] = (
        lambda: services.knowledge_service
    )

    # recommendations.py
    overrides[recommendations_routes.get_engine] = lambda: active_engine
    overrides[recommendations_routes.get_knowledge_service] = (
        lambda: services.knowledge_service
    )

    # decisions.py
    overrides[decisions_routes.get_engine] = lambda: active_engine
    overrides[decisions_routes.get_knowledge_service] = (
        lambda: services.knowledge_service
    )

    # trails.py
    overrides[trails_routes.get_engine] = lambda: active_engine
    overrides[trails_routes.get_trail_service] = lambda: services.trail_service

    # provenance.py
    overrides[provenance_routes.get_engine] = lambda: active_engine
    overrides[provenance_routes.get_provenance_navigator] = (
        lambda: services.provenance_navigator
    )

    # ------------------------------------------------------------------
    # Slice 2 (Planning_Service) route wiring.
    #
    # The planning router lives in ``walking_slice.planning._routes``
    # and declares its own ``get_engine`` / ``get_<service>``
    # placeholders so the wire-up is grep-friendly: a developer
    # searching for "where does the planning router get its
    # ObjectiveService from" finds exactly one match below. Every
    # override points at the same ``SliceServices`` bundle so the
    # planning routes share the singletons with the rest of the app.
    # ------------------------------------------------------------------

    overrides[planning_routes.get_engine] = lambda: active_engine
    overrides[planning_routes.get_objective_service] = (
        lambda: services.objective_service
    )
    overrides[planning_routes.get_intended_outcome_service] = (
        lambda: services.intended_outcome_service
    )
    overrides[planning_routes.get_project_service] = (
        lambda: services.project_service
    )
    overrides[planning_routes.get_deliverable_expectation_service] = (
        lambda: services.deliverable_expectation_service
    )
    overrides[planning_routes.get_activity_plan_service] = (
        lambda: services.activity_plan_service
    )
    overrides[planning_routes.get_plan_revision_service] = (
        lambda: services.plan_revision_service
    )
    overrides[planning_routes.get_plan_review_service] = (
        lambda: services.plan_review_service
    )
    overrides[planning_routes.get_plan_approval_service] = (
        lambda: services.plan_approval_service
    )
    overrides[planning_routes.get_provenance_navigator] = (
        lambda: services.provenance_navigator
    )

    # ------------------------------------------------------------------
    # Slice 3 (Execution_Service + Deliverable_Repository) route wiring.
    #
    # The Execution_Service router declares its own ``get_engine`` /
    # ``get_<service>`` / ``get_status_projector`` placeholders, and
    # the Deliverable_Repository router declares
    # ``get_deliverable_repository_service``. The Slice 3 traversal
    # endpoints additively mounted on the existing
    # :mod:`walking_slice.routes.provenance` module additionally
    # declare a Slice-3-only :func:`provenance_routes.get_request_context`
    # placeholder (parallel to the Slice 1 / Slice 2 placeholder in
    # this module) that we must override here as well.
    # ------------------------------------------------------------------

    # execution._routes
    overrides[execution_routes.get_engine] = lambda: active_engine
    overrides[execution_routes.get_work_assignment_service] = (
        lambda: services.work_assignment_service
    )
    overrides[execution_routes.get_work_event_service] = (
        lambda: services.work_event_service
    )
    overrides[execution_routes.get_time_entry_service] = (
        lambda: services.time_entry_service
    )
    overrides[execution_routes.get_deliverable_production_service] = (
        lambda: services.deliverable_production_service
    )
    overrides[execution_routes.get_milestone_acceptance_service] = (
        lambda: services.milestone_acceptance_service
    )
    overrides[execution_routes.get_completion_service] = (
        lambda: services.completion_service
    )
    overrides[execution_routes.get_status_projector] = (
        lambda: services.status_projector
    )

    # deliverables._routes
    overrides[deliverables_routes.get_deliverable_repository_service] = (
        lambda: services.deliverable_repository_service
    )

    # ------------------------------------------------------------------
    # Slice 4 (Outcome_Service) route wiring.
    #
    # The outcome router (``walking_slice.outcome._routes``) declares its
    # own ``get_engine`` / ``get_<service>`` / ``get_provenance_navigator``
    # placeholders so the wire-up is grep-friendly. Every override points
    # at the same ``SliceServices`` bundle so the outcome routes share the
    # singletons — and the single shared :class:`ProvenanceNavigator`
    # carrying the additive ``navigate_outcome_*`` methods — with the rest
    # of the app. The outcome routes resolve their :class:`RequestContext`
    # through the Slice-1 ``walking_slice.app.get_request_context`` symbol
    # overridden below, so no separate context placeholder exists on the
    # module.
    # ------------------------------------------------------------------
    overrides[outcome_routes.get_engine] = lambda: active_engine
    overrides[outcome_routes.get_measurement_definition_service] = (
        lambda: services.measurement_definition_service
    )
    overrides[outcome_routes.get_measurement_record_service] = (
        lambda: services.measurement_record_service
    )
    overrides[outcome_routes.get_observed_outcome_service] = (
        lambda: services.observed_outcome_service
    )
    overrides[outcome_routes.get_success_condition_assessment_service] = (
        lambda: services.success_condition_assessment_service
    )
    overrides[outcome_routes.get_outcome_review_service] = (
        lambda: services.outcome_review_service
    )
    overrides[outcome_routes.get_provenance_navigator] = (
        lambda: services.provenance_navigator
    )

    # The Slice 3 provenance endpoints (additive on
    # ``walking_slice.routes.provenance``) use a Slice-3-only
    # placeholder declared on the same module so the route signatures
    # can resolve the RequestContext without forming a circular
    # import with :mod:`walking_slice.app`. Override it with the same
    # resolver the rest of the app uses so all endpoints — Slice 1,
    # Slice 2, and Slice 3 — observe one consistent context.
    overrides[provenance_routes.get_request_context] = (
        services.request_context_resolver
    )

    # RequestContext for routes that have been migrated off the
    # placeholder ``X-Actor-Party-Id`` header (task 15.1 / future
    # waves). The resolver is callable as-is.
    overrides[get_request_context] = services.request_context_resolver

    # ------------------------------------------------------------------
    # Router mounting.
    # ------------------------------------------------------------------

    app.include_router(roles_routes.router)
    app.include_router(evidence_routes.router)
    app.include_router(findings_routes.router)
    app.include_router(recommendations_routes.router)
    app.include_router(decisions_routes.router)
    app.include_router(trails_routes.router)
    app.include_router(provenance_routes.router)
    # Slice 2: Planning_Service router (Objectives, Intended Outcomes,
    # Projects, Deliverable Expectations, Activity Plans, Plan
    # Revisions, Plan Reviews, Plan Approvals, and the Plan Approval
    # provenance walk endpoint). Mounted last so the Slice 1 routers'
    # endpoints retain their existing ordering on
    # ``app.routes`` — Slice 2 endpoints are strictly additive.
    app.include_router(planning_routes.router)
    # Slice 3: Execution_Service router (Work Assignments, Work Events,
    # Time Entries, Deliverable Productions, Milestone Acceptances,
    # Completions, and the execution-status Projection endpoint) and
    # Deliverable_Repository router (produced Deliverables and their
    # Revisions). The Slice 3 traversal endpoints — ``GET
    # /completions/{id}/provenance``, ``GET
    # /deliverable-productions/{id}/provenance``, and ``GET
    # /deliverables/{id}/revisions/{id}/provenance`` — are additive
    # endpoints on the existing :mod:`walking_slice.routes.provenance`
    # module mounted above, so no separate ``include_router`` call is
    # needed for them. Slice 3 routers are mounted last so the Slice 1
    # and Slice 2 endpoints retain their existing ordering on
    # ``app.routes`` (Requirement 40.1 — additive only).
    app.include_router(execution_routes.router)
    app.include_router(deliverables_routes.router)
    # Slice 4: Outcome_Service router (Measurement Definitions,
    # Measurement Records — native and imported, Observed Outcomes,
    # Success-Condition Assessments, Outcome Reviews, the Outcome
    # Measurement Provenance Chain traversal endpoints, and the
    # outcome-status Projection endpoint). Mounted last so the Slice 1,
    # Slice 2, and Slice 3 endpoints retain their existing ordering on
    # ``app.routes`` (Requirement 60.1 — additive only).
    app.include_router(outcome_routes.router)

    # ------------------------------------------------------------------
    # Healthcheck.
    # ------------------------------------------------------------------

    @app.get("/healthz", tags=["meta"])
    async def healthcheck() -> dict:
        """Return ``{"status": "ok"}``.

        Smoke tests and container orchestration use this endpoint to
        confirm the app is up. The response is intentionally minimal
        — no service introspection — so a healthcheck cannot leak
        operational details to unauthenticated callers.
        """
        return {"status": "ok"}

    return app
