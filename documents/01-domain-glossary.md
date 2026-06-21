# Domain Glossary

**Project:** Document Management

## 1. Purpose

This glossary defines the canonical language used throughout the Document Management project. Its purpose is to create a shared vocabulary for product analysis, user stories, requirements, domain models, acceptance criteria, architecture decisions, implementation, and testing.

Terms in this document describe the business and product domain. They should not be interpreted as implementation classes, database tables, API resources, or user-interface controls unless a later design explicitly establishes that mapping.

When a downstream document uses a term differently from this glossary, the difference must be stated explicitly or the glossary must be amended.

## 2. Language Conventions

- A capitalized term refers to a formally defined domain concept in this glossary.
- Singular nouns name concepts; plural nouns refer to collections of those concepts.
- A Resource is identified independently of its repository path.
- A Version identifies a historical state of a Resource.
- A Reference identifies a managed relationship rather than a copied value.
- Source material is distinguished from assembled, generated, verified, and rendered results.
- The canonical reference modes are **Live Reference**, **Approval-Controlled Reference**, and **Pinned Reference**.
- The terms *managed reference* and *snapshot reference* should not be used in new requirements. Use **Approval-Controlled Reference** and **Pinned Reference** respectively.

## 3. Core Resource Concepts

### Managed Resource

An identifiable item governed by the system. A Managed Resource has a stable identity, metadata, history, ownership, provenance, and applicable authorization and lifecycle policies.

Documents, Artifacts, Content Regions, Published Versions, Comments, Verification Results, and selected knowledge entities may be Managed Resources.

### Resource Identity

A stable identifier assigned to a Managed Resource. Resource Identity is independent of the Resource's name, repository path, folder, display title, or current Version.

### Resource Version

An identifiable historical state of a Managed Resource. A Resource Version records enough information to distinguish that state from earlier and later states.

A Resource Version may be mutable while it is being prepared only when the system explicitly models it as a draft. A Published Version is always immutable.

### Repository

A managed storage environment containing Source Documents, Artifacts, Metadata, Relationships, Comments, Published Versions, and other Resources used by the system.

Repository is a domain concept and is not limited to a specific Git host, source-control provider, storage product, or filesystem layout.

### Repository Path

The current hierarchical location of a Resource within a Repository. A Repository Path is not the Resource's stable identity and may change without changing that identity.

### Library

A meaningful collection of related Main Documents, Source Documents, Artifacts, Published Versions, and Relationships organized around a shared subject, product, initiative, or body of knowledge.

A Resource may participate in more than one Library.

### Folder

A hierarchical organizational container within a Repository. A Folder helps people navigate Resources but does not define all semantic relationships among them.

## 4. Document Concepts

### Document

The general domain concept for a text-based Managed Resource intended to communicate structured information.

Use a more specific term—such as Source Document, Main Document, Partial Document, Aggregated Document, Assembled Document, or Published Version—when the distinction matters.

### Source Document

A mutable, text-based Managed Resource used for authoring. A Source Document may contain original content, Metadata, Executable Declarations, Content Regions, and References to other Documents, Artifacts, or Published Versions.

### Main Document

A Source Document designated as an entry point into a graph of Documents, Content Regions, and Artifacts for Assembly or Publication.

A Main Document identifies where a reader or Publication Process begins. It does not need to contain all content directly.

### Partial Document

A Source Document intended primarily for reuse within one or more other Documents rather than as a standalone reading entry point.

A Partial Document is independently identified and versioned. It differs from a Content Region, which is an addressable portion within a Source Document.

### Aggregated Document

A Source Document containing original content together with explicit Inclusion Points for other Documents, Content Regions, or Artifacts.

### Assembled Document

The deterministic, resolved text representation produced by traversing a Main Document and its selected Dependencies according to their Reference Policies.

An Assembled Document is derived from Source Documents and Artifacts. It is not itself the authoritative authoring source.

### Document Graph

