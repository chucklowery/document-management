# Implementation Plan: Fourth Walking Slice

## Overview

This plan implements the *fourth walking slice* of the Organizational Knowledge and Work System: the additive Outcome_Service specified in [`design.md`](./design.md) that satisfies the requirements in [`requirements.md`](./requirements.md). The slice extends the cumulative Slice 1 + Slice 2 + Slice 3 modular monolith with one new `walking_slice.outcome/` package, four additive authority enumeration values (`define_measurement`, `record_measurement`, `assess_outcome`, `issue_outcome_review`), seven new insert-only tables, seven additive `Disclosure_Policy_Coverage` rows (one carrying the imported-Measurement-Record restricted-attributes payload), additive `navigate_*` and `_authorized_source_kinds` extensions on `walking_slice.provenance`, and one additive `IntendedOutcomeService.get_revision(...)` read API on `walking_slice.planning` — and adds the Outcome Measurement Provenance Chain traversal from Outcome Review back through Measurement to the originating Slice 1 Decision and the parallel Completion → produced Deliverable leg.

The plan is incremental and additive. It begins by extending Slice 1's authority enumeration and seeding the new schema and disclosure-policy coverage (the smallest changes that unblock everything else), then adds the additive Planning read API (AD-WS-40) plus the shared value objects and helpers, then builds the five Outcome_Service write services in dependency order (Measurement Definition → Measurement Record → Observed Outcome → Success-Condition Assessment → Outcome Review), then layers the outcome-aware provenance and backlink extensions, the outcome-status Projection, and the Interim ADR seeding, and finishes with HTTP composition, cross-cutting property tests, and end-to-end integration tests. Every correctness property from design §"Correctness Properties" (Properties 46 through 60) gets its own property-test sub-task annotated with the property number and the Requirements clauses it validates.

Throughout, the plan honors Requirement 60 (Reuse and Non-Modification of Slice 1, Slice 2, and Slice 3 Contexts): the only prior-slice modifications are the additive four-value authority enumeration extension (AD-WS-33), the additive `Disclosure_Policy_Coverage` rows (AD-WS-34), the additive `Identifier_Registry.resource_kind` values (AD-WS-37), and additive read-only functions on `walking_slice.planning` and `walking_slice.provenance`. No Slice 1, Slice 2, or Slice 3 function, class, table, trigger, or invariant is removed, renamed, or narrowed.

## Tasks

