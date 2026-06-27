# Feature: third-walking-slice, Property 44: Execution-status Projection envelope and contents
"""Property 44 — Execution-status Projection envelope and contents (task 16.14).

**Property 44: Execution-status Projection envelope and contents**

*For all* status-bearing responses returned by the Execution_Service
that surface a derived execution status — ``Plan Revision approved``,
``Plan Revision in execution``, ``Plan Revision execution paused``,
``Plan Revision deliverable produced``, ``Plan Revision milestone
accepted``, ``Plan Revision completion recorded``, or ``Provenance
incomplete`` — the response body contains a
:class:`~walking_slice.projection.ProjectionEnvelope` carrying the
Projection Definition, source Resource Identities, source Revision
Identities, applicable temporal boundary at ISO-8601 second
precision, generated time at ISO-8601 second precision, and a
derivation indicator distinguishing the projected status from
authoritative source Records. The response does not include any
derived percent-complete, actual-cost, remaining-work,
budget-variance, forecast-cost, or outcome-attainment value, nor any
field that would constitute an observed-outcome alias. When the
Projection Definition is unresolvable the response withholds the
projected status and returns an
:class:`~walking_slice.projection.ExplanationUnavailableResponse`
identifying the missing element; in every case source Records
remain byte-equivalent.

**Validates: Requirements 39.1, 39.2, 39.3, 39.5, 39.6, 41.5, 41.6**

Strategy
========

Each Hypothesis case draws:

- a *pipeline stage* drawn from the seven projection-producing
  configurations plus the two withholding configurations (eleven
  stages in total — see :data:`_PIPELINE_STAGE_STRATEGY` for the
  enumeration). Each stage names a fully-seeded Slice 1 + Slice 2 +
  Slice 3 graph that drives :func:`project_execution_status` down a
  distinct branch of the seven-step Projection Definition from
  ``.kiro/specs/third-walking-slice/design.md`` §"Execution-status
  Projection";
- the per-case applicable temporal boundary (UTC, second precision —
  the envelope validator requires this canonical form);
- the per-case clock instant the :class:`StatusProjector` stamps as
  ``generated_at`` on the envelope (UTC, second precision);
- the per-case projector kind drawn from
  ``{"registered", "empty_registry"}``. The ``"empty_registry"``
  kind exercises Requirement 39.5 (unresolvable Projection
  Definition); the ``"registered"`` kind exercises Requirements
  39.1, 39.2, 39.3, 39.6 on every status-bearing response.

For each case the test:

1. Spins up a fresh per-case SQLite engine carrying Slice 1 + Slice 2
   + Slice 3 schemas on a unique :class:`tempfile.TemporaryDirectory`
   path so cross-case state cannot leak. The engine is the same shape
   :class:`walking_slice.app.create_app` constructs.
2. Seeds the per-stage rows by direct INSERT (AD-WS-27 append-only
   triggers fire only on UPDATE / DELETE so single-statement seeds
   are valid; mirrors :mod:`tests.unit.test_execution_projection`).
3. Snapshots every consulted Slice 1 + Slice 2 + Slice 3 table.
4. Calls :func:`project_execution_status` with the per-case projector,
   boundary, and clock instant inside a read-only connection.
5. Asserts the universal invariants:

   - **39.5 — withholding path.** When the projector has no
     Projection Definition registered, the response is an
     :class:`ExplanationUnavailableResponse` with
     ``missing_element_kind == "projection_definition"`` and
     ``missing_element_identifier`` naming the Execution Projection
     Definition; the projection withholds the projected status (the
     response shape carries no ``envelope`` or ``projected_status``
     attribute).
   - **39.1 — envelope fields.** Every status-bearing response
     (whether on the happy path or on the withholding-by-source
     branch where the projected status is ``"Provenance incomplete"``
     and the response carries an
     :class:`ExplanationUnavailableResponse` indicator) wraps the
     projected status in a :class:`ProjectionEnvelope` whose
     :attr:`~ProjectionEnvelope.definition`,
     :attr:`~ProjectionEnvelope.source_resource_ids`,
     :attr:`~ProjectionEnvelope.source_revision_ids`,
     :attr:`~ProjectionEnvelope.applicable_temporal_boundary`, and
     :attr:`~ProjectionEnvelope.generated_at` are populated;
     timestamps round-trip with UTC tzinfo and ``microsecond == 0``;
     ``source_resource_ids`` is a tuple containing the requested
     Plan Revision Identity (the addressed Resource).
   - **39.2 — derivation indicator.**
     :attr:`~ProjectionEnvelope.derivation` is pinned to
     ``"derived"`` on every status-bearing response.
   - **39.3 — no derived metric fields.** No field name on any
     response shape (recursively into every serialized payload key)
     and no key in the serialized envelope payload contains any of
     the six prohibited derived-metric substrings: ``"percent"``,
     ``"actual_cost"`` (and its compact form ``"actualcost"``),
     ``"remaining_work"`` (and ``"remainingwork"``),
     ``"budget_variance"`` (and ``"budgetvariance"``),
     ``"forecast_cost"`` (and ``"forecastcost"``),
     ``"outcome_attainment"`` (and ``"outcomeattainment"``).
   - **39.6 — no Observed-Outcome aliasing.** No field name on any
     response shape and no key in the serialized envelope payload
     contains ``"observed_outcome"``, ``"measurement"``,
     ``"success_condition"``, or ``"attribution_evidence"``; the
     projected status string itself contains no such substring.
   - **41.5 / 41.6 — Plan/Execution and Output/Outcome separation.**
     The serialized envelope and the response shape carry no
     planning-attribute key (``planned_``, ``planning_assumption``,
     ``ordering_rationale``, ``plan_review``, ``plan_approval``) and
     no observed-outcome key, mirroring the slice-wide separation
     invariants Property 35 / Property 36 assert on persisted rows.
   - **Source-Record byte-equivalence.** The full graph snapshot
     taken in step 3 equals the full graph snapshot taken after
     the projection runs (the helper is read-only — Principle 5.23,
     Requirement 39.4).

Hypothesis settings
===================

``@settings(max_examples=100, deadline=2000)`` per the Slice 3 task
brief, suppressing ``HealthCheck.too_slow`` and
``HealthCheck.data_too_large`` because each case allocates a fresh
on-disk SQLite database carrying three schemas and seeds a per-stage
pipeline before running the projection.
"""

