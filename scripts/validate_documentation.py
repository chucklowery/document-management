#!/usr/bin/env python3
"""Validate documentation inventory and traceability references.

The validator intentionally uses only the Python standard library so it can run
locally and in a minimal GitHub Actions job.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ROOT / "documents"
COVERAGE_FILE = ROOT / "traceability" / "constitutional-coverage.json"

STATUS_VALUES = {
    "Defined",
    "Specified",
    "Designed",
    "Verified",
    "Excepted",
    "Uncovered",
}

DEFINITION_PATTERNS = {
    "stories": re.compile(r"^##\s+(US-\d{3})\b", re.MULTILINE),
    "requirements": re.compile(r"^##\s+(REQ-[A-Z]{2}-\d{3})\b", re.MULTILINE),
    "acceptance": re.compile(r"^#\s+Feature\s+(AS-\d{3})\b", re.MULTILINE),
    "acceptance_scenarios": re.compile(
        r"^##\s+Scenario\s+(AS-\d{3}\.\d+)\b", re.MULTILINE
    ),
    "adrs": re.compile(r"^##\s+(ADR-[A-Z]{2}-\d{3})\b", re.MULTILINE),
}

REFERENCE_PATTERNS = {
    "stories": re.compile(r"\bUS-\d{3}\b"),
    "requirements": re.compile(r"\bREQ-[A-Z]{2}-\d{3}\b"),
    "acceptance": re.compile(r"\bAS-\d{3}\b"),
    "adrs": re.compile(r"\bADR-[A-Z]{2}-\d{3}\b"),
}


def markdown_files() -> list[Path]:
    return sorted(DOCUMENTS.rglob("*.md"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def collect_definitions(files: Iterable[Path]) -> tuple[dict[str, set[str]], list[str]]:
    definitions: dict[str, set[str]] = {
        key: set() for key in DEFINITION_PATTERNS
    }
    locations: dict[tuple[str, str], list[str]] = defaultdict(list)

    for path in files:
        text = read_text(path)
        relative = str(path.relative_to(ROOT))
        for kind, pattern in DEFINITION_PATTERNS.items():
            for identifier in pattern.findall(text):
                definitions[kind].add(identifier)
                locations[(kind, identifier)].append(relative)

    errors: list[str] = []
    for (kind, identifier), paths in sorted(locations.items()):
        if len(paths) > 1:
            errors.append(
                f"Duplicate {kind} definition {identifier}: {', '.join(paths)}"
            )

    return definitions, errors


def validate_required_documents(data: dict) -> list[str]:
    errors: list[str] = []
    required = data.get("required_documents")
    if not isinstance(required, list) or not required:
        return ["Coverage file must contain a non-empty required_documents list."]

    seen: set[str] = set()
    for item in required:
        if not isinstance(item, str) or not item:
            errors.append("Every required_documents value must be a non-empty string.")
            continue
        if item in seen:
            errors.append(f"Duplicate required document entry: {item}")
        seen.add(item)
        if not (ROOT / item).is_file():
            errors.append(f"Required document is missing: {item}")

    return errors


def validate_identifier_list(
    principle_id: str,
    field: str,
    values: object,
    definitions: dict[str, set[str]],
) -> list[str]:
    errors: list[str] = []
    if not isinstance(values, list):
        return [f"{principle_id}.{field} must be a list."]

    known = definitions[field]
    for value in values:
        if not isinstance(value, str):
            errors.append(f"{principle_id}.{field} contains a non-string value.")
        elif value not in known:
            errors.append(f"{principle_id}.{field} references unknown ID {value}.")
    return errors


def validate_principles(data: dict, definitions: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    principles = data.get("principles")
    if not isinstance(principles, list) or not principles:
        return ["Coverage file must contain a non-empty principles list."]

    seen_ids: set[str] = set()
    for index, principle in enumerate(principles):
        if not isinstance(principle, dict):
            errors.append(f"principles[{index}] must be an object.")
            continue

        principle_id = principle.get("id")
        if not isinstance(principle_id, str) or not principle_id:
            errors.append(f"principles[{index}] has no valid id.")
            principle_id = f"principles[{index}]"
        elif principle_id in seen_ids:
            errors.append(f"Duplicate principle id: {principle_id}")
        else:
            seen_ids.add(principle_id)

        status = principle.get("status")
        if status not in STATUS_VALUES:
            errors.append(f"{principle_id} has invalid status {status!r}.")
            continue

        for field in ("stories", "requirements", "acceptance", "adrs"):
            errors.extend(
                validate_identifier_list(
                    principle_id, field, principle.get(field), definitions
                )
            )

        verification = principle.get("verification")
        if not isinstance(verification, list):
            errors.append(f"{principle_id}.verification must be a list.")
            verification = []

        if status in {"Specified", "Designed", "Verified"}:
            for field in ("stories", "requirements", "acceptance"):
                if not principle.get(field):
                    errors.append(
                        f"{principle_id} is {status} but has no {field} evidence."
                    )

        if status in {"Designed", "Verified"} and not principle.get("adrs"):
            errors.append(f"{principle_id} is {status} but has no ADR evidence.")

        if status == "Verified":
            if not verification:
                errors.append(
                    f"{principle_id} is Verified but has no verification evidence."
                )
            for evidence_path in verification:
                if not isinstance(evidence_path, str) or not (ROOT / evidence_path).exists():
                    errors.append(
                        f"{principle_id} verification evidence is missing: {evidence_path}"
                    )

        if status == "Excepted":
            exception = principle.get("exception")
            required_fields = {
                "scope",
                "authority",
                "rationale",
                "review_date",
                "expiration",
            }
            if not isinstance(exception, dict):
                errors.append(f"{principle_id} is Excepted but has no exception object.")
            else:
                missing = sorted(
                    field for field in required_fields if not exception.get(field)
                )
                if missing:
                    errors.append(
                        f"{principle_id} exception is missing: {', '.join(missing)}"
                    )

        if not principle.get("remaining_gap"):
            errors.append(f"{principle_id} must describe its remaining_gap.")

    return errors


def validate_document_references(
    files: Iterable[Path], definitions: dict[str, set[str]]
) -> list[str]:
    """Validate the most important downstream reference directions."""
    errors: list[str] = []

    for path in files:
        relative = str(path.relative_to(ROOT))
        text = read_text(path)

        if "requirements" in path.name:
            for story_id in sorted(set(REFERENCE_PATTERNS["stories"].findall(text))):
                if story_id not in definitions["stories"]:
                    errors.append(
                        f"{relative} references unknown Story ID {story_id}."
                    )

        if "acceptance" in path.name:
            for requirement_id in sorted(
                set(REFERENCE_PATTERNS["requirements"].findall(text))
            ):
                if requirement_id not in definitions["requirements"]:
                    errors.append(
                        f"{relative} references unknown Requirement ID {requirement_id}."
                    )
            for story_id in sorted(set(REFERENCE_PATTERNS["stories"].findall(text))):
                if story_id not in definitions["stories"]:
                    errors.append(
                        f"{relative} references unknown Story ID {story_id}."
                    )

    return errors


def main() -> int:
    errors: list[str] = []

    if not COVERAGE_FILE.is_file():
        print(f"ERROR: missing coverage file: {COVERAGE_FILE.relative_to(ROOT)}")
        return 1

    try:
        data = json.loads(read_text(COVERAGE_FILE))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: unable to read coverage file: {exc}")
        return 1

    files = markdown_files()
    definitions, duplicate_errors = collect_definitions(files)
    errors.extend(duplicate_errors)
    errors.extend(validate_required_documents(data))
    errors.extend(validate_principles(data, definitions))
    errors.extend(validate_document_references(files, definitions))

    if errors:
        print("Documentation validation failed:\n")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Documentation validation passed.")
    print(f"Markdown files scanned: {len(files)}")
    print(f"Stories defined: {len(definitions['stories'])}")
    print(f"Requirements defined: {len(definitions['requirements'])}")
    print(f"Acceptance features defined: {len(definitions['acceptance'])}")
    print(f"Acceptance scenarios defined: {len(definitions['acceptance_scenarios'])}")
    print(f"ADRs or ADR backlog items defined: {len(definitions['adrs'])}")
    print(f"Constitutional principles tracked: {len(data['principles'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
