# Implementation Plan: Second Walking Slice

## Overview

This plan implements the *second walking slice* of the Organizational Knowledge and Work System: the additive Planning_Service specified in [`design.md`](./design.md) that satisfies the requirements in [`requirements.md`](./requirements.md). The slice extends the first-walking-slice modular monolith with a new `walking_slice.planning/` package, two strictly additive Slice 1 schema columns (`Relationships.semantic_role`, `Identifier_Registry.resource_kind`), one additive authority enumeration value (`review`), and one additive disclosure-policy coverage table — and adds the Planning Provenance Chain `navigate_plan_approval` traversal.

The plan is incremental and additive. It begins by extending Slice 1's enumeration, schema, and disclosure surface (the smallest changes that unblock everything else), then builds the eight Planning Resource services in dependency order (Objective → Intended Outcome and Project → Deliverable Expectation, Activity Plan → Plan Revision → Plan Review → Plan Approval), then layers the planning-aware provenance and backlink extensions, and finishes with HTTP composition, cross-cutting property tests, and end-to-end integration tests. Every correctness property from design §"Correctness Properties" gets its own property-test sub-task annotated with the property number and the Requirements clauses it validates.

Throughout, the plan honors Requirement 19 (Reuse and Non-Modification of Slice 1 Contexts): the only Slice 1 modifications are the additive enumeration value, the two additive schema columns, the new disclosure-coverage table, and additive functions on `walking_slice.provenance` and `walking_slice.disclosure`. No Slice 1 function, class, table, trigger, or invariant is removed, renamed, or narrowed.

## Tasks

- [x] 1. Extend Slice 1 enumerations, schema, and registries (additive only)
  - [x] 1.1 Add `review` to the Slice 1 authority enumeration
    - In `src/walking_slice/authorization.py`, extend the `_VALID_AUTHORITIES` constant to include the literal value `"review"` alongside `"view"`, `"modify"`, and `"approve"`. Do not remove or rename any existing value.
    - Extend the `_required_authority` mapping with the eight Slice 2 action types: `create.objective` → `modify`, `create.intended_outcome` → `modify`, `create.project` → `modify`, `create.deliverable_expectation` → `modify`, `create.activity_plan` → `modify`, `create.plan_revision` → `modify`, `create.plan_review` → `review`, `create.plan_approval` → `approve`.
    - Do not change the non-substitution behavior of `AuthorizationService.evaluate`.
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 19.2_

  - [x] 1.2 Add `Relationships.semantic_role` and `Identifier_Registry.resource_kind` columns
    - Extend `src/walking_slice/persistence.py` with two `ALTER TABLE` statements emitted during schema creation when those columns do not yet exist: `Relationships ADD COLUMN semantic_role TEXT NULL` and `Identifier_Registry ADD COLUMN resource_kind TEXT NULL`.
    - Do not change the existing `Relationships` UPDATE/DELETE triggers or the `Identifier_Registry` UNIQUE index on `identifier`.
    - _Requirements: 4.5, 19.2, 19.4_

  - [x] 1.3 Create the Planning schema and append-only triggers
    - Create `src/walking_slice/planning/_persistence.py` exposing `create_planning_schema(engine)` that issues every `CREATE TABLE` and `CREATE TRIGGER` statement from design §"Data Models — Schema Additions" for `Objectives`, `Objective_Revisions`, `Intended_Outcomes`, `Intended_Outcome_Revisions`, `Projects`, `Project_Revisions`, `Deliverable_Expectations`, `Deliverable_Expectation_Revisions`, `Activity_Plans`, `Plan_Revisions`, `Plan_Reviews`, `Plan_Review_Revisions`, `Plan_Approval_Records`, and `Disclosure_Policy_Coverage`.
    - Add `UPDATE` and `DELETE` triggers that reject mutation on every new table, matching the Slice 1 AD-WS-4 pattern.
    - Add the special `Plan_Revisions` UPDATE trigger that permits exactly one transition `('draft','approved')` when the SQLite session pragma `walking_slice.plan_approval_in_progress` is set and rejects every other UPDATE.
    - Add every composite index named in design §"Indexes".
    - _Requirements: 9.4, 12.5, 13.6, 16.3, 19.4, 20.4, 20.11_

  - [x] 1.4 Extend `Disclosure_Policies` registry with `slice-default-2026` coverage rows for new node kinds
    - Create `src/walking_slice/planning/_disclosure.py` exposing `seed_planning_coverage(connection)` that inserts one `Disclosure_Policy_Coverage` row per Slice 2 node kind (`objective`, `objective_revision`, `intended_outcome`, `intended_outcome_revision`, `project`, `project_revision`, `deliverable_expectation`, `deliverable_expectation_revision`, `activity_plan`, `plan_revision`, `plan_review`, `plan_review_revision`, `plan_approval`) with `policy_id = 'slice-default-2026'`, the recorded date, and `backlog_adr_id = 'ADR-HT-009'`.
    - Extend `walking_slice.disclosure.policy_for(node_kind)` so it consults `Disclosure_Policy_Coverage` as well as the existing `Disclosure_Policies` rows. Do not change the existing row contents.
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 19.2_

  - [x] 1.5 Write unit tests for the additive Slice 1 extensions
    - Cover: `review` is accepted by `AuthorizationService.assign_role` and required by `create.plan_review`; existing actions still demand their pre-Slice-2 authority; `Relationships` rows with `semantic_role = NULL` continue to be readable unchanged; `Identifier_Registry.resource_kind` accepts NULL on Slice 1 rows; planning tables reject UPDATE and DELETE; the `Plan_Revisions` lifecycle trigger permits exactly the one transition only when the session pragma is set.
    - _Requirements: 1.6, 11.1, 11.6, 17.1, 19.2, 19.4_

