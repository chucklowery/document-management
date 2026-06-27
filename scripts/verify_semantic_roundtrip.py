#!/usr/bin/env python3
"""Verify canonical semantic envelope fixtures.

This baseline intentionally uses only JSON values that are deterministic under
Python's standard serializer and the RFC 8785 subset adopted by ADR-HT-002.
Full RFC 8785 conformance for all number and Unicode edge cases belongs in a
later implementation library and its cross-language test suite.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "semantic-envelope" / "roundtrip-fixtures.json"

REQUIRED_TOP_LEVEL_FIELDS = {
    "contract",
    "contract_version",
    "identity",
    "recorded_at",
    "payload",
    "provenance",
    "extensions",
}


def reject_unsupported_values(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise ValueError(
            f"{path}: floating-point values are not permitted in baseline fixtures"
        )
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path}: JSON object key is not a string")
            reject_unsupported_values(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            reject_unsupported_values(nested, f"{path}[{index}]")
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise ValueError(f"{path}: unsupported JSON value type {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    reject_unsupported_values(value)
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def validate_envelope(name: str, envelope: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_TOP_LEVEL_FIELDS - set(envelope))
    if missing:
        errors.append(f"{name}: missing top-level fields: {', '.join(missing)}")

    contract = envelope.get("contract")
    if not isinstance(contract, str) or not contract.startswith("okw."):
        errors.append(f"{name}: contract must be an okw.* identifier")

    version = envelope.get("contract_version")
    if not isinstance(version, str) or len(version.split(".")) != 3:
        errors.append(f"{name}: contract_version must use MAJOR.MINOR.PATCH")

    identity = envelope.get("identity")
    if not isinstance(identity, dict):
        errors.append(f"{name}: identity must be an object")
    else:
        for field in ("id", "kind", "owning_context"):
            if not identity.get(field):
                errors.append(f"{name}: identity.{field} is required")

    if not isinstance(envelope.get("payload"), dict):
        errors.append(f"{name}: payload must be an object")
    if not isinstance(envelope.get("provenance"), dict):
        errors.append(f"{name}: provenance must be an object")
    if not isinstance(envelope.get("extensions"), dict):
        errors.append(f"{name}: extensions must be an object")

    return errors


def main() -> int:
    if not FIXTURES.is_file():
        print(f"ERROR: fixture file not found: {FIXTURES.relative_to(ROOT)}")
        return 1

    try:
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: unable to read fixtures: {exc}")
        return 1

    if not isinstance(fixtures, list) or not fixtures:
        print("ERROR: fixture file must contain a non-empty array")
        return 1

    errors: list[str] = []
    names: set[str] = set()

    for fixture in fixtures:
        if not isinstance(fixture, dict):
            errors.append("fixture entry is not an object")
            continue

        name = fixture.get("name")
        envelope = fixture.get("envelope")
        expected = fixture.get("expected_sha256")

        if not isinstance(name, str) or not name:
            errors.append("fixture has no valid name")
            continue
        if name in names:
            errors.append(f"duplicate fixture name: {name}")
        names.add(name)

        if not isinstance(envelope, dict):
            errors.append(f"{name}: envelope must be an object")
            continue

        errors.extend(validate_envelope(name, envelope))

        try:
            first = canonical_bytes(envelope)
            reparsed = json.loads(first.decode("utf-8"))
            second = canonical_bytes(reparsed)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{name}: canonicalization failed: {exc}")
            continue

        if first != second:
            errors.append(f"{name}: parse/serialize round trip changed canonical bytes")

        actual = hashlib.sha256(first).hexdigest()
        if expected != actual:
            errors.append(
                f"{name}: digest mismatch; expected {expected}, calculated {actual}"
            )

    if errors:
        print("Semantic round-trip verification failed:\n")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Semantic round-trip verification passed.")
    print(f"Fixtures verified: {len(fixtures)}")
    for fixture in fixtures:
        print(f"- {fixture['name']}: {fixture['expected_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