The directed graph formed by Documents, Artifacts, Published Versions, and other Resources connected through References and Relationships.

The Document Graph may cross Folder and Library boundaries.

### Document Structure

The logical organization of a Document into elements such as headings, paragraphs, lists, tables, definitions, code blocks, examples, and Content Regions.

### Inclusion Point

An explicit location in a Source Document where another Document, Content Region, Artifact, or generated result is resolved into an Assembled Document.

### Content Region

A stably identified and explicitly bounded portion of a Source Document that may be referenced, reused, discussed, versioned, owned, or validated independently of the entire Document.

A Content Region may correspond to a definition, paragraph, table, list, named section, example, or other supported content block.

### Region Identity

The stable identifier of a Content Region. Region Identity must survive ordinary insertion, deletion, formatting changes, renaming, and movement that do not replace the Region itself.

### Region Boundary

The explicit start and end of a Content Region. Changes outside the Region Boundary do not constitute changes to the Content Region.

### Region Version

An identifiable historical state of a Content Region. A new Region Version is created when content within the Region Boundary changes according to the system's versioning policy.

## 5. Artifact Concepts

### Artifact

A Managed Resource that may be referenced by a Document or another Managed Resource, including images, diagrams, datasets, spreadsheets, generated charts, PDFs, recordings, and supporting files.

An Artifact has a stable identity, Metadata, Version History, Ownership, Provenance, and an Accessible Textual Description where applicable.

### Artifact Version

An identifiable historical state of an Artifact. An Artifact Version may include the binary content, source form, Metadata, checksum, and Provenance needed to distinguish and reproduce that state.

### Artifact Source

The editable or generative source used to create an Artifact when such a source exists. Examples include diagram source, chart data, spreadsheet formulas, or image-editing source files.

### Accessible Textual Description

A text-based explanation of an Artifact sufficient to help people and automated tools understand its purpose, content, and relevant meaning when the Artifact itself is not fully text-readable.

### Diagram

An Artifact that communicates structure, relationships, behavior, flow, or another model visually. A Diagram should preserve its editable source where available and include an Accessible Textual Description.

### Dataset

An Artifact containing structured or semi-structured data used as evidence, reference material, analysis input, or a source for generated outputs.

## 6. Reference and Dependency Concepts

### Reference

A managed relationship from one Resource to another Resource. A Reference records the identities, relationship type, version or Resolution Policy, and other information required to resolve and audit the relationship.

### Content Reference

A Reference from a Destination Location to a Source Document, Content Region, Artifact, or Published Version for reuse or inclusion.

A Content Reference records its target identity, Reference Mode, relevant Version information, and Synchronization State.

### Source Resource

The Resource targeted by a Reference.

### Destination Resource

The Resource containing or owning a Reference.

### Destination Location

The explicit location within a Destination Resource where referenced content or an Artifact is used, displayed, or included.

### Reference Mode

The policy controlling how a Content Reference selects and adopts a Source Resource Version.

The supported canonical modes are Live Reference, Approval-Controlled Reference, and Pinned Reference.

### Live Reference

A Content Reference that resolves to the currently selected Source Resource Version according to an explicit Resolution Policy.

A Live Reference reflects qualifying source changes without a separate approval action by the destination owner.

### Approval-Controlled Reference

A Content Reference that detects qualifying source changes but requires explicit approval before the Destination Resource adopts them.

Pending changes remain visible until approved, rejected, superseded, or otherwise resolved.

### Pinned Reference

A Content Reference that resolves to a specific immutable Source Resource Version until deliberately changed.

### Resolution Policy

The explicit rule used to select a Source Resource Version when resolving a Reference. A Resolution Policy may identify a specific Version, an approved branch or state, the latest qualifying Version, or another deterministic selection rule.

### Reference Resolution

The process of selecting and retrieving the Source Resource Version required by a Reference according to its Resolution Policy.

### Synchronization State

