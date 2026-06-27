"""Unit tests for :mod:`walking_slice.planning.activity_plans` (task 7.2).

Pins the contract established in task 7.1, design
§"Planning_Service.ActivityPlans", and Requirements 4.5, 6.2, 6.3 for
:meth:`ActivityPlanService.create_activity_plan`:

- **6.2 — title length boundaries.** Every Activity Plan creation
  request must carry a title of 1..200 characters. The static
  validator in :class:`ActivityPlanService` rejects values outside
  the range with the stable ``failed_constraint`` identifiers
  ``title_missing`` (empty / non-string) and ``title_too_long``
  (length > 200) *before* any database read or authorization
  side-effect. The schema CHECK constraint on ``Activity_Plans``
  (``CHECK (length(title) BETWEEN 1 AND 200)``) is the
  defense-in-depth layer: a hand-rolled INSERT that bypasses the
  service-level validator is rejected by the database with
  :class:`sqlalchemy.exc.IntegrityError`.
- **6.3 — unresolved Project.** A target Project Identity that does
  not resolve to an existing ``Projects`` row raises
  :class:`ActivityPlanProjectNotResolvableError` before any
  authorization evaluation or identifier minting happens, so the
  unresolved-target deny path never leaves an
  ``Identifier_Registry``, ``Activity_Plans``, or ``Relationships``
  row behind.
- **4.5 — identifier-set tagging.** The Activity Plan Resource
  Identity is registered in ``Identifier_Registry`` with the
  additive ``resource_kind = 'activity_plan'`` tag (AD-WS-19). The
  tag is what keeps Activity Plan Resource identifiers inspectably
  disjoint from Project Resource identifiers at row granularity.

The tests intentionally mirror the test style of
``tests/unit/test_planning_projects.py``: a per-test engine carrying
both the Slice 1 and Slice 2 schemas, a real
:class:`AuthorizationService` driven through a seeded role assignment
on the happy paths, and counter helpers that confirm nothing was
persisted on the negative paths.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.activity_plans import (
    ActivityPlanProjectNotResolvableError,
    ActivityPlanService,
    ActivityPlanValidationError,
    CreateActivityPlanResult,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_PROJECT_ID = "00000000-0000-7000-8000-000000c00010"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

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
    required: Activity Plan creation does not consult the disclosure
    registry.
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def activity_plan_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> ActivityPlanService:
    """ActivityPlanService wired with a real AuthorizationService.

    The same instance is used by every test in this module; the
    authorization deny path is exercised by *not* assigning a role
    rather than by swapping in a stub service, so the real
    evaluation code path participates in the test.
    """
    return ActivityPlanService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
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
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Project Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_project(engine: Engine, project_id: str = _PROJECT_ID) -> None:
    """Seed one Project Resource row.

    Inserted directly: the upstream Objective dependency is irrelevant
    to the Activity Plan tests because Activity Plans address a
    Project, not an Objective. The schema only requires the Project
    header row to exist for ``Activity_Plans.target_project_id`` to
    resolve.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": project_id, "ts": _TS_FIXED},
        )


def _assign_project_owner_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Project Owner authority (``modify``) to ``party_id``.

    Per AD-WS-15, ``create.activity_plan`` maps to the ``modify``
    authority type. A Party with an effective Role Assignment
    carrying ``modify`` over ``scope`` is permitted to create
    Activity Plans in that scope.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="project_owner",
        scope=scope,
        authorities_granted=("modify",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


# ---------------------------------------------------------------------------
# Row readers — used by negative-path tests to confirm nothing was persisted.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_registry_row(engine: Engine, identifier: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT identifier, kind, content_digest, resource_kind "
                "FROM Identifier_Registry WHERE identifier = :id"
            ),
            {"id": identifier},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ===========================================================================
# Happy path baseline — confirms the test wiring before the focus tests run.
# ===========================================================================


