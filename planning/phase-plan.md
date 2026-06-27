# Risk-Driven Phase Plan

## Planning premise

The repository already contains extensive constitutional, domain, requirements, acceptance, architecture, and traceability material. Slices 1, 2, and 4 are recorded as coded. The immediate work is therefore not a document-first restart. It is to establish objective evidence, stabilize the shared architecture, close the missing Slice 3 execution path, and then operate the complete learning loop.

The machine-readable source of truth for phase and deliverable status is [`deliverables.json`](deliverables.json). The active risk register is [`risks.json`](risks.json).

## Phase sequence

| Phase | Boehm control point | Primary outcome | Initial state |
|---|---|---|---|
| P0 | LCO | Establish a truthful baseline and stakeholder commitment | In progress |
| P1 | Risk-retirement cycle | Verify coded Slices 1, 2, and 4 | Proposed |
| P2 | LCA | Accept the shared architecture and construction plan | Proposed |
| P3 | Construction cycle | Implement Slice 3, Planned Work to Deliverable | Proposed |
| P4 | Integrated release cycle | Accept an end-to-end Slices 1–4 release candidate | Proposed |
| P5 | Learning-loop cycle | Implement Slice 5, Learning to Adaptation | Proposed |
| P6 | IOC | Operate a supported pilot | Proposed |
| P7 | Separate extension spirals | Evaluate and, if justified, deliver Slices 6 and 7 | Deferred |

The sequence may change only through a gate decision that cites changed risk evidence.

---

## P0 — Life Cycle Objectives baseline

### Objective

Create a shared, reviewable statement of what exists, what is merely specified, what is coded, what is verified, and what outcome the first operational release must achieve.

### Dominant risks

- coded capability may not match the current specification;
- stakeholders may use different definitions of success;
- current documentation may overstate implementation maturity;
- later work may optimize for architecture completeness rather than user value.

### Deliverables

- **P0-D01** — repository and implementation baseline;
- **P0-D02** — stakeholder win-condition record;
- **P0-D03** — initial risk register;
- **P0-D04** — LCO gate review.

### Entry criteria

- repository access is available;
- the constitution and slice roadmap are identifiable;
- current implementation claims can be stated.

### Exit criteria

- Slices 1, 2, and 4 are explicitly classified as coded, verified, partially verified, or unsupported by evidence;
- the first release outcome and boundaries are agreed;
- top risks have owners and retirement evidence;
- an LCO gate decision is recorded.

### Gate

A `go` or `go-with-conditions` decision authorizes P1. A `redirect` decision revises the release boundary before further implementation.

---

## P1 — Verify coded Slices 1, 2, and 4

### Objective

Convert “coded” into inspectable, reproducible evidence and identify specification, architecture, and integration variances before more construction.

### Dominant risks

- happy paths exist but authority, history, provenance, or failure behavior is incomplete;
- each slice uses incompatible identifiers, persistence rules, or domain terms;
- tests demonstrate implementation details rather than user outcomes;
- Slice 4 assumes work and deliverable records that Slice 3 has not yet supplied.

### Deliverables

- **P1-D01** — Slice 1 verification packet;
- **P1-D02** — Slice 2 verification packet;
- **P1-D03** — Slice 4 verification packet;
- **P1-D04** — cross-slice variance and defect log;
- **P1-D05** — evidence index linking tests, demos, logs, and exact revisions.

### Required evidence

Each slice packet must include:

- the executable entry point or deployed interface;
- tests mapped to the slice acceptance examples;
- authorization-denial evidence;
- history and provenance evidence;
- persistence and recovery behavior;
- known deviations and deferred behavior;
- a reproducible demonstration procedure.

### Exit criteria

- every coded slice has an accepted, rejected, or conditionally accepted packet;
- unverified behavior is not labeled complete;
- shared inconsistencies are translated into P2 architecture risks;
- Slice 3 integration assumptions are explicit.

### Gate

P2 may begin when the verification evidence is sufficient to choose architecture alternatives. Full slice acceptance is desirable but is not required if all remaining gaps are bounded and owned.

---

## P2 — Life Cycle Architecture

### Objective

Accept the minimum shared architecture that can support Slices 1 through 5 and the operational pilot without irreversible, high-risk divergence.

### Dominant risks

- identity and revision semantics cannot preserve history across contexts;
- authorization filters leak omitted resources through backlinks or derived views;
- cross-context contracts cannot evolve safely;
- persistence, migration, and recovery are undefined;
- the architecture becomes a generalized platform before the user workflow is proven.

### Deliverables

- **P2-D01** — LCA architecture baseline;
- **P2-D02** — versioned cross-context contracts;
- **P2-D03** — persistence, revision, migration, and recovery plan;
- **P2-D04** — security, privacy, authorization, and provenance analysis;
- **P2-D05** — LCA gate review.

### Exit criteria

- the architecture satisfies or explicitly excepts the applicable constitutional rules;
- highest technical risks have executable evidence;
- Slice 3 can be implemented without redesigning the accepted core;
- integration and test environments are defined;
- construction cost and sequence are credible;
- an LCA gate decision authorizes P3.

### Gate

