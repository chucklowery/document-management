"""Unit tests for task 14.2 and task 14.3 — projection-envelope wrapping
and explainable-withholding source-record byte-equivalence.

Three scopes are covered:

1. :class:`walking_slice.projection.StatusProjector` in isolation —
   happy-path wrapping and the explanation-unavailable paths required by
   Requirement 14.4 (unresolvable Projection Definition; missing source
   Revision).
2. :meth:`walking_slice.trails.TrailService.create_trail_projected` —
   end-to-end demonstration that the additive entry point on the Trail
   service produces a :class:`ProjectedStatusResponse` on the happy path
   and on the "Trail unresolved" path (Requirement 9.5 + 14.1) and an
   :class:`ExplanationUnavailableResponse` when the Projection Definition
   is not registered (Requirement 14.4).
3. **Task 14.3** — byte-equivalence of pre-existing source Records across
   a withholding/correction cycle. Requirement 14.3 mandates that when a
   corrected or late-arriving source fact changes a projected status,
   every prior source Record, Revision, and correction record remains
   byte-equivalent to its recorded state; new facts arrive as additional
   Revisions or Records rather than as overwrites. Two correction
   scenarios are exercised:

   - the producer is reinvoked after the missing Projection Definition is
     registered (Requirement 14.4 path),
   - the producer is reinvoked after the originally unresolvable Trail
     target is corrected (Requirement 9.5 + 14.1 / 14.3 path).

   In both scenarios the immutable source-record tables snapshotted
   before the first attempt must remain bit-for-bit identical after both
   the withheld attempt and the corrected attempt that follows.

The structural-validator coverage for :meth:`create_trail` already lives
in ``test_trails.py``; these tests focus on the envelope-wrapping and
explainable-withholding behavior task 14.2 / 14.3 introduce.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService, SupportRef
from walking_slice.models import AuthorityBasisRef
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectedStatusResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
    StatusProjector,
)
from walking_slice.trails import (
    TRAIL_PROJECTION_DEFINITION_NAME,
    TRAIL_STATUS_RESOLVED,
    TRAIL_STATUS_UNRESOLVED,
    TrailService,
    TrailStepInput,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared constants and helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-00000000a001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_BOUNDARY = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

_TRAIL_DEFINITION = ProjectionDefinition(
    name=TRAIL_PROJECTION_DEFINITION_NAME, version="2026.01"
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
# StatusProjector — isolated unit tests.
# ---------------------------------------------------------------------------


class TestStatusProjectorHappyPath:
    """Happy-path tests: a registered Projection Definition produces a
    :class:`ProjectedStatusResponse` wrapping the supplied status name
    inside a :class:`ProjectionEnvelope` (Requirement 14.1, 14.2).
    """

    def test_returns_projected_status_response_when_definition_resolves(
        self,
    ) -> None:
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            definition_registry={
                TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION,
            },
        )

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_UNRESOLVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.status == TRAIL_STATUS_UNRESOLVED
        # Requirement 14.1 — every required envelope field is populated.
        assert isinstance(response.envelope, ProjectionEnvelope)
        assert response.envelope.definition == _TRAIL_DEFINITION
        assert response.envelope.applicable_temporal_boundary == _BOUNDARY
        assert response.envelope.generated_at == datetime(
            2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc
        )
        # Requirement 14.2 — derivation indicator pinned to "derived".
        assert response.envelope.derivation == "derived"
        # Defaults — empty source-id tuples, empty details.
        assert response.envelope.source_resource_ids == ()
        assert response.envelope.source_revision_ids == ()
        assert response.details == {}

    def test_propagates_source_ids_and_details(self) -> None:
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            definition_registry={
                TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION,
            },
        )
        resource_a = UUID("01890000-0000-7000-8000-00000000000a")
        resource_b = UUID("01890000-0000-7000-8000-00000000000b")
        revision_a = UUID("01890000-0000-7000-8000-00000000000c")

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_RESOLVED,
            source_resource_ids=[resource_a, resource_b],
            source_revision_ids=[revision_a],
            applicable_temporal_boundary=_BOUNDARY,
            details={"trail_id": "abc", "manifest_id": "def"},
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.envelope.source_resource_ids == (resource_a, resource_b)
        assert response.envelope.source_revision_ids == (revision_a,)
        assert response.details == {"trail_id": "abc", "manifest_id": "def"}

    def test_truncates_clock_to_second_precision_for_generated_at(self) -> None:
        # The Clock returns millisecond precision; the projector must
        # truncate to seconds so the envelope's validator accepts it.
        clock = FixedClock(
            datetime(2026, 1, 1, 12, 0, 5, 123_000, tzinfo=timezone.utc)
        )
        projector = StatusProjector(
            clock=clock,
            definition_registry={
                TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION,
            },
        )

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_RESOLVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.envelope.generated_at.microsecond == 0
        assert response.envelope.generated_at == datetime(
            2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc
        )

    def test_accepts_mapping_proxy_registry(self) -> None:
        # MappingProxyType is what the production composition (task 15.2)
        # will hand the projector to make the registry read-only at the
        # caller's side; the projector must accept any Mapping.
        registry: Mapping[str, ProjectionDefinition] = MappingProxyType(
            {TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION}
        )
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            definition_registry=registry,
        )

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_UNRESOLVED,
            applicable_temporal_boundary=_BOUNDARY,
        )
        assert isinstance(response, ProjectedStatusResponse)


class TestStatusProjectorExplanationUnavailable:
    """Withholding-path tests (Requirement 14.4). The projector returns an
    :class:`ExplanationUnavailableResponse` identifying the missing element
    and never builds an envelope. The caller is responsible for not
    persisting anything when this response is returned; the projector
    itself does not touch the database.
    """

    def test_returns_explanation_unavailable_when_definition_is_unregistered(
        self,
    ) -> None:
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            # Registry is empty — every lookup is unresolvable.
            definition_registry={},
        )

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_UNRESOLVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "projection_definition"
        assert response.missing_element_identifier == TRAIL_PROJECTION_DEFINITION_NAME

    def test_returns_explanation_unavailable_when_source_revision_is_missing(
        self,
    ) -> None:
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            definition_registry={
                TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION,
            },
        )
        missing_revision = UUID("01890000-0000-7000-8000-0000000000ff")

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_UNRESOLVED,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=missing_revision,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "source_revision"
        assert response.missing_element_identifier == str(missing_revision)

    def test_missing_source_revision_takes_precedence_over_unregistered_definition(
        self,
    ) -> None:
        # When the producer already knows the source Revision is missing
        # we report that specifically; the explanation-unavailable path
        # is more useful when the *precise* missing element is named.
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            # Empty registry: would otherwise trip the
            # "projection_definition" branch.
            definition_registry={},
        )
        missing_revision = UUID("01890000-0000-7000-8000-0000000000fe")

        response = projector.project_status(
            definition_name=TRAIL_PROJECTION_DEFINITION_NAME,
            status=TRAIL_STATUS_UNRESOLVED,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=missing_revision,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "source_revision"

    def test_has_definition_reflects_registry(self) -> None:
        projector = StatusProjector(
            clock=FixedClock(datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)),
            definition_registry={
                TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION,
            },
        )
        assert projector.has_definition(TRAIL_PROJECTION_DEFINITION_NAME) is True
        assert projector.has_definition("provenance.completeness") is False


# ---------------------------------------------------------------------------
# TrailService.create_trail_projected — integration tests.
# ---------------------------------------------------------------------------


# Re-use the pipeline seeding helper from the existing trails unit tests by
# duplicating its essentials here. Importing from another test file is not
# done in this repo's other unit-test files; we keep the helpers local so
# the test file's behavior is self-contained.


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


@pytest.fixture
def projector_with_trail_definition(clock) -> StatusProjector:
    return StatusProjector(
        clock=clock,
        definition_registry={
            TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION,
        },
    )


@pytest.fixture
def empty_projector(clock) -> StatusProjector:
    return StatusProjector(clock=clock, definition_registry={})


def _seed_full_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> dict[str, str]:
    """Seed a Source Document → Decision pipeline and return identifiers.

    Mirrors the helper in ``test_trails.py``; duplicated here so this
    test file is self-contained.
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
                )
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
    """Five valid Trail Steps targeting the seeded pipeline."""
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


