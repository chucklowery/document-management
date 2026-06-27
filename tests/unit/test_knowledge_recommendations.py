"""Unit tests for :mod:`walking_slice.knowledge` — Recommendations.

These tests pin the contract established in task 7.1, design
§"Knowledge_Service" + §"Recommendations and Recommendation_Revisions"
+ §"Relationships", and Requirements 5.1 through 5.7:

- 5.1 — a valid Recommendation with 1..50 ``Derived From`` references
  records one Recommendation Resource, one Recommendation Revision, one
  ``Derived From`` Relationship row per Finding identity, and one
  consequential audit row, all inside the caller's transaction (AD-WS-5).
- 5.2 — the Recommendation Resource carries an Identity distinct from
  every Finding and Decision (verified indirectly here by the fact that
  the Resource Identity comes from a fresh UUIDv7 — Property 10 covers
  the universal claim).
- 5.3 — rationale, when supplied, persists 1..10,000 characters on the
  Recommendation Revision.
- 5.4 — assumptions, when supplied, persists 0..50 entries × 1..2,000
  characters on the Recommendation Revision.
- 5.5 — confidence, when supplied, persists a value from
  ``{Low, Medium, High}`` on the Recommendation Revision.
- 5.6 — a Recommendation submitted with zero ``Derived From`` references
  or with any reference that does not resolve is rejected; no Resource,
  Revision, or Relationship row is observable post-rejection.
- 5.7 — an unauthenticated caller (empty ``authoring_party_id``) is
  rejected with :class:`RecommendationValidationError`, and a caller
  without effective Analyst role for the applicable scope (i.e. the
  wired :class:`AuthorizationService` returns ``deny``) is rejected with
  :class:`RecommendationAuthorizationError`. Neither case persists a
  Recommendation row.

The audit row for ``create.recommendation`` is also covered (Requirement
13.1) so Property 11 (audit completeness) has the action recorded.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.authorization import (
    AssignRoleRequest,
    AuthorizationService,
)
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateFindingResult,
    CreateRecommendationResult,
    KnowledgeService,
    RecommendationAuthorizationError,
    RecommendationNotResolvableError,
    RecommendationValidationError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seeding helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_ASSIGNING_PARTY_ID = "00000000-0000-7000-8000-000000000002"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000000003"
_SCOPE = "pilot/team-a"
_TS_FIXED = "2026-01-01T00:00:00.000Z"
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def _seed_party(conn, party_id: str = _PARTY_ID, display: str = "Analyst") -> None:
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
#
# ``knowledge_service`` here is wired *without* an authorization_service so
# the back-compatible code path is exercised. Tests that need authorization
# enforcement use ``knowledge_service_authorized`` instead.
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_service(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
) -> KnowledgeService:
    """Knowledge_Service without authorization (back-compat path)."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
    )


@pytest.fixture
def knowledge_service_authorized(
    clock,
    identity_service: IdentityService,
    audit_log: AuditLog,
    authorization_service: AuthorizationService,
) -> KnowledgeService:
    """Knowledge_Service with authorization enforced (Requirement 5.7)."""
    return KnowledgeService(
        clock=clock,
        identity_service=identity_service,
        audit_log=audit_log,
        authorization_service=authorization_service,
    )


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _create_hypothesis_finding(
    engine: Engine,
    knowledge_service: KnowledgeService,
    *,
    statement: str,
    seed_party: bool = False,
) -> CreateFindingResult:
    """Create a hypothesis Finding (no supports needed) for use as a
    Derived From target.

    ``seed_party`` controls whether to insert the Parties row first; the
    helper offers it as a flag because most tests seed the Party once in
    a setup block before calling this helper repeatedly.
    """
    with engine.begin() as conn:
        if seed_party:
            _seed_party(conn)
        return knowledge_service.create_finding(
            conn,
            statement=statement,
            authoring_party_id=_PARTY_ID,
            is_hypothesis=True,
        )


