# Requirements Document

## Introduction

This document specifies the requirements for the **second walking slice** of the Organizational Knowledge and Work System: the minimum end-to-end software capability that carries an authorized Decision (produced by the [first walking slice](../first-walking-slice/requirements.md)) through to a planned, reviewed, and approved unit of work without recording any execution or any observed result.

The slice realizes the pipeline:

```text
Authorized Decision → Objective → Intended Outcome
                              ↘
                                Project → Deliverable Expectation
                                       ↘
                                         Activity Plan → Plan Revision
                                                                ↓
                                                          Plan Review
                                                                ↓
                                                         Plan Approval
```

and must demonstrate six named behaviors end-to-end:

1. **Decision-to-Objective traceability** — every Objective is provably anchored to an authorized Slice 1 Decision Immutable Record.
2. **Plan/Execution separation** — no execution facts (work events, time entries, deliverable production, completion records) may be recorded on or attached to any planning Resource in this slice.
3. **Output/Outcome separation** — Intended Outcomes and Deliverable Expectations are declarations of intent; no observed outcome and no actual produced deliverable may be recorded in this slice.
4. **Distinct Plan Reviewer and Plan Approver authorities** — review authority and approval authority are separate authority types; an approval cannot be issued by an actor holding only review authority and vice versa.
5. **Immutability of approved Plan Revisions** — once a Plan Approval Record exists for a Plan Revision, the Plan Revision and its constituent fields and associations are byte-equivalent forever.
6. **Indistinguishable denial for unauthorized planning actions** — every denial path on the new planning endpoints conforms to the Slice 1 `slice-default-2026` disclosure policy (AD-WS-9), with no information leakage about the existence, attributes, or counts of restricted planning Resources.

The slice is **executable system behavior**, distinct from documentation work. It is the software realization of *Release 1B — Decision to Planned Work* defined in [`documents/07-user-story-map.md`](../../../documents/07-user-story-map.md) §4, constrained by the foundational system model in [`documents/00-project-constitution.md`](../../../documents/00-project-constitution.md) §2, §5.21, and §5.23, by the Work Planning context defined in [`documents/03-context-map.md`](../../../documents/03-context-map.md) §2.5, and by the Intent and Specification Record contract in [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §7.4.

These requirements reconcile with, and do not duplicate, the upstream authoritative documents and the first walking slice. Where an upstream constitutional principle, domain-model contract, context-map ownership, or first-walking-slice requirement already governs a behavior, the corresponding requirement here carries an explicit **Traceability** block and refines that behavior to the slice's scope. Where no upstream identifier exists, the requirement is flagged as a **Gap** for resolution before implementation. The slice continues the Slice 1 Gap numbering scheme: new gaps are recorded as **G-6** onward.

### Scope of this slice

In scope:

- Creating an `Objective` Resource whose first material source is an authorized `Decision Immutable Record` produced by the first walking slice.
- Recording one `Intended Outcome` linked to that Objective, distinguished from any observed outcome.
- Creating one `Project` Resource that addresses the Objective.
- Declaring one `Deliverable Expectation` on the Project, distinguished from any actual produced deliverable.
- Creating one `Activity Plan` Resource within the Project.
- Submitting one `Plan Revision` of the Activity Plan for review.
- Recording one `Plan Review` by a Party holding Plan Reviewer authority.
- Recording one `Plan Approval` by a Party holding Plan Approver authority, after which the Plan Revision becomes immutable.
- Authorization-aware backlinks and forward-provenance navigation across the new planning Resources, joining seamlessly with the Slice 1 provenance chain back to exact Evidence.
- Audit records for every consequential planning write and every denied unauthorized planning attempt.
- Additive extension of the Slice 1 `slice-default-2026` disclosure policy to cover the new planning node kinds.
- Additive extension of the Slice 1 authority enumeration with a `review` authority type, recorded as input to a new backlog ADR.

Out of scope for this slice (deferred to later slices — see §"Out-of-Scope Boundaries"):

- Slice 1C — Planned Work to Deliverable (assignment, work events, time entries, deliverable production, milestone acceptance, completion records).
- Slice 1D — Deliverable to Outcome Review (measurement definitions, measurement records, observed outcomes, outcome reviews).
- Slice 1E — Learning to Adaptation (learning records, adaptation decisions, plan supersession beyond approval-immutability).
- Slice 2 — Reproducible Publication of planning artifacts.
- Slice 3 — Investment, cost, capacity, and portfolio reporting against plans.
- Programs, Initiatives, Roadmaps, and Milestones beyond Activity Plans within a single Project.
- Multiple parallel Plan Revisions; concurrent-author reconciliation of draft Plan Revisions.
- Approval workflows requiring two-of-N approvers, conditional approvals, or delegated approvals.
- Withdrawal, redaction, retention expiry, or cryptographic erasure of approved Plan Revisions.
- Portability export of planning Resources.
- Automated Agent contribution provenance on planning Resources beyond recording that an authoring Party is human.
- Any modification of Slice 1 contexts (Identity, Audit, Authorization, Evidence, Knowledge, Trails, Provenance) other than additive extensions defined in this document.

## Glossary

This glossary names the systems, sub-systems, role-bearing Parties, and Resource kinds required by the requirements below. Defined Capitalized Terms not redefined here carry the meaning given in [`documents/01-domain-glossary.md`](../../../documents/01-domain-glossary.md), [`documents/02-domain-model.md`](../../../documents/02-domain-model.md), and the [first-walking-slice requirements](../first-walking-slice/requirements.md). Sub-system names introduced in Slice 1 are referenced here without re-definition.

### Sub-systems

- **Walking_Slice_System**: The cumulative software realization of the first and second walking slices. References to "the system" map to this term throughout this document.
- **Planning_Service**: The new Slice 2 sub-system that records Objectives, Intended Outcomes, Projects, Deliverable Expectations, Activity Plans, Plan Revisions, Plan Reviews, and Plan Approval Records. Owned by the Work Planning bounded context per [`documents/03-context-map.md`](../../../documents/03-context-map.md) §2.5.
- **Identity_Service**, **Authorization_Service**, **Audit_Log**, **Evidence_Repository**, **Knowledge_Service**, **Trail_Service**, **Provenance_Navigator**: As defined in the [first-walking-slice requirements](../first-walking-slice/requirements.md) §"Glossary". This slice reuses these sub-systems and SHALL NOT modify them except through additive extensions described in Requirements 17 and 19.

### Resource kinds and Immutable Records introduced by this slice

- **Objective**: An Intent and Specification Record (Intent kind) per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §7.4 expressing a desired future condition. Within this slice, an Objective Resource is created with at least one `Addresses` Relationship to a Decision Immutable Record produced by Slice 1.
- **Intended Outcome**: An Intent and Specification Record (Result kind) per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §7.4 whose `outcome_kind` attribute is `intended` and that is associated with exactly one Objective. Distinguished from an Observed Outcome (out of scope) per Principle 5.21.
- **Project**: A Resource representing a planned unit of work in the Work Planning context, with at least one `Addresses` Relationship to an Objective.
- **Deliverable Expectation**: An Intent and Specification Record declaring an expected output of a Project. Distinguished from an actual produced Deliverable (out of scope) per Principle 5.21.
- **Activity Plan**: A Resource representing a coordinated set of planned activities within a Project, owned by the Work Planning context per [`documents/03-context-map.md`](../../../documents/03-context-map.md) §2.5.
- **Plan Revision**: An immutable Resource Revision of an Activity Plan that records the planned scope, planned deliverable references, planning assumptions, and ordering rationale at one point in the Activity Plan's history.
- **Plan Review**: A Collaboration Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §7.6 (Review Decision kind) that records the outcome of a review against an exact Plan Revision by a Party holding effective Plan Reviewer authority.
- **Plan Approval Record**: A Governance Decision Immutable Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.5 that records the approval of an exact Plan Revision by a Party holding effective Plan Approver authority. Creation of the Plan Approval Record finalizes the targeted Plan Revision as immutable for all purposes per Principle 5.6.

### Roles introduced by this slice

The four roles below are **contextual roles** per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §9 and extend the contextual role catalog already established in Slice 1. A Party holds a role only by virtue of an effective Role Assignment recorded by the Authorization_Service.

- **Objective Owner**: A Party authorized to create or modify Objectives and Intended Outcomes within an applicable scope. Granted authorities include `modify`.
- **Project Owner**: A Party authorized to create or modify Projects, Deliverable Expectations, Activity Plans, and draft Plan Revisions within an applicable scope. Granted authorities include `modify`.
- **Plan Reviewer**: A Party authorized to record a Plan Review against a Plan Revision within an applicable scope. Granted authorities include the new `review` authority type defined in Requirement 11. Plan Reviewer authority SHALL NOT be substituted for Plan Approver authority and vice versa.
- **Plan Approver**: A Party authorized to record a Plan Approval against a Plan Revision within an applicable scope. Granted authorities include `approve`. Plan Approver authority SHALL NOT be substituted for Plan Reviewer authority and vice versa.

### Slice-specific terms

- **Approved Plan Revision**: A Plan Revision for which a Plan Approval Record has been finalized. An Approved Plan Revision is immutable for all purposes per Requirement 9.
- **Draft Plan Revision**: A Plan Revision for which no Plan Approval Record has been finalized. A Draft Plan Revision may be replaced by a later Draft Plan Revision before approval; replacement is represented by a `Supersedes` Relationship per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.6.
- **Slice 1 Decision**: A Decision Immutable Record produced by the first walking slice and conforming to its Requirement 6 (Record an Authorized Decision).
- **Planning Provenance Chain**: The end-to-end traversal Plan Approval Record → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision → Recommendation Revision → Finding Revision(s) → Content Region Occurrence(s) → Document Revision.


## Requirements

### Requirement 1: Durable Identity Foundation for Planning Resources

**User Story:** As an implementer of the second walking slice, I want every managed identity produced by the Planning_Service to conform to the durable identity strategy already enforced by the Identity_Service, so that Objectives, Intended Outcomes, Projects, Deliverable Expectations, Activity Plans, Plan Revisions, Plan Reviews, and Plan Approval Records remain referenceable across future bounded contexts, exports, and migrations on the same terms as Slice 1 Resources.

**Traceability:**
- Constitution: Principle 5.5 (Identity is independent of location), Principle 5.6 (Durable states are historical).
- Domain model: §3 (Resource invariants), §4 (Resource Revision invariants), §8 (Immutable Record model).
- Context map: §2.1 (Shared Graph Foundation).
- Slice 1: Requirement 1 (Durable Identity Foundation), AD-WS-2, AD-WS-3.
- Invariants: identity survives movement; identifier never reused; principal identifier is opaque; Resource Identity and Revision Identity are distinct.

#### Acceptance Criteria

1. WHEN the Planning_Service requests the Identity_Service to create an Objective Resource, an Intended Outcome Resource, a Project Resource, a Deliverable Expectation Resource, an Activity Plan Resource, a Plan Revision, a Plan Review, or a Plan Approval Record, THE Identity_Service SHALL assign exactly one UUID version 7 identifier in canonical lowercase hyphenated 8-4-4-4-12 hex form, exactly once, before the entity becomes referenceable, per Slice 1 Requirement 1.1.
2. THE Identity_Service SHALL hold Activity Plan Resource Identity and Plan Revision Identity as two distinct values for every Activity Plan and SHALL hold Project Resource Identity and Project Revision Identity as two distinct values for every Project, with cardinality one Resource Identity to one or more Revision Identities, and no Revision Identity shared across Resources, per Slice 1 Requirement 1.2.
3. WHEN an authorized actor renames or relocates an Activity Plan, a Project, or an Objective within the Planning_Service, THE Identity_Service SHALL preserve the existing Resource Identity and every existing Revision Identity unchanged, generate no new Resource Identity, and replace no existing identity, per Slice 1 Requirement 1.3.
4. IF an identifier generation, import, or reference operation in the Planning_Service would assign an existing identifier to different domain content, or would introduce a malformed identifier, THEN THE Identity_Service SHALL reject the operation, return an error indication identifying the conflicting identifier, leave the existing identifier bound to its original content unchanged, and append a Denial Record to the Audit_Log within the same operation, per Slice 1 Requirement 1.4.
5. WHEN the Provenance_Navigator resolves a Relationship from either its source endpoint or its target endpoint between any pair of slice Resources (Slice 1 or Slice 2), THE Identity_Service SHALL return the same single authoritative Relationship Identity from both source-direction and backlink queries, per Slice 1 Requirement 1.5.
6. THE Identity_Service SHALL NOT reassign a once-assigned identifier to different domain content, even after Objective withdrawal, Project withdrawal, Plan Revision supersession, retention expiry, or deletion of the original content, per Slice 1 Requirement 1.6.
7. THE Identity_Service SHALL NOT encode mutable name, repository path, organization name, security classification, lifecycle state, authority, semantic version, owning Party, or other business meaning into any issued identifier for any Planning_Service entity, per Slice 1 Requirement 1.7.

### Requirement 2: Create an Objective Linked to an Authorized Decision

**User Story:** As an Objective Owner, I want to create an Objective whose first material source is an authorized Slice 1 Decision, so that strategic intent is anchored to a recorded organizational choice and remains navigable back to its source Evidence.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct), Principle 5.22 (Organizational Learning Is a Closed Loop).
- Domain model: §7.4 (Intent and Specification Record contract — Intent kinds), §10.9 (Addresses Relationship).
- Context map: §2.5 (Work Planning), §3 Customer-Supplier (Knowledge and Provenance ↔ Work Planning).
- User story map: §4 Release 1B step 1.
- Slice 1: Requirement 6 (Record an Authorized Decision) — the consumed authority.
- Invariants: Objective addresses an exact, authorized Decision Revision; Objective is a Resource with its own identity and revisions.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Objective Owner authority for the applicable scope submits an Objective creation request that names exactly one target Decision Immutable Record Identity, THE Planning_Service SHALL create an Objective Resource and an initial immutable Objective Revision within a nominal 5 seconds.
2. THE Planning_Service SHALL require, for every Objective creation, that the named target Decision Immutable Record Identity resolves to an existing Decision Immutable Record in the Knowledge_Service whose outcome at creation time is `Accept`.
3. WHEN the Planning_Service creates an Objective Revision, THE Planning_Service SHALL record on the Objective Revision the Objective statement of 1 to 4,000 characters, the rationale text of 0 to 10,000 characters, the authoring Party Identity, the applicable scope, the recorded time in UTC with millisecond precision, and an `Addresses` Relationship from the Objective Revision to the target Decision Immutable Record Identity, per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.9.
4. IF the named target Decision Immutable Record Identity does not resolve to any Decision Immutable Record, or resolves to a Decision Immutable Record whose outcome is `Reject` or `Defer`, THEN THE Planning_Service SHALL reject the Objective creation, decline to create any Resource or Revision, and return an error indication identifying the unresolvable-or-non-accepting Decision target.
5. IF the requesting Party is unauthenticated or does not hold effective Objective Owner authority for the applicable scope at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Resource or Revision, and the Audit_Log SHALL append a Denial Record conforming to the Slice 1 AD-WS-9 disclosure policy.
6. IF an Objective creation request omits the Objective statement, omits a target Decision Immutable Record Identity, names more than one target Decision Immutable Record Identity, or omits the applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Resource or Revision, and return an error indication identifying the missing or invalid attribute.
7. WHEN an Objective Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the Objective Resource Identity, Objective Revision Identity, authoring Party Identity, and recorded time within 1 second of and in the same transaction as the Objective Revision creation, per Slice 1 Requirement 13.1.

