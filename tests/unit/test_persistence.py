"""Unit tests for :mod:`walking_slice.persistence`.

These tests pin the contract established in
``.kiro/specs/first-walking-slice/design.md`` §"Table-by-Table Specification",
§"Persistence Invariants Summary", and AD-WS-4/AD-WS-8:

- Every immutable table rejects ``UPDATE`` and ``DELETE`` (Property 12;
  Requirements 2.4, 6.6, 13.3, 13.5).
- ``Role_Assignments.revoked_at`` is one-shot: it may transition from
  ``NULL`` to a recorded timestamp exactly once.
- ``Omission_Entries.resolved_at`` is one-shot under the same rules.
- The composite indexes named in AD-WS-8 are present.
- Every new DBAPI connection has ``PRAGMA journal_mode=WAL`` and
  ``PRAGMA foreign_keys=ON`` set (Requirement 16.2).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.persistence import (
    SCHEMA_STATEMENTS,
    create_schema,
    install_pragmas,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers
#
# These helpers insert the minimum rows each test needs so that immutability
# triggers can be exercised against real data. They never touch the columns
# being mutated by the test under examination — each test is responsible for
# choosing the table and field it modifies.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_RESOURCE_ID = "00000000-0000-7000-8000-000000000010"
_REVISION_ID = "00000000-0000-7000-8000-000000000011"
_REGION_ID = "00000000-0000-7000-8000-000000000020"
_FINDING_ID = "00000000-0000-7000-8000-000000000030"
_FINDING_REV_ID = "00000000-0000-7000-8000-000000000031"
_REC_ID = "00000000-0000-7000-8000-000000000040"
_REC_REV_ID = "00000000-0000-7000-8000-000000000041"
_DECISION_ID = "00000000-0000-7000-8000-000000000050"
_RELATIONSHIP_ID = "00000000-0000-7000-8000-000000000060"
_TRAIL_ID = "00000000-0000-7000-8000-000000000070"
_TRAIL_REV_ID = "00000000-0000-7000-8000-000000000071"
_TRAIL_STEP_ID = "00000000-0000-7000-8000-000000000072"
_MANIFEST_ID = "00000000-0000-7000-8000-000000000080"
_OMISSION_ID = "00000000-0000-7000-8000-000000000081"
_AUDIT_ID = "00000000-0000-7000-8000-000000000090"
_ROLE_ID = "00000000-0000-7000-8000-0000000000a0"
_AUTHORITY_ID = "00000000-0000-7000-8000-0000000000a1"
_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-0000000000a2"

_TS = "2026-01-01T00:00:00.000Z"
_TS_LATER = "2026-01-02T00:00:00.000Z"


def _seed_party(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Test Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _TS},
    )


def _seed_source_document(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Source_Documents
                (resource_id, current_location, authority, created_at)
            VALUES (:rid, '/seed/path', 'authoritative', :ts)
            """
        ),
        {"rid": _RESOURCE_ID, "ts": _TS},
    )


def _seed_document_revision(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Document_Revisions
                (revision_id, resource_id, content_bytes, content_digest_sha256,
                 contributing_party_id, recorded_at, change_description)
            VALUES (:rev, :rid, :body, :digest, :pid, :ts, 'initial')
            """
        ),
        {
            "rev": _REVISION_ID,
            "rid": _RESOURCE_ID,
            "body": b"hello world",
            "digest": "0" * 64,
            "pid": _PARTY_ID,
            "ts": _TS,
        },
    )


def _seed_content_region(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Content_Regions (region_id, parent_resource_id, created_at)
            VALUES (:rid, :doc, :ts)
            """
        ),
        {"rid": _REGION_ID, "doc": _RESOURCE_ID, "ts": _TS},
    )


def _seed_region_occurrence(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Region_Occurrences
                (region_id, document_revision_id, start_offset_bytes,
                 end_offset_bytes, span_byte_length, span_content_digest_sha256,
                 recorded_at)
            VALUES (:rid, :doc, 0, 5, 5, :digest, :ts)
            """
        ),
        {
            "rid": _REGION_ID,
            "doc": _REVISION_ID,
            "digest": "f" * 64,
            "ts": _TS,
        },
    )


def _seed_finding(conn) -> None:
    conn.execute(
        text("INSERT INTO Findings (finding_id, created_at) VALUES (:fid, :ts)"),
        {"fid": _FINDING_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Finding_Revisions
                (finding_revision_id, finding_id, statement, is_hypothesis,
                 authoring_party_id, assumptions_json, recorded_at)
            VALUES (:frev, :fid, 'A claim.', 0, :pid, '[]', :ts)
            """
        ),
        {"frev": _FINDING_REV_ID, "fid": _FINDING_ID, "pid": _PARTY_ID, "ts": _TS},
    )