def _create_findings(
    engine: Engine, knowledge_service: KnowledgeService, *, count: int
) -> list[CreateFindingResult]:
    """Seed Party (once) and create ``count`` distinct hypothesis Findings."""
    findings: list[CreateFindingResult] = []
    for index in range(count):
        findings.append(
            _create_hypothesis_finding(
                engine,
                knowledge_service,
                statement=f"hypothesis finding {index}",
                seed_party=(index == 0),
            )
        )
    return findings


def _assign_analyst_role(
    authorization_service: AuthorizationService,
    engine: Engine,
    *,
    party_id: str = _PARTY_ID,
    scope: str = _SCOPE,
    effective_end: datetime | None = None,
    authorities: Iterable[str] = ("view", "modify"),
) -> str:
    """Grant ``party_id`` an Analyst role with ``modify`` authority.

    An Analyst's effective authority over Recommendation creation is the
    ``modify`` authority type — design §"Authorization_Service"
    (ActionType enumeration) maps ``create.recommendation`` to that type
    (Requirement 12.4 — no substitution between view/modify/approve).
    Granting ``view`` alongside is harmless and mirrors how a real
    Analyst role would be configured.
    """
    request = AssignRoleRequest(
        party_id=party_id,
        role_name="analyst",
        scope=scope,
        authorities_granted=tuple(authorities),
        effective_start=datetime(2025, 1, 1, tzinfo=timezone.utc),
        effective_end=effective_end,
        assigning_authority_id=_ASSIGNING_PARTY_ID,
    )
    with engine.begin() as conn:
        return str(authorization_service.assign_role(conn, request))