### Requirement 3: Define an Intended Outcome for an Objective

**User Story:** As an Objective Owner, I want to record one Intended Outcome for an Objective, so that the desired future condition is stated explicitly and remains distinguishable from any later observed outcome.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct), Principle 5.23 (Operational Events and Current Projections Are Distinct).
- Domain model: §7.4 (Intent and Specification Record contract — Result kinds; invariant 6 — explicit intended-vs-observed).
- Context map: §2.5 (Work Planning) consumes intended Outcomes; §2.8 (Outcome Measurement and Learning) owns observed Outcomes (out of scope).
- User story map: §4 Release 1B step 2.
- Invariants: Intended Outcome is declarative; observed Outcomes belong to a later slice.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Objective Owner authority for the applicable scope submits an Intended Outcome creation request that names exactly one target Objective Resource Identity, THE Planning_Service SHALL create an Intended Outcome Resource and an initial immutable Intended Outcome Revision within a nominal 5 seconds.
2. THE Planning_Service SHALL record on every Intended Outcome Revision an `outcome_kind` attribute set to the literal value `intended`, the success condition statement of 1 to 4,000 characters, the optional observation-window descriptor of 0 to 1,000 characters, the optional attribution-assumption text of 0 to 4,000 characters, the authoring Party Identity, the applicable scope, the recorded time in UTC with millisecond precision, and an `Addresses` Relationship from the Intended Outcome Revision to the target Objective Resource Identity.
3. IF an Intended Outcome creation request would set `outcome_kind` to any value other than the literal `intended`, or would include any field naming observed measurements, observed outcome values, observed outcome time, or attribution-evidence references, THEN THE Planning_Service SHALL reject the action, decline to create any Resource or Revision, and return an error indication identifying the prohibited observed-outcome attribute.
4. IF an Intended Outcome creation request names a target Objective Resource Identity that does not resolve to an existing Objective Resource, or names more than one target Objective, or omits the success condition statement, THEN THE Planning_Service SHALL reject the action, decline to create any Resource or Revision, and return an error indication identifying the missing or invalid attribute.
5. IF the requesting Party is unauthenticated or does not hold effective Objective Owner authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Resource or Revision, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9.
6. WHEN an Intended Outcome Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the Intended Outcome Resource Identity, Intended Outcome Revision Identity, authoring Party Identity, and recorded time within 1 second of and in the same transaction as the Intended Outcome Revision creation.

### Requirement 4: Create a Project Linked to an Objective

**User Story:** As a Project Owner, I want to create a Project that addresses an Objective, so that planned work has an explicit reason-for-being traceable to organizational intent.

**Traceability:**
- Constitution: Principle 5.10 (Context Is Preserved), Principle 5.21.
- Domain model: §7.4 (Intent kinds — Initiative, Program, Project), §10.9 (Addresses Relationship).
- Context map: §2.5 (Work Planning).
- User story map: §4 Release 1B step 3.
- Invariants: Project Resource has its own identity; addresses an existing Objective; Project is not its Activity Plan.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Project Owner authority for the applicable scope submits a Project creation request that names exactly one target Objective Resource Identity, THE Planning_Service SHALL create a Project Resource and an initial immutable Project Revision within a nominal 5 seconds.
2. THE Planning_Service SHALL record on every Project Revision the Project name of 1 to 200 characters, the Project summary of 0 to 4,000 characters, the planned start date and planned end date each expressed as ISO-8601 calendar dates with the planned start date not after the planned end date, the authoring Party Identity, the applicable scope, the recorded time in UTC with millisecond precision, and an `Addresses` Relationship from the Project Revision to the target Objective Resource Identity.
3. IF a Project creation request names a target Objective Resource Identity that does not resolve to an existing Objective Resource, names more than one target Objective, omits the Project name, supplies a planned start date later than the planned end date, or omits the applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Resource or Revision, and return an error indication identifying the missing or invalid attribute.
4. IF the requesting Party is unauthenticated or does not hold effective Project Owner authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Resource or Revision, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9.
5. THE Planning_Service SHALL hold Project Resource Identity and Activity Plan Resource Identity as two disjoint identifier sets; a Project Resource Identity SHALL NOT also identify an Activity Plan, and an Activity Plan Resource Identity SHALL NOT also identify a Project.
6. WHEN a Project Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the Project Resource Identity, Project Revision Identity, authoring Party Identity, and recorded time within 1 second of and in the same transaction as the Project Revision creation.

