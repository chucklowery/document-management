# Project Constitution

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This project will create a unified system for preserving organizational knowledge, coordinating work, and learning from the relationship between evidence, decisions, investments, execution, and outcomes.

The system is founded on a shared graph of versioned Resources connected by explicit, typed Relationships. Documents, artifacts, knowledge records, objectives, initiatives, programs, projects, milestones, activities, parties, decisions, and outcomes may participate in this graph while retaining their distinct domain meanings, lifecycles, and rules.

The system will support two complementary capabilities:

1. **Document and Knowledge Management** — producing, maintaining, assembling, validating, publishing, and preserving complex specifications, evidence, decisions, and collaborative working materials.
2. **Work and Portfolio Intelligence** — planning, executing, measuring, and improving organizational work from strategic objectives through programs, projects, milestones, activities, investments, and observed outcomes.

These capabilities shall share identity, revision history, provenance, classification, authorization, search, and relationship infrastructure. They shall not be collapsed into a single undifferentiated domain model.

The system will enable people and automated agents to move through a durable organizational learning loop:

> **Sense → Interpret → Decide → Plan → Execute → Measure → Learn → Adapt**

Its ultimate purpose is not merely to store documents or report completed work, but to help organizations understand why work exists, what it costs, what it produces, what outcomes follow, and how new evidence should change future decisions.

## 2. Foundational System Model

The system is governed by one foundational pattern:

> **The system is a graph of versioned Resources connected by explicit Relationships and supplemented by immutable records of consequential events.**

The foundational model distinguishes:

1. **Resource** — the continuing identity of something managed by the system.
2. **Resource Revision** — an immutable recorded state of a Resource.
3. **Relationship** — an explicit, typed connection between Resources, Revisions, regions, or immutable records.
4. **Immutable Record** — a durable account of an event, decision, execution, publication, allocation, measurement, or other completed activity.
5. **Projection** — a reproducible current or historical view calculated from Resources, Revisions, Relationships, and immutable records.

Documents and projects are not interchangeable. They are different Resource kinds governed by different contracts. Their unification comes from participating in the same organizational knowledge-and-work graph.

## 3. Mission and Vision

### 3.1 Mission

Enable organizations to preserve knowledge, coordinate work, and improve decisions by connecting authoritative information, plans, execution, investments, verification, and outcomes in one traceable system.

### 3.2 Vision

1. Provide a comprehensive environment in which knowledge and work can be understood as connected parts of an organizational system rather than as isolated documents, tasks, and reports.
2. Preserve authoritative source material in open, text-based forms that are understandable to people and consumable, searchable, and interpretable by automated tools and large language models.
3. Enable complex working materials to be assembled into complete, versioned, reproducible publications for internal and external audiences.
4. Connect evidence, observations, findings, insights, recommendations, and decisions to the objectives, initiatives, programs, projects, activities, investments, deliverables, and outcomes they influence.
5. Enable leaders and contributors to understand what work is occurring, why it exists, who or what is responsible, what resources it consumes, and what outcomes it produces.
6. Treat charts, diagrams, datasets, spreadsheets, images, generated reports, plans, roadmaps, and other supporting materials as reusable, versioned Resources with metadata, provenance, and accessible descriptions.
7. Support concurrent human and automated contributors without silently losing durably saved, committed, approved, or otherwise acknowledged work.
8. Transform documentation into executable knowledge where appropriate, allowing structured information to define, generate, validate, and explain software, workflows, tests, analyses, and operational processes.
9. Build enduring institutional knowledge by preserving context, provenance, competing perspectives, relationships, assumptions, decisions, and observed results.
10. Help organizations answer the continuing question:

> **Where should we invest the next dollar, hour, and resource to produce the most valuable organizational outcomes?**

## 4. Objectives

1. Reduce the time and effort required to maintain complex specifications and working materials through controlled reuse rather than duplication.
2. Create an integrated ecosystem in which documents, artifacts, evidence, metadata, comments, plans, work records, outcomes, and Relationships can be discovered and incorporated into new work.
3. Connect strategic intent to operational execution without erasing the distinctions among objectives, programs, projects, milestones, activities, documents, decisions, and outcomes.
4. Support human and automated contributors without sacrificing clarity, reviewability, ownership, security, accountability, or traceability.
5. Enable deterministic assembly and reproducible publication of complex documents from distributed source materials.
6. Reduce documentation and specification drift by connecting behavioral claims to objective verification and executable examples where practical.
7. Preserve the lineage from source evidence through interpretations, specifications, decisions, investments, actions, deliverables, measurements, and outcomes.
8. Provide portfolio visibility into planned and actual effort, cost, capacity, classifications, dependencies, risks, and outcomes.
9. Distinguish intended outcomes from observed outcomes and preserve the evidence, assumptions, timing, and confidence associated with each.
10. Make delays, dependencies, unresolved changes, stale information, and conflicting perspectives visible while action remains possible.
11. Support organizational learning by comparing expected and observed results and feeding that evidence into revised knowledge, plans, and investment decisions.
12. Favor incremental delivery and empirical validation of complete workflows over conceptual expansion without operational feedback.