def _fetch_audit_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT actor_party_id, action_type, outcome, target_id, "
                    "target_revision_id, correlation_id, recorded_at "
                    "FROM Audit_Records ORDER BY append_sequence"
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_relationships_by_source(engine: Engine, *, source_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, source_kind,
                           source_id, source_revision_id, target_kind,
                           target_id, target_revision_id, authoring_party_id,
                           recorded_at
                    FROM Relationships
                    WHERE source_id = :source_id
                    ORDER BY relationship_id
                    """
                ),
                {"source_id": source_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_recommendation(engine: Engine, *, recommendation_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT recommendation_id, created_at FROM Recommendations "
                    "WHERE recommendation_id = :rid"
                ),
                {"rid": recommendation_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_recommendation_revision(
    engine: Engine, *, recommendation_revision_id: str
) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT recommendation_revision_id, recommendation_id,
                           parent_revision_id, rationale, assumptions_json,
                           confidence, authoring_party_id, recorded_at
                    FROM Recommendation_Revisions
                    WHERE recommendation_revision_id = :rrid
                    """
                ),
                {"rrid": recommendation_revision_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# create_recommendation — happy paths.
# ---------------------------------------------------------------------------


def test_create_recommendation_with_one_derived_from_persists_rows(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """A Recommendation with one Derived From Finding persists the
    Recommendations row, the Recommendation_Revisions row, and one
    Derived From Relationships row."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="Source finding for the recommendation.",
        seed_party=True,
    )

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale="We recommend X based on the source finding.",
            assumptions=["Stable funding for the pilot."],
            confidence="High",
            correlation_id="corr-create-rec",
        )

    assert isinstance(result, CreateRecommendationResult)
    assert _CANONICAL_UUID7.match(result.recommendation_id)
    assert _CANONICAL_UUID7.match(result.recommendation_revision_id)
    assert result.recommendation_id != result.recommendation_revision_id
    assert result.rationale == "We recommend X based on the source finding."
    assert result.assumptions == ("Stable funding for the pilot.",)
    assert result.confidence == "High"
    assert len(result.derived_from_relationship_ids) == 1
    assert _CANONICAL_UUID7.match(result.derived_from_relationship_ids[0])
    assert _ISO_8601_MS_PATTERN.match(result.recorded_at)

    recommendation_row = _fetch_recommendation(
        engine, recommendation_id=result.recommendation_id
    )
    assert recommendation_row is not None
    assert recommendation_row["created_at"] == _TS_FIXED

    revision_row = _fetch_recommendation_revision(
        engine, recommendation_revision_id=result.recommendation_revision_id
    )
    assert revision_row is not None
    assert revision_row["recommendation_id"] == result.recommendation_id
    assert revision_row["parent_revision_id"] is None
    assert revision_row["rationale"] == "We recommend X based on the source finding."
    assert json.loads(revision_row["assumptions_json"]) == [
        "Stable funding for the pilot."
    ]
    assert revision_row["confidence"] == "High"
    assert revision_row["authoring_party_id"] == _PARTY_ID
    assert revision_row["recorded_at"] == _TS_FIXED

    relationships = _fetch_relationships_by_source(
        engine, source_id=result.recommendation_id
    )
    assert len(relationships) == 1
    rel = relationships[0]
    assert rel["relationship_id"] == result.derived_from_relationship_ids[0]
    assert rel["relationship_type"] == "Derived From"
    assert rel["source_kind"] == "recommendation_revision"
    assert rel["source_id"] == result.recommendation_id
    assert rel["source_revision_id"] == result.recommendation_revision_id
    assert rel["target_kind"] == "finding"
    assert rel["target_id"] == finding.finding_id
    assert rel["target_revision_id"] is None
    assert rel["authoring_party_id"] == _PARTY_ID
    assert rel["recorded_at"] == _TS_FIXED


def test_create_recommendation_with_twenty_five_derived_from_creates_each_relationship(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """25 Derived From references produce 25 Relationships rows."""
    findings = _create_findings(engine, knowledge_service, count=25)
    finding_ids = [f.finding_id for f in findings]

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=finding_ids,
        )

    assert len(result.derived_from_relationship_ids) == 25
    relationships = _fetch_relationships_by_source(
        engine, source_id=result.recommendation_id
    )
    assert len(relationships) == 25
    assert all(r["relationship_type"] == "Derived From" for r in relationships)
    assert {r["target_id"] for r in relationships} == set(finding_ids)


def test_create_recommendation_with_fifty_derived_from_is_accepted(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """The upper bound of Requirement 5.1 is 50 — exactly 50 is accepted."""
    findings = _create_findings(engine, knowledge_service, count=50)
    finding_ids = [f.finding_id for f in findings]

    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=finding_ids,
        )
    assert len(result.derived_from_relationship_ids) == 50

    relationships = _fetch_relationships_by_source(
        engine, source_id=result.recommendation_id
    )
    assert len(relationships) == 50


def test_create_recommendation_without_optional_fields_persists_nulls(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """rationale, assumptions, and confidence are all optional.

    With none supplied, the Recommendation Revision persists rationale
    and confidence as SQL NULL and assumptions_json as ``"[]"`` (the
    NOT NULL column accepts an empty JSON array).
    """
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="Optional-fields-omitted finding.",
        seed_party=True,
    )
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
        )

    revision_row = _fetch_recommendation_revision(
        engine, recommendation_revision_id=result.recommendation_revision_id
    )
    assert revision_row is not None
    assert revision_row["rationale"] is None
    assert revision_row["assumptions_json"] == "[]"
    assert revision_row["confidence"] is None
    assert result.rationale is None
    assert result.assumptions == ()
    assert result.confidence is None


# ---------------------------------------------------------------------------
# create_recommendation — Requirement 5.6 rejection (count + resolvability).
# ---------------------------------------------------------------------------


def test_create_recommendation_rejects_zero_derived_from(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.6: zero Derived From entries is rejected; no
    Recommendations row is written."""
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[],
            )
    assert exc_info.value.failed_constraint == "derived_from_too_few"

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Recommendations")).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM Recommendation_Revisions")
            ).scalar_one()
            == 0
        )


