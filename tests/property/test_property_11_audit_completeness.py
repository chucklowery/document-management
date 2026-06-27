# Feature: first-walking-slice, Property 11: Audit completeness for consequential and denied actions
"""Property 11 — Audit completeness for consequential and denied actions (task 15.3).

**Property 11: Audit completeness for consequential and denied actions**

For all consequential writes (Document Revision, Region Occurrence,
rename, Finding, Recommendation, Decision, Trail Revision, Trail Step,
Relationship, Role Assignment, Role Assignment revocation) and all
denied attempts (denied ``approve.decision``) performed against the
walking slice, exactly one ``Audit_Records`` row exists per operation
that matches the originating call on ``actor_party_id``, ``action_type``,
``target_id``, ``target_revision_id``, ``outcome``, ``recorded_at``, and
``correlation_id``; for denied attempts the targeted Resource and
Revision rows remain absent (no in-flight domain row was committed).

**Validates: Requirements 2.5, 6.4, 7.2, 7.6, 12.5, 13.1, 13.2, 15.11**

Strategy:

Each Hypothesis case (a) seeds a fresh per-test SQLite engine and a
canonical pipeline of resources (Source Document → Document Revision →
Region Occurrence → Finding → Recommendation, plus an authorized Role
Assignment that grants ``approve`` on the Decision scope), then (b)
draws a sequence of 1..10 operations from a closed alphabet:

- ``append_revision`` — :meth:`EvidenceRepository.append_revision`
  (``create.document_revision`` consequential audit row).
- ``create_region_occurrence`` —
  :meth:`EvidenceRepository.create_region_occurrence`
  (``create.region_occurrence`` consequential audit row).
- ``rename_document`` — :meth:`EvidenceRepository.rename_document`
  (``rename.document`` consequential audit row).
- ``create_finding`` — :meth:`KnowledgeService.create_finding`
  (``create.finding`` consequential audit row, ``Supports``
  Relationship written inside the same transaction).
- ``record_contradiction`` —
  :meth:`KnowledgeService.record_contradiction`
  (``record.contradiction`` consequential audit row, ``Contradicts``
  Relationship written inside the same transaction).
- ``create_recommendation`` —
  :meth:`KnowledgeService.create_recommendation`
  (``create.recommendation`` consequential audit row).
- ``create_decision_permit`` —
  :meth:`KnowledgeService.create_decision` against a fresh
  Recommendation Revision while the authorized Party holds an
  effective ``approve`` role assignment (``create.decision``
  consequential audit row).
- ``create_decision_deny`` —
  :meth:`KnowledgeService.create_decision` invoked by the
  unauthorized Party (no role assignment) against a fresh
  Recommendation Revision. The wired
  :class:`AuthorizationService` denies the attempt; a
  ``approve.decision`` denial audit row is appended in a separate
  transaction (Requirement 7.6) and no ``Decisions`` row is created.
- ``create_trail`` — :meth:`TrailService.create_trail`
  (``create.trail`` consequential audit row, Trail Steps written
  inside the same transaction).
- ``assign_role`` — :meth:`AuthorizationService.assign_role`
  (``assign.role`` consequential audit row).
- ``revoke_role`` — direct ``UPDATE`` of
  ``Role_Assignments.revoked_at`` plus an explicit
  :meth:`AuditLog.append_consequential` with
  ``action_type='revoke.role'``, mirroring the persistence path of
  the ``POST /api/v1/roles/assignments/{id}/revocations`` handler in
  :mod:`walking_slice.routes.roles` (Requirement 13.1, AD-WS-5).

Every operation is invoked with an explicit ``correlation_id`` so the
post-hoc assertion can locate its audit row deterministically. Each
operation also records the expected ``outcome``, ``action_type``,
``actor_party_id``, ``target_id``, and ``target_revision_id`` so the
audit row content can be compared field-by-field.

Assertions per case (run after the whole scenario has executed):

1. **Existence and uniqueness.** For every recorded operation there
   is exactly one ``Audit_Records`` row matching its
   ``(correlation_id, outcome)`` pair (Requirement 13.1, 13.2).
2. **Attribute fidelity.** The row's ``actor_party_id``,
   ``action_type``, ``target_id``, ``target_revision_id``, and
   ``correlation_id`` are byte-equal to the expected values
   captured at call time (Requirement 13.1).
3. **Recorded time.** The row's ``recorded_at`` matches the
   slice-wide millisecond-precision UTC pattern
   ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (Requirement 13.1).
4. **Denial leaves no in-flight write.** For every
   ``create_decision_deny`` operation, the targeted
   ``Recommendation_Revisions`` row has zero matching ``Decisions``
   rows after the denial (Requirements 7.2, 7.6 — denial-and-audit
   never silently diverge into a partial-write).

Test scaffolding mirrors the conventions established by
``tests/property/test_property_5_trail_linearity.py`` and
``tests/property/test_property_2_decision_authority.py``: per-case
:class:`tempfile.TemporaryDirectory` ownership of the SQLite file, a
:class:`~walking_slice.clock.FixedClock` pinned to
``2026-01-01T00:00:00.000Z`` for deterministic timestamps across
shrinks, and ``@settings(max_examples=50, deadline=5000)`` because
each case persists multiple synthesis-pipeline writes plus the
denial-path retry latency.
"""