## 5. Constitutional Principles

### 5.1 The Resource Graph Is the Shared Foundation

Resources, Revisions, Relationships, immutable records, and projections form the common platform foundation. Domain-specific concepts shall use this foundation without being reduced to generic labels that erase their meaning.

### 5.2 Bounded Contexts Preserve Meaning

Document authoring, publication, knowledge synthesis, work planning, work execution, portfolio intelligence, outcome measurement, access governance, and external integration shall be modeled as explicit bounded contexts. Shared infrastructure shall not imply identical lifecycle rules, invariants, authority, or behavior.

### 5.3 Text Is Canonical Where Practical

Authoritative source documents, definitions, metadata, relationship declarations, policies, and configuration shall be maintained in open, text-based formats unless a documented architectural decision demonstrates that this is impractical. Rendered outputs and generated artifacts are derivative works and shall not replace their authoritative source.

### 5.4 Authority and Derivation Are Distinct

Authoritative source, imported records, projections, assembled documents, generated code, executable tests, verification results, published versions, reports, and rendered outputs shall remain distinguishable even when produced from a common graph.

### 5.5 Identity Is Independent of Location

Managed Resources, Content Regions, Relationships, and immutable records shall have stable identities that survive ordinary renaming, movement, repository restructuring, and changes in external storage location.

### 5.6 Durable States Are Historical, Not Overwritten

A durable state of a Resource shall be represented by an immutable Resource Revision or another explicit immutable record. Historical states, decisions, measurements, and conclusions shall not be silently rewritten to match later understanding.

### 5.7 No Acknowledged Work Is Silently Lost

Changes or contributions that have been durably saved, committed, approved, or otherwise acknowledged by the system shall never be silently overwritten or discarded. Concurrent human or automated changes shall be detected, preserved, and reconciled explicitly.

### 5.8 Relationships Are Explicit, Typed, and Traceable

Every managed Relationship shall identify its source, target, type, applicable version or selection policy, and relevant context. Relationship semantics shall be defined by domain contracts rather than inferred solely from display labels.

### 5.9 Provenance Is Preserved End to End

Evidence, synthesized findings, generated artifacts, requirements, recommendations, decisions, plans, allocations, actions, deliverables, measurements, and outcomes shall retain navigable provenance to their sources, transformations, contributors or generators, assumptions, and applicable versions.

### 5.10 Context Is Preserved

Managed knowledge and work shall preserve the context necessary to understand why it exists, by whom or what it was created, from what evidence, under what conditions, for what purpose, and in relation to which objectives, decisions, organizations, and outcomes.

### 5.11 Human and Machine Readability Remain Aligned

Structured content shall remain understandable to people while retaining sufficient semantics for automated indexing, transformation, generation, validation, reasoning, reporting, and analysis.

### 5.12 Dependencies Are Visible Before Change

People and automated agents shall be able to inspect relevant inbound and outbound dependencies and understand likely effects before shared knowledge, plans, rules, classifications, or other consequential Resources are changed or propagated.

### 5.13 Reference Adoption Is Governed

Content reuse shall support explicit reference modes, including Live Reference, Approval-Controlled Reference, and Pinned Reference. The selection of a mode shall reflect the cost of staleness, the cost of unintended change, and the authority of the destination.

### 5.14 Assembly Is Deterministic

Given the same source Revisions, dependency manifest, Artifact Revisions, and assembly configuration, the system shall produce the same Assembled Document. Assembly shall not permit unresolved references or inclusion cycles that prevent deterministic resolution.

### 5.15 Published Versions Are Reproducible and Immutable

Every Published Version shall be reproducible from an immutable record of its source Revisions, dependencies, Artifact Revisions, configuration, templates, and tool versions. A Published Version shall not be modified after release; corrections shall produce a new Published Version.

### 5.16 Artifacts Are First-Class Managed Resources