def _seed_recommendation(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO Recommendations (recommendation_id, created_at) "
            "VALUES (:rid, :ts)"
        ),
        {"rid": _REC_ID, "ts": _TS},
    )
    conn.execute(
        text(
            """
            INSERT INTO Recommendation_Revisions
                (recommendation_revision_id, recommendation_id, rationale,
                 assumptions_json, confidence, authoring_party_id, recorded_at)
            VALUES (:rrev, :rid, 'Because.', '[]', 'Medium', :pid, :ts)
            """
        ),
        {"rrev": _REC_REV_ID, "rid": _REC_ID, "pid": _PARTY_ID, "ts": _TS},
    )


def _seed_decision(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Decisions
                (decision_id, target_recommendation_id,
                 target_recommendation_revision_id, outcome, rationale,
                 deciding_party_id, authority_basis_type, authority_basis_id,
                 applicable_scope, recorded_at)
            VALUES (:did, :rid, :rrev, 'Accept', 'Approved.', :pid,
                    'role-grant-id', :abid, 'scope-1', :ts)
            """
        ),
        {
            "did": _DECISION_ID,
            "rid": _REC_ID,
            "rrev": _REC_REV_ID,
            "pid": _PARTY_ID,
            "abid": _AUTHORITY_BASIS_ID,
            "ts": _TS,
        },
    )


def _seed_relationship(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Relationships
                (relationship_id, relationship_type, source_kind, source_id,
                 source_revision_id, target_kind, target_id,
                 target_revision_id, authoring_party_id, recorded_at)
            VALUES (:rid, 'Supports', 'finding_revision', :fid, :frev,
                    'region_occurrence', :reg, :doc, :pid, :ts)
            """
        ),
        {
            "rid": _RELATIONSHIP_ID,
            "fid": _FINDING_ID,
            "frev": _FINDING_REV_ID,
            "reg": _REGION_ID,
            "doc": _REVISION_ID,
            "pid": _PARTY_ID,
            "ts": _TS,
        },
    )


def _seed_trail(conn) -> None:
    conn.execute(
        text(
            "INSERT INTO Trails (trail_id, created_at, current_revision_id) "
            "VALUES (:tid, :ts, :trev)"
        ),
        {"tid": _TRAIL_ID, "ts": _TS, "trev": _TRAIL_REV_ID},
    )
    conn.execute(
        text(
            """
            INSERT INTO Trail_Revisions
                (trail_revision_id, trail_id, purpose, audience_id,
                 ordering_rationale, authoring_party_id, recorded_at)
            VALUES (:trev, :tid, 'Walk', 'audience-1', 'because', :pid, :ts)
            """
        ),
        {"trev": _TRAIL_REV_ID, "tid": _TRAIL_ID, "pid": _PARTY_ID, "ts": _TS},
    )


def _seed_trail_step(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Trail_Steps
                (trail_step_id, trail_revision_id, ordinal, selection_mode,
                 target_kind, target_id, target_revision_id, region_id,
                 annotation)
            VALUES (:tsid, :trev, 1, 'Pinned', 'document_revision', :rid,
                    :rev, NULL, 'first step')
            """
        ),
        {
            "tsid": _TRAIL_STEP_ID,
            "trev": _TRAIL_REV_ID,
            "rid": _RESOURCE_ID,
            "rev": _REVISION_ID,
        },
    )


def _seed_manifest(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Provenance_Manifests
                (manifest_id, subject_kind, subject_id, subject_revision_id,
                 authoring_party_id, recorded_at, included_sources_json,
                 is_complete)
            VALUES (:mid, 'decision', :did, NULL, :pid, :ts, '[]', 1)
            """
        ),
        {"mid": _MANIFEST_ID, "did": _DECISION_ID, "pid": _PARTY_ID, "ts": _TS},
    )


def _seed_omission(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Omission_Entries
                (omission_entry_id, manifest_id, excluded_source_id, category,
                 rationale, authoring_party_id, recorded_at)
            VALUES (:oid, :mid, 'src-x', 'unavailable', 'not reachable',
                    :pid, :ts)
            """
        ),
        {"oid": _OMISSION_ID, "mid": _MANIFEST_ID, "pid": _PARTY_ID, "ts": _TS},
    )


def _seed_audit(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Audit_Records
                (audit_record_id, append_sequence, actor_party_id, action_type,
                 outcome, target_id, correlation_id, recorded_at)
            VALUES (:aid, 1, :pid, 'create.finding', 'consequential', :fid,
                    'corr-1', :ts)
            """
        ),
        {"aid": _AUDIT_ID, "pid": _PARTY_ID, "fid": _FINDING_ID, "ts": _TS},
    )