### Requirement 5: Declare an Expected Deliverable for a Project

**User Story:** As a Project Owner, I want to declare one expected Deliverable for a Project, so that the planned output is stated explicitly and remains distinguishable from any produced Deliverable.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct).
- Domain model: §7.4 (Intent kinds — Specification, Result), §10.9 (Addresses Relationship).
- Context map: §2.5 (Work Planning) owns Deliverable expectations; §2.6 (Work Execution) owns Deliverable Production (out of scope).
- User story map: §4 Release 1B step 4.
- Invariants: Expected Deliverable is declarative; produced deliverables are recorded in a later slice.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Project Owner authority for the applicable scope submits a Deliverable Expectation creation request that names exactly one target Project Resource Identity, THE Planning_Service SHALL create a Deliverable Expectation Resource and an initial immutable Deliverable Expectation Revision within a nominal 5 seconds.
2. THE Planning_Service SHALL record on every Deliverable Expectation Revision the expected Deliverable name of 1 to 200 characters, the expected Deliverable description of 0 to 10,000 characters, the expected Deliverable kind drawn from the enumerated set {Document, Artifact, Service, Other}, the acceptance criteria text of 0 to 10,000 characters, the authoring Party Identity, the applicable scope, the recorded time in UTC with millisecond precision, and an `Addresses` Relationship from the Deliverable Expectation Revision to the target Project Resource Identity.
3. IF a Deliverable Expectation creation request would include any field naming an actual produced Deliverable Identity, a produced Deliverable Revision Identity, a deliverable production time, a deliverable hand-off Party, or any reference to a Deliverable Production Record (out-of-scope kind), THEN THE Planning_Service SHALL reject the action, decline to create any Resource or Revision, and return an error indication identifying the prohibited produced-deliverable attribute.
4. IF a Deliverable Expectation creation request names a target Project Resource Identity that does not resolve to an existing Project Resource, names more than one target Project, omits the expected Deliverable name, supplies a Deliverable kind outside the enumerated set, or omits the applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Resource or Revision, and return an error indication identifying the missing or invalid attribute.
5. IF the requesting Party is unauthenticated or does not hold effective Project Owner authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Resource or Revision, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9.
6. WHEN a Deliverable Expectation Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the Deliverable Expectation Resource Identity, Revision Identity, authoring Party Identity, and recorded time within 1 second of and in the same transaction as the Revision creation.


### Requirement 6: Create an Activity Plan within a Project

**User Story:** As a Project Owner, I want to create an Activity Plan within a Project, so that planned work activities are organized under one named planning Resource that can carry versioned Plan Revisions.

**Traceability:**
- Constitution: Principle 5.6 (Durable states are historical), Principle 5.21.
- Domain model: §3 (Resource), §4 (Resource Revision), §7.4 (Intent kinds).
- Context map: §2.5 (Work Planning) owns Activity Plans and Plan Revisions.
- User story map: §4 Release 1B step 5.
- Invariants: Activity Plan Resource Identity is distinct from any Plan Revision Identity; an Activity Plan is created under exactly one Project.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Project Owner authority for the applicable scope submits an Activity Plan creation request that names exactly one target Project Resource Identity, THE Planning_Service SHALL create an Activity Plan Resource within a nominal 5 seconds.
2. THE Planning_Service SHALL record on every Activity Plan creation the Activity Plan title of 1 to 200 characters, the parent Project Resource Identity, the authoring Party Identity, the applicable scope, and the recorded time in UTC with millisecond precision.
3. IF an Activity Plan creation request names a target Project Resource Identity that does not resolve to an existing Project Resource, names more than one target Project, omits the Activity Plan title, or omits the applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Activity Plan Resource, and return an error indication identifying the missing or invalid attribute.
4. IF the requesting Party is unauthenticated or does not hold effective Project Owner authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Activity Plan Resource, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9.
5. WHEN an Activity Plan Resource is created, THE Audit_Log SHALL append an immutable creation record identifying the Activity Plan Resource Identity, parent Project Resource Identity, authoring Party Identity, and recorded time within 1 second of and in the same transaction as the Activity Plan creation.

### Requirement 7: Submit a Plan Revision for Review

**User Story:** As a Project Owner, I want to submit a Plan Revision against an Activity Plan, so that a versioned, reviewable snapshot of the plan exists and can be evaluated by a Plan Reviewer before approval.

**Traceability:**
- Constitution: Principle 5.6 (Durable states are historical, not overwritten), Principle 5.21.
- Domain model: §4 (Resource Revision invariants), §7.4.
- Context map: §2.5 (Work Planning).
- User story map: §4 Release 1B step 6 (precursor to approval).
- Invariants: a Plan Revision is initially a Draft Plan Revision; superseding a Draft Plan Revision is represented by a `Supersedes` Relationship per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.6.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Project Owner authority for the applicable scope submits a Plan Revision creation request that names exactly one target Activity Plan Resource Identity, THE Planning_Service SHALL create an immutable Plan Revision within a nominal 5 seconds.
2. THE Planning_Service SHALL record on every Plan Revision the parent Activity Plan Resource Identity, the planned scope statement of 1 to 10,000 characters, between 0 and 50 references to existing Deliverable Expectation Resource Identities each of which resolves at creation time, between 0 and 100 planning-assumption entries each of 1 to 2,000 characters, the optional ordering rationale of 0 to 2,000 characters, the authoring Party Identity, the applicable scope, the recorded time in UTC with millisecond precision, the lifecycle state set to the literal value `draft`, and an optional predecessor Plan Revision Identity that resolves to an unapproved Plan Revision of the same Activity Plan.
3. WHILE a Plan Revision's lifecycle state is `draft`, THE Planning_Service SHALL permit a later Plan Revision of the same Activity Plan to be recorded with the prior Plan Revision Identity as its predecessor and SHALL record a `Supersedes` Relationship from the new Plan Revision to the predecessor, leaving the predecessor Plan Revision row byte-equivalent to its prior state.
4. IF a Plan Revision creation request names a target Activity Plan Resource Identity that does not resolve to an existing Activity Plan Resource, names a Deliverable Expectation reference that does not resolve, names a predecessor Plan Revision Identity that does not resolve to a Plan Revision of the same Activity Plan, names a predecessor Plan Revision that is already an Approved Plan Revision, or omits the planned scope statement or applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Plan Revision, and return an error indication identifying each invalid attribute.
5. IF the requesting Party is unauthenticated or does not hold effective Project Owner authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Plan Revision, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9.
6. WHEN a Plan Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the parent Activity Plan Resource Identity, Plan Revision Identity, authoring Party Identity, lifecycle state, predecessor Plan Revision Identity when present, and recorded time within 1 second of and in the same transaction as the Plan Revision creation.

### Requirement 8: Record a Plan Review by a Plan Reviewer

**User Story:** As a Plan Reviewer, I want to record a review against an exact Plan Revision, so that an assessment by a Party holding review authority is preserved as evidence prior to approval.

**Traceability:**
- Constitution: Principle 5.25 (Access Is Explicit and Auditable).
- Domain model: §7.6 (Collaboration Record contract — Review Decision kind), invariant 4.
- Context map: §2.5 (Work Planning) consumes review records; §2.9 (Identity, Access, and Governance) supplies authorization.
- User story map: §4 Release 1B precursor to step 6.
- Invariants: Plan Review references an exact Plan Revision Identity; review does not approve; review records are immutable.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Plan Reviewer authority for the applicable scope submits a Plan Review against an exact Plan Revision Identity whose lifecycle state is `draft`, THE Planning_Service SHALL create a Plan Review Resource and an initial immutable Plan Review Revision within a nominal 5 seconds.
2. THE Planning_Service SHALL record on every Plan Review Revision the target Plan Revision Identity, the review outcome drawn from the enumerated set {Endorse, Changes_Requested, Reject}, the review rationale of 1 to 10,000 characters, the reviewing Party Identity, the authority basis drawn from the set defined in Slice 1 AD-WS-10 and extended per Requirement 19 if necessary, the applicable scope, and the recorded time in UTC with millisecond precision.
3. THE Planning_Service SHALL link every Plan Review Revision to its target Plan Revision through exactly one `Relates To` Relationship with a `review` semantic role marker, per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.5.
4. WHEN a Plan Review Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the Plan Review Resource Identity, Plan Review Revision Identity, target Plan Revision Identity, reviewing Party Identity, authority basis, and recorded time within 1 second of and in the same transaction as the Plan Review Revision creation.
5. IF the requesting Party is unauthenticated or does not hold effective Plan Reviewer authority for the applicable scope at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Planning_Service SHALL decline to create any Plan Review Resource or Revision, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9.
6. IF a Plan Review submission omits the target Plan Revision Identity, names a target Plan Revision that does not resolve, names a target Plan Revision whose lifecycle state is not `draft`, supplies a review outcome outside the enumerated set, omits review rationale, or omits the applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Plan Review Resource or Revision, and return an error indication identifying each invalid attribute.
7. THE Planning_Service SHALL NOT change the lifecycle state of the target Plan Revision as a consequence of recording a Plan Review; recording a Plan Review SHALL NOT by itself approve, finalize, withdraw, or supersede the target Plan Revision.

