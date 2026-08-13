"""``craft_item``: the deterministic craft mission — one run, one command.

The goal channel's crafting wave, and the first mission in this package whose
work cannot be walked back. A journey can be re-walked, a bandage re-applied, a
swing costs the character nothing it keeps; two planks and a nail spent on a
spear are spent, and no later observation puts them back. The mission is a state
machine over observations, shaped like
:class:`~pz_agent_cli.combat_mission.EngageZombieMission` so the wrapper in
:mod:`pz_agent_cli.navigation_planner` drives it through the same seam with the
same frozen move vocabulary imported from :mod:`pz_agent_cli.loot_mission`:
every ``crafting.craft`` travels the loop's action channel as a
:class:`~pz_agent_cli.loot_mission.MissionStep` — a queued craft is not a
product in the bag, so no step's own success is the goal's postcondition — and
the goal ends through the queue, or through one
:class:`~pz_agent_cli.loot_mission.MissionProbe` when the product turned up
without a command of this mission's ever succeeding.

The safety shape, each clause pinned by a test:

* **The policy decides, before every run.** Each pass through
  :meth:`CraftItemMission.next_step` re-reads the observation and asks
  :func:`~pz_agent_core.policy.crafting.assess_craft` again, so which recipe
  spends which materials is a deterministic answer about the world as it is
  now — never a plan made once and replayed against a bag that has changed. The
  mission itself never ranks a recipe, never counts a material and never
  decides that a reserve may be broken.
* **One item, one command, no loop.** Every run is its own bounded
  ``crafting.craft`` with ``count`` pinned at the adapter's own
  :data:`~pz_agent_core.actions.adapters.crafting.MAX_CRAFT_COUNT`, submitted
  through the loop's channel and re-decided from a fresh observation before the
  next one — so ``safety.stop``, the reflex guard and the user interrupt land
  *between* runs structurally, because between runs nothing is running. At most
  :data:`MAX_CRAFT_ATTEMPTS` attempts and :data:`MAX_RECIPES_TRIED` distinct
  recipes; a run that could happen again is a report, never a retry.
* **A failed craft retires its recipe.** The command that failed may or may not
  have spent what it took; issuing it again is a retry of an irreversible
  action, which is the one thing this rung refuses to do on its own. The
  mission asks the policy again, and a policy that answers with the same recipe
  is answering that nothing has changed — which ends the mission honestly
  rather than spending the materials on a second guess.
* **A shortfall is a report, not an errand.** A known recipe short of materials
  ends the goal with ``RECIPE_MATERIALS_MISSING`` and the shortfall named. The
  mission does *not* go looting for what is missing: chaining a craft onto a
  loot is a decision about the user's time and the character's safety, and
  making it here would be an excursion nobody asked for.
* **Success is the product, observed.** The mission completes only when the
  newest observation holds :attr:`CraftItemMission.wanted` more of the product
  than the observation it started from — the count the craft adapter proves per
  command, re-proved at the goal's own scale. An ack saying the craft finished
  is a statement about the queue.

Two renderings of game-authored text live here, and they are deliberately
different. The report carries recipe names and material types through
:func:`~pz_agent_core.observation.compact.redact_text`, exactly as the loot
report carries room names: a reader wants to know what the thing is called. The
goal record's ``detail`` does not — it is one printable line whose length the
goal channel bounds and whose construction the channel refuses if a line break
gets in, so a type reaching it is filtered down to the item-type alphabet and
truncated by :func:`_detail_token` first. A goal that could not be *failed*
because the failure text was too long is the failure mode that filter exists to
make impossible.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pz_agent_core.actions.adapters.crafting import MAX_CRAFT_COUNT
from pz_agent_core.actions.adapters.movement import MOVE_RETRY_POLICY
from pz_agent_core.actions.engine import ActionRequest
from pz_agent_core.navigation.local_map import GridSquare, square_of
from pz_agent_core.observation.compact import redact_text
from pz_agent_core.policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from pz_agent_core.policy.crafting import (
    CraftingDecision,
    CraftingRefusal,
    Shortfall,
    assess_craft,
)
from pz_agent_core.protocol import (
    ActionName,
    ActionResult,
    ActionStatus,
    InventoryView,
    JsonDict,
    Observation,
    ReasonCode,
)

from .loot_mission import (
    ENDED_CANCELLED,
    ENDED_COMPLETE,
    ENDED_IN_PROGRESS,
    ENDED_NO_PROGRESS,
    PHASE_START,
    MissionComplete,
    MissionProbe,
    MissionRefused,
    MissionStep,
    NextMissionMove,
)

__all__ = [
    "CRAFT_PHASES",
    "ENDED_EXHAUSTED",
    "ENDED_RECIPES_SPENT",
    "ENDED_REFUSED",
    "MAX_COMPLETION_PROBES",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_CRAFT_ATTEMPTS",
    "MAX_DETAIL_TYPE_CHARS",
    "MAX_NAMED_SHORTFALLS",
    "MAX_RECIPES_TRIED",
    "PHASE_CRAFT",
    "REFUSAL_REASONS",
    "CraftItemMission",
    "CraftMissionLimits",
]


# --------------------------------------------------------------------------
# bounds
# --------------------------------------------------------------------------

#: Craft commands one mission may issue, and the hard ceiling no limits object
#: may raise. The goal channel admits at most four runs
#: (``goals.model.MAX_CRAFT_GOAL_COUNT``, pinned by a test rather than imported
#: — the mission keeps the channel's number out of its own bounds), so six
#: leaves room for two runs to fail without the fourth item becoming
#: unreachable, and stops well short of "keep crafting until it works".
MAX_CRAFT_ATTEMPTS: Final = 6

#: Distinct recipes one mission may ask to run. A second recipe is the policy
#: genuinely changing its mind — the first recipe's materials are gone and
#: another way to make the product is affordable — and that is worth serving.
#: A fourth is a search, and a search that spends materials at every step is
#: exactly what this rung must not do on its own initiative.
MAX_RECIPES_TRIED: Final = 3

#: Consecutive failed channel actions before the mission ends. The other
#: missions' number, restated because missions may diverge and a shared
#: constant would hide the review when one does.
MAX_CONSECUTIVE_FAILURES: Final = 3

#: Completion probes a mission that ran no successful command may spend — the
#: loot mission's arrangement: each probe is one goal-seam request, so the
#: goal's own step budget bounds them a second time.
MAX_COMPLETION_PROBES: Final = 3

#: Material types named in a goal record's one-line ``detail``. Two, because
#: the line also has to carry the summary and the channel caps it at 200
#: characters; the whole list — bounded by the policy's own
#: ``MAX_REPORTED_SHORTFALLS`` — is in the report, which is where a reader who
#: wants all of it looks.
MAX_NAMED_SHORTFALLS: Final = 2

#: Longest rendering of one material type inside that line, and the alphabet it
#: is rendered in. See the module docstring: a detail is a single printable line
#: the goal channel refuses to hold if it is long or broken, and a type string
#: comes from the game.
MAX_DETAIL_TYPE_CHARS: Final = 32

#: Stand-in for a type whose every character the filter dropped. The goal
#: channel's own answer for an evidence key it cannot name, borrowed for the
#: same reason: a blank where a name belongs reads as a bug in the reporter.
UNNAMED_TYPE: Final = "unnamed"

#: Magnitude past which a count is described rather than quoted. Material
#: counts arrive from the game's recipe tables, so their width is the game's
#: choice, and a detail line's width must not be.
_MAX_RENDERED_COUNT: Final = 9_999
_UNQUOTABLE_COUNT: Final = "many"


# --------------------------------------------------------------------------
# the report's vocabulary
# --------------------------------------------------------------------------

#: The crafting policy refused: the recipe is unknown, the materials are short,
#: or what is short is only short because the user reserved it. Nothing was
#: spent — the refusal happens before any command exists to send.
ENDED_REFUSED: Final = "refused"

#: The attempt budget ran out with the goal's count unmade.
ENDED_EXHAUSTED: Final = "exhausted"

#: The recipe budget ran out: the policy went on naming recipes this mission
#: had not tried, and trying another would be a search paid for in materials.
ENDED_RECIPES_SPENT: Final = "recipes_spent"

#: The pipeline phases. ``start`` is the loot mission's token, imported rather
#: than restated; ``craft`` is the only other place this mission can be,
#: because it has exactly one command to send.
PHASE_CRAFT: Final = "craft"

CRAFT_PHASES: Final = (PHASE_START, PHASE_CRAFT)

#: The crafting policy's refusal token → the protocol reason the goal ends
#: with. A second copy of the table
#: :mod:`pz_agent_core.actions.adapters.crafting` keeps for its own validation
#: refusals, and knowingly so: that one is private to the adapter module and
#: this mission refuses *before* an adapter exists to ask. The two must agree,
#: which is asserted by a test that reads both rather than left to this
#: comment. Total over the enum — also pinned by a test.
REFUSAL_REASONS: Final[dict[CraftingRefusal, ReasonCode]] = {
    CraftingRefusal.RECIPE_UNKNOWN: ReasonCode.RECIPE_UNKNOWN,
    CraftingRefusal.MATERIALS_MISSING: ReasonCode.RECIPE_MATERIALS_MISSING,
    CraftingRefusal.MATERIALS_RESERVED: ReasonCode.RESOURCE_RESERVED,
}


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _detail_token(raw: str) -> str:
    """One game-authored identifier, made safe for a goal record's detail.

    Everything outside the item-type alphabet is dropped rather than replaced,
    and the result is truncated to :data:`MAX_DETAIL_TYPE_CHARS`. Harsher than
    :func:`~pz_agent_core.observation.compact.redact_text`, on purpose: that one
    preserves the wording a user would recognise, at a length the game chose,
    which is right for a report and wrong for a line whose *construction* fails
    if it runs long. A goal that could not be failed because its failure text
    was too long would be the worst outcome in this file.
    """
    kept = "".join(
        character
        for character in raw
        if character.isascii() and (character.isalnum() or character in "._-")
    )[:MAX_DETAIL_TYPE_CHARS]
    return kept or UNNAMED_TYPE


def _render_count(value: int) -> str:
    """A material count for a detail line, at a width this side chooses."""
    if 0 <= value <= _MAX_RENDERED_COUNT:
        return f"{value}"
    return _UNQUOTABLE_COUNT


def _shortfall_line(shortfalls: tuple[Shortfall, ...]) -> str:
    """What is short, named and bounded, for one line of a refusal."""
    return "; ".join(
        f"{_detail_token(short.full_type)} "
        f"({_render_count(short.free)} of {_render_count(short.needed)})"
        for short in shortfalls[:MAX_NAMED_SHORTFALLS]
    )


def _held(inventory: InventoryView | None, full_type: str) -> int | None:
    """How many of *full_type* the observation holds, or None when unreadable.

    The craft adapter's own count, restated with the same rule: every observed
    item of the type, wherever it sits. The postcondition is "the product is in
    the inventory", and an item in a bag on the character's back satisfies it as
    much as one in their hands.
    """
    if inventory is None:
        return None
    return sum(1 for item in inventory.items if item.full_type == full_type)


# --------------------------------------------------------------------------
# limits
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CraftMissionLimits:
    """Every bound one mission runs under.

    ``max_attempts`` and ``max_recipes`` may be narrowed, never widened:
    :data:`MAX_CRAFT_ATTEMPTS` and :data:`MAX_RECIPES_TRIED` are ceilings a
    limits object cannot raise, because how many irreversible commands one
    submission may authorise is a safety property rather than a tuning.
    """

    max_attempts: int = MAX_CRAFT_ATTEMPTS
    max_recipes: int = MAX_RECIPES_TRIED
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    max_completion_probes: int = MAX_COMPLETION_PROBES

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= MAX_CRAFT_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be within 1..{MAX_CRAFT_ATTEMPTS}, got {self.max_attempts}"
            )
        if not 1 <= self.max_recipes <= MAX_RECIPES_TRIED:
            raise ValueError(
                f"max_recipes must be within 1..{MAX_RECIPES_TRIED}, got {self.max_recipes}"
            )
        for name in ("max_consecutive_failures", "max_completion_probes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1, got {getattr(self, name)}")


# --------------------------------------------------------------------------
# the mission
# --------------------------------------------------------------------------


class CraftItemMission:
    """One ``craft_item`` goal, driven one bounded run at a time.

    Created by the wrapper when the goal activates and discarded when the goal
    reaches a terminal state — the mission never outlives its goal. All inputs
    are injected and deterministic: the product and count the goal validated,
    the policy configuration the assembled loop holds, and the frozen limits.
    The mission never picks a recipe (:func:`assess_craft` does), never counts a
    material (the policy does) and never proves a craft (the adapter does): what
    it owns is when to ask, how often, and when to stop asking.
    """

    def __init__(
        self,
        goal_id: str,
        *,
        product: str,
        count: int = 1,
        policy: PolicyConfig = DEFAULT_POLICY_CONFIG,
        limits: CraftMissionLimits | None = None,
    ) -> None:
        if not goal_id:
            raise ValueError("a mission belongs to a goal; goal_id is empty")
        if not product:
            raise ValueError("a craft mission must name the product it makes")
        if count < 1:
            raise ValueError(f"a craft mission must make at least one item, got {count}")
        self._goal_id = goal_id
        self._product = product
        self._wanted = count
        self._policy = policy
        self._limits = limits if limits is not None else CraftMissionLimits()

        self._stage = PHASE_START
        self._pending_action: str | None = None
        self._pending_recipe: str | None = None
        self._steps = 0
        self._probes = 0
        self._consecutive_failures = 0
        self._any_success = False

        #: How many of the product the first observation held. The mission's
        #: whole postcondition is measured against it, which is what makes
        #: "make two" mean two *more* rather than "own two".
        self._baseline: int | None = None
        self._made = 0
        self._attempts = 0
        self._crafts_succeeded = 0
        #: Distinct recipes asked to run, in the order the policy named them.
        self._recipes: list[str] = []
        #: Recipes whose craft command failed. A retired recipe is never asked
        #: for again by this mission — see the module docstring.
        self._retired: set[str] = set()
        self._refusal: str | None = None
        self._shortfalls: tuple[Shortfall, ...] = ()
        self._needs_travel: bool | None = None
        self._ended = ENDED_IN_PROGRESS
        self._final: MissionComplete | MissionRefused | None = None

    # -- read-only state ----------------------------------------------------

    @property
    def goal_id(self) -> str:
        return self._goal_id

    @property
    def product(self) -> str:
        """The item type this mission is making. Never interpreted here."""
        return self._product

    @property
    def wanted(self) -> int:
        """How many more of the product the goal asked for."""
        return self._wanted

    @property
    def ended(self) -> str:
        return self._ended

    @property
    def phase(self) -> str:
        """Where the mission stands: one of :data:`CRAFT_PHASES`.

        ``start`` covers "not yet begun" and "between runs"; a mission that
        already ended keeps the phase it stopped in, because whether it ended is
        :attr:`ended`'s answer, not this one's.
        """
        return self._stage

    @property
    def any_success(self) -> bool:
        """Whether any channel action this mission asked for succeeded."""
        return self._any_success

    def mark_abandoned(self) -> None:
        """Seal the report of a mission whose goal died under it.

        The wrapper's prune path. Only a mission still in flight is resealed — a
        mission that already ended keeps the ending it earned.
        """
        if self._ended is ENDED_IN_PROGRESS:
            self._ended = ENDED_CANCELLED

    @property
    def report(self) -> JsonDict:
        """The mission's deliverable, rendered fresh on every read.

        Counts, closed tokens and game-authored names run through
        :func:`~pz_agent_core.observation.compact.redact_text` — the loot
        report's rule, for the loot report's reason: a user asking "what did it
        try?" wants the recipe's name, and a redacted name is safe to carry.
        ``made``/``wanted``/``ended`` is the deliverable a confirmation reads
        back; ``shortfalls`` is what makes a refusal actionable.
        """
        return {
            "product": redact_text(self._product),
            "wanted": self._wanted,
            "made": self._made,
            "attempts": self._attempts,
            "crafts_succeeded": self._crafts_succeeded,
            "recipes_tried": [redact_text(name) for name in self._recipes],
            "needs_travel": self._needs_travel,
            "refusal": self._refusal,
            "shortfalls": [
                {**short.as_dict(), "full_type": redact_text(short.full_type)}
                for short in self._shortfalls
            ],
            "ended": self._ended,
        }

    def summary_line(self) -> str:
        """One bounded line for a goal record's ``detail``.

        Constants, counts and the policy's closed refusal token only — no refs,
        no game-authored names — so it satisfies the goal channel's rule that a
        detail is never caller text.
        """
        refusal = self._refusal if self._refusal is not None else "none"
        # The counts go through the same width guard the material counts do.
        # A goal-channel goal cannot carry a count past MAX_CRAFT_GOAL_COUNT,
        # but a mission is constructible without one, and a detail line whose
        # length a caller chose is a detail line the goal record would refuse.
        return (
            f"craft {self._ended}: made={_render_count(self._made)}/"
            f"{_render_count(self._wanted)} attempts={self._attempts} "
            f"recipes={len(self._recipes)} refusal={refusal}"
        )

    # -- results ------------------------------------------------------------

    def note_result(self, result: ActionResult) -> None:
        """Fold one terminal engine result into the mission's bookkeeping.

        Non-terminal acks and results for actions this mission did not ask for
        are ignored — the mission has at most one step in flight. A failed craft
        retires its recipe here rather than at the next decision, because this
        is the only place that knows which recipe the failure belonged to.
        """
        if self._final is not None or not result.is_terminal:
            return
        expected = self._pending_action
        if expected is None or result.action != expected:
            return
        self._pending_action = None
        recipe = self._pending_recipe
        self._pending_recipe = None
        if result.status is ActionStatus.SUCCEEDED:
            self._any_success = True
            self._consecutive_failures = 0
            self._crafts_succeeded += 1
        else:
            self._consecutive_failures += 1
            if recipe is not None:
                self._retired.add(recipe)
        self._stage = PHASE_START

    # -- the decision -------------------------------------------------------

    def next_step(self, observation: Observation) -> NextMissionMove:
        """The next thing to do, from the newest observation.

        Order is fixed: replay a sealed ending, wait out the one step in flight,
        stop a failing mission, count the product (the observed postcondition
        beats every budget), then spend a budget and ask the policy. Every
        branch either emits one bounded command, ends the mission, or waits for
        a fresh observation; the attempt, recipe and failure budgets bound how
        long the loop can be kept walking through here.
        """
        if self._final is not None:
            return self._final
        if self._pending_action is not None:
            return None
        if self._consecutive_failures >= self._limits.max_consecutive_failures:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                f"{self._limits.max_consecutive_failures} consecutive craft commands failed",
            )

        held = _held(observation.inventory, self._product)
        if held is None:
            # No inventory tier is no proof: a craft whose product could not be
            # counted afterwards must not be started, and one already started
            # must not be judged.
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "the mod sent no inventory tier this tick, so the product cannot be counted",
            )
        if self._baseline is None:
            self._baseline = held
        self._made = max(0, held - self._baseline)

        # The observed postcondition, before any budget: the goal's count made
        # in this very observation ends the mission on real evidence.
        if self._made >= self._wanted:
            return self._finish(observation)
        if self._attempts >= self._limits.max_attempts:
            return self._refuse(
                ENDED_EXHAUSTED,
                ReasonCode.NO_PROGRESS,
                f"the attempt budget ({self._limits.max_attempts}) ran out",
            )

        decision = assess_craft(observation, self._product, self._policy)
        self._needs_travel = decision.needs_travel
        if decision.refused:
            return self._refuse_policy(decision)
        recipe = decision.recipe
        assert recipe is not None  # a craft verdict is always about a recipe
        if recipe.name in self._retired:
            # The policy is answering that nothing has changed since the run
            # that failed. Another command with the same recipe would be a
            # retry of an irreversible action, which this rung does not make on
            # its own; the honest move is to stop and say so.
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                "the crafting policy chose again the recipe whose craft already failed",
            )
        if recipe.name not in self._recipes and len(self._recipes) >= self._limits.max_recipes:
            return self._refuse(
                ENDED_RECIPES_SPENT,
                ReasonCode.NO_PROGRESS,
                f"the recipe budget ({self._limits.max_recipes}) ran out",
            )
        if recipe.name not in self._recipes:
            self._recipes.append(recipe.name)
        self._attempts += 1
        self._pending_recipe = recipe.name
        self._stage = PHASE_CRAFT
        # ``count`` travels on the wire rather than defaulting in the mod, and
        # the adapter's own ceiling is imported rather than restated: one run
        # per command is a protocol fact, and a widening of it should be a
        # review here as well as there.
        return self._emit(
            observation,
            ActionName.CRAFTING_CRAFT,
            {"recipe": recipe.name, "count": MAX_CRAFT_COUNT},
        )

    # -- endings -------------------------------------------------------------

    def _refuse_policy(self, decision: CraftingDecision) -> MissionRefused:
        """The crafting policy's refusal, as the goal's typed end.

        Nothing has been spent when this fires — the policy is asked before a
        command exists — so the mission's whole remaining duty is to say what is
        short and stop. It does not go looting: whether to spend the next hour
        finding two planks is the user's decision and their next sentence, and
        making it here would be an errand nobody ordered.
        """
        refusal = decision.refusal
        assert refusal is not None  # a refused decision carries its token
        self._refusal = refusal.value
        self._shortfalls = decision.shortfalls
        listed = _shortfall_line(decision.shortfalls)
        if listed:
            cause = f"short of {listed}"
        elif decision.recipe is None:
            cause = "no observed item participates in a recipe that makes it"
        else:
            cause = "the character is not observed to have learned the recipe"
        return self._refuse(ENDED_REFUSED, REFUSAL_REASONS[refusal], cause)

    def _finish(self, observation: Observation) -> NextMissionMove:
        """Complete — through the last real result, or through one probe.

        The loot mission's arrangement, for its reason. The no-command case is
        real here and not a formality: the count is measured against the
        observation the mission started from, so a player who crafts the thing
        themselves while the goal waits — or a craft whose result was lost while
        its product landed — satisfies the postcondition with no result of this
        mission's to succeed the goal with. Minting one would fabricate
        evidence; the probe is a bounded move inside the character's own square,
        numbered so a failed probe's retry never reuses a key.
        """
        if self._any_success:
            self._ended = ENDED_COMPLETE
            self._final = MissionComplete()
            return self._final
        if self._probes >= self._limits.max_completion_probes:
            return self._refuse(
                ENDED_NO_PROGRESS,
                ReasonCode.NO_PROGRESS,
                "the completion probe was never observed succeeding",
            )
        self._ended = ENDED_COMPLETE
        self._probes += 1
        here: GridSquare = square_of(observation.player.position)
        return MissionProbe(
            request=ActionRequest(
                action=ActionName.MOVEMENT_MOVE_TO,
                session_id=observation.session_id,
                idempotency_key=f"craft:{self._goal_id}:done{self._probes}",
                args={
                    "target": {"x": here[0], "y": here[1], "z": here[2]},
                    "radius": 1.0,
                    "max_distance": 1,
                    "allow_doors": True,
                    "allow_stairs": True,
                },
                policy=MOVE_RETRY_POLICY,
            )
        )

    def _refuse(self, ended: str, reason_code: ReasonCode, cause: str) -> MissionRefused:
        self._ended = ended
        refused = MissionRefused(reason_code=reason_code, detail=f"{cause}; {self.summary_line()}")
        self._final = refused
        return refused

    # -- machinery -----------------------------------------------------------

    def _emit(self, observation: Observation, action: ActionName, args: JsonDict) -> MissionStep:
        self._steps += 1
        request = ActionRequest(
            action=action,
            session_id=observation.session_id,
            idempotency_key=f"craft:{self._goal_id}:s{self._steps}",
            args=args,
        )
        self._pending_action = action.value
        return MissionStep(request=request)
