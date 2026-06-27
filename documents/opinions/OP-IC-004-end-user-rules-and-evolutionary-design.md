# Opinion Article OP-IC-004 — End-User Rules Must Not Become a Hidden Universal Platform

**Status:** Interpretive  
**Counterpoint:** Martin Fowler  
**Associated principles:** IC-P-004, IC-P-005, IC-P-007, IC-P-008  
**Associated requirements:** REQ-IC-002, REQ-IC-003, REQ-IC-004, REQ-IC-006, REQ-IC-009, REQ-IC-017, REQ-IC-021

A Fowler-oriented counterpoint is that a convenient rule builder can become a second programming environment and an accidental central domain model.

The project should keep configurable behavior aligned with published contracts and bounded-context ownership. Simple rules may remain user-configurable; behavior that requires private domain knowledge, complex branching, or coordinated migration should move behind an owned service or an explicit architecture decision.

The interface may simplify authoring, but it should not hide coupling or force every context to evolve through one shared runtime model.