# Domain Scenarios

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document validates the domain models and requirements through complete scenarios that cross bounded contexts.

A scenario identifies:

- actors and authorities;
- Resources and Revisions created;
- Immutable Records created;
- Relationships created;
- Projections produced;
- invariants exercised;
- relevant User Stories and Requirements.

The scenarios are analytical narratives. They are not user-interface designs.

# Scenario 1 — Customer Evidence to Organizational Learning

## 2. Business Situation

Several customers report that onboarding takes too long and requires repeated support intervention. Product leadership wants to understand the problem, decide whether to act, deliver an improvement, measure the result, and adapt based on evidence.

## 3. Actors

- Researcher
- Analyst
- Decision Maker
- Objective Owner
- Project Owner
- Plan Approver
- Assignment Authority
- Contributor
- Milestone Acceptance Authority
- Completion Authority
- Measurement Designer
- Measurement Recorder
- Outcome Analyst
- Outcome Reviewer
- Learning Author
- Adaptation Decision Maker

A small pilot may assign several roles to the same Party while preserving the authority exercised in each action.

## 4. Stage A — Capture Evidence

### Actions

1. The Researcher imports three customer interview transcripts.
2. Each transcript becomes a Document Resource with an immutable Document Revision.
3. The external interview source, consent constraints, sensitivity classification, and collection context are recorded.
4. The Researcher identifies exact Content Regions describing onboarding difficulty.

### Resources and Revisions

- 3 Document Resources
- 3 Document Revisions
- multiple Content Regions and Region Occurrences

### Relationships

- Imported From external interview source
- classification and governance references

### Requirements exercised

- REQ-SF-001
- REQ-SF-002
- REQ-SF-007
- REQ-KP-001 through REQ-KP-004
- REQ-IG-004

### Expected result

An authorized user can navigate to the exact interview Revision and region. Unauthorized users cannot infer restricted content through search, counts, or graph traversal.

## 5. Stage B — Interpret Evidence

### Actions

1. The Analyst creates a Finding: “New administrators cannot identify the minimum steps required to complete onboarding.”
2. The Finding references exact regions in all three transcripts.
3. The Analyst records medium confidence and an assumption that the sample represents the target customer population.
4. A fourth interview contains contrary Evidence indicating that some experienced administrators complete onboarding easily.
5. The contrary Evidence is linked without changing the original Finding.
6. The Analyst creates a Recommendation: “Create a guided onboarding checklist with role-specific steps.”

### Resources and Revisions

- Finding Resource and Revision
- Recommendation Resource and Revision
- Planning or analytical assumption

### Relationships

- Supports from interview regions to Finding
- Contradicts from fourth interview region to Finding
- Derived From from Recommendation to Finding

### Requirements exercised

- REQ-KP-005 through REQ-KP-007
- REQ-SF-004
- REQ-SF-005

### Expected result

Supporting and contrary Evidence coexist. The Recommendation remains distinguishable from the Finding and from any later Decision.

## 6. Stage C — Make a Decision

### Actions

1. The Decision Maker reviews the Recommendation and provenance.
2. The Decision Maker accepts a limited pilot rather than a full rollout.
3. The Decision records rationale, scope, authority, date, acknowledged uncertainty, and review condition.

### Immutable Records

- Decision record

### Relationships

- Decision addresses Recommendation
- Decision derived from Findings and Evidence
- Decision governed by authority assignment

### Requirements exercised

- REQ-KP-008 through REQ-KP-010
- REQ-IG-003
- REQ-IG-005

### Expected result

The Decision is immutable and navigable to the Recommendation, Findings, supporting Evidence, and contrary Evidence.

## 7. Stage D — Plan Work

### Actions