- [x] 2. Implement Planning value objects and shared helpers
  - [x] 2.1 Create `walking_slice.planning.models`
    - Create `src/walking_slice/planning/models.py` containing the frozen Pydantic value objects from design §"In-Memory Value Objects": `ObjectiveRef`, `IntendedOutcomeRef`, `ProjectRef`, `DeliverableExpectationRef`, `ActivityPlanRef`, `PlanRevisionRef`, `PlanApprovalRef`, and `PlanApprovalOmissionEntry`.
    - Reuse `AuthorityBasisRef`, `TargetRef`, and `Clock` from existing Slice 1 modules; do not redefine them.
    - _Requirements: 1.1, 1.2, 8.2, 9.2_

  - [x] 2.2 Create shared Planning helpers
    - Create `src/walking_slice/planning/_helpers.py` with: a `_record_planning_resource(connection, registry_kind, resource_kind, identifier, content_digest, ...)` helper that calls `IdentityService.reject_if_duplicate` and inserts into `Identifier_Registry` with the `resource_kind` tag (covering Requirement 4.5 disjointness); a `_reject_prohibited_attributes(request_body, prefixes)` helper that raises a `PlanningValidationError` if any top-level key in the request body matches any prefix in `{execution_prefixes, observed_outcome_prefixes, produced_deliverable_prefixes}` (Property 22).
    - _Requirements: 4.5, 12.1, 12.2, 13.1, 13.2, 13.5, 20.5, 20.6, 20.12_

- [x] 3. Implement Objectives service
  - [x] 3.1 Implement `ObjectiveService.create_objective`
    - Create `src/walking_slice/planning/objectives.py` exposing the `ObjectiveService` dataclass from design §"Planning_Service.Objectives".
    - Validate inputs against the Requirement 2.3 ranges (statement 1..4000, rationale 0..10000) and reject any prohibited execution / observed-outcome / produced-deliverable attribute via `_reject_prohibited_attributes` per design §"Components and Interfaces".
    - Resolve the target Decision through `KnowledgeService.get_decision(decision_id)` (AD-WS-21); reject when unresolvable or `outcome != 'Accept'`.
    - Evaluate `Authorization_Service.evaluate(party, "create.objective", target, at)`; on deny, use the Slice 1 separate-transaction Denial Record pattern with 3-attempt retry (Requirement 7.6) and return the AD-WS-9 denial response shape.
    - On permit, open the caller's transaction, insert `Objectives`, `Objective_Revisions`, the `Addresses` Relationship to the Decision, and the consequential `Audit_Records` row.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 3.2 Write unit tests for `ObjectiveService.create_objective`
    - Cover: boundary lengths on statement and rationale; Decision outcome variants (`Accept`, `Reject`, `Defer`, unresolved); prohibited-attribute rejection; missing required attributes; authorization deny path producing exactly one Denial Record.
    - _Requirements: 2.3, 2.4, 2.5, 2.6_

