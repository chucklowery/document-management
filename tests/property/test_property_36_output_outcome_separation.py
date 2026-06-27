# Feature: third-walking-slice, Property 36: Output/Outcome separation and Relationship structure
"""Property 36 — Output / Outcome separation enforced from the
execution side, and Relationship structure invariants (task 16.6).

**Property 36: Output / Outcome separation enforced from the execution
side, and Relationship structure invariants**

For all request bodies submitted to any Execution_Service or
Deliverable_Repository endpoint, if the body contains any field whose
name matches a prohibited observed-outcome prefix (``observed-``,
``measurement-``, ``outcome-review-``, ``attribution-evidence-``,
``success-condition-assessment-``), the request is rejected with no
row persisted. Every persisted Completion Record carries
``outcome ∈ {Completed, Completed_With_Reservation}`` and no
observed-outcome attribute. Furthermore, for every persisted Slice 3
Record, the prescribed Relationship rows from AD-WS-26 exist with the
exact ``relationship_type``, ``source_kind``, ``target_kind``, and
``semantic_role`` values listed there, and no additional rows of those
types exist for the same source.

**Validates: Requirements 23.3, 24.2, 26.2, 27.2, 28.2, 29.2, 29.8,
34.1, 34.2, 34.3, 34.4, 34.5, 41.5, 41.6**

Strategy
========

The property statement bundles three sub-invariants. This module
exercises all of them through two Hypothesis-driven property tests:

1. **Observed-outcome rejection invariant** (Requirements 34.1, 34.2,
   34.5 / Property 36's "no row persisted"): every request body that
   names a top-level field matching one of the five observed-outcome
   prefixes must be rejected with a ``*ValidationError`` exposing
   ``failed_constraint = 'prohibited_attribute'`` and the offending
   key on :attr:`prohibited_keys`. After rejection every Slice 3
   table — the six Execution_Service Record tables plus the two
   Deliverable_Repository Resource / Revision tables — must contain
   zero rows for this Hypothesis case. This mirrors Property 35's
   rejection pattern but draws from the observed-outcome prefix list
   instead of the planning-attribute prefix list.

2. **AD-WS-26 Relationship-structure invariant** (Requirements 41.5,
   41.6, 34.3, 34.4 / Property 36's "prescribed Relationship rows
   exist with exact values, no additional rows of those types exist
   for the same source"): for every Slice 3 Record persisted through
   its real service code path, the
   ``(relationship_type, source_kind, target_kind, semantic_role)``
   tuples that AD-WS-26 prescribes must appear exactly once each,
   and no additional ``Relationships`` row of the same
   ``relationship_type`` for the same ``source_id`` may exist. The
   prescribed table per AD-WS-26 is:

   | Source Record kind            | Relationships                                                                                                                                                                                                                                       |
   |-------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
   | Work Assignment Record        | (Addresses, plan_revision, NULL); (Relates To, party, 'assignee')                                                                                                                                                                                   |
   | Work Event Record             | (Relates To, work_assignment_record, 'work_event')                                                                                                                                                                                                  |
   | Time Entry Record             | (Relates To, work_assignment_record, 'time_entry')                                                                                                                                                                                                  |
   | Deliverable Production Record | (Produces, deliverable_revision, NULL); (Addresses, deliverable_expectation_revision, NULL); (Relates To, work_assignment_record, 'production_source')                                                                                              |
   | Milestone Acceptance Record   | (Addresses, deliverable_revision, NULL)                                                                                                                                                                                                             |
   | Completion Record             | (Addresses, plan_revision, NULL)                                                                                                                                                                                                                    |

3. **Completion outcome invariant** (Requirements 29.2, 29.8, 34.3 /
   Property 36's "every persisted Completion Record carries outcome
   in {Completed, Completed_With_Reservation} and no observed-outcome
   attribute"): every persisted Completion Record carries an
   ``outcome`` value drawn from the two-value enumeration and the
   persisted row's column set does not include any observed-outcome
   attribute (verified structurally — the schema does not declare any
   observed-outcome column, and every Hypothesis-generated successful
   creation flows through the rejection screen so no caller-supplied
   observed-outcome attribute can leak into the persisted row).

Setup follows the conventions established by Property 31
(:mod:`tests.property.test_property_31_execution_creation_success`)
and Property 35
(:mod:`tests.property.test_property_35_plan_execution_separation`):
per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory
state cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps.
"""

from __future__ import annotations

import re
import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, Mapping, Optional

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
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import (
    DeliverableContentValidationError,
    DeliverableRepositoryService,
)
from walking_slice.execution._helpers import (
    OBSERVED_OUTCOME_PROHIBITED_PREFIXES,
)
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import (
    CompletionService,
    CompletionValidationError,
)
from walking_slice.execution.deliverable_productions import (
    DeliverableProductionService,
    DeliverableProductionValidationError,
)
from walking_slice.execution.milestone_acceptances import (
    MilestoneAcceptanceService,
    MilestoneAcceptanceValidationError,
)
from walking_slice.execution.time_entries import (
    TimeEntryService,
    TimeEntryValidationError,
)
from walking_slice.execution.work_assignments import (
    WorkAssignmentService,
    WorkAssignmentValidationError,
)
from walking_slice.execution.work_events import (
    WorkEventService,
    WorkEventValidationError,
)
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
# Fixed constants.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_ACTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a1"
_ASSIGNEE_PARTY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a2"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-0000000000a3"

_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-0000000000b1"
)
_AUTHORITY_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)
_SCOPE: Final[str] = "property-36/scope"

