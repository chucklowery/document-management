# Opinion Article OP-SM-002 — Retrieval Boundaries Should Evolve Without Becoming a Distributed Monolith

**Status:** Interpretive  
**Primary counterpoint:** Martin Fowler  
**Associated principles:** SM-P-001, SM-P-004, SM-P-008, SM-P-012  
**Associated requirements:** REQ-IR-001, REQ-IR-002, REQ-IR-007, REQ-IO-001, REQ-SM-012, REQ-SM-014  
**Associated decisions:** ADR-IR-001

## Position

A shared retrieval experience is valuable, but one search model must not become an accidental universal domain model.

Retrieval needs cross-context identity, selected text, metadata, Relationships, temporal information, and authorization. That creates pressure to copy internal context schemas into one centralized index and to treat the index as the easiest place to implement domain behavior. The result may initially appear efficient while coupling every context to one evolving representation.

## Fowler Counterpoint

From an evolutionary architecture and bounded-context perspective, duplicated read models are acceptable when their derivation is explicit and replaceable. The danger is not duplication itself; it is hidden ownership and a model that must change in lockstep with every producer.

The retrieval context should consume published contracts, tolerate schema evolution, and build purpose-specific projections. Source contexts should own language, invariants, lifecycle, and correction. Retrieval should own query and ranking semantics. Integration should own translation. Operations should own propagation and rebuild evidence.

## Opinion

Start with the smallest retrieval projection that validates real user tasks. Do not begin with a universal enterprise ontology or a requirement that all contexts share one runtime schema.

A retrieval index may denormalize aggressively, but it must retain source identity, contract version, transformation provenance, and rebuild capability. New ranking features should depend on published semantics rather than private database fields. When operational learning shows the boundary is wrong, change it through an ADR and migration rather than preserving a poor abstraction for consistency's sake.