Artifacts shall have stable identity, metadata, revision history, ownership, provenance, integrity information, and accessible textual descriptions where applicable.

### 5.17 Execution Is Explicit and Controlled

Executable content shall be explicitly identified and executed only through controlled, reviewable, authorized, and auditable mechanisms. Ordinary prose shall not be executed merely because it appears in a managed Resource.

### 5.18 Behavioral Claims Should Be Verifiable

Behavioral Requirements and Acceptance Examples should be objectively verifiable and, where practical, executable. Explanatory, historical, legal, research, strategic, and rationale content need not be executable to remain valuable.

### 5.19 Verification Is Durable Evidence

Validation and execution processes shall produce durable Verification Results that identify the evaluated specification or claim, target, applicable versions, execution environment, time, outcome, and supporting evidence.

### 5.20 Generated Outputs Remain Traceable

Generated code, tests, diagrams, configurations, plans, reports, forecasts, and other outputs shall remain traceable to the Resources, Revisions, rules, data, and execution records from which they were produced.

### 5.21 Intent, Work, Output, and Outcome Are Distinct

The system shall distinguish:

- why work is proposed;
- what future condition is intended;
- what work is planned or performed;
- what deliverable or output is produced; and
- what outcome is later observed.

Completion of work or delivery of an output shall not by itself be treated as proof that an intended outcome occurred.

### 5.22 Organizational Learning Is a Closed Loop

The system shall support traceability through the loop:

> **Evidence → Interpretation → Decision → Plan → Execution → Measurement → Outcome → New Evidence**

Observed outcomes and contrary evidence shall be able to challenge assumptions, supersede conclusions, and influence future decisions without erasing the historical record.

### 5.23 Operational Events and Current Projections Are Distinct

Completed events such as publications, approvals, time entries, allocations, executions, measurements, and milestone acceptances shall be recorded durably. Current summaries such as remaining capacity, forecast cost, percent complete, and portfolio allocation shall be represented as reproducible projections rather than silently mutable historical truth.

### 5.24 Governance Is Proportional to Consequence

Controls shall increase with sensitivity, external exposure, regulatory or financial consequence, degree of reuse, automation privilege, and irreversibility. Exploratory work shall not automatically carry publication-grade process, and authoritative work shall not rely on informal controls.

### 5.25 Access Is Explicit and Auditable

Access to managed Resources, Revisions, Relationships, executable content, work records, financial information, Verification Results, and Published Versions shall be governed by explicit authorization policies. Privileged, sensitive, administrative, execution, allocation, approval, and publication actions shall be restricted and auditable.

### 5.26 Sensitive Information Is Governed

Personal, confidential, financial, restricted, or otherwise sensitive information shall be identifiable and subject to explicit policies for consent, access, redaction, disclosure, retention, and lifecycle handling.

### 5.27 External Authorities Remain Explicit

For each externally sourced concept, the system shall state whether it is authoritative, a replica, a projection, an index, a federation point, or a reference to another system of record. Integration shall not silently transfer authority.

### 5.28 Openness and Replaceability Are Preferred

The system should favor open formats, documented interfaces, exportability, and replaceable integrations. Domain concepts shall not be unnecessarily coupled to a repository host, rendering engine, project-management tool, financial system, testing framework, identity provider, or AI provider.

### 5.29 Empirical Learning Constrains Conceptual Expansion

New concepts, classifications, and controls should be justified by demonstrated workflows, domain needs, or explicit risks. The project shall prefer thin end-to-end capabilities that produce user feedback over indefinite expansion of an untested conceptual model.

### 5.30 System Health Must Be Observable

The system shall expose measures of its own behavior, including unresolved dependencies, stale references, pending approvals, publication reproducibility, verification coverage, outcome review status, provenance completeness, processing delays, and user bypass where observable and appropriate.

## 6. Scope

### 6.1 Shared Resource Graph

- Assign stable identities to managed Resources independently of location.
- Record immutable Resource Revisions and relevant parentage.
- Define typed Relationships among Resources, Revisions, Content Regions, and immutable records.
- Preserve metadata, classification, ownership, provenance, and lifecycle information.
- Support impact analysis, graph navigation, and historical reconstruction.
- Produce reproducible current and historical projections.

### 6.2 Document Authoring

- Create and modify text-based Source Documents.
- Identify Main Documents, Partial Documents, templates, and other document roles.
- Define addressable Content Regions within Documents.
- Apply metadata, tags, classifications, ownership, sensitivity markings, and access controls.
- Author structured content without requiring direct manipulation of source syntax.
- Preview Documents with resolved References and included partials.

