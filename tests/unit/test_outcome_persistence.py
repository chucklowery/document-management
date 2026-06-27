"""Unit tests for :mod:`walking_slice.outcome._persistence` (fourth-walking-slice
task 1.4).

These tests pin the contract established in
``.kiro/specs/fourth-walking-slice/design.md`` §"Data Models — Schema
Additions", AD-WS-36 (Slice 4 Resources/Revisions/Records are append-only
with no supersession path; Observed Outcome Revisions form an explicit
predecessor chain), AD-WS-37 (per-kind tables with append-only triggers),
AD-WS-38 (origin / source-system-authority enumeration columns), and
AD-WS-39 (idempotency key for imported Measurement Records):

- Every Slice 4 Outcome_Service table in
  :data:`walking_slice.outcome._persistence.OUTCOME_IMMUTABLE_TABLES`
  rejects ``UPDATE`` and ``DELETE`` (Requirements 47.7, 48.6, 49.7, 57.3).
- ``Measurement_Records`` enforces the native-vs-imported source-system
  attribute CHECK: a native row carrying any source-system attribute is
  rejected, an imported row missing any source-system attribute is
  rejected (Requirements 45.3, 46.2, 46.4).
- ``Measurement_Records`` rejects a ``source_system_authority`` outside
  the enumerated set ``{authoritative, replica, projection, index,
  federation}`` (AD-WS-38 / Requirement 46.4).
- The AD-WS-39 partial unique index
  ``idx_measurement_records_import_idempotency`` rejects a duplicate
  imported ``(source_system_id, source_system_record_id)`` pair per
  Measurement Definition Revision (Requirement 46.3).
- The ``Observed_Outcome_Revisions`` partial unique index
  ``idx_oo_revisions_one_successor`` rejects a second successor per
  predecessor, keeping the chain linear (AD-WS-36 / Requirement 47).
- The Unassessable ``length >= 200`` rationale CHECK fires on
  ``Success_Condition_Assessment_Records`` (Requirement 48.3).
- The Asserted/Contradicted non-empty attribution-evidence CHECK fires
  on ``Outcome_Review_Records`` (Requirement 49.4).
- The Slice 1 schema co-exists with the Slice 4 schema in one SQLite
  file (Requirement 60.1, 60.3 — no prior-slice table is mutated by
  schema creation).

The disclosure-policy coverage rows visible via
``walking_slice.disclosure.policy_for`` and the byte-equivalence of the
prior-slice disclosure rows after Slice 4 seeding are covered by
:mod:`tests.unit.test_outcome_disclosure` (task 1.3) and are not
duplicated here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.outcome._persistence import (
    OUTCOME_IMMUTABLE_TABLES,
    OUTCOME_SCHEMA_STATEMENTS,
    create_outcome_schema,
)
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixed identifiers — predictable seed contents per test.
# ---------------------------------------------------------------------------


_PARTY_A = "00000000-0000-7000-8000-000000000a01"
_PARTY_B = "00000000-0000-7000-8000-000000000a02"

_INTENDED_OUTCOME_RESOURCE_ID = "00000000-0000-7000-8000-000000000b01"
_INTENDED_OUTCOME_REVISION_ID = "00000000-0000-7000-8000-000000000b02"

_MEASUREMENT_DEFINITION_ID = "00000000-0000-7000-8000-000000000c01"
_MEASUREMENT_DEFINITION_REVISION_ID = "00000000-0000-7000-8000-000000000c02"

_MEASUREMENT_RECORD_ID = "00000000-0000-7000-8000-000000000d01"
_MEASUREMENT_RECORD_ID_2 = "00000000-0000-7000-8000-000000000d02"

_OBSERVED_OUTCOME_ID = "00000000-0000-7000-8000-000000000e01"
_OO_REVISION_ID = "00000000-0000-7000-8000-000000000e02"
_OO_REVISION_ID_2 = "00000000-0000-7000-8000-000000000e03"
_OO_REVISION_ID_3 = "00000000-0000-7000-8000-000000000e04"

_ASSESSMENT_ID = "00000000-0000-7000-8000-000000000f01"
_OUTCOME_REVIEW_ID = "00000000-0000-7000-8000-000000000f02"

_AUTHORITY_BASIS_ID = "00000000-0000-7000-8000-0000000000a0"
_SCOPE = "pilot/team-a"

# Ordered timestamps (lexicographic order == chronological order).
_TS_OBSERVE = "2026-01-01T00:00:00.000+00:00"
_TS_RETRIEVE = "2026-01-01T01:00:00.000+00:00"
_TS_RECORDED = "2026-01-01T02:00:00.000+00:00"


# ---------------------------------------------------------------------------
# Schema fixture (Slice 1 + Slice 4).
#
# Slice 4 Outcome_Service tables only carry SQL FOREIGN KEYs to the Slice 1
# ``Parties`` table and to each other (prior-slice identity references are
# validated in application code per AD-WS-40), so the Slice 1 schema plus
# the Slice 4 schema is the minimal set this test needs.
# ---------------------------------------------------------------------------


@pytest.fixture
def outcome_engine(engine: Engine) -> Engine:
    """Per-test engine carrying the Slice 1 and Slice 4 schemas."""
    create_schema(engine)
    create_outcome_schema(engine)
    return engine


# ---------------------------------------------------------------------------
# Seed helpers.
# ---------------------------------------------------------------------------


def _seed_parties(conn) -> None:
    for party_id, display in ((_PARTY_A, "Authoring Party"), (_PARTY_B, "Assessing Party")):
        conn.execute(
            text(
                """
                INSERT INTO Parties (party_id, kind, display_name, created_at)
                VALUES (:pid, 'person', :name, :ts)
                """
            ),
            {"pid": party_id, "name": display, "ts": _TS_OBSERVE},
        )


def _seed_measurement_definition(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Measurement_Definitions (
                measurement_definition_id, target_intended_outcome_resource_id, created_at
            ) VALUES (:mdid, :ioid, :ts)
            """
        ),
        {
            "mdid": _MEASUREMENT_DEFINITION_ID,
            "ioid": _INTENDED_OUTCOME_RESOURCE_ID,
            "ts": _TS_OBSERVE,
        },
    )
    conn.execute(
        text(
            """
            INSERT INTO Measurement_Definition_Revisions (
                measurement_definition_revision_id, measurement_definition_id,
                target_intended_outcome_resource_id, target_intended_outcome_revision_id,
                measurand_description, unit_of_measure, observation_window,
                cadence, data_source, authoring_party_id, applicable_scope, recorded_at
            ) VALUES (
                :mdrev, :mdid, :ioid, :iorev,
                'Adoption rate of the new workflow.', 'percent', 'Q1 2026',
                'monthly', 'product analytics', :party, :scope, :ts
            )
            """
        ),
        {
            "mdrev": _MEASUREMENT_DEFINITION_REVISION_ID,
            "mdid": _MEASUREMENT_DEFINITION_ID,
            "ioid": _INTENDED_OUTCOME_RESOURCE_ID,
            "iorev": _INTENDED_OUTCOME_REVISION_ID,
            "party": _PARTY_A,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


_NATIVE_COLUMNS = (
    "measurement_record_id, target_measurement_definition_id, "
    "target_measurement_definition_revision_id, origin, observed_value, "
    "observed_value_unit, observation_time, source_system_id, "
    "source_system_record_id, source_system_authority, "
    "source_system_retrieval_at, import_at, recording_party_id, "
    "applicable_scope, recorded_at"
)


def _native_params(**overrides) -> dict:
    params = {
        "measurement_record_id": _MEASUREMENT_RECORD_ID,
        "target_measurement_definition_id": _MEASUREMENT_DEFINITION_ID,
        "target_measurement_definition_revision_id": _MEASUREMENT_DEFINITION_REVISION_ID,
        "origin": "native",
        "observed_value": "42.5",
        "observed_value_unit": "percent",
        "observation_time": _TS_OBSERVE,
        "source_system_id": None,
        "source_system_record_id": None,
        "source_system_authority": None,
        "source_system_retrieval_at": None,
        "import_at": None,
        "recording_party_id": _PARTY_A,
        "applicable_scope": _SCOPE,
        "recorded_at": _TS_RECORDED,
    }
    params.update(overrides)
    return params


def _imported_params(**overrides) -> dict:
    params = {
        "measurement_record_id": _MEASUREMENT_RECORD_ID,
        "target_measurement_definition_id": _MEASUREMENT_DEFINITION_ID,
        "target_measurement_definition_revision_id": _MEASUREMENT_DEFINITION_REVISION_ID,
        "origin": "imported",
        "observed_value": "42.5",
        "observed_value_unit": "percent",
        "observation_time": _TS_OBSERVE,
        "source_system_id": "crm-system",
        "source_system_record_id": "rec-001",
        "source_system_authority": "authoritative",
        "source_system_retrieval_at": _TS_RETRIEVE,
        "import_at": _TS_RECORDED,
        "recording_party_id": _PARTY_A,
        "applicable_scope": _SCOPE,
        "recorded_at": _TS_RECORDED,
    }
    params.update(overrides)
    return params


def _insert_measurement_record(conn, params: dict) -> None:
    conn.execute(
        text(
            f"""
            INSERT INTO Measurement_Records ({_NATIVE_COLUMNS})
            VALUES (
                :measurement_record_id, :target_measurement_definition_id,
                :target_measurement_definition_revision_id, :origin, :observed_value,
                :observed_value_unit, :observation_time, :source_system_id,
                :source_system_record_id, :source_system_authority,
                :source_system_retrieval_at, :import_at, :recording_party_id,
                :applicable_scope, :recorded_at
            )
            """
        ),
        params,
    )


def _seed_observed_outcome(conn, *, with_initial_revision: bool = True) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Observed_Outcomes (
                observed_outcome_id, target_intended_outcome_resource_id, created_at
            ) VALUES (:ooid, :ioid, :ts)
            """
        ),
        {"ooid": _OBSERVED_OUTCOME_ID, "ioid": _INTENDED_OUTCOME_RESOURCE_ID, "ts": _TS_OBSERVE},
    )
    if with_initial_revision:
        _insert_oo_revision(conn, _OO_REVISION_ID, predecessor=None)


def _insert_oo_revision(conn, revision_id: str, *, predecessor: str | None) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Observed_Outcome_Revisions (
                observed_outcome_revision_id, observed_outcome_id, outcome_kind,
                target_intended_outcome_resource_id, target_intended_outcome_revision_id,
                assessment_summary, predecessor_revision_id, authoring_party_id,
                applicable_scope, recorded_at
            ) VALUES (
                :oorev, :ooid, 'observed', :ioid, :iorev,
                'Workflow adoption observed at 42.5 percent.', :pred, :party,
                :scope, :ts
            )
            """
        ),
        {
            "oorev": revision_id,
            "ooid": _OBSERVED_OUTCOME_ID,
            "ioid": _INTENDED_OUTCOME_RESOURCE_ID,
            "iorev": _INTENDED_OUTCOME_REVISION_ID,
            "pred": predecessor,
            "party": _PARTY_A,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_assessment(conn, *, category: str = "Satisfied", rationale: str = "Goal met.") -> None:
    conn.execute(
        text(
            """
            INSERT INTO Success_Condition_Assessment_Records (
                assessment_id, target_intended_outcome_resource_id,
                target_intended_outcome_revision_id, sourced_observed_outcome_id,
                sourced_observed_outcome_revision_id, assessment_category,
                assessment_rationale, assessing_party_id, authority_basis_type,
                authority_basis_id, applicable_scope, recorded_at
            ) VALUES (
                :aid, :ioid, :iorev, :ooid, :oorev, :cat, :rationale,
                :party, 'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "aid": _ASSESSMENT_ID,
            "ioid": _INTENDED_OUTCOME_RESOURCE_ID,
            "iorev": _INTENDED_OUTCOME_REVISION_ID,
            "ooid": _OBSERVED_OUTCOME_ID,
            "oorev": _OO_REVISION_ID,
            "cat": category,
            "rationale": rationale,
            "party": _PARTY_B,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_outcome_review(
    conn,
    *,
    stance: str = "Partial",
    evidence: str = "Linked to the rollout milestone.",
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Outcome_Review_Records (
                outcome_review_id, target_intended_outcome_resource_id,
                target_intended_outcome_revision_id, review_outcome,
                attribution_stance, confidence, review_rationale,
                attribution_evidence_reference, reviewing_party_id,
                authority_basis_type, authority_basis_id, applicable_scope, recorded_at
            ) VALUES (
                :orid, :ioid, :iorev, 'Achieved', :stance, 'High',
                'Reviewed against the success conditions.', :evidence, :party,
                'role-grant-id', :abid, :scope, :ts
            )
            """
        ),
        {
            "orid": _OUTCOME_REVIEW_ID,
            "ioid": _INTENDED_OUTCOME_RESOURCE_ID,
            "iorev": _INTENDED_OUTCOME_REVISION_ID,
            "stance": stance,
            "evidence": evidence,
            "party": _PARTY_B,
            "abid": _AUTHORITY_BASIS_ID,
            "scope": _SCOPE,
            "ts": _TS_RECORDED,
        },
    )


def _seed_full_outcome_graph(conn) -> None:
    """Insert one row into every Slice 4 Outcome_Service table."""
    _seed_parties(conn)
    _seed_measurement_definition(conn)
    _insert_measurement_record(conn, _native_params())
    _seed_observed_outcome(conn)
    _seed_assessment(conn)
    _seed_outcome_review(conn)


# ---------------------------------------------------------------------------
# Schema shape sanity.
# ---------------------------------------------------------------------------


def test_immutable_tables_constant_lists_every_outcome_table() -> None:
    """``OUTCOME_IMMUTABLE_TABLES`` covers every Slice 4 Outcome_Service table.

    Per AD-WS-36 / AD-WS-37, every Slice 4 Resource, Revision, and
    Immutable Record is insert-only with no supersession path.
    """
    expected = {
        "Measurement_Definitions",
        "Measurement_Definition_Revisions",
        "Measurement_Records",
        "Observed_Outcomes",
        "Observed_Outcome_Revisions",
        "Success_Condition_Assessment_Records",
        "Outcome_Review_Records",
    }
    assert set(OUTCOME_IMMUTABLE_TABLES) == expected


def test_create_outcome_schema_is_idempotent(outcome_engine: Engine) -> None:
    """Calling ``create_outcome_schema`` twice does not raise."""
    create_outcome_schema(outcome_engine)


def test_schema_statements_listed_in_dependency_order() -> None:
    """Tables appear before indexes appear before triggers."""
    table_indices = [
        i for i, stmt in enumerate(OUTCOME_SCHEMA_STATEMENTS) if "CREATE TABLE" in stmt
    ]
    index_indices = [
        i
        for i, stmt in enumerate(OUTCOME_SCHEMA_STATEMENTS)
        if "CREATE INDEX" in stmt or "CREATE UNIQUE INDEX" in stmt
    ]
    trigger_indices = [
        i for i, stmt in enumerate(OUTCOME_SCHEMA_STATEMENTS) if "CREATE TRIGGER" in stmt
    ]
    assert max(table_indices) < min(index_indices)
    assert max(index_indices) < min(trigger_indices)


# ---------------------------------------------------------------------------
# Append-only triggers (AD-WS-36 / AD-WS-37).
# ---------------------------------------------------------------------------


_IMMUTABLE_TABLE_CASES: tuple[tuple[str, str, dict[str, str], str], ...] = (
    (
        "Measurement_Definitions",
        "UPDATE Measurement_Definitions SET created_at='x' WHERE measurement_definition_id = :id",
        {"id": _MEASUREMENT_DEFINITION_ID},
        "DELETE FROM Measurement_Definitions WHERE measurement_definition_id = :id",
    ),
    (
        "Measurement_Definition_Revisions",
        "UPDATE Measurement_Definition_Revisions SET cadence='weekly' "
        "WHERE measurement_definition_revision_id = :id",
        {"id": _MEASUREMENT_DEFINITION_REVISION_ID},
        "DELETE FROM Measurement_Definition_Revisions WHERE measurement_definition_revision_id = :id",
    ),
    (
        "Measurement_Records",
        "UPDATE Measurement_Records SET observed_value='99.9' WHERE measurement_record_id = :id",
        {"id": _MEASUREMENT_RECORD_ID},
        "DELETE FROM Measurement_Records WHERE measurement_record_id = :id",
    ),
    (
        "Observed_Outcomes",
        "UPDATE Observed_Outcomes SET created_at='x' WHERE observed_outcome_id = :id",
        {"id": _OBSERVED_OUTCOME_ID},
        "DELETE FROM Observed_Outcomes WHERE observed_outcome_id = :id",
    ),
    (
        "Observed_Outcome_Revisions",
        "UPDATE Observed_Outcome_Revisions SET assessment_summary='changed' "
        "WHERE observed_outcome_revision_id = :id",
        {"id": _OO_REVISION_ID},
        "DELETE FROM Observed_Outcome_Revisions WHERE observed_outcome_revision_id = :id",
    ),
    (
        "Success_Condition_Assessment_Records",
        "UPDATE Success_Condition_Assessment_Records SET assessment_rationale='changed' "
        "WHERE assessment_id = :id",
        {"id": _ASSESSMENT_ID},
        "DELETE FROM Success_Condition_Assessment_Records WHERE assessment_id = :id",
    ),
    (
        "Outcome_Review_Records",
        "UPDATE Outcome_Review_Records SET review_rationale='changed' "
        "WHERE outcome_review_id = :id",
        {"id": _OUTCOME_REVIEW_ID},
        "DELETE FROM Outcome_Review_Records WHERE outcome_review_id = :id",
    ),
)


@pytest.mark.parametrize("table, update_sql, params, delete_sql", _IMMUTABLE_TABLE_CASES)
def test_outcome_table_rejects_update(
    outcome_engine: Engine,
    table: str,
    update_sql: str,
    params: dict,
    delete_sql: str,
) -> None:
    """Every Slice 4 Outcome_Service table rejects UPDATE per AD-WS-36 / AD-WS-37."""
    del delete_sql
    with outcome_engine.begin() as conn:
        _seed_full_outcome_graph(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(update_sql), params)


@pytest.mark.parametrize("table, update_sql, params, delete_sql", _IMMUTABLE_TABLE_CASES)
def test_outcome_table_rejects_delete(
    outcome_engine: Engine,
    table: str,
    update_sql: str,
    params: dict,
    delete_sql: str,
) -> None:
    """Every Slice 4 Outcome_Service table rejects DELETE per AD-WS-36 / AD-WS-37."""
    del update_sql
    with outcome_engine.begin() as conn:
        _seed_full_outcome_graph(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(text(delete_sql), params)


def test_rejected_update_leaves_outcome_row_byte_equivalent(outcome_engine: Engine) -> None:
    """A rejected UPDATE must not mutate the row."""
    with outcome_engine.begin() as conn:
        _seed_full_outcome_graph(conn)

    with outcome_engine.connect() as conn:
        before = conn.execute(
            text("SELECT * FROM Measurement_Records WHERE measurement_record_id = :id"),
            {"id": _MEASUREMENT_RECORD_ID},
        ).one()

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Measurement_Records SET observed_value='99.9' "
                    "WHERE measurement_record_id = :id"
                ),
                {"id": _MEASUREMENT_RECORD_ID},
            )

    with outcome_engine.connect() as conn:
        after = conn.execute(
            text("SELECT * FROM Measurement_Records WHERE measurement_record_id = :id"),
            {"id": _MEASUREMENT_RECORD_ID},
        ).one()
    assert before == after


# ---------------------------------------------------------------------------
# Measurement_Records native-vs-imported source-system CHECK
# (Requirements 45.3, 46.2, 46.4).
# ---------------------------------------------------------------------------


def test_native_measurement_record_accepts_all_source_system_columns_null(
    outcome_engine: Engine,
) -> None:
    """A native row with every source-system column NULL is accepted (Requirement 45.3)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)
        _insert_measurement_record(conn, _native_params())


@pytest.mark.parametrize(
    "attribute, value",
    [
        ("source_system_id", "crm-system"),
        ("source_system_record_id", "rec-001"),
        ("source_system_authority", "authoritative"),
        ("source_system_retrieval_at", _TS_RETRIEVE),
        ("import_at", _TS_RECORDED),
    ],
)
def test_native_measurement_record_rejects_any_source_system_attribute(
    outcome_engine: Engine, attribute: str, value: str
) -> None:
    """A native row carrying any single source-system attribute is rejected.

    Requirement 45.3 — native Measurement Records carry no source-system
    attributes; the table-level CHECK keyed on ``origin = 'native'``
    enforces every source-system column is NULL.
    """
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _insert_measurement_record(conn, _native_params(**{attribute: value}))


def test_imported_measurement_record_accepts_full_source_system_payload(
    outcome_engine: Engine,
) -> None:
    """An imported row carrying every source-system attribute is accepted (Requirement 46.2)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)
        _insert_measurement_record(conn, _imported_params())


