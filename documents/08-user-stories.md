# User Stories

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines the initial user-story catalog derived from the context map, vertical slices, user roles, and cross-context invariants.

Each story identifies:

- a primary role;
- a user-visible goal;
- the expected benefit;
- the owning bounded context;
- key acceptance examples;
- related invariants.

These stories describe behavior and outcomes. They do not prescribe user-interface structure, storage technology, or service boundaries.

# Release 1A — Evidence to Decision

## US-001 — Capture Source Evidence

**As a Researcher,**
I want to create or import a source Document or Artifact,
so that organizational knowledge can be traced to durable source material.

**Owning context:** Document Authoring and Composition

### Acceptance examples

- Given a new interview transcript, when the Researcher records it, then the system assigns stable Resource and Revision identities.
- Given imported source material, when it is recorded, then the external source and authority designation are preserved.
- Given sensitive material, when it is recorded, then applicable sensitivity information is retained and enforced.

**Related invariants:** stable identity, explicit authority, immutable Revisions, sensitive information governance.

## US-002 — Identify an Evidence Region

**As a Researcher,**
I want to identify an exact region within a source Revision,
so that later interpretations can cite the precise material on which they rely.

**Owning context:** Document Authoring and Composition

### Acceptance examples

- Given a source Document Revision, when the Researcher marks an Evidence region, then the region receives a stable identity within the Document.
- Given a later Document Revision, when the original region changes or disappears, then prior references still resolve to the historical occurrence.
- Given a citation, when a user follows it, then the exact source Revision and region are displayed.

**Related invariants:** historical reconstruction, exact provenance, identity survives revision.

## US-003 — Create a Finding from Evidence

**As an Analyst,**
I want to create a Finding linked to supporting Evidence,
so that interpretations remain traceable to their sources.

**Owning context:** Knowledge and Provenance

### Acceptance examples

- Given one or more Evidence references, when the Analyst creates a Finding, then the Finding records those references.
- Given no supporting Evidence, when the Analyst finalizes the Finding, then the system requires it to be marked as a hypothesis or blocks finalization.
- Given a later change in interpretation, when a new Finding supersedes the earlier one, then both remain visible.

**Related invariants:** support does not establish truth, competing interpretations may coexist, history is not rewritten.

## US-004 — Record Contrary Evidence

**As an Analyst,**
I want to link contrary Evidence to a Finding,
so that conflicting perspectives remain visible.

**Owning context:** Knowledge and Provenance

### Acceptance examples

- Given an existing Finding, when contrary Evidence is linked, then the original Finding remains unchanged.
- Given both supporting and contrary Evidence, when a user views the Finding, then both evidence paths are visible.
- Given a later resolution, when a new interpretation is created, then it may supersede but does not erase earlier interpretations.

**Related invariants:** contradiction does not delete either side, competing interpretations remain visible.

## US-005 — Create a Recommendation

**As an Analyst,**
I want to create a Recommendation derived from one or more Findings,
so that proposed action is separated from interpretation.

**Owning context:** Knowledge and Provenance

### Acceptance examples

- Given a Finding, when a Recommendation is created, then the derivation is recorded.
- Given multiple Findings, when a Recommendation relies on them, then each material source is traceable.
- Given uncertainty, when the Recommendation is recorded, then assumptions and confidence may be included.

**Related invariants:** Recommendations remain distinct from Decisions, provenance does not imply endorsement.

## US-006 — Record an Authorized Decision

**As a Decision Maker,**
I want to accept, reject, defer, or supersede a Recommendation,
so that organizational direction is explicit and accountable.

**Owning context:** Knowledge and Provenance

### Acceptance examples

- Given a Recommendation, when an authorized Decision Maker records a Decision, then the outcome, rationale, authority, scope, and time are preserved.
- Given an unauthorized Party, when they attempt to finalize the Decision, then the action is rejected and audited.
- Given a later Decision, when it supersedes an earlier Decision, then the earlier Decision remains historically visible.

**Related invariants:** authority is explicit, privileged actions are auditable, supersession preserves history.

## US-007 — Navigate Decision Provenance

**As a Decision Reviewer,**
I want to navigate from a Decision to its Recommendation, Findings, and exact source Evidence,
so that I can understand why the Decision was made.

**Owning context:** Knowledge and Provenance

### Acceptance examples