from __future__ import annotations

import tempfile
import uuid as uuid_lib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterator, Literal

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution._projection import (
    EXECUTION_PROJECTED_STATUSES,
    EXECUTION_PROJECTION_DEFINITION,
    EXECUTION_PROJECTION_DEFINITION_NAME,
    EXECUTION_PROJECTION_DEFINITION_VERSION,
    EXECUTION_STATUS_APPROVED,
    EXECUTION_STATUS_COMPLETION_RECORDED,
    EXECUTION_STATUS_DELIVERABLE_PRODUCED,
    EXECUTION_STATUS_EXECUTION_PAUSED,
    EXECUTION_STATUS_IN_EXECUTION,
    EXECUTION_STATUS_MILESTONE_ACCEPTED,
    EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
    ExecutionStatusProjection,
    execution_projection_registry,
    project_execution_status,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
    StatusProjector,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and seed values.
#
# Mirrors the deterministic identifier set in
# :mod:`tests.unit.test_execution_projection` so a property-test
# failure references identifiers the unit-test suite already exercises.
# Identifiers are canonical UUIDv7 hex strings; reusing fixed ids
# keeps the shrunken counterexamples focused on the per-case
# stage / boundary / clock-instant dimensions Hypothesis varies.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00001"
_QUERY_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00002"

_PROJECT_ID: Final[str] = "00000000-0000-7000-8000-000000c00010"
_ACTIVITY_PLAN_ID: Final[str] = "00000000-0000-7000-8000-000000c00020"

_APPROVED_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-000000c00030"
_DRAFT_PLAN_REVISION_ID: Final[str] = "00000000-0000-7000-8000-000000c00031"
_UNRESOLVABLE_PLAN_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-0000deadbe01"
)

_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00001"
_SECOND_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00002"

_WORK_EVENT_STARTED_ID: Final[str] = "00000000-0000-7000-8000-000000e00001"
_WORK_EVENT_PAUSED_ID: Final[str] = "00000000-0000-7000-8000-000000e00002"
_SECOND_WORK_EVENT_STARTED_ID: Final[str] = (
    "00000000-0000-7000-8000-000000e00003"
)

_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-000000f00001"
_DELIVERABLE_REVISION_ID: Final[str] = "00000000-0000-7000-8000-000000f00002"
_DELIVERABLE_EXPECTATION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000f00003"
)
_DELIVERABLE_EXPECTATION_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000f00004"
)

_DELIVERABLE_PRODUCTION_ID: Final[str] = "00000000-0000-7000-8000-00000d000a01"
_ACCEPT_MILESTONE_ID: Final[str] = "00000000-0000-7000-8000-00000d000b01"
_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-00000d000c01"

_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-000000b00001"
_SCOPE: Final[str] = "pilot/team-a"

# Seed timestamps stored on every Slice 1 / Slice 2 / Slice 3 row.
# Distinct values for the started / paused events let the
# most-recent-event lookup pick the paused row deterministically.
_TS_FIRST: Final[str] = "2026-01-01T00:00:00.000Z"
_TS_SECOND: Final[str] = "2026-01-01T00:01:00.000Z"


# ---------------------------------------------------------------------------
# Prohibited field substrings (Requirements 39.3 and 39.6).
#
# Used to assert no response-shape field name and no serialized
# envelope payload key contains any of these substrings. Centralized
# so a future regression that adds a key carrying one of these
# substrings on any branch is caught by every case in the property
# test.
# ---------------------------------------------------------------------------


# Requirement 39.3 — derived metrics this slice MUST NOT surface.
# Each entry covers the snake_case canonical form plus the compact
# form a future regression might use (camelCase / no-underscore).
_PROHIBITED_DERIVED_TERMS: Final[tuple[str, ...]] = (
    "percent_complete",
    "percentcomplete",
    "actual_cost",
    "actualcost",
    "remaining_work",
    "remainingwork",
    "budget_variance",
    "budgetvariance",
    "forecast_cost",
    "forecastcost",
    "outcome_attainment",
    "outcomeattainment",
)


# Requirement 39.6 — observed-outcome aliases this slice MUST NOT
# surface on any projection response.
_PROHIBITED_OUTCOME_TERMS: Final[tuple[str, ...]] = (
    "observed_outcome",
    "observedoutcome",
    "measurement",
    "success_condition",
    "successcondition",
    "attribution_evidence",
    "attributionevidence",
)