def _seed_role_assignment(conn, revoked_at: str | None = None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Role_Assignments
                (role_assignment_id, party_id, role_name, scope,
                 authorities_granted, effective_start, effective_end,
                 revoked_at, assigning_authority_id, recorded_at)
            VALUES (:rid, :pid, 'analyst', 'scope-1', '["view","approve"]',
                    :ts, NULL, :revoked, :aid, :ts)
            """
        ),
        {
            "rid": _ROLE_ID,
            "pid": _PARTY_ID,
            "ts": _TS,
            "revoked": revoked_at,
            "aid": _AUTHORITY_ID,
        },
    )


def _seed_full_graph(conn) -> None:
    """Seed a full, FK-consistent row in every table the tests touch."""
    _seed_party(conn)
    _seed_source_document(conn)
    _seed_document_revision(conn)
    _seed_content_region(conn)
    _seed_region_occurrence(conn)
    _seed_finding(conn)
    _seed_recommendation(conn)
    _seed_decision(conn)
    _seed_relationship(conn)
    _seed_trail(conn)
    _seed_trail_step(conn)
    _seed_manifest(conn)
    _seed_omission(conn)
    _seed_audit(conn)
    _seed_role_assignment(conn)


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


def test_create_schema_creates_every_named_table(engine: Engine) -> None:
    create_schema(engine)
    expected = {
        "Identifier_Registry",
        "Parties",
        "Role_Assignments",
        "Source_Documents",
        "Document_Revisions",
        "Content_Regions",
        "Region_Occurrences",
        "Findings",
        "Finding_Revisions",
        "Recommendations",
        "Recommendation_Revisions",
        "Decisions",
        "Relationships",
        "Trails",
        "Trail_Revisions",
        "Trail_Steps",
        "Provenance_Manifests",
        "Omission_Entries",
        "Audit_Records",
        "Interim_ADR_Records",
        "Disclosure_Policies",
    }
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    actual = {row[0] for row in rows}
    missing = expected - actual
    assert not missing, f"missing tables: {sorted(missing)}"


def test_create_schema_is_idempotent(engine: Engine) -> None:
    create_schema(engine)
    # Re-running must not raise — every statement uses IF NOT EXISTS.
    create_schema(engine)


def test_required_indexes_are_present(engine: Engine) -> None:
    """AD-WS-8 backlink index and the §Persistence-Invariants companions."""
    create_schema(engine)
    expected = {
        "ix_document_revisions_resource_recorded",
        "ix_relationships_target_backlink",
        "ix_relationships_source_outbound",
        "ix_audit_records_recorded_sequence",
    }
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index'")
        ).all()
    actual = {row[0] for row in rows}
    missing = expected - actual
    assert not missing, f"missing indexes: {sorted(missing)}"


def test_schema_statements_tuple_is_immutable() -> None:
    """``SCHEMA_STATEMENTS`` is exposed for inspection; it must be a tuple."""
    assert isinstance(SCHEMA_STATEMENTS, tuple)
    assert len(SCHEMA_STATEMENTS) > 0


# ---------------------------------------------------------------------------
# Pragmas (Requirement 16.2)
# ---------------------------------------------------------------------------


def test_install_pragmas_enables_wal_and_foreign_keys(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'pragma.sqlite').as_posix()}"
    eng = create_engine(url, future=True)
    try:
        install_pragmas(eng)
        with eng.connect() as conn:
            journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
            foreign_keys = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
        assert str(journal_mode).lower() == "wal"
        assert int(foreign_keys) == 1
    finally:
        eng.dispose()


def test_install_pragmas_is_idempotent(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'pragma.sqlite').as_posix()}"
    eng = create_engine(url, future=True)
    try:
        install_pragmas(eng)
        install_pragmas(eng)  # second call must be a no-op
        with eng.connect() as conn:
            assert str(
                conn.execute(text("PRAGMA journal_mode")).scalar_one()
            ).lower() == "wal"
    finally:
        eng.dispose()


def test_create_schema_also_installs_pragmas(tmp_path: Path) -> None:
    """`create_schema` must leave every new connection with WAL + FK on."""
    url = f"sqlite:///{(tmp_path / 'schema.sqlite').as_posix()}"
    eng = create_engine(url, future=True)
    try:
        create_schema(eng)
        # Open a brand-new connection — pragmas must already be set.
        with eng.connect() as conn:
            assert str(
                conn.execute(text("PRAGMA journal_mode")).scalar_one()
            ).lower() == "wal"
            assert int(
                conn.execute(text("PRAGMA foreign_keys")).scalar_one()
            ) == 1
    finally:
        eng.dispose()


# ---------------------------------------------------------------------------
# Append-only immutability triggers (Property 12; Requirements 2.4, 6.6, 13.3)
# ---------------------------------------------------------------------------


# Each entry: (table, seed callable, identity-column-name, identity-value, mutation SQL).
# The mutation must touch an existing row so the trigger has something to fire on.
_IMMUTABLE_CASES: tuple[tuple, ...] = (
    (
        "Document_Revisions",
        _seed_document_revision,
        "UPDATE Document_Revisions SET change_description='oops' WHERE revision_id = :id",
        {"id": _REVISION_ID},
        "DELETE FROM Document_Revisions WHERE revision_id = :id",
    ),
    (
        "Region_Occurrences",
        _seed_region_occurrence,
        "UPDATE Region_Occurrences SET start_offset_bytes = 1 WHERE region_id = :id",
        {"id": _REGION_ID},
        "DELETE FROM Region_Occurrences WHERE region_id = :id",
    ),
    (
        "Finding_Revisions",
        _seed_finding,
        "UPDATE Finding_Revisions SET statement = 'oops' WHERE finding_revision_id = :id",
        {"id": _FINDING_REV_ID},
        "DELETE FROM Finding_Revisions WHERE finding_revision_id = :id",
    ),
    (
        "Recommendation_Revisions",
        _seed_recommendation,
        "UPDATE Recommendation_Revisions SET rationale = 'oops' "
        "WHERE recommendation_revision_id = :id",
        {"id": _REC_REV_ID},
        "DELETE FROM Recommendation_Revisions WHERE recommendation_revision_id = :id",
    ),
    (
        "Decisions",
        _seed_decision,
        "UPDATE Decisions SET rationale = 'oops' WHERE decision_id = :id",
        {"id": _DECISION_ID},
        "DELETE FROM Decisions WHERE decision_id = :id",
    ),
    (
        "Relationships",
        _seed_relationship,
        "UPDATE Relationships SET relationship_type = 'Contradicts' "
        "WHERE relationship_id = :id",
        {"id": _RELATIONSHIP_ID},
        "DELETE FROM Relationships WHERE relationship_id = :id",
    ),
    (
        "Trail_Revisions",
        _seed_trail,
        "UPDATE Trail_Revisions SET purpose = 'oops' WHERE trail_revision_id = :id",
        {"id": _TRAIL_REV_ID},
        "DELETE FROM Trail_Revisions WHERE trail_revision_id = :id",
    ),
    (
        "Trail_Steps",
        _seed_trail_step,
        "UPDATE Trail_Steps SET annotation = 'oops' WHERE trail_step_id = :id",
        {"id": _TRAIL_STEP_ID},
        "DELETE FROM Trail_Steps WHERE trail_step_id = :id",
    ),
    (
        "Provenance_Manifests",
        _seed_manifest,
        "UPDATE Provenance_Manifests SET is_complete = 0 WHERE manifest_id = :id",
        {"id": _MANIFEST_ID},
        "DELETE FROM Provenance_Manifests WHERE manifest_id = :id",
    ),
    (
        "Audit_Records",
        _seed_audit,
        "UPDATE Audit_Records SET reason_code = 'oops' WHERE audit_record_id = :id",
        {"id": _AUDIT_ID},
        "DELETE FROM Audit_Records WHERE audit_record_id = :id",
    ),
)


def _seed_immutable_target(table: str, conn) -> None:
    """Seed every FK dependency for ``table`` plus the row under test exactly once.

    The slice's schema is fully connected — Decisions reference Recommendations,
    Recommendations reference Findings (in the application layer), Manifests
    reference Decisions, and so on. This helper makes the dependency order
    explicit per table so each parametrized test sets up exactly the rows it
    needs without double-inserting any single seed.
    """
    _seed_party(conn)
    _seed_source_document(conn)

    if table == "Document_Revisions":
        _seed_document_revision(conn)
        return

    # Every other table needs a Document Revision in scope so the audit/region
    # FKs resolve cleanly.
    _seed_document_revision(conn)

    if table == "Region_Occurrences":
        _seed_content_region(conn)
        _seed_region_occurrence(conn)
        return

    if table == "Finding_Revisions":
        _seed_finding(conn)
        return

    if table == "Recommendation_Revisions":
        _seed_recommendation(conn)
        return

    if table == "Decisions":
        _seed_recommendation(conn)
        _seed_decision(conn)
        return

    if table == "Relationships":
        _seed_content_region(conn)
        _seed_region_occurrence(conn)
        _seed_finding(conn)
        _seed_relationship(conn)
        return

    if table == "Trail_Revisions":
        _seed_trail(conn)
        return

    if table == "Trail_Steps":
        _seed_trail(conn)
        _seed_trail_step(conn)
        return

    if table == "Provenance_Manifests":
        _seed_recommendation(conn)
        _seed_decision(conn)
        _seed_manifest(conn)
        return

    if table == "Audit_Records":
        _seed_finding(conn)  # provides target_id FK
        _seed_audit(conn)
        return

    raise AssertionError(f"unknown immutable table: {table}")


@pytest.mark.parametrize(
    "table, seed, update_sql, params, delete_sql", _IMMUTABLE_CASES
)
def test_immutable_table_rejects_update(
    engine: Engine, table, seed, update_sql, params, delete_sql
) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_immutable_target(table, conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(update_sql), params)


@pytest.mark.parametrize(
    "table, seed, update_sql, params, delete_sql", _IMMUTABLE_CASES
)
def test_immutable_table_rejects_delete(
    engine: Engine, table, seed, update_sql, params, delete_sql
) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_immutable_target(table, conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(delete_sql), params)


def test_immutable_row_remains_byte_equivalent_after_rejected_update(
    engine: Engine,
) -> None:
    """Property 12 — rejected updates must leave the row byte-equivalent."""
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_source_document(conn)
        _seed_document_revision(conn)

    before = _row(engine, "Document_Revisions", {"revision_id": _REVISION_ID})
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Document_Revisions SET change_description='oops' "
                    "WHERE revision_id=:r"
                ),
                {"r": _REVISION_ID},
            )
    after = _row(engine, "Document_Revisions", {"revision_id": _REVISION_ID})
    assert before == after, "rejected UPDATE must not mutate the row"


def _row(engine: Engine, table: str, where: dict[str, str]) -> tuple:
    column = next(iter(where.keys()))
    with engine.connect() as conn:
        return conn.execute(
            text(f"SELECT * FROM {table} WHERE {column}=:v"),
            {"v": where[column]},
        ).one()


# ---------------------------------------------------------------------------
# One-shot field rules: Role_Assignments.revoked_at
# ---------------------------------------------------------------------------


def test_role_assignment_revoked_at_first_set_from_null_succeeds(
    engine: Engine,
) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_role_assignment(conn, revoked_at=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Role_Assignments SET revoked_at=:ts "
                "WHERE role_assignment_id=:r"
            ),
            {"ts": _TS_LATER, "r": _ROLE_ID},
        )
    with engine.connect() as conn:
        revoked = conn.execute(
            text(
                "SELECT revoked_at FROM Role_Assignments "
                "WHERE role_assignment_id=:r"
            ),
            {"r": _ROLE_ID},
        ).scalar_one()
    assert revoked == _TS_LATER


def test_role_assignment_revoked_at_cannot_be_overwritten(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_role_assignment(conn, revoked_at=_TS_LATER)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Role_Assignments SET revoked_at='2026-02-01T00:00:00.000Z' "
                    "WHERE role_assignment_id=:r"
                ),
                {"r": _ROLE_ID},
            )


def test_role_assignment_revoked_at_cannot_be_cleared(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_role_assignment(conn, revoked_at=_TS_LATER)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Role_Assignments SET revoked_at=NULL "
                    "WHERE role_assignment_id=:r"
                ),
                {"r": _ROLE_ID},
            )


def test_role_assignment_non_revoked_columns_are_immutable(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_role_assignment(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Role_Assignments SET role_name='manager' "
                    "WHERE role_assignment_id=:r"
                ),
                {"r": _ROLE_ID},
            )


def test_role_assignment_delete_is_rejected(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_role_assignment(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text("DELETE FROM Role_Assignments WHERE role_assignment_id=:r"),
                {"r": _ROLE_ID},
            )


# ---------------------------------------------------------------------------
# One-shot field rules: Omission_Entries.resolved_at
# ---------------------------------------------------------------------------


def _seed_decision_chain_for_omission(conn) -> None:
    _seed_party(conn)
    _seed_source_document(conn)
    _seed_document_revision(conn)
    _seed_finding(conn)
    _seed_recommendation(conn)
    _seed_decision(conn)
    _seed_manifest(conn)


def test_omission_resolved_at_first_set_from_null_succeeds(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_decision_chain_for_omission(conn)
        _seed_omission(conn)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE Omission_Entries SET resolved_at=:ts "
                "WHERE omission_entry_id=:o"
            ),
            {"ts": _TS_LATER, "o": _OMISSION_ID},
        )
    with engine.connect() as conn:
        resolved = conn.execute(
            text(
                "SELECT resolved_at FROM Omission_Entries "
                "WHERE omission_entry_id=:o"
            ),
            {"o": _OMISSION_ID},
        ).scalar_one()
    assert resolved == _TS_LATER


def test_omission_resolved_at_cannot_be_overwritten(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_decision_chain_for_omission(conn)
        _seed_omission(conn)
        conn.execute(
            text(
                "UPDATE Omission_Entries SET resolved_at=:ts "
                "WHERE omission_entry_id=:o"
            ),
            {"ts": _TS_LATER, "o": _OMISSION_ID},
        )
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Omission_Entries SET resolved_at='2026-02-01T00:00:00.000Z' "
                    "WHERE omission_entry_id=:o"
                ),
                {"o": _OMISSION_ID},
            )


def test_omission_resolved_at_cannot_be_cleared(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_decision_chain_for_omission(conn)
        _seed_omission(conn)
        conn.execute(
            text(
                "UPDATE Omission_Entries SET resolved_at=:ts "
                "WHERE omission_entry_id=:o"
            ),
            {"ts": _TS_LATER, "o": _OMISSION_ID},
        )
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Omission_Entries SET resolved_at=NULL "
                    "WHERE omission_entry_id=:o"
                ),
                {"o": _OMISSION_ID},
            )


def test_omission_other_columns_are_immutable(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_decision_chain_for_omission(conn)
        _seed_omission(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Omission_Entries SET rationale='different' "
                    "WHERE omission_entry_id=:o"
                ),
                {"o": _OMISSION_ID},
            )


def test_omission_delete_is_rejected(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        _seed_decision_chain_for_omission(conn)
        _seed_omission(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text("DELETE FROM Omission_Entries WHERE omission_entry_id=:o"),
                {"o": _OMISSION_ID},
            )


# ---------------------------------------------------------------------------
# Identifier_Registry append-only (AD-WS-2 support)
# ---------------------------------------------------------------------------


def test_identifier_registry_update_is_rejected(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Identifier_Registry "
                "(identifier, kind, content_digest, issued_at) "
                "VALUES ('00000000-0000-7000-8000-000000000999','resource','d',:ts)"
            ),
            {"ts": _TS},
        )
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Identifier_Registry SET content_digest='other' "
                    "WHERE identifier='00000000-0000-7000-8000-000000000999'"
                )
            )


def test_identifier_registry_delete_is_rejected(engine: Engine) -> None:
    create_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Identifier_Registry "
                "(identifier, kind, content_digest, issued_at) "
                "VALUES ('00000000-0000-7000-8000-000000000998','resource','d',:ts)"
            ),
            {"ts": _TS},
        )
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "DELETE FROM Identifier_Registry "
                    "WHERE identifier='00000000-0000-7000-8000-000000000998'"
                )
            )


# ---------------------------------------------------------------------------
# Structural CHECK constraints sanity (Trail_Steps target_kind ↔ ordinal)
# ---------------------------------------------------------------------------


def test_trail_step_ordinal_target_kind_check(engine: Engine) -> None:
    """A Trail Step with ordinal=1 but target_kind='decision' is rejected.

    Supports Property 5 (Trail linearity) and Requirements 9.2/9.7.
    """
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_source_document(conn)
        _seed_document_revision(conn)
        _seed_trail(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    """
                    INSERT INTO Trail_Steps
                        (trail_step_id, trail_revision_id, ordinal,
                         selection_mode, target_kind, target_id,
                         target_revision_id, region_id, annotation)
                    VALUES (:tsid, :trev, 1, 'Pinned', 'decision', :did,
                            NULL, NULL, NULL)
                    """
                ),
                {
                    "tsid": "00000000-0000-7000-8000-0000000000ff",
                    "trev": _TRAIL_REV_ID,
                    "did": _DECISION_ID,
                },
            )


def test_decision_unique_per_recommendation_revision(engine: Engine) -> None:
    """Requirement 6.5 — at most one Decision per Recommendation Revision."""
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_source_document(conn)
        _seed_document_revision(conn)
        _seed_recommendation(conn)
        _seed_decision(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    """
                    INSERT INTO Decisions
                        (decision_id, target_recommendation_id,
                         target_recommendation_revision_id, outcome, rationale,
                         deciding_party_id, authority_basis_type,
                         authority_basis_id, applicable_scope, recorded_at)
                    VALUES (:did2, :rid, :rrev, 'Reject', 'Second.', :pid,
                            'role-grant-id', :abid, 'scope-1', :ts)
                    """
                ),
                {
                    "did2": "00000000-0000-7000-8000-0000000000ee",
                    "rid": _REC_ID,
                    "rrev": _REC_REV_ID,
                    "pid": _PARTY_ID,
                    "abid": _AUTHORITY_BASIS_ID,
                    "ts": _TS,
                },
            )


# ---------------------------------------------------------------------------
# Inserts succeed (sanity-check no overzealous triggers block writes)
# ---------------------------------------------------------------------------


def test_inserts_into_immutable_tables_still_succeed(engine: Engine) -> None:
    """Append-only triggers must not block legitimate INSERTs."""
    create_schema(engine)
    with engine.begin() as conn:
        _seed_full_graph(conn)
    # No exception means success — every dependent INSERT made it through.


# ---------------------------------------------------------------------------
# Slice 2 additive columns (Requirements 4.5, 19.2, 19.4)
#
# Per second-walking-slice tasks.md §1.2, ``create_schema`` must extend the
# Slice 1 schema with two additive NULLable columns:
#   * Relationships.semantic_role
#   * Identifier_Registry.resource_kind
# The extension must be additive only: existing Slice 1 rows remain
# byte-equivalent, the Relationships UPDATE/DELETE triggers still fire, and
# the Identifier_Registry primary-key UNIQUE constraint on ``identifier`` is
# preserved.
# ---------------------------------------------------------------------------


def _columns_of(engine: Engine, table: str) -> dict[str, dict[str, object]]:
    """Return a ``{column_name: {info...}}`` map for ``table`` via PRAGMA."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    return {
        row[1]: {
            "type": row[2],
            "notnull": row[3],
            "dflt_value": row[4],
            "pk": row[5],
        }
        for row in rows
    }


