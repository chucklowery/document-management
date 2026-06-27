"""Unit tests for :mod:`walking_slice.identity` registry persistence (task 2.2).

Coverage scope (per task 2.2):

- **Persistent issuance** — when :meth:`IdentityService.reject_if_duplicate`
  is called with a SQLAlchemy connection and the caller's transaction
  commits, a row appears in ``Identifier_Registry`` with the supplied
  ``kind``, ``content_digest``, and a millisecond-precision
  ``issued_at`` timestamp (AD-WS-5; Requirements 1.1, 1.6).
- **Identifier conflict** — re-binding an already-bound identifier to
  a different content digest raises :class:`IdentityConflictError`,
  leaves the prior row unchanged, and appends an ``Audit_Records``
  denial row carrying ``reason_code='identifier-conflict'`` from a
  separate transaction so the row survives the caller-side rollback
  (Requirement 1.4; design §"Error Handling — Identifier conflict").
- **Idempotent re-confirmation** — re-binding to the same content
  digest is a no-op: no duplicate ``Identifier_Registry`` row, no
  audit append.
- **Rollback semantics** — when the caller's transaction is rolled
  back, no ``Identifier_Registry`` row is persisted (Requirement 2.7
  pattern for identity bindings).

Backwards compatibility with the in-memory surface introduced in task
2.1 (``IdentityService()`` without a connection) is also exercised so
the dual-mode contract documented on :class:`IdentityService` does not
regress.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from walking_slice.audit import AuditLog
from walking_slice.identity import (
    ALLOWED_IDENTIFIER_KINDS,
    IDENTIFIER_CONFLICT_REASON_CODE,
    IdentityConflictError,
    IdentityFormatError,
    IdentityService,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Seed helpers — Parties FK is required for any Audit_Records denial row.
# ---------------------------------------------------------------------------


_PARTY_ID = "00000000-0000-7000-8000-000000000001"
_TS = "2026-01-01T00:00:00.000Z"
_ISO_8601_MS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def _seed_party(conn, party_id: str = _PARTY_ID) -> None:
    conn.execute(
        text(
            """
            INSERT INTO Parties (party_id, kind, display_name, created_at)
            VALUES (:pid, 'person', 'Test Party', :ts)
            """
        ),
        {"pid": party_id, "ts": _TS},
    )


def _fetch_registry_row(engine: Engine, identifier: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT identifier, kind, content_digest, issued_at "
                "FROM Identifier_Registry WHERE identifier = :id"
            ),
            {"id": identifier},
        ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _count_registry_rows(engine: Engine, identifier: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT COUNT(*) FROM Identifier_Registry "
                "WHERE identifier = :id"
            ),
            {"id": identifier},
        ).scalar_one()


def _fetch_denial_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT actor_party_id, action_type, outcome, target_id, "
                "target_revision_id, reason_code, correlation_id, recorded_at "
                "FROM Audit_Records "
                "WHERE reason_code = :reason "
                "ORDER BY append_sequence"
            ),
            {"reason": IDENTIFIER_CONFLICT_REASON_CODE},
        ).mappings().all()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Persistent issuance — Identifier_Registry row appears after commit.
# ---------------------------------------------------------------------------


def test_persistent_first_binding_inserts_registry_row(
    engine: Engine, identity_service: IdentityService
) -> None:
    """A first-time binding inserts a row with the supplied kind and digest."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-persist-1",
        )

    row = _fetch_registry_row(engine, identifier)
    assert row is not None
    assert row["identifier"] == identifier
    assert row["kind"] == "resource"
    assert row["content_digest"] == "digest-a"
    assert _ISO_8601_MS_PATTERN.match(row["issued_at"]), row["issued_at"]


def test_persistent_first_binding_emits_no_denial(
    engine: Engine, identity_service: IdentityService
) -> None:
    """A first-time binding must not append an audit denial row."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-no-denial",
        )

    assert _fetch_denial_rows(engine) == []


def test_persistent_binding_uses_injected_clock_for_issued_at(
    engine: Engine, identity_service: IdentityService
) -> None:
    """``issued_at`` is sourced from the injected :class:`Clock` by default."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            identifier,
            "digest-x",
            connection=conn,
            kind="revision",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-clock",
        )

    row = _fetch_registry_row(engine, identifier)
    assert row is not None
    # The default conftest clock is pinned to 2026-01-01T00:00:00.000Z.
    assert row["issued_at"] == "2026-01-01T00:00:00.000Z"


