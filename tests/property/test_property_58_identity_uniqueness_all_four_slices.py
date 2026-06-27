# Feature: fourth-walking-slice, Property 58: Identity uniqueness across all four slices
"""Property 58 — Identity uniqueness across all four slices (task 15.13).

**Property 58: Identity uniqueness across all four slices**

*For all* identifiers issued by the Identity_Service in any test session
covering all four slices, identifiers are unique across all four slices and
across every Resource kind and every Record kind, are in canonical UUIDv7
lowercase hyphenated 8-4-4-4-12 form, and embed no business metadata;
Measurement Definition and Observed Outcome each hold distinct Resource
Identity and Revision Identity with one Resource to one-or-more Revisions and
no Revision shared across Resources; the seven Slice 4 identifier roles are
disjoint from every Slice 1, Slice 2, and Slice 3 identifier; rename/relocate
of a Measurement Definition or Observed Outcome Resource preserves its
Resource Identity and every Revision Identity; and no once-assigned identifier
is reused.

**Validates: Requirements 43.1, 43.2, 43.3, 43.4, 43.6, 43.7, 43.8, 61.13**

Strategy
========

Each Hypothesis case represents one "single test session covering all four
slices" in the property's wording. Per case the test:

1. Builds a fresh per-case SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path carrying the Slice 1, Slice 2
   (Planning), Slice 3 (Execution + Deliverable_Repository), and Slice 4
   (Outcome) schemas so cross-case identifier and registry state cannot leak.
2. Constructs a *single* fresh :class:`IdentityService` shared by every Slice
   1–4 collaborator so the uniqueness guarantee is exercised through one
   Identity_Service registry across all four slices (rather than relying on
   per-slice in-memory state to coincidentally avoid collisions).
3. Drives the real Slice 2 + Slice 4 services to mint the full
   outcome-measurement pipeline against one seeded ``intended`` Intended
   Outcome Revision:

   - one Measurement Definition (Resource + Revision identities),
   - one native and one imported Measurement Record,
   - one Observed Outcome plus ``num_revisions - 1`` appended Revisions
     (one Resource to one-or-more Revisions, AD-WS-36),
   - one Success-Condition Assessment sourced from the most-recent Observed
     Outcome Revision,
   - one Outcome Review citing the Assessment, a seeded Completion Record, and
     a seeded produced Deliverable Revision.

4. Mints a batch of Slice 1 factory identifiers (``new_resource_id``,
   ``new_revision_id``, ``new_relationship_id``, ``new_region_id``,
   ``new_immutable_record_id``) from the *same* Identity_Service so the
   cross-slice union the property quantifies over spans all four slices.

Six assertion groups then hold for the case:

1. **Canonical form** — every issued identifier matches
   :data:`~walking_slice.identity.CANONICAL_UUID7_REGEX` (Requirements 43.1,
   43.2, 43.6).
2. **Uniqueness / no reuse** — no identifier is issued twice across the whole
   four-slice session, including across every Observed Outcome revise
   (Requirements 43.1, 43.3, 61.13).
3. **Opacity** — no meaningful business-attribute substring (Party Identity,
   scope value, role name, display name) appears inside any issued identifier
   (Requirement 43.7).
4. **Distinct Resource vs Revision identity** — the Measurement Definition and
   the Observed Outcome each hold a Resource Identity distinct from each of its
   Revision Identities, and every Revision belongs to exactly one Resource
   (Requirements 43.2, 43.6).
5. **Seven Slice 4 roles disjoint** — verified through
   ``Identifier_Registry.resource_kind``: every Slice 4 identifier carries one
   of the seven :data:`OUTCOME_RESOURCE_KINDS`, and that set of identifiers is
   disjoint from every identifier tagged with a non-Slice-4 ``resource_kind``
   or ``NULL`` (Requirement 43.8).
6. **Rename/relocate (revise) preserves identity** — appending each Observed
   Outcome Revision leaves the Resource Identity and every previously issued
   Revision Identity byte-equivalent and present (Requirements 43.6, 61.13).

Setup follows the conventions established by the Slice 1/2/3/4 property tests
(per-case :class:`tempfile.TemporaryDirectory` ownership of the SQLite file,
fresh services per case, :class:`FixedClock` pinned to
``2026-01-01T00:00:00.000Z``) and reuses the deterministic prerequisite-chain
style from ``tests/property/test_property_46_outcome_creation_anchoring.py``.
``@settings(max_examples=100, deadline=2000)`` per design §"Testing Strategy"
and Requirement 61.15.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Final

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
from walking_slice.identity import CANONICAL_UUID7_REGEX, IdentityService
from walking_slice.models import AuthorityBasisRef
from walking_slice.outcome._persistence import (
    OUTCOME_RESOURCE_KINDS,
    create_outcome_schema,
)
from walking_slice.outcome.measurement_definitions import (
    MeasurementDefinitionService,
)
from walking_slice.outcome.measurement_records import MeasurementRecordService
from walking_slice.outcome.observed_outcomes import ObservedOutcomeService
from walking_slice.outcome.outcome_reviews import OutcomeReviewService
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
# Property 58 asserts identity invariants over the *issued* identifiers, so
# deterministic prerequisite IDs keep shrunken counterexamples actionable; the
# pipeline shape (number of Observed Outcome Revisions, imported source
# authority) and the opacity needles are what Hypothesis draws.
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

_AUTHORITY_BASIS_ID: Final[str] = "00000000-0000-7000-8000-0000000ba001"
_BASIS: Final[AuthorityBasisRef] = AuthorityBasisRef(
    type="role-grant-id", id=_AUTHORITY_BASIS_ID
)

# Directly-seeded citable Slice 3 artifacts for the Outcome Review.
_CITABLE_COMPLETION_ID: Final[str] = "00000000-0000-7000-8000-000000d00001"
_CITABLE_WORK_ASSIGNMENT_ID: Final[str] = "00000000-0000-7000-8000-000000d00002"
_CITABLE_DELIVERABLE_ID: Final[str] = "00000000-0000-7000-8000-000000d00003"
_CITABLE_DELIVERABLE_REVISION_ID: Final[str] = (
    "00000000-0000-7000-8000-000000d00004"
)

_UNIT: Final[str] = "percent"
# ISO-8601 closed window covering the 2025 observation instants; both edges
# precede the fixed recorded time (2026-01-01).
_WINDOW_2025: Final[str] = "2025-01-01T00:00:00Z/2025-12-31T23:59:59Z"

_SOURCE_SYSTEM_AUTHORITIES: Final[tuple[str, ...]] = (
    "authoritative",
    "replica",
    "projection",
    "index",
    "federation",
)

# The 5 Slice 1 :class:`IdentityService` factory methods exercised so the
# cross-slice union the property quantifies over spans all four slices.
_SLICE1_FACTORY_METHOD_NAMES: Final[tuple[str, ...]] = (
    "new_resource_id",
    "new_revision_id",
    "new_relationship_id",
    "new_region_id",
    "new_immutable_record_id",
)
_SLICE1_IDENTIFIERS_PER_FACTORY: Final[int] = 4


# ---------------------------------------------------------------------------
# Opacity-check helpers (mirrors Property 27).
#
# A canonical UUIDv7 is composed entirely of ``[0-9a-f-]``; a short or all-hex
# business fragment can occur inside random hex by chance, producing a
# false-positive opacity violation that says nothing about Requirement 43.7.
# A needle is checked only when it is >= 4 characters long AND contains at
# least one character outside the canonical UUID alphabet.
# ---------------------------------------------------------------------------


_MIN_BUSINESS_LENGTH: Final[int] = 4
_UUID_ALPHABET: Final[frozenset[str]] = frozenset("0123456789abcdef-")


def _meaningful_needle(business_attribute: str) -> bool:
    """Return ``True`` iff *business_attribute* is worth checking for opacity."""
    if len(business_attribute) < _MIN_BUSINESS_LENGTH:
        return False
    lowered = business_attribute.lower()
    return any(ch not in _UUID_ALPHABET for ch in lowered)


# ---------------------------------------------------------------------------
# Per-case engine builder. Each Hypothesis case builds a fresh SQLite engine
# on a unique temp-dir path; the engine carries every cumulative schema.
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
# Per-case service bundle. Fresh per Hypothesis case; a *single*
# IdentityService is shared by every collaborator so the uniqueness guarantee
# is exercised through one Identity_Service registry across all four slices.
# ---------------------------------------------------------------------------


class _Services:
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
# Seed helpers (mirror tests/property/test_property_46_*.py).
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


def _seed_citable_completion(engine: Engine) -> str:
    """Seed one resolvable Completion Record by direct INSERT."""
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
                "abid": _AUTHORITY_BASIS_ID,
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
                "abid": _AUTHORITY_BASIS_ID,
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


# ---------------------------------------------------------------------------
# Probe helpers.
# ---------------------------------------------------------------------------


def _observed_outcome_revision_ids(engine: Engine, resource_id: str) -> set[str]:
    """Return every Observed Outcome Revision Identity for *resource_id*."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT observed_outcome_revision_id "
                "FROM Observed_Outcome_Revisions "
                "WHERE observed_outcome_id = :rid"
            ),
            {"rid": resource_id},
        ).all()
    return {row[0] for row in rows}


