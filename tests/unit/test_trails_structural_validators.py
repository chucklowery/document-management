"""Unit tests for :mod:`walking_slice.trails` structural validators
(task 10.4 — the dedicated edge-case sweep).

These tests pin Requirement 9.7 (the structural-validation contract):

> IF a Trail submission contains fewer than 5 or more than 5 Trail
> Steps, contains ordinals that are not the contiguous integers 1
> through 5, or contains a Trail Step whose target kind does not
> match the pipeline stage of its ordinal, THEN THE Trail_Service
> SHALL reject the submission, decline to create a Trail Revision,
> and return an error indication identifying the structural
> validation failure.

…together with AD-WS-12 (slice-restricted ``selection_mode = 'Pinned'``;
Live and Approval-Controlled modes are deferred).

The companion file ``test_trails.py`` covers the happy path, target
resolvability, and the audit / row-count smoke tests for task 10.1.
This file is the dedicated edge-case sweep called out in that file's
module docstring: it deliberately enumerates every combination the
validator must reject, so a regression that loosens one branch of the
structural validator surfaces here as a parametrized failure rather
than being missed.

Structural validation runs before any database round-trip
(``TrailService.create_trail`` step #1; see the method docstring), so
the tests need not seed the pipeline — synthetic but well-formed
UUID-shaped identifiers are enough. A per-test SQLAlchemy connection
is still required because ``create_trail`` accepts one positionally,
but the connection is never consumed for SELECT/INSERT before the
validator raises.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine

from walking_slice.trails import (
    ORDINAL_TARGET_KIND,
    TrailService,
    TrailStepInput,
    TrailValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and synthetic identifiers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"

# Synthetic UUIDv7-shaped identifiers. Structural validation runs
# before target resolvability so these never need to resolve against
# real rows in the slice tables.
_DOC_ID = "00000000-0000-7000-8000-000000000a01"
_DOC_REV_ID = "00000000-0000-7000-8000-000000000a02"
_REGION_ID = "00000000-0000-7000-8000-000000000a03"
_FINDING_ID = "00000000-0000-7000-8000-000000000a04"
_FINDING_REV_ID = "00000000-0000-7000-8000-000000000a05"
_REC_ID = "00000000-0000-7000-8000-000000000a06"
_REC_REV_ID = "00000000-0000-7000-8000-000000000a07"
_DECISION_ID = "00000000-0000-7000-8000-000000000a08"


# ---------------------------------------------------------------------------
# Step builders.
#
# These return TrailStepInput entries shaped per-ordinal exactly as the
# validator's _validate_step_identifiers expects, so any rejection the
# tests observe comes from the specific structural property the test
# is exercising — not from an accidental shape mismatch on a different
# step.
# ---------------------------------------------------------------------------


def _step_ordinal_1(
    *,
    target_kind: str = "document_revision",
    selection_mode: str = "Pinned",
) -> TrailStepInput:
    return TrailStepInput(
        ordinal=1,
        target_kind=target_kind,
        target_id=_DOC_ID,
        target_revision_id=_DOC_REV_ID,
        selection_mode=selection_mode,
    )


def _step_ordinal_2(
    *,
    target_kind: str = "region_occurrence",
    selection_mode: str = "Pinned",
) -> TrailStepInput:
    return TrailStepInput(
        ordinal=2,
        target_kind=target_kind,
        target_id=_DOC_REV_ID,
        region_id=_REGION_ID,
        selection_mode=selection_mode,
    )


def _step_ordinal_3(
    *,
    target_kind: str = "finding_revision",
    selection_mode: str = "Pinned",
) -> TrailStepInput:
    return TrailStepInput(
        ordinal=3,
        target_kind=target_kind,
        target_id=_FINDING_ID,
        target_revision_id=_FINDING_REV_ID,
        selection_mode=selection_mode,
    )


def _step_ordinal_4(
    *,
    target_kind: str = "recommendation_revision",
    selection_mode: str = "Pinned",
) -> TrailStepInput:
    return TrailStepInput(
        ordinal=4,
        target_kind=target_kind,
        target_id=_REC_ID,
        target_revision_id=_REC_REV_ID,
        selection_mode=selection_mode,
    )


def _step_ordinal_5(
    *,
    target_kind: str = "decision",
    selection_mode: str = "Pinned",
) -> TrailStepInput:
    return TrailStepInput(
        ordinal=5,
        target_kind=target_kind,
        target_id=_DECISION_ID,
        selection_mode=selection_mode,
    )


# Per-ordinal builder lookup. Indexed 1..5 so test parametrization can
# substitute one step at a time while leaving the other four valid.
_STEP_BUILDERS = {
    1: _step_ordinal_1,
    2: _step_ordinal_2,
    3: _step_ordinal_3,
    4: _step_ordinal_4,
    5: _step_ordinal_5,
}


def _five_valid_steps() -> list[TrailStepInput]:
    """A valid five-step submission used as the starting point for
    edge-case mutations."""
    return [
        _step_ordinal_1(),
        _step_ordinal_2(),
        _step_ordinal_3(),
        _step_ordinal_4(),
        _step_ordinal_5(),
    ]


def _call_create_trail(
    engine: Engine,
    trail_service: TrailService,
    steps: list[TrailStepInput],
    *,
    purpose: str = "Edge-case structural validator sweep.",
    audience_id: str = "pilot-reviewers",
) -> None:
    """Invoke ``create_trail`` for the edge-case sweep.

    Wrapped so each parametrized test reads as one line. The
    structural validator raises before the connection is read from
    or written to, so the surrounding ``engine.begin()`` is purely
    plumbing.
    """
    with engine.begin() as conn:
        trail_service.create_trail(
            conn,
            purpose=purpose,
            audience_id=audience_id,
            steps=steps,
            authoring_party_id=_PARTY_ID,
        )


# ---------------------------------------------------------------------------
# Step-count edge cases (Requirement 9.7 — "fewer than 5 or more than 5").
# ---------------------------------------------------------------------------


def test_four_step_submission_is_rejected(
    engine: Engine, trail_service: TrailService
) -> None:
    """4-step submission → ``step_count_invalid``.

    Requirement 9.7's "fewer than 5" branch. The validator must fire
    before the ordinal-set check (which would also notice the missing
    ordinal 5) so the failing constraint identifies the count, not
    the ordinal set.
    """
    steps = _five_valid_steps()[:4]
    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "step_count_invalid"


def test_six_step_submission_is_rejected(
    engine: Engine, trail_service: TrailService
) -> None:
    """6-step submission → ``step_count_invalid``.

    Requirement 9.7's "more than 5" branch. The sixth step duplicates
    ordinal 5 (a Decision step). The validator must reject on count
    rather than allowing the duplicate-ordinal path to surface.
    """
    steps = _five_valid_steps()
    # A second decision step — fabricated identifier so the test does
    # not accidentally collide with a fixture.
    extra_decision = TrailStepInput(
        ordinal=5,
        target_kind="decision",
        target_id="00000000-0000-7000-8000-000000000a09",
    )
    steps.append(extra_decision)
    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "step_count_invalid"


@pytest.mark.parametrize("step_count", [0, 1, 2, 3, 7, 10])
def test_other_step_counts_are_rejected(
    engine: Engine,
    trail_service: TrailService,
    step_count: int,
) -> None:
    """Counts other than exactly 5 → ``step_count_invalid``.

    Requirement 9.7 reads "fewer than 5 or more than 5". The boundary
    cases that matter operationally are 4 and 6 (covered above); this
    parametrized test sweeps a representative span to lock the
    "exactly 5" reading.
    """
    valid_steps = _five_valid_steps()
    if step_count <= 5:
        steps = valid_steps[:step_count]
    else:
        # Extend with extra ordinal-5 placeholders. Any shape suffices
        # because the count check runs before the ordinal-set check.
        steps = list(valid_steps)
        for i in range(step_count - 5):
            steps.append(
                TrailStepInput(
                    ordinal=5,
                    target_kind="decision",
                    target_id=f"00000000-0000-7000-8000-00000000ff{i:02x}",
                )
            )
    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "step_count_invalid"


# ---------------------------------------------------------------------------
# Non-contiguous ordinal edge cases (Requirement 9.7).
# ---------------------------------------------------------------------------


# Each case has exactly 5 steps so the step-count check passes and the
# ordinal-set check is the one we are exercising. ``ids`` make the
# parametrize output read as the failing ordinal pattern.
_NON_CONTIGUOUS_ORDINAL_SETS = [
    (1, 2, 3, 3, 5),  # duplicate ordinal 3 (missing 4)
    (1, 2, 3, 5, 5),  # duplicate ordinal 5 (missing 4)
    (1, 1, 2, 3, 4),  # duplicate ordinal 1 (missing 5)
    (1, 2, 3, 4, 6),  # missing 5, extra 6
    (0, 1, 2, 3, 4),  # off-by-one low (missing 5)
    (2, 3, 4, 5, 6),  # off-by-one high (missing 1)
    (1, 2, 3, 4, 4),  # duplicate 4 (missing 5)
    (1, 2, 4, 5, 5),  # missing 3, duplicate 5
]


@pytest.mark.parametrize(
    "ordinal_set",
    _NON_CONTIGUOUS_ORDINAL_SETS,
    ids=[str(s) for s in _NON_CONTIGUOUS_ORDINAL_SETS],
)
def test_non_contiguous_ordinals_are_rejected(
    engine: Engine,
    trail_service: TrailService,
    ordinal_set: tuple[int, int, int, int, int],
) -> None:
    """5 steps whose ordinals are not exactly {1,2,3,4,5} are rejected.

    Requirement 9.7's "ordinals that are not the contiguous integers
    1 through 5" branch. The validator must surface the
    ``ordinals_not_contiguous_1_to_5`` constraint name so callers
    (and the HTTP layer in task 10.3) can render a structured 400
    response rather than relying on the message text.

    Steps in this test carry synthetic target identifiers; per-step
    target-kind and identifier validation never runs because the
    ordinal-set check fires first.
    """
    # For each ordinal in the set, pick a target_kind that is at
    # least valid for SOME ordinal — for ordinal 1..5 we use the
    # mapped kind; for ordinals outside 1..5 (0, 6, etc.) we re-use
    # ``decision`` as a placeholder because the ordinal-set check
    # rejects the request before per-step target-kind is evaluated.
    steps = []
    for ord_value in ordinal_set:
        if ord_value in ORDINAL_TARGET_KIND:
            kind = ORDINAL_TARGET_KIND[ord_value]
        else:
            kind = "decision"
        steps.append(
            TrailStepInput(
                ordinal=ord_value,
                target_kind=kind,
                target_id=_DECISION_ID,
                # No revision / region — irrelevant to the check this
                # test exercises; identifier validation never runs.
            )
        )

    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "ordinals_not_contiguous_1_to_5"


# ---------------------------------------------------------------------------
# Mismatched target_kind per ordinal (Requirement 9.7).
#
# Every (ordinal, wrong_kind) pair is exercised. Five ordinals × four
# wrong kinds = 20 parametrized cases. A regression that loosens any
# one row of the ORDINAL_TARGET_KIND constraint surfaces as exactly
# one parametrized failure rather than being masked by a smoke test.
# ---------------------------------------------------------------------------


def _mismatched_kind_cases() -> list[tuple[int, str]]:
    """Enumerate every (ordinal, wrong_kind) pair for the parametrize."""
    all_kinds = tuple(ORDINAL_TARGET_KIND.values())
    cases: list[tuple[int, str]] = []
    for ordinal in sorted(ORDINAL_TARGET_KIND):
        expected = ORDINAL_TARGET_KIND[ordinal]
        for kind in all_kinds:
            if kind == expected:
                continue
            cases.append((ordinal, kind))
    return cases


@pytest.mark.parametrize(
    ("ordinal", "wrong_kind"),
    _mismatched_kind_cases(),
    ids=[f"ord{ord_v}_kind_{kind}" for ord_v, kind in _mismatched_kind_cases()],
)
def test_mismatched_target_kind_for_ordinal_is_rejected(
    engine: Engine,
    trail_service: TrailService,
    ordinal: int,
    wrong_kind: str,
) -> None:
    """Each ordinal × each wrong target_kind → ``target_kind_invalid_for_ordinal``.

    Requirement 9.2 fixes the target kind per ordinal and Requirement
    9.7 makes the mismatch a structural rejection. The mapping in
    ``ORDINAL_TARGET_KIND`` is the single source of truth — the
    schema CHECK constraint on ``Trail_Steps`` enforces the same
    pairing, but the Python validator must fire first so the HTTP
    layer can render a structured 400 instead of an opaque
    IntegrityError.

    The other four steps in each parametrized case stay valid so the
    test isolates the kind mismatch on a single ordinal.
    """
    steps = _five_valid_steps()
    # Replace the targeted ordinal's step with one carrying the wrong
    # target_kind. Identifier shape stays correct for the ordinal so
    # the failure is unambiguously about target_kind.
    steps[ordinal - 1] = _STEP_BUILDERS[ordinal](target_kind=wrong_kind)

    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "target_kind_invalid_for_ordinal"
    # Message names the offending ordinal so an operator reading a 400
    # response can find the step without diffing the submission.
    assert f"ordinal={ordinal}" in str(exc.value)


# ---------------------------------------------------------------------------
# Selection-mode rejection (AD-WS-12 — slice restricts to ``Pinned``).
# ---------------------------------------------------------------------------


# AD-WS-12 explicitly defers Live, Approval-Controlled, and
# Historical-As-Of selection modes. Task 10.4 calls out Live and
# Approval-Controlled by name; Historical-As-Of is included so the
# parametrize sweeps every documented deferred value.
_DEFERRED_SELECTION_MODES = ["Live", "Approval-Controlled", "Historical-As-Of"]


@pytest.mark.parametrize("ordinal", sorted(ORDINAL_TARGET_KIND))
@pytest.mark.parametrize("mode", _DEFERRED_SELECTION_MODES)
def test_deferred_selection_modes_are_rejected_on_every_ordinal(
    engine: Engine,
    trail_service: TrailService,
    ordinal: int,
    mode: str,
) -> None:
    """Any non-Pinned ``selection_mode`` is rejected, on any ordinal.

    AD-WS-12 restricts the slice to ``selection_mode = 'Pinned'``;
    Live, Approval-Controlled, and Historical-As-Of are deferred.
    The validator must surface ``selection_mode_invalid`` on every
    ordinal so a regression that special-cases one stage of the
    pipeline surfaces as a parametrized failure rather than being
    masked by an ordinal-1-only test.
    """
    steps = _five_valid_steps()
    steps[ordinal - 1] = _STEP_BUILDERS[ordinal](selection_mode=mode)

    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "selection_mode_invalid"
    # Message names both the offending ordinal and the rejected mode
    # so a 400 response surfaces enough detail to fix the submission.
    assert f"ordinal={ordinal}" in str(exc.value)
    assert mode in str(exc.value)


@pytest.mark.parametrize(
    "mode",
    ["pinned", "PINNED", "Pinned ", " Pinned", "", "Other"],
    ids=[
        "lowercase",
        "uppercase",
        "trailing_space",
        "leading_space",
        "empty",
        "unknown",
    ],
)
def test_non_pinned_typo_variants_are_rejected(
    engine: Engine,
    trail_service: TrailService,
    mode: str,
) -> None:
    """``selection_mode`` is compared as an exact ``'Pinned'`` string.

    AD-WS-12 fixes the spelling; the schema CHECK constraint on
    ``Trail_Steps`` is case-sensitive. The validator must mirror
    that behavior so a malformed-but-similar value does not slip
    through to a schema-level IntegrityError.
    """
    steps = _five_valid_steps()
    steps[0] = _step_ordinal_1(selection_mode=mode)

    with pytest.raises(TrailValidationError) as exc:
        _call_create_trail(engine, trail_service, steps)
    assert exc.value.failed_constraint == "selection_mode_invalid"
