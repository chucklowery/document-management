# Organizational Knowledge and Work System

This repository contains the constitutional, domain, workflow, requirements, architecture, and acceptance specifications for a system that preserves organizational knowledge, coordinates work, and learns from the relationship among evidence, decisions, plans, execution, investments, outputs, and outcomes.

The repository is currently **specified with initial architecture decisions, but not yet implemented**. Documents describe intended behavior and constraints; they are not evidence that the system has been built or verified.

## Constitutional authority

The following documents jointly define the project's constitutional authority:

1. [`documents/00-project-constitution.md`](documents/00-project-constitution.md)
2. [`documents/00.05-constitution-amendment-context-and-delivery.md`](documents/00.05-constitution-amendment-context-and-delivery.md)
3. [`documents/00.06-hypertext-knowledge-integrity-amendment.md`](documents/00.06-hypertext-knowledge-integrity-amendment.md)
4. [`documents/00.07-constitutional-amendment-index.md`](documents/00.07-constitutional-amendment-index.md)

A downstream story, requirement, design, implementation, projection, or automated answer does not override the Constitution or a ratified amendment.

## Recommended reading order

### 1. Language and foundational model

- [`documents/01-domain-glossary.md`](documents/01-domain-glossary.md)
- [`documents/01.10-hypertext-canonical-terms.md`](documents/01.10-hypertext-canonical-terms.md)
- [`documents/02-domain-model.md`](documents/02-domain-model.md)
- [`documents/02.10-hypertext-domain-model-integration.md`](documents/02.10-hypertext-domain-model-integration.md)

### 2. Bounded contexts and invariants

- [`documents/03-context-map.md`](documents/03-context-map.md)
- [`documents/03.01-shared-foundation-domain-model.md`](documents/03.01-shared-foundation-domain-model.md)
- [`documents/03.04-work-planning-domain-model.md`](documents/03.04-work-planning-domain-model.md)
- [`documents/03.05-work-execution-domain-model.md`](documents/03.05-work-execution-domain-model.md)
- [`documents/03.06-outcome-learning-domain-model.md`](documents/03.06-outcome-learning-domain-model.md)
- [`documents/03.08-trail-domain-model.md`](documents/03.08-trail-domain-model.md)
- [`documents/04-cross-context-invariants.md`](documents/04-cross-context-invariants.md)

### 3. Delivery model and user intent

- [`documents/05-user-roles.md`](documents/05-user-roles.md)
- [`documents/05.01-hypertext-role-extension.md`](documents/05.01-hypertext-role-extension.md)
- [`documents/06-thin-vertical-slices.md`](documents/06-thin-vertical-slices.md)
- [`documents/07-user-story-map.md`](documents/07-user-story-map.md)
- [`documents/08-user-stories.md`](documents/08-user-stories.md)
- [`documents/08.01-hypertext-user-stories.md`](documents/08.01-hypertext-user-stories.md)
- [`documents/08.02-human-comprehension-user-stories.md`](documents/08.02-human-comprehension-user-stories.md)
- [`documents/08.03-attention-governance-user-stories.md`](documents/08.03-attention-governance-user-stories.md)
- [`documents/08.04-stewardship-cost-user-stories.md`](documents/08.04-stewardship-cost-user-stories.md)

### 4. Formal specification and validation

- [`documents/09-requirements-ears.md`](documents/09-requirements-ears.md)
- [`documents/09.10-hypertext-requirements-ears.md`](documents/09.10-hypertext-requirements-ears.md)
- [`documents/09.11-comprehension-attention-stewardship-requirements-ears.md`](documents/09.11-comprehension-attention-stewardship-requirements-ears.md)
- [`documents/10-domain-scenarios.md`](documents/10-domain-scenarios.md)
- [`documents/11-acceptance-specifications.md`](documents/11-acceptance-specifications.md)
- [`documents/11.10-hypertext-acceptance-specifications.md`](documents/11.10-hypertext-acceptance-specifications.md)
- [`documents/11.11-comprehension-attention-stewardship-acceptance-specifications.md`](documents/11.11-comprehension-attention-stewardship-acceptance-specifications.md)
- [`documents/12-constitutional-traceability-ledger.md`](documents/12-constitutional-traceability-ledger.md)

### 5. Architecture decisions

- [`documents/10.10-hypertext-architecture-decision-backlog.md`](documents/10.10-hypertext-architecture-decision-backlog.md)
- [`documents/13.01-adr-ht-001-durable-identity-strategy.md`](documents/13.01-adr-ht-001-durable-identity-strategy.md)

## Core value stream

```text
Evidence
→ Interpretation
→ Decision
→ Plan
→ Execution
→ Deliverable
→ Measurement
→ Outcome Review
→ Learning
→ Adaptation
```

The central distinctions are:

- intent is not work;
- a plan is not execution;
- completion is not acceptance;
- an output is not an outcome;
- measurement is not interpretation;
- correlation is not causation;
- learning is not adoption;
- a source is not one of its views;
- a Relationship does not establish truth merely by existing.

## Current maturity

| Layer | State |
|---|---|
| Constitution and amendments | Established |
| Canonical language | Established; consolidation remains |
| Context map and ownership | Established |
| Domain models | Established for the initial learning loop; some contexts remain less detailed |
| User stories | Specified, including all current constitutional principles |
| EARS requirements | Specified |
| Acceptance specifications | Specified |
| Architecture decisions | ADR-HT-001 accepted; remaining foundational ADRs open |
| Implementation | Not demonstrated |
| Automated verification | Repository validator exists; workflow execution still needs confirmation |
| Pilot evidence | Not yet recorded |

See [`documents/12-constitutional-traceability-ledger.md`](documents/12-constitutional-traceability-ledger.md) for principle-level status.

## Next delivery sequence

1. Confirm the documentation-validation workflow executes on `main` and configure it as a required check.
2. Approve the remaining graph and reference foundation decisions:
   - ADR-HT-002 — Canonical Semantic Serialization
   - ADR-HT-003 — Content Region Anchoring
   - ADR-HT-004 — Relationship Persistence and Lifecycle
   - ADR-HT-005 — Backlink and Reverse-Dependency Indexing
3. Define semantic contracts between bounded contexts for the first walking slice.
4. Implement the first walking slice:

```text
Source Evidence
→ Content Region
→ Finding
→ Recommendation
→ Authorized Decision
```

5. Demonstrate durable identity, authorization-aware backlinks, one linear Trail, omission-aware provenance, denial of unauthorized decisions, and navigation back to exact Evidence.
6. Record verification evidence before changing any constitutional principle to `Verified`.

## Documentation validation

The machine-readable coverage file is:

- [`traceability/constitutional-coverage.json`](traceability/constitutional-coverage.json)

Run validation locally with:

```bash
python scripts/validate_documentation.py
```

The validator checks:

- required documentation files exist;
- every machine-readable coverage entry has valid status data;
- referenced Story, Requirement, Acceptance, and ADR identifiers are defined;
- entries marked `Specified`, `Designed`, `Verified`, or `Excepted` satisfy the minimum evidence required by that status.

## Contribution rules

1. New normative principles must be added to the constitutional coverage ledger.
2. New stories, requirements, acceptance specifications, and ADRs must use stable identifiers.
3. A requirement must reference a known story.
4. An acceptance specification must reference known requirements.
5. A new domain concept must update its owning domain model or explain why it remains local to a workflow.
6. Consequential changes must preserve rationale, authority, affected assumptions, dependencies, and superseded scope.
7. No document should claim implementation or verification without current evidence.