### 6.3 Document Composition

- Compose Documents from other Documents, Content Regions, Artifacts, and generated results.
- Maintain explicit dependency Relationships.
- Support deterministic assembly of document graphs.
- Detect unresolved References and inclusion cycles.
- Support Live, Approval-Controlled, and Pinned References.
- Preserve source identity and provenance during composition.

### 6.4 Artifact Management

- Register, upload, classify, version, and reuse Artifacts.
- Associate metadata, ownership, sensitivity, provenance, integrity information, and textual descriptions with Artifacts.
- Reference Artifacts from Documents and other managed Resources.
- Preserve editable or generative source where available.
- Identify inbound and outbound Artifact dependencies.

### 6.5 Knowledge and Decision Management

- Link Evidence to Observations, Findings, Insights, Recommendations, Decisions, and Actions.
- Preserve contextual metadata, assumptions, confidence, competing perspectives, and source lineage.
- Permit contradictory or competing knowledge records to coexist.
- Record supersession without erasing historical interpretations.
- Connect Decisions to objectives, plans, actions, measurements, and outcomes.

### 6.6 Work and Portfolio Planning

- Represent Organizations, Teams, Objectives, Initiatives, Programs, Projects, Roadmaps, Milestones, and Activities.
- Connect work to strategic intent, Decisions, Requirements, Evidence, and intended Outcomes.
- Support dependency, capacity, resource, budget, forecast, and scenario planning.
- Support configurable classifications such as capitalized or operational, billable or non-billable, innovation, maintenance, technical debt, support, compliance, risk, and product development.
- Preserve planning assumptions and applicable effective periods.

### 6.7 Work Execution and Measurement

- Record planned and actual effort, assignments, time, cost, rate, status, risk, and completion evidence where applicable.
- Record completed operational events as immutable records.
- Connect Activities to Milestones, Projects, Programs, Decisions, Requirements, deliverables, and Outcomes.
- Support current operational projections without rewriting historical records.
- Identify delays, bottlenecks, conflicts, and unplanned work.

### 6.8 Financial and Resource Intelligence

- Represent budgets, allocations, labor costs, billable rates, capitalization treatment, forecasts, and expenditure records at appropriate levels.
- Distinguish authoritative financial records from imported, estimated, or projected values.
- Report investment and effort by Organization, Program, Project, classification, resource, and Outcome.
- Preserve the derivation of calculated cost, utilization, forecast, and allocation values.

### 6.9 Outcome Management and Organizational Learning

- Represent intended and observed Outcomes separately.
- Record success conditions, observation windows, measurements, attribution assumptions, confidence, and review dates.
- Compare expected and observed results.
- Connect outcomes and new evidence to the Decisions, assumptions, investments, and work that preceded them.
- Support review and adaptation of future plans based on observed results.

### 6.10 Search, Classification, and Navigation

- Search Resources, Revisions, Artifacts, metadata, comments, work records, Relationships, immutable records, and Published Versions.
- Navigate information as both a hierarchy and a graph.
- Discover related material without prior knowledge of storage location.
- Support tag-based, metadata-based, full-text, temporal, and relationship-based discovery.
- Respect authorization and sensitivity policies during search and navigation.

### 6.11 Collaboration and Review

- Comment on Resources, Revisions, Content Regions, Relationships, plans, work records, and Published Versions.
- Preserve discussions, annotations, Review Decisions, approvals, and follow-up Actions as managed information.
- Support concurrent human and automated contributors.
- Detect and reconcile conflicting changes without silent data loss.
- Preserve distinct contributor perspectives alongside synthesized conclusions.

### 6.12 Publication Process

- Assemble a Main Document and its resolved dependency graph.
- Validate References, policies, required metadata, approvals, and publication readiness.
- Create immutable, numbered Published Versions.
- Record source and dependency manifests for every Published Version.
- Produce one or more Rendered Outputs, including PDF, DOCX, HTML, and assembled text where configured.
- Preserve all information required to reproduce the Published Version.

### 6.13 Diagram and Visualization Management

- Create, upload, version, reference, and render diagrams and visualizations.
- Preserve diagram or visualization source where available.
- Associate visual resources with accessible textual descriptions and provenance.
- Generate visual projections from traceable source data where configured.

### 6.14 Executable Specifications and Validation

- Represent Behavioral Requirements through structured, readable examples.
- Execute explicitly designated specifications through controlled adapters or integrations.
- Record Verification Evidence and Verification Results.
- Integrate validation into delivery, review, and operational workflows where appropriate.
- Generate code, tests, configuration, reports, or other outputs from structured source when explicitly configured.

