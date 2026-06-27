# Organizational Knowledge and Work System

This repository defines and implements a system for preserving organizational knowledge, coordinating work, finding changing information, and connecting evidence, decisions, plans, execution, deliverables, measurements, outcomes, and learning.

## Current delivery status

Slices 1, 2, and 4 are recorded as coded. They are classified as **coded but not accepted** until reproducible evidence demonstrates conformance with the current requirements, acceptance specifications, authority rules, revision semantics, and provenance rules.

Slice 3 is the next construction priority because it closes the missing path between approved plans and produced deliverables.

| Slice | Capability | Planning state |
|---|---|---|
| 1 | Evidence to Decision | Coded; verification packet required |
| 2 | Decision to Planned Work | Coded; verification packet required |
| 3 | Planned Work to Deliverable | Planned after Life Cycle Architecture approval |
| 4 | Deliverable to Outcome Review | Coded; verification packet required |
| 5 | Learning to Adaptation | Planned after the Slices 1–4 release candidate |
| 6 | Reproducible Publication | Deferred extension spiral |
| 7 | Minimal Investment Traceability | Deferred extension spiral |

## Start here

### Delivery and implementation

1. [`planning/README.md`](planning/README.md) — delivery-control index.
2. [`planning/boehm-lifecycle.md`](planning/boehm-lifecycle.md) — Boehm risk-driven lifecycle and anchor points.
3. [`planning/phase-plan.md`](planning/phase-plan.md) — phased implementation plan.
4. [`planning/deliverables.json`](planning/deliverables.json) — machine-readable deliverable ledger.
5. [`planning/risks.json`](planning/risks.json) — machine-readable risk register.
6. [`documents/06-thin-vertical-slices.md`](documents/06-thin-vertical-slices.md) — user-visible slice definitions.

### Constitutional authority

1. [`documents/00-project-constitution.md`](documents/00-project-constitution.md)
2. [`documents/00.05-constitution-amendment-context-and-delivery.md`](documents/00.05-constitution-amendment-context-and-delivery.md)
3. [`documents/00.06-hypertext-knowledge-integrity-amendment.md`](documents/00.06-hypertext-knowledge-integrity-amendment.md)
4. [`documents/00.08-information-retrieval-and-system-management-amendment.md`](documents/00.08-information-retrieval-and-system-management-amendment.md)
5. [`documents/00.09-findability-and-information-architecture-amendment.md`](documents/00.09-findability-and-information-architecture-amendment.md)
6. [`documents/00.10-literate-operations-and-contract-evolution-amendment.md`](documents/00.10-literate-operations-and-contract-evolution-amendment.md)
7. [`documents/00.07-constitutional-amendment-index.md`](documents/00.07-constitutional-amendment-index.md)

Downstream stories, requirements, designs, plans, projections, and implementations do not override constitutional authority.

## Repository organization

| Path | Purpose |
|---|---|
| [`documents/`](documents/) | Normative constitution, language, models, stories, requirements, acceptance specifications, ADRs, and traceability narratives |
| [`planning/`](planning/) | Risk-driven phases, deliverables, gate rules, templates, and active risks |
| [`traceability/`](traceability/) | Machine-readable constitutional coverage ledgers |
| [`scripts/`](scripts/) | Documentation and delivery-plan validation |
| [`.github/workflows/`](.github/workflows/) | Validation on direct pushes to `main` and pull requests |

## Boehm phase map

The implementation plan uses risk-driven spiral cycles and three commitment anchors:

- **LCO — Life Cycle Objectives:** establish stakeholder win conditions, release boundaries, alternatives, constraints, and a truthful baseline.
- **LCA — Life Cycle Architecture:** accept the shared architecture and retire the major identity, revision, authorization, persistence, integration, and recovery risks.
- **IOC — Initial Operational Capability:** operate a bounded, supported pilot and decide whether to continue, redirect, hold, or stop.

```text
P0 LCO baseline
→ P1 verify coded Slices 1, 2, and 4
→ P2 LCA
→ P3 implement Slice 3
→ P4 integrate and accept Slices 1–4
→ P5 implement Slice 5
→ P6 IOC pilot
→ P7 separately evaluate Slices 6 and 7
```

A phase passes only when its required evidence is accepted. Code presence alone is not an exit criterion.

## Reading paths

### Foundation

- [`documents/01-domain-glossary.md`](documents/01-domain-glossary.md)
- [`documents/02-domain-model.md`](documents/02-domain-model.md)
- [`documents/03-context-map.md`](documents/03-context-map.md)
- [`documents/04-cross-context-invariants.md`](documents/04-cross-context-invariants.md)

### Retrieval and system management

- [`documents/01.11-information-retrieval-and-system-management-canonical-terms.md`](documents/01.11-information-retrieval-and-system-management-canonical-terms.md)
- [`documents/02.11-information-retrieval-and-system-management-domain-model.md`](documents/02.11-information-retrieval-and-system-management-domain-model.md)
- [`documents/08.05-information-retrieval-and-system-management-user-stories.md`](documents/08.05-information-retrieval-and-system-management-user-stories.md)
- [`documents/09.12-information-retrieval-requirements-ears.md`](documents/09.12-information-retrieval-requirements-ears.md)
- [`documents/09.13-change-processing-and-consistency-requirements-ears.md`](documents/09.13-change-processing-and-consistency-requirements-ears.md)
- [`documents/09.14-input-output-and-delivery-requirements-ears.md`](documents/09.14-input-output-and-delivery-requirements-ears.md)
- [`documents/09.15-operations-and-recovery-requirements-ears.md`](documents/09.15-operations-and-recovery-requirements-ears.md)
- [`documents/09.16-derived-store-security-requirements-ears.md`](documents/09.16-derived-store-security-requirements-ears.md)

### Findability

- [`documents/00.09-findability-and-information-architecture-amendment.md`](documents/00.09-findability-and-information-architecture-amendment.md)
- [`documents/12.02-findability-traceability.md`](documents/12.02-findability-traceability.md)

### Literate operations

- [`documents/01.13-literate-operations-canonical-terms.md`](documents/01.13-literate-operations-canonical-terms.md)
- [`documents/02.13-literate-operations-domain-model.md`](documents/02.13-literate-operations-domain-model.md)
- [`documents/03.12-literate-operations-context-extension.md`](documents/03.12-literate-operations-context-extension.md)
- [`documents/05.04-literate-operations-role-extension.md`](documents/05.04-literate-operations-role-extension.md)
- [`documents/08.07-literate-operations-user-stories.md`](documents/08.07-literate-operations-user-stories.md)
- [`documents/09.26-literate-operations-requirements.md`](documents/09.26-literate-operations-requirements.md)
- [`documents/10.13-operations-adrs.md`](documents/10.13-operations-adrs.md)
- [`documents/11.23a-contract-acceptance.md`](documents/11.23a-contract-acceptance.md)
- [`documents/11.23-acceptance.md`](documents/11.23-acceptance.md)
- [`documents/11.23c-narrative.md`](documents/11.23c-narrative.md)
- [`documents/12.03.md`](documents/12.03.md)

## Validation

Run:

```bash
python scripts/validate_all_documentation.py
python scripts/validate_delivery_plan.py
```

The first command validates the constitutional and documentation ledgers. The second validates phase, deliverable, dependency, and risk references. Neither validator substitutes for review of implementation evidence.
