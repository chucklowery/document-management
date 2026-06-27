"""Unit tests for :mod:`walking_slice.execution.time_entries` (task 7.2).

Pins the contract established in task 7.1, design
§"Execution_Service.TimeEntries", AD-WS-9 (separate-transaction
Denial Record), AD-WS-24 (``create.time_entry`` → ``contribute``),
AD-WS-26 (Relationship-Type / semantic-role table), AD-WS-27
(append-only Slice 3 tables), AD-WS-28 (additive ``resource_kind``
values), AD-WS-29 (two-stage Contributor authority evaluation), and
Requirements 25.2, 25.3, 25.4, 25.5, 32.7:

- **25.2 — ``effort_hours`` boundary values.** ``0.00`` and ``24.00``
  sit at the inclusive boundaries (accepted); ``24.01`` (one tick past
  the upper bound), ``-0.01`` (one tick past the lower bound), and any
  value carrying three or more fractional digits are rejected.
  Validation runs in the static validator before any database read so
  a malformed request never touches the Work-Assignment lookup or the
  authorization service. The schema CHECK constraints on
  ``Time_Entry_Records.effort_hours`` are the defense-in-depth layer,
  exercised in ``tests/unit/test_execution_persistence.py``.
- **25.3 — effort-period ordering.**
  ``effort_period_start > effort_period_end`` raises with
  ``failed_constraint='effort_period_start_after_end'`` (rejected
  pre-database). ``effort_period_end > recorded_at`` raises with
  ``failed_constraint='effort_period_end_after_recorded_at'``
  (rejected after the clock is consulted but before any row is
  written).
- **32.7 / AD-WS-29 — assignee-binding rejection.** Even when the
  recording Party holds the ``contribute`` authority on the Work
  Assignment's scope, the request is rejected unless the persisted
  ``Work_Assignment_Records.assignee_party_id`` matches the supplied
  ``recording_party_id``. The rejection surfaces as
  :class:`TimeEntryAssigneeBindingError` with
  ``reason_code = 'no-role-assignment'`` and persists exactly one
  Denial Record in a separate transaction; the caller's transaction
  rolls back so no ``Time_Entry_Records`` row, no ``Relationships``
  row, and no consequential audit row is persisted.
- **25.4 — authorization deny path.** A denied request appends
  exactly one Denial Record in a separate transaction and raises
  :class:`TimeEntryAuthorizationError`; no
  ``Time_Entry_Records`` row, no ``Relationships`` row, and no
  consequential audit row is persisted (Requirement 25.5 — the
  consequential audit row only fires alongside a successful row
  insert).

The tests mirror the style of
``tests/unit/test_execution_work_assignments.py`` (task 5.2) and
``tests/unit/test_deliverables_repository.py`` (task 4.3): a per-test
engine carrying the Slice 1 + Slice 3 Execution schemas, a real
:class:`AuthorizationService` driven through a seeded role assignment
on happy paths, direct INSERTs to seed the Work Assignment Record
fixture, and counter helpers that confirm nothing was persisted on
negative paths.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.time_entries import (
    CreateTimeEntryResult,
    TimeEntryAssigneeBindingError,
    TimeEntryAuthorizationError,
    TimeEntryService,
    TimeEntryValidationError,
)
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


_RECORDING_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000a00002"
_ASSIGNMENT_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00003"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00004"

_BOUND_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00001"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-000000b00001")
_SCOPE = "pilot/team-a"

# The per-test ``clock`` fixture is :class:`FixedClock` pinned to
# ``2026-01-01T00:00:00.000Z``. The effort-period boundaries below sit
# strictly inside that window so every happy-path call satisfies
# ``effort_period_start <= effort_period_end <= recorded_at``.
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_RECORDED_AT_DT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
_PERIOD_START_DT = datetime(2025, 12, 31, 22, 0, 0, tzinfo=timezone.utc)
_PERIOD_END_DT = datetime(2025, 12, 31, 23, 0, 0, tzinfo=timezone.utc)

_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def execution_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 3 Execution_Service schemas.

    ``create_schema`` installs the Slice 1 tables (``Parties``,
    ``Identifier_Registry``, ``Audit_Records``, ``Role_Assignments``,
    ``Relationships``, plus the additive
    ``Identifier_Registry.resource_kind`` and
    ``Relationships.semantic_role`` columns from task 1.2).
    ``create_execution_schema`` installs the Slice 3 Execution_Service
    tables including ``Work_Assignment_Records`` and
    ``Time_Entry_Records`` with their AD-WS-27 append-only triggers
    and Requirement 25.2 / 25.3 CHECK constraints.

    The Slice 2 Planning schema and the Deliverable_Repository schema
    are intentionally not installed — the Time Entry Service resolves
    only the originating Work Assignment Record by primary key, so a
    smaller surface keeps the fixture minimal.
    """
    create_schema(engine)
    create_execution_schema(engine)
    return engine


