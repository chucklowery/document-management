"""Slice 4 Outcome_Service package (fourth walking slice).

This package is the single additive module the fourth walking slice
introduces. It is subordinate to the Outcome Measurement and Learning
bounded context (see ``03-context-map.md`` §2.8) and owns the five
Outcome_Service writes (Measurement Definitions, Measurement Records,
Observed Outcomes, Success-Condition Assessments, and Outcome Reviews),
the additive ``Disclosure_Policy_Coverage`` rows (``_disclosure``), the
outcome-aware provenance traversals (``_provenance``), the outcome-status
Projection (``_projection``), the Slice 4 Interim ADR seeding
(``_interim_adr``), the shared helpers (``_helpers``), and the frozen
value objects (``models``).

Design reference: ``.kiro/specs/fourth-walking-slice/design.md`` §"AD-WS-32
— One new ``walking_slice.outcome`` package".

The package is deliberately additive: it depends on the existing
``walking_slice.identity``, ``walking_slice.audit``,
``walking_slice.authorization``, ``walking_slice.knowledge``,
``walking_slice.provenance``, ``walking_slice.disclosure``,
``walking_slice.interim_adr``, ``walking_slice.persistence``,
``walking_slice.projection``, ``walking_slice.clock``,
``walking_slice.planning``, ``walking_slice.execution``, and
``walking_slice.deliverables`` modules through their public APIs only.

This module intentionally keeps ``__init__`` minimal (no eager
re-exports) so the package's submodules can be imported and wired
independently by the application startup hook.
"""

from __future__ import annotations
