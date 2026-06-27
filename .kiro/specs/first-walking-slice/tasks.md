# Implementation Plan: First Walking Slice

## Overview

This plan implements the *first walking slice* of the Organizational Knowledge and Work System: the modular monolith specified in [`design.md`](./design.md) that satisfies the requirements in [`requirements.md`](./requirements.md). The slice is a Python 3.11+ FastAPI service backed by SQLite (WAL journal mode) accessed through SQLAlchemy Core, verified by example-based tests with `pytest` and property-based tests with Hypothesis.

The plan is incremental: it starts at the persistence and identity foundation (which every other component depends on), builds the synthesis pipeline (Evidence → Region → Finding → Recommendation → Decision), then layers the Trail and Provenance navigation surfaces, and finishes with HTTP composition and the cross-cutting property tests. Each property in the design's Correctness Properties section is wired to its own sub-task and annotated with the property number and the requirements clauses it validates, per the property-based testing rules in `requirements.md` Requirement 15.

## Tasks

- [x] 1. Set up project structure and persistence foundation
  - [x] 1.1 Set up Python project skeleton with FastAPI and tooling
    - Create `pyproject.toml` pinning Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy Core, `uvicorn`, `httpx`, `pytest`, `pytest-asyncio`, `hypothesis>=6`, and `uuid-utils` (UUIDv7 shim).
    - Create source layout `src/walking_slice/` (modules per bounded context) and `tests/` (unit, property, end-to-end).
    - Add `tests/conftest.py` with a per-test SQLite file fixture and shared dependency-injection fixtures.
    - Add a `pytest.ini` enabling `--hypothesis-seed` propagation and per-test Hypothesis profile selection.
    - _Requirements: 16.1_

  - [x] 1.2 Implement Clock protocol and shared value-object module
    - Create `src/walking_slice/clock.py` with a `Clock` protocol and a `SystemClock` and `FixedClock` implementation, both returning UTC `datetime` with millisecond precision.
    - Create `src/walking_slice/models.py` containing the frozen Pydantic value objects from design §"In-Memory Value Objects" (`ResourceRef`, `FindingRef`, `RegionOccurrenceRef`, `Span`, `AuthorityBasisRef`, `ProvenanceNode`, `GapDescriptor`).
    - _Requirements: 2.5, 4.2, 6.2, 12.1, 13.1_

  - [x] 1.3 Implement SQLite schema with append-only triggers
    - Create `src/walking_slice/persistence.py` exposing `create_schema(engine)` that issues every `CREATE TABLE` and `CREATE TRIGGER` statement from design §"Table-by-Table Specification".
    - Add triggers that reject `UPDATE` and `DELETE` on every immutable table (`Document_Revisions`, `Region_Occurrences`, `Finding_Revisions`, `Recommendation_Revisions`, `Decisions`, `Relationships`, `Trail_Revisions`, `Trail_Steps`, `Provenance_Manifests`, `Audit_Records`) and the one-shot-field rules for `Role_Assignments.revoked_at` and `Omission_Entries.resolved_at`.
    - Add the composite indexes named in AD-WS-8 and design §"Persistence Invariants Summary".
    - Set `journal_mode=WAL` and `foreign_keys=ON` pragmas on every new connection.
    - _Requirements: 2.4, 2.7, 6.6, 13.3, 13.5, 16.2_