def test_relationships_has_semantic_role_column(engine: Engine) -> None:
    """AD-WS-17 — ``Relationships.semantic_role`` is added as a NULLable TEXT column."""
    create_schema(engine)
    cols = _columns_of(engine, "Relationships")
    assert "semantic_role" in cols, "semantic_role column must exist on Relationships"
    info = cols["semantic_role"]
    assert info["type"].upper() == "TEXT"
    assert info["notnull"] == 0, "semantic_role must be NULLable"


def test_identifier_registry_has_resource_kind_column(engine: Engine) -> None:
    """AD-WS-19 — ``Identifier_Registry.resource_kind`` is a NULLable TEXT column."""
    create_schema(engine)
    cols = _columns_of(engine, "Identifier_Registry")
    assert "resource_kind" in cols, "resource_kind column must exist on Identifier_Registry"
    info = cols["resource_kind"]
    assert info["type"].upper() == "TEXT"
    assert info["notnull"] == 0, "resource_kind must be NULLable"


def test_slice2_additive_columns_are_idempotent(engine: Engine) -> None:
    """Re-running schema creation does not duplicate or error on the additive columns."""
    create_schema(engine)
    create_schema(engine)  # must not raise — ALTER guarded by PRAGMA check
    rel_cols = _columns_of(engine, "Relationships")
    idr_cols = _columns_of(engine, "Identifier_Registry")
    assert "semantic_role" in rel_cols
    assert "resource_kind" in idr_cols