def test_create_recommendation_rejects_more_than_fifty_derived_from(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.1 caps Derived From count at 50 — 51 is rejected."""
    findings = _create_findings(engine, knowledge_service, count=50)
    finding_ids = [f.finding_id for f in findings]
    # Pad to 51 by duplicating one Finding identity — duplicates are
    # allowed by Requirement 5.1's count-based limit but the 51st entry
    # still trips the upper bound.
    finding_ids.append(findings[0].finding_id)
    assert len(finding_ids) == 51

    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=finding_ids,
            )
    assert exc_info.value.failed_constraint == "derived_from_too_many"


def test_create_recommendation_rejects_unresolved_derived_from(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.6: any Derived From reference that does not
    resolve to an existing Finding is rejected; no Recommendation row
    is written."""
    bogus_finding = "00000000-0000-7000-8000-00000000beef"
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(RecommendationNotResolvableError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[bogus_finding],
            )
    assert exc_info.value.finding_id == bogus_finding
    assert exc_info.value.failed_constraint == "invalid_derived_from"

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Recommendations")).scalar_one()
            == 0
        )
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM Recommendation_Revisions")
            ).scalar_one()
            == 0
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Relationships")).scalar_one() == 0
        )


def test_create_recommendation_rejects_when_any_derived_from_is_unresolved(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """When the Derived From list mixes resolvable and unresolvable
    Findings, the entire write is rejected — no partial Recommendation
    or Relationships rows are inserted."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="Real finding.",
        seed_party=True,
    )
    bogus_finding = "00000000-0000-7000-8000-00000000fade"
    with engine.begin() as conn:
        with pytest.raises(RecommendationNotResolvableError):
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id, bogus_finding],
            )

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Recommendations")).scalar_one()
            == 0
        )
        # The only Relationships row should be the (zero) ones the
        # Finding-creation step did not create (hypothesis Findings have
        # no Supports rows).
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Relationships")).scalar_one() == 0
        )


# ---------------------------------------------------------------------------
# create_recommendation — Requirement 5.3 rationale boundaries.
# ---------------------------------------------------------------------------


def test_create_recommendation_rejects_empty_rationale(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.3: rationale, when supplied, must be 1..10,000
    characters. An explicit empty string is rejected."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="rationale-boundary finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="",
            )
    assert exc_info.value.failed_constraint == "rationale_empty"


def test_create_recommendation_accepts_rationale_at_max_length(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.3: exactly 10,000 characters is accepted."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="rationale-at-max finding",
        seed_party=True,
    )
    rationale = "x" * 10_000
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            rationale=rationale,
        )
    revision = _fetch_recommendation_revision(
        engine, recommendation_revision_id=result.recommendation_revision_id
    )
    assert revision is not None
    assert revision["rationale"] == rationale
    assert len(revision["rationale"]) == 10_000


def test_create_recommendation_rejects_rationale_over_max_length(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.3: 10,001 characters is rejected."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="rationale-over-max finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                rationale="x" * 10_001,
            )
    assert exc_info.value.failed_constraint == "rationale_too_long"


# ---------------------------------------------------------------------------
# create_recommendation — Requirement 5.4 assumption boundaries.
# ---------------------------------------------------------------------------


def test_create_recommendation_accepts_empty_assumptions(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.4 explicitly permits zero assumptions."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="zero-assumptions finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            assumptions=[],
        )
    assert result.assumptions == ()
    revision = _fetch_recommendation_revision(
        engine, recommendation_revision_id=result.recommendation_revision_id
    )
    assert revision is not None
    assert revision["assumptions_json"] == "[]"


def test_create_recommendation_accepts_fifty_assumptions(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """The upper bound of Requirement 5.4 is 50 entries — exactly 50 accepted."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="fifty-assumptions finding",
        seed_party=True,
    )
    assumptions = [f"assumption {i}" for i in range(50)]
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            assumptions=assumptions,
        )
    assert len(result.assumptions) == 50
    revision = _fetch_recommendation_revision(
        engine, recommendation_revision_id=result.recommendation_revision_id
    )
    assert revision is not None
    assert json.loads(revision["assumptions_json"]) == assumptions


