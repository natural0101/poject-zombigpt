"""Deterministic food selection.

Which item the character eats is decided here, by code, and never by a language
model: a model that picks the sandwich is a model that eventually picks the
rotten one. The model may say "I am hungry"; :func:`select_food` says *what*,
*how much of it*, and — when it refuses — exactly what was wrong with every
candidate it looked at.

The two halves of the decision are deliberately different in kind:

* **Hard filters** reject outright and are never traded off against a score. No
  amount of freshness makes a poisonous item edible.
* **Scoring** ranks what is left, and returns its factors rather than a bare
  number, because "why that one?" is a question the user asks out loud.

Blueprint: ``docs/blueprint/05_GAMEPLAY_SKILLS.md`` §5.2 and
``docs/blueprint/17_DECISION_TABLES.md`` §17.3.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from ..protocol import InventoryView, ItemView, JsonDict, PlayerState, ReasonCode
from .config import CAPABILITY_EAT_PERCENTAGE, DEFAULT_POLICY_CONFIG, PolicyConfig
from .selection import (
    NO_CAPABILITIES,
    CapabilityLookup,
    Rejection,
    RejectionReason,
    ScoreBreakdown,
    ScoredCandidate,
    ScoreFactor,
    bounded_rejections,
    count_by_full_type,
    is_strategic_reserve,
    is_user_reserved,
    needs_transfer_to_main,
    rank_candidates,
    reach_rejection,
    read_bool,
    read_float,
    read_int,
    read_str,
    truncation_note,
)

__all__ = [
    "FoodChoice",
    "FoodSelection",
    "FoodView",
    "select_food",
]

#: Freshness labels the mod may send. Anything else falls back to the numeric
#: rot progress, which is the value the game actually tracks.
FRESHNESS_FRESH: Final = "fresh"
FRESHNESS_STALE: Final = "stale"
FRESHNESS_ROTTEN: Final = "rotten"

#: A stale item is half-spoiled by definition, whatever its rot counter says.
_STALE_CEILING: Final = 0.5


@dataclass(frozen=True, slots=True)
class FoodView:
    """Typed reading of :attr:`ItemView.food`.

    ``ItemView`` keeps the sub-object as a raw mapping on purpose — the domain
    rules that interpret it live here. Sign conventions follow the game:
    ``hunger_change`` and ``thirst_change`` are *deltas applied to the
    character*, so a negative hunger change is the one that feeds you.
    """

    edible: bool
    destroyed: bool
    hunger_change: float
    thirst_change: float
    calories: float
    freshness: str
    rot_progress: float
    raw: bool
    raw_unsafe: bool
    requires_cooking: bool
    cooked: bool
    burnt: bool
    burn_progress: float
    poisonous: bool
    tainted: bool
    frozen: bool
    required_tool: str
    unhappy_change: float
    boredom_change: float
    remaining_portions: int
    total_portions: int

    @classmethod
    def from_item(cls, item: ItemView) -> FoodView | None:
        """Read the food sub-object, or None when the item is not food."""
        payload: Mapping[str, object] | None = item.food
        if payload is None:
            return None
        total = max(1, read_int(payload, "total_portions", 1))
        return cls(
            edible=read_bool(payload, "edible", True),
            destroyed=read_bool(payload, "destroyed"),
            hunger_change=read_float(payload, "hunger_change"),
            thirst_change=read_float(payload, "thirst_change"),
            calories=read_float(payload, "calories"),
            freshness=read_str(payload, "freshness").lower(),
            rot_progress=_clamp01(read_float(payload, "rot_progress")),
            raw=read_bool(payload, "raw"),
            raw_unsafe=read_bool(payload, "raw_unsafe"),
            requires_cooking=read_bool(payload, "requires_cooking"),
            cooked=read_bool(payload, "cooked"),
            burnt=read_bool(payload, "burnt"),
            burn_progress=_clamp01(read_float(payload, "burn_progress")),
            # Either flag is enough: the mod reports a boolean for known-toxic
            # types and a power for the ones the game scores numerically.
            poisonous=read_bool(payload, "poisonous") or read_float(payload, "poison_power") > 0.0,
            tainted=read_bool(payload, "tainted"),
            frozen=read_bool(payload, "frozen"),
            required_tool=read_str(payload, "required_tool").lower(),
            unhappy_change=read_float(payload, "unhappy_change"),
            boredom_change=read_float(payload, "boredom_change"),
            remaining_portions=max(0, read_int(payload, "remaining_portions", total)),
            total_portions=total,
        )

    @property
    def hunger_relief(self) -> float:
        """How much hunger the whole item removes; 0 when it removes none."""
        return max(0.0, -self.hunger_change)

    @property
    def is_rotten(self) -> bool:
        return self.freshness == FRESHNESS_ROTTEN

    @property
    def freshness_score(self) -> float:
        """1.0 for pristine, 0.0 for rotten."""
        if self.is_rotten:
            return 0.0
        remaining = 1.0 - self.rot_progress
        if self.freshness == FRESHNESS_STALE:
            return min(_STALE_CEILING, remaining)
        return remaining

    @property
    def portion_fraction(self) -> float:
        """Share of the item still uneaten."""
        return _clamp01(self.remaining_portions / self.total_portions)

    @property
    def cooking_would_help(self) -> bool:
        """True for food that is edible as-is but meant to be cooked."""
        return self.raw and not self.cooked


@dataclass(frozen=True, slots=True)
class FoodChoice:
    """The item to eat, how much of it, and why."""

    item_ref: str
    display_name: str
    container_ref: str
    fraction: float
    portioned: bool
    needs_transfer_to_main: bool
    rationale: str
    breakdown: ScoreBreakdown

    def __post_init__(self) -> None:
        if not 0.0 < self.fraction <= 1.0:
            raise ValueError(f"fraction must be within (0, 1], got {self.fraction}")
        if not self.portioned and self.fraction != 1.0:
            raise ValueError("a whole-unit choice must have fraction 1.0")

    def as_dict(self) -> JsonDict:
        return {
            "item_ref": self.item_ref,
            "display_name": self.display_name,
            "container_ref": self.container_ref,
            "fraction": self.fraction,
            "portioned": self.portioned,
            "needs_transfer_to_main": self.needs_transfer_to_main,
            "rationale": self.rationale,
            "score": self.breakdown.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class FoodSelection:
    """A choice or a refusal, plus everything that was turned down.

    The rejections travel with both outcomes. On a refusal they *are* the
    answer; on a success they are what lets the agent say "the ham is rotten,
    so I had the beans" instead of an unexplained pick.
    """

    choice: FoodChoice | None
    reason_code: ReasonCode | None
    rejections: tuple[Rejection, ...]
    ranked: tuple[ScoredCandidate, ...]
    #: How many candidates were refused in total. ``rejections`` is a bounded
    #: sample of them, so this is the number that must not be rounded down: a
    #: refusal that listed 64 of 300 reasons and said nothing about the other
    #: 236 would read as a complete answer while being a truncated one.
    rejected_count: int = 0

    def __post_init__(self) -> None:
        if (self.choice is None) == (self.reason_code is None):
            raise ValueError("a selection is either a choice or a refusal, never both or neither")
        if self.rejected_count < len(self.rejections):
            raise ValueError(
                f"rejected_count {self.rejected_count} is below the "
                f"{len(self.rejections)} rejections carried"
            )

    @property
    def is_refusal(self) -> bool:
        return self.choice is None

    @property
    def rejections_truncated(self) -> bool:
        return self.rejected_count > len(self.rejections)

    @classmethod
    def chosen(
        cls,
        choice: FoodChoice,
        *,
        rejections: tuple[Rejection, ...],
        ranked: tuple[ScoredCandidate, ...],
        rejected_count: int,
    ) -> FoodSelection:
        return cls(
            choice=choice,
            reason_code=None,
            rejections=rejections,
            ranked=ranked,
            rejected_count=rejected_count,
        )

    @classmethod
    def refused(cls, rejections: tuple[Rejection, ...], rejected_count: int) -> FoodSelection:
        return cls(
            choice=None,
            reason_code=ReasonCode.NO_SAFE_FOOD,
            rejections=rejections,
            ranked=(),
            rejected_count=rejected_count,
        )

    def explain(self) -> str:
        """A sentence for the user, whichever way the selection went."""
        if self.choice is not None:
            return self.choice.rationale
        if not self.rejections:
            return "there is no food in reach"
        reasons = "; ".join(r.describe() for r in self.rejections)
        note = truncation_note(len(self.rejections), self.rejected_count)
        return f"nothing safe to eat: {reasons}{note}"

    def as_dict(self) -> JsonDict:
        return {
            "choice": None if self.choice is None else self.choice.as_dict(),
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "rejections": [r.as_dict() for r in self.rejections],
            "rejected_count": self.rejected_count,
            "ranked": [
                {"item_ref": c.item_ref, "score": c.score, "factors": c.breakdown.as_dict()}
                for c in self.ranked
            ],
        }


@dataclass(frozen=True, slots=True)
class _Context:
    """Everything the filters and the scorer need besides the item itself."""

    config: PolicyConfig
    hunger: float
    thirst: float
    deficit: float
    hunger_critical: bool
    type_counts: Mapping[str, int]
    available_tools: frozenset[str]
    inventory: InventoryView
    portioning_available: bool


def select_food(
    inventory: InventoryView,
    player: PlayerState,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
    *,
    capabilities: CapabilityLookup = NO_CAPABILITIES,
) -> FoodSelection:
    """Pick the best safe thing to eat, or refuse with the reasons why.

    Args:
        inventory: the containers and items the mod enumerated. Items whose
            container is missing from the view are refused rather than guessed
            at.
        player: read for hunger and thirst only; nothing here mutates it.
        config: thresholds and weights.
        capabilities: probe results. Percentage eating is only used when its
            probe says so — otherwise the choice falls back to whole units and
            the rationale says as much.

    Returns:
        A :class:`FoodSelection` carrying either a choice or
        :attr:`ReasonCode.NO_SAFE_FOOD`, together with one rejection per
        candidate examined — trimmed to ``config.max_reported_rejections``,
        with the untrimmed total in ``rejected_count``.
    """
    context = _Context(
        config=config,
        hunger=player.hunger,
        thirst=player.thirst,
        deficit=config.hunger_deficit(player.hunger),
        hunger_critical=config.is_hunger_critical(player.hunger),
        type_counts=count_by_full_type(inventory.items),
        available_tools=_available_tools(inventory, config),
        inventory=inventory,
        portioning_available=capabilities.usable(CAPABILITY_EAT_PERCENTAGE),
    )

    rejections: list[Rejection] = []
    candidates: list[tuple[ItemView, FoodView, ScoredCandidate]] = []
    for item in inventory.items:
        view = FoodView.from_item(item)
        if view is None:
            continue  # not food at all: not a rejected candidate, just noise
        rejection = _first_rejection(item, view, context)
        if rejection is not None:
            rejections.append(rejection)
            continue
        breakdown = _score(item, view, context)
        candidates.append(
            (item, view, ScoredCandidate(item.ref, item.display_name, breakdown)),
        )

    reported = bounded_rejections(rejections, config)
    if not candidates:
        return FoodSelection.refused(reported, len(rejections))

    ranked = rank_candidates(scored for _, _, scored in candidates)
    best_ref = ranked[0].item_ref
    item, view, scored = next(entry for entry in candidates if entry[0].ref == best_ref)
    fraction, portioned = _choose_fraction(view, context)
    choice = FoodChoice(
        item_ref=item.ref,
        display_name=item.display_name,
        container_ref=item.container_ref,
        fraction=fraction,
        portioned=portioned,
        needs_transfer_to_main=needs_transfer_to_main(inventory, item),
        rationale=_rationale(
            item,
            view,
            scored=scored,
            fraction=fraction,
            portioned=portioned,
            context=context,
            capabilities=capabilities,
        ),
        breakdown=scored.breakdown,
    )
    return FoodSelection.chosen(
        choice,
        rejections=reported,
        ranked=ranked,
        rejected_count=len(rejections),
    )


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------
# Order is fixed and meaningful: the reported reason is the first one that
# matches, and the list runs from "this will hurt you" through "this is not
# yours" to "this would not help". A user reading a refusal gets the most
# fundamental problem with the item, not whichever check happened to run first.

_Filter = Callable[[ItemView, FoodView, _Context], Rejection | None]


def _reject(item: ItemView, reason: RejectionReason, detail: str) -> Rejection:
    return Rejection(
        item_ref=item.ref,
        display_name=item.display_name,
        reason=reason,
        detail=detail,
    )


def _filter_edible(item: ItemView, view: FoodView, _context: _Context) -> Rejection | None:
    if view.edible:
        return None
    return _reject(item, RejectionReason.NOT_EDIBLE, "the game does not consider it edible")


def _filter_destroyed(item: ItemView, view: FoodView, _context: _Context) -> Rejection | None:
    if not view.destroyed:
        return None
    return _reject(item, RejectionReason.DESTROYED, "the item is destroyed")


def _filter_poisonous(item: ItemView, view: FoodView, _context: _Context) -> Rejection | None:
    if not view.poisonous:
        return None
    return _reject(item, RejectionReason.POISONOUS, "it is poisonous")


def _filter_tainted(item: ItemView, view: FoodView, _context: _Context) -> Rejection | None:
    if not view.tainted:
        return None
    return _reject(item, RejectionReason.TAINTED, "it is tainted")


def _filter_rotten(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    if context.config.allow_rotten:
        return None
    if view.is_rotten:
        return _reject(item, RejectionReason.ROTTEN, "it has rotted")
    if view.rot_progress > context.config.max_rot_progress:
        return _reject(
            item,
            RejectionReason.ROTTEN,
            f"it is {view.rot_progress:.0%} spoiled, past the "
            f"{context.config.max_rot_progress:.0%} the policy tolerates",
        )
    return None


def _filter_raw(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    if context.config.allow_raw or not view.raw or not view.raw_unsafe:
        return None
    return _reject(item, RejectionReason.RAW_UNSAFE, "it is raw and unsafe to eat raw")


def _filter_requires_cooking(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    if context.config.allow_required_cooking or not view.requires_cooking or view.cooked:
        return None
    return _reject(
        item,
        RejectionReason.REQUIRES_COOKING,
        "it has to be cooked first and cooking is not part of this plan",
    )


def _filter_burnt(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    if view.burnt:
        return _reject(item, RejectionReason.BURNT, "it is burnt")
    if view.burn_progress > context.config.max_burn_progress:
        return _reject(
            item,
            RejectionReason.BURNT,
            f"it is {view.burn_progress:.0%} burnt, past the "
            f"{context.config.max_burn_progress:.0%} the policy tolerates",
        )
    return None


def _filter_frozen(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    if context.config.allow_frozen or not view.frozen:
        return None
    return _reject(item, RejectionReason.FROZEN, "it is frozen solid and would have to thaw first")


def _filter_tool(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    if not view.required_tool or view.required_tool in context.available_tools:
        return None
    return _reject(
        item,
        RejectionReason.TOOL_UNAVAILABLE,
        f"it needs a {view.required_tool} and there is none in reach",
    )


def _filter_user_reserved(item: ItemView, _view: FoodView, context: _Context) -> Rejection | None:
    if not is_user_reserved(item, context.config):
        return None
    return _reject(
        item,
        RejectionReason.USER_RESERVED,
        "you marked it as reserved, so it is never taken automatically",
    )


def _filter_strategic_reserve(
    item: ItemView, _view: FoodView, context: _Context
) -> Rejection | None:
    if context.hunger_critical or not is_strategic_reserve(item, context.config):
        return None
    return _reject(
        item,
        RejectionReason.STRATEGIC_RESERVE,
        f"it is emergency stock and hunger is {context.hunger:.2f}, below the critical "
        f"{context.config.critical_hunger:.2f} that would open it up",
    )


def _filter_reach(item: ItemView, _view: FoodView, context: _Context) -> Rejection | None:
    return reach_rejection(context.inventory, item, context.config)


def _filter_portions_left(item: ItemView, view: FoodView, _context: _Context) -> Rejection | None:
    """Refuse an item the mod says is already finished.

    Without this an empty tin is not merely selectable, it is *preferred*: the
    portions factor rewards an already-opened item, and zero portions left is
    the extreme of already-opened.
    """
    if view.remaining_portions > 0:
        return None
    return _reject(
        item,
        RejectionReason.NO_PORTIONS_LEFT,
        f"none of its {view.total_portions} portions are left",
    )


def _filter_no_relief(item: ItemView, view: FoodView, _context: _Context) -> Rejection | None:
    if view.hunger_relief > 0.0:
        return None
    return _reject(
        item,
        RejectionReason.NO_HUNGER_RELIEF,
        "eating it would not reduce hunger at all",
    )


_FILTERS: Final[tuple[_Filter, ...]] = (
    _filter_edible,
    _filter_destroyed,
    _filter_poisonous,
    _filter_tainted,
    _filter_rotten,
    _filter_raw,
    _filter_requires_cooking,
    _filter_burnt,
    _filter_frozen,
    _filter_tool,
    _filter_user_reserved,
    _filter_strategic_reserve,
    _filter_reach,
    _filter_portions_left,
    _filter_no_relief,
)


def _first_rejection(item: ItemView, view: FoodView, context: _Context) -> Rejection | None:
    for check in _FILTERS:
        rejection = check(item, view, context)
        if rejection is not None:
            return rejection
    return None


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _score(item: ItemView, view: FoodView, context: _Context) -> ScoreBreakdown:
    config = context.config
    deficit = context.deficit
    relief = view.hunger_relief
    # With percentage eating the surplus of a large item is simply not eaten,
    # so it is not waste. Without it, the whole item goes.
    effective = min(relief, deficit) if context.portioning_available and deficit > 0 else relief
    coverage = min(effective, deficit) / deficit if deficit > 0 else 1.0
    scarcity_count = max(1, context.type_counts.get(item.full_type, 1))

    factors = (
        ScoreFactor(
            name="hunger_fit",
            raw=-abs(deficit - effective),
            weight=config.food_weight_hunger_fit,
            detail=f"covers {effective:.2f} of a {deficit:.2f} deficit",
        ),
        ScoreFactor(
            name="urgency",
            raw=context.hunger * coverage,
            weight=config.food_weight_urgency,
            detail=f"hunger {context.hunger:.2f}, coverage {coverage:.0%}",
        ),
        ScoreFactor(
            name="calories",
            raw=min(1.0, view.calories / config.calorie_reference),
            weight=config.food_weight_calories,
            detail=f"{view.calories:.0f} kcal",
        ),
        ScoreFactor(
            name="freshness",
            raw=view.freshness_score,
            weight=config.food_weight_freshness,
            detail=view.freshness or f"{view.rot_progress:.0%} spoiled",
        ),
        ScoreFactor(
            name="unhappiness",
            raw=-max(0.0, view.unhappy_change) / config.unhappiness_reference,
            weight=config.food_weight_unhappiness,
        ),
        ScoreFactor(
            name="boredom",
            raw=-max(0.0, view.boredom_change) / config.boredom_reference,
            weight=config.food_weight_boredom,
        ),
        ScoreFactor(
            name="thirst_side_effect",
            # A salty meal costs more when the character is already thirsty.
            raw=-(max(0.0, view.thirst_change) / config.thirst_change_reference)
            * (1.0 + context.thirst),
            weight=config.food_weight_thirst_side_effect,
        ),
        ScoreFactor(
            name="weight",
            raw=-item.weight / config.weight_reference,
            weight=config.food_weight_weight,
            detail=f"{item.weight:.2f} units carried",
        ),
        ScoreFactor(
            name="scarcity",
            raw=-1.0 / scarcity_count,
            weight=config.food_weight_scarcity,
            detail=f"{scarcity_count} of this type in reach",
        ),
        ScoreFactor(
            name="cooking",
            raw=-1.0 if view.cooking_would_help else 0.0,
            weight=config.food_weight_cooking,
        ),
        ScoreFactor(
            name="portions",
            # Prefer an item already opened: it is the one that spoils next.
            raw=1.0 - view.portion_fraction,
            weight=config.food_weight_portions,
            detail=f"{view.remaining_portions}/{view.total_portions} portions left",
        ),
        ScoreFactor(
            name="waste",
            raw=-max(0.0, effective - deficit),
            weight=config.food_weight_waste,
        ),
    )
    return ScoreBreakdown(factors)


# --------------------------------------------------------------------------
# portion and rationale
# --------------------------------------------------------------------------


def _choose_fraction(view: FoodView, context: _Context) -> tuple[float, bool]:
    """How much of the item to eat, and whether that is a real fraction.

    Returns ``(1.0, False)`` when percentage eating is unavailable: the action
    can then only consume a whole unit, and claiming otherwise would produce a
    command the mod cannot honour.
    """
    if not context.portioning_available:
        return 1.0, False
    config = context.config
    relief = view.hunger_relief
    needed = context.deficit / relief if relief > 0 else 1.0
    step = config.eat_fraction_step
    # Round the portion up: under-eating leaves the need unmet and costs a
    # second action, which is worse than a little surplus.
    quantised = math.ceil(max(needed, config.min_eat_fraction) / step) * step
    return min(1.0, round(quantised, 4)), True


def _rationale(
    item: ItemView,
    view: FoodView,
    *,
    scored: ScoredCandidate,
    fraction: float,
    portioned: bool,
    context: _Context,
    capabilities: CapabilityLookup,
) -> str:
    parts = [
        f"{item.display_name} scores {scored.score:.2f} ({scored.breakdown.explain()})",
        f"hunger {context.hunger:.2f} against a target of {context.config.satisfied_hunger:.2f}",
    ]
    if portioned:
        parts.append(f"eating {fraction:.0%} of it")
    else:
        state = capabilities.state(CAPABILITY_EAT_PERCENTAGE).value
        parts.append(
            f"eating the whole item because percentage eating is unavailable "
            f"(probe {CAPABILITY_EAT_PERCENTAGE}: {state})"
        )
    if needs_transfer_to_main(context.inventory, item):
        parts.append("it has to be moved to the main inventory first")
    return "; ".join(parts)


def _available_tools(inventory: InventoryView, config: PolicyConfig) -> frozenset[str]:
    """Tool names the inventory can supply, lower-cased.

    A tool is matched by tag or by the short form of its type, so the mod can
    say ``required_tool: "canopener"`` and be satisfied by ``Base.TinOpener``
    tagged ``canopener`` without the policy hard-coding a table of item types.

    Only tools the policy could actually reach count. A can opener on a shelf
    the policy refuses to walk to is not an opener the plan may rely on, and
    counting it would clear the tool filter on a promise selection cannot keep.
    """
    names: set[str] = set()
    for item in inventory.items:
        if reach_rejection(inventory, item, config) is not None:
            continue
        names.update(tag.lower() for tag in item.tags)
        names.add(item.full_type.rsplit(".", 1)[-1].lower())
    return frozenset(names)


def _clamp01(value: float) -> float:
    return min(1.0, max(0.0, value))