def test_relationships_semantic_role_defaults_to_null_for_existing_rows(
    engine: Engine,
) -> None:
    """Requirement 19.4 — existing Slice 1 rows are byte-equivalent (new column = NULL)."""
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_source_document(conn)
        _seed_document_revision(conn)
        _seed_content_region(conn)
        _seed_region_occurrence(conn)
        _seed_finding(conn)
        _seed_relationship(conn)
    with engine.connect() as conn:
        role = conn.execute(
            text(
                "SELECT semantic_role FROM Relationships "
                "WHERE relationship_id=:r"
            ),
            {"r": _RELATIONSHIP_ID},
        ).scalar_one()
    assert role is None, "semantic_role must default to NULL for Slice 1 rows"


def test_identifier_registry_resource_kind_defaults_to_null_for_existing_rows(
    engine: Engine,
) -> None:
    """Requirement 19.4 — existing Identifier_Registry rows carry NULL resource_kind."""
    create_schema(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Identifier_Registry "
                "(identifier, kind, content_digest, issued_at) "
                "VALUES (:id, 'resource', 'digest-1', :ts)"
            ),
            {"id": "00000000-0000-7000-8000-000000000aaa", "ts": _TS},
        )
    with engine.connect() as conn:
        kind = conn.execute(
            text(
                "SELECT resource_kind FROM Identifier_Registry "
                "WHERE identifier=:id"
            ),
            {"id": "00000000-0000-7000-8000-000000000aaa"},
        ).scalar_one()
    assert kind is None, "resource_kind must default to NULL for Slice 1 rows"


