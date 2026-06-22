# Acceptance Specifications

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines the initial acceptance specifications derived from the user stories, EARS requirements, and domain scenarios.

The specifications use Given/When/Then language so they can be reviewed by domain experts and later automated where valuable.

Each specification identifies:

- Feature ID;
- source User Stories;
- source Requirements;
- affected bounded contexts;
- principal invariants.

These specifications describe observable behavior. They do not prescribe user-interface layout, storage technology, service boundaries, or test framework.

## 2. Conventions

### Identities

Named Resources, Revisions, Relationships, and immutable records in examples are assumed to have stable identities.

### Authorization

Unless a scenario explicitly tests denial, the acting Party is assumed to hold the required contextual role and authority.

### Historical behavior

“Remains visible” means historically addressable to an authorized user and not silently overwritten or deleted.

### Provenance

“Traceable” means the system exposes navigable source identities, exact Revisions where required, Relationship semantics, and applicable authority information.

# Feature AS-001 — Capture Evidence and Create a Finding

**Stories:** US-001, US-002, US-003, US-004

**Requirements:** REQ-KP-001 through REQ-KP-006; REQ-SF-001 through REQ-SF-005

**Contexts:** Document Authoring and Composition; Knowledge and Provenance; Shared Graph Foundation

**Invariants:** stable identity; immutable Revisions; exact provenance; competing interpretations may coexist.

## Scenario AS-001.1 — Capture an interview transcript

**Given** a Researcher has an interview transcript and applicable source context,

**When** the Researcher records the transcript as managed source material,

**Then** the system assigns a stable Document Resource Identity,

**And** creates an immutable Document Revision,

**And** records source, collection context, and authority designation,

**And** preserves applicable sensitivity and consent information.

## Scenario AS-001.2 — Identify an exact Evidence region

**Given** a managed Document Revision contains a statement relevant to an investigation,

**When** the Researcher identifies that statement as an Evidence region,

**Then** the system assigns or preserves a stable Region Identity,

**And** records the exact Region Occurrence in the Document Revision,

**And** permits an authorized user to navigate to the exact text.

## Scenario AS-001.3 — Historical citation survives later revision

**Given** a Finding cites a Region Occurrence in Document Revision 1,

**And** Document Revision 2 changes or removes the cited text,

**When** an authorized user follows the original citation,

**Then** the system resolves the citation to Document Revision 1 and the historical Region Occurrence,

**And** does not redirect it to unrelated content in Revision 2.

## Scenario AS-001.4 — Evidence-backed Finding

**Given** one or more exact Evidence references exist,

**When** an Analyst creates and finalizes a Finding using those references,

**Then** the system records the Finding and its supporting Relationships,

**And** preserves the exact source Revisions or Region Occurrences,

**And** records assumptions and confidence when provided.

## Scenario AS-001.5 — Unsupported Finding must be marked as hypothesis

**Given** an Analyst creates a Finding with no supporting Evidence,

**When** the Analyst attempts to finalize it,

**Then** the system shall require supporting Evidence or an explicit hypothesis designation,

**And** shall not present the unsupported Finding as Evidence-backed knowledge.

## Scenario AS-001.6 — Contrary Evidence does not rewrite the Finding

**Given** a Finding has supporting Evidence,

**And** another source provides contrary Evidence,

**When** the Analyst links the contrary Evidence,

**Then** the system preserves both supporting and contrary Evidence,

**And** leaves the original Finding historically unchanged,

**And** permits a later interpretation to supersede rather than erase it.

# Feature AS-002 — Recommendation and Authorized Decision

**Stories:** US-005, US-006, US-007

**Requirements:** REQ-KP-007 through REQ-KP-010; REQ-IG-003 through REQ-IG-005

**Contexts:** Knowledge and Provenance; Identity, Access, and Governance

**Invariants:** Recommendation is distinct from Decision; authority is explicit; provenance is navigable.

## Scenario AS-002.1 — Create Recommendation from Findings

**Given** one or more Findings exist,

**When** an Analyst creates a Recommendation derived from them,

**Then** the system records the Recommendation as distinct from the Findings,

**And** records Derived From Relationships to all material Findings and Evidence,

**And** permits assumptions, confidence, and rationale to be recorded.

## Scenario AS-002.2 — Authorized Decision Maker accepts Recommendation

**Given** a Recommendation exists,

**And** a Party has applicable Decision Maker authority,

**When** the Party accepts the Recommendation,

**Then** the system creates an immutable Decision record,

