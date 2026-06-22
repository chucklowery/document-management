# Constitutional Traceability Ledger

**Project:** Organizational Knowledge and Work System

## Purpose

This ledger records operational coverage for normative constitutional principles. A principle is not considered implemented merely because documentation exists.

## Status Values

- **Defined** — principle exists but implementation and verification are incomplete.
- **Specified** — linked stories, requirements, and acceptance specifications exist.
- **Designed** — required architecture decisions and interfaces are approved.
- **Verified** — implementation has current passing verification evidence.
- **Excepted** — an approved, scoped, time-bounded exception exists.
- **Uncovered** — required downstream coverage is missing.

## Hypertext Amendment Coverage

| Principle | Owning context | Stories | Requirements | Acceptance | Model or policy | Status | Remaining gap |
|---|---|---|---|---|---|---|---|
| Authored Relationship assertions | Shared Graph plus owning context | US-036, US-047 | REQ-HT-001, REQ-HT-021 | AS-010, AS-016 | `02.10-hypertext-domain-model-integration.md` | Specified | Relationship lifecycle and persistence ADRs |
| Bidirectional discovery | Shared Graph; Governance | US-036 | REQ-HT-001, REQ-HT-002 | AS-010 | `03-context-map.md` | Specified | Backlink indexing and authorization ADR |
| Canonical source versus plural views | Knowledge and Provenance | US-038 through US-040 | REQ-HT-005, REQ-HT-008, REQ-HT-009 | AS-011, AS-012 | `03.08-trail-domain-model.md` | Specified | Pilot validation and UI projections |
| Trails | Knowledge and Provenance | US-039 through US-041 | REQ-HT-006 through REQ-HT-011 | AS-011 | `03.08-trail-domain-model.md` | Specified | Open Trail ADRs and implementation |
| Derivative/source distinction | Document Authoring; Knowledge | US-038 | REQ-HT-005 | AS-012.1 | Canonical terms and domain integration | Specified | Representation metadata contract ADR |
| Disagreement and epistemic status | Knowledge and Provenance | US-040, US-042 | REQ-HT-008, REQ-HT-012 | AS-011.3, AS-012.2 | Canonical terms and domain integration | Specified | Claim and disagreement policy model |
| Omission-aware provenance | Knowledge and Provenance | US-042, US-044 | REQ-HT-012 through REQ-HT-014 | AS-012 | Amendment and canonical terms | Specified | Completeness and inference-risk ADR |
| Governed history | Governance plus owning context | US-043 | REQ-HT-015, REQ-HT-016 | AS-013 | Amendment and domain integration | Specified | Pilot-specific legal and retention policy |
| Human comprehension | All presentation contexts | US-038 through US-048 | Indirect coverage | Partial acceptance coverage | Amendment | Uncovered | Dedicated usability and explanation requirements |
| Attention governance | Governance | US-048 | REQ-HT-022 | Not yet defined | Amendment | Uncovered | Acceptance specifications and pilot policy |
| Portability and survivability | Integration; Shared Graph | US-045 | REQ-HT-017, REQ-HT-018 | AS-014 | Amendment and domain integration | Specified | Export format and reconstruction ADR |
| Automation governance | Governance | US-046 | REQ-HT-019, REQ-HT-020 | AS-015 | Amendment and domain integration | Specified | Delegation and instruction-reference ADR |
| Shared Graph boundary | Shared Graph; all contexts | US-036 through US-048 | Cross-cutting | Cross-cutting | `03-context-map.md` | Defined | Architecture conformance checks |
| Explainable consequential change | All consequential contexts | US-047 | REQ-HT-021 | AS-016 | Amendment | Specified | Change-explanation schema and enforcement |
| Stewardship cost | All owning contexts | Partial | Partial | None | Amendment | Uncovered | Stories, requirements, acceptance, metrics |
| Constitutional operationalization | Documentation and Governance | US-036 through US-048 | REQ-HT-023 | AS-017 | This ledger | Specified | Automated repository validator |

## Required Repository Validation

The documentation build shall eventually fail when:

1. a normative principle has no ledger entry;
2. a ledger entry marked Specified lacks a valid story, requirement, or acceptance reference;
3. a requirement references an unknown story;
4. an acceptance specification references an unknown requirement;
5. an architecture decision required by a Designed entry is missing or superseded;
6. a Verified entry lacks current verification evidence;
7. an exception lacks scope, authority, rationale, review date, or expiration;
8. an expired exception remains active.

## Immediate Actions

1. Add dedicated human-comprehension stories, requirements, and acceptance scenarios.
2. Add attention-governance acceptance scenarios.
3. Add stewardship-cost stories, requirements, and acceptance scenarios.
4. Create the foundational ADR set.
5. Implement a machine-readable ledger and repository validator.
6. Validate the model using the customer-interview-to-decision pilot.