def test_relationships_accepts_semantic_role_value_on_new_insert(engine: Engine) -> None:
    """AD-WS-17 — Slice 2 rows may carry ``semantic_role`` such as 'review'."""
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_source_document(conn)
        _seed_document_revision(conn)
        _seed_content_region(conn)
        _seed_region_occurrence(conn)
        _seed_finding(conn)
        new_rel_id = "00000000-0000-7000-8000-0000000000bb"
        conn.execute(
            text(
                """
                INSERT INTO Relationships
                    (relationship_id, relationship_type, source_kind, source_id,
                     source_revision_id, target_kind, target_id,
                     target_revision_id, authoring_party_id, recorded_at,
                     semantic_role)
                VALUES (:rid, 'Supports', 'finding_revision', :fid, :frev,
                        'region_occurrence', :reg, :doc, :pid, :ts, 'review')
                """
            ),
            {
                "rid": new_rel_id,
                "fid": _FINDING_ID,
                "frev": _FINDING_REV_ID,
                "reg": _REGION_ID,
                "doc": _REVISION_ID,
                "pid": _PARTY_ID,
                "ts": _TS,
            },
        )
    with engine.connect() as conn:
        role = conn.execute(
            text(
                "SELECT semantic_role FROM Relationships "
                "WHERE relationship_id=:r"
            ),
            {"r": "00000000-0000-7000-8000-0000000000bb"},
        ).scalar_one()
    assert role == "review"


