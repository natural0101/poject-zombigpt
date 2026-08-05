"""Deterministic drink selection.

Same contract as :mod:`.food`: hard filters that reject, a scored ranking of
what survives, and a refusal that explains itself candidate by candidate.

Two rules here are safety rules rather than preferences, and both are visible
in the code as filters rather than as a low score:

* **Tainted water is never drunk automatically.** Blueprint §5.19 lists it
  among the actions that require explicit permission.
* **The last container is not emptied for a thirst that is not yet critical.**
  When the game cannot pour half a bottle — the ``drink_percentage`` probe is
  the thing that would say it can — the policy refuses that candidate outright
  instead of draining the character's last water.

World water sources are a *separate capability* and are not modelled as
candidates here: this module only ever proposes something already in the
inventory. It reports the probe state for the world source so the caller can
decide whether looking for a tap is even an option.

Blueprint: ``docs/blueprint/05_GAMEPLAY_SKILLS.md`` §5.3 and
``docs/blueprint/17_DECISION_TABLES.md`` §17.4.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from ..protocol import (
    CapabilityState,
    InventoryView,
    ItemView,
    JsonDict,
    PlayerState,
    ReasonCode,
)
from .config import (
    CAPABILITY_DRINK_PERCENTAGE,
    CAPABILITY_DRINK_WORLD_SOURCE,
    DEFAULT_POLICY_CONFIG,
    PolicyConfig,
)
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
    read_str,
)

__all__ = [
    "DrinkChoice",
    "DrinkSelection",
    "DrinkView",
    "select_drink",
]

#: Fluid type names the mod may report. ``water`` is the only one the policy
#: actively prefers; the rest are ranked but never promoted.
FLUID_WATER: Final = "water"
FLUID_ALCOHOL: Final = "alcohol"


@dataclass(frozen=True, slots=True)
class DrinkView:
    """Typed reading of :attr:`ItemView.fluid`, falling back to ``food``.

    A canned drink is a food item with a thirst change in this protocol, so
    both sub-objects are accepted. ``thirst_change`` is the delta applied to
    the character for consuming *everything the container still holds*, so a
    negative value is the one that helps.
    """

    drinkable: bool
    destroyed: bool
    thirst_change: float
    fluid_type: str
    water: bool
    tainted: bool
    poisonous: bool
    alcoholic: bool
    alcohol_units: float
    remaining_units: float
    capacity_units: float
    unhappy_change: float

    @classmethod
    def from_item(cls, item: ItemView) -> DrinkView | None:
        """Read the fluid (or food) sub-object, or None when it is not a drink."""
        payload: Mapping[str, object] | None = item.fluid if item.fluid is not None else item.food
        if payload is None:
            return None
        fluid_type = read_str(payload, "type").lower()
        water = read_bool(payload, "water") or fluid_type == FLUID_WATER
        alcohol_units = read_float(payload, "alcohol_units")
        capacity = read_float(payload, "capacity_units", 1.0)
        capacity = capacity if capacity > 0.0 else 1.0
        thirst_change = read_float(payload, "thirst_change")
        return cls(
            # An item with no explicit flag counts as drinkable only when it
            # actually relieves thirst; guessing the other way would let the
            # policy try to drink a tin of beans.
            drinkable=read_bool(payload, "drinkable", water or thirst_change < 0.0),
            destroyed=read_bool(payload, "destroyed"),
            thirst_change=thirst_change,
            fluid_type=fluid_type,
            water=water,
            tainted=read_bool(payload, "tainted"),
            poisonous=read_bool(payload, "poisonous") or read_float(payload, "poison_power") > 0.0,
            alcoholic=read_bool(payload, "alcoholic")
            or fluid_type == FLUID_ALCOHOL
            or alcohol_units > 0.0,
            alcohol_units=alcohol_units,
            remaining_units=max(0.0, read_float(payload, "remaining_units", capacity)),
            capacity_units=capacity,
            unhappy_change=read_float(payload, "unhappy_change"),
        )

    @property
    def thirst_relief(self) -> float:
        """How much thirst emptying the container would remove."""
        return max(0.0, -self.thirst_change)

    @property
    def remaining_fraction(self) -> float:
        return min(1.0, max(0.0, self.remaining_units / self.capacity_units))


@dataclass(frozen=True, slots=True)
class DrinkChoice:
    """The container to drink from, how much of it, and why."""

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
            raise ValueError("a whole-container choice must have fraction 1.0")

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
class DrinkSelection:
    """A choice or a refusal, plus the state of the world-source capability.

    ``world_source_state`` is reported on both outcomes and is never acted on
    here. It exists so that "there is nothing in your bag" and "there is
    nothing in your bag and I also cannot use the sink" are distinguishable to
    whoever has to tell the user.
    """

    choice: DrinkChoice | None
    reason_code: ReasonCode | None
    rejections: tuple[Rejection, ...]
    ranked: tuple[ScoredCandidate, ...]
    world_source_state: CapabilityState

    def __post_init__(self) -> None:
        if (self.choice is None) == (self.reason_code is None):
            raise ValueError("a selection is either a choice or a refusal, never both or neither")

    @property
    def is_refusal(self) -> bool:
        return self.choice is None

    @property
    def world_source_usable(self) -> bool:
        """True when a *separate* skill could try a world water source."""
        return self.world_source_state.usable

    @classmethod
    def chosen(
        cls,
        choice: DrinkChoice,
        *,
        rejections: tuple[Rejection, ...],
        ranked: tuple[ScoredCandidate, ...],
        world_source_state: CapabilityState,
    ) -> DrinkSelection:
        return cls(
            choice=choice,
            reason_code=None,
            rejections=rejections,
            ranked=ranked,
            world_source_state=world_source_state,
        )

    @classmethod
    def refused(
        cls,
        rejections: tuple[Rejection, ...],
        *,
        world_source_state: CapabilityState,
    ) -> DrinkSelection:
        return cls(
            choice=None,
            reason_code=ReasonCode.NO_SAFE_DRINK,
            rejections=rejections,
            ranked=(),
            world_source_state=world_source_state,
        )

    def explain(self) -> str:
        if self.choice is not None:
            return self.choice.rationale
        head = "nothing safe to drink"
        if self.rejections:
            head = f"{head}: " + "; ".join(r.describe() for r in self.rejections)
        if self.world_source_usable:
            return f"{head}; a world water source may still be an option"
        return (
            f"{head}; world water sources are unavailable "
            f"({CAPABILITY_DRINK_WORLD_SOURCE}: {self.world_source_state.value})"
        )

    def as_dict(self) -> JsonDict:
        return {
            "choice": None if self.choice is None else self.choice.as_dict(),
            "reason_code": None if self.reason_code is None else self.reason_code.value,
            "rejections": [r.as_dict() for r in self.rejections],
            "ranked": [
                {"item_ref": c.item_ref, "score": c.score, "factors": c.breakdown.as_dict()}
                for c in self.ranked
            ],
            "world_source_state": self.world_source_state.value,
        }


@dataclass(frozen=True, slots=True)
class _Context:
    config: PolicyConfig
    thirst: float
    deficit: float
    thirst_critical: bool
    type_counts: Mapping[str, int]
    inventory: InventoryView
    portioning_available: bool


def select_drink(
    inventory: InventoryView,
    player: PlayerState,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
    *,
    capabilities: CapabilityLookup = NO_CAPABILITIES,
) -> DrinkSelection:
    """Pick the best safe thing to drink, or refuse with the reasons why.

    Selection runs in two passes. The first applies the filters that depend
    only on the item; the second applies the last-container rule, which cannot
    be decided until it is known how many candidates survived the first.
    """
    context = _Context(
        config=config,
        thirst=player.thirst,
        deficit=config.thirst_deficit(player.thirst),
        thirst_critical=config.is_thirst_critical(player.thirst),
        type_counts=count_by_full_type(inventory.items),
        inventory=inventory,
        portioning_available=capabilities.is_usable(CAPABILITY_DRINK_PERCENTAGE),
    )
    world_state = capabilities.state(CAPABILITY_DRINK_WORLD_SOURCE)

    rejections: list[Rejection] = []
    survivors: list[tuple[ItemView, DrinkView]] = []
    for item in inventory.items:
        view = DrinkView.from_item(item)
        if view is None:
            continue
        rejection = _first_rejection(item, view, context)
        if rejection is not None:
            rejections.append(rejection)
            continue
        survivors.append((item, view))

    last_container = len(survivors) == 1
    candidates: list[tuple[ItemView, DrinkView, ScoredCandidate]] = []
    for item, view in survivors:
        rejection = _last_container_rejection(item, context, last_container=last_container)
        if rejection is not None:
            rejections.append(rejection)
            continue
        breakdown = _score(item, view, context)
        candidates.append((item, view, ScoredCandidate(item.ref, item.display_name, breakdown)))

    reported = bounded_rejections(rejections, config)
    if not candidates:
        return DrinkSelection.refused(reported, world_source_state=world_state)

    ranked = rank_candidates(scored for _, _, scored in candidates)
    best_ref = ranked[0].item_ref
    item, view, scored = next(entry for entry in candidates if entry[0].ref == best_ref)
    fraction, portioned, capped = _choose_fraction(view, context, last_container=last_container)
    choice = DrinkChoice(
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
            capped=capped,
            context=context,
        ),
        breakdown=scored.breakdown,
    )
    return DrinkSelection.chosen(
        choice,
        rejections=reported,
        ranked=ranked,
        world_source_state=world_state,
    )


# --------------------------------------------------------------------------
# hard filters
# --------------------------------------------------------------------------

_Filter = Callable[[ItemView, DrinkView, _Context], Rejection | None]


def _reject(item: ItemView, reason: RejectionReason, detail: str) -> Rejection:
    return Rejection(
        item_ref=item.ref,
        display_name=item.display_name,
        reason=reason,
        detail=detail,
    )


def _filter_drinkable(item: ItemView, view: DrinkView, _context: _Context) -> Rejection | None:
    if view.drinkable:
        return None
    return _reject(item, RejectionReason.NOT_DRINKABLE, "it is not something that can be drunk")


def _filter_destroyed(item: ItemView, view: DrinkView, _context: _Context) -> Rejection | None:
    if not view.destroyed:
        return None
    return _reject(item, RejectionReason.DESTROYED, "the container is destroyed")


def _filter_empty(item: ItemView, view: DrinkView, context: _Context) -> Rejection | None:
    if view.remaining_units > context.config.min_remaining_units:
        return None
    return _reject(
        item,
        RejectionReason.EMPTY_CONTAINER,
        f"only {view.remaining_units:.2f} units left, which counts as empty",
    )


def _filter_tainted(item: ItemView, view: DrinkView, context: _Context) -> Rejection | None:
    if not view.tainted or context.config.allow_tainted_water:
        return None
    return _reject(
        item,
        RejectionReason.TAINTED,
        "it is tainted water, which is never drunk without explicit permission",
    )


def _filter_poisonous(item: ItemView, view: DrinkView, _context: _Context) -> Rejection | None:
    if not view.poisonous:
        return None
    return _reject(item, RejectionReason.POISONOUS, "it is poisonous")


def _filter_alcohol(item: ItemView, view: DrinkView, context: _Context) -> Rejection | None:
    if not view.alcoholic or context.config.allow_alcohol_for_thirst:
        return None
    return _reject(
        item,
        RejectionReason.ALCOHOL_NOT_PERMITTED,
        "it is alcoholic and policy forbids drinking alcohol for thirst",
    )


def _filter_user_reserved(item: ItemView, _view: DrinkView, context: _Context) -> Rejection | None:
    if not is_user_reserved(item, context.config):
        return None
    return _reject(
        item,
        RejectionReason.USER_RESERVED,
        "you marked it as reserved, so it is never taken automatically",
    )


def _filter_strategic_reserve(
    item: ItemView, _view: DrinkView, context: _Context
) -> Rejection | None:
    if context.thirst_critical or not is_strategic_reserve(item, context.config):
        return None
    return _reject(
        item,
        RejectionReason.STRATEGIC_RESERVE,
        f"it is emergency stock and thirst is {context.thirst:.2f}, below the critical "
        f"{context.config.critical_thirst:.2f} that would open it up",
    )


def _filter_reach(item: ItemView, _view: DrinkView, context: _Context) -> Rejection | None:
    return reach_rejection(context.inventory, item, context.config)


def _filter_no_relief(item: ItemView, view: DrinkView, _context: _Context) -> Rejection | None:
    if view.thirst_relief > 0.0:
        return None
    return _reject(
        item,
        RejectionReason.NO_THIRST_RELIEF,
        "drinking it would not reduce thirst at all",
    )


_FILTERS: Final[tuple[_Filter, ...]] = (
    _filter_drinkable,
    _filter_destroyed,
    _filter_empty,
    _filter_tainted,
    _filter_poisonous,
    _filter_alcohol,
    _filter_user_reserved,
    _filter_strategic_reserve,
    _filter_reach,
    _filter_no_relief,
)


def _first_rejection(item: ItemView, view: DrinkView, context: _Context) -> Rejection | None:
    for check in _FILTERS:
        rejection = check(item, view, context)
        if rejection is not None:
            return rejection
    return None


def _last_container_rejection(
    item: ItemView,
    context: _Context,
    *,
    last_container: bool,
) -> Rejection | None:
    """Refuse to empty the last container for a thirst that can wait.

    Only reachable when partial drinking is unavailable: with the
    ``drink_percentage`` capability the same rule is expressed as a cap on the
    fraction instead, and the character still gets a drink.
    """
    if not last_container or context.thirst_critical or context.portioning_available:
        return None
    return _reject(
        item,
        RejectionReason.LAST_CONTAINER_RESERVE,
        f"it is the last drink in reach and thirst is {context.thirst:.2f}, below the critical "
        f"{context.config.critical_thirst:.2f}; drinking it would empty it completely because "
        f"partial drinking is unavailable ({CAPABILITY_DRINK_PERCENTAGE})",
    )


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _score(item: ItemView, view: DrinkView, context: _Context) -> ScoreBreakdown:
    config = context.config
    deficit = context.deficit
    relief = view.thirst_relief
    effective = min(relief, deficit) if context.portioning_available and deficit > 0 else relief
    scarcity_count = max(1, context.type_counts.get(item.full_type, 1))
    transfer = needs_transfer_to_main(context.inventory, item)

    factors = (
        ScoreFactor(
            name="thirst_fit",
            raw=-abs(deficit - effective),
            weight=config.drink_weight_thirst_fit,
            detail=f"covers {effective:.2f} of a {deficit:.2f} deficit",
        ),
        ScoreFactor(
            name="water",
            raw=1.0 if view.water else 0.0,
            weight=config.drink_weight_water,
            detail=view.fluid_type or ("water" if view.water else "other fluid"),
        ),
        ScoreFactor(
            name="remaining",
            # Prefer the fuller container so the small backups stay intact.
            raw=view.remaining_fraction,
            weight=config.drink_weight_remaining,
            detail=f"{view.remaining_units:.2f}/{view.capacity_units:.2f} units left",
        ),
        ScoreFactor(
            name="side_effects",
            raw=-(
                max(0.0, view.unhappy_change) / config.unhappiness_reference
                + view.alcohol_units / config.alcohol_reference
            ),
            weight=config.drink_weight_side_effects,
        ),
        ScoreFactor(
            name="weight",
            raw=-item.weight / config.weight_reference,
            weight=config.drink_weight_weight,
            detail=f"{item.weight:.2f} units carried",
        ),
        ScoreFactor(
            name="scarcity",
            raw=-1.0 / scarcity_count,
            weight=config.drink_weight_scarcity,
            detail=f"{scarcity_count} of this type in reach",
        ),
        ScoreFactor(
            name="transfer",
            raw=-1.0 if transfer else 0.0,
            weight=config.drink_weight_transfer,
            detail="has to be moved to the main inventory" if transfer else "already in hand",
        ),
    )
    return ScoreBreakdown(factors)


# --------------------------------------------------------------------------
# portion and rationale
# --------------------------------------------------------------------------


def _choose_fraction(
    view: DrinkView,
    context: _Context,
    *,
    last_container: bool,
) -> tuple[float, bool, bool]:
    """Returns ``(fraction, portioned, capped_by_last_container_rule)``."""
    if not context.portioning_available:
        return 1.0, False, False
    config = context.config
    relief = view.thirst_relief
    needed = context.deficit / relief if relief > 0 else 1.0
    step = config.drink_fraction_step
    quantised = math.ceil(max(needed, config.min_drink_fraction) / step) * step
    fraction = min(1.0, round(quantised, 4))
    if last_container and not context.thirst_critical:
        cap = config.max_last_container_fraction
        # Reported as capped when the portion sits at the ceiling too, not only
        # when it was cut: in both cases the cap is what stopped it going up.
        return min(fraction, cap), True, fraction >= cap
    return fraction, True, False


def _rationale(
    item: ItemView,
    view: DrinkView,
    *,
    scored: ScoredCandidate,
    fraction: float,
    portioned: bool,
    capped: bool,
    context: _Context,
) -> str:
    parts = [
        f"{item.display_name} scores {scored.score:.2f} ({scored.breakdown.explain()})",
        f"thirst {context.thirst:.2f} against a target of {context.config.satisfied_thirst:.2f}",
    ]
    if portioned:
        parts.append(f"drinking {fraction:.0%} of the {view.remaining_units:.2f} units left")
    else:
        parts.append(
            f"finishing the container because partial drinking is unavailable "
            f"({CAPABILITY_DRINK_PERCENTAGE})"
        )
    if capped:
        parts.append(
            f"held to {context.config.max_last_container_fraction:.0%} because it is the last "
            "drink in reach"
        )
    if needs_transfer_to_main(context.inventory, item):
        parts.append("it has to be moved to the main inventory first")
    return "; ".join(parts)
