"""Unit tests for :mod:`walking_slice.planning.plan_reviews` (task 10.2).

Pins the Requirement 8.3, 8.6, 8.7 contract for
:meth:`PlanReviewService.create_plan_review`:

- **8.6 — outcome enumeration.** The service must accept exactly the
  three review outcomes ``{Endorse, Changes_Requested, Reject}`` and
  reject any other value with the stable ``outcome_out_of_set``
  constraint, before any database read or authorization side-effect.
- **8.6 — authority basis type validation.** The
  ``authority_basis.type`` value must be drawn from the AD-WS-10
  enumeration ``{role-grant-id, scope-id, delegation-chain-id}``;
  any other value is rejected with
  ``authority_basis_type_out_of_set``. The same enumeration is
  enforced by the ``Plan_Review_Revisions.authority_basis_type``
  CHECK constraint; the validator surfaces a precise error before
  the SQL layer.
- **8.6 — non-draft target rejection.** A target Plan Revision whose
  ``lifecycle_state`` is not ``'draft'`` (i.e. ``'approved'``) raises
  :class:`PlanReviewTargetNotDraftError` with no Plan Review
  Resource, Plan Review Revision, ``Relates To`` Relationship,
  consequential audit row, or ``Identifier_Registry`` binding
  created.
- **8.7 — lifecycle byte-equivalence of the target.** Recording a
  Plan Review must not change the target Plan Revision row in any
  way: the row is read once and never UPDATEd. The tests snapshot
  every column of the target row before and after the Plan Review
  creation and assert equality byte-for-byte.
- **8.3 — exactly one ``Relates To`` row with
  ``semantic_role = 'review'``.** Per the AD-WS-17 additive
  ``semantic_role`` discriminator the service inserts exactly one
  ``Relationships`` row whose ``relationship_type = 'Relates To'``,
  ``semantic_role = 'review'``, and ``source_id`` = new Plan Review
  Resource Identity.

The tests mirror the style of
``tests/unit/test_planning_plan_revisions.py``: a per-test engine
carrying both the Slice 1 and Slice 2 schemas, a real
:class:`AuthorizationService` driven through a seeded ``review``
role assignment on happy paths, and counter helpers that confirm
nothing was persisted on negative paths.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.plan_reviews import (
    CreatePlanReviewResult,
    PlanReviewService,
    PlanReviewTargetNotDraftError,
    PlanReviewTargetNotResolvableError,
    PlanReviewValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_ACTIVITY_PLAN_ID = "00000000-0000-7000-8000-000000c00020"
_DRAFT_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00040"
_APPROVED_PLAN_REVISION_ID = "00000000-0000-7000-8000-000000c00041"
_UNRESOLVABLE_PLAN_REVISION_ID = "00000000-0000-7000-8000-0000deadbe10"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_RATIONALE = "Reviewer endorses Phase 1 scope; no changes requested."

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def planning_engine(engine: Engine) -> Engine:
    """Per-test engine carrying both Slice 1 and Slice 2 schemas.

    ``create_schema`` installs Slice 1 plus the additive
    ``Identifier_Registry.resource_kind`` and
    ``Relationships.semantic_role`` columns (task 1.2);
    ``create_planning_schema`` installs every Slice 2 table, index,
    and append-only trigger (task 1.3). No disclosure seeding is
    required: Plan Review creation does not consult the disclosure
    registry.
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def plan_review_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> PlanReviewService:
    """:class:`PlanReviewService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is not the focus of task 10.2 — the
    happy paths assume an effective Plan Reviewer role assignment is
    seeded so the validator-focused tests can exercise the post-
    validation flow end-to-end.
    """
    return PlanReviewService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
#
# A Plan Review depends on the target Plan Revision (and transitively
# on the parent Activity Plan / Project) existing. These helpers seed
# just enough header rows for the target-lookup SELECT to succeed.
# ---------------------------------------------------------------------------


