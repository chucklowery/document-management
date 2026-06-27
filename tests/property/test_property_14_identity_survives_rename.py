# Feature: first-walking-slice, Property 14: Identity survives rename and relocation
"""Property 14 — Identity survives rename and relocation (task 5.6).

**Property 14: Identity survives rename and relocation**

For all Source Document Resources, for any sequence of rename and
relocation operations applied to that Resource, the Resource Identity
and every existing Document Revision Identity remain byte-equivalent
to their pre-operation values, and no new Resource Identity or
Revision Identity is generated.

**Validates: Requirements 1.3**

Strategy:

Hypothesis draws an initial Source Document (content bytes, initial
location), then a sequence of mixed operations against that Document:

- ``rename`` — change ``Source_Documents.current_location`` to a new
  display path (or ``None`` to clear it). Requirement 1.3 forbids
  changing the Resource Identity or any existing Revision Identity
  across a rename.
- ``append_revision`` — append a new immutable Document Revision with
  fresh content. The new Revision gets a freshly issued
  ``revision_id``; every previously issued ``revision_id`` for the
  same ``resource_id`` must remain byte-equivalent and present in
  ``Document_Revisions``.

Each Hypothesis case spins up a fresh per-test SQLite engine + schema
so cross-case state cannot contaminate the byte-equivalence check.
After every operation the test asserts:

1. The ``Source_Documents`` row for ``resource_id`` still exists
   (Requirement 1.3 — rename never deletes the Source Document).
2. ``resource_id`` is byte-equivalent to the initial value returned
   by ``create_document`` (no new Resource Identity was generated).
3. Every ``revision_id`` ever issued for this Resource is still
   present in ``Document_Revisions`` and byte-equivalent across the
   operation sequence (no Revision was deleted, mutated, or
   re-keyed).

The test deliberately uses the public :class:`EvidenceRepository`
surface (``create_document``, ``append_revision``, ``rename_document``)
rather than reaching into the database directly so a regression in
either the rename or the append path surfaces as a property violation.
"""

from __future__ import annotations

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
from walking_slice.evidence import EvidenceRepository
from walking_slice.identity import IdentityService
from walking_slice.persistence import create_schema


pytestmark = pytest.mark.property


# ---------------------------------------------------------------------------
# Seed constants — the Parties row required by the
# ``Document_Revisions.contributing_party_id`` and
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
            VALUES (:pid, 'person', 'Property 14 Party', :ts)
            """
        ),
        {"pid": _PARTY_ID, "ts": _PARTY_CREATED_AT},
    )


# ---------------------------------------------------------------------------
# Hypothesis strategies.
#
# - ``_content_strategy`` draws between 1 byte and 1 KB of arbitrary bytes,
#   exercising both small spans and modest payloads without making each
#   Hypothesis case prohibitively slow (Requirement 2.1 caps Document
#   Revision content at 100 MB, but property runs do not need to probe
#   that ceiling).
# - ``_location_strategy`` draws either ``None`` (to exercise the cleared
#   ``current_location`` path) or a short display-path-like string.
#   ``Source_Documents.current_location`` is nullable in the schema and
#   has no length constraint in this slice (task 5.4 will enforce length
#   at the HTTP boundary).
# - ``_rename_op`` / ``_append_op`` together compose the operation
#   sequence; ``_operations_strategy`` draws 0..16 operations per case so
#   shrinking can produce small counterexamples while still exercising
#   long sequences when Hypothesis explores them.
# ---------------------------------------------------------------------------


_content_strategy = st.binary(min_size=1, max_size=1024)
_location_strategy = st.one_of(
    st.none(),
    st.text(min_size=1, max_size=64),
)


def _rename_op() -> st.SearchStrategy[tuple[str, object]]:
    """Strategy for a rename operation: ``("rename", new_location)``."""
    return st.tuples(st.just("rename"), _location_strategy)


def _append_op() -> st.SearchStrategy[tuple[str, bytes]]:
    """Strategy for an append-revision operation: ``("append", content)``."""
    return st.tuples(st.just("append"), _content_strategy)


_operations_strategy = st.lists(
    st.one_of(_rename_op(), _append_op()),
    min_size=0,
    max_size=16,
)


# ---------------------------------------------------------------------------
# Per-case engine helper.
#
# Each Hypothesis case builds a fresh SQLite engine on a unique temp-dir
# path so cross-case identifiers, audit rows, and Source_Documents rows
# cannot leak between cases (design §"Testing Strategy" — "Each property
# and example test gets a fresh SQLite database"). A
# :class:`tempfile.TemporaryDirectory` context inside the test body owns
# the per-case directory; Hypothesis disallows function-scoped pytest
# fixtures for per-case state because they would not reset between
# generated inputs.
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
# Database probe helpers used inside the assertion loop.
# ---------------------------------------------------------------------------


def _fetch_resource_id(engine: Engine, resource_id: str) -> str | None:
    """Return the ``resource_id`` row value, or ``None`` if the row is gone."""
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT resource_id FROM Source_Documents "
                "WHERE resource_id = :rid"
            ),
            {"rid": resource_id},
        ).scalar_one_or_none()


def _fetch_revision_ids(engine: Engine, resource_id: str) -> set[str]:
    """Return every ``revision_id`` currently present for ``resource_id``."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT revision_id FROM Document_Revisions "
                "WHERE resource_id = :rid"
            ),
            {"rid": resource_id},
        ).all()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# The property test.