- [x] 2. Implement Identity_Service
  - [x] 2.1 Implement Identity_Service UUIDv7 generation and validation
    - Create `src/walking_slice/identity.py` with the `IdentityService` surface from design §"Identity_Service" (`new_resource_id`, `new_revision_id`, `new_relationship_id`, `new_region_id`, `new_immutable_record_id`, `new_trail_id`, `new_trail_revision_id`, `new_trail_step_id`, `new_manifest_id`, `validate_canonical`, `reject_if_duplicate`).
    - Generate identifiers with `uuid_utils.uuid7()` and validate against the canonical regex `^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`.
    - _Requirements: 1.1, 1.2, 1.6, 1.7_

  - [x] 2.2 Implement Identifier_Registry persistence and non-reuse enforcement
    - Persist every issued identifier to `Identifier_Registry` in the same transaction as the originating write.
    - Implement `reject_if_duplicate` to raise `IdentityConflictError` when an existing identifier is bound to a different content digest, and to append a Denial Record per the Identifier-conflict path in design §"Error Handling".
    - _Requirements: 1.4, 1.6_

  - [x] 2.3 Write unit tests for Identity_Service canonical form and conflict rejection
    - Cover canonical form validation, malformed-identifier rejection, and re-binding rejection per design §"Identifier conflict (Requirement 1.4)".
    - _Requirements: 1.1, 1.4, 1.6, 1.7_

  - [x] 2.4 Write property test for Identity opacity and uniqueness
    - **Property 10: Identity opacity and uniqueness**
    - **Validates: Requirements 1.1, 1.2, 1.4, 1.6, 1.7, 15.10**
    - Use a Hypothesis strategy that draws display names, role names, scope values, and content excerpts, asks the `Identity_Service` for ≥ 100 identifiers per case, and asserts uniqueness, canonical form, and absence of any business-attribute substring inside the issued identifier.

- [x] 3. Implement Audit_Log and Authorization_Service
  - [x] 3.1 Implement Audit_Log append-only service
    - Create `src/walking_slice/audit.py` exposing `append_consequential` and `append_denial` per design §"Audit_Log".
    - Both methods participate in the caller's transaction; failure raises `AuditAppendError` so callers can roll back per Requirement 2.7 and 13.6.
    - Persist a monotonically increasing `append_sequence` and `recorded_at` in UTC millisecond precision.
    - _Requirements: 13.1, 13.3, 13.4, 13.6_

  - [x] 3.2 Implement Authorization_Service evaluator with role assignments
    - Create `src/walking_slice/authorization.py` exposing `assign_role` and `evaluate` per design §"Authorization_Service".
    - Evaluate role assignments against effective-start, effective-end, revocation, and scope, returning `permit(authority_basis)` or `deny(reason_code, correlation_id)` where `reason_code ∈ {not-yet-effective, expired, revoked, out-of-scope, no-role-assignment}`.
    - Distinguish `view`, `modify`, and `approve` as three separate authority types and never substitute one for another.
    - Append an evaluation record to the Audit_Log per Requirement 12.5 in the same transaction as any consequential write that consumed the evaluation.
    - _Requirements: 7.3, 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

  - [x] 3.3 Implement role assignment HTTP endpoints
    - Add `POST /api/v1/roles/assignments` and `POST /api/v1/roles/assignments/{id}/revocations` per design §"Authorization_Service" HTTP surface.
    - Reject role assignment submissions missing Party Identity, role, scope, granted authorities, or effective-start time.
    - _Requirements: 12.1, 12.6_

  - [x] 3.4 Write unit tests for Authorization_Service denial reason codes
    - Exercise each branch in `{not-yet-effective, expired, revoked, out-of-scope, no-role-assignment}` and confirm the denial response shape conforms to AD-WS-9.
    - _Requirements: 7.4, 12.2, 12.4_

- [x] 4. Checkpoint - Foundational services
  - Ensure all tests pass, ask the user if questions arise.

- [x] 5. Implement Evidence_Repository
  - [x] 5.1 Implement Source_Documents and Document_Revisions persistence
    - Create `src/walking_slice/evidence.py` with `create_document`, `append_revision`, and `get_revision` operations.
    - Compute SHA-256 content digest at write time and persist `recorded_at` as UTC millisecond precision; reject empty content, content over 100 MB, and submissions missing a contributing Party identity.
    - Append a creation record to `Audit_Records` inside the same transaction; roll back the Revision creation if the audit append fails.
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x] 5.2 Implement Content_Regions and Region_Occurrences with byte-offset anchoring
    - Add `create_region_occurrence` to `evidence.py` enforcing `0 ≤ start < end ≤ len(content_bytes)` and computing `span_byte_length` and `span_content_digest_sha256` over `content_bytes[start:end]`.
    - Preserve prior Region Occurrences when later Document Revisions are recorded so historical citations remain resolvable.
    - Record a row in `Interim_ADR_Records` referencing AD-WS-6 and Gap G-1.
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 16.3_

  - [x] 5.3 Implement Source Document rename and relocate preserving identity
    - Add `rename_document` to `evidence.py` mutating only the display-path attribute on `Source_Documents` and never changing `resource_id` or any existing `revision_id`.
    - Append an Audit record for the rename action.
    - _Requirements: 1.3, 13.1_

  - [x] 5.4 Implement Evidence_Repository HTTP endpoints
    - Add `routes/evidence.py` exposing the six endpoints in design §"Evidence_Repository" HTTP surface, wired through `RequestContext` dependency injection.
    - _Requirements: 2.1, 3.1, 3.4, 13.1_

  - [x] 5.5 Write unit tests for span validation and Region Occurrence retrieval
    - Cover empty span, out-of-range span, `start >= end`, unresolvable region reference, and digest-matching retrieval.
    - _Requirements: 3.4, 3.5, 3.6_

  - [x] 5.6 Write property test for Identity survives rename and relocation
    - **Property 14: Identity survives rename and relocation**
    - **Validates: Requirements 1.3**
    - Generate a Source Document plus a Hypothesis-drawn sequence of rename and relocate operations; assert `resource_id` and every existing `revision_id` are byte-equivalent across all operations.