### Requirement 9: Approve a Plan Revision (Demonstration: Approved Plan Revisions are immutable)

**User Story:** As a Plan Approver, I want to approve a Plan Revision, so that organizational authority to proceed with the planned work is recorded and the Plan Revision becomes durably immutable for downstream traceability.

**Traceability:**
- Constitution: Principle 5.6 (Durable states are historical), Principle 5.21, Principle 5.25.
- Domain model: §8.5 (Governance Decision Immutable Record), §10.6 (Supersedes), invariant — Governance decisions are immutable.
- Context map: §2.5 (Work Planning); §2.9 (Identity, Access, and Governance) supplies approval authority.
- User story map: §4 Release 1B step 6.
- Slice 1: Requirement 6 (Decision Immutable Records), Requirement 13 (Audit of consequential actions).
- Invariants: an Approved Plan Revision is immutable; exactly one Plan Approval Record per Plan Revision; the approving Party held effective `approve` authority at the recorded time.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Plan Approver authority for the applicable scope submits a Plan Approval request against an exact Plan Revision Identity whose lifecycle state is `draft`, THE Planning_Service SHALL create a Plan Approval Immutable Record within a nominal 5 seconds and SHALL atomically transition the target Plan Revision's lifecycle state from `draft` to `approved` within the same transaction.
2. THE Plan Approval Immutable Record SHALL identify the target Activity Plan Resource Identity, the target Plan Revision Identity, the approval outcome drawn from the enumerated set {Approve, Reject_Approval}, the approval rationale of 1 to 4,000 characters, the approving Party Identity, the authority basis drawn from the set defined in Slice 1 AD-WS-10, the applicable scope, and the recorded time in UTC with millisecond precision.
3. WHEN the Planning_Service creates a Plan Approval Immutable Record, THE Planning_Service SHALL link the Plan Approval Record to its target Plan Revision through exactly one `Addresses` Relationship.
4. ONCE a Plan Revision's lifecycle state is `approved`, THE Planning_Service SHALL leave the Plan Revision row, every constituent field of the Plan Revision Revision, every `Supports` and `Addresses` Relationship sourced from or targeting that Plan Revision, and every Plan Review Revision targeting that Plan Revision byte-equivalent to their state immediately before approval, indefinitely, until and unless a future slice introduces a governed supersession path.
5. IF a Plan Approval submission targets a Plan Revision whose lifecycle state is not `draft`, targets a Plan Revision that is already the target of any finalized Plan Approval Immutable Record, names a Plan Revision Identity that does not resolve, omits the approval outcome, omits the approval rationale, omits the authority basis, or omits the applicable scope, THEN THE Planning_Service SHALL reject the action, decline to create any Plan Approval Record, leave the target Plan Revision byte-equivalent to its prior state, and return an error indication identifying each invalid attribute.
6. IF an actor attempts to modify or delete a previously created Plan Approval Immutable Record, or to modify or delete an Approved Plan Revision or any of its constituent rows or Relationships, THEN THE Planning_Service SHALL reject the operation, leave the affected records byte-equivalent to their prior state, return an error indication identifying the immutability violation, and append a Denial Record to the Audit_Log conforming to Slice 1 Requirement 13.5.
7. WHEN the Planning_Service creates a Plan Approval Record, THE Audit_Log SHALL append an immutable creation record identifying the Plan Approval Record Identity, target Activity Plan Resource Identity, target Plan Revision Identity, approving Party Identity, authority basis, approval outcome, and recorded time within 1 second of and in the same transaction as the Plan Approval Record creation.

### Requirement 10: Deny Unauthorized Plan Approval (Demonstration: indistinguishable denial)

**User Story:** As a Security Auditor, I want any attempt to approve a Plan Revision by a Party lacking effective Plan Approver authority to be rejected, audited, and indistinguishable from a non-existent endpoint, so that approval authority cannot be silently bypassed and existence of restricted Plan Revisions cannot be inferred from denial behavior.

**Traceability:**
- Constitution: Principle 5.25 (Access Is Explicit and Auditable), Principle 5.26 (Sensitive Information Is Governed).
- Domain model: §8.4 (Audit Event), §8.5 (Governance Decision).
- Context map: §2.9 (Identity, Access, and Governance).
- Slice 1: Requirement 7 (Deny Unauthorized Decisions), AD-WS-9 (default Completeness Disclosure policy).
- Invariants: privileged actions are restricted and auditable; sensitive information does not leak through denial.

#### Acceptance Criteria

1. IF a Party attempts to finalize a Plan Approval while lacking effective Plan Approver authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action within 2 seconds and THE Planning_Service SHALL ensure no Plan Approval Record is created, no Plan Revision lifecycle transition is recorded, and no in-flight row is persisted.
2. WHEN the Authorization_Service rejects a Plan Approval attempt, THE Audit_Log SHALL append exactly one immutable Denial Record within 1 second containing actor Party Identity, attempted action, target Activity Plan Resource Identity, target Plan Revision Identity, recorded time in UTC with millisecond precision, and denial reason code drawn from the enumerated set defined in Slice 1 Requirement 7.2.
3. WHEN evaluating Plan Approver authority for any Plan Approval attempt, THE Authorization_Service SHALL treat a role assignment as not in effect if its effective-start time is in the future, its expiration time has passed, its revocation has been recorded, or its scope does not cover the target Plan Revision's applicable scope, per Slice 1 Requirement 7.3.
4. WHEN the Authorization_Service rejects a Plan Approval attempt because of missing authority, THE Authorization_Service SHALL return a denial response containing only a generic denial indicator, the denial reason code, and a correlation identifier, and SHALL NOT contain Plan Reviewer or Plan Approver Party identities, Plan Revision contents, Activity Plan titles, Project names, Objective statements, role assignment details, or any target existence information beyond the requesting Party's view authority, per the Slice 1 `slice-default-2026` policy.
5. THE Planning_Service SHALL leave the targeted Plan Revision row, every constituent Relationship, every Plan Review Revision targeting that Plan Revision, and every related Activity Plan, Project, Deliverable Expectation, Intended Outcome, and Objective row byte-equivalent to their state immediately before the denied Plan Approval attempt.
6. IF the Audit_Log append for a denied Plan Approval attempt fails, THEN THE Authorization_Service SHALL retry up to 3 times, keep the action denied, and surface an audit-failure indicator to the operator so that denial and audit cannot silently diverge, per Slice 1 Requirement 7.6.
7. THE Planning_Service SHALL produce denial responses for Plan Approval, Plan Review, Objective creation, Intended Outcome creation, Project creation, Deliverable Expectation creation, Activity Plan creation, and Plan Revision creation that are indistinguishable in counts, identifier sets, response size, error category, error wording, and latency (within 100 milliseconds variation) from responses produced when the target Resource does not exist, when the requesting Party lacks view authority on the target Resource, or when the action is denied for missing authority, per the Slice 1 `slice-default-2026` policy.


### Requirement 11: Distinct Plan Reviewer and Plan Approver Authority Types

**User Story:** As a Resource Steward, I want Plan Reviewer authority and Plan Approver authority to be modelled as two distinct authority types that are never substituted for each other, so that "who can review" and "who can approve" remain separately granted, separately revoked, and separately auditable.

**Traceability:**
- Constitution: Principle 5.25 (Access Is Explicit and Auditable).
- Context map: §2.9 (Identity, Access, and Governance) owns Authorization Policy and Role Assignment.
- Slice 1: Requirement 12 (Contextual Role Assignment and Enforcement) — view, modify, approve are three distinct authority types; non-substitution rule.
- Gap: G-6 (additive extension of the authority enumeration).
- Invariants: review authority ≠ approve authority; non-substitution rule applies to both directions.

#### Acceptance Criteria

1. THE Authorization_Service SHALL accept and persist a `review` authority value in the `authorities_granted` set of a Role Assignment, alongside the Slice 1 values `view`, `modify`, and `approve`, expanding the canonical authority enumeration additively per Gap G-6.
2. WHEN a Resource Steward assigns a Plan Reviewer role to a Party, THE Authorization_Service SHALL accept a Role Assignment whose `authorities_granted` set contains `review` and SHALL NOT require, infer, or auto-include `approve` in the same Role Assignment.
3. WHEN a Resource Steward assigns a Plan Approver role to a Party, THE Authorization_Service SHALL accept a Role Assignment whose `authorities_granted` set contains `approve` and SHALL NOT require, infer, or auto-include `review` in the same Role Assignment.
4. WHEN the Authorization_Service evaluates a Plan Review attempt, THE Authorization_Service SHALL require the `review` authority on the evaluated Role Assignment and SHALL NOT permit the action solely on the basis of `approve` authority held by the same Party.
5. WHEN the Authorization_Service evaluates a Plan Approval attempt, THE Authorization_Service SHALL require the `approve` authority on the evaluated Role Assignment and SHALL NOT permit the action solely on the basis of `review` authority held by the same Party.
6. THE Authorization_Service SHALL distinguish `view`, `modify`, `review`, and `approve` as four distinct authority types and SHALL NOT substitute one authority type for another when evaluating any consequential action, extending the Slice 1 Requirement 12.3 non-substitution rule.
7. WHEN the Authorization_Service evaluates any action requiring `review` or `approve` authority, THE Authorization_Service SHALL append an evaluation record to the Audit_Log per Slice 1 Requirement 12.5 identifying the specific authority required and the specific authority held on the evaluated Role Assignment.

### Requirement 12: Plan / Execution Separation (Slice Scope Invariant)

