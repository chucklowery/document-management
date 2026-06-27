# Implementation Plan: Third Walking Slice

## Overview

This plan implements the *third walking slice* of the Organizational Knowledge and Work System: the additive Execution_Service and Deliverable_Repository specified in [`design.md`](./design.md) that satisfies the requirements in [`requirements.md`](./requirements.md). The slice extends the cumulative Slice 1 + Slice 2 modular monolith with two new `walking_slice.execution/` and `walking_slice.deliverables/` packages, four additive authority enumeration values (`assign`, `contribute`, `accept_milestone`, `complete`), eight new insert-only tables, eight additive `Disclosure_Policy_Coverage` rows, and additive `navigate_*` and `_authorized_source_kinds` extensions on `walking_slice.provenance` — and adds the Execution Provenance Chain traversal from Completion Record back to exact Source Evidence.

The plan is incremental and additive. It begins by extending Slice 1's authority enumeration and seeding the new schema, disclosure-policy coverage, and Interim ADR rows (the smallest changes that unblock everything else), then adds the additive Planning_Service and Deliverable_Expectation read APIs (AD-WS-30), then builds the seven Execution_Service and Deliverable_Repository services in dependency order (Deliverable_Repository → Work Assignment → Work Event → Time Entry → Deliverable Production → Milestone Acceptance → Completion), then layers the planning-aware provenance and backlink extensions and the execution-status Projection, and finishes with HTTP composition, cross-cutting property tests, and end-to-end integration tests. Every correctness property from design §"Correctness Properties" gets its own property-test sub-task annotated with the property number and the Requirements clauses it validates.

Throughout, the plan honors Requirement 40 (Reuse and Non-Modification of Slice 1 and Slice 2 Contexts): the only prior-slice modifications are the additive four-value authority enumeration extension, the additive `Disclosure_Policy_Coverage` rows, the additive `Identifier_Registry.resource_kind` values, and additive read-only functions on `walking_slice.planning` and `walking_slice.provenance`. No Slice 1 or Slice 2 function, class, table, trigger, or invariant is removed, renamed, or narrowed.

## Tasks

- [x] 1. Extend Slice 1 + Slice 2 enumerations, schema, and registries (additive only)
  - [x] 1.1 Add `assign`, `contribute`, `accept_milestone`, `complete` to the authority enumeration
    - In `src/walking_slice/authorization.py`, extend the `_VALID_AUTHORITIES` constant to include the literal values `"assign"`, `"contribute"`, `"accept_milestone"`, `"complete"` alongside the existing four values. Do not remove or rename any existing value.
    - Extend the `_required_authority` mapping with the seven Slice 3 action types: `create.work_assignment` → `assign`; `create.work_event` → `contribute`; `create.time_entry` → `contribute`; `create.produced_deliverable` → `contribute`; `create.deliverable_production` → `contribute`; `create.milestone_acceptance` → `accept_milestone`; `create.completion` → `complete`.
    - Do not change the non-substitution behavior of `AuthorizationService.evaluate`.
    - _Requirements: 32.1, 32.2, 32.3, 32.4, 32.5, 32.6, 32.7, 32.8, 32.9, 32.10, 32.11, 40.2_

  - [x] 1.2 Create the Execution_Service schema and append-only triggers
    - Create `src/walking_slice/execution/__init__.py` and `src/walking_slice/execution/_persistence.py` exposing `create_execution_schema(engine)` that issues every `CREATE TABLE` and `CREATE TRIGGER` statement from design §"Data Models — Schema Additions" for `Work_Assignment_Records`, `Work_Event_Records`, `Time_Entry_Records`, `Deliverable_Production_Records`, `Milestone_Acceptance_Records`, and `Completion_Records`.
    - Add `UPDATE` and `DELETE` triggers that reject mutation on every new table, matching the Slice 1 AD-WS-4 and Slice 2 AD-WS-19 patterns.
    - Add the `Work_Assignment_Records` CHECK constraint `assignee_party_id != assignment_authority_party_id` for Requirement 23.5.
    - Add the `Time_Entry_Records` CHECK constraints for `effort_hours` (decimal regex + numeric range 0.00..24.00) and the `effort_period_start <= effort_period_end <= recorded_at` ordering.
    - Add the `Milestone_Acceptance_Records.source_deliverable_production_id` UNIQUE column (Requirement 28.3) and the `Completion_Records.target_plan_revision_id` UNIQUE column (Requirement 29.3).
    - Add the partial UNIQUE index `idx_work_events_one_started_per_wa` for at-most-one `started` per Work Assignment (Requirement 24.3).
    - Add every composite index named in design §"Data Models — Schema Additions" and §"Indexes".
    - _Requirements: 23.1, 23.9, 24.7, 25.2, 25.6, 27.7, 28.3, 28.7, 29.3, 29.7, 41.4, 41.10, 42.4_

  - [x] 1.3 Create the Deliverable_Repository schema and append-only triggers
    - Create `src/walking_slice/deliverables/__init__.py` and `src/walking_slice/deliverables/_persistence.py` exposing `create_deliverable_schema(engine)` that issues every `CREATE TABLE` and `CREATE TRIGGER` statement from design §"Data Models — Schema Additions" for `Deliverable_Resources` and `Deliverable_Revisions`.
    - Add `UPDATE` and `DELETE` triggers that reject mutation on both tables (AD-WS-27).
    - Add the `Deliverable_Revisions.content_bytes` CHECK constraint (1 byte..100 MB per Requirement 26.1), the `role_marker = 'generated_output'` CHECK (Requirement 26.2 / design §"Persistence Invariants Summary" rule 9), and the `content_digest_sha256` length CHECK.
    - Add the indexes `idx_deliverable_revisions_by_resource` and `idx_deliverable_revisions_by_wa`.
    - _Requirements: 22.1, 22.2, 22.3, 26.1, 26.2, 26.4, 26.5, 41.13, 42.4_

  - [x] 1.4 Extend `Disclosure_Policies` registry with `slice-default-2026` coverage rows for Slice 3 node kinds
    - Create `src/walking_slice/execution/_disclosure.py` exposing `seed_execution_coverage(connection)` that inserts one `Disclosure_Policy_Coverage` row per Slice 3 node kind (`work_assignment_record`, `work_event_record`, `time_entry_record`, `deliverable_resource`, `deliverable_revision`, `deliverable_production_record`, `milestone_acceptance_record`, `completion_record`) with `policy_id = 'slice-default-2026'`, the recorded date, and `backlog_adr_id = 'ADR-HT-014'`.
    - Confirm `walking_slice.disclosure.policy_for(node_kind)` resolves these new rows via the existing Slice 2 lookup behavior; do not modify the Slice 1 or Slice 2 row contents.
    - _Requirements: 38.1, 38.2, 38.3, 38.4, 38.5, 40.2_

  - [x] 1.5 Write unit tests for the additive Slice 1 + Slice 2 extensions
    - Cover: `assign`, `contribute`, `accept_milestone`, `complete` are accepted by `AuthorizationService.assign_role` and required by their seven action types; existing actions still demand their pre-Slice-3 authority; the eight execution / deliverable tables reject UPDATE and DELETE; the `Time_Entry_Records` decimal-effort CHECK rejects `24.01`, `-0.01`, three fractional digits, and accepts boundary values `0.00` / `24.00`; the partial UNIQUE index rejects a second `started` event per Work Assignment; the Slice 3 disclosure-policy coverage rows are visible via `walking_slice.disclosure.policy_for(node_kind)`; the existing Slice 1 + Slice 2 disclosure rows are byte-equivalent after Slice 3 seeding.
    - _Requirements: 25.2, 32.1, 32.10, 32.11, 38.1, 40.1, 40.2_

