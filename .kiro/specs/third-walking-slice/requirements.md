# Requirements Document

## Introduction

This document specifies the requirements for the **third walking slice** of the Organizational Knowledge and Work System: the minimum end-to-end software capability that carries an Approved Plan Revision (produced by the [second walking slice](../second-walking-slice/requirements.md)) through to a recorded produced Deliverable, an accepted Milestone, and a recorded Completion of the planned work, without recording any observed Outcome and without retroactively modifying any planning Resource.

The slice realizes the pipeline:

```text
Approved Plan Revision ──Addresses──> Work Assignment Record
                                              ↓ Relates To (assignee)
                                          Contributor (Party)
                                              ↓
                                          Work Event Record  (started, consequential)
                                              ↓
                                          Time Entry Record  (effort)
                                              ↓
                                          Deliverable Production Record ──Produces──> Deliverable Revision
                                                                                       (Document or Artifact)
                                              ↓
                                          Milestone Acceptance Record   (against Deliverable Expectation)
                                              ↓
                                          Completion Record             (against Plan Revision)
```

and must demonstrate six named behaviors end-to-end:

1. **Approved-Plan-to-Completion traceability** — every Completion Record is provably anchored to an Approved Plan Revision produced by the second walking slice, through an unbroken chain Completion Record → Milestone Acceptance Record(s) → Deliverable Production Record(s) → Work Event Record(s) → Work Assignment Record → Approved Plan Revision.
2. **Plan/Execution separation enforced from the execution side** — execution facts (Work Assignment, Work Event, Time Entry, Deliverable Production, Milestone Acceptance, Completion Record) are recorded here, not on planning Resources, and no Slice 3 action mutates, supersedes, or extends any Slice 2 planning Resource or Revision.
3. **Output declared separately from Outcome** — Slice 3 records produced Deliverable Revisions, accepted Milestones, and completion of planned work; Slice 3 SHALL NOT record any Observed Outcome, Measurement Record, Outcome Review, or success-condition assessment.
4. **Distinct Assignment / Contributor / Milestone Acceptance / Completion authorities** — Assignment Authority, Contributor authority, Milestone Acceptance Authority, and Completion Authority are four distinct authority types extending the Slice 1 + Slice 2 enumeration `{view, modify, review, approve}` additively; no two of the new types are substituted for each other and none is substituted for any prior type.
5. **Immutability of recorded execution events** — once any Work Assignment Record, Work Event Record, Time Entry Record, Deliverable Production Record, Milestone Acceptance Record, or Completion Record is finalized, the record and its constituent fields and Relationships are byte-equivalent forever, per Principle 5.6 and per the Immutable Record Model in [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.
6. **Indistinguishable denial for unauthorized execution actions** — every denial path on the new execution and Deliverable Production endpoints conforms to the `slice-default-2026` disclosure policy (AD-WS-9), additively extended to the new node kinds, with no information leakage about the existence, attributes, or counts of restricted execution Resources or produced Deliverable Revisions.

The slice is **executable system behavior**, distinct from documentation work. It is the software realization of *Release 1C — Planned Work to Deliverable* defined in [`documents/07-user-story-map.md`](../../../documents/07-user-story-map.md) §4, constrained by the foundational system model in [`documents/00-project-constitution.md`](../../../documents/00-project-constitution.md) §5.21 (Intent/Work/Output/Outcome are distinct), §5.23 (operational events distinct from current projections), and §5.25 (access is explicit and auditable), by the Work Execution context defined in [`documents/03-context-map.md`](../../../documents/03-context-map.md) §2.6 and by the Document Authoring and Composition context in §2.2 for the produced Document or Artifact Revisions, and by the Immutable Record contracts in [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §§8.2, 8.4, 8.5, 9.1, 10.2, 10.5, 10.9, 10.10.

These requirements reconcile with, and do not duplicate, the upstream authoritative documents, the first walking slice, and the second walking slice. Where an upstream constitutional principle, domain-model contract, context-map ownership, or prior-slice requirement already governs a behavior, the corresponding requirement here carries an explicit **Traceability** block and refines that behavior to the slice's scope. Where no upstream identifier exists, the requirement is flagged as a **Gap** for resolution before implementation. The slice continues the cumulative Gap numbering scheme established by Slices 1 and 2: new gaps are recorded as **G-11** onward. The slice continues the cumulative Requirement numbering scheme established by Slice 2: new requirements are numbered starting at **Requirement 22**.

### Scope of this slice

In scope:

- Recording one `Work Assignment Record` from an Approved Plan Revision (Slice 2 Req 9) to a Contributor Party by an Assignment Authority.
- Recording one `Work Event Record` of kind `started` and one or more consequential `Work Event Record`s against the Work Assignment by the assigned Contributor.
- Recording one `Time Entry Record` against the Work Assignment by the assigned Contributor.
- Recording one produced `Deliverable Revision` (Document Revision or Artifact Revision) in the new `Deliverable_Repository` by the assigned Contributor.
- Recording one `Deliverable Production Record` in the new `Execution_Service` that has a `Produces` Relationship to the produced Deliverable Revision and an `Addresses` Relationship to the target Deliverable Expectation Revision from Slice 2.
- Recording one `Milestone Acceptance Record` by a Party holding Milestone Acceptance Authority against the produced Deliverable Revision and the addressed Deliverable Expectation.
- Recording one `Completion Record` by a Party holding Completion Authority against the Approved Plan Revision, after which the Approved Plan Revision is durably marked as having a recorded completion (without any modification of the Plan Revision row itself).
- Authorization-aware backlinks and forward-provenance navigation across the new execution Resources and produced Deliverable Revisions, joining seamlessly with the Slice 1 + Slice 2 provenance chain back to the originating Decision and exact source Evidence.
- Audit records for every consequential execution write and every denied unauthorized execution attempt, written atomically with the execution write.
- Additive extension of the Slice 1 + Slice 2 `slice-default-2026` disclosure policy to cover the new execution and produced-Deliverable node kinds.
- Additive extension of the Slice 1 + Slice 2 authority enumeration with the four new authority types `assign`, `contribute`, `accept_milestone`, `complete`, recorded as input to new backlog ADRs.
- One explainable Projection of current execution status (for example, `Plan Revision in execution`, `Plan Revision deliverable produced`, `Plan Revision milestone accepted`, `Plan Revision completion recorded`) derived from source Records, distinguished from authoritative Records per Principle 5.23.

Out of scope for this slice (deferred to later slices — see §"Out-of-Scope Boundaries"):

- Slice 1D — Deliverable to Outcome Review (Measurement Definition, Measurement Record, Observed Outcome, Outcome Review, success-condition evaluation, attribution assessment).
- Slice 1E — Learning to Adaptation (Learning Record, Adaptation Decision, Plan supersession beyond approval-immutability).
- Slice 2 — Reproducible Publication of execution artifacts.
- Slice 3 — Investment, cost, capacity, and portfolio reporting against execution records (Budget, Allocation, Capacity Plan, Rate, Estimate, Commitment, Expenditure reference, Forecast Definition, Portfolio Projection).
- Blockage Observations, risk observations, and derived current-risk status (acknowledged as a backbone activity in [`documents/07-user-story-map.md`](../../../documents/07-user-story-map.md) §3 but deferred to a later slice).
- Multiple parallel Work Assignments per Approved Plan Revision; multiple Contributors per Work Assignment; reassignment of a Work Assignment to a different Contributor.
- Decline, withdrawal, or supersession of a Work Assignment Record; replacement of a recorded Work Event, Time Entry, Deliverable Production, Milestone Acceptance, or Completion Record (a later governed supersession path is out of scope).
- Conditional Milestone Acceptance, two-of-N Milestone Acceptance, or delegated Milestone Acceptance; this slice records exactly one Milestone Acceptance Authority per Milestone Acceptance.
- Conditional Completion, partial Completion, or delegated Completion; this slice records exactly one Completion Authority per Completion Record.
- Deliverable Reviewer authority and any Deliverable Review record (deferred); Slice 3 records production and acceptance, not review-of-Deliverable as a distinct authority.
- Portability export of execution Records or produced Deliverable Revisions.
- Automated Agent contribution provenance on execution Records or produced Deliverable Revisions beyond recording that an authoring Party is human.
- Any modification of Slice 1 or Slice 2 contexts (Identity, Audit, Authorization, Evidence, Knowledge, Trails, Provenance, Planning) other than additive extensions defined in this document.

## Glossary

This glossary names the systems, sub-systems, role-bearing Parties, and Resource kinds required by the requirements below. Defined Capitalized Terms not redefined here carry the meaning given in [`documents/01-domain-glossary.md`](../../../documents/01-domain-glossary.md), [`documents/02-domain-model.md`](../../../documents/02-domain-model.md), the [first-walking-slice requirements](../first-walking-slice/requirements.md), and the [second-walking-slice requirements](../second-walking-slice/requirements.md). Sub-system names introduced in Slice 1 or Slice 2 are referenced here without re-definition.

### Sub-systems

- **Walking_Slice_System**: The cumulative software realization of the first, second, and third walking slices. References to "the system" map to this term throughout this document.
- **Execution_Service**: The new Slice 3 sub-system that records Work Assignment Records, Work Event Records, Time Entry Records, Deliverable Production Records, Milestone Acceptance Records, and Completion Records. Owned by the Work Execution bounded context per [`documents/03-context-map.md`](../../../documents/03-context-map.md) §2.6.
- **Deliverable_Repository**: The new Slice 3 sub-system that records produced Document Revisions and produced Artifact Revisions in their Generated Output role per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §9.1. Owned by the Document Authoring and Composition bounded context per [`documents/03-context-map.md`](../../../documents/03-context-map.md) §2.2. The Deliverable_Repository is distinct from the Slice 1 Evidence_Repository: the Evidence_Repository records Source Evidence Document Revisions; the Deliverable_Repository records produced Deliverable Revisions. The two repositories SHALL NOT alias, mutate, or replace each other's Records.
- **Identity_Service**, **Authorization_Service**, **Audit_Log**, **Evidence_Repository**, **Knowledge_Service**, **Trail_Service**, **Provenance_Navigator**, **Planning_Service**, **Disclosure_Policies registry**, **Interim_ADR_Records registry**: As defined in the [first-walking-slice requirements](../first-walking-slice/requirements.md) §"Glossary" and the [second-walking-slice requirements](../second-walking-slice/requirements.md) §"Glossary". This slice reuses these sub-systems and SHALL NOT modify them except through additive extensions described in Requirements 32, 38, and 40.

### Resource kinds and Immutable Records introduced by this slice

- **Work Assignment Record**: An Immutable Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8 that records the assignment of an Approved Plan Revision (Slice 2) to a Contributor Party by an Assignment Authority. The Work Assignment Record is append-only; this slice does not record acceptance, decline, withdrawal, supersession, or reassignment of a Work Assignment.
- **Work Event Record**: An Immutable Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8 that records a consequential event in the execution of a Work Assignment by the assigned Contributor. Supported event kinds in this slice are drawn from the enumerated set `{started, progress_note, paused, resumed, deliverable_drafted}`. Work Event Records are append-only.
- **Time Entry Record**: An Immutable Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8 that records reported effort against a Work Assignment by the assigned Contributor. Time Entry Records are append-only.
- **Deliverable Revision**: A Document Revision or Artifact Revision recorded in the Deliverable_Repository in its Generated Output role per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §9.1. A Deliverable Revision is a Resource Revision per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §4 and is distinguished from a source Evidence Document Revision by the `Produces` Relationship that targets it from a Deliverable Production Record.
- **Deliverable Production Record**: An Execution Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.2 that records the controlled production of a Deliverable Revision by an assigned Contributor against an Approved Plan Revision. The Deliverable Production Record carries a `Produces` Relationship to the produced Deliverable Revision per §10.10 and an `Addresses` Relationship to the target Deliverable Expectation Revision (from Slice 2) per §10.9.
- **Milestone Acceptance Record**: A Governance Decision Immutable Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.5 that records the acceptance or rejection of a produced Deliverable Revision against its addressed Deliverable Expectation by a Party holding effective Milestone Acceptance Authority. Milestone Acceptance Records are immutable per §8.5 invariant 4.
- **Completion Record**: A Governance Decision Immutable Record per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.5 that records the recorded completion of an Approved Plan Revision by a Party holding effective Completion Authority, after one or more accepted Milestones. Completion Records are immutable per §8.5 invariant 4. A Completion Record does not by itself assert that any Intended Outcome has been observed; observed Outcomes belong to Slice 1D and remain out of scope.

### Roles introduced by this slice

The four roles below are **contextual roles** per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §9 and extend the contextual role catalog already established in Slices 1 and 2. A Party holds a role only by virtue of an effective Role Assignment recorded by the Authorization_Service.

- **Assignment Authority**: A Party authorized to record a Work Assignment Record against an Approved Plan Revision within an applicable scope. Granted authorities include the new `assign` authority type defined in Requirement 32. Assignment Authority SHALL NOT be substituted for Contributor, Milestone Acceptance, or Completion authority and vice versa.
- **Contributor**: A Party authorized, as the named assignee of a Work Assignment Record, to record Work Event Records, Time Entry Records, produced Deliverable Revisions, and Deliverable Production Records against that Work Assignment within an applicable scope. Granted authorities include the new `contribute` authority type defined in Requirement 32. Contributor authority SHALL NOT be substituted for Assignment, Milestone Acceptance, or Completion authority and vice versa.
- **Milestone Acceptance Authority**: A Party authorized to record a Milestone Acceptance Record against a produced Deliverable Revision and its addressed Deliverable Expectation within an applicable scope. Granted authorities include the new `accept_milestone` authority type defined in Requirement 32. Milestone Acceptance Authority SHALL NOT be substituted for Assignment, Contributor, or Completion authority and vice versa.
- **Completion Authority**: A Party authorized to record a Completion Record against an Approved Plan Revision within an applicable scope. Granted authorities include the new `complete` authority type defined in Requirement 32. Completion Authority SHALL NOT be substituted for Assignment, Contributor, or Milestone Acceptance authority and vice versa.

### Slice-specific terms

- **Approved Plan Revision**: A Plan Revision for which a Plan Approval Immutable Record has been finalized in Slice 2 per Slice 2 Requirement 9. An Approved Plan Revision is the only valid target of a Work Assignment Record in this slice.
- **Assigned Contributor**: The Party named as the assignee on a Work Assignment Record and holding effective `contribute` authority for the Work Assignment's applicable scope at the recorded time of every subsequent execution write against that Work Assignment.
- **Produced Deliverable Revision**: A Document Revision or Artifact Revision recorded in the Deliverable_Repository that is the target of exactly one `Produces` Relationship sourced from a Deliverable Production Record.
- **Execution Provenance Chain**: The end-to-end traversal Completion Record → Plan Approval Immutable Record → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision Immutable Record → Recommendation Revision → Finding Revision(s) → Content Region Occurrence(s) → Document Revision; with a parallel forward traversal from Completion Record through Milestone Acceptance Record(s) → Deliverable Production Record(s) → produced Deliverable Revision(s), and through Work Assignment Record → Work Event Record(s) → Time Entry Record(s).
- **Slice 2 Approved Plan Revision**: An Approved Plan Revision produced by the second walking slice and conforming to its Requirement 9 (Approve a Plan Revision).


## Requirements

### Requirement 22: Durable Identity Foundation for Execution Resources

**User Story:** As an implementer of the third walking slice, I want every managed identity produced by the Execution_Service and the Deliverable_Repository to conform to the durable identity strategy already enforced by the Identity_Service, so that Work Assignment Records, Work Event Records, Time Entry Records, produced Deliverable Revisions, Deliverable Production Records, Milestone Acceptance Records, and Completion Records remain referenceable across future bounded contexts, exports, and migrations on the same terms as Slice 1 and Slice 2 Resources.

**Traceability:**
- Constitution: Principle 5.5 (Identity is independent of location), Principle 5.6 (Durable states are historical).
- Domain model: §3 (Resource invariants), §4 (Resource Revision invariants), §8 (Immutable Record model), §8.2 (Execution Record).
- Context map: §2.1 (Shared Graph Foundation).
- User story map: §4 Release 1C.
- Slice 1: Requirement 1 (Durable Identity Foundation), AD-WS-2, AD-WS-3.
- Slice 2: Requirement 1 (Durable Identity Foundation for Planning Resources).
- Invariants: identity survives movement; identifier never reused; principal identifier is opaque; produced-Deliverable Resource Identity and Revision Identity are distinct.

#### Acceptance Criteria

1. WHEN the Execution_Service requests the Identity_Service to create a Work Assignment Record, a Work Event Record, a Time Entry Record, a Deliverable Production Record, a Milestone Acceptance Record, or a Completion Record, and WHEN the Deliverable_Repository requests the Identity_Service to create a produced Deliverable Resource or produced Deliverable Revision, THE Identity_Service SHALL assign exactly one UUID version 7 identifier in canonical lowercase hyphenated 8-4-4-4-12 hex form, exactly once, before the entity becomes referenceable, per Slice 1 Requirement 1.1.
2. THE Identity_Service SHALL hold produced Deliverable Resource Identity and produced Deliverable Revision Identity as two distinct values for every produced Deliverable, with cardinality one Resource Identity to one or more Revision Identities, and no Revision Identity shared across Resources, per Slice 1 Requirement 1.2 and Slice 2 Requirement 1.2.
3. WHEN an authorized actor renames or relocates a produced Deliverable Resource within the Deliverable_Repository, THE Identity_Service SHALL preserve the existing Resource Identity and every existing Revision Identity unchanged, generate no new Resource Identity, and replace no existing identity, per Slice 1 Requirement 1.3.
4. IF an identifier generation, import, or reference operation in the Execution_Service or the Deliverable_Repository would assign an existing identifier to different domain content, or would introduce a malformed identifier, THEN THE Identity_Service SHALL reject the operation, return an error indication identifying the conflicting identifier, leave the existing identifier bound to its original content unchanged, and append a Denial Record to the Audit_Log within the same operation, per Slice 1 Requirement 1.4.
5. WHEN the Provenance_Navigator resolves a Relationship from either its source endpoint or its target endpoint between any pair of slice Resources or Records (Slice 1, Slice 2, or Slice 3), THE Identity_Service SHALL return the same single authoritative Relationship Identity from both source-direction and backlink queries, per Slice 1 Requirement 1.5.
6. THE Identity_Service SHALL NOT reassign a once-assigned identifier to different domain content, even after Work Assignment recording, produced Deliverable replacement, Milestone Acceptance recording, Completion Record finalization, retention expiry, or deletion of the original content, per Slice 1 Requirement 1.6.
7. THE Identity_Service SHALL NOT encode mutable name, repository path, organization name, security classification, lifecycle state, authority, semantic version, owning Party, or other business meaning into any issued identifier for any Execution_Service Record or Deliverable_Repository Resource, per Slice 1 Requirement 1.7.
8. THE Identity_Service SHALL hold produced Deliverable Resource Identity, produced Deliverable Revision Identity, Work Assignment Record Identity, Work Event Record Identity, Time Entry Record Identity, Deliverable Production Record Identity, Milestone Acceptance Record Identity, and Completion Record Identity as eight disjoint identifier roles relative to every Slice 1 identifier and every Slice 2 identifier; no Slice 3 identifier SHALL also identify a Slice 1 or Slice 2 entity, and no Slice 1 or Slice 2 identifier SHALL be reissued as a Slice 3 identifier.

### Requirement 23: Record a Work Assignment Against an Approved Plan Revision

**User Story:** As an Assignment Authority, I want to record a Work Assignment from an Approved Plan Revision to a Contributor, so that planned work has an explicit named executor and remains navigable back to the approval that authorized it.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct), Principle 5.25 (Access Is Explicit and Auditable).
- Domain model: §8 (Immutable Record model), §10.9 (Addresses Relationship), §10.5 (Relates To).
- Context map: §2.5 (Work Planning) ↔ §2.6 (Work Execution) — Work Execution consumes plans from Work Planning.
- User story map: §4 Release 1C step 1.
- Slice 2: Requirement 9 (Approve a Plan Revision) — the consumed authority.
- Invariants: Work Assignment Record addresses an exact Approved Plan Revision; Work Assignment Record is an Immutable Record; the assignee Party is named explicitly; the Assignment Authority Party is named explicitly.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Assignment Authority for the applicable scope submits a Work Assignment creation request that names exactly one target Approved Plan Revision Identity and exactly one assignee Party Identity, THE Execution_Service SHALL create an immutable Work Assignment Record within a nominal 5 seconds.
2. THE Execution_Service SHALL require, for every Work Assignment creation, that the named target Plan Revision Identity resolves to a Plan Revision in the Planning_Service whose lifecycle state at the recorded time is `approved` per Slice 2 Requirement 9.1.
3. WHEN the Execution_Service creates a Work Assignment Record, THE Execution_Service SHALL record on the Work Assignment Record the target Approved Plan Revision Identity, the assignee Party Identity, the Assignment Authority Party Identity, the assignment-rationale text of 0 to 4,000 characters, the authority basis drawn from the set defined in Slice 1 AD-WS-10, the applicable scope, the recorded time in UTC with millisecond precision, an `Addresses` Relationship from the Work Assignment Record to the target Approved Plan Revision Identity per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.9, and a `Relates To` Relationship with the semantic role marker `assignee` from the Work Assignment Record to the assignee Party Identity per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.5.
4. IF the named target Plan Revision Identity does not resolve to any Plan Revision, or resolves to a Plan Revision whose lifecycle state is not `approved`, or resolves to a Plan Revision whose applicable scope is not within the requesting Party's effective Assignment Authority scope, THEN THE Execution_Service SHALL reject the Work Assignment creation, decline to create any Work Assignment Record, and return an error indication identifying the unresolvable-or-non-approved Plan Revision target.
5. IF the named assignee Party Identity does not resolve to an existing Party, names a Party whose recorded status is inactive at the recorded time, or names the requesting Party itself (self-assignment is out of scope for this slice), THEN THE Execution_Service SHALL reject the Work Assignment creation, decline to create any Work Assignment Record, and return an error indication identifying the invalid assignee.
6. IF the requesting Party is unauthenticated or does not hold effective Assignment Authority for the applicable scope at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Execution_Service SHALL decline to create any Work Assignment Record, and the Audit_Log SHALL append a Denial Record conforming to the Slice 1 AD-WS-9 disclosure policy as extended by Requirement 38.
7. IF a Work Assignment creation request omits the target Plan Revision Identity, omits the assignee Party Identity, omits the applicable scope, omits the authority basis, or names more than one target Plan Revision Identity or more than one assignee Party Identity, THEN THE Execution_Service SHALL reject the action, decline to create any Work Assignment Record, and return an error indication identifying the missing or invalid attribute.
8. WHEN a Work Assignment Record is finalized, THE Audit_Log SHALL append an immutable creation record identifying the Work Assignment Record Identity, target Approved Plan Revision Identity, assignee Party Identity, Assignment Authority Party Identity, authority basis, and recorded time within 1 second of and in the same transaction as the Work Assignment Record creation, per Slice 1 Requirement 13.1.
9. IF an actor attempts to modify or delete a previously created Work Assignment Record, THEN THE Execution_Service SHALL reject the operation, leave the Work Assignment Record byte-equivalent to its prior state, return an error indication identifying the immutability violation, and append a Denial Record to the Audit_Log conforming to Slice 1 Requirement 13.5.

### Requirement 24: Record a Work Event Against a Work Assignment

**User Story:** As an Assigned Contributor, I want to record consequential events against my Work Assignment (for example, started, progress notes, paused, resumed, deliverable drafted), so that performed work is preserved durably and independently of any plan replanning.

**Traceability:**
- Constitution: Principle 5.21, Principle 5.23 (Operational Events and Current Projections Are Distinct), Principle 5.7 (No Acknowledged Work Is Silently Lost).
- Domain model: §8 (Immutable Record model), §8.4 (Audit Event analog), §10.5 (Relates To).
- Context map: §2.6 (Work Execution).
- User story map: §4 Release 1C steps 2 and 3.
- Invariants: Work Event Records are append-only; recorded events are not silently lost; events do not mutate the source Work Assignment Record or any planning Resource.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Contributor authority for the applicable scope, and named as the assignee on a Work Assignment Record, submits a Work Event creation request that names exactly one target Work Assignment Record Identity, THE Execution_Service SHALL create an immutable Work Event Record within a nominal 5 seconds.
2. THE Execution_Service SHALL record on every Work Event Record the target Work Assignment Record Identity, the event kind drawn from the enumerated set `{started, progress_note, paused, resumed, deliverable_drafted}`, the event note text of 0 to 4,000 characters, the recording Contributor Party Identity, the authority basis drawn from the set defined in Slice 1 AD-WS-10 and extended per Requirement 32 if necessary, the applicable scope, the recorded time in UTC with millisecond precision, and a `Relates To` Relationship with the semantic role marker `work_event` from the Work Event Record to the target Work Assignment Record Identity.
3. THE Execution_Service SHALL permit at most one Work Event Record per Work Assignment Record whose event kind is `started`, SHALL require that a `started` Work Event Record exists on the same Work Assignment Record before any Work Event Record of kind `progress_note`, `paused`, `resumed`, or `deliverable_drafted` is recorded, and SHALL require that a `paused` Work Event Record exists on the same Work Assignment Record before any later `resumed` Work Event Record is recorded.
4. IF a Work Event creation request names a target Work Assignment Record Identity that does not resolve, names a target Work Assignment Record on which the requesting Party is not the named assignee, supplies an event kind outside the enumerated set, attempts to record a second `started` event on the same Work Assignment Record, attempts to record a non-`started` event on a Work Assignment Record that has no prior `started` event, attempts to record a `resumed` event without a prior `paused` event on the same Work Assignment Record, or omits the applicable scope, THEN THE Execution_Service SHALL reject the action, decline to create any Work Event Record, and return an error indication identifying each invalid attribute.
5. IF the requesting Party is unauthenticated, does not hold effective Contributor authority for the applicable scope at the recorded time, or is not the named assignee on the target Work Assignment Record at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Execution_Service SHALL decline to create any Work Event Record, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9 as extended by Requirement 38.
6. WHEN a Work Event Record is finalized, THE Audit_Log SHALL append an immutable creation record identifying the Work Event Record Identity, target Work Assignment Record Identity, event kind, recording Contributor Party Identity, and recorded time within 1 second of and in the same transaction as the Work Event Record creation.
7. IF an actor attempts to modify or delete a previously created Work Event Record, THEN THE Execution_Service SHALL reject the operation, leave the Work Event Record byte-equivalent to its prior state, return an error indication identifying the immutability violation, and append a Denial Record to the Audit_Log conforming to Slice 1 Requirement 13.5.


### Requirement 25: Record a Time Entry Against a Work Assignment

**User Story:** As an Assigned Contributor, I want to record reported effort against my Work Assignment as a discrete Time Entry, so that effort is preserved as an actual execution fact separately from any planned-effort value on the Plan Revision.

**Traceability:**
- Constitution: Principle 5.21, Principle 5.23.
- Domain model: §8 (Immutable Record model).
- Context map: §2.6 (Work Execution).
- User story map: §4 Release 1C step 3 and §5.4 (Plan and Execution Separation must-have story group).
- Invariants: Time Entry Records are append-only; reported effort is an execution fact, not a planning fact; reported effort does not appear on any planning Resource.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Contributor authority for the applicable scope, and named as the assignee on a Work Assignment Record, submits a Time Entry creation request that names exactly one target Work Assignment Record Identity, THE Execution_Service SHALL create an immutable Time Entry Record within a nominal 5 seconds.
2. THE Execution_Service SHALL record on every Time Entry Record the target Work Assignment Record Identity, the reported effort quantity expressed as a non-negative decimal number of hours with at most two fractional digits and not exceeding 24.00 hours per single Time Entry Record, the effort-period start time and effort-period end time each in UTC with millisecond precision and with the start time not after the end time and with the end time not after the recorded time, the recording Contributor Party Identity, the authority basis, the applicable scope, the recorded time in UTC with millisecond precision, and a `Relates To` Relationship with the semantic role marker `time_entry` from the Time Entry Record to the target Work Assignment Record Identity.
3. IF a Time Entry creation request names a target Work Assignment Record Identity that does not resolve, names a target Work Assignment Record on which the requesting Party is not the named assignee, supplies an effort quantity that is negative or exceeds 24.00 hours, supplies an effort-period start time later than the end time or an end time later than the recorded time, omits the effort quantity, omits the effort-period, or omits the applicable scope, THEN THE Execution_Service SHALL reject the action, decline to create any Time Entry Record, and return an error indication identifying each invalid attribute.
4. IF the requesting Party is unauthenticated, does not hold effective Contributor authority for the applicable scope at the recorded time, or is not the named assignee on the target Work Assignment Record at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Execution_Service SHALL decline to create any Time Entry Record, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9 as extended by Requirement 38.
5. WHEN a Time Entry Record is finalized, THE Audit_Log SHALL append an immutable creation record identifying the Time Entry Record Identity, target Work Assignment Record Identity, reported effort quantity, recording Contributor Party Identity, and recorded time within 1 second of and in the same transaction as the Time Entry Record creation.
6. IF an actor attempts to modify or delete a previously created Time Entry Record, THEN THE Execution_Service SHALL reject the operation, leave the Time Entry Record byte-equivalent to its prior state, return an error indication identifying the immutability violation, and append a Denial Record to the Audit_Log conforming to Slice 1 Requirement 13.5.
7. IF the Audit_Log append for a Time Entry Record creation fails, THEN THE Execution_Service SHALL roll back the Time Entry Record creation, decline to make the Time Entry Record referenceable, and return an error indication identifying the audit append failure, per Slice 1 Requirement 13.6.

### Requirement 26: Record a Produced Deliverable Revision

**User Story:** As an Assigned Contributor, I want to record a produced Document Revision or Artifact Revision in the Deliverable_Repository, so that the actual output of my work has a durable identity, content digest, and authoring provenance distinct from any source Evidence Document Revision.

**Traceability:**
- Constitution: Principle 5.6 (Durable states are historical), Principle 5.9 (Provenance Is Preserved End to End), Principle 5.21.
- Domain model: §3 (Resource invariants), §4 (Resource Revision invariants), §7.1 (Document contract), §7.2 (Artifact contract), §9.1 (Generated Output Role), §10.10 (Produces), §10.2 (Derived From).
- Context map: §2.2 (Document Authoring and Composition).
- User story map: §3 (Produce a Deliverable) and §4 Release 1C step 4.
- Invariants: produced Deliverable Resource Identity is independent of name and location; produced Deliverable Revisions are immutable; the produced Deliverable Revision is distinguished from a source Evidence Document Revision only by the `Produces` Relationship that targets it from a Deliverable Production Record.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Contributor authority for the applicable scope, and named as the assignee on a Work Assignment Record whose target Approved Plan Revision is in scope, submits a produced Deliverable creation request containing produced content of between 1 byte and 100 megabytes, a content type drawn from the enumerated set `{text/markdown, text/plain, application/pdf, application/json, image/png, image/svg+xml, application/octet-stream}`, and a non-empty produced Deliverable name of 1 to 200 characters, THE Deliverable_Repository SHALL create a produced Deliverable Resource and an associated immutable produced Deliverable Revision within a nominal 5 seconds.
2. WHEN the Deliverable_Repository creates a produced Deliverable Revision, THE Deliverable_Repository SHALL record on the produced Deliverable Revision the produced Deliverable Resource Identity, the produced Deliverable Revision Identity, the content type, a content digest computed over the full byte content of the produced Revision (per Slice 1 Requirement 2.2), the authoring Contributor Party Identity, the recorded time in UTC with millisecond precision, the produced-Deliverable role marker `generated_output` per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §9.1, and the originating Work Assignment Record Identity.
3. THE Deliverable_Repository SHALL hold produced Deliverable Resource Identity and Source Evidence Document Resource Identity as two disjoint identifier roles; a produced Deliverable Resource Identity SHALL NOT also identify a Source Evidence Document Resource recorded by the Slice 1 Evidence_Repository, and a Slice 1 Evidence_Repository Source Evidence Document Resource Identity SHALL NOT be reissued as a produced Deliverable Resource Identity.
4. IF an actor attempts to modify the content digest, recorded time, authoring Contributor Party Identity, originating Work Assignment Record Identity, or produced-Deliverable role marker of an existing produced Deliverable Revision, THEN THE Deliverable_Repository SHALL reject the modification, return an error indication, leave the existing produced Deliverable Revision byte-equivalent to its prior state, and require creation of a new produced Deliverable Revision per Slice 1 Requirement 2.4.
5. IF a produced Deliverable creation request submits content of zero bytes, content exceeding 100 megabytes, a content type outside the enumerated set, an omitted produced Deliverable name, or names an originating Work Assignment Record Identity that does not resolve or on which the requesting Party is not the named assignee, THEN THE Deliverable_Repository SHALL reject the submission, decline to create any produced Deliverable Resource or Revision, and return an error indication identifying the failing validation.
6. IF the requesting Party is unauthenticated, does not hold effective Contributor authority for the applicable scope at the recorded time, or is not the named assignee on the originating Work Assignment Record at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Deliverable_Repository SHALL decline to create any produced Deliverable Resource or Revision, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9 as extended by Requirement 38.
7. WHEN a produced Deliverable Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the produced Deliverable Resource Identity, produced Deliverable Revision Identity, originating Work Assignment Record Identity, authoring Contributor Party Identity, content digest, and recorded time within 1 second of and in the same transaction as the produced Deliverable Revision creation, per Slice 1 Requirement 13.1.
8. IF the Audit_Log append for a produced Deliverable Revision creation fails, THEN THE Deliverable_Repository SHALL roll back the produced Deliverable Revision creation, decline to make the produced Deliverable Revision referenceable, and return an error indication identifying the audit append failure, per Slice 1 Requirement 13.6.

### Requirement 27: Record a Deliverable Production Record

**User Story:** As an Assigned Contributor, I want to record a Deliverable Production Record that links my produced Deliverable Revision to the addressed Deliverable Expectation from Slice 2, so that the produced output is durably traceable to the planned expectation it was produced against.

**Traceability:**
- Constitution: Principle 5.9 (Provenance Is Preserved End to End), Principle 5.21.
- Domain model: §8.2 (Execution Record), §9.1 (Generated Output Role), §10.10 (Produces), §10.9 (Addresses), §10.2 (Derived From).
- Context map: §2.6 (Work Execution).
- User story map: §3 (Produce a Deliverable) and §4 Release 1C step 5.
- Slice 2: Requirement 5 (Declare an Expected Deliverable for a Project).
- Invariants: a Deliverable Production Record is an Execution Record per §8.2; it carries `Produces` to the produced Deliverable Revision and `Addresses` to the target Deliverable Expectation Revision; it is immutable once finalized.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Contributor authority for the applicable scope, and named as the assignee on a Work Assignment Record, submits a Deliverable Production Record creation request that names exactly one source Work Assignment Record Identity, exactly one produced Deliverable Revision Identity, and exactly one target Deliverable Expectation Revision Identity, THE Execution_Service SHALL create an immutable Deliverable Production Record within a nominal 5 seconds.
2. THE Execution_Service SHALL record on every Deliverable Production Record the source Work Assignment Record Identity, the produced Deliverable Resource Identity, the produced Deliverable Revision Identity, the target Deliverable Expectation Resource Identity, the target Deliverable Expectation Revision Identity, the production-rationale text of 0 to 4,000 characters, the recording Contributor Party Identity, the applicable scope, the recorded time in UTC with millisecond precision, exactly one `Produces` Relationship from the Deliverable Production Record to the produced Deliverable Revision Identity per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.10, exactly one `Addresses` Relationship from the Deliverable Production Record to the target Deliverable Expectation Revision Identity per §10.9, and exactly one `Relates To` Relationship with the semantic role marker `production_source` from the Deliverable Production Record to the source Work Assignment Record Identity per §10.5.
3. THE Execution_Service SHALL require that the target Deliverable Expectation Revision Identity is associated by `Addresses` Relationship in the Planning_Service to a Project Resource whose Project Identity matches the Project Identity associated with the Approved Plan Revision named on the source Work Assignment Record, so that the produced Deliverable's Deliverable Expectation belongs to the same Project as the work that produced it.
4. IF a Deliverable Production Record creation request names a source Work Assignment Record Identity that does not resolve, names a Work Assignment Record on which the requesting Party is not the named assignee, names a produced Deliverable Revision Identity that does not resolve in the Deliverable_Repository, names a produced Deliverable Revision whose originating Work Assignment Record Identity does not match the named source Work Assignment Record Identity, names a target Deliverable Expectation Revision Identity that does not resolve, names a target Deliverable Expectation Revision whose associated Project is not the Project of the source Work Assignment's Approved Plan Revision, or omits the applicable scope, THEN THE Execution_Service SHALL reject the action, decline to create any Deliverable Production Record, and return an error indication identifying each invalid attribute.
5. IF the requesting Party is unauthenticated, does not hold effective Contributor authority for the applicable scope at the recorded time, or is not the named assignee on the source Work Assignment Record at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Execution_Service SHALL decline to create any Deliverable Production Record, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9 as extended by Requirement 38.
6. WHEN a Deliverable Production Record is finalized, THE Audit_Log SHALL append an immutable creation record identifying the Deliverable Production Record Identity, source Work Assignment Record Identity, produced Deliverable Revision Identity, target Deliverable Expectation Revision Identity, recording Contributor Party Identity, and recorded time within 1 second of and in the same transaction as the Deliverable Production Record creation.
7. IF an actor attempts to modify or delete a previously created Deliverable Production Record, or to modify or delete any of its `Produces`, `Addresses`, or `Relates To` Relationships, THEN THE Execution_Service SHALL reject the operation, leave the Deliverable Production Record and its Relationships byte-equivalent to their prior state, return an error indication identifying the immutability violation, and append a Denial Record to the Audit_Log conforming to Slice 1 Requirement 13.5.

### Requirement 28: Record a Milestone Acceptance

**User Story:** As a Milestone Acceptance Authority, I want to record a Milestone Acceptance against a produced Deliverable Revision and the addressed Deliverable Expectation, so that the organization's acceptance of the produced output is preserved as an Immutable Governance Decision.

**Traceability:**
- Constitution: Principle 5.21, Principle 5.25 (Access Is Explicit and Auditable).
- Domain model: §8.5 (Governance Decision Immutable Record), §10.9 (Addresses).
- Context map: §2.6 (Work Execution); §2.9 (Identity, Access, and Governance) supplies Milestone Acceptance authority.
- User story map: §4 Release 1C step 6.
- Invariants: Milestone Acceptance Records are immutable; exactly one Milestone Acceptance Record per (produced Deliverable Revision, target Deliverable Expectation Revision) pair; the accepting Party held effective `accept_milestone` authority at the recorded time.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Milestone Acceptance Authority for the applicable scope submits a Milestone Acceptance request that names exactly one source Deliverable Production Record Identity, THE Execution_Service SHALL create an immutable Milestone Acceptance Record within a nominal 5 seconds and SHALL resolve the produced Deliverable Revision Identity and target Deliverable Expectation Revision Identity from the source Deliverable Production Record's `Produces` and `Addresses` Relationships.
2. THE Milestone Acceptance Record SHALL identify the source Deliverable Production Record Identity, the produced Deliverable Resource Identity, the produced Deliverable Revision Identity, the target Deliverable Expectation Resource Identity, the target Deliverable Expectation Revision Identity, the milestone-acceptance outcome drawn from the enumerated set `{Accept, Reject}`, the acceptance rationale text of 1 to 4,000 characters, the accepting Party Identity, the authority basis drawn from the set defined in Slice 1 AD-WS-10, the applicable scope, the recorded time in UTC with millisecond precision, and exactly one `Addresses` Relationship from the Milestone Acceptance Record to the produced Deliverable Revision Identity per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.9.
3. THE Execution_Service SHALL permit at most one Milestone Acceptance Record per source Deliverable Production Record Identity; a second Milestone Acceptance attempt against the same Deliverable Production Record SHALL be rejected with no Milestone Acceptance Record persisted, per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.5 invariant 4.
4. IF a Milestone Acceptance request names a source Deliverable Production Record Identity that does not resolve, names a source Deliverable Production Record that is already the source of any finalized Milestone Acceptance Record, supplies a milestone-acceptance outcome outside the enumerated set, omits the acceptance rationale, omits the authority basis, or omits the applicable scope, THEN THE Execution_Service SHALL reject the action, decline to create any Milestone Acceptance Record, and return an error indication identifying each invalid attribute.
5. IF the requesting Party is unauthenticated or does not hold effective Milestone Acceptance Authority for the applicable scope at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Execution_Service SHALL decline to create any Milestone Acceptance Record, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9 as extended by Requirement 38.
6. WHEN a Milestone Acceptance Record is finalized, THE Audit_Log SHALL append an immutable creation record identifying the Milestone Acceptance Record Identity, source Deliverable Production Record Identity, produced Deliverable Revision Identity, target Deliverable Expectation Revision Identity, accepting Party Identity, authority basis, milestone-acceptance outcome, and recorded time within 1 second of and in the same transaction as the Milestone Acceptance Record creation.
7. IF an actor attempts to modify or delete a previously created Milestone Acceptance Record, or to modify or delete its `Addresses` Relationship, THEN THE Execution_Service SHALL reject the operation, leave the Milestone Acceptance Record byte-equivalent to its prior state, return an error indication identifying the immutability violation, and append a Denial Record to the Audit_Log conforming to Slice 1 Requirement 13.5.
8. THE Execution_Service SHALL NOT modify, supersede, withdraw, or extend the source Deliverable Production Record, the produced Deliverable Revision, the target Deliverable Expectation Revision, the addressed Project Revision, the addressed Plan Revision, or any Slice 1 or Slice 2 Resource or Record as a consequence of recording a Milestone Acceptance Record.

### Requirement 29: Record a Completion Record Against an Approved Plan Revision

**User Story:** As a Completion Authority, I want to record a Completion Record against an Approved Plan Revision after its Deliverable Expectations have been satisfied by accepted Milestones, so that the recorded completion of planned work is preserved as an Immutable Governance Decision distinct from any observed Outcome.

**Traceability:**
- Constitution: Principle 5.6 (Durable states are historical), Principle 5.21 (Output is not Outcome), Principle 5.25.
- Domain model: §8.5 (Governance Decision Immutable Record), §10.9 (Addresses).
- Context map: §2.6 (Work Execution); §2.9 (Identity, Access, and Governance) supplies Completion authority.
- User story map: §4 Release 1C step 6 and step 7.
- Slice 2: Requirement 9 (Approve a Plan Revision).
- Invariants: a Completion Record is an Immutable Governance Decision; exactly one Completion Record per Approved Plan Revision; the completing Party held effective `complete` authority at the recorded time; recording completion does not assert any observed Outcome; recording completion does not mutate any planning Resource.

#### Acceptance Criteria

1. WHEN an authenticated Party holding effective Completion Authority for the applicable scope submits a Completion Record creation request that names exactly one target Approved Plan Revision Identity, and WHEN at least one Milestone Acceptance Record whose outcome is `Accept` exists against a Deliverable Production Record whose source Work Assignment Record's target Plan Revision Identity equals the named target, THE Execution_Service SHALL create an immutable Completion Record within a nominal 5 seconds.
2. THE Completion Record SHALL identify the target Approved Plan Revision Identity, the target Activity Plan Resource Identity, the target Project Resource Identity, the completion outcome drawn from the enumerated set `{Completed, Completed_With_Reservation}`, the completion rationale text of 1 to 4,000 characters, the optional list of source Milestone Acceptance Record Identities (each of which is an `Accept` outcome and each of which resolves), the completing Party Identity, the authority basis drawn from the set defined in Slice 1 AD-WS-10, the applicable scope, the recorded time in UTC with millisecond precision, and exactly one `Addresses` Relationship from the Completion Record to the target Approved Plan Revision Identity per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §10.9.
3. THE Execution_Service SHALL permit at most one Completion Record per target Approved Plan Revision Identity; a second Completion Record attempt against the same Plan Revision SHALL be rejected with no Completion Record persisted, per [`documents/02-domain-model.md`](../../../documents/02-domain-model.md) §8.5 invariant 4.
4. IF a Completion Record creation request names a target Plan Revision Identity that does not resolve, names a Plan Revision whose lifecycle state is not `approved`, names a target Plan Revision that is already the target of any finalized Completion Record, supplies a completion outcome outside the enumerated set, omits the completion rationale, omits the authority basis, omits the applicable scope, or supplies zero source Milestone Acceptance Record Identities and zero accepted Milestone Acceptance Records exist for the target Plan Revision at the recorded time, THEN THE Execution_Service SHALL reject the action, decline to create any Completion Record, and return an error indication identifying each invalid attribute.
5. IF the requesting Party is unauthenticated or does not hold effective Completion Authority for the applicable scope at the recorded time, THEN THE Authorization_Service SHALL reject the action, the Execution_Service SHALL decline to create any Completion Record, and the Audit_Log SHALL append a Denial Record conforming to AD-WS-9 as extended by Requirement 38.
6. WHEN a Completion Record is finalized, THE Audit_Log SHALL append an immutable creation record identifying the Completion Record Identity, target Approved Plan Revision Identity, completion outcome, completing Party Identity, authority basis, source Milestone Acceptance Record Identities, and recorded time within 1 second of and in the same transaction as the Completion Record creation.
7. THE Execution_Service SHALL NOT mutate any field, Revision, or Relationship of the target Plan Revision, target Activity Plan Resource, target Project Resource, target Objective Resource, target Intended Outcome Resource, target Deliverable Expectation Resource, Plan Approval Immutable Record, or any Slice 1 Resource or Record as a consequence of recording a Completion Record; the Approved Plan Revision and every associated Slice 2 Resource SHALL remain byte-equivalent to their state immediately before the Completion Record creation, per Slice 2 Requirement 9.4 and Requirement 40 of this slice.
8. THE Completion Record SHALL NOT assert, imply, or alias any observed Outcome, Measurement Record, success-condition assessment, or attribution-evidence reference; recording a Completion Record SHALL NOT by itself satisfy any Intended Outcome declared in Slice 2 per Principle 5.21.

### Requirement 30: Deny Unauthorized Execution Actions (Demonstration: indistinguishable denial)

**User Story:** As a Security Auditor, I want any attempt to record a Work Assignment, Work Event, Time Entry, produced Deliverable Revision, Deliverable Production, Milestone Acceptance, or Completion Record by a Party lacking effective authority to be rejected, audited, and indistinguishable from a non-existent endpoint, so that execution authority cannot be silently bypassed and existence of restricted execution Records cannot be inferred from denial behavior.

**Traceability:**
- Constitution: Principle 5.25, Principle 5.26 (Sensitive Information Is Governed).
- Domain model: §8.4 (Audit Event), §8.5 (Governance Decision).
- Context map: §2.9 (Identity, Access, and Governance).
- Slice 1: Requirement 7 (Deny Unauthorized Decisions), AD-WS-9 (`slice-default-2026` default Completeness Disclosure policy).
- Slice 2: Requirement 10 (Deny Unauthorized Plan Approval).
- Invariants: privileged actions are restricted and auditable; sensitive information does not leak through denial.

#### Acceptance Criteria

1. IF a Party attempts to finalize a Work Assignment Record, Work Event Record, Time Entry Record, produced Deliverable Revision, Deliverable Production Record, Milestone Acceptance Record, or Completion Record while lacking the specific effective authority required by Requirements 23 through 29 for the applicable scope, THEN THE Authorization_Service SHALL reject the action within 2 seconds and THE Execution_Service or Deliverable_Repository SHALL ensure no execution Record, Resource, or Revision is created and no in-flight row is persisted.
2. WHEN the Authorization_Service rejects an execution attempt, THE Audit_Log SHALL append exactly one immutable Denial Record within 1 second containing actor Party Identity, attempted action, target Resource or Record Identity, target Revision Identity when applicable, recorded time in UTC with millisecond precision, and denial reason code drawn from the enumerated set defined in Slice 1 Requirement 7.2.
3. WHEN evaluating any execution authority (`assign`, `contribute`, `accept_milestone`, `complete`) for any execution attempt, THE Authorization_Service SHALL treat a role assignment as not in effect if its effective-start time is in the future, its expiration time has passed, its revocation has been recorded, or its scope does not cover the target Resource's or Record's applicable scope, per Slice 1 Requirement 7.3.
4. WHEN the Authorization_Service rejects an execution attempt because of missing authority, THE Authorization_Service SHALL return a denial response containing only a generic denial indicator, the denial reason code, and a correlation identifier, and SHALL NOT contain Assignment Authority, Contributor, Milestone Acceptance Authority, or Completion Authority Party identities, Work Assignment Record contents, Time Entry quantities, produced Deliverable Revision content or content digests, Milestone Acceptance rationale, Completion rationale, role assignment details, or any target existence information beyond the requesting Party's view authority, per the Slice 1 `slice-default-2026` policy.
5. THE Execution_Service and the Deliverable_Repository SHALL leave the targeted Work Assignment Record, every constituent Relationship, every Work Event Record, Time Entry Record, Deliverable Production Record, Milestone Acceptance Record, and Completion Record sourced from or targeting that Work Assignment, every produced Deliverable Resource and Revision, and every related Slice 1 and Slice 2 Resource or Record byte-equivalent to their state immediately before the denied execution attempt.
6. IF the Audit_Log append for a denied execution attempt fails, THEN THE Authorization_Service SHALL retry up to 3 times, keep the action denied, and surface an audit-failure indicator to the operator so that denial and audit cannot silently diverge, per Slice 1 Requirement 7.6.
7. THE Execution_Service and the Deliverable_Repository SHALL produce denial responses for Work Assignment, Work Event, Time Entry, produced Deliverable, Deliverable Production, Milestone Acceptance, and Completion Record creation that are indistinguishable in counts, identifier sets, response size, error category, error wording, and latency (within 100 milliseconds variation) from responses produced when the target Resource or Record does not exist, when the requesting Party lacks view authority on the target Resource or Record, or when the action is denied for missing authority, per the Slice 1 `slice-default-2026` policy.


### Requirement 31: Approved-Plan-to-Completion Traceability (Slice Invariant)

**User Story:** As a Pilot Reviewer, I want every Completion Record to be provably anchored to an Approved Plan Revision through an unbroken chain of execution Records, so that recorded completion of planned work is never floating and is always traceable back to the authorization that permitted the work.

**Traceability:**
- Constitution: Principle 5.9 (Provenance Is Preserved End to End), Principle 5.21, Principle 5.22 (Organizational Learning Is a Closed Loop).
- Domain model: §10.9 (Addresses), §10.10 (Produces), §19 (Provenance graph).
- Context map: §2.5 ↔ §2.6.
- User story map: §4 Release 1C (full pipeline) and §5.4 (Plan and Execution Separation must-have story group).
- Slice 2: Requirement 9 (Approve a Plan Revision), Requirement 14 (Provenance chain back to originating Decision).
- Invariants: every Completion Record traces to an Approved Plan Revision via at least one chain of Records; no orphan Completion Records; the chain is the same in both directions.

#### Acceptance Criteria

1. FOR every Completion Record finalized by the Execution_Service, the Walking_Slice_System SHALL satisfy: the Completion Record's `Addresses` target resolves to a Plan Revision whose lifecycle state at Completion Record recording time is `approved`; at least one Milestone Acceptance Record whose outcome is `Accept` exists whose `Addresses` target resolves to a produced Deliverable Revision whose originating Work Assignment Record's target Plan Revision Identity equals the Completion Record's target Plan Revision Identity; and at least one Work Assignment Record exists whose `Addresses` target equals the Completion Record's target Plan Revision Identity.
2. WHEN an authorized Party requests the execution chain for a Completion Record Identity, THE Provenance_Navigator SHALL return an ordered traversal Completion Record → Milestone Acceptance Record(s) → Deliverable Production Record(s) → produced Deliverable Revision(s); Completion Record → Plan Approval Immutable Record → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision; and Completion Record → Work Assignment Record → Work Event Record(s) → Time Entry Record(s), identifying each node by its Identity and (where applicable) Revision Identity, within 5 seconds for chains of up to 100 nodes.
3. IF a required upstream link in any of the three traversals is unresolved, restricted, stale, or unavailable, THEN THE Provenance_Navigator SHALL identify the gap explicitly with a gap descriptor identifying the stage in the chain, the gap category drawn from `{unavailable, restricted, stale, unresolved}`, and the Identity of the next reachable node where applicable, per Slice 1 Requirement 11.4 and Slice 2 Requirement 14.4.
4. THE Provenance_Navigator SHALL produce the same execution chain for the same Completion Record Identity, requesting Party authority set, and effective time inputs (idempotent retrieval), within 5 seconds for chains of up to 100 nodes.
5. IF the requested Completion Record Identity does not resolve to a Completion Record, THEN THE Provenance_Navigator SHALL return an error indication identifying the unresolvable Completion Record reference and SHALL NOT disclose existence of any related execution Records or planning Resources.
6. IF the requesting Party is unauthenticated or lacks any view authority on the Completion Record itself, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from one for a non-existent Completion Record, conforming to the Slice 1 `slice-default-2026` policy as extended by Requirement 38.

### Requirement 32: Distinct Assignment, Contributor, Milestone Acceptance, and Completion Authority Types

**User Story:** As a Resource Steward, I want Assignment Authority, Contributor authority, Milestone Acceptance Authority, and Completion Authority to be modelled as four distinct authority types that are never substituted for each other and never substituted for any Slice 1 or Slice 2 authority, so that "who can assign", "who can contribute", "who can accept a milestone", and "who can complete" remain separately granted, separately revoked, and separately auditable.

**Traceability:**
- Constitution: Principle 5.25 (Access Is Explicit and Auditable).
- Context map: §2.9 (Identity, Access, and Governance) owns Authorization Policy and Role Assignment.
- Slice 1: Requirement 12 (Contextual Role Assignment and Enforcement) — view, modify, approve are distinct authority types; non-substitution rule.
- Slice 2: Requirement 11 (Distinct Plan Reviewer and Plan Approver Authority Types) — additive `review` authority.
- Gap: G-11 (additive extension of the authority enumeration).
- Invariants: assign authority ≠ contribute authority ≠ accept_milestone authority ≠ complete authority; non-substitution rule applies in every direction; no prior authority is substituted for any new authority and vice versa.

#### Acceptance Criteria

1. THE Authorization_Service SHALL accept and persist the values `assign`, `contribute`, `accept_milestone`, and `complete` in the `authorities_granted` set of a Role Assignment, alongside the prior values `view`, `modify`, `review`, and `approve`, expanding the canonical authority enumeration additively per Gap G-11.
2. WHEN a Resource Steward assigns an Assignment Authority role to a Party, THE Authorization_Service SHALL accept a Role Assignment whose `authorities_granted` set contains `assign` and SHALL NOT require, infer, or auto-include `contribute`, `accept_milestone`, `complete`, `modify`, `review`, or `approve` in the same Role Assignment.
3. WHEN a Resource Steward assigns a Contributor role to a Party, THE Authorization_Service SHALL accept a Role Assignment whose `authorities_granted` set contains `contribute` and SHALL NOT require, infer, or auto-include `assign`, `accept_milestone`, `complete`, `review`, or `approve` in the same Role Assignment.
4. WHEN a Resource Steward assigns a Milestone Acceptance Authority role to a Party, THE Authorization_Service SHALL accept a Role Assignment whose `authorities_granted` set contains `accept_milestone` and SHALL NOT require, infer, or auto-include `assign`, `contribute`, `complete`, `review`, or `approve` in the same Role Assignment.
5. WHEN a Resource Steward assigns a Completion Authority role to a Party, THE Authorization_Service SHALL accept a Role Assignment whose `authorities_granted` set contains `complete` and SHALL NOT require, infer, or auto-include `assign`, `contribute`, `accept_milestone`, `review`, or `approve` in the same Role Assignment.
6. WHEN the Authorization_Service evaluates a Work Assignment recording attempt, THE Authorization_Service SHALL require the `assign` authority on the evaluated Role Assignment; IF `assign` is not present on the evaluated Role Assignment, THEN THE Authorization_Service SHALL reject the attempt with an authorization error indicating the missing `assign` authority and SHALL leave the Work Assignment Record store unchanged.
7. WHEN the Authorization_Service evaluates a Work Event, Time Entry, produced Deliverable, or Deliverable Production recording attempt, THE Authorization_Service SHALL require both the `contribute` authority on the evaluated Role Assignment AND that the requesting Party is the named assignee on the referenced Work Assignment Record; IF either condition is not satisfied, THEN THE Authorization_Service SHALL reject the attempt with an authorization error identifying which condition failed and SHALL leave the corresponding record store unchanged.
8. WHEN the Authorization_Service evaluates a Milestone Acceptance recording attempt, THE Authorization_Service SHALL require the `accept_milestone` authority on the evaluated Role Assignment; IF `accept_milestone` is not present on the evaluated Role Assignment, THEN THE Authorization_Service SHALL reject the attempt with an authorization error indicating the missing `accept_milestone` authority and SHALL leave the Milestone Acceptance Record store unchanged.
9. WHEN the Authorization_Service evaluates a Completion Record recording attempt, THE Authorization_Service SHALL require the `complete` authority on the evaluated Role Assignment; IF `complete` is not present on the evaluated Role Assignment, THEN THE Authorization_Service SHALL reject the attempt with an authorization error indicating the missing `complete` authority and SHALL leave the Completion Record store unchanged.
10. THE Authorization_Service SHALL distinguish `view`, `modify`, `review`, `approve`, `assign`, `contribute`, `accept_milestone`, and `complete` as eight distinct authority types and WHEN evaluating any Work Assignment, Work Event, Time Entry, produced Deliverable, Deliverable Production, Milestone Acceptance, or Completion Record recording attempt, THE Authorization_Service SHALL NOT substitute one authority type for another, extending the Slice 1 Requirement 12.3 and Slice 2 Requirement 11.6 non-substitution rules.
11. WHEN the Authorization_Service evaluates any action requiring `assign`, `contribute`, `accept_milestone`, or `complete` authority, THE Authorization_Service SHALL append an evaluation record to the Audit_Log per Slice 1 Requirement 12.5 identifying the specific authority required and the specific authority held on the evaluated Role Assignment.

### Requirement 33: Plan / Execution Separation Enforced from the Execution Side (Slice Scope Invariant)

**User Story:** As a Pilot Reviewer, I want this slice to record execution facts only on execution Records and to reject any attempt to record execution facts on planning Resources or to record planning facts on execution Records, so that the Plan/Execution separation principle is enforced in software from both directions.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct), Principle 5.23 (Operational Events and Current Projections Are Distinct).
- Context map: §2.5 (Work Planning) ↔ §2.6 (Work Execution) — distinct contexts with distinct ownership.
- User story map: §5.4 (Plan and Execution Separation must-have story group).
- Slice 2: Requirement 12 (Plan / Execution Separation enforced from the planning side).
- Invariants: planning Resources do not carry execution facts; execution Records do not carry planning facts; the planning ↔ execution boundary is bidirectional and software-enforced.

