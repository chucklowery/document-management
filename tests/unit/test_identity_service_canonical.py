"""Spec-coverage unit tests for :mod:`walking_slice.identity` (task 2.3).

This file is the consolidated "spec coverage" suite for Identity_Service
canonical form and conflict rejection. Each test maps explicitly to one or
more acceptance criteria of Requirement 1 in
``.kiro/specs/first-walking-slice/requirements.md`` and to design
§"Identifier conflict (Requirement 1.4)".

Existing tests already cover the happy paths:

- ``tests/unit/test_identity_service.py`` — canonical form smoke tests,
  factory uniqueness, version + variant bit checks across every factory,
  basic in-memory ``reject_if_duplicate`` semantics (task 2.1).
- ``tests/unit/test_identity_service_registry.py`` — persistent
  Identifier_Registry binding, denial record appending in a separate
  transaction, idempotent re-confirmation, caller-side rollback
  semantics, kind validation, malformed identifier rejection on the
  persistent path (task 2.2).

This file fills the remaining gaps named by task 2.3:

- **Requirement 1.1** — `validate_canonical` rejects every invalid group
  length, lowercase violation, hyphen-position violation, version nibble
  ≠ 7, and variant nibble ∉ ``{8, 9, a, b}`` — enumerated case-by-case
  rather than as a single regex sanity check.
- **Requirement 1.4 (malformed identifier branch)** — both the
  in-memory and persistent paths raise :class:`IdentityFormatError`
  before any binding side-effect, and the persistent path inserts no
  ``Identifier_Registry`` row.
- **Requirement 1.4 (re-binding branch)** — both the in-memory and
  persistent paths raise :class:`IdentityConflictError` and leave the
  existing identifier bound to its original content unchanged
  ("…leave the existing identifier bound to its original content
  unchanged…"). The persistent path additionally appends a Denial
  Record carrying ``reason_code='identifier-conflict'`` and the
  conflicting identifier in ``target_id`` (design §"Error Handling →
  Identifier conflict").
- **Requirement 1.6** — once persisted, an ``Identifier_Registry`` row
  is never reassigned: the append-only triggers installed in
  :mod:`walking_slice.persistence` reject both ``UPDATE`` and
  ``DELETE``.
- **Requirement 1.7** — identifiers carry no business meaning; an
  identifier generated with knowledge of caller-side display names,
  scopes, role names, and content excerpts contains none of those
  substrings.

Each test docstring opens with the requirement clause(s) it validates so a
reader can scan this file as a coverage matrix without leaving the source.
"""

from __future__ import annotations

import re
from typing import Final

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from walking_slice.identity import (
    CANONICAL_UUID7_REGEX,
    IDENTIFIER_CONFLICT_REASON_CODE,
    IdentityConflictError,
    IdentityFormatError,
    IdentityService,
)


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Shared seed helpers.
#
# These mirror the helpers in tests/unit/test_identity_service_registry.py so
# the two test files remain independently runnable. Duplication is preferred
# over a shared helper here because the helpers are tiny and the test files
# are the canonical readable surface of the spec-coverage suite.
# ---------------------------------------------------------------------------


_PARTY_ID: Final[str] = "00000000-0000-7000-8000-000000000001"
_TS: Final[str] = "2026-01-01T00:00:00.000Z"
_ISO_8601_MS_PATTERN: Final[re.Pattern[str]] = re.compile(
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


def _registry_row(engine: Engine, identifier: str) -> dict | None:
    with engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT identifier, kind, content_digest, issued_at "
                    "FROM Identifier_Registry WHERE identifier = :id"
                ),
                {"id": identifier},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def _registry_count(engine: Engine, identifier: str) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM Identifier_Registry WHERE identifier = :id"),
            {"id": identifier},
        ).scalar_one()


def _denial_rows(engine: Engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT actor_party_id, action_type, outcome, target_id, "
                    "target_revision_id, reason_code, correlation_id, recorded_at "
                    "FROM Audit_Records "
                    "WHERE reason_code = :reason "
                    "ORDER BY append_sequence"
                ),
                {"reason": IDENTIFIER_CONFLICT_REASON_CODE},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# ===========================================================================