@pytest.mark.parametrize(
    "attribute",
    [
        "source_system_id",
        "source_system_record_id",
        "source_system_authority",
        "source_system_retrieval_at",
        "import_at",
    ],
)
def test_imported_measurement_record_rejects_missing_source_system_attribute(
    outcome_engine: Engine, attribute: str
) -> None:
    """An imported row missing any source-system attribute is rejected.

    Requirement 46.2 / 46.4 — imported Measurement Records require all
    source-system attributes non-null; the authority designation in
    particular is never defaulted to ``authoritative`` (a NULL value is
    rejected rather than silently filled).
    """
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _insert_measurement_record(conn, _imported_params(**{attribute: None}))


@pytest.mark.parametrize(
    "authority",
    ["authoritative", "replica", "projection", "index", "federation"],
)
def test_imported_measurement_record_accepts_enumerated_authority(
    outcome_engine: Engine, authority: str
) -> None:
    """Each enumerated ``source_system_authority`` value is accepted (AD-WS-38)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)
        _insert_measurement_record(conn, _imported_params(source_system_authority=authority))


@pytest.mark.parametrize("bad_authority", ["primary", "secondary", "AUTHORITATIVE", "", "cache"])
def test_imported_measurement_record_rejects_authority_outside_enumeration(
    outcome_engine: Engine, bad_authority: str
) -> None:
    """A ``source_system_authority`` outside the enumerated set is rejected (Requirement 46.4)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _insert_measurement_record(
                conn, _imported_params(source_system_authority=bad_authority)
            )