- [x] 6. Implement Findings
  - [x] 6.1 Implement Findings, Finding_Revisions, and Supports/Contradicts Relationships
    - Create `src/walking_slice/knowledge.py` with `create_finding` and `record_contradiction` operations.
    - Require at least one `Supports` Relationship to a resolvable Content Region Occurrence unless `is_hypothesis=True`; record one Relationship per cited Region Occurrence.
    - Record `source_revision_id`, `target_id`, `relationship_type`, `authoring_party_id`, and `recorded_at` per Requirement 4.2 on every Relationship.
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_

  - [x] 6.2 Implement Findings HTTP endpoints
    - Add `routes/findings.py` exposing `POST /api/v1/findings`, `POST /api/v1/findings/{finding_id}/contradictions`, and `GET /api/v1/findings/{finding_id}/revisions/{revision_id}` per design §"Knowledge_Service" HTTP surface.
    - _Requirements: 4.1, 4.4_

  - [x] 6.3 Write unit tests for Finding finalization branches
    - Hypothesis-flag true with zero supports (accepted), non-hypothesis with zero supports (rejected), multiple supports (one Relationship each), and contradictions preserving both Finding records.
    - _Requirements: 4.1, 4.3, 4.4, 4.5_

  - [x] 6.4 Write property test for Evidence support
    - **Property 1: Evidence support**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.5, 15.1**
    - For all persisted non-hypothesis Findings drawn from a relationship-graph strategy, assert at least one `Supports` Relationship resolves to a Region Occurrence whose `span_content_digest_sha256`, anchors, and Document Revision match the Evidence_Repository row.

- [x] 7. Implement Recommendations
  - [x] 7.1 Implement Recommendations, Recommendation_Revisions, and Derived From Relationships
    - Add `create_recommendation` to `knowledge.py` (or a sibling `recommendations.py` module) requiring 1 to 50 `Derived From` Relationships, each resolving to an existing Finding Resource at creation time.
    - Persist rationale (1..10,000 chars when present), assumptions (0..50 entries × 1..2,000 chars), and confidence (`{Low, Medium, High}`) on the Recommendation Revision.
    - Reject unauthenticated callers and callers lacking effective Analyst role for the applicable scope.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7_

  - [x] 7.2 Implement Recommendations HTTP endpoints
    - Add `routes/recommendations.py` exposing `POST /api/v1/recommendations` and `GET /api/v1/recommendations/{rec_id}/revisions/{revision_id}`.
    - _Requirements: 5.1, 5.7_

  - [x] 7.3 Write unit tests for Recommendation creation rules
    - Cover 1, 25, and 50 Derived From references (accepted); zero references (rejected); unresolved Finding reference (rejected); rationale and assumptions length boundaries.
    - _Requirements: 5.1, 5.6_

