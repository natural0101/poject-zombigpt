"""Which recipe to run, and whether it may be run at all, decided the boring way.

Crafting is the first action this project ships that *destroys* things. A swing
can be walked back by walking away; two planks and a nail spent on a spear are
gone, and the character is poorer whether or not the spear was what the user
wanted. So the decision is deterministic code with unit tests, exactly as food
and literature selection are (AGENTS.md, "Determinism where it counts"): the
model may say "make a spear", it never picks which recipe spends which planks.

The whole decision surface is one observation. There is no recipe database in
this package and there will not be one — a table written here would be a second,
untested copy of the game's, drifting the moment a mod adds a recipe. What the
policy reads is the crafting readout the mod publishes per item, described in
:class:`RecipeView`: every recipe an observed item participates in, whether the
character has learned it, what one run consumes, and whether it needs a surface
to run on. An item that participates in nothing carries no readout at all.

Three honesty rules, in the tri-state discipline the door adapters established
and the combat policy restated:

* **Unreadable is never the good reading.** ``known`` absent means the build did
  not report whether the character has learned the recipe, which is not the same
  as having learned it: the recipe refuses as unknown. ``needs_surface`` absent
  means the reader could not tell whether the craft needs a workbench, so the
  answer is "assume it does", which is the reading that costs a permission tier
  rather than the one that spends materials at the wrong place.
* **A reserved item outranks a craving.** :func:`~pz_agent_core.policy.selection.is_user_reserved`
  is honoured here exactly as the food policy honours it — never overridden by
  need, because "make me a spear" is not permission to break the axe the player
  marked as kept. When reserves are the *only* reason a recipe is short, the
  refusal says so with its own token, so the user can answer the real question
  ("release the reserve?") instead of being told the materials do not exist.
* **What is short, and how much.** A refusal carries a
  :class:`Shortfall` per material — needed, free, reserved — because "you cannot
  make that" is not an answer anyone can act on.

Bounded, like everything else: :data:`MAX_CANDIDATE_RECIPES` recipes are
considered for one product and :data:`MAX_RECIPES_PER_ITEM` readout entries are
read off any one item, so neither a mod with a thousand recipes nor a malformed
readout can turn a selection into an unbounded walk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from ..protocol import (
    ON_PERSON_CONTAINERS,
    InventoryView,
    ItemView,
    JsonDict,
    Observation,
)
from .config import DEFAULT_POLICY_CONFIG, PolicyConfig
from .selection import is_user_reserved, read_int, read_str

__all__ = [
    "CRAFTING_KEY",
    "MAX_CANDIDATE_RECIPES",
    "MAX_MATERIALS_PER_RECIPE",
    "MAX_RECIPES_PER_ITEM",
    "MAX_REPORTED_SHORTFALLS",
    "CraftingDecision",
    "CraftingRefusal",
    "CraftingVerdict",
    "MaterialNeed",
    "MaterialTally",
    "RecipeView",
    "Shortfall",
    "assess_craft",
    "assess_recipe",
    "observed_recipes",
    "recipe_named",
    "recipes_for_product",
]

#: Where the crafting readout rides on an item. ``ItemView`` types ``food``,
#: ``literature`` and ``fluid`` and nothing else, so the crafting block travels
#: under :attr:`~pz_agent_core.protocol.ItemView.extra` — the same place the
#: combat policy reads a weapon's condition from, and for the same reason: the
#: typed views have not grown the domain yet, and inventing one on the wire
#: would be a protocol change this wave does not make.
CRAFTING_KEY: Final = "crafting"

#: How many recipes for one product are ever weighed against each other. Eight
#: is far past what any vanilla product has and small enough that the ranking
#: stays a decision a reviewer can re-do by hand.
MAX_CANDIDATE_RECIPES: Final = 8

#: How many readout entries are read off a single item. The readout is data the
#: game produced, so it is bounded on arrival rather than trusted to be short.
MAX_RECIPES_PER_ITEM: Final = 16

#: How many materials one recipe may name before this side stops modelling it.
#: A recipe past this bound is not refused as unmakeable — it is not treated as
#: a candidate at all, because a partially read requirement list would
#: understate what the craft is about to spend.
MAX_MATERIALS_PER_RECIPE: Final = 8

#: Shortfalls carried in a refusal. Bounded like every other diagnostic that
#: reaches a user; the tally count travels beside it so a trimmed list never
#: reads as a complete one.
MAX_REPORTED_SHORTFALLS: Final = 6


class CraftingVerdict(StrEnum):
    """The two answers, closed. A craft either may run now or may not."""

    CRAFT = "craft"
    REFUSE = "refuse"


class CraftingRefusal(StrEnum):
    """Why a craft is refused.

    Three tokens, one per fact, shaped like
    :class:`~pz_agent_core.combat.policy.CombatRefusal`: they reach reports and
    goal detail lines, so they are constants here and nowhere else. The
    adapters map each onto the protocol reason code that already exists for it
    — ``RECIPE_UNKNOWN``, ``RECIPE_MATERIALS_MISSING``, ``RESOURCE_RESERVED``.
    """

    RECIPE_UNKNOWN = "recipe_unknown"
    MATERIALS_MISSING = "materials_missing"
    MATERIALS_RESERVED = "materials_reserved"


@dataclass(frozen=True, slots=True)
class MaterialNeed:
    """One line of a recipe's requirement list: a type, and how many of it."""

    full_type: str
    count: int

    def __post_init__(self) -> None:
        if not self.full_type:
            raise ValueError("a material must name the item type it consumes")
        if self.count < 1:
            raise ValueError(f"a material must consume at least one item, got {self.count}")


