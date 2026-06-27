"""Unit tests for :meth:`walking_slice.trails.TrailService.append_revision`
— material-change detection that creates new Trail Revisions (task 10.2).

These tests pin the contract established in task 10.2, design
§"Trail_Service — Material-change detection", Requirements 9.4 and 9.6,
and AD-WS-4 (immutable Trail_Revisions rows):

- No-op path: a submission whose canonical form matches the prior
  Trail Revision returns the prior Revision unchanged. No new
  ``Trail_Revisions`` or ``Trail_Steps`` row is written, the
  ``Trails.current_revision_id`` pointer is unchanged, and no
  consequential audit row is appended.
- Material-change paths: a difference in purpose, audience, ordering
  rationale, target reference, ordinal-to-target mapping, or
  annotation creates a new immutable Trail Revision with
  ``predecessor_revision_id`` set to the prior Revision Identity
  and updates the ``Trails.current_revision_id`` pointer.
- The prior Trail Revision and its five Trail Step rows are
  byte-equivalent before and after the material-change append
  (Requirement 9.4 — "preserve the prior Trail Revision
  unchanged"; AD-WS-4 — Trail_Revisions immutability).
- Unknown ``trail_id`` raises :class:`TrailNotFoundError` with no
  database mutation.
- Structural validators and target resolvability checks run before
  the prior-Revision lookup so malformed/unresolvable submissions
  are rejected even when the Trail does not exist.

Property tests for Trail linearity and target resolvability (tasks
10.5 / 10.6) live separately in ``tests/property/``.
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
from walking_slice.knowledge import KnowledgeService, SupportRef
from walking_slice.models import AuthorityBasisRef
from walking_slice.trails import (
    AppendTrailRevisionResult,
    ORDINAL_TARGET_KIND,
    TrailNotFoundError,
    TrailService,
    TrailStepInput,
    TrailTargetUnresolvedError,
    TrailValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test constants and seeding helpers (mirroring ``test_trails.py``).
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
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
# ---------------------------------------------------------------------------


def _seed_full_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> dict[str, str]:
    """Seed a Document → Region → Finding → Recommendation → Decision pipeline.

    Returns the identifiers needed to assemble a five-step Trail.
    """
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
                SupportRef(
                    region_id=region.region_id,
                    document_revision_id=doc.revision_id,
                ),
            ],
        )
        recommendation = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Rationale derived from supporting finding.",
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


def _count_audit_create_trail(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT COUNT(*) FROM Audit_Records "
                    "WHERE action_type = 'create.trail'"
                )
            ).scalar_one()
        )


def _seed_initial_trail(
    engine: Engine,
    trail_service: TrailService,
    ids: dict[str, str],
    *,
    purpose: str = "Walk a reader from Evidence to Decision.",
    audience_id: str = "pilot-reviewers",
    ordering_rationale: str | None = "Pipeline-stage order.",
    steps: list[TrailStepInput] | None = None,
):
    """Create an initial Trail and return its CreateTrailResult."""
    if steps is None:
        steps = _five_valid_steps(ids)
    with engine.begin() as conn:
        return trail_service.create_trail(
            conn,
            purpose=purpose,
            audience_id=audience_id,
            ordering_rationale=ordering_rationale,
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )


# ---------------------------------------------------------------------------
# No-op (byte-equivalent canonical form) path.
# ---------------------------------------------------------------------------


def test_append_revision_returns_existing_when_canonical_form_matches(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Byte-equivalent canonical form returns the prior Revision unchanged.

    Validates Requirement 9.4 — only a *change* to purpose, audience,
    ordering rationale, included Trail Step targets, ordering, or
    annotation creates a new Trail Revision; an identical submission
    is preserved as-is. No INSERT, UPDATE, manifest, or audit row is
    written on the connection (AD-WS-4: Trail_Revisions immutability).
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)

    # Snapshot the persistent state before the no-op append.
    revisions_before = _count_rows(engine, "Trail_Revisions")
    steps_before = _count_rows(engine, "Trail_Steps")
    audit_create_trail_before = _count_audit_create_trail(engine)
    prior_revision_row = _fetch_trail_revision(
        engine, trail_revision_id=initial.trail_revision_id
    )
    prior_step_rows = _fetch_trail_steps(
        engine, trail_revision_id=initial.trail_revision_id
    )
    trail_row_before = _fetch_trail(engine, trail_id=initial.trail_id)

    # Submit a byte-equivalent submission. Steps are shuffled to
    # confirm the canonical form is order-insensitive (ordinals 1..5
    # define the canonical order — supplying the same steps in a
    # different list order must still match).
    steps = _five_valid_steps(ids)
    shuffled = [steps[3], steps[0], steps[4], steps[1], steps[2]]

    with engine.begin() as conn:
        result = trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose=initial.purpose,
            audience_id=initial.audience_id,
            ordering_rationale=initial.ordering_rationale,
            steps=shuffled,
            authoring_party_id=_PARTY_ID,
        )

    # Result: existing Revision returned, no new Revision created.
    assert isinstance(result, AppendTrailRevisionResult)
    assert result.created_new_revision is False
    assert result.trail_revision_id == initial.trail_revision_id
    # When no new revision is created the predecessor field names the
    # existing Revision so callers can still read a non-None value
    # uniformly.
    assert result.predecessor_revision_id == initial.trail_revision_id
    assert result.manifest_id is None  # no manifest written for a no-op

    # No new rows on the immutable tables.
    assert _count_rows(engine, "Trail_Revisions") == revisions_before
    assert _count_rows(engine, "Trail_Steps") == steps_before
    # No new consequential audit row.
    assert _count_audit_create_trail(engine) == audit_create_trail_before
    # The Trails.current_revision_id pointer is unchanged.
    trail_row_after = _fetch_trail(engine, trail_id=initial.trail_id)
    assert trail_row_after == trail_row_before
    # The prior Revision and its Step rows are byte-equivalent.
    assert (
        _fetch_trail_revision(
            engine, trail_revision_id=initial.trail_revision_id
        )
        == prior_revision_row
    )
    assert (
        _fetch_trail_steps(
            engine, trail_revision_id=initial.trail_revision_id
        )
        == prior_step_rows
    )


# ---------------------------------------------------------------------------
# Material-change paths.
# ---------------------------------------------------------------------------


def test_append_revision_creates_new_revision_when_purpose_changes(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A different ``purpose`` is a material change (Requirement 9.4).

    Asserts that a new immutable Trail Revision is inserted with
    ``predecessor_revision_id`` set to the prior Revision Identity,
    that the ``Trails.current_revision_id`` pointer advances to the
    new Revision, that the prior Revision is preserved byte-
    equivalent, and that exactly one new consequential audit row is
    appended.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)
    prior_revision_row = _fetch_trail_revision(
        engine, trail_revision_id=initial.trail_revision_id
    )
    prior_step_rows = _fetch_trail_steps(
        engine, trail_revision_id=initial.trail_revision_id
    )
    audit_before = _count_audit_create_trail(engine)

    with engine.begin() as conn:
        result = trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose="A different purpose for the same Trail.",
            audience_id=initial.audience_id,
            ordering_rationale=initial.ordering_rationale,
            steps=_five_valid_steps(ids),
            authoring_party_id=_PARTY_ID,
        )

    # New Revision created.
    assert result.created_new_revision is True
    assert result.trail_id == initial.trail_id
    assert result.trail_revision_id != initial.trail_revision_id
    assert _CANONICAL_UUID7.match(result.trail_revision_id)
    assert result.predecessor_revision_id == initial.trail_revision_id
    assert result.purpose == "A different purpose for the same Trail."

    # New Trail_Revisions row exists with predecessor link.
    new_row = _fetch_trail_revision(
        engine, trail_revision_id=result.trail_revision_id
    )
    assert new_row is not None
    assert new_row["predecessor_revision_id"] == initial.trail_revision_id
    assert new_row["trail_id"] == initial.trail_id
    assert new_row["purpose"] == "A different purpose for the same Trail."

    # Trails.current_revision_id advances to the new Revision.
    trail_row = _fetch_trail(engine, trail_id=initial.trail_id)
    assert trail_row is not None
    assert trail_row["current_revision_id"] == result.trail_revision_id

    # The prior Revision is preserved byte-equivalent (Requirement 9.4).
    assert (
        _fetch_trail_revision(
            engine, trail_revision_id=initial.trail_revision_id
        )
        == prior_revision_row
    )
    assert (
        _fetch_trail_steps(
            engine, trail_revision_id=initial.trail_revision_id
        )
        == prior_step_rows
    )

    # Five new Trail_Step rows for the new Revision.
    new_step_rows = _fetch_trail_steps(
        engine, trail_revision_id=result.trail_revision_id
    )
    assert len(new_step_rows) == 5
    assert [row["ordinal"] for row in new_step_rows] == [1, 2, 3, 4, 5]

    # Exactly one new consequential audit row for the new Revision.
    assert _count_audit_create_trail(engine) == audit_before + 1


def test_append_revision_creates_new_revision_when_annotation_changes(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A different Trail Step annotation is a material change (Requirement 9.4)."""
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)

    new_steps = _five_valid_steps(ids)
    # Edit the annotation on ordinal 3.
    new_steps[2] = TrailStepInput(
        ordinal=3,
        target_kind="finding_revision",
        target_id=ids["finding_id"],
        target_revision_id=ids["finding_revision_id"],
        annotation="A revised annotation describing the finding more carefully.",
    )

    with engine.begin() as conn:
        result = trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose=initial.purpose,
            audience_id=initial.audience_id,
            ordering_rationale=initial.ordering_rationale,
            steps=new_steps,
            authoring_party_id=_PARTY_ID,
        )

    assert result.created_new_revision is True
    assert result.trail_revision_id != initial.trail_revision_id
    assert result.predecessor_revision_id == initial.trail_revision_id

    # Confirm the annotation change is persisted on the new ordinal 3.
    new_step_rows = _fetch_trail_steps(
        engine, trail_revision_id=result.trail_revision_id
    )
    new_step_3 = next(row for row in new_step_rows if row["ordinal"] == 3)
    assert (
        new_step_3["annotation"]
        == "A revised annotation describing the finding more carefully."
    )