#### Acceptance Criteria

1. THE Execution_Service and the Deliverable_Repository SHALL NOT, in any creation request or any persisted Record or Revision, write to or extend any planning Resource or Revision created by Slice 2 (Objective, Intended Outcome, Project, Deliverable Expectation, Activity Plan, Plan Revision, Plan Review, Plan Approval Immutable Record); planning Resources and Revisions SHALL remain byte-equivalent to their state immediately before the execution write, per Slice 2 Requirement 9.4.
2. THE Execution_Service SHALL NOT accept, persist, or expose any field naming a planned scope value, planned-deliverable reference, planning-assumption entry, ordering-rationale value, plan-review outcome, plan-approval outcome, or any value sourced from a planning Resource other than the target Identity references named explicitly in Requirements 23 through 29 (`Addresses` and `Relates To` targets to Plan Revision, Deliverable Expectation Revision, and the associated Project Identity), on any Work Assignment Record, Work Event Record, Time Entry Record, Deliverable Production Record, Milestone Acceptance Record, or Completion Record.
3. THE Deliverable_Repository SHALL NOT accept, persist, or expose any field naming a planned scope value, planning-assumption entry, plan-review outcome, plan-approval outcome, planned start date, or planned end date on any produced Deliverable Resource or produced Deliverable Revision.
4. IF a Work Assignment Record, Work Event Record, Time Entry Record, produced Deliverable creation request, Deliverable Production Record, Milestone Acceptance Record, or Completion Record creation request contains any of the prohibited planning attributes named in 33.2 or 33.3, THEN THE Execution_Service or the Deliverable_Repository SHALL reject the request, decline to create any Record, Resource, or Revision, and return an error indication identifying each prohibited planning attribute.
5. THE Execution_Service SHALL NOT expose any HTTP endpoint, function, or relationship type whose stated purpose is to record planning facts, plan revisions, plan reviews, or plan approvals against any execution Record or produced Deliverable Resource created by this slice.
6. WHEN a future slice introduces additional execution or learning Resources targeting any planning Resource created by Slice 2 or any execution Record created by this slice, THE Walking_Slice_System SHALL leave every planning Resource row, Revision, and Relationship created by Slice 2 and every execution Record created by this slice byte-equivalent to its prior state, per Principle 5.23.