_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class TestCreateTrailProjectedHappyPath:
    """Happy-path integration: a fully resolvable Trail submission yields a
    :class:`ProjectedStatusResponse` carrying ``trail.resolved`` plus the
    persisted identifiers in :attr:`details`, wrapped by a fully populated
    :class:`ProjectionEnvelope` (Requirements 14.1, 14.2).
    """

    def test_returns_projected_status_response_with_envelope(
        self,
        engine: Engine,
        trail_service: TrailService,
        projector_with_trail_definition: StatusProjector,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
    ) -> None:
        ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
        steps = _five_valid_steps(ids)

        with engine.begin() as conn:
            response = trail_service.create_trail_projected(
                conn,
                status_projector=projector_with_trail_definition,
                purpose="Walk from Evidence to Decision.",
                audience_id="pilot-reviewers",
                steps=steps,
                authoring_party_id=_PARTY_ID,
            )

        assert isinstance(response, ProjectedStatusResponse)
        # Requirement 14.1 — status is wrapped, envelope is present.
        assert response.status == TRAIL_STATUS_RESOLVED
        assert isinstance(response.envelope, ProjectionEnvelope)
        assert response.envelope.definition == _TRAIL_DEFINITION
        # Requirement 14.2 — derivation indicator pinned to "derived".
        assert response.envelope.derivation == "derived"
        # The envelope's source-Resource/Revision lists name the
        # identifiers the Trail steps cited (Requirement 14.1).
        assert UUID(ids["document_resource_id"]) in response.envelope.source_resource_ids
        assert UUID(ids["decision_id"]) in response.envelope.source_resource_ids
        assert UUID(ids["document_revision_id"]) in response.envelope.source_revision_ids
        assert UUID(ids["finding_revision_id"]) in response.envelope.source_revision_ids
        # The persisted identifiers ride in details.
        assert _CANONICAL_UUID7.match(response.details["trail_id"])
        assert _CANONICAL_UUID7.match(response.details["trail_revision_id"])
        # The trail row really landed in the database.
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text("SELECT trail_id FROM Trails WHERE trail_id = :tid"),
                    {"tid": response.details["trail_id"]},
                )
                .scalar_one_or_none()
            )
        assert row == response.details["trail_id"]


