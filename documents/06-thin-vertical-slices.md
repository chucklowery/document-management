# Thin Vertical Slices

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines the initial set of thin, end-to-end vertical slices used to validate the domain model, context boundaries, authority rules, and cross-context invariants.

Each slice delivers one meaningful user outcome across the minimum necessary contexts and technical layers. The slices intentionally avoid broad platform completion before operational feedback exists.

## 2. Delivery Principles

1. Each slice begins with a real user problem and ends with a visible result.
2. Each slice crosses all required layers: user interaction, application behavior, domain rules, persistence, authorization, and explanation of provenance.
3. Each slice uses the smallest concept set that can validate the workflow.
4. A slice shall not introduce a generalized framework unless the workflow proves the need.
5. Every displayed conclusion or status must be explainable from source Resources, Revisions, Relationships, and Immutable Records.
6. Scope expands only after the preceding slice produces usable feedback.

## 3. Slice Sequence

The initial sequence is:

1. Evidence to Decision
2. Decision to Planned Work
3. Planned Work to Deliverable
4. Deliverable to Outcome Review
5. Learning to Adaptation
6. Reproducible Publication
7. Minimal Investment Traceability

Slices 1 through 5 form the first complete organizational learning loop. Slices 6 and 7 validate publication and financial traceability after the core loop works.

# Slice 1 — Evidence to Decision

## 4. User Outcome

A researcher or product leader can capture customer evidence, derive a Finding, record a Recommendation, and preserve an authorized Decision with complete provenance.

## 5. Primary Actor

- Researcher or Product Analyst

Supporting actors:

- Decision Maker
- Reviewer

## 6. Trigger

New customer interview evidence is available and may affect product or organizational direction.

## 7. Happy Path

1. The actor creates or imports an interview Document or Artifact.
2. The actor identifies one or more addressable Evidence regions.
3. The actor creates a Finding linked to exact Evidence Revisions or regions.
4. The actor records assumptions and confidence.
5. The actor creates a Recommendation derived from the Finding.
6. An authorized Decision Maker records a Decision accepting, rejecting, or deferring the Recommendation.
7. The system presents a navigable provenance path from Decision back to Recommendation, Finding, and source Evidence.

## 8. Minimum Concepts

- Document or Artifact
- Resource Revision
- Content Region
- Finding
- Recommendation
- Decision
- Supports
- Derived From
- Contradicts
- Supersedes
- Party

## 9. Minimum Rules

1. A Finding identifies exact supporting Evidence or is explicitly marked as a hypothesis.
2. A Recommendation remains distinct from a Decision.
3. A Decision records authority and rationale.
4. Competing Findings or Recommendations may coexist.
5. Historical Decision Revisions or superseding Decisions remain visible.

## 10. Acceptance Examples

### Example 1: Accepted recommendation

**Given** an interview Revision contains a statement describing repeated customer difficulty,

**When** an analyst creates a Finding linked to that exact statement, creates a Recommendation derived from the Finding, and an authorized leader accepts it,

**Then** the system records a Decision and can display the complete provenance path to the exact interview Revision and region.

### Example 2: Unsupported finding

**Given** an analyst creates a Finding without Evidence,

**When** the analyst attempts to finalize it,

**Then** the system requires the Finding to be marked as a hypothesis or linked to supporting Evidence.

### Example 3: Contrary evidence

**Given** an existing Finding is supported by one interview,

**When** another interview provides contrary Evidence,

**Then** both Evidence paths remain visible and the original Finding is not silently rewritten.

## 11. Deferred Capabilities

- automated thematic clustering;
- sophisticated confidence scoring;
- statistical analysis;
- bulk interview ingestion;
- advanced approval workflows;
- portfolio implications.

## 12. Learning Questions

- Can users distinguish Finding, Recommendation, and Decision?
- Is region-level provenance worth the authoring effort?
- Which decisions require formal authority?
- How often do users need competing interpretations?

# Slice 2 — Decision to Planned Work

## 13. User Outcome

A product or organizational leader can turn an authorized Decision into an Objective, Project, and initial Activity Plan without losing the reasoning that justified the work.

## 14. Primary Actor

- Product Leader or Program Leader

Supporting actors:

- Project Owner
- Reviewer

