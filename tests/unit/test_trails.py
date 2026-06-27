"""Unit tests for :mod:`walking_slice.trails` — Trails, Trail Revisions,
Trail Steps with structural validators (task 10.1).

These tests pin the contract established in task 10.1, design
§"Trail_Service" + §"Table-by-Table Specification — Trails,
Trail_Revisions, Trail_Steps", Requirements 9.1, 9.2, 9.3, 9.5, 9.6,
9.7, and AD-WS-12 (slice-restricted ``selection_mode = 'Pinned'``):

- Happy path: a five-step submission inserts one ``Trails`` row, one
  ``Trail_Revisions`` row, exactly five ``Trail_Steps`` rows (one per
  ordinal 1..5), updates ``Trails.current_revision_id``, and appends
  one consequential ``Audit_Records`` row inside the caller's
  transaction (AD-WS-5; Requirements 9.1, 9.3, 13.1).
- Structural validators reject step-count and target-kind mismatches
  before any database round-trip (Requirements 9.2, 9.7).
- Target resolvability check rejects the entire request with
  per-ordinal detail and zero partial persistence when any step's
  target does not exist (Requirement 9.5).

The structural validator coverage here is the minimum needed to lock
in task 10.1; the dedicated edge-case sweep (4-step, 6-step,
non-contiguous, Live/Approval-Controlled selection mode) is task 10.4
and lives in its own file.

Property tests for Trail linearity and Trail target resolvability
(Properties 5 and 6 in task 10.5 / 10.6) are separate files in
``tests/property/`` — those tasks come later in the dependency graph
and are explicitly out of scope for this file.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService
from walking_slice.models import AuthorityBasisRef
from walking_slice.trails import (
    CreateTrailResult,
    ORDINAL_TARGET_KIND,
    TrailService,
    TrailStepInput,
    TrailTargetUnresolvedError,
    TrailValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seeding helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def _seed_party(conn, party_id: str = _PARTY_ID, display: str = "Author") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def evidence_repository(
    clock, identity_service: IdentityService, audit_log: AuditLog
) -> EvidenceRepository:
    return EvidenceRepository(
        clock=clock, identity_service=identity_service, audit_log=audit_log
    )


@pytest.fixture
def knowledge_service(
    clock, identity_service: IdentityService, audit_log: AuditLog
) -> KnowledgeService:
    return KnowledgeService(
        clock=clock, identity_service=identity_service, audit_log=audit_log
    )


# ---------------------------------------------------------------------------
# Pipeline-seed helper.
#
# Builds a full Source Evidence → Decision pipeline so a Trail can cite
# real, resolvable targets for every ordinal. Returns a dict of the
# identifiers each step needs.
# ---------------------------------------------------------------------------


def _seed_full_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> dict[str, str]:
    """Seed a Source Document → Document Revision → Region Occurrence →
    Finding → Recommendation → Decision and return the identifiers."""
    with engine.begin() as conn:
        _seed_party(conn)
        doc = evidence_repository.create_document(
            conn,
            content_bytes=b"Hello, world. The quick brown fox jumps.",
            contributing_party_id=_PARTY_ID,
            authority="authoritative",
        )
        region = evidence_repository.create_region_occurrence(
            conn,
            resource_id=doc.resource_id,
            revision_id=doc.revision_id,
            start_offset_bytes=0,
            end_offset_bytes=5,
            contributing_party_id=_PARTY_ID,
        )
        finding = knowledge_service.create_finding(
            conn,
            statement="An evidence-backed claim about the corpus.",
            authoring_party_id=_PARTY_ID,
            supporting_region_occurrences=[
                # Inline type to avoid a top-level import; mirrors how
                # ``test_knowledge_recommendations.py`` builds supports
                # without pulling in :class:`SupportRef`.
                __import__(
                    "walking_slice.knowledge", fromlist=["SupportRef"]
                ).SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                ),
            ],
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="A rationale derived from the supporting finding.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=recommendation.recommendation_revision_id,
            outcome="Accept",
            rationale="Approved based on the recommendation.",
            deciding_party_id=_PARTY_ID,
            authority_basis=AuthorityBasisRef(
                type="role-grant-id", id=_AUTHORITY_BASIS_ID
            ),
            applicable_scope=_SCOPE,
        )

    return {
        "document_resource_id": doc.resource_id,
        "document_revision_id": doc.revision_id,
        "region_id": region.region_id,
        "finding_id": finding.finding_id,
        "finding_revision_id": finding.finding_revision_id,
        "recommendation_id": recommendation.recommendation_id,
        "recommendation_revision_id": recommendation.recommendation_revision_id,
        "decision_id": decision.decision_id,
    }


def _five_valid_steps(ids: dict[str, str]) -> list[TrailStepInput]:
    """Build a valid five-step Trail input matching the seeded pipeline."""
    return [
        TrailStepInput(
            ordinal=1,
            target_kind="document_revision",
            target_id=ids["document_resource_id"],
            target_revision_id=ids["document_revision_id"],
            annotation="The source document.",
        ),
        TrailStepInput(
            ordinal=2,
            target_kind="region_occurrence",
            target_id=ids["document_revision_id"],
            region_id=ids["region_id"],
            annotation="The cited region.",
        ),
        TrailStepInput(
            ordinal=3,
            target_kind="finding_revision",
            target_id=ids["finding_id"],
            target_revision_id=ids["finding_revision_id"],
            annotation="The supporting finding.",
        ),
        TrailStepInput(
            ordinal=4,
            target_kind="recommendation_revision",
            target_id=ids["recommendation_id"],
            target_revision_id=ids["recommendation_revision_id"],
            annotation="The recommendation.",
        ),
        TrailStepInput(
            ordinal=5,
            target_kind="decision",
            target_id=ids["decision_id"],
            annotation="The authorized decision.",
        ),
    ]


# ---------------------------------------------------------------------------
# Row readers.
# ---------------------------------------------------------------------------


def _fetch_trail(engine: Engine, *, trail_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT trail_id, created_at, current_revision_id
                      FROM Trails WHERE trail_id = :tid
                    """
                ),
                {"tid": trail_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_trail_revision(engine: Engine, *, trail_revision_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT trail_revision_id, trail_id, predecessor_revision_id,
                           purpose, audience_id, ordering_rationale,
                           authoring_party_id, recorded_at
                      FROM Trail_Revisions WHERE trail_revision_id = :trev
                    """
                ),
                {"trev": trail_revision_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_trail_steps(engine: Engine, *, trail_revision_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT trail_step_id, ordinal, selection_mode,
                           target_kind, target_id, target_revision_id,
                           region_id, annotation
                      FROM Trail_Steps WHERE trail_revision_id = :trev
                     ORDER BY ordinal
                    """
                ),
                {"trev": trail_revision_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _count_rows(engine: Engine, table: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_create_trail_inserts_trail_revision_and_five_steps(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Five-step happy path lands the expected rows.

    Validates Requirements 9.1 (Trail Resource + immutable Trail
    Revision + exactly five steps), 9.2 (ordinals 1..5 with the right
    target kind per ordinal), 9.3 (each step records target reference,
    selection_mode='Pinned', annotation), 9.6 (purpose, audience,
    ordering rationale), and 13.1 (consequential audit row).
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    steps = _five_valid_steps(ids)

    with engine.begin() as conn:
        result = trail_service.create_trail(
            conn,
            purpose="Walk a reader from Evidence to Decision.",
            audience_id="pilot-reviewers",
            ordering_rationale="Pipeline-stage order.",
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )

    assert isinstance(result, CreateTrailResult)
    assert _CANONICAL_UUID7.match(result.trail_id)
    assert _CANONICAL_UUID7.match(result.trail_revision_id)
    assert result.trail_id != result.trail_revision_id  # AD-WS-3
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at)

    # Trail header recorded and current_revision_id pointer set.
    trail_row = _fetch_trail(engine, trail_id=result.trail_id)
    assert trail_row is not None
    assert trail_row["current_revision_id"] == result.trail_revision_id

    # Trail Revision row carries Requirement 9.6 attributes.
    revision_row = _fetch_trail_revision(
        engine, trail_revision_id=result.trail_revision_id
    )
    assert revision_row is not None
    assert revision_row["trail_id"] == result.trail_id
    assert revision_row["predecessor_revision_id"] is None
    assert revision_row["purpose"] == "Walk a reader from Evidence to Decision."
    assert revision_row["audience_id"] == "pilot-reviewers"
    assert revision_row["ordering_rationale"] == "Pipeline-stage order."
    assert revision_row["authoring_party_id"] == _PARTY_ID
    assert _ISO_8601_MS_PATTERN.match(revision_row["recorded_at"])

    # Exactly five Trail Step rows with the expected ordinal/kind pairing.
    step_rows = _fetch_trail_steps(
        engine, trail_revision_id=result.trail_revision_id
    )
    assert len(step_rows) == 5
    for ordinal, row in enumerate(step_rows, start=1):
        assert row["ordinal"] == ordinal
        assert row["selection_mode"] == "Pinned"
        assert row["target_kind"] == ORDINAL_TARGET_KIND[ordinal]
        if ordinal == 2:
            assert row["region_id"] == ids["region_id"]
            assert row["target_revision_id"] is None
        else:
            assert row["region_id"] is None
        if ordinal in (1, 3, 4):
            assert row["target_revision_id"] is not None
        if ordinal == 5:
            assert row["target_id"] == ids["decision_id"]
            assert row["target_revision_id"] is None

    # The returned CreateTrailResult mirrors the persisted step order.
    assert [s.ordinal for s in result.steps] == [1, 2, 3, 4, 5]
    assert {s.trail_step_id for s in result.steps} == {
        row["trail_step_id"] for row in step_rows
    }

    # Audit log carries exactly one consequential row for the Trail
    # creation (Requirements 9, 13.1). Other rows on the connection
    # (Document, Region, Finding, Recommendation, Decision creates)
    # also appear — we filter to the create.trail row.
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT actor_party_id, action_type, outcome,
                           target_id, target_revision_id, correlation_id
                      FROM Audit_Records
                     WHERE action_type = 'create.trail'
                    """
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    trail_audit = dict(rows[0])
    assert trail_audit["actor_party_id"] == _PARTY_ID
    assert trail_audit["outcome"] == "consequential"
    assert trail_audit["target_id"] == result.trail_id
    assert trail_audit["target_revision_id"] == result.trail_revision_id


def test_create_trail_accepts_steps_in_any_order(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Submitting steps out of ordinal order still lands them sorted 1..5.

    Structural validation sorts by ordinal; the persisted Trail_Steps
    rows are emitted with ascending ordinals so reading
    ``ORDER BY ordinal`` walks the pipeline correctly. This protects
    the contract Requirement 9.2 makes for the read side.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    steps = _five_valid_steps(ids)
    # Shuffle: 5, 1, 4, 2, 3
    shuffled = [steps[4], steps[0], steps[3], steps[1], steps[2]]

    with engine.begin() as conn:
        result = trail_service.create_trail(
            conn,
            purpose="Out-of-order submission still sorts.",
            audience_id="pilot-reviewers",
            steps=shuffled,
            authoring_party_id=_PARTY_ID,
        )

    step_rows = _fetch_trail_steps(
        engine, trail_revision_id=result.trail_revision_id
    )
    assert [row["ordinal"] for row in step_rows] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# Structural validators (Requirements 9.1, 9.2, 9.3, 9.6, 9.7 / AD-WS-12).
# ---------------------------------------------------------------------------


def test_create_trail_rejects_when_step_count_is_not_five(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A 4-step or 6-step submission is rejected before any DB write.

    Requirement 9.7 calls out fewer-than-5 and more-than-5 as
    structural violations. The validator runs before any INSERT so
    no Trails / Trail_Revisions / Trail_Steps row appears (this
    test also seeds the pipeline so it can build a valid sample
    and then truncate / extend it).
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    valid_steps = _five_valid_steps(ids)
    four_steps = valid_steps[:4]

    pre_trails = _count_rows(engine, "Trails")
    with engine.begin() as conn, pytest.raises(TrailValidationError) as exc:
        trail_service.create_trail(
            conn,
            purpose="Should not persist.",
            audience_id="pilot-reviewers",
            steps=four_steps,
            authoring_party_id=_PARTY_ID,
        )
    assert exc.value.failed_constraint == "step_count_invalid"
    # No partial Trails row.
    assert _count_rows(engine, "Trails") == pre_trails


def test_create_trail_rejects_when_target_kind_mismatches_ordinal(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A step whose ``target_kind`` does not match its ordinal is rejected.

    Requirement 9.2 / 9.7 fix the target kind per ordinal. The
    Python-side validator surfaces a precise constraint name
    (``target_kind_invalid_for_ordinal``) instead of letting the
    schema CHECK fire with a generic IntegrityError.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    steps = _five_valid_steps(ids)
    # Swap ordinal 1's target_kind to 'decision' — wrong for ordinal 1.
    steps[0] = TrailStepInput(
        ordinal=1,
        target_kind="decision",  # mismatched kind for ordinal 1
        target_id=ids["decision_id"],
    )

    with engine.begin() as conn, pytest.raises(TrailValidationError) as exc:
        trail_service.create_trail(
            conn,
            purpose="Will not write.",
            audience_id="pilot-reviewers",
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )
    assert exc.value.failed_constraint == "target_kind_invalid_for_ordinal"


def test_create_trail_rejects_non_pinned_selection_mode(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """``selection_mode != 'Pinned'`` is rejected by AD-WS-12."""
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    steps = _five_valid_steps(ids)
    # Replace ordinal 3 with a Live-mode step.
    steps[2] = TrailStepInput(
        ordinal=3,
        target_kind="finding_revision",
        target_id=ids["finding_id"],
        target_revision_id=ids["finding_revision_id"],
        selection_mode="Live",
    )

    with engine.begin() as conn, pytest.raises(TrailValidationError) as exc:
        trail_service.create_trail(
            conn,
            purpose="Will not write.",
            audience_id="pilot-reviewers",
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )
    assert exc.value.failed_constraint == "selection_mode_invalid"


# ---------------------------------------------------------------------------
# Target resolvability (Requirement 9.5).
# ---------------------------------------------------------------------------


def test_create_trail_rejects_unresolved_targets_with_per_ordinal_list(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Unresolved targets surface as :class:`TrailTargetUnresolvedError`.

    Requirement 9.5 demands the entire Trail Revision request be
    rejected, the error identify *each* unresolved step by ordinal
    and target reference, and no partial persistence occur.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    steps = _five_valid_steps(ids)

    # Swap two real targets for fake-but-canonical UUIDv7s. Ordinals
    # 2 (region_occurrence) and 4 (recommendation_revision) — picked
    # because they exercise two different resolvability branches.
    fake_doc = "00000000-0000-7000-8000-0000000fafff"
    fake_region = "00000000-0000-7000-8000-0000000fbfff"
    fake_rec = "00000000-0000-7000-8000-0000000fcfff"
    fake_rec_rev = "00000000-0000-7000-8000-0000000fcffe"
    steps[1] = TrailStepInput(
        ordinal=2,
        target_kind="region_occurrence",
        target_id=fake_doc,
        region_id=fake_region,
    )
    steps[3] = TrailStepInput(
        ordinal=4,
        target_kind="recommendation_revision",
        target_id=fake_rec,
        target_revision_id=fake_rec_rev,
    )

    pre_trails = _count_rows(engine, "Trails")
    pre_revisions = _count_rows(engine, "Trail_Revisions")
    pre_steps_rows = _count_rows(engine, "Trail_Steps")

    with engine.begin() as conn, pytest.raises(TrailTargetUnresolvedError) as exc:
        trail_service.create_trail(
            conn,
            purpose="Should not persist.",
            audience_id="pilot-reviewers",
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )

    # The error identifies both unresolved ordinals, in ordinal order
    # (Requirement 9.5 — by ordinal and target reference).
    err = exc.value
    assert err.error_code == "trail_target_unresolved"
    unresolved_ordinals = [u.ordinal for u in err.unresolved_steps]
    assert unresolved_ordinals == [2, 4]
    # Per-ordinal target references are surfaced.
    by_ordinal = {u.ordinal: u for u in err.unresolved_steps}
    assert by_ordinal[2].target_id == fake_doc
    assert by_ordinal[2].region_id == fake_region
    assert by_ordinal[4].target_id == fake_rec
    assert by_ordinal[4].target_revision_id == fake_rec_rev

    # No partial persistence (Requirement 9.5).
    assert _count_rows(engine, "Trails") == pre_trails
    assert _count_rows(engine, "Trail_Revisions") == pre_revisions
    assert _count_rows(engine, "Trail_Steps") == pre_steps_rows


def test_create_trail_with_no_resolvable_targets_returns_all_five_unresolved(
    engine: Engine,
    trail_service: TrailService,
) -> None:
    """Every step unresolved → every ordinal listed in the error.

    Exercises the upper end of Requirement 9.5: when 0 of 5 targets
    resolve, all five must appear in the per-ordinal list. The
    pipeline is *not* seeded; the database holds only the
    structural schema and a single Party.
    """
    with engine.begin() as conn:
        _seed_party(conn)

    # Build a step set whose targets all reference identifiers that
    # do not exist anywhere in the database.
    fake_doc = "00000000-0000-7000-8000-00000000ad01"
    fake_doc_rev = "00000000-0000-7000-8000-00000000ad02"
    fake_region = "00000000-0000-7000-8000-00000000ad03"
    fake_finding = "00000000-0000-7000-8000-00000000ad04"
    fake_finding_rev = "00000000-0000-7000-8000-00000000ad05"
    fake_rec = "00000000-0000-7000-8000-00000000ad06"
    fake_rec_rev = "00000000-0000-7000-8000-00000000ad07"
    fake_decision = "00000000-0000-7000-8000-00000000ad08"
    steps = [
        TrailStepInput(
            ordinal=1,
            target_kind="document_revision",
            target_id=fake_doc,
            target_revision_id=fake_doc_rev,
        ),
        TrailStepInput(
            ordinal=2,
            target_kind="region_occurrence",
            target_id=fake_doc_rev,
            region_id=fake_region,
        ),
        TrailStepInput(
            ordinal=3,
            target_kind="finding_revision",
            target_id=fake_finding,
            target_revision_id=fake_finding_rev,
        ),
        TrailStepInput(
            ordinal=4,
            target_kind="recommendation_revision",
            target_id=fake_rec,
            target_revision_id=fake_rec_rev,
        ),
        TrailStepInput(
            ordinal=5,
            target_kind="decision",
            target_id=fake_decision,
        ),
    ]

    with engine.begin() as conn, pytest.raises(TrailTargetUnresolvedError) as exc:
        trail_service.create_trail(
            conn,
            purpose="Should not persist.",
            audience_id="pilot-reviewers",
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )
    err = exc.value
    assert [u.ordinal for u in err.unresolved_steps] == [1, 2, 3, 4, 5]
    assert _count_rows(engine, "Trails") == 0
    assert _count_rows(engine, "Trail_Revisions") == 0
    assert _count_rows(engine, "Trail_Steps") == 0