# Slice 2 prerequisite identifiers (seeded directly via INSERT).
_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-0000000000c1"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-0000000000c2"
_APPROVED_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000c3"

# Slice 3 prerequisite identifiers (seeded directly via INSERT).
_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-0000000000d1"
_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-0000000000e1"
_DELIVERABLE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-0000000000e2"
_DELIVERABLE_EXPECTATION_ID: Final[str] = "00000000-0000-7000-8000-0000000000f1"
_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000f2"
)
_DELIVERABLE_PRODUCTION_ID: Final[str] = "00000000-0000-7000-8000-0000000000d2"
_ACCEPT_MILESTONE_ACCEPTANCE_ID: Final[str] = (
    "00000000-0000-7000-8000-0000000000d3"
)

# Placeholder UUIDv7 — used for Slice 3 rejection-path kwargs the
# service never inspects because the prohibited-attribute screen
# fires first.
_PLACEHOLDER_UUID7: Final[str] = "00000000-0000-7000-8000-0000000000ff"

# Canonical UUIDv7 lowercase-hex pattern.
_CANONICAL_UUID7: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Effort-period window strictly inside the FixedClock instant.
_PERIOD_START_DT: Final[datetime] = datetime(
    2025, 12, 31, 22, 0, 0, tzinfo=timezone.utc
)
_PERIOD_END_DT: Final[datetime] = datetime(
    2025, 12, 31, 23, 0, 0, tzinfo=timezone.utc
)


# ---------------------------------------------------------------------------
# AD-WS-26 prescribed Relationship-row table.
#
# One frozen tuple per Slice 3 Record kind. Each tuple lists every
# (relationship_type, target_kind, semantic_role) triple AD-WS-26
# prescribes for sources of that kind. The Hypothesis-driven structure
# test reads this table directly so any future schema drift surfaces
# as a row-count mismatch at the property level.
#
# ``source_kind`` is the table-row constant the production code writes
# into ``Relationships.source_kind`` (e.g., ``work_assignment_record``).
# ---------------------------------------------------------------------------


_AD_WS_26: Final[dict[str, tuple[tuple[str, str, Optional[str]], ...]]] = {
    "work_assignment_record": (
        ("Addresses", "plan_revision", None),
        ("Relates To", "party", "assignee"),
    ),
    "work_event_record": (
        ("Relates To", "work_assignment_record", "work_event"),
    ),
    "time_entry_record": (
        ("Relates To", "work_assignment_record", "time_entry"),
    ),
    "deliverable_production_record": (
        ("Produces", "deliverable_revision", None),
        ("Addresses", "deliverable_expectation_revision", None),
        ("Relates To", "work_assignment_record", "production_source"),
    ),
    "milestone_acceptance_record": (
        ("Addresses", "deliverable_revision", None),
    ),
    "completion_record": (
        ("Addresses", "plan_revision", None),
    ),
}


# ---------------------------------------------------------------------------
# Slice 3 tables that the rejection path MUST NOT populate.
# Listed in Resource → Revision → Record order so a counterexample
# reads naturally.
# ---------------------------------------------------------------------------


_SLICE3_TABLES: Final[tuple[str, ...]] = (
    "Work_Assignment_Records",
    "Work_Event_Records",
    "Time_Entry_Records",
    "Deliverable_Resources",
    "Deliverable_Revisions",
    "Deliverable_Production_Records",
    "Milestone_Acceptance_Records",
    "Completion_Records",
)


# ---------------------------------------------------------------------------
# Per-case engine builder.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine carrying every schema."""
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
# Service bundle.
# ---------------------------------------------------------------------------


def _build_services() -> tuple[
    FixedClock,
    IdentityService,
    AuditLog,
    AuthorizationService,
]:
    """Construct the per-case Slice 3 collaborator bundle.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state and any audit-correlation accumulator cannot
    bleed across shrinks.
    """
    clock = FixedClock(_NOW)
    identity_service = IdentityService()
    audit_log = AuditLog(clock)
    authorization_service = AuthorizationService(
        clock=clock,
        audit_log=audit_log,
        identity_service=identity_service,
    )
    return clock, identity_service, audit_log, authorization_service



# ---------------------------------------------------------------------------
# Seed helpers (direct INSERT pattern matching Property 31).
# ---------------------------------------------------------------------------


