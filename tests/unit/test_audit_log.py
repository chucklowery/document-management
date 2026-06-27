"""Unit tests for :mod:`walking_slice.audit`.

These tests pin the contract established in
``.kiro/specs/first-walking-slice/design.md`` §"Audit_Log" and the task 3.1
description:

- ``append_consequential`` and ``append_denial`` both append an immutable
  ``Audit_Records`` row that participates in the caller's transaction
  (AD-WS-5; Requirements 13.1, 13.2, 13.6).
- ``append_sequence`` is monotonically increasing across appends
  (Requirement 13.4).
- ``recorded_at`` is UTC ISO-8601 text with millisecond precision sourced
  from the injected :class:`~walking_slice.clock.Clock` (Requirement 13.1).
- Insert failure (e.g. missing FK on ``actor_party_id``) raises
  :class:`~walking_slice.audit.AuditAppendError` so the surrounding
  transaction rolls back (Requirements 2.7, 13.6).
- The append-only triggers from :mod:`walking_slice.persistence` reject
  every ``UPDATE`` and ``DELETE`` against appended rows — reaffirming the
  contract task 1.3 establishes (Requirements 13.3, 13.5).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.audit import (
    AuditAppendError,
    AuditLog,
    AuditRecord,
    format_iso8601_ms,
)
from walking_slice.clock import FixedClock


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers — only the Parties row is needed to exercise the audit log.
# Re-creating the helpers here (rather than importing from test_persistence)
# keeps the unit suite self-contained.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_OTHER_PARTY_ID = "00000000-0000-7000-8000-000000000002"
_TARGET_ID = "00000000-0000-7000-8000-000000000010"
_TARGET_REV_ID = "00000000-0000-7000-8000-000000000011"


def _seed_party(conn, party_id: str = _PARTY_ID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Test Party', :ts)
            """
        ),
        {"pid": party_id, "ts": "2026-01-01T00:00:00.000Z"},
    )


def _fetch_audit_row(engine: Engine, audit_record_id: str) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT audit_record_id, append_sequence, actor_party_id, "
                "action_type, outcome, target_id, target_revision_id, "
                "reason_code, correlation_id, recorded_at "
                "FROM Audit_Records WHERE audit_record_id = :id"
            ),
            {"id": audit_record_id},
        ).mappings().one()
    return dict(row)


def _all_append_sequences(engine: Engine) -> list[int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT append_sequence FROM Audit_Records "
                "ORDER BY append_sequence"
            )
        ).all()
    return [int(row[0]) for row in rows]


# ---------------------------------------------------------------------------
# format_iso8601_ms
# ---------------------------------------------------------------------------


def test_format_iso8601_ms_emits_canonical_wire_format() -> None:
    value = datetime(2026, 1, 1, 12, 30, 45, 123_000, tzinfo=timezone.utc)
    assert format_iso8601_ms(value) == "2026-01-01T12:30:45.123Z"


def test_format_iso8601_ms_truncates_sub_millisecond_microseconds() -> None:
    value = datetime(2026, 1, 1, 0, 0, 0, 999_999, tzinfo=timezone.utc)
    assert format_iso8601_ms(value) == "2026-01-01T00:00:00.999Z"


def test_format_iso8601_ms_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        format_iso8601_ms(datetime(2026, 1, 1))  # noqa: DTZ001 — intentional


# ---------------------------------------------------------------------------
# Append semantics — happy path
# ---------------------------------------------------------------------------


def test_append_consequential_inserts_row_with_outcome_consequential(
    engine: Engine, audit_log: AuditLog
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REV_ID,
            correlation_id="corr-1",
        )

    assert isinstance(record, AuditRecord)
    assert record.append_sequence == 1

    row = _fetch_audit_row(engine, record.audit_record_id)
    assert row["actor_party_id"] == _PARTY_ID
    assert row["action_type"] == "create.finding"
    assert row["outcome"] == "consequential"
    assert row["target_id"] == _TARGET_ID
    assert row["target_revision_id"] == _TARGET_REV_ID
    assert row["correlation_id"] == "corr-1"
    assert row["reason_code"] is None
    assert row["recorded_at"] == "2026-01-01T00:00:00.000Z"
    assert row["append_sequence"] == 1


