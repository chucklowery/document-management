# User Roles and Responsibilities

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines contextual user roles revealed by the initial vertical slices.

A role is a responsibility or authority exercised by a Party in a particular context. Roles are not permanent user types and do not redefine the Party.

## 2. Role Principles

1. A Party may hold multiple roles.
2. Roles may be scoped to a Resource, Project, Organization, context, or effective period.
3. Authority is explicit and auditable.
4. Permission to view information is distinct from authority to change or approve it.
5. Automated Agents may perform delegated actions only within explicit authorization.
6. Separation of duties may be required for consequential actions.

## 3. Research and Knowledge Roles

### 3.1 Evidence Contributor

Provides source material such as interviews, observations, datasets, documents, or artifacts.

Responsibilities:

- submit or identify Evidence;
- preserve source context;
- identify sensitivity and consent constraints where known;
- correct factual metadata through explicit correction.

### 3.2 Researcher

Collects and organizes Evidence.

Responsibilities:

- create or import source materials;
- identify addressable Evidence regions;
- preserve provenance;
- record collection context and limitations.

### 3.3 Analyst

Interprets Evidence and creates Findings, Insights, and Recommendations.

Responsibilities:

- link interpretations to supporting or contrary Evidence;
- record assumptions and confidence;
- distinguish hypothesis from supported Finding;
- preserve competing interpretations.

### 3.4 Knowledge Reviewer

Reviews the quality, reasoning, and provenance of knowledge records.

Responsibilities:

- evaluate Evidence sufficiency;
- identify unsupported conclusions;
- request revision without erasing prior work;
- approve where review is required.

## 4. Decision Roles

### 4.1 Decision Proposer

Submits a Recommendation or decision request for consideration.

### 4.2 Decision Maker

Has authority to accept, reject, defer, or supersede a Recommendation or prior Decision.

Responsibilities:

- state the decision outcome;
- record rationale;
- identify applicable scope;
- acknowledge significant Evidence, assumptions, and dissent;
- preserve decision authority and time.

### 4.3 Decision Reviewer

Provides advice or required review without holding final decision authority.

## 5. Planning Roles

### 5.1 Objective Owner

Owns the meaning, scope, and review of an Objective.

Responsibilities:

- define the desired future condition;
- establish or approve Success Conditions;
- ensure the Objective is not confused with planned work;
- review continued relevance.

### 5.2 Initiative Sponsor

Authorizes and supports a coordinated response to Objectives, Needs, or Decisions.

### 5.3 Program Leader

Coordinates related Projects and cross-project dependencies.

### 5.4 Project Owner

Owns the governing Project plan.

Responsibilities:

- define scope, expected Deliverables, assumptions, and Intended Outcomes;
- establish Milestones and Activity Plans;
- manage plan Revisions;
- maintain dependency visibility;
- request or record required approvals.

### 5.5 Plan Reviewer

Reviews an exact Plan Revision for feasibility, policy, risk, or alignment.

### 5.6 Plan Approver

Has authority to approve, reject, or conditionally approve an exact Plan Revision.

## 6. Execution Roles

### 6.1 Assignment Authority

Creates or changes Work Assignments within delegated authority.

### 6.2 Contributor

Performs work under an Assignment, Activity Plan, or operational obligation.

Responsibilities:

- accept or reject Assignments where applicable;
- record consequential Work Events;
- identify produced Deliverables;
- report blockages and material risks;
- preserve execution Evidence.

### 6.3 Deliverable Reviewer

Evaluates a Deliverable against stated criteria.

### 6.4 Milestone Acceptance Authority

Accepts, rejects, or conditionally accepts a Milestone against an exact Plan Revision and its criteria.

### 6.5 Completion Authority

Records that a defined work scope is completed, cancelled, or terminated.

Completion authority does not imply authority to declare an Outcome achieved.

## 7. Outcome and Learning Roles

### 7.1 Outcome Owner

Owns an Intended Outcome and its review process.

Responsibilities:

- define scope and observation window;
- establish or approve Success Conditions;
- ensure measurement is planned;
- initiate Outcome Reviews.

### 7.2 Measurement Designer

Defines how a measure is collected or calculated.

Responsibilities:

- define units, populations, criteria, and temporal windows;
- document data sources and quality constraints;
- version material rule changes;
- avoid presenting a proxy as the Outcome itself.

### 7.3 Measurement Recorder

Records or imports Measurement Records under a defined Measurement Definition.

### 7.4 Outcome Analyst

Interprets Measurement Records and creates Observed Outcomes.

Responsibilities:

- distinguish measurement from interpretation;
- state confidence and uncertainty;
- record attribution assumptions and competing explanations.

### 7.5 Outcome Reviewer

Has authority to issue an Outcome Review against exact Success Conditions.

### 7.6 Learning Author

Creates a Learning Record from Outcome Reviews, contrary Evidence, or repeated patterns.

### 7.7 Adaptation Decision Maker

Decides whether learning should change a Decision, Objective, Plan, policy, or Measurement Definition.

## 8. Document and Publication Roles

### 8.1 Author

Creates and revises authoritative source Documents.

### 8.2 Editor

Maintains structure, references, metadata, and composition quality.

### 8.3 Reference Maintainer

Reviews and adopts Approval-Controlled reference updates.

### 8.4 Publication Reviewer

Evaluates publication readiness, source resolution, metadata, and policy compliance.

### 8.5 Publication Authority

Authorizes creation of an immutable Published Version.

## 9. Governance and Platform Roles

### 9.1 Resource Steward

Maintains ownership, classification, metadata quality, and lifecycle expectations for a Resource.

### 9.2 Sensitivity Authority

Classifies sensitive information and governs permitted handling.

### 9.3 Access Administrator

Administers authorization policies without automatically receiving domain decision authority.

### 9.4 Integration Steward

Owns authority mappings, synchronization policy, and translation contracts for an external integration.

### 9.5 System Operator

Operates system infrastructure and observes system health.

System operation does not automatically grant permission to read sensitive domain content.

### 9.6 Automated Agent

Performs explicitly authorized tasks on behalf of a Party or process.

Responsibilities:

- identify itself and its authority context;
- preserve provenance and tool version where relevant;
- avoid actions beyond delegated scope;
- surface uncertainty and failures;
- never silently discard acknowledged work.

## 10. Role Combinations for the Initial Release

The first release may combine roles to reduce workflow burden.

Suggested combinations:

- Researcher + Analyst;
- Decision Proposer + Analyst;
- Objective Owner + Project Owner;
- Assignment Authority + Project Owner;
- Contributor + Deliverable Producer;
- Outcome Owner + Outcome Analyst;
- Measurement Designer + Measurement Recorder;
- Learning Author + Outcome Analyst.

Roles that should remain separately identifiable even when held by one Party:

- Decision Maker;
- Plan Approver;
- Milestone Acceptance Authority;
- Completion Authority;
- Outcome Reviewer;
- Publication Authority.

## 11. Separation-of-Duties Candidates

The system should support policies requiring separate Parties for:

- author and publication authority;
- contributor and deliverable reviewer;
- measurement recorder and outcome reviewer;
- decision proposer and decision maker;
- access administrator and sensitive-content owner;
- automated generator and final approver.

These separations are policy-driven, not universal defaults.

## 12. Open Questions

1. Which roles are distinct in the first pilot organization?
2. Which authorities are delegated by Project, Organization, or Resource?
3. Which role assignments require effective periods?
4. Which automated actions require human approval?
5. Which role combinations create unacceptable conflicts of interest?
6. Which actions require dual control or independent review?