- Given a Decision, when the reviewer opens its provenance, then all material upstream records are visible subject to authorization.
- Given restricted source Evidence, when the reviewer lacks access, then the system does not leak sensitive content or metadata.
- Given imported Evidence, when provenance is displayed, then the external source and authority are shown.

**Related invariants:** provenance is navigable, sensitive information does not leak, external authority remains explicit.

# Release 1B — Decision to Planned Work

## US-008 — Create an Objective from a Decision

**As an Objective Owner,**
I want to create an Objective linked to an authorized Decision,
so that the desired future condition remains connected to its rationale.

**Owning context:** Work Planning

### Acceptance examples

- Given an authorized Decision, when an Objective is created, then the Objective links back to that Decision.
- Given an Objective statement that merely describes work, when the Objective Owner attempts to save it, then the system prompts for a desired condition rather than an activity.
- Given a material Objective change, when it is saved, then a new Revision is created.

**Related invariants:** Objective is distinct from work, plan history remains reconstructable.

## US-009 — Define an Intended Outcome

**As an Outcome Owner,**
I want to define an Intended Outcome with Success Conditions,
so that later review can determine whether the desired result occurred.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given an Objective, when the Outcome Owner defines an Intended Outcome, then scope, observation window, and Success Conditions are recorded.
- Given a statement that only names a Deliverable, when it is entered as an Outcome, then the system requires a distinct desired result.
- Given changed Success Conditions, when they are adopted, then a new Revision is created and the prior criteria remain visible.

**Related invariants:** output is distinct from outcome, historical criteria remain visible.

## US-010 — Create a Project

**As a Project Owner,**
I want to create a Project linked to a Decision, Objective, and Intended Outcome,
so that planned work retains its strategic and evidential context.

**Owning context:** Work Planning

### Acceptance examples

- Given an authorized Decision, when a Project is created, then the Project records the Decision or governing Objective it Addresses.
- Given a Project, when expected Deliverables and Intended Outcomes are entered, then they remain separate fields and concepts.
- Given a Project with no accountable owner, when approval is requested, then the request is rejected.

**Related invariants:** plan, output, and outcome remain distinct; accountability is explicit.

## US-011 — Record Planning Assumptions

**As a Project Owner,**
I want to record assumptions that influence the Project,
so that later evidence can challenge them explicitly.

**Owning context:** Work Planning

### Acceptance examples

- Given a Project, when an assumption is recorded, then its rationale, confidence, scope, and review date may be captured.
- Given contrary Evidence, when it challenges the assumption, then the assumption remains visible and may be superseded.
- Given a Plan Revision, when it depends on an assumption, then that relationship is navigable.

**Related invariants:** assumptions are explicit and reviewable, challenged information is not erased.

## US-012 — Define a Milestone and Activity Plan

**As a Project Owner,**
I want to define a Milestone and Activity Plan,
so that planned execution and acceptance criteria are explicit.

**Owning context:** Work Planning

### Acceptance examples

- Given a Project, when a Milestone is created, then it records a planned condition and acceptance criteria.
- Given an Activity Plan, when it is created, then expected Deliverables, dependencies, and completion conditions are explicit.
- Given actual execution, when it occurs, then the Activity Plan is not mutated to represent what happened.

**Related invariants:** plans do not prove work, actual execution does not rewrite the plan.

## US-013 — Approve an Exact Plan Revision

**As a Plan Approver,**
I want to approve or reject an exact Plan Revision,
so that execution is governed by a known historical plan.

**Owning context:** Work Planning

### Acceptance examples

- Given a Project Revision, when approval is recorded, then the exact Revision, authority, outcome, rationale, and time are preserved.
- Given a later Project Revision, when it is created, then prior approval does not automatically apply.
- Given an unauthorized reviewer, when approval is attempted, then the action is rejected and audited.

**Related invariants:** approval is Revision-specific, authority is explicit, history remains reconstructable.

# Release 1C — Planned Work to Deliverable

## US-014 — Assign Planned Work

**As an Assignment Authority,**
I want to assign an approved Activity Plan to a Contributor,
so that responsibility and effective scope are explicit.

**Owning context:** Work Execution

### Acceptance examples

- Given an approved Activity Plan Revision, when an Assignment is created, then the exact Revision is referenced.
- Given an Assignment, when its effective period or responsibility changes, then a new Revision or replacement is recorded.
- Given an Assignment, then the system does not infer that work has begun.