# ---------------------------------------------------------------------------
# AD-WS-39 import idempotency index (Requirement 46.3).
# ---------------------------------------------------------------------------


def test_duplicate_imported_pair_rejected_per_definition_revision(
    outcome_engine: Engine,
) -> None:
    """A second imported Record with the same source pair per Definition Revision is rejected.

    AD-WS-39 / Requirement 46.3 — the partial unique index
    ``idx_measurement_records_import_idempotency`` scoped
    ``WHERE origin = 'imported'`` rejects a duplicate
    ``(target_measurement_definition_revision_id, source_system_id,
    source_system_record_id)`` triple.
    """
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)
        _insert_measurement_record(conn, _imported_params())

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _insert_measurement_record(
                conn,
                _imported_params(measurement_record_id=_MEASUREMENT_RECORD_ID_2),
            )


def test_distinct_source_record_id_allows_second_imported_record(
    outcome_engine: Engine,
) -> None:
    """A different ``source_system_record_id`` is not a duplicate (idempotency scope)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)
        _insert_measurement_record(conn, _imported_params())
        _insert_measurement_record(
            conn,
            _imported_params(
                measurement_record_id=_MEASUREMENT_RECORD_ID_2,
                source_system_record_id="rec-002",
            ),
        )


def test_native_records_unaffected_by_import_idempotency_index(
    outcome_engine: Engine,
) -> None:
    """Two native Records (no source attributes) coexist; the partial index is imported-only."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_measurement_definition(conn)
        _insert_measurement_record(conn, _native_params())
        _insert_measurement_record(
            conn, _native_params(measurement_record_id=_MEASUREMENT_RECORD_ID_2)
        )


