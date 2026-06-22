# Context Map

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines the initial bounded-context hypothesis for the Organizational Knowledge and Work System.

The contexts share stable identity, revision addressing, relationship semantics, provenance references, authority designations, and immutable-record conventions. They do not share one universal lifecycle, schema, datastore, or transaction model.

Context boundaries may change through an Architectural Decision Record when operational learning shows that a different boundary better preserves language, authority, invariants, or delivery independence.

## 2. Contexts

### 2.1 Shared Graph Foundation

**Purpose:** Provide cross-context identity, revision addressing, relationship addressing, immutable-record references, provenance references, authority designation, and projection provenance.

**Owns:**

- Resource Identity
- Revision Identity
- Relationship Identity
- Immutable Record Identity
- External Identity
- Authority Designation
- Recorded Time
- Effective Period
- Provenance Reference
- Classification Reference

**Does not own:** document lifecycle, project status, financial rules, publication workflow, or outcome semantics.

### 2.2 Document Authoring and Composition

**Purpose:** Author, revise, reference, assemble, and reuse structured Documents and Artifacts.

**Owns:**

- Document
- Document Revision
- Artifact
- Artifact Revision
- Content Region
- Reference Declaration
- Reference Subscription
- Assembly
- Resolution Manifest

**Produces:** assembled documents, source manifests, dependency manifests, provenance, and impact information.

### 2.3 Publication

**Purpose:** Validate and freeze reproducible, immutable releases.

**Owns:**

- Publication Candidate
- Publication Assessment
- Published Version
- Publication Number
- Rendered Output association

**Consumes:** assembled documents, exact Revisions, manifests, policies, approvals, and templates.

### 2.4 Knowledge and Provenance

**Purpose:** Preserve Evidence, interpretations, decisions, competing perspectives, and lineage.

**Owns:**

- Observation
- Finding
- Insight
- Recommendation
- Decision
- Action
- Supports
- Contradicts
- Derived From
- Supersedes

### 2.5 Work Planning

**Purpose:** Express why work exists and how intended work is organized.

**Owns:**

- Objective
- Initiative
- Program
- Project
- Roadmap
- Milestone
- Activity Plan
- Plan Revision
- Planning Assumption
- Intended Outcome reference

### 2.6 Work Execution

**Purpose:** Record assignments, performed work, delivery, delays, risks, and completion Evidence.

**Owns:**

- Work Assignment
- Work Event
- Time Entry
- Milestone Acceptance
- Deliverable Production
- Blockage Observation
- Completion Record

### 2.7 Portfolio and Financial Intelligence

**Purpose:** Explain planned and actual investment, capacity, cost, allocation, and forecast across portfolios.

**Owns:**

- Budget
- Allocation
- Capacity Plan
- Rate
- Estimate
- Commitment
- Expenditure reference
- Forecast Definition
- Portfolio Projection

Authoritative accounting and payroll records may remain external.

### 2.8 Outcome Measurement and Learning

**Purpose:** Distinguish intended Outcomes from observed Outcomes and support learning from measured results.

**Owns:**

- Intended Outcome
- Observed Outcome
- Measurement Definition
- Measurement Record
- Observation Window
- Success Condition
- Attribution Assumption
- Outcome Review

### 2.9 Identity, Access, and Governance

**Purpose:** Govern actors, roles, authorization, sensitivity, consent, disclosure, redaction, and consequential decisions.

**Owns:**

- Party authentication reference
- Authorization Policy
- Role Assignment
- Sensitivity Classification
- Consent Decision
- Disclosure Decision
- Redaction Decision
- Governance Decision

### 2.10 Integration and External Systems

**Purpose:** Preserve external identity, authority, synchronization, and translation across system boundaries.

**Owns:**

- External System Reference
- External Identity Mapping
- Synchronization Record
- Authority Mapping
- Translation Contract
- Integration Failure Record

## 3. Context Relationships

### Shared Kernel

All contexts share identifiers, temporal references, authority designations, and provenance-addressing conventions from the Shared Graph Foundation.

### Customer–Supplier

- Publication consumes assemblies from Document Authoring and Composition.
- Work Execution consumes plans from Work Planning.
- Outcome Measurement consumes intended Outcomes from Work Planning and execution facts from Work Execution.
- Portfolio Intelligence consumes plans, execution records, and outcome measurements.

### Anti-Corruption Layers

External project, financial, identity, testing, and repository systems are translated into local context language. External authority is preserved explicitly.

A context may conform directly to an external model only when the authority, coupling, and exit cost are documented.

## 4. Cross-Context Rules

1. Cross-context links use stable identities rather than internal object references.
2. A context does not mutate another context's authoritative record.
3. Imported information identifies its source and authority.
4. Projections do not become authoritative facts merely because they are widely displayed.
5. Context translations preserve source identity, applicable version, time, and provenance.
6. Context-specific states are not promoted into the Shared Kernel without demonstrated cross-context semantics.
7. A shared term with different meanings in two contexts must be translated explicitly.

## 5. Initial Delivery Sequence

1. Shared Graph Foundation
2. Document Authoring and Composition
3. Knowledge and Provenance
4. Work Planning
5. Work Execution
6. Outcome Measurement and Learning
7. Portfolio and Financial Intelligence
8. Publication and integration expansion

The sequence is a delivery hypothesis, not a dependency mandate.
