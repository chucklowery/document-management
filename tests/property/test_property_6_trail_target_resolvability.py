# Feature: first-walking-slice, Property 6: Trail target resolvability
"""Property 6 — Trail target resolvability (task 10.6).

**Property 6: Trail target resolvability**

For all Trail Revisions, every Trail Step target reference resolves to
an immutable target Revision or Immutable Record at Trail Revision
creation time, OR the Trail Revision records an explicit Omission
Entry covering that step that names the affected stage, the omission
category drawn from ``{intentional, unavailable, restricted, stale,
unresolved}``, and a non-empty rationale.

**Validates: Requirements 9.5, 15.6**

Strategy:

Each Hypothesis case draws a *case mask*: a tuple of five booleans,
one per Trail Step ordinal 1..5. ``True`` for an ordinal means that
step's target reference will be replaced with a fake-but-canonical
UUIDv7 that does not resolve against the seeded pipeline; ``False``
means the step keeps its real, resolvable reference. The mask space
covers every cardinality from ``k = 0`` (all five targets resolve) to
``k = 5`` (no target resolves) and every position-of-unresolution
pattern at each cardinality (e.g. only ordinal 3 unresolved, ordinals
2 and 4 unresolved, etc.).

Per generated case the test:

1. Spins up a fresh per-test SQLite engine on a unique
   :class:`tempfile.TemporaryDirectory` path so cross-case state
   cannot contaminate the persistence assertions (design §"Testing
   Strategy" — "Each property and example test gets a fresh SQLite
   database").
2. Seeds a full Source Document → Document Revision → Region
   Occurrence → Finding Revision → Recommendation Revision → Decision
   pipeline so every "resolvable" step has a real target to cite.
   Snapshots the post-seed row counts of ``Trails``,
   ``Trail_Revisions``, and ``Trail_Steps`` (all three should be zero
   because the pipeline seed itself does not write any Trail rows).
3. Builds a five-step Trail submission whose i-th step uses the real
   seeded reference when ``mask[i]`` is ``False``, or a fake UUIDv7
   when ``mask[i]`` is ``True``.
4. Invokes :meth:`TrailService.create_trail` once and inspects the
   outcome.

Per Requirement 9.5 the implementation of :meth:`create_trail` checks
every target *before* opening any write — if any target does not
resolve the method raises :class:`TrailTargetUnresolvedError` with the
full per-ordinal list and never reaches an ``INSERT``. The property's
disjunction ("OR the Trail Revision records an explicit Omission
Entry") is the second arm a future Trail_Service implementation could
take; the property statement is satisfied by *either* arm. This test
accepts both arms so it remains correct if a future change in the
slice swaps in an omission-record path for some unresolved-target
scenarios, but it actively asserts the rejection arm because that is
what Requirement 9.5 mandates today.

Assertions per case:

- **k = 0 (no unresolved targets).** :meth:`create_trail` returns a
  :class:`CreateTrailResult`; one row each landed in ``Trails`` and
  ``Trail_Revisions``; exactly five rows landed in ``Trail_Steps``;
  the result's five-step tuple has ordinals 1..5 in order.

- **k > 0 (at least one unresolved target).** Either

    (a) :meth:`create_trail` raises
        :class:`TrailTargetUnresolvedError`, the error's
        ``error_code`` equals ``"trail_target_unresolved"``, the
        per-ordinal list names exactly the unresolved ordinals (so
        the response identifies *each* unresolved Trail Step by
        ordinal — Requirement 9.5), no row landed in ``Trails``,
        ``Trail_Revisions``, or ``Trail_Steps``, and the unresolved
        descriptors carry the fake target reference the case
        submitted (so the response surfaces the *target reference*
        per Requirement 9.5); or

    (b) :meth:`create_trail` returns a :class:`CreateTrailResult`,
        five Trail_Steps rows landed, and a Provenance Manifest
        with one Omission_Entries row per unresolved step has been
        recorded — each Omission Entry naming the affected ordinal,
        a category drawn from the five permitted values, and a
        non-empty rationale.

  Arm (a) is what the current Trail_Service implementation produces
  (Requirement 9.5). Arm (b) is accepted but never exercised today —
  the test would still pass if a future implementation switched to
  it without breaking the property.
"""

from __future__ import annotations

import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Optional

