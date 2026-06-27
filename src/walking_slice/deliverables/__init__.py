"""walking_slice.deliverables — Third-walking-slice Deliverable_Repository package.

Implements the additive Deliverable_Repository specified in
``.kiro/specs/third-walking-slice/design.md``. Modules are added by the
sub-tasks in ``.kiro/specs/third-walking-slice/tasks.md``:

- ``_persistence``  — Slice 3 Deliverable_Repository SQLite schema (the
                      ``Deliverable_Resources`` and ``Deliverable_Revisions``
                      tables, their composite indexes, and the AD-WS-27
                      UPDATE/DELETE rejection triggers).
- ``models``        — frozen Pydantic value objects ``DeliverableRef`` and
                      ``DeliverableRevisionRef`` (task 3.2).
- ``repository``    — :class:`DeliverableRepositoryService` exposing
                      ``create_produced_deliverable`` and the read APIs
                      (task 4).

The package is strictly additive with respect to Slice 1 and Slice 2
(Requirement 40): no Slice 1 or Slice 2 module is mutated, and the only
cross-slice touch-points are:

- foreign-key references to ``Parties`` (Slice 1) and
  ``Work_Assignment_Records`` (Slice 3, owned by
  :mod:`walking_slice.execution`);
- registry-kind tagging in ``Identifier_Registry.resource_kind`` via the
  Slice 2 additive column (Requirement 22.8 / Requirement 26.3).
"""

from walking_slice.deliverables._persistence import (
    DELIVERABLE_IMMUTABLE_TABLES,
    DELIVERABLE_SCHEMA_STATEMENTS,
    create_deliverable_schema,
)


__all__: list[str] = [
    "DELIVERABLE_IMMUTABLE_TABLES",
    "DELIVERABLE_SCHEMA_STATEMENTS",
    "create_deliverable_schema",
]
