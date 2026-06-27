# Feature: fourth-walking-slice, Property 51: Output is not Outcome, re-asserted from the outcome side
"""Property 51 — Output is not Outcome, re-asserted from the outcome side (task 15.6).

**Property 51: Output is not Outcome, re-asserted from the outcome side**

*For all* Outcome Review Records, no Review was created by automatic
derivation from any Slice 3 Completion Record, Milestone Acceptance,
Deliverable Production, or produced Deliverable Revision finalization;
every Review carries an explicit attribution stance drawn from
``{Asserted, Partial, Unattributed, Contradicted}``; and an attribution
stance of ``Asserted`` or ``Contradicted`` is accompanied by an
attribution-evidence reference of at least one character. *For all*
request bodies, any field whose stated purpose is to assert that a
Completion Record by itself satisfies the addressed Intended Outcome, or
to alias a Completion Record as an Observed Outcome, is rejected with no
row persisted.

**Validates: Requirements 49.9, 54.1, 54.2, 54.3, 54.4, 61.6**

Strategy
========

The property statement bundles three sub-invariants. This module
exercises all of them through two Hypothesis-driven property tests, both
built on the same per-case full Slice 1–4 prerequisite chain so the only
variable under test is (a) the attribution stance / evidence the Review
carries, or (b) the prohibited Completion-as-Outcome request field that
must be rejected.

1. **Explicit-only creation + explicit attribution stance**
   (:func:`test_outcome_review_is_explicit_and_carries_attribution_stance`,
   Requirements 49.9, 54.1, 54.2, 54.3). Per case the test seeds the
   complete prerequisite chain — including a finalized Slice 3 Completion
   Record and a produced Deliverable Revision — through the real Slice 2 /
   Slice 4 services, then asserts ``Outcome_Review_Records`` is **empty**
   before any explicit ``create_outcome_review`` call: recording the
   Slice 3 Completion / produced Deliverable finalization and the whole
   Slice 4 measurement chain did not by itself materialise any Outcome
   Review (Requirements 49.9, 54.1). It then invokes
   :meth:`OutcomeReviewService.create_outcome_review` with a
   Hypothesis-drawn attribution stance (across all four enumerated values)
   and a stance-appropriate attribution-evidence reference, and asserts the
   single persisted Review row carries an attribution stance in
   ``{Asserted, Partial, Unattributed, Contradicted}`` and — when the
   stance is ``Asserted`` or ``Contradicted`` — an attribution-evidence
   reference of at least one character (Requirements 54.2, 54.3 / 49.4).

2. **Completion-as-Outcome aliasing rejected**
   (:func:`test_completion_as_outcome_intent_field_is_rejected`,
   Requirement 54.4). Per case the test seeds the same complete, otherwise
   valid prerequisite chain so the *only* reason a creation could fail is
   the prohibited request field, then submits an otherwise-valid
   ``create_outcome_review`` request whose forwarded raw request body
   carries one Hypothesis-drawn field whose stated purpose is to assert a
   Completion Record by itself satisfies the addressed Intended Outcome, or
   to alias a Completion Record as an Observed Outcome (drawn from
   :data:`COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS` with random qualifier
   prefixes/suffixes and hyphen/underscore/case perturbation). It asserts
   the request is rejected with an
   :class:`OutcomeReviewValidationError` whose ``failed_constraint`` is
   ``"prohibited_attribute"`` and whose ``prohibited_keys`` names the
   offending field, and that no ``Outcome_Review_Records`` row was
   persisted.

Setup mirrors the Slice 4 Property 46 conventions
(:mod:`tests.property.test_property_46_outcome_creation_anchoring`):
per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite
file, fresh services per case so :class:`IdentityService` in-memory state
cannot bleed across shrinks, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps, and the
authorization permit path exercised by granting the precise required
authority so the real evaluation code path participates. The cited
Slice 3 Completion Record and produced Deliverable Revision are seeded by
direct INSERT (the established Slice 4 convention), which is the strongest
form of the "no automatic derivation" invariant: those rows exist in the
database, yet no Outcome Review appears until the explicit Outcome_Service
write is invoked.
"""

from __future__ import annotations