- [x] 2. Add additive Planning_Service and Deliverable_Expectation read APIs (AD-WS-30)
  - [x] 2.1 Add `PlanRevisionService.get_plan_revision` read function
    - Add a read-only method `get_plan_revision(connection, plan_revision_id)` on `src/walking_slice/planning/plan_revisions.py` performing a single indexed `SELECT plan_revision_id, lifecycle_state, activity_plan_id, applicable_scope FROM Plan_Revisions WHERE plan_revision_id = :id`.
    - Return a frozen `PlanRevisionRow` value object; do not introduce any write path.
    - _Requirements: 23.2, 23.4, 40.1_

  - [x] 2.2 Add `DeliverableExpectationService.get_revision` read function and `ProjectResolver`
    - Add a read-only method `get_revision(connection, deliverable_expectation_revision_id)` on `src/walking_slice/planning/deliverable_expectations.py` returning the row with its target Project Identity.
    - Create `src/walking_slice/planning/_project_resolver.py` exposing `ProjectResolver.resolve_project(connection, plan_revision_id) -> project_id` that follows Plan Revision → Activity Plan → Project via existing Slice 2 tables.
    - _Requirements: 27.3, 27.4, 40.1_

  - [x] 2.3 Write unit tests for the additive Planning read APIs
    - Cover: `get_plan_revision` returns the correct `lifecycle_state` for `draft` and `approved` Plan Revisions; `get_revision` returns target Project Identity; `ProjectResolver.resolve_project` returns the correct Project Identity for a known Plan Revision and raises a structured error when the Plan Revision is unresolvable.
    - _Requirements: 23.2, 23.4, 27.3_

- [x] 3. Implement Slice 3 value objects and shared helpers
  - [x] 3.1 Create `walking_slice.execution.models`
    - Create `src/walking_slice/execution/models.py` containing the frozen Pydantic value objects from design §"In-Memory Value Objects": `WorkAssignmentRef`, `WorkEventRef`, `TimeEntryRef`, `DeliverableProductionRef`, `MilestoneAcceptanceRef`, `CompletionRef`.
    - Reuse `AuthorityBasisRef`, `TargetRef`, `ProvenanceNode`, `GapDescriptor`, `ProjectionEnvelope`, and `Clock` from existing Slice 1 / Slice 2 modules; do not redefine them.
    - _Requirements: 22.1, 22.2, 23.3, 24.2, 25.2, 27.2, 28.2, 29.2_

  - [x] 3.2 Create `walking_slice.deliverables.models`
    - Create `src/walking_slice/deliverables/models.py` containing the frozen Pydantic value objects `DeliverableRef` and `DeliverableRevisionRef` per design §"In-Memory Value Objects".
    - _Requirements: 22.1, 22.2, 26.2, 41.13_

  - [x] 3.3 Create shared Execution_Service helpers
    - Create `src/walking_slice/execution/_helpers.py` with: a `_record_execution_artifact(connection, registry_kind, resource_kind, identifier, ...)` helper that calls `IdentityService.reject_if_duplicate` and inserts into `Identifier_Registry` with the Slice 3 `resource_kind` tag (covering Requirements 22.8 and 26.3); a `_reject_prohibited_attributes(request_body, prefixes)` helper extending the Slice 2 helper to also reject planning-attribute prefixes `{planned-, planning-assumption-, ordering-rationale-, plan-review-, plan-approval-}` per Requirement 33 and observed-outcome prefixes `{observed-, measurement-, outcome-review-, attribution-evidence-, success-condition-assessment-}` per Requirement 34.
    - _Requirements: 22.8, 26.3, 33.2, 33.3, 33.4, 34.1, 34.2, 34.5, 40.3_

- [x] 4. Implement Deliverable_Repository
  - [x] 4.1 Implement `DeliverableRepositoryService.create_produced_deliverable`
    - Create `src/walking_slice/deliverables/repository.py` exposing the `DeliverableRepositoryService` dataclass from design §"Deliverable_Repository".
    - Validate `content_bytes` length in 1..100 MB; validate `content_type` against the enumerated set `{text/markdown, text/plain, application/pdf, application/json, image/png, image/svg+xml, application/octet-stream}`; validate `produced_deliverable_name` length 1..200.
    - Reject any prohibited planning-attribute or observed-outcome key via `_reject_prohibited_attributes` per Requirements 33.3, 34.2.
    - Compute SHA-256 content digest at write time; tag the issued Deliverable Resource and Revision identifiers with `resource_kind` in `{deliverable_resource, deliverable_revision}` (Requirement 22.8, Requirement 26.3, disjointness from Slice 1 Source Evidence).
    - Evaluate `Authorization_Service.evaluate(party, "create.produced_deliverable", target=work_assignment_record, at=now())`; on deny, use the Slice 2 separate-transaction Denial Record pattern with 3-attempt retry per Requirement 30.6.
    - On permit, perform the AD-WS-29 two-stage check: re-read the persisted `Work_Assignment_Records` row and require `assignee_party_id == authoring_party_id`; on mismatch roll back and append a Denial Record with `reason_code = 'no-role-assignment'`.
    - On permit, insert `Deliverable_Resources`, `Deliverable_Revisions` (with `role_marker = 'generated_output'`), and the consequential `Audit_Records` row inside one transaction.
    - _Requirements: 22.1, 22.2, 22.3, 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7, 26.8, 32.7, 33.3, 34.2, 41.13_

  - [x] 4.2 Implement Deliverable_Repository read APIs
    - Add `get_revision(connection, deliverable_revision_id)` returning the full row with digest, role marker, and `originating_work_assignment_id`.
    - Add `get_revision_text(connection, deliverable_revision_id)` returning the byte-equivalent content bytes and a digest comparison against the stored `content_digest_sha256`.
    - _Requirements: 26.2, 35.8_

  - [x] 4.3 Write unit tests for the Deliverable_Repository
    - Cover: content-bytes boundary values (`1 byte`, `100 MB`, `0 bytes` rejected, `100 MB + 1 byte` rejected); content-type enumeration rejection; produced-Deliverable name length boundaries (1, 200, 0 rejected, 201 rejected); `role_marker = 'generated_output'` recorded on every Revision; produced-Deliverable Resource Identity is recorded in `Identifier_Registry` with `resource_kind = 'deliverable_resource'`; unresolvable `originating_work_assignment_id` rejected; AD-WS-29 assignee-binding rejection when authoring Party is not the named assignee.
    - _Requirements: 26.1, 26.2, 26.3, 26.5, 26.6, 32.7, 41.13_