- [x] 8. Implement Decisions
  - [x] 8.1 Implement Decisions Immutable Record with Addresses Relationship
    - Create `src/walking_slice/decisions.py` (or extend `knowledge.py`) with `create_decision` enforcing the `UNIQUE(target_recommendation_id, target_recommendation_revision_id)` rule for Requirement 6.5.
    - Restrict `outcome` to `{Accept, Reject, Defer}` per AD-WS-11 and validate `authority_basis.type ∈ {role-grant-id, scope-id, delegation-chain-id}` per AD-WS-10.
    - Insert the Decision row, the `Addresses` Relationship, the Audit record, the Provenance Manifest, and any Omission Entries in one transaction per AD-WS-5.
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_

  - [x] 8.2 Implement Decision authority check and denial-with-audit retry path
    - Wire `Authorization_Service.evaluate(party, "approve.decision", target=recommendation_revision, at=now())` into the Decision flow.
    - On `deny`, open an audit-only transaction, retry up to three times with exponential backoff per Requirement 7.6, and return the indistinguishable denial response shape from AD-WS-9.
    - _Requirements: 7.1, 7.2, 7.4, 7.5, 7.6_

  - [x] 8.3 Implement Decisions HTTP endpoints
    - Add `routes/decisions.py` exposing `POST /api/v1/recommendations/{rec_id}/decisions` and `GET /api/v1/decisions/{decision_id}`.
    - _Requirements: 6.1, 7.1_

  - [x] 8.4 Write unit tests for unauthorized Decision denial
    - Validate that no Decision row, Relationship, or in-flight write is persisted; that exactly one Denial Record is appended; and that the response body contains only `{generic_denial_indicator, reason_code, correlation_id}`.
    - _Requirements: 7.1, 7.4, 7.5_

  - [x] 8.5 Write property test for Decision authority
    - **Property 2: Decision authority**
    - **Validates: Requirements 6.1, 6.2, 7.1, 7.3, 7.5, 12.2, 12.3, 12.4, 15.2**
    - Generate Parties with role assignments that vary across effective-start, expiration, revocation, scope, and granted-authority dimensions; for every persisted Decision Record, assert a matching `approve`-bearing role assignment exists whose scope covers the target and whose effective period encloses the recorded time.

- [x] 9. Implement Provenance Manifests and Omissions
  - [x] 9.1 Implement Provenance_Manifests and Omission_Entries persistence
    - Create `src/walking_slice/manifests.py` with a `ProvenanceManifestWriter` that records included sources and Omission Entries with categories `{intentional, unavailable, restricted, stale, unresolved}`.
    - Compute `is_complete = 0` whenever any unresolved Omission Entry has a non-intentional category.
    - Enforce the 24-hour default Source Freshness Window for the `stale` category.
    - _Requirements: 10.1, 10.2, 10.3, 10.6_

  - [x] 9.2 Wire manifest writes into Finding, Recommendation, and Decision finalization
    - Insert the Provenance Manifest and any Omission Entries inside the originating transaction in `knowledge.py`, `recommendations.py`, and `decisions.py`.
    - On manifest persistence failure, roll back the originating finalization and return `503 provenance_manifest_persistence_failed`.
    - _Requirements: 10.1, 10.2, 10.6_

  - [x] 9.3 Write property test for Provenance non-omission
    - **Property 7: Provenance non-omission**
    - **Validates: Requirements 10.1, 10.2, 10.3, 15.7**
    - Generate Findings, Recommendations, Decisions, and Trail Revisions with random material sources and intentional/unavailable/restricted/stale/unresolved omissions; assert every material source is either listed in `included_sources_json` or recorded as an Omission Entry with category and non-empty rationale.