def test_create_activity_plan_permits_when_project_owner_role_grants_modify(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    activity_plan_service: ActivityPlanService,
) -> None:
    """Permit path: with an effective Project Owner role and an
    existing Project, the service creates one Activity Plan Resource,
    one ``Addresses`` Relationship to the Project, and one
    consequential audit row inside one transaction (AD-WS-5).

    Activity Plans have no Revision (AD-WS-3 /
    design §"Planning_Service.ActivityPlans") so the result carries
    no ``activity_plan_revision_id`` — distinct from the Projects
    pattern which mints both a Resource and first Revision identity.
    """
    _seed_required_parties(planning_engine)
    _assign_project_owner_role(authorization_service, planning_engine)
    _seed_project(planning_engine)

    with planning_engine.begin() as conn:
        result = activity_plan_service.create_activity_plan(
            conn,
            target_project_id=_PROJECT_ID,
            title="Mesh Rollout Activities",
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateActivityPlanResult)
    assert _CANONICAL_UUID7.match(result.activity_plan_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.target_project_id == _PROJECT_ID
    assert result.title == "Mesh Rollout Activities"
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Activity_Plans") == 1


# ===========================================================================
# Requirement 6.2 / 6.3 — title length boundaries, validator layer.
#
# The static validator in :class:`ActivityPlanService` rejects values
# outside 1..200 chars *before* any database read, identifier minting,
# or authorization side-effect — so a malformed request never touches
# the database. The schema CHECK is exercised in
# :class:`TestTitleLengthCheckLayer` below.
# ===========================================================================


class TestTitleLengthValidatorLayer:
    """Static validator rejects out-of-range titles before any DB read."""

    def test_one_char_title_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """A 1-character title sits at the lower boundary of Requirement 6.2."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = activity_plan_service.create_activity_plan(
                conn,
                target_project_id=_PROJECT_ID,
                title="A",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.title == "A"
        assert _count(planning_engine, "Activity_Plans") == 1

    def test_two_hundred_char_title_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """A 200-character title sits at the upper boundary of Requirement 6.2."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)

        title = "x" * 200
        with planning_engine.begin() as conn:
            result = activity_plan_service.create_activity_plan(
                conn,
                target_project_id=_PROJECT_ID,
                title=title,
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.title == title
        assert len(result.title) == 200
        assert _count(planning_engine, "Activity_Plans") == 1

    def test_two_hundred_one_char_title_rejected_with_stable_constraint_name(
        self,
        planning_engine: Engine,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """``len(title) == 201`` raises with ``failed_constraint == 'title_too_long'``.

        The error type is :class:`ActivityPlanValidationError`; the
        ``failed_constraint`` is the stable identifier the route
        layer maps to a structured 400 response.
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(ActivityPlanValidationError) as exc_info:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=_PROJECT_ID,
                    title="x" * 201,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "title_too_long"
        # Nothing persisted: the validator runs before identifier
        # minting, the Project resolution SELECT, and any INSERT.
        assert _count(planning_engine, "Activity_Plans") == 0
        assert _count(planning_engine, "Relationships") == 0

    def test_empty_title_rejected_as_missing(
        self,
        planning_engine: Engine,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """An empty title surfaces ``failed_constraint == 'title_missing'``.

        Per Requirement 6.3, an Activity Plan creation request that
        omits the Activity Plan title is rejected. The validator
        treats ``""`` as omission (the actionable next step is the
        same as a missing string).
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(ActivityPlanValidationError) as exc_info:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=_PROJECT_ID,
                    title="",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "title_missing"
        assert _count(planning_engine, "Activity_Plans") == 0

    @pytest.mark.parametrize("bad_title", [None, 123, 3.14, ["title"], {"t": 1}])
    def test_non_string_title_rejected_as_missing(
        self,
        planning_engine: Engine,
        activity_plan_service: ActivityPlanService,
        bad_title: object,
    ) -> None:
        """Non-string titles surface ``failed_constraint == 'title_missing'``.

        The validator collapses "wrong type" and "empty" into one
        actionable constraint because the next step is the same:
        supply a non-empty string of 1..200 chars.
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(ActivityPlanValidationError) as exc_info:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=_PROJECT_ID,
                    title=bad_title,  # type: ignore[arg-type]
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "title_missing"
        assert _count(planning_engine, "Activity_Plans") == 0

    def test_over_long_title_runs_before_project_lookup(
        self,
        planning_engine: Engine,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """A garbage Project Identity is never read from the DB when
        the title is out of range.

        The validator runs *before* the Project resolution SELECT,
        so an over-long title surfaces ``title_too_long`` rather
        than ``target_project_not_resolvable``. This is the ordering
        the route layer relies on for stable error messages.
        """
        _seed_required_parties(planning_engine)

        fake_project_id = "00000000-0000-7000-8000-0000deadbeef"
        with planning_engine.begin() as conn:
            with pytest.raises(ActivityPlanValidationError) as exc_info:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=fake_project_id,
                    title="x" * 500,
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == "title_too_long"


# ===========================================================================
# Requirement 6.2 — title length, schema CHECK layer (defense in depth).
#
# A hand-rolled INSERT that bypasses the static validator must still be
# rejected by the database. The schema declares
# ``CHECK (length(title) BETWEEN 1 AND 200)`` on ``Activity_Plans``;
# SQLite reports a violation through :class:`sqlalchemy.exc.IntegrityError`.
# ===========================================================================


class TestTitleLengthCheckLayer:
    """Schema CHECK constraint rejects out-of-range titles at INSERT time."""

    def test_direct_insert_with_empty_title_rejected_by_check(
        self,
        planning_engine: Engine,
    ) -> None:
        """A hand-rolled INSERT with an empty title is rejected by the
        ``Activity_Plans`` CHECK constraint.

        This bypasses the ActivityPlanService static validator entirely
        so the schema-level guarantee is exercised on its own.
        """
        _seed_required_parties(planning_engine)
        _seed_project(planning_engine)

        activity_plan_id = "00000000-0000-7000-8000-000000d00001"
        with planning_engine.connect() as conn, pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO Activity_Plans (
                            activity_plan_id, target_project_id, title,
                            authoring_party_id, applicable_scope, recorded_at
                        ) VALUES (
                            :ap, :pid, '', :party, :scope, :ts
                        )
                        """
                    ),
                    {
                        "ap": activity_plan_id,
                        "pid": _PROJECT_ID,
                        "party": _PARTY_ID,
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        assert _count(planning_engine, "Activity_Plans") == 0

    def test_direct_insert_with_over_long_title_rejected_by_check(
        self,
        planning_engine: Engine,
    ) -> None:
        """A hand-rolled INSERT with a 201-character title is rejected
        by the ``Activity_Plans`` CHECK constraint."""
        _seed_required_parties(planning_engine)
        _seed_project(planning_engine)

        activity_plan_id = "00000000-0000-7000-8000-000000d00002"
        with planning_engine.connect() as conn, pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO Activity_Plans (
                            activity_plan_id, target_project_id, title,
                            authoring_party_id, applicable_scope, recorded_at
                        ) VALUES (
                            :ap, :pid, :title, :party, :scope, :ts
                        )
                        """
                    ),
                    {
                        "ap": activity_plan_id,
                        "pid": _PROJECT_ID,
                        "title": "x" * 201,
                        "party": _PARTY_ID,
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        assert _count(planning_engine, "Activity_Plans") == 0

    def test_direct_insert_with_boundary_titles_accepted_by_check(
        self,
        planning_engine: Engine,
    ) -> None:
        """The CHECK accepts both the 1-char and 200-char boundary titles."""
        _seed_required_parties(planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            for ap_id, title in (
                ("00000000-0000-7000-8000-000000d00010", "A"),
                ("00000000-0000-7000-8000-000000d00011", "x" * 200),
            ):
                conn.execute(
                    text(
                        """
                        INSERT INTO Activity_Plans (
                            activity_plan_id, target_project_id, title,
                            authoring_party_id, applicable_scope, recorded_at
                        ) VALUES (
                            :ap, :pid, :title, :party, :scope, :ts
                        )
                        """
                    ),
                    {
                        "ap": ap_id,
                        "pid": _PROJECT_ID,
                        "title": title,
                        "party": _PARTY_ID,
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        assert _count(planning_engine, "Activity_Plans") == 2


# ===========================================================================
# Requirement 6.3 — unresolved Project.
#
# An Activity Plan creation request that names a target Project
# Resource Identity that does not resolve to an existing Project
# Resource is rejected. The check runs after input validation but
# *before* authorization evaluation so the deny path never reveals
# whether a Project exists for an unauthorized caller (AD-WS-9 /
# Requirement 10.4), and never leaves a partial row behind.
# ===========================================================================


class TestUnresolvedProjectRejection:
    """Unresolvable target Projects raise the dedicated error type."""

    def test_unresolved_project_id_raises_dedicated_error(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """A target Project Identity with no matching ``Projects`` row
        raises :class:`ActivityPlanProjectNotResolvableError`.

        The error carries the offending Identity verbatim so the
        route layer can surface a structured 4xx response per
        Requirement 6.3 ("...return an error indication identifying
        the missing or invalid attribute").
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        # No _seed_project — the target_project_id below does not
        # resolve to any ``Projects`` row.

        fake_project_id = "00000000-0000-7000-8000-0000deadbeef"
        with pytest.raises(ActivityPlanProjectNotResolvableError) as exc_info:
            with planning_engine.begin() as conn:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=fake_project_id,
                    title="Doomed",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.target_project_id == fake_project_id
        assert exc_info.value.failed_constraint == (
            "target_project_not_resolvable"
        )

    def test_unresolved_project_lookup_persists_nothing(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """When the target Project does not resolve, no
        ``Activity_Plans``, ``Relationships``, ``Audit_Records``, or
        ``Identifier_Registry`` row is added.

        The Project resolution SELECT runs before identifier minting,
        so an unresolvable target surfaces
        :class:`ActivityPlanProjectNotResolvableError` without
        leaving any Slice 2 row behind.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        # Snapshot the registry size *after* the role-assignment write
        # (which itself may register identifiers) so the comparison
        # isolates Activity-Plan-side inserts.
        registry_before = _count(planning_engine, "Identifier_Registry")
        audit_before = _count(planning_engine, "Audit_Records")
        relationships_before = _count(planning_engine, "Relationships")

        fake_project_id = "00000000-0000-7000-8000-0000deadbeef"
        with pytest.raises(ActivityPlanProjectNotResolvableError):
            with planning_engine.begin() as conn:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=fake_project_id,
                    title="Doomed",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert _count(planning_engine, "Activity_Plans") == 0
        assert _count(planning_engine, "Identifier_Registry") == registry_before
        assert _count(planning_engine, "Relationships") == relationships_before
        assert _count(planning_engine, "Audit_Records") == audit_before

    def test_unresolved_project_check_runs_before_authorization(
        self,
        planning_engine: Engine,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """The Project resolution check fires even when the caller
        holds no role assignment.

        Requirement 6.3 (missing/invalid target Project) and
        Requirement 6.4 (unauthorized caller) are distinct denial
        paths. The implementation surfaces the
        :class:`ActivityPlanProjectNotResolvableError` first so the
        error mapping in the route layer is stable: an
        unresolvable-target failure cannot be silently rewritten
        into an authorization denial.
        """
        _seed_required_parties(planning_engine)
        # No role assignment, no Project — but the Project lookup
        # should fire before any authorization evaluation.

        fake_project_id = "00000000-0000-7000-8000-0000deadbeef"
        with pytest.raises(ActivityPlanProjectNotResolvableError):
            with planning_engine.begin() as conn:
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=fake_project_id,
                    title="Doomed",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )


# ===========================================================================
# Requirement 4.5 — identifier-set tagging.
#
# Activity Plan Resource Identity is registered in
# ``Identifier_Registry`` with the additive ``resource_kind`` tag set
# to ``'activity_plan'``. The tag is what keeps Activity Plan Resource
# identifiers inspectably disjoint from Project Resource identifiers
# at row granularity (AD-WS-19).
#
# Activity Plans do not carry Revisions (AD-WS-3 / design
# §"Planning_Service.ActivityPlans") so there is only one identifier
# to tag per creation — distinct from the Projects pattern which
# tags both a Resource and first-Revision identifier.
# ===========================================================================


class TestIdentifierSetTagging:
    """Activity Plan identifiers carry the AD-WS-19 ``resource_kind`` tag."""

    def test_activity_plan_resource_id_tagged_activity_plan(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """The Activity Plan Resource Identity has
        ``resource_kind = 'activity_plan'``."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = activity_plan_service.create_activity_plan(
                conn,
                target_project_id=_PROJECT_ID,
                title="Tagged",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        row = _fetch_registry_row(planning_engine, result.activity_plan_id)
        assert row is not None
        assert row["kind"] == "resource"
        assert row["resource_kind"] == "activity_plan"

    def test_activity_plan_resource_id_is_canonical_uuid7(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """Resource Identity is a canonical UUIDv7 — Slice 1
        Requirement 1.1 carried into Slice 2 by :class:`IdentityService`."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            result = activity_plan_service.create_activity_plan(
                conn,
                target_project_id=_PROJECT_ID,
                title="Canonical",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert _CANONICAL_UUID7.match(result.activity_plan_id)

    def test_activity_plan_resource_id_disjoint_from_project_resource_id(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """Activity Plan and Project Resource identifiers carry
        distinct ``resource_kind`` tags so the two identifier sets are
        inspectably disjoint at row granularity (Requirement 4.5).

        Driven directly through ``Identifier_Registry`` for the
        Project side (its creation path lives in task 5.1's
        :class:`ProjectService` and is exercised in
        ``test_planning_projects.py``); the Activity Plan side is
        driven through :meth:`create_activity_plan` so the tag
        flowing through :func:`_record_planning_resource` is verified
        end-to-end.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)

        with planning_engine.begin() as conn:
            activity_plan_result = (
                activity_plan_service.create_activity_plan(
                    conn,
                    target_project_id=_PROJECT_ID,
                    title="Mesh Activities",
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
            )

        # Independently register a Project-tagged identifier.
        project_identifier = "00000000-0000-7000-8000-0000000ee001"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO Identifier_Registry
                        (identifier, kind, content_digest, issued_at,
                         resource_kind)
                    VALUES
                        (:id, 'resource', 'd', :ts, 'project')
                    """
                ),
                {"id": project_identifier, "ts": _TS_FIXED},
            )

        activity_row = _fetch_registry_row(
            planning_engine, activity_plan_result.activity_plan_id
        )
        project_row = _fetch_registry_row(planning_engine, project_identifier)

        assert activity_row is not None
        assert project_row is not None
        # Identifiers themselves are distinct.
        assert activity_row["identifier"] != project_row["identifier"]
        # And the AD-WS-19 ``resource_kind`` tag distinguishes them.
        assert activity_row["resource_kind"] == "activity_plan"
        assert project_row["resource_kind"] == "project"

    def test_activity_plan_resource_id_unique_in_registry(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        activity_plan_service: ActivityPlanService,
    ) -> None:
        """Each Activity Plan creation INSERTs exactly one row in
        ``Identifier_Registry`` tagged ``'activity_plan'``.

        Counts the rows tagged ``'activity_plan'`` before and after
        two distinct creations; the delta is 2.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_project(planning_engine)

        def _activity_plan_tag_count() -> int:
            with planning_engine.connect() as conn:
                return int(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM Identifier_Registry "
                            "WHERE resource_kind = 'activity_plan'"
                        )
                    ).scalar_one()
                )

        before = _activity_plan_tag_count()

        with planning_engine.begin() as conn:
            activity_plan_service.create_activity_plan(
                conn,
                target_project_id=_PROJECT_ID,
                title="First",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )
        with planning_engine.begin() as conn:
            activity_plan_service.create_activity_plan(
                conn,
                target_project_id=_PROJECT_ID,
                title="Second",
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert _activity_plan_tag_count() - before == 2
        assert _count(planning_engine, "Activity_Plans") == 2