class TestCreateTrailProjectedTrailUnresolved:
    """When :meth:`create_trail` raises :class:`TrailTargetUnresolvedError`
    the wrapper returns a :class:`ProjectedStatusResponse` carrying
    ``trail.unresolved`` rather than re-raising. The per-ordinal
    unresolved-step detail is preserved inside :attr:`details` so
    Requirement 9.5's identification requirement is met.
    """

    def test_returns_trail_unresolved_status_with_unresolved_step_details(
        self,
        engine: Engine,
        trail_service: TrailService,
        projector_with_trail_definition: StatusProjector,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
    ) -> None:
        ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
        steps = _five_valid_steps(ids)
        # Replace ordinal 4 with a fake-but-canonical UUIDv7 so the
        # recommendation lookup fails.
        fake_rec = "00000000-0000-7000-8000-0000000fcfff"
        fake_rec_rev = "00000000-0000-7000-8000-0000000fcffe"
        steps[3] = TrailStepInput(
            ordinal=4,
            target_kind="recommendation_revision",
            target_id=fake_rec,
            target_revision_id=fake_rec_rev,
        )

        pre_trails = engine.connect().execute(
            text("SELECT COUNT(*) FROM Trails")
        ).scalar_one()

        with engine.begin() as conn:
            response = trail_service.create_trail_projected(
                conn,
                status_projector=projector_with_trail_definition,
                purpose="Walk to a missing recommendation.",
                audience_id="pilot-reviewers",
                steps=steps,
                authoring_party_id=_PARTY_ID,
            )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.status == TRAIL_STATUS_UNRESOLVED
        # The envelope is still present (Requirement 14.1).
        assert isinstance(response.envelope, ProjectionEnvelope)
        # The per-ordinal unresolved step detail is preserved
        # (Requirement 9.5).
        assert response.details["error_code"] == "trail_target_unresolved"
        unresolved = response.details["unresolved_steps"]
        assert len(unresolved) == 1
        assert unresolved[0]["ordinal"] == 4
        assert unresolved[0]["target_id"] == fake_rec
        assert unresolved[0]["target_revision_id"] == fake_rec_rev
        # No partial persistence — Requirement 9.5 / 14.3.
        post_trails = engine.connect().execute(
            text("SELECT COUNT(*) FROM Trails")
        ).scalar_one()
        assert post_trails == pre_trails