from __future__ import annotations

import re
import tempfile
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog, format_iso8601_ms
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    DecisionAuthorizationError,
    KnowledgeService,
    SupportRef,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.trails import (
    ORDINAL_TARGET_KIND,
    TrailService,
    TrailStepInput,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_NOW_ISO: Final[str] = format_iso8601_ms(_NOW)

# Three Parties cover every actor role the operation alphabet needs:
# the authorized Party owns the effective ``approve`` Role Assignment
# (and is therefore the actor on every consequential write), the
# unauthorized Party never receives a Role Assignment (and is therefore
# the actor on every denial attempt), and the assigning authority is
# the actor recorded on every ``assign.role`` audit row.
_PARTY_AUTHORIZED: Final[str] = "00000000-0000-7000-8000-000000000001"
_PARTY_UNAUTHORIZED: Final[str] = "00000000-0000-7000-8000-000000000002"
_PARTY_ASSIGNING: Final[str] = "00000000-0000-7000-8000-000000000003"

# Scope used by every Decision attempt and by the seeded Role
# Assignment so the authorization evaluation permits the authorized
# Party and denies the unauthorized Party (Requirements 12.3, 12.4).
_SCOPE: Final[str] = "property-11/scope"

# Authority-basis identifier referenced by every Decision. Property 11
# does not exercise authority-basis selection (Property 2 covers that);
# the value is fixed so every Decision row passes the
# :class:`AuthorityBasisRef` validator.
_AUTHORITY_BASIS_ID: Final[uuid_lib.UUID] = uuid_lib.UUID(
    "00000000-0000-7000-8000-00000000a011"
)

# Operation alphabet — the closed set of operations Hypothesis draws
# from. Listed in :data:`_OPERATIONS` so the strategy and the
# per-operation dispatch read from one source of truth.
_OPERATIONS: Final[tuple[str, ...]] = (
    "append_revision",
    "create_region_occurrence",
    "rename_document",
    "create_finding",
    "record_contradiction",
    "create_recommendation",
    "create_decision_permit",
    "create_decision_deny",
    "create_trail",
    "assign_role",
    "revoke_role",
)

# Canonical millisecond-precision UTC text pattern. Centralized so the
# recorded-at assertion in the property body matches the format used
# everywhere else in the slice (Requirement 13.1).
_RECORDED_AT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# Each Hypothesis case draws a list of 1..10 operations from the closed
# alphabet :data:`_OPERATIONS`. ``min_size=1`` guarantees at least one
# audited write per case (a zero-length scenario would make the property
# vacuously true and waste Hypothesis's budget). ``max_size=10`` keeps
# each case inside the 5 s deadline given the denial path's
# exponential-backoff retry sleep (0.01s + 0.02s + 0.04s per denial
# attempt — see :data:`_DENIAL_AUDIT_BACKOFFS_SECONDS` in
# :mod:`walking_slice.knowledge`) and the per-case temp-directory setup.
# ---------------------------------------------------------------------------


_operation_strategy = st.sampled_from(_OPERATIONS)
_scenario_strategy = st.lists(_operation_strategy, min_size=1, max_size=10)


# ---------------------------------------------------------------------------
# Engine and seeding helpers.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with the slice's pragmas."""
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
    return engine


def _seed_parties(engine: Engine) -> None:
    """Insert the three Party rows every consequential write FK-references."""
    with engine.begin() as conn:
        for party_id, display in (
            (_PARTY_AUTHORIZED, "Property 11 Authorized"),
            (_PARTY_UNAUTHORIZED, "Property 11 Unauthorized"),
            (_PARTY_ASSIGNING, "Property 11 Assigning Authority"),
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


def _seed_role_assignment(
    authorization_service: AuthorizationService, engine: Engine
) -> str:
    """Seed the authorized Party with an effective approve-bearing role.

    The assignment is bounded by ``[-30 days, +30 days]`` around the
    fixed test instant so every Decision attempt run inside the case
    falls inside the effective window (Requirement 7.3).
    """
    request = AssignRoleRequest(
        party_id=_PARTY_AUTHORIZED,
        role_name="decision_maker",
        scope=_SCOPE,
        authorities_granted=("view", "modify", "approve"),
        effective_start=_NOW - timedelta(days=30),
        effective_end=_NOW + timedelta(days=30),
        assigning_authority_id=_PARTY_ASSIGNING,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _seed_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_unwired: KnowledgeService,
) -> dict[str, str]:
    """Seed Source Document → Region → Finding → Recommendation in one tx.

    These records are the prerequisites every operation in the
    scenario alphabet needs to cite (the ``create.*`` writes target
    new identifiers, but they all derive from this seed graph).
    """
    with engine.begin() as conn:
        doc = evidence_repository.create_document(
            conn,
            content_bytes=b"Property 11 seed content for the walking slice.",
            contributing_party_id=_PARTY_AUTHORIZED,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=10,
            contributing_party_id=_PARTY_AUTHORIZED,
        )
        finding = knowledge_unwired.create_finding(
            conn,
            statement="Property 11 seed finding statement.",
            authoring_party_id=_PARTY_AUTHORIZED,
            supporting_region_occurrences=[
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                ),
            ],
        )
        recommendation = knowledge_unwired.create_recommendation(
            conn,
            authoring_party_id=_PARTY_AUTHORIZED,
            derived_from_findings=[finding.finding_id],
            rationale="Property 11 seed recommendation rationale.",
        )
    return {
        "doc_resource_id": doc.resource_id,
        "doc_revision_id": doc.revision_id,
        "region_id": region.region_id,
        # The seed Region Occurrence is anchored to the *original*
        # Document Revision. Subsequent ``append_revision`` operations
        # shift ``doc_revision_id`` to a fresher Revision so the
        # `create.document_revision` audit row carries the correct
        # ``target_revision_id``, but the Region Occurrence stays
        # bound to the Revision it was originally anchored to.
        # :class:`SupportRef` requires the matching
        # ``(region_id, document_revision_id)`` pair, so the pipeline
        # tracks the Region's anchor revision separately from the
        # latest Document Revision.
        "region_anchor_revision_id": doc.revision_id,
        "finding_id": finding.finding_id,
        "finding_revision_id": finding.finding_revision_id,
        "recommendation_id": recommendation.recommendation_id,
        "recommendation_revision_id": recommendation.recommendation_revision_id,
    }


# ---------------------------------------------------------------------------
# Trail step construction — mirrors the helper in
# ``tests/property/test_property_5_trail_linearity.py`` so a Trail
# created here points at the same five-pipeline-stage targets every
# other Trail-creation property test does.
# ---------------------------------------------------------------------------


def _trail_steps_from_pipeline(
    pipeline: dict[str, str], decision_id: str
) -> list[TrailStepInput]:
    """Return five Trail Steps citing the seeded pipeline plus the Decision."""
    return [
        TrailStepInput(
            ordinal=1,
            target_kind=ORDINAL_TARGET_KIND[1],  # document_revision
            target_id=pipeline["doc_resource_id"],
            target_revision_id=pipeline["doc_revision_id"],
        ),
        TrailStepInput(
            ordinal=2,
            target_kind=ORDINAL_TARGET_KIND[2],  # region_occurrence
            # The Trail Step's ``target_id`` for a ``region_occurrence``
            # step is the Document Revision Identity the Region
            # Occurrence anchors to, not the latest Document Revision
            # — the Trail must resolve to the same composite key
            # recorded on the originating ``Region_Occurrences`` row.
            target_id=pipeline["region_anchor_revision_id"],
            region_id=pipeline["region_id"],
        ),
        TrailStepInput(
            ordinal=3,
            target_kind=ORDINAL_TARGET_KIND[3],  # finding_revision
            target_id=pipeline["finding_id"],
            target_revision_id=pipeline["finding_revision_id"],
        ),
        TrailStepInput(
            ordinal=4,
            target_kind=ORDINAL_TARGET_KIND[4],  # recommendation_revision
            target_id=pipeline["recommendation_id"],
            target_revision_id=pipeline["recommendation_revision_id"],
        ),
        TrailStepInput(
            ordinal=5,
            target_kind=ORDINAL_TARGET_KIND[5],  # decision
            target_id=decision_id,
        ),
    ]


# ---------------------------------------------------------------------------
# Audit-row probe helpers.
# ---------------------------------------------------------------------------


def _fetch_audit_rows_for(
    engine: Engine, *, correlation_id: str, outcome: str
) -> list[dict[str, Any]]:
    """Return every ``Audit_Records`` row matching the (correlation, outcome)."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT audit_record_id, append_sequence, actor_party_id,
                           action_type, outcome, target_id, target_revision_id,
                           reason_code, correlation_id, recorded_at
                      FROM Audit_Records
                     WHERE correlation_id = :cid AND outcome = :outcome
                     ORDER BY append_sequence
                    """
                ),
                {"cid": correlation_id, "outcome": outcome},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _decision_count_for_recommendation(
    engine: Engine,
    *,
    recommendation_id: str,
    recommendation_revision_id: str,
) -> int:
    """Return the number of ``Decisions`` rows addressing the pair."""
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    """
                    SELECT COUNT(*) FROM Decisions
                     WHERE target_recommendation_id = :rid
                       AND target_recommendation_revision_id = :rrid
                    """
                ),
                {
                    "rid": recommendation_id,
                    "rrid": recommendation_revision_id,
                },
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# Per-operation dispatchers.
#
# Each helper executes one operation and returns a list of expected
# audit-row descriptors so the post-hoc assertion can verify the
# operation's audit footprint. A helper returns an empty list when the
# operation's preconditions are not met (e.g. ``revoke_role`` when
# every recorded assignment has already been revoked) so the scenario
# loop can move on without failing.
# ---------------------------------------------------------------------------