The current relationship between the Version adopted by a Destination Resource and the qualifying Version available from its Source Resource.

Examples may include current, update available, approval pending, pinned, unresolved, conflicted, and unavailable. These values require formal definition in the domain model.

### Dependency

A relationship in which one Resource relies on another Resource for assembly, rendering, generation, validation, interpretation, or publication.

### Direct Dependency

A Resource referenced immediately by another Resource.

### Transitive Dependency

A Dependency reached through one or more intermediate Dependencies.

### Inbound Reference

A Reference that targets the Resource being examined.

### Outbound Reference

A Reference owned by the Resource being examined and directed toward another Resource.

### Dependency Manifest

An immutable record of the resolved direct and transitive Dependencies used to produce an Assembled Document, Published Version, Generated Output, or Verification Result.

### Impact Analysis

The process and result of identifying Resources, assemblies, publications, generated outputs, tests, or decisions that may be affected by a proposed or completed change.

### Propagation

The controlled process of making a qualifying Source Resource change available to, or adopted by, one or more Destination Resources.

### Inclusion Cycle

A circular chain of Inclusion References in which a Resource depends, directly or transitively, on itself.

An unresolved Inclusion Cycle prevents deterministic Assembly and is invalid.

### Unresolved Reference

A Reference whose Source Resource, Source Version, Destination Location, or Resolution Policy cannot be resolved deterministically.

## 7. Assembly and Publication Concepts

### Assembly

The deterministic process of resolving a Main Document and its selected Dependencies to produce an Assembled Document.

### Assembly Configuration

The explicit settings, policies, templates, variables, and tool versions used during Assembly.

### Assembly Result

The result of an Assembly attempt, including the Assembled Document when successful and any warnings, errors, unresolved References, cycles, or validation findings.

### Publication Process

The controlled process of resolving, validating, versioning, and releasing an Assembled Document.

### Published Version

An immutable, numbered release record created by the Publication Process. A Published Version contains or identifies the Assembled Document, Source Manifest, Dependency Manifest, Publication Metadata, and one or more Rendered Outputs.

### Publication Number

The human-recognizable version designation assigned to a Published Version according to an explicit numbering policy.

A Publication Number is not necessarily the same as a Resource Version identifier or source-control commit identifier.

### Source Manifest

An immutable record identifying the authoritative source revisions, configuration, templates, and other inputs used by an Assembly, Publication Process, generation operation, or verification operation.

### Publication Metadata

Metadata describing a Published Version, including its Publication Number, title, release date, publisher, status, source revision, applicable approvals, and other release information.

### Rendered Output

A derived representation of a Published Version, such as PDF, DOCX, HTML, assembled Markdown, or another finalized format.

### Reproducibility

The ability to produce the same Assembled Document and equivalent Rendered Outputs from the recorded source revisions, Dependencies, Artifact Versions, configuration, templates, and tool versions.

### Deterministic Assembly

The property that the same recorded inputs and Assembly Configuration produce the same Assembled Document.

### Publication Readiness

The state in which a candidate Assembled Document satisfies all required validation, Metadata, authorization, approval, and Dependency conditions for Publication.

## 8. Knowledge and Provenance Concepts

### Provenance

The recorded lineage connecting Managed Information to its sources, contributors, transformations, Versions, evidence, and resulting outputs.

### Provenance Relationship

A typed Relationship recording how one Resource or knowledge entity was derived from, supported by, generated from, summarized from, or otherwise connected to another.

### Evidence

Source material used to support an Observation, Finding, Insight, Recommendation, Decision, or Action.

Evidence may be a Document, Content Region, Artifact, recording, Dataset, Verification Result, or another Managed Resource.

### Observation

A recorded account of something directly noticed, measured, stated, or captured without requiring a broader conclusion.

### Finding

A supported conclusion derived from one or more Observations or pieces of Evidence within a defined analytical context.

### Insight

An interpretation that explains the significance, pattern, implication, or opportunity revealed by one or more Findings or pieces of Evidence.