- [x] 10. Implement Trail_Service
  - [x] 10.1 Implement Trails, Trail_Revisions, and Trail_Steps with structural validators
    - Create `src/walking_slice/trails.py` enforcing exactly five steps, contiguous ordinals 1..5, target-kind-per-ordinal (`document_revision`, `region_occurrence`, `finding_revision`, `recommendation_revision`, `decision`), and `selection_mode = 'Pinned'` per AD-WS-12.
    - Resolve every target before opening a transaction; on any unresolved target return `400 trail_target_unresolved` with the per-ordinal list and no partial persistence.
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.7_

  - [x] 10.2 Implement material-change detection that creates new Trail Revisions
    - Compare the canonical form of `(purpose, audience_id, ordering_rationale, ordered (ordinal, target_ref, annotation))` against the prior Trail Revision; on difference, insert a new immutable Trail Revision with `predecessor_revision_id` set.
    - _Requirements: 9.4, 9.6_

  - [x] 10.3 Implement Trail_Service HTTP endpoints
    - Add `routes/trails.py` exposing `POST /api/v1/trails`, `POST /api/v1/trails/{trail_id}/revisions`, and `GET /api/v1/trails/{trail_id}/revisions/{revision_id}`.
    - _Requirements: 9.1, 9.4_

  - [x] 10.4 Write unit tests for Trail structural validators
    - Cover 4-step, 6-step, non-contiguous-ordinal, mismatched-target-kind, and Live/Approval-Controlled `selection_mode` rejection.
    - _Requirements: 9.7_

  - [x] 10.5 Write property test for Trail linearity
    - **Property 5: Trail linearity**
    - **Validates: Requirements 9.1, 9.2, 9.3, 9.7, 15.5**
    - Generate full pipelines and Trail inputs that vary ordinal sets and target kinds; assert every persisted Trail Revision has exactly five steps, one per pipeline stage, with `selection_mode = 'Pinned'`.

  - [x] 10.6 Write property test for Trail target resolvability
    - **Property 6: Trail target resolvability**
    - **Validates: Requirements 9.5, 15.6**
    - Generate Trail submissions where 0..5 targets are unresolved; assert that on any unresolved target the Trail Revision is rejected with no partial persistence, OR the persisted Trail Revision records an Omission Entry naming the stage, category, and non-empty rationale.

- [x] 11. Checkpoint - Synthesis pipeline complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement Provenance_Navigator
  - [x] 12.1 Implement backlink discovery with constant-time response shaping
    - Create `src/walking_slice/provenance.py` implementing the backlink algorithm in design §"Provenance_Navigator" — load candidates, build the authorized projection, compute cursor and response size from the authorized projection alone, and emit the response after a fixed latency baseline derived from the authorized response size.
    - Enforce the 500-Relationship page limit per Requirement 8.6.
    - _Requirements: 8.1, 8.2, 8.4, 8.6_

  - [x] 12.2 Implement Decision-to-Evidence provenance traversal
    - Add `navigate_decision(decision_id, party, at)` traversing Decision → Recommendation → Finding(s) → Region Occurrence(s) → Document Revision per design §"Provenance traversal algorithm".
    - Return identical results across repeated invocations for the same `(D, P, t)` per Requirement 11.5.
    - _Requirements: 11.1, 11.2, 11.5, 11.6_

  - [x] 12.3 Implement Region Occurrence text resolution endpoint
    - Add `GET /api/v1/regions/{region_id}/occurrences/{revision_id}/text` returning the byte-equivalent span from the resolved Document Revision and a digest comparison against the stored `span_content_digest_sha256`.
    - _Requirements: 3.4, 11.2_

  - [x] 12.4 Implement Completeness Disclosure policy enforcement
    - Apply the `slice-default-2026` policy from AD-WS-9: replace restricted nodes with `{kind, redacted: true}` markers, return gap descriptors for unavailable/stale/unresolved categories, and normalize observability between restricted-and-nonexistent.
    - _Requirements: 10.5, 10.7, 11.3, 11.4, 11.7_

  - [x] 12.5 Implement Provenance_Navigator HTTP endpoints
    - Add `routes/provenance.py` exposing the six endpoints in design §"Provenance_Navigator" HTTP surface (`/backlinks`, `/decisions/{id}/provenance`, `/findings/{id}/provenance`, `/recommendations/{id}/provenance`, `/trails/{id}/revisions/{revision_id}/provenance`, region text).
    - _Requirements: 8.1, 10.4, 11.1_

  - [x] 12.6 Write property test for Backlink bidirectionality
    - **Property 3: Backlink bidirectionality**
    - **Validates: Requirements 1.5, 8.1, 8.2, 15.3**
    - Generate a relationship graph and a requesting Party with view authority on both `R` and its source endpoint; assert backlink and outbound queries return the same Relationship Identity and attribute values from both directions.

  - [x] 12.7 Write property test for Non-leakage of restricted information
    - **Property 4: Non-leakage of restricted information**
    - **Validates: Requirements 7.4, 8.3, 8.5, 10.5, 11.3, 11.7, 15.4**
    - Generate pairs `(P, P′)` differing only in view authority on one node; assert the responses to `P′` are indistinguishable from responses produced in a universe where the restricted node does not exist, across count, identifier set, ordering, cursor, response size, error wording, and latency (within 100 ms tolerance).

  - [x] 12.8 Write property test for Provenance traversal idempotence
    - **Property 8: Provenance traversal idempotence**
    - **Validates: Requirements 11.5, 15.8**
    - For each generated `(D, P, t)` tuple, invoke `navigate_decision` at least five times and assert equal node identities, attribute values, and ordering.

  - [x] 12.9 Write property test for Navigation back to exact Evidence
    - **Property 9: Navigation back to exact Evidence**
    - **Validates: Requirements 3.4, 11.1, 11.2, 15.9**
    - Generate Decision chains whose provenance is fully visible to the requesting Party; assert the returned span fields are present, the digest equals the recorded `span_content_digest_sha256`, and the returned bytes are byte-equivalent to `content_bytes[start:end]` of the resolved Document Revision.

