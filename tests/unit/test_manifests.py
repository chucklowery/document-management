"""Unit tests for :mod:`walking_slice.manifests` (task 9.1).

These tests pin the contract for the stand-alone
:class:`ProvenanceManifestWriter` introduced by task 9.1 — design
§"Provenance_Manifests and Omission_Entries", Requirement 10.1
(material sources recorded on every Finding, Recommendation, Decision,
or Trail Revision manifest), Requirement 10.2 (explicit Omission Entry
shape and length bounds), Requirement 10.3 (incomplete manifests when
an unresolved Omission Entry has a non-intentional category), and
Requirement 10.6 (default 24-hour Source Freshness Window).

Test cases pin five named behaviours called out in the task
description:

1. Valid manifest with Included Sources and no omissions →
   ``is_complete = 1``.
2. Manifest with an *intentional* omission → ``is_complete = 1``.
3. Manifest with each non-intentional category (``unavailable``,
   ``restricted``, ``stale``, ``unresolved``) → ``is_complete = 0``.
4. Stale Included Source rejected unless paired with a ``stale``
   Omission Entry.
5. Source Freshness Window of 24 hours — Included Sources older than
   24 hours before the manifest's ``recorded_at`` and without a
   ``stale`` Omission are rejected.

The tests use the per-test SQLite ``engine`` fixture from
:mod:`tests.conftest` so every persisted row is observable through a
read query against the same database the writer just used.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.manifests import (
    DEFAULT_FRESHNESS_WINDOW_SECONDS,
    IncludedSource,
    ManifestValidationError,
    ManifestWriteResult,
    OmissionEntry,
    ProvenanceManifestWriter,
    StalenessError,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Constants and seeding helpers.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_SUBJECT_ID = "00000000-0000-7000-8000-0000000000aa"
_SUBJECT_REVISION_ID = "00000000-0000-7000-8000-0000000000ab"
_SOURCE_ID = "00000000-0000-7000-8000-0000000000b1"
_SOURCE_REVISION_ID = "00000000-0000-7000-8000-0000000000b2"
_MANIFEST_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
_MANIFEST_TS_ISO = "2026-01-01T12:00:00.000Z"
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
_CANONICAL_UUID7 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _seed_party(conn, *, party_id: str = _PARTY_ID) -> None:
    """Insert a Party row so FK constraints on the manifest pass."""
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Tester', :ts)
            """
        ),
        {"pid": party_id, "ts": _MANIFEST_TS_ISO},
    )


def _fresh_included_source(
    *,
    kind: str = "recommendation_revision",
    resource_id: str = _SOURCE_ID,
    revision_id: str | None = _SOURCE_REVISION_ID,
    age_hours: float = 1.0,
) -> IncludedSource:
    """Build an :class:`IncludedSource` whose ``recorded_at`` is *fresh*.

    ``age_hours`` controls how far before the fixed manifest time the
    source's ``recorded_at`` falls. The default 1 hour sits well inside
    the 24-hour freshness window so happy-path tests are not subject to
    boundary effects.
    """
    return IncludedSource(
        kind=kind,
        resource_id=resource_id,
        revision_id=revision_id,
        recorded_at=_MANIFEST_TS - timedelta(hours=age_hours),
    )


# ---------------------------------------------------------------------------
# Row readers.
# ---------------------------------------------------------------------------