@dataclass(frozen=True, slots=True)
class MaterialTally:
    """What the observation holds against one requirement line.

    The three counts are kept apart rather than summed because they answer
    different questions. ``on_person`` is what can be spent without going
    anywhere; ``in_world`` is what is reachable but needs travel, which is the
    fact that escalates the craft's risk tier; ``reserved`` is what the user
    said is not available, which is a refusal the user can lift.
    """

    need: MaterialNeed
    on_person: int
    in_world: int
    reserved: int

    @property
    def free(self) -> int:
        """Items that may be spent — reachable, and not the user's to keep."""
        return self.on_person + self.in_world

    @property
    def satisfied(self) -> bool:
        return self.free >= self.need.count

    @property
    def satisfied_with_reserves(self) -> bool:
        """Whether releasing the user's reserves alone would cover this line."""
        return self.free + self.reserved >= self.need.count

    @property
    def needs_travel(self) -> bool:
        """True when what is on the character is not enough on its own."""
        return self.on_person < self.need.count

    def as_shortfall(self) -> Shortfall:
        return Shortfall(
            full_type=self.need.full_type,
            needed=self.need.count,
            free=self.free,
            reserved=self.reserved,
        )


@dataclass(frozen=True, slots=True)
class Shortfall:
    """One material the character does not have enough of, as it is reported."""

    full_type: str
    needed: int
    free: int
    reserved: int

    def describe(self) -> str:
        """One line for a user-facing explanation."""
        held = f"{self.free} of {self.needed} {self.full_type}"
        if self.reserved:
            return f"{held} (a further {self.reserved} are reserved)"
        return held

    def as_dict(self) -> JsonDict:
        return {
            "full_type": self.full_type,
            "needed": self.needed,
            "free": self.free,
            "reserved": self.reserved,
        }


@dataclass(frozen=True, slots=True)
class RecipeView:
    """Typed reading of one entry of an item's crafting readout.

    The wire shape, which the mod's observer produces and this side never
    invents::

        item.extra["crafting"] = {
            "recipe_count": 3,
            "known_recipe_count": 1,
            "recipes": [
                {
                    "name": "MakeSpear",
                    "product": "Base.SpearCrude",
                    "display_name": "Make Crude Spear",
                    "known": true,
                    "needs_surface": false,
                    "materials": [{"full_type": "Base.TreeBranch", "count": 1}]
                }
            ]
        }

    ``known`` and ``needs_surface`` are tri-state: absent means the build did
    not report the fact, which is never read as the convenient answer. Every
    other field is required, and :meth:`from_payload` returns ``None`` rather
    than filling a default — a recipe that cannot say what it makes has no
    postcondition, and one whose requirement list cannot be read whole would
    understate what it is about to spend.
    """

    name: str
    product: str
    display_name: str
    known: bool | None
    needs_surface: bool | None
    materials: tuple[MaterialNeed, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RecipeView | None:
        name = read_str(payload, "name")
        product = read_str(payload, "product")
        if not name or not product:
            return None
        materials = _read_materials(payload.get("materials"))
        if materials is None:
            return None
        return cls(
            name=name,
            product=product,
            display_name=read_str(payload, "display_name") or name,
            known=_read_tristate(payload, "known"),
            needs_surface=_read_tristate(payload, "needs_surface"),
            materials=materials,
        )

    @property
    def is_known(self) -> bool:
        """True only when the build said so. Unreadable is not learned."""
        return self.known is True

    @property
    def may_need_surface(self) -> bool:
        """True unless the build said the craft needs no surface.

        Absent falls to True on purpose: the cost of assuming a surface is
        needed is one permission tier, and the cost of assuming none is a
        craft queued where it cannot run.
        """
        return self.needs_surface is not False

    def as_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "product": self.product,
            "display_name": self.display_name,
            "known": self.known,
            "needs_surface": self.needs_surface,
            "materials": [{"full_type": m.full_type, "count": m.count} for m in self.materials],
        }