**And** records outcome, rationale, authority, scope, and time,

**And** links the Decision to the Recommendation and its provenance.

## Scenario AS-002.3 — Unauthorized Decision attempt is denied

**Given** a Party may view a Recommendation but lacks decision authority,

**When** the Party attempts to accept the Recommendation,

**Then** the system denies the action,

**And** creates no Decision record,

**And** preserves an audit record containing actor, attempted action, target, time, and denial reason,

**And** does not modify the Recommendation or any acknowledged work.

## Scenario AS-002.4 — Decision provenance is navigable

**Given** a Decision exists,

**When** an authorized reviewer opens its provenance,

**Then** the system exposes navigable links to the Recommendation, Findings, supporting and contrary Evidence, and exact source Revisions or regions,

**And** preserves external source and authority information,

**And** hides or safely redacts restricted nodes according to policy.

## Scenario AS-002.5 — Later Decision supersedes without erasing history

**Given** an earlier Decision exists,

**When** an authorized Decision Maker records a later Decision that supersedes it for a defined scope,

**Then** the system preserves both Decisions,

**And** records the supersession scope and rationale,

**And** identifies which Decision is applicable for the stated scope and time.

# Feature AS-003 — Decision to Approved Project Plan

**Stories:** US-008 through US-013

**Requirements:** REQ-WP-001 through REQ-WP-014

**Contexts:** Work Planning; Knowledge and Provenance; Outcome Measurement and Learning; Governance

**Invariants:** Objective is distinct from work; Deliverable is distinct from Outcome; approval is Revision-specific.

## Scenario AS-003.1 — Create Objective from Decision

**Given** an authorized Decision exists,

**When** an Objective Owner creates an Objective from that Decision,

**Then** the system records the Relationship between the Objective and Decision,

**And** records the desired future condition,

**And** does not treat the Objective as an Activity Plan.

## Scenario AS-003.2 — Reject activity-only Objective statement

**Given** an Objective Owner enters “Build an onboarding checklist” as the Objective,

**When** the Objective is submitted,

**Then** the system requires a desired future condition or prompts the user to classify the statement as planned work,

**And** does not silently treat the Deliverable as the Objective.

## Scenario AS-003.3 — Define Intended Outcome and Success Conditions

**Given** an Objective exists,

**When** an Outcome Owner defines an Intended Outcome,

**Then** the system records scope, owner, observation window, baseline where applicable, and Success Conditions,

**And** links the Intended Outcome to the Objective,

**And** preserves Deliverables as separate concepts.

## Scenario AS-003.4 — Reject Deliverable as sole Intended Outcome

**Given** a user enters “Publish the checklist” as the Intended Outcome,

**When** the user attempts to finalize it,

**Then** the system requires a distinct desired result or an explicit exception rationale,

**And** does not infer value realization from delivery alone.

## Scenario AS-003.5 — Create traceable Project plan

**Given** an authorized Decision, Objective, and Intended Outcome exist,

**When** a Project Owner creates a Project,

**Then** the system permits the Project to reference each governing object,

**And** records accountable owner, scope, expected Deliverables, assumptions, Milestones, and Activity Plans,

**And** preserves expected Deliverables and Intended Outcomes separately.

## Scenario AS-003.6 — Project approval requires exact Revision

**Given** Project Revision 1 is ready for approval,

**And** a Party has Plan Approver authority,

**When** the Party approves it,

**Then** the system records an immutable approval for Project Revision 1,

**And** records authority, outcome, rationale, conditions, and time.

## Scenario AS-003.7 — Approval does not carry to revised plan

**Given** Project Revision 1 is approved,

**When** a material scope, assumption, Success Condition, governance, or Deliverable change creates Project Revision 2,

**Then** the approval for Revision 1 remains historically visible,

**And** Revision 2 is not treated as approved unless an explicit policy permits and records that adoption.

## Scenario AS-003.8 — Planning Assumption is challenged

**Given** an approved Project Revision depends on a Planning Assumption,

**When** contrary Evidence or a Learning Record challenges that assumption,

**Then** the system preserves the original assumption,

**And** records the challenge Relationship,

**And** does not modify the approved Project Revision automatically.

# Feature AS-004 — Planned Work to Traceable Deliverable

**Stories:** US-014 through US-020

**Requirements:** REQ-WE-001 through REQ-WE-014

**Contexts:** Work Planning; Work Execution; Document Authoring and Composition