- [x] 5. Implement Work Assignments service
  - [x] 5.1 Implement `WorkAssignmentService.create_work_assignment`
    - Create `src/walking_slice/execution/work_assignments.py` exposing the `WorkAssignmentService` dataclass from design §"Execution_Service.WorkAssignments".
    - Validate inputs per Requirement 23.3 (assignment-rationale 0..4000, authority basis type drawn from AD-WS-10 set, applicable_scope present); reject any prohibited planning-attribute or observed-outcome key via `_reject_prohibited_attributes`.
    - Reject self-assignment (`assignment_authority_party_id == assignee_party_id`) per Requirement 23.5.
    - Resolve the target Plan Revision via `planning_reader.get_plan_revision(...)`; reject when unresolvable, `lifecycle_state != 'approved'`, or scope is outside the requesting Party's applicable Assignment Authority scope (Requirement 23.4).
    - Resolve the assignee Party; reject if unresolvable or inactive (Requirement 23.5).
    - Evaluate `Authorization_Service.evaluate(party=assignment_authority_party_id, action="create.work_assignment", ...)`; on deny use the AD-WS-9 separate-transaction Denial Record pattern with 3-attempt retry.
    - On permit, open the caller's transaction, insert `Work_Assignment_Records`, the `Addresses` Relationship to the Plan Revision (with `semantic_role IS NULL`), the `Relates To` Relationship to the assignee Party (with `semantic_role = 'assignee'`), and the consequential `Audit_Records` row.
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 23.7, 23.8, 23.9, 32.6, 33.4, 34.5, 41.1, 41.2_

  - [x] 5.2 Write unit tests for `WorkAssignmentService.create_work_assignment`
    - Cover: target Plan Revision unresolvable / `draft` / `approved` / scope-mismatch outcomes; assignee unresolvable / inactive / self-assignment rejections; assignment-rationale length boundaries (0, 4000, 4001 rejected); authority-basis-type enumeration validation; prohibited-attribute rejection; authorization deny path producing exactly one Denial Record; immutability rejection of an UPDATE / DELETE attempt; one `Addresses` and one `Relates To` Relationship row per Work Assignment.
    - _Requirements: 23.3, 23.4, 23.5, 23.6, 23.7, 23.9, 32.6, 33.4_

- [x] 6. Implement Work Events service with state machine
  - [x] 6.1 Implement `WorkEventService.create_work_event`
    - Create `src/walking_slice/execution/work_events.py` exposing the `WorkEventService` dataclass from design §"Execution_Service.WorkEvents".
    - Validate `event_kind` against `{started, progress_note, paused, resumed, deliverable_drafted}` and `event_note` length 0..4000; reject any prohibited planning-attribute or observed-outcome key.
    - Enforce the per-Work-Assignment state machine described in design §"Event-kind state machine": `started` rejected if any prior `started` exists; `progress_note`/`paused`/`deliverable_drafted` rejected if no prior `started`; `resumed` rejected if no prior `started` or if the most recent prior event in `{paused, resumed}` is not `paused`. Use the indexed `(target_work_assignment_id, recorded_at DESC)` query.
    - Resolve the target Work Assignment; reject if unresolvable.
    - Evaluate `Authorization_Service.evaluate(party, "create.work_event", target=work_assignment_record, at=now())`; on deny use the AD-WS-9 separate-transaction Denial Record pattern.
    - On permit, perform the AD-WS-29 two-stage check: re-read the persisted `Work_Assignment_Records` row and require `assignee_party_id == recording_party_id`; on mismatch roll back and append a Denial Record with `reason_code = 'no-role-assignment'`.
    - On permit, insert `Work_Event_Records` and the `Relates To` Relationship to the Work Assignment Record (with `semantic_role = 'work_event'`), and the consequential `Audit_Records` row.
    - _Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7, 32.7, 33.4, 34.5, 41.1, 41.2_

  - [x] 6.2 Write unit tests for `WorkEventService.create_work_event`
    - Cover the full state-machine corner cases: single `started`, two `started` attempts (second rejected via partial UNIQUE), `started` → `paused` → `resumed` → `paused` → `resumed`, `resumed` without prior `paused`, `progress_note` before `started`, `deliverable_drafted` before `started`, two concurrent `started` attempts (one wins via SQLite write lock).
    - Cover AD-WS-29 assignee-binding rejection when recording Party is not the named assignee; cover authorization deny path producing exactly one Denial Record.
    - _Requirements: 24.3, 24.4, 24.5, 24.6, 32.7_