@pytest.fixture
def time_entry_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> TimeEntryService:
    """:class:`TimeEntryService` wired with a real :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a role
    rather than by swapping in a stub service, so the real evaluation
    code path participates in the test. The denial-audit sleep is
    replaced with a no-op so the deny-path retries do not spend real
    time.
    """
    return TimeEntryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
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
    """Seed every Party identity referenced by the test surface.

    All four Parties are required: the recording Contributor, an
    alternate Party used to exercise the AD-WS-29 mismatch path, the
    Assignment-Authority Party (named on
    ``Work_Assignment_Records.assignment_authority_party_id``), and
    the Assigning-Authority Party recorded on the seeded role.
    """
    with engine.begin() as conn:
        _seed_party(conn, _RECORDING_PARTY_ID, "Contributor")
        _seed_party(conn, _OTHER_PARTY_ID, "Other Contributor")
        _seed_party(conn, _ASSIGNMENT_AUTHORITY_ID, "Assignment Authority")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str = _BOUND_WORK_ASSIGNMENT_ID,
    assignee_party_id: str = _RECORDING_PARTY_ID,
    assignment_authority_party_id: str = _ASSIGNMENT_AUTHORITY_ID,
    applicable_scope: str = _SCOPE,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The AD-WS-27 UPDATE/DELETE rejection triggers only fire on UPDATE
    and DELETE, so an INSERT may proceed in one statement without
    driving the full :class:`WorkAssignmentService`. The
    ``assignee_party_id != assignment_authority_party_id`` CHECK
    constraint (Requirement 23.5) is honored by the default values.

    The ``target_plan_revision_id`` column has no FK to a Slice 2
    table that exists in this fixture (Plan Revisions are not
    installed), so a plausible UUID is sufficient — SQLite resolves
    FK targets only against tables present in the schema.
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
                    'Assigning the rollout.', 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_assignment_id,
                "prev": "00000000-0000-7000-8000-000000c00030",
                "assignee": assignee_party_id,
                "authority": assignment_authority_party_id,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _assign_contribute_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _RECORDING_PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Contributor authority (``contribute``) to ``party_id``.

    Per AD-WS-24, ``create.time_entry`` maps to the ``contribute``
    authority. A Party with an effective Role Assignment carrying
    ``contribute`` over ``scope`` plus the AD-WS-29 assignee binding
    may record Time Entries.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="contributor",
        scope=scope,
        authorities_granted=("contribute",),
        effective_start=effective_start,
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_happy_path(
    engine: Engine,
    authorization_service: AuthorizationService,
    *,
    assignee_party_id: str = _RECORDING_PARTY_ID,
) -> None:
    """Seed every dependency required for a permitted
    :meth:`TimeEntryService.create_time_entry` call.

    The default ``assignee_party_id`` matches the recording Party so
    AD-WS-29's second stage passes; tests for the assignee-binding
    mismatch override this argument with a different Party.
    """
    _seed_required_parties(engine)
    _assign_contribute_role(authorization_service, engine)
    _seed_work_assignment(
        engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=assignee_party_id,
    )


# ---------------------------------------------------------------------------
# Row counters.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _count_denial_audit_rows(engine: Engine, action_type: str) -> int:
    """Count Denial Record rows for ``action_type``.

    A Denial Record is distinguished from the authorization evaluation
    row (which also carries ``outcome='deny'``) by
    ``authorities_required`` being NULL — the evaluation row always
    populates that column with the JSON-encoded required authority.
    """
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE outcome = 'deny' "
                    "AND action_type = :a "
                    "AND authorities_required IS NULL"
                ),
                {"a": action_type},
            ).scalar_one()
        )


