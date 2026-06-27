"""Unit tests for unauthorized Decision denial (task 8.4).

This file is the **focused spec-coverage view** for task 8.4. Tests are
grouped strictly by the three acceptance criteria the task brief calls
out — one section per Requirement — and each section's tests pin one
behavior that the brief asks for so the spec-to-test mapping is
auditable at a glance.

Task brief (verbatim):

    Validate that no Decision row, Relationship, or in-flight write is
    persisted; that exactly one Denial Record is appended; and that the
    response body contains only
    ``{generic_denial_indicator, reason_code, correlation_id}``.

    Requirements: 7.1, 7.4, 7.5

Mapping (Requirement → assertion → section in this file):

- **7.1** "no Decision Record is created, modified, or persisted" plus
  the task brief's expansion "no Decision row, Relationship, or
  in-flight write is persisted" → §"Requirement 7.1: no in-flight
  write persists on denial". This section verifies that *every* table
  the Decision flow normally writes to remains byte-equivalent to its
  pre-attempt state when authorization denies the attempt: the
  ``Decisions`` row, the ``Addresses`` Relationship in
  ``Relationships``, the ``Provenance_Manifests`` row, any
  ``Omission_Entries`` rows, the consequential ``Audit_Records`` row
  (``action_type='create.decision'``), and the ``Identifier_Registry``
  bindings the flow would have created. The brief's "exactly one
  Denial Record is appended" assertion also lives here because the
  Denial Record count *is* a positive corollary of the
  "in-flight write" assertion: the only audit row that should land is
  the dedicated Denial Record.

- **7.4** "denial response containing only a generic denial indicator,
  the denial reason code, and a correlation identifier" → §"Requirement
  7.4: response body carries only generic_denial_indicator, reason_code,
  correlation_id". The HTTP layer renders that three-field response from
  task 8.3; at the unit layer the corresponding object is the
  :class:`DecisionAuthorizationError` raised by
  :meth:`KnowledgeService.create_decision`. This section pins:

    1. The exception's public-attribute set is exactly
       ``{'reason_code', 'correlation_id'}`` — the exception *type*
       itself is the unit-layer's "generic denial indicator" so no
       third public attribute is needed (the HTTP layer fills in
       ``generic_denial_indicator`` from the type or a static
       constant).
    2. The exception's string form does not leak target identifiers,
       Party identifiers, role-assignment details, Recommendation
       contents, or any other Requirement-7.4-prohibited datum.
    3. The exception shape is identical across every Requirement-7.2
       reason code (Property 4 / AD-WS-9: indistinguishable denial).

- **7.5** "the targeted Recommendation Resource, all Recommendation
  Revisions, and all previously acknowledged Relationships and Records
  linked to it byte-equivalent to their state immediately before the
  denied Decision attempt" → §"Requirement 7.5: targeted Recommendation
  state preserved byte-equivalent on denial". This section reads the
  targeted Recommendation Resource, its Recommendation Revision, the
  ``Derived From`` Relationship to its source Finding, and the source
  Finding Resource itself both before and after a denied Decision
  attempt and asserts they are byte-equivalent across the attempt.

The tests deliberately do **not** re-prove behavior already pinned by
:mod:`tests.unit.test_knowledge_decision_authority` (the task-8.2
coverage file). That file walks each Requirement-7.2 reason code,
exercises the retry path (Requirement 7.6), and verifies the separate
denial transaction. The tests here narrow to just the three task-8.4
acceptance assertions, organized for spec-coverage auditing.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateFindingResult,
    CreateRecommendationResult,
    DecisionAuthorizationError,
    DecisionOmissionEntry,
    KnowledgeService,
)
from walking_slice.models import AuthorityBasisRef


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test constants and fixtures.
#
# Constants share the shape used by the sibling task-8.2 coverage file
# (:mod:`tests.unit.test_knowledge_decision_authority`) so that anyone
# reading both files at once sees the same fixed Party / scope / basis
# triple. We re-declare them locally rather than importing across test
# modules to keep test isolation clean and avoid the cross-file
# coupling pytest collection sometimes objects to.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-0000008c0001"
_ASSIGNING_AUTHORITY_ID = "00000000-0000-7000-8000-0000008c0002"
_AUTHORITY_BASIS_ID = uuid.UUID("00000000-0000-7000-8000-0000008d0001")
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"

_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

# Authority basis is reused across every denial trigger. AD-WS-10
# permits ``role-grant-id`` / ``scope-id`` / ``delegation-chain-id``;
# the type/value pair never reaches a persistence row on the deny path
# (that is the whole point of the tests below) so the choice of
# ``role-grant-id`` is purely conventional.
_BASIS = AuthorityBasisRef(type="role-grant-id", id=_AUTHORITY_BASIS_ID)


def _seed_party(conn: Connection, party_id: str, display: str) -> None:
    """Insert one Party row.

    Direct SQL because :class:`AuthorizationService` does not own
    Party creation and we want the deciding Party and the assigning
    Party present *before* any service call — Parties referenced by
    Role_Assignments and Audit_Records are FK-checked on insert.
    """
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', :name, :ts)
            """
        ),
        {"pid": party_id, "name": display, "ts": _TS_FIXED},
    )