## 15. Trigger

An authorized Decision calls for coordinated work.

## 16. Happy Path

1. The actor selects an authorized Decision.
2. The actor creates or links an Objective describing the intended future condition.
3. The actor creates a Project that Addresses the Decision and Contributes To the Objective.
4. The actor records scope, owner, assumptions, expected Deliverables, and Intended Outcome reference.
5. The actor creates one Milestone and one Activity Plan.
6. The actor submits the Project plan for review.
7. An authorized reviewer approves the applicable Project Revision.
8. The system displays why the Project exists and which Decision and Evidence led to it.

## 17. Minimum Concepts

- Decision
- Objective
- Project
- Milestone
- Activity Plan
- Planning Assumption
- Intended Outcome
- Addresses
- Contributes To
- Governed By
- Depends On

## 18. Minimum Rules

1. An Objective describes a desired condition, not work.
2. A Project represents temporary governed work.
3. A Project identifies expected Deliverables and Intended Outcomes separately.
4. Approval applies to an exact Plan Revision.
5. Replanning creates a new Revision.

## 19. Acceptance Examples

### Example 1: Project created from decision

**Given** an authorized Decision exists,

**When** a leader creates a Project from it,

**Then** the Project retains a navigable link to the Decision and its Evidence lineage.

### Example 2: Deliverable confused with outcome

**Given** a Project defines “publish a new onboarding guide” as an expected Deliverable,

**When** the actor attempts to use the same statement as the Intended Outcome,

**Then** the system requires a distinct desired result such as “reduce onboarding completion time.”

### Example 3: Plan revision

**Given** an approved Project Revision exists,

**When** scope or success assumptions materially change,

**Then** the system creates a new Project Revision and preserves the approved historical plan.

## 20. Deferred Capabilities

- Programs and multi-project coordination;
- roadmap scenarios;
- capacity planning;
- budget allocation;
- complex dependency scheduling;
- configurable lifecycle engines.

## 21. Learning Questions

- Do users need Initiative and Program in the first workflow?
- Which plan changes require approval?
- Are expected Deliverables and Intended Outcomes understandable as separate concepts?
- Which assumptions must be made explicit?

# Slice 3 — Planned Work to Deliverable

## 22. User Outcome

A contributor can accept planned work, record consequential execution events, produce a Deliverable, and close the work without rewriting the original plan.

## 23. Primary Actor

- Contributor

Supporting actors:

- Project Owner
- Reviewer or Approver

## 24. Trigger

An approved Activity Plan is ready for execution.

## 25. Happy Path

1. A Project Owner creates a Work Assignment referencing an exact Activity Plan Revision.
2. A contributor accepts the Assignment.
3. The contributor records Work Started.
4. The contributor records one or more material Work Events.
5. The contributor produces a Document or Artifact Revision as the Deliverable.
6. A Deliverable Production record links the exact output Revision to the work.
7. An authorized reviewer accepts the relevant Milestone or completion criteria.
8. A Completion Record closes the execution scope while preserving unresolved items.
9. The system derives current execution status from the immutable records.

## 26. Minimum Concepts

- Work Assignment
- Work Event
- Deliverable Production
- Milestone Acceptance
- Completion Record
- Document or Artifact Revision
- Performs
- Executes
- Produces
- Fulfills

## 27. Minimum Rules

1. Assignment does not prove work occurred.
2. Work Events are immutable.
3. The produced Deliverable identifies an exact Revision.
4. Completion does not rewrite the Activity Plan.
5. Completion and Deliverable production do not prove Outcome achievement.
6. Current status is explainable from source records.

## 28. Acceptance Examples

### Example 1: Traceable deliverable

**Given** a contributor is assigned an approved Activity Plan,

**When** the contributor records work and produces an Artifact Revision,

**Then** the system links that exact Revision to the Assignment, Activity Plan, Project, and producing Work Event.

### Example 2: Reopened work

**Given** a Completion Record exists,

**When** additional work becomes necessary,

**Then** the system creates a new Work Event or execution cycle and retains the original Completion Record.

### Example 3: Blocked work

**Given** work cannot continue because of an unresolved dependency,

**When** a Blockage Observation is recorded,

**Then** the current status may project as Blocked while preserving the underlying observation and dependency.

