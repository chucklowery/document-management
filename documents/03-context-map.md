# Context Map

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines the initial bounded-context hypothesis for the Organizational Knowledge and Work System.

The contexts share stable identity, revision addressing, relationship addressing, provenance references, authority designations, immutable-record conventions, and authorization-aware discovery conventions. They do not share one universal lifecycle, schema, datastore, transaction model, epistemic model, or authority model.

Context boundaries may change through an Architectural Decision Record when operational learning shows that a different boundary better preserves language, authority, invariants, or delivery independence.

## 2. Contexts

### 2.1 Shared Graph Foundation

**Purpose:** Provide cross-context identity, revision addressing, relationship addressing, immutable-record references, provenance references, authority designation, backlink discovery conventions, and projection provenance.

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
- Cross-context addressability conventions
- Authorization-aware relationship discovery conventions

**Does not own:** document lifecycle, Trail meaning, project status, financial rules, publication workflow, outcome semantics, domain-specific Relationship meaning, or context-specific authority.

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

Document Authoring may provide Trail editing and rendering capabilities but does not own Trail semantics, adoption, or epistemic authority.

### 2.3 Publication

**Purpose:** Validate and freeze reproducible, immutable releases.

**Owns:**

- Publication Candidate
- Publication Assessment
- Published Version
- Publication Number
- Rendered Output association

**Consumes:** assembled documents, exact Revisions, manifests, policies, approvals, templates, and adopted Trail Revisions when a Trail is used as a publication source.

### 2.4 Knowledge and Provenance

**Purpose:** Preserve Evidence, interpretations, decisions, competing perspectives, authored reasoning paths, and lineage.

**Owns:**

- Observation
- Finding
- Insight
- Recommendation
- Decision
- Action
- Trail
- Trail Revision
- Trail Step semantics
- Trail Review
- Trail Adoption
- Supports
- Contradicts
- Derived From
- Supersedes

**Produces:** evidence-to-decision paths, alternative interpretations, omission-aware provenance, Trail projections, and decision lineage.

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

**Purpose:** Govern actors, roles, authorization, sensitivity, consent, disclosure, redaction, retention, completeness disclosure, automation authority, and consequential decisions.

**Owns:**

- Party authentication reference
- Authorization Policy
- Role Assignment
- Sensitivity Classification
- Consent Decision
- Disclosure Decision
- Redaction Decision
- Retention and erasure policy
- Inference-risk policy
- Completeness-disclosure policy
- Automation Delegation
- Governance Decision

### 2.10 Integration and External Systems

**Purpose:** Preserve external identity, authority, synchronization, translation, and portability across system boundaries.

**Owns:**

- External System Reference
- External Identity Mapping
- Synchronization Record
- Authority Mapping
- Translation Contract
- Integration Failure Record
- Portability Export Contract
- Reconstruction Contract

## 3. Context Relationships

### Shared Kernel

All contexts share identifiers, temporal references, authority designations, provenance-addressing conventions, and relationship-addressing conventions from the Shared Graph Foundation.

The Shared Kernel does not make context-specific Relationship semantics, epistemic states, lifecycle states, or authority scopes universal.

### Customer–Supplier

- Publication consumes assemblies from Document Authoring and Composition.
- Publication may consume exact adopted Trail Revisions from Knowledge and Provenance.
- Work Execution consumes plans from Work Planning.
- Outcome Measurement consumes intended Outcomes from Work Planning and execution facts from Work Execution.
- Portfolio Intelligence consumes plans, execution records, and outcome measurements.
- Knowledge and Provenance consumes Evidence references from all producing contexts without taking ownership of their authoritative records.
- Identity, Access, and Governance supplies authorization, completeness-disclosure, retention, consent, and automation-delegation policy to all contexts.
- Integration and External Systems supplies portability and reconstruction contracts without transferring source authority.

### Anti-Corruption Layers

External project, financial, identity, testing, repository, AI, and publication systems are translated into local context language. External authority is preserved explicitly.

A context may conform directly to an external model only when the authority, coupling, exit cost, and effect on portability are documented.

## 4. Cross-Context Rules

1. Cross-context links use stable identities rather than internal object references.
2. A context does not mutate another context's authoritative record.
3. Imported information identifies its source, authority, applicable version, and synchronization state.
4. Projections, Trails, summaries, and generated answers do not become authoritative facts merely because they are prominent, adopted, published, popular, or widely displayed.
5. Context translations preserve source identity, applicable version, time, provenance, and material transformation.
6. Context-specific states are not promoted into the Shared Kernel without demonstrated cross-context semantics.
7. A shared term with different meanings in two contexts must be translated explicitly.
8. Relationship discoverability from both directions does not transfer ownership or mutation authority.
9. Authorization filtering shall follow completeness-disclosure and inference-risk policy and shall not silently claim complete knowledge.
10. Trail ordering does not alter source ordering, ownership, authority, or meaning.
11. Automated contributions preserve delegated authority, material inputs, governing instruction or policy reference, relevant tool or model version, limitations, and provenance.
12. Cross-context portability preserves documented semantics while enforcing consent, sensitivity, retention, deletion, and legal restrictions.

## 5. Initial Delivery Sequence

1. Shared Graph Foundation
2. Document Authoring and Composition
3. Knowledge and Provenance, including linear Trail authoring and comparison
4. Work Planning
5. Work Execution
6. Outcome Measurement and Learning
7. Portfolio and Financial Intelligence
8. Publication and integration expansion

The sequence is a delivery hypothesis, not a dependency mandate. Each increment shall be validated through an end-to-end journey rather than by isolated context completion.