- [x] 4. Implement Intended Outcomes service
  - [x] 4.1 Implement `IntendedOutcomeService.create_intended_outcome`
    - Create `src/walking_slice/planning/intended_outcomes.py` exposing the `IntendedOutcomeService` dataclass from design §"Planning_Service.IntendedOutcomes".
    - Use a Pydantic request model with `Config(extra='forbid')` and an explicit validator rejecting any observed-outcome attribute key (Requirement 3.3).
    - Resolve the target Objective via a single SELECT against `Objectives` by Identity.
    - Evaluate `Authorization_Service.evaluate(party, "create.intended_outcome", ...)`; reuse the AD-WS-9 denial path from `objectives.py`.
    - Insert `Intended_Outcomes`, `Intended_Outcome_Revisions` (with `outcome_kind = 'intended'`), the `Addresses` Relationship to the Objective, and the consequential audit row inside one transaction.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 13.1, 13.3_

  - [x] 4.2 Write unit tests for `IntendedOutcomeService.create_intended_outcome`
    - Cover: `outcome_kind` CHECK rejection if anything other than `'intended'` is attempted; observed-outcome-key rejection across a curated key list; boundary lengths on success condition, observation window, attribution assumption.
    - _Requirements: 3.3, 13.1, 13.3_

- [x] 5. Implement Projects service
  - [x] 5.1 Implement `ProjectService.create_project`
    - Create `src/walking_slice/planning/projects.py` exposing the `ProjectService` dataclass from design §"Planning_Service.Projects".
    - Validate planned-date range (`planned_start_date <= planned_end_date`); reject malformed dates and other invalid attributes per Requirement 4.3.
    - Resolve the target Objective by Identity; reject unresolvable.
    - Tag the issued Project Resource identifier with `resource_kind = 'project'` in `Identifier_Registry` (Requirement 4.5).
    - Insert `Projects`, `Project_Revisions`, the `Addresses` Relationship to the Objective, and the consequential audit row inside one transaction.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 5.2 Write unit tests for `ProjectService.create_project`
    - Cover: planned-date order validation (start > end rejected at both Pydantic and CHECK layers); identifier-set tagging.
    - _Requirements: 4.2, 4.3, 4.5_

- [x] 6. Implement Deliverable Expectations service
  - [x] 6.1 Implement `DeliverableExpectationService.create_deliverable_expectation`
    - Create `src/walking_slice/planning/deliverable_expectations.py` exposing the `DeliverableExpectationService` dataclass.
    - Use a Pydantic request model with `Config(extra='forbid')` and an explicit validator rejecting any produced-deliverable attribute key (Requirement 5.3 / 13.2).
    - Validate `deliverable_kind` against the enumerated set `{Document, Artifact, Service, Other}`; reject any other value.
    - Insert `Deliverable_Expectations`, `Deliverable_Expectation_Revisions`, the `Addresses` Relationship to the Project, and the consequential audit row inside one transaction.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 13.2_

  - [x] 6.2 Write unit tests for `DeliverableExpectationService.create_deliverable_expectation`
    - Cover: produced-deliverable-key rejection across a curated key list; deliverable_kind enumeration boundaries; boundary lengths on name, description, acceptance criteria.
    - _Requirements: 5.2, 5.3, 13.2_

- [x] 7. Implement Activity Plans service
  - [x] 7.1 Implement `ActivityPlanService.create_activity_plan`
    - Create `src/walking_slice/planning/activity_plans.py` exposing the `ActivityPlanService` dataclass.
    - Validate the title length (1..200) and resolve the target Project by Identity.
    - Tag the issued Activity Plan Resource identifier with `resource_kind = 'activity_plan'` in `Identifier_Registry` (Requirement 4.5 — disjoint from Project identifier set).
    - Insert one `Activity_Plans` row and the consequential audit row inside one transaction.
    - _Requirements: 4.5, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x] 7.2 Write unit tests for `ActivityPlanService.create_activity_plan`
    - Cover: title length boundaries; unresolved Project; identifier-set tagging.
    - _Requirements: 4.5, 6.2, 6.3_