**Invariants:** assignment does not prove work; execution does not rewrite plan; output does not prove acceptance or Outcome.

## Scenario AS-004.1 — Assign exact Activity Plan Revision

**Given** an approved Activity Plan Revision exists,

**When** an Assignment Authority assigns it to a Contributor,

**Then** the system creates a Work Assignment referencing the exact Activity Plan Revision,

**And** records assignment authority, effective period, expected contribution, and assigned Party,

**And** does not infer that work has started.

## Scenario AS-004.2 — Contributor accepts Assignment

**Given** a Work Assignment exists for a Contributor,

**When** the Contributor accepts it,

**Then** the system records the response, actor, and time,

**And** preserves the Assignment independently of later Work Events.

## Scenario AS-004.3 — Record Work Started

**Given** a Contributor has accepted an Assignment,

**When** the Contributor records Work Started,

**Then** the system creates an immutable Work Event linked to the Assignment and governing Plan Revision,

**And** current status may project as In Progress according to policy.

## Scenario AS-004.4 — Correct Work Started time without rewriting history

**Given** a Work Started event contains an incorrect occurrence time,

**When** an authorized actor records a correction,

**Then** the system preserves the original event,

**And** creates a correcting record,

**And** recalculates current Projections where applicable.

## Scenario AS-004.5 — Record and resolve blockage

**Given** execution cannot continue because of a dependency,

**When** a Contributor records a Blockage Observation,

**Then** the system records affected work, condition, dependency, severity, actor, occurrence time, and Evidence,

**And** current status may project as Blocked.

**When** a later resolution record is created,

**Then** the original Blockage Observation remains visible,

**And** the current status is recalculated according to policy.

## Scenario AS-004.6 — Record exact Deliverable Revision

**Given** execution produces a Document or Artifact Revision,

**When** the Contributor records Deliverable Production,

**Then** the system records the exact output Resource Revision,

**And** records producing actor or agent, production time, governing work, and provenance,

**And** does not infer acceptance, completion, or Outcome achievement.

## Scenario AS-004.7 — Generated Deliverable retains provenance

**Given** a Deliverable is generated from source Revisions,

**When** Deliverable Production is recorded,

**Then** the system records the producing execution context and exact material source Revisions,

**And** treats Generated Output as a role rather than a Resource Kind.

## Scenario AS-004.8 — Accept Milestone against approved criteria

**Given** a Milestone and its approved Plan Revision exist,

**When** a Milestone Acceptance Authority evaluates it,

**Then** the system records the exact Milestone, Plan Revision, criteria, Evidence, authority, outcome, and time,

**And** preserves any later superseding acceptance separately.

## Scenario AS-004.9 — Complete work with unresolved items

**Given** execution has produced a Deliverable,

**When** a Completion Authority records completion with unresolved follow-up items,

**Then** the system records the applicable plan, completion outcome, authority, time, Deliverables, unresolved items, Evidence, and rationale,

**And** does not infer Intended Outcome achievement.

## Scenario AS-004.10 — Reopen completed work

**Given** a Completion Record exists,

**When** additional execution becomes necessary,

**Then** the system creates a new Work Event or execution cycle,

**And** preserves the original Completion Record,

**And** recalculates current status without deleting prior history.

# Feature AS-005 — Measurement and Outcome Review

**Stories:** US-021 through US-026

**Requirements:** REQ-OM-001 through REQ-OM-014

**Contexts:** Outcome Measurement and Learning; Work Execution; Work Planning; Integration

**Invariants:** measurement is distinct from interpretation; completion does not prove Outcome; correlation is not causation.

## Scenario AS-005.1 — Define a reproducible measure

**Given** an Intended Outcome exists,

**When** a Measurement Designer creates a Measurement Definition,

**Then** the system records subject, population, unit or scale, source, collection or calculation rule, temporal window, quality constraints, owner, and policy,

**And** assigns an immutable Revision to the definition.

## Scenario AS-005.2 — Material measurement-rule change creates new Revision

**Given** Measurement Definition Revision 1 exists,

**When** a material calculation, collection, inclusion, exclusion, aggregation, or interpretation rule changes,

**Then** the system creates Measurement Definition Revision 2,

**And** preserves Revision 1 for historical interpretation.

## Scenario AS-005.3 — Record imported Measurement with authority

**Given** an external system provides a measurement value,

**When** the Measurement Recorder imports it,

**Then** the system creates an immutable Measurement Record,

**And** records exact Measurement Definition Revision, subject, observation time, recorded time, value, unit, source, external identity, synchronization state, authority, and quality information.