# Requirement 41.5 — planning-attribute keys forbidden on every
# execution response shape per the slice-wide Plan / Execution
# separation invariant.
_PROHIBITED_PLANNING_TERMS: Final[tuple[str, ...]] = (
    "planned_",
    "planning_assumption",
    "ordering_rationale",
    "plan_review",
    "plan_approval",
)


# ---------------------------------------------------------------------------
# Pipeline-stage enumeration.
#
# Each stage names a fully-seeded Slice 1 + Slice 2 + Slice 3 graph
# that drives :func:`project_execution_status` down a distinct branch
# of the seven-step Projection Definition. The enumeration is closed:
# adding a stage requires updating the seeding switch and the
# expected-status switch together so a regression where a new branch
# escapes coverage is impossible to merge silently.
# ---------------------------------------------------------------------------


_PipelineStage = Literal[
    "no_assignment",
    "assignment_no_event",
    "started_event",
    "paused_most_recent_event",
    "paused_but_extra_wa_without_event",
    "deliverable_produced",
    "reject_milestone_only",
    "accept_milestone",
    "completion_recorded",
    "unresolvable_plan_revision",
    "draft_plan_revision",
]


_ALL_PIPELINE_STAGES: Final[tuple[_PipelineStage, ...]] = (
    "no_assignment",
    "assignment_no_event",
    "started_event",
    "paused_most_recent_event",
    "paused_but_extra_wa_without_event",
    "deliverable_produced",
    "reject_milestone_only",
    "accept_milestone",
    "completion_recorded",
    "unresolvable_plan_revision",
    "draft_plan_revision",
)


# Expected projected status per pipeline stage. The map is consulted
# only on the ``registered`` projector path; on the
# ``empty_registry`` projector the response is always an
# :class:`ExplanationUnavailableResponse` regardless of the seeded
# pipeline.
_EXPECTED_STATUS_BY_STAGE: Final[dict[_PipelineStage, str]] = {
    "no_assignment": EXECUTION_STATUS_APPROVED,
    "assignment_no_event": EXECUTION_STATUS_APPROVED,
    "started_event": EXECUTION_STATUS_IN_EXECUTION,
    "paused_most_recent_event": EXECUTION_STATUS_EXECUTION_PAUSED,
    "paused_but_extra_wa_without_event": EXECUTION_STATUS_IN_EXECUTION,
    "deliverable_produced": EXECUTION_STATUS_DELIVERABLE_PRODUCED,
    "reject_milestone_only": EXECUTION_STATUS_DELIVERABLE_PRODUCED,
    "accept_milestone": EXECUTION_STATUS_MILESTONE_ACCEPTED,
    "completion_recorded": EXECUTION_STATUS_COMPLETION_RECORDED,
    "unresolvable_plan_revision": EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
    "draft_plan_revision": EXECUTION_STATUS_PROVENANCE_INCOMPLETE,
}


# Stages whose seeded Plan Revision Identity the projection should
# target. Stages that exercise the withholding-by-source path target
# the unresolvable / draft Plan Revision Identity; every other stage
# targets the approved Plan Revision.
_TARGET_PLAN_REVISION_BY_STAGE: Final[dict[_PipelineStage, str]] = {
    "no_assignment": _APPROVED_PLAN_REVISION_ID,
    "assignment_no_event": _APPROVED_PLAN_REVISION_ID,
    "started_event": _APPROVED_PLAN_REVISION_ID,
    "paused_most_recent_event": _APPROVED_PLAN_REVISION_ID,
    "paused_but_extra_wa_without_event": _APPROVED_PLAN_REVISION_ID,
    "deliverable_produced": _APPROVED_PLAN_REVISION_ID,
    "reject_milestone_only": _APPROVED_PLAN_REVISION_ID,
    "accept_milestone": _APPROVED_PLAN_REVISION_ID,
    "completion_recorded": _APPROVED_PLAN_REVISION_ID,
    "unresolvable_plan_revision": _UNRESOLVABLE_PLAN_REVISION_ID,
    "draft_plan_revision": _DRAFT_PLAN_REVISION_ID,
}


# ---------------------------------------------------------------------------
# Per-case engine builder.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique
# tempdir path so cross-case state cannot leak between generated
# inputs. The engine carries the full Slice 1 + Slice 2 + Slice 3
# schema layered in the same order :class:`walking_slice.app.create_app`
# uses, so triggers and FK constraints match production.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys."""
    db_path = tmp_dir / "walking_slice.sqlite"
    url = f"sqlite:///{db_path.as_posix()}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _connection_record) -> None:  # pragma: no cover - exercised by every case
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
# Seed helpers — direct INSERT against the three schemas.
#
# AD-WS-19 (Slice 2 Plan Revision lifecycle trigger) fires only on
# UPDATE so a Plan Revision row may be inserted with
# ``lifecycle_state='approved'`` in a single statement. AD-WS-27
# append-only triggers similarly fire only on UPDATE / DELETE so all
# seeds below use single-statement INSERTs. The helpers mirror those
# in :mod:`tests.unit.test_execution_projection` so a property-test
# failure references the same seed shape the unit suite exercises.
# ---------------------------------------------------------------------------