def _fetch_manifest(engine: Engine, *, manifest_id: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT manifest_id, subject_kind, subject_id,
                           subject_revision_id, authoring_party_id,
                           recorded_at, included_sources_json, is_complete
                    FROM Provenance_Manifests
                    WHERE manifest_id = :mid
                    """
                ),
                {"mid": manifest_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row else None


def _fetch_omissions(engine: Engine, *, manifest_id: str) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT omission_entry_id, manifest_id, excluded_source_id,
                           excluded_source_revision_id, category, rationale,
                           authoring_party_id, recorded_at, resolved_at
                    FROM Omission_Entries
                    WHERE manifest_id = :mid
                    ORDER BY omission_entry_id
                    """
                ),
                {"mid": manifest_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture
def writer_clock() -> FixedClock:
    """Pin the manifest's ``recorded_at`` so freshness assertions are exact."""
    return FixedClock(_MANIFEST_TS)


@pytest.fixture
def manifest_writer(
    writer_clock: FixedClock, identity_service: IdentityService
) -> ProvenanceManifestWriter:
    """Wire the writer with a pinned :class:`FixedClock`.

    The conftest-provided ``identity_service`` is reused so manifest
    identifiers come from the same UUIDv7 source the rest of the slice
    uses; the alternate clock here overrides the conftest fixture for
    this module so freshness-window arithmetic is deterministic.
    """
    return ProvenanceManifestWriter(
        clock=writer_clock,
        identity_service=identity_service,
    )


# ---------------------------------------------------------------------------
# Happy path — included sources, no omissions.
# ---------------------------------------------------------------------------


def test_write_manifest_with_included_sources_and_no_omissions_is_complete(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.1: a manifest with material sources and zero
    omissions persists with ``is_complete = 1``."""
    source = _fresh_included_source()
    with engine.begin() as conn:
        _seed_party(conn)
        result = manifest_writer.write_manifest(
            conn,
            subject_kind="decision",
            subject_id=_SUBJECT_ID,
            subject_revision_id=None,
            authoring_party_id=_PARTY_ID,
            included_sources=[source],
            omissions=(),
        )

    assert isinstance(result, ManifestWriteResult)
    assert _CANONICAL_UUID7.match(result.manifest_id)
    assert result.is_complete is True
    assert result.omission_entry_ids == ()
    assert result.recorded_at == _MANIFEST_TS_ISO

    manifest = _fetch_manifest(engine, manifest_id=result.manifest_id)
    assert manifest is not None
    assert manifest["subject_kind"] == "decision"
    assert manifest["subject_id"] == _SUBJECT_ID
    assert manifest["subject_revision_id"] is None
    assert manifest["authoring_party_id"] == _PARTY_ID
    assert manifest["recorded_at"] == _MANIFEST_TS_ISO
    assert manifest["is_complete"] == 1

    sources = json.loads(manifest["included_sources_json"])
    assert len(sources) == 1
    assert sources[0]["kind"] == "recommendation_revision"
    assert sources[0]["resource_id"] == _SOURCE_ID
    assert sources[0]["revision_id"] == _SOURCE_REVISION_ID
    assert _ISO_8601_MS_PATTERN.match(sources[0]["recorded_at"])

    assert _fetch_omissions(engine, manifest_id=result.manifest_id) == []


# ---------------------------------------------------------------------------
# Intentional omission — manifest is still complete.
# ---------------------------------------------------------------------------


def test_write_manifest_with_intentional_omission_is_complete(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.3: an *intentional* omission does not mark the
    synthesis incomplete; ``is_complete`` stays 1 and the Omission
    Entry is persisted with the recorded rationale and category."""
    intentional = OmissionEntry(
        excluded_source_id="00000000-0000-7000-8000-0000000000c1",
        excluded_source_revision_id=None,
        category="intentional",
        rationale="Out of scope for this synthesis (covered by Recommendation R).",
    )
    with engine.begin() as conn:
        _seed_party(conn)
        result = manifest_writer.write_manifest(
            conn,
            subject_kind="finding_revision",
            subject_id=_SUBJECT_ID,
            subject_revision_id=_SUBJECT_REVISION_ID,
            authoring_party_id=_PARTY_ID,
            included_sources=[_fresh_included_source()],
            omissions=[intentional],
        )

    assert result.is_complete is True
    assert len(result.omission_entry_ids) == 1

    manifest = _fetch_manifest(engine, manifest_id=result.manifest_id)
    assert manifest is not None
    assert manifest["subject_kind"] == "finding_revision"
    assert manifest["subject_revision_id"] == _SUBJECT_REVISION_ID
    assert manifest["is_complete"] == 1

    omissions = _fetch_omissions(engine, manifest_id=result.manifest_id)
    assert len(omissions) == 1
    persisted = omissions[0]
    assert persisted["omission_entry_id"] == result.omission_entry_ids[0]
    assert persisted["category"] == "intentional"
    assert persisted["rationale"] == intentional.rationale
    assert persisted["authoring_party_id"] == _PARTY_ID
    assert persisted["recorded_at"] == _MANIFEST_TS_ISO
    assert persisted["resolved_at"] is None


# ---------------------------------------------------------------------------
# Each non-intentional category drives is_complete = 0.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    ["unavailable", "restricted", "stale", "unresolved"],
)
def test_write_manifest_with_non_intentional_omission_is_incomplete(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
    category: str,
) -> None:
    """Requirement 10.3 / design §"Persistence Invariants Summary"
    item 9: any unresolved Omission Entry with a non-intentional
    category causes ``is_complete = 0``. The test exercises all four
    permitted non-intentional categories — including ``'stale'``,
    which is paired with a freshly-recorded Included Source so the
    Source Freshness Window check stays passive."""
    omission = OmissionEntry(
        excluded_source_id="00000000-0000-7000-8000-0000000000d1",
        excluded_source_revision_id="00000000-0000-7000-8000-0000000000d2",
        category=category,  # type: ignore[arg-type]
        rationale=f"Source declared {category} for this synthesis.",
    )
    with engine.begin() as conn:
        _seed_party(conn)
        result = manifest_writer.write_manifest(
            conn,
            subject_kind="trail_revision",
            subject_id=_SUBJECT_ID,
            subject_revision_id=_SUBJECT_REVISION_ID,
            authoring_party_id=_PARTY_ID,
            included_sources=[_fresh_included_source()],
            omissions=[omission],
        )

    assert result.is_complete is False

    manifest = _fetch_manifest(engine, manifest_id=result.manifest_id)
    assert manifest is not None
    assert manifest["is_complete"] == 0

    persisted = _fetch_omissions(engine, manifest_id=result.manifest_id)
    assert len(persisted) == 1
    assert persisted[0]["category"] == category


# ---------------------------------------------------------------------------
# Stale source rejected unless paired with a stale Omission Entry.
# ---------------------------------------------------------------------------


def test_stale_included_source_without_stale_omission_is_rejected(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.6: an Included Source older than the freshness
    window (default 24 hours) and *not* paired with a stale Omission
    Entry causes :class:`StalenessError`; no manifest is persisted."""
    stale_source = IncludedSource(
        kind="recommendation_revision",
        resource_id=_SOURCE_ID,
        revision_id=_SOURCE_REVISION_ID,
        recorded_at=_MANIFEST_TS - timedelta(hours=25),  # outside 24-hour window
    )
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(StalenessError) as excinfo:
            manifest_writer.write_manifest(
                conn,
                subject_kind="decision",
                subject_id=_SUBJECT_ID,
                subject_revision_id=None,
                authoring_party_id=_PARTY_ID,
                included_sources=[stale_source],
                omissions=(),
            )

    assert excinfo.value.excluded_source_id == _SOURCE_ID
    assert excinfo.value.freshness_window_seconds == (
        DEFAULT_FRESHNESS_WINDOW_SECONDS
    )
    assert excinfo.value.failed_constraint == "source_stale"

    # The exception aborted the transaction before any manifest was
    # inserted; the table is empty.
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Provenance_Manifests")
        ).scalar_one()
    assert count == 0


def test_stale_included_source_paired_with_stale_omission_persists(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.6 / task description: a stale Included Source may
    be carried on the manifest when the caller also records a
    ``'stale'`` Omission Entry that names the same source. The
    manifest persists with ``is_complete = 0`` because the
    acknowledgment itself is a non-intentional omission
    (Requirement 10.3)."""
    stale_source = IncludedSource(
        kind="recommendation_revision",
        resource_id=_SOURCE_ID,
        revision_id=_SOURCE_REVISION_ID,
        recorded_at=_MANIFEST_TS - timedelta(hours=48),
    )
    ack = OmissionEntry(
        excluded_source_id=_SOURCE_ID,
        excluded_source_revision_id=_SOURCE_REVISION_ID,
        category="stale",
        rationale="Source has not been refreshed in 48 hours; carried with caveat.",
    )
    with engine.begin() as conn:
        _seed_party(conn)
        result = manifest_writer.write_manifest(
            conn,
            subject_kind="decision",
            subject_id=_SUBJECT_ID,
            subject_revision_id=None,
            authoring_party_id=_PARTY_ID,
            included_sources=[stale_source],
            omissions=[ack],
        )

    assert result.is_complete is False
    manifest = _fetch_manifest(engine, manifest_id=result.manifest_id)
    assert manifest is not None
    assert manifest["is_complete"] == 0

    omissions = _fetch_omissions(engine, manifest_id=result.manifest_id)
    assert len(omissions) == 1
    assert omissions[0]["category"] == "stale"
    assert omissions[0]["excluded_source_id"] == _SOURCE_ID


# ---------------------------------------------------------------------------
# 24-hour Source Freshness Window boundary.
# ---------------------------------------------------------------------------


def test_source_recorded_exactly_at_24_hours_is_accepted(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.6: the freshness window is inclusive at 24
    hours; a source recorded exactly 24 hours before the manifest
    persists without a stale Omission Entry."""
    boundary_source = IncludedSource(
        kind="finding_revision",
        resource_id=_SOURCE_ID,
        revision_id=_SOURCE_REVISION_ID,
        recorded_at=_MANIFEST_TS - timedelta(hours=24),
    )
    with engine.begin() as conn:
        _seed_party(conn)
        result = manifest_writer.write_manifest(
            conn,
            subject_kind="recommendation_revision",
            subject_id=_SUBJECT_ID,
            subject_revision_id=_SUBJECT_REVISION_ID,
            authoring_party_id=_PARTY_ID,
            included_sources=[boundary_source],
            omissions=(),
        )

    assert result.is_complete is True
    manifest = _fetch_manifest(engine, manifest_id=result.manifest_id)
    assert manifest is not None
    assert manifest["is_complete"] == 1


def test_source_recorded_just_past_24_hours_is_rejected(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.6: a source recorded one millisecond past the
    24-hour window is stale and rejected."""
    just_past = IncludedSource(
        kind="finding_revision",
        resource_id=_SOURCE_ID,
        revision_id=_SOURCE_REVISION_ID,
        recorded_at=_MANIFEST_TS - timedelta(hours=24, milliseconds=1),
    )
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(StalenessError):
            manifest_writer.write_manifest(
                conn,
                subject_kind="recommendation_revision",
                subject_id=_SUBJECT_ID,
                subject_revision_id=_SUBJECT_REVISION_ID,
                authoring_party_id=_PARTY_ID,
                included_sources=[just_past],
                omissions=(),
            )


# ---------------------------------------------------------------------------
# Envelope and entry validation.
# ---------------------------------------------------------------------------


def test_invalid_subject_kind_is_rejected(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.1: ``subject_kind`` must be one of the four
    permitted syntheses."""
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(ManifestValidationError) as excinfo:
            manifest_writer.write_manifest(
                conn,
                subject_kind="document_revision",  # type: ignore[arg-type]
                subject_id=_SUBJECT_ID,
                subject_revision_id=None,
                authoring_party_id=_PARTY_ID,
                included_sources=[_fresh_included_source()],
            )
    assert excinfo.value.failed_constraint == "subject_kind_invalid"


def test_omission_with_invalid_category_is_rejected(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.3: only the five named categories are accepted."""
    bad = OmissionEntry(
        excluded_source_id=_SOURCE_ID,
        excluded_source_revision_id=None,
        category="forgotten",  # type: ignore[arg-type]
        rationale="not a real category",
    )
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(ManifestValidationError) as excinfo:
            manifest_writer.write_manifest(
                conn,
                subject_kind="decision",
                subject_id=_SUBJECT_ID,
                subject_revision_id=None,
                authoring_party_id=_PARTY_ID,
                included_sources=[_fresh_included_source()],
                omissions=[bad],
            )
    assert excinfo.value.failed_constraint == "omission_category_invalid"


def test_omission_with_empty_rationale_is_rejected(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.2: rationale must be 1..2,000 characters."""
    bad = OmissionEntry(
        excluded_source_id=_SOURCE_ID,
        excluded_source_revision_id=None,
        category="intentional",
        rationale="",
    )
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(ManifestValidationError) as excinfo:
            manifest_writer.write_manifest(
                conn,
                subject_kind="decision",
                subject_id=_SUBJECT_ID,
                subject_revision_id=None,
                authoring_party_id=_PARTY_ID,
                included_sources=[_fresh_included_source()],
                omissions=[bad],
            )
    assert excinfo.value.failed_constraint == "omission_rationale_missing"


def test_omission_with_rationale_over_2000_chars_is_rejected(
    engine: Engine,
    manifest_writer: ProvenanceManifestWriter,
) -> None:
    """Requirement 10.2: rationale upper bound enforced before INSERT."""
    bad = OmissionEntry(
        excluded_source_id=_SOURCE_ID,
        excluded_source_revision_id=None,
        category="intentional",
        rationale="x" * 2_001,
    )
    with engine.begin() as conn:
        _seed_party(conn)
        with pytest.raises(ManifestValidationError) as excinfo:
            manifest_writer.write_manifest(
                conn,
                subject_kind="decision",
                subject_id=_SUBJECT_ID,
                subject_revision_id=None,
                authoring_party_id=_PARTY_ID,
                included_sources=[_fresh_included_source()],
                omissions=[bad],
            )
    assert excinfo.value.failed_constraint == "omission_rationale_too_long"