## Scenario AS-005.4 — Correct a Measurement Record

**Given** an immutable Measurement Record contains a factual error,

**When** an authorized actor records a correction,

**Then** the system preserves the original record,

**And** creates a correcting record referencing it,

**And** recalculates current Projections,

**And** does not rewrite an earlier Outcome Review.

## Scenario AS-005.5 — Create Observed Outcome from measurements

**Given** one or more Measurement Records exist,

**When** an Outcome Analyst creates an Observed Outcome,

**Then** the system records the supporting measurements, observation window, interpretation, confidence, attribution assessment, competing explanations, and actor,

**And** preserves the measurements separately from the interpretation.

## Scenario AS-005.6 — Preserve competing interpretations

**Given** two Outcome Analysts interpret the same Measurement Records differently,

**When** each records an Observed Outcome,

**Then** the system preserves both interpretations and their supporting Evidence,

**And** does not silently select one as authoritative without an explicit review or Decision.

## Scenario AS-005.7 — Contribution does not become causation

**Given** an Observed Outcome improved after a Project delivered an output,

**And** external conditions also changed,

**When** the Outcome Analyst records a contribution claim,

**Then** the system records scope, rationale, confidence, and competing explanations,

**And** does not present the relationship as proven causation unless supported by explicit evidence and reasoning.

## Scenario AS-005.8 — Issue Partially Achieved Outcome Review

**Given** an Intended Outcome targets completion time of 6 days,

**And** valid measurements show completion time of 7 days,

**When** an authorized Outcome Reviewer evaluates the exact Intended Outcome Revision and Success Conditions,

**Then** the system permits a Partially Achieved assessment,

**And** records reviewed criteria, measurements, Observed Outcomes, authority, confidence, attribution findings, unresolved questions, and time.

## Scenario AS-005.9 — Completed Deliverable but Outcome not yet observable

**Given** a Deliverable is completed and accepted,

**And** the Outcome observation window has not elapsed,

**When** current Outcome status is evaluated,

**Then** the system does not infer Achieved,

**And** may project Not Yet Measurable or Awaiting Data,

**And** may permit an Outcome Review assessment of Not Yet Observable.

## Scenario AS-005.10 — Navigate Outcome provenance

**Given** an Outcome Review exists,

**When** an authorized Outcome Owner opens its provenance,

**Then** the system exposes navigable references to measurements, Observed Outcomes, Deliverables, execution records, plans, Decisions, Findings, and source Evidence where available,

**And** identifies unresolved, stale, or missing provenance links,

**And** does not leak restricted information.

# Feature AS-006 — Learning and Explicit Adaptation

**Stories:** US-027 through US-030

**Requirements:** REQ-OM-015 through REQ-OM-019; REQ-WP-012 through REQ-WP-014

**Contexts:** Outcome Measurement and Learning; Knowledge and Provenance; Work Planning

**Invariants:** learning is distinct from adoption; adaptation requires explicit authority; replanning preserves history.

## Scenario AS-006.1 — Create Learning Record from Outcome Review

**Given** an Outcome Review identifies a lesson or failed assumption,

**When** a Learning Author records the lesson,

**Then** the system preserves the lesson, supporting Evidence, affected assumptions or Decisions, confidence, unresolved questions, and proposed implications,

**And** does not modify any authoritative Resource automatically.

## Scenario AS-006.2 — Challenge an assumption without changing the plan

**Given** a Learning Record challenges a Planning Assumption,

**When** the challenge is recorded,

**Then** the system preserves both the Learning Record and Planning Assumption,

**And** records the challenge Relationship,

**And** leaves the current approved Plan unchanged until an authorized Decision occurs.

## Scenario AS-006.3 — Authorized adaptation Decision

**Given** a Learning Record proposes a change,

**And** a Party has Adaptation Decision Maker authority,

**When** the Party accepts the adaptation,

**Then** the system creates an immutable Decision recording outcome, rationale, authority, scope, time, and related Learning Record.

## Scenario AS-006.4 — Learning not adopted

**Given** a Learning Record exists,

**When** no authorized adaptation Decision accepts it,

**Then** the Learning Record remains visible,

**And** the current Decision, Objective, policy, and Plan remain unchanged.

## Scenario AS-006.5 — Revise Project from adaptation Decision

**Given** an accepted adaptation Decision exists,

**When** the Project Owner revises the Project,

**Then** the system creates a new Project Revision linked to the adaptation Decision and Learning Record,