1. The Objective Owner creates an Objective: “Reduce the time required for a new administrator to complete onboarding successfully.”
2. The Outcome Owner defines an Intended Outcome with a target reduction from 10 days to 6 days within a 60-day observation window.
3. The Project Owner creates a Project for the guided checklist pilot.
4. The Project records the accepted Decision, Objective, expected Deliverable, Intended Outcome, Planning Assumption, and owner.
5. One Milestone and one Activity Plan are created.
6. The Plan Approver approves the exact Project Revision.

### Resources and Revisions

- Objective Resource and Revision
- Intended Outcome Resource and Revision
- Success Condition Resource and Revision
- Project Resource and Revision
- Planning Assumption Resource and Revision
- Milestone
- Activity Plan

### Immutable Records

- Plan approval record

### Relationships

- Objective derived from or governed by Decision
- Project Addresses Decision
- Project Contributes To Objective and Intended Outcome
- Project Contains Milestone and Activity Plan
- Plan influenced by Planning Assumption

### Requirements exercised

- REQ-WP-001 through REQ-WP-014

### Expected result

The Project explains why it exists, what it will produce, and what result is expected. Deliverable and Intended Outcome remain distinct.

## 8. Stage E — Execute Work

### Actions

1. The Assignment Authority assigns the Activity Plan to a Contributor.
2. The Contributor accepts the Assignment.
3. The Contributor records Work Started.
4. A dependency on policy review blocks progress; a Blockage Observation is recorded.
5. The dependency is resolved through a later record.
6. The Contributor creates a checklist Document Revision.
7. A Deliverable Production record links the exact checklist Revision to the work.
8. The Milestone Acceptance Authority accepts the Milestone against the approved criteria.
9. The Completion Authority records completion with one unresolved follow-up item.

### Continuing Resources

- Work Assignment Resource and Revision
- optional Execution Case
- checklist Document Resource and Revision

### Immutable Records

- Assignment acceptance
- Work Started event
- Blockage Observation
- blockage resolution record
- Deliverable Production
- Milestone Acceptance
- Completion Record

### Projections

- current execution status

### Relationships

- Assignment Executes Activity Plan Revision
- Contributor Performs Assignment
- Execution Produces checklist Revision
- checklist Fulfills specified acceptance condition

### Requirements exercised

- REQ-WE-001 through REQ-WE-014
- REQ-SF-008 through REQ-SF-010

### Expected result

The current status is explainable from source events. Completion does not alter the historical plan and does not imply Outcome achievement.

## 9. Stage F — Measure Results

### Actions

1. The Measurement Designer defines onboarding completion time and its calculation rule.
2. The Measurement Recorder imports completion data for the pilot population.
3. Each Measurement Record preserves source, authority, observation time, recorded time, and definition Revision.
4. The Outcome Analyst creates an Observed Outcome: median completion time declined from 10 to 7 days.
5. The analyst records that a concurrent support-staffing increase may also have contributed.

### Resources and Revisions

- Measurement Definition Resource and Revision
- Attribution Assumption Resource and Revision

### Immutable Records

- Measurement Records
- Observed Outcome

### Relationships

- Measurement Records Measure onboarding completion time
- Measurement Records Indicate Observed Outcome
- checklist Project Contributes To Observed Outcome
- staffing increase also Contributes To Observed Outcome

### Requirements exercised

- REQ-OM-001 through REQ-OM-009
- REQ-EX-001 through REQ-EX-003

### Expected result

The observed improvement is preserved without overstating causation. Measurement and interpretation remain distinct.

## 10. Stage G — Review Outcome

### Actions

1. The Outcome Reviewer compares the Observed Outcome with the exact Intended Outcome Revision and Success Condition.
2. The target of 6 days was not reached, but substantial improvement occurred.
3. The reviewer issues a Partially Achieved Outcome Review.
4. Attribution is recorded as inconclusive because of the staffing change.
5. The review records unresolved questions and a recommendation for another observation period.

### Immutable Records

- Outcome Review

### Relationships

- Outcome Review Evaluates Intended Outcome Revision
- Outcome Review considers Measurement Records and Observed Outcome
- Outcome Review Supports partial improvement conclusion