def _seed_party(conn, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    """Seed the reviewing Party and the assigning-authority Party.

    Both rows are referenced by foreign keys on
    ``Plan_Review_Revisions.reviewing_party_id`` (the reviewer) and
    ``Role_Assignments.assigning_authority_id`` (the role-granter)
    respectively.
    """
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Plan Reviewer")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_project(engine: Engine, project_id: str = _PROJECT_ID) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": project_id, "ts": _TS_FIXED},
        )


def _seed_activity_plan(
    engine: Engine,
    *,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    project_id: str = _PROJECT_ID,
) -> None:
    """Seed one Activity Plan row.

    The Plan Revision FK requires the Activity Plan row to exist;
    seeding directly bypasses :class:`ActivityPlanService` which is
    exercised by its own test module.
    """
    with engine.begin() as conn:
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
                "aid": activity_plan_id,
                "pid": project_id,
                "title": "Mesh Rollout Activities",
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _seed_plan_revision_directly(
    engine: Engine,
    *,
    plan_revision_id: str,
    activity_plan_id: str = _ACTIVITY_PLAN_ID,
    lifecycle_state: str = "draft",
    planned_scope: str = "Phase 1 scope.",
) -> None:
    """Seed a ``Plan_Revisions`` row by hand, bypassing the service.

    INSERTs into ``Plan_Revisions`` are not gated by the AD-WS-19
    lifecycle trigger (which only watches UPDATE), so seeding a row
    with ``lifecycle_state = 'approved'`` is a direct INSERT — no
    pragma plumbing required (mirrors the pattern in
    ``tests/unit/test_planning_persistence.py``).
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
                    :rev, :aid, NULL, :state, :scope_text, '[]', '[]',
                    NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": activity_plan_id,
                "state": lifecycle_state,
                "scope_text": planned_scope,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
        )


def _assign_plan_reviewer_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Plan Reviewer authority (``review``) to ``party_id``.

    Per AD-WS-15, ``create.plan_review`` maps to the ``review``
    authority type. A Party with an effective Role Assignment
    carrying ``review`` over ``scope`` is permitted to record a Plan
    Review against a Plan Revision in that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="plan_reviewer",
        scope=scope,
        authorities_granted=("review",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


# ---------------------------------------------------------------------------
# Row counters — used by negative-path tests to confirm nothing was
# persisted and by positive-path tests to inspect inserted rows.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_review_relationships_with_source(
    engine: Engine, source_id: str
) -> int:
    """Count ``Relates To`` rows with ``semantic_role='review'`` whose
    ``source_id`` equals ``source_id``.

    This is the headline assertion of Requirement 8.3: exactly one
    such row per Plan Review (AD-WS-17 ``semantic_role`` discriminator).
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Relationships "
                    "WHERE relationship_type = 'Relates To' "
                    "AND semantic_role = 'review' "
                    "AND source_id = :sid"
                ),
                {"sid": source_id},
            ).scalar_one()
        )


def _snapshot_plan_revision_row(engine: Engine, plan_revision_id: str) -> dict:
    """Return every persisted column of a single ``Plan_Revisions``
    row as a plain dict.

    Used by the Requirement 8.7 byte-equivalence test to compare the
    target row's contents before and after a Plan Review creation.
    Selecting every column explicitly (rather than ``SELECT *``) pins
    the assertion against the schema in
    :mod:`walking_slice.planning._persistence`.
    """
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    """
                    SELECT plan_revision_id, activity_plan_id,
                           predecessor_revision_id, lifecycle_state,
                           planned_scope, deliverable_expectation_refs_json,
                           planning_assumptions_json, ordering_rationale,
                           authoring_party_id, applicable_scope, recorded_at
                    FROM Plan_Revisions
                    WHERE plan_revision_id = :id
                    """
                ),
                {"id": plan_revision_id},
            )
            .mappings()
            .one()
        )


# ===========================================================================
# Happy-path baseline — confirms the test wiring before the focus tests run.
# ===========================================================================


