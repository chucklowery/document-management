# Requirements Document

## Introduction

This document specifies the requirements for the **first walking slice** of the Organizational Knowledge and Work System: the minimum end-to-end software capability that proves the foundational system model with real running code.

The slice realizes the pipeline:

```text
Source Evidence → Content Region → Finding → Recommendation → Authorized Decision
```

and must demonstrate five named behaviors end-to-end:

1. authorization-aware backlinks,
2. one linear Trail,
3. omission-aware provenance,
4. denial of unauthorized decisions, and
5. navigation back to exact Evidence.

The slice is **executable system behavior**, distinct from documentation work. It is the software realization of *Slice 1 — Evidence to Decision* defined in [`documents/06-thin-vertical-slices.md`](../../../documents/06-thin-vertical-slices.md), constrained by the foundational system model in [`documents/00-project-constitution.md`](../../../documents/00-project-constitution.md) §2 and by the Hypertext Knowledge Integrity Amendment ([`documents/00.06-hypertext-knowledge-integrity-amendment.md`](../../../documents/00.06-hypertext-knowledge-integrity-amendment.md)).

These requirements reconcile with, and do not duplicate, the upstream authoritative documents. Where an upstream User Story (`US-*`), EARS Requirement (`REQ-*`), Acceptance Specification (`AS-*`), or Architecture Decision (`ADR-HT-*`) already governs a behavior, the corresponding requirement in this document carries an explicit **Traceability** block and refines that behavior to the walking-slice scope. Where no upstream identifier exists, the requirement is flagged as a **Gap** for resolution before implementation.

### Scope of this slice

In scope:

- Capturing source Evidence as a managed `Source Document` with stable identity and an immutable `Document Revision`.
- Identifying an addressable `Content Region` within a Document Revision.
- Creating a `Finding` whose support is one or more exact `Content Region` occurrences (or an explicit hypothesis flag).
- Creating a `Recommendation` derived from one or more Findings.
- Recording an `Authorized Decision` that accepts, rejects, or defers a Recommendation.
- One linear `Trail` linking each stage of the pipeline.
- Authorization-aware backlink and provenance navigation.
- Audit records for denied unauthorized Decisions.

Out of scope for this slice (deferred to later slices or future increments — see §8):

- Decision-to-plan, plan-to-execution, execution-to-outcome, and learning-to-adaptation flows (Slices 2–5).
- Publication assembly, rendered outputs, and reproducible Published Versions (Slice 6).
- Investment traceability (Slice 7).
- Branched or alternative Trails; Trail Adoption authority workflow.
- Approval-Controlled and Live reference modes for source adoption.
- Governed historical withdrawal/redaction/erasure (US-043).
- Portability export (US-045).
- Automated Agent contribution provenance (US-046).
- Attention governance (US-048).

## Glossary

This glossary names the systems and terms required by the requirements below. Defined Capitalized Terms not redefined here carry the meaning given in [`documents/01-domain-glossary.md`](../../../documents/01-domain-glossary.md) and [`documents/01.10-hypertext-canonical-terms.md`](../../../documents/01.10-hypertext-canonical-terms.md).

- **Walking_Slice_System**: The complete software realization of this slice, comprising all named sub-systems below. References to "the system" in upstream documents map to this term when applied to the slice.
- **Identity_Service**: The Walking_Slice_System sub-system responsible for generating, validating, and resolving durable identities for Resources, Resource Revisions, Relationships, Content Regions, Immutable Records, Trails, and Projections, as constrained by [`ADR-HT-001`](../../../documents/13.01-adr-ht-001-durable-identity-strategy.md).
- **Evidence_Repository**: The sub-system that stores Source Documents, Document Revisions, and Content Region occurrences with their authority designation and provenance.
- **Knowledge_Service**: The sub-system that records Findings, Recommendations, Decisions, and the typed Relationships among them.
- **Authorization_Service**: The sub-system that enforces contextual roles, scopes, and effective periods for view, modify, and approve actions; emits denial Audit Records.
- **Trail_Service**: The sub-system that records Trails, Trail Revisions, and Trail Steps for the slice.
- **Provenance_Navigator**: The sub-system that exposes navigable provenance and backlinks subject to authorization and inference-risk policy.
- **Audit_Log**: The append-only Immutable Record store for consequential actions and denied attempts within the slice.
- **Source Evidence**: A managed Source Document Revision identified for use as Evidence within the slice. (Refines `Evidence` in §8 of the Domain Glossary to the specific kinds in scope.)
- **Content Region**: As defined in [`documents/01-domain-glossary.md`](../../../documents/01-domain-glossary.md) §4. Within the slice, addressable text spans within a Document Revision.
- **Finding**: As defined in [`documents/01-domain-glossary.md`](../../../documents/01-domain-glossary.md) §8.
- **Recommendation**: As defined in [`documents/01-domain-glossary.md`](../../../documents/01-domain-glossary.md) §8.
- **Authorized Decision**: A Decision (Glossary §8) recorded by a Party holding effective `Decision Maker` role authority for the applicable scope, per [`documents/05-user-roles.md`](../../../documents/05-user-roles.md) §4.2.
- **Linear Trail**: A Trail Revision (per [`documents/03.08-trail-domain-model.md`](../../../documents/03.08-trail-domain-model.md)) whose Trail Steps form one ordered sequence with no alternative branches.
- **Omission-Aware Provenance**: Provenance that records material sources, transformations, selection rules, exclusions, and unavailable/restricted/stale/unresolved information, per [`documents/01.10-hypertext-canonical-terms.md`](../../../documents/01.10-hypertext-canonical-terms.md) §"Omission-Aware Provenance".
- **Authorization-Aware Backlink**: A reverse-direction Relationship discovery result filtered by the inspecting Party's effective authority, per `REQ-HT-001` and `REQ-HT-002`.
- **Denial Record**: An Immutable Record created by the Audit_Log when the Authorization_Service rejects a consequential action, per `REQ-IG-005`.

## Requirements

### Requirement 1: Durable Identity Foundation

**User Story:** As an implementer of the walking slice, I want all managed identities produced by the slice to conform to the durable identity strategy, so that Resources, Revisions, Relationships, Regions, Records, and Trails created in this slice remain referenceable across future bounded contexts, exports, and migrations.

**Traceability:**
- Stories: US-001, US-002, US-031
- Requirements: REQ-SF-001, REQ-SF-002, REQ-SF-004, REQ-HT-023
- Acceptance: AS-001.1, AS-001.2
- ADRs: ADR-HT-001 (prerequisite)
- Invariants: identity survives movement; identifier never reused; principal identifier is opaque.

#### Acceptance Criteria