def _count_consequential_audit_rows(engine: Engine, action_type: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE outcome = 'consequential' "
                    "AND action_type = :a"
                ),
                {"a": action_type},
            ).scalar_one()
        )


# ===========================================================================
# Happy-path baseline — confirms wiring and pins the result-object surface.
# ===========================================================================


def test_create_time_entry_permits_and_records_one_relationship(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
    time_entry_service: TimeEntryService,
) -> None:
    """Happy path: an authorized assignee Contributor records exactly
    one Time Entry Record, one ``Relates To`` Relationship to the
    target Work Assignment Record with ``semantic_role = 'time_entry'``
    (AD-WS-26), and one consequential audit row inside one
    transaction.

    This is the headline assertion for Requirements 25.1, 25.2, and
    25.5: the consequential audit row participates in the same
    transaction so the count is exactly one.
    """
    _seed_happy_path(execution_engine, authorization_service)

    with execution_engine.begin() as conn:
        result = time_entry_service.create_time_entry(
            conn,
            target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
            effort_hours=Decimal("1.50"),
            effort_period_start=_PERIOD_START_DT,
            effort_period_end=_PERIOD_END_DT,
            recording_party_id=_RECORDING_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=execution_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateTimeEntryResult)
    assert _CANONICAL_UUID7.match(result.time_entry_id)
    assert _CANONICAL_UUID7.match(result.relates_to_relationship_id)
    assert result.target_work_assignment_id == _BOUND_WORK_ASSIGNMENT_ID
    assert result.effort_hours == Decimal("1.50")
    assert result.recording_party_id == _RECORDING_PARTY_ID
    assert result.correlation_id == "corr-permit"

    assert _count(execution_engine, "Time_Entry_Records") == 1
    assert _count_consequential_audit_rows(
        execution_engine, "create.time_entry"
    ) == 1

    # Exactly one ``Relates To`` Relationship with
    # ``semantic_role='time_entry'`` per AD-WS-26.
    with execution_engine.connect() as conn:
        row_count = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Relates To' "
                "AND source_id = :sid "
                "AND target_id = :tid "
                "AND semantic_role = 'time_entry'"
            ),
            {
                "sid": result.time_entry_id,
                "tid": _BOUND_WORK_ASSIGNMENT_ID,
            },
        ).scalar_one()
    assert row_count == 1


# ===========================================================================
# Requirement 25.2 — ``effort_hours`` boundary values.
# ===========================================================================