def test_create_plan_review_permits_when_plan_reviewer_role_grants_review(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    plan_review_service: PlanReviewService,
) -> None:
    """Permit path: with an effective Plan Reviewer role and a Draft
    target Plan Revision, the service creates exactly one
    ``Plan_Reviews`` row, one ``Plan_Review_Revisions`` row, one
    ``Relates To`` Relationship row, and one consequential audit row
    inside one transaction.
    """
    _seed_required_parties(planning_engine)
    _assign_plan_reviewer_role(authorization_service, planning_engine)
    _seed_project(planning_engine)
    _seed_activity_plan(planning_engine)
    _seed_plan_revision_directly(
        planning_engine,
        plan_revision_id=_DRAFT_PLAN_REVISION_ID,
        lifecycle_state="draft",
    )

    with planning_engine.begin() as conn:
        result = plan_review_service.create_plan_review(
            conn,
            target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            outcome="Endorse",
            rationale=_RATIONALE,
            reviewing_party_id=_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreatePlanReviewResult)
    assert _CANONICAL_UUID7.match(result.plan_review_id)
    assert _CANONICAL_UUID7.match(result.plan_review_revision_id)
    assert _CANONICAL_UUID7.match(result.relates_to_relationship_id)
    assert result.target_plan_revision_id == _DRAFT_PLAN_REVISION_ID
    assert result.outcome == "Endorse"
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Plan_Reviews") == 1
    assert _count(planning_engine, "Plan_Review_Revisions") == 1
    assert _count_review_relationships_with_source(
        planning_engine, result.plan_review_id
    ) == 1


# ===========================================================================
# Requirement 8.6 — outcome enumeration validation.
#
# The validator must accept exactly the three Requirement 8.2 values
# and reject any other value with the stable ``outcome_out_of_set``
# constraint. The rejection path runs before any database read or
# authorization side-effect so a malformed outcome never touches the
# Plan_Reviews / Plan_Review_Revisions / Relationships / Audit_Records
# tables.
# ===========================================================================