**Related invariants:** assignment does not prove participation, exact plan reference is preserved.

## US-015 — Accept or Reject an Assignment

**As a Contributor,**
I want to accept or reject an Assignment,
so that my participation is explicit.

**Owning context:** Work Execution

### Acceptance examples

- Given an Assignment, when the Contributor accepts it, then the acceptance is recorded with time and actor.
- Given a rejection, when it is recorded, then rationale may be included and the Assignment remains historically visible.
- Given an unauthorized Party, when they respond to another Party's Assignment, then the action is rejected.

**Related invariants:** participation is explicit, authority and identity are preserved.

## US-016 — Record Work Started

**As a Contributor,**
I want to record that work started,
so that execution history can be distinguished from the plan.

**Owning context:** Work Execution

### Acceptance examples

- Given an accepted Assignment, when Work Started is recorded, then an immutable Work Event is created.
- Given a later correction, when the recorded time was wrong, then a correcting record is created rather than rewriting the event.
- Given current status, when it is displayed, then it is derived from source events.

**Related invariants:** immutable records are append-only, projections remain explainable.

## US-017 — Record a Blockage

**As a Contributor,**
I want to record a blockage and its dependency,
so that current risk and delay are visible while action remains possible.

**Owning context:** Work Execution

### Acceptance examples

- Given work cannot continue, when a Blockage Observation is recorded, then the affected work, condition, severity, and dependency are preserved.
- Given the blockage is resolved, when a resolution record is created, then the original blockage remains visible.
- Given unresolved blockage records, when current status is calculated, then it may be projected as Blocked.

**Related invariants:** blockage is an observation, current status is a Projection, history is preserved.

## US-018 — Record a Produced Deliverable

**As a Contributor,**
I want to link an exact Document or Artifact Revision as the produced Deliverable,
so that output provenance is complete.

**Owning context:** Work Execution

### Acceptance examples

- Given completed work, when a Deliverable is produced, then the exact Resource Revision is linked through a Deliverable Production record.
- Given a generated Deliverable, when it is recorded, then source Revisions and execution context are traceable.
- Given a produced Deliverable, then the system does not infer acceptance or Outcome achievement.

**Related invariants:** generated output is a role, production does not prove acceptance or outcome.

## US-019 — Accept a Milestone

**As a Milestone Acceptance Authority,**
I want to accept, reject, or conditionally accept a Milestone,
so that completion is evaluated against the approved criteria.

**Owning context:** Work Execution

### Acceptance examples

- Given a Milestone, when it is reviewed, then the exact Plan Revision and acceptance criteria are identified.
- Given insufficient Evidence, when acceptance is attempted, then the reviewer may reject or conditionally accept with explicit exceptions.
- Given a later changed decision, when it is recorded, then a superseding acceptance record is created.

**Related invariants:** acceptance is immutable, acceptance does not prove outcome achievement.

## US-020 — Record Completion

**As a Completion Authority,**
I want to record that a defined work scope is completed, cancelled, or terminated,
so that the execution history is explicit without rewriting the plan.

**Owning context:** Work Execution

### Acceptance examples

- Given an execution scope, when completion is recorded, then the applicable plan, authority, Deliverables, unresolved items, and outcome are preserved.
- Given reopened work, when additional execution begins, then new Work Events are created and the prior Completion Record remains visible.
- Given completion, then the system does not infer that an Intended Outcome occurred.

**Related invariants:** completion is distinct from outcome, historical execution remains reconstructable.

# Release 1D — Deliverable to Outcome Review

## US-021 — Define a Measurement

**As a Measurement Designer,**
I want to define how a measure is collected or calculated,
so that later results are interpretable and reproducible.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given an Intended Outcome, when a Measurement Definition is created, then subject, unit, source, rule, temporal window, and quality constraints are recorded.
- Given a material rule change, when it is adopted, then a new Revision is created.
- Given a proxy measure, when it is linked to an Outcome, then the relationship is explicit and does not redefine the proxy as the Outcome itself.

**Related invariants:** measures are distinct from outcomes, definitions are versioned, derivation is reproducible.

## US-022 — Record or Import a Measurement

**As a Measurement Recorder,**
I want to record or import a Measurement Record,
so that observed results retain source, time, authority, and quality context.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given a Measurement Definition Revision, when a value is recorded, then the exact definition, observation time, source, unit, and authority are preserved.
- Given imported data, when it is recorded, then its external identifier and authority designation are included.
- Given a correction, when the original value was wrong, then a correcting record is created.