### Requirement 34: Output / Outcome Separation Enforced from the Execution Side (Slice Scope Invariant)

**User Story:** As a Pilot Reviewer, I want this slice to record produced Deliverables and recorded completions of planned work, and to reject any attempt to record observed Outcomes, Measurement Records, or Outcome Reviews on any execution Record or produced Deliverable, so that the Output/Outcome separation principle is enforced in software from the execution side.

**Traceability:**
- Constitution: Principle 5.21 (Intent, Work, Output, and Outcome are Distinct).
- Domain model: §7.4 invariant 6 — Outcome is explicitly distinguished as intended or observed.
- Context map: §2.8 (Outcome Measurement and Learning) owns Observed Outcomes (out of scope).
- User story map: §5.5 (Output and Outcome Separation must-have story group).
- Slice 2: Requirement 13 (Output / Outcome Separation enforced from the planning side).
- Invariants: produced Deliverable does not satisfy observed Outcome; recorded completion does not satisfy observed Outcome.

#### Acceptance Criteria

1. THE Execution_Service SHALL NOT accept, persist, or expose any Observed Outcome, Measurement Definition, Measurement Record, Observation Window observation, Outcome Review, attribution-evidence reference, or success-condition assessment on any Work Assignment Record, Work Event Record, Time Entry Record, Deliverable Production Record, Milestone Acceptance Record, or Completion Record created by this slice.
2. THE Deliverable_Repository SHALL NOT accept, persist, or expose any Observed Outcome, Measurement Definition, Measurement Record, Observation Window observation, Outcome Review, attribution-evidence reference, or success-condition assessment on any produced Deliverable Resource or produced Deliverable Revision created by this slice.
3. WHEN the Execution_Service returns any Completion Record representation, THE Execution_Service SHALL include the completion outcome attribute drawn from `{Completed, Completed_With_Reservation}` and SHALL NOT include any field whose value would constitute an observed measurement, observed outcome value, observed outcome time, attribution-evidence reference, or success-condition assessment; the Completion Record SHALL be explicitly labelled as a recorded completion of planned work and SHALL NOT be labelled or aliased as an observed Outcome or as evidence that the Intended Outcome occurred.
4. WHEN the Execution_Service returns any Milestone Acceptance Record representation, THE Execution_Service SHALL distinguish the Record kind as Milestone Acceptance and SHALL NOT label or alias the Record as an Outcome Review or success-condition evaluation.
5. IF a Work Assignment, Work Event, Time Entry, produced Deliverable, Deliverable Production, Milestone Acceptance, or Completion creation request contains any of the prohibited observed-outcome attributes named in 34.1 or 34.2, THEN THE Execution_Service or the Deliverable_Repository SHALL reject the request, decline to create any Record, Resource, or Revision, and return an error indication identifying each prohibited attribute.
6. WHEN a future slice introduces Observed Outcome, Measurement Definition, Measurement Record, or Outcome Review Resources targeting any execution Record or produced Deliverable Revision created by this slice, THE Walking_Slice_System SHALL leave every execution Record, produced Deliverable Resource, and produced Deliverable Revision byte-equivalent to its prior state, per Principle 5.23.