def test_append_denial_inserts_row_with_outcome_deny_and_reason_code(
    engine: Engine, audit_log: AuditLog
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_denial(
            conn,
            actor_party_id=_PARTY_ID,
            attempted_action="approve.decision",
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REV_ID,
            reason_code="no-role-assignment",
            correlation_id="corr-deny-1",
        )

    row = _fetch_audit_row(engine, record.audit_record_id)
    assert row["action_type"] == "approve.decision"
    assert row["outcome"] == "deny"
    assert row["reason_code"] == "no-role-assignment"
    assert row["correlation_id"] == "corr-deny-1"
    assert row["recorded_at"] == "2026-01-01T00:00:00.000Z"


def test_append_consequential_omits_optional_targets(
    engine: Engine, audit_log: AuditLog
) -> None:
    """``target_id`` and ``target_revision_id`` are nullable per the schema."""
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="assign.role",
            correlation_id="corr-no-target",
        )

    row = _fetch_audit_row(engine, record.audit_record_id)
    assert row["target_id"] is None
    assert row["target_revision_id"] is None


def test_append_uses_caller_supplied_recorded_time_over_clock(
    engine: Engine, audit_log: AuditLog
) -> None:
    """A caller-supplied ``recorded_time`` wins over the injected Clock.

    The walking-slice production code paths stamp ``recorded_time`` once at
    the request boundary so every row written in the transaction (audit,
    domain row, manifest) shares an identical timestamp.
    """
    caller_time = datetime(2026, 6, 15, 9, 0, 0, 250_000, tzinfo=timezone.utc)
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.document_revision",
            correlation_id="corr-caller-time",
            recorded_time=caller_time,
        )

    row = _fetch_audit_row(engine, record.audit_record_id)
    assert row["recorded_at"] == "2026-06-15T09:00:00.250Z"
    assert record.recorded_at == "2026-06-15T09:00:00.250Z"


def test_audit_record_id_is_canonical_uuidv7(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Identifiers issued by the service are canonical UUIDv7 strings."""
    import re

    canonical = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-id-shape",
        )

    assert canonical.match(record.audit_record_id), record.audit_record_id


# ---------------------------------------------------------------------------
# Monotonic append_sequence (Requirement 13.4)
# ---------------------------------------------------------------------------


def test_append_sequence_is_monotonically_increasing(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Consecutive appends produce strictly increasing append_sequence values."""
    with engine.begin() as conn:
        _seed_party(conn)
        r1 = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-mono-1",
        )
        r2 = audit_log.append_denial(
            conn,
            actor_party_id=_PARTY_ID,
            attempted_action="approve.decision",
            reason_code="expired",
            correlation_id="corr-mono-2",
        )
        r3 = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.recommendation",
            correlation_id="corr-mono-3",
        )

    assert (r1.append_sequence, r2.append_sequence, r3.append_sequence) == (1, 2, 3)
    assert _all_append_sequences(engine) == [1, 2, 3]


def test_append_sequence_continues_across_transactions(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Sequence numbers persist across separate committed transactions."""
    with engine.begin() as conn:
        _seed_party(conn)
        first = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-a",
        )

    with engine.begin() as conn:
        second = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-b",
        )

    assert first.append_sequence == 1
    assert second.append_sequence == 2
    assert _all_append_sequences(engine) == [1, 2]


def test_unique_append_sequence_constraint_is_enforced(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Hand-rolled INSERTs that reuse a sequence fail per the UNIQUE index."""
    with engine.begin() as conn:
        _seed_party(conn)
        audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-unique",
        )

    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    """
                    INSERT INTO Audit_Records (
                        audit_record_id, append_sequence, actor_party_id,
                        action_type, outcome, correlation_id, recorded_at
                    ) VALUES (
                        :id, 1, :pid, 'create.finding', 'consequential',
                        'corr-dup', '2026-01-01T00:00:00.001Z'
                    )
                    """
                ),
                {"id": "00000000-0000-7000-8000-aaaaaaaaaaaa", "pid": _PARTY_ID},
            )


# ---------------------------------------------------------------------------
# recorded_at — UTC millisecond precision (Requirement 13.1)
# ---------------------------------------------------------------------------


def test_recorded_at_comes_from_injected_clock(engine: Engine) -> None:
    from walking_slice.persistence import create_schema

    create_schema(engine)
    fixed = datetime(2027, 3, 14, 15, 9, 26, 535_000, tzinfo=timezone.utc)
    audit = AuditLog(FixedClock(fixed))

    with engine.begin() as conn:
        _seed_party(conn)
        record = audit.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.document_revision",
            correlation_id="corr-clock",
        )

    row = _fetch_audit_row(engine, record.audit_record_id)
    assert row["recorded_at"] == "2027-03-14T15:09:26.535Z"


def test_recorded_at_is_millisecond_precise_text(
    engine: Engine, audit_log: AuditLog
) -> None:
    """The stored ``recorded_at`` always carries exactly three fractional digits."""
    import re

    pattern = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-ms",
        )

    row = _fetch_audit_row(engine, record.audit_record_id)
    assert pattern.match(row["recorded_at"]), row["recorded_at"]