- [x] 8. Checkpoint - Slice 2 declarative-intent layer
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement Plan Revisions service
  - [x] 9.1 Implement `PlanRevisionService.create_plan_revision`
    - Create `src/walking_slice/planning/plan_revisions.py` exposing the `PlanRevisionService` dataclass.
    - Validate count and length boundaries on `deliverable_expectation_refs` (0..50, each resolves), `planning_assumptions` (0..100 entries, each 1..2000 chars), `planned_scope` (1..10000 chars), `ordering_rationale` (0..2000 chars).
    - When `predecessor_plan_revision_id` is supplied, validate that it resolves to a Plan Revision of the same Activity Plan whose `lifecycle_state` is `draft` (Requirement 7.4 — approved predecessors rejected).
    - Insert `Plan_Revisions` with `lifecycle_state = 'draft'`; when a predecessor is present, additionally insert a `Supersedes` Relationship row.
    - Append the consequential audit row inside the same transaction.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6_

  - [x] 9.2 Write unit tests for `PlanRevisionService.create_plan_revision`
    - Cover: count and length boundaries; unresolved Deliverable Expectation reference; approved-predecessor rejection; Supersedes Relationship row inserted exactly once for predecessor case.
    - _Requirements: 7.2, 7.3, 7.4_

- [x] 10. Implement Plan Reviews service
  - [x] 10.1 Implement `PlanReviewService.create_plan_review`
    - Create `src/walking_slice/planning/plan_reviews.py` exposing the `PlanReviewService` dataclass.
    - Validate `outcome` against `{Endorse, Changes_Requested, Reject}` and `rationale` length (1..10000); validate `authority_basis.type` against AD-WS-10 set.
    - Resolve the target Plan Revision; reject if not found or `lifecycle_state != 'draft'`.
    - Evaluate `Authorization_Service.evaluate(party, "create.plan_review", ...)` (requires `review` authority per AD-WS-15); on deny, use the AD-WS-9 separate-transaction denial pattern.
    - Insert `Plan_Reviews`, `Plan_Review_Revisions`, and one `Relationships` row with `relationship_type = 'Relates To'`, `semantic_role = 'review'`, `source_kind = 'plan_review_revision'`, `target_kind = 'plan_revision'` (AD-WS-17).
    - Do not change the target Plan Revision's `lifecycle_state` (Requirement 8.7).
    - Append the consequential audit row inside the same transaction.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 11.4_

  - [x] 10.2 Write unit tests for `PlanReviewService.create_plan_review`
    - Cover: outcome enumeration validation; authority basis type validation; non-draft target rejection; lifecycle_state byte-equivalence after Plan Review; one Relates To row per Plan Review with `semantic_role = 'review'`.
    - _Requirements: 8.3, 8.6, 8.7_

- [x] 11. Implement Plan Approvals service
  - [x] 11.1 Implement `PlanApprovalService.create_plan_approval` happy path
    - Create `src/walking_slice/planning/plan_approvals.py` exposing the `PlanApprovalService` dataclass from design §"Planning_Service.PlanApprovals".
    - Validate inputs against Requirement 9.2: `outcome ∈ {Approve, Reject_Approval}`, `rationale` 1..4000, `authority_basis.type` in AD-WS-10 set, scope present.
    - Resolve the target Plan Revision; reject if unresolved, already approved, or already the target of any Plan Approval Record (pre-check Requirement 9.5; UNIQUE constraint is the source of truth).
    - Evaluate `Authorization_Service.evaluate(party, "create.plan_approval", ...)` on a fresh transaction (Slice 1 pattern from `KnowledgeService.create_decision`); on deny, append a Denial Record in a separate transaction with 3-attempt retry and raise `PlanApprovalAuthorizationError` (or `PlanApprovalAuditFailureError` if every retry fails).
    - On permit, open the caller's transaction, set the SQLite session pragma `walking_slice.plan_approval_in_progress` to the correlation identifier, insert `Plan_Approval_Records`, insert the `Addresses` Relationship, insert the Provenance Manifest via the existing `ProvenanceManifestWriter`, insert any Omission Entries, execute the one permitted UPDATE on `Plan_Revisions.lifecycle_state` to `'approved'`, append the consequential audit row, and unset the session pragma.
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.7, 10.1, 10.2, 10.4, 10.5, 10.6_

  - [x] 11.2 Implement Plan Approval immutability enforcement
    - When any UPDATE or DELETE is attempted against an Approved Plan Revision, its associated Relationships, its Plan Review Revisions, or its Plan Approval Record, return HTTP 409 with `error_code = approved_plan_revision_immutable` and append a Denial Record (Requirement 9.6).
    - The database triggers from task 1.3 already reject these mutations; this task wires the application-level error mapping and Denial Record append.
    - _Requirements: 9.4, 9.6_

  - [x] 11.3 Write unit tests for `PlanApprovalService.create_plan_approval`
    - Cover: duplicate-approval rejection (Requirement 9.5); authority deny path with all 5 reason codes (Slice 1 enumeration); the session-pragma lifecycle trigger permits exactly the one transition; manifest persistence failure rolls back the entire transaction (audit row, lifecycle UPDATE, Relationships row).
    - _Requirements: 9.1, 9.4, 9.5, 9.6, 10.1, 10.6_