def test_create_recommendation_rejects_fifty_one_assumptions(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.4: 51 assumptions is rejected."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="fifty-one-assumptions finding",
        seed_party=True,
    )
    assumptions = [f"assumption {i}" for i in range(51)]
    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                assumptions=assumptions,
            )
    assert exc_info.value.failed_constraint == "assumptions_too_many"


def test_create_recommendation_rejects_empty_assumption_entry(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.4: every assumption entry must be 1..2,000 chars."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="empty-entry finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                assumptions=["valid entry", ""],
            )
    assert exc_info.value.failed_constraint == "assumption_empty"


def test_create_recommendation_accepts_assumption_at_max_length(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.4: exactly 2,000 characters per entry is accepted."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="assumption-max-length finding",
        seed_party=True,
    )
    entry = "y" * 2_000
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            assumptions=[entry],
        )
    assert result.assumptions == (entry,)


def test_create_recommendation_rejects_assumption_over_max_length(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.4: 2,001 characters per entry is rejected."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="assumption-over-max finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                assumptions=["y" * 2_001],
            )
    assert exc_info.value.failed_constraint == "assumption_too_long"


# ---------------------------------------------------------------------------
# create_recommendation — Requirement 5.5 confidence enumeration.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["Low", "Medium", "High"])
def test_create_recommendation_accepts_valid_confidence_values(
    engine: Engine, knowledge_service: KnowledgeService, value: str
) -> None:
    """Requirement 5.5: confidence values from ``{Low, Medium, High}`` are accepted."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement=f"confidence-{value} finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            confidence=value,
        )
    assert result.confidence == value
    revision = _fetch_recommendation_revision(
        engine, recommendation_revision_id=result.recommendation_revision_id
    )
    assert revision is not None
    assert revision["confidence"] == value


def test_create_recommendation_rejects_invalid_confidence_value(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.5: confidence values outside the enumeration are rejected."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="bad-confidence finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                confidence="Critical",  # type: ignore[arg-type]
            )
    assert exc_info.value.failed_constraint == "confidence_invalid"


# ---------------------------------------------------------------------------
# create_recommendation — Requirement 5.7 authorization.
# ---------------------------------------------------------------------------


def test_create_recommendation_rejects_empty_authoring_party(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 5.7: unauthenticated callers (empty Party Identity)
    are rejected before any write happens."""
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(RecommendationValidationError) as exc_info:
            knowledge_service.create_recommendation(
                conn,
                authoring_party_id="",
                derived_from_findings=["00000000-0000-7000-8000-000000000abc"],
            )
    assert exc_info.value.failed_constraint == "authoring_party_id_missing"


def test_create_recommendation_permits_when_analyst_role_grants_modify(
    engine: Engine,
    knowledge_service_authorized: KnowledgeService,
    authorization_service: AuthorizationService,
) -> None:
    """Requirement 5.7 happy path: a Party with effective Analyst role
    for the applicable scope succeeds."""
    # Seed Parties and the role assignment.
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Analyst")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")
    _assign_analyst_role(authorization_service, engine)

    finding = _create_hypothesis_finding(
        engine,
        knowledge_service_authorized,
        statement="finding under analyst role",
        seed_party=False,
    )

    with engine.begin() as conn:
        result = knowledge_service_authorized.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            applicable_scope=_SCOPE,
            evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            correlation_id="corr-authorized",
        )

    recommendation_row = _fetch_recommendation(
        engine, recommendation_id=result.recommendation_id
    )
    assert recommendation_row is not None