@dataclass(frozen=True, slots=True)
class CraftingDecision:
    """The verdict plus the observed facts that produced it.

    Shaped like :class:`~pz_agent_core.combat.policy.EngagementDecision`: a
    refusal has to be able to say why without recomputing anything, and the
    adapter's precondition failure reads its evidence straight off the decision
    it acted on.
    """

    verdict: CraftingVerdict
    refusal: CraftingRefusal | None
    detail: str
    #: The recipe the decision is about, or None when nothing was observed for
    #: the requested product at all.
    recipe: RecipeView | None
    tallies: tuple[MaterialTally, ...]
    #: Recipes weighed for this request, and whether the cap hid any of them.
    candidates_considered: int = 0
    candidates_truncated: bool = False

    def __post_init__(self) -> None:
        if (self.verdict is CraftingVerdict.REFUSE) != (self.refusal is not None):
            raise ValueError("a refusal carries its token and only a refusal does")
        if not self.detail.strip():
            raise ValueError("a decision must say what it saw")

    @property
    def refused(self) -> bool:
        return self.verdict is CraftingVerdict.REFUSE

    @property
    def shortfalls(self) -> tuple[Shortfall, ...]:
        """Every requirement line the observation does not cover, bounded."""
        short = [t.as_shortfall() for t in self.tallies if not t.satisfied]
        return tuple(short[:MAX_REPORTED_SHORTFALLS])

    @property
    def needs_travel(self) -> bool:
        """Whether running this recipe means leaving the character's own bags.

        True when the craft may need a surface to run on, or when any material
        line is only covered by counting items in a world container. Both are
        the same fact for the permission layer — the character has to go
        somewhere and touch something that is not theirs to carry — and the
        craft adapter escalates its risk tier on exactly this.
        """
        if self.recipe is not None and self.recipe.may_need_surface:
            return True
        return any(t.needs_travel for t in self.tallies)

    def explain(self) -> str:
        return self.detail

    def as_dict(self) -> JsonDict:
        return {
            "verdict": self.verdict.value,
            "refusal": None if self.refusal is None else self.refusal.value,
            "detail": self.detail,
            "recipe": None if self.recipe is None else self.recipe.as_dict(),
            "shortfalls": [s.as_dict() for s in self.shortfalls],
            "needs_travel": self.needs_travel,
            "candidates_considered": self.candidates_considered,
            "candidates_truncated": self.candidates_truncated,
        }


# --------------------------------------------------------------------------
# reading the observation
# --------------------------------------------------------------------------
# Everything below is total over the mod's untyped readout: it is data from the
# game, which the working agreement classes as untrusted, so a missing key, a
# null or a value of the wrong type drops the entry rather than raising.


def _read_tristate(payload: Mapping[str, Any], key: str) -> bool | None:
    """A flag that may also be absent, kept absent.

    ``read_bool`` would fold "not reported" into ``False``, which is the whole
    mistake this function exists to avoid: "the build did not say whether the
    character knows this recipe" and "the character does not know it" authorise
    different sentences, and only one of them is honest here.
    """
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return None


