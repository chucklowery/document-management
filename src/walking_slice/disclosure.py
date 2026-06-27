"""Default Completeness Disclosure policy seeding and lookup.

Design reference: ``.kiro/specs/first-walking-slice/design.md`` §"AD-WS-9 —
Default Completeness Disclosure policy (closes Gap G-4)" and §"Table-by-Table
Specification" — ``Disclosure_Policies``.

Task scope (task 13.2):

This module is responsible for two things:

1. :func:`seed` — idempotently insert the single default Completeness
   Disclosure policy row (``slice-default-2026``) into the
   ``Disclosure_Policies`` table during application startup. The function is
   called from ``app.py``'s startup hook (task 15.2) alongside
   :func:`walking_slice.persistence.create_schema` and
   ``interim_adr.seed`` (task 13.1).
2. :func:`get_policy` — read a persisted policy row back as a typed
   :class:`DisclosurePolicy` value object. The ``Provenance_Navigator``
   (task 12.4) calls this lookup once per request to obtain the active
   policy and then applies the rules in :attr:`DisclosurePolicy.ruleset` to
   each provenance traversal and backlink response.

The seeded policy encodes AD-WS-9 verbatim:

- **Restricted node treatment.** Any node in a provenance chain or backlink
  set that is ``restricted`` for the requesting Party is replaced with a
  redaction marker of shape ``{"kind": "<node_kind>", "redacted": true}``.
  The marker carries no identifier, attribute, or count, so a restricted
  node is observationally indistinguishable from a node that does not exist.
- **Gap descriptor categories.** Nodes that are ``unavailable``, ``stale``,
  or ``unresolved`` are returned as gap descriptors carrying only ``stage``,
  ``category``, and (when the next reachable node is visible to the
  requesting Party) the next reachable node's identity.
- **Restricted-vs-nonexistent normalization.** Counts, identifier sets,
  ordering, pagination cursors, response sizes, error wording, and latency
  (within a 100 ms tolerance) produced for restricted nodes match those
  produced for nonexistent nodes. This is the contract Property 4 ("Non-
  leakage of restricted information", task 12.7) verifies.

The policy is stored as a row in ``Disclosure_Policies`` (rather than
hard-coded in code) so it can be replaced by a future row when ADR-HT-008 is
accepted — the navigator will pick up the new ``ruleset_json`` without
redeploying the slice.

Requirements satisfied (per task 13.2):

- 10.5 — restricted information is normalized so its presence cannot be
         inferred from counts, identifiers, ordering, cursors, response
         sizes, error wording, or latency.
- 11.3 — provenance navigation applies the default Completeness Disclosure
         policy when shaping responses to authorized callers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine


__all__ = [
    "DisclosurePolicy",
    "DisclosurePolicyNotFoundError",
    "SLICE_DEFAULT_POLICY_ID",
    "SLICE_DEFAULT_POLICY_NAME",
    "SLICE_DEFAULT_EFFECTIVE_START",
    "SLICE_DEFAULT_RULESET",
    "get_policy",
    "policy_for",
    "seed",
]


# ---------------------------------------------------------------------------
# Public constants.
#
# The slice ships exactly one default policy. Its identifier and name are
# both ``slice-default-2026``: ``policy_id`` is the PRIMARY KEY in
# ``Disclosure_Policies`` and ``policy_name`` is the UNIQUE human-readable
# label. Using the same string for both lets callers look up the active
# policy by its well-known name without first having to translate a name
# to an opaque identifier.
# ---------------------------------------------------------------------------


SLICE_DEFAULT_POLICY_ID: Final[str] = "slice-default-2026"
"""Primary key of the seeded policy row."""

SLICE_DEFAULT_POLICY_NAME: Final[str] = "slice-default-2026"
"""Human-readable name of the seeded policy row.

Matches the identifier in design §"AD-WS-9". ``Provenance_Navigator``
(task 12.4) uses this name to retrieve the active policy.
"""

SLICE_DEFAULT_EFFECTIVE_START: Final[str] = "2026-01-01T00:00:00.000Z"
"""``effective_start`` for the seeded policy, in ISO-8601 UTC ms precision.