- [x] 12. Implement Planning Provenance Chain traversal
  - [x] 12.1 Implement `navigate_plan_approval` on `Provenance_Navigator`
    - Add `navigate_plan_approval(plan_approval_id, party, at)` to `src/walking_slice/provenance.py` as an additive function. Do not modify the existing `navigate_decision` function.
    - Walk Plan Approval → Plan Revision → Activity Plan → Project → Objective → Slice 1 Decision and delegate to the existing `navigate_decision` for the Decision → Recommendation → Finding → Region → Document tail.
    - Apply the `slice-default-2026` Completeness Disclosure policy (now covering Slice 2 node kinds via task 1.4) for restricted-vs-nonexistent observability.
    - Return identical results across repeated invocations for the same `(plan_approval_id, party, at)` (Requirement 14.5 — idempotent retrieval).
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7_

  - [x] 12.2 Extend backlinks to cover planning node kinds
    - Extend the existing `_authorized_source_kinds` (or equivalent set) in `src/walking_slice/provenance.py` to include `'objective_revision'`, `'intended_outcome_revision'`, `'project_revision'`, `'deliverable_expectation_revision'`, `'plan_revision'`, `'plan_review_revision'`, and `'plan_approval'` so the existing constant-time backlink algorithm returns Slice 2 relationships when the queried endpoint is a planning node.
    - Do not change the algorithm itself; this is an additive coverage extension only.
    - _Requirements: 15.1, 15.2, 15.4, 15.6_

- [x] 13. Implement Interim ADR seeding for Slice 2 gaps
  - [x] 13.1 Seed Interim_ADR_Records for Gaps G-6 through G-10
    - Extend `src/walking_slice/interim_adr.py` (or create `src/walking_slice/planning/_interim_adr.py` and have it call the existing seed function) to insert one row per AD-WS-15 (G-6 / `ADR-HT-006`), AD-WS-16 (G-7 / `ADR-HT-009`), AD-WS-17 (G-8 / `ADR-HT-010`), AD-WS-18 (G-9 / `ADR-HT-011`), and AD-WS-19 (G-10 / `ADR-HT-012`).
    - Each row records the motivating Requirement number, motivating criterion number, observable behavior chosen, recorded date, and backlog ADR identifier per Slice 1 Requirement 16.3.
    - _Requirements: 19.5, 21.3_

  - [x] 13.2 Write unit tests for Slice 2 Interim ADR seeding
    - Cover: every Slice 2 backlog ADR identifier maps to at least one seeded row with the documented fields; re-running the seeder is idempotent.
    - _Requirements: 19.5_

- [x] 14. Implement Projection envelope wrapping for status-bearing planning responses
  - [x] 14.1 Wrap Planning_Service status responses in `ProjectionEnvelope`
    - In each Planning_Service module, ensure any response carrying a derived status (e.g. `"Plan Approved"`, `"Plan Revision draft"`, `"Plan Revision superseded"`, `"Provenance incomplete"`) uses the existing Slice 1 `ProjectionEnvelope` value object from `walking_slice.projection`.
    - On unresolvable Projection Definition or missing source Revision, withhold the projected status and return an explanation-unavailable indicator identifying the missing element (Slice 1 task 14.2 pattern).
    - _Requirements: 18.1, 18.2, 18.3, 18.4_

  - [x] 14.2 Write unit tests for projection envelope wrapping
    - Cover: every status-bearing response includes the envelope with all required fields; unresolvable-definition path withholds status and returns the explanation-unavailable indicator; source records remain byte-equivalent when corrections arrive.
    - _Requirements: 18.1, 18.3, 18.4_