**And** preserves earlier Project Revisions and approvals,

**And** requires new approval when policy requires it.

# Feature AS-007 — Contextual Authorization and Data Protection

**Stories:** US-031, US-032

**Requirements:** REQ-IG-001 through REQ-IG-008

**Contexts:** Identity, Access, and Governance; all bounded contexts

**Invariants:** role is contextual; view access is distinct from action authority; sensitive information does not leak.

## Scenario AS-007.1 — Assign contextual role

**Given** an authorized Resource Steward manages role assignments,

**When** the steward assigns a role to a Party,

**Then** the system records Party, role, scope, authority, effective period, and assigning authority,

**And** preserves the assignment history.

## Scenario AS-007.2 — Expired role cannot authorize action

**Given** a Party's role assignment has expired,

**When** the Party attempts an action requiring that role,

**Then** the system denies the action,

**And** records the denial according to audit policy.

## Scenario AS-007.3 — View permission does not imply approval authority

**Given** a Party may view a Project Revision,

**But** lacks Plan Approver authority,

**When** the Party attempts to approve the Revision,

**Then** the system denies the action,

**And** creates no approval record.

## Scenario AS-007.4 — Search does not leak restricted information

**Given** restricted Resources exist,

**And** a user lacks access,

**When** the user searches, browses counts, requests graph traversal, or asks for Projection explanation,

**Then** the system does not disclose restricted content, sensitive metadata, counts, identifiers, or inference-enabling errors beyond policy.

## Scenario AS-007.5 — Automated Agent acts within delegated scope

**Given** an Automated Agent has delegated authority for a defined action and scope,

**When** the agent performs the action,

**Then** the system records agent identity, delegated authority, relevant tool or model version, and provenance.

## Scenario AS-007.6 — Automated Agent exceeds scope

**Given** an Automated Agent lacks authority for a consequential action,

**When** it attempts that action,

**Then** the system denies the action,

**And** records the attempted action and denial,

**And** does not silently modify acknowledged work.

# Feature AS-008 — External Authority and Synchronization

**Stories:** US-035

**Requirements:** REQ-EX-001 through REQ-EX-004

**Contexts:** Integration and External Systems; Shared Graph Foundation; consuming contexts

**Invariants:** synchronization does not transfer authority; stale state is visible; translation is explicit.

## Scenario AS-008.1 — Import externally authoritative work item

**Given** an external issue-tracking system is authoritative for a work item,

**When** the item is synchronized locally,

**Then** the system preserves external identifier, local identity, authority mapping, synchronization policy, and last successful synchronization time.

## Scenario AS-008.2 — Block local mutation of read-only authoritative field

**Given** an external system is authoritative for assignment and status,

**When** a local user attempts to modify either field,

**Then** the system blocks the mutation or records it as a local proposal according to policy,

**And** does not silently make the local system authoritative.

## Scenario AS-008.3 — Surface stale synchronization

**Given** synchronization has failed or exceeded its freshness policy,

**When** an authorized user views the local representation,

**Then** the system displays stale state, last successful synchronization time, and relevant failure information.

## Scenario AS-008.4 — Translate differing domain terms

**Given** an external system uses “completed” to mean technically closed,

**And** the local context distinguishes completion from Outcome achievement,

**When** the external status is imported,

**Then** the system applies an explicit translation contract,

**And** does not map “completed” to Outcome Achieved.

# Feature AS-009 — Explainable Projections

**Stories:** US-034

**Requirements:** REQ-SF-009, REQ-SF-010, REQ-TQ-005

**Contexts:** Shared Graph Foundation; Work Execution; Outcome Measurement; analytical contexts

**Invariants:** Projections are derived, reproducible, and do not replace source facts.

## Scenario AS-009.1 — Explain execution status

**Given** current execution status is displayed as Blocked,

**When** an authorized user requests an explanation,

**Then** the system identifies the governing plan, unresolved Blockage Observations, applicable policy, temporal boundary, and generated time.

## Scenario AS-009.2 — Explain Outcome status

**Given** current Outcome status is displayed as Partially Achieved,

**When** an authorized user requests an explanation,

**Then** the system identifies the Intended Outcome Revision, Success Conditions, Measurement Records, Outcome Reviews, Projection Definition, assumptions, and generated time.

## Scenario AS-009.3 — Late fact changes current Projection only

**Given** a late Measurement Record is added with an earlier observation time,

**When** the current Outcome Projection is recalculated,

