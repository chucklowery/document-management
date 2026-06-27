"""walking_slice.execution — Third-walking-slice Execution_Service package.

Implements the additive Execution_Service specified in
``.kiro/specs/third-walking-slice/design.md`` §"Architecture",
§"Components and Interfaces", and §"Data Models — Schema Additions".

Modules are added by the sub-tasks in
``.kiro/specs/third-walking-slice/tasks.md``:

- ``_persistence``    — Slice 3 SQLite schema for Execution_Service tables
                        (``Work_Assignment_Records``, ``Work_Event_Records``,
                        ``Time_Entry_Records``,
                        ``Deliverable_Production_Records``,
                        ``Milestone_Acceptance_Records``,
                        ``Completion_Records``) with their indexes and
                        append-only triggers per AD-WS-27 / AD-WS-28
                        (task 1.2).
- ``_disclosure``     — additive ``Disclosure_Policy_Coverage`` rows for
                        Slice 3 node kinds per AD-WS-25 (task 1.4).
- ``_interim_adr``    — Slice 3 Interim ADR row seeding (task 1.4 / 1.5).
- ``_helpers``        — shared helpers (identifier registration,
                        prohibited-attribute rejection) consumed by every
                        Execution_Service service module (task 3.3).
- ``models``          — frozen Pydantic value objects (task 3.1).
- ``work_assignments``,
  ``work_events``,
  ``time_entries``,
  ``deliverable_productions``,
  ``milestone_acceptances``,
  ``completions``     — per-Record-kind Execution_Service modules
                        (tasks 5..11).
- ``_routes``         — FastAPI router composition (task 13).
- ``_projection``     — execution-status Projection wrapped in
                        ``ProjectionEnvelope`` (task 13).

The package is strictly additive with respect to Slices 1 and 2
(Requirement 40): no prior-slice module is imported in a way that
requires modification, and the only prior-slice touch-points are the
four additive authority enumeration values (task 1.1) and the additive
``Disclosure_Policy_Coverage`` rows (task 1.4).
"""

from __future__ import annotations


__all__: list[str] = []