1. WHEN the Identity_Service creates a managed Resource, Resource Revision, Relationship, Content Region, Immutable Record, Trail, Trail Revision, or Trail Step, THE Identity_Service SHALL assign exactly one UUID version 7 identifier in canonical lowercase hyphenated 8-4-4-4-12 hex form, exactly once, before the entity becomes referenceable, as specified in `ADR-HT-001` §1.
2. THE Identity_Service SHALL hold Resource Identity and Resource Revision Identity as two distinct values for every managed Resource and SHALL hold Trail Identity and Trail Revision Identity as two distinct values for every managed Trail, with cardinality one Resource Identity to one or more Resource Revision Identities and one Trail Identity to one or more Trail Revision Identities, and no Revision Identity shared across Resources or Trails, per `ADR-HT-001` §4.
3. WHEN an authorized actor renames or relocates a Source Document within the Evidence_Repository, THE Identity_Service SHALL preserve the existing Resource Identity and every existing Resource Revision Identity unchanged, generate no new Resource Identity, and replace no existing identity.
4. IF an identifier generation, import, or reference operation would assign an existing identifier to different domain content, or would introduce a malformed identifier, THEN THE Identity_Service SHALL reject the operation, return an error indication identifying the conflicting identifier, leave the existing identifier bound to its original content unchanged, and append a Denial Record to the Audit_Log within the same operation, per `ADR-HT-001` §15.
5. WHEN the Provenance_Navigator resolves a Relationship from either its source endpoint or its target endpoint, THE Identity_Service SHALL return the same single authoritative Relationship Identity from both source-direction and backlink queries, per `ADR-HT-001` §6.
6. THE Identity_Service SHALL NOT reassign a once-assigned identifier to different domain content, even after withdrawal, redaction, retention expiry, or deletion of the original content, per `ADR-HT-001` §3 and §14.
7. THE Identity_Service SHALL NOT encode mutable name, repository path, organization name, security classification, lifecycle state, authority, semantic version, owning Party, or other business meaning into any issued identifier, per `ADR-HT-001` §2.

### Requirement 2: Capture Source Evidence

**User Story:** As a Researcher, I want to record interview or document content as managed Source Evidence, so that downstream Findings, Recommendations, and Decisions trace back to durable source material.

**Traceability:**
- Stories: US-001
- Requirements: REQ-SF-002, REQ-SF-007, REQ-KP-001, REQ-KP-002
- Acceptance: AS-001.1
- Invariants: stable identity, explicit authority, immutable Revisions.

#### Acceptance Criteria

1. WHEN a Researcher submits new source content of between 1 byte and 100 megabytes with a non-empty contributing Party identity to the Evidence_Repository, THE Evidence_Repository SHALL create a Source Document Resource and an associated immutable Document Revision within a nominal 5 seconds.
2. WHEN the Evidence_Repository creates a Document Revision, THE Evidence_Repository SHALL record the recorded time as UTC with millisecond precision, the resolved contributing Party Identifier, and a content digest computed over the full byte content of the Revision, per `ADR-HT-001` §5.
3. WHERE the submitted content originates from an external system, THE Evidence_Repository SHALL record the external identifier of 1 to 256 characters, the source system identifier of 1 to 128 characters, and an authority designation drawn from the authority enumeration used by `REQ-KP-002`, per `REQ-SF-007` and `REQ-KP-002`.
4. IF a Researcher attempts to modify the content digest, recorded time, contributing Party identity, or external-system fields of an existing Document Revision, THEN THE Evidence_Repository SHALL reject the modification, return an error indication to the caller, leave the existing Document Revision unchanged, and require creation of a new Document Revision.
5. WHEN a Document Revision is recorded, THE Audit_Log SHALL append an immutable creation record identifying the Resource Identity, Revision Identity, Party, and recorded time within 1 second of Revision creation and within the same Revision-creation transaction.
6. IF the submitted content is empty, exceeds 100 megabytes, or omits a contributing Party identity, THEN THE Evidence_Repository SHALL reject the submission, decline to create a Source Document Resource or Document Revision, and return an error indication identifying the failing validation.
7. IF the Audit_Log append for a Document Revision creation fails, THEN THE Evidence_Repository SHALL roll back the Document Revision creation, decline to make the Revision referenceable, and return an error indication identifying the audit append failure.

### Requirement 3: Identify an Addressable Content Region

**User Story:** As a Researcher, I want to mark an exact span of text within a Document Revision as a Content Region, so that later Findings can cite the precise evidence on which they rely.

**Traceability:**
- Stories: US-002
- Requirements: REQ-KP-003, REQ-KP-004
- Acceptance: AS-001.2, AS-001.3
- Invariants: region identity survives ordinary movement; historical citations remain resolvable.

#### Acceptance Criteria

1. WHEN a Researcher identifies a non-empty span fully contained within the bounded text of a Document Revision as a Content Region, THE Evidence_Repository SHALL assign a stable Region Identity per `ADR-HT-001` §7 and record a Region Occurrence within the exact Document Revision.
2. THE Evidence_Repository SHALL record, for each Region Occurrence, the owning Document Revision Identity, start anchor, end anchor, and content digest of the bounded span.
3. WHEN a later Document Revision of the same Source Document is recorded, THE Evidence_Repository SHALL preserve every prior Region Occurrence such that its Region Identity, owning Document Revision Identity, start anchor, end anchor, content digest, and bounded text span remain resolvable without alteration.
4. WHEN an authorized user resolves a Content Region reference that corresponds to a recorded Region Occurrence, THE Provenance_Navigator SHALL return the exact Document Revision Identity, Region Identity, Region Occurrence, and a bounded text span byte-equivalent to the span originally recorded for that Region Occurrence.
5. IF a Researcher submits a span that is empty, extends outside the bounded text of the target Document Revision, or has a start anchor positioned at or after its end anchor, THEN THE Evidence_Repository SHALL reject the request, decline to assign a Region Identity or record a Region Occurrence, and return an error indication identifying the invalid span condition.
6. IF an authorized user resolves a Content Region reference that does not correspond to any recorded Region Occurrence, THEN THE Provenance_Navigator SHALL decline to return a bounded text span and return an error indication identifying the unresolvable reference.

### Requirement 4: Create a Finding Linked to Exact Evidence

**User Story:** As an Analyst, I want to record a Finding that cites one or more exact Content Region occurrences, so that interpretations stay traceable to the supporting evidence.

**Traceability:**
- Stories: US-003
- Requirements: REQ-KP-005, REQ-KP-006, REQ-SF-004
- Acceptance: AS-001.4, AS-001.5
- Invariants: Findings are evidence-backed or explicitly uncertain; competing interpretations may coexist.

#### Acceptance Criteria