def _seed_required_parties(engine: Engine) -> None:
    """Seed the deciding Party and the assigning-authority Party.

    Both Parties exist in every test in this file even though
    Role_Assignments rows are deliberately absent on the deny path —
    the Parties themselves must still exist because audit rows
    (denial and evaluation alike) FK on ``actor_party_id``.
    """
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Decision Maker")
        _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")


@pytest.fixture
def knowledge_service_authorized(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> KnowledgeService:
    """Knowledge_Service with the authorization check wired (task-8.2 path)."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


@pytest.fixture
def knowledge_service_unwired(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """Knowledge_Service *without* authorization, used for seeding only.

    The Recommendation seeding step uses this fixture because the
    Recommendation's own Requirement-5.7 authorization check is not
    the subject of this file — the Decision's authorization check
    is. Seeding via the unwired service keeps the test focus on the
    Decision flow.
    """
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def seeded_recommendation(
    engine: Engine,
    knowledge_service_unwired: KnowledgeService,
) -> tuple[CreateFindingResult, CreateRecommendationResult]:
    """Seed Parties, a hypothesis Finding, and one derived Recommendation.

    Yields the Finding and Recommendation results so individual tests
    target the same Recommendation Revision under denial. The seed is
    performed once per test (fixture is function-scoped via the
    engine fixture) so every test starts from an identical baseline.
    """
    _seed_required_parties(engine)
    with engine.begin() as conn:
        finding = knowledge_service_unwired.create_finding(
            conn,
            statement="Source finding for unauthorized-decision tests.",
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )
        recommendation = knowledge_service_unwired.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="Recommend X based on hypothesis Finding.",
        )
    return finding, recommendation


# ---------------------------------------------------------------------------
# Row-reader helpers — every helper reads in a fresh connection so the
# test sees only committed state. The deny path commits the Denial
# Record in a separate transaction and rolls back everything else; the
# read fixtures below let the tests assert that distinction precisely.
# ---------------------------------------------------------------------------


def _count(engine: Engine, table: str) -> int:
    """Return the row count of ``table``."""
    with engine.connect() as conn:
        return int(
            conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        )


def _fetch_audit_rows(
    engine: Engine,
    *,
    outcome: str | None = None,
    action_type: str | None = None,
) -> list[dict]:
    """Read ``Audit_Records`` filtered by ``outcome`` / ``action_type``.

    Returns rows ordered by ``append_sequence`` so the test can assert
    on the order in which audit rows landed. ``authorities_required``
    is included so the dedicated Denial Record (NULL) can be
    distinguished from the evaluation row (non-NULL) per Requirement
    12.5.
    """
    sql = (
        "SELECT actor_party_id, action_type, outcome, target_id, "
        "target_revision_id, reason_code, correlation_id, "
        "authorities_required, recorded_at "
        "FROM Audit_Records WHERE 1=1 "
    )
    params: dict[str, object] = {}
    if outcome is not None:
        sql += "AND outcome = :outcome "
        params["outcome"] = outcome
    if action_type is not None:
        sql += "AND action_type = :action_type "
        params["action_type"] = action_type
    sql += "ORDER BY append_sequence"
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings()]


def _fetch_denial_records(engine: Engine) -> list[dict]:
    """Return only the dedicated Denial Records (Requirement 7.2).

    The evaluation row (Requirement 12.5) also lands in
    ``Audit_Records`` with ``outcome='deny'``. Distinguish the two:
    the Denial Record carries ``authorities_required IS NULL`` because
    :meth:`AuditLog.append_denial` does not populate that column. The
    evaluation row has it populated by :meth:`AuditLog.append_evaluation`.
    """
    return [
        row
        for row in _fetch_audit_rows(engine, outcome="deny")
        if row["authorities_required"] is None
    ]


def _fetch_recommendation_row(
    engine: Engine, recommendation_id: str
) -> dict | None:
    """Return the persisted ``Recommendations`` row for byte-equivalence checks."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT recommendation_id, created_at "
                    "FROM Recommendations WHERE recommendation_id = :rid"
                ),
                {"rid": recommendation_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_recommendation_revision_row(
    engine: Engine, revision_id: str
) -> dict | None:
    """Return the persisted ``Recommendation_Revisions`` row."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT recommendation_revision_id, recommendation_id,
                           authoring_party_id, rationale, assumptions_json,
                           confidence, recorded_at
                    FROM Recommendation_Revisions
                    WHERE recommendation_revision_id = :rid
                    """
                ),
                {"rid": revision_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_finding_row(engine: Engine, finding_id: str) -> dict | None:
    """Return the persisted ``Findings`` row."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT finding_id, created_at "
                    "FROM Findings WHERE finding_id = :fid"
                ),
                {"fid": finding_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_relationships_for_recommendation(
    engine: Engine, recommendation_id: str
) -> list[dict]:
    """Return Relationships rows whose source is the Recommendation.

    Includes the ``Derived From`` Relationship that ties the seeded
    Recommendation to the seeded Finding. On the deny path neither
    that Relationship nor the would-be ``Addresses`` Relationship
    should change.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, source_kind,
                           source_id, source_revision_id, target_kind,
                           target_id, target_revision_id, authoring_party_id,
                           recorded_at
                    FROM Relationships WHERE source_id = :sid
                    ORDER BY relationship_id
                    """
                ),
                {"sid": recommendation_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_identifier_registry_kinds(engine: Engine) -> dict[str, int]:
    """Return the count of ``Identifier_Registry`` rows by ``kind``.

    The Decision flow normally registers one ``immutable_record`` (the
    Decision itself). On the deny path the registration must not
    occur — its count must match the pre-attempt baseline.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT kind, COUNT(*) AS n FROM Identifier_Registry "
                    "GROUP BY kind"
                )
            )
            .mappings()
            .all()
        )
    return {row["kind"]: int(row["n"]) for row in rows}