A fixed constant (rather than ``Clock.now()`` at seed time) so that every
slice instance — production, CI, and developer machines — agrees on the
policy's effective start. When ADR-HT-008 lands and a new policy row is
inserted, the new row carries its own ``effective_start`` and points the
old row's ``superseded_by`` at itself.
"""


# ---------------------------------------------------------------------------
# Ruleset.
#
# The ruleset is the structured form of AD-WS-9. It is stored as
# canonical-form JSON in ``Disclosure_Policies.ruleset_json`` and round-
# trips back through :func:`get_policy`. Field names are stable: tests and
# the Provenance_Navigator address them by name.
# ---------------------------------------------------------------------------


SLICE_DEFAULT_RULESET: Final[Mapping[str, Any]] = {
    "policy_name": SLICE_DEFAULT_POLICY_NAME,
    "version": "2026.01",
    "restricted_node_treatment": {
        # AD-WS-9 rule 1. Replace any restricted node with a marker that
        # leaks nothing about the underlying node.
        "action": "replace_with_marker",
        "marker_shape": {
            "kind": "<node_kind>",
            "redacted": True,
        },
        # The marker must NOT carry these fields; tests assert that
        # Provenance_Navigator responses never include them for a
        # restricted node.
        "marker_excludes": ["identifier", "attributes", "count"],
    },
    "gap_descriptor": {
        # AD-WS-9 rule 2. Categories enumerated here are the categories
        # that produce a gap descriptor; ``restricted`` is intentionally
        # NOT in this list because restricted nodes are replaced with a
        # redaction marker, not surfaced as a gap. ``intentional`` is also
        # not in this list — intentional omissions are documented in the
        # Provenance Manifest, not surfaced to navigation callers.
        "categories": ["unavailable", "stale", "unresolved"],
        "fields": [
            "stage",
            "category",
            "next_reachable_node_identity_if_visible",
        ],
    },
    "restricted_vs_nonexistent_normalization": {
        # AD-WS-9 rule 3 / Property 4. Every observable surface in this
        # list must be byte-equivalent between (a) a request from a Party
        # without view authority on a restricted node and (b) the same
        # request in a universe where the restricted node does not exist.
        "indistinguishable_dimensions": [
            "count",
            "identifier_set",
            "ordering",
            "cursor",
            "response_size",
            "error_wording",
        ],
        # Latency is held to a tolerance rather than equality because the
        # underlying database and process scheduler introduce noise we do
        # not control. Property 4 (task 12.7) enforces this tolerance.
        "latency_tolerance_ms": 100,
    },
}


# ---------------------------------------------------------------------------
# Value object and error.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisclosurePolicy:
    """In-memory projection of a ``Disclosure_Policies`` row.

    Attributes:
        policy_id: Primary key of the row.
        policy_name: Human-readable name (``UNIQUE`` in the table).
        ruleset: Decoded JSON ruleset. The shape of the active default is
            documented at :data:`SLICE_DEFAULT_RULESET`.
        effective_start: ISO-8601 UTC timestamp (ms precision) at which
            the policy began applying.
        superseded_by: ``policy_id`` of the row that replaces this one,
            or ``None`` if this policy is still active.
    """

    policy_id: str
    policy_name: str
    ruleset: Mapping[str, Any]
    effective_start: str
    superseded_by: Optional[str]


class DisclosurePolicyNotFoundError(LookupError):
    """Raised when :func:`get_policy` cannot resolve a ``policy_id``.

    The error message names the missing identifier so callers (and tests)
    can include it in their own diagnostics. ``Provenance_Navigator``
    treats this as an internal error rather than a denial: if the seeded
    default policy is missing, the slice is mis-bootstrapped and the
    operator-visible log surfaces ``disclosure_policy_unavailable``.
    """


# ---------------------------------------------------------------------------
# Seed and lookup.
# ---------------------------------------------------------------------------


def seed(engine: Engine) -> None:
    """Insert the ``slice-default-2026`` row into ``Disclosure_Policies``.

    Idempotent: ``INSERT OR IGNORE`` keyed on the ``policy_id`` PRIMARY KEY
    ensures repeated invocations (developer hot-reloads, restart loops,
    test fixtures that share an engine) do not produce a UNIQUE-constraint
    violation and do not overwrite a row that has been edited by an
    operator. The write runs inside a single transaction so a partial
    insert cannot leave the table inconsistent.

    Args:
        engine: A SQLAlchemy Core engine bound to a SQLite database whose
            schema has already been created by
            :func:`walking_slice.persistence.create_schema`.
    """
    ruleset_json = json.dumps(SLICE_DEFAULT_RULESET, sort_keys=True, ensure_ascii=True)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO Disclosure_Policies (
                    policy_id, policy_name, ruleset_json,
                    effective_start, superseded_by
                ) VALUES (
                    :policy_id, :policy_name, :ruleset_json,
                    :effective_start, NULL
                )
                """
            ),
            {
                "policy_id": SLICE_DEFAULT_POLICY_ID,
                "policy_name": SLICE_DEFAULT_POLICY_NAME,
                "ruleset_json": ruleset_json,
                "effective_start": SLICE_DEFAULT_EFFECTIVE_START,
            },
        )