def test_append_revision_creates_new_revision_when_ordering_rationale_changes(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A different ``ordering_rationale`` is a material change."""
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)

    with engine.begin() as conn:
        result = trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose=initial.purpose,
            audience_id=initial.audience_id,
            ordering_rationale="Reordered to surface the decision first.",
            steps=_five_valid_steps(ids),
            authoring_party_id=_PARTY_ID,
        )

    assert result.created_new_revision is True
    new_row = _fetch_trail_revision(
        engine, trail_revision_id=result.trail_revision_id
    )
    assert new_row is not None
    assert new_row["ordering_rationale"] == "Reordered to surface the decision first."


def test_append_revision_creates_new_revision_when_target_changes(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A different Trail Step target reference is a material change."""
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)

    # Add a second Document Revision so ordinal 1 can target a
    # different but real Document Revision.
    with engine.begin() as conn:
        second_doc_rev = evidence_repository.append_revision(
            conn,
            resource_id=ids["document_resource_id"],
            content_bytes=b"A second revision of the source document.",
            contributing_party_id=_PARTY_ID,
        )

    new_steps = _five_valid_steps(ids)
    # Swap ordinal 1 to point at the second Document Revision.
    new_steps[0] = TrailStepInput(
        ordinal=1,
        target_kind="document_revision",
        target_id=ids["document_resource_id"],
        target_revision_id=second_doc_rev.revision_id,
        annotation="The source document.",
    )

    with engine.begin() as conn:
        result = trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose=initial.purpose,
            audience_id=initial.audience_id,
            ordering_rationale=initial.ordering_rationale,
            steps=new_steps,
            authoring_party_id=_PARTY_ID,
        )

    assert result.created_new_revision is True
    new_step_rows = _fetch_trail_steps(
        engine, trail_revision_id=result.trail_revision_id
    )
    new_step_1 = next(row for row in new_step_rows if row["ordinal"] == 1)
    assert new_step_1["target_revision_id"] == second_doc_rev.revision_id