### Recommendation

A proposed course of action supported by Findings, Insights, objectives, policies, or other evidence.

### Decision

A recorded choice among alternatives, including its rationale, decision makers, date, status, and supporting Evidence where applicable.

### Action

A recorded activity undertaken or planned as a consequence of a Decision, Recommendation, obligation, or identified need.

### Context

The information necessary to understand why Managed Information was created, by whom, under what conditions, for what purpose, and in relation to which objectives, people, organizations, initiatives, or events.

### Synthesis

The process of combining multiple source materials, perspectives, Observations, or Findings into a consolidated representation while preserving Provenance.

### Synthesized Content

Content produced through Synthesis. Synthesized Content must remain traceable to its contributing sources and transformations.

### Contributor Perspective

A distinct interpretation, note set, opinion, or account associated with a specific Contributor or role. Contributor Perspectives may coexist with Synthesized Content.

### Institutional Knowledge

Managed Information preserved so that it remains discoverable, understandable, and useful beyond the tenure or direct memory of its original Contributors.

### Knowledge Relationship

A typed semantic connection among Resources or knowledge entities, such as supports, contradicts, refines, derives from, addresses, implements, verifies, influences, or results in.

## 9. Collaboration and Governance Concepts

### Contributor

A person or Automated Agent that creates, modifies, reviews, comments on, approves, generates, validates, or publishes Managed Information.

### Human Contributor

A person acting as a Contributor.

### Automated Agent

A software-based Contributor, including an AI-assisted tool, workflow, integration, generator, validator, or other automated process.

### Owner

A Contributor or organizational role accountable for maintaining a Managed Resource and governing applicable changes, References, approvals, and policies.

Ownership does not necessarily imply exclusive authorship or unrestricted authorization.

### Author

A Contributor who creates or modifies Source Documents, Content Regions, Metadata, or other authored content.

### Reviewer

A Contributor responsible for evaluating a proposed change, publication candidate, Reference update, or other reviewable item.

### Approver

A Contributor authorized to accept or reject a controlled change, Reference update, Publication Process, execution, or other governed action.

### Comment

A Managed Resource containing discussion or feedback associated with another Resource, Content Region, Version, or review activity.

### Annotation

A Managed Resource attached to a specific location or object for explanation, classification, review, or commentary without becoming part of the authoritative content by default.

### Review Decision

A recorded outcome of a review, such as approved, rejected, changes requested, or superseded, together with its Contributor, date, rationale, and target Version.

### Acknowledged Work

A change or contribution that has been durably saved, committed, approved, or otherwise explicitly recognized by the system as retained work.

### Conflict

A condition in which two or more changes, policies, Versions, or interpretations cannot be applied together without explicit reconciliation.

### Reconciliation

The explicit process of resolving a Conflict while preserving relevant changes, Provenance, decisions, and audit information.

### Audit Trail

A durable, ordered record of significant actions, state changes, approvals, executions, publications, access-sensitive operations, and other governed events.

### Governance Policy

An explicit rule controlling ownership, authorization, approval, propagation, retention, publication, execution, or another governed behavior.

## 10. Metadata, Classification, and Discovery Concepts

### Metadata

Structured information describing a Managed Resource, its identity, purpose, ownership, classification, lifecycle, context, Provenance, or Relationships.

### Required Metadata

Metadata that must be present and valid before a defined operation, such as Publication, execution, or approval, may proceed.

### Tag

A user- or system-assigned label used for flexible classification, grouping, filtering, and discovery.

A Tag does not replace formally modeled Metadata or Relationships when those distinctions are important.

### Classification

The assignment of a Managed Resource to one or more governed categories according to an explicit classification scheme.

### Search

The process of locating Managed Resources or knowledge entities by content, Metadata, Tag, identity, status, or another indexed property.

### Relationship-Based Discovery

The process of finding Managed Resources by traversing typed Relationships rather than relying only on names, paths, Tags, or text matches.