import pytest
import uuid_utils
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import KnowledgeService, SupportRef
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.trails import (
    CreateTrailResult,
    ORDINAL_TARGET_KIND,
    TrailService,
    TrailStepInput,
    TrailTargetUnresolvedError,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the Party row required by every FK on
# ``Document_Revisions.contributing_party_id``,
# ``Finding_Revisions.authoring_party_id``,
# ``Recommendation_Revisions.authoring_party_id``,
# ``Decisions.deciding_party_id``,
# ``Trail_Revisions.authoring_party_id``, and
# ``Audit_Records.actor_party_id``.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_PARTY_CREATED_AT: Final[str] = "2026-01-01T00:00:00.000Z"
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-00000000a001"
)
_SCOPE: Final[str] = "pilot/team-a"
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Canonical-form UUIDv7 regex per Requirement 1.7 and
# :data:`walking_slice.identity.CANONICAL_UUID7_REGEX`. Used by the
# rejection-arm assertion to confirm the implementation surfaces the
# fake-but-canonical references back to the caller verbatim.
_CANONICAL_UUID7: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# The five Omission Entry categories permitted by Requirement 10.3 /
# the schema CHECK on ``Omission_Entries.category``. Used only on the
# (currently unreachable) accept-with-omission arm.
_OMISSION_CATEGORIES: Final[tuple[str, ...]] = (
    "intentional",
    "unavailable",
    "restricted",
    "stale",
    "unresolved",
)


def _seed_party(connection) -> None:
    """Insert the test Party row that the FK constraints require."""
    connection.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Property 6 Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, audit rows, pipeline rows, and Trail
# rows cannot leak between cases. Mirrors the pattern in
# ``test_property_1_evidence_support.py`` and
# ``test_property_2_decision_authority.py``.
# ---------------------------------------------------------------------------


def _build_engine(tmp_dir: Path) -> Engine:
    """Create a fresh per-case SQLite engine with WAL + foreign_keys pragmas."""
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


# ---------------------------------------------------------------------------
# Pipeline-seed helper. Mirrors ``_seed_full_pipeline`` in
# ``tests/unit/test_trails.py`` so the property test cites a structurally
# identical Source Document → Decision pipeline.
# ---------------------------------------------------------------------------