1. WHEN an Analyst submits a Finding for finalization, THE Knowledge_Service SHALL require either at least one `Supports` Relationship to a Content Region Occurrence identified by Content Region Identity, or a hypothesis designation explicitly set to true on the Finding by the authoring Analyst.
2. WHEN the Knowledge_Service records a `Supports` Relationship, THE Knowledge_Service SHALL record source Resource Identity, source Revision Identity, target Content Region Identity, Relationship Type, authoring Party, and recorded time as a timestamp with at least second precision, per `REQ-SF-004`.
3. IF an Analyst attempts to finalize a non-hypothesis Finding with zero supporting Content Region Occurrences, THEN THE Knowledge_Service SHALL reject the finalization, SHALL NOT create the Finding record, and SHALL return an error indication to the requesting Analyst identifying the missing supporting-evidence requirement.
4. WHEN an Analyst asserts that a newly created Finding contradicts an existing Finding, THE Knowledge_Service SHALL preserve both Finding records unchanged and SHALL record a `Contradicts` Relationship between them capturing source Finding Identity, target Finding Identity, authoring Party, and recorded time, per `REQ-KP-006`.
5. WHERE a Finding cites more than one Content Region Occurrence, THE Knowledge_Service SHALL record a separate `Supports` Relationship for each cited Content Region Occurrence.

### Requirement 5: Create a Recommendation Derived from a Finding

**User Story:** As an Analyst, I want to record a Recommendation derived from one or more Findings, so that a proposed course of action is distinct from the interpretive analysis that justifies it.

**Traceability:**
- Stories: US-005
- Requirements: REQ-KP-007
- Acceptance: AS-002.1
- Invariants: Recommendations remain distinct from Decisions; provenance does not imply endorsement.

#### Acceptance Criteria

1. WHEN an authenticated Analyst creates a Recommendation, THE Knowledge_Service SHALL record between 1 and 50 `Derived From` Relationships from the Recommendation to Findings, each of which resolves to an existing Finding Resource at creation time, and SHALL complete the creation within 2 seconds.
2. THE Knowledge_Service SHALL store the Recommendation as a Resource whose type identifier is `Recommendation`, with Resource Identity not shared with any Finding or Decision Resource, and not retrievable via Finding or Decision lookups.
3. WHERE the Analyst provides rationale, THE Knowledge_Service SHALL preserve rationale text of 1 to 10,000 characters on the Recommendation Revision.
4. WHERE the Analyst provides assumptions, THE Knowledge_Service SHALL preserve between 0 and 50 assumption entries, each of 1 to 2,000 characters, on the Recommendation Revision.
5. WHERE the Analyst provides a confidence designation, THE Knowledge_Service SHALL preserve a confidence value drawn from the enumerated set {Low, Medium, High} on the Recommendation Revision.
6. IF an Analyst attempts to record a Recommendation with zero `Derived From` Relationships or with any `Derived From` reference that does not resolve to an existing Finding, THEN THE Knowledge_Service SHALL reject the action, decline to create any Resource or Revision, and return an error response indicating that at least one valid Finding reference is required.
7. IF the requester is unauthenticated or does not hold an effective Analyst role for the applicable scope, THEN THE Knowledge_Service SHALL reject the Recommendation creation, decline to create any Resource or Revision, and return an authorization-denial response, per `REQ-IG-002` and `REQ-IG-003`.

### Requirement 6: Record an Authorized Decision

**User Story:** As a Decision Maker, I want to accept, reject, or defer a Recommendation, so that organizational direction is explicit, authorized, and accountable.

**Traceability:**
- Stories: US-006
- Requirements: REQ-KP-008, REQ-SF-003, REQ-IG-003
- Acceptance: AS-002.2
- Invariants: authority is explicit; consequential records are immutable.

#### Acceptance Criteria

1. WHEN a Party holding effective Decision Maker authority for the applicable scope submits a Decision on a Recommendation Revision that is in a decidable state (not already decided and not withdrawn), THE Knowledge_Service SHALL create an immutable Decision Immutable Record within 5 seconds.
2. THE Decision Immutable Record SHALL identify the target Recommendation Identity and Revision Identity, decision outcome drawn from the enumerated set {Accept, Reject, Defer}, rationale text of 1 to 4,000 characters, deciding Party Identity, authority basis drawn from the defined set {role-grant identity, scope identity, delegation chain}, applicable scope, and recorded time expressed in UTC with at least second precision.
3. WHEN the Knowledge_Service creates a Decision Record, THE Knowledge_Service SHALL link the Decision Record to its Recommendation through exactly one `Addresses` Relationship.
4. WHEN the Knowledge_Service creates a Decision Record, THE Audit_Log SHALL append a corresponding record of the consequential Decision action, including authority basis and the Decision Record Identity, within 1 second of and in the same operation as the Decision Record creation.
5. IF a Party submits a Decision for a Recommendation Revision that is already the target of a finalized Decision Record, THEN THE Knowledge_Service SHALL reject the submission, decline to create a Decision Record, and return an error indication identifying the duplicate-decision condition.
6. IF an actor attempts to modify or delete a previously created Decision Immutable Record, THEN THE Knowledge_Service SHALL reject the operation, leave the Decision Record byte-equivalent to its prior state, and return an error indication identifying the immutability violation.
7. IF the submitted Decision omits a required attribute (target Recommendation Identity, target Recommendation Revision Identity, decision outcome, rationale, deciding Party Identity, authority basis, applicable scope, or recorded time), THEN THE Knowledge_Service SHALL reject the submission, decline to create a Decision Record, and return an error indication identifying the missing attribute.

### Requirement 7: Deny Unauthorized Decisions (Demonstration #4)

**User Story:** As a Security Auditor, I want any attempt to record a Decision by a Party lacking applicable authority to be rejected and audited, so that decision authority cannot be silently bypassed.

**Traceability:**
- Stories: US-006, US-032
- Requirements: REQ-KP-009, REQ-IG-002, REQ-IG-003, REQ-IG-005
- Acceptance: AS-002.3, AS-007.2, AS-007.3
- Invariants: privileged actions are restricted and auditable; sensitive information does not leak through denial.

#### Acceptance Criteria

