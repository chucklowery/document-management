# Feature: first-walking-slice, Property 5: Trail linearity
"""Property 5 — Trail linearity (task 10.5).

**Property 5: Trail linearity**

For all Trail Revisions created within this slice, the ordered Trail
Steps form one totally-ordered sequence of exactly five Trail Steps
with exactly one step at each of the five pipeline stages (Document
Revision, Region Occurrence, Finding Revision, Recommendation
Revision, Decision), no Trail Step carries an alternative or branch
attribute, the ``selection_mode`` of every step is ``Pinned``, and the
ordinals are the contiguous integers 1, 2, 3, 4, 5.

**Validates: Requirements 9.1, 9.2, 9.3, 9.7, 15.5**

Strategy:

Each Hypothesis case (a) seeds one full pipeline (Source Document →
Document Revision → Region Occurrence → Finding → Recommendation →
Decision) so every ordinal has a real, resolvable target, then (b)
makes a sequence of Trail-creation *attempts* whose step lists are
canonical 5-step inputs perturbed by one of a small fixed set of
mutations:

- ``none`` — the canonical 5-step input (always valid).
- ``drop_step`` — remove one step (Requirement 9.1 / 9.7
  ``step_count_invalid`` rejection).
- ``add_step`` — append a sixth step (same rejection path).
- ``swap_kinds`` — swap two steps' ``target_kind`` values while
  keeping their ordinals (Requirement 9.2 / 9.7
  ``target_kind_invalid_for_ordinal`` rejection unless the indices
  coincide).
- ``shift_ordinal`` — reassign one step's ordinal to a drawn value in
  ``[0, 7]`` (rejection paths ``ordinals_not_contiguous_1_to_5`` and,
  when the new ordinal collides with another step's ordinal,
  ``target_kind_invalid_for_ordinal``; when the new ordinal happens
  to equal the original the attempt remains valid).
- ``duplicate_ordinal`` — force one step's ordinal to match another's
  (always produces a non-contiguous ordinal set unless the source and
  target indices coincide).

The mutation alphabet is deliberately closed: every value either
yields a valid Trail (so a row lands and the property is exercised
positively) or fires one of the structural-validation rejection paths
in :class:`walking_slice.trails.TrailService._validate_steps`. The
strategy keeps the index parameters bounded to ``[0, 4]`` and the
ordinal parameters to ``[0, 7]`` so the input space is small enough
for Hypothesis to cover every mutation × index combination within a
100-case budget while still spanning the rejection branches Requirements
9.2 / 9.7 enumerate (off-by-one ordinal at either end, duplicate
ordinal, mismatched kind).

Identifier shape for each step is derived from its drawn
``target_kind`` (not its drawn ordinal) so a structurally-valid trail
also passes the per-step identifier validators
(:meth:`TrailService._validate_step_identifiers`). When ``target_kind``
and ordinal disagree, ``_validate_step_target_kind`` raises first —
the identifier validators never see the inconsistency. This keeps the
strategy focused on Property 5's quantifier ("for all *persisted*
Trail Revisions") rather than incidentally exercising the identifier
validators (which are covered separately by task 10.4).

Per case the test spins up a fresh per-test SQLite engine on a unique
:class:`tempfile.TemporaryDirectory` path (design §"Testing Strategy"
— per-case database isolation), runs every attempt against the wired
:class:`TrailService`, and then queries ``Trail_Revisions`` and
``Trail_Steps`` directly. For every persisted Trail Revision row the
assertion loop checks four invariants:

1. The number of ``Trail_Steps`` rows attached to the Revision equals
   exactly five (Requirement 9.1, 9.7).
2. The ordinals across those rows are the contiguous integers
   ``[1, 2, 3, 4, 5]`` in ascending order (Requirement 9.2, 9.7).
3. Each row's ``target_kind`` matches the kind required for its
   ordinal per :data:`walking_slice.trails.ORDINAL_TARGET_KIND`
   (Requirement 9.2, 9.7).
4. Each row's ``selection_mode`` is ``'Pinned'`` (Requirement 9.3 /
   AD-WS-12).

The assertion is run *post-hoc* by reading the database directly
(rather than relying on the in-memory :class:`CreateTrailResult` /
:class:`AppendTrailRevisionResult` return values) so the property
catches any future regression that lands a Trail Revision past the
structural validators — for example, a code path that bypasses
:meth:`TrailService._validate_steps`, or a schema migration that
weakens the ``Trail_Steps`` CHECK constraint.

Test scaffolding follows the conventions established by
``tests/property/test_property_1_evidence_support.py``,
``tests/property/test_property_2_decision_authority.py``, and
``tests/property/test_property_7_provenance_non_omission.py``:

- :class:`tempfile.TemporaryDirectory` owns the per-case SQLite file
  (function-scoped pytest fixtures would not reset between Hypothesis
  cases).
- The :class:`~walking_slice.clock.FixedClock` is pinned to
  ``2026-01-01T00:00:00.000Z`` so every recorded timestamp is
  deterministic across shrinks.
- An :class:`~walking_slice.identity.IdentityService` constructed
  without an engine in in-memory mode is fine — Trail/Step
  identifiers still bind to ``Identifier_Registry`` via the
  connection-aware persistent path inside
  :meth:`TrailService.create_trail`.
- ``@settings(max_examples=100, deadline=2000)`` per Requirement
  15.13.
"""