def _registry_by_outcome_kind(engine: Engine) -> tuple[set[str], set[str]]:
    """Return ``(slice4_identifiers, other_identifiers)`` from the registry.

    ``slice4_identifiers`` are the rows whose ``resource_kind`` is one of the
    seven :data:`OUTCOME_RESOURCE_KINDS`; ``other_identifiers`` are every other
    registry row (a non-Slice-4 ``resource_kind`` or ``NULL``).
    """
    slice4: set[str] = set()
    other: set[str] = set()
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT identifier, resource_kind FROM Identifier_Registry")
        ).all()
    for identifier, resource_kind in rows:
        if resource_kind in OUTCOME_RESOURCE_KINDS:
            slice4.add(identifier)
        else:
            other.add(identifier)
    return slice4, other


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# Each case draws the pipeline shape (number of Observed Outcome Revisions),
# the imported Measurement Record's source-system authority, the native and
# imported observed values, and a bundle of opacity needles checked against
# every issued identifier.
# ---------------------------------------------------------------------------


_case_strategy = st.fixed_dictionaries(
    {
        # 1..4 Observed Outcome Revisions: one create plus 0..3 revises so the
        # "one Resource to one-or-more Revisions" and "revise preserves
        # identity" clauses are exercised at the boundary and beyond.
        "num_revisions": st.integers(min_value=1, max_value=4),
        "source_authority": st.sampled_from(_SOURCE_SYSTEM_AUTHORITIES),
        # Observed values within 0..100 with <= 6 fractional digits; the unit
        # is fixed to the Definition's unit ('percent').
        "native_value": st.integers(min_value=0, max_value=100000),
        "imported_value": st.integers(min_value=0, max_value=100000),
        # Opacity needles: stand-ins for Party Identity, scope, role label, and
        # display name. The opacity check filters trivial / all-hex fragments.
        "business_attributes": st.fixed_dictionaries(
            {
                "party_id": st.text(min_size=0, max_size=64),
                "scope": st.text(min_size=0, max_size=128),
                "role_name": st.text(min_size=0, max_size=64),
                "display_name": st.text(min_size=0, max_size=64),
            }
        ),
    }
)