# ---------------------------------------------------------------------------
# Error paths.
# ---------------------------------------------------------------------------


def test_append_revision_raises_when_trail_does_not_exist(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """An unknown ``trail_id`` raises :class:`TrailNotFoundError`.

    No INSERT, UPDATE, or audit row runs on the connection.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    pre_trails = _count_rows(engine, "Trails")
    pre_revisions = _count_rows(engine, "Trail_Revisions")
    pre_audit = _count_audit_create_trail(engine)

    unknown_trail_id = "00000000-0000-7000-8000-0000beefbeef"
    with engine.begin() as conn, pytest.raises(TrailNotFoundError) as exc:
        trail_service.append_revision(
            conn,
            trail_id=unknown_trail_id,
            purpose="Should not persist.",
            audience_id="pilot-reviewers",
            steps=_five_valid_steps(ids),
            authoring_party_id=_PARTY_ID,
        )
    assert exc.value.trail_id == unknown_trail_id
    assert exc.value.error_code == "trail_not_found"
    assert _count_rows(engine, "Trails") == pre_trails
    assert _count_rows(engine, "Trail_Revisions") == pre_revisions
    assert _count_audit_create_trail(engine) == pre_audit


def test_append_revision_rejects_structural_violations_before_lookup(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """A step-count violation surfaces as :class:`TrailValidationError`.

    Confirms structural validation runs before the prior-Revision
    lookup so a malformed submission is rejected even when it
    references an unknown Trail. No partial Trail_Revisions row
    appears.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)

    four_steps = _five_valid_steps(ids)[:4]
    pre_revisions = _count_rows(engine, "Trail_Revisions")
    with engine.begin() as conn, pytest.raises(TrailValidationError) as exc:
        trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose=initial.purpose,
            audience_id=initial.audience_id,
            steps=four_steps,
            authoring_party_id=_PARTY_ID,
        )
    assert exc.value.failed_constraint == "step_count_invalid"
    assert _count_rows(engine, "Trail_Revisions") == pre_revisions


