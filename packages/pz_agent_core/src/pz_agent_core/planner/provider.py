"""Where a plan comes from, and the one source that needs no network.

§7.12 lists the planner providers: ``none``, three hosted models, a local
OpenAI-compatible endpoint, and a custom TeamON one. :class:`PlanProvider` is
the seam all of them plug into — a provider is handed a
:class:`PlanRequest` and answers with a :class:`PlanProposal`, and that is the
entire contract. Nothing in this package speaks HTTP, holds a key or knows what
a token is; wiring a model up means writing a provider outside core that returns
a :class:`~pz_agent_core.planner.plan.Plan`, and the plan type is what makes
that safe rather than the provider being trusted.

:class:`NullProvider` is ``provider = "none"``. It is not a degraded mode and
not a placeholder: it is the deterministic path, and it is the one this build
actually ships, because ``pz_agent_cli.config.SUPPORTED_PROVIDERS`` names it and
nothing else. It reaches a plan the way the rest of this package reaches every
other decision — by asking the selection policies, which already own "which
sandwich" and answer it identically every time (AGENTS.md, "Determinism where it
counts").

Two things it deliberately does not do:

* **It does not invent an item.** Every reference in a plan it builds came from
  a :class:`~pz_agent_core.policy.food.FoodChoice`,
  :class:`~pz_agent_core.policy.drink.DrinkChoice` or
  :class:`~pz_agent_core.policy.literature.LiteratureChoice`, which in turn came
  from the observation the mod sent.
* **It does not report a confidence.** §7.6 asks a planner for one and then says
  policy must not trust it; a deterministic rule has no calibrated probability to
  offer, and emitting ``1.0`` would supply exactly the number that clause warns
  against being believed. The field stays empty and the critic never reads it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Final, Protocol

from ..observation.compact import redact_text
from ..policy.config import DEFAULT_POLICY_CONFIG, PolicyConfig
from ..policy.drink import select_drink
from ..policy.food import select_food
from ..policy.literature import LiteratureGoal, select_literature
from ..policy.selection import NO_CAPABILITIES, CapabilityLookup, Rejection
from ..protocol import ActionName, InventoryView, Observation, ReasonCode
from .plan import (
    DEFAULT_READ_PAGES,
    MAX_PLAN_STEPS,
    MAX_READ_PAGES,
    MAX_SUMMARY_CHARS,
    ConsumeArgs,
    FailureMode,
    ItemArgs,
    Plan,
    PlanStep,
    ReadArgs,
    StepFailure,
    SuccessCriterion,
    SuccessKind,
)

__all__ = [
    "MAX_RECENT_FAILURES",
    "MAX_REPORTED_REASONS",
    "PROVIDER_NONE",
    "Goal",
    "GoalKind",
    "NullProvider",
    "PlanProposal",
    "PlanProvider",
    "PlanRequest",
]

#: The value ``planner.provider`` takes for the deterministic path.
PROVIDER_NONE: Final = "none"

#: How much failure history a request may carry. The critic only needs to
#: recognise a step the agent has just tried; an unbounded log of them would be
#: a growing input to a decision that is supposed to be cheap.
MAX_RECENT_FAILURES: Final = 16

#: Refusal lines a proposal reports. A refusal is rendered to a user, so it is
#: bounded like every other diagnostic in this codebase.
MAX_REPORTED_REASONS: Final = 5


class GoalKind(StrEnum):
    """What a goal asks for, in the vocabulary the deterministic path can serve.

    Closed on purpose. A goal is the one thing a model is allowed to express
    (AGENTS.md: "The model may express a *goal*; it never picks the sandwich"),
    so the set of goals is a reviewed list rather than a free string that a
    provider could stretch into an instruction.

    ``NAVIGATE_TO``, ``LOOT_AREA``, ``RETURN_HOME`` and ``EXPLORE_AREA`` are
    in the vocabulary and deliberately not in any provider's repertoire:
    routes are walked by the deterministic executor in
    :mod:`pz_agent_core.navigation`, one observed square at a time — a
    ``return_home`` is that same walk with its target read from the save's
    memory — loot sweeps are driven by the CLI's deterministic loot mission,
    one observed container at a time, and explore sweeps by the CLI's
    deterministic explore mission, one frontier square at a time. A provider
    asked for any of them answers with a typed refusal naming the
    deterministic server — never with a plan that approximates the work with
    something else.

    ``TREAT_WOUNDS``, ``REST_UNTIL`` and ``SLEEP_UNTIL_RESTED`` sit in the
    same column, served by the CLI's deterministic care missions over the
    medical and survival adapters — bandaging wound by observed wound,
    resting and sleeping under the adapters' own verification. A provider
    asked for any of the three answers with the same typed refusal shape.

    ``AVOID_THREAT`` sits there too, and most emphatically: a retreat is
    driven by the CLI's deterministic avoid mission over the threat-aware
    route executor, from the observed threat picture, and a provider asked
    to plan one would be a model choosing where to run from zombies. The
    typed refusal names the deterministic server, like the rest.

    ``ENGAGE_SINGLE_ZOMBIE`` is that argument at its absolute limit: the
    assisted-combat kind is driven by the CLI's deterministic combat
    mission over the combat policy's per-window gates, and a provider asked
    to plan one would be a model deciding a fight is safe. Every provider
    refuses it by name; only an explicit user submission reaches it, and
    the autonomy initiative path never mints it (pinned by test).

    ``CRAFT_ITEM`` sits in the column too, and it is the one whose refusal
    cannot be walked back if it is ever dropped: a craft *destroys* what it
    spends. Which recipe makes the named product, whether the character has
    learned it, and whether the materials may be spent are
    :mod:`pz_agent_core.policy.crafting`'s deterministic answers, re-asked
    against a fresh observation before every run by the CLI's craft mission
    (``pz_agent_cli.craft_mission``); a provider asked to plan one would be a
    model choosing which of the character's possessions to destroy. The typed
    refusal names the deterministic server, like the rest.

    ``BUILD_STRUCTURE`` is the end of that column and the one member no
    provider will ever be given a reason to serve. A craft destroys what it
    spends; a build puts a permanent object on a square, and this project
    ships no action that removes one. Which structure may stand where is
    :mod:`pz_agent_core.policy.building`'s deterministic answer — the square
    free, the materials present, and above all the placement not sealing the
    character in — re-asked against a fresh observation before the one command
    the CLI's build mission (``pz_agent_cli.build_mission``) issues. A provider
    asked to plan one would be a model deciding to put something in the world
    that nothing in this build can take out of it, so the typed refusal names
    the deterministic server like the rest, and the action underneath is P4
    with no autonomous path in any mode besides.
    """

    SATISFY_HUNGER = "satisfy_hunger"
    SATISFY_THIRST = "satisfy_thirst"
    READ_FOR_BOREDOM = "read_for_boredom"
    TRAIN_SKILL = "train_skill"
    LEARN_RECIPE = "learn_recipe"
    NAVIGATE_TO = "navigate_to"
    LOOT_AREA = "loot_area"
    RETURN_HOME = "return_home"
    EXPLORE_AREA = "explore_area"
    TREAT_WOUNDS = "treat_wounds"
    REST_UNTIL = "rest_until"
    SLEEP_UNTIL_RESTED = "sleep_until_rested"
    AVOID_THREAT = "avoid_threat"
    ENGAGE_SINGLE_ZOMBIE = "engage_single_zombie"
    CRAFT_ITEM = "craft_item"
    BUILD_STRUCTURE = "build_structure"


@dataclass(frozen=True, slots=True)
class Goal:
    """One thing the user or the autonomy loop wants done."""

    goal_id: str
    kind: GoalKind
    #: Required by :attr:`GoalKind.TRAIN_SKILL` and meaningless otherwise.
    skill: str = ""

    def __post_init__(self) -> None:
        try:
            uuid.UUID(self.goal_id)
        except ValueError as exc:
            raise ValueError(f"goal_id must be a UUID, got {self.goal_id!r}") from exc
        if self.kind is GoalKind.TRAIN_SKILL and not self.skill.strip():
            raise ValueError("a train_skill goal must name the skill")
        if self.kind is not GoalKind.TRAIN_SKILL and self.skill:
            raise ValueError(f"{self.kind.value} takes no skill, got {self.skill!r}")


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """Everything a provider is given, and nothing else.

    The master prompt's Этап 6 lists the planner's inputs: the goal, a compact
    observation, capabilities, policy, memory and the last action results. The
    first five are here directly; the sixth is :attr:`recent_failures`, reduced
    to what a planner can act on — which step, and why it ended.

    The full :class:`~pz_agent_core.protocol.Observation` is carried rather than
    the compact view because the selection policies score against the whole
    inventory. A provider that hands state to a model must run it through
    :func:`~pz_agent_core.observation.compact_for_planner` first; that is the
    boundary, and it is one call away.
    """

    goal: Goal
    observation: Observation
    capabilities: CapabilityLookup = NO_CAPABILITIES
    policy: PolicyConfig = DEFAULT_POLICY_CONFIG
    max_steps: int = MAX_PLAN_STEPS
    recent_failures: tuple[StepFailure, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.max_steps <= MAX_PLAN_STEPS:
            raise ValueError(f"max_steps must be within 1..{MAX_PLAN_STEPS}, got {self.max_steps}")
        if len(self.recent_failures) > MAX_RECENT_FAILURES:
            # Trimmed rather than refused: a caller accumulating history is doing
            # the right thing, and the oldest entries are the ones a replan has
            # least use for.
            object.__setattr__(self, "recent_failures", self.recent_failures[-MAX_RECENT_FAILURES:])

    def failed_signatures(self) -> frozenset[str]:
        """Signatures of the steps that have already failed."""
        return frozenset(failure.signature for failure in self.recent_failures)


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A plan, or a refusal explaining why the goal cannot be served.

    A refusal carries the selection policy's own reasons verbatim, because "I
    cannot eat" is useless next to "the ham is rotten and the beans are yours".
    """

    plan: Plan | None
    reason_code: ReasonCode | None
    detail: str
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.plan is None) == (self.reason_code is None):
            raise ValueError("a proposal is either a plan or a refusal, never both or neither")
        if not self.detail.strip():
            raise ValueError("a proposal must explain itself")
        if len(self.reasons) > MAX_REPORTED_REASONS:
            object.__setattr__(self, "reasons", self.reasons[:MAX_REPORTED_REASONS])

    @property
    def refused(self) -> bool:
        return self.plan is None

    @classmethod
    def proposed(cls, plan: Plan, detail: str) -> PlanProposal:
        return cls(plan=plan, reason_code=None, detail=detail)

    @classmethod
    def refusal(
        cls,
        reason_code: ReasonCode,
        detail: str,
        *,
        reasons: tuple[str, ...] = (),
    ) -> PlanProposal:
        if reason_code is ReasonCode.POSTCONDITION_MET:
            raise ValueError("POSTCONDITION_MET cannot explain a refusal")
        return cls(plan=None, reason_code=reason_code, detail=detail, reasons=reasons)