@pytest.mark.parametrize("kind", sorted(ALLOWED_IDENTIFIER_KINDS))
def test_persistent_binding_accepts_every_allowed_kind(
    engine: Engine, identity_service: IdentityService, kind: str
) -> None:
    """All ten kinds the Identifier_Registry CHECK accepts round-trip."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            identifier,
            f"digest-{kind}",
            connection=conn,
            kind=kind,
            actor_party_id=_PARTY_ID,
            correlation_id=f"corr-kind-{kind}",
        )

    row = _fetch_registry_row(engine, identifier)
    assert row is not None
    assert row["kind"] == kind


# ---------------------------------------------------------------------------
# Identifier conflict — raises, leaves prior row, appends audit denial.
# ---------------------------------------------------------------------------


def test_persistent_conflict_raises_identity_conflict_error(
    engine: Engine, identity_service: IdentityService
) -> None:
    """Re-binding to a different digest raises :class:`IdentityConflictError`."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-bind-a",
        )

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError) as exc_info:
            identity_service.reject_if_duplicate(
                identifier,
                "digest-b",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-bind-b",
            )
        trans.rollback()

    err = exc_info.value
    assert err.identifier == identifier
    assert err.existing_digest == "digest-a"
    assert err.attempted_digest == "digest-b"


def test_persistent_conflict_leaves_prior_registry_row_unchanged(
    engine: Engine, identity_service: IdentityService
) -> None:
    """A rejected re-bind must leave the existing row byte-equivalent."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-keep-prior",
        )

    before = _fetch_registry_row(engine, identifier)

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            identity_service.reject_if_duplicate(
                identifier,
                "digest-b",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-attempt-b",
            )
        trans.rollback()

    after = _fetch_registry_row(engine, identifier)
    assert before == after
    assert _count_registry_rows(engine, identifier) == 1


def test_persistent_conflict_appends_audit_denial_in_separate_transaction(
    engine: Engine, identity_service: IdentityService
) -> None:
    """The denial row persists even when the caller rolls back."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-bind",
        )

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            identity_service.reject_if_duplicate(
                identifier,
                "digest-b",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-deny-1",
                attempted_action="create.document_revision",
            )
        trans.rollback()  # caller's originating tx rolls back

    denials = _fetch_denial_rows(engine)
    assert len(denials) == 1, denials
    denial = denials[0]
    assert denial["actor_party_id"] == _PARTY_ID
    assert denial["action_type"] == "create.document_revision"
    assert denial["outcome"] == "deny"
    assert denial["target_id"] == identifier
    assert denial["target_revision_id"] is None
    assert denial["reason_code"] == IDENTIFIER_CONFLICT_REASON_CODE
    assert denial["correlation_id"] == "corr-deny-1"
    assert _ISO_8601_MS_PATTERN.match(denial["recorded_at"]), denial["recorded_at"]


def test_persistent_conflict_without_audit_wiring_still_raises(
    engine: Engine, audit_log: AuditLog
) -> None:
    """If no audit context is supplied the conflict still raises, with no row.

    The persistent registry path is still available; only the audit
    append is skipped per the service contract ("Insufficient wiring to
    write a denial. The conflict will still be raised...").
    """
    service = IdentityService(engine=engine, audit_log=audit_log)
    identifier = service.new_resource_id()
    with engine.begin() as conn:
        service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
        )

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            service.reject_if_duplicate(
                identifier,
                "digest-b",
                connection=conn,
                kind="resource",
            )
        trans.rollback()

    # No denial row because actor/correlation context was not supplied.
    assert _fetch_denial_rows(engine) == []
    # Prior binding row is untouched.
    row = _fetch_registry_row(engine, identifier)
    assert row is not None and row["content_digest"] == "digest-a"


# ---------------------------------------------------------------------------
# Idempotent re-confirmation — same digest is a no-op.
# ---------------------------------------------------------------------------