import re
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import AssignRoleRequest, AuthorizationService
from walking_slice.clock import FixedClock
from walking_slice.deliverables._persistence import create_deliverable_schema
from walking_slice.deliverables.repository import DeliverableRepositoryService
from walking_slice.execution._persistence import create_execution_schema
from walking_slice.execution.completions import CompletionService
from walking_slice.identity import IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._helpers import (
    COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS,
)
from walking_slice.outcome._persistence import create_outcome_schema
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import (
    OutcomeReviewService,
    OutcomeReviewValidationError,
)
from walking_slice.outcome.success_condition_assessments import (
    SuccessConditionAssessmentService,
)
from walking_slice.persistence import create_schema
from walking_slice.planning._persistence import create_planning_schema
from walking_slice.planning._project_resolver import ProjectResolver
from walking_slice.planning.intended_outcomes import IntendedOutcomeService
from walking_slice.planning.plan_revisions import PlanRevisionService


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed identifiers and constants — the deterministic prerequisite chain.
# Property 51 only quantifies over the attribution stance / evidence the
# Review carries and the prohibited request field, so deterministic
# prerequisite IDs keep shrunken counterexamples actionable.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = "2026-01-01T00:00:00.000Z"

_OWNER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00001"
_ASSIGNING_AUTHORITY_ID: Final[str] = "00000000-0000-7000-8000-000000a00002"
_DEFINER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00003"
_RECORDER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00004"
_ASSESSOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00005"
_REVIEWER_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00006"
_COMPLETING_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00007"
_CONTRIBUTOR_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000a00008"

_OBJECTIVE_ID: Final[str] = "00000000-0000-7000-8000-000000c00001"
_OBJECTIVE_REV_ID: Final[str] = "00000000-0000-7000-8000-000000c00002"
_DECISION_ID: Final[str] = "00000000-0000-7000-8000-000000c00003"
_SCOPE: Final[str] = "pilot/team-a"

_AUTHORITY_BASIS_ID: Final[UUID] = UUID("00000000-0000-7000-8000-0000000ba001")
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Directly-seeded citable Slice 3 finalization artifacts.
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000d00004"
)

_UNIT: Final[str] = "percent"
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"

_CANONICAL_UUID7: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_ATTRIBUTION_STANCES: Final[tuple[str, ...]] = (
    "Asserted",
    "Partial",
    "Unattributed",
    "Contradicted",
)
_STANCES_REQUIRING_EVIDENCE: Final[frozenset[str]] = frozenset(
    {"Asserted", "Contradicted"}
)


# ---------------------------------------------------------------------------
# Per-case engine builder. Each Hypothesis case builds a fresh SQLite engine
# on a unique temp-dir path so cross-case state cannot leak; the engine
# carries every schema the Outcome Review write path may consult.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys."""
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
    create_outcome_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Per-case service bundle (mirrors Property 46).
# ---------------------------------------------------------------------------


class _Services:
    """Per-case bundle of every collaborator the chain builders need."""

    def __init__(self) -> None:
        self.clock = FixedClock(_NOW)
        self.identity = IdentityService()
        self.audit = AuditLog(self.clock)
        self.authz = AuthorizationService(
            clock=self.clock,
            audit_log=self.audit,
            identity_service=self.identity,
        )
        self.intended = IntendedOutcomeService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
        )
        self.definitions = MeasurementDefinitionService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
        )
        self.records = MeasurementRecordService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            definition_reader=self.definitions,
        )
        self.observed = ObservedOutcomeService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
            measurement_reader=self.records,
            definition_reader=self.definitions,
        )
        self.assessments = SuccessConditionAssessmentService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
            observed_outcome_reader=self.observed,
        )
        self.completions = CompletionService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            planning_reader=PlanRevisionService(
                clock=None,  # type: ignore[arg-type]
                identity_service=None,  # type: ignore[arg-type]
                audit_log=None,  # type: ignore[arg-type]
                authorization_service=None,  # type: ignore[arg-type]
            ),
            project_resolver=ProjectResolver(),
            denial_audit_sleep=lambda _seconds: None,
        )
        self.deliverables = DeliverableRepositoryService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            denial_audit_sleep=lambda _seconds: None,
        )
        self.reviews = OutcomeReviewService(
            clock=self.clock,
            identity_service=self.identity,
            audit_log=self.audit,
            authorization_service=self.authz,
            intended_outcome_reader=self.intended,
            assessment_reader=self.assessments,
            completion_reader=self.completions,
            deliverable_reader=self.deliverables,
            denial_audit_sleep=lambda _seconds: None,
        )


# ---------------------------------------------------------------------------
# Seed helpers (mirror Property 46 / tests/unit/test_outcome_*.py).
# ---------------------------------------------------------------------------


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _NOW_ISO},
    )


