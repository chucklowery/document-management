"""Unit tests for :mod:`walking_slice.planning.projects` (task 5.2).

Pins the contract established in task 5.1, design
§"Planning_Service.Projects", and Requirements 4.2, 4.3, 4.5 for
:meth:`ProjectService.create_project`:

- **4.2 / 4.3** — planned-date order validation. The static validator
  in :class:`ProjectService` rejects ``planned_start_date >
  planned_end_date`` with the stable ``failed_constraint`` identifier
  ``planned_date_range_inverted`` *before* any database read or write.
  The schema CHECK constraint on ``Project_Revisions``
  (``CHECK (planned_start_date <= planned_end_date)``) is the
  defense-in-depth layer: a hand-rolled INSERT that bypasses the
  service-level validator is rejected by the database with
  :class:`sqlalchemy.exc.IntegrityError`.
- **4.5** — Project Resource Identity and the first Project Revision
  Identity are tagged in ``Identifier_Registry`` with
  ``resource_kind = 'project'`` and ``resource_kind = 'project_revision'``
  respectively. The tagging is what keeps Project Resource identifiers
  inspectably disjoint from Activity Plan Resource identifiers at row
  granularity (AD-WS-19).

The tests intentionally mirror the test style of
``tests/unit/test_planning_objectives.py``: a per-test engine carrying
both the Slice 1 and Slice 2 schemas, a real
:class:`AuthorizationService` driven through a seeded role assignment
on the happy paths, and counter helpers that confirm nothing was
persisted on the negative paths.
"""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timezone
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
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning.projects import (
    CreateProjectResult,
    ProjectObjectiveNotResolvableError,
    ProjectService,
    ProjectValidationError,
)
from walking_slice.models import AuthorityBasisRef


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_OBJECTIVE_ID = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID = "00000000-0000-7000-8000-000000c00003"
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
    ``Identifier_Registry.resource_kind`` and ``Relationships.semantic_role``
    columns (task 1.2); ``create_planning_schema`` installs every Slice 2
    table, index, and append-only trigger (task 1.3). No disclosure
    seeding is required: Project creation does not consult the
    disclosure registry.
    """
    create_schema(engine)
    create_planning_schema(engine)
    return engine


@pytest.fixture
def project_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> ProjectService:
    """ProjectService wired with a real AuthorizationService.

    The same instance is used by every test in this module; the
    authorization deny path is exercised by *not* assigning a role
    rather than by swapping in a stub service, so the real evaluation
    code path participates in the test.
    """
    return ProjectService(
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


def _seed_objective(engine: Engine) -> None:
    """Seed one Objective Resource + its first Objective Revision.

    Inserted directly (the upstream Decision dependency is irrelevant
    to the Project tests; the schema only requires the Objective row
    to exist for Project_Revisions.target_objective_id to resolve).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _TS_FIXED},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id, parent_revision_id,
                    statement, rationale, target_decision_id,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :rev, :oid, NULL,
                    'Adopt service-mesh telemetry.',
                    'Anchored on the accepted decision.',
                    :did, :pid, :scope, :ts
                )
                """
            ),
            {
                "rev": _OBJECTIVE_REV_ID,
                "oid": _OBJECTIVE_ID,
                "did": _DECISION_ID,
                "pid": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIXED,
            },
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

    Per AD-WS-15, ``create.project`` maps to the ``modify`` authority
    type. A Party with an effective Role Assignment carrying
    ``modify`` over ``scope`` is permitted to create Projects in that
    scope.
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


def test_create_project_permits_when_project_owner_role_grants_modify(
    planning_engine: Engine,
    authorization_service: AuthorizationService,
    project_service: ProjectService,
) -> None:
    """Permit path: with an effective Project Owner role and an
    existing Objective, the service creates one Project Resource, one
    Project Revision, one ``Addresses`` Relationship, and one
    consequential audit row inside one transaction (AD-WS-5)."""
    _seed_required_parties(planning_engine)
    _assign_project_owner_role(authorization_service, planning_engine)
    _seed_objective(planning_engine)

    with planning_engine.begin() as conn:
        result = project_service.create_project(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            name="Mesh Rollout",
            summary="Roll out the service mesh.",
            planned_start_date=date(2026, 1, 15),
            planned_end_date=date(2026, 6, 30),
            authoring_party_id=_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=planning_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateProjectResult)
    assert _CANONICAL_UUID7.match(result.project_id)
    assert _CANONICAL_UUID7.match(result.project_revision_id)
    assert _CANONICAL_UUID7.match(result.addresses_relationship_id)
    assert result.target_objective_id == _OBJECTIVE_ID
    assert result.planned_start_date == "2026-01-15"
    assert result.planned_end_date == "2026-06-30"
    assert result.correlation_id == "corr-permit"

    assert _count(planning_engine, "Projects") == 1
    assert _count(planning_engine, "Project_Revisions") == 1


# ===========================================================================
# Requirement 4.2 / 4.3 — planned-date order validation, Pydantic layer.
#
# Per task 5.1's note in the prompt, the implementation uses a static
# validator (not Pydantic) to enforce planned-date order. The constraint
# name for an inverted range is ``planned_date_range_inverted``; the
# validator is exercised here through the public service surface so the
# behavior the route layer (task 15.1) relies on is pinned.
# ===========================================================================


class TestPlannedDateOrderValidatorLayer:
    """Static validator rejects inverted ranges before any DB read.

    The validator is the first line of defense: it runs ahead of the
    Objective resolution SELECT, the authorization evaluation, and any
    INSERT, so a malformed request never touches the database. The
    schema CHECK is exercised in :class:`TestPlannedDateOrderCheckLayer`
    below.
    """

    def test_inverted_range_rejected_with_stable_constraint_name(
        self,
        planning_engine: Engine,
        project_service: ProjectService,
    ) -> None:
        """``planned_start_date > planned_end_date`` raises with
        ``failed_constraint == 'planned_date_range_inverted'``.

        The error type is :class:`ProjectValidationError`; the
        ``failed_constraint`` is the stable identifier the route
        layer maps to a structured 400 response.
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(ProjectValidationError) as exc_info:
                project_service.create_project(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    name="Mesh Rollout",
                    summary=None,
                    planned_start_date=date(2026, 6, 30),
                    planned_end_date=date(2026, 1, 15),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "planned_date_range_inverted"
        )
        # Nothing persisted: the validator runs before identifier
        # minting, the Objective resolution SELECT, and any INSERT.
        assert _count(planning_engine, "Projects") == 0
        assert _count(planning_engine, "Project_Revisions") == 0

    def test_equal_dates_accepted(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        project_service: ProjectService,
    ) -> None:
        """``planned_start_date == planned_end_date`` is accepted.

        Requirement 4.2 phrases the constraint as "planned start date
        not after the planned end date" — equality is permitted.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = project_service.create_project(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                name="One-Day Project",
                summary=None,
                planned_start_date=date(2026, 3, 1),
                planned_end_date=date(2026, 3, 1),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.planned_start_date == "2026-03-01"
        assert result.planned_end_date == "2026-03-01"
        assert _count(planning_engine, "Projects") == 1

    def test_inverted_range_runs_before_objective_lookup(
        self,
        planning_engine: Engine,
        project_service: ProjectService,
    ) -> None:
        """A garbage Objective Identity is never read from the DB
        when the dates are inverted.

        The validator runs *before* the Objective resolution SELECT,
        so an inverted range surfaces ``planned_date_range_inverted``
        rather than ``target_objective_not_resolvable``. This is the
        ordering the route layer relies on for stable error messages.
        """
        _seed_required_parties(planning_engine)

        fake_objective_id = "00000000-0000-7000-8000-0000deadbeef"
        with planning_engine.begin() as conn:
            with pytest.raises(ProjectValidationError) as exc_info:
                project_service.create_project(
                    conn,
                    target_objective_id=fake_objective_id,
                    name="Mesh Rollout",
                    summary=None,
                    planned_start_date=date(2026, 12, 31),
                    planned_end_date=date(2026, 1, 1),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.failed_constraint == (
            "planned_date_range_inverted"
        )

    @pytest.mark.parametrize(
        "bad_value",
        [
            datetime(2026, 1, 15, tzinfo=timezone.utc),  # datetime, not date
            "2026-01-15",  # ISO string, not date
            None,
        ],
    )
    def test_planned_start_date_invalid_type_rejected(
        self,
        planning_engine: Engine,
        project_service: ProjectService,
        bad_value: object,
    ) -> None:
        """Non-``date`` values for ``planned_start_date`` raise with
        ``planned_start_date_invalid_type`` before any DB access.

        Note that :class:`datetime.datetime` instances are explicitly
        rejected even though they subclass :class:`datetime.date` —
        the schema column type is calendar date.
        """
        _seed_required_parties(planning_engine)

        with planning_engine.begin() as conn:
            with pytest.raises(ProjectValidationError) as exc_info:
                project_service.create_project(
                    conn,
                    target_objective_id=_OBJECTIVE_ID,
                    name="Mesh Rollout",
                    summary=None,
                    planned_start_date=bad_value,  # type: ignore[arg-type]
                    planned_end_date=date(2026, 6, 30),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )
        assert exc_info.value.failed_constraint == (
            "planned_start_date_invalid_type"
        )
        assert _count(planning_engine, "Projects") == 0


# ===========================================================================
# Requirement 4.2 / 4.3 — planned-date order, schema CHECK layer
# (defense in depth).
#
# A hand-rolled INSERT that bypasses the static validator must still be
# rejected by the database. The schema declares
# ``CHECK (planned_start_date <= planned_end_date)`` on
# ``Project_Revisions``; SQLite reports a violation through
# :class:`sqlalchemy.exc.IntegrityError`.
# ===========================================================================


class TestPlannedDateOrderCheckLayer:
    """Schema CHECK constraint rejects inverted ranges at INSERT time."""

    def test_direct_insert_with_inverted_range_rejected_by_check(
        self,
        planning_engine: Engine,
    ) -> None:
        """A hand-rolled INSERT with start > end is rejected by the
        ``Project_Revisions`` CHECK constraint.

        This bypasses the ProjectService static validator entirely so
        the schema-level guarantee is exercised on its own. ISO-8601
        calendar dates lexicographically compare as expected, so
        ``'2026-12-31' <= '2026-01-01'`` is false and the CHECK
        rejects the row.
        """
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        # Pre-create a Project header so the FK target exists and the
        # only constraint left to fail is the date-order CHECK.
        project_id = "00000000-0000-7000-8000-000000d00001"
        revision_id = "00000000-0000-7000-8000-000000d00002"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO Projects (project_id, created_at) "
                    "VALUES (:pid, :ts)"
                ),
                {"pid": project_id, "ts": _TS_FIXED},
            )

        with planning_engine.connect() as conn, pytest.raises(IntegrityError):
            with conn.begin():
                conn.execute(
                    text(
                        """
                        INSERT INTO Project_Revisions (
                            project_revision_id, project_id,
                            parent_revision_id, name, summary,
                            target_objective_id, planned_start_date,
                            planned_end_date, authoring_party_id,
                            applicable_scope, recorded_at
                        ) VALUES (
                            :rev, :pid, NULL,
                            'Inverted', NULL, :oid,
                            '2026-12-31', '2026-01-01',
                            :party, :scope, :ts
                        )
                        """
                    ),
                    {
                        "rev": revision_id,
                        "pid": project_id,
                        "oid": _OBJECTIVE_ID,
                        "party": _PARTY_ID,
                        "scope": _SCOPE,
                        "ts": _TS_FIXED,
                    },
                )

        assert _count(planning_engine, "Project_Revisions") == 0

    def test_direct_insert_with_equal_dates_accepted_by_check(
        self,
        planning_engine: Engine,
    ) -> None:
        """The CHECK accepts equal dates (the constraint is ``<=``)."""
        _seed_required_parties(planning_engine)
        _seed_objective(planning_engine)

        project_id = "00000000-0000-7000-8000-000000d00010"
        revision_id = "00000000-0000-7000-8000-000000d00011"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO Projects (project_id, created_at) "
                    "VALUES (:pid, :ts)"
                ),
                {"pid": project_id, "ts": _TS_FIXED},
            )
            conn.execute(
                text(
                    """
                    INSERT INTO Project_Revisions (
                        project_revision_id, project_id,
                        parent_revision_id, name, summary,
                        target_objective_id, planned_start_date,
                        planned_end_date, authoring_party_id,
                        applicable_scope, recorded_at
                    ) VALUES (
                        :rev, :pid, NULL,
                        'Same-Day', NULL, :oid,
                        '2026-03-01', '2026-03-01',
                        :party, :scope, :ts
                    )
                    """
                ),
                {
                    "rev": revision_id,
                    "pid": project_id,
                    "oid": _OBJECTIVE_ID,
                    "party": _PARTY_ID,
                    "scope": _SCOPE,
                    "ts": _TS_FIXED,
                },
            )

        assert _count(planning_engine, "Project_Revisions") == 1


# ===========================================================================
# Requirement 4.5 — identifier-set tagging.
#
# Project Resource and first Project Revision identifiers are registered
# in ``Identifier_Registry`` with the additive ``resource_kind`` tag set
# to ``'project'`` and ``'project_revision'`` respectively. The tagging
# is what keeps Project Resource identifiers inspectably disjoint from
# Activity Plan Resource identifiers at row granularity (AD-WS-19).
# ===========================================================================


class TestIdentifierSetTagging:
    """Project identifiers carry the AD-WS-19 ``resource_kind`` tag."""

    def test_project_resource_id_tagged_project(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        project_service: ProjectService,
    ) -> None:
        """The Project Resource Identity has ``resource_kind = 'project'``."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = project_service.create_project(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                name="Tagged",
                summary=None,
                planned_start_date=date(2026, 1, 15),
                planned_end_date=date(2026, 6, 30),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        row = _fetch_registry_row(planning_engine, result.project_id)
        assert row is not None
        assert row["kind"] == "resource"
        assert row["resource_kind"] == "project"

    def test_project_revision_id_tagged_project_revision(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        project_service: ProjectService,
    ) -> None:
        """The first Revision Identity has
        ``resource_kind = 'project_revision'``."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = project_service.create_project(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                name="Tagged Revision",
                summary=None,
                planned_start_date=date(2026, 1, 15),
                planned_end_date=date(2026, 6, 30),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        row = _fetch_registry_row(planning_engine, result.project_revision_id)
        assert row is not None
        assert row["kind"] == "revision"
        assert row["resource_kind"] == "project_revision"

    def test_project_and_revision_ids_distinct(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        project_service: ProjectService,
    ) -> None:
        """Resource Identity and first-Revision Identity are distinct
        UUIDv7s — Slice 1 Requirement 1.2 carried into Slice 2 by
        :class:`IdentityService`."""
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            result = project_service.create_project(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                name="Distinct",
                summary=None,
                planned_start_date=date(2026, 1, 15),
                planned_end_date=date(2026, 6, 30),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        assert result.project_id != result.project_revision_id
        assert _CANONICAL_UUID7.match(result.project_id)
        assert _CANONICAL_UUID7.match(result.project_revision_id)

    def test_project_resource_id_disjoint_from_activity_plan_resource_id(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        project_service: ProjectService,
    ) -> None:
        """Project and Activity Plan Resource identifiers carry distinct
        ``resource_kind`` tags so the two identifier sets are
        inspectably disjoint at row granularity (Requirement 4.5).

        Driven directly through ``Identifier_Registry`` for the
        Activity Plan side (its service ships in task 7.1); the
        Project side is driven through :meth:`create_project` so the
        tag flowing through :func:`_record_planning_resource` is
        verified end-to-end.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        _seed_objective(planning_engine)

        with planning_engine.begin() as conn:
            project_result = project_service.create_project(
                conn,
                target_objective_id=_OBJECTIVE_ID,
                name="Mesh Rollout",
                summary=None,
                planned_start_date=date(2026, 1, 15),
                planned_end_date=date(2026, 6, 30),
                authoring_party_id=_PARTY_ID,
                applicable_scope=_SCOPE,
                engine=planning_engine,
            )

        # Independently register an Activity Plan-tagged identifier.
        activity_plan_id = "00000000-0000-7000-8000-0000000aa001"
        with planning_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO Identifier_Registry
                        (identifier, kind, content_digest, issued_at,
                         resource_kind)
                    VALUES
                        (:id, 'resource', 'd', :ts, 'activity_plan')
                    """
                ),
                {"id": activity_plan_id, "ts": _TS_FIXED},
            )

        project_row = _fetch_registry_row(
            planning_engine, project_result.project_id
        )
        activity_row = _fetch_registry_row(planning_engine, activity_plan_id)

        assert project_row is not None
        assert activity_row is not None
        # Identifiers themselves are distinct.
        assert project_row["identifier"] != activity_row["identifier"]
        # And the AD-WS-19 ``resource_kind`` tag distinguishes them.
        assert project_row["resource_kind"] == "project"
        assert activity_row["resource_kind"] == "activity_plan"

    def test_objective_lookup_failure_persists_no_registry_row(
        self,
        planning_engine: Engine,
        authorization_service: AuthorizationService,
        project_service: ProjectService,
    ) -> None:
        """When the target Objective does not resolve, no
        ``Identifier_Registry`` row is added.

        The Objective resolution SELECT runs before identifier minting,
        so an unresolvable Objective surfaces
        :class:`ProjectObjectiveNotResolvableError` without leaving any
        Slice 2 identifier behind.
        """
        _seed_required_parties(planning_engine)
        _assign_project_owner_role(authorization_service, planning_engine)
        # Snapshot the registry size *after* the role-assignment write
        # (which itself may register identifiers) so the comparison
        # isolates Project-side inserts.
        before = _count(planning_engine, "Identifier_Registry")

        fake_objective_id = "00000000-0000-7000-8000-0000deadbeef"
        with pytest.raises(ProjectObjectiveNotResolvableError) as exc_info:
            with planning_engine.begin() as conn:
                project_service.create_project(
                    conn,
                    target_objective_id=fake_objective_id,
                    name="Doomed",
                    summary=None,
                    planned_start_date=date(2026, 1, 15),
                    planned_end_date=date(2026, 6, 30),
                    authoring_party_id=_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=planning_engine,
                )

        assert exc_info.value.target_objective_id == fake_objective_id
        assert _count(planning_engine, "Identifier_Registry") == before
        assert _count(planning_engine, "Projects") == 0
        assert _count(planning_engine, "Project_Revisions") == 0