# Requirement 1.1 — canonical UUIDv7 form
#
# "WHEN the Identity_Service creates a managed Resource, Resource Revision,
# Relationship, Content Region, Immutable Record, Trail, Trail Revision, or
# Trail Step, THE Identity_Service SHALL assign exactly one UUID version 7
# identifier in canonical lowercase hyphenated 8-4-4-4-12 hex form…"
#
# `validate_canonical` is the single chokepoint the slice uses to enforce
# this contract on inbound identifiers (imports, API references, etc.). The
# tests below decompose the regex into its independent constraints so a
# regression in any one of them is named explicitly.
# ===========================================================================


# A known-canonical UUIDv7 used as the base for mutation-style negative cases.
_BASE: Final[str] = "019ef20a-95d0-7521-983d-d7d5d81aad60"


# Group lengths required by the spec (8-4-4-4-12). Each parametrisation
# perturbs exactly one group at a time so the failure points to the violated
# group.
@pytest.mark.parametrize(
    ("description", "candidate"),
    [
        ("group 1 too short (7 hex)",   "019ef20-95d0-7521-983d-d7d5d81aad60"),
        ("group 1 too long  (9 hex)",   "019ef20aa-95d0-7521-983d-d7d5d81aad60"),
        ("group 2 too short (3 hex)",   "019ef20a-95d-7521-983d-d7d5d81aad60"),
        ("group 2 too long  (5 hex)",   "019ef20a-95d00-7521-983d-d7d5d81aad60"),
        ("group 3 too short (3 hex)",   "019ef20a-95d0-752-983d-d7d5d81aad60"),
        ("group 3 too long  (5 hex)",   "019ef20a-95d0-75211-983d-d7d5d81aad60"),
        ("group 4 too short (3 hex)",   "019ef20a-95d0-7521-983-d7d5d81aad60"),
        ("group 4 too long  (5 hex)",   "019ef20a-95d0-7521-9833d-d7d5d81aad60"),
        ("group 5 too short (11 hex)",  "019ef20a-95d0-7521-983d-d7d5d81aad6"),
        ("group 5 too long  (13 hex)",  "019ef20a-95d0-7521-983d-d7d5d81aad600"),
    ],
)
def test_validate_canonical_rejects_each_group_length_violation(
    description: str, candidate: str
) -> None:
    """**Validates: Requirement 1.1** — 8-4-4-4-12 hex grouping per group."""
    service = IdentityService()
    assert service.validate_canonical(candidate) is False, description


@pytest.mark.parametrize(
    "candidate",
    [
        "019ef20a95d07521983dd7d5d81aad60",                     # no hyphens
        "019ef20a-95d07521-983d-d7d5d81aad60",                  # only 3 groups
        "019ef20a-95d0-7521-983dd7d5d81aad60",                  # missing 4th hyphen
        "019ef20a-95d0-7521-983d-d7d5d81aad6 0",                # space instead of hex
        "019ef20a:95d0:7521:983d:d7d5d81aad60",                 # wrong separator
    ],
)
def test_validate_canonical_rejects_hyphen_or_separator_violations(candidate: str) -> None:
    """**Validates: Requirement 1.1** — exactly four hyphens at canonical positions."""
    service = IdentityService()
    assert service.validate_canonical(candidate) is False


# A canonical UUIDv7 whose every group contains at least one hex letter,
# so case-flipping any single group is observable.
_BASE_ALPHA: Final[str] = "01abcdef-abcd-7abc-9abc-abcdef0123ab"


@pytest.mark.parametrize(
    ("description", "candidate"),
    [
        ("entire identifier uppercase", _BASE_ALPHA.upper()),
        ("group 1 uppercase",           _BASE_ALPHA[:8].upper() + _BASE_ALPHA[8:]),
        ("group 2 uppercase",           _BASE_ALPHA[:9] + _BASE_ALPHA[9:13].upper() + _BASE_ALPHA[13:]),
        ("group 3 uppercase",           _BASE_ALPHA[:14] + _BASE_ALPHA[14:18].upper() + _BASE_ALPHA[18:]),
        ("group 4 uppercase",           _BASE_ALPHA[:19] + _BASE_ALPHA[19:23].upper() + _BASE_ALPHA[23:]),
        ("group 5 uppercase",           _BASE_ALPHA[:24] + _BASE_ALPHA[24:].upper()),
        ("single letter in group 5",    _BASE_ALPHA[:-1] + _BASE_ALPHA[-1].upper()),
    ],
)
def test_validate_canonical_rejects_uppercase_hex(
    description: str, candidate: str
) -> None:
    """**Validates: Requirement 1.1** — canonical *lowercase* hex form."""
    service = IdentityService()
    # Sanity: our parametrisation actually mutates the base so the test
    # cannot silently pass against the unchanged lowercase string.
    assert candidate != _BASE_ALPHA, description
    assert service.validate_canonical(candidate) is False, description