def _seed_parties(engine: Engine) -> None:
    """Insert the three Party rows referenced by the test surface."""
    with engine.begin() as conn:
        for party_id, display in (
            (_ACTOR_PARTY_ID, "Property 36 Actor"),
            (_ASSIGNEE_PARTY_ID, "Property 36 Assignee"),
            (_ASSIGNING_AUTHORITY_ID, "Property 36 Resource Steward"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, :ts)
                    """
                ),
                {"pid": party_id, "name": display, "ts": _NOW_ISO},
            )


def _assign_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    authorities: tuple[str, ...],
    role_name: str,
    party_id: str = _ACTOR_PARTY_ID,
) -> None:
    """Grant ``authorities`` over ``_SCOPE`` to the actor Party."""
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=_SCOPE,
        authorities_granted=authorities,
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authorization_service.assign_role(conn, request)


def _seed_project(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _NOW_ISO},
        )


def _seed_activity_plan(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Activity_Plans (
                    activity_plan_id, target_project_id, title,
                    authoring_party_id, applicable_scope, recorded_at
                ) VALUES (
                    :aid, :pid, 'Property 36 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_approved_plan_revision(engine: Engine) -> None:
    """Seed one ``Plan_Revisions`` row with ``lifecycle_state='approved'``."""
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
                    :rev, :aid, NULL, 'approved',
                    'Property 36 planned scope.', '[]', '[]', NULL,
                    :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    assignee_party_id: str = _ACTOR_PARTY_ID,
    assignment_authority_party_id: str = _ASSIGNEE_PARTY_ID,
) -> None:
    """Insert one ``Work_Assignment_Records`` row by direct INSERT.

    The CHECK constraint
    ``assignee_party_id != assignment_authority_party_id``
    (Requirement 23.5) is satisfied because the two parameters default
    to distinct seed identities.
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
                    'Property 36 Work Assignment rationale.',
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": _WORK_ASSIGNMENT_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
                "assignee": assignee_party_id,
                "authority": assignment_authority_party_id,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_resource_and_revision(
    engine: Engine,
    *,
    originating_work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    authoring_party_id: str = _ACTOR_PARTY_ID,
) -> None:
    """Insert one Deliverable Resource + first Revision pair."""
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Property 36 runbook', :ts)
                """
            ),
            {"did": _DELIVERABLE_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Revisions (
                    deliverable_revision_id, deliverable_id,
                    content_type, content_bytes, content_digest_sha256,
                    role_marker, originating_work_assignment_id,
                    authoring_party_id, recorded_at
                ) VALUES (
                    :rev, :did, 'text/markdown', :bytes, :digest,
                    'generated_output', :wa, :party, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_REVISION_ID,
                "did": _DELIVERABLE_ID,
                "bytes": b"produced",
                "digest": digest,
                "wa": originating_work_assignment_id,
                "party": authoring_party_id,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    """Insert one Deliverable Expectation header + first Revision row."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _DELIVERABLE_EXPECTATION_ID, "ts": _NOW_ISO},
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
                    'Property 36 Expected Deliverable',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _ACTOR_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _seed_deliverable_production_with_relationships(engine: Engine) -> None:
    """Insert one ``Deliverable_Production_Records`` row plus its three
    AD-WS-26 Relationship rows.

    Required by the Milestone Acceptance and Completion tests so the
    Milestone Acceptance Service can resolve the produced Revision /
    Expectation Revision and the Completion Service's
    accepted-Milestone existence check succeeds.
    """
    produces_id = "00000000-0000-7000-8000-00000000d201"
    addresses_id = "00000000-0000-7000-8000-00000000d202"
    relates_to_id = "00000000-0000-7000-8000-00000000d203"
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
                    'Property 36 production rationale.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "wa": _WORK_ASSIGNMENT_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _ACTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Produces',
                    'deliverable_production_record', :pid, NULL,
                    'deliverable_revision', :did, :rev,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": produces_id,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "party": _ACTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Addresses',
                    'deliverable_production_record', :pid, NULL,
                    'deliverable_expectation_revision',
                    :exp_did, :exp_rev,
                    :party, :ts, NULL
                )
                """
            ),
            {
                "rid": addresses_id,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _ACTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Relationships (
                    relationship_id, relationship_type,
                    source_kind, source_id, source_revision_id,
                    target_kind, target_id, target_revision_id,
                    authoring_party_id, recorded_at, semantic_role
                ) VALUES (
                    :rid, 'Relates To',
                    'deliverable_production_record', :pid, NULL,
                    'work_assignment_record', :wa, NULL,
                    :party, :ts, 'production_source'
                )
                """
            ),
            {
                "rid": relates_to_id,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _ACTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )


