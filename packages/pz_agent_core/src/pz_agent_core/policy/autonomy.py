"""The self-directed loop's gate: may the agent act unasked, and on what?

Everything below answers one question per tick — "is there something I should
start on my own initiative right now?" — and the default answer is no. A gate
whose refusals are cheap and whose allowance is expensive is the right shape
here: an assistant that acts when it should have asked is the failure mode §7.7
is written to prevent, and the reverse merely wastes a tick.

The gates, in the order they run:

1. **Session.** Only ``AUTONOMOUS`` acts unasked (§7.5), and only while armed,
   un-taken-over and attached to a live character.
2. **Backup.** §7.7 puts "сохранение не резервировано" on the ask-the-user side,
   so a backup is a *parameter* of the decision — :meth:`AutonomyGate.evaluate`
   takes it as a keyword with no default, the way
   :meth:`BackupManager.restore` takes ``game_running``. There is no code path
   that assumes one exists.
3. **Threat and manual queue.** A dangerous tick belongs to the reflex guard,
   not to a maintenance plan.
4. **Needs and arbitration.** Needs are derived from the observation against the
   §17.1 table and handed to :class:`~pz_agent_core.safety.priority.NeedArbiter`,
   which already owns both the interrupt ladder and the §7.8 anti-loop brake.
   This module adds no counting of its own — a second loop detector with its own
   thresholds is how two subsystems end up disagreeing about whether the agent
   is stuck.
5. **Permission.** The winning need's plan goes through
   :func:`~pz_agent_core.policy.permissions.check_permission` on
   ``SELF_DIRECTED`` initiative, which is where P4 and unverified capabilities
   are refused.
6. **Bounds.** Plan length, actions per interval, and the home radius of §7.10.
7. **Reservation.** §7.9 is absolute: an item the user set aside is not consumed
   autonomously. When the reservation is the only reason there is nothing to
   consume, the agent asks instead of going hungry in silence.

The gate is stateful in exactly two ways — the arbiter's counters and the action
budget — and both are bounded. Everything else is a pure function of the inputs.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from ..protocol import (
    ActionName,
    DangerLevel,
    InventoryView,
    Observation,
    PlayerState,
    Priority,
    ReasonCode,
    RiskClass,
    SessionMode,
)
from ..safety.priority import (
    DEFAULT_ANTI_LOOP_CONFIG,
    AntiLoopConfig,
    Arbitration,
    ArbitrationDecision,
    Need,
    NeedArbiter,
    RunningPlan,
)
from .config import (
    CAPABILITY_DRINK_CARRIED,
    CAPABILITY_EAT_PERCENTAGE,
    CAPABILITY_READ_LITERATURE,
    DEFAULT_POLICY_CONFIG,
    PolicyConfig,
)
from .drink import select_drink
from .food import select_food
from .literature import LiteratureGoal, select_literature
from .permissions import (
    DEFAULT_PERMISSION_CONFIG,
    Initiative,
    PermissionConfig,
    PermissionRequest,
    check_permission,
)
from .selection import NO_CAPABILITIES, CapabilityLookup, Rejection, RejectionReason

__all__ = [
    "DEFAULT_AUTONOMY_CONFIG",
    "MAX_ACTIONS_PER_INTERVAL",
    "MAX_HOME_RADIUS",
    "MAX_PLAN_STEPS",
    "NEED_BLEEDING",
    "NEED_BOREDOM",
    "NEED_FATIGUE",
    "NEED_HUNGER",
    "NEED_PLANS",
    "NEED_THIRST",
    "NO_MEMORY",
    "AutonomyConfig",
    "AutonomyDecision",
    "AutonomyGate",
    "AutonomyMemory",
    "AutonomyOutcome",
    "BackupEvidence",
    "Consumable",
    "HomeSquare",
    "ItemSelection",
    "NeedPlan",
    "NoMemory",
    "SelectedItem",
    "derive_needs",
]

#: Ceilings the configuration may not exceed. §7.10 bounds the autonomous radius
#: as a product decision, not a taste; the other two bound how much the agent can
#: set in motion before a human sees any of it.
MAX_HOME_RADIUS: Final = 100
MAX_PLAN_STEPS: Final = 12
MAX_ACTIONS_PER_INTERVAL: Final = 30


class AutonomyOutcome(StrEnum):
    """What the loop should do with this tick."""

    #: Start the plan on the decision. The action budget has been charged.
    ACT = "act"
    #: Autonomy is available but there is nothing to start right now.
    HOLD = "hold"
    #: Something needs a human answer before the agent may proceed (§7.7).
    ASK_USER = "ask_user"
    #: Autonomy is not available in this state at all. Stop asking until the
    #: session changes.
    REFUSE = "refuse"


class Consumable(StrEnum):
    """Which selection policy a need's plan defers the item choice to.

    Autonomy never picks the sandwich (AGENTS.md, "Determinism where it
    counts"); it decides *that* eating is warranted and asks
    :mod:`pz_agent_core.policy.food` which item.
    """

    FOOD = "food"
    DRINK = "drink"
    LITERATURE = "literature"


NEED_BLEEDING: Final = "bleeding"
NEED_THIRST: Final = "thirst"
NEED_HUNGER: Final = "hunger"
NEED_FATIGUE: Final = "fatigue"
NEED_BOREDOM: Final = "boredom"


@dataclass(frozen=True, slots=True)
class NeedPlan:
    """The bounded sequence of actions one need is served by."""

    need_key: str
    actions: tuple[ActionName, ...]
    risk: RiskClass
    capability: str | None
    consumable: Consumable | None = None
    #: True when serving the need can take the character off its square. Only
    #: these plans are bounded by the home radius: eating is safe anywhere,
    #: while walking somewhere is exactly what §7.10 limits.
    requires_travel: bool = False

    def __post_init__(self) -> None:
        if not self.actions:
            raise ValueError(f"{self.need_key}: a plan must name at least one action")
        if len(self.actions) > MAX_PLAN_STEPS:
            raise ValueError(
                f"{self.need_key}: a plan may hold at most {MAX_PLAN_STEPS} steps, "
                f"got {len(self.actions)}"
            )


#: Which needs the agent can actually serve on its own, and with what.
#:
#: Bleeding and fatigue are deliberately absent. Both are real needs that
#: :func:`derive_needs` reports, and neither has a verified action behind it —
#: there is no first-aid or sleep command in the protocol's action set. A need
#: with no plan escalates to the user, which is the honest answer; inventing a
#: plan that queues nothing would report "handled" for a wound that is still
#: bleeding.
NEED_PLANS: Final[Mapping[str, NeedPlan]] = MappingProxyType(
    {
        NEED_THIRST: NeedPlan(
            need_key=NEED_THIRST,
            actions=(ActionName.CONSUME_DRINK,),
            risk=RiskClass.P2,
            capability=CAPABILITY_DRINK_CARRIED,
            consumable=Consumable.DRINK,
        ),
        NEED_HUNGER: NeedPlan(
            need_key=NEED_HUNGER,
            actions=(ActionName.CONSUME_EAT,),
            risk=RiskClass.P2,
            capability=CAPABILITY_EAT_PERCENTAGE,
            consumable=Consumable.FOOD,
        ),
        NEED_BOREDOM: NeedPlan(
            need_key=NEED_BOREDOM,
            actions=(ActionName.LITERATURE_READ,),
            risk=RiskClass.P2,
            capability=CAPABILITY_READ_LITERATURE,
            consumable=Consumable.LITERATURE,
        ),
    }
)


# --------------------------------------------------------------------------
# inputs the gate cannot read off an observation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BackupEvidence:
    """Proof that this save can be restored if autonomy goes wrong.

    ``save_id`` is part of the evidence, not decoration: a backup of another
    world is not a safety net for this one, and "a backup exists somewhere" is
    the kind of claim that reads as reassurance right up until a restore is
    needed.
    """

    save_id: str
    created_at_ms: int
    #: True only when the manifest's hashes were checked, not merely listed.
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.save_id:
            raise ValueError("backup evidence must name the save it covers")
        if self.created_at_ms < 0:
            raise ValueError(f"created_at_ms must be non-negative, got {self.created_at_ms}")


class HomeSquare(Protocol):
    """A grid square, as much of one as the radius check needs.

    Structural so that this module needs no import from the memory package: the
    dependency runs the other way round in the wiring, and a policy that imported
    a store would be a policy that could not be tested without one.
    """

    @property
    def x(self) -> int: ...

    @property
    def y(self) -> int: ...

    @property
    def z(self) -> int: ...


@runtime_checkable
class AutonomyMemory(Protocol):
    """The two questions autonomy asks of memory.

    Structural, and matching :class:`~pz_agent_core.memory.store.SaveMemory`
    member for member, so the real store satisfies it with no adapter — a test
    asserts that, because a drift would otherwise only surface when the two
    halves are first wired together.
    """

    def home_point(self) -> HomeSquare | None:
        """The remembered home square, or None when none has been set."""
        ...

    def reserves_item(self, full_type: str, /) -> bool:
        """True when the user set this item type aside (§7.9)."""
        ...


@dataclass(frozen=True, slots=True)
class NoMemory:
    """What autonomy sees before anything has been remembered.

    Reserves nothing and knows no home. Permissive on purpose: an empty memory
    must not be able to *forbid* — the user's reservations are the only thing
    that does — but it also cannot vouch for a radius, so plans that travel are
    refused for want of a home point rather than allowed for want of a bound.
    """

    def home_point(self) -> HomeSquare | None:
        return None

    def reserves_item(self, full_type: str, /) -> bool:
        return False


NO_MEMORY: Final = NoMemory()


class SelectedItem(Protocol):
    """The half of a selection policy's choice that autonomy reads."""

    @property
    def item_ref(self) -> str: ...

    @property
    def display_name(self) -> str: ...


class ItemSelection(Protocol):
    """What food, drink and literature selections have in common.

    All three answer "which item, or why none", and the gate treats them
    identically. Declaring the shared shape here rather than branching on three
    concrete types keeps the reservation rule in one place for all of them.
    """

    @property
    def choice(self) -> SelectedItem | None: ...

    @property
    def reason_code(self) -> ReasonCode | None: ...

    @property
    def rejections(self) -> tuple[Rejection, ...]: ...


#: Rejections that mean "there is something, but it is spoken for". §7.9 makes
#: these an ask, not a refusal: the user may well say yes.
#:
#: A sound signal but not a complete one: the list a selection carries is bounded
#: by :attr:`PolicyConfig.max_reported_rejections`, so a reserve rejection can be
#: trimmed out of it. :meth:`AutonomyGate._reserves_are_the_blocker` therefore
#: also asks the question directly.
_RESERVED_REASONS: Final[frozenset[RejectionReason]] = frozenset(
    {
        RejectionReason.USER_RESERVED,
        RejectionReason.STRATEGIC_RESERVE,
        RejectionReason.LAST_CONTAINER_RESERVE,
    }
)


@dataclass(frozen=True, slots=True)
class AutonomyConfig:
    """Every bound the self-directed loop obeys."""

    #: §7.10: how far from the home point an autonomous plan may travel.
    home_radius: int = 30
    #: Longest plan the loop may start unattended.
    max_plan_steps: int = 4
    #: Actions per :attr:`interval_ms`. A budget rather than a cooldown so a
    #: short burst is allowed while a runaway loop is not.
    max_actions_per_interval: int = 6
    interval_ms: int = 60_000
    #: Danger at or above this leaves the tick to the reflex guard.
    danger_ceiling: DangerLevel = DangerLevel.MEDIUM
    #: Fatigue at or above this is the §17.1 critical-fatigue state.
    critical_fatigue: float = 0.80
    #: Moodle key the mod reports boredom under. The game's own enum name, which
    #: no probe has confirmed on 42.20 yet — hence a setting rather than a
    #: constant, so a wrong guess is corrected in configuration, not in code.
    boredom_moodle: str = "Bored"
    #: A backup whose hashes were never checked is not proof a restore would
    #: work. Off only for a deployment that verifies backups some other way.
    require_verified_backup: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.home_radius <= MAX_HOME_RADIUS:
            raise ValueError(f"home_radius must be within 1..{MAX_HOME_RADIUS}")
        if not 1 <= self.max_plan_steps <= MAX_PLAN_STEPS:
            raise ValueError(f"max_plan_steps must be within 1..{MAX_PLAN_STEPS}")
        if not 1 <= self.max_actions_per_interval <= MAX_ACTIONS_PER_INTERVAL:
            raise ValueError(
                f"max_actions_per_interval must be within 1..{MAX_ACTIONS_PER_INTERVAL}"
            )
        if self.interval_ms <= 0:
            raise ValueError(f"interval_ms must be positive, got {self.interval_ms}")
        if not 0.0 < self.critical_fatigue <= 1.0:
            raise ValueError(f"critical_fatigue must be within (0, 1], got {self.critical_fatigue}")
        if not self.boredom_moodle.strip():
            raise ValueError("boredom_moodle must name the moodle key the mod reports")


DEFAULT_AUTONOMY_CONFIG: Final = AutonomyConfig()


@dataclass(frozen=True, slots=True)
class AutonomyDecision:
    """The verdict for one tick, and everything needed to explain it."""

    outcome: AutonomyOutcome
    detail: str
    need: Need | None = None
    plan: NeedPlan | None = None
    item_ref: str | None = None
    reason_code: ReasonCode | None = None
    arbitration: ArbitrationDecision | None = None
    deferred: tuple[Need, ...] = ()

    def __post_init__(self) -> None:
        if not self.detail.strip():
            raise ValueError("an autonomy decision must explain itself")
        if self.outcome is AutonomyOutcome.ACT:
            if self.need is None or self.plan is None:
                raise ValueError("an ACT decision must name the need it serves and the plan")
            if self.reason_code is not None:
                raise ValueError("an ACT decision is not a refusal and carries no reason code")
        elif self.outcome in (AutonomyOutcome.REFUSE, AutonomyOutcome.ASK_USER) and (
            self.reason_code is None
        ):
            raise ValueError(f"a {self.outcome.value} decision must carry a reason code")

    @property
    def should_act(self) -> bool:
        return self.outcome is AutonomyOutcome.ACT


# --------------------------------------------------------------------------
# needs
# --------------------------------------------------------------------------


def derive_needs(
    observation: Observation,
    *,
    policy: PolicyConfig = DEFAULT_POLICY_CONFIG,
    config: AutonomyConfig = DEFAULT_AUTONOMY_CONFIG,
) -> tuple[Need, ...]:
    """The §17.1 needs this observation reports, most urgent first.

    Each need's ``state_signature`` is quantised to a twentieth. That is the
    anti-loop input: raw floats drift by a thousandth every tick, so an
    unquantised signature would read as "the situation is moving" forever and
    the brake in :class:`NeedArbiter` would never engage on anything.
    """
    player = observation.player
    needs: list[Need] = []
    if player.is_bleeding:
        bleeding = sum(1 for wound in player.wounds if wound.bleeding)
        needs.append(
            Need(
                key=NEED_BLEEDING,
                priority=Priority.CRITICAL_BLEEDING,
                state_signature=f"bleeding={bleeding}",
                message=f"{bleeding} wound(s) still bleeding",
            )
        )
    needs.extend(_thirst_need(player, policy))
    needs.extend(_hunger_need(player, policy))
    needs.extend(_fatigue_need(player, config))
    needs.extend(_boredom_need(player, policy, config))
    return tuple(sorted(needs, key=lambda need: need.priority))


def _thirst_need(player: PlayerState, policy: PolicyConfig) -> tuple[Need, ...]:
    thirst = player.thirst
    if thirst < policy.thirst_trigger:
        return ()
    critical = policy.is_thirst_critical(thirst)
    return (
        Need(
            key=NEED_THIRST,
            priority=Priority.CRITICAL_THIRST if critical else Priority.BASE_MAINTENANCE,
            state_signature=_signature("thirst", thirst),
            message=f"thirst is at {thirst:.2f}",
            # Ordinary thirst waits for the current plan; critical thirst is on
            # §7.7's act-without-asking list and may interrupt.
            preemptive=critical,
        ),
    )


def _hunger_need(player: PlayerState, policy: PolicyConfig) -> tuple[Need, ...]:
    hunger = player.hunger
    if hunger < policy.hunger_trigger:
        return ()
    critical = policy.is_hunger_critical(hunger)
    return (
        Need(
            key=NEED_HUNGER,
            priority=Priority.CRITICAL_HUNGER if critical else Priority.BASE_MAINTENANCE,
            state_signature=_signature("hunger", hunger),
            message=f"hunger is at {hunger:.2f}",
            preemptive=critical,
        ),
    )


def _fatigue_need(player: PlayerState, config: AutonomyConfig) -> tuple[Need, ...]:
    fatigue = player.fatigue
    if fatigue < config.critical_fatigue:
        return ()
    return (
        Need(
            key=NEED_FATIGUE,
            priority=Priority.EXHAUSTION,
            state_signature=_signature("fatigue", fatigue),
            message=f"fatigue is at {fatigue:.2f}",
            preemptive=False,
        ),
    )


def _boredom_need(
    player: PlayerState, policy: PolicyConfig, config: AutonomyConfig
) -> tuple[Need, ...]:
    level = player.moodles.get(config.boredom_moodle, 0)
    if level < policy.boredom_moodle_trigger:
        return ()
    return (
        Need(
            key=NEED_BOREDOM,
            priority=Priority.OPTIONAL_ACTIVITY,
            state_signature=f"boredom={level}",
            message=f"the boredom moodle is at level {level}",
            preemptive=False,
        ),
    )


def _signature(name: str, value: float) -> str:
    """Coarse rendering of a stat, for loop detection rather than display."""
    return f"{name}={int(value * 20)}"


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _ActionBudget:
    """Actions started per interval, as a ring of timestamps.

    The deque's ``maxlen`` is the cap itself, so the structure cannot grow past
    the number it enforces even if the pruning were wrong.
    """

    limit: int
    interval_ms: int
    _started: deque[int] = field(init=False, repr=False, default_factory=deque)

    def __post_init__(self) -> None:
        self._started = deque(maxlen=self.limit)

    def spent(self, now_ms: int) -> int:
        self._prune(now_ms)
        return len(self._started)

    def has_room(self, now_ms: int) -> bool:
        return self.spent(now_ms) < self.limit

    def charge(self, now_ms: int) -> None:
        self._prune(now_ms)
        self._started.append(now_ms)

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - self.interval_ms
        while self._started and self._started[0] <= cutoff:
            self._started.popleft()


class AutonomyGate:
    """Decides whether the agent starts something on its own, and what."""

    def __init__(
        self,
        *,
        config: AutonomyConfig = DEFAULT_AUTONOMY_CONFIG,
        policy: PolicyConfig = DEFAULT_POLICY_CONFIG,
        permissions: PermissionConfig = DEFAULT_PERMISSION_CONFIG,
        anti_loop: AntiLoopConfig = DEFAULT_ANTI_LOOP_CONFIG,
        arbiter: NeedArbiter | None = None,
        plans: Mapping[str, NeedPlan] = NEED_PLANS,
    ) -> None:
        mismatched = sorted(key for key, plan in plans.items() if plan.need_key != key)
        if mismatched:
            # A plan filed under a need it does not name would be served for the
            # wrong need and reported under the right one.
            raise ValueError(f"plans are keyed by need; these disagree: {mismatched}")
        self._config = config
        self._policy = policy
        # The same policy with the user's reserve rules switched off. It is only
        # ever used to answer the counterfactual "if you lifted your reserves,
        # would there be something to use?" — never to choose an item.
        self._unreserved_policy = replace(
            policy,
            user_reserve_tags=frozenset(),
            strategic_reserve_tags=frozenset(),
            treat_favorite_as_reserved=False,
        )
        self._permissions = permissions
        self._plans = MappingProxyType(dict(plans))
        self._arbiter = arbiter if arbiter is not None else NeedArbiter(anti_loop)
        self._budget = _ActionBudget(
            limit=config.max_actions_per_interval, interval_ms=config.interval_ms
        )

    @property
    def arbiter(self) -> NeedArbiter:
        """The shared arbiter, so a caller can clear a need it saw resolved."""
        return self._arbiter

    def actions_spent(self, now_ms: int) -> int:
        """Actions charged to the current interval."""
        return self._budget.spent(now_ms)

    def evaluate(
        self,
        observation: Observation,
        *,
        now_ms: int,
        backup: BackupEvidence | None,
        capabilities: CapabilityLookup = NO_CAPABILITIES,
        memory: AutonomyMemory = NO_MEMORY,
        running: RunningPlan | None = None,
    ) -> AutonomyDecision:
        """Decide this tick.

        *backup* has no default: §7.7 makes an unbacked save an ask-the-user
        condition, and a default would let a caller that never thought about
        backups get autonomy anyway. Pass None when none exists.

        Returning :attr:`AutonomyOutcome.ACT` charges the action budget, so a
        caller that asks and then does not act has still spent the tick. That is
        the safe direction: the alternative is a caller that polls the gate in a
        loop and never charges anything.
        """
        session = self._session_gate(observation, backup)
        if session is not None:
            return session
        blocked = self._tick_gate(observation, now_ms)
        if blocked is not None:
            return blocked

        needs = derive_needs(observation, policy=self._policy, config=self._config)
        arbitration = self._arbiter.arbitrate(running, needs, now_ms)
        chosen = self._arbitration_gate(arbitration)
        if chosen is not None:
            return chosen
        need = arbitration.serve
        # `_arbitration_gate` returns a decision for every case where `serve` is
        # None; this restates that for the type checker rather than assuming it.
        if need is None:
            return _hold("nothing needs attention", arbitration=arbitration)

        plan = self._plans.get(need.key)
        if plan is None:
            return AutonomyDecision(
                outcome=AutonomyOutcome.ASK_USER,
                detail=(
                    f"{need.message}, and I have no verified action for it. "
                    "This one is yours to handle."
                ),
                need=need,
                reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )

        for gate in (
            self._plan_bounds_gate(need, plan, arbitration),
            self._permission_gate(need, plan, observation, capabilities, arbitration),
            self._radius_gate(need, plan, observation, memory, arbitration),
        ):
            if gate is not None:
                return gate
        return self._commit(
            need,
            plan,
            observation=observation,
            capabilities=capabilities,
            memory=memory,
            arbitration=arbitration,
            now_ms=now_ms,
        )

    # -- gates -------------------------------------------------------------

    def _session_gate(
        self, observation: Observation, backup: BackupEvidence | None
    ) -> AutonomyDecision | None:
        """Conditions under which autonomy is simply not on offer."""
        safety = observation.safety
        if safety.mode is not SessionMode.AUTONOMOUS:
            return _refuse(
                ReasonCode.POLICY_DENIED,
                f"the session is in {safety.mode.value}; I only act unasked in AUTONOMOUS.",
            )
        if not safety.armed or safety.manual_takeover or safety.sidecar_stale:
            return _refuse(
                ReasonCode.NOT_ARMED,
                "automation is not armed and attached, so I start nothing on my own.",
            )
        if not observation.player.present or not observation.player.alive:
            return _refuse(
                ReasonCode.SESSION_TERMINATED,
                "there is no live character to act for.",
            )
        return self._backup_gate(observation, backup)

    def _backup_gate(
        self, observation: Observation, backup: BackupEvidence | None
    ) -> AutonomyDecision | None:
        if backup is None:
            return _ask(
                ReasonCode.PRECONDITION_FAILED,
                "this save has no backup. Take one and I will start working on my own; "
                "until then I will only do what you ask.",
            )
        if backup.save_id != observation.game.save_id:
            return _ask(
                ReasonCode.SAVE_CHANGED,
                "the backup I know about is of a different save, so it is not a safety "
                "net for this one.",
            )
        if self._config.require_verified_backup and not backup.verified:
            return _ask(
                ReasonCode.PRECONDITION_FAILED,
                "the backup of this save has never been verified, so I cannot promise it "
                "would restore.",
            )
        return None

    def _tick_gate(self, observation: Observation, now_ms: int) -> AutonomyDecision | None:
        """Conditions that pass, but not on this tick."""
        if observation.action.blocks_automation:
            return _hold(
                f"the action queue is running a {observation.action.ownership.value} action.",
                reason_code=ReasonCode.PLAYER_BUSY_MANUAL_ACTION,
            )
        if observation.safety.danger_level >= self._config.danger_ceiling:
            return _hold(
                f"danger is {observation.safety.danger_level.value}; the safety guard owns "
                "this tick, not a maintenance plan.",
                reason_code=ReasonCode.THREAT_INTERRUPTED,
            )
        if not self._budget.has_room(now_ms):
            return _hold(
                f"I have already started {self._config.max_actions_per_interval} action(s) in "
                f"the last {self._config.interval_ms} ms on my own.",
                reason_code=ReasonCode.POLICY_DENIED,
            )
        return None

    def _arbitration_gate(self, arbitration: ArbitrationDecision) -> AutonomyDecision | None:
        """Turn the arbiter's verdict into an autonomy decision.

        The ASK_USER outcome is the §7.8 escalation: a need that kept firing
        while the state it complains about did not move. The arbiter counted it;
        this module only has to stop acting on it.
        """
        if arbitration.outcome is Arbitration.ASK_USER:
            return AutonomyDecision(
                outcome=AutonomyOutcome.ASK_USER,
                detail=arbitration.detail,
                need=arbitration.serve,
                reason_code=ReasonCode.NO_PROGRESS,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )
        if arbitration.serve is None:
            return _hold(arbitration.detail, arbitration=arbitration)
        return None

    def _plan_bounds_gate(
        self, need: Need, plan: NeedPlan, arbitration: ArbitrationDecision
    ) -> AutonomyDecision | None:
        if len(plan.actions) > self._config.max_plan_steps:
            return AutonomyDecision(
                outcome=AutonomyOutcome.ASK_USER,
                detail=(
                    f"serving {need.key} takes {len(plan.actions)} steps and I start at most "
                    f"{self._config.max_plan_steps} unattended."
                ),
                need=need,
                plan=plan,
                reason_code=ReasonCode.POLICY_DENIED,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )
        return None

    def _permission_gate(
        self,
        need: Need,
        plan: NeedPlan,
        observation: Observation,
        capabilities: CapabilityLookup,
        arbitration: ArbitrationDecision,
    ) -> AutonomyDecision | None:
        """Run every action of the plan past the permission ladder."""
        for action in plan.actions:
            decision = check_permission(
                PermissionRequest(
                    action=action,
                    risk=plan.risk,
                    mode=observation.safety.mode,
                    initiative=Initiative.SELF_DIRECTED,
                    capability=plan.capability,
                ),
                capabilities=capabilities,
                config=self._permissions,
            )
            if decision.allowed:
                continue
            outcome = AutonomyOutcome.ASK_USER if decision.requires_grant else AutonomyOutcome.HOLD
            return AutonomyDecision(
                outcome=outcome,
                detail=decision.detail,
                need=need,
                plan=plan,
                reason_code=decision.reason_code,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )
        return None

    def _radius_gate(
        self,
        need: Need,
        plan: NeedPlan,
        observation: Observation,
        memory: AutonomyMemory,
        arbitration: ArbitrationDecision,
    ) -> AutonomyDecision | None:
        """§7.10: an autonomous plan stays inside the home radius.

        Only plans that travel are bounded. A plan that does not move the
        character cannot leave any radius, and refusing to drink because the
        player wandered off would be a bound applied to the wrong thing.

        No urgency lifts this. §7.7 lets the agent eat and drink without asking,
        and §7.10 still says leaving bounds needs an explicit goal or permission
        — a critical need is a reason to ask quickly, not a reason to wander.
        """
        if not plan.requires_travel:
            return None
        home = memory.home_point()
        if home is None:
            return AutonomyDecision(
                outcome=AutonomyOutcome.ASK_USER,
                detail=(
                    f"serving {need.key} means going somewhere and no home point is set, so "
                    "there is no radius for me to stay inside."
                ),
                need=need,
                plan=plan,
                reason_code=ReasonCode.PRECONDITION_FAILED,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )
        distance = _distance_from_home(home, observation)
        if distance is None or distance > self._config.home_radius:
            return AutonomyDecision(
                outcome=AutonomyOutcome.ASK_USER,
                detail=(
                    f"serving {need.key} would travel and we are outside the "
                    f"{self._config.home_radius}-square home radius."
                ),
                need=need,
                plan=plan,
                reason_code=ReasonCode.TARGET_OUT_OF_RANGE,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )
        return None

    def _commit(
        self,
        need: Need,
        plan: NeedPlan,
        *,
        observation: Observation,
        capabilities: CapabilityLookup,
        memory: AutonomyMemory,
        arbitration: ArbitrationDecision,
        now_ms: int,
    ) -> AutonomyDecision:
        """Choose the item, honour the reservations, and charge the budget."""
        item_ref: str | None = None
        if plan.consumable is not None:
            resolved = self._consumable_gate(
                need,
                plan,
                consumable=plan.consumable,
                observation=observation,
                capabilities=capabilities,
                memory=memory,
                arbitration=arbitration,
            )
            if isinstance(resolved, AutonomyDecision):
                return resolved
            item_ref = resolved
        self._budget.charge(now_ms)
        return AutonomyDecision(
            outcome=AutonomyOutcome.ACT,
            detail=f"{need.message}; serving it with {plan.actions[0].value}.",
            need=need,
            plan=plan,
            item_ref=item_ref,
            arbitration=arbitration,
            deferred=arbitration.deferred,
        )

    def _consumable_gate(
        self,
        need: Need,
        plan: NeedPlan,
        *,
        consumable: Consumable,
        observation: Observation,
        capabilities: CapabilityLookup,
        memory: AutonomyMemory,
        arbitration: ArbitrationDecision,
    ) -> AutonomyDecision | str:
        """Pick the item, or explain why there is nothing to pick.

        Items the user reserved are removed *before* the selection runs, so a
        reserved item cannot be chosen even by a policy that would otherwise
        have scored it best. When that removal is what left nothing to choose,
        the answer is a question rather than a refusal: the user may well say
        yes, and going hungry beside a full shelf without mentioning it is not
        a service.
        """
        inventory = observation.inventory
        if inventory is None:
            return _hold(
                "the mod reported no inventory this tick, so I cannot tell what is available.",
                reason_code=ReasonCode.CAPABILITY_UNAVAILABLE,
                need=need,
                plan=plan,
                arbitration=arbitration,
            )
        permitted = _split_reserved(inventory, memory)
        selection = _select(consumable, permitted, observation.player, self._policy, capabilities)
        choice = selection.choice
        if choice is not None:
            return choice.item_ref
        if self._reserves_are_the_blocker(
            consumable,
            inventory=inventory,
            observation=observation,
            capabilities=capabilities,
            selection=selection,
        ):
            return AutonomyDecision(
                outcome=AutonomyOutcome.ASK_USER,
                detail=(
                    f"{need.message}, and everything that would serve it is reserved. "
                    "Say the word and I will use one."
                ),
                need=need,
                plan=plan,
                reason_code=ReasonCode.RESOURCE_RESERVED,
                arbitration=arbitration,
                deferred=arbitration.deferred,
            )
        return _hold(
            f"{need.message}, and there is nothing safe to use for it.",
            reason_code=selection.reason_code or ReasonCode.PRECONDITION_FAILED,
            need=need,
            plan=plan,
            arbitration=arbitration,
        )

    def _reserves_are_the_blocker(
        self,
        consumable: Consumable,
        *,
        inventory: InventoryView,
        observation: Observation,
        capabilities: CapabilityLookup,
        selection: ItemSelection,
    ) -> bool:
        """Would lifting the user's reserves actually give the agent something?

        Asked by re-running the selection over the whole inventory with the
        reserve rules switched off, rather than by taking the *presence* of a
        reserved item as proof. A reserved axe is not an answer to hunger: it
        would leave the agent telling the user "everything that would serve it
        is reserved. Say the word and I will use one" when saying the word would
        produce nothing to eat — a sentence that reads as a fact and is not one.

        The reported rejections are consulted first because they catch the two
        reserve shapes the counterfactual cannot switch off (an item flagged
        ``reserved`` in its own data, and the drink policy's last-container
        rule); the counterfactual then catches what a bounded rejection list
        left out.
        """
        if any(rejection.reason in _RESERVED_REASONS for rejection in selection.rejections):
            return True
        lifted = _select(
            consumable, inventory, observation.player, self._unreserved_policy, capabilities
        )
        return lifted.choice is not None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _split_reserved(inventory: InventoryView, memory: AutonomyMemory) -> InventoryView:
    """The inventory with every item type the user reserved removed.

    Removal happens *before* the selection policy runs, so a reserved item
    cannot be chosen even by a policy that would have scored it best. The
    original view is returned unchanged when nothing is reserved, so the common
    case allocates nothing.
    """
    permitted = [item for item in inventory.items if not memory.reserves_item(item.full_type)]
    if len(permitted) == len(inventory.items):
        return inventory
    return InventoryView(containers=list(inventory.containers), items=permitted)


def _select(
    consumable: Consumable,
    inventory: InventoryView,
    player: PlayerState,
    policy: PolicyConfig,
    capabilities: CapabilityLookup,
) -> ItemSelection:
    """Defer the item choice to the deterministic selection policy."""
    if consumable is Consumable.FOOD:
        return select_food(inventory, player, policy, capabilities=capabilities)
    if consumable is Consumable.DRINK:
        return select_drink(inventory, player, policy, capabilities=capabilities)
    return select_literature(inventory, player, LiteratureGoal.boredom(), policy)


def _distance_from_home(home: HomeSquare, observation: Observation) -> int | None:
    """Chebyshev distance from the remembered home square, in squares.

    None when the character is on a different floor. A home point one storey
    down is not "zero squares away" in any sense a travel budget can use, and
    the caller treats None as outside the radius — the safe direction.
    """
    position = observation.player.position
    if home.z != position.z:
        return None
    return max(abs(int(position.x) - home.x), abs(int(position.y) - home.y))


def _hold(
    detail: str,
    *,
    reason_code: ReasonCode | None = None,
    need: Need | None = None,
    plan: NeedPlan | None = None,
    arbitration: ArbitrationDecision | None = None,
) -> AutonomyDecision:
    return AutonomyDecision(
        outcome=AutonomyOutcome.HOLD,
        detail=detail,
        need=need,
        plan=plan,
        reason_code=reason_code,
        arbitration=arbitration,
        deferred=() if arbitration is None else arbitration.deferred,
    )


def _refuse(reason_code: ReasonCode, detail: str) -> AutonomyDecision:
    return AutonomyDecision(outcome=AutonomyOutcome.REFUSE, detail=detail, reason_code=reason_code)


def _ask(reason_code: ReasonCode, detail: str) -> AutonomyDecision:
    return AutonomyDecision(
        outcome=AutonomyOutcome.ASK_USER, detail=detail, reason_code=reason_code
    )