**Related invariants:** immutable measurement records, external authority explicit, recorded and observation time distinct.

## US-023 — Create an Observed Outcome

**As an Outcome Analyst,**
I want to interpret Measurement Records into an Observed Outcome,
so that evidence and interpretation remain connected but distinct.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given one or more Measurement Records, when an Observed Outcome is created, then the supporting records and interpretation are preserved.
- Given uncertainty, when the Outcome is recorded, then confidence and competing explanations may be included.
- Given conflicting interpretations, when another Observed Outcome is created, then both may coexist.

**Related invariants:** measurement is distinct from interpretation, competing interpretations remain visible.

## US-024 — Record Attribution Assumptions

**As an Outcome Analyst,**
I want to record assumptions about what contributed to an Outcome,
so that correlation is not misrepresented as causation.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given an observed improvement, when the analyst links a Project or Deliverable, then the contribution claim and confidence are explicit.
- Given external factors, when they may have influenced the result, then they can be recorded as competing explanations.
- Given insufficient evidence, then the system does not present the relationship as proven causation.

**Related invariants:** correlation is not causation, contribution claims are distinct from causal claims.

## US-025 — Issue an Outcome Review

**As an Outcome Reviewer,**
I want to evaluate an Intended Outcome against exact Success Conditions and Evidence,
so that achievement is assessed explicitly.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given an Intended Outcome Revision and Measurement Records, when the reviewer issues an Outcome Review, then the exact criteria and evidence are recorded.
- Given insufficient data, when the review is issued, then the outcome may be Inconclusive or Not Yet Observable.
- Given a Deliverable was completed but no valid measurement exists, then the reviewer cannot mark the Outcome Achieved solely from completion.
- Given a later review, when it supersedes an earlier one, then both remain visible.

**Related invariants:** output does not prove outcome, reviews are immutable, projections do not replace reviews.

## US-026 — Trace Outcome Back to Evidence

**As an Outcome Owner,**
I want to navigate from an Outcome Review back through measurements, execution, plans, Decisions, and source Evidence,
so that I can understand the full chain of reasoning and action.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given an Outcome Review, when provenance is opened, then the chain to exact source identities and Revisions is available subject to authorization.
- Given a missing cross-context reference, when the chain cannot be completed, then the gap is visible rather than silently ignored.
- Given restricted information, then unauthorized content is not leaked through the graph.

**Related invariants:** provenance is end to end, cross-context references use stable identities, sensitive information does not leak.

# Release 1E — Learning to Adaptation

## US-027 — Create a Learning Record

**As a Learning Author,**
I want to record a lesson from an Outcome Review,
so that changed understanding is preserved independently of later action.

**Owning context:** Outcome Measurement and Learning

### Acceptance examples

- Given an Outcome Review, when a Learning Record is created, then supporting evidence and affected assumptions or Decisions are linked.
- Given uncertain learning, when it is recorded, then confidence and unresolved questions may be included.
- Given a Learning Record, then no authoritative Plan or Decision changes automatically.

**Related invariants:** learning is distinct from adoption, provenance is preserved.

## US-028 — Challenge an Assumption or Decision

**As a Learning Author,**
I want to link learning that challenges an assumption, Finding, Decision, or Plan,
so that unresolved contradictions remain visible.

**Owning context:** Knowledge and Provenance / Outcome Measurement and Learning

### Acceptance examples

- Given a Learning Record, when it challenges a Planning Assumption, then both records remain visible.
- Given a challenged Decision, when no new Decision is made, then the original Decision remains authoritative within its scope.
- Given competing lessons, then neither is silently discarded.

**Related invariants:** contradiction does not delete either side, learning does not automatically revise authority.

## US-029 — Record an Adaptation Decision

**As an Adaptation Decision Maker,**
I want to accept, reject, or defer a proposed adaptation,
so that learning changes direction only through explicit authority.

**Owning context:** Knowledge and Provenance

### Acceptance examples

- Given a Learning Record, when the Decision Maker accepts an adaptation, then rationale, authority, and scope are recorded.
- Given rejection or deferral, when it is recorded, then the Learning Record remains visible.
- Given an unauthorized Party, when they attempt to adopt the learning, then the action is rejected and audited.

**Related invariants:** adaptation is explicit, authority is auditable, history is preserved.