def test_persistent_idempotent_rebind_does_not_duplicate_row(
    engine: Engine, identity_service: IdentityService
) -> None:
    """Re-binding to the *same* digest leaves a single Identifier_Registry row."""
    identifier = identity_service.new_resource_id()
    for correlation in ("corr-idem-1", "corr-idem-2", "corr-idem-3"):
        with engine.begin() as conn:
            identity_service.reject_if_duplicate(
                identifier,
                "digest-same",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id=correlation,
            )

    assert _count_registry_rows(engine, identifier) == 1
    row = _fetch_registry_row(engine, identifier)
    assert row is not None
    assert row["content_digest"] == "digest-same"


def test_persistent_idempotent_rebind_does_not_append_audit(
    engine: Engine, identity_service: IdentityService
) -> None:
    """Idempotent re-confirmation must not write any audit row at all."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-same",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-idem-a",
        )
        identity_service.reject_if_duplicate(
            identifier,
            "digest-same",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-idem-b",
        )

    with engine.connect() as conn:
        audit_count = conn.execute(
            text("SELECT COUNT(*) FROM Audit_Records")
        ).scalar_one()
    assert audit_count == 0


# ---------------------------------------------------------------------------
# Rollback semantics — caller-side rollback discards the registry row.
# ---------------------------------------------------------------------------


def test_caller_rollback_discards_identifier_registry_row(
    engine: Engine, identity_service: IdentityService
) -> None:
    """The registry INSERT participates in the caller's transaction (AD-WS-5)."""
    identifier = identity_service.new_resource_id()

    with engine.connect() as conn:
        trans = conn.begin()
        identity_service.reject_if_duplicate(
            identifier,
            "digest-rb",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-rb",
        )
        trans.rollback()

    assert _count_registry_rows(engine, identifier) == 0
    assert _fetch_registry_row(engine, identifier) is None


def test_caller_rollback_after_idempotent_rebind_does_not_resurrect_prior_row(
    engine: Engine, identity_service: IdentityService
) -> None:
    """If the initial binding is rolled back, an idempotent re-call does not
    leave a stray row either."""
    identifier = identity_service.new_resource_id()

    with engine.connect() as conn:
        trans = conn.begin()
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-rb-1",
        )
        identity_service.reject_if_duplicate(
            identifier,
            "digest-a",  # idempotent on same digest
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-rb-2",
        )
        trans.rollback()

    assert _count_registry_rows(engine, identifier) == 0


# ---------------------------------------------------------------------------
# Validation guards — kind enforcement, malformed identifiers.
# ---------------------------------------------------------------------------


def test_persistent_path_requires_kind(
    engine: Engine, identity_service: IdentityService
) -> None:
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn, pytest.raises(ValueError, match="kind is required"):
        identity_service.reject_if_duplicate(
            identifier,
            "digest",
            connection=conn,
            actor_party_id=_PARTY_ID,
            correlation_id="corr-no-kind",
        )


def test_persistent_path_rejects_unknown_kind(
    engine: Engine, identity_service: IdentityService
) -> None:
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn, pytest.raises(ValueError, match="unknown identifier kind"):
        identity_service.reject_if_duplicate(
            identifier,
            "digest",
            connection=conn,
            kind="not-a-kind",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-bad-kind",
        )


def test_persistent_path_rejects_malformed_identifier(
    engine: Engine, identity_service: IdentityService
) -> None:
    with engine.begin() as conn, pytest.raises(IdentityFormatError):
        identity_service.reject_if_duplicate(
            "not-a-uuid",
            "digest",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-malformed",
        )


# ---------------------------------------------------------------------------
# Backwards compatibility — task-2.1 in-memory surface still works.
# ---------------------------------------------------------------------------


def test_in_memory_path_still_binds_and_rejects() -> None:
    """``IdentityService()`` with no engine still operates from memory."""
    service = IdentityService()
    identifier = service.new_resource_id()
    service.reject_if_duplicate(identifier, "digest-a")
    service.reject_if_duplicate(identifier, "digest-a")  # idempotent
    with pytest.raises(IdentityConflictError):
        service.reject_if_duplicate(identifier, "digest-b")


def test_persistent_service_in_memory_path_when_no_connection_supplied(
    identity_service: IdentityService,
) -> None:
    """Even a fully-wired service falls through to memory without a connection."""
    identifier = identity_service.new_resource_id()
    identity_service.reject_if_duplicate(identifier, "digest-a")
    identity_service.reject_if_duplicate(identifier, "digest-a")  # idempotent
    with pytest.raises(IdentityConflictError):
        identity_service.reject_if_duplicate(identifier, "digest-b")