- [x] 15. Implement HTTP routes for the Planning_Service
  - [x] 15.1 Implement planning HTTP endpoints
    - Create `src/walking_slice/planning/_routes.py` exposing one FastAPI APIRouter under `/api/v1` with the endpoints from design §"Components and Interfaces": Objectives (`POST /objectives`, `GET /objectives/{id}/revisions/{revision_id}`), Intended Outcomes, Projects, Deliverable Expectations, Activity Plans, Plan Revisions, Plan Reviews, Plan Approvals, and the Plan Approval provenance walk endpoint `GET /plan-approvals/{plan_approval_id}/provenance`.
    - Wire each route to the corresponding service through the Slice 1 `RequestContext` dependency.
    - Use Pydantic request models with `Config(extra='forbid')` so unknown fields are rejected at the API boundary; combine with `_reject_prohibited_attributes` for the execution / observed-outcome / produced-deliverable rejection paths.
    - _Requirements: 2.1, 3.1, 4.1, 5.1, 6.1, 7.1, 8.1, 9.1, 14.1, 15.1, 17.1_

  - [x] 15.2 Mount planning routes into the FastAPI application
    - Extend `src/walking_slice/app.py` so the FastAPI application loads `planning._routes`, calls `planning._persistence.create_planning_schema(engine)` and `planning._disclosure.seed_planning_coverage(connection)` on startup, and calls the extended Interim ADR seeder.
    - Do not alter Slice 1 startup behavior beyond these additive calls.
    - _Requirements: 17.5, 19.5, 21.1, 21.3_

