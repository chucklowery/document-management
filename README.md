# Organizational Knowledge and Work System

This repository specifies a system for preserving organizational knowledge, coordinating work, finding changing information, and managing many input and output channels.

The repository describes intended behavior. It does not demonstrate that the system is implemented or verified.

## Constitutional authority

1. [`documents/00-project-constitution.md`](documents/00-project-constitution.md)
2. [`documents/00.05-constitution-amendment-context-and-delivery.md`](documents/00.05-constitution-amendment-context-and-delivery.md)
3. [`documents/00.06-hypertext-knowledge-integrity-amendment.md`](documents/00.06-hypertext-knowledge-integrity-amendment.md)
4. [`documents/00.08-information-retrieval-and-system-management-amendment.md`](documents/00.08-information-retrieval-and-system-management-amendment.md)
5. [`documents/00.09-findability-and-information-architecture-amendment.md`](documents/00.09-findability-and-information-architecture-amendment.md)
6. [`documents/00.10-literate-operations-and-contract-evolution-amendment.md`](documents/00.10-literate-operations-and-contract-evolution-amendment.md)
7. [`documents/00.07-constitutional-amendment-index.md`](documents/00.07-constitutional-amendment-index.md)

Downstream stories, requirements, designs, projections, and implementations do not override constitutional authority.

## Reading paths

### Foundation

- [`documents/01-domain-glossary.md`](documents/01-domain-glossary.md)
- [`documents/02-domain-model.md`](documents/02-domain-model.md)
- [`documents/03-context-map.md`](documents/03-context-map.md)
- [`documents/04-cross-context-invariants.md`](documents/04-cross-context-invariants.md)

### Retrieval and system management

- [`documents/01.11-information-retrieval-and-system-management-canonical-terms.md`](documents/01.11-information-retrieval-and-system-management-canonical-terms.md)
- [`documents/02.11-information-retrieval-and-system-management-domain-model.md`](documents/02.11-information-retrieval-and-system-management-domain-model.md)
- [`documents/08.05-information-retrieval-and-system-management-user-stories.md`](documents/08.05-information-retrieval-and-system-management-user-stories.md)
- [`documents/09.12-information-retrieval-requirements-ears.md`](documents/09.12-information-retrieval-requirements-ears.md)
- [`documents/09.13-change-processing-and-consistency-requirements-ears.md`](documents/09.13-change-processing-and-consistency-requirements-ears.md)
- [`documents/09.14-input-output-and-delivery-requirements-ears.md`](documents/09.14-input-output-and-delivery-requirements-ears.md)
- [`documents/09.15-operations-and-recovery-requirements-ears.md`](documents/09.15-operations-and-recovery-requirements-ears.md)
- [`documents/09.16-derived-store-security-requirements-ears.md`](documents/09.16-derived-store-security-requirements-ears.md)

### Findability

- [`documents/00.09-findability-and-information-architecture-amendment.md`](documents/00.09-findability-and-information-architecture-amendment.md)
- [`documents/12.02-findability-traceability.md`](documents/12.02-findability-traceability.md)

### Literate operations

- [`documents/01.13-literate-operations-canonical-terms.md`](documents/01.13-literate-operations-canonical-terms.md)
- [`documents/02.13-literate-operations-domain-model.md`](documents/02.13-literate-operations-domain-model.md)
- [`documents/03.12-literate-operations-context-extension.md`](documents/03.12-literate-operations-context-extension.md)
- [`documents/05.04-literate-operations-role-extension.md`](documents/05.04-literate-operations-role-extension.md)
- [`documents/08.07-literate-operations-user-stories.md`](documents/08.07-literate-operations-user-stories.md)
- [`documents/09.26-literate-operations-requirements.md`](documents/09.26-literate-operations-requirements.md)
- [`documents/10.13-operations-adrs.md`](documents/10.13-operations-adrs.md)
- [`documents/11.23a-contract-acceptance.md`](documents/11.23a-contract-acceptance.md)
- [`documents/11.23-acceptance.md`](documents/11.23-acceptance.md)
- [`documents/11.23c-narrative.md`](documents/11.23c-narrative.md)
- [`documents/12.03.md`](documents/12.03.md)

## Maturity

Constitutional language, stories, requirements, and acceptance specifications are present. Most architecture decisions remain proposed. Implementation and pilot evidence are not demonstrated.

## Validation

Run the multi-ledger validator:

```bash
python scripts/validate_all_documentation.py
```

Machine-readable coverage is under [`traceability/`](traceability/). The legacy single-ledger validator remains available for compatibility.
