# Delivery Planning

This directory is the delivery-control layer for the Organizational Knowledge and Work System.

The normative product definition remains under [`../documents/`](../documents/). This directory does not override the constitution, domain language, requirements, acceptance specifications, or architecture decisions. It turns those materials into a risk-driven implementation sequence with explicit evidence and commitment gates.

## Start here

1. [`boehm-lifecycle.md`](boehm-lifecycle.md) — how Barry W. Boehm's risk-driven principles are applied.
2. [`phase-plan.md`](phase-plan.md) — the ordered implementation plan.
3. [`deliverables.json`](deliverables.json) — machine-readable phases, deliverables, status, dependencies, and evidence requirements.
4. [`risks.json`](risks.json) — machine-readable active risk register.
5. [`templates/deliverable-record.md`](templates/deliverable-record.md) — evidence packet for one deliverable.
6. [`templates/phase-gate-review.md`](templates/phase-gate-review.md) — decision record for a phase boundary.

## Current implementation baseline

The working baseline records Slices 1, 2, and 4 as coded. They are treated as **coded but not accepted** until their behavior is demonstrated against the repository's requirements and acceptance specifications and the evidence is linked from the deliverables ledger.

Slice 3 is the highest-priority functional gap because it connects approved plans to produced deliverables. Without it, the coded slices cannot form a complete Evidence → Decision → Plan → Execution → Deliverable → Outcome Review chain.

## Control rules

- Work begins only when the phase entry criteria are met.
- Every deliverable has one owner role, acceptance criteria, and required evidence.
- A phase cannot pass because code exists; it passes when the required evidence is accepted.
- The next phase is selected by the highest remaining project risk, not by document order.
- A gate decision is one of: `go`, `go-with-conditions`, `redirect`, `hold`, or `stop`.
- A failed experiment or rejected deliverable remains recorded. History is not rewritten.
- Changes to the constitutional or domain specification continue to follow the authority rules in [`../documents/00-project-constitution.md`](../documents/00-project-constitution.md).

## Status vocabulary

| State | Meaning |
|---|---|
| `proposed` | Identified but not yet authorized for execution |
| `ready` | Entry criteria and dependencies are satisfied |
| `in_progress` | Active work is underway |
| `evidence_pending` | Implementation exists, but acceptance evidence is incomplete |
| `accepted` | Acceptance criteria and evidence have been approved |
| `rejected` | Reviewed and not accepted |
| `deferred` | Intentionally postponed |
| `cancelled` | Explicitly ended and retained for history |

## Validation

Run both repository validators:

```bash
python scripts/validate_all_documentation.py
python scripts/validate_delivery_plan.py
```

The delivery validator checks phase, deliverable, dependency, and risk references. It does not claim that implementation evidence is true; gate reviewers must inspect that evidence.
