"""Unit tests for :mod:`walking_slice.projection`.

These tests pin the contract task 14.1 establishes for the projection
envelope:

- The envelope is a frozen Pydantic value object (matches the conventions
  in ``walking_slice.models``).
- It carries the Projection Definition, source Resource Identities, source
  Revision Identities, applicable temporal boundary, generated time, and a
  derivation indicator (Requirement 14.1, 14.2).
- ``applicable_temporal_boundary`` and ``generated_at`` are UTC datetimes
  at strict second precision; sub-second precision and non-UTC offsets are
  rejected (Requirement 14.1).
- ``derivation`` is pinned to ``"derived"``; tampering raises
  ``ValidationError`` (Requirement 14.2).

Subsequent tasks (14.2 — integration into status-bearing responses; 14.3 —
explainable-withholding paths) layer behavior on top of these guarantees;
the example-level checks here exist so a regression in the envelope's shape
fails fast before it leaks into a status response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from walking_slice.projection import ProjectionDefinition, ProjectionEnvelope


pytestmark = pytest.mark.unit


RESOURCE_ID_A = UUID("01890000-0000-7000-8000-00000000000a")
RESOURCE_ID_B = UUID("01890000-0000-7000-8000-00000000000b")
REVISION_ID_A = UUID("01890000-0000-7000-8000-00000000000c")
REVISION_ID_B = UUID("01890000-0000-7000-8000-00000000000d")

BOUNDARY = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
GENERATED = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)


def _make_definition(**overrides: object) -> ProjectionDefinition:
    defaults: dict[str, object] = {
        "name": "trail.unresolved-step",
        "version": "2026.01",
    }
    defaults.update(overrides)
    return ProjectionDefinition(**defaults)  # type: ignore[arg-type]


def _make_envelope(**overrides: object) -> ProjectionEnvelope:
    defaults: dict[str, object] = {
        "definition": _make_definition(),
        "source_resource_ids": (RESOURCE_ID_A,),
        "source_revision_ids": (REVISION_ID_A,),
        "applicable_temporal_boundary": BOUNDARY,
        "generated_at": GENERATED,
    }
    defaults.update(overrides)
    return ProjectionEnvelope(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ProjectionDefinition
# ---------------------------------------------------------------------------


def test_projection_definition_accepts_valid_inputs() -> None:
    definition = _make_definition()
    assert definition.name == "trail.unresolved-step"
    assert definition.version == "2026.01"


def test_projection_definition_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        _make_definition(name="")


def test_projection_definition_rejects_empty_version() -> None:
    with pytest.raises(ValidationError):
        _make_definition(version="")


def test_projection_definition_rejects_oversized_name() -> None:
    with pytest.raises(ValidationError):
        _make_definition(name="x" * 257)


def test_projection_definition_rejects_oversized_version() -> None:
    with pytest.raises(ValidationError):
        _make_definition(version="v" * 65)


def test_projection_definition_is_frozen() -> None:
    definition = _make_definition()
    with pytest.raises(ValidationError):
        definition.name = "tampered"  # type: ignore[misc]


def test_projection_definition_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectionDefinition(name="x", version="1", extra="nope")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ProjectionEnvelope — happy path and shape
# ---------------------------------------------------------------------------


def test_envelope_carries_required_metadata() -> None:
    envelope = _make_envelope(
        source_resource_ids=(RESOURCE_ID_A, RESOURCE_ID_B),
        source_revision_ids=(REVISION_ID_A, REVISION_ID_B),
    )
    assert envelope.definition.name == "trail.unresolved-step"
    assert envelope.source_resource_ids == (RESOURCE_ID_A, RESOURCE_ID_B)
    assert envelope.source_revision_ids == (REVISION_ID_A, REVISION_ID_B)
    assert envelope.applicable_temporal_boundary == BOUNDARY
    assert envelope.generated_at == GENERATED


def test_envelope_default_derivation_indicator_is_derived() -> None:
    envelope = _make_envelope()
    assert envelope.derivation == "derived"


def test_envelope_allows_empty_source_id_tuples() -> None:
    envelope = _make_envelope(source_resource_ids=(), source_revision_ids=())
    assert envelope.source_resource_ids == ()
    assert envelope.source_revision_ids == ()


def test_envelope_coerces_source_id_iterables_to_tuple() -> None:
    envelope = _make_envelope(
        source_resource_ids=[RESOURCE_ID_A, RESOURCE_ID_B],
        source_revision_ids=[REVISION_ID_A],
    )
    assert envelope.source_resource_ids == (RESOURCE_ID_A, RESOURCE_ID_B)
    assert envelope.source_revision_ids == (REVISION_ID_A,)
    assert isinstance(envelope.source_resource_ids, tuple)
    assert isinstance(envelope.source_revision_ids, tuple)


def test_envelope_is_hashable() -> None:
    envelope = _make_envelope()
    # frozen=True + tuple/UUID/datetime fields makes the envelope hashable;
    # tests rely on this when comparing or memoizing envelope identities.
    assert hash(envelope) == hash(_make_envelope())


# ---------------------------------------------------------------------------
# ProjectionEnvelope — immutability
# ---------------------------------------------------------------------------


def test_envelope_is_frozen_against_field_reassignment() -> None:
    envelope = _make_envelope()
    with pytest.raises(ValidationError):
        envelope.generated_at = GENERATED + timedelta(seconds=1)  # type: ignore[misc]


def test_envelope_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ProjectionEnvelope(  # type: ignore[call-arg]
            definition=_make_definition(),
            source_resource_ids=(),
            source_revision_ids=(),
            applicable_temporal_boundary=BOUNDARY,
            generated_at=GENERATED,
            extra="nope",
        )


def test_envelope_rejects_attempt_to_widen_derivation_indicator() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(derivation="authoritative")


# ---------------------------------------------------------------------------
# ProjectionEnvelope — ISO-8601 second precision (Requirement 14.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["applicable_temporal_boundary", "generated_at"],
)
def test_envelope_rejects_naive_datetime(field_name: str) -> None:
    naive = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValidationError):
        _make_envelope(**{field_name: naive})


@pytest.mark.parametrize(
    "field_name",
    ["applicable_temporal_boundary", "generated_at"],
)
def test_envelope_rejects_non_utc_offset(field_name: str) -> None:
    eastern = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=-5)))
    with pytest.raises(ValidationError):
        _make_envelope(**{field_name: eastern})


@pytest.mark.parametrize(
    "field_name",
    ["applicable_temporal_boundary", "generated_at"],
)
def test_envelope_rejects_sub_second_precision(field_name: str) -> None:
    sub_second = datetime(2026, 1, 1, 12, 0, 0, microsecond=500_000, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        _make_envelope(**{field_name: sub_second})


def test_envelope_accepts_zero_microsecond_utc_datetime() -> None:
    # Sanity check: the happy path uses second-precision UTC datetimes and
    # should not be rejected by the validator that guards Requirement 14.1.
    envelope = _make_envelope()
    assert envelope.applicable_temporal_boundary.microsecond == 0
    assert envelope.generated_at.microsecond == 0
    assert envelope.applicable_temporal_boundary.tzinfo == timezone.utc
    assert envelope.generated_at.tzinfo == timezone.utc