### Requirements exercised

- REQ-OM-010 through REQ-OM-014

### Expected result

An authorized user can navigate from the Outcome Review back through measurements, execution, plans, Decision, Findings, and exact interview Evidence.

## 11. Stage H — Learn and Adapt

### Actions

1. The Learning Author records: “Role-specific guidance appears useful, but onboarding delay also depends materially on support availability.”
2. The Learning Record challenges the original assumption that documentation was the dominant cause.
3. The Adaptation Decision Maker accepts a follow-up experiment combining checklist improvements with scheduled support coverage.
4. The Project Owner creates a new Project Revision linked to the Learning Record and Adaptation Decision.
5. The earlier Plan, Decision, assumption, and Outcome Review remain visible.

### Resources and Revisions

- Learning Record
- new Project Revision
- revised or additional Planning Assumption

### Immutable Records

- Adaptation Decision
- new Plan approval if required

### Relationships

- Learning Record derived from Outcome Review
- Learning Record Challenges original assumption
- Adaptation Decision informed by Learning Record
- new Project Revision governed by Adaptation Decision
- new Project Revision Supersedes prior Project Revision for future execution

### Requirements exercised

- REQ-OM-015 through REQ-OM-019
- REQ-WP-012 through REQ-WP-014

### Expected result

The organizational learning loop closes without rewriting any historical record.

# Scenario 2 — Unsupported Finding

## 12. Situation

An Analyst creates a Finding based on personal intuition without linking Evidence.

## 13. Expected Behavior

1. The system identifies that the Finding has no supporting Evidence.
2. Before finalization, the Analyst must either:
   - link supporting Evidence; or
   - mark the Finding explicitly as a hypothesis.
3. A hypothesis remains usable for exploration but is distinguishable from an Evidence-supported Finding.

## 14. Requirements exercised

- REQ-KP-005
- REQ-TQ-005

## 15. Failure prevented

The system does not allow unsupported interpretation to appear indistinguishable from Evidence-backed knowledge.

# Scenario 3 — Unauthorized Decision Attempt

## 16. Situation

A Contributor can view a Recommendation but lacks decision authority.

## 17. Expected Behavior

1. The Contributor attempts to accept the Recommendation.
2. The system evaluates contextual role, scope, and effective period.
3. The action is denied.
4. No Decision record is created.
5. An audit record preserves actor, action, target, time, and denial reason according to policy.
6. The Recommendation and acknowledged work remain unchanged.

## 18. Requirements exercised

- REQ-KP-009
- REQ-IG-001 through REQ-IG-005

## 19. Failure prevented

View access does not silently become authority to make consequential Decisions.

# Scenario 4 — Correction of a Measurement Record

## 20. Situation

A Measurement Recorder discovers that a value was imported using the wrong unit.

## 21. Expected Behavior

1. The original Measurement Record remains immutable.
2. An authorized correction creates a correcting record referencing the original.
3. The corrected unit and value are recorded with rationale and time.
4. Current Outcome Projections are recalculated.
5. Prior Outcome Reviews remain unchanged unless a new review is issued.
6. Historical reconstruction shows the original record, correction, and resulting Projection changes.

## 22. Requirements exercised

- REQ-SF-008 through REQ-SF-010
- REQ-OM-004
- REQ-TQ-005

## 23. Failure prevented

A correction does not destroy evidence of what was originally recorded or silently rewrite an earlier review.

# Scenario 5 — Late-Arriving Evidence

## 24. Situation

A customer interview conducted during the original research period is imported after the Project is completed.

## 25. Expected Behavior

1. The interview records both its original observation time and later recorded time.
2. Its Evidence may support or contradict the original Finding.
3. The original Decision and Plan remain unchanged.
4. Current knowledge and Outcome Projections may change.
5. An Analyst may create a new Finding or a superseding interpretation.
6. Adaptation requires an explicit new Decision.