def get_policy(engine: Engine, policy_id: str) -> DisclosurePolicy:
    """Load a policy row from ``Disclosure_Policies`` by ``policy_id``.

    Used by ``Provenance_Navigator`` (task 12.4) to fetch the active
    default policy — ``policy_id = "slice-default-2026"`` — and apply its
    ruleset to backlink and provenance responses. The lookup runs in its
    own read-only connection; callers needing the policy inside an
    existing transaction can read the same row directly because policies
    are insert-and-supersede, never updated in place.

    Args:
        engine: A SQLAlchemy Core engine bound to the slice's SQLite
            database.
        policy_id: Primary key of the policy row to load. For the slice's
            default policy this is :data:`SLICE_DEFAULT_POLICY_ID`.

    Returns:
        The decoded :class:`DisclosurePolicy`.

    Raises:
        DisclosurePolicyNotFoundError: If no row exists with the given
            ``policy_id``.
    """
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT policy_id, policy_name, ruleset_json,
                       effective_start, superseded_by
                  FROM Disclosure_Policies
                 WHERE policy_id = :policy_id
                """
            ),
            {"policy_id": policy_id},
        ).first()

    if row is None:
        raise DisclosurePolicyNotFoundError(
            f"No Disclosure_Policies row found for policy_id={policy_id!r}."
        )

    return DisclosurePolicy(
        policy_id=row.policy_id,
        policy_name=row.policy_name,
        ruleset=json.loads(row.ruleset_json),
        effective_start=row.effective_start,
        superseded_by=row.superseded_by,
    )


def policy_for(engine: Engine, node_kind: str) -> DisclosurePolicy:
    """Return the active Disclosure policy that covers ``node_kind``.

    The lookup consults two tables in priority order, matching design
    §"AD-WS-23 — Disclosure policy coverage is enforced by lookup, not by
    per-node code paths":

    1. ``Disclosure_Policy_Coverage`` — the additive Slice 2 surface
       seeded by
       :func:`walking_slice.planning._disclosure.seed_planning_coverage`.
       Every Slice 2 node kind (Objective Resource, Objective Revision,
       Intended Outcome Resource, Intended Outcome Revision, Project
       Resource, Project Revision, Deliverable Expectation Resource,
       Deliverable Expectation Revision, Activity Plan Resource, Plan
       Revision, Plan Review Resource, Plan Review Revision, Plan
       Approval Immutable Record) has a row here pointing at the
       ``slice-default-2026`` policy (AD-WS-16). When a future ADR
       supersedes the default by inserting a new policy row, the
       coverage rows can be migrated to point at the new policy without
       touching this function.
    2. ``Disclosure_Policies`` — the baseline default
       ``slice-default-2026`` policy. Slice 1 node kinds (Document
       Revision, Region Occurrence, Finding Revision, Recommendation
       Revision, Decision Immutable Record, Trail Revision, Trail Step,
       Relationship, Provenance Manifest, Omission Entry) do not have
       coverage rows because the policy was authored before they had
       any need to be enumerated; the baseline default applies to them
       via fallback. The Provenance_Navigator (task 12.4) and the
       Planning_Service consume the returned policy uniformly, so the
       single returned :class:`DisclosurePolicy` carries the full AD-WS-9
       rule set for both slices.

    The function does not return a *set* of applicable policies — it
    returns the one policy whose rules apply to ``node_kind`` at the
    point of the call. The seeded slice ships exactly one default
    policy, so the two lookups converge on the same row in every code
    path the slice exercises; the dual lookup is the explicit additive
    surface AD-WS-23 mandates so a future ADR can replace the default
    by seeding a new policy and pointing the coverage rows at it
    without modifying any call site.

    Args:
        engine: A SQLAlchemy Core engine bound to the slice's SQLite
            database. The schema (Slice 1 + Slice 2) and the
            ``slice-default-2026`` policy row MUST already be in place
            — the slice's startup hook guarantees this and tests
            should run ``create_schema`` /
            ``create_planning_schema`` / ``seed`` /
            ``seed_planning_coverage`` before invoking this function.
        node_kind: The node-kind discriminator the navigator or
            Authorization_Service is shaping a response for. Values
            are unconstrained at the SQL layer — the column is
            ``TEXT`` — but every value consulted by the slice is drawn
            from the Slice 1 / Slice 2 enumerations.

    Returns:
        The :class:`DisclosurePolicy` whose ruleset applies to
        ``node_kind``. Callers apply the ruleset in-process; the
        function performs no shaping itself.

    Raises:
        DisclosurePolicyNotFoundError: When neither a coverage row nor
            the baseline default policy can be resolved. This indicates
            a mis-bootstrapped slice (the startup hook did not run
            :func:`seed`); the navigator treats this as an internal
            error (``disclosure_policy_unavailable``) rather than a
            denial because no observability contract can be honored
            without a policy.
    """
    with engine.connect() as conn:
        # ---- Step 1: Additive Slice 2 coverage lookup. -------------------
        #
        # ``Disclosure_Policy_Coverage`` is the sibling table seeded by
        # ``planning._disclosure.seed_planning_coverage``. A row keyed on
        # ``node_kind`` identifies the policy that explicitly covers this
        # kind. The join against ``Disclosure_Policies`` materializes the
        # full policy row so we can return a :class:`DisclosurePolicy`
        # without a second round-trip.
        #
        # The ``LEFT JOIN`` shape lets the query degrade gracefully if
        # the schema predates Slice 2: the ``Disclosure_Policy_Coverage``
        # table is created by ``create_planning_schema`` and absent on a
        # pure Slice 1 database, so the try/except below falls back to
        # the baseline-policy lookup when the table is missing.
        coverage_row = None
        try:
            coverage_row = conn.execute(
                text(
                    """
                    SELECT p.policy_id, p.policy_name, p.ruleset_json,
                           p.effective_start, p.superseded_by
                      FROM Disclosure_Policy_Coverage AS c
                      JOIN Disclosure_Policies       AS p
                        ON p.policy_id = c.policy_id
                     WHERE c.node_kind = :node_kind
                     LIMIT 1
                    """
                ),
                {"node_kind": node_kind},
            ).first()
        except Exception:
            # ``Disclosure_Policy_Coverage`` not present (Slice 1-only
            # database) — fall through to the baseline lookup below.
            coverage_row = None

        # ---- Step 2: Baseline default policy fallback. -------------------
        #
        # Used both (a) when no coverage row exists for ``node_kind`` (the
        # Slice 1 node-kind path) and (b) when the additive coverage table
        # has not yet been created (the schema-bootstrap path used by a
        # handful of tests that run a Slice 1-only fixture). The lookup is
        # keyed on the well-known ``slice-default-2026`` identifier; if
        # that row is missing the slice is mis-bootstrapped and we raise
        # :class:`DisclosurePolicyNotFoundError` so the navigator surfaces
        # ``disclosure_policy_unavailable`` in its operator log.
        if coverage_row is None:
            row = conn.execute(
                text(
                    """
                    SELECT policy_id, policy_name, ruleset_json,
                           effective_start, superseded_by
                      FROM Disclosure_Policies
                     WHERE policy_id = :policy_id
                    """
                ),
                {"policy_id": SLICE_DEFAULT_POLICY_ID},
            ).first()
        else:
            row = coverage_row

    if row is None:
        raise DisclosurePolicyNotFoundError(
            f"No Disclosure policy covers node_kind={node_kind!r}: neither "
            f"Disclosure_Policy_Coverage nor the baseline "
            f"{SLICE_DEFAULT_POLICY_ID!r} policy row resolved. The slice is "
            f"mis-bootstrapped — invoke disclosure.seed() and, for Slice 2 "
            f"node kinds, planning._disclosure.seed_planning_coverage() "
            f"during startup."
        )

    return DisclosurePolicy(
        policy_id=row.policy_id,
        policy_name=row.policy_name,
        ruleset=json.loads(row.ruleset_json),
        effective_start=row.effective_start,
        superseded_by=row.superseded_by,
    )
