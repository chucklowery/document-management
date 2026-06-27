# Architecture Decision Index

**Project:** Organizational Knowledge and Work System

## Purpose

This index records accepted and pending architecture decisions required to move the repository from specification into implementation.

The machine-readable registry is `traceability/architecture-decisions.json`.

## Accepted Decisions

### ADR-HT-001 — Durable Identity Strategy

- **Status:** Accepted
- **Document:** `13.01-adr-ht-001-durable-identity-strategy.md`
- **Decision:** Principal managed identities use opaque UUID version 7 identifiers. Resource and Revision identities remain distinct, content digests are integrity metadata rather than identity, external identifiers remain mappings, and identifiers are never reused.
- **Remaining work:** Implement and verify identity behavior.

### ADR-HT-002 — Canonical Semantic Serialization

- **Status:** Accepted
- **Document:** `13.02-adr-ht-002-canonical-semantic-serialization.md`
- **Decision:** Cross-context semantic exchange uses versioned JSON envelopes. Integrity operations use RFC 8785 canonicalization. Contracts declare identity, version, payload, provenance, and governed extensions. Markdown and visual editors are adapters or projections rather than the sole semantic authority.
- **Conformance baseline:**
  - `examples/semantic-envelope/roundtrip-fixtures.json`
  - `scripts/verify_semantic_roundtrip.py`
- **Remaining work:** Implement production adapters and independent cross-language conformance tests.

## Next Decisions

### ADR-HT-003 — Content Region Anchoring

Decide how a stable Content Region identity maps to occurrences across edits, movement, splitting, merging, concurrent revisions, and conflicting changes without silently targeting the wrong content.

### ADR-HT-004 — Relationship Persistence and Lifecycle

Decide how asserted, continuing, effective-dated, immutable, derived, corrected, superseded, withdrawn, and restricted Relationships are persisted and resolved.

### ADR-HT-005 — Backlink and Reverse-Dependency Indexing

Decide how inbound discovery is generated, refreshed, authorized, audited, and protected against inference leakage.

## Delivery Gate

The first production implementation increment shall not begin until ADR-HT-003 through ADR-HT-005 are accepted and the first Evidence-to-Decision context contracts are defined.

The intended first walking slice remains:

```text
Source Evidence
→ Content Region
→ Finding
→ Recommendation
→ Authorized Decision
```