### Requirement 35: Provenance Chain Through Execution Records to Originating Decision and Evidence

**User Story:** As a Plan Reviewer, Decision Reviewer, or Auditor, I want to navigate from any Completion Record, Milestone Acceptance Record, or produced Deliverable Revision back through its Deliverable Production Record, Work Assignment Record, Plan Approval Record, Plan Revision, Activity Plan, Project, Objective, originating Decision, Recommendation, supporting Findings, and exact Content Region Occurrences to the precise Document Revision text, so that I can verify why a piece of work was performed.

**Traceability:**
- Constitution: Principle 5.9 (Provenance Is Preserved End to End), Principle 5.22 (Organizational Learning Is a Closed Loop).
- Domain model: §19 (Provenance graph), §10.9 (Addresses), §10.10 (Produces), §10.2 (Derived From).
- Context map: §2.4 (Knowledge and Provenance) ↔ §2.5 (Work Planning) ↔ §2.6 (Work Execution).
- Slice 1: Requirement 11 (Navigation back to exact Evidence).
- Slice 2: Requirement 14 (Provenance chain to originating Decision and Evidence).
- Invariants: provenance is end-to-end and authorization-aware; missing links are visible; sensitive information does not leak.

#### Acceptance Criteria

1. WHEN an authorized Party requests the provenance chain of a Completion Record, Milestone Acceptance Record, Deliverable Production Record, produced Deliverable Revision, Time Entry Record, Work Event Record, or Work Assignment Record, THE Provenance_Navigator SHALL return an ordered traversal that begins at the requested node and ends at one or more Document Revisions in the Slice 1 Evidence_Repository, identifying each intermediate node (Approved Plan Revision, Activity Plan, Project, Objective, Slice 1 Decision, Recommendation Revision, Finding Revision(s), Content Region Occurrence(s)) by its Identity and (where applicable) Revision Identity, joining seamlessly with the Slice 2 Requirement 14.1 traversal.
2. WHEN the Provenance_Navigator returns a Content Region Occurrence in an Execution Provenance Chain, THE Provenance_Navigator SHALL include the exact start anchor, end anchor, and bounded text span of that Occurrence in the originating Document Revision, byte-equivalent to the text recorded for that Region Occurrence and digest-matching against the recorded content digest, per Slice 1 Requirement 11.2.
3. IF a node in an Execution Provenance Chain is restricted from the requesting Party, THEN THE Provenance_Navigator SHALL replace that node with a policy-conformant redaction marker containing only a generic redaction indicator and the original node kind, and SHALL NOT disclose any identifier, count, or attribute value of the redacted node beyond the `slice-default-2026` policy as extended by Requirement 38.
4. IF a required upstream link is unresolved, restricted, stale, or unavailable, THEN THE Provenance_Navigator SHALL identify the gap explicitly with a gap descriptor identifying the stage in the chain, the gap category drawn from `{unavailable, restricted, stale, unresolved}`, and the Identity of the next reachable node where applicable.
5. THE Provenance_Navigator SHALL produce the same Execution Provenance Chain for the same anchor node Identity, requesting Party authority set, and effective time inputs (idempotent retrieval), within 5 seconds for chains of up to 100 nodes.
6. IF the requested anchor node Identity does not resolve, THEN THE Provenance_Navigator SHALL return an error indication identifying the unresolvable anchor reference and SHALL NOT disclose existence of any related execution Records, produced Deliverable Revisions, or planning Resources.
7. IF the requesting Party is unauthenticated or lacks any view authority on the anchor node, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from one for a non-existent anchor, conforming to the Slice 1 `slice-default-2026` policy as extended by Requirement 38.
8. WHEN the Provenance_Navigator returns a produced Deliverable Revision node in an Execution Provenance Chain, it SHALL include the produced-Deliverable role marker `generated_output` and the content digest of the produced Revision, and SHALL distinguish the produced Deliverable Revision from any Source Evidence Document Revision recorded in the Slice 1 Evidence_Repository.