- [x] 16. Cross-cutting property tests for Slice 2
  - [x] 16.1 Write property test for Planning-creation success
    - **Property 16: Planning-creation success**
    - **Validates: Requirements 2.1, 2.7, 3.1, 3.6, 4.1, 4.6, 5.1, 5.6, 6.1, 6.5, 7.1, 7.6, 8.1, 8.4, 9.1, 9.7, 16.1, 20.1**
    - Use Hypothesis strategies for each of the eight planning request bodies; for every authorized valid request assert exactly one Resource (and where applicable, one first Revision) plus one consequential `Audit_Records` row are persisted in one transaction with byte-equivalent recorded times.

  - [x] 16.2 Write property test for Planning-Resource authority correctness
    - **Property 17: Planning-Resource authority correctness**
    - **Validates: Requirements 2.5, 3.5, 4.4, 5.5, 6.4, 7.5, 8.5, 9.1, 10.1, 10.3, 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 20.2, 20.3**
    - Generate role assignments varying across effective-start, expiration, revocation, scope, and granted-authority dimensions; for every persisted planning artifact assert a matching role assignment exists whose `authorities_granted` includes the precise required authority, whose scope covers the target, and whose effective period encloses the recorded time. Assert no persisted artifact exists when the authoring Party held only a different authority type.

  - [x] 16.3 Write property test for Indistinguishable denial across planning endpoints
    - **Property 18: Indistinguishable denial across planning endpoints**
    - **Validates: Requirements 10.1, 10.4, 10.5, 10.7, 14.3, 14.7, 15.3, 15.5, 17.2, 17.3, 17.4, 20.8**
    - Generate pairs `(P, P′)` differing only in authority on `R`; assert responses to `P′` for creation, review, approval, backlink, and provenance attempts on `R` are indistinguishable from non-existent-endpoint responses across count, identifier set, ordering positions, pagination cursors, response size, body keys, error category, error wording, and latency (within 100 ms tolerance).

  - [x] 16.4 Write property test for Audit completeness across planning actions
    - **Property 19: Audit completeness for consequential and denied planning actions**
    - **Validates: Requirements 2.7, 7.6, 16.1, 16.2, 16.5**
    - Generate sequences of planning operations (creations, reviews, approvals, denied attempts, attempted modifications of approved resources); assert exactly one consequential `Audit_Records` row per consequential write and exactly one Denial Record per denied attempt, each with the required fields and `correlation_id` consistent with the originating operation. Assert denied attempts leave no in-flight planning write persisted.

  - [x] 16.5 Write property test for Approved Plan Revision immutability
    - **Property 20: Approved Plan Revision immutability**
    - **Validates: Requirements 9.4, 9.6, 16.5, 20.4**
    - For each generated full pipeline that reaches approval, apply Hypothesis-drawn mutation-attempt sequences and assert the Plan Revision row, every constituent field, every Relationship sourced from or targeting it, every Plan Review Revision targeting it, and the Plan Approval Record are byte-equivalent across all later observation points.

  - [x] 16.6 Write property test for Slice 1 non-modification
    - **Property 21: Slice 1 non-modification**
    - **Validates: Requirements 19.1, 19.2, 19.3, 19.4, 20.11**
    - Snapshot every Slice 1 table before any Slice 2 action; run Hypothesis-drawn Slice 2 operation sequences; assert every Slice 1 row is byte-equivalent at every observation point, apart from the additive `Relationships.semantic_role = NULL` and `Identifier_Registry.resource_kind = NULL` columns whose new values are only populated for Slice 2 rows.

  - [x] 16.7 Write property test for Plan/Execution and Output/Outcome separation
    - **Property 22: Plan/Execution and Output/Outcome separation**
    - **Validates: Requirements 3.3, 5.3, 12.1, 12.2, 12.4, 13.1, 13.2, 13.3, 13.4, 13.5, 20.5, 20.6**
    - Generate request bodies with random keys drawn from prohibited execution / observed-outcome / produced-deliverable prefixes; assert every such request is rejected with no row persisted, every persisted Intended Outcome Revision carries `outcome_kind = 'intended'`, and no response body for a planning Resource includes a derived current-execution status, percent-complete value, actual-cost value, or remaining-work value.

  - [x] 16.8 Write property test for Planning Provenance Chain end-to-end
    - **Property 23: Planning Provenance Chain end-to-end**
    - **Validates: Requirements 14.1, 14.2, 14.4, 14.5, 20.7**
    - Generate full Slice 1 + Slice 2 pipelines whose chain is fully visible to the requesting Party; navigate from each Plan Approval Record and assert the full ordered chain (Plan Approval → Plan Revision → Activity Plan → Project → Objective → Decision → Recommendation → Finding → Region Occurrence → Document Revision) returns, every identity resolves, the returned Content Region Occurrence span digest matches the recorded digest, and the chain is byte-equivalent across 5 repetitions per generated case.

  - [x] 16.9 Write property test for Backlink bidirectionality across planning nodes
    - **Property 24: Backlink bidirectionality for planning nodes**
    - **Validates: Requirements 1.5, 15.1, 15.2, 15.4, 15.6, 20.9**
    - Generate Slice 1 + Slice 2 relationship graphs; for each requesting Party holding view authority on both `R` and its source endpoint, assert the Provenance_Navigator returns `R` from both the source's outbound and the target's backlink query with identical Relationship attribute values, including the `semantic_role` column.

  - [x] 16.10 Write property test for Plan Approval uniqueness
    - **Property 25: Plan Approval uniqueness**
    - **Validates: Requirements 9.5, 20.10**
    - Generate double-approval attempts for arbitrary Plan Revisions; assert only the first attempt persists a `Plan_Approval_Records` row and the second is rejected with no second row persisted; assert the first Plan Approval Record remains byte-equivalent after the second attempt.

  - [x] 16.11 Write property test for Slice 2 Interim ADR records retrievability
    - **Property 26: Slice 2 Interim ADR records retrievability**
    - **Validates: Requirements 19.5, 21.3**
    - For each backlog ADR identifier in `{ADR-HT-006, ADR-HT-009, ADR-HT-010, ADR-HT-011, ADR-HT-012}`, query `Interim_ADR_Records` and assert at least one row exists with the documented motivating Requirement number, criterion number, observable behavior, recorded date, and backlog ADR identifier. Assert rows are byte-equivalent across observation points.

  - [x] 16.12 Write property test for Identity uniqueness, opacity, and Project / Activity-Plan disjointness
    - **Property 27: Identity uniqueness, opacity, and Project / Activity-Plan disjointness**
    - **Validates: Requirements 1.1, 1.2, 1.4, 1.6, 1.7, 4.5, 20.12**
    - Generate ≥ 100 identifiers per case across Slice 1 and Slice 2 kinds; assert all are unique, canonical UUIDv7, contain no business-attribute substring, and that the Project identifier set is disjoint from the Activity Plan identifier set (verified via `Identifier_Registry.resource_kind`).

  - [x] 16.13 Write property test for Planning relationship-structure invariants
    - **Property 28: Planning relationship-structure invariants**
    - **Validates: Requirements 7.3, 8.3, 9.3, 20.7**
    - For every persisted Plan Approval Record assert exactly one `Addresses` Relationship row with `source_kind='plan_approval'`, `target_kind='plan_revision'`, and `semantic_role IS NULL`. For every Plan Review Revision assert exactly one `Relates To` row with `semantic_role='review'`. For every Plan Revision created with a predecessor assert exactly one `Supersedes` row.

  - [x] 16.14 Write property test for Projection envelope wrapper
    - **Property 29: Projection envelope wrapper**
    - **Validates: Requirements 18.1, 18.2**
    - Generate planning operation sequences that produce status-bearing responses; assert every such response carries a `ProjectionEnvelope` with Projection Definition, source Resource Identities, source Revision Identities, applicable temporal boundary, generated time, and a derivation indicator.

  - [x] 16.15 Configure repeatable property runs and seed capture for Slice 2
    - **Property 30: Repeatable property runs (operational)**
    - **Validates: Requirements 20.13, 21.3**
    - Extend the Slice 1 Hypothesis profile to the new Slice 2 property tests (`@settings(max_examples=100, deadline=2000)`), enable `--hypothesis-seed` capture on every Slice 2 property test, persist the seed of every property test invocation to a build artifact alongside the Slice 1 seeds, and add a re-execution check that confirms identical pass/fail outcomes and minimal counterexamples for failing properties.

