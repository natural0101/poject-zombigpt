"""The needs arbiter: interrupt the current goal, satisfy the need, resume.

The directive this module serves is the interruption half of autonomy («когда
нужно прервать текущую цель … продолжить исходную цель»): a survivor mid-loot
whose hunger crosses the critical line eats *now* and goes back to the crate
list afterwards. The queue already knows how to step a goal aside and bring it
back (:meth:`~pz_agent_core.goals.GoalQueue.suspend` /
:meth:`~pz_agent_core.goals.GoalQueue.submit_front`); this module is the one
place that decides *when* those levers are pulled, and it is deterministic
policy code on the wrapper's side of the planner seam — no model call anywhere
near the decision, exactly as AGENTS.md's "priority arbitration … is
deterministic policy code" requires.

Event-driven, never per-tick
----------------------------

The arbiter reacts to threshold **crossings** between consecutive observations
it has seen, never to levels: hunger *rising through* the policy's critical
line, bleeding *appearing*, danger *reaching* the avoid rung. A character who
sits at critical hunger for a hundred ticks triggers once, when the line was
crossed, and once more only after the need was satisfied and crossed the line
again. The first observation ever seen can trigger nothing — a crossing is a
claim about two readings, and there is only one. A stat the build does not
report (``None``) is "no reading", which can neither cross nor be crossed;
inventing a zero there would manufacture a crossing out of a missing reader.
The last-seen readings are one small frozen record, replaced whole — bounded
state, not a history.

Only autonomy preempts
----------------------

Preemption happens only when the observation's own safety block reports
``mode=AUTONOMOUS`` (``observation.safety.mode`` — the mod's word for what the
session is, the same field the reflex guard trusts). ASSISTED autonomy *asks*:
its plan provider may propose a meal and the user confirms it; a mode whose
whole contract is "nothing happens without you" must not have goals reshuffled
under the user's feet. The arbiter still tracks readings in every mode, so a
flip into AUTONOMOUS cannot manufacture a crossing out of values that moved
while it was not allowed to act.

The priority table, and why this order
--------------------------------------

One trigger is served per crossing tick, the highest of:

1. **Bleeding → treat_wounds.** Blood loss is the only trigger already costing
   health on every tick it stands; every other need kills on a slower clock,
   and the threat ladder itself escalates around a bleeding character.
2. **Danger reaching HIGH, with no chasing zombie observed → avoid_threat.**
   Distance decays; needs do not. Below bleeding because a bandage is one
   action and retreating while dripping marks a trail. The no-chasing
   qualifier is the reflex guard's jurisdiction line: a *chasing* threat at
   HIGH is already the guard's stop/flee band (its ``block_at`` rung starts
   nothing new and its ``flee_at`` rung stops everything), and injecting a
   retreat goal underneath the guard would be a second driver fighting the
   wheel. What this trigger serves is the picture the guard's rungs do not
   drive anywhere: a crowd or a mod-reported HIGH with nothing actually
   chasing yet — retreat now, while walking away still works.
3. **Thirst past the policy's critical line → satisfy_thirst.** Dehydration
   outpaces starvation, so of the two needs it goes first.
4. **Hunger past the policy's critical line → satisfy_hunger.**

The critical lines are the policy's own
(:attr:`~pz_agent_core.policy.config.PolicyConfig.critical_hunger` /
``critical_thirst``), read through a provider each time so the user's config —
not a restated constant — decides where critical is. The danger reading is the
higher of the observed assessment (:func:`~pz_agent_core.safety.threat.
assess_threat` over the same nearby view the reflex guard reads) and the mod's
own ``safety.danger_level``, exactly the maximum the guard takes; with no
nearby view the mod's word is the only honest input.

How a preemption runs
---------------------

One preemption is in flight at a time, tracked by the injected goal's id.
On a crossing, under the host's own goal lock:

* **check** — a preemptor needs a free ``max_open`` slot (suspension frees
  none); with the channel full the arbiter stands down and the ledger says so.
* **suspend** — the active goal, when one exists, through the queue's own
  lever. Every typed refusal is a stand-down, recorded: the fourth suspension
  (``QUEUE_REJECTED``) means the goal has been interrupted enough and runs to
  its own end; a user's outstanding cancel (``CANCELLED_BY_REQUEST``) means
  user input already won. A goal already serving the triggered kind is left
  alone — suspending a meal to inject a meal is churn, not arbitration.
* **inject** — the needs goal through :meth:`~pz_agent_core.goals.GoalQueue.
  submit_front`, with the deterministic idempotency key
  ``arb.<original>.<trigger>.<n>`` (the suspended goal's id, or ``idle`` when
  none was running; ``n`` the arbiter's own injection counter, so a refused
  injection never replays a key). The delimiter is ``.`` rather than ``:`` on
  purpose: this same string is passed to ``suspend`` as ``by_goal_id`` — the
  marker readable on the parked record — and the status boundary publishes
  ``suspended_by`` as a *token*, whose closed shape has no colon. The queue
  mints the preemptor's real id only at admission, after the suspension has
  already happened (suspend-then-inject is what puts the preemptor's front
  rank ahead of the parked goal's), so the key is the one deterministic name
  for the preemptor that exists on both sides of that ordering.
* **resume is nobody's job** — the parked goal sits at the front of the
  backlog behind the preemptor, and when the preemptor reaches *any* terminal
  state — success, failure, expiry, a cancel — the loop's ordinary activation
  picks the original up again, wall clock banked, steps intact, and (the
  wrapper's half of the promise) its mission drive still holding the
  candidate list it was suspended over. A failed preemptor therefore cannot
  orphan the original: nothing about resumption depends on how the preemptor
  ended. With no goal active the injection is an ordinary front submission —
  autonomy answering its own needs — and there is nothing to resume.

Honesty ledger
--------------

Every arbitration decision — a preemption started, a stand-down, a refused
injection — lands in a bounded ledger (:attr:`NeedsArbiter.log`, newest last,
capped at :data:`MAX_ARBITER_LOG`): ``{trigger, suspended_goal,
injected_goal, at_seq, outcome}``. A started preemption's outcome opens as
``pending`` and is settled with the injected goal's own terminal state the
next time the arbiter sees the channel — or ``forgotten`` when the channel no
longer remembers the goal at all (a restart under the preemption). Entries are
immutable and replaced whole, so a status thread reading the tuple sees one
decision's truth or the next one's, never a torn one.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pz_agent_core.goals import GoalKind, GoalRecord, GoalRequest
from pz_agent_core.policy.config import PolicyConfig
from pz_agent_core.protocol import DangerLevel, JsonDict, Observation, SessionMode
from pz_agent_core.safety.threat import DEFAULT_THREAT_CONFIG, ThreatConfig, assess_threat

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, types only
    from .navigation_planner import NavigationHost

__all__ = [
    "AVOID_AT",
    "MAX_ARBITER_LOG",
    "OUTCOME_FORGOTTEN",
    "OUTCOME_INJECT_REFUSED",
    "OUTCOME_PENDING",
    "OUTCOME_STOOD_DOWN",
    "TRIGGER_BLEEDING",
    "TRIGGER_HUNGER",
    "TRIGGER_KINDS",
    "TRIGGER_PRIORITY",
    "TRIGGER_THIRST",
    "TRIGGER_THREAT",
    "NeedsArbiter",
]

#: Arbitration decisions remembered for reading back. Sixteen covers a long
#: session's interruptions while keeping the ledger a screen, not a leak.
MAX_ARBITER_LOG: Final = 16

#: The danger rung at which the avoid trigger fires. The reflex guard's own
#: ``block_at`` sits at the same rung by design: whichever layer sees the
#: picture first acts, the guard by refusing to start work, this arbiter by
#: queueing the retreat that runs when work may start again.
AVOID_AT: Final = DangerLevel.HIGH

#: The closed trigger tokens. They land in idempotency keys, suspension
#: reasons and the ledger, so they are constants here and nowhere else.
TRIGGER_BLEEDING: Final = "bleeding"
TRIGGER_THREAT: Final = "threat"
TRIGGER_THIRST: Final = "thirst"
TRIGGER_HUNGER: Final = "hunger"

#: Highest first; the table the module docstring argues for.
TRIGGER_PRIORITY: Final[tuple[str, ...]] = (
    TRIGGER_BLEEDING,
    TRIGGER_THREAT,
    TRIGGER_THIRST,
    TRIGGER_HUNGER,
)

#: Which goal kind answers which trigger. All four kinds admit a bare request
#: — no parameters — which is exactly what an arbiter should submit: *which*
#: wound, *which* sandwich and *where* to retreat are the missions' own
#: deterministic decisions, not this table's.
TRIGGER_KINDS: Final[dict[str, GoalKind]] = {
    TRIGGER_BLEEDING: GoalKind.TREAT_WOUNDS,
    TRIGGER_THREAT: GoalKind.AVOID_THREAT,
    TRIGGER_THIRST: GoalKind.SATISFY_THIRST,
    TRIGGER_HUNGER: GoalKind.SATISFY_HUNGER,
}

#: Ledger outcome tokens. A pending preemption settles to the injected goal's
#: own terminal state value (``succeeded``/``failed``/``cancelled``/
#: ``expired``), which is the queue's vocabulary, not restated here.
OUTCOME_PENDING: Final = "pending"
OUTCOME_STOOD_DOWN: Final = "stood_down"
OUTCOME_INJECT_REFUSED: Final = "inject_refused"
OUTCOME_FORGOTTEN: Final = "forgotten"


@dataclass(frozen=True, slots=True)
class _Readings:
    """The last-seen values crossings are measured against. One frozen record,
    replaced whole per observation — the arbiter's entire memory of the world."""

    seq: int
    hunger: float | None
    thirst: float | None
    bleeding: bool
    danger: DangerLevel
    chasing: bool