Production-oriented construction does not proceed without `go` or `go-with-conditions` at LCA.

---

## P3 — Implement Slice 3: Planned Work to Deliverable

### Objective

Close the missing middle of the value stream so approved plans produce immutable execution evidence and exact deliverable revisions.

### Dominant risks

- mutable status replaces event-derived state;
- assignment is mistaken for execution;
- completion is mistaken for outcome achievement;
- deliverables cannot be traced to the exact plan, work events, and revision;
- reopen and blockage behavior corrupts history.

### Deliverables

- **P3-D01** — Slice 3 implementation;
- **P3-D02** — automated Slice 3 acceptance suite;
- **P3-D03** — adapters and migrations needed to integrate Slices 2, 3, and 4;
- **P3-D04** — Slice 3 verification and demonstration packet.

### Exit criteria

- the Slice 3 happy path works end to end;
- work events are immutable and status is derived;
- produced deliverables identify exact revisions;
- completion and outcome achievement remain distinct;
- blocked and reopened work preserve history;
- the packet is accepted at the phase review.

---

## P4 — Integrated Slices 1–4 release candidate

### Objective

Demonstrate one complete Evidence → Decision → Plan → Execution → Deliverable → Measurement → Outcome Review chain.

### Dominant risks

- individually working slices fail when identifiers, authority, or temporal semantics cross boundaries;
- partial failure leaves inconsistent records;
- the full provenance chain is too difficult for users to understand;
- recovery and observability are insufficient for a pilot.

### Deliverables

- **P4-D01** — canonical end-to-end scenario and seed data;
- **P4-D02** — integrated acceptance and contract-test suite;
- **P4-D03** — observability, reconciliation, and recovery controls;
- **P4-D04** — Slices 1–4 release candidate;
- **P4-D05** — integrated release gate review.

### Exit criteria

- the canonical scenario is reproducible from a clean environment;
- exact revisions and authority are visible across the chain;
- denied or omitted information is not leaked;
- partial failures are recoverable or explicitly reconciled;
- the release candidate satisfies the agreed pilot boundary.

---

## P5 — Implement Slice 5: Learning to Adaptation

### Objective

Close the organizational learning loop by converting an Outcome Review into an explicit, authorized change while preserving prior decisions and plans.

### Dominant risks

- learning changes authoritative resources without a decision;
- supersession and revision rules are inconsistent;
- disagreement and non-adoption are lost;
- users cannot navigate the complete loop without overload.

### Deliverables

- **P5-D01** — Learning Record and adoption workflow;
- **P5-D02** — supersession, challenge, and revision rules;
- **P5-D03** — Slice 5 automated acceptance suite;
- **P5-D04** — complete learning-loop demonstration packet.

### Exit criteria

- learning can exist without automatic adoption;
- adoption requires explicit authority;
- original assumptions, decisions, and plans remain visible;
- revised plans retain provenance to the review and learning;
- the complete loop is demonstrable and understandable.

---

## P6 — Initial Operational Capability pilot

### Objective

Operate the accepted learning loop for real users in a bounded, supported pilot.

### Dominant risks

- security, privacy, support, and recovery are inadequate outside development;
- operational data invalidates architecture assumptions;
- user behavior differs from acceptance-test behavior;
- ownership after deployment is unclear.

### Deliverables

- **P6-D01** — pilot charter, participants, outcomes, and constraints;
- **P6-D02** — deployment and operations runbook;
- **P6-D03** — security, privacy, and data-handling review;
- **P6-D04** — monitoring, support, backup, rollback, and incident process;
- **P6-D05** — IOC gate review and continuation decision.

### Exit criteria

- a bounded pilot has run with named support and data owners;
- service, security, usability, and recovery observations are recorded;
- critical defects are closed or accepted with authority;
- the IOC gate authorizes continuation, redirect, or shutdown.

---

## P7 — Extension tracks for Slices 6 and 7

### Objective

Treat Reproducible Publication and Minimal Investment Traceability as separate value/risk spirals after the core learning loop is operational.

### Deliverables

- **P7-D01** — Slice 6 value case and risk spike;
- **P7-D02** — Slice 6 increment, only if authorized;
- **P7-D03** — Slice 7 value case and risk spike;
- **P7-D04** — Slice 7 increment, only if authorized;
- **P7-D05** — separate extension gate records.

### Rules

- neither extension is bundled into IOC by default;
- each extension has its own stakeholders, risks, acceptance evidence, and stop decision;
- financial traceability does not expand into general accounting without a new LCO/LCA cycle;
- publication does not weaken exact-revision and reproducibility requirements.

## Deliverable evidence storage

Evidence should be committed under a stable path such as:

```text
evidence/
  P1-D01/
    deliverable.md
    test-results/
    demo/
    known-deviations.md
```

Large or sensitive artifacts may be stored externally, but the repository record must contain an immutable identifier, access rule, checksum where practical, and retention owner.

## Change control

Update `deliverables.json` and `risks.json` in the same change whenever:

- a deliverable state changes;
- a dependency is added or removed;
- a risk is opened, escalated, transferred, accepted, or retired;
- a phase gate decision changes the sequence;
- acceptance evidence is added or invalidated.