### Full-Text Search

Search based on the indexed textual content of Documents, descriptions, comments, Metadata, or other text-readable Resources.

### Index

A derived structure used to support efficient Search, classification, relationship traversal, or automated retrieval. An Index is not the authoritative source of the indexed information.

### Search Result

A Resource or knowledge entity returned by Search together with sufficient identity, authorization, context, and relevance information for the user or Automated Agent to evaluate it.

## 11. Execution and Verification Concepts

### Executable Declaration

An explicitly identified, structured statement within a Source Document that may be interpreted by an authorized execution mechanism.

Ordinary prose is not an Executable Declaration.

### Executable Specification

A human-readable specification containing Executable Declarations or structured examples that can be evaluated against a System Under Test or another defined target.

### Behavioral Requirement

A Requirement describing observable system behavior under defined conditions, events, states, or inputs.

### Acceptance Example

A concrete example of expected behavior used to clarify, verify, or execute a Behavioral Requirement.

### Adapter

A controlled integration that translates an Executable Specification into operations against a System Under Test, generator, validator, workflow, or external tool.

### Execution

The controlled act of interpreting and running explicitly designated executable content through an authorized Adapter or integration.

### Execution Environment

The identified technical and policy context in which an Execution occurs, including relevant runtime, configuration, dependencies, permissions, and isolation controls.

### System Under Test

The identified software system, service, workflow, dataset, configuration, or generated result evaluated by an Executable Specification or validation process.

### Validation

The process of determining whether a Resource, Assembly, Publication candidate, generated result, or System Under Test satisfies defined rules, constraints, or expectations.

### Verification

The process of obtaining objective evidence that specified requirements or expected behaviors have been satisfied.

### Verification Result

A durable record of an Execution or other validation activity, including the specification Version, Dependency Versions, System Under Test Version, Execution Environment, execution time, outcome, and supporting evidence.

### Verification Evidence

Artifacts, logs, measurements, reports, screenshots, traces, or other Managed Resources supporting a Verification Result.

### Outcome

The recorded result of an Execution or Validation, such as passed, failed, errored, skipped, inconclusive, or not executed. The final set of allowed outcomes requires formal definition in the domain model.

### Documentation Drift

A condition in which managed specifications, explanations, examples, or diagrams no longer accurately represent the system, policy, process, evidence, or behavior they describe.

### Generated Output

A derived Resource produced from structured source, such as code, tests, configuration, diagrams, reports, or operational instructions.

### Generation Rule

An explicit, versioned rule or transformation used to create a Generated Output from Source Documents, Content Regions, Metadata, or other inputs.

## 12. Security and Sensitive Information Concepts

### Authorization Policy

An explicit rule determining which Contributors or Automated Agents may perform an operation on a Managed Resource.

### Permission

A granted capability to perform a defined operation, such as view, create, modify, comment, approve, execute, publish, administer, or disclose.

### Access Control

The enforcement of Authorization Policies and Permissions when a Contributor or Automated Agent attempts an operation.

### Privileged Action

An operation requiring elevated authorization because it can materially affect security, execution, publication, governance, sensitive information, or other Contributors' work.

### Sensitive Information

Personal, confidential, restricted, or otherwise protected information subject to explicit policies governing access, consent, redaction, disclosure, retention, and lifecycle handling.

### Sensitivity Classification

A governed category assigned to a Resource or portion of a Resource to indicate required handling and access restrictions.

### Consent

A recorded authorization from an appropriate person or authority permitting a specified collection, use, retention, publication, or disclosure of Sensitive Information.

### Redaction

The controlled removal or concealment of Sensitive Information from a view, Rendered Output, Published Version, or disclosure while preserving appropriate audit information.

### Disclosure

The act of making Managed Information available to a person, group, system, or external party.

### Retention Policy

An explicit rule governing how long a Managed Resource or category of information is retained and what occurs at the end of that period.