- [x] 1. Extend Slice 1 + Slice 2 + Slice 3 enumerations, schema, and disclosure coverage (additive only)
  - [x] 1.1 Add `define_measurement`, `record_measurement`, `assess_outcome`, `issue_outcome_review` to the authority enumeration
    - In `src/walking_slice/authorization.py`, extend the `_VALID_AUTHORITIES` constant to include the literal values `"define_measurement"`, `"record_measurement"`, `"assess_outcome"`, `"issue_outcome_review"` alongside the existing eight values, producing the cumulative twelve-value set. Do not remove or rename any existing value.
    - Extend the `_required_authority` mapping with the five Slice 4 action types: `create.measurement_definition` → `define_measurement`; `create.measurement_record` → `record_measurement`; `create.observed_outcome` → `assess_outcome`; `create.success_condition_assessment` → `assess_outcome`; `create.outcome_review` → `issue_outcome_review`.
    - Do not change the non-substitution behavior of `AuthorizationService.evaluate` (it must continue to require the exact authority named by the action's mapping).
    - _Requirements: 52.1, 52.2, 52.3, 52.4, 52.5, 52.6, 52.7, 52.8, 52.9, 52.10, 52.11, 60.2_

  - [x] 1.2 Create the Outcome_Service schema and append-only triggers
    - Create `src/walking_slice/outcome/__init__.py` and `src/walking_slice/outcome/_persistence.py` exposing `create_outcome_schema(engine)` that issues every `CREATE TABLE`, `CREATE TRIGGER`, and `CREATE INDEX` statement from design §"Data Models — Schema Additions" for `Measurement_Definitions`, `Measurement_Definition_Revisions`, `Measurement_Records`, `Observed_Outcomes`, `Observed_Outcome_Revisions`, `Success_Condition_Assessment_Records`, and `Outcome_Review_Records`.
    - Add `UPDATE` and `DELETE` triggers that reject mutation on every new table, matching the Slice 1 AD-WS-4, Slice 2 AD-WS-19, and Slice 3 AD-WS-27 patterns (AD-WS-36, AD-WS-37).
    - Add the `Measurement_Definitions.target_intended_outcome_resource_id` UNIQUE column (Requirement 44.3) and the `Outcome_Review_Records.target_intended_outcome_revision_id` UNIQUE column (Requirement 49.3).
    - Add the `Measurement_Records` `origin` CHECK, the native-vs-imported source-system attribute CHECK, the `observation_time <= recorded_at` CHECK, and the AD-WS-39 partial `idx_measurement_records_import_idempotency` unique index scoped `WHERE origin = 'imported'` (Requirement 46.3).
    - Add the `Observed_Outcome_Revisions.outcome_kind = 'observed'` CHECK, the `predecessor_revision_id` self-reference, and the `idx_oo_revisions_one_successor` partial unique index keeping the chain linear (AD-WS-36, Requirement 47).
    - Add the `Success_Condition_Assessment_Records` `assessment_category` CHECK and the Unassessable `length >= 200` CHECK (Requirement 48.3), and the `Outcome_Review_Records` `review_outcome` / `attribution_stance` / `confidence` CHECKs and the Asserted/Contradicted non-empty attribution-evidence CHECK (Requirement 49.4).
    - Register the seven new `resource_kind` values on `Identifier_Registry` per AD-WS-37; add every index named in design §"Data Models — Schema Additions".
    - _Requirements: 43.8, 44.3, 45.3, 46.2, 46.3, 46.4, 47.7, 48.3, 48.6, 49.3, 49.4, 49.7, 57.3, 60.3_

  - [x] 1.3 Extend `Disclosure_Policies` registry with `slice-default-2026` coverage rows for Slice 4 node kinds
    - Create `src/walking_slice/outcome/_disclosure.py` exposing `seed_outcome_coverage(connection)` that inserts one `Disclosure_Policy_Coverage` row per Slice 4 node kind (`measurement_definition`, `measurement_definition_revision`, `measurement_record`, `observed_outcome`, `observed_outcome_revision`, `success_condition_assessment_record`, `outcome_review_record`) with `policy_id = 'slice-default-2026'`, the recorded date, and `backlog_adr_id = 'ADR-HT-020'`.
    - Populate the `restricted_attributes_json` payload on the `measurement_record` coverage row only, naming `source_system_id`, `source_system_record_id`, `source_system_authority`, `source_system_retrieval_at`, and `import_at` as restricted attributes per AD-WS-34 / Requirement 58.5; for an unauthorized requester the whole Record is replaced with `{"kind": "measurement_record", "redacted": true}`.
    - Confirm `walking_slice.disclosure.policy_for(node_kind)` resolves these new rows via the existing Slice 2 lookup behavior; do not modify any Slice 1, Slice 2, or Slice 3 row contents.
    - _Requirements: 58.1, 58.2, 58.3, 58.4, 58.5, 60.2_

  - [x] 1.4 Write unit tests for the additive enumeration, schema, and disclosure extensions
    - Cover: the four new authority values are accepted by `AuthorizationService.assign_role` and required by their five action types; existing actions still demand their pre-Slice-4 authority; the seven outcome tables reject UPDATE and DELETE; the `Measurement_Records` native/imported CHECK rejects a native row carrying any source-system attribute and an imported row missing one, and rejects `source_system_authority` outside the enumerated set; the AD-WS-39 partial unique index rejects a duplicate imported `(source_system_id, source_system_record_id)` pair per Definition Revision; the `Observed_Outcome_Revisions` linear-chain index rejects a second successor per predecessor; the Unassessable `>= 200`-char CHECK and the Asserted/Contradicted non-empty attribution-evidence CHECK fire; the seven disclosure-policy coverage rows are visible via `walking_slice.disclosure.policy_for(node_kind)`; existing Slice 1 + Slice 2 + Slice 3 disclosure rows are byte-equivalent after Slice 4 seeding.
    - _Requirements: 43.8, 46.3, 46.4, 47.7, 48.3, 49.4, 52.1, 52.10, 52.11, 58.1, 60.1, 60.2_

- [x] 2. Add the additive Planning_Service read API (AD-WS-40)
  - [x] 2.1 Add `IntendedOutcomeService.get_revision` read function
    - Add a read-only method `get_revision(connection, intended_outcome_revision_id)` on the appropriate module in `src/walking_slice/planning/` performing a single indexed `SELECT` that returns the Intended Outcome Revision row including its `outcome_kind` attribute and target Intended Outcome Resource Identity.
    - Return a frozen value object; do not introduce any write path on the Planning_Service.
    - _Requirements: 44.4, 47.4, 48.3, 49.4, 60.1_

  - [x] 2.2 Write unit tests for the additive Planning read API
    - Cover: `get_revision` returns the correct `outcome_kind` (`intended`) and target Intended Outcome Resource Identity for a known Intended Outcome Revision; returns a structured not-found indication for an unresolvable identifier; performs no mutation of any Slice 2 row.
    - _Requirements: 44.4, 47.4, 60.1_

- [x] 3. Implement Slice 4 value objects and shared helpers
  - [x] 3.1 Create `walking_slice.outcome.models`
    - Create `src/walking_slice/outcome/models.py` containing the frozen Pydantic value objects from design §"In-Memory Value Objects": `MeasurementDefinitionRef`, `MeasurementRecordRef`, `ObservedOutcomeRef`, `SuccessConditionAssessmentRef`, and `OutcomeReviewRef`, plus the per-service result objects (`CreateMeasurementDefinitionResult`, `CreateMeasurementRecordResult`, `CreateObservedOutcomeResult`, `CreateAssessmentResult`, `CreateOutcomeReviewResult`) and the row value objects (`MeasurementDefinitionRow`).
    - Reuse `AuthorityBasisRef`, `TargetRef`, `ProvenanceNode`, `GapDescriptor`, `ProjectionEnvelope`, `RequestContext`, and `Clock` from existing Slice 1–3 modules; do not redefine them.
    - _Requirements: 43.2, 44.2, 45.1, 46.1, 47.1, 48.1, 49.1_

  - [x] 3.2 Create the shared Outcome_Service prohibited-attribute helper
    - Create `src/walking_slice/outcome/_helpers.py` with `_reject_prohibited_attributes(request_body, prefixes)` that rejects any top-level request key matching a prohibited intended-side prefix (`success-condition-`, `attribution-assumption-`, `planned-`, `plan-review-`, `plan-approval-`, `milestone-acceptance-outcome-`, `completion-outcome-`, `intended-`) and any field whose stated purpose is to assert Outcome from Completion or to alias a Completion Record as an Observed Outcome, returning a 400 with no row persisted (Requirements 53, 54).
    - Add a `_record_outcome_artifact(connection, kind, resource_kind, identifier, ...)` helper that calls the existing `IdentityService` duplicate-rejection path and inserts into `Identifier_Registry` with the correct Slice 4 `resource_kind` tag (AD-WS-37, Requirement 43.1/43.4/43.8).
    - _Requirements: 43.1, 43.4, 43.8, 53.2, 53.3, 54.1, 54.4_

- [x] 4. Implement Measurement Definitions service
  - [x] 4.1 Implement `MeasurementDefinitionService.create_measurement_definition`
    - Create `src/walking_slice/outcome/measurement_definitions.py` exposing the `MeasurementDefinitionService` dataclass from design §"Outcome_Service.MeasurementDefinitions", plus the `get_definition_for_intended_outcome(...)` read.
    - Validate inputs per Requirement 44.2 (measurand description 1..4000, unit 1..200, observation window / cadence / data source 1..1000, applicable scope present); reject any prohibited intended-side key via `_reject_prohibited_attributes`.
    - Resolve the target Intended Outcome Revision via `intended_outcome_reader.get_revision(...)`; reject when unresolvable or `outcome_kind != 'intended'` (Requirement 44.4); reject when more than one target is named.
    - Uniqueness pre-check: reject when a Measurement Definition Resource already addresses the same target Intended Outcome Resource (Requirement 44.3), backed by the DB `UNIQUE(target_intended_outcome_resource_id)`.
    - Evaluate `Authorization_Service.evaluate(party=authoring_party_id, action="create.measurement_definition", target=intended_outcome_ref, at=now())`; on deny, use the cumulative separate-transaction Denial Record pattern (AD-WS-9) with 3-attempt retry per Requirement 50.6.
    - On permit, open the caller's transaction and insert `Measurement_Definitions`, the initial immutable `Measurement_Definition_Revisions`, one `Addresses` Relationship to the target Intended Outcome Revision (`semantic_role IS NULL`), and the consequential `Audit_Records` row.
    - _Requirements: 44.1, 44.2, 44.3, 44.4, 44.5, 44.6, 44.7, 52.6, 53.2, 57.1_

  - [x] 4.2 Write unit tests for `MeasurementDefinitionService.create_measurement_definition`
    - Cover: target Intended Outcome Revision unresolvable / `outcome_kind != 'intended'` / multi-target rejections; measurand / unit / window / cadence / data-source length boundaries; duplicate Measurement Definition against the same Intended Outcome Resource rejected with the first left byte-equivalent; prohibited-attribute rejection; authorization deny path producing exactly one Denial Record; immutability rejection of an UPDATE / DELETE attempt; exactly one `Addresses` Relationship per Revision.
    - _Requirements: 44.2, 44.3, 44.4, 44.7, 52.6, 53.2_

- [x] 5. Implement Measurement Records service (native and imported)
  - [x] 5.1 Implement `MeasurementRecordService` native and imported creation
    - Create `src/walking_slice/outcome/measurement_records.py` exposing the `MeasurementRecordService` dataclass from design §"Outcome_Service.MeasurementRecords" with `create_native_measurement` and `create_imported_measurement`.
    - Native validation (Requirement 45): reject when the target Measurement Definition Revision does not resolve, the observed value has more than six fractional digits, the unit does not match the Definition's `unit_of_measure`, the observation time is outside the observation window or later than the recorded time, any required attribute is omitted, or any imported-only source-system attribute is supplied; persist `origin = 'native'` with all source-system columns NULL; normalize the Decimal before persistence.
    - Imported validation (Requirement 46): reject when any source-system attribute is omitted, `source_system_authority` is outside `{authoritative, replica, projection, index, federation}`, observation time is later than retrieval time, retrieval time is later than recorded time, or `origin` is supplied as anything other than `imported`; never default the authority designation to `authoritative` (reject if absent); set `import_at = recorded_at`; enforce the AD-WS-39 idempotency key, rejecting a duplicate `(source_system_id, source_system_record_id)` pair per Definition Revision with no second Record persisted.
    - Insert one `Cites` Relationship to the target Measurement Definition Revision (`semantic_role = 'measurement_basis'`, AD-WS-35).
    - Evaluate `Authorization_Service.evaluate(party, "create.measurement_record", ...)` for both native and imported writes; on deny use the AD-WS-9 separate-transaction Denial Record pattern.
    - On permit, insert `Measurement_Records`, the `Cites` Relationship, and the consequential `Audit_Records` row inside one transaction.
    - _Requirements: 45.1, 45.2, 45.3, 45.4, 45.5, 45.6, 45.7, 46.1, 46.2, 46.3, 46.4, 46.5, 46.6, 46.7, 46.8, 52.7, 57.1_

  - [x] 5.2 Write unit tests for `MeasurementRecordService`
    - Cover (native): observed-value fractional-digit boundary (6 accepted, 7 rejected); unit mismatch rejected; observation time outside window / later than recorded time rejected; any supplied source-system attribute rejected; `origin = 'native'` persisted with all source-system columns NULL.
    - Cover (imported): every source-system attribute required; `source_system_authority` enumeration validation and explicit rejection when absent (never defaulted to `authoritative`); observation ≤ retrieval ≤ recorded ordering enforced; `import_at = recorded_at`; duplicate idempotency-key pair rejected with no second Record persisted and the first left byte-equivalent; the returned representation surfaces `origin = imported` and the authority designation explicitly.
    - Cover (both): authorization deny path producing exactly one Denial Record; immutability rejection of UPDATE / DELETE; one `Cites` Relationship with `semantic_role = 'measurement_basis'`.
    - _Requirements: 45.2, 45.3, 45.6, 46.2, 46.3, 46.4, 46.7, 52.7_

- [x] 6. Checkpoint - Measurement ingestion services
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement Observed Outcomes service
  - [x] 7.1 Implement `ObservedOutcomeService` create and revise
    - Create `src/walking_slice/outcome/observed_outcomes.py` exposing the `ObservedOutcomeService` dataclass from design §"Outcome_Service.ObservedOutcomes" with `create_observed_outcome` and `revise_observed_outcome`.
    - Validate inputs per Requirement 47 (assessment summary 1..4000, applicable scope present, at least one cited Measurement Record); reject any prohibited intended-side key; reject when `outcome_kind` is supplied with any value other than `observed`.
    - Resolve the target Intended Outcome Revision via `intended_outcome_reader.get_revision(...)`; reject when unresolvable or `outcome_kind != 'intended'`.
    - Resolve every cited Measurement Record via `measurement_reader`; reject when any does not resolve or its target Measurement Definition Resource does not match the Measurement Definition Resource that addresses the target Intended Outcome Resource (Requirement 47.2/47.4, anchored via the single Measurement Definition per AD-WS-40).
    - On revise, require the supplied `predecessor_revision_id` to equal the current most-recent Revision for the Resource (optimistic concurrency, AD-WS-36); reject otherwise; append a new Revision and leave prior Revisions byte-equivalent; never mutate the addressed Intended Outcome Revision (Requirement 47.8).
    - Evaluate `Authorization_Service.evaluate(party, "create.observed_outcome", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, insert (on create) `Observed_Outcomes` plus the initial `Observed_Outcome_Revisions`, or (on revise) the next `Observed_Outcome_Revisions` with `predecessor_revision_id` set; insert one `Addresses` Relationship to the target Intended Outcome Revision (`semantic_role IS NULL`), one `Cites` Relationship per cited Measurement Record (`semantic_role = 'observation_basis'`), and the consequential `Audit_Records` row, all in one transaction. Every Revision records `outcome_kind = 'observed'`.
    - _Requirements: 47.1, 47.2, 47.3, 47.4, 47.5, 47.6, 47.7, 47.8, 52.8, 53.2, 57.1_

  - [x] 7.2 Write unit tests for `ObservedOutcomeService`
    - Cover: target Intended Outcome Revision unresolvable / non-`intended` rejection; zero cited Measurement Records rejected; a cited Measurement Record whose Definition does not address the target Intended Outcome rejected; `outcome_kind` other than `observed` rejected; predecessor-chain optimistic-concurrency rejection on a stale `predecessor_revision_id`; the most-recent Revision is the one not named as any other Revision's predecessor; addressed Intended Outcome Revision byte-equivalent after revision; authorization deny path producing exactly one Denial Record; immutability rejection of UPDATE / DELETE; one `Addresses` plus one `Cites` per cited Record.
    - _Requirements: 47.2, 47.3, 47.4, 47.7, 47.8, 52.8, 53.2_

- [x] 8. Implement Success-Condition Assessments service
  - [x] 8.1 Implement `SuccessConditionAssessmentService.create_assessment`
    - Create `src/walking_slice/outcome/success_condition_assessments.py` exposing the `SuccessConditionAssessmentService` dataclass from design §"Outcome_Service.SuccessConditionAssessments".
    - Validate inputs per Requirement 48 (assessment category in `{Satisfied, Partially_Satisfied, Not_Satisfied, Unassessable}`, rationale 1..4000 with `>= 200` when Unassessable, authority basis type in the AD-WS-10 set, applicable scope present); reject any prohibited intended-side key.
    - Resolve the target Intended Outcome Revision (reject when unresolvable or non-`intended`) and the sourced Observed Outcome Revision via `observed_outcome_reader`; reject when the sourced Observed Outcome Revision's `Addresses` target does not equal the named target Intended Outcome Revision (Requirement 48.3).
    - Evaluate `Authorization_Service.evaluate(party, "create.success_condition_assessment", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, insert `Success_Condition_Assessment_Records`, one `Addresses` Relationship to the target Intended Outcome Revision (`semantic_role IS NULL`), one `Cites` Relationship to the sourced Observed Outcome Revision (`semantic_role = 'assessment_basis'`), and the consequential `Audit_Records` row inside one transaction. Leave the addressed Intended Outcome Revision and the sourced Observed Outcome Revision byte-equivalent (Requirement 48.7).
    - _Requirements: 48.1, 48.2, 48.3, 48.4, 48.5, 48.6, 48.7, 52.8, 53.2, 57.1_

  - [x] 8.2 Write unit tests for `SuccessConditionAssessmentService.create_assessment`
    - Cover: assessment-category enumeration boundaries; rationale length boundaries (1, 4000, 0 rejected, 4001 rejected) and the Unassessable `>= 200`-char rule (199 rejected, 200 accepted); authority-basis-type enumeration; sourced Observed Outcome Revision whose `Addresses` target differs from the named target rejected; addressed Intended Outcome Revision and sourced Observed Outcome Revision byte-equivalent after assessment; authorization deny path producing exactly one Denial Record; immutability rejection of UPDATE / DELETE; one `Addresses` plus one `Cites` Relationship.
    - _Requirements: 48.3, 48.4, 48.6, 48.7, 52.8_

- [x] 9. Implement Outcome Reviews service
  - [x] 9.1 Implement `OutcomeReviewService.create_outcome_review`
    - Create `src/walking_slice/outcome/outcome_reviews.py` exposing the `OutcomeReviewService` dataclass from design §"Outcome_Service.OutcomeReviews".
    - Validate inputs per Requirement 49 (review outcome, attribution stance, confidence each within their enumerated sets; review rationale 1..4000; authority basis type in the AD-WS-10 set; applicable scope present; at least one cited Success-Condition Assessment and at least one cited Completion Record); require a non-empty attribution-evidence reference when the stance is `Asserted` or `Contradicted` (Requirement 49.4); reject any prohibited intended-side key and any field that would assert Outcome from Completion alone or alias a Completion Record as an Observed Outcome (Requirements 54.1, 54.4).
    - Resolve the target Intended Outcome Revision (reject when unresolvable or non-`intended`); pre-check the `UNIQUE(target_intended_outcome_revision_id)` constraint per Requirement 49.3 and return `outcome_review_already_exists` when present, carrying the existing `outcome_review_id` only when the caller holds view authority on it (AD-WS-9).
    - Resolve every cited Success-Condition Assessment via `assessment_reader` (reject when any does not resolve or its `Addresses` target differs from the named target); resolve every cited Completion Record via `completion_reader` (Slice 3 read API, AD-WS-40) and every cited produced Deliverable Revision via `deliverable_reader.get_revision(...)`; reject any unresolvable identifier.
    - Confirm the Review is created only by explicit request and never as a side effect of any Slice 3 finalization (Requirements 49.9, 54.1).
    - Evaluate `Authorization_Service.evaluate(party, "create.outcome_review", ...)`; on deny use the AD-WS-9 pattern.
    - On permit, insert `Outcome_Review_Records`, one `Addresses` Relationship to the target Intended Outcome Revision (`semantic_role IS NULL`), one `Cites` Relationship per cited Success-Condition Assessment (`semantic_role = 'review_assessment'`), per cited Completion Record (`semantic_role = 'review_completion'`), and per cited produced Deliverable Revision (`semantic_role = 'review_deliverable'`), and the consequential `Audit_Records` row, all in one transaction.
    - _Requirements: 49.1, 49.2, 49.3, 49.4, 49.5, 49.6, 49.7, 49.8, 49.9, 52.9, 53.2, 54.1, 54.2, 54.3, 54.4, 57.1_

  - [x] 9.2 Write unit tests for `OutcomeReviewService.create_outcome_review`
    - Cover: review-outcome / attribution-stance / confidence enumeration boundaries; Asserted and Contradicted require a non-empty attribution-evidence reference (empty rejected); zero cited Assessments or zero cited Completion Records rejected; cited Assessment whose `Addresses` target differs from the named target rejected; unresolvable cited Completion or Deliverable Revision rejected; duplicate Outcome Review against the same Intended Outcome Revision rejected with the first left byte-equivalent; explicit assertion that no Review is created as a side effect of a Slice 3 Completion finalization; authorization deny path producing exactly one Denial Record; immutability rejection of UPDATE / DELETE; the three `Cites` `semantic_role` markers present.
    - _Requirements: 49.3, 49.4, 49.9, 52.9, 54.1, 54.2_

- [x] 10. Extend Provenance_Navigator additively for Slice 4
  - [x] 10.1 Implement `navigate_outcome_review` and `navigate_outcome_node` traversals
    - Create `src/walking_slice/outcome/_provenance.py` exposing `navigate_outcome_review(connection, outcome_review_id, party_id, at)` and `navigate_outcome_node(connection, node_kind, node_id, party_id, at)`, registered with `walking_slice.provenance`. Do not modify the existing `navigate_decision` (Slice 1) or `navigate_completion` (Slice 3) functions; reuse them for the chain tails.
    - Walk Outcome Review → Success-Condition Assessment(s) → Observed Outcome Revision → Measurement Record(s) → Measurement Definition Revision → Intended Outcome Revision → Objective → Slice 1 Decision (delegating to `navigate_decision`); in parallel walk Outcome Review → Cites Completion Record(s) → (delegating to `navigate_completion`) → produced Deliverable Revision(s). Return a single tree rooted at the Outcome Review.
    - Replace nodes restricted to the requesting Party with `{kind, redacted: true}` markers (Requirement 55.3, 58.2); emit gap descriptors `{stage, category, next_reachable_node?}` with `category ∈ {unavailable, restricted, stale, unresolved}` for unresolved/stale/unavailable links (Requirements 51.3, 55.4); include Region Occurrence span fields digest-matching the recorded content digest (Requirement 55.2); include the origin indicator on Measurement Record nodes and, for imported Records visible to the Party, the source-system identifier and authority designation (Requirement 55.8).
    - Return byte-equivalent trees across repeated invocations for the same `(node_id, party_id, at)` (idempotent retrieval, Requirements 51.4, 55.5).
    - _Requirements: 51.1, 51.2, 51.3, 51.4, 55.1, 55.2, 55.4, 55.5, 55.8_

  - [x] 10.2 Extend backlinks to cover Slice 4 node kinds
    - Extend the existing `_authorized_source_kinds` (or equivalent set) in `src/walking_slice/provenance.py` to include `'measurement_definition'`, `'measurement_definition_revision'`, `'measurement_record'`, `'observed_outcome'`, `'observed_outcome_revision'`, `'success_condition_assessment_record'`, and `'outcome_review_record'` so the existing constant-time backlink algorithm returns Slice 4 relationships when the queried endpoint is a Slice 4 node.
    - Do not change the algorithm itself; this is an additive coverage extension only. Preserve the at-most-500-relationship bound and continuation reference whose presence does not vary by relationships the Party lacks authority to know (Requirement 56.6).
    - _Requirements: 56.1, 56.2, 56.4, 56.6, 43.5_

  - [x] 10.3 Write unit tests for the Slice 4 traversals and backlink extension
    - Cover: `navigate_outcome_review` returns both ordered chains with all node identities resolving; restricted nodes appear as `{kind, redacted: true}` markers; unresolved / stale / unavailable nodes return gap descriptors with `stage`, `category`, and (when visible) next reachable identity; idempotent retrieval across 5 repetitions; Measurement Record nodes carry the origin indicator and (for visible imported Records) the source-system authority designation; backlink queries for Slice 4 node kinds return relationships with `semantic_role` populated correctly and identical attribute values from both directions.
    - _Requirements: 51.3, 51.4, 55.2, 55.4, 55.5, 55.8, 56.1, 56.2_

- [x] 11. Implement the outcome-status Projection
  - [x] 11.1 Implement `project_outcome_status` and `OutcomeStatusProjection`
    - Create `src/walking_slice/outcome/_projection.py` exposing `OutcomeStatusProjection` and `project_outcome_status(connection, intended_outcome_revision_id, party_id, at)`.
    - Derive the projected status from source Records per design §"Outcome-status Projection": `Intended Outcome unmeasured`, `Intended Outcome measurement defined`, `Intended Outcome measured`, `Intended Outcome observed`, `Intended Outcome success condition <satisfied|partially satisfied|not satisfied|unassessable>`, `Intended Outcome reviewed`, with `Provenance incomplete` fall-back when a required source link is unresolved; the projected status is the most-progressed label observed.
    - Wrap the response in the existing Slice 1 `ProjectionEnvelope` from `walking_slice.projection` carrying the Projection Definition, source Record Identities, source Revision Identities, applicable temporal boundary (ISO-8601 ≥ second precision), generated time, and a derivation indicator distinguishing the status from authoritative source Records and from the Outcome Review Record itself (Requirements 59.1, 59.2).
    - On unresolvable Projection Definition or missing source Record, withhold the projected status and return an explanation-unavailable indicator naming the missing element (Requirement 59.5); source Records remain byte-equivalent.
    - Never include a derived percent-attainment, cost-per-outcome, ROI, budget-variance, forecast-attainment, causal-attribution probability, or cross-Outcome aggregate value (Requirement 59.3); never alias the projection as an Observed Outcome, Success-Condition Assessment, or Outcome Review, and never cite it from any Outcome Review (Requirement 59.6).
    - Require `view` authority on the target Intended Outcome Revision; restricted targets return AD-WS-9-shaped indistinguishable responses.
    - _Requirements: 59.1, 59.2, 59.3, 59.4, 59.5, 59.6_

  - [x] 11.2 Write unit tests for the outcome-status Projection
    - Cover: each projected status value at the appropriate pipeline stage; envelope carries every required field; unresolvable-definition path withholds the status and returns the explanation-unavailable indicator; source Records remain byte-equivalent when new evidence arrives; absence of every prohibited derived field in the response body; the projection is never labelled as an Observed Outcome or Outcome Review.
    - _Requirements: 59.1, 59.2, 59.3, 59.5, 59.6_

- [x] 12. Implement Interim ADR seeding for Slice 4 gaps
  - [x] 12.1 Seed `Interim_ADR_Records` for Gaps G-16 through G-20
    - Create `src/walking_slice/outcome/_interim_adr.py` exposing `seed_outcome_interim_adr(connection)` that inserts one row per AD-WS-38 (G-16 / `ADR-HT-018`), AD-WS-33 (G-17 / `ADR-HT-019`), AD-WS-34 (G-18 / `ADR-HT-020`), AD-WS-35 (G-19 / `ADR-HT-021`), and AD-WS-36 / AD-WS-37 (G-20 / `ADR-HT-022`).
    - Each row records the motivating Requirement number, motivating criterion number, observable behavior chosen, recorded date, and backlog ADR identifier per the Slice 1–3 Interim ADR contract; the `ADR-HT-018` row records the chosen `{native, imported}` and `{authoritative, replica, projection, index, federation}` member sets.
    - The seeder is idempotent on re-invocation.
    - _Requirements: 60.4, 60.5_

  - [x] 12.2 Write unit tests for Slice 4 Interim ADR seeding
    - Cover: every Slice 4 backlog ADR identifier in `{ADR-HT-018, ADR-HT-019, ADR-HT-020, ADR-HT-021, ADR-HT-022}` maps to at least one seeded row with the documented fields; re-running the seeder is idempotent; existing Slice 1 + Slice 2 + Slice 3 Interim ADR rows are byte-equivalent after Slice 4 seeding.
    - _Requirements: 60.4, 60.5_

- [x] 13. Implement HTTP routes for the Outcome_Service and wire startup
  - [x] 13.1 Implement Outcome_Service HTTP endpoints
    - Create `src/walking_slice/outcome/_routes.py` exposing one FastAPI `APIRouter` under `/api/v1` with the endpoints from design §"Components and Interfaces": Measurement Definitions (`POST /measurement-definitions`, `GET /measurement-definitions/{id}`, `GET /measurement-definitions/{id}/revisions/{rid}`), Measurement Records (`POST /measurement-records`, `POST /measurement-records/imported`, `GET /measurement-records/{id}`), Observed Outcomes (`POST /observed-outcomes`, `POST /observed-outcomes/{id}/revisions`, `GET /observed-outcomes/{id}/revisions/{rid}`), Success-Condition Assessments (`POST /success-condition-assessments`, `GET /success-condition-assessments/{id}`), Outcome Reviews (`POST /outcome-reviews`, `GET /outcome-reviews/{id}`), the provenance routes (`GET /outcome-reviews/{id}/provenance`, `GET /measurement-records/{id}/provenance`, `GET /observed-outcomes/{id}/revisions/{rid}/provenance`), and the outcome-status Projection endpoint (`GET /intended-outcomes/{intended_outcome_revision_id}/outcome-status`).
    - Wire each route to the corresponding service through the existing Slice 1 `RequestContext` dependency.
    - Use Pydantic request models with `Config(extra='forbid')` so unknown fields are rejected at the API boundary; combine with `_reject_prohibited_attributes` for the intended-side rejection paths; shape every response through the existing `walking_slice.provenance._shape_response_constant_time(...)` helper.
    - _Requirements: 44.1, 45.1, 46.1, 47.1, 48.1, 49.1, 50.7, 51.1, 53.2, 55.7, 59.1_

  - [x] 13.2 Mount Slice 4 routes into the FastAPI application and wire startup seeders
    - Extend `src/walking_slice/app.py` so the FastAPI application loads `outcome._routes`, calls `outcome._persistence.create_outcome_schema(engine)` on startup, calls `outcome._disclosure.seed_outcome_coverage(connection)`, calls `outcome._interim_adr.seed_outcome_interim_adr(connection)`, and registers the additive `navigate_outcome_review` / `navigate_outcome_node` functions and the extended backlink kinds with `walking_slice.provenance`.
    - Do not alter Slice 1, Slice 2, or Slice 3 startup behavior beyond these additive calls.
    - _Requirements: 58.1, 60.1, 60.2, 60.4_

- [x] 14. Checkpoint - Outcome pipeline wired end-to-end
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Cross-cutting property tests for Slice 4
  - [x] 15.1 Write property test for Intended-Outcome anchoring and creation success
    - **Property 46: Intended-Outcome anchoring and creation success**
    - **Validates: Requirements 44.1, 44.2, 45.1, 45.2, 46.1, 46.2, 47.1, 47.2, 48.1, 48.2, 49.1, 49.2, 61.1**
    - Use Hypothesis strategies for each outcome-measurement request body; for every authorized valid request assert exactly one Resource/Revision/Record row, exactly one consequential `Audit_Records` row, and the prescribed `Addresses`/`Cites` Relationship rows per AD-WS-35 persist in one transaction with byte-equivalent recorded times; assert every Measurement Definition Revision, Observed Outcome Revision, Success-Condition Assessment, and Outcome Review has exactly one `Addresses` to an Intended Outcome Revision resolving with `outcome_kind = 'intended'`, and no such entity exists without a matching `intended` Revision.

  - [x] 15.2 Write property test for Outcome-Record authority correctness
    - **Property 47: Outcome-Record authority correctness**
    - **Validates: Requirements 44.5, 45.4, 46.5, 47.5, 48.4, 49.5, 50.1, 50.3, 52.6, 52.7, 52.8, 52.9, 61.2**
    - Generate role assignments varying across effective-start, expiration, revocation, scope, and granted-authority dimensions; for every persisted Slice 4 entity assert a matching effective Role Assignment exists whose granted authorities include the precise required authority (`define_measurement`/`record_measurement`/`assess_outcome`/`issue_outcome_review`), whose scope covers the target, and whose effective period encloses the recorded time; assert no entity exists without a matching effective authority record.

  - [x] 15.3 Write property test for authority non-substitution across twelve types
    - **Property 48: Authority non-substitution across twelve types**
    - **Validates: Requirements 52.1, 52.2, 52.3, 52.4, 52.5, 52.10, 52.11, 61.3**
    - Assert the twelve authority types are pairwise distinct in the Role-Assignment evaluation function; assert no Measurement Definition exists whose authoring Party held only a single non-`define_measurement` authority, no Measurement Record whose recorder held only a single non-`record_measurement` authority, no Observed Outcome Revision or Success-Condition Assessment whose author held only a single non-`assess_outcome` authority, and no Outcome Review whose reviewer held only a single non-`issue_outcome_review` authority; assert no prior authority is substituted for a new authority in either direction.

  - [x] 15.4 Write property test for Outcome-entity immutability
    - **Property 49: Outcome-entity immutability**
    - **Validates: Requirements 44.7, 45.6, 47.7, 48.6, 49.7, 57.3, 57.5, 61.4**
    - For each generated full pipeline, apply Hypothesis-drawn UPDATE / DELETE attempt sequences against every Slice 4 table and assert every Resource / Revision / Record row, every constituent field, every `Addresses` and `Cites` Relationship, and every corresponding `Audit_Records` row are byte-equivalent across all later observation points, and every mutation attempt is rejected and appends a Denial Record.

  - [x] 15.5 Write property test for Intended/Observed separation from the outcome side
    - **Property 50: Intended/Observed separation enforced from the outcome side**
    - **Validates: Requirements 53.1, 53.2, 53.3, 53.4, 47.8, 48.7, 61.5**
    - Generate request bodies with random keys drawn from the prohibited intended-side prefixes and `outcome_kind` values other than `observed`; assert every such request is rejected with no row persisted; assert no persisted outcome-measurement entity carries an intended-side attribute on the row; assert no Intended Outcome Resource/Revision, Objective, Project, Deliverable Expectation, Plan Revision, Plan Review/Approval, or Slice 3 execution Record is mutated as a consequence of any Slice 4 action.

  - [x] 15.6 Write property test for Output is not Outcome, re-asserted from the outcome side
    - **Property 51: Output is not Outcome, re-asserted from the outcome side**
    - **Validates: Requirements 49.9, 54.1, 54.2, 54.3, 54.4, 61.6**
    - Assert no Outcome Review was created by automatic derivation from any Slice 3 Completion, Milestone Acceptance, Deliverable Production, or produced Deliverable Revision finalization; assert every Review carries an attribution stance in `{Asserted, Partial, Unattributed, Contradicted}` and that `Asserted`/`Contradicted` carry an attribution-evidence reference of ≥ 1 character; assert any request field whose stated purpose is to assert that a Completion alone satisfies the Intended Outcome, or to alias a Completion as an Observed Outcome, is rejected with no row persisted.

  - [x] 15.7 Write property test for outcome-status Projection envelope and contents
    - **Property 52: Outcome-status Projection envelope and contents**
    - **Validates: Requirements 59.1, 59.2, 59.3, 59.4, 59.5, 59.6**
    - Generate pipelines at various stages; assert every status-bearing response carries a `ProjectionEnvelope` with Projection Definition, source Record Identities, source Revision Identities, applicable temporal boundary, generated time, and derivation indicator; assert absence of percent-attainment, cost-per-outcome, ROI, budget-variance, forecast-attainment, causal-attribution, and cross-Outcome aggregate values; assert the unresolvable-definition path withholds the status and returns an explanation-unavailable indicator; assert the projected status is never aliased as an Observed Outcome, Assessment, or Review.

  - [x] 15.8 Write property test for the Outcome Measurement Provenance Chain end-to-end
    - **Property 53: Outcome Measurement Provenance Chain end-to-end**
    - **Validates: Requirements 51.1, 51.2, 51.3, 51.4, 55.1, 55.2, 55.4, 55.5, 55.8, 61.8**
    - Generate full Slice 1–4 pipelines fully visible to the requesting Party; navigate from each Outcome Review and assert both ordered chains return, every identity resolves, the returned Content Region Occurrence span digest-matches the recorded content digest, Measurement Record nodes carry the origin indicator (and source-system authority for visible imported Records), the chains are byte-equivalent across ≥ 5 repeated `navigate_outcome_review` invocations, restricted nodes appear as `{kind, redacted: true}` markers, and unresolved/stale/unavailable nodes return gap descriptors.

  - [x] 15.9 Write property test for indistinguishable denial across Slice 4 endpoints
    - **Property 54: Indistinguishable denial for outcome-measurement endpoints**
    - **Validates: Requirements 50.2, 50.4, 50.5, 50.6, 50.7, 51.5, 51.6, 55.3, 55.6, 55.7, 56.3, 56.5, 58.2, 58.3, 58.4, 58.5, 61.9**
    - Generate pairs `(P, P′)` differing only in authority on `R`; assert responses to `P′` for creation, read, backlink, provenance, and projection attempts on `R` are indistinguishable from non-existent-target responses across result count, identifier set, ordering positions, pagination cursors, response size, body keys, error category, error wording, and latency (within 100 ms); assert the same holds under the `slice-default-2026` policy as extended by AD-WS-34, including the per-attribute restriction on imported Measurement Record source-system attributes.

  - [x] 15.10 Write property test for backlink bidirectionality for outcome-measurement Resources
    - **Property 55: Backlink bidirectionality for outcome-measurement Resources**
    - **Validates: Requirements 43.5, 56.1, 56.2, 56.4, 56.6, 61.10**
    - Generate Slice 1–4 relationship graphs; for each requesting Party holding view authority on both `R` and its source endpoint, assert the Provenance_Navigator returns `R` from the target's backlink query iff from the source's outbound query with identical Relationship attribute values (including `semantic_role`); assert returning a backlink grants no authority on the source endpoint or any traversed Record and that the response is bounded to ≤ 500 relationships with a continuation reference whose presence does not vary by relationships `P` lacks authority to know.

  - [x] 15.11 Write property test for uniqueness of Measurement Definition, Outcome Review, and imported Measurement Record
    - **Property 56: Uniqueness of Measurement Definition, Outcome Review, and imported Measurement Record**
    - **Validates: Requirements 44.3, 46.3, 49.3, 61.11**
    - Generate double-creation attempts: a second Measurement Definition against the same Intended Outcome Resource, a second Outcome Review against the same Intended Outcome Revision, and a second imported Measurement Record with a matching `(source_system_id, source_system_record_id)` pair against the same Definition Revision; assert only the first persists in each case, the second is rejected with no row persisted, and the first remains byte-equivalent.

  - [x] 15.12 Write property test for Slice 1, Slice 2, and Slice 3 non-modification
    - **Property 57: Slice 1, Slice 2, and Slice 3 non-modification under Slice 4 actions**
    - **Validates: Requirements 46.8, 47.8, 48.7, 49.8, 53.1, 53.5, 54.5, 60.1, 60.2, 60.3, 60.4, 61.12**
    - Snapshot every Slice 1 + Slice 2 + Slice 3 table before any Slice 4 action; run Hypothesis-drawn Slice 4 operation sequences; assert every prior-slice row is byte-equivalent at every observation point, apart from the additive `Disclosure_Policy_Coverage` rows seeded by AD-WS-34, the additive twelve-value authority enumeration permitted by AD-WS-33, the additive `Identifier_Registry.resource_kind` values, and new `Relationships` rows inserted by Slice 4 actions.

  - [x] 15.13 Write property test for identity uniqueness across all four slices
    - **Property 58: Identity uniqueness across all four slices**
    - **Validates: Requirements 43.1, 43.2, 43.3, 43.4, 43.6, 43.7, 43.8, 61.13**
    - Generate identifiers across all four slices; assert every identifier is unique across all slices, kinds, and Record kinds, is canonical UUIDv7 lowercase hyphenated 8-4-4-4-12 form, and embeds no business metadata; assert Measurement Definition and Observed Outcome each hold distinct Resource and Revision Identities with one Resource to one-or-more Revisions and no Revision shared across Resources; assert the seven Slice 4 identifier roles are disjoint from every prior-slice identifier; assert rename/relocate preserves Resource and Revision Identities and no once-assigned identifier is reused.

  - [x] 15.14 Write property test for audit completeness and atomicity across outcome-measurement actions
    - **Property 59: Audit completeness and atomicity for every outcome-measurement action**
    - **Validates: Requirements 44.6, 45.5, 45.7, 46.6, 47.6, 48.5, 49.6, 50.2, 57.1, 57.2, 57.4, 57.6, 61.14**
    - Generate sequences of Slice 4 operations (creations, denied attempts, attempted modifications of finalized entities); assert exactly one consequential `Audit_Records` row per consequential write and exactly one Denial Record per denied attempt, each with `actor_party_id`, `action_type`, `target_id`, `target_revision_id` when applicable, `outcome`, `recorded_at`, and `correlation_id` consistent with the originating operation, appended in the same transaction; assert `Audit_Records.append_sequence` is monotonically non-decreasing by `recorded_at`; assert an audit-append failure rolls back the originating finalization and leaves it unobservable from any query path.

  - [x] 15.15 Write property test for repeatable property runs and Interim ADR retrievability
    - **Property 60: Repeatable property runs**
    - **Validates: Requirements 60.5, 61.15**
    - Confirm the Slice 4 property suite runs ≥ 100 generated cases per property under `@settings(max_examples=100, deadline=2000)`, records the seed of every invocation, and on re-execution with the same seed produces identical pass/fail outcomes and minimal counterexamples; for each backlog ADR identifier in `{ADR-HT-018, ADR-HT-019, ADR-HT-020, ADR-HT-021, ADR-HT-022}`, assert querying `Interim_ADR_Records` returns at least one row whose motivating Requirement number, criterion number, observable behavior, recorded date, and backlog ADR identifier match the AD-WS-33..AD-WS-38 decisions, that the rows are byte-equivalent at every observation point, and that re-seeding is idempotent.

  - [x] 15.16 Configure repeatable property runs and seed capture for Slice 4
    - Configure the Slice 4 property tests under the existing Hypothesis profile (`@settings(max_examples=100, deadline=2000)`); enable `--hypothesis-seed` capture on every Slice 4 property test; persist the seed of every invocation to the build artifact alongside the Slice 1–3 seeds; add a re-execution check that confirms identical pass/fail outcomes and minimal counterexamples for failing properties.
    - _Requirements: 61.15_

- [x] 16. End-to-end HTTP integration tests for Slice 4
  - [x] 16.1 Write end-to-end tests for the Release 1D journey
    - Drive the FastAPI app via `httpx.AsyncClient` and exercise the full pipeline: complete the Slice 1–3 journey through a Completion Record, then define a Measurement Definition → record a native Measurement Record → record an imported Measurement Record → record an Observed Outcome Revision → record a Success-Condition Assessment → record an Outcome Review citing the Assessment and the Completion Record → navigate the Outcome Measurement Provenance Chain back to the exact Document Revision text and along the parallel leg to the produced Deliverable Revision → read the outcome-status Projection.
    - _Requirements: 44.1, 45.1, 46.1, 47.1, 48.1, 49.1, 51.1, 59.1_

  - [x] 16.2 Write end-to-end tests for the named denial and separation demonstrations
    - Drive the FastAPI app and assert: a Measurement Recorder attempting an Outcome Review is denied with an AD-WS-9-shaped response and a Denial Record; an Outcome Assessor attempting a Measurement Definition is denied; submitting an outcome-measurement request with a prohibited intended-side attribute (e.g. `intended-`, `planned-`) is rejected with no row persisted and no prior-slice row mutated; submitting an Observed Outcome with `outcome_kind` other than `observed` is rejected; an Outcome Review with stance `Asserted` and an empty attribution-evidence reference is rejected; a second imported Measurement Record with a matching source-system pair is rejected; an unauthorized requester reading an imported Measurement Record receives the `{kind, redacted: true}` marker with no source-system attribute leakage; a creation against a non-existent Intended Outcome Revision is indistinguishable from one against a restricted Revision the caller cannot view.
    - _Requirements: 50.4, 50.5, 50.7, 53.2, 54.1, 54.2, 55.7, 58.4, 58.5_

- [x] 17. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; the core implementation tasks (no `*`) are mandatory.
- Property tests directly map to design §"Correctness Properties" — every property in the Slice 4 suite (46 through 60) has its own sub-task under task 15, annotated with its property number and the Requirements clauses it validates.
- The Interim ADR records seeded in task 12.1 close Gaps G-16 through G-20 from requirements.md and provide the input for backlog ADRs HT-018 through HT-022 without re-implementing the slice.
- Checkpoints sit after the Measurement ingestion services (task 6), after the pipeline is wired end-to-end (task 14), and at the end (task 17); each is a manual verification gate.
- Requirement 60 (Slice 1, Slice 2, and Slice 3 non-modification) is enforced both by code structure (additive `Disclosure_Policy_Coverage` rows, additive enumeration values, additive read-only functions on `walking_slice.planning` and `walking_slice.provenance`) and by Property 57 running at every observation point.
- All testing uses `pytest` with Hypothesis configured per design §"Testing Strategy"; each property test runs at least 100 generated cases with deterministic seed capture per Requirement 61.15.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.2", "1.3", "2.1", "3.1", "3.2"] },
    { "id": 1,  "tasks": ["1.4", "2.2", "4.1"] },
    { "id": 2,  "tasks": ["4.2", "5.1"] },
    { "id": 3,  "tasks": ["5.2", "7.1"] },
    { "id": 4,  "tasks": ["7.2", "8.1"] },
    { "id": 5,  "tasks": ["8.2", "9.1"] },
    { "id": 6,  "tasks": ["9.2", "10.1", "10.2", "11.1", "12.1"] },
    { "id": 7,  "tasks": ["10.3", "11.2", "12.2", "13.1"] },
    { "id": 8,  "tasks": ["13.2"] },
    { "id": 9,  "tasks": ["15.1", "15.2", "15.3", "15.4", "15.5", "15.6", "15.7", "15.8", "15.9", "15.10", "15.11", "15.12", "15.13", "15.14", "15.15", "15.16"] },
    { "id": 10, "tasks": ["16.1", "16.2"] }
  ]
}
```