1. IF a Party attempts to finalize a Decision while lacking effective Decision Maker authority for the applicable scope, THEN THE Authorization_Service SHALL reject the action within 2 seconds and the Knowledge_Service SHALL ensure no Decision Record is created, modified, or persisted.
2. WHEN the Authorization_Service rejects a Decision attempt, THE Audit_Log SHALL append exactly one immutable Denial Record within 1 second containing actor Identity, attempted action, target Recommendation Identity and Revision Identity, recorded time in UTC with millisecond precision, and denial reason code drawn from the enumerated set {not-yet-effective, expired, revoked, out-of-scope, no-role-assignment}.
3. WHEN evaluating authority for any consequential action, THE Authorization_Service SHALL treat a role assignment as not in effect if its effective-start time is in the future, its expiration time has passed, its revocation has been recorded, or its scope does not cover the target Resource, per `REQ-IG-002`.
4. WHEN the Authorization_Service rejects an action because of missing authority, THE Authorization_Service SHALL return a denial response containing only a generic denial indicator, the denial reason code, and a correlation identifier, and SHALL NOT contain authorized Party identities, Recommendation contents, role assignment details, target existence beyond the requesting Party's view authority, or other attribute values, per `REQ-IG-004`.
5. THE Knowledge_Service SHALL leave the targeted Recommendation Resource, all Recommendation Revisions, and all previously acknowledged Relationships and Records linked to it byte-equivalent to their state immediately before the denied Decision attempt.
6. IF the Audit_Log append for a denied Decision attempt fails, THEN THE Authorization_Service SHALL retry up to 3 times, keep the action denied, and surface an audit-failure indicator to the operator so that denial and audit cannot silently diverge.

### Requirement 8: Authorization-Aware Backlinks (Demonstration #1)

**User Story:** As a Source Owner, I want to see which Findings, Recommendations, Decisions, and Trail Steps depend on a Document Revision or Content Region, subject to my authorization, so that I can understand downstream impact without leaking restricted relationships.

**Traceability:**
- Stories: US-036, US-007
- Requirements: REQ-HT-001, REQ-HT-002, REQ-SF-004, REQ-IG-004
- Acceptance: AS-010.1, AS-010.2, AS-010.3
- Invariants: bidirectional discovery, no inference leakage, discovery does not transfer authority.

#### Acceptance Criteria

1. WHEN an authorized Party holding view authority on the queried endpoint requests inbound Relationships for a Source Document, Document Revision, Content Region, Finding, Recommendation, Decision, or Trail Step, THE Provenance_Navigator SHALL return every inbound Relationship for which the requesting Party holds applicable view authority on both the Relationship and its source endpoint, in deterministic ordering, within 2 seconds for result sets of up to 500 backlinks.
2. WHEN the Provenance_Navigator returns a backlink, THE Provenance_Navigator SHALL identify the backlink by its Relationship Identity, Relationship Type, source endpoint Identity, source endpoint Type, source endpoint Revision Identity, and authoring Party Identity, per `ADR-HT-001` §6.
3. IF the requesting Party lacks authority to know that an inbound Relationship or its source endpoint exists, THEN THE Provenance_Navigator SHALL omit the Relationship from results and SHALL produce results indistinguishable in counts, identifier sets, ordering positions, pagination cursors, response size, and latency (within 100 milliseconds variation) from results in which the omitted Relationships do not exist, per `REQ-HT-002` and `REQ-IG-004`.
4. THE Provenance_Navigator SHALL NOT grant the requesting Party any view, modify, or approve authority on the source endpoint, on the Relationship Identity itself, or on any traversed Revisions or Content Regions of the source endpoint, solely as a result of returning a backlink, per `AS-010.2`.
5. IF the requesting Party is unauthenticated or lacks view authority on the queried endpoint, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from a response for a non-existent endpoint, per `REQ-IG-004`.
6. THE Provenance_Navigator SHALL bound each backlink response to at most 500 Relationships and SHALL provide a continuation reference whose length, identifier values, and presence do not vary based on the existence of Relationships the requesting Party lacks authority to know.

### Requirement 9: One Linear Trail (Demonstration #2)

**User Story:** As a Trail Author, I want to record one ordered, linear Trail that walks a reader from Source Evidence through Content Region, Finding, Recommendation, to Authorized Decision, so that a stated audience can follow the slice's reasoning path.

**Traceability:**
- Stories: US-039
- Requirements: REQ-HT-006, REQ-HT-007, REQ-SF-002
- Acceptance: AS-011.1, AS-011.2
- Invariants: Trail identity is distinct from referenced objects; material changes create a new Trail Revision; Trail ordering does not alter source meaning. (See [`documents/03.08-trail-domain-model.md`](../../../documents/03.08-trail-domain-model.md) §6.)

#### Acceptance Criteria

1. WHEN a Trail Author records a Trail for the slice, THE Trail_Service SHALL create a Trail Resource (whose Resource Identity is distinct from every referenced endpoint Identity) and an immutable Trail Revision containing exactly one ordered sequence of exactly five Trail Steps and no alternative or branch steps, within 5 seconds.
2. THE Trail_Service SHALL require, for each in-scope Trail Revision, that the Trail Steps carry ordinals 1 through 5 in ascending order without gaps, with ordinal 1 referencing an exact Document Revision (Source Evidence), ordinal 2 a Content Region Occurrence, ordinal 3 a Finding Revision, ordinal 4 a Recommendation Revision, and ordinal 5 a Decision Immutable Record.
3. THE Trail_Service SHALL record on each Trail Step its target reference identity, a selection mode value `Pinned`, an optional annotation of 0 to 2,000 characters of plain text, and an ordinal that is a unique integer from 1 to 5 within the Trail Revision, per [`documents/03.08-trail-domain-model.md`](../../../documents/03.08-trail-domain-model.md) §3.3.
4. WHEN a Trail Author materially changes Trail purpose text, audience identifier, Trail Step ordering, any Trail Step target reference, any Trail Step annotation, or any stated omission, THE Trail_Service SHALL create a new Trail Revision that links to its immutable predecessor Revision by identity and SHALL preserve the prior Trail Revision unchanged, per `REQ-HT-007`.
5. IF one or more Trail Step target references cannot be resolved to an immutable target Revision at the time of Trail Revision creation, THEN THE Trail_Service SHALL reject the entire Trail Revision request with no partial persistence, return an error indication identifying each unresolved Trail Step by ordinal and target reference, and require either resolution of the references or recording them as stated omissions per Requirement 10.
6. THE Trail_Service SHALL record on each Trail Revision the stated purpose of 1 to 500 characters, the stated audience identifier, and an ordering rationale of 0 to 500 characters, per the user story's requirement that a stated audience can follow the reasoning path.
7. IF a Trail submission contains fewer than 5 or more than 5 Trail Steps, contains ordinals that are not the contiguous integers 1 through 5, or contains a Trail Step whose target kind does not match the pipeline stage of its ordinal, THEN THE Trail_Service SHALL reject the submission, decline to create a Trail Revision, and return an error indication identifying the structural validation failure.

### Requirement 10: Omission-Aware Provenance (Demonstration #3)

**User Story:** As a Reviewer, I want every synthesis in the slice (Finding, Recommendation, Decision, Trail Revision) to record material sources and material omissions, so that I can assess what was used, what was excluded, and what was unavailable.

**Traceability:**
- Stories: US-042, US-044
- Requirements: REQ-HT-012, REQ-HT-013, REQ-HT-014
- Acceptance: AS-012.2, AS-012.3
- Invariants: no unjustified completeness claim; restricted information disclosure follows policy.

