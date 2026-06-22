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
| Derivative/source distinction | Document Authoring; Knowledge | US-038, US-051 | REQ-HT-005, REQ-HT-025, REQ-HT-028 | AS-012, AS-018 | Canonical terms and domain integration | Specified | Representation metadata and serialization ADR |
| Disagreement and epistemic status | Knowledge and Provenance | US-040, US-042, US-049 | REQ-HT-008, REQ-HT-012, REQ-HT-025 | AS-011, AS-012, AS-018 | Canonical terms and domain integration | Specified | Claim and disagreement policy model |
| Omission-aware provenance | Knowledge and Provenance | US-042, US-044, US-051 | REQ-HT-012 through REQ-HT-014, REQ-HT-028 | AS-012, AS-018 | Amendment and canonical terms | Specified | Completeness and inference-risk ADR |
| Governed history | Governance plus owning context | US-043, US-058 | REQ-HT-015, REQ-HT-016, REQ-HT-038 | AS-013, AS-020 | Amendment and domain integration | Specified | Pilot-specific legal and retention policy |
| Human comprehension | All presentation contexts | US-049 through US-051 | REQ-HT-024 through REQ-HT-028 | AS-018 | Comprehension stories and acceptance specifications | Specified | Pilot usability measures and verification evidence |
| Attention governance | Governance | US-048, US-052 through US-054 | REQ-HT-022, REQ-HT-029 through REQ-HT-032 | AS-019 | Attention stories and acceptance specifications | Specified | Pilot notification policy and verification evidence |
| Portability and survivability | Integration; Shared Graph | US-045 | REQ-HT-017, REQ-HT-018 | AS-014 | Amendment and domain integration | Specified | Export format and reconstruction ADR |
| Automation governance | Governance | US-046 | REQ-HT-019, REQ-HT-020 | AS-015 | Amendment and domain integration | Specified | Delegation and instruction-reference ADR |
| Shared Graph boundary | Shared Graph; all contexts | US-036 through US-058 | Cross-cutting | Cross-cutting | `03-context-map.md`, ADR-HT-001 | Defined | Durable identity is decided; remaining graph ADRs and conformance checks are open |
| Explainable consequential change | All consequential contexts | US-047, US-049 | REQ-HT-021, REQ-HT-024 | AS-016, AS-018 | Amendment | Specified | Change-explanation schema and enforcement |
| Stewardship cost | All owning contexts | US-055 through US-058 | REQ-HT-033 through REQ-HT-039 | AS-020 | Stewardship stories and acceptance specifications | Specified | Pilot review policy, cost signals, and verification evidence |
| Constitutional operationalization | Documentation and Governance | US-036 through US-058 | REQ-HT-023 | AS-017 | This ledger and machine-readable coverage | Specified | Confirm workflow execution and require the validation check |

## Architecture Decision Status

| Decision | Status | Document | Remaining work |
|---|---|---|---|
| ADR-HT-001 — Durable Identity Strategy | Accepted | `13.01-adr-ht-001-durable-identity-strategy.md` | Implement and verify identity behavior |
| ADR-HT-002 through ADR-HT-005 | Backlog | `10.10-hypertext-architecture-decision-backlog.md` | Decide serialization, anchoring, Relationship lifecycle, and backlinks |
| ADR-HT-006 through ADR-HT-013 | Backlog | `10.10-hypertext-architecture-decision-backlog.md` | Decide Trail, authority, completeness, publication, history, automation, portability, and conformance details |

## Required Repository Validation

The documentation build shall fail when:

1. a normative principle has no machine-readable ledger entry;
2. a ledger entry marked Specified lacks a valid story, requirement, or acceptance reference;
3. a requirement references an unknown story;
4. an acceptance specification references an unknown requirement;
5. an architecture decision required by a Designed entry is missing or superseded;
6. a Verified entry lacks current verification evidence;
7. an exception lacks scope, authority, rationale, review date, or expiration;
8. an expired exception remains active.

## Immediate Actions

1. Confirm the documentation-validation workflow executes on `main` and configure it as a required check.
2. Approve ADR-HT-002 through ADR-HT-005.
3. Define semantic context contracts for the first walking slice.
4. Implement and validate the customer-interview-to-decision pilot.
5. Record current verification evidence before moving any principle to Verified.