**Then** the current Projection may change,

**And** the late record preserves its observation and recorded times,

**And** prior source records and historical reviews remain unchanged.

# Acceptance Coverage Matrix

## 3. Story Coverage

| Feature | Story Coverage |
|---|---|
| AS-001 | US-001 through US-004 |
| AS-002 | US-005 through US-007 |
| AS-003 | US-008 through US-013 |
| AS-004 | US-014 through US-020 |
| AS-005 | US-021 through US-026 |
| AS-006 | US-027 through US-030 |
| AS-007 | US-031, US-032 |
| AS-008 | US-035 |
| AS-009 | US-034 |

US-033 is exercised across AS-004.4 and AS-005.4. Cross-cutting correction behavior may later receive a dedicated automated feature.

## 4. Requirement Coverage

The current acceptance baseline covers:

- Shared Foundation requirements REQ-SF-001 through REQ-SF-010;
- Knowledge and Provenance requirements REQ-KP-001 through REQ-KP-010;
- Work Planning requirements REQ-WP-001 through REQ-WP-014;
- Work Execution requirements REQ-WE-001 through REQ-WE-014;
- Outcome Measurement requirements REQ-OM-001 through REQ-OM-019;
- Governance requirements REQ-IG-001 through REQ-IG-008;
- Integration requirements REQ-EX-001 through REQ-EX-004;
- explainability requirement REQ-TQ-005.

Traceability-process requirements REQ-TQ-001 through REQ-TQ-004 are verified through repository review and documentation checks rather than product behavior scenarios.

# Automation Priorities

## 5. Priority 1 — Walking Skeleton

Automate first:

1. AS-001.1 — Capture source Evidence
2. AS-001.4 — Evidence-backed Finding
3. AS-002.2 — Authorized Decision
4. AS-002.3 — Unauthorized Decision denial
5. AS-003.5 — Create traceable Project plan
6. AS-004.6 — Record exact Deliverable Revision
7. AS-005.9 — Completed Deliverable does not imply Outcome
8. AS-009.1 — Explain execution status

These scenarios validate identity, Revision history, Relationships, immutable records, authority, provenance, and Projection explanation.

## 6. Priority 2 — Complete Release 1 Loop

Automate next:

1. AS-003.6 and AS-003.7 — Revision-specific plan approval
2. AS-004.3 through AS-004.10 — execution lifecycle
3. AS-005.1 through AS-005.10 — measurement and Outcome review
4. AS-006.1 through AS-006.5 — learning and adaptation

## 7. Priority 3 — Integration and Advanced Governance

Automate after the core loop is usable:

- AS-007.4 — indirect data leakage protection
- AS-007.5 and AS-007.6 — Automated Agent governance
- AS-008.1 through AS-008.4 — external authority and translation
- AS-009.2 and AS-009.3 — Outcome Projection explanation and late facts

# Exit Criteria

## 8. Feature Acceptance

A Feature is accepted when:

1. all required scenarios pass;
2. authority failures are safe and auditable;
3. historical information remains reconstructable;
4. provenance is navigable;
5. derived information is explainable;
6. restricted information is not leaked;
7. domain reviewers confirm that the behavior matches the intended language and workflow;
8. any new domain concepts are reflected in the owning model or explicitly documented as local implementation concepts.

## 9. Release 1 Acceptance

Release 1 is acceptable when an authorized pilot user can complete the following chain:

```text
Source Evidence
→ Finding
→ Recommendation
→ Decision
→ Objective
→ Intended Outcome
→ Project Plan
→ Work Assignment
→ Work Event
→ Deliverable
→ Measurement
→ Observed Outcome
→ Outcome Review
```

and the system can:

- navigate provenance from Outcome Review back to exact source Evidence;
- distinguish authority at each consequential action;
- preserve historical plans and execution records;
- distinguish Deliverable completion from Outcome achievement;
- explain current execution and Outcome Projections;
- deny unauthorized actions without losing acknowledged work.

Learning and adaptation scenarios complete the next immediate increment if not included in the initial pilot.

# Open Questions

1. Which specification format and runner will be used for executable scenarios?
2. Which scenarios require API-level tests, domain-level tests, user-interface tests, or contract tests?
3. Which example data should become the canonical pilot fixture?
4. Which scenarios require quantitative performance or availability expectations?
5. How will authorization and redaction be tested across search and graph traversal?
6. Which cross-context interactions require consumer-driven contract tests?
7. Which scenarios should be divided further to isolate failure causes?