- [x] 7. Implement Time Entries service
  - [x] 7.1 Implement `TimeEntryService.create_time_entry`
    - Create `src/walking_slice/execution/time_entries.py` exposing the `TimeEntryService` dataclass from design §"Execution_Service.TimeEntries".
    - Validate `effort_hours` against the ISO-decimal regex and numeric range 0.00..24.00; validate `effort_period_start <= effort_period_end <= recorded_at`; normalize Decimal to two-decimal-place form before persistence.
    - Reject any prohibited planning-attribute or observed-outcome key.
    - Resolve the target Work Assignment; reject if unresolvable.
    - Evaluate `Authorization_Service.evaluate(party, "create.time_entry", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, perform the AD-WS-29 two-stage check; on mismatch roll back and append a Denial Record.
    - On permit, insert `Time_Entry_Records` and the `Relates To` Relationship to the Work Assignment Record (with `semantic_role = 'time_entry'`), and the consequential `Audit_Records` row.
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.5, 25.6, 25.7, 32.7, 41.1, 41.2_

  - [x] 7.2 Write unit tests for `TimeEntryService.create_time_entry`
    - Cover: `effort_hours` boundary values (`0.00`, `24.00`, `24.01` rejected, `-0.01` rejected, three fractional digits rejected); `effort_period_start > effort_period_end` rejected; `effort_period_end > recorded_at` rejected; AD-WS-29 assignee-binding rejection; authorization deny path producing exactly one Denial Record.
    - _Requirements: 25.2, 25.3, 25.4, 25.5, 32.7_

- [x] 8. Checkpoint - Slice 3 ingestion services
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Implement Deliverable Productions service
  - [x] 9.1 Implement `DeliverableProductionService.create_deliverable_production`
    - Create `src/walking_slice/execution/deliverable_productions.py` exposing the `DeliverableProductionService` dataclass from design §"Execution_Service.DeliverableProductions".
    - Validate `production_rationale` length 0..4000; reject any prohibited planning-attribute or observed-outcome key.
    - Resolve the source Work Assignment, the produced Deliverable Revision, and the target Deliverable Expectation Revision; reject any unresolvable identifier.
    - Run the project-membership check per Requirement 27.3: walk `wa.target_plan_revision_id` → Activity Plan → Project via `ProjectResolver`, walk `deliverable_expectation_revision.target_project_id`, and require the two Project Identities to match; on mismatch return `deliverable_expectation_project_mismatch` with no row persisted.
    - Run the originating-binding check per Requirement 27.4: require the produced Deliverable Revision's `originating_work_assignment_id == source_work_assignment_id`; on mismatch reject.
    - Evaluate `Authorization_Service.evaluate(party, "create.deliverable_production", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, perform the AD-WS-29 two-stage check; on mismatch roll back and append a Denial Record.
    - On permit, insert `Deliverable_Production_Records`, one `Produces` Relationship to the produced Deliverable Revision (with `semantic_role IS NULL`), one `Addresses` Relationship to the target Deliverable Expectation Revision (with `semantic_role IS NULL`), one `Relates To` Relationship to the source Work Assignment Record (with `semantic_role = 'production_source'`), and the consequential `Audit_Records` row.
    - _Requirements: 27.1, 27.2, 27.3, 27.4, 27.5, 27.6, 27.7, 32.7, 41.1, 41.2_

  - [x] 9.2 Write unit tests for `DeliverableProductionService.create_deliverable_production`
    - Cover: cross-Project mismatch rejected (Deliverable Expectation in a different Project than the source Work Assignment's Plan Revision); forged-production rejected (produced Deliverable Revision's `originating_work_assignment_id` does not match `source_work_assignment_id`); AD-WS-29 assignee-binding rejection; immutability rejection of an UPDATE / DELETE attempt on the Record or its Relationships; one `Produces`, one `Addresses`, and one `Relates To` Relationship per Production Record.
    - _Requirements: 27.3, 27.4, 27.5, 27.7, 32.7_

- [x] 10. Implement Milestone Acceptances service
  - [x] 10.1 Implement `MilestoneAcceptanceService.create_milestone_acceptance`
    - Create `src/walking_slice/execution/milestone_acceptances.py` exposing the `MilestoneAcceptanceService` dataclass from design §"Execution_Service.MilestoneAcceptances".
    - Validate `outcome` against `{Accept, Reject}` and `rationale` length 1..4000; validate `authority_basis.type` against AD-WS-10 set; reject any prohibited planning-attribute or observed-outcome key.
    - Resolve the source Deliverable Production Record; resolve the produced Deliverable Revision and target Deliverable Expectation Revision from its `Produces` and `Addresses` Relationships; reject any unresolvable identifier.
    - Pre-check the `UNIQUE(source_deliverable_production_id)` constraint per Requirement 28.3 and return `milestone_acceptance_already_exists` (structured 409) when a Milestone Acceptance already exists; the response carries the existing `milestone_acceptance_id` only when the caller holds view authority on it (AD-WS-9).
    - Evaluate `Authorization_Service.evaluate(party, "create.milestone_acceptance", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, insert `Milestone_Acceptance_Records`, one `Addresses` Relationship to the produced Deliverable Revision (with `semantic_role IS NULL`), and the consequential `Audit_Records` row inside one transaction.
    - Do not change the source Deliverable Production Record, produced Deliverable Revision, target Deliverable Expectation Revision, or any Slice 1 / Slice 2 row (Requirement 28.8).
    - _Requirements: 28.1, 28.2, 28.3, 28.4, 28.5, 28.6, 28.7, 28.8, 32.8, 41.1, 41.10_

  - [x] 10.2 Write unit tests for `MilestoneAcceptanceService.create_milestone_acceptance`
    - Cover: duplicate-Milestone-Acceptance rejection (UNIQUE constraint at both pre-check and DB layer); `outcome` enumeration boundaries; rationale length boundaries (1, 4000, 0 rejected, 4001 rejected); authority-basis-type enumeration; immutability rejection of UPDATE / DELETE; Slice 1 / Slice 2 row byte-equivalence after a Milestone Acceptance.
    - _Requirements: 28.3, 28.4, 28.7, 28.8, 32.8_

- [x] 11. Implement Completions service
  - [x] 11.1 Implement `CompletionService.create_completion`
    - Create `src/walking_slice/execution/completions.py` exposing the `CompletionService` dataclass from design §"Execution_Service.Completions".
    - Validate `outcome` against `{Completed, Completed_With_Reservation}` and `rationale` length 1..4000; validate `authority_basis.type` against AD-WS-10 set; reject any prohibited planning-attribute or observed-outcome key.
    - Resolve the target Plan Revision; reject if unresolvable or `lifecycle_state != 'approved'` per Requirement 29.4.
    - Run the accepted-Milestone existence check per Requirement 29.1 / 29.4 using the covering SQL from design §"Accepted-Milestone existence check"; require `COUNT >= 1`. When `source_milestone_acceptance_ids` is supplied, require every identifier to resolve to an `Accept`-outcome row in the result set.
    - Pre-check the `UNIQUE(target_plan_revision_id)` constraint per Requirement 29.3 and return `completion_already_exists` when present; carry the existing `completion_id` only when the caller holds view authority on it (AD-WS-9).
    - Evaluate `Authorization_Service.evaluate(party, "create.completion", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, insert `Completion_Records`, one `Addresses` Relationship to the target Plan Revision (with `semantic_role IS NULL`), and the consequential `Audit_Records` row inside one transaction. The Completion target Activity Plan / Project Identities are resolved via `ProjectResolver` and persisted on the row.
    - Do not change the target Plan Revision, Activity Plan, Project, Objective, Intended Outcome, Deliverable Expectation, Plan Approval Record, or any Slice 1 row (Requirement 29.7).
    - _Requirements: 29.1, 29.2, 29.3, 29.4, 29.5, 29.6, 29.7, 29.8, 32.9, 41.1, 41.10_

  - [x] 11.2 Write unit tests for `CompletionService.create_completion`
    - Cover: target Plan Revision unresolvable / `draft` / `approved` outcomes; zero accepted Milestones with empty `source_milestone_acceptance_ids` rejected; supplied identifier list with at least one non-Accept entry rejected; duplicate-Completion rejection (UNIQUE constraint); `outcome` enumeration; rationale length boundaries; immutability rejection of UPDATE / DELETE; Slice 1 + Slice 2 row byte-equivalence after a Completion; explicit assertion that a Completion does not assert any observed Outcome (no observed-outcome attribute appears on the persisted row).
    - _Requirements: 29.1, 29.3, 29.4, 29.7, 29.8, 32.9, 34.3_

- [x] 12. Extend Provenance_Navigator additively for Slice 3
  - [x] 12.1 Implement `navigate_completion` traversal
    - Add `navigate_completion(connection, completion_id, party, at)` to `src/walking_slice/provenance.py` as an additive function. Do not modify the existing `navigate_decision` or `navigate_plan_approval` functions.
    - Walk Completion → Plan Approval (via Plan Revision Identity) → existing `navigate_plan_approval` for the Planning chain; also walk Completion → Milestone Acceptance(s) → Deliverable Production(s) → produced Deliverable Revision(s); also walk Completion → Work Assignment → Work Event(s) → Time Entry(ies).
    - Apply the `slice-default-2026` Completeness Disclosure policy (now covering Slice 3 node kinds via task 1.4) for restricted-vs-nonexistent observability.
    - Return identical results across repeated invocations for the same `(completion_id, party, at)` (Requirement 31.4 — idempotent retrieval).
    - _Requirements: 31.1, 31.2, 31.3, 31.4, 31.5, 31.6, 35.1, 35.2, 35.3, 35.4, 35.5, 35.6, 35.7, 35.8_

  - [x] 12.2 Implement `navigate_deliverable_production` and `navigate_produced_deliverable_revision`
    - Add the two additive functions to `src/walking_slice/provenance.py`. Each walks from the requested anchor back through Slice 3 → Slice 2 → Slice 1 to the originating Decision and the exact Document Revision text.
    - For produced Deliverable Revision nodes, include the role marker `generated_output` and the content digest per Requirement 35.8.
    - _Requirements: 35.1, 35.2, 35.5, 35.8_

  - [x] 12.3 Extend backlinks to cover Slice 3 node kinds
    - Extend the existing `_authorized_source_kinds` (or equivalent set) in `src/walking_slice/provenance.py` to include `'work_assignment_record'`, `'work_event_record'`, `'time_entry_record'`, `'deliverable_resource'`, `'deliverable_revision'`, `'deliverable_production_record'`, `'milestone_acceptance_record'`, and `'completion_record'` so the existing constant-time backlink algorithm returns Slice 3 relationships when the queried endpoint is a Slice 3 node.
    - Do not change the algorithm itself; this is an additive coverage extension only.
    - _Requirements: 36.1, 36.2, 36.3, 36.4, 36.5, 36.6_

  - [x] 12.4 Write unit tests for the Slice 3 traversals and backlink extension
    - Cover: `navigate_completion` returns the three ordered chains with all node identities resolving; restricted nodes are replaced with `{kind, redacted: true}` markers; unresolved / stale / unavailable nodes return gap descriptors with `stage`, `category`, and (when visible) next reachable identity; idempotent retrieval across 5 repetitions; backlink queries for Slice 3 node kinds return relationships with `semantic_role` populated correctly; produced Deliverable Revision content digest and `role_marker` are included in the chain.
    - _Requirements: 31.2, 31.3, 31.4, 35.2, 35.3, 35.4, 35.8, 36.1, 36.2, 36.6_

- [x] 13. Implement execution-status Projection
  - [x] 13.1 Implement `project_execution_status` and `ExecutionStatusProjection`
    - Create `src/walking_slice/execution/_projection.py` exposing `ExecutionStatusProjection` and `project_execution_status(connection, plan_revision_id, party_id, at)`.
    - Derive the projected status from source Records per design §"Execution_Service Status Projection": `Plan Revision approved` (no Work Event), `Plan Revision in execution` (at least one `started`), `Plan Revision execution paused` (most-recent event is `paused`), `Plan Revision deliverable produced` (at least one Deliverable Production), `Plan Revision milestone accepted` (at least one Accept Milestone), `Plan Revision completion recorded` (Completion Record exists), `Provenance incomplete` (source Record unresolvable).
    - Wrap the response in the existing Slice 1 `ProjectionEnvelope` value object from `walking_slice.projection` with Projection Definition, source Record Identities, source Revision Identities, applicable temporal boundary (ISO-8601 second precision), generated time, and derivation indicator.
    - On unresolvable Projection Definition or missing source Record, withhold the projected status and return an explanation-unavailable indicator identifying the missing element (Requirement 39.5).
    - Never include a derived percent-complete, actual-cost, remaining-work, budget-variance, forecast-cost, or outcome-attainment value (Requirement 39.3).
    - Never label or alias the projection as evidence of an Observed Outcome (Requirement 39.6).
    - _Requirements: 39.1, 39.2, 39.3, 39.4, 39.5, 39.6_

  - [x] 13.2 Write unit tests for the execution-status Projection
    - Cover: each projected status value at the appropriate pipeline stage; envelope carries every required field; unresolvable-definition path withholds status and returns the explanation-unavailable indicator; source Records remain byte-equivalent when a correction arrives; absence of prohibited derived fields in the response body.
    - _Requirements: 39.1, 39.2, 39.3, 39.4, 39.5, 39.6_

- [x] 14. Implement Interim ADR seeding for Slice 3 gaps
  - [x] 14.1 Seed Interim_ADR_Records for Gaps G-11 through G-15
    - Create `src/walking_slice/execution/_interim_adr.py` exposing `seed_execution_interim_adr(connection)` that inserts one row per AD-WS-24 (G-11 / `ADR-HT-013`), AD-WS-25 (G-12 / `ADR-HT-014`), AD-WS-26 (G-13 / `ADR-HT-015`), AD-WS-27 (G-14 / `ADR-HT-016`), and AD-WS-28 (G-15 / `ADR-HT-017`).
    - Each row records the motivating Requirement number, motivating criterion number, observable behavior chosen, recorded date, and backlog ADR identifier per Slice 1 Requirement 16.3 and Slice 2 Requirement 19.5.
    - The seeder is idempotent on re-invocation.
    - _Requirements: 40.5, 42.3, 42.4_

  - [x] 14.2 Write unit tests for Slice 3 Interim ADR seeding
    - Cover: every Slice 3 backlog ADR identifier maps to at least one seeded row with the documented fields; re-running the seeder is idempotent; existing Slice 1 + Slice 2 Interim ADR rows are byte-equivalent after Slice 3 seeding.
    - _Requirements: 40.5, 42.3_

- [x] 15. Implement HTTP routes for the Execution_Service and Deliverable_Repository
  - [x] 15.1 Implement Execution_Service HTTP endpoints
    - Create `src/walking_slice/execution/_routes.py` exposing one FastAPI APIRouter under `/api/v1` with the endpoints from design §"Components and Interfaces": Work Assignments (`POST /work-assignments`, `GET /work-assignments/{id}`), Work Events (`POST /work-events`, `GET /work-events/{id}`), Time Entries (`POST /time-entries`, `GET /time-entries/{id}`), Deliverable Productions (`POST /deliverable-productions`, `GET /deliverable-productions/{id}`), Milestone Acceptances (`POST /milestone-acceptances`, `GET /milestone-acceptances/{id}`), Completions (`POST /completions`, `GET /completions/{id}`), and the execution-status Projection endpoint (`GET /plan-revisions/{plan_revision_id}/execution-status`).
    - Wire each route to the corresponding service through the Slice 1 `RequestContext` dependency.
    - Use Pydantic request models with `Config(extra='forbid')` so unknown fields are rejected at the API boundary; combine with `_reject_prohibited_attributes` for the planning-attribute and observed-outcome rejection paths.
    - _Requirements: 23.1, 24.1, 25.1, 27.1, 28.1, 29.1, 30.1, 33.4, 34.5, 39.1_

  - [x] 15.2 Implement Deliverable_Repository HTTP endpoints and provenance routes
    - Create `src/walking_slice/deliverables/_routes.py` exposing the Deliverable endpoints: `POST /deliverables`, `POST /deliverables/{deliverable_id}/revisions`, `GET /deliverables/{deliverable_id}/revisions/{deliverable_revision_id}`, `GET /deliverables/{deliverable_id}/revisions/{deliverable_revision_id}/content`.
    - Extend the existing provenance routes to expose `GET /completions/{completion_id}/provenance`, `GET /deliverable-productions/{id}/provenance`, and `GET /deliverables/{deliverable_id}/revisions/{deliverable_revision_id}/provenance`.
    - _Requirements: 26.1, 26.2, 31.1, 35.1_

  - [x] 15.3 Mount Slice 3 routes into the FastAPI application and wire startup seeders
    - Extend `src/walking_slice/app.py` so the FastAPI application loads `execution._routes` and `deliverables._routes`, calls `execution._persistence.create_execution_schema(engine)` and `deliverables._persistence.create_deliverable_schema(engine)` on startup, calls `execution._disclosure.seed_execution_coverage(connection)`, and calls `execution._interim_adr.seed_execution_interim_adr(connection)`.
    - Do not alter Slice 1 or Slice 2 startup behavior beyond these additive calls.
    - _Requirements: 38.1, 38.5, 40.1, 40.5, 42.3_

- [x] 16. Cross-cutting property tests for Slice 3
  - [x] 16.1 Write property test for Execution-creation success
    - **Property 31: Execution-creation success**
    - **Validates: Requirements 23.1, 23.3, 23.8, 24.1, 24.6, 25.1, 25.5, 26.1, 26.7, 27.1, 27.6, 28.1, 28.6, 29.1, 29.6, 37.1, 41.1**
    - Use Hypothesis strategies for each of the seven execution request bodies; for every authorized valid request assert exactly one Record row (and for produced Deliverables one first Revision row), the prescribed Relationship rows per AD-WS-26, and one consequential `Audit_Records` row are persisted in one transaction with byte-equivalent recorded times.

  - [x] 16.2 Write property test for Execution-Record authority correctness and non-substitution
    - **Property 32: Execution-Record authority correctness and non-substitution**
    - **Validates: Requirements 23.6, 24.5, 25.4, 26.6, 27.5, 28.5, 29.5, 30.3, 32.2, 32.3, 32.4, 32.5, 32.6, 32.7, 32.8, 32.9, 32.10, 32.11, 41.2, 41.3**
    - Generate role assignments varying across effective-start, expiration, revocation, scope, and granted-authority dimensions across all eight values; for every persisted execution Record assert a matching role assignment exists whose `authorities_granted` includes the precise required authority, whose scope covers the target, and whose effective period encloses the recorded time. Assert no persisted Record exists when the recording Party held only a different authority type among the eight; assert Contributor writes additionally require assignee binding.

  - [x] 16.3 Write property test for Execution-Record immutability
    - **Property 33: Execution-Record immutability**
    - **Validates: Requirements 23.9, 24.7, 25.6, 26.4, 27.7, 28.7, 37.3, 37.5, 39.4, 41.4**
    - For each generated full pipeline that reaches Completion, apply Hypothesis-drawn mutation-attempt sequences (UPDATE / DELETE against every Slice 3 table) and assert every Slice 3 Record row, every constituent field, every Relationship sourced from or targeting it, every produced Deliverable Revision (with its `content_digest_sha256`, `role_marker`, `originating_work_assignment_id`, and content bytes), and every Audit_Records row are byte-equivalent across all later observation points.

  - [x] 16.4 Write property test for Approved-Plan-to-Completion traceability
    - **Property 34: Approved-Plan-to-Completion traceability**
    - **Validates: Requirements 23.2, 23.4, 27.3, 29.1, 31.1, 41.1**
    - Generate Completion Records reachable through full pipelines; for every persisted Completion assert (a) the `Addresses` target resolves to an `approved` Plan Revision at the recorded time, (b) at least one Accept-outcome Milestone Acceptance exists whose `Addresses` target resolves to a produced Deliverable Revision whose Production Record's source Work Assignment targets the same Plan Revision, and (c) at least one Work Assignment Record exists whose `Addresses` target equals the Completion's target Plan Revision Identity. Assert no orphan Completion Record exists.

  - [x] 16.5 Write property test for Plan/Execution separation
    - **Property 35: Plan / Execution separation enforced from the execution side**
    - **Validates: Requirements 33.1, 33.2, 33.3, 33.4, 40.3, 40.4, 41.5**
    - Generate request bodies with random keys drawn from prohibited planning-attribute prefixes (`planned-`, `planning-assumption-`, `ordering-rationale-`, `plan-review-`, `plan-approval-`); assert every such request is rejected with no row persisted; assert no row of any Slice 2 planning table is mutated as a consequence of any Slice 3 action.

  - [x] 16.6 Write property test for Output/Outcome separation and Relationship structure
    - **Property 36: Output / Outcome separation enforced from the execution side, and Relationship structure invariants**
    - **Validates: Requirements 23.3, 24.2, 26.2, 27.2, 28.2, 29.2, 29.8, 34.1, 34.2, 34.3, 34.4, 34.5, 41.5, 41.6**
    - Generate request bodies with random keys drawn from prohibited observed-outcome prefixes; assert every such request is rejected; assert every persisted Completion Record carries `outcome ∈ {Completed, Completed_With_Reservation}` and no observed-outcome attribute; assert for every persisted Slice 3 Record the prescribed Relationship rows per AD-WS-26 exist with exact `relationship_type`, `source_kind`, `target_kind`, and `semantic_role` values, and no additional rows of those types exist for the same source.

  - [x] 16.7 Write property test for Execution Provenance Chain end-to-end
    - **Property 37: Execution Provenance Chain end-to-end**
    - **Validates: Requirements 31.2, 31.3, 31.4, 35.1, 35.2, 35.4, 35.5, 35.8, 41.7**
    - Generate full Slice 1 + Slice 2 + Slice 3 pipelines whose chain is fully visible to the requesting Party; navigate from each Completion Record, Deliverable Production Record, and produced Deliverable Revision and assert the three ordered chains return, every identity resolves, the returned Content Region Occurrence span digest matches the recorded digest, the chain is byte-equivalent across 5 repetitions, restricted nodes appear as `{kind, redacted: true}` markers, and unresolved / stale / unavailable nodes return gap descriptors.

  - [x] 16.8 Write property test for Indistinguishable denial across Slice 3 endpoints
    - **Property 38: Indistinguishable denial across Slice 3 endpoints**
    - **Validates: Requirements 30.1, 30.4, 30.5, 30.7, 31.5, 31.6, 35.3, 35.6, 35.7, 36.3, 36.5, 38.2, 38.3, 38.4, 41.8**
    - Generate pairs `(P, P′)` differing only in authority on `R`; assert responses to `P′` for creation, backlink, provenance, projection, and read attempts on `R` are indistinguishable from non-existent-endpoint responses across result count, identifier set, ordering positions, pagination cursors, response size, body keys, error category, error wording, and latency (within 100 ms tolerance).

  - [x] 16.9 Write property test for Backlink bidirectionality for Slice 3 Resources
    - **Property 39: Backlink bidirectionality for Slice 3 Resources**
    - **Validates: Requirements 22.5, 36.1, 36.2, 36.4, 36.6, 41.9**
    - Generate Slice 1 + Slice 2 + Slice 3 relationship graphs; for each requesting Party holding view authority on both `R` and its source endpoint, assert the Provenance_Navigator returns `R` from both the source's outbound and the target's backlink query with identical Relationship attribute values, including the `semantic_role` column.

  - [x] 16.10 Write property test for Milestone Acceptance and Completion uniqueness
    - **Property 40: Uniqueness of Milestone Acceptance and Completion**
    - **Validates: Requirements 28.3, 29.3, 41.10**
    - Generate double-Milestone-Acceptance attempts for arbitrary Deliverable Production Records and double-Completion attempts for arbitrary Plan Revisions; assert only the first attempt persists in each case and the second is rejected; assert the first Record remains byte-equivalent after the second attempt.

  - [x] 16.11 Write property test for Slice 1 and Slice 2 non-modification
    - **Property 41: Slice 1 and Slice 2 non-modification under Slice 3 actions**
    - **Validates: Requirements 22.4, 22.6, 22.7, 22.8, 28.8, 29.7, 33.1, 40.1, 40.2, 40.3, 40.4, 41.11, 41.12**
    - Snapshot every Slice 1 and Slice 2 table before any Slice 3 action; run Hypothesis-drawn Slice 3 operation sequences; assert every Slice 1 + Slice 2 row is byte-equivalent at every observation point, apart from the additive `Disclosure_Policy_Coverage` rows, the additive `Identifier_Registry.resource_kind` values, and the additive `Role_Assignments` enumeration extensions whose new values are only populated for Slice 3 rows.

  - [x] 16.12 Write property test for Audit completeness and atomicity across execution actions
    - **Property 42: Audit completeness and atomicity for consequential and denied execution actions**
    - **Validates: Requirements 23.8, 24.6, 25.5, 25.7, 26.7, 26.8, 27.6, 28.6, 29.6, 30.2, 32.11, 37.1, 37.2, 37.4, 37.6, 41.14**
    - Generate sequences of Slice 3 operations (creations, denied attempts, attempted modifications of finalized Records); assert exactly one consequential `Audit_Records` row per consequential write and exactly one Denial Record per denied attempt, each with the required fields and `correlation_id` consistent with the originating operation; assert `Audit_Records.append_sequence` is monotonically non-decreasing by `recorded_at`; assert denied attempts and audit-append-failure attempts leave no in-flight Slice 3 row persisted.

  - [x] 16.13 Write property test for Produced-Deliverable vs Source-Evidence disjointness
    - **Property 43: Produced-Deliverable vs Source-Evidence disjointness**
    - **Validates: Requirements 22.2, 22.3, 26.3, 35.8, 41.13**
    - Generate Slice 1 Source Documents and Slice 3 produced Deliverables in interleaved sequences; assert the produced Deliverable Resource Identity set is disjoint from the Slice 1 Source Evidence Document Resource Identity set (verified via `Identifier_Registry.resource_kind`); assert every produced Deliverable Revision carries `role_marker = 'generated_output'` and no Source Evidence Document Revision carries this column; assert rename and relocate operations on a produced Deliverable Resource preserve its Resource Identity and every existing Revision Identity unchanged.

  - [x] 16.14 Write property test for Execution-status Projection envelope and contents
    - **Property 44: Execution-status Projection envelope and contents**
    - **Validates: Requirements 39.1, 39.2, 39.3, 39.5, 39.6, 41.5, 41.6**
    - Generate full pipelines at various stages (no execution, started, paused, deliverable produced, milestone accepted, completed); assert every status-bearing response carries a `ProjectionEnvelope` with Projection Definition, source Record Identities, source Revision Identities, applicable temporal boundary, generated time, and derivation indicator; assert absence of derived percent-complete, actual-cost, remaining-work, budget-variance, forecast-cost, or outcome-attainment values; assert unresolvable-definition path withholds the projected status and returns an explanation-unavailable indicator; assert source Records remain byte-equivalent.

  - [x] 16.15 Write property test for Slice 3 Interim ADR records retrievability
    - **Property 45: Slice 3 Interim ADR records retrievability**
    - **Validates: Requirements 40.5, 42.3, 42.4, 41.15**
    - For each backlog ADR identifier in `{ADR-HT-013, ADR-HT-014, ADR-HT-015, ADR-HT-016, ADR-HT-017}`, query `Interim_ADR_Records` and assert at least one row exists with the documented motivating Requirement number, criterion number, observable behavior, recorded date, and backlog ADR identifier. Assert rows are byte-equivalent across observation points and re-running the seeder is idempotent.

  - [x] 16.16 Configure repeatable property runs and seed capture for Slice 3
    - Configure the Slice 3 property tests under the existing Hypothesis profile (`@settings(max_examples=100, deadline=2000)`); enable `--hypothesis-seed` capture on every Slice 3 property test; persist the seed of every property test invocation to the build artifact alongside the Slice 1 and Slice 2 seeds; add a re-execution check that confirms identical pass/fail outcomes and minimal counterexamples for failing properties.
    - _Requirements: 41.15_

- [x] 17. End-to-end HTTP integration tests for Slice 3
  - [x] 17.1 Write end-to-end tests for the Release 1C journey
    - Drive the FastAPI app via `httpx.AsyncClient` and exercise the full pipeline: capture Evidence → create Finding → create Recommendation → record Decision (Slice 1) → create Objective → record Intended Outcome → create Project → declare Deliverable Expectation → create Activity Plan → submit Plan Revision → record Plan Review → approve Plan Revision (Slice 2) → record Work Assignment → record `started` Work Event → record `progress_note` Work Event → record Time Entry → record produced Deliverable Revision → record Deliverable Production → record Milestone Acceptance → record Completion → navigate Execution Provenance Chain back to exact Document Revision text.
    - _Requirements: 23.1, 24.1, 25.1, 26.1, 27.1, 28.1, 29.1, 31.1, 35.1_

  - [x] 17.2 Write end-to-end tests for the named denial demonstrations
    - Drive the FastAPI app and assert: a Contributor attempting a Work Assignment is denied with an AD-WS-9-shaped response and a Denial Record; a Completion-Authority Party attempting a Milestone Acceptance is denied; a Party with `contribute` authority but not the named assignee is denied with `no-role-assignment`; modifying a finalized Completion Record is rejected and a Denial Record is appended; submitting a Work Assignment with a `planned-` attribute is rejected with no row persisted and no Slice 2 row mutated; submitting a Completion with an `observed-` attribute is rejected; submitting a Completion against a non-existent Plan Revision is indistinguishable from submitting against a restricted Plan Revision the caller cannot view.
    - _Requirements: 30.1, 30.4, 30.5, 30.7, 32.7, 33.4, 34.5, 37.5_

- [x] 18. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; the core implementation tasks (no `*`) are mandatory.
- Property tests directly map to design §"Correctness Properties" — every property in the Slice 3 suite (31 through 45) has its own sub-task under task 16.
- The Interim ADR records seeded in task 14.1 close Gaps G-11 through G-15 from requirements.md and unblock acceptance of the new backlog ADRs (HT-013, HT-014, HT-015, HT-016, HT-017) without re-implementing the slice.
- Checkpoints sit after the ingestion-services layer (task 8) and at the end (task 18); each is a manual verification gate.
- Requirement 40 (Slice 1 and Slice 2 non-modification) is enforced both by code structure (additive `Disclosure_Policy_Coverage` rows, additive enumeration values, additive read-only functions on `walking_slice.planning` and `walking_slice.provenance`) and by Property 41 (Slice 1 and Slice 2 non-modification) running at every observation point.
- All testing uses `pytest` with Hypothesis configured per design §"Testing Strategy"; each property test runs at least 100 generated cases with deterministic seed capture per Requirement 41.15.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.2", "1.3", "2.1", "2.2", "3.1", "3.2"] },
    { "id": 1,  "tasks": ["1.4", "3.3"] },
    { "id": 2,  "tasks": ["1.5", "2.3"] },
    { "id": 3,  "tasks": ["4.1", "5.1"] },
    { "id": 4,  "tasks": ["4.2", "5.2"] },
    { "id": 5,  "tasks": ["4.3", "6.1", "7.1"] },
    { "id": 6,  "tasks": ["6.2", "7.2"] },
    { "id": 7,  "tasks": ["9.1"] },
    { "id": 8,  "tasks": ["9.2", "10.1"] },
    { "id": 9,  "tasks": ["10.2", "11.1"] },
    { "id": 10, "tasks": ["11.2", "12.1", "12.2", "12.3", "13.1", "14.1"] },
    { "id": 11, "tasks": ["12.4", "13.2", "14.2"] },
    { "id": 12, "tasks": ["15.1", "15.2"] },
    { "id": 13, "tasks": ["15.3"] },
    { "id": 14, "tasks": ["16.1", "16.2", "16.3", "16.4", "16.5", "16.6", "16.7", "16.8", "16.9", "16.10", "16.11", "16.12", "16.13", "16.14", "16.15", "16.16"] },
    { "id": 15, "tasks": ["17.1", "17.2"] }
  ]
}
```
