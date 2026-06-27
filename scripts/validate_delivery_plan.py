#!/usr/bin/env python3
"""Validate phase, deliverable, dependency, and risk references."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: str) -> dict:
    target = ROOT / path
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: root must be an object")
    return value


def index(items: object, label: str, errors: list[str]) -> dict[str, dict]:
    if not isinstance(items, list) or not items:
        errors.append(f"{label} must be a non-empty list")
        return {}
    result: dict[str, dict] = {}
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"{label}[{position}] must be an object")
            continue
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            errors.append(f"{label}[{position}] has no valid id")
        elif item_id in result:
            errors.append(f"duplicate {label} id: {item_id}")
        else:
            result[item_id] = item
    return result


def main() -> int:
    try:
        plan = load("planning/deliverables.json")
        register = load("planning/risks.json")
    except ValueError as exc:
        print(f"Delivery plan validation failed:\n- {exc}")
        return 1

    errors: list[str] = []
    phases = index(plan.get("phases"), "phases", errors)
    deliverables = index(plan.get("deliverables"), "deliverables", errors)
    risks = index(register.get("risks"), "risks", errors)

    statuses = set(plan.get("status_values", []))
    risk_statuses = set(register.get("status_values", []))
    listed: set[str] = set()

    defaults = plan.get("defaults")
    if not isinstance(defaults, dict) or not defaults.get("acceptance"):
        errors.append("defaults.acceptance is required")
    if not isinstance(defaults, dict) or not defaults.get("required_evidence"):
        errors.append("defaults.required_evidence is required")

    for phase_id, phase in phases.items():
        if phase.get("status") not in statuses:
            errors.append(f"{phase_id} has invalid status")
        phase_deliverables = phase.get("deliverables")
        phase_risks = phase.get("primary_risks")
        if not isinstance(phase_deliverables, list) or not phase_deliverables:
            errors.append(f"{phase_id} has no deliverables")
            phase_deliverables = []
        if not isinstance(phase_risks, list) or not phase_risks:
            errors.append(f"{phase_id} has no primary risks")
            phase_risks = []
        for deliverable_id in phase_deliverables:
            listed.add(deliverable_id)
            item = deliverables.get(deliverable_id)
            if item is None:
                errors.append(f"{phase_id} references unknown {deliverable_id}")
            elif item.get("phase") != phase_id:
                errors.append(f"{deliverable_id} is assigned to the wrong phase")
        for risk_id in phase_risks:
            if risk_id not in risks:
                errors.append(f"{phase_id} references unknown {risk_id}")

    for deliverable_id, item in deliverables.items():
        if item.get("phase") not in phases:
            errors.append(f"{deliverable_id} references an unknown phase")
        if item.get("status") not in statuses:
            errors.append(f"{deliverable_id} has invalid status")
        if deliverable_id not in listed:
            errors.append(f"{deliverable_id} is not listed by a phase")
        dependencies = item.get("depends_on")
        if not isinstance(dependencies, list):
            errors.append(f"{deliverable_id}.depends_on must be a list")
            continue
        for dependency in dependencies:
            if dependency not in deliverables:
                errors.append(f"{deliverable_id} depends on unknown {dependency}")
            if dependency == deliverable_id:
                errors.append(f"{deliverable_id} depends on itself")

    for risk_id, item in risks.items():
        if item.get("phase") not in phases:
            errors.append(f"{risk_id} references an unknown phase")
        if item.get("status") not in risk_statuses:
            errors.append(f"{risk_id} has invalid status")
        for field in ("probability", "impact", "exposure", "owner_role", "trigger", "mitigation"):
            if not item.get(field):
                errors.append(f"{risk_id}.{field} is required")
        if not item.get("retirement_evidence"):
            errors.append(f"{risk_id}.retirement_evidence is required")

    if errors:
        print("Delivery plan validation failed:\n")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Delivery plan validation passed.")
    print(f"Phases: {len(phases)}")
    print(f"Deliverables: {len(deliverables)}")
    print(f"Risks: {len(risks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