@dataclass(frozen=True, slots=True)
class _Preemption:
    """The one preemption in flight: which goal was injected, under which
    ledger entry, so its terminal state can settle the entry later."""

    injected_goal_id: str
    entry_key: int


class NeedsArbiter:
    """Crossing detection plus the suspend/inject/settle choreography.

    ``policy`` is a provider rather than a value because the wrapper resolves
    its policy off the wrapped planner chain per call — the same late binding
    the consume mission gets, so the critical lines this arbiter reads are
    always the ones the missions aim for.
    """

    def __init__(
        self,
        *,
        policy: Callable[[], PolicyConfig],
        threat_config: ThreatConfig = DEFAULT_THREAT_CONFIG,
    ) -> None:
        self._policy = policy
        self._threat_config = threat_config
        self._last: _Readings | None = None
        self._in_flight: _Preemption | None = None
        #: Injection attempts ever made; the ``n`` in every key it mints.
        self._injections = 0
        #: entry key -> ledger entry, oldest first, capped. Entries are
        #: replaced whole on settlement (never mutated), so an unlocked
        #: reader of :attr:`log` sees each decision's before or after.
        self._log: OrderedDict[int, JsonDict] = OrderedDict()

    @property
    def log(self) -> tuple[JsonDict, ...]:
        """The bounded arbitration ledger, oldest first. Copies on the way out."""
        return tuple(dict(entry) for entry in self._log.values())

    def observe(self, observation: Observation, host: NavigationHost | None) -> None:
        """Fold one observation in; preempt when a crossing and the mode allow.

        Called by the wrapper once per observation it is handed, on either
        planner path. Takes ``host.goal_lock`` only for the queue touches, in
        the same short-hold discipline as every other wrapper call.
        """
        current = self._read(observation)
        previous = self._last
        self._last = current
        queue = None if host is None else host.goals
        if queue is not None and self._in_flight is not None:
            assert host is not None  # queue came off it
            with host.goal_lock:
                record = queue.record(self._in_flight.injected_goal_id)
            self._settle(record)
        if previous is None or previous.seq == current.seq:
            # Nothing to cross against: the first reading ever, or the same
            # observation handed twice by a tick that learned nothing new.
            return
        trigger = self._crossed(previous, current)
        if trigger is None:
            return
        if observation.safety.mode is not SessionMode.AUTONOMOUS:
            # ASSISTED autonomy asks; it does not preempt. The crossing was
            # still consumed above — the readings moved — so a later mode
            # flip cannot replay it.
            return
        if queue is None or self._in_flight is not None:
            # Nowhere to act, or one preemption already running: one at a
            # time is the bound that keeps arbitration from cascading.
            return
        assert host is not None  # queue came off it
        with host.goal_lock:
            self._preempt(host, trigger, at_seq=current.seq)

    # -- readings and crossings -------------------------------------------

    def _read(self, observation: Observation) -> _Readings:
        player = observation.player
        nearby = observation.nearby
        if nearby is None:
            # No world data: the mod's own assessment is the only honest
            # input, exactly the reflex guard's reading of the same absence.
            danger = observation.safety.danger_level
            chasing = False
        else:
            assessment = assess_threat(nearby, player, self._threat_config)
            danger = max(assessment.level, observation.safety.danger_level)
            chasing = assessment.chasing_count > 0
        return _Readings(
            seq=observation.seq,
            hunger=_stat(player.stats.get("hunger")),
            thirst=_stat(player.stats.get("thirst")),
            bleeding=player.is_bleeding,
            danger=danger,
            chasing=chasing,
        )

    def _crossed(self, previous: _Readings, current: _Readings) -> str | None:
        """The highest-priority trigger that crossed between the two readings."""
        policy = self._policy()
        if not previous.bleeding and current.bleeding:
            return TRIGGER_BLEEDING
        if previous.danger < AVOID_AT <= current.danger and not current.chasing:
            return TRIGGER_THREAT
        if (
            previous.thirst is not None
            and current.thirst is not None
            and not policy.is_thirst_critical(previous.thirst)
            and policy.is_thirst_critical(current.thirst)
        ):
            return TRIGGER_THIRST
        if (
            previous.hunger is not None
            and current.hunger is not None
            and not policy.is_hunger_critical(previous.hunger)
            and policy.is_hunger_critical(current.hunger)
        ):
            return TRIGGER_HUNGER
        return None

    # -- the preemption itself (goal lock held by the caller) --------------

    def _preempt(self, host: NavigationHost, trigger: str, *, at_seq: int) -> None:
        queue = host.goals
        assert queue is not None  # the caller checked before taking the lock
        kind = TRIGGER_KINDS[trigger]
        active = queue.active
        if active is not None and active.kind is kind:
            # The need is already being served; suspending its server to
            # inject a copy would be churn. Recorded, because a trigger that
            # fired and did nothing is still a decision somebody may ask about.
            self._record(
                trigger,
                suspended=None,
                injected=None,
                at_seq=at_seq,
                outcome=f"{OUTCOME_STOOD_DOWN}:already_serving",
            )
            return
        if queue.open_count >= queue.max_open:
            # Suspension frees no slot, so the preemptor cannot be admitted;
            # suspending first would park the original for nothing.
            self._record(
                trigger,
                suspended=None,
                injected=None,
                at_seq=at_seq,
                outcome=f"{OUTCOME_STOOD_DOWN}:channel_full",
            )
            return
        self._injections += 1
        original = active.goal_id if active is not None else "idle"
        key = f"arb.{original}.{trigger}.{self._injections}"
        if active is not None:
            # ``now_ms=0``: the queue monotonises (max of its own clock and
            # the argument), so zero means "your own last reading stands".
            # The observation's timestamp is the game process's clock, and
            # feeding a foreign clock into the queue's deadline arithmetic
            # is exactly what that maximum exists to make harmless.
            parked = queue.suspend(
                active.goal_id,
                by_goal_id=key,
                reason=f"needs arbiter: {trigger} crossed its threshold",
                now_ms=0,
            )
            if parked.refusal is not None:
                # A fourth suspension, a user's outstanding cancel, a race
                # with a terminal transition: every one means do not preempt.
                # The goal runs to its own end and the ledger says we stood
                # down and why, in the refusal's own vocabulary.
                self._record(
                    trigger,
                    suspended=None,
                    injected=None,
                    at_seq=at_seq,
                    outcome=f"{OUTCOME_STOOD_DOWN}:{parked.refusal.reason_code.value}",
                )
                return
        injected = queue.submit_front(GoalRequest(kind=kind, idempotency_key=key), now_ms=0)
        if injected.goal is None:
            # Refused after the slot check — only reachable through a caller
            # bug or a duplicate key, and the fresh counter forbids the
            # second. Not silent either way: the suspended goal (if any)
            # sits at the front of the backlog and ordinary activation
            # resumes it, so a failed injection strands nothing.
            self._record(
                trigger,
                suspended=None if active is None else active.goal_id,
                injected=None,
                at_seq=at_seq,
                outcome=OUTCOME_INJECT_REFUSED,
            )
            return
        entry_key = self._record(
            trigger,
            suspended=None if active is None else active.goal_id,
            injected=injected.goal.goal_id,
            at_seq=at_seq,
            outcome=OUTCOME_PENDING,
        )
        self._in_flight = _Preemption(injected_goal_id=injected.goal.goal_id, entry_key=entry_key)

    # -- settlement and the ledger -----------------------------------------

    def _settle(self, record: GoalRecord | None) -> None:
        """Close the in-flight preemption once its goal has ended."""
        preemption = self._in_flight
        assert preemption is not None  # the caller checked
        if record is not None and not record.state.is_terminal:
            return
        outcome = OUTCOME_FORGOTTEN if record is None else record.state.value
        entry = self._log.get(preemption.entry_key)
        if entry is not None:
            # Replaced whole, never mutated: an unlocked reader of the log
            # sees the pending entry or the settled one, nothing in between.
            self._log[preemption.entry_key] = {**entry, "outcome": outcome}
        self._in_flight = None

    def _record(
        self,
        trigger: str,
        *,
        suspended: str | None,
        injected: str | None,
        at_seq: int,
        outcome: str,
    ) -> int:
        # Entry keys are their own monotonic counter: one above the newest
        # key still held. Eviction only drops from the front, so a settled
        # entry's key can never be reissued while the entry could still be
        # looked up.
        key = 1 + (next(reversed(self._log)) if self._log else 0)
        self._log[key] = {
            "trigger": trigger,
            "suspended_goal": suspended,
            "injected_goal": injected,
            "at_seq": at_seq,
            "outcome": outcome,
        }
        while len(self._log) > MAX_ARBITER_LOG:
            self._log.popitem(last=False)
        return key


def _stat(value: object) -> float | None:
    """A numeric stat reading, or None for anything that is not one.

    ``bool`` is excluded on purpose — it is an ``int`` to ``isinstance`` and
    a lie to arithmetic — and ``None`` stays ``None``: an unreported need has
    no reading, and a reading is what a crossing is made of.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