#### Acceptance Criteria

1. WHEN the Knowledge_Service finalizes a Finding, Recommendation, or Decision, THE Knowledge_Service SHALL record a provenance manifest identifying every material source Identity and Revision Identity actually used to produce the synthesis, where a material source is any source whose content or interpretation contributed to a claim, rationale, or recommendation conclusion of the synthesis.
2. WHEN the authoring Party intentionally excludes a known material source from a Finding, Recommendation, or Decision, THE Knowledge_Service SHALL record an explicit Omission Entry on the provenance manifest identifying the excluded source Identity, the excluded source Revision Identity when known, an exclusion rationale of 1 to 2,000 characters, the authoring Party Identity, and a recorded time at second precision.
3. IF a material source is in one of the omission categories {unavailable, restricted, stale, unresolved}, where "stale" means the source has not been refreshed within the configured Source Freshness Window (default 24 hours), THEN THE Knowledge_Service SHALL record an Omission Entry identifying the omission category and SHALL mark the synthesis incomplete until each such Omission Entry is resolved, per `REQ-HT-013`.
4. WHEN an authorized Reviewer requests the provenance of a Finding, Recommendation, Decision, or Trail Revision, THE Provenance_Navigator SHALL return the recorded sources, transformations, selection rules, and Omission Entries within 5 seconds for manifests of up to 500 entries.
5. IF disclosure of an Omission Entry's existence would itself be unsafe or unauthorized, THEN THE Provenance_Navigator SHALL apply the configured Completeness Disclosure policy, return a policy-conformant placeholder that does not reveal restricted identifiers or rationale beyond that policy, and record the redaction action, per `REQ-HT-014`.
6. IF persisting the provenance manifest fails during Finding, Recommendation, or Decision finalization, THEN THE Knowledge_Service SHALL reject the finalization, decline to make the synthesis referenceable, preserve any in-progress draft, and return an error indication identifying the manifest persistence failure.
7. IF an unauthorized Party requests provenance, THEN THE Provenance_Navigator SHALL reject the request with an authorization-denial response and SHALL constrain any disclosure to the configured Completeness Disclosure policy, per `REQ-IG-004`.

### Requirement 11: Navigation Back to Exact Evidence (Demonstration #5)

**User Story:** As a Decision Reviewer, I want to navigate from any Authorized Decision back through its Recommendation, supporting Findings, and exact Content Region Occurrences to the precise Document Revision text, so that I can verify why the Decision was recorded.

**Traceability:**
- Stories: US-007
- Requirements: REQ-KP-010, REQ-SF-005, REQ-OM-014 (gap-filtered for this slice)
- Acceptance: AS-002.4
- Invariants: provenance is end-to-end and authorization-aware; missing links are visible; sensitive information does not leak.

#### Acceptance Criteria

1. WHEN an authorized Party requests the provenance chain of a Decision Immutable Record, THE Provenance_Navigator SHALL return an ordered traversal Decision → Recommendation Revision → Finding Revision(s) → Content Region Occurrence(s) → Document Revision identifying each node by its Identity and (where applicable) Revision Identity.
2. WHEN the Provenance_Navigator returns a Content Region Occurrence in a provenance chain, THE Provenance_Navigator SHALL include the exact start anchor, end anchor, and bounded text span of that Occurrence in the originating Document Revision, byte-equivalent to the text recorded for that Region Occurrence and digest-matching against the recorded content digest.
3. IF a node in the provenance chain is restricted from the requesting Party, THEN THE Provenance_Navigator SHALL replace that node with a policy-conformant redaction marker containing only a generic redaction indicator and the original node kind, and SHALL NOT disclose any identifier, count, or attribute value of the redacted node beyond policy, per `REQ-IG-004`.
4. IF a required upstream link is unresolved, stale, or unavailable, THEN THE Provenance_Navigator SHALL identify the gap explicitly with a gap descriptor identifying the stage in the chain, the gap category drawn from {unavailable, restricted, stale, unresolved}, and the Identity of the next reachable node where applicable, per `REQ-OM-014`.
5. THE Provenance_Navigator SHALL produce the same provenance chain for the same Decision Identity, requesting Party authority set, and effective time inputs (idempotent retrieval), within 5 seconds for chains of up to 50 nodes.
6. IF the requested Decision Identity does not resolve to a Decision Immutable Record, THEN THE Provenance_Navigator SHALL return an error indication identifying the unresolvable Decision reference and SHALL NOT disclose existence of any related Resources.
7. IF the requesting Party is unauthenticated or lacks any view authority on the Decision Immutable Record itself, THEN THE Provenance_Navigator SHALL return a response indistinguishable in form and timing from one for a non-existent Decision, per `REQ-IG-004`.

### Requirement 12: Contextual Role Assignment and Enforcement

**User Story:** As a Resource Steward, I want contextual roles assigned with explicit scope and effective period, and enforced for every consequential action in the slice, so that authority is auditable and bounded.

**Traceability:**
- Stories: US-031
- Requirements: REQ-IG-001, REQ-IG-002, REQ-IG-003
- Acceptance: AS-007.1, AS-007.2

#### Acceptance Criteria

1. WHEN a Resource Steward assigns a role to a Party, THE Authorization_Service SHALL record Party Identity, role, scope, granted authorities drawn from the enumerated set {view, modify, approve}, effective period expressed as a start time and an optional end time each in UTC with at least second precision, and assigning authority.
2. WHILE the current time is before the role assignment's effective-start time, after its expiration time, after its recorded revocation time, or when the target Resource is outside the role assignment's scope, THE Authorization_Service SHALL deny actions requiring that role with a reason code drawn from {not-yet-effective, expired, revoked, out-of-scope}.
3. THE Authorization_Service SHALL distinguish view authority from modify authority and from approve authority as three distinct authority types.
4. WHEN the Authorization_Service evaluates a consequential action, THE Authorization_Service SHALL evaluate the action against the specific authority type it requires and SHALL NOT substitute one authority type for another, per `REQ-IG-003`.
5. WHEN the Authorization_Service evaluates a consequential action, THE Authorization_Service SHALL append an immutable decision record to the Audit_Log identifying the actor, attempted action, target Identity, evaluated role assignment Identity, authorities required, authorities held, decision outcome (permit or deny), reason code when denied, and recorded time.
6. IF a role assignment lacks any of Party Identity, role, scope, granted authorities, or effective-start time, THEN THE Authorization_Service SHALL reject the assignment, decline to record it, and return an error indication identifying the missing field.

### Requirement 13: Audit of Consequential and Denied Actions

