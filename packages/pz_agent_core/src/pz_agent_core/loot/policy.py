"""Deterministic loot selection: which items leave a container, and why not.

The loot-area goal asks one question per inspected container: of these
contents, what goes into the bag? The answer is decided here, by code, and
never by a language model — the model may say "loot the warehouse"; this module
says *which* can of beans, in *which* order, and why every other item stayed on
the shelf. A left-behind item is a first-class value with a typed reason, not a
log line, because the goal's final report has to say "taken/left and why" and a
filter that silently drops items cannot answer that.

Invariant: everything in this module is a pure function of its arguments. No
I/O, no clock, no random source, and every ranking ends in a total order whose
last key is the item reference — so shuffling a container's enumeration order
cannot change what gets taken.

The classification table is deliberately small: it is the *seed* of the P3
machine-readable knowledge base, which will replace it with a versioned,
data-driven catalogue. Until then every rule is written inline with the reason
it exists, and anything the table does not recognise is
:attr:`LootCategory.OTHER` — an honest "I do not know this item", never a guess
dressed as knowledge.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ..protocol import ItemView, JsonDict

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


class LootCategory(StrEnum):
    """The closed vocabulary a looted item is filed under.

    Declaration order is meaningful: it is the take priority
    :func:`select` uses, from "keeps you alive tonight" down to "unknown".
    Reordering members changes which items win a tight capacity budget, so the
    order is pinned by a test.
    """

    FOOD = "FOOD"
    WATER = "WATER"
    MEDICAL = "MEDICAL"
    WEAPONS = "WEAPONS"
    TOOLS = "TOOLS"
    MATERIALS = "MATERIALS"
    LITERATURE = "LITERATURE"
    CLOTHING = "CLOTHING"
    OTHER = "OTHER"


class LeaveReason(StrEnum):
    """Why one item stayed in the container.

    Exactly one reason per left item, chosen by the first matching check in
    the fixed order below (the order the members are declared in). The first
    match is the most fundamental problem — "you told me never to take this"
    beats "the bag is full" — so the reported reason is deterministic and
    reads as the real obstacle rather than an arbitrary one of several.
    """

    #: The item's category is outside the policy's wanted set.
    NOT_WANTED = "NOT_WANTED"
    #: The user reserved this item type. Never overridden, not even by
    #: ``take_all`` — the user's word outranks the flag.
    RESERVED = "RESERVED"
    #: The item's weight alone exceeds the whole reported free capacity, so it
    #: could not be carried even as the only pick of the trip.
    UNCARRIABLE = "UNCARRIABLE"
    #: The selection already holds ``max_items_per_container`` picks.
    BATCH_FULL = "BATCH_FULL"
    #: The item would fit an empty bag, but not what is left after the
    #: earlier (higher-priority, lighter) picks.
    OVER_CAPACITY = "OVER_CAPACITY"


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

#: Game display-category string → loot category. Keys are lower-cased once
#: here; :func:`classify` lower-cases the observed string once before lookup,
#: so the mod's casing ("Food", "FOOD", "food") cannot split one game category
#: into two behaviours. Each entry names a category the game itself is
#: authoritative about, which is why this table is consulted *before* the
#: full-type heuristics: the game's own label beats our pattern-matching.
_BY_GAME_CATEGORY: Final[Mapping[str, LootCategory]] = MappingProxyType(
    {
        # The game's food category covers everything edible or drinkable that
        # it models as Food; drink-specific handling happens downstream.
        "food": LootCategory.FOOD,
        # B42 fluid containers report a Water display category; when the game
        # says an item is water storage, it is.
        "water": LootCategory.WATER,
        "weapon": LootCategory.WEAPONS,
        # The game has used both labels for the medical shelf across builds;
        # mapping both costs nothing and misses neither.
        "firstaid": LootCategory.MEDICAL,
        "medical": LootCategory.MEDICAL,
        "tool": LootCategory.TOOLS,
        "material": LootCategory.MATERIALS,
        "literature": LootCategory.LITERATURE,
        "clothing": LootCategory.CLOTHING,
    }
)

#: Full-type prefix → loot category, tried in order, first match wins. Only
#: for the classics whose display category is unhelpful ("Item") while the
#: type name is unambiguous. Bounded on purpose: every prefix added here is a
#: claim about the game's item catalogue, and the P3 knowledge base is where
#: such claims belong at scale.
_BY_FULL_TYPE_PREFIX: Final[tuple[tuple[str, LootCategory], ...]] = (
    # Base.WaterBottleFull / Base.WaterBottleEmpty: the canonical water
    # carriers, listed under a generic display category in some builds.
    ("Base.WaterBottle", LootCategory.WATER),
    # Base.PopBottle*: soda bottles double as refillable water storage, which
    # is what the loot goal wants them for.
    ("Base.PopBottle", LootCategory.WATER),
)


def classify(item: ItemView) -> LootCategory:
    """File one observed item under a :class:`LootCategory`.

    Deterministic and total, from the observed fields only: the game's
    ``category`` string first (the game is authoritative about its own
    labels), then the bounded full-type prefix heuristics, and
    :attr:`LootCategory.OTHER` for everything else. OTHER is an honest
    "unrecognised", never a guess — the P3 machine-readable knowledge base
    epic will replace this seed table with a data-driven catalogue.
    """
    mapped = _BY_GAME_CATEGORY.get(item.category.strip().lower())
    if mapped is not None:
        return mapped
    for prefix, category in _BY_FULL_TYPE_PREFIX:
        if item.full_type.startswith(prefix):
            return category
    return LootCategory.OTHER


# --------------------------------------------------------------------------
# policy
# --------------------------------------------------------------------------

#: The ``useful_only`` default: what a survivor grabs when the user did not
#: say. Everything that feeds, waters, heals, arms, fixes or teaches — and
#: deliberately *not* CLOTHING (bulky, situational, the user asks for it
#: explicitly) or OTHER (taking what we cannot name is how a bag fills with
#: junk while the beans stay on the shelf).
DEFAULT_WANTED: Final[frozenset[LootCategory]] = frozenset(
    {
        LootCategory.FOOD,
        LootCategory.WATER,
        LootCategory.MEDICAL,
        LootCategory.TOOLS,
        LootCategory.WEAPONS,
        LootCategory.MATERIALS,
        LootCategory.LITERATURE,
    }
)

#: Hard cap and default for picks per container. Equal to the transfer batch
#: width (``actions.adapters.inventory.MAX_BATCH_ITEMS``, value pinned in
#: sync by a test rather than imported, so this package stays a leaf with no
#: dependency on the action layer): one selection is one
#: ``inventory.transfer_batch``, and a wider selection would be a list of
#: claims that single command could not honour. A container holding more is
#: re-selected after the batch lands, against its remaining contents.
MAX_ITEMS_PER_CONTAINER: Final = 8

#: Decimal places the running capacity remainder is kept to. Same job as
#: ``policy.selection.SCORE_PRECISION``: game weights are two-decimal values,
#: and without this an exact fit fails on binary float noise (1.0 - 0.9
#: leaves 0.09999…, which 0.1 then "exceeds"). The at-most-5e-7 units the
#: rounding can forgive is far below the game's weight granularity, and the
#: mod re-checks real capacity before every transferred item regardless.
CAPACITY_PRECISION: Final = 6

#: The most items one ``select`` call will consider. The observation layer
#: bounds container enumerations far below this; the cap exists so that a
#: broken caller cannot make this module the place where an unbounded list is
#: quietly ground through.
MAX_SELECT_CONTENTS: Final = 1024


@dataclass(frozen=True, slots=True)
class LootPolicy:
    """What the loot goal is allowed to want.

    ``wanted`` empty means :data:`DEFAULT_WANTED` — the ``useful_only``
    default. ``take_all`` widens the net to every category, including OTHER;
    it does *not* override reserves, because the user's word outranks the
    flag. ``respect_reserves`` exists so a user can say "yes, including my
    reserved stock" in as many words; it defaults on.
    """

    wanted: frozenset[LootCategory] = frozenset()
    take_all: bool = False
    max_items_per_container: int = MAX_ITEMS_PER_CONTAINER
    respect_reserves: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.max_items_per_container <= MAX_ITEMS_PER_CONTAINER:
            raise ValueError(
                f"max_items_per_container must be within "
                f"1..{MAX_ITEMS_PER_CONTAINER}, got {self.max_items_per_container}"
            )

    @property
    def effective_wanted(self) -> frozenset[LootCategory]:
        """The categories this policy actually takes."""
        if self.take_all:
            return frozenset(LootCategory)
        return self.wanted or DEFAULT_WANTED


#: The policy a bare ``loot_area`` with no arguments runs under.
DEFAULT_LOOT_POLICY: Final = LootPolicy()


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pick:
    """One item the selection takes: identity, category, weight."""

    item_ref: str
    full_type: str
    display_name: str
    category: LootCategory
    weight: float

    def as_dict(self) -> JsonDict:
        return {
            "item_ref": self.item_ref,
            "full_type": self.full_type,
            "display_name": self.display_name,
            "category": self.category.value,
            "weight": self.weight,
        }


@dataclass(frozen=True, slots=True)
class Leave:
    """One item the selection leaves, and the single reason why."""

    item_ref: str
    full_type: str
    display_name: str
    category: LootCategory
    weight: float
    reason: LeaveReason

    def as_dict(self) -> JsonDict:
        return {
            "item_ref": self.item_ref,
            "full_type": self.full_type,
            "display_name": self.display_name,
            "category": self.category.value,
            "weight": self.weight,
            "reason": self.reason.value,
        }


@dataclass(frozen=True, slots=True)
class Selection:
    """The verdict on one container's contents.

    Every input item appears exactly once, in ``take`` or in ``leave``, both
    in the same deterministic priority order the selection was scanned in.
    ``take_refs`` is shaped for ``inventory.transfer_batch``'s ``item_refs``.
    """

    take: tuple[Pick, ...]
    leave: tuple[Leave, ...]

    @property
    def take_refs(self) -> tuple[str, ...]:
        return tuple(pick.item_ref for pick in self.take)

    @property
    def take_weight(self) -> float:
        return sum(pick.weight for pick in self.take)

    def as_dict(self) -> JsonDict:
        return {
            "take": [pick.as_dict() for pick in self.take],
            "leave": [leave.as_dict() for leave in self.leave],
        }


#: Category → its position in the declared enum order; the first sort key.
_PRIORITY: Final[Mapping[LootCategory, int]] = MappingProxyType(
    {category: index for index, category in enumerate(LootCategory)}
)


def _safe_weight(value: float) -> float:
    """A weight the arithmetic can trust.

    Item weights come off the wire and are untrusted; a NaN would break the
    sort's total order and poison the capacity budget for every later item.
    A weight that is not a finite non-negative number is read as weightless —
    the smaller loss: one broken item over-taken beats a whole selection
    corrupted.
    """
    if not math.isfinite(value) or value < 0.0:
        return 0.0
    return value


def select(
    contents: Iterable[ItemView],
    *,
    policy: LootPolicy,
    free_capacity: float | None,
    is_reserved: Callable[[str], bool],
) -> Selection:
    """Decide, deterministically, what to take from one container.

    Args:
        contents: the container's enumerated items, in any order — the result
            does not depend on it. At most :data:`MAX_SELECT_CONTENTS` items.
        policy: what is wanted and how many picks one batch may hold.
        free_capacity: spare carrying capacity in weight units, or ``None``
            when unknown. Known capacity is spent greedily in priority order;
            ``None`` takes up to the batch cap and trusts the mod's own
            per-item stop to be the backstop.
        is_reserved: the user's reserve list, asked per ``full_type``. Must be
            a pure function of its argument — the selection's determinism is
            only as good as this callable's.

    Returns:
        A :class:`Selection` in which every input item appears exactly once.
        Candidates are visited in a total order — category priority (the
        declared :class:`LootCategory` order), then weight ascending, then
        ``full_type``, ``display_name`` and finally the unique item reference,
        so no two entries ever compare equal — and each is either taken or
        left with the first matching :class:`LeaveReason`.

    Raises:
        ValueError: on more than :data:`MAX_SELECT_CONTENTS` items, or a
            ``free_capacity`` that is negative or not a finite number.
    """
    items = tuple(contents)
    if len(items) > MAX_SELECT_CONTENTS:
        raise ValueError(
            f"select is bounded to {MAX_SELECT_CONTENTS} items per container, got {len(items)}"
        )
    if free_capacity is not None and (not math.isfinite(free_capacity) or free_capacity < 0.0):
        raise ValueError(f"free_capacity must be a finite non-negative number, got {free_capacity}")

    wanted = policy.effective_wanted
    ordered = sorted(
        ((item, classify(item), _safe_weight(item.weight)) for item in items),
        key=lambda entry: (
            _PRIORITY[entry[1]],
            entry[2],
            entry[0].full_type,
            entry[0].display_name,
            entry[0].ref,
        ),
    )

    take: list[Pick] = []
    leave: list[Leave] = []
    remaining = free_capacity
    for item, category, weight in ordered:
        reason = _leave_reason(
            category=category,
            weight=weight,
            full_type=item.full_type,
            policy=policy,
            wanted=wanted,
            is_reserved=is_reserved,
            free_capacity=free_capacity,
            remaining=remaining,
            picked=len(take),
        )
        if reason is None:
            take.append(
                Pick(
                    item_ref=item.ref,
                    full_type=item.full_type,
                    display_name=item.display_name,
                    category=category,
                    weight=weight,
                )
            )
            if remaining is not None:
                remaining = round(remaining - weight, CAPACITY_PRECISION)
        else:
            leave.append(
                Leave(
                    item_ref=item.ref,
                    full_type=item.full_type,
                    display_name=item.display_name,
                    category=category,
                    weight=weight,
                    reason=reason,
                )
            )
    return Selection(take=tuple(take), leave=tuple(leave))


def _leave_reason(
    *,
    category: LootCategory,
    weight: float,
    full_type: str,
    policy: LootPolicy,
    wanted: frozenset[LootCategory],
    is_reserved: Callable[[str], bool],
    free_capacity: float | None,
    remaining: float | None,
    picked: int,
) -> LeaveReason | None:
    """First reason not to take this item, or None to take it.

    Check order is the :class:`LeaveReason` declaration order and is fixed:
    the reported reason must be the most fundamental obstacle, not whichever
    check happened to run first. An exact fit is a fit — the capacity
    comparisons are strict — which the boundary test pins.
    """
    if policy.respect_reserves and is_reserved(full_type):
        return LeaveReason.RESERVED
    if category not in wanted:
        return LeaveReason.NOT_WANTED
    if free_capacity is not None and weight > free_capacity:
        return LeaveReason.UNCARRIABLE
    if picked >= policy.max_items_per_container:
        return LeaveReason.BATCH_FULL
    if remaining is not None and weight > remaining:
        return LeaveReason.OVER_CAPACITY
    return None


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SelectionSummary:
    """The counts a loot report is built from.

    Both mappings are copied and frozen at construction, so a caller holding
    the dict it passed in cannot rewrite a summary that has already been
    reported. The redundant totals are validated against the mappings —
    a summary whose headline number disagrees with its own breakdown is
    exactly the kind of lie this codebase refuses to emit.
    """

    taken_by_category: Mapping[LootCategory, int]
    left_by_category: Mapping[LootCategory, int]
    taken_count: int
    left_count: int
    taken_weight: float

    def __post_init__(self) -> None:
        taken_frozen = MappingProxyType(dict(self.taken_by_category))
        object.__setattr__(self, "taken_by_category", taken_frozen)
        object.__setattr__(self, "left_by_category", MappingProxyType(dict(self.left_by_category)))
        if self.taken_count != sum(self.taken_by_category.values()):
            raise ValueError(
                f"taken_count {self.taken_count} disagrees with its per-category breakdown"
            )
        if self.left_count != sum(self.left_by_category.values()):
            raise ValueError(
                f"left_count {self.left_count} disagrees with its per-category breakdown"
            )
        if not math.isfinite(self.taken_weight) or self.taken_weight < 0.0:
            raise ValueError(
                f"taken_weight must be finite and non-negative, got {self.taken_weight}"
            )

    def as_dict(self) -> JsonDict:
        # Categories render in declared (priority) order so two summaries of
        # the same selection serialise identically.
        return {
            "taken_by_category": {
                category.value: self.taken_by_category[category]
                for category in LootCategory
                if category in self.taken_by_category
            },
            "left_by_category": {
                category.value: self.left_by_category[category]
                for category in LootCategory
                if category in self.left_by_category
            },
            "taken_count": self.taken_count,
            "left_count": self.left_count,
            "taken_weight": self.taken_weight,
        }


def summarise(selection: Selection) -> SelectionSummary:
    """Fold one selection into the counts the goal's report emits."""
    taken: dict[LootCategory, int] = {}
    for pick in selection.take:
        taken[pick.category] = taken.get(pick.category, 0) + 1
    left: dict[LootCategory, int] = {}
    for entry in selection.leave:
        left[entry.category] = left.get(entry.category, 0) + 1
    return SelectionSummary(
        taken_by_category=taken,
        left_by_category=left,
        taken_count=len(selection.take),
        left_count=len(selection.leave),
        taken_weight=selection.take_weight,
    )