### Requirement 36: Authorization-Aware Backlinks Extended to Execution Nodes

**User Story:** As a Project Owner, Plan Approver, Contributor, or Source Owner, I want to see which Work Assignment Records, Work Event Records, Time Entry Records, Deliverable Production Records, produced Deliverable Revisions, Milestone Acceptance Records, and Completion Records depend on a particular planning, knowledge, or evidence Resource, subject to my authorization, so that I can understand downstream impact across the execution chain without leaking restricted relationships.

**Traceability:**
- Constitution: Principle 5.12 (Dependencies Are Visible Before Change), Principle 5.25, Principle 5.26.
- Context map: §2.1 (Shared Graph Foundation), §3 Cross-Context Rules — bidirectional discoverability does not transfer authority.
- Slice 1: Requirement 8 (Authorization-Aware Backlinks).
- Slice 2: Requirement 15 (Authorization-Aware Backlinks Extended to Planning Nodes).
- Invariants: bidirectional discovery, no inference leakage, discovery does not transfer authority.

#### Acceptance Criteria

1. WHEN an authorized Party holding view authority on the queried endpoint requests inbound Relationships for a Work Assignment Record, Work Event Record, Time Entry Record, Deliverable Production Record, produced Deliverable Resource, produced Deliverable Revision, Milestone Acceptance Record, or Completion Record, THE Provenance_Navigator SHALL return every inbound Relationship for which the requesting Party holds applicable view authority on both the Relationship and its source endpoint, in deterministic ordering, within 2 seconds for result sets of up to 500 backlinks.
2. WHEN the Provenance_Navigator returns a backlink whose source endpoint is an execution Record or produced Deliverable introduced by this slice, THE Provenance_Navigator SHALL identify the backlink by its Relationship Identity, Relationship Type, source endpoint Identity, source endpoint Type, source endpoint Revision Identity (when applicable), and authoring Party Identity, per Slice 1 Requirement 8.2 and Slice 2 Requirement 15.2.
3. IF the requesting Party lacks authority to know that an inbound Relationship or its source endpoint exists, THEN THE Provenance_Navigator SHALL omit the Relationship from results and SHALL produce results indistinguishable in counts, identifier sets, ordering positions, pagination cursors, response size, and latency (within 100 milliseconds variation) from results in which the omitted Relationships do not exist, per Slice 1 Requirement 8.3 and Slice 2 Requirement 15.3.
4. THE Provenance_Navigator SHALL NOT grant the requesting Party any view, modify, review, approve, assign, contribute, accept_milestone, or complete authority on the source endpoint, on the Relationship Identity itself, or on any traversed Records or Revisions of the source endpoint, solely as a result of returning a backlink, per Slice 2 Requirement 15.4.
5. IF the requesting Party is unauthenticated or lacks view authority on the queried endpoint, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from a response for a non-existent endpoint, conforming to the Slice 1 `slice-default-2026` policy as extended by Requirement 38.
6. THE Provenance_Navigator SHALL bound each backlink response for execution endpoints to at most 500 Relationships and SHALL provide a continuation reference whose length, identifier values, and presence do not vary based on the existence of Relationships the requesting Party lacks authority to know.


