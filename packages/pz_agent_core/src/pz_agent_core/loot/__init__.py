"""Deterministic loot selection for the loot-area goal.

The loot mission walks, opens and inspects; *this* package decides. Given one
container's contents and a :class:`~.policy.LootPolicy`, :func:`~.policy.select`
returns which items to take (shaped for ``inventory.transfer_batch``) and, for
every item left behind, a typed :class:`~.policy.LeaveReason` — so the goal's
final report can say "taken/left and why" instead of shrugging.

Pure on purpose: no I/O, no clocks, and every ordering is total, so a shuffled
container enumeration selects the identical items. The classification table in
:mod:`.policy` is the seed of the P3 machine-readable knowledge base and will
be replaced by it; until then it stays small, honest, and pinned by tests.
"""

from __future__ import annotations

from .policy import (
    CAPACITY_PRECISION,
    DEFAULT_LOOT_POLICY,
    DEFAULT_WANTED,
    MAX_ITEMS_PER_CONTAINER,
    MAX_SELECT_CONTENTS,
    Leave,
    LeaveReason,
    LootCategory,
    LootPolicy,
    Pick,
    Selection,
    SelectionSummary,
    classify,
    select,
    summarise,
)

__all__ = [
    "CAPACITY_PRECISION",
    "DEFAULT_LOOT_POLICY",
    "DEFAULT_WANTED",
    "MAX_ITEMS_PER_CONTAINER",
    "MAX_SELECT_CONTENTS",
    "Leave",
    "LeaveReason",
    "LootCategory",
    "LootPolicy",
    "Pick",
    "Selection",
    "SelectionSummary",
    "classify",
    "select",
    "summarise",
]