def _seed_required_parties(engine: Engine) -> None:
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_ID, "Contributor"),
            (_QUERY_PARTY_ID, "Pilot Reviewer"),
        ):
            conn.execute(
                text(
                    """
                    INSERT INTO Parties (party_id, kind, display_name, created_at)
                    VALUES (:pid, 'person', :name, :ts)
                    """
                ),
                {"pid": party_id, "name": display, "ts": _TS_FIRST},
            )


def _seed_project(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Projects (project_id, created_at) "
                "VALUES (:pid, :ts)"
            ),
            {"pid": _PROJECT_ID, "ts": _TS_FIRST},
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
                    :aid, :pid, 'Property 44 Activity Plan',
                    :party, :scope, :ts
                )
                """
            ),
            {
                "aid": _ACTIVITY_PLAN_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_plan_revision(
    engine: Engine,
    *,
    plan_revision_id: str,
    lifecycle_state: str = "approved",
) -> None:
    """Insert one ``Plan_Revisions`` row.

    Per AD-WS-19 (lifecycle trigger only on UPDATE), a row may be
    inserted with ``lifecycle_state='approved'`` in one statement;
    the same applies to seeding ``draft`` rows for the withholding
    branch.
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
                    :rev, :aid, NULL, :state, 'Phase 1 scope', '[]',
                    '[]', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": plan_revision_id,
                "aid": _ACTIVITY_PLAN_ID,
                "state": lifecycle_state,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_work_assignment(
    engine: Engine,
    *,
    work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    target_plan_revision_id: str = _APPROVED_PLAN_REVISION_ID,
) -> None:
    """Insert one ``Work_Assignment_Records`` row.

    Honors the Requirement 23.5 CHECK
    (``assignee_party_id != assignment_authority_party_id``); both
    Identities reference seeded ``Parties`` rows.
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
                "prev": target_plan_revision_id,
                "assignee": _PARTY_ID,
                "authority": _QUERY_PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_work_event(
    engine: Engine,
    *,
    work_event_id: str,
    work_assignment_id: str = _WORK_ASSIGNMENT_ID,
    event_kind: str,
    recorded_at: str = _TS_FIRST,
) -> None:
    """Insert one ``Work_Event_Records`` row.

    The partial UNIQUE index ``idx_work_events_one_started_per_wa``
    enforces at-most-one ``started`` per Work Assignment, so any
    pipeline stage that needs more than one ``started`` event uses
    distinct Work Assignment Identities.
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Work_Event_Records (
                    work_event_id, target_work_assignment_id,
                    event_kind, event_note, recording_party_id,
                    authority_basis_type, authority_basis_id,
                    applicable_scope, recorded_at
                ) VALUES (
                    :wid, :wa, :kind, NULL, :party,
                    'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "wid": work_event_id,
                "wa": work_assignment_id,
                "kind": event_kind,
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": recorded_at,
            },
        )