# Every invalid version nibble (0..6 and 8..f) is enumerated so a regression
# that accepts a non-UUIDv7 version is caught by name, not by a single
# representative sample.
_INVALID_VERSION_NIBBLES: Final[tuple[str, ...]] = tuple(
    nibble for nibble in "0123456789abcdef" if nibble != "7"
)


@pytest.mark.parametrize("invalid_version_nibble", _INVALID_VERSION_NIBBLES)
def test_validate_canonical_rejects_every_non_seven_version_nibble(
    invalid_version_nibble: str,
) -> None:
    """**Validates: Requirement 1.1** — version nibble must be ``7`` (UUIDv7)."""
    service = IdentityService()
    # Replace only the first char of the third group (the version-nibble slot).
    third_group = invalid_version_nibble + _BASE.split("-")[2][1:]
    parts = _BASE.split("-")
    parts[2] = third_group
    candidate = "-".join(parts)
    assert service.validate_canonical(candidate) is False, candidate


# Variant nibble must be one of {8, 9, a, b} — i.e. top two bits == 0b10.
_VALID_VARIANT_NIBBLES: Final[frozenset[str]] = frozenset("89ab")
_INVALID_VARIANT_NIBBLES: Final[tuple[str, ...]] = tuple(
    nibble for nibble in "0123456789abcdef" if nibble not in _VALID_VARIANT_NIBBLES
)


@pytest.mark.parametrize("invalid_variant_nibble", _INVALID_VARIANT_NIBBLES)
def test_validate_canonical_rejects_every_non_rfc_4122_variant_nibble(
    invalid_variant_nibble: str,
) -> None:
    """**Validates: Requirement 1.1** — variant nibble must be in ``[89ab]``."""
    service = IdentityService()
    # Replace only the first char of the fourth group (the variant-nibble slot).
    fourth_group = invalid_variant_nibble + _BASE.split("-")[3][1:]
    parts = _BASE.split("-")
    parts[3] = fourth_group
    candidate = "-".join(parts)
    assert service.validate_canonical(candidate) is False, candidate


@pytest.mark.parametrize("valid_variant_nibble", sorted(_VALID_VARIANT_NIBBLES))
def test_validate_canonical_accepts_every_rfc_4122_variant_nibble(
    valid_variant_nibble: str,
) -> None:
    """**Validates: Requirement 1.1** — all four RFC 4122 variants accepted."""
    service = IdentityService()
    parts = _BASE.split("-")
    parts[3] = valid_variant_nibble + parts[3][1:]
    candidate = "-".join(parts)
    assert service.validate_canonical(candidate) is True, candidate


def test_canonical_regex_pattern_is_the_one_the_spec_mandates() -> None:
    """**Validates: Requirement 1.1** — published regex matches the spec verbatim."""
    expected = (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )
    assert CANONICAL_UUID7_REGEX.pattern == expected
    # Defence in depth: no case-insensitive flag silently weakens the regex.
    assert CANONICAL_UUID7_REGEX.flags & re.IGNORECASE == 0