### Requirement 37: Audit of Consequential and Denied Execution Actions

**User Story:** As an Auditor, I want every consequential creation and every denied unauthorized attempt within the third walking slice to leave an immutable Audit_Log record, so that I can reconstruct what happened and what was rejected across all three slices.

**Traceability:**
- Constitution: Principle 5.25 (Access Is Explicit and Auditable), Principle 5.7 (No Acknowledged Work Is Silently Lost).
- Domain model: §8.4 (Audit Event invariants — append-only).
- Slice 1: Requirement 13 (Audit of Consequential and Denied Actions).
- Slice 2: Requirement 16 (Audit of Consequential and Denied Planning Actions).
- Invariants: audit is append-only; insertion order preserved by recorded time and append sequence; failure to audit rolls back the originating action.

#### Acceptance Criteria

1. WHEN the Walking_Slice_System finalizes the creation of a Work Assignment Record, Work Event Record, Time Entry Record, produced Deliverable Revision, Deliverable Production Record, Milestone Acceptance Record, or Completion Record, THE Audit_Log SHALL append an immutable record identifying actor Party Identity, action type, target Record or Resource Identity, target Revision Identity when applicable, recorded time in UTC with millisecond precision, and operation correlation identifier, before the success response returns to the caller, per Slice 1 Requirement 13.1.
2. WHEN the Authorization_Service denies any consequential execution action (Work Assignment, Work Event, Time Entry, produced Deliverable, Deliverable Production, Milestone Acceptance, or Completion Record), THE Audit_Log SHALL append an immutable Denial Record identifying actor Party Identity, attempted action, target Identity, target Revision Identity when applicable, recorded time, denial reason category drawn from the enumerated set in Slice 1 Requirement 7.2, and correlation identifier, before the denial response returns to the caller, per Slice 1 Requirement 13.2.
3. THE Audit_Log SHALL remain append-only across all three slices and SHALL reject all update and delete operations on previously appended records, per Slice 1 Requirement 13.3.
4. THE Audit_Log SHALL preserve insertion order of appended records using recorded time as primary order and append sequence as tiebreaker across all three slices, per Slice 1 Requirement 13.4.
5. IF an actor attempts to modify or delete a previously appended Audit_Log record arising from a Slice 3 execution action, THEN THE Audit_Log SHALL reject the operation and SHALL append an immutable Denial Record covering the rejected attempt, per Slice 1 Requirement 13.5.
6. IF an audit append for any consequential execution creation or execution denial fails, THEN THE Walking_Slice_System SHALL roll back the originating action, decline to expose any artifact of that action, and return an error indication identifying the audit append failure, per Slice 1 Requirement 13.6.

### Requirement 38: Additive Extension of the Completeness Disclosure Policy to Execution Node Kinds

**User Story:** As a Disclosure Policy Owner, I want the new execution and produced-Deliverable node kinds introduced by this slice to be covered by an additive extension of the Slice 1 and Slice 2 `slice-default-2026` policy rather than by a separate policy, so that one cohesive disclosure contract governs every backlink, provenance, and denial response across all three slices.

**Traceability:**
- Constitution: Principle 5.25, Principle 5.26.
- Context map: §3 Cross-Context Rule 9 (Authorization filtering shall follow completeness-disclosure and inference-risk policy).
- Slice 1: AD-WS-9 (`slice-default-2026` default Completeness Disclosure policy).
- Slice 2: Requirement 17 (Additive Extension of the Completeness Disclosure Policy).
- Gap: G-12 (additive policy extension for new execution and produced-Deliverable node kinds).
- Invariants: policy identity is unchanged; rule set is additively extended; restricted-vs-nonexistent observability remains constant across all three slices.

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL extend the policy named `slice-default-2026` in the `Disclosure_Policies` registry to cover every node kind introduced by this slice — Work Assignment Record, Work Event Record, Time Entry Record, produced Deliverable Resource, produced Deliverable Revision, Deliverable Production Record, Milestone Acceptance Record, and Completion Record — and SHALL NOT introduce a separate disclosure policy or replace the existing policy.
2. WHEN the Provenance_Navigator, the Execution_Service, the Deliverable_Repository, or the Authorization_Service encounters a restricted node whose kind was introduced by this slice, THE Walking_Slice_System SHALL replace the node with a redaction marker of the form `{"kind": "<node_kind>", "redacted": true}` carrying no identifier, attribute, or count, per the AD-WS-9 rule set as extended by Slice 2 Requirement 17.2.
3. WHEN the Provenance_Navigator, the Execution_Service, the Deliverable_Repository, or the Authorization_Service encounters a node introduced by this slice in an `unavailable`, `stale`, or `unresolved` category, THE Walking_Slice_System SHALL return a gap descriptor containing only `stage`, `category`, and (if the next reachable node is visible to the requesting Party) the next reachable node's identity, per the AD-WS-9 rule set as extended by Slice 2 Requirement 17.3.
4. THE Walking_Slice_System SHALL produce indistinguishable restricted-vs-nonexistent observability for every node kind introduced by this slice across counts, identifier sets, pagination cursors, response sizes, error wording, and latency (within 100 milliseconds variation), matching the Slice 1 and Slice 2 observability guarantees.
5. THE Walking_Slice_System SHALL record the additive extension of `slice-default-2026` as a new row entry in the policy registry or as an additive update on the existing row that does not alter the policy identity or the Slice 1 + Slice 2 rule scope; the recorded extension SHALL identify each newly covered node kind, the recorded date of the extension, and the backlog ADR identifier reserved for replacement, per Gap G-12.

### Requirement 39: Explainable Projection of Current Execution Status

**User Story:** As a Pilot Reviewer or Project Owner, I want any projected status surfaced over execution Records (for example, "Plan Revision in execution", "Plan Revision deliverable produced", "Plan Revision milestone accepted", "Plan Revision completion recorded") to be explainable from its source Records, so that derived views of current execution status cannot be mistaken for authoritative facts and so that derived current-execution status is distinct from authoritative completion or outcome.

**Traceability:**
- Constitution: Principle 5.23 (Operational Events and Current Projections Are Distinct), Principle 5.30 (System Health Must Be Observable).
- Slice 1: Requirement 14 (Explainable Projection of Slice Status), Projection Envelope from Slice 1 design.
- Slice 2: Requirement 18 (Explainable Projection of Plan Status).
- User story map: §4 Release 1C step 7 ("Derive current status from source records").
- Invariants: projections carry derivation indicator; source records are unaltered when corrections arrive; derived current-execution status is not an Observed Outcome.

#### Acceptance Criteria

1. WHEN the Execution_Service exposes a projected status over slice Records — including but not limited to "Plan Revision in execution", "Plan Revision deliverable produced", "Plan Revision milestone accepted", "Plan Revision completion recorded", "Plan Revision execution paused", or "Provenance incomplete" — THE Execution_Service SHALL include alongside the projected status in the same response the Projection Definition, source Record Identities, source Revision Identities, applicable temporal boundary, and generated time, with the temporal boundary and generated time expressed in ISO-8601 form with at least second precision, per Slice 1 Requirement 14.1 and Slice 2 Requirement 18.1.
2. THE Execution_Service SHALL include on every exposed projected status a derivation indicator distinguishing it from authoritative source Records, per Slice 1 Requirement 14.2 and Principle 5.23.
3. THE Execution_Service SHALL NOT include in any projected status response a derived percent-complete value, derived actual-cost value, derived remaining-work value, derived budget-variance value, derived forecast-cost value, or derived outcome-attainment value; such derived values are reserved for later slices (Slice 1D Outcome Measurement and Slice 3 Portfolio Intelligence) and SHALL NOT be surfaced by this slice.
4. WHEN a corrected or late-arriving source fact changes a Plan Revision's projected execution status (for example, a Work Event Record arrives whose recorded time precedes a previously surfaced "milestone accepted" projection), THE Execution_Service SHALL retain every prior source Record and correction record byte-equivalent to its recorded state and SHALL append new facts as additional Records rather than overwriting existing ones, per Slice 1 Requirement 14.3.
5. IF the Projection Definition or any required source Record cannot be resolved, THEN THE Execution_Service SHALL withhold the projected status, return an explanation-unavailable indicator identifying the missing element, and leave stored source Records unchanged, per Slice 1 Requirement 14.4.
6. THE Execution_Service SHALL NOT label or alias any projected execution status as evidence of an Observed Outcome, a satisfied Intended Outcome, or a success-condition assessment; projected execution status is a projection of work performed, not a projection of outcome.

### Requirement 40: Reuse and Non-Modification of Slice 1 and Slice 2 Contexts

**User Story:** As a Project Owner, I want the implementation of this slice to extend Slice 1 and Slice 2 contexts only through additive interfaces rather than through modification of existing behavior, so that previously-recorded Evidence chains, Decision Records, planning Resources, Plan Approvals, and audit history remain stable.