def _seed_deliverable(engine: Engine) -> None:
    """Insert one Deliverable Resource + Revision pair.

    The Revision carries ``role_marker = 'generated_output'``
    (Requirement 26.2) and ``originating_work_assignment_id`` points
    at the source Work Assignment so the Production Record's FK
    references resolve.
    """
    digest = "a" * 64
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Pipeline runbook', :ts)
                """
            ),
            {"did": _DELIVERABLE_ID, "ts": _TS_FIRST},
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
                "wa": _WORK_ASSIGNMENT_ID,
                "party": _PARTY_ID,
                "ts": _TS_FIRST,
            },
        )


def _seed_deliverable_expectation(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Deliverable_Expectations "
                "(deliverable_expectation_id, created_at) "
                "VALUES (:did, :ts)"
            ),
            {"did": _DELIVERABLE_EXPECTATION_ID, "ts": _TS_FIRST},
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
                    :rev, :did, NULL, :pid, 'Pipeline runbook',
                    NULL, 'Document', NULL, :party, :scope, :ts
                )
                """
            ),
            {
                "rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "did": _DELIVERABLE_EXPECTATION_ID,
                "pid": _PROJECT_ID,
                "party": _PARTY_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_deliverable_production(engine: Engine) -> None:
    """Insert one ``Deliverable_Production_Records`` row."""
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
                    'Property 44 production.', :party,
                    'role-grant-id', :abid, :scope, :ts
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
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_milestone_acceptance(
    engine: Engine, *, outcome: str = "Accept"
) -> None:
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
                    :outcome, 'Milestone one criteria satisfied.',
                    :party, 'role-grant-id', :abid, :scope, :ts
                )
                """
            ),
            {
                "mid": _ACCEPT_MILESTONE_ID,
                "pid": _DELIVERABLE_PRODUCTION_ID,
                "did": _DELIVERABLE_ID,
                "rev": _DELIVERABLE_REVISION_ID,
                "exp_did": _DELIVERABLE_EXPECTATION_ID,
                "exp_rev": _DELIVERABLE_EXPECTATION_REVISION_ID,
                "outcome": outcome,
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_completion(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id, target_plan_revision_id,
                    target_activity_plan_id, target_project_id,
                    outcome, rationale,
                    source_milestone_acceptance_ids_json,
                    completing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :cid, :prev, :aid, :proj,
                    'Completed', 'Phase 1 completed.',
                    :sources, :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": _COMPLETION_ID,
                "prev": _APPROVED_PLAN_REVISION_ID,
                "aid": _ACTIVITY_PLAN_ID,
                "proj": _PROJECT_ID,
                "sources": f'["{_ACCEPT_MILESTONE_ID}"]',
                "party": _PARTY_ID,
                "abid": _AUTHORITY_BASIS_ID,
                "scope": _SCOPE,
                "ts": _TS_FIRST,
            },
        )


def _seed_baseline_graph(engine: Engine) -> None:
    """Seed the Slice 1 / Slice 2 prerequisites plus the approved
    Plan Revision used by every happy-path stage.

    The unresolvable / draft stages override this by either omitting
    the Plan Revision or seeding it with ``lifecycle_state='draft'``.
    """
    _seed_required_parties(engine)
    _seed_project(engine)
    _seed_activity_plan(engine)
    _seed_plan_revision(
        engine, plan_revision_id=_APPROVED_PLAN_REVISION_ID
    )


def _seed_stage(engine: Engine, stage: _PipelineStage) -> None:
    """Dispatch to the per-stage seed routine.

    The switch is exhaustive over :data:`_ALL_PIPELINE_STAGES`; a
    missing branch would surface as a ``KeyError`` rather than a
    silent stage skip.
    """
    if stage == "no_assignment":
        _seed_baseline_graph(engine)
        return

    if stage == "assignment_no_event":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        return

    if stage == "started_event":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        return

    if stage == "paused_most_recent_event":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_PAUSED_ID,
            event_kind="paused",
            recorded_at=_TS_SECOND,
        )
        return

    if stage == "paused_but_extra_wa_without_event":
        # Two Work Assignments, but only the first carries any
        # Work Event (and it is ``paused``). Per design step 7 the
        # ``paused`` projection requires *every* Work Assignment to
        # have at least one Work Event, so this stage falls back to
        # ``in execution`` — the other half of the most-recent-event
        # logic this property must hit.
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_assignment(
            engine, work_assignment_id=_SECOND_WORK_ASSIGNMENT_ID
        )
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_PAUSED_ID,
            event_kind="paused",
            recorded_at=_TS_FIRST,
        )
        return

    if stage == "deliverable_produced":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(engine)
        _seed_deliverable_expectation(engine)
        _seed_deliverable_production(engine)
        return

    if stage == "reject_milestone_only":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(engine)
        _seed_deliverable_expectation(engine)
        _seed_deliverable_production(engine)
        _seed_milestone_acceptance(engine, outcome="Reject")
        return

    if stage == "accept_milestone":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(engine)
        _seed_deliverable_expectation(engine)
        _seed_deliverable_production(engine)
        _seed_milestone_acceptance(engine, outcome="Accept")
        return

    if stage == "completion_recorded":
        _seed_baseline_graph(engine)
        _seed_work_assignment(engine)
        _seed_work_event(
            engine,
            work_event_id=_WORK_EVENT_STARTED_ID,
            event_kind="started",
            recorded_at=_TS_FIRST,
        )
        _seed_deliverable(engine)
        _seed_deliverable_expectation(engine)
        _seed_deliverable_production(engine)
        _seed_milestone_acceptance(engine, outcome="Accept")
        _seed_completion(engine)
        return

    if stage == "unresolvable_plan_revision":
        # No Plan Revision seeded for the unresolvable Identity; the
        # AD-WS-30 read returns ``None`` and the projection routes
        # down the withholding-by-source path (Requirement 39.5).
        _seed_required_parties(engine)
        _seed_project(engine)
        _seed_activity_plan(engine)
        return

    if stage == "draft_plan_revision":
        # The Plan Revision exists but is in lifecycle ``draft``; per
        # design step 1 the projection only applies to ``approved``
        # Plan Revisions, so this stage exercises the
        # withholding-by-source path with the missing-element
        # identifier set to the seeded Plan Revision Identity.
        _seed_required_parties(engine)
        _seed_project(engine)
        _seed_activity_plan(engine)
        _seed_plan_revision(
            engine,
            plan_revision_id=_DRAFT_PLAN_REVISION_ID,
            lifecycle_state="draft",
        )
        return

    raise AssertionError(f"unhandled pipeline stage {stage!r}")


# ---------------------------------------------------------------------------
# Row-level helpers for byte-equivalence assertions.
# ---------------------------------------------------------------------------


# Tables Property 44 snapshots before and after the projection runs
# to assert no Slice 1, Slice 2, or Slice 3 row is mutated. ``Audit_
# Records`` is included so a regression that accidentally appends a
# consequential row (the projection is read-only — Principle 5.23 /
# Requirement 39.4) trips the assertion immediately.
_SNAPSHOT_TABLES: Final[tuple[str, ...]] = (
    # Slice 1.
    "Parties",
    "Audit_Records",
    # Slice 2.
    "Projects",
    "Activity_Plans",
    "Plan_Revisions",
    "Deliverable_Expectations",
    "Deliverable_Expectation_Revisions",
    # Slice 3 Execution.
    "Work_Assignment_Records",
    "Work_Event_Records",
    "Deliverable_Production_Records",
    "Milestone_Acceptance_Records",
    "Completion_Records",
    # Slice 3 Deliverable_Repository.
    "Deliverable_Resources",
    "Deliverable_Revisions",
)


def _table_snapshot(engine: Engine, table: str) -> list[dict]:
    """Return every row in ``table`` as a sorted list of dicts.

    The dict form preserves every column value verbatim, so a
    regression mutating any column surfaces as a key-value
    difference. ``sorted`` keys the comparison off the full row
    contents so a row reordering does not falsely trip the
    byte-equivalence assertion (the projection's deterministic
    ORDER BY discipline is asserted by other tests).
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
    return sorted(
        (dict(row) for row in rows),
        key=lambda row: tuple(sorted(row.items())),
    )


