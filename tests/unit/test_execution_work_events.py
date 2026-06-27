"""Unit tests for :mod:`walking_slice.execution.work_events` (task 6.2).

Pins the contract established in task 6.1, design
§"Execution_Service.WorkEvents" and §"Event-kind state machine",
AD-WS-9 (separate-transaction Denial Record), AD-WS-24
(``create.work_event`` → ``contribute``), AD-WS-26
(Relationship-Type / semantic-role table), AD-WS-27 (append-only
Slice 3 tables), AD-WS-29 (two-stage Contributor authority
evaluation), and Requirements 24.3, 24.4, 24.5, 24.6, 32.7:

- **24.3 — Event-kind state machine.** Per Work Assignment the legal
  sequence of event kinds is bounded:

    * a ``started`` event is rejected when any prior ``started``
      exists (state-machine catch via
      :class:`WorkEventStartedAlreadyExistsError`; the partial
      UNIQUE index ``idx_work_events_one_started_per_wa`` is the
      database-layer safety net for the concurrent-writer race);
    * ``progress_note`` / ``paused`` / ``resumed`` /
      ``deliverable_drafted`` are rejected when no prior ``started``
      exists on the same Work Assignment
      (:class:`WorkEventNoPriorStartedError`);
    * ``resumed`` is rejected when no prior ``paused`` exists or
      when the most recent prior event in ``{paused, resumed}`` is
      not ``paused``
      (:class:`WorkEventResumeRequiresPausedError`).

  The headline positive sequence
  ``started → paused → resumed → paused → resumed`` is exercised
  end-to-end so each transition is observably accepted in turn.

- **24.4 — structural rejection.** State-machine violations leave
  ``Work_Event_Records``, ``Relationships``, and ``Audit_Records``
  in their pre-call state — no Work Event Record is created and no
  Denial Record is appended (state-machine rejections are pure
  validation rejections, distinct from the AD-WS-9 authorization
  deny path).

- **24.5 / AD-WS-29 — assignee-binding rejection.** Even when
  authorization permits the ``create.work_event`` action (the
  recording Party holds the ``contribute`` authority over the
  relevant scope), the request is rejected unless the persisted
  ``Work_Assignment_Records.assignee_party_id`` matches the
  supplied ``recording_party_id``. The rejection surfaces as
  :class:`WorkEventAssigneeBindingError` (a subclass of
  :class:`WorkEventAuthorizationError`) with
  ``reason_code = 'no-role-assignment'`` and persists exactly one
  Denial Record in a separate transaction; the caller's
  transaction rolls back so no Work Event Record / Relationship /
  consequential audit row is persisted.

- **24.5 / AD-WS-9 — authorization deny path.** A denied request
  (Party holds no ``contribute`` authority on the relevant scope)
  appends exactly one Denial Record in a separate transaction and
  raises :class:`WorkEventAuthorizationError`; no Work Event Record /
  Relationship / consequential audit row is persisted.

- **24.6 — consequential audit row.** Pinned indirectly through the
  happy-path baseline (exactly one consequential audit row
  appended in the same transaction as the Work Event Record).

- **32.7 — ``create.work_event`` requires ``contribute``.** The
  AD-WS-24 mapping is exercised through the happy and deny paths;
  the non-substitution invariant on :class:`AuthorizationService`
  is preserved.

The tests mirror the style of
``tests/unit/test_execution_work_assignments.py`` (task 5.2) and
``tests/unit/test_deliverables_repository.py`` (task 4.3): a
per-test engine carrying the Slice 1 + Slice 3 Execution schemas, a
real :class:`AuthorizationService` driven through a seeded role
assignment on happy paths, direct INSERTs to seed the Work
Assignment Record fixture, and counter helpers that confirm
nothing was persisted on negative paths. State-machine sequences
that record multiple Work Events per Work Assignment use an
advancing clock so successive ``recorded_at`` values stay strictly
monotonic, keeping the ``recorded_at DESC`` ordering used by the
state-machine query deterministic.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import Clock
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.work_events import (
    CreateWorkEventResult,
    WorkEventAssigneeBindingError,
    WorkEventAuthorizationError,
    WorkEventNoPriorStartedError,
    WorkEventResumeRequiresPausedError,
    WorkEventService,
    WorkEventStartedAlreadyExistsError,
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
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_FIXED_NOW = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ---------------------------------------------------------------------------
# Test-local clock that advances by a fixed step on every read.
#
# Multiple Work Events recorded against the same Work Assignment in
# one test must each carry a distinct ``recorded_at`` so the
# state-machine query (``ORDER BY recorded_at DESC``) returns a
# deterministic chronology. The conftest-provided
# :class:`FixedClock` ties every read to a single instant, which is
# fine for single-event tests but ambiguous for the multi-event
# state-machine sequences below. ``_AdvancingClock`` is a Protocol-
# compliant :class:`Clock` whose ``now()`` returns
# ``start + step * n_calls``; one millisecond per call is the
# coarsest step that still fits in the audit-storage millisecond
# precision contract from design §"Cross-Cutting Concerns".
# ---------------------------------------------------------------------------


class _AdvancingClock:
    """Test-only :class:`Clock` that advances by ``step`` on each read.

    Constructed with a starting instant and a step. Each call to
    :meth:`now` returns ``start + step * n_calls_so_far`` and
    increments the call count. Two consecutive reads therefore
    differ by exactly ``step``.

    The class satisfies the :class:`walking_slice.clock.Clock`
    Protocol (a single :meth:`now` method returning a UTC
    millisecond-precision :class:`datetime`).
    """

    def __init__(
        self,
        *,
        start: datetime = _FIXED_NOW,
        step: timedelta = timedelta(milliseconds=1),
    ) -> None:
        self._start = start
        self._step = step
        self._calls = 0

    def now(self) -> datetime:
        result = self._start + self._step * self._calls
        self._calls += 1
        return result


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
    ``create_execution_schema`` installs the Slice 3
    Execution_Service tables including ``Work_Assignment_Records``
    and ``Work_Event_Records`` with their AD-WS-27 append-only
    triggers, the Requirement 24.2 / 24.3 CHECK constraints, and the
    partial UNIQUE index ``idx_work_events_one_started_per_wa``.

    The Slice 2 Planning schema and the Deliverable_Repository
    schema are intentionally not installed: the Work Event Service
    resolves only the originating Work Assignment Record by primary
    key, so a smaller surface keeps the fixture minimal.
    """
    create_schema(engine)
    create_execution_schema(engine)
    return engine