**User Story:** As a Pilot Reviewer, I want this slice to record only planning information and to reject any attempt to record execution information on planning Resources, so that the Plan/Execution separation principle is enforced in software, not only in documentation.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct), Principle 5.23 (Operational Events and Current Projections Are Distinct).
- Context map: §2.5 (Work Planning) ↔ §2.6 (Work Execution) — distinct contexts with distinct ownership.
- User story map: §5.4 (Plan and Execution Separation must-have story group).
- Invariants: planning Resources do not carry execution facts; execution context owns Work Assignment, Work Event, Time Entry, Milestone Acceptance, Deliverable Production, Blockage Observation, Completion Record.

#### Acceptance Criteria

1. THE Planning_Service SHALL NOT accept, persist, or expose any Work Assignment, Work Event, Time Entry, Milestone Acceptance, Deliverable Production Record, Blockage Observation, or Completion Record, and SHALL NOT include any field naming an actor-assigned-time, work-started-time, work-completed-time, time-entry quantity, actual-cost value, percent-complete value, blockage-observation text, or completion-evidence reference on any planning Resource or Revision.
2. IF a Plan Revision creation request, Activity Plan creation request, Objective creation request, Intended Outcome creation request, Project creation request, Deliverable Expectation creation request, Plan Review submission, or Plan Approval submission contains any of the prohibited execution attributes named in 12.1, THEN THE Planning_Service SHALL reject the request, decline to create any Resource or Revision, and return an error indication identifying each prohibited execution attribute.
3. THE Planning_Service SHALL NOT expose any HTTP endpoint, function, or relationship type whose stated purpose is to record execution facts, assignment acceptances, time entries, deliverable production, or completion against any Plan Revision, Activity Plan, Project, Deliverable Expectation, Intended Outcome, or Objective Resource created by this slice.
4. WHEN the Planning_Service returns any planning Resource representation, THE Planning_Service SHALL NOT include any derived current-execution status, derived percent-complete value, derived actual-cost value, or derived remaining-work value in the response body.
5. WHEN a future slice introduces execution Resources targeting any planning Resource created by this slice, THE Walking_Slice_System SHALL leave every planning Resource row, Revision, and Relationship created by this slice byte-equivalent to its prior state, per Principle 5.23.

### Requirement 13: Output / Outcome Separation (Slice Scope Invariant)

**User Story:** As a Pilot Reviewer, I want this slice to record only intended outcomes and expected deliverables and to reject any attempt to record observed outcomes or produced deliverables, so that the Output/Outcome separation principle is enforced in software, not only in documentation.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct).
- Domain model: §7.4 invariant 6 — Outcome is explicitly distinguished as intended or observed.
- Context map: §2.8 (Outcome Measurement and Learning) owns Observed Outcomes (out of scope).
- User story map: §5.5 (Output and Outcome Separation must-have story group).
- Invariants: declared intent does not satisfy observed outcome; declared deliverable does not satisfy produced deliverable.

#### Acceptance Criteria

1. THE Planning_Service SHALL NOT accept, persist, or expose any Observed Outcome, Measurement Definition, Measurement Record, Observation Window observation, Outcome Review, attribution-evidence reference, or actual-success-condition assessment on any Intended Outcome Resource or Revision created by this slice.
2. THE Planning_Service SHALL NOT accept, persist, or expose any Deliverable Production Record, produced-Deliverable Resource Identity, produced-Deliverable Revision Identity, hand-off receipt, or acceptance-by-customer record on any Deliverable Expectation Resource or Revision created by this slice.
3. WHEN the Planning_Service returns any Intended Outcome Resource representation, THE Planning_Service SHALL include the `outcome_kind` attribute set to the literal value `intended` and SHALL NOT include any field whose value would constitute an observed measurement, observed outcome value, observed outcome time, attribution-evidence reference, or success-condition assessment.
4. WHEN the Planning_Service returns any Deliverable Expectation Resource representation, THE Planning_Service SHALL distinguish the Resource kind as Deliverable Expectation and SHALL NOT label or alias the Resource as a Deliverable Production, produced Deliverable, or accepted Deliverable.
5. IF an Intended Outcome or Deliverable Expectation creation request contains any of the prohibited observed-outcome or produced-deliverable attributes named in 13.1 or 13.2, THEN THE Planning_Service SHALL reject the request, decline to create any Resource or Revision, and return an error indication identifying each prohibited attribute.
6. WHEN a future slice introduces Observed Outcome or Deliverable Production Resources targeting any planning Resource created by this slice, THE Walking_Slice_System SHALL leave every Intended Outcome Resource and Deliverable Expectation Resource byte-equivalent to its prior state, per Principle 5.23.

### Requirement 14: Provenance Chain to Originating Decision and Evidence

**User Story:** As a Plan Reviewer or Decision Reviewer, I want to navigate from any Plan Approval Record back through its Plan Revision, Activity Plan, Project, Objective, originating Decision, Recommendation, supporting Findings, and exact Content Region Occurrences to the precise Document Revision text, so that I can verify why a plan was approved.

**Traceability:**
- Constitution: Principle 5.9 (Provenance Is Preserved End to End), Principle 5.22 (Organizational Learning Is a Closed Loop).
- Domain model: §19 (Provenance graph).
- Context map: §2.4 (Knowledge and Provenance) ↔ §2.5 (Work Planning).
- Slice 1: Requirement 11 (Navigation back to exact Evidence).
- Invariants: provenance is end-to-end and authorization-aware; missing links are visible; sensitive information does not leak.

#### Acceptance Criteria

1. WHEN an authorized Party requests the provenance chain of a Plan Approval Immutable Record, THE Provenance_Navigator SHALL return an ordered traversal Plan Approval Record → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision Immutable Record → Recommendation Revision → Finding Revision(s) → Content Region Occurrence(s) → Document Revision, identifying each node by its Identity and (where applicable) Revision Identity.
2. WHEN the Provenance_Navigator returns a Content Region Occurrence in a Planning Provenance Chain, THE Provenance_Navigator SHALL include the exact start anchor, end anchor, and bounded text span of that Occurrence in the originating Document Revision, byte-equivalent to the text recorded for that Region Occurrence and digest-matching against the recorded content digest, per Slice 1 Requirement 11.2.
3. IF a node in a Planning Provenance Chain is restricted from the requesting Party, THEN THE Provenance_Navigator SHALL replace that node with a policy-conformant redaction marker containing only a generic redaction indicator and the original node kind, and SHALL NOT disclose any identifier, count, or attribute value of the redacted node beyond the `slice-default-2026` policy as extended by Requirement 17.
4. IF a required upstream link is unresolved, restricted, stale, or unavailable, THEN THE Provenance_Navigator SHALL identify the gap explicitly with a gap descriptor identifying the stage in the chain, the gap category drawn from {unavailable, restricted, stale, unresolved}, and the Identity of the next reachable node where applicable.
5. THE Provenance_Navigator SHALL produce the same Planning Provenance Chain for the same Plan Approval Record Identity, requesting Party authority set, and effective time inputs (idempotent retrieval), within 5 seconds for chains of up to 60 nodes.
6. IF the requested Plan Approval Record Identity does not resolve to a Plan Approval Immutable Record, THEN THE Provenance_Navigator SHALL return an error indication identifying the unresolvable Plan Approval reference and SHALL NOT disclose existence of any related planning Resources.
7. IF the requesting Party is unauthenticated or lacks any view authority on the Plan Approval Immutable Record itself, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from one for a non-existent Plan Approval Record, conforming to the Slice 1 `slice-default-2026` policy.

### Requirement 15: Authorization-Aware Backlinks Extended to Planning Nodes

**User Story:** As a Project Owner, Objective Owner, or Source Owner, I want to see which Plan Revisions, Activity Plans, Projects, Objectives, Intended Outcomes, Deliverable Expectations, Plan Reviews, and Plan Approval Records depend on a particular planning or knowledge Resource, subject to my authorization, so that I can understand downstream impact across the planning chain without leaking restricted relationships.

**Traceability:**
- Constitution: Principle 5.12 (Dependencies Are Visible Before Change), Principle 5.25, Principle 5.26.
- Context map: §2.1 (Shared Graph Foundation), §3 Cross-Context Rules — bidirectional discoverability does not transfer authority.
- Slice 1: Requirement 8 (Authorization-Aware Backlinks).
- Invariants: bidirectional discovery, no inference leakage, discovery does not transfer authority.

#### Acceptance Criteria

1. WHEN an authorized Party holding view authority on the queried endpoint requests inbound Relationships for an Objective, Intended Outcome, Project, Deliverable Expectation, Activity Plan, Plan Revision, Plan Review, or Plan Approval Record, THE Provenance_Navigator SHALL return every inbound Relationship for which the requesting Party holds applicable view authority on both the Relationship and its source endpoint, in deterministic ordering, within 2 seconds for result sets of up to 500 backlinks.
2. WHEN the Provenance_Navigator returns a backlink whose source endpoint is a planning Resource introduced by this slice, THE Provenance_Navigator SHALL identify the backlink by its Relationship Identity, Relationship Type, source endpoint Identity, source endpoint Type, source endpoint Revision Identity, and authoring Party Identity, per Slice 1 Requirement 8.2.
3. IF the requesting Party lacks authority to know that an inbound Relationship or its source endpoint exists, THEN THE Provenance_Navigator SHALL omit the Relationship from results and SHALL produce results indistinguishable in counts, identifier sets, ordering positions, pagination cursors, response size, and latency (within 100 milliseconds variation) from results in which the omitted Relationships do not exist, per Slice 1 Requirement 8.3.
4. THE Provenance_Navigator SHALL NOT grant the requesting Party any view, modify, review, or approve authority on the source endpoint, on the Relationship Identity itself, or on any traversed Revisions of the source endpoint, solely as a result of returning a backlink, per Slice 1 Requirement 8.4 and Cross-Context Rule 8.
5. IF the requesting Party is unauthenticated or lacks view authority on the queried endpoint, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from a response for a non-existent endpoint, conforming to the Slice 1 `slice-default-2026` policy.
6. THE Provenance_Navigator SHALL bound each backlink response for planning endpoints to at most 500 Relationships and SHALL provide a continuation reference whose length, identifier values, and presence do not vary based on the existence of Relationships the requesting Party lacks authority to know.