class PlanProvider(Protocol):
    """Turns a goal into a typed plan.

    The return type is the whole security boundary. A provider cannot hand back
    Lua, a shell command, a keystroke or a file path, because
    :class:`~pz_agent_core.planner.plan.Plan` has nowhere to put one — and a
    provider that fabricates a reference is caught by
    :class:`~pz_agent_core.planner.critic.PlanCritic` before anything runs.
    """

    @property
    def name(self) -> str:
        """The ``planner.provider`` value this implements."""
        ...

    def propose(self, request: PlanRequest) -> PlanProposal:
        """Answer *request* with a plan or a refusal. Never raises."""
        ...


@dataclass(frozen=True, slots=True)
class NullProvider:
    """``provider = "none"``: plans built by ordinary deterministic code.

    Every plan it produces is at most two steps — bring the item to hand if it
    is not already, then use it — which is what §7.4's "план не должен содержать
    десятки слепых действий" describes and what the selection policies can
    actually justify. Anything longer would be this module guessing about a
    world it re-observes between every step anyway.
    """

    name: ClassVar[str] = PROVIDER_NONE

    def propose(self, request: PlanRequest) -> PlanProposal:
        """Serve *request* from the policy modules, or explain why nothing fits."""
        if request.goal.kind is GoalKind.NAVIGATE_TO:
            # Refused before the inventory gate: walking needs no inventory,
            # and the honest refusal names the component that does serve it.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "navigate_to is walked by the deterministic route executor, not planned "
                "by a provider; a sidecar without the navigating planner wired cannot "
                "serve it.",
            )
        if request.goal.kind is GoalKind.LOOT_AREA:
            # Same refusal shape for the same reason: a loot sweep is a
            # deterministic mission over navigation, inspection, selection and
            # batch transfer, and a provider that answered it with a plan
            # would be approximating all four with a guess.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "loot_area is driven by the deterministic loot mission, not planned by "
                "a provider; a sidecar without the navigating planner wired cannot "
                "serve it.",
            )
        if request.goal.kind is GoalKind.RETURN_HOME:
            # The same walk as navigate_to with its target read from the
            # save's memory; a provider holds neither the memory nor the map.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "return_home is walked by the deterministic route executor to the "
                "remembered home point, not planned by a provider; a sidecar without "
                "the navigating planner wired cannot serve it.",
            )
        if request.goal.kind is GoalKind.EXPLORE_AREA:
            # Frontier selection is arithmetic over the local map, and the map
            # lives behind the navigating wrapper, not behind any provider.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "explore_area is driven by the deterministic explore mission, not "
                "planned by a provider; a sidecar without the navigating planner "
                "wired cannot serve it.",
            )
        if request.goal.kind is GoalKind.TREAT_WOUNDS:
            # Triage is policy.medical's decision made per observation; a plan
            # would freeze one wound and one dressing into a guess about a
            # body that is re-observed between every bandage.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "treat_wounds is driven by the deterministic care mission, not "
                "planned by a provider; a sidecar without the navigating planner "
                "wired cannot serve it.",
            )
        if request.goal.kind is GoalKind.REST_UNTIL:
            # One survival.rest whose adapter does the waiting and the
            # verifying; a provider holds neither the wait nor the proof.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "rest_until is driven by the deterministic care mission, not "
                "planned by a provider; a sidecar without the navigating planner "
                "wired cannot serve it.",
            )
        if request.goal.kind is GoalKind.AVOID_THREAT:
            # A retreat is decided from the observed threat picture by the
            # deterministic avoid mission; a provider planning one would be
            # a model choosing where to run from zombies.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "avoid_threat is driven by the deterministic avoid mission, not "
                "planned by a provider; a sidecar without the navigating planner "
                "wired cannot serve it.",
            )
        if request.goal.kind is GoalKind.ENGAGE_SINGLE_ZOMBIE:
            # The assisted-combat kind, refused by name and most emphatically
            # of all: whether one zombie may be engaged is the deterministic
            # combat policy's decision, remade before every bounded window by
            # the combat mission, and a provider planning a fight would be a
            # model deciding it is safe to swing.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "engage_single_zombie is driven by the deterministic combat mission, "
                "not planned by a provider; a sidecar without the navigating planner "
                "wired cannot serve it.",
            )
        if request.goal.kind is GoalKind.CRAFT_ITEM:
            # Refused by name like the rest of the column, and with the least
            # room for argument of any of them: a craft spends materials that
            # no later observation puts back, so which recipe runs is the
            # crafting policy's deterministic answer re-asked before every
            # run — never a plan a provider guessed once.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "craft_item is driven by the deterministic craft mission, not planned "
                "by a provider; a sidecar without the navigating planner wired cannot "
                "serve it.",
            )
        if request.goal.kind is GoalKind.BUILD_STRUCTURE:
            # The last of the column and the least arguable: a build puts a
            # permanent object on a square, nothing in this build takes one
            # back down, and whether the placement would seal the character in
            # is the building policy's deterministic answer about the observed
            # window — not a plan a provider guessed from a compacted view.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "build_structure is driven by the deterministic build mission, not "
                "planned by a provider; a sidecar without the navigating planner wired "
                "cannot serve it.",
            )
        if request.goal.kind is GoalKind.SLEEP_UNTIL_RESTED:
            # Sleep is the one P4 action; the reflex-guard refusal must reach
            # the goal unchanged, which only the deterministic server does.
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "sleep_until_rested is driven by the deterministic care mission, "
                "not planned by a provider; a sidecar without the navigating "
                "planner wired cannot serve it.",
            )
        if request.observation.inventory is None:
            return PlanProposal.refusal(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "the mod sent no inventory this tick, so I cannot tell what is available.",
            )
        kind = request.goal.kind
        if kind is GoalKind.SATISFY_HUNGER:
            return self._eat(request)
        if kind is GoalKind.SATISFY_THIRST:
            return self._drink(request)
        return self._read(request)

    # -- goals -------------------------------------------------------------

    def _eat(self, request: PlanRequest) -> PlanProposal:
        player = request.observation.player
        policy = request.policy
        if player.hunger <= policy.satisfied_hunger:
            return _already_met("hunger", player.hunger, policy.satisfied_hunger)
        inventory = _inventory(request)
        selection = select_food(inventory, player, policy, capabilities=request.capabilities)
        choice = selection.choice
        if choice is None:
            return _nothing_to_use(
                "eat", selection.reason_code or ReasonCode.NO_SAFE_FOOD, selection.rejections
            )
        return _proposal(
            request,
            item_ref=choice.item_ref,
            display_name=choice.display_name,
            needs_transfer=choice.needs_transfer_to_main,
            action=ActionName.CONSUME_EAT,
            args=ConsumeArgs(item_ref=choice.item_ref, fraction=choice.fraction),
            success=SuccessCriterion(
                kind=SuccessKind.HUNGER_AT_MOST, value=policy.satisfied_hunger
            ),
            verb="Eat",
            factors=choice.breakdown.explain(),
        )

    def _drink(self, request: PlanRequest) -> PlanProposal:
        player = request.observation.player
        policy = request.policy
        if player.thirst <= policy.satisfied_thirst:
            return _already_met("thirst", player.thirst, policy.satisfied_thirst)
        inventory = _inventory(request)
        selection = select_drink(inventory, player, policy, capabilities=request.capabilities)
        choice = selection.choice
        if choice is None:
            return _nothing_to_use(
                "drink", selection.reason_code or ReasonCode.NO_SAFE_DRINK, selection.rejections
            )
        return _proposal(
            request,
            item_ref=choice.item_ref,
            display_name=choice.display_name,
            needs_transfer=choice.needs_transfer_to_main,
            action=ActionName.CONSUME_DRINK,
            args=ConsumeArgs(item_ref=choice.item_ref, fraction=choice.fraction),
            success=SuccessCriterion(
                kind=SuccessKind.THIRST_AT_MOST, value=policy.satisfied_thirst
            ),
            verb="Drink",
            factors=choice.breakdown.explain(),
        )

    def _read(self, request: PlanRequest) -> PlanProposal:
        goal = _literature_goal(request.goal)
        selection = select_literature(
            _inventory(request), request.observation.player, goal, request.policy
        )
        choice = selection.choice
        if choice is None:
            return _nothing_to_use(
                "read",
                selection.reason_code or ReasonCode.NO_SUITABLE_LITERATURE,
                selection.rejections,
            )
        pages = min(choice.pages_remaining or DEFAULT_READ_PAGES, DEFAULT_READ_PAGES)
        return _proposal(
            request,
            item_ref=choice.item_ref,
            display_name=choice.display_name,
            needs_transfer=choice.needs_transfer_to_main,
            action=ActionName.LITERATURE_READ,
            args=ReadArgs(item_ref=choice.item_ref, pages=max(1, min(pages, MAX_READ_PAGES))),
            success=SuccessCriterion(kind=SuccessKind.ADAPTER_EVIDENCE),
            verb="Read",
            factors=choice.breakdown.explain(),
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_LITERATURE_GOALS: Final = {
    GoalKind.READ_FOR_BOREDOM: LiteratureGoal.boredom,
    GoalKind.LEARN_RECIPE: LiteratureGoal.recipe,
}


def _literature_goal(goal: Goal) -> LiteratureGoal:
    if goal.kind is GoalKind.TRAIN_SKILL:
        return LiteratureGoal.train(goal.skill)
    return _LITERATURE_GOALS[goal.kind]()


def _inventory(request: PlanRequest) -> InventoryView:
    """The observation's inventory, which :meth:`NullProvider.propose` checked.

    Restated rather than asserted so the type is narrowed without a cast; the
    empty view is the honest fallback for a state ``propose`` already refused.
    """
    return request.observation.inventory or InventoryView()


def _already_met(need: str, current: float, target: float) -> PlanProposal:
    return PlanProposal.refusal(
        ReasonCode.PRECONDITION_FAILED,
        f"{need} is already at {current:.2f}, at or below the {target:.2f} I aim for; "
        "there is nothing worth spending an item on.",
    )


def _nothing_to_use(
    verb: str, reason_code: ReasonCode, rejections: tuple[Rejection, ...]
) -> PlanProposal:
    return PlanProposal.refusal(
        reason_code,
        f"there is nothing safe to {verb}.",
        reasons=tuple(rejection.describe() for rejection in rejections[:MAX_REPORTED_REASONS]),
    )


def _proposal(
    request: PlanRequest,
    *,
    item_ref: str,
    display_name: str,
    needs_transfer: bool,
    action: ActionName,
    args: ConsumeArgs | ReadArgs,
    success: SuccessCriterion,
    verb: str,
    factors: str,
) -> PlanProposal:
    """Assemble the one- or two-step plan every deterministic goal reduces to.

    The summary is built from exactly two things: the item's display name, run
    through :func:`~pz_agent_core.observation.compact.redact_text` because it is
    game-authored (§7.11), and the score factors, whose names are constants in
    this codebase.

    The selection policies' own ``rationale`` is deliberately *not* used, even
    though it reads better. It interpolates several strings that came out of the
    game — the display name, a fluid type, a skill name — and redacting a
    sentence after those have been embedded in it means finding them again. A
    summary assembled only from safe parts needs no such search.
    """
    name = redact_text(display_name)
    steps: list[PlanStep] = []
    if needs_transfer and request.max_steps < 2:
        return PlanProposal.refusal(
            ReasonCode.POLICY_DENIED,
            f"{name} has to be moved into the main inventory first, which makes this a "
            f"two-step plan and you allowed {request.max_steps}.",
        )
    if needs_transfer:
        steps.append(
            PlanStep(
                step_id="s1",
                action=ActionName.INVENTORY_ENSURE_MAIN,
                args=ItemArgs(item_ref=item_ref),
                success=SuccessCriterion(kind=SuccessKind.ITEM_IN_MAIN_INVENTORY),
                # A transfer that cannot be made is a reason to choose a
                # different item, which is exactly what one replan does.
                on_failure=FailureMode.REPLAN_ONCE,
            )
        )
    steps.append(
        PlanStep(
            step_id=f"s{len(steps) + 1}",
            action=action,
            args=args,
            success=success,
            # Nothing deterministic is left to try once the chosen item refused
            # to be used, so the honest next move is a question.
            on_failure=FailureMode.ASK_USER,
        )
    )
    # The breakdown is bounded by the number of factors, not by 500 characters;
    # truncating here keeps a long one from turning a valid plan into a rejected
    # one.
    summary = f"{verb} {name} — {factors}"[:MAX_SUMMARY_CHARS] if factors else f"{verb} {name}"
    plan = Plan(goal_id=request.goal.goal_id, summary=summary, steps=tuple(steps))
    return PlanProposal.proposed(plan, summary)