The initial product may support basic lifecycle policies without providing full enterprise records-management capabilities.

### Lifecycle Policy

An explicit rule governing the allowed states, transitions, retention, archival, redaction, publication, or disposition of a Managed Resource.

## 13. Change and Lifecycle Concepts

### Draft

A mutable Resource Version that has not yet reached an approved or published state.

### Change

A proposed or completed modification to a Managed Resource, Relationship, Metadata value, policy, or configuration.

### Change Set

A related collection of Changes intended to be reviewed, committed, approved, merged, or published together.

### Commit

A durable repository record of a Change Set and its parent state or states. A Commit is a repository concept and is not equivalent to a Published Version.

### Merge

The process of combining Changes from distinct histories into a unified state.

### Approval

An explicit decision by an authorized Approver permitting a controlled change or operation to proceed.

### State

A named lifecycle condition of a Managed Resource or process. Examples may include draft, under review, approved, published, superseded, archived, or withdrawn. The formal state models belong in the domain model.

### Transition

A governed change from one State to another.

### Superseded

A State indicating that a newer Resource Version or Published Version has replaced an earlier one for current use without modifying the earlier immutable record.

### Archived

A State indicating that a Managed Resource is retained but is no longer active for ordinary use.

### Withdrawn

A State indicating that a Resource or Published Version should no longer be relied upon for current use while its history and audit record remain preserved.

## 14. Requirement and Decision Concepts

### Need

A problem, opportunity, constraint, or desired outcome that motivates product behavior or capability.

### Product Driver

A significant Need, scenario, principle, or external concern that influences the product's scope and priorities.

### User Story

A concise statement of a user's goal and the value expected from achieving it. User Stories express intent and context rather than complete system behavior.

### Requirement

A testable statement describing a capability, behavior, constraint, quality, or condition the system must satisfy.

### EARS Requirement

A Requirement expressed using an Easy Approach to Requirements Syntax pattern that makes its trigger, condition, state, and required system response explicit.

### Acceptance Criterion

A verifiable condition that must be satisfied for a User Story, Requirement, capability, or change to be accepted.

### Invariant

A condition that must remain true throughout all valid states or transitions of the modeled concept.

### Business Rule

A rule defining or constraining behavior, decisions, calculations, Relationships, or lifecycle transitions within the domain.

### Architecture Decision Record

A durable record of a significant architecture decision, including its context, considered options, decision, status, and consequences.

### Constitutional Amendment

An approved change to the Project Constitution that alters a governing principle, scope boundary, or canonical definition.

## 15. Terms Requiring Further Domain Modeling

The following concepts are intentionally identified but not yet fully defined. Their identities, attributes, Relationships, invariants, and lifecycle states should be resolved in `02-domain-model.md`:

- exact Resource and Region versioning semantics;
- Reference synchronization states and transitions;
- reference propagation and approval workflow;
- ownership inheritance and delegation;
- Publication Number policies;
- draft, review, approval, publication, supersession, archival, and withdrawal states;
- semantic identities for Observation, Finding, Insight, Recommendation, Decision, and Action;
- authorization inheritance and policy precedence;
- Metadata schemas and classification schemes;
- Verification Result outcomes and evidence requirements;
- Generated Output ownership and regeneration policies;
- Repository synchronization, conflict detection, and reconciliation behavior;
- consent, redaction, disclosure, retention, and lifecycle-policy enforcement.

## 16. Source Documents

This glossary is governed by and derived from:

- [Project Constitution](./00-project-constitution.md)
- [Customer Insight Documentation System Vision](./00.01-Interview-Constitution.md)
- [Referenced Content Management Vision](./00.02-Partial-References.md)
- [Documentation as Executable Code](./00.03.Literate-Programming.md)
- [Documentation as Test](./00.04.FitNesse.md)

When a supporting need uses terminology that conflicts with this glossary, the canonical terms in this glossary and the Project Constitution take precedence unless amended through the constitutional amendment process.