**User Story:** As an Auditor, I want every consequential creation and every denied unauthorized attempt within the slice to leave an immutable Audit_Log record, so that I can reconstruct what happened and what was rejected.

**Traceability:**
- Stories: US-006, US-032, US-033
- Requirements: REQ-SF-003, REQ-IG-005, REQ-IG-006
- Acceptance: AS-002.3, AS-007.4

#### Acceptance Criteria

1. WHEN the Walking_Slice_System finalizes the creation of a Document Revision, Content Region, Finding, Recommendation, Decision Record, Trail Revision, or Trail Step, THE Audit_Log SHALL append an immutable record identifying actor Identity, action type, target Identity, target Revision Identity when applicable, recorded time in UTC with millisecond precision, and operation correlation identifier, before the success response returns to the caller.
2. WHEN the Authorization_Service denies any consequential action, THE Audit_Log SHALL append an immutable Denial Record identifying actor Identity, attempted action, target Identity, target Revision Identity when applicable, recorded time, denial reason category drawn from the enumerated set in Requirement 7.2, and correlation identifier, before the denial response returns to the caller.
3. THE Audit_Log SHALL be append-only and SHALL reject all update and delete operations on previously appended records, per `REQ-SF-003`.
4. THE Audit_Log SHALL preserve insertion order of appended records using recorded time as primary order and append sequence as tiebreaker.
5. IF an actor attempts to modify or delete a previously appended Audit_Log record, THEN THE Audit_Log SHALL reject the operation and SHALL append an immutable Denial Record covering the rejected attempt.
6. IF an audit append for any consequential creation or denial fails, THEN THE Walking_Slice_System SHALL roll back the originating action, decline to expose any artifact of that action, and return an error indication identifying the audit append failure.

### Requirement 14: Explainable Projection of Slice Status

**User Story:** As a Pilot Reviewer, I want any projected status surfaced by the slice (for example, "Trail unresolved", "Provenance incomplete") to be explainable from its source Records, so that derived views cannot be mistaken for authoritative facts.

**Traceability:**
- Stories: US-034
- Requirements: REQ-SF-009, REQ-SF-010
- Acceptance: AS-009.1

#### Acceptance Criteria

1. WHEN the Walking_Slice_System exposes a projected status over slice Resources, THE Walking_Slice_System SHALL include alongside the projected status in the same response the Projection Definition, source Resource Identities, source Revision Identities, applicable temporal boundary, and generated time, with the temporal boundary and generated time expressed in ISO-8601 form with at least second precision, per `REQ-SF-009`.
2. THE Walking_Slice_System SHALL include on every exposed projected status a derivation indicator distinguishing it from authoritative source Records, per `REQ-SF-009` and Principle 5.23.
3. WHEN a corrected or late-arriving source fact changes a projected status, THE Walking_Slice_System SHALL retain every prior source Record, Revision, and correction record byte-equivalent to its recorded state and SHALL append new facts as additional Revisions or Records rather than overwriting existing ones, per `REQ-SF-010`.
4. IF the Projection Definition or any required source Revision cannot be resolved, THEN THE Walking_Slice_System SHALL withhold the projected status, return an explanation-unavailable indicator identifying the missing element, and leave stored source Records unchanged.

### Requirement 15: Correctness Properties for Property-Based Testing

**User Story:** As a Verification Engineer, I want the slice to be verified by property-based tests that exercise the slice's invariants over generated inputs, so that the named demonstrations are tested at the level of properties, not only worked examples.

**Traceability:**
- Stories: US-007, US-036, US-039, US-042, US-044
- Requirements: REQ-KP-005, REQ-KP-009, REQ-KP-010, REQ-HT-001, REQ-HT-002, REQ-HT-006, REQ-HT-007, REQ-HT-012, REQ-HT-013
- Acceptance: AS-001.4, AS-002.3, AS-002.4, AS-010.1, AS-010.3, AS-011.1, AS-011.2, AS-012.2

Each acceptance criterion below states a property the implementation SHALL preserve under property-based testing.

#### Acceptance Criteria

1. **Evidence support (invariant).** FOR ALL Findings recorded by the Knowledge_Service, the Walking_Slice_System SHALL satisfy: every non-hypothesis Finding has at least one `Supports` Relationship to an exact Content Region Occurrence, where "exact Content Region Occurrence" is one whose start anchor, end anchor, bounded length, and content digest match a Region Occurrence resolvable from the Evidence_Repository at query time and whose target Document Revision Identity resolves in the Evidence_Repository.
2. **Decision authority (invariant).** FOR ALL Decision Immutable Records, the Walking_Slice_System SHALL satisfy: the deciding Party held an effective Decision Maker role assignment at the Decision's recorded time, where "effective" means the role assignment's granted authorities include `approve`, its scope covers the target Recommendation, and its effective period encloses the Decision's recorded time. No Decision Record exists without a matching authority record.
3. **Backlink bidirectionality (round-trip).** FOR ALL Relationships `R` recorded between in-scope endpoints, and FOR ALL requesting Parties `P` who hold view authority on both `R` and its source endpoint, the Walking_Slice_System SHALL satisfy: the Provenance_Navigator returns `R` from the target's backlink query if and only if `R` is returned from the source's outbound query, and the Relationship attribute values returned from both directions are identical, where "in-scope endpoints" are Source Document Revisions, Content Region Occurrences, Finding Revisions, Recommendation Revisions, Decision Immutable Records, and Trail Step Identities.
4. **Backlink non-leakage (metamorphic).** FOR ALL Parties `P` and `P′` differing only in that `P′` lacks view authority on some Relationship `R` or its source endpoint, the Walking_Slice_System SHALL satisfy: the backlink results returned to `P′` are indistinguishable from results produced when `R` does not exist across the observable channels result count, identifier set, ordering positions, pagination cursors, response size, error category, error wording, and latency (within 100 milliseconds variation).
5. **Trail linearity (invariant).** FOR ALL Trail Revisions created within this slice, the Walking_Slice_System SHALL satisfy: the ordered Trail Steps form one totally-ordered sequence of exactly five Trail Steps with exactly one step at each of the five pipeline stages, no Trail Step carries an alternative or branch attribute, and the ordinals are the contiguous integers 1 through 5.
6. **Trail target resolvability (invariant).** FOR ALL Trail Revisions, the Walking_Slice_System SHALL satisfy: every Trail Step target reference resolves to an immutable target Revision or Immutable Record at Trail Revision creation time, OR the Trail Revision records an explicit Omission Entry covering that step that names the affected stage, the omission category, and a non-empty rationale.
7. **Provenance non-omission (invariant).** FOR ALL Findings, Recommendations, Decisions, and Trail Revisions, the Walking_Slice_System SHALL satisfy: every material source actually consulted (drawn from the artifact types Source Document Revision, Content Region Occurrence, Finding Revision, Recommendation Revision, Decision Immutable Record, Measurement Record where applicable, and external Evidence Resource) is either listed in the provenance manifest as an included source, or is listed as an Omission Entry recording category and a non-empty rationale. No material contributing source is silently absent.
8. **Provenance traversal idempotence.** FOR ALL Decision Records `D`, requesting Parties `P`, and effective-time inputs `t`, the Walking_Slice_System SHALL satisfy, across at least 5 repetitions per generated case: repeated invocations of provenance traversal `navigate(D, P, t)` return equal results, where "equal results" means identical node identities, identical node attribute values, and identical ordering, provided that the underlying Records and authority assignments are unchanged.
9. **Navigation-to-exact-Evidence (invariant).** FOR ALL Decision Records `D` whose provenance chain is fully visible to a requesting Party, the Walking_Slice_System SHALL satisfy: traversal from `D` yields at least one Content Region Occurrence for which the returned span fields (start anchor, end anchor, bounded text, content digest, Document Revision Identity) are present and the returned content digest equals the digest recorded on that Region Occurrence.
10. **Identity opacity and uniqueness.** FOR ALL identifiers issued by the Identity_Service within a single test session (a single process invocation with at least 100 generated cases per property), the Walking_Slice_System SHALL satisfy: identifiers are unique within the session, are in canonical UUIDv7 lowercase hyphenated form, and do not embed business metadata, per `ADR-HT-001` §1 and §2.
11. **Audit completeness for denied Decisions.** FOR ALL denied Decision attempts produced by Authorization_Service rejection, the Walking_Slice_System SHALL satisfy: a corresponding Denial Record exists in the Audit_Log identifying actor, target, recorded time, and denial reason drawn from the enumerated set in Requirement 7.2; and no Decision Record exists for that attempt.
12. **Append-only Audit_Log.** FOR ALL sequences of operations applied to the Audit_Log, the Walking_Slice_System SHALL satisfy: at any two observation points in the test, the byte content of every previously appended record is identical; no operation rewrites, reorders, or deletes an earlier record.
13. **Repeatable property runs (operational).** THE property-based test suite SHALL execute at least 100 generated cases per property, record the seed of every test invocation, and on re-execution with the same seed produce identical pass/fail outcomes and identical minimal counterexamples for failing properties.