- [x] 17. End-to-end HTTP integration tests for Slice 2
  - [x] 17.1 Write end-to-end tests for the Release 1B journey
    - Drive the FastAPI app via `httpx.AsyncClient` and exercise the full pipeline: capture Evidence → create Finding → create Recommendation → record Decision (Slice 1) → create Objective → record Intended Outcome → create Project → declare Deliverable Expectation → create Activity Plan → submit Plan Revision → record Plan Review → approve Plan Revision → navigate Planning Provenance Chain back to exact Document Revision text.
    - _Requirements: 2.1, 3.1, 4.1, 5.1, 6.1, 7.1, 8.1, 9.1, 14.1_

  - [x] 17.2 Write end-to-end tests for the named denial demonstrations
    - Drive the FastAPI app and assert: a Plan Reviewer attempting Plan Approval is denied with an AD-WS-9-shaped response and a Denial Record; modifying an Approved Plan Revision is rejected with `error_code = approved_plan_revision_immutable` and a Denial Record; submitting an Intended Outcome with an observed-outcome attribute is rejected with no row persisted; submitting a Plan Approval against a non-existent Plan Revision is indistinguishable from submitting against a restricted Plan Revision the caller cannot view.
    - _Requirements: 9.6, 10.1, 10.4, 13.1, 14.7_

- [x] 18. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; the core implementation tasks (no `*`) are mandatory.
- Property tests directly map to design §"Correctness Properties" — every property in the Slice 2 suite (16 through 30) has its own sub-task under task 16.
- The Interim ADR records seeded in task 13.1 close Gaps G-6 through G-10 from requirements.md and unblock acceptance of the new backlog ADRs (HT-006, HT-009, HT-010, HT-011, HT-012) without re-implementing the slice.
- Checkpoints sit after the declarative-intent layer (task 8) and at the end (task 18); each is a manual verification gate.
- Requirement 19 (Slice 1 non-modification) is enforced both by code structure (additive `Disclosure_Policy_Coverage` table, additive enumeration values, additive functions on `walking_slice.provenance` and `walking_slice.disclosure`) and by Property 21 (Slice 1 non-modification) running at every observation point.
- All testing uses `pytest` with Hypothesis configured per design §"Testing Strategy"; each property test runs at least 100 generated cases with deterministic seed capture per Requirement 20.13.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.2"] },
    { "id": 1,  "tasks": ["1.3"] },
    { "id": 2,  "tasks": ["1.4", "2.1"] },
    { "id": 3,  "tasks": ["1.5", "2.2"] },
    { "id": 4,  "tasks": ["3.1"] },
    { "id": 5,  "tasks": ["3.2", "4.1", "5.1"] },
    { "id": 6,  "tasks": ["4.2", "5.2", "6.1", "7.1"] },
    { "id": 7,  "tasks": ["6.2", "7.2"] },
    { "id": 8,  "tasks": ["8"] },
    { "id": 9,  "tasks": ["9.1"] },
    { "id": 10, "tasks": ["9.2", "10.1"] },
    { "id": 11, "tasks": ["10.2", "11.1"] },
    { "id": 12, "tasks": ["11.2"] },
    { "id": 13, "tasks": ["11.3", "12.1"] },
    { "id": 14, "tasks": ["12.2", "13.1", "14.1"] },
    { "id": 15, "tasks": ["13.2", "14.2"] },
    { "id": 16, "tasks": ["15.1"] },
    { "id": 17, "tasks": ["15.2"] },
    { "id": 18, "tasks": ["16.1", "16.2", "16.3", "16.4", "16.5", "16.6", "16.7", "16.8", "16.9", "16.10", "16.11", "16.12", "16.13", "16.14", "16.15"] },
    { "id": 19, "tasks": ["17.1", "17.2"] },
    { "id": 20, "tasks": ["18"] }
  ]
}
```