**Traceability:**
- Constitution: Principle 5.4 (Authority and Derivation Are Distinct), Principle 5.6 (Durable states are historical), Principle 5.29 (Empirical Learning Constrains Conceptual Expansion).
- Context map: §3 Cross-Context Rules — context translation, no silent mutation across contexts.
- Slice 1: Requirement 16 (Prerequisite Architecture Decisions), AD-WS-1 through AD-WS-13.
- Slice 2: Requirement 19 (Reuse and Non-Modification of Slice 1 Contexts).
- Gaps: G-11 through G-15 (recorded in §"Gaps Flagged for Resolution") all require additive Interim ADR records under this requirement's regime.
- Invariants: Slice 1 modules and Slice 2 modules in `src/walking_slice/` are not modified to satisfy this slice; new behavior is recorded as an additive Execution module and an additive Deliverable_Repository module, or as additive extension records on the existing registries.

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL implement every new behavior introduced by this slice in a new Execution module (or in a set of new modules subordinate to a new Execution context) and a new Deliverable_Repository module, without removing, renaming, narrowing, or changing the semantics of any function, class, table, trigger, route, or invariant established by Slice 1 or Slice 2.
2. WHERE this slice extends a Slice 1 or Slice 2 enumeration (for example, the authority enumeration in Requirement 32) or a Slice 1 or Slice 2 registry (for example, the disclosure policy in Requirement 38), THE Walking_Slice_System SHALL implement the extension as an additive change that preserves every prior enumeration member, registry row, and behavior unchanged.
3. WHEN this slice records a Relationship from a Slice 3 execution Record or produced Deliverable to a Slice 1 or Slice 2 Resource (for example, an `Addresses` Relationship from a Work Assignment Record to a Slice 2 Approved Plan Revision, or an `Addresses` Relationship from a Deliverable Production Record to a Slice 2 Deliverable Expectation Revision), THE Walking_Slice_System SHALL leave the Slice 1 or Slice 2 Resource row, Revision, and any pre-existing Relationships sourced from or targeting that Resource byte-equivalent to their prior state.
4. THE Walking_Slice_System SHALL NOT mutate any Audit_Records row, Identifier_Registry row, Interim_ADR_Records row, Disclosure_Policies row (apart from the additive extensions permitted by Slice 2 Requirement 17.5 and Requirement 38.5 of this slice), Decisions row, Role_Assignments row (apart from the additive enumeration extensions permitted by Slice 2 Requirement 11.1 and Requirement 32.1 of this slice), Document_Revisions row, Region_Occurrences row, Finding_Revisions row, Recommendation_Revisions row, Relationships row, Trail_Revisions row, Trail_Steps row, Provenance_Manifests row, Objective_Revisions row, Intended_Outcome_Revisions row, Project_Revisions row, Deliverable_Expectation_Revisions row, Activity_Plans row, Plan_Revisions row, Plan_Review_Revisions row, or Plan_Approval_Records row created by Slice 1 or Slice 2 as a consequence of any Slice 3 execution action.
5. THE Walking_Slice_System SHALL record additive Interim ADR records covering the gaps introduced by this slice (Gaps G-11 through G-15 — see §"Gaps Flagged for Resolution") as new rows in the `Interim_ADR_Records` registry, each identifying the motivating Requirement number, the motivating criterion number, the observable behavior chosen, the recorded date of the choice, and the backlog ADR identifier, per Slice 1 Requirement 16.3 and Slice 2 Requirement 19.5.
6. IF a Slice 3 implementation change would require modification of any Slice 1 or Slice 2 module behavior or schema, THEN the Walking_Slice_System SHALL record the proposed modification as a new Interim ADR row and SHALL halt the Slice 3 implementation until the user is asked to approve the modification, so that Slice 1 and Slice 2 stability remain explicit decisions rather than side effects.

### Requirement 41: Correctness Properties for Property-Based Testing

**User Story:** As a Verification Engineer, I want the third walking slice to be verified by property-based tests that exercise the slice's invariants over generated inputs, so that the named demonstrations are tested at the level of properties, not only worked examples.

**Traceability:**
- Slice 1: Requirement 15 (Correctness Properties for Property-Based Testing), AD-WS-13 (Hypothesis ≥ 100 cases per property, seeded).
- Slice 2: Requirement 20 (Correctness Properties for Property-Based Testing).
- Domain model: §3 Resource invariants, §4 Resource Revision invariants, §8 Immutable Record invariants, §10 Relationship Type invariants.
- Invariants: execution operations preserve identity, immutability, authority separation, and provenance traceability under all generated inputs.

Each acceptance criterion below states a property the implementation SHALL preserve under property-based testing. Per Slice 1 AD-WS-13 and Slice 2 Requirement 20.13, every property test SHALL run under the Hypothesis library with `@settings(max_examples=100, deadline=2000)` and SHALL capture and replay the generation seed.

#### Acceptance Criteria

1. **Approved-Plan anchoring (invariant).** FOR ALL Work Assignment Records recorded by the Execution_Service, the Walking_Slice_System SHALL satisfy: every Work Assignment Record has exactly one `Addresses` Relationship to a Plan Revision Identity that resolves in the Planning_Service and whose lifecycle state at Work Assignment recording time is `approved`. No Work Assignment Record exists without a matching Approved Plan Revision.
2. **Execution-Record authority (invariant).** FOR ALL Work Assignment Records, the Assignment Authority Party held an effective Role Assignment whose granted authorities include `assign` at the recorded time; FOR ALL Work Event Records, Time Entry Records, produced Deliverable Revisions, and Deliverable Production Records, the recording Party held an effective Role Assignment whose granted authorities include `contribute` at the recorded time AND was the named assignee on the referenced Work Assignment Record; FOR ALL Milestone Acceptance Records, the accepting Party held an effective Role Assignment whose granted authorities include `accept_milestone` at the recorded time; FOR ALL Completion Records, the completing Party held an effective Role Assignment whose granted authorities include `complete` at the recorded time. No execution Record exists without a matching authority record.
3. **Authority non-substitution (invariant).** FOR ALL execution Records, the Walking_Slice_System SHALL satisfy: the eight authority types `{view, modify, review, approve, assign, contribute, accept_milestone, complete}` are pairwise distinct in the Role Assignment evaluation function; no Work Assignment exists whose Assignment Authority Party held only `view`, `modify`, `review`, `approve`, `contribute`, `accept_milestone`, or `complete` authority; no Milestone Acceptance exists whose accepting Party held only any single non-`accept_milestone` authority among those eight; no Completion Record exists whose completing Party held only any single non-`complete` authority among those eight; and no Work Event, Time Entry, produced Deliverable, or Deliverable Production exists whose recording Party held only any single non-`contribute` authority among those eight.
4. **Execution-Record immutability (invariant).** FOR ALL Work Assignment Records, Work Event Records, Time Entry Records, produced Deliverable Revisions, Deliverable Production Records, Milestone Acceptance Records, and Completion Records finalized at any observation point in the test session, the Walking_Slice_System SHALL satisfy: at every later observation point in the test session, the Record row, every constituent field of the Record, and every `Produces`, `Addresses`, and `Relates To` Relationship sourced from or targeting that Record are byte-equivalent to their state at first finalization.
5. **Plan/Execution separation enforced from execution side (invariant).** FOR ALL execution Records and produced Deliverable Revisions created by the Execution_Service or Deliverable_Repository, the Walking_Slice_System SHALL satisfy: no row of any execution Record or produced Deliverable carries a planned scope value, planning-assumption entry, ordering-rationale value, plan-review outcome, plan-approval outcome, planned start date, or planned end date attribute; and no planning Resource or Revision created by Slice 2 has been mutated as a consequence of any Slice 3 action.
6. **Output/Outcome separation enforced from execution side (invariant).** FOR ALL execution Records and produced Deliverable Revisions, the Walking_Slice_System SHALL satisfy: no execution Record and no produced Deliverable Revision carries an Observed Outcome, Measurement Definition, Measurement Record, Outcome Review, attribution-evidence reference, or success-condition assessment attribute; and every Completion Record carries the completion outcome attribute drawn from `{Completed, Completed_With_Reservation}` and no observed-outcome attribute.
7. **Execution provenance chain end-to-end (invariant).** FOR ALL Completion Records whose entire Execution Provenance Chain is visible to a requesting Party, the Walking_Slice_System SHALL satisfy: traversal from the Completion Record yields the ordered sequences Completion → Milestone Acceptance → Deliverable Production → produced Deliverable Revision, Completion → Plan Approval → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision → Recommendation Revision → Finding Revision(s) → Content Region Occurrence(s) → Document Revision, and Completion → Work Assignment → Work Event(s) → Time Entry(ies); every node identity in the returned chains resolves; the returned Content Region Occurrence span fields match the digest recorded on the Region Occurrence; and the chains are byte-equivalent across at least five repeated invocations of `navigate(completion, party, t)` (idempotent retrieval).
8. **Indistinguishable denial for execution endpoints (metamorphic).** FOR ALL Parties `P` and `P′` differing only in that `P′` lacks effective Assignment Authority, Contributor authority, Milestone Acceptance Authority, or Completion Authority on some execution Record or produced Deliverable target `R`, the Walking_Slice_System SHALL satisfy: responses returned to `P′` for creation attempts on `R` are indistinguishable from responses produced when `R` does not exist, across observable channels result count, identifier set, ordering positions, pagination cursors, response size, error category, error wording, and latency (within 100 milliseconds variation).
9. **Backlink bidirectionality for execution Resources (round-trip).** FOR ALL Relationships `R` recorded between execution Records, between execution Records and produced Deliverable Revisions, between execution Records and planning Resources, or between execution Records and Slice 1 Resources, and FOR ALL requesting Parties `P` who hold view authority on both `R` and its source endpoint, the Walking_Slice_System SHALL satisfy: the Provenance_Navigator returns `R` from the target's backlink query if and only if `R` is returned from the source's outbound query, and the Relationship attribute values returned from both directions are identical.
10. **Uniqueness of Milestone Acceptance and Completion (invariant).** FOR ALL Deliverable Production Record Identities created in any test session, the Walking_Slice_System SHALL satisfy: at most one Milestone Acceptance Record exists for a given source Deliverable Production Record Identity; a second Milestone Acceptance attempt against the same Deliverable Production Record is rejected with no Milestone Acceptance Record persisted. FOR ALL Plan Revision Identities created in any test session, at most one Completion Record exists for a given target Approved Plan Revision Identity; a second Completion Record attempt against the same Plan Revision is rejected with no Completion Record persisted.
11. **Slice 1 and Slice 2 non-modification (invariant).** FOR ALL test sessions exercising the Execution_Service and Deliverable_Repository, the Walking_Slice_System SHALL satisfy: at every observation point after any sequence of Slice 3 actions, every Audit_Records row, Identifier_Registry row, Interim_ADR_Records row, Disclosure_Policies row (apart from the additive extensions permitted by Slice 2 Requirement 17.5 and Requirement 38.5 of this slice), Decisions row, Role_Assignments row (apart from the additive enumeration extensions permitted by Slice 2 Requirement 11.1 and Requirement 32.1 of this slice), Document_Revisions row, Region_Occurrences row, Finding_Revisions row, Recommendation_Revisions row, Relationships row, Trail_Revisions row, Trail_Steps row, Provenance_Manifests row, Objective_Revisions row, Intended_Outcome_Revisions row, Project_Revisions row, Deliverable_Expectation_Revisions row, Activity_Plans row, Plan_Revisions row, Plan_Review_Revisions row, and Plan_Approval_Records row created by Slice 1 or Slice 2 is byte-equivalent to its state before the Slice 3 actions began.
12. **Identity uniqueness across slices (invariant).** FOR ALL identifiers issued by the Identity_Service in any test session covering all three slices, the Walking_Slice_System SHALL satisfy: identifiers are unique across all three slices and across every Resource kind and every Record kind, are in canonical UUIDv7 lowercase hyphenated form, and do not embed business metadata, per Slice 1 Requirement 1.1 and Slice 2 Requirement 20.12.
13. **Produced-Deliverable vs Source-Evidence disjointness (invariant).** FOR ALL produced Deliverable Resource Identities created by the Deliverable_Repository in any test session, the Walking_Slice_System SHALL satisfy: the produced Deliverable Resource Identity is not also identifying any Source Evidence Document Resource recorded by the Slice 1 Evidence_Repository; and conversely, no Source Evidence Document Resource Identity is reissued as a produced Deliverable Resource Identity. The produced-Deliverable role marker `generated_output` is recorded on every produced Deliverable Revision and is absent from every Source Evidence Document Revision.
14. **Audit atomicity for every execution write (invariant).** FOR ALL execution Records and produced Deliverable Revisions finalized in any test session, the Walking_Slice_System SHALL satisfy: an Audit_Log creation record exists for the finalization, was appended within the same transaction as the finalization, and identifies the same actor Party Identity, target Identity, and recorded time as the finalized Record or Revision. If the Audit_Log append fails for any test-generated finalization, the originating finalization SHALL have been rolled back and SHALL NOT be observable from any query path.
15. **Repeatable property runs (operational).** THE property-based test suite for Slice 3 SHALL execute at least 100 generated cases per property under the Hypothesis library with `@settings(max_examples=100, deadline=2000)`, record the seed of every test invocation, and on re-execution with the same seed produce identical pass/fail outcomes and identical minimal counterexamples for failing properties, per Slice 1 Requirement 15.13 and Slice 2 Requirement 20.13.

Properties 1–15 are the verification targets for the property-based test suite associated with this slice. They complement, and do not replace, the Slice 1 and Slice 2 property suites; all three suites run together in the cumulative verification of the Walking_Slice_System.

### Requirement 42: Prerequisite Architecture Decisions and Interim ADRs

**User Story:** As a Project Owner, I want this slice's implementation to depend only on architecture decisions whose status is `Accepted` or that are explicitly recorded as Interim ADRs, so that downstream work does not rest on unresolved foundational choices.