def _read_materials(value: Any) -> tuple[MaterialNeed, ...] | None:
    """One recipe's requirement list, or None when it cannot be read whole.

    All-or-nothing on purpose. A list that quietly dropped the entry it could
    not parse would tell the caller the craft is affordable while hiding what
    it spends, which is the one lie this module cannot afford to tell.
    """
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if len(value) > MAX_MATERIALS_PER_RECIPE:
        return None
    needs: list[MaterialNeed] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            return None
        full_type = read_str(entry, "full_type")
        count = read_int(entry, "count", 1)
        if not full_type or count < 1:
            return None
        needs.append(MaterialNeed(full_type=full_type, count=count))
    return tuple(needs)


def _recipes_on(item: ItemView) -> tuple[RecipeView, ...]:
    """The readable recipe entries one item carries, bounded per item."""
    block = item.extra.get(CRAFTING_KEY)
    if not isinstance(block, Mapping):
        return ()
    entries = block.get("recipes")
    if not isinstance(entries, (list, tuple)):
        return ()
    found: list[RecipeView] = []
    for entry in entries[:MAX_RECIPES_PER_ITEM]:
        if not isinstance(entry, Mapping):
            continue
        recipe = RecipeView.from_payload(entry)
        if recipe is not None:
            found.append(recipe)
    return tuple(found)


def observed_recipes(inventory: InventoryView) -> tuple[RecipeView, ...]:
    """Every readable recipe the observation names, deduplicated, name-ordered.

    The same recipe is reported by every material it consumes, so the first
    reading of a name wins and the rest are dropped; sorting by name afterwards
    is what makes the result independent of the order the mod happened to
    enumerate the inventory in.
    """
    seen: dict[str, RecipeView] = {}
    for item in inventory.items:
        for recipe in _recipes_on(item):
            seen.setdefault(recipe.name, recipe)
    return tuple(sorted(seen.values(), key=lambda r: r.name))


def recipe_named(inventory: InventoryView, name: str) -> RecipeView | None:
    """The one recipe the observation reports under this exact name."""
    return next((r for r in observed_recipes(inventory) if r.name == name), None)


def recipes_for_product(inventory: InventoryView, product: str) -> tuple[RecipeView, ...]:
    """Recipes that make *product*, best candidate first.

    The order is the whole of the preference and it is total: recipes the
    character is observed to know come before ones it may not, recipes that
    need no surface come before ones that may, then the shorter requirement
    list, then the name. Nothing here consults the inventory's order, so two
    runs over the same observation always weigh the same recipe first.
    """
    matching = [r for r in observed_recipes(inventory) if r.product == product]
    matching.sort(
        key=lambda r: (
            0 if r.is_known else 1,
            1 if r.may_need_surface else 0,
            len(r.materials),
            r.name,
        )
    )
    return tuple(matching)


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------


def _reachable(inventory: InventoryView, item: ItemView) -> bool:
    """Whether the policy can honestly promise to reach this item.

    An item whose container the mod did not report, or reported as
    inaccessible, is not counted towards anything: the food policy refuses such
    an item outright, and a material this side cannot locate must not make a
    craft look affordable.
    """
    container = inventory.container(item.container_ref)
    return container is not None and container.accessible


def _tally(inventory: InventoryView, need: MaterialNeed, config: PolicyConfig) -> MaterialTally:
    """Count what the observation holds against one requirement line."""
    on_person = 0
    in_world = 0
    reserved = 0
    for item in inventory.items:
        if item.full_type != need.full_type or not _reachable(inventory, item):
            continue
        if is_user_reserved(item, config):
            reserved += 1
            continue
        container = inventory.container(item.container_ref)
        if container is not None and container.kind in ON_PERSON_CONTAINERS:
            on_person += 1
        else:
            in_world += 1
    return MaterialTally(need=need, on_person=on_person, in_world=in_world, reserved=reserved)


def _tallies_for(
    inventory: InventoryView, recipe: RecipeView, config: PolicyConfig
) -> tuple[MaterialTally, ...]:
    return tuple(_tally(inventory, need, config) for need in recipe.materials)