def _decimal_from_thousandths(value: int) -> Decimal:
    """Map an integer 0..100000 to a Decimal in 0..100 with 3 fractional
    digits (well within the 6-fractional-digit ceiling, Requirement 45.2)."""
    return (Decimal(value) / Decimal(1000)).quantize(Decimal("0.001"))


# ===========================================================================
# Property 58 — Identity uniqueness across all four slices.
# ===========================================================================


# Feature: fourth-walking-slice, Property 58: Identity uniqueness across all four slices
@given(case=_case_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_identity_uniqueness_across_all_four_slices(case: dict) -> None:
    """**Validates: Requirements 43.1, 43.2, 43.3, 43.4, 43.6, 43.7, 43.8,
    61.13**

    For every fresh four-slice session, the union of Slice 1 factory-minted
    identifiers and the Slice 2 + Slice 4 pipeline identifiers satisfies the
    six identity invariants of Property 58.
    """
    num_revisions: int = case["num_revisions"]
    source_authority: str = case["source_authority"]
    native_value = _decimal_from_thousandths(case["native_value"])
    imported_value = _decimal_from_thousandths(case["imported_value"])
    business = case["business_attributes"]

    with tempfile.TemporaryDirectory(prefix="prop58_") as raw_tmp:
        engine = _build_engine(Path(raw_tmp))
        try:
            svc = _Services()
            _seed_world(svc, engine)

            # -- Slice 2: one intended Intended Outcome Revision --------------
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
            intended_outcome_revision_id = intended.intended_outcome_revision_id

            # -- Slice 4: Measurement Definition (Resource + Revision) --------
            with engine.begin() as conn:
                definition = svc.definitions.create_measurement_definition(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    measurand_description="Adoption rate of the new workflow.",
                    unit_of_measure=_UNIT,
                    observation_window=_WINDOW_2025,
                    cadence="monthly",
                    data_source="product analytics",
                    authoring_party_id=_DEFINER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            definition_revision_id = (
                definition.measurement_definition_revision_id
            )

            # -- Slice 4: one native + one imported Measurement Record --------
            with engine.begin() as conn:
                native_record = svc.records.create_native_measurement(
                    conn,
                    target_measurement_definition_revision_id=(
                        definition_revision_id
                    ),
                    observed_value=native_value,
                    observed_value_unit=_UNIT,
                    observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                    recording_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            with engine.begin() as conn:
                imported_record = svc.records.create_imported_measurement(
                    conn,
                    target_measurement_definition_revision_id=(
                        definition_revision_id
                    ),
                    observed_value=imported_value,
                    observed_value_unit=_UNIT,
                    observation_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
                    source_system_id="metrics-warehouse",
                    source_system_record_id="row-001",
                    source_system_authority=source_authority,
                    source_system_retrieval_time=datetime(
                        2025, 7, 1, tzinfo=timezone.utc
                    ),
                    importing_party_id=_RECORDER_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            cited_record_ids = [native_record.measurement_record_id]

            # -- Slice 4: Observed Outcome create + (num_revisions-1) revises -
            with engine.begin() as conn:
                observed = svc.observed.create_observed_outcome(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    assessment_summary="Adoption trending toward the target.",
                    cited_measurement_record_ids=cited_record_ids,
                    authoring_party_id=_ASSESSOR_PARTY_ID,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )
            observed_outcome_id = observed.observed_outcome_id
            # Track the issued Revision Identities and the Resource Identity
            # across every revise. The Resource Identity must never change and
            # no prior Revision Identity may be dropped or reused.
            issued_revision_ids: list[str] = [
                observed.observed_outcome_revision_id
            ]
            current_revision_id = observed.observed_outcome_revision_id

            for revise_index in range(1, num_revisions):
                # Before each revise, the Resource still owns exactly the
                # Revisions issued so far (rename/relocate preserves identity).
                persisted_now = _observed_outcome_revision_ids(
                    engine, observed_outcome_id
                )
                assert persisted_now == set(issued_revision_ids), (
                    "Observed Outcome Revision set drifted before revise "
                    f"{revise_index}: expected {sorted(issued_revision_ids)!r}, "
                    f"found {sorted(persisted_now)!r}."
                )
                with engine.begin() as conn:
                    revised = svc.observed.revise_observed_outcome(
                        conn,
                        observed_outcome_id=observed_outcome_id,
                        predecessor_revision_id=current_revision_id,
                        assessment_summary=(
                            f"Adoption update {revise_index}."
                        ),
                        cited_measurement_record_ids=cited_record_ids,
                        authoring_party_id=_ASSESSOR_PARTY_ID,
                        applicable_scope=_SCOPE,
                        engine=engine,
                    )
                # The Resource Identity is preserved across the revise
                # (Requirement 43.6).
                assert revised.observed_outcome_id == observed_outcome_id, (
                    "revise_observed_outcome changed the Resource Identity at "
                    f"revise {revise_index}: expected {observed_outcome_id!r}, "
                    f"got {revised.observed_outcome_id!r}."
                )
                assert (
                    revised.observed_outcome_revision_id
                    not in issued_revision_ids
                ), (
                    "revise_observed_outcome reissued an existing Revision "
                    f"Identity {revised.observed_outcome_revision_id!r} at "
                    f"revise {revise_index}."
                )
                issued_revision_ids.append(revised.observed_outcome_revision_id)
                current_revision_id = revised.observed_outcome_revision_id

            # -- Slice 4: Success-Condition Assessment ------------------------
            with engine.begin() as conn:
                assessment = svc.assessments.create_assessment(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    sourced_observed_outcome_revision_id=current_revision_id,
                    assessment_category="Satisfied",
                    assessment_rationale=(
                        "Measured adoption met the success threshold."
                    ),
                    assessing_party_id=_ASSESSOR_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            # -- Slice 4: Outcome Review (cites Slice 3 artifacts) ------------
            completion_id = _seed_citable_completion(engine)
            deliverable_revision_id = _seed_citable_deliverable_revision(engine)
            with engine.begin() as conn:
                review = svc.reviews.create_outcome_review(
                    conn,
                    target_intended_outcome_revision_id=(
                        intended_outcome_revision_id
                    ),
                    review_outcome="Achieved",
                    attribution_stance="Asserted",
                    confidence="High",
                    review_rationale="The Intended Outcome was achieved.",
                    attribution_evidence_reference=(
                        "Adoption telemetry and the cited completion."
                    ),
                    cited_assessment_ids=[assessment.assessment_id],
                    cited_completion_ids=[completion_id],
                    cited_produced_deliverable_revision_ids=[
                        deliverable_revision_id
                    ],
                    reviewing_party_id=_REVIEWER_PARTY_ID,
                    authority_basis=_BASIS,
                    applicable_scope=_SCOPE,
                    engine=engine,
                )

            # -- Slice 1 factory identifiers (same Identity_Service) ----------
            slice1_identifiers: list[str] = []
            for method_name in _SLICE1_FACTORY_METHOD_NAMES:
                factory = getattr(svc.identity, method_name)
                for _ in range(_SLICE1_IDENTIFIERS_PER_FACTORY):
                    slice1_identifiers.append(factory())

            # ----------------------------------------------------------------
            # Assemble the full set of issued identifiers across all four
            # slices for the uniqueness / canonical-form / opacity checks.
            # ----------------------------------------------------------------
            slice4_definition_ids = [
                definition.measurement_definition_id,
                definition_revision_id,
            ]
            slice4_record_ids = [
                native_record.measurement_record_id,
                imported_record.measurement_record_id,
            ]
            slice4_observed_ids = [observed_outcome_id, *issued_revision_ids]
            slice4_other_ids = [
                assessment.assessment_id,
                review.outcome_review_id,
            ]
            slice2_ids = [
                intended.intended_outcome_id,
                intended_outcome_revision_id,
            ]

            all_identifiers: list[str] = (
                slice1_identifiers
                + slice2_ids
                + slice4_definition_ids
                + slice4_record_ids
                + slice4_observed_ids
                + slice4_other_ids
            )

            # --- 1. Canonical form (Requirements 43.1, 43.2, 43.6) ----------
            for identifier in all_identifiers:
                assert (
                    CANONICAL_UUID7_REGEX.match(identifier) is not None
                ), (
                    f"Issued identifier {identifier!r} does not match "
                    f"canonical UUIDv7 form {CANONICAL_UUID7_REGEX.pattern!r}."
                )

            # --- 2. Uniqueness / no reuse (Requirements 43.1, 43.3, 61.13) --
            distinct = set(all_identifiers)
            assert len(distinct) == len(all_identifiers), (
                "Identity_Service reissued an identifier within a single "
                "four-slice session: "
                f"total={len(all_identifiers)}, distinct={len(distinct)}."
            )

            # --- 3. Opacity (Requirement 43.7) ------------------------------
            needles = [
                attr.lower()
                for attr in (
                    business["party_id"],
                    business["scope"],
                    business["role_name"],
                    business["display_name"],
                )
                if _meaningful_needle(attr)
            ]
            for identifier in all_identifiers:
                lowered = identifier.lower()
                for needle in needles:
                    assert needle not in lowered, (
                        f"Identifier {identifier!r} embeds business-attribute "
                        f"substring {needle!r}; identifiers must not encode "
                        "Party Identity, scope value, role name, or display "
                        "name (Requirement 43.7)."
                    )

            # --- 4. Distinct Resource vs Revision identity (43.2, 43.6) -----
            assert (
                definition.measurement_definition_id != definition_revision_id
            ), (
                "Measurement Definition Resource Identity equals its Revision "
                "Identity; the two must be distinct (Requirement 43.2)."
            )
            assert observed_outcome_id not in issued_revision_ids, (
                "Observed Outcome Resource Identity equals one of its Revision "
                "Identities; the two must be distinct (Requirement 43.2)."
            )
            assert len(set(issued_revision_ids)) == len(issued_revision_ids), (
                "Observed Outcome Revision Identities are not all distinct: "
                f"{issued_revision_ids!r}."
            )

            # The Observed Outcome Resource owns exactly the issued Revisions
            # (one Resource to one-or-more Revisions; no Revision shared across
            # Resources, Requirement 43.6).
            persisted_revisions = _observed_outcome_revision_ids(
                engine, observed_outcome_id
            )
            assert persisted_revisions == set(issued_revision_ids), (
                "Observed Outcome Revision set differs from the issued set: "
                f"persisted {sorted(persisted_revisions)!r} vs issued "
                f"{sorted(issued_revision_ids)!r}."
            )
            assert len(persisted_revisions) == num_revisions, (
                "Observed Outcome owns the wrong number of Revisions: "
                f"expected {num_revisions}, found {len(persisted_revisions)}."
            )
            # No other Observed Outcome Resource exists owning any of these
            # Revisions (no Revision shared across Resources).
            with engine.connect() as conn:
                owning_resources = {
                    row[0]
                    for row in conn.execute(
                        text(
                            "SELECT DISTINCT observed_outcome_id "
                            "FROM Observed_Outcome_Revisions"
                        )
                    ).all()
                }
            assert owning_resources == {observed_outcome_id}, (
                "Observed Outcome Revisions are spread across more than one "
                f"Resource: {sorted(owning_resources)!r}."
            )

            # --- 5. Seven Slice 4 roles disjoint (Requirement 43.8) ---------
            slice4_registry, other_registry = _registry_by_outcome_kind(engine)
            overlap = slice4_registry & other_registry
            assert overlap == set(), (
                "Slice 4 identifier roles overlap non-Slice-4 identifiers in "
                f"Identifier_Registry: {sorted(overlap)!r} (Requirement 43.8)."
            )
            # Every Slice 4 identifier the pipeline returned is tagged with one
            # of the seven outcome resource_kinds.
            expected_slice4 = set(
                slice4_definition_ids
                + slice4_record_ids
                + slice4_observed_ids
                + slice4_other_ids
            )
            missing_tags = expected_slice4 - slice4_registry
            assert missing_tags == set(), (
                "Slice 4 identifiers missing an OUTCOME_RESOURCE_KINDS tag in "
                f"Identifier_Registry: {sorted(missing_tags)!r} "
                "(Requirement 43.8)."
            )
            # The Slice 4 registry rows must be disjoint from the Slice 2
            # intended-outcome identifiers (a concrete prior-slice witness).
            assert not (set(slice2_ids) & slice4_registry), (
                "A Slice 2 intended-outcome identifier is tagged as a Slice 4 "
                "resource_kind (Requirement 43.8)."
            )
        finally:
            engine.dispose()