### Requirement 16: Audit of Consequential and Denied Planning Actions

**User Story:** As an Auditor, I want every consequential creation and every denied unauthorized attempt within the second walking slice to leave an immutable Audit_Log record, so that I can reconstruct what happened and what was rejected across both slices.

**Traceability:**
- Constitution: Principle 5.25 (Access Is Explicit and Auditable), Principle 5.7 (No Acknowledged Work Is Silently Lost).
- Domain model: §8.4 (Audit Event invariants — append-only).
- Slice 1: Requirement 13 (Audit of Consequential and Denied Actions).
- Invariants: audit is append-only; insertion order preserved by recorded time and append sequence; failure to audit rolls back the originating action.

#### Acceptance Criteria

1. WHEN the Walking_Slice_System finalizes the creation of an Objective Revision, Intended Outcome Revision, Project Revision, Deliverable Expectation Revision, Activity Plan Resource, Plan Revision, Plan Review Revision, or Plan Approval Immutable Record, THE Audit_Log SHALL append an immutable record identifying actor Party Identity, action type, target Resource Identity, target Revision Identity when applicable, recorded time in UTC with millisecond precision, and operation correlation identifier, before the success response returns to the caller, per Slice 1 Requirement 13.1.
2. WHEN the Authorization_Service denies any consequential planning action (Objective, Intended Outcome, Project, Deliverable Expectation, Activity Plan, Plan Revision, Plan Review, or Plan Approval), THE Audit_Log SHALL append an immutable Denial Record identifying actor Party Identity, attempted action, target Identity, target Revision Identity when applicable, recorded time, denial reason category drawn from the enumerated set in Slice 1 Requirement 7.2, and correlation identifier, before the denial response returns to the caller, per Slice 1 Requirement 13.2.
3. THE Audit_Log SHALL remain append-only across both slices and SHALL reject all update and delete operations on previously appended records, per Slice 1 Requirement 13.3.
4. THE Audit_Log SHALL preserve insertion order of appended records using recorded time as primary order and append sequence as tiebreaker across both slices, per Slice 1 Requirement 13.4.
5. IF an actor attempts to modify or delete a previously appended Audit_Log record arising from a Slice 2 planning action, THEN THE Audit_Log SHALL reject the operation and SHALL append an immutable Denial Record covering the rejected attempt, per Slice 1 Requirement 13.5.
6. IF an audit append for any consequential planning creation or planning denial fails, THEN THE Walking_Slice_System SHALL roll back the originating action, decline to expose any artifact of that action, and return an error indication identifying the audit append failure, per Slice 1 Requirement 13.6.


### Requirement 17: Additive Extension of the Completeness Disclosure Policy

**User Story:** As a Disclosure Policy Owner, I want the new planning node kinds introduced by this slice to be covered by an additive extension of the Slice 1 `slice-default-2026` policy rather than by a separate policy, so that one cohesive disclosure contract governs every backlink, provenance, and denial response across both slices.

**Traceability:**
- Constitution: Principle 5.25, Principle 5.26.
- Context map: §3 Cross-Context Rule 9 (Authorization filtering shall follow completeness-disclosure and inference-risk policy).
- Slice 1: AD-WS-9 (`slice-default-2026` default Completeness Disclosure policy).
- Gap: G-7 (additive policy extension for new node kinds).
- Invariants: policy identity is unchanged; rule set is additively extended; restricted-vs-nonexistent observability remains constant across both slices.

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL extend the policy named `slice-default-2026` in the `Disclosure_Policies` registry to cover every node kind introduced by this slice — Objective Resource, Objective Revision, Intended Outcome Resource, Intended Outcome Revision, Project Resource, Project Revision, Deliverable Expectation Resource, Deliverable Expectation Revision, Activity Plan Resource, Plan Revision, Plan Review Resource, Plan Review Revision, and Plan Approval Immutable Record — and SHALL NOT introduce a separate disclosure policy or replace the existing policy.
2. WHEN the Provenance_Navigator or the Authorization_Service encounters a restricted node whose kind was introduced by this slice, THE Walking_Slice_System SHALL replace the node with a redaction marker of the form `{"kind": "<node_kind>", "redacted": true}` carrying no identifier, attribute, or count, per the AD-WS-9 rule set.
3. WHEN the Provenance_Navigator or the Authorization_Service encounters a node introduced by this slice in an `unavailable`, `stale`, or `unresolved` category, THE Walking_Slice_System SHALL return a gap descriptor containing only `stage`, `category`, and (if the next reachable node is visible to the requesting Party) the next reachable node's identity, per the AD-WS-9 rule set.
4. THE Walking_Slice_System SHALL produce indistinguishable restricted-vs-nonexistent observability for every node kind introduced by this slice across counts, identifier sets, pagination cursors, response sizes, error wording, and latency (within 100 milliseconds variation), matching the Slice 1 observability guarantees.
5. THE Walking_Slice_System SHALL record the additive extension of `slice-default-2026` as a new row entry in the policy registry or as an additive update on the existing row that does not alter the policy identity or the Slice 1 rule scope; the recorded extension SHALL identify each newly covered node kind, the recorded date of the extension, and the backlog ADR identifier reserved for replacement, per Gap G-7.

### Requirement 18: Explainable Projection of Plan Status

**User Story:** As a Pilot Reviewer, I want any projected status surfaced by the Planning_Service (for example, "Plan Revision under review", "Plan Approved", "Provenance incomplete") to be explainable from its source Records, so that derived views of plan status cannot be mistaken for authoritative facts.

**Traceability:**
- Constitution: Principle 5.23 (Operational Events and Current Projections Are Distinct), Principle 5.30 (System Health Must Be Observable).
- Slice 1: Requirement 14 (Explainable Projection of Slice Status), Projection Envelope from Slice 1 design.
- Invariants: projections carry derivation indicator; source records are unaltered when corrections arrive.

#### Acceptance Criteria

1. WHEN the Planning_Service exposes a projected status over slice Resources — including but not limited to "Plan Revision draft", "Plan Revision under review", "Plan Approved", "Plan Revision superseded", "Provenance incomplete", or "Plan Revision orphaned" — THE Planning_Service SHALL include alongside the projected status in the same response the Projection Definition, source Resource Identities, source Revision Identities, applicable temporal boundary, and generated time, with the temporal boundary and generated time expressed in ISO-8601 form with at least second precision, per Slice 1 Requirement 14.1.
2. THE Planning_Service SHALL include on every exposed projected status a derivation indicator distinguishing it from authoritative source Records, per Slice 1 Requirement 14.2 and Principle 5.23.
3. WHEN a corrected or late-arriving source fact changes a Plan Revision's projected status (for example, an Audit_Log record reveals a previously unrecorded Plan Approval), THE Planning_Service SHALL retain every prior source Record, Revision, and correction record byte-equivalent to its recorded state and SHALL append new facts as additional Revisions or Records rather than overwriting existing ones, per Slice 1 Requirement 14.3.
4. IF the Projection Definition or any required source Revision cannot be resolved, THEN THE Planning_Service SHALL withhold the projected status, return an explanation-unavailable indicator identifying the missing element, and leave stored source Records unchanged, per Slice 1 Requirement 14.4.

### Requirement 19: Reuse and Non-Modification of Slice 1 Contexts

**User Story:** As a Project Owner, I want the implementation of this slice to extend Slice 1 contexts only through additive interfaces rather than through modification of existing behavior, so that downstream work and previously-recorded evidence chains remain stable.

**Traceability:**
- Constitution: Principle 5.4 (Authority and Derivation Are Distinct), Principle 5.6 (Durable states are historical), Principle 5.29 (Empirical Learning Constrains Conceptual Expansion).
- Context map: §3 Cross-Context Rules — context translation, no silent mutation across contexts.
- Slice 1: Requirement 16 (Prerequisite Architecture Decisions), AD-WS-1 through AD-WS-13.
- Gaps: G-6 through G-10 (recorded in §"Gaps Flagged for Resolution") all require additive Interim ADR records under this requirement's regime.
- Invariants: Slice 1 modules in `src/walking_slice/` are not modified to satisfy this slice; new behavior is recorded as an additive Planning module or as additive extension records.

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL implement every new behavior introduced by this slice in a new Planning module (or in a set of new modules subordinate to a new Planning context) without removing, renaming, narrowing, or changing the semantics of any function, class, table, trigger, route, or invariant established by Slice 1.
2. WHERE this slice extends a Slice 1 enumeration (for example, the authority enumeration in Requirement 11) or a Slice 1 registry (for example, the disclosure policy in Requirement 17), THE Walking_Slice_System SHALL implement the extension as an additive change that preserves every Slice 1 enumeration member, registry row, and behavior unchanged.
3. WHEN this slice records a Relationship from a Slice 2 planning Resource to a Slice 1 Resource (for example, an `Addresses` Relationship from an Objective Revision to a Slice 1 Decision Immutable Record), THE Walking_Slice_System SHALL leave the Slice 1 Resource row, Revision, and any pre-existing Relationships sourced from or targeting that Resource byte-equivalent to their prior state.
4. THE Walking_Slice_System SHALL NOT mutate any Audit_Records row, Identifier_Registry row, Interim_ADR_Records row, Disclosure_Policies row, Decisions row, Role_Assignments row, Document_Revisions row, Region_Occurrences row, Finding_Revisions row, Recommendation_Revisions row, Relationships row, Trail_Revisions row, Trail_Steps row, or Provenance_Manifests row created by Slice 1 as a consequence of any Slice 2 planning action.
5. THE Walking_Slice_System SHALL record additive Interim ADR records covering the gaps introduced by this slice (Gaps G-6 through G-10 — see §"Gaps Flagged for Resolution") as new rows in the `Interim_ADR_Records` registry, each identifying the motivating Requirement number, the motivating criterion number, the observable behavior chosen, the recorded date of the choice, and the backlog ADR identifier, per Slice 1 Requirement 16.3.
6. IF a Slice 2 implementation change would require modification of any Slice 1 module behavior or schema, THEN the Walking_Slice_System SHALL record the proposed modification as a new Interim ADR row and SHALL halt the Slice 2 implementation until the user is asked to approve the modification, so that Slice 1 stability remains an explicit decision rather than a side effect.