## 26. Requirements exercised

- REQ-SF-006
- REQ-KP-002 through REQ-KP-006
- REQ-SF-010
- REQ-OM-016 through REQ-OM-019

## 27. Failure prevented

Late evidence influences current understanding without falsifying what was known or decided earlier.

# Scenario 6 — External System Remains Authoritative

## 28. Situation

Project work items are synchronized from an external issue-tracking system that remains authoritative for assignment and status.

## 29. Expected Behavior

1. Local records preserve external identity and authority designation.
2. Local users may annotate or relate the work item according to policy.
3. Direct local mutation of externally authoritative assignment or status is blocked or represented as a proposal.
4. Synchronization time and stale state are visible.
5. A synchronization failure does not silently convert the local copy into authoritative truth.

## 30. Requirements exercised

- REQ-SF-007
- REQ-EX-001 through REQ-EX-004
- REQ-IG-003

## 31. Failure prevented

Integration does not erase authority boundaries or create conflicting silent sources of truth.

# Scenario 7 — Deliverable Completed, Outcome Not Yet Observable

## 32. Situation

A checklist is delivered and accepted, but the defined 60-day observation window has not elapsed.

## 33. Expected Behavior

1. Deliverable Production, Milestone Acceptance, and Completion Records are preserved.
2. The Project may project as completed.
3. The Intended Outcome remains Not Yet Observable.
4. The system does not mark the Outcome Achieved.
5. An Outcome Review may record Not Yet Observable if a formal review is required.

## 34. Requirements exercised

- REQ-WE-009
- REQ-WE-012
- REQ-OM-011
- REQ-OM-012

## 35. Failure prevented

The system does not confuse work completion with value realization.

# Scenario Validation Matrix

## 36. Context Coverage

| Context | Scenario Coverage |
|---|---|
| Shared Graph Foundation | 1–7 |
| Document Authoring and Composition | 1, 2, 5 |
| Knowledge and Provenance | 1, 2, 3, 5 |
| Work Planning | 1, 5, 7 |
| Work Execution | 1, 6, 7 |
| Outcome Measurement and Learning | 1, 4, 5, 7 |
| Identity, Access, and Governance | 1, 3, 6 |
| Integration and External Systems | 1, 4, 5, 6 |
| Publication | not yet exercised by these scenarios |
| Portfolio and Financial Intelligence | not yet exercised by these scenarios |

## 37. Invariant Coverage

The scenarios collectively exercise:

- stable identity;
- immutable history;
- explicit authority;
- plan and execution separation;
- output and Outcome separation;
- measurement and interpretation separation;
- correlation and causation separation;
- explicit provenance;
- explainable Projections;
- authorization without leakage;
- correction without deletion;
- external authority preservation;
- adaptation through explicit Decisions.

# Implementation Guidance

## 38. First Executable Scenario

Scenario 1 should be implemented incrementally rather than all at once.

Recommended increments:

1. Evidence to Finding
2. Finding to Decision
3. Decision to Project
4. Project to Deliverable
5. Deliverable to Measurement
6. Measurement to Outcome Review
7. Outcome Review to Adaptation

Each increment should retain end-to-end traceability to all preceding increments.

## 39. Scenario Exit Criteria

A scenario is validated when:

- domain users recognize the language and workflow;
- all consequential authority is explicit;
- history can be reconstructed;
- provenance can be navigated;
- failures are visible and safe;
- no Projection is mistaken for source truth;
- acceptance specifications cover the principal happy and failure paths;
- unnecessary abstractions are removed or deferred.

## 40. Open Questions

1. Which real customer workflow should replace the hypothetical onboarding example?
2. Which roles will be combined in the first pilot?
3. What minimum provenance visualization is needed?
4. Which scenario steps need external integration immediately?
5. Which scenario data is sensitive or regulated?
6. Which requirements need quantitative performance or availability criteria?
7. Which scenarios should become executable acceptance specifications first?