# ---------------------------------------------------------------------------
# Observed_Outcome_Revisions linear-chain index (AD-WS-36 / Requirement 47).
# ---------------------------------------------------------------------------


def test_observed_outcome_revision_chain_allows_linear_successors(
    outcome_engine: Engine,
) -> None:
    """A linear predecessor chain (rev1 → rev2 → rev3) is accepted."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_observed_outcome(conn)  # rev1 with predecessor=None
        _insert_oo_revision(conn, _OO_REVISION_ID_2, predecessor=_OO_REVISION_ID)
        _insert_oo_revision(conn, _OO_REVISION_ID_3, predecessor=_OO_REVISION_ID_2)


def test_observed_outcome_revision_rejects_second_successor_per_predecessor(
    outcome_engine: Engine,
) -> None:
    """A second Revision naming the same predecessor is rejected.

    AD-WS-36 / Requirement 47 — ``idx_oo_revisions_one_successor`` keeps
    the chain linear: at most one successor per predecessor (no forking).
    """
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_observed_outcome(conn)  # rev1
        _insert_oo_revision(conn, _OO_REVISION_ID_2, predecessor=_OO_REVISION_ID)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _insert_oo_revision(conn, _OO_REVISION_ID_3, predecessor=_OO_REVISION_ID)


def test_observed_outcome_revision_rejects_non_observed_outcome_kind(
    outcome_engine: Engine,
) -> None:
    """``outcome_kind`` other than ``observed`` is rejected (§7.4 invariant 6)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_observed_outcome(conn, with_initial_revision=False)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    """
                    INSERT INTO Observed_Outcome_Revisions (
                        observed_outcome_revision_id, observed_outcome_id, outcome_kind,
                        target_intended_outcome_resource_id, target_intended_outcome_revision_id,
                        assessment_summary, predecessor_revision_id, authoring_party_id,
                        applicable_scope, recorded_at
                    ) VALUES (
                        :oorev, :ooid, 'intended', :ioid, :iorev,
                        'Mislabelled as intended.', NULL, :party, :scope, :ts
                    )
                    """
                ),
                {
                    "oorev": _OO_REVISION_ID,
                    "ooid": _OBSERVED_OUTCOME_ID,
                    "ioid": _INTENDED_OUTCOME_RESOURCE_ID,
                    "iorev": _INTENDED_OUTCOME_REVISION_ID,
                    "party": _PARTY_A,
                    "scope": _SCOPE,
                    "ts": _TS_RECORDED,
                },
            )


