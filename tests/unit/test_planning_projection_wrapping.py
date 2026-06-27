"""Dedicated unit tests for task 14.2 — projection-envelope wrapping
on Planning_Service status responses.

Scope (per ``.kiro/specs/second-walking-slice/tasks.md`` §14.2):

    Cover: every status-bearing response includes the envelope with
    all required fields; unresolvable-definition path withholds
    status and returns the explanation-unavailable indicator; source
    records remain byte-equivalent when corrections arrive.

    _Requirements: 18.1, 18.3, 18.4_

These tests are deliberately a separate file from
``test_planning_projection.py`` (which holds the task 14.1 smoke
tests). The smoke file establishes that the helper exists, exports
the named constants, and round-trips a single happy path; this file
exhaustively covers the three Requirement 18 acceptance criteria
named in the task brief:

- **18.1** — every projected status carries the Projection
  Definition, source Resource Identities, source Revision
  Identities, applicable temporal boundary (ISO-8601 second
  precision), and generated time (ISO-8601 second precision)
  inside the wrapped :class:`ProjectionEnvelope`. Tested
  parametrically across every known Slice 2 status string so a
  future status added to :data:`PLANNING_PROJECTED_STATUSES`
  trips an assertion if its wrapping behavior diverges.
- **18.4** — when the Projection Definition is unregistered or a
  required source Revision is missing, the helper withholds the
  projected status and returns an
  :class:`ExplanationUnavailableResponse` identifying the missing
  element. Tested across all five known statuses to demonstrate
  the withholding path short-circuits *before* the status is
  surfaced (Requirement 18.4 phrasing: "withhold the projected
  status").
- **18.3** — source Records remain byte-equivalent when
  corrections arrive. The Slice 2 helper is a pure value-object
  wrapper that performs no persistence, so "byte-equivalence" at
  this unit-test level means:

    1. The helper does not mutate any input collection
       (``source_resource_ids``, ``source_revision_ids``,
       ``details``).
    2. A previously built :class:`ProjectionEnvelope` /
       :class:`ProjectedStatusResponse` is byte-equivalent to its
       initial state after a *later* wrap call corrects the
       status, adds a previously-missing source Revision, or
       registers a previously-missing Projection Definition.
    3. Frozen Pydantic value-object equality is preserved across
       repeated wrap calls with identical inputs.

  The byte-equivalence pattern parallels the Slice 1 task 14.2
  pattern in ``test_projection_status_wrapping.py``; the source
  Records there are SQL rows seeded into the test database, while
  here they are the input collections and the value-object
  envelopes the helper returns. Both shapes satisfy the same
  property: "prior recorded state is unchanged when the
  Planning_Service is reinvoked with corrected inputs".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from walking_slice.clock import FixedClock
from walking_slice.planning._projection import (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_ORPHANED,
    PLAN_STATUS_SUPERSEDED,
    PLANNING_PROJECTED_STATUSES,
    PLANNING_PROJECTION_DEFINITION,
    PLANNING_PROJECTION_DEFINITION_NAME,
    PROVENANCE_STATUS_INCOMPLETE,
    planning_projection_registry,
    wrap_planning_status,
)
from walking_slice.projection import (
    ExplanationUnavailableResponse,
    ProjectedStatusResponse,
    ProjectionDefinition,
    ProjectionEnvelope,
    StatusProjector,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test fixtures and helpers.
# ---------------------------------------------------------------------------


# Temporal boundary at second precision (microsecond == 0) so the envelope
# validator accepts it directly. Constant across tests so the assertion
# messages are easy to read when a test fails.
_BOUNDARY = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# Clock fixed to a *different* second than the boundary so a test can
# distinguish "envelope.generated_at sourced from the clock" from
# "envelope.generated_at incorrectly mirrors the boundary".
_CLOCK_INSTANT = datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc)


# Source Identities used across tests. Fixed values so the byte-equivalence
# assertions compare the same UUIDs both sides of a correction.
_PLAN_REVISION_ID = uuid.UUID("01890000-0000-7000-8000-00000000a001")
_ACTIVITY_PLAN_ID = uuid.UUID("01890000-0000-7000-8000-00000000a002")
_PLAN_APPROVAL_ID = uuid.UUID("01890000-0000-7000-8000-00000000a003")


def _projector(*, with_planning_definition: bool = True) -> StatusProjector:
    """Return a :class:`StatusProjector` for the test.

    ``with_planning_definition`` toggles whether the Planning_Service
    Projection Definition is pre-registered — the boundary between
    the happy path (Requirement 18.1) and the unresolvable-definition
    withholding path (Requirement 18.4).
    """
    registry = planning_projection_registry() if with_planning_definition else {}
    return StatusProjector(
        clock=FixedClock(_CLOCK_INSTANT),
        definition_registry=registry,
    )


# Every known Slice 2 projected status string. Sourced from the module's
# :data:`PLANNING_PROJECTED_STATUSES` set so adding a new status to the
# module without adding a wrapping test trips the parametrized check.
_KNOWN_STATUSES: tuple[str, ...] = (
    PLAN_STATUS_APPROVED,
    PLAN_STATUS_DRAFT,
    PLAN_STATUS_SUPERSEDED,
    PLAN_STATUS_ORPHANED,
    PROVENANCE_STATUS_INCOMPLETE,
)


# ---------------------------------------------------------------------------
# Requirement 18.1 — every status-bearing response includes the envelope
# with all required fields.
# ---------------------------------------------------------------------------


class TestStatusBearingResponseEnvelopeFields:
    """Requirement 18.1: every projected status surfaced by the
    Planning_Service is wrapped in a :class:`ProjectionEnvelope`
    carrying the Projection Definition, source Resource Identities,
    source Revision Identities, applicable temporal boundary, and
    generated time — all at ISO-8601 second precision.

    Requirement 18.2 (derivation indicator) is asserted alongside
    because the indicator is part of every envelope; failing 18.2
    means the envelope shape is wrong, which is what 18.1's
    "all required fields" phrasing demands.
    """

    def test_known_statuses_set_is_complete(self) -> None:
        # If a new status string is added to the module's published
        # set without being added to this test file's parametrization
        # constant, this assertion fails — the next assertion below
        # would otherwise silently skip the new status.
        assert frozenset(_KNOWN_STATUSES) == PLANNING_PROJECTED_STATUSES

    @pytest.mark.parametrize("status", _KNOWN_STATUSES)
    def test_envelope_carries_every_required_field_for_each_known_status(
        self, status: str
    ) -> None:
        # Per Requirement 18.1, *every* status-bearing response must
        # surface the envelope with every required field. Running
        # this assertion across all five known Slice 2 statuses
        # demonstrates uniform behavior — a future status that
        # diverges (for example, accidentally omits source_revision_ids)
        # fails the assertion specifically for that status.
        projector = _projector(with_planning_definition=True)

        response = wrap_planning_status(
            status_projector=projector,
            status=status,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[_ACTIVITY_PLAN_ID],
            source_revision_ids=[_PLAN_REVISION_ID, _PLAN_APPROVAL_ID],
            details={"plan_revision_id": str(_PLAN_REVISION_ID)},
        )

        # Status was surfaced (Requirement 18.1 — projected status is
        # *included*, not withheld).
        assert isinstance(response, ProjectedStatusResponse)
        assert response.status == status

        envelope = response.envelope
        assert isinstance(envelope, ProjectionEnvelope)

        # Required field 1 — Projection Definition.
        assert envelope.definition == PLANNING_PROJECTION_DEFINITION

        # Required field 2 — source Resource Identities (tuple form so
        # the envelope itself is hashable and immutable).
        assert envelope.source_resource_ids == (_ACTIVITY_PLAN_ID,)

        # Required field 3 — source Revision Identities.
        assert envelope.source_revision_ids == (
            _PLAN_REVISION_ID,
            _PLAN_APPROVAL_ID,
        )

        # Required field 4 — applicable temporal boundary at second
        # precision, in UTC.
        assert envelope.applicable_temporal_boundary == _BOUNDARY
        assert envelope.applicable_temporal_boundary.tzinfo == timezone.utc
        assert envelope.applicable_temporal_boundary.microsecond == 0

        # Required field 5 — generated time, sourced from the
        # projector's clock and truncated to second precision.
        assert envelope.generated_at == _CLOCK_INSTANT
        assert envelope.generated_at.tzinfo == timezone.utc
        assert envelope.generated_at.microsecond == 0

        # Requirement 18.2 — derivation indicator pinned to "derived".
        assert envelope.derivation == "derived"

    def test_envelope_source_ids_are_stored_as_tuples(self) -> None:
        # The envelope's source-id fields are tuples so the value
        # object stays hashable and immutable. A producer that passes
        # a Python list does not retain a back-reference into the
        # envelope.
        projector = _projector()
        resource_ids = [_ACTIVITY_PLAN_ID]
        revision_ids = [_PLAN_REVISION_ID]

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=resource_ids,
            source_revision_ids=revision_ids,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert isinstance(response.envelope.source_resource_ids, tuple)
        assert isinstance(response.envelope.source_revision_ids, tuple)

    def test_envelope_definition_is_resolved_from_registry_by_name(self) -> None:
        # Requirement 18.1 expects the Projection Definition recorded
        # in the envelope to be the one the projector resolved by
        # name — not a fresh instance built by the helper. Equality
        # alone would not catch a "helper synthesizes its own
        # definition" regression because the equality is structural;
        # we instead assert the registered definition value-object
        # equals the envelope's definition value-object and shares
        # the registered name and version.
        projector = _projector()

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert (
            response.envelope.definition.name
            == PLANNING_PROJECTION_DEFINITION_NAME
        )
        assert response.envelope.definition.version == (
            PLANNING_PROJECTION_DEFINITION.version
        )

    def test_envelope_propagates_optional_details_payload(self) -> None:
        # Producer-specific payload rides on
        # :attr:`ProjectedStatusResponse.details`, not on the
        # envelope itself (the envelope is pure metadata). Requirement
        # 18.1 mentions the envelope's *required* fields; details is
        # the producer-extension surface.
        projector = _projector()
        details = {
            "plan_revision_id": str(_PLAN_REVISION_ID),
            "plan_approval_id": str(_PLAN_APPROVAL_ID),
        }

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            details=details,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.details == details

    def test_envelope_generated_at_at_second_precision_when_clock_carries_milliseconds(
        self,
    ) -> None:
        # The Slice 1 Clock returns millisecond precision; the
        # projector truncates that to second precision so the envelope
        # validator accepts the value (Requirement 18.1 phrasing:
        # "ISO-8601 with at least second precision" — strict second
        # precision is enforced by :class:`ProjectionEnvelope`).
        millisecond_clock_instant = datetime(
            2026, 1, 1, 12, 0, 5, 123_000, tzinfo=timezone.utc
        )
        projector = StatusProjector(
            clock=FixedClock(millisecond_clock_instant),
            definition_registry=planning_projection_registry(),
        )

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.envelope.generated_at.microsecond == 0
        assert response.envelope.generated_at == datetime(
            2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc
        )

    def test_envelope_with_no_source_ids_still_carries_required_metadata(
        self,
    ) -> None:
        # A status that depends only on temporal facts (no source
        # Revisions) still receives a fully populated envelope. The
        # source-id tuples default to empty rather than absent so
        # consumers do not need to special-case missing keys.
        projector = _projector()

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_ORPHANED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ProjectedStatusResponse)
        assert response.envelope.source_resource_ids == ()
        assert response.envelope.source_revision_ids == ()
        # The other required fields are still present — empty source
        # lists do not weaken the envelope shape.
        assert response.envelope.definition == PLANNING_PROJECTION_DEFINITION
        assert response.envelope.applicable_temporal_boundary == _BOUNDARY
        assert response.envelope.generated_at == _CLOCK_INSTANT
        assert response.envelope.derivation == "derived"


# ---------------------------------------------------------------------------
# Requirement 18.4 — unresolvable-definition path withholds status.
# ---------------------------------------------------------------------------


class TestUnresolvableDefinitionWithholdsStatus:
    """Requirement 18.4: when the Projection Definition or a required
    source Revision cannot be resolved, the Planning_Service withholds
    the projected status and returns an explanation-unavailable
    indicator identifying the missing element.

    The tests assert *both* halves of the requirement: (a) the
    projected status is not surfaced (the response is not a
    :class:`ProjectedStatusResponse`), and (b) the response identifies
    the missing element by kind and identifier.
    """

    @pytest.mark.parametrize("status", _KNOWN_STATUSES)
    def test_unregistered_definition_yields_explanation_unavailable(
        self, status: str
    ) -> None:
        # Registry deliberately empty: the Planning_Service Projection
        # Definition is unresolvable from the projector's vantage.
        # Run across every known status to demonstrate the withholding
        # short-circuits *before* the status string influences the
        # response, regardless of which status was being produced.
        projector = _projector(with_planning_definition=False)

        response = wrap_planning_status(
            status_projector=projector,
            status=status,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[_ACTIVITY_PLAN_ID],
            source_revision_ids=[_PLAN_REVISION_ID],
        )

        # Half (a) — status withheld: the response is *not* a
        # ProjectedStatusResponse and has no ``status`` attribute.
        assert not isinstance(response, ProjectedStatusResponse)
        assert isinstance(response, ExplanationUnavailableResponse)
        assert not hasattr(response, "status")
        assert not hasattr(response, "envelope")

        # Half (b) — missing element identified.
        assert response.missing_element_kind == "projection_definition"
        assert response.missing_element_identifier == (
            PLANNING_PROJECTION_DEFINITION_NAME
        )

    def test_unregistered_definition_response_does_not_leak_status_string(
        self,
    ) -> None:
        # The status string passed in must not appear in the
        # withheld response — Requirement 18.4 says the projected
        # status is *withheld*. Any leakage of the status string
        # through the response shape would let a caller learn what
        # status the projection would have produced absent the
        # missing definition.
        projector = _projector(with_planning_definition=False)

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        dumped = response.model_dump()
        assert PLAN_STATUS_APPROVED not in dumped.values()
        # The missing-element identifier is the *definition name*, not
        # a status name.
        assert (
            response.missing_element_identifier
            == PLANNING_PROJECTION_DEFINITION_NAME
        )

    def test_missing_source_revision_yields_explanation_unavailable(
        self,
    ) -> None:
        # Definition is registered, but the producer detected a
        # missing source Revision. The most precise missing element
        # wins per the helper's documented precedence (mirrors
        # :meth:`StatusProjector.project_status`).
        projector = _projector(with_planning_definition=True)
        missing_revision_id = uuid.UUID(
            "01890000-0000-7000-8000-00000000bfff"
        )

        response = wrap_planning_status(
            status_projector=projector,
            status=PROVENANCE_STATUS_INCOMPLETE,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=missing_revision_id,
        )

        assert not isinstance(response, ProjectedStatusResponse)
        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "source_revision"
        assert response.missing_element_identifier == str(missing_revision_id)

    def test_missing_revision_takes_precedence_over_unregistered_definition(
        self,
    ) -> None:
        # Both gaps exist: registry is empty AND the producer detected
        # a missing source Revision. Naming the precise missing
        # element is more useful to the caller than reporting a
        # definition gap that the producer cannot fix.
        projector = _projector(with_planning_definition=False)
        missing_revision_id = uuid.UUID(
            "01890000-0000-7000-8000-00000000bffe"
        )

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=missing_revision_id,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        assert response.missing_element_kind == "source_revision"
        assert response.missing_element_identifier == str(missing_revision_id)

    def test_explanation_unavailable_shape_has_no_envelope(self) -> None:
        # The withheld response carries only the missing-element kind
        # and identifier — no envelope fields leak through. Asserted
        # via the model's declared field set rather than hasattr so a
        # future field addition forces a deliberate update here.
        projector = _projector(with_planning_definition=False)

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
        )

        assert isinstance(response, ExplanationUnavailableResponse)
        # Pydantic 2 records declared fields under ``model_fields``.
        declared = set(type(response).model_fields.keys())
        assert declared == {
            "missing_element_kind",
            "missing_element_identifier",
        }


# ---------------------------------------------------------------------------
# Requirement 18.3 — source records remain byte-equivalent when corrections
# arrive.
# ---------------------------------------------------------------------------


class TestSourceRecordsByteEquivalenceAcrossCorrections:
    """Requirement 18.3: when a corrected or late-arriving source fact
    changes a Plan Revision's projected status, every prior source
    Record, Revision, and correction record is retained byte-equivalent
    to its recorded state; new facts arrive as additional Revisions or
    Records rather than as overwrites.

    The Slice 2 helper persists nothing (Principle 5.23 — Projections
    are derived); at the unit-test level "byte-equivalence" means
    three concrete behaviors:

    1. The helper does not mutate any input collection — the producer
       can pass mutable lists/dicts and re-use them safely.
    2. A previously built envelope value object remains byte-equivalent
       to its initial state across later wrap calls that surface a
       different ("corrected") status from a richer source set.
    3. Repeated wrap calls with byte-identical inputs produce
       byte-equivalent envelopes (frozen-model equality).
    """

    def test_input_source_resource_ids_list_is_not_mutated(self) -> None:
        # The producer passes a mutable list; after the wrap call the
        # list must still contain the same elements in the same order.
        # The helper copies into a tuple internally so this property
        # is automatic, but asserting it here pins the behavior so a
        # future "optimisation" that mutates the input fails fast.
        projector = _projector()
        resource_ids = [_ACTIVITY_PLAN_ID]
        snapshot = list(resource_ids)

        wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=resource_ids,
        )

        assert resource_ids == snapshot

    def test_input_source_revision_ids_list_is_not_mutated(self) -> None:
        projector = _projector()
        revision_ids = [_PLAN_REVISION_ID, _PLAN_APPROVAL_ID]
        snapshot = list(revision_ids)

        wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_revision_ids=revision_ids,
        )

        assert revision_ids == snapshot

    def test_input_details_mapping_is_not_mutated(self) -> None:
        projector = _projector()
        details = {"plan_revision_id": str(_PLAN_REVISION_ID)}
        snapshot = dict(details)

        wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
            details=details,
        )

        assert details == snapshot

    def test_input_details_mapping_proxy_is_accepted(self) -> None:
        # A defensive producer may hand the helper a read-only
        # mapping (MappingProxyType). The helper must accept it
        # without raising and must not attempt to mutate it.
        projector = _projector()
        backing = {"plan_revision_id": str(_PLAN_REVISION_ID)}
        details: MappingProxyType[str, str] = MappingProxyType(backing)

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            details=details,
        )

        assert isinstance(response, ProjectedStatusResponse)
        # The wrapped response carries an independent dict so a
        # producer that subsequently mutates the backing dict does
        # not retroactively mutate the response.
        backing["leaked"] = "should-not-appear"
        assert "leaked" not in response.details

    def test_external_mutation_of_input_list_does_not_alter_prior_envelope(
        self,
    ) -> None:
        # Stronger form of the byte-equivalence property: even if the
        # producer mutates its own list *after* the wrap call returns,
        # the envelope's source-id tuple must remain unchanged. Pins
        # that the helper stores a snapshot, not a reference.
        projector = _projector()
        revision_ids = [_PLAN_REVISION_ID]

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
            source_revision_ids=revision_ids,
        )
        assert isinstance(response, ProjectedStatusResponse)

        # Simulate a "correction" arriving on the producer's side —
        # the producer mutates its own list to reflect new source
        # Revisions. Requirement 18.3 says the prior envelope must
        # remain byte-equivalent.
        revision_ids.append(_PLAN_APPROVAL_ID)

        assert response.envelope.source_revision_ids == (_PLAN_REVISION_ID,)

    def test_repeated_wrap_with_identical_inputs_produces_equal_envelopes(
        self,
    ) -> None:
        # Two wrap calls with byte-identical inputs return
        # byte-equivalent responses. Frozen Pydantic models compare
        # equal field-by-field; demonstrating the equality pins the
        # property explicitly.
        projector = _projector()

        first = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[_ACTIVITY_PLAN_ID],
            source_revision_ids=[_PLAN_REVISION_ID, _PLAN_APPROVAL_ID],
            details={"plan_approval_id": str(_PLAN_APPROVAL_ID)},
        )
        second = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[_ACTIVITY_PLAN_ID],
            source_revision_ids=[_PLAN_REVISION_ID, _PLAN_APPROVAL_ID],
            details={"plan_approval_id": str(_PLAN_APPROVAL_ID)},
        )

        assert isinstance(first, ProjectedStatusResponse)
        assert isinstance(second, ProjectedStatusResponse)
        assert first == second
        assert first.envelope == second.envelope
        assert first.model_dump() == second.model_dump()

    def test_status_correction_preserves_prior_envelope_byte_equivalence(
        self,
    ) -> None:
        # The headline byte-equivalence test mirroring the Slice 1
        # task 14.2 pattern in
        # ``test_projection_status_wrapping.py``::
        # ``TestSourceRecordsByteEquivalenceAcrossCorrections``.
        #
        # 1. The producer wraps a status from the *current* source
        #    set ("Plan Revision draft" before approval).
        # 2. A correction arrives: a Plan Approval Record lands and
        #    the projection should now surface "Plan Approved".
        # 3. The producer wraps the *corrected* status, supplying
        #    the richer source set.
        # 4. The original wrapped response must remain byte-equivalent
        #    to its recorded state — no field, including
        #    source_resource_ids and source_revision_ids, was
        #    overwritten.
        #
        # Frozen-model semantics make this automatic at the value-
        # object level; the assertion pins the property so a future
        # change that introduces a shared mutable cache would fail
        # fast.
        projector = _projector()

        # Capture the source-id lists in their pre-correction state.
        draft_resource_ids = [_ACTIVITY_PLAN_ID]
        draft_revision_ids = [_PLAN_REVISION_ID]

        before = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=draft_resource_ids,
            source_revision_ids=draft_revision_ids,
            details={"plan_revision_id": str(_PLAN_REVISION_ID)},
        )
        assert isinstance(before, ProjectedStatusResponse)
        before_snapshot = before.model_dump()

        # Late-arriving fact: a Plan Approval Record landed; the
        # projection's source set is now richer and its status is
        # "Plan Approved". Requirement 18.3 says the prior projected-
        # status response remains byte-equivalent to its recorded
        # state.
        approved_resource_ids = [_ACTIVITY_PLAN_ID]
        approved_revision_ids = [_PLAN_REVISION_ID, _PLAN_APPROVAL_ID]

        after = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=approved_resource_ids,
            source_revision_ids=approved_revision_ids,
            details={
                "plan_revision_id": str(_PLAN_REVISION_ID),
                "plan_approval_id": str(_PLAN_APPROVAL_ID),
            },
        )
        assert isinstance(after, ProjectedStatusResponse)
        assert after.status == PLAN_STATUS_APPROVED

        # Prior response unchanged — value-object byte-equivalence.
        assert before.model_dump() == before_snapshot
        assert before.status == PLAN_STATUS_DRAFT
        assert before.envelope.source_revision_ids == (_PLAN_REVISION_ID,)
        assert before.envelope.source_resource_ids == (_ACTIVITY_PLAN_ID,)

        # The pre-correction input lists also remain byte-equivalent —
        # the producer can re-use them without seeing residue from
        # the later corrected call.
        assert draft_resource_ids == [_ACTIVITY_PLAN_ID]
        assert draft_revision_ids == [_PLAN_REVISION_ID]

    def test_withheld_then_corrected_call_preserves_prior_withheld_response(
        self,
    ) -> None:
        # Mirror of the Slice 1 task 14.2 "register the missing
        # Projection Definition" correction scenario, scaled to the
        # Slice 2 helper:
        #
        # 1. Initial wrap call against an empty registry withholds
        #    the projected status (Requirement 18.4).
        # 2. The "correction" arrives — the slice's composition
        #    registers the Planning_Service Projection Definition.
        # 3. The next wrap call succeeds with a full envelope.
        # 4. The originally withheld response remains byte-equivalent
        #    to its recorded state (Requirement 18.3 — prior
        #    records are not overwritten by the correction).
        empty_projector = _projector(with_planning_definition=False)

        withheld = wrap_planning_status(
            status_projector=empty_projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[_ACTIVITY_PLAN_ID],
            source_revision_ids=[_PLAN_REVISION_ID],
        )
        assert isinstance(withheld, ExplanationUnavailableResponse)
        withheld_snapshot = withheld.model_dump()

        # Correction: the composition root now has the Planning
        # Projection Definition registered.
        corrected_projector = _projector(with_planning_definition=True)
        corrected = wrap_planning_status(
            status_projector=corrected_projector,
            status=PLAN_STATUS_APPROVED,
            applicable_temporal_boundary=_BOUNDARY,
            source_resource_ids=[_ACTIVITY_PLAN_ID],
            source_revision_ids=[_PLAN_REVISION_ID],
        )
        assert isinstance(corrected, ProjectedStatusResponse)
        assert corrected.status == PLAN_STATUS_APPROVED

        # Withheld response unchanged after the corrected call —
        # byte-equivalence at the value-object level.
        assert withheld.model_dump() == withheld_snapshot
        assert withheld.missing_element_kind == "projection_definition"
        assert (
            withheld.missing_element_identifier
            == PLANNING_PROJECTION_DEFINITION_NAME
        )

    def test_missing_source_revision_correction_preserves_prior_response(
        self,
    ) -> None:
        # Mirror of the Slice 1 task 14.2 "correct the unresolvable
        # source target" scenario: the producer initially flagged a
        # missing source Revision (Requirement 18.4), then resolves it
        # and reissues the wrap. The prior withheld response remains
        # byte-equivalent.
        projector = _projector()
        originally_missing = uuid.UUID(
            "01890000-0000-7000-8000-00000000bdaa"
        )

        withheld = wrap_planning_status(
            status_projector=projector,
            status=PROVENANCE_STATUS_INCOMPLETE,
            applicable_temporal_boundary=_BOUNDARY,
            missing_source_revision_id=originally_missing,
        )
        assert isinstance(withheld, ExplanationUnavailableResponse)
        withheld_snapshot = withheld.model_dump()

        # Correction: the previously missing source Revision is now
        # resolvable. The producer reissues the wrap without
        # ``missing_source_revision_id`` and with the Revision in
        # the source-revision list.
        corrected = wrap_planning_status(
            status_projector=projector,
            status=PROVENANCE_STATUS_INCOMPLETE,
            applicable_temporal_boundary=_BOUNDARY,
            source_revision_ids=[originally_missing],
        )
        assert isinstance(corrected, ProjectedStatusResponse)
        assert corrected.envelope.source_revision_ids == (originally_missing,)

        # Prior withheld response unchanged.
        assert withheld.model_dump() == withheld_snapshot
        assert withheld.missing_element_identifier == str(originally_missing)

    def test_frozen_response_rejects_mutation_at_runtime(self) -> None:
        # The byte-equivalence guarantee depends on the value object
        # being frozen — direct field assignment must raise. Pinning
        # this here means a future loosening of the Pydantic config
        # (``frozen=False``) trips a unit test instead of silently
        # weakening Requirement 18.3.
        projector = _projector()

        response = wrap_planning_status(
            status_projector=projector,
            status=PLAN_STATUS_DRAFT,
            applicable_temporal_boundary=_BOUNDARY,
        )
        assert isinstance(response, ProjectedStatusResponse)

        with pytest.raises(Exception):
            # The exact exception class is Pydantic-internal
            # (``ValidationError`` in v2 on frozen models); we assert
            # only that *some* exception is raised so the test does
            # not couple to the Pydantic implementation detail.
            response.status = PLAN_STATUS_APPROVED  # type: ignore[misc]
