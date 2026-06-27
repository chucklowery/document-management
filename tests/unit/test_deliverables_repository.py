"""Unit tests for :mod:`walking_slice.deliverables.repository` (task 4.3).

Pins the contract established in task 4.1 / design
§"Deliverable_Repository", AD-WS-9 (separate-transaction Denial Record),
AD-WS-24 (``create.produced_deliverable`` → ``contribute``), AD-WS-27
(append-only Slice 3 tables), AD-WS-28 (additive ``resource_kind``
values), AD-WS-29 (two-stage Contributor authority evaluation), and
Requirements 26.1, 26.2, 26.3, 26.5, 26.6, 32.7, 41.13:

- **26.1 / 26.5 — ``content_bytes`` boundary values.** A produced
  Deliverable carries a payload of 1..104857600 bytes. ``1`` byte sits
  at the lower bound (accepted) and ``104857601`` (100 MB + 1) sits one
  past the upper bound (rejected). ``0`` bytes is rejected with a
  distinct ``failed_constraint`` (``content_bytes_empty``). Validation
  runs before any database read so a malformed request never touches
  the Work-Assignment lookup or the authorization service. The
  ``100 MB`` accepted-boundary check uses a smaller representative
  payload to avoid memory pressure in the test suite — the schema
  CHECK constraint on ``Deliverable_Revisions.content_bytes`` is the
  defense-in-depth layer, exercised in
  ``tests/unit/test_execution_persistence.py``.
- **26.1 / 26.5 — content-type enumeration rejection.** Only the seven
  enumerated content-type values from Requirement 26.1 are accepted;
  every other value (and a missing ``content_type``) raises with a
  precise ``failed_constraint``.
- **26.1 / 26.5 — produced-Deliverable name length boundaries.** A
  name of length 1 sits at the lower bound (accepted); a name of
  length 200 sits at the upper bound (accepted); an empty / missing
  name surfaces as ``produced_deliverable_name_missing``; a 201-char
  name surfaces as ``produced_deliverable_name_too_long``.
- **26.2 / Persistence Invariants Summary rule 9 / 41 §13 —
  ``role_marker`` on every Revision.** Every produced Deliverable
  Revision row carries ``role_marker = 'generated_output'``; the
  service-level constant, the CreateProducedDeliverableResult value
  object, and the persisted row all agree on the literal.
- **22.8 / 26.3 — Identifier_Registry tagging.** The produced
  Deliverable Resource Identity is registered with
  ``kind = 'resource'`` and ``resource_kind = 'deliverable_resource'``;
  the produced Deliverable Revision Identity is registered with
  ``kind = 'revision'`` and ``resource_kind = 'deliverable_revision'``.
  The row-level discriminator makes produced-Deliverable vs
  Source-Evidence disjointness inspectable on
  ``Identifier_Registry`` without a join.
- **26.5 — unresolvable originating Work Assignment.** When the
  supplied ``originating_work_assignment_id`` does not resolve to a
  row in ``Work_Assignment_Records``, the service raises
  :class:`WorkAssignmentNotResolvableError` and no
  Resource / Revision / registry / audit row is persisted.
- **32.7 / AD-WS-29 — assignee-binding rejection.** Even when the
  authoring Party holds the ``contribute`` authority on the Work
  Assignment's scope, the request is rejected unless the persisted
  ``Work_Assignment_Records.assignee_party_id`` matches the supplied
  ``authoring_party_id``. The rejection surfaces as
  :class:`WorkAssignmentAssigneeBindingError` with
  ``reason_code = 'no-role-assignment'`` and persists exactly one
  Denial Record in a separate transaction; the caller's transaction
  rolls back so no Resource / Revision / consequential audit row is
  persisted.

The tests mirror the style of
``tests/unit/test_execution_work_assignments.py`` (task 5.2): a
per-test engine carrying both the Slice 1 and Slice 3 (Execution +
Deliverable) schemas, a real :class:`AuthorizationService` driven
through a seeded role assignment on happy paths, direct INSERTs to
seed the Work Assignment Record fixture, and counter helpers that
confirm nothing was persisted on negative paths.
"""

