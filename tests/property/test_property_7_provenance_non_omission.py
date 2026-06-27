# Feature: first-walking-slice, Property 7: Provenance non-omission
"""Property 7 — Provenance non-omission (task 9.3).

**Property 7: Provenance non-omission**

For all Findings, Recommendations, Decisions, and Trail Revisions, the
Walking_Slice_System SHALL satisfy: every material source actually
consulted — drawn from Source Document Revision, Content Region
Occurrence, Finding Revision, Recommendation Revision, Decision
Immutable Record, Trail Revision (and Measurement Record / external
Evidence Resource where applicable) — is either listed in the
provenance manifest's ``included_sources_json`` as an Included Source,
or is listed as an Omission Entry in ``Omission_Entries`` recording a
non-empty rationale and one of the five permitted categories
``{intentional, unavailable, restricted, stale, unresolved}``. No
material contributing source is silently absent.

**Validates: Requirements 10.1, 10.2, 10.3, 15.7**

Strategy:

The :class:`~walking_slice.manifests.ProvenanceManifestWriter` is the
single canonical persistence path for every Provenance Manifest in the
slice. :class:`~walking_slice.knowledge.KnowledgeService.create_finding`,
:meth:`~walking_slice.knowledge.KnowledgeService.create_recommendation`,
and :meth:`~walking_slice.knowledge.KnowledgeService.create_decision`
all delegate to it (task 9.2), so a property test that drives the
writer directly covers the manifest contract for every synthesis that
records one — including ``subject_kind='trail_revision'``, whose
upstream Trail_Service is still being built (task 10.x). Driving the
writer directly avoids having to construct full Finding /
Recommendation / Decision rows just to exercise the manifest shape
the property targets.

Per generated case the test draws one *scenario*:

- ``subject_kind`` — one of ``{finding_revision,
  recommendation_revision, decision, trail_revision}`` per Requirement
  10.1.
- A list of *material sources* — ``(kind, resource_id, revision_id)``
  tuples with unique ``resource_id`` values so each source can be
  followed independently through the persistence layer.
- A *role assignment* per material source: either ``'include'`` (the
  source goes into ``included_sources_json``) or one of the five
  omission categories (the source becomes an ``Omission_Entries`` row
  with that category).
- A non-empty rationale per omitted source (1..200 chars), satisfying
  Requirement 10.2's lower bound.

Per scenario the writer is invoked once on a fresh per-case SQLite
engine, then three assertions hold:

1. **Containment.** For every material source in the scenario, exactly
   one persisted record describes it: either an Included Source row
   in ``included_sources_json`` matching ``(kind, resource_id,
   revision_id)``, or an Omission Entry row matching
   ``(excluded_source_id, excluded_source_revision_id)``. No material
   source is silently absent (Requirement 10.1 / 15.7).
2. **Omission integrity.** Every omitted source's persisted Omission
   Entry carries one of the five valid categories and a non-empty
   rationale (Requirements 10.2, 10.3).
3. **No extras.** The persisted ``included_sources_json`` does not
   contain entries the scenario did not declare, and the persisted
   ``Omission_Entries`` rows do not contain entries the scenario did
   not declare. Combined with assertion 1, this gives a true
   round-trip: the persisted manifest is a faithful image of the
   declared material-source set, with no silent omission and no
   silent addition.

The test deliberately restricts Included Sources to
``recorded_at == manifest_recorded_at`` so the Source Freshness Window
(Requirement 10.6) does not reject the write — staleness handling has
its own dedicated unit tests in ``tests/unit/test_manifests.py`` and
is not the property under test here.

.. note::
   Trail Revision finalization in the upstream :mod:`Trail_Service`
   is still being implemented (tasks 10.1, 10.2). This property test
   exercises the manifest-layer contract for
   ``subject_kind='trail_revision'`` because the writer already
   supports that subject kind today; once the Trail_Service lands,
   end-to-end coverage of Trail Revisions through the service surface
   will be added in a follow-up. The property under test holds at
   the manifest layer regardless of how the manifest gets there.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal, Optional

import pytest
import uuid_utils
from hypothesis import HealthCheck, given, settings, strategies as st
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

from walking_slice.clock import FixedClock
from walking_slice.identity import IdentityService
from walking_slice.manifests import (
    IncludedSource,
    OmissionEntry,
    ProvenanceManifestWriter,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the Parties row required by the
# ``Provenance_Manifests.authoring_party_id`` and
# ``Omission_Entries.authoring_party_id`` foreign keys.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_PARTY_CREATED_AT: Final[str] = "2026-01-01T00:00:00.000Z"


# Manifest ``recorded_at`` is pinned via a :class:`FixedClock` so the
# Source Freshness Window check is deterministic across cases — every
# Included Source carries the same ``recorded_at`` as the manifest, so
# no Included Source is ever stale by construction.
_FIXED_NOW: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)


# The four subject_kind values permitted by the
# ``Provenance_Manifests.subject_kind`` CHECK constraint (Requirement
# 10.1). Generating all four exercises the property's "for all
# Findings, Recommendations, Decisions, and Trail Revisions" quantifier
# at the persistence layer. See the module docstring's TODO note about
# end-to-end Trail Revision coverage.
_SUBJECT_KINDS: Final[tuple[str, ...]] = (
    "finding_revision",
    "recommendation_revision",
    "decision",
    "trail_revision",
)


# Kinds permitted on Included Sources entries — matches the slice's
# pipeline stages plus the ``trail_revision`` and ``decision`` summits.
# Mirrors ``walking_slice.manifests._INCLUDED_SOURCE_KINDS``.
_INCLUDED_SOURCE_KINDS: Final[tuple[str, ...]] = (
    "document_revision",
    "region_occurrence",
    "finding_revision",
    "recommendation_revision",
    "decision",
    "trail_revision",
)


# The five Omission Entry categories permitted by Requirement 10.3
# and enforced by the schema CHECK on ``Omission_Entries.category``.
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
            VALUES (:pid, 'person', 'Property 7 Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# A *material source* is a ``(kind, resource_id, revision_id, role,
# rationale)`` tuple. ``role`` is either ``"include"`` (the source goes
# into ``included_sources_json``) or one of the five Omission Entry
# categories (the source becomes an ``Omission_Entries`` row). The
# ``rationale`` is drawn for every source but only consumed when the
# role is one of the five categories; for included sources it is
# ignored.
#
# Unique ``resource_id`` values across one scenario keep each source
# independently traceable in the post-write assertions — a Resource
# Identity that appears both as an Included Source and as an Omission
# Entry would be ambiguous, and the property statement under test
# already names a partition ("either included or omitted"). The schema
# itself does not forbid the same Resource Identity appearing on both
# sides, but the partition is the property's natural reading and is
# what we exercise here.
# ---------------------------------------------------------------------------


def _fresh_uuid7() -> str:
    """Mint one fresh UUIDv7 string.

    Used as a one-off identity for the manifest subject, for material
    source resource and revision identifiers, and (when applicable) for
    revision identifiers paired with included sources. Each call
    returns a fresh value so generated material sources do not collide
    across cases.
    """
    return str(uuid_utils.uuid7())


_revision_id_or_none = st.one_of(
    st.none(),
    st.builds(_fresh_uuid7),
)


# Roles a material source may play in the scenario. The ``include``
# role drops the source into ``included_sources_json``; each of the
# five omission categories drops it into ``Omission_Entries`` with that
# category. Drawing the role per source makes the partition random per
# scenario, exercising the full Cartesian product of (count, role mix)
# the property quantifies over.
_role_strategy = st.sampled_from(
    ("include",) + _OMISSION_CATEGORIES
)


# Rationale length — Requirement 10.2 names 1..2,000 characters; the
# test caps the upper bound at 200 so generated cases stay small
# enough for fast shrinking while still spanning the lower bound, a
# short middle, and a value well above any plausible boundary error in
# the implementation. The lower bound is the one the writer actively
# enforces (empty rationale is rejected); the upper bound is included
# for breadth.
_rationale_strategy = st.text(min_size=1, max_size=200)


@st.composite
def _material_source(draw) -> dict:
    """Draw one material source descriptor.

    Returns a dict with keys:

    - ``kind`` (str): one of the six Included Source kinds.
    - ``resource_id`` (str): a fresh UUIDv7 string. Unique within the
      scenario (enforced by ``unique_by`` on the parent ``lists``
      strategy below).
    - ``revision_id`` (str | None): a fresh UUIDv7 or ``None``.
      Drawn ``None`` for some sources so the property exercises both
      Included Sources without a Revision and Omission Entries
      submitted without ``excluded_source_revision_id`` (Requirement
      10.2: "the excluded source Revision Identity *when known*").
    - ``role`` (str): one of ``("include", "intentional",
      "unavailable", "restricted", "stale", "unresolved")``.
    - ``rationale`` (str): a 1..200 character non-empty rationale.
      Consumed only when ``role`` is an omission category.
    """
    return {
        "kind": draw(st.sampled_from(_INCLUDED_SOURCE_KINDS)),
        "resource_id": _fresh_uuid7(),
        "revision_id": draw(_revision_id_or_none),
        "role": draw(_role_strategy),
        "rationale": draw(_rationale_strategy),
    }


_material_sources_strategy = st.lists(
    _material_source(),
    min_size=0,
    max_size=8,
    # Unique resource_id values so each source is independently
    # traceable in the post-write assertion loop. ``unique_by`` is a
    # Hypothesis primitive that takes a key function and discards
    # draws whose key would duplicate an earlier element.
    unique_by=lambda source: source["resource_id"],
)


_subject_kind_strategy = st.sampled_from(_SUBJECT_KINDS)


@st.composite
def _scenario(draw) -> dict:
    """Draw one manifest scenario.

    Each scenario carries one ``subject_kind`` (the manifest's
    subject classification), a fresh ``subject_id``, an optional
    ``subject_revision_id`` (``None`` for ``decision`` per AD-WS-4;
    the writer accepts ``None`` for any kind so we draw it freely
    rather than coupling the strategy to AD-WS-4 here — the property
    holds regardless of whether the subject is a Resource Revision
    or an Immutable Record), and a list of material sources.
    """
    subject_kind = draw(_subject_kind_strategy)
    subject_id = _fresh_uuid7()
    # ``subject_revision_id`` is nullable for every ``subject_kind``
    # in the schema. The writer does not couple it to ``subject_kind``
    # (other consumers may; the property under test does not).
    subject_revision_id = draw(_revision_id_or_none)
    material_sources = draw(_material_sources_strategy)
    return {
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "subject_revision_id": subject_revision_id,
        "material_sources": material_sources,
    }


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers and Provenance_Manifests /
# Omission_Entries rows cannot leak between cases (design §"Testing
# Strategy" — "Each property and example test gets a fresh SQLite
# database"). A :class:`tempfile.TemporaryDirectory` context inside the
# test body owns the per-case directory; Hypothesis disallows
# function-scoped pytest fixtures for per-case state because they would
# not reset between generated inputs.
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


def _fetch_manifest(engine: Engine, manifest_id: str) -> Optional[dict]:
    """Return the ``Provenance_Manifests`` row for *manifest_id*, or ``None``."""
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


def _fetch_omissions(engine: Engine, manifest_id: str) -> list[dict]:
    """Return every ``Omission_Entries`` row attached to *manifest_id*."""
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT omission_entry_id, manifest_id,
                           excluded_source_id, excluded_source_revision_id,
                           category, rationale, authoring_party_id,
                           recorded_at, resolved_at
                      FROM Omission_Entries
                     WHERE manifest_id = :mid
                    """
                ),
                {"mid": manifest_id},
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 7: Provenance non-omission
@given(scenario=_scenario())
@settings(
    max_examples=100,
    deadline=2000,
    # Each case allocates a fresh temp directory and a fresh SQLite
    # database so per-case setup is more expensive than a pure
    # in-memory property test. The setup is still well under the
    # 2000 ms deadline locally but we suppress the data-generation
    # health check so any one slow case does not abort the run.
    suppress_health_check=[HealthCheck.too_slow],
)
def test_provenance_non_omission(scenario: dict) -> None:
    """Every material source declared for a Provenance Manifest is either
    listed in ``included_sources_json`` or recorded as an Omission Entry
    with a valid category and a non-empty rationale."""
    subject_kind: str = scenario["subject_kind"]
    subject_id: str = scenario["subject_id"]
    subject_revision_id: Optional[str] = scenario["subject_revision_id"]
    material_sources: list[dict] = scenario["material_sources"]

    # Partition the material sources by role. ``included`` carries the
    # sources that will be passed to ``write_manifest`` as Included
    # Sources; ``omitted`` carries those that will be passed as
    # Omission Entries with one of the five categories. The partition
    # is the natural reading of the property statement ("either listed
    # as an included source, or listed as an Omission Entry") and is
    # what the post-write assertions check.
    included_sources: list[IncludedSource] = []
    omissions: list[OmissionEntry] = []
    declared_included: list[dict] = []  # the original dict for each
    declared_omitted: list[dict] = []

    for source in material_sources:
        if source["role"] == "include":
            included_sources.append(
                IncludedSource(
                    kind=source["kind"],  # type: ignore[arg-type]
                    resource_id=source["resource_id"],
                    revision_id=source["revision_id"],
                    # Pin every Included Source to the manifest's
                    # ``recorded_at`` so the Source Freshness Window
                    # check (Requirement 10.6) never rejects a write.
                    # Staleness handling has dedicated unit tests; the
                    # property under test here is non-omission, not
                    # freshness.
                    recorded_at=_FIXED_NOW,
                )
            )
            declared_included.append(source)
        else:
            # ``role`` is one of the five omission categories.
            omissions.append(
                OmissionEntry(
                    excluded_source_id=source["resource_id"],
                    excluded_source_revision_id=source["revision_id"],
                    category=source["role"],  # type: ignore[arg-type]
                    rationale=source["rationale"],
                )
            )
            declared_omitted.append(source)

    with tempfile.TemporaryDirectory(prefix="walking_slice_prop7_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh services per case so :class:`IdentityService` in-memory
        # state cannot bleed across cases. The pinned :class:`FixedClock`
        # makes ``manifest_recorded_at`` deterministic for shrinking
        # and aligns it with every Included Source's ``recorded_at``
        # so freshness is satisfied by construction.
        clock = FixedClock(_FIXED_NOW)
        identity_service = IdentityService()
        writer = ProvenanceManifestWriter(
            clock=clock,
            identity_service=identity_service,
        )

        try:
            with engine.begin() as conn:
                _seed_party(conn)
                result = writer.write_manifest(
                    conn,
                    subject_kind=subject_kind,  # type: ignore[arg-type]
                    subject_id=subject_id,
                    subject_revision_id=subject_revision_id,
                    authoring_party_id=_PARTY_ID,
                    included_sources=tuple(included_sources),
                    omissions=tuple(omissions),
                )

            # ----- Read back the persisted state ---------------------
            manifest_row = _fetch_manifest(engine, result.manifest_id)
            assert manifest_row is not None, (
                "ProvenanceManifestWriter.write_manifest returned "
                f"manifest_id={result.manifest_id!r} but no "
                "Provenance_Manifests row resolves to that identity."
            )

            included_json: list[dict] = json.loads(
                manifest_row["included_sources_json"]
            )
            omission_rows = _fetch_omissions(engine, result.manifest_id)

            # Index the persisted Included Sources by
            # ``(kind, resource_id, revision_id)``. The triple uniquely
            # identifies an Included Source within one manifest because
            # the scenario strategy mints unique ``resource_id`` values
            # per case.
            included_index: dict[tuple[str, str, Optional[str]], dict] = {
                (entry["kind"], entry["resource_id"], entry["revision_id"]): entry
                for entry in included_json
            }
            # Index the persisted Omission Entries by
            # ``(excluded_source_id, excluded_source_revision_id)``.
            # The pair uniquely identifies an Omission Entry within
            # one manifest because the scenario mints unique
            # ``resource_id`` values per case.
            omission_index: dict[tuple[str, Optional[str]], dict] = {
                (row["excluded_source_id"], row["excluded_source_revision_id"]): row
                for row in omission_rows
            }

            # ----- Assertion 1 — Containment -------------------------
            # Every declared material source resolves either to an
            # Included Source or to an Omission Entry. No material
            # source is silently absent (Requirement 10.1 / 15.7).
            for declared in declared_included:
                key = (
                    declared["kind"],
                    declared["resource_id"],
                    declared["revision_id"],
                )
                assert key in included_index, (
                    f"Material source {key!r} declared as 'include' "
                    "for the manifest subject "
                    f"(subject_kind={subject_kind!r}, "
                    f"subject_id={subject_id!r}) is missing from "
                    "included_sources_json. Requirement 10.1 / 15.7 "
                    "forbid silently omitting a material source."
                )

            for declared in declared_omitted:
                key = (declared["resource_id"], declared["revision_id"])
                assert key in omission_index, (
                    f"Material source {key!r} declared as an "
                    f"omission (category={declared['role']!r}) for "
                    "the manifest subject "
                    f"(subject_kind={subject_kind!r}, "
                    f"subject_id={subject_id!r}) is missing from "
                    "Omission_Entries. Requirement 10.2 / 15.7 "
                    "require every omitted source to surface as an "
                    "Omission Entry."
                )

            # ----- Assertion 2 — Omission integrity ------------------
            # Every persisted Omission Entry that corresponds to a
            # declared omitted source carries a valid category and a
            # non-empty rationale (Requirements 10.2, 10.3).
            for declared in declared_omitted:
                key = (declared["resource_id"], declared["revision_id"])
                row = omission_index[key]
                assert row["category"] in _OMISSION_CATEGORIES, (
                    f"Omission Entry for {key!r} has category "
                    f"{row['category']!r}; Requirement 10.3 names the "
                    f"five permitted categories {_OMISSION_CATEGORIES!r}."
                )
                assert row["category"] == declared["role"], (
                    f"Omission Entry for {key!r} persisted category "
                    f"{row['category']!r} but the scenario declared "
                    f"{declared['role']!r}. The writer must faithfully "
                    "record the declared category (Requirement 10.3)."
                )
                assert isinstance(row["rationale"], str) and len(
                    row["rationale"]
                ) >= 1, (
                    f"Omission Entry for {key!r} has rationale "
                    f"{row['rationale']!r}; Requirement 10.2 requires "
                    "a non-empty rationale (1..2,000 characters)."
                )
                assert row["rationale"] == declared["rationale"], (
                    f"Omission Entry for {key!r} persisted rationale "
                    f"differs from the scenario declaration: "
                    f"persisted={row['rationale']!r}, "
                    f"declared={declared['rationale']!r}. The writer "
                    "must round-trip the rationale verbatim."
                )

            # ----- Assertion 3 — No extras ---------------------------
            # The persisted Included Sources and Omission Entries
            # collections contain exactly the declared sources — no
            # silent addition. Combined with assertion 1, this gives a
            # true round-trip.
            declared_included_keys = {
                (d["kind"], d["resource_id"], d["revision_id"])
                for d in declared_included
            }
            assert set(included_index.keys()) == declared_included_keys, (
                "Persisted included_sources_json diverges from the "
                f"scenario declaration: persisted="
                f"{sorted(included_index.keys())!r}, declared="
                f"{sorted(declared_included_keys)!r}."
            )
            declared_omitted_keys = {
                (d["resource_id"], d["revision_id"]) for d in declared_omitted
            }
            assert set(omission_index.keys()) == declared_omitted_keys, (
                "Persisted Omission_Entries diverges from the scenario "
                f"declaration: persisted={sorted(omission_index.keys())!r}, "
                f"declared={sorted(declared_omitted_keys)!r}."
            )

            # ----- Bonus invariant — is_complete reflects categories -
            # Requirement 10.3 / design §"Persistence Invariants
            # Summary" item 9: ``is_complete = 0`` when any unresolved
            # Omission Entry has a non-intentional category. The
            # property statement itself focuses on the "every material
            # source observable" invariant, but the implementation's
            # ``is_complete`` computation is part of the same write
            # path — checking it here closes one loop more on
            # Requirement 10.3.
            has_non_intentional = any(
                d["role"] != "include" and d["role"] != "intentional"
                for d in material_sources
            )
            expected_is_complete = 0 if has_non_intentional else 1
            assert manifest_row["is_complete"] == expected_is_complete, (
                "Provenance_Manifests.is_complete diverged from the "
                "category mix: "
                f"persisted={manifest_row['is_complete']}, "
                f"expected={expected_is_complete}. Requirement 10.3 "
                "requires is_complete=0 whenever any unresolved "
                "Omission Entry has a non-intentional category."
            )
        finally:
            engine.dispose()