# ===========================================================================
# Requirement 1.4 — malformed identifier branch
#
# "IF an identifier generation, import, or reference operation would assign
# an existing identifier to different domain content, or would introduce a
# malformed identifier, THEN THE Identity_Service SHALL reject the
# operation, return an error indication identifying the conflicting
# identifier, leave the existing identifier bound to its original content
# unchanged, and append a Denial Record to the Audit_Log within the same
# operation."
#
# The "malformed identifier" branch is exercised here for both the in-memory
# and persistent paths; the "different domain content" branch is exercised
# in the following section.
# ===========================================================================


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "not-a-uuid",
        _BASE.upper(),
        _BASE[:-1],                                               # missing trailing char
        _BASE.replace("-", ""),                                   # no hyphens
        _BASE.replace("7521", "6521"),                            # version nibble = 6
        _BASE.replace("983d", "c83d"),                            # variant nibble = c
    ],
)
def test_in_memory_reject_if_duplicate_raises_format_error_for_malformed(
    malformed: str,
) -> None:
    """**Validates: Requirement 1.4** (malformed-identifier branch, in-memory path)."""
    service = IdentityService()
    with pytest.raises(IdentityFormatError):
        service.reject_if_duplicate(malformed, "digest")


@pytest.mark.parametrize(
    "malformed",
    [
        "",
        "not-a-uuid",
        _BASE.upper(),
        _BASE.replace("7521", "6521"),
        _BASE.replace("983d", "c83d"),
    ],
)
def test_persistent_reject_if_duplicate_rejects_malformed_without_inserting_row(
    engine: Engine, identity_service: IdentityService, malformed: str
) -> None:
    """**Validates: Requirement 1.4** (malformed-identifier branch, persistent path).

    The format check runs before the connection is touched, so no
    ``Identifier_Registry`` row is inserted for the bad identifier.
    """
    with engine.begin() as conn:
        with pytest.raises(IdentityFormatError):
            identity_service.reject_if_duplicate(
                malformed,
                "digest-malformed",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-malformed",
            )

    with engine.connect() as conn:
        total = conn.execute(
            text("SELECT COUNT(*) FROM Identifier_Registry")
        ).scalar_one()
    assert total == 0


def test_in_memory_format_error_leaves_registry_empty() -> None:
    """**Validates: Requirement 1.4** — malformed identifier does not pollute registry.

    A subsequent in-memory rebind of a legitimately-issued identifier to
    a fresh digest must succeed; the previous failed call did not
    silently bind anything.
    """
    service = IdentityService()
    with pytest.raises(IdentityFormatError):
        service.reject_if_duplicate("not-a-uuid", "digest-x")
    legitimate = service.new_resource_id()
    # No latent binding from the failed call — first real bind succeeds.
    service.reject_if_duplicate(legitimate, "digest-y")


# ===========================================================================
# Requirement 1.4 — re-binding branch (different domain content)
#
# Each test names the clause it validates:
# - rejection ("…SHALL reject the operation…")
# - error indication identifying the conflicting identifier
# - "…leave the existing identifier bound to its original content
#   unchanged…"
# - "…append a Denial Record to the Audit_Log within the same operation…"
# ===========================================================================


def test_in_memory_rebind_to_different_digest_raises_identity_conflict_error() -> None:
    """**Validates: Requirement 1.4** — rejection + error identifies conflict (in-memory)."""
    service = IdentityService()
    identifier = service.new_resource_id()
    service.reject_if_duplicate(identifier, "digest-original")
    with pytest.raises(IdentityConflictError) as exc_info:
        service.reject_if_duplicate(identifier, "digest-attempt")
    err = exc_info.value
    assert err.identifier == identifier
    assert err.existing_digest == "digest-original"
    assert err.attempted_digest == "digest-attempt"


def test_in_memory_rebind_leaves_existing_binding_unchanged() -> None:
    """**Validates: Requirement 1.4** — original content remains bound (in-memory).

    After a rejected re-bind, the original digest is still in effect:
    re-confirmation with the original digest succeeds (idempotent), and
    a second re-bind to a *different* digest still raises with the same
    ``existing_digest`` field — proving the conflict path is read-only.
    """
    service = IdentityService()
    identifier = service.new_resource_id()
    service.reject_if_duplicate(identifier, "digest-original")

    with pytest.raises(IdentityConflictError):
        service.reject_if_duplicate(identifier, "digest-attempt-1")

    # Idempotent confirmation of the *original* digest still succeeds.
    service.reject_if_duplicate(identifier, "digest-original")

    # A second rejected re-bind still names the original digest, never
    # the first attempted one.
    with pytest.raises(IdentityConflictError) as exc_info:
        service.reject_if_duplicate(identifier, "digest-attempt-2")
    assert exc_info.value.existing_digest == "digest-original"
    assert exc_info.value.attempted_digest == "digest-attempt-2"


