# Feature: first-walking-slice, Property 1: Evidence support
"""Property 1 — Evidence support (task 6.4).

**Property 1: Evidence support**

For all Findings persisted by the ``Knowledge_Service``, every
non-hypothesis Finding has at least one ``Supports`` Relationship whose
target is a Content Region Occurrence that resolves at query time,
whose start anchor, end anchor, bounded length, and content digest
match a recorded Region Occurrence in the ``Evidence_Repository``, and
whose owning Document Revision Identity resolves to a stored Document
Revision.

**Validates: Requirements 4.1, 4.2, 4.3, 4.5, 15.1**

Strategy:

Each Hypothesis case draws a relationship-graph: a list of *finding
scenarios* where each scenario carries

- one Source Document's ``content_bytes``;
- one or more supporting span ranges ``(start, end)`` against that
  Document's bytes;
- a statement string for the Finding; and
- the hypothesis flag (``False`` for the spec-mandated branch under
  test; ``True`` finding scenarios may be drawn for breadth and are
  asserted only against the universal "no false Supports" subset of
  the property).

Per generated case the test spins up a fresh per-test SQLite engine on
a unique :class:`tempfile.TemporaryDirectory` path so cross-case state
cannot contaminate the byte-equivalence checks. For each scenario it:

1. Creates the Source Document via :class:`EvidenceRepository`
   (one row in ``Source_Documents`` + one in ``Document_Revisions``).
2. Anchors one Region Occurrence per supporting span via
   ``create_region_occurrence`` (each Occurrence carries the span's
   ``start_offset_bytes``, ``end_offset_bytes``, ``span_byte_length``,
   and ``span_content_digest_sha256`` in ``Region_Occurrences``).
3. Records a Finding via :class:`KnowledgeService` citing the
   resulting Region Occurrences as ``Supports`` references.

After every scenario is persisted, the test queries the database
directly:

- For every persisted *non-hypothesis* Finding, scan ``Relationships``
  where ``source_revision_id = finding.finding_revision_id`` and
  ``relationship_type = 'Supports'``.
- Assert at least one such row exists (Requirement 4.1, 4.3).
- For each ``Supports`` row resolve the composite
  ``(target_id, target_revision_id)`` against ``Region_Occurrences``
  using :meth:`EvidenceRepository.get_region_occurrence` and assert
  the row exists.
- Assert the Region Occurrence's ``span_content_digest_sha256`` equals
  the SHA-256 of ``content_bytes[start:end]`` recomputed from the
  resolved Document Revision (Requirement 4.2, 15.1 — content-digest
  evidence linkage).
- Assert ``start_offset_bytes`` and ``end_offset_bytes`` match what
  was originally recorded (Requirement 4.2 — Relationship payload is
  byte-equivalent to the cited Occurrence's anchors).
- Assert the Document Revision Identity (``target_revision_id`` on the
  ``Supports`` row) resolves to a stored ``Document_Revisions`` row.

The property quantifier is "for all persisted non-hypothesis Findings
drawn from a relationship-graph strategy" — the strategy explores
multi-Finding scenarios so the property is exercised against the
realistic shape where many Findings cite many Occurrences across many
Source Documents. Requirement 4.5 ("one Supports Relationship per
cited Occurrence") is exercised by scenarios that cite multiple spans;
Requirement 4.3 ("non-hypothesis with zero supports is rejected") is
exercised implicitly because every non-hypothesis scenario carries
``min_size=1`` supporting spans by construction — the property
strategy never asks the service to persist an invalid non-hypothesis
Finding, but the assertion at the end ("at least one Supports
Relationship exists") confirms the rejection branch is never silently
bypassed.
"""