def test_create_recommendation_denies_when_party_has_no_role(
    engine: Engine,
    knowledge_service_authorized: KnowledgeService,
    authorization_service: AuthorizationService,
) -> None:
    """Requirement 5.7: a caller without any role assignment is denied,
    and no Recommendation, Revision, or Relationship row is persisted.

    The :class:`AuthorizationService` returns ``deny`` with reason code
    ``"no-role-assignment"``, which the Knowledge_Service surfaces as
    :class:`RecommendationAuthorizationError`.
    """
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Subject")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")

    finding = _create_hypothesis_finding(
        engine,
        knowledge_service_authorized,
        statement="finding for unauthorized attempt",
        seed_party=False,
    )

    with pytest.raises(RecommendationAuthorizationError) as exc_info:
        with engine.begin() as conn:
            knowledge_service_authorized.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                applicable_scope=_SCOPE,
                evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                correlation_id="corr-unauthorized",
            )
    assert exc_info.value.reason_code == "no-role-assignment"
    assert exc_info.value.correlation_id == "corr-unauthorized"

    with engine.connect() as conn:
        # No Recommendations row was persisted (the transaction rolled
        # back when the exception propagated out of ``engine.begin()``).
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Recommendations")).scalar_one()
            == 0
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Recommendation_Revisions"))
            .scalar_one()
            == 0
        )
        # No ``Derived From`` Relationship rows.
        rels = conn.execute(
            text(
                "SELECT COUNT(*) FROM Relationships "
                "WHERE relationship_type = 'Derived From'"
            )
        ).scalar_one()
        assert rels == 0


def test_create_recommendation_denies_when_party_lacks_scope(
    engine: Engine,
    knowledge_service_authorized: KnowledgeService,
    authorization_service: AuthorizationService,
) -> None:
    """A Party with Analyst role for a *different* scope is denied with
    reason code ``"out-of-scope"`` and no Recommendation is persisted."""
    with engine.begin() as conn:
        _seed_party(conn, _PARTY_ID, "Analyst")
        _seed_party(conn, _ASSIGNING_PARTY_ID, "Resource Steward")
    _assign_analyst_role(
        authorization_service, engine, party_id=_PARTY_ID, scope="pilot/team-b"
    )

    finding = _create_hypothesis_finding(
        engine,
        knowledge_service_authorized,
        statement="finding behind wrong scope",
        seed_party=False,
    )

    with pytest.raises(RecommendationAuthorizationError) as exc_info:
        with engine.begin() as conn:
            knowledge_service_authorized.create_recommendation(
                conn,
                authoring_party_id=_PARTY_ID,
                derived_from_findings=[finding.finding_id],
                applicable_scope=_SCOPE,
                evaluation_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )
    assert exc_info.value.reason_code == "out-of-scope"

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT COUNT(*) FROM Recommendations")).scalar_one()
            == 0
        )


# ---------------------------------------------------------------------------
# create_recommendation — Requirement 13.1 audit row.
# ---------------------------------------------------------------------------


def test_create_recommendation_appends_consequential_audit_row(
    engine: Engine, knowledge_service: KnowledgeService
) -> None:
    """Requirement 13.1: every consequential write appends an audit
    row with the expected actor, action, target, and correlation id."""
    finding = _create_hypothesis_finding(
        engine,
        knowledge_service,
        statement="audit-row finding",
        seed_party=True,
    )
    with engine.begin() as conn:
        result = knowledge_service.create_recommendation(
            conn,
            authoring_party_id=_PARTY_ID,
            derived_from_findings=[finding.finding_id],
            correlation_id="corr-audit",
        )

    audit_rows = _fetch_audit_rows(engine)
    # Filter to the create.recommendation row — the helper finding
    # creation already appended its own ``create.finding`` row.
    create_rec_rows = [
        row for row in audit_rows if row["action_type"] == "create.recommendation"
    ]
    assert len(create_rec_rows) == 1
    audit = create_rec_rows[0]
    assert audit["actor_party_id"] == _PARTY_ID
    assert audit["outcome"] == "consequential"
    assert audit["target_id"] == result.recommendation_id
    assert audit["target_revision_id"] == result.recommendation_revision_id
    assert audit["correlation_id"] == "corr-audit"
    assert audit["recorded_at"] == _TS_FIXED
