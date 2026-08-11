"""The two crafting actions: read a recipe, then run it exactly once.

Crafting is the first thing the agent does that cannot be undone. A door can be
closed again, a step can be walked back, a shove costs the character nothing it
keeps; two planks and a nail spent on a spear are spent, and no later
observation can put them back. That single fact shapes everything in this file:

* **``crafting.craft`` runs ONE recipe ONCE.** :data:`MAX_CRAFT_COUNT` is one,
  and the ``count`` argument exists so the number is on the wire rather than a
  default the mod picks — raising it later is a visible protocol change, not a
  quiet widening. There is no loop in the mod and no retry here: a recipe that
  could be run again is a *report*, and running it again means another command,
  through the policy, the permission gate and the safety stop a second time.
* **Nothing here chooses.** Which recipe makes the product, whether the
  character has learned it, and whether the materials may be spent are
  :mod:`pz_agent_core.policy.crafting`'s answers, re-asked in ``validate``
  against a fresh observation so a refusal happens before any command exists to
  send.
* **Success is the product, observed.** ``verify`` counts items of the recipe's
  product type in the following observation and answers only if that count
  actually rose. An ack saying the craft finished is a statement about the
  queue; a spear in the bag is a spear in the bag.

Risk follows the arguments, the way ``movement.move_to`` and
``inventory.transfer`` already do (``docs/SAFETY.md``). Spending materials the
character is carrying is P3 — it consumes and it cannot be reversed. The moment
the recipe may need a surface to run on, or a material line is only covered by
counting items sitting in a world container, the command means travelling to
somewhere and touching something that is not the character's to carry, and
:meth:`CraftingCraftAdapter.risk_for` escalates it to P4. Neither fact is
visible from the action name, so neither is published as the tool's tier.

``crafting.inspect`` is the read half and is genuinely read-only: it names one
recipe and answers from the crafting readout the observer already produces, so
it sits at P0 in :data:`~pz_agent_core.protocol.READ_ONLY_ACTIONS` beside the
other two inspects, and it names no capability for the reason they do not — a
probe over the Java recipe accessors a Lua scan can never see would report
``unsupported`` on a perfectly healthy install. "The recipe was not found" is a
finding, not a failure; "no item on the character carries a crafting readout at
all" is the failure, and that is the same line ``container.inspect`` draws
between an empty crate and a crate nobody described.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Final

from ...capabilities.probes import CRAFTING
from ...policy.crafting import (
    CRAFTING_KEY,
    CraftingDecision,
    CraftingRefusal,
    RecipeView,
    assess_recipe,
    recipe_named,
)
from ...protocol import (
    ActionName,
    Command,
    InventoryView,
    JsonDict,
    Observation,
    ReasonCode,
    RiskClass,
)
from ..adapter import Evidence, PreconditionFailed
from .common import check_args, read_count, require_inventory

__all__ = [
    "DEFAULT_CRAFT_TIMEOUT_MS",
    "DEFAULT_INSPECT_TIMEOUT_MS",
    "MAX_CRAFT_COUNT",
    "MAX_RECIPE_NAME_LEN",
    "CraftingCraftAdapter",
    "CraftingInspectAdapter",
]

#: Recipes one command may run. One, and the argument is on the wire anyway so
#: that the mod never guesses a default and so that a future batch is a protocol
#: change a reviewer sees rather than a bound that quietly moved.
MAX_CRAFT_COUNT: Final = 1

#: Longest recipe name accepted, mirroring the mod dispatcher's own string
#: bound. The value arrives from a model and is matched against game data, so it
#: is bounded and alphabet-checked here exactly as ``inventory.search`` bounds
#: its type filters.
MAX_RECIPE_NAME_LEN: Final = 64

#: A reading is answered by the next observation, so the budget is short:
#: waiting longer would not make the mod describe more recipes.
DEFAULT_INSPECT_TIMEOUT_MS: Final = 5_000
DEFAULT_INSPECT_POLL_MS: Final = 100

#: One craft, and the ceiling on the whole of it. Long enough for the slow
#: vanilla recipes, short enough that a craft which has not produced anything
#: by then is a report the mission must re-decide rather than something to keep
#: polling for.
DEFAULT_CRAFT_TIMEOUT_MS: Final = 60_000
DEFAULT_CRAFT_POLL_MS: Final = 250

#: CraftingRefusal → the protocol reason a validation refusal reports. The
#: policy's vocabulary is the mission's; the wire speaks reason codes, and this
#: table is the one place the two are joined so they cannot drift per-adapter.
#: Total over the enum — pinned by a test.
_REFUSAL_REASONS: Final[dict[CraftingRefusal, ReasonCode]] = {
    CraftingRefusal.RECIPE_UNKNOWN: ReasonCode.RECIPE_UNKNOWN,
    CraftingRefusal.MATERIALS_MISSING: ReasonCode.RECIPE_MATERIALS_MISSING,
    CraftingRefusal.MATERIALS_RESERVED: ReasonCode.RESOURCE_RESERVED,
}


def _inspect_recipe(command: Command) -> str:
    """The whole of an inspect command: one checked ``recipe`` name.

    Parsed by ``validate``, emitted by ``build_args`` and re-parsed by
    ``verify``, so the recipe that is reported on is the recipe that was asked
    about — the one-parser rule ``world.inspect`` follows for the same reason.
    """
    check_args(command, allowed=("recipe",), required=("recipe",))
    return _read_recipe_name(command)


def _read_recipe_name(command: Command) -> str:
    """Read the ``recipe`` argument, bounded and alphabet-checked.

    A recipe name is a game identifier (``MakeSpear``-shaped). Anything
    carrying a space, a colon or a wildcard is a pattern this adapter does not
    implement rather than a recipe it failed to find, and refusing it here
    keeps a model-authored string from reaching the mod as a lookup key.
    """
    raw = command.args.get("recipe")
    if not isinstance(raw, str) or not raw:
        raise PreconditionFailed(
            "recipe must be a non-empty string", reason_code=ReasonCode.INVALID_ARGUMENT
        )
    if len(raw) > MAX_RECIPE_NAME_LEN:
        raise PreconditionFailed(
            f"recipe must be at most {MAX_RECIPE_NAME_LEN} characters",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    if not all(character.isalnum() or character in "._-" for character in raw):
        raise PreconditionFailed(
            "recipe may only hold letters, digits, '.', '_' and '-'",
            reason_code=ReasonCode.INVALID_ARGUMENT,
        )
    return raw


@dataclass(frozen=True, slots=True)
class _CraftSpec:
    """``recipe`` and ``count`` — the whole of what a craft command carries."""

    recipe: str
    count: int

    @classmethod
    def parse(cls, command: Command) -> _CraftSpec:
        check_args(command, allowed=("recipe", "count"), required=("recipe",))
        return cls(
            recipe=_read_recipe_name(command),
            count=read_count(
                command, "count", default=MAX_CRAFT_COUNT, minimum=1, maximum=MAX_CRAFT_COUNT
            ),
        )

    def as_args(self) -> JsonDict:
        return {"recipe": self.recipe, "count": self.count}


def _resolve_recipe(inventory: InventoryView, name: str) -> RecipeView:
    """The observed readout for this recipe, or a refusal naming it.

    ``RECIPE_UNKNOWN`` rather than ``INVALID_ARGUMENT``: a recipe no observed
    item participates in is one this side cannot say the character knows, which
    is precisely the reason code the protocol grew for it.
    """
    recipe = recipe_named(inventory, name)
    if recipe is None:
        raise PreconditionFailed(
            f"no observed item participates in a recipe called {name}",
            reason_code=ReasonCode.RECIPE_UNKNOWN,
            evidence={"recipe": name},
        )
    return recipe


def _refuse_from(decision: CraftingDecision) -> PreconditionFailed:
    """The policy's refusal as the sidecar-side precondition failure it is.

    Raised in ``validate`` so the command never leaves this process: nothing is
    consumed, nothing is queued, and the refusal carries the policy's own token
    and the observed shortfalls as evidence.
    """
    refusal = decision.refusal
    assert refusal is not None  # only ever called on a refused decision
    return PreconditionFailed(
        f"crafting policy refused: {decision.detail}",
        reason_code=_REFUSAL_REASONS[refusal],
        evidence={
            "refusal": refusal.value,
            "recipe": None if decision.recipe is None else decision.recipe.name,
            "shortfalls": [s.as_dict() for s in decision.shortfalls],
        },
    )


def _count_of(inventory: InventoryView | None, full_type: str) -> int | None:
    """How many items of a type the observation holds, or None when unreadable."""
    if inventory is None:
        return None
    return sum(1 for item in inventory.items if item.full_type == full_type)


def _carries_readout(inventory: InventoryView) -> bool:
    """Whether any observed item carries a crafting readout at all.

    The inspect's own postcondition. An observation where nothing carries one is
    an observation that answers no crafting question — which is a failed
    reading, not the finding that a recipe is unknown.
    """
    return any(CRAFTING_KEY in item.extra for item in inventory.items)


@dataclass(frozen=True, slots=True)
class CraftingInspectAdapter:
    """``crafting.inspect``: what one recipe needs, and whether it may run.

    Read-only in the protocol's sense and in fact: the character does not move
    and touches nothing, and the answer comes off the crafting readout the
    observer already produces rather than from walking the world. It is the
    action a mission runs *before* spending anything, and the one whose finding
    a user can be shown without a permission being granted first.
    """

    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_INSPECT_POLL_MS

    name: ClassVar[ActionName] = ActionName.CRAFTING_INSPECT
    risk: ClassVar[RiskClass] = RiskClass.P0
    required_capability: ClassVar[str | None] = None

    def validate(self, command: Command, observation: Observation) -> None:
        _inspect_recipe(command)
        # Refuses with CAPABILITY_UNAVAILABLE when the mod produced no
        # inventory: a reading whose findings cannot be read back is a reading
        # whose result could only ever be asserted.
        require_inventory(observation)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return {"recipe": _inspect_recipe(command)}

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        name = _inspect_recipe(command)
        inventory = after.inventory
        if inventory is None or not _carries_readout(inventory):
            return None
        recipe = recipe_named(inventory, name)
        observed: JsonDict = {"recipe": name, "found": recipe is not None}
        if recipe is not None:
            decision = assess_recipe(after, recipe)
            observed.update(
                {
                    "known": recipe.known,
                    "product": recipe.product,
                    "needs_surface": recipe.needs_surface,
                    "runnable": not decision.refused,
                    "refusal": None if decision.refusal is None else decision.refusal.value,
                    "shortfalls": [s.as_dict() for s in decision.shortfalls],
                }
            )
        return Evidence(kind="recipe_described", observation_seq=after.seq, observed=observed)


@dataclass(frozen=True, slots=True)
class CraftingCraftAdapter:
    """``crafting.craft``: run one known recipe once, prove it by the product.

    Terminal after the one run. There is no "keep crafting until", no second
    attempt behind this command's id, and no fallback to a different recipe
    when this one turns out to be short — a mission that wants another run
    issues another command, and every gate between the user and the materials
    gets its turn again in between.
    """

    timeout_ms: int = DEFAULT_CRAFT_TIMEOUT_MS
    poll_interval_ms: int = DEFAULT_CRAFT_POLL_MS

    name: ClassVar[ActionName] = ActionName.CRAFTING_CRAFT
    risk: ClassVar[RiskClass] = RiskClass.P3
    required_capability: ClassVar[str | None] = CRAFTING

    def validate(self, command: Command, observation: Observation) -> None:
        spec = _CraftSpec.parse(command)
        inventory = require_inventory(observation)
        recipe = _resolve_recipe(inventory, spec.recipe)
        decision = assess_recipe(observation, recipe)
        if decision.refused:
            raise _refuse_from(decision)

    def build_args(self, command: Command, observation: Observation) -> JsonDict:
        return _CraftSpec.parse(command).as_args()

    def risk_for(self, command: Command, observation: Observation) -> RiskClass:
        """The tier this particular craft needs, never below :attr:`risk`.

        Spending what the character carries is P3. A recipe that may need a
        surface to run on, or one whose materials are only covered by counting
        items in a world container, means travel and world contact — the two
        things the permission ladder reserves its top tier for — so it becomes
        P4 and can never be taken on the agent's own initiative.
        """
        spec = _CraftSpec.parse(command)
        inventory = require_inventory(observation)
        recipe = _resolve_recipe(inventory, spec.recipe)
        decision = assess_recipe(observation, recipe)
        return RiskClass.P4 if decision.needs_travel else self.risk

    def verify(self, command: Command, before: Observation, after: Observation) -> Evidence | None:
        spec = _CraftSpec.parse(command)
        # The product is read from the *before* observation on purpose: a
        # successful craft consumes the materials, and with them the readout
        # entry that named what the recipe makes.
        recipe = None if before.inventory is None else recipe_named(before.inventory, spec.recipe)
        if recipe is None:
            return None
        count_before = _count_of(before.inventory, recipe.product)
        count_after = _count_of(after.inventory, recipe.product)
        if count_before is None or count_after is None or count_after <= count_before:
            # Not yet, or not at all. "The mod said it finished" is not a
            # product in the bag, and an unreadable inventory on either side
            # leaves the postcondition unproven rather than assumed.
            return None
        return Evidence(
            kind="product_in_inventory",
            observation_seq=after.seq,
            observed={
                "recipe": recipe.name,
                "product": recipe.product,
                "count_before": count_before,
                "count_after": count_after,
                "runs": spec.count,
            },
        )