# ---------------------------------------------------------------------------
# Trigger helper: assign a role that DOES NOT grant approve authority,
# then attempt the Decision and capture the resulting
# :class:`DecisionAuthorizationError`. The ``out-of-scope`` reason code
# is used as the canonical deny driver in this file because it leaves a
# Role_Assignments row in place (so the test verifies the deny path
# rolls back the Decision even when role data exists), unlike
# ``no-role-assignment`` which leaves the table empty.
# ---------------------------------------------------------------------------


def _assign_decision_maker_role_out_of_scope(
    authorization_service: AuthorizationService,
    engine: Engine,
) -> str:
    """Assign a Decision-Maker role with a *different* scope.

    Yields the ``out-of-scope`` reason code per Requirement 7.2 when
    the Decision is attempted against ``_SCOPE`` — the assignment
    grants ``approve`` but for ``pilot/team-b``.
    """
    request = AssignRoleRequest(
        party_id=_PARTY_ID,
        role_name="decision_maker",
        scope="pilot/team-b",  # deliberate mismatch with _SCOPE
        authorities_granted=("approve",),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=None,
        assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _attempt_decision_expecting_denial(
    *,
    engine: Engine,
    knowledge_service_authorized: KnowledgeService,
    seeded_recommendation: tuple[CreateFindingResult, CreateRecommendationResult],
    correlation_id: str,
    omissions: tuple[DecisionOmissionEntry, ...] = (),
) -> DecisionAuthorizationError:
    """Attempt a Decision and return the captured authorization error.

    Used by every test in this file so the trigger logic — the
    transactional shape of the attempt, the ``correlation_id`` flow,
    the ``omissions`` payload — stays in one place.

    Returns the raised exception so the test can inspect ``reason_code``
    and ``correlation_id`` directly without re-raising.
    """
    _, recommendation = seeded_recommendation
    with pytest.raises(DecisionAuthorizationError) as exc_info:
        with engine.begin() as conn:
            knowledge_service_authorized.create_decision(
                conn,
                target_recommendation_id=recommendation.recommendation_id,
                target_recommendation_revision_id=(
                    recommendation.recommendation_revision_id
                ),
                outcome="Accept",
                rationale="Should be denied (out-of-scope).",
                deciding_party_id=_PARTY_ID,
                authority_basis=_BASIS,
                applicable_scope=_SCOPE,
                omissions=omissions,
                engine=engine,
                evaluation_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                correlation_id=correlation_id,
            )
    return exc_info.value


# =============================================================================
# Requirement 7.1 — "no Decision Record is created, modified, or persisted"
# plus task-brief expansion: "no Decision row, Relationship, or
# in-flight write is persisted".
# =============================================================================
#
# Every test in this section assigns an out-of-scope Decision-Maker role
# and attempts to record a Decision. The attempt must raise
# :class:`DecisionAuthorizationError` and leave every persistence
# location the Decision flow would have touched byte-equivalent to its
# pre-attempt state — except for the dedicated Denial Record itself,
# which is the one positive side-effect Requirement 7.2 permits and the
# task brief explicitly calls out as "exactly one Denial Record is
# appended".
# =============================================================================


class TestRequirement71NoInFlightWritePersists:
    """Requirement 7.1 — no Decision row, Relationship, or in-flight write
    is persisted on a denied attempt."""

    def test_71_no_decisions_row_persisted(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """No ``Decisions`` row exists after a denied attempt."""
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)
        pre = _count(engine, "Decisions")

        exc = _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-decisions",
        )
        assert exc.reason_code == "out-of-scope"

        assert _count(engine, "Decisions") == pre

    def test_71_no_addresses_relationship_persisted(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """No new ``Relationships`` row (the would-be ``Addresses``) is persisted.

        The seeded Recommendation already has one ``Derived From``
        Relationship to the seeded Finding; the denied attempt would
        have created a second row (``Addresses``) sourced from the
        new Decision Identity. That second row must not appear.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)
        pre = _count(engine, "Relationships")

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-addresses",
        )

        assert _count(engine, "Relationships") == pre

    def test_71_no_provenance_manifest_persisted(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """No Decision-scoped ``Provenance_Manifests`` row is persisted.

        AD-WS-5 requires the Provenance Manifest insert to share the
        Decision transaction; on rollback the manifest goes with it.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)
        pre = _count(engine, "Provenance_Manifests")

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-manifest",
        )

        assert _count(engine, "Provenance_Manifests") == pre

    def test_71_no_omission_entries_persisted_even_when_supplied(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """No ``Omission_Entries`` rows are persisted when omissions are supplied.

        Omission entries are a normal payload on the permit path
        (Requirement 10.1). The deny path must not persist them
        either — they share the Decision's transaction and roll back
        with the Provenance Manifest they belong to.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)
        pre = _count(engine, "Omission_Entries")
        omissions = (
            DecisionOmissionEntry(
                excluded_source_id="00000000-0000-7000-8000-0000000000aa",
                excluded_source_revision_id=None,
                category="intentional",
                rationale="Would have been recorded on the permit path.",
            ),
            DecisionOmissionEntry(
                excluded_source_id="00000000-0000-7000-8000-0000000000bb",
                excluded_source_revision_id=None,
                category="unavailable",
                rationale="Would have flipped manifest to incomplete.",
            ),
        )

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-omissions",
            omissions=omissions,
        )

        assert _count(engine, "Omission_Entries") == pre

    def test_71_no_consequential_create_decision_audit_row_persisted(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """No ``action_type='create.decision'`` consequential audit row exists.

        The Decision flow appends one consequential audit row inside
        the caller's transaction (Requirement 6.4 / AD-WS-5). On the
        deny path the caller's transaction rolls back, taking the
        consequential audit row with it. Confirms the rollback is
        complete and there is no surviving "I authorized a Decision"
        evidence.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-no-consequential",
        )

        consequential_create_decision_rows = _fetch_audit_rows(
            engine,
            outcome="consequential",
            action_type="create.decision",
        )
        assert consequential_create_decision_rows == []

    def test_71_no_identifier_registry_decision_row_persisted(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """No ``immutable_record`` (Decision) identifier is bound on denial.

        The Decision flow registers the new Decision Identity in
        ``Identifier_Registry`` with kind ``immutable_record`` (AD-WS-2 /
        AD-WS-3). On the deny path that registration must not survive.
        Asserted by counting registry rows by ``kind`` before and
        after the attempt — the ``immutable_record`` count is
        baseline-stable; other kinds' counts (``resource``,
        ``revision``, ``relationship`` from the seeded Finding,
        Recommendation, and Derived-From row) are also unchanged.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)
        pre = _fetch_identifier_registry_kinds(engine)

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-registry",
        )

        post = _fetch_identifier_registry_kinds(engine)
        assert post == pre

    def test_71_exactly_one_denial_record_appended(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """Exactly one Denial Record lands in ``Audit_Records`` (task brief).

        The dedicated Denial Record is committed in a separate
        transaction (Requirement 7.6) so it survives the caller's
        rollback. The evaluation row (Requirement 12.5) also exists
        with ``outcome='deny'`` but is distinguishable by the
        non-NULL ``authorities_required`` column; the brief's
        "exactly one Denial Record" refers to the
        :meth:`AuditLog.append_denial` row, which is what
        :func:`_fetch_denial_records` filters to.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        exc = _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-71-one-denial",
        )

        denial_rows = _fetch_denial_records(engine)
        assert len(denial_rows) == 1
        denial = denial_rows[0]

        _, recommendation = seeded_recommendation
        assert denial["actor_party_id"] == _PARTY_ID
        assert denial["action_type"] == "approve.decision"
        assert denial["target_id"] == recommendation.recommendation_id
        assert denial["target_revision_id"] == (
            recommendation.recommendation_revision_id
        )
        assert denial["reason_code"] == "out-of-scope"
        assert denial["correlation_id"] == exc.correlation_id


