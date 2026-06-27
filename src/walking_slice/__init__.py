"""walking_slice — First walking slice of the Organizational Knowledge and Work System.

This package is the modular monolith specified in
``.kiro/specs/first-walking-slice/design.md``. The module layout mirrors the
bounded contexts named in design §"Module Layout":

- ``clock`` and shared ``models``                              — Shared Graph Foundation.
- ``identity``                                                 — Identity_Service.
- ``audit``                                                    — Audit_Log.
- ``authorization``                                            — Authorization_Service.
- ``evidence``                                                 — Evidence_Repository
                                                                 (Document Authoring and Composition).
- ``knowledge``, ``recommendations``, ``decisions``,
  ``trails``, ``manifests``, ``provenance``, ``projection``    — Knowledge and Provenance.
- ``interim_adr``, ``disclosure``                              — startup seeding.
- ``auth_middleware``, ``app``, ``routes``                     — HTTP edge composition.

Implementation modules are added by subsequent tasks (1.2 onward). Task 1.1
provides only the project skeleton and tooling.
"""

__all__: list[str] = []