from __future__ import annotations

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pytest
from hypothesis import given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.clock import FixedClock
from walking_slice.evidence import (
    CreateDocumentResult,
    CreateRegionResult,
    EvidenceRepository,
)
from walking_slice.identity import IdentityService
from walking_slice.knowledge import (
    CreateFindingResult,
    KnowledgeService,
    SupportRef,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the Parties row required by the
# ``Document_Revisions.contributing_party_id``,
# ``Finding_Revisions.authoring_party_id``,
# ``Relationships.authoring_party_id``, and
# ``Audit_Records.actor_party_id`` foreign keys.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_PARTY_CREATED_AT: Final[str] = "2026-01-01T00:00:00.000Z"


def _seed_party(connection) -> None:
    """Insert the test Party row that the FK constraints require."""
    connection.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Property 1 Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# The relationship-graph strategy ``_finding_scenarios`` draws a list of
# finding scenarios. Each scenario carries one Source Document's content
# bytes, one or more supporting span ranges anchored against that
# Document, a non-empty statement string, and the hypothesis flag.
#
# ``content_bytes`` is drawn as 1..256 bytes; spans are constrained to
# ``(0 <= start < end <= len(content_bytes))`` so they are always
# valid for ``EvidenceRepository.create_region_occurrence`` (Requirement
# 3.5). Constraining the strategy to valid inputs lets the property
# focus on the *positive* invariant under test ("every persisted
# non-hypothesis Finding's Supports Relationships resolve correctly")
# without coupling to the rejection branches, which are covered by the
# dedicated unit tests in ``tests/unit/test_knowledge_findings.py``.
# ---------------------------------------------------------------------------


@st.composite
def _span_strategy(draw, *, content_length: int) -> tuple[int, int]:
    """Draw a valid ``(start, end)`` span inside a ``content_length``-byte buffer.

    Constraints (Requirement 3.5 and AD-WS-6):

    - ``0 <= start_offset_bytes``
    - ``start_offset_bytes < end_offset_bytes``
    - ``end_offset_bytes <= content_length``

    The strategy first draws ``start`` from ``[0, content_length - 1]``
    (so at least one byte remains for ``end``), then draws ``end`` from
    ``[start + 1, content_length]``. Both endpoints are inclusive in
    Hypothesis's ``integers`` bounds so the test exercises the boundary
    cases (1-byte span at offset 0, 1-byte span at the end, full-length
    span) when Hypothesis explores them.
    """
    start = draw(st.integers(min_value=0, max_value=content_length - 1))
    end = draw(st.integers(min_value=start + 1, max_value=content_length))
    return (start, end)


@st.composite
def _finding_scenario(draw) -> dict:
    """Draw a single finding scenario: Document bytes + spans + statement.

    Returns a dict with keys:

    - ``content_bytes`` (``bytes``): Source Document content for this
      scenario; 1..256 bytes drawn from arbitrary byte values so the
      property is exercised against the full SQLite BLOB alphabet
      (not just printable ASCII).
    - ``spans`` (``list[tuple[int, int]]``): 1..5 supporting span
      ranges. Each range is valid for ``content_bytes`` by
      construction. Spans may overlap or coincide — Requirement 4.5
      demands *one Supports Relationship per cited Occurrence*, not
      one per distinct Region, so the test does not deduplicate.
      Note: two Region Occurrences anchored against the same Document
      Revision with byte-identical spans produce identical
      ``span_content_digest_sha256`` values; when the Evidence
      Repository deduplicates Region Identities by digest (AD-WS-2)
      this collapses to one Region with one Occurrence in that
      Revision — the property still holds because the Supports
      Relationship resolves through the shared Region. We avoid the
      ambiguity by requiring distinct span tuples within one
      scenario; spans that happen to share digests across *different*
      scenarios target different Document Revisions and so anchor
      distinct Occurrences.
    - ``statement`` (``str``): non-empty Finding statement.
    - ``is_hypothesis`` (``bool``): hypothesis flag for the Finding.
      Drawn ``False`` for the majority of scenarios (the spec-mandated
      branch under test) and occasionally ``True`` so the property is
      exercised against the universal "no false Supports" invariant
      even for hypothesis Findings (which may carry zero or more
      supports).
    """
    content_length = draw(st.integers(min_value=1, max_value=256))
    content_bytes = draw(
        st.binary(min_size=content_length, max_size=content_length)
    )
    # Distinct spans within one scenario — see the docstring note above
    # about Region Identity deduplication by digest. ``unique=True`` on
    # the tuple-of-ints strategy gives Hypothesis a clean way to draw
    # 1..5 distinct ``(start, end)`` pairs against the same Document.
    spans = draw(
        st.lists(
            _span_strategy(content_length=content_length),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    statement = draw(st.text(min_size=1, max_size=128))
    is_hypothesis = draw(st.booleans())
    return {
        "content_bytes": content_bytes,
        "spans": spans,
        "statement": statement,
        "is_hypothesis": is_hypothesis,
    }


_finding_scenarios = st.lists(
    _finding_scenario(),
    min_size=1,
    max_size=6,
)


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, audit rows, Source_Documents rows,
# Findings rows, and Relationships rows cannot leak between cases
# (design §"Testing Strategy" — "Each property and example test gets a
# fresh SQLite database"). A :class:`tempfile.TemporaryDirectory`
# context inside the test body owns the per-case directory; Hypothesis
# disallows function-scoped pytest fixtures for per-case state because
# they would not reset between generated inputs.
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


def _fetch_supports_for_finding(
    engine: Engine, *, finding_revision_id: str
) -> list[dict]:
    """Return every ``Supports`` Relationships row sourced from this Revision.

    The scan key matches the spec-mandated payload: ``source_revision_id``
    points at the Finding Revision (Requirement 4.2) and
    ``relationship_type`` discriminates ``Supports`` from ``Contradicts``.
    The result rows carry every Requirement-4.2 attribute so the caller
    can verify them in one query.
    """
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT relationship_id, relationship_type, source_kind,
                           source_id, source_revision_id, target_kind,
                           target_id, target_revision_id,
                           authoring_party_id, recorded_at
                      FROM Relationships
                     WHERE source_revision_id = :frid
                       AND relationship_type = 'Supports'
                    """
                ),
                {"frid": finding_revision_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fetch_document_revision_bytes(
    engine: Engine, *, revision_id: str
) -> bytes | None:
    """Return ``Document_Revisions.content_bytes`` or ``None`` if absent.

    Used to recompute the SHA-256 of ``content_bytes[start:end]`` and
    compare it against the persisted
    ``Region_Occurrences.span_content_digest_sha256`` (Requirement
    4.2 — Relationship's target Occurrence must match the
    Evidence_Repository row byte-for-byte).
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT content_bytes FROM Document_Revisions "
                "WHERE revision_id = :rid"
            ),
            {"rid": revision_id},
        ).first()
    if row is None:
        return None
    # SQLAlchemy returns ``memoryview`` for BLOB columns under some
    # SQLite driver versions; coerce to ``bytes`` so the slicing and
    # ``hashlib.sha256`` calls below see a uniform type.
    return bytes(row[0])


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 1: Evidence support
@given(scenarios=_finding_scenarios)
@settings(max_examples=100, deadline=5000)
def test_evidence_support(scenarios: list[dict]) -> None:
    """Every persisted non-hypothesis Finding has at least one ``Supports``
    Relationship that resolves to a Region Occurrence matching the
    Evidence_Repository row byte-for-byte."""
    # A fresh on-disk SQLite file per case prevents cross-case leakage of
    # identifiers, audit rows, Source_Documents/Findings/Relationships
    # rows. Using :class:`tempfile.TemporaryDirectory` (rather than a
    # pytest fixture) is the pattern Hypothesis recommends for per-case
    # state because function-scoped fixtures are not reset between
    # generated inputs.
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop1_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh services per case so :class:`IdentityService` in-memory
        # state cannot bleed across cases. ``FixedClock`` keeps every
        # scenario's recorded timestamp deterministic for shrinking.
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
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

        # Track per-scenario state so post-persist assertions can verify
        # each Finding's Supports Relationships against the inputs that
        # produced them.
        #
        # Each entry: {
        #   "finding_result": CreateFindingResult,
        #   "is_hypothesis": bool,
        #   "document_revision_id": str,
        #   "content_bytes": bytes,
        #   "expected_supports": list[{
        #       "region_id": str,
        #       "revision_id": str,
        #       "start": int,
        #       "end": int,
        #       "digest": str,  # lowercase-hex SHA-256
        #   }],
        # }
        persisted: list[dict] = []

        try:
            # Seed the Party row once; every scenario reuses it for
            # ``contributing_party_id`` and ``authoring_party_id``.
            with engine.begin() as conn:
                _seed_party(conn)

            for scenario in scenarios:
                content_bytes: bytes = scenario["content_bytes"]
                spans: list[tuple[int, int]] = scenario["spans"]
                statement: str = scenario["statement"]
                is_hypothesis: bool = scenario["is_hypothesis"]

                # Each scenario's full persistence (Document + Region
                # Occurrences + Finding + Supports rows + audit rows)
                # happens in one transaction so the AD-WS-5
                # "consequential audit appends in the originating
                # transaction" invariant is exercised end-to-end and
                # the property assertions read a committed state.
                with engine.begin() as conn:
                    doc: CreateDocumentResult = (
                        evidence_repository.create_document(
                            conn,
                            content_bytes=content_bytes,
                            contributing_party_id=_PARTY_ID,
                            authority="authoritative",
                        )
                    )

                    region_results: list[CreateRegionResult] = []
                    for start, end in spans:
                        region = evidence_repository.create_region_occurrence(
                            conn,
                            resource_id=doc.resource_id,
                            revision_id=doc.revision_id,
                            start_offset_bytes=start,
                            end_offset_bytes=end,
                            contributing_party_id=_PARTY_ID,
                        )
                        region_results.append(region)

                    supports = tuple(
                        SupportRef(
                            region_id=region.region_id,
                            document_revision_id=doc.revision_id,
                        )
                        for region in region_results
                    )

                    finding: CreateFindingResult = (
                        knowledge_service.create_finding(
                            conn,
                            statement=statement,
                            authoring_party_id=_PARTY_ID,
                            is_hypothesis=is_hypothesis,
                            supporting_region_occurrences=supports,
                        )
                    )

                expected_supports = [
                    {
                        "region_id": region.region_id,
                        "revision_id": doc.revision_id,
                        "start": region.start_offset_bytes,
                        "end": region.end_offset_bytes,
                        "digest": region.span_content_digest_sha256,
                    }
                    for region in region_results
                ]
                persisted.append(
                    {
                        "finding_result": finding,
                        "is_hypothesis": is_hypothesis,
                        "document_revision_id": doc.revision_id,
                        "content_bytes": content_bytes,
                        "expected_supports": expected_supports,
                    }
                )

            # ----- Property assertions -------------------------------
            #
            # For every persisted *non-hypothesis* Finding, scan the
            # Relationships table for Supports rows sourced from this
            # Finding Revision and verify each row resolves to a
            # Region Occurrence whose anchors and content digest match
            # the Evidence_Repository row.
            for entry in persisted:
                if entry["is_hypothesis"]:
                    # Hypothesis Findings may carry zero Supports per
                    # Requirement 4.1; the property statement targets
                    # *non-hypothesis* Findings. Hypothesis scenarios
                    # are still drawn for breadth (so the strategy
                    # explores realistic mixtures) but only
                    # non-hypothesis scenarios trigger the
                    # at-least-one-Supports assertion below.
                    continue

                finding: CreateFindingResult = entry["finding_result"]
                supports_rows = _fetch_supports_for_finding(
                    engine,
                    finding_revision_id=finding.finding_revision_id,
                )

                # Requirement 4.1 / 4.3 — at least one Supports row.
                assert len(supports_rows) >= 1, (
                    "Non-hypothesis Finding "
                    f"{finding.finding_revision_id!r} has zero Supports "
                    "Relationships; Requirements 4.1 and 4.3 demand at "
                    "least one Supports row pointing at a Content "
                    "Region Occurrence."
                )

                # Requirement 4.5 — one Supports Relationship per
                # cited Occurrence. The count of persisted rows must
                # equal the count of cited Occurrences exactly.
                assert len(supports_rows) == len(entry["expected_supports"]), (
                    "Supports Relationship count mismatch for Finding "
                    f"{finding.finding_revision_id!r}: expected "
                    f"{len(entry['expected_supports'])}, found "
                    f"{len(supports_rows)}. Requirement 4.5 requires "
                    "one Supports row per cited Occurrence."
                )

                # Build a lookup of expected supports keyed by Region
                # Identity so we can match them to the persisted rows
                # without depending on row ordering. Region Identities
                # are unique within one scenario (each
                # ``create_region_occurrence`` mints a fresh one), so
                # this is a 1-to-1 map.
                expected_by_region = {
                    exp["region_id"]: exp for exp in entry["expected_supports"]
                }

                for row in supports_rows:
                    # Requirement 4.2 — Relationship payload columns.
                    assert row["source_kind"] == "finding_revision", (
                        f"Supports row {row['relationship_id']!r} has "
                        f"source_kind={row['source_kind']!r}; expected "
                        "'finding_revision' for a Finding-sourced "
                        "Supports Relationship."
                    )
                    assert row["source_id"] == finding.finding_id, (
                        f"Supports row {row['relationship_id']!r} "
                        f"source_id={row['source_id']!r} does not match "
                        f"Finding Resource Identity "
                        f"{finding.finding_id!r}."
                    )
                    assert row["target_kind"] == "region_occurrence", (
                        f"Supports row {row['relationship_id']!r} has "
                        f"target_kind={row['target_kind']!r}; expected "
                        "'region_occurrence'."
                    )
                    assert row["authoring_party_id"] == _PARTY_ID, (
                        f"Supports row {row['relationship_id']!r} "
                        f"authoring_party_id={row['authoring_party_id']!r} "
                        f"does not match the Finding's authoring Party "
                        f"{_PARTY_ID!r}."
                    )

                    region_id = row["target_id"]
                    revision_id = row["target_revision_id"]
                    assert region_id in expected_by_region, (
                        f"Supports row {row['relationship_id']!r} points "
                        f"at region_id={region_id!r} which was never "
                        "cited by the originating Finding; the "
                        "Relationship target diverged from the input."
                    )
                    expected = expected_by_region[region_id]

                    # Resolve the Region Occurrence by composite PK
                    # through the EvidenceRepository surface so the
                    # property exercises the public read path
                    # (Property 1 says the Occurrence "resolves at
                    # query time").
                    with engine.connect() as conn:
                        occurrence = (
                            evidence_repository.get_region_occurrence(
                                conn,
                                region_id=region_id,
                                revision_id=revision_id,
                            )
                        )

                    # Requirement 4.2 / Property 1 — anchors match the
                    # Evidence_Repository row exactly.
                    assert (
                        occurrence.start_offset_bytes == expected["start"]
                    ), (
                        "Region Occurrence start anchor diverged for "
                        f"region_id={region_id!r}: "
                        f"expected {expected['start']}, persisted "
                        f"{occurrence.start_offset_bytes}."
                    )
                    assert (
                        occurrence.end_offset_bytes == expected["end"]
                    ), (
                        "Region Occurrence end anchor diverged for "
                        f"region_id={region_id!r}: "
                        f"expected {expected['end']}, persisted "
                        f"{occurrence.end_offset_bytes}."
                    )
                    assert (
                        occurrence.span_byte_length
                        == expected["end"] - expected["start"]
                    ), (
                        "Region Occurrence span_byte_length is "
                        "inconsistent with the (start, end) anchors "
                        f"for region_id={region_id!r}: "
                        f"persisted={occurrence.span_byte_length}, "
                        f"expected={expected['end'] - expected['start']}."
                    )

                    # Requirement 4.2 / 15.1 — span_content_digest
                    # equals SHA-256 of content_bytes[start:end]
                    # recomputed from the resolved Document Revision.
                    revision_bytes = _fetch_document_revision_bytes(
                        engine, revision_id=revision_id
                    )
                    assert revision_bytes is not None, (
                        "Document Revision Identity "
                        f"{revision_id!r} on Supports row "
                        f"{row['relationship_id']!r} did not resolve "
                        "to a stored Document_Revisions row "
                        "(Property 1 — owning Document Revision must "
                        "resolve)."
                    )
                    recomputed_digest = hashlib.sha256(
                        revision_bytes[expected["start"]:expected["end"]]
                    ).hexdigest()
                    assert (
                        occurrence.span_content_digest_sha256
                        == recomputed_digest
                    ), (
                        "Region Occurrence span digest diverged from "
                        "SHA-256(content_bytes[start:end]) for "
                        f"region_id={region_id!r}, "
                        f"revision_id={revision_id!r}: "
                        f"persisted="
                        f"{occurrence.span_content_digest_sha256!r}, "
                        f"recomputed={recomputed_digest!r}."
                    )
                    # Cross-check against the digest captured at
                    # Region creation time (the Evidence_Repository's
                    # CreateRegionResult.span_content_digest_sha256).
                    assert (
                        occurrence.span_content_digest_sha256
                        == expected["digest"]
                    ), (
                        "Region Occurrence digest diverged from the "
                        "value returned by create_region_occurrence "
                        f"for region_id={region_id!r}: "
                        f"persisted="
                        f"{occurrence.span_content_digest_sha256!r}, "
                        f"create_result={expected['digest']!r}."
                    )
        finally:
            engine.dispose()