def test_append_revision_rejects_unresolved_targets(
    engine: Engine,
    trail_service: TrailService,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> None:
    """Unresolved targets surface as :class:`TrailTargetUnresolvedError`.

    Confirms target-resolvability checking runs even on the append
    path (Requirement 9.5) and that no partial persistence occurs.
    """
    ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
    initial = _seed_initial_trail(engine, trail_service, ids)

    new_steps = _five_valid_steps(ids)
    # Replace ordinal 4's recommendation with a fake one.
    fake_rec = "00000000-0000-7000-8000-0000000fcfff"
    fake_rec_rev = "00000000-0000-7000-8000-0000000fcffe"
    new_steps[3] = TrailStepInput(
        ordinal=4,
        target_kind="recommendation_revision",
        target_id=fake_rec,
        target_revision_id=fake_rec_rev,
    )
    pre_revisions = _count_rows(engine, "Trail_Revisions")
    with engine.begin() as conn, pytest.raises(TrailTargetUnresolvedError) as exc:
        trail_service.append_revision(
            conn,
            trail_id=initial.trail_id,
            purpose=initial.purpose,
            audience_id=initial.audience_id,
            steps=new_steps,
            authoring_party_id=_PARTY_ID,
        )
    assert [u.ordinal for u in exc.value.unresolved_steps] == [4]
    assert _count_rows(engine, "Trail_Revisions") == pre_revisions