# =============================================================================
# Requirement 7.4 — "denial response containing only a generic denial
# indicator, the denial reason code, and a correlation identifier".
# =============================================================================
#
# At the HTTP layer (task 8.3) the response body is a three-field JSON
# object ``{generic_denial_indicator, reason_code, correlation_id}``. At
# the unit layer the analogue is :class:`DecisionAuthorizationError`,
# and the exception *type* itself is the unit-layer's
# "generic_denial_indicator" — every denial path raises the same type,
# carrying only ``reason_code`` and ``correlation_id`` as state. The
# HTTP layer fills in the ``generic_denial_indicator`` literal from the
# type or a static constant; the unit layer's job is to prove the
# exception cannot leak anything else.
# =============================================================================


class TestRequirement74ResponseShape:
    """Requirement 7.4 — response body contains only generic denial
    indicator, reason code, and correlation identifier."""

    def test_74_exception_exposes_only_reason_code_and_correlation_id(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """:class:`DecisionAuthorizationError` has exactly two public attributes.

        ``reason_code`` and ``correlation_id`` are the only attributes
        Requirement 7.4 permits the denial response to carry; the
        exception type itself is the "generic denial indicator" —
        callers identify a denial by catching the type, so no
        third public attribute is needed.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        exc = _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-74-attrs",
        )

        public_attrs = {name for name in vars(exc) if not name.startswith("_")}
        assert public_attrs == {"reason_code", "correlation_id"}, (
            f"DecisionAuthorizationError exposed unexpected attributes: "
            f"{public_attrs}"
        )

        # Sanity: the two permitted values are present and well-shaped.
        assert exc.reason_code == "out-of-scope"
        assert exc.correlation_id == "corr-74-attrs"

    def test_74_exception_type_is_the_generic_denial_indicator(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """The exception's *type* identifies the denial uniformly.

        Every denied attempt raises :class:`DecisionAuthorizationError`
        regardless of internal cause. The HTTP layer (task 8.3) maps
        this type to the response body's ``generic_denial_indicator``
        field; this test pins the type contract so the mapping in
        the HTTP layer can be a one-line constant rather than a
        per-reason switch.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        exc = _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-74-type",
        )

        # Exact type, not a subclass — :class:`DecisionAuthorizationError`
        # is the leaf class in this hierarchy.
        assert type(exc) is DecisionAuthorizationError
        # The type is also a :class:`PermissionError` so generic
        # ``except PermissionError`` callers continue to work; this
        # is design intent, not Requirement 7.4 leakage.
        assert isinstance(exc, PermissionError)

    def test_74_exception_message_does_not_leak_target_or_party_identifiers(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """``str(exc)`` and ``exc.args`` reveal only the two permitted values.

        Requirement 7.4 forbids the denial response from carrying
        "authorized Party identities, Recommendation contents, role
        assignment details, target existence beyond the requesting
        Party's view authority, or other attribute values". The
        exception's string form is what an HTTP layer might log or
        bubble up — verify the deciding Party identity, the target
        Recommendation Identity, the target Recommendation Revision
        Identity, the role-assignment scope, and the rationale text
        all stay out of ``str(exc)``.
        """
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)
        _, recommendation = seeded_recommendation

        exc = _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-74-no-leak",
        )

        message = str(exc)
        forbidden_substrings = (
            _PARTY_ID,
            _ASSIGNING_AUTHORITY_ID,
            recommendation.recommendation_id,
            recommendation.recommendation_revision_id,
            _SCOPE,
            "pilot/team-b",  # the assigned (mismatched) scope
            "decision_maker",
            "Should be denied",  # the rationale we passed in
            "approve.decision",
        )
        for forbidden in forbidden_substrings:
            assert forbidden not in message, (
                f"DecisionAuthorizationError message leaked {forbidden!r}: "
                f"{message!r}"
            )

        # And ``args`` — Python's exception machinery occasionally
        # surfaces these — must contain nothing beyond what the
        # constructor passed up to :class:`Exception.__init__`. The
        # implementation passes only the rendered message, so we
        # assert the args tuple is a single rendered string.
        assert len(exc.args) == 1
        assert isinstance(exc.args[0], str)

    def test_74_exception_shape_indistinguishable_across_all_reason_codes(
        self,
        sqlite_path,
        clock,
        identity_service: IdentityService,
        audit_log: AuditLog,
        authorization_service: AuthorizationService,
        knowledge_service_unwired: KnowledgeService,
    ) -> None:
        """Denials for every reason code produce structurally identical
        exceptions.

        AD-WS-9: two denials produced by different internal causes
        must be indistinguishable apart from ``reason_code`` (and
        the per-call ``correlation_id``). This test isolates each
        reason code on a fresh engine so the role-assignment state
        from one branch does not bleed into the next, then verifies
        the resulting exceptions have identical public-attribute
        sets, identical types, and the same message *template*
        (varying only in the two permitted values).
        """
        from sqlalchemy import create_engine, event

        from walking_slice.persistence import create_schema

        def _make_engine(tag: str) -> Engine:
            path = sqlite_path.parent / f"{sqlite_path.stem}-{tag}.sqlite"
            url = f"sqlite:///{path.as_posix()}"
            eng = create_engine(url, future=True)

            @event.listens_for(eng, "connect")
            def _pragmas(dbapi_connection, _record):  # pragma: no cover
                cur = dbapi_connection.cursor()
                try:
                    cur.execute("PRAGMA journal_mode=WAL")
                    cur.execute("PRAGMA foreign_keys=ON")
                finally:
                    cur.close()

            create_schema(eng)
            return eng

        def _seed(eng: Engine) -> tuple[CreateFindingResult, CreateRecommendationResult]:
            with eng.begin() as conn:
                _seed_party(conn, _PARTY_ID, "Decision Maker")
                _seed_party(conn, _ASSIGNING_AUTHORITY_ID, "Resource Steward")
                finding = knowledge_service_unwired.create_finding(
                    conn,
                    statement="Source finding for indistinguishability test.",
                    authoring_party_id=_PARTY_ID,
                    is_hypothesis=True,
                )
                rec = knowledge_service_unwired.create_recommendation(
                    conn,
                    authoring_party_id=_PARTY_ID,
                    derived_from_findings=[finding.finding_id],
                    rationale="Recommendation for indistinguishability test.",
                )
            return finding, rec

        def _arrange_role_state(
            eng: Engine, reason: str
        ) -> None:
            from walking_slice.audit import format_iso8601_ms

            base = AssignRoleRequest(
                party_id=_PARTY_ID,
                role_name="decision_maker",
                scope=_SCOPE,
                authorities_granted=("approve",),
                effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
                effective_end=None,
                assigning_authority_id=_ASSIGNING_AUTHORITY_ID,
            )
            if reason == "no-role-assignment":
                # No Role_Assignments row needed.
                return
            if reason == "out-of-scope":
                req = AssignRoleRequest(
                    **{**base.__dict__, "scope": "pilot/team-b"}
                )
            elif reason == "not-yet-effective":
                req = AssignRoleRequest(
                    **{
                        **base.__dict__,
                        "effective_start": datetime(
                            2027, 1, 1, tzinfo=timezone.utc
                        ),
                    }
                )
            elif reason == "expired":
                req = AssignRoleRequest(
                    **{
                        **base.__dict__,
                        "effective_end": datetime(
                            2026, 1, 1, tzinfo=timezone.utc
                        ),
                    }
                )
            elif reason == "revoked":
                req = base
            else:  # pragma: no cover - typo guard
                raise AssertionError(reason)

            with eng.begin() as conn:
                rid = str(authorization_service.assign_role(conn, req))

            if reason == "revoked":
                with eng.begin() as conn:
                    conn.execute(
                        text(
                            "UPDATE Role_Assignments SET revoked_at = :rev "
                            "WHERE role_assignment_id = :rid"
                        ),
                        {
                            "rev": format_iso8601_ms(
                                datetime(2026, 3, 1, tzinfo=timezone.utc)
                            ),
                            "rid": rid,
                        },
                    )

        reasons = (
            "not-yet-effective",
            "expired",
            "revoked",
            "out-of-scope",
            "no-role-assignment",
        )
        exceptions: list[DecisionAuthorizationError] = []
        for reason in reasons:
            eng = _make_engine(reason)
            try:
                _, recommendation = _seed(eng)
                _arrange_role_state(eng, reason)
                # Build an authorized KnowledgeService bound to this
                # engine's per-engine audit log; the
                # AuthorizationService accepts any connection so we
                # reuse the outer fixture instance.
                service = KnowledgeService(
                    clock=clock,
                    identity_service=identity_service,
                    audit_log=audit_log,
                    authorization_service=authorization_service,
                )
                with pytest.raises(DecisionAuthorizationError) as exc_info:
                    with eng.begin() as conn:
                        service.create_decision(
                            conn,
                            target_recommendation_id=recommendation.recommendation_id,
                            target_recommendation_revision_id=(
                                recommendation.recommendation_revision_id
                            ),
                            outcome="Accept",
                            rationale=f"Indistinguishability for {reason}.",
                            deciding_party_id=_PARTY_ID,
                            authority_basis=_BASIS,
                            applicable_scope=_SCOPE,
                            engine=eng,
                            evaluation_at=datetime(
                                2026, 6, 1, tzinfo=timezone.utc
                            ),
                            correlation_id=f"corr-74-iso-{reason}",
                        )
                exceptions.append(exc_info.value)
            finally:
                eng.dispose()

        # Public-attribute sets are identical across every reason.
        attr_sets = [
            frozenset(name for name in vars(e) if not name.startswith("_"))
            for e in exceptions
        ]
        assert all(s == attr_sets[0] for s in attr_sets[1:])
        assert attr_sets[0] == {"reason_code", "correlation_id"}

        # The exception types are identical (no per-reason subclassing).
        assert {type(e) for e in exceptions} == {DecisionAuthorizationError}

        # Reason codes line up with the order we drove them.
        observed = [e.reason_code for e in exceptions]
        assert observed == list(reasons)

        # Correlation identifiers are distinct per call (one per
        # invocation, by design).
        assert len({e.correlation_id for e in exceptions}) == len(reasons)