- [x] 13. Implement Interim ADR records and Disclosure Policy seeding
  - [x] 13.1 Seed Interim_ADR_Records for Gaps G-1 through G-5
    - Create `src/walking_slice/interim_adr.py` that on application startup inserts a row per AD-WS-6 (G-1), AD-WS-7 (G-2), AD-WS-8 (G-3), AD-WS-9 (G-4), and AD-WS-10 (G-5), each recording motivating requirement, criterion, observable behavior, recorded date, and backlog ADR identifier.
    - _Requirements: 16.3_

  - [x] 13.2 Seed slice-default-2026 Disclosure Policy
    - Create `src/walking_slice/disclosure.py` that inserts the `slice-default-2026` row into `Disclosure_Policies` on startup and exposes a lookup used by `Provenance_Navigator`.
    - _Requirements: 10.5, 11.3_

  - [x] 13.3 Write property test for Interim ADR records retrievability
    - **Property 15: Interim ADR records retrievable by backlog ADR identifier**
    - **Validates: Requirements 16.3**
    - For each backlog ADR identifier in `{ADR-HT-002, ADR-HT-003, ADR-HT-004, ADR-HT-005, ADR-HT-008}`, query `Interim_ADR_Records` and assert the complete set of motivating-requirement rows is returned.

- [x] 14. Implement Explainable Projection of slice status
  - [x] 14.1 Implement Projection envelope
    - Create `src/walking_slice/projection.py` with a `ProjectionEnvelope` value object carrying Projection Definition, source Resource Identities, source Revision Identities, applicable temporal boundary (ISO-8601 second precision), generated time, and a derivation indicator.
    - _Requirements: 14.1, 14.2_

  - [x] 14.2 Integrate projection envelope into status-bearing responses
    - Wrap any status-bearing response from `Trail_Service` and `Provenance_Navigator` (e.g., "Trail unresolved", "Provenance incomplete") with the `ProjectionEnvelope`.
    - On unresolvable Projection Definition or missing source Revision, withhold the projected status and return an explanation-unavailable indicator identifying the missing element.
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

  - [x] 14.3 Write unit tests for explainable projection withholding
    - Cover the happy path and the unresolvable-definition path; assert source records are left byte-equivalent when corrections arrive.
    - _Requirements: 14.3, 14.4_