# ---------------------------------------------------------------------------
# Success_Condition_Assessment_Records Unassessable >= 200 CHECK (Req 48.3).
# ---------------------------------------------------------------------------


def test_unassessable_assessment_accepts_rationale_at_200_chars(
    outcome_engine: Engine,
) -> None:
    """An Unassessable assessment with a 200-char rationale is accepted."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_observed_outcome(conn)
        _seed_assessment(conn, category="Unassessable", rationale="x" * 200)


def test_unassessable_assessment_rejects_rationale_below_200_chars(
    outcome_engine: Engine,
) -> None:
    """An Unassessable assessment with a 199-char rationale is rejected (Requirement 48.3)."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_observed_outcome(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_assessment(conn, category="Unassessable", rationale="x" * 199)


def test_non_unassessable_assessment_accepts_short_rationale(
    outcome_engine: Engine,
) -> None:
    """A Satisfied assessment is not bound by the >= 200-char rule."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_observed_outcome(conn)
        _seed_assessment(conn, category="Satisfied", rationale="Goal met.")


# ---------------------------------------------------------------------------
# Outcome_Review_Records Asserted/Contradicted attribution-evidence CHECK
# (Requirement 49.4).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stance", ["Asserted", "Contradicted"])
def test_outcome_review_rejects_empty_attribution_evidence_for_strong_stance(
    outcome_engine: Engine, stance: str
) -> None:
    """An Asserted/Contradicted review with empty attribution evidence is rejected.

    Requirement 49.4 — when the attribution stance is ``Asserted`` or
    ``Contradicted`` the attribution-evidence reference must be non-empty;
    the trailing CHECK fires on an empty string.
    """
    with outcome_engine.begin() as conn:
        _seed_parties(conn)

    with outcome_engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            _seed_outcome_review(conn, stance=stance, evidence="")


@pytest.mark.parametrize("stance", ["Asserted", "Contradicted"])
def test_outcome_review_accepts_non_empty_attribution_evidence_for_strong_stance(
    outcome_engine: Engine, stance: str
) -> None:
    """An Asserted/Contradicted review with a non-empty reference is accepted."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_outcome_review(conn, stance=stance, evidence="Linked to the rollout milestone.")


@pytest.mark.parametrize("stance", ["Partial", "Unattributed"])
def test_outcome_review_allows_empty_attribution_evidence_for_weak_stance(
    outcome_engine: Engine, stance: str
) -> None:
    """Partial / Unattributed reviews may carry empty attribution evidence."""
    with outcome_engine.begin() as conn:
        _seed_parties(conn)
        _seed_outcome_review(conn, stance=stance, evidence="")


# ---------------------------------------------------------------------------
# Prior-slice co-existence (Requirement 60.1, 60.3).
# ---------------------------------------------------------------------------


def test_slice_1_tables_present_alongside_slice_4_tables(outcome_engine: Engine) -> None:
    """The Slice 1 schema co-exists with the Slice 4 schema in one file."""
    with outcome_engine.connect() as conn:
        names = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).all()
        }
    # A representative Slice 1 table and every Slice 4 table are present.
    assert "Parties" in names
    assert "Audit_Records" in names
    for table in OUTCOME_IMMUTABLE_TABLES:
        assert table in names