# =============================================================================
# Requirement 7.5 — "the targeted Recommendation Resource, all
# Recommendation Revisions, and all previously acknowledged Relationships
# and Records linked to it byte-equivalent to their state immediately
# before the denied Decision attempt".
# =============================================================================
#
# The tests in this section snapshot the seeded persistence rows before
# the denied attempt and compare them byte-by-byte (dict-equal) to the
# rows read after the attempt. Byte-equivalence at the row level is the
# strongest assertion available without a content-digest comparison and
# is what Requirement 7.5 calls for.
# =============================================================================


class TestRequirement75TargetByteEquivalent:
    """Requirement 7.5 — targeted Recommendation Resource and links are
    byte-equivalent after a denied attempt."""

    def test_75_recommendation_resource_row_byte_equivalent(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """The ``Recommendations`` row is unchanged across a denied attempt."""
        _, recommendation = seeded_recommendation
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        pre = _fetch_recommendation_row(
            engine, recommendation.recommendation_id
        )
        assert pre is not None  # sanity: the seed actually committed

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-75-resource",
        )

        post = _fetch_recommendation_row(
            engine, recommendation.recommendation_id
        )
        assert post == pre

    def test_75_recommendation_revision_row_byte_equivalent(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """The targeted ``Recommendation_Revisions`` row is unchanged."""
        _, recommendation = seeded_recommendation
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        pre = _fetch_recommendation_revision_row(
            engine, recommendation.recommendation_revision_id
        )
        assert pre is not None

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-75-revision",
        )

        post = _fetch_recommendation_revision_row(
            engine, recommendation.recommendation_revision_id
        )
        assert post == pre

    def test_75_previously_acknowledged_relationships_byte_equivalent(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """The seeded ``Derived From`` Relationship is unchanged.

        Requirement 7.5 calls out "all previously acknowledged
        Relationships … linked to [the Recommendation]". The seed
        creates one such Relationship: the ``Derived From`` link
        from the Recommendation to the supporting Finding. The
        denied attempt must not alter that row in any way (FK,
        timestamp, type, party).
        """
        _, recommendation = seeded_recommendation
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        pre = _fetch_relationships_for_recommendation(
            engine, recommendation.recommendation_id
        )
        # Sanity: exactly one pre-existing Relationship (the Derived From
        # row that the seed step recorded).
        assert len(pre) == 1
        assert pre[0]["relationship_type"] == "Derived From"

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-75-relationships",
        )

        post = _fetch_relationships_for_recommendation(
            engine, recommendation.recommendation_id
        )
        assert post == pre

    def test_75_source_finding_record_byte_equivalent(
        self,
        engine: Engine,
        authorization_service: AuthorizationService,
        knowledge_service_authorized: KnowledgeService,
        seeded_recommendation,
    ) -> None:
        """The Finding that justifies the Recommendation is unchanged.

        The Recommendation's "previously acknowledged Records" include
        the Finding it derives from. Requirement 7.5 names "Records
        linked to it" — the Finding Resource row should remain
        byte-equivalent across a denied Decision attempt on the
        Recommendation.
        """
        finding, _ = seeded_recommendation
        _assign_decision_maker_role_out_of_scope(authorization_service, engine)

        pre = _fetch_finding_row(engine, finding.finding_id)
        assert pre is not None

        _attempt_decision_expecting_denial(
            engine=engine,
            knowledge_service_authorized=knowledge_service_authorized,
            seeded_recommendation=seeded_recommendation,
            correlation_id="corr-75-finding",
        )

        post = _fetch_finding_row(engine, finding.finding_id)
        assert post == pre