# ---------------------------------------------------------------------------
# AuditAppendError — failure propagation (Requirements 2.7, 13.6)
# ---------------------------------------------------------------------------


def test_append_raises_audit_append_error_on_missing_fk(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Unknown ``actor_party_id`` violates the FK and surfaces as AuditAppendError."""
    with engine.connect() as conn:
        with pytest.raises(AuditAppendError) as excinfo:
            with conn.begin():
                # Note: no Party seeded for _PARTY_ID — FK reference fails.
                audit_log.append_consequential(
                    conn,
                    actor_party_id=_PARTY_ID,
                    action_type="create.finding",
                    correlation_id="corr-fk",
                )

    # Original SQLAlchemy exception preserved on __cause__ for diagnostics.
    assert excinfo.value.__cause__ is not None
    assert isinstance(excinfo.value.__cause__, IntegrityError)


def test_audit_append_error_rolls_back_caller_transaction(
    engine: Engine, audit_log: AuditLog
) -> None:
    """When the audit append fails, the caller's transaction must roll back —
    no audit row is observable after the failing transaction returns."""
    with engine.connect() as conn:
        with pytest.raises(AuditAppendError):
            with conn.begin():
                audit_log.append_consequential(
                    conn,
                    actor_party_id=_PARTY_ID,  # not seeded → FK fails
                    action_type="create.finding",
                    correlation_id="corr-rollback",
                )

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Audit_Records")
        ).scalar_one()
    assert count == 0


def test_audit_append_error_carries_diagnostic_message(
    engine: Engine, audit_log: AuditLog
) -> None:
    with engine.connect() as conn:
        with pytest.raises(AuditAppendError, match="create\\.finding"):
            with conn.begin():
                audit_log.append_consequential(
                    conn,
                    actor_party_id=_PARTY_ID,
                    action_type="create.finding",
                    correlation_id="corr-diag",
                )


# ---------------------------------------------------------------------------
# Append-only triggers (Requirements 13.3, 13.5) — reaffirm task 1.3 contract
# ---------------------------------------------------------------------------


def test_audit_records_update_is_rejected_by_trigger(
    engine: Engine, audit_log: AuditLog
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-update",
        )

    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Audit_Records SET reason_code='tampered' "
                    "WHERE audit_record_id=:id"
                ),
                {"id": record.audit_record_id},
            )


def test_audit_records_delete_is_rejected_by_trigger(
    engine: Engine, audit_log: AuditLog
) -> None:
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-delete",
        )

    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text("DELETE FROM Audit_Records WHERE audit_record_id=:id"),
                {"id": record.audit_record_id},
            )


def test_appended_row_remains_byte_equivalent_after_rejected_update(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Reaffirms Property 12 against the row this service inserted."""
    with engine.begin() as conn:
        _seed_party(conn)
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            target_id=_TARGET_ID,
            target_revision_id=_TARGET_REV_ID,
            correlation_id="corr-immut",
        )

    before = _fetch_audit_row(engine, record.audit_record_id)
    with engine.connect() as conn, pytest.raises(IntegrityError):
        with conn.begin():
            conn.execute(
                text(
                    "UPDATE Audit_Records SET correlation_id='tampered' "
                    "WHERE audit_record_id=:id"
                ),
                {"id": record.audit_record_id},
            )
    after = _fetch_audit_row(engine, record.audit_record_id)
    assert before == after, "rejected UPDATE must leave the row byte-equivalent"


# ---------------------------------------------------------------------------
# Transactional participation (AD-WS-5)
# ---------------------------------------------------------------------------


def test_caller_rollback_discards_appended_audit_row(
    engine: Engine, audit_log: AuditLog
) -> None:
    """The append participates in the caller's transaction — explicit rollback
    discards the audit row alongside any domain rows in flight."""
    with engine.begin() as conn:
        _seed_party(conn)

    with engine.connect() as conn:
        trans = conn.begin()
        record = audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-rollback-explicit",
        )
        trans.rollback()

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT COUNT(*) FROM Audit_Records "
                "WHERE audit_record_id = :id"
            ),
            {"id": record.audit_record_id},
        ).scalar_one()
    assert rows == 0


def test_two_appends_in_one_transaction_share_the_transaction(
    engine: Engine, audit_log: AuditLog
) -> None:
    """Two appends inside one transaction either both commit or both roll back."""
    with engine.begin() as conn:
        _seed_party(conn)

    with engine.connect() as conn:
        trans = conn.begin()
        audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.finding",
            correlation_id="corr-pair",
        )
        audit_log.append_consequential(
            conn,
            actor_party_id=_PARTY_ID,
            action_type="create.recommendation",
            correlation_id="corr-pair",
        )
        trans.rollback()

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM Audit_Records")
        ).scalar_one()
    assert count == 0