def test_persistent_rebind_appends_denial_record_with_conflicting_identifier(
    engine: Engine, identity_service: IdentityService
) -> None:
    """**Validates: Requirement 1.4** — Denial Record carries the conflicting identifier and reason code.

    Mirrors design §"Error Handling → Identifier conflict":
      1. Originating transaction rolls back.
      2. Denial Record appended with ``reason_code='identifier-conflict'``
         in a separate transaction so the row survives the rollback.
      3. ``target_id`` carries the conflicting identifier so an auditor
         can locate the binding from the audit row alone.
    """
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-original",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-orig",
        )

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            identity_service.reject_if_duplicate(
                identifier,
                "digest-attempt",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-deny",
                attempted_action="bind.identifier",
            )
        trans.rollback()

    denials = _denial_rows(engine)
    assert len(denials) == 1
    denial = denials[0]
    assert denial["outcome"] == "deny"
    assert denial["reason_code"] == IDENTIFIER_CONFLICT_REASON_CODE
    assert denial["target_id"] == identifier
    assert denial["actor_party_id"] == _PARTY_ID
    assert denial["correlation_id"] == "corr-deny"
    assert _ISO_8601_MS_PATTERN.match(denial["recorded_at"]), denial["recorded_at"]


def test_persistent_rebind_leaves_prior_row_byte_equivalent(
    engine: Engine, identity_service: IdentityService
) -> None:
    """**Validates: Requirement 1.4** — original ``Identifier_Registry`` row unchanged (persistent)."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-original",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-orig",
        )
    before = _registry_row(engine, identifier)

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            identity_service.reject_if_duplicate(
                identifier,
                "digest-attempt",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-deny",
            )
        trans.rollback()
    after = _registry_row(engine, identifier)

    assert before == after
    assert _registry_count(engine, identifier) == 1


def test_persistent_rebind_after_failure_still_accepts_original_digest(
    engine: Engine, identity_service: IdentityService
) -> None:
    """**Validates: Requirement 1.4** — original binding remains addressable (persistent).

    Idempotent re-confirmation with the original digest succeeds after a
    rejected re-bind; the registry row count stays at 1 and no new
    denial is recorded for the idempotent call.
    """
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        _seed_party(conn)
        identity_service.reject_if_duplicate(
            identifier,
            "digest-original",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-orig",
        )

    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            identity_service.reject_if_duplicate(
                identifier,
                "digest-attempt",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-deny",
            )
        trans.rollback()

    # Idempotent re-confirmation of the original digest still succeeds.
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            identifier,
            "digest-original",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-confirm",
        )

    assert _registry_count(engine, identifier) == 1
    # Exactly one denial was recorded for the one rejected re-bind.
    assert len(_denial_rows(engine)) == 1


# ===========================================================================
# Requirement 1.6 — Identifier never re-assigned to different content
#
# "THE Identity_Service SHALL NOT reassign a once-assigned identifier to
# different domain content, even after withdrawal, redaction, retention
# expiry, or deletion of the original content…"
#
# The Python-level enforcement lives in `reject_if_duplicate` and is covered
# above. The schema-level enforcement is the append-only Identifier_Registry
# triggers installed in `walking_slice.persistence`: UPDATE and DELETE on
# that table are rejected unconditionally. We exercise those triggers
# directly so a regression in the SQL surface cannot hide behind the Python
# surface.
# ===========================================================================


def test_identifier_registry_rejects_update_via_trigger(
    engine: Engine, identity_service: IdentityService
) -> None:
    """**Validates: Requirement 1.6** — SQL-level UPDATE on Identifier_Registry rejected."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            identifier,
            "digest-original",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-upd-1",
        )

    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text(
                "UPDATE Identifier_Registry SET content_digest = :d "
                "WHERE identifier = :id"
            ),
            {"d": "digest-attempt", "id": identifier},
        )

    row = _registry_row(engine, identifier)
    assert row is not None
    assert row["content_digest"] == "digest-original"