### 6.15 Security and Sensitive Information Management

- Define authorization policies for managed Resources and privileged actions.
- Mark and classify sensitive, personal, financial, or restricted information.
- Support consent, redaction, restricted disclosure, and lifecycle policies where applicable.
- Record access-sensitive and privileged operations for audit.

### 6.16 Integration and Federation

- Integrate with repositories, identity providers, project-management systems, financial systems, timekeeping tools, analytics platforms, testing systems, and publication tools through documented interfaces.
- Preserve external identifiers, synchronization state, authority, and provenance.
- Support export and reconstruction of authoritative information in open or documented forms.
- Detect and surface integration failures and stale synchronized information.

## 7. Bounded Contexts

The system shall define and maintain an explicit context map. At minimum, it shall distinguish:

1. Shared Resource Graph
2. Document Authoring and Composition
3. Publication
4. Knowledge and Provenance
5. Work Planning
6. Work Execution
7. Portfolio and Financial Intelligence
8. Outcome Measurement and Learning
9. Identity, Access, and Governance
10. Integration and External Systems

Each bounded context shall identify:

- its purpose and language;
- authoritative concepts and records;
- lifecycle states and transition authority;
- invariants and policies;
- information it consumes and produces;
- integration relationships with other contexts; and
- whether shared concepts are translated, referenced, projected, or directly reused.

## 8. Out of Scope Unless Explicitly Added

The following are not assumed to be part of the initial product unless later adopted through requirements and architectural decisions:

- Replacing Git or every external repository hosting platform.
- Replacing general-purpose accounting, payroll, human-resources, identity, or enterprise-resource-planning systems.
- Replacing every specialized project-management, issue-tracking, messaging, or real-time collaboration tool.
- General-purpose binary file editing.
- Uncontrolled execution of prose or uploaded content.
- Real-time character-by-character collaborative editing.
- Full enterprise records retention, legal hold, or regulatory disposition management.
- Public content distribution portals.
- Autonomous financial, staffing, or strategic decisions without explicit authority and governance.
- Treating correlation between work and outcomes as proven causation without supporting evidence and stated assumptions.
- A proprietary document or interchange format required for basic interoperability.

The system may integrate with or provide focused capabilities associated with these areas without assuming responsibility for the entire external domain.

Basic privacy, authorization, redaction, consent, financial-information protection, and sensitive-information handling remain in scope even though full enterprise records management and replacement of external systems are not.

## 9. Core Domain Dictionary

The Domain Glossary remains the canonical source for detailed terminology. The following definitions establish the constitutional foundation.

### Resource

The continuing identity of something managed by the system. A Resource has stable identity independent of its current name, location, representation, or Revision.

### Resource Revision

An immutable recorded state of a Resource at a particular point in its history.

### Relationship

An explicit, typed connection among Resources, Resource Revisions, Content Regions, or immutable records. A Relationship identifies its source, target, semantics, and applicable version-selection or effective-time rules.

### Immutable Record

A durable record of a completed event, decision, execution, publication, allocation, measurement, or outcome. A later event creates another record rather than modifying the completed event.

### Projection

A reproducible current or historical view calculated from Resources, Revisions, Relationships, and immutable records. A Projection is not automatically authoritative source information.

### Artifact

A managed Resource that contains or identifies supporting content that is not primarily authored document text, including images, diagrams, datasets, spreadsheets, generated charts, PDFs, recordings, source code, configurations, and supporting files.

### Source Document

A mutable-through-revision, text-based Resource used to communicate structured information. A Source Document may contain original content, metadata, executable declarations, Content Regions, and References to other Resources.

### Main Document

A Source Document designated as an entry point into a graph of Documents, Content Regions, Artifacts, and other Resources for Assembly or Publication.

### Partial Document

A Source Document intended primarily for reuse within one or more other Documents rather than as a standalone reading entry point.

### Content Region

A stably identified, explicitly bounded portion of a Source Document that may be referenced, reused, discussed, versioned, owned, or validated independently of the entire Document.

### Content Reference

A Relationship from a destination location to a Source Document, Content Region, Artifact, Published Version, or another supported Resource for reuse or inclusion.

### Live Reference

A Content Reference that resolves to the currently qualifying source Revision according to an explicit resolution policy.

### Approval-Controlled Reference

A Content Reference that detects qualifying source changes but requires explicit approval before the destination adopts them.