### Requirement 20: Correctness Properties for Property-Based Testing

**User Story:** As a Verification Engineer, I want the second walking slice to be verified by property-based tests that exercise the slice's invariants over generated inputs, so that the named demonstrations are tested at the level of properties, not only worked examples.

**Traceability:**
- Slice 1: Requirement 15 (Correctness Properties for Property-Based Testing), AD-WS-13 (Hypothesis ≥ 100 cases per property, seeded).
- Domain model: §3 Resource invariants, §4 Resource Revision invariants, §8 Immutable Record invariants, §10 Relationship Type invariants.
- Invariants: planning operations preserve identity, immutability, authority separation, and provenance traceability under all generated inputs.

Each acceptance criterion below states a property the implementation SHALL preserve under property-based testing.

#### Acceptance Criteria

1. **Decision-to-Objective anchoring (invariant).** FOR ALL Objective Revisions recorded by the Planning_Service, the Walking_Slice_System SHALL satisfy: every Objective Revision has exactly one `Addresses` Relationship to a Decision Immutable Record Identity that resolves in the Knowledge_Service and whose outcome at Objective creation time was `Accept`. No Objective Revision exists without a matching authorized Decision.
2. **Planning-Resource authority (invariant).** FOR ALL Objective Revisions, Intended Outcome Revisions, Project Revisions, Deliverable Expectation Revisions, Activity Plan Resources, and Plan Revisions, the Walking_Slice_System SHALL satisfy: the authoring Party held an effective Role Assignment at the Revision's recorded time whose granted authorities include `modify`, whose scope covers the target Resource's applicable scope, and whose effective period encloses the Revision's recorded time. No planning Revision exists without a matching authority record.
3. **Reviewer/Approver authority non-substitution (invariant).** FOR ALL Plan Review Revisions, the reviewing Party held an effective Role Assignment whose granted authorities include `review` at the Plan Review's recorded time; and FOR ALL Plan Approval Immutable Records, the approving Party held an effective Role Assignment whose granted authorities include `approve` at the Plan Approval's recorded time. No Plan Review exists whose reviewing Party held only `approve` authority. No Plan Approval exists whose approving Party held only `review` authority.
4. **Approved Plan Revision immutability (invariant).** FOR ALL Plan Revisions whose lifecycle state has been `approved` at any observation point in the test session, the Walking_Slice_System SHALL satisfy: at every later observation point in the test session, the Plan Revision row, every constituent field of the Plan Revision Revision, every `Supports` and `Addresses` Relationship sourced from or targeting that Plan Revision, and every Plan Review Revision targeting that Plan Revision are byte-equivalent to their state at first approval.
5. **Plan/Execution separation (invariant).** FOR ALL planning Resources created by the Planning_Service, the Walking_Slice_System SHALL satisfy: no row of any planning Resource carries any execution-attribute value (Work Assignment, Work Event, Time Entry, Milestone Acceptance, Deliverable Production Record, Blockage Observation, Completion Record) and no HTTP response body for a planning Resource includes a derived current-execution status, percent-complete value, actual-cost value, or remaining-work value.
6. **Output/Outcome separation (invariant).** FOR ALL Intended Outcome Revisions and Deliverable Expectation Revisions created by the Planning_Service, the Walking_Slice_System SHALL satisfy: every Intended Outcome Revision carries `outcome_kind = "intended"` and carries no observed-measurement, observed-outcome-value, observed-outcome-time, or attribution-evidence attribute; every Deliverable Expectation Revision carries no produced-Deliverable Identity, produced-Deliverable Revision Identity, hand-off receipt, or acceptance-by-customer attribute.
7. **Provenance chain end-to-end (invariant).** FOR ALL Plan Approval Immutable Records whose entire Planning Provenance Chain is visible to a requesting Party, the Walking_Slice_System SHALL satisfy: traversal from the Plan Approval Record yields the ordered sequence Plan Approval → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision → Recommendation Revision → Finding Revision(s) → Content Region Occurrence(s) → Document Revision; every node identity in the returned chain resolves; the returned Content Region Occurrence span fields match the digest recorded on the Region Occurrence; and the chain is byte-equivalent across at least five repeated invocations of `navigate(plan_approval, party, t)` (idempotent retrieval).
8. **Indistinguishable denial for planning endpoints (metamorphic).** FOR ALL Parties `P` and `P′` differing only in that `P′` lacks effective Plan Approver authority, Plan Reviewer authority, Objective Owner authority, or Project Owner authority on some planning Resource `R`, the Walking_Slice_System SHALL satisfy: responses returned to `P′` for creation, review, or approval attempts on `R` are indistinguishable from responses produced when `R` does not exist, across observable channels result count, identifier set, ordering positions, pagination cursors, response size, error category, error wording, and latency (within 100 milliseconds variation).
9. **Backlink bidirectionality for planning Resources (round-trip).** FOR ALL Relationships `R` recorded between planning Resources or between a planning Resource and a Slice 1 Resource, and FOR ALL requesting Parties `P` who hold view authority on both `R` and its source endpoint, the Walking_Slice_System SHALL satisfy: the Provenance_Navigator returns `R` from the target's backlink query if and only if `R` is returned from the source's outbound query, and the Relationship attribute values returned from both directions are identical.
10. **Plan Approval uniqueness (invariant).** FOR ALL Plan Revision Identities created in any test session, the Walking_Slice_System SHALL satisfy: at most one Plan Approval Immutable Record exists for a given target Plan Revision Identity; a second Plan Approval attempt against the same Plan Revision is rejected with no Plan Approval Record persisted.
11. **Slice 1 non-modification (invariant).** FOR ALL test sessions exercising the Planning_Service, the Walking_Slice_System SHALL satisfy: at every observation point after any sequence of Slice 2 actions, every Audit_Records row, Identifier_Registry row, Interim_ADR_Records row, Disclosure_Policies row (apart from the additive extension permitted by Requirement 17.5), Decisions row, Role_Assignments row, Document_Revisions row, Region_Occurrences row, Finding_Revisions row, Recommendation_Revisions row, Relationships row, Trail_Revisions row, Trail_Steps row, and Provenance_Manifests row created by Slice 1 is byte-equivalent to its state before the Slice 2 actions began.
12. **Identity uniqueness across slices (invariant).** FOR ALL identifiers issued by the Identity_Service in any test session covering both slices, the Walking_Slice_System SHALL satisfy: identifiers are unique across both slices and across every Resource kind, are in canonical UUIDv7 lowercase hyphenated form, and do not embed business metadata, per Slice 1 Requirement 1.1 and Requirement 15.10.
13. **Repeatable property runs (operational).** THE property-based test suite for Slice 2 SHALL execute at least 100 generated cases per property, record the seed of every test invocation, and on re-execution with the same seed produce identical pass/fail outcomes and identical minimal counterexamples for failing properties, per Slice 1 Requirement 15.13.

Properties 1–13 are the verification targets for the property-based test suite associated with this slice. They complement, and do not replace, the Slice 1 property suite; both suites run together in the cumulative verification of the Walking_Slice_System.

### Requirement 21: Prerequisite Architecture Decisions and Interim ADRs

**User Story:** As a Project Owner, I want this slice's implementation to depend only on architecture decisions whose status is `Accepted` or that are explicitly recorded as Interim ADRs, so that downstream work does not rest on unresolved foundational choices.

**Traceability:**
- Constitution: Principle 5.29 (Empirical Learning Constrains Conceptual Expansion).
- Slice 1: Requirement 16 (Prerequisite Architecture Decisions), AD-WS-1 through AD-WS-13, Interim ADR records for Gaps G-1 through G-5.
- Invariants: every interim choice is recorded; transition to an accepted ADR requires explicit revision of the affected criteria.

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL, for every identity, audit, authorization, evidence, knowledge, trail, and provenance behavior reused from Slice 1, conform to the Slice 1 acceptance criteria for that behavior without weakening, broadening, or replacing those criteria, per Slice 1 Requirement 16.1.
2. WHERE a behavior in this slice requires a choice that is not yet resolved by an `Accepted` ADR (Gaps G-6 through G-10 in §"Gaps Flagged for Resolution"), THE Walking_Slice_System SHALL implement the behavior required by the specific acceptance criteria in this document that motivated the dependency.
3. WHERE the slice implements an interim behavior in advance of a backlog ADR being `Accepted`, THE project SHALL record, for each such interim behavior, the motivating Requirement number, the motivating criterion number, the observable behavior chosen, the recorded date of the choice, and the backlog ADR identifier reserved for replacement, and SHALL make the record retrievable by backlog ADR identifier in the `Interim_ADR_Records` registry, per Slice 1 Requirement 16.3.
4. IF a backlog ADR transitions to `Accepted` status with a decision whose observable behavior is not consistent with that ADR's accepted decisions, THEN the slice implementation SHALL be revised so that every affected acceptance criterion is satisfied before the verification status of the affected criteria advances beyond `Specified`, per Slice 1 Requirement 16.4.