class TestOutcomeEnumerationValidation:
    """``outcome`` must be one of ``{Endorse, Changes_Requested, Reject}``."""

    @pytest.mark.parametrize(
        "valid_outcome",
        ["Endorse", "Changes_Requested", "Reject"],
    )
    def test_every_valid_outcome_is_accepted_and_persisted(
        self,
        valid_outcome: str,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """All three Requirement 8.2 outcomes pass the validator and
        land on the ``Plan_Review_Revisions.outcome`` column verbatim.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with planning_engine.begin() as conn:
            result = plan_review_service.create_plan_review(
                conn,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome=valid_outcome,
                rationale=_RATIONALE,
                reviewing_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.outcome == valid_outcome
        with planning_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT outcome FROM Plan_Review_Revisions "
                    "WHERE plan_review_revision_id = :rid"
                ),
                {"rid": result.plan_review_revision_id},
            ).scalar_one()
        assert stored == valid_outcome

    @pytest.mark.parametrize(
        "invalid_outcome",
        [
            "Approve",            # belongs to Plan Approval, not Plan Review
            "Reject_Approval",    # belongs to Plan Approval, not Plan Review
            "endorse",            # case-sensitive match required
            "Changes Requested",  # space instead of underscore
            "Other",
        ],
    )
    def test_outcome_outside_the_enumeration_is_rejected(
        self,
        invalid_outcome: str,
        planning_engine: Engine,
        plan_review_service: PlanReviewService,
    ) -> None:
        """An outcome value outside the Requirement 8.2 set raises
        :class:`PlanReviewValidationError` with stable constraint
        ``'outcome_out_of_set'`` and no row is persisted anywhere.
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(PlanReviewValidationError) as exc_info:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=invalid_outcome,
                    rationale=_RATIONALE,
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "outcome_out_of_set"
        # Validator runs before identifier minting, lookups, INSERTs.
        assert _count(planning_engine, "Plan_Reviews") == 0
        assert _count(planning_engine, "Plan_Review_Revisions") == 0
        assert _count(planning_engine, "Relationships") == 0
        assert _count(planning_engine, "Identifier_Registry") == 0

    def test_empty_outcome_rejected_with_distinct_constraint(
        self,
        planning_engine: Engine,
        plan_review_service: PlanReviewService,
    ) -> None:
        """An empty-string outcome surfaces ``'outcome_missing'``
        rather than ``'outcome_out_of_set'``.

        The two negative paths carry distinct constraint identifiers
        so the HTTP layer (task 15.1) can render distinct 400
        messages: "outcome required" vs "outcome not in the
        enumeration".
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(PlanReviewValidationError) as exc_info:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome="",
                    rationale=_RATIONALE,
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "outcome_missing"
        assert _count(planning_engine, "Plan_Reviews") == 0


# ===========================================================================
# Requirement 8.6 — authority basis type validation.
#
# Per AD-WS-10 / AD-WS-22 the ``authority_basis.type`` value is drawn
# from ``{role-grant-id, scope-id, delegation-chain-id}``. The
# Pydantic ``AuthorityBasisRef`` Literal already constrains
# Python-typed callers; the dict-shaped path that the HTTP layer (task
# 15.1) may take must be rejected by the service-level validator
# before the SQL CHECK constraint is reached.
# ===========================================================================


class TestAuthorityBasisTypeValidation:
    """``authority_basis.type`` must be in the AD-WS-10 set."""

    @pytest.mark.parametrize(
        "valid_basis_type",
        ["role-grant-id", "scope-id", "delegation-chain-id"],
    )
    def test_every_ad_ws_10_basis_type_is_accepted_and_persisted(
        self,
        valid_basis_type: str,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """All three AD-WS-10 basis types pass the validator and land
        on the ``Plan_Review_Revisions.authority_basis_type`` column
        verbatim.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        basis = AuthorityBasisRef(
            type=valid_basis_type, id=_AUTHORITY_BASIS_ID
        )
        with planning_engine.begin() as conn:
            result = plan_review_service.create_plan_review(
                conn,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Endorse",
                rationale=_RATIONALE,
                reviewing_party_id=_PARTY_ID,
                authority_basis=basis,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.authority_basis.type == valid_basis_type
        with planning_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT authority_basis_type, authority_basis_id "
                    "FROM Plan_Review_Revisions "
                    "WHERE plan_review_revision_id = :rid"
                ),
                {"rid": result.plan_review_revision_id},
            ).mappings().one()
        assert stored["authority_basis_type"] == valid_basis_type
        assert stored["authority_basis_id"] == str(_AUTHORITY_BASIS_ID)

    @pytest.mark.parametrize(
        "invalid_basis_type",
        [
            "role_grant_id",     # underscores instead of hyphens
            "ROLE-GRANT-ID",     # case-sensitive
            "delegation-id",     # close but not a member of the set
            "session-id",        # plausible-looking value outside AD-WS-10
            "x",
        ],
    )
    def test_authority_basis_type_outside_ad_ws_10_set_is_rejected(
        self,
        invalid_basis_type: str,
        planning_engine: Engine,
        plan_review_service: PlanReviewService,
    ) -> None:
        """A dict-shaped ``authority_basis`` whose ``type`` is outside
        the AD-WS-10 set raises :class:`PlanReviewValidationError`
        with constraint ``'authority_basis_type_out_of_set'``.

        The Pydantic :class:`AuthorityBasisRef` Literal already
        rejects out-of-set values at model-construction time, so
        this path is exercised through a raw dict that simulates the
        HTTP layer forwarding an unvalidated request body.
        """
        _seed_required_parties(planning_engine)

        basis_dict = {
            "type": invalid_basis_type,
            "id": str(_AUTHORITY_BASIS_ID),
        }
        with planning_engine.begin() as conn:
            with pytest.raises(PlanReviewValidationError) as exc_info:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome="Endorse",
                    rationale=_RATIONALE,
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=basis_dict,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "authority_basis_type_out_of_set"
        )
        assert _count(planning_engine, "Plan_Reviews") == 0
        assert _count(planning_engine, "Plan_Review_Revisions") == 0

    def test_missing_authority_basis_id_rejected(
        self,
        planning_engine: Engine,
        plan_review_service: PlanReviewService,
    ) -> None:
        """A dict-shaped ``authority_basis`` missing ``id`` raises
        :class:`PlanReviewValidationError` with constraint
        ``'authority_basis_id_missing'``.

        The two negative paths (out-of-set type vs missing id)
        surface as distinct constraint identifiers per Requirement
        8.6's "structured error identifying the invalid attribute".
        """
        _seed_required_parties(planning_engine)

        basis_dict = {"type": "role-grant-id", "id": ""}
        with planning_engine.begin() as conn:
            with pytest.raises(PlanReviewValidationError) as exc_info:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome="Endorse",
                    rationale=_RATIONALE,
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=basis_dict,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "authority_basis_id_missing"
        assert _count(planning_engine, "Plan_Reviews") == 0