## 29. Deferred Capabilities

- time entry and cost calculation;
- complex delegation;
- automated workflow engines;
- recurring operational work;
- advanced risk management;
- external issue-tracker synchronization.

## 30. Learning Questions

- Which Work Events provide real value?
- Is an Execution Case needed in the initial workflow?
- Can users understand projected status rather than mutable status?
- Which completion and acceptance actions require separate authorities?

# Slice 4 — Deliverable to Outcome Review

## 31. User Outcome

An outcome owner can define a measure, record an observation, compare actual results with the Intended Outcome, and issue an evidence-backed Outcome Review.

## 32. Primary Actor

- Outcome Owner or Analyst

Supporting actors:

- Project Owner
- Decision Maker

## 33. Trigger

The observation window for an Intended Outcome has begun or concluded and relevant data is available.

## 34. Happy Path

1. The actor selects an Intended Outcome and its Success Condition.
2. The actor creates or selects a Measurement Definition.
3. The actor records or imports a Measurement Record.
4. The actor creates an Observed Outcome supported by the Measurement Record.
5. The actor records attribution assumptions and competing explanations.
6. An authorized reviewer creates an Outcome Review.
7. The review records Achieved, Partially Achieved, Not Achieved, Inconclusive, or Not Yet Observable.
8. The system displays the chain from Intended Outcome through work and Deliverable to measurements and review.

## 35. Minimum Concepts

- Intended Outcome
- Success Condition
- Measurement Definition
- Measurement Record
- Observed Outcome
- Attribution Assumption
- Outcome Review
- Measures
- Indicates
- Evaluates
- Contributes To

## 36. Minimum Rules

1. A Measurement Record identifies the exact Measurement Definition Revision.
2. Measurements remain distinct from interpretations.
3. Correlation is not presented as proven causation.
4. Outcome Reviews evaluate exact Success Conditions.
5. Competing explanations may coexist.
6. Outcome status is derived from Evidence and reviews.

## 37. Acceptance Examples

### Example 1: Outcome achieved

**Given** an Intended Outcome targets a reduction in onboarding completion time,

**When** measurements during the observation window satisfy the approved Success Condition,

**Then** an authorized reviewer may record an Achieved Outcome Review with links to the exact measurements and criteria.

### Example 2: Deliverable without outcome

**Given** the planned onboarding guide was delivered,

**When** no valid Measurement Record exists,

**Then** the system does not infer that the Intended Outcome was achieved.

### Example 3: Inconclusive attribution

**Given** the target measure improved while several external factors changed,

**When** the reviewer cannot reasonably attribute the change to the Project,

**Then** the review may record the Outcome as achieved while marking attribution as inconclusive or low confidence.

## 38. Deferred Capabilities

- causal inference engines;
- advanced statistical significance;
- real-time dashboards;
- automated anomaly detection;
- composite outcome scoring;
- portfolio-level outcome aggregation.

## 39. Learning Questions

- Can users distinguish Measurement Record, Observed Outcome, and Outcome Review?
- What minimum attribution language is useful without becoming burdensome?
- Who owns Success Conditions?
- How often are outcomes not yet observable when work completes?

# Slice 5 — Learning to Adaptation

## 40. User Outcome

A leader can turn an Outcome Review into a Learning Record and explicitly revise or supersede a prior assumption, Decision, Objective, or Plan.

## 41. Primary Actor

- Product or Organizational Leader

Supporting actors:

- Analyst
- Decision Maker
- Project Owner

## 42. Trigger

An Outcome Review or contrary Evidence suggests that prior understanding or plans should change.

## 43. Happy Path

1. The actor selects an Outcome Review.
2. The actor creates a Learning Record that identifies the lesson and supporting Evidence.
3. The actor links the learning to affected assumptions, Findings, Decisions, Objectives, and Projects.
4. An authorized Decision Maker records a new Decision.
5. The system creates or links revised planning Resources.
6. Prior Decisions and Plans remain visible and are explicitly superseded where appropriate.
7. The system displays the closed learning loop from original Evidence to adaptation.

## 44. Minimum Concepts

- Outcome Review
- Learning Record
- Finding
- Planning Assumption
- Decision
- Objective
- Project Revision
- Challenges
- Informs
- Supersedes

