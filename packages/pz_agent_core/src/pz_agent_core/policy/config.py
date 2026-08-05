"""Every tunable number the selection policies consult.

The food, drink and literature policies read thresholds and weights from here
and nowhere else, so the whole decision surface is reviewable on one screen and
retunable without touching the logic that uses it.

Values that ``docs/blueprint/17_DECISION_TABLES.md`` §17.1 names are copied from
that table; the scoring weights are the starting balance of §17.3. They are
starting points for tests, not proven game balance — §17 says so explicitly.

The dataclass is frozen because a selection has to be reproducible: a policy
call must not be able to mutate the configuration it was handed, or a later
call with the same inventory could answer differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CAPABILITY_DRINK_CARRIED",
    "CAPABILITY_DRINK_PERCENTAGE",
    "CAPABILITY_DRINK_WORLD_SOURCE",
    "CAPABILITY_EAT_PERCENTAGE",
    "CAPABILITY_READ_LITERATURE",
    "DEFAULT_POLICY_CONFIG",
    "PolicyConfig",
]

#: Probe names from ``docs/blueprint/03_GAME_STATE_AND_IPC.md`` §3.8.
CAPABILITY_EAT_PERCENTAGE: Final = "eat_percentage"
CAPABILITY_DRINK_CARRIED: Final = "drink_carried"
CAPABILITY_DRINK_WORLD_SOURCE: Final = "drink_world_source"
CAPABILITY_READ_LITERATURE: Final = "read_literature"

#: Partial consumption of a fluid container is a different API surface from
#: drinking one at all, so it gets its own probe rather than being inferred
#: from ``drink_carried``. Until something probes it the policy treats it as
#: unavailable, which is what makes the last-container rule refuse instead of
#: quietly emptying the bottle.
CAPABILITY_DRINK_PERCENTAGE: Final = "drink_percentage"


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Thresholds and weights shared by the three selection policies.

    Weights are always non-negative; the sign of a scoring factor lives in the
    factor's raw value. That convention keeps "is this a bonus or a penalty?"
    readable in the breakdown instead of hidden in a minus sign in the config.
    """

    # --- need thresholds (blueprint §17.1) ---------------------------------
    #: Hunger at or above this is the CRITICAL_HUNGER state: reserves open up.
    critical_hunger: float = 0.70
    #: Hunger that makes eating worth doing at all.
    hunger_trigger: float = 0.35
    #: Hunger the policy aims to leave the character at; the exit threshold of
    #: §17.1. Everything above it is the deficit a portion has to cover.
    satisfied_hunger: float = 0.15

    critical_thirst: float = 0.70
    thirst_trigger: float = 0.35
    satisfied_thirst: float = 0.15

    #: Boredom moodle level at which reading for mood is justified (§17.1).
    boredom_moodle_trigger: int = 3

    # --- food safety filters ----------------------------------------------
    #: Eating raw food that the game flags as risky is a policy decision, not a
    #: taste: it is off by default because the failure mode is food poisoning.
    allow_raw: bool = False
    allow_rotten: bool = False
    #: Rot fraction above which an item counts as rotten even when the game did
    #: not label it so. 0.0 is fresh, 1.0 is fully rotten.
    max_rot_progress: float = 0.60
    #: Burn fraction above which the item is refused.
    max_burn_progress: float = 0.25
    #: Frozen food cannot be eaten until it thaws; the policy does not model
    #: thawing, so it refuses rather than queueing an action that cannot run.
    allow_frozen: bool = False
    #: Items that are inedible until cooked. Cooking is a separate skill, so
    #: selecting one would produce a plan this policy cannot honour.
    allow_required_cooking: bool = False

    # --- drink safety filters ---------------------------------------------
    allow_tainted_water: bool = False
    allow_alcohol_for_thirst: bool = False
    #: Remaining volume (in the container's own units) at or below which a
    #: container counts as empty.
    min_remaining_units: float = 0.05
    #: Most of the last usable container the policy will drink when thirst is
    #: not yet critical.
    max_last_container_fraction: float = 0.5

    # --- reserves ----------------------------------------------------------
    #: Tags marking an item the user set aside. Never consumed automatically.
    user_reserve_tags: frozenset[str] = frozenset({"reserved", "keep", "do_not_use"})
    #: Tags marking a strategic reserve: consumable, but only once the matching
    #: need is critical.
    strategic_reserve_tags: frozenset[str] = frozenset({"strategic_reserve", "emergency"})
    #: The in-game favourite flag is the most common way a player says "hands
    #: off", so it is honoured like an explicit reserve tag.
    treat_favorite_as_reserved: bool = True

    # --- reachability ------------------------------------------------------
    #: World containers require travel, which is the autonomy layer's decision
    #: and bounded by its radius cap, not this module's. Selection therefore
    #: stays on the player by default.
    allow_world_containers: bool = False

    # --- portions ----------------------------------------------------------
    #: Smallest portion worth queueing an action for.
    min_eat_fraction: float = 0.25
    #: Portions are quantised so the same deficit always yields the same
    #: fraction, and so the value is one a human can sanity-check.
    eat_fraction_step: float = 0.25
    min_drink_fraction: float = 0.25
    drink_fraction_step: float = 0.25

    # --- normalisation references -----------------------------------------
    #: Divisors that map raw game numbers onto roughly 0..1 so weights are
    #: comparable with each other.
    calorie_reference: float = 1000.0
    unhappiness_reference: float = 20.0
    boredom_reference: float = 20.0
    thirst_change_reference: float = 0.5
    weight_reference: float = 2.0
    alcohol_reference: float = 1.0
    recipe_reference: float = 3.0

    # --- food scoring weights (blueprint §17.3) ----------------------------
    food_weight_hunger_fit: float = 4.0
    food_weight_urgency: float = 1.0
    food_weight_calories: float = 0.2
    food_weight_freshness: float = 1.0
    food_weight_unhappiness: float = 0.5
    food_weight_boredom: float = 0.2
    food_weight_thirst_side_effect: float = 0.5
    food_weight_weight: float = 0.2
    food_weight_scarcity: float = 0.6
    food_weight_cooking: float = 0.4
    food_weight_portions: float = 0.3
    food_weight_waste: float = 1.5

    # --- drink scoring weights (blueprint §17.4) ---------------------------
    drink_weight_thirst_fit: float = 4.0
    drink_weight_water: float = 1.5
    drink_weight_remaining: float = 0.5
    drink_weight_side_effects: float = 1.0
    drink_weight_weight: float = 0.2
    drink_weight_scarcity: float = 0.6
    drink_weight_transfer: float = 0.3

    # --- literature scoring weights (blueprint §17.5) ----------------------
    literature_weight_goal_match: float = 4.0
    literature_weight_progress: float = 1.0
    literature_weight_boredom_relief: float = 1.0
    literature_weight_recipes: float = 0.8
    literature_weight_level_fit: float = 0.6
    literature_weight_weight: float = 0.2
    literature_weight_transfer: float = 0.3

    # --- literature filters ------------------------------------------------
    #: Re-reading a finished book yields nothing in game; allowing it is only
    #: useful for a user who asked for a specific item by name.
    allow_reread: bool = False

    #: Longest rejection list a selection reports. Bounded because a refusal is
    #: rendered to a user and written to a log, and an inventory can be large.
    max_reported_rejections: int = 64

    def __post_init__(self) -> None:
        self._check_need_ladder(
            "hunger", self.satisfied_hunger, self.hunger_trigger, self.critical_hunger
        )
        self._check_need_ladder(
            "thirst", self.satisfied_thirst, self.thirst_trigger, self.critical_thirst
        )
        self._check_fraction("min_eat_fraction", self.min_eat_fraction)
        self._check_fraction("eat_fraction_step", self.eat_fraction_step)
        self._check_fraction("min_drink_fraction", self.min_drink_fraction)
        self._check_fraction("drink_fraction_step", self.drink_fraction_step)
        self._check_fraction("max_last_container_fraction", self.max_last_container_fraction)
        self._check_fraction("max_rot_progress", self.max_rot_progress)
        self._check_fraction("max_burn_progress", self.max_burn_progress)
        for name, value in self._references().items():
            if value <= 0.0:
                raise ValueError(f"{name} must be positive, got {value}")
        for name, value in self._weights().items():
            if value < 0.0:
                raise ValueError(f"{name} must be non-negative, got {value}")
        if self.max_reported_rejections < 1:
            raise ValueError(
                f"max_reported_rejections must be at least 1, got {self.max_reported_rejections}"
            )
        if self.boredom_moodle_trigger < 0:
            raise ValueError(
                f"boredom_moodle_trigger must be non-negative, got {self.boredom_moodle_trigger}"
            )

    @staticmethod
    def _check_need_ladder(need: str, satisfied: float, trigger: float, critical: float) -> None:
        if not 0.0 <= satisfied < trigger < critical <= 1.0:
            raise ValueError(
                f"{need} thresholds must satisfy 0 <= satisfied < trigger < critical <= 1, "
                f"got {satisfied}, {trigger}, {critical}"
            )

    @staticmethod
    def _check_fraction(name: str, value: float) -> None:
        if not 0.0 < value <= 1.0:
            raise ValueError(f"{name} must be within (0, 1], got {value}")

    def _references(self) -> dict[str, float]:
        return {
            "calorie_reference": self.calorie_reference,
            "unhappiness_reference": self.unhappiness_reference,
            "boredom_reference": self.boredom_reference,
            "thirst_change_reference": self.thirst_change_reference,
            "weight_reference": self.weight_reference,
            "alcohol_reference": self.alcohol_reference,
            "recipe_reference": self.recipe_reference,
        }

    def _weights(self) -> dict[str, float]:
        return {
            "food_weight_hunger_fit": self.food_weight_hunger_fit,
            "food_weight_urgency": self.food_weight_urgency,
            "food_weight_calories": self.food_weight_calories,
            "food_weight_freshness": self.food_weight_freshness,
            "food_weight_unhappiness": self.food_weight_unhappiness,
            "food_weight_boredom": self.food_weight_boredom,
            "food_weight_thirst_side_effect": self.food_weight_thirst_side_effect,
            "food_weight_weight": self.food_weight_weight,
            "food_weight_scarcity": self.food_weight_scarcity,
            "food_weight_cooking": self.food_weight_cooking,
            "food_weight_portions": self.food_weight_portions,
            "food_weight_waste": self.food_weight_waste,
            "drink_weight_thirst_fit": self.drink_weight_thirst_fit,
            "drink_weight_water": self.drink_weight_water,
            "drink_weight_remaining": self.drink_weight_remaining,
            "drink_weight_side_effects": self.drink_weight_side_effects,
            "drink_weight_weight": self.drink_weight_weight,
            "drink_weight_scarcity": self.drink_weight_scarcity,
            "drink_weight_transfer": self.drink_weight_transfer,
            "literature_weight_goal_match": self.literature_weight_goal_match,
            "literature_weight_progress": self.literature_weight_progress,
            "literature_weight_boredom_relief": self.literature_weight_boredom_relief,
            "literature_weight_recipes": self.literature_weight_recipes,
            "literature_weight_level_fit": self.literature_weight_level_fit,
            "literature_weight_weight": self.literature_weight_weight,
            "literature_weight_transfer": self.literature_weight_transfer,
        }

    def hunger_deficit(self, hunger: float) -> float:
        """How much hunger a meal should remove to reach the satisfied level."""
        return max(0.0, hunger - self.satisfied_hunger)

    def thirst_deficit(self, thirst: float) -> float:
        """How much thirst a drink should remove to reach the satisfied level."""
        return max(0.0, thirst - self.satisfied_thirst)

    def is_hunger_critical(self, hunger: float) -> bool:
        return hunger >= self.critical_hunger

    def is_thirst_critical(self, thirst: float) -> bool:
        return thirst >= self.critical_thirst


#: Shared default so callers that have no user configuration still get the
#: documented behaviour rather than an ad-hoc one.
DEFAULT_POLICY_CONFIG: Final = PolicyConfig()