# ===========================================================================
# Requirement 8.6 — non-draft target rejection.
#
# Approved Plan Revisions are byte-equivalent forever per Requirement
# 9.4. Accepting a Plan Review against an Approved Plan Revision
# would have no observable effect on the target row (Requirement
# 8.7), but Requirement 8.6 explicitly rejects the path so the
# observable response distinguishes Draft from Approved targets only
# behind the same authorization gate that protects every Plan
# Revision (no leakage through the non-draft path).
# ===========================================================================


class TestNonDraftTargetRejection:
    """Target Plan Revision must have ``lifecycle_state = 'draft'``."""

    def test_approved_target_raises_non_draft_error(
        self,
        planning_engine: Engine,
        plan_review_service: PlanReviewService,
    ) -> None:
        """An Approved target Plan Revision raises
        :class:`PlanReviewTargetNotDraftError`; no Plan Review
        Resource, Revision, Relationship, audit row, or registry
        binding is created.

        The check runs before authorization evaluation so the deny
        path never reveals whether the target is in Draft or
        Approved state for an unauthorized caller (Requirement 8.6
        / 10.7 indistinguishable-denial requirement).
        """
        _seed_required_parties(planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_APPROVED_PLAN_REVISION_ID,
            lifecycle_state="approved",
        )

        plan_reviews_before = _count(planning_engine, "Plan_Reviews")
        revisions_before = _count(planning_engine, "Plan_Review_Revisions")
        relationships_before = _count(planning_engine, "Relationships")
        audit_before = _count(planning_engine, "Audit_Records")
        registry_before = _count(planning_engine, "Identifier_Registry")

        with pytest.raises(PlanReviewTargetNotDraftError) as exc_info:
            with planning_engine.begin() as conn:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome="Endorse",
                    rationale=_RATIONALE,
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _APPROVED_PLAN_REVISION_ID
        )
        assert exc_info.value.lifecycle_state == "approved"
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_not_draft"
        )

        # Nothing was persisted: the rejection runs before any INSERT.
        assert _count(planning_engine, "Plan_Reviews") == plan_reviews_before
        assert _count(planning_engine, "Plan_Review_Revisions") == (
            revisions_before
        )
        assert _count(planning_engine, "Relationships") == relationships_before
        assert _count(planning_engine, "Audit_Records") == audit_before
        assert _count(planning_engine, "Identifier_Registry") == registry_before

    def test_unresolvable_target_raises_distinct_error(
        self,
        planning_engine: Engine,
        plan_review_service: PlanReviewService,
    ) -> None:
        """A target Plan Revision Identity that does not resolve raises
        :class:`PlanReviewTargetNotResolvableError`, not the
        non-draft error.

        Requirement 8.6 splits the rejection paths so distinct error
        types surface to the HTTP layer; both checks run before
        authorization evaluation for the indistinguishable-denial
        reason.
        """
        _seed_required_parties(planning_engine)

        with pytest.raises(PlanReviewTargetNotResolvableError) as exc_info:
            with planning_engine.begin() as conn:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_UNRESOLVABLE_PLAN_REVISION_ID,
                    outcome="Endorse",
                    rationale=_RATIONALE,
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.target_plan_revision_id == (
            _UNRESOLVABLE_PLAN_REVISION_ID
        )
        assert exc_info.value.failed_constraint == (
            "target_plan_revision_not_resolvable"
        )