def _seed_accept_milestone_acceptance(engine: Engine) -> None:
    """Insert one ``Milestone_Acceptance_Records`` row with outcome
    ``'Accept'`` so the Completion service's accepted-Milestone
    existence check returns ``>= 1``.
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
                    'Accept', 'Property 36 milestone accepted.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": _ACCEPT_MILESTONE_ACCEPTANCE_ID,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "party": _ACTOR_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


# ---------------------------------------------------------------------------
# Read helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a fresh connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_relationship_rows_for_source(
    engine: Engine,
    *,
    source_id: str,
    relationship_type: str,
) -> list[dict[str, Any]]:
    """Return every ``Relationships`` row with the given ``source_id``
    and ``relationship_type``.

    Property 36 verifies *exactly* the prescribed rows exist and no
    other rows of the same type exist for the same source — the
    invariant is checked by counting rows per ``relationship_type``,
    not by selecting a single match.
    """
    sql = (
        "SELECT relationship_id, relationship_type, source_kind, "
        "source_id, source_revision_id, target_kind, target_id, "
        "target_revision_id, semantic_role, recorded_at "
        "FROM Relationships "
        "WHERE relationship_type = :rt AND source_id = :sid "
        "ORDER BY relationship_id"
    )
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(sql),
                {"rt": relationship_type, "sid": source_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _assert_ad_ws_26_invariants(
    engine: Engine, *, source_kind: str, source_id: str
) -> None:
    """Assert AD-WS-26's invariants for one persisted Slice 3 Record.

    For the Record kind named by ``source_kind``:

    - Every prescribed ``(relationship_type, target_kind,
      semantic_role)`` triple from :data:`_AD_WS_26` appears exactly
      once with ``source_kind`` and ``source_id`` matching the
      arguments.
    - No additional ``Relationships`` row of any prescribed
      ``relationship_type`` exists for ``source_id`` beyond the
      prescribed count.
    """
    prescribed = _AD_WS_26[source_kind]

    # Group prescribed rows by relationship_type so the
    # "no additional rows of those types exist for the same source"
    # clause can be checked once per type, not per row.
    by_type: dict[str, list[tuple[str, Optional[str]]]] = {}
    for relationship_type, target_kind, semantic_role in prescribed:
        by_type.setdefault(relationship_type, []).append(
            (target_kind, semantic_role)
        )

    for relationship_type, expected_targets in by_type.items():
        rows = _fetch_relationship_rows_for_source(
            engine,
            source_id=source_id,
            relationship_type=relationship_type,
        )
        assert len(rows) == len(expected_targets), (
            "Property 36 violated: expected exactly "
            f"{len(expected_targets)} {relationship_type!r} rows for "
            f"source_kind={source_kind!r} source_id={source_id!r}, "
            f"got {len(rows)}. Rows: {rows!r}."
        )

        # Match each prescribed (target_kind, semantic_role) to one
        # persisted row exactly once. Property 36 requires the
        # persisted set to equal the prescribed set element-wise
        # (no extra rows, no missing rows, no duplicates).
        observed: list[tuple[str, Optional[str]]] = [
            (row["target_kind"], row["semantic_role"]) for row in rows
        ]
        observed_sorted = sorted(
            observed, key=lambda t: (t[0], t[1] or "")
        )
        expected_sorted = sorted(
            expected_targets, key=lambda t: (t[0], t[1] or "")
        )
        assert observed_sorted == expected_sorted, (
            "Property 36 violated: AD-WS-26 prescribes target_kind / "
            f"semantic_role tuples {expected_sorted!r} for "
            f"{relationship_type!r} rows from "
            f"source_kind={source_kind!r}, "
            f"but observed {observed_sorted!r} on source_id="
            f"{source_id!r}."
        )

        # Every persisted row's source_kind must equal the
        # prescribed source_kind (the third invariant clause of
        # Property 36).
        for row in rows:
            assert row["source_kind"] == source_kind, (
                "Property 36 violated: AD-WS-26 prescribes "
                f"source_kind={source_kind!r} for {relationship_type!r} "
                "rows from this Record kind, but persisted "
                f"row carried source_kind={row['source_kind']!r}. "
                f"Row: {row!r}."
            )



# ---------------------------------------------------------------------------
# Rejection-path drivers.
#
# Each driver invokes one Slice 3 write surface with the supplied
# ``request_attributes`` mapping. The typed kwargs the closure passes
# are valid-looking but never inspected because the
# prohibited-attribute screen fires first.
#
# Mirrors :mod:`tests.property.test_property_35_plan_execution_separation`.
# ---------------------------------------------------------------------------


def _build_rejection_services() -> dict[str, Any]:
    """Construct the per-case Slice 3 service bundle for the rejection
    test.

    Fresh services per Hypothesis case so :class:`IdentityService`
    in-memory state cannot bleed across shrinks. The
    prohibited-attribute screen runs before any collaborator is
    consulted, so the read-only Planning_Service and
    Deliverable_Repository collaborators only need to be
    constructible.
    """
    clock, identity_service, audit_log, authorization_service = _build_services()

    plan_revision_reader = PlanRevisionService(
        clock=None,  # type: ignore[arg-type]
        identity_service=None,  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        authorization_service=None,  # type: ignore[arg-type]
    )
    expectation_reader = DeliverableExpectationService(
        clock=None,  # type: ignore[arg-type]
        identity_service=None,  # type: ignore[arg-type]
        audit_log=None,  # type: ignore[arg-type]
        authorization_service=None,  # type: ignore[arg-type]
    )
    project_resolver = ProjectResolver()

    deliverable_repository = DeliverableRepositoryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    work_assignment_service = WorkAssignmentService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        denial_audit_sleep=lambda _seconds: None,
    )
    work_event_service = WorkEventService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    time_entry_service = TimeEntryService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    deliverable_production_service = DeliverableProductionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        deliverable_reader=deliverable_repository,
        planning_reader=expectation_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )
    milestone_acceptance_service = MilestoneAcceptanceService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        production_reader=deliverable_production_service,
        denial_audit_sleep=lambda _seconds: None,
    )
    completion_service = CompletionService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
        planning_reader=plan_revision_reader,
        project_resolver=project_resolver,
        denial_audit_sleep=lambda _seconds: None,
    )
    return {
        "work_assignment": work_assignment_service,
        "work_event": work_event_service,
        "time_entry": time_entry_service,
        "produced_deliverable": deliverable_repository,
        "deliverable_production": deliverable_production_service,
        "milestone_acceptance": milestone_acceptance_service,
        "completion": completion_service,
    }


def _call_work_assignment(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["work_assignment"].create_work_assignment(
            conn,
            target_plan_revision_id=_PLACEHOLDER_UUID7,
            assignee_party_id=_ASSIGNEE_PARTY_ID,
            assignment_authority_party_id=_ACTOR_PARTY_ID,
            assignment_rationale=None,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_work_event(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["work_event"].create_work_event(
            conn,
            target_work_assignment_id=_PLACEHOLDER_UUID7,
            event_kind="started",
            event_note=None,
            recording_party_id=_ACTOR_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_time_entry(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["time_entry"].create_time_entry(
            conn,
            target_work_assignment_id=_PLACEHOLDER_UUID7,
            effort_hours=Decimal("1.00"),
            effort_period_start=_NOW,
            effort_period_end=_NOW,
            recording_party_id=_ACTOR_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_produced_deliverable(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["produced_deliverable"].create_produced_deliverable(
            conn,
            content_bytes=b"x",
            content_type="text/plain",
            produced_deliverable_name="placeholder",
            originating_work_assignment_id=_PLACEHOLDER_UUID7,
            authoring_party_id=_ACTOR_PARTY_ID,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_deliverable_production(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["deliverable_production"].create_deliverable_production(
            conn,
            source_work_assignment_id=_PLACEHOLDER_UUID7,
            produced_deliverable_revision_id=_PLACEHOLDER_UUID7,
            target_deliverable_expectation_revision_id=_PLACEHOLDER_UUID7,
            production_rationale=None,
            recording_party_id=_ACTOR_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_milestone_acceptance(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["milestone_acceptance"].create_milestone_acceptance(
            conn,
            source_deliverable_production_id=_PLACEHOLDER_UUID7,
            outcome="Accept",
            rationale="placeholder",
            accepting_party_id=_ACTOR_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


def _call_completion(
    services: dict[str, Any],
    engine: Engine,
    request_attributes: Mapping[str, Any],
) -> None:
    with engine.begin() as conn:
        services["completion"].create_completion(
            conn,
            target_plan_revision_id=_PLACEHOLDER_UUID7,
            outcome="Completed",
            rationale="placeholder",
            source_milestone_acceptance_ids=(),
            completing_party_id=_ACTOR_PARTY_ID,
            authority_basis=_AUTHORITY_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
            request_attributes=request_attributes,
        )


_SERVICE_DISPATCH: Final[dict[str, dict[str, Any]]] = {
    "work_assignment": {
        "error_class": WorkAssignmentValidationError,
        "call": _call_work_assignment,
    },
    "work_event": {
        "error_class": WorkEventValidationError,
        "call": _call_work_event,
    },
    "time_entry": {
        "error_class": TimeEntryValidationError,
        "call": _call_time_entry,
    },
    "produced_deliverable": {
        "error_class": DeliverableContentValidationError,
        "call": _call_produced_deliverable,
    },
    "deliverable_production": {
        "error_class": DeliverableProductionValidationError,
        "call": _call_deliverable_production,
    },
    "milestone_acceptance": {
        "error_class": MilestoneAcceptanceValidationError,
        "call": _call_milestone_acceptance,
    },
    "completion": {
        "error_class": CompletionValidationError,
        "call": _call_completion,
    },
}


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


# Tail characters appended to a chosen prohibited prefix. ASCII
# alphanumerics plus ``-`` and ``_`` so the matcher's
# hyphen/underscore canonicalization is exercised by both variants.
_TAIL_ALPHABET: Final[str] = (
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789-_"
)


@st.composite
def _prohibited_observed_outcome_attribute(draw: Any) -> dict[str, Any]:
    """Draw one ``(service_kind, prohibited_key)`` rejection scenario.

    Steps:

    1. Pick a service kind from the seven Slice 3 write surfaces.
    2. Pick a prefix from
       :data:`walking_slice.execution._helpers.OBSERVED_OUTCOME_PROHIBITED_PREFIXES`
       — Property 36 covers observed-outcome prefixes only.
    3. Generate a random tail of 0..32 alphanumeric / hyphen /
       underscore characters and concatenate to the prefix.
    4. Optionally swap the case of the resulting key
       (case-insensitive matching is part of the screen contract).
    """
    service_kind = draw(st.sampled_from(sorted(_SERVICE_DISPATCH.keys())))
    prefix = draw(
        st.sampled_from(list(OBSERVED_OUTCOME_PROHIBITED_PREFIXES))
    )
    tail = draw(st.text(alphabet=_TAIL_ALPHABET, min_size=0, max_size=32))
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
# Test 1: observed-outcome prefix rejection.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 23.3, 24.2, 26.2, 27.2, 28.2, 29.2, 34.1,
# 34.2, 34.5, 41.5
@given(scenario=_prohibited_observed_outcome_attribute())
@settings(
    max_examples=100,
    deadline=2000,
    # Each case provisions a fresh on-disk SQLite database and
    # installs four schemas; the per-case setup is slower than a
    # purely in-memory test. Health-check suppressions match the
    # Property 35 / Property 22 convention.
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_prohibited_observed_outcome_attribute_rejected_no_row_persisted(
    scenario: dict[str, Any],
) -> None:
    """For all Slice 3 write surfaces and all prohibited
    observed-outcome keys, the request is rejected and no Slice 3
    row is persisted.

    The prohibited-attribute screen (step 1 of every
    ``create_<entity>`` method on each Slice 3 service) raises the
    service's ``*ValidationError`` with
    ``failed_constraint='prohibited_attribute'`` and the offending
    key on :attr:`prohibited_keys`. The caller's transaction rolls
    back; every Slice 3 Record / Resource / Revision table remains
    empty.

    **Validates: Requirements 23.3, 24.2, 26.2, 27.2, 28.2, 29.2,
    34.1, 34.2, 34.5, 41.5**
    """
    service_kind: str = scenario["service_kind"]
    prohibited_key: str = scenario["prohibited_key"]
    spec = _SERVICE_DISPATCH[service_kind]

    with tempfile.TemporaryDirectory(prefix="prop36_reject_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            services = _build_rejection_services()

            # The request_attributes mapping the route layer would
            # forward to the service. The ``placeholder`` key (and
            # its value) is an arbitrary non-prohibited entry — the
            # screen iterates keys, not values, so only the
            # ``prohibited_key`` entry triggers the rejection.
            request_attributes: dict[str, Any] = {
                "placeholder": "ignored",
                prohibited_key: "prohibited-value",
            }

            with pytest.raises(spec["error_class"]) as exc_info:
                spec["call"](services, engine, request_attributes)

            # The error must carry the structured discriminator and
            # the offending key so Requirement 34.5 holds.
            assert exc_info.value.failed_constraint == (
                "prohibited_attribute"
            ), (
                "Property 36 violated: the service raised "
                f"{type(exc_info.value).__name__} but with "
                f"failed_constraint="
                f"{exc_info.value.failed_constraint!r} (expected "
                "'prohibited_attribute'). The rejected request was: "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}."
            )
            assert prohibited_key in exc_info.value.prohibited_keys, (
                "Property 36 violated: the prohibited key was not "
                "surfaced on the error's prohibited_keys attribute. "
                f"service_kind={service_kind!r}, "
                f"prohibited_key={prohibited_key!r}, "
                f"reported={exc_info.value.prohibited_keys!r}."
            )

            # No row landed in any Slice 3 table — Property 36's "no
            # row persisted" clause (Requirement 41.5).
            for table in _SLICE3_TABLES:
                assert _count(engine, table) == 0, (
                    "Property 36 violated: rejected request "
                    f"persisted a row in Slice 3 table {table!r}. "
                    f"service_kind={service_kind!r}, "
                    f"prohibited_key={prohibited_key!r}."
                )
        finally:
            engine.dispose()



# ===========================================================================
# Test 2: AD-WS-26 Relationship structure invariants per Slice 3 Record kind.
#
# Six Hypothesis-driven tests, one per Slice 3 Record kind that writes
# Relationship rows (produced Deliverables write no Relationship row of
# their own — AD-WS-26 attaches them later via Deliverable Production).
# Each test drives one creation through its real service code path with
# a Hypothesis-generated valid payload, then asserts the AD-WS-26
# invariants via :func:`_assert_ad_ws_26_invariants`.
# ===========================================================================


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    """Strategy for a non-control text run of ``min_size..max_size`` chars."""
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


_work_assignment_strategy = st.fixed_dictionaries(
    {
        "assignment_rationale": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


_work_event_strategy = st.fixed_dictionaries(
    {
        "event_note": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


@st.composite
def _time_entry_payload(draw: Any) -> dict[str, Any]:
    """Time Entry — Requirement 25.2. Draws ``effort_hours`` in the
    canonical two-decimal-place form on ``[0.00, 24.00]``."""
    hundredths = draw(st.integers(min_value=0, max_value=2400))
    effort = (Decimal(hundredths) / Decimal(100)).quantize(Decimal("0.01"))
    return {"effort_hours": effort}


_deliverable_production_strategy = st.fixed_dictionaries(
    {
        "production_rationale": st.one_of(
            st.none(),
            _bounded_text(0, 500),
        ),
    }
)


_milestone_acceptance_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(("Accept", "Reject")),
        "rationale": _bounded_text(1, 500),
    }
)


# Completion's ``outcome`` strategy is the precise enum Requirement
# 29.2 prescribes — the property's "outcome ∈ {Completed,
# Completed_With_Reservation}" clause is verified by reading back the
# persisted row's ``outcome`` column and confirming it lies in this
# set.
_COMPLETION_OUTCOMES: Final[tuple[str, str]] = (
    "Completed",
    "Completed_With_Reservation",
)
_completion_strategy = st.fixed_dictionaries(
    {
        "outcome": st.sampled_from(_COMPLETION_OUTCOMES),
        "rationale": _bounded_text(1, 500),
    }
)


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 23.3, 41.5, 41.6
@given(payload=_work_assignment_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_work_assignment_relationship_structure_invariants(
    payload: dict[str, Any],
) -> None:
    """AD-WS-26 invariants for ``Work_Assignment_Records``.

    For every authorized, valid Work Assignment creation:

    - Exactly one ``Addresses`` row to the Plan Revision with
      ``source_kind='work_assignment_record'``,
      ``target_kind='plan_revision'``, ``semantic_role IS NULL``.
    - Exactly one ``Relates To`` row to the assignee Party with
      ``source_kind='work_assignment_record'``,
      ``target_kind='party'``, ``semantic_role='assignee'``.
    - No additional ``Addresses`` or ``Relates To`` rows for the
      Work Assignment source.

    **Validates: Requirements 23.3, 41.5, 41.6**
    """
    with tempfile.TemporaryDirectory(prefix="prop36_wa_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("assign",),
                role_name="assignment_authority",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)

            service = WorkAssignmentService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=PlanRevisionService(
                    clock=clock,
                    identity_service=identity_service,
                    audit_log=audit_log,
                    authorization_service=authorization_service,
                ),
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_work_assignment(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    assignee_party_id=_ASSIGNEE_PARTY_ID,
                    assignment_authority_party_id=_ACTOR_PARTY_ID,
                    assignment_rationale=payload["assignment_rationale"],
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.work_assignment_id)
            assert _count(engine, "Work_Assignment_Records") == 1

            _assert_ad_ws_26_invariants(
                engine,
                source_kind="work_assignment_record",
                source_id=result.work_assignment_id,
            )
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 24.2, 41.5, 41.6
@given(payload=_work_event_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_work_event_relationship_structure_invariants(
    payload: dict[str, Any],
) -> None:
    """AD-WS-26 invariants for ``Work_Event_Records``.

    For every authorized, valid Work Event ``started`` creation:

    - Exactly one ``Relates To`` row to the target Work Assignment
      Record with ``source_kind='work_event_record'``,
      ``target_kind='work_assignment_record'``,
      ``semantic_role='work_event'``.
    - No additional ``Relates To`` rows for the Work Event source.

    The recording Party matches the Work Assignment's assignee so
    the AD-WS-29 second-stage check passes.

    **Validates: Requirements 24.2, 41.5, 41.6**
    """
    with tempfile.TemporaryDirectory(prefix="prop36_we_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(
                engine,
                assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )

            service = WorkEventService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_work_event(
                    conn,
                    target_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    event_kind="started",
                    event_note=payload["event_note"],
                    recording_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.work_event_id)
            assert _count(engine, "Work_Event_Records") == 1

            _assert_ad_ws_26_invariants(
                engine,
                source_kind="work_event_record",
                source_id=result.work_event_id,
            )
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 25.2 (Time Entry shape), 41.5, 41.6
@given(payload=_time_entry_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_time_entry_relationship_structure_invariants(
    payload: dict[str, Any],
) -> None:
    """AD-WS-26 invariants for ``Time_Entry_Records``.

    For every authorized, valid Time Entry creation:

    - Exactly one ``Relates To`` row to the target Work Assignment
      Record with ``source_kind='time_entry_record'``,
      ``target_kind='work_assignment_record'``,
      ``semantic_role='time_entry'``.
    - No additional ``Relates To`` rows for the Time Entry source.

    **Validates: Requirements 41.5, 41.6**
    """
    with tempfile.TemporaryDirectory(prefix="prop36_te_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(
                engine,
                assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )

            service = TimeEntryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_time_entry(
                    conn,
                    target_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    effort_hours=payload["effort_hours"],
                    effort_period_start=_PERIOD_START_DT,
                    effort_period_end=_PERIOD_END_DT,
                    recording_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.time_entry_id)
            assert _count(engine, "Time_Entry_Records") == 1

            _assert_ad_ws_26_invariants(
                engine,
                source_kind="time_entry_record",
                source_id=result.time_entry_id,
            )
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 27.2, 41.5, 41.6
@given(payload=_deliverable_production_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_deliverable_production_relationship_structure_invariants(
    payload: dict[str, Any],
) -> None:
    """AD-WS-26 invariants for ``Deliverable_Production_Records``.

    For every authorized, valid Deliverable Production creation:

    - Exactly one ``Produces`` row to the produced Deliverable
      Revision with ``source_kind='deliverable_production_record'``,
      ``target_kind='deliverable_revision'``, ``semantic_role IS
      NULL``.
    - Exactly one ``Addresses`` row to the target Deliverable
      Expectation Revision with
      ``source_kind='deliverable_production_record'``,
      ``target_kind='deliverable_expectation_revision'``,
      ``semantic_role IS NULL``.
    - Exactly one ``Relates To`` row to the source Work Assignment
      Record with
      ``source_kind='deliverable_production_record'``,
      ``target_kind='work_assignment_record'``,
      ``semantic_role='production_source'``.
    - No additional ``Produces`` / ``Addresses`` / ``Relates To``
      rows for the Production source.

    **Validates: Requirements 27.2, 41.5, 41.6**
    """
    with tempfile.TemporaryDirectory(prefix="prop36_dp_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("contribute",),
                role_name="contributor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(
                engine,
                assignee_party_id=_ACTOR_PARTY_ID,
                assignment_authority_party_id=_ASSIGNEE_PARTY_ID,
            )
            _seed_deliverable_resource_and_revision(
                engine, authoring_party_id=_ACTOR_PARTY_ID
            )
            _seed_deliverable_expectation(engine)

            deliverable_reader = DeliverableRepositoryService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                denial_audit_sleep=lambda _seconds: None,
            )
            expectation_reader = DeliverableExpectationService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            project_resolver = ProjectResolver()

            service = DeliverableProductionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                deliverable_reader=deliverable_reader,
                planning_reader=expectation_reader,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_deliverable_production(
                    conn,
                    source_work_assignment_id=_WORK_ASSIGNMENT_ID,
                    produced_deliverable_revision_id=(
                        _DELIVERABLE_REVISION_ID
                    ),
                    target_deliverable_expectation_revision_id=(
                        _DELIVERABLE_EXPECTATION_REVISION_ID
                    ),
                    production_rationale=payload["production_rationale"],
                    recording_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.deliverable_production_id)
            assert _count(engine, "Deliverable_Production_Records") == 1

            _assert_ad_ws_26_invariants(
                engine,
                source_kind="deliverable_production_record",
                source_id=result.deliverable_production_id,
            )
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 28.2, 34.4, 41.5, 41.6
@given(payload=_milestone_acceptance_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_milestone_acceptance_relationship_structure_invariants(
    payload: dict[str, Any],
) -> None:
    """AD-WS-26 invariants for ``Milestone_Acceptance_Records``.

    For every authorized, valid Milestone Acceptance creation:

    - Exactly one ``Addresses`` row to the produced Deliverable
      Revision with
      ``source_kind='milestone_acceptance_record'``,
      ``target_kind='deliverable_revision'``,
      ``semantic_role IS NULL``.
    - No additional ``Addresses`` rows for the Milestone Acceptance
      source.

    The persisted source_kind is ``milestone_acceptance_record`` —
    Requirement 34.4 / Property 36's "every Milestone Acceptance
    Record is labelled as Milestone Acceptance and not as Outcome
    Review" clause is satisfied structurally by the column value.

    **Validates: Requirements 28.2, 34.4, 41.5, 41.6**
    """
    with tempfile.TemporaryDirectory(prefix="prop36_ma_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("accept_milestone",),
                role_name="milestone_acceptor",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(engine)
            _seed_deliverable_resource_and_revision(engine)
            _seed_deliverable_expectation(engine)
            _seed_deliverable_production_with_relationships(engine)

            # The Milestone Acceptance Service keeps a Production
            # Service reference on its public dataclass even though
            # the implementation resolves the Production row via
            # direct SQL; mirroring the Property 31 fixture pattern.
            production_reader = DeliverableProductionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                deliverable_reader=None,  # type: ignore[arg-type]
                planning_reader=None,  # type: ignore[arg-type]
                project_resolver=None,  # type: ignore[arg-type]
                denial_audit_sleep=lambda _seconds: None,
            )

            service = MilestoneAcceptanceService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                production_reader=production_reader,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_milestone_acceptance(
                    conn,
                    source_deliverable_production_id=(
                        _DELIVERABLE_PRODUCTION_ID
                    ),
                    outcome=payload["outcome"],
                    rationale=payload["rationale"],
                    accepting_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.milestone_acceptance_id)
            assert _count(engine, "Milestone_Acceptance_Records") == 1

            _assert_ad_ws_26_invariants(
                engine,
                source_kind="milestone_acceptance_record",
                source_id=result.milestone_acceptance_id,
            )
        finally:
            engine.dispose()


# Feature: third-walking-slice, Property 36: Output / Outcome separation
# Validates: Requirements 29.2, 29.8, 34.3, 41.5, 41.6
@given(payload=_completion_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)
def test_completion_outcome_enum_and_relationship_structure_invariants(
    payload: dict[str, Any],
) -> None:
    """AD-WS-26 invariants for ``Completion_Records`` plus the
    outcome-enum and no-observed-outcome-attribute invariants.

    For every authorized, valid Completion creation:

    - Exactly one ``Addresses`` row to the target Plan Revision
      with ``source_kind='completion_record'``,
      ``target_kind='plan_revision'``, ``semantic_role IS NULL``.
    - No additional ``Addresses`` rows for the Completion source.
    - The persisted ``outcome`` column lies in
      ``{Completed, Completed_With_Reservation}`` (Requirement 29.2).
    - The persisted row's column set carries no observed-outcome
      attribute (Requirements 29.8, 34.3): the
      :data:`OBSERVED_OUTCOME_PROHIBITED_PREFIXES` set is checked
      against every persisted column name, and none may match.

    **Validates: Requirements 29.2, 29.8, 34.3, 41.5, 41.6**
    """
    with tempfile.TemporaryDirectory(prefix="prop36_cp_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            (
                clock,
                identity_service,
                audit_log,
                authorization_service,
            ) = _build_services()

            _seed_parties(engine)
            _assign_role(
                authorization_service,
                engine,
                authorities=("complete",),
                role_name="completion_authority",
            )
            _seed_project(engine)
            _seed_activity_plan(engine)
            _seed_approved_plan_revision(engine)
            _seed_work_assignment(engine)
            _seed_deliverable_resource_and_revision(engine)
            _seed_deliverable_expectation(engine)
            _seed_deliverable_production_with_relationships(engine)
            _seed_accept_milestone_acceptance(engine)

            planning_reader = PlanRevisionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            project_resolver = ProjectResolver()

            service = CompletionService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
                planning_reader=planning_reader,
                project_resolver=project_resolver,
                denial_audit_sleep=lambda _seconds: None,
            )

            with engine.begin() as conn:
                result = service.create_completion(
                    conn,
                    target_plan_revision_id=_APPROVED_PLAN_REVISION_ID,
                    outcome=payload["outcome"],  # type: ignore[arg-type]
                    rationale=payload["rationale"],
                    source_milestone_acceptance_ids=(),
                    completing_party_id=_ACTOR_PARTY_ID,
                    authority_basis=_AUTHORITY_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            assert _CANONICAL_UUID7.match(result.completion_id)
            assert _count(engine, "Completion_Records") == 1

            # Read every persisted column on the Completion row.
            # The full ``SELECT *`` lets the no-observed-outcome
            # check inspect both the column names and their values.
            with engine.connect() as conn:
                row = (
                    conn.execute(
                        text(
                            "SELECT * FROM Completion_Records "
                            "WHERE completion_id = :i"
                        ),
                        {"i": result.completion_id},
                    )
                    .mappings()
                    .one()
                )

            # Property 36's outcome-enum clause (Requirement 29.2).
            assert row["outcome"] in _COMPLETION_OUTCOMES, (
                "Property 36 violated: Completion Record "
                f"{result.completion_id!r} carries outcome="
                f"{row['outcome']!r} which is not in "
                f"{_COMPLETION_OUTCOMES!r}."
            )
            assert row["outcome"] == payload["outcome"], (
                "Property 36 violated: persisted Completion outcome "
                f"({row['outcome']!r}) does not equal the request "
                f"outcome ({payload['outcome']!r})."
            )

            # Property 36's "no observed-outcome attribute" clause
            # (Requirements 29.8, 34.3): no persisted column name on
            # the Completion row matches any observed-outcome prefix.
            # Matching is hyphen/underscore-invariant and
            # case-insensitive, matching
            # :func:`walking_slice.execution._helpers._normalize_key`.
            for column_name in row.keys():
                normalized = column_name.lower().replace("_", "-")
                for prefix in OBSERVED_OUTCOME_PROHIBITED_PREFIXES:
                    assert not normalized.startswith(prefix), (
                        "Property 36 violated: Completion_Records "
                        f"column {column_name!r} matches prohibited "
                        f"observed-outcome prefix {prefix!r} on "
                        f"completion_id={result.completion_id!r}."
                    )

            # AD-WS-26 Relationship-structure invariants.
            _assert_ad_ws_26_invariants(
                engine,
                source_kind="completion_record",
                source_id=result.completion_id,
            )
        finally:
            engine.dispose()