def _full_snapshot(engine: Engine) -> dict[str, list[dict]]:
    return {table: _table_snapshot(engine, table) for table in _SNAPSHOT_TABLES}


# ---------------------------------------------------------------------------
# Recursive key collection for serialized envelope payloads.
# ---------------------------------------------------------------------------


def _collect_keys(node: Any) -> Iterator[str]:
    """Yield every dict key reachable from ``node``.

    Used by the prohibited-field assertions so a regression that
    nests a forbidden key inside a sub-dict or a list of dicts is
    caught alongside a top-level regression.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _collect_keys(v)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _collect_keys(item)


# ---------------------------------------------------------------------------
# Hypothesis strategies.
# ---------------------------------------------------------------------------


# Pipeline stage: sampled from the closed enumeration above.
# ``sorted`` keeps the draw order deterministic across Python versions
# so the Hypothesis shrink corpus is stable.
_PIPELINE_STAGE_STRATEGY: Final[st.SearchStrategy[_PipelineStage]] = (
    st.sampled_from(sorted(_ALL_PIPELINE_STAGES))
)


# Applicable temporal boundary. :class:`ProjectionEnvelope` requires
# UTC tzinfo with ``microsecond == 0``; the ``.map`` step truncates
# sub-second precision so the generated value is always acceptable to
# the envelope validator. The 2020..2030 window keeps shrunken
# counterexamples readable.
_TEMPORAL_BOUNDARY_STRATEGY: Final[st.SearchStrategy[datetime]] = st.datetimes(
    min_value=datetime(2020, 1, 1, 0, 0, 0),
    max_value=datetime(2030, 12, 31, 23, 59, 59),
    timezones=st.just(timezone.utc),
).map(lambda dt: dt.replace(microsecond=0))


# Generated time the projector stamps on every envelope. Same shape
# as the boundary strategy — the projector clock and the envelope
# validator share the same canonical-form constraint.
_CLOCK_INSTANT_STRATEGY: Final[st.SearchStrategy[datetime]] = (
    _TEMPORAL_BOUNDARY_STRATEGY
)


# Projector kind. ``registered`` exercises Requirements 39.1, 39.2,
# 39.3, 39.6 and the source-Record byte-equivalence invariant on
# every status-bearing response; ``empty_registry`` exercises
# Requirement 39.5 (unresolvable Projection Definition).
_ProjectorKind = Literal["registered", "empty_registry"]
_PROJECTOR_KIND_STRATEGY: Final[st.SearchStrategy[_ProjectorKind]] = (
    st.sampled_from(["registered", "empty_registry"])
)


def _build_projector(
    *, clock_instant: datetime, kind: _ProjectorKind
) -> StatusProjector:
    """Construct the per-case :class:`StatusProjector`.

    The ``registered`` projector carries the
    :func:`execution_projection_registry`; the ``empty_registry``
    projector carries no Projection Definitions, so every call to
    :func:`project_execution_status` routes through the
    unresolvable-definition withholding path (Requirement 39.5).
    """
    registry = (
        execution_projection_registry() if kind == "registered" else {}
    )
    return StatusProjector(
        clock=FixedClock(clock_instant),
        definition_registry=registry,
    )


# ---------------------------------------------------------------------------
# Universal envelope-shape and content-contents assertions.
# ---------------------------------------------------------------------------


def _assert_envelope_shape(
    *,
    envelope: ProjectionEnvelope,
    expected_boundary: datetime,
    expected_generated_at: datetime,
    expected_resource_id: uuid_lib.UUID,
    assertion_context: str,
) -> None:
    """Assert every required envelope field is populated correctly
    (Requirements 39.1, 39.2).

    Centralized so the happy-path and the withholding-by-source path
    assert the same shape; the only difference between the two paths
    is ``source_revision_ids`` (populated on the happy path,
    deliberately empty on the withholding-by-source path).
    """
    # Required field 1 — Projection Definition (Requirement 39.1).
    assert isinstance(envelope.definition, ProjectionDefinition), (
        assertion_context
    )
    assert envelope.definition == EXECUTION_PROJECTION_DEFINITION, (
        assertion_context
    )
    assert envelope.definition.name == EXECUTION_PROJECTION_DEFINITION_NAME, (
        assertion_context
    )
    assert envelope.definition.version == (
        EXECUTION_PROJECTION_DEFINITION_VERSION
    ), assertion_context

    # Required field 2 — source Resource Identities (Requirement
    # 39.1). The projection records the target Plan Revision Identity
    # as the single addressed Resource on every branch, including
    # the withholding-by-source branch.
    assert isinstance(envelope.source_resource_ids, tuple), assertion_context
    assert envelope.source_resource_ids == (expected_resource_id,), (
        assertion_context
    )

    # Required field 3 — source Revision Identities (Requirement
    # 39.1). Shape contract only — the per-stage caller asserts the
    # exact composition where relevant; here we pin the type.
    assert isinstance(envelope.source_revision_ids, tuple), assertion_context

    # Required field 4 — applicable temporal boundary (Requirement
    # 39.1). UTC, microsecond == 0 — the envelope validator already
    # enforces these constraints; pinning them here documents the
    # invariant for failure triage.
    assert envelope.applicable_temporal_boundary == expected_boundary, (
        assertion_context
    )
    assert envelope.applicable_temporal_boundary.tzinfo == timezone.utc, (
        assertion_context
    )
    assert envelope.applicable_temporal_boundary.microsecond == 0, (
        assertion_context
    )

    # Required field 5 — generated time (Requirement 39.1). Stamped
    # by the projector's :class:`FixedClock`; with a per-case fixed
    # instant this is deterministic across the case.
    assert envelope.generated_at == expected_generated_at, assertion_context
    assert envelope.generated_at.tzinfo == timezone.utc, assertion_context
    assert envelope.generated_at.microsecond == 0, assertion_context

    # Required field 6 — derivation indicator (Requirement 39.2 /
    # Principle 5.23). Pinned at ``"derived"`` by
    # :class:`ProjectionEnvelope`'s :class:`Literal` typing; the
    # assertion here pins the literal so any drift is caught.
    assert envelope.derivation == "derived", assertion_context


def _assert_no_prohibited_fields_in_payload(
    payload: dict[str, Any],
    *,
    assertion_context: str,
) -> None:
    """Assert no key reachable from ``payload`` carries a prohibited
    derived-metric, observed-outcome alias, or planning-attribute
    substring (Requirements 39.3, 39.6, 41.5, 41.6).
    """
    keys_in_payload = set(_collect_keys(payload))
    for key in keys_in_payload:
        lower = key.lower()
        # Requirement 39.3 — no derived-metric substring.
        for term in _PROHIBITED_DERIVED_TERMS:
            assert term not in lower, (
                f"prohibited derived term {term!r} appeared in "
                f"payload key {key!r} ({assertion_context})"
            )
        # Requirement 39.6 — no observed-outcome alias.
        for term in _PROHIBITED_OUTCOME_TERMS:
            assert term not in lower, (
                f"prohibited outcome term {term!r} appeared in "
                f"payload key {key!r} ({assertion_context})"
            )
        # Requirement 41.5 — no planning-attribute leakage on the
        # execution response shape.
        for term in _PROHIBITED_PLANNING_TERMS:
            assert term not in lower, (
                f"prohibited planning term {term!r} appeared in "
                f"payload key {key!r} ({assertion_context})"
            )


def _assert_status_string_clean(
    status: str, *, assertion_context: str
) -> None:
    """Assert the projected status string does not alias an Observed
    Outcome, a Measurement, a success-condition assessment, or an
    Intended Outcome (Requirement 39.6).
    """
    forbidden_substrings = (
        "Observed Outcome",
        "Measurement",
        "Intended Outcome",
        "success condition",
        "attainment",
    )
    for term in forbidden_substrings:
        assert term.lower() not in status.lower(), (
            f"projected status {status!r} references prohibited "
            f"outcome term {term!r} ({assertion_context})"
        )


# ---------------------------------------------------------------------------
# Property 44 — the universal envelope-shape invariant over generated
# pipeline stages.
# ---------------------------------------------------------------------------


# Feature: third-walking-slice, Property 44: Execution-status Projection envelope and contents
# Validates: Requirements 39.1, 39.2, 39.3, 39.5, 39.6, 41.5, 41.6
@given(
    stage=_PIPELINE_STAGE_STRATEGY,
    boundary=_TEMPORAL_BOUNDARY_STRATEGY,
    clock_instant=_CLOCK_INSTANT_STRATEGY,
    projector_kind=_PROJECTOR_KIND_STRATEGY,
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
def test_execution_status_projection_envelope_and_contents(
    stage: _PipelineStage,
    boundary: datetime,
    clock_instant: datetime,
    projector_kind: _ProjectorKind,
) -> None:
    """For every generated execution-status Projection:

    - **Requirement 39.5 (unresolvable Projection Definition).** When
      the projector has no Projection Definition registered, the
      response is an :class:`ExplanationUnavailableResponse` whose
      ``missing_element_kind`` is ``"projection_definition"`` and
      whose ``missing_element_identifier`` names the Execution
      Projection Definition; no envelope and no projected status are
      surfaced.

    - **Requirements 39.1, 39.2 (envelope contents).** On every
      status-bearing response (happy path and withholding-by-source
      branch), the response wraps the projected status in a
      :class:`ProjectionEnvelope` with the Projection Definition,
      source Resource Identities, source Revision Identities,
      applicable temporal boundary at ISO-8601 second precision,
      generated time at ISO-8601 second precision, and the
      ``"derived"`` derivation indicator.

    - **Requirement 39.3 (no prohibited derived metrics).** The
      response and the serialized envelope payload carry no field
      whose name references a derived percent-complete, actual-cost,
      remaining-work, budget-variance, forecast-cost, or
      outcome-attainment value.

    - **Requirement 39.6 (no Observed-Outcome aliasing).** The
      response and the serialized envelope payload carry no field
      whose name references an Observed Outcome, a Measurement, a
      success-condition assessment, or an attribution-evidence
      reference; the projected-status string itself contains no such
      substring.

    - **Requirements 41.5 / 41.6 (Plan/Execution and Output/Outcome
      separation).** The serialized envelope payload key set
      contains no planning-attribute substring (the slice-wide
      Plan / Execution separation mirrors at the projection surface).

    - **Source-Record byte equivalence (Requirement 39.4 mirror).**
      Every snapshotted Slice 1 + Slice 2 + Slice 3 table is
      byte-equivalent across the projection call; no row is mutated
      and no new row is appended.

    **Validates: Requirements 39.1, 39.2, 39.3, 39.5, 39.6, 41.5, 41.6**
    """
    target_plan_revision_id = _TARGET_PLAN_REVISION_BY_STAGE[stage]
    expected_resource_id = uuid_lib.UUID(target_plan_revision_id)
    assertion_context = (
        f"stage={stage!r} boundary={boundary.isoformat()} "
        f"clock={clock_instant.isoformat()} projector={projector_kind!r}"
    )

    with tempfile.TemporaryDirectory(prefix="prop44_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            _seed_stage(engine, stage)

            # Snapshot the full graph before the projection runs so
            # the byte-equivalence assertion can compare against
            # state-after-call.
            before = _full_snapshot(engine)

            projector = _build_projector(
                clock_instant=clock_instant, kind=projector_kind
            )

            with engine.connect() as conn:
                response = project_execution_status(
                    conn,
                    plan_revision_id=target_plan_revision_id,
                    party_id=_QUERY_PARTY_ID,
                    at=boundary,
                    status_projector=projector,
                )

            # ------------------------------------------------------------------
            # Source-Record byte equivalence (Principle 5.23 — the
            # projection is derived).
            # ------------------------------------------------------------------
            after = _full_snapshot(engine)
            assert before == after, (
                f"source Records mutated by projection call "
                f"({assertion_context})"
            )

            # ------------------------------------------------------------------
            # Requirement 39.5 — unresolvable Projection Definition
            # withholding path.
            # ------------------------------------------------------------------
            if projector_kind == "empty_registry":
                assert isinstance(response, ExplanationUnavailableResponse), (
                    f"expected ExplanationUnavailableResponse on the "
                    f"empty-registry path; got {type(response).__name__} "
                    f"({assertion_context})"
                )
                assert response.missing_element_kind == (
                    "projection_definition"
                ), assertion_context
                assert response.missing_element_identifier == (
                    EXECUTION_PROJECTION_DEFINITION_NAME
                ), assertion_context

                # The response shape carries no wrapped-status
                # attributes (Requirement 39.5 "withhold" semantics
                # are structural, not just absent values).
                assert not hasattr(response, "envelope"), assertion_context
                assert not hasattr(response, "projected_status"), (
                    assertion_context
                )

                # The serialized response carries no prohibited
                # derived-metric, observed-outcome, or
                # planning-attribute key either.
                _assert_no_prohibited_fields_in_payload(
                    response.model_dump(),
                    assertion_context=(
                        f"empty-registry payload ({assertion_context})"
                    ),
                )
                return

            # ------------------------------------------------------------------
            # Registered-projector path. Both the happy path and the
            # withholding-by-source branch produce an
            # :class:`ExecutionStatusProjection` value object.
            # ------------------------------------------------------------------
            assert isinstance(response, ExecutionStatusProjection), (
                f"expected ExecutionStatusProjection on the registered-"
                f"projector path; got {type(response).__name__} "
                f"({assertion_context})"
            )

            expected_status = _EXPECTED_STATUS_BY_STAGE[stage]
            assert response.projected_status == expected_status, (
                f"projected_status {response.projected_status!r} != "
                f"expected {expected_status!r} ({assertion_context})"
            )

            # Requirement 39.6 — projected-status string itself never
            # references an outcome alias.
            _assert_status_string_clean(
                response.projected_status,
                assertion_context=assertion_context,
            )

            # Every projected status surfaced lives in the published
            # membership set.
            assert response.projected_status in EXECUTION_PROJECTED_STATUSES, (
                assertion_context
            )

            # Requirements 39.1, 39.2 — envelope shape.
            _assert_envelope_shape(
                envelope=response.envelope,
                expected_boundary=boundary,
                expected_generated_at=clock_instant,
                expected_resource_id=expected_resource_id,
                assertion_context=assertion_context,
            )

            # Withholding-by-source branch carries an
            # :class:`ExplanationUnavailableResponse` indicator on
            # the projection (Requirement 39.5). The happy path
            # leaves the field as ``None``.
            if stage in {"unresolvable_plan_revision", "draft_plan_revision"}:
                assert (
                    response.projected_status
                    == EXECUTION_STATUS_PROVENANCE_INCOMPLETE
                ), assertion_context
                assert response.explanation_unavailable is not None, (
                    assertion_context
                )
                assert (
                    response.explanation_unavailable.missing_element_kind
                    == "source_revision"
                ), assertion_context
                assert (
                    response.explanation_unavailable.missing_element_identifier
                    == target_plan_revision_id
                ), assertion_context
                # No Slice 3 Record could be consulted on the
                # withholding-by-source path, so the envelope's
                # source-revision list is empty.
                assert response.envelope.source_revision_ids == (), (
                    assertion_context
                )
            else:
                assert response.explanation_unavailable is None, (
                    assertion_context
                )

            # Requirements 39.3, 39.6, 41.5 — no prohibited field
            # name appears on the serialized envelope payload.
            _assert_no_prohibited_fields_in_payload(
                response.envelope.model_dump(),
                assertion_context=(
                    f"envelope payload ({assertion_context})"
                ),
            )
        finally:
            engine.dispose()