- [x] 15. Application composition and cross-cutting invariants
  - [x] 15.1 Implement bearer token authentication and RequestContext injection
    - Create `src/walking_slice/auth_middleware.py` validating HMAC-signed JWTs with a slice-local key; resolve Party Identity from the token claims.
    - Create the `RequestContext` dataclass from design §"Application-Level Composition" and wire it as a FastAPI dependency.
    - _Requirements: 7.1, 12.1_

  - [x] 15.2 Wire FastAPI app composition and startup database bootstrap
    - Create `src/walking_slice/app.py` constructing the FastAPI router, mounting every `routes/*.py` module, and calling `create_schema`, `interim_adr.seed`, and `disclosure.seed` on startup.
    - _Requirements: 16.1, 16.3_

  - [x] 15.3 Write property test for Audit completeness for consequential and denied actions
    - **Property 11: Audit completeness for consequential and denied actions**
    - **Validates: Requirements 2.5, 6.4, 7.2, 7.6, 12.5, 13.1, 13.2, 15.11**
    - Generate sequences of writes (Document Revision, Region Occurrence, Finding, Recommendation, Decision, Trail Revision, Trail Step, Relationship, Role Assignment, Role Assignment revocation) and denial attempts; assert a matching `Audit_Records` row exists with the correct `actor_party_id`, `action_type`, `target_id`, `target_revision_id`, `outcome`, `recorded_at`, and `correlation_id`, and that denied attempts left no in-flight write.

  - [x] 15.4 Write property test for Append-only immutability across all immutable tables
    - **Property 12: Append-only immutability across all immutable tables**
    - **Validates: Requirements 2.4, 2.7, 4.4, 6.6, 7.5, 13.3, 13.4, 13.5, 13.6, 15.12**
    - Generate sequences of operations that attempt update or delete against every immutable table; assert previously inserted rows remain byte-equivalent and `Audit_Records.append_sequence` is monotonically non-decreasing by `recorded_at`.

  - [x] 15.5 Write end-to-end HTTP integration tests for the five named demonstrations
    - Drive the FastAPI app via `httpx.AsyncClient` and exercise: authorization-aware backlinks, one linear Trail, omission-aware provenance, denied unauthorized Decision, and navigation back to exact Evidence.
    - _Requirements: 7.1, 8.1, 9.1, 10.4, 11.1_

  - [x] 15.6 Configure repeatable property runs with seed capture
    - **Property 13: Repeatable property runs (operational)**
    - **Validates: Requirements 15.13**
    - Configure Hypothesis profiles (`@settings(max_examples=100, deadline=2000)`), enable `--hypothesis-seed` capture, persist the seed of every property test invocation to a build artifact, and add a re-execution check confirming identical pass/fail outcomes and minimal counterexamples.

- [x] 16. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP; the core implementation tasks (no `*`) are mandatory.
- Property tests directly map to design §"Correctness Properties" — Property 10 lives near Identity_Service, Properties 1, 2, 5, 6, 7, 14 sit beside their producing components, and Properties 3, 4, 8, 9, 11, 12, 13, 15 land in the navigation and cross-cutting waves where the system is wired enough to verify them.
- The Interim ADR records seeded in task 13.1 close Gaps G-1 through G-5 from requirements.md and unblock acceptance of the backlog ADRs (HT-002 through HT-005, HT-008) without re-implementing the slice.
- Checkpoints sit after the foundational services (task 4), after the synthesis pipeline (task 11), and at the end (task 16); each is a manual verification gate.
- All testing uses `pytest` with Hypothesis configured per design §"Testing Strategy"; each property test runs at least 100 generated cases with deterministic seed capture per Requirement 15.13.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1"] },
    { "id": 1,  "tasks": ["1.2"] },
    { "id": 2,  "tasks": ["1.3"] },
    { "id": 3,  "tasks": ["2.1", "3.1"] },
    { "id": 4,  "tasks": ["2.2", "3.2"] },
    { "id": 5,  "tasks": ["2.3", "2.4", "3.3", "3.4", "5.1"] },
    { "id": 6,  "tasks": ["5.2"] },
    { "id": 7,  "tasks": ["5.3"] },
    { "id": 8,  "tasks": ["5.4", "5.5", "5.6", "6.1"] },
    { "id": 9,  "tasks": ["6.2", "6.3", "6.4", "7.1"] },
    { "id": 10, "tasks": ["7.2", "7.3", "8.1"] },
    { "id": 11, "tasks": ["8.2"] },
    { "id": 12, "tasks": ["8.3", "8.4", "9.1"] },
    { "id": 13, "tasks": ["9.2"] },
    { "id": 14, "tasks": ["8.5", "9.3", "10.1", "13.1", "13.2", "14.1"] },
    { "id": 15, "tasks": ["10.2", "14.2"] },
    { "id": 16, "tasks": ["10.3", "10.4", "13.3", "14.3"] },
    { "id": 17, "tasks": ["10.5", "10.6", "12.1"] },
    { "id": 18, "tasks": ["12.2"] },
    { "id": 19, "tasks": ["12.3", "12.4"] },
    { "id": 20, "tasks": ["12.5", "15.1"] },
    { "id": 21, "tasks": ["12.6", "12.7", "12.8", "12.9", "15.2"] },
    { "id": 22, "tasks": ["15.3", "15.4", "15.5", "15.6"] }
  ]
}
```