## 45. Minimum Rules

1. Learning does not automatically change authoritative Resources.
2. Adoption occurs through an explicit Decision or new Revision.
3. Challenged assumptions and Decisions remain historically visible.
4. New plans retain provenance to the learning and Outcome Review that motivated them.

## 46. Acceptance Examples

### Example 1: Failed assumption

**Given** an Outcome Review contradicts a Planning Assumption,

**When** a leader adopts the learning,

**Then** the system records a new Decision and Project Revision while preserving the original assumption and plan.

### Example 2: Learning not adopted

**Given** a Learning Record exists,

**When** no authorized Decision adopts it,

**Then** the learning remains visible but does not alter the current approved plan.

## 47. Deferred Capabilities

- automated plan recommendations;
- autonomous strategic decisions;
- organization-wide policy propagation;
- learning maturity scores;
- AI-generated decisions without human authority.

## 48. Learning Questions

- Which forms of learning require formal adoption?
- How should unresolved disagreement be represented?
- When should a new Decision supersede rather than amend a prior one?
- Can users navigate the complete loop without information overload?

# Slice 6 — Reproducible Publication

## 49. User Outcome

An editor can assemble approved source materials into a reproducible Published Version and later explain exactly which inputs produced it.

## 50. Minimum Flow

1. Select a Main Document Revision.
2. Resolve Live, Approval-Controlled, and Pinned References.
3. Validate required metadata, authorization, approvals, and dependency cycles.
4. Produce an Assembled Document and Resolution Manifest.
5. Create an immutable Published Version.
6. Produce one Rendered Output.
7. Reproduce the Published Version from the recorded inputs.

## 51. Key Validation

- exact Revision selection;
- deterministic assembly;
- immutable publication;
- provenance to source;
- generated output as role rather than Resource Kind.

This slice may be delivered independently of Slices 1 through 5, but it should reuse the Shared Foundation.

# Slice 7 — Minimal Investment Traceability

## 52. User Outcome

A portfolio leader can see planned effort and one authoritative or imported actual-cost value for a Project and trace the calculation to its source.

## 53. Minimum Flow

1. Record planned effort on an Activity Plan.
2. Record or import one Time Entry.
3. Associate an effective Rate reference where authorized.
4. calculate a Project cost Projection.
5. Display source Time Entry, Rate, authority, temporal boundary, and calculation rule.
6. Compare planned and actual values without rewriting either.

## 54. Constraints

This slice intentionally excludes:

- general ledger behavior;
- payroll;
- invoice management;
- complex capitalization;
- multi-currency accounting;
- enterprise budgeting;
- autonomous allocation decisions.

It validates only that financial Projections preserve authority, provenance, and temporal semantics.

# Delivery Readiness

## 55. Slice Entry Criteria

A slice is ready to begin when:

- its primary actor and user outcome are explicit;
- owning contexts are identified;
- required cross-context contracts are known;
- minimum invariants are documented;
- deferred capabilities are explicit;
- acceptance examples can be demonstrated manually.

## 56. Slice Exit Criteria

A slice is complete when:

- a user can complete the workflow end to end;
- authoritative and derived information are distinguishable;
- history and provenance are navigable;
- permission failures are safe and understandable;
- the workflow satisfies its acceptance examples;
- user feedback and unresolved domain questions are recorded;
- unnecessary abstractions identified during delivery are removed or deferred.

## 57. Recommended Initial Release Boundary

The first release candidate should include Slices 1 through 4 at minimum:

```text
Evidence
→ Decision
→ Planned Work
→ Execution
→ Deliverable
→ Measurement
→ Outcome Review
```

Slice 5 closes the adaptation loop and should follow immediately when the initial review workflow is usable.

Publication and minimal investment traceability should remain separate release tracks unless an early customer workflow requires them.

## 58. Open Questions

1. Which customer or internal workflow will serve as the first real pilot?
2. Which slice provides the fastest meaningful feedback with available users and data?
3. Which roles may be combined for the first release?
4. Which integrations are essential rather than convenient?
5. What is the minimum usable interface for navigating provenance?
6. Which acceptance examples should become executable specifications?
7. What evidence will determine whether each slice should be expanded, revised, or abandoned?