@pytest.fixture
def work_event_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> WorkEventService:
    """:class:`WorkEventService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test. The denial-audit
    sleep is replaced with a no-op so the deny-path retries do not
    spend real time.
    """
    return WorkEventService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )


def _build_advancing_service(
    *,
    engine: Engine,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> WorkEventService:
    """Build a :class:`WorkEventService` wired with an
    :class:`_AdvancingClock` so successive Work Events carry
    monotonically increasing ``recorded_at`` values.

    Used by the multi-event state-machine sequences; tests that
    record a single Work Event use the conftest-provided
    :class:`FixedClock` via :func:`work_event_service`.
    """
    return WorkEventService(
        clock=_AdvancingClock(),
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
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

    The AD-WS-27 UPDATE/DELETE rejection triggers only fire on
    UPDATE and DELETE, so an INSERT may proceed in one statement
    without driving the full :class:`WorkAssignmentService`. The
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

    Per AD-WS-24, ``create.work_event`` maps to the ``contribute``
    authority. A Party with an effective Role Assignment carrying
    ``contribute`` over ``scope`` plus the AD-WS-29 assignee
    binding may record Work Events.
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
    :meth:`WorkEventService.create_work_event` call.

    The default ``assignee_party_id`` matches the recording Party
    so AD-WS-29's second stage passes; tests for the assignee-
    binding mismatch override this argument with a different Party.
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


def _count_work_events(
    engine: Engine, *, work_assignment_id: str = _BOUND_WORK_ASSIGNMENT_ID
) -> int:
    """Count ``Work_Event_Records`` rows for ``work_assignment_id``."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Work_Event_Records "
                    "WHERE target_work_assignment_id = :wid"
                ),
                {"wid": work_assignment_id},
            ).scalar_one()
        )


def _count_denial_audit_rows(engine: Engine, action_type: str) -> int:
    """Count Denial Record rows for ``action_type``.

    A Denial Record is distinguished from the authorization
    evaluation row (which also carries ``outcome='deny'``) by
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
# Happy-path baseline — confirms wiring and pins the result-object surface
# for a single ``started`` event (Requirement 24.1 / 24.2 / 24.6).
# ===========================================================================


def test_single_started_event_permits_and_records_one_relationship(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
    work_event_service: WorkEventService,
) -> None:
    """A single ``started`` event against a Work Assignment whose
    named assignee matches the recording Party is permitted and
    records exactly one Work Event Record, one ``Relates To``
    Relationship carrying ``semantic_role='work_event'`` (AD-WS-26
    row 3), and one consequential audit row inside one transaction
    (Requirement 24.6).

    The fixed :class:`FixedClock` is sufficient here because only
    one event is recorded; later tests that record multiple events
    in a single Work Assignment use an :class:`_AdvancingClock`.
    """
    _seed_happy_path(execution_engine, authorization_service)

    with execution_engine.begin() as conn:
        result = work_event_service.create_work_event(
            conn,
            target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
            event_kind="started",
            event_note=None,
            recording_party_id=_RECORDING_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=execution_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateWorkEventResult)
    assert _CANONICAL_UUID7.match(result.work_event_id)
    assert _CANONICAL_UUID7.match(result.relates_to_relationship_id)
    assert result.target_work_assignment_id == _BOUND_WORK_ASSIGNMENT_ID
    assert result.event_kind == "started"
    assert result.event_note is None
    assert result.recording_party_id == _RECORDING_PARTY_ID
    assert result.applicable_scope == _SCOPE
    assert result.correlation_id == "corr-permit"

    assert _count_work_events(execution_engine) == 1

    # The single Relationship row is a ``Relates To`` carrying
    # ``semantic_role='work_event'`` per AD-WS-26 row 3.
    with execution_engine.connect() as conn:
        relationship_row = conn.execute(
            text(
                """
                SELECT source_kind, source_id, source_revision_id,
                       target_kind, target_id, target_revision_id,
                       relationship_type, semantic_role
                FROM Relationships
                WHERE source_id = :sid
                """
            ),
            {"sid": result.work_event_id},
        ).mappings().one()
    assert relationship_row["relationship_type"] == "Relates To"
    assert relationship_row["source_kind"] == "work_event_record"
    assert relationship_row["source_id"] == result.work_event_id
    assert relationship_row["source_revision_id"] is None
    assert relationship_row["target_kind"] == "work_assignment_record"
    assert relationship_row["target_id"] == _BOUND_WORK_ASSIGNMENT_ID
    assert relationship_row["target_revision_id"] is None
    assert relationship_row["semantic_role"] == "work_event"

    # Exactly one consequential audit row (Requirement 24.6).
    assert _count_consequential_audit_rows(
        execution_engine, "create.work_event"
    ) == 1


# ===========================================================================
# Requirement 24.3 — Event-kind state machine.
# ===========================================================================


class TestStateMachineHappyPath:
    """Per Requirement 24.3 and design §"Event-kind state machine"
    the legal positive sequence
    ``started → paused → resumed → paused → resumed`` is accepted
    end-to-end.
    """

    def test_full_pause_resume_cycle_accepted(
        self,
        execution_engine: Engine,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
    ) -> None:
        """The sequence ``started → paused → resumed → paused →
        resumed`` succeeds for the same Work Assignment.

        Each event lands in ``Work_Event_Records`` and carries a
        strictly later ``recorded_at`` than the previous one (driven
        by the :class:`_AdvancingClock`). The final Work Assignment
        carries exactly five Work Event Records, one ``Relates To``
        Relationship per event, and one consequential audit row per
        event.
        """
        _seed_happy_path(execution_engine, authorization_service)
        service = _build_advancing_service(
            engine=execution_engine,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        )

        sequence = ("started", "paused", "resumed", "paused", "resumed")
        recorded_at_values: list[str] = []
        for index, kind in enumerate(sequence):
            with execution_engine.begin() as conn:
                result = service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind=kind,
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                    correlation_id=f"corr-cycle-{index}",
                )
            assert result.event_kind == kind
            recorded_at_values.append(result.recorded_at)

        # Every event was persisted.
        assert _count_work_events(execution_engine) == 5
        assert _count_consequential_audit_rows(
            execution_engine, "create.work_event"
        ) == 5

        # The advancing clock yielded strictly monotonic timestamps,
        # so the state-machine query's ``ORDER BY recorded_at DESC``
        # has a deterministic chronology to work with.
        assert recorded_at_values == sorted(recorded_at_values)
        assert len(set(recorded_at_values)) == 5

        # The persisted event_kinds match the requested sequence.
        with execution_engine.connect() as conn:
            persisted_sequence = tuple(
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT event_kind FROM Work_Event_Records "
                        "WHERE target_work_assignment_id = :wid "
                        "ORDER BY recorded_at ASC"
                    ),
                    {"wid": _BOUND_WORK_ASSIGNMENT_ID},
                ).all()
            )
        assert persisted_sequence == sequence