class TestEffortHoursBoundaries:
    """Per Requirement 25.2 / design §"Effort-quantity validation":
    ``effort_hours`` is a non-negative :class:`~decimal.Decimal` with
    at most two fractional digits and at most 24.00 hours.

    Accepted-boundary tests drive the full happy-path persistence so
    the row actually lands and the boundary value is observable on the
    persisted ``effort_hours`` text column. Rejected-boundary tests
    confirm the rejection fires *before* any database write so the
    schema-level CHECK constraints are never exercised (they remain a
    defense-in-depth layer covered separately in
    ``tests/unit/test_execution_persistence.py``).
    """

    def test_zero_point_zero_zero_is_accepted_at_lower_boundary(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        time_entry_service: TimeEntryService,
    ) -> None:
        """``Decimal("0.00")`` sits at the lower inclusive boundary
        and is persisted as the canonical two-decimal-place string
        ``"0.00"``.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with execution_engine.begin() as conn:
            result = time_entry_service.create_time_entry(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                effort_hours=Decimal("0.00"),
                effort_period_start=_PERIOD_START_DT,
                effort_period_end=_PERIOD_END_DT,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        assert result.effort_hours == Decimal("0.00")
        with execution_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT effort_hours FROM Time_Entry_Records "
                    "WHERE time_entry_id = :id"
                ),
                {"id": result.time_entry_id},
            ).scalar_one()
        assert stored == "0.00"

    def test_twenty_four_point_zero_zero_is_accepted_at_upper_boundary(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        time_entry_service: TimeEntryService,
    ) -> None:
        """``Decimal("24.00")`` sits at the upper inclusive boundary
        and is persisted as ``"24.00"``.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with execution_engine.begin() as conn:
            result = time_entry_service.create_time_entry(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                effort_hours=Decimal("24.00"),
                effort_period_start=_PERIOD_START_DT,
                effort_period_end=_PERIOD_END_DT,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        assert result.effort_hours == Decimal("24.00")
        with execution_engine.connect() as conn:
            stored = conn.execute(
                text(
                    "SELECT effort_hours FROM Time_Entry_Records "
                    "WHERE time_entry_id = :id"
                ),
                {"id": result.time_entry_id},
            ).scalar_one()
        assert stored == "24.00"

    def test_twenty_four_point_zero_one_is_rejected_above_upper_boundary(
        self,
        execution_engine: Engine,
        time_entry_service: TimeEntryService,
    ) -> None:
        """``Decimal("24.01")`` sits one tick above the upper bound
        and raises with ``failed_constraint='effort_hours_too_large'``.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        _seed_required_parties(execution_engine)

        with execution_engine.begin() as conn:
            with pytest.raises(TimeEntryValidationError) as exc_info:
                time_entry_service.create_time_entry(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    effort_hours=Decimal("24.01"),
                    effort_period_start=_PERIOD_START_DT,
                    effort_period_end=_PERIOD_END_DT,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == "effort_hours_too_large"
        assert _count(execution_engine, "Time_Entry_Records") == 0

    def test_negative_zero_point_zero_one_is_rejected_below_lower_boundary(
        self,
        execution_engine: Engine,
        time_entry_service: TimeEntryService,
    ) -> None:
        """``Decimal("-0.01")`` sits one tick below the lower bound
        and raises with ``failed_constraint='effort_hours_negative'``.

        The slice forbids negative reported effort per Requirement
        25.2 / 25.3; the service surfaces a distinct
        ``failed_constraint`` so the HTTP layer can render a precise
        400 response identifying the negative-value violation.
        """
        _seed_required_parties(execution_engine)

        with execution_engine.begin() as conn:
            with pytest.raises(TimeEntryValidationError) as exc_info:
                time_entry_service.create_time_entry(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    effort_hours=Decimal("-0.01"),
                    effort_period_start=_PERIOD_START_DT,
                    effort_period_end=_PERIOD_END_DT,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == "effort_hours_negative"
        assert _count(execution_engine, "Time_Entry_Records") == 0

    def test_three_fractional_digits_are_rejected(
        self,
        execution_engine: Engine,
        time_entry_service: TimeEntryService,
    ) -> None:
        """A value with three fractional digits (e.g.
        ``Decimal("0.123")``) violates the ISO-decimal regex
        ``^(0|[1-9][0-9]?)(\\.[0-9]{1,2})?$`` and raises with
        ``failed_constraint='effort_hours_format'``.

        The check inspects the Decimal's ``as_tuple().exponent``; for
        ``Decimal("0.123")`` the exponent is ``-3`` (three fractional
        digits), which falls outside the at-most-two-fractional-digits
        admissible set per Requirement 25.2.
        """
        _seed_required_parties(execution_engine)

        with execution_engine.begin() as conn:
            with pytest.raises(TimeEntryValidationError) as exc_info:
                time_entry_service.create_time_entry(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    effort_hours=Decimal("0.123"),
                    effort_period_start=_PERIOD_START_DT,
                    effort_period_end=_PERIOD_END_DT,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == "effort_hours_format"
        assert _count(execution_engine, "Time_Entry_Records") == 0


# ===========================================================================
# Requirement 25.3 — effort-period ordering.
# ===========================================================================


class TestEffortPeriodOrdering:
    """Per Requirement 25.3 / design §"Execution_Service.TimeEntries":
    ``effort_period_start <= effort_period_end <= recorded_at``.

    The two ordering checks fire at different stages of the validator:

    - ``effort_period_start <= effort_period_end`` is checked in the
      static :meth:`TimeEntryService._validate_effort_period` validator
      before any clock or database read, surfacing as
      ``failed_constraint='effort_period_start_after_end'``.
    - ``effort_period_end <= recorded_at`` is checked inside
      :meth:`TimeEntryService.create_time_entry` once the clock is
      consulted, surfacing as
      ``failed_constraint='effort_period_end_after_recorded_at'``.

    Both rejections fire before any row is written so the schema-level
    CHECK constraints are never reached (they remain a defense-in-depth
    layer covered separately in
    ``tests/unit/test_execution_persistence.py``).
    """

    def test_period_start_after_period_end_is_rejected(
        self,
        execution_engine: Engine,
        time_entry_service: TimeEntryService,
    ) -> None:
        """``effort_period_start > effort_period_end`` raises with
        ``failed_constraint='effort_period_start_after_end'``.

        The rejection runs before any database read so no row is
        persisted; the comparison runs on the ISO-8601 millisecond
        forms because lexicographic ``<=`` on those strings matches
        chronological ordering.
        """
        _seed_required_parties(execution_engine)
        period_start = _PERIOD_END_DT  # later
        period_end = _PERIOD_START_DT  # earlier

        with execution_engine.begin() as conn:
            with pytest.raises(TimeEntryValidationError) as exc_info:
                time_entry_service.create_time_entry(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    effort_hours=Decimal("1.00"),
                    effort_period_start=period_start,
                    effort_period_end=period_end,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == (
            "effort_period_start_after_end"
        )
        assert _count(execution_engine, "Time_Entry_Records") == 0

    def test_period_end_after_recorded_at_is_rejected(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        time_entry_service: TimeEntryService,
    ) -> None:
        """``effort_period_end > recorded_at`` raises with
        ``failed_constraint='effort_period_end_after_recorded_at'``.

        The per-test ``clock`` fixture is pinned to
        ``2026-01-01T00:00:00.000Z``; a ``period_end`` one hour in
        the future of that instant exceeds the recorded time and the
        validator rejects the request. The happy-path deps are seeded
        so the request reaches the recorded-time comparison (i.e. the
        Work Assignment resolves and authorization permits) — only the
        period-end ordering invariant fires.

        Per Requirement 25.5 / 25.6, no consequential audit row, no
        Denial Record, and no ``Time_Entry_Records`` row is written
        on a validation rejection; the rejection is purely a
        client-error response and does not invoke the denial-audit
        pathway.
        """
        _seed_happy_path(execution_engine, authorization_service)
        period_end_future = _RECORDED_AT_DT + timedelta(hours=1)

        with execution_engine.begin() as conn:
            with pytest.raises(TimeEntryValidationError) as exc_info:
                time_entry_service.create_time_entry(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    effort_hours=Decimal("1.00"),
                    effort_period_start=_PERIOD_START_DT,
                    effort_period_end=period_end_future,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == (
            "effort_period_end_after_recorded_at"
        )
        assert _count(execution_engine, "Time_Entry_Records") == 0
        assert _count_consequential_audit_rows(
            execution_engine, "create.time_entry"
        ) == 0


# ===========================================================================
# Requirement 32.7 / AD-WS-29 — assignee-binding rejection.
# ===========================================================================


def test_ad_ws_29_assignee_binding_mismatch_rejects_with_one_denial_record(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
    time_entry_service: TimeEntryService,
) -> None:
    """Per AD-WS-29 / Requirement 32.7, the recording Party must be
    the named assignee on the originating Work Assignment Record.

    Even when authorization permits the ``create.time_entry`` action
    (the recording Party holds the ``contribute`` authority over the
    relevant scope), the request is rejected unless the persisted
    ``Work_Assignment_Records.assignee_party_id`` matches
    ``recording_party_id``. The rejection surfaces as
    :class:`TimeEntryAssigneeBindingError` with
    ``reason_code = 'no-role-assignment'`` (Slice 1 Requirement 7.2's
    denial enumeration) and persists exactly one Denial Record in a
    separate transaction; the caller's transaction rolls back so no
    ``Time_Entry_Records`` row, no ``Relationships`` row, and no
    consequential audit row is persisted.

    The test seeds a Work Assignment whose ``assignee_party_id`` is a
    Party *other* than the recording Party; the recording Party
    independently holds the ``contribute`` authority on the relevant
    scope so the AD-WS-9 first stage permits, allowing the AD-WS-29
    second stage to be the deciding gate.
    """
    _seed_required_parties(execution_engine)
    # Grant the recording Party the ``contribute`` authority over the
    # relevant scope so AD-WS-9 / authorization permits the action.
    # The AD-WS-29 second stage is then the only gate left.
    _assign_contribute_role(
        authorization_service,
        execution_engine,
        party_id=_RECORDING_PARTY_ID,
    )
    # Seed the Work Assignment with a *different* Party as the named
    # assignee so the AD-WS-29 second stage fails.
    _seed_work_assignment(
        execution_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_OTHER_PARTY_ID,
    )

    correlation = "corr-time-entry-ad-ws-29"
    with pytest.raises(TimeEntryAssigneeBindingError) as exc_info:
        with execution_engine.begin() as conn:
            time_entry_service.create_time_entry(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                effort_hours=Decimal("1.00"),
                effort_period_start=_PERIOD_START_DT,
                effort_period_end=_PERIOD_END_DT,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
                correlation_id=correlation,
            )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation
    assert exc_info.value.target_work_assignment_id == (
        _BOUND_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.recording_party_id == _RECORDING_PARTY_ID
    assert exc_info.value.actual_assignee_party_id == _OTHER_PARTY_ID

    # Caller's transaction rolled back: no Time Entry / Relationship /
    # consequential audit row persisted.
    assert _count(execution_engine, "Time_Entry_Records") == 0
    assert _count_consequential_audit_rows(
        execution_engine, "create.time_entry"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (AD-WS-9 / Requirement 30.6).
    assert _count_denial_audit_rows(
        execution_engine, "create.time_entry"
    ) == 1


# ===========================================================================
# Requirement 25.4 — authorization deny path.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    execution_engine: Engine,
    time_entry_service: TimeEntryService,
) -> None:
    """A denied request appends exactly one Denial Record in a
    separate transaction and raises
    :class:`TimeEntryAuthorizationError`.

    Requirement 25.4 / AD-WS-9: the authorization deny path uses the
    Slice 1 separate-transaction Denial-Record pattern. The caller's
    transaction rolls back so no ``Time_Entry_Records`` row, no
    ``Relationships`` row, and no consequential audit row is
    persisted; exactly one denial row (``outcome='deny'`` with
    ``authorities_required IS NULL``) survives in its own transaction.

    Crucially, no Role Assignment is seeded so the evaluator returns
    ``deny('no-role-assignment')`` — this is the same denial pathway
    a Party with no Contributor authority would encounter at runtime.
    The Work Assignment is seeded with the recording Party as its
    assignee so the AD-WS-29 second stage would have permitted; the
    rejection therefore unambiguously comes from the AD-WS-9 first
    stage, not the AD-WS-29 second stage.
    """
    _seed_required_parties(execution_engine)
    # NB: no role assignment is seeded — the authorization evaluator
    # returns deny.
    _seed_work_assignment(
        execution_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_RECORDING_PARTY_ID,
    )

    correlation = "corr-time-entry-deny"
    with pytest.raises(TimeEntryAuthorizationError) as exc_info:
        with execution_engine.begin() as conn:
            time_entry_service.create_time_entry(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                effort_hours=Decimal("1.00"),
                effort_period_start=_PERIOD_START_DT,
                effort_period_end=_PERIOD_END_DT,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
                correlation_id=correlation,
            )

    # The wider :class:`TimeEntryAuthorizationError` is the base type;
    # the AD-WS-9 deny path is *not* the AD-WS-29 assignee-binding
    # subclass.
    assert not isinstance(exc_info.value, TimeEntryAssigneeBindingError)
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation

    # Caller's transaction rolled back: no Time Entry / consequential
    # audit row was persisted.
    assert _count(execution_engine, "Time_Entry_Records") == 0
    assert _count_consequential_audit_rows(
        execution_engine, "create.time_entry"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (Requirement 25.4 / AD-WS-9).
    assert _count_denial_audit_rows(
        execution_engine, "create.time_entry"
    ) == 1
