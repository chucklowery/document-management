# User Story Map

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document organizes user activity across the initial vertical slices.

The story map preserves the end-to-end workflow while allowing delivery to proceed in small, testable increments.

## 2. Backbone Activities

The initial product backbone is:

1. Capture Evidence
2. Interpret Evidence
3. Make a Decision
4. Plan Work
5. Execute Work
6. Produce a Deliverable
7. Measure Results
8. Review Outcomes
9. Learn and Adapt
10. Publish or Share

## 3. Story Map

| Backbone Activity | User Tasks | Primary Roles | Initial Slice |
|---|---|---|---|
| Capture Evidence | Create or import source material; identify exact Evidence regions; record source context and sensitivity | Researcher, Evidence Contributor | Slice 1 |
| Interpret Evidence | Create Finding; link support and contradiction; record assumptions and confidence; create Recommendation | Analyst, Knowledge Reviewer | Slice 1 |
| Make a Decision | Review Recommendation; inspect provenance; accept, reject, or defer; record rationale and authority | Decision Maker, Decision Reviewer | Slice 1 |
| Plan Work | Create Objective; define Intended Outcome; create Project; record Deliverables, assumptions, Milestones, and Activity Plans; approve Plan Revision | Objective Owner, Project Owner, Plan Reviewer, Plan Approver | Slice 2 |
| Execute Work | Assign work; accept Assignment; record Work Events; report blockage or risk; derive current status | Assignment Authority, Contributor, Project Owner | Slice 3 |
| Produce a Deliverable | Create Document or Artifact Revision; record production provenance; review Deliverable; accept Milestone or completion | Contributor, Deliverable Reviewer, Milestone Acceptance Authority, Completion Authority | Slice 3 |
| Measure Results | Define Measurement Definition; record or import Measurement Record; preserve quality and temporal context | Measurement Designer, Measurement Recorder | Slice 4 |
| Review Outcomes | Create Observed Outcome; compare against Success Conditions; assess attribution; issue Outcome Review | Outcome Analyst, Outcome Reviewer, Outcome Owner | Slice 4 |
| Learn and Adapt | Create Learning Record; challenge assumptions; make adaptation Decision; revise Objective or Plan | Learning Author, Adaptation Decision Maker, Project Owner | Slice 5 |
| Publish or Share | Assemble sources; validate dependencies and approvals; create immutable Published Version; reproduce release | Editor, Publication Reviewer, Publication Authority | Slice 6 |

## 4. Release Slices

### Release 1A — Evidence to Decision

Minimum user journey:

1. Capture one source Document or Artifact.
2. Identify one Evidence region.
3. Create one Finding.
4. Create one Recommendation.
5. Record one authorized Decision.
6. Navigate from Decision to exact source Evidence.

### Release 1B — Decision to Planned Work

Minimum user journey:

1. Create an Objective from a Decision.
2. Define one Intended Outcome.
3. Create one Project.
4. Define one expected Deliverable.
5. Create one Activity Plan.
6. Approve one Plan Revision.

### Release 1C — Planned Work to Deliverable

Minimum user journey:

1. Assign one Activity Plan.
2. Record Work Started.
3. Record one consequential Work Event.
4. Produce one Deliverable Revision.
5. Record Deliverable Production.
6. Accept or reject one Milestone or completion condition.
7. Derive current status from source records.

### Release 1D — Deliverable to Outcome Review

Minimum user journey:

1. Define one Measurement Definition.
2. Record one Measurement Record.
3. Create one Observed Outcome.
4. Evaluate one Success Condition.
5. Issue one Outcome Review.
6. Trace the review to measurements, Deliverable, Project, Decision, and Evidence.

### Release 1E — Learning to Adaptation

Minimum user journey:

1. Create one Learning Record from an Outcome Review.
2. Link the learning to one affected assumption or Decision.
3. Record one adaptation Decision.
4. Create one revised Objective or Project Revision.
5. Preserve and navigate the prior state.

## 5. Must-Have Story Groups

### 5.1 Identity and Provenance

- Assign stable identity to every managed Resource, Revision, Relationship, and immutable record.
- Link interpretations and decisions to exact source Revisions or regions.
- Display complete provenance paths.
- Preserve authority designation for imported information.

### 5.2 History and Correction

- Preserve historical Revisions and records.
- Correct immutable records through explicit correction.
- Supersede conclusions, Decisions, and Plans without deletion.
- Reconstruct what was known and approved at a prior time.

### 5.3 Authorization

- Restrict consequential actions by contextual role and scope.
- Record decision and approval authority.
- Separate view permission from change or approval authority.
- Audit privileged actions.

### 5.4 Plan and Execution Separation

- Reference exact Plan Revisions from execution records.
- Preserve actual execution independently of replanning.
- Derive current status from source records.
- Preserve planned and actual values separately.

### 5.5 Output and Outcome Separation

- Record exact produced Deliverable Revisions.
- Prevent Deliverable completion from automatically satisfying an Outcome.
- Require explicit measurement and review.
- Preserve attribution assumptions and uncertainty.

## 6. Should-Have Story Groups

- Competing Findings and Recommendations.
- Contrary Evidence.
- Conditional approvals.
- Blockage and risk observations.
- Imported measurements.
- Multiple Outcome Reviews over time.
- Approval-Controlled reference updates.
- Reproducible publication.

## 7. Later Story Groups

- Programs and multi-project coordination.
- Roadmap scenarios.
- Capacity and budget planning.
- Advanced financial projections.
- Automated thematic analysis.
- Statistical and causal inference.
- Portfolio-level Outcome aggregation.
- Configurable workflow engines.
- Autonomous recommendations under explicit governance.

## 8. Walking Skeleton

The smallest technical and domain walking skeleton should support:

1. Create a Resource and immutable Revision.
2. Create a typed Relationship.
3. Create an immutable record.
4. Assign a contextual role.
5. Navigate provenance.
6. Apply authorization.
7. Generate one explainable Projection.

This skeleton is then exercised by each release slice rather than expanded as an abstract platform first.

## 9. Story Readiness Rules

A story is ready when:

- it names one primary role;
- it produces a domain-visible result;
- its owning bounded context is known;
- cross-context references are explicit;
- applicable invariants are identified;
- acceptance examples are concrete;
- deferred behavior is stated.

## 10. Story Completion Rules

A story is complete when:

- the user can complete the task;
- authority is enforced;
- history is preserved;
- provenance is navigable;
- failures are safe and understandable;
- acceptance examples pass;
- derived results are explainable;
- new domain questions are documented.

## 11. Open Questions

1. Which release slice should be piloted first with real users?
2. Which roles can be combined in the pilot without hiding authority distinctions?
3. Which interactions require a user interface in the first release versus import or API support?
4. What is the minimum usable provenance view?
5. Which stories should become executable acceptance specifications?
6. Which stories require external integrations from the outset?