from __future__ import annotations

import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.clock import FixedClock
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    KnowledgeService,
    SupportRef,
)
from walking_slice.models import AuthorityBasisRef
from walking_slice.persistence import create_schema
from walking_slice.trails import (
    ORDINAL_TARGET_KIND,
    TrailService,
    TrailStepInput,
    TrailTargetUnresolvedError,
    TrailValidationError,
)


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Fixed constants.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_PARTY_CREATED_AT: Final[str] = "2026-01-01T00:00:00.000Z"
# Property 5 does not exercise Decision authority; the basis is just
# the FK target required by :class:`AuthorityBasisRef`.
_AUTHORITY_BASIS_ID: Final[uuid.UUID] = uuid.UUID(
    "00000000-0000-7000-8000-00000000a005"
)
_SCOPE: Final[str] = "property-5/scope"
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Valid target_kind alphabet — the five values mapped by
# :data:`ORDINAL_TARGET_KIND`. Centralized so the mutation strategy
# and the post-hoc kind-matches-ordinal assertion read from the same
# source of truth.
_VALID_KINDS: Final[tuple[str, ...]] = tuple(ORDINAL_TARGET_KIND.values())


def _seed_party(conn) -> None:
    """Insert the test Party row required by every Party FK in the seed."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Property 5 Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Pipeline seeding.
#
# A fresh Source Document → Document Revision → Region Occurrence →
# Finding → Recommendation → Decision is produced per case so every
# Trail Step ordinal has a real, resolvable target. The pipeline is
# built once per case (not per attempt) because the Property-5
# quantifier ranges over *Trail Revisions*, not over distinct source
# pipelines; reusing the same five targets across attempts is
# semantically fine (each Trail mints its own Trail Resource Identity
# and Step Identities) and keeps per-case setup cheap.
# ---------------------------------------------------------------------------


def _seed_pipeline(
    engine: Engine,
    evidence_repository: EvidenceRepository,
    knowledge_service: KnowledgeService,
) -> dict[str, str]:
    """Seed one full pipeline and return the identifiers each ordinal cites."""
    with engine.begin() as conn:
        _seed_party(conn)
        doc = evidence_repository.create_document(
            conn,
            content_bytes=b"Walking slice content for Property 5.",
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
            statement="Evidence-backed claim for Property 5.",
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
            rationale="Recommendation derived from the Property 5 Finding.",
        )
        decision = knowledge_service.create_decision(
            conn,
            target_recommendation_id=recommendation.recommendation_id,
            target_recommendation_revision_id=(
                recommendation.recommendation_revision_id
            ),
            outcome="Accept",
            rationale="Approve based on the recommendation.",
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


# ---------------------------------------------------------------------------
# Step construction.
#
# Identifier shape is driven by ``target_kind`` (not by ``ordinal``) so
# a structurally-valid trail produced by the mutation strategy also
# passes the per-step identifier validators. The mapping mirrors the
# per-ordinal interpretation table in :class:`TrailStepInput`'s
# docstring exactly.
# ---------------------------------------------------------------------------


def _step_for_kind(
    ids: dict[str, str],
    *,
    ordinal: int,
    target_kind: str,
    annotation: Optional[str] = None,
) -> TrailStepInput:
    """Build a TrailStepInput with identifier shape matching ``target_kind``."""
    if target_kind == "document_revision":
        return TrailStepInput(
            ordinal=ordinal,
            target_kind="document_revision",
            target_id=ids["document_resource_id"],
            target_revision_id=ids["document_revision_id"],
            annotation=annotation,
        )
    if target_kind == "region_occurrence":
        return TrailStepInput(
            ordinal=ordinal,
            target_kind="region_occurrence",
            target_id=ids["document_revision_id"],
            region_id=ids["region_id"],
            annotation=annotation,
        )
    if target_kind == "finding_revision":
        return TrailStepInput(
            ordinal=ordinal,
            target_kind="finding_revision",
            target_id=ids["finding_id"],
            target_revision_id=ids["finding_revision_id"],
            annotation=annotation,
        )
    if target_kind == "recommendation_revision":
        return TrailStepInput(
            ordinal=ordinal,
            target_kind="recommendation_revision",
            target_id=ids["recommendation_id"],
            target_revision_id=ids["recommendation_revision_id"],
            annotation=annotation,
        )
    if target_kind == "decision":
        return TrailStepInput(
            ordinal=ordinal,
            target_kind="decision",
            target_id=ids["decision_id"],
            annotation=annotation,
        )
    raise AssertionError(  # pragma: no cover - defensive
        f"unknown target_kind: {target_kind!r}"
    )


def _canonical_steps(ids: dict[str, str]) -> list[TrailStepInput]:
    """Return the canonical 5-step Trail input — ordinals 1..5 with matched kinds."""
    return [
        _step_for_kind(ids, ordinal=1, target_kind="document_revision"),
        _step_for_kind(ids, ordinal=2, target_kind="region_occurrence"),
        _step_for_kind(ids, ordinal=3, target_kind="finding_revision"),
        _step_for_kind(ids, ordinal=4, target_kind="recommendation_revision"),
        _step_for_kind(ids, ordinal=5, target_kind="decision"),
    ]


# ---------------------------------------------------------------------------
# Mutation strategy.
#
# Each attempt draws a mutation name plus a small bundle of index /
# ordinal / kind parameters consumed only by the mutations that need
# them. Drawing everything upfront keeps the strategy flat and the
# shrinking deterministic: a failing case shrinks to the smallest
# mutation × parameter combination that breaks the property.
# ---------------------------------------------------------------------------


_MUTATIONS: Final[tuple[str, ...]] = (
    "none",
    "drop_step",
    "add_step",
    "swap_kinds",
    "shift_ordinal",
    "duplicate_ordinal",
)


@st.composite
def _attempt_strategy(draw) -> dict[str, Any]:
    """Draw one Trail-creation attempt descriptor.

    Returns a dict with the mutation name and the auxiliary parameters
    every mutation might consume. Parameters not consumed by the drawn
    mutation are simply ignored (drawing them all unconditionally keeps
    the strategy and the shrinker deterministic).
    """
    return {
        "mutation": draw(st.sampled_from(_MUTATIONS)),
        # Indices into the 5-step canonical list ([0..4]).
        "drop_index": draw(st.integers(min_value=0, max_value=4)),
        "swap_a": draw(st.integers(min_value=0, max_value=4)),
        "swap_b": draw(st.integers(min_value=0, max_value=4)),
        "shift_index": draw(st.integers(min_value=0, max_value=4)),
        "dup_source": draw(st.integers(min_value=0, max_value=4)),
        "dup_target": draw(st.integers(min_value=0, max_value=4)),
        # Ordinal alphabet covers the valid range (1..5) plus the
        # off-by-one boundaries (0, 6, 7) Requirement 9.7 calls out.
        "shift_to": draw(st.integers(min_value=0, max_value=7)),
        "add_ordinal": draw(st.integers(min_value=0, max_value=7)),
        # Drawn from the full target_kind alphabet so the appended
        # step (when ``mutation == 'add_step'``) can be any of the
        # five valid kinds.
        "add_kind": draw(st.sampled_from(_VALID_KINDS)),
    }


# Each scenario is 1..5 attempts; min_size=1 guarantees at least one
# Trail-creation invocation per case (a case with zero attempts would
# leave the assertion loop vacuously true and waste Hypothesis budget).
_scenario_strategy = st.lists(_attempt_strategy(), min_size=1, max_size=5)


def _apply_mutation(
    ids: dict[str, str], attempt: dict[str, Any]
) -> list[TrailStepInput]:
    """Apply the requested mutation to the canonical 5-step list.

    Each branch corresponds to one entry in :data:`_MUTATIONS`. Every
    branch returns a fresh list — the canonical steps are not mutated
    in place — so a downstream caller iterating multiple attempts in
    one case sees a clean starting point each time.
    """
    steps = _canonical_steps(ids)
    mutation = attempt["mutation"]

    if mutation == "none":
        return steps

    if mutation == "drop_step":
        i = attempt["drop_index"]
        # ``drop_index`` is already constrained to [0, 4]; the mod
        # is defensive against future refactors that widen the
        # parameter range.
        return [s for j, s in enumerate(steps) if j != (i % len(steps))]

    if mutation == "add_step":
        # Inject a sixth step with an arbitrary kind and ordinal.
        # Steps with ``ordinal == 0`` or ``ordinal in {6, 7}`` are
        # always rejected by the ordinal-set check; steps with an
        # in-range ordinal (1..5) duplicate one of the canonical
        # ordinals and are also rejected. Either way the structural
        # validator returns before any database round-trip.
        extra = _step_for_kind(
            ids,
            ordinal=attempt["add_ordinal"],
            target_kind=attempt["add_kind"],
        )
        return steps + [extra]

    if mutation == "swap_kinds":
        a = attempt["swap_a"]
        b = attempt["swap_b"]
        if a == b:
            # Swapping a step with itself is a no-op; the resulting
            # input is the canonical 5-step (valid). Returning the
            # canonical list keeps the mutation total: every drawn
            # value maps to a defined step list.
            return steps
        ord_a = steps[a].ordinal
        ord_b = steps[b].ordinal
        kind_a = steps[a].target_kind
        kind_b = steps[b].target_kind
        mutated: list[TrailStepInput] = []
        for j, s in enumerate(steps):
            if j == a:
                mutated.append(
                    _step_for_kind(ids, ordinal=ord_a, target_kind=kind_b)
                )
            elif j == b:
                mutated.append(
                    _step_for_kind(ids, ordinal=ord_b, target_kind=kind_a)
                )
            else:
                mutated.append(s)
        return mutated

    if mutation == "shift_ordinal":
        i = attempt["shift_index"]
        new_ord = attempt["shift_to"]
        # Replace step ``i``'s ordinal with the drawn value while
        # leaving its ``target_kind`` (and therefore its identifier
        # shape) intact. When ``new_ord`` happens to equal the
        # original ordinal the step is unchanged and the input
        # remains valid; when ``new_ord`` is outside [1, 5] or
        # collides with another step's ordinal the ordinal-set check
        # rejects the submission.
        kind = steps[i].target_kind
        return [
            _step_for_kind(ids, ordinal=new_ord, target_kind=kind)
            if j == i
            else s
            for j, s in enumerate(steps)
        ]

    if mutation == "duplicate_ordinal":
        a = attempt["dup_source"]
        b = attempt["dup_target"]
        if a == b:
            # Self-duplicate is a no-op; the canonical list is valid.
            return steps
        new_b_ordinal = steps[a].ordinal
        kind = steps[b].target_kind
        return [
            _step_for_kind(ids, ordinal=new_b_ordinal, target_kind=kind)
            if j == b
            else s
            for j, s in enumerate(steps)
        ]

    raise AssertionError(  # pragma: no cover - defensive
        f"unknown mutation: {mutation!r}"
    )


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, audit rows, source pipelines, and
# Trail rows cannot leak between cases (design §"Testing Strategy" —
# "Each property and example test gets a fresh SQLite database"). A
# :class:`tempfile.TemporaryDirectory` context inside the test body
# owns the per-case directory; Hypothesis disallows function-scoped
# pytest fixtures for per-case state because they would not reset
# between generated inputs.
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
# Database probe helpers used in the assertion loop.
# ---------------------------------------------------------------------------


def _fetch_trail_revisions(engine: Engine) -> list[dict[str, Any]]:
    """Return every persisted Trail Revision in stable append order.

    The order is determined by ``recorded_at`` first and the Revision
    Identity second so two Revisions sharing one recorded timestamp
    (which is the common case here — :class:`FixedClock` pins every
    write to the same instant) still come back in a deterministic
    order across runs.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT trail_revision_id, trail_id, recorded_at
                      FROM Trail_Revisions
                     ORDER BY recorded_at, trail_revision_id
                    """
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_steps_for_revision(
    engine: Engine, *, trail_revision_id: str
) -> list[dict[str, Any]]:
    """Return every ``Trail_Steps`` row attached to one Trail Revision."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT trail_step_id, ordinal, selection_mode,
                           target_kind, target_id, target_revision_id,
                           region_id, annotation
                      FROM Trail_Steps
                     WHERE trail_revision_id = :trev
                     ORDER BY ordinal
                    """
                ),
                {"trev": trail_revision_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 5: Trail linearity
@given(scenario=_scenario_strategy)
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates a fresh temp directory, a fresh SQLite
    # database, and seeds a full pipeline (Document Revision through
    # Decision). The setup runs well inside the 2 s deadline locally
    # but the data-generation health check can flag it on slower CI
    # hosts; suppressing it keeps the run deterministic without
    # weakening the per-case timing budget.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_trail_linearity(scenario: list[dict[str, Any]]) -> None:
    """Every persisted Trail Revision has exactly five Trail Steps,
    one per pipeline stage (ordinals 1..5 with matched target kinds),
    and ``selection_mode='Pinned'`` on every step."""
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop5_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)
        try:
            # Fresh services per case so :class:`IdentityService`
            # in-memory state cannot bleed across cases. The
            # :class:`FixedClock` keeps every recorded timestamp
            # deterministic for Hypothesis shrinks.
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

            # Seed the pipeline once; every attempt cites the same
            # five resolvable targets.
            ids = _seed_pipeline(
                engine, evidence_repository, knowledge_service
            )

            for attempt_index, attempt in enumerate(scenario):
                steps = _apply_mutation(ids, attempt)
                try:
                    with engine.begin() as conn:
                        trail_service.create_trail(
                            conn,
                            purpose=(
                                f"Property 5 trail attempt {attempt_index}."
                            ),
                            audience_id="property-5-audience",
                            ordering_rationale="Linear pipeline order.",
                            steps=steps,
                            authoring_party_id=_PARTY_ID,
                        )
                except TrailValidationError:
                    # Expected for every mutation that produced a
                    # structurally-invalid input. The property holds
                    # vacuously because no Trail Revision was
                    # persisted (Requirement 9.7 — "decline to create
                    # a Trail Revision").
                    pass
                except TrailTargetUnresolvedError:
                    # Defensive: the strategy always cites seeded
                    # pipeline targets so resolvability cannot fail
                    # in practice, but catching this keeps the
                    # property test focused on Property 5's
                    # linearity quantifier rather than Property 6's
                    # resolvability quantifier (task 10.6 covers
                    # resolvability separately).
                    pass

            # ----- Property assertions -------------------------------
            # Read the persisted state directly so the property
            # catches any regression that lets a malformed Trail
            # Revision past the structural validators.
            revisions = _fetch_trail_revisions(engine)
            for revision in revisions:
                trev_id = revision["trail_revision_id"]
                step_rows = _fetch_steps_for_revision(
                    engine, trail_revision_id=trev_id
                )

                # Invariant 1 — exactly five Trail Steps per Revision.
                assert len(step_rows) == 5, (
                    f"Trail Revision {trev_id!r} has "
                    f"{len(step_rows)} Trail_Steps rows; Property 5 "
                    "requires exactly five steps per Revision "
                    "(Requirements 9.1, 9.7)."
                )

                # Invariant 2 — ordinals are the contiguous integers
                # 1..5 in ascending order (Requirement 9.2, 9.7).
                ordinals = [row["ordinal"] for row in step_rows]
                assert ordinals == [1, 2, 3, 4, 5], (
                    f"Trail Revision {trev_id!r} has ordinals "
                    f"{ordinals!r}; Property 5 requires the "
                    "contiguous integers [1, 2, 3, 4, 5] in "
                    "ascending order (Requirements 9.2, 9.7)."
                )

                # Invariants 3 and 4 — target_kind matches the
                # expected kind for each ordinal, and selection_mode
                # is 'Pinned' on every step.
                for row in step_rows:
                    expected_kind = ORDINAL_TARGET_KIND[row["ordinal"]]
                    assert row["target_kind"] == expected_kind, (
                        f"Trail Step {row['trail_step_id']!r} "
                        f"(ordinal {row['ordinal']}) on Revision "
                        f"{trev_id!r} has target_kind="
                        f"{row['target_kind']!r}; Property 5 / "
                        "Requirement 9.2 require "
                        f"{expected_kind!r} for ordinal "
                        f"{row['ordinal']}."
                    )
                    assert row["selection_mode"] == "Pinned", (
                        f"Trail Step {row['trail_step_id']!r} on "
                        f"Revision {trev_id!r} has selection_mode="
                        f"{row['selection_mode']!r}; Property 5 / "
                        "AD-WS-12 / Requirement 9.3 require "
                        "'Pinned' on every Trail Step."
                    )
        finally:
            engine.dispose()
