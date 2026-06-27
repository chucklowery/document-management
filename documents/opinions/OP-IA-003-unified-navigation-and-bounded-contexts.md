# Opinion Article OP-IA-003 — Unified Navigation Must Not Become a Unified Domain Model

**Status:** Interpretive  
**Primary counterpoint:** Martin Fowler  
**Associated principles:** IA-P-002, IA-P-003, IA-P-011  
**Associated requirements:** REQ-IA-003, REQ-IA-005, REQ-IA-016  
**Associated decisions:** ADR-IA-001

## Position

A coherent information experience benefits from consistent navigation, labels, and interaction patterns across the system.

## Fowler Counterpoint

Bounded contexts preserve distinct language, ownership, invariants, and rates of change. A global navigation schema can become a distributed monolith when every domain must conform to one taxonomy or release together.

## Opinion

The system should compose navigation from published contracts and contextual mappings. Shared interaction conventions are desirable; shared private models are not. When terms differ, expose context and translation rather than forcing false equivalence.