Properties 1–13 are the verification targets for the property-based test suite associated with this slice. Worked examples in upstream acceptance specifications (AS-001 through AS-002, AS-010 through AS-012) remain valid as scenario-level checks alongside these properties.

### Requirement 16: Prerequisite Architecture Decisions

**User Story:** As a Project Owner, I want the slice's implementation to depend only on architecture decisions whose status is `Accepted`, so that downstream work does not rest on unresolved foundational choices.

**Traceability:**
- ADR: ADR-HT-001 (Accepted) — durable identity strategy.
- Backlog ADRs identified as required follow-ups but not blockers for this slice: ADR-HT-002 (canonical serialization), ADR-HT-003 (Content Region anchoring), ADR-HT-004 (Relationship persistence and lifecycle), ADR-HT-005 (backlink and reverse-dependency indexing).

#### Acceptance Criteria

1. THE Walking_Slice_System SHALL, for each managed identity type listed in `ADR-HT-001` §1, be name-traceable to the ADR-HT-001 section defining that identity type's generation, uniqueness, and opacity rules, and every identifier issued by the slice SHALL satisfy the canonical-form, uniqueness, and opacity constraints in `ADR-HT-001` §1, §2, §3, §6, §7, §8, §9, §10, and §15.
2. WHERE a behavior in this slice requires a choice that ADR-HT-002, ADR-HT-003, ADR-HT-004, or ADR-HT-005 will eventually resolve, THE Walking_Slice_System SHALL implement the behavior required by the specific acceptance criteria in this document that motivated the dependency.
3. WHERE the slice implements an interim behavior in advance of a backlog ADR being `Accepted`, THE project SHALL record, for each such interim behavior, the motivating Requirement number, the motivating criterion number, the observable behavior chosen, the recorded date of the choice, and the backlog ADR identifier, and SHALL make the record retrievable by backlog ADR identifier.
4. IF a backlog ADR transitions to `Accepted` status with a decision whose observable behavior is not consistent with that ADR's accepted decisions, THEN the slice implementation SHALL be revised so that every affected acceptance criterion is satisfied before the verification status of the affected criteria advances beyond `Specified`.

## Out-of-Scope Boundaries

The following are intentionally deferred from this slice and SHALL NOT be required to be implemented to satisfy the requirements above. They appear here to make scope discipline explicit and to align with [`documents/06-thin-vertical-slices.md`](../../../documents/06-thin-vertical-slices.md) §11 and §52 and with [`documents/00.05-constitution-amendment-context-and-delivery.md`](../../../documents/00.05-constitution-amendment-context-and-delivery.md) §4.