## US-030 — Revise a Plan from Learning

**As a Project Owner,**
I want to create a new Plan Revision linked to an Adaptation Decision,
so that the organization can change direction without rewriting history.

**Owning context:** Work Planning

### Acceptance examples

- Given an accepted Adaptation Decision, when the Project Owner revises the Project, then the new Revision links to the Decision and Learning Record.
- Given the new Revision, then prior Plan Revisions and approvals remain visible.
- Given approval is required, then the revised Plan does not become approved automatically.

**Related invariants:** replanning creates a new Revision, learning is adopted through explicit Decision and approval.

# Cross-Cutting Stories

## US-031 — Assign Contextual Roles

**As a Resource Steward,**
I want to assign contextual roles to Parties for a defined scope and period,
so that responsibility and authority are explicit.

**Owning context:** Identity, Access, and Governance

### Acceptance examples

- Given a Party, when a role is assigned, then scope, authority, effective period, and assigning authority are recorded.
- Given an expired assignment, when an action is attempted, then the action is rejected.
- Given one Party with multiple roles, then each role remains independently auditable.

## US-032 — Enforce Authorization Without Data Leakage

**As a Sensitivity Authority,**
I want authorization enforced across search, navigation, and projections,
so that restricted information is not disclosed indirectly.

**Owning context:** Identity, Access, and Governance

### Acceptance examples

- Given an unauthorized user, when they search for restricted content, then neither content nor sensitive counts or metadata are leaked.
- Given a provenance path containing restricted nodes, then the visible graph omits or safely redacts them according to policy.
- Given a denied consequential action, then the denial is auditable.

## US-033 — Correct an Immutable Record

**As an authorized record owner,**
I want to correct a factual error without deleting the original record,
so that history remains trustworthy.

**Owning context:** Shared Graph Foundation and owning domain context

### Acceptance examples

- Given an immutable record with an error, when a correction is submitted, then a correcting record references the original.
- Given consumers of current information, then the correction is reflected in appropriate Projections.
- Given historical reconstruction, then both the original and correction are visible.

## US-034 — Explain a Projection

**As a user viewing a status or summary,**
I want to see how it was calculated,
so that I can distinguish derived information from authoritative facts.

**Owning context:** Owning analytical context

### Acceptance examples

- Given a projected status, when explanation is requested, then the definition, inputs, temporal boundary, assumptions, and generated time are shown.
- Given corrected or late-arriving facts, when the current Projection changes, then prior source facts remain unchanged.
- Given multiple valid Projection policies, then the applied policy is explicit.

## US-035 — Preserve External Authority

**As an Integration Steward,**
I want imported information to retain its external identity and authority status,
so that synchronization does not silently make the local system authoritative.

**Owning context:** Integration and External Systems

### Acceptance examples

- Given imported information, when it is stored locally, then external system, identifier, authority designation, and synchronization state are recorded.
- Given a read-only authoritative source, when a local edit is attempted, then the action is blocked or represented as a local proposal according to policy.
- Given synchronization failure, then stale state is visible.

# Story Prioritization

## Must Have for Release 1

- US-001 through US-007
- US-008 through US-013
- US-014, US-016, US-018, US-019, US-020
- US-021 through US-026
- US-031 through US-034

## Should Have for Release 1

- US-004
- US-011
- US-015
- US-017
- US-024
- US-027 through US-030
- US-035

## Later

- advanced role separation policies;
- multi-project Program coordination;
- portfolio and financial stories;
- automated recommendation generation;
- sophisticated causal analysis;
- configurable lifecycle engines;
- enterprise-scale publication workflows.

# Traceability Rules

1. Every requirement derived from this catalog shall reference one or more Story IDs.
2. Every Story shall reference its owning bounded context.
3. Acceptance specifications shall preserve the Story ID.
4. New Stories shall identify applicable cross-context invariants.
5. Stories that introduce a new domain concept shall update the appropriate domain model or document why the concept remains local to the workflow.

# Open Questions

1. Which Stories are required for the first pilot rather than the broader Release 1 boundary?
2. Which acceptance examples should become executable specifications first?
3. Which Stories require a graphical provenance view?
4. Which Stories can initially be supported through import or API rather than a dedicated user interface?
5. Which authorization rules must be demonstrated in the first walking skeleton?
6. Which Should-Have Stories reveal enough risk to be promoted before implementation begins?