# ---------------------------------------------------------------------------


# Feature: first-walking-slice, Property 14: Identity survives rename and relocation
@given(
    initial_content=_content_strategy,
    initial_location=_location_strategy,
    operations=_operations_strategy,
)
@settings(max_examples=100, deadline=5000)
def test_identity_survives_rename_and_relocation(
    initial_content: bytes,
    initial_location: str | None,
    operations: list[tuple[str, object]],
) -> None:
    """After every rename or append_revision operation, ``resource_id`` and
    every previously issued ``revision_id`` remain byte-equivalent."""
    # A fresh on-disk SQLite file per case prevents cross-case leakage of
    # identifiers, audit rows, and Source_Documents rows. Using
    # :class:`tempfile.TemporaryDirectory` (rather than a pytest fixture)
    # is the pattern Hypothesis recommends for per-case state because
    # function-scoped fixtures are not reset between generated inputs.
    with tempfile.TemporaryDirectory(prefix="walking_slice_prop14_") as raw_tmp:
        case_dir = Path(raw_tmp)
        engine = _build_engine(case_dir)

        # Fresh services per case so the ``IdentityService`` in-memory state
        # cannot bleed across cases — the property is about a single Source
        # Document's identity surviving its own operation sequence, not about
        # any global cross-case property.
        clock = FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        identity_service = IdentityService()
        audit_log = AuditLog(clock)
        repository = EvidenceRepository(
            clock=clock,
            identity_service=identity_service,
            audit_log=audit_log,
        )

        try:
            # Seed the Party and create the initial Source Document inside
            # one transaction. The Resource Identity issued here is the
            # invariant ``resource_id`` we track for the remainder of the
            # case.
            with engine.begin() as conn:
                _seed_party(conn)
                initial = repository.create_document(
                    conn,
                    content_bytes=initial_content,
                    contributing_party_id=_PARTY_ID,
                    authority="authoritative",
                    current_location=initial_location,
                )

            initial_resource_id = initial.resource_id
            # Track every ``revision_id`` ever issued for this Resource.
            # The initial Document Revision is the first entry;
            # ``append_revision`` operations append further entries.
            # Renames never add an entry (renames don't touch
            # ``Document_Revisions``) but must leave the set
            # byte-equivalent.
            issued_revision_ids: set[str] = {initial.revision_id}

            # Baseline assertion before any operations: the
            # ``Source_Documents`` row exists and the initial Revision is
            # present. This guards against a regression where
            # ``create_document`` silently failed to persist either row.
            assert (
                _fetch_resource_id(engine, initial_resource_id)
                == initial_resource_id
            )
            assert (
                _fetch_revision_ids(engine, initial_resource_id)
                == issued_revision_ids
            )

            # Run each operation and re-check the three invariants after
            # it.
            for op_index, op in enumerate(operations):
                op_kind = op[0]
                if op_kind == "rename":
                    new_location = op[1]
                    with engine.begin() as conn:
                        result = repository.rename_document(
                            conn,
                            resource_id=initial_resource_id,
                            # ``new_location`` may be ``None`` (clear
                            # path) or a short display-path string.
                            new_current_location=new_location,  # type: ignore[arg-type]
                            actor_party_id=_PARTY_ID,
                        )
                    # The rename result echoes the unchanged Resource
                    # Identity; this is the strongest expression of
                    # Requirement 1.3 at the API boundary.
                    assert result.resource_id == initial_resource_id, (
                        "rename_document returned a different "
                        f"resource_id at operation {op_index}: expected "
                        f"{initial_resource_id!r}, got "
                        f"{result.resource_id!r}."
                    )
                elif op_kind == "append":
                    content_bytes = op[1]
                    with engine.begin() as conn:
                        appended = repository.append_revision(
                            conn,
                            resource_id=initial_resource_id,
                            content_bytes=content_bytes,  # type: ignore[arg-type]
                            contributing_party_id=_PARTY_ID,
                        )
                    # ``append_revision`` issues exactly one new Revision
                    # Identity per call; record it so subsequent
                    # assertions confirm the whole set is preserved.
                    assert appended.resource_id == initial_resource_id, (
                        "append_revision returned a different "
                        f"resource_id at operation {op_index}: expected "
                        f"{initial_resource_id!r}, got "
                        f"{appended.resource_id!r}."
                    )
                    assert appended.revision_id not in issued_revision_ids, (
                        "append_revision reissued an existing revision_id "
                        f"at operation {op_index}: "
                        f"{appended.revision_id!r}."
                    )
                    issued_revision_ids.add(appended.revision_id)
                else:  # pragma: no cover - defensive; the strategy emits
                    # only ("rename", ...) or ("append", ...) tuples.
                    raise AssertionError(f"Unknown operation kind: {op_kind!r}")

                # --- Invariants after every operation -------------------

                # 1. Source_Documents row for resource_id still exists.
                persisted_resource_id = _fetch_resource_id(
                    engine, initial_resource_id
                )
                assert persisted_resource_id is not None, (
                    f"Source_Documents row for {initial_resource_id!r} "
                    f"disappeared after operation {op_index} "
                    f"({op_kind!r}); Requirement 1.3 forbids deletion "
                    "across rename/relocate."
                )

                # 2. resource_id is byte-equivalent to the initial value.
                assert persisted_resource_id == initial_resource_id, (
                    "Source_Documents.resource_id changed after "
                    f"operation {op_index} ({op_kind!r}): expected "
                    f"{initial_resource_id!r}, found "
                    f"{persisted_resource_id!r}. Requirement 1.3 demands "
                    "byte-equivalence."
                )

                # 3. Every revision_id ever issued is still present.
                current_revision_ids = _fetch_revision_ids(
                    engine, initial_resource_id
                )
                missing = issued_revision_ids - current_revision_ids
                assert not missing, (
                    "Document_Revisions lost revision_id(s) "
                    f"{sorted(missing)!r} after operation {op_index} "
                    f"({op_kind!r}); Requirement 1.3 requires every "
                    "existing Revision Identity to survive."
                )
                # No extra ``revision_id`` should have appeared for this
                # Resource — only ``append_revision`` adds Revisions,
                # and the newly added id is already in
                # ``issued_revision_ids`` by the time this assertion
                # runs.
                unexpected = current_revision_ids - issued_revision_ids
                assert not unexpected, (
                    "Document_Revisions gained unexpected revision_id(s) "
                    f"{sorted(unexpected)!r} after operation "
                    f"{op_index} ({op_kind!r}); only append_revision "
                    "should introduce new Revisions."
                )
        finally:
            engine.dispose()