def assess_recipe(
    observation: Observation,
    recipe: RecipeView,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
) -> CraftingDecision:
    """One named recipe, one observation, one deterministic answer.

    Total: every input shape yields a decision, never an exception. The gate
    order is fixed and load-bearing — knowledge first, then materials, then
    whose materials they are — so the same picture always produces the same
    refusal however many gates it fails, and the refusal a user sees is the
    most fundamental problem rather than an arbitrary one of several.
    """
    inventory = observation.inventory
    if inventory is None:
        # No inventory tier is no materials and no proof: a craft whose
        # postcondition could not be read afterwards must not be assessed as
        # runnable now. The adapters refuse this earlier with
        # CAPABILITY_UNAVAILABLE; the policy stays total rather than assuming
        # a caller reached it in the right order.
        return _refuse(
            CraftingRefusal.MATERIALS_MISSING,
            "the mod reported no inventory, so no material can be counted",
            recipe=recipe,
            tallies=(),
        )
    if not recipe.is_known:
        cause = (
            "the build did not report whether the character has learned it, "
            "and unreadable is never learned"
            if recipe.known is None
            else "the character has not learned it"
        )
        return _refuse(
            CraftingRefusal.RECIPE_UNKNOWN,
            f"recipe {recipe.name}: {cause}",
            recipe=recipe,
            tallies=(),
        )

    tallies = _tallies_for(inventory, recipe, config)
    short = [t for t in tallies if not t.satisfied]
    if short:
        listed = "; ".join(t.as_shortfall().describe() for t in short[:MAX_REPORTED_SHORTFALLS])
        if all(t.satisfied_with_reserves for t in short):
            return _refuse(
                CraftingRefusal.MATERIALS_RESERVED,
                f"recipe {recipe.name} is short only because you reserved what it needs: {listed}",
                recipe=recipe,
                tallies=tallies,
            )
        return _refuse(
            CraftingRefusal.MATERIALS_MISSING,
            f"recipe {recipe.name} is short of {listed}",
            recipe=recipe,
            tallies=tallies,
        )

    return CraftingDecision(
        verdict=CraftingVerdict.CRAFT,
        refusal=None,
        detail=(
            f"recipe {recipe.name} is known and every material is held: "
            f"{len(tallies)} requirement lines, one run"
        ),
        recipe=recipe,
        tallies=tallies,
    )


def assess_craft(
    observation: Observation,
    product: str,
    config: PolicyConfig = DEFAULT_POLICY_CONFIG,
) -> CraftingDecision:
    """Pick the recipe that makes *product*, or refuse with the reason why.

    Candidates are weighed in :func:`recipes_for_product`'s order and the first
    that may run wins. When none may, the refusal reported is the *best*
    candidate's — the ranking puts known recipes first precisely so that
    "you are two planks short" outranks "you do not know some other way to make
    this", which is the more actionable of the two sentences.
    """
    inventory = observation.inventory
    candidates = () if inventory is None else recipes_for_product(inventory, product)
    truncated = len(candidates) > MAX_CANDIDATE_RECIPES
    considered = candidates[:MAX_CANDIDATE_RECIPES]
    if not considered:
        return _refuse(
            CraftingRefusal.RECIPE_UNKNOWN,
            f"nothing observed on the character participates in a recipe that makes {product}",
            recipe=None,
            tallies=(),
            candidates_considered=0,
            candidates_truncated=truncated,
        )

    first: CraftingDecision | None = None
    for recipe in considered:
        decision = assess_recipe(observation, recipe, config)
        decision = _with_census(decision, len(considered), truncated)
        if not decision.refused:
            return decision
        if first is None:
            first = decision
    assert first is not None  # `considered` is non-empty, so the loop ran
    return first


def _with_census(decision: CraftingDecision, considered: int, truncated: bool) -> CraftingDecision:
    return CraftingDecision(
        verdict=decision.verdict,
        refusal=decision.refusal,
        detail=decision.detail,
        recipe=decision.recipe,
        tallies=decision.tallies,
        candidates_considered=considered,
        candidates_truncated=truncated,
    )


def _refuse(
    refusal: CraftingRefusal,
    detail: str,
    *,
    recipe: RecipeView | None,
    tallies: Sequence[MaterialTally],
    candidates_considered: int = 0,
    candidates_truncated: bool = False,
) -> CraftingDecision:
    return CraftingDecision(
        verdict=CraftingVerdict.REFUSE,
        refusal=refusal,
        detail=detail,
        recipe=recipe,
        tallies=tuple(tallies),
        candidates_considered=candidates_considered,
        candidates_truncated=candidates_truncated,
    )