def _seed_objective(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Objectives (objective_id, created_at) "
                "VALUES (:oid, :ts)"
            ),
            {"oid": _OBJECTIVE_ID, "ts": _NOW_ISO},
        )
        conn.execute(
            text(
                """
                INSERT INTO Objective_Revisions (
                    objective_revision_id, objective_id,
                    parent_revision_id, statement, rationale,
                    target_decision_id, authoring_party_id,
                    applicable_scope, recorded_at
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
                "pid": _OWNER_PARTY_ID,
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )


def _assign_role(
    authz: AuthorizationService,
    engine: Engine,
    *,
    party_id: str,
    role_name: str,
    authority: str,
) -> None:
    request = AssignRoleRequest(
        party_id=party_id,
        role_name=role_name,
        scope=_SCOPE,
        authorities_granted=(authority,),
        effective_start=_NOW - timedelta(days=365),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        authz.assign_role(conn, request)


def _seed_world(svc: _Services, engine: Engine) -> None:
    """Seed every Party, the Objective, and all role grants the chain needs."""
    with engine.begin() as conn:
        _seed_party(conn, _OWNER_PARTY_ID, "Intended Outcome Owner")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
        _seed_party(conn, _DEFINER_PARTY_ID, "Measurement Definer")
        _seed_party(conn, _RECORDER_PARTY_ID, "Measurement Recorder")
        _seed_party(conn, _ASSESSOR_PARTY_ID, "Outcome Assessor")
        _seed_party(conn, _REVIEWER_PARTY_ID, "Outcome Reviewer")
        _seed_party(conn, _COMPLETING_PARTY_ID, "Completion Authority")
        _seed_party(conn, _CONTRIBUTOR_PARTY_ID, "Contributor")
    _seed_objective(engine)
    for party_id, role_name, authority in (
        (_OWNER_PARTY_ID, "intended_outcome_owner", "modify"),
        (_DEFINER_PARTY_ID, "measurement_definer", "define_measurement"),
        (_RECORDER_PARTY_ID, "measurement_recorder", "record_measurement"),
        (_ASSESSOR_PARTY_ID, "outcome_assessor", "assess_outcome"),
        (_REVIEWER_PARTY_ID, "outcome_reviewer", "issue_outcome_review"),
    ):
        _assign_role(
            svc.authz,
            engine,
            party_id=party_id,
            role_name=role_name,
            authority=authority,
        )


def _seed_intended_outcome(svc: _Services, engine: Engine) -> tuple[str, str]:
    """Create one ``intended`` Intended Outcome via the real Slice 2 service."""
    with engine.begin() as conn:
        intended = svc.intended.create_intended_outcome(
            conn,
            target_objective_id=_OBJECTIVE_ID,
            success_condition="Onboarding completes in under two days.",
            observation_window="30 days post launch",
            attribution_assumption="Sampling rate held constant.",
            authoring_party_id=_OWNER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return intended.intended_outcome_id, intended.intended_outcome_revision_id


def _seed_definition(
    svc: _Services, engine: Engine, *, intended_outcome_revision_id: str
) -> str:
    with engine.begin() as conn:
        definition = svc.definitions.create_measurement_definition(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            measurand_description="Adoption rate of the new workflow.",
            unit_of_measure=_UNIT,
            observation_window=_WINDOW_2025,
            cadence="monthly",
            data_source="product analytics",
            authoring_party_id=_DEFINER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return definition.measurement_definition_revision_id


def _seed_native_record(
    svc: _Services, engine: Engine, *, definition_revision_id: str
) -> str:
    with engine.begin() as conn:
        result = svc.records.create_native_measurement(
            conn,
            target_measurement_definition_revision_id=definition_revision_id,
            observed_value=Decimal("12.5"),
            observed_value_unit=_UNIT,
            observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
            recording_party_id=_RECORDER_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.measurement_record_id


def _seed_observed_outcome(
    svc: _Services,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    cited_record_ids: list[str],
) -> str:
    with engine.begin() as conn:
        result = svc.observed.create_observed_outcome(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            assessment_summary="Adoption trending toward the success target.",
            cited_measurement_record_ids=cited_record_ids,
            authoring_party_id=_ASSESSOR_PARTY_ID,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.observed_outcome_revision_id


def _seed_assessment(
    svc: _Services,
    engine: Engine,
    *,
    intended_outcome_revision_id: str,
    sourced_observed_outcome_revision_id: str,
) -> str:
    with engine.begin() as conn:
        result = svc.assessments.create_assessment(
            conn,
            target_intended_outcome_revision_id=intended_outcome_revision_id,
            sourced_observed_outcome_revision_id=(
                sourced_observed_outcome_revision_id
            ),
            assessment_category="Satisfied",
            assessment_rationale="Measured adoption met the success threshold.",
            assessing_party_id=_ASSESSOR_PARTY_ID,
            authority_basis=_BASIS,
            applicable_scope=_SCOPE,
            engine=engine,
        )
    return result.assessment_id


def _seed_citable_completion(engine: Engine) -> str:
    """Seed one resolvable Slice 3 Completion Record by direct INSERT.

    Recording this Completion Record (a Slice 3 finalization artifact) must
    not by itself materialise any Outcome Review (Requirements 49.9, 54.1).
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO Completion_Records (
                    completion_id, target_plan_revision_id,
                    target_activity_plan_id, target_project_id,
                    outcome, rationale, source_milestone_acceptance_ids_json,
                    completing_party_id, authority_basis_type,
                    authority_basis_id, applicable_scope, recorded_at
                ) VALUES (
                    :cid, :prev, :aid, :proj, 'Completed',
                    'Phase 1 completed.', '[]', :party, 'role-grant-id',
                    :abid, :scope, :ts
                )
                """
            ),
            {
                "cid": _CITABLE_COMPLETION_ID,
                "prev": "00000000-0000-7000-8000-0000000c0fff",
                "aid": "00000000-0000-7000-8000-0000000a0fff",
                "proj": "00000000-0000-7000-8000-0000000b0fff",
                "party": _COMPLETING_PARTY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )
    return _CITABLE_COMPLETION_ID


def _seed_citable_deliverable_revision(engine: Engine) -> str:
    """Seed one resolvable produced Deliverable Revision by direct INSERT."""
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
                "wid": _CITABLE_WORK_ASSIGNMENT_ID,
                "prev": "00000000-0000-7000-8000-0000000c0ffe",
                "assignee": _CONTRIBUTOR_PARTY_ID,
                "authority": _ASSIGNING_AUTHORITY_ID,
                "abid": str(_AUTHORITY_BASIS_ID),
                "scope": _SCOPE,
                "ts": _NOW_ISO,
            },
        )
        conn.execute(
            text(
                """
                INSERT INTO Deliverable_Resources (
                    deliverable_id, produced_deliverable_name, created_at
                ) VALUES (:did, 'Mesh runbook', :ts)
                """
            ),
            {"did": _CITABLE_DELIVERABLE_ID, "ts": _NOW_ISO},
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
                "rev": _CITABLE_DELIVERABLE_REVISION_ID,
                "did": _CITABLE_DELIVERABLE_ID,
                "bytes": b"produced",
                "digest": "a" * 64,
                "wa": _CITABLE_WORK_ASSIGNMENT_ID,
                "party": _CONTRIBUTOR_PARTY_ID,
                "ts": _NOW_ISO,
            },
        )
    return _CITABLE_DELIVERABLE_REVISION_ID


def _seed_full_review_prerequisites(
    svc: _Services, engine: Engine
) -> dict[str, str]:
    """Seed the full Slice 1–4 chain an Outcome Review needs to cite.

    Returns the identities (target Intended Outcome Revision, cited
    Assessment, cited Completion Record, cited produced Deliverable
    Revision) the explicit ``create_outcome_review`` call references.
    """
    _seed_world(svc, engine)
    _intended_id, intended_rev = _seed_intended_outcome(svc, engine)
    definition_rev = _seed_definition(
        svc, engine, intended_outcome_revision_id=intended_rev
    )
    record_id = _seed_native_record(
        svc, engine, definition_revision_id=definition_rev
    )
    observed_rev = _seed_observed_outcome(
        svc,
        engine,
        intended_outcome_revision_id=intended_rev,
        cited_record_ids=[record_id],
    )
    assessment_id = _seed_assessment(
        svc,
        engine,
        intended_outcome_revision_id=intended_rev,
        sourced_observed_outcome_revision_id=observed_rev,
    )
    completion_id = _seed_citable_completion(engine)
    deliverable_rev = _seed_citable_deliverable_revision(engine)
    return {
        "intended_rev": intended_rev,
        "assessment_id": assessment_id,
        "completion_id": completion_id,
        "deliverable_rev": deliverable_rev,
    }


# ---------------------------------------------------------------------------
# Probe helpers.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _review_row(engine: Engine, outcome_review_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    """
                    SELECT outcome_review_id, attribution_stance,
                           attribution_evidence_reference
                      FROM Outcome_Review_Records
                     WHERE outcome_review_id = :rid
                    """
                ),
                {"rid": outcome_review_id},
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------------------
# Strategies.
# ---------------------------------------------------------------------------


_TEXT_ALPHABET: Final[str] = (
    " ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    "0123456789-_.,;:'!?"
)


def _bounded_text(min_size: int, max_size: int) -> st.SearchStrategy[str]:
    return st.text(
        alphabet=_TEXT_ALPHABET, min_size=min_size, max_size=max_size
    )


@st.composite
def _review_stance_payload(draw: Any) -> dict[str, Any]:
    """Draw an Outcome Review body across all four attribution stances.

    The attribution-evidence reference is non-empty when the drawn stance
    is ``Asserted`` or ``Contradicted`` (the Requirement 49.4 / 54.3 rule)
    and free to be empty otherwise so the test exercises both sides of the
    stance/evidence coupling.
    """
    stance = draw(st.sampled_from(_ATTRIBUTION_STANCES))
    if stance in _STANCES_REQUIRING_EVIDENCE:
        evidence = draw(_bounded_text(1, 200))
    else:
        evidence = draw(_bounded_text(0, 200))
    return {
        "review_outcome": draw(
            st.sampled_from(
                ["Achieved", "Partially_Achieved", "Not_Achieved", "Inconclusive"]
            )
        ),
        "attribution_stance": stance,
        "confidence": draw(st.sampled_from(["High", "Moderate", "Low"])),
        "review_rationale": draw(_bounded_text(1, 200)),
        "attribution_evidence_reference": evidence,
    }


# Alphabet for the qualifier prefix/suffix wrapped around an intent marker;
# only characters that survive :func:`_normalize_key` without introducing a
# separate prohibited prefix.
_QUALIFIER_ALPHABET: Final[str] = "abcdefghijklmnopqrstuvwxyz0123456789-_"


def _perturb_separators_and_case(key: str) -> st.SearchStrategy[str]:
    """Return a strategy that randomly rewrites ``-``/``_`` and case.

    The Outcome_Service matches prohibited fields case-insensitively and
    invariant under hyphen/underscore swaps, so a Completion-as-Outcome
    intent marker must be rejected under every such perturbation.
    """

    def _rewrite(seed: list[int]) -> str:
        out_chars: list[str] = []
        for index, ch in enumerate(key):
            choice = seed[index % len(seed)] if seed else 0
            if ch in "-_":
                out_chars.append("-" if choice % 2 == 0 else "_")
            elif choice % 3 == 0:
                out_chars.append(ch.upper())
            else:
                out_chars.append(ch)
        return "".join(out_chars)

    return st.lists(st.integers(0, 5), min_size=1, max_size=8).map(_rewrite)


@st.composite
def _completion_as_outcome_field(draw: Any) -> str:
    """Draw a field name whose stated purpose is to assert a Completion
    satisfies the Intended Outcome, or to alias a Completion as an Observed
    Outcome (Requirement 54.4).

    Built by embedding one canonical intent marker drawn from
    :data:`COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS` inside an optional
    qualifier prefix/suffix, then perturbing separators and case so the
    test covers the full case-insensitive, hyphen/underscore-invariant
    matching contract.
    """
    marker = draw(st.sampled_from(COMPLETION_AS_OUTCOME_INTENT_SUBSTRINGS))
    prefix = draw(st.text(alphabet=_QUALIFIER_ALPHABET, min_size=0, max_size=12))
    suffix = draw(st.text(alphabet=_QUALIFIER_ALPHABET, min_size=0, max_size=12))
    # Join with hyphens so the qualifier never fuses into the marker in a way
    # that would split the substring; the marker stays contiguous.
    parts = [p for p in (prefix, marker, suffix) if p != ""]
    composed = "-".join(parts)
    return draw(_perturb_separators_and_case(composed))


# ===========================================================================
# Property 51 tests.
# ===========================================================================


# Feature: fourth-walking-slice, Property 51: Output is not Outcome, re-asserted from the outcome side
@given(payload=_review_stance_payload())
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_outcome_review_is_explicit_and_carries_attribution_stance(
    payload: dict[str, Any],
) -> None:
    """No Outcome Review is derived from Slice 3 finalization, and every
    Review carries an explicit attribution stance.

    Recording the Slice 3 Completion Record and produced Deliverable
    Revision (and the whole Slice 4 measurement chain) leaves
    ``Outcome_Review_Records`` empty (Requirements 49.9, 54.1); the single
    Review that appears does so only via the explicit
    ``create_outcome_review`` call and carries an attribution stance in
    ``{Asserted, Partial, Unattributed, Contradicted}`` with a non-empty
    attribution-evidence reference whenever the stance is ``Asserted`` or
    ``Contradicted`` (Requirements 54.2, 54.3)."""
    with tempfile.TemporaryDirectory(prefix="prop51_stance_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            refs = _seed_full_review_prerequisites(svc, engine)

            # No Review was derived automatically from the Slice 3 Completion /
            # produced Deliverable finalization or the Slice 4 chain.
            assert _count(engine, "Outcome_Review_Records") == 0

            with engine.begin() as conn:
                result = svc.reviews.create_outcome_review(
                    conn,
                    target_intended_outcome_revision_id=refs["intended_rev"],
                    review_outcome=payload["review_outcome"],
                    attribution_stance=payload["attribution_stance"],
                    confidence=payload["confidence"],
                    review_rationale=payload["review_rationale"],
                    attribution_evidence_reference=(
                        payload["attribution_evidence_reference"]
                    ),
                    cited_assessment_ids=[refs["assessment_id"]],
                    cited_completion_ids=[refs["completion_id"]],
                    cited_produced_deliverable_revision_ids=[
                        refs["deliverable_rev"]
                    ],
                    reviewing_party_id=_REVIEWER_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            # Exactly one Review, created only by the explicit request.
            assert _count(engine, "Outcome_Review_Records") == 1
            assert _CANONICAL_UUID7.match(result.outcome_review_id)

            row = _review_row(engine, result.outcome_review_id)
            persisted_stance = row["attribution_stance"]
            persisted_evidence = row["attribution_evidence_reference"] or ""

            # Every Review carries an explicit attribution stance in the
            # enumerated set (Requirement 54.2).
            assert persisted_stance in _ATTRIBUTION_STANCES
            assert persisted_stance == payload["attribution_stance"]
            assert result.attribution_stance == payload["attribution_stance"]

            # Asserted / Contradicted carry a >= 1-char evidence reference
            # (Requirement 54.3 / 49.4).
            if persisted_stance in _STANCES_REQUIRING_EVIDENCE:
                assert len(persisted_evidence) >= 1
            assert persisted_evidence == result.attribution_evidence_reference
        finally:
            engine.dispose()


# Feature: fourth-walking-slice, Property 51: Output is not Outcome, re-asserted from the outcome side
@given(
    offending_key=_completion_as_outcome_field(),
    payload=_review_stance_payload(),
)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_completion_as_outcome_intent_field_is_rejected(
    offending_key: str,
    payload: dict[str, Any],
) -> None:
    """Any request field asserting a Completion satisfies the Intended
    Outcome, or aliasing a Completion as an Observed Outcome, is rejected
    with no Outcome Review row persisted (Requirement 54.4).

    The prerequisite chain is otherwise fully valid, so the prohibited
    field is the only reason the creation can fail — confirming the
    rejection is attributable to the Output-is-not-Outcome guard and not to
    a missing citation or authority."""
    with tempfile.TemporaryDirectory(prefix="prop51_alias_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            refs = _seed_full_review_prerequisites(svc, engine)

            request_attributes = {offending_key: "completion satisfies outcome"}

            with pytest.raises(OutcomeReviewValidationError) as exc_info:
                with engine.begin() as conn:
                    svc.reviews.create_outcome_review(
                        conn,
                        target_intended_outcome_revision_id=refs["intended_rev"],
                        review_outcome=payload["review_outcome"],
                        attribution_stance=payload["attribution_stance"],
                        confidence=payload["confidence"],
                        review_rationale=payload["review_rationale"],
                        attribution_evidence_reference=(
                            payload["attribution_evidence_reference"]
                        ),
                        cited_assessment_ids=[refs["assessment_id"]],
                        cited_completion_ids=[refs["completion_id"]],
                        cited_produced_deliverable_revision_ids=[
                            refs["deliverable_rev"]
                        ],
                        reviewing_party_id=_REVIEWER_PARTY_ID,
                        authority_basis=_BASIS,
                        applicable_scope=_SCOPE,
                        engine=engine,
                        request_attributes=request_attributes,
                    )

            error = exc_info.value
            assert error.failed_constraint == "prohibited_attribute"
            assert offending_key in error.prohibited_keys

            # No row persisted — the guard fires before any write.
            assert _count(engine, "Outcome_Review_Records") == 0
        finally:
            engine.dispose()