### Pinned Reference

A Content Reference that resolves to a specific immutable source Revision until deliberately changed.

### Assembled Document

The deterministic, resolved text representation produced by traversing a Main Document and its selected dependencies according to their Reference policies.

### Published Version

An immutable, numbered release record created by the Publication Process and containing or identifying an Assembled Document, source manifest, resolved dependency manifest, publication metadata, approvals, and one or more Rendered Outputs.

### Party

A person, Organization, Team, customer organization, vendor, or Automated Agent that participates in the system. Roles such as Author, Owner, Reviewer, Approver, Decision Maker, Contributor, or Assignee are contextual.

### Evidence

A Resource, Revision, record, measurement, or external source used to support or challenge an Observation, Finding, Insight, Recommendation, Decision, Requirement, plan, or Outcome claim.

### Decision

A recorded choice among alternatives, including rationale, authority, date, status, assumptions, and supporting Evidence where applicable.

### Objective

A desired future condition against which progress or success may be evaluated.

### Initiative

A coordinated commitment intended to advance one or more Objectives and commonly realized through Programs, Projects, or other organized work.

### Program

A coordinated collection of related Projects and other work managed together to advance Objectives and realize benefits or Outcomes not obtainable by managing the components independently.

### Project

A bounded, governed body of work undertaken to produce defined deliverables, capabilities, changes, or Outcomes within applicable constraints.

### Milestone

A significant planned or observed point in the lifecycle of a Program, Project, or other body of work, commonly associated with completion conditions or acceptance evidence.

### Activity

An atomic or otherwise schedulable unit of work that may consume effort or cost, produce an output, satisfy a Requirement, contribute to a Milestone, or influence an Outcome.

### Investment

A commitment or consumption of money, time, capacity, assets, or other organizational Resources in support of an Objective, Initiative, Program, Project, Activity, capability, or Outcome.

### Deliverable

A produced output, capability, service, document, Artifact, change, or other result of performed work. A Deliverable is distinct from the Outcome it is intended to influence.

### Outcome

A condition or effect associated with organizational activity. An Outcome shall be identified as intended or observed and may include success conditions, measurements, observation periods, attribution assumptions, and confidence.

### Verification Result

A durable record of executing or otherwise validating a specification, claim, Requirement, Acceptance Example, output, or Outcome measure against a target.

### Provenance

The recorded lineage connecting managed information and work to sources, contributors, transformations, Revisions, Evidence, Decisions, executions, investments, and resulting outputs or Outcomes.

### Acknowledged Work

A change or contribution that has been durably saved, committed, approved, or otherwise explicitly recognized by the system as retained work.

### Sensitive Information

Personal, confidential, financial, restricted, or otherwise protected information subject to explicit policies governing access, consent, redaction, disclosure, retention, and lifecycle handling.

## 10. Product Drivers and Supporting Needs

- [Customer Insight Documentation System Vision](./00.01-Interview-Constitution.md)
- [Referenced Content Management Vision](./00.02-Partial-References.md)
- [Documentation as Executable Code](./00.03.Literate-Programming.md)
- [Documentation as Test](./00.04.FitNesse.md)
- [Enterprise Work Intelligence System](./00.01.ProjectManagement.md)

These documents provide source needs, examples, and product drivers. They do not independently override this constitution. Conflicts shall be resolved in favor of this constitution unless the constitution is explicitly amended.

The reference-mode terminology used by downstream documents and requirements shall be normalized to **Live Reference**, **Approval-Controlled Reference**, and **Pinned Reference**.

Downstream documents shall distinguish Resource kinds, immutable records, and projections. They shall also distinguish intended Outcomes, Deliverables, completed work, and observed Outcomes.

## 11. Amendment Process

1. Proposed amendments shall identify the principle, scope boundary, bounded context, or definition being changed.
2. Each proposal shall explain its motivation and downstream effect on needs, the context map, glossary, user stories, requirements, domain models, acceptance criteria, architecture decisions, security, and integrations.
3. Amendments that broaden the system boundary shall identify the authority of external systems and the additional operational feedback needed to validate the expansion.
4. Approved amendments shall be committed with a clear rationale and revision history.
5. Requirements or architecture decisions that conflict with this constitution shall be revised or accompanied by an approved constitutional amendment.
6. Supporting-needs documents may propose new principles or scope, but they do not override this constitution without an approved amendment.
7. Changes that introduce or redefine canonical terms require a corresponding update to the Domain Glossary and, where boundaries change, the Context Map.
