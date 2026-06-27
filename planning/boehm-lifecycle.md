# Boehm Risk-Driven Delivery Model

## Purpose

This project uses Barry W. Boehm's work as a control framework rather than as a fixed sequence of waterfall stages. The plan combines four ideas:

1. **Risk-driven spiral cycles** — choose the next work according to the most important unresolved risk.
2. **Concurrent definition** — refine objectives, requirements, architecture, implementation, verification, and plans together where their risks interact.
3. **Stakeholder win conditions** — make success conditions and conflicts explicit before commitment.
4. **Anchor-point commitments** — increase commitment only when objective evidence supports it.

## One cycle

Every phase is a spiral cycle with the same decision structure.

### 1. Determine objectives, alternatives, and constraints

Record:

- the user or organizational outcome;
- stakeholder win conditions;
- applicable constitutional principles and requirements;
- viable implementation or operational alternatives;
- constraints, assumptions, dependencies, and non-goals.

### 2. Evaluate alternatives and resolve the dominant risks

For each serious risk, choose the least expensive evidence that can reduce uncertainty. Evidence may be a prototype, test, model, interface contract, data migration rehearsal, threat analysis, usability session, or operational experiment.

A document alone retires only a documentation risk. A behavior risk requires behavioral evidence.

### 3. Develop and verify the selected increment

Implement the smallest end-to-end increment that can produce the required evidence. Verification must trace to acceptance criteria and preserve exact revisions, authority, provenance, and history.

### 4. Review results and commit to the next cycle

Stakeholders review:

- delivered outcomes and evidence;
- unresolved risks and newly discovered risks;
- deviations from win conditions;
- cost and schedule observations;
- whether to proceed, redirect, hold, or stop.

The next phase is authorized only through a recorded gate decision.

## Anchor points

### Life Cycle Objectives (LCO)

LCO answers: **Is there a credible, shared definition of the problem, success conditions, scope, alternatives, constraints, and business rationale?**

For this repository, LCO requires:

- explicit stakeholder win conditions;
- an agreed slice sequence and release boundary;
- a truthful inventory of coded, specified, and unverified capabilities;
- an initial risk register;
- acceptance of Phase 0 evidence.

### Life Cycle Architecture (LCA)

LCA answers: **Is there an architecture and plan capable of satisfying the objectives while retiring the major technical and operational risks?**

For this repository, LCA requires evidence for:

- durable identity and revision semantics;
- authorization and omission-aware provenance;
- cross-context contracts for Slices 1 through 5;
- deterministic status and history derivation;
- data persistence, migration, and recovery;
- an executable verification strategy;
- a feasible plan for Slice 3 and end-to-end integration.

### Initial Operational Capability (IOC)

IOC answers: **Can the system operate for real users in a supported environment with acceptable risk?**

For this repository, IOC requires:

- a deployed pilot;
- runbooks, monitoring, recovery, and rollback;
- security and privacy review;
- user training and support ownership;
- accepted evidence for the end-to-end learning loop;
- a gate decision authorizing continued operation.

## Incremental commitment

Commitment grows with evidence:

| Commitment level | Allowed commitment |
|---|---|
| Exploration | Time-boxed analysis, prototypes, and spikes |
| Feasibility | Architecture work and limited implementation |
| Construction | Production implementation after LCA |
| Pilot | Limited operational use after release-candidate acceptance |
| Operation | Continued use after IOC and operational review |
| Expansion | Optional Slices 6 and 7 after separate risk and value decisions |

The project should not make production-scale commitments while foundational risks remain untested.

## Win conditions

At each gate, the following stakeholder groups must have explicit win conditions or an explicit exception:

- end users and contributors;
- decision makers and outcome owners;
- product and project leadership;
- engineering and architecture;
- security, privacy, and compliance;
- operations and support;
- maintainers and future integrators.

Conflicts are resolved by recording alternatives, trade-offs, and the authority for the selected decision.

## Tailoring rules for this repository

- The existing thin vertical slices remain the unit of user-visible scope.
- Phases are not identical to slices. A phase may verify several coded slices or retire a cross-cutting risk.
- Slices 1, 2, and 4 are not reimplemented by default. They first enter a verification and architecture-conformance phase.
- Slice 3 precedes a Slices 1–4 release candidate because it closes the missing execution path.
- Slice 5 follows the accepted Slices 1–4 chain and closes the organizational learning loop.
- Slices 6 and 7 are separate extension spirals and require their own value and risk decisions.

## Primary references

- Barry W. Boehm, “A Spiral Model of Software Development and Enhancement,” *Computer*, 21(5), 1988. https://doi.org/10.1109/2.59
- Barry W. Boehm, “Anchoring the Software Process,” *IEEE Software*, 13(4), 1996. https://doi.org/10.1109/52.526834
- Barry Boehm et al., “Using the WinWin Spiral Model: A Case Study,” *Computer*, 31(7), 1998. https://doi.org/10.1109/2.689675