class TestCreateTrailProjectedExplanationUnavailable:
    """When the Projection Definition is not registered the wrapper
    short-circuits with :class:`ExplanationUnavailableResponse` and never
    invokes :meth:`create_trail`; source Records remain unchanged
    (Requirement 14.4 + 14.3).
    """

    def test_returns_explanation_unavailable_when_definition_is_unregistered(
        self,
        engine: Engine,
        trail_service: TrailService,
        empty_projector: StatusProjector,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
    ) -> None:
        ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
        steps = _five_valid_steps(ids)

        pre_trails = engine.connect().execute(
            text("SELECT COUNT(*) FROM Trails")
        ).scalar_one()

        with engine.begin() as conn:
            response = trail_service.create_trail_projected(
                conn,
                status_projector=empty_projector,
                purpose="Walk that should withhold its status.",
                audience_id="pilot-reviewers",
                steps=steps,
                authoring_party_id=_PARTY_ID,
            )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "projection_definition"
        assert response.missing_element_identifier == TRAIL_PROJECTION_DEFINITION_NAME
        # No source Records changed (Requirement 14.3 + 14.4). The
        # wrapper short-circuited before any INSERT could run.
        post_trails = engine.connect().execute(
            text("SELECT COUNT(*) FROM Trails")
        ).scalar_one()
        assert post_trails == pre_trails


# ---------------------------------------------------------------------------
# Task 14.3 — source-record byte-equivalence across withholding + correction.
#
# Requirement 14.3 (cross-referencing REQ-SF-010) requires that when a
# corrected or late-arriving source fact changes a projected status, every
# prior source Record, Revision, and correction record is retained
# byte-equivalent to its recorded state and new facts are appended as
# additional Revisions or Records rather than overwriting existing ones.
#
# The tests below exercise the two correction scenarios reachable through
# the Trail service's ``create_trail_projected`` surface:
#
# 1. Withholding triggered by an unresolvable Projection Definition
#    (Requirement 14.4), corrected by registering the Definition in the
#    projector's registry. The corrected follow-up call must succeed and
#    every immutable source-record table snapshotted before the first
#    attempt must remain bit-for-bit identical after both the withheld
#    attempt and the corrected attempt.
#
# 2. Withholding triggered by an unresolvable Trail target (Requirement
#    9.5 surfaced as the ``trail.unresolved`` projected status per task
#    14.2), corrected by submitting the same Trail with the resolvable
#    target identifiers. The same byte-equivalence guarantee applies.
#
# These tests focus narrowly on the byte-equivalence guarantee added by
# task 14.3. The existing classes above already cover the happy path,
# the unresolvable-definition path, and the trail-unresolved path; this
# class supplements that coverage with the specific assertion the task
# brief calls out ("assert source records are left byte-equivalent when
# corrections arrive").
# ---------------------------------------------------------------------------


# Immutable source-record tables that contribute material sources to a
# Trail Revision. Every table in this list is enforced insert-only by
# triggers (AD-WS-4); the byte-equivalence guarantee in Requirement 14.3
# therefore reduces to "the same row set still exists after the corrected
# request". The order matches the design's §"Table-by-Table
# Specification" so a reader can cross-reference each entry without
# rebuilding the mapping from memory.
_SOURCE_RECORD_TABLES: tuple[str, ...] = (
    "Source_Documents",
    "Document_Revisions",
    "Content_Regions",
    "Region_Occurrences",
    "Findings",
    "Finding_Revisions",
    "Recommendations",
    "Recommendation_Revisions",
    "Decisions",
    "Relationships",
    "Provenance_Manifests",
    "Omission_Entries",
)


