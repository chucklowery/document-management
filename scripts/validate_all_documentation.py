#!/usr/bin/env python3
"""Run documentation validation across all coverage ledgers."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import validate_documentation as base

ROOT = Path(__file__).resolve().parents[1]
TRACEABILITY = ROOT / "traceability"


def main() -> int:
    errors: list[str] = []
    markdown = base.markdown_files()
    definitions, duplicate_errors = base.collect_definitions(markdown)
    errors.extend(duplicate_errors)

    coverage_files = sorted(TRACEABILITY.glob("*-coverage.json"))
    if not coverage_files:
        print("ERROR: no coverage ledgers found")
        return 1

    principle_ids: set[str] = set()
    principle_count = 0

    for path in coverage_files:
        try:
            data = json.loads(base.read_text(path))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Unable to read {path.relative_to(ROOT)}: {exc}")
            continue

        required = data.get("required_documents", [])
        principles = data.get("principles", [])
        if required:
            errors.extend(base.validate_required_documents(data))
        if not isinstance(principles, list):
            errors.append(f"{path.relative_to(ROOT)}.principles must be a list.")
            continue
        if not principles:
            continue

        for principle in principles:
            if isinstance(principle, dict):
                principle_id = principle.get("id")
                if isinstance(principle_id, str):
                    if principle_id in principle_ids:
                        errors.append(f"Duplicate principle id across ledgers: {principle_id}")
                    principle_ids.add(principle_id)
        principle_count += len(principles)
        errors.extend(base.validate_principles(data, definitions))

    errors.extend(base.validate_document_references(markdown, definitions))

    if errors:
        print("Documentation validation failed:\n")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Documentation validation passed.")
    print(f"Coverage ledgers loaded: {len(coverage_files)}")
    print(f"Markdown files scanned: {len(markdown)}")
    print(f"Stories defined: {len(definitions['stories'])}")
    print(f"Requirements defined: {len(definitions['requirements'])}")
    print(f"Acceptance features defined: {len(definitions['acceptance'])}")
    print(f"ADRs defined: {len(definitions['adrs'])}")
    print(f"Constitutional principles tracked: {principle_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