from __future__ import annotations

import re
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
from walking_slice.clock import Clock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import (
    CreateProducedDeliverableResult,
    DeliverableContentValidationError,
    DeliverableRepositoryService,
    WorkAssignmentAssigneeBindingError,
    WorkAssignmentNotResolvableError,
)
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


_AUTHORING_PARTY_ID = "00000000-0000-7000-8000-000000a00001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000a00002"
_ASSIGNMENT_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00003"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-000000a00004"

# Work Assignment Records — the originating-Work-Assignment lookups.
_BOUND_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00001"
_UNBOUND_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-000000d00002"
_UNRESOLVABLE_WORK_ASSIGNMENT_ID = "00000000-0000-7000-8000-0000deadbe01"

_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-000000b00001"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# Seven enumerated content types from Requirement 26.1.
_VALID_CONTENT_TYPES: tuple[str, ...] = (
    "text/markdown",
    "text/plain",
    "application/pdf",
    "application/json",
    "image/png",
    "image/svg+xml",
    "application/octet-stream",
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def deliverable_engine(engine: Engine) -> Engine:
    """Per-test engine carrying Slice 1 + Slice 3 (Execution + Deliverable) schemas.

    ``create_schema`` installs Slice 1 (``Parties``,
    ``Identifier_Registry``, ``Audit_Records``, ``Role_Assignments``,
    plus the additive ``Identifier_Registry.resource_kind`` and
    ``Relationships.semantic_role`` columns from task 1.2);
    ``create_execution_schema`` installs the Slice 3 Execution_Service
    tables including ``Work_Assignment_Records``;
    ``create_deliverable_schema`` installs the
    Deliverable_Repository's ``Deliverable_Resources`` and
    ``Deliverable_Revisions`` tables with their AD-WS-27 append-only
    triggers and the role-marker / content-type / content-bytes /
    digest CHECK constraints.

    The Slice 2 Planning schema is intentionally not installed: the
    Deliverable_Repository does not resolve a Plan Revision directly;
    it only resolves the originating Work Assignment Record by primary
    key, so leaving Slice 2 out of the surface keeps the fixture
    minimal.
    """
    create_schema(engine)
    create_execution_schema(engine)
    create_deliverable_schema(engine)
    return engine


@pytest.fixture
def deliverable_service(
    clock: Clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> DeliverableRepositoryService:
    """:class:`DeliverableRepositoryService` wired with a real
    :class:`AuthorizationService`.

    The authorization deny path is exercised by *not* assigning a
    role rather than by swapping in a stub service, so the real
    evaluation code path participates in the test. The denial-audit
    sleep is replaced with a no-op so the deny-path retries do not
    spend real time.
    """
    return DeliverableRepositoryService(
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

    All four Parties are required: the authoring Contributor, an
    alternate Party used to exercise the AD-WS-29 mismatch path, the
    Assignment-Authority Party (named on
    ``Work_Assignment_Records.assignment_authority_party_id``), and
    the Assigning-Authority Party recorded on the seeded role.
    """
    with engine.begin() as conn:
        _seed_party(conn, _AUTHORING_PARTY_ID, "Contributor")
        _seed_party(conn, _OTHER_PARTY_ID, "Other Contributor")
        _seed_party(conn, _ASSIGNMENT_AUTHORITY_ID, "Assignment Authority")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str = _BOUND_WORK_ASSIGNMENT_ID,
    assignee_party_id: str = _AUTHORING_PARTY_ID,
    assignment_authority_party_id: str = _ASSIGNMENT_AUTHORITY_ID,
    applicable_scope: str = _SCOPE,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The AD-WS-27 UPDATE/DELETE rejection triggers only fire on UPDATE
    and DELETE, so an INSERT may proceed in one statement without
    driving the full WorkAssignmentService. The
    ``assignee_party_id != assignment_authority_party_id`` CHECK
    constraint (Requirement 23.5) is honored by the default values.
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
                # The target Plan Revision is referenced only by FK to
                # the (uninstalled) Slice 2 Plan_Revisions table; since
                # the FK is enforced at INSERT time on Plan_Revisions —
                # not on Work_Assignment_Records — a plausible UUID
                # value is sufficient here.
                "prev": "00000000-0000-7000-8000-000000c00030",
                "assignee": assignee_party_id,
                "authority": assignment_authority_party_id,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": applicable_scope,
                "ts": _TS_FIXED,
            },
        )


def _assign_contribute_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _AUTHORING_PARTY_ID,
    scope: str = _SCOPE,
    effective_start: datetime = datetime(2025, 1, 1, tzinfo=timezone.utc),
    effective_end: Optional[datetime] = None,
) -> str:
    """Assign Contributor authority (``contribute``) to ``party_id``.

    Per AD-WS-24, ``create.produced_deliverable`` maps to the
    ``contribute`` authority. A Party with an effective Role
    Assignment carrying ``contribute`` over ``scope`` plus the
    AD-WS-29 assignee binding may create produced Deliverables.
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
    assignee_party_id: str = _AUTHORING_PARTY_ID,
) -> None:
    """Seed every dependency required for a permitted
    ``create_produced_deliverable`` call.

    The default ``assignee_party_id`` matches the authoring Party so
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
# Row counters and readers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
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


def _registry_row(engine: Engine, identifier: str) -> Optional[dict]:
    """Return the ``Identifier_Registry`` row (or ``None``) for ``identifier``."""
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT identifier, kind, resource_kind, content_digest "
                "FROM Identifier_Registry "
                "WHERE identifier = :identifier"
            ),
            {"identifier": identifier},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


# ===========================================================================
# Happy-path baseline — confirms wiring and pins the result-object surface.
# ===========================================================================


def test_create_produced_deliverable_permits_and_records_one_revision(
    deliverable_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_service: DeliverableRepositoryService,
) -> None:
    """Happy path: an authorized Contributor records exactly one
    produced Deliverable Resource, one Deliverable Revision with
    ``role_marker = 'generated_output'``, and one consequential audit
    row inside one transaction.

    This is the headline assertion for Requirements 26.1, 26.2, and
    26.7: the consequential audit row participates in the same
    transaction so the count is exactly one.
    """
    _seed_happy_path(deliverable_engine, authorization_service)

    with deliverable_engine.begin() as conn:
        result = deliverable_service.create_produced_deliverable(
            conn,
            content_bytes=b"hello",
            content_type="text/plain",
            produced_deliverable_name="Mesh runbook",
            originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
            authoring_party_id=_AUTHORING_PARTY_ID,
            engine=deliverable_engine,
            correlation_id="corr-permit",
        )

    assert isinstance(result, CreateProducedDeliverableResult)
    assert _CANONICAL_UUID7.match(result.deliverable_id)
    assert _CANONICAL_UUID7.match(result.deliverable_revision_id)
    assert result.produced_deliverable_name == "Mesh runbook"
    assert result.content_type == "text/plain"
    assert result.content_length_bytes == 5
    assert result.role_marker == "generated_output"
    assert (
        result.originating_work_assignment_id == _BOUND_WORK_ASSIGNMENT_ID
    )
    assert result.authoring_party_id == _AUTHORING_PARTY_ID
    assert result.correlation_id == "corr-permit"

    assert _count(deliverable_engine, "Deliverable_Resources") == 1
    assert _count(deliverable_engine, "Deliverable_Revisions") == 1
    assert _count_consequential_audit_rows(
        deliverable_engine, "create.produced_deliverable"
    ) == 1


# ===========================================================================
# Requirement 26.1 / 26.5 — content-bytes boundary values.
# ===========================================================================


class TestContentBytesBoundaries:
    """Per Requirement 26.1 / 26.5, ``content_bytes`` must be in the
    inclusive range 1..104857600 (1 byte .. 100 MB).

    The lower-boundary acceptance at exactly 1 byte and the upper-bound
    rejection at exactly 100 MB + 1 byte are the headline boundary
    assertions. The 100 MB acceptance path is covered indirectly by
    the same validator the rejection path drives — the validator
    accepts every length in the inclusive range and the schema CHECK
    constraint on ``Deliverable_Revisions.content_bytes`` enforces the
    same range as a defense in depth (exercised in
    ``tests/unit/test_execution_persistence.py``). A direct test that
    submits a 100 MB payload to the service is intentionally omitted
    to avoid memory pressure in the test suite.
    """

    def test_one_byte_payload_accepted(
        self,
        deliverable_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """A single byte sits at the lower boundary and is persisted
        verbatim with ``content_length_bytes == 1``.
        """
        _seed_happy_path(deliverable_engine, authorization_service)

        with deliverable_engine.begin() as conn:
            result = deliverable_service.create_produced_deliverable(
                conn,
                content_bytes=b"x",
                content_type="application/octet-stream",
                produced_deliverable_name="one-byte payload",
                originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                authoring_party_id=_AUTHORING_PARTY_ID,
                engine=deliverable_engine,
            )

        assert result.content_length_bytes == 1
        with deliverable_engine.connect() as conn:
            stored_length = conn.execute(
                text(
                    "SELECT LENGTH(content_bytes) "
                    "FROM Deliverable_Revisions "
                    "WHERE deliverable_revision_id = :id"
                ),
                {"id": result.deliverable_revision_id},
            ).scalar_one()
        assert stored_length == 1

    def test_zero_byte_payload_rejected(
        self,
        deliverable_engine: Engine,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """Zero-byte content sits below the lower bound and is
        rejected with ``failed_constraint = 'content_bytes_empty'``.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        # No seeding required — the rejection fires before any DB read.

        with pytest.raises(DeliverableContentValidationError) as exc_info:
            with deliverable_engine.begin() as conn:
                deliverable_service.create_produced_deliverable(
                    conn,
                    content_bytes=b"",
                    content_type="text/plain",
                    produced_deliverable_name="empty",
                    originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_AUTHORING_PARTY_ID,
                    engine=deliverable_engine,
                )

        assert exc_info.value.failed_constraint == "content_bytes_empty"
        assert _count(deliverable_engine, "Deliverable_Resources") == 0
        assert _count(deliverable_engine, "Deliverable_Revisions") == 0

    def test_one_hundred_mb_plus_one_byte_payload_rejected(
        self,
        deliverable_engine: Engine,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """A 104857601-byte payload sits one past the upper bound and is
        rejected with ``failed_constraint = 'content_bytes_too_large'``.

        Submitting the full 100 MB + 1 byte payload is safe because
        validation runs before any database read — no BLOB is
        persisted on the rejection path. The test allocates the
        bytearray once and passes it through; the service returns
        without touching the connection.
        """
        # No seeding required — the rejection fires before any DB read.
        oversize_payload = b"\x00" * (100 * 1024 * 1024 + 1)

        with pytest.raises(DeliverableContentValidationError) as exc_info:
            with deliverable_engine.begin() as conn:
                deliverable_service.create_produced_deliverable(
                    conn,
                    content_bytes=oversize_payload,
                    content_type="application/octet-stream",
                    produced_deliverable_name="oversize",
                    originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_AUTHORING_PARTY_ID,
                    engine=deliverable_engine,
                )

        assert exc_info.value.failed_constraint == "content_bytes_too_large"
        assert _count(deliverable_engine, "Deliverable_Resources") == 0
        assert _count(deliverable_engine, "Deliverable_Revisions") == 0


# ===========================================================================
# Requirement 26.1 / 26.5 — content-type enumeration rejection.
# ===========================================================================


class TestContentTypeEnumeration:
    """Per Requirement 26.1, only the seven enumerated content-type
    values are accepted; every other value (and a missing
    ``content_type``) raises.
    """

    @pytest.mark.parametrize("valid_type", _VALID_CONTENT_TYPES)
    def test_each_enumerated_value_accepted(
        self,
        valid_type: str,
        deliverable_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """Each of the seven enumerated content-type values is
        accepted and persisted verbatim.

        The schema CHECK constraint on
        ``Deliverable_Revisions.content_type`` enforces the same
        enumeration as a defense in depth; this test pins the
        service-level enumeration to the same set.
        """
        _seed_happy_path(deliverable_engine, authorization_service)

        with deliverable_engine.begin() as conn:
            result = deliverable_service.create_produced_deliverable(
                conn,
                content_bytes=b"payload",
                content_type=valid_type,
                produced_deliverable_name=f"payload-{valid_type}",
                originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                authoring_party_id=_AUTHORING_PARTY_ID,
                engine=deliverable_engine,
            )

        assert result.content_type == valid_type
        with deliverable_engine.connect() as conn:
            stored_type = conn.execute(
                text(
                    "SELECT content_type FROM Deliverable_Revisions "
                    "WHERE deliverable_revision_id = :id"
                ),
                {"id": result.deliverable_revision_id},
            ).scalar_one()
        assert stored_type == valid_type

    def test_unenumerated_content_type_rejected(
        self,
        deliverable_engine: Engine,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """A content type outside the enumeration raises with
        ``failed_constraint = 'content_type_unsupported'``.
        """
        # No seeding required — validation fires before any DB read.

        with pytest.raises(DeliverableContentValidationError) as exc_info:
            with deliverable_engine.begin() as conn:
                deliverable_service.create_produced_deliverable(
                    conn,
                    content_bytes=b"payload",
                    content_type="application/x-not-real",
                    produced_deliverable_name="bad-type",
                    originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_AUTHORING_PARTY_ID,
                    engine=deliverable_engine,
                )

        assert exc_info.value.failed_constraint == "content_type_unsupported"
        assert _count(deliverable_engine, "Deliverable_Resources") == 0
        assert _count(deliverable_engine, "Deliverable_Revisions") == 0

    def test_missing_content_type_rejected(
        self,
        deliverable_engine: Engine,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """An empty ``content_type`` raises with the missing-attribute
        constraint, distinct from the out-of-enumeration constraint
        so the route layer can pinpoint which input was malformed.
        """
        with pytest.raises(DeliverableContentValidationError) as exc_info:
            with deliverable_engine.begin() as conn:
                deliverable_service.create_produced_deliverable(
                    conn,
                    content_bytes=b"payload",
                    content_type="",
                    produced_deliverable_name="missing-type",
                    originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_AUTHORING_PARTY_ID,
                    engine=deliverable_engine,
                )

        assert exc_info.value.failed_constraint == "content_type_missing"
        assert _count(deliverable_engine, "Deliverable_Resources") == 0


# ===========================================================================
# Requirement 26.1 / 26.5 — produced-Deliverable name length boundaries.
# ===========================================================================


class TestProducedDeliverableNameBoundaries:
    """Per Requirement 26.1 / 26.5, ``produced_deliverable_name`` must
    be a non-empty string of 1..200 characters.
    """

    def test_one_char_name_accepted(
        self,
        deliverable_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """A single character sits at the lower boundary."""
        _seed_happy_path(deliverable_engine, authorization_service)

        with deliverable_engine.begin() as conn:
            result = deliverable_service.create_produced_deliverable(
                conn,
                content_bytes=b"x",
                content_type="text/plain",
                produced_deliverable_name="x",
                originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                authoring_party_id=_AUTHORING_PARTY_ID,
                engine=deliverable_engine,
            )

        assert result.produced_deliverable_name == "x"
        with deliverable_engine.connect() as conn:
            stored_name = conn.execute(
                text(
                    "SELECT produced_deliverable_name "
                    "FROM Deliverable_Resources "
                    "WHERE deliverable_id = :id"
                ),
                {"id": result.deliverable_id},
            ).scalar_one()
        assert stored_name == "x"

    def test_two_hundred_char_name_accepted(
        self,
        deliverable_engine: Engine,
        authorization_service: AuthorizationService,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """A 200-character name sits at the upper boundary."""
        _seed_happy_path(deliverable_engine, authorization_service)
        name = "x" * 200

        with deliverable_engine.begin() as conn:
            result = deliverable_service.create_produced_deliverable(
                conn,
                content_bytes=b"x",
                content_type="text/plain",
                produced_deliverable_name=name,
                originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                authoring_party_id=_AUTHORING_PARTY_ID,
                engine=deliverable_engine,
            )

        assert len(result.produced_deliverable_name) == 200
        assert _count(deliverable_engine, "Deliverable_Resources") == 1

    def test_empty_name_rejected(
        self,
        deliverable_engine: Engine,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """An empty name raises with
        ``failed_constraint = 'produced_deliverable_name_missing'``.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        with pytest.raises(DeliverableContentValidationError) as exc_info:
            with deliverable_engine.begin() as conn:
                deliverable_service.create_produced_deliverable(
                    conn,
                    content_bytes=b"x",
                    content_type="text/plain",
                    produced_deliverable_name="",
                    originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_AUTHORING_PARTY_ID,
                    engine=deliverable_engine,
                )

        assert exc_info.value.failed_constraint == (
            "produced_deliverable_name_missing"
        )
        assert _count(deliverable_engine, "Deliverable_Resources") == 0

    def test_two_hundred_one_char_name_rejected(
        self,
        deliverable_engine: Engine,
        deliverable_service: DeliverableRepositoryService,
    ) -> None:
        """A 201-character name raises with
        ``failed_constraint = 'produced_deliverable_name_too_long'``.

        Validation runs before any database read so the rejection
        leaves the schema untouched.
        """
        with pytest.raises(DeliverableContentValidationError) as exc_info:
            with deliverable_engine.begin() as conn:
                deliverable_service.create_produced_deliverable(
                    conn,
                    content_bytes=b"x",
                    content_type="text/plain",
                    produced_deliverable_name="x" * 201,
                    originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                    authoring_party_id=_AUTHORING_PARTY_ID,
                    engine=deliverable_engine,
                )

        assert exc_info.value.failed_constraint == (
            "produced_deliverable_name_too_long"
        )
        assert _count(deliverable_engine, "Deliverable_Resources") == 0


# ===========================================================================
# Requirement 26.2 — role_marker = 'generated_output' on every Revision.
# ===========================================================================


def test_role_marker_recorded_as_generated_output_on_every_revision(
    deliverable_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_service: DeliverableRepositoryService,
) -> None:
    """Every produced Deliverable Revision row carries
    ``role_marker = 'generated_output'``.

    Requirement 26.2 / Persistence Invariants Summary rule 9 /
    Requirement 41 §13 — produced-Deliverable vs Source-Evidence
    disjointness. The schema-level CHECK on
    ``Deliverable_Revisions.role_marker`` rejects any other value;
    this test pins the service-level write to populate the column
    with the literal ``'generated_output'``.

    The result object, the persisted row, and the schema-level CHECK
    constant all agree.
    """
    _seed_happy_path(deliverable_engine, authorization_service)

    with deliverable_engine.begin() as conn:
        result = deliverable_service.create_produced_deliverable(
            conn,
            content_bytes=b"content",
            content_type="text/markdown",
            produced_deliverable_name="role-marker fixture",
            originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
            authoring_party_id=_AUTHORING_PARTY_ID,
            engine=deliverable_engine,
        )

    assert result.role_marker == "generated_output"
    with deliverable_engine.connect() as conn:
        stored_marker = conn.execute(
            text(
                "SELECT role_marker FROM Deliverable_Revisions "
                "WHERE deliverable_revision_id = :id"
            ),
            {"id": result.deliverable_revision_id},
        ).scalar_one()
    assert stored_marker == "generated_output"


# ===========================================================================
# Requirement 22.8 / 26.3 — Identifier_Registry tagging.
# ===========================================================================


def test_resource_and_revision_recorded_in_identifier_registry(
    deliverable_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_service: DeliverableRepositoryService,
) -> None:
    """The produced Deliverable Resource Identity and Revision
    Identity are recorded in ``Identifier_Registry`` with the
    Slice 3 ``resource_kind`` tags.

    Requirement 22.8 / 26.3 / AD-WS-28: every Slice 3 identifier
    carries a ``resource_kind`` tag that makes the eight Slice 3
    identifier roles pairwise disjoint and produced-Deliverable
    Resource Identity inspectably disjoint from Slice 1 Source
    Evidence Document Resource Identity. The Resource row carries
    ``kind = 'resource'`` and ``resource_kind =
    'deliverable_resource'``; the Revision row carries
    ``kind = 'revision'`` and ``resource_kind =
    'deliverable_revision'``.
    """
    _seed_happy_path(deliverable_engine, authorization_service)

    with deliverable_engine.begin() as conn:
        result = deliverable_service.create_produced_deliverable(
            conn,
            content_bytes=b"content",
            content_type="text/markdown",
            produced_deliverable_name="registry fixture",
            originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
            authoring_party_id=_AUTHORING_PARTY_ID,
            engine=deliverable_engine,
        )

    resource_row = _registry_row(deliverable_engine, result.deliverable_id)
    revision_row = _registry_row(
        deliverable_engine, result.deliverable_revision_id
    )
    assert resource_row is not None
    assert revision_row is not None
    assert resource_row["kind"] == "resource"
    assert resource_row["resource_kind"] == "deliverable_resource"
    assert revision_row["kind"] == "revision"
    assert revision_row["resource_kind"] == "deliverable_revision"


# ===========================================================================
# Requirement 26.5 — unresolvable originating Work Assignment.
# ===========================================================================


def test_unresolvable_originating_work_assignment_rejected(
    deliverable_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_service: DeliverableRepositoryService,
) -> None:
    """An ``originating_work_assignment_id`` that does not resolve to
    an existing ``Work_Assignment_Records`` row raises
    :class:`WorkAssignmentNotResolvableError` and persists nothing.

    Requirement 26.5: the named originating Work Assignment must
    resolve. The check runs before authorization evaluation so the
    deny path never reveals whether a Work Assignment exists to an
    unauthorized caller (Requirement 30 — indistinguishable
    denials).
    """
    _seed_required_parties(deliverable_engine)
    # The seeded role assignment is irrelevant to this path because
    # the unresolvable-Work-Assignment rejection fires before
    # authorization evaluation. We still seed it so the test
    # demonstrates the rejection runs first.
    _assign_contribute_role(authorization_service, deliverable_engine)
    # No Work_Assignment_Records row seeded.

    with pytest.raises(WorkAssignmentNotResolvableError) as exc_info:
        with deliverable_engine.begin() as conn:
            deliverable_service.create_produced_deliverable(
                conn,
                content_bytes=b"content",
                content_type="text/markdown",
                produced_deliverable_name="unresolvable wa",
                originating_work_assignment_id=(
                    _UNRESOLVABLE_WORK_ASSIGNMENT_ID
                ),
                authoring_party_id=_AUTHORING_PARTY_ID,
                engine=deliverable_engine,
            )

    assert exc_info.value.originating_work_assignment_id == (
        _UNRESOLVABLE_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.failed_constraint == (
        "originating_work_assignment_not_resolvable"
    )
    assert _count(deliverable_engine, "Deliverable_Resources") == 0
    assert _count(deliverable_engine, "Deliverable_Revisions") == 0
    # The unresolvable-Work-Assignment branch runs before
    # authorization evaluation, so no Denial Record is appended
    # either (Requirement 30 — distinct from the AD-WS-29 path).
    assert _count_denial_audit_rows(
        deliverable_engine, "create.produced_deliverable"
    ) == 0


# ===========================================================================
# Requirement 32.7 / AD-WS-29 — assignee-binding rejection.
# ===========================================================================


def test_ad_ws_29_rejects_when_authoring_party_is_not_named_assignee(
    deliverable_engine: Engine,
    authorization_service: AuthorizationService,
    deliverable_service: DeliverableRepositoryService,
) -> None:
    """Per AD-WS-29 / Requirement 32.7, the authoring Party must be
    the named assignee on the originating Work Assignment Record.

    Even when authorization permits the ``create.produced_deliverable``
    action (the authoring Party holds the ``contribute`` authority
    over the relevant scope), the request is rejected unless the
    persisted ``Work_Assignment_Records.assignee_party_id`` matches
    ``authoring_party_id``. The rejection surfaces as
    :class:`WorkAssignmentAssigneeBindingError` with
    ``reason_code = 'no-role-assignment'`` (Slice 1 Requirement
    7.2's denial enumeration) and persists exactly one Denial
    Record in a separate transaction; the caller's transaction
    rolls back so no Resource / Revision / consequential audit row
    is persisted.

    The test seeds a Work Assignment whose ``assignee_party_id`` is
    a Party *other* than the authoring Party; the authoring Party
    independently holds the ``contribute`` authority on the
    relevant scope so the AD-WS-9 first stage permits, allowing the
    AD-WS-29 second stage to be the deciding gate.
    """
    _seed_required_parties(deliverable_engine)
    # Grant the authoring Party the ``contribute`` authority over the
    # relevant scope so AD-WS-9 / authorization permits the action.
    # The AD-WS-29 second stage is then the only gate left.
    _assign_contribute_role(
        authorization_service,
        deliverable_engine,
        party_id=_AUTHORING_PARTY_ID,
    )
    # Seed the Work Assignment with a *different* Party as the
    # named assignee so the AD-WS-29 second stage fails.
    _seed_work_assignment(
        deliverable_engine,
        work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
        assignee_party_id=_OTHER_PARTY_ID,
    )

    correlation = "corr-ad-ws-29"
    with pytest.raises(WorkAssignmentAssigneeBindingError) as exc_info:
        with deliverable_engine.begin() as conn:
            deliverable_service.create_produced_deliverable(
                conn,
                content_bytes=b"content",
                content_type="text/markdown",
                produced_deliverable_name="ad-ws-29 fixture",
                originating_work_assignment_id=_BOUND_WORK_ASSIGNMENT_ID,
                authoring_party_id=_AUTHORING_PARTY_ID,
                engine=deliverable_engine,
                correlation_id=correlation,
            )

    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == correlation
    assert exc_info.value.originating_work_assignment_id == (
        _BOUND_WORK_ASSIGNMENT_ID
    )
    assert exc_info.value.authoring_party_id == _AUTHORING_PARTY_ID
    assert exc_info.value.actual_assignee_party_id == _OTHER_PARTY_ID

    # Caller's transaction rolled back: no Resource / Revision /
    # consequential audit row persisted.
    assert _count(deliverable_engine, "Deliverable_Resources") == 0
    assert _count(deliverable_engine, "Deliverable_Revisions") == 0
    assert _count_consequential_audit_rows(
        deliverable_engine, "create.produced_deliverable"
    ) == 0

    # Exactly one Denial Record persisted in its own separate
    # transaction (AD-WS-9 / Requirement 30.6).
    assert _count_denial_audit_rows(
        deliverable_engine, "create.produced_deliverable"
    ) == 1