def _snapshot_source_records(engine: Engine) -> dict[str, list[tuple]]:
    """Return a deterministic snapshot of every immutable source-record
    table.

    The rows are ordered by the table's first column so two snapshots
    taken at different times compare equal if and only if the same rows
    exist with the same byte content. Each row is materialized as a
    tuple of column values so Python's equality semantics give us a
    bit-for-bit comparison without round-tripping through JSON.
    """
    snapshot: dict[str, list[tuple]] = {}
    with engine.connect() as conn:
        for table in _SOURCE_RECORD_TABLES:
            rows = conn.execute(
                # ``ORDER BY 1`` orders by the table's first column
                # (the primary key in every immutable table we
                # snapshot). Stability across snapshots is what we
                # need; the absolute ordering does not matter as long
                # as it is reproducible.
                text(f"SELECT * FROM {table} ORDER BY 1")
            ).all()
            # ``Row`` instances compare equal element-wise but
            # converting to tuples gives us a hashable, repr-friendly
            # form for the assertion messages.
            snapshot[table] = [tuple(row) for row in rows]
    return snapshot


def _snapshot_audit_records_by_id(engine: Engine) -> dict[str, tuple]:
    """Return existing :class:`Audit_Records` rows keyed by
    ``audit_record_id``.

    Audit_Records grows when the corrected Trail submission succeeds
    (the trail service appends a consequential audit row inside the
    Trail-creation transaction). Snapshotting by ``audit_record_id``
    lets us assert that *previously appended* audit rows remain
    byte-equivalent (Requirement 13.3, 13.4, 14.3) without forbidding
    new audit rows from arriving after the correction.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM Audit_Records ORDER BY append_sequence")
        ).all()
    return {row.audit_record_id: tuple(row) for row in rows}


class TestSourceRecordsByteEquivalenceAcrossCorrections:
    """Requirement 14.3 — source Records remain byte-equivalent when a
    withheld projection is corrected by a follow-up request.

    Two correction paths are exercised:

    - the missing Projection Definition is registered in the projector
      and the same Trail submission is retried (Requirement 14.4 →
      Requirement 14.1 happy path);
    - the originally unresolvable Trail target is replaced with a
      resolvable one and the Trail submission is retried (Requirement
      9.5 + 14.1 / 14.3).

    In both paths the immutable source-record tables and previously
    appended audit rows seeded by the pipeline must remain identical
    after the withheld attempt and after the corrected attempt. New
    facts (a Trail row, Trail_Revision row, five Trail_Step rows, and
    the consequential audit row) are appended on the corrected attempt;
    the assertion strategy snapshots by primary key so those additions
    do not falsify the byte-equivalence check on prior rows.
    """

    def test_byte_equivalence_after_registering_missing_projection_definition(
        self,
        engine: Engine,
        clock,
        trail_service: TrailService,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
    ) -> None:
        ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
        steps = _five_valid_steps(ids)

        # Snapshot the source-record universe BEFORE any projection
        # attempt runs. This is the byte-image Requirement 14.3
        # promises will be preserved through the correction cycle.
        initial_snapshot = _snapshot_source_records(engine)
        initial_audit_by_id = _snapshot_audit_records_by_id(engine)
        # Sanity: the seed produced at least one row per source-record
        # table the Trail will reference, so the byte-equivalence
        # assertions below are meaningful (an empty universe would
        # trivially compare equal).
        assert initial_snapshot["Source_Documents"], "pipeline seed missing source document"
        assert initial_snapshot["Document_Revisions"], "pipeline seed missing document revision"
        assert initial_snapshot["Region_Occurrences"], "pipeline seed missing region occurrence"
        assert initial_snapshot["Finding_Revisions"], "pipeline seed missing finding revision"
        assert initial_snapshot["Recommendation_Revisions"], (
            "pipeline seed missing recommendation revision"
        )
        assert initial_snapshot["Decisions"], "pipeline seed missing decision"
        assert initial_audit_by_id, "pipeline seed missing audit rows"

        # Step 1 — initial attempt with an empty projector registry
        # withholds the projected status (Requirement 14.4). No
        # ``create_trail`` call is made; no row is touched.
        empty_projector = StatusProjector(clock=clock, definition_registry={})
        with engine.begin() as conn:
            withheld = trail_service.create_trail_projected(
                conn,
                status_projector=empty_projector,
                purpose="Walk that withholds until the definition is registered.",
                audience_id="pilot-reviewers",
                steps=steps,
                authoring_party_id=_PARTY_ID,
            )
        assert isinstance(withheld, ExplanationUnavailableResponse)
        assert withheld.missing_element_kind == "projection_definition"

        # Source Records and previously appended audit rows are
        # unchanged by the withheld attempt (Requirement 14.3, 14.4).
        post_withhold_snapshot = _snapshot_source_records(engine)
        assert post_withhold_snapshot == initial_snapshot
        post_withhold_audit_by_id = _snapshot_audit_records_by_id(engine)
        for audit_id, original_row in initial_audit_by_id.items():
            assert post_withhold_audit_by_id[audit_id] == original_row, (
                f"Audit_Records row {audit_id} mutated across the "
                "withheld attempt"
            )

        # Step 2 — correction arrives: the same Projection Definition
        # the slice's composition root registers in production is now
        # available to the projector. The same Trail submission is
        # retried.
        corrected_projector = StatusProjector(
            clock=clock,
            definition_registry={TRAIL_PROJECTION_DEFINITION_NAME: _TRAIL_DEFINITION},
        )
        with engine.begin() as conn:
            corrected = trail_service.create_trail_projected(
                conn,
                status_projector=corrected_projector,
                purpose="Walk that withholds until the definition is registered.",
                audience_id="pilot-reviewers",
                steps=steps,
                authoring_party_id=_PARTY_ID,
            )

        assert isinstance(corrected, ProjectedStatusResponse)
        assert corrected.status == TRAIL_STATUS_RESOLVED
        # The corrected attempt appended new facts, not overwrites: a
        # Trail row, a Trail_Revision row, five Trail_Step rows, and
        # one consequential audit row.
        new_trail_id = corrected.details["trail_id"]
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT trail_id FROM Trails WHERE trail_id = :tid"),
                {"tid": new_trail_id},
            ).scalar_one() == new_trail_id
            new_step_count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM Trail_Steps "
                    "WHERE trail_revision_id = :rid"
                ),
                {"rid": corrected.details["trail_revision_id"]},
            ).scalar_one()
        assert new_step_count == 5

        # Requirement 14.3 — every prior source Record remains
        # byte-equivalent after the corrected attempt. New facts are
        # in the Trail-related tables, not in the source-record
        # tables snapshotted here.
        post_correction_snapshot = _snapshot_source_records(engine)
        assert post_correction_snapshot == initial_snapshot

        # Previously appended audit rows also remain byte-equivalent
        # (the consequential audit row written for the new Trail
        # Revision is a *new* row with a fresh audit_record_id, so it
        # does not collide with the snapshot keys).
        post_correction_audit_by_id = _snapshot_audit_records_by_id(engine)
        for audit_id, original_row in initial_audit_by_id.items():
            assert post_correction_audit_by_id[audit_id] == original_row, (
                f"Audit_Records row {audit_id} mutated across the "
                "corrected attempt"
            )
        # And the corrected attempt's audit row really was appended,
        # not substituted for a prior one.
        assert len(post_correction_audit_by_id) > len(initial_audit_by_id)

    def test_byte_equivalence_after_correcting_unresolvable_trail_target(
        self,
        engine: Engine,
        clock,
        trail_service: TrailService,
        projector_with_trail_definition: StatusProjector,
        evidence_repository: EvidenceRepository,
        knowledge_service: KnowledgeService,
    ) -> None:
        ids = _seed_full_pipeline(engine, evidence_repository, knowledge_service)
        valid_steps = _five_valid_steps(ids)

        # Snapshot the source-record universe BEFORE the first attempt.
        initial_snapshot = _snapshot_source_records(engine)
        initial_audit_by_id = _snapshot_audit_records_by_id(engine)

        # Step 1 — initial submission references a recommendation that
        # does not exist; the Trail service surfaces this as the
        # ``trail.unresolved`` projected status (Requirement 9.5 + 14.1).
        # No partial persistence occurs because the resolvability
        # check runs before any INSERT (Requirement 9.5).
        bad_steps = list(valid_steps)
        fake_rec_id = "00000000-0000-7000-8000-0000000fafff"
        fake_rec_rev = "00000000-0000-7000-8000-0000000fafee"
        bad_steps[3] = TrailStepInput(
            ordinal=4,
            target_kind="recommendation_revision",
            target_id=fake_rec_id,
            target_revision_id=fake_rec_rev,
            annotation="Initially unresolvable recommendation.",
        )

        with engine.begin() as conn:
            withheld = trail_service.create_trail_projected(
                conn,
                status_projector=projector_with_trail_definition,
                purpose="Walk pending a correction to its recommendation step.",
                audience_id="pilot-reviewers",
                steps=bad_steps,
                authoring_party_id=_PARTY_ID,
            )
        assert isinstance(withheld, ProjectedStatusResponse)
        assert withheld.status == TRAIL_STATUS_UNRESOLVED
        assert withheld.details["error_code"] == "trail_target_unresolved"
        unresolved_ordinals = [u["ordinal"] for u in withheld.details["unresolved_steps"]]
        assert unresolved_ordinals == [4]

        # No source Record mutated across the withheld attempt
        # (Requirement 14.3) and previously appended audit rows are
        # byte-equivalent (Requirement 13.3).
        post_withhold_snapshot = _snapshot_source_records(engine)
        assert post_withhold_snapshot == initial_snapshot
        post_withhold_audit_by_id = _snapshot_audit_records_by_id(engine)
        for audit_id, original_row in initial_audit_by_id.items():
            assert post_withhold_audit_by_id[audit_id] == original_row, (
                f"Audit_Records row {audit_id} mutated across the "
                "withheld trail-target attempt"
            )
        # The withheld attempt also did not append any audit row
        # because no INSERT was issued.
        assert post_withhold_audit_by_id == initial_audit_by_id

        # Step 2 — the correction arrives: the originally unresolvable
        # ordinal 4 is replaced with the resolvable identifier the
        # pipeline produced. Re-submitting the Trail with the
        # corrected step succeeds (Requirement 14.1 happy path).
        with engine.begin() as conn:
            corrected = trail_service.create_trail_projected(
                conn,
                status_projector=projector_with_trail_definition,
                purpose="Walk pending a correction to its recommendation step.",
                audience_id="pilot-reviewers",
                steps=valid_steps,
                authoring_party_id=_PARTY_ID,
            )
        assert isinstance(corrected, ProjectedStatusResponse)
        assert corrected.status == TRAIL_STATUS_RESOLVED
        # The corrected attempt appended a Trail row, a Trail Revision,
        # five Trail Steps, and an audit row — not overwrites.
        with engine.connect() as conn:
            new_trail_id = conn.execute(
                text("SELECT trail_id FROM Trails WHERE trail_id = :tid"),
                {"tid": corrected.details["trail_id"]},
            ).scalar_one()
            new_revision_id = conn.execute(
                text(
                    "SELECT trail_revision_id FROM Trail_Revisions "
                    "WHERE trail_revision_id = :rid"
                ),
                {"rid": corrected.details["trail_revision_id"]},
            ).scalar_one()
        assert new_trail_id == corrected.details["trail_id"]
        assert new_revision_id == corrected.details["trail_revision_id"]

        # Requirement 14.3 — source Records are byte-equivalent after
        # the corrected attempt. The new Trail-related rows live in
        # tables outside ``_SOURCE_RECORD_TABLES``.
        post_correction_snapshot = _snapshot_source_records(engine)
        assert post_correction_snapshot == initial_snapshot

        # Previously appended audit rows remain byte-equivalent and a
        # new consequential audit row was appended for the successful
        # Trail creation.
        post_correction_audit_by_id = _snapshot_audit_records_by_id(engine)
        for audit_id, original_row in initial_audit_by_id.items():
            assert post_correction_audit_by_id[audit_id] == original_row
        assert len(post_correction_audit_by_id) > len(initial_audit_by_id)