**Traceability:**
- Constitution: Principle 5.29 (Empirical Learning Constrains Conceptual Expansion).
- Slice 1: Requirement 16 (Prerequisite Architecture Decisions), AD-WS-1 through AD-WS-13, Interim ADR records for Gaps G-1 through G-6.
- Slice 2: Requirement 21 (Prerequisite Architecture Decisions and Interim ADRs), Interim ADR records for Gaps G-6 through G-10.
- Invariants: every interim choice is recorded; transition to an accepted ADR requires explicit revision of the affected criteria.

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL, for every identity, audit, authorization, evidence, knowledge, trail, provenance, and planning behavior reused from Slice 1 and Slice 2, conform to the prior-slice acceptance criteria for that behavior without weakening, broadening, or replacing those criteria, per Slice 1 Requirement 16.1 and Slice 2 Requirement 21.1.
2. WHERE a behavior in this slice requires a choice that is not yet resolved by an `Accepted` ADR (Gaps G-11 through G-15 in §"Gaps Flagged for Resolution"), THE Walking_Slice_System SHALL implement the behavior required by the specific acceptance criteria in this document that motivated the dependency.
3. WHERE the slice implements an interim behavior in advance of a backlog ADR being `Accepted`, THE project SHALL record, for each such interim behavior, the motivating Requirement number, the motivating criterion number, the observable behavior chosen, the recorded date of the choice, and the backlog ADR identifier reserved for replacement, and SHALL make the record retrievable by backlog ADR identifier in the `Interim_ADR_Records` registry, per Slice 1 Requirement 16.3.
4. THE Walking_Slice_System SHALL seed five new Interim ADR rows in the `Interim_ADR_Records` registry on first start of the Slice 3 implementation, one each for the placeholder backlog ADR identifiers `ADR-HT-013`, `ADR-HT-014`, `ADR-HT-015`, `ADR-HT-016`, and `ADR-HT-017`, corresponding respectively to Gaps G-11 through G-15 below; the seeded rows SHALL identify the motivating Requirement number, motivating criterion number, observable behavior chosen, recorded date, and backlog ADR identifier.
5. IF a backlog ADR transitions to `Accepted` status with a decision whose observable behavior is not consistent with that ADR's accepted decisions, THEN the slice implementation SHALL be revised so that every affected acceptance criterion is satisfied before the verification status of the affected criteria advances beyond `Specified`, per Slice 1 Requirement 16.4 and Slice 2 Requirement 21.4.


## Out-of-Scope Boundaries

The following are intentionally deferred from this slice and SHALL NOT be required to be implemented to satisfy the requirements above. They are listed here to make scope discipline explicit and to align with [`documents/07-user-story-map.md`](../../../documents/07-user-story-map.md) §§4 and 5 and with the Plan/Execution and Output/Outcome separation principles in [`documents/00-project-constitution.md`](../../../documents/00-project-constitution.md) §5.21.

- Slice 1D — Deliverable to Outcome Review: Measurement Definition, Measurement Record, Observation Window observation, Observed Outcome Resource, Outcome Review, success-condition evaluation, attribution-assumption assessment.
- Slice 1E — Learning to Adaptation: Learning Record, Adaptation Decision, Plan supersession beyond the approval-immutability boundary, revised-Objective workflow.
- Slice 2 — Reproducible Publication of execution artifacts: Publication Candidate, Publication Assessment, Published Version, Rendered Output association for produced Deliverable Revisions and execution Records.
- Slice 3 — Investment, cost, capacity, and portfolio reporting against execution records: Budget, Allocation, Capacity Plan, Rate, Estimate, Commitment, Expenditure reference, Forecast Definition, Portfolio Projection.
- Blockage Observation and risk observation Records, and any derived current-risk projection.
- Deliverable Reviewer authority and any Deliverable Review record distinct from Milestone Acceptance.
- Multiple parallel Work Assignments per Approved Plan Revision; multiple Contributors per Work Assignment; reassignment, decline, withdrawal, or supersession of a Work Assignment.
- Replacement, withdrawal, retraction, or correction of a recorded Work Event, Time Entry, produced Deliverable Revision, Deliverable Production Record, Milestone Acceptance Record, or Completion Record (a later governed supersession path is out of scope).
- Conditional Milestone Acceptance, two-of-N Milestone Acceptance, delegated Milestone Acceptance, conditional Completion, partial Completion, or delegated Completion; this slice records exactly one Milestone Acceptance Authority per Milestone Acceptance Record and exactly one Completion Authority per Completion Record.
- Portability export of execution Records or produced Deliverable Revisions.
- Automated Agent contribution provenance for execution Records or produced Deliverable Revisions beyond recording that an authoring Party is human.
- Modifications to Slice 1 or Slice 2 contexts (Identity, Audit, Authorization, Evidence, Knowledge, Trails, Provenance, Planning) other than the additive enumeration and registry extensions defined in Requirements 32, 38, and 40.

## Traceability Summary

This slice realizes a strict subset of upstream artifacts. The table below summarizes the principal sources of authority for each requirement; the **Traceability** blocks within each requirement above are authoritative for individual mappings.

| Req | Primary Constitution | Primary Domain Model | Primary Context Map | Primary Prior-Slice Anchor |
|---|---|---|---|---|
| 22 | 5.5, 5.6 | §3, §4, §8, §8.2 | §2.1 | Slice 1 Req 1, Slice 2 Req 1 |
| 23 | 5.21, 5.25 | §8, §10.9, §10.5 | §2.5 ↔ §2.6 | Slice 2 Req 9 |
| 24 | 5.21, 5.23, 5.7 | §8, §10.5 | §2.6 | — |
| 25 | 5.21, 5.23 | §8 | §2.6 | — |
| 26 | 5.6, 5.9, 5.21 | §3, §4, §7.1, §7.2, §9.1, §10.10, §10.2 | §2.2 | Slice 1 Req 2 |
| 27 | 5.9, 5.21 | §8.2, §9.1, §10.10, §10.9, §10.2 | §2.6 | Slice 2 Req 5 |
| 28 | 5.21, 5.25 | §8.5, §10.9 | §2.6, §2.9 | — |
| 29 | 5.6, 5.21, 5.25 | §8.5, §10.9 | §2.6, §2.9 | Slice 2 Req 9 |
| 30 | 5.25, 5.26 | §8.4, §8.5 | §2.9 | Slice 1 Req 7, Slice 2 Req 10, AD-WS-9 |
| 31 | 5.9, 5.21, 5.22 | §10.9, §10.10, §19 | §2.5 ↔ §2.6 | Slice 2 Req 9, Req 14 |
| 32 | 5.25 | §9 (Contextual Roles) | §2.9 | Slice 1 Req 12, Slice 2 Req 11 |
| 33 | 5.21, 5.23 | §7.4, §8 | §2.5 ↔ §2.6 | Slice 2 Req 12 |
| 34 | 5.21 | §7.4 invariant 6 | §2.8 | Slice 2 Req 13 |
| 35 | 5.9, 5.22 | §19, §10.9, §10.10, §10.2 | §2.4 ↔ §2.5 ↔ §2.6 | Slice 1 Req 11, Slice 2 Req 14 |
| 36 | 5.12, 5.25, 5.26 | §10 (Relationship invariants) | §2.1, §3 rule 8 | Slice 1 Req 8, Slice 2 Req 15 |
| 37 | 5.25, 5.7 | §8.4 invariants | §2.9 | Slice 1 Req 13, Slice 2 Req 16 |
| 38 | 5.25, 5.26 | — | §3 rule 9 | AD-WS-9, Slice 2 Req 17 |
| 39 | 5.23, 5.30 | §19 | §2.6 | Slice 1 Req 14, Slice 2 Req 18 |
| 40 | 5.4, 5.6, 5.29 | — | §3 cross-context rules | Slice 1 Req 16, Slice 2 Req 19, AD-WS-1..AD-WS-13 |
| 41 | (verification target across slice) | (invariants cited per property) | — | Slice 1 Req 15, Slice 2 Req 20, AD-WS-13 |
| 42 | 5.29 | — | — | Slice 1 Req 16, Slice 2 Req 21 |

## Gaps Flagged for Resolution

The following gaps were identified while reconciling this slice with the upstream documents, Slice 1, and Slice 2. They continue the cumulative Gap numbering scheme established by Slice 1 (G-1 through G-6, recorded in [`../first-walking-slice/requirements.md`](../first-walking-slice/requirements.md) §"Gaps Flagged for Resolution") and Slice 2 (G-6 through G-10, recorded in [`../second-walking-slice/requirements.md`](../second-walking-slice/requirements.md) §"Gaps Flagged for Resolution"). New gaps start at **G-11**. They are recorded here so they can be addressed in the design phase rather than rediscovered during implementation.

1. **G-11 — Authority enumeration extension for execution authorities is undecided.** Requirement 32 requires four additive authority types `assign`, `contribute`, `accept_milestone`, and `complete` alongside the Slice 1 + Slice 2 enumeration `{view, modify, review, approve}`. No upstream ADR yet enumerates the full canonical authority types or governs additive expansion beyond Slice 2's `review` value. The slice's design SHALL choose an interim representation (for example, extending the JSON array column already extended for `review` and adding four registry rows covering the new values' semantics) and document the choice as input to a new backlog ADR (placeholder `ADR-HT-013`).
2. **G-12 — Disclosure policy extension to execution and produced-Deliverable node kinds is undecided.** Requirement 38 requires the `slice-default-2026` policy to be additively extended to cover the new execution Record kinds and produced Deliverable kinds. The mechanism chosen by Slice 2 (Gap G-7) is a candidate, but its applicability to Execution Record kinds and to produced Document/Artifact Revisions has not been validated. The slice's design SHALL choose an interim representation consistent with G-7's resolution and document the choice as input to a new backlog ADR (placeholder `ADR-HT-014`).
3. **G-13 — Relationship semantics for `Produces`, `Addresses`, and `Relates To` between execution Records, produced Deliverable Revisions, and planning Resources are partially undecided.** Requirement 27 uses `Produces` for Deliverable Production Record → produced Deliverable Revision, `Addresses` for Deliverable Production Record → Deliverable Expectation Revision, and `Relates To` with a `production_source` semantic role marker for Deliverable Production Record → Work Assignment Record. Requirement 23 uses `Addresses` for Work Assignment Record → Approved Plan Revision and `Relates To` with an `assignee` semantic role marker for Work Assignment Record → assignee Party. The domain model permits each but does not prescribe a single canonical convention for the assignee or production_source cases. The slice's design SHALL choose the canonical Relationship Types and semantic role markers and document the choice as input to a new backlog ADR (placeholder `ADR-HT-015`).
4. **G-14 — Execution Record lifecycle and corrections are undecided.** Requirements 23 through 29 model every execution Record as append-only and reject every modification or deletion. The constitution acknowledges supersession and correction paths for durable Records (Principle 5.6), but the user story map's Release 1C scope deliberately omits supersession of execution Records. The slice's design SHALL document the chosen append-only stance and the deferred supersession path as input to a new backlog ADR (placeholder `ADR-HT-016`).
5. **G-15 — Persistence representation for execution Records and produced Deliverable Revisions is undecided.** Requirement 22 requires distinct Resource and Revision identity columns for produced Deliverable Revisions and append-only identity for every execution Record. The interim representation chosen by Slice 1 (insert-only tables with append-only triggers, AD-WS-4) and extended by Slice 2 (Gap G-10) is the natural carrier, but the specific tables, columns, triggers, and Audit_Log atomic transactions for the seven new Record/Resource kinds have not been chosen. The slice's design SHALL choose the persistence representation, ensure it preserves the Slice 1 and Slice 2 append-only invariants, and document the choice as input to a new backlog ADR (placeholder `ADR-HT-017`).

## References

- Constitutional authority: [`00-project-constitution.md`](../../../documents/00-project-constitution.md) (in particular §2, §5.4, §5.5, §5.6, §5.7, §5.8, §5.9, §5.10, §5.12, §5.21, §5.22, §5.23, §5.25, §5.26, §5.29, §5.30; §6.6 Work and Portfolio Planning; §7 Bounded Contexts; §9 Core Domain Dictionary entries for Project, Activity, Deliverable, Party).
- Language and foundational model: [`01-domain-glossary.md`](../../../documents/01-domain-glossary.md), [`02-domain-model.md`](../../../documents/02-domain-model.md) (in particular §§3, 4, 5, 7.1, 7.2, 7.4, 7.5, 7.6, 8.2, 8.4, 8.5, 9, 9.1, 10.2, 10.5, 10.6, 10.9, 10.10, 19).
- Bounded contexts and cross-context invariants: [`03-context-map.md`](../../../documents/03-context-map.md) §§2.1, 2.2, 2.4, 2.5, 2.6, 2.8, 2.9, 3, 4.
- User roles: [`04-user-roles.md`](../../../documents/04-user-roles.md).
- Delivery model and user intent: [`07-user-story-map.md`](../../../documents/07-user-story-map.md) §§2, 3, 4 (Release 1C), 5.3, 5.4, 5.5, 9, 10.
- EARS requirements and acceptance style: [`06-ears-requirements.md`](../../../documents/06-ears-requirements.md), [`07-acceptance-criteria.md`](../../../documents/07-acceptance-criteria.md).
- Architecture decisions backlog: [`08-architecture-decisions.md`](../../../documents/08-architecture-decisions.md).
- Slice 1 spec (this slice's first prerequisite): [`../first-walking-slice/requirements.md`](../first-walking-slice/requirements.md), [`../first-walking-slice/design.md`](../first-walking-slice/design.md), [`../first-walking-slice/tasks.md`](../first-walking-slice/tasks.md).
- Slice 2 spec (this slice's second prerequisite): [`../second-walking-slice/requirements.md`](../second-walking-slice/requirements.md), [`../second-walking-slice/design.md`](../second-walking-slice/design.md), [`../second-walking-slice/tasks.md`](../second-walking-slice/tasks.md).
- Existing Slice 1 and Slice 2 implementation (not modified by this slice except via additive extension): `src/walking_slice/` — including `identity.py`, `audit.py`, `authorization.py`, `evidence.py`, `knowledge.py`, `trails.py`, `provenance.py`, `manifests.py`, `disclosure.py`, `interim_adr.py`, `projection.py`, `persistence.py`, `app.py`, `auth_middleware.py`, `clock.py`, `models.py`, and the Slice 2 Planning module(s).