def test_identifier_registry_rejects_delete_via_trigger(
    engine: Engine, identity_service: IdentityService
) -> None:
    """**Validates: Requirement 1.6** — SQL-level DELETE on Identifier_Registry rejected."""
    identifier = identity_service.new_resource_id()
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            identifier,
            "digest-keep",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-del-1",
        )

    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("DELETE FROM Identifier_Registry WHERE identifier = :id"),
            {"id": identifier},
        )

    assert _registry_count(engine, identifier) == 1


# ===========================================================================
# Requirement 1.7 — Identifier opacity
#
# "THE Identity_Service SHALL NOT encode mutable name, repository path,
# organization name, security classification, lifecycle state, authority,
# semantic version, owning Party, or other business meaning into any
# issued identifier…"
#
# Property 10 covers this universally over a Hypothesis strategy. The
# example test here is a focused smoke that confirms the example values
# named in Requirement 1.7 do not leak into a freshly minted identifier.
# ===========================================================================


_BUSINESS_ATTRIBUTES: Final[tuple[str, ...]] = (
    "alice",                            # display name
    "documents/sources/interview-3",    # repository path
    "acme-corp",                        # organization
    "confidential",                     # security classification
    "draft",                            # lifecycle state
    "authoritative",                    # authority designation
    "1.2.0",                            # semantic version
    "party-007",                        # owning Party identifier
)


def test_issued_identifier_does_not_carry_business_attributes() -> None:
    """**Validates: Requirement 1.7** — no business attribute substring in the issued id.

    The Identity_Service is intentionally side-effect-free with respect
    to caller-side metadata; this test simply asserts that none of the
    example business attributes listed in Requirement 1.7 appear inside
    any of a batch of 128 freshly-minted identifiers across every
    factory method.
    """
    service = IdentityService()
    factories = (
        service.new_resource_id,
        service.new_revision_id,
        service.new_relationship_id,
        service.new_region_id,
        service.new_immutable_record_id,
        service.new_trail_id,
        service.new_trail_revision_id,
        service.new_trail_step_id,
        service.new_manifest_id,
    )
    for _ in range(16):
        for factory in factories:
            identifier = factory()
            for attribute in _BUSINESS_ATTRIBUTES:
                assert attribute not in identifier, (identifier, attribute)


# ===========================================================================
# Cross-mode parity smoke
#
# The in-memory and persistent paths must surface the same exception type
# for malformed identifiers and for re-binding conflicts so the higher
# layers (HTTP handlers, request context) can catch a single exception type
# regardless of whether they passed a connection. The two surfaces are
# already exercised separately above; this one test asserts they agree.
# ===========================================================================


def test_in_memory_and_persistent_paths_raise_same_exception_types(
    engine: Engine, identity_service: IdentityService
) -> None:
    """**Validates: Requirements 1.1, 1.4** — both paths agree on exception types."""
    # Malformed identifier:
    in_memory = IdentityService()
    with pytest.raises(IdentityFormatError):
        in_memory.reject_if_duplicate("not-a-uuid", "digest")
    with engine.begin() as conn, pytest.raises(IdentityFormatError):
        identity_service.reject_if_duplicate(
            "not-a-uuid",
            "digest",
            connection=conn,
            kind="resource",
        )

    # Re-binding conflict:
    in_memory_id = in_memory.new_resource_id()
    in_memory.reject_if_duplicate(in_memory_id, "digest-a")
    with pytest.raises(IdentityConflictError):
        in_memory.reject_if_duplicate(in_memory_id, "digest-b")

    persistent_id = identity_service.new_resource_id()
    with engine.begin() as conn:
        identity_service.reject_if_duplicate(
            persistent_id,
            "digest-a",
            connection=conn,
            kind="resource",
            actor_party_id=_PARTY_ID,
            correlation_id="corr-parity-1",
        )
    with engine.connect() as conn:
        trans = conn.begin()
        with pytest.raises(IdentityConflictError):
            identity_service.reject_if_duplicate(
                persistent_id,
                "digest-b",
                connection=conn,
                kind="resource",
                actor_party_id=_PARTY_ID,
                correlation_id="corr-parity-2",
            )
        trans.rollback()