## Out-of-Scope Boundaries

The following are intentionally deferred from this slice and SHALL NOT be required to be implemented to satisfy the requirements above. They are listed here to make scope discipline explicit and to align with [`documents/07-user-story-map.md`](../../../documents/07-user-story-map.md) §§4 and 5 and with the Plan/Execution and Output/Outcome separation principles in [`documents/00-project-constitution.md`](../../../documents/00-project-constitution.md) §5.21.

- Slice 1C — Planned Work to Deliverable: Work Assignment, Work Event, Time Entry, Milestone Acceptance, Deliverable Production Record, Blockage Observation, Completion Record, derived current status from execution records.
- Slice 1D — Deliverable to Outcome Review: Measurement Definition, Measurement Record, Observation Window observation, Observed Outcome Resource, Outcome Review.
- Slice 1E — Learning to Adaptation: Learning Record, Adaptation Decision, Plan supersession beyond the approval-immutability boundary, revised-Objective workflow.
- Slice 2 — Reproducible Publication of planning artifacts: Publication Candidate, Publication Assessment, Published Version, Rendered Output association for planning Resources.
- Slice 3 — Investment, cost, capacity, and portfolio reporting: Budget, Allocation, Capacity Plan, Rate, Estimate, Commitment, Expenditure reference, Forecast Definition, Portfolio Projection.
- Programs, Initiatives, Roadmaps, and Milestones beyond a single Activity Plan within a single Project.
- Multiple parallel Plan Revisions; concurrent-author reconciliation of Draft Plan Revisions; merge of competing Draft Plan Revisions.
- Two-of-N approvals, conditional approvals, delegated approvals, and time-bounded approvals; this slice records exactly one Plan Approver per Plan Revision.
- Withdrawal, redaction, retention expiry, or cryptographic erasure of Approved Plan Revisions (a later governed supersession path is out of scope).
- Portability export and reconstruction of planning Resources.
- Automated Agent contribution provenance for planning Resources beyond recording that an authoring Party is human.
- Modifications to Slice 1 contexts (Identity, Audit, Authorization, Evidence, Knowledge, Trails, Provenance) other than the additive enumeration and registry extensions defined in Requirements 11, 17, and 19.

## Traceability Summary

This slice realizes a strict subset of upstream artifacts. The table below summarizes the principal sources of authority for each requirement; the **Traceability** blocks within each requirement above are authoritative for individual mappings.

| Req | Primary Constitution | Primary Domain Model | Primary Context Map | Primary Slice 1 Anchor |
|---|---|---|---|---|
| 1 | 5.5, 5.6 | §3, §4, §8 | §2.1 | Req 1, AD-WS-2, AD-WS-3 |
| 2 | 5.21, 5.22 | §7.4, §10.9 | §2.5 | Req 6 (Decision) |
| 3 | 5.21, 5.23 | §7.4 (Result kinds) | §2.5, §2.8 | — |
| 4 | 5.10, 5.21 | §7.4, §10.9 | §2.5 | — |
| 5 | 5.21 | §7.4, §10.9 | §2.5, §2.6 | — |
| 6 | 5.6, 5.21 | §3, §4, §7.4 | §2.5 | — |
| 7 | 5.6, 5.21 | §4, §7.4, §10.6 | §2.5 | — |
| 8 | 5.25 | §7.6 (Review Decision), §10.5 | §2.5, §2.9 | — |
| 9 | 5.6, 5.21, 5.25 | §8.5, §10.6 | §2.5, §2.9 | Req 6, Req 13 |
| 10 | 5.25, 5.26 | §8.4, §8.5 | §2.9 | Req 7, AD-WS-9 |
| 11 | 5.25 | §9 (Contextual Roles) | §2.9 | Req 12 |
| 12 | 5.21, 5.23 | §7.4 | §2.5 ↔ §2.6 | — |
| 13 | 5.21 | §7.4 invariant 6 | §2.8 | — |
| 14 | 5.9, 5.22 | §19 (Provenance graph) | §2.4 ↔ §2.5 | Req 11 |
| 15 | 5.12, 5.25, 5.26 | §10 (Relationship invariants) | §2.1, §3 rule 8 | Req 8 |
| 16 | 5.25, 5.7 | §8.4 invariants | §2.9 | Req 13 |
| 17 | 5.25, 5.26 | — | §3 rule 9 | AD-WS-9 |
| 18 | 5.23, 5.30 | §19 | §2.5 | Req 14 |
| 19 | 5.4, 5.6, 5.29 | — | §3 cross-context rules | Req 16, AD-WS-1..AD-WS-13 |
| 20 | (verification target across slice) | (invariants cited per property) | — | Req 15, AD-WS-13 |
| 21 | 5.29 | — | — | Req 16 |

## Gaps Flagged for Resolution

The following gaps were identified while reconciling this slice with the upstream documents and Slice 1. They continue the Slice 1 Gap numbering scheme (G-1 through G-5 are recorded in [`../first-walking-slice/requirements.md`](../first-walking-slice/requirements.md) §"Gaps Flagged for Resolution"). They are recorded here so they can be addressed in the design phase rather than rediscovered during implementation.

1. **G-6 — Authority enumeration extension for `review` authority is undecided.** Requirement 11 requires an additive `review` authority type alongside the Slice 1 `{view, modify, approve}` enumeration. No upstream ADR yet enumerates the canonical authority types or governs additive expansion. The slice's design SHALL choose an interim representation (for example, a JSON array column extended with the new value and a registry row covering the new value's semantics) and document the choice as input to a new backlog ADR (placeholder ADR-HT-006).
2. **G-7 — Disclosure policy extension mechanism is undecided.** Requirement 17 requires the `slice-default-2026` policy to be additively extended to cover the new node kinds. No upstream ADR governs whether such extension is represented by mutation of the existing policy row, by an additive coverage row, or by versioned policy revisions. The slice's design SHALL choose an interim representation and document the choice as input to a new backlog ADR (placeholder ADR-HT-009, continuing the Slice 1 series).
3. **G-8 — Plan Review and Plan Approval relationship semantics are partially undecided.** Requirement 8 uses `Relates To` with a `review` semantic role marker for Plan Review → Plan Revision, and Requirement 9 uses `Addresses` for Plan Approval Record → Plan Revision. The domain model permits both but does not prescribe a single canonical convention. The slice's design SHALL choose the canonical Relationship Types and document the choice as input to a new backlog ADR (placeholder ADR-HT-010).
4. **G-9 — Plan Revision lifecycle states beyond `{draft, approved}` are undecided.** Requirement 7 defines `draft` and Requirement 9 introduces `approved`. The user story map's Release 1B requires only these two for the minimum journey, but the constitution acknowledges `superseded`, `withdrawn`, and `archived` lifecycle states. The slice's design SHALL enumerate exactly the lifecycle values supported by Slice 2 and document the choice as input to a new backlog ADR (placeholder ADR-HT-011).
5. **G-10 — Project, Activity Plan, and Plan Revision storage representation is undecided.** Requirement 1 requires distinct Resource and Revision identity columns for every Planning Resource that admits Revisions. The interim representation chosen by Slice 1 (insert-only tables with append-only triggers) is the natural carrier, but the specific tables, columns, and triggers for Planning Resources have not been chosen. The slice's design SHALL choose the persistence representation, ensure it preserves the Slice 1 append-only invariants under AD-WS-4, and document the choice as input to a new backlog ADR (placeholder ADR-HT-012). 

## References

- Constitutional authority: [`00-project-constitution.md`](../../../documents/00-project-constitution.md) (in particular §2, §5.4, §5.5, §5.6, §5.7, §5.8, §5.9, §5.10, §5.12, §5.21, §5.22, §5.23, §5.25, §5.26, §5.29, §5.30; §6.6 Work and Portfolio Planning; §7 Bounded Contexts; §9 Core Domain Dictionary entries for Objective, Decision, Party, Evidence).
- Language and foundational model: [`01-domain-glossary.md`](../../../documents/01-domain-glossary.md) (in particular §§3, 4, 8, 9, 13, 14), [`02-domain-model.md`](../../../documents/02-domain-model.md) (in particular §§3, 4, 5, 7.4, 7.5, 7.6, 8.4, 8.5, 9, 10.5, 10.6, 10.9, 19).
- Bounded contexts and cross-context invariants: [`03-context-map.md`](../../../documents/03-context-map.md) §§2.1, 2.4, 2.5, 2.6, 2.8, 2.9, 3, 4.
- Delivery model and user intent: [`07-user-story-map.md`](../../../documents/07-user-story-map.md) §§2, 3, 4 (Release 1B), 5.3, 5.4, 5.5, 9, 10.
- Slice 1 spec (this slice's prerequisite): [`../first-walking-slice/requirements.md`](../first-walking-slice/requirements.md), [`../first-walking-slice/design.md`](../first-walking-slice/design.md), [`../first-walking-slice/tasks.md`](../first-walking-slice/tasks.md).
- Existing Slice 1 implementation (not modified by this slice except via additive extension): `src/walking_slice/` — `identity.py`, `audit.py`, `authorization.py`, `evidence.py`, `knowledge.py`, `trails.py`, `provenance.py`, `manifests.py`, `disclosure.py`, `interim_adr.py`, `projection.py`, `persistence.py`, `app.py`, `auth_middleware.py`, `clock.py`, `models.py`.