# ===========================================================================
# Requirement 8.7 — lifecycle byte-equivalence of the target Plan Revision.
#
# Recording a Plan Review must not change the target Plan Revision
# row in any way. No UPDATE is issued against ``Plan_Revisions``
# anywhere in the service implementation; the row is read once in the
# resolution SELECT and is otherwise untouched. The AD-WS-19
# lifecycle trigger would also reject any UPDATE attempt — the
# application-layer restraint here is belt-and-braces with the
# database trigger.
# ===========================================================================


class TestTargetLifecycleByteEquivalence:
    """Recording a Plan Review leaves the target Plan Revision row
    byte-equivalent to its prior state (Requirement 8.7)."""

    def test_target_plan_revision_row_byte_equivalent_after_review(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """Every persisted column of the target Plan Revision row is
        byte-identical before and after a successful Plan Review
        creation. The row's ``lifecycle_state`` in particular stays
        ``'draft'``.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        target_before = _snapshot_plan_revision_row(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        )

        with planning_engine.begin() as conn:
            result = plan_review_service.create_plan_review(
                conn,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Endorse",
                rationale=_RATIONALE,
                reviewing_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        # Sanity check: the Plan Review itself was created.
        assert _count(planning_engine, "Plan_Reviews") == 1
        assert _count(planning_engine, "Plan_Review_Revisions") == 1
        assert _CANONICAL_UUID7.match(result.plan_review_revision_id)

        target_after = _snapshot_plan_revision_row(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        )
        assert target_after == target_before
        # The lifecycle_state column in particular is unchanged.
        assert target_after["lifecycle_state"] == "draft"

    def test_target_row_count_unchanged_across_three_plan_reviews(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """Recording multiple Plan Reviews against the same Draft
        target Plan Revision still leaves the target row
        byte-equivalent (no UPDATE issued by any of the writes).

        Requirement 8.7 does not cap the number of Plan Reviews per
        Plan Revision; the immutability invariant applies regardless
        of how many reviews accumulate against a single Draft.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        target_before = _snapshot_plan_revision_row(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        )

        for outcome in ("Endorse", "Changes_Requested", "Reject"):
            with planning_engine.begin() as conn:
                plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=outcome,
                    rationale=f"rationale for {outcome}",
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert _count(planning_engine, "Plan_Reviews") == 3
        assert _count(planning_engine, "Plan_Review_Revisions") == 3
        # The target Plan Revision row is still byte-equivalent.
        target_after = _snapshot_plan_revision_row(
            planning_engine, _DRAFT_PLAN_REVISION_ID
        )
        assert target_after == target_before


# ===========================================================================
# Requirement 8.3 — exactly one ``Relates To`` Relationship per Plan
# Review with ``semantic_role = 'review'``.
#
# Per the AD-WS-17 additive ``semantic_role`` column the service
# inserts exactly one ``Relationships`` row binding the new Plan
# Review Revision (source) to the target Plan Revision (target). The
# ``semantic_role = 'review'`` discriminator distinguishes this edge
# from any future non-review ``Relates To`` rows in the slice.
# ===========================================================================


class TestRelatesToRelationshipInsertion:
    """``Relates To`` Relationship is INSERTed exactly once per Plan Review."""

    def test_exactly_one_relates_to_row_per_plan_review(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """``COUNT(*) FROM Relationships WHERE relationship_type='Relates To'
        AND semantic_role='review' AND source_id = plan_review_id``
        is exactly 1.

        This is the headline assertion of Requirement 8.3 / AD-WS-17.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with planning_engine.begin() as conn:
            result = plan_review_service.create_plan_review(
                conn,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Endorse",
                rationale=_RATIONALE,
                reviewing_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert _count_review_relationships_with_source(
            planning_engine, result.plan_review_id
        ) == 1
        # The result carries the Identity of that ``Relates To`` row.
        assert _CANONICAL_UUID7.match(result.relates_to_relationship_id)

    def test_relates_to_row_columns_pin_source_target_type_and_role(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """The single inserted ``Relates To`` row carries
        ``relationship_type = 'Relates To'``,
        ``semantic_role = 'review'``,
        ``source_kind = 'plan_review_revision'``,
        ``source_id = plan_review_id``,
        ``source_revision_id = plan_review_revision_id``,
        ``target_kind = 'plan_revision'``,
        ``target_id = target_plan_revision_id``, and
        ``target_revision_id IS NULL`` (Plan Revisions live in a
        single Revision-level table with no separate Resource
        header).
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        with planning_engine.begin() as conn:
            result = plan_review_service.create_plan_review(
                conn,
                target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                outcome="Endorse",
                rationale=_RATIONALE,
                reviewing_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        with planning_engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, semantic_role,
                           source_kind, source_id, source_revision_id,
                           target_kind, target_id, target_revision_id
                    FROM Relationships
                    WHERE relationship_type = 'Relates To'
                      AND semantic_role = 'review'
                      AND source_id = :sid
                    """
                ),
                {"sid": result.plan_review_id},
            ).mappings().one()

        assert row["relationship_id"] == result.relates_to_relationship_id
        assert row["relationship_type"] == "Relates To"
        assert row["semantic_role"] == "review"
        assert row["source_kind"] == "plan_review_revision"
        assert row["source_id"] == result.plan_review_id
        assert row["source_revision_id"] == result.plan_review_revision_id
        assert row["target_kind"] == "plan_revision"
        assert row["target_id"] == _DRAFT_PLAN_REVISION_ID
        # Plan Revisions live in a single Revision-level table with no
        # separate Resource header, so the target's revision_id is
        # NULL (the convention used by ``Supersedes`` rows as well).
        assert row["target_revision_id"] is None

    def test_three_plan_reviews_produce_three_relates_to_rows(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        plan_review_service: PlanReviewService,
    ) -> None:
        """Each Plan Review creation inserts exactly one ``Relates To``
        Relationship row with ``semantic_role='review'``: three Plan
        Reviews against one Draft target produce three such rows,
        and each row's ``source_id`` matches a distinct Plan Review
        Resource Identity.
        """
        _seed_required_parties(planning_engine)
        _assign_plan_reviewer_role(authorization_service, planning_engine)
        _seed_project(planning_engine)
        _seed_activity_plan(planning_engine)
        _seed_plan_revision_directly(
            planning_engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )

        plan_review_ids: list[str] = []
        for outcome in ("Endorse", "Changes_Requested", "Reject"):
            with planning_engine.begin() as conn:
                result = plan_review_service.create_plan_review(
                    conn,
                    target_plan_revision_id=_DRAFT_PLAN_REVISION_ID,
                    outcome=outcome,
                    rationale=f"rationale for {outcome}",
                    reviewing_party_id=_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
            plan_review_ids.append(result.plan_review_id)

        # Three distinct Plan Reviews → three distinct Relates To rows.
        assert len(set(plan_review_ids)) == 3
        with planning_engine.connect() as conn:
            review_relationships = int(
                conn.execute(
                    text(
                        "SELECT COUNT(*) FROM Relationships "
                        "WHERE relationship_type = 'Relates To' "
                        "AND semantic_role = 'review'"
                    )
                ).scalar_one()
            )
        assert review_relationships == 3
        for plan_review_id in plan_review_ids:
            assert _count_review_relationships_with_source(
                planning_engine, plan_review_id
            ) == 1
