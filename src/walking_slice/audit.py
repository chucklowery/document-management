"""Audit_Log — append-only ledger for the first walking slice.

Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"Audit_Log",
AD-WS-4 (append-only Immutable Records), AD-WS-5 (audit append in the same
transaction as the originating write).

The :class:`AuditLog` exposes two methods, mirroring the surface in
design §"Audit_Log":

- :meth:`AuditLog.append_consequential` — record a consequential write
  (Document Revision creation, Region Occurrence registration, Finding
  creation, Recommendation creation, Decision finalization, Trail Revision
  creation, Trail Step insertion, Relationship recording, Role assignment).
- :meth:`AuditLog.append_denial` — record a denial of a consequential
  attempt (denied Decision, denied modification, identifier conflict, etc.).

Both methods take a SQLAlchemy :class:`~sqlalchemy.engine.Connection` —
*not* a fresh transaction — so the row is inserted inside the *caller's*
transaction (AD-WS-5). On failure :class:`AuditAppendError` is raised so the
caller can let SQLAlchemy roll the surrounding transaction back per
Requirements 2.7 and 13.6.

The service computes ``append_sequence`` atomically inside the caller's
transaction via ``SELECT COALESCE(MAX(append_sequence), 0) + 1 FROM
Audit_Records`` — SQLite's writer serialization (and the
``UNIQUE(append_sequence)`` constraint on the table itself) ensures the
result is monotonically increasing across all writers per Requirement 13.4.

Requirements satisfied (per task 3.1):
    13.1 — every consequential and every denial append carries actor Identity,
           action type, target Identity, target Revision Identity (when
           applicable), recorded time in UTC with millisecond precision, and
           operation correlation identifier.
    13.3 — append-only Audit_Records; UPDATE/DELETE rejected by triggers in
           :mod:`walking_slice.persistence`. Reaffirmed by unit tests.
    13.4 — insertion order is preserved by ``recorded_at`` as primary key and
           ``append_sequence`` as tiebreaker, both written here.
    13.6 — audit append failure raises :class:`AuditAppendError`, which causes
           the caller's transaction to roll back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Optional

import uuid_utils
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from walking_slice.clock import Clock, truncate_to_milliseconds


__all__ = [
    "AuditLog",
    "AuditAppendError",
    "AuditRecord",
    "format_iso8601_ms",
]


# ---------------------------------------------------------------------------
# Outcome enumeration.
#
# The ``Audit_Records.outcome`` column has a CHECK constraint listing the
# three accepted values; we keep typed constants here so callers and tests
# do not have to remember the spelling.
# ---------------------------------------------------------------------------


_OUTCOME_CONSEQUENTIAL: Final = "consequential"
_OUTCOME_DENY: Final = "deny"
_OUTCOME_PERMIT: Final = "permit"


# Outcomes accepted by :meth:`AuditLog.append_evaluation`. Tracks the
# ``Audit_Records.outcome`` CHECK constraint in
# :mod:`walking_slice.persistence` — every value here is permitted by that
# CHECK and corresponds to an authorization evaluation row (Requirement
# 12.5), not a consequential write (which uses ``'consequential'``).
_EVALUATION_OUTCOMES: Final[frozenset[str]] = frozenset({_OUTCOME_PERMIT, _OUTCOME_DENY})


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


class AuditAppendError(RuntimeError):
    """Raised when an audit append fails inside the caller's transaction.

    Callers must propagate this exception so the surrounding transaction is
    rolled back per Requirements 2.7 and 13.6.

    The original SQLAlchemy exception (e.g.
    :class:`sqlalchemy.exc.IntegrityError` when a foreign-key reference does
    not resolve) is preserved as ``__cause__`` for diagnostic purposes.
    """


# ---------------------------------------------------------------------------
# Return type.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditRecord:
    """Identifier and ordering information for a freshly appended audit row.

    Returned by both :meth:`AuditLog.append_consequential` and
    :meth:`AuditLog.append_denial`. Callers use ``audit_record_id`` when they
    need to reference the audit row from a domain row (none of the domain
    tables FK-reference audit in this slice, but the value is returned for
    test assertions and downstream logging) and ``append_sequence`` to
    verify the monotonic-ordering invariant in Property 11 / 12.

    Attributes:
        audit_record_id: Canonical UUIDv7 identifier of the appended row.
        append_sequence: Monotonically increasing integer assigned inside
            the caller's transaction.
        recorded_at: ISO-8601 UTC text actually written to the row
            (millisecond precision; ``YYYY-MM-DDTHH:MM:SS.mmmZ``).
    """

    audit_record_id: str
    append_sequence: int
    recorded_at: str


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def format_iso8601_ms(value: datetime) -> str:
    """Format ``value`` as ISO-8601 UTC text with millisecond precision.

    Matches the wire format used by the rest of the slice (see the seed
    constants in ``tests/unit/test_persistence.py``): ``YYYY-MM-DDTHH:MM:
    SS.mmmZ``. The input is normalized to UTC and truncated to milliseconds
    via :func:`walking_slice.clock.truncate_to_milliseconds` before
    formatting, so naive datetimes are rejected with :class:`ValueError`.

    Args:
        value: A timezone-aware :class:`datetime.datetime`.

    Returns:
        The formatted timestamp.
    """
    normalized = truncate_to_milliseconds(value)
    milliseconds = normalized.microsecond // 1_000
    return (
        f"{normalized.year:04d}-{normalized.month:02d}-{normalized.day:02d}"
        f"T{normalized.hour:02d}:{normalized.minute:02d}:{normalized.second:02d}"
        f".{milliseconds:03d}Z"
    )


def _new_audit_record_id() -> str:
    """Generate a canonical UUIDv7 string for ``audit_record_id``.

    Uses :func:`uuid_utils.uuid7` directly so this module remains usable
    while task 2.1 (``IdentityService``) is being implemented in parallel.
    Once :class:`walking_slice.identity.IdentityService` is available, the
    application composition layer can swap this helper for
    ``identity_service.new_immutable_record_id()`` without changing the
    rest of :class:`AuditLog`.
    """
    return str(uuid_utils.uuid7())


# ---------------------------------------------------------------------------
# Service.
# ---------------------------------------------------------------------------


class AuditLog:
    """Append-only ledger of consequential and denied actions.

    The service is a thin wrapper over the ``Audit_Records`` table. Instances
    are cheap to construct and can be shared across requests; the
    per-request state (the SQLAlchemy connection, the recorded time) is
    passed as method arguments.

    Args:
        clock: A :class:`walking_slice.clock.Clock` used as the default
            source of ``recorded_at`` when a caller does not pass an
            explicit ``recorded_time``. The clock is *only* consulted when
            the caller omits ``recorded_time``; supplying ``recorded_time``
            from the surrounding ``RequestContext`` clock (the typical
            pattern in production code paths) means every row inside one
            transaction shares an identical timestamp.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    # -- public surface ----------------------------------------------------

    def append_consequential(
        self,
        connection: Connection,
        *,
        actor_party_id: str,
        action_type: str,
        target_id: Optional[str] = None,
        target_revision_id: Optional[str] = None,
        correlation_id: str,
        recorded_time: Optional[datetime] = None,
        payload_digest: Optional[str] = None,
        evaluated_role_assignment_id: Optional[str] = None,
        authorities_required: Optional[str] = None,
        authorities_held: Optional[str] = None,
    ) -> AuditRecord:
        """Append a record for a consequential action.

        Inserts a row into ``Audit_Records`` with ``outcome =
        'consequential'``. The row participates in the caller's transaction
        — ``connection`` is *not* committed or rolled back here.

        Args:
            connection: The SQLAlchemy connection bound to the caller's
                transaction (typically obtained via
                ``engine.begin()`` in the request handler).
            actor_party_id: Identity of the Party that performed the
                consequential action.
            action_type: The action name (e.g. ``"create.finding"``,
                ``"approve.decision"``). Free-form text per the schema; the
                application layer should standardize on the verbs listed in
                design §"Audit_Log".
            target_id: Identity of the affected resource, when applicable.
            target_revision_id: Identity of the affected Revision, when
                applicable.
            correlation_id: Operation correlation identifier shared by every
                row written inside this transaction.
            recorded_time: Optional explicit recorded time. When omitted,
                ``self._clock.now()`` is used. The value is always
                normalized to UTC and truncated to millisecond precision.
            payload_digest: Optional digest of the consequential payload —
                accepted for forward compatibility with design §"Audit_Log"
                but not persisted in the current schema (see notes below).
            evaluated_role_assignment_id: When the consequential action was
                authorized by a specific role assignment, its identity is
                recorded here (Requirement 12.5).
            authorities_required: JSON-encoded list of authorities required
                for the action (used by Property 11 for audit completeness).
            authorities_held: JSON-encoded list of authorities the actor
                actually held at the recorded time.

        Returns:
            An :class:`AuditRecord` carrying the appended row's identifiers
            and ordering metadata.

        Raises:
            AuditAppendError: If the insert fails for any reason (FK
                violation, transaction closed, etc.). The caller MUST allow
                the surrounding transaction to roll back.

        Notes:
            ``payload_digest`` is accepted in the public signature because
            design §"Audit_Log" lists it, but the ``Audit_Records`` schema
            does not yet have a column for it. The value is ignored until
            the schema is extended; tests should not rely on its
            persistence.
        """
        del payload_digest  # not persisted in this schema revision
        return self._append(
            connection=connection,
            outcome=_OUTCOME_CONSEQUENTIAL,
            actor_party_id=actor_party_id,
            action_type=action_type,
            target_id=target_id,
            target_revision_id=target_revision_id,
            reason_code=None,
            correlation_id=correlation_id,
            recorded_time=recorded_time,
            evaluated_role_assignment_id=evaluated_role_assignment_id,
            authorities_required=authorities_required,
            authorities_held=authorities_held,
        )

    def append_denial(
        self,
        connection: Connection,
        *,
        actor_party_id: str,
        attempted_action: str,
        target_id: Optional[str] = None,
        target_revision_id: Optional[str] = None,
        reason_code: str,
        correlation_id: str,
        recorded_time: Optional[datetime] = None,
        evaluated_role_assignment_id: Optional[str] = None,
        authorities_required: Optional[str] = None,
        authorities_held: Optional[str] = None,
    ) -> AuditRecord:
        """Append a record for a denied action.

        Inserts a row into ``Audit_Records`` with ``outcome = 'deny'``. The
        ``reason_code`` is recorded verbatim and should be drawn from the
        Requirement 7.2 enumeration (``not-yet-effective``, ``expired``,
        ``revoked``, ``out-of-scope``, ``no-role-assignment``,
        ``identifier-conflict``, …) — the schema does not constrain the
        value because Requirement 13.2 allows for future expansion.

        See :meth:`append_consequential` for shared parameter and
        exception semantics.
        """
        return self._append(
            connection=connection,
            outcome=_OUTCOME_DENY,
            actor_party_id=actor_party_id,
            action_type=attempted_action,
            target_id=target_id,
            target_revision_id=target_revision_id,
            reason_code=reason_code,
            correlation_id=correlation_id,
            recorded_time=recorded_time,
            evaluated_role_assignment_id=evaluated_role_assignment_id,
            authorities_required=authorities_required,
            authorities_held=authorities_held,
        )

    def append_evaluation(
        self,
        connection: Connection,
        *,
        actor_party_id: str,
        action_type: str,
        outcome: Literal["permit", "deny"],
        target_id: Optional[str] = None,
        target_revision_id: Optional[str] = None,
        evaluated_role_assignment_id: Optional[str] = None,
        authorities_required: Optional[str] = None,
        authorities_held: Optional[str] = None,
        reason_code: Optional[str] = None,
        correlation_id: str,
        recorded_time: Optional[datetime] = None,
    ) -> AuditRecord:
        """Append a record for an authorization evaluation (Requirement 12.5).

        Inserts a row into ``Audit_Records`` with ``outcome`` ∈
        ``{'permit', 'deny'}`` — the two outcomes a single evaluation can
        produce, distinct from the ``'consequential'`` outcome used by the
        originating write itself. The row participates in the caller's
        transaction so the evaluation record either commits alongside any
        consequential write it authorized or rolls back with the rejected
        attempt (AD-WS-5).

        This method is used by
        :class:`walking_slice.authorization.AuthorizationService` so that
        every call to ``evaluate(party, action, target, at)`` leaves a
        durable audit trail identifying the actor, attempted action,
        evaluated role assignment, authorities required, authorities held,
        decision outcome, reason code when denied, and recorded time —
        exactly the set named in Requirement 12.5.

        Args:
            outcome: Must be ``'permit'`` or ``'deny'``; the
                ``'consequential'`` outcome belongs to
                :meth:`append_consequential` and is rejected here so
                callers cannot confuse evaluation rows with
                originating-write rows.
            reason_code: Required for ``'deny'`` (drawn from the
                Requirement 7.2 enumeration); should be ``None`` for
                ``'permit'``.

        Returns:
            An :class:`AuditRecord` carrying the appended row's
            identifiers and ordering metadata.

        Raises:
            ValueError: If ``outcome`` is not in
                :data:`_EVALUATION_OUTCOMES`.
            AuditAppendError: If the underlying insert fails for any
                reason (FK violation, transaction closed, etc.). The
                caller MUST allow the surrounding transaction to roll
                back.

        See :meth:`append_consequential` for shared parameter and
        exception semantics.
        """
        if outcome not in _EVALUATION_OUTCOMES:
            raise ValueError(
                f"append_evaluation outcome must be one of "
                f"{sorted(_EVALUATION_OUTCOMES)!r}; got {outcome!r}."
            )
        return self._append(
            connection=connection,
            outcome=outcome,
            actor_party_id=actor_party_id,
            action_type=action_type,
            target_id=target_id,
            target_revision_id=target_revision_id,
            reason_code=reason_code,
            correlation_id=correlation_id,
            recorded_time=recorded_time,
            evaluated_role_assignment_id=evaluated_role_assignment_id,
            authorities_required=authorities_required,
            authorities_held=authorities_held,
        )

    def _append(
        self,
        *,
        connection: Connection,
        outcome: str,
        actor_party_id: str,
        action_type: str,
        target_id: Optional[str],
        target_revision_id: Optional[str],
        reason_code: Optional[str],
        correlation_id: str,
        recorded_time: Optional[datetime],
        evaluated_role_assignment_id: Optional[str],
        authorities_required: Optional[str],
        authorities_held: Optional[str],
    ) -> AuditRecord:
        # Resolve recorded_at first so any clock error is surfaced before we
        # generate identifiers or hit the database. ``recorded_time`` may be
        # supplied to keep every row in one transaction perfectly aligned;
        # when omitted the injected Clock is queried.
        resolved_time = (
            recorded_time if recorded_time is not None else self._clock.now()
        )
        recorded_at = format_iso8601_ms(resolved_time)
        audit_record_id = _new_audit_record_id()

        try:
            # ``append_sequence`` is computed atomically inside the caller's
            # transaction. SQLite serializes writers, so the read-then-insert
            # is race-free; the UNIQUE constraint on append_sequence catches
            # any pathological case (e.g. unexpected nested transactions) by
            # raising IntegrityError, which we surface as AuditAppendError.
            next_sequence = connection.execute(
                text(
                    "SELECT COALESCE(MAX(append_sequence), 0) + 1 "
                    "FROM Audit_Records"
                )
            ).scalar_one()

            connection.execute(
                text(
                    """
                    INSERT INTO Audit_Records (
                        audit_record_id,
                        append_sequence,
                        actor_party_id,
                        action_type,
                        outcome,
                        target_id,
                        target_revision_id,
                        evaluated_role_assignment_id,
                        authorities_required,
                        authorities_held,
                        reason_code,
                        correlation_id,
                        recorded_at
                    ) VALUES (
                        :audit_record_id,
                        :append_sequence,
                        :actor_party_id,
                        :action_type,
                        :outcome,
                        :target_id,
                        :target_revision_id,
                        :evaluated_role_assignment_id,
                        :authorities_required,
                        :authorities_held,
                        :reason_code,
                        :correlation_id,
                        :recorded_at
                    )
                    """
                ),
                {
                    "audit_record_id": audit_record_id,
                    "append_sequence": int(next_sequence),
                    "actor_party_id": actor_party_id,
                    "action_type": action_type,
                    "outcome": outcome,
                    "target_id": target_id,
                    "target_revision_id": target_revision_id,
                    "evaluated_role_assignment_id": evaluated_role_assignment_id,
                    "authorities_required": authorities_required,
                    "authorities_held": authorities_held,
                    "reason_code": reason_code,
                    "correlation_id": correlation_id,
                    "recorded_at": recorded_at,
                },
            )
        except SQLAlchemyError as exc:
            # Surface every failure as AuditAppendError so callers know to
            # let the surrounding transaction roll back (Requirements 2.7,
            # 13.6). Preserve the original exception via ``__cause__`` for
            # diagnostics.
            raise AuditAppendError(
                f"Audit append failed for action '{action_type}' "
                f"(outcome '{outcome}', correlation_id '{correlation_id}'): {exc}"
            ) from exc

        return AuditRecord(
            audit_record_id=audit_record_id,
            append_sequence=int(next_sequence),
            recorded_at=recorded_at,
        )