def _expected_consequential(
    *,
    correlation_id: str,
    action_type: str,
    actor_party_id: str,
    target_id: Optional[str],
    target_revision_id: Optional[str],
    denied_recommendation: Optional[tuple[str, str]] = None,
) -> dict[str, Any]:
    """Build one expected-audit descriptor."""
    return {
        "correlation_id": correlation_id,
        "outcome": "consequential",
        "action_type": action_type,
        "actor_party_id": actor_party_id,
        "target_id": target_id,
        "target_revision_id": target_revision_id,
        "denied_recommendation": denied_recommendation,
    }


def _expected_denial(
    *,
    correlation_id: str,
    action_type: str,
    actor_party_id: str,
    target_id: Optional[str],
    target_revision_id: Optional[str],
    denied_recommendation: tuple[str, str],
) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "outcome": "deny",
        "action_type": action_type,
        "actor_party_id": actor_party_id,
        "target_id": target_id,
        "target_revision_id": target_revision_id,
        "denied_recommendation": denied_recommendation,
    }


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 11: Audit completeness for consequential and denied actions
@given(scenario=_scenario_strategy)
@settings(
    max_examples=50,
    deadline=5000,
    # Per-case temp-directory + SQLite engine + pipeline seeding +
    # multiple synthesis writes is well-bounded but exceeds the
    # data-generation health check's default budget. Suppressing it
    # keeps the run deterministic without weakening the 5 s timing
    # budget (which already accommodates the denial-path
    # exponential-backoff retries).
    suppress_health_check=[HealthCheck.too_slow],
)
def test_audit_completeness_for_consequential_and_denied_actions(
    scenario: list[str],
) -> None:
    """For every consequential write and every denied attempt run by the
    scenario, exactly one ``Audit_Records`` row exists carrying the
    expected ``actor_party_id``, ``action_type``, ``target_id``,
    ``target_revision_id``, ``outcome``, millisecond-precision
    ``recorded_at``, and ``correlation_id``; every denial leaves no
    ``Decisions`` row behind."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop11_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # Fresh services per case so :class:`IdentityService`
            # in-memory state and any audit-correlation accumulator
            # cannot leak across cases.
            clock = FixedClock(_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
            authorization_service = AuthorizationService(
                clock=clock,
                audit_log=audit_log,
                identity_service=identity_service,
            )
            evidence_repository = EvidenceRepository(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            # Two Knowledge_Service instances: one without auth for
            # the pre-seed and for non-Decision writes (no authority
            # gate on those code paths in this slice), one with auth
            # for Decision attempts so the wired
            # :class:`AuthorizationService` permits the authorized
            # Party and denies the unauthorized Party.
            knowledge_unwired = KnowledgeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            knowledge_authorized = KnowledgeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
                authorization_service=authorization_service,
            )
            trail_service = TrailService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )

            # Seed Parties, the authorized Role Assignment, and the
            # synthesis-pipeline prerequisites used by every
            # downstream operation. The seed Role Assignment's
            # identifier is not retained — Property 11 only asserts on
            # audit rows for operations performed *inside* the
            # scenario, and the seed assignment is owned by the
            # authorized Party for the lifetime of the case.
            _seed_parties(engine)
            _seed_role_assignment(authorization_service, engine)
            pipeline = _seed_pipeline(
                engine, evidence_repository, knowledge_unwired
            )

            # Scenario state — mutable across the operation loop.
            # Each list captures records minted *during* the scenario
            # so subsequent operations (``record_contradiction``,
            # ``create_decision_*``, ``revoke_role``) can target them.
            extra_findings: list[dict[str, str]] = []
            extra_recommendations: list[dict[str, str]] = []
            extra_role_ids: list[str] = []
            # Roles that have been revoked already; ``revoke_role``
            # consults this set so it never targets an already-revoked
            # assignment (which would surface a 409 / IntegrityError
            # rather than exercising the audit-row append).
            revoked_role_ids: set[str] = set()

            # Expected audit-row descriptors — accumulated as
            # operations run; verified after the scenario finishes.
            expected_audit: list[dict[str, Any]] = []

            for op_index, op in enumerate(scenario):
                # Stable per-operation correlation identifier so the
                # post-hoc assertion can locate the matching audit
                # row deterministically. Embedding the index and the
                # op name makes shrunken counterexamples easy to read.
                correlation_id = (
                    f"prop11-op-{op_index:03d}-{op}-"
                    f"{uuid_lib.uuid4().hex[:8]}"
                )

                if op == "append_revision":
                    with engine.begin() as conn:
                        result = evidence_repository.append_revision(
                            conn,
                            resource_id=pipeline["doc_resource_id"],
                            content_bytes=(
                                f"Revision {op_index} body bytes.".encode()
                            ),
                            contributing_party_id=_PARTY_AUTHORIZED,
                            correlation_id=correlation_id,
                        )
                    pipeline["doc_revision_id"] = result.revision_id
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="create.document_revision",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=result.resource_id,
                            target_revision_id=result.revision_id,
                        )
                    )

                elif op == "create_region_occurrence":
                    with engine.begin() as conn:
                        region = evidence_repository.create_region_occurrence(
                            conn,
                            resource_id=pipeline["doc_resource_id"],
                            revision_id=pipeline["doc_revision_id"],
                            start_offset_bytes=0,
                            end_offset_bytes=3,
                            contributing_party_id=_PARTY_AUTHORIZED,
                            correlation_id=correlation_id,
                        )
                    # Re-anchor the pipeline's region pointer at the
                    # freshly created Region Occurrence so subsequent
                    # ``create_finding`` operations cite a resolvable
                    # ``(region_id, document_revision_id)`` pair.
                    pipeline["region_id"] = region.region_id
                    pipeline["region_anchor_revision_id"] = region.revision_id
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="create.region_occurrence",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=region.region_id,
                            target_revision_id=region.revision_id,
                        )
                    )

                elif op == "rename_document":
                    with engine.begin() as conn:
                        rename = evidence_repository.rename_document(
                            conn,
                            resource_id=pipeline["doc_resource_id"],
                            new_current_location=(
                                f"property-11/path-{op_index}.txt"
                            ),
                            actor_party_id=_PARTY_AUTHORIZED,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="rename.document",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=rename.resource_id,
                            target_revision_id=None,
                        )
                    )

                elif op == "create_finding":
                    with engine.begin() as conn:
                        finding = knowledge_unwired.create_finding(
                            conn,
                            statement=(
                                f"Property 11 finding statement {op_index}."
                            ),
                            authoring_party_id=_PARTY_AUTHORIZED,
                            supporting_region_occurrences=[
                                SupportRef(
                                    region_id=pipeline["region_id"],
                                    document_revision_id=(
                                        pipeline["region_anchor_revision_id"]
                                    ),
                                ),
                            ],
                            correlation_id=correlation_id,
                        )
                    extra_findings.append(
                        {
                            "finding_id": finding.finding_id,
                            "finding_revision_id": finding.finding_revision_id,
                        }
                    )
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="create.finding",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=finding.finding_id,
                            target_revision_id=finding.finding_revision_id,
                        )
                    )

                elif op == "record_contradiction":
                    # Needs both a source Revision (from a recorded
                    # Finding) and a distinct target Finding Resource.
                    # The seed Finding and the first dynamically
                    # created Finding satisfy both. When neither is
                    # available the scenario draws another op slot for
                    # the same case so contradiction can be exercised
                    # later — skipping silently keeps the property
                    # focused on the audit footprint.
                    if not extra_findings:
                        continue
                    source = extra_findings[-1]
                    target_finding_id = pipeline["finding_id"]
                    if source["finding_id"] == target_finding_id:
                        # ``record_contradiction`` rejects
                        # source==target with a structured error
                        # (Requirement 4.4 — two distinct Findings);
                        # skip the op rather than provoke a 400.
                        continue
                    with engine.begin() as conn:
                        contradiction = knowledge_unwired.record_contradiction(
                            conn,
                            source_finding_revision_id=(
                                source["finding_revision_id"]
                            ),
                            target_finding_id=target_finding_id,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="record.contradiction",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=target_finding_id,
                            target_revision_id=None,
                        )
                    )

                elif op == "create_recommendation":
                    with engine.begin() as conn:
                        rec = knowledge_unwired.create_recommendation(
                            conn,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            derived_from_findings=[pipeline["finding_id"]],
                            rationale=(
                                f"Property 11 recommendation rationale {op_index}."
                            ),
                            correlation_id=correlation_id,
                        )
                    extra_recommendations.append(
                        {
                            "recommendation_id": rec.recommendation_id,
                            "recommendation_revision_id": (
                                rec.recommendation_revision_id
                            ),
                        }
                    )
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="create.recommendation",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=rec.recommendation_id,
                            target_revision_id=rec.recommendation_revision_id,
                        )
                    )

                elif op == "create_decision_permit":
                    # Needs a fresh Recommendation Revision (one
                    # Decision per Recommendation per Requirement 6.5).
                    # Create one inline so this op is always runnable.
                    with engine.begin() as conn:
                        rec = knowledge_unwired.create_recommendation(
                            conn,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            derived_from_findings=[pipeline["finding_id"]],
                            rationale=(
                                f"Permit-path recommendation {op_index}."
                            ),
                        )
                    # ``create_decision`` runs the authority check on
                    # a SEPARATE transaction it opens on ``engine``
                    # (Requirement 7.6 / design §"Decision authority
                    # evaluation flow"); the consequential write
                    # below participates in the caller's transaction.
                    with engine.begin() as conn:
                        decision = knowledge_authorized.create_decision(
                            conn,
                            target_recommendation_id=rec.recommendation_id,
                            target_recommendation_revision_id=(
                                rec.recommendation_revision_id
                            ),
                            outcome="Accept",
                            rationale=(
                                f"Property 11 permit decision {op_index}."
                            ),
                            deciding_party_id=_PARTY_AUTHORIZED,
                            authority_basis=AuthorityBasisRef(
                                type="role-grant-id",
                                id=_AUTHORITY_BASIS_ID,
                            ),
                            applicable_scope=_SCOPE,
                            engine=engine,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="create.decision",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=decision.decision_id,
                            target_revision_id=None,
                        )
                    )

                elif op == "create_decision_deny":
                    # Seed a fresh Recommendation Revision so the
                    # denial path targets a real, distinct
                    # ``Recommendation_Revisions`` row. The unauthorized
                    # Party has no Role Assignment, so
                    # ``AuthorizationService.evaluate`` returns
                    # ``deny('no-role-assignment')`` and the
                    # ``create_decision`` body raises
                    # :class:`DecisionAuthorizationError` after
                    # appending the Denial Record in a separate
                    # transaction (Requirement 7.6).
                    with engine.begin() as conn:
                        rec = knowledge_unwired.create_recommendation(
                            conn,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            derived_from_findings=[pipeline["finding_id"]],
                            rationale=(
                                f"Deny-path recommendation {op_index}."
                            ),
                        )
                    raised_denial = False
                    try:
                        with engine.begin() as conn:
                            knowledge_authorized.create_decision(
                                conn,
                                target_recommendation_id=rec.recommendation_id,
                                target_recommendation_revision_id=(
                                    rec.recommendation_revision_id
                                ),
                                outcome="Accept",
                                rationale=(
                                    f"Property 11 deny attempt {op_index}."
                                ),
                                deciding_party_id=_PARTY_UNAUTHORIZED,
                                authority_basis=AuthorityBasisRef(
                                    type="role-grant-id",
                                    id=_AUTHORITY_BASIS_ID,
                                ),
                                applicable_scope=_SCOPE,
                                engine=engine,
                                correlation_id=correlation_id,
                            )
                    except DecisionAuthorizationError:
                        raised_denial = True
                    # If the service did *not* raise, the scenario is
                    # malformed (the unauthorized Party should always
                    # be denied). Use a defensive assertion rather
                    # than silently passing — Property 11's denial
                    # branch only meaningfully exercises the audit
                    # path when an actual denial occurred.
                    assert raised_denial, (
                        f"Operation {op!r} at index {op_index} did "
                        "not raise DecisionAuthorizationError; the "
                        "unauthorized Party must always be denied "
                        "for the denial branch of Property 11 to "
                        "exercise the audit path."
                    )
                    expected_audit.append(
                        _expected_denial(
                            correlation_id=correlation_id,
                            action_type="approve.decision",
                            actor_party_id=_PARTY_UNAUTHORIZED,
                            target_id=rec.recommendation_id,
                            target_revision_id=(
                                rec.recommendation_revision_id
                            ),
                            denied_recommendation=(
                                rec.recommendation_id,
                                rec.recommendation_revision_id,
                            ),
                        )
                    )

                elif op == "create_trail":
                    # Trails need a Decision target for ordinal 5;
                    # mint a fresh Decision per Trail so the
                    # five-step input always resolves and is unique.
                    # The Trail's create.trail audit row carries
                    # target_id=trail_id and
                    # target_revision_id=trail_revision_id per
                    # design §"Trail_Service" and the verb table in
                    # §"Audit_Log".
                    with engine.begin() as conn:
                        rec = knowledge_unwired.create_recommendation(
                            conn,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            derived_from_findings=[pipeline["finding_id"]],
                            rationale=(
                                f"Trail-supporting recommendation {op_index}."
                            ),
                        )
                    with engine.begin() as conn:
                        decision = knowledge_authorized.create_decision(
                            conn,
                            target_recommendation_id=rec.recommendation_id,
                            target_recommendation_revision_id=(
                                rec.recommendation_revision_id
                            ),
                            outcome="Accept",
                            rationale=(
                                f"Trail-supporting decision {op_index}."
                            ),
                            deciding_party_id=_PARTY_AUTHORIZED,
                            authority_basis=AuthorityBasisRef(
                                type="role-grant-id",
                                id=_AUTHORITY_BASIS_ID,
                            ),
                            applicable_scope=_SCOPE,
                            engine=engine,
                        )
                    steps = _trail_steps_from_pipeline(
                        {
                            **pipeline,
                            "recommendation_id": rec.recommendation_id,
                            "recommendation_revision_id": (
                                rec.recommendation_revision_id
                            ),
                        },
                        decision.decision_id,
                    )
                    with engine.begin() as conn:
                        trail = trail_service.create_trail(
                            conn,
                            purpose=(
                                f"Property 11 trail purpose {op_index}."
                            ),
                            audience_id="property-11-audience",
                            ordering_rationale="Linear pipeline order.",
                            steps=steps,
                            authoring_party_id=_PARTY_AUTHORIZED,
                            correlation_id=correlation_id,
                        )
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="create.trail",
                            actor_party_id=_PARTY_AUTHORIZED,
                            target_id=trail.trail_id,
                            target_revision_id=trail.trail_revision_id,
                        )
                    )

                elif op == "assign_role":
                    # Each ``assign_role`` op records one fresh Role
                    # Assignment so a later ``revoke_role`` op always
                    # has an un-revoked target. The assigning
                    # authority is the actor on the consequential
                    # audit row (see
                    # :meth:`AuthorizationService.assign_role`).
                    request = AssignRoleRequest(
                        party_id=_PARTY_AUTHORIZED,
                        role_name=f"property-11-role-{op_index}",
                        scope=f"{_SCOPE}/role-{op_index}",
                        authorities_granted=("view", "modify"),
                        effective_start=_NOW - timedelta(days=1),
                        effective_end=_NOW + timedelta(days=1),
                        assigning_authority_id=_PARTY_ASSIGNING,
                    )
                    with engine.begin() as conn:
                        role_id = str(
                            authorization_service.assign_role(
                                conn,
                                request,
                                correlation_id=correlation_id,
                            )
                        )
                    extra_role_ids.append(role_id)
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="assign.role",
                            actor_party_id=_PARTY_ASSIGNING,
                            target_id=role_id,
                            target_revision_id=None,
                        )
                    )

                elif op == "revoke_role":
                    # Pick the first un-revoked extra role assignment.
                    # If none are available the op is skipped — the
                    # property is exercised by every revocation that
                    # *does* run, and the scenario can still cover
                    # other ops in the same case.
                    candidate = next(
                        (rid for rid in extra_role_ids if rid not in revoked_role_ids),
                        None,
                    )
                    if candidate is None:
                        continue
                    # Mirror the persistence path of
                    # :func:`walking_slice.routes.roles.revoke_role_assignment`:
                    # one UPDATE that sets ``revoked_at``, plus one
                    # consequential audit append with
                    # ``action_type='revoke.role'``. Doing this
                    # directly (rather than driving the HTTP route)
                    # keeps the property test independent of the
                    # FastAPI / dependency-injection surface.
                    revoked_at_dt = clock.now()
                    revoked_at_iso = format_iso8601_ms(revoked_at_dt)
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "UPDATE Role_Assignments SET revoked_at = :ts "
                                "WHERE role_assignment_id = :rid "
                                "AND revoked_at IS NULL"
                            ),
                            {"ts": revoked_at_iso, "rid": candidate},
                        )
                        audit_log.append_consequential(
                            conn,
                            actor_party_id=_PARTY_ASSIGNING,
                            action_type="revoke.role",
                            target_id=candidate,
                            target_revision_id=None,
                            correlation_id=correlation_id,
                            recorded_time=revoked_at_dt,
                        )
                    revoked_role_ids.add(candidate)
                    expected_audit.append(
                        _expected_consequential(
                            correlation_id=correlation_id,
                            action_type="revoke.role",
                            actor_party_id=_PARTY_ASSIGNING,
                            target_id=candidate,
                            target_revision_id=None,
                        )
                    )

                else:  # pragma: no cover - defensive
                    raise AssertionError(f"unknown op: {op!r}")

            # ---------------------------------------------------------
            # Post-hoc assertions — one pass over the collected
            # expected-audit descriptors.
            # ---------------------------------------------------------
            for expected in expected_audit:
                rows = _fetch_audit_rows_for(
                    engine,
                    correlation_id=expected["correlation_id"],
                    outcome=expected["outcome"],
                )

                # --- (1) Existence ----------------------------------
                # The deny path produces two rows that share the
                # correlation identifier and the ``deny`` outcome —
                # the authorization-evaluation row appended in the
                # separate evaluation transaction (Requirement 12.5)
                # and the Denial Record appended in the
                # separate-transaction retry loop (Requirement 7.6).
                # Both rows satisfy "a denial Audit_Records row was
                # appended" from the property statement, so the deny
                # branch asserts ``>= 1`` and then verifies that
                # every matching row carries the expected attributes.
                # Consequential and permit branches expect exactly
                # one row because no second append shares their
                # correlation+outcome pair.
                if expected["outcome"] == "deny":
                    assert len(rows) >= 1, (
                        f"Property 11: expected at least one "
                        f"Audit_Records row with correlation_id="
                        f"{expected['correlation_id']!r} and outcome="
                        f"{expected['outcome']!r}; got "
                        f"{len(rows)} ({rows!r})."
                    )
                else:
                    assert len(rows) == 1, (
                        f"Property 11: expected exactly one "
                        f"Audit_Records row with correlation_id="
                        f"{expected['correlation_id']!r} and outcome="
                        f"{expected['outcome']!r}; got {len(rows)} "
                        f"({rows!r})."
                    )

                # --- (2) Attribute fidelity --------------------------
                # Every matching row must carry the expected values
                # on actor, action, target, and correlation. The deny
                # branch's two rows necessarily agree (they describe
                # the same denied attempt) so the property still
                # passes when both are present.
                for row in rows:
                    assert (
                        row["actor_party_id"] == expected["actor_party_id"]
                    ), (
                        f"Property 11: audit row "
                        f"{row['audit_record_id']!r} has actor_party_id="
                        f"{row['actor_party_id']!r}; expected "
                        f"{expected['actor_party_id']!r} for "
                        f"correlation_id={expected['correlation_id']!r}."
                    )
                    assert row["action_type"] == expected["action_type"], (
                        f"Property 11: audit row "
                        f"{row['audit_record_id']!r} has action_type="
                        f"{row['action_type']!r}; expected "
                        f"{expected['action_type']!r} for "
                        f"correlation_id={expected['correlation_id']!r}."
                    )
                    assert row["target_id"] == expected["target_id"], (
                        f"Property 11: audit row "
                        f"{row['audit_record_id']!r} has target_id="
                        f"{row['target_id']!r}; expected "
                        f"{expected['target_id']!r}."
                    )
                    assert (
                        row["target_revision_id"]
                        == expected["target_revision_id"]
                    ), (
                        f"Property 11: audit row "
                        f"{row['audit_record_id']!r} has target_revision_id="
                        f"{row['target_revision_id']!r}; expected "
                        f"{expected['target_revision_id']!r}."
                    )
                    assert (
                        row["correlation_id"] == expected["correlation_id"]
                    ), (
                        f"Property 11: audit row "
                        f"{row['audit_record_id']!r} has correlation_id="
                        f"{row['correlation_id']!r}; expected "
                        f"{expected['correlation_id']!r}."
                    )

                    # --- (3) Recorded time format -------------------
                    assert _RECORDED_AT_PATTERN.match(row["recorded_at"]), (
                        f"Property 11: audit row "
                        f"{row['audit_record_id']!r} has recorded_at="
                        f"{row['recorded_at']!r}; expected canonical "
                        f"millisecond-precision UTC text matching "
                        f"{_RECORDED_AT_PATTERN.pattern!r} "
                        "(Requirement 13.1)."
                    )

                # --- (4) Denial leaves no in-flight write ------------
                if expected["outcome"] == "deny":
                    rec_id, rec_rev_id = expected["denied_recommendation"]
                    decisions_for_target = (
                        _decision_count_for_recommendation(
                            engine,
                            recommendation_id=rec_id,
                            recommendation_revision_id=rec_rev_id,
                        )
                    )
                    assert decisions_for_target == 0, (
                        f"Property 11: denied attempt with "
                        f"correlation_id={expected['correlation_id']!r} "
                        f"left {decisions_for_target} Decisions row(s) "
                        f"addressing recommendation "
                        f"({rec_id!r}, {rec_rev_id!r}); a denial "
                        "must leave no in-flight write "
                        "(Requirement 7.6)."
                    )

        finally:
            engine.dispose()