def test_identifier_registry_accepts_resource_kind_on_new_insert(engine: Engine) -> None:
    """Requirement 4.5 — Slice 2 rows may tag identifiers with a resource_kind."""
    create_schema(engine)
    new_id = "00000000-0000-7000-8000-0000000000cc"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Identifier_Registry "
                "(identifier, kind, content_digest, issued_at, resource_kind) "
                "VALUES (:id, 'resource', 'digest-2', :ts, 'project')"
            ),
            {"id": new_id, "ts": _TS},
        )
    with engine.connect() as conn:
        kind = conn.execute(
            text(
                "SELECT resource_kind FROM Identifier_Registry "
                "WHERE identifier=:id"
            ),
            {"id": new_id},
        ).scalar_one()
    assert kind == "project"


def test_relationships_update_trigger_unchanged_for_semantic_role(engine: Engine) -> None:
    """Task 1.2 — Relationships UPDATE trigger continues to reject every UPDATE,
    including attempts to mutate the new ``semantic_role`` column."""
    create_schema(engine)
    with engine.begin() as conn:
        _seed_party(conn)
        _seed_source_document(conn)
        _seed_document_revision(conn)
        _seed_content_region(conn)
        _seed_region_occurrence(conn)
        _seed_finding(conn)
        _seed_relationship(conn)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Relationships SET semantic_role='review' "
                    "WHERE relationship_id=:r"
                ),
                {"r": _RELATIONSHIP_ID},
            )


def test_identifier_registry_unique_constraint_on_identifier_preserved(
    engine: Engine,
) -> None:
    """Task 1.2 — the Identifier_Registry UNIQUE index on ``identifier``
    (its primary key) is unchanged after the additive ``resource_kind`` column."""
    create_schema(engine)
    dup_id = "00000000-0000-7000-8000-0000000000dd"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO Identifier_Registry "
                "(identifier, kind, content_digest, issued_at, resource_kind) "
                "VALUES (:id, 'resource', 'digest-3', :ts, 'project')"
            ),
            {"id": dup_id, "ts": _TS},
        )
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "INSERT INTO Identifier_Registry "
                    "(identifier, kind, content_digest, issued_at, resource_kind) "
                    "VALUES (:id, 'resource', 'digest-4', :ts, 'activity_plan')"
                ),
                {"id": dup_id, "ts": _TS},
            )
