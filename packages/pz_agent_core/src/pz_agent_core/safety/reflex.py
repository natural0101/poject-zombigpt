"""The deterministic guard: no LLM, no IO, no opinions.

:meth:`ReflexGuard.evaluate` is a pure function of two observations and a small
record of out-of-band signals. That is not a stylistic preference — §7.1 puts
reflexes below the planner precisely so they keep working when the planner is
absent, slow, unreachable or disabled, and a function that reads a file or calls
a provider cannot make that promise. ``provider = "none"`` must be as safe as
``provider = "anthropic"``, and this module is why.

Three invariants:

* **A returned event means "start no new task".** Every rule here fires on
  something that makes the next command unsafe or pointless, so the caller needs
  no per-code table to know that much. What varies is how much *more* to do,
  which the booleans on :class:`SafetyEvent` say explicitly.
* **The mod's queue is the only queue this guard will touch.** §8.1 and §8.12:
  a panic stop clears mod-owned entries and never an action the player queued.
  ``cancels_running_action`` is therefore gated on
  :func:`may_cancel_running_action`, in one place, for every rule.
* **Terminal conditions are level-triggered, transitions are edge-triggered.**
  A dead character or a lost game must be caught on every tick, including the
  first one after a reconnect where there is no previous observation to compare
  against. A save id or session id *changing* has no meaning without a previous
  observation, so those rules need one.

Messages are assembled from constants and numbers only: the voice adapter speaks
them aloud, so no ref, no path and no game-authored text may reach one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..protocol import (
    ActionOwnership,
    ActionState,
    DangerLevel,
    Observation,
    Priority,
    ReasonCode,
)
from .threat import DEFAULT_THREAT_CONFIG, ThreatConfig, assess_danger

#: Upper bound on one evaluation's output. The rule set is finite, but the
#: bound is stated rather than assumed so that adding a per-command rule later
#: cannot turn a stuck queue into an unbounded event list.
MAX_EVENTS: Final = 12

#: Long, interruptible activities during which a threat must pre-empt: §17.2's
#: "visible zombie near during read/eat → interrupt". Matched against
#: ``ActionState.type``, which the mod fills with the action name it queued.
DEFAULT_VULNERABLE_ACTIONS: Final = frozenset({"consume.eat", "consume.drink", "literature.read"})

#: Rung each reason code occupies in the §"Иерархия приоритетов" ladder.
#: Lifecycle faults sit at PANIC_STOP because none of them leaves any safe work
#: to do; plan-level faults sit at USER_COMMAND so they pre-empt background
#: maintenance without ever outranking a real emergency.
_PRIORITY: Final[dict[ReasonCode, Priority]] = {
    ReasonCode.PANIC_STOP: Priority.PANIC_STOP,
    ReasonCode.SESSION_TERMINATED: Priority.PANIC_STOP,
    ReasonCode.GAME_DISCONNECTED: Priority.PANIC_STOP,
    ReasonCode.SAVE_CHANGED: Priority.PANIC_STOP,
    ReasonCode.STALE_SESSION: Priority.PANIC_STOP,
    ReasonCode.USER_TAKEOVER: Priority.MANUAL_INPUT,
    ReasonCode.PLAYER_BUSY_MANUAL_ACTION: Priority.MANUAL_INPUT,
    ReasonCode.THREAT_INTERRUPTED: Priority.LETHAL_THREAT,
    ReasonCode.LEASE_EXPIRED: Priority.USER_COMMAND,
    ReasonCode.NO_PROGRESS: Priority.USER_COMMAND,
    ReasonCode.PATH_STUCK: Priority.USER_COMMAND,
}


@dataclass(frozen=True, slots=True)
class SafetyEvent:
    """One thing the guard noticed, and what it authorises.

    ``command_ids`` names the in-flight commands the event terminates; it is
    empty for events that are about the session rather than about a command.
    """

    priority: Priority
    reason_code: ReasonCode
    message: str
    forces_disarm: bool = False
    cancels_mod_owned_queue: bool = False
    cancels_running_action: bool = False
    command_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("a safety event must carry a message that can be spoken aloud")


@dataclass(frozen=True, slots=True)
class InFlightCommand:
    """A command the executor has sent and not yet seen a terminal ack for."""

    command_id: str
    action: str
    deadline_ms: int
    last_progress_ms: int
    moves_character: bool = False


@dataclass(frozen=True, slots=True)
class ReflexSignals:
    """What the guard cannot read off an observation.

    Heartbeats live in files and the panic hotkey is handled inside Lua, so both
    reach the guard as already-decided booleans. Keeping them as inputs rather
    than as reads is what makes :meth:`ReflexGuard.evaluate` testable without a
    filesystem and safe to call from anywhere.
    """

    now_ms: int
    panic_requested: bool = False
    game_alive: bool = True
    sidecar_alive: bool = True
    in_flight: tuple[InFlightCommand, ...] = ()


@dataclass(frozen=True, slots=True)
class ReflexConfig:
    """Thresholds for the guard. Everything tunable lives here."""

    threat: ThreatConfig = DEFAULT_THREAT_CONFIG
    #: Danger at or above this interrupts a vulnerable activity.
    interrupt_at: DangerLevel = DangerLevel.MEDIUM
    #: Danger at or above this interrupts whatever is running, including
    #: nothing: at this point the only correct plan is to deal with the threat.
    flee_at: DangerLevel = DangerLevel.CRITICAL
    #: A command that has not progressed for this long is stuck. Well above the
    #: longest single timed action the engine issues, so a slow eat is not a
    #: stall.
    stall_ms: int = 15_000
    vulnerable_actions: frozenset[str] = DEFAULT_VULNERABLE_ACTIONS
    max_events: int = MAX_EVENTS

    def __post_init__(self) -> None:
        if self.stall_ms <= 0:
            raise ValueError(f"stall_ms must be positive, got {self.stall_ms}")
        if not 1 <= self.max_events <= MAX_EVENTS:
            raise ValueError(f"max_events must be within 1..{MAX_EVENTS}, got {self.max_events}")
        if self.interrupt_at > self.flee_at:
            raise ValueError("interrupt_at must not be stricter than flee_at")


DEFAULT_REFLEX_CONFIG: Final = ReflexConfig()


def may_cancel_running_action(action: ActionState) -> bool:
    """True only when the running action is provably the mod's own.

    ``ambiguous`` counts as the player's. §8.12: under uncertainty the character
    stays under manual control, which means the guard leaves the queue alone.
    """
    return action.busy and action.ownership is ActionOwnership.MOD


@dataclass(frozen=True, slots=True)
class ReflexGuard:
    """Evaluates the deterministic safety rules over consecutive observations."""

    config: ReflexConfig = DEFAULT_REFLEX_CONFIG

    def evaluate(
        self,
        previous: Observation | None,
        current: Observation,
        signals: ReflexSignals | None = None,
    ) -> list[SafetyEvent]:
        """Every rule that fires this tick, most urgent first.

        *previous* may be None on the first observation of a session; the rules
        that need a transition simply do not fire, while every terminal rule
        still does.
        """
        moment = ReflexSignals(now_ms=current.timestamp_ms) if signals is None else signals
        may_cancel = may_cancel_running_action(current.action)
        events: list[SafetyEvent] = []

        events.extend(self._lifecycle(previous, current, moment, may_cancel))
        events.extend(self._takeover(previous, current, may_cancel))
        events.extend(self._threat(current, may_cancel))
        events.extend(self._commands(previous, current, moment))

        events.sort(key=lambda event: event.priority)
        return _first_per_reason(events)[: self.config.max_events]

    # -- rules ---------------------------------------------------------------

    def _lifecycle(
        self,
        previous: Observation | None,
        current: Observation,
        signals: ReflexSignals,
        may_cancel: bool,
    ) -> list[SafetyEvent]:
        events: list[SafetyEvent] = []
        in_flight = tuple(command.command_id for command in signals.in_flight)

        if signals.panic_requested:
            events.append(
                _event(
                    ReasonCode.PANIC_STOP,
                    "Panic stop. Disarming and clearing everything I queued.",
                    forces_disarm=True,
                    cancels_mod_owned_queue=True,
                    cancels_running_action=may_cancel,
                    command_ids=in_flight,
                )
            )
        if not current.player.alive:
            events.append(
                _event(
                    ReasonCode.SESSION_TERMINATED,
                    "The character has died. Ending the session.",
                    forces_disarm=True,
                    cancels_mod_owned_queue=True,
                    command_ids=in_flight,
                )
            )
        elif not current.player.present:
            events.append(
                _event(
                    ReasonCode.SESSION_TERMINATED,
                    "The character is no longer in the world. Ending the session.",
                    forces_disarm=True,
                    cancels_mod_owned_queue=True,
                    command_ids=in_flight,
                )
            )
        if not signals.game_alive:
            events.append(
                _event(
                    ReasonCode.GAME_DISCONNECTED,
                    "Lost contact with the game. Closing anything still in flight as lost.",
                    forces_disarm=True,
                    command_ids=in_flight,
                )
            )
        if current.safety.sidecar_stale or not signals.sidecar_alive:
            events.append(
                _event(
                    ReasonCode.STALE_SESSION,
                    "The game has stopped seeing me. I will not start anything new "
                    "until the link recovers.",
                )
            )
        if previous is None:
            return events

        if previous.session_id != current.session_id:
            events.append(
                _event(
                    ReasonCode.STALE_SESSION,
                    "The session was replaced. Every reference I held is invalid.",
                    forces_disarm=True,
                    command_ids=in_flight,
                )
            )
        elif previous.game.save_id != current.game.save_id:
            events.append(
                _event(
                    ReasonCode.SAVE_CHANGED,
                    "The save changed. Every reference I held is invalid; re-arm when ready.",
                    forces_disarm=True,
                    command_ids=in_flight,
                )
            )
        return events

    def _takeover(
        self,
        previous: Observation | None,
        current: Observation,
        may_cancel: bool,
    ) -> list[SafetyEvent]:
        events: list[SafetyEvent] = []
        manual_now = current.action.blocks_automation
        manual_before = previous is not None and previous.action.blocks_automation

        if current.safety.manual_takeover:
            events.append(
                _event(
                    ReasonCode.USER_TAKEOVER,
                    "You took over. Cancelling my automation.",
                    cancels_mod_owned_queue=True,
                    cancels_running_action=may_cancel,
                )
            )
        if manual_now and not manual_before:
            events.append(
                _event(
                    ReasonCode.USER_TAKEOVER,
                    "An action I did not queue started. Cancelling my automation "
                    "and leaving yours alone.",
                    cancels_mod_owned_queue=True,
                    cancels_running_action=may_cancel,
                )
            )
        elif manual_now:
            events.append(
                _event(
                    ReasonCode.PLAYER_BUSY_MANUAL_ACTION,
                    "You are still busy with your own action. Waiting.",
                )
            )
        return events

    def _threat(self, current: Observation, may_cancel: bool) -> list[SafetyEvent]:
        if current.nearby is None:
            # No world data this tick: the mod's own assessment is the only
            # honest input, and inventing NONE from missing data is exactly the
            # fabrication this project forbids.
            danger = current.safety.danger_level
            reason = "reported by the game"
        else:
            observed = assess_danger(current.nearby, current.player, self.config.threat)
            danger = max(observed, current.safety.danger_level)
            reason = "zombies nearby"

        running_type = current.action.type or ""
        vulnerable = (
            may_cancel and current.action.busy and running_type in self.config.vulnerable_actions
        )
        if danger >= self.config.flee_at:
            return [
                _event(
                    ReasonCode.THREAT_INTERRUPTED,
                    f"Danger is {danger.value} ({reason}). Stopping everything.",
                    cancels_mod_owned_queue=True,
                    cancels_running_action=may_cancel,
                )
            ]
        if vulnerable and danger >= self.config.interrupt_at:
            return [
                _event(
                    ReasonCode.THREAT_INTERRUPTED,
                    f"Danger is {danger.value} ({reason}). Interrupting what I was doing.",
                    cancels_mod_owned_queue=True,
                    cancels_running_action=True,
                )
            ]
        return []

    def _commands(
        self,
        previous: Observation | None,
        current: Observation,
        signals: ReflexSignals,
    ) -> list[SafetyEvent]:
        expired = tuple(
            command.command_id
            for command in signals.in_flight
            if signals.now_ms > command.deadline_ms
        )
        stalled = [
            command
            for command in signals.in_flight
            if command.command_id not in expired
            and signals.now_ms - command.last_progress_ms > self.config.stall_ms
        ]
        moved = previous is not None and _position_changed(previous, current)

        events: list[SafetyEvent] = []
        if expired:
            events.append(
                _event(
                    ReasonCode.LEASE_EXPIRED,
                    f"{len(expired)} command(s) ran out of time before finishing. Rejecting them.",
                    command_ids=expired,
                )
            )
        stuck = tuple(c.command_id for c in stalled if c.moves_character and not moved)
        no_progress = tuple(c.command_id for c in stalled if c.command_id not in stuck)
        if stuck:
            events.append(
                _event(
                    ReasonCode.PATH_STUCK,
                    "I am not getting anywhere on foot. Cancelling the move.",
                    cancels_mod_owned_queue=True,
                    cancels_running_action=may_cancel_running_action(current.action),
                    command_ids=stuck,
                )
            )
        if no_progress:
            events.append(
                _event(
                    ReasonCode.NO_PROGRESS,
                    f"{len(no_progress)} action(s) stopped making progress. Cancelling them.",
                    cancels_mod_owned_queue=True,
                    cancels_running_action=may_cancel_running_action(current.action),
                    command_ids=no_progress,
                )
            )
        return events


def _event(
    reason_code: ReasonCode,
    message: str,
    *,
    forces_disarm: bool = False,
    cancels_mod_owned_queue: bool = False,
    cancels_running_action: bool = False,
    command_ids: tuple[str, ...] = (),
) -> SafetyEvent:
    return SafetyEvent(
        priority=_PRIORITY[reason_code],
        reason_code=reason_code,
        message=message,
        forces_disarm=forces_disarm,
        cancels_mod_owned_queue=cancels_mod_owned_queue,
        cancels_running_action=cancels_running_action,
        command_ids=command_ids,
    )


def _first_per_reason(events: list[SafetyEvent]) -> list[SafetyEvent]:
    """Keep the most urgent event per reason code.

    Two rules can reach the same conclusion in one tick — the takeover flag and
    a manual action appearing, for instance. Reporting it twice would make a
    caller cancel twice and a voice adapter say it twice.
    """
    seen: set[ReasonCode] = set()
    kept: list[SafetyEvent] = []
    for event in events:
        if event.reason_code in seen:
            continue
        seen.add(event.reason_code)
        kept.append(event)
    return kept


def _position_changed(previous: Observation, current: Observation) -> bool:
    before = previous.player.position
    after = current.player.position
    return (before.x, before.y, before.z) != (after.x, after.y, after.z)