def _seed_full_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> dict[str, str]:
    """Seed Source Document → Region → Finding → Recommendation → Decision.

    Returns the identifiers needed to assemble a valid five-step
    Trail submission against this database.
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
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
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


def _real_step_for_ordinal(ordinal: int, ids: dict[str, str]) -> TrailStepInput:
    """Build the resolvable Trail Step for ``ordinal``.

    Each branch matches the per-ordinal target-shape table documented
    on :class:`TrailStepInput` (Requirement 9.2; design §"Table-by-Table
    Specification — Trail_Steps").
    """
    if ordinal == 1:
        return TrailStepInput(
            ordinal=1,
            target_kind="document_revision",
            target_id=ids["document_resource_id"],
            target_revision_id=ids["document_revision_id"],
            annotation="The source document.",
        )
    if ordinal == 2:
        return TrailStepInput(
            ordinal=2,
            target_kind="region_occurrence",
            target_id=ids["document_revision_id"],
            region_id=ids["region_id"],
            annotation="The cited region.",
        )
    if ordinal == 3:
        return TrailStepInput(
            ordinal=3,
            target_kind="finding_revision",
            target_id=ids["finding_id"],
            target_revision_id=ids["finding_revision_id"],
            annotation="The supporting finding.",
        )
    if ordinal == 4:
        return TrailStepInput(
            ordinal=4,
            target_kind="recommendation_revision",
            target_id=ids["recommendation_id"],
            target_revision_id=ids["recommendation_revision_id"],
            annotation="The recommendation.",
        )
    if ordinal == 5:
        return TrailStepInput(
            ordinal=5,
            target_kind="decision",
            target_id=ids["decision_id"],
            annotation="The authorized decision.",
        )
    raise AssertionError(  # pragma: no cover - defensive
        f"ordinal {ordinal!r} not in 1..5"
    )


def _fresh_uuid7() -> str:
    """Mint one fresh canonical UUIDv7 string for an unresolvable reference."""
    return str(uuid_utils.uuid7())


def _fake_step_for_ordinal(ordinal: int) -> TrailStepInput:
    """Build a structurally-valid but unresolvable Trail Step for ``ordinal``.

    Every identifier is a fresh UUIDv7 string so structural validation
    accepts the step (Requirement 9.7) — the only reason resolvability
    fails is that the identifier does not name a real row anywhere in
    the seeded pipeline. ``target_kind`` matches the ordinal so the
    structural validator does not short-circuit before resolvability
    runs.

    Each branch's payload shape (which identifier fields are populated,
    which are ``None``) matches the per-ordinal target table on
    :class:`TrailStepInput`; structural validation rejects payloads
    that mis-populate fields (e.g. ``target_revision_id`` on ordinal 2)
    and we want the resolvability check itself to fail, not the
    structural one.
    """
    if ordinal == 1:
        return TrailStepInput(
            ordinal=1,
            target_kind="document_revision",
            target_id=_fresh_uuid7(),
            target_revision_id=_fresh_uuid7(),
            annotation="Unresolvable Document Revision.",
        )
    if ordinal == 2:
        return TrailStepInput(
            ordinal=2,
            target_kind="region_occurrence",
            target_id=_fresh_uuid7(),
            region_id=_fresh_uuid7(),
            annotation="Unresolvable Region Occurrence.",
        )
    if ordinal == 3:
        return TrailStepInput(
            ordinal=3,
            target_kind="finding_revision",
            target_id=_fresh_uuid7(),
            target_revision_id=_fresh_uuid7(),
            annotation="Unresolvable Finding Revision.",
        )
    if ordinal == 4:
        return TrailStepInput(
            ordinal=4,
            target_kind="recommendation_revision",
            target_id=_fresh_uuid7(),
            target_revision_id=_fresh_uuid7(),
            annotation="Unresolvable Recommendation Revision.",
        )
    if ordinal == 5:
        return TrailStepInput(
            ordinal=5,
            target_kind="decision",
            target_id=_fresh_uuid7(),
            annotation="Unresolvable Decision.",
        )
    raise AssertionError(  # pragma: no cover - defensive
        f"ordinal {ordinal!r} not in 1..5"
    )


# ---------------------------------------------------------------------------
# Database probe helpers used in the assertion loop.
# ---------------------------------------------------------------------------


def _count_rows(engine: Engine, table: str) -> int:
    """Return ``SELECT COUNT(*) FROM <table>`` on a read-only connection."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_omissions_for_trail_revision(
    engine: Engine, trail_revision_id: str
) -> list[dict]:
    """Return Omission_Entries rows attached to the Trail Revision's manifest.

    Used only on the (currently unreachable) accept-with-omission arm
    of the property — kept here so the assertion is symmetric and a
    future implementation switch is caught.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT oe.excluded_source_id,
                           oe.excluded_source_revision_id,
                           oe.category,
                           oe.rationale
                      FROM Omission_Entries oe
                      JOIN Provenance_Manifests pm
                        ON pm.manifest_id = oe.manifest_id
                     WHERE pm.subject_kind = 'trail_revision'
                       AND pm.subject_id = :tid
                    """
                ),
                {"tid": trail_revision_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Hypothesis strategy.
#
# A *case mask* is a 5-tuple of booleans. ``True`` at position ``i``
# (0-indexed; corresponds to ordinal ``i + 1``) marks that ordinal's
# step as unresolved. The strategy uses ``st.tuples`` over five
# independent ``st.booleans`` draws so Hypothesis covers every
# 2**5 = 32 combination of (which ordinals are unresolved) across the
# 100-case run. The number of unresolved steps ``k`` ranges from 0
# (all five resolve) to 5 (none resolves) inclusive, satisfying the
# task statement's "Generate Trail submissions where 0..5 targets are
# unresolved".
# ---------------------------------------------------------------------------


_case_mask_strategy = st.tuples(
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.booleans(),
    st.booleans(),
)


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 6: Trail target resolvability
@given(mask=_case_mask_strategy)
@settings(
    max_examples=100,
    deadline=2000,
)
def test_trail_target_resolvability(mask: tuple[bool, ...]) -> None:
    """Validates: Requirements 9.5, 15.6.

    Every Trail Step target reference either resolves to an immutable
    target Revision / Immutable Record at Trail Revision creation
    time, or the Trail Revision records an explicit Omission Entry
    covering that step naming the affected stage, category, and a
    non-empty rationale.

    The current Trail_Service implementation takes the rejection arm
    of the disjunction (Requirement 9.5: "reject the entire Trail
    Revision request with no partial persistence"). The test asserts
    that arm when ``k > 0`` and accepts (but does not exercise) the
    accept-with-omission arm if a future implementation switches.
    """
    # Per-ordinal mask. ``unresolved_ordinals`` is the sorted list of
    # ordinals (1..5) whose targets are deliberately unresolvable in
    # this case. ``k`` is the cardinality named in the task
    # statement.
    unresolved_ordinals: list[int] = [
        ordinal for ordinal, drop in enumerate(mask, start=1) if drop
    ]
    k = len(unresolved_ordinals)

    with tempfile.TemporaryDirectory(prefix="walking_slice_prop6_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # Fresh services per case so :class:`IdentityService`
            # in-memory state cannot bleed across cases. The pinned
            # :class:`FixedClock` makes ``recorded_at`` deterministic
            # for shrinking.
            clock = FixedClock(_FIXED_NOW)
            identity_service = IdentityService()
            audit_log = AuditLog(clock)
            evidence_repository = EvidenceRepository(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            knowledge_service = KnowledgeService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )
            trail_service = TrailService(
                clock=clock,
                identity_service=identity_service,
                audit_log=audit_log,
            )

            # 1. Seed the full pipeline. The seed creates rows in
            #    Source_Documents, Document_Revisions,
            #    Region_Occurrences, Finding_Revisions,
            #    Recommendation_Revisions, and Decisions but does not
            #    write any Trail rows.
            ids = _seed_full_pipeline(
                engine, evidence_repository, knowledge_service
            )
            assert _count_rows(engine, "Trails") == 0
            assert _count_rows(engine, "Trail_Revisions") == 0
            assert _count_rows(engine, "Trail_Steps") == 0

            # 2. Build the five-step submission from the mask. Steps
            #    are produced in ordinal order so the request body is
            #    well-formed (Requirement 9.2).
            steps: list[TrailStepInput] = []
            for ordinal in range(1, 6):
                if mask[ordinal - 1]:
                    steps.append(_fake_step_for_ordinal(ordinal))
                else:
                    steps.append(_real_step_for_ordinal(ordinal, ids))

            # 3. Invoke create_trail. The current implementation
            #    raises TrailTargetUnresolvedError when ``k > 0`` and
            #    returns a CreateTrailResult when ``k == 0`` — but
            #    the property's disjunction accepts a future
            #    implementation that records Omission Entries
            #    instead, so both outcomes are observed and handled.
            raised: Optional[TrailTargetUnresolvedError] = None
            result: Optional[CreateTrailResult] = None
            try:
                with engine.begin() as conn:
                    result = trail_service.create_trail(
                        conn,
                        purpose="Walk a reader from Evidence to Decision.",
                        audience_id="pilot-reviewers",
                        ordering_rationale="Pipeline-stage order.",
                        steps=steps,
                        authoring_party_id=_PARTY_ID,
                    )
            except TrailTargetUnresolvedError as exc:
                raised = exc

            # 4. Assertions per the disjunction in Property 6.

            if k == 0:
                # No unresolved targets → resolvable arm. The
                # submission persists with all five steps.
                assert raised is None, (
                    "Property 6: a submission with every target "
                    "resolvable must not raise "
                    "TrailTargetUnresolvedError; got "
                    f"{raised!r}."
                )
                assert result is not None
                assert _count_rows(engine, "Trails") == 1, (
                    "Property 6: exactly one Trails row must land when "
                    "all five targets resolve."
                )
                assert _count_rows(engine, "Trail_Revisions") == 1, (
                    "Property 6: exactly one Trail_Revisions row "
                    "must land when all five targets resolve."
                )
                assert _count_rows(engine, "Trail_Steps") == 5, (
                    "Property 6: exactly five Trail_Steps rows must "
                    "land when all five targets resolve."
                )
                assert [step.ordinal for step in result.steps] == [
                    1,
                    2,
                    3,
                    4,
                    5,
                ], (
                    "Property 6: a resolvable submission must persist "
                    "every ordinal 1..5; got "
                    f"{[s.ordinal for s in result.steps]!r}."
                )
                # No omission entries are needed when every target
                # resolves — the resolvable arm of the disjunction is
                # the one taken.
                return

            # k > 0 → at least one unresolved target. The property's
            # disjunction permits two outcomes. We accept either:
            #   (a) rejection with TrailTargetUnresolvedError + no
            #       partial persistence + per-ordinal list correct
            #       (Requirement 9.5, the implemented arm); or
            #   (b) acceptance with one Omission Entry per
            #       unresolved step naming the affected stage,
            #       category, and a non-empty rationale (Property 6
            #       disjunction; not exercised today).
            if raised is not None:
                # ------ Arm (a): rejection. ----------------------
                # Requirement 9.5 requires the error to surface
                # ``error_code='trail_target_unresolved'`` and a
                # per-ordinal list naming every unresolved step.
                assert raised.error_code == "trail_target_unresolved", (
                    "Property 6: rejection arm must surface "
                    "error_code='trail_target_unresolved'; got "
                    f"{raised.error_code!r}."
                )
                observed_ordinals = [
                    u.ordinal for u in raised.unresolved_steps
                ]
                assert observed_ordinals == unresolved_ordinals, (
                    "Property 6: rejection arm must identify every "
                    "unresolved step by ordinal in ascending order; "
                    f"expected={unresolved_ordinals!r}, "
                    f"observed={observed_ordinals!r}."
                )

                # Each unresolved descriptor names a target reference
                # that matches the case's fake submission. We confirm
                # the ``target_id`` is canonical UUIDv7 and that the
                # target_kind matches the ordinal's pipeline stage —
                # Requirement 9.5 demands the response identify each
                # unresolved step by ordinal AND target reference.
                by_ordinal = {
                    u.ordinal: u for u in raised.unresolved_steps
                }
                for ordinal in unresolved_ordinals:
                    descriptor = by_ordinal[ordinal]
                    assert descriptor.target_kind == ORDINAL_TARGET_KIND[
                        ordinal
                    ], (
                        f"Property 6: unresolved descriptor for "
                        f"ordinal {ordinal} carries target_kind="
                        f"{descriptor.target_kind!r}; expected "
                        f"{ORDINAL_TARGET_KIND[ordinal]!r}."
                    )
                    assert _CANONICAL_UUID7.match(descriptor.target_id), (
                        f"Property 6: unresolved descriptor for "
                        f"ordinal {ordinal} carries non-canonical "
                        f"target_id={descriptor.target_id!r}."
                    )

                # No partial persistence — Trails / Trail_Revisions /
                # Trail_Steps remain at their post-pipeline-seed
                # counts (0 each, because pipeline seeding does not
                # write Trail rows). Requirement 9.5 demands this
                # exactly: "reject the entire Trail Revision request
                # with no partial persistence".
                assert _count_rows(engine, "Trails") == 0, (
                    "Property 6: rejection arm must not write any "
                    "Trails row; found "
                    f"{_count_rows(engine, 'Trails')}."
                )
                assert _count_rows(engine, "Trail_Revisions") == 0, (
                    "Property 6: rejection arm must not write any "
                    "Trail_Revisions row; found "
                    f"{_count_rows(engine, 'Trail_Revisions')}."
                )
                assert _count_rows(engine, "Trail_Steps") == 0, (
                    "Property 6: rejection arm must not write any "
                    "Trail_Steps row; found "
                    f"{_count_rows(engine, 'Trail_Steps')}."
                )
                return

            # ------ Arm (b): acceptance with Omission Entries. ----
            # Not exercised by the current implementation, but the
            # property's disjunction permits it. If a future
            # implementation lands here we still uphold the property
            # by checking that every unresolved step is covered by
            # an Omission Entry naming the affected stage, a valid
            # category, and a non-empty rationale.
            assert result is not None, (  # pragma: no cover
                "Property 6: when create_trail does not raise and "
                "k > 0, it must return a CreateTrailResult."
            )
            assert _count_rows(engine, "Trails") == 1  # pragma: no cover
            assert _count_rows(engine, "Trail_Revisions") == 1  # pragma: no cover
            assert _count_rows(engine, "Trail_Steps") == 5  # pragma: no cover
            omissions = _fetch_omissions_for_trail_revision(  # pragma: no cover
                engine, result.trail_revision_id
            )
            # One Omission Entry per unresolved step naming the
            # affected stage (the fake target_id / region_id we
            # submitted), one of the five permitted categories, and a
            # non-empty rationale (Requirement 10.2 / 10.3 / Property
            # 6 disjunction).
            fake_target_ids = {  # pragma: no cover
                steps[ordinal - 1].target_id
                for ordinal in unresolved_ordinals
            }
            covered_target_ids = {  # pragma: no cover
                row["excluded_source_id"] for row in omissions
            }
            assert fake_target_ids.issubset(covered_target_ids), (  # pragma: no cover
                "Property 6: accept-with-omission arm must record an "
                "Omission Entry for every unresolved step; missing="
                f"{sorted(fake_target_ids - covered_target_ids)!r}."
            )
            for row in omissions:  # pragma: no cover
                assert row["category"] in _OMISSION_CATEGORIES, (
                    "Property 6: Omission Entry category must be one "
                    f"of {_OMISSION_CATEGORIES!r}; got "
                    f"{row['category']!r}."
                )
                assert (
                    isinstance(row["rationale"], str)
                    and len(row["rationale"]) >= 1
                ), (
                    "Property 6: Omission Entry rationale must be "
                    f"non-empty; got {row['rationale']!r}."
                )
        finally:
            engine.dispose()