class TestStateMachineSecondStartedRejected:
    """Per Requirement 24.3 a second ``started`` event on the same
    Work Assignment is rejected.

    The application-level state-machine check is the first line of
    defense; the partial UNIQUE index
    ``idx_work_events_one_started_per_wa`` is the database-layer
    safety net for the concurrent-writer race (covered separately
    by :class:`TestConcurrentStartedPartialUnique`).
    """

    def test_second_started_rejected_by_state_machine(
        self,
        execution_engine: Engine,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
    ) -> None:
        """The second ``started`` attempt raises
        :class:`WorkEventStartedAlreadyExistsError` and persists
        nothing.

        Requirement 24.3 / 24.4: state-machine rejections are pure
        validation rejections, so no Denial Record is appended.
        The first ``started`` event remains intact; only the second
        attempt is rolled back.
        """
        _seed_happy_path(execution_engine, authorization_service)
        service = _build_advancing_service(
            engine=execution_engine,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        )

        # First started succeeds.
        with execution_engine.begin() as conn:
            service.create_work_event(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                event_kind="started",
                event_note=None,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        # Second started is rejected by the application-level state
        # machine.
        with pytest.raises(WorkEventStartedAlreadyExistsError) as exc_info:
            with execution_engine.begin() as conn:
                service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind="started",
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.target_work_assignment_id == (
            _BOUND_WORK_ASSIGNMENT_ID
        )
        assert exc_info.value.failed_constraint == "started_already_exists"

        # The first event is intact; nothing further was persisted.
        assert _count_work_events(execution_engine) == 1
        assert _count_consequential_audit_rows(
            execution_engine, "create.work_event"
        ) == 1

        # State-machine rejections do NOT append Denial Records.
        assert _count_denial_audit_rows(
            execution_engine, "create.work_event"
        ) == 0


class TestStateMachineNoPriorStarted:
    """Per Requirement 24.3 / 24.4 every non-``started`` event kind
    requires a prior ``started`` Work Event on the same Work
    Assignment.
    """

    def test_progress_note_before_started_rejected(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_event_service: WorkEventService,
    ) -> None:
        """A ``progress_note`` event submitted before any
        ``started`` event raises
        :class:`WorkEventNoPriorStartedError`; no row is persisted
        and no Denial Record is appended.
        """
        _seed_happy_path(execution_engine, authorization_service)

        with pytest.raises(WorkEventNoPriorStartedError) as exc_info:
            with execution_engine.begin() as conn:
                work_event_service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind="progress_note",
                    event_note="early progress note",
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.event_kind == "progress_note"
        assert exc_info.value.target_work_assignment_id == (
            _BOUND_WORK_ASSIGNMENT_ID
        )
        assert exc_info.value.failed_constraint == "no_prior_started_event"

        assert _count_work_events(execution_engine) == 0
        assert _count_consequential_audit_rows(
            execution_engine, "create.work_event"
        ) == 0
        assert _count_denial_audit_rows(
            execution_engine, "create.work_event"
        ) == 0

    def test_deliverable_drafted_before_started_rejected(
        self,
        execution_engine: Engine,
        authorization_service: AuthorizationService,
        work_event_service: WorkEventService,
    ) -> None:
        """A ``deliverable_drafted`` event submitted before any
        ``started`` event raises
        :class:`WorkEventNoPriorStartedError`; no row is persisted
        and no Denial Record is appended.

        Symmetric to the ``progress_note`` case but pins the same
        rejection for the ``deliverable_drafted`` kind so the
        no-prior-started branch is exercised over both
        non-pause-cycle kinds (a regression that special-cased one
        kind would not be caught by the other).
        """
        _seed_happy_path(execution_engine, authorization_service)

        with pytest.raises(WorkEventNoPriorStartedError) as exc_info:
            with execution_engine.begin() as conn:
                work_event_service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind="deliverable_drafted",
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.event_kind == "deliverable_drafted"
        assert exc_info.value.failed_constraint == "no_prior_started_event"

        assert _count_work_events(execution_engine) == 0
        assert _count_denial_audit_rows(
            execution_engine, "create.work_event"
        ) == 0


class TestStateMachineResumeRequiresPaused:
    """Per Requirement 24.3 a ``resumed`` event is rejected when the
    most recent prior event in ``{paused, resumed}`` is not
    ``paused``.
    """

    def test_resumed_after_started_without_paused_rejected(
        self,
        execution_engine: Engine,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
    ) -> None:
        """``resumed`` directly after ``started`` is rejected: the
        most recent prior event in ``{paused, resumed}`` is ``None``
        (no such event exists yet), which Requirement 24.3 treats
        as a missing pause.
        """
        _seed_happy_path(execution_engine, authorization_service)
        service = _build_advancing_service(
            engine=execution_engine,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        )

        # Seed the prior ``started`` so the no-prior-started branch
        # does not fire — the only state-machine rejection left is
        # the resume-requires-paused branch.
        with execution_engine.begin() as conn:
            service.create_work_event(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                event_kind="started",
                event_note=None,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
            )

        with pytest.raises(WorkEventResumeRequiresPausedError) as exc_info:
            with execution_engine.begin() as conn:
                service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind="resumed",
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == "resume_requires_paused"
        assert exc_info.value.most_recent_pause_cycle_kind is None

        # Only the ``started`` event was persisted; no Denial
        # Record was appended (state-machine rejections are pure
        # validation rejections).
        assert _count_work_events(execution_engine) == 1
        assert _count_denial_audit_rows(
            execution_engine, "create.work_event"
        ) == 0

    def test_resumed_after_already_resumed_rejected(
        self,
        execution_engine: Engine,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
    ) -> None:
        """Two ``resumed`` events back-to-back are rejected — the
        second ``resumed`` has no fresh ``paused`` to resume.

        Sequence: ``started → paused → resumed → resumed``. The
        second ``resumed`` finds ``resumed`` (not ``paused``) as
        the most recent prior event in ``{paused, resumed}``, so
        Requirement 24.3 rejects it.
        """
        _seed_happy_path(execution_engine, authorization_service)
        service = _build_advancing_service(
            engine=execution_engine,
            identity_service=identity_service,
            audit_log=audit_log,
            authorization_service=authorization_service,
        )

        for kind in ("started", "paused", "resumed"):
            with execution_engine.begin() as conn:
                service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind=kind,
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        # Now attempt a second ``resumed`` without an intervening
        # ``paused``.
        with pytest.raises(WorkEventResumeRequiresPausedError) as exc_info:
            with execution_engine.begin() as conn:
                service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind="resumed",
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        assert exc_info.value.failed_constraint == "resume_requires_paused"
        assert exc_info.value.most_recent_pause_cycle_kind == "resumed"

        # Three events persisted from the priming sequence; the
        # rejected fourth attempt persisted nothing.
        assert _count_work_events(execution_engine) == 3
        assert _count_denial_audit_rows(
            execution_engine, "create.work_event"
        ) == 0


# ===========================================================================
# Requirement 24.3 — partial UNIQUE index safety net for the
# concurrent ``started`` race.
# ===========================================================================


class _RacingIdentityService:
    """Test double that races a concurrent ``started`` Work Event
    into the database between the state-machine check and the
    caller's INSERT.

    The Work Event Service's flow is (per task 6.1):

      1. validate inputs
      2. resolve Work Assignment
      3. authorization evaluation (separate transaction)
      4. AD-WS-29 assignee binding
      5. state-machine check (covering query against
         ``Work_Event_Records`` for the same Work Assignment)
      6. mint Work Event identifier (this stub's hook point)
      7. mint Relationship identifier
      8. ``_record_execution_artifact`` (INSERT
         ``Identifier_Registry``)
      9. INSERT ``Work_Event_Records``
      10. INSERT ``Relationships``
      11. append consequential audit row

    By inserting a ``started`` row in its *own* committed
    transaction during step 6, the stub recreates the worst-case
    race: the application-level state-machine check (step 5)
    observed an empty history, but by the time the caller's
    transaction reaches step 9 a second writer has committed a
    ``started`` row, so the partial UNIQUE index
    ``idx_work_events_one_started_per_wa`` (Requirement 24.3 /
    design §"Indexes") must catch the duplicate. The expected
    surface is an :class:`sqlalchemy.exc.IntegrityError` raised at
    step 9; the caller's surrounding ``engine.begin()`` rolls back
    the registry and any partial state.

    Delegates every other :class:`IdentityService` method to the
    underlying real service so identifier issuance, registry
    confirmation, and conflict reporting continue to behave
    exactly as in production.
    """

    def __init__(
        self,
        *,
        wrapped: IdentityService,
        engine: Engine,
        racing_work_assignment_id: str,
    ) -> None:
        self._wrapped = wrapped
        self._engine = engine
        self._racing_work_assignment_id = racing_work_assignment_id
        self._raced = False

    def new_immutable_record_id(self) -> object:
        """Race a concurrent committed ``started`` then delegate."""
        if not self._raced:
            self._raced = True
            # Race: commit a ``started`` row in a separate
            # transaction. The recorded_at is intentionally one
            # second past the caller's :class:`FixedClock` so the
            # row is observable to subsequent state-machine
            # queries.
            racing_id = str(self._wrapped.new_immutable_record_id())
            racing_at = format_iso8601_ms(
                _FIXED_NOW + timedelta(seconds=1)
            )
            with self._engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO Work_Event_Records (
                            work_event_id, target_work_assignment_id,
                            event_kind, event_note,
                            recording_party_id,
                            authority_basis_type, authority_basis_id,
                            applicable_scope, recorded_at
                        ) VALUES (
                            :wid, :waid, 'started', NULL,
                            :rid,
                            'role-grant-id', :abid,
                            :scope, :ts
                        )
                        """
                    ),
                    {
                        "wid": racing_id,
                        "waid": self._racing_work_assignment_id,
                        "rid": _RECORDING_PARTY_ID,
                        "abid": str(_AUTHORITY_BASIS_ID),
                        "scope": _SCOPE,
                        "ts": racing_at,
                    },
                )
        return self._wrapped.new_immutable_record_id()

    def __getattr__(self, name: str) -> object:
        # Delegate every other attribute lookup to the wrapped
        # service so identifier issuance, registry confirmation,
        # validation, and conflict reporting continue to behave
        # exactly as in production.
        return getattr(self._wrapped, name)


class TestConcurrentStartedPartialUnique:
    """Per Requirement 24.3 / design §"Indexes", the partial UNIQUE
    index ``idx_work_events_one_started_per_wa`` is the database-
    layer safety net for the concurrent ``started`` race.

    Even when two transactions both pass the application-level
    state-machine check (each one's covering query sees an empty
    history because the other has not yet committed), the partial
    UNIQUE index rejects the second INSERT at the database layer,
    producing :class:`sqlalchemy.exc.IntegrityError` and rolling
    back the caller's transaction so no Work Event Record / no
    Relationship / no consequential audit row from the loser is
    persisted.
    """

    def test_concurrent_started_loses_to_partial_unique(
        self,
        execution_engine: Engine,
        clock: Clock,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
    ) -> None:
        """The partial UNIQUE index rejects the second ``started``
        even when the application-level state-machine check
        observed an empty history.

        The :class:`_RacingIdentityService` commits a concurrent
        ``started`` *after* the caller's state-machine check has
        passed but *before* the caller's
        ``Work_Event_Records`` INSERT runs, recreating the worst-
        case race. The expected surface is
        :class:`sqlalchemy.exc.IntegrityError`; the caller's
        ``engine.begin()`` context manager rolls back so neither
        the registry row, the (non-existent) Relationship row, nor
        the consequential audit row is persisted. The "winning"
        ``started`` (committed by the racing stub in its own
        transaction) survives.
        """
        _seed_happy_path(execution_engine, authorization_service)

        racing_ids = _RacingIdentityService(
            wrapped=identity_service,
            engine=execution_engine,
            racing_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        )
        service = WorkEventService(
            clock=clock,
            identity_service=racing_ids,  # type: ignore[arg-type]
            audit_log=audit_log,
            authorization_service=authorization_service,
            denial_audit_sleep=lambda _seconds: None,
        )

        with pytest.raises(IntegrityError):
            with execution_engine.begin() as conn:
                service.create_work_event(
                    conn,
                    target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    event_kind="started",
                    event_note=None,
                    recording_party_id=_RECORDING_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=execution_engine,
                )

        # Only the racing ``started`` survives. The caller's
        # transaction was rolled back so no consequential audit row
        # from the losing attempt is observable.
        assert _count_work_events(execution_engine) == 1
        assert _count_consequential_audit_rows(
            execution_engine, "create.work_event"
        ) == 0


# ===========================================================================
# Requirement 24.5 / AD-WS-29 — assignee-binding rejection.
# ===========================================================================


def test_assignee_binding_rejection_persists_one_denial_record(
    execution_engine: Engine,
    authorization_service: AuthorizationService,
    work_event_service: WorkEventService,
) -> None:
    """Per AD-WS-29 / Requirement 24.5 / 32.7, the recording Party
    must be the named assignee on the target Work Assignment.

    Even when authorization permits the ``create.work_event``
    action (the recording Party holds the ``contribute`` authority
    over the relevant scope), the request is rejected unless the
    persisted ``Work_Assignment_Records.assignee_party_id`` matches
    ``recording_party_id``. The rejection surfaces as
    :class:`WorkEventAssigneeBindingError` (a subclass of
    :class:`WorkEventAuthorizationError`) with
    ``reason_code = 'no-role-assignment'`` (Slice 1 Requirement
    7.2's denial enumeration) and persists exactly one Denial
    Record in a separate transaction; the caller's transaction
    rolls back so no Work Event Record / Relationship /
    consequential audit row is persisted.

    The test seeds a Work Assignment whose ``assignee_party_id``
    is a Party *other* than the recording Party; the recording
    Party independently holds the ``contribute`` authority on the
    relevant scope so the AD-WS-9 first stage permits, leaving
    the AD-WS-29 second stage as the only gate.
    """
    _seed_required_parties(execution_engine)
    # Grant the recording Party the ``contribute`` authority over
    # the relevant scope so the AD-WS-9 first stage permits the
    # action; the AD-WS-29 second stage is then the only gate.
    _assign_contribute_role(
        authorization_service,
        execution_engine,
        party_id=_RECORDING_PARTY_ID,
    )
    # Seed the Work Assignment with a *different* Party as the
    # named assignee so the AD-WS-29 second stage fails.
    _seed_work_assignment(
        execution_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_OTHER_PARTY_ID,
    )

    correlation = "corr-work-event-ad-ws-29"
    with pytest.raises(WorkEventAssigneeBindingError) as exc_info:
        with execution_engine.begin() as conn:
            work_event_service.create_work_event(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                event_kind="started",
                event_note=None,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
                correlation_id=correlation,
            )

    # AD-WS-29 reuses Slice 1 Requirement 7.2's denial enumeration.
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation
    assert exc_info.value.target_work_assignment_id == (
        _BOUND_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.recording_party_id == _RECORDING_PARTY_ID
    assert exc_info.value.actual_assignee_party_id == _OTHER_PARTY_ID

    # Caller's transaction rolled back: no Work Event Record /
    # Relationship / consequential audit row persisted.
    assert _count_work_events(execution_engine) == 0
    assert _count_consequential_audit_rows(
        execution_engine, "create.work_event"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (AD-WS-9 / Requirement 30.6).
    assert _count_denial_audit_rows(
        execution_engine, "create.work_event"
    ) == 1


# ===========================================================================
# Requirement 24.5 / AD-WS-9 — authorization deny path.
# ===========================================================================


def test_authorization_deny_appends_exactly_one_denial_record(
    execution_engine: Engine,
    work_event_service: WorkEventService,
) -> None:
    """A denied request appends exactly one Denial Record in a
    separate transaction and raises
    :class:`WorkEventAuthorizationError`.

    Requirement 24.5 / AD-WS-9: the authorization deny path uses
    the Slice 1 separate-transaction Denial-Record pattern. The
    caller's transaction rolls back so no Work Event Record /
    Relationship / consequential audit row is persisted; exactly
    one denial row (``outcome='deny'`` with
    ``authorities_required IS NULL``) survives in its own
    transaction.

    Crucially, no Role Assignment is seeded so the evaluator
    returns ``deny('no-role-assignment')`` — this is the same
    denial pathway a Party with no Contributor authority would
    encounter at runtime. The Work Assignment is seeded with the
    recording Party as its assignee so the AD-WS-29 second stage
    would have permitted; the rejection therefore unambiguously
    comes from the AD-WS-9 first stage, not the AD-WS-29 second
    stage (asserted by checking the exception is the base
    :class:`WorkEventAuthorizationError`, not the
    :class:`WorkEventAssigneeBindingError` subclass).
    """
    _seed_required_parties(execution_engine)
    # NB: no role assignment is seeded — the authorization
    # evaluator returns deny.
    _seed_work_assignment(
        execution_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_RECORDING_PARTY_ID,
    )

    correlation = "corr-work-event-deny"
    with pytest.raises(WorkEventAuthorizationError) as exc_info:
        with execution_engine.begin() as conn:
            work_event_service.create_work_event(
                conn,
                target_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                event_kind="started",
                event_note=None,
                recording_party_id=_RECORDING_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                engine=execution_engine,
                correlation_id=correlation,
            )

    # The wider :class:`WorkEventAuthorizationError` is the base
    # type; the AD-WS-9 deny path is *not* the AD-WS-29 assignee-
    # binding subclass.
    assert not isinstance(exc_info.value, WorkEventAssigneeBindingError)
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation

    # Caller's transaction rolled back: no Work Event Record /
    # Relationship / consequential audit row was persisted.
    assert _count_work_events(execution_engine) == 0
    assert _count_consequential_audit_rows(
        execution_engine, "create.work_event"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (Requirement 24.5 / AD-WS-9).
    assert _count_denial_audit_rows(
        execution_engine, "create.work_event"
    ) == 1
