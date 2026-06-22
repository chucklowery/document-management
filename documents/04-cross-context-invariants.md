# Cross-Context Invariants

**Project:** Organizational Knowledge and Work System

## 1. Purpose

This document defines rules that must remain true across bounded contexts.

These invariants constrain integration and interpretation without forcing all contexts into one universal model.

## 2. Identity and Authority

1. Stable identity survives ordinary renaming, movement, repository restructuring, and changes in external storage location.
2. A bounded context owns the meaning and lifecycle of its authoritative concepts.
3. A consuming context references another context's object through a published contract and stable identity.
4. Import, synchronization, indexing, or projection does not silently transfer authority.
5. Every externally sourced fact identifies its source system and authority designation.
6. Conflicting authoritative claims are surfaced rather than silently merged.

## 3. History and Correction

1. Recorded Resource Revisions are immutable.
2. Immutable Records are append-only.
3. Corrections create correcting or superseding records rather than rewriting history.
4. Historical plans, decisions, measurements, execution facts, publications, and conclusions remain reconstructable.
5. Late-arriving information preserves its observation or effective time when known.
6. Recorded Time, Effective Time, Observation Time, and Generated Time are not assumed to be equal.

## 4. Plans and Execution

1. Plans do not prove that work occurred.
2. Execution facts do not silently rewrite historical plans.
3. Replanning creates a new Revision or superseding planning Resource.
4. Planned effort and actual effort remain distinct.
5. Assignment does not prove participation or completion.
6. Completion records identify the exact planned scope or Revision evaluated.
7. Current execution status is explainable from source records and policy.

## 5. Outputs and Outcomes

1. Work completion does not prove delivery of an output.
2. Delivery of an output does not prove acceptance.
3. Acceptance does not prove use.
4. Use does not prove an intended Outcome occurred.
5. Intended Outcomes and Observed Outcomes remain distinct.
6. Outcome achievement is evaluated against explicit Success Conditions and Evidence.
7. Outcome status Projections do not replace Outcome Reviews or source measurements.

## 6. Evidence and Interpretation

1. Measurements remain distinct from interpretations of those measurements.
2. Findings, Observed Outcomes, and Learning Records identify their supporting Evidence.
3. Competing or contradictory interpretations may coexist.
4. A later interpretation supersedes rather than rewrites an earlier interpretation.
5. Support does not automatically establish truth.
6. Contradiction does not delete either side.
7. Recommendations remain distinct from Decisions.
8. Learning does not automatically revise plans, policies, or decisions; adoption is explicit.

## 7. Causation and Attribution

1. Correlation is not represented as proven causation without supporting evidence and stated reasoning.
2. Contribution claims are distinct from causal claims.
3. Attribution assumptions are explicit, versioned, and reviewable.
4. Competing explanations remain visible.
5. Outcome confidence reflects uncertainty in evidence, measurement, and attribution.

## 8. Relationships and Provenance

1. Managed Relationships identify explicit source, target, type, and applicable version or selection rule.
2. Relationship semantics are defined by a domain contract.
3. Cross-context links use stable identities rather than private object references.
4. Material provenance identifies exact source Revisions where reproducibility matters.
5. Generated and imported information remains distinguishable from authoritative source.
6. Provenance does not imply endorsement.
7. Supersession preserves the superseded object and states its scope.

## 9. Projections

1. A Projection is derived information.
2. A Projection does not silently become authoritative source.
3. A Projection identifies its definition, relevant inputs, temporal boundary, assumptions, and generated time.
4. Historical Projections are reproducible when their definitions and inputs remain available.
5. Different valid Projections may coexist for different policies, scenarios, or audiences.
6. Late-arriving or corrected source facts may change a current Projection without rewriting prior facts.
7. A Projection states whether it is current, historical, forecast, estimated, or scenario-based.

## 10. Documents, Assembly, and Publication

1. Authoritative source remains distinct from assembled, generated, rendered, and published representations.
2. Assembly does not mutate source Resources or Revisions.
3. Successful Assembly resolves every required dependency deterministically.
4. A Publication freezes exact source Revisions, dependencies, configuration, templates, and relevant tool versions.
5. Published content is immutable.
6. Corrections produce a new Published Version.
7. Generated Output is a role established through provenance, not an intrinsic Resource Kind.

## 11. Governance and Access

1. Authorization is explicit for consequential actions.
2. Governance is proportional to sensitivity, authority, exposure, consequence, automation privilege, and reversibility.
3. Exploratory work is not automatically subject to publication-grade controls.
4. Sensitive information does not leak through search results, counts, errors, projections, or graph traversal.
5. Consent, disclosure, redaction, and retention decisions identify their exact scope and authority.
6. Privileged actions are auditable.
7. No acknowledged contribution is silently discarded.

## 12. Integration

1. Every integration identifies whether the external system is authoritative, replicated, referenced, indexed, projected, or federated.
2. Translation is explicit when two contexts use the same term differently.
3. Integration failure and synchronization staleness are visible.
4. Local modification rules follow authority designation.
5. External identifiers and source provenance are preserved.
6. Replaceable integrations are preferred over domain coupling to a specific vendor or platform.

## 13. Organizational Learning Loop

The canonical loop is:

> **Evidence → Interpretation → Decision → Plan → Execution → Measurement → Outcome → New Evidence**

The following rules apply:

1. No transition in the loop is implicit.
2. Each transition is represented by explicit Resources, Relationships, or Immutable Records.
3. New Evidence may challenge assumptions, conclusions, plans, or decisions.
4. Challenged information remains historically visible.
5. Adaptation occurs through explicit Decisions and new Revisions.

## 14. Delivery Invariants

1. New concepts require demonstrated workflow value, explicit domain need, or material risk.
2. Thin end-to-end slices are preferred over broad speculative modeling.
3. Shared-kernel concepts remain minimal.
4. Context-specific lifecycle states are not promoted into the Shared Kernel without proven shared semantics.
5. Architecture decisions remain reversible where practical.
6. The product shall preserve the ability to explain how a displayed conclusion or status was derived.

## 15. Review Checklist

A proposed model or feature should be challenged with these questions:

- Who owns this concept's meaning and lifecycle?
- Is this a continuing Resource, an immutable event, or a Projection?
- What is authoritative?
- What history must remain reconstructable?
- Which exact inputs and versions produced this result?
- Does this confuse a plan, activity, output, or outcome?
- Does it imply causation without evidence?
- Does it introduce a universal state or schema that belongs in one context?
- Can the result be explained to a domain user?
- Has the concept been justified by a real workflow?