- Slices 2–7 of the delivery sequence: Decision → Planned Work, Planned Work → Deliverable, Deliverable → Outcome Review, Learning → Adaptation, Reproducible Publication, Minimal Investment Traceability.
- Approval-Controlled, Live, and Historical-As-Of source adoption modes for content references (only Pinned selection is in scope for the slice's Trail Steps).
- Multiple Trails per pipeline; alternative or branched Trails; Trail Adoption authority and Trail Review workflow.
- Automated thematic clustering, statistical analysis, confidence scoring beyond a free-text confidence note, and bulk interview ingestion.
- Outcome measurement, Measurement Definitions, Observed Outcomes, Outcome Reviews, and Learning Records.
- Publication assembly, Rendered Outputs, and Published Versions.
- Investment, cost, capacity, and portfolio reporting.
- Governed historical withdrawal, redaction, anonymization, retention expiry, and cryptographic erasure (`REQ-HT-015`, `REQ-HT-016`).
- Portability export and independent reconstruction (`REQ-HT-017`, `REQ-HT-018`).
- Automated Agent contribution provenance (`REQ-HT-019`, `REQ-HT-020`) beyond recording that an authoring Party is human.
- Attention governance policies (`REQ-HT-022`).
- Real-time collaborative editing; concurrency reconciliation beyond rejection of mid-Revision mutation (Requirement 2.4).

## Traceability Summary

This slice realizes a strict subset of upstream artifacts. The table below summarizes the principal sources of authority for each requirement; the **Traceability** blocks within each requirement above are authoritative for individual mappings.

| Req | Primary Stories | Primary EARS | Primary Acceptance | ADR |
|---|---|---|---|---|
| 1 | US-001, US-002, US-031 | REQ-SF-001, REQ-SF-002, REQ-SF-004 | AS-001.1, AS-001.2 | ADR-HT-001 |
| 2 | US-001 | REQ-SF-002, REQ-KP-001, REQ-KP-002 | AS-001.1 | — |
| 3 | US-002 | REQ-KP-003, REQ-KP-004 | AS-001.2, AS-001.3 | ADR-HT-001 §7 |
| 4 | US-003 | REQ-KP-005, REQ-KP-006 | AS-001.4, AS-001.5 | — |
| 5 | US-005 | REQ-KP-007 | AS-002.1 | — |
| 6 | US-006 | REQ-KP-008, REQ-SF-003 | AS-002.2 | — |
| 7 | US-006, US-032 | REQ-KP-009, REQ-IG-002, REQ-IG-005 | AS-002.3, AS-007.2, AS-007.3 | — |
| 8 | US-036, US-007 | REQ-HT-001, REQ-HT-002 | AS-010.1, AS-010.2, AS-010.3 | ADR-HT-001 §6 |
| 9 | US-039 | REQ-HT-006, REQ-HT-007 | AS-011.1, AS-011.2 | — |
| 10 | US-042, US-044 | REQ-HT-012, REQ-HT-013, REQ-HT-014 | AS-012.2, AS-012.3 | — |
| 11 | US-007 | REQ-KP-010, REQ-SF-005, REQ-OM-014 | AS-002.4 | — |
| 12 | US-031 | REQ-IG-001, REQ-IG-002, REQ-IG-003 | AS-007.1, AS-007.2 | — |
| 13 | US-006, US-032, US-033 | REQ-SF-003, REQ-IG-005, REQ-IG-006 | AS-002.3, AS-007.4 | — |
| 14 | US-034 | REQ-SF-009, REQ-SF-010 | AS-009.1 | — |
| 15 | (verification target across slice) | (properties cite cross-slice REQs) | (properties cite cross-slice AS) | ADR-HT-001 |
| 16 | (project-level) | (cross-slice) | (cross-slice) | ADR-HT-001 |

## Gaps Flagged for Resolution

The following gaps were identified while reconciling this slice with the upstream documents. They are recorded here so they can be addressed in the design phase rather than rediscovered during implementation.

1. **G-1 — Region anchoring schema is not yet decided.** Requirement 3 records start and end "anchors" and a content digest, but the durable form of those anchors (offset-based, structural, hybrid) is the subject of ADR-HT-003, which remains in the backlog. The slice's design SHALL choose an interim anchoring representation and document it as input to ADR-HT-003.
2. **G-2 — Relationship lifecycle for `Contradicts`, `Supports`, `Derived From`, and `Addresses` is undecided.** ADR-HT-004 is in the backlog. The slice's design SHALL specify whether these Relationships are immutable assertions, effective-dated, or governed-continuing, and document the choice as input to ADR-HT-004.
3. **G-3 — Backlink indexing approach is undecided.** ADR-HT-005 is in the backlog. The slice's design SHALL choose an interim indexing approach (for example, on-demand reverse scan, dedicated index table, or graph adjacency cache) and document the choice as input to ADR-HT-005.
4. **G-4 — Completeness Disclosure policy detail.** Requirement 10.5 and 11.3 depend on a configured Completeness Disclosure policy. No upstream document defines the slice's default disclosure policy at the level needed for implementation. The slice's design SHALL define a minimum default policy (with a placeholder if pilot-specific policy is not yet available) and reference `REQ-HT-014` and ADR-HT-008.
5. **G-5 — Authority basis records for Decision Maker role.** Requirement 6 and Requirement 12 require an "authority basis" field on Decision Records and role assignments. Upstream documents establish the concept (`REQ-IG-001`, `REQ-KP-008`) but do not enumerate the permitted authority-basis values. The slice's design SHALL enumerate an initial set sufficient for the pilot (for example, role-grant identity, scope identity, delegation chain) and flag any gap.
6. **G-6 — Recommendation outcome statuses beyond Accept/Reject/Defer.** Slice scope uses three outcomes (Requirement 6.2). Upstream `REQ-KP-008` permits "supersede" as a fourth outcome. The slice excludes supersession by scope; if pilot feedback requires supersession in this slice, Requirement 6 SHALL be revised.

## References

- Constitutional authority: [`00-project-constitution.md`](../../../documents/00-project-constitution.md), [`00.05-constitution-amendment-context-and-delivery.md`](../../../documents/00.05-constitution-amendment-context-and-delivery.md), [`00.06-hypertext-knowledge-integrity-amendment.md`](../../../documents/00.06-hypertext-knowledge-integrity-amendment.md), [`00.07-constitutional-amendment-index.md`](../../../documents/00.07-constitutional-amendment-index.md).
- Language and foundational model: [`01-domain-glossary.md`](../../../documents/01-domain-glossary.md), [`01.10-hypertext-canonical-terms.md`](../../../documents/01.10-hypertext-canonical-terms.md), [`02-domain-model.md`](../../../documents/02-domain-model.md), [`02.10-hypertext-domain-model-integration.md`](../../../documents/02.10-hypertext-domain-model-integration.md).
- Bounded contexts and invariants: [`03-context-map.md`](../../../documents/03-context-map.md), [`03.01-shared-foundation-domain-model.md`](../../../documents/03.01-shared-foundation-domain-model.md), [`03.08-trail-domain-model.md`](../../../documents/03.08-trail-domain-model.md), [`04-cross-context-invariants.md`](../../../documents/04-cross-context-invariants.md).
- Delivery model and user intent: [`05-user-roles.md`](../../../documents/05-user-roles.md), [`05.01-hypertext-role-extension.md`](../../../documents/05.01-hypertext-role-extension.md), [`06-thin-vertical-slices.md`](../../../documents/06-thin-vertical-slices.md), [`07-user-story-map.md`](../../../documents/07-user-story-map.md), [`08-user-stories.md`](../../../documents/08-user-stories.md), [`08.01-hypertext-user-stories.md`](../../../documents/08.01-hypertext-user-stories.md).
- Formal specification and validation: [`09-requirements-ears.md`](../../../documents/09-requirements-ears.md), [`09.10-hypertext-requirements-ears.md`](../../../documents/09.10-hypertext-requirements-ears.md), [`10-domain-scenarios.md`](../../../documents/10-domain-scenarios.md), [`10.10-hypertext-architecture-decision-backlog.md`](../../../documents/10.10-hypertext-architecture-decision-backlog.md), [`11-acceptance-specifications.md`](../../../documents/11-acceptance-specifications.md), [`11.10-hypertext-acceptance-specifications.md`](../../../documents/11.10-hypertext-acceptance-specifications.md), [`12-constitutional-traceability-ledger.md`](../../../documents/12-constitutional-traceability-ledger.md), [`13.01-adr-ht-001-durable-identity-strategy.md`](../../../documents/13.01-adr-ht-001-durable-identity-strategy.md